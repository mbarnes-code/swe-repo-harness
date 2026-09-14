"""`fleet.vcs`: git is the record of code state (SPEC §3.2 step 6, §3.3 step 1, §3.4 steps 4-5).

Almost everything here runs against a **real temp git repository**. `git` is a hard dependency of
the harness, and the properties under test — that a trailer survives a commit, that a scoped range
excludes older history, that `git apply --check --reverse` distinguishes applied from not-applied,
that `reset --hard` onto a task anchor preserves a sibling task's commit — are properties of git
itself. A mock would only prove we can spell the flags.

Mocks (an injected `CommandRunner`) carry the bulk of the `git-filter-repo` and `gh` coverage,
because command construction and reply parsing are what can be wrong there and neither needs a
binary. Both binaries ARE installed here now (`.venv/bin/git-filter-repo`, `tools/bin/gh`; PATH
wired by `tests/conftest.py`) and the tests at the bottom of each section really run them — but
they are not equally strong, and the difference is the point:

* `git-filter-repo` is proven END TO END. A real multi-commit history is rewritten, and the
  assertions are on the ROOT commit's tree and on `git log --follow`, so a copy-the-tree
  implementation cannot pass. Re-runnability under ADR-0014's retry ladder is proven too.
* `gh` is proven only as far as this host can honestly go: the binary exists, runs, and reports
  a 2.x version, and `available()` converts an unauthenticated exit into `False` rather than an
  exception. There is no GitHub credential here and nothing is created on github.com, so
  `create_pr` and live `pr view` remain proven only against recorded JSON.

`docs/INTEGRATION_HONESTY.md` is the full ledger.
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import pytest

from fleet.models.enums import PrState
from fleet.util.proc import CommandRunner, ProcResult, is_producible_shape, run
from fleet.vcs import commits as C
from fleet.vcs import filter_repo as FR
from fleet.vcs import github as GH
from fleet.vcs.git import DiffStat, Git, GitCommandError, GitRefError

RUN_ID = UUID("00000000-0000-4000-8000-0000000000ff")
BRANCH = "migrate/acme-billing"
REPO_ID = "acme/billing"

#: A mirror remote's origin URL in this environment embeds a plaintext PAT (CLAUDE.md); the
#: redaction contract that keeps it out of every egress is SPEC §11.4.
FAKE_PAT = "github_pat_11ABCDEFG0aBcDeFgHiJkLmNoPqRsTuVwXyZ012345"
PAT_URL = f"https://oauth2:{FAKE_PAT}@git.example.invalid/acme/billing.git"


# --------------------------------------------------------------------------------------
# fixtures and helpers — real repositories
# --------------------------------------------------------------------------------------
async def _sh(cwd: Path, *args: str) -> ProcResult:
    return await run(["git", *args], cwd=cwd, timeout_s=60)


def _write_files(path: Path, files: Mapping[str, str]) -> None:
    """Sync on purpose: blocking file IO belongs outside the coroutines under test."""
    path.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)


def _swap_text(target: Path, new_text: str) -> str:
    """Write `new_text`, returning what was there. Sync for the same reason as `_write_files`."""
    original = target.read_text()
    target.write_text(new_text)
    return original


async def _git_out(cwd: Path, *args: str) -> str:
    """`_sh` plus "the command actually worked". A `git log` that exited non-zero returns empty
    stdout, and an assertion over empty stdout passes for all the wrong reasons."""
    result = await _sh(cwd, *args)
    assert result.exit_code == 0, f"git {' '.join(args)} → {result.exit_code}: {result.stderr_tail}"
    return result.stdout_tail


async def _init_repo(path: Path, *, files: Mapping[str, str]) -> None:
    _write_files(path, {})
    await _sh(path, "init", "--initial-branch=main", ".")
    await _sh(path, "config", "user.email", "fleet@example.invalid")
    await _sh(path, "config", "user.name", "Fleet Test")
    _write_files(path, files)
    await _sh(path, "add", "--all")
    await _sh(path, "commit", "-m", "initial")


async def _write_and_commit(path: Path, name: str, text: str, subject: str) -> None:
    _write_files(path, {name: text})
    await _sh(path, "add", "--all")
    await _sh(path, "commit", "-m", subject)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo with `migrate/<repo>` checked out — the Phase 2 worktree shape."""

    async def build() -> Path:
        path = tmp_path / "repo"
        await _init_repo(path, files={"a.txt": "v1\n", "b.txt": "keep\n"})
        await _sh(path, "checkout", "-b", BRANCH)
        return path

    return asyncio.run(build())


@pytest.fixture
def git(repo: Path) -> Git:
    return Git(repo, timeout_s=60)


async def build_patch(git_: Git, rel: str, new_text: str, patch_path: Path) -> Path:
    """Produce a real unified diff for `rel` -> `new_text`, leaving the tree unchanged.

    Generated by git rather than hand-written so the tests exercise the same patch dialect the
    transform engine emits.
    """
    target = git_.path / rel
    original = _swap_text(target, new_text)
    diff = await git_.text(["diff", "--", rel])
    _write_files(patch_path.parent, {patch_path.name: diff + "\n"})
    _swap_text(target, original)
    return patch_path


@dataclass(frozen=True, slots=True)
class Patch:
    """Minimal `PatchLike`: `models.tasks.FilePatch` has the same two attributes."""

    path: str
    diff: str


def trailers_for(patch_id_value: str, *, task_id: str = "11111111-1111-4111-8111-111111111111",
                 attempt: int = 1) -> C.FleetTrailers:
    return C.FleetTrailers(
        run_id=RUN_ID,
        repo_id=REPO_ID,
        phase=2,
        task_id=task_id,
        attempt=attempt,
        patch_id=patch_id_value,
    )


class ScriptedRunner:
    """`CommandRunner` that replays canned results and records every argv it was handed.

    The seam that makes the `gh`/`git-filter-repo` boundaries testable with neither binary nor
    network present (SPEC §7.1, CLAUDE.md guardrail 3).

    `timed_out` and `started` are both constructible, because the three ways a call can fail to
    produce a verdict are not interchangeable and `ProcResult.ok` is false for all of them:
    `started=False, timed_out=True, exit_code=124` is the passed-deadline result `util.proc.run`
    synthesises without spawning anything, `timed_out=True` alone is a process killed at its
    deadline, and a plain non-zero `exit_code` is a command that ran and answered. A fake that
    can only build the third makes the first two untestable — which is how "not ok" keeps being
    read as "the repo said no".

    **Anti-drift, enforced at construction.** `util.proc.is_producible_shape` is the invariant
    read off `util/proc.py`'s only producer of a real `ProcResult`; the constructor below checks
    every instance against it. Without this, a test could build `started=False, timed_out=False`
    — a state `run()` can never return, because its one `started=False` branch always sets
    `timed_out=True` and `exit_code=TIMEOUT_EXIT_CODE` together — and pass by asserting against
    evidence no real invocation could produce. See
    `tests/test_proc.py::test_scripted_runner_cannot_construct_states_the_real_runner_cannot_produce`.
    """

    def __init__(
        self,
        *,
        stdout: str = "",
        exit_code: int = 0,
        started: bool = True,
        timed_out: bool = False,
        stderr: str = "",
    ) -> None:
        if not is_producible_shape(started=started, timed_out=timed_out, exit_code=exit_code):
            raise ValueError(
                f"ScriptedRunner(started={started}, timed_out={timed_out}, exit_code={exit_code}) "
                "is a state util.proc.run can never produce: started=False is synthesised in "
                "exactly one place, and it always pairs with timed_out=True and "
                "exit_code=TIMEOUT_EXIT_CODE (§7.1) — a test built against any other pairing "
                "would pass against a ProcResult no real invocation can generate"
            )
        self.calls: list[tuple[str, ...]] = []
        self.stdout = stdout
        self.exit_code = exit_code
        self.started = started
        self.timed_out = timed_out
        self.stderr = stderr

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
            stderr_tail=self.stderr,
            duration_ms=1,
            timed_out=self.timed_out,
            started=self.started,
            cwd=cwd,
        )


class DeadlineMidRollbackRunner:
    """Real git for every call except argv shapes named in `unsettle`, which come back as an
    unsettled `ProcResult` — the same shape `util.proc.run` synthesises for a deadline that had
    already passed (`started=False, timed_out=True, exit_code=124`, §7.1). Models an expired
    deadline landing between two of `discard_task`'s own git calls: the interaction D42 made
    reachable, since `land_patches` calls `discard_task` on the SAME deadline that failed the
    original patch.
    """

    def __init__(self, *, unsettle: Sequence[str], real: CommandRunner = run) -> None:
        self._unsettle = tuple(unsettle)
        self._real = real

    async def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        deadline: float | None = None,
        timeout_s: float | None = None,
    ) -> ProcResult:
        if all(token in argv for token in self._unsettle):
            return ProcResult(
                argv=tuple(argv),
                exit_code=124,
                stdout_tail="",
                stderr_tail="",
                duration_ms=1,
                timed_out=True,
                started=False,
                cwd=cwd,
            )
        return await self._real(argv, cwd=cwd, env=env, deadline=deadline, timeout_s=timeout_s)


class RefuseOneCommandRunner:
    """Real git for every call except argv shapes named in `refuse`, which come back as a
    SETTLED, genuine, non-zero exit — the "git actually ran and said no" case, as opposed to
    `DeadlineMidRollbackRunner`'s "git never got to answer" case. Used to prove the two are
    reported distinguishably.
    """

    def __init__(self, *, refuse: Sequence[str], real: CommandRunner = run) -> None:
        self._refuse = tuple(refuse)
        self._real = real

    async def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        deadline: float | None = None,
        timeout_s: float | None = None,
    ) -> ProcResult:
        if all(token in argv for token in self._refuse):
            return ProcResult(
                argv=tuple(argv),
                exit_code=1,
                stdout_tail="",
                stderr_tail="fatal: refused",
                duration_ms=1,
                timed_out=False,
                started=True,
                cwd=cwd,
            )
        return await self._real(argv, cwd=cwd, env=env, deadline=deadline, timeout_s=timeout_s)


class RaisingRunner:
    """A `CommandRunner` that raises instead of returning a `ProcResult` — what a genuinely
    missing binary looks like at this seam.

    No shell is ever involved (`util/proc.py`'s module docstring), so a binary absent from PATH
    is never a `ProcResult` with some sentinel `exit_code`: `asyncio.create_subprocess_exec`
    raises `FileNotFoundError` in the PARENT, before any process exists to report an exit code.
    `ScriptedRunner(exit_code=127, ...)` therefore cannot stand in for this case — nothing
    produces that `ProcResult` for real — and this double exists so `github.py`/`gitea.py`/
    `filter_repo.py`'s `except FileNotFoundError` branches are exercised against the actual
    failure mode instead of an invented exit code.
    """

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    async def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        deadline: float | None = None,
        timeout_s: float | None = None,
    ) -> ProcResult:
        raise self.exc


# --------------------------------------------------------------------------------------
# git.py — the typed surface
# --------------------------------------------------------------------------------------
async def test_ref_reads_are_typed_and_a_missing_ref_is_loud(git: Git) -> None:
    """`resolve` may legitimately miss ("has the anchor been created yet?"); `rev_parse` may not.

    Collapsing the two into "returns '' on failure" is how a rollback silently resets to nothing.
    """
    head = await git.rev_parse("HEAD")
    assert len(head) == 40
    assert await git.resolve("refs/fleet/nope") is None
    assert await git.ref_exists(f"refs/heads/{BRANCH}") is True
    with pytest.raises(GitRefError):
        await git.rev_parse("refs/fleet/nope")


