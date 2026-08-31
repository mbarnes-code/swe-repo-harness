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

import ast
import asyncio
import inspect
import random
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
import pytest

from fleet.models.enums import ModelTier
from fleet.models.tasks import BackendTarget, Price
from fleet.orchestrator import budgets as budgets_mod
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
    ResizableLimiter,
    RevalidationBudgetExhausted,
    RunBudgetExhausted,
    SpendKind,
    SpendScope,
    TaskTokenBudgetExhausted,
    TokenEstimator,
    WaveBudgetExhausted,
    estimate_cost,
    new_cpu_pool,
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

    async def slots(gate: asyncio.Semaphore | ResizableLimiter) -> int:
        """How many holders fit before the next acquire would block — the number that matters.

        Both kinds, and the annotation has to say so: the `llm` tiers below are
        `ResizableLimiter` and `git_net` / `docker` are still `asyncio.Semaphore`. This reads
        only what the two share, `locked()` and `acquire()`. `ResizableLimiter.__slots__` has no
        `_value`, so anything reaching for semaphore internals here raises at runtime.
        """
        taken = 0
        while not gate.locked():
            await gate.acquire()
            taken += 1
        return taken

    assert await slots(limits.for_tier(ModelTier.HEAVY)) == 2
    assert await slots(limits.for_tier(ModelTier.WORKHORSE)) == 2, "an override may only LOWER"
    assert await slots(limits.for_tier(ModelTier.CHEAP)) == 16
    assert await slots(limits.git_net) == 8
    assert await slots(limits.docker) == 4


def test_new_cpu_pool_passes_no_db_handle_bearing_kwarg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§12.28: pool children get no DB handle — assert the actual `ProcessPoolExecutor` call.

    `forkserver` children never inherit an open SQLite connection unless one is handed to them
    explicitly — `initializer=`/`initargs=` is exactly that door (a picklable initargs value
    carrying a connection or its path+handle), and so is any other kwarg that hands the pool a
    live file handle. This spies on the real `ProcessPoolExecutor(...)` call `new_cpu_pool` makes
    rather than reading the source text, so a regression that adds either kwarg fails the test
    that reads it, not just an inspection someone has to remember to redo.
    """
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    class _SpyExecutor:
        def __init__(self, *args: object, **kwargs: object) -> None:
            calls.append((args, kwargs))

    monkeypatch.setattr(budgets_mod, "ProcessPoolExecutor", _SpyExecutor)

    pool = new_cpu_pool(4)

    assert isinstance(pool, _SpyExecutor)
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == (), "every argument must be a keyword, or a future addition slips by unnamed"
    assert kwargs["max_workers"] == 4
    assert "mp_context" in kwargs
    # The whole point: no DB-handle-bearing kwarg reaches the child, named or not. A future
    # addition of ANY third kwarg — not just the two named below — must fail this assertion.
    assert set(kwargs) == {"max_workers", "mp_context"}
    assert "initializer" not in kwargs
    assert "initargs" not in kwargs


# --------------------------------------------------------------------------------------
# 8. `ResizableLimiter` — the §11.8 primitive whose ceiling moves while slots are held
# --------------------------------------------------------------------------------------
#
# These are property tests, not shape tests. Asserting that `capacity` holds the number that was
# passed to `resize` proves nothing about bounding: the number is only interesting if a task that
# would have run is actually made to wait for it. Every assertion below is therefore made on
# observed concurrency (a peak counter, an admission order, a list of tasks that got to run)
# rather than on the limiter's own bookkeeping, except where the bookkeeping is the thing that
# leaks (a slot charged to a cancelled task is invisible in every other observation).
#
# No test here sleeps for a real duration. `_settle` yields to the event loop with zero-duration
# `asyncio.sleep(0)`, which is enough to run every ready task to its next await, and a limiter
# under test is never waiting on time — only on other tasks.


async def _settle(rounds: int = 8) -> None:
    """Run every ready task to its next await. Zero-duration: adds nothing to the suite's clock."""
    for _ in range(rounds):
        await asyncio.sleep(0)


