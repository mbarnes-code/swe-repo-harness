"""§12.22's runtime RSS-sampling sub-clause, piece B3 (round VI, task 28): the adversarial
`getrusage`-defeating end-to-end proof, through the REAL `fleet` CLI. Extended by task 29 (round
VI) with the mirror-image proof for the OTHER ceiling: `budgets.max_rss_mb`, the orchestrator's
own RSS, which is genuinely read VIA `resource.getrusage` (see `memory_guard.py`'s module
docstring for why that primitive is correct for this ceiling specifically, and wrong for the one
this file's original tests below defend).

Tasks 26 (`util/cgroup.py`, `sandbox/containerstats.py`, merged) and 27
(`orchestrator/memory_guard.py`'s `HostMemorySampler`, wired into `cli.py`'s four
`_run_*_wave` composition roots, merged) built and unit-proved the sampler in isolation. Nothing
until this file drove it through a real `fleet scan` invocation and proved the ONE property
`docs/SPEC.md` §12 item 22 states as its own explicit anti-pattern callout, verbatim:

    "...sampled every second from cgroup `memory.current` + container stats, not from
    `resource.getrusage` -- a variant test that inflates only the pool children and the
    containers must fail this criterion, which is exactly what a `getrusage`-only
    implementation would pass"

Every test below constructs the adversarial scenario entirely through task 26/27/29's own injected
seams (`cli.CONTAINER_STATS_RUNNER`/`cli.CGROUP_MEMORY_READER`/`cli.RSS_READER`) -- per this
task's hard environmental constraint, no real `docker` command is ever invoked and no real
memory-hungry process is ever spawned. `tests/conftest.py`'s autouse `_no_real_docker_stats`
fixture already patches all three seams to safe zero-reading fakes for every test that loads
`fleet.cli`; each test below explicitly re-patches whichever it needs (its own documented escape
hatch) to install the specific readings its scenario needs.

**Fixture scale.** SPEC's literal text calls for "indexing a synthetic 50 000-file repo." This
file's fleet is ONE trivial one-file git repository -- see `docs/DECISIONS.md` ADR-0115 for the
explicit adjudication: the property under test (the sampler correctly reads INJECTED cgroup/
container numbers and halts or does not, regardless of what real work is or is not happening) does
not depend on file count at all, and in fact the primary/negative-control halt scenarios never
reach real per-repo work at all -- `PhaseRunner._drive`'s `resource_guard()` poll fires at the very
top of its loop, before lease acquisition, before any clone. A 50 000-file repo would exercise
identical sampler-comparison logic, N files later.

**`FailureClass.MEMORY_EXHAUSTED`, a routed premise that did not survive contact with the code.**
No such enum member exists anywhere in this codebase (`grep -rn MEMORY_EXHAUSTED src/` finds only
`ExitCode.MEMORY_EXHAUSTED`, an `IntEnum` alias for exit code 5 -- `models/enums.py`'s
`FailureClass` has no member by that name). This halt path also never touches ANY `FailureClass`:
`resource_guard()` is polled and raises `RunHalted` at the top of `_drive`'s loop, strictly BEFORE
a lease is acquired or a worker dispatched -- the one path in this codebase where a halt is
discovered with no repo-scoped attempt in flight to attribute a failure class to at all (contrast
`HaltReason.DISK`, discovered THROUGH a repo's `FailureClass.DISK_EXHAUSTED` and only then
re-raised as a run-level `RunHalted`, per `runner.py`'s `_on_breach`). What this halt genuinely
reports is `HaltReason.HOST_MEMORY` (in the `RunHalted` message the CLI prints) and
`ExitCode.MEMORY_EXHAUSTED` (the process exit code) -- both asserted below in place of the
non-existent `FailureClass` member.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from typer.testing import CliRunner

from fleet.cli import ExitCode, app
from fleet.orchestrator.memory_guard import HostMemorySampler
from fleet.orchestrator.runner import HOST_MEMORY_EXIT_CODE
from fleet.sandbox.containerstats import ContainerMemoryTotal
from fleet.settings import FleetSettings
from fleet.util.proc import ProcResult
from tests.test_scan_e2e import FLEET_YAML, _fresh_db, _make_repo, _write_config, query

runner = CliRunner()


def scan(root: Path, *extra: str) -> object:
    return runner.invoke(
        app,
        [
            "--config",
            str(root / "config" / "fleet.yaml"),
            "--db",
            str(root / "state/fleet.db"),
            "scan",
            "--skip-classify",
            *extra,
        ],
        catch_exceptions=False,
    )


@pytest.fixture
def tiny_fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The smallest possible real fleet: ONE trivial git repo, no manifest relations, no
    cross-repo edges. See ADR-0115 (`docs/DECISIONS.md`) for why this -- not SPEC §12.22's
    literal 50 000-file synthetic repo -- is what every test in this file drives."""
    sources = {"acme-tiny": _make_repo(tmp_path / "sources", "acme-tiny", {"README.md": "hi\n"})}
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_config(workspace, sources, names=["acme-tiny"], fleet_yaml=FLEET_YAML)
    _fresh_db(workspace / "state" / "fleet.db")
    monkeypatch.chdir(workspace)
    return workspace


