"""Git is the record of code state: one commit per task, trailers as the index (SPEC §3.2 step 6).

Replaces the deleted `fleet.state.mutations` write-ahead journal (ADR-0024). That journal stored
tree SHAs, patch blobs, and a manual rollback log in SQLite — a shadow version-control system,
which duplicates what git already stores atomically and therefore drifts away from it across a
crash. The split here is absolute: **SQLite manages orchestration state (tasks, attempts, retries,
cost); git manages code state.** On any disagreement git is authoritative and the row is corrected,
never the reverse.

**The idempotency key.** `Fleet-Patch-Id` is `sha256` over the sorted `path\\x1f sha256(diff)` of
every patch in the result — a pure function of CONTENT, never of attempt number, wall clock, or row
id. Two rungs proposing byte-identical edits therefore produce the same id, and the same rung
re-executed after a crash reproduces it. It rides in the commit rather than in a SQLite unique
index, so it survives a lost database and is checkable from a bare clone.

**The guard is two reads, and the second one is the one that decides.**

    (a) git log --format='%H %(trailers:key=Fleet-Patch-Id,valueonly)' <pre_commit_sha>..<branch>
    (b) git apply --check --reverse <patch>

(a) is scoped to `<phases.pre_commit_sha>..migrate/<repo>` — never the branch's full history — and
it is authoritative *only together with* (b). A trailer proves the patch was once committed; it
does not prove the effect survived a later rebase or revert. The failure this prevents actually
happened: a full-history trailer grep alone permanently skipped a patch whose hunk a rebase had
dropped, and the consumer shipped a PR pointing at a path that no longer existed. So (b) — "is the
change present in the CURRENT tree?" — is necessary before any skip. SPEC §3.2 step 6.1 also notes
(b) alone suffices, because the change being present by some other route is still the change being
present; (a) then supplies the commit SHA recorded on the `attempts` row.

**Two anchors, and they are not interchangeable.** `phases.pre_commit_sha` precedes the phase's
*first* mutation; `tasks.pre_commit_sha` precedes *this* task's. Crash-discard of one task resets to
the TASK anchor. Resetting a crashed task to the phase anchor is forbidden and `discard_task()`
cannot express it: it would delete the commits of earlier tasks whose rows are already `DONE` and
will therefore never re-run, leaving the phase's success criterion to pass on a tree missing most of
its rewrites. Whole-phase rollback is a separate, explicitly named call.

Writes no SQL (§11.5 single-writer rule): the runner persists what these helpers return.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol
from uuid import UUID

from fleet.vcs.git import Git, GitCommandError, GitError

__all__ = [
    "CONTRACT_ROLLBACK_ID_TRAILER",
    "PATCH_ID_TRAILER",
    "TASK_ID_TRAILER",
    "TRAILER_PREFIX",
    "CommitOutcome",
    "FleetTrailers",
    "GuardOutcome",
    "PatchApplyError",
    "PatchLike",
    "RevertOutcome",
    "RollbackAnchorError",
    "RollbackIndeterminateError",
    "apply_and_commit",
    "commits_in_range",
    "contract_rollback_shas_in_range",
    "discard_task",
    "find_task_commit",
    "guard",
    "patch_id",
    "record_task_anchor",
    "revert_and_commit",
    "rollback_phase",
    "scoped_range",
]

TRAILER_PREFIX: Final = "Fleet-"
RUN_ID_TRAILER: Final = "Fleet-Run-Id"
REPO_ID_TRAILER: Final = "Fleet-Repo-Id"
PHASE_TRAILER: Final = "Fleet-Phase"
TASK_ID_TRAILER: Final = "Fleet-Task-Id"
ATTEMPT_TRAILER: Final = "Fleet-Attempt"
PATCH_ID_TRAILER: Final = "Fleet-Patch-Id"
CONTRACT_ROLLBACK_ID_TRAILER: Final = "Fleet-Contract-Rollback-Id"
"""ADR-0122 Decision 5: stamped on every revert commit in one hoist-rollback series, all sharing
the SAME value (the failed contract's own `contract_id`) — the trailer alone does not say which
original sha a given revert commit reverts; `contract_rollback_shas_in_range` below reads each
matching commit's body for that (Decision 5's own "This reverts commit <sha>." text)."""


class PatchLike(Protocol):
    """Structural view of `models.tasks.FilePatch`.

    A Protocol rather than the model import so the id is computable from anything carrying a path
    and a unified diff — including a test's two-line stand-in — and so `fleet.vcs` does not depend
    on the pydantic layer to answer a hashing question (CLAUDE.md guardrail 3).
    """

    @property
    def path(self) -> str: ...

    @property
    def diff(self) -> str: ...


class PatchApplyError(GitError):
    """`git apply` refused a patch that the pre-check said should apply, or the check itself
    failed. Loud by construction (Rule 11): the repair loop needs the verbatim refusal."""


class RollbackAnchorError(GitError):
    """A rollback was asked to rewind to an anchor that is missing, or that is not an ancestor of
    the tip it would rewind — i.e. a "rollback" that would move history sideways or forward.

    A SETTLED fact about history: the probe that decided this actually ran and actually answered.
    See `RollbackIndeterminateError` for the sibling case where the probe never got to answer at
    all — the two are deliberately not the same type (four-state-collapse discipline, D29/D34-45).
    """


class RollbackIndeterminateError(GitError):
    """`discard_task` could not establish whether its rollback succeeded: one of its own git calls
    — `resolve`, `is_ancestor`, or the `reset --hard` / `clean -fdx` that actually mutate the tree
    — never settled or failed outright, on the SAME clock that most often triggered the rollback
    in the first place (the deadline that failed the original patch is still expired when
    `discard_task` runs). "Could not determine" must never collapse into a settled verdict (D42's
    own discipline, one layer up): a tree that could not be proven discarded is not a rejected
    patch, and reporting it as one spends a repair rung on a tree in an unknown state.

    Deliberately a bare `GitError`, never a `GitCommandError` or `PatchApplyError` subclass:
    `workers/rewrite.py`'s `land_patches` calls `discard_task` from inside
    `except (PatchApplyError, GitCommandError)`, and `RewriteWorker.run` catches the same pair
    around `land_patches` itself. Either a plain `GitCommandError` escaping `discard_task`, or this
    exception subclassing one, would be swallowed there and misreported as `PATCH_REJECTED` — the
    original defect (`d37f4ba`'s D42 fix interacting with the pre-existing rollback path). Distinct
    from `RollbackAnchorError` (a settled "no") so a caller that does distinguish them still can;
    the underlying `GitCommandError` survives as `__cause__` for whoever wants the verbatim detail.

    When git genuinely cannot be consulted at all — not merely unsettled, but every call in this
    function fails outright, e.g. a corrupted worktree — the same exception is raised: the tree's
    relationship to the anchor is unknown either way, and the caller's correct response ("this
    task's outcome cannot be trusted; do not spend a repair rung on it") does not depend on which.
    """


def patch_id(patches: Sequence[PatchLike]) -> str:
    """The `Fleet-Patch-Id` trailer: content-only, order-independent, stable across attempts.

    SPEC §3.2 step 6::

        sha256("\\n".join(sorted(f"{p.path}\\x1f{sha256(p.diff)}" for p in result.patches)))

    Sorted so that a model re-proposing the same edits in a different order is recognised as the
    same work; hashing the diff rather than embedding it so the key is fixed-width in a trailer.
    """
    lines = sorted(
        f"{patch.path}\x1f{hashlib.sha256(patch.diff.encode('utf-8')).hexdigest()}"
        for patch in patches
    )
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class FleetTrailers:
    """The six trailers every harness commit carries (SPEC §3.2 step 6), plus one optional
    seventh (ADR-0122 Decision 5).

    They are what turn "did my work land?" into a git query, from a bare clone, with no database.
    """

    run_id: UUID | str
    repo_id: str
    phase: int
    task_id: UUID | str
    attempt: int
    patch_id: str
    contract_rollback_id: str | None = None
    """ADR-0122 Decision 5, appended LAST so every existing call site (`apply_and_commit`, and
    every test constructing `FleetTrailers` positionally) is unaffected. Set on a hoist-rollback
    revert commit to the failed contract's own `contract_id`; left `None` everywhere else, in
    which case `as_mapping()` omits the trailer entirely (unlike the six required fields above,
    which are always present) — the first optional trailer this dataclass has ever carried."""

    def as_mapping(self) -> dict[str, str]:
        mapping = {
            RUN_ID_TRAILER: str(self.run_id),
            REPO_ID_TRAILER: self.repo_id,
            PHASE_TRAILER: str(self.phase),
            TASK_ID_TRAILER: str(self.task_id),
            ATTEMPT_TRAILER: str(self.attempt),
            PATCH_ID_TRAILER: self.patch_id,
        }
        if self.contract_rollback_id is not None:
            mapping[CONTRACT_ROLLBACK_ID_TRAILER] = self.contract_rollback_id
        return mapping


@dataclass(frozen=True, slots=True)
class GuardOutcome:
    """Both halves of the §3.2 step 6.1 guard, kept separate on purpose.

    `trailer_commit` alone must never gate a skip — it is provenance for the `attempts` row. The
    decision is `already_applied`, and it requires `effect_present`.
    """

    already_applied: bool
    effect_present: bool
    trailer_commit: str | None
    reason: str


@dataclass(frozen=True, slots=True)
class CommitOutcome:
    """What the runner persists: a pointer into git, plus why nothing was written if nothing was.

    `commit_sha` is None exactly when `skipped` — the `already_applied` event of §3.2 step 6.1,
    which consumes no attempt.
    """

    commit_sha: str | None
    skipped: bool
    guard: GuardOutcome

    @property
    def committed(self) -> bool:
        return self.commit_sha is not None and not self.skipped


@dataclass(frozen=True, slots=True)
class RevertOutcome:
    """What `revert_and_commit` produced: a new commit SHA, or a conflict the caller must handle.

    Deliberately not `CommitOutcome` — a revert has no `already_applied` idempotency guard of its
    own the way `apply_and_commit` does (that guard reads the `Fleet-Patch-Id` of the PATCH about
    to be applied; a revert has no such patch). Whether "this rollback already landed" holds is a
    question for whatever calls this (SPEC §3.1's Leg C2/D, not yet built) to answer by reading
    ITS OWN trailer of interest (e.g. the failed contract's id) over `commits_in_range` — this
    primitive does not know what that trailer is. `commit_sha` is None exactly when `conflicted`.
    """

    commit_sha: str | None
    conflicted: bool


def scoped_range(pre_commit_sha: str, branch: str) -> str:
    """`<phases.pre_commit_sha>..<branch>` — the ONLY range the trailer guard may search.

    A function rather than an f-string at three call sites so the scoping rule has one place to be
    read, and so `tests/test_vcs.py` can assert the range that was searched.
    """
    if not pre_commit_sha:
        raise RollbackAnchorError(
            "the trailer guard must be scoped to the phase anchor (§3.2 step 6.1); an empty "
            "pre_commit_sha would search the branch's whole history and skip work a rebase dropped"
        )
    return f"{pre_commit_sha}..{branch}"


async def guard(
    git: Git,
    *,
    branch: str,
    pre_commit_sha: str,
    patch: Path | str,
    patch_id_value: str,
) -> GuardOutcome:
    """The two-read pre-apply guard. No SQL, no worktree mutation.

    Returns `already_applied=True` only when the patch's effect is present in the CURRENT tree
    (`git apply --check --reverse` succeeds). A trailer found in the scoped range is reported for
    the `attempts` row, and — alone — is explicitly NOT enough: that is the rebase-dropped-hunk
    defect this whole guard exists to prevent.
    """
    trailer_commit = await git.find_trailer_commit(
        scoped_range(pre_commit_sha, branch), PATCH_ID_TRAILER, patch_id_value
    )
    effect_present = await git.apply_check(patch, reverse=True)
    if effect_present:
        reason = (
            "effect present at the current tip (reverse-apply succeeded)"
            + (f"; committed as {trailer_commit}" if trailer_commit else "; no trailer in range")
        )
    elif trailer_commit is not None:
        reason = (
            f"trailer found on {trailer_commit} but its effect is NOT in the current tree — "
            "re-applying (a later rebase or revert dropped it; §3.2 step 6.1)"
        )
    else:
        reason = "not applied: no trailer in the scoped range and reverse-apply failed"
    return GuardOutcome(
        already_applied=effect_present,
        effect_present=effect_present,
        trailer_commit=trailer_commit,
        reason=reason,
    )


async def apply_and_commit(
    git: Git,
    *,
    patch: Path | str,
    subject: str,
    trailers: FleetTrailers,
    branch: str,
    pre_commit_sha: str,
    body: str | None = None,
) -> CommitOutcome:
    """Guard → `git apply --check` → `git apply --index` → `git commit --trailer …`.

    The whole §3.2 step 6 sequence for one task, in one call, so no caller can implement three of
    the four steps. The commit is the record: its SHA is what goes on the `attempts` row.
    """
    outcome = await guard(
        git,
        branch=branch,
        pre_commit_sha=pre_commit_sha,
        patch=patch,
        patch_id_value=trailers.patch_id,
    )
    if outcome.already_applied:
        return CommitOutcome(commit_sha=outcome.trailer_commit, skipped=True, guard=outcome)

    if not await git.apply_check(patch):
        raise PatchApplyError(
            f"git apply --check refused {patch} in {git.path}: the patch does not apply to the "
            "current tree and no commit was made"
        )
    await git.apply(patch, index=True)
    sha = await git.commit(subject, trailers=trailers.as_mapping(), body=body)
    return CommitOutcome(commit_sha=sha, skipped=False, guard=outcome)


async def revert_and_commit(
    git: Git,
    *,
    sha: str,
    subject: str,
    trailers: FleetTrailers,
    mainline: int = 1,
    body: str | None = None,
) -> RevertOutcome:
    """`git revert -m <mainline> --no-commit <sha>` staged, then stamped with the standard six
    `Fleet-*` trailers exactly like any other mutation (SPEC §3.1's hoist rollback, §3.2 step 6)
    — one call so no caller re-implements the stage/commit split.

    On a CONFLICT (`Git.revert` returns `False`), this makes NO commit and leaves the conflict
    staged for the caller to resolve or discard (`git.abort_revert()`) — the same contract as
    `Git.rebase()`/`abort_rebase()`. Deliberately not resolved here: reacting to a revert conflict
    (e.g. the "merged case" of an un-hoist reaching a repo whose own history has since moved on)
    is SPEC §3.1's Leg D, not yet built — this primitive only proves the mechanical stage-then-
    stamp step standalone.
    """
    staged = await git.revert(sha, mainline=mainline)
    if not staged:
        return RevertOutcome(commit_sha=None, conflicted=True)
    new_sha = await git.commit(subject, trailers=trailers.as_mapping(), body=body)
    return RevertOutcome(commit_sha=new_sha, conflicted=False)


async def find_task_commit(
    git: Git, *, branch: str, pre_commit_sha: str, task_id: UUID | str
) -> str | None:
    """Resume's ONLY question (§3.2 step 6.4): did this task's commit land on `branch`?

    Yes → the work landed; reconcile the task row to `DONE` with that SHA, re-run nothing, consume
    no attempt. No → nothing landed; discard the worktree and re-run the rung. There is no third
    answer, because there is no third state git can be in.
    """
    return await git.find_trailer_commit(
        scoped_range(pre_commit_sha, branch), TASK_ID_TRAILER, str(task_id)
    )


async def commits_in_range(
    git: Git, *, branch: str, pre_commit_sha: str
) -> tuple[Mapping[str, str | None], ...]:
    """Every harness commit in the scoped range, as `{sha, task_id, patch_id}` mappings.

    The reconciliation read: one git call answers "what does this phase actually contain?", which
    is what a resume compares its `tasks` rows against.
    """
    found = await git.log(
        scoped_range(pre_commit_sha, branch),
        trailer_keys=[TASK_ID_TRAILER, PATCH_ID_TRAILER],
    )
    return tuple(
        {
            "sha": commit.sha,
            "task_id": commit.trailer(TASK_ID_TRAILER),
            "patch_id": commit.trailer(PATCH_ID_TRAILER),
        }
        for commit in found
    )


_REVERTS_COMMIT_RE = re.compile(r"This reverts commit ([0-9a-f]{7,40})\.")
"""Decision 5's own revert-body text, verbatim (`execute_hoist_rollback`'s real pass writes it).
Matches a short OR full hex sha (7-40 hex chars) so this stays correct even if some future caller
writes an abbreviated sha; every writer in THIS codebase always writes the full 40-char form."""


async def contract_rollback_shas_in_range(
    git: Git, *, pre_commit_sha: str, branch: str, contract_id: str
) -> frozenset[str]:
    """ADR-0122 Decision 5's resume/idempotency query: which of a hoist-rollback's ORIGINAL merge
    shas already have a landed revert commit on `branch`.

    Every revert commit in one rollback series carries the IDENTICAL `Fleet-Contract-Rollback-Id`
    trailer value (the failed contract's own `contract_id`) — the trailer alone cannot say which
    original sha a given revert commit reverts, only that it belongs to this contract's series. So
    this reads each matching commit's BODY for Decision 5's "This reverts commit <sha>." text
    (`revert_and_commit`'s caller supplies exactly that as `body`) to recover the mapping.

    `git.log()` does not carry commit bodies (only subject + parsed trailers), so each matching
    commit costs one extra `git log -1 --format=%b` call — one call per rollback commit that
    exists, never per candidate sha, and a hoist-rollback series is always small (one contract plus
    its blast set).

    `pre_commit_sha` should anchor at a commit that is a real ancestor of every possible rollback
    commit and of nothing that could be one falsely — research-40 (task-71 brief) recommends the
    CONTRACT's own merge sha: it is always an ancestor of `integration` by the time Leg D fires
    (the contract must already be merged to have broken a build), and no rollback commit for this
    contract can exist before its own merge sha does. `scoped_range` accepts any non-empty sha
    string; this function does not itself validate which one the caller chose.
    """
    found = await git.log(
        scoped_range(pre_commit_sha, branch), trailer_keys=[CONTRACT_ROLLBACK_ID_TRAILER]
    )
    reverted: set[str] = set()
    for commit in found:
        if commit.trailer(CONTRACT_ROLLBACK_ID_TRAILER) != contract_id:
            continue
        body = await git.text(["log", "-1", "--format=%b", commit.sha])
        match = _REVERTS_COMMIT_RE.search(body)
        if match:
            reverted.add(match.group(1))
    return frozenset(reverted)


async def record_task_anchor(git: Git, branch: str) -> str:
    """`tasks.pre_commit_sha`: the tip of `migrate/<repo>` read at task start.

    Written in the same transaction that moves the task row to `RUNNING` (§3.2 step 6.5). Read
    from git rather than copied from the phase anchor — that copy is exactly the bug the two
    distinct anchors exist to prevent.
    """
    return await git.rev_parse(branch)


async def discard_task(git: Git, *, task_pre_commit_sha: str, branch: str | None = None) -> None:
    """Crash-discard of ONE task: `reset --hard <tasks.pre_commit_sha>` + `clean -fdx`.

    Refuses an anchor that is not an ancestor of the current tip. Without that check a caller who
    passed the *phase* anchor by mistake would delete the commits of earlier tasks whose rows are
    already `DONE` and will never re-run — the phase would then pass its success criterion on a
    tree missing most of its rewrites. That is not a hypothetical; it is why this function takes a
    parameter named `task_pre_commit_sha` and why whole-phase rollback is a different function.
    """
    if not task_pre_commit_sha:
        raise RollbackAnchorError(
            "discard_task requires tasks.pre_commit_sha; the phase anchor is NOT a substitute "
            "(§3.2 step 6.5) and there is no default that is safe"
        )
    try:
        anchor = await git.resolve(task_pre_commit_sha)
    except GitCommandError as exc:
        # D42 made `resolve` raise instead of returning `None` for a probe that never settled —
        # correct there, but this call runs on the SAME clock that (often) triggered this very
        # rollback. Letting `GitCommandError` escape here is indistinguishable, to `land_patches`'
        # `except (PatchApplyError, GitCommandError)`, from the original patch failure it is
        # rolling back from.
        raise RollbackIndeterminateError(
            f"could not resolve task anchor {task_pre_commit_sha!r} in {git.path}: the probe "
            "did not settle, so whether the anchor exists is unknown, not refused"
        ) from exc
    if anchor is None:
        raise RollbackAnchorError(
            f"task anchor {task_pre_commit_sha!r} does not resolve in {git.path}"
        )
    tip = branch or "HEAD"
    try:
        is_ancestor = await git.is_ancestor(anchor, tip)
    except GitCommandError as exc:
        raise RollbackIndeterminateError(
            f"could not verify {anchor[:12]} is an ancestor of {tip} in {git.path}: the probe "
            "did not settle, so refusing to guess either way"
        ) from exc
    if not is_ancestor:
        raise RollbackAnchorError(
            f"refusing to reset {tip} to {anchor[:12]}: the anchor is not an ancestor of the "
            "current tip, so this would rewrite history rather than discard one task"
        )
    try:
        await git.reset_hard(anchor)
        await git.clean(directories=True, ignored=True)
    except GitCommandError as exc:
        # The mutation itself, not just the probes: a `reset --hard`/`clean -fdx` that fails —
        # whether unsettled or a genuine refusal — leaves the worktree's relationship to the
        # anchor just as unproven as an unsettled probe would, and must be reported the same way.
        raise RollbackIndeterminateError(
            f"reset/clean to {anchor[:12]} did not complete in {git.path}: the worktree's state "
            "relative to the anchor is now unknown"
        ) from exc


async def rollback_phase(git: Git, *, branch: str, phase_base_ref: str) -> str:
    """Explicit WHOLE-phase rollback: `update-ref` the branch back to the phase anchor.

    Named separately from `discard_task` so it cannot be reached by accident. The caller MUST, in
    the same SQLite transaction, reset every `tasks` row of the phase from `DONE` to `PENDING` and
    clear their `pre_commit_sha` (§3.2 step 6.5) — otherwise a row claims work that is no longer on
    the branch. A rollback increments no attempt counter: it is `TRANSIENT_INFRA` (ADR-0014).
    """
    target = await git.resolve(phase_base_ref)
    if target is None:
        raise RollbackAnchorError(
            f"phase anchor {phase_base_ref!r} does not resolve in {git.path}; the anchor is a real "
            "ref (refs/fleet/<run>/<repo>/phase-<n>/base) and its absence is not recoverable here"
        )
    await git.update_ref(f"refs/heads/{branch}", target, message="fleet rollback")
    return target
