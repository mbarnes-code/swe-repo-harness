"""Phase 2 step 1: the relocation plan, applied to the worktree as one trailered commit per move.

**Why a rename patch and not `git mv`.** §3.2 step 6.6 makes `git apply` the only writer into a
worktree, and a pure rename is expressible as one: `rename from`/`rename to` with a 100%
similarity index. That is not a formality — it is what makes the §3.2 step 6.1 guard work on a
move. `git apply --check --reverse` on a rename succeeds exactly when the file is already at its
destination and no longer at its source, which is the *effect* half of the guard that the trailer
half cannot supply. A `git mv` would need a bespoke "has this move happened?" check, and a bespoke
check is the one that gets written to agree with the trailer instead of with the tree.

**Why `preconditions_hold` is the whole point of this worker.** Inheriting a defaulted
`return True` re-runs a path rename over an already-renamed tree and produces `java/java/com/x`
(§7.1). Two mechanical facts are checked before any re-entry: every owed source is either still at
its source path or already at its destination, and **no owed source already sits under
`dest_path`** — the second is exactly the doubled-path case, and it means the plan was computed
against a tree that has already been relocated, so the phase must re-run from `phases.base_ref`
rather than move the tree a second time.

`git-filter-repo` renders the same mapping into *history* in Phase 3 (`vcs/filter_repo.py`,
ADR-0011). This worker rewrites the *worktree*, because Phase 2's job is a tree that is correct at
its monorepo path before any build system is involved, and because history rewriting is not
resumable one commit at a time.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import ClassVar

from pydantic import Field

from fleet.models.enums import FailureClass, Phase, TransformTier
from fleet.models.tasks import FilePatch, TokenUsage
from fleet.orchestrator.registry import register_worker
from fleet.util.fs import scoped_tempdir
from fleet.vcs.commits import PatchApplyError
from fleet.vcs.git import Git, GitCommandError
from fleet.workers.base import (
    BaseWorker,
    WorkerContext,
    WorkerError,
    WorkerInput,
    WorkerOutput,
    WorkerResult,
    loop_now,
)
from fleet.workers.rewrite import land_patches, units_owed

__all__ = [
    "RelocateInput",
    "RelocateOutput",
    "RelocateWorker",
    "relocated_path",
    "rename_diff",
]


def _plan_matches_tree(root: Path, dest_path: str, units: Sequence[str]) -> bool:
    """Sync filesystem probes, deliberately outside the async body (ruff ASYNC240).

    False means "this plan does not describe this tree": re-run the phase from its anchor rather
    than apply a rename over a tree that has already been renamed.
    """
    if not root.is_dir():
        return False
    dest = PurePosixPath(dest_path.rstrip("/"))
    for unit in units:
        if PurePosixPath(unit).is_relative_to(dest):
            return False  # the plan was computed against an already-relocated tree
        if (root / unit).is_file():
            continue
        if (root / relocated_path(dest_path, unit)).is_file():
            continue  # already landed by an earlier invocation; the guard will skip it
        return False
    return True


def relocated_path(dest_path: str, source: str) -> str:
    """`old_path → new_path` for one tracked file (§3.2 step 1)."""
    return f"{dest_path.rstrip('/')}/{source.lstrip('/')}"


def rename_diff(old_path: str, new_path: str) -> str:
    """A 100%-similarity rename, in the exact shape `git apply` and `git apply --reverse` accept.

    No hunks: the bytes do not change, only the path. Reversing it is "is the file already at
    `new_path` and gone from `old_path`?", which is guard (b) of §3.2 step 6.1 for a move.
    """
    return (
        f"diff --git a/{old_path} b/{new_path}\n"
        f"similarity index 100%\n"
        f"rename from {old_path}\n"
        f"rename to {new_path}\n"
    )


class RelocateInput(WorkerInput):
    """The relocation plan as data: an explicit mapping, never a glob evaluated at apply time."""

    branch: str = Field(min_length=1, description="`migrate/<repo>`")
    phase_pre_commit_sha: str = Field(
        pattern=r"^[0-9a-f]{40}$", description="`phases.pre_commit_sha`; the guard's only range"
    )
    dest_path: str = Field(min_length=1, description="`layout(repo)` (§3.3), computed once")
    sources: list[str] = Field(
        default_factory=list,
        description="Every tracked file to move, repo-relative, pre-relocation. Supplied by the "
        "caller rather than derived from `git ls-files` here: `Git.text` returns a BOUNDED tail "
        "of stdout, so deriving the plan inside the worker would silently truncate it on a large "
        "repo — a plan that moves most of a tree and drops the rest.",
    )
    completed_units: list[str] = Field(
        default_factory=list, description="The `partial` checkpoint, replayed by the runner"
    )


class RelocateOutput(WorkerOutput):
    """The checkpoint: the mapping that actually landed, plus the commits that carry it."""

    dest_path: str = ""
    moved: dict[str, str] = Field(default_factory=dict, description="old_path → new_path")
    commits: list[str] = Field(default_factory=list)
    skipped: list[str] = Field(
        default_factory=list, description="Guard said the move is already present in the tree"
    )


@register_worker
class RelocateWorker(BaseWorker[RelocateInput, RelocateOutput]):
    """Move one repo's tree to its monorepo path, one trailered commit per file."""

    __slots__ = ()

    name: ClassVar[str] = "relocate"
    phase: ClassVar[Phase] = Phase.TRANSFORM
    input_model: ClassVar[type[WorkerInput]] = RelocateInput
    output_model: ClassVar[type[WorkerOutput]] = RelocateOutput

    async def preconditions_hold(self, ctx: WorkerContext, payload: RelocateInput) -> bool:
        """Never move an already-moved tree. See the module docstring — this is the
        `java/java/com/x` guard, and it is why §7.1 made this method abstract.

        Returns False (re-run the phase from its anchor) when the plan does not describe the tree
        in front of it: a source that is under `dest_path` already, or a source that is neither at
        its old path nor at its new one.
        """
        return _plan_matches_tree(
            Path(ctx.workdir),
            payload.dest_path,
            units_owed(payload.sources, payload.completed_units),
        )

    async def run(
        self, ctx: WorkerContext, payload: RelocateInput
    ) -> WorkerResult[RelocateOutput]:
        """One commit per moved file, guarded, committed immediately (§3.2 step 6)."""
        git = Git(ctx.workdir, deadline=ctx.deadline)
        owed = units_owed(payload.sources, payload.completed_units)
        landed = list(payload.completed_units)
        output = RelocateOutput(dest_path=payload.dest_path)

        with scoped_tempdir(prefix="fleet-relocate-") as patch_dir:
            for index, unit in enumerate(owed):
                now = loop_now()
                if ctx.expired(now) or ctx.cancelled():
                    return self._interrupted(
                        timed_out=ctx.expired(now),
                        landed=landed,
                        remaining=owed[index:],
                        output=output,
                    )

                new_path = relocated_path(payload.dest_path, unit)
                patch = FilePatch(
                    path=new_path,
                    diff=rename_diff(unit, new_path),
                    tier=TransformTier.DETERMINISTIC,
                    # A rename changes no bytes, so the destination parses exactly as the source
                    # did at HEAD: the probe of §3.2 step 4 has nothing left to establish.
                    parse_probe_ok=True,
                )
                try:
                    commit = await land_patches(
                        git,
                        (patch,),
                        ctx=ctx,
                        phase=self.phase,
                        unit=unit,
                        subject=f"fleet(relocate): {unit} -> {new_path}",
                        branch=payload.branch,
                        phase_pre_commit_sha=payload.phase_pre_commit_sha,
                        patch_dir=patch_dir,
                    )
                except (PatchApplyError, GitCommandError) as exc:
                    return self._failed(unit, exc, landed, owed[index:], output)

                landed.append(unit)
                output.moved[unit] = new_path
                if commit.skipped:
                    output.skipped.append(unit)
                if commit.commit_sha is not None:
                    output.commits.append(commit.commit_sha)

        return WorkerResult(
            status="ok", output=output, completed_units=landed, evidence=output.commits
        )

    def _interrupted(
        self,
        *,
        timed_out: bool,
        landed: list[str],
        remaining: list[str],
        output: RelocateOutput,
    ) -> WorkerResult[RelocateOutput]:
        """Deadline or cancel at a unit boundary: the moves before it are commits, not a rollback
        candidate. `partial` with nothing landed would be `failed` wearing a friendlier name."""
        if landed:
            return WorkerResult(
                status="partial",
                output=output,
                completed_units=landed,
                remaining_units=remaining,
                evidence=output.commits,
            )
        return WorkerResult(
            status="timeout" if timed_out else "cancelled",
            remaining_units=remaining,
            error=WorkerError(
                failure_class=FailureClass.TIMEOUT if timed_out else FailureClass.TRANSIENT_INFRA,
                retryable=True,
                stderr_tail=f"{self.name}: stopped at a unit boundary with nothing landed",
            ),
        )

    @staticmethod
    def _failed(
        unit: str,
        exc: Exception,
        landed: list[str],
        remaining: list[str],
        output: RelocateOutput,
    ) -> WorkerResult[RelocateOutput]:
        """`land_patches` has already reset this task to its own anchor; the earlier moves stay.

        `retryable=True`: a refused rename is usually a tree that moved under the plan, which the
        next rung re-reads. The commits already reported are on the branch whatever this row says.
        """
        stderr = exc.stderr if isinstance(exc, GitCommandError) else str(exc)
        return WorkerResult(
            status="failed",
            output=output,
            completed_units=landed,
            remaining_units=[owed for owed in remaining if owed not in set(landed)],
            evidence=output.commits,
            usage=TokenUsage(),
            error=WorkerError(
                failure_class=FailureClass.PATCH_REJECTED,
                retryable=True,
                stderr_tail=f"{unit}: {stderr}",
            ),
        )