# --------------------------------------------------------------------------------------
# the injected seams: a fake `docker` CLI and a fake cgroup reader
# --------------------------------------------------------------------------------------


class _AdversarialDockerRunner:
    """A fake `docker` CLI standing in for `cli.CONTAINER_STATS_RUNNER`. `docker ps` reports
    exactly one RUNNING `fleet-<run_id>-*` container -- its name derived from the REAL
    `--filter name=^<prefix>` argv `ContainerStatsReader._list_running` builds (never
    hardcoded), so this fake correctly answers whatever run_id `fleet scan` actually assigns at
    invocation time. `docker stats` reports that one container using far more memory than
    `budgets.max_host_rss_mb`'s default ceiling (12 288 MB). This is "inflating only the
    containers" -- the orchestrator's own process-tree reading is controlled separately by
    `_low_cgroup_reader` below and never touched here.

    `prefix` is read back verbatim from the `ps` filter argv, not re-derived from a run_id this
    fake never sees -- `re.escape` is the identity function over a `run_prefix()` output's
    alphabet (hex digits and `-`, both unescaped by `re.escape` since Python 3.7), so this holds
    for every real run_id `fleet scan` can generate.
    """

    def __init__(self, *, mem_field: str = "20GiB / 64GiB") -> None:
        self.calls: list[tuple[str, ...]] = []
        self._mem_field = mem_field

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
            # argv: [docker, ps, --filter, name=^<escaped-prefix>, --format, {{.Names}}]
            filter_value = parts[3]
            assert filter_value.startswith("name=^"), filter_value
            prefix = filter_value.removeprefix("name=^")
            name = f"{prefix}adversarial-0"
            return ProcResult(
                argv=parts,
                exit_code=0,
                stdout_tail=name + "\n",
                stderr_tail="",
                duration_ms=1,
                timed_out=False,
                started=True,
            )
        assert parts[1] == "stats", parts
        # argv: [docker, stats, --no-stream, --format, {{.Name}}\t{{.MemUsage}}, <names...>]
        names = parts[5:]
        stdout = "\n".join(f"{name}\t{self._mem_field}" for name in names)
        return ProcResult(
            argv=parts,
            exit_code=0,
            stdout_tail=stdout,
            stderr_tail="",
            duration_ms=1,
            timed_out=False,
            started=True,
        )


class _NoContainersRunner:
    """A fake `docker` CLI reporting zero running `fleet-<run_id>-*` containers -- `docker
    stats` is never even invoked (mirrors `ContainerStatsReader.read_total`'s own documented
    "there are none" shortcut). The healthy half of the control case below."""

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
        assert parts[1] == "ps", (
            "a reader reporting zero containers must never reach a `docker stats` call"
        )
        return ProcResult(
            argv=parts,
            exit_code=0,
            stdout_tail="",
            stderr_tail="",
            duration_ms=1,
            timed_out=False,
            started=True,
        )


def _low_cgroup_reader() -> int:
    """Stands in for `cli.CGROUP_MEMORY_READER`: the orchestrator's own whole-process-tree
    `memory.current` reading, held at 100 MB -- comfortably under every ceiling this file's
    fixtures configure. The "orchestrator's own `resource.getrusage()`-visible usage stays low
    throughout" half of the adversarial scenario."""
    return 100 * 1024 * 1024


def _getrusage_only_tree_bytes() -> int:
    """The exact defeated implementation SPEC §12.22 names by name: `resource.getrusage(
    RUSAGE_SELF).ru_maxrss` (Linux reports KiB). It sees only the CALLING PROCESS's own peak
    RSS -- never a child's, never a container's -- so a breach confined to a
    `fleet-<run_id>-*` container is invisible to it no matter how large. Used only by
    `_getrusage_only_sampler` below, standing in for `HostMemorySampler`'s `cgroup_reader`."""
    import resource

    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024


