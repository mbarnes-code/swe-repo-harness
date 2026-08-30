"""`util/proc.py`: the subprocess boundary (SPEC §11.1, §11.3).

These tests exist because "the coroutine raised TimeoutError" proves nothing about the machine.
The property that matters is that the *process tree* is gone — an orphaned `bazel` or `git` keeps
a CPU core and the worktree lock the cleanup path is about to delete, while the runner has
already recorded the task failed and moved on (SPEC §11.1). So the headline test spawns a
grandchild and checks its pid afterwards.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

from fleet.models.base import LOG_TAIL_BYTES
from fleet.util import proc
from fleet.util.proc import (
    HEAD_BYTES,
    TIMEOUT_EXIT_CODE,
    ProcResult,
    is_producible_shape,
    no_verdict,
    run,
)
from fleet.workers.base import WorkerError

# A child that spawns its own long-lived child, records the grandchild's pid, then sleeps.
# The grandchild inherits the process group, which is the only reason `killpg` can reach it.
SPAWN_GRANDCHILD = """
import subprocess, sys, time, pathlib
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
pathlib.Path(sys.argv[1]).write_text(str(child.pid))
print("spawned", flush=True)
time.sleep(300)
"""

# Ignores SIGTERM entirely: only the SIGKILL escalation can end it.
IGNORES_SIGTERM = """
import signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
print("armed", flush=True)
while True:
    time.sleep(0.05)
"""


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - not expected for our own children
        return True
    return True


def _same_dir(left: str | Path, right: str | Path) -> bool:
    """Sync helper: /tmp is a symlink on some hosts, so compare resolved paths."""
    return os.path.realpath(left) == os.path.realpath(right)


def _wait_for_pid_exit(pid: int, timeout_s: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)
    return not _pid_alive(pid)


async def test_deadline_kills_the_whole_process_tree(tmp_path: Path) -> None:
    """The headline property: on deadline expiry the GRANDCHILD is dead too.

    Why this and not `pytest.raises(TimeoutError)`: cancelling the awaiting coroutine is free and
    proves nothing. A `bazel` server or a shell's background job that survives its parent holds a
    CPU core and the worktree lock that `on_cancel` is about to remove, and the runner has
    already moved on — which is precisely the orphan `start_new_session` + `killpg` prevents.
    """
    pid_file = tmp_path / "grandchild.pid"
    result = await run(
        [sys.executable, "-c", SPAWN_GRANDCHILD, str(pid_file)],
        timeout_s=1.5,
        term_grace_s=0.5,
    )

    assert result.timed_out is True
    assert result.ok is False
    assert pid_file.exists(), "child never got far enough to spawn a grandchild"
    grandchild_pid = int(pid_file.read_text())

    assert _wait_for_pid_exit(grandchild_pid), (
        f"grandchild pid {grandchild_pid} survived the deadline — the process GROUP was not "
        "killed, so an orphan still holds its CPU core and its worktree lock"
    )


async def test_sigterm_ignoring_process_is_escalated_to_sigkill() -> None:
    """A process that ignores SIGTERM is still killed, and within the grace window.

    Real builds trap SIGTERM (Gradle daemons, `bazel`), so a teardown that stops at SIGTERM is a
    teardown that does not tear down.
    """
    started = time.monotonic()
    result = await run([sys.executable, "-c", IGNORES_SIGTERM], timeout_s=1.0, term_grace_s=0.5)
    elapsed = time.monotonic() - started

    assert result.timed_out is True
    assert result.exit_code == -9, f"expected SIGKILL (-9), got {result.exit_code}"
    assert elapsed < 8.0, "escalation did not happen inside the grace window"


async def test_huge_output_is_tail_bounded_but_fully_on_disk(tmp_path: Path) -> None:
    """~8 MB of stdout must cost 32 KiB of RSS, with the full stream still recoverable.

    A 400 MB Gradle stderr held as a Python object is the §11.3 memory ceiling breached by the
    capture path itself; the durable row keeps a tail, and `WorkerError.artifact_ref` keeps the
    path to everything else.
    """
    emit = "import sys\nline = 'x' * 1023 + '\\n'\nsys.stdout.write(line * 8192)\n"
    log_dir = tmp_path / "logs"
    result = await run([sys.executable, "-c", emit], timeout_s=60, log_dir=log_dir)

    assert result.ok is True
    total = 1024 * 8192
    assert result.stdout_bytes == total
    assert result.stdout_truncated is True
    # The capture — content plus every marker it adds — must fit the durable-row bound exactly,
    # not "roughly": a return value over LOG_TAIL_BYTES here is what silently re-truncates the
    # next time it passes through a `TruncatedStr` field (`WorkerError.stderr_tail`), stacking a
    # second marker on top of this one. See test_proc.py's double-truncation regression tests
    # below for the defect this bound exists to prevent.
    assert len(result.stdout_tail.encode()) <= LOG_TAIL_BYTES
    assert "bytes elided" in result.stdout_tail

    assert result.stdout_path is not None
    assert result.stdout_path.stat().st_size == total, "the full stream must survive on disk"


# --------------------------------------------------------------------------------------
# a REALISTIC multi-target `bazel build --keep_going` failure log (research round 38, Q3).
#
# `"x" * 100_000` proves the truncator's arithmetic and nothing about the use case it exists
# for: a real Bazel failure interleaves multiple targets' diagnostics with progress noise, and
# under `--keep_going` the FIRST failing target is ordinarily the root cause while everything
# after it is cascade. The shape below is modelled on the real 79,337-byte fixture research-38
# measured (Q3.2/Q3.3) — an unresolved-import/no-method pair for the first target
# (`acme-case-rs`), a long run of a second target's generated-module diagnostics
# (`acme-codec-rs`), and Bazel's own closing summary lines.
# --------------------------------------------------------------------------------------

_FIRST_TARGET_ERRORS = """\
INFO: Analyzed 2 targets (0 packages loaded, 0 targets configured).
ERROR: /workspace/rust/acme-case-rs/BUILD.bazel:3:11: Compiling Rust rlib acme_case_rs
  (2 files) failed: (Exit 101): rustc failed
