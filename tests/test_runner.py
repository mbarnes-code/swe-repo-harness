"""Behaviour tests for `src/fleet/orchestrator/runner.py` — the wave/phase driver (§11.1, §11.5).

Real temp database, real `StateWriter`, real leases and fences, real `CostLedger`; only the
workers are fake, because what is under test is the driver's discipline and not any phase's
work. Nothing here sleeps for longer than a heartbeat interval.

The properties pinned here are the ones whose absence is silent or catastrophic:

* **A worker exception must not cancel its siblings.** This is the headline. The stub docstring
  this module replaced said the opposite, and implementing it as documented converts one latent
  bug into a fleet stop that discards every sibling's paid-for in-flight LLM and container work
  — once per bug class, on a multi-day run (§11.1).
* **But isolation is not blanket exception-swallowing.** A declared halt state — budget
  exhaustion — must still stop the wave, or "fail-closed" is a comment.
* **A result under a stale fence is discarded, not merged.** A reaped worker that finished late
  has no way to know it lost the lease. If its write lands, two owners have written one repo and
  the reaper's entire guarantee evaporates — silently, with both writes succeeding.
* **A `partial` is checkpointed and re-entry resumes at `remaining_units`.** Otherwise attempt 2
  replays 60 units over an already-rewritten tree and the resulting no-op patches read as
  `RULE_MISS` — a real failure invented out of a successful one.
* **`RetryPolicy` decides; the runner obeys.** A transient failure must not charge an attempt,
  and a substantive one must. Re-implementing that judgement in the driver is how the two
  copies drift.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar, cast
from uuid import UUID

import aiosqlite
import pytest
import structlog
from pydantic import BaseModel, Field

from fleet.graph.sequence import WavePlan
from fleet.llm.client import (
    BackendFailover,
    BackendReply,
    BudgetExhausted,
    CallBudget,
    Message,
    StructuredOutputMode,
)
from fleet.llm.roles import SPEC_ROLE_TIERS, LlmRouter, Role
from fleet.models.enums import (
    ContextPolicy,
    FailureClass,
    ModelTier,
    Phase,
    RepoStatus,
    TransformTier,
)
from fleet.models.graph import MigrationWave
from fleet.models.state import SCHEMA_VERSION
from fleet.models.tasks import BackendTarget, ModelCapabilities, Price, TokenUsage
from fleet.orchestrator.budgets import (
    Ceilings,
    CostEstimate,
    CostLedger,
    Limits,
)
from fleet.orchestrator.context import RunContext, default_logger
from fleet.orchestrator.findings import BACKEND_UNAVAILABLE, CAPABILITY_DRIFT
from fleet.orchestrator.retry import RetryAction, RetryDecision, RetryPolicy
from fleet.orchestrator.runner import (
    HaltReason,
    PhaseCheckpoint,
    PhaseRunner,
    PreDispatchHook,
    ResultSink,
)
from fleet.orchestrator.scheduler import (
    SqliteSchedulerStore,
    WaveNotReadyError,
    WaveScheduler,
    WaveState,
)
from fleet.settings import BudgetsSection, FleetConfig, RunSection
from fleet.state import checkpoints
from fleet.state import db as dbmod
from fleet.state.db import StateWriter, connect_ro, initialize_database
from fleet.state.repository import AttemptRow, LeaseStolenError, SqliteStateRepository
from fleet.workers.base import (
    BaseWorker,
    WorkerContext,
    WorkerError,
    WorkerExecution,
    WorkerInput,
    WorkerOutput,
    WorkerResult,
)

NOW = datetime(2026, 8, 9, 12, 0, 0, tzinfo=UTC)
RUN_ID = UUID("33333333-3333-4333-8333-333333333333")
RUN = str(RUN_ID)
PHASE = Phase.TRANSFORM
UNITS = ("u1", "u2", "u3")

#: `repo_id -> queue of behaviours`, one per dispatch; the last entry repeats. Module level
#: because a worker is a stateless singleton (§7.2) and may not carry a test's script on `self`.
BEHAVIOURS: dict[str, list[Behaviour]] = {}
#: `(repo_id, attempt, tier, units_asked_for, context_policy)` per `run()` call — what the
#: worker ACTUALLY observed, which is the evidence that escalation reached it.
CALLS: list[tuple[str, int, TransformTier, tuple[str, ...], ContextPolicy | None]] = []
#: The rung the RUNNER asked for, per payload built. Kept beside `CALLS` so the two can be
#: compared: the driver's ladder position and the worker's observed rung must be the same
#: number, and for a long time they silently were not.
RUNGS: list[int] = []
#: `(repo_id, units_the_payload_asked_for)` per `preconditions_hold` call. THE regression guard:
#: the §7.1 re-entry check was inert for its whole existence — abstract, implemented by all
#: eleven workers, and invoked by nothing — so "the driver asked" is itself a property under
#: test, not a detail. An empty list here means the guard is dead again.
PRECONDITION_CALLS: list[tuple[str, tuple[str, ...]]] = []
#: `repo_id -> queue of verdicts`, one per check; the last entry repeats. Absent means `True`.
PRECONDITIONS: dict[str, list[bool]] = {}


class ScriptedInput(WorkerInput):
    repo_id: str
    units: list[str] = Field(default_factory=list)


class ScriptedOutput(WorkerOutput):
    note: str = ""


Behaviour = Callable[[WorkerContext, ScriptedInput], Awaitable["WorkerResult[ScriptedOutput]"]]


class ScriptedWorker(BaseWorker[ScriptedInput, ScriptedOutput]):
    """A worker whose only behaviour is the test's. Stateless, as §7.2 requires."""

    __slots__ = ()

    name: ClassVar[str] = "scripted"
    phase: ClassVar[Phase] = PHASE
    input_model: ClassVar[type[WorkerInput]] = ScriptedInput
    output_model: ClassVar[type[WorkerOutput]] = ScriptedOutput
    cancel_grace_s: ClassVar[float] = 0.01
    #: Nothing to disable any more: `RetryPolicy` is the ONLY transient-retry budget, so these
    #: tests no longer have to neuter a second layer to be able to assert on the first.

    async def run(
        self, ctx: WorkerContext, payload: ScriptedInput
    ) -> WorkerResult[ScriptedOutput]:
        CALLS.append(
            (ctx.repo_id, ctx.attempt, ctx.tier, tuple(payload.units), ctx.context_policy)
        )
        script = BEHAVIOURS[ctx.repo_id]
        behaviour = script.pop(0) if len(script) > 1 else script[0]
        return await behaviour(ctx, payload)

    async def preconditions_hold(self, ctx: WorkerContext, payload: ScriptedInput) -> bool:
        """Records what the driver asked and answers the test's script.

        The recording is the point: §7.1's contract is that `True` admits re-entry for
        `remaining_units` alone and `False` sends the phase back to its anchor, and neither is
        worth anything if the driver never asks. `payload.units` is captured so the test can see
        that the question was asked about the RESUMED payload — before the work, not after.
        """
        PRECONDITION_CALLS.append((ctx.repo_id, tuple(payload.units)))
        script = PRECONDITIONS.get(ctx.repo_id)
        if not script:
            return True
        return script.pop(0) if len(script) > 1 else script[0]


class ExplodingPreconditionWorker(ScriptedWorker):
    """A worker whose §7.1 guard raises instead of answering — neither verdict, a defect."""

    __slots__ = ()

    async def preconditions_hold(self, ctx: WorkerContext, payload: ScriptedInput) -> bool:
        raise RuntimeError(f"preconditions_hold exploded for {ctx.repo_id}")


class ExplodingExecuteWorker(ScriptedWorker):
    """A worker whose ladder itself raises — the runner's own boundary, not `execute`'s."""

    __slots__ = ()

    async def execute(
        self,
        ctx: WorkerContext,
        payload: ScriptedInput,
        *,
        max_attempts: int | None = None,
    ) -> WorkerExecution[ScriptedOutput]:
        raise RuntimeError(f"execute() itself exploded for {ctx.repo_id}")


# ---------------------------------------------------------------------------- behaviours


def ok(note: str = "done") -> Behaviour:
    async def behaviour(
        ctx: WorkerContext, payload: ScriptedInput
    ) -> WorkerResult[ScriptedOutput]:
        return WorkerResult(status="ok", output=ScriptedOutput(note=note))

    return behaviour


def boom(message: str) -> Behaviour:
    async def behaviour(
        ctx: WorkerContext, payload: ScriptedInput
    ) -> WorkerResult[ScriptedOutput]:
        raise RuntimeError(message)

    return behaviour


def fails(failure_class: FailureClass, *, retryable: bool = True) -> Behaviour:
    async def behaviour(
        ctx: WorkerContext, payload: ScriptedInput
    ) -> WorkerResult[ScriptedOutput]:
        return WorkerResult(
            status="failed",
            error=WorkerError(
                failure_class=failure_class,
                retryable=retryable,
                stderr_tail=f"{failure_class} on {ctx.repo_id}",
            ),
        )

    return behaviour


def asks_the_model(role: str = "transform_repair") -> Behaviour:
    """A worker that actually uses `ctx.llm`. The LLM findings only exist if a model was called,
    so a behaviour that never calls one cannot exercise the drift path at all."""

    async def behaviour(
        ctx: WorkerContext, payload: ScriptedInput
    ) -> WorkerResult[ScriptedOutput]:
        answer = await ctx.llm.complete(role, [Message(role="user", content="go")], Verdict)
        return WorkerResult(status="ok", output=ScriptedOutput(note=answer.value.summary))

    return behaviour


def asks_the_model_and_bills_it(role: str = "transform_repair") -> Behaviour:
    """Like `asks_the_model`, but propagates the call's real `TokenUsage` onto the returned
    result.

    `reservation.record()` (`runner.py:804-811`) bills `execution.usage`, which `BaseWorker.execute`
    accumulates FROM the `WorkerResult` a behaviour returns (`workers/base.py:741`) — it is never
    read back off `ctx.llm` independently. `asks_the_model` leaves `usage` at its zero default,
    which is fine for the drift-path tests it exists for, but would make a ledger-movement
    assertion vacuous: without this, `budget_ledger.spent_usd` would stay 0 regardless of how many
    priced calls the worker made.
    """

    async def behaviour(
        ctx: WorkerContext, payload: ScriptedInput
    ) -> WorkerResult[ScriptedOutput]:
        answer = await ctx.llm.complete(role, [Message(role="user", content="go")], Verdict)
        return WorkerResult(
            status="ok", output=ScriptedOutput(note=answer.value.summary), usage=answer.usage
        )

    return behaviour


def fails_with(
    failure_class: FailureClass, stderr_tail: str, *, retryable: bool = False
) -> Behaviour:
    """Like `fails`, but with the worker's OWN message — which is what the §13 row 40 finding
    carries, so a test that let `fails` synthesise one would be asserting on the fixture."""

    async def behaviour(
        ctx: WorkerContext, payload: ScriptedInput
    ) -> WorkerResult[ScriptedOutput]:
        return WorkerResult(
            status="failed",
            error=WorkerError(
                failure_class=failure_class, retryable=retryable, stderr_tail=stderr_tail
            ),
        )

    return behaviour


def partial(completed: Sequence[str], remaining: Sequence[str]) -> Behaviour:
    async def behaviour(
        ctx: WorkerContext, payload: ScriptedInput
    ) -> WorkerResult[ScriptedOutput]:
        return WorkerResult(
            status="partial",
            output=ScriptedOutput(note="partial"),
            completed_units=list(completed),
            remaining_units=list(remaining),
        )

    return behaviour


def waits(started: asyncio.Event, gate: asyncio.Event) -> Behaviour:
    async def behaviour(
        ctx: WorkerContext, payload: ScriptedInput
    ) -> WorkerResult[ScriptedOutput]:
        started.set()
        await gate.wait()
        return WorkerResult(status="ok", output=ScriptedOutput(note="late"))

    return behaviour


def waits_for_cancel(started: asyncio.Event, observed: list[bool]) -> Behaviour:
    async def behaviour(
        ctx: WorkerContext, payload: ScriptedInput
    ) -> WorkerResult[ScriptedOutput]:
        started.set()
        await ctx.cancel.wait()
        observed.append(True)
        return WorkerResult(
            status="failed",
            error=WorkerError(
                failure_class=FailureClass.TRANSIENT_INFRA,
                retryable=True,
                stderr_tail="aborted on reclaim without touching git",
            ),
        )

    return behaviour


# ---------------------------------------------------------------------------- harness


class SteppableClock:
    """The orchestrator host's wall clock, moved by hand (§11.5: workers never stamp one)."""

    def __init__(self, start: datetime = NOW) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class RecordingPolicy(RetryPolicy):
    """The real policy, with every judgement recorded so the test can assert the runner OBEYED
    it rather than re-deriving the same answer independently."""

    def decide(self, state: Any, error: WorkerError) -> RetryDecision:
        decision = super().decide(state, error)
        DECISIONS.append(decision)
        return decision


DECISIONS: list[RetryDecision] = []


class Verdict(BaseModel):
    """The typed handoff a rung asks for. Nothing leaves `complete()` unvalidated (ADR-0002)."""

    summary: str