async def test_a_ref_read_that_never_ran_is_not_a_missing_ref(tmp_path: Path) -> None:
    """`resolve()` USED TO answer `None` for a rev-parse that was never started, exactly as it
    does for a ref that is genuinely absent — that collapse was D42. `util.proc.run` reports a
    deadline that passed before the call as `started=False` AND `timed_out=True` AND
    `exit_code=124`, all at once, and `ProcResult.ok` is `started and not timed_out and
    exit_code == 0` — false for a missing ref and false for a call that never happened.
    `workers/clone.py` read that `None` as an `EmptyRepo` finding and returned `status="ok"`
    beside it. The fix makes `resolve()` raise `GitCommandError` instead of returning `None` for
    an unsettled `ProcResult`, so only a rev-parse that ACTUALLY ran and said "no such rev" may
    mean "does not exist" — the distinguishing facts are pinned here, at the seam.
    """
    never_ran = ScriptedRunner(exit_code=124, started=False, timed_out=True)
    git_ = Git(tmp_path, runner=never_ran)

    with pytest.raises(GitCommandError) as never_resolved:
        await git_.resolve("refs/heads/main")
    assert never_resolved.value.started is False
    assert never_resolved.value.timed_out is True

    probe = await git_.exec(["rev-parse", "--verify", "--quiet", "main"], check=False)
    assert probe.ok is False
    assert (probe.started, probe.timed_out) == (False, True)  # the evidence `resolve` now raises on

    with pytest.raises(GitCommandError) as never_started:
        await git_.exec(["rev-parse", "main"])
    assert never_started.value.timed_out is True
    assert "exit 124" not in str(never_started.value), (
        "a command that was never spawned was reported as a process that exited 124"
    )
    assert never_started.value.started is False, (
        "the exception carried `timed_out` but dropped `started`, which is the fact that "
        "separates a measurement nobody took from one that ran too long — `workers/base."
        "clock_failure` reads both, and a rung is charged for the second and not the first"
    )

    killed = Git(tmp_path, runner=ScriptedRunner(exit_code=-9, started=True, timed_out=True))
    with pytest.raises(GitCommandError) as at_deadline:
        await killed.exec(["fetch", "--unshallow"])
    assert at_deadline.value.timed_out is True
    assert at_deadline.value.started is True  # this one really did run; the kill is substantive
    assert "exit -9" not in str(at_deadline.value)


async def test_diff_stat_is_structured_not_a_string(git: Git) -> None:
    """Phase 2's success criterion is "`git diff --stat` … is non-empty" — a decision, so it must
    read a field, not regex a human-readable summary."""
    assert (await git.diff_stat()).is_empty
    (git.path / "a.txt").write_text("v1\nv2\n")
    stat = await git.diff_stat()
    assert isinstance(stat, DiffStat)
    assert stat.paths == ("a.txt",)
    assert stat.insertions == 1
    assert not stat.is_empty


async def test_diff_stat_resolves_a_renamed_files_real_path(git: Git) -> None:
    """`git diff --numstat` compresses a rename into `common/{old => new}` (shared affix) or a
    bare `old => new` (no common affix) — not a plain path. Taking the trailing tab-separated
    field verbatim would leave `FileStat.path` a bogus compound string instead of the file's real,
    current path (the defect this test pins).

    Both forms appear ONLY once a rename is fully absorbed into one side of the comparison with
    nothing left uncommitted on top of it — a plain worktree-vs-index `diff_stat()` right after
    `git mv` never contains an arrow at all, because `git mv` itself already stages the rename
    into the index, so the remaining worktree-vs-index diff is a same-path content diff (verified
    empirically: `git diff --numstat` after a bare `git mv` prints e.g. `1\t0\trenamed.txt`, no
    `=>`). The old form of this test asserted the resolved name after exactly that sequence and so
    passed whether or not `_resolve_numstat_path` did anything at all — replacing its body with
    `return raw` left every assertion here green. Reading the arrow/brace notation for real
    requires diffing the rename ITSELF: `staged=True` against `HEAD`, with no further edit on top
    (and both renames must be staged-but-uncommitted at the moment they're read — committing one
    folds it into `HEAD` and removes it from a subsequent `staged=True` diff)."""
    (git.path / "common").mkdir()
    (git.path / "common" / "old.txt").write_text("shared\n")
    await _sh(git.path, "add", "common/old.txt")
    await _sh(git.path, "commit", "-m", "add common/old.txt")

    # Bare "old => new" form: no common prefix/suffix at all.
    await _sh(git.path, "mv", "a.txt", "renamed.txt")
    # Brace-compressed form: a shared prefix/suffix survives outside the braces.
    await _sh(git.path, "mv", "common/old.txt", "common/new.txt")

    stat = await git.diff_stat(staged=True)
    assert set(stat.paths) == {"renamed.txt", "common/new.txt"}


async def test_apply_check_reverse_distinguishes_applied_from_not_applied(
    git: Git, tmp_path: Path
) -> None:
    """Guard (b) of §3.2 step 6.1 in isolation: `--check --reverse` succeeds iff the change is
    already present in the CURRENT tree. Everything the idempotency guard claims rests on this."""
    patch = await build_patch(git, "a.txt", "v2\n", tmp_path / "p.patch")
    assert await git.apply_check(patch) is True
    assert await git.apply_check(patch, reverse=True) is False
    await git.apply(patch)
    assert await git.apply_check(patch, reverse=True) is True
    assert await git.apply_check(patch) is False


# --------------------------------------------------------------------------------------
# git.py — D42: `resolve`, `ref_exists`, `apply_check`, and `is_ancestor` are check=False
# probes whose `result.ok` collapses "the process never started", "it was killed at its
# deadline", and "it ran and genuinely answered no" into one boolean/`None`. Only the last is a
# real answer to the question each method asks; the fix raises `GitCommandError` on the first
# two rather than let a clock failure be reported as the probe's negative branch. `ScriptedRunner`
# gained a real `timed_out` (D45) precisely so these states could be pinned here.
#
# `current_branch`, `remote_url`, and `blob_at` (334edeb) are the SAME shape — `check=False` +
# `_require_settled` + a `None` negative branch — but round VIII Batch 12's re-verification found
# they had never been added to `PROBE_NAMES`: nothing exercised an unsettled `ProcResult` through
# any of the three, so 334edeb's `_require_settled()` additions to them shipped with zero
# discriminating coverage. Folded in here rather than given their own test bodies, since the
# three parametrized tests below are already generic over "what does calling the probe return".
# --------------------------------------------------------------------------------------
async def _run_probe(name: str, git_: Git) -> object:
    if name == "resolve":
        return await git_.resolve("HEAD")
    if name == "ref_exists":
        return await git_.ref_exists("refs/heads/main")
    if name == "apply_check":
        return await git_.apply_check("some.patch")
    if name == "is_ancestor":
        return await git_.is_ancestor("HEAD", "HEAD")
    if name == "current_branch":
        return await git_.current_branch()
    if name == "remote_url":
        return await git_.remote_url("origin")
    if name == "blob_at":
        return await git_.blob_at("HEAD", "some/path.txt")
    raise AssertionError(f"unknown probe {name!r}")


PROBE_NAMES = (
    "resolve",
    "ref_exists",
    "apply_check",
    "is_ancestor",
    "current_branch",
    "remote_url",
    "blob_at",
)


@pytest.mark.parametrize("probe", PROBE_NAMES)
async def test_d42_probe_never_started_raises_naming_it_never_started(
    tmp_path: Path, probe: str
) -> None:
    """`started=False` always pairs with `timed_out=True` (§7.1's passed-deadline synthesis) —
    the one shape where both flags carry a non-default value, and exactly where the started-first
    ordering matters: the raised message must say "never started", not "timed out", even though
    `timed_out` is also `True` on this result. Getting the order backwards would report a rev-parse
    that never ran as one that merely took too long."""
    git_ = Git(tmp_path, runner=ScriptedRunner(exit_code=124, started=False, timed_out=True))
    with pytest.raises(GitCommandError) as caught:
        await _run_probe(probe, git_)
    assert caught.value.started is False
    assert caught.value.timed_out is True
    assert "never started" in str(caught.value)
    assert "timed out" not in str(caught.value), "started must be checked before timed_out"


@pytest.mark.parametrize("probe", PROBE_NAMES)
async def test_d42_probe_killed_at_deadline_raises_naming_the_kill(
    tmp_path: Path, probe: str
) -> None:
    """A probe that genuinely ran (`started=True`) and was killed at the deadline is equally not
    an answer to the question it was asked: `-15` is SIGTERM's negative signal code, not a git
    verdict, and reporting it as the probe's `False`/`None` would assert a fact about history that
    was never measured."""
    git_ = Git(tmp_path, runner=ScriptedRunner(exit_code=-15, started=True, timed_out=True))
    with pytest.raises(GitCommandError) as caught:
        await _run_probe(probe, git_)
    assert caught.value.started is True
    assert caught.value.timed_out is True
    assert "timed out" in str(caught.value)
    assert "exit -15" not in str(caught.value)


@pytest.mark.parametrize("probe", PROBE_NAMES)
async def test_d42_probe_genuine_no_still_returns_the_old_answer_without_raising(
    tmp_path: Path, probe: str
) -> None:
    """The case that proves the fix is not a blanket "raise on any non-zero exit": a settled
    `started=True, timed_out=False` non-zero exit is a REAL negative answer — the repo really
    lacks the ref, the patch really does not apply, the anchor really is not an ancestor — and
    must still come back as the method's ordinary `False`/`None`, not an exception."""
    git_ = Git(tmp_path, runner=ScriptedRunner(exit_code=1, started=True, timed_out=False))
    result = await _run_probe(probe, git_)
    assert result in (False, None)


# --------------------------------------------------------------------------------------
# git.py — D94's foundational primitive: `rebase`, `abort_rebase`, `push_force_with_lease`.
# This is the git-level building block a later PR-promotion mechanism will call
# (`docs/INTEGRATION_HONESTY.md`, `## D94 —`) — no caller exists yet, nothing here is wired to
# anything. `push_force_with_lease`'s safety property is a property of git's SERVER-side lease
# check, not of argument-spelling (this file's own testing philosophy, module docstring above),
# so the discriminating test below runs against a real bare remote, never mocked.
# --------------------------------------------------------------------------------------
@pytest.fixture
def remote_and_clone(tmp_path: Path) -> tuple[Path, Path]:
    """A bare `remote.git` plus one clone with `main` checked out and pushed once — the minimum
    shape `push_force_with_lease` needs a real server-side lease check against."""

    async def build() -> tuple[Path, Path]:
        remote = tmp_path / "remote.git"
        await _sh(tmp_path, "init", "-q", "--bare", str(remote))
        clone = tmp_path / "clone"
        await _sh(tmp_path, "clone", "-q", str(remote), str(clone))
        await _sh(clone, "config", "user.email", "fleet@example.invalid")
        await _sh(clone, "config", "user.name", "Fleet Test")
        _write_files(clone, {"a.txt": "v1\n"})
        await _sh(clone, "add", "--all")
        await _sh(clone, "commit", "-m", "c1")
        await _sh(clone, "push", "-q", "origin", "main")
        return remote, clone

    return asyncio.run(build())


async def _remote_tip(tmp_path: Path, remote: Path, ref: str = "main") -> str:
    """Read a bare repo's ref directly, never through a clone's own belief about it — the
    discriminating assertion in the negative safety test below depends on this being an
    independent read."""
    return (await _git_out(tmp_path, f"--git-dir={remote}", "rev-parse", ref)).strip()


