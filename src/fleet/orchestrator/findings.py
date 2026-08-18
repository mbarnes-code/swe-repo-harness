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
    from fleet.models.enums import Phase
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
    _failovers: list[BackendFailover] = field(default_factory=list, init=False, repr=False)

    # -- the two callbacks `LadderModelClient` calls ------------------------------------------

    def on_drift(self, drift: CapabilityDrift) -> None:
        """`LadderModelClient(on_drift=...)`. Synchronous by contract; buffers, never writes.

        Cheap on purpose: this runs inside `complete()`, once per target, on the hot path.
        """
        self._drifts.append(drift)

    def on_failover(self, event: BackendFailover) -> None:
        """`LadderModelClient(on_failover=...)`. Same contract, same reason."""
        self._failovers.append(event)

    @property
    def pending(self) -> int:
        """How much is buffered. Diagnostics and tests; never a control-flow input."""
        return len(self._drifts) + len(self._failovers)

    # -- persistence -------------------------------------------------------------------------

    async def flush(self) -> int:
        """Drain the buffer to SQLite and return how many records were written.

        The swap is atomic on an event loop (no `await` between the read and the rebind), so a
        concurrent flush from a sibling repo's task cannot write the same record twice.
        """
        drifts, self._drifts = self._drifts, []
        failovers, self._failovers = self._failovers, []
        written = 0
        if drifts:
            written += await self._write_drifts(drifts)
        for event in failovers:
            await self._write_failover(event)
            written += 1
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

    async def _write_failover(self, event: BackendFailover) -> None:
        """One `events` row. `seq` is allocated in-statement by the repository, never here (§6)."""
        await self.repository.append_event(
            EventRow(
                run_id=self.run_id,
                seq=0,  # ignored on the way in; the insert allocates it
                ts=_iso(self.clock()),
                repo_id=None,
                phase=None,
                level="warn",
                event=BACKEND_FAILOVER_EVENT,
                event_uid=str(uuid.uuid4()),
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
        self,
        *,
        repo_id: str,
        phase: Phase,
        detail: str | None,
        reason: str | None = None,
    ) -> None:
        """§13 row 40: every target for the tier is spent. Written BEFORE the exit-8 halt.

        `detail` is the worker's own `stderr_tail` — for the path this exists to record it is
        `TierUnavailable`'s message verbatim (client.py:151-155), which names the tier and every
        target that was tried, in order. It is carried through unparsed on purpose: nothing in
        this codebase branches on message text, and re-deriving the target list from the router
        here would answer a *different* question (what the route says now) than the one the
        finding asks (what was actually tried when it failed).

        This is the last thing written before `RunHalted`, because a halt whose cause was never
        recorded is a halt an operator has to reconstruct from a log tail.
        """
        stamp = _iso(self.clock())
        params = (
            self.run_id,
            repo_id,
            BACKEND_UNAVAILABLE,
            "error",  # this one stops the run; 'warn' would understate it in every report
            sha256_text("\x00".join((repo_id, phase.name, detail or ""))),
            _json(
                {
                    "repo_id": repo_id,
                    "phase": phase.name,
                    "targets_tried": detail or "",
                    "reason": reason or "",
                }
            ),
            stamp,
        )

        async def unit(conn: aiosqlite.Connection) -> None:
            await conn.execute(_INSERT_FINDING, params)

        await self.writer.submit(unit)