class ScriptedBackend:
    """One transport, offline. Answers every role with the same JSON and counts its turns.

    A real `ModelBackend` rather than a stubbed `ModelClient` on purpose: the thing under test is
    the wiring `RunContext` assembles — router → `CachingModelClient` → `LadderModelClient` →
    backend — and a fake client would replace exactly the part that was broken. Three hops since
    `cac537d` wired §11.6's cache; it was two while the cache was never installed.
    """

    name: ClassVar[str] = "fake"
    version: ClassVar[int] = 1

    def __init__(self) -> None:
        self.calls: list[str] = []
        #: Mutable so a test can make the endpoint DISHONEST — promising the top rung in
        #: `structured_output_modes` while the booleans can honour none of it, which is exactly
        #: the §13 row 37 shape `_emit_drift` reports.
        self.caps = ModelCapabilities(supports_json_schema=True, max_output_tokens=8192)

    def declared_capabilities(self, target: BackendTarget) -> ModelCapabilities:
        return self.caps

    async def invoke(
        self,
        target: BackendTarget,
        messages: Sequence[Message],
        schema: dict[str, object] | None,
        mode: StructuredOutputMode,
        *,
        max_output_tokens: int,
        timeout_s: float,
    ) -> BackendReply:
        self.calls.append(target.model_id)
        return BackendReply(
            text=Verdict(summary="answered offline").model_dump_json(),
            usage=TokenUsage(input_tokens=12, output_tokens=4),
            finish_reason="stop",
        )


def make_router() -> LlmRouter:
    """The shipped role table over one priced fake target per tier — a REAL `LlmRouter`, because
    the tier a role resolves to is what `RunContext` hands workers as `ctx.router`."""
    target = BackendTarget(
        backend="fake", model_id="fake-1", price=Price(in_per_mtok=1.0, out_per_mtok=2.0)
    )
    return LlmRouter(
        dict(SPEC_ROLE_TIERS), dict.fromkeys(ModelTier, (target,)), profile="test"
    )


