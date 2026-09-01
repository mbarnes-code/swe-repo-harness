"""`PhaseRunner`: TaskGroup fan-out, fenced state transitions, heartbeats (SPEC §11.1, §11.5).

One `asyncio.TaskGroup` per phase per wave, and **per-repo tasks are isolated from each other**.
Every non-`BaseException` escaping the worker boundary is caught here, recorded as
`FailureClass.UNKNOWN` with `last_error` on the failing repo, and **does not cancel a sibling**
(§11.1). This is the module's single most important property and it is easy to get backwards:
wave-wide sibling cancellation turns one latent bug into a fleet stop that discards paid-for
in-flight LLM and container work, once per bug class, on a multi-day run. TaskGroup-wide
cancellation is reserved for the declared halt states, which are properties of the RUN rather
than of a repo: budget exhaustion (§11.2), the host memory ceiling and disk exhaustion (§11.3),
and `TierUnavailable` (§11.8). Those raise `RunHalted` out of a child, which is the only
exception this runner lets reach the group.

The runner is the **single writer** (§11.5): workers return a `WorkerResult` and the runner
persists it, always under the `lease_fence` the worker's `WorkerContext` was issued with. A
result arriving under a stale fence — the reaper reclaimed the lease mid-flight — is
**discarded, never merged**, and nothing is written, billed, or advanced: the phase is left for
whoever owns it now.

The rung loop is deliberately driven from here rather than left inside
`BaseWorker.execute`'s in-process ladder: each rung is dispatched with `max_attempts=1` so the
outcome of every rung reaches SQLite before the next one starts. An in-memory ladder loses the
whole escalation history to the `SIGKILL` it exists to survive. Which rung comes next is
`RetryPolicy.decide`'s judgement, never re-implemented here.

Three consequences of that, each of which was a defect before it was code:

* **`phases.attempts` is the single authority for "what rung am I on".** The dispatched
  `WorkerContext.attempt` is `phases.attempts + 1` read off disk, and `BaseWorker.execute`
  *derives* the tier and the ADR-0021 `ContextPolicy` from that number instead of counting its
  own. While it counted its own, one-rung-per-dispatch meant every dispatch was attempt 1 at
  `DETERMINISTIC`: the ladder recorded here climbed 1 → 2 → 3 and the worker re-ran the
  deterministic rung three times, so escalation never actually reached an LLM.
* **The §11.8 transient budget is `RetryPolicy`'s alone.** `RETRY_TRANSIENT` re-runs the same
  rung under the same lease and writes no attempt at all (ADR-0014). The worker no longer has a
  budget of its own, because two nested budgets multiply instead of agreeing.
* **`preconditions_hold` is consulted before every re-entry, and this is the only thing that
  consults it.** §7.1 made it abstract so that no worker could inherit a defaulted `return True`
  — the blind replay that turns a re-run `relocate` into `java/java/com/x` — and all eleven
  workers implement it, but nothing called it: the guard was inert for its whole existence and
  every phase's "re-running completed work is a no-op" rested on whatever each worker happened
  to do internally. `_re_entry` now asks, and reads the answer the way the contract writes it:
  `True` admits re-entry for `remaining_units` alone, `False` discards the checkpoint and re-runs
  the phase whole from its anchor. `False` is not a skip in either direction — every worker
  returns it when there is nothing to resume, so a driver that skipped on `False` would skip the
  entire fleet and call it success.
* **An attempt is charged iff the ladder actually advanced.** `complete_phase` increments
  unconditionally, which is right for `ok`, `partial`, `ADVANCE_LADDER` and the exhausting
  failure, and wrong for a `TERMINATE` that spent no rung — charging that one would let a later
  `fleet resume` skip a rung the repo never ran. The test is `decision.state.attempts` against
  the state that went in, which is the same authority everything else here reads.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC
from enum import StrEnum
from typing import TYPE_CHECKING, Final, Protocol

from pydantic import Field

from fleet.models.enums import FailureClass, Phase, RepoStatus
from fleet.models.tasks import MAX_ATTEMPTS
from fleet.obs.redact import redact_text
from fleet.orchestrator.budgets import (
    RUN_BUDGET_EXIT_CODE,
    ZERO_COST,
    CostEstimate,
    LedgerBreach,
    SpendScope,
)
from fleet.orchestrator.retry import LadderState, RetryAction, RetryPolicy
from fleet.orchestrator.scheduler import Admission, WaveScheduler, WaveState
from fleet.state import checkpoints
from fleet.state.repository import LeaseStolenError
from fleet.workers.base import (
    BaseWorker,
    WorkerContext,
    WorkerError,
    WorkerExecution,
    WorkerInput,
    WorkerOutput,
    WorkerResult,
    error_from_exception,
)

if TYPE_CHECKING:
    import aiosqlite

    from fleet.orchestrator.context import RunContext

__all__ = [
    "HaltReason",
    "PayloadFactory",
    "PhaseCheckpoint",
    "PhaseRunner",
    "PreDispatchHook",
    "ReEntry",
    "RepoOutcome",
    "ResultSink",
    "RunHalted",
    "WaveReport",
]

#: §3.4: a per-wave wall-clock breach is the same fail-closed path as a budget breach. The
#: breached wave is left `PARTIAL`, its withheld members `PENDING` with no attempt consumed —
#: but NOTHING RE-ADMITS THEM TODAY. This comment used to say `fleet resume` does; it does not.
#: It used to give the reason as "step 8 has no implementation"; `f6a2e4e` (ADR-0080) wired step
#: 8 and deleted `ResumeIncompleteError`, so `fleet resume` DOES continue now — and it still does
#: not re-admit this wave. The surviving reason is the second one, unchanged and sufficient on
#: its own: `waves.wave_started_at` is stamped once by `begin_wave`'s `COALESCE` and never
#: cleared, so any later scheduler over that wave — in this phase or any other, in this run or a
#: later one, including one a step-8 continuation composes — re-reads the same breach.
#: ADR-0080 records that step 8 does not touch `WaveScheduler` at all.
#: `docs/SPEC.md` §3.4 USED TO
#: state re-admission as INTENT against its own budget-table row; `f54dac8` moved the prose half,
#: so the SPEC now agrees with the paragraph above and that question is decided. D82's OTHER half
#: is still open and is a DIFFERENT question: `waves` carries no phase column, so one phase's
#: breach withholds every later phase of that wave too, and nothing in the tree says whether that
#: sharing is intended. Recorded as D82/D83 in `docs/INTEGRATION_HONESTY.md`.
WAVE_WALLCLOCK_EXIT_CODE: Final = 4
#: §11.3 / §11.8. Named here for the same reason the ledger names its two: `fleet.cli.ExitCode`
#: stops at 7 today, and inventing members into another module's enum from here is worse.
HOST_MEMORY_EXIT_CODE: Final = 5
TIER_UNAVAILABLE_EXIT_CODE: Final = 8
DISK_EXIT_CODE: Final = 9


class HaltReason(StrEnum):
    """The declared halt states (§11.1). Everything NOT on this list is a repo-scoped failure
    that the fleet continues past — which is the whole content of per-repo isolation."""

    RUN_BUDGET = "RUN_BUDGET"
    WAVE_BUDGET = "WAVE_BUDGET"
    WAVE_WALLCLOCK = "WAVE_WALLCLOCK"
    """§3.4's per-wave wall clock. The ONE member no repo task ever raises: it is a property of
    the wave, discovered by `admit`/`may_admit` rather than by a worker, so `run_wave`
    CONSTRUCTS its `RunHalted` instead of catching one. It is on this list because §3.4 calls a
    breach "the same fail-closed path as a budget breach" and because the alternative — leaving
    `WaveReport.halt` `None` under a non-zero exit — is what shipped the operator the literal
    string `'None'` (D83)."""
    HOST_MEMORY = "HOST_MEMORY"
    DISK = "DISK"
    TIER_UNAVAILABLE = "TIER_UNAVAILABLE"


_EXIT_CODES: Final[Mapping[HaltReason, int]] = {
    HaltReason.RUN_BUDGET: RUN_BUDGET_EXIT_CODE,
    HaltReason.WAVE_BUDGET: 10,
    HaltReason.WAVE_WALLCLOCK: WAVE_WALLCLOCK_EXIT_CODE,
    HaltReason.HOST_MEMORY: HOST_MEMORY_EXIT_CODE,
    HaltReason.DISK: DISK_EXIT_CODE,
    HaltReason.TIER_UNAVAILABLE: TIER_UNAVAILABLE_EXIT_CODE,
}


class RunHalted(RuntimeError):
    """A declared halt state. The ONLY exception a repo task lets reach the `TaskGroup`.

    Carrying `exit_code` on the exception is what keeps the halt path honest: every halt state
    §11.1 names has a distinct process exit, and a caller cannot accidentally collapse them into
    "something went wrong" (CLAUDE.md Rule 11).
    """

    def __init__(self, reason: HaltReason, detail: str, *, exit_code: int | None = None) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail
        self.exit_code = _EXIT_CODES[reason] if exit_code is None else exit_code


class PhaseCheckpoint(WorkerOutput):
    """The §11.5 partial-work checkpoint: what landed, and what is still owed.

    Persisted for every `partial` result and consulted on re-entry, which is what makes a
    `partial` worth persisting rather than worth discarding: a rewriter that committed 40 of 60
    units must resume at unit 41, not replay all 60 against an already-rewritten tree and read
    the resulting no-op patches as `RULE_MISS`.

    `completed_units` is the UNION across re-entries, not just the last invocation's: the record
    of what landed has to survive the attempt that landed it.
    """

    completed_units: list[str] = Field(default_factory=list)
    remaining_units: list[str] = Field(default_factory=list)
    attempt: int = 0


class PayloadFactory[I: WorkerInput](Protocol):
    """Builds one dispatch's `WorkerInput`. Injected, because what a payload contains is the
    phase's business and not the driver's (Guardrail 3).

    `remaining_units` is `None` when there is no usable checkpoint and the phase runs whole.
    """

    async def __call__(
        self,
        *,
        repo_id: str,
        phase: Phase,
        attempt: int,
        remaining_units: Sequence[str] | None,
    ) -> I: ...


class PreDispatchHook[I: WorkerInput](Protocol):
    """Runs once per dispatch, after the payload is final and before the worker executes.

    D89 Phase 2 Task A (ADR-0102): the one optional collaborator that lets a phase give its
    coarse `tasks` row (D89 Phase 1 / ADR-0101) a real claim lifecycle, without `PhaseRunner`
    itself becoming aware of `tasks`, `kind`, or any other phase-specific concept — mirroring
    `ResultSink`'s injection shape so every phase that does not need this stays untouched.
    `PhaseRunner` still does not persist run state itself (§11.5): a hook implementation is
    handed `self.ctx.repository`/`self._writer` at ITS OWN construction site (`cli.py`'s
    composition root), never through this call, so the single-writer boundary is unchanged.
    """

    async def __call__(self, *, repo_id: str, phase: Phase, payload: I) -> None: ...


class ResultSink[O: WorkerOutput](Protocol):
    """Persists ONE dispatch's landed evidence, under the fence that produced it.

    A worker returns rows and writes none of them (§11.5); `WaveReport` carries only
    `RepoOutcome`, which is status and not evidence. Without this hook a phase's output — the
    `repos`/`manifests`/`coordinates`/`symbols` rows Phase 1 exists to produce — has nowhere to
    go but the caller's memory, and a process killed mid-wave then leaves repos whose phase says
    `SUCCEEDED` and whose evidence was never written: state that no re-run will ever repair,
    because a `SUCCEEDED` phase is never re-admitted.

    Injected, and called on the runner's own task **before** the terminal write, so the evidence
    is durable by the time the status that vouches for it is. Still the single writer: the sink
    the CLI supplies submits through the same `StateWriter` this runner holds. `fence` is passed
    so an implementation can carry it into its own statements; the runner has already established
    that the fence was current when the worker returned.
    """

    async def __call__(
        self, *, repo_id: str, phase: Phase, fence: int, result: WorkerResult[O]
    ) -> None: ...


class ReEntry(StrEnum):
    """What `BaseWorker.preconditions_hold` said about re-entering this phase (§7.1).

    The polarity is the whole point and it is not symmetric, so it is named rather than left as a
    bare bool at a call site: `True` means "the checkpoint describes the tree in front of me,
    re-enter for `remaining_units` alone", and `False` means "it does not — run the phase whole
    from its anchor". **Neither verdict ever means "skip the work".** A driver that read `False`
    as a skip would skip every fresh repo in the fleet (`interrogate`, `symbolindex`, `rewrite`
    and `clone` all return `False` when there is nothing to resume) and report success for work
    that never ran.
    """

    FRESH = "FRESH"
    """No checkpoint: there is nothing to replay, so there is nothing to guard. The phase runs
    whole and `preconditions_hold` is not consulted — its own contract says "checked on resume
    before re-entry", and a fresh dispatch is neither."""

    RESUME = "RESUME"
    """Preconditions hold and units are still owed: dispatch `remaining_units` only. The landed
    `completed_units` are consumed by the payload, never replayed."""

    COMPLETE = "COMPLETE"
    """Preconditions hold and the checkpoint owes nothing. Re-running completed work is exactly
    the blind replay §7.1 forbids, so the worker is NOT dispatched and the phase is completed.
    Safe only because the checkpoint is this run's own record: the evidence for those units was
    written by the dispatch that landed them (see `ResultSink`), so nothing is lost."""

    REJECTED = "REJECTED"
    """Preconditions do NOT hold: the checkpoint does not describe the tree, so it is discarded
    and the phase re-runs whole from `phases.base_ref`. Loud — a warning log and
    `RepoOutcome.checkpoint_rejected` — because the alternative reading of `False` ("already
    done, skip") would silently abandon the work (Rule 11)."""


