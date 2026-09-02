"""Behaviour tests for `src/fleet/orchestrator/retry.py` — the ADR-0014 judgement (§11.8).

Two regressions are pinned here, and both are silent when they return.

**Counting a transient failure as an attempt.** A 429 or a `SQLITE_BUSY` produced no evidence
about the repo, so charging it a rung lets one endpoint outage consume the whole fleet's ladder
budget while learning nothing — every repo arrives at `REQUIRES_HUMAN_INTERVENTION` having never
actually been attempted. The assertions are therefore on the *counters*, not on the exception.

**Branching on message text.** `retryable` is the contract; stderr is not. Exit 1 from a build is
a repair prompt and exit 137 from the OOM killer is a re-queue, and they can carry byte-identical
messages. `test_decision_follows_retryable_not_the_message` hands the policy two failures whose
`stderr_tail` is character-for-character the same and asserts they are treated differently —
which no string matcher can pass.
"""

from __future__ import annotations

import random

import pytest

from fleet.models.enums import ContextPolicy, FailureClass, RepoStatus, TransformTier
from fleet.models.tasks import DEFAULT_LADDER
from fleet.orchestrator.retry import (
    DEFAULT_BACKOFF_CAP_S,
    LadderState,
    RetryAction,
    RetryPolicy,
    classify_reply,
)
from fleet.workers.base import WorkerError

IDENTICAL_MESSAGE = "connection reset by peer while running the integration suite"


def error(
    failure_class: FailureClass, *, retryable: bool, message: str = "boom"
) -> WorkerError:
    return WorkerError(failure_class=failure_class, retryable=retryable, stderr_tail=message)


# --------------------------------------------------------------------------------------
# transient vs substantive vs terminal
# --------------------------------------------------------------------------------------


def test_transient_failure_retries_without_incrementing_attempts() -> None:
    """A transient infrastructure failure re-runs the SAME rung and costs no attempt (§11.8).

    If it cost one, a throttled endpoint would spend all three of a repo's chances in under a
    minute and leave a perfectly healthy repo needing a human.
    """
    policy = RetryPolicy()
    state = LadderState(attempts=1, transient_retries=0)  # rung 2 is running and 429'd

    decision = policy.decide(state, error(FailureClass.TRANSIENT_INFRA, retryable=True))

    assert decision.action is RetryAction.RETRY_TRANSIENT
    assert decision.charges_attempt is False
    assert decision.state.attempts == 1, "a transient retry must not consume a ladder rung"
    assert decision.state.transient_retries == 1
    assert decision.tier is state.tier(2), "the SAME rung re-runs, not the next one"
    assert decision.delay_s > 0.0


def test_genuine_failure_increments_attempts_and_advances_the_rung() -> None:
    """Real evidence costs a rung, and the next rung must DIFFER: identical retries are how the
    reference material's infinite-loop failure mode begins (ADR-0021)."""
    policy = RetryPolicy()
    state = LadderState(attempts=0)  # the deterministic rung 1 has just failed, uncharged

    decision = policy.decide(state, error(FailureClass.RULE_MISS, retryable=True))

    assert decision.action is RetryAction.ADVANCE_LADDER
    assert decision.charges_attempt is True
    assert decision.state.attempts == 1, "rung 1 is charged; rung 2 is what runs next"
    assert decision.state.transient_retries == 0
    assert decision.context_policy is ContextPolicy.EVIDENCE_ONLY
    assert decision.context_policy is DEFAULT_LADDER[1], "rung index == attempt number"
    assert decision.tier is TransformTier.LLM_REPAIR


def test_non_retryable_failure_neither_retries_nor_advances() -> None:
    """A structural failure re-run at a higher tier is the same failure at a higher price, so it
    consumes nothing: no rung is issued, so no rung is charged."""
    policy = RetryPolicy()
    state = LadderState(attempts=1, transient_retries=2)

    decision = policy.decide(state, error(FailureClass.DEP_CONFLICT, retryable=False))

    assert decision.action is RetryAction.TERMINATE
    assert decision.charges_attempt is False
    assert decision.state.attempts == 1
    assert decision.state.transient_retries == 2
    assert decision.terminal_status is RepoStatus.REQUIRES_HUMAN_INTERVENTION


def test_decision_follows_retryable_not_the_message() -> None:
    """THE guard against regressing to string matching.

    Both failures carry the identical class and the identical stderr; only `retryable` differs.
    Any classifier that reads the message must return the same answer for both — so a passing
    run of this test is proof that the flag, and nothing else, is what branches.
    """
    policy = RetryPolicy()
    state = LadderState(attempts=0)

    retryable = policy.decide(
        state, error(FailureClass.BUILD_ERROR, retryable=True, message=IDENTICAL_MESSAGE)
    )
    terminal = policy.decide(
        state, error(FailureClass.BUILD_ERROR, retryable=False, message=IDENTICAL_MESSAGE)
    )

    assert retryable.action is RetryAction.ADVANCE_LADDER
    assert terminal.action is RetryAction.TERMINATE
    assert retryable.state.attempts == 1
    assert terminal.state.attempts == 0

    # And the mirror image: the message is a transient-sounding one, yet a non-retryable verdict
    # still terminates. A grep for "connection reset" would have retried it.
    assert terminal.reason != retryable.reason


def test_ladder_exhaustion_terminates_with_human_intervention() -> None:
    """The attempt past the ceiling is never issued: the fleet moves on and the scheduler marks
    the dependents `blocked_by` (§3.5). The ceiling is the task's number, never a constant."""
    policy = RetryPolicy()
    state = LadderState(attempts=2, max_attempts=3)

    decision = policy.decide(state, error(FailureClass.BUILD_ERROR, retryable=True))

    assert decision.action is RetryAction.TERMINATE
    assert decision.state.attempts == 3
    assert decision.terminal_status is RepoStatus.REQUIRES_HUMAN_INTERVENTION