@contextmanager
def _watch_admissions(limiter: ResizableLimiter) -> Iterator[list[tuple[int, int]]]:
    """Record `(borrowed, capacity)` at the instant each parked waiter is handed its slot.

    **What this measures, and why it is not `borrowed`.** The over-admission this class had is
    invisible in `borrowed`: a cancelled waiter hands on a slot that was *already* charged, so the
    count reads the same either side of the breach (2 before, 2 after, against a ceiling of 1).
    The observable event is the **admission** — a parked waiter being charged a slot and having
    its future settled — and the quantity that says whether it was legitimate is the ceiling in
    force at that same instant. `borrowed > capacity` in an entry below therefore means a slot was
    handed to a waiter the ceiling had no room for.

    **Why it hooks the future rather than the limiter.** An instrument that wraps whichever
    private method happens to do the waking is only valid for the shape it was written against.
    The predecessor of this helper subclassed the limiter and overrode `_wake_next`; when that
    method was folded into `_drain` the override stopped being called, the instrument recorded
    nothing, and the fuzz below went on **passing vacuously** — a detector that cannot fire is
    indistinguishable from a clean result. Settling the waiter's future is the one step every
    shape must perform, so patching the loop's `create_future` survives a refactor of the
    limiter's internals *and* survives a mutation that reintroduces the old shape, which is what
    lets the same instrument judge both.

    Recording happens before `super().set_result`, while the future is still in `_waiters` — the
    waiter's own `finally` removes it only once it resumes — which is also what distinguishes a
    real waiter from any other future created on this loop.

    Enter this **before the waiters park.** A future created outside the window is an ordinary
    one whose admission goes unrecorded, and the first draft of the test below opened the window
    after the queue had formed and read a real admission as silence. The guard below turns that
    mistake into an error instead of a clean result.
    """
    if limiter._waiters:
        raise AssertionError("waiters parked before the watch opened would be invisible to it")
    loop = asyncio.get_running_loop()
    original = loop.create_future
    admissions: list[tuple[int, int]] = []

    class _WatchedFuture(asyncio.Future[None]):
        def set_result(self, result: None, /) -> None:
            if any(fut is self for fut in limiter._waiters):
                admissions.append((limiter.borrowed, limiter.capacity))
            super().set_result(result)

    def create_future() -> asyncio.Future[None]:
        return _WatchedFuture(loop=loop)

    loop.create_future = create_future  # type: ignore[method-assign]
    try:
        yield admissions
    finally:
        loop.create_future = original  # type: ignore[method-assign]


async def test_limiter_never_admits_more_than_capacity_under_contention() -> None:
    """Six tasks, three slots: the bound is what the peak counter saw, not what `capacity` says."""
    limiter = ResizableLimiter(3)
    live = 0
    peak = 0

    async def call() -> None:
        nonlocal live, peak
        async with limiter:
            live += 1
            peak = max(peak, live)
            await _settle()  # hold the slot across every other task's chance to barge in
            live -= 1

    await asyncio.gather(*(call() for _ in range(6)))

    assert peak == 3, "three is both the ceiling and reachable — a lower peak would not prove it"
    assert limiter.borrowed == 0, "every slot returned"


async def test_shrinking_while_slots_are_held_bars_entrants_and_harms_no_holder() -> None:
    """§11.8's "halved on a 429" applies to the *next* call, never to the three already in flight.

    A shrink that cancelled or errored its current holders would turn a throttle into a failed
    attempt, which is the opposite of backpressure.
    """
    limiter = ResizableLimiter(3)
    gates = [asyncio.Event() for _ in range(3)]
    finished: list[int] = []

    async def holder(index: int) -> None:
        async with limiter:
            await gates[index].wait()
        finished.append(index)

    holders = [asyncio.create_task(holder(i)) for i in range(3)]
    await _settle()
    assert limiter.borrowed == 3

    limiter.resize(1)

    entered = asyncio.Event()

    async def entrant() -> None:
        async with limiter:
            entered.set()

    late = asyncio.create_task(entrant())
    await _settle()
    assert not entered.is_set(), "capacity 1 with 3 borrowed admits nobody"

    gates[0].set()
    await _settle()
    assert not entered.is_set(), "2 borrowed is still at or above the new ceiling of 1"
    gates[1].set()
    await _settle()
    assert not entered.is_set(), "1 borrowed is still at the new ceiling of 1"
    gates[2].set()
    await _settle()
    assert entered.is_set(), "the entrant runs only once the shrunk ceiling has drained"

    await asyncio.gather(*holders, late)
    assert finished == [0, 1, 2], "no holder was cancelled or errored by the shrink"


