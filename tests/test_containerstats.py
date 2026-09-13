"""`sandbox/containerstats.py`: the `fleet-<run_id>-*` container memory reader (SPEC §12.22
RSS-sampling sub-clause, task B1).

Every test injects a FAKE `CommandRunner` and never invokes a real `docker` command -- per the
task's hard environmental constraint, a real `docker` invocation would risk hanging this session
indefinitely in a headless context. `docker stats`' MEM USAGE format strings used below are
Docker's own documented shape (`docker stats --help`'s example: `"796KiB / 15.57GiB"`), never
observed by running `docker` live.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from fleet.sandbox.containerstats import (
    ContainerStatsReader,
    ContainerStatsUnavailableError,
    parse_mem_usage_bytes,
)
from fleet.sandbox.worktree import run_prefix
from fleet.util.proc import ProcResult

RUN_ID = "11111111-1111-1111-1111-111111111111"


class FakeDockerRunner:
    """Returns a canned `ProcResult` keyed on whether the argv is a `ps` or a `stats` call, so
    one fake exercises the reader's real two-call sequence (list, then stat) exactly as
    `ContainerStatsReader.read_total` performs it."""

    def __init__(
        self,
        *,
        ps_stdout: str = "",
        ps_exit_code: int = 0,
        stats_stdout: str = "",
        stats_exit_code: int = 0,
    ) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.ps_stdout = ps_stdout
        self.ps_exit_code = ps_exit_code
        self.stats_stdout = stats_stdout
        self.stats_exit_code = stats_exit_code

    async def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        deadline: float | None = None,
        timeout_s: float | None = None,
    ) -> ProcResult:
        parts = tuple(argv)
        self.calls.append(parts)
        if parts[1] == "ps":
            return ProcResult(
                argv=parts,
                exit_code=self.ps_exit_code,
                stdout_tail=self.ps_stdout,
                stderr_tail="" if self.ps_exit_code == 0 else "docker ps failed",
                duration_ms=1,
                timed_out=False,
                started=True,
            )
        assert parts[1] == "stats"
        return ProcResult(
            argv=parts,
            exit_code=self.stats_exit_code,
            stdout_tail=self.stats_stdout,
            stderr_tail="" if self.stats_exit_code == 0 else "docker stats failed",
            duration_ms=1,
            timed_out=False,
            started=True,
        )


# --------------------------------------------------------------------------------------
# parse_mem_usage_bytes -- the documented `docker stats` MEM USAGE column format
# --------------------------------------------------------------------------------------


def test_parses_the_documented_kib_gib_example() -> None:
    assert parse_mem_usage_bytes("796KiB / 15.57GiB") == 796 * 1024


def test_parses_zero_bytes() -> None:
    assert parse_mem_usage_bytes("0B / 128MiB") == 0


def test_parses_fractional_mib() -> None:
    assert parse_mem_usage_bytes("15.5MiB / 8GiB") == int(15.5 * 1024**2)


def test_unrecognized_unit_fails_loudly() -> None:
    with pytest.raises(ContainerStatsUnavailableError, match="unrecognized memory unit"):
        parse_mem_usage_bytes("15.5XiB / 8GiB")


def test_missing_separator_fails_loudly() -> None:
    with pytest.raises(ContainerStatsUnavailableError, match="could not parse"):
        parse_mem_usage_bytes("15.5MiB only")


# --------------------------------------------------------------------------------------
# ContainerStatsReader.read_total
# --------------------------------------------------------------------------------------


async def test_zero_matching_containers_returns_an_empty_zero_total_with_no_error() -> None:
    """A run with no active verify containers is a normal, valid state -- not an error -- and
    `docker stats` must never even be invoked in that case."""
    fake = FakeDockerRunner(ps_stdout="")
    reader = ContainerStatsReader(runner=fake)

    total = await reader.read_total(RUN_ID)

    assert total.readings == ()
    assert total.total_bytes == 0
    assert len(fake.calls) == 1  # only `docker ps` -- `docker stats` was never called


async def test_sums_multiple_containers_into_one_total() -> None:
    prefix = run_prefix(RUN_ID)
    ps_stdout = f"{prefix}acme-commons-1\n{prefix}acme-widgets-1\n"
    stats_stdout = (
        f"{prefix}acme-commons-1\t796KiB / 15.57GiB\n{prefix}acme-widgets-1\t1.5MiB / 8GiB\n"
    )
    fake = FakeDockerRunner(ps_stdout=ps_stdout, stats_stdout=stats_stdout)
    reader = ContainerStatsReader(runner=fake)

    total = await reader.read_total(RUN_ID)

    expected_first = 796 * 1024
    expected_second = int(1.5 * 1024**2)
    assert tuple((r.name, r.memory_bytes) for r in total.readings) == (
        (f"{prefix}acme-commons-1", expected_first),
        (f"{prefix}acme-widgets-1", expected_second),
    )
    assert total.total_bytes == expected_first + expected_second
    assert len(fake.calls) == 2  # ps, then stats
    # the stats call must name exactly the two containers `ps` reported, nothing more
    assert list(fake.calls[1][-2:]) == [
        f"{prefix}acme-commons-1",
        f"{prefix}acme-widgets-1",
    ]


async def test_docker_ps_failure_raises_loudly() -> None:
    fake = FakeDockerRunner(ps_exit_code=1)
    reader = ContainerStatsReader(runner=fake)

    with pytest.raises(ContainerStatsUnavailableError, match="docker ps"):
        await reader.read_total(RUN_ID)


async def test_docker_stats_failure_raises_loudly() -> None:
    prefix = run_prefix(RUN_ID)
    fake = FakeDockerRunner(ps_stdout=f"{prefix}acme-commons-1\n", stats_exit_code=1)
    reader = ContainerStatsReader(runner=fake)

    with pytest.raises(ContainerStatsUnavailableError, match="docker stats"):
        await reader.read_total(RUN_ID)


class TOCTOURunner:
    """Simulates the TOCTOU race the retry exists for: a container present in the first `docker
    ps` listing exits (`--rm`) before `docker stats` reaches it, failing that whole batch. A
    re-list (`docker ps`) then reflects only the survivor, and the SECOND `docker stats` call --
    against that freshly re-listed set -- succeeds."""

    def __init__(
        self, *, first_ps_stdout: str, second_ps_stdout: str, second_stats_stdout: str
    ) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._ps_replies = [first_ps_stdout, second_ps_stdout]
        self._second_stats_stdout = second_stats_stdout
        self._stats_call_count = 0

    async def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        deadline: float | None = None,
        timeout_s: float | None = None,
    ) -> ProcResult:
        parts = tuple(argv)
        self.calls.append(parts)
        if parts[1] == "ps":
            stdout = self._ps_replies.pop(0)
            return ProcResult(
                argv=parts,
                exit_code=0,
                stdout_tail=stdout,
                stderr_tail="",
                duration_ms=1,
                timed_out=False,
                started=True,
            )
        assert parts[1] == "stats"
        self._stats_call_count += 1
        if self._stats_call_count == 1:
            return ProcResult(
                argv=parts,
                exit_code=1,
                stdout_tail="",
                stderr_tail="docker stats failed: no such container",
                duration_ms=1,
                timed_out=False,
                started=True,
            )
        return ProcResult(
            argv=parts,
            exit_code=0,
            stdout_tail=self._second_stats_stdout,
            stderr_tail="",
            duration_ms=1,
            timed_out=False,
            started=True,
        )


async def test_a_stats_failure_retries_once_against_a_relisted_container_set() -> None:
    """TOCTOU (round VIII fix): a container in `names` can legitimately exit between `docker ps`
    and `docker stats`, failing the whole `docker stats` batch. The reader must re-list (dropping
    the exited container) and retry ONCE against the survivors, rather than permanently reporting
    the host unreadable on an ordinary mid-build container exit."""
    prefix = run_prefix(RUN_ID)
    fake = TOCTOURunner(
        first_ps_stdout=f"{prefix}acme-commons-1\n{prefix}acme-widgets-1\n",
        second_ps_stdout=f"{prefix}acme-widgets-1\n",
        second_stats_stdout=f"{prefix}acme-widgets-1\t1.5MiB / 8GiB\n",
    )
    reader = ContainerStatsReader(runner=fake)

    total = await reader.read_total(RUN_ID)

    expected = int(1.5 * 1024**2)
    assert tuple((r.name, r.memory_bytes) for r in total.readings) == (
        (f"{prefix}acme-widgets-1", expected),
    )
    assert total.total_bytes == expected
    # ps, stats (fails on the exited container), ps (re-list), stats (succeeds on survivors)
    assert [c[1] for c in fake.calls] == ["ps", "stats", "ps", "stats"]


async def test_malformed_stats_line_raises_loudly() -> None:
    prefix = run_prefix(RUN_ID)
    fake = FakeDockerRunner(
        ps_stdout=f"{prefix}acme-commons-1\n",
        stats_stdout="not-tab-separated\n",
    )
    reader = ContainerStatsReader(runner=fake)

    with pytest.raises(ContainerStatsUnavailableError, match="malformed docker stats"):
        await reader.read_total(RUN_ID)


async def test_ps_filter_is_anchored_and_escaped_against_the_run_prefix() -> None:
    """Mirrors `ContainerSandbox.list_with_verdict`'s own regex-escape requirement: a run id (or
    a repo id folded into a checkout-scoped prefix) can contain a literal `.` or `-`, either of
    which is significant to `docker ps --filter name=`'s REGEX engine (`.` matches any character;
    an unescaped `-`, while not a regex metacharacter itself, is exercised here as part of the
    whole literal-match contract). An unescaped filter would over-match a sibling container."""
    run_id_with_dot = "run.5"
    fake = FakeDockerRunner(ps_stdout="")
    reader = ContainerStatsReader(runner=fake)

    await reader.read_total(run_id_with_dot)

    ps_argv = fake.calls[0]
    prefix = run_prefix(run_id_with_dot)
    filter_arg = ps_argv[ps_argv.index("--filter") + 1]
    assert filter_arg == f"name=^{re.escape(prefix)}"
    assert "\\." in filter_arg  # the literal dot really was escaped, not passed through raw
