"""`BaseWorker` — one logical unit of fleet work (SPEC §7.1, ADR-0014).

A worker is *stateless*: everything it may touch arrives in a `WorkerContext`, and everything
it produces leaves as a `WorkerResult`. It writes no SQL (the PhaseRunner is the single
writer, §11.5) and it swallows no errors (CLAUDE.md Rule 11).

Three things in this module are load-bearing and were each a defect before they were code:

* **`WorkerResult.status` is five-valued, not a bool.** A binary verdict is a lie about
  interrupted work: a rewriter that lands 40 of 60 units and then hits `ctx.deadline` has 40
  real commits in the tree, and reporting `failed` with `output=None` makes attempt 2 replay all
  60 against an already-rewritten tree and emit no-op patches the ladder misreads as
  `RULE_MISS`. `partial` carries `completed_units`, that output is the checkpoint, and
  `preconditions_hold` consumes it on re-entry.
* **Deadline and cancellation are cooperative.** `asyncio.timeout` cannot interrupt a
  process-pool child or a `git` subprocess, so a worker that ignores `ctx.deadline`/`ctx.cancel`
  leaves an orphan holding a CPU core and the worktree lock the cleanup path is about to delete.
  `run()` passes `ctx.deadline` into every `util/proc.py` call and polls `ctx.cancel` between
  units; `execute()` gives it a grace window and then calls `on_cancel()`.
* **The attempt ceiling is runtime policy, not a field constraint.** `le=MAX_ATTEMPTS` on
  `attempts` would make §9's per-run configurable ladder a lie and would make a *lowered*
  constant retroactively unable to load historical rows. The ceiling is checked against the
  owning task's `max_attempts`, which is carried on the row beside the count.

The retry contract lives here as executable code rather than prose. `execute()` is a template
method around the abstract `run()`:

* **`ctx.attempt` is the ladder position, and it is the ONLY one.** It is the runner's persisted
  `phases.attempts + 1` — the number a `fleet resume` reads off disk — and `execute()` *derives*
  the rung from it (`tier_for_attempt`, `context_policy_for_attempt`) rather than re-deriving it
  from a counter of its own. It used to do the latter, which meant that a runner dispatching one
  durable rung per invocation (`max_attempts=1`, §11.5) always handed `run()` `attempt=1,
  tier=DETERMINISTIC`: the persisted ladder advanced 1 → 2 → 3 and the worker re-ran the
  deterministic rung all three times, so ADR-0014/ADR-0021 escalation never reached an LLM at
  all. Two counters that must agree is the defect; there is now one number and it lives on disk;
* at most `max_attempts` substantive attempts per `(repo, phase)` — default `MAX_ATTEMPTS`,
  supplied per invocation by the owning `TransformTask` — and the attempts are deliberately
  **not** identical: attempt N runs at `TIER_LADDER[N-1]`
  (`DETERMINISTIC` → `LLM_REPAIR` → `LLM_ESCALATION`), because identical retries are exactly
  how the reference material's "infinite loop" failure mode begins. The ceiling is checked
  against the ABSOLUTE attempt number, not against this invocation's own count, so a resume at
  rung 3 of 3 runs one rung and stops rather than starting a fresh three;
* `WorkerError.retryable` — never a string match on the message — is what branches the ladder;
* `FailureClass.TRANSIENT_INFRA` is **not** an attempt, and it is **not this module's business**.
  The §11.8 transient budget lives in `orchestrator.retry.RetryPolicy` alone. It used to live in
  both, and two nested budgets do not add — they MULTIPLY: a worker granted 5 free re-runs inside
  a driver granted 4 issues 30 calls at a throttled endpoint, not 5. Only the runner can persist
  `phases.transient_retries`, so only the runner can own a budget that has to survive a
  `SIGKILL`; a worker returns the typed `TRANSIENT_INFRA` failure and stops there;
* after the final substantive failure the result is `RepoStatus.REQUIRES_HUMAN_INTERVENTION`
  and the ladder stops. The fleet is never blocked: the caller records the terminal state, the
  scheduler marks the dependents `blocked_by`, and the run continues;
* a result produced under a **stale `lease_fence`** is discarded whole — never merged — and no
  usage from it is billed, because another process owns that worktree now (§11.5).
"""

from __future__ import annotations

import asyncio
import inspect
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from hashlib import sha256
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from fleet.llm.client import BudgetExhausted, CallBudget, LlmError, TierUnavailable
from fleet.models.base import FleetModel, TruncatedStr
from fleet.models.enums import (
    ContextPolicy,
    FailureClass,
    ModelTier,
    Phase,
    RepoStatus,
    TransformTier,
)
from fleet.models.repo import RepoId
from fleet.models.tasks import DEFAULT_LADDER, MAX_ATTEMPTS, TokenUsage

if TYPE_CHECKING:  # imported for types only; none of these modules is needed at runtime here
    from structlog.stdlib import BoundLogger

    from fleet.llm.client import ModelClient, RoleRouter
    from fleet.orchestrator.budgets import Limits
    from fleet.state.repository import ReadOnlyRepository

_TIER_FOR_RUNG: dict[ContextPolicy | None, TransformTier] = {
    None: TransformTier.DETERMINISTIC,                                  # no prompt is rendered
    ContextPolicy.EVIDENCE_ONLY: TransformTier.LLM_REPAIR,
    ContextPolicy.EVIDENCE_PLUS_REJECTED_APPROACHES: TransformTier.LLM_ESCALATION,
    ContextPolicy.EVIDENCE_PLUS_PRIORS: TransformTier.LLM_ESCALATION,
}

TIER_LADDER: tuple[TransformTier, ...] = tuple(_TIER_FOR_RUNG[p] for p in DEFAULT_LADDER)
"""The tier per attempt, one rung per `DEFAULT_LADDER` rung (ADR-0021: rung index == attempt).
`models.enums` no longer owns a tier ladder — the ladder is the CONTEXT ladder, and the tier is
a consequence of which rung is running, so it is derived here rather than declared twice."""


def context_policy_for_attempt(
    attempt: int, ladder: Sequence[ContextPolicy | None] = DEFAULT_LADDER
) -> ContextPolicy | None:
    """The ADR-0021 rung for a 1-based attempt number. Rung 1 is `None`: deterministic, no prompt.

    `ladder` defaults to the hardcoded `DEFAULT_LADDER` but is the CALLER's to override — the one
    real caller, `execute()`, passes the CONFIGURED `transform.ladder` (§9), so a per-run
    `--context-policy` override (§10) that already landed in `FleetSettings.config.transform.ladder`
    genuinely changes which policy a rung gets, rather than being silently ignored.

    Past the declared ladder the last rung repeats rather than raising — a ladder shorter than a
    task's `max_attempts` is configuration (§9), not a crash. Clamped at the bottom too, because
    an attempt number below 1 is a caller bug that must not silently index from the end.
    """
    if not ladder:
        return None
    return ladder[min(max(attempt, 1), len(ladder)) - 1]


