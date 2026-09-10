"""Shared fixtures (SPEC §8 `tests/conftest.py`).

Kept dependency-light on purpose: the state and worker layers must be testable with nothing
but `pydantic` and `pytest` installed, so nothing here imports aiosqlite, structlog, typer or
anthropic. The real `tmp fleet.db` / `RunContext` fixtures land with `state/db.py`.

**External-tool discovery happens at import time, not in a fixture.** `@pytest.mark.skipif(
shutil.which(...) is None, ...)` is evaluated while a test *module* is being imported, which is
strictly after conftest import and strictly before any fixture runs. A PATH repaired in a
fixture would therefore repair nothing: the skip decision is already frozen. See
`docs/INTEGRATION_HONESTY.md` for what each of those tools is and is not proving.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import stat as stat_module
import sys
import tempfile
import time
import urllib.request
from collections.abc import Iterator
from contextlib import suppress
from datetime import UTC, datetime
from functools import cache
from hashlib import sha256
from pathlib import Path
from time import monotonic
from typing import Any, Final
from uuid import UUID

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# src layout: importable without an editable install, so `pytest tests/` works from a bare venv.
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# --------------------------------------------------------------------------------------
# vendored tool discovery
# --------------------------------------------------------------------------------------
# `tools/bin` holds bazelisk (as `bazel`) and `gh`; `.venv/bin` holds `git-filter-repo`, which
# also makes the `git filter-repo` subcommand form resolve. Both are derived from the repo root
# so a clone at any path — or a different $HOME — wires up identically.
TOOLS_BIN = REPO_ROOT / "tools" / "bin"
VENV_BIN = REPO_ROOT / ".venv" / "bin"
# Bazelisk caches downloaded Bazel releases here. Default is `~/.cache/bazelisk`; pointing it
# inside the workspace keeps the suite from writing outside the project directory (CLAUDE.md §2).
BAZELISK_HOME = REPO_ROOT / "tools" / "bazelisk"


def _prepend_path(*entries: Path) -> None:
    current = os.environ.get("PATH", "")
    parts = current.split(os.pathsep) if current else []
    for entry in entries:
        if entry.is_dir() and str(entry) not in parts:
            parts.insert(0, str(entry))
    os.environ["PATH"] = os.pathsep.join(parts)


_prepend_path(TOOLS_BIN, VENV_BIN)
os.environ.setdefault("BAZELISK_HOME", str(BAZELISK_HOME))


from fleet.bazel.query import registry_args  # noqa: E402
from fleet.llm.client import CallBudget  # noqa: E402
from fleet.models.enums import Phase, RepoStatus, StubState  # noqa: E402
from fleet.models.state import MigrationState, PhaseRecord, RepoState  # noqa: E402
from fleet.settings import (  # noqa: E402
    BCR_DEFAULT_REGISTRY,
    BCR_MIRROR_REGISTRY,
    BuildSection,
)
from fleet.workers.base import WorkerContext  # noqa: E402

FROZEN_NOW = datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)
RUN_ID = UUID("00000000-0000-4000-8000-000000000001")


@pytest.fixture(autouse=True)
def _no_real_docker_stats(monkeypatch: pytest.MonkeyPatch) -> None:
    """Task 27 (round VI B2): `HostMemorySampler` (`orchestrator/memory_guard.py`) ticks
    UNCONDITIONALLY as soon as any of `cli.py`'s four `_run_*_wave` composition roots start a
    wave -- independent of whether that wave ever dispatches a repo, which is the whole point of
    the redesign (SPEC §12.22). Three of its seams default to `None` in production ("really read
    the host"), matching `BAZEL_RUNNER`/`FILTER_REPO_RUNNER`/`RESOLVER_RUNNER`/`GAZELLE_RUNNER`'s
    existing "real unless a test opts in to a fake" contract -- but unlike those four, which only
    fire when a worker actually dispatches real work a test would already have to set up, all
    three of these reach the real host from ANY test that merely constructs a wave, admitted
    repos or not:

    * `cli.CONTAINER_STATS_RUNNER` -- a real `docker ps` invocation.
    * `cli.CGROUP_MEMORY_READER` -- a real read of THIS session's cgroup `memory.current`, which
      on a real sandbox is shared by every process in the login session, not scoped to the test;
      measured live at ~18.4 GiB, already over `budgets.max_host_rss_mb`'s 12 288 MB default. Left
      real, `tests/test_cli.py::test_run_cost_exhausted_exits_3` and three siblings spuriously
      halted with `HaltReason.HOST_MEMORY` the first time this was wired without an override --
      not because anything about those tests' own memory use, but because the SESSION they ran
      inside of does.
    * `cli.RSS_READER` (task 29 / round VI): a real `resource.getrusage(RUSAGE_SELF).ru_maxrss`
      read of THIS pytest process. Scoped to one process rather than a whole login session, but
      `ru_maxrss` is a PEAK over that process's entire lifetime, not per-test -- across a long,
      import-heavy `pytest tests/` session it is not a number this fixture can assume stays under
      `budgets.max_rss_mb`'s 4096 MB default, and the whole point of patching the two seams above
      was refusing to make that same kind of assumption about the host. Patched to the same
      harmless-reading fake for the same reason, not because it was measured to breach.

    All three patched here to fast, harmless-reading fakes by default, for every test that has
    `fleet.cli` loaded, so the many e2e-flavored test files that build a real wave do not silently
    gain a new host-`docker`-daemon dependency or a spurious host-memory halt they never had
    before this task. A test proving something ABOUT any of the three seams can still
    `monkeypatch.setattr(cli, "CONTAINER_STATS_RUNNER"/"CGROUP_MEMORY_READER"/"RSS_READER", ...)`
    to opt back out.

    `sys.modules`-gated rather than a module-level `import fleet.cli`, per this file's own module
    docstring: `cli.py` pulls in `aiosqlite`/`structlog`/`typer`/`anthropic`, and the
    state/worker-layer tests this file is kept dependency-light for never import it -- so this
    fixture costs them nothing beyond the `sys.modules` membership check itself.
    """
    if "fleet.cli" not in sys.modules:
        return
    cli = sys.modules["fleet.cli"]

    from fleet.util.proc import ProcResult

    async def _zero_containers(
        argv: object,
        *,
        cwd: object = None,
        env: object = None,
        deadline: object = None,
        timeout_s: object = None,
    ) -> ProcResult:
        return ProcResult(
            argv=tuple(argv),  # type: ignore[arg-type]
            exit_code=0,
            stdout_tail="",
            stderr_tail="",
            duration_ms=0,
            timed_out=False,
            started=True,
        )

    def _zero_process_tree_bytes() -> int:
        return 0

    def _zero_own_rss_bytes() -> int:
        return 0

    monkeypatch.setattr(cli, "CONTAINER_STATS_RUNNER", _zero_containers)
    monkeypatch.setattr(cli, "CGROUP_MEMORY_READER", _zero_process_tree_bytes)
    monkeypatch.setattr(cli, "RSS_READER", _zero_own_rss_bytes)


BASELINE_BUILD_DISABLED_YAML = "  baseline_build:\n    enabled: false\n"
"""§12.11/D116 Leg C (round VI task 111, fix round). A `preflight:` YAML fragment
(2-space-indented, ready to splice under a `preflight:` block that already declares at least one
sibling key such as `min_free_bytes`) that disables the native-baseline worker.

**Why this lives in `conftest.py`, session-wide, rather than in whichever e2e file first needed
it.** §12.11 Leg C wires a genuine native build/test failure to `RepoStatus.SKIPPED` for the
first time (`src/fleet/cli.py::_gate_baseline_red`) — and several e2e fixture fleets across this
suite declare real `package.json`/`pyproject.toml` manifests that were only ever vetted to parse
correctly, never to succeed under a REAL `npm install`/`pip install` + test run (most declare no
`"test"` script at all, or a deliberately-unpublished cross-repo dependency name so this suite's
own edge-inference/hoist/collision tests have something to detect). Once Leg C is wired, EVERY
such fixture that leaves `preflight.baseline_build` at its shipped default (`enabled: true`) is
consequentially affected — measured directly across `tests/test_scan_e2e.py`,
`tests/test_transform_e2e.py`, `tests/test_sequence_e2e.py`, `tests/test_collisions_wiring.py`,
`tests/test_contracts_criterion_scale.py`, and `tests/test_workers_contracts.py`; none of them
test baseline behavior itself. The ONE fixture that genuinely needs the shipped, unmodified
default is `tests/test_baseline_ok_exclusion.py`, whose entire premise is proving §12.11's "the
exclusion set is empty under the SHIPPED config" sentence -- it overrides the `baseline_build_yaml`
fixture below LOCALLY, to `""` (no override), which pytest resolves ahead of this session-wide one
for tests in that module (closer-scope fixtures always win). `tests/test_baseline_scan_e2e.py` is
the one place this project wants the red path genuinely exercised live, against fixtures
purpose-built and vetted for it -- it does not use this fixture at all.
"""


@pytest.fixture
def baseline_build_yaml() -> str:
    """The default `preflight.baseline_build` YAML override every e2e fixture fleet in this
    suite splices in, unless a test module locally overrides this fixture (see
    `BASELINE_BUILD_DISABLED_YAML`'s own docstring immediately above for why, and for the one
    file that does)."""
    return BASELINE_BUILD_DISABLED_YAML


@pytest.fixture
def frozen_now() -> datetime:
    """A fixed tz-aware instant, so round-trip equality is not a race against the clock."""
    return FROZEN_NOW


@pytest.fixture
def run_id() -> UUID:
    return RUN_ID


@pytest.fixture
def sample_state(run_id: UUID, frozen_now: datetime) -> MigrationState:
    """A state with enough shape that a lossy serializer would be caught: nested phase
    records, a terminal repo, a blocked dependent and a degraded stub consumer."""
    return MigrationState(
        run_id=run_id,
        started_at=frozen_now,
        updated_at=frozen_now,
        config_sha256="a" * 64,
        harness_version="0.1.0",
        budget_remaining_usd=123.45,
        repos={
            "acme-commons": RepoState(
                phase=Phase.TRANSFORM,
                status=RepoStatus.REQUIRES_HUMAN_INTERVENTION,
                attempts=3,
                last_error="ast-grep rule miss on 4 files",
                blast_radius=7,
                wave_index=0,
                dest_path="libs/com/acme/commons",
                updated_at=frozen_now,
                phases={
                    Phase.SCAN: PhaseRecord(
                        phase=Phase.SCAN,
                        status=RepoStatus.SUCCEEDED,
                        started_at=frozen_now,
                        updated_at=frozen_now,
                    ),
                    Phase.TRANSFORM: PhaseRecord(
                        phase=Phase.TRANSFORM,
                        status=RepoStatus.REQUIRES_HUMAN_INTERVENTION,
                        attempts=3,
                        transient_retries=2,
                        last_error="ast-grep rule miss on 4 files",
                        pre_commit_sha="0" * 40,
                        started_at=frozen_now,
                        updated_at=frozen_now,
                    ),
                },
            ),
            "acme-billing": RepoState(
                phase=Phase.SCAN,
                status=RepoStatus.BLOCKED,
                blocked_by=["acme-commons"],
                depends_on=["acme-commons"],
                blast_radius=1,
                wave_index=1,
                updated_at=frozen_now,
            ),
            "acme-portal": RepoState(
                phase=Phase.BUILD,
                status=RepoStatus.DEGRADED,
                stubbed_deps=["maven:com.acme:commons"],
                # SPEC §5.5: `stub_states` must name exactly the coord_keys in `stubbed_deps` —
                # the projection and its lifecycle are written together or not at all (§3.5.1).
                stub_states={"maven:com.acme:commons": StubState.ACTIVE},
                depends_on=["acme-commons"],
                scc_id=None,
                wave_index=1,
                updated_at=frozen_now,
            ),
        },
    )


@pytest.fixture
def worker_ctx(run_id: UUID) -> WorkerContext:
    """A context whose collaborators are inert sentinels.

    `WorkerContext` is a plain dataclass by design (SPEC §7.1) — a worker may only touch what
    it is handed — so a unit test of the retry ladder needs no database, router or logger.

    `deadline` is an ABSOLUTE `loop.time()`, which is `time.monotonic()` for the default event
    loop, so a far-future offset is honest here rather than a magic constant that a long-uptime
    host would silently walk past.
    """
    sentinel: Any = object()
    return WorkerContext(
        run_id=run_id,
        repo_id="acme-commons",
        attempt=1,
        workdir="/nonexistent/worktree",
        lease_owner="test-host:test-container:1:boot",
        lease_fence=1,
        deadline=monotonic() + 3600.0,
        cancel=asyncio.Event(),
        budget=CallBudget(
            remaining_tokens=200_000, remaining_usd=5.0, deadline=monotonic() + 3600.0
        ),
        db=sentinel,
        llm=sentinel,
        router=sentinel,
        limits=sentinel,
        log=sentinel,
    )


# --------------------------------------------------------------------------------------
# real-tool fixtures (see docs/INTEGRATION_HONESTY.md)
# --------------------------------------------------------------------------------------
# **One root, and everything real Bazel writes lives under it.** A full-suite run once drove
# this host's volume to 0 bytes free: 54 output bases at ~236 MB apiece under a single
# `--output_user_root`, plus ~11 GB of Bazel state under `/tmp/pytest-of-<user>`. Neither was an
# accident of cleanup — both are what the fixtures *asked for*:
#
# * `--output_user_root` is NOT an output base. Bazel derives the output base from the MD5 of the
#   workspace directory, so `tmp_path`-per-test meant one ~236 MB `external/` tree, one action
#   cache and one live Bazel server PER TEST, all of them retained. Sharing the root shared
#   nothing. The fix is `bazel_workspace`: ONE stable workspace path, wiped between tests, so
#   every direct-invocation test resolves to ONE output base and one warm server.
# * The tests that drive `fleet build` cannot pass startup flags (the harness owns that argv), so
#   they redirect `XDG_CACHE_HOME`. Pointing it at `tmp_path` put a fresh ~700 MB install base
#   plus an output base inside a directory pytest retains for three sessions. `bazel_cache_home`
#   points it here instead and reaps it when the test ends.
#
# The root lives inside the workspace (CLAUDE.md §2) UNLESS the workspace path contains a space,
# which this checkout's does: Bazel itself prints "Output user root ... contains a space. This
# will probably break the build." That is not a warning to route around by asserting something
# weaker, so the fallback is a stable, space-free directory under the system temp dir, keyed by a
# digest of the repo root so two checkouts never share one.
def _bazel_root() -> Path:
    inside = REPO_ROOT / "tools" / "bazel-test-root"
    if " " not in str(inside):
        return inside
    digest = sha256(str(REPO_ROOT).encode("utf-8")).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / f"fleet-bazel-{digest}"


BAZEL_ROOT = _bazel_root()
BAZEL_OUTPUT_USER_ROOT = BAZEL_ROOT / "out"
BAZEL_WORKSPACE = BAZEL_ROOT / "ws"
BAZEL_XDG_CACHE_HOME = BAZEL_ROOT / "xdg"

NO_C_COMPILER_OUTPUT_USER_ROOT = BAZEL_ROOT / "nocc"
"""Its OWN `--output_user_root`, for the one test that runs Bazel with no C compiler on `PATH`.

Not a hygiene preference: `@@rules_cc++cc_configure_extension+local_config_cc` is generated by
probing the filesystem, and a copy of it generated under a deliberately broken `PATH` must not be
reachable by any other test in the session. Bazel does invalidate it on an environment change,
but "the marker file is right" is a weaker bound than "the directory is gone", and the tests that
share `BAZEL_OUTPUT_USER_ROOT` are the ones that compile real Go and Rust. It lives under
`BAZEL_ROOT` so the peak sampler and `pytest_sessionfinish` account for and reap it like
everything else; the test reaps it in a `finally` as well, so it is empty by then."""

BAZEL_PEAK_CEILING_BYTES: Final = 6 * 1024**3
"""What the whole suite's Bazel state may occupy at its widest, sampled after every real-bazel
test. **Measured, not guessed: a full `-m integration` run peaks at 3.36 GiB** (the session
prints the figure every run), against 16 GB under the per-test output base this replaced. 6 GiB
is ~1.8× that — room for a ruleset to grow, none at all for a regression to per-test bases. It
is a tripwire, not a target: breaching it fails the session."""

BAZEL_KEEP_CACHE_CEILING_BYTES: Final = 3 * 1024**3
"""The one directory deliberately kept between sessions is `repos/`, the content-addressed
repository cache — a stale entry there is a contradiction in terms and re-fetching the ruleset
archives costs ~3 minutes per session. "It is content-addressed" is not a licence to grow without
bound, so there is still a ceiling; what changed is the number and what a breach DOES.

**The number.** Measured, like the peak: `repos/` holds 1595 MiB with every real-Bazel test in
the suite fetching through it, and the whole `BAZEL_ROOT` peaks at 4.18 GiB — so ~2.6 GiB of that
peak is output bases and install bases, not this cache. 3 GiB is the largest this may be while
2.6 GiB of un-kept state still fits under `BAZEL_PEAK_CEILING_BYTES`, and it is ~1.9× what the
cache actually holds — the same headroom ratio the peak ceiling carries, room for a ruleset bump,
none for an unbounded leak. The old 2 GiB left 28% headroom over a cache every real-Bazel test
now depends on, which is a tripwire sited where a single `bazel_dep` bump trips it.

**The breach.** It used to DELETE the whole cache, which is the worst available response: the
next session silently re-fetches every archive for every real-Bazel test — minutes of network per
run, and the intermittent `Read timed out` failures that come with it — and nothing says why. So
a breach is now reported and fails the session, exactly like the peak ceiling, and the bytes stay
on disk. Pruning a cache is a deliberate act (`rm -rf` the path the failure names), not a thing
the test suite does to itself while reporting green."""


@pytest.fixture(scope="session")
def bazel_output_user_root() -> Path:
    BAZEL_OUTPUT_USER_ROOT.mkdir(parents=True, exist_ok=True)
    return BAZEL_OUTPUT_USER_ROOT


@pytest.fixture(scope="session")
def bazel_startup_argv(bazel_output_user_root: Path) -> tuple[str, ...]:
    """`bazel` plus the startup flags every real-bazel test in this suite needs.

    `--noblock_for_lock` so two tests racing one output base fail fast and visibly rather than
    hanging the suite for the length of the other's build.
    """
    return ("bazel", f"--output_user_root={bazel_output_user_root}", "--noblock_for_lock")


@pytest.fixture
def bazel_workspace() -> Iterator[Path]:
    """THE workspace every direct-invocation real-bazel test builds in — one path, session-wide.

    Not `tmp_path`. Bazel keys its output base off the MD5 of the workspace directory, so a fresh
    `tmp_path` per test is a fresh multi-hundred-MB output base per test and a fresh Bazel server
    per test; 54 of them is how a full suite reached 16 GB and 0 bytes free. A stable path means
    the external repositories, the action cache and the running server are shared, which is both
    the disk fix and a large speedup.

    The directory is emptied — not recreated — before each test, so no test can read a file
    another one wrote while the output base it analyses stays warm. Bazel is built for exactly
    this: a workspace whose files changed is the normal case, and it re-analyses what moved.
    """
    BAZEL_WORKSPACE.mkdir(parents=True, exist_ok=True)
    _empty_dir(BAZEL_WORKSPACE)
    yield BAZEL_WORKSPACE
    _empty_dir(BAZEL_WORKSPACE)


@pytest.fixture
def bazel_cache_home(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """`XDG_CACHE_HOME` for the tests where the HARNESS owns the `bazel` argv.

    `fleet build` builds its own command line, so there is no `--output_user_root` for a test to
    add; the only lever is the environment. Redirecting it away from `~/.cache` was always the
    point — a suite does not get to grow a developer's home directory by 700 MB — but pointing it
    at `tmp_path` merely moved the growth somewhere pytest keeps for three sessions.

    The workspace here is the per-test monorepo, so this output base genuinely cannot be shared;
    it is therefore reaped the moment the test ends, which bounds the peak to one at a time.
    """
    BAZEL_XDG_CACHE_HOME.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_CACHE_HOME", str(BAZEL_XDG_CACHE_HOME))
    yield BAZEL_XDG_CACHE_HOME
    reap_bazel_state(BAZEL_XDG_CACHE_HOME)


# --------------------------------------------------------------------------------------
# the reaper, and the numbers it is judged on
# --------------------------------------------------------------------------------------
def _force_rmtree(target: Path) -> None:
    """`rm -rf` that survives Bazel's read-only `external/` trees.

    Not a nicety — it is the difference between a reaper and a reaper-shaped comment. Bazel
    strips the write bit from every fetched external repository directory, and `unlink` needs
    write permission on the *parent*, so a plain `shutil.rmtree(..., ignore_errors=True)` walks
    the tree, fails on every entry, swallows the errors and leaves gigabytes exactly where they
    were while reporting nothing. That is precisely the "mechanism that exists but never runs"
    shape this work exists to remove, so the failure handler restores the bit and retries.
    """

    def _retry(func: Any, path: str, _exc: BaseException) -> None:
        parent = Path(path).parent
        with suppress(OSError):
            parent.chmod(parent.stat().st_mode | stat_module.S_IWUSR | stat_module.S_IXUSR)
        with suppress(OSError):
            Path(path).chmod(Path(path).stat().st_mode | stat_module.S_IWUSR)
        func(path)

    shutil.rmtree(target, onexc=_retry)


def _empty_dir(target: Path) -> None:
    """Remove everything *inside* `target`, keeping the directory itself (and its inode, and
    therefore Bazel's output-base hash)."""
    for entry in target.iterdir():
        if entry.is_dir() and not entry.is_symlink():
            _force_rmtree(entry)
        else:
            entry.unlink(missing_ok=True)


def tree_bytes(root: Path) -> int:
    """Bytes actually occupied under `root`, following no symlinks and counting each hard link
    once — Bazel's execroot is a forest of symlinks into `external/`, and a walker that followed
    them would report a number several times larger than the volume ever gave up."""
    if not root.exists():
        return 0
    total = 0
    seen: set[tuple[int, int]] = set()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(dirpath)
        dirnames[:] = [d for d in dirnames if not (base / d).is_symlink()]
        for name in filenames:
            try:
                stat = (base / name).lstat()
            except OSError:  # a Bazel server unlinking under us is not a measurement failure
                continue
            key = (stat.st_dev, stat.st_ino)
            if stat.st_nlink > 1:
                if key in seen:
                    continue
                seen.add(key)
            total += stat.st_size
    return total


def reap_bazel_state(root: Path) -> int:
    """Shut down every Bazel server living under `root`, delete the tree, return residual bytes.

    A `rm -rf` alone is not enough and not honest: each output base holds a *running* server
    holding its own state open, and one of those per test is the memory half of the same defect.
    So the servers are asked to exit first — `bazel shutdown`, then `SIGTERM` to whatever the pid
    file names if the client could not talk to it — and only then is the tree removed.

    Returns what is still there afterwards, so a caller can assert on the filesystem rather than
    on the intention to have cleaned it.
    """
    if not root.exists():
        return 0
    for pid_file in sorted(root.rglob("server/server.pid.txt")):
        _stop_bazel_server(pid_file)
    with suppress(OSError):
        _force_rmtree(root)
    return tree_bytes(root)


def _stop_bazel_server(pid_file: Path) -> None:
    """`SIGTERM` to the pid Bazel wrote, which is exactly what `bazel shutdown` sends it.

    Deliberately NOT `bazel --output_base=… shutdown`: the `bazel` on PATH here is bazelisk, and
    invoking it outside a workspace makes it resolve a version from nothing and *download* one —
    a teardown that reaches for the network and grows the disk is not a teardown. The pid file is
    the same handle the client would have used.
    """
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return
    with suppress(OSError):
        os.kill(pid, signal.SIGTERM)
    for _ in range(100):  # ~10 s; the server unlinks its own output base entries as it exits
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.1)


_BAZEL_FIXTURES: Final = frozenset(
    {"bazel_startup_argv", "bazel_output_user_root", "bazel_workspace", "bazel_cache_home"}
)
_peak_bytes = 0


def pytest_runtest_teardown(item: pytest.Item) -> None:
    """Sample the whole Bazel root after every test that touched real Bazel.

    Peak is measured here, at the only quiet moment — between tests, with no build in flight —
    because "it cleans up eventually" is exactly what the suite had while 16 GB accumulated. A
    number sampled at every boundary and reported at the end is a bound; a teardown that deletes
    things is only a hope.
    """
    global _peak_bytes
    if _BAZEL_FIXTURES.isdisjoint(item.fixturenames):
        return
    _peak_bytes = max(_peak_bytes, tree_bytes(BAZEL_ROOT))


def _skip_reason(report: pytest.TestReport) -> str:
    """The human-readable reason a skipped report carries, tolerant of both skip shapes.

    A marker-based `@pytest.mark.skipif` and an in-body `pytest.skip(...)` both land here as a
    `(path, lineno, reason)` triple on `.longrepr`, but nothing guarantees that shape forever —
    a bare string is treated as the reason outright rather than raising.
    """
    longrepr = report.longrepr
    if isinstance(longrepr, tuple) and len(longrepr) == 3:
        return str(longrepr[2])
    return str(longrepr)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Leave no output base behind, and say what the run actually cost.

    This is the assertion the incident wanted: it runs after the last test, it looks at the
    filesystem, and a residual output base or a breached peak fails the session — it does not
    print a warning nobody reads. `repos/` is the one deliberate survivor (content-addressed
    archives, ~3 minutes of fetching); it has its own ceiling, and over it the session FAILS
    naming the directory rather than deleting the thing every real-Bazel test now fetches
    through.

    It also reports how many tests skipped and why, for the same reason it reports disk: a run
    that stayed green while covering less than the run before it is a silent regression, and
    "1141 passed" alone cannot distinguish that from a run that covered everything. This does
    NOT fail the session on a skip — some skip conditions are legitimate (an offline lane, a
    tool genuinely absent) — it only makes the count and the reason impossible to miss.
    """
    _ = exitstatus
    if not BAZEL_ROOT.exists():
        return
    peak = max(_peak_bytes, tree_bytes(BAZEL_ROOT))
    kept = BAZEL_ROOT / "repos"
    kept_bytes = tree_bytes(kept)
    residual = 0
    for child in sorted(BAZEL_ROOT.iterdir()):
        if child != kept:
            residual += reap_bazel_state(child)

    breaches = []
    if residual:
        breaches.append(f"{residual} bytes of Bazel state survived the session under {BAZEL_ROOT}")
    if peak > BAZEL_PEAK_CEILING_BYTES:
        breaches.append(
            f"peak Bazel state {peak} bytes over the {BAZEL_PEAK_CEILING_BYTES}-byte ceiling"
        )
    if kept_bytes > BAZEL_KEEP_CACHE_CEILING_BYTES:
        breaches.append(
            f"repository cache {kept} holds {kept_bytes} bytes, over the "
            f"{BAZEL_KEEP_CACHE_CEILING_BYTES}-byte keep ceiling — prune it deliberately "
            f"(`rm -rf {kept}`); the next session re-fetches everything if you do"
        )
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:  # pragma: no branch - always present under the console runner
        reporter.write_sep("-", "bazel disk")
        reporter.write_line(
            f"peak {peak / 1024**3:.2f} GiB (ceiling {BAZEL_PEAK_CEILING_BYTES / 1024**3:.0f} "
            f"GiB) · residual output bases {residual} bytes · repository cache kept "
            f"{kept_bytes / 1024**2:.0f} MiB"
        )
        for breach in breaches:
            reporter.write_line(f"DISK CEILING BREACHED: {breach}", red=True)
        reporter.write_sep("-", "test coverage")
        skipped_reports = reporter.stats.get("skipped", [])
        if skipped_reports:
            reasons: dict[str, int] = {}
            for rep in skipped_reports:
                reason = _skip_reason(rep)
                reasons[reason] = reasons.get(reason, 0) + 1
            reporter.write_line(
                f"{len(skipped_reports)} test(s) skipped this session — a green run that covers "
                f"less than a prior green run is exactly the failure mode this reports, not just "
                f"a diff count:",
                yellow=True,
            )
            for reason, count in sorted(reasons.items()):
                reporter.write_line(f"  ({count}x) {reason}")
        else:
            reporter.write_line("0 tests skipped this session — full collected coverage ran")
    if breaches:
        session.exitstatus = 1


# --------------------------------------------------------------------------------------
# the ruleset archives, and the two reasons a real build here fails without touching src
# --------------------------------------------------------------------------------------
# A ruleset's *metadata* comes from the registry the fixture below resolves; its *source archive*
# comes from wherever that registry's `source.json` points, which for the `aspect_rules_js` /
# `aspect_rules_ts` pins in `build.ruleset_versions` is a GitHub release asset. On this host that
# redirect (`objects.githubusercontent.com`) completes but takes ~17 s per connection, and Bazel's
# default HTTP timeout gives up first — `Error downloading […rules_js-v3.4.0.tar.gz]: Connect
# timed out`, then `no such package '@@aspect_rules_js+//js'`.
#
# That error is *weather*, and it is exactly the hazard `bazel_registry` below was written about:
# a real-bazel test whose failure mode is indistinguishable from a slow socket cannot pin a src
# defect. So the two `.bazelrc` lines a real-build test writes are declared here, once:
#
# * `--http_timeout_scaling` — enough headroom for this host's slow redirect. Measured, not
#   guessed: an unscaled fetch of `aspect_rules_js@3.4.0` fails and a scaled one takes ~176 s.
# * `--repository_cache` — a STABLE directory, so the ~3 minutes of ruleset tarballs are paid
#   once per checkout instead of once per test. It cannot live in the per-test `XDG_CACHE_HOME`
#   those tests set (that is the point of setting it) and it is deliberately the only piece of
#   real-bazel state shared across runs: an archive is content-addressed, so a stale entry is a
#   contradiction in terms.
BAZEL_REPOSITORY_CACHE = BAZEL_ROOT / "repos"
HTTP_TIMEOUT_SCALING: Final = 8.0


@pytest.fixture(scope="session")
def bazel_fetch_bazelrc() -> str:
    """The `.bazelrc` lines that make a fetch on THIS host complete. Never a substitute for a
    finding: nothing here changes what Bazel analyses, only how long it waits for a tarball."""
    BAZEL_REPOSITORY_CACHE.mkdir(parents=True, exist_ok=True)
    return (
        f"common --repository_cache={BAZEL_REPOSITORY_CACHE}\n"
        f"common --http_timeout_scaling={HTTP_TIMEOUT_SCALING}\n"
    )


# --------------------------------------------------------------------------------------
# the module registry the real-bazel tests resolve against
# --------------------------------------------------------------------------------------
# **A skip is not a verification, and this suite had four of them pointed at the only real check
# it has on generated Bazel output.** `bcr.bazel.build` is unreachable from this host — the
# socket is accepted and then stalls, so Bazel spends its timeout and reports `Error accessing
# registry …: Connect timed out`. The old guard turned that into `pytest.skip`, and a skipped
# test reads as a green one in every summary line anybody actually looks at. The result was a
# `MODULE.bazel` generator whose acceptance tests had silently stopped running.
#
# So the default here is FAIL, not skip, and the reasoning is this: the registry is now a
# configured input (`build.registry` → `--registry=`), and a configured input that does not work
# is a broken configuration, not weather. There is a second, byte-identical address for BCR that
# this host CAN reach (`BCR_MIRROR_REGISTRY`), so "no registry is reachable" is a much stronger
# claim than "bcr.bazel.build is down", and it is the right thing to be loud about — a fleet
# whose pins resolve nowhere cannot build anything either.
#
# Degrading to a skip therefore requires somebody to say so out loud, by exporting
# `FLEET_TEST_ALLOW_OFFLINE_BAZEL=1` (an air-gapped CI lane is the legitimate case). Even then
# the skip reason names every registry that was probed and the exact error each returned, so it
# can never again read as "bazel is not installed".
REGISTRY_ENV: Final = "FLEET_TEST_BAZEL_REGISTRY"
"""Pin the registry for the suite explicitly. Set it and nothing else is probed or substituted:
an operator naming a registry wants THAT registry's answer, and a silent fallback to another
would make the run prove something about a module graph nobody asked for."""

ALLOW_OFFLINE_ENV: Final = "FLEET_TEST_ALLOW_OFFLINE_BAZEL"
"""Set to `1` to convert "no registry answered" from a failure into a (fully-detailed) skip."""

REGISTRY_PROBE_TIMEOUT_S: Final = 8.0
"""Long enough for a cold TLS handshake, short enough that a blackholed host does not add its
own timeout — Bazel's — to every session before anyone learns why."""


def _probe_registry(url: str) -> str:
    """`""` if the registry answered, else a one-line description of how it did not.

    `bazel_registry.json` is the file Bazel itself fetches first from a registry root, so a 200
    here is the same reachability question the build will ask, not a ping.
    """
    target = f"{url.rstrip('/')}/bazel_registry.json"
    try:
        with urllib.request.urlopen(target, timeout=REGISTRY_PROBE_TIMEOUT_S) as response:  # noqa: S310
            status = int(response.status)
    except Exception as exc:  # every failure mode is equally "the registry did not answer"
        return f"{target}: {type(exc).__name__}: {exc}"
    return "" if status == 200 else f"{target}: HTTP {status}"


@cache
def _resolve_bazel_registry() -> tuple[str | None, tuple[str, ...]]:
    """`(registry, failures)` — resolved once per session, because probing is network I/O.

    Order: the env pin if there is one; otherwise `build.registry`, then the two known BCR
    addresses. The first that answers wins and the rest are never contacted.
    """
    pinned = os.environ.get(REGISTRY_ENV, "").strip()
    candidates = (
        (pinned,)
        if pinned
        else tuple(
            dict.fromkeys(
                [BuildSection().registry or BCR_DEFAULT_REGISTRY, BCR_MIRROR_REGISTRY]
            )
        )
    )
    failures: list[str] = []
    for candidate in candidates:
        failure = _probe_registry(candidate)
        if not failure:
            return candidate, ()
        failures.append(failure)
    return None, tuple(failures)


@pytest.fixture(scope="session")
def bazel_registry() -> str:
    """The registry every real-bazel test passes as `--registry=`, or a loud stop.

    Returned as an explicit URL rather than `None`-means-default so that the argv a test builds
    says which registry the assertion was made against — a verification whose subject is implicit
    cannot be audited later.
    """
    registry, failures = _resolve_bazel_registry()
    if registry is not None:
        return registry
    detail = "\n".join(f"  - {failure}" for failure in failures)
    message = (
        "no Bazel module registry answered, so nothing below can resolve a `bazel_dep` and the "
        "generated-MODULE.bazel acceptance tests would prove nothing:\n"
        f"{detail}\n"
        f"Set {REGISTRY_ENV}=<url> to name a reachable registry (a private BCR mirror, a "
        f"vendored copy), or {ALLOW_OFFLINE_ENV}=1 to accept an unverified run as skips."
    )
    if os.environ.get(ALLOW_OFFLINE_ENV, "").strip() in {"1", "true", "yes"}:
        pytest.skip(message)
    pytest.fail(message)


@pytest.fixture(scope="session")
def bazel_registry_args(bazel_registry: str) -> tuple[str, ...]:
    """The flag itself, built by `fleet.bazel.query` — the same code path the harness runs.

    Not `(f"--registry={url}",)` spelled here: a test that hand-rolls the flag proves the flag
    works and says nothing about whether the harness ever emits it.
    """
    return registry_args(bazel_registry)