error[E0432]: unresolved import `heck::ToSnakeCaseMissing`
 --> rust/acme-case-rs/src/lib.rs:3:5
error[E0599]: no method named `to_snake_case_missing` found for reference `&str`
 --> rust/acme-case-rs/src/lib.rs:9:24
error: aborting due to 2 previous errors
ERROR: Build did NOT complete successfully for target //rust/acme-case-rs:acme-case-rs
"""

_CASCADE_MARKER_MODULE = "acme_codec_generated_marker_module"


def _second_target_cascade(n_modules: int = 150, marker_at: int = 75) -> str:
    """`n_modules` generated-source diagnostics for the SECOND target — the cascade, not the
    cause. One module (`marker_at`) is tagged uniquely so a test can assert it was elided."""
    blocks = []
    for m in range(n_modules):
        tag = _CASCADE_MARKER_MODULE if m == marker_at else f"generated_{m:03d}"
        blocks.append(
            f"ERROR: /workspace/rust/acme-codec-rs/BUILD.bazel:5:11: Compiling Rust rlib "
            f"acme_codec_rs ({m}) failed\n"
            f"error[E0433]: failed to resolve: use of undeclared crate or module `missing_{tag}`\n"
            f"  --> rust/acme-codec-rs/src/{tag}.rs:{m + 1}:5\n"
            f"error[E0599]: no method named `decode_{tag}` found\n"
            f"  --> rust/acme-codec-rs/src/{tag}.rs:{m + 20}:12\n"
        )
    return "".join(blocks)


_BUILD_SUMMARY = """\
error: aborting due to 240 previous errors
For more information about this error, try `rustc --explain E0432`.
INFO: Elapsed time: 42.734s, Critical Path: 38.2s
INFO: 12 processes: 10 internal, 2 linux-sandbox.
FAILED: Build did NOT complete successfully
"""


def _realistic_bazel_failure_log() -> str:
    log = _FIRST_TARGET_ERRORS + _second_target_cascade() + _BUILD_SUMMARY
    assert len(log.encode()) > LOG_TAIL_BYTES, "fixture must actually force truncation"
    return log


async def test_first_targets_diagnostics_survive_a_large_multitarget_failure_log(
    tmp_path: Path,
) -> None:
    """The head/tail decision, pinned: under `--keep_going`, the FIRST target's errors are
    usually the root cause and the rest is cascade (research-38 Q3.2). A tail-only truncator
    was measured to discard exactly the first target's diagnostics while keeping only the
    build's closing summary — the consequence, not the cause. This asserts the fix: the first
    target's unique error text AND the final summary both survive, while a marker planted deep
    in the cascade does not.
    """
    log = _realistic_bazel_failure_log()
    script = f"import sys\nsys.stderr.write({log!r})\n"
    result = await run([sys.executable, "-c", script], timeout_s=30, log_dir=tmp_path / "logs")

    assert result.stderr_truncated is True
    tail = result.stderr_tail
    assert "ToSnakeCaseMissing" in tail, "the first (root-cause) target's error was dropped"
    assert "no method named `to_snake_case_missing`" in tail
    assert "Build did NOT complete successfully" in tail, "the closing summary was dropped"
    assert _CASCADE_MARKER_MODULE not in tail, (
        "a marker planted deep in the second target's cascade survived — the head/tail split "
        "did not actually elide the middle"
    )
    assert "bytes elided" in tail
    # The head slice is bounded by HEAD_BYTES, not the whole first target's block, by design.
    assert len(tail.encode()) <= LOG_TAIL_BYTES


async def test_head_and_tail_split_never_double_truncates_through_workererror(
    tmp_path: Path,
) -> None:
    """Regression test for the defect research-38 Q3.3 found: `_read_head_and_tail`'s own
    output landing OVER `LOG_TAIL_BYTES` (content plus its `[truncated N bytes]` marker) meant
    the next `TruncatedStr` validator downstream — `WorkerError.stderr_tail`, exactly what
    `buildverify.error_from_proc` constructs from a real `ProcResult` — silently re-truncated it
    and stacked a SECOND marker on top of the first. Reproduced against the same realistic
    multi-target log used above, not a synthetic blob, because the defect only manifests once
    the capture is big enough to truncate at all.
    """
    log = _realistic_bazel_failure_log()
    script = f"import sys\nsys.stderr.write({log!r})\n"
    result = await run([sys.executable, "-c", script], timeout_s=30, log_dir=tmp_path / "logs")
    tail = result.stderr_tail

    assert len(tail.encode()) <= LOG_TAIL_BYTES, (
        "proc.py's own capture already overflowed the durable-row bound; every downstream "
        "TruncatedStr field will silently re-truncate it"
    )

    # The exact consumer path: buildverify.error_from_proc hands `ProcResult.stderr_tail`
    # straight to `WorkerError(stderr_tail=...)`, a `TruncatedStr` field.
    error = WorkerError(
        failure_class="BUILD_ERROR", retryable=True, exit_code=1, stderr_tail=tail
    )

    assert error.stderr_tail.count("bytes elided") == 1, "the elision marker must not multiply"
    assert "[truncated" not in error.stderr_tail, (
        "a second, differently-shaped truncation marker appeared — the double-truncation defect"
    )
    # Modulo FleetModel's `str_strip_whitespace`, the WorkerError must carry the SAME text
    # proc.py produced: no bytes were cut a second time.
    assert error.stderr_tail == tail.strip()


def test_head_bytes_is_smaller_than_the_total_budget() -> None:
    """Sanity bound on the constant itself: a head slice that consumed the whole budget would
    leave no room for the tail's build summary, and one bigger than `LOG_TAIL_BYTES` would make
    `_read_head_and_tail`'s tail-budget arithmetic go negative."""
    assert 0 < HEAD_BYTES < LOG_TAIL_BYTES


