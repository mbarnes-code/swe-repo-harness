"""SQLite → `MigrationState` → atomic `migration_state.json` (SPEC §6, §11.5, ADR-0012).

**`migration_state.json` is an OUTPUT of this module and NEVER an input to anything.** SQLite is
authoritative; the file is a projection for humans and `fleet status`. Nothing in the harness
reads it back to decide what to do — `fleet resume` regenerates it from SQLite — so a stale or
even absent file costs an operator one second of freshness and costs the run nothing.

**The projection does NOT run inside the state transition (§6).** At 250 repos × 4 phases with
two correlated `json_group_array` subqueries per row, plus serialization and `os.replace`,
running it inside a transition would hold SQLite's single write lock for hundreds of milliseconds
on each of ~4 000 transitions — sustained writer starvation well before ten workers. Instead:

* the transition commits **alone** through `StateWriter`;
* one dedicated `Projector` rebuilds the file from a **read** snapshot (`BEGIN DEFERRED` on a
  `mode=ro` handle, which under WAL never blocks the writer);
* rebuilds are **debounced to at most 1 Hz** (`MIN_PROJECTION_INTERVAL_S`), so a burst of
  transitions coalesces into one write;
* **all file I/O happens after the read transaction has ended** — `build_state()` takes no path
  and therefore cannot write, and `write_projection()` takes no connection and therefore cannot
  read. The split is the enforcement.

Staleness of up to one second is explicitly accepted. The write itself is atomic
(`util.fs.atomic_write`: temp file + `Path.replace`), so a crash mid-write leaves the previous
valid file intact rather than a torn one a human would read as truth.

Not projected, deliberately: `usage` and `budget_remaining_usd` are not derivable from SQLite
alone (`budget_remaining_usd` is `run_max_cost_usd` — config, §11.2 — minus the ledger), and
`MigrationWave.depends_on_waves` / `atomic_scc_ids` are sequencing intermediates that no table
carries. They keep their model defaults. A `waves` row with no members is skipped rather than
raised on: `MigrationWave` forbids an empty wave (§5), and a member-less row is not one.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict
from pathlib import Path
from types import TracebackType
from typing import Final
from uuid import UUID

import aiosqlite

from fleet.models.enums import Phase, RepoStatus
from fleet.models.graph import CollisionFinding, ContractNode, CycleFinding, MigrationWave
from fleet.models.state import MigrationState
from fleet.state.db import DEFAULT_DB_PATH, connect_ro
from fleet.util.fs import atomic_write

__all__ = [
    "DEFAULT_PROJECTION_PATH",
    "MIN_PROJECTION_INTERVAL_S",
    "ProjectionError",
    "Projector",
    "build_state",
    "project_once",
    "write_projection",
]

DEFAULT_PROJECTION_PATH: Final = Path("migration_state.json")

#: At most one rebuild per second (§6). The debounce is what keeps a 4 000-transition run from
#: paying for 4 000 projections, and what stops a reader from being re-opened in a tight loop.
MIN_PROJECTION_INTERVAL_S: Final = 1.0

_HOISTED_CONTRACT_STATUSES: Final = ("HOISTED", "MIGRATED", "FAILED")


class ProjectionError(RuntimeError):
    """The projection could not be built. Never swallowed (CLAUDE.md Rule 11)."""


# --------------------------------------------------------------------------------------
# the read side — one snapshot, no file I/O
# --------------------------------------------------------------------------------------

_SQL_RUN: Final = """
SELECT started_at, monorepo_branch, config_sha256, harness_version
  FROM runs WHERE run_id = ?
