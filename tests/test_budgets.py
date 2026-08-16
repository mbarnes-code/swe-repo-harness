"""Behaviour tests for `src/fleet/orchestrator/budgets.py` — the §11.2 reserve-then-spend policy.

Every property pinned here is one whose absence is *silent*, and the first one is a bug that
shipped: reserving `price(target, max_tokens_in + max_tokens_out)` against a per-repo ceiling
never raises at the point of the mistake. It simply makes the top escalation rung arithmetically
undispatchable, so every repo that reaches attempt 3 dies `BUDGET_EXHAUSTED →
REQUIRES_HUMAN_INTERVENTION` with `blocked_by` cascading to its dependents, on work whose real
cost was a tenth of the reservation. `test_p95_reservation_dispatches_where_max_tokens_refuses`
constructs both reservations against one ledger and asserts the outcomes differ; it is the fix.

The ledger assertions run against a **real temp SQLite database** with the real single writer,
because the guarantee under test is the one an in-memory fake cannot express: a compare-and-swap
that holds under concurrency. A fake that returns `True` twice for a $497/$500 ledger is exactly
the defect the CAS exists to prevent.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
import pytest

from fleet.models.enums import ModelTier
from fleet.models.tasks import BackendTarget, Price
from fleet.orchestrator.budgets import (
    P95_MIN_SAMPLES,
    RUN_BUDGET_EXIT_CODE,
    WAVE_BUDGET_EXIT_CODE,
    BackpressureTimeout,
    Ceilings,
    CostEstimate,
    CostLedger,
    LedgerHalted,
    Limits,
    RepoBudgetExhausted,
    RevalidationBudgetExhausted,
    RunBudgetExhausted,
    SpendKind,
    SpendScope,
    TaskTokenBudgetExhausted,
    TokenEstimator,
    WaveBudgetExhausted,
    estimate_cost,
)
from fleet.settings import BudgetsSection, ConcurrencySection, StubsSection
from fleet.state import db as dbmod
from fleet.state.db import StateWriter, connect_ro, initialize_database
from fleet.state.repository import SqliteStateRepository

NOW = datetime(2026, 8, 9, 12, 0, 0, tzinfo=UTC)
RUN = "22222222-2222-4222-8222-222222222222"
REPO = "acme-commons"

#: A HEAVY target with a real hosted price, so the p95-vs-max-tokens arithmetic is the arithmetic
#: that actually bites: 200k in + 64k out at these rates is $7.80, an entire repo ceiling and more.
HEAVY_TARGET = BackendTarget(
    backend="anthropic",
    model_id="heavy-1",
    price=Price(in_per_mtok=15.0, out_per_mtok=75.0),
)
HEAVY_MAX_IN = 200_000
HEAVY_MAX_OUT = 64_000


def ceilings(**overrides: object) -> Ceilings:
    """§11.2 defaults unless a test is specifically about a different number."""
    return replace(Ceilings.from_settings(BudgetsSection(), StubsSection()), **overrides)


@pytest.fixture(autouse=True)
def _clean_write_slot() -> Iterator[None]:
    """A leaked single-writer slot would fail every later test for the wrong reason."""
    yield
    dbmod._release_write_slot()


@pytest.fixture
async def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "state" / "fleet.db"
    await initialize_database(path)
    return path


class Harness:
    """Store + writer, wired exactly as the runner wires them: one writer, one `mode=ro` handle."""

    def __init__(self, store: SqliteStateRepository, writer: StateWriter, db_path: Path) -> None:
        self.store = store
        self.writer = writer
        self.db_path = db_path

    def ledger(self, *, ceil: Ceilings | None = None, **kwargs: object) -> CostLedger:
        return CostLedger(
            self.store,
            run_id=RUN,
            ceilings=ceil or ceilings(),
            clock=lambda: NOW,
            **kwargs,  # type: ignore[arg-type]
        )

    async def open_ledger(self, max_usd: float) -> None:
        await self.store.open_budget_ledger(RUN, max_usd=max_usd, now=NOW)

    async def force_halt_in_the_database(self) -> None:
        """Another process (or `fleet resume` refusing to clear it) set `halted = 1`."""

        async def unit(conn: aiosqlite.Connection) -> None:
            await conn.execute("UPDATE budget_ledger SET halted = 1 WHERE run_id = ?", (RUN,))

        await self.writer.submit(unit)


@pytest.fixture
async def harness(db_path: Path) -> AsyncIterator[Harness]:
    async with StateWriter(db_path, owner="test-budgets") as writer:
        read_conn = await connect_ro(db_path)
        try:
            store = SqliteStateRepository(writer=writer, read_conn=read_conn)
            await store.upsert_run(
                RUN, started_at=NOW, config_sha256="b" * 64, harness_version="0.1.0"
            )
            await store.upsert_repo(
                REPO, name=REPO, url=f"https://example.invalid/{REPO}.git", now=NOW
            )
            yield Harness(store, writer, db_path)
        finally:
            await read_conn.close()


@asynccontextmanager
async def _process(db_path: Path, owner: str) -> AsyncIterator[Harness]:
    """ONE process's view of the run: its own single writer, read handle and `CostLedger`.

    Leaving the block is the crash. Nothing survives it but the file, so anything the next block
    can see was durable — which is the only way to test a ledger whose whole job is to outlive
    the process that spent the money. The `harness` fixture is deliberately NOT used with this:
    §11.5 allows one writable connection per process, and holding two would be the violation.
    """
    async with StateWriter(db_path, owner=owner) as writer:
        read_conn = await connect_ro(db_path)
        try:
            store = SqliteStateRepository(writer=writer, read_conn=read_conn)
            yield Harness(store, writer, db_path)
        finally:
            await read_conn.close()


def scope(**kwargs: object) -> SpendScope:
    return SpendScope(repo_id=REPO, **kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------------------
# 1. THE BUG FIX: a p95 reservation dispatches where a max-tokens reservation cannot
# --------------------------------------------------------------------------------------


async def test_p95_reservation_dispatches_where_max_tokens_refuses(harness: Harness) -> None:
    """The top escalation rung must be *dispatchable* at the end of a repo's budget.

    A repo that has already spent $5.40 of its $6.00 ceiling has $0.60 left — plenty for a HEAVY
    repair whose real cost is $0.16. Reserving the worst case instead ($7.80) refuses the call,
    and the repo dies REQUIRES_HUMAN_INTERVENTION on a ceiling it never came close to. Both
    reservations are constructed here against one ledger: the outcomes must differ.
    """
    await harness.open_ledger(400.0)  # the RUN ceiling is not what is binding here
    ledger = harness.ledger(wait_timeout_s=0.0)

    # Spend exactly $5.40 so $0.60 of the repo's ceiling remains.
    spend = CostEstimate(in_tokens=0, out_tokens=0, usd=5.40)
    held = await ledger.reserve(spend, scope=scope())
    await ledger.settle(held, spend)

    worst_case = estimate_cost(HEAVY_TARGET, in_tokens=HEAVY_MAX_IN, out_tokens=HEAVY_MAX_OUT)
    p95 = TokenEstimator(floors={("escalation", ModelTier.HEAVY): (8_000, 2_000)}).estimate(
        HEAVY_TARGET, role="escalation", tier=ModelTier.HEAVY
    )
    assert worst_case.usd > 6.0 > p95.usd, "the arithmetic under test must actually differ"

    with pytest.raises(RepoBudgetExhausted) as refused:
        await ledger.reserve(worst_case, scope=scope())
    assert refused.value.exit_code is None, "a repo-scoped breach never stops the fleet"

    granted = await ledger.reserve(p95, scope=scope())
    assert granted.estimate.usd == pytest.approx(p95.usd)

    # And the real cost reconciles to a fraction of even the p95 hold.
    await ledger.settle(granted, CostEstimate(in_tokens=6_000, out_tokens=900, usd=0.1575))
    row = await ledger.refresh()
    assert row.spent_usd == pytest.approx(5.5575)
    assert row.reserved_usd == pytest.approx(0.0)
    assert ledger.repo_spent_usd(REPO) == pytest.approx(5.5575)


async def test_estimator_floors_until_p95_has_samples(harness: Harness) -> None:
    """A p95 over three calls is noise, so the floor wins until §11.2's 20 samples exist —
    otherwise one cheap early call would set the reservation for every later expensive one."""
    estimator = TokenEstimator(floors={("repair", ModelTier.WORKHORSE): (4_000, 1_000)})
    floored = estimator.estimate(HEAVY_TARGET, role="repair", tier=ModelTier.WORKHORSE)
    assert (floored.in_tokens, floored.out_tokens) == (4_000, 1_000)

    for _ in range(P95_MIN_SAMPLES):
        estimator.observe("repair", ModelTier.WORKHORSE, in_tokens=30_000, out_tokens=800)
    observed = estimator.estimate(HEAVY_TARGET, role="repair", tier=ModelTier.WORKHORSE)
    # Component-wise max: the observed input p95 dominates, the floor still protects the output.
    assert observed.in_tokens == 30_000
    assert observed.out_tokens == 1_000


# --------------------------------------------------------------------------------------
# 2. reconciliation
# --------------------------------------------------------------------------------------


async def test_reservation_reconciles_and_releases_headroom_to_the_next_caller(
    harness: Harness,
) -> None:
    """Reserving at a p95 is only safe because the hold is released IN FULL on completion.

    Without reconciliation the over-estimate is indistinguishable from spend, and a run paced by
    estimates ratchets itself closed while its real invoice is a fraction of the ceiling.
    """
    await harness.open_ledger(6.0)
    ledger = harness.ledger(wait_timeout_s=0.0)

    wide = scope(blast_radius=1024)  # the RUN ceiling is the one under test, not the repo's
    big = CostEstimate(in_tokens=0, out_tokens=0, usd=5.0)
    reservation = await ledger.reserve(big, scope=wide)

    follower = CostEstimate(in_tokens=0, out_tokens=0, usd=2.0)
    with pytest.raises(BackpressureTimeout):
        await ledger.reserve(follower, scope=wide)  # $5.00 is held, only $1.00 free

    await ledger.settle(reservation, CostEstimate(in_tokens=0, out_tokens=0, usd=0.5))

    granted = await ledger.reserve(follower, scope=wide)
    assert granted.settled is False
    row = await ledger.refresh()
    assert row.spent_usd == pytest.approx(0.5)
    assert row.reserved_usd == pytest.approx(2.0)
    assert ledger.repo_spent_usd(REPO) == pytest.approx(0.5)


# --------------------------------------------------------------------------------------
# 3. backpressure vs death
# --------------------------------------------------------------------------------------


async def test_refused_caller_waits_for_an_inflight_settlement_and_then_succeeds(
    harness: Harness,
) -> None:
    """A shortfall held by in-flight reservations is BACKPRESSURE: the dispatch waits.

    Failing here instead would kill a repo for a ceiling that was about to have room, which is
    the difference between a ceiling that paces a run and one that ends it.
    """
    await harness.open_ledger(6.0)
    ledger = harness.ledger(wait_timeout_s=5.0, wait_poll_s=0.01)

    wide = scope(blast_radius=1024)
    inflight = await ledger.reserve(CostEstimate(in_tokens=0, out_tokens=0, usd=5.0), scope=wide)

    async def settle_soon() -> None:
        await asyncio.sleep(0.05)
        await ledger.settle(inflight, CostEstimate(in_tokens=0, out_tokens=0, usd=0.25))

    settler = asyncio.create_task(settle_soon())
    granted = await ledger.reserve(CostEstimate(in_tokens=0, out_tokens=0, usd=2.0), scope=wide)
    await settler

    assert inflight.settled is True
    assert granted.estimate.usd == pytest.approx(2.0)
    row = await ledger.refresh()
    assert row.spent_usd + row.reserved_usd <= row.max_usd + 1e-9


async def test_refusal_from_spent_alone_fails_immediately_without_waiting(
    harness: Harness,
) -> None:
    """When `spent_usd + estimate` breaches the ceiling on its own, no wait can help.

    Waiting there would burn `task_max_wallclock_s` per dispatch for a ledger that will never
    release anything, so `BudgetExhausted` is the honest answer — and it must be immediate.
    """
    await harness.open_ledger(6.0)
    ledger = harness.ledger(wait_timeout_s=30.0, wait_poll_s=0.01)

    spend = CostEstimate(in_tokens=0, out_tokens=0, usd=5.9)
    held = await ledger.reserve(spend, scope=scope(blast_radius=64))
    await ledger.settle(held, spend)

    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(RunBudgetExhausted):
        await ledger.reserve(
            CostEstimate(in_tokens=0, out_tokens=0, usd=2.0), scope=scope(blast_radius=64)
        )
    assert loop.time() - started < 1.0, "a hopeless reservation must not wait out its timeout"


# --------------------------------------------------------------------------------------
# 4. concurrency
# --------------------------------------------------------------------------------------


async def test_concurrent_reservations_never_collectively_exceed_the_ceiling(
    harness: Harness,
) -> None:
    """The failure this prevents is silent: read-then-write lets 12 workers each reserve $3
    against a $497/$500 ledger and all 12 writes succeed — a 6% overspend no assertion catches.

    24 callers drive a nearly-exhausted ledger at once; the grants must fit, and the durable row
    must never show `spent + reserved > max` (the schema CHECK would abort the write if it did).
    """
    await harness.open_ledger(10.0)
    ledger = harness.ledger(wait_timeout_s=0.0)

    seed = CostEstimate(in_tokens=0, out_tokens=0, usd=8.0)
    held = await ledger.reserve(seed, scope=scope(blast_radius=1024))
    await ledger.settle(held, seed)  # $8.00 spent, $2.00 left

    unit = CostEstimate(in_tokens=0, out_tokens=0, usd=0.5)
    big_repo = scope(blast_radius=1024)  # the repo ceiling is not what is under test here

    async def one() -> bool:
        try:
            await ledger.reserve(unit, scope=big_repo)
        except (RunBudgetExhausted, BackpressureTimeout):
            return False
        return True

    grants = await asyncio.gather(*(one() for _ in range(24)))
    row = await ledger.refresh()
    assert sum(grants) == 4, "exactly $2.00 of headroom is exactly four $0.50 reservations"
    assert row.spent_usd + row.reserved_usd <= row.max_usd + 1e-9


# --------------------------------------------------------------------------------------
# 5. fail-closed is a state
# --------------------------------------------------------------------------------------


async def test_halted_ledger_refuses_dispatch_and_no_call_reaches_the_backend(
    harness: Harness,
) -> None:
    """"Fail-closed" that still dispatches is fail-closed in name only.

    The assertion is on the backend's call count, not on the exception: a guard that raised
    after invoking the model would satisfy a `pytest.raises` and still spend the money.
    """
    await harness.open_ledger(400.0)
    ledger = harness.ledger(wait_timeout_s=0.0)
    calls: list[str] = []

    async def backend() -> None:
        calls.append("dispatched")

    async with ledger.dispatch(
        CostEstimate(in_tokens=0, out_tokens=0, usd=0.25), scope=scope(), deadline=1.0
    ) as (reservation, budget):
        await backend()
        reservation.record(CostEstimate(in_tokens=100, out_tokens=50, usd=0.01))
    assert calls == ["dispatched"]
    assert budget.remaining_usd > 0

    await ledger.halt("run_max_cost_usd reached")
    with pytest.raises(LedgerHalted):
        async with ledger.dispatch(
            CostEstimate(in_tokens=0, out_tokens=0, usd=0.25), scope=scope(), deadline=1.0
        ):
            await backend()  # pragma: no cover - reaching this line IS the defect
    assert calls == ["dispatched"], "a halted ledger dispatched an LLM call"


async def test_durable_halt_is_honoured_by_a_fresh_ledger(harness: Harness) -> None:
    """`halted = 1` is durable and sticky, so a resumed run refuses before its first dispatch —
    an in-process flag would evaporate in exactly the crash the ceiling exists to bound."""
    await harness.open_ledger(400.0)
    await harness.force_halt_in_the_database()

    resumed = harness.ledger(wait_timeout_s=0.0)
    assert resumed.halted is False, "the flag is on disk, not in this fresh object"
    await resumed.refresh()
    assert resumed.halted is True
    with pytest.raises(LedgerHalted):
        await resumed.reserve(CostEstimate(in_tokens=0, out_tokens=0, usd=0.01), scope=scope())


async def test_a_halt_entered_in_one_process_is_observed_by_a_fresh_ledger(db_path: Path) -> None:
    """`CostLedger.halt()` WRITES `halted = 1`, and the next process refuses before its first call.

    Why: `halt()` only ever set an in-process flag. Nothing in the harness wrote the column, so
    the durable halt was read-only — a run that halted at hour 30 came back at hour 31 with a
    clean flag and resumed spending, which is precisely the runaway the sticky halt exists to
    stop. The assertion is on the BACKEND's call count as well as the exception, because a guard
    that raises after invoking the model satisfies `pytest.raises` and still spends the money.
    """
    async with _process(db_path, "process-1") as first:
        await first.store.upsert_run(
            RUN, started_at=NOW, config_sha256="b" * 64, harness_version="0.1.0"
        )
        await first.store.upsert_repo(
            REPO, name=REPO, url=f"https://example.invalid/{REPO}.git", now=NOW
        )
        await first.open_ledger(400.0)
        await first.ledger(wait_timeout_s=0.0).halt("run_max_cost_usd reached")

    async with _process(db_path, "process-2") as second:
        resumed = second.ledger(wait_timeout_s=0.0)
        assert resumed.halted is False, "the flag is on disk, not in this fresh object"
        calls: list[str] = []

        async def backend() -> None:  # pragma: no cover - reaching this line IS the defect
            calls.append("dispatched")

        # No `refresh()` first, deliberately: fail-closed cannot depend on the resumed process
        # remembering to ask. The reservation CAS's own `halted = 0` predicate refuses it.
        with pytest.raises(LedgerHalted):
            async with resumed.dispatch(
                CostEstimate(in_tokens=0, out_tokens=0, usd=0.25), scope=scope(), deadline=1.0
            ):
                await backend()

        assert calls == [], "a halted ledger dispatched an LLM call in the resumed process"
        assert resumed.halted is True, "the durable halt did not become this process's state"
        row = await second.store.get_repo_budget(RUN, REPO)
        assert row is not None
        assert row.reserved_usd == pytest.approx(0.0), "a halted run left a repo hold behind"


async def test_run_ceiling_breach_halts_the_ledger_stickily(harness: Harness) -> None:
    """Reaching `run_max_cost_usd` is a state change, not one refused call: the next dispatch of
    ANY size must be refused rather than re-testing the same arithmetic."""
    await harness.open_ledger(1.0)
    ledger = harness.ledger(wait_timeout_s=0.0)
    spend = CostEstimate(in_tokens=0, out_tokens=0, usd=1.0)
    held = await ledger.reserve(spend, scope=scope(blast_radius=1024))
    await ledger.settle(held, spend)

    with pytest.raises(RunBudgetExhausted):
        await ledger.reserve(
            CostEstimate(in_tokens=0, out_tokens=0, usd=0.01), scope=scope(blast_radius=1024)
        )
    assert ledger.halted is True
    with pytest.raises(LedgerHalted):
        await ledger.reserve(
            CostEstimate(in_tokens=0, out_tokens=0, usd=0.0001), scope=scope(blast_radius=1024)
        )


# --------------------------------------------------------------------------------------
# 5b. the sub-ledger is DURABLE: it outlives the process that spent the money
# --------------------------------------------------------------------------------------


async def test_a_repos_spend_survives_a_process_restart(db_path: Path) -> None:
    """A repo spends $5.00 of its $6.00 ceiling, the process dies, and the resumed run has $1.00.

    Why this is the headline: the repo ceiling used to live in a `dict` on the `CostLedger`, so
    the second process started that repo at zero. A 250-repo run that crashed at hour 30 came
    back and handed a repo that had already burned its $6.00 a fresh $6.00 — and then another,
    every time it crashed. `run_max_cost_usd` was the only real ceiling in the harness, and it is
    two orders of magnitude too coarse to stop one runaway repo.

    The second block shares nothing with the first but the database file.
    """
    async with _process(db_path, "process-1") as first:
        await first.store.upsert_run(
            RUN, started_at=NOW, config_sha256="b" * 64, harness_version="0.1.0"
        )
        await first.store.upsert_repo(
            REPO, name=REPO, url=f"https://example.invalid/{REPO}.git", now=NOW
        )
        await first.open_ledger(400.0)  # the RUN ceiling is nowhere near binding
        ledger = first.ledger(wait_timeout_s=0.0)
        spend = CostEstimate(in_tokens=0, out_tokens=0, usd=5.0)
        held = await ledger.reserve(spend, scope=scope())  # blast_radius 0 => a $6.00 ceiling
        await ledger.settle(held, spend)
        assert await ledger.repo_headroom_usd(REPO) == pytest.approx(1.0)

    async with _process(db_path, "process-2") as second:
        resumed = second.ledger(wait_timeout_s=0.0)
        assert await resumed.repo_headroom_usd(REPO) == pytest.approx(1.0), (
            "the resumed run does not know what this repo already spent"
        )

        with pytest.raises(RepoBudgetExhausted) as breach:
            await resumed.reserve(CostEstimate(in_tokens=0, out_tokens=0, usd=2.0), scope=scope())
        assert breach.value.exit_code is None, "a repo-scoped breach never stops the fleet"

        # The surviving $1.00 is still spendable: the ceiling paces the resumed run, not ends it.
        granted = await resumed.reserve(
            CostEstimate(in_tokens=0, out_tokens=0, usd=1.0), scope=scope()
        )
        await resumed.settle(granted, CostEstimate(in_tokens=0, out_tokens=0, usd=1.0))
        assert await resumed.repo_headroom_usd(REPO) == pytest.approx(0.0)
        row = await second.store.get_repo_budget(RUN, REPO)
        assert row is not None
        assert row.spent_usd == pytest.approx(6.0), "the two processes' spend did not accumulate"


async def test_concurrent_dispatches_never_exceed_a_nearly_exhausted_repo_ceiling(
    harness: Harness,
) -> None:
    """24 dispatches of one repo race for its last $1.00 in $0.25 units: four win, twenty do not.

    Why: the per-repo `dict` was read, compared and written across `await` points, which is a
    read-then-write with extra steps — every racer saw the same $1.00 and every one of them was
    admitted. The durable `WHERE spent + reserved + :amt <= max_usd` is what makes the losers
    match zero rows, and the schema CHECK would abort the write if it did not.
    """
    await harness.open_ledger(400.0)
    ledger = harness.ledger(wait_timeout_s=0.0)

    seed = CostEstimate(in_tokens=0, out_tokens=0, usd=5.0)
    held = await ledger.reserve(seed, scope=scope())  # $5.00 of a $6.00 repo ceiling
    await ledger.settle(held, seed)

    unit = CostEstimate(in_tokens=0, out_tokens=0, usd=0.25)

    async def one() -> bool:
        try:
            await ledger.reserve(unit, scope=scope())
        except (RepoBudgetExhausted, BackpressureTimeout):
            return False
        return True

    grants = await asyncio.gather(*(one() for _ in range(24)))

    assert sum(grants) == 4, "exactly $1.00 of repo headroom is exactly four $0.25 reservations"
    row = await harness.store.get_repo_budget(RUN, REPO)
    assert row is not None
    assert row.spent_usd + row.reserved_usd <= row.max_usd + 1e-9


# --------------------------------------------------------------------------------------
# 5c. the nesting decision, asserted in both directions
# --------------------------------------------------------------------------------------


async def test_a_run_breach_refuses_the_dispatch_and_leaves_no_phantom_repo_hold(
    harness: Harness,
) -> None:
    """It fits the repo's $30 ceiling and breaches the run's $5 one: refused, repo row untouched.

    Why: §6 says "the same CAS discipline" and never says how the two ledgers relate. They are
    NESTED — one dollar held once in each — so the repo hold has to vanish when the run refuses.
    A compensating second write could be lost to the crash between the two, and a leftover
    `reserved_usd` is indistinguishable from a live worker's hold: it would ratchet the repo
    closed until a human read the table. One transaction, one rollback, nothing to reconcile.
    """
    await harness.open_ledger(5.0)
    ledger = harness.ledger(wait_timeout_s=0.0)
    wide = scope(blast_radius=1024)  # a $30 repo ceiling: the RUN ceiling is the binding one

    spend = CostEstimate(in_tokens=0, out_tokens=0, usd=4.0)
    held = await ledger.reserve(spend, scope=wide)
    await ledger.settle(held, spend)

    with pytest.raises(RunBudgetExhausted) as breach:
        await ledger.reserve(CostEstimate(in_tokens=0, out_tokens=0, usd=2.0), scope=wide)
    assert breach.value.exit_code == RUN_BUDGET_EXIT_CODE

    row = await harness.store.get_repo_budget(RUN, REPO)
    assert row is not None
    assert row.reserved_usd == pytest.approx(0.0), "a phantom repo hold survived the run refusal"
    assert row.spent_usd == pytest.approx(4.0)
    assert row.max_usd == pytest.approx(30.0)


async def test_a_repo_breach_never_consumes_run_headroom_to_find_out(harness: Harness) -> None:
    """The other direction: $7.00 breaches the $6.00 repo ceiling, and the run row never moves.

    Why: nesting means the narrower ceiling is tested first and the outer one is never charged
    for a dispatch that could not have happened. Charging the run ledger first would make a repo
    breach cost the fleet real headroom — and under backpressure it would make one doomed repo
    the reason a healthy one had to wait.
    """
    await harness.open_ledger(400.0)
    ledger = harness.ledger(wait_timeout_s=0.0)

    with pytest.raises(RepoBudgetExhausted):
        await ledger.reserve(
            CostEstimate(in_tokens=0, out_tokens=0, usd=7.0), scope=scope(blast_radius=0)
        )

    run_row = await ledger.refresh()
    assert run_row.spent_usd == pytest.approx(0.0)
    assert run_row.reserved_usd == pytest.approx(0.0), "a repo breach consumed run headroom"


# --------------------------------------------------------------------------------------
# 6. the sub-ceilings
# --------------------------------------------------------------------------------------


async def test_wave_ceiling_scales_with_member_count_and_exits_10(harness: Harness) -> None:
    """A fixed per-wave figure halts a 40-repo wave for being large and leaves a 2-repo wave 40×
    over-provisioned, so the ceiling is `wave_max_cost_usd_per_repo × COUNT(wave_members)`."""
    await harness.open_ledger(400.0)
    ledger = harness.ledger()
    limits = ceilings()

    assert limits.wave_max_usd(2) == pytest.approx(16.0)
    assert limits.wave_max_usd(40) == pytest.approx(320.0)

    ask = CostEstimate(in_tokens=0, out_tokens=0, usd=20.0)
    with pytest.raises(WaveBudgetExhausted) as small:
        await ledger.reserve(ask, scope=scope(wave_index=0, wave_members=2, blast_radius=1024))
    assert small.value.exit_code == WAVE_BUDGET_EXIT_CODE == 10

    granted = await ledger.reserve(
        ask, scope=scope(wave_index=1, wave_members=40, blast_radius=1024)
    )
    assert granted.estimate.usd == pytest.approx(20.0)


async def test_repo_ceiling_scales_with_blast_radius_and_is_capped(harness: Harness) -> None:
    """§3.5: a repo 40 dependents wait on is worth more attempts than a leaf — but the scaling
    is capped, because "worth more" must not mean "unbounded"."""
    await harness.open_ledger(400.0)
    ledger = harness.ledger()
    limits = ceilings()

    assert limits.repo_max_usd_for(0) == pytest.approx(6.0)
    assert limits.repo_max_usd_for(7) == pytest.approx(24.0)
    assert limits.repo_max_usd_for(10_000) == pytest.approx(30.0)  # the ceiling caps it

    with pytest.raises(RepoBudgetExhausted):
        await ledger.reserve(
            CostEstimate(in_tokens=0, out_tokens=0, usd=7.0), scope=scope(blast_radius=0)
        )
    granted = await ledger.reserve(
        CostEstimate(in_tokens=0, out_tokens=0, usd=7.0), scope=scope(blast_radius=7)
    )
    assert granted.estimate.usd == pytest.approx(7.0)


async def test_revalidation_is_a_subceiling_inside_the_repo_ceiling(harness: Harness) -> None:
    """Stub rework is priced separately so "the rework was free" cannot be asserted again, and it
    can never raise a repo's total: exhausting it holds the repo DEGRADED rather than promoting."""
    await harness.open_ledger(400.0)
    ledger = harness.ledger()

    with pytest.raises(RevalidationBudgetExhausted):
        await ledger.reserve(
            CostEstimate(in_tokens=0, out_tokens=0, usd=2.5),
            scope=scope(kind=SpendKind.REVALIDATION, blast_radius=64),
        )
    # The same money spent as ordinary transform work is inside the repo ceiling and admitted.
    granted = await ledger.reserve(
        CostEstimate(in_tokens=0, out_tokens=0, usd=2.5), scope=scope(blast_radius=64)
    )
    assert granted.estimate.usd == pytest.approx(2.5)


