"""`BaseWorker` — the contracts that stop interrupted work from being replayed (SPEC §7.1).

Every test here is about a failure that has a cost attached, not about a shape:

* a five-valued `status`, because a boolean verdict made 40 landed commits invisible;
* cooperative cancellation, because `CancelledError` never reaches a subprocess;
* a structured `WorkerError`, because `retry.py` must not branch on message text;
* a fenced result, because two owners writing one worktree is corruption, not a race;
* `TokenUsage` used through the API it actually has, which is what the `.merged()`
  `AttributeError` on the first attempt of every worker would have caught.

Coroutines are driven with `asyncio.run` rather than a plugin marker, matching
`tests/conftest.py`: the suite must stay runnable in a bare pydantic+pytest environment.
"""

from __future__ import annotations

import asyncio
import inspect
import pkgutil
from importlib import import_module
from typing import ClassVar
from uuid import UUID

import pytest
from pydantic import Field, ValidationError

import fleet.workers as workers_pkg
from fleet.llm.client import (
    BackendTarget,
    BudgetExhausted,
    CallBudget,
    SchemaUnsatisfied,
    TierUnavailable,
)
from fleet.models.base import LOG_TAIL_BYTES
from fleet.models.enums import (
    ContextPolicy,
    FailureClass,
    ModelTier,
    Phase,
    RepoStatus,
    TransformTier,
)
from fleet.models.tasks import TokenUsage
from fleet.state.repository import PhaseRow
from fleet.workers import rewrite as rewrite_mod
from fleet.workers.base import (
    TIER_LADDER,
    BaseWorker,
    UnsafeSourcePathError,
    WorkerContext,
    WorkerError,
    WorkerInput,
    WorkerOutput,
    WorkerResult,
    accumulate,
    assert_stateless,
    classify_exception,
    error_from_exception,
    implements_preconditions,
    loop_now,
    total_tokens,
    unfinished_units,
)

RUN_ID = UUID("00000000-0000-4000-8000-00000000beef")
OWNER = "host:container:4242:boot"
FENCE = 7


# =======================================================================================
# fakes — a worker, its payload, and the read-only half of the repository
# =======================================================================================


class Units(WorkerInput):
    """A payload that names its units of work, so `completed`/`remaining` mean something."""

    units: list[str] = Field(default_factory=list)
    deadline_after: int | None = None  # simulate the runner's wall clock running out mid-run
    block_on: str | None = None  # simulate a subprocess that outlives the cancel signal


class Landed(WorkerOutput):
    """The checkpoint payload: what actually reached the tree."""

    completed: list[str] = Field(default_factory=list)


class FakeDb:
    """`ReadOnlyRepository` narrowed to what `BaseWorker` reads: the phase's lease.

    `fences` is consumed one entry per `get_phase` call, so a test can express "the lease was
    ours when we started and had been reclaimed by the time we finished" — the only interleaving
    that matters, and the one a single-shot stub cannot express.
    """

    def __init__(self, fences: list[int] | None = None, owner: str = OWNER) -> None:
        self.fences = fences
        self.owner = owner
        self.calls = 0

    async def get_phase(self, run_id: str, repo_id: str, phase: Phase) -> PhaseRow | None:
        self.calls += 1
        if self.fences is None:
            return None
        fence = self.fences[min(self.calls - 1, len(self.fences) - 1)]
        return PhaseRow(
            run_id=run_id,
            repo_id=repo_id,
            phase=phase,
            status=RepoStatus.RUNNING,
            attempts=0,
            max_attempts=3,
            transient_retries=0,
            lease_owner=self.owner,
            lease_fence=fence,
            lease_expires_at=None,
            last_error=None,
            updated_at="2026-08-09T00:00:00+00:00",
        )


def make_ctx(
    now: float,
    *,
    db: FakeDb | None = None,
    seconds_left: float = 60.0,
    tokens_left: int = 1_000_000,
    usd_left: float = 100.0,
    attempt: int = 1,
) -> WorkerContext:
    """A context with inert collaborators and a REAL deadline on the loop's own clock.

    `attempt` is the runner's persisted ladder position (`phases.attempts + 1`) — the single
    input that decides which rung `execute()` runs.
    """
    sentinel: object = object()
    deadline = now + seconds_left
    return WorkerContext(
        run_id=RUN_ID,
        repo_id="acme-commons",
        attempt=attempt,
        workdir="/nonexistent/worktree",
        lease_owner=OWNER,
        lease_fence=FENCE,
        deadline=deadline,
        cancel=asyncio.Event(),
        budget=CallBudget(
            remaining_tokens=tokens_left, remaining_usd=usd_left, deadline=deadline
        ),
        db=db or FakeDb(),  # type: ignore[arg-type]
        llm=sentinel,  # type: ignore[arg-type]
        router=sentinel,  # type: ignore[arg-type]
        limits=sentinel,  # type: ignore[arg-type]
        log=sentinel,  # type: ignore[arg-type]
    )