"""

#: §6's projection query, verbatim in shape, widened to the columns `PhaseRecord` needs so the
#: per-phase detail is complete rather than half-populated.
_SQL_PHASES: Final = """
SELECT p.repo_id, p.phase, p.status, p.attempts, p.max_attempts, p.transient_retries,
       p.failure_class, p.last_error, p.blocked_by, p.scc_id, p.pr_url,
       p.heartbeat_at, p.heartbeat_ttl_seconds, p.lease_owner, p.lease_fence,
       p.lease_expires_at, p.base_ref, p.pre_commit_sha, p.post_commit_sha,
       p.started_at, p.updated_at,
       r.dest_path, r.blast_radius, wm.wave_index,
       (SELECT json_group_array(DISTINCT e.dst_id)
          FROM edges e
         WHERE e.run_id = p.run_id AND e.src_kind = 'REPO' AND e.src_id = p.repo_id
           AND e.dst_kind = 'REPO' AND e.dst_id IS NOT NULL) AS depends_on,
       (SELECT json_group_array(DISTINCT e.dst_id)
          FROM edges e
         WHERE e.run_id = p.run_id AND e.src_kind = 'REPO' AND e.src_id = p.repo_id
           AND e.dst_kind = 'CONTRACT') AS depends_on_contracts
  FROM phases p
  JOIN repos r        ON r.repo_id = p.repo_id
  LEFT JOIN wave_members wm ON wm.run_id = p.run_id
                           AND wm.node_kind = 'REPO' AND wm.node_id = p.repo_id
 WHERE p.run_id = ?
 ORDER BY p.repo_id, p.phase
"""

_SQL_STUBS: Final = """
SELECT repo_id, stub_coord_key, state, revalidation_round
  FROM stubs WHERE run_id = ?
 ORDER BY repo_id, stub_coord_key, revalidation_round
"""

_SQL_WAVES: Final = """
SELECT wave_index, synthetic, wave_started_at, computed_at
  FROM waves WHERE run_id = ? ORDER BY wave_index
"""

_SQL_WAVE_MEMBERS: Final = """
SELECT wave_index, node_kind, node_id
  FROM wave_members WHERE run_id = ? ORDER BY wave_index, node_kind, node_id
"""

_SQL_CONTRACTS: Final = f"""
SELECT contract_id, kind, identifier, owning_repo_id, source_paths, generated_paths,
       consumer_repo_ids, extractable, extraction_confidence, confidence_factors,
       content_sha256, hoist_target_path, status, status_detail, detected_at
  FROM contracts
 WHERE run_id = ? AND status IN ({",".join("'" + s + "'" for s in _HOISTED_CONTRACT_STATUSES)})
 ORDER BY contract_id
"""  # noqa: S608 — the interpolated list is a module constant, not input

_SQL_CYCLES: Final = """
SELECT payload FROM findings
 WHERE run_id = ? AND kind = 'CycleDetected' ORDER BY fingerprint
"""

_SQL_COLLISIONS: Final = """
SELECT collision_id, kind, key, repo_ids, blob_shas, severity, resolution, detected_at
  FROM collisions WHERE run_id = ? ORDER BY kind, key
"""

_PHASE_RECORD_COLUMNS: Final[tuple[str, ...]] = (
    "phase",
    "status",
    "attempts",
    "max_attempts",
    "transient_retries",
    "failure_class",
    "last_error",
    "heartbeat_at",
    "heartbeat_ttl_seconds",
    "lease_owner",
    "lease_fence",
    "lease_expires_at",
    "base_ref",
    "pre_commit_sha",
    "post_commit_sha",
    "started_at",
    "updated_at",
)

type _Row = dict[str, object]


async def _rows(conn: aiosqlite.Connection, sql: str, params: tuple[object, ...]) -> list[_Row]:
    """Name-keyed rows without mutating the caller's `row_factory`."""
    async with conn.execute(sql, params) as cursor:
        names = [str(column[0]) for column in cursor.description]
        return [dict(zip(names, raw, strict=True)) for raw in await cursor.fetchall()]


def _json_list(value: object) -> list[str]:
    if not isinstance(value, str) or not value:
        return []
    parsed = json.loads(value)
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _json_obj(value: object) -> object:
    return json.loads(value) if isinstance(value, str) and value else {}