def tier_for_attempt(
    attempt: int, ladder: Sequence[ContextPolicy | None] = DEFAULT_LADDER
) -> TransformTier:
    """The tier that rung runs at, derived from `ladder` via `_TIER_FOR_RUNG` — never declared
    twice, and never independently configurable: the tier is a CONSEQUENCE of which context
    policy is running, so `LadderRung.tier` in config is descriptive, not a second input here.

    Deliberately the same clamp as `context_policy_for_attempt` and as
    `orchestrator.retry.LadderState.tier` (which now delegates to this function): the tier a
    worker OBSERVES and the tier the policy REPORTS are the same function of the same attempt
    number and the same ladder, so they cannot drift.
    """
    return _TIER_FOR_RUNG[context_policy_for_attempt(attempt, ladder)]


WorkerStatus = Literal["ok", "partial", "failed", "timeout", "cancelled"]
"""Set from mechanical evidence, never from prose. `partial` is the reason this is not a bool."""

NON_RETRYABLE: frozenset[FailureClass] = frozenset(
    {
        FailureClass.BUDGET_EXHAUSTED,   # §11.2 is fail-closed: a re-run only spends money again
        FailureClass.DISK_EXHAUSTED,     # terminal, exit 9 (§11.3)
        FailureClass.BACKEND_UNAVAILABLE,  # terminal for the RUN, exit 8 (§11.8)
        FailureClass.COLLISION,          # an unresolved `collisions` row; a re-run cannot resolve
        FailureClass.PREFLIGHT,          # the repo's own shape; identical on every attempt
        FailureClass.CYCLE,              # structural
        FailureClass.DEP_CONFLICT,       # structural
        FailureClass.STUB_DIVERGED,      # straight to a human, never to the ladder (§3.5.1)
    }
)
"""Failure classes for which another attempt cannot plausibly differ. Everything else is
retryable by DEFAULT — the ladder's whole point is that attempt 2 sees more than attempt 1 —
and a worker with mechanical evidence to the contrary (an exit code, a probe) sets
`WorkerError.retryable` itself. Nothing in this module branches on message text."""


def is_retryable(failure_class: FailureClass) -> bool:
    """Default retryability for a class. Advisory: `WorkerError.retryable` is what actually
    branches, because only the worker saw the exit code (`1` from a build is a repair prompt,
    `137` from the OOM killer is a re-queue, and no string comparison tells them apart)."""
    return failure_class not in NON_RETRYABLE


def clock_failure(*, started: bool, timed_out: bool) -> tuple[FailureClass, bool] | None:
    """`(failure_class, retryable)` when a subprocess result says something about the FLEET'S CLOCK
    rather than about the repository — or `None` when the process ran to a verdict of its own.

    `util.proc.run` reports a call made after its deadline had already passed as `started=False`
    **and** `timed_out=True` **and** `exit_code=124`, all three at once. Those two flags therefore
    describe two different events that must not be collapsed:

    * `started=False` — nothing was spawned. **No measurement was taken at all**, so there is no
      evidence about the repo, and ADR-0014 forbids charging a ladder rung for it — for a while:
      it is `TRANSIENT_INFRA`, which `RetryPolicy.decide` re-runs on the same rung with no attempt
      charged, but only up to `RetryPolicy.max_transient_retries` (default 4, `retry.py:200-202`).
      Past that cap the identical clock failure is charged exactly like any other substantive
      failure (§11.8) — "no attempt charged" is a bounded reprieve, not a standing exemption, and
      a caller reporting it to an operator has to say so. (Whether a given caller ALSO prompts a
      model for this same retry is that caller's own choice, not something this function decides
      or promises either way — see the caller's own docstring, e.g. `buildverify._diagnose`.)
    * `started=True, timed_out=True` — the process ran and was killed at its deadline. Taking
      longer than the deadline IS behaviour of the repo (a pathological history, a hanging fetch),
      so it is substantive `TIMEOUT` and costs a rung.

    Order matters and is the whole point: reading `timed_out` first makes the never-started branch
    unreachable through the only producer of real `ProcResult`s, and reports a command that never
    ran as one that ran too long.

    **This function exists so the `FailureClass` answer is written down once** — not so the
    `(started, timed_out)` invariant only ever gets decoded here. `util.proc.no_verdict` (ADR-0067
    part 4) is a separate, deliberate decoder of the identical two flags for a different need — a
    reason STRING for `clone._no_verdict`'s five call sites, not a `FailureClass` — and the two
    return different types for different callers and must not be merged (see `no_verdict`'s own
    docstring). `rewrite/astgrep.py`'s parse probe still hand-orders the same two flags inline
    (`if result.started and not result.timed_out:`) rather than calling either decoder, so today
    the invariant is written down in three places, not one — this function is the single owner of
    the `FailureClass` half only.

    `buildverify.classify_build_failure` and `clone._error_for` are the two callers this function
    unifies. Before `44d5550`, both answered `TIMEOUT` for the never-started shape — wrong, but in
    step, because both read `timed_out` alone. `44d5550` reordered ONLY
    `classify_build_failure`'s branches to check `started` first, and in the same hunk added a
    comment claiming `clone.py` already drew the same line "for this reason" and that the two were
    "meant to stay in step" — without touching `clone.py`, which still discarded `started` on the
    way into its `GitCommandError` and kept answering `TIMEOUT`. The comment was false the moment
    it was committed: the divergence it denied did not exist yet, and was then manufactured by
    that half-applied reorder, holding only for the window between `44d5550` and this fix
    (`68a41ff`) — no real wave ever saw it (review-36 I4). A shared callee makes the two callers'
    `FailureClass` agreement mechanical rather than aspirational, and
    `test_workers_scan.test_the_clone_and_build_classifiers_agree_on_every_clock_failure` pins it.
    """
    if not started:
        return FailureClass.TRANSIENT_INFRA, True
    if timed_out:
        return FailureClass.TIMEOUT, True
    return None


def loop_now() -> float:
    """The running loop's monotonic clock — the SAME clock `ctx.deadline` is expressed in.

    Deliberately not `utcnow()`: §5 invariant 4 forbids a worker stamping wall-clock time (the
    orchestrator host owns that), and a deadline compared against a wall clock breaks the moment
    NTP steps it. This is elapsed-time arithmetic only; nothing here is ever persisted.
    """
    return asyncio.get_running_loop().time()