class _ZeroContainerReader:
    """A `ContainerStatsReader`-shaped stand-in that never reaches a container at all -- the
    other half of "a `resource.getrusage()`-only implementation": it does not merely read zero
    FROM a real container, it never asks. However high `_AdversarialDockerRunner`'s injected
    reading is, this reader reports nothing, by construction."""

    async def read_total(self, run_id: object, *, timeout_s: float = 30.0) -> ContainerMemoryTotal:
        return ContainerMemoryTotal(readings=(), total_bytes=0)


def _getrusage_only_sampler(settings: FleetSettings, run_id: str) -> HostMemorySampler:
    """Replaces `cli._host_memory_sampler` for the negative-control test: the SAME
    `HostMemorySampler`/`resource_guard()` wiring task 27 built, but constructed with a
    `getrusage`-only `cgroup_reader` and a container reader that never looks at a container --
    SPEC §12.22's own named anti-pattern, made real rather than merely asserted about."""
    return HostMemorySampler(
        run_id=run_id,
        max_host_rss_mb=settings.config.budgets.max_host_rss_mb,
        max_rss_mb=settings.config.budgets.max_rss_mb,
        cgroup_reader=_getrusage_only_tree_bytes,
        rss_reader=lambda: 0,  # this file's negative control is about the HOST leg only
        container_reader=_ZeroContainerReader(),  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------------------
# the proof
# --------------------------------------------------------------------------------------


def test_a_breach_confined_to_pool_children_and_containers_halts_the_run(
    tiny_fleet: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SPEC §12.22's own anti-pattern callout, made real: the orchestrator's OWN process-tree
    cgroup reading stays LOW throughout (100 MB, `_low_cgroup_reader`) while a single
    `fleet-<run_id>-*` container's memory alone breaches `budgets.max_host_rss_mb` (12 288 MB
    default, `_AdversarialDockerRunner`'s 20 GiB reading). The real cgroup+container-stats
    sampler this test drives through the ACTUAL `fleet` CLI (not a direct call into
    `memory_guard.py`) must halt the run rather than let it proceed -- exactly what a
    `resource.getrusage()`-only implementation would fail to do (proven directly by
    `test_a_getrusage_only_sampler_does_not_catch_the_same_breach` below).
    """
    from fleet import cli

    monkeypatch.setattr(cli, "CGROUP_MEMORY_READER", _low_cgroup_reader)
    monkeypatch.setattr(cli, "CONTAINER_STATS_RUNNER", _AdversarialDockerRunner())

    result = scan(tiny_fleet)

    assert ExitCode.MEMORY_EXHAUSTED == HOST_MEMORY_EXIT_CODE == 5
    assert result.exit_code == ExitCode.MEMORY_EXHAUSTED, result.output
    assert "HOST_MEMORY" in result.output, (
        "the reported failure must be attributable to `HaltReason.HOST_MEMORY` -- see this "
        "file's module docstring for why this, not `FailureClass.MEMORY_EXHAUSTED` (no such "
        "enum member exists in this codebase), is what 'the failure reporting' names here"
    )

    # The guard trips at the very TOP of `_drive`'s per-repo loop, strictly before lease
    # acquisition -- so this is a genuine PRE-DISPATCH refusal, not a repo that failed mid-work
    # and got misclassified. `upsert_phase` (`_open_run`) pre-creates the PENDING row before the
    # wave starts, so the row exists; what proves no dispatch happened is that it never moved.
    assert query(
        tiny_fleet, "SELECT status, attempts, lease_owner FROM phases WHERE phase = 1"
    ) == [("PENDING", 0, None)]


def test_a_getrusage_only_sampler_does_not_catch_the_same_breach(
    tiny_fleet: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE negative control that actually proves the `getrusage`-defeating property (SPEC
    §12.22, verbatim). Under the EXACT SAME adversarial container breach as the test above (the
    same `_AdversarialDockerRunner`, the same `_low_cgroup_reader`), a sampler built the way a
    `resource.getrusage()`-only implementation would be -- `_getrusage_only_sampler`, reading
    only the calling process's own peak RSS and never a container -- does NOT see the breach,
    and the run completes normally.

    This is the regression guard, and it is a PAIR with the test above, not a standalone
    assertion: if `memory_guard.py`'s real sampler were ever swapped to read only
    `resource.getrusage()` (for convenience, or because a future author judged the cgroup/
    container readers redundant), the primary adversarial test above would flip from GREEN to
    RED -- the run would no longer halt under the real implementation. THIS test's own
    assertion stays green regardless of that regression, because it always exercises a
    deliberately-constructed getrusage-only stand-in (`_getrusage_only_sampler`), never
    whatever the real sampler currently does. That asymmetry is deliberate, not a gap: this
    test's job is to prove the getrusage-only SHAPE genuinely fails to catch the breach (so the
    property the test above defends against is real and not a strawman); the test above's job
    is to prove the actual shipped code does not degrade to that shape. Together they pin the
    discriminating property from both directions; either alone would not.
    """
    from fleet import cli

    monkeypatch.setattr(cli, "CGROUP_MEMORY_READER", _low_cgroup_reader)
    monkeypatch.setattr(cli, "CONTAINER_STATS_RUNNER", _AdversarialDockerRunner())
    monkeypatch.setattr(cli, "_host_memory_sampler", _getrusage_only_sampler)

    result = scan(tiny_fleet)

    assert result.exit_code == ExitCode.SUCCESS, (
        f"a getrusage-only sampler must NOT catch a breach confined to pool children/"
        f"containers -- got exit {result.exit_code}, output: {result.output}"
    )


def test_low_readings_on_both_readers_complete_normally(
    tiny_fleet: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control proving the halt in the primary adversarial test above is not "the run
    always halts regardless of the readings": the same fixture, the same seams, the same real
    `HostMemorySampler`/`resource_guard()` wiring -- with BOTH the process-tree cgroup reading
    and the container total kept low -- completes normally (exit 0).

    `cli.RSS_READER` is not overridden here -- `tests/conftest.py`'s autouse fixture already
    patches it to a zero-reading fake, so this test also incidentally proves the `max_rss_mb`
    leg (task 29) stays quiet when low, alongside `max_host_rss_mb`. The dedicated case for that
    leg is `test_an_orchestrator_rss_breach_halts_the_run_with_exit_5` below."""
    from fleet import cli

    monkeypatch.setattr(cli, "CGROUP_MEMORY_READER", _low_cgroup_reader)
    monkeypatch.setattr(cli, "CONTAINER_STATS_RUNNER", _NoContainersRunner())

    result = scan(tiny_fleet)
    assert result.exit_code == ExitCode.SUCCESS, result.output


# --------------------------------------------------------------------------------------
# task 29 (round VI): the OTHER ceiling -- `budgets.max_rss_mb`, the orchestrator's own RSS,
# genuinely enforced via `resource.getrusage` (the primitive §12.22 bans for the ceiling above,
# and names as correct for this one -- see `memory_guard.py`'s module docstring)
# --------------------------------------------------------------------------------------


def _high_own_rss_bytes() -> int:
    """Stands in for `cli.RSS_READER`: an orchestrator-own-RSS reading comfortably over
    `budgets.max_rss_mb`'s 4096 MB default (5000 MB here), with the process-tree cgroup and
    container readings held low by the same fixtures the tests above use -- isolating this
    ceiling's breach from the other one's."""
    return 5000 * 1024 * 1024


def test_an_orchestrator_rss_breach_halts_the_run_with_exit_5(
    tiny_fleet: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The end-to-end proof for `budgets.max_rss_mb` that task 28's own e2e tests never covered
    (they only drove `max_host_rss_mb`, see this file's original module docstring). The
    process-tree cgroup reading and the container total both stay low (`_low_cgroup_reader`,
    `_NoContainersRunner` -- the same healthy readings the control above uses); only
    `cli.RSS_READER` breaches. The real sampler this test drives through the ACTUAL `fleet` CLI
    must halt the run exactly as it does for the other ceiling -- same `HaltReason.HOST_MEMORY`,
    same exit 5, per task 29's brief (one halt reason/exit code for either ceiling)."""
    from fleet import cli

    monkeypatch.setattr(cli, "CGROUP_MEMORY_READER", _low_cgroup_reader)
    monkeypatch.setattr(cli, "CONTAINER_STATS_RUNNER", _NoContainersRunner())
    monkeypatch.setattr(cli, "RSS_READER", _high_own_rss_bytes)

    result = scan(tiny_fleet)

    assert ExitCode.MEMORY_EXHAUSTED == HOST_MEMORY_EXIT_CODE == 5
    assert result.exit_code == ExitCode.MEMORY_EXHAUSTED, result.output
    assert "HOST_MEMORY" in result.output

    # Same pre-dispatch-refusal proof as the host-ceiling test above: the guard trips at the
    # top of `_drive`'s loop, strictly before lease acquisition, so the PENDING row never moves.
    assert query(
        tiny_fleet, "SELECT status, attempts, lease_owner FROM phases WHERE phase = 1"
    ) == [("PENDING", 0, None)]
