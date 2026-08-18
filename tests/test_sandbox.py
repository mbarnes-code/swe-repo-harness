"""`sandbox/`: worktree lifecycle and the network-less container (SPEC §3.2/§3.3, §11.5).

The worktree tests run against a REAL temp git repo — `git` is a hard dependency of the harness,
and a mocked `git worktree add` would prove only that we can spell the flags. The container
tests run against an injected fake runner, because command construction is the thing that can be
wrong (a missing `--memory` silently voids the §11.3 host ceiling) and a daemon is not needed to
observe it. The two tests that genuinely need Docker skip loudly when it is absent — and gate on
the image being present LOCALLY, never pulling one.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path
from uuid import UUID

import pytest

from fleet.sandbox.container import (
    ContainerSandbox,
    ContainerSpec,
    Mount,
    docker_run_argv,
    spec_for_attempt,
)
from fleet.sandbox.worktree import (
    Worktree,
    WorktreeError,
    WorktreeManager,
    run_prefix,
    sandbox_name,
)
from fleet.settings import VerifySection
from fleet.util.fs import DiskFloorBreached
from fleet.util.proc import ProcResult, is_producible_shape, run
from fleet.workers.buildverify import C_TOOLCHAIN_PROBE

RUN_ID = UUID("00000000-0000-4000-8000-00000000abcd")
DOCKER_TEST_IMAGE = os.environ.get("FLEET_TEST_DOCKER_IMAGE", "busybox:latest")
FLEET_BUILD_IMAGE = VerifySection().container_image
"""Read from the setting, never re-typed: this test is about the image the fleet will actually
run, so retagging in `settings.py` must move the test with it."""

UNMAPPED_UID = "4242:4242"
"""A uid/gid with no `/etc/passwd` entry — the condition `current_user_spec()` creates in
production (the HOST user's uid, which no image knows) reproduced deterministically. Using the
real host uid here would make the test's premise depend on whoever ran it."""


# --------------------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------------------
class FakeRunner:
    """Records argv instead of executing it — the `CommandRunner` seam in one class.

    `started`/`timed_out` are constructible (mirroring `tests/test_vcs.py`'s `ScriptedRunner`,
    D45) so a `ProcResult` that never settled can be scripted for the D44 coverage below: `not
    result.ok` is false for three different reasons — never started, killed at its deadline, or a
    genuine non-zero exit — and a fake that can only build the last makes the first two
    untestable.
    """

    def __init__(
        self,
        *,
        stdout: str = "",
        exit_code: int = 0,
        started: bool = True,
        timed_out: bool = False,
    ) -> None:
        if not is_producible_shape(started=started, timed_out=timed_out, exit_code=exit_code):
            raise ValueError(
                f"FakeRunner(started={started}, timed_out={timed_out}, exit_code={exit_code}) "
                "is a state util.proc.run can never produce"
            )
        self.calls: list[tuple[str, ...]] = []
        self.stdout = stdout
        self.exit_code = exit_code
        self.started = started
        self.timed_out = timed_out

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
        return ProcResult(
            argv=parts,
            exit_code=self.exit_code,
            stdout_tail=self.stdout,
            stderr_tail="",
            duration_ms=1,
            timed_out=self.timed_out,
            started=self.started,
            cwd=cwd,
        )


# --------------------------------------------------------------------------------------
# naming
# --------------------------------------------------------------------------------------
def test_name_matches_the_spec_scheme() -> None:
    """`fleet-<run_id>-<repo>-<attempt>` (SPEC §3.3) is what `fleet resume` reaps by, so the
    scheme is a contract with the reaper, not cosmetics."""
    assert sandbox_name(RUN_ID, "acme-commons", 2) == f"fleet-{RUN_ID}-acme-commons-2"
    assert run_prefix(RUN_ID) == f"fleet-{RUN_ID}-"
    # A repo id containing `/` must still yield one path segment and a legal container name.
    assert "/" not in sandbox_name(RUN_ID, "acme/commons", 1)
    assert sandbox_name(RUN_ID, "acme/commons", 1).startswith(run_prefix(RUN_ID))


def test_worktree_and_container_share_one_name() -> None:
    """One string names both, so an orphan of either kind is attributable to its task by name
    alone — which is what lets the reaper work before it reads the database."""
    name = sandbox_name(RUN_ID, "acme-commons", 3)
    spec = spec_for_attempt(
        run_id=RUN_ID,
        repo="acme-commons",
        attempt=3,
        image="img:1",
        command=["true"],
        worktree=Path("/work/x"),
    )
    assert spec.name == name


# --------------------------------------------------------------------------------------
# worktrees, against a real git repo
# --------------------------------------------------------------------------------------
async def _git(cwd: Path, *args: str) -> ProcResult:
    return await run(["git", *args], cwd=cwd, timeout_s=60)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A real repo with one commit — `git worktree add` needs a resolvable ref."""

    async def build() -> Path:
        repo = tmp_path / "repo"
        repo.mkdir()
        await _git(repo, "init", "--initial-branch=main", ".")
        await _git(repo, "config", "user.email", "fleet@example.invalid")
        await _git(repo, "config", "user.name", "Fleet Test")
        (repo / "README.md").write_text("hello\n")
        await _git(repo, "add", "README.md")
        await _git(repo, "commit", "-m", "initial")
        return repo

    return asyncio.run(build())


async def test_worktree_create_remove_roundtrip(git_repo: Path, tmp_path: Path) -> None:
    """The lifecycle the build phase depends on: a checkout cut from a ref, then removed."""
    manager = WorktreeManager(repo_dir=git_repo, work_dir=tmp_path / "work", run_id=RUN_ID)

    wt = await manager.create("acme-commons", 1, "main")
    assert isinstance(wt, Worktree)
    assert wt.name == sandbox_name(RUN_ID, "acme-commons", 1)
    assert wt.path.is_dir()
    assert (wt.path / "README.md").read_text() == "hello\n"
    assert wt.path in await manager.list_registered()

    assert await manager.remove(wt) is True
    assert not wt.path.exists()
    assert wt.path not in await manager.list_registered()


async def test_removing_an_already_removed_worktree_is_not_an_error(
    git_repo: Path, tmp_path: Path
) -> None:
    """Cleanup runs after `on_cancel`, after a crash, and AGAIN at `fleet resume`. If the second
    removal raised, one crash would leave the run permanently un-resumable."""
    manager = WorktreeManager(repo_dir=git_repo, work_dir=tmp_path / "work", run_id=RUN_ID)
    wt = await manager.create("acme-commons", 1, "main")

    assert await manager.remove(wt) is True
    assert await manager.remove(wt) is False  # idempotent, not an exception
    assert await manager.remove("fleet-never-existed") is False


async def test_worktree_removal_survives_a_directory_deleted_underneath_git(
    git_repo: Path, tmp_path: Path
) -> None:
    """A killed task can leave the directory gone but git's administrative record behind. Cleanup
    must reconcile that instead of refusing to run."""
    manager = WorktreeManager(repo_dir=git_repo, work_dir=tmp_path / "work", run_id=RUN_ID)
    wt = await manager.create("acme-commons", 2, "main")
    shutil.rmtree(wt.path)

    assert await manager.remove(wt) is False
    assert wt.path not in await manager.list_registered()


async def test_create_refuses_a_second_owner(git_repo: Path, tmp_path: Path) -> None:
    """A worktree has exactly ONE owning task (SPEC §11.5). Two owners would make the Git-native
    'did this task's commit land?' question unanswerable."""
    manager = WorktreeManager(repo_dir=git_repo, work_dir=tmp_path / "work", run_id=RUN_ID)
    await manager.create("acme-commons", 1, "main")

    with pytest.raises(WorktreeError):
        await manager.create("acme-commons", 1, "main")


async def test_reap_spares_a_live_owner(git_repo: Path, tmp_path: Path) -> None:
    """`fleet resume` reaps `fleet-<run_id>-*` worktrees 'that no live phases row claims'. A
    reaper that ignores liveness deletes the checkout of a task mid-`git apply`."""
    manager = WorktreeManager(repo_dir=git_repo, work_dir=tmp_path / "work", run_id=RUN_ID)
    live = await manager.create("acme-commons", 1, "main")
    dead = await manager.create("acme-widgets", 1, "main")

    reaped = await manager.reap(live_names={live.name})

    assert reaped == [dead.name]
    assert live.path.is_dir(), "a live task's worktree was reaped"
    assert not dead.path.exists()


# --------------------------------------------------------------------------------------
# D44 — `remove()`'s `rmtree` must be licensed by a SETTLED git refusal, never by a probe that
# never established one. `not result.ok` is false for three different reasons — never started,
# killed at its deadline, or a genuine non-zero exit — and only the last means "git refused this
# path"; the other two mean the fleet's clock, not git, and must not authorise deleting a
# still-registered worktree.
# --------------------------------------------------------------------------------------
async def test_remove_does_not_delete_a_worktree_when_the_probe_never_started(
    tmp_path: Path,
) -> None:
    """A `git worktree remove` made after the deadline had already passed never runs at all —
    `started=False, timed_out=True` together (§7.1's passed-deadline synthesis) — so git was
    never consulted about the path. `remove()` must not read that silence as a refusal and
    delete a still-registered worktree out from under it. The raised message must say "never
    started", not "timed out", even though `timed_out` is also `True` here — `no_verdict` checks
    `started` BEFORE `timed_out` for exactly this reason.
    """
    path = tmp_path / "work" / "fleet-run-acme-1"
    path.mkdir(parents=True)
    (path / "marker").write_text("still here")
    runner = FakeRunner(exit_code=124, started=False, timed_out=True)
    manager = WorktreeManager(
        repo_dir=tmp_path / "repo", work_dir=tmp_path / "work", run_id=RUN_ID, runner=runner
    )

    with pytest.raises(WorktreeError) as caught:
        await manager.remove(path)

    assert "never started" in str(caught.value)
    assert path.exists(), "an unsettled probe must not authorise the rmtree"
    assert (path / "marker").exists()


async def test_remove_does_not_delete_a_worktree_when_the_probe_is_killed_at_its_deadline(
    tmp_path: Path,
) -> None:
    """The second unsettled shape: the process genuinely ran (`started=True`) but was killed at
    its deadline before git could answer. `-15` is SIGTERM's negative signal code, not a git
    verdict, and must not license the delete either."""
    path = tmp_path / "work" / "fleet-run-acme-2"
    path.mkdir(parents=True)
    runner = FakeRunner(exit_code=-15, started=True, timed_out=True)
    manager = WorktreeManager(
        repo_dir=tmp_path / "repo", work_dir=tmp_path / "work", run_id=RUN_ID, runner=runner
    )

    with pytest.raises(WorktreeError) as caught:
        await manager.remove(path)

    assert "killed at its deadline" in str(caught.value)
    assert path.exists()


async def test_remove_still_deletes_on_a_settled_genuine_refusal(tmp_path: Path) -> None:
    """The case that proves the fix is not a blanket raise-on-any-failure: a `git worktree
    remove` that actually RAN and actually refused (`started=True, timed_out=False`, non-zero
    exit) is a real answer, and the old behaviour — `rmtree` the orphaned directory — must still
    fire unchanged."""
    path = tmp_path / "work" / "fleet-run-acme-3"
    path.mkdir(parents=True)
    runner = FakeRunner(exit_code=128, started=True, timed_out=False)
    manager = WorktreeManager(
        repo_dir=tmp_path / "repo", work_dir=tmp_path / "work", run_id=RUN_ID, runner=runner
    )

    existed = await manager.remove(path)

    assert existed is True
    assert not path.exists(), "a settled genuine refusal must still authorise the rmtree"


# --------------------------------------------------------------------------------------
# container command construction, no daemon required
# --------------------------------------------------------------------------------------
async def test_docker_argv_carries_caps_name_and_no_network() -> None:
    """Each flag here is a guarantee somewhere else in the spec: `--network=none` is what makes a
    green build evidence rather than a download; `--memory` is half of the §11.3 host-memory
    arithmetic; `--name` is what teardown and the reaper address; `--user` keeps root out of a
    host-owned worktree."""
    spec = spec_for_attempt(
        run_id=RUN_ID,
        repo="acme-commons",
        attempt=1,
        image="ghcr.io/acme/fleet-build:2026-08",
        command=["bazel", "build", "//java/acme/commons/..."],
        worktree=Path("/work/wt"),
        memory="8g",
        cpus="4.0",
        extra_mounts=[Mount(source=Path("/cache/toolchain"), target="/toolchain", read_only=True)],
        env={"BAZEL_JOBS": "4"},
    )
    argv = docker_run_argv(spec)

    assert argv[:2] == ["docker", "run"]
    assert "--rm" in argv
    assert f"--name={sandbox_name(RUN_ID, 'acme-commons', 1)}" in argv
    assert "--network=none" in argv
    assert "--memory=8g" in argv
    assert "--cpus=4.0" in argv
    assert f"--user={os.getuid()}:{os.getgid()}" in argv
    assert "--volume=/work/wt:/work" in argv
    assert "--volume=/cache/toolchain:/toolchain:ro" in argv
    assert "--env=BAZEL_JOBS=4" in argv
    # image then command, in that order, with the command never flattened into a string.
    image_at = argv.index("ghcr.io/acme/fleet-build:2026-08")
    assert argv[image_at + 1 :] == ["bazel", "build", "//java/acme/commons/..."]


async def test_container_is_force_removed_even_when_the_command_times_out() -> None:
    """Killing the `docker run` CLIENT does not stop the container: the runner kills it by name
    (SPEC §11.1). Without this `finally`, a timed-out build keeps 8 GiB of the host ceiling."""

    class TimingOutRunner(FakeRunner):
        async def __call__(  # type: ignore[override]
            self,
            argv: Sequence[str],
            *,
            cwd: Path | None = None,
            env: Mapping[str, str] | None = None,
            deadline: float | None = None,
            timeout_s: float | None = None,
        ) -> ProcResult:
            result = await FakeRunner.__call__(
                self, argv, cwd=cwd, env=env, deadline=deadline, timeout_s=timeout_s
            )
            if argv[1] == "run":
                return ProcResult(
                    argv=result.argv,
                    exit_code=-9,
                    stdout_tail="",
                    stderr_tail="",
                    duration_ms=1,
                    timed_out=True,
                )
            return result

    runner = TimingOutRunner()
    sandbox = ContainerSandbox(runner=runner)
    spec = spec_for_attempt(
        run_id=RUN_ID,
        repo="acme-commons",
        attempt=1,
        image="img:1",
        command=["bazel", "build", "//..."],
        worktree=Path("/work/wt"),
    )

    result = await sandbox.run(spec, timeout_s=1)

    assert result.timed_out is True
    assert runner.calls[-1] == ("docker", "rm", "--force", spec.name)


async def test_a_container_is_not_started_when_the_volume_is_under_the_floor() -> None:
    """§11.3: `preflight.min_free_bytes` is re-checked before EVERY container start.

    Why the check lives on the spec and is made here rather than in the caller: what fills the
    volume is what the container WRITES, and that is exactly the bind mounts — the worktree plus
    the read-write Bazel disk and repository caches §3.4 shares across every container. Checking
    each mount's volume is therefore checking the thing that will actually run out.

    It raises instead of returning a failed `ProcResult` because "refused to start" and "started
    and exited non-zero" are different events: collapsed together, a full disk would be charged
    to the repo's retry ladder as a build failure.
    """
    runner = FakeRunner()
    sandbox = ContainerSandbox(runner=runner)
    spec = spec_for_attempt(
        run_id=RUN_ID,
        repo="acme-commons",
        attempt=1,
        image="img:1",
        command=["bazel", "build", "//..."],
        worktree=Path.cwd(),
        min_free_bytes=2**62,  # ~4.6 EB: a real statvfs of a real volume, no patching
    )

    with pytest.raises(DiskFloorBreached) as raised:
        await sandbox.run(spec, timeout_s=1)

    assert raised.value.floor == 2**62
    assert raised.value.free == shutil.disk_usage(Path.cwd()).free
    assert str(raised.value.free) in str(raised.value), str(raised.value)
    assert not runner.calls, f"docker was invoked anyway: {runner.calls}"


async def test_a_container_starts_normally_when_the_volume_clears_the_floor() -> None:
    """The negative control. A gate that refused unconditionally would pass the test above and
    stop every sandboxed build in the fleet."""
    runner = FakeRunner()
    spec = spec_for_attempt(
        run_id=RUN_ID,
        repo="acme-commons",
        attempt=1,
        image="img:1",
        command=["true"],
        worktree=Path.cwd(),
        min_free_bytes=1,
    )

    await ContainerSandbox(runner=runner).run(spec, timeout_s=1)

    assert runner.calls[0][1] == "run"


async def test_reap_lists_by_run_prefix_and_spares_live_containers() -> None:
    """`fleet resume` step 2, containers half: same liveness rule as worktrees."""
    live = sandbox_name(RUN_ID, "acme-commons", 1)
    dead = sandbox_name(RUN_ID, "acme-widgets", 1)
    runner = FakeRunner(stdout=f"{live}\n{dead}\n")
    sandbox = ContainerSandbox(runner=runner)

    reaped = await sandbox.reap(run_id=RUN_ID, live_names={live})

    assert reaped == [dead]
    listing = runner.calls[0]
    # The prefix is `re.escape`d before it reaches docker's `--filter` (see
    # `test_list_by_prefix_escapes_dots_so_a_sibling_repo_is_not_cross_matched`), so the argv
    # under test carries the ESCAPED form, not the raw string `run_prefix` returns.
    assert f"name=^{re.escape(run_prefix(RUN_ID))}" in listing
    assert "--all" in listing
    assert ("docker", "rm", "--force", dead) in runner.calls
    assert ("docker", "rm", "--force", live) not in runner.calls


class RegexFilterRunner:
    """Emulates Docker's REAL `--filter name=^<pattern>` semantics against a fixed pool of
    container names — `<pattern>` is matched as a regex, exactly like the daemon does, instead of
    a stub that treats it as a literal prefix. That distinction is the whole point: a fake that
    special-cases prefix matching would pass even if `list_by_prefix` stopped escaping, and prove
    nothing about the bug this guards (research-36 Q1.5(1) — MEASURED: `docker ps --filter
    'name=^resq1d-a.b-1-t'` matched a SIBLING container `resq1d-aXb-1-t...` because `.` is a regex
    metacharacter `slug` deliberately preserves).
    """

    def __init__(self, names: Sequence[str]) -> None:
        self.names = list(names)
        self.removed: list[str] = []

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
        if parts[1] == "ps":
            raw = parts[parts.index("--filter") + 1]
            assert raw.startswith("name=")
            matched = [n for n in self.names if re.match(raw[len("name=") :], n)]
            return ProcResult(
                argv=parts, exit_code=0, stdout_tail="\n".join(matched), stderr_tail="",
                duration_ms=1, timed_out=False,
            )
        assert parts[1] == "rm"
        name = parts[-1]
        self.removed.append(name)
        return ProcResult(
            argv=parts, exit_code=0, stdout_tail=name, stderr_tail="", duration_ms=1,
            timed_out=False,
        )


async def test_list_by_prefix_escapes_dots_so_a_sibling_repo_is_not_cross_matched() -> None:
    """Regression test for research-36 Q1.5(1): `slug` (`sandbox/worktree.py`) deliberately
    PRESERVES `.` in a repo id (`my.repo.js` stays `my.repo.js`), and Docker's `name` filter is a
    REGEX, so an unescaped `^{prefix}` lets that `.` match ANY character. `_container_prefix`
    (`buildverify.py`) builds exactly this shape — `sandbox_name(run_id, repo, attempt) + "-t"` —
    and `on_cancel` sweeps it with `list_by_prefix`, force-removing everything it returns. Two
    sibling repos whose names differ only where the target has a literal `.` — `my.repo.js` and
    `myXrepo.js` — must stay distinguishable, or a cancel/sweep for one repo `docker rm --force`s
    the OTHER repo's live container (which then exits 137, read by the harness as a substantive
    failure, not infrastructure — research-36 Q1(d)). Without `re.escape` in `list_by_prefix`,
    both names come back and this assertion fails.
    """
    target_repo = "my.repo.js"
    victim_repo = "myXrepo.js"  # differs from target ONLY at the position of target's literal `.`
    target_name = f"{sandbox_name(RUN_ID, target_repo, 1)}-t{'a' * 8}"
    victim_name = f"{sandbox_name(RUN_ID, victim_repo, 1)}-t{'b' * 8}"
    prefix = f"{sandbox_name(RUN_ID, target_repo, 1)}-t"
    runner = RegexFilterRunner([target_name, victim_name])
    sandbox = ContainerSandbox(runner=runner)

    matched = await sandbox.list_by_prefix(prefix)

    assert matched == [target_name]
    assert victim_name not in matched


# --------------------------------------------------------------------------------------
# the tests that need a live daemon
# --------------------------------------------------------------------------------------
def _docker_usable() -> tuple[bool, str]:
    """Skip honestly: report WHY, and never let an absent daemon look like a pass."""
    if shutil.which("docker") is None:
        return False, "docker CLI not on PATH"

    async def probe() -> tuple[bool, str]:
        if not await ContainerSandbox().available():
            return False, "docker daemon not reachable"
        images = await run(["docker", "images", "--quiet", DOCKER_TEST_IMAGE], timeout_s=30)
        if not images.ok or not images.stdout_tail.strip():
            return False, f"image {DOCKER_TEST_IMAGE} not present locally (no network pull here)"
        return True, ""

    return asyncio.run(probe())


_DOCKER_OK, _DOCKER_WHY = _docker_usable()


@pytest.mark.integration
@pytest.mark.skipif(not _DOCKER_OK, reason=f"docker sandbox unavailable: {_DOCKER_WHY}")
async def test_real_container_runs_without_network_and_is_torn_down(tmp_path: Path) -> None:
    """Against a live daemon: the mounted worktree is visible, the network is genuinely absent,
    and the container does not survive the call."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "marker.txt").write_text("mounted\n")

    sandbox = ContainerSandbox()
    spec = spec_for_attempt(
        run_id=RUN_ID,
        repo="acme-commons",
        attempt=7,
        image=DOCKER_TEST_IMAGE,
        command=["sh", "-c", "cat /work/marker.txt; ip -o link show | wc -l"],
        worktree=worktree,
        memory="256m",
        cpus="1.0",
    )

    result = await sandbox.run(spec, timeout_s=120)

    assert result.ok is True, result.stderr_tail
    lines = result.stdout_tail.split()
    assert "mounted" in lines
    # `--network=none` leaves loopback and nothing else.
    assert lines[-1] == "1", f"expected only loopback, got: {result.stdout_tail!r}"
    assert spec.name not in await sandbox.list_by_prefix(run_prefix(RUN_ID))


def _fleet_build_image_usable() -> tuple[bool, str]:
    """`_docker_usable`'s shape, deliberately: gate on the image being present LOCALLY and never
    pull. This suite already fights live-index flakes, and a registry round-trip would make a
    green run depend on someone else's uptime."""
    if shutil.which("docker") is None:
        return False, "docker CLI not on PATH"

    async def probe() -> tuple[bool, str]:
        if not await ContainerSandbox().available():
            return False, "docker daemon not reachable"
        images = await run(["docker", "images", "--quiet", FLEET_BUILD_IMAGE], timeout_s=30)
        if not images.ok or not images.stdout_tail.strip():
            return False, (
                f"image {FLEET_BUILD_IMAGE} not built on this host (no network pull here); "
                f"build it: docker build -f docker/fleet-build.Dockerfile "
                f"-t {FLEET_BUILD_IMAGE} docker/"
            )
        return True, ""

    return asyncio.run(probe())


_IMAGE_OK, _IMAGE_WHY = _fleet_build_image_usable()


@pytest.mark.integration
@pytest.mark.skipif(not _IMAGE_OK, reason=f"fleet build image unusable: {_IMAGE_WHY}")
async def test_the_fleet_build_image_runs_bazels_lookups_as_an_unmapped_uid() -> None:
    """Against the real image `docker/fleet-build.Dockerfile` builds: the two things a passwd-less
    `--user` breaks, checked where they break.

    **The compiler lookup.** The first command is `sh -c C_TOOLCHAIN_PROBE` — the CONSTANT, so a
    change to the probe changes what this asserts rather than silently drifting from it — because
    `local_config_cc` is fetched before any target of any language analyses, and a
    `Cannot find gcc or CC` there is a bare exit 1 the fleet spends three rungs on.

    **The client's user name.** `--user 4242:4242` has no `/etc/passwd` entry, and for such a uid
    Docker sets `HOME=/` (unwritable) and leaves `USER` unset. Bazel's *client* then dies in
    `blaze::GetUserName()` with `FATAL: $USER is not set, and unable to look up name of current
    user` — `LOCAL_ENVIRONMENTAL_ERROR`, exit 36, which is in `INFRA_EXIT_CODES`, so ADR-0014
    spends no attempt and the fleet re-queues the repo forever against an image defect. The
    image's `ENV USER`/`ENV HOME` are the fix, and the assertions below are exactly what makes
    them non-removable.

    **No `--env`, and that is asserted rather than assumed:** `buildverify` passes none, so image
    `ENV` is the ONLY channel those two values can arrive by. An `--env` in this argv would make
    the test prove something the fleet never does.

    **What this does NOT prove.** That a build works. No `bazel` runs here — `command -v` resolves
    a path, it does not execute it — and the mounted repository cache is empty (`_cache_mounts`
    creates the directory and nothing fills it), so a `--network=none` build still resolves no
    module and stays red. This test closes the image defect, not the cache one.
    """
    sandbox = ContainerSandbox()
    probe = ContainerSpec(
        image=FLEET_BUILD_IMAGE,
        name=f"{sandbox_name(RUN_ID, 'fleet-build-image', 1)}-cc-probe",
        command=("sh", "-c", C_TOOLCHAIN_PROBE),
        network="none",
        user=UNMAPPED_UID,
    )
    assert not [a for a in docker_run_argv(probe) if a.startswith("--env")], (
        "the harness passes no --env; a test that did would prove the wrong image"
    )

    found = await sandbox.run(probe, timeout_s=120)

    assert found.ok, (
        f"{FLEET_BUILD_IMAGE} fails Bazel's own compiler lookup: {found.stderr_tail}"
    )
    assert "gcc" in found.stdout_tail, found.stdout_tail

    environment = ContainerSpec(
        image=FLEET_BUILD_IMAGE,
        name=f"{sandbox_name(RUN_ID, 'fleet-build-image', 1)}-env",
        command=(
            "sh",
            "-c",
            'printf "bazel=%s\\nuser=%s\\npasswd=%s\\n" '
            '"$(command -v bazel)" "$USER" "$(getent passwd 4242 || echo NONE)"; '
            'touch "$HOME/.fleet-writable" && rm -f "$HOME/.fleet-writable" '
            '&& printf "home_writable=%s\\n" "$HOME"',
        ),
        network="none",
        user=UNMAPPED_UID,
    )

    result = await sandbox.run(environment, timeout_s=120)

    assert result.ok, result.stderr_tail
    reported = dict(
        line.split("=", 1) for line in result.stdout_tail.split("\n") if "=" in line
    )
    assert reported.get("passwd") == "NONE", (
        f"uid 4242 has a passwd entry in {FLEET_BUILD_IMAGE}; this test no longer reproduces "
        "the condition it exists for"
    )
    assert reported.get("bazel", "").endswith("/bazel"), result.stdout_tail
    assert reported.get("user"), (
        "USER is empty, so Bazel's client would call GetUserName() and exit 36 — the image lost "
        "its `ENV USER`"
    )
    assert reported.get("home_writable", "").startswith("/"), (
        f"$HOME is not writable as an unmapped uid — Docker's default `HOME=/` for a passwd-less "
        f"user is not, which is the other half of exit 36: {result.stdout_tail}"
    )
