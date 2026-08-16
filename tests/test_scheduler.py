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

import json
from collections.abc import AsyncIterator, Iterator
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
    is `PARTIAL` so `fleet resume` re-admits exactly them in the same order."""
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