async def test_growing_admits_parked_waiters_without_waiting_for_a_release() -> None:
    """The reason `asyncio.Semaphore` cannot be used: raising the ceiling must wake waiters.

    "One slot returned per clean minute" is worthless if the returned slot is only noticed the
    next time an unrelated call happens to finish.
    """
    limiter = ResizableLimiter(3)
    limiter.resize(1)
    await limiter.acquire()

    admitted: list[int] = []

    async def waiter(index: int) -> None:
        async with limiter:
            admitted.append(index)

    tasks = [asyncio.create_task(waiter(i)) for i in range(2)]
    await _settle()
    assert admitted == [], "capacity 1, one slot held"

    limiter.resize(3)
    assert limiter.borrowed == 3, "the grow charges both waiters before either of them resumes"
    await _settle()
    assert admitted == [0, 1], "both parked waiters ran on the grow alone — no release intervened"

    limiter.release()
    await asyncio.gather(*tasks)


async def test_resize_clamps_to_floor_and_ceiling_and_refuses_zero() -> None:
    """The clamp is asserted by what runs concurrently, not by reading `capacity` back."""
    limiter = ResizableLimiter(8, floor=2, ceiling=8)

    limiter.resize(1)
    assert limiter.capacity == 2, "never below the floor"

    live = 0
    peak = 0

    async def call() -> None:
        nonlocal live, peak
        async with limiter:
            live += 1
            peak = max(peak, live)
            await _settle()
            live -= 1

    await asyncio.gather(*(call() for _ in range(4)))
    assert peak == 2, "the floor is a real bound, not a number stored on the object"

    limiter.resize(99)
    assert limiter.capacity == 8, "never above the ceiling"

    with pytest.raises(ValueError):
        limiter.resize(0)
    with pytest.raises(ValueError):
        limiter.resize(-4)
    assert limiter.capacity == 8, "a refused resize changed nothing"


async def test_waiters_are_admitted_in_arrival_order() -> None:
    """FIFO. Without it a run under sustained contention starves whichever call queued first,
    and the tier ceiling starts producing timeouts instead of backpressure."""
    limiter = ResizableLimiter(1)
    await limiter.acquire()
    order: list[str] = []

    async def waiter(name: str) -> None:
        async with limiter:
            order.append(name)

    tasks = [asyncio.create_task(waiter(name)) for name in ("a", "b", "c")]
    await _settle()

    limiter.release()
    await asyncio.gather(*tasks)

    assert order == ["a", "b", "c"]


async def test_a_waiter_cancelled_while_parked_consumes_no_wake() -> None:
    """Two queued, cancel the first, release once: the second must RUN.

    A cancelled waiter that still absorbs the wake does not raise anything — it silently lowers
    the effective ceiling by one for the rest of the run.
    """
    limiter = ResizableLimiter(1)
    await limiter.acquire()
    ran: list[str] = []

    async def waiter(name: str) -> None:
        await limiter.acquire()
        ran.append(name)

    first = asyncio.create_task(waiter("first"))
    second = asyncio.create_task(waiter("second"))
    await _settle()

    first.cancel()
    await _settle()
    limiter.release()
    await _settle()

    assert ran == ["second"], "the release reached the waiter that was still there"
    assert limiter.borrowed == 1, "exactly one slot is out — the cancelled waiter returned none"
    with pytest.raises(asyncio.CancelledError):
        await first
    await second


async def test_a_waiter_cancelled_after_being_woken_hands_its_slot_on() -> None:
    """The narrower race: cancelled *between* the wake and the resume, holding a charged slot.

    `release()` charges the slot to the chosen waiter synchronously, so a task cancelled before
    it resumes owns a slot nobody will ever release. Left uncorrected that is a permanent leak,
    and on a tier whose ceiling is 2 it takes two of them to wedge the tier forever.
    """
    limiter = ResizableLimiter(1)
    await limiter.acquire()
    ran: list[str] = []

    async def waiter(name: str) -> None:
        await limiter.acquire()
        ran.append(name)

    first = asyncio.create_task(waiter("first"))
    second = asyncio.create_task(waiter("second"))
    await _settle()

    limiter.release()  # charges the slot to `first`, which has not resumed yet
    first.cancel()
    await _settle()

    assert ran == ["second"], "the slot `first` never used went on to `second`"
    assert limiter.borrowed == 1
    with pytest.raises(asyncio.CancelledError):
        await first
    await second


