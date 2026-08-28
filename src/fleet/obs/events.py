"""`emit()`: the typed event stream — redacted, `seq`-ordered, JSONL + the `events` table.

The event stream is append-only and is the third place a terminal state is recorded (ADR-0014),
alongside the `phases` row and `migration_state.json`. Each event goes to two sinks: a line on
`logs/events-<run_id>.jsonl` (what `jq` reads, §10) and a row in `events` (what SQL reads, §6).

**`seq`, not `ts`, is the ordering key (§6, §11.5).** This module never computes a sequence
number. Allocation happens *inside* the insert —
`VALUES (?, (SELECT COALESCE(MAX(seq),0)+1 FROM events WHERE run_id=?), …)` under
`BEGIN IMMEDIATE`, in `state/repository.py` — because a `MAX(seq)+1` read into Python and passed
back down makes the second concurrent emitter collide on `UNIQUE (run_id, seq)` and raise
`IntegrityError`. There is deliberately no `MAX(` anywhere in this file.

**`event_uid` is the sole conflict target.** The insert is `ON CONFLICT (run_id, event_uid) DO
NOTHING`, so replaying a JSONL tail after a crash converges on the row that is already there and
returns its existing `seq` (§11.7). Conflicting on `seq` instead would make a replay mint
duplicates under new sequence numbers.

**Emission never raises into the caller — and this is the ONE place in this codebase where not
raising is correct.** Everywhere else CLAUDE.md Rule 11 applies: fail loud. Here the caller is a
worker mid-transform and the failing operation is *telemetry*. A full disk, a `SQLITE_BUSY`
storm, or an unserialisable payload must not abort a migration that is otherwise succeeding —
that is the exact bug §6's in-statement allocator was written to fix, and swallowing it at the
sink is the other half of the same fix. It is **swallowed and surfaced**, never swallowed and
hidden: the failure is counted (`failed_emits`), retained (`failures`, most recent last) and
logged at `error` through the redacted pipeline. A caller that wants to know can ask; a caller
that does not, keeps running. The two sinks fail independently, so a broken JSONL file does not
cost the SQL row.

Every payload passes `redact()` before *either* sink (§11.4) — at the boundary, so no call site
can forget it.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections import deque
from collections.abc import Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Protocol, runtime_checkable

from fleet.models.enums import Phase
from fleet.obs.log import get_logger
from fleet.obs.redact import JSONValue, redact_mapping, redact_text
from fleet.state.repository import EventRow

__all__ = [
    "MAX_RETAINED_FAILURES",
    "EmitFailure",
    "EmitResult",
    "EventEmitter",
    "EventSink",
    "events_jsonl_path",
]

#: Failures are diagnostics, not state: keep the recent tail, never an unbounded list.
MAX_RETAINED_FAILURES: Final = 64

_LEVELS: Final[frozenset[str]] = frozenset({"debug", "info", "warning", "error", "critical"})


def events_jsonl_path(root: Path, run_id: str) -> Path:
    """`<root>/logs/events-<run_id>.jsonl` — the one path §8's directory listing names.

    Here rather than at the call sites because there are two callers per run and they must
    agree: `configure(json_path=…)` opens the structlog half of ADR-0012's "one event pipeline,
    two renderers", and `EventEmitter(jsonl_path=…)` writes the typed half. Two call sites
    spelling the same literal is how the two renderers end up in different files, and then §10's
    `jq` recipe reads one of them and reports half a run.
    """
    return (root / "logs" / f"events-{run_id}.jsonl").resolve()


@runtime_checkable
class EventSink(Protocol):
    """The SQL half of the stream. `SqliteStateRepository` satisfies it structurally.

    Depending on the Protocol rather than the concrete repository is CLAUDE.md Guardrail 3, and
    it is what lets a test inject a sink that fails on purpose.
    """

    async def append_event(self, row: EventRow) -> int:
        """Insert one event and return its in-statement-allocated `seq` (§6)."""
        ...


@dataclass(frozen=True, slots=True)
class EmitFailure:
    """One sink that did not take an event. Retained so a failure is surfaced, not hidden."""

    event: str
    event_uid: str
    sink: str
    error: str


@dataclass(frozen=True, slots=True)
class EmitResult:
    """What actually happened to one event. `seq` is `None` iff the SQL sink did not take it."""

    event: str
    event_uid: str
    seq: int | None
    stored: bool
    streamed: bool
    failures: tuple[EmitFailure, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.failures


@dataclass(slots=True)
class EventEmitter:
    """Emits redacted events to the JSONL stream and the `events` table. Never raises.

    Both sinks are optional: an emitter with neither is a valid no-op (a `--dry-run` still logs).
    Construct one per run — `run_id` is the partition `seq` is monotonic within.
    """

    run_id: str
    sink: EventSink | None = None
    jsonl_path: Path | None = None
    failed_emits: int = 0
    failures: deque[EmitFailure] = field(
        default_factory=lambda: deque(maxlen=MAX_RETAINED_FAILURES)
    )
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    async def emit(
        self,
        event: str,
        *,
        level: str = "info",
        repo_id: str | None = None,
        phase: Phase | None = None,
        payload: Mapping[str, JSONValue] | None = None,
        event_uid: str | None = None,
        now: datetime | None = None,
    ) -> EmitResult:
        """Emit one event to every configured sink. Returns what happened; raises nothing.

        `event_uid` defaults to a fresh uuid4 and is the idempotency key: pass the *same* uid to
        re-emit an event after a crash and the row is not duplicated (§11.7).
        """
        uid = event_uid or str(uuid.uuid4())
        failures: list[EmitFailure] = []
        try:
            row = self._build_row(event, level, repo_id, phase, payload, uid, now)
        except Exception as exc:  # a payload we cannot even render is still not the caller's crash
            self._record(failures, event, uid, "build", exc)
            self._surface(failures)
            return EmitResult(event=event, event_uid=uid, seq=None, stored=False, streamed=False)

        streamed = await self._write_jsonl(row, failures)
        seq = await self._write_sql(row, failures)
        self._surface(failures)
        return EmitResult(
            event=row.event,
            event_uid=uid,
            seq=seq,
            stored=seq is not None,
            streamed=streamed,
            failures=tuple(failures),
        )

    # -- internals ----------------------------------------------------------------------

    def _build_row(
        self,
        event: str,
        level: str,
        repo_id: str | None,
        phase: Phase | None,
        payload: Mapping[str, JSONValue] | None,
        uid: str,
        now: datetime | None,
    ) -> EventRow:
        """Redact FIRST, then serialise. Nothing unredacted is ever handed to a sink (§11.4)."""
        moment = now or datetime.now(UTC)
        if moment.tzinfo is None:
            raise ValueError(f"naive datetime {moment!r}: every persisted instant is UTC (§11.5)")
        clean = redact_mapping(payload or {})
        return EventRow(
            run_id=self.run_id,
            seq=0,  # ignored on the way in: the INSERT allocates it in-statement (§6)
            ts=moment.astimezone(UTC).isoformat(timespec="microseconds"),
            level=level if level in _LEVELS else "info",
            event=redact_text(event),
            event_uid=uid,
            # No `default=`: a value that survived redaction unserialised would be rendered by
            # `json` itself, AFTER the redactor saw it. `redact()` already renders unknown types.
            payload=json.dumps(clean, sort_keys=True, separators=(",", ":")),
            repo_id=None if repo_id is None else redact_text(repo_id),
            phase=phase,
        )

    async def _write_jsonl(self, row: EventRow, failures: list[EmitFailure]) -> bool:
        if self.jsonl_path is None:
            return False
        line = json.dumps(
            {
                "ts": row.ts,
                "level": row.level,
                "event": row.event,
                "run_id": row.run_id,
                "repo_id": row.repo_id,
                "phase": None if row.phase is None else int(row.phase),
                "event_uid": row.event_uid,
                "payload": json.loads(row.payload),
            },
            sort_keys=False,
        )
        try:
            # One append per line, serialised: two concurrent emitters must not interleave a line.
            async with self._lock:
                await asyncio.to_thread(_append_line, self.jsonl_path, line)
        except Exception as exc:
            self._record(failures, row.event, row.event_uid, "jsonl", exc)
            return False
        return True

    async def _write_sql(self, row: EventRow, failures: list[EmitFailure]) -> int | None:
        if self.sink is None:
            return None
        try:
            return await self.sink.append_event(row)
        except Exception as exc:
            self._record(failures, row.event, row.event_uid, "events", exc)
            return None

    def _record(
        self, failures: list[EmitFailure], event: str, uid: str, sink: str, exc: Exception
    ) -> None:
        failure = EmitFailure(
            event=event, event_uid=uid, sink=sink, error=f"{type(exc).__name__}: {exc}"
        )
        failures.append(failure)
        self.failures.append(failure)
        self.failed_emits += 1

    def _surface(self, failures: Iterable[EmitFailure]) -> None:
        """Counted and retained above; here it is also *said out loud*, through the log pipeline.

        Wrapped, because a logger that is itself broken must not turn a dropped event into a
        dropped migration.
        """
        for failure in failures:
            # The last resort: if the logger itself is broken there is nowhere left to report to,
            # and a dropped event must still not become a dropped migration.
            with suppress(Exception):
                get_logger("fleet.obs.events").error(
                    "event_emit_failed",
                    run_id=self.run_id,
                    emitted_event=failure.event,
                    event_uid=failure.event_uid,
                    sink=failure.sink,
                    error=failure.error,
                )


def _append_line(path: Path, line: str) -> None:
    """One `write` of one line in append mode: the POSIX call the JSONL stream is built on."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
