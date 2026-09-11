"""`fleet-<run_id>-*` container memory reader (SPEC §12.22 runtime RSS-sampling sub-clause, task
B1; naming from SPEC §3.3/§11.1, `sandbox/worktree.py::run_prefix`/`sandbox_name`).

**Scope: a pure reader, not a sampler.** Enumerates currently-RUNNING `fleet-<run_id>-*`
containers and reads each one's current memory usage. It does nothing periodic, counts no
breaches, and is not wired into `PhaseRunner`/`resource_guard` -- that (a genuine new periodic
background task plus a breach-counting halt policy) is task B2's scope, dispatched separately.

**Hard constraint honoured throughout this module's own development: no real `docker` command was
ever invoked to build or test it.** Every subprocess call goes through the injected
`fleet.util.proc.CommandRunner` seam -- the same Protocol `sandbox/container.py::ContainerSandbox`
already depends on instead of importing a Docker SDK (CLAUDE.md guardrail 3: dependency inversion
at every external boundary; no `docker.from_env`/`docker` Python package exists in this project's
dependencies). `ContainerStatsReader.__init__`'s `runner: CommandRunner = run` mirrors
`ContainerSandbox.__init__`'s identical parameter exactly, so production needs no extra wiring
and a test injects a fake/recording implementation and never touches a real `docker` binary. The
`docker stats` MEM USAGE column format this module parses (`"796KiB / 15.57GiB"`-shaped) is built
from Docker's own documented output, never observed by running `docker` live.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID

from fleet.sandbox.worktree import run_prefix
from fleet.util.proc import CommandRunner, ProcResult, no_verdict, run

#: `docker stats`' `MEM USAGE / LIMIT` column reports the USED half in Docker's own binary (IEC)
#: unit table (go-units' `BytesSize`): `B`, `KiB`, `MiB`, `GiB`, `TiB`, `PiB`, `EiB`. A documented
#: example (Docker CLI reference, `docker stats --help`): `796KiB / 15.57GiB`.
_MEM_USAGE_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([A-Za-z]+)\s*/")

_BINARY_UNITS: dict[str, int] = {
    "b": 1,
    "kib": 1024,
    "mib": 1024**2,
    "gib": 1024**3,
    "tib": 1024**4,
    "pib": 1024**5,
    "eib": 1024**6,
}


class ContainerStatsUnavailableError(RuntimeError):
    """`docker ps`/`docker stats` did not settle, failed at the CLI level, or returned output
    this module cannot parse.

    Raised rather than silently returning a wrong or partial total (CLAUDE.md Rule 11). This is
    deliberately NOT what a run with zero matching containers raises -- that is a normal, valid
    state (no verify containers active right now) and `ContainerStatsReader.read_total` returns
    an empty, zero total cleanly instead; see that method's docstring for the same "there are
    none" vs. "I could not find out" distinction `ContainerSandbox.list_with_verdict` states.
    """


@dataclass(frozen=True, slots=True)
class ContainerMemoryReading:
    """One container's current memory usage, as parsed from one `docker stats` row."""

    name: str
    memory_bytes: int


@dataclass(frozen=True, slots=True)
class ContainerMemoryTotal:
    """Every matching container's reading, plus the combined total `ContainerStatsReader.
    read_total` computes them into (see that method's docstring for why SUM is the right
    combination for SPEC §12.22's ceiling comparison)."""

    readings: tuple[ContainerMemoryReading, ...]
    total_bytes: int


def parse_mem_usage_bytes(field: str) -> int:
    """Parse the USED half of a `docker stats` `MEM USAGE / LIMIT` field (e.g. `"796KiB /
    15.57GiB"` -> `796 * 1024`) into a plain byte count.

    Fails loudly (`ContainerStatsUnavailableError`) on anything not shaped like `<number><unit> /
    ...` or naming a unit outside Docker's documented binary (IEC) table -- never silently
    returns 0 for malformed input (CLAUDE.md Rule 11).
    """
    match = _MEM_USAGE_RE.match(field)
    if match is None:
        raise ContainerStatsUnavailableError(
            f"could not parse a 'USED / LIMIT' MEM USAGE field from {field!r}"
        )
    value_str, unit = match.groups()
    multiplier = _BINARY_UNITS.get(unit.lower())
    if multiplier is None:
        raise ContainerStatsUnavailableError(
            f"unrecognized memory unit {unit!r} in MEM USAGE field {field!r} "
            f"(known units: {sorted(_BINARY_UNITS)})"
        )
    return int(float(value_str) * multiplier)


