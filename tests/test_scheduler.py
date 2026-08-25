"""Behaviour tests for `src/fleet/orchestrator/scheduler.py` (SPEC §3.4, §3.5, §11.1).

Every property here is one whose absence is silent, which is why each is pinned against a real
temp database rather than against the source:

* Opening wave N+1 while wave N still has a `PENDING` member does not raise on its own — it
  migrates a dependent against a dependency that never landed, and the failure surfaces as a
  Bazel link error twenty minutes later in a container.
* Admitting in `repo_id` order instead of blast-radius order does not raise either. It just
  spends the run's budget on leaves and discovers that `acme-commons` gates 187 repos at wave 9,
  when there is nothing left to react with.
* Restarting the wave clock on resume does not raise. It converts `wave_max_wallclock_s` from a
  bound into a suggestion, because a crash-loop buys four fresh hours every time.
* Losing an unadmitted member on a wall-clock breach does not raise. The repo simply never
  migrates, with no attempt consumed and nothing naming it.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from fleet.graph.sequence import WavePlan
from fleet.models.enums import Phase, RepoStatus
from fleet.models.graph import MigrationWave
from fleet.orchestrator.scheduler import (
    SqliteSchedulerStore,
    WaveNotReadyError,
    WaveScheduler,
    WaveState,
    ordering_descendants,
)
from fleet.settings import BudgetsSection
from fleet.state import db as dbmod
from fleet.state.db import StateWriter, connect_ro, initialize_database
from fleet.state.repository import SqliteStateRepository

NOW = datetime(2026, 8, 9, 12, 0, 0, tzinfo=UTC)
RUN = "22222222-2222-4222-8222-222222222222"
PHASE = Phase.TRANSFORM

# wave 0 = three repos with deliberately non-alphabetical blast radii; wave 1 = one dependent.
WAVE_0 = {"acme-commons": 187, "acme-auth": 42, "acme-batch": 0}
WAVE_1 = {"acme-portal": 3}


class SteppableClock:
    """A wall clock a test moves by hand. The scheduler's cumulative wave budget is four hours;
    asserting it with `time.sleep` is not a test, it is an outage."""

    def __init__(self, start: datetime = NOW) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture(autouse=True)
def _clean_write_slot() -> Iterator[None]:
    """A leaked single-writer slot fails every later test for the wrong reason."""
    yield
    dbmod._release_write_slot()


Wired = tuple[SqliteStateRepository, SqliteSchedulerStore, aiosqlite.Connection]


@pytest.fixture
async def wired(tmp_path: Path) -> AsyncIterator[Wired]:
    """A run with two planned waves, wired exactly as the orchestrator wires it."""
    path = tmp_path / "state" / "fleet.db"
    await initialize_database(path)
    async with StateWriter(path, owner="test-scheduler") as writer:
        read_conn = await connect_ro(path)
        try:
            repo = SqliteStateRepository(writer=writer, read_conn=read_conn)
            store = SqliteSchedulerStore(writer=writer, read_conn=read_conn)
            await repo.upsert_run(
                RUN, started_at=NOW, config_sha256="a" * 64, harness_version="0.1.0"
            )
            for repo_id, radius in {**WAVE_0, **WAVE_1}.items():
                await repo.upsert_repo(
                    repo_id, name=repo_id, url=f"https://example.invalid/{repo_id}.git", now=NOW
                )
                await repo.upsert_phase(RUN, repo_id, PHASE, now=NOW)
                await _set_blast_radius(writer, repo_id, radius)
            await store.record_plan(RUN, _plan(), now=NOW, max_usd_per_repo=8.0)
            yield repo, store, read_conn
        finally:
            await read_conn.close()


def _plan() -> WavePlan:
    return WavePlan(
        waves=(
            MigrationWave(wave_index=0, repo_ids=sorted(WAVE_0)),
            MigrationWave(wave_index=1, repo_ids=sorted(WAVE_1), depends_on_waves=[0]),
        ),
        wave_index_by_node={},
        cycle_findings=(),
        excluded_repo_ids=(),
    )


async def _set_blast_radius(writer: StateWriter, repo_id: str, radius: int) -> None:
    async def unit(conn: aiosqlite.Connection) -> None:
        await conn.execute(
            "UPDATE repos SET blast_radius = ? WHERE repo_id = ?", (radius, repo_id)
        )

    await writer.submit(unit)


async def _set_status(
    repo: SqliteStateRepository, repo_id: str, status: RepoStatus, clock: SteppableClock
) -> None:
    """Drive a phase to a status through the real CAS pair, never by hand-editing the row."""
    fence = await repo.acquire_phase_lease(
        RUN, repo_id, PHASE, owner="test:cid:1:boot", now=clock(), lease_ttl_s=300
    )
    assert fence is not None
    await repo.complete_phase(RUN, repo_id, PHASE, fence=fence, status=status, now=clock())


def _scheduler(
    repo: SqliteStateRepository,
    store: SqliteSchedulerStore,
    clock: SteppableClock,
    *,
    wave_max_wallclock_s: int = 14_400,
) -> WaveScheduler:
    return WaveScheduler(
        run_id=RUN,
        phase=PHASE,
        store=store,
        db=repo,
        budgets=BudgetsSection(wave_max_wallclock_s=wave_max_wallclock_s),
        clock=clock,
    )


# --------------------------------------------------------------------------------------
# gating
# --------------------------------------------------------------------------------------


async def test_wave_1_does_not_open_until_wave_0_has_closed(
    wired: Wired,
) -> None:
    """"Wave 2 cannot open before wave 1 closes" is the ONLY thing topological sequencing buys.

    A scheduler that opened the next layer while one member was still `PENDING` would migrate a
    dependent against a dependency that has not landed — which does not fail here, it fails in a
    container twenty minutes later with an unresolvable label.
    """
    repo, store, _ = wired
    clock = SteppableClock()
    scheduler = _scheduler(repo, store, clock)

    with pytest.raises(WaveNotReadyError, match="wave 1 cannot open"):
        await scheduler.open_wave(1)

    for repo_id in WAVE_0:
        await _set_status(repo, repo_id, RepoStatus.SUCCEEDED, clock)

    assert await scheduler.wave_state(0) is WaveState.CLOSED
    assert await scheduler.open_wave(1) == clock()


async def test_a_partial_predecessor_still_blocks_a_later_wave(
    wired: Wired,
) -> None:
    """Every EARLIER index is checked, not only `wave_index - 1`: a `PARTIAL` wave 0 left by an
    exit-4 halt must not be stepped over by opening wave 2 once wave 1 happens to be empty."""
    repo, store, _ = wired
    clock = SteppableClock()
    scheduler = _scheduler(repo, store, clock, wave_max_wallclock_s=60)
    await scheduler.open_wave(0)
    await _set_status(repo, "acme-commons", RepoStatus.SUCCEEDED, clock)
    clock.advance(120)

    assert await scheduler.wave_state(0) is WaveState.PARTIAL
    with pytest.raises(WaveNotReadyError):
        await scheduler.open_wave(1)


# --------------------------------------------------------------------------------------
# admission
# --------------------------------------------------------------------------------------


async def test_admission_is_ordered_by_blast_radius_descending(
    wired: Wired,
) -> None:
    """§3.5: the widest blast radius goes first, so the expensive failures surface while the run
    still has budget to react. Alphabetical order would put `acme-auth` (42 dependents) ahead of
    `acme-commons` (187) and nothing would ever say so."""
    repo, store, _ = wired
    admission = await _scheduler(repo, store, SteppableClock()).admit(0)

    assert admission.admitted == ("acme-commons", "acme-auth", "acme-batch")
    assert admission.blocked == ()
    assert admission.breached is False


async def test_a_blocked_repo_is_not_admitted(
    wired: Wired,
) -> None:
    """A `BLOCKED` repo waits on an abandoned ancestor. Dispatching it spends attempts — and,
    at the transform phase, real tokens — on work that cannot pass its precondition."""
    repo, store, _ = wired
    clock = SteppableClock()
    await store.append_blocked_by(RUN, "acme-auth", "acme-commons", now=clock())

    admission = await _scheduler(repo, store, clock).admit(0)

    assert "acme-auth" not in admission.admitted
    assert admission.blocked == ("acme-auth",)
    assert admission.admitted == ("acme-commons", "acme-batch")


async def test_blocked_by_is_a_set_union_over_exactly_the_transitive_dependents(
    wired: Wired,
) -> None:
    """§3.5: a repo blocked by three abandoned ancestors lists all three, and repos outside the
    descendant set are untouched. Replacement — the obvious implementation — silently forgets a
    cause, and a repo unblocked from one ancestor would then look runnable while still broken."""
    repo, store, read_conn = wired
    clock = SteppableClock()
    scheduler = WaveScheduler(
        run_id=RUN,
        phase=PHASE,
        store=store,
        db=repo,
        budgets=BudgetsSection(),
        clock=clock,
        descendants=ordering_descendants(
            [("acme-commons", "acme-portal"), ("acme-auth", "acme-portal")]
        ),
    )

    assert await scheduler.propagate_blocked("acme-commons") == frozenset({"acme-portal"})
    assert await scheduler.propagate_blocked("acme-auth") == frozenset({"acme-portal"})

    async with read_conn.execute(
        "SELECT status, blocked_by FROM phases WHERE run_id = ? AND repo_id = ?",
        (RUN, "acme-portal"),
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None
    assert row[0] == "BLOCKED"
    assert json.loads(str(row[1])) == ["acme-auth", "acme-commons"]

    # Exactly the descendants, no more: `acme-batch` is not downstream of anything abandoned.
    batch = await repo.get_phase(RUN, "acme-batch", PHASE)
    assert batch is not None and batch.status is RepoStatus.PENDING


# --------------------------------------------------------------------------------------
# the cumulative wall clock
# --------------------------------------------------------------------------------------


async def test_the_wave_clock_is_cumulative_across_a_resume(
    wired: Wired,
) -> None:
    """§3.4: the wave's wall clock is measured from the PERSISTED `wave_started_at`.

    A resume that restarted it would let a crash-loop buy four fresh hours per crash, which is
    exactly the unbounded run the ceiling exists to prevent. The second scheduler here is a
    second process in every way that matters: fresh object, fresh clock, same database.
    """
    repo, store, _ = wired
    first_clock = SteppableClock()
    started = await _scheduler(repo, store, first_clock, wave_max_wallclock_s=3600).open_wave(0)
    assert started == NOW

    # ---- crash, then resume 3 500 s later with a brand new scheduler ----
    resumed_clock = SteppableClock(NOW + timedelta(seconds=3_500))
    resumed = _scheduler(repo, store, resumed_clock, wave_max_wallclock_s=3600)

    assert await resumed.open_wave(0) == NOW, "resume RESTARTED the wave clock"
    assert await resumed.elapsed_s(0) == pytest.approx(3_500.0)
    assert await resumed.may_admit(0) is True

    resumed_clock.advance(200)
    assert await resumed.elapsed_s(0) == pytest.approx(3_700.0)
    assert await resumed.may_admit(0) is False


async def test_a_wall_clock_breach_withholds_members_as_pending_and_leaves_the_wave_partial(
    wired: Wired,
) -> None:
    """Exit 4 is ordinary operations on a 250-repo run, so its semantics are normative: members
    never admitted stay `PENDING` with **no attempt consumed and no `blocked_by`**, and the wave
    is left `PARTIAL`.

    What this test does NOT assert, because the code does not do it: that anything re-admits
    them. This docstring used to say `fleet resume` re-admits exactly them in the same order.
    It does not — step 8 is unimplemented, and `wave_started_at` is stamped once and never
    cleared, so the breach recurs on every later read of that wave (D82 in
    `docs/INTEGRATION_HONESTY.md`). The property pinned here is the WITHHOLDING, not a
    recovery."""
    repo, store, _ = wired
    clock = SteppableClock()
    scheduler = _scheduler(repo, store, clock, wave_max_wallclock_s=60)
    await scheduler.open_wave(0)
    clock.advance(61)

    admission = await scheduler.admit(0)

    assert admission.breached is True
    assert admission.admitted == ()
    assert admission.withheld == ("acme-commons", "acme-auth", "acme-batch")
    assert await scheduler.wave_state(0) is WaveState.PARTIAL
    for repo_id in WAVE_0:
        row = await repo.get_phase(RUN, repo_id, PHASE)
        assert row is not None
        assert row.status is RepoStatus.PENDING
        assert row.attempts == 0


async def test_replanning_preserves_wave_started_at(
    wired: Wired,
) -> None:
    """§6: a `waves` rewrite PRESERVES `wave_started_at`, or a resequence mid-run restarts the
    clock the previous test just proved must be cumulative."""
    repo, store, _ = wired
    clock = SteppableClock()
    await _scheduler(repo, store, clock).open_wave(0)

    clock.advance(900)
    await store.record_plan(RUN, _plan(), now=clock(), max_usd_per_repo=8.0)

    assert await store.wave_started_at(RUN, 0) == NOW


# --------------------------------------------------------------------------------------
# step 6 — the wave that admits the repos a `blocked_by` recompute has freed
# --------------------------------------------------------------------------------------
#
# `append_unblocked_wave` MOVES members that already carry a `wave_index`, which is why the
# obvious reuse (`graph.sequence.append_synthetic_waves`) cannot serve: it filters to refs
# carrying no index, so on this population it returns its input unchanged (ADR-0090 ruling W).
#
# Its load-bearing property is an ABSENCE — "no pre-existing `waves` row is touched" — and an
# absence is proved here by ENUMERATING what was written, observed from the database two
# genuinely different ways:
#
#   Instrument V (value dump) — every row of every user table before and after, compared as
#   sets. Quantity watched: the rows that appear and disappear. Declared blind spot: a write
#   that stores the value already present (`UPDATE waves SET max_usd = max_usd`) moves no row,
#   so V cannot see it — precisely the shape the defect can take.
#
#   Instrument T (row-write triggers) — an `AFTER INSERT` / `AFTER UPDATE` / `AFTER DELETE`
#   trigger on every user table, appending `(table, op)` to an audit table. Quantity watched:
#   the ordered log of row-write events during the call. A trigger fires on a value-preserving
#   UPDATE, so T sees exactly what V cannot. Declared blind spot: T cannot see a read, nor a
#   write through a connection that bypasses SQLite triggers — nothing here has one, every
#   write goes through the run's single `StateWriter` (§11.5).
#
# The fixture seeds THREE waves with pairwise-distinct `max_usd`, two of them carrying a
# `wave_started_at` and one not, and one of them CLOSED. With a single pre-existing wave the
# absence claim is nearly vacuous: there is no row a cross-row write could land on.

UNBLOCK_PLAN = {
    0: ("u-alpha", "u-beta", "u-gamma"),
    1: ("u-delta", "u-freed"),
    2: ("u-epsilon",),
}
UNBLOCK_MAX_USD_PER_REPO = 8.0
_AUDIT = "w17_row_write_audit"

# repo, scheduler store, read connection, writer, clock
UnblockWired = tuple[
    SqliteStateRepository, SqliteSchedulerStore, aiosqlite.Connection, StateWriter, SteppableClock
]


async def _user_tables(conn: aiosqlite.Connection) -> tuple[str, ...]:
    sql = (
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        " AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    async with conn.execute(sql) as cursor:
        return tuple(str(row[0]) for row in await cursor.fetchall() if str(row[0]) != _AUDIT)


async def _dump(
    conn: aiosqlite.Connection, tables: Sequence[str]
) -> dict[str, set[tuple[object, ...]]]:
    """Instrument V: every row of every user table, as comparable value tuples."""
    out: dict[str, set[tuple[object, ...]]] = {}
    for table in tables:
        async with conn.execute(f"SELECT * FROM {table}") as cursor:  # noqa: S608 - fixed names
            out[table] = {tuple(row) for row in await cursor.fetchall()}
    return out


async def _install_row_write_audit(writer: StateWriter, tables: Sequence[str]) -> None:
    """Instrument T: an AFTER INSERT/UPDATE/DELETE trigger on EVERY user table."""

    async def unit(conn: aiosqlite.Connection) -> None:
        await conn.execute(
            f"CREATE TABLE {_AUDIT} (seq INTEGER PRIMARY KEY, tbl TEXT NOT NULL, op TEXT NOT NULL)"
        )
        for table in tables:
            for op in ("INSERT", "UPDATE", "DELETE"):
                sql = (
                    f"CREATE TRIGGER {_AUDIT}_{table}_{op.lower()} AFTER {op} ON {table} "  # noqa: S608
                    f"BEGIN INSERT INTO {_AUDIT} (tbl, op) VALUES ('{table}', '{op}'); END"
                )
                await conn.execute(sql)

    await writer.submit(unit)


async def _row_writes(conn: aiosqlite.Connection) -> tuple[tuple[str, str], ...]:
    sql = f"SELECT tbl, op FROM {_AUDIT} ORDER BY seq"  # noqa: S608 - fixed literal name
    async with conn.execute(sql) as cursor:
        return tuple((str(row[0]), str(row[1])) for row in await cursor.fetchall())


@asynccontextmanager
async def _unblock_wiring(path: Path) -> AsyncIterator[UnblockWired]:
    """Three planned waves, one of them CLOSED, and `u-freed` BLOCKED by the real §3.5 writer.

    A plain context manager rather than only a fixture so the same wiring can be driven outside
    a pytest session — the mutation battery for these tests runs as a standalone probe.
    """
    await initialize_database(path)
    async with StateWriter(path, owner="test-scheduler-unblock") as writer:
        read_conn = await connect_ro(path)
        clock = SteppableClock()
        try:
            repo = SqliteStateRepository(writer=writer, read_conn=read_conn)
            store = SqliteSchedulerStore(writer=writer, read_conn=read_conn)
            await repo.upsert_run(
                RUN, started_at=NOW, config_sha256="b" * 64, harness_version="0.1.0"
            )
            for members in UNBLOCK_PLAN.values():
                for repo_id in members:
                    await repo.upsert_repo(
                        repo_id,
                        name=repo_id,
                        url=f"https://example.invalid/{repo_id}.git",
                        now=NOW,
                    )
                    await repo.upsert_phase(RUN, repo_id, PHASE, now=NOW)
            plan = WavePlan(
                waves=tuple(
                    MigrationWave(
                        wave_index=index,
                        repo_ids=list(members),
                        depends_on_waves=[index - 1] if index else [],
                    )
                    for index, members in UNBLOCK_PLAN.items()
                ),
                wave_index_by_node={},
                cycle_findings=(),
                excluded_repo_ids=(),
            )
            await store.record_plan(
                RUN, plan, now=NOW, max_usd_per_repo=UNBLOCK_MAX_USD_PER_REPO
            )
            # Wave 0 closes; wave 1 holds the repo the recompute will free; wave 2 is untouched.
            for repo_id in UNBLOCK_PLAN[0]:
                await _set_status(repo, repo_id, RepoStatus.SUCCEEDED, clock)
            await _set_status(repo, "u-delta", RepoStatus.SUCCEEDED, clock)
            await store.append_blocked_by(RUN, "u-freed", "u-alpha", now=clock())
            # Two waves carry a persisted start stamp and one does not, and the two stamps
            # differ — so a write that copied one wave's row over another's would show.
            await store.begin_wave(RUN, 0, now=clock())
            clock.advance(600)
            await store.begin_wave(RUN, 1, now=clock())
            clock.advance(600)
            yield repo, store, read_conn, writer, clock
        finally:
            await read_conn.close()


@pytest.fixture
async def unblock_wired(tmp_path: Path) -> AsyncIterator[UnblockWired]:
    async with _unblock_wiring(tmp_path / "state" / "fleet.db") as wiring:
        yield wiring


async def test_the_appended_wave_writes_only_its_own_row_and_the_members_it_moves(
    unblock_wired: UnblockWired,
) -> None:
    """The complete write set of `append_unblocked_wave`, enumerated from the database.

    "Touches no existing `waves` row" is an absence, so it is not asserted — it is READ OFF an
    enumeration of everything that WAS written, by two instruments that do not share a blind
    spot (see the section header). The pre-existing `waves` rows are then byte-identical by
    construction: they are absent from both the added and the removed set.
    """
    repo, store, read_conn, writer, clock = unblock_wired
    tables = await _user_tables(read_conn)
    assert "waves" in tables and "wave_members" in tables
    assert await _scheduler(repo, store, clock).wave_state(0) is WaveState.CLOSED
    before = await _dump(read_conn, tables)
    assert len(before["waves"]) == 3, "the absence claim needs several rows to be about"

    await _install_row_write_audit(writer, tables)
    appended = await store.append_unblocked_wave(
        RUN, ["u-freed"], now=clock(), max_usd_per_repo=UNBLOCK_MAX_USD_PER_REPO
    )

    after = await _dump(read_conn, tables)
    writes = await _row_writes(read_conn)

    # Instrument V — the rows that appeared and disappeared, table by table. Asserted FIRST so
    # that a failure further down is evidence V stayed silent while T fired, which is the whole
    # reason there are two of them.
    added = {t: after[t] - before[t] for t in tables}
    removed = {t: before[t] - after[t] for t in tables}
    assert appended == 3, "the appended index sits above every existing wave"
    assert added["waves"] == {
        (RUN, 3, _iso_stamp(clock), None, 1, UNBLOCK_MAX_USD_PER_REPO * 1)
    }, "one new row, synthetic=1, its own derived ceiling, no start stamp inherited"
    assert removed["waves"] == set(), "no pre-existing `waves` row was rewritten or deleted"
    assert removed["wave_members"] == {(RUN, 1, "REPO", "u-freed")}
    assert added["wave_members"] == {(RUN, 3, "REPO", "u-freed")}
    untouched = sorted(t for t in tables if t not in {"waves", "wave_members"})
    assert [t for t in untouched if added[t] or removed[t]] == []

    # Instrument T — the ordered log of every row-write event during the call. This is the
    # complete write set, and it is what makes the absence above a reading rather than a claim:
    # a value-preserving `UPDATE waves SET c = c` moves no row and is invisible to V.
    assert writes == (("waves", "INSERT"), ("wave_members", "UPDATE"))


async def test_two_concurrent_appends_do_not_allocate_the_same_wave_index(
    unblock_wired: UnblockWired,
) -> None:
    """The index is allocated INSIDE the write transaction, not read into Python first.

    A `MAX(wave_index) + 1` read outside the unit is a read-then-write race: both callers read
    the same maximum and the second `INSERT` collides on `PRIMARY KEY (run_id, wave_index)`.
    Two appends are driven concurrently precisely so that form cannot pass — under it this
    raises `IntegrityError` while every other test in this module still passes.
    """
    _repo, store, _read_conn, _writer, clock = unblock_wired

    first, second = await asyncio.gather(
        store.append_unblocked_wave(
            RUN, ["u-freed"], now=clock(), max_usd_per_repo=UNBLOCK_MAX_USD_PER_REPO
        ),
        store.append_unblocked_wave(
            RUN, ["u-delta"], now=clock(), max_usd_per_repo=UNBLOCK_MAX_USD_PER_REPO
        ),
    )

    assert sorted([first, second]) == [3, 4]


async def test_appending_no_repos_writes_no_wave_row_at_all(
    unblock_wired: UnblockWired,
) -> None:
    """An empty appended wave would be a row that closes on sight and claims a membership it
    has not got, so the method declines rather than inserting one."""
    _repo, store, _read_conn, _writer, clock = unblock_wired

    appended = await store.append_unblocked_wave(
        RUN, [], now=clock(), max_usd_per_repo=UNBLOCK_MAX_USD_PER_REPO
    )

    assert appended is None
    assert await store.wave_indices(RUN) == (0, 1, 2)


async def test_a_repo_no_wave_holds_is_refused_rather_than_admitted(
    unblock_wired: UnblockWired,
) -> None:
    """This method MOVES members. A repo with no `wave_members` row is one the sequencer never
    planned, and silently appending a wave that does not contain it is the failure — the wave
    would open, admit nothing, and close, with the repo still parked in no wave at all."""
    _repo, store, _read_conn, _writer, clock = unblock_wired

    with pytest.raises(WaveNotReadyError, match="u-ghost"):
        await store.append_unblocked_wave(
            RUN, ["u-freed", "u-ghost"], now=clock(), max_usd_per_repo=UNBLOCK_MAX_USD_PER_REPO
        )

    assert await store.wave_indices(RUN) == (0, 1, 2)


def _iso_stamp(clock: SteppableClock) -> str:
    """The exact text `_iso` persists, so the expected `waves` row is a value, not a wildcard."""
    return clock().astimezone(UTC).isoformat(timespec="microseconds")
