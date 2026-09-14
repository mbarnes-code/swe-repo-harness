"""`orchestrator/memory_guard.py`: the periodic host-memory sampler + `resource_guard()`
implementation (SPEC §12.22 runtime RSS-sampling sub-clause, task 27 / round VI B2).

Every test constructs `HostMemorySampler` with FAKE `cgroup_reader`/`container_reader` seams --
`container_reader` is a real `ContainerStatsReader` wired to `FakeDockerRunner` (task 26's own
fake-seam pattern, copied from `tests/test_containerstats.py`), never a real `docker`/`cgroup`
read. No real `docker` command is ever invoked, per the task's hard environmental constraint.

This is wiring-level proof only (Rule 13: this task does not close §12.22 or its RSS-sampling
sub-clause) -- it does not exercise a real 50k-file scan or real containers; that is task 28's
(B3's) job.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from fleet.orchestrator.memory_guard import HostMemorySampler
from fleet.orchestrator.runner import HaltReason
from fleet.sandbox.containerstats import ContainerStatsReader
from fleet.util.proc import ProcResult

RUN_ID = "22222222-2222-2222-2222-222222222222"
_MIB = 1024 * 1024


class FakeDockerRunner:
    """Copied from `tests/test_containerstats.py`: a canned `ProcResult` keyed on `ps` vs.
    `stats`, so a sampler test can control the container total precisely without ever invoking a
    real `docker` binary."""

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


def _no_containers() -> ContainerStatsReader:
    """A reader with zero running containers -- `docker stats` is never even invoked (mirrors
    `ContainerStatsReader.read_total`'s own documented "there are none" shortcut)."""
    return ContainerStatsReader(runner=FakeDockerRunner(ps_stdout=""))


async def _settle(*, ticks_at_least: float = 1.0, interval_s: float) -> None:
    """Real, millisecond-scale sleep -- long enough for `ticks_at_least` sampler ticks to have
    completed, short enough to keep the suite fast (per the task brief: "use a short injected
    interval... for a fast test -- do not sleep for real seconds")."""
    await asyncio.sleep(interval_s * ticks_at_least + 0.05)


# --------------------------------------------------------------------------------------
# guard() reflects the sampler's last tick (Rule-12 test shape items 1-2)
# --------------------------------------------------------------------------------------


async def test_guard_returns_host_memory_when_the_sample_is_at_or_over_the_ceiling() -> None:
    sampler = HostMemorySampler(
        run_id=RUN_ID,
        max_host_rss_mb=1,  # ceiling = 1 MiB
        max_rss_mb=100_000,  # this leg is not under test here: kept far out of reach
        interval_s=0.001,
        cgroup_reader=lambda: 2 * _MIB,  # whole tree alone already over the 1 MiB ceiling
        rss_reader=lambda: 0,
        container_reader=_no_containers(),
    )
    sampler.start()
    try:
        await _settle(interval_s=0.001)
        assert sampler.guard() is HaltReason.HOST_MEMORY
    finally:
        await sampler.stop()


async def test_guard_returns_none_when_the_sample_is_under_the_ceiling() -> None:
    sampler = HostMemorySampler(
        run_id=RUN_ID,
        max_host_rss_mb=100,  # ceiling = 100 MiB
        max_rss_mb=100_000,  # this leg is not under test here: kept far out of reach
        interval_s=0.001,
        cgroup_reader=lambda: 1 * _MIB,
        rss_reader=lambda: 0,
        container_reader=_no_containers(),
    )
    sampler.start()
    try:
        await _settle(interval_s=0.001)
        assert sampler.guard() is None
    finally:
        await sampler.stop()


async def test_container_memory_is_summed_into_the_same_ceiling_comparison() -> None:
    """The whole-tree reading alone is under the ceiling; adding the container total tips it
    over -- proves the two readers are genuinely SUMMED (§12.22: "whole process tree PLUS every
    container"), not compared separately or the container total ignored."""
    prefix_container_reader = ContainerStatsReader(
        runner=FakeDockerRunner(
            ps_stdout="fleet-22222222-2222-2222-2222-222222222222-worker-1\n",
            stats_stdout="fleet-22222222-2222-2222-2222-222222222222-worker-1\t90MiB / 1GiB\n",
        )
    )
    sampler = HostMemorySampler(
        run_id=RUN_ID,
        max_host_rss_mb=100,  # ceiling = 100 MiB
        max_rss_mb=100_000,  # this leg is not under test here: kept far out of reach
        interval_s=0.001,
        cgroup_reader=lambda: 50 * _MIB,  # alone: comfortably under 100 MiB
        rss_reader=lambda: 0,
        container_reader=prefix_container_reader,  # +90 MiB -> 140 MiB, over ceiling
    )
    sampler.start()
    try:
        await _settle(interval_s=0.001)
        assert sampler.guard() is HaltReason.HOST_MEMORY
    finally:
        await sampler.stop()


# --------------------------------------------------------------------------------------
# task 29 (round VI): `budgets.max_rss_mb` -- the orchestrator's OWN RSS, enforced independently
# of `max_host_rss_mb` above (Rule-12 test shape items 1-3)
# --------------------------------------------------------------------------------------


async def test_guard_returns_host_memory_when_only_orchestrator_rss_is_over_the_ceiling() -> None:
    """The host leg (`max_host_rss_mb`) stays comfortably low -- only the orchestrator's own
    RSS breaches `max_rss_mb`. Proves the new leg is independently wired, not merely riding
    along with the existing `max_host_rss_mb` comparison (which alone would report `None`
    here -- see `test_guard_returns_none_when_the_sample_is_under_the_ceiling` above for that
    exact reading in isolation)."""
    sampler = HostMemorySampler(
        run_id=RUN_ID,
        max_host_rss_mb=100_000,  # this leg is not under test here: kept far out of reach
        max_rss_mb=1,  # ceiling = 1 MiB
        interval_s=0.001,
        cgroup_reader=lambda: 1 * _MIB,  # host leg: comfortably low
        rss_reader=lambda: 2 * _MIB,  # own RSS: already over the 1 MiB ceiling
        container_reader=_no_containers(),
    )
    sampler.start()
    try:
        await _settle(interval_s=0.001)
        assert sampler.guard() is HaltReason.HOST_MEMORY
    finally:
        await sampler.stop()


async def test_guard_returns_none_when_orchestrator_rss_is_under_its_own_ceiling() -> None:
    sampler = HostMemorySampler(
        run_id=RUN_ID,
        max_host_rss_mb=100_000,
        max_rss_mb=100,  # ceiling = 100 MiB
        interval_s=0.001,
        cgroup_reader=lambda: 1 * _MIB,
        rss_reader=lambda: 1 * _MIB,
        container_reader=_no_containers(),
    )
    sampler.start()
    try:
        await _settle(interval_s=0.001)
        assert sampler.guard() is None
    finally:
        await sampler.stop()


async def test_breach_kinds_distinguishes_which_ceiling_tripped() -> None:
    """`guard()` itself deliberately collapses both ceilings to one `HaltReason` (task 29's
    brief: the enum member and exit code stay `HOST_MEMORY`/5 for either) -- `breach_kinds()` is
    where the two are told apart, proven here across all four reachable states by mutating both
    readers' underlying values between settles, the same "reading changes, sampler reflects it"
    shape `test_sampler_keeps_ticking_across_many_intervals_unattended` above uses."""
    readings = {"tree": 1 * _MIB, "own": 1 * _MIB}
    sampler = HostMemorySampler(
        run_id=RUN_ID,
        max_host_rss_mb=100,  # ceiling = 100 MiB
        max_rss_mb=100,  # ceiling = 100 MiB
        interval_s=0.001,
        cgroup_reader=lambda: readings["tree"],
        rss_reader=lambda: readings["own"],
        container_reader=_no_containers(),
    )
    assert sampler.breach_kinds() == frozenset()  # nothing has ticked yet
    sampler.start()
    try:
        await _settle(interval_s=0.001)
        assert sampler.breach_kinds() == frozenset()

        readings["tree"] = 200 * _MIB  # host leg alone breaches
        await _settle(interval_s=0.001)
        assert sampler.breach_kinds() == frozenset({"host_rss"})

        readings["tree"] = 1 * _MIB
        readings["own"] = 200 * _MIB  # own leg alone breaches
        await _settle(interval_s=0.001)
        assert sampler.breach_kinds() == frozenset({"own_rss"})

        readings["tree"] = 200 * _MIB  # both breach
        await _settle(interval_s=0.001)
        assert sampler.breach_kinds() == frozenset({"host_rss", "own_rss"})
    finally:
        await sampler.stop()


async def test_a_low_orchestrator_rss_with_a_host_breach_still_halts() -> None:
    """The inverse of the two tests above: orchestrator RSS stays low, only the host leg
    breaches -- confirms task 29's addition did not regress task 27's own `max_host_rss_mb`
    path (task 28's e2e suite proves the same property end to end; this is the unit-level
    mirror)."""
    sampler = HostMemorySampler(
        run_id=RUN_ID,
        max_host_rss_mb=1,  # ceiling = 1 MiB
        max_rss_mb=100,  # own RSS ceiling: comfortably out of reach
        interval_s=0.001,
        cgroup_reader=lambda: 2 * _MIB,  # host leg: already over its own 1 MiB ceiling
        rss_reader=lambda: 1 * _MIB,  # own RSS: low
        container_reader=_no_containers(),
    )
    sampler.start()
    try:
        await _settle(interval_s=0.001)
        assert sampler.guard() is HaltReason.HOST_MEMORY
    finally:
        await sampler.stop()


async def test_an_own_rss_reader_failure_surfaces_loudly_when_stopped() -> None:
    """Rule 11, mirrored for the new reader: a `rss_reader` exception must not vanish, exactly
    like `test_a_reader_failure_surfaces_loudly_when_stopped_rather_than_being_swallowed` above
    proves for `cgroup_reader`."""

    def _broken_rss_reader() -> int:
        raise RuntimeError("synthetic getrusage read failure")

    sampler = HostMemorySampler(
        run_id=RUN_ID,
        max_host_rss_mb=100,
        max_rss_mb=100,
        interval_s=0.001,
        cgroup_reader=lambda: 0,
        rss_reader=_broken_rss_reader,
        container_reader=_no_containers(),
    )
    sampler.start()
    await _settle(interval_s=0.001)
    with pytest.raises(RuntimeError, match="synthetic getrusage read failure"):
        await sampler.stop()
    assert sampler.is_running is False


# --------------------------------------------------------------------------------------
# the sampler ticks independent of dispatch activity (Rule-12 test shape item 3 -- the
# property that motivated the whole redesign, per the task brief)
# --------------------------------------------------------------------------------------


async def test_sampler_updates_guard_state_with_no_simulated_dispatch_activity_at_all() -> None:
    """No `PhaseRunner`, no `_drive`, no repo dispatch of any kind is constructed or driven in
    this test -- ONLY `HostMemorySampler.start()` and a real wall-clock wait. If `guard()`
    reflects the breach anyway, the sampler's own timer -- not a poll site some other caller
    drives -- is what produced it."""
    sampler = HostMemorySampler(
        run_id=RUN_ID,
        max_host_rss_mb=1,
        max_rss_mb=100_000,  # this leg is not under test here: kept far out of reach
        interval_s=0.001,
        cgroup_reader=lambda: 5 * _MIB,
        rss_reader=lambda: 0,
        container_reader=_no_containers(),
    )
    assert sampler.guard() is None  # nothing has ticked yet
    sampler.start()
    try:
        await _settle(interval_s=0.001, ticks_at_least=3)  # let several ticks elapse, untouched
        assert sampler.guard() is HaltReason.HOST_MEMORY
    finally:
        await sampler.stop()


async def test_sampler_keeps_ticking_across_many_intervals_unattended() -> None:
    """A second wall-clock wait, well past the first, still reflects a fresh reading -- proves
    this is a genuinely repeating loop and not a one-shot tick that happened to run once."""
    reading = {"bytes": 5 * _MIB}
    sampler = HostMemorySampler(
        run_id=RUN_ID,
        max_host_rss_mb=1,
        max_rss_mb=100_000,  # this leg is not under test here: kept far out of reach
        interval_s=0.001,
        cgroup_reader=lambda: reading["bytes"],
        rss_reader=lambda: 0,
        container_reader=_no_containers(),
    )
    sampler.start()
    try:
        await _settle(interval_s=0.001, ticks_at_least=2)
        assert sampler.guard() is HaltReason.HOST_MEMORY

        reading["bytes"] = 0  # host recovers
        await _settle(interval_s=0.001, ticks_at_least=5)
        assert sampler.guard() is None
    finally:
        await sampler.stop()


# --------------------------------------------------------------------------------------
# clean task lifecycle (Rule-12 test shape item 4)
# --------------------------------------------------------------------------------------


async def test_start_creates_exactly_one_task_and_stop_leaves_none_lingering() -> None:
    sampler = HostMemorySampler(
        run_id=RUN_ID,
        max_host_rss_mb=100,
        max_rss_mb=100_000,  # this leg is not under test here: kept far out of reach
        interval_s=0.001,
        cgroup_reader=lambda: 0,
        rss_reader=lambda: 0,
        container_reader=_no_containers(),
    )
    before = asyncio.all_tasks()
    assert sampler.is_running is False

    sampler.start()
    assert sampler.is_running is True
    running_tasks = asyncio.all_tasks() - before
    assert len(running_tasks) == 1
    (sampler_task,) = running_tasks
    assert not sampler_task.done()

    await _settle(interval_s=0.001)  # let it tick at least once while alive
    await sampler.stop()

    assert sampler.is_running is False
    assert sampler_task.cancelled()
    assert asyncio.all_tasks() == before  # nothing lingering


async def test_start_twice_without_stop_is_refused_loudly() -> None:
    sampler = HostMemorySampler(
        run_id=RUN_ID,
        max_host_rss_mb=100,
        max_rss_mb=100_000,  # this leg is not under test here: kept far out of reach
        interval_s=0.001,
        cgroup_reader=lambda: 0,
        rss_reader=lambda: 0,
        container_reader=_no_containers(),
    )
    sampler.start()
    try:
        with pytest.raises(RuntimeError, match="already running"):
            sampler.start()
    finally:
        await sampler.stop()


async def test_stop_before_start_is_a_no_op() -> None:
    sampler = HostMemorySampler(
        run_id=RUN_ID,
        max_host_rss_mb=100,
        max_rss_mb=100_000,  # this leg is not under test here: kept far out of reach
        interval_s=0.001,
        cgroup_reader=lambda: 0,
        rss_reader=lambda: 0,
        container_reader=_no_containers(),
    )
    await sampler.stop()  # must not raise
    assert sampler.is_running is False


async def test_a_sample_sitting_exactly_at_either_ceiling_is_a_breach() -> None:
    """`_tick()`'s own docstring: "`>=`, not `>`, for both: 'keeps RSS under the ceiling' (§12.22)
    reads a sample sitting exactly AT the ceiling as not under it." Every other test in this file
    uses readings comfortably above or below a ceiling, never exactly equal to one -- so a
    mutation flipping either `>=` to `>` in `_tick()` (`total_bytes >= self._host_ceiling_bytes`
    or `own_bytes >= self._own_ceiling_bytes`) would pass every existing test unnoticed. Checked
    for both ceilings independently, mirroring how the rest of this file keeps the untested leg
    "far out of reach" rather than at zero (zero would not discriminate `>=` from `>` either)."""
    host_ceiling_mb = 10
    sampler = HostMemorySampler(
        run_id=RUN_ID,
        max_host_rss_mb=host_ceiling_mb,
        max_rss_mb=100_000,  # this leg is not under test here: kept far out of reach
        interval_s=0.001,
        cgroup_reader=lambda: host_ceiling_mb * _MIB,  # exactly at the ceiling, not over it
        rss_reader=lambda: 0,
        container_reader=_no_containers(),
    )
    sampler.start()
    try:
        await _settle(interval_s=0.001)
        assert sampler.guard() is HaltReason.HOST_MEMORY
        assert sampler.breach_kinds() == frozenset({"host_rss"})
    finally:
        await sampler.stop()

    own_ceiling_mb = 10
    sampler = HostMemorySampler(
        run_id=RUN_ID,
        max_host_rss_mb=100_000,  # this leg is not under test here: kept far out of reach
        max_rss_mb=own_ceiling_mb,
        interval_s=0.001,
        cgroup_reader=lambda: 0,
        rss_reader=lambda: own_ceiling_mb * _MIB,  # exactly at the ceiling, not over it
        container_reader=_no_containers(),
    )
    sampler.start()
    try:
        await _settle(interval_s=0.001)
        assert sampler.guard() is HaltReason.HOST_MEMORY
        assert sampler.breach_kinds() == frozenset({"own_rss"})
    finally:
        await sampler.stop()


async def test_a_reader_failure_surfaces_loudly_when_stopped_rather_than_being_swallowed() -> None:
    """Rule 11: an unreadable host is not the same fact as "the host has room." A `_tick()`
    exception must not vanish -- it is stored on the background task and re-raised when `stop()`
    awaits it."""

    def _broken_cgroup_reader() -> int:
        raise RuntimeError("synthetic cgroup read failure")

    sampler = HostMemorySampler(
        run_id=RUN_ID,
        max_host_rss_mb=100,
        max_rss_mb=100_000,  # this leg is not under test here: kept far out of reach
        interval_s=0.001,
        cgroup_reader=_broken_cgroup_reader,
        rss_reader=lambda: 0,
        container_reader=_no_containers(),
    )
    sampler.start()
    await _settle(interval_s=0.001)
    with pytest.raises(RuntimeError, match="synthetic cgroup read failure"):
        await sampler.stop()
    # a caller that stopped once must be able to tell the sampler is no longer running, even
    # though stop() raised -- otherwise a caller's own cleanup could double-stop and get confused
    assert sampler.is_running is False