class UnitWorker(BaseWorker[Units, Landed]):
    """Lands units one at a time and honours `deadline`/`cancel` BETWEEN them, never inside one.

    Class-level recorders, not instance attributes: a registry value is a singleton (§7.2), so
    the fake must be as stateless as the real thing or the test would prove the wrong shape.
    """

    name: ClassVar[str] = "unit"
    phase: ClassVar[Phase] = Phase.TRANSFORM
    input_model: ClassVar[type[WorkerInput]] = Units
    output_model: ClassVar[type[WorkerOutput]] = Landed
    cancel_grace_s: ClassVar[float] = 0.05

    PROCESSED: ClassVar[list[str]] = []
    CHECKPOINT: ClassVar[WorkerResult[Landed] | None] = None
    ON_CANCEL: ClassVar[list[str]] = []
    BLOCKED: ClassVar[asyncio.Event] = asyncio.Event()
    RELEASE: ClassVar[asyncio.Event] = asyncio.Event()

    @classmethod
    def reset(cls) -> None:
        cls.PROCESSED = []
        cls.CHECKPOINT = None
        cls.ON_CANCEL = []
        cls.BLOCKED = asyncio.Event()
        cls.RELEASE = asyncio.Event()

    async def run(self, ctx: WorkerContext, payload: Units) -> WorkerResult[Landed]:
        done: list[str] = []
        for index, unit in enumerate(payload.units):
            # The ONLY place a worker may stop: a unit boundary. Stopping inside a unit is what
            # leaves a half-applied rewrite that the next attempt cannot tell from a rule miss.
            if ctx.expired(loop_now()) or ctx.cancelled():
                return WorkerResult(
                    status="partial",
                    output=Landed(completed=done),
                    completed_units=done,
                    remaining_units=list(payload.units[index:]),
                    usage=TokenUsage(input_tokens=10, output_tokens=5, cost_usd=0.01),
                )
            if unit == payload.block_on:
                type(self).BLOCKED.set()
                await type(self).RELEASE.wait()  # the "subprocess" for this unit
            type(self).PROCESSED.append(unit)
            done.append(unit)
            if payload.deadline_after is not None and len(done) == payload.deadline_after:
                ctx.deadline = loop_now()  # the runner's wall clock is spent, mid-run
        return WorkerResult(
            status="ok",
            output=Landed(completed=done),
            completed_units=done,
            usage=TokenUsage(input_tokens=10, output_tokens=5, cost_usd=0.01),
        )

    async def preconditions_hold(self, ctx: WorkerContext, payload: Units) -> bool:
        checkpoint = type(self).CHECKPOINT
        if checkpoint is None:
            return True
        if checkpoint.output is None:
            return False  # a checkpoint with no landed output cannot be resumed from
        return bool(unfinished_units(payload.units, checkpoint))

    async def on_cancel(self, ctx: WorkerContext) -> None:
        type(self).ON_CANCEL.append("called")
        type(self).RELEASE.set()  # what a real worker's kill of the process group achieves


class NeverRuns(UnitWorker):
    """Records the fact of being called at all, so "refused before doing work" is provable."""

    name: ClassVar[str] = "never-runs"
    CALLS: ClassVar[list[str]] = []

    async def run(self, ctx: WorkerContext, payload: Units) -> WorkerResult[Landed]:
        type(self).CALLS.append("ran")
        return WorkerResult(status="ok", output=Landed())


class FailingWorker(UnitWorker):
    """Always fails with a caller-supplied, fully structured `WorkerError`."""

    name: ClassVar[str] = "failing"
    FAILURE: ClassVar[WorkerError] = WorkerError(
        failure_class=FailureClass.BUILD_ERROR, retryable=True
    )

    async def run(self, ctx: WorkerContext, payload: Units) -> WorkerResult[Landed]:
        type(self).PROCESSED.append("attempt")
        return WorkerResult(
            status="failed",
            error=type(self).FAILURE,
            usage=TokenUsage(input_tokens=10, output_tokens=5, cost_usd=0.01),
        )


SIXTY = [f"u{n:02d}" for n in range(60)]


# =======================================================================================
# (1) the headline: interrupted work is kept, and re-entry does not replay it
# =======================================================================================


def test_forty_of_sixty_units_survive_the_deadline_and_are_not_redone_on_re_entry() -> None:
    """A worker that lands 40 of 60 units and hits its deadline reports `partial` WITH the 40.

    Why it matters: with a boolean verdict this invocation recorded `ok=False, output=None`, so
    the 40 real commits were invisible to state and attempt 2 replayed all 60 against an
    already-rewritten tree — producing no-op patches the ladder misreads as `RULE_MISS`, at full
    LLM price, forever. The partial output IS the checkpoint, `preconditions_hold` consumes it,
    and re-entry resumes at `remaining_units`.
    """
    UnitWorker.reset()
    worker = UnitWorker()

    async def first() -> object:
        ctx = make_ctx(loop_now(), db=FakeDb())
        return await worker.execute(ctx, Units(units=SIXTY, deadline_after=40))

    execution = asyncio.run(first())
    partial = execution.final
    assert partial is not None
    assert partial.status == "partial"
    assert partial.output is not None, "the landed work must be persistable as the checkpoint"
    assert partial.completed_units == SIXTY[:40]
    assert partial.remaining_units == SIXTY[40:]
    assert partial.landed and not partial.ok, "landed work, but not a success"
    assert execution.status is RepoStatus.PENDING, "re-entry is owed, not a human"
    landed_first = SIXTY[:40]
    assert landed_first == UnitWorker.PROCESSED

    # Re-entry: the checkpoint is what the next invocation is allowed to see.
    UnitWorker.CHECKPOINT = partial
    owed = unfinished_units(SIXTY, partial)
    assert owed == SIXTY[40:]

    async def second() -> tuple[bool, object]:
        ctx = make_ctx(loop_now(), db=FakeDb())
        payload = Units(units=owed)
        admitted = await worker.preconditions_hold(ctx, payload)
        return admitted, await worker.execute(ctx, payload)

    admitted, resumed = asyncio.run(second())
    assert admitted is True
    assert resumed.ok is True
    assert UnitWorker.PROCESSED == SIXTY, "every unit landed exactly once"
    assert len(set(UnitWorker.PROCESSED)) == 60, "a replayed unit is a no-op patch, not progress"


class PreconditionSpy(FailingWorker):
    """Counts §7.1 checks, so *who* consults the re-entry guard is provable, not assumed."""

    name: ClassVar[str] = "precondition-spy"
    CHECKS: ClassVar[list[str]] = []

    async def preconditions_hold(self, ctx: WorkerContext, payload: Units) -> bool:
        type(self).CHECKS.append(ctx.repo_id)
        return await super().preconditions_hold(ctx, payload)


