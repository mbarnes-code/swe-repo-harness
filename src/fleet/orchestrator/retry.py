"""The retry judgement: transient-vs-substantive, ladder advance, backoff (ADR-0014, §11.8).

`BaseWorker.execute()` owns the loop; this module owns the *decision* that feeds it, as one pure
function of `(LadderState, WorkerError)` so the policy can be tested without a worker, a clock,
or a network.

**`WorkerError.retryable` is what branches — never the message text.** Exit 1 from a build is a
repair prompt; exit 137 from the OOM killer is a re-queue; the two carry the same
`FailureClass.BUILD_ERROR` and can carry byte-identical stderr. Only the worker saw the exit code,
so only the worker can set `retryable`, and this module reads that flag and nothing else. A
classifier that greps stderr for "connection reset" is the regression this design refuses: it
misreads a *test* asserting on that string as an infrastructure fault and burns the repo's ladder
on a failure that was never transient.

**A transient failure is not an attempt** (§11.8). HTTP 429/5xx, connection resets, `SQLITE_BUSY`
and Docker daemon errors re-run the *same* rung after a jittered backoff and increment
`transient_retries`; `phases.attempts` is untouched. The rationale is ADR-0014's own: an endpoint
that refused to answer produced no evidence about the repo, so charging the repo one of its three
chances lets an outage consume the whole fleet's ladder budget while learning nothing.

**A genuine failure advances the ADR-0021 context ladder.** Rung index == attempt number: attempt
1 is deterministic (no prompt), attempt 2 is `EVIDENCE_ONLY`, attempt 3 is
`EVIDENCE_PLUS_REJECTED_APPROACHES`. Identical retries are how the reference material's "infinite
loop" failure mode begins, so the ladder never re-runs a rung it has already spent.

**Attempts count rungs that will actually run.** `LadderState.attempts` is the number of rungs
already *charged*, and `decide()` is handed the outcome of the rung that has just run and is not
yet charged — so a fresh repo whose deterministic attempt 1 failed calls in with `attempts=0`.
`TERMINATE` charges nothing: no further rung is issued, the runner already holds the failing
attempt row, and the repo's counter is not inflated by a decision to stop.

Backoff is **full jitter over an exponentially growing, capped ceiling** — `random() * min(base *
2**(n-1), cap)`. Full jitter rather than a fixed multiplier because 16 workers that all failed on
the same throttled endpoint must not return to it in lockstep, and capped because an unbounded
sleep inside a lease is a lease that expires while its holder is idle.
"""

from __future__ import annotations

import json
import random
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Final

from fleet.llm.client import FinishReason
from fleet.models.enums import ContextPolicy, FailureClass, RepoStatus, TransformTier
from fleet.models.tasks import DEFAULT_LADDER, MAX_ATTEMPTS
from fleet.workers.base import TIER_LADDER, WorkerError

__all__ = [
    "DEFAULT_BACKOFF_BASE_S",
    "DEFAULT_BACKOFF_CAP_S",
    "DEFAULT_MAX_TRANSIENT_RETRIES",
    "LadderState",
    "RetryAction",
    "RetryDecision",
    "RetryPolicy",
    "classify_reply",
]

#: §11.8: retried with exponential backoff + jitter inside the call, "by the SDK where possible
#: (`max_retries=4`)". The harness-side budget matches so the two layers agree on what "spent" is.
DEFAULT_MAX_TRANSIENT_RETRIES: Final = 4
DEFAULT_BACKOFF_BASE_S: Final = 0.5
DEFAULT_BACKOFF_CAP_S: Final = 30.0

class RetryAction(StrEnum):
    """What the runner does next. Three outcomes, and no fourth: a decision that is neither a
    retry nor an escalation nor a stop is a loop with no exit."""

    RETRY_TRANSIENT = "RETRY_TRANSIENT"  # same rung, after backoff; `attempts` unchanged
    ADVANCE_LADDER = "ADVANCE_LADDER"    # next rung, one attempt charged
    TERMINATE = "TERMINATE"              # the ladder stops; `terminal_status` says how


@dataclass(frozen=True, slots=True)
class LadderState:
    """The ADR-0014 counters for one `(repo, phase)`. Immutable: every decision returns the next
    state rather than mutating this one, so a decision can be inspected before it is applied."""

    attempts: int = 0
    transient_retries: int = 0
    max_attempts: int = MAX_ATTEMPTS
    ladder: tuple[ContextPolicy | None, ...] = DEFAULT_LADDER

    def context_policy(self, attempt: int) -> ContextPolicy | None:
        """ADR-0021: rung index == attempt number, 1-based. Past the declared ladder the last
        rung repeats rather than raising — a shortened ladder is config, not a crash."""
        if not self.ladder:
            return None
        return self.ladder[min(max(attempt, 1), len(self.ladder)) - 1]

    def tier(self, attempt: int) -> TransformTier:
        """The tier that rung runs at. Derived from the ladder (`workers.base.TIER_LADDER`), never
        declared twice: the tier is a consequence of which rung is running."""
        if not TIER_LADDER:
            return TransformTier.DETERMINISTIC
        return TIER_LADDER[min(max(attempt, 1), len(TIER_LADDER)) - 1]

    @property
    def exhausted(self) -> bool:
        return self.attempts >= self.max_attempts