async def test_a_cancelled_waiter_hands_no_slot_over_a_ceiling_that_shrank_under_it() -> None:
    """The same race as above, but with a `resize` down inside the window — R5's actual trigger.

    §11.8's "halved on a 429" fires while calls are in flight, so a shrink can land between the
    wake and the resume of a task that is also being cancelled. The recovery must still hand the
    cancelled task's slot on, but it has to re-read the ceiling first: handing it on unguarded
    admits a waiter over the ceiling the shrink just set, so the cancellation silently undoes the
    throttle it arrived with. Asserted on tasks that actually got inside the limiter and on the
    ceiling in force at that instant — `borrowed` alone cannot see this, because the count is
    decremented either way and it is the *admission* that leaks.
    """
    limiter = ResizableLimiter(2, floor=1)
    await limiter.acquire()
    await limiter.acquire()  # the test itself holds both slots, so no holder task can race us

    inflight = 2
    breaches: list[tuple[str, int, int]] = []
    ran: list[str] = []

    async def waiter(name: str) -> None:
        nonlocal inflight
        await limiter.acquire()
        ran.append(name)
        inflight += 1
        if inflight > limiter.capacity:
            breaches.append((name, inflight, limiter.capacity))
        inflight -= 1
        limiter.release()

    first = asyncio.create_task(waiter("first"))
    second = asyncio.create_task(waiter("second"))
    await _settle()
    assert ran == [], "both slots are held; the queue is two deep"

    limiter.release()  # charges the freed slot to `first`, which has not resumed yet
    inflight -= 1
    limiter.resize(1)  # the 429 lands inside that wake window
    first.cancel()
    await _settle()

    assert breaches == [], (
        f"a cancelled waiter admitted someone over the shrunk ceiling: {breaches}"
    )
    assert ran == [], "one slot is still held against a ceiling of 1 — nobody may be admitted"

    with pytest.raises(asyncio.CancelledError):
        await first

    limiter.release()  # the last held slot drains: now the shrunken ceiling really has headroom
    inflight -= 1
    await _settle()

    assert ran == ["second"], "the slot was withheld, not swallowed — it went out on the drain"
    assert breaches == []
    await second
    assert limiter.borrowed == 0


async def test_a_drain_from_a_full_ceiling_admits_nobody_so_no_caller_needs_a_guard() -> None:
    """The guarantee ADR-0084 buys: the admission gate itself refuses, not each caller in turn.

    The test above pins the *behaviour* of one call site. This pins the **shape** that stops the
    next call site repeating its history: `_drain` is called here directly, from the worst state
    a caller can be in — `borrowed` above `capacity` after a shrink, with two live waiters queued
    — and must charge nothing. A future caller (R5's AIMD controller adds some) that writes a
    bare `self._drain()` therefore cannot over-admit however wrong its idea of the ceiling is,
    because it never gets to express one.

    Discriminating mutation: restore the old split — `_drain` charging one slot unconditionally,
    with `release` and `resize` guarding their own calls, i.e. `d44b94f`'s behaviour with the
    check back outside. Every other test in this file passes under it, including both tests that
    `d44b94f` added, because no *existing* caller misbehaves. Only this one fails, on the bare
    `_drain()` below charging a third slot against a ceiling of 1.
    """
    limiter = ResizableLimiter(2, floor=1)
    await limiter.acquire()
    await limiter.acquire()  # the test holds both slots itself; no holder task can race us
    ran: list[str] = []

    async def waiter(name: str) -> None:
        await limiter.acquire()
        ran.append(name)

    with _watch_admissions(limiter) as admissions:
        first = asyncio.create_task(waiter("first"))
        second = asyncio.create_task(waiter("second"))
        await _settle()
        assert ran == [], "both slots are held; the queue is two deep"

        limiter.resize(1)  # the 429: borrowed 2, ceiling 1 — no headroom exists at all
        assert admissions == [], "a shrink admits nobody"

        limiter._drain()  # the gate itself, called by a caller that checked nothing
        await _settle()
        assert admissions == [], f"the gate charged a slot it had no room for: {admissions}"
        assert limiter.borrowed == 2, "nothing was charged and nothing was returned"
        assert ran == [], "nobody was admitted over the shrunken ceiling"

        limiter.release()  # borrowed 1, ceiling 1 — still no headroom, still nobody
        await _settle()
        assert admissions == [], f"a slot went out with the ceiling exactly full: {admissions}"
        assert ran == []

        limiter.release()  # borrowed 0 < 1 — now the headroom is real, and exactly one goes out
        await _settle()
        assert admissions == [(1, 1)], "one waiter admitted, inside the ceiling, once"
        assert ran == ["first"], "and it was the oldest — the drain did not reorder the queue"

    second.cancel()
    await asyncio.gather(first, second, return_exceptions=True)