def test_the_retry_ladder_never_re_asks_the_re_entry_guard() -> None:
    """`execute()` runs rungs; `PhaseRunner._re_entry` decides re-entry — exactly once, before
    the dispatch. This pins WHERE the check lives, and the two halves must not both own it.

    Why it is worth a test of its own: `preconditions_hold` was inert for the whole life of the
    harness (nothing called it at all), and the obvious repair is to bolt it onto the nearest
    thing that touches a payload — this template method. That would be wrong twice over. It
    re-asks a question about landed work once per rung of a ladder that deliberately re-runs the
    SAME payload, and only the driver can act on a `False`: the checkpoint it must discard and
    the whole-phase payload it must rebuild are the driver's, not `execute`'s. A second caller
    here would silently double-check every resumed dispatch and, worse, invite a re-derived
    verdict that disagrees with the one the driver already acted on.
    """
    PreconditionSpy.reset()
    PreconditionSpy.CHECKS = []
    # Pinned on the subclass rather than inherited: `FailingWorker.FAILURE` is a mutable
    # ClassVar another test rebinds, and a non-retryable one would stop the ladder at one rung.
    PreconditionSpy.FAILURE = WorkerError(
        failure_class=FailureClass.BUILD_ERROR, retryable=True, exit_code=1
    )
    worker = PreconditionSpy()

    async def drive() -> object:
        return await worker.execute(make_ctx(loop_now(), db=FakeDb()), Units(units=SIXTY))

    execution = asyncio.run(drive())

    assert execution.attempts == 3, "the ladder must really have run more than one rung"
    assert PreconditionSpy.CHECKS == [], (
        "execute() consulted preconditions_hold: the guard now has two owners that can disagree"
    )


def test_preconditions_refuse_re_entry_when_the_checkpoint_has_no_landed_output() -> None:
    """A checkpoint that recorded no output cannot be resumed from — the phase re-runs.

    Why it matters: admitting re-entry against an empty checkpoint is the blind replay the
    method exists to forbid, and it is indistinguishable from success until the diff is empty.
    """
    UnitWorker.reset()
    UnitWorker.CHECKPOINT = WorkerResult[Landed](
        status="partial", completed_units=["u00"], remaining_units=SIXTY[1:]
    )
    worker = UnitWorker()

    async def check() -> bool:
        return await worker.preconditions_hold(make_ctx(loop_now()), Units(units=SIXTY))

    assert asyncio.run(check()) is False


# =======================================================================================
# (2) cancellation is cooperative, and it lands on a unit boundary
# =======================================================================================


def test_on_cancel_runs_and_the_worker_stops_between_units_not_inside_one() -> None:
    """`on_cancel` fires once run() has not returned inside the grace window, and the in-flight
    unit still completes before the worker returns `partial`.

    Why it matters: `CancelledError` cannot interrupt a subprocess or a process-pool child, so a
    runner that merely cancelled the coroutine would mark the task failed while the orphan kept a
    CPU core and the worktree lock the cleanup path is about to delete. `on_cancel` is what kills
    the process group; the unit boundary is what keeps the tree consistent.
    """
    UnitWorker.reset()
    worker = UnitWorker()

    async def drive() -> object:
        ctx = make_ctx(loop_now(), db=FakeDb())
        payload = Units(units=SIXTY[:6], block_on="u02")
        task = asyncio.create_task(worker.execute(ctx, payload))
        await UnitWorker.BLOCKED.wait()  # the worker is inside unit u02
        ctx.cancel.set()
        return await task

    execution = asyncio.run(drive())
    result = execution.final
    assert UnitWorker.ON_CANCEL == ["called"], "the grace window expired; the killer must run"
    assert result is not None
    assert result.status == "partial"
    assert result.completed_units == ["u00", "u01", "u02"], "the in-flight unit finished"
    assert result.remaining_units == ["u03", "u04", "u05"]
    assert UnitWorker.PROCESSED == ["u00", "u01", "u02"]
    assert execution.status is RepoStatus.PENDING


# =======================================================================================
# (3) a spent deadline refuses before it spawns anything
# =======================================================================================


def test_a_deadline_already_in_the_past_refuses_before_doing_any_work() -> None:
    """Nothing is attempted, nothing is spawned, and no attempt is consumed.

    Why it matters: a subprocess launched one second past the deadline outlives the wave that
    would have reaped it, and burning a ladder rung on an invocation that cannot finish is how a
    repo reaches REQUIRES_HUMAN_INTERVENTION without a single real failure.
    """
    UnitWorker.reset()
    NeverRuns.CALLS = []
    worker = NeverRuns()

    async def drive() -> object:
        ctx = make_ctx(loop_now(), db=FakeDb(), seconds_left=-1.0)
        return await worker.execute(ctx, Units(units=SIXTY))

    execution = asyncio.run(drive())
    assert NeverRuns.CALLS == [], "run() must not be entered past the deadline"
    assert execution.attempts == 0, "a refusal is not an attempt"
    assert execution.final is not None and execution.final.status == "timeout"
    assert execution.status is RepoStatus.PENDING


# =======================================================================================
# (4) a stale fence discards, never merges
# =======================================================================================