async def test_rebase_onto_a_clean_target_replays_commits_and_reports_ok(
    git: Git, tmp_path: Path
) -> None:
    """The success path: a clean rebase returns `True`, and the branch's own commit is still
    present with the main-only commit now an ancestor of it — proving history was REPLAYED onto
    the new base, not merged or dropped."""
    await _write_and_commit(git.path, "on_branch.txt", "branch work\n", "branch commit")

    await git.exec(["checkout", "main"])
    await _write_and_commit(git.path, "on_main.txt", "main work\n", "main-only commit")
    main_tip = await git.rev_parse("main")
    await git.exec(["checkout", BRANCH])

    assert await git.rebase("main") is True

    assert await git.is_ancestor(main_tip, BRANCH) is True
    replayed = await git.log(f"{main_tip}..HEAD")
    assert [c.subject for c in replayed] == ["branch commit"]


async def test_rebase_onto_a_conflicting_target_leaves_it_mid_flight_and_abort_restores_the_tip(
    git: Git,
) -> None:
    """A conflict is a settled, meaningful `False`, not an exception — and `abort_rebase()`
    returns the branch to EXACTLY its pre-rebase tip, with `REBASE_HEAD` cleared."""
    await _write_and_commit(git.path, "a.txt", "branch version\n", "branch edits a.txt")
    branch_tip = await git.rev_parse(BRANCH)

    await git.exec(["checkout", "main"])
    await _write_and_commit(git.path, "a.txt", "main version\n", "main edits a.txt")
    await git.exec(["checkout", BRANCH])

    assert await git.rebase("main") is False
    assert await git.resolve("REBASE_HEAD") is not None, "rebase must be genuinely mid-flight"

    await git.abort_rebase()

    assert await git.rev_parse(BRANCH) == branch_tip
    assert await git.resolve("REBASE_HEAD") is None


async def test_rebase_onto_an_unknown_ref_raises_rather_than_reporting_a_conflict(
    git: Git,
) -> None:
    """The discriminating case between the `raise` and `return False` branches: an unresolvable
    `onto` must not be silently read as "a conflict happened" just because both are non-zero
    exits — `REBASE_HEAD` never gets written for this failure."""
    with pytest.raises(GitCommandError) as caught:
        await git.rebase("does-not-exist")
    assert "invalid upstream" in caught.value.stderr
    assert await git.resolve("REBASE_HEAD") is None


async def test_abort_rebase_with_nothing_in_progress_raises(git: Git) -> None:
    with pytest.raises(GitCommandError):
        await git.abort_rebase()


# --------------------------------------------------------------------------------------
# D111 Leg B: the `git revert -m 1` + `Fleet-*`-trailer primitive (SPEC §3.1's hoist rollback,
# §3.2 step 6). Standalone: nothing wires this into the cycle-breaking machinery yet (Legs C/D).
# --------------------------------------------------------------------------------------
async def _merge_feature_branch(git_: Git, *, branch: str, file_name: str, feature_text: str,
                                 subject: str = "merge feature",
                                 feature_branch: str = "hoist-feature") -> str:
    """Build the `-m 1` shape: a real merge commit with two parents, on `branch`.

    `branch`'s own tip becomes parent 1 (mainline) and the new feature branch's tip becomes
    parent 2 — exactly the shape SPEC's rollback reverts ("a hoist … merged … `git revert -m 1`",
    where the integration branch is always parent 1). Returns the merge commit's SHA.

    `feature_branch` defaults to the original hardcoded name so every existing caller is
    unaffected; a caller building MULTIPLE merges in one test (a real revert-series fixture) must
    pass a distinct name each time, since `git checkout -b` on a name already used raises.
    """
    await git_.exec(["checkout", "-b", feature_branch])
    await _write_and_commit(git_.path, file_name, feature_text, "feature commit")
    await git_.exec(["checkout", branch])
    await git_.exec(["merge", "--no-ff", "-m", subject, feature_branch])
    return await git_.rev_parse(branch)


async def test_git_revert_stages_a_clean_revert_without_committing(git: Git) -> None:
    """The low-level primitive: `-m 1` on a real merge commit stages the reverted tree into the
    index and returns `True`, WITHOUT creating a commit — proving the `--no-commit` split from
    `commit()` actually holds (a caller gets a chance to stamp trailers before anything lands)."""
    tip_before_revert = await git.rev_parse(BRANCH)
    merge_sha = await _merge_feature_branch(
        git, branch=BRANCH, file_name="feature.txt", feature_text="from the hoist\n"
    )
    assert (git.path / "feature.txt").exists()

    assert await git.revert(merge_sha, mainline=1) is True

    assert await git.rev_parse(BRANCH) == merge_sha, "no commit was made by revert() itself"
    assert not (git.path / "feature.txt").exists(), "the revert IS staged into the worktree/index"
    assert await git.resolve("REVERT_HEAD") is not None, (
        "git leaves the sequencer state around after a clean --no-commit revert; only a "
        "subsequent commit() (or abort_revert()) clears it"
    )
    assert tip_before_revert != merge_sha  # sanity: the merge really did move the branch


async def test_git_revert_on_a_conflicting_target_leaves_it_mid_flight_and_abort_restores_the_tip(
    git: Git,
) -> None:
    """A conflict is a settled, meaningful `False`, not an exception — mirrors `rebase()`'s own
    conflict contract exactly. `abort_revert()` returns the branch to EXACTLY its pre-revert tip,
    with `REVERT_HEAD` cleared, same as `abort_rebase()`."""
    merge_sha = await _merge_feature_branch(
        git, branch=BRANCH, file_name="a.txt", feature_text="feature-edit\n"
    )
    # A later commit on `branch` that touches the SAME lines the merge changed: reverting the
    # merge must now conflict with this commit, which is exactly the "un-hoist reaches a repo
    # whose history has since moved on" shape SPEC's rollback has to detect.
    await _write_and_commit(
        git.path, "a.txt", "feature-edit\nmain-edit-after-merge\n", "edits a.txt after the merge"
    )
    branch_tip = await git.rev_parse(BRANCH)

    assert await git.revert(merge_sha, mainline=1) is False
    assert await git.resolve("REVERT_HEAD") is not None, "revert must be genuinely mid-flight"

    await git.abort_revert()

    assert await git.rev_parse(BRANCH) == branch_tip
    assert await git.resolve("REVERT_HEAD") is None
    assert await git.is_dirty() is False


async def test_git_revert_accepts_mainline_1_on_an_ordinary_non_merge_commit(git: Git) -> None:
    """Verified empirically (not assumed): git 2.43 treats `-m 1` on a plain single-parent commit
    as valid — parent 1 is simply the only parent — and only refuses a mainline number the commit
    does not actually have. `Git.revert` deliberately does not pre-validate `mainline` against
    parent count; this documents that git's own behaviour is what this method relies on."""
    await _write_and_commit(git.path, "a.txt", "v2\n", "ordinary commit")
    head = await git.rev_parse(BRANCH)

    assert await git.revert(head, mainline=1) is True
    assert (git.path / "a.txt").read_text() == "v1\n"  # back to what it was before "v2"


async def test_git_revert_on_an_unknown_mainline_raises_rather_than_reporting_a_conflict(
    git: Git,
) -> None:
    """The discriminating case between the `raise` and `return False` branches: a mainline number
    the commit does not have ("does not have parent 2") must not be silently read as "a conflict
    happened" just because both are non-zero exits — `REVERT_HEAD` never gets written for this
    failure, exactly the way `rebase()`'s unknown-`onto` test proves for `REBASE_HEAD`."""
    await _write_and_commit(git.path, "a.txt", "v2\n", "ordinary commit")
    head = await git.rev_parse(BRANCH)  # exactly one parent — `-m 2` names a parent it lacks
    with pytest.raises(GitCommandError) as caught:
        await git.revert(head, mainline=2)
    assert "parent 2" in caught.value.stderr
    assert await git.resolve("REVERT_HEAD") is None


async def test_abort_revert_with_nothing_in_progress_raises(git: Git) -> None:
    with pytest.raises(GitCommandError):
        await git.abort_revert()


async def test_revert_and_commit_stages_and_stamps_fleet_trailers_without_rewriting_history(
    git: Git,
) -> None:
    """The full Leg B primitive: stage the `-m 1` revert, then commit it with the standard six
    `Fleet-*` trailers — round-trippable by `find_trailer_commit` exactly like a normal patch
    commit (`test_commit_trailer_round_trips_and_is_found_by_the_scoped_search` above). The merge
    commit itself must still be present, unmodified, in history: this is a REVERT (a new commit
    undoing the change), never a `reset`/rewrite (CLAUDE.md guardrail 4 — git is the sole record,
    and `docs/SPEC.md`'s own text is explicit that "the revert *is* the record")."""
    anchor = await C.record_task_anchor(git, BRANCH)
    merge_sha = await _merge_feature_branch(
        git, branch=BRANCH, file_name="feature.txt", feature_text="from the hoist\n"
    )
    pid = "0" * 64  # this primitive does not derive a patch id of its own — see RevertOutcome
    task_id = "33333333-3333-4333-8333-333333333333"

    outcome = await C.revert_and_commit(
        git,
        sha=merge_sha,
        subject="fleet: revert hoist merge (D111 leg B)",
        trailers=trailers_for(pid, task_id=task_id),
        mainline=1,
    )

    assert outcome.conflicted is False
    assert outcome.commit_sha is not None
    assert not (git.path / "feature.txt").exists(), "the revert's effect landed in the worktree"

    # a plain, single-parent commit was made ON TOP — not a rewrite of the merge commit itself
    parents = (await git.text(["log", "-1", "--format=%P", outcome.commit_sha])).split()
    assert parents == [merge_sha]
    still_present = await git.text(["log", "-1", "--format=%H", merge_sha])
    assert still_present == merge_sha, "the merge commit was reverted, not erased from history"

    found = await git.find_trailer_commit(
        C.scoped_range(anchor, BRANCH), C.TASK_ID_TRAILER, task_id
    )
    assert found == outcome.commit_sha
    rows = await C.commits_in_range(git, branch=BRANCH, pre_commit_sha=anchor)
    assert rows[0]["sha"] == outcome.commit_sha, "git log lists newest first"
    assert rows[0]["task_id"] == task_id


async def test_revert_and_commit_on_a_conflict_makes_no_commit_and_leaves_it_for_the_caller(
    git: Git,
) -> None:
    """The conflict path stamps NOTHING — `RevertOutcome.commit_sha` is `None`, the branch tip is
    unmoved, and the repo is left exactly as `Git.revert()` alone would leave it (mid-flight),
    ready for `git.abort_revert()`. Reacting to this (SPEC §3.1's Leg D) is deliberately out of
    scope for this primitive."""
    merge_sha = await _merge_feature_branch(
        git, branch=BRANCH, file_name="a.txt", feature_text="feature-edit\n"
    )
    await _write_and_commit(
        git.path, "a.txt", "feature-edit\nmain-edit-after-merge\n", "edits a.txt after the merge"
    )
    branch_tip = await git.rev_parse(BRANCH)

    outcome = await C.revert_and_commit(
        git,
        sha=merge_sha,
        subject="fleet: revert hoist merge (D111 leg B)",
        trailers=trailers_for("1" * 64),
        mainline=1,
    )

    assert outcome == C.RevertOutcome(commit_sha=None, conflicted=True)
    assert await git.rev_parse(BRANCH) == branch_tip

    await git.abort_revert()
    assert await git.rev_parse(BRANCH) == branch_tip
    assert await git.is_dirty() is False