def total_tokens(usage: TokenUsage) -> int:
    """Every token this call is billed for. `cache_read_tokens` is counted because it is NOT a
    subset of `input_tokens` (ADR-0023): omitting it would let a heavily-cached run overshoot a
    token ceiling that §11.2 promises is fail-closed. `TokenUsage` deliberately has no
    `total_tokens` field — a stored total is a second source of truth that can disagree."""
    return usage.input_tokens + usage.output_tokens + usage.cache_read_tokens


def accumulate(*usages: TokenUsage) -> TokenUsage:
    """Sum the countable fields of several `TokenUsage` records.

    A free function rather than a `TokenUsage.merged()` method, because the sum is NOT a
    `TokenUsage` in the same sense as its parts: `role`, `tier` and `model_id`
    identify ONE dispatch, and a ladder that escalates DETERMINISTIC → WORKHORSE → HEAVY has
    three of each. They are dropped rather than silently taking the last writer's value, which
    would attribute the whole run's spend to whichever tier happened to answer last.

    `backend` is the one exception (ADR-0107): last-non-empty-wins, order-preserving over the
    fold's argument order (every caller passes `accumulate(seed, *results_in_call_order)`, so
    "last" means "the target that answered most recently"). §12.24 needs a non-empty `backend`
    on the attempt row for the common single-call-per-rung case, and last-non-empty degrades
    gracefully rather than incorrectly for the rare multi-role rung: the column reports *a*
    backend that genuinely answered during the attempt, never a fabricated or averaged one.
    """
    backend = ""
    for u in usages:
        if u.backend:
            backend = u.backend
    return TokenUsage(
        backend=backend,
        input_tokens=sum(u.input_tokens for u in usages),
        output_tokens=sum(u.output_tokens for u in usages),
        cache_read_tokens=sum(u.cache_read_tokens for u in usages),
        cost_usd=sum(u.cost_usd for u in usages),
        # Summed, not AND-ed. Every caller uses this as a running fold seeded with a zero
        # `TokenUsage()`, and a boolean AND over that seed is `False` for every attempt; a
        # boolean OR is any-hit, which `schema.sql`'s `llm_cache_hit = 1 => cost_usd = 0` forbids
        # the moment one call in the attempt missed and this `sum` of `cost_usd` is non-zero.
        # `TokenUsage.all_served_from_llm_cache` derives the flag from the pair.
        llm_cache_lookups=sum(u.llm_cache_lookups for u in usages),
        llm_cache_hits=sum(u.llm_cache_hits for u in usages),
        # ADR-0107. Total backend hops across the whole attempt, matching `schema.sql`'s
        # `llm_failovers` comment ("backend hops spent inside THIS attempt") directly — no
        # identity-element hazard, exactly like the two cache counters above.
        llm_failovers=sum(u.llm_failovers for u in usages),
    )


class WorkerInput(FleetModel):
    """Marker base. Every worker declares a concrete subclass."""


class WorkerOutput(FleetModel):
    """Marker base. Persisted as the worker's checkpoint payload.

    Versioned for the same reason every adapter is (§7.3, §7.5): `checkpoints.load` (§8) compares
    the persisted `written_schema_version` against the loading class's `schema_version`, and a
    mismatch INVALIDATES the checkpoint and re-runs the phase from `phases.base_ref` — it does not
    raise, because a renamed field must not take 180 resuming repos down at once. Equality, not
    `>=`, is the test: a field added with a default would otherwise let resume proceed on a plan
    half-populated from stale data, which is the quiet failure, not the loud one.
    """

    schema_version: ClassVar[int] = 1   # subclasses bump on ANY field rename, removal, or
                                        #   semantic change; mirrors the adapter `version` ClassVar
    written_schema_version: int = 0     # persisted copy of the ClassVar above

    @model_validator(mode="before")
    @classmethod
    def _stamp_schema_version(cls, data: Any) -> Any:
        if isinstance(data, dict) and "written_schema_version" not in data:
            return {**data, "written_schema_version": cls.schema_version}
        return data

    @classmethod
    def checkpoint_is_current(cls, persisted: Mapping[str, object]) -> bool:
        """Is a persisted checkpoint payload loadable into THIS class? Equality, never `>=`."""
        return persisted.get("written_schema_version") == cls.schema_version


class WorkerBudget(FleetModel):
    """Per-invocation ceilings. Set by the runner, never by the worker — but enforced
    COOPERATIVELY: the runner projects them onto `WorkerContext.deadline`/`cancel`/`budget`, and
    `run()` honours those. Nothing else can, since neither `asyncio.timeout` nor `CancelledError`
    reaches a subprocess or a process-pool child."""

    wall_clock_s: int = 900
    max_tokens: int = 200_000
    max_cost_usd: float = 5.0
    max_subprocesses: int = 16