def test_a_result_produced_under_a_reclaimed_lease_is_discarded_not_merged() -> None:
    """The work happened, the lease moved, and the result is dropped whole — bill included.

    Why it matters: a reclaimed lease means another process already owns this worktree (§11.5).
    Merging the old owner's result interleaves two writers on one repo, and billing its tokens
    charges the run twice for work only one of them may keep.
    """
    UnitWorker.reset()
    worker = UnitWorker()
    db = FakeDb(fences=[FENCE, FENCE + 1])  # ours on entry, reclaimed by the time we return

    async def drive() -> object:
        return await worker.execute(make_ctx(loop_now(), db=db), Units(units=SIXTY[:3]))

    execution = asyncio.run(drive())
    worked_on = SIXTY[:3]
    assert worked_on == UnitWorker.PROCESSED, "the work DID happen: a discard, not a skip"
    assert execution.fence_stale is True
    assert execution.results == [], "a stale result is discarded, never merged"
    assert execution.usage == TokenUsage(), "and never billed"
    assert execution.attempts == 0
    assert execution.status is RepoStatus.PENDING, "the repo belongs to its new owner now"


def test_a_lease_already_reclaimed_aborts_without_touching_git() -> None:
    """Reclaimed before entry: run() is never called at all."""
    UnitWorker.reset()
    NeverRuns.CALLS = []
    worker = NeverRuns()

    async def drive() -> object:
        db = FakeDb(fences=[FENCE + 1])
        return await worker.execute(make_ctx(loop_now(), db=db), Units(units=SIXTY[:3]))

    execution = asyncio.run(drive())
    assert NeverRuns.CALLS == []
    assert execution.fence_stale is True


def test_a_lease_held_by_another_owner_at_the_same_fence_is_also_stale() -> None:
    """Fence equality alone is not ownership: the owner string is checked too."""
    UnitWorker.reset()
    NeverRuns.CALLS = []
    worker = NeverRuns()

    async def drive() -> object:
        db = FakeDb(fences=[FENCE], owner="other-host:other:1:boot")
        return await worker.execute(make_ctx(loop_now(), db=db), Units(units=SIXTY[:3]))

    assert asyncio.run(drive()).fence_stale is True
    assert NeverRuns.CALLS == []


# =======================================================================================
# (5) the error is structured: a real exit code, and a tail that truncates
# =======================================================================================


def test_a_worker_error_keeps_the_exit_code_and_truncates_a_400_000_char_stderr() -> None:
    """A 400 000-character stderr VALIDATES, truncated, with its exit code intact.

    Why it matters: a length bound that REJECTS oversized evidence is an attempt that is never
    persisted, an `attempts` counter that never increments, and a repair loop re-running the
    identical failing build forever — Rule 11 inverted into a silent infinite loop. And exit 137
    (OOM killer, re-queue) versus exit 1 (build error, repair prompt) is a difference no string
    comparison recovers, so the code is a field, not prose.
    """
    stderr = "".join(f"line {n} of a very angry gradle daemon\n" for n in range(12_000))
    assert len(stderr) > 400_000

    error = WorkerError(
        failure_class=FailureClass.BUILD_ERROR,
        retryable=True,
        exit_code=137,
        stderr_tail=stderr,
        artifact_ref="artifacts/logs/run/attempt.log",
    )

    assert error.exit_code == 137
    assert error.stderr_tail.endswith("bytes]"), "truncation must announce itself"
    assert len(error.stderr_tail.encode()) < LOG_TAIL_BYTES + 200
    assert error.stderr_tail.count("angry gradle daemon") > 0, "the TAIL is what is kept"
    assert error.artifact_ref is not None, "the full stream lives on disk, by reference"


def test_a_failed_result_cannot_be_recorded_as_prose() -> None:
    """`status='failed'` without a structured error is rejected at construction.

    Why it matters: `retry.py` branches on `retryable`. A result carrying only a message string
    leaves it nothing to branch on but text matching, which is the defect this type removes.
    """
    with pytest.raises(ValidationError):
        WorkerResult[Landed](status="failed")
    with pytest.raises(ValidationError):
        WorkerResult[Landed](status="partial", completed_units=[])
    with pytest.raises(ValidationError):
        WorkerResult[Landed](status="ok", completed_units=["a"], remaining_units=["a"])


# =======================================================================================
# (5b) D133: the fallback classifier consults a declared `LlmError.failure_class`
# =======================================================================================


def test_classify_exception_consults_a_declared_llm_failure_class() -> None:
    """`TierUnavailable`/`BudgetExhausted` declare their own `failure_class` (`llm/client.py`).

    Before D133, `classify_exception` never looked — a bare, unwrapped `TierUnavailable`
    escaping a worker that does not classify its own LLM errors (research-51's finding) fell
    through every isinstance arm and came out `UNKNOWN`, despite declaring
    `BACKEND_UNAVAILABLE` at the raise site.
    """
    bare = TierUnavailable(ModelTier.HEAVY, ("fake:heavy-1", "fake:heavy-2"))
    assert classify_exception(bare) is FailureClass.BACKEND_UNAVAILABLE

    budget = BudgetExhausted("would exceed the run's remaining ceiling")
    assert classify_exception(budget) is FailureClass.BUDGET_EXHAUSTED


def test_classify_exception_walks_a_wrapping_exceptions_cause_chain() -> None:
    """`rewrite.py::_repair` re-raises a caught `LlmError` wrapped: `raise WorkerRepairError(...)
    from exc` (a plain `RuntimeError`, itself declaring no `failure_class`). Before D133 this
    wrap lost `TierUnavailable`'s declaration to the generic isinstance arms and misclassified a
    genuine backend outage as `UNKNOWN` — worse, as a *retryable* `UNKNOWN`, charging the repo an
    attempt for an outage that was not its fault.

    `error_from_exception` must also recover the exhausted `tier` through the same wrap, so
    `record_backend_unavailable`'s finding can name it instead of falling back to the whole-run
    disclosure caveat.
    """
    def _wrap(cause: TierUnavailable) -> None:
        raise RuntimeError("escalation rung failed: tier heavy exhausted") from cause

    origin = TierUnavailable(ModelTier.HEAVY, ("fake:heavy-1",))
    try:
        _wrap(origin)
    except RuntimeError as wrapped:
        assert classify_exception(wrapped) is FailureClass.BACKEND_UNAVAILABLE
        error = error_from_exception(wrapped)

    assert error.failure_class is FailureClass.BACKEND_UNAVAILABLE
    assert error.retryable is False, "BACKEND_UNAVAILABLE is in NON_RETRYABLE"
    assert error.tier is ModelTier.HEAVY