@dataclass(slots=True)
class Harness:
    repo: SqliteStateRepository
    store: SqliteSchedulerStore
    writer: StateWriter
    read_conn: aiosqlite.Connection
    ctx: RunContext
    clock: SteppableClock
    sleeps: list[float]
    backend: ScriptedBackend

    async def plan(self, *waves: Sequence[str]) -> None:
        await self.store.record_plan(
            RUN,
            WavePlan(
                waves=tuple(
                    MigrationWave(wave_index=index, repo_ids=sorted(members))
                    for index, members in enumerate(waves)
                ),
                wave_index_by_node={},
                cycle_findings=(),
                excluded_repo_ids=(),
            ),
            now=self.clock(),
            max_usd_per_repo=8.0,
        )

    def scheduler(self, *, wave_max_wallclock_s: int = 14_400) -> WaveScheduler:
        return WaveScheduler(
            run_id=RUN,
            phase=PHASE,
            store=self.store,
            db=self.repo,
            budgets=BudgetsSection(wave_max_wallclock_s=wave_max_wallclock_s),
            clock=self.clock,
        )

    def runner(
        self,
        *,
        worker: BaseWorker[ScriptedInput, ScriptedOutput] | None = None,
        scheduler: WaveScheduler | None = None,
        policy: RetryPolicy | None = None,
        estimate: Callable[[str], CostEstimate] | None = None,
        resource_guard: Callable[[], Any] | None = None,
        sink: ResultSink[ScriptedOutput] | None = None,
        pre_dispatch: PreDispatchHook[ScriptedInput] | None = None,
    ) -> PhaseRunner[ScriptedInput, ScriptedOutput]:
        async def payloads(
            *,
            repo_id: str,
            phase: Phase,
            attempt: int,
            remaining_units: Sequence[str] | None,
        ) -> ScriptedInput:
            RUNGS.append(attempt)
            owed = list(UNITS) if remaining_units is None else list(remaining_units)
            return ScriptedInput(repo_id=repo_id, units=owed)

        async def sleep(delay: float) -> None:
            self.sleeps.append(delay)

        return PhaseRunner(
            self.ctx,
            worker or ScriptedWorker(),
            scheduler or self.scheduler(),
            payloads=payloads,
            policy=policy,
            estimate=estimate or (lambda _repo_id: CostEstimate(0, 0, 0.0)),
            resource_guard=resource_guard or (lambda: None),
            sleep=sleep,
            sink=sink,
            pre_dispatch=pre_dispatch,
        )

    async def set_attempts(self, repo_id: str, attempts: int) -> None:
        """Put `phases.attempts` where a previous run would have left it, so the next dispatch
        is a genuine RESUME reading its ladder position off disk rather than off memory."""

        async def unit(conn: aiosqlite.Connection) -> None:
            await conn.execute(
                "UPDATE phases SET attempts = ?, status = 'PENDING' "
                " WHERE run_id = ? AND repo_id = ? AND phase = ?",
                (attempts, RUN, repo_id, int(PHASE)),
            )

        await self.writer.submit(unit)

    async def break_findings_writes(self) -> None:
        """Make every `findings` INSERT fail, and nothing else.

        A real failure through the real `StateWriter`, not a stubbed sink: swapping
        `ctx.llm_findings` after construction cannot work, because `__post_init__` has already
        handed the ORIGINAL sink's bound callbacks to the client — the swap would silence the
        emission path and leave the assertion measuring the stub instead of the code. Dropping
        the table makes the live sink's own write raise while phase, lease and attempt writes
        keep working, which is exactly the blast radius under test.
        """

        async def unit(conn: aiosqlite.Connection) -> None:
            await conn.execute("DROP TABLE findings")

        await self.writer.submit(unit)

    async def findings(self, kind: str) -> list[tuple[str | None, str, dict[str, object]]]:
        """`(repo_id, severity, payload)` per `findings` row of a kind — read raw, because what
        is under test is what an operator would find on disk after the run stopped."""
        async with self.read_conn.execute(
            "SELECT repo_id, severity, payload FROM findings "
            "  WHERE run_id = ? AND kind = ? ORDER BY finding_id",
            (RUN, kind),
        ) as cursor:
            rows = await cursor.fetchall()
        return [(r[0], str(r[1]), dict(json.loads(r[2]))) for r in rows]

    async def phase_row(self, repo_id: str) -> tuple[str, int, int, str | None, str | None]:
        """`(status, attempts, transient_retries, failure_class, last_error)` — read raw, so the
        assertion is about the row a resume would read and not about an in-memory object."""
        async with self.read_conn.execute(
            "SELECT status, attempts, transient_retries, failure_class, last_error "
            "  FROM phases WHERE run_id = ? AND repo_id = ? AND phase = ?",
            (RUN, repo_id, int(PHASE)),
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        return str(row[0]), int(row[1]), int(row[2]), row[3], row[4]


@pytest.fixture(autouse=True)
def _reset_script() -> Iterator[None]:
    for collection in (BEHAVIOURS, CALLS, DECISIONS, RUNGS, PRECONDITIONS, PRECONDITION_CALLS):
        collection.clear()
    yield
    for collection in (BEHAVIOURS, CALLS, DECISIONS, RUNGS, PRECONDITIONS, PRECONDITION_CALLS):
        collection.clear()
    dbmod._release_write_slot()


@pytest.fixture
async def harness(tmp_path: Path) -> AsyncIterator[Harness]:
    async for built in _build(tmp_path, FleetConfig(), max_usd=1000.0):
        yield built


async def _build(
    tmp_path: Path, config: FleetConfig, *, max_usd: float, reservation_ttl_s: float | None = None
) -> AsyncIterator[Harness]:
    path = tmp_path / "state" / "fleet.db"
    await initialize_database(path)
    clock = SteppableClock()
    async with StateWriter(path, owner="test-runner") as writer:
        read_conn = await connect_ro(path)
        try:
            repo = SqliteStateRepository(writer=writer, read_conn=read_conn)
            store = SqliteSchedulerStore(writer=writer, read_conn=read_conn)
            await repo.upsert_run(
                RUN, started_at=NOW, config_sha256="a" * 64, harness_version="0.1.0"
            )
            await repo.open_budget_ledger(RUN, max_usd=max_usd, now=NOW)
            ledger = CostLedger(
                repo,
                run_id=RUN,
                ceilings=Ceilings.from_settings(config.budgets, config.stubs),
                clock=clock,
                wait_timeout_s=0.05,
                wait_poll_s=0.01,
                reservation_ttl_s=reservation_ttl_s,
            )
            backend = ScriptedBackend()
            limits = Limits(
                git_net=asyncio.Semaphore(8),
                subprocess=asyncio.Semaphore(16),
                docker=asyncio.Semaphore(4),
                llm={},
                cpu_pool=cast(Any, None),
                ledger=ledger,
            )
            ctx = RunContext(
                run_id=RUN_ID,
                config=config,
                writer=writer,
                repository=repo,
                read_conn=read_conn,
                ledger=ledger,
                limits=limits,
                llm=make_router(),
                backends={"fake": backend},
                log=default_logger("test.runner"),
                work_dir=tmp_path / "work",
                lease_owner="test-host:test-cid:1:boot",
                clock=clock,
            )
            yield Harness(
                repo=repo,
                store=store,
                writer=writer,
                read_conn=read_conn,
                ctx=ctx,
                clock=clock,
                sleeps=[],
                backend=backend,
            )
        finally:
            await read_conn.close()


async def _seed(harness: Harness, *repo_ids: str, blast_radius: int = 0) -> None:
    for repo_id in repo_ids:
        await harness.repo.upsert_repo(
            repo_id, name=repo_id, url=f"https://example.invalid/{repo_id}.git", now=NOW
        )
        await harness.repo.upsert_phase(RUN, repo_id, PHASE, now=NOW)
        if blast_radius:
            async def unit(conn: aiosqlite.Connection, rid: str = repo_id) -> None:
                await conn.execute(
                    "UPDATE repos SET blast_radius = ? WHERE repo_id = ?", (blast_radius, rid)
                )

            await harness.writer.submit(unit)


# ======================================================================================
# the wiring itself: RunContext builds the client its WorkerContexts call
# ======================================================================================


async def test_run_context_hands_workers_a_client_they_can_actually_call(
    harness: Harness,
) -> None:
    """A `WorkerContext` from the REAL `RunContext` can complete a typed call end to end.

    Why it matters: this is the defect. `WorkerContext.llm` used to be the `LlmRouter`, which
    resolves a role to a tier and has no `complete()` — so every rung above the deterministic one
    was wired to a collaborator that could not do the one thing the rung existed to do, and two
    workers independently grew workarounds (a constructor-injected client; a structural
    "is this actually a client?" probe) rather than fail. Asserting through a hand-built context
    would re-introduce exactly the gap: the context a test writes by hand is the one place the
    real assembly is not exercised. So this goes through `RunContext.worker_context()`, whose
    client `RunContext` built from the router, the backends and — since `cac537d` wired §11.6 —
    the cache, at every `RunContext` including this harness's, which passes neither `llm_cache`
    nor `llm_cache_mode` and so gets the shipped store at `config.llm.cache_mode`. The single
    backend call asserted below is therefore a cache MISS paid for once;
    `tests/test_run_context_llm_cache.py` is where a HIT is the property under test.
    """
    ctx = harness.ctx.worker_context(
        repo_id="repo-a",
        phase=PHASE,
        attempt=2,
        lease_fence=1,
        cancel=asyncio.Event(),
        budget=CallBudget(
            remaining_tokens=100_000,
            remaining_usd=5.0,
            deadline=asyncio.get_running_loop().time() + 60,
        ),
    )

    response = await ctx.llm.complete(
        Role.TRANSFORM_REPAIR.value,
        [Message(role="user", content="repair this")],
        Verdict,
        budget=ctx.budget,
    )

    assert response.value == Verdict(summary="answered offline"), "validated and typed"
    assert harness.backend.calls == ["fake-1"], "the call reached a backend, not a router"
    assert ctx.router.resolve(Role.TRANSFORM_REPAIR.value).tier is ModelTier.WORKHORSE, (
        "the router is still on the context — as the `limits.for_tier` key, not a call surface"
    )


async def test_the_context_client_refuses_a_call_the_budget_cannot_pay_for(
    harness: Harness,
) -> None:
    """`BudgetExhausted` is raised BEFORE the backend is invoked, through the new call path.

    Why it matters: §11.2 is fail-closed, and a ceiling checked after the call is a ceiling that
    has already been broken. The check lives inside `complete()` because that is the only place
    the target's price is known — so routing the workers through a real client rather than a
    hand-rolled one is what keeps the gate on the path they use.
    """
    ctx = harness.ctx.worker_context(
        repo_id="repo-a",
        phase=PHASE,
        attempt=2,
        lease_fence=1,
        cancel=asyncio.Event(),
        budget=CallBudget(
            remaining_tokens=100_000,
            remaining_usd=0.0,
            deadline=asyncio.get_running_loop().time() + 60,
        ),
    )

    with pytest.raises(BudgetExhausted):
        await ctx.llm.complete(
            Role.TRANSFORM_REPAIR.value,
            [Message(role="user", content="repair this")],
            Verdict,
            budget=ctx.budget,
        )

    assert harness.backend.calls == [], "spend is refused before the transport, never after"


# ======================================================================================
# the headline: per-repo isolation
# ======================================================================================

SIX = ("repo-a", "repo-b", "repo-c", "repo-d", "repo-e", "repo-f")


async def test_one_worker_exception_does_not_cancel_its_siblings(harness: Harness) -> None:
    """§11.1, stated as an executable fact: a non-`BaseException` escaping the worker boundary
    is recorded on the failing repo and **does not cancel a sibling**.

    The stale stub this module replaced documented the opposite. Implementing it that way is not
    a cosmetic difference: one unhandled bug would then discard five siblings' in-flight LLM and
    container work and stop the fleet, once per latent bug class, on a run measured in days.
    """
    await _seed(harness, *SIX)
    await harness.plan(SIX)
    for repo_id in SIX:
        exploded = repo_id == "repo-c"
        BEHAVIOURS[repo_id] = [boom("null deref in the rule engine") if exploded else ok()]

    report = await harness.runner().run_wave(0)

    assert report.halt is None, "a repo-level bug must never be a run-level halt"
    survivors = [r for r in SIX if r != "repo-c"]
    for repo_id in survivors:
        status, attempts, _, _, _ = await harness.phase_row(repo_id)
        assert (status, attempts) == ("SUCCEEDED", 1), f"{repo_id} was cancelled by its sibling"
    assert report.state is WaveState.CLOSED, "the wave must still close"


async def test_the_failing_repo_lands_unknown_with_a_recorded_last_error(
    harness: Harness,
) -> None:
    """Isolation is not silence (Rule 11). The exception is classified `UNKNOWN`, its message is
    persisted as `last_error`, and the repo exhausts its ladder — a swallowed exception that
    left the repo `PENDING` would make the run report success with the repo simply missing."""
    await _seed(harness, "repo-a", "repo-c")
    await harness.plan(("repo-a", "repo-c"))
    BEHAVIOURS["repo-a"] = [ok()]
    BEHAVIOURS["repo-c"] = [boom("null deref in the rule engine")]

    await harness.runner().run_wave(0)

    status, attempts, _, failure_class, last_error = await harness.phase_row("repo-c")
    assert status == "REQUIRES_HUMAN_INTERVENTION"
    assert failure_class == FailureClass.UNKNOWN
    assert last_error is not None and "null deref" in last_error
    assert attempts == 3, "the ladder ran to its ceiling rather than dropping the repo"


async def test_an_exception_from_execute_itself_is_also_contained(harness: Harness) -> None:
    """The boundary is the RUNNER's, not `BaseWorker.execute`'s. A worker whose ladder raises
    before it ever calls `run()` must be contained identically, or the isolation guarantee holds
    only for bugs in the one method that already handles them."""
    await _seed(harness, "repo-a", "repo-c")
    await harness.plan(("repo-a", "repo-c"))
    BEHAVIOURS["repo-a"] = [ok()]
    BEHAVIOURS["repo-c"] = [ok()]

    report = await harness.runner(worker=ExplodingExecuteWorker()).run_wave(0)

    assert report.halt is None
    status, _, _, failure_class, last_error = await harness.phase_row("repo-c")
    assert (status, failure_class) == ("REQUIRES_HUMAN_INTERVENTION", FailureClass.UNKNOWN)
    assert last_error is not None and "exploded" in last_error


# ======================================================================================
# ... but a DECLARED halt state does stop the wave
# ======================================================================================


async def test_budget_exhaustion_halts_the_wave(tmp_path: Path) -> None:
    """The other half of isolation: `RunHalted` is the ONE exception a repo task lets reach the
    `TaskGroup` (§11.1). Without this test, "catch everything at the worker boundary" would be
    indistinguishable from "swallow everything", and a run past its ceiling would keep spending.
    """
    config = FleetConfig(budgets=BudgetsSection(run_max_cost_usd=1.0))
    async for harness in _build(tmp_path, config, max_usd=1.0):
        await _seed(harness, "repo-slow", "repo-boom")
        await harness.plan(("repo-slow", "repo-boom"))
        started, never = asyncio.Event(), asyncio.Event()
        BEHAVIOURS["repo-slow"] = [waits(started, never)]
        BEHAVIOURS["repo-boom"] = [ok()]

        runner = harness.runner(
            estimate=lambda repo_id: CostEstimate(0, 0, 2.0 if repo_id == "repo-boom" else 0.0)
        )
        report = await asyncio.wait_for(runner.run_wave(0), timeout=5)

        assert report.halt is not None, "a run-scoped ledger breach must stop the wave"
        assert report.halt.exit_code == 3
        assert report.exit_code == 3
        slow_status, slow_attempts, _, _, _ = await harness.phase_row("repo-slow")
        assert slow_status != "SUCCEEDED", "the sibling was NOT allowed to finish"
        assert slow_attempts == 0, "a halted run charges nothing to a repo that did nothing wrong"
        assert report.state is not WaveState.CLOSED


async def test_a_repo_scoped_ceiling_does_not_halt_the_fleet(tmp_path: Path) -> None:
    """§11.2: the five ceilings differ in blast radius, and the difference is the exception's
    `exit_code`. A repo ceiling sends ONE repo to a human; the fleet continues."""
    config = FleetConfig()
    async for harness in _build(tmp_path, config, max_usd=1000.0):
        await _seed(harness, "repo-a", "repo-greedy")
        await harness.plan(("repo-a", "repo-greedy"))
        BEHAVIOURS["repo-a"] = [ok()]
        BEHAVIOURS["repo-greedy"] = [ok()]

        report = await harness.runner(
            estimate=lambda repo_id: CostEstimate(0, 0, 99.0 if repo_id == "repo-greedy" else 0.0)
        ).run_wave(0)

        assert report.halt is None
        assert (await harness.phase_row("repo-a"))[0] == "SUCCEEDED"
        status, _, _, failure_class, _ = await harness.phase_row("repo-greedy")
        assert (status, failure_class) == (
            "REQUIRES_HUMAN_INTERVENTION",
            FailureClass.BUDGET_EXHAUSTED,
        )


# ======================================================================================
# lease / fence, end to end
# ======================================================================================


async def test_a_result_under_a_stale_fence_is_discarded_and_the_phase_is_redispatchable(
    harness: Harness,
) -> None:
    """§11.5, the whole reaper guarantee in one run.

    The lease is reaped WHILE the worker runs; the worker then finishes successfully and returns
    a result describing a worktree it no longer owns. That result is discarded — not merged —
    and nothing is written: no attempt charged, no status advanced, no `last_error`. If it were
    merged, two owners would have written one repo and every check would still pass.
    """
    await _seed(harness, "repo-a")
    await harness.plan(("repo-a",))
    started, gate = asyncio.Event(), asyncio.Event()
    BEHAVIOURS["repo-a"] = [waits(started, gate)]

    flight = asyncio.create_task(harness.runner().run_wave(0))
    await asyncio.wait_for(started.wait(), timeout=5)

    reaped = await harness.repo.reap_expired_phase_leases(RUN, now=NOW + timedelta(hours=1))
    assert reaped == 1, "the reaper did not reclaim the lease the runner was holding"
    gate.set()
    report = await asyncio.wait_for(flight, timeout=5)

    assert report.outcomes["repo-a"].discarded is True
    status, attempts, _, failure_class, last_error = await harness.phase_row("repo-a")
    assert (status, attempts) == ("PENDING", 0), "a discarded result was merged anyway"
    assert (failure_class, last_error) == (None, None)

    # ---- and the phase is re-dispatchable by its new owner, exactly once ----
    BEHAVIOURS["repo-a"] = [ok()]
    await harness.runner().run_wave(0)
    status, attempts, _, _, _ = await harness.phase_row("repo-a")
    assert (status, attempts) == ("SUCCEEDED", 1), "the discarded attempt was double-counted"


async def test_the_heartbeat_cancels_a_worker_whose_lease_was_stolen(tmp_path: Path) -> None:
    """A reclaimed lease must reach the worker: it MUST abort without touching git, because
    another process owns that worktree now. The heartbeat is the only thing that can tell it —
    `renew_phase_lease` raising `LeaseStolenError` is the signal, and the fence is the proof."""
    config = FleetConfig(run=RunSection(lease_ttl_s=1))
    async for harness in _build(tmp_path, config, max_usd=1000.0):
        await _seed(harness, "repo-a")
        await harness.plan(("repo-a",))
        started, observed = asyncio.Event(), []
        BEHAVIOURS["repo-a"] = [waits_for_cancel(started, observed)]

        flight = asyncio.create_task(harness.runner().run_wave(0))
        await asyncio.wait_for(started.wait(), timeout=5)
        assert await harness.repo.reap_expired_phase_leases(RUN, now=NOW + timedelta(hours=1)) == 1
        report = await asyncio.wait_for(flight, timeout=5)

        assert observed == [True], "the worker never saw ctx.cancel after the reclaim"
        assert report.outcomes["repo-a"].discarded is True
        assert (await harness.phase_row("repo-a"))[:2] == ("PENDING", 0)


async def _reservation_row(harness: Harness, repo_id: str) -> tuple[str, int | None, int | None]:
    """`(state, phase, lease_fence)` off the durable `reservations` row — WHOSE dollars these
    are, read the way the reaper reads it rather than off the in-process `Reservation`."""
    async with harness.read_conn.execute(
        "SELECT state, phase, lease_fence FROM reservations WHERE run_id = ? AND repo_id = ?",
        (RUN, repo_id),
    ) as cursor:
        rows = await cursor.fetchall()
    assert len(rows) == 1, f"expected exactly one reservation for {repo_id}, got {len(rows)}"
    row = rows[0]
    return str(row[0]), row[1], row[2]


async def _lease_fence(harness: Harness, repo_id: str) -> int:
    async with harness.read_conn.execute(
        "SELECT lease_fence FROM phases WHERE run_id = ? AND repo_id = ? AND phase = ?",
        (RUN, repo_id, int(PHASE)),
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None
    return int(row[0])


async def test_a_runner_minted_reservation_names_its_owner_so_the_reaper_can_fence_it(
    tmp_path: Path,
) -> None:
    """A hold minted by the runner carries the phase lease it was dispatched under (§6, v8).

    Why this and not just the dollars: releasing an expired hold refunds the money, and if that
    is ALL that happens, the dead worker still holds a fence `phases` accepts. It wakes up,
    writes its result, and two owners have written one repo — the exact double-writer condition
    `lease_fence` exists to prevent. `reservations.phase` / `reservations.lease_fence` are what
    let the release and the fence bump be one transaction, and they can only be populated by the
    dispatcher, because it is the only thing that knows which lease the money is being spent
    under. A NULL/NULL hold stays legal (an unowned reserver is still reaped by expiry); what is
    not acceptable is the runner minting one while holding a lease it could have named.

    The reap below runs while the LEASE is still live — `reclaimed == 0` — so the fence bump
    under test can only have come from the expired reservation's owner, never from the lease
    reaper that would have bumped it anyway.
    """
    async for harness in _build(tmp_path, FleetConfig(), max_usd=1000.0, reservation_ttl_s=30.0):
        await _seed(harness, "repo-a")
        await harness.plan(("repo-a",))
        started, gate = asyncio.Event(), asyncio.Event()
        BEHAVIOURS["repo-a"] = [waits(started, gate)]

        runner = harness.runner(estimate=lambda _repo_id: CostEstimate(1_000, 500, 0.25))
        flight = asyncio.create_task(runner.run_wave(0))
        await asyncio.wait_for(started.wait(), timeout=5)

        fence = await _lease_fence(harness, "repo-a")
        assert await _reservation_row(harness, "repo-a") == ("HELD", int(PHASE), fence), (
            "the runner minted an UNOWNED hold: the reaper would have no fence to bump"
        )

        reaped = await harness.repo.reap_expired_phase_leases(RUN, now=NOW + timedelta(seconds=60))
        assert reaped == 0, "the lease expired too, so this no longer isolates the owner's bump"
        assert (await _reservation_row(harness, "repo-a"))[0] == "EXPIRED"
        repo_ledger = await harness.repo.get_repo_budget(RUN, "repo-a")
        assert repo_ledger is not None and repo_ledger.reserved_usd == pytest.approx(0.0)

        # The other half, and the one an unowned hold does not get: the reaped worker is refused.
        assert await _lease_fence(harness, "repo-a") == fence + 1, (
            "the money came back but the dead holder's fence still writes"
        )
        with pytest.raises(LeaseStolenError):
            await harness.repo.complete_phase(
                RUN, "repo-a", PHASE, fence=fence, status=RepoStatus.SUCCEEDED, now=NOW
            )

        gate.set()
        report = await asyncio.wait_for(flight, timeout=5)
        # The late worker succeeds and still merges nothing: its settlement is refused (the row
        # is no longer HELD) and its fence is stale either way. The phase is left RUNNING under
        # the OLD lease because the reservation half of the reaper bumps the fence, it does not
        # reclaim a lease that has not expired — invalidating the writer is the whole job here.
        assert report.outcomes["repo-a"].failure_class is FailureClass.UNKNOWN
        assert (await harness.phase_row("repo-a"))[:2] == ("RUNNING", 0), (
            "the reaped worker's late result landed under a fence the reaper had invalidated"
        )


# ======================================================================================
# partial work
# ======================================================================================


async def test_a_partial_result_is_checkpointed_and_re_entry_does_not_redo_landed_units(
    harness: Harness,
) -> None:
    """§11.5 / §7.1: `partial` exists because a binary verdict lies about interrupted work.

    Attempt 1 lands `u1`/`u2`; the checkpoint records them; attempt 2 is handed ONLY `u3`. A
    driver that replayed all three would produce no-op patches over an already-rewritten tree,
    which the ladder reads as `RULE_MISS` — a failure invented from a success.
    """
    await _seed(harness, "repo-a")
    await harness.plan(("repo-a",))
    BEHAVIOURS["repo-a"] = [partial(["u1", "u2"], ["u3"]), ok()]

    report = await harness.runner().run_wave(0)

    assert [call[3] for call in CALLS] == [("u1", "u2", "u3"), ("u3",)]
    assert report.outcomes["repo-a"].checkpointed is True
    status, attempts, _, _, _ = await harness.phase_row("repo-a")
    assert (status, attempts) == ("SUCCEEDED", 2), "landed work must cost exactly one attempt"

    stored = await checkpoints.load(
        harness.read_conn,
        run_id=RUN_ID,
        repo_id="repo-a",
        phase=PHASE,
        model=PhaseCheckpoint,
    )
    assert stored.payload is not None
    assert stored.payload.completed_units == ["u1", "u2"]
    assert stored.payload.remaining_units == ["u3"]


# ======================================================================================
# §7.1: the re-entry guard the driver must actually consult
#
# `BaseWorker.preconditions_hold` was made ABSTRACT so that no worker could inherit a defaulted
# `return True` — the blind replay that turns a re-run `relocate` into `java/java/com/x`. All
# eleven workers implement it. Nothing called it: not `execute`, not this driver. The mechanism
# was inert for its entire existence and no test noticed, because every test asserted on what
# the workers do internally rather than on whether the driver ever asked. These tests assert the
# asking, and they pin the POLARITY in both directions, because an inverted check is worse than
# no check at all: it would skip every repo in the fleet and report success.
# ======================================================================================


async def _checkpoint(
    harness: Harness, repo_id: str, *, completed: Sequence[str], remaining: Sequence[str]
) -> None:
    """Put a §11.5 checkpoint where a previous dispatch's `partial` would have left it."""
    await checkpoints.save(
        harness.writer,
        run_id=RUN_ID,
        repo_id=repo_id,
        phase=PHASE,
        payload=PhaseCheckpoint(
            completed_units=list(completed), remaining_units=list(remaining), attempt=1
        ),
    )


async def test_the_driver_consults_preconditions_hold_before_re_entering_a_checkpoint(
    harness: Harness,
) -> None:
    """THE regression guard. `preconditions_hold` must be CALLED by the driver.

    Every other property in this section is downstream of this one, and this is the property
    whose absence was silent for the whole life of the harness: the method was abstract,
    implemented eleven times, documented as "the whole of never blind replay" — and invoked by
    nothing outside one pipeline worker in the CLI. A guard nobody calls is a comment.

    It must also be asked about the RESUMED payload, and asked BEFORE the work: the question is
    "may I re-enter for these remaining units", which is unanswerable after the units have run.
    """
    await _seed(harness, "repo-a")
    await harness.plan(("repo-a",))
    await _checkpoint(harness, "repo-a", completed=["u1", "u2"], remaining=["u3"])
    BEHAVIOURS["repo-a"] = [ok()]

    await harness.runner().run_wave(0)

    assert PRECONDITION_CALLS == [("repo-a", ("u3",))], (
        "the driver re-entered a checkpointed phase without asking preconditions_hold"
    )
    assert [call[3] for call in CALLS] == [("u3",)]
    assert (await harness.phase_row("repo-a"))[0] == "SUCCEEDED"


async def test_a_fresh_dispatch_does_not_consult_preconditions(harness: Harness) -> None:
    """The other half of "checked on resume": with no checkpoint there is nothing to replay, so
    there is nothing to guard, and the phase runs whole.

    Pinned because the honest implementations make the fresh case indistinguishable otherwise:
    `interrogate`, `symbolindex`, `rewrite` and the CLI's pipeline workers all return `False`
    when there is nothing to resume. Asking on a fresh dispatch and acting on the answer is
    exactly how `False` gets misread as "already done" — and a driver that skipped there would
    skip the entire fleet.
    """
    await _seed(harness, "repo-a")
    await harness.plan(("repo-a",))
    BEHAVIOURS["repo-a"] = [ok()]

    await harness.runner().run_wave(0)

    assert PRECONDITION_CALLS == []
    assert [call[3] for call in CALLS] == [UNITS], "a fresh phase must run whole"


async def test_completed_work_is_not_re_run_while_owed_work_is(harness: Harness) -> None:
    """THE polarity, both directions, in one wave — because only the pair pins it.

    `repo-done` has a checkpoint that owes nothing and a worker that EXPLODES if it is ever
    re-entered: re-running completed work is precisely the `java/java/com/x` replay §7.1 forbids.
    `repo-owed` has a checkpoint that still owes `u2`/`u3` and must be dispatched for exactly
    those. One assertion cannot tell a correct check from an inverted one; two can, and an
    inverted check fails both — `repo-done` would detonate and `repo-owed` would be silently
    marked SUCCEEDED with two units never done.
    """
    await _seed(harness, "repo-done", "repo-owed")
    await harness.plan(("repo-done", "repo-owed"))
    await _checkpoint(harness, "repo-done", completed=list(UNITS), remaining=[])
    await _checkpoint(harness, "repo-owed", completed=["u1"], remaining=["u2", "u3"])
    BEHAVIOURS["repo-done"] = [boom("completed work was re-run")]
    BEHAVIOURS["repo-owed"] = [ok()]

    report = await harness.runner().run_wave(0)

    assert sorted(PRECONDITION_CALLS) == [
        ("repo-done", ()),
        ("repo-owed", ("u2", "u3")),
    ], "both repos must be asked; the payload each was asked about is what it owes"
    assert CALLS == [("repo-owed", 1, TransformTier.DETERMINISTIC, ("u2", "u3"), None)], (
        "the driver either re-ran completed work or skipped work that was still owed"
    )
    assert report.outcomes["repo-done"].skipped_complete is True
    assert report.outcomes["repo-owed"].skipped_complete is False
    assert (await harness.phase_row("repo-done"))[0] == "SUCCEEDED"
    assert (await harness.phase_row("repo-owed"))[0] == "SUCCEEDED"


async def test_re_entry_after_a_partial_consumes_the_checkpoint_through_the_driver(
    harness: Harness,
) -> None:
    """The `partial` → re-entry round trip, driven entirely by `run_wave` (§11.5).

    Deliberately not by calling the worker directly: the thing under test is that the DRIVER
    loads the checkpoint, asks whether it may be re-entered, and then hands the worker only what
    is still owed. A rewriter that landed 40 of 60 units and is handed all 60 again emits no-op
    patches that the ladder reads as `RULE_MISS` — a failure invented out of a success.
    """
    await _seed(harness, "repo-a")
    await harness.plan(("repo-a",))
    BEHAVIOURS["repo-a"] = [partial(["u1"], ["u2", "u3"]), ok()]

    await harness.runner().run_wave(0)

    assert [call[3] for call in CALLS] == [UNITS, ("u2", "u3")], "u1 was replayed"
    assert PRECONDITION_CALLS == [("repo-a", ("u2", "u3"))], (
        "the re-entry that consumed the checkpoint was never guarded"
    )
    status, attempts, _, _, _ = await harness.phase_row("repo-a")
    assert (status, attempts) == ("SUCCEEDED", 2)


async def test_a_refused_precondition_re_runs_the_phase_whole_and_never_skips_it(
    harness: Harness,
) -> None:
    """`False` means "this checkpoint does not describe the tree in front of me".

    Per the method's own contract that sends the phase back to `phases.base_ref` to run WHOLE —
    it is emphatically not "already done, skip". A skip here is the catastrophic reading: the
    landed-work record is wrong, so the units it claims are the ones least safe to assume, and
    marking the phase done would abandon them with a green status no re-run will ever revisit.
    The rejection is loud (`RepoOutcome.checkpoint_rejected`), not a log line nobody reads.
    """
    await _seed(harness, "repo-a")
    await harness.plan(("repo-a",))
    await _checkpoint(harness, "repo-a", completed=["u1", "u2"], remaining=["u3"])
    PRECONDITIONS["repo-a"] = [False]
    BEHAVIOURS["repo-a"] = [ok()]

    report = await harness.runner().run_wave(0)

    assert PRECONDITION_CALLS == [("repo-a", ("u3",))]
    assert [call[3] for call in CALLS] == [UNITS], (
        "a refused checkpoint must re-run the phase whole, not resume it and not skip it"
    )
    assert report.outcomes["repo-a"].checkpoint_rejected is True
    assert report.outcomes["repo-a"].skipped_complete is False
    assert (await harness.phase_row("repo-a"))[0] == "SUCCEEDED"


class _NotAPhaseCheckpoint(BaseModel):
    """A checkpoint written by some other class, so `load` refuses it on the model name alone.

    Deliberately field-compatible with `PhaseCheckpoint`: if the rejection were keyed on the
    DATA rather than on the recorded class, this payload would validate and the test would be
    measuring nothing.
    """

    completed_units: list[str] = []
    remaining_units: list[str] = []
    attempt: int = 1


def _unreadable_events(logs: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [entry for entry in logs if entry.get("event") == "checkpoint_unreadable"]


async def test_a_checkpoint_at_another_schema_version_is_reported_with_BOTH_versions(
    harness: Harness,
) -> None:
    """A stored checkpoint the loader refuses must NAME what it refused and why (Rule 11).

    The quantity watched is the `checkpoint_unreadable` emit and its `rejection`/`detail` pair,
    not "some log line appeared": the runner already emits a `checkpoint_rejected` for the
    worker's verdict on a checkpoint that loaded fine, so an instrument that counted emits, or
    matched on the substring "checkpoint", would read that unrelated event as this one. The
    defect cannot leave this quantity unchanged, because the pre-fix loader returned
    `loaded.payload` and dropped `rejection` and `detail` on the floor with nothing emitted at
    all — and `detail` is asserted against the two version NUMBERS resolved here rather than
    against `checkpoints.py`'s message text, which nothing would keep in step with a copy.

    This is the path a `SCHEMA_VERSION` bump takes for every repo holding a checkpoint, which is
    what makes the silence expensive: the whole fleet re-runs completed units and says nothing.
    """
    await _seed(harness, "repo-a")
    await harness.plan(("repo-a",))
    await checkpoints.save(
        harness.writer,
        run_id=RUN_ID,
        repo_id="repo-a",
        phase=PHASE,
        payload=PhaseCheckpoint(completed_units=["u1", "u2"], remaining_units=["u3"], attempt=1),
        schema_version=SCHEMA_VERSION - 1,
    )
    BEHAVIOURS["repo-a"] = [ok()]

    with structlog.testing.capture_logs() as logs:
        report = await harness.runner().run_wave(0)

    events = _unreadable_events(logs)
    assert len(events) == 1, "the discarded checkpoint was never reported"
    assert events[0]["rejection"] == "SCHEMA_VERSION_MISMATCH"
    assert events[0]["repo_id"] == "repo-a"
    assert str(SCHEMA_VERSION - 1) in events[0]["detail"], "the stored version is not named"
    assert str(SCHEMA_VERSION) in events[0]["detail"], "the loader version is not named"
    assert report.outcomes["repo-a"].checkpoint_unreadable is True
    assert [call[3] for call in CALLS] == [UNITS], (
        "an unreadable checkpoint must re-run the phase whole from its anchor"
    )


async def test_a_checkpoint_from_another_model_reports_THAT_rejection_not_the_schema_one(
    harness: Harness,
) -> None:
    """The report must identify WHICH refusal happened, not merely that one did.

    `checkpoints.load` has four ways to refuse stored bytes and the schema bump is only one of
    them; a report that names a constant tells an operator to go looking at the schema version
    when the actual cause was a renamed class, a truncated envelope, or data the model refused.
    So this case fixes the rejection kind while holding everything else — the emit, the outcome
    flag, the whole-phase re-run — identical to the case above.
    """
    await _seed(harness, "repo-a")
    await harness.plan(("repo-a",))
    await checkpoints.save(
        harness.writer,
        run_id=RUN_ID,
        repo_id="repo-a",
        phase=PHASE,
        payload=_NotAPhaseCheckpoint(completed_units=["u1"], remaining_units=["u2", "u3"]),
    )
    BEHAVIOURS["repo-a"] = [ok()]

    with structlog.testing.capture_logs() as logs:
        report = await harness.runner().run_wave(0)

    events = _unreadable_events(logs)
    assert len(events) == 1, "the discarded checkpoint was never reported"
    assert events[0]["rejection"] == "MODEL_MISMATCH", (
        "the report names a rejection other than the one that happened"
    )
    assert report.outcomes["repo-a"].checkpoint_unreadable is True


async def test_a_key_with_no_checkpoint_at_all_is_not_reported_as_a_discard(
    harness: Harness,
) -> None:
    """The control, and the half a "did anything get emitted?" assertion cannot express.

    `ABSENT` is the ordinary fresh dispatch: nothing was stored, so nothing was thrown away.
    Reporting it would fire once per repo on the first wave of every run and bury the four
    refusals that DO mean landed work is gone — the loudness would be spent on the one case that
    carries no information. A fix that simply reported every `rejection` is silent under both
    cases above and fails only here.
    """
    await _seed(harness, "repo-a")
    await harness.plan(("repo-a",))
    BEHAVIOURS["repo-a"] = [ok()]

    with structlog.testing.capture_logs() as logs:
        report = await harness.runner().run_wave(0)

    assert _unreadable_events(logs) == [], "a fresh key was reported as a discarded checkpoint"
    assert report.outcomes["repo-a"].checkpoint_unreadable is False


async def test_a_refused_checkpoint_is_not_resurrected_by_the_next_partial(
    harness: Harness,
) -> None:
    """A rejected checkpoint is GONE, and the state store must not launder it back in.

    `_save_checkpoint` unions the prior `completed_units` into the new one, which is right for a
    resume and catastrophic after a rejection: the units the worker just told us it does not
    believe in would come back as "already done" on the very next `partial`, and the next
    re-entry would skip them for good. Here the refused checkpoint claims `u1`/`u2`; the whole
    re-run lands only `u1`; `u2` must be owed again.
    """
    await _seed(harness, "repo-a")
    await harness.plan(("repo-a",))
    await _checkpoint(harness, "repo-a", completed=["u1", "u2"], remaining=["u3"])
    PRECONDITIONS["repo-a"] = [False, True]
    BEHAVIOURS["repo-a"] = [partial(["u1"], ["u2", "u3"]), ok()]

    await harness.runner().run_wave(0)

    assert [call[3] for call in CALLS] == [UNITS, ("u2", "u3")], (
        "u2 was inherited from the checkpoint the worker refused and is now never done"
    )
    stored = await checkpoints.load(
        harness.read_conn, run_id=RUN_ID, repo_id="repo-a", phase=PHASE, model=PhaseCheckpoint
    )
    assert stored.payload is not None
    assert stored.payload.completed_units == ["u1"]


async def test_a_precondition_that_raises_is_a_typed_failure_not_a_verdict(
    harness: Harness,
) -> None:
    """A guard that raises has answered NEITHER way, and guessing either answer is how a replay
    guard becomes a replay (Rule 11).

    Read as `True` it would re-enter a tree nothing vouched for; read as `False` it would throw
    away landed work on every dispatch. It is this repo's typed failure, the worker is never
    dispatched, and the ladder ends where an unfixable failure ends: with a human.
    """
    await _seed(harness, "repo-a")
    await harness.plan(("repo-a",))
    await _checkpoint(harness, "repo-a", completed=["u1", "u2"], remaining=["u3"])
    BEHAVIOURS["repo-a"] = [ok()]

    report = await harness.runner(worker=ExplodingPreconditionWorker()).run_wave(0)

    assert CALLS == [], "run() was dispatched behind a precondition that never answered"
    assert report.outcomes["repo-a"].failure_class is FailureClass.UNKNOWN
    status, _, _, failure_class, last_error = await harness.phase_row("repo-a")
    assert status == "REQUIRES_HUMAN_INTERVENTION"
    assert failure_class == "UNKNOWN"
    assert last_error is not None and "preconditions_hold exploded" in last_error


# ======================================================================================
# the ladder — decided by RetryPolicy, obeyed by the runner
# ======================================================================================


async def test_a_transient_failure_retries_the_same_rung_without_charging_an_attempt(
    harness: Harness,
) -> None:
    """ADR-0014 / §11.8: `TRANSIENT_INFRA` is the infrastructure failing, not the repo.

    The runner must honour `RetryDecision.charges_attempt` rather than counting dispatches: the
    fleet's own flaky network may not spend a repo's three chances. The rung is re-run under the
    SAME lease, so `phases.attempts` is untouched and `transient_retries` records what happened.
    """
    await _seed(harness, "repo-a")
    await harness.plan(("repo-a",))
    BEHAVIOURS["repo-a"] = [fails(FailureClass.TRANSIENT_INFRA), ok()]

    await harness.runner(policy=RecordingPolicy()).run_wave(0)

    assert [d.action for d in DECISIONS] == [RetryAction.RETRY_TRANSIENT]
    assert DECISIONS[0].charges_attempt is False
    status, attempts, transient, _, _ = await harness.phase_row("repo-a")
    assert (status, attempts, transient) == ("SUCCEEDED", 1, 1)
    assert harness.sleeps == [pytest.approx(DECISIONS[0].delay_s)], "backoff was not honoured"
    assert RUNGS == [1, 1], "a transient retry moved the rung"


async def test_a_substantive_failure_charges_an_attempt_and_advances_the_rung(
    harness: Harness,
) -> None:
    """REPLACES the version of this test that asserted the rung ONLY on what the runner asked
    for (`RUNGS`), and documented in its own docstring that the tier a worker observed was
    "currently pinned at `DETERMINISTIC`" because `BaseWorker.execute` re-derived it from an
    in-process counter that is always 0 on a one-rung dispatch.

    That gap was the defect, not a footnote: the driver's ladder position was correct and
    durable, and the work done at each position was identical. It now asserts the thing that
    actually matters — the rung the WORKER RAN — and the two views must agree.

    Two substantive failures each cost a rung, and the third dispatch is rung 3 because that is
    what `RetryPolicy` said, not what the driver guessed.
    """
    await _seed(harness, "repo-a")
    await harness.plan(("repo-a",))
    BEHAVIOURS["repo-a"] = [
        fails(FailureClass.RULE_MISS),
        fails(FailureClass.RULE_MISS),
        ok(),
    ]

    await harness.runner(policy=RecordingPolicy()).run_wave(0)

    assert [d.action for d in DECISIONS] == [
        RetryAction.ADVANCE_LADDER,
        RetryAction.ADVANCE_LADDER,
    ]
    assert all(d.charges_attempt for d in DECISIONS)
    assert RUNGS == [1, 2, 3], "the runner did not advance the rung the policy charged for"
    assert [d.tier for d in DECISIONS] == [
        TransformTier.LLM_REPAIR,
        TransformTier.LLM_ESCALATION,
    ]

    # The half that used to be missing: what the worker was actually asked to do each time.
    assert [call[1] for call in CALLS] == RUNGS, (
        "the worker ran a different rung than the one the driver charged the repo for"
    )
    assert [call[2] for call in CALLS] == [
        TransformTier.DETERMINISTIC,
        TransformTier.LLM_REPAIR,
        TransformTier.LLM_ESCALATION,
    ], "the worker re-ran the deterministic rung while the ladder claimed to escalate"
    assert [call[4] for call in CALLS] == [
        None,
        ContextPolicy.EVIDENCE_ONLY,
        ContextPolicy.EVIDENCE_PLUS_REJECTED_APPROACHES,
    ], "the ADR-0021 context ladder never widened"

    status, attempts, transient, _, _ = await harness.phase_row("repo-a")
    assert (status, attempts, transient) == ("SUCCEEDED", 3, 0)


async def test_a_worker_resumed_at_attempt_three_runs_rung_three(harness: Harness) -> None:
    """THE test that proves escalation happens at all, at the level where it must: a repo whose
    `phases.attempts` already says 2 is dispatched straight onto rung 3 and reaches an LLM.

    Why it matters: a multi-day run is a sequence of resumes. If the rung is re-derived from
    anything other than the persisted number, every resumed dispatch is a fresh deterministic
    attempt — the ladder is a counter that costs money and buys nothing, and the repos that
    needed `LLM_ESCALATION` are abandoned to a human having never once been shown to a model.
    """
    await _seed(harness, "repo-a")
    await harness.plan(("repo-a",))
    await harness.set_attempts("repo-a", 2)  # two rungs already spent and recorded
    BEHAVIOURS["repo-a"] = [ok()]

    await harness.runner().run_wave(0)

    assert len(CALLS) == 1, "a resume must dispatch exactly the rung it left off at"
    _, attempt, tier, _, policy = CALLS[0]
    assert attempt == 3, "the resumed dispatch did not read its position off disk"
    assert tier is TransformTier.LLM_ESCALATION
    assert policy is ContextPolicy.EVIDENCE_PLUS_REJECTED_APPROACHES, (
        "rung 3 rendered no prompt: the escalation ladder never reached an LLM worker"
    )
    status, attempts, _, _, _ = await harness.phase_row("repo-a")
    assert (status, attempts) == ("SUCCEEDED", 3)


async def test_a_repo_dispatched_fresh_runs_the_deterministic_rung(harness: Harness) -> None:
    """The other end of the same property. `EVIDENCE_PLUS_...` on attempt 1 would be just as
    wrong as `DETERMINISTIC` on attempt 3 — and it would spend HEAVY-tier money on every repo in
    the fleet before anything had even failed once."""
    await _seed(harness, "repo-a")
    await harness.plan(("repo-a",))
    BEHAVIOURS["repo-a"] = [ok()]

    await harness.runner().run_wave(0)

    _, attempt, tier, _, policy = CALLS[0]
    assert (attempt, tier, policy) == (1, TransformTier.DETERMINISTIC, None), (
        "attempt 1 must render no prompt at all"
    )


def _one_dispatch_then_crash() -> Callable[[], Any]:
    """A resource guard that admits one dispatch and then trips, which is where `_drive` checks
    it — i.e. exactly between two rungs, with no lease held and the last rung already committed.
    A faithful stand-in for the `SIGKILL` the durable ladder exists to survive."""
    dispatched = [0]

    def guard() -> Any:
        dispatched[0] += 1
        return None if dispatched[0] == 1 else HaltReason.HOST_MEMORY

    return guard


async def test_the_ladder_escalates_across_crash_and_resume_and_never_repeats_a_rung(
    harness: Harness,
) -> None:
    """Attempt 1 → 2 → 3, each in a SEPARATE wave driven by a SEPARATE `PhaseRunner`, with the
    process notionally killed between rungs. Nothing is carried in memory: each runner reads
    `phases.attempts` off disk and must continue, not restart.

    Why it matters: "attempts must escalate, not repeat" is the whole of ADR-0014. A resume that
    re-runs a spent rung is an identical retry — the reference material's infinite-loop failure
    mode — that also charges the repo for the privilege.
    """
    await _seed(harness, "repo-a")
    await harness.plan(("repo-a",))
    BEHAVIOURS["repo-a"] = [fails(FailureClass.RULE_MISS)]

    observed_per_run: list[int] = []
    for _ in range(3):
        before = len(CALLS)
        await harness.runner(resource_guard=_one_dispatch_then_crash()).run_wave(0)
        observed_per_run.append(len(CALLS) - before)

    assert observed_per_run == [1, 1, 1], "a 'crash' let more than one rung run per process"
    rungs = [call[1] for call in CALLS]
    assert rungs == [1, 2, 3], f"the ladder did not survive the resume boundary: {rungs}"
    assert sorted(set(rungs)) == rungs, "a rung ran twice across the resumes"
    tiers = [call[2] for call in CALLS]
    assert tiers == [
        TransformTier.DETERMINISTIC,
        TransformTier.LLM_REPAIR,
        TransformTier.LLM_ESCALATION,
    ], "the tiers did not escalate strictly across the resumes"
    assert len(set(tiers)) == 3, "two dispatches ran at the same tier"

    status, attempts, _, _, _ = await harness.phase_row("repo-a")
    assert (status, attempts) == ("REQUIRES_HUMAN_INTERVENTION", 3), (
        "three rungs ran, so three attempts must be on the row a human now reads"
    )


async def test_the_transient_budget_is_honoured_once_and_not_squared(
    harness: Harness,
) -> None:
    """ONE retry authority. The §11.8 transient allowance used to be implemented in BOTH
    `BaseWorker.execute` and `RetryPolicy`, and these tests could only assert on the driver by
    setting the worker's `max_transient_retries` to 0 — which is the tell: a budget you have to
    switch off to measure the other one is not a budget, it is a multiplier.

    With a policy allowance of 2, a permanently-transient endpoint must be called 1 + 2 = 3 times
    at rung 1 and then ONCE per remaining rung, because the transient budget is spent for the
    whole `(repo, phase)` and does not reset per rung. Five calls, not the thirty that two nested
    budgets produced.
    """
    await _seed(harness, "repo-a")
    await harness.plan(("repo-a",))
    BEHAVIOURS["repo-a"] = [fails(FailureClass.TRANSIENT_INFRA)]

    await harness.runner(policy=RecordingPolicy(max_transient_retries=2)).run_wave(0)

    calls_per_rung = Counter(call[1] for call in CALLS)
    assert calls_per_rung == {1: 3, 2: 1, 3: 1}, (
        f"the transient budget was applied more than once per failure: {calls_per_rung}"
    )
    assert len(CALLS) == 5, "two nested transient budgets multiply instead of agreeing"
    assert [d.action for d in DECISIONS] == [
        RetryAction.RETRY_TRANSIENT,
        RetryAction.RETRY_TRANSIENT,
        RetryAction.ADVANCE_LADDER,
        RetryAction.ADVANCE_LADDER,
        RetryAction.TERMINATE,
    ]
    assert len(harness.sleeps) == 2, "one backoff per transient retry, from one layer"

    status, attempts, transient, _, _ = await harness.phase_row("repo-a")
    assert (status, attempts, transient) == ("REQUIRES_HUMAN_INTERVENTION", 3, 2)


async def test_a_non_retryable_failure_terminates_without_another_rung(
    harness: Harness,
) -> None:
    """`WorkerError.retryable` — mechanical evidence, never prose — overrules the class default:
    a structural failure re-run at a higher tier is the same failure at a higher price."""
    await _seed(harness, "repo-a")
    await harness.plan(("repo-a",))
    BEHAVIOURS["repo-a"] = [fails(FailureClass.DEP_CONFLICT, retryable=False)]

    await harness.runner(policy=RecordingPolicy()).run_wave(0)

    assert [d.action for d in DECISIONS] == [RetryAction.TERMINATE]
    status, _, _, failure_class, _ = await harness.phase_row("repo-a")
    assert (status, failure_class) == (
        "REQUIRES_HUMAN_INTERVENTION",
        FailureClass.DEP_CONFLICT,
    )
    assert len(CALLS) == 1, "a non-retryable failure bought another rung"


async def test_a_non_retryable_failure_does_not_consume_a_rung_a_resume_would_skip(
    harness: Harness,
) -> None:
    """`RetryDecision.charges_attempt` is `False` for a non-retryable TERMINATE — no further rung
    is issued, so none is charged — but `complete_phase` increments `attempts` unconditionally,
    so the row recorded one anyway.

    Why that is a real cost and not a cosmetic miscount: `phases.attempts` is the ladder
    position a `fleet resume` reads. A repo that failed structurally at rung 2 and was fixed by
    hand comes back at rung 3 — the `EVIDENCE_ONLY` repair rung it never actually ran is skipped
    silently, and the repo is escalated to HEAVY-tier prompting for a problem that the cheaper
    rung had never been given a chance at. Multiply by a fleet.

    The exhausting failure is the other side of this and must still charge: it terminates too,
    and it did spend its rung.
    """
    await _seed(harness, "repo-a", "repo-b")
    await harness.plan(("repo-a", "repo-b"))
    await harness.set_attempts("repo-a", 1)  # one rung spent; a resume belongs at rung 2
    await harness.set_attempts("repo-b", 2)  # the LAST rung is about to run
    BEHAVIOURS["repo-a"] = [fails(FailureClass.DEP_CONFLICT, retryable=False)]
    BEHAVIOURS["repo-b"] = [fails(FailureClass.RULE_MISS)]

    await harness.runner(policy=RecordingPolicy()).run_wave(0)

    ran_at = {call[0]: call[1] for call in CALLS}
    assert ran_at["repo-a"] == 2, "the structural failure did not run at the resumed rung"

    status, attempts, _, _, _ = await harness.phase_row("repo-a")
    assert (status, attempts) == ("REQUIRES_HUMAN_INTERVENTION", 1), (
        "a rung nobody ran was charged; a resume would now skip rung 2 entirely"
    )

    status_b, attempts_b, _, _, _ = await harness.phase_row("repo-b")
    assert (status_b, attempts_b) == ("REQUIRES_HUMAN_INTERVENTION", 3), (
        "the EXHAUSTING failure terminates too, but it spent its rung and must be charged"
    )


# ======================================================================================
# D90 (§12.20, SECURITY-RELEVANT) — `_terminate_uncharged` and `_record_diagnostics` each
# issue their own raw `UPDATE phases ... last_error = ?` and bypass `complete_phase` entirely,
# so D88's fix there (redact `last_error` at the write boundary) never covers either of them.
# ======================================================================================

_D90_PAT = "github_pat_11ABCDEFG0abcdefghijklmnopqrstuvwxyz0123456789ABCDEF"
_D90_TAINTED = f"clone failed for remote https://oauth2:{_D90_PAT}@gitea.local:3001/x.git"


async def test_terminate_uncharged_redacts_a_credential_in_last_error_before_the_write(
    harness: Harness,
) -> None:
    """D90: a non-retryable TERMINATE reaches `_terminate_uncharged`, not `complete_phase` — the
    same `error_from_exception`-sourced, unredacted `stderr_tail` D88 traced for
    `phases.last_error` reaches this column too, and this write is the LAST one the row gets: it
    lands at REQUIRES_HUMAN_INTERVENTION and a human reads it from there.
    """
    await _seed(harness, "repo-a")
    await harness.plan(("repo-a",))
    BEHAVIOURS["repo-a"] = [
        fails_with(FailureClass.DEP_CONFLICT, _D90_TAINTED, retryable=False)
    ]

    await harness.runner().run_wave(0)

    status, _, _, _, last_error = await harness.phase_row("repo-a")
    assert status == "REQUIRES_HUMAN_INTERVENTION"
    assert last_error is not None
    assert _D90_PAT not in last_error, f"a live PAT reached phases.last_error: {last_error!r}"
    assert "github_pat_" not in last_error
    assert "«redacted:" in last_error, "the placeholder must survive, or debugging is blind"
    assert "gitea.local" in last_error, "over-redaction destroys the debuggable part too"


async def test_terminate_uncharged_leaves_an_innocuous_last_error_unchanged(
    harness: Harness,
) -> None:
    """The control for the test above: this fix is not free to over-redact its way to green."""
    await _seed(harness, "repo-a")
    await harness.plan(("repo-a",))
    BEHAVIOURS["repo-a"] = [
        fails_with(FailureClass.DEP_CONFLICT, "bazel test //...: 3 failures", retryable=False)
    ]

    await harness.runner().run_wave(0)

    status, _, _, _, last_error = await harness.phase_row("repo-a")
    assert status == "REQUIRES_HUMAN_INTERVENTION"
    assert last_error == "bazel test //...: 3 failures"


async def test_record_diagnostics_redacts_a_credential_in_last_error_before_the_write(
    harness: Harness,
) -> None:
    """D90: `_record_diagnostics` runs on EVERY failure — retryable or not — before
    `RetryPolicy`'s decision is acted on, and its own raw `UPDATE` bypasses `complete_phase` too.

    A later `complete_phase`/`_terminate_uncharged` write would overwrite whatever
    `_record_diagnostics` left behind, masking an unredacted write with a later, fixed one — so
    this test must read the row BEFORE any such write happens. `_one_dispatch_then_crash` trips
    the resource guard right after the first failure's diagnostics write and before the retry's
    second dispatch, so the row read here is exactly what `_record_diagnostics` wrote.
    """
    await _seed(harness, "repo-a")
    await harness.plan(("repo-a",))
    tainted = f"transient dial to https://oauth2:{_D90_PAT}@gitea.local:3001/x.git failed"
    BEHAVIOURS["repo-a"] = [fails_with(FailureClass.TRANSIENT_INFRA, tainted, retryable=True)]

    await harness.runner(resource_guard=_one_dispatch_then_crash()).run_wave(0)

    status, _, transient, failure_class, last_error = await harness.phase_row("repo-a")
    assert status == "RUNNING", "the lease is still held; complete_phase never ran"
    assert transient == 1, "the diagnostics write this test reads did happen"
    assert failure_class == "TRANSIENT_INFRA"
    assert last_error is not None
    assert _D90_PAT not in last_error, f"a live PAT reached phases.last_error: {last_error!r}"
    assert "github_pat_" not in last_error
    assert "«redacted:" in last_error, "the placeholder must survive, or debugging is blind"
    assert "gitea.local" in last_error, "over-redaction destroys the debuggable part too"


async def test_record_diagnostics_leaves_an_innocuous_last_error_unchanged(
    harness: Harness,
) -> None:
    """The control for the test above."""
    await _seed(harness, "repo-a")
    await harness.plan(("repo-a",))
    BEHAVIOURS["repo-a"] = [
        fails_with(
            FailureClass.TRANSIENT_INFRA, "dial tcp: connection refused", retryable=True
        )
    ]

    await harness.runner(resource_guard=_one_dispatch_then_crash()).run_wave(0)

    status, _, transient, _, last_error = await harness.phase_row("repo-a")
    assert status == "RUNNING"
    assert transient == 1
    assert last_error == "dial tcp: connection refused"


async def test_a_tier_outage_halts_the_run_and_leaves_the_repo_untouched(
    harness: Harness,
) -> None:
    """§11.8: `BACKEND_UNAVAILABLE` is terminal for the RUN and never for the repo. The repos are
    fine and the infrastructure is not, so they stay `PENDING` for `fleet resume` with no attempt
    charged — a two-hour outage must not burn 250 ladders."""
    await _seed(harness, "repo-a")
    await harness.plan(("repo-a",))
    BEHAVIOURS["repo-a"] = [fails(FailureClass.BACKEND_UNAVAILABLE)]

    report = await harness.runner().run_wave(0)

    assert report.halt is not None
    assert report.halt.exit_code == 8
    _, attempts, _, _, _ = await harness.phase_row("repo-a")
    assert attempts == 0, "an outage consumed one of the repo's three chances"


async def test_a_tier_outage_writes_a_backend_unavailable_finding_before_it_halts(
    harness: Harness,
) -> None:
    """§13 row 40. An exit-8 halt whose cause exists only in a log line makes the operator
    reconstruct which tier died and what was tried from a stderr tail — and `RunHalted` unwinds
    the TaskGroup, so "we will record it afterwards" has no afterwards. The finding is therefore
    written BEFORE the raise, and this test reads it back off disk after the halt.

    `targets_tried` carries the worker's own `stderr_tail` verbatim, which on the real path is
    `TierUnavailable`'s message (client.py:151-155) — the tier and, in order, every target the
    ladder spent. Verbatim rather than parsed: nothing in this codebase branches on message text.
    """
    await _seed(harness, "repo-a")
    await harness.plan(("repo-a",))
    BEHAVIOURS["repo-a"] = [
        fails_with(
            FailureClass.BACKEND_UNAVAILABLE,
            "tier WORKHORSE exhausted after targets: fake:fake-1, fake:fake-2",
        )
    ]

    report = await harness.runner().run_wave(0)

    assert report.halt is not None and report.halt.exit_code == 8
    rows = await harness.findings(BACKEND_UNAVAILABLE)
    assert len(rows) == 1, "the halt was recorded nowhere an operator can query"
    repo_id, severity, payload = rows[0]
    assert repo_id == "repo-a"
    assert severity == "error", "the reason the run stopped must not rank beside a warning"
    assert payload["phase"] == PHASE.name
    observed = str(payload["observed"])
    assert "WORKHORSE" in observed
    assert "fake:fake-1" in observed and "fake:fake-2" in observed, (
        "EVERY target, not just the last: an operator deciding whether to fail a whole profile "
        "over needs to know the fallback was tried too"
    )
    # §13 row 43. The halt string this runner raises says the targets are "DOWN"; the finding it
    # writes must not, because a sustained 429 reaches this same line (`client.py:532` retires a
    # target without reading `TransportError.trigger`) and `BackendHealth.DOWN` is computed
    # nowhere in `src/`. The log line scrolls away — the finding is what a human reads later.
    assert payload["asserts_outage"] is False
    assert payload["failover_triggers_recorded"] == "unknown", (
        "this is the SHIPPED arm: `WorkerError` carries no tier, so the row cannot say whether "
        "the triggers it holds (here, none) belong to the tier that died. See "
        "`test_the_finding_reports_the_partial_trigger_set_it_actually_holds` for the narrowed "
        "arm, which is reachable only when a caller can supply `tier=`"
    )
    assert "down" not in json.dumps(
        {k: v for k, v in payload.items() if k != "caveat"}
    ).lower(), "`decision.reason`'s DOWN claim must not be copied into the row"


async def test_a_drift_during_a_dispatch_is_flushed_by_the_runner(harness: Harness) -> None:
    """The end-to-end proof that `CapabilityDrift` survives the process, with NO explicit flush.

    Everything here is the shipped path: `RunContext` assembles the client and its sink, the
    worker calls `ctx.llm.complete`, the endpoint answers at PROMPTED while its config promised
    JSON_SCHEMA, and the runner drains the sink after the dispatch. Before this lane the same
    scenario produced a green wave and an empty `findings` table.

    The wave SUCCEEDS, deliberately. `_emit_drift` fires per target regardless of call outcome
    (§13 row 37), so a flush that only ran on the failure paths would lose exactly the case the
    row exists for: an endpoint that answers happily at a rung below the one it advertised.
    """
    await _seed(harness, "repo-a")
    await harness.plan(("repo-a",))
    harness.backend.caps = ModelCapabilities(
        supports_json_schema=False,
        supports_tools=False,
        supports_constrained_decoding=False,
        max_output_tokens=8192,
        structured_output_modes=(
            StructuredOutputMode.JSON_SCHEMA,
            StructuredOutputMode.PROMPTED,
        ),
    )
    BEHAVIOURS["repo-a"] = [asks_the_model()]

    report = await harness.runner().run_wave(0)

    assert report.halt is None
    status, _, _, _, _ = await harness.phase_row("repo-a")
    assert status == "SUCCEEDED", "drift is a finding, never a failure"

    rows = await harness.findings(CAPABILITY_DRIFT)
    assert len(rows) == 1, (
        "the client computed the drift and nobody wrote it — the exact defect this lane closes"
    )
    repo_id, severity, payload = rows[0]
    assert repo_id is None, "a drift belongs to a TARGET, not to whichever repo was in flight"
    assert severity == "warn"
    assert payload["promised"] == str(StructuredOutputMode.JSON_SCHEMA)
    assert payload["actual"] == str(StructuredOutputMode.PROMPTED)
    assert payload["model_id"] == "fake-1"


async def test_the_shipped_halt_path_refuses_both_derived_claims_when_triggers_exist(
    harness: Harness,
) -> None:
    """The arm that ACTUALLY SHIPS, with a contaminated map — the case N9 said was untested.

    Nothing in `src/` passes `tier=`: `PhaseRunner`'s halt path is the only production caller and
    `WorkerError` (`workers/base.py:359-374`) carries no tier, so every row this harness writes
    today is `scope: "run"`. That arm must refuse BOTH derived fields, and the refusal only means
    anything when the map is non-empty — an empty map would make `"unknown"` indistinguishable
    from `"none"` and hide a regression that answered from run-wide data.

    So a CHEAP-tier 429 is planted before the wave, exactly as one would arrive hours earlier in a
    real run, and then a HEAVY-ish outage halts it. The row must hand over the raw map keyed by
    tier and decline to summarise it.
    """
    await _seed(harness, "repo-a")
    await harness.plan(("repo-a",))
    harness.ctx.llm_findings.on_failover(
        BackendFailover(
            role="repo_classify",
            tier=ModelTier.CHEAP,
            from_backend="fake",
            from_model_id="cheap-1",
            to_backend="fake",
            to_model_id="cheap-2",
            trigger="RATE_LIMIT",
        )
    )
    BEHAVIOURS["repo-a"] = [
        fails_with(
            FailureClass.BACKEND_UNAVAILABLE,
            "tier HEAVY exhausted after targets: fake:heavy-1, fake:heavy-2",
        )
    ]

    report = await harness.runner().run_wave(0)
    assert report.halt is not None and report.halt.exit_code == 8

    _, _, payload = (await harness.findings(BACKEND_UNAVAILABLE))[0]
    assert payload["failover_triggers_scope"] == "run"
    assert payload["failover_triggers"] == {"CHEAP": {"fake:cheap-1": "RATE_LIMIT"}}, (
        "the raw map is still handed over, keyed by tier so the operator can match it against "
        "`observed` — which names HEAVY, not CHEAP"
    )
    assert payload["failover_triggers_recorded"] == "unknown", (
        "'none' would claim we hold no trigger for the exhausted tier; 'partial' would claim we "
        "do. This row cannot identify the tier, so it must claim neither"
    )
    assert payload["throttling_observed"] is None, (
        "the only 429 in this run belongs to CHEAP and the outage names HEAVY — answering `true` "
        "here is the cross-tier contamination, and `false` is its mirror image"
    )


async def test_a_failing_findings_sink_cannot_rewrite_a_successful_repos_verdict(
    harness: Harness,
) -> None:
    """F2. The drain sits between `_dispatch` and the outcome handling, so an unguarded exception
    escapes `_drive`, is caught by `_isolated`'s `except Exception`, and records a repo that just
    SUCCEEDED as `FailureClass.UNKNOWN` / PENDING with its execution never processed — no phase
    advance, no attempt row, the work redone on resume.

    An observability path that can take down the thing it observes is strictly worse than the
    silence it replaced. The repo's verdict is the assertion; `pending` afterwards is the second
    half, because "isolated" must mean *deferred*, not *dropped* — swallowing the failure AND the
    records would trade one instance of this lane's bug class for another.
    """
    await _seed(harness, "repo-a")
    await harness.plan(("repo-a",))
    # A real drift, produced by the real client through the real `RunContext` wiring: the endpoint
    # promises JSON_SCHEMA and can honour none of it. Without this the sink would have nothing to
    # buffer and `pending` below would prove nothing.
    harness.backend.caps = ModelCapabilities(
        supports_json_schema=False,
        supports_tools=False,
        supports_constrained_decoding=False,
        max_output_tokens=8192,
        structured_output_modes=(
            StructuredOutputMode.JSON_SCHEMA,
            StructuredOutputMode.PROMPTED,
        ),
    )
    BEHAVIOURS["repo-a"] = [asks_the_model()]
    await harness.break_findings_writes()

    report = await harness.runner().run_wave(0)

    assert report.halt is None, "a telemetry write must not halt the run either"
    status, attempts, _, failure_class, _ = await harness.phase_row("repo-a")
    assert (status, attempts) == ("SUCCEEDED", 1), (
        "the repo did its work; a diagnostics writer failing is not its failure"
    )
    assert failure_class is None
    # 1 drift + 1 `llm_call` (§12.18) — the one `backend.invoke()` this repo made returned, so it
    # is buffered alongside the drift and both are still DEFERRED, not dropped, by the failure.
    assert harness.ctx.llm_findings.pending == 2, (
        "isolated must mean DEFERRED, not dropped — the drift the client really computed is "
        "still held for the next drain. Swallowing the failure AND the record would trade one "
        "instance of this lane's bug class for another"
    )


async def test_a_failing_findings_sink_cannot_swallow_the_exit_8_halt(harness: Harness) -> None:
    """F5, the same defect on the halt path. The wave-final drain runs AFTER `halt` is captured
    from the TaskGroup, so an exception escaping it discards a real `RunHalted` and `cli.py`'s
    funnel maps the resulting `StateDbError` to `UNEXPECTED_ERROR` — a documented, operator-facing
    exit 8 silently becoming an unexpected error because telemetry failed."""
    await _seed(harness, "repo-a")
    await harness.plan(("repo-a",))
    BEHAVIOURS["repo-a"] = [fails(FailureClass.BACKEND_UNAVAILABLE)]
    await harness.break_findings_writes()

    report = await harness.runner().run_wave(0)

    assert report.halt is not None, "the tier outage was swallowed by its own diagnostics"
    assert report.halt.exit_code == 8
    assert report.halt.reason is HaltReason.TIER_UNAVAILABLE


async def test_a_full_volume_halts_the_run_with_exit_9_and_leaves_the_repo_untouched(
    harness: Harness,
) -> None:
    """§11.3 / §13 row 42: `DISK_EXHAUSTED` is terminal for the RUN, exit **9**, never for a repo.

    Why it cannot be a repo-scoped failure — the shape the enum's `NON_RETRYABLE` membership
    would otherwise give it: the volume is not repo-a's fault, and the next repo would meet the
    identical floor. Letting the fleet continue would grind 249 further repos into
    `REQUIRES_HUMAN_INTERVENTION` for one full disk, and would keep writing until the failing
    write is the `BEGIN IMMEDIATE` the "every non-zero exit leaves a valid checkpoint" guarantee
    (§10) depends on. Halting here is what leaves room to write the checkpoint.

    The repo therefore stays where the tier-outage test leaves its own: no attempt charged, ready
    for `fleet resume` once the operator has reclaimed space.
    """
    await _seed(harness, "repo-a")
    await harness.plan(("repo-a",))
    BEHAVIOURS["repo-a"] = [fails(FailureClass.DISK_EXHAUSTED)]

    report = await harness.runner().run_wave(0)

    assert report.halt is not None
    assert report.halt.exit_code == 9, "§10 gives ENOSPC its own code precisely so it is legible"
    assert report.halt.reason is HaltReason.DISK
    _, attempts, _, _, _ = await harness.phase_row("repo-a")
    assert attempts == 0, "a full disk consumed one of the repo's three chances"


# ======================================================================================
# wave gating, seen from the runner
# ======================================================================================


async def test_the_runner_refuses_to_open_a_wave_whose_predecessor_is_open(
    harness: Harness,
) -> None:
    """The gate lives in the scheduler, but the runner must not route around it: a driver that
    caught `WaveNotReadyError` and fanned out anyway would migrate a dependent against a
    dependency that never landed."""
    await _seed(harness, "repo-a", "repo-late")
    await harness.plan(("repo-a",), ("repo-late",))
    BEHAVIOURS["repo-a"] = [fails(FailureClass.RULE_MISS)]
    BEHAVIOURS["repo-late"] = [ok()]
    runner = harness.runner()

    with pytest.raises(WaveNotReadyError, match="wave 1 cannot open"):
        await runner.run_wave(1)
    assert CALLS == [], "wave 1 dispatched work while wave 0 was still open"

    # repo-a exhausts its ladder: REQUIRES_HUMAN_INTERVENTION is terminal, so wave 0 CLOSES —
    # a wave closes on a verdict, not on success, or one abandoned repo stalls the fleet.
    await runner.run_wave(0)
    assert (await harness.phase_row("repo-a"))[0] == "REQUIRES_HUMAN_INTERVENTION"

    report = await runner.run_wave(1)
    assert report.state is WaveState.CLOSED
    assert (await harness.phase_row("repo-late"))[0] == "SUCCEEDED"


async def test_a_blocked_member_is_never_dispatched(harness: Harness) -> None:
    """A `BLOCKED` repo is waiting on an abandoned ancestor: dispatching it spends attempts and
    real tokens on work whose precondition cannot hold."""
    await _seed(harness, "repo-a", "repo-blocked")
    await harness.plan(("repo-a", "repo-blocked"))
    await harness.store.append_blocked_by(RUN, "repo-blocked", "repo-a", now=NOW)
    BEHAVIOURS["repo-a"] = [ok()]
    BEHAVIOURS["repo-blocked"] = [boom("must never run")]

    report = await harness.runner().run_wave(0)

    assert report.admission.blocked == ("repo-blocked",)
    assert [call[0] for call in CALLS] == ["repo-a"]
    assert (await harness.phase_row("repo-blocked"))[:2] == ("BLOCKED", 0)


async def test_a_wall_clock_breach_mid_wave_withholds_the_rest_as_pending(
    harness: Harness,
) -> None:
    """§3.4: on breach the runner stops ADMITTING; it does not cancel what is in flight, and the
    members never admitted keep their attempts and their place. Exit 4 is a resumable state."""
    await _seed(harness, *SIX)
    await harness.plan(SIX)
    for repo_id in SIX:
        BEHAVIOURS[repo_id] = [ok()]
    scheduler = harness.scheduler(wave_max_wallclock_s=60)
    await scheduler.open_wave(0)
    harness.clock.advance(61)

    report = await harness.runner(scheduler=scheduler).run_wave(0)

    assert report.admission.breached is True
    assert sorted(report.withheld) == sorted(SIX)
    assert CALLS == []
    assert report.state is WaveState.PARTIAL
    assert report.exit_code == 4
    for repo_id in SIX:
        assert (await harness.phase_row(repo_id))[:2] == ("PENDING", 0)


async def test_a_wall_clock_breach_says_what_happened_rather_than_the_string_none(
    harness: Harness,
) -> None:
    """D83: exit 4 was always right and the operator-facing message was the literal `'None'`.

    `WaveReport.halt` is populated only from a `RunHalted` escaping the wave's `TaskGroup`, and
    a wall-clock breach raises nothing — `admit` returns `admitted=()` and `may_admit` withholds
    the rest — so `halt` stayed `None` under a non-zero exit and every caller that folds
    `str(report.halt)` into its payload handed the operator `'None'`.

    The assertions PARSE the ceiling, the elapsed, the wave index and the withheld count back
    OUT of the message and check each against the value the code actually used — the
    scheduler's own `budgets.wave_max_wallclock_s`, the admission's own `elapsed_s`, the
    report's own `wave_index` and `withheld`. Rewording the message therefore does not weaken
    what is asserted, while hard-coding any of those four into the text fails: the fixture's
    ceiling is 60, not the 14 400 default, and the elapsed and the member list are the
    fixture's.
    """
    await _seed(harness, *SIX)
    await harness.plan(SIX)
    for repo_id in SIX:
        BEHAVIOURS[repo_id] = [ok()]
    scheduler = harness.scheduler(wave_max_wallclock_s=60)
    await scheduler.open_wave(0)
    harness.clock.advance(61)

    report = await harness.runner(scheduler=scheduler).run_wave(0)

    assert report.exit_code == 4, "the exit code was never the defect and must not move"
    assert report.halt is not None, (
        "a non-zero exit with no halt is exactly what makes `str(report.halt)` read `'None'`"
    )
    assert report.halt.reason is HaltReason.WAVE_WALLCLOCK
    assert report.halt.exit_code == 4, (
        "the two branches of `WaveReport.exit_code` must not disagree about a breach"
    )

    message = str(report.halt)
    assert message != "None" and "None" not in message

    ceiling = re.search(r"(\d+)s wall-clock ceiling", message)
    assert ceiling is not None and int(ceiling.group(1)) == 60, (
        "the message must name the ceiling the run was actually given, not the default"
    )
    elapsed = re.search(r"after ([\d.]+)s", message)
    assert elapsed is not None
    assert float(elapsed.group(1)) == pytest.approx(await scheduler.elapsed_s(0), abs=0.05), (
        "the elapsed must be the wave's elapsed at the moment it stopped admitting"
    )
    assert float(elapsed.group(1)) >= 60, (
        "a message that says a 60s ceiling was SPENT after less than 60s contradicts itself"
    )
    wave = re.search(r"wave (\d+)", message)
    assert wave is not None and int(wave.group(1)) == report.wave_index
    count = re.search(r"(\d+) member\(s\) withheld", message)
    assert count is not None and int(count.group(1)) == len(report.withheld) == len(SIX)
    for repo_id in report.withheld:
        assert repo_id in message, (
            "the operator cannot get the withheld members from anywhere else on this path"
        )


class BreachesOnceTheWaveHasStarted(WaveScheduler):
    """The production scheduler with exactly ONE seam: the wave's clock jumps past the ceiling
    right after the runner's FIRST successful `may_admit` poll.

    That seam, and not a trip-point count on clock reads, because a count is a magic number
    that silently stops selecting the mid-wave path the moment anything reads the clock one
    more or one fewer time (CLAUDE.md Rule 11). Overriding `may_admit` names the event
    directly. `elapsed_s`, `breached`, `admit` and the withholding are all untouched
    production code, which is what makes the elapsed the message prints a real measurement.
    """

    async def may_admit(self, wave_index: int) -> bool:
        allowed = await super().may_admit(wave_index)
        if allowed and not MID_WAVE_TRIPS:
            MID_WAVE_TRIPS.append(wave_index)
            cast(SteppableClock, self.clock).advance(61)
        return allowed


#: One entry per trip, so the test can assert the seam fired at all rather than passing
#: vacuously on a scheduler that never breached.
MID_WAVE_TRIPS: list[int] = []


async def test_a_mid_wave_wall_clock_breach_names_the_elapsed_the_withholding_saw(
    harness: Harness,
) -> None:
    """D83, the half its own fix could not see. `run_wave`'s docstring says `may_admit` is
    re-polled between admissions "so a wall-clock breach stops admitting *inside* the wave" —
    and on that path `admit` did NOT breach, so `admission.elapsed_s`, which `admit` snapshots
    before the wave runs, is BY CONSTRUCTION below the ceiling. Building the message from it
    printed "spent its 60s wall-clock ceiling after 0.0s".

    Why the pre-existing D83 test cannot fail under that defect, stated so it is not re-added:
    it drives `open_wave(0)` then `clock.advance(61)`, so `admit` itself breaches
    (`admission.breached is True`, nothing dispatched) and `admission.elapsed_s` is already 61;
    and its elapsed assertion compared the parsed number against **that same snapshot**, i.e.
    against the value the message was built from. It asserted internal consistency, never
    correspondence, so no change to the SOURCE of the elapsed could redden it.

    `assert report.admission.breached is False` below is the proof this is the mid-wave path
    and not the pre-wave one, and it is what makes the elapsed assertion discriminating: the
    two quantities are EQUAL on the pre-wave path and differ by the whole ceiling here. That is
    the two-anchor rule from CLAUDE.md Rule 12 — a fixture whose two anchors coincide cannot
    express the defect at all, and the pre-existing test is exactly that fixture.
    """
    MID_WAVE_TRIPS.clear()
    await _seed(harness, *SIX)
    await harness.plan(SIX)
    for repo_id in SIX:
        BEHAVIOURS[repo_id] = [ok()]
    scheduler = BreachesOnceTheWaveHasStarted(
        run_id=RUN,
        phase=PHASE,
        store=harness.store,
        db=harness.repo,
        budgets=BudgetsSection(wave_max_wallclock_s=60),
        clock=harness.clock,
    )
    await scheduler.open_wave(0)

    report = await harness.runner(scheduler=scheduler).run_wave(0)

    assert MID_WAVE_TRIPS == [0], "the seam must have fired, or this fixture proves nothing"
    assert report.admission.breached is False, (
        "if `admit` itself breached this is the PRE-wave path and the defect is not expressible"
    )
    assert len(CALLS) == 1 and CALLS[0][0] == "repo-a", (
        "exactly the member admitted before the trip runs; the rest are discovered mid-wave"
    )
    assert sorted(report.withheld) == sorted(SIX[1:])
    assert report.state is WaveState.PARTIAL
    assert report.exit_code == 4
    assert report.halt is not None
    assert report.halt.reason is HaltReason.WAVE_WALLCLOCK

    message = str(report.halt)
    elapsed = re.search(r"after ([\d.]+)s", message)
    assert elapsed is not None
    assert float(elapsed.group(1)) == pytest.approx(await scheduler.elapsed_s(0), abs=0.05), (
        "the elapsed must be the wave's elapsed at the moment it stopped admitting, not the "
        "snapshot `admit` took before the wave started"
    )
    assert float(elapsed.group(1)) >= 60, (
        "a message that says a 60s ceiling was SPENT after less than 60s contradicts itself, "
        "and reads as a broken harness rather than as a ceiling to raise"
    )
    assert float(elapsed.group(1)) > report.admission.elapsed_s, (
        "THE discriminator: on the mid-wave path the pre-wave snapshot is strictly smaller, "
        "and it is the number the defect printed"
    )


# ======================================================================================
# §12.24: budget_ledger.spent_usd == SUM(attempts.cost_usd)
# ======================================================================================


def _cost_recording_sink(harness: Harness) -> ResultSink[ScriptedOutput]:
    """Wires each landed dispatch's `WorkerResult.usage.cost_usd` into a real `attempts` row via
    the real `record_attempt` (`state/repository.py:2124`) — the sink §12.24 needs and that no
    existing fixture in this file wires. `PhaseRunner`'s `sink=None` default means none of this
    file's other tests ever write an `attempts` row at all.
    """
    counts: dict[str, int] = {}

    async def sink(
        *, repo_id: str, phase: Phase, fence: int, result: WorkerResult[ScriptedOutput]
    ) -> None:
        counts[repo_id] = counts.get(repo_id, 0) + 1
        stamp = harness.clock().isoformat()
        await harness.repo.record_attempt(
            AttemptRow(
                attempt_id=f"{repo_id}-{counts[repo_id]}",
                run_id=RUN,
                repo_id=repo_id,
                phase=phase,
                attempt=counts[repo_id],
                started_at=stamp,
                finished_at=stamp,
                cost_usd=result.usage.cost_usd,
            )
        )

    return sink


async def test_budget_ledger_spent_usd_equals_the_sum_of_attempts_cost_usd(
    harness: Harness,
) -> None:
    """§12.24's ledger-sum invariant, under a real priced dispatch: `budget_ledger.spent_usd ==
    SUM(attempts.cost_usd)`.

    Both columns share one source value (`TokenUsage.cost_usd`, `client.py:965-994`'s `_stamp`),
    traced through `runner.py:804-811` (which bills `reservation.record()` off `execution.usage`)
    and `budgets.py:637-663` (`CostLedger.settle`, which grows `budget_ledger.spent_usd` by the
    settled reservation's actual cost) on one side, and this test's own sink → `record_attempt` on
    the other. Two repos, each with one real `ctx.llm.complete` call through `make_router()`'s
    priced `fake-1` target (`Price(in=1.0, out=2.0)`, `ScriptedBackend`'s fixed
    `TokenUsage(input_tokens=12, output_tokens=4)`), so the SUM has two real addends rather than
    one — a single-repo fixture cannot tell "the ledger equals the one attempt it made" apart from
    "the ledger equals the SUM of all attempts", and those are the same number when there is only
    one row.
    """
    await _seed(harness, "repo-a", "repo-b")
    await harness.plan(("repo-a", "repo-b"))
    BEHAVIOURS["repo-a"] = [asks_the_model_and_bills_it()]
    BEHAVIOURS["repo-b"] = [asks_the_model_and_bills_it()]

    report = await harness.runner(sink=_cost_recording_sink(harness)).run_wave(0)

    assert report.halt is None
    for repo_id in ("repo-a", "repo-b"):
        status, attempts, _, _, _ = await harness.phase_row(repo_id)
        assert (status, attempts) == ("SUCCEEDED", 1), (
            f"{repo_id} must actually have dispatched and billed, or the sums below are vacuous"
        )

    budget = await harness.repo.get_budget(RUN)
    assert budget is not None
    async with harness.read_conn.execute(
        "SELECT SUM(cost_usd) FROM attempts WHERE run_id = ?", (RUN,)
    ) as cursor:
        row = await cursor.fetchone()
    attempts_total = float(row[0]) if row is not None and row[0] is not None else 0.0

    assert budget.spent_usd > 0, (
        "the fixture must actually be priced and actually have dispatched, or an untested-but-"
        "equal 0.0 == 0.0 would pass this test for the wrong reason"
    )
    assert abs(budget.spent_usd - attempts_total) < 1e-9, (
        f"ledger spent ${budget.spent_usd} but attempts.cost_usd sums to ${attempts_total}"
    )


# ======================================================================================
# D89 Phase 2 Task A (ADR-0102): `PreDispatchHook` ordering, at the `PhaseRunner` layer
# ======================================================================================
#
# The claim-lifecycle mechanics (repository CAS, the TRANSFORM-only wiring in `cli.py`, the
# happy-path DONE/PENDING resolution) are proven in `tests/test_d89_phase2_claim_lifecycle.py`.
# What belongs HERE, in the driver's own test file, is the one property `_dispatch` itself owns
# and that no `cli.py`-level test can see: a hook passed as `pre_dispatch` runs EXACTLY ONCE per
# dispatch, strictly BEFORE `worker.execute`, carrying the FINAL payload — and is not invoked at
# all when `ReEntry.COMPLETE` means the worker never runs.


def _pre_dispatch_recorder(
    events: list[str], calls: list[tuple[str, Phase, tuple[str, ...]]]
) -> PreDispatchHook[ScriptedInput]:
    async def hook(*, repo_id: str, phase: Phase, payload: ScriptedInput) -> None:
        events.append(f"pre_dispatch:{repo_id}")
        calls.append((repo_id, phase, tuple(payload.units)))

    return hook


def marks(events: list[str], label: str) -> Behaviour:
    """A behaviour that appends to the SAME event log the hook writes to, so ordering between
    the two collaborators is one list's order and not two lists a reader has to interleave."""

    async def behaviour(
        ctx: WorkerContext, payload: ScriptedInput
    ) -> WorkerResult[ScriptedOutput]:
        events.append(label)
        return WorkerResult(status="ok", output=ScriptedOutput(note=label))

    return behaviour


async def test_pre_dispatch_hook_fires_once_before_worker_execute_with_final_payload(
    harness: Harness,
) -> None:
    """The core ordering contract `_dispatch` (`runner.py`) must hold for D89 Phase 2 Task A's
    coarse-row claim to be sound: the hook has to see the units the worker is ABOUT to run, and
    it has to run BEFORE `worker.execute`, so a crash between the two leaves the coarse row
    genuinely `RUNNING` for `_ARBITRATED_TASKS_SQL` to find — a hook that fired after execute (or
    not at all) would make the row's `RUNNING` window a fiction.
    """
    await _seed(harness, "repo-a")
    await harness.plan(("repo-a",))
    events: list[str] = []
    calls: list[tuple[str, Phase, tuple[str, ...]]] = []
    BEHAVIOURS["repo-a"] = [marks(events, "run:repo-a")]

    report = await harness.runner(pre_dispatch=_pre_dispatch_recorder(events, calls)).run_wave(0)

    assert report.halt is None
    assert events == ["pre_dispatch:repo-a", "run:repo-a"], (
        "the hook must fire exactly once, strictly before the worker ran"
    )
    assert calls == [("repo-a", PHASE, UNITS)], (
        "the hook must observe this repo's phase and the FINAL payload the worker was given"
    )


async def test_pre_dispatch_hook_sees_the_rebuilt_payload_after_a_rejected_checkpoint(
    harness: Harness,
) -> None:
    """A `REJECTED` checkpoint (§7.1) makes `_dispatch` rebuild `payload` a second time BEFORE
    the worker runs (`runner.py`, the `re_entry is ReEntry.REJECTED` branch). The hook must see
    THAT rebuilt payload — the whole units set, not the stale checkpoint-scoped one it was first
    called with — or a claim hook downstream (`cli.py`'s `_TransformClaimHook`) would populate
    `target_paths` with a partial unit list while the worker is about to run the phase whole.
    """
    await _seed(harness, "repo-a")
    await harness.plan(("repo-a",))
    await _checkpoint(harness, "repo-a", completed=["u1", "u2"], remaining=["u3"])
    PRECONDITIONS["repo-a"] = [False]  # refused: re-runs whole, rebuilding the payload
    events: list[str] = []
    calls: list[tuple[str, Phase, tuple[str, ...]]] = []
    BEHAVIOURS["repo-a"] = [marks(events, "run:repo-a")]

    report = await harness.runner(pre_dispatch=_pre_dispatch_recorder(events, calls)).run_wave(0)

    assert report.halt is None
    assert report.outcomes["repo-a"].checkpoint_rejected is True
    assert events == ["pre_dispatch:repo-a", "run:repo-a"]
    assert calls == [("repo-a", PHASE, UNITS)], (
        "the hook was called with the STALE (checkpoint-scoped) payload instead of the "
        "REJECTED-rebuilt whole-phase one"
    )


async def test_pre_dispatch_hook_is_not_called_when_reentry_is_already_complete(
    harness: Harness,
) -> None:
    """`ReEntry.COMPLETE` means the checkpoint owes nothing and the worker is NEVER dispatched
    (`runner.py`'s `_already_complete` early return, before the hook call site). A hook that
    fired here would claim a coarse row for a dispatch that is never going to happen — a phantom
    `RUNNING` row nothing will ever resolve to `DONE` or back to `PENDING`.
    """
    await _seed(harness, "repo-a")
    await harness.plan(("repo-a",))
    await _checkpoint(harness, "repo-a", completed=list(UNITS), remaining=[])
    events: list[str] = []
    calls: list[tuple[str, Phase, tuple[str, ...]]] = []
    BEHAVIOURS["repo-a"] = [boom("completed work must not be re-dispatched, hook included")]

    report = await harness.runner(pre_dispatch=_pre_dispatch_recorder(events, calls)).run_wave(0)

    assert report.halt is None
    assert report.outcomes["repo-a"].skipped_complete is True
    assert events == [] and calls == [], (
        "the pre-dispatch hook fired for a repo the runner never actually dispatched"
    )


async def test_pre_dispatch_hook_absent_is_byte_identical_to_before_this_change(
    harness: Harness,
) -> None:
    """BUILD/VERIFY/SCAN's `PhaseRunner(...)` sites (`cli.py`) pass no `pre_dispatch` — this is
    their exact call shape. Every OTHER assertion in this file already runs `harness.runner()`
    with no `pre_dispatch` and must keep passing unmodified; this test additionally pins that a
    default-`pre_dispatch` dispatch reaches `SUCCEEDED` with the untouched call/units shape, so a
    future default-value regression (e.g. `pre_dispatch` defaulting to something callable) would
    show up here even in isolation from the rest of the suite.
    """
    await _seed(harness, "repo-a")
    await harness.plan(("repo-a",))
    BEHAVIOURS["repo-a"] = [ok()]

    report = await harness.runner().run_wave(0)

    assert report.halt is None
    assert (await harness.phase_row("repo-a"))[0] == "SUCCEEDED"
    assert [call[3] for call in CALLS] == [UNITS]