@dataclass(slots=True)
class WorkerContext:
    """Everything a worker may touch. Handed in; never imported as a global."""

    run_id: UUID
    repo_id: RepoId
    attempt: int                  # THE ladder position, 1-based: the runner's persisted
    #                               `phases.attempts + 1` (ADR-0021, rung index == attempt).
    #                               `tier` and `context_policy` below are DERIVED from it by
    #                               `execute()`, never counted independently — a worker that
    #                               re-derived the rung from an in-process counter would run the
    #                               deterministic rung on every attempt of a resumable ladder
    workdir: str                  # the repo's git worktree
    lease_owner: str              # '{host}:{container_id}:{pid}:{boot_uuid}'; §6 phases.lease_owner
    lease_fence: int              # §6 `phases.lease_fence`. EVERY write the runner makes on this
    #                               worker's behalf carries it. A write whose owner/fence no longer
    #                               matches the stored one is REJECTED, and the worker MUST abort
    #                               WITHOUT touching git — a reclaimed lease means another process
    #                               now owns this worktree (§11.5)
    deadline: float               # ABSOLUTE `loop.time()`, not a duration. Passed into every
    #                               `util/proc.py` call so a subprocess inherits the ceiling
    cancel: asyncio.Event         # set on reclaim, budget halt, or sibling failure; polled by run()
    budget: CallBudget            # what is LEFT for this invocation — tokens, usd, deadline (§11.2)
    db: ReadOnlyRepository        # fleet.state.repository opened `mode=ro` (ADR-0016, §11.5).
    #                               A worker MAY NOT open a writable SQLite connection at all;
    #                               every write funnels through the single StateWriter actor, and
    #                               `StateRepository`'s write methods are callable only there
    llm: ModelClient              # §7.7's ONE call surface — the thing that can actually make a
    #                               validated, typed call (`complete()`). A rung handed only the
    #                               ROUTER can resolve a role to a tier and then do nothing with
    #                               it, which is how ADR-0014's repair/escalation rungs came to be
    #                               unreachable under real wiring. Built by `RunContext` from the
    #                               router + backends + cache and handed down (Guardrail 3); a
    #                               worker never constructs one and never takes one by constructor
    router: RoleRouter            # role → tier ONLY, for the `limits.for_tier` semaphore. Not a
    #                               second call surface: it cannot make a call, which is the
    #                               point — the tier a role routes to is the concurrency key, and
    #                               only the router knows it (`resolve(role).tier`)
    limits: Limits                # fleet.orchestrator.budgets semaphores
    log: BoundLogger              # structlog, pre-bound with run_id/repo_id/phase
    tier: TransformTier = TransformTier.DETERMINISTIC  # rung of the ladder for this attempt;
    #                               `tier_for_attempt(attempt)`, set by `execute()`
    context_policy: ContextPolicy | None = None  # ADR-0021's rung, `context_policy_for_attempt`.
    #                               `None` IS rung 1 — deterministic, no prompt rendered — so a
    #                               worker branches on it to decide whether it talks to a model
    #                               at all, and how much evidence the prompt carries when it does
    # Deliberately absent: a clock. Workers never stamp time; `utcnow()` is the orchestrator host's
    # clock only (§5 invariant 4, §11.5), so heartbeat and lease arithmetic compares one clock.
    # `deadline` is the LOOP's monotonic clock, which is elapsed time, not a timestamp.

    def time_left(self, now: float) -> float:
        """Seconds until `deadline`; never negative. Pass this into every subprocess call."""
        return max(0.0, self.deadline - now)

    def expired(self, now: float) -> bool:
        """Has the ceiling already passed? Checked BEFORE work starts, not only after."""
        return now >= self.deadline

    def cancelled(self) -> bool:
        """Polled between units — the only granularity at which a worker may stop."""
        return self.cancel.is_set()

    def fence_is_current(self, stored_fence: int, stored_owner: str | None = None) -> bool:
        """Does this invocation still own the lease it started under? A `False` here means the
        write must be DISCARDED and git must not be touched: another process owns the worktree."""
        if stored_fence != self.lease_fence:
            return False
        return stored_owner is None or stored_owner == self.lease_owner


class WorkerError(FleetModel):
    """A worker's failure, machine-readable. `retryable` — not prose — is what `retry.py` branches
    on: exit 1 from a build is a repair prompt, exit 137 from the OOM killer is a re-queue, and no
    string comparison can tell them apart reliably."""

    failure_class: FailureClass
    retryable: bool
    exit_code: int | None = None
    stderr_tail: TruncatedStr = ""   # bounded at capture and redacted at the write boundary
    #                                  (§11.3, §11.4); never the whole stream. TRUNCATED, never
    #                                  rejected: a 400 KB Gradle stderr that fails validation is
    #                                  an attempt that is never persisted (see models.base)
    artifact_ref: str | None = None  # artifacts/logs/<run_id>/<attempt_id>.log — the FULL stream.
    #                                  The repair prompt reads THIS verbatim, never a summary and
    #                                  never a transcript (Constraint 5, §3.2)
    exception_type: str | None = None   # qualified name only; never a formatted traceback
    tier: ModelTier | None = None       # D78: the tier TierUnavailable named, when known. Lets
    #                                    # `record_backend_unavailable`'s `tier=` arm narrow its
    #                                    # derived fields to the ACTUAL exhausted tier instead of
    #                                    # falling back to a whole-run map (findings.py:421-423).


class WorkerResult[O: WorkerOutput](FleetModel):
    """The ONLY thing a worker returns. `status` is set from mechanical evidence, never prose.

    `partial` exists because a binary verdict is a lie about interrupted work: a rewriter that
    commits 40 of 60 units and then hits `deadline` has landed 40 real commits, and reporting
    `failed` with no output would make attempt 2 replay all 60 against an already-rewritten tree
    and produce no-op patches the ladder would misread as `RULE_MISS`.

    A result returned under a STALE fence is DISCARDED by the runner, never merged (§11.5).
    """

    status: WorkerStatus
    output: O | None = None
    completed_units: list[str] = Field(default_factory=list)  # unit ids already landed — the
    #                                                           checkpoint's whole content
    remaining_units: list[str] = Field(default_factory=list)  # unit ids still owed
    error: WorkerError | None = None
    usage: TokenUsage = Field(default_factory=TokenUsage)
    duration_ms: int = Field(default=0, ge=0)
    evidence: list[str] = Field(default_factory=list)  # attempt_ids / paths backing the verdict

    @model_validator(mode="after")
    def _units_are_disjoint(self) -> Self:
        """`completed ∩ remaining` must be empty — a unit in both is a replay waiting to happen."""
        overlap = set(self.completed_units) & set(self.remaining_units)
        if overlap:
            raise ValueError(f"completed and remaining units overlap: {sorted(overlap)}")
        return self

    @model_validator(mode="after")
    def _status_matches_its_evidence(self) -> Self:
        """The five statuses are claims about mechanical facts, so the facts must be present.

        `partial` with nothing completed is `failed` wearing a friendlier name — and it is the
        exact shape that made 40 landed commits invisible to state. `failed`/`timeout` without a
        structured `error` leaves `retry.py` nothing to branch on but prose.
        """
        if self.status == "partial" and not self.completed_units:
            raise ValueError(
                "status='partial' requires completed_units: nothing landed is 'failed'"
            )
        if self.status in ("failed", "timeout") and self.error is None:
            raise ValueError(f"status={self.status!r} requires a structured WorkerError")
        if self.status == "ok" and self.remaining_units:
            raise ValueError("status='ok' with remaining_units owed is a contradiction")
        return self

    @property
    def ok(self) -> bool:
        """Success, and nothing else. `partial` is deliberately NOT ok."""
        return self.status == "ok"

    @property
    def landed(self) -> bool:
        """Did this invocation put anything real in the tree? `ok` or `partial`; the checkpoint
        is written for both, which is what makes re-entry cheap instead of a full replay."""
        return self.status in ("ok", "partial")

    @property
    def retryable(self) -> bool:
        """Whether the ladder may advance. Reads the structured flag; never the message."""
        return self.error is not None and self.error.retryable


