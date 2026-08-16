"""structlog configuration: one pipeline, a JSONL sink and a console sink (ADR-0012, §11.4).

This module is the logging boundary. It imports `structlog` at module scope precisely because
nothing else in the harness may: the model layer, the workers and the state layer take a bound
logger, they never reach for the global one.

**One pipeline, and redaction is in it.** `redaction_processor` sits immediately before the
renderer, after every processor that can add a field. That placement is the whole point: a
call site cannot forget to redact, because the redaction happens after the call site is done —
`log.error("clone failed", url=remote_url)` is safe even though the caller never mentioned it
(§11.4).

**Deterministic field order.** The renderer does not sort blindly and does not emit insertion
order: `ORDER_HEAD` goes first in a fixed sequence (`timestamp`, `level`, `event`, then the
identity keys), and everything else follows sorted alphabetically. Two runs that log the same
facts therefore produce byte-identical lines, which is what makes a golden-log diff readable and
`jq` pipelines over `logs/events-<run_id>.jsonl` stable (§10).

**A write that fails NEVER reaches the caller (see `_Tee`).** This is the same carve-out
`obs/events.py` documents at length and for the same reason: the failing operation is telemetry
and the caller is a worker mid-migration. It is the one place where CLAUDE.md Rule 11 ("fail
loud") is served by *not* raising, because raising here does not surface a problem — it invents
one. It has already happened in this harness: a bound console stream outlived the file behind it,
`ValueError: I/O operation on closed file` came out of a `log.warning(...)` inside a phase's
error path, and five repositories that had done nothing wrong were recorded as
`REQUIRES_HUMAN_INTERVENTION`. A sink is degraded per `_Tee.write`; the degradation is counted
and retained in `write_stats()`, exactly as `EventEmitter` counts `failed_emits` and retains
`failures`.
"""

from __future__ import annotations

import logging
import sys
from collections import deque
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TextIO, cast

import structlog
from structlog.typing import EventDict, FilteringBoundLogger, WrappedLogger

from fleet.obs.redact import JSONValue, redact_mapping

__all__ = [
    "DEFAULT_LEVEL",
    "MAX_RETAINED_FAILURES",
    "ORDER_HEAD",
    "SinkFailure",
    "WriteStats",
    "configure",
    "get_logger",
    "is_configured",
    "order_keys_processor",
    "redaction_processor",
    "reset_write_stats",
    "shutdown",
    "write_stats",
]

DEFAULT_LEVEL: Final = "INFO"

#: Sink failures are diagnostics, not state: keep the recent tail, never an unbounded list.
#: Same bound and same reasoning as `obs.events.MAX_RETAINED_FAILURES` — a stream that dies at
#: the start of a 250-repo run refuses every line after it, and the tail is what an operator
#: reads anyway.
MAX_RETAINED_FAILURES: Final = 64

#: The fixed prefix of every rendered line. Everything not named here is appended sorted.
ORDER_HEAD: Final[tuple[str, ...]] = (
    "timestamp",
    "level",
    "event",
    "run_id",
    "repo_id",
    "phase",
    "seq",
    "event_uid",
)

@dataclass(frozen=True, slots=True)
class SinkFailure:
    """One sink that refused one line. Retained so a degraded write is surfaced, not hidden.

    Deliberately the same shape as `obs.events.EmitFailure` — an operator who has learned to read
    one failure record can read the other.
    """

    sink: str
    error: str


@dataclass(frozen=True, slots=True)
class WriteStats:
    """A snapshot of what the log pipeline has failed to deliver, for humans and for tests.

    `failed_writes` counts sink *refusals* (one line refused by two sinks counts twice, like
    `failed_emits`); `dropped_lines` counts lines that reached **no** sink at all, including the
    fallback — that is the number that means telemetry was actually lost.
    """

    failed_writes: int
    dropped_lines: int
    failures: tuple[SinkFailure, ...]

    @property
    def ok(self) -> bool:
        return self.failed_writes == 0 and self.dropped_lines == 0


_owned_files: list[TextIO] = []
_configured = False
_failed_writes = 0
_dropped_lines = 0
_failures: deque[SinkFailure] = deque(maxlen=MAX_RETAINED_FAILURES)


