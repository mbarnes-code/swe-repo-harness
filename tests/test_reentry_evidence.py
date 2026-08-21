"""`orchestrator.reentry.evidence_holds` — the four per-phase DURABLE evidence predicates.

These tests bind three things that are easy to state and easy to get wrong.

**1. The predicate is not `preconditions_hold`, and for Phase 4 it must actively DISAGREE with the
implementation it mirrors.** `workers/rdepverify.preconditions_hold` answers `True` when the BUILD
row is missing — correct for an admission gate ("no BUILD row is a first admission by the runner")
and catastrophic for evidence, because a missing BUILD row is the state of every repo that has
never built. `design-resume-step5.md` §3 claims each clause "duplicates a fact … so the two cannot
disagree"; for this row that is false, and
`test_verify_evidence_refuses_the_missing_build_row_that_rdepverify_admits` is what keeps the
divergence from being "fixed" back into agreement by a later reader.

**2. A fresh repo answers `False` at every phase, and that has no promotion effect.** Those are two
separate facts and each needs its own assertion: the predicate's answer
(`test_a_fresh_repo_has_no_evidence_at_any_phase`) and the walk's behaviour
(`test_a_fresh_repo_is_asked_nothing_at_all`). A test of only the first would stay green under a
`phase_floor` that scanned upward, which is the exact defect §3(b) is written about.

**3. Evidence is read lazily.** `phase_floor` asks only for phases strictly below the frontier.
`test_phase_floor_asks_evidence_only_for_phases_strictly_below_the_frontier` records every lookup
and fails on one at or above it — without that recording, "lazy" is a docstring sentence with no
mechanism behind it, and an eager `phase_floor` would keep every floor-result test green.

The Phase-1 and Phase-3 predicates are restatements of helpers that live in
`workers/clone.py` and `workers/buildverify.py` (importing those modules would fire
`@register_worker` and drag the LLM and sandbox stacks into the resume driver).
`test_the_restated_filesystem_facts_agree_with_the_worker_helpers_they_mirror` calls both sides
over one fixture matrix, so the restatement cannot silently drift from its original.

Fixtures are real git repositories driven through the real `Git` wrapper: the predicates exist to
tell "the ref is reachable" from "a SHA that merely parses", and a stub `Git` would answer both
from the same dict.
"""

from __future__ import annotations

import ast
import re
import subprocess
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, cast

import pytest

from fleet.models.enums import Phase, RepoStatus
from fleet.orchestrator import reentry
from fleet.orchestrator.reentry import (
    MIRROR_CACHE_SUBDIR,
    EvidenceRow,
    RepoEvidence,
    evidence_holds,
    phase_floor,
    resume_floor,
)
from fleet.vcs.git import Git

REPO_ID = "acme/widget"
BRANCH = f"migrate/{REPO_ID}"
DEST = "java/com/acme/widget"


