"""`LlmFindingSink` — the persistence half of the LLM layer's diagnostics (§7.7, §11.8, §13).

`LadderModelClient` **computes** things it deliberately cannot store: a `CapabilityDrift`
(a reply produced at a lower structured-output rung than the profile promised, §13 row 37), a
`BackendFailover` (§11.8's `backend_failover` event), an `LlmCall` (§12.18's `llm_call` event,
one per actual provider call), a `BackendHealthTransition` (§11.8's circuit-breaker state changes,
ADR-0132) and, when every target for a tier is spent, a `TierUnavailable` (§13 row 40). The client
owns no database handle and must not acquire one — it is the module with no vendor import and no
I/O beyond the backend call, and that is what makes it testable offline. So it accepts four sinks
(`on_drift`, `on_failover`, `on_llm_call`, `on_health_transition`) and calls them.

**Until this module existed nobody supplied `on_drift`/`on_failover`.** `orchestrator/context.py`
built the client with neither, so `_emit_drift` and `_emit_failover` returned at their `is None`
guards and every drift and every failover the fleet ever computed was discarded — a silent local
server that dropped guided JSON showed up as nothing at all. This is the sink, wired once per run
where the client is assembled, which is the only place that knows both the client and the writer.
`on_llm_call` is the same shape, added later (§12.18) once the sink already existed, and
`on_health_transition` is the same shape again, added with `llm/failover.py::BackendHealth` itself
(ADR-0132) rather than left to repeat D59's whole thesis a fourth time.

Three design points, each of which is load-bearing rather than taste:

* **The callbacks are synchronous and the database is not.** `Callable[[CapabilityDrift], None]`
  cannot await, and turning each callback into a fire-and-forget `create_task` puts a write of
  unknown lifetime behind a `StateWriter` that the run may close first — the classic "Task
  exception was never retrieved" at shutdown, on the exact path whose whole purpose is to record
  that something went wrong. So emission **buffers**, in memory, in O(1), and `flush()` drains
  the buffer through the run's one writer at a point the caller chooses. `PhaseRunner` flushes
  after every dispatch, so nothing sits in the buffer across a wave.
* **Drift, failover and `llm_call` carry no `repo_id`, and that is correct, not a shortcut.** One
  client serves every repo in a wave concurrently, so the callback has no way to know which repo's
  task it is running inside; attributing it to "the current repo" would be a guess that is wrong
  whenever two repos are in flight. All three are properties of a *target* (backend + model_id),
  not of a repository, and `CapabilityDrift` (client.py:215) has no repo field for the same
  reason. They are therefore fleet-level — `repo_id IS NULL`, which `ux_findings_ident` already
  supports via its `IFNULL(repo_id, '')` expression for the two finding kinds, and which
  `emit(repo_id=None)` supports directly for the `llm_call` event.
* **`BackendUnavailable` is the one that DOES know its repo**, because it is written from
  `PhaseRunner` at the §11.8 halt, where the repo whose dispatch met the outage is in hand.

Idempotency follows §11.7 everywhere: the fingerprint is the semantic identity of the finding, so
a hundred drifts against the same target on the same rung converge on one row with a refreshed
`created_at` rather than a hundred rows.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from fleet.models.base import utcnow
from fleet.obs.redact import redact_text
from fleet.state.repository import EventRow
from fleet.util.hashing import sha256_text

if TYPE_CHECKING:
    import aiosqlite

    from fleet.llm.client import BackendFailover, CapabilityDrift, LlmCall
    from fleet.llm.failover import BackendHealthTransition
    from fleet.models.enums import ModelTier, Phase
    from fleet.obs.events import EventEmitter
    from fleet.state.db import StateWriter
    from fleet.state.repository import StateRepository

__all__ = [
    "BACKEND_FAILOVER_EVENT",
    "BACKEND_HEALTH_TRANSITION_EVENT",
    "BACKEND_UNAVAILABLE",
    "CAPABILITY_DRIFT",
    "LLM_CALL_EVENT",
    "LlmFindingSink",
]

#: `findings.kind` for §13 row 37 — the promised rung was not the rung that answered.
CAPABILITY_DRIFT: Final = "CapabilityDrift"

#: `findings.kind` for §13 row 40 — every target for a tier was spent (exit 8).
BACKEND_UNAVAILABLE: Final = "BackendUnavailable"

#: `events.event` for §11.8. A failover is an EVENT, not a finding: it is a hop the fleet took
#: and recovered from, and `BackendFailover`'s own docstring (client.py:228) names it as one.
BACKEND_FAILOVER_EVENT: Final = "backend_failover"

#: `events.event` for §12.18. One per completed `backend.invoke()` — see `LlmCall`'s docstring
#: (client.py) for why it is not one per `complete()` or per `_call_target()`.
LLM_CALL_EVENT: Final = "llm_call"

#: `events.event` for §11.8's `BackendHealth` breaker (ADR-0132). One per STATE CHANGE, never per
#: call that stayed UP — see `BackendHealthTransition`'s own docstring (`llm/failover.py`).
BACKEND_HEALTH_TRANSITION_EVENT: Final = "backend_health_transition"

#: Carried in every `BackendUnavailable` payload. Prose in a row is normally a smell; here it is
#: the point — the row's own name overstates what the harness measured, and the operator reading
#: it months later is the person who would otherwise act on the overstatement (§13 row 43).
_CAVEAT_HEAD: Final = (
    "Tier exhaustion only. This is NOT evidence that any backend is down: llm/client.py fails a "
    "target over without inspecting TransportError.trigger, so sustained RATE_LIMIT throttling "
    "reaches this finding identically to a CONNECTION or SERVER_ERROR failure (SPEC 13 row 43 -- "
    "a 429 alone can never mean DOWN). Read failover_triggers together with "
    "failover_triggers_scope. "
)

#: Appended when the row could NOT be told which tier died — every row this harness writes today.
_CAVEAT_RUN: Final = (
    "This row has scope 'run': the map spans EVERY tier the run touched, not only the exhausted "
    "one named in `observed`, and failover_triggers_recorded and throttling_observed are "
    "unanswered on purpose. Match the tier key against `observed` yourself before concluding "
    "anything. "
)

#: Appended when a caller supplied the tier. Unreachable in production until `WorkerError` carries
#: a tier -- but it MUST already be correct, because the row that gets it is the row whose `scope`
#: field would otherwise be denied by its own caveat.
_CAVEAT_TIER: Final = (
    "This row has scope 'tier': the map, failover_triggers_recorded and throttling_observed all "
    "describe ONLY the exhausted tier named in `observed`. "
)

_CAVEAT_TAIL: Final = (
    "Within a tier the map still omits the target that exhausted it, which never reports its own "
    "trigger. If the triggers for the EXHAUSTED tier are throttling, the correct action is to run "
    "at lower concurrency, not to repair infrastructure."
)


def _caveat(tier_known: bool) -> str:
    """The operator-facing text, branched on scope.

    A single unconditional caveat asserted "every row has scope 'run'" while `scope` was a live
    field — so a tier-scoped row would have shipped carrying prose that denied its own data. That
    is latent today (nothing passes `tier=`) and goes live the moment a caller can, which is
    exactly the future the `record_backend_unavailable` disclosure is written for. A caveat that
    contradicts the row it annotates is worse than no caveat: it is the overclaim this whole field
    block exists to prevent, wearing the costume of the fix.
    """
    return _CAVEAT_HEAD + (_CAVEAT_TIER if tier_known else _CAVEAT_RUN) + _CAVEAT_TAIL


_INSERT_FINDING: Final = (
    "INSERT INTO findings (run_id, repo_id, kind, severity, fingerprint, payload, created_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT (run_id, IFNULL(repo_id, ''), kind, fingerprint) "
    "DO UPDATE SET payload = excluded.payload, created_at = excluded.created_at"
)


def _iso(moment: datetime) -> str:
    """Fixed-width UTC. Every persisted instant in this codebase is compared as TEXT (§11.5)."""
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def _json(payload: dict[str, object]) -> str:
    """Dump, then redact — at the boundary, so no call site can forget it (§11.4)."""
    return redact_text(json.dumps(payload, sort_keys=True))


@dataclass(slots=True)
class LlmFindingSink:
    """Buffers what the LLM client computes; `flush()` writes it through the run's one writer.

    Constructed by `RunContext.__post_init__` and handed to `LadderModelClient` as `on_drift=`,
    `on_failover=` and `on_llm_call=`. Nothing here reaches for a module global (CLAUDE.md
    Guardrail 3): the writer, the repository, the emitter and the clock all arrive by
    construction, which is what lets a test drive it against a temp database with no network.
    """

    run_id: str
    writer: StateWriter
    repository: StateRepository
    clock: Callable[[], datetime] = utcnow
    emitter: EventEmitter | None = None
    """§12.18's `llm_call` events go through THIS, not through `repository.append_event`
    directly, the way `_write_failover` does — `llm_call` is what CLAUDE.md's redaction
    discipline is about here (this sink's payloads are LLM-adjacent, and `obs/events.py` is the
    one boundary that redacts before either sink), and going through `emit()` is also what puts
    it on `logs/events-<run_id>.jsonl` and, for `level='error'`, on the §12.18 errors sink.

    `None` is a valid no-op, the same shape as `RunContext.root` and as `EventEmitter`'s own
    `sink`/`jsonl_path`: a caller that only cares about drift/failover (every existing test in
    this file, before this field existed) does not have to construct one. `RunContext` always
    builds a real one; `_write_llm_call` drops the record on the floor when this is `None`,
    exactly as `EventEmitter.emit()` drops a line when `jsonl_path` is `None` — a caller's
    deliberate scope, not a swallowed failure."""
    _drifts: list[CapabilityDrift] = field(default_factory=list, init=False, repr=False)
    _failovers: list[tuple[BackendFailover, str]] = field(
        default_factory=list, init=False, repr=False
    )
    """`(event, event_uid)`. The uid is minted ONCE, when the record is buffered — never per
    write attempt. `StateWriter.submit` queues the unit and then awaits its future, so a
    `CancelledError` delivered at that await leaves the unit queued and it still commits; a
    re-buffered record re-written under a FRESH uid would slip past
    `ON CONFLICT (run_id, event_uid) DO NOTHING` and double-count. A stable uid makes the retry
    idempotent, which is the same property `_INSERT_FINDING`'s fingerprint gives the drift side."""

    _llm_calls: list[tuple[LlmCall, str]] = field(default_factory=list, init=False, repr=False)
    """`(event, event_uid)`, same idempotency reasoning as `_failovers` — `emitter.emit()` also
    conflicts on `(run_id, event_uid)`, so a re-buffered record after a cancelled flush must reuse
    its uid or double-count on retry."""

    _health_transitions: list[tuple[BackendHealthTransition, str]] = field(
        default_factory=list, init=False, repr=False
    )
    """`(event, event_uid)`, same idempotency reasoning as `_failovers`/`_llm_calls`. §11.8's
    breaker (ADR-0132) — buffered here alongside the other three sinks `LadderModelClient`
    already computes and nobody used to persist (D59's whole thesis, one sink over)."""

    _triggers: dict[str, dict[str, str]] = field(default_factory=dict, init=False, repr=False)
    """`{tier: {"<backend>:<model_id>": trigger}}`. **Keyed by TIER first, and that is the whole
    point.** `SPEC_ROLE_TIERS` (`llm/roles.py:65-77`) routes the twelve roles across HEAVY,
    WORKHORSE and CHEAP, so one run drives all three tiers through one client and one sink. A
    flat target-keyed map let a CHEAP-tier 429 set `throttling_observed: true` on a HEAVY-tier
    outage row — telling the operator to lower concurrency while a HEAVY endpoint stayed dead."""

    # -- the two callbacks `LadderModelClient` calls ------------------------------------------

    def on_drift(self, drift: CapabilityDrift) -> None:
        """`LadderModelClient(on_drift=...)`. Synchronous by contract; buffers, never writes.

        Cheap on purpose: this runs inside `complete()`, once per target, on the hot path.
        """
        self._drifts.append(drift)

    def on_failover(self, event: BackendFailover) -> None:
        """`LadderModelClient(on_failover=...)`. Same contract, same reason.

        Also records WHY the source target was retired, **under that target's tier**. That map is
        what stops `BackendUnavailable` from claiming it knows nothing about the triggers — see
        `record_backend_unavailable`. In-memory and per-run, the same lifetime SPEC §11.8 gives
        `BackendHealth`: a resumed run must re-observe rather than inherit a stale verdict.

        Last write wins per (tier, target). A target retired by `RATE_LIMIT` in wave 3 and by
        `CONNECTION` in wave 7 reports only the later one. Known and deliberate at this size — the
        map is a *current* observation, not a history, and a history belongs in the
        `backend_failover` events, which keep every hop.
        """
        self._failovers.append((event, str(uuid.uuid4())))
        self._triggers.setdefault(str(event.tier), {})[
            f"{event.from_backend}:{event.from_model_id}"
        ] = event.trigger

    def on_llm_call(self, call: LlmCall) -> None:
        """`LadderModelClient(on_llm_call=...)`. Same synchronous-buffer contract as `on_drift`/
        `on_failover` — this runs inside `_call_target`, once per actual provider call, on the
        hot path, and the client itself owns no database handle to write through.
        """
        self._llm_calls.append((call, str(uuid.uuid4())))

    def on_health_transition(self, event: BackendHealthTransition) -> None:
        """`LadderModelClient(on_health_transition=...)`. Same synchronous-buffer contract as the
        three above — fires from `llm/failover.py::BackendHealth._transition`, once per STATE
        CHANGE (never per call that stayed `UP`), on the hot path."""
        self._health_transitions.append((event, str(uuid.uuid4())))

    @property
    def pending(self) -> int:
        """How much is buffered. Diagnostics and tests; never a control-flow input."""
        return (
            len(self._drifts)
            + len(self._failovers)
            + len(self._llm_calls)
            + len(self._health_transitions)
        )

    def observed_triggers(self, tier: ModelTier | None = None) -> dict[str, dict[str, str]]:
        """`{tier: {"<backend>:<model_id>": "<FailoverTrigger>"}}`, optionally narrowed to one tier.

        A deep COPY, so a caller cannot mutate the sink's state. Keyed by target within a tier for
        the same reason `CapabilityDrift` carries no `repo_id`: a trigger is a fact about an
        endpoint, not about whichever repo happened to be holding it. Necessarily PARTIAL —
        `client.py:539-541` guards `_emit_failover` with `index + 1 < len(targets)`, so the target
        that exhausts a tier never reports its trigger, and a single-target tier reports none.

        The MAP's shape is the same whether or not `tier` narrows it — always
        `{tier: {target: trigger}}`, so no consumer has to branch to *read* it. That is the only
        thing the shape guarantees: the row's derived fields around it deliberately DO differ, and
        `failover_triggers_scope` exists precisely so a consumer can branch on which arm wrote it
        (`throttling_observed` is `bool` under `"tier"` and `null` under `"run"`). See
        `record_backend_unavailable`.
        """
        wanted = None if tier is None else str(tier)
        return {
            name: dict(targets)
            for name, targets in self._triggers.items()
            if wanted is None or name == wanted
        }

    # -- persistence -------------------------------------------------------------------------

    async def flush(self) -> int:
        """Drain the buffer to SQLite and return how many records were written.

        The swap is atomic on an event loop (no `await` between the read and the rebind), so a
        concurrent flush from a sibling repo's task cannot write the same record twice.

        **Anything that did not land is put BACK.** Detaching the buffer and then losing it to a
        busy timeout or a closed writer would be "computed, then discarded" — the exact defect
        this module exists to close — reinstated one layer up, and reinstated on the error path,
        which is the path most likely to be carrying interesting drift. Re-buffering is at the
        FRONT so emission order survives, and the guard is `BaseException` so a cancelled wave
        keeps its records too.
        """
        drifts, self._drifts = self._drifts, []
        failovers, self._failovers = self._failovers, []
        llm_calls, self._llm_calls = self._llm_calls, []
        health_transitions, self._health_transitions = self._health_transitions, []
        written = 0
        unsent_failovers = 0
        unsent_calls = 0
        unsent_transitions = 0
        try:
            if drifts:
                written += await self._write_drifts(drifts)
                drifts = []
            while unsent_failovers < len(failovers):
                event, event_uid = failovers[unsent_failovers]
                await self._write_failover(event, event_uid)
                unsent_failovers += 1
                written += 1
            while unsent_calls < len(llm_calls):
                call, event_uid = llm_calls[unsent_calls]
                await self._write_llm_call(call, event_uid)
                unsent_calls += 1
                written += 1
            while unsent_transitions < len(health_transitions):
                transition, event_uid = health_transitions[unsent_transitions]
                await self._write_health_transition(transition, event_uid)
                unsent_transitions += 1
                written += 1
        except BaseException:
            self._drifts[:0] = drifts
            self._failovers[:0] = failovers[unsent_failovers:]
            self._llm_calls[:0] = llm_calls[unsent_calls:]
            self._health_transitions[:0] = health_transitions[unsent_transitions:]
            raise
        return written

    async def _write_drifts(self, drifts: Sequence[CapabilityDrift]) -> int:
        stamp = _iso(self.clock())
        params = [
            (
                self.run_id,
                None,  # fleet-level: a drift is a property of a TARGET, not of a repository
                CAPABILITY_DRIFT,
                "warn",
                sha256_text(
                    "\x00".join(
                        (
                            drift.role,
                            str(drift.tier),
                            drift.backend,
                            drift.model_id,
                            str(drift.promised),
                            str(drift.actual),
                        )
                    )
                ),
                _json(
                    {
                        "role": drift.role,
                        "tier": str(drift.tier),
                        "backend": drift.backend,
                        "model_id": drift.model_id,
                        "promised": str(drift.promised),
                        "actual": str(drift.actual),
                    }
                ),
                stamp,
            )
            for drift in drifts
        ]

        async def unit(conn: aiosqlite.Connection) -> None:
            await conn.executemany(_INSERT_FINDING, params)

        await self.writer.submit(unit)
        return len(params)

    async def _write_failover(self, event: BackendFailover, event_uid: str) -> None:
        """One `events` row. `seq` is allocated in-statement by the repository, never here (§6).

        `event_uid` is passed IN rather than minted here: see `_failovers`. A retry must reuse the
        uid the record was buffered with, or `ON CONFLICT (run_id, event_uid) DO NOTHING` cannot
        recognise a row that already landed.
        """
        await self.repository.append_event(
            EventRow(
                run_id=self.run_id,
                seq=0,  # ignored on the way in; the insert allocates it
                ts=_iso(self.clock()),
                repo_id=None,
                phase=None,
                level="warn",
                event=BACKEND_FAILOVER_EVENT,
                event_uid=event_uid,
                payload=_json(
                    {
                        "role": event.role,
                        "tier": str(event.tier),
                        "from_backend": event.from_backend,
                        "from_model_id": event.from_model_id,
                        "to_backend": event.to_backend,
                        "to_model_id": event.to_model_id,
                        "trigger": event.trigger,
                    }
                ),
            )
        )

    async def _write_llm_call(self, call: LlmCall, event_uid: str) -> None:
        """One §12.18 `llm_call` event, through `self.emitter` rather than
        `repository.append_event` directly — unlike `_write_failover`, this is the ONE call site
        in this module CLAUDE.md's redaction discipline actually requires it for: `obs/events.py`
        is where §11.4's redaction happens (`_build_row` redacts, THEN serialises), and going
        through `emit()` is what puts the event on `logs/events-<run_id>.jsonl` — and, for a
        `level='error'` call, on the §12.18 errors sink — as well as in `events`.

        `event_uid` is passed IN, same reasoning as `_write_failover`: a retry after a cancelled
        flush must reuse the uid the record was buffered with, or `emit()`'s own `ON CONFLICT
        (run_id, event_uid) DO NOTHING` cannot recognise a row that already landed.

        `emit()` never raises (§11.4's carve-out: the caller is a worker mid-call and the failing
        operation is telemetry), so this method cannot raise either — a dropped `llm_call` is
        counted and retained by the emitter itself, exactly like a dropped `backend_failover` row
        currently is not (that gap is pre-existing and out of this method's scope).

        A `None` emitter (see the field docstring) is a no-op: the record is dropped without a
        write, which is a caller's deliberate scope and not itself a failure to count.
        """
        if self.emitter is None:
            return
        await self.emitter.emit(
            LLM_CALL_EVENT,
            level=call.level,
            repo_id=None,  # fleet-level: same reasoning as CapabilityDrift/BackendFailover
            phase=None,
            payload={
                "role": call.role,
                "tier": str(call.tier),
                "backend": call.backend,
                "model_id": call.model_id,
                "structured_output_mode": str(call.structured_output_mode),
                "input_tokens": call.input_tokens,
                "output_tokens": call.output_tokens,
                "cost_usd": call.cost_usd,
                "latency_ms": call.latency_ms,
                # ADR-0149/§12.51(i): which endpoint answered — the per-endpoint split of a run
                # is `jq`-able from here, and nowhere else carries it.
                "base_url": call.base_url,
            },
            event_uid=event_uid,
            now=self.clock(),
        )

    async def _write_health_transition(
        self, transition: BackendHealthTransition, event_uid: str
    ) -> None:
        """One `backend_health_transition` event (§11.8, ADR-0132). Through `self.emitter`, same
        reasoning as `_write_llm_call`: this sink's payloads are LLM-adjacent, so redaction and
        `logs/events-<run_id>.jsonl` both go through the one boundary that provides them — and,
        unlike `_write_failover`'s direct `repository.append_event`, going through `emitter.emit`
        means `level` is validated against `obs/events.py`'s `_LEVELS` allowlist before it lands
        (`_build_row` silently coerces anything else to `"info"`), which is why this uses the
        real member name rather than the "warn" abbreviation `_write_failover` gets away with.

        `level` is `"warning"` for `DOWN` (an operator-actionable state — the target is being
        skipped) and `"info"` for `HALF_OPEN`/`UP` (routine recovery, not itself actionable).
        """
        if self.emitter is None:
            return
        await self.emitter.emit(
            BACKEND_HEALTH_TRANSITION_EVENT,
            level="warning" if transition.to_state == "DOWN" else "info",
            repo_id=None,  # fleet-level: a target's health is not a property of one repo
            phase=None,
            payload={
                "tier": str(transition.tier),
                "backend": transition.backend,
                "model_id": transition.model_id,
                "from_state": transition.from_state,
                "to_state": transition.to_state,
                "reason": transition.reason,
            },
            event_uid=event_uid,
            now=self.clock(),
        )

    async def record_backend_unavailable(
        self, *, repo_id: str, phase: Phase, observed: str, tier: ModelTier | None = None
    ) -> None:
        """§13 row 40: a tier was exhausted. Written BEFORE the exit-8 halt.

        **This finding reports an OBSERVATION and refuses to assert a cause.** That refusal is
        the whole design, and it is worth stating why, because the obvious wording is wrong:

        The name of the halt (`BACKEND_UNAVAILABLE`, `HaltReason.TIER_UNAVAILABLE`) and the prose
        already attached to it (`retry.py:196` and `runner.py`: "every target for the tier is
        DOWN") both assert a `BackendHealth.DOWN` state. `DOWN` **has no representation anywhere
        in `src/`** — nothing computes it and nothing stores it — and §13 row 43 forbids the claim
        outright: *`DOWN` requires a connection-level failure or a 5xx, never throttling alone.*

        Yet a pure 429 reaches this exact call site in three hops today. `client.py:532` catches
        `TransportError` **without inspecting `exc.trigger`**, so a `RATE_LIMIT` retires a target
        exactly like a refused connection; three of them exhaust `max_targets_per_call`;
        `TierUnavailable` is raised; and `classify.py:243-258` makes it non-retryable. Row 43's
        named disaster — a long run exiting 8 when the correct action was to lower concurrency —
        is therefore reachable at HEAD, wearing row 40's vocabulary.

        A log line that says the wrong thing scrolls away. A FINDING is what a human reads
        afterwards, so a finding that said "the backend is down" would convert a transient,
        correctable throttle into a durable false record. This one says only what was measured.

        `observed` is the worker's own `stderr_tail`, which on this path is `TierUnavailable`'s
        message verbatim (client.py:151-155) and therefore names the tier and, in order, every
        target the ladder spent. Carried through unparsed: nothing in this codebase branches on
        message text, and re-deriving the target list from the router would answer a *different*
        question (what the route says now) than the one the finding asks (what was tried then).

        **What it can say about the triggers, and what it cannot.** `TierUnavailable` carries
        `tier` and `targets_tried` (client.py:149-155) and no triggers, and `_emit_failover` is
        guarded by `index + 1 < len(targets)` (client.py:539-541) — so the target that EXHAUSTS
        the tier never reports what retired it, and a single-target tier reports nothing. The
        complete set is genuinely unreconstructable.

        The set this run actually holds, however, is **not empty**, and saying so would have been
        a second false statement in the opposite direction. For an N-target tier the first N-1
        triggers are emitted, `on_failover` is now wired, and this same sink both persists them as
        `backend_failover` events and keeps them. An operator who read "triggers: none recorded"
        and concluded the throttle-vs-outage question was unanswerable would be repairing
        infrastructure while N-1 `RATE_LIMIT` rows sat in `events` for the same run. So
        `failover_triggers` carries what is known and `failover_triggers_recorded` is a
        three-valued STRING — `"none"` or `"partial"`, never `"complete"`, because completeness
        is structurally unreachable until `TierUnavailable` itself carries the triggers.

        **`tier` is what keeps that map at the right GRAIN, and omitting it costs a field.** One
        run drives HEAVY, WORKHORSE and CHEAP through one client and one sink
        (`SPEC_ROLE_TIERS`, `llm/roles.py:65-77`). An unfiltered map let a CHEAP-tier 429 two
        hours earlier set `throttling_observed: true` on a HEAVY-tier outage row, sending the
        operator to lower concurrency while a dead HEAVY endpoint went unrepaired — the same
        false-statement class this row exists to avoid, pointed the other way.

        * `tier` given → the map is narrowed to it, `failover_triggers_scope` is `"tier"`,
          `failover_triggers_recorded` is `"none"`/`"partial"` **about that tier**, and
          `throttling_observed` is a real boolean about that tier.
        * `tier` omitted → the map is the whole run, **keyed by tier so every entry is still
          self-describing**, `failover_triggers_scope` is `"run"`, and BOTH derived fields refuse
          to answer: `failover_triggers_recorded` is `"unknown"` and `throttling_observed` is
          `null`. Neither is `false`/`"none"` — we hold triggers, we simply cannot say whether any
          of them belongs to the tier that died, and answering would be the mirror-image of the
          cross-tier contamination this parameter exists to prevent.

        **D78 (FIXED): the narrowed arm is now live in production.** `WorkerError`
        (`workers/base.py`) carries a `tier` field, `classify.py::_error_for` populates it from
        `TierUnavailable.tier` on the `BACKEND_UNAVAILABLE` branch, and `PhaseRunner._drive`
        forwards `failure.tier` into this call's `tier=` kwarg — so the production caller
        supplies it whenever the halt originated from a real `TierUnavailable`. **The run-scoped
        arm (`tier` omitted) is, as of this fix, UNREACHED IN PRODUCTION** — corrected here after
        an earlier version of this note claimed both arms were live, which does not hold up:
        `classify.py::_error_for` is the sole site in `src/` that assigns
        `FailureClass.BACKEND_UNAVAILABLE` to a `WorkerError`, and it reaches that branch only via
        `isinstance(exc, TierUnavailable)`, whose `tier` constructor parameter is non-optional —
        so every production `BACKEND_UNAVAILABLE` `WorkerError` now carries a `tier`. The
        synthetic "worker returned no result" `WorkerError` (`runner.py:621-624`) cannot take this
        arm either: it is built with `FailureClass.UNKNOWN`, and the gate on the only
        `record_backend_unavailable` call site (`runner.py:675` guarding `runner.py:705`) checks
        `failure.failure_class is FailureClass.BACKEND_UNAVAILABLE`, so that synthetic error
        never reaches it. The
        run-scoped arm stays real only as a guard against a *future* `BACKEND_UNAVAILABLE`
        `WorkerError` constructed without going through `_error_for` — which is why the test
        covering it is still worth keeping, even though nothing in `src/` exercises it today.

        The operator cross-references by eye in the meantime: `observed` names the exhausted tier
        and the map's keys are tier names.

        `throttling_observed` is the derived answer to the only question that changes the
        operator's next action. It is deliberately NOT the negation of `asserts_outage` — both
        may be false, which means "we do not know".
        """
        stamp = _iso(self.clock())
        triggers = self.observed_triggers(tier)
        # Both derived fields answer ONLY when the row knows which tier it is about. At run scope
        # `"none"` would be as contaminated as `false` — it would claim we hold no trigger for the
        # exhausted tier when what we actually hold is a map we cannot attribute.
        throttled: bool | None = None
        recorded = "unknown"
        if tier is not None:
            throttled = any(
                t == "RATE_LIMIT" for targets in triggers.values() for t in targets.values()
            )
            recorded = "partial" if triggers else "none"
        params = (
            self.run_id,
            repo_id,
            BACKEND_UNAVAILABLE,
            "error",  # this one stops the run; 'warn' would understate it in every report
            sha256_text("\x00".join((repo_id, phase.name, observed))),
            _json(
                {
                    "repo_id": repo_id,
                    "phase": phase.name,
                    "observed": observed,
                    # The honesty block. Nothing in `src/` reads these today — `projection.py:135`
                    # and `digest.py:110` both filter to `CycleDetected` — so their live function
                    # is the regression tripwire in `tests/test_llm_findings.py`, which fails if a
                    # future edit starts asserting a cause. The human-facing channel is `caveat`.
                    "asserts_outage": False,
                    "failover_triggers": triggers,
                    "failover_triggers_recorded": recorded,
                    "failover_triggers_scope": "run" if tier is None else "tier",
                    "throttling_observed": throttled,
                    "caveat": _caveat(tier is not None),
                }
            ),
            stamp,
        )

        async def unit(conn: aiosqlite.Connection) -> None:
            await conn.execute(_INSERT_FINDING, params)

        await self.writer.submit(unit)