@dataclass(frozen=True, slots=True)
class RetryDecision:
    """One judgement, fully explicit. `state` is the state AFTER applying it, so the runner
    persists `state.attempts` / `state.transient_retries` verbatim to the `phases` row."""

    action: RetryAction
    state: LadderState
    delay_s: float = 0.0
    context_policy: ContextPolicy | None = None
    tier: TransformTier = TransformTier.DETERMINISTIC
    terminal_status: RepoStatus | None = None
    reason: str = ""

    @property
    def charges_attempt(self) -> bool:
        """Did this decision spend a rung of the ladder? The property the `phases.attempts`
        column is written from, so "transient retries are not attempts" is checkable."""
        return self.action is RetryAction.ADVANCE_LADDER


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """ADR-0014's ladder policy. Stateless with respect to any one repo — the state is the
    `LadderState` passed in — so one instance serves the whole fleet."""

    max_transient_retries: int = DEFAULT_MAX_TRANSIENT_RETRIES
    backoff_base_s: float = DEFAULT_BACKOFF_BASE_S
    backoff_cap_s: float = DEFAULT_BACKOFF_CAP_S
    rng: random.Random = field(default_factory=random.Random, repr=False, compare=False)

    def backoff_delay(self, retry_index: int) -> float:
        """Full jitter over a capped exponential ceiling: `random() * min(base·2^(n-1), cap)`.

        Jittered so N workers that failed against one throttled endpoint do not return to it in
        lockstep, and capped so a retry can never sleep past the lease it is holding.
        """
        growth = 2.0 ** max(retry_index - 1, 0)
        ceiling = min(self.backoff_base_s * growth, self.backoff_cap_s)
        jitter: float = self.rng.random()
        return jitter * max(ceiling, 0.0)

    def decide(self, state: LadderState, error: WorkerError) -> RetryDecision:
        """The whole branch, in the order the properties must hold.

        `retryable` is consulted BEFORE the failure class, because that is the guarantee: a
        worker with mechanical evidence that another attempt cannot differ (an exit code, a
        probe) overrules any default the class carries, and no message is read at any point.
        """
        if not error.retryable:
            # Neither an attempt nor a rung: a structural failure re-run at a higher tier is the
            # same failure at a higher price, and no further rung will run to charge for.
            return RetryDecision(
                action=RetryAction.TERMINATE,
                state=state,
                terminal_status=self._terminal_status(error),
                context_policy=state.context_policy(state.attempts + 1),
                tier=state.tier(state.attempts + 1),
                reason=(
                    f"{error.failure_class} is not retryable (WorkerError.retryable=False); "
                    "the ladder stops without charging an attempt"
                ),
            )

        if error.failure_class is FailureClass.DISK_EXHAUSTED:
            # §11.3 / §13 row 42, and the same argument as the outage below: the volume is not
            # this repo's fault and the next repo would meet the identical floor. Charging a rung
            # would spend the fleet's ladders on a condition no attempt can change, so the repo
            # stays PENDING and the RUN halts (exit 9). Decided here rather than left to
            # `WorkerError.retryable` so the class means the same thing whichever worker raised
            # it — `NON_RETRYABLE` already says so, and a flag is not a second opinion.
            return RetryDecision(
                action=RetryAction.TERMINATE,
                state=state,
                terminal_status=RepoStatus.PENDING,
                context_policy=state.context_policy(state.attempts + 1),
                tier=state.tier(state.attempts + 1),
                reason="the volume is under preflight.min_free_bytes; the run halts and the repo "
                "stays PENDING — a full disk is not a repo failure",
            )

        if error.failure_class is FailureClass.BACKEND_UNAVAILABLE:
            # Terminal for the RUN, never for the repo (§11.8, exit 8): the repos are fine and
            # the infrastructure is not, so they stay PENDING for `fleet resume`.
            return RetryDecision(
                action=RetryAction.TERMINATE,
                state=state,
                terminal_status=RepoStatus.PENDING,
                context_policy=state.context_policy(state.attempts + 1),
                tier=state.tier(state.attempts + 1),
                reason="every target for the tier is DOWN; the run halts and the repo stays "
                "PENDING — an outage is not a repo failure",
            )

        if (
            error.failure_class is FailureClass.TRANSIENT_INFRA
            and state.transient_retries < self.max_transient_retries
        ):
            # Same rung, jittered backoff, `attempts` untouched (ADR-0014 / §11.8).
            nxt = replace(state, transient_retries=state.transient_retries + 1)
            attempt = state.attempts + 1  # the rung that just failed re-runs unchanged
            return RetryDecision(
                action=RetryAction.RETRY_TRANSIENT,
                state=nxt,
                delay_s=self.backoff_delay(nxt.transient_retries),
                context_policy=state.context_policy(attempt),
                tier=state.tier(attempt),
                reason=(
                    f"transient infrastructure failure {nxt.transient_retries}/"
                    f"{self.max_transient_retries}: same rung, no attempt charged"
                ),
            )

        # Substantive: the repo produced evidence, so it costs a rung. A transient failure past
        # its own cap lands here too — at that point the endpoint's behaviour IS the evidence.
        charged = replace(state, attempts=state.attempts + 1)
        if charged.exhausted:
            return RetryDecision(
                action=RetryAction.TERMINATE,
                state=charged,
                terminal_status=RepoStatus.REQUIRES_HUMAN_INTERVENTION,
                context_policy=state.context_policy(charged.attempts),
                tier=state.tier(charged.attempts),
                reason=(
                    f"attempt {charged.attempts}/{charged.max_attempts} failed and the ladder has "
                    "no further rung; the fleet continues and the dependents go blocked_by"
                ),
            )
        next_attempt = charged.attempts + 1
        return RetryDecision(
            action=RetryAction.ADVANCE_LADDER,
            state=charged,
            context_policy=charged.context_policy(next_attempt),
            tier=charged.tier(next_attempt),
            reason=(
                f"attempt {charged.attempts} failed ({error.failure_class}); advancing to rung "
                f"{next_attempt} — an identical retry is how the infinite-loop failure begins"
            ),
        )

    @staticmethod
    def _terminal_status(error: WorkerError) -> RepoStatus:
        """Where a non-retryable failure leaves the repo. Budget and disk exhaustion, a rejected
        preflight and a structural cycle are all a human's problem; nothing here is a retry."""
        if error.failure_class is FailureClass.BACKEND_UNAVAILABLE:
            return RepoStatus.PENDING
        return RepoStatus.REQUIRES_HUMAN_INTERVENTION