# --------------------------------------------------------------------------------------
# ADR-0122 Decision 5 (round VI task 71): the `Fleet-Contract-Rollback-Id` trailer and its
# resume/idempotency query, `contract_rollback_shas_in_range`.
# --------------------------------------------------------------------------------------
def test_fleet_trailers_as_mapping_has_exactly_six_keys_when_contract_rollback_id_is_unset() -> (
    None
):
    """Old-passes half of Rule 12's mutation proof: every existing call site constructs
    `FleetTrailers` without the new seventh field, and `as_mapping()` must still produce EXACTLY
    the original six-key mapping for them — the new field must not leak in as e.g. a `None`-valued
    entry."""
    mapping = trailers_for("0" * 64).as_mapping()
    assert set(mapping) == {
        C.RUN_ID_TRAILER, C.REPO_ID_TRAILER, C.PHASE_TRAILER,
        C.TASK_ID_TRAILER, C.ATTEMPT_TRAILER, C.PATCH_ID_TRAILER,
    }
    assert C.CONTRACT_ROLLBACK_ID_TRAILER not in mapping


def test_fleet_trailers_as_mapping_includes_seventh_key_when_contract_rollback_id_is_set() -> None:
    """New-fails-without-the-change half of the same proof (Rule 12): setting
    `contract_rollback_id` must add EXACTLY one new key, with the exact value, on top of the same
    six. This is the discriminator — reverting this task's `as_mapping()` change makes this test
    fail (`Fleet-Contract-Rollback-Id` never appears) while the test above keeps passing, verified
    with a real reverted-diff mutation (this task's report)."""
    trailers = C.FleetTrailers(
        run_id=RUN_ID, repo_id=REPO_ID, phase=2,
        task_id="11111111-1111-4111-8111-111111111111", attempt=1, patch_id="0" * 64,
        contract_rollback_id="proto:demo",
    )
    mapping = trailers.as_mapping()
    assert mapping[C.CONTRACT_ROLLBACK_ID_TRAILER] == "proto:demo"
    without_seventh = dict(mapping)
    del without_seventh[C.CONTRACT_ROLLBACK_ID_TRAILER]
    assert without_seventh == trailers_for("0" * 64).as_mapping()


async def test_contract_rollback_shas_in_range_round_trips_a_real_revert_series(
    git: Git,
) -> None:
    """The full Decision 5 primitive, against real commits: two merges, both reverted with the
    SAME `Fleet-Contract-Rollback-Id` trailer value — proving the function does not just check the
    trailer (which cannot by itself distinguish which original sha a given revert commit reverts)
    but reads each matching commit's BODY for "This reverts commit <sha>." to recover the mapping.
    A third, unrelated revert carrying a DIFFERENT contract id must be excluded."""
    anchor = await C.record_task_anchor(git, BRANCH)
    merge_1 = await _merge_feature_branch(
        git, branch=BRANCH, file_name="one.txt", feature_text="hoist one\n",
        subject="merge one", feature_branch="feature-one",
    )
    merge_2 = await _merge_feature_branch(
        git, branch=BRANCH, file_name="two.txt", feature_text="hoist two\n",
        subject="merge two", feature_branch="feature-two",
    )
    merge_3 = await _merge_feature_branch(
        git, branch=BRANCH, file_name="three.txt", feature_text="unrelated\n",
        subject="merge three", feature_branch="feature-three",
    )

    async def _revert(sha: str, *, contract_id: str, task_suffix: str) -> None:
        outcome = await C.revert_and_commit(
            git,
            sha=sha,
            subject=f"fleet: revert {sha[:8]}",
            trailers=C.FleetTrailers(
                run_id=RUN_ID, repo_id=REPO_ID, phase=3,
                task_id=f"22222222-2222-4222-8222-2222222222{task_suffix}",
                attempt=1, patch_id=sha, contract_rollback_id=contract_id,
            ),
            mainline=1,
            body=f"This reverts commit {sha}.",
        )
        assert outcome.conflicted is False

    # Reverse-chronological order, exactly ADR-0122 Decision 4's ordering rule (most recently
    # merged reverted first): merge_2 then merge_1, both stamped for "proto:demo".
    await _revert(merge_2, contract_id="proto:demo", task_suffix="02")
    await _revert(merge_1, contract_id="proto:demo", task_suffix="01")
    # An unrelated rollback for a DIFFERENT contract, sharing nothing but the mechanism.
    await _revert(merge_3, contract_id="proto:other", task_suffix="03")

    reverted = await C.contract_rollback_shas_in_range(
        git, pre_commit_sha=anchor, branch=BRANCH, contract_id="proto:demo"
    )
    assert reverted == frozenset({merge_1, merge_2})

    reverted_other = await C.contract_rollback_shas_in_range(
        git, pre_commit_sha=anchor, branch=BRANCH, contract_id="proto:other"
    )
    assert reverted_other == frozenset({merge_3})

    reverted_unknown = await C.contract_rollback_shas_in_range(
        git, pre_commit_sha=anchor, branch=BRANCH, contract_id="proto:no-such-contract"
    )
    assert reverted_unknown == frozenset()


async def test_push_force_with_lease_with_the_correct_expected_sha_succeeds(
    remote_and_clone: tuple[Path, Path], tmp_path: Path
) -> None:
    remote, clone = remote_and_clone
    expected_sha = await _remote_tip(tmp_path, remote)

    await _write_and_commit(clone, "b.txt", "v2\n", "a second commit")
    git_clone = Git(clone, timeout_s=60)
    new_tip = await git_clone.rev_parse("HEAD")

    await git_clone.push_force_with_lease("origin", "main", expected_sha=expected_sha)

    assert await _remote_tip(tmp_path, remote) == new_tip


async def test_push_force_with_lease_refuses_when_the_remote_moved_past_the_expected_sha(
    remote_and_clone: tuple[Path, Path], tmp_path: Path
) -> None:
    """The core safety proof. Clone B pushes first, moving the bare remote's `main`; clone A,
    still holding its now-STALE belief of `origin/main` as `expected_sha`, must be refused by
    the server — and the remote tip, read independently via the bare repo (never through either
    clone's own belief), must still be clone B's commit, unchanged.

    This is the discriminating test named by the brief: an implementation that silently used
    bare `--force` instead of the explicit `--force-with-lease=<branch>:<sha>` form would pass
    every other test in this file and fail only this one.
    """
    remote, clone_a = remote_and_clone
    stale_sha = await _remote_tip(tmp_path, remote)

    clone_b = tmp_path / "clone_b"
    await _sh(tmp_path, "clone", "-q", str(remote), str(clone_b))
    await _sh(clone_b, "config", "user.email", "fleet@example.invalid")
    await _sh(clone_b, "config", "user.name", "Fleet Test")
    await _write_and_commit(clone_b, "concurrent.txt", "clone B's work\n", "clone B's commit")
    await _sh(clone_b, "push", "-q", "origin", "main")
    b_tip = await _remote_tip(tmp_path, remote)
    assert b_tip != stale_sha, "the setup must actually move the remote, or this test is vacuous"

    await _write_and_commit(clone_a, "a_only.txt", "clone A's work\n", "clone A's commit")
    git_a = Git(clone_a, timeout_s=60)

    with pytest.raises(GitCommandError) as caught:
        await git_a.push_force_with_lease("origin", "main", expected_sha=stale_sha)
    assert "stale info" in caught.value.stderr or "rejected" in caught.value.stderr

    assert await _remote_tip(tmp_path, remote) == b_tip, "clone B's push must survive unchanged"


async def test_push_force_with_lease_to_a_branch_that_does_not_exist_yet_raises(
    remote_and_clone: tuple[Path, Path],
) -> None:
    """A LOCAL branch that does not exist at all fails at refspec resolution (`src refspec ...
    does not match any`) — an ordinary git failure unrelated to lease semantics (it would fail
    identically under bare `--force` or no force at all), but still something this method must
    not swallow. `expected_sha` here is an ordinary (wrong-looking but non-sentinel) SHA, not the
    all-zero sentinel — that scenario (a branch that exists LOCALLY but was never pushed to the
    remote) is a different failure mode, covered separately by
    `test_push_force_with_lease_refuses_the_all_zero_sentinel_on_an_unpushed_branch`
    below (round VI task 15 review finding B: conflating the two here would make this test pass
    for the wrong reason)."""
    _remote, clone = remote_and_clone
    git_clone = Git(clone, timeout_s=60)
    with pytest.raises(GitCommandError) as caught:
        await git_clone.push_force_with_lease(
            "origin", "does-not-exist-branch", expected_sha="1" * 40
        )
    assert "does not match any" in caught.value.stderr


async def test_push_force_with_lease_refuses_the_all_zero_sentinel_on_an_unpushed_branch(
    remote_and_clone: tuple[Path, Path], tmp_path: Path
) -> None:
    """The real "branch does not exist ON THE REMOTE yet" scenario (round VI task 15 review
    finding B): a branch that exists LOCALLY but has never been pushed. Git's own
    force-with-lease semantics treat the all-zero SHA (`"0" * 40`, and the empty string
    identically) as a sentinel meaning "the ref must NOT currently exist" — passing it here would
    otherwise silently CREATE the remote branch, exit 0, no exception (verified empirically
    before this guard was added: the un-guarded call succeeded and left a new
    `refs/heads/feature-branch` on the bare remote). `push_force_with_lease` must refuse
    client-side, before git ever runs, and nothing must land on the remote."""
    remote, clone = remote_and_clone
    git_clone = Git(clone, timeout_s=60)
    await git_clone.exec(["checkout", "-b", "feature-branch"])

    with pytest.raises(ValueError, match="all-zero"):
        await git_clone.push_force_with_lease("origin", "feature-branch", expected_sha="0" * 40)
    with pytest.raises(ValueError, match="all-zero"):
        await git_clone.push_force_with_lease("origin", "feature-branch", expected_sha="")

    # Independently confirm the guard fired BEFORE git ran: no branch was created on the remote.
    result = await _sh(
        tmp_path,
        f"--git-dir={remote}",
        "show-ref",
        "--verify",
        "--quiet",
        "refs/heads/feature-branch",
    )
    assert result.exit_code != 0, "the all-zero sentinel must never reach git at all"


# --------------------------------------------------------------------------------------
# commits.py — the patch id and the trailer round-trip
# --------------------------------------------------------------------------------------
def test_patch_id_is_a_pure_function_of_content() -> None:
    """A crash-and-retry must recompute the SAME id, or the guard never recognises its own work;
    a different edit must not collide, or a guard skips work that was never done."""
    one = Patch("a.txt", "--- a\n+++ b\n+x\n")
    two = Patch("b.txt", "--- a\n+++ b\n+y\n")
    assert C.patch_id([one, two]) == C.patch_id([two, one])  # order-independent
    assert C.patch_id([one]) != C.patch_id([two])
    assert len(C.patch_id([one])) == 64


async def test_commit_trailer_round_trips_and_is_found_by_the_scoped_search(
    git: Git, tmp_path: Path
) -> None:
    """The whole record lives in the commit: written by `--trailer`, read back by git's own
    trailer parser over `<phases.pre_commit_sha>..migrate/<repo>`. If this round trip breaks,
    resume cannot tell "landed" from "never ran"."""
    anchor = await C.record_task_anchor(git, BRANCH)
    patch = await build_patch(git, "a.txt", "v2\n", tmp_path / "p.patch")
    pid = C.patch_id([Patch("a.txt", patch.read_text())])
    task_id = "22222222-2222-4222-8222-222222222222"

    outcome = await C.apply_and_commit(
        git,
        patch=patch,
        subject="fleet: relocate a.txt",
        trailers=trailers_for(pid, task_id=task_id),
        branch=BRANCH,
        pre_commit_sha=anchor,
    )
    assert outcome.committed and outcome.commit_sha is not None
    assert (git.path / "a.txt").read_text() == "v2\n"

    found = await git.find_trailer_commit(
        C.scoped_range(anchor, BRANCH), C.PATCH_ID_TRAILER, pid
    )
    assert found == outcome.commit_sha
    assert await C.find_task_commit(
        git, branch=BRANCH, pre_commit_sha=anchor, task_id=task_id
    ) == outcome.commit_sha
    rows = await C.commits_in_range(git, branch=BRANCH, pre_commit_sha=anchor)
    assert rows[0]["patch_id"] == pid and rows[0]["task_id"] == task_id


