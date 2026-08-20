"""`Limits` and `CostLedger`: semaphores and the reserve-then-spend cost policy (§11.1, §11.2).

This module is the **policy layer above** `state/repository.py`'s two normative primitives
(`reserve_budget` / `settle_budget`, §6 "RESERVATION"). It writes no SQL: the compare-and-swap
that makes a ceiling un-overshootable lives in exactly one place, and re-implementing it here
would give the harness two ledgers that disagree under concurrency. What lives here is the
*judgement* the CAS cannot make on its own — how much to reserve, which of the five ceilings a
refusal belongs to, and whether a refusal means "wait" or "stop".

Three properties are load-bearing, and each was a defect before it was code.

**1. The estimate is a p95, not `max_tokens`.** Reserving `price(target, max_tokens_in +
max_tokens_out)` against a $6 repo ceiling makes the top escalation rung *arithmetically
undispatchable*: a HEAVY rung's worst-case reservation alone exceeds what remains, so every repo
reaching attempt 3 died `BUDGET_EXHAUSTED → REQUIRES_HUMAN_INTERVENTION`, with `blocked_by`
cascading to its dependents, on work whose real spend was $0.60. `TokenEstimator` reserves
`max(role_p95_observed, role_floor)` — the p95 of this run's own observed `(role, tier)` samples,
falling back to the floor until `P95_MIN_SAMPLES` exist — and every reservation is **reconciled**
on completion, so a mis-estimate never survives one call.

**2. A refusal is backpressure before it is a verdict.** When `spent + reserved + estimate`
breaches the ceiling but `spent + estimate` alone does not, the shortfall is held by in-flight
reservations that will release, so the dispatch **waits** on a condition signalled by every
settlement and re-evaluates the CAS on each wake, bounded by `task_max_wallclock_s`. Only when
`spent + estimate` breaches on its own can no amount of waiting help, and only then is the call
refused. That is the difference between a ceiling that paces a run and one that kills it.

**3. Fail-closed is a state, not a signal.** Once the ledger is halted, `reserve()` refuses every
caller in this process *before* a backend is touched, and the SQL CAS's own `halted = 0` predicate
refuses every caller in every other process. In-flight calls are allowed to finish: cancelling
them wastes what was already paid for.

Five ceilings, each with the breach behaviour §11.2 assigns, each carried by its own exception
type so no caller has to parse a message to know what to do (Rule 11):

| Ceiling | Exception | Breach behaviour |
|---|---|---|
| `task_max_tokens` | `TaskTokenBudgetExhausted` | attempt fails, ladder advances |
| `repo_max_cost_usd` | `RepoBudgetExhausted` | repo → `REQUIRES_HUMAN_INTERVENTION` |
| `revalidation_max_cost_usd` | `RevalidationBudgetExhausted` | stub `ABANDONED`, repo `DEGRADED` |
| `wave_max_cost_usd_per_repo` × members | `WaveBudgetExhausted` | wave stops admitting, exit 10 |
| `run_max_cost_usd` | `RunBudgetExhausted` / `LedgerHalted` | ledger halted, sticky, exit 3 |
"""

from __future__ import annotations

import asyncio
import math
import multiprocessing
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator, Callable, Mapping
from concurrent.futures import Executor, ProcessPoolExecutor
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import ClassVar, Final, Protocol

from fleet.llm.client import BudgetExhausted, CallBudget
from fleet.models.base import utcnow
from fleet.models.enums import ModelTier, Phase
from fleet.models.tasks import BackendTarget
from fleet.settings import BudgetsSection, ConcurrencySection, StubsSection, target_price_usd
from fleet.state.repository import (
    BudgetLedgerRow,
    BudgetRefusedError,
    RepoBudgetRefusedError,
    RepoLedgerRow,
    ReservationRefusedError,
)

__all__ = [
    "DEFAULT_ROLE_FLOOR",
    "P95_MIN_SAMPLES",
    "RUN_BUDGET_EXIT_CODE",
    "WAVE_BUDGET_EXIT_CODE",
    "BackpressureTimeout",
    "BudgetCeiling",
    "Ceilings",
    "CostEstimate",
    "CostLedger",
    "LedgerBreach",
    "LedgerHalted",
    "LedgerStore",
    "Limits",
    "RepoBudgetExhausted",
    "Reservation",
    "RevalidationBudgetExhausted",
    "RunBudgetExhausted",
    "SpendKind",
    "SpendScope",
    "TaskTokenBudgetExhausted",
    "TokenEstimator",
    "WaveBudgetExhausted",
    "estimate_cost",
]

#: §11.2: `role_p95_observed` is read from the run's own `attempts` rows and falls back to
#: `role_floor` until this many samples exist for that `(role, tier)`.
P95_MIN_SAMPLES: Final = 20

#: The `role_floor` used for a `(role, tier)` with no configured floor. §11.2 names `role_floor`
#: but §9 declares no key for it, so it is a constructor argument with this default rather than a
#: config read that would silently resolve to zero (SPEC gap, reported not invented).
DEFAULT_ROLE_FLOOR: Final = (4_000, 1_000)

#: §10/§11.2. `fleet.cli.ExitCode` stops at 7 today, so the two ledger exits are named here
#: rather than invented into that enum from another module's scope.
RUN_BUDGET_EXIT_CODE: Final = 3
WAVE_BUDGET_EXIT_CODE: Final = 10

#: Ledger arithmetic is float; the guards must tolerate one ulp without letting a real breach pass.
_USD_EPSILON: Final = 1e-9


# --------------------------------------------------------------------------------------
# breaches — five ceilings, five types; never a bool and never a message a caller must parse
# --------------------------------------------------------------------------------------


class BudgetCeiling(StrEnum):
    """Which of §11.2's five ceilings a breach belongs to. The breach behaviours differ."""

    TASK = "TASK"
    REPO = "REPO"
    REVALIDATION = "REVALIDATION"
    WAVE = "WAVE"
    RUN = "RUN"