def redaction_processor(
    _logger: WrappedLogger, _method_name: str, event_dict: EventDict
) -> EventDict:
    """Scrub every value in the line (§11.4). The last thing that runs before the renderer.

    Returns a NEW dict: the caller's bound context and the objects it passed as keyword
    arguments are still the caller's, and a processor that edited them in place would change
    data a worker is about to act on.
    """
    payload = cast(dict[str, JSONValue], dict(event_dict))
    return cast(EventDict, redact_mapping(payload))


def order_keys_processor(
    _logger: WrappedLogger, _method_name: str, event_dict: EventDict
) -> EventDict:
    """`ORDER_HEAD` first, then everything else sorted — so the output diffs cleanly."""
    ordered: dict[str, object] = {}
    for key in ORDER_HEAD:
        if key in event_dict:
            ordered[key] = event_dict[key]
    for key in sorted(k for k in event_dict if k not in ordered):
        ordered[key] = event_dict[key]
    return cast(EventDict, ordered)


class _Sink:
    """One named write target, resolved at write time rather than at `configure()` time.

    `handle=None` means "whatever `sys.stderr` is *now*". That late binding is half the fix: the
    console sink is conceptually "this process's standard error", not "the file object standard
    error happened to be during `configure()`". Capturing the object is what let an in-process
    CLI runner's capture buffer — closed the moment the invocation returned — stay wired into the
    global pipeline for the rest of the process.
    """

    __slots__ = ("_handle", "name")

    def __init__(self, name: str, handle: TextIO | None = None) -> None:
        self.name = name
        self._handle = handle

    @property
    def follows_stderr(self) -> bool:
        """True iff this sink re-resolves `sys.stderr` — i.e. the fallback would be a repeat."""
        return self._handle is None

    def resolve(self) -> TextIO:
        return sys.stderr if self._handle is None else self._handle


#: The last resort: the process's *current* standard error, whatever it is by then.
_FALLBACK: Final = _Sink("fallback:stderr")


class _Tee:
    """The write half of several sinks at once: the console handle and the JSONL file (§8).

    **Degradation strategy, and why this one.** Each sink is attempted independently; one that
    raises (`ValueError: I/O operation on closed file`, `BrokenPipeError`, a full disk) is
    *skipped for that line only* and recorded. If every configured sink refused and none of them
    was already the live `sys.stderr`, the line is retried once against the re-resolved
    `sys.stderr` — a stream that died is usually one stale handle, not the absence of anywhere to
    write, so re-resolving recovers the common case instead of discarding data. Only if that also
    fails is the line dropped, and then `dropped_lines` counts it.

    Skip-and-retry is chosen over the alternatives on purpose:

    * *Propagating* is what caused the incident this class now guards against — the exception
      escaped a `log.warning` in an error path and five healthy repos were marked
      `REQUIRES_HUMAN_INTERVENTION`. Telemetry may not adjudicate the work it describes.
    * *Reopening the sink* would mean this module owning reconnect policy for handles it did not
      open (a caller's `stream=`), and a file reopened mid-run silently forks the log.
    * *Buffering until the sink returns* would grow without bound behind a permanently dead
      stream, i.e. trade a crash for an OOM.

    **Redaction is not weakened by any of this.** By the time a byte reaches `write()`, the
    pipeline has already run `redaction_processor` and the renderer — `data` is a finished,
    redacted JSON line. The fallback re-sends *that same string*, so a `github_pat_…` cannot
    re-enter through the degraded path; there is no route into this method that bypasses the
    processor chain.

    **Nothing here logs.** Reporting a dropped line through `get_logger()` would re-enter this
    very `write()` and, with a sink that fails every line, recurse. The failure is surfaced by
    `write_stats()` instead — counted and retained, the shape `obs.events` established.
    """

    def __init__(self, sinks: Sequence[_Sink]) -> None:
        self._sinks = tuple(sinks)

    def write(self, data: str) -> int:
        global _dropped_lines
        delivered = False
        stderr_tried = False
        for sink in self._sinks:
            stderr_tried = stderr_tried or sink.follows_stderr
            delivered = _write_to(sink, data) or delivered
        if not delivered and not stderr_tried:
            delivered = _write_to(_FALLBACK, data)
        if not delivered:
            _dropped_lines += 1
        # The byte count the caller asked us to write: a partial delivery is still one line to
        # `structlog`, and a short return here would make `WriteLogger` look like the broken one.
        return len(data)

    def flush(self) -> None:
        """Best-effort, and uncounted: a sink that cannot flush already failed its `write()`."""
        for sink in self._sinks:
            with suppress(Exception):
                sink.resolve().flush()