def _derive_updated_at(
    run: _Row,
    phase_rows: list[_Row],
    wave_rows: list[_Row],
    contract_rows: list[_Row],
    collision_rows: list[_Row],
) -> str:
    """`MigrationState.updated_at`, deterministically — the latest of the fixed-width UTC TEXT
    timestamps this function already read off `phases.updated_at`, `waves.computed_at`,
    `contracts.detected_at` and `collisions.detected_at`, falling back to `runs.started_at` for a
    run with none of those rows yet.

    Why not `default_factory=utcnow` at construction (the field's default, still correct for
    every OTHER model that carries it, e.g. a freshly built `PhaseRecord`/`RepoState` in code
    that isn't reading a persisted row): two projections of an untouched database must be
    byte-identical (SPEC §12.17), and wall-clock-at-build-time can never satisfy that — it
    differs on every call by construction, whether or not SQLite moved. These four columns are
    §11.5's fixed-width `_iso()` rendering ("the reaper compares these instants as TEXT",
    `state/repository.py::_iso`), so a lexicographic `max()` over them is a correct chronological
    max without parsing. This does not scan every table that can move `MigrationState` (budget
    ledger, events, stubs, attempts): it doesn't need to, because those fields' own values already
    carry any difference they make to the JSON dump — `updated_at`'s only job is to stop being a
    FALSE source of difference when nothing did, and a deterministic function of already-read rows
    does that by construction (ADR-0106).
    """
    candidates = [str(run["started_at"])]
    candidates.extend(str(row["updated_at"]) for row in phase_rows)
    candidates.extend(str(row["computed_at"]) for row in wave_rows)
    candidates.extend(str(row["detected_at"]) for row in contract_rows)
    candidates.extend(str(row["detected_at"]) for row in collision_rows)
    return max(candidates)


async def build_state(conn: aiosqlite.Connection, run_id: UUID) -> MigrationState:
    """Read one consistent snapshot and fold it into a `MigrationState`.

    `BEGIN DEFERRED` — never `IMMEDIATE`: this is a reader, and under WAL a deferred read
    snapshot runs concurrently with the single writer instead of queueing behind it (§11.5).
    Every row is fetched inside the transaction; every model is built *after* it commits, and
    this function has no path parameter at all, so no file I/O can occur under the snapshot.
    """
    rid = str(run_id)
    await conn.execute("BEGIN DEFERRED")
    try:
        run_rows = await _rows(conn, _SQL_RUN, (rid,))
        phase_rows = await _rows(conn, _SQL_PHASES, (rid,))
        stub_rows = await _rows(conn, _SQL_STUBS, (rid,))
        wave_rows = await _rows(conn, _SQL_WAVES, (rid,))
        member_rows = await _rows(conn, _SQL_WAVE_MEMBERS, (rid,))
        contract_rows = await _rows(conn, _SQL_CONTRACTS, (rid,))
        cycle_rows = await _rows(conn, _SQL_CYCLES, (rid,))
        collision_rows = await _rows(conn, _SQL_COLLISIONS, (rid,))
    except BaseException:
        await conn.rollback()
        raise
    await conn.commit()

    if not run_rows:
        raise ProjectionError(f"no runs row for run_id={rid}; nothing to project")
    run = run_rows[0]

    return MigrationState.model_validate(
        {
            "run_id": rid,
            "started_at": run["started_at"],
            "updated_at": _derive_updated_at(
                run, phase_rows, wave_rows, contract_rows, collision_rows
            ),
            "monorepo_branch": run["monorepo_branch"],
            "config_sha256": run["config_sha256"],
            "harness_version": run["harness_version"],
            "repos": _fold_repos(phase_rows, stub_rows),
            "waves": _fold_waves(wave_rows, member_rows),
            "contracts": [ContractNode.model_validate(_contract(row)) for row in contract_rows],
            "cycles": sorted(
                (CycleFinding.model_validate_json(str(row["payload"])) for row in cycle_rows),
                key=lambda finding: finding.scc_id,
            ),
            "collisions": [
                CollisionFinding.model_validate(
                    {
                        **row,
                        "repo_ids": _json_list(row["repo_ids"]),
                        "blob_shas": _json_list(row["blob_shas"]),
                    }
                )
                for row in collision_rows
            ],
        }
    )