class LedgerBreach(BudgetExhausted):
    """A ceiling refused this dispatch. Subclasses `llm.client.BudgetExhausted` so the existing
    `except BudgetExhausted` in the LLM path keeps working and `classify_exception` still maps it
    to `FailureClass.BUDGET_EXHAUSTED` — the *class* is what tells a caller which ceiling broke."""

    ceiling: ClassVar[BudgetCeiling] = BudgetCeiling.RUN
    exit_code: ClassVar[int | None] = None
    """The process exit code this breach terminates the run with, or `None` when the run
    continues (a task- or repo-scoped breach never stops the fleet, §11.2)."""


class TaskTokenBudgetExhausted(LedgerBreach):
    """`task_max_tokens` for one `TransformTask`. The attempt fails and the ladder advances."""

    ceiling: ClassVar[BudgetCeiling] = BudgetCeiling.TASK


class RepoBudgetExhausted(LedgerBreach):
    """`repo_max_cost_usd`, blast-radius scaled. Repo → `REQUIRES_HUMAN_INTERVENTION`,
    `blocked_by` propagates (§3.5), and the fleet keeps going."""

    ceiling: ClassVar[BudgetCeiling] = BudgetCeiling.REPO


class RevalidationBudgetExhausted(LedgerBreach):
    """`stubs.revalidation_max_cost_usd`, a SUB-ceiling inside the repo ceiling (§3.5.1).

    Stub → `ABANDONED`, repo **stays `DEGRADED`**, PR held as a draft. Never a promotion to
    `SUCCEEDED`: an exhausted budget means unfinished work, not finished work.
    """

    ceiling: ClassVar[BudgetCeiling] = BudgetCeiling.REVALIDATION


class WaveBudgetExhausted(LedgerBreach):
    """`wave_max_cost_usd_per_repo × COUNT(wave_members)`. The wave stops admitting, in-flight
    work drains, and the run **halts rather than silently starting the next wave**: exit 10."""

    ceiling: ClassVar[BudgetCeiling] = BudgetCeiling.WAVE
    exit_code: ClassVar[int | None] = WAVE_BUDGET_EXIT_CODE


class RunBudgetExhausted(LedgerBreach):
    """`run_max_cost_usd`: `spent_usd + estimate` breaches the ceiling on its own, so no amount
    of waiting can help. Exit 3."""

    ceiling: ClassVar[BudgetCeiling] = BudgetCeiling.RUN
    exit_code: ClassVar[int | None] = RUN_BUDGET_EXIT_CODE


class LedgerHalted(RunBudgetExhausted):
    """The ledger is halted and no further LLM call may be dispatched by any worker (§11.2).

    Raised *before* the backend is reached, which is the whole content of "fail-closed is a
    state": a halted run that still dispatches is a halted run in name only.
    """


class BackpressureTimeout(LedgerBreach):
    """In-flight reservations could have released the headroom, but did not within the wait
    bound. A task-scoped failure — the run is healthy, this dispatch simply waited long enough."""

    ceiling: ClassVar[BudgetCeiling] = BudgetCeiling.RUN


# --------------------------------------------------------------------------------------
# estimation — p95 of observed usage, floored; never `max_tokens`
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CostEstimate:
    """What a dispatch is expected (or was observed) to consume. Tokens *and* dollars, because
    §11.2's task ceiling is in tokens and the other four are in dollars."""

    in_tokens: int
    out_tokens: int
    usd: float

    @property
    def tokens(self) -> int:
        """Every token this call is billed for, against `task_max_tokens`."""
        return self.in_tokens + self.out_tokens


ZERO_COST: Final = CostEstimate(in_tokens=0, out_tokens=0, usd=0.0)


def estimate_cost(target: BackendTarget, *, in_tokens: int, out_tokens: int) -> CostEstimate:
    """`price(target, n)` from §11.2, wrapped with the token counts it was computed from.

    A `price: free` target reserves and spends `0.0` with the ledger machinery fully live — which
    is why `llm_cache_hit`, and never `cost_usd == 0`, is the cache signal (§11.6).
    """
    usd = target_price_usd(target, in_tokens=in_tokens, out_tokens=out_tokens)
    return CostEstimate(in_tokens=in_tokens, out_tokens=out_tokens, usd=usd)


@dataclass(slots=True)
class TokenEstimator:
    """`max(role_p95_observed, role_floor)` per `(role, tier)` — §11.2's estimate, exactly.

    The alternative that this class exists to refuse is `max_tokens_in + max_tokens_out`. That
    reservation is not merely pessimistic: against a $6 repo ceiling it makes the HEAVY rung
    undispatchable, so a repo whose real spend was $0.60 dies at attempt 3 with a ceiling it
    never came close to. The p95 is read from this run's own observations and floored, so the
    estimate is wrong by a bounded factor rather than by an order of magnitude.
    """

    floors: Mapping[tuple[str, ModelTier], tuple[int, int]] = field(default_factory=dict)
    default_floor: tuple[int, int] = DEFAULT_ROLE_FLOOR
    min_samples: int = P95_MIN_SAMPLES
    _samples: dict[tuple[str, ModelTier], list[tuple[int, int]]] = field(
        default_factory=dict, repr=False
    )

    def observe(self, role: str, tier: ModelTier, *, in_tokens: int, out_tokens: int) -> None:
        """Record one completed call's real usage. Fed from the run's own `attempts` rows on
        resume, so a resumed run does not start estimating from the floor again."""
        self._samples.setdefault((role, tier), []).append((in_tokens, out_tokens))

    def sample_count(self, role: str, tier: ModelTier) -> int:
        return len(self._samples.get((role, tier), ()))

    def floor_for(self, role: str, tier: ModelTier) -> tuple[int, int]:
        return self.floors.get((role, tier), self.default_floor)

    def estimate(self, target: BackendTarget, *, role: str, tier: ModelTier) -> CostEstimate:
        """The reservation for the next `(role, tier)` dispatch on `target`.

        Below `min_samples` the p95 of a handful of calls is noise, so the floor wins outright;
        above it the two are combined component-wise, because an input-heavy role and an
        output-heavy role have different shapes and one scalar would flatten both.
        """
        floor_in, floor_out = self.floor_for(role, tier)
        samples = self._samples.get((role, tier), [])
        if len(samples) >= self.min_samples:
            p95_in = _p95([s[0] for s in samples])
            p95_out = _p95([s[1] for s in samples])
        else:
            p95_in, p95_out = 0, 0
        return estimate_cost(
            target, in_tokens=max(p95_in, floor_in), out_tokens=max(p95_out, floor_out)
        )