def test_classify_exception_leaves_an_undeclared_llm_error_to_the_generic_arms() -> None:
    """Not every `LlmError` declares a `failure_class` — only `BudgetExhausted` and
    `TierUnavailable` do (`llm/client.py`). `SchemaUnsatisfied` — a genuine model-output failure,
    not a fail-closed infrastructure one — declares none, and `classify_exception`'s generic
    arms below the new one have never known about LLM-specific exceptions (that finer mapping is
    `workers/classify.py::_error_for`'s job, the "authoritative", payload-aware classifier this
    module's docstring says it is only the FALLBACK for). This proves D133 did not turn every
    `LlmError` into a hard classification wholesale — the walk finds no declared `failure_class`
    on it (or on any wrapper), so it falls all the way through to `UNKNOWN`, exactly as it did
    before this fix.
    """
    target = BackendTarget(backend="fake", model_id="fake-heavy", price="free")
    schema_unsatisfied = SchemaUnsatisfied(target, 2, "response failed schema validation twice")
    assert classify_exception(schema_unsatisfied) is FailureClass.UNKNOWN
    assert error_from_exception(schema_unsatisfied).tier is None


# =======================================================================================
# security finding #7 — a refused symlink read classifies as UNSAFE_SOURCE_PATH, not UNKNOWN
# =======================================================================================
def test_classify_exception_gives_a_refused_symlink_read_its_own_non_retryable_class() -> None:
    """`UnsafeSourcePathError` (`rewrite.py`'s `is_symlink()` guard, security finding #7) is a
    plain `RuntimeError`, not an `LlmError` — the D133 walk above never reaches it. Left to the
    generic isinstance arms it would classify as `UNKNOWN`, and `UNKNOWN` is retryable BY DEFAULT
    (`NON_RETRYABLE` does not contain it): a retry only re-reads the same tracked symlink, so a
    default-retryable `UNKNOWN` would burn a repair rung re-discovering the exact same refusal.
    `classify_exception` must name it `UNSAFE_SOURCE_PATH` — one of `NON_RETRYABLE`'s structural
    members, alongside `PREFLIGHT`/`CYCLE`, because the tree still names the same symlink next
    attempt.
    """
    exc = UnsafeSourcePathError("java/com/acme/Evil.java: worktree path is a symlink")
    assert classify_exception(exc) is FailureClass.UNSAFE_SOURCE_PATH

    error = error_from_exception(exc)
    assert error.failure_class is FailureClass.UNSAFE_SOURCE_PATH
    assert error.retryable is False, "a symlink is the tree's own shape; a retry cannot differ"
    assert error.tier is None


# =======================================================================================
# (6) `retryable` — not the message — drives the ladder
# =======================================================================================


def _run_ladder(failure: WorkerError) -> object:
    FailingWorker.reset()
    FailingWorker.FAILURE = failure
    worker = FailingWorker()

    async def drive() -> object:
        return await worker.execute(make_ctx(loop_now(), db=FakeDb()), Units(units=["u"]))

    return asyncio.run(drive())

IDENTICAL_MESSAGE = "error: cannot find symbol com.acme.commons.Widget"


def test_retryable_not_the_message_decides_whether_the_ladder_advances() -> None:
    """Two failures with byte-identical messages; only the structured flag differs.

    Why it matters: escalation costs HEAVY-tier money. Deciding it by grepping the message means
    a build error and a dependency conflict that happen to phrase themselves alike get the same
    treatment — one wasting two LLM rungs on a structural problem no prompt can fix.
    """
    retryable = _run_ladder(
        WorkerError(
            failure_class=FailureClass.BUILD_ERROR,
            retryable=True,
            exit_code=1,
            stderr_tail=IDENTICAL_MESSAGE,
        )
    )
    structural = _run_ladder(
        WorkerError(
            failure_class=FailureClass.DEP_CONFLICT,
            retryable=False,
            exit_code=1,
            stderr_tail=IDENTICAL_MESSAGE,
        )
    )

    assert retryable.final is not None and structural.final is not None
    assert retryable.final.error is not None and structural.final.error is not None
    assert retryable.final.error.stderr_tail == structural.final.error.stderr_tail

    assert retryable.attempts == 3, "a retryable failure walks the whole ladder"
    assert retryable.tiers == list(TIER_LADDER), "and escalates, never repeats a rung"
    assert retryable.tiers[0] is TransformTier.DETERMINISTIC

    assert structural.attempts == 1, "a non-retryable failure stops at the first rung"
    assert structural.status is RepoStatus.REQUIRES_HUMAN_INTERVENTION
    assert structural.tiers == [TransformTier.DETERMINISTIC]


