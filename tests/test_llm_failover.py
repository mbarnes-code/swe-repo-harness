"""§11.8's `BackendHealth` circuit breaker + same-target `RATE_LIMIT` backoff (ADR-0132), closing
§12.43 case (ii).

Same fake-backend / recorded-call-log pattern as `tests/test_llm_client.py`: every test drives a
scripted `ModelBackend`, nothing opens a socket, and the call log is asserted against directly so
"the same target was retried" / "the target was skipped" are observations, not inferences from a
final answer alone.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import ClassVar

import pytest
from pydantic import BaseModel

from fleet.llm import client as client_module
from fleet.llm.client import (
    BackendFailover,
    BackendReply,
    CallPolicy,
    LadderModelClient,
    Message,
    ModelResponse,
    StructuredOutputMode,
    TierUnavailable,
    TransportError,
    UnknownRole,
)
from fleet.llm.failover import BackendHealthTransition
from fleet.models.enums import ModelTier
from fleet.models.tasks import BackendTarget, ModelCapabilities, Price, TokenUsage
from fleet.orchestrator.retry import RetryPolicy

ROLE = "transform_repair"


class Answer(BaseModel):
    verdict: str
    score: int


# ---------------------------------------------------------------------------------------------
# Fakes — same shape as test_llm_client.py's FakeBackend/FakeRouter
# ---------------------------------------------------------------------------------------------


class FakeBackend:
    """One transport, scripted. Records every turn so a test can assert WHICH target was called,
    in what order, and how many times — the skip/probe assertions are meaningless without this."""

    name: ClassVar[str] = "fake"
    version: ClassVar[int] = 1

    def __init__(
        self,
        script: Sequence[BackendReply | Exception],
        caps: ModelCapabilities | None = None,
    ) -> None:
        self._script = list(script)
        self._caps = caps or ModelCapabilities(supports_json_schema=True, max_output_tokens=8192)
        self.calls: list[dict[str, object]] = []

    def declared_capabilities(self, target: BackendTarget) -> ModelCapabilities:
        return self._caps

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
        self.calls.append({"model_id": target.model_id})
        if not self._script:
            raise AssertionError(
                f"fake backend called more times than the script allows "
                f"(target={target.model_id!r}, calls so far={self.calls!r})"
            )
        nxt = self._script.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


class FakeRouter:
    def __init__(self, tier: ModelTier, targets: Sequence[BackendTarget]) -> None:
        self._route = client_module.TierRoute(tier=tier, targets=tuple(targets))

    def resolve(
        self,
        role: str,
        *,
        tier_override: ModelTier | None = None,
    ) -> client_module.TierRoute:
        if role != ROLE:
            raise UnknownRole(role)
        return self._route


class MutableClock:
    """A clock a test can advance between calls — `time.monotonic` stood in for. Needed for the
    cooldown/`HALF_OPEN` tests, unlike `test_llm_client.py`'s fixed-value clocks."""

    def __init__(self, start: float = 0.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value


def target(model_id: str) -> BackendTarget:
    return BackendTarget(
        backend="fake",
        model_id=model_id,
        price=Price(in_per_mtok=1.0, out_per_mtok=2.0),
    )


def ok_reply() -> BackendReply:
    return BackendReply(
        text=json.dumps({"verdict": "migrate", "score": 7}),
        usage=TokenUsage(input_tokens=100, output_tokens=40),
        finish_reason="stop",
    )


#: Zero delay, by default: NONE of these tests should spend real wall-clock seconds on §11.8's
#: production-tuned backoff schedule. Tests that care about the RETRY COUNT (not the delay) pass
#: their own `max_transient_retries` on top of this.
INSTANT_RETRIES = RetryPolicy(backoff_base_s=0.0, backoff_cap_s=0.0)


def build_client(
    backend: FakeBackend,
    targets: Sequence[BackendTarget],
    *,
    policy: CallPolicy | None = None,
    retry_policy: RetryPolicy | None = None,
    failovers: list[BackendFailover] | None = None,
    health_transitions: list[BackendHealthTransition] | None = None,
    clock: Callable[[], float] = lambda: 0.0,
) -> LadderModelClient:
    return LadderModelClient(
        FakeRouter(ModelTier.WORKHORSE, targets),
        {"fake": backend},
        policy=policy or CallPolicy(),
        retry_policy=retry_policy or INSTANT_RETRIES,
        on_failover=None if failovers is None else failovers.append,
        on_health_transition=None if health_transitions is None else health_transitions.append,
        clock=clock,
    )


def call(client: LadderModelClient, **kwargs: object) -> ModelResponse[Answer]:
    messages = [Message(role="user", content="does this repo migrate?")]
    return asyncio.run(client.complete(ROLE, messages, Answer, **kwargs))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------------------------


def test_a_single_429_absorbed_by_backoff_does_not_open_the_breaker() -> None:
    """WHY (§12.43 case (ii)'s first clause, and §11.8's "never throttling alone"): one 429 is
    ordinary backpressure. It must be retried on the SAME target and must never reach the breaker
    at all — no failover, no health transition, no second target touched."""
    backend = FakeBackend([TransportError("slow down", trigger="RATE_LIMIT"), ok_reply()])
    failovers: list[BackendFailover] = []
    transitions: list[BackendHealthTransition] = []
    client = build_client(
        backend,
        [target("m1"), target("m2")],
        failovers=failovers,
        health_transitions=transitions,
    )

    response = call(client)

    assert [c["model_id"] for c in backend.calls] == ["m1", "m1"], "retried the SAME target"
    assert failovers == [], "a single absorbed 429 must never fail over"
    assert transitions == [], "a single absorbed 429 must never touch the breaker at all"
    assert response.usage.model_id == "m1"


def test_exhausting_the_entire_backoff_schedule_is_one_qualifying_failure() -> None:
    """WHY (§12.43 case (ii)'s literal text): a target that answers 429 through its ENTIRE
    backoff schedule — not one, not some — is what counts as ONE qualifying failure, and with
    `open_after_failures=1` that single qualifying failure is enough to mark it `DOWN`."""
    retry_policy = RetryPolicy(max_transient_retries=1, backoff_base_s=0.0, backoff_cap_s=0.0)
    backend = FakeBackend(
        [
            TransportError("slow down", trigger="RATE_LIMIT"),  # initial attempt
            TransportError("still slow", trigger="RATE_LIMIT"),  # the one retry — schedule spent
            ok_reply(),  # m2 answers
        ]
    )
    failovers: list[BackendFailover] = []
    transitions: list[BackendHealthTransition] = []
    client = build_client(
        backend,
        [target("m1"), target("m2")],
        policy=CallPolicy(open_after_failures=1),
        retry_policy=retry_policy,
        failovers=failovers,
        health_transitions=transitions,
    )

    response = call(client)

    assert [c["model_id"] for c in backend.calls] == ["m1", "m1", "m2"]
    assert len(failovers) == 1
    assert failovers[0].trigger == "RATE_LIMIT"
    assert len(transitions) == 1, "the qualifying failure opens the breaker exactly once"
    assert (transitions[0].from_state, transitions[0].to_state) == ("UP", "DOWN")
    assert response.usage.model_id == "m2"


def test_a_down_target_is_skipped_not_called_again() -> None:
    """WHY (§12.43 case (ii)): once `open_after_failures` is reached, the target is skipped —
    immediate failover, no call at all — until `cooldown_s` elapses. The fake's script has no
    entries left for `m1`'s second turn, so a call there would fail the test by itself."""
    retry_policy = RetryPolicy(max_transient_retries=0, backoff_base_s=0.0, backoff_cap_s=0.0)
    backend = FakeBackend(
        [
            TransportError("slow down", trigger="RATE_LIMIT"),  # m1: exhausts on the FIRST try
            ok_reply(),  # m2 answers call 1
            ok_reply(),  # m2 answers call 2 — m1 must not be invoked at all this time
        ]
    )
    client = build_client(
        backend,
        [target("m1"), target("m2")],
        policy=CallPolicy(open_after_failures=1, cooldown_s=999.0),
        retry_policy=retry_policy,
    )

    call(client)  # m1 fails once -> DOWN (open_after_failures=1)
    call(client)  # m1 must be skipped this time — cooldown nowhere near elapsed

    assert [c["model_id"] for c in backend.calls] == ["m1", "m2", "m2"]


def test_cooldown_lets_exactly_one_half_open_probe_through_then_success_resets_to_up() -> None:
    """WHY (§12.43 case (ii), §11.8): after `cooldown_s`, exactly the NEXT call to the DOWN
    target is let through as a probe; a successful probe resets it to `UP`."""
    clock = MutableClock(0.0)
    retry_policy = RetryPolicy(max_transient_retries=0, backoff_base_s=0.0, backoff_cap_s=0.0)
    backend = FakeBackend(
        [
            TransportError("slow down", trigger="RATE_LIMIT"),  # m1 -> DOWN at t=0
            ok_reply(),  # m2 answers call 1 (t=0)
            ok_reply(),  # m2 answers call 2 (t=5, still within cooldown)
            ok_reply(),  # m1's HALF_OPEN probe succeeds (t=11, cooldown elapsed)
        ]
    )
    transitions: list[BackendHealthTransition] = []
    client = build_client(
        backend,
        [target("m1"), target("m2")],
        policy=CallPolicy(open_after_failures=1, cooldown_s=10.0),
        retry_policy=retry_policy,
        health_transitions=transitions,
        clock=clock,
    )

    call(client)  # t=0: m1 fails -> DOWN
    clock.value = 5.0
    call(client)  # t=5: cooldown (10s) not elapsed — m1 skipped
    clock.value = 11.0
    response = call(client)  # t=11: cooldown elapsed — m1 gets its one probe, and it succeeds

    assert [c["model_id"] for c in backend.calls] == ["m1", "m2", "m2", "m1"]
    assert response.usage.model_id == "m1", "the probe succeeded — m1 answers directly, no hop"
    assert [t.to_state for t in transitions] == ["DOWN", "HALF_OPEN", "UP"]


def test_a_failed_half_open_probe_restarts_the_cooldown() -> None:
    """WHY (§11.8): a probe is a chance, not a pardon. If it fails, the target goes straight back
    to `DOWN` and the cooldown clock restarts from the probe's failure, not from the original."""
    clock = MutableClock(0.0)
    retry_policy = RetryPolicy(max_transient_retries=0, backoff_base_s=0.0, backoff_cap_s=0.0)
    backend = FakeBackend(
        [
            TransportError("slow down", trigger="RATE_LIMIT"),  # m1 -> DOWN at t=0
            ok_reply(),  # m2 answers call 1
            TransportError("still slow", trigger="RATE_LIMIT"),  # m1's probe at t=11 FAILS
            ok_reply(),  # m2 answers call 2 (same complete(), after the failed probe)
            ok_reply(),  # m2 answers call 3 (t=15 — m1 must still be skipped)
        ]
    )
    transitions: list[BackendHealthTransition] = []
    client = build_client(
        backend,
        [target("m1"), target("m2")],
        policy=CallPolicy(open_after_failures=1, cooldown_s=10.0),
        retry_policy=retry_policy,
        health_transitions=transitions,
        clock=clock,
    )

    call(client)  # t=0: m1 -> DOWN
    clock.value = 11.0
    response = call(client)  # t=11: probe fails -> back to DOWN, cooldown restarts from t=11
    assert response.usage.model_id == "m2"
    assert [t.to_state for t in transitions] == ["DOWN", "HALF_OPEN", "DOWN"]

    clock.value = 15.0  # only 4s since the SECOND down (t=11); 10s cooldown, must still skip
    call(client)
    assert [c["model_id"] for c in backend.calls] == ["m1", "m2", "m1", "m2", "m2"]


def test_backend_health_does_not_persist_across_a_fresh_client_instance() -> None:
    """WHY (§11.8's own explicit requirement): "a resumed run re-probes rather than inheriting a
    stale verdict" — a fresh `LadderModelClient` (what a resumed run constructs) must have no
    memory of a target another instance drove `DOWN`. Asserted directly: a fresh client tries the
    target fresh rather than skipping it."""
    retry_policy = RetryPolicy(max_transient_retries=0, backoff_base_s=0.0, backoff_cap_s=0.0)
    targets = [target("m1"), target("m2")]

    backend_a = FakeBackend([TransportError("slow down", trigger="RATE_LIMIT"), ok_reply()])
    client_a = build_client(
        backend_a, targets, policy=CallPolicy(open_after_failures=1), retry_policy=retry_policy
    )
    call(client_a)  # m1 -> DOWN on client_a

    # A fresh client — what `fleet resume` constructs — has never seen m1 fail.
    backend_b = FakeBackend([ok_reply()])
    client_b = build_client(
        backend_b, targets, policy=CallPolicy(open_after_failures=1), retry_policy=retry_policy
    )
    response = call(client_b)

    assert [c["model_id"] for c in backend_b.calls] == ["m1"], "tried fresh, not skipped"
    assert response.usage.model_id == "m1"


def test_tier_unavailable_is_unaffected_for_a_genuinely_exhausted_tier() -> None:
    """WHY (regression guard): every target down or newly failing must still raise
    `TierUnavailable` with the same shape as before the breaker existed — the fail-closed exit-8
    path this fix must not touch. A SECOND call, with both targets already `DOWN`, must still
    raise the identical exception and name both targets in `targets_tried`, even though this time
    neither target is actually invoked (both are skipped as `DOWN`)."""
    retry_policy = RetryPolicy(max_transient_retries=0, backoff_base_s=0.0, backoff_cap_s=0.0)
    backend = FakeBackend(
        [
            TransportError("refused", trigger="CONNECTION"),
            TransportError("refused", trigger="CONNECTION"),
        ]
    )
    client = build_client(
        backend,
        [target("m1"), target("m2")],
        policy=CallPolicy(open_after_failures=1),
        retry_policy=retry_policy,
    )

    with pytest.raises(TierUnavailable) as excinfo:
        call(client)
    assert excinfo.value.targets_tried == ("fake:m1", "fake:m2")
    assert len(backend.calls) == 2

    with pytest.raises(TierUnavailable) as excinfo2:
        call(client)
    assert excinfo2.value.targets_tried == ("fake:m1", "fake:m2"), (
        "both targets are still named as tried, even though both were skipped as DOWN this time"
    )
    assert len(backend.calls) == 2, "no new calls — both targets were DOWN and skipped"


def test_schema_unsatisfied_never_opens_the_breaker() -> None:
    """WHY (§11.8's own scoping): a model that cannot produce the schema is a negotiation-ladder
    problem (§7.7), not evidence the endpoint is unreachable. `SCHEMA_UNSATISFIED` must fail the
    target over exactly as before, but must NEVER count as a qualifying failure toward
    `open_after_failures` — the breaker is about connectivity, not schema compliance."""
    invalid = BackendReply(
        text=json.dumps({"verdict": "migrate", "score": "not-an-int"}),
        usage=TokenUsage(input_tokens=100, output_tokens=20),
        finish_reason="stop",
    )
    backend = FakeBackend([invalid, invalid, ok_reply()])
    transitions: list[BackendHealthTransition] = []
    client = build_client(
        backend,
        [target("m1"), target("m2")],
        policy=CallPolicy(open_after_failures=1, max_schema_repairs=1),
        health_transitions=transitions,
    )

    response = call(client)

    assert [c["model_id"] for c in backend.calls] == ["m1", "m1", "m2"]
    assert transitions == [], "SchemaUnsatisfied is never a qualifying failure for the breaker"
    assert response.usage.model_id == "m2"


def test_schema_unsatisfied_during_a_half_open_probe_abandons_it_to_down_not_wedged() -> None:
    """Finding 1 (review round 1, a real bug): before this fix, `may_call` returned `False`
    unconditionally for `HALF_OPEN` and the only two exits were `record_success`/`record_failure`
    — so a probe resolving via `SchemaUnsatisfied` (an ORDINARY, CAUGHT outcome, not even a
    propagating exception) left the target wedged at `HALF_OPEN` FOREVER, silently, for the rest
    of the process. Proven directly: the probe fails via `SchemaUnsatisfied`, the target goes
    straight back to `DOWN` (not stuck), and — because this is not connectivity evidence —
    `down_since` is NOT reset, so the very next call is immediately eligible to probe again (no
    forced extra wait, unlike a genuine `TransportError` failure during a probe)."""
    clock = MutableClock(0.0)
    retry_policy = RetryPolicy(max_transient_retries=0, backoff_base_s=0.0, backoff_cap_s=0.0)
    invalid = BackendReply(
        text=json.dumps({"verdict": "migrate", "score": "not-an-int"}),
        usage=TokenUsage(input_tokens=100, output_tokens=20),
        finish_reason="stop",
    )
    backend = FakeBackend(
        [
            TransportError("slow down", trigger="RATE_LIMIT"),  # m1 -> DOWN at t=0
            ok_reply(),  # m2 answers call 1
            invalid,  # m1's probe at t=11: initial attempt
            invalid,  # m1's probe: one repair, still invalid -> SchemaUnsatisfied
            ok_reply(),  # m2 answers call 2 (m1 failed over via SchemaUnsatisfied)
            ok_reply(),  # m1's SECOND probe, same clock, succeeds -> UP
        ]
    )
    transitions: list[BackendHealthTransition] = []
    client = build_client(
        backend,
        [target("m1"), target("m2")],
        policy=CallPolicy(open_after_failures=1, cooldown_s=10.0, max_schema_repairs=1),
        retry_policy=retry_policy,
        health_transitions=transitions,
        clock=clock,
    )

    call(client)  # t=0: m1 -> DOWN
    clock.value = 11.0
    response = call(client)  # t=11: m1's probe fails via SchemaUnsatisfied

    assert response.usage.model_id == "m2"
    assert [t.to_state for t in transitions] == ["DOWN", "HALF_OPEN", "DOWN"]
    assert transitions[-1].reason.startswith("probe abandoned"), (
        "abandoned (no connectivity evidence), not treated as a qualifying failure"
    )

    # The actual proof it is not wedged: down_since was NOT reset by the abandon (the clock has
    # not moved), so the target is immediately eligible to probe again -- a genuine TransportError
    # failure during a probe would instead have restarted the 10s cooldown.
    response2 = call(client)
    assert response2.usage.model_id == "m1", "immediately probed again — no extra wait imposed"
    assert [t.to_state for t in transitions] == ["DOWN", "HALF_OPEN", "DOWN", "HALF_OPEN", "UP"]


def test_a_propagating_exception_during_a_half_open_probe_still_abandons_it_to_down() -> None:
    """Finding 1's second required proof: an exception that ESCAPES `complete()` entirely (never
    caught by the `SchemaUnsatisfied`/`TransportError` clause) must still resolve a `HALF_OPEN`
    probe before propagating. `BudgetExhausted` stands in for the whole class the review named
    (`OutputTruncated`/`ModelRefused`/`UnknownBackend`/a cancellation take the identical path,
    since `abandon_probe` is called from the same catch-all `except BaseException` regardless of
    which one fires)."""
    clock = MutableClock(0.0)
    retry_policy = RetryPolicy(max_transient_retries=0, backoff_base_s=0.0, backoff_cap_s=0.0)
    backend = FakeBackend(
        [
            TransportError("slow down", trigger="RATE_LIMIT"),  # m1 -> DOWN at t=0
            ok_reply(),  # m2 answers call 1
            ok_reply(),  # m1's probe succeeds on the FOLLOWING ordinary call
        ]
    )
    transitions: list[BackendHealthTransition] = []
    client = build_client(
        backend,
        [target("m1"), target("m2")],
        policy=CallPolicy(open_after_failures=1, cooldown_s=10.0),
        retry_policy=retry_policy,
        health_transitions=transitions,
        clock=clock,
    )

    call(client)  # t=0: m1 -> DOWN
    clock.value = 11.0

    # A budget too small for the probed target: `_check_budget` raises BEFORE any
    # `backend.invoke()`, so this exercises the propagating-exception exit door with zero script
    # consumption — `BudgetExhausted` is never caught by complete()'s
    # `(SchemaUnsatisfied, TransportError)` clause, so it propagates straight out.
    starving_budget = client_module.CallBudget(
        remaining_tokens=1_000_000, remaining_usd=0.000_000_001, deadline=1_000.0
    )
    with pytest.raises(client_module.BudgetExhausted):
        call(client, budget=starving_budget)

    # m1 was claimed as the HALF_OPEN probe before the raise; it must have been abandoned back
    # to DOWN rather than left permanently wedged.
    assert [t.to_state for t in transitions] == ["DOWN", "HALF_OPEN", "DOWN"]
    assert transitions[-1].reason.startswith("probe abandoned")

    # The actual proof it is not wedged: an ordinary next call (no starved budget) probes m1
    # again and succeeds, rather than skipping it forever.
    response = call(client)
    assert response.usage.model_id == "m1"
    assert [t.to_state for t in transitions] == ["DOWN", "HALF_OPEN", "DOWN", "HALF_OPEN", "UP"]


def test_backoff_is_genuinely_consulted_between_retries_not_a_busy_loop() -> None:
    """Finding 5 (review round 1, minor): the call log alone cannot tell "the client backed off
    between retries" apart from "the client busy-loops calling `invoke()` in a tight loop that
    happens to succeed on the Nth try" — both produce an identical sequence of `model_id`s. A spy
    wrapping `RetryPolicy.backoff_delay` proves `_call_target` genuinely consults the backoff
    primitive once per retry, with the retry index incrementing each time, rather than looping
    without ever asking how long to wait."""

    @dataclass(frozen=True, slots=True)
    class SpyRetryPolicy(RetryPolicy):
        calls: list[int] = field(default_factory=list, compare=False)

        def backoff_delay(self, retry_index: int) -> float:
            self.calls.append(retry_index)
            return 0.0  # still instant — this asserts CONSULTATION, not real timing

    spy = SpyRetryPolicy(max_transient_retries=3)
    backend = FakeBackend(
        [
            TransportError("slow down", trigger="RATE_LIMIT"),
            TransportError("slow down", trigger="RATE_LIMIT"),
            TransportError("slow down", trigger="RATE_LIMIT"),
            ok_reply(),
        ]
    )
    client = build_client(backend, [target("m1")], retry_policy=spy)

    response = call(client)

    assert spy.calls == [1, 2, 3], "backoff consulted once per retry, index incrementing"
    assert response.usage.model_id == "m1"
    assert [c["model_id"] for c in backend.calls] == ["m1", "m1", "m1", "m1"]


def test_record_success_resets_consecutive_failures_across_intervening_failures() -> None:
    """WHY: `record_success` clearing `consecutive_failures` is what makes "N qualifying failures
    in a row" (the module docstring's own words) mean literally that. Six CONNECTION failures
    total here, arranged 2-success-2-success, `open_after_failures=3`: a target that has never
    failed three times in a row without an intervening success must stay UP throughout. If the
    reset silently broke, `consecutive_failures` would carry over the first block's 2 instead of
    clearing to 0, so the SECOND block's very first failure would take the running count to 3
    (2 carried + 1) and open the breaker for a target that never actually failed twice
    back-to-back — the "throttling mistaken for an outage" shape §13 row 43 forbids, arriving by a
    different door than a 429. Regression for the audit's Mutation D (delete
    `state.consecutive_failures = 0` from `record_success`), which survived all of
    `test_llm_failover.py` + `test_llm_client.py` + `test_llm_findings.py` (46 tests, nothing
    fired) before this test was added."""
    retry_policy = RetryPolicy(max_transient_retries=0, backoff_base_s=0.0, backoff_cap_s=0.0)
    backend = FakeBackend(
        [
            TransportError("down", trigger="CONNECTION"),  # cf 0 -> 1
            TransportError("down", trigger="CONNECTION"),  # cf 1 -> 2
            ok_reply(),  # success -- cf must reset to 0 here
            TransportError("down", trigger="CONNECTION"),  # cf 0 -> 1 (or 2->3 if not reset)
            TransportError("down", trigger="CONNECTION"),  # cf 1 -> 2 (or already DOWN)
            ok_reply(),  # success -- must still be reachable and UP
        ]
    )
    transitions: list[BackendHealthTransition] = []
    client = build_client(
        backend,
        [target("m1")],  # single target: no failover to mask the breaker's own state
        policy=CallPolicy(open_after_failures=3, cooldown_s=999.0),
        retry_policy=retry_policy,
        health_transitions=transitions,
    )

    with pytest.raises(TierUnavailable):
        call(client)
    with pytest.raises(TierUnavailable):
        call(client)
    first_success = call(client)
    assert first_success.usage.model_id == "m1"
    with pytest.raises(TierUnavailable):
        call(client)
    with pytest.raises(TierUnavailable):
        call(client)
    second_success = call(client)

    assert second_success.usage.model_id == "m1", (
        "still UP and reachable after two more failures below the threshold -- "
        "the reset held across the intervening success"
    )
    assert transitions == [], (
        "no DOWN transition anywhere in this run -- consecutive_failures reset on every success, "
        "so the target never accumulated 3 in a row"
    )