async def test_guard_skips_a_patch_that_is_applied_and_still_present(
    git: Git, tmp_path: Path
) -> None:
    """Case (a): trailer in the scoped range AND the effect at the tip. Re-running the rung must
    consume no attempt and write no duplicate commit (§3.2 step 6.1, `already_applied`)."""
    anchor = await C.record_task_anchor(git, BRANCH)
    patch = await build_patch(git, "a.txt", "v2\n", tmp_path / "p.patch")
    pid = C.patch_id([Patch("a.txt", patch.read_text())])
    first = await C.apply_and_commit(
        git, patch=patch, subject="fleet: v2", trailers=trailers_for(pid),
        branch=BRANCH, pre_commit_sha=anchor,
    )

    again = await C.apply_and_commit(
        git, patch=patch, subject="fleet: v2", trailers=trailers_for(pid, attempt=2),
        branch=BRANCH, pre_commit_sha=anchor,
    )
    assert again.skipped is True
    assert again.guard.effect_present is True
    assert again.guard.trailer_commit == first.commit_sha
    assert await git.rev_parse(BRANCH) == first.commit_sha  # no second commit was written


async def test_a_trailer_whose_effect_was_dropped_is_RE_APPLIED_not_skipped(
    git: Git, tmp_path: Path
) -> None:
    """THE headline case. A trailer proves the patch was once committed — not that its effect
    survived. Under a trailer-only guard this patch is skipped forever, and the consumer ships a
    PR pointing at a path that no longer exists. Both guard conditions are required, and the
    deciding one is `git apply --check --reverse` against the CURRENT worktree.
    """
    anchor = await C.record_task_anchor(git, BRANCH)
    patch = await build_patch(git, "a.txt", "v2\n", tmp_path / "p.patch")
    pid = C.patch_id([Patch("a.txt", patch.read_text())])
    first = await C.apply_and_commit(
        git, patch=patch, subject="fleet: v2", trailers=trailers_for(pid),
        branch=BRANCH, pre_commit_sha=anchor,
    )

    # A later rebase/revert drops the hunk while the trailer stays in history.
    await git.exec(["revert", "--no-edit", str(first.commit_sha)], with_identity=True)
    assert (git.path / "a.txt").read_text() == "v1\n"

    # A trailer-only guard WOULD skip here — the trailer is still in the scoped range.
    assert await git.find_trailer_commit(
        C.scoped_range(anchor, BRANCH), C.PATCH_ID_TRAILER, pid
    ) == first.commit_sha

    outcome = await C.guard(
        git, branch=BRANCH, pre_commit_sha=anchor, patch=patch, patch_id_value=pid
    )
    assert outcome.already_applied is False
    assert outcome.effect_present is False
    assert outcome.trailer_commit == first.commit_sha
    assert "effect is NOT in the current tree" in outcome.reason

    redone = await C.apply_and_commit(
        git, patch=patch, subject="fleet: v2 (re-applied)", trailers=trailers_for(pid, attempt=2),
        branch=BRANCH, pre_commit_sha=anchor,
    )
    assert redone.skipped is False
    assert redone.commit_sha not in (None, first.commit_sha)
    assert (git.path / "a.txt").read_text() == "v2\n"


async def test_the_trailer_search_is_scoped_to_the_phase_anchor(git: Git, tmp_path: Path) -> None:
    """A trailer BEFORE `phases.pre_commit_sha` does not count as applied in this phase.

    Searching the branch's whole history is what §3.2 step 6.1 forbids: an earlier run's commit
    would make this run believe work it never did had landed.
    """
    anchor_one = await C.record_task_anchor(git, BRANCH)
    patch = await build_patch(git, "a.txt", "v2\n", tmp_path / "p.patch")
    pid = C.patch_id([Patch("a.txt", patch.read_text())])
    first = await C.apply_and_commit(
        git, patch=patch, subject="fleet: v2", trailers=trailers_for(pid),
        branch=BRANCH, pre_commit_sha=anchor_one,
    )
    anchor_two = await C.record_task_anchor(git, BRANCH)  # a later phase's anchor

    assert await git.find_trailer_commit(BRANCH, C.PATCH_ID_TRAILER, pid) == first.commit_sha
    assert await git.find_trailer_commit(
        C.scoped_range(anchor_two, BRANCH), C.PATCH_ID_TRAILER, pid
    ) is None
    assert await C.commits_in_range(git, branch=BRANCH, pre_commit_sha=anchor_two) == ()

    with pytest.raises(C.RollbackAnchorError):
        C.scoped_range("", BRANCH)


# --------------------------------------------------------------------------------------
# commits.py — the two rollback anchors
# --------------------------------------------------------------------------------------
async def test_task_rollback_resets_to_the_task_anchor_and_keeps_earlier_work(
    git: Git, tmp_path: Path
) -> None:
    """Crash-discard of ONE task must not rewind past that task's start.

    Resetting to the PHASE anchor instead would delete task 1's commit — whose row is already
    `DONE` and will never re-run — leaving the phase to pass its success criterion on a tree
    missing most of its rewrites (§3.2 step 6.5).
    """
    phase_anchor = await C.record_task_anchor(git, BRANCH)
    p1 = await build_patch(git, "a.txt", "v2\n", tmp_path / "p1.patch")
    task_one = "33333333-3333-4333-8333-333333333333"
    first = await C.apply_and_commit(
        git, patch=p1, subject="task 1", branch=BRANCH, pre_commit_sha=phase_anchor,
        trailers=trailers_for(C.patch_id([Patch("a.txt", p1.read_text())]), task_id=task_one),
    )

    task_anchor = await C.record_task_anchor(git, BRANCH)
    assert task_anchor == first.commit_sha != phase_anchor

    p2 = await build_patch(git, "b.txt", "changed\n", tmp_path / "p2.patch")
    await C.apply_and_commit(
        git, patch=p2, subject="task 2", branch=BRANCH, pre_commit_sha=phase_anchor,
        trailers=trailers_for(C.patch_id([Patch("b.txt", p2.read_text())]), task_id="t2"),
    )
    (git.path / "debris.tmp").write_text("half-written by a killed git apply\n")

    await C.discard_task(git, task_pre_commit_sha=task_anchor, branch="HEAD")

    assert await git.rev_parse(BRANCH) == task_anchor
    assert (git.path / "a.txt").read_text() == "v2\n"      # task 1's work survives
    assert (git.path / "b.txt").read_text() == "keep\n"    # task 2's work is gone
    assert not (git.path / "debris.tmp").exists()          # clean -fdx ran
    assert await C.find_task_commit(
        git, branch=BRANCH, pre_commit_sha=phase_anchor, task_id=task_one
    ) == first.commit_sha


async def test_task_rollback_refuses_an_anchor_that_would_rewrite_history(
    git: Git, tmp_path: Path
) -> None:
    """A "rollback" to a non-ancestor is not a rollback — it moves the branch sideways onto a
    tree nobody produced. And an empty anchor has no safe default (Rule 11: refuse, loudly)."""
    (git.path / "a.txt").write_text("side\n")
    await git.exec(["checkout", "-b", "sidebranch"], check=True)
    await git.add_all()
    sideways = await git.commit("a sibling branch's commit")
    await git.exec(["checkout", BRANCH])

    with pytest.raises(C.RollbackAnchorError):
        await C.discard_task(git, task_pre_commit_sha="")
    with pytest.raises(C.RollbackAnchorError):
        await C.discard_task(git, task_pre_commit_sha="0" * 40)
    with pytest.raises(C.RollbackAnchorError):
        await C.discard_task(git, task_pre_commit_sha=sideways, branch=BRANCH)


# --------------------------------------------------------------------------------------
# commits.py — the D42 x rollback interaction: `discard_task`'s OWN probes run on the same
# expired deadline that (usually) triggered the rollback in the first place. A plain
# `GitCommandError` escaping `discard_task` is caught by `workers/rewrite.py`'s
# `except (PatchApplyError, GitCommandError)` around `land_patches` and misreported as
# PATCH_REJECTED — spending an LLM repair rung on a tree that was never actually discarded.
# `RollbackIndeterminateError` is a bare `GitError`, so it escapes that catch instead.
# --------------------------------------------------------------------------------------
async def test_discard_task_unsettled_resolve_probe_is_not_a_plain_GitCommandError(
    git: Git,
) -> None:
    """`git.resolve` raises `GitCommandError` (D42) when its own probe never settles. If
    `discard_task` let that escape unchanged, `land_patches`'s `except (PatchApplyError,
    GitCommandError)` would swallow it exactly like the original patch failure it is trying to
    roll back from — the interaction this task exists to close.
    """
    anchor = await C.record_task_anchor(git, BRANCH)
    starved = Git(git.path, runner=DeadlineMidRollbackRunner(unsettle=("rev-parse",)), timeout_s=60)

    with pytest.raises(C.RollbackIndeterminateError) as caught:
        await C.discard_task(starved, task_pre_commit_sha=anchor, branch="HEAD")
    assert not isinstance(caught.value, GitCommandError)
    assert isinstance(caught.value.__cause__, GitCommandError)


async def test_discard_task_unsettled_is_ancestor_probe_is_not_a_plain_GitCommandError(
    git: Git, tmp_path: Path
) -> None:
    """The second probe, not just the first: `resolve` settles normally here, and `is_ancestor` is
    the one starved of an answer — proving the wrapping covers both calls, not just the first."""
    phase_anchor = await C.record_task_anchor(git, BRANCH)
    p1 = await build_patch(git, "a.txt", "v2\n", tmp_path / "p1.patch")
    await C.apply_and_commit(
        git, patch=p1, subject="task 1", branch=BRANCH, pre_commit_sha=phase_anchor,
        trailers=trailers_for(C.patch_id([Patch("a.txt", p1.read_text())]), task_id="t1"),
    )
    task_anchor = await C.record_task_anchor(git, BRANCH)

    starved = Git(
        git.path,
        runner=DeadlineMidRollbackRunner(unsettle=("merge-base", "--is-ancestor")),
        timeout_s=60,
    )
    with pytest.raises(C.RollbackIndeterminateError) as caught:
        await C.discard_task(starved, task_pre_commit_sha=task_anchor, branch="HEAD")
    assert not isinstance(caught.value, GitCommandError)


async def test_discard_task_a_failed_reset_is_also_indeterminate_not_a_plain_GitCommandError(
    git: Git,
) -> None:
    """Not just the two `check=False` probes: `reset --hard`/`clean -fdx` actually mutate the
    tree, and a failure there (genuine refusal, kill at deadline, or anything else) leaves the
    worktree's relationship to the anchor just as unproven as an unsettled probe would — "git
    genuinely cannot be consulted at all" still must not read as PATCH_REJECTED.
    """
    anchor = await C.record_task_anchor(git, BRANCH)
    broken = Git(
        git.path,
        runner=RefuseOneCommandRunner(refuse=("reset", "--hard")),
        timeout_s=60,
    )
    with pytest.raises(C.RollbackIndeterminateError) as caught:
        await C.discard_task(broken, task_pre_commit_sha=anchor, branch="HEAD")
    assert not isinstance(caught.value, GitCommandError)