def _p95(values: list[int]) -> int:
    """Nearest-rank p95: no interpolation, so the estimate is a value that really occurred."""
    ordered = sorted(values)
    index = math.ceil(0.95 * len(ordered)) - 1
    return ordered[max(index, 0)]


# --------------------------------------------------------------------------------------
# ceilings and scopes
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Ceilings:
    """The five §11.2 numbers, resolved from config once and passed down."""

    run_max_usd: float
    wave_max_usd_per_repo: float
    repo_max_usd: float
    repo_max_ceiling_usd: float
    revalidation_max_usd: float
    max_revalidation_rounds: int
    task_max_tokens: int

    @classmethod
    def from_settings(cls, budgets: BudgetsSection, stubs: StubsSection) -> Ceilings:
        return cls(
            run_max_usd=budgets.run_max_cost_usd,
            wave_max_usd_per_repo=budgets.wave_max_cost_usd_per_repo,
            repo_max_usd=budgets.repo_max_cost_usd,
            repo_max_ceiling_usd=budgets.repo_max_cost_ceiling_usd,
            revalidation_max_usd=stubs.revalidation_max_cost_usd,
            max_revalidation_rounds=stubs.max_revalidation_rounds,
            task_max_tokens=budgets.task_max_tokens,
        )

    def wave_max_usd(self, members: int) -> float:
        """§11.2: the wave ceiling **scales with `COUNT(wave_members)`**. A fixed per-wave figure
        would halt a 40-repo wave for being large while leaving a 2-repo wave 40× over-funded."""
        return self.wave_max_usd_per_repo * max(members, 0)

    def repo_max_usd_for(self, blast_radius: int) -> float:
        """§3.5: `repo_max_cost_usd` scaled by `1 + log2(1 + blast_radius)`, capped at
        `repo_max_cost_ceiling_usd`. A repo 40 dependents are waiting on is worth more attempts
        than a leaf, and the cap is what keeps "worth more" from meaning "unbounded"."""
        scaled = self.repo_max_usd * (1.0 + math.log2(1 + max(blast_radius, 0)))
        return min(scaled, self.repo_max_ceiling_usd)


class SpendKind(StrEnum):
    """Which cost class a dispatch belongs to. `REVALIDATION` is priced separately so "the stub
    rework was free" cannot be asserted again (§3.5.1)."""

    NORMAL = "NORMAL"
    REVALIDATION = "REVALIDATION"


@dataclass(frozen=True, slots=True)
class SpendScope:
    """Who is spending. Every sub-ceiling is keyed off exactly these fields."""

    repo_id: str
    task_id: str | None = None
    wave_index: int | None = None
    wave_members: int = 0
    blast_radius: int = 0
    kind: SpendKind = SpendKind.NORMAL
    #: The phase lease this dispatch is covered by, when there is one. Carried onto the durable
    #: `reservations` row so the reaper can bump the OWNER's fence in the same transaction it
    #: releases the hold — a reaped worker must not be able to settle money it no longer holds.
    #: `None` is a dispatch outside a lease; it is still reaped by expiry, just with no fence.
    phase: Phase | None = None
    lease_fence: int | None = None


@dataclass(slots=True)
class Reservation:
    """One granted reservation. Held until `settle()` moves it into `spent_usd`.

    `actual` is what the call really cost. It exists because §11.2's reconciliation is the other
    half of reserving at a p95: an estimate that is never corrected is just a slower overshoot.
    """

    reservation_id: str
    scope: SpendScope
    estimate: CostEstimate
    granted_at: datetime
    actual: CostEstimate | None = None
    settled: bool = False

    def record(self, actual: CostEstimate) -> None:
        """Report the real usage. The reservation settles against this, not against the estimate."""
        self.actual = actual


@dataclass(slots=True)
class _Bucket:
    """One scope's share of the ledger. Sub-ceilings are enforced against `committed_usd`, so a
    ceiling cannot be overshot by two in-flight dispatches that each fit on their own."""

    spent_usd: float = 0.0
    reserved_usd: float = 0.0
    reserved_tokens: int = 0
    spent_tokens: int = 0

    @property
    def committed_usd(self) -> float:
        return self.spent_usd + self.reserved_usd

    @property
    def committed_tokens(self) -> int:
        return self.spent_tokens + self.reserved_tokens


class LedgerStore(Protocol):
    """The durable primitives this policy layer needs, and nothing else (Guardrail 3).

    Deliberately narrower than `StateRepository`: the ledger has no business being handed a
    handle that can insert symbols, and a narrow Protocol is what lets the §6 ADR-0004 swap to
    Postgres happen without this module noticing.

    Note what is *absent*: a run-only `reserve_budget`. Every dispatch belongs to a repo
    (`SpendScope.repo_id` is not optional), and reserving against the run ledger alone is the
    hole this Protocol closes — see `state.repository.reserve_repo_budget` for why the pair is
    nested rather than independent.
    """

    async def get_budget(self, run_id: str) -> BudgetLedgerRow | None: ...

    async def get_repo_budget(self, run_id: str, repo_id: str) -> RepoLedgerRow | None: ...

    async def halt_budget_ledger(self, run_id: str, *, now: datetime) -> None: ...

    async def open_repo_ledger(
        self,
        run_id: str,
        repo_id: str,
        *,
        max_usd: float,
        now: datetime,
        revalidation_max_usd: float = ...,
    ) -> None: ...

    async def reserve_repo_budget(
        self,
        run_id: str,
        repo_id: str,
        *,
        reservation_id: str,
        amount_usd: float,
        now: datetime,
        expires_at: datetime | None = ...,
        phase: Phase | None = ...,
        lease_fence: int | None = ...,
    ) -> None: ...

    async def settle_repo_budget(
        self,
        run_id: str,
        repo_id: str,
        *,
        reservation_id: str,
        reserved_usd: float,
        actual_usd: float,
        now: datetime,
        revalidation: bool = ...,
    ) -> None: ...