def _fold_repos(phase_rows: list[_Row], stub_rows: list[_Row]) -> dict[str, _Row]:
    """Rows ordered by `(repo_id, phase)`; the highest phase reached wins the top level (§6)."""
    stub_states: dict[str, dict[str, str]] = defaultdict(dict)
    stub_rounds: dict[str, int] = defaultdict(int)
    for row in stub_rows:
        repo_id = str(row["repo_id"])
        stub_states[repo_id][str(row["stub_coord_key"])] = str(row["state"])
        stub_rounds[repo_id] = max(stub_rounds[repo_id], int(str(row["revalidation_round"])))

    phases: dict[str, dict[Phase, _Row]] = defaultdict(dict)
    top: dict[str, _Row] = {}
    for row in phase_rows:
        repo_id = str(row["repo_id"])
        phase = Phase(int(str(row["phase"])))
        phases[repo_id][phase] = {name: row[name] for name in _PHASE_RECORD_COLUMNS}
        status = RepoStatus(str(row["status"]))
        # `stubbed_deps` is non-empty iff DEGRADED (`RepoState._stub_invariants`), so the stub
        # projection is carried only while the repo is actually degraded; the audit trail of a
        # retired stub lives on `stubs`, which is why clearing it here is safe (§3.5.1).
        degraded_stubs = dict(stub_states[repo_id]) if status is RepoStatus.DEGRADED else {}
        top[repo_id] = {
            "phase": phase,
            "status": status,
            "attempts": row["attempts"],
            "last_error": row["last_error"],
            "blocked_by": sorted(_json_list(row["blocked_by"])),
            "depends_on": sorted(_json_list(row["depends_on"])),
            "depends_on_contracts": sorted(_json_list(row["depends_on_contracts"])),
            "blast_radius": row["blast_radius"],
            "stubbed_deps": sorted(degraded_stubs),
            "stub_states": degraded_stubs,
            "revalidation_rounds": stub_rounds[repo_id],
            "scc_id": row["scc_id"],
            "pr_url": row["pr_url"],
            "wave_index": row["wave_index"],
            "dest_path": row["dest_path"],
            "updated_at": row["updated_at"],
        }

    for repo_id, entry in top.items():
        entry["phases"] = phases[repo_id]
    return top


def _fold_waves(wave_rows: list[_Row], member_rows: list[_Row]) -> list[MigrationWave]:
    members: dict[int, dict[str, list[str]]] = defaultdict(lambda: {"REPO": [], "CONTRACT": []})
    for row in member_rows:
        members[int(str(row["wave_index"]))][str(row["node_kind"])].append(str(row["node_id"]))

    waves: list[MigrationWave] = []
    for row in wave_rows:
        index = int(str(row["wave_index"]))
        repo_ids = sorted(members[index]["REPO"])
        contract_ids = sorted(members[index]["CONTRACT"])
        if not repo_ids and not contract_ids:
            continue  # MigrationWave forbids an empty wave; a member-less row is not one
        waves.append(
            MigrationWave.model_validate(
                {
                    "wave_index": index,
                    "repo_ids": repo_ids,
                    "contract_ids": contract_ids,
                    "synthetic": bool(row["synthetic"]),
                    "wave_started_at": row["wave_started_at"],
                    "computed_at": row["computed_at"],
                }
            )
        )
    return waves