async def test_discard_task_distinguishes_a_settled_refusal_from_an_unsettled_probe(
    git: Git,
) -> None:
    """A settled "no" (`RollbackAnchorError`: the anchor is genuinely missing or not an ancestor)
    and an unsettled probe (`RollbackIndeterminateError`: the same probe never got an answer) must
    not collapse into one type — the four-state-collapse discipline this codebase already applies
    elsewhere (D29, D34-D45), one layer up: a tree that could not be verified discarded is not the
    same fact as a tree that was verified NOT discardable.
    """
    with pytest.raises(C.RollbackAnchorError) as settled:
        await C.discard_task(git, task_pre_commit_sha="0" * 40)
    assert not isinstance(settled.value, C.RollbackIndeterminateError)

    starved = Git(git.path, runner=DeadlineMidRollbackRunner(unsettle=("rev-parse",)), timeout_s=60)
    with pytest.raises(C.RollbackIndeterminateError) as unsettled:
        await C.discard_task(starved, task_pre_commit_sha="0" * 40)
    assert not isinstance(unsettled.value, C.RollbackAnchorError)


async def test_whole_phase_rollback_uses_the_phase_anchor_ref(git: Git, tmp_path: Path) -> None:
    """The phase anchor is a REAL ref (`refs/fleet/<run>/<repo>/phase-<n>/base`), so rollback is
    `update-ref` and is exact by construction — nothing is reconstructed from a stored diff."""
    base = await git.rev_parse(BRANCH)
    ref = f"refs/fleet/{RUN_ID}/acme-billing/phase-2/base"
    await git.update_ref(ref, base, message="fleet phase anchor")

    patch = await build_patch(git, "a.txt", "v2\n", tmp_path / "p.patch")
    await C.apply_and_commit(
        git, patch=patch, subject="task 1", branch=BRANCH, pre_commit_sha=base,
        trailers=trailers_for(C.patch_id([Patch("a.txt", patch.read_text())])),
    )
    assert await git.rev_parse(BRANCH) != base

    assert await C.rollback_phase(git, branch=BRANCH, phase_base_ref=ref) == base
    assert await git.rev_parse(BRANCH) == base
    with pytest.raises(C.RollbackAnchorError):
        await C.rollback_phase(git, branch=BRANCH, phase_base_ref="refs/fleet/absent")


# --------------------------------------------------------------------------------------
# secrets and the no-shell invariant
# --------------------------------------------------------------------------------------
async def test_a_pat_in_a_remote_url_never_reaches_a_returned_string_or_an_error(
    git: Git,
) -> None:
    """Mirror remotes in this environment carry a plaintext `github_pat_…` in origin (CLAUDE.md).

    §12.20 greps logs and artifacts for that literal and requires zero hits, so neither a returned
    URL nor an exception message built from argv may contain it. The redactor is `obs/redact.py`;
    this test is what keeps the call sites wired to it.
    """
    await git.exec(["remote", "add", "origin", PAT_URL])

    url = await git.remote_url("origin")
    assert url is not None
    assert FAKE_PAT not in url and "github_pat_" not in url
    assert "«redacted:" in url
    assert "git.example.invalid" in url  # the host survives: a log nobody can debug is no good

    with pytest.raises(GitCommandError) as caught:
        await git.exec(["cat-file", "-p", PAT_URL])  # local, failing, token in argv
    rendered = str(caught.value)
    assert "github_pat_" not in rendered
    assert all("github_pat_" not in part for part in caught.value.argv)
    assert "github_pat_" not in caught.value.stderr


def test_the_package_never_uses_a_shell() -> None:
    """A repo id, a branch name, or a model-proposed path must never be parsed as shell syntax.

    Asserted on the source text because the guarantee is "nowhere in this package", which no
    individual call-site test can establish.
    """
    package = Path(__file__).resolve().parents[1] / "src" / "fleet" / "vcs"
    banned = ("shell=True", "create_subprocess_shell", "os.system", "subprocess.run", "os.popen")
    for module in sorted(package.glob("*.py")):
        source = module.read_text()
        for needle in banned:
            assert needle not in source, f"{module.name} uses {needle}"


# --------------------------------------------------------------------------------------
# filter_repo.py — argv, the mutex, snapshots, and the idempotent merge
# --------------------------------------------------------------------------------------
def test_filter_repo_argv_encodes_the_relocation_plan() -> None:
    """The whole-repo move and the hoisted-contract path filter are one command shape (§3.3 step
    1); building it as data is what keeps it assertable on a host with no git-filter-repo."""
    whole = FR.filter_repo_argv(FR.RelocationSpec(dest_path="ts/@acme/billing"))
    assert whole[0] == "git-filter-repo"
    assert "--path-rename" in whole and ":ts/@acme/billing/" in whole
    assert "--strip-blobs-bigger-than" in whole and "10M" in whole
    assert "--force" in whole  # every retry filters a fresh throwaway clone

    hoisted = FR.filter_repo_argv(
        FR.RelocationSpec(
            dest_path="contracts/billing",
            source_paths=("proto/billing", "proto/shared"),
            source_prefix="proto/",
        )
    )
    assert hoisted.count("--path") == 2
    assert "proto/:contracts/billing/" in hoisted


def test_filter_repo_argv_renders_replace_text_when_set(tmp_path: Path) -> None:
    """§11.4 / D21: `--replace-text` had zero test hits of any kind before this — this is the
    render half of the wiring gap. `resolve_replace_text` below is the settings half."""
    scrub_file = tmp_path / "secrets.txt"
    scrub_file.write_text("literal:SECRET==>«redacted»\n", encoding="utf-8")

    with_scrub = FR.filter_repo_argv(FR.RelocationSpec(dest_path="ts/x", replace_text=scrub_file))
    assert "--replace-text" in with_scrub
    assert str(scrub_file) in with_scrub

    without_scrub = FR.filter_repo_argv(FR.RelocationSpec(dest_path="ts/x"))
    assert "--replace-text" not in without_scrub


async def test_relocate_passes_replace_text_through_to_the_executed_argv(tmp_path: Path) -> None:
    """The test D21 asked for: a caller that threads `RelocationSpec.replace_text` through
    `relocate()` must reach the real, executed `git-filter-repo` argv. If a future edit stops
    rendering `--replace-text` (or stops passing the spec's field into `filter_repo_argv`), THIS
    fails. It does not prove any production caller sets the field — only that the mechanism, once
    fed a spec that carries it, is not lost between here and the process that runs.
    """
    scrub_file = tmp_path / "secrets.txt"
    scrub_file.write_text("literal:SECRET==>«redacted»\n", encoding="utf-8")
    runner = ScriptedRunner(exit_code=0)

    await FR.relocate(
        tmp_path, FR.RelocationSpec(dest_path="ts/x", replace_text=scrub_file), runner=runner
    )

    assert len(runner.calls) == 1
    executed = runner.calls[0]
    assert "--replace-text" in executed
    assert str(scrub_file) in executed


def test_resolve_replace_text_disables_the_scrub_on_an_empty_setting() -> None:
    """An operator who wants no file-based scrub sets the field to `""` explicitly; before this
    function existed there was no code path that read the setting at all (D21)."""
    assert FR.resolve_replace_text(Path("/nonexistent-root"), "") is None
    assert FR.resolve_replace_text(Path("/nonexistent-root"), "   ") is None


def test_resolve_replace_text_resolves_an_existing_file_against_the_settings_root(
    tmp_path: Path,
) -> None:
    (tmp_path / "config" / "rules").mkdir(parents=True)
    scrub_file = tmp_path / "config" / "rules" / "secrets.txt"
    scrub_file.write_text("literal:x==>y\n", encoding="utf-8")
    resolved = FR.resolve_replace_text(tmp_path, "config/rules/secrets.txt")
    assert resolved == scrub_file.resolve()


def test_resolve_replace_text_refuses_a_configured_but_missing_file(tmp_path: Path) -> None:
    """D21's own severity note: a scrub that is configured but silently no-ops is worse than one
    that is visibly absent. `config/` does not exist in THIS repository at all — the exact
    condition the docs entry names as the default's real-world state today."""
    with pytest.raises(FR.HistoryScrubUnavailableError, match=r"config/rules/secrets\.txt"):
        FR.resolve_replace_text(tmp_path, "config/rules/secrets.txt")


async def test_relocate_names_a_missing_git_filter_repo_instead_of_falling_back(
    tmp_path: Path,
) -> None:
    """Rule 11. A missing rewriter must be a named failure, never a silent degradation to
    `filter-branch`, which has different semantics and would produce a different history.

    The double is `RaisingRunner`, not `ScriptedRunner(exit_code=127, ...)`: no shell is ever
    involved, so a genuinely missing `git-filter-repo` is `FileNotFoundError` raised by
    `asyncio.create_subprocess_exec` before any `ProcResult` exists — never an exit code.
    """
    runner = RaisingRunner(FileNotFoundError("git-filter-repo"))
    with pytest.raises(FR.FilterRepoUnavailableError):
        await FR.relocate(tmp_path, FR.RelocationSpec(dest_path="ts/x"), runner=runner)


async def test_relocate_exit_127_is_an_ordinary_failure_not_a_missing_binary(
    tmp_path: Path,
) -> None:
    """`exit_code == 127` on a returned `ProcResult` used to be read as "binary missing", but
    nothing in this stack can produce that: no shell means no shell "command not found"
    convention. A `git-filter-repo` that RAN and happened to exit 127 of its own accord is an
    ordinary rewrite failure, not an unavailable binary."""
    runner = ScriptedRunner(exit_code=127, started=True, stderr="boom")
    with pytest.raises(FR.IngestError):
        await FR.relocate(tmp_path, FR.RelocationSpec(dest_path="ts/x"), runner=runner)


async def test_relocate_a_passed_deadline_is_not_mistaken_for_a_missing_binary(
    tmp_path: Path,
) -> None:
    """`not started` is `util.proc.run`'s call-past-deadline synthesis (§7.1) and nothing else —
    never a missing binary. Misreading it as `FilterRepoUnavailableError` blames the operator's
    PATH for the fleet's own clock instead of surfacing the retryable clock failure it is."""
    never_ran = ScriptedRunner(
        exit_code=124,
        started=False,
        timed_out=True,
        stderr="deadline had already passed; process was not started",
    )
    with pytest.raises(FR.IngestError) as exc_info:
        await FR.relocate(tmp_path, FR.RelocationSpec(dest_path="ts/x"), runner=never_ran)
    assert not isinstance(exc_info.value, FR.FilterRepoUnavailableError)
    assert "deadline had already passed" in str(exc_info.value)


async def test_the_integration_mutex_admits_exactly_one_writer(tmp_path: Path) -> None:
    """Concurrent `--allow-unrelated-histories` merges racing one ref update orphan all but one
    merge commit, leaving an `attempts.commit_sha` unreachable from `integration` — a corruption
    no later phase can detect (§3.3 step 1). So the second writer must WAIT, not proceed."""
    first = FR.IntegrationMutex(tmp_path, RUN_ID)
    second = FR.IntegrationMutex(tmp_path, RUN_ID, timeout_s=0.05, poll_interval_s=0.01)
    async with first:
        assert first.held
        with pytest.raises(FR.LockTimeoutError):
            await second.acquire()
        assert not second.held
    await second.acquire()  # released by __aexit__, so the queue drains
    assert second.held
    second.release()