def test_a_shortened_ladder_is_config_not_a_crash() -> None:
    """§9 makes the ladder per-run configurable, so a 2-rung ladder must terminate at 2 rather
    than index past its own tuple."""
    policy = RetryPolicy()
    state = LadderState(attempts=1, max_attempts=2, ladder=DEFAULT_LADDER[:2])

    decision = policy.decide(state, error(FailureClass.RULE_MISS, retryable=True))

    assert decision.action is RetryAction.TERMINATE
    assert decision.state.attempts == 2


def test_transient_retries_are_capped_and_then_become_a_real_attempt() -> None:
    """The transient budget is bounded: past it, the endpoint's behaviour IS the evidence, and
    the ladder advances rather than looping forever on a permanently broken endpoint."""
    policy = RetryPolicy(max_transient_retries=2)
    state = LadderState(attempts=1, transient_retries=2)

    decision = policy.decide(state, error(FailureClass.TRANSIENT_INFRA, retryable=True))

    assert decision.action is RetryAction.ADVANCE_LADDER
    assert decision.state.attempts == 2
    assert decision.state.transient_retries == 2, "the transient counter stops at its own cap"


def test_backend_unavailable_is_never_an_attempt_and_leaves_the_repo_pending() -> None:
    """The infrastructure failed, not the repo (§11.8, exit 8): the run halts and the repo is
    still PENDING on `fleet resume`. Marking it REQUIRES_HUMAN_INTERVENTION would libel it."""
    policy = RetryPolicy()
    state = LadderState(attempts=1)

    decision = policy.decide(state, error(FailureClass.BACKEND_UNAVAILABLE, retryable=True))

    assert decision.action is RetryAction.TERMINATE
    assert decision.charges_attempt is False
    assert decision.state.attempts == 1
    assert decision.terminal_status is RepoStatus.PENDING


# --------------------------------------------------------------------------------------
# backoff
# --------------------------------------------------------------------------------------


def test_backoff_is_jittered_bounded_and_never_a_thundering_herd() -> None:
    """Full jitter over a capped exponential ceiling.

    Unjittered backoff returns N workers to one throttled endpoint in lockstep, reproducing the
    429 that caused the wait; an uncapped one sleeps past the lease its holder is renewing.
    """
    policy = RetryPolicy(backoff_base_s=0.5, backoff_cap_s=DEFAULT_BACKOFF_CAP_S)

    for index in range(1, 12):
        ceiling = min(0.5 * 2 ** (index - 1), DEFAULT_BACKOFF_CAP_S)
        draws = [policy.backoff_delay(index) for _ in range(50)]
        assert all(0.0 <= d <= ceiling for d in draws), "a delay outside its own ceiling"
        assert max(draws) <= DEFAULT_BACKOFF_CAP_S, "the cap must bound every rung"
        assert len(set(draws)) > 1, "identical delays are a thundering herd by construction"

    assert policy.backoff_delay(1) <= policy.backoff_cap_s
    assert RetryPolicy(rng=random.Random(7)).backoff_delay(3) == pytest.approx(
        random.Random(7).random() * 2.0
    ), "the RNG is injectable, so a test never needs to sleep to be deterministic"


def test_backoff_growth_is_exponential_in_expectation() -> None:
    """Bounded is not the same as flat: later retries must actually wait longer, or a broken
    endpoint is hammered at a constant rate for the whole transient budget."""
    policy = RetryPolicy(rng=random.Random(11), backoff_base_s=0.5, backoff_cap_s=60.0)
    means = [
        sum(policy.backoff_delay(index) for _ in range(200)) / 200 for index in (1, 3, 5)
    ]
    assert means[0] < means[1] < means[2]


# --------------------------------------------------------------------------------------
# payload classification (§11.8)
# --------------------------------------------------------------------------------------


def test_an_error_envelope_inside_a_200_is_transient_not_an_answer() -> None:
    """A transient API error can arrive as text inside a 200 OK, so the payload is classified —
    treating it as a reply would feed a provider error object to the schema validator."""
    classified = classify_reply(text='{"error": {"type": "overloaded_error"}}')
    assert classified is not None
    assert classified.failure_class is FailureClass.TRANSIENT_INFRA
    assert classified.retryable is True


def test_an_empty_reply_is_a_loud_failure_never_a_clean_finish() -> None:
    """§11.8: an unclassifiable empty response is loud. Silently accepting it produces an
    `ok` result with no output, which the ladder reads as success and never retries."""
    classified = classify_reply(text="   ")
    assert classified is not None
    assert classified.failure_class is FailureClass.PARSE_ERROR
    assert classified.retryable is True


def test_a_truncated_reply_is_classified_but_stays_on_the_same_target() -> None:
    """`finish_reason == 'length'` is a property of the request, not the target (§7.7): it is
    refused as an answer here, and the LLM layer re-asks the SAME target with more tokens."""
    classified = classify_reply(text='{"value": 1', finish_reason="length")
    assert classified is not None
    assert classified.failure_class is FailureClass.PARSE_ERROR


def test_a_real_answer_is_not_classified_as_a_failure() -> None:
    """The classifier must not fire on prose that merely mentions an error — a repair prompt's
    whole subject is a build failure, and a harness that failed those calls would never repair."""
    assert classify_reply(text='{"summary": "the build error was a missing dependency"}') is None
    assert classify_reply(text="", tool_arguments={"patch": "diff"}) is None