def test_only_the_fast_path_and_the_drain_may_charge_a_slot() -> None:
    """A whitelist, not a blacklist of the ways a caller might over-admit (CLAUDE.md Rule 12).

    Guarding `_drain` closes the escape a caller reaches *through* it. It says nothing about a
    caller that hand-rolls `self._borrowed += 1` beside its own ceiling test — which is the shape
    the class had, and the one that shipped a defect. Enumerating the ways to do that is a
    blacklist a third form defeats, so this asserts instead what the class may name at all: an
    increment of `_borrowed` may appear in exactly two methods. `acquire` is the fast path, whose
    guard is `locked()` on the line above it; `_drain` is the gate. A new charge site anywhere
    else trips this test whether or not its author remembered a ceiling check.

    Read from the source rather than from `dis`, because the point is what the next author will
    write, and asserted over the AST rather than a grep so that reformatting does not move it.
    """
    path = inspect.getsourcefile(ResizableLimiter)
    assert path is not None
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    limiter_class = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "ResizableLimiter"
    )

    def charges(node: ast.AST) -> bool:
        """True for `self._borrowed += ...`; the `-= 1` returns are not charges."""
        return (
            isinstance(node, ast.AugAssign)
            and isinstance(node.op, ast.Add)
            and isinstance(node.target, ast.Attribute)
            and node.target.attr == "_borrowed"
            and isinstance(node.target.value, ast.Name)
            and node.target.value.id == "self"
        )

    def rebinds(node: ast.AST) -> bool:
        """True for `self._borrowed = ...`, which would smuggle a charge past `charges`."""
        return isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Attribute)
            and target.attr == "_borrowed"
            and isinstance(target.value, ast.Name)
            and target.value.id == "self"
            for target in node.targets
        )

    charge_sites = {
        method.name
        for method in limiter_class.body
        if isinstance(method, ast.FunctionDef | ast.AsyncFunctionDef)
        for node in ast.walk(method)
        if charges(node)
    }
    rebind_sites = {
        method.name
        for method in limiter_class.body
        if isinstance(method, ast.FunctionDef | ast.AsyncFunctionDef)
        for node in ast.walk(method)
        if rebinds(node)
    }

    assert charge_sites == {"acquire", "_drain"}, (
        f"a slot is charged outside the fast path and the admission gate: {charge_sites}"
    )
    assert rebind_sites == {"__init__"}, (
        f"`_borrowed` is rebound outside the constructor, dodging the gate: {rebind_sites}"
    )