async def test_ingest_merges_with_provenance_and_is_safe_to_re_run(tmp_path: Path) -> None:
    """Ingest is offline (a local-path fetch), records `Source-Repo`/`Source-Sha` on the merge
    commit, and a retry must not merge the same history twice — a Phase 3 task is retried up to
    three times, and a duplicated merge is history a reviewer cannot read.
    """

    async def build() -> tuple[Git, Path, str]:
        mono = tmp_path / "mono"
        await _init_repo(mono, files={"README.md": "monorepo\n"})
        await _sh(mono, "checkout", "-b", "integration")
        source = tmp_path / "filtered"
        await _init_repo(source, files={"ts/@acme/billing/index.ts": "export const x = 1;\n"})
        head = (await _sh(source, "rev-parse", "HEAD")).stdout_tail.strip()
        return Git(mono, timeout_s=60), source, head

    git_, source, source_sha = await build()
    provenance = FR.SourceProvenance(repo_id=REPO_ID, sha=source_sha)

    result = await FR.ingest(
        git_, source_dir=source, source=provenance, run_id=RUN_ID,
        integration_branch="integration",
    )
    assert result.already_present is False
    assert (git_.path / "ts/@acme/billing/index.ts").exists()

    merge = (await git_.log("integration", trailer_keys=[FR.SOURCE_REPO_TRAILER,
                                                         FR.SOURCE_SHA_TRAILER], limit=1))[0]
    assert merge.trailer(FR.SOURCE_REPO_TRAILER) == REPO_ID
    assert merge.trailer(FR.SOURCE_SHA_TRAILER) == source_sha

    # D115: a fresh ingest creates `migrate/<repo>` inside the monorepo, an alias of the merge
    # commit, so D94's PR-promotion mechanism has something to operate on.
    migrate_ref = f"migrate/{REPO_ID}"
    assert await git_.rev_parse(migrate_ref) == result.merge_sha

    # Drift `migrate/<repo>` to a DIFFERENT, but still valid, object before the re-run — a plain
    # "still equals result.merge_sha" check after the re-run cannot discriminate the idempotent
    # path's own `create_branch` call from simply never having moved the branch a second time,
    # since the branch already points at the right sha from the first call. Only a genuine
    # drift-and-repoint proves the idempotent path re-asserts the ref itself.
    assert source_sha != result.merge_sha
    await git_.create_branch(migrate_ref, source_sha, force=True)
    assert await git_.rev_parse(migrate_ref) == source_sha

    again = await FR.ingest(
        git_, source_dir=source, source=provenance, run_id=RUN_ID,
        integration_branch="integration",
    )
    assert again.already_present is True
    assert again.merge_sha == result.merge_sha
    assert again.snapshot.seq == result.snapshot.seq + 1  # a fresh, immutable ref per build

    # Idempotent path: `migrate/<repo>` is corrected back to the merge commit, not left drifted.
    assert await git_.rev_parse(migrate_ref) == result.merge_sha

    # Every build reads a snapshot, never the branch: the first snapshot still names the tree it
    # named, even though the branch has moved on since.
    assert await git_.rev_parse(result.snapshot.ref) == result.snapshot.sha

    with pytest.raises(FR.IngestError):
        await git_.exec(["checkout", "-b", "elsewhere"])
        await FR.ingest(git_, source_dir=source, source=provenance, run_id=RUN_ID,
                        integration_branch="integration")


async def test_ingest_repoints_a_stale_migrate_branch_to_the_correct_sha(tmp_path: Path) -> None:
    """D115/ADR-0118: `migrate/<repo>` is created with `force=True` because a prior, unrelated
    process (or a previous crashed attempt) may have already left that name pointing somewhere
    else. `ingest()` must correct it, not refuse or silently leave it stale."""
    mono = tmp_path / "mono"
    await _init_repo(mono, files={"README.md": "monorepo\n"})
    await _sh(mono, "checkout", "-b", "integration")
    source = tmp_path / "filtered"
    await _init_repo(source, files={"ts/@acme/billing/index.ts": "export const x = 1;\n"})
    head = (await _sh(source, "rev-parse", "HEAD")).stdout_tail.strip()
    git_ = Git(mono, timeout_s=60)
    provenance = FR.SourceProvenance(repo_id=REPO_ID, sha=head)

    # Seed `migrate/<repo>` pointing at some unrelated, wrong sha (the integration branch's own
    # initial commit) BEFORE ingest ever runs.
    stale_target = await git_.rev_parse("integration")
    await git_.create_branch(f"migrate/{REPO_ID}", stale_target)
    assert await git_.rev_parse(f"migrate/{REPO_ID}") == stale_target

    result = await FR.ingest(
        git_, source_dir=source, source=provenance, run_id=RUN_ID,
        integration_branch="integration",
    )

    assert await git_.rev_parse(f"migrate/{REPO_ID}") != stale_target
    assert await git_.rev_parse(f"migrate/{REPO_ID}") == result.merge_sha


async def test_ingest_makes_migrate_branch_resolvable_for_promote_one_pr_first_check(
    tmp_path: Path,
) -> None:
    """D115: `_promote_one_pr`'s very first precondition check resolves `migrate/<repo_id>` and
    refuses if it's `None`. This proves that check now passes after a real `ingest()`, without
    driving the whole `fleet pr` CLI (item 5 of the task-50 brief's test list)."""
    mono = tmp_path / "mono"
    await _init_repo(mono, files={"README.md": "monorepo\n"})
    await _sh(mono, "checkout", "-b", "integration")
    source = tmp_path / "filtered"
    await _init_repo(source, files={"ts/@acme/billing/index.ts": "export const x = 1;\n"})
    head = (await _sh(source, "rev-parse", "HEAD")).stdout_tail.strip()
    git_ = Git(mono, timeout_s=60)
    provenance = FR.SourceProvenance(repo_id=REPO_ID, sha=head)

    assert await git_.resolve(f"migrate/{REPO_ID}") is None  # pre-condition: not yet reachable

    result = await FR.ingest(
        git_, source_dir=source, source=provenance, run_id=RUN_ID,
        integration_branch="integration",
    )

    resolved = await git_.resolve(f"migrate/{REPO_ID}")
    assert resolved is not None
    assert resolved == result.merge_sha


async def test_snapshot_refs_are_monotonic_and_immutable(repo: Path) -> None:
    """`seq` is derived from the refs git already holds, not from a SQLite counter: a crash
    between "increment" and "create" would otherwise let two different trees answer to one
    `attempts.integration_ref`."""
    git_ = Git(repo, timeout_s=60)
    first = await FR.integration_snapshot(git_, RUN_ID, tip=BRANCH)
    assert first.seq == 0

    (repo / "a.txt").write_text("moved on\n")
    await git_.add_all()
    await git_.commit("later work")

    second = await FR.integration_snapshot(git_, RUN_ID, tip=BRANCH)
    assert second.seq == 1
    assert second.sha != first.sha
    assert await git_.rev_parse(first.ref) == first.sha


@pytest.mark.integration
@pytest.mark.skipif(
    shutil.which("git-filter-repo") is None,
    reason="git-filter-repo is not installed on this host; history rewriting is unproven here",
)
async def test_relocate_rewrites_history_into_the_monorepo_path(tmp_path: Path) -> None:
    """The real rewrite: paths move IN HISTORY, so `git log --follow` still works after migration
    (§3.3 step 1) — which a copy-the-tree implementation would silently lose.

    A tip-only check is worthless here, because the thing that goes wrong is invisible at the tip:
    `cp -r && git commit` produces exactly the same worktree. So this asserts on **every commit**
    — the tree of the ROOT commit, three commits behind the tip, must already be relocated — and
    on `git log --follow`, which is the user-visible reason §3.3 rewrites rather than copies.
    """
    src = tmp_path / "src-repo"
    await _init_repo(src, files={"index.ts": "export const x = 1;\n"})
    await _write_and_commit(src, "index.ts", "export const x = 2;\n", "second")
    await _write_and_commit(src, "index.ts", "export const x = 3;\n", "third")
    before = (await _git_out(src, "rev-list", "--count", "HEAD")).strip()
    assert before == "3"

    await FR.relocate(src, FR.RelocationSpec(dest_path="ts/@acme/billing"))

    assert (src / "ts/@acme/billing/index.ts").exists()
    assert not (src / "index.ts").exists(), "the pre-migration path must be gone from the tree"

    # EVERY commit, not just the tip: `--name-only` over the whole history.
    listed = await _git_out(src, "log", "--name-only", "--format=", "--all")
    paths = [line for line in listed.splitlines() if line.strip()]
    assert paths, "the rewrite produced a history with no paths at all"
    assert all(p.startswith("ts/@acme/billing/") for p in paths), paths

    # The root commit specifically — the one a copy-the-tree implementation cannot have moved.
    root = (await _git_out(src, "rev-list", "--max-parents=0", "HEAD")).strip()
    root_tree = await _git_out(src, "ls-tree", "-r", "--name-only", root)
    assert root_tree.split() == ["ts/@acme/billing/index.ts"], root_tree

    # The point of rewriting instead of copying: history survives the move.
    followed = await _git_out(
        src, "log", "--follow", "--format=%s", "--", "ts/@acme/billing/index.ts"
    )
    assert followed.split() == ["third", "second", "initial"], followed


@pytest.mark.integration
@pytest.mark.skipif(
    shutil.which("git-filter-repo") is None,
    reason="git-filter-repo is not installed on this host; history rewriting is unproven here",
)
async def test_the_retry_of_a_relocation_reproduces_it_exactly(tmp_path: Path) -> None:
    """ADR-0014 retries a Phase 3 task up to three times, so attempt 2 must land the same tree as
    attempt 1 — modelled here the way `cli._ingest_one` really does it: rmtree, re-clone from the
    Phase 2 worktree, relocate again.

    Two claims, both real:

    * `--force` (unconditional in `filter_repo_argv`) is load-bearing. A fresh `git clone` still
      carries a reflog, which is exactly what git-filter-repo's freshness heuristic refuses; a
      retry without `--force` therefore dies for a reason unrelated to the migration, burns a
      rung of the ladder and reaches `REQUIRES_HUMAN_INTERVENTION` over a heuristic.
    * The rewrite is deterministic. The retry's paths AND its commit subjects must match the
      first attempt's; a rewrite that varied per run would make `attempts.commit_sha` a fact
      about scheduling luck.
    """
    source = tmp_path / "source"
    await _init_repo(source, files={"index.ts": "export const x = 1;\n"})
    await _write_and_commit(source, "index.ts", "export const x = 2;\n", "second")
    spec = FR.RelocationSpec(dest_path="ts/@acme/billing")

    async def one_attempt(n: int) -> tuple[str, str]:
        clone = tmp_path / f"clone-{n}"
        if clone.exists():
            shutil.rmtree(clone)
        await _sh(tmp_path, "clone", "--no-local", str(source), str(clone))
        await FR.relocate(clone, spec)
        return (
            await _git_out(clone, "ls-tree", "-r", "--name-only", "HEAD"),
            await _git_out(clone, "log", "--format=%s"),
        )

    first = await one_attempt(1)
    second = await one_attempt(2)
    assert first[0].split() == ["ts/@acme/billing/index.ts"], first[0]
    assert second == first, "the retry did not reproduce the first attempt's rewrite"


@pytest.mark.integration
@pytest.mark.skipif(
    shutil.which("git-filter-repo") is None,
    reason="git-filter-repo is not installed on this host; history rewriting is unproven here",
)
async def test_relocating_an_ALREADY_relocated_clone_nests_the_destination(
    tmp_path: Path,
) -> None:
    """A sharp edge, pinned so it cannot be discovered in production: `relocate()` is NOT
    idempotent against the same directory.

    This is not currently a live bug — `cli._ingest_one` rmtrees and re-clones before every
    attempt, and `relocate`'s own docstring requires a THROWAWAY clone. It is recorded because
    the unconditional `--force` that makes retries possible is the same flag that turns
    git-filter-repo's loud refusal of an already-filtered repo into a silent second
    `--path-rename`. The failure mode is a monorepo path of `ts/@acme/billing/ts/@acme/billing/`
    that every later phase treats as real, so any future caller that reuses a clone directory
    breaks the layout without an error anywhere.

    If `relocate()` ever grows an idempotency guard, this test is the one that must be inverted.
    """
    src = tmp_path / "src-repo"
    await _init_repo(src, files={"index.ts": "export const x = 1;\n"})
    spec = FR.RelocationSpec(dest_path="ts/@acme/billing")

    await FR.relocate(src, spec)
    await FR.relocate(src, spec)

    nested = await _git_out(src, "ls-tree", "-r", "--name-only", "HEAD")
    assert nested.split() == ["ts/@acme/billing/ts/@acme/billing/index.ts"], nested