# --------------------------------------------------------------------------------------
# the ledger policy
# --------------------------------------------------------------------------------------


class CostLedger:
    """Reserve-then-spend policy over the durable `budget_ledger` / `repo_ledger` CAS (§11.2).

    The run **and** repo ceilings are enforced by SQL statements in `state/repository.py`, held
    in one transaction (see `reserve_repo_budget` for the nesting rationale) — which is why they
    hold across processes and across a crash. The in-process buckets below are a *pre-check*, not
    the enforcement: they refuse the hopeless dispatch one round-trip earlier and they carry the
    two ceilings that have no durable home in §6's schema at all.

    **What is durable and what is not, stated plainly** (Rule 11 — a ceiling nobody can name the
    lifetime of is a ceiling nobody can trust):

    | Ceiling | Durable? | Where |
    |---|---|---|
    | run | yes | `budget_ledger` CAS |
    | repo | yes | `repo_ledger` CAS, nested inside the run's |
    | revalidation | yes | `repo_ledger.revalidation_usd`, settled in the same statement |
    | wave | derived | `SUM(repo_ledger.spent_usd)` over the wave's members (§6 `waves`) |
    | task tokens | no | in-process only: §6 has no per-task token ledger, and a task does not
      outlive the process that claimed it — its lease is reaped and the rung re-runs |

    On construction the in-process repo bucket is empty; it is seeded from the durable row the
    first time a repo is touched, so a **resumed** process pre-checks against what the previous
    one actually spent instead of starting that repo at zero.
    """

    def __init__(
        self,
        store: LedgerStore,
        *,
        run_id: str,
        ceilings: Ceilings,
        clock: Callable[[], datetime] = utcnow,
        monotonic: Callable[[], float] = time.monotonic,
        wait_timeout_s: float = 1800.0,
        wait_poll_s: float = 0.25,
        reservation_ttl_s: float | None = None,
    ) -> None:
        self._store = store
        self._run_id = run_id
        self._ceilings = ceilings
        self._clock = clock
        self._monotonic = monotonic
        #: §11.2 bounds the wait by `budgets.task_max_wallclock_s`; the runner passes the phase's.
        self._wait_timeout_s = wait_timeout_s
        #: A settlement in *another* process signals no condition here, so the wait re-polls the
        #: durable row at this interval instead of sleeping until a local wake that never comes.
        self._wait_poll_s = wait_poll_s
        self._reservation_ttl_s = reservation_ttl_s
        self._halted = False
        self._halt_reason: str | None = None
        self._cond = asyncio.Condition()
        self._settlements = 0
        self._repos: dict[str, _Bucket] = {}
        #: `repo_id -> the durable ceiling this process has already opened`. §3.5 scales the repo
        #: ceiling by a blast radius that only grows, so the row is re-opened when the scaled
        #: ceiling rises and left alone otherwise — one write per repo, not one per dispatch.
        self._repo_ceilings: dict[str, float] = {}
        self._repo_lock = asyncio.Lock()
        self._waves: dict[int, _Bucket] = {}
        self._tasks: dict[str, _Bucket] = {}
        self._revalidation: dict[str, _Bucket] = {}

    # -- state ------------------------------------------------------------------------

    @property
    def halted(self) -> bool:
        """Fail-closed is a state. Sticky: nothing in this class clears it, by construction —
        §11.2 allows exactly one way out, an audited `fleet resume --raise-budget`."""
        return self._halted

    @property
    def halt_reason(self) -> str | None:
        return self._halt_reason

    async def halt(self, reason: str) -> None:
        """Enter the halted state **durably** and wake every waiter so it raises instead of
        waiting out its timeout. In-flight calls are NOT cancelled — cancelling them wastes what
        was paid for.

        The in-process flag is set *before* the write, so this process stops dispatching even if
        the write then fails; the write is what makes the halt outlive the process, and it is not
        best-effort — a halt that could not be persisted is raised, never logged and swallowed.
        Every other process observes it through `budget_ledger.halted = 1`, which the reservation
        CAS itself tests (`WHERE ... AND halted = 0`), so no worker anywhere can dispatch after it.
        """
        self._halted = True
        self._halt_reason = reason
        try:
            await self._store.halt_budget_ledger(self._run_id, now=self._clock())
        finally:
            async with self._cond:
                self._cond.notify_all()

    async def refresh(self) -> BudgetLedgerRow:
        """Read the durable row. `fleet resume` calls this before dispatching anything, because
        an in-process ledger cannot survive the crash it exists to bound."""
        row = await self._store.get_budget(self._run_id)
        if row is None:
            raise RunBudgetExhausted(
                f"no budget_ledger row exists for run {self._run_id}: a run with no durable "
                "ledger cannot be paced, and pacing it in memory is what §11.2 forbids"
            )
        if row.halted:
            self._halted = True
            self._halt_reason = self._halt_reason or "budget_ledger.halted = 1 (durable, sticky)"
        return row

    async def call_budget(self, *, scope: SpendScope, deadline: float) -> CallBudget:
        """§11.2: a `CallBudget` is constructed **per dispatch from the ledger**, carried on
        `WorkerContext`, and re-checked against each failover target."""
        row = await self.refresh()
        remaining_usd = max(0.0, row.max_usd - row.spent_usd - row.reserved_usd)
        remaining_tokens = self._ceilings.task_max_tokens
        if scope.task_id is not None:
            bucket = self._tasks.get(scope.task_id)
            if bucket is not None:
                remaining_tokens = max(0, remaining_tokens - bucket.committed_tokens)
        return CallBudget(
            remaining_tokens=remaining_tokens, remaining_usd=remaining_usd, deadline=deadline
        )

    # -- reserve / settle ---------------------------------------------------------------

    async def reserve(
        self, estimate: CostEstimate, *, scope: SpendScope, timeout_s: float | None = None
    ) -> Reservation:
        """Hold `estimate` against every ceiling, waiting out in-flight reservations if that is
        all that stands in the way.

        Raises the ceiling-specific `LedgerBreach` subclass. Never returns a falsy value: a
        refusal a caller can drop on the floor is a ceiling that is enforced by convention.
        """
        wait_deadline = self._monotonic() + (
            self._wait_timeout_s if timeout_s is None else timeout_s
        )
        while True:
            self._refuse_if_halted(estimate, scope)
            await self._ensure_repo_ledger(scope)
            self._check_subceilings(estimate, scope)
            epoch = self._settlements
            # Minted per ATTEMPT, never per `reserve()` call: a refused attempt is rolled back
            # whole, and re-using its id on the retry would collide with a row that only *might*
            # not exist. The id is what the reaper attributes the money to (§6, v8).
            reservation_id = self._mint_reservation_id()
            try:
                await self._store.reserve_repo_budget(
                    self._run_id,
                    scope.repo_id,
                    reservation_id=reservation_id,
                    amount_usd=estimate.usd,
                    now=self._clock(),
                    expires_at=self._expires_at(),
                    phase=scope.phase,
                    lease_fence=scope.lease_fence,
                )
            except ReservationRefusedError:
                # No ceiling refused — the id itself was rejected. There is no headroom a wait
                # could free, so this must not enter the backpressure loop below.
                raise
            except BudgetRefusedError as refusal:
                # WHICH ledger refused decides what a wait could even accomplish, so the two are
                # interpreted separately — but both may be backpressure, and the tail is shared.
                if isinstance(refusal, RepoBudgetRefusedError):
                    await self._interpret_repo_refusal(estimate, scope, refusal)
                else:
                    await self._interpret_refusal(estimate, refusal)
                if self._monotonic() >= wait_deadline:
                    raise BackpressureTimeout(
                        f"waited {self._wait_timeout_s if timeout_s is None else timeout_s:.1f}s "
                        f"for ${estimate.usd:.4f} of headroom on run {self._run_id} and the "
                        "in-flight reservations never released it"
                    ) from refusal
                await self._wait_for_settlement(epoch, wait_deadline)
                continue
            reservation = Reservation(
                reservation_id=reservation_id,
                scope=scope,
                estimate=estimate,
                granted_at=self._clock(),
            )
            self._hold(reservation)
            return reservation

    async def settle(self, reservation: Reservation, actual: CostEstimate | None = None) -> None:
        """Release the reservation **in full** and grow `spent_usd` by the actual cost (§11.2).

        This is the half that makes a p95 estimate safe: the released headroom is immediately
        available to the next caller, so an over-estimate paces one call rather than the run.
        """
        if reservation.settled:
            raise RunBudgetExhausted(
                f"reservation for {reservation.scope.repo_id} was already settled; settling it "
                "twice would double-count the spend and silently lower the ceiling"
            )
        spend = actual if actual is not None else (reservation.actual or ZERO_COST)
        await self._store.settle_repo_budget(
            self._run_id,
            reservation.scope.repo_id,
            reservation_id=reservation.reservation_id,
            reserved_usd=reservation.estimate.usd,
            actual_usd=spend.usd,
            now=self._clock(),
            revalidation=reservation.scope.kind is SpendKind.REVALIDATION,
        )
        reservation.settled = True
        reservation.actual = spend
        self._release(reservation, spend)
        async with self._cond:
            self._settlements += 1
            self._cond.notify_all()

    @asynccontextmanager
    async def dispatch(
        self,
        estimate: CostEstimate,
        *,
        scope: SpendScope,
        deadline: float,
        timeout_s: float | None = None,
    ) -> AsyncIterator[tuple[Reservation, CallBudget]]:
        """Reserve, hand out the `CallBudget`, and reconcile on the way out — including when the
        body raises, because a reservation leaked by a failed call ratchets the ledger closed.

        A halted ledger raises **before the body runs**, so no call reaches a backend (§11.2).
        """
        reservation = await self.reserve(estimate, scope=scope, timeout_s=timeout_s)
        budget = await self.call_budget(scope=scope, deadline=deadline)
        try:
            yield reservation, budget
        finally:
            if not reservation.settled:
                await self.settle(reservation)

    # -- internals ----------------------------------------------------------------------

    @staticmethod
    def _mint_reservation_id() -> str:
        """A fresh UUID4 per granted hold. Uniqueness is the only requirement — the durable row
        carries run, repo, phase and fence, so the id itself need encode nothing."""
        return str(uuid.uuid4())

    def _expires_at(self) -> datetime | None:
        """A reservation carries an expiry so a killed worker's hold is reaped with its lease
        rather than ratcheting `reserved_usd` up until a healthy run halts (§6, §11.5)."""
        if self._reservation_ttl_s is None:
            return None
        return self._clock() + timedelta(seconds=self._reservation_ttl_s)

    async def _ensure_repo_ledger(self, scope: SpendScope) -> None:
        """Open this repo's durable ledger row before its first reservation, and seed the bucket.

        Two jobs, both once per repo per process. Opening is what makes the repo CAS possible at
        all — a missing row means every reservation matches zero rows, which is fail-closed but
        useless. Seeding `spent_usd` from that row is what makes a **resumed** process honest:
        without it the in-process pre-check believes the repo has spent nothing, and while the
        durable CAS would still refuse the overspend, it would do so as a surprise at dispatch
        time instead of as the ceiling the scheduler can see.

        The row is re-opened only when §3.5's blast-radius scaling has *raised* the ceiling, so
        this is one write per repo in the common case rather than one per dispatch. The lock is
        what keeps 24 concurrent dispatches of one repo from racing here: without it a second
        caller could see the ceiling recorded and reach the CAS before the row it addresses
        exists, and seeding could overwrite a settlement that landed mid-await. Seeding therefore
        happens exactly once, at first touch, before any reservation for that repo is in flight.
        """
        repo_max = self._ceilings.repo_max_usd_for(scope.blast_radius)
        async with self._repo_lock:
            opened = self._repo_ceilings.get(scope.repo_id)
            if opened is not None and repo_max <= opened + _USD_EPSILON:
                return
            await self._store.open_repo_ledger(
                self._run_id,
                scope.repo_id,
                max_usd=repo_max,
                now=self._clock(),
                revalidation_max_usd=self._ceilings.revalidation_max_usd,
            )
            self._repo_ceilings[scope.repo_id] = repo_max
            if opened is None:
                row = await self._store.get_repo_budget(self._run_id, scope.repo_id)
                if row is not None:
                    self._repos.setdefault(scope.repo_id, _Bucket()).spent_usd = row.spent_usd
                    self._revalidation.setdefault(
                        scope.repo_id, _Bucket()
                    ).spent_usd = row.revalidation_usd

    def _refuse_if_halted(self, estimate: CostEstimate, scope: SpendScope) -> None:
        if self._halted:
            raise LedgerHalted(
                f"run {self._run_id} is halted ({self._halt_reason}); refusing to dispatch "
                f"${estimate.usd:.4f} for {scope.repo_id} — no LLM call may be dispatched by any "
                "worker in any process while the ledger is halted (§11.2)"
            )

    def _check_subceilings(self, estimate: CostEstimate, scope: SpendScope) -> None:
        """The four ceilings inside the run ceiling, narrowest first, each with its own type."""
        if scope.task_id is not None:
            task = self._tasks.get(scope.task_id, _Bucket())
            if task.committed_tokens + estimate.tokens > self._ceilings.task_max_tokens:
                raise TaskTokenBudgetExhausted(
                    f"task {scope.task_id} would reach "
                    f"{task.committed_tokens + estimate.tokens} tokens against "
                    f"task_max_tokens={self._ceilings.task_max_tokens}: the attempt fails "
                    "BUDGET_EXHAUSTED and the ladder advances"
                )

        if scope.kind is SpendKind.REVALIDATION:
            reval = self._revalidation.get(scope.repo_id, _Bucket())
            reval_max = self._ceilings.revalidation_max_usd
            if reval.committed_usd + estimate.usd > reval_max + _USD_EPSILON:
                raise RevalidationBudgetExhausted(
                    f"stub revalidation for {scope.repo_id} would reach "
                    f"${reval.committed_usd + estimate.usd:.4f} against "
                    f"${reval_max:.2f}: the stub is ABANDONED and the "
                    "repo stays DEGRADED — an exhausted budget means unfinished work (§3.5.1)"
                )

        repo = self._repos.get(scope.repo_id, _Bucket())
        repo_max = self._ceilings.repo_max_usd_for(scope.blast_radius)
        if repo.committed_usd + estimate.usd > repo_max + _USD_EPSILON:
            raise RepoBudgetExhausted(
                f"repo {scope.repo_id} would reach ${repo.committed_usd + estimate.usd:.4f} "
                f"against ${repo_max:.4f} (blast_radius={scope.blast_radius}): the repo goes to "
                "REQUIRES_HUMAN_INTERVENTION and the fleet continues"
            )

        if scope.wave_index is not None:
            wave = self._waves.get(scope.wave_index, _Bucket())
            wave_max = self._ceilings.wave_max_usd(scope.wave_members)
            if wave.committed_usd + estimate.usd > wave_max + _USD_EPSILON:
                raise WaveBudgetExhausted(
                    f"wave {scope.wave_index} would reach "
                    f"${wave.committed_usd + estimate.usd:.4f} against ${wave_max:.4f} "
                    f"(= ${self._ceilings.wave_max_usd_per_repo:.2f} × {scope.wave_members} "
                    f"members): the wave stops admitting, in-flight work drains, and the run "
                    f"halts with exit {WAVE_BUDGET_EXIT_CODE} rather than silently starting the "
                    "next wave on a wrong assumption"
                )

    async def _interpret_refusal(self, estimate: CostEstimate, refusal: BudgetRefusedError) -> None:
        """Decide what a zero-row CAS *meant*. Returns only when waiting could still help.

        This is §11.2's distinction in code: the shortfall is either held by reservations that
        will release — backpressure — or it is spend that is already gone, which no wait can undo.
        """
        row = await self._store.get_budget(self._run_id)
        if row is None:
            raise RunBudgetExhausted(
                f"budget CAS refused for run {self._run_id} and no ledger row exists"
            ) from refusal
        if row.halted:
            self._halted = True
            self._halt_reason = self._halt_reason or "budget_ledger.halted = 1 (durable, sticky)"
            raise LedgerHalted(
                f"run {self._run_id} is halted; ${estimate.usd:.4f} refused before dispatch"
            ) from refusal
        if row.spent_usd + estimate.usd > row.max_usd + _USD_EPSILON:
            if row.spent_usd >= row.max_usd - _USD_EPSILON:
                # The ceiling is not merely tight, it is reached: nothing this run does next can
                # be paid for, so the state — not just this call — is closed. Durably: the next
                # process must not rediscover this by spending its way back up to the ceiling.
                await self.halt(
                    f"run_max_cost_usd ${row.max_usd:.2f} reached (spent ${row.spent_usd:.4f})"
                )
            raise RunBudgetExhausted(
                f"${estimate.usd:.4f} refused for run {self._run_id}: spent ${row.spent_usd:.4f} "
                f"+ estimate exceeds max ${row.max_usd:.2f} on its own, so no amount of waiting "
                f"can help (exit {RUN_BUDGET_EXIT_CODE})"
            ) from refusal

    async def _interpret_repo_refusal(
        self, estimate: CostEstimate, scope: SpendScope, refusal: RepoBudgetRefusedError
    ) -> None:
        """The repo half of `_interpret_refusal`. Returns only when waiting could still help.

        The same distinction, one ledger down: money already *spent* by this repo is gone and no
        settlement will bring it back, so the repo is finished and goes to a human (§11.2). Money
        merely *held* by this repo's other in-flight dispatches will release, so the caller waits.

        The durable row is *read* here and never written back into the in-process buckets: a
        settlement that lands between the durable write and its in-memory release would otherwise
        be counted twice. The buckets are seeded once, in `_ensure_repo_ledger`; the row is the
        authority everywhere else.
        """
        row = await self._store.get_repo_budget(self._run_id, scope.repo_id)
        if row is None:
            raise RepoBudgetExhausted(
                f"repo {scope.repo_id} has no repo_ledger row in run {self._run_id}: a repo with "
                "no durable ledger cannot be paced, and pacing it in memory is the defect (§3.5)"
            ) from refusal
        if (
            scope.kind is SpendKind.REVALIDATION
            and row.revalidation_usd + estimate.usd > row.revalidation_max_usd + _USD_EPSILON
        ):
            raise RevalidationBudgetExhausted(
                f"stub revalidation for {scope.repo_id} has spent ${row.revalidation_usd:.4f} of "
                f"${row.revalidation_max_usd:.2f} durably: the stub is ABANDONED and the repo "
                "stays DEGRADED (§3.5.1)"
            ) from refusal
        if row.spent_usd + estimate.usd > row.max_usd + _USD_EPSILON:
            raise RepoBudgetExhausted(
                f"repo {scope.repo_id} has spent ${row.spent_usd:.4f} of its durable "
                f"${row.max_usd:.4f} ceiling and ${estimate.usd:.4f} more does not fit on its "
                "own, so no amount of waiting can help: the repo goes to "
                "REQUIRES_HUMAN_INTERVENTION and the fleet continues (§11.2)"
            ) from refusal

    async def _wait_for_settlement(self, epoch: int, wait_deadline: float) -> None:
        """Block until a reservation settles, the ledger halts, or the poll interval elapses.

        The epoch check closes the missed-wakeup race: a settlement between the refused CAS and
        this wait must not park the caller for a wake that has already happened.
        """
        async with self._cond:
            if self._settlements != epoch or self._halted:
                return
            remaining = max(0.0, wait_deadline - self._monotonic())
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    self._cond.wait(), timeout=min(self._wait_poll_s, remaining)
                )

    def _hold(self, reservation: Reservation) -> None:
        scope, estimate = reservation.scope, reservation.estimate
        self._repos.setdefault(scope.repo_id, _Bucket()).reserved_usd += estimate.usd
        if scope.kind is SpendKind.REVALIDATION:
            self._revalidation.setdefault(scope.repo_id, _Bucket()).reserved_usd += estimate.usd
        if scope.wave_index is not None:
            self._waves.setdefault(scope.wave_index, _Bucket()).reserved_usd += estimate.usd
        if scope.task_id is not None:
            self._tasks.setdefault(scope.task_id, _Bucket()).reserved_tokens += estimate.tokens

    def _release(self, reservation: Reservation, actual: CostEstimate) -> None:
        """Released in full, spend grown by the actual — the same reconciliation the SQL does,
        applied to the sub-ledgers so an over-estimate frees repo and wave headroom too."""
        scope, estimate = reservation.scope, reservation.estimate
        repo = self._repos.setdefault(scope.repo_id, _Bucket())
        repo.reserved_usd = max(0.0, repo.reserved_usd - estimate.usd)
        repo.spent_usd += actual.usd
        if scope.kind is SpendKind.REVALIDATION:
            reval = self._revalidation.setdefault(scope.repo_id, _Bucket())
            reval.reserved_usd = max(0.0, reval.reserved_usd - estimate.usd)
            reval.spent_usd += actual.usd
        if scope.wave_index is not None:
            wave = self._waves.setdefault(scope.wave_index, _Bucket())
            wave.reserved_usd = max(0.0, wave.reserved_usd - estimate.usd)
            wave.spent_usd += actual.usd
        if scope.task_id is not None:
            task = self._tasks.setdefault(scope.task_id, _Bucket())
            task.reserved_tokens = max(0, task.reserved_tokens - estimate.tokens)
            task.spent_tokens += actual.tokens

    # -- read-back (for the runner's events and `migration_state.json`) ------------------

    def repo_spent_usd(self, repo_id: str) -> float:
        return self._repos.get(repo_id, _Bucket()).spent_usd

    async def repo_headroom_usd(self, repo_id: str) -> float:
        """What this repo may still spend, **read off disk**. Zero when it has no ledger row.

        Async and durable on purpose: the in-process number is this process's share, and the
        question a resumed run asks is what the *repo* has left across every process that ever
        worked on it.
        """
        row = await self._store.get_repo_budget(self._run_id, repo_id)
        return 0.0 if row is None else row.remaining_usd

    def wave_spent_usd(self, wave_index: int) -> float:
        return self._waves.get(wave_index, _Bucket()).spent_usd

    def task_spent_tokens(self, task_id: str) -> int:
        return self._tasks.get(task_id, _Bucket()).spent_tokens


