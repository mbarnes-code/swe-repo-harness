"""§11.8's per-target `BackendHealth` circuit breaker (ADR-0132, closing §12.43 case (ii)).

**What this module is not.** It is not the token-bucket/AIMD rate limiter (`llm.rate_limit.*`) —
that block stays `KNOWN_INERT` (D55's disclosed residual). This module is the REACTIVE half only:
once `llm/client.py`'s same-target backoff-retry arm has exhausted its own bound for a `RATE_LIMIT`
(the "qualifying failure" §12.43 case (ii) names), or a connection-level/5xx failure has already
given the transient layer nothing to retry, `LadderModelClient.complete()` reports that outcome
here and this module decides whether the target is still worth calling.

**Three states, one direction of travel each transition takes:**

* `UP` — the default. Called normally. `consecutive_failures` qualifying failures in a row without
  an intervening success moves it to `DOWN`.
* `DOWN` — skipped (immediate failover to the next target in the tier) until `cooldown_s` has
  elapsed since it went down, at which point exactly the NEXT call is let through as a probe and
  the state becomes `HALF_OPEN` for the duration of that one call.
* `HALF_OPEN` — a probe is in flight. A successful reply resets to `UP` (`consecutive_failures`
  cleared). A qualifying failure sends it back to `DOWN` and restarts the cooldown clock. Any OTHER
  outcome — `SchemaUnsatisfied`, `OutputTruncated`, `ModelRefused`, `BudgetExhausted`,
  `UnknownBackend`, a cancellation, anything that is not success and not a qualifying
  `TransportError` — abandons the probe (`abandon_probe`) straight back to `DOWN` WITHOUT
  restarting the cooldown: none of those outcomes is connectivity evidence, so the next call to
  this target is eligible to probe again immediately (§11.8: only a connection-level/5xx failure or
  an exhausted `RATE_LIMIT` schedule may ever extend how long a target stays unavailable). Without
  this, `may_call` returning `False` unconditionally for `HALF_OPEN` and only `record_success`/
  `record_failure` ever leaving it means a probe resolving any other way wedges the target at
  `HALF_OPEN` **permanently** — worse than never building the breaker at all, because every future
  call to every worker sharing this client silently skips a target nothing is actually wrong with.

**Never throttling alone (§11.8).** `record_failure` is called by `complete()` only on the two
events SPEC §11.8 names as DOWN-worthy: a connection-level failure, a 5xx that survived the
transient layer, or a `RATE_LIMIT` that has *already* walked its entire client-side backoff
schedule. A single 429 absorbed by that backoff and followed by a success never reaches this
module at all — from here it is indistinguishable from a target that was never throttled.
`SchemaUnsatisfied` (§11.8 trigger 4) is deliberately excluded too: a model that cannot produce
the schema is a negotiation-ladder problem (§7.7), not evidence the endpoint itself is unreachable,
and marking it `DOWN` would fail a healthy connection over for a reason connectivity cannot fix.

**In-memory, per-run, no persistence — by SPEC's own words, not an oversight.** "A resumed run
re-probes rather than inheriting a stale verdict, because the outage it recorded may have ended
hours ago" (§11.8). `BackendHealth` holds nothing but a plain dict and is held once per
`LadderModelClient`; a fresh client (what a resumed run constructs) starts every target at `UP`.

**Concurrency.** `LadderModelClient` serves one asyncio event loop; `may_call` claims the one
`HALF_OPEN` probe slot synchronously (no `await` between reading and mutating state), so two
coroutines racing `complete()` for the same target cannot both observe "cooldown elapsed" and both
be let through — the second sees the first's claim.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel

from fleet.models.enums import ModelTier
from fleet.models.tasks import BackendTarget

__all__ = ["BackendHealth", "BackendHealthTransition", "HealthState"]

HealthState = Literal["UP", "DOWN", "HALF_OPEN"]


class BackendHealthTransition(BaseModel):
    """One state change the breaker made, for observability only — never read back to decide
    anything (the breaker's own dict is the state; this is a record of it changing). Emitted for
    `UP -> DOWN`, `DOWN -> HALF_OPEN` (a probe was let through) and the two ways a probe resolves,
    `HALF_OPEN -> UP` (restored) and `HALF_OPEN -> DOWN` (cooldown restarts). Never emitted for a
    call that stayed `UP` throughout — that is the overwhelming common case and would be noise."""

    tier: ModelTier
    backend: str
    model_id: str
    from_state: HealthState
    to_state: HealthState
    reason: str


@dataclass(slots=True)
class _TargetState:
    state: HealthState = "UP"
    consecutive_failures: int = 0
    down_since: float | None = None


@dataclass(slots=True)
class BackendHealth:
    """Per-target three-state circuit breaker (§11.8). See module docstring for the full contract.

    `open_after_failures`/`cooldown_s` are `CallPolicy`'s fields of the same name, themselves
    mapped from `llm.failover.open_after_failures`/`.cooldown_s` (`orchestrator/context.py::
    call_policy_for`) — this class reads only the two numbers, never the config object.
    """

    open_after_failures: int
    cooldown_s: float
    clock: Callable[[], float] = time.monotonic
    on_transition: Callable[[BackendHealthTransition], None] | None = None
    _targets: dict[str, _TargetState] = field(default_factory=dict, init=False, repr=False)

    @staticmethod
    def _key(target: BackendTarget) -> str:
        # ADR-0149: the ENDPOINT is part of the breaker's identity. Keyed on `backend:model_id`
        # alone, a replica refusing connections opened the breaker for its healthy peer too (both
        # resolve to the same pair), and after `open_after_failures` calls the whole tier read DOWN
        # while one replica was serving fine — the exact outage replicas exist to absorb.
        if target.base_url is None:
            return f"{target.backend}:{target.model_id}"
        return f"{target.backend}:{target.model_id}@{target.base_url}"

    def _state_for(self, target: BackendTarget) -> _TargetState:
        return self._targets.setdefault(self._key(target), _TargetState())

    def may_call(self, target: BackendTarget, tier: ModelTier) -> bool:
        """`True`: call it — the target is `UP`, or this call IS the one `HALF_OPEN` probe it just
        earned. `False`: skip it — still `DOWN` and either the cooldown has not elapsed or another
        call already claimed this target's one probe.

        Claiming the probe is a state mutation with no `await` between the read above it and the
        write inside `_transition` — see the module docstring's Concurrency note.
        """
        state = self._state_for(target)
        if state.state == "UP":
            return True
        if state.state == "HALF_OPEN":
            return False
        # DOWN. `down_since` is always set in the same `_transition` call that sets this state,
        # so `is None` here cannot happen in practice — checked rather than asserted (no bare
        # `assert` in shipped code) and treated as "not yet eligible" if it somehow did.
        if state.down_since is None or self.clock() - state.down_since < self.cooldown_s:
            return False
        self._transition(target, tier, state, "HALF_OPEN", "cooldown elapsed; probing")
        return True

    def record_success(self, target: BackendTarget, tier: ModelTier) -> None:
        """Any successful reply, whatever state the target was in. Clears the failure count; a
        `HALF_OPEN` probe that succeeds is the one path back to `UP`."""
        state = self._state_for(target)
        state.consecutive_failures = 0
        if state.state != "UP":
            reason = "probe succeeded" if state.state == "HALF_OPEN" else "recovered"
            self._transition(target, tier, state, "UP", reason)
        state.down_since = None

    def record_failure(self, target: BackendTarget, tier: ModelTier) -> None:
        """A QUALIFYING failure only — the caller's job, not this method's, to have established
        that (see the module docstring's "Never throttling alone"). A `HALF_OPEN` probe that fails
        goes straight back to `DOWN` and restarts the cooldown clock rather than counting toward
        `open_after_failures` a second time; it already used up its one chance."""
        state = self._state_for(target)
        if state.state == "HALF_OPEN":
            state.consecutive_failures = max(state.consecutive_failures, self.open_after_failures)
            state.down_since = self.clock()
            self._transition(target, tier, state, "DOWN", "probe failed; cooldown restarts")
            return
        state.consecutive_failures += 1
        if state.consecutive_failures >= self.open_after_failures and state.state != "DOWN":
            # Only the UP -> DOWN transition may (re)start the cooldown clock. A target that is
            # already DOWN can keep receiving qualifying failures from calls that started before
            # the trip (concurrent in-flight calls, since `may_call` only blocks NEW calls) — those
            # must not push `down_since` forward, or the cooldown would never elapse under a
            # sustained trickle of late-arriving failures for an already-tripped target.
            state.down_since = self.clock()
            self._transition(
                target,
                tier,
                state,
                "DOWN",
                f"{state.consecutive_failures} consecutive qualifying failures "
                f"(open_after_failures={self.open_after_failures})",
            )

    def abandon_probe(self, target: BackendTarget, tier: ModelTier) -> None:
        """Any call outcome that is neither success (`record_success`) nor a qualifying
        `TransportError` failure (`record_failure`) — `SchemaUnsatisfied`, `OutputTruncated`,
        `ModelRefused`, `BudgetExhausted`, `UnknownBackend`, a cancellation, or anything else the
        call site did not otherwise handle. A no-op unless the target is `HALF_OPEN`: an ordinary
        `UP` target hitting one of these needs nothing from the breaker, which is what makes it
        safe for `complete()` to call this unconditionally on every non-success exit rather than
        having to track separately whether THIS call was the one probe.

        `down_since` is deliberately left UNCHANGED — this is the one behavioural difference from
        `record_failure`'s own `HALF_OPEN` branch. None of the outcomes this method exists for is
        connectivity evidence, so extending the cooldown for them would be exactly the "throttling
        alone" over-inference §11.8 forbids, pointed at a new class of non-signal. Leaving
        `down_since` alone means the clock condition `may_call` re-checks is already satisfied (it
        was, or this probe would not have been claimed), so the very next call to this target is
        immediately eligible to probe again — the target gets another chance right away rather
        than being punished for a reason that says nothing about whether it is reachable.
        """
        state = self._state_for(target)
        if state.state != "HALF_OPEN":
            return
        self._transition(
            target,
            tier,
            state,
            "DOWN",
            "probe abandoned (non-connectivity outcome); cooldown NOT restarted",
        )

    def _transition(
        self,
        target: BackendTarget,
        tier: ModelTier,
        state: _TargetState,
        to_state: HealthState,
        reason: str,
    ) -> None:
        from_state = state.state
        state.state = to_state
        if self.on_transition is None:
            return
        self.on_transition(
            BackendHealthTransition(
                tier=tier,
                backend=target.backend,
                model_id=target.model_id,
                from_state=from_state,
                to_state=to_state,
                reason=reason,
            ),
        )
