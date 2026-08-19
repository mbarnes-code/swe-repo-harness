"""`LlmFindingSink` — the persistence half of the LLM layer's diagnostics (§7.7, §11.8, §13).

`LadderModelClient` **computes** three things it deliberately cannot store: a `CapabilityDrift`
(a reply produced at a lower structured-output rung than the profile promised, §13 row 37), a
`BackendFailover` (§11.8's `backend_failover` event) and, when every target for a tier is spent,
a `TierUnavailable` (§13 row 40). The client owns no database handle and must not acquire one —
it is the module with no vendor import and no I/O beyond the backend call, and that is what makes
it testable offline. So it accepts two sinks (`on_drift`, `on_failover`) and calls them.

**Until this module existed nobody supplied those sinks.** `orchestrator/context.py` built the
client with neither, so `_emit_drift` and `_emit_failover` returned at their `is None` guards and
every drift and every failover the fleet ever computed was discarded — a silent local server that
dropped guided JSON showed up as nothing at all. This is the sink, wired once per run where the
client is assembled, which is the only place that knows both the client and the writer.

Three design points, each of which is load-bearing rather than taste:

* **The callbacks are synchronous and the database is not.** `Callable[[CapabilityDrift], None]`
  cannot await, and turning each callback into a fire-and-forget `create_task` puts a write of
  unknown lifetime behind a `StateWriter` that the run may close first — the classic "Task
  exception was never retrieved" at shutdown, on the exact path whose whole purpose is to record
  that something went wrong. So emission **buffers**, in memory, in O(1), and `flush()` drains
  the buffer through the run's one writer at a point the caller chooses. `PhaseRunner` flushes
  after every dispatch, so nothing sits in the buffer across a wave.
* **Drift and failover carry no `repo_id`, and that is correct, not a shortcut.** One client
  serves every repo in a wave concurrently, so the callback has no way to know which repo's task
  it is running inside; attributing it to "the current repo" would be a guess that is wrong
  whenever two repos are in flight. Both are properties of a *target* (backend + model_id), not
  of a repository, and `CapabilityDrift` (client.py:215) has no repo field for the same reason.
  They are therefore fleet-level findings — `repo_id IS NULL`, which `ux_findings_ident` already
  supports via its `IFNULL(repo_id, '')` expression.
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

    from fleet.llm.client import BackendFailover, CapabilityDrift
    from fleet.models.enums import ModelTier, Phase
    from fleet.state.db import StateWriter
    from fleet.state.repository import StateRepository

__all__ = [
    "BACKEND_FAILOVER_EVENT",
    "BACKEND_UNAVAILABLE",
    "CAPABILITY_DRIFT",
    "LlmFindingSink",
]

#: `findings.kind` for §13 row 37 — the promised rung was not the rung that answered.
CAPABILITY_DRIFT: Final = "CapabilityDrift"

#: `findings.kind` for §13 row 40 — every target for a tier was spent (exit 8).
BACKEND_UNAVAILABLE: Final = "BackendUnavailable"

#: `events.event` for §11.8. A failover is an EVENT, not a finding: it is a hop the fleet took
#: and recovered from, and `BackendFailover`'s own docstring (client.py:228) names it as one.
BACKEND_FAILOVER_EVENT: Final = "backend_failover"

#: Carried in every `BackendUnavailable` payload. Prose in a row is normally a smell; here it is
#: the point — the row's own name overstates what the harness measured, and the operator reading
#: it months later is the person who would otherwise act on the overstatement (§13 row 43).
_CAVEAT: Final = (
    "Tier exhaustion only. This is NOT evidence that any backend is down: llm/client.py fails a "
    "target over without inspecting TransportError.trigger, so sustained RATE_LIMIT throttling "
    "reaches this finding identically to a CONNECTION or SERVER_ERROR failure (SPEC 13 row 43 -- "
    "a 429 alone can never mean DOWN). Read failover_triggers together with "
    "failover_triggers_scope: when the scope is 'run' the map spans EVERY tier this run touched, "
    "not only the exhausted one named in `observed`, so match the tier key yourself before "
    "concluding anything. Within a tier the map still omits the target that exhausted it, which "
    "never reports its own trigger. If the triggers for the exhausted tier are throttling, the "
    "correct action is to run at lower concurrency, not to repair infrastructure."
)


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

    Constructed by `RunContext.__post_init__` and handed to `LadderModelClient` as `on_drift=`
    and `on_failover=`. Nothing here reaches for a module global (CLAUDE.md Guardrail 3): the
    writer, the repository and the clock all arrive by construction, which is what lets a test
    drive it against a temp database with no network.
    """

    run_id: str
    writer: StateWriter
    repository: StateRepository
    clock: Callable[[], datetime] = utcnow
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

    @property
    def pending(self) -> int:
        """How much is buffered. Diagnostics and tests; never a control-flow input."""
        return len(self._drifts) + len(self._failovers)

    def observed_triggers(self, tier: ModelTier | None = None) -> dict[str, dict[str, str]]:
        """`{tier: {"<backend>:<model_id>": "<FailoverTrigger>"}}`, optionally narrowed to one tier.

        A deep COPY, so a caller cannot mutate the sink's state. Keyed by target within a tier for
        the same reason `CapabilityDrift` carries no `repo_id`: a trigger is a fact about an
        endpoint, not about whichever repo happened to be holding it. Necessarily PARTIAL —
        `client.py:539-541` guards `_emit_failover` with `index + 1 < len(targets)`, so the target
        that exhausts a tier never reports its trigger, and a single-target tier reports none.

        The shape is the SAME whether or not `tier` narrows it: one field name, one shape, so a
        consumer never has to branch on which caller wrote the row.
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
        written = 0
        unsent = 0
        try:
            if drifts:
                written += await self._write_drifts(drifts)
                drifts = []
            while unsent < len(failovers):
                event, event_uid = failovers[unsent]
                await self._write_failover(event, event_uid)
                unsent += 1
                written += 1
        except BaseException:
            self._drifts[:0] = drifts
            self._failovers[:0] = failovers[unsent:]
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

        * `tier` given → the map is narrowed to it, `failover_triggers_scope` is `"tier"`, and
          `throttling_observed` is a real boolean about *that* tier.
        * `tier` omitted → the map is the whole run, **keyed by tier so every entry is still
          self-describing**, `failover_triggers_scope` is `"run"`, and `throttling_observed` is
          `null`. Not `false`: we hold triggers, we simply cannot say whether any belongs to the
          tier that died, and a `false` there would be the mirror-image lie.

        The runner's call site omits it, because `TierUnavailable.tier` is lost at the
        exception→`WorkerError` boundary and `observed` — the only carrier left — must not be
        parsed. The operator cross-references it by eye: `observed` names the tier and the map's
        keys are tier names. Carrying it structurally means changing `WorkerError`; another lane.

        `throttling_observed` is the derived answer to the only question that changes the
        operator's next action. It is deliberately NOT the negation of `asserts_outage` — both
        may be false, which means "we do not know".
        """
        stamp = _iso(self.clock())
        triggers = self.observed_triggers(tier)
        throttled: bool | None = None
        if tier is not None:
            throttled = any(
                t == "RATE_LIMIT" for targets in triggers.values() for t in targets.values()
            )
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
                    "failover_triggers_recorded": "partial" if triggers else "none",
                    "failover_triggers_scope": "run" if tier is None else "tier",
                    "throttling_observed": throttled,
                    "caveat": _CAVEAT,
                }
            ),
            stamp,
        )

        async def unit(conn: aiosqlite.Connection) -> None:
            await conn.execute(_INSERT_FINDING, params)

        await self.writer.submit(unit)