class ContainerStatsReader:
    """Reads real, current memory usage for every RUNNING `fleet-<run_id>-*` container, over the
    injected `CommandRunner` seam -- see module docstring for why this mirrors `ContainerSandbox`
    exactly rather than a `cli.py`-style module-global seam: this is a standalone primitive, not
    yet wired into any singleton runtime state, so constructor injection (this class's own
    `runner` parameter, defaulted to the real `fleet.util.proc.run`) is the narrower seam --
    `CommandRunner` is the identical Protocol `BAZEL_RUNNER`/`FILTER_REPO_RUNNER`/etc. share, so a
    later wiring pass (task B2) can lift this into a module-global if the run-time architecture
    it lands in calls for one.
    """

    def __init__(self, *, runner: CommandRunner = run, docker_bin: str = "docker") -> None:
        self._runner = runner
        self._docker = docker_bin

    async def _list_running(self, prefix: str, *, timeout_s: float) -> list[str]:
        """`docker ps` (no `--all`: only RUNNING containers have anything for `docker stats` to
        report) filtered to `prefix`, mirroring `ContainerSandbox.list_with_verdict`'s
        `re.escape`d anchored-prefix regex filter for the identical reason stated there: `prefix`
        can contain a literal `.` (a repo id like `my.repo.js`), and an unescaped filter would
        over-match a sibling container."""
        result = await self._runner(
            [
                self._docker,
                "ps",
                "--filter",
                f"name=^{re.escape(prefix)}",
                "--format",
                "{{.Names}}",
            ],
            timeout_s=timeout_s,
        )
        unsettled = no_verdict(result)
        if unsettled is not None:
            raise ContainerStatsUnavailableError(
                f"docker ps for {prefix}* did not settle: {unsettled}"
            )
        if not result.ok:
            raise ContainerStatsUnavailableError(
                f"docker ps for {prefix}* failed (exit {result.exit_code}): {result.stderr_tail}"
            )
        return [line.strip() for line in result.stdout_tail.splitlines() if line.strip()]

    async def _stats(self, names: list[str], *, timeout_s: float) -> ProcResult:
        argv = [
            self._docker,
            "stats",
            "--no-stream",
            "--format",
            "{{.Name}}\t{{.MemUsage}}",
            *names,
        ]
        result = await self._runner(argv, timeout_s=timeout_s)
        unsettled = no_verdict(result)
        if unsettled is not None:
            raise ContainerStatsUnavailableError(f"docker stats did not settle: {unsettled}")
        if not result.ok:
            raise ContainerStatsUnavailableError(
                f"docker stats failed (exit {result.exit_code}): {result.stderr_tail}"
            )
        return result

    async def read_total(
        self, run_id: UUID | str, *, timeout_s: float = 30.0
    ) -> ContainerMemoryTotal:
        """Sum every currently-running `fleet-<run_id>-*` container's MEM USAGE into one total
        bytes figure.

        **SUM, not max or any other combination** -- SPEC §12.22's literal text wants "the whole
        process tree PLUS every `fleet-<run_id>-*` container" compared against ONE ceiling
        (`budgets.max_host_rss_mb`). Each running container's live memory adds directly to host
        memory pressure regardless of what any other container is doing, so summing is the only
        combination consistent with that ceiling -- a `max()` would silently let N containers each
        just under the per-container cap add up to N times the host's actual free memory while
        this reader kept reporting only the single largest one.

        Zero matching (running) containers is a normal, valid state -- a run with no active verify
        containers right now -- and returns an EMPTY, ZERO total with NO error; `docker stats` is
        not even invoked in that case. This is deliberately distinct from
        `ContainerStatsUnavailableError`, whose whole point is to separate "there are none" from
        "I could not find out" (mirrors `ContainerSandbox.list_with_verdict`'s identical
        distinction for the same reason: a caller comparing this total against a ceiling must
        never read a failed lookup as "0 bytes used, therefore under budget").
        """
        prefix = run_prefix(run_id)
        names = await self._list_running(prefix, timeout_s=timeout_s)
        if not names:
            return ContainerMemoryTotal(readings=(), total_bytes=0)

        # A container in `names` can legitimately exit and be removed (`--rm`) between the
        # `docker ps` above and the `docker stats` call below -- an ordinary verify container
        # finishing mid-build, not a sign the host is unreadable. `docker stats` fails its whole
        # batch on any one missing name, so on a first failure re-list (dropping any container
        # that has since exited) and retry once against the survivors; only a SECOND failure --
        # against a freshly re-listed set -- is treated as a genuinely unreadable host.
        try:
            result = await self._stats(names, timeout_s=timeout_s)
        except ContainerStatsUnavailableError:
            names = await self._list_running(prefix, timeout_s=timeout_s)
            if not names:
                return ContainerMemoryTotal(readings=(), total_bytes=0)
            result = await self._stats(names, timeout_s=timeout_s)
        readings: list[ContainerMemoryReading] = []
        for raw_line in result.stdout_tail.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) != 2:
                raise ContainerStatsUnavailableError(
                    f"malformed docker stats output line (expected NAME<TAB>MEM_USAGE): {line!r}"
                )
            name, mem_field = parts
            readings.append(
                ContainerMemoryReading(name=name, memory_bytes=parse_mem_usage_bytes(mem_field))
            )
        total = sum(reading.memory_bytes for reading in readings)
        return ContainerMemoryTotal(readings=tuple(readings), total_bytes=total)