@dataclass(frozen=True, slots=True)
class RepoOutcome:
    """What one `(repo, phase)` did in this wave. `discarded` is the fence verdict."""

    repo_id: str
    status: RepoStatus
    attempts: int = 0
    transient_retries: int = 0
    dispatches: int = 0
    failure_class: FailureClass | None = None
    last_error: str | None = None
    discarded: bool = False
    """The lease was reclaimed mid-flight: the result was thrown away, nothing was written and
    nothing was billed. The phase is re-dispatchable by its new owner."""
    admitted: bool = True
    checkpointed: bool = False
    checkpoint_rejected: bool = False
    """`preconditions_hold` refused the checkpoint: it was discarded and the phase re-ran whole.
    Surfaced on the outcome rather than only in a log, because a rejection means landed work was
    thrown away and that must be visible to whoever reads the wave."""
    checkpoint_unreadable: bool = False
    """`checkpoints.load` refused the STORED checkpoint before the worker ever saw it — a stale
    `schema_version`, a different model class, a truncated envelope, or a payload the model would
    not validate. Distinct from `checkpoint_rejected`, which is the worker's verdict on a
    checkpoint that loaded fine; these two answer different questions and a reader that cannot
    tell them apart cannot tell a schema bump from a moved tree. Surfaced here for the same
    reason as its sibling: landed work was thrown away."""
    skipped_complete: bool = False
    """The checkpoint owed nothing and its preconditions held, so no rung was dispatched."""