# --------------------------------------------------------------------------------------
# semaphores (§11.1)
# --------------------------------------------------------------------------------------


def new_cpu_pool(max_workers: int) -> ProcessPoolExecutor:
    """The CPU pool for `ctx.limits.cpu_pool`, started via **forkserver, never bare fork**.

    The orchestrator is always multi-threaded by the time a pool is built: the asyncio default
    executor, `asyncio.to_thread`, and aiosqlite each hold threads. `os.fork()` copies only the
    calling thread, so any lock another thread happened to hold is inherited **locked** and the
    child deadlocks on the next allocation or logging call. CPython 3.12 raises a
    `DeprecationWarning` for exactly this, and 3.14 makes `forkserver` the Linux default.

    `forkserver` forks children from a clean single-threaded server process, so it has none of
    that hazard while staying far cheaper than `spawn`. Everything submitted here must therefore
    be a module-level callable with picklable arguments (ADR-0003 already requires that: paths
    in, data out).
    """
    return ProcessPoolExecutor(
        max_workers=max_workers, mp_context=multiprocessing.get_context("forkserver")
    )


class ResizableLimiter:
    """A counting concurrency limiter whose ceiling can change **while slots are held** (§11.8).

    `asyncio.Semaphore` cannot do that. Its `_value` is available-slot count with no public
    setter, and assigning to it does not wake the waiters that the new headroom just admitted, so
    a grow is silently a no-op until the next unrelated `release()`. §11.8's AIMD rule — "halved
    on a 429 or a `retry-after`, one slot returned per clean minute" — is a resize of a live
    ceiling, so the primitive has to be one that supports it. This class is that primitive and
    **nothing more**: it reads no config, subscribes to no signal, and decides no policy. What
    calls `resize()`, and with what, is a later change.

    **Counting, not per-borrower.** One task may hold two slots and is charged two, exactly as
    `asyncio.Semaphore` behaves. That is deliberate: it makes this a drop-in at the one existing
    acquisition site (`workers/classify.py`) rather than a change that must land atomically with
    every other caller.

    Two properties are easy to get wrong and are pinned by tests, not by inspection:

    * **A slot is transferred at wake time, not at resume time.** `_wake_next` charges
      `_borrowed` itself and hands the waiter a settled future. If instead the resumed waiter
      charged itself, every task scheduled between the wake and the resume would see stale
      headroom and barge in over the ceiling.
    * **A cancelled waiter must not swallow a wake.** A task cancelled *after* `_wake_next` chose
      it still owns a slot nobody will release, so `acquire()` gives it back and wakes the next
      waiter in the same breath. Without that, one cancellation permanently shrinks the effective
      ceiling by one.

    Fairness is FIFO, and it falls out of transferring at wake time rather than needing a guard
    in `acquire`: every path that creates headroom — `release`, `resize`, the cancellation
    recovery — drains the queue **synchronously**, with no `await` between freeing a slot and
    charging it to the oldest waiter. So a live waiter can never be parked while headroom exists,
    and an arrival taking the fast path is therefore never jumping a queue.
    """

    __slots__ = ("_borrowed", "_capacity", "_ceiling", "_floor", "_waiters")

    def __init__(self, capacity: int, *, floor: int = 1, ceiling: int | None = None) -> None:
        """`floor` and `ceiling` bound every later `resize`; `ceiling` defaults to `capacity`.

        Defaulting the ceiling to the starting capacity is what makes a *lowered* tier stay
        lowered: `llm.concurrency_overrides` is the operator saying "run this slower", and a
        controller that grew back to the unoverridden `concurrency.llm.*` would undo that.
        """
        if capacity < 1:
            raise ValueError(f"capacity must be >= 1, got {capacity}")
        if floor < 1:
            raise ValueError(f"floor must be >= 1, got {floor}")
        top = capacity if ceiling is None else ceiling
        if top < floor:
            raise ValueError(f"ceiling {top} is below floor {floor}")
        self._floor = floor
        self._ceiling = top
        self._capacity = max(floor, min(top, capacity))
        self._borrowed = 0
        self._waiters: deque[asyncio.Future[None]] = deque()

    @property
    def capacity(self) -> int:
        """The current ceiling — what `resize` moves."""
        return self._capacity

    @property
    def borrowed(self) -> int:
        """Slots held right now. May exceed `capacity` transiently after a shrink."""
        return self._borrowed

    def locked(self) -> bool:
        """True when the next `acquire()` would park. Mirrors `asyncio.Semaphore.locked()`."""
        return self._borrowed >= self._capacity

    async def acquire(self) -> None:
        """Take a slot, waiting FIFO until one is free."""
        if not self.locked():
            self._borrowed += 1
            return
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters.append(fut)
        try:
            try:
                await fut
            finally:
                self._waiters.remove(fut)
        except asyncio.CancelledError:
            if not fut.cancelled():
                # Woken, then cancelled: we own a slot we will never use. Hand it straight on.
                self._borrowed -= 1
                self._wake_next()
            raise

    def release(self) -> None:
        """Return a slot and wake the next waiter the current capacity admits."""
        if self._borrowed <= 0:
            raise RuntimeError("ResizableLimiter released more times than it was acquired")
        self._borrowed -= 1
        if self._borrowed < self._capacity:
            self._wake_next()

    def resize(self, capacity: int) -> None:
        """Move the ceiling into `[floor, ceiling]`; growing admits waiters immediately.

        A shrink never cancels or errors a current holder — `borrowed` is simply allowed to sit
        above `capacity` until it drains, which is what "halved on a 429" has to mean for calls
        already in flight.
        """
        if capacity < 1:
            raise ValueError(f"capacity must be >= 1, got {capacity}")
        self._capacity = max(self._floor, min(self._ceiling, capacity))
        while self._borrowed < self._capacity and self._wake_next():
            pass

    def _wake_next(self) -> bool:
        """Charge a slot to the first live waiter and settle its future. True if one was woken."""
        for fut in self._waiters:
            if not fut.done():
                self._borrowed += 1
                fut.set_result(None)
                return True
        return False

    async def __aenter__(self) -> None:
        await self.acquire()

    async def __aexit__(self, *exc_info: object) -> None:
        self.release()