async def test_exit_code_and_stderr_survive_a_normal_failure() -> None:
    """The exit code IS the verdict (SPEC §3.3) and the error text is what a repair prompt reads,
    so a wrapper that loses either turns a diagnosable failure into `UNKNOWN`."""
    script = "import sys\nsys.stderr.write('boom: missing dep\\n')\nsys.exit(3)\n"
    result = await run([sys.executable, "-c", script], timeout_s=30)

    assert result.exit_code == 3
    assert result.ok is False
    assert result.timed_out is False
    assert "boom: missing dep" in result.stderr_tail
    assert result.stdout_tail == ""
    assert result.duration_ms >= 0


async def test_cwd_and_env_are_honoured(tmp_path: Path) -> None:
    """`git -C` is not always available (`bazel`, `ast-grep`), so cwd/env must be real."""
    workdir = tmp_path / "sub"
    workdir.mkdir()
    script = "import os, sys\nsys.stdout.write(os.getcwd() + '|' + os.environ['FLEET_MARK'])\n"
    result = await run(
        [sys.executable, "-c", script],
        cwd=workdir,
        env={"FLEET_MARK": "sentinel", "PATH": os.environ.get("PATH", "")},
        timeout_s=30,
    )

    assert result.ok is True
    cwd_out, mark = result.stdout_tail.split("|")
    assert _same_dir(cwd_out, workdir)
    assert mark == "sentinel"