@dataclass(frozen=True, slots=True)
class _Dispatched[O: WorkerOutput]:
    """One turn of `_drive`'s loop, as `_dispatch` left it.

    A record rather than a widening tuple because `re_entry` has to reach `_drive`: a REJECTED
    checkpoint must not be unioned into the next one by `_save_checkpoint`, or the units the
    rejected checkpoint claimed would come back as "already done" on the very next partial —
    the replay this whole mechanism exists to prevent, laundered through the state store.
    """

    execution: WorkerExecution[O] | None = None
    breach: LedgerBreach | None = None
    re_entry: ReEntry = ReEntry.FRESH


@dataclass(frozen=True, slots=True)
class WaveReport:
    """One wave's result. `state` is `CLOSED` or `PARTIAL`; never a bool."""

    wave_index: int
    phase: Phase
    admission: Admission
    outcomes: Mapping[str, RepoOutcome]
    state: WaveState
    withheld: tuple[str, ...] = ()
    halt: RunHalted | None = None

    @property
    def exit_code(self) -> int | None:
        """The process exit this wave implies, or `None` to continue. Exit 4 is a RESUMABLE
        state, not a dead end (§3.4): the `PARTIAL` wave's withheld members kept their attempts.
        """
        if self.halt is not None:
            return self.halt.exit_code
        if self.withheld and self.state is WaveState.PARTIAL:
            return WAVE_WALLCLOCK_EXIT_CODE
        return None


def _flatten(group: BaseExceptionGroup[RunHalted]) -> RunHalted:
    """The first `RunHalted` in a (possibly nested) group. One halt is enough to stop a run and
    the rest are its consequences, so the first is reported and the others are not invented into
    a list a caller would have to reconcile."""
    for exc in group.exceptions:
        if isinstance(exc, RunHalted):
            return exc
        if isinstance(exc, BaseExceptionGroup):
            return _flatten(exc)
    raise AssertionError("except* RunHalted matched a group holding none")  # pragma: no cover