@dataclass(slots=True)
class Limits:
    """All concurrency ceilings and the cost ledger for one run. Handed to workers, never imported.

    `llm` is keyed by **tier, not by backend** (ADR-0023), so a failover inherits the tier's
    budget instead of opening a second unbounded lane.
    """

    git_net: asyncio.Semaphore
    subprocess: asyncio.Semaphore
    docker: asyncio.Semaphore
    llm: Mapping[ModelTier, ResizableLimiter]
    cpu_pool: Executor
    ledger: CostLedger

    @classmethod
    def create(
        cls,
        concurrency: ConcurrencySection,
        *,
        ledger: CostLedger,
        cpu_pool: Executor | None = None,
        llm_overrides: Mapping[ModelTier, int] | None = None,
    ) -> Limits:
        """Construct from `config/fleet.yaml#concurrency`. `llm.concurrency_overrides` may only
        LOWER a tier: "run this slower" is the intended response to throttling (§11.8)."""
        overrides = llm_overrides or {}
        llm = {
            tier: ResizableLimiter(
                max(1, min(concurrency.llm.for_tier(tier), overrides.get(tier, 1 << 30)))
            )
            for tier in ModelTier
        }
        return cls(
            git_net=asyncio.Semaphore(concurrency.git_net),
            subprocess=asyncio.Semaphore(concurrency.subprocess),
            docker=asyncio.Semaphore(concurrency.docker),
            llm=llm,
            cpu_pool=cpu_pool or new_cpu_pool(concurrency.cpu_pool_workers),
            ledger=ledger,
        )

    def for_tier(self, tier: ModelTier) -> ResizableLimiter:
        """The limiter every `ModelClient.complete` on this tier must hold."""
        return self.llm[tier]
