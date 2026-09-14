"""Behaviour tests for `src/fleet/state/projection.py` (SPEC §6, §11.5, ADR-0012).

Every property here is one whose *absence* is silent. A projection that runs inside the state
transition does not raise — it starves the single writer for hundreds of milliseconds per
transition, and the run merely gets slower and slower. An un-debounced projector does not raise
— it rebuilds 4 000 times. A non-atomic write does not raise — it leaves a truncated JSON file
that a human reads as the truth about a 250-repo migration. So the assertions are behavioural:
a real database, a real `StateWriter`, real files, and counted writes rather than sleeps.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import aiosqlite
import pytest

from fleet.models.enums import BreakStrategy, Phase, RepoStatus, StubState
from fleet.models.graph import CycleFinding
from fleet.models.state import MigrationState
from fleet.state import db as dbmod
from fleet.state import projection as projmod
from fleet.state.db import StateWriter, connect_ro, initialize_database
from fleet.state.projection import Projector, build_state, project_once, write_projection

RUN_ID = UUID("22222222-2222-4222-8222-222222222222")
RUN = str(RUN_ID)
TS = "2026-08-09T12:00:00+00:00"
EDGE = "1" * 64
EDGE2 = "2" * 64
EDGE3 = "3" * 64
SCC = "scc:" + "a" * 16


@pytest.fixture(autouse=True)
def _clean_write_slot() -> Iterator[None]:
    """A leaked write slot would make every later test fail for the wrong reason."""
    yield
    dbmod._release_write_slot()


def _read(path: Path) -> str:
    """Sync file access, deliberately outside the async test bodies (ruff ASYNC240)."""
    return path.read_text(encoding="utf-8")


def _read_bytes(path: Path) -> bytes:
    """Sync file access, deliberately outside the async test bodies (ruff ASYNC240)."""
    return path.read_bytes()


def _names(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir())


def _rename(src: Path, dst: Path) -> None:
    """Sync file access, deliberately outside the async test bodies (ruff ASYNC240)."""
    src.rename(dst)


def _seed(path: Path) -> None:
    """Populate the authoritative tables directly. Test setup only — the harness writes through
    `StateWriter`; here the point is to have rows the projection must reproduce faithfully."""
    cycle = CycleFinding(
        scc_id=SCC,
        members=["acme-billing", "acme-portal"],
        edges=[EDGE, EDGE2],
        broken_edge_keys=[EDGE2],
        break_strategy=BreakStrategy.EDGE_BREAK,
        rationale="cheapest break_cost",
    )
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO runs (run_id, started_at, config_sha256, harness_version, "
            "monorepo_branch) VALUES (?, ?, ?, ?, 'integration')",
            (RUN, TS, "a" * 64, "0.1.0"),
        )
        for repo_id, dest, radius in (
            ("acme-commons", "libs/com/acme/commons", 7),
            ("acme-billing", "services/billing", 1),
            ("acme-portal", "apps/portal", 0),
        ):
            conn.execute(
                "INSERT INTO repos (repo_id, name, url, dest_path, blast_radius, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (repo_id, repo_id, f"ssh://git/{repo_id}.git", dest, radius, TS),
            )
        phase_rows = (
            ("acme-commons", 1, "SUCCEEDED", 1, None, "[]"),
            ("acme-commons", 2, "REQUIRES_HUMAN_INTERVENTION", 3, "ast-grep rule miss", "[]"),
            ("acme-billing", 1, "BLOCKED", 0, None, '["acme-commons"]'),
            ("acme-portal", 1, "SUCCEEDED", 1, None, "[]"),
            ("acme-portal", 3, "DEGRADED", 1, None, "[]"),
        )
        for repo_id, phase, status, attempts, error, blocked in phase_rows:
            conn.execute(
                "INSERT INTO phases (run_id, repo_id, phase, status, attempts, last_error, "
                "blocked_by, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (RUN, repo_id, phase, status, attempts, error, blocked, TS),
            )
        edge_rows = (
            (EDGE, "acme-billing", "REPO", "acme-commons", "maven:com.acme:commons",
             "DECLARED_DEP"),
            (EDGE2, "acme-portal", "REPO", "acme-commons", "maven:com.acme:commons",
             "DECLARED_DEP"),
            (EDGE3, "acme-billing", "CONTRACT", "proto:acme.billing.v1", "proto:acme.billing.v1",
             "CONTRACT_CONSUME"),
        )
        for key, src, dst_kind, dst_id, coord, kind in edge_rows:
            conn.execute(
                "INSERT INTO edges (edge_key, run_id, src_kind, src_id, dst_kind, dst_id, "
                "dst_coord_key, kind, base_confidence, confidence, evidence_path, detected_at) "
                "VALUES (?, ?, 'REPO', ?, ?, ?, ?, ?, 0.9, 0.9, 'pom.xml', ?)",
                (key, RUN, src, dst_kind, dst_id, coord, kind, TS),
            )
        for index in (0, 1):
            conn.execute(
                "INSERT INTO waves (run_id, wave_index, computed_at, synthetic) "
                "VALUES (?, ?, ?, ?)",
                (RUN, index, TS, 1 if index else 0),
            )
        for index, kind, node in (
            (0, "REPO", "acme-commons"),
            (0, "CONTRACT", "proto:acme.billing.v1"),
            (1, "REPO", "acme-billing"),
            (1, "REPO", "acme-portal"),
        ):
            conn.execute(
                "INSERT INTO wave_members (run_id, wave_index, node_kind, node_id) "
                "VALUES (?, ?, ?, ?)",
                (RUN, index, kind, node),
            )
        conn.execute(
            "INSERT INTO contracts (run_id, contract_id, kind, identifier, owning_repo_id, "
            "extractable, extraction_confidence, hoist_target_path, status, detected_at) "
            "VALUES (?, 'proto:acme.billing.v1', 'PROTO', 'acme.billing.v1', 'acme-commons', "
            "1, 0.9, 'contracts/acme/billing/v1', 'HOISTED', ?)",
            (RUN, TS),
        )
        conn.execute(
            "INSERT INTO findings (run_id, kind, severity, fingerprint, payload, created_at) "
            "VALUES (?, 'CycleDetected', 'warn', ?, ?, ?)",
            (RUN, "f" * 64, cycle.model_dump_json(), TS),
        )
        conn.execute(
            "INSERT INTO collisions (run_id, kind, key, repo_ids, severity, resolution, "
            "detected_at) VALUES (?, 'DEST_PATH', 'apps/portal', ?, 'warn', 'suffixed', ?)",
            (RUN, json.dumps(["acme-portal", "acme-billing"]), TS),
        )
        conn.execute(
            "INSERT INTO stubs (stub_id, run_id, repo_id, stub_coord_key, consumer_repo_id, "
            "provider_repo_id, pinned_version, bazel_label, state, stub_fidelity, "
            "state_changed_at, created_at) VALUES ('stub-1', ?, 'acme-portal', "
            "'maven:com.acme:commons', 'acme-portal', 'acme-commons', '1.2.3', "
            "'//third_party/stubs/commons', 'ACTIVE', 'PUBLISHED_ARTIFACT', ?, ?)",
            (RUN, TS, TS),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
async def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "state" / "fleet.db"
    await initialize_database(path)
    _seed(path)
    return path


@pytest.fixture
def out_path(tmp_path: Path) -> Path:
    return tmp_path / "out" / "migration_state.json"


async def test_projection_reproduces_the_authoritative_tables(db_path: Path) -> None:
    """The projection is only useful if it says what SQLite says.

    Why: every field here is one an operator triages on — who is blocked, on whom, which stub,
    which wave. A projection that quietly drops `blocked_by` or `stub_states` looks healthy and
    is wrong, and nothing downstream would contradict it (the file is never read back, §11.5).
    """
    conn = await connect_ro(db_path)
    try:
        state = await build_state(conn, RUN_ID)
    finally:
        await conn.close()

    assert state.run_id == RUN_ID
    assert state.config_sha256 == "a" * 64

    commons = state.repos["acme-commons"]
    assert commons.phase is Phase.TRANSFORM  # highest phase reached wins the top level (§6)
    assert commons.status is RepoStatus.REQUIRES_HUMAN_INTERVENTION
    assert commons.attempts == 3
    assert set(commons.phases) == {Phase.SCAN, Phase.TRANSFORM}
    assert commons.phases[Phase.SCAN].status is RepoStatus.SUCCEEDED
    assert commons.dest_path == "libs/com/acme/commons"
    assert commons.blast_radius == 7
    assert commons.wave_index == 0

    billing = state.repos["acme-billing"]
    assert billing.blocked_by == ["acme-commons"]
    assert billing.depends_on == ["acme-commons"]
    assert billing.depends_on_contracts == ["proto:acme.billing.v1"]

    portal = state.repos["acme-portal"]
    assert portal.status is RepoStatus.DEGRADED
    assert portal.stubbed_deps == ["maven:com.acme:commons"]
    assert portal.stub_states == {"maven:com.acme:commons": StubState.ACTIVE}

    assert [w.wave_index for w in state.waves] == [0, 1]
    assert state.waves[0].contract_ids == ["proto:acme.billing.v1"]
    assert state.waves[1].synthetic is True
    assert [c.contract_id for c in state.contracts] == ["proto:acme.billing.v1"]
    assert [c.scc_id for c in state.cycles] == [SCC]
    assert state.cycles[0].broken_edge_keys == [EDGE2]
    assert [c.key for c in state.collisions] == ["apps/portal"]
    assert state.needs_human == ["acme-commons"]  # the computed triage list still computes


async def test_migration_state_json_round_trips_back_into_the_model(
    db_path: Path, out_path: Path
) -> None:
    """The file must be exactly the shape `MigrationState` parses.

    Why: `fleet resume` regenerates the projection and `fleet status` reads it; a file that only
    *looks* like the model — an enum dumped as an int key, a naive datetime — is discovered by an
    operator at the worst moment, not by the code that wrote it.
    """
    conn = await connect_ro(db_path)
    try:
        state = await build_state(conn, RUN_ID)
    finally:
        await conn.close()
    await write_projection(state, out_path)

    reloaded = MigrationState.model_validate_json(_read(out_path))
    assert reloaded == state


async def test_project_once_writes_the_file_atomically_and_leaves_no_temp(
    db_path: Path, out_path: Path
) -> None:
    """A torn projection is worse than a missing one: a human reads it as truth.

    Why: `atomic_write` renames a temp file into place, so a reader sees the whole old file or
    the whole new one. The stray-temp assertion is the part that rots silently — a leaked
    `.migration_state.json.*.tmp` next to the real file is indistinguishable from the real file
    to an operator with a glob.
    """
    await project_once(db_path, run_id=RUN_ID, path=out_path)
    first = _read(out_path)
    await project_once(db_path, run_id=RUN_ID, path=out_path)
    second = _read(out_path)

    assert _names(out_path.parent) == ["migration_state.json"]
    for payload in (first, second):
        MigrationState.model_validate_json(payload)  # fully old or fully new, never a prefix


async def test_two_projections_of_an_untouched_database_are_byte_identical(
    db_path: Path, out_path: Path
) -> None:
    """SPEC §12.17's literal claim, restored (round Y task 1, ADR-0106).

    `MigrationState.updated_at` carries `default_factory=utcnow`, which is correct for a
    freshly-constructed model but was, until ADR-0106, also what `build_state` fell back to for
    the top-level projection — so two regenerations of a database NOTHING had touched between
    them produced different files, on wall-clock time alone, and the criterion's "byte-identical"
    text was false by construction (`docs/SPEC.md` §12.17's round-K disclosure). `build_state`
    now derives `updated_at` from the latest of the durable `phases`/`waves`/`contracts`/
    `collisions` timestamps it already reads (`projection.py::_derive_updated_at`), which is a
    deterministic function of `db_path`'s current content — unchanged content, unchanged result.

    This asserts the whole-file bytes directly, not a field-scoped or model comparison: the
    criterion's own wording is literal byte-identity, and that is what must hold.
    """
    await project_once(db_path, run_id=RUN_ID, path=out_path)
    first = _read_bytes(out_path)
    await project_once(db_path, run_id=RUN_ID, path=out_path)
    second = _read_bytes(out_path)

    assert first == second, (
        "two projections of an untouched database produced different bytes; SPEC §12.17's "
        "byte-identity claim does not hold"
    )


async def test_a_failed_rename_leaves_the_previous_projection_intact(
    db_path: Path, out_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulate a crash at the rename: the old file survives and no temp file is orphaned.

    Why: this is the failure the temp-file dance exists for. If `atomic_write` wrote in place,
    the same interruption would leave a half-serialized 250-repo document on disk.
    """
    await project_once(db_path, run_id=RUN_ID, path=out_path)
    original = _read(out_path)

    def boom(self: Path, target: Any) -> Path:
        raise OSError("simulated crash during rename")

    monkeypatch.setattr(Path, "replace", boom)
    with pytest.raises(OSError, match="simulated crash"):
        await project_once(db_path, run_id=RUN_ID, path=out_path)

    assert _read(out_path) == original
    assert _names(out_path.parent) == ["migration_state.json"]


async def test_projector_debounces_a_burst_into_at_most_one_write(
    db_path: Path, out_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fifty transitions inside one second must not buy fifty projections (§6).

    Why: un-debounced, a 250-repo × 4-phase run rebuilds the file ~4 000 times, each rebuild two
    correlated subqueries per repo — the cost the debounce exists to delete. The assertion counts
    *observed* writes (a wrapper around `atomic_write`), not elapsed time.
    """
    observed: list[Path] = []
    real = projmod.atomic_write

    def counting(path: Any, data: Any, **kwargs: Any) -> Path:
        observed.append(Path(path))
        return real(path, data, **kwargs)

    monkeypatch.setattr(projmod, "atomic_write", counting)

    async with Projector(db_path, run_id=RUN_ID, path=out_path) as projector:
        for _ in range(50):
            projector.request()
        await asyncio.sleep(0.15)
        assert len(observed) <= 1, f"burst produced {len(observed)} writes"
        assert projector.writes <= 1

    assert observed  # ... and it did produce one: debounced, not disabled
    assert _names(out_path.parent) == ["migration_state.json"]


async def test_projector_coalesces_the_trailing_burst_into_one_more_write(
    db_path: Path, out_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Requests arriving during the debounce window collapse into exactly one later rebuild.

    Why: dropping them would make the file permanently stale after a busy moment; queueing them
    would defeat the debounce. Exactly one trailing rebuild is the contract, and it is what
    bounds staleness at one interval.
    """
    observed: list[Path] = []
    real = projmod.atomic_write

    def counting(path: Any, data: Any, **kwargs: Any) -> Path:
        observed.append(Path(path))
        return real(path, data, **kwargs)

    monkeypatch.setattr(projmod, "atomic_write", counting)

    async with Projector(
        db_path, run_id=RUN_ID, path=out_path, min_interval_s=0.2
    ) as projector:
        projector.request()
        await asyncio.sleep(0.05)  # leading-edge write has landed
        leading = len(observed)
        for _ in range(30):
            projector.request()
        await asyncio.sleep(0.35)
        assert leading == 1
        assert len(observed) == 2, f"30 queued requests produced {len(observed) - 1} rebuilds"
        assert projector.writes == 2


async def test_a_held_read_snapshot_does_not_block_the_single_writer(
    db_path: Path
) -> None:
    """The strongest available proof: an OPEN `BEGIN DEFERRED` snapshot, and a write lands anyway.

    Why: this is the defect the review corrected. Running the projection inside the transition
    held the one write lock for the length of the projection; running it as a WAL reader does not
    block the writer at all. The read transaction is deliberately still open across the write.
    """
    reader = await connect_ro(db_path)
    async with StateWriter(db_path, owner="test-writer") as writer:
        await reader.execute("BEGIN DEFERRED")
        async with reader.execute("SELECT COUNT(*) FROM phases") as cursor:
            assert await cursor.fetchone() is not None  # snapshot is now genuinely open

        async def insert(conn: aiosqlite.Connection) -> None:
            await conn.execute(
                "INSERT INTO events (run_id, seq, ts, level, event, event_uid) "
                "VALUES (?, 1, ?, 'INFO', 'x', 'uid-1')",
                (RUN, TS),
            )

        started = time.monotonic()
        await asyncio.wait_for(writer.submit(insert), timeout=5.0)
        elapsed = time.monotonic() - started
        await reader.commit()
    await reader.close()
    assert elapsed < 1.0, f"write waited {elapsed:.3f}s behind an open read snapshot"


async def test_writes_stay_prompt_while_a_projection_is_being_built(
    db_path: Path, out_path: Path
) -> None:
    """Writes submitted concurrently with a full rebuild must not queue behind it.

    Why: the failure mode is latency, not an exception — the pre-review design made every
    transition wait for a projection. Here the projection runs as a task while 20 transitions are
    submitted; each one's own latency is measured, so a serialized design shows up as a slow
    write rather than as a passing test.
    """
    async with StateWriter(db_path, owner="test-writer") as writer:
        latencies: list[float] = []

        async def write(seq: int) -> None:
            async def unit(conn: aiosqlite.Connection) -> None:
                await conn.execute(
                    "INSERT INTO events (run_id, seq, ts, level, event, event_uid) "
                    "VALUES (?, ?, ?, 'INFO', 'x', ?)",
                    (RUN, seq, TS, f"uid-{seq}"),
                )

            started = time.monotonic()
            await writer.submit(unit)
            latencies.append(time.monotonic() - started)

        projection = asyncio.create_task(
            project_once(db_path, run_id=RUN_ID, path=out_path)
        )
        await asyncio.gather(*(write(seq) for seq in range(1, 21)))
        await projection

    assert len(latencies) == 20
    assert max(latencies) < 1.0, f"slowest write took {max(latencies):.3f}s"
    assert _names(out_path.parent) == ["migration_state.json"]


def test_build_state_cannot_perform_file_io_by_construction() -> None:
    """Structural, and stated as such: `build_state` takes no path, `write_projection` takes no
    connection. The split is what guarantees "all file I/O after the read transaction ends" (§6);
    a runtime assertion could only observe one ordering of one run, so this asserts the shape
    that makes the wrong ordering unexpressible.
    """
    import inspect

    read_params = set(inspect.signature(build_state).parameters)
    write_params = set(inspect.signature(write_projection).parameters)
    assert read_params == {"conn", "run_id"}
    assert write_params == {"state", "path"}


async def test_projector_records_a_failed_rebuild_and_re_raises_it_on_close(
    tmp_path: Path, out_path: Path
) -> None:
    """A projection failure is survivable but never silent (CLAUDE.md Rule 11).

    Why: the projector must not die on one bad rebuild — staleness is accepted — but a run that
    quietly stopped projecting for an hour is a lie by omission at triage time.
    """
    missing = tmp_path / "state" / "absent.db"
    missing.parent.mkdir(parents=True, exist_ok=True)
    await initialize_database(missing)  # a real, EMPTY database: no runs row for RUN_ID

    projector = Projector(missing, run_id=RUN_ID, path=out_path, min_interval_s=0.01)
    await projector.start()
    projector.request()
    await asyncio.sleep(0.1)
    assert projector.writes == 0
    assert isinstance(projector.last_error, projmod.ProjectionError)
    with pytest.raises(projmod.ProjectionError):
        await projector.aclose()


async def test_projector_clears_last_error_after_a_later_successful_rebuild(
    db_path: Path, out_path: Path
) -> None:
    """A later success resolves an earlier failure (334edeb): `last_error` must not outlive it.

    Why: `aclose()` re-raises whatever `last_error` holds (Rule 11) — exactly right while the
    projection is genuinely stale, but wrong forever after a SUBSEQUENT successful rebuild wrote
    a fresh, correct file. Before 334edeb, a database briefly unreachable (a transient disk
    hiccup, a lagging mount) left `last_error` set for the rest of the run: every later
    `aclose()` would re-raise a resolved error even though `writes` had already moved past it —
    a false-positive Rule-11 alarm on a run that was, by then, actually fine.
    """
    moved = db_path.with_suffix(".moved")
    _rename(db_path, moved)  # the DB is briefly unreachable -- the failure this simulates

    projector = Projector(db_path, run_id=RUN_ID, path=out_path, min_interval_s=0.01)
    await projector.start()
    projector.request()
    await asyncio.sleep(0.1)
    error_after_outage = projector.last_error
    assert projector.writes == 0
    assert error_after_outage is not None, "the outage must be recorded, not swallowed"

    _rename(moved, db_path)  # the DB is back
    projector.request()
    await asyncio.sleep(0.1)
    # Re-read via a fresh local binding, not the same `projector.last_error` expression narrowed
    # above: mypy's flow analysis has no model of the background `_loop` task mutating this
    # attribute between awaits, so re-checking the same narrowed expression makes it (wrongly)
    # conclude the code below is unreachable (`warn_unreachable`, reproduced against a minimal
    # standalone file before landing this form).
    error_after_recovery = projector.last_error
    assert projector.writes == 1, "the recovered rebuild must actually run"
    assert error_after_recovery is None, (
        "a later success must resolve the earlier failure, or aclose() re-raises a stale error"
    )

    await projector.aclose()  # must not raise: the last rebuild succeeded