async def test_call_past_the_deadline_never_spawns() -> None:
    """A subprocess launched past the deadline outlives the wave that would reap it (§7.1)."""
    loop = asyncio.get_running_loop()
    result = await run([sys.executable, "-c", "print('should not run')"], deadline=loop.time() - 1)

    assert result.started is False
    assert result.timed_out is True
    assert result.exit_code == TIMEOUT_EXIT_CODE
    assert result.stdout_tail == ""


async def test_earlier_of_deadline_and_timeout_wins() -> None:
    """A per-call timeout may tighten a worker's ceiling, never extend it: otherwise a chain of
    'ten minute' subprocesses outlives the ten-minute worker that owns them."""
    loop = asyncio.get_running_loop()
    started = time.monotonic()
    result = await run(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        deadline=loop.time() + 1.0,
        timeout_s=600,
        term_grace_s=0.5,
    )
    assert result.timed_out is True
    assert time.monotonic() - started < 10.0


async def test_cancellation_kills_the_child_too(tmp_path: Path) -> None:
    """`CancelledError` never reaches a child (§11.1) — the wrapper must kill it explicitly, or
    `on_cancel`'s 30 s grace window returns while the process is still building."""
    pid_file = tmp_path / "grandchild.pid"
    task = asyncio.create_task(
        run(
            [sys.executable, "-c", SPAWN_GRANDCHILD, str(pid_file)],
            timeout_s=60,
            term_grace_s=0.2,
        )
    )
    for _ in range(200):
        await asyncio.sleep(0.05)
        if pid_file.exists():
            break
    assert pid_file.exists(), "child never spawned its grandchild"
    grandchild_pid = int(pid_file.read_text())

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert _wait_for_pid_exit(grandchild_pid), "cancellation left an orphaned grandchild"


def test_argv_must_not_be_a_command_string() -> None:
    """`run("git log")` would iterate characters; the tempting 'fix' is `shell=True`. Refused."""
    with pytest.raises(TypeError):
        asyncio.run(run("echo hello"))  # type: ignore[arg-type]


def test_no_shell_anywhere_in_the_subprocess_boundary() -> None:
    """Grepped rather than argued: untrusted text (repo names, branches, model-proposed paths)
    reaches these modules, and a single `shell=True` makes every one of them injectable."""
    roots = [
        Path(proc.__file__),
        Path(proc.__file__).parents[1] / "sandbox" / "worktree.py",
        Path(proc.__file__).parents[1] / "sandbox" / "container.py",
    ]
    for path in roots:
        source = path.read_text()
        assert "shell=True" not in source, f"{path} uses shell=True"
        assert "create_subprocess_shell" not in source, f"{path} uses create_subprocess_shell"
        assert "subprocess.run" not in source, f"{path} uses blocking subprocess.run"