async def test_task_token_ceiling_is_measured_in_tokens_and_advances_the_ladder(
    harness: Harness,
) -> None:
    """`task_max_tokens` is a token ceiling, so it must trip on a FREE target too — a local
    profile runs with every ceiling live, priced at $0.00 (§11.2)."""
    await harness.open_ledger(400.0)
    free = BackendTarget(backend="openai_compatible", model_id="local", price="free")
    ledger = harness.ledger(ceil=ceilings(task_max_tokens=10_000))

    big = estimate_cost(free, in_tokens=9_000, out_tokens=800)
    assert big.usd == 0.0
    first = await ledger.reserve(big, scope=scope(task_id="t-1", blast_radius=64))
    await ledger.settle(first, big)

    with pytest.raises(TaskTokenBudgetExhausted) as breach:
        await ledger.reserve(big, scope=scope(task_id="t-1", blast_radius=64))
    assert breach.value.exit_code is None, "a task-scoped breach never stops the fleet"
    assert ledger.task_spent_tokens("t-1") == 9_800


# --------------------------------------------------------------------------------------
# 7. semaphores (§11.1)
# --------------------------------------------------------------------------------------


async def test_limits_key_llm_semaphores_by_tier_not_by_backend(harness: Harness) -> None:
    """ADR-0023: keyed by TIER, so a failover inherits the tier's budget instead of opening a
    second unbounded lane against the same rate limit."""
    await harness.open_ledger(400.0)

    class _NullExecutor:
        def submit(self, *args: object, **kwargs: object) -> None:  # pragma: no cover - unused
            raise NotImplementedError

        def shutdown(self, wait: bool = True) -> None:  # pragma: no cover - unused
            return None

    limits = Limits.create(
        ConcurrencySection(),
        ledger=harness.ledger(),
        cpu_pool=_NullExecutor(),  # type: ignore[arg-type]
        llm_overrides={ModelTier.WORKHORSE: 2},
    )

    async def slots(sem: asyncio.Semaphore) -> int:
        """How many holders fit before the next acquire would block — the number that matters."""
        taken = 0
        while not sem.locked():
            await sem.acquire()
            taken += 1
        return taken

    assert await slots(limits.for_tier(ModelTier.HEAVY)) == 2
    assert await slots(limits.for_tier(ModelTier.WORKHORSE)) == 2, "an override may only LOWER"
    assert await slots(limits.for_tier(ModelTier.CHEAP)) == 16
    assert await slots(limits.git_net) == 8
    assert await slots(limits.docker) == 4
