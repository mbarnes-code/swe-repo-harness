"""`create_subprocess_exec` wrapper: timeout, capture, tail truncation (ADR-0003, SPEC §11.1/§11.3).

Every shell-out in this harness — `git`, `bazel`, `ast-grep`, `gh`, `docker` — goes through
`run()`. Four properties are load-bearing, and each was a defect before it was code:

* **The deadline kills the process TREE, not the coroutine.** `asyncio.timeout` and
  `CancelledError` cannot reach a child process (SPEC §11.1, `workers/base.py`): cancelling the
  awaiting coroutine leaves the `git`/`bazel` process running, holding a CPU core and the
  worktree lock the cleanup path is about to delete, while the runner has already recorded the
  task as failed and moved on. So the child is started with `start_new_session=True` — it becomes
  a process-group leader — and expiry sends `SIGTERM` to the whole **group**, waits
  `term_grace_s`, then sends `SIGKILL` to the group. A grandchild (`bazel` server, a shell's
  background job) inherits the group and therefore dies with it.
* **The deadline is absolute, in the loop's monotonic clock.** It is `WorkerContext.deadline`
  passed straight through — never a duration recomputed per call, which would let a chain of
  "10 minute" subprocesses outlive a 10-minute worker.
* **Output capture is bounded by construction.** A Gradle or Bazel failure can emit hundreds of
  megabytes; `stdout=PIPE` + `communicate()` would hold all of it as a Python object. Both
  streams are written straight to files and only the last `LOG_TAIL_BYTES` are ever read back
  (SPEC §11.3: "tail-truncated to 32 KiB at capture time, before it reaches Python memory").
  The full stream stays on disk at `ProcResult.stdout_path` — that path is what
  `WorkerError.artifact_ref` records.
* **Tails are redacted at capture** (SPEC §11.4): a build log that echoes a token must never
  exist unredacted as a Python object beyond the read buffer.

No shell is ever involved — neither the `exec` family's shell flag nor the shell-spawning
`asyncio` variant. Commands are argv lists, so a repo name, a branch, or a model-proposed path
can never be parsed as shell syntax. Passing a `str` as `argv` is a `TypeError`, not a silently
character-iterated command.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from fleet.models.base import LOG_TAIL_BYTES, _truncate_tail
from fleet.obs.redact import redact_text

TAIL_BYTES = LOG_TAIL_BYTES
"""What a durable `attempts` row keeps. Re-exported so callers need not know which module owns
the constant; the truncation itself is `models.base`'s single `TruncatedStr` implementation."""

DEFAULT_TERM_GRACE_S = 5.0
"""Seconds between `SIGTERM` and `SIGKILL`. Long enough for `bazel` to unwind its client, short
enough that a wave drain (`budgets.wave_drain_timeout_s`) is not spent waiting on one process."""

TIMEOUT_EXIT_CODE = 124
"""GNU `timeout(1)`'s convention, used only when the deadline had already passed at call time so
there is no real exit status. A killed process reports its real negative signal code instead."""


@dataclass(frozen=True, slots=True)
class ProcResult:
    """One subprocess invocation, as structured evidence rather than an unlabelled tuple.

    `attempts` rows are built from this directly: `command`, `exit_code`, `duration_ms` and the
    truncated output are exactly the columns SPEC §3.3 requires per attempt.
    """

    argv: tuple[str, ...]
    exit_code: int
    stdout_tail: str
    stderr_tail: str
    duration_ms: int
    timed_out: bool
    started: bool = True
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    stdout_path: Path | None = None
    stderr_path: Path | None = None
    cwd: Path | None = None

    @property
    def ok(self) -> bool:
        """The exit code IS the verdict (SPEC §3.3) — a timed-out process is never `ok`, even in
        the pathological case where the signal it died from produced a zero status."""
        return self.started and not self.timed_out and self.exit_code == 0

    @property
    def stdout_truncated(self) -> bool:
        return self.stdout_bytes > LOG_TAIL_BYTES

    @property
    def stderr_truncated(self) -> bool:
        return self.stderr_bytes > LOG_TAIL_BYTES

    def command_line(self) -> str:
        """Display only — deliberately NOT a string any code may hand to a shell."""
        return " ".join(self.argv)


