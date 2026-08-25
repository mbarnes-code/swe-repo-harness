"""Wave admission and blast containment (SPEC §3.5, §3.4's per-wave wall clock, §11.1).

Four rules, and the scheduler is exactly their implementation:

1. **A wave opens only when its predecessor has closed.** "Wave 2 cannot open before wave 1
   closes" is the whole ordering guarantee the topological sequencer bought; a runner that
   fanned out wave 2 while wave 1 still had a `PENDING` member would migrate a dependent
   against a dependency that had not landed.
2. **Within a wave, admission is by blast radius, descending.** `repos.blast_radius` is
   `|descendants(G_order, r)|` — the repos that transitively depend on `r` — so admitting the
   widest first surfaces the expensive failures while the run still has budget to react (§3.5).
3. **A `BLOCKED` repo is not admitted.** It is waiting on an abandoned ancestor; dispatching it
   spends attempts on work that cannot pass.
4. **The per-wave wall clock is CUMULATIVE across resumes.** It is measured from the persisted
   `waves.wave_started_at`, never from process start, or a crash-loop buys unbounded time
   (§3.4). On breach the wave stops admitting and is left `PARTIAL`: members never admitted
   stay `PENDING` with no attempt consumed and no `blocked_by`. **Nothing re-admits them
   today** — this rule used to claim `fleet resume` does. `fleet resume`'s step 8 has no
   implementation, and `wave_started_at` is stamped once by `begin_wave`'s `COALESCE` and
   never cleared, so a later scheduler over the same wave reads the same breach — including
   a scheduler for a DIFFERENT phase, because `waves` has no phase column while this class is
   one instance per (run, phase). `docs/SPEC.md` §3.4 USED TO state re-admission as intent
   against its own budget-table row; `f54dac8` moved the prose half, so the SPEC now agrees with
   this rule and that question is decided. What D82 still records as OPEN is a different
   question — whether sharing one wall clock across a wave's four phases is intended at all —
   and this rule does not answer it.

`blocked_by` propagation (§3.5) lives here too, stated once and implemented once: the
descendants over the **ordering subgraph only** are appended by set union, never replacement,
and a repo already terminal is left alone.

Everything durable goes through the injected `SchedulerStore`; the SQLite implementation writes
exclusively through the run's single `StateWriter` (§11.5) and reads through its `mode=ro`
handle, so the scheduler adds no second writer.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Final, Protocol, runtime_checkable

from fleet.models.enums import TERMINAL_STATUSES, Phase, RepoStatus

if TYPE_CHECKING:
    import aiosqlite

    from fleet.graph.sequence import WavePlan
    from fleet.settings import BudgetsSection
    from fleet.state.db import StateWriter
    from fleet.state.repository import ReadOnlyRepository

__all__ = [
    "Admission",
    "SchedulerStore",
    "SqliteSchedulerStore",
    "WaveNotReadyError",
    "WaveScheduler",
    "WaveState",
    "ordering_descendants",
]

#: A wave member is "settled" for the purposes of closing a wave when nothing further will be
#: dispatched for it in this wave. `BLOCKED` and `DEGRADED` are NOT in `TERMINAL_STATUSES`
#: (both are resolvable, §3.5.1), but neither is dispatchable now, and a wave that waited for
#: them would never close — the resolution arrives as a SYNTHETIC wave, by construction.
SETTLED_STATUSES: Final = frozenset(
    {*TERMINAL_STATUSES, RepoStatus.BLOCKED, RepoStatus.DEGRADED}
)


class WaveState(StrEnum):
    """Where a wave stands. `PARTIAL` is normative, not an implementation detail (§3.4)."""

    OPEN = "OPEN"        # members still dispatchable
    CLOSED = "CLOSED"    # every member settled; the next wave may open
    PARTIAL = "PARTIAL"  # the wall clock breached with members left; they stay PENDING


class WaveNotReadyError(RuntimeError):
    """Wave N+1 was opened while wave N still had a dispatchable member (§3.1 step 7)."""


@dataclass(frozen=True, slots=True)
class Admission:
    """One wave's admission decision, in the order the runner must fan out.

    `withheld` is the point of the type: a breach must leave those members visibly *not
    admitted* rather than quietly absent, because §3.4 promises they keep their attempts.
    """

    wave_index: int
    phase: Phase
    admitted: tuple[str, ...]
    blocked: tuple[str, ...] = ()
    settled: tuple[str, ...] = ()
    withheld: tuple[str, ...] = ()
    started_at: datetime | None = None
    elapsed_s: float = 0.0
    breached: bool = False


# --------------------------------------------------------------------------------------
# the durable surface — narrow by design (Guardrail 3)
# --------------------------------------------------------------------------------------


@runtime_checkable
class SchedulerStore(Protocol):
    """The `waves` / `wave_members` / `repos.blast_radius` / `phases.blocked_by` reads and
    writes the scheduler needs, and nothing else. Deliberately narrower than `StateRepository`:
    a scheduler handed the full repository could insert symbols."""

    async def record_plan(
        self, run_id: str, plan: WavePlan, *, now: datetime, max_usd_per_repo: float
    ) -> None: ...

    async def wave_indices(self, run_id: str) -> tuple[int, ...]: ...

    async def wave_members(self, run_id: str, wave_index: int) -> tuple[str, ...]: ...

    async def wave_started_at(self, run_id: str, wave_index: int) -> datetime | None: ...

    async def begin_wave(self, run_id: str, wave_index: int, *, now: datetime) -> datetime: ...

    async def blast_radii(
        self, run_id: str, repo_ids: Sequence[str]
    ) -> Mapping[str, int]: ...

    async def append_blocked_by(
        self, run_id: str, repo_id: str, blocker: str, *, now: datetime
    ) -> int: ...

    async def append_unblocked_wave(
        self,
        run_id: str,
        repo_ids: Sequence[str],
        *,
        now: datetime,
        max_usd_per_repo: float,
    ) -> int | None: ...


def _iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        raise ValueError(f"naive datetime {moment!r}: every persisted instant is UTC (§11.5)")
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def _parse(stamp: object) -> datetime | None:
    if stamp is None:
        return None
    parsed = datetime.fromisoformat(str(stamp))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


class SqliteSchedulerStore:
    """`SchedulerStore` over the run's single writer and its read-only connection."""

    def __init__(self, *, writer: StateWriter, read_conn: aiosqlite.Connection) -> None:
        self._writer = writer
        self._read = read_conn

    async def record_plan(
        self, run_id: str, plan: WavePlan, *, now: datetime, max_usd_per_repo: float
    ) -> None:
        """Persist `waves` + `wave_members` for a computed plan.

        A re-plan PRESERVES `wave_started_at` (§6): the column is omitted from the upsert's
        `DO UPDATE` clause, so `fleet sequence` re-run mid-flight cannot restart a wave's
        cumulative wall clock — which is the one thing that would make the §3.4 halt unbounded.
        """
        stamp = _iso(now)
        waves = [
            (
                run_id,
                wave.wave_index,
                stamp,
                1 if wave.synthetic else 0,
                max_usd_per_repo * (len(wave.repo_ids) + len(wave.contract_ids)),
            )
            for wave in plan.waves
        ]
        members = [
            (run_id, wave.wave_index, kind, node_id)
            for wave in plan.waves
            for kind, ids in (("REPO", wave.repo_ids), ("CONTRACT", wave.contract_ids))
            for node_id in ids
        ]

        async def unit(conn: aiosqlite.Connection) -> None:
            await conn.executemany(
                "INSERT INTO waves (run_id, wave_index, computed_at, synthetic, max_usd) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (run_id, wave_index) DO UPDATE SET "
                "    computed_at = excluded.computed_at, synthetic = excluded.synthetic, "
                "    max_usd = excluded.max_usd",
                waves,
            )
            await conn.executemany(
                "INSERT INTO wave_members (run_id, wave_index, node_kind, node_id) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT (run_id, node_kind, node_id) DO UPDATE SET "
                "    wave_index = excluded.wave_index",
                members,
            )

        await self._writer.submit(unit)

    async def wave_indices(self, run_id: str) -> tuple[int, ...]:
        sql = "SELECT wave_index FROM waves WHERE run_id = ? ORDER BY wave_index"
        async with self._read.execute(sql, (run_id,)) as cursor:
            return tuple(int(row[0]) for row in await cursor.fetchall())

    async def wave_members(self, run_id: str, wave_index: int) -> tuple[str, ...]:
        sql = (
            "SELECT node_id FROM wave_members "
            " WHERE run_id = ? AND wave_index = ? AND node_kind = 'REPO' ORDER BY node_id"
        )
        async with self._read.execute(sql, (run_id, wave_index)) as cursor:
            return tuple(str(row[0]) for row in await cursor.fetchall())

    async def wave_started_at(self, run_id: str, wave_index: int) -> datetime | None:
        sql = "SELECT wave_started_at FROM waves WHERE run_id = ? AND wave_index = ?"
        async with self._read.execute(sql, (run_id, wave_index)) as cursor:
            row = await cursor.fetchone()
        return None if row is None else _parse(row[0])

    async def begin_wave(self, run_id: str, wave_index: int, *, now: datetime) -> datetime:
        """Stamp the first admission, or return the one a previous process already stamped.

        `COALESCE` in one statement, not read-then-write: two orchestrators racing a resume must
        not each decide the wave started now, which would silently reset the cumulative clock.
        """
        stamp = _iso(now)

        async def unit(conn: aiosqlite.Connection) -> str:
            await conn.execute(
                "UPDATE waves SET wave_started_at = COALESCE(wave_started_at, ?) "
                " WHERE run_id = ? AND wave_index = ?",
                (stamp, run_id, wave_index),
            )
            async with conn.execute(
                "SELECT wave_started_at FROM waves WHERE run_id = ? AND wave_index = ?",
                (run_id, wave_index),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                raise WaveNotReadyError(
                    f"wave {wave_index} of run {run_id} has no `waves` row: a wave cannot be "
                    "opened before `fleet sequence` planned it"
                )
            return str(row[0])

        started = _parse(await self._writer.submit(unit))
        assert started is not None  # noqa: S101 - COALESCE cannot yield NULL here
        return started

    async def blast_radii(self, run_id: str, repo_ids: Sequence[str]) -> Mapping[str, int]:
        """`repos.blast_radius`, precomputed after Phase 1 (§3.5) — never derived here."""
        if not repo_ids:
            return {}
        placeholders = ",".join("?" for _ in repo_ids)
        sql = f"SELECT repo_id, blast_radius FROM repos WHERE repo_id IN ({placeholders})"  # noqa: S608
        async with self._read.execute(sql, tuple(repo_ids)) as cursor:
            rows = await cursor.fetchall()
        return {str(row[0]): int(row[1]) for row in rows}

    async def append_blocked_by(
        self, run_id: str, repo_id: str, blocker: str, *, now: datetime
    ) -> int:
        """Set-union `blocker` into every non-`SUCCEEDED` phase of `repo_id` and mark it BLOCKED.

        Union, never replacement (§3.5): a repo blocked by three abandoned ancestors lists all
        three, and the whole operation is idempotent, so a resume that re-derives propagation
        writes the same rows rather than doubling them.
        """
        stamp = _iso(now)

        async def unit(conn: aiosqlite.Connection) -> int:
            async with conn.execute(
                "SELECT phase, status, blocked_by FROM phases WHERE run_id = ? AND repo_id = ?",
                (run_id, repo_id),
            ) as cursor:
                rows = list(await cursor.fetchall())
            touched = 0
            for phase, status, blocked_by in rows:
                current = RepoStatus(str(status))
                if current is RepoStatus.SUCCEEDED or current in TERMINAL_STATUSES:
                    continue
                names = set(json.loads(str(blocked_by) or "[]"))
                names.add(blocker)
                await conn.execute(
                    "UPDATE phases SET blocked_by = ?, status = 'BLOCKED', updated_at = ? "
                    " WHERE run_id = ? AND repo_id = ? AND phase = ?",
                    (json.dumps(sorted(names)), stamp, run_id, repo_id, int(phase)),
                )
                touched += 1
            return touched

        return await self._writer.submit(unit)

    async def append_unblocked_wave(
        self,
        run_id: str,
        repo_ids: Sequence[str],
        *,
        now: datetime,
        max_usd_per_repo: float,
    ) -> int | None:
        """Append ONE synthetic wave above every existing index and MOVE `repo_ids` into it.

        Step 6's writer (§11.5): the repos a `blocked_by` recompute has freed already carry a
        `wave_index`, so they are moved, never inserted fresh. That is why this is not
        `graph.sequence.append_synthetic_waves` — that function filters to refs carrying no
        wave index, so on this population it returns its input unchanged (SPEC §3.5's scope
        note; ADR-0090 ruling W).

        Two properties it exists to hold structurally rather than by convention:

        * **The index is allocated INSIDE the write transaction.** `MAX(wave_index) + 1` is
          read in the same `BEGIN IMMEDIATE` that inserts the row, never read into Python and
          passed back down — the read-then-write form is what makes a second resume compute
          the offset the first one already took and collide with the row it wrote.
        * **No existing `waves` row is read, modified or upserted.** The only `waves` statement
          is an INSERT of the new index, so a closed wave's frozen `max_usd` and its
          `wave_started_at` cannot be re-budgeted or restarted as a side effect of moving one
          member — the hazard `record_plan`'s `DO UPDATE` carries, and the reason step 6 is not
          routed through it.

        `max_usd` is derived exactly as `record_plan` derives it (§11.2:
        `budgets.wave_max_cost_usd_per_repo` × member count), because a wave inserted at the
        column default would carry a zero ceiling and halt on its first admission.

        Returns the appended `wave_index`, or `None` when `repo_ids` is empty — an empty
        appended wave is a row that closes on sight and claims a membership it has not got.
        Raises `WaveNotReadyError` if any named repo has no `wave_members` row: this method
        moves members, and admitting a repo the sequencer never planned is not its job.
        """
        stamp = _iso(now)
        targets = tuple(sorted(set(repo_ids)))
        if not targets:
            return None
        placeholders = ",".join("?" for _ in targets)
        ceiling = max_usd_per_repo * len(targets)

        async def unit(conn: aiosqlite.Connection) -> int:
            async with conn.execute(
                "SELECT node_id FROM wave_members "  # noqa: S608 - placeholders are '?' only
                f" WHERE run_id = ? AND node_kind = 'REPO' AND node_id IN ({placeholders})",
                (run_id, *targets),
            ) as cursor:
                present = {str(row[0]) for row in await cursor.fetchall()}
            missing = sorted(set(targets) - present)
            if missing:
                raise WaveNotReadyError(
                    f"run {run_id} has no `wave_members` row for {missing}: an appended wave "
                    "MOVES repos an earlier wave already holds, so a repo the sequencer never "
                    "planned cannot be admitted by un-blocking it"
                )
            async with conn.execute(
                "SELECT COALESCE(MAX(wave_index), -1) + 1 FROM waves WHERE run_id = ?",
                (run_id,),
            ) as cursor:
                row = await cursor.fetchone()
            assert row is not None  # noqa: S101 - an aggregate returns exactly one row
            wave_index = int(row[0])
            await conn.execute(
                "INSERT INTO waves (run_id, wave_index, computed_at, synthetic, max_usd) "
                "VALUES (?, ?, ?, 1, ?)",
                (run_id, wave_index, stamp, ceiling),
            )
            await conn.execute(
                "UPDATE wave_members SET wave_index = ? "  # noqa: S608 - as above
                f" WHERE run_id = ? AND node_kind = 'REPO' AND node_id IN ({placeholders})",
                (wave_index, run_id, *targets),
            )
            return wave_index

        return await self._writer.submit(unit)


# --------------------------------------------------------------------------------------
# the policy
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class WaveScheduler:
    """Decides what may run next, and what a failure costs. One instance per (run, phase)."""

    run_id: str
    phase: Phase
    store: SchedulerStore
    db: ReadOnlyRepository
    budgets: BudgetsSection
    clock: Callable[[], datetime]
    descendants: Callable[[str], Collection[str]] = lambda _repo_id: ()
    """Ordering-subgraph descendants of a repo — `graph/query.py`'s job, injected so the
    scheduler does not carry a `networkx` graph it would only ever ask one question of."""
    _statuses: dict[str, RepoStatus] = field(default_factory=dict, repr=False)

    # ---------------------------------------------------------------- reads

    async def status_of(self, repo_id: str) -> RepoStatus:
        """This repo's status for the driving phase. A phase row that does not exist yet is
        `PENDING`: the sequencer planned the wave, the phase row is created on admission."""
        row = await self.db.get_phase(self.run_id, repo_id, self.phase)
        return RepoStatus.PENDING if row is None else row.status

    async def wave_state(self, wave_index: int) -> WaveState:
        """`CLOSED` when every member is settled; otherwise `PARTIAL` if the wall clock has
        already breached (nothing more will be admitted), else `OPEN`."""
        members = await self.store.wave_members(self.run_id, wave_index)
        for repo_id in members:
            if await self.status_of(repo_id) not in SETTLED_STATUSES:
                return WaveState.PARTIAL if await self.breached(wave_index) else WaveState.OPEN
        return WaveState.CLOSED

    async def elapsed_s(self, wave_index: int) -> float:
        """Seconds since the PERSISTED first admission — cumulative across resumes (§3.4)."""
        started = await self.store.wave_started_at(self.run_id, wave_index)
        if started is None:
            return 0.0
        return max(0.0, (self.clock() - started).total_seconds())

    async def breached(self, wave_index: int) -> bool:
        """Has `budgets.wave_max_wallclock_s` been spent? The predicate the runner polls between
        admissions, so a long wave stops admitting the moment it breaches rather than at the
        next wave boundary."""
        started = await self.store.wave_started_at(self.run_id, wave_index)
        if started is None:
            return False
        return await self.elapsed_s(wave_index) >= self.budgets.wave_max_wallclock_s

    # ---------------------------------------------------------------- gates

    async def open_wave(self, wave_index: int) -> datetime:
        """Open the wave and return its (possibly inherited) `wave_started_at`.

        Raises `WaveNotReadyError` if any earlier wave is not `CLOSED`. The check is over every
        earlier index rather than only `wave_index - 1`, because a `PARTIAL` wave 0 left behind
        by an exit-4 halt must not be stepped over by opening wave 2.
        """
        for earlier in await self.store.wave_indices(self.run_id):
            if earlier >= wave_index:
                break
            state = await self.wave_state(earlier)
            if state is not WaveState.CLOSED:
                raise WaveNotReadyError(
                    f"wave {wave_index} cannot open: wave {earlier} is {state} — a dependent "
                    "migrated against a dependency that has not landed is the failure the "
                    "topological sequencer exists to prevent (§3.1 step 7)"
                )
        return await self.store.begin_wave(self.run_id, wave_index, now=self.clock())

    async def admit(self, wave_index: int) -> Admission:
        """The wave's dispatch list, highest blast radius first.

        Not admitted, and each for its own reason: a `BLOCKED` member waits on an abandoned
        ancestor; a settled member already has its verdict; and every member past a wall-clock
        breach is `withheld` — still `PENDING`, no attempt consumed (§3.4).
        """
        started = await self.open_wave(wave_index)
        members = await self.store.wave_members(self.run_id, wave_index)
        radii = await self.store.blast_radii(self.run_id, members)

        blocked: list[str] = []
        settled: list[str] = []
        candidates: list[str] = []
        for repo_id in members:
            status = await self.status_of(repo_id)
            if status is RepoStatus.BLOCKED:
                blocked.append(repo_id)
            elif status in SETTLED_STATUSES:
                settled.append(repo_id)
            else:
                candidates.append(repo_id)

        ordered = tuple(
            sorted(candidates, key=lambda repo_id: (-radii.get(repo_id, 0), repo_id))
        )
        breached = await self.breached(wave_index)
        return Admission(
            wave_index=wave_index,
            phase=self.phase,
            admitted=() if breached else ordered,
            blocked=tuple(blocked),
            settled=tuple(settled),
            withheld=ordered if breached else (),
            started_at=started,
            elapsed_s=await self.elapsed_s(wave_index),
            breached=breached,
        )

    async def may_admit(self, wave_index: int) -> bool:
        """Polled between admissions inside a live wave. `False` stops admitting; it never
        cancels what is already in flight — that work is paid for and is allowed to drain."""
        return not await self.breached(wave_index)

    # ---------------------------------------------------------------- blast containment

    async def propagate_blocked(self, abandoned_repo_id: str) -> frozenset[str]:
        """§3.5, verbatim: append `r` to `blocked_by` for exactly its transitive dependents.

        Exactly the descendants over the ordering subgraph — no more, no less. Repos outside
        `D` are untouched, and a dependent already terminal keeps its own verdict rather than
        having a second cause written over it.
        """
        now = self.clock()
        blocked: set[str] = set()
        for dependent in sorted(set(self.descendants(abandoned_repo_id))):
            if dependent == abandoned_repo_id:
                continue
            if await self.store.append_blocked_by(
                self.run_id, dependent, abandoned_repo_id, now=now
            ):
                blocked.add(dependent)
        return frozenset(blocked)


def ordering_descendants(
    edges: Iterable[tuple[str, str]],
) -> Callable[[str], Collection[str]]:
    """Build a `descendants` callable from `(dependency, dependent)` pairs of the **ordering
    subgraph** — `G_rev`, so an edge runs dependency → dependent and the transitive closure is
    "who breaks if this repo is abandoned" (§3.5). Computed on the fly rather than cached: the
    ordering subgraph is small next to the fleet, and a cached closure that outlived one
    `blocked_by` reversal is exactly how a fixed repo stays blocked forever.
    """
    forward: dict[str, set[str]] = {}
    for dependency, dependent in edges:
        forward.setdefault(dependency, set()).add(dependent)

    def descendants(repo_id: str) -> Collection[str]:
        seen: set[str] = set()
        stack = list(forward.get(repo_id, ()))
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            stack.extend(forward.get(node, ()))
        return seen

    return descendants