def _write_to(sink: _Sink, data: str) -> bool:
    """Write one line to one sink. Returns whether it landed; never raises."""
    global _failed_writes
    try:
        sink.resolve().write(data)
    except Exception as exc:  # any sink failure degrades this line; none reaches the caller
        _failed_writes += 1
        _failures.append(SinkFailure(sink=sink.name, error=f"{type(exc).__name__}: {exc}"))
        return False
    return True


def write_stats() -> WriteStats:
    """What the pipeline has failed to deliver so far. The observable half of Rule 11.

    Cumulative across `configure()` on purpose: a run that reconfigures per phase verb must not
    be able to zero its own damage report. `reset_write_stats()` is explicit.
    """
    return WriteStats(
        failed_writes=_failed_writes, dropped_lines=_dropped_lines, failures=tuple(_failures)
    )


def reset_write_stats() -> None:
    """Zero the counters. For tests and for a supervisor starting a fresh run in-process."""
    global _failed_writes, _dropped_lines
    _failed_writes = 0
    _dropped_lines = 0
    _failures.clear()


def configure(
    level: str = DEFAULT_LEVEL,
    *,
    json_path: str | Path | None = None,
    stream: TextIO | None = None,
) -> None:
    """Install the one pipeline. Idempotent — calling it again replaces the configuration.

    `json_path` opens `logs/events-<run_id>.jsonl`-style append sinks; `stream` is the console
    handle (default: the process's *current* `sys.stderr`, re-resolved on every write rather
    than captured here — see `_Sink`). Pass `stream` explicitly in tests to assert on the exact
    bytes a call produced. `shutdown()` closes whatever this opened.

    Calling it again fully replaces the sinks, so a process whose stream died recovers by
    reconfiguring: the new `_Tee` holds no reference to the dead handle.
    """
    shutdown()
    sinks: list[_Sink] = [
        _Sink("console") if stream is None else _Sink("console:stream", stream)
    ]
    if json_path is not None:
        path = Path(json_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a", encoding="utf-8")
        _owned_files.append(handle)
        sinks.append(_Sink(f"jsonl:{path.name}", handle))

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            # LAST before rendering, on purpose: every field that exists, exists by now (§11.4).
            redaction_processor,
            order_keys_processor,
            structlog.processors.JSONRenderer(sort_keys=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(_level_number(level)),
        logger_factory=structlog.WriteLoggerFactory(file=cast(TextIO, _Tee(sinks))),
        cache_logger_on_first_use=False,
    )
    global _configured
    _configured = True


def _level_number(level: str) -> int:
    number = logging.getLevelNamesMapping().get(level.upper())
    if number is None:
        raise ValueError(f"unknown log level {level!r}; expected one of DEBUG/INFO/WARNING/ERROR")
    return number


def is_configured() -> bool:
    """Whether `configure()` has run in this process. Diagnostics and tests."""
    return _configured


def shutdown() -> None:
    """Close every file `configure()` opened. Idempotent; safe to call on a bare process."""
    while _owned_files:
        handle = _owned_files.pop()
        handle.flush()
        handle.close()


def get_logger(name: str | None = None) -> FilteringBoundLogger:
    """The logger the rest of the package binds context onto.

    Configures with defaults on first use rather than logging into an unconfigured (and therefore
    UNREDACTED) pipeline — an import-order accident must not be able to disable redaction.
    """
    if not _configured:
        configure()
    return cast(FilteringBoundLogger, structlog.get_logger(name))