def test_the_worker_owns_no_transient_retry_budget_of_its_own() -> None:
    """REPLACES `test_transient_infra_is_not_an_attempt_but_is_capped`, which asserted that
    `execute()` re-ran a `TRANSIENT_INFRA` rung up to `max_transient_retries` times for free.

    That budget was implemented TWICE — here and in `orchestrator.retry.RetryPolicy` — and the
    §11.8 allowance is a per-`(repo, phase)` number, not a per-layer one. Nested budgets do not
    agree, they MULTIPLY: the worker's 5 free re-runs inside the driver's 4 is up to 30 calls
    against an endpoint that has already said no, and only the driver's count is persisted, so
    the surviving record understates what the fleet actually did by a factor of six.

    ADR-0014 still holds — a transient failure is not an attempt — but it is `RetryPolicy` that
    says so, because it is the only layer whose count reaches `phases.transient_retries` and
    therefore the only one whose budget survives the `SIGKILL` it is supposed to be robust to.
    Here, a transient failure is simply returned, once, typed.
    """
    FailingWorker.reset()
    FailingWorker.FAILURE = WorkerError(
        failure_class=FailureClass.TRANSIENT_INFRA, retryable=True, stderr_tail="conn reset"
    )
    worker = FailingWorker()

    async def drive() -> object:
        # `max_attempts=1` is how the runner dispatches: exactly one rung, durably recorded.
        return await worker.execute(
            make_ctx(loop_now(), db=FakeDb()), Units(units=["u"]), max_attempts=1
        )

    execution = asyncio.run(drive())

    assert len(FailingWorker.PROCESSED) == 1, "the worker re-ran a transient rung on its own"
    assert not hasattr(worker, "max_transient_retries"), "a second transient budget came back"
    assert not hasattr(execution, "transient_retries"), "a second transient COUNT came back"
    final = execution.final
    assert final is not None and final.error is not None
    assert final.error.failure_class is FailureClass.TRANSIENT_INFRA, (
        "the failure must reach the policy TYPED — that classification is the whole input to "
        "the one retry decision that is left"
    )


# =======================================================================================
# (6b) the ladder position: ONE authority, and it is the caller's persisted number
# =======================================================================================


def _rungs_observed(worker: LadderSpy) -> list[tuple[int, TransformTier, ContextPolicy | None]]:
    return list(worker.OBSERVED)


class LadderSpy(FailingWorker):
    """Records the rung each invocation actually observed, which is the thing under test."""

    name: ClassVar[str] = "ladder-spy"
    OBSERVED: ClassVar[list[tuple[int, TransformTier, ContextPolicy | None]]] = []

    async def run(self, ctx: WorkerContext, payload: Units) -> WorkerResult[Landed]:
        type(self).OBSERVED.append((ctx.attempt, ctx.tier, ctx.context_policy))
        return await super().run(ctx, payload)


def test_a_worker_dispatched_at_attempt_three_observes_rung_three_not_rung_zero() -> None:
    """THE headline: escalation has to actually escalate, or ADR-0014 is a comment.

    The runner drives one rung per dispatch (`max_attempts=1`) so each rung's outcome is durable
    before the next starts, and it hands the persisted ladder position in as `ctx.attempt`.
    `execute()` used to throw that away and re-derive the rung from a fresh in-process counter,
    which is always 0 on a one-rung dispatch — so attempt 3 ran the DETERMINISTIC, no-prompt rung
    exactly like attempt 1. The persisted ladder climbed; the work never did; the LLM rungs the
    whole escalation design exists to reach were unreachable in production.

    Both ends are asserted, because "always rung 3" would pass a one-sided test just as happily
    as "always rung 1" did.
    """
    LadderSpy.reset()
    LadderSpy.OBSERVED.clear()
    LadderSpy.FAILURE = WorkerError(failure_class=FailureClass.BUILD_ERROR, retryable=True)
    worker = LadderSpy()

    async def dispatch(attempt: int) -> None:
        await worker.execute(
            make_ctx(loop_now(), db=FakeDb(), attempt=attempt),
            Units(units=["u"]),
            max_attempts=1,
        )

    asyncio.run(dispatch(1))
    asyncio.run(dispatch(3))
    first, third = _rungs_observed(worker)

    assert first == (1, TransformTier.DETERMINISTIC, None), (
        "rung 1 is deterministic and renders no prompt at all"
    )
    assert third == (
        3,
        TransformTier.LLM_ESCALATION,
        ContextPolicy.EVIDENCE_PLUS_REJECTED_APPROACHES,
    ), "attempt 3 ran the deterministic rung: the ladder never reached an LLM"

    assert third[2] is not None, "the LLM-bearing rung was never reached"
    assert third[1] is not TransformTier.DETERMINISTIC


def test_a_resumed_ladder_escalates_strictly_and_never_repeats_a_rung() -> None:
    """Attempts 1 → 2 → 3, each a SEPARATE dispatch as the runner issues them, with the process
    notionally dying in between: the only thing carried across is the persisted attempt number.

    Why it matters: "attempts must escalate, not repeat" is the entire content of ADR-0014.
    Identical retries are how the reference material's infinite-loop failure mode begins, and a
    resume that re-runs a spent rung is an identical retry that also costs a rung.
    """
    LadderSpy.reset()
    LadderSpy.OBSERVED.clear()
    LadderSpy.FAILURE = WorkerError(failure_class=FailureClass.BUILD_ERROR, retryable=True)
    worker = LadderSpy()

    async def dispatch(attempt: int) -> None:
        # `max_attempts=1` is exactly how `PhaseRunner._dispatch` issues a rung: one rung per
        # invocation, so its outcome is in SQLite before the next one is allowed to start.
        await worker.execute(
            make_ctx(loop_now(), db=FakeDb(), attempt=attempt),
            Units(units=["u"]),
            max_attempts=1,
        )

    for persisted_attempts in (0, 1, 2):  # what `phases.attempts` held at each resume
        asyncio.run(dispatch(persisted_attempts + 1))

    observed = _rungs_observed(worker)
    assert [rung for rung, _, _ in observed] == [1, 2, 3]
    tiers = [tier for _, tier, _ in observed]
    assert tiers == list(TIER_LADDER), "a rung was repeated across the resume boundary"
    assert len(set(tiers)) == len(tiers), "two dispatches ran the same tier"
    assert [policy for _, _, policy in observed] == [
        None,
        ContextPolicy.EVIDENCE_ONLY,
        ContextPolicy.EVIDENCE_PLUS_REJECTED_APPROACHES,
    ], "the ADR-0021 context ladder did not widen as the attempts climbed"