class PhaseRunner[I: WorkerInput, O: WorkerOutput]:
    """Runs one phase over one wave. The only writer of run state (§11.5)."""

    def __init__(
        self,
        ctx: RunContext,
        worker: BaseWorker[I, O],
        scheduler: WaveScheduler,
        *,
        payloads: PayloadFactory[I],
        policy: RetryPolicy | None = None,
        estimate: Callable[[str], CostEstimate] = lambda _repo_id: ZERO_COST,
        resource_guard: Callable[[], HaltReason | None] = lambda: None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        sink: ResultSink[O] | None = None,
        pre_dispatch: PreDispatchHook[I] | None = None,
    ) -> None:
        self.ctx = ctx
        self.worker = worker
        self.scheduler = scheduler
        self.phase = worker.phase
        self._payloads = payloads
        self._sink = sink
        self._pre_dispatch = pre_dispatch
        self._policy = policy or RetryPolicy()
        self._estimate = estimate
        #: §11.3's RSS and disk ceilings are host facts, not repo facts, so they are polled
        #: rather than raised by a worker — injected so a test can trip one without a full host.
        self._guard = resource_guard
        self._sleep = sleep
        self._run_id = str(ctx.run_id)

    # ------------------------------------------------------------------ the wave

    async def run_wave(self, wave_index: int) -> WaveReport:
        """Admit the wave and fan out, one `TaskGroup`, one task per repo.

        Admission order is the scheduler's (blast radius, descending). `may_admit` is re-polled
        between admissions so a wall-clock breach stops admitting *inside* the wave; work
        already in flight is paid for and is allowed to drain (§3.4).
        """
        admission = await self.scheduler.admit(wave_index)
        radii = await self.scheduler.store.blast_radii(self._run_id, list(admission.admitted))
        members = len(admission.admitted) + len(admission.blocked) + len(admission.settled)
        outcomes: dict[str, RepoOutcome] = {}
        withheld: list[str] = list(admission.withheld)
        halt: RunHalted | None = None

        try:
            async with asyncio.TaskGroup() as group:
                for position, repo_id in enumerate(admission.admitted):
                    if not await self.scheduler.may_admit(wave_index):
                        withheld.extend(admission.admitted[position:])
                        break
                    scope = SpendScope(
                        repo_id=repo_id,
                        wave_index=wave_index,
                        wave_members=members,
                        blast_radius=radii.get(repo_id, 0),
                    )
                    group.create_task(
                        self._isolated(repo_id, scope, outcomes),
                        name=f"phase{int(self.phase)}-wave{wave_index}-{repo_id}",
                    )
        except* RunHalted as raised:
            halt = _flatten(raised)

        # The wave's last drain, and the retry for anything a per-dispatch drain could not land.
        # Isolated for the same reason as that one, plus a sharper one here: it runs AFTER
        # `halt` has been captured from the TaskGroup, so an exception escaping would discard a
        # real `RunHalted` and make an exit-8 tier outage surface as `UNEXPECTED_ERROR` at
        # `cli.py`. A diagnostics write must not be able to rewrite the run's exit code either.
        await self._drain_llm_findings(None)

        self.ctx.project()
        state = await self.scheduler.wave_state(wave_index)
        if halt is None and withheld and state is WaveState.PARTIAL:
            # A wall-clock breach raises nothing — `admit` simply returns `admitted=()` and
            # `may_admit` withholds the rest — so without this the report carries exit 4 with
            # `halt = None`, and every caller that folds `str(report.halt)` into its payload
            # hands the operator the literal string `'None'` (D83). The exit code was always
            # right; the text carried nothing. Everything named here is a fact the operator
            # cannot otherwise get: which wave, which phase, the ceiling that was spent, the
            # elapsed at the moment the wave stopped admitting, and how many members are still
            # owed. No remedy is named on purpose: §3.4 calls the state resumable, but nothing
            # re-admits a breached wave today (D82, and `scheduler.py`'s module docstring).
            #
            # The elapsed is RE-READ here rather than taken from `admission.elapsed_s`, which
            # `admit` snapshots BEFORE the wave runs. On the mid-wave path — the one this
            # method's own docstring describes, where `may_admit` discovers the breach between
            # admissions — that snapshot is by construction BELOW the ceiling, because `admit`
            # itself did not breach; the message then read "spent its 60s ceiling after 0.0s",
            # which is self-contradictory and reads as a broken harness rather than as a
            # ceiling to raise. `elapsed_s` is the same quantity `breached` compares against,
            # so the number printed is the number the withholding was decided on.
            ceiling = self.scheduler.budgets.wave_max_wallclock_s
            elapsed_s = await self.scheduler.elapsed_s(wave_index)
            withheld_names = ", ".join(sorted(withheld))
            halt = RunHalted(
                HaltReason.WAVE_WALLCLOCK,
                f"wave {wave_index} of phase {int(self.phase)} ({self.phase.name}) spent its "
                f"{ceiling}s wall-clock ceiling after {elapsed_s:.1f}s; "
                f"{len(withheld)} member(s) withheld, still PENDING with no attempt consumed "
                f"[{withheld_names}]",
            )
        return WaveReport(
            wave_index=wave_index,
            phase=self.phase,
            admission=admission,
            outcomes=outcomes,
            state=state,
            withheld=tuple(withheld),
            halt=halt,
        )

    async def _isolated(
        self, repo_id: str, scope: SpendScope, outcomes: dict[str, RepoOutcome]
    ) -> None:
        """THE per-repo isolation boundary (§11.1).

        Only `RunHalted` and `BaseException` (cancellation, `KeyboardInterrupt`) leave this
        method. Anything else is this repo's problem and is recorded as `FailureClass.UNKNOWN`
        with `last_error` — never a silent drop (Rule 11) and never a cancelled sibling.
        """
        try:
            outcomes[repo_id] = await self._drive(repo_id, scope)
        except RunHalted:
            raise
        except Exception as exc:
            error = error_from_exception(exc)
            outcomes[repo_id] = RepoOutcome(
                repo_id=repo_id,
                status=RepoStatus.PENDING,
                failure_class=FailureClass.UNKNOWN,
                last_error=error.stderr_tail or repr(exc),
            )
            self.ctx.log.warning(
                "repo_task_escaped",
                repo_id=repo_id,
                phase=int(self.phase),
                exception_type=error.exception_type,
                error=error.stderr_tail,
            )

    # ------------------------------------------------------------------ one repo

    async def _drive(self, repo_id: str, scope: SpendScope) -> RepoOutcome:
        """claim → lease → run under a deadline → heartbeat → fenced write → advance or retry."""
        row = await self.ctx.repository.get_phase(self._run_id, repo_id, self.phase)
        ladder = LadderState(
            attempts=0 if row is None else row.attempts,
            max_attempts=MAX_ATTEMPTS if row is None else row.max_attempts,
        )
        outcome = RepoOutcome(
            repo_id=repo_id,
            status=RepoStatus.PENDING if row is None else row.status,
            attempts=ladder.attempts,
        )
        fence: int | None = None

        while True:
            reason = self._guard()
            if reason is not None:
                raise RunHalted(reason, f"resource guard tripped before {repo_id} was dispatched")

            if fence is None:
                fence = await self.ctx.repository.acquire_phase_lease(
                    self._run_id,
                    repo_id,
                    self.phase,
                    owner=self.ctx.lease_owner,
                    now=self.ctx.clock(),
                    lease_ttl_s=self.ctx.lease_ttl_s,
                )
                if fence is None:
                    # Not PENDING: another owner won the CAS, or the phase is already terminal.
                    # Either way this task has no claim and must not touch the worktree.
                    return replace(outcome, status=await self._status(repo_id), admitted=False)

            loaded = await self._load_checkpoint(repo_id)
            if loaded.discarded_stored_work:
                outcome = replace(outcome, checkpoint_unreadable=True)
            checkpoint = loaded.payload
            cancel = asyncio.Event()
            dispatched = await self._dispatch(
                repo_id,
                scope=scope,
                fence=fence,
                ladder=ladder,
                cancel=cancel,
                checkpoint=checkpoint,
            )
            # Whatever the LLM layer computed during that dispatch is written NOW, before the
            # dispatch's own outcome is acted on. `on_drift`/`on_failover` are synchronous
            # callbacks on the client's hot path, so they can only buffer; this is the await
            # that makes them durable, and it sits here — after every dispatch, on every path,
            # success included — because a `CapabilityDrift` is emitted whether or not the call
            # SUCCEEDED (§13 row 37: a local server that silently drops guided JSON is still a
            # finding, and would otherwise never be flushed by a green wave).
            await self._drain_llm_findings(repo_id)

            execution, breach = dispatched.execution, dispatched.breach
            if dispatched.re_entry is ReEntry.REJECTED:
                # The worker refused the checkpoint, so it is gone: the phase re-ran whole and
                # the next `_save_checkpoint` must not resurrect what this one claimed had landed.
                checkpoint = None
                outcome = replace(outcome, checkpoint_rejected=True)
            elif dispatched.re_entry is ReEntry.COMPLETE:
                outcome = replace(outcome, skipped_complete=True)
            outcome = replace(outcome, dispatches=outcome.dispatches + 1)

            if breach is not None:
                return await self._on_breach(repo_id, breach, fence=fence, outcome=outcome)

            if execution is None or execution.fence_stale:
                # §11.5: the lease was reclaimed while the worker ran. DISCARD — not merged, not
                # billed, not advanced. Writing here would interleave two owners on one worktree.
                self.ctx.log.info("result_discarded_stale_fence", repo_id=repo_id, fence=fence)
                return replace(
                    outcome, status=await self._status(repo_id), discarded=True
                )

            result = execution.final
            error = execution.last_error

            if self._sink is not None and result is not None and result.output is not None:
                # Evidence first, status second: a `SUCCEEDED` phase whose rows were never
                # written is unrecoverable, because it is never re-admitted. Deliberately not
                # wrapped: a persistence failure is this repo's failure and belongs in
                # `_isolated`'s hands (Rule 11), not swallowed behind a green phase.
                await self._sink(
                    repo_id=repo_id, phase=self.phase, fence=fence, result=result
                )

            if execution.status is RepoStatus.SUCCEEDED:
                written = await self._complete(repo_id, fence, RepoStatus.SUCCEEDED, None)
                if written is None:
                    return replace(outcome, status=await self._status(repo_id), discarded=True)
                return replace(
                    outcome, status=written, attempts=ladder.attempts + 1
                )

            if result is not None and result.status == "partial":
                await self._save_checkpoint(repo_id, result, checkpoint, ladder.attempts + 1)
                written = await self._complete(
                    repo_id, fence, RepoStatus.PENDING, self._detail(error)
                )
                if written is None:
                    return replace(outcome, status=await self._status(repo_id), discarded=True)
                ladder = replace(ladder, attempts=ladder.attempts + 1)
                outcome = replace(
                    outcome, attempts=ladder.attempts, checkpointed=True, status=written
                )
                if written is not RepoStatus.PENDING:
                    return outcome
                fence = None
                continue

            failure = error or WorkerError(
                failure_class=FailureClass.UNKNOWN,
                retryable=True,
                stderr_tail=f"{self.worker.name} returned no result for {repo_id}",
            )
            decision = self._policy.decide(ladder, failure)
            # Did the ladder actually move? Read off the state the policy returned rather than
            # off `charges_attempt`, which is `action is ADVANCE_LADDER` and therefore `False`
            # for the TERMINATE that fires when the LAST rung fails — a rung that was very much
            # spent. One number, `LadderState.attempts`, answers both questions consistently.
            charged = decision.state.attempts > ladder.attempts
            ladder = decision.state
            outcome = replace(
                outcome,
                failure_class=failure.failure_class,
                last_error=self._detail(failure),
                transient_retries=ladder.transient_retries,
            )
            if not await self._record_diagnostics(repo_id, fence, failure, ladder):
                return replace(outcome, status=await self._status(repo_id), discarded=True)

            if decision.action is RetryAction.RETRY_TRANSIENT:
                # Same rung, same lease, NO attempt charged (ADR-0014). The lease is renewed
                # because the backoff is dead time the reaper would otherwise count against us.
                await self._sleep(decision.delay_s)
                if not await self._renew(repo_id, fence):
                    return replace(outcome, status=await self._status(repo_id), discarded=True)
                continue

            if decision.action is RetryAction.ADVANCE_LADDER:
                written = await self._complete(
                    repo_id, fence, RepoStatus.PENDING, self._detail(failure)
                )
                if written is None:
                    return replace(outcome, status=await self._status(repo_id), discarded=True)
                outcome = replace(outcome, attempts=ladder.attempts, status=written)
                if written is not RepoStatus.PENDING:
                    await self._contain(repo_id, written)
                    return outcome
                fence = None
                continue

            # TERMINATE.
            if failure.failure_class is FailureClass.DISK_EXHAUSTED:
                # §11.3 / §13 row 42: terminal for the RUN, never for the repo. The volume is not
                # this repo's fault and the next repo would meet the identical floor, so letting
                # the fleet continue would burn 249 more repos into REQUIRES_HUMAN_INTERVENTION
                # for one full disk — and would keep writing until the `BEGIN IMMEDIATE` that the
                # checkpoint guarantee depends on is the write that meets ENOSPC. Halting here
                # means the checkpoint is written while there is still room to write it.
                raise RunHalted(
                    HaltReason.DISK,
                    f"{repo_id}: {self._detail(failure)}",
                )
            if failure.failure_class is FailureClass.BACKEND_UNAVAILABLE:
                # §11.8: terminal for the RUN, never for the repo. The lease is left to expire
                # and the row stays for `fleet resume`, so no attempt is charged to a repo that
                # did nothing wrong.
                #
                # §13 row 40's `BackendUnavailable` finding is written FIRST, and deliberately
                # not after: `RunHalted` unwinds the TaskGroup, and an exit-8 whose cause exists
                # only in a log line leaves the operator reconstructing which tier died and
                # which targets were tried from a stderr tail. `failure.stderr_tail` is
                # `TierUnavailable`'s own message (client.py:151-155) and already names both.
                #
                # The finding records the OBSERVATION and refuses to assert a cause — see
                # `LlmFindingSink.record_backend_unavailable`. `decision.reason` is deliberately
                # NOT copied into it: `retry.py:196` (and the halt string just below) both say
                # "every target for the tier is DOWN", and `DOWN` is a `BackendHealth` state that
                # exists nowhere in `src/` and that §13 row 43 forbids inferring from throttling
                # alone — which is exactly what a rate-limited account produces here, because
                # `client.py:532` retires a target without inspecting `TransportError.trigger`.
                # The message below is left as it stands: correcting that vocabulary spans
                # `retry.py`, `enums.py:289` and the SPEC, and building the health state machine
                # that would make it true is §13 row 43's own (large) ticket. A log line scrolls
                # away; a finding is what a human reads afterwards, so only the finding is fixed
                # here — it must not carry the claim.
                #
                # Isolated for the same reason as `_drain_llm_findings`, and here the stake is
                # higher than a wrong repo verdict: an exception escaping this line propagates
                # out of `_drive` into `_isolated`'s `except Exception`, so the `RunHalted`
                # below is NEVER RAISED and the tier outage vanishes — the run continues into a
                # dead tier and exits 0. The finding is best-effort; the halt is not.
                try:
                    await self.ctx.llm_findings.record_backend_unavailable(
                        repo_id=repo_id,
                        phase=self.phase,
                        observed=self._detail(failure) or str(failure.failure_class),
                        tier=failure.tier,
                    )
                except Exception as exc:
                    self.ctx.log.error(  # noqa: TRY400 - §11.4: no formatted traceback
                        "backend_unavailable_finding_not_written",
                        repo_id=repo_id,
                        phase=int(self.phase),
                        exception_type=f"{type(exc).__module__}.{type(exc).__qualname__}",
                        error=str(exc),
                    )
                raise RunHalted(
                    HaltReason.TIER_UNAVAILABLE,
                    f"every backend target for {repo_id}'s tier is DOWN: {decision.reason}",
                )
            terminal = decision.terminal_status or RepoStatus.REQUIRES_HUMAN_INTERVENTION
            written = await (
                self._complete(repo_id, fence, terminal, self._detail(failure))
                if charged
                else self._terminate_uncharged(
                    repo_id, fence, terminal, self._detail(failure)
                )
            )
            if written is None:
                return replace(outcome, status=await self._status(repo_id), discarded=True)
            await self._contain(repo_id, written)
            return replace(outcome, attempts=ladder.attempts, status=written)

    # ------------------------------------------------------------------ dispatch

    async def _dispatch(
        self,
        repo_id: str,
        *,
        scope: SpendScope,
        fence: int,
        ladder: LadderState,
        cancel: asyncio.Event,
        checkpoint: PhaseCheckpoint | None,
    ) -> _Dispatched[O]:
        """One rung, under a deadline, a live heartbeat and a ledger reservation.

        `max_attempts=1`: the ladder is driven from `_drive` so every rung's outcome is durable
        before the next begins. The heartbeat runs for exactly this rung — a renewal task that
        outlived its dispatch would keep a reclaimed lease looking alive.

        `attempt` is `phases.attempts + 1` — the position read off disk — and it is what tells
        the worker WHICH rung to run: `BaseWorker.execute` derives the tier and the ADR-0021
        `ContextPolicy` from `ctx.attempt`. `tier=` below is the same value by the same function
        (`TIER_LADDER[attempt - 1]`), passed so the context is coherent before `execute` refines
        it per rung; it is not an independent judgement and cannot disagree.

        **`preconditions_hold` is consulted HERE**, between the payload and `execute` — the only
        point where both the typed payload and the `WorkerContext` the worker would receive
        exist, and still inside the heartbeat window a filesystem-walking precondition needs. It
        is not consulted in `execute()`: `execute` is the retry LADDER, it re-runs the same rung
        under the same payload, and a per-rung re-check would re-answer a question about state
        the driver is the only one able to act on.
        """
        attempt = ladder.attempts + 1
        # Stamp the OWNER onto the scope the reservation is minted from. `SpendScope` carries
        # `phase`/`lease_fence` so the durable `reservations` row names who holds the dollars,
        # and the reaper's fence bump is scoped to that owner: an unowned hold is still reaped
        # by expiry, but the reaped worker's next fenced write is not refused, which is the half
        # of the protection that stops two owners writing one worktree (§6, §11.5). Stamped
        # HERE, per dispatch, because `_drive` re-acquires the lease after a rung and the fence
        # of the lease this dispatch actually runs under is the only one worth invalidating.
        scope = replace(scope, phase=self.phase, lease_fence=fence)
        deadline = self.ctx.deadline_for(self.phase)
        heartbeat = asyncio.create_task(
            self._heartbeat(repo_id, fence, cancel), name=f"heartbeat-{repo_id}"
        )
        try:
            async with self.ctx.ledger.dispatch(
                self._estimate(repo_id), scope=scope, deadline=deadline
            ) as (reservation, budget):
                worker_ctx = self.ctx.worker_context(
                    repo_id=repo_id,
                    phase=self.phase,
                    attempt=attempt,
                    lease_fence=fence,
                    cancel=cancel,
                    budget=budget,
                    tier=ladder.tier(attempt),
                    deadline=deadline,
                )
                payload = await self._payloads(
                    repo_id=repo_id,
                    phase=self.phase,
                    attempt=attempt,
                    remaining_units=None if checkpoint is None else checkpoint.remaining_units,
                )
                try:
                    re_entry = await self._re_entry(repo_id, worker_ctx, payload, checkpoint)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # A precondition that RAISES has answered neither way, and guessing either
                    # verdict is how a replay guard becomes a replay (Rule 11). It is this
                    # repo's typed failure and goes to the ladder like any other.
                    return _Dispatched(execution=self._unknown(repo_id, exc))
                if re_entry is ReEntry.COMPLETE:
                    return _Dispatched(
                        execution=self._already_complete(repo_id), re_entry=re_entry
                    )
                if re_entry is ReEntry.REJECTED:
                    payload = await self._payloads(
                        repo_id=repo_id, phase=self.phase, attempt=attempt, remaining_units=None
                    )
                # D89 Phase 2 Task A (ADR-0102): fired on the FINAL payload — after a possible
                # REJECTED rebuild above, never on the COMPLETE early-return, since the worker
                # is not about to run in that case and there is nothing to claim a dispatch for.
                if self._pre_dispatch is not None:
                    try:
                        await self._pre_dispatch(
                            repo_id=repo_id, phase=self.phase, payload=payload
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        # Same shape as `_re_entry`'s guard above: a hook that raises has
                        # answered neither way, the worker never ran, and swallowing it or
                        # letting it escape uncaught are both wrong (Rule 11). It is this
                        # repo's typed failure and goes to the ladder like any other.
                        # `re_entry` is threaded through (not defaulted to FRESH) so a REJECTED
                        # checkpoint decided above is not silently forgotten by `_drive`.
                        return _Dispatched(
                            execution=self._unknown(repo_id, exc), re_entry=re_entry
                        )
                try:
                    execution = await self.worker.execute(worker_ctx, payload, max_attempts=1)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    execution = self._unknown(repo_id, exc)
                usage = execution.usage
                reservation.record(
                    CostEstimate(
                        in_tokens=usage.input_tokens,
                        out_tokens=usage.output_tokens,
                        usd=usage.cost_usd,
                    )
                )
                return _Dispatched(execution=execution, re_entry=re_entry)
        except LedgerBreach as breach:
            # Raised by `reserve()` BEFORE the body ran, so nothing was dispatched and nothing
            # was spent. Which ceiling broke is the exception's TYPE, and `exit_code` says
            # whether it stops the run or only this repo (§11.2).
            return _Dispatched(breach=breach)
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat

    async def _re_entry(
        self,
        repo_id: str,
        worker_ctx: WorkerContext,
        payload: I,
        checkpoint: PhaseCheckpoint | None,
    ) -> ReEntry:
        """Ask the worker whether this dispatch may re-enter its own landed work (§7.1).

        THE call this method's existence depends on: `preconditions_hold` was made abstract so no
        worker could inherit a defaulted `return True` — `relocate` re-running a path rename over
        an already-renamed tree is `java/java/com/x` — and all eleven workers implement it
        honestly, but for its entire existence NOTHING invoked it. The guarantee it encodes,
        "re-running completed work is a no-op", was enforced only by whatever each worker
        happened to do internally, which is precisely the thing a guard is supposed to replace.

        The verdict is read exactly as the method's own contract writes it: `True` admits
        re-entry for `remaining_units` alone, `False` invalidates the checkpoint so the phase
        re-runs from `phases.base_ref`. `False` is NEVER a skip — every worker returns `False`
        when there is nothing to resume, so reading it as one would skip the whole fleet and
        report success.

        The driver does the half of "re-run from the anchor" that is its own: it drops the
        checkpoint and dispatches the phase whole. It does not reset the worktree — git is the
        record and the worker owns its tree — and re-running whole is safe rather than a replay
        because each unit is re-guarded before it is applied (§3.2 step 6.1).
        """
        if checkpoint is None:
            return ReEntry.FRESH
        if not await self.worker.preconditions_hold(worker_ctx, payload):
            self.ctx.log.warning(
                "checkpoint_rejected",
                repo_id=repo_id,
                phase=int(self.phase),
                worker=self.worker.name,
                completed_units=len(checkpoint.completed_units),
                remaining_units=len(checkpoint.remaining_units),
            )
            return ReEntry.REJECTED
        return ReEntry.RESUME if checkpoint.remaining_units else ReEntry.COMPLETE

    def _already_complete(self, repo_id: str) -> WorkerExecution[O]:
        """The checkpoint owes nothing and the worker vouched for it: no rung is dispatched.

        `attempts=0` because nothing was attempted — the rung is not spent, the ladder is not
        advanced by it, and `_complete` writing `SUCCEEDED` is a statement about work that
        already landed under this run's own checkpoint, not about work invented here.
        """
        self.ctx.log.info(
            "phase_already_complete",
            repo_id=repo_id,
            phase=int(self.phase),
            worker=self.worker.name,
        )
        return WorkerExecution[O](
            worker=self.worker.name,
            phase=self.phase,
            repo_id=repo_id,
            status=RepoStatus.SUCCEEDED,
            attempts=0,
            max_attempts=1,
        )

    def _unknown(self, repo_id: str, exc: BaseException) -> WorkerExecution[O]:
        """An exception that escaped `BaseWorker.execute` itself, typed rather than swallowed.

        `error_from_exception` records the qualified exception type and the message, never a
        formatted traceback — a traceback carries locals, and locals carry credentials (§11.4).
        """
        error = error_from_exception(exc)
        self.ctx.log.warning(
            "worker_execute_escaped",
            repo_id=repo_id,
            phase=int(self.phase),
            failure_class=str(error.failure_class),
            exception_type=error.exception_type,
        )
        result: WorkerResult[O] = WorkerResult(status="failed", error=error)
        return WorkerExecution[O](
            worker=self.worker.name,
            phase=self.phase,
            repo_id=repo_id,
            status=RepoStatus.RUNNING,
            attempts=0,
            max_attempts=1,
            results=[result],
        )

    async def _heartbeat(self, repo_id: str, fence: int, cancel: asyncio.Event) -> None:
        """Renew `phases.heartbeat_at` on the worker's behalf until the rung ends.

        On `LeaseStolenError` it sets `ctx.cancel` and stops: the worker must abort WITHOUT
        touching git, because another process owns this worktree now. It does not, and must not,
        write the reclaim — the reaper already did, and the fence is the proof.
        """
        while True:
            # Deliberately the real clock, not the injectable `sleep`: the heartbeat's interval
            # is a liveness promise made to the reaper in another process, and a test that
            # stubbed it out would spin this loop against the database instead of pacing it.
            await asyncio.sleep(self.ctx.heartbeat_interval_s)
            try:
                await self.ctx.repository.renew_phase_lease(
                    self._run_id,
                    repo_id,
                    self.phase,
                    fence=fence,
                    now=self.ctx.clock(),
                    lease_ttl_s=self.ctx.lease_ttl_s,
                )
            except LeaseStolenError:
                self.ctx.log.warning("lease_stolen", repo_id=repo_id, fence=fence)
                cancel.set()
                return

    # ------------------------------------------------------------------ fenced writes

    async def _complete(
        self, repo_id: str, fence: int, status: RepoStatus, last_error: str | None
    ) -> RepoStatus | None:
        """The fenced terminal write. `None` means the fence was stale and nothing was written.

        `complete_phase` increments `attempts` and, on a `PENDING` hand-back, escalates to
        `REQUIRES_HUMAN_INTERVENTION` in the SAME statement when the increment reaches
        `max_attempts` — which is why the ladder cannot run one rung past its ceiling.
        """
        try:
            written = await self.ctx.repository.complete_phase(
                self._run_id,
                repo_id,
                self.phase,
                fence=fence,
                status=status,
                now=self.ctx.clock(),
                last_error=last_error,
            )
        except LeaseStolenError:
            self.ctx.log.info("write_discarded_stale_fence", repo_id=repo_id, fence=fence)
            return None
        self.ctx.project()
        return written

    async def _terminate_uncharged(
        self, repo_id: str, fence: int, status: RepoStatus, last_error: str | None
    ) -> RepoStatus | None:
        """The fenced terminal write for a decision that spent NO rung. `None` means stale fence.

        `complete_phase` does `attempts = attempts + 1` unconditionally, and for every write that
        follows a rung the repo really ran that is exactly right. A non-retryable failure is the
        one case that is not: `RetryPolicy` returns the ladder state UNCHANGED for it (§ADR-0014
        — "a structural failure re-run at a higher tier is the same failure at a higher price",
        so no further rung is issued and none is charged), and incrementing anyway spends a rung
        the repo never used. That is not merely a cosmetic miscount: `phases.attempts` is what a
        `fleet resume` reads to decide where the ladder is, so a human who fixes the structural
        problem and resumes gets rung N+1 — the deterministic rung that would have fixed it is
        silently skipped, and the repo is escalated to an LLM for no reason.

        A statement of its own rather than a flag on `complete_phase`, because the §6 CAS is a
        repository primitive and this is the runner's policy; it is written here for the same
        reason `_record_diagnostics` is, is still the single writer (§11.5), and is still fenced.
        There is no `max_attempts` escalation branch because there is no increment to reach it —
        the status being written is already terminal.

        `last_error` is redacted HERE, at this write boundary (SPEC §11.4, D90): this raw
        `UPDATE` bypasses `complete_phase` entirely, so D88's fix there never covers it — the
        same `error_from_exception`-sourced, unredacted `stderr_tail` that D88 traced can reach
        this terminal write for a non-retryable failure, and this is the LAST write the row
        gets.
        """
        params = (
            str(status),
            None if last_error is None else redact_text(last_error),
            # UTC, and fixed-width: the reaper compares these instants as TEXT (§11.5).
            self.ctx.clock().astimezone(UTC).isoformat(timespec="microseconds"),
            self._run_id,
            repo_id,
            int(self.phase),
            fence,
        )

        async def unit(conn: aiosqlite.Connection) -> int:
            cursor = await conn.execute(
                "UPDATE phases "
                "   SET status = ?, last_error = ?, lease_owner = NULL, "
                "       lease_expires_at = NULL, heartbeat_at = NULL, updated_at = ? "
                " WHERE run_id = ? AND repo_id = ? AND phase = ? AND lease_fence = ?",
                params,
            )
            return int(cursor.rowcount)

        if await self.ctx.writer.submit(unit) == 0:
            self.ctx.log.info("write_discarded_stale_fence", repo_id=repo_id, fence=fence)
            return None
        self.ctx.project()
        return status

    async def _drain_llm_findings(self, repo_id: str | None) -> None:
        """Persist what the LLM client buffered. **Never lets a diagnostics write become the
        repo's verdict.**

        The unguarded version of this call sat between `_dispatch` and the outcome handling, so a
        busy timeout or a closed writer propagated out of `_drive`, was caught by `_isolated`'s
        `except Exception`, and recorded a repo that had just SUCCEEDED as `FailureClass.UNKNOWN`
        / PENDING with its execution never processed — the phase not advanced, the attempt row
        never written, the work redone on resume. An observability path that can take down the
        thing it observes is strictly worse than the silence it replaced.

        Swallowing here does not violate Rule 11, and the precedent is this codebase's own:
        `obs/events.py` makes exactly this trade for exactly this reason ("the caller is a worker
        mid-transform and the failing operation is *telemetry*"), and makes it the same way —
        **swallowed and surfaced, never swallowed and hidden.** The failure is logged at `error`
        with the buffer depth, and `flush()` re-buffers everything that did not land, so the next
        drain (or the wave-final one) retries it rather than losing it.

        `except Exception`, not `BaseException`: a `CancelledError` must still propagate, or a
        cancelled wave would be held open by its own telemetry.
        """
        try:
            await self.ctx.llm_findings.flush()
        except Exception as exc:
            # `log.error`, never `log.exception`: §11.4 forbids a formatted traceback in the
            # durable record because locals carry credentials, which is the same rule
            # `error_from_exception` (workers/base.py:536-539) states for `WorkerError`. The
            # qualified type name and the message carry everything a diagnosis needs.
            self.ctx.log.error(  # noqa: TRY400 - §11.4: no formatted traceback
                "llm_findings_flush_failed",
                repo_id=repo_id,
                phase=int(self.phase),
                pending=self.ctx.llm_findings.pending,
                exception_type=f"{type(exc).__module__}.{type(exc).__qualname__}",
                error=str(exc),
            )

    async def _record_diagnostics(
        self, repo_id: str, fence: int, error: WorkerError, ladder: LadderState
    ) -> bool:
        """Write `failure_class` / `transient_retries` / `last_error` under the fence.

        A separate statement from `complete_phase` only because the §6 CAS does not carry these
        columns; it is still the single writer, still fenced, and `False` here means the same
        thing it means everywhere else — the lease is gone, discard.

        `last_error` is redacted HERE, at this write boundary (SPEC §11.4, D90): this raw
        `UPDATE` bypasses `complete_phase` entirely, so D88's fix there never covers it, and
        this write runs on EVERY failure — retryable or not — before `RetryPolicy`'s decision
        is even acted on.
        """
        detail = self._detail(error)
        params = (
            str(error.failure_class),
            ladder.transient_retries,
            None if detail is None else redact_text(detail),
            # UTC, and fixed-width: the reaper compares these instants as TEXT (§11.5).
            self.ctx.clock().astimezone(UTC).isoformat(timespec="microseconds"),
            self._run_id,
            repo_id,
            int(self.phase),
            fence,
        )

        async def unit(conn: aiosqlite.Connection) -> int:
            cursor = await conn.execute(
                "UPDATE phases "
                "   SET failure_class = ?, transient_retries = ?, last_error = ?, updated_at = ? "
                " WHERE run_id = ? AND repo_id = ? AND phase = ? AND lease_fence = ?",
                params,
            )
            return int(cursor.rowcount)

        return await self.ctx.writer.submit(unit) > 0

    async def _renew(self, repo_id: str, fence: int) -> bool:
        try:
            await self.ctx.repository.renew_phase_lease(
                self._run_id,
                repo_id,
                self.phase,
                fence=fence,
                now=self.ctx.clock(),
                lease_ttl_s=self.ctx.lease_ttl_s,
            )
        except LeaseStolenError:
            return False
        return True

    async def _on_breach(
        self, repo_id: str, breach: LedgerBreach, *, fence: int, outcome: RepoOutcome
    ) -> RepoOutcome:
        """A ceiling refused the dispatch. Whether the fleet stops is the breach's `exit_code`.

        This is where "isolation is not blanket exception-swallowing" is decided: a run- or
        wave-scoped breach is a property of the RUN and raises `RunHalted`, which cancels the
        wave; a repo- or task-scoped one leaves the repo to a human and the fleet keeps going
        (§11.2).
        """
        if breach.exit_code is not None:
            raise RunHalted(
                HaltReason.WAVE_BUDGET
                if breach.exit_code == _EXIT_CODES[HaltReason.WAVE_BUDGET]
                else HaltReason.RUN_BUDGET,
                f"{type(breach).__name__} refused {repo_id}: {breach}",
                exit_code=breach.exit_code,
            )
        error = WorkerError(
            failure_class=FailureClass.BUDGET_EXHAUSTED, retryable=False, stderr_tail=str(breach)
        )
        await self._record_diagnostics(repo_id, fence, error, LadderState())
        written = await self._complete(
            repo_id, fence, RepoStatus.REQUIRES_HUMAN_INTERVENTION, str(breach)
        )
        if written is not None:
            await self._contain(repo_id, written)
        return replace(
            outcome,
            status=written or await self._status(repo_id),
            failure_class=FailureClass.BUDGET_EXHAUSTED,
            last_error=str(breach),
            discarded=written is None,
        )

    async def _contain(self, repo_id: str, written: RepoStatus) -> None:
        """§3.5: an abandoned repo marks exactly its transitive dependents `blocked_by`."""
        if written is RepoStatus.REQUIRES_HUMAN_INTERVENTION:
            blocked = await self.scheduler.propagate_blocked(repo_id)
            if blocked:
                self.ctx.log.warning(
                    "blocked_by_propagated", repo_id=repo_id, dependents=sorted(blocked)
                )

    # ------------------------------------------------------------------ checkpoints

    async def _load_checkpoint(
        self, repo_id: str
    ) -> checkpoints.LoadedCheckpoint[PhaseCheckpoint]:
        """Load this phase's checkpoint, and REPORT a refusal instead of dropping it (Rule 11).

        `checkpoints.load` never raises on a bad checkpoint: a stale `schema_version`, a
        different model class, a truncated envelope and a payload the model rejects all come back
        as `payload=None` with a named `rejection` and a `detail`, and that module's own contract
        for this caller is to log both and proceed. Returning `loaded.payload` alone dropped
        both, so the phase re-ran units it had already completed with nothing emitted anywhere —
        the only symptom was repeated work, noticed by whoever happened to be watching.

        The whole `LoadedCheckpoint` is returned rather than the payload because the caller owes
        the wave a `RepoOutcome.checkpoint_unreadable`; a log line alone is the standard this
        codebase already declined for `checkpoint_rejected`, for the same reason.

        This changes what is REPORTED and nothing about what is accepted: which checkpoints
        `load` refuses is its decision and is untouched here.
        """
        loaded = await checkpoints.load(
            self.ctx.read_conn,
            run_id=self.ctx.run_id,
            repo_id=repo_id,
            phase=self.phase,
            model=PhaseCheckpoint,
        )
        if loaded.discarded_stored_work:
            self.ctx.log.warning(
                "checkpoint_unreadable",
                repo_id=repo_id,
                phase=int(self.phase),
                rejection=str(loaded.rejection),
                detail=loaded.detail,
            )
        return loaded

    async def _save_checkpoint(
        self,
        repo_id: str,
        result: WorkerResult[O],
        prior: PhaseCheckpoint | None,
        attempt: int,
    ) -> None:
        landed = [*(prior.completed_units if prior is not None else []), *result.completed_units]
        done = list(dict.fromkeys(landed))
        payload = PhaseCheckpoint(
            completed_units=done,
            remaining_units=[unit for unit in result.remaining_units if unit not in set(done)],
            attempt=attempt,
        )
        await checkpoints.save(
            self.ctx.writer,
            run_id=self.ctx.run_id,
            repo_id=repo_id,
            phase=self.phase,
            payload=payload,
        )

    # ------------------------------------------------------------------ helpers

    async def _status(self, repo_id: str) -> RepoStatus:
        row = await self.ctx.repository.get_phase(self._run_id, repo_id, self.phase)
        return RepoStatus.PENDING if row is None else row.status

    @staticmethod
    def _detail(error: WorkerError | None) -> str | None:
        """`last_error` is never prose the harness composed: it is the worker's own bounded
        `stderr_tail`, or the exception type when there was no output at all.

        NOT guaranteed redacted at this point. `workers/base.py::error_from_exception`
        (`:544-556`) sets `stderr_tail=str(exc)` with no redaction for any exception that
        escapes a worker's `run()`, and this helper returns that value unchanged. Redaction
        happens later, at each write boundary that persists the result of this call (SPEC
        §11.4, D88/D90): `state/repository.py::complete_phase` (`:1407`),
        `orchestrator/runner.py::_terminate_uncharged` (`:993`) and `::_record_diagnostics`
        (`:1073`, the caller of this method), and `state/repository.py::record_attempt`
        (`:2136-2137`, for the sibling `attempts.stdout_tail`/`stderr_tail` columns). Do not
        remove those `redact_text` calls on the strength of this docstring — they are not
        redundant."""
        if error is None:
            return None
        return error.stderr_tail or error.exception_type or str(error.failure_class)