class WorkerExecution[O: WorkerOutput](FleetModel):
    """Outcome of the rungs this invocation ran for one `(repo, phase)`.

    `attempts` counts the rungs THIS invocation charged; the durable count is `phases.attempts`,
    which the PhaseRunner owns and advances one fenced write at a time (§11.5). There is
    deliberately no `transient_retries` here: the §11.8 transient budget belongs to
    `orchestrator.retry.RetryPolicy`, whose count is the one that reaches the `phases` row and
    therefore the one that survives a restart. A mirror of it on this record would be a second
    number that can disagree with the first — and, worse, a second budget that multiplies it.
    """

    worker: str
    phase: Phase
    repo_id: RepoId
    status: RepoStatus
    attempts: int = Field(default=0, ge=0, description="Substantive attempts only")
    # NO `le=` here, deliberately (§5.4): a compiled ceiling would make §9's per-run configurable
    # ladder false for any ladder longer than 3, and a LOWERED constant would make historical rows
    # unloadable. The ceiling is `max_attempts` — the owning task's number, carried BESIDE the
    # count so the row stays self-describing — and it is enforced at runtime, below.
    max_attempts: int = Field(default=MAX_ATTEMPTS, ge=1, description="The owning task's ceiling")
    tiers: list[TransformTier] = Field(default_factory=list, description="Rung used per attempt")
    results: list[WorkerResult[O]] = Field(default_factory=list)
    usage: TokenUsage = Field(default_factory=TokenUsage)
    duration_ms: int = Field(default=0, ge=0)
    fence_stale: bool = False
    """The lease was reclaimed mid-flight: every result was DISCARDED rather than merged, no
    usage was billed, and git was not touched. The repo goes back to PENDING for its new owner."""

    @model_validator(mode="after")
    def _ceiling_is_the_owning_tasks(self) -> Self:
        if self.attempts > self.max_attempts:
            raise ValueError(
                f"{self.attempts} attempts exceeds the owning task's max_attempts="
                f"{self.max_attempts}"
            )
        return self

    @property
    def ok(self) -> bool:
        return self.status is RepoStatus.SUCCEEDED

    @property
    def final(self) -> WorkerResult[O] | None:
        return self.results[-1] if self.results else None

    @property
    def failure_class(self) -> FailureClass | None:
        last = self.final
        if last is None or last.error is None:
            return None
        return last.error.failure_class

    @property
    def last_error(self) -> WorkerError | None:
        last = self.final
        return None if last is None else last.error


def unfinished_units[O: WorkerOutput](
    all_units: Sequence[str], checkpoint: WorkerResult[O] | None
) -> list[str]:
    """The units a re-entry still owes, given the persisted checkpoint.

    This is the executable half of "never blind replay": with no checkpoint everything is owed;
    with a `partial` one, only what it did not complete. Order is preserved, so a resumed run
    walks the same sequence it would have walked without the interruption.
    """
    if checkpoint is None:
        return list(all_units)
    done = set(checkpoint.completed_units)
    return [unit for unit in all_units if unit not in done]


def _llm_origin(exc: BaseException) -> LlmError | None:
    """The nearest `LlmError` in `exc`'s own type or its `__cause__` chain (D133).

    A worker that classifies its own LLM errors never reaches `classify_exception` — this is the
    fallback path only, for a worker that re-raises a caught `LlmError` wrapped in a domain
    exception (`rewrite.py::_repair`: `raise WorkerRepairError(...) from exc`). Without the walk,
    that wrap loses `TierUnavailable`'s declared `failure_class` to the coarse isinstance arms
    below and misclassifies a backend outage as `UNKNOWN`. Bounded against a `__cause__` cycle —
    nothing here trusts one is impossible, only that normal code does not construct one.
    """
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, LlmError):
            return current
        current = current.__cause__
    return None


def classify_exception(exc: BaseException) -> FailureClass:
    """Coarse, exception-shaped classification — the fallback path only.

    The authoritative classifier inspects the *response payload* (ADR-0014) and lives in
    `fleet.orchestrator.retry`; this exists so that an exception escaping a worker is still
    recorded as a typed failure instead of crashing the wave.

    An `LlmError` (direct, or wrapped per `_llm_origin`) that declares its own `failure_class`
    (D133: only `BudgetExhausted` and `TierUnavailable` do, in `llm/client.py`) is consulted
    FIRST, ahead of the generic isinstance arms — those two are §11.2/§11.8 fail-closed
    classifications and must survive a wrap the same way they would survive a bare raise.
    """
    origin = _llm_origin(exc)
    if origin is not None:
        declared: FailureClass | None = getattr(origin, "failure_class", None)
        if declared is not None:
            return declared
    if isinstance(exc, BudgetExhausted):
        return BudgetExhausted.failure_class
    if isinstance(exc, TimeoutError):
        return FailureClass.TIMEOUT
    if isinstance(exc, MemoryError):
        return FailureClass.BUDGET_EXHAUSTED
    if isinstance(exc, ConnectionError | BrokenPipeError | InterruptedError):
        return FailureClass.TRANSIENT_INFRA
    if isinstance(exc, SyntaxError):
        return FailureClass.PARSE_ERROR
    if isinstance(exc, NotImplementedError):
        return FailureClass.RULE_MISS
    return FailureClass.UNKNOWN


def error_from_exception(exc: BaseException) -> WorkerError:
    """A structured `WorkerError` for an exception that escaped `run()`.

    `exception_type` is the qualified name and `stderr_tail` the message — never a formatted
    traceback (§11.4: a traceback carries local variables, and locals carry credentials).

    `tier` is populated when classification traced to a `TierUnavailable` (D133), direct or
    wrapped, so `record_backend_unavailable`'s `tier=` arm (`orchestrator/runner.py`) can name
    the actually-exhausted tier instead of falling back to the whole-run disclosure caveat
    (`findings.py`'s `tier_known=False` wording) — the same signal `workers/classify.py::_error_for`
    already carries for its own tier-aware caller.
    """
    failure_class = classify_exception(exc)
    origin = _llm_origin(exc)
    tier = origin.tier if isinstance(origin, TierUnavailable) else None
    return WorkerError(
        failure_class=failure_class,
        retryable=is_retryable(failure_class),
        stderr_tail=str(exc),
        exception_type=f"{type(exc).__module__}.{type(exc).__qualname__}",
        tier=tier,
    )