class CommandRunner(Protocol):
    """The seam `sandbox/container.py` and the git/bazel wrappers depend on instead of importing
    `run` directly (CLAUDE.md guardrail 3: dependency inversion at every external boundary).

    A test injects a recording fake and asserts on the argv that WOULD have been executed, so
    command construction is verifiable with no Docker daemon and no network.
    """

    async def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = ...,
        env: Mapping[str, str] | None = ...,
        deadline: float | None = ...,
        timeout_s: float | None = ...,
    ) -> ProcResult: ...


def _validate_argv(argv: Sequence[str]) -> tuple[str, ...]:
    """A `str` here is the classic shell-injection foothold: `create_subprocess_exec("git log")`
    would iterate the characters, and the "fix" a hurried author reaches for is to hand the
    string to a shell."""
    if isinstance(argv, str | bytes):
        raise TypeError("argv must be a sequence of arguments, never a command string (no shell)")
    parts = tuple(argv)
    if not parts:
        raise ValueError("argv must be non-empty")
    for part in parts:
        if not isinstance(part, str):
            raise TypeError(f"argv entries must be str, got {type(part).__name__}")
    return parts


def _signal_group(proc: asyncio.subprocess.Process, sig: signal.Signals) -> None:
    """Signal the child's whole process group. Signalling only `proc.pid` is what leaves the
    orphaned grandchild this module exists to prevent."""
    if proc.returncode is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except (ProcessLookupError, PermissionError):
        # Race with a normal exit, or a child that changed its own group. Fall back to the
        # direct child so we never silently skip the kill.
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.send_signal(sig)


async def _kill_process_group(proc: asyncio.subprocess.Process, grace_s: float) -> int:
    """SIGTERM → grace → SIGKILL, on the group. Returns the child's final status.

    The waits are `shield`ed because this runs on the cancellation path too: a second cancel
    must not abort the escalation halfway and re-create the orphan.
    """
    if proc.returncode is not None:
        return proc.returncode
    _signal_group(proc, signal.SIGTERM)
    if grace_s > 0:
        with contextlib.suppress(TimeoutError, asyncio.CancelledError):
            async with asyncio.timeout(grace_s):
                await asyncio.shield(proc.wait())
    if proc.returncode is None:
        _signal_group(proc, signal.SIGKILL)
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.shield(proc.wait())
    return proc.returncode if proc.returncode is not None else -int(signal.SIGKILL)


def _read_tail(path: Path) -> tuple[str, int]:
    """Read at most `LOG_TAIL_BYTES` from the END of a log file, redacted.

    Only the window is ever resident, so a 400 MB stderr costs 32 KiB of RSS here. The single
    truncator (`models.base._truncate_tail`, the `TruncatedStr` validator) is applied as a guard
    rather than re-implemented: decoding with `errors="replace"` can expand a 1-byte sequence
    into a 3-byte U+FFFD and push the window back over the durable-row bound.
    """
    try:
        size = path.stat().st_size
    except OSError:  # pragma: no cover - the stream file always exists while we own it
        return "", 0
    with path.open("rb") as handle:
        if size > LOG_TAIL_BYTES:
            handle.seek(size - LOG_TAIL_BYTES)
        window = handle.read(LOG_TAIL_BYTES)
    text = window.decode("utf-8", errors="replace")
    guarded = _truncate_tail(text)
    tail = guarded if isinstance(guarded, str) else text
    dropped = size - len(window)
    if dropped > 0:
        tail = f"{tail}\n[truncated {dropped} bytes]"
    return redact_text(tail), size