def test_the_ceiling_bounds_the_absolute_rung_not_this_invocations_own_count() -> None:
    """A resume handed the LAST rung has one rung left, not a fresh ladder.

    Why it matters: the ceiling exists so a repo gets three chances in total. Counting it per
    invocation would give a repo that crashed twice nine — three of them past the top of the
    declared ladder, each an identical `LLM_ESCALATION` re-run at HEAVY-tier prices.
    """
    LadderSpy.reset()
    LadderSpy.OBSERVED.clear()
    LadderSpy.FAILURE = WorkerError(failure_class=FailureClass.BUILD_ERROR, retryable=True)
    worker = LadderSpy()

    async def drive() -> object:
        return await worker.execute(
            make_ctx(loop_now(), db=FakeDb(), attempt=3), Units(units=["u"]), max_attempts=3
        )

    execution = asyncio.run(drive())
    assert _rungs_observed(worker) == [
        (3, TransformTier.LLM_ESCALATION, ContextPolicy.EVIDENCE_PLUS_REJECTED_APPROACHES)
    ]
    assert execution.attempts == 1, "the resume started a whole new three-rung ladder"
    assert execution.status is RepoStatus.REQUIRES_HUMAN_INTERVENTION


def test_the_attempt_ceiling_is_the_owning_tasks_number_not_a_field_constraint() -> None:
    """A run configured with a longer ladder is allowed to record more attempts.

    Why it matters: `le=MAX_ATTEMPTS` on the field would make §9's per-run configurable ladder a
    lie, and a LOWERED constant would make historical rows unloadable. The ceiling travels with
    the row as `max_attempts`, so the record stays self-describing.
    """
    FailingWorker.reset()
    FailingWorker.FAILURE = WorkerError(
        failure_class=FailureClass.BUILD_ERROR, retryable=True, exit_code=1
    )

    class LongLadder(FailingWorker):
        name: ClassVar[str] = "long-ladder"
        retries: ClassVar[int] = 5

    worker = LongLadder()

    async def drive(ceiling: int) -> object:
        ctx = make_ctx(loop_now(), db=FakeDb())
        return await worker.execute(ctx, Units(units=["u"]), max_attempts=ceiling)

    five = asyncio.run(drive(5))
    assert five.attempts == 5 and five.max_attempts == 5
    assert five.tiers[-1] is TIER_LADDER[-1], "past the last rung, the TOP rung repeats"

    two = asyncio.run(drive(2))
    assert two.attempts == 2, "a task may lower its own ceiling"


# =======================================================================================
# (7) preconditions cannot be skipped, and singletons carry no state
# =======================================================================================


def _discovered_workers() -> list[type[BaseWorker[WorkerInput, WorkerOutput]]]:
    found: list[type[BaseWorker[WorkerInput, WorkerOutput]]] = []
    for module_info in pkgutil.iter_modules(workers_pkg.__path__):
        module = import_module(f"{workers_pkg.__name__}.{module_info.name}")
        for obj in vars(module).values():
            if (
                isinstance(obj, type)
                and issubclass(obj, BaseWorker)
                and obj is not BaseWorker
                and obj.__module__ == module.__name__
            ):
                found.append(obj)
    return found


def test_no_concrete_worker_can_silently_skip_its_preconditions() -> None:
    """Every worker in the package either OVERRIDES `preconditions_hold` or stays abstract.

    Why it matters: the old default `return True` admitted every re-entry unconditionally, which
    is precisely the blind replay the method exists to forbid — `relocate.py` inheriting it
    re-runs a path rename over an already-renamed tree and produces `java/java/com/x`. Making it
    abstract turns that from a silent inheritance into an un-instantiable class.
    """
    discovered = _discovered_workers()
    assert discovered, "the workers package declares no worker classes at all"

    silent = [cls.__name__ for cls in discovered if not implements_preconditions(cls)]
    assert silent == [], f"these workers inherit re-entry without claiming it: {silent}"

    for cls in discovered:
        if inspect.isabstract(cls):
            with pytest.raises(TypeError):
                cls()  # abstract is only a guarantee if it is un-instantiable


def test_there_is_exactly_one_way_for_a_worker_to_reach_a_model() -> None:
    """No worker takes a `ModelClient` by constructor, and the structural probe is gone.

    Why it matters: two authorities for one collaborator is a bug class this codebase has already
    paid for twice (the attempt counter, the transient-retry budget), and the loser is always the
    authority the caller forgot. Here the forgotten one was the constructor: the registry
    instantiates workers with NO arguments, so `ClassifyWorker(client)` was a client no production
    path could ever supply, and `classify` reported `BACKEND_UNAVAILABLE` on healthy runs.
    `rewrite` grew the other half of the same defect — a `model_client_of()` probe that asked
    whether `ctx.llm` happened to be a client and reported a wiring failure when it was the router
    §7.1 promised. `ctx.llm` IS the client now, so both spellings must be unrepresentable rather
    than merely unused: a constructor argument nobody passes is a rung nobody can fire.
    """
    discovered = _discovered_workers()
    assert discovered, "the workers package declares no worker classes at all"

    offenders: list[str] = []
    for cls in discovered:
        for name, parameter in inspect.signature(cls).parameters.items():
            annotation = str(parameter.annotation)
            if name in {"client", "model"} or "ModelClient" in annotation:
                offenders.append(f"{cls.__name__}.__init__({name})")
    assert offenders == [], (
        f"these workers take a model client by constructor as well as on ctx.llm: {offenders}"
    )

    assert not hasattr(rewrite_mod, "model_client_of"), (
        "the structural 'is ctx.llm really a client?' probe outlived the defect it worked around"
    )
    assert "model_client_of" not in rewrite_mod.__all__