# ---------------------------------------------------------------------------------------
# fixtures — real git, hermetic env
# ---------------------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    """A real `git`, with every ambient config source cut off, so a developer's `~/.gitconfig`
    cannot change what these assertions mean."""
    result = subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={
            "GIT_AUTHOR_NAME": "Fleet Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Fleet Fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "HOME": str(cwd),
            "PATH": "/usr/bin:/bin:/usr/local/bin",
        },
    )
    return result.stdout.strip()


def _write(path: Path, text: str) -> None:
    """Sync file access, kept out of the async test bodies (ruff ASYNC240)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class _Fleet:
    """One repo materialised the way Phases 1–3 leave it, plus the two commit pointers."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.cache = root / "cache" / "git"
        self.work = root / "work"
        self.worktree = self.work / REPO_ID
        self.mirror = self.cache / "acme-widget.git"
        self.transform_sha = ""
        self.build_sha = ""

    def repo(self, **overrides: Any) -> RepoEvidence:
        base = {
            "repo_id": REPO_ID,
            "mirror": self.mirror,
            "worktree": self.worktree,
            "dest": DEST,
            "has_verification_report": True,
        }
        return RepoEvidence(**{**base, **overrides})

    def rows(self, **overrides: EvidenceRow | None) -> dict[Phase, EvidenceRow | None]:
        """The four `phases` rows of a repo that has finished 1-3 and not started 4. Overrides
        are named by `Phase` member name (`BUILD=...`), which keeps the call sites readable."""
        rows: dict[Phase, EvidenceRow | None] = {
            Phase.SCAN: EvidenceRow(RepoStatus.SUCCEEDED, None),
            Phase.TRANSFORM: EvidenceRow(RepoStatus.SUCCEEDED, self.transform_sha),
            Phase.BUILD: EvidenceRow(RepoStatus.SUCCEEDED, self.build_sha),
            Phase.VERIFY: EvidenceRow(RepoStatus.PENDING, None),
        }
        rows.update({Phase[name]: row for name, row in overrides.items()})
        return rows


@pytest.fixture
def fleet(tmp_path: Path) -> Iterator[_Fleet]:
    """A repo that has been through Phases 1, 2 and 3: a mirror, a linked-shaped worktree with
    `migrate/<repo>` checked out, a commit on the branch (Phase 2's `post_commit_sha`), and a
    committed `<dest>/BUILD.bazel` (Phase 3's)."""
    state = _Fleet(tmp_path)
    state.worktree.mkdir(parents=True)
    _git(state.worktree, "init", "--initial-branch=main", ".")
    _write(state.worktree / "README.md", "widget\n")
    _git(state.worktree, "add", "--all")
    _git(state.worktree, "commit", "-m", "initial")

    _git(state.worktree, "checkout", "-b", BRANCH)
    _write(state.worktree / "src" / "main.java", "class Widget {}\n")
    _git(state.worktree, "add", "--all")
    _git(state.worktree, "commit", "-m", "relocate")
    state.transform_sha = _git(state.worktree, "rev-parse", "HEAD")

    _write(state.worktree / DEST / "BUILD.bazel", 'java_library(name = "widget")\n')
    _git(state.worktree, "add", "--all")
    _git(state.worktree, "commit", "-m", "buildgen")
    state.build_sha = _git(state.worktree, "rev-parse", "HEAD")

    state.cache.mkdir(parents=True)
    _git(state.cache, "clone", "--mirror", str(state.worktree), str(state.mirror))
    yield state


def _git_factory(path: Path) -> Git:
    """The predicates bind their own path; the factory only pins a timeout so a hung fixture fails
    the test rather than the session."""
    return Git(path, timeout_s=60)


async def _holds(state: _Fleet, phase: Phase, **overrides: Any) -> bool:
    rows = cast("Mapping[Phase, EvidenceRow | None]", overrides.pop("rows", state.rows()))
    return await evidence_holds(phase, rows=rows, repo=state.repo(**overrides), git=_git_factory)


# ---------------------------------------------------------------------------------------
# the four predicates, on a healthy repo
# ---------------------------------------------------------------------------------------


async def test_every_phase_holds_for_a_repo_that_really_did_the_work(fleet: _Fleet) -> None:
    """The baseline the negative cases are read against. Without it every `False` below could be
    produced by a predicate that is simply broken, and the suite would still be green."""
    rows = fleet.rows(BUILD=EvidenceRow(RepoStatus.SUCCEEDED, fleet.build_sha))
    for phase in Phase:
        assert await _holds(fleet, phase, rows=rows) is True, phase


# ---------------------------------------------------------------------------------------
# Phase 1 — clone.py's three facts
# ---------------------------------------------------------------------------------------


async def test_scan_evidence_fails_when_the_mirror_is_gone(fleet: _Fleet, tmp_path: Path) -> None:
    """A mirror the operator or `fleet gc` removed. `clone.preconditions_hold` refuses for the
    same reason: the thing Phase 1 claims to have materialised is not there."""
    assert await _holds(fleet, Phase.SCAN, mirror=tmp_path / "absent.git") is False


async def test_scan_evidence_fails_for_a_directory_that_is_not_a_bare_repo(
    fleet: _Fleet, tmp_path: Path
) -> None:
    """The half-created case, which is the whole reason `clone` checks `HEAD`+`objects` rather
    than `is_dir()`: a killed `git clone --mirror` leaves a directory that exists and is not a
    mirror, and treating it as one is how a repo gets stuck."""
    debris = tmp_path / "debris.git"
    debris.mkdir()
    assert await _holds(fleet, Phase.SCAN, mirror=debris) is False


async def test_scan_evidence_fails_when_the_worktree_was_reaped(
    fleet: _Fleet, tmp_path: Path
) -> None:
    """The reaper removed the checkout. The mirror survives, so a predicate that only looked at
    the mirror would answer `True` for a repo with nothing on disk to re-enter."""
    empty = tmp_path / "reaped"
    empty.mkdir()
    assert await _holds(fleet, Phase.SCAN, worktree=empty) is False


async def test_scan_evidence_fails_when_head_does_not_resolve(
    fleet: _Fleet, tmp_path: Path
) -> None:
    """`git init` and nothing else: a `.git` is present, so the cheap facts both pass, and only
    the `HEAD` read distinguishes an empty repo from a cloned one. This is the fact that costs a
    subprocess, and it is the one a shortcut would drop."""
    empty = tmp_path / "initialized-only"
    empty.mkdir()
    _git(empty, "init", "--initial-branch=main", ".")
    assert await _holds(fleet, Phase.SCAN, worktree=empty) is False


# ---------------------------------------------------------------------------------------
# Phase 2 — the pointer is checked AGAINST the branch, not merely parsed
# ---------------------------------------------------------------------------------------


async def test_transform_evidence_fails_when_the_migrate_branch_does_not_exist(
    fleet: _Fleet,
) -> None:
    """No `migrate/<repo>` means Phase 2 produced nothing that survived, whatever the column
    remembers."""
    _git(fleet.worktree, "checkout", "main")
    _git(fleet.worktree, "branch", "-D", BRANCH)
    assert await _holds(fleet, Phase.TRANSFORM) is False


async def test_transform_evidence_fails_when_the_column_is_empty(fleet: _Fleet) -> None:
    """A branch alone is not evidence: `phases.post_commit_sha` is the pointer that says which
    commit Phase 2 produced, and without it there is nothing to check against the branch."""
    rows = fleet.rows(TRANSFORM=EvidenceRow(RepoStatus.SUCCEEDED, None))
    assert await _holds(fleet, Phase.TRANSFORM, rows=rows) is False


async def test_transform_evidence_fails_for_a_sha_that_does_not_resolve(fleet: _Fleet) -> None:
    """A pointer into an object the repo does not have — a stale column, or a worktree recreated
    from a different mirror."""
    rows = fleet.rows(TRANSFORM=EvidenceRow(RepoStatus.SUCCEEDED, "0" * 40))
    assert await _holds(fleet, Phase.TRANSFORM, rows=rows) is False


async def test_transform_evidence_fails_for_a_sha_that_resolves_but_is_not_on_the_branch(
    fleet: _Fleet,
) -> None:
    """**The discriminating case, and the reason this clause is an ancestry question.**

    The commit is a real object in the repository, so any predicate that stops at "does it
    resolve" answers `True`. But a rollback or a force-reset has taken it off `migrate/<repo>`,
    which is exactly the state whose Phase-2 result must not be trusted — §11.5's authority table
    calls the column "a **pointer** into Git … never trusted over Git", so the pointer is checked
    against the branch rather than read as the answer.
    """
    orphan = fleet.transform_sha
    _git(fleet.worktree, "reset", "--hard", "HEAD~2")
    handle = Git(fleet.worktree, timeout_s=60)
    assert await handle.resolve(orphan) is not None, "fixture: the commit must still resolve"
    rows = fleet.rows(TRANSFORM=EvidenceRow(RepoStatus.SUCCEEDED, orphan))
    assert await _holds(fleet, Phase.TRANSFORM, rows=rows) is False


# ---------------------------------------------------------------------------------------
# Phase 3 — §5 row 5's named acceptance criterion
# ---------------------------------------------------------------------------------------


async def test_build_evidence_fails_when_the_dest_build_file_is_deleted(fleet: _Fleet) -> None:
    """§5 row 5, verbatim: "a fixture whose `<dest>/BUILD.bazel` is deleted yields
    `evidence_holds(r,3) == False`".

    Deleted from the **worktree**, not from history — which is the case that matters and the
    reason this reads the filesystem rather than the integration ref. The blob is still reachable
    from `post_commit_sha`, so a ref read answers `True` for a package that will not build; §6
    option C is written about precisely this repo.
    """
    build_file = fleet.worktree / DEST / "BUILD.bazel"
    assert build_file.exists(), "fixture: the file must be there before it is deleted"
    build_file.unlink()
    assert await _holds(fleet, Phase.BUILD) is False


async def test_build_evidence_fails_when_the_column_does_not_resolve(fleet: _Fleet) -> None:
    """Both halves of the clause are load-bearing: the `BUILD.bazel` is on disk here, so only the
    pointer check can produce the refusal."""
    rows = fleet.rows(BUILD=EvidenceRow(RepoStatus.SUCCEEDED, "0" * 40))
    assert (fleet.worktree / DEST / "BUILD.bazel").exists()
    assert await _holds(fleet, Phase.BUILD, rows=rows) is False


@pytest.mark.parametrize("phase", [Phase.TRANSFORM, Phase.BUILD])
async def test_a_dangling_post_commit_sha_is_false_and_never_raises(
    fleet: _Fleet, phase: Phase
) -> None:
    """**The class step 4 does not reconcile, and the reason the answer is a verdict not a raise.**

    §11.5 step 4 has a second selector — reconcile "any `phases` row whose `post_commit_sha` does
    not resolve on its branch" — that ADR-0087 §4 discloses as unimplemented: a phase row with a
    dangling pointer and no `RUNNING` task is never reconciled, so these two predicates meet
    unresolvable SHAs in production even after step 4 has run.

    The SHA here is well-formed and absent from the repository, which is what the unreconciled
    row actually looks like — not a malformed string a parser would reject. Three outcomes were
    possible and only one is defensible: `True` would leave a repo's floor above work nothing
    verified; a raise would abort the **whole** resume, so one repo's dangling pointer would stop
    the other 249 being reconciled at all; `False` costs one repo a re-run. This asserts the
    verdict, and by returning at all it asserts the absence of the raise.
    """
    dangling = "0" * 39 + "1"
    rows = fleet.rows(**{phase.name: EvidenceRow(RepoStatus.SUCCEEDED, dangling)})
    assert await _holds(fleet, phase, rows=rows) is False


# ---------------------------------------------------------------------------------------
# Phase 4 — the deliberate disagreement with `rdepverify`
# ---------------------------------------------------------------------------------------


async def test_verify_evidence_refuses_the_missing_build_row_that_rdepverify_admits(
    fleet: _Fleet,
) -> None:
    """**The trap this predicate exists to avoid, asserted directly against its mirror.**

    `rdepverify.preconditions_hold` returns `True` for a missing BUILD row and says why in its
    own comment. `evidence_holds` must return `False` for the same row: a missing BUILD row is
    the state of every repo that has never built, and a `True` here would let the backward walk
    stop at Phase 4 and leave a never-built repo's floor there. The two predicates are asked
    different questions, so the disagreement is the design, not a defect — and this test names
    the disagreement so nobody "restores consistency" by copying the carve-out across.
    """
    missing = fleet.rows(BUILD=None)
    assert await _holds(fleet, Phase.VERIFY, rows=missing) is False

    absent_key = fleet.rows()
    del absent_key[Phase.BUILD]
    assert await _holds(fleet, Phase.VERIFY, rows=absent_key) is False


async def test_verify_evidence_fails_when_the_build_row_is_not_succeeded(fleet: _Fleet) -> None:
    """Every non-`SUCCEEDED` status, not just the missing row: `DEGRADED` and `SKIPPED` are hard
    stops for the *walk* but they are not evidence that Phase 3 produced a testable package."""
    for status in RepoStatus:
        if status is RepoStatus.SUCCEEDED:
            continue
        rows = fleet.rows(BUILD=EvidenceRow(status, fleet.build_sha))
        assert await _holds(fleet, Phase.VERIFY, rows=rows) is False, status


async def test_verify_evidence_fails_without_a_persisted_verification_report(
    fleet: _Fleet,
) -> None:
    """The second half of the clause. `fleet pr` runs in a later process and reads the report back
    out of `findings`; a Phase 4 that left no report behind has produced nothing a resume can
    stand on, however green Phase 3 was."""
    assert await _holds(fleet, Phase.VERIFY, has_verification_report=False) is False


# ---------------------------------------------------------------------------------------
# the fresh repo — two separate facts
# ---------------------------------------------------------------------------------------


async def test_a_fresh_repo_has_no_evidence_at_any_phase(tmp_path: Path) -> None:
    """§5 row 5: "a fresh repo yields `False` at every phase". Nothing has been cloned, so there
    is no mirror, no worktree, no branch, no pointer and no report.

    `False`, not an exception — which is a real distinction and not pedantry. `Git` shells out
    with its bound path as `cwd`, so a `resolve()` against a worktree that does not exist raises
    `FileNotFoundError`; the first draft of `_transform_evidence` read Git before checking the
    directory and this test failed with that error rather than a verdict. `_worktree_present`
    (`workers/buildverify.preconditions_hold`'s second refusal) is what makes the criterion true.
    """
    state = _Fleet(tmp_path)
    rows: Mapping[Phase, EvidenceRow | None] = dict.fromkeys(Phase)
    for phase in Phase:
        verdict = await evidence_holds(
            phase,
            rows=rows,
            repo=state.repo(has_verification_report=False),
            git=_git_factory,
        )
        assert verdict is False, phase


async def test_a_fresh_repo_is_asked_nothing_at_all(tmp_path: Path) -> None:
    """The other half of §5 row 5 — "without any promotion effect (the caller never asks upward)"
    — and a different claim from the one above.

    A fresh repo's frontier is `SCAN`, and the backward walk starts *below* the frontier, so its
    range is empty. The evidence mapping is therefore never consulted: the floor is `SCAN` because
    of where the frontier is, not because four predicates answered `False`. This is what makes the
    §3(b) inversion unreachable — even a Phase-4 predicate that wrongly answered `True` (see
    `test_verify_evidence_refuses_the_missing_build_row_that_rdepverify_admits`) could not promote
    this repo, because nothing asks it.
    """
    state = _Fleet(tmp_path)
    rows: Mapping[Phase, EvidenceRow | None] = dict.fromkeys(Phase)
    asked: list[Phase] = []

    async def probe(phase: Phase) -> bool:
        asked.append(phase)
        return await evidence_holds(
            phase, rows=rows, repo=state.repo(has_verification_report=False), git=_git_factory
        )

    floor, gathered = await resume_floor(rows, probe)
    assert floor is Phase.SCAN
    assert asked == []
    assert gathered == {}


# ---------------------------------------------------------------------------------------
# laziness — the binding the controller ruling requires
# ---------------------------------------------------------------------------------------


class _RecordingEvidence(Mapping[Phase, bool]):
    """A mapping that remembers which phases were looked up.

    A `collections.abc.Mapping` implementation rather than a `dict` subclass, and the difference
    is not cosmetic: `Mapping.get` is the ABC's Python implementation and delegates to
    `__getitem__`, so one override records every access `phase_floor` can make. `dict.get` is a C
    fast path that does **not** route through `__getitem__`, so a `dict` subclass overriding only
    `__getitem__` would report an empty log and this test would pass for the wrong reason.
    """

    def __init__(self, values: Mapping[Phase, bool]) -> None:
        self._values = dict(values)
        self.asked: list[Phase] = []

    def __getitem__(self, phase: Phase) -> bool:
        self.asked.append(phase)
        return self._values[phase]

    def __iter__(self) -> Iterator[Phase]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


def _rows(*statuses: RepoStatus) -> dict[Phase, EvidenceRow | None]:
    return {phase: EvidenceRow(status, None) for phase, status in zip(Phase, statuses, strict=True)}


S = RepoStatus.SUCCEEDED
P = RepoStatus.PENDING
D = RepoStatus.DEGRADED


@pytest.mark.parametrize(
    ("statuses", "frontier", "expected_asked", "expected_floor"),
    [
        # frontier VERIFY: the walk asks BUILD, and stops there because evidence holds.
        ((S, S, S, P), Phase.VERIFY, [Phase.BUILD], Phase.VERIFY),
        # frontier BUILD: TRANSFORM holds, so SCAN is never reached either.
        ((S, S, P, P), Phase.BUILD, [Phase.TRANSFORM], Phase.BUILD),
        # frontier TRANSFORM: only SCAN is below it.
        ((S, P, P, P), Phase.TRANSFORM, [Phase.SCAN], Phase.TRANSFORM),
        # frontier SCAN: nothing is below it, so nothing is asked at all.
        ((P, P, P, P), Phase.SCAN, [], Phase.SCAN),
    ],
)
def test_phase_floor_asks_evidence_only_for_phases_strictly_below_the_frontier(
    statuses: tuple[RepoStatus, ...],
    frontier: Phase,
    expected_asked: list[Phase],
    expected_floor: Phase,
) -> None:
    """**The mechanism behind `phase_floor`'s laziness contract.**

    Without this, "a mapping that computes on demand is a supported argument" is a sentence, and
    an eager `phase_floor` that pre-read all four verdicts would keep every existing floor-result
    test green while making the sentence false — the controller ruling's stated reason for
    demanding a test rather than a convention.

    The recorded set is exact, in both directions: nothing at or above the frontier (the eager
    defect), and nothing below the phase the walk stopped at (the over-evaluation defect). All
    four verdicts are `True` here so the answer never depends on a phase not being asked.
    """
    evidence = _RecordingEvidence(dict.fromkeys(Phase, True))
    rows = cast("Mapping[Phase, Any]", _rows(*statuses))

    floor = phase_floor(rows, evidence)

    assert evidence.asked == expected_asked
    assert all(phase < frontier for phase in evidence.asked), evidence.asked
    assert floor is expected_floor


def test_a_hard_stop_ends_the_walk_before_any_evidence_below_it_is_computed() -> None:
    """The laziness and the ADR-0077 §5 hard stop interact, and the interaction is a cost claim.

    Evidence is `False` everywhere, so without the `DEGRADED` stop at `TRANSFORM` the walk would
    continue past it and ask `SCAN` too. It does not: the hard stop is tested *before* evidence,
    so `TRANSFORM` is never asked and `SCAN` is never reached. A driver that computed all four
    verdicts up front and classified afterwards would have paid for both reads anyway, which is
    what makes this an assertion about the mapping and not only about the returned floor.
    """
    evidence = _RecordingEvidence(dict.fromkeys(Phase, False))
    rows = cast("Mapping[Phase, Any]", _rows(S, D, S, P))

    assert phase_floor(rows, evidence) is Phase.BUILD
    assert evidence.asked == [Phase.BUILD]


async def test_resume_floor_probes_each_asked_phase_exactly_once_and_reports_what_it_asked() -> (
    None
):
    """`resume_floor` re-runs the pure `phase_floor` once per newly needed phase, so a probe must
    never be paid for twice; and it returns the verdicts it did gather, so a `--dry-run` report
    can say why a repo was demoted without a second pass over Git."""
    calls: list[Phase] = []

    async def probe(phase: Phase) -> bool:
        calls.append(phase)
        return False

    rows = cast("Mapping[Phase, EvidenceRow | None]", _rows(S, S, S, P))
    floor, gathered = await resume_floor(rows, probe)

    assert floor is Phase.SCAN
    assert calls == [Phase.BUILD, Phase.TRANSFORM, Phase.SCAN]
    assert gathered == {Phase.BUILD: False, Phase.TRANSFORM: False, Phase.SCAN: False}


# ---------------------------------------------------------------------------------------
# the restatements, bound to their originals
# ---------------------------------------------------------------------------------------


def test_the_restated_filesystem_facts_agree_with_the_worker_helpers_they_mirror(
    tmp_path: Path,
) -> None:
    """`reentry` restates three filesystem facts that `workers/clone.py` and
    `workers/buildverify.py` own, because importing those modules fires `@register_worker` and
    pulls the LLM and sandbox stacks into the resume driver. A restatement with nothing binding it
    is a copy waiting to drift, so both sides are called over one matrix and compared.

    The matrix carries the states that distinguish the predicates from a naive `exists()`: a bare
    directory, a directory with only `HEAD`, a directory with only `objects/`, and the real thing.
    A matrix of only "present" and "absent" would agree under any implementation.
    """
    from fleet.workers import buildverify, clone

    cases: list[Path] = []
    absent = tmp_path / "absent"
    cases.append(absent)

    bare = tmp_path / "bare-dir"
    bare.mkdir()
    cases.append(bare)

    head_only = tmp_path / "head-only"
    head_only.mkdir()
    (head_only / "HEAD").write_text("ref: refs/heads/main\n")
    cases.append(head_only)

    objects_only = tmp_path / "objects-only"
    (objects_only / "objects").mkdir(parents=True)
    cases.append(objects_only)

    real = tmp_path / "real.git"
    real.mkdir()
    (real / "HEAD").write_text("ref: refs/heads/main\n")
    (real / "objects").mkdir()
    (real / ".git").write_text("gitdir: elsewhere\n")
    cases.append(real)

    for case in cases:
        assert reentry._mirror_is_initialized(case) == clone._mirror_is_initialized(case), case
        assert reentry._worktree_is_initialized(case) == clone._worktree_is_initialized(case), case
        probe = RepoEvidence(repo_id=REPO_ID, mirror=case, worktree=case, dest=DEST)
        assert reentry._build_file_present(probe) == buildverify.files_present(
            case / DEST / "BUILD.bazel"
        ), case


# ---------------------------------------------------------------------------------------
# Rule 11 — an unknown phase is not a quiet `False`
# ---------------------------------------------------------------------------------------


async def test_an_unregistered_phase_raises_rather_than_defaulting_to_false() -> None:
    """A fifth `Phase` added without a predicate must stop the run. Defaulting to `False` would
    demote every repo past the new phase on every resume, silently — the failure mode Rule 11
    exists for, and the one a `dict.get(phase, False)` dispatch would produce."""
    with pytest.raises(ValueError, match="no step-5 evidence predicate"):
        await evidence_holds(
            cast("Phase", 99),
            rows={},
            repo=RepoEvidence(repo_id=REPO_ID, mirror=Path(), worktree=Path(), dest=DEST),
        )


async def test_build_evidence_fails_for_a_reaped_worktree_that_still_has_its_pointer(
    fleet: _Fleet, tmp_path: Path
) -> None:
    """**The reaped-worktree repo this whole subtask is written about, at Phase 3.**

    `phases(r,3).post_commit_sha` is populated and real — the run got that far — and the worktree
    is gone, which is what a reaper or an operator's `rm -rf` leaves. Every other Phase-3 test runs
    against a fixture that *has* a worktree, and the fresh-repo test short-circuits at `if not
    sha` before Git is reached, so until this fixture existed `_build_evidence`'s worktree guard
    was correct, present, and held there by nothing: no test and no mutation could express the one
    state it exists for.

    Without the guard this raises `FileNotFoundError` rather than returning `False` —
    `util.proc._run_locked` wraps `create_subprocess_exec(..., cwd=str(cwd))` in a `try:` whose
    only handler is `finally:`, with no `except OSError`, so the OS error propagates out of
    `Git.exec`. A raise is the outcome ADR-0088 §4 argues against at length: it would abort the
    whole resume rather than lower this one repo's floor.
    """
    reaped = tmp_path / "reaped"
    assert not reaped.exists(), "fixture: the worktree must be genuinely absent, not empty"
    rows = fleet.rows()
    assert rows[Phase.BUILD] is not None and rows[Phase.BUILD].post_commit_sha
    assert await _holds(fleet, Phase.BUILD, rows=rows, worktree=reaped) is False


def test_evidence_row_refuses_to_be_constructed_without_the_pointer_column() -> None:
    """**The mechanism for the one mistake no fixture can express.**

    `state.repository.PhaseRow` does not carry `post_commit_sha` and `state/repository.py` has no
    reader that returns it, so the natural projection from the row a subtask-7 caller already
    holds is `EvidenceRow(status=row.status)`. With a default on the field that expression
    constructs, type-checks and runs — and answers `False` at Phases 2 and 3 for every repo, so
    the walk never stops and the whole fleet is demoted to `SCAN` on every resume, silently.

    A test cannot catch that, because any fixture reaching this class has already answered the
    question. The signature is the only place the mistake is visible, so the field is required and
    omission is a `TypeError` at construction. Passing `None` explicitly stays legal: a phase that
    genuinely has no pointer is a real state, and this asserts both halves.
    """
    with pytest.raises(TypeError):
        EvidenceRow(RepoStatus.SUCCEEDED)  # type: ignore[call-arg]
    assert EvidenceRow(RepoStatus.SUCCEEDED, None).post_commit_sha is None


def test_the_mirror_cache_subdir_is_the_one_cli_writes() -> None:
    """`MIRROR_CACHE_SUBDIR` parsed out of `cli._scan_payloads`'s own source, not restated.

    `for_repo` appends this segment so no caller has to remember it, which is only safe while the
    segment is the one production writes. A caller passing the wrong root probes a directory no
    worker ever wrote, `_mirror_is_initialized` answers `False` for every repo, and the fleet's
    floor is `SCAN` — the same silent failure as I1, one layer out.

    The claim is parsed **out of** the source rather than compared text-to-text, so editing
    `cli.py`'s expression changes what this asserts. The segment is extracted from
    `_scan_payloads`'s body only — a whole-file grep would also match `_gc_impl` and the
    gazelle / resolve / ingest roots, which are different directories that are not this one — and
    the body is whitespace-normalised first, because a line-oriented search certifies a class as
    fixed when a wrapped match defeats it.
    """
    source = (Path(__file__).resolve().parents[1] / "src" / "fleet" / "cli.py").read_text()
    tree = ast.parse(source)
    bodies = [
        ast.get_source_segment(source, node) or ""
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_scan_payloads"
    ]
    assert len(bodies) == 1, f"expected exactly one `_scan_payloads`, found {len(bodies)}"

    normalised = re.sub(r"\s+", " ", bodies[0])
    segments = re.findall(r'settings\.config\.run\.cache_dir\s*/\s*"([^"]+)"', normalised)
    assert segments == [MIRROR_CACHE_SUBDIR], (
        f"`cli._scan_payloads` builds the git mirror root as "
        f"`run.cache_dir / {segments}`, but `reentry.MIRROR_CACHE_SUBDIR` is "
        f"{MIRROR_CACHE_SUBDIR!r}. `RepoEvidence.for_repo` would probe a directory no worker "
        f"writes and every repo's floor would be SCAN."
    )


def test_a_membership_test_on_the_lazy_evidence_answers_instead_of_raising() -> None:
    """`_EvidenceWanted` is not a `KeyError`, which is what makes the laziness work — and which
    would also let an internal control-flow exception out of `phase in evidence`, since
    `Mapping.__contains__` catches only `KeyError`. Inert while `phase_floor` uses `.get`
    exclusively; asserted so the next edit to either side does not have to know that."""
    lazy = reentry._LazyEvidence({Phase.SCAN: True})
    assert Phase.SCAN in lazy
    assert Phase.BUILD not in lazy


def test_for_repo_derives_the_paths_production_writes(tmp_path: Path) -> None:
    """The derivations are `clone.CloneWorker._mirror_path` (`<cache>/<slug(repo_id)>.git`) and
    `RunContext.worktree` (`<work_dir>/<repo_id>`). A step 5 that probed a directory no worker
    ever wrote would answer `False` for every repo and demote the whole fleet to `SCAN`."""
    repo = RepoEvidence.for_repo(
        REPO_ID, cache_dir=tmp_path / "cache", work_dir=tmp_path / "work", dest=DEST
    )
    assert repo.mirror == tmp_path / "cache" / MIRROR_CACHE_SUBDIR / "acme-widget.git"
    assert repo.worktree == tmp_path / "work" / REPO_ID
    assert repo.migrate_branch() == BRANCH
    assert repo.has_verification_report is False