async def run(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    deadline: float | None = None,
    timeout_s: float | None = None,
    log_dir: Path | None = None,
    log_stem: str = "proc",
    term_grace_s: float = DEFAULT_TERM_GRACE_S,
    semaphore: asyncio.Semaphore | None = None,
) -> ProcResult:
    """Run `argv` under a deadline, capturing bounded output. Never raises on a non-zero exit.

    `deadline` is an ABSOLUTE `loop.time()` — `WorkerContext.deadline`, passed straight through
    (SPEC §7.1). `timeout_s` is the convenience form for callers with no worker context; when
    both are given the EARLIER wins, so a per-call timeout can only tighten the worker's ceiling,
    never extend it past the wave that would reap the process.

    A call made after the deadline has already passed does not spawn anything: a subprocess
    launched past the deadline outlives the wave that would reap it (SPEC §7.1).

    `semaphore` is `limits.subprocess` (SPEC §11.1); it is a parameter rather than a module
    global so the bound is the one the run's config declared.
    """
    parts = _validate_argv(argv)
    loop = asyncio.get_running_loop()
    if timeout_s is not None:
        candidate = loop.time() + timeout_s
        deadline = candidate if deadline is None else min(deadline, candidate)

    if semaphore is not None:
        async with semaphore:
            return await _run_locked(
                parts,
                cwd=cwd,
                env=env,
                deadline=deadline,
                log_dir=log_dir,
                log_stem=log_stem,
                term_grace_s=term_grace_s,
            )
    return await _run_locked(
        parts,
        cwd=cwd,
        env=env,
        deadline=deadline,
        log_dir=log_dir,
        log_stem=log_stem,
        term_grace_s=term_grace_s,
    )


async def _run_locked(
    parts: tuple[str, ...],
    *,
    cwd: Path | None,
    env: Mapping[str, str] | None,
    deadline: float | None,
    log_dir: Path | None,
    log_stem: str,
    term_grace_s: float,
) -> ProcResult:
    loop = asyncio.get_running_loop()
    started_at = loop.time()

    if deadline is not None and started_at >= deadline:
        return ProcResult(
            argv=parts,
            exit_code=TIMEOUT_EXIT_CODE,
            stdout_tail="",
            stderr_tail="deadline had already passed; process was not started",
            duration_ms=0,
            timed_out=True,
            started=False,
            cwd=cwd,
        )

    keep_logs = log_dir is not None
    sink_dir = (
        Path(log_dir) if log_dir is not None else Path(tempfile.mkdtemp(prefix="fleet-proc-"))
    )
    sink_dir.mkdir(parents=True, exist_ok=True)
    unique = f"{log_stem}-{uuid4().hex[:8]}"
    out_path = sink_dir / f"{unique}.stdout.log"
    err_path = sink_dir / f"{unique}.stderr.log"

    timed_out = False
    try:
        with out_path.open("wb") as out_fh, err_path.open("wb") as err_fh:
            proc = await asyncio.create_subprocess_exec(
                *parts,
                cwd=None if cwd is None else str(cwd),
                env=None if env is None else dict(env),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=out_fh,
                stderr=err_fh,
                # The whole point: the child leads its own process group, so `killpg` reaches
                # every grandchild it spawns.
                start_new_session=True,
            )
            try:
                if deadline is None:
                    exit_code = await proc.wait()
                else:
                    try:
                        async with asyncio.timeout_at(deadline):
                            exit_code = await proc.wait()
                    except TimeoutError:
                        timed_out = True
                        exit_code = await _kill_process_group(proc, term_grace_s)
            except asyncio.CancelledError:
                # `CancelledError` never reaches the child (SPEC §11.1) — kill it explicitly,
                # then let the cancellation continue.
                await _kill_process_group(proc, term_grace_s)
                raise

        duration_ms = int((loop.time() - started_at) * 1000)
        stdout_tail, stdout_bytes = _read_tail(out_path)
        stderr_tail, stderr_bytes = _read_tail(err_path)
        return ProcResult(
            argv=parts,
            exit_code=exit_code,
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
            duration_ms=duration_ms,
            timed_out=timed_out,
            stdout_bytes=stdout_bytes,
            stderr_bytes=stderr_bytes,
            stdout_path=out_path if keep_logs else None,
            stderr_path=err_path if keep_logs else None,
            cwd=cwd,
        )
    finally:
        if not keep_logs:
            shutil.rmtree(sink_dir, ignore_errors=True)