def assert_stateless(obj: object) -> None:
    """Every registry value is a singleton, so every worker/adapter MUST be stateless (§7.2).

    An instance attribute other than a `ClassVar` is a defect, not a style preference: an adapter
    caching a parsed lockfile on `self` leaks repo A's dependencies into repo B, and in a pool
    child the parent's singleton was never registered at all. Mechanical, so it cannot rot.
    """
    state = getattr(obj, "__dict__", {})
    if state:
        raise TypeError(
            f"{type(obj).__qualname__} is a shared singleton but carries instance state: "
            f"{sorted(state)}"
        )


class BaseWorker[I: WorkerInput, O: WorkerOutput](ABC):
    """One logical unit of fleet work. Stateless; all state goes through ctx.db."""

    __slots__ = ()  # subclasses that also declare `__slots__` get `assert_stateless` for free

    name: ClassVar[str]
    phase: ClassVar[Phase]
    input_model: ClassVar[type[WorkerInput]]
    output_model: ClassVar[type[WorkerOutput]]
    budget: ClassVar[WorkerBudget] = WorkerBudget()
    retries: ClassVar[int] = MAX_ATTEMPTS  # ADR-0014 ceiling; a worker may lower it, never raise

    #: NOTE: there is deliberately no `max_transient_retries` here. A transient failure re-runs
    #: the same rung under the same lease, and that budget is `RetryPolicy`'s alone (§11.8) —
    #: see this module's docstring for why a second copy multiplied it rather than agreeing.

    #: How long `run()` gets to notice `ctx.cancel` and return at a unit boundary before
    #: `on_cancel()` is called to kill what it owns. Lowered by tests, never raised in prod.
    cancel_grace_s: ClassVar[float] = 5.0

    @abstractmethod
    async def run(self, ctx: WorkerContext, payload: I) -> WorkerResult[O]:
        """Perform the work. MUST NOT swallow errors (Rule 11); MUST NOT open a writable SQLite
        connection or write SQL at all — the runner hands the returned WorkerResult to the single
        StateWriter actor (§11.5), and a result carrying a stale `ctx.lease_fence` is discarded;
        MUST push CPU-bound work to ctx.limits.cpu_pool, passing paths and returning data,
        never handing a database connection to a pool child; MUST land every file mutation as an
        atomic trailered commit via vcs/commits.py — git is the record, SQLite gets a pointer
        (§3.2 step 6, ADR-0024); MUST stamp no timestamps of its own (§5 invariant 4).

        MUST also keep its lease alive: the runner renews `phases.heartbeat_at` on this worker's
        behalf inside `heartbeat_ttl_seconds` (default 300, §6), which for a long single-shot LLM
        call means driving `ModelClient.stream`'s progress events (§7.7) rather than blocking
        silently on `complete()`. A lapsed heartbeat is a reclaim, and a reclaim bumps
        `lease_fence` — after which every write this invocation attempts is rejected.

        Deadline and cancellation are cooperative because they have to be: `asyncio.timeout`
        around the coroutine cannot interrupt a process-pool child or a `git` subprocess, so a
        worker that ignores them leaves an orphan holding a CPU core and a worktree lock the
        cleanup path is about to delete. Therefore run() MUST pass `ctx.deadline` into every
        `util/proc.py` call and MUST poll `ctx.cancel` between units.

        On `deadline` or `cancel` with units already landed, run() returns `status='partial'` with
        `completed_units`/`remaining_units` populated. That output MUST be persisted as the phase
        checkpoint, and MUST be consumed by `preconditions_hold` on re-entry: re-entry resumes at
        `remaining_units` and never replays `completed_units`.

        A build-running worker MUST additionally record `integration_ref` on the attempt and write
        the full build output to `artifacts/logs/<run_id>/<attempt_id>.log`, persisting only that
        path (as `WorkerError.artifact_ref`) beside the tail (§3.3, §5 invariant 3).
        """

    @abstractmethod
    async def preconditions_hold(self, ctx: WorkerContext, payload: I) -> bool:
        """Checked on resume before re-entry — never blind replay (Constraint 7). Abstract rather
        than a defaulted `return True`, because a default that always admits re-entry IS the blind
        replay this method exists to forbid: `relocate.py` inheriting it would re-run a path rename
        over an already-renamed tree and produce `java/java/com/x`. A worker with genuinely no
        precondition writes `return True` explicitly, which is a visible claim in a diff.

        The contract: consult the phase's checkpoint via `ctx.db`; if it is `partial`, admit
        re-entry only for `remaining_units`; if its referenced paths no longer exist, or its
        `written_schema_version` mismatches, return False so the phase re-runs from
        `phases.base_ref`.

        **Called by `PhaseRunner._re_entry`, before the dispatch and only when a checkpoint
        exists** — deliberately not by `execute()`, whose ladder re-runs the same payload and
        which could act on neither answer. Named here because the caller is the half of this
        contract that went missing: the method was abstract, implemented by all eleven workers,
        and for its entire existence invoked by nothing at all, so every worker's re-entry check
        was dead code and "re-running completed work is a no-op" held only by accident.
        """

    def idempotency_key(self, ctx: WorkerContext, payload: I) -> str:
        """This invocation's identity — a function of WHAT is being asked, never of when or how
        often. Content-addressed exactly as the §3.2 step 6 `Fleet-Patch-Id` is. The runner SKIPS
        any invocation whose key already appears as a committed `Fleet-Patch-Id` on
        `migrate/<repo>` and emits `already_applied` (§12.23); that skip — not the worker's own
        care — is what makes a partial re-run free. Override only to widen the key, never to
        narrow it.
        """
        return sha256(
            f"{ctx.run_id}|{ctx.repo_id}|{self.phase}|{payload.model_dump_json()}".encode()
        ).hexdigest()

    async def on_cancel(self, ctx: WorkerContext) -> None:
        """Called by the runner once `ctx.cancel` is set and run() has not returned inside the
        grace window. Default: no-op, which is correct only for a worker owning nothing external.
        A worker holding a subprocess or a pool future MUST override it to kill the process group
        and release the worktree lock — `CancelledError` never reaches a pool child (§11.1).
        """
        return None

    # ------------------------------------------------------------------ the ADR-0014 ladder

    async def execute(
        self,
        ctx: WorkerContext,
        payload: I,
        *,
        max_attempts: int | None = None,
        ladder: Sequence[ContextPolicy | None] = DEFAULT_LADDER,
    ) -> WorkerExecution[O]:
        """Run `run()` from rung `ctx.attempt` onward. Never raises for worker failure.

        **The ladder STARTS where the caller says it starts.** `ctx.attempt` is the runner's
        persisted `phases.attempts + 1` — the durable position a `fleet resume` reads — and every
        rung this invocation runs is numbered from it. Nothing here counts attempts to decide
        which rung to run; the local counter below only says how many rungs THIS invocation has
        spent, which is what the ceiling and `WorkerExecution.attempts` are about.

        `ladder` is the ADR-0021 CONTEXT ladder (`tier_for_attempt`/`context_policy_for_attempt`
        are indexed by it, not by a module constant) — the runner's one call site
        (`orchestrator/runner.py::_dispatch`) passes `LadderState.ladder`, itself sourced from
        `FleetSettings.config.transform.ladder` (§9), so a per-run `--context-policy` override
        (§10) reaches the rung actually dispatched. Defaults to `DEFAULT_LADDER` for every other
        caller (tests, mainly), which is byte-identical to the settings default (§9).

        Why that distinction is the whole point: the runner dispatches one rung per invocation
        (`max_attempts=1`) so each rung's outcome is in SQLite before the next begins (§11.5).
        With the rung derived from a fresh in-process counter, every one of those dispatches was
        attempt 1 at `DETERMINISTIC` — the persisted ladder climbed and the work never did, so
        ADR-0014 escalation could not reach an LLM rung even once.

        `max_attempts` is the OWNING TASK's ceiling (`TransformTask.max_attempts`, §5.4), passed
        in per invocation rather than compiled into a field constraint, and clamped down — never
        up — by this worker's own `retries`. It bounds the ABSOLUTE attempt number, so a resume
        that starts at rung 3 of 3 runs that rung and stops instead of starting a new ladder.
        Only `asyncio.CancelledError` propagates: a cancelled wave is the runner's decision, not
        a repo-level failure, and must not be recorded as an attempt.
        """
        ceiling = self._attempt_ceiling(max_attempts)
        started = loop_now()

        start_attempt = max(ctx.attempt, 1)
        attempts = 0
        results: list[WorkerResult[O]] = []
        tiers: list[TransformTier] = []
        usage = TokenUsage()
        status = RepoStatus.RUNNING

        # A lease reclaimed BEFORE any work starts: abort without touching git (§11.5).
        if not await self._fence_is_current(ctx):
            return self._finish(ctx, ceiling, started, status=RepoStatus.PENDING, fence_stale=True)

        while True:
            now = loop_now()
            if ctx.expired(now):
                # The ceiling is fail-closed and checked BEFORE the work, not only after: a
                # subprocess launched past the deadline outlives the wave that would reap it.
                results.append(self._refusal("timeout", FailureClass.TIMEOUT, "deadline passed"))
                status = RepoStatus.PENDING
                break
            if ctx.cancelled():
                results.append(self._refusal("cancelled", FailureClass.TIMEOUT, "cancelled"))
                status = RepoStatus.PENDING
                break

            # THE line the defect lived on. The rung is a function of the ladder position the
            # caller handed in, not of how many times this coroutine has been round its own loop.
            attempt = start_attempt + attempts
            tier = tier_for_attempt(attempt, ladder)
            attempt_ctx = replace(
                ctx,
                attempt=attempt,
                tier=tier,
                context_policy=context_policy_for_attempt(attempt, ladder),
            )
            result = await self._run_one(attempt_ctx, payload)

            # The fence is re-read AFTER the work: a reclaim mid-flight means this result describes
            # a worktree we no longer own. It is DISCARDED whole — not merged, not billed — because
            # merging it would interleave two owners' writes on one repo (§11.5).
            if not await self._fence_is_current(ctx):
                return self._finish(
                    ctx, ceiling, started, status=RepoStatus.PENDING, fence_stale=True
                )

            results.append(result)
            usage = accumulate(usage, result.usage)

            if result.status == "ok":
                attempts += 1
                tiers.append(tier)
                status = RepoStatus.SUCCEEDED
                break

            if result.status == "partial":
                # Landed work is real work: it costs an attempt, its output is the checkpoint, and
                # re-entry resumes at `remaining_units` rather than replaying `completed_units`.
                attempts += 1
                tiers.append(tier)
                status = RepoStatus.PENDING
                break

            if result.status == "cancelled":
                # The runner's decision, not the repo's failure. Not an attempt.
                status = RepoStatus.PENDING
                break

            error = result.error or WorkerError(
                failure_class=FailureClass.UNKNOWN, retryable=False
            )

            # `TRANSIENT_INFRA` is NOT re-run here. It is not an attempt either (ADR-0014), but
            # the entity that gets to say so is the one that can PERSIST having said so:
            # `RetryPolicy` returns `RETRY_TRANSIENT`, and the runner re-dispatches the same rung
            # under the same lease with `phases.transient_retries` bumped on disk. A second
            # budget in here did not agree with that one, it multiplied it — 5 free re-runs
            # inside 4 is 30 calls at an endpoint that already said no.

            # The infrastructure failed, not the repo: never an attempt, and terminal for the RUN
            # (§11.8, exit 8). The repo stays PENDING so `fleet resume` picks it up untouched.
            if error.failure_class is FailureClass.BACKEND_UNAVAILABLE:
                status = RepoStatus.PENDING
                break

            attempts += 1
            tiers.append(tier)

            if self._budget_exhausted(ctx, usage):
                # §11.2: the ceiling is fail-closed. Advancing the ladder would only spend money
                # we have already refused to spend.
                result.error = WorkerError(
                    failure_class=FailureClass.BUDGET_EXHAUSTED,
                    retryable=False,
                    exit_code=error.exit_code,
                    stderr_tail=error.stderr_tail,
                    artifact_ref=error.artifact_ref,
                    exception_type=error.exception_type,
                )
                status = RepoStatus.REQUIRES_HUMAN_INTERVENTION
                break

            if not error.retryable:
                # THE branch that must never be a string match on the message: a structural
                # failure re-run at a higher tier is the same failure at a higher price.
                status = RepoStatus.REQUIRES_HUMAN_INTERVENTION
                break

            if attempt >= ceiling:
                # Terminal (ADR-0014), and the test is the ABSOLUTE rung number rather than this
                # invocation's own count: a resume handed rung 3 of 3 has one rung left, not
                # three. The attempt past the ceiling is never issued; the fleet moves on and the
                # scheduler marks the dependents `blocked_by`.
                status = RepoStatus.REQUIRES_HUMAN_INTERVENTION
                break

            if ctx.expired(loop_now()):
                # Out of wall clock, not out of ladder: re-queue rather than burn the last rung
                # on an invocation that cannot finish.
                status = RepoStatus.PENDING
                break

        return self._finish(
            ctx,
            ceiling,
            started,
            status=status,
            attempts=attempts,
            tiers=tiers,
            results=results,
            usage=usage,
        )

    # ------------------------------------------------------------------ internals

    def _attempt_ceiling(self, max_attempts: int | None) -> int:
        """The owning task's ceiling, clamped by this worker's own `retries` (lower, never higher).
        A worker may lower the ADR-0014 ceiling; nothing may raise it above the task's number."""
        requested = MAX_ATTEMPTS if max_attempts is None else max_attempts
        if requested < 1:
            raise ValueError(f"{self.name}: max_attempts must be >= 1, got {requested}")
        if self.retries < 1:
            raise ValueError(f"{self.name}: retries must be >= 1, got {self.retries}")
        return min(requested, self.retries)

    def _finish(
        self,
        ctx: WorkerContext,
        ceiling: int,
        started: float,
        *,
        status: RepoStatus,
        attempts: int = 0,
        tiers: list[TransformTier] | None = None,
        results: list[WorkerResult[O]] | None = None,
        usage: TokenUsage | None = None,
        fence_stale: bool = False,
    ) -> WorkerExecution[O]:
        """Assemble the execution record. A fence-stale execution carries NO results and NO
        usage — discarding means discarding, including the bill."""
        return WorkerExecution[O](
            worker=self.name,
            phase=self.phase,
            repo_id=ctx.repo_id,
            status=status,
            attempts=attempts,
            max_attempts=ceiling,
            tiers=[] if fence_stale else (tiers or []),
            results=[] if fence_stale else (results or []),
            usage=TokenUsage() if fence_stale else (usage or TokenUsage()),
            duration_ms=int((loop_now() - started) * 1000),
            fence_stale=fence_stale,
        )

    def _refusal(self, status: WorkerStatus, failure_class: FailureClass, why: str) -> (
        WorkerResult[O]
    ):
        """A result recorded WITHOUT calling `run()`. Not an attempt — nothing was attempted."""
        result: WorkerResult[O] = WorkerResult(
            status=status,
            error=WorkerError(failure_class=failure_class, retryable=True, stderr_tail=why),
        )
        return result

    async def _fence_is_current(self, ctx: WorkerContext) -> bool:
        """Re-read the lease from the read-only repository. A phase row that does not exist yet
        cannot have been reclaimed, so it is current by construction."""
        row = await ctx.db.get_phase(str(ctx.run_id), ctx.repo_id, self.phase)
        if row is None:
            return True
        return ctx.fence_is_current(row.lease_fence, row.lease_owner)

    async def _run_one(self, ctx: WorkerContext, payload: I) -> WorkerResult[O]:
        """One invocation of `run()`, deadline- and cancel-bounded, with every escape classified.

        The bounding is cooperative on purpose. `run()` is driven as a task and merely *watched*:
        when the deadline passes or `ctx.cancel` is set, `run()` is given `cancel_grace_s` to
        reach a unit boundary and return — typically `status='partial'`, which is the whole point.
        Only if it does not is `on_cancel()` called to kill what it owns, and only then is the
        task hard-cancelled. Cancelling first would abandon a `git` child mid-write.
        """
        started = loop_now()
        run_task = asyncio.create_task(self.run(ctx, payload))
        cancel_watch = asyncio.create_task(ctx.cancel.wait())
        try:
            await asyncio.wait(
                {run_task, cancel_watch},
                timeout=ctx.time_left(loop_now()),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not run_task.done():
                # Deadline and cancel converge here: both mean "stop owning things".
                timed_out = ctx.expired(loop_now())
                ctx.cancel.set()
                if not await self._settled(run_task, self.cancel_grace_s):
                    await self.on_cancel(ctx)
                if not await self._settled(run_task, self.cancel_grace_s):
                    run_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await run_task
                    return self._abandoned(timed_out, started)
            result = run_task.result()
        except asyncio.CancelledError:
            run_task.cancel()  # a cancelled wave must not leak the child task
            raise  # a cancelled wave is not a repo failure
        except Exception as exc:  # every escape is classified, none is swallowed (Rule 11)
            result = WorkerResult(status="failed", error=error_from_exception(exc))
        finally:
            cancel_watch.cancel()
            with suppress(asyncio.CancelledError):
                await cancel_watch
        if result.duration_ms == 0:
            result.duration_ms = int((loop_now() - started) * 1000)
        return result

    @staticmethod
    async def _settled(task: asyncio.Task[WorkerResult[O]], grace_s: float) -> bool:
        """Wait up to `grace_s` for `run()` to return at a unit boundary. `True` if it did."""
        done, _ = await asyncio.wait({task}, timeout=grace_s)
        return bool(done)

    def _abandoned(self, timed_out: bool, started: float) -> WorkerResult[O]:
        """`run()` ignored both the deadline and `on_cancel()`. Loud, and typed: whatever it owns
        is now an orphan, which is a worker defect and must not read as a repo failure."""
        status: WorkerStatus = "timeout" if timed_out else "cancelled"
        failure_class = FailureClass.TIMEOUT if timed_out else FailureClass.TRANSIENT_INFRA
        result: WorkerResult[O] = WorkerResult(
            status=status,
            error=WorkerError(
                failure_class=failure_class,
                retryable=True,
                stderr_tail=(
                    f"{self.name}: run() did not return within {self.cancel_grace_s}s of "
                    f"on_cancel(); the task was cancelled and anything it owned is an orphan"
                ),
            ),
            duration_ms=int((loop_now() - started) * 1000),
        )
        return result

    def _budget_exhausted(self, ctx: WorkerContext, usage: TokenUsage) -> bool:
        """Fail-closed against BOTH ceilings: this worker's per-invocation `WorkerBudget` and what
        the run has LEFT (`ctx.budget`, §11.2). `TokenUsage` stores no total, so the sum is
        computed from the fields that exist — the reason this predicate exists at all."""
        spent_tokens = total_tokens(usage)
        return (
            spent_tokens > self.budget.max_tokens
            or usage.cost_usd > self.budget.max_cost_usd
            or spent_tokens > ctx.budget.remaining_tokens
            or usage.cost_usd > ctx.budget.remaining_usd
        )

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={getattr(self, 'name', '?')!r}>"


def implements_preconditions(cls: type[BaseWorker[Any, Any]]) -> bool:
    """Does this worker class make the §7.1 precondition claim explicitly?

    A concrete worker must either OVERRIDE `preconditions_hold` or remain abstract. Inheriting the
    abstract declaration and being instantiable anyway is impossible by construction — which is
    exactly why the method is abstract rather than a defaulted `return True`.
    """
    return inspect.isabstract(cls) or cls.preconditions_hold is not BaseWorker.preconditions_hold