# --------------------------------------------------------------------------------------
# payload classification (§11.8) — the input to `retryable`, never a substitute for it
# --------------------------------------------------------------------------------------


def classify_reply(
    *,
    text: str | None,
    tool_arguments: Mapping[str, object] | None = None,
    finish_reason: FinishReason = "stop",
) -> WorkerError | None:
    """Classify a 200 OK's *payload*, because a transient API error can arrive as text inside one.

    Returns `None` when the payload is a candidate answer — validation is Pydantic's job, not
    this function's. Otherwise it returns a typed `WorkerError`, and the caller is left with a
    failure it can branch on rather than an empty string it will treat as a clean finish.

    The tests are structural on purpose (a JSON error envelope; an empty content block; a
    truncated `stop_reason`) rather than a keyword list: "error" appearing in a model's prose is
    not an outage, and a harness that decided otherwise would fail every repo whose build output
    it was asked to reason about.
    """
    envelope = _error_envelope(text)
    if envelope is not None:
        return WorkerError(
            failure_class=FailureClass.TRANSIENT_INFRA,
            retryable=True,
            stderr_tail=f"error envelope in a 200 OK body: {envelope}",
        )
    if finish_reason == "length":
        # NOT a failover trigger and NOT a schema violation (§7.7, §11.8): the same target is
        # re-asked with a raised `max_output_tokens` by the LLM layer. What this refuses to do is
        # hand a truncated body onward as if it were complete.
        return WorkerError(
            failure_class=FailureClass.PARSE_ERROR,
            retryable=True,
            stderr_tail="reply truncated (finish_reason='length'); retry the SAME target with a "
            "raised max_output_tokens (§7.7)",
        )
    if not (text or "").strip() and not tool_arguments:
        # "An unclassifiable empty response is a loud failure, never a clean finish" (§11.8).
        return WorkerError(
            failure_class=FailureClass.PARSE_ERROR,
            retryable=True,
            stderr_tail=f"empty content block (finish_reason={finish_reason!r})",
        )
    return None


def _error_envelope(text: str | None) -> str | None:
    """A provider error object serialized into the body: `{"error": ...}` / `{"type": "error"}`."""
    if not text:
        return None
    stripped = text.strip()
    if not stripped.startswith("{"):
        return None
    try:
        parsed = json.loads(stripped)
    except (ValueError, RecursionError):
        return None
    if not isinstance(parsed, dict):
        return None
    if "error" in parsed:
        return str(parsed["error"])[:200]
    if parsed.get("type") == "error":
        return str(parsed.get("message", parsed))[:200]
    return None
