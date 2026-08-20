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
    ContainerReapFailure,
    ContainerReapResult,
    ContainerSandbox,
    ContainerSpec,
    Mount,
    claims,
    docker_run_argv,
    spec_for_attempt,
)
from fleet.sandbox.worktree import (
    ReapFailure,
    ReapResult,
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

    result = await manager.reap(live_names={live.name})

    assert result.reaped == [dead.name]
    assert result.failed == []
    assert result.complete is True
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


class _FailOneRemoveRunner:
    """Real git for every call except `worktree remove --force <fail_path>`, which is answered
    with a scripted, deliberately UNSETTLED `ProcResult` (the D44 shape) instead of running git.

    This is what lets a test drive `WorktreeManager.reap()`'s loop against a REAL multi-worktree
    repo (so `list_registered`/`prune` behave exactly as production sees them) while making
    exactly one `remove()` call inside that loop raise `WorktreeError` — the scenario `reap()`
    must survive without discarding work already done or abandoning work still to do.
    """

    def __init__(self, *, fail_path: Path, exit_code: int, started: bool, timed_out: bool) -> None:
        if not is_producible_shape(started=started, timed_out=timed_out, exit_code=exit_code):
            raise ValueError(
                f"_FailOneRemoveRunner(started={started}, timed_out={timed_out}, "
                f"exit_code={exit_code}) is a state util.proc.run can never produce"
            )
        self._fail_path = str(fail_path)
        self._exit_code = exit_code
        self._started = started
        self._timed_out = timed_out
        self.calls: list[tuple[str, ...]] = []

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
        if "remove" in parts and self._fail_path in parts:
            return ProcResult(
                argv=parts,
                exit_code=self._exit_code,
                stdout_tail="",
                stderr_tail="",
                duration_ms=1,
                timed_out=self._timed_out,
                started=self._started,
                cwd=cwd,
            )
        return await run(list(argv), cwd=cwd, env=env, deadline=deadline, timeout_s=timeout_s)


class _FailOneRemoveWithEnvironmentFaultRunner:
    """Real git for every call except `worktree remove --force <fail_path>`, which raises
    `OSError` instead of returning a `ProcResult` at all — the shape `_run_locked`'s unguarded
    `asyncio.create_subprocess_exec` produces for a missing `git` binary (`FileNotFoundError`), a
    `PermissionError` on `cwd`, or resource exhaustion. Distinct from `_FailOneRemoveRunner`
    above: that class scripts a git process that RAN and never settled (the D44 shape); this one
    scripts the process never being spawned at all — the `remove()` call is never even consulted
    about the path, so the failure must read as an environment fault, not a git refusal.
    """

    def __init__(self, *, fail_path: Path) -> None:
        self._fail_path = str(fail_path)
        self.calls: list[tuple[str, ...]] = []

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
        if "remove" in parts and self._fail_path in parts:
            raise FileNotFoundError(2, "No such file or directory", self._fail_path)
        return await run(list(argv), cwd=cwd, env=env, deadline=deadline, timeout_s=timeout_s)


# --------------------------------------------------------------------------------------
# `WorktreeManager.reap()` must not let ONE unsettled `remove()` (D44's shape) collapse the
# whole sweep. Two failure modes are equally wrong: aborting mid-loop discards worktrees already
# reaped AND abandons the ones still to come; swallowing the failure silently reports a partial
# sweep as if it were complete. Both are the four-state-collapse family D44 belongs to, one layer
# up (docs/INTEGRATION_HONESTY.md D44).
# --------------------------------------------------------------------------------------
async def test_reap_continues_past_a_failed_removal_and_does_not_discard_earlier_work(
    git_repo: Path, tmp_path: Path
) -> None:
    """A `remove()` failure partway through the loop must not discard entries already reaped
    (`early`), must not prevent later dead entries from being attempted (`later`), must not be
    conflated with a deliberately-spared live owner (`live`), and must be visible rather than
    silent (`stuck` lands in `ReapResult.failed`, not swallowed)."""
    work_dir = tmp_path / "work"
    manager = WorktreeManager(repo_dir=git_repo, work_dir=work_dir, run_id=RUN_ID)
    live = await manager.create("acme-live", 1, "main")
    early = await manager.create("acme-early-dead", 1, "main")
    stuck = await manager.create("acme-stuck", 1, "main")
    later = await manager.create("acme-later-dead", 1, "main")

    runner = _FailOneRemoveRunner(fail_path=stuck.path, exit_code=-15, started=True, timed_out=True)
    reaper = WorktreeManager(repo_dir=git_repo, work_dir=work_dir, run_id=RUN_ID, runner=runner)

    result = await reaper.reap(live_names={live.name})

    assert set(result.reaped) == {early.name, later.name}, (
        "the successful removals on either side of the failure must both survive"
    )
    assert all(isinstance(f, ReapFailure) for f in result.failed)
    assert [f.name for f in result.failed] == [stuck.name]
    assert "killed at its deadline" in result.failed[0].reason
    assert result.complete is False

    assert live.path.is_dir(), "a live owner is spared, not attempted and not failed"
    assert not early.path.exists()
    assert not later.path.exists()
    assert stuck.path.exists(), "an unsettled probe must not license deleting the directory"


async def test_reap_continues_past_an_environment_fault_and_does_not_discard_earlier_work(
    git_repo: Path, tmp_path: Path
) -> None:
    """A narrower trigger for the same D44-family bug: `remove()`'s subprocess spawn itself can
    fail — a missing `git` binary, a `PermissionError` on `cwd`, or resource exhaustion —
    `_run_locked`'s `asyncio.create_subprocess_exec` call is unguarded, so that surfaces as a raw
    `OSError` rather than a `ProcResult` git never even settled. `reap()` must survive it exactly
    like the D44 unsettled-probe case above: earlier removals kept (`early`), later dead entries
    still attempted (`later`), a live owner untouched, and the failure visible in
    `ReapResult.failed` — worded so a caller can tell "the machine could not run git" apart from
    "git ran and refused" rather than collapsing both into one undifferentiated failure.
    """
    work_dir = tmp_path / "work"
    manager = WorktreeManager(repo_dir=git_repo, work_dir=work_dir, run_id=RUN_ID)
    live = await manager.create("acme-live", 1, "main")
    early = await manager.create("acme-early-dead", 1, "main")
    stuck = await manager.create("acme-stuck", 1, "main")
    later = await manager.create("acme-later-dead", 1, "main")

    runner = _FailOneRemoveWithEnvironmentFaultRunner(fail_path=stuck.path)
    reaper = WorktreeManager(repo_dir=git_repo, work_dir=work_dir, run_id=RUN_ID, runner=runner)

    result = await reaper.reap(live_names={live.name})

    assert set(result.reaped) == {early.name, later.name}, (
        "the successful removals on either side of the environment fault must both survive"
    )
    assert all(isinstance(f, ReapFailure) for f in result.failed)
    assert [f.name for f in result.failed] == [stuck.name]
    assert "environment fault" in result.failed[0].reason
    assert "not a git-level refusal" in result.failed[0].reason
    assert result.complete is False

    assert live.path.is_dir(), "a live owner is spared, not attempted and not failed"
    assert not early.path.exists()
    assert not later.path.exists()
    assert stuck.path.exists(), "an environment fault must not license deleting the directory"


class _FailCreateWithEnvironmentFaultRunner:
    """Real git for every call except `worktree add --detach <path> <ref>` whose path contains
    `fail_marker`, which raises `OSError` instead of returning a `ProcResult` — the identical
    unguarded-spawn shape `_FailOneRemoveWithEnvironmentFaultRunner` scripts for `remove()`,
    applied to `create()`'s own `git worktree add` spawn."""

    def __init__(self, *, fail_marker: str) -> None:
        self._fail_marker = fail_marker
        self.calls: list[tuple[str, ...]] = []

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
        if "add" in parts and any(self._fail_marker in p for p in parts):
            raise FileNotFoundError(2, "No such file or directory", self._fail_marker)
        return await run(list(argv), cwd=cwd, env=env, deadline=deadline, timeout_s=timeout_s)


class _AlwaysFailListRunner:
    """Real git for every call except `worktree list --porcelain`, which raises `OSError` instead
    of returning a `ProcResult` — `list_registered()`'s own unguarded git spawn, the read-path
    analogue of `_FailOneRemoveWithEnvironmentFaultRunner`."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

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
        if "list" in parts and "--porcelain" in parts:
            raise FileNotFoundError(2, "No such file or directory", "git")
        return await run(list(argv), cwd=cwd, env=env, deadline=deadline, timeout_s=timeout_s)


# --------------------------------------------------------------------------------------
# `create()` and `list_registered()` have the identical unguarded-spawn shape `remove()` was
# fixed against in `ec31b0f` — `_git_run` -> `self._runner` never wraps
# `asyncio.create_subprocess_exec` itself. Verified real for both: neither was already guarded,
# and both call sites genuinely spawn a subprocess that can fail to start.
# --------------------------------------------------------------------------------------
async def test_create_raises_worktree_error_not_a_raw_oserror_on_environment_fault(
    git_repo: Path, tmp_path: Path
) -> None:
    """A missing `git` binary (or a `PermissionError` on `cwd`, or resource exhaustion) during
    `create()`'s own `git worktree add` spawn must not escape as a raw, untyped `OSError` — every
    other failure `create()` raises is `WorktreeError` (the "already exists" guard, the exit-code
    branch), so a caller catching that family would not catch a bare `OSError`. Re-raised with the
    same environment-fault wording `remove()` uses, so it stays distinguishable from a settled
    git-level refusal rather than inventing a fourth vocabulary."""
    work_dir = tmp_path / "work"
    runner = _FailCreateWithEnvironmentFaultRunner(fail_marker="acme-widgets")
    manager = WorktreeManager(repo_dir=git_repo, work_dir=work_dir, run_id=RUN_ID, runner=runner)

    with pytest.raises(WorktreeError) as caught:
        await manager.create("acme-widgets", 1, "main")

    assert "environment fault" in str(caught.value)
    assert "not a git-level refusal" in str(caught.value)
    assert not (work_dir / sandbox_name(RUN_ID, "acme-widgets", 1)).exists()


async def test_create_still_succeeds_on_the_normal_path_after_the_guard(
    git_repo: Path, tmp_path: Path
) -> None:
    """The case that proves the guard above is not a blanket raise: `create()` against a runner
    that never raises must behave exactly as it did before the fix — same success path, same
    returned `Worktree`, no over-correction."""
    manager = WorktreeManager(repo_dir=git_repo, work_dir=tmp_path / "work", run_id=RUN_ID)

    wt = await manager.create("acme-commons", 1, "main")

    assert isinstance(wt, Worktree)
    assert wt.path.is_dir()


async def test_list_registered_raises_worktree_error_not_a_raw_oserror_on_environment_fault(
    git_repo: Path, tmp_path: Path
) -> None:
    """`list_registered()` is a READ, and its current failure mode matters more than `create()`'s:
    if an environment fault here silently collapsed into an empty list, `reap()` — which trusts
    `list_registered()`'s answer as the full set of what exists (SPEC §11.5) — would believe
    "nothing is registered" when the true fact is "I could not find out", and do nothing. Verified
    here that this is NOT what happens today or after the fix: the `OSError` from the unguarded
    `git worktree list --porcelain` spawn propagates (it is not swallowed into `[]`), so no silent
    four-state collapse exists at this layer. What the guard adds is only the type: the escaping
    exception becomes `WorktreeError` with the same environment-fault wording used everywhere else
    in this module, rather than an untyped `OSError` a caller of `create()`/`remove()` would not
    think to catch."""
    manager = WorktreeManager(
        repo_dir=git_repo,
        work_dir=tmp_path / "work",
        run_id=RUN_ID,
        runner=_AlwaysFailListRunner(),
    )

    with pytest.raises(WorktreeError) as caught:
        await manager.list_registered()

    assert "environment fault" in str(caught.value)
    assert "not a git-level refusal" in str(caught.value)


async def test_list_registered_still_succeeds_on_the_normal_path_after_the_guard(
    git_repo: Path, tmp_path: Path
) -> None:
    """Proves the guard above is not a blanket raise: against a runner that never raises,
    `list_registered()` must still return the real registered worktrees, unchanged."""
    manager = WorktreeManager(repo_dir=git_repo, work_dir=tmp_path / "work", run_id=RUN_ID)
    wt = await manager.create("acme-commons", 1, "main")

    registered = await manager.list_registered()

    assert wt.path in registered


async def test_reap_raises_rather_than_reporting_a_clean_sweep_when_it_cannot_enumerate(
    git_repo: Path, tmp_path: Path
) -> None:
    """The consequential case: `reap()` calls `list_registered()` once, outside any per-entry
    try/except (that only wraps `remove()`, for the D44-family failures recorded in
    `ReapResult.failed`). If an environment fault during enumeration produced an empty
    `ReapResult(reaped=[], failed=[])`, a caller would read that as "swept clean, nothing to
    reap" — the same four-state collapse D44 is named for, one layer up, and the more dangerous
    of the two because it is silent: a reaper that believes nothing is registered does nothing.
    Pinned here: `reap()` propagates the `WorktreeError` instead, so "confirmed nothing to reap"
    (an empty `ReapResult`) stays a different, distinguishable fact from "could not determine
    what is registered" (an exception) — and untouches everything, including a live owner, since
    it never got far enough to decide anything."""
    work_dir = tmp_path / "work"
    live = await WorktreeManager(repo_dir=git_repo, work_dir=work_dir, run_id=RUN_ID).create(
        "acme-live", 1, "main"
    )
    reaper = WorktreeManager(
        repo_dir=git_repo, work_dir=work_dir, run_id=RUN_ID, runner=_AlwaysFailListRunner()
    )

    with pytest.raises(WorktreeError) as caught:
        await reaper.reap(live_names={live.name})

    assert "environment fault" in str(caught.value)
    assert "not a git-level refusal" in str(caught.value)
    assert live.path.is_dir(), "reap() must not have touched anything it never enumerated"


async def test_reap_is_reported_complete_when_every_entry_succeeds(
    git_repo: Path, tmp_path: Path
) -> None:
    """Regression guard for `ReapResult.complete`: with no failures, it must read True — the
    inverse of the partial case above — so a caller can tell the two apart without inspecting
    `failed` by hand."""
    manager = WorktreeManager(repo_dir=git_repo, work_dir=tmp_path / "work", run_id=RUN_ID)
    dead = await manager.create("acme-dead", 1, "main")

    result = await manager.reap(live_names=set())

    assert result == ReapResult(reaped=[dead.name], failed=[])
    assert result.complete is True


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
    """`fleet resume` step 2, containers half: same liveness rule as worktrees.

    Also the "no over-correction" negative control for the reporting-collapse fix below: a clean
    sweep with no `docker rm` failures must still report the removed container in `reaped`,
    `failed` empty, `complete` True — exactly what a caller of the OLD bare-`list[str]` `reap()`
    saw, just now spelled through `ContainerReapResult` instead of an unqualified list.
    """
    live = sandbox_name(RUN_ID, "acme-commons", 1)
    dead = sandbox_name(RUN_ID, "acme-widgets", 1)
    runner = FakeRunner(stdout=f"{live}\n{dead}\n")
    sandbox = ContainerSandbox(runner=runner)

    result = await sandbox.reap(run_id=RUN_ID, live_names={live})

    assert isinstance(result, ContainerReapResult)
    assert result.reaped == [dead]
    assert result.failed == []
    assert result.complete is True
    listing = runner.calls[0]
    # The prefix is `re.escape`d before it reaches docker's `--filter` (see
    # `test_list_by_prefix_escapes_dots_so_a_sibling_repo_is_not_cross_matched`), so the argv
    # under test carries the ESCAPED form, not the raw string `run_prefix` returns.
    assert f"name=^{re.escape(run_prefix(RUN_ID))}" in listing
    assert "--all" in listing
    assert ("docker", "rm", "--force", dead) in runner.calls
    assert ("docker", "rm", "--force", live) not in runner.calls


class _ContainerRemoveScriptRunner:
    """`docker ps --all --filter ...` answers with a fixed, scripted listing. `docker rm --force
    <fail_name>` is answered either with a scripted docker-level refusal (non-zero exit) or by
    RAISING `OSError` (an environment fault — `docker` never even ran), depending on
    `raise_os_error`. Every other call — including `rm --force` for any other name — succeeds,
    mirroring `FakeRunner`.

    This is what lets a test drive `ContainerSandbox.reap()`'s loop with exactly one scripted
    failure among several real-looking removals, the same shape as `worktree.py`'s
    `_FailOneRemoveRunner`.
    """

    def __init__(
        self, *, listing: str, fail_name: str, exit_code: int = 1, raise_os_error: bool = False
    ) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._listing = listing
        self._fail_name = fail_name
        self._exit_code = exit_code
        self._raise_os_error = raise_os_error

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
                exit_code=0,
                stdout_tail=self._listing,
                stderr_tail="",
                duration_ms=1,
                timed_out=False,
            )
        if parts[-1] == self._fail_name:
            if self._raise_os_error:
                raise FileNotFoundError(2, "No such file or directory", "docker")
            return ProcResult(
                argv=parts,
                exit_code=self._exit_code,
                stdout_tail="",
                stderr_tail="Error: cannot remove container: still running",
                duration_ms=1,
                timed_out=False,
            )
        return ProcResult(
            argv=parts, exit_code=0, stdout_tail="", stderr_tail="", duration_ms=1, timed_out=False
        )


# --------------------------------------------------------------------------------------
# `ContainerSandbox.reap()` used to append every candidate to `reaped` unconditionally,
# ignoring `remove()`'s own bool verdict: a `docker rm` that failed was reported reaped while the
# container still existed — a verdict the code never established (the "four-state collapse"
# family, docs/INTEGRATION_HONESTY.md D29/D34-D45) and directly adjacent to D32, the open
# container-leak defect: a caller told "these were reaped" had no way to know a leak remained.
# --------------------------------------------------------------------------------------
async def test_reap_does_not_report_a_failed_removal_as_reaped_and_continues_the_sweep() -> None:
    """A failed `docker rm --force` mid-sweep must not land in `reaped` — that would report a
    container as gone while `docker ps` would still show it running, the exact leak this fixes.
    Earlier and later successes in the SAME sweep must both survive the failure in between."""
    early = sandbox_name(RUN_ID, "acme-early", 1)
    stuck = sandbox_name(RUN_ID, "acme-stuck", 1)
    later = sandbox_name(RUN_ID, "acme-later", 1)
    runner = _ContainerRemoveScriptRunner(
        listing=f"{early}\n{stuck}\n{later}\n", fail_name=stuck, exit_code=1
    )
    sandbox = ContainerSandbox(runner=runner)

    result = await sandbox.reap(run_id=RUN_ID, live_names=set())

    assert set(result.reaped) == {early, later}, (
        "a mid-sweep failure must not discard the successful removal on either side of it"
    )
    assert [f.name for f in result.failed] == [stuck]
    assert isinstance(result.failed[0], ContainerReapFailure)
    assert "failed (exit 1)" in result.failed[0].reason
    assert "still running" in result.failed[0].reason
    assert result.complete is False
    # The sweep continued: every dead name was attempted, in spite of the failure in the middle.
    assert ("docker", "rm", "--force", early) in runner.calls
    assert ("docker", "rm", "--force", stuck) in runner.calls
    assert ("docker", "rm", "--force", later) in runner.calls


async def test_reap_distinguishes_an_environment_fault_from_a_docker_level_refusal() -> None:
    """`_remove_with_reason`'s `OSError` guard (D38, mirroring `WorktreeManager.remove`'s
    `ec31b0f` fix): a missing `docker` binary — `docker` never even ran — is a materially
    different fact from `docker` looking at a container and refusing to remove it. Both must
    leave the sweep able to continue, and the two reasons in `ContainerReapFailure.reason` must
    stay distinguishable rather than collapsing into one "it failed" bucket."""
    env_fault = sandbox_name(RUN_ID, "acme-envfault", 1)
    refused = sandbox_name(RUN_ID, "acme-refused", 1)
    survives = sandbox_name(RUN_ID, "acme-survives", 1)

    class _MixedFailureRunner:
        def __init__(self) -> None:
            self.calls: list[tuple[str, ...]] = []

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
                    exit_code=0,
                    stdout_tail=f"{env_fault}\n{refused}\n{survives}\n",
                    stderr_tail="",
                    duration_ms=1,
                    timed_out=False,
                )
            if parts[-1] == env_fault:
                raise FileNotFoundError(2, "No such file or directory", "docker")
            if parts[-1] == refused:
                return ProcResult(
                    argv=parts,
                    exit_code=1,
                    stdout_tail="",
                    stderr_tail="Error: cannot remove container: still running",
                    duration_ms=1,
                    timed_out=False,
                )
            return ProcResult(
                argv=parts,
                exit_code=0,
                stdout_tail="",
                stderr_tail="",
                duration_ms=1,
                timed_out=False,
            )

    runner = _MixedFailureRunner()
    sandbox = ContainerSandbox(runner=runner)

    result = await sandbox.reap(run_id=RUN_ID, live_names=set())

    assert set(result.reaped) == {survives}
    reasons = {f.name: f.reason for f in result.failed}
    assert set(reasons) == {env_fault, refused}
    assert "environment fault" in reasons[env_fault]
    assert "not a docker-level refusal" in reasons[env_fault]
    assert "FileNotFoundError" in reasons[env_fault]
    assert "environment fault" not in reasons[refused]
    assert "failed (exit 1)" in reasons[refused]
    assert result.complete is False


async def test_remove_does_not_raise_when_docker_is_unreachable() -> None:
    """`remove()`'s own contract stays bool / never-raises (its docstring: "already gone is not
    an error") even after closing the D38 `OSError` gap: `run()`'s `finally` and the
    fire-and-forget cleanup in `rdepverify.on_cancel`/`buildverify._sweep_containers` call
    `remove()` without expecting an exception, so the spawn guard must resolve to `False`, not
    propagate."""

    class _MissingDockerRunner:
        def __init__(self) -> None:
            self.calls: list[tuple[str, ...]] = []

        async def __call__(
            self,
            argv: Sequence[str],
            *,
            cwd: Path | None = None,
            env: Mapping[str, str] | None = None,
            deadline: float | None = None,
            timeout_s: float | None = None,
        ) -> ProcResult:
            self.calls.append(tuple(argv))
            raise FileNotFoundError(2, "No such file or directory", "docker")

    sandbox = ContainerSandbox(runner=_MissingDockerRunner())

    removed = await sandbox.remove("fleet-some-container")

    assert removed is False


class _ListingFailsRunner:
    """`docker ps` answers with a scripted FAILURE; every other call succeeds. The read-path
    counterpart of `_ContainerRemoveScriptRunner`, and the container analogue of `worktree.py`'s
    `_AlwaysFailListRunner` — except that a failed `docker ps` is a settled non-zero exit, not an
    `OSError`, which is exactly the state `list_by_prefix` used to collapse into `[]`."""

    def __init__(self, *, exit_code: int = 1, stderr: str = "") -> None:
        self.calls: list[tuple[str, ...]] = []
        self._exit_code = exit_code
        self._stderr = stderr

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
                exit_code=self._exit_code,
                stdout_tail="",
                stderr_tail=self._stderr,
                duration_ms=1,
                timed_out=False,
            )
        return ProcResult(
            argv=parts, exit_code=0, stdout_tail="", stderr_tail="", duration_ms=1, timed_out=False
        )


# --------------------------------------------------------------------------------------
# `ContainerSandbox.list_by_prefix` returned `[]` on a non-zero `docker ps`
# (docs/INTEGRATION_HONESTY.md D73), so `reap()` handed its caller an empty
# `ContainerReapResult` — indistinguishable from "this run has no containers", which
# `cli._reap_lines` prints as `no orphan containers`. The D44 collapse on the READ side, and the
# silent one: a reaper that believes there is nothing to reap does nothing and says it is clean.
#
# WHAT THESE TESTS MEASURE, and why it is not the obvious quantity. A test that measured removals
# — `result.reaped`, or the `docker rm --force` argv in `runner.calls` — CANNOT observe this
# defect: a failed listing produces zero removals both before and after the fix, so the number it
# reads is 0 either way. Both are asserted below as the control, and the discriminating
# assertions are on `failed`/`complete`, which is where the behaviour actually moves.
# --------------------------------------------------------------------------------------
async def test_reap_reports_a_failed_listing_instead_of_a_clean_empty_sweep() -> None:
    """A `docker ps` that exits non-zero — a stopped daemon, a permission error on the socket —
    must not be reported as a sweep that found nothing. `complete` is what a caller reads to tell
    the two apart, and the reason carries docker's own stderr because the operator's next action
    (start the daemon, then re-run) depends on which failure it was."""
    runner = _ListingFailsRunner(
        exit_code=1,
        stderr="Cannot connect to the Docker daemon at unix:///var/run/docker.sock.",
    )
    sandbox = ContainerSandbox(runner=runner)

    result = await sandbox.reap(run_id=RUN_ID, live_names=set())

    # The control: neither of these moves under the defect. A test built on them is blind to it.
    assert result.reaped == []
    assert not any(call[1] == "rm" for call in runner.calls), (
        "nothing may be removed on the strength of an inventory docker never provided"
    )
    # The discriminating assertions.
    assert result.complete is False, (
        "an unreadable inventory reported as a complete sweep is the whole defect"
    )
    assert [f.name for f in result.failed] == [f"{run_prefix(RUN_ID)}*"], (
        "the entry names the namespace the sweep could not enumerate; there is no container name "
        "to report, because none was ever learned"
    )
    assert "failed (exit 1)" in result.failed[0].reason
    assert "Cannot connect to the Docker daemon" in result.failed[0].reason


async def test_reap_reports_a_listing_that_never_settled_rather_than_an_empty_inventory() -> None:
    """The second of the three states `not result.ok` collapses (ADR-0067 part 4): a `docker ps`
    that never started because the deadline had already passed. `run()` synthesises that as
    `started=False` AND `timed_out=True` at once, so `no_verdict` must be asked BEFORE `ok` and
    must read `started` first — otherwise a command that never ran is reported as one that ran
    too long, and either way an inventory nobody obtained becomes an empty one."""
    runner = FakeRunner(exit_code=124, started=False, timed_out=True)
    sandbox = ContainerSandbox(runner=runner)

    result = await sandbox.reap(run_id=RUN_ID, live_names=set())

    assert result.complete is False
    assert [f.name for f in result.failed] == [f"{run_prefix(RUN_ID)}*"]
    assert "did not settle" in result.failed[0].reason
    assert "never started" in result.failed[0].reason, (
        "the never-started cause must not be reported as a timeout"
    )
    assert "failed (exit" not in result.failed[0].reason, (
        "a call that never ran is not a docker-level refusal"
    )


async def test_reap_reports_a_real_empty_inventory_as_a_clean_complete_sweep() -> None:
    """The no-over-correction control, and the reason the fix above is not simply "report a
    failure whenever nothing was removed": a `docker ps` that exits 0 with no output is a real
    answer — this run has no containers — and must stay a clean, complete sweep with an empty
    `failed`. This test passes before the fix and after it; it is the one that would keep passing
    under a mutation that removed the fix, which is why it cannot be the test that pins it."""
    sandbox = ContainerSandbox(runner=FakeRunner(stdout=""))

    result = await sandbox.reap(run_id=RUN_ID, live_names=set())

    assert result == ContainerReapResult(reaped=[], failed=[])
    assert result.complete is True


async def test_list_by_prefix_stays_the_lenient_view_and_list_with_verdict_the_honest_one() -> (
    None
):
    """Pins the residual D73 records, so it cannot be closed by accident in the wrong direction.

    `list_by_prefix` still answers `[]` for a failed listing, and its signature is unchanged,
    because `BuildverifyWorker._sweep_containers` and `cli._reap_orphan_containers`'s
    `--dry-run` branch both iterate the returned list: making this method raise would turn a
    best-effort cancellation sweep into an exception escaping `on_cancel`, and a `--dry-run`
    preview into a traceback. Closing the residual means moving those two call sites to
    `list_with_verdict` — in the modules that own them — not changing this method under them.

    Both halves are asserted together on the SAME failure, because the pair is the contract: the
    honest answer exists and is reachable, and the lenient one is lenient on purpose.
    """
    runner = _ListingFailsRunner(exit_code=1, stderr="permission denied on /var/run/docker.sock")
    sandbox = ContainerSandbox(runner=runner)
    prefix = run_prefix(RUN_ID)

    lenient = await sandbox.list_by_prefix(prefix)
    honest = await sandbox.list_with_verdict(prefix)

    assert lenient == []
    assert honest.names == []
    assert honest.error is not None
    assert "permission denied" in honest.error


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
# `reap()` spares by OWNERSHIP, not equality — a live container's name carries a per-call token
#
# `live_names` can only ever carry SANDBOX names: `sandbox_name(run_id, repo, attempt)` is what a
# caller derives from a live `phases` row. No container is named that. `_invocation_name`
# (`workers/buildverify.py`) appends `-t<8 hex>` (+ an optional `-cc-probe`) to every `docker
# run --name=` it issues, so under an equality test the live set matches NOTHING that is running
# and the sweep force-removes the build it exists to spare. MEASURED against the pre-fix code:
# `live_names={fleet-<run>-acme-commons-1}` over a listing containing
# `fleet-<run>-acme-commons-1-tde39ce31` issued `docker rm --force` for that exact name.
# --------------------------------------------------------------------------------------
_FIXED_HEX = "a1b2c3d4"
"""A stand-in for `uuid.uuid4().hex[:8]`, fixed so a failure is reproducible. The token's VALUE
is irrelevant to every assertion below — what matters is that one exists at all."""


def _live_sandbox_name() -> str:
    """The name a live `phases` row yields — the only shape a `reap()` caller can supply."""
    return sandbox_name(RUN_ID, "acme/commons", 1)


async def test_reap_spares_a_live_container_whose_name_carries_a_per_call_token() -> None:
    """The defect: `reap()` decided liveness by `name in live`, and no live container's name is
    ever IN that set — `BuildverifyWorker` names them `<sandbox_name>-t<token>[-suffix]`.

    This is the discriminating case for the fix. Under the old equality predicate the two live
    names below are absent from `live` and are force-removed mid-build; under `claims()` they are
    spared. The two orphans in the same listing must still be reaped, or "spare the live one"
    would have been bought by reaping nothing.

    `attempt_10_orphan` is the boundary, and it is the reason `claims()` requires a `-`
    separator rather than a bare `startswith`. Attempt 1 and attempt 10 are different rungs;
    `fleet-<run>-acme-commons-1` is a bare-character prefix of `fleet-<run>-acme-commons-10-t...`,
    so a looser predicate would let a live attempt 1 spare attempt 10's orphans for the life of
    the run — a silent leak dressed as liveness.
    """
    live_row = _live_sandbox_name()
    live_build = f"{live_row}-t{_FIXED_HEX}"
    live_probe = f"{live_row}-t{_FIXED_HEX}-cc-probe"
    other_repo_orphan = f"{sandbox_name(RUN_ID, 'acme/widgets', 1)}-t{'d' * 8}"
    attempt_10_orphan = f"{sandbox_name(RUN_ID, 'acme/commons', 10)}-t{'e' * 8}"
    runner = RegexFilterRunner([live_build, live_probe, other_repo_orphan, attempt_10_orphan])

    result = await ContainerSandbox(runner=runner).reap(run_id=RUN_ID, live_names={live_row})

    assert live_build not in runner.removed, (
        "a running build's container was force-removed: its name extends the live sandbox name "
        "by buildverify's per-call token, which an equality test can never match"
    )
    assert live_probe not in runner.removed, (
        "the C-toolchain probe of a live rung was force-removed; the `-cc-probe` suffix is a "
        "further `-` segment under the SAME live sandbox, not a different owner"
    )
    assert set(runner.removed) == {other_repo_orphan, attempt_10_orphan}
    assert set(result.reaped) == {other_repo_orphan, attempt_10_orphan}
    assert result.failed == []
    assert result.complete is True


async def test_claims_spares_a_slug_colliding_repo_as_a_stated_boundary() -> None:
    """The repo segment is NOT bounded by the `-` requirement, and this pins that as a boundary.

    `slug` maps `/` to `-`, so `acme/commons` and `acme/commons/1` produce sandbox names where the
    first is a `-`-delimited prefix of the second. A live `acme/commons` attempt 1 therefore
    spares every container of repo `acme/commons/1` attempt 2 — a different repo AND a different
    attempt — for as long as that rung is live.

    This asserts the CURRENT, spare-too-much behaviour on purpose. It is not a wish: the only
    tightening available is to match buildverify's `-t<hex>` token, which `claims()` refuses so
    that the first worker naming its containers any other way is not force-removed mid-build.
    Trading a bounded over-spare (a leak the next idempotent `reap()` clears once the rung ends)
    for the one error no later sweep can undo is the wrong trade, so the boundary is documented
    in `claims()` and executable here. A future author who "fixes" this flips the assertion and
    has to read why first.
    """
    live = sandbox_name(RUN_ID, "acme/commons", 1)
    nested_repo_orphan = f"{sandbox_name(RUN_ID, 'acme/commons/1', 2)}-t{'d' * 8}"

    assert live == f"{run_prefix(RUN_ID)}acme-commons-1"
    assert nested_repo_orphan == f"{run_prefix(RUN_ID)}acme-commons-1-2-t{'d' * 8}", (
        "the collision is in `slug`, not in `claims`: both repo ids flatten to the same "
        "`-`-delimited stem, and that is what makes one name extend the other"
    )
    assert claims(live, nested_repo_orphan) is True, (
        "stated boundary, not an aspiration — see `claims()`'s docstring before changing this"
    )
    # And the property the `-` requirement DOES buy, asserted alongside so the boundary above is
    # read as a scope limit rather than as the predicate being useless.
    assert claims(live, f"{sandbox_name(RUN_ID, 'acme/commons', 10)}-t{'e' * 8}") is False


async def test_reap_never_removes_a_container_outside_the_runs_own_namespace() -> None:
    """The hard safety property, proven rather than argued: a `reap()` for run A can never issue
    `docker rm --force` for anything that is not `fleet-<run A>-*`.

    The listing is deliberately supplied by a runner that IGNORES the `--filter` argument and
    answers with everything it holds. That is not a hypothetical: `list_by_prefix` delegates the
    match to the DAEMON's regex engine, and a daemon quirk, a dropped `^` anchor, or a lost
    `re.escape` all reach `reap()` as an over-wide listing — which the pre-fix loop would have
    force-removed name by name. The floor must therefore live in `reap()` itself, checked against
    a prefix this process built.

    `wt-WT1-example` is in the pool on purpose: `docs/DECISIONS.md` quotes THAT worktree's own
    `git rev-parse --absolute-git-dir` as ADR-0074's live evidence, and it is on disk right now.
    Nothing in this sweep may name it.
    """
    live_row = _live_sandbox_name()
    mine_live = f"{live_row}-t{_FIXED_HEX}"
    mine_orphan = f"{sandbox_name(RUN_ID, 'acme/widgets', 1)}-t{'d' * 8}"
    other_run = UUID("00000000-0000-4000-8000-0000000fffff")
    outsiders = [
        f"{sandbox_name(other_run, 'acme/commons', 1)}-t{'f' * 8}",  # a CONCURRENT run's build
        "fleet-build-cache",  # `fleet`-prefixed, but not this run's
        "wt-WT1-example",  # ADR-0074's live evidence worktree
        "postgres",  # somebody else's container entirely
    ]

    class _UnfilteredRunner(RegexFilterRunner):
        """Answers `docker ps` with the whole pool, filter argument ignored — the over-wide
        listing an unanchored or unescaped daemon-side regex produces."""

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
                return ProcResult(
                    argv=parts, exit_code=0, stdout_tail="\n".join(self.names), stderr_tail="",
                    duration_ms=1, timed_out=False,
                )
            return await RegexFilterRunner.__call__(
                self, argv, cwd=cwd, env=env, deadline=deadline, timeout_s=timeout_s
            )

    runner = _UnfilteredRunner([mine_live, mine_orphan, *outsiders])

    result = await ContainerSandbox(runner=runner).reap(run_id=RUN_ID, live_names={live_row})

    for outsider in outsiders:
        assert outsider not in runner.removed, (
            f"reap() for run {RUN_ID} reached {outsider!r}, which is outside "
            f"{run_prefix(RUN_ID)!r}"
        )
        assert outsider not in result.reaped
        assert outsider not in [f.name for f in result.failed]
    assert runner.removed == [mine_orphan], (
        "the run's own orphan must still be reaped — a floor that spares everything would pass "
        "the assertions above while reaping nothing"
    )


async def test_worktree_reap_never_removes_a_checkout_outside_the_runs_namespace(
    git_repo: Path, tmp_path: Path
) -> None:
    """The same floor, one layer down, against a REAL git worktree registry.

    `wt-WT1-example` is registered in the very `work_dir` the sweep enumerates, so git's
    `worktree list --porcelain` genuinely returns it — this is not a fake declining to offer the
    name. `reap(live_names=set())` is the maximally destructive call available (nothing is live),
    and the checkout must survive it because its name does not start with `fleet-<run_id>-`.
    """
    work_dir = tmp_path / "work"
    manager = WorktreeManager(repo_dir=git_repo, work_dir=work_dir, run_id=RUN_ID)
    orphan = await manager.create("acme-widgets", 1, "main")
    outsider = work_dir / "wt-WT1-example"
    await _git(git_repo, "worktree", "add", "--detach", str(outsider), "main")
    registered = await manager.list_registered()
    assert any(p.resolve() == outsider.resolve() for p in registered), (
        "premise: git must actually enumerate the outsider, or this test proves nothing"
    )

    result = await manager.reap(live_names=set())

    assert outsider.is_dir(), f"reap() removed {outsider}, outside {run_prefix(RUN_ID)!r}"
    assert result.reaped == [orphan.name]
    assert "wt-WT1-example" not in [f.name for f in result.failed]


# --------------------------------------------------------------------------------------
# Instrument validation (CLAUDE.md guardrail 6). The instrument above is "`reap()` over a
# `RegexFilterRunner` pool, read through `runner.removed`". A detector never observed firing is
# not evidence of absence, so it is exercised three ways: it FIRES on a planted orphan, stays
# SILENT on a pool where every container is claimed, and fires AGAIN when one synthetic orphan is
# injected into that same silent pool. The third is the one that catches an instrument broken on
# fresh instances — a pool builder that quietly returns nothing would pass the first two.
# --------------------------------------------------------------------------------------
def _all_live_pool() -> tuple[list[str], set[str]]:
    """A pool in which EVERY container is claimed by one of the returned live sandbox names."""
    rows = [sandbox_name(RUN_ID, "acme/commons", 1), sandbox_name(RUN_ID, "acme/widgets", 3)]
    pool = [
        f"{rows[0]}-t{_FIXED_HEX}",
        f"{rows[0]}-t{_FIXED_HEX}-cc-probe",
        f"{rows[1]}-t{'b' * 8}",
    ]
    return pool, set(rows)


async def test_instrument_fires_on_a_planted_orphan() -> None:
    """Validation 1: the detector reports a removal when one is due."""
    orphan = f"{sandbox_name(RUN_ID, 'acme/widgets', 1)}-t{'d' * 8}"
    runner = RegexFilterRunner([orphan])

    result = await ContainerSandbox(runner=runner).reap(run_id=RUN_ID, live_names=set())

    assert runner.removed == [orphan]
    assert result.reaped == [orphan]


async def test_instrument_is_silent_on_an_all_live_pool() -> None:
    """Validation 2: the detector reports nothing when nothing is due."""
    pool, live = _all_live_pool()
    runner = RegexFilterRunner(pool)

    result = await ContainerSandbox(runner=runner).reap(run_id=RUN_ID, live_names=live)

    assert runner.removed == []
    assert result.reaped == []
    assert result.failed == []


async def test_instrument_fires_on_a_synthetic_orphan_injected_into_the_all_live_pool() -> None:
    """Validation 3, the load-bearing one: the SAME fixture that produced silence above, plus one
    synthetic orphan, must fire — and fire only on the injected name.

    A `_all_live_pool()` that had silently degenerated (an empty listing, a runner that never
    answers `docker ps`) would satisfy validations 1 and 2 and still be measuring nothing. Here
    the silent pool is rebuilt, perturbed by exactly one name, and the detector has to
    distinguish it from its neighbours.
    """
    pool, live = _all_live_pool()
    injected = f"{sandbox_name(RUN_ID, 'acme/commons', 2)}-t{'c' * 8}"
    assert injected not in pool
    runner = RegexFilterRunner([*pool, injected])

    result = await ContainerSandbox(runner=runner).reap(run_id=RUN_ID, live_names=live)

    assert runner.removed == [injected]
    assert result.reaped == [injected]
    for claimed in pool:
        assert claimed not in runner.removed


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