def test_no_forbidden_construct_appears_anywhere_in_src_fleet() -> None:
    """SPEC §12 item 5, reproduced directly: `grep -rn "import pickle\\|ThreadPoolExecutor\\|
    subprocess.run" src/fleet/` must return nothing, over the WHOLE tree — not just the three
    files `test_no_shell_anywhere_in_the_subprocess_boundary` above scopes to, and a different
    property besides: that test is about `shell=True` reaching the subprocess boundary; this one
    is about three constructs the criterion forbids outright, each for its own reason.

    - `import pickle`: pickle's REDUCE/BUILD opcodes are an arbitrary-callable VM (CWE-502,
      `docs/DECISIONS.md` reference #2) — the reference harness's checkpoint module carries an
      explicit note that there is *deliberately* no `import pickle`, and this harness inherits
      that: every checkpoint is re-hydrated through Pydantic `model_validate` instead.
    - `ThreadPoolExecutor`: ADR-0003's offload boundary is `ProcessPoolExecutor` for CPU-bound
      work and `asyncio.create_subprocess_exec` for everything that blocks on a socket or pipe
      (`docs/SPEC.md` §7); a `ThreadPoolExecutor` is a third lane neither `limits.cpu_pool` nor
      `limits.subprocess` accounts for.
    - `subprocess.run`: it blocks the event loop — exactly the failure mode this module
      (`util/proc.py`) and every `ModelBackend.invoke` exist to avoid by going through
      `asyncio.create_subprocess_exec` or an async client instead.

    Plain substring containment, not `grep`'s basic-regex alternation, so an unescaped `.` in
    `subprocess.run` cannot pass on a look-alike character — stricter than the literal criterion
    command in the one spot where that matters, identical everywhere else. And every file under
    the tree, not only `*.py`: `grep -rn` has no extension filter either, and `src/fleet/` holds
    one non-Python file (`state/schema.sql`) the criterion's own command would still scan.
    `__pycache__/` is excluded — it is gitignored, a build artifact rather than source, and
    scanning it as text raises `UnicodeDecodeError` on the compiled `.pyc` bytes; a real `grep`
    on a fresh checkout would never encounter it at all, since it never lands in git.
    """
    forbidden = ("import pickle", "ThreadPoolExecutor", "subprocess.run")
    src_fleet = Path(proc.__file__).parents[1]
    repo_root = src_fleet.parents[1]
    offenders = [
        f"{path.relative_to(repo_root)}:{n}: {line.strip()}"
        for path in sorted(
            p for p in src_fleet.rglob("*") if p.is_file() and "__pycache__" not in p.parts
        )
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if any(needle in line for needle in forbidden)
    ]
    assert offenders == []


async def test_result_is_labelled_not_a_tuple() -> None:
    """`attempts` rows are built from this: an unlabelled tuple is how stdout ends up in the
    `stderr_tail` column."""
    result = await run([sys.executable, "-c", "print('ok')"], timeout_s=30)
    assert isinstance(result, ProcResult)
    assert result.argv[0] == sys.executable
    assert result.stdout_tail.strip() == "ok"
    assert result.command_line().startswith(sys.executable)


# --------------------------------------------------------------------------------------
# the ScriptedRunner anti-drift invariant (SPEC §7.1: `not started` has exactly one cause)
# --------------------------------------------------------------------------------------
async def test_is_producible_shape_matches_the_real_never_started_branch() -> None:
    """`is_producible_shape`'s claim about `started=False` is checked against a LIVE call, not
    against its own docstring: `_run_locked`'s deadline-already-passed branch is the only place
    `run()` ever sets `started=False`, and it always pairs that with `timed_out=True` and
    `exit_code=TIMEOUT_EXIT_CODE`. If a future change decoupled those three, this assertion —
    not just the docstring — would be the thing that catches it.
    """
    loop = asyncio.get_running_loop()
    real = await run([sys.executable, "-c", "print('should not run')"], deadline=loop.time() - 1)
    assert (real.started, real.timed_out, real.exit_code) == (False, True, TIMEOUT_EXIT_CODE)
    assert is_producible_shape(
        started=real.started, timed_out=real.timed_out, exit_code=real.exit_code
    )

    # Perturbing any one of the three away from what `run()` actually returned must be rejected:
    # these are exactly the states a test double must never be able to construct.
    assert not is_producible_shape(started=False, timed_out=False, exit_code=TIMEOUT_EXIT_CODE)
    assert not is_producible_shape(started=False, timed_out=True, exit_code=1)
    assert not is_producible_shape(started=False, timed_out=False, exit_code=0)

    # `started=True` places no constraint on the other two — a real process may exit any code,
    # timed out or not (including the pathological zero-after-SIGKILL `ProcResult.ok` documents).
    for timed_out, exit_code in [(False, 0), (False, 3), (True, -15), (True, -9), (True, 0)]:
        assert is_producible_shape(started=True, timed_out=timed_out, exit_code=exit_code)