def _contract(row: _Row) -> _Row:
    return {
        **row,
        "source_paths": _json_obj(row["source_paths"]),
        "generated_paths": _json_obj(row["generated_paths"]),
        "consumer_repo_ids": _json_list(row["consumer_repo_ids"]),
        "confidence_factors": _json_obj(row["confidence_factors"]),
        "extractable": bool(row["extractable"]),
    }


# --------------------------------------------------------------------------------------
# the write side — no connection, therefore no lock
# --------------------------------------------------------------------------------------


async def write_projection(
    state: MigrationState, path: Path = DEFAULT_PROJECTION_PATH
) -> Path:
    """Serialize and `atomic_write` the projection. Takes no connection: it cannot hold a lock.

    The rename is atomic within a filesystem, so a crash mid-write leaves the previous valid
    file — never a truncated prefix a human would read as truth. Serialization and the blocking
    write are offloaded so a 250-repo dump cannot stall the event loop.
    """
    payload = await asyncio.to_thread(state.model_dump_json, indent=2)
    return await asyncio.to_thread(atomic_write, path, payload)


async def project_once(
    db_path: Path = DEFAULT_DB_PATH,
    *,
    run_id: UUID,
    path: Path = DEFAULT_PROJECTION_PATH,
) -> Path:
    """One rebuild: open a `mode=ro` handle, snapshot, close the transaction, then write."""
    conn = await connect_ro(db_path)
    try:
        state = await build_state(conn, run_id)
    finally:
        await conn.close()
    return await write_projection(state, path)


class Projector:
    """The single dedicated projector: one task, coalescing requests, at most 1 Hz (§6).

    `request()` is what a state transition calls. It never awaits, never opens a connection and
    never touches the filesystem — it sets a flag. Everything expensive happens on this task,
    outside the write transaction that triggered it::

        async with Projector(db_path, run_id=run_id, path=out) as projector:
            ...
            projector.request()   # after each transition; bursts coalesce into one write

    A failed rebuild does not kill the projector — staleness is survivable and the next request
    tries again — but it is not swallowed either: it is kept in `last_error` and re-raised by
    `aclose()` (CLAUDE.md Rule 11).
    """

    def __init__(
        self,
        db_path: Path = DEFAULT_DB_PATH,
        *,
        run_id: UUID,
        path: Path = DEFAULT_PROJECTION_PATH,
        min_interval_s: float = MIN_PROJECTION_INTERVAL_S,
    ) -> None:
        self._db_path = db_path
        self._run_id = run_id
        self._path = path
        self._min_interval_s = min_interval_s
        self._pending = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._closing = False
        self._last_write_at = float("-inf")
        self.writes = 0
        """Rebuilds actually written. Bursts coalesce, so this is < requests by construction."""
        self.last_error: BaseException | None = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._closing

    async def start(self) -> None:
        if self._task is not None:
            raise ProjectionError("projector is already started")
        self._task = asyncio.create_task(self._loop(), name="projector")

    async def aclose(self) -> None:
        """Stop the loop, then re-raise whatever the last failed rebuild raised, if any."""
        self._closing = True
        self._pending.set()
        if self._task is not None:
            await self._task
            self._task = None
        if self.last_error is not None:
            raise self.last_error

    async def __aenter__(self) -> Projector:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    def request(self) -> None:
        """Ask for a rebuild. Cheap, non-blocking, idempotent within the debounce window."""
        self._pending.set()

    def _stopping(self) -> bool:
        """A method, not the attribute: the flag is set by another task and must be re-read."""
        return self._closing

    async def _loop(self) -> None:
        while True:
            await self._pending.wait()
            if self._stopping():
                return
            self._pending.clear()
            delay = self._min_interval_s - (time.monotonic() - self._last_write_at)
            if delay > 0:
                await asyncio.sleep(delay)
            if self._stopping():
                return
            try:
                await project_once(self._db_path, run_id=self._run_id, path=self._path)
                self.writes += 1
            except Exception as exc:  # a stale projection is survivable; a silent one is not
                self.last_error = exc
            finally:
                self._last_write_at = time.monotonic()