async def test_random_resize_and_cancel_interleavings_never_admit_over_the_ceiling() -> None:
    """A seeded fuzz over the whole class, watching the **admission event**, not a counter.

    Both halves of this were got wrong first and are worth keeping written down.

    *The instrument.* A cancelled waiter handing its slot on transfers a slot that was **already
    charged**, so `borrowed` does not rise when it over-admits — measured on the deterministic
    case above, where `borrowed` reads 2 both before and after the over-admitting cancellation,
    against a ceiling of 1. A rise in `borrowed` is therefore the wrong thing to watch. The charge
    itself is the right thing, and `_watch_admissions` records it at the only step every possible
    shape of this class must take: settling the parked waiter's future. Its predecessor overrode
    the private method that did the waking, and was silently neutered the moment that method was
    renamed — see `_watch_admissions`, which is where that near-miss is written down.

    *The driver.* A woken-but-not-resumed waiter exists only between a charge and the next turn
    of the loop, so a driver that awaits after every operation can never cancel one, and never
    reaches this path at all: an earlier cut of this fuzz, with a correct instrument but one
    operation per turn, reported 0/800 seeds against the **unfixed** class. The bursts below
    issue several operations in a single turn, which is what reaches the window.

    Validated per CLAUDE.md Guardrail 6 before its clean result was trusted, three states, each
    re-measured against this instrument rather than inherited from the one it replaced: it fires
    on the known-bad state (the pre-`d44b94f` split shape with `acquire`'s cancellation recovery
    unguarded — 68/400 seeds, 71 events), stays silent on the swept class (0/400), and fires on a
    fresh fault injected into the swept class (an extra unguarded charge in `release` — 400/400
    seeds, 3,531 events).
    """
    over_admitted: list[tuple[int, int, int]] = []
    for seed in range(400):
        rng = random.Random(seed)
        limiter = ResizableLimiter(rng.randint(1, 4), floor=1, ceiling=4)

        async def body(
            limiter: ResizableLimiter = limiter, rng: random.Random = rng
        ) -> None:
            async with limiter:
                for _ in range(rng.randint(0, 3)):
                    await asyncio.sleep(0)

        with _watch_admissions(limiter) as admissions:
            tasks = [asyncio.create_task(body()) for _ in range(20)]
            for _ in range(60):
                for _ in range(rng.randint(1, 4)):  # one turn, several operations
                    roll = rng.random()
                    if roll < 0.40:
                        limiter.resize(rng.randint(1, 4))
                    elif roll < 0.80:
                        alive = [t for t in tasks if not t.done()]
                        if alive:
                            rng.choice(alive).cancel()
                await asyncio.sleep(0)

            limiter.resize(4)
            await _settle(64)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        over_admitted.extend(
            (seed, borrowed, cap) for borrowed, cap in admissions if borrowed > cap
        )

    assert over_admitted == [], (
        f"(seed, in flight, ceiling) admitted over the ceiling: {over_admitted[:5]}"
    )


async def test_release_without_acquire_fails_loud() -> None:
    """Rule 11. A negative borrowed count would silently raise the effective ceiling instead."""
    limiter = ResizableLimiter(2)
    with pytest.raises(RuntimeError):
        limiter.release()


async def test_limits_hands_each_tier_a_resizable_limiter(harness: Harness) -> None:
    """The retype is the point of this change: `for_tier` must return something AIMD can move.

    Asserted through the real `Limits.create` path, because a limiter that is resizable in
    isolation but reached through a `Mapping` of semaphores would be resizable nowhere.
    """
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
    )

    heavy = limits.for_tier(ModelTier.HEAVY)
    assert isinstance(heavy, ResizableLimiter)
    assert heavy.capacity == 2

    heavy.resize(1)
    live = 0
    peak = 0

    async def call() -> None:
        nonlocal live, peak
        async with heavy:
            live += 1
            peak = max(peak, live)
            await _settle()
            live -= 1

    await asyncio.gather(*(call() for _ in range(3)))
    assert peak == 1, "a HEAVY tier shrunk to 1 actually serializes"

    heavy.resize(99)
    assert heavy.capacity == 2, "the ceiling stays the configured `concurrency.llm.heavy`"


async def test_a_freed_slot_is_charged_at_wake_not_when_the_waiter_resumes() -> None:
    """The window between choosing a waiter and that waiter running must show no headroom.

    Charging the slot inside the resumed waiter instead of inside `_drain` passes the
    contention test — the woken tasks are scheduled ahead of any later arrival, so the race
    usually does not open. It opens when two slots are freed back to back with no await between
    them: both waiters are chosen, neither has resumed, and `borrowed` still reads 0. An arrival
    in that window is admitted over the ceiling.
    """
    limiter = ResizableLimiter(2)
    await limiter.acquire()
    await limiter.acquire()

    async def waiter() -> None:
        async with limiter:
            await _settle()

    parked = [asyncio.create_task(waiter()) for _ in range(2)]
    await _settle()
    assert limiter.borrowed == 2

    limiter.release()
    limiter.release()  # both waiters are now chosen; neither has run a single line

    assert limiter.borrowed == 2, "the two freed slots went to the two waiters, not to nobody"

    # And the admission decision itself: step an arrival's `acquire()` by hand, because awaiting
    # it would hand control to the woken waiters and close the window we are testing.
    arrival = limiter.acquire()
    try:
        arrival.send(None)
    except StopIteration:  # pragma: no cover - the failure this test exists to catch
        pytest.fail("an arrival in the wake window was admitted over the ceiling")
    finally:
        arrival.close()

    await asyncio.gather(*parked)
    assert limiter.borrowed == 0