def test_registry_singletons_carry_no_instance_state() -> None:
    """A worker/adapter instance with attributes leaks repo A's data into repo B (§7.2).

    Why it matters: registry values are singletons shared across the whole TaskGroup fan-out, so
    a lockfile cached on `self` is not a micro-optimisation, it is cross-repo contamination.
    """
    assert_stateless(UnitWorker())  # ClassVars only: fine

    class Leaky(UnitWorker):
        name: ClassVar[str] = "leaky"

        def __init__(self) -> None:
            self.parsed_lockfile = {"acme": "1.0"}

    with pytest.raises(TypeError, match="instance state"):
        assert_stateless(Leaky())


# =======================================================================================
# (8) TokenUsage through the API it actually has
# =======================================================================================


def test_token_usage_is_summed_through_the_fields_it_actually_has() -> None:
    """The regression guard for the `AttributeError` on every worker's first attempt.

    Why it matters: `execute()` called `TokenUsage.merged()` and read `usage.total_tokens`, and
    neither exists on the §5.4 model — so the ladder raised before it ever ran a second rung. The
    model is correct and mypy-clean; the CALL SITE was wrong, so the sum lives here.
    """
    assert not hasattr(TokenUsage, "merged"), "the model must not grow methods to fit a call site"
    assert "total_tokens" not in TokenUsage.model_fields, "a stored total is a second truth"

    a = TokenUsage(role="repair", input_tokens=100, output_tokens=20, cost_usd=0.5)
    b = TokenUsage(role="escalate", input_tokens=7, cache_read_tokens=3, cost_usd=0.25)
    summed = accumulate(a, b)

    assert summed.input_tokens == 107
    assert summed.output_tokens == 20
    assert summed.cache_read_tokens == 3
    assert summed.cost_usd == pytest.approx(0.75)
    assert summed.role == "", "the sum belongs to no single dispatch, so it claims none"
    assert total_tokens(summed) == 130, "cached reads are billed, so they are counted"


def test_the_ladder_accumulates_usage_across_attempts() -> None:
    """End-to-end: three attempts, three usages, one total — the path that used to raise."""
    FailingWorker.reset()
    FailingWorker.FAILURE = WorkerError(
        failure_class=FailureClass.BUILD_ERROR, retryable=True, exit_code=1
    )
    worker = FailingWorker()

    async def drive() -> object:
        return await worker.execute(make_ctx(loop_now(), db=FakeDb()), Units(units=["u"]))

    execution = asyncio.run(drive())
    assert execution.attempts == 3
    assert total_tokens(execution.usage) == 45  # 3 × (10 + 5)
    assert execution.usage.cost_usd == pytest.approx(0.03)


def test_a_spent_budget_stops_the_ladder_fail_closed() -> None:
    """The remaining-budget ceiling ends the ladder rather than buying the next rung.

    Why it matters: §11.2 is fail-closed. A ceiling observed only after it is overshot is not a
    ceiling, and the check reads `TokenUsage`'s real fields — the second half of the same bug.
    """
    FailingWorker.reset()
    FailingWorker.FAILURE = WorkerError(
        failure_class=FailureClass.BUILD_ERROR, retryable=True, exit_code=1
    )
    worker = FailingWorker()

    async def drive() -> object:
        ctx = make_ctx(loop_now(), db=FakeDb(), tokens_left=10)  # one attempt costs 15
        return await worker.execute(ctx, Units(units=["u"]))

    execution = asyncio.run(drive())
    assert execution.attempts == 1, "the ladder stopped at the ceiling, not at its length"
    assert execution.status is RepoStatus.REQUIRES_HUMAN_INTERVENTION
    assert execution.failure_class is FailureClass.BUDGET_EXHAUSTED


# =======================================================================================
# (9) checkpoint schema versioning
# =======================================================================================


def test_a_checkpoint_from_an_older_shape_invalidates_rather_than_half_loading() -> None:
    """Equality, not `>=`: a stale payload is refused instead of partially populating a plan.

    Why it matters: a field added with a default would let resume proceed on a plan half-filled
    from stale data — the quiet failure. Invalidating re-runs the phase from `phases.base_ref`,
    which is loud, cheap and correct.
    """

    class PlanV1(WorkerOutput):
        targets: list[str] = Field(default_factory=list)

    class PlanV2(WorkerOutput):
        schema_version: ClassVar[int] = 2
        targets: list[str] = Field(default_factory=list)

    persisted = PlanV1(targets=["//libs/acme:commons"]).model_dump()
    assert persisted["written_schema_version"] == 1, "the version is stamped on the way out"

    assert PlanV1.checkpoint_is_current(persisted) is True
    assert PlanV2.checkpoint_is_current(persisted) is False, "an older shape must invalidate"
    assert PlanV1.checkpoint_is_current(PlanV2().model_dump()) is False, "a newer one too"


def test_idempotency_key_is_a_function_of_the_request_only() -> None:
    """Same question, same key — regardless of attempt number or how often it is asked.

    Why it matters: the runner skips any invocation whose key already appears as a committed
    `Fleet-Patch-Id`, and THAT skip is what makes a partial re-run free. A key that varied with
    the attempt would make every retry look like new work.
    """
    worker = UnitWorker()

    async def keys() -> tuple[str, str, str]:
        first = make_ctx(loop_now())
        second = make_ctx(loop_now())
        second.attempt = 3
        second.deadline = first.deadline + 1000
        payload = Units(units=["a", "b"])
        return (
            worker.idempotency_key(first, payload),
            worker.idempotency_key(second, payload),
            worker.idempotency_key(first, Units(units=["a", "c"])),
        )

    same_a, same_b, different = asyncio.run(keys())
    assert same_a == same_b, "attempt and deadline are not part of WHAT was asked"
    assert same_a != different, "a different payload is a different question"
