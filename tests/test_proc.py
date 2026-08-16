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
from fleet.util.proc import TIMEOUT_EXIT_CODE, ProcResult, run

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
    # The tail is the durable-row bound plus only the truncation marker.
    assert len(result.stdout_tail.encode()) < LOG_TAIL_BYTES + 128
    assert "[truncated " in result.stdout_tail

    assert result.stdout_path is not None
    assert result.stdout_path.stat().st_size == total, "the full stream must survive on disk"


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


async def test_result_is_labelled_not_a_tuple() -> None:
    """`attempts` rows are built from this: an unlabelled tuple is how stdout ends up in the
    `stderr_tail` column."""
    result = await run([sys.executable, "-c", "print('ok')"], timeout_s=30)
    assert isinstance(result, ProcResult)
    assert result.argv[0] == sys.executable
    assert result.stdout_tail.strip() == "ok"
    assert result.command_line().startswith(sys.executable)