async def test_scripted_runner_cannot_construct_states_the_real_runner_cannot_produce() -> None:
    """Anti-drift, end to end: `ScriptedRunner` (`tests/test_vcs.py`) validates every instance
    against `is_producible_shape`, defined beside `_run_locked` — the one function that actually
    produces a `ProcResult`. If `util/proc.py`'s contract ever changes (a different
    `TIMEOUT_EXIT_CODE`, or `started=False` decoupled from `timed_out`) without `ScriptedRunner`
    following, this test is what fails: the real, live `run()` call below re-derives the ground
    truth every time rather than trusting a constant copied into the test file, so `ScriptedRunner`
    and `util.proc.run` cannot silently drift apart the way the ladder-position counters in
    `workers/base.py` once did (see that module's docstring for the sibling defect).
    """
    from tests.test_vcs import ScriptedRunner  # the double every vcs/gh/gitea test replays against

    loop = asyncio.get_running_loop()
    real = await run([sys.executable, "-c", "pass"], deadline=loop.time() - 1)

    # The one state `run()` actually produces for `started=False` must remain constructible.
    ScriptedRunner(started=real.started, timed_out=real.timed_out, exit_code=real.exit_code)

    # Every other `started=False` pairing must be refused — these are exactly the "impossible"
    # states that let a test assert against evidence no real invocation could generate.
    for started, timed_out, exit_code in [
        (False, False, TIMEOUT_EXIT_CODE),
        (False, True, 1),
        (False, False, 0),
    ]:
        with pytest.raises(ValueError):
            ScriptedRunner(started=started, timed_out=timed_out, exit_code=exit_code)

    # `started=True` remains unconstrained — the double must still cover every real outcome.
    for timed_out, exit_code in [(False, 0), (False, 3), (True, -15), (True, -9), (True, 0)]:
        ScriptedRunner(started=True, timed_out=timed_out, exit_code=exit_code)


# --------------------------------------------------------------------------------------
# no_verdict: the decoder lives with the encoder (ADR-0067 part 4)
# --------------------------------------------------------------------------------------


def test_no_verdict_reads_started_before_timed_out() -> None:
    """`started` must be tested BEFORE `timed_out` — a call made past the deadline carries BOTH
    flags at once (see `_run_locked`'s deadline-already-passed branch), so reading `timed_out`
    first would report a command that never ran as one that ran too long. That is the exact
    misattribution this function exists to prevent, one layer up from where it originates."""
    never_started = ProcResult(
        argv=("git", "rev-parse", "HEAD"),
        exit_code=TIMEOUT_EXIT_CODE,
        stdout_tail="",
        stderr_tail="deadline had already passed; process was not started",
        duration_ms=0,
        timed_out=True,
        started=False,
    )
    reason = no_verdict(never_started)
    assert reason is not None
    assert "never started" in reason
    assert "killed" not in reason


def test_no_verdict_reports_a_real_kill_distinctly_from_never_started() -> None:
    """A process that DID run and was killed at its deadline is a different fact — `TIMEOUT` is
    substantive on the retry ladder for this one, unlike the never-started case above — so the
    reason string must not collapse the two."""
    killed = ProcResult(
        argv=("git", "fetch", "--unshallow"),
        exit_code=-15,
        stdout_tail="",
        stderr_tail="",
        duration_ms=60_000,
        timed_out=True,
        started=True,
    )
    reason = no_verdict(killed)
    assert reason is not None
    assert "killed at its deadline" in reason
    assert "never started" not in reason


def test_no_verdict_is_none_for_a_real_answer() -> None:
    """A command that ran to completion — whatever its exit code — established SOMETHING about
    the question it was asked, even when that something is a legitimate non-zero exit. `None`
    means "trust this result", never "it succeeded"."""
    ran_and_failed = ProcResult(
        argv=("git", "show", "HEAD:.gitmodules"),
        exit_code=128,
        stdout_tail="",
        stderr_tail="fatal: path '.gitmodules' does not exist",
        duration_ms=5,
        timed_out=False,
        started=True,
    )
    assert no_verdict(ran_and_failed) is None


async def test_no_verdict_matches_the_real_never_started_branch() -> None:
    """Checked against a LIVE call, not only against hand-built `ProcResult`s — the same
    discipline `test_is_producible_shape_matches_the_real_never_started_branch` applies to
    `is_producible_shape`, so a future change to `_run_locked`'s deadline branch cannot drift out
    of step with this decoder silently."""
    loop = asyncio.get_running_loop()
    real = await run([sys.executable, "-c", "print('should not run')"], deadline=loop.time() - 1)
    reason = no_verdict(real)
    assert reason is not None and "never started" in reason