# --------------------------------------------------------------------------------------
# github.py — PR creation and, critically, PR STATE INGESTION
# --------------------------------------------------------------------------------------
MERGED_JSON = (
    '{"state":"MERGED","mergedAt":"2026-07-01T09:15:00Z",'
    '"mergeCommit":{"oid":"abc1234def5678901234567890abcdef12345678"}}'
)
OPEN_JSON = '{"state":"OPEN","mergedAt":null,"mergeCommit":null}'


def test_pr_view_parsing_produces_the_MERGED_that_gates_the_fleet() -> None:
    """`MERGED` is a fact about GitHub; three gates consume it and nothing else produces it, so a
    parser that loses it deadlocks the fleet after wave 0 (§3.4 step 5)."""
    merged = GH.parse_pr_view("https://x/pull/1", MERGED_JSON)
    assert merged.state is PrState.MERGED
    assert merged.is_merged and merged.is_terminal
    assert merged.merged_at is not None and merged.merged_at.year == 2026
    assert merged.merge_commit_sha == "abc1234def5678901234567890abcdef12345678"

    still_open = GH.parse_pr_view("https://x/pull/2", OPEN_JSON)
    assert still_open.state is PrState.OPEN
    assert still_open.merged_at is None and still_open.merge_commit_sha is None
    assert not still_open.is_terminal


def test_an_unreadable_pr_state_raises_instead_of_defaulting_to_open() -> None:
    """An assumed OPEN holds every dependent's Phase 4 precondition open forever, and a merged
    dependency would never release its `blocked_by`. Fail loud instead (Rule 11)."""
    with pytest.raises(GH.GhError):
        GH.parse_pr_view("https://x/pull/3", '{"state":"QUEUED"}')
    with pytest.raises(GH.GhError):
        GH.parse_pr_view("https://x/pull/4", "not json at all")
    with pytest.raises(GH.GhError):
        GH.parse_pr_view("https://x/pull/5", '{"state":"MERGED","mergedAt":"yesterday"}')


async def test_sync_polls_only_non_terminal_prs_with_the_spec_field_set() -> None:
    """One `gh` call per OPEN PR per interval across a 250-PR fleet (§3.4 step 5): re-polling a
    terminal PR spends the `git_net` semaphore on an answer that cannot change."""
    runner = ScriptedRunner(stdout=MERGED_JSON)
    cli = GH.GitHubCli(runner=runner)
    statuses = await cli.sync(
        [
            GH.PrSyncItem("https://x/pull/1", PrState.DRAFTED),
            GH.PrSyncItem("https://x/pull/2", PrState.MERGED),
            GH.PrSyncItem("https://x/pull/3", PrState.CLOSED),
            GH.PrSyncItem("https://x/pull/4", PrState.HELD),
        ]
    )
    assert [s.url for s in statuses] == ["https://x/pull/1", "https://x/pull/4"]
    assert len(runner.calls) == 2
    assert runner.calls[0] == (
        "gh", "pr", "view", "https://x/pull/1", "--json", "state,mergedAt,mergeCommit",
    )
    assert all(s.state is PrState.MERGED for s in statuses)


async def test_create_pr_returns_the_url_and_keeps_the_body_out_of_argv(tmp_path: Path) -> None:
    """`--body-file`, not `--body`: a PR body quotes build logs and source URLs, and argv is
    world-readable in the process table. The URL is the Phase 4 success criterion, so a `gh` that
    printed none must raise rather than return None."""
    body = tmp_path / "body.md"
    body.write_text("## Migration\nsecret-ish build log\n")
    runner = ScriptedRunner(stdout="https://github.com/acme/mono/pull/42\n")
    cli = GH.GitHubCli(runner=runner)

    url = await cli.create_pr(
        base="integration", head=BRANCH, title="Migrate acme/billing",
        body_file=body, draft=True,
    )
    assert url == "https://github.com/acme/mono/pull/42"
    argv = runner.calls[0]
    assert "--body-file" in argv and str(body) in argv
    assert "--draft" in argv
    assert not any("secret-ish" in part for part in argv)

    quiet = GH.GitHubCli(runner=ScriptedRunner(stdout=""))
    with pytest.raises(GH.GhError):
        await quiet.create_pr(base="integration", head=BRANCH, title="t", body_file=body)


async def test_a_missing_gh_is_named_not_retried_forever() -> None:
    """`gh` absent or unauthenticated is an operator-visible condition, not a transient the poll
    loop should retry for 48 hours.

    The double is `RaisingRunner`, not `ScriptedRunner(exit_code=127, ...)`: no shell is ever
    involved, so a genuinely missing `gh` is `FileNotFoundError` raised in the parent — never an
    exit code on a `ProcResult` that was never produced.
    """
    cli = GH.GitHubCli(runner=RaisingRunner(FileNotFoundError("gh")))
    with pytest.raises(GH.GhUnavailableError):
        await cli.view("https://x/pull/1")
    assert await cli.available() is False


async def test_gh_exit_127_is_an_ordinary_failure_not_a_missing_binary() -> None:
    """`exit_code == 127` on a returned `ProcResult` is dead code as a "missing binary" signal:
    nothing in this stack can produce it, because there is no shell to apply the "command not
    found" convention. A `gh` that RAN and exited 127 of its own accord is an ordinary `GhError`,
    not `GhUnavailableError`."""
    cli = GH.GitHubCli(runner=ScriptedRunner(exit_code=127, started=True, stderr="boom"))
    with pytest.raises(GH.GhError) as exc_info:
        await cli.view("https://x/pull/1")
    assert not isinstance(exc_info.value, GH.GhUnavailableError)


async def test_gh_a_passed_deadline_is_not_mistaken_for_a_missing_binary() -> None:
    """`not started` is `util.proc.run`'s call-past-deadline synthesis (§7.1) — never a missing
    binary. Misreading it as `GhUnavailableError` blames the operator's PATH for the fleet's own
    clock instead of surfacing the retryable clock failure it actually is."""
    never_ran = ScriptedRunner(
        exit_code=124,
        started=False,
        timed_out=True,
        stderr="deadline had already passed; process was not started",
    )
    cli = GH.GitHubCli(runner=never_ran)
    with pytest.raises(GH.GhError) as exc_info:
        await cli.view("https://x/pull/1")
    assert not isinstance(exc_info.value, GH.GhUnavailableError)
    assert "deadline had already passed" in str(exc_info.value)


async def test_available_reports_false_for_a_genuinely_missing_binary() -> None:
    """D39's surviving half: `available()` must still answer `False`, not raise, when `gh` is
    genuinely not on PATH — that is a settled true negative, not an indeterminate probe. The
    double is `RaisingRunner`, matching `_exec`'s own `FileNotFoundError` case: no shell is ever
    involved, so a missing binary never comes back as a `ProcResult` at all."""
    cli = GH.GitHubCli(runner=RaisingRunner(FileNotFoundError("gh")))
    assert await cli.available() is False


async def test_available_reports_false_for_a_settled_unauthenticated_exit() -> None:
    """The case that proves the fix is not a blanket raise-on-any-failure (mirroring `d37f4ba`'s
    equivalent case for the git probes): `gh auth status` that actually RAN and exited non-zero —
    no credential, or a bad one — is a settled "no", and `available()` must still return `False`
    rather than raise. `gh` converting an unauthenticated exit into `False` (not an exception) is
    exactly what this module's docstring promises callers."""
    cli = GH.GitHubCli(runner=ScriptedRunner(exit_code=1, started=True, stderr="not logged in"))
    assert await cli.available() is False


async def test_available_does_not_report_false_for_an_unsettled_probe() -> None:
    """D39, the surviving half: before this fix, `available()`'s `except GhError: return False`
    caught EVERY `GhError` — including a passed-deadline probe, because `GhUnavailableError`
    subclasses `GhError` — so "gh is not installed" and "we could not find out" both came back as
    the identical `False`. A probe that never started must not settle the question either way:
    it raises instead of guessing "no"."""
    never_ran = ScriptedRunner(
        exit_code=124,
        started=False,
        timed_out=True,
        stderr="deadline had already passed; process was not started",
    )
    cli = GH.GitHubCli(runner=never_ran)
    with pytest.raises(GH.GhError) as exc_info:
        await cli.available()
    assert not isinstance(exc_info.value, GH.GhUnavailableError)


async def test_available_does_not_report_false_for_a_deadline_kill() -> None:
    """The other unsettled shape (started, then killed at the deadline, `timed_out=True` alone):
    also not a verdict, so also not `False`. Distinct from the never-started case above per
    `util.proc.no_verdict`'s ordering, but both must raise rather than collapse to "not
    installed"."""
    killed = ScriptedRunner(
        exit_code=124, started=True, timed_out=True, stderr="killed at deadline"
    )
    cli = GH.GitHubCli(runner=killed)
    with pytest.raises(GH.GhError) as exc_info:
        await cli.available()
    assert not isinstance(exc_info.value, GH.GhUnavailableError)
    assert "killed at deadline" in str(exc_info.value)


@pytest.mark.integration
@pytest.mark.skipif(
    shutil.which("gh") is None,
    reason="gh is not installed on this host; live PR create/sync is unproven here",
)
async def test_real_gh_reports_its_auth_state() -> None:
    """The only test that touches the real binary. It asserts `available()` answers rather than
    raising, so an unauthenticated host is a fact this code states, never a silent pass.

    Read the ceiling honestly: `gh` IS installed here (`tools/bin/gh`, v2.97.0) but there is no
    GitHub credential and this harness deliberately creates nothing on github.com, so what runs
    is `gh auth status` and what is proven is that `GitHubCli` spawns the real binary, survives
    its non-zero exit, and converts that into `False` instead of an exception the 48-hour poll
    loop would retry forever. `create_pr` and `pr view` against a live forge remain unproven
    here — see `docs/INTEGRATION_HONESTY.md`.
    """
    cli = GH.GitHubCli()
    authenticated = await cli.available()
    assert isinstance(authenticated, bool)
    # Not `assert authenticated is False`: a host that DOES have a credential must not fail this
    # test. The invariant is that the answer is produced, not what it is.
    if not authenticated:
        # …and that the unauthenticated path is the *named* one, not a crash on the way there.
        with pytest.raises(GH.GhError):
            await cli.view("https://github.com/acme/mono/pull/1")


@pytest.mark.integration
@pytest.mark.skipif(
    shutil.which("gh") is None,
    reason="gh is not installed on this host; live PR create/sync is unproven here",
)
async def test_the_real_gh_binary_is_the_one_this_harness_would_invoke() -> None:
    """`gh` resolves to a real, runnable executable of a version whose `--json` surface we parse.

    Why it matters: every other `gh` test in this file replays recorded JSON through an injected
    runner. That proves the parser and the argv; it cannot notice that the binary on PATH is
    absent, a shell wrapper, or a major version whose `pr view --json state,mergedAt,mergeCommit`
    means something else. This is the cheapest check that closes that specific gap, and it is
    also the *only* thing about `gh` that can be proven without a forge credential.
    """
    result = await run(["gh", "--version"], timeout_s=60)
    assert result.started and result.exit_code == 0, result.stderr_tail
    first = result.stdout_tail.splitlines()[0]
    assert first.startswith("gh version "), first
    major = int(first.removeprefix("gh version ").split(".")[0])
    assert major >= 2, f"the recorded `pr view --json` fixtures are a gh 2.x surface: {first}"
