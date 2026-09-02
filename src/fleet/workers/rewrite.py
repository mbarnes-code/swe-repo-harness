"""Phase 2 steps 3–6: deterministic rewrite, the escalation ladder, one commit per task.

The division of labour is the design, and every clause of it was a defect first:

* **`RewritePipeline` owns the buffer, the order, the fixpoint and conflict detection (§7.4).**
  This worker never drives an engine and never holds a buffer; it hands the pipeline one file's
  committed text and receives at most one `FilePatch` back. A worker that drove engines itself
  re-derived the `(priority, id)` order and produced two overlapping diffs per file, the second
  of which `git apply` rejected — and the ladder then charged a YAML authoring error to the LLM
  repair budget.
* **A `RuleConflict` does not advance the ladder.** The pipeline returns
  `advance_ladder=False` and leaves the file unchanged; this worker converts that into a
  **non-retryable** `WorkerError`, which `RetryPolicy.decide` answers with `TERMINATE` and
  `charges_attempt == False`. Two rules disagreeing is an operator's YAML defect, and spending a
  `WORKHORSE` and then a `HEAVY` call to rediscover it is the expensive version of a lint error.
* **One commit per task, immediately (§3.2 step 6, ADR-0024).** Every unit lands through
  `vcs/commits.apply_and_commit`, which runs the two-condition guard, `git apply --index` and
  `git commit --trailer Fleet-Patch-Id=…` in one call, so no caller can implement three of the
  four steps. The commit is the record; nothing here writes SQL and nothing stores a diff.
* **`partial` is a real answer.** Landing 40 of 60 units and then hitting `ctx.deadline` is 40
  real commits, and reporting `failed` would make the next attempt replay all 60 against an
  already-rewritten tree and emit the no-op patches the ladder misreads as `RULE_MISS`. The
  result carries `completed_units`, and re-entry resumes at what is still owed.
* **The repair prompt gets THIS failure's verbatim stderr and nothing else** (CLAUDE.md
  guardrail 5, §3.2 step 5). Evidence is rebuilt from scratch on every invocation from the
  failure in front of it — the worker is stateless, so it has no transcript to leak — and the
  rejected *diff* is never rendered: `RejectedApproach` has no field able to hold one.

`ctx.attempt` is the ladder position and `ctx.tier` is its consequence, both set by
`BaseWorker.execute()` from the persisted `phases.attempts`. Nothing here counts attempts.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Final
from uuid import UUID, uuid5

from pydantic import Field, JsonValue

from fleet.llm.calls import escalate_repair, propose_repair
from fleet.llm.client import LlmError
from fleet.llm.schemas import ProposedFileEdit
from fleet.models.enums import ContextPolicy, FailureClass, Phase, TransformTier
from fleet.models.tasks import FilePatch, RejectedApproach, TokenUsage
from fleet.orchestrator.registry import register_worker
from fleet.rewrite.apply import check_diff
from fleet.rewrite.pipeline import DEFAULT_MAX_PASSES, RewriteOutcome, RewritePipeline
from fleet.rewrite.rules import EngineRegistry, RewriteRule
from fleet.util.fs import DiskFloorBreached, require_free_space, scoped_tempdir
from fleet.vcs.commits import (
    CommitOutcome,
    FleetTrailers,
    PatchApplyError,
    apply_and_commit,
    discard_task,
    find_task_commit,
    patch_id,
    record_task_anchor,
)
from fleet.vcs.git import Git, GitCommandError
from fleet.workers.base import (
    BaseWorker,
    WorkerContext,
    WorkerError,
    WorkerInput,
    WorkerOutput,
    WorkerResult,
    accumulate,
    loop_now,
    unfinished_units,
)

__all__ = [
    "TASK_NAMESPACE",
    "RewriteInput",
    "RewriteOutput",
    "RewriteWorker",
    "WorkerRepairError",
    "land_patches",
    "task_id_for",
    "task_id_for_ids",
    "units_owed",
    "write_patch_file",
]


class WorkerRepairError(RuntimeError):
    """A repair rung's model call failed outright. Raised, never swallowed (Rule 11): the runner
    classifies it, and a silently skipped rung looks exactly like a rung that found nothing."""


TASK_NAMESPACE: Final = UUID("1b7f4d2e-0000-4000-8000-000000000002")
"""Namespace for the per-unit `Fleet-Task-Id`.

*Agent recommendation* (CLAUDE.md guardrail 1): the trailer must be **stable across re-entry**,
because §3.2 step 6.4's crash recovery asks git "does a commit bearing this task's id exist?".
A `uuid4()` minted per invocation answers that question differently every time, so the id is
derived from `(run_id, repo_id, phase, unit)` — the identity of the work, not of the attempt.
"""

_SAFE_NAME: Final = re.compile(r"[^A-Za-z0-9_.-]")


def task_id_for_ids(run_id: str | UUID, repo_id: str, phase: int, unit: str) -> UUID:
    """The `Fleet-Task-Id` for one unit of work, ctx-free. Deterministic; see `TASK_NAMESPACE`.

    Pure function over the raw identity components so callers with no `WorkerContext` (e.g.
    `cli.py`'s reconciliation loop, re-deriving a landed unit's expected trailer at arbitration
    time rather than at commit time) reuse the exact formula `land_patches` used to write the
    trailer, instead of a second copy that could drift (CLAUDE.md "sweep for the class"). `run_id`
    takes `str | UUID` because `WorkerContext.run_id` is a `UUID` (`workers/base.py`) while
    `cli.py`'s reconciliation loop only ever has the plain `str` read back from SQL — `str()`
    below is what the old f-string interpolation did implicitly for the `UUID` case.
    """
    return uuid5(TASK_NAMESPACE, f"{run_id}|{repo_id}|{int(phase)}|{unit}")


def task_id_for(ctx: WorkerContext, phase: Phase, unit: str) -> UUID:
    """The `Fleet-Task-Id` for one unit of work. Deterministic; see `TASK_NAMESPACE`."""
    return task_id_for_ids(ctx.run_id, ctx.repo_id, int(phase), unit)


def units_owed(all_units: Sequence[str], completed: Sequence[str]) -> list[str]:
    """What re-entry still owes, through §7.1's `unfinished_units`.

    `completed` is the `partial` checkpoint's `completed_units`, replayed onto the payload by the
    runner (the single SQLite reader, §11.5). Order is preserved, so a resumed invocation walks
    the same sequence the interrupted one would have walked.
    """
    checkpoint: WorkerResult[WorkerOutput] | None = None
    if completed:
        checkpoint = WorkerResult(status="partial", completed_units=list(completed))
    return unfinished_units(all_units, checkpoint)


def write_patch_file(directory: Path, unit: str, diffs: Sequence[str]) -> Path:
    """Materialise one task's patch. The trailing newline is restored because `FleetModel`'s
    `str_strip_whitespace` eats it off `FilePatch.diff`, and `git apply` rejects a diff whose
    last hunk line has none — a corrupt-patch error with nothing to do with the rewrite."""
    target = directory / f"{_SAFE_NAME.sub('_', unit)}.patch"
    body = "".join(diff if diff.endswith("\n") else diff + "\n" for diff in diffs)
    target.write_text(body, encoding="utf-8")
    return target


async def land_patches(
    git: Git,
    patches: Sequence[FilePatch],
    *,
    ctx: WorkerContext,
    phase: Phase,
    unit: str,
    subject: str,
    branch: str,
    phase_pre_commit_sha: str,
    patch_dir: Path,
) -> CommitOutcome:
    """One task → one guarded, trailered commit, or a rollback to **this task's** anchor.

    The anchor is read from git at task start (`tasks.pre_commit_sha`, §3.2 step 6.5) and is not
    the phase anchor: resetting a crashed task to the phase anchor deletes the commits of earlier
    tasks whose rows are already `DONE`, and the phase then passes its success criterion on a tree
    missing most of its rewrites.

    Shared with `workers/relocate.py` — a rename and a rewrite differ in how the diff is produced,
    never in how it is committed, and two copies of this sequence would be two chances to skip the
    guard.
    """
    patch_file = write_patch_file(patch_dir, unit, [patch.diff for patch in patches])
    trailers = FleetTrailers(
        run_id=ctx.run_id,
        repo_id=ctx.repo_id,
        phase=int(phase),
        task_id=task_id_for(ctx, phase, unit),
        attempt=ctx.attempt,
        patch_id=patch_id(patches),
    )
    anchor = await record_task_anchor(git, branch)
    try:
        return await apply_and_commit(
            git,
            patch=patch_file,
            subject=subject,
            trailers=trailers,
            branch=branch,
            pre_commit_sha=phase_pre_commit_sha,
        )
    except (PatchApplyError, GitCommandError):
        # A killed `git apply` leaves the worktree dirty and nothing on the branch; the disposable
        # worktree is reset to this task's anchor, which leaves every earlier task's commit intact.
        await discard_task(git, task_pre_commit_sha=anchor, branch=branch)
        raise


@dataclass(frozen=True, slots=True)
class _Repair:
    """One repair rung's answer: patches to land, what it cost, and whether it gave up."""

    patches: tuple[FilePatch, ...]
    usage: TokenUsage
    abandon_reason: str | None = None


class RewriteInput(WorkerInput):
    """One repo's rewrite task set. Everything the rung may touch, handed in (§7.1)."""

    branch: str = Field(min_length=1, description="`migrate/<repo>`; run-reconciled, not scoped")
    phase_pre_commit_sha: str = Field(
        pattern=r"^[0-9a-f]{40}$",
        description="`phases.pre_commit_sha` — the ONLY range the trailer guard may search",
    )
    dest_path: str = Field(min_length=1, description="The repo's monorepo subtree; §3.3 layout()")
    targets: list[str] = Field(default_factory=list, description="Repo-relative files to rewrite")
    rules: list[RewriteRule] = Field(default_factory=list)
    engines: dict[str, str] = Field(
        default_factory=dict, description="§9 `transform.engines`: engine name → module"
    )
    params: dict[str, str] = Field(
        default_factory=dict, description="Relocation-map substitutions, e.g. {{new_pkg}}"
    )
    max_passes: int = Field(default=DEFAULT_MAX_PASSES, ge=1)
    max_patch_bytes: int = Field(
        default=1_048_576,
        gt=0,
        description="`transform.max_patch_bytes` (`settings.py:452`), whose Settings default "
        "this mirrors so `check_diff` enforces the cap even before a driver threads the live "
        "configured value onto this field — see `ContractsInput.config` (`workers/contracts.py`) "
        "for the shape a driver uses to pass a non-default value explicitly.",
    )
    completed_units: list[str] = Field(
        default_factory=list,
        description="The `partial` checkpoint replayed by the runner; never replayed by this "
        "worker (§7.1) — the units in it already have commits on the branch.",
    )
    rejected_approaches: list[RejectedApproach] = Field(
        default_factory=list,
        description="ADR-0021 memory, rendered ONLY under EVIDENCE_PLUS_REJECTED_APPROACHES. It "
        "carries no diff text, so a raw prior patch cannot travel inside it.",
    )
    min_free_bytes: int = Field(
        default=0,
        ge=0,
        description="`preflight.min_free_bytes`, re-checked before THIS repo's rewrites rather "
        "than once at startup (§11.3): the fleet fills the volume as it runs, so a floor tested "
        "at repo 1 says nothing about repo 180. `0` disables the gate and is the default only "
        "because a payload built by hand in a test has no `config/fleet.yaml` behind it; `fleet "
        "transform` always passes the configured value.",
    )


class RewriteOutput(WorkerOutput):
    """The checkpoint: what reached the tree, and what did not."""

    rewritten: list[str] = Field(default_factory=list)
    commits: list[str] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list, description="Guard said already applied")
    unresolved: list[str] = Field(default_factory=list, description="§3.2 success needs this empty")


@register_worker
class RewriteWorker(BaseWorker[RewriteInput, RewriteOutput]):
    """Rewrite one repo's files at their monorepo path, one commit per task. See the module
    docstring for why each rule of the sequence exists."""

    __slots__ = ()

    name: ClassVar[str] = "rewrite"
    phase: ClassVar[Phase] = Phase.TRANSFORM
    input_model: ClassVar[type[WorkerInput]] = RewriteInput
    output_model: ClassVar[type[WorkerOutput]] = RewriteOutput

    def pipeline_for(self, ctx: WorkerContext, payload: RewriteInput) -> RewritePipeline:
        """Construct the §7.4 pipeline for this repo.

        A method rather than an inline expression so a test can inject an in-process engine: the
        three real drivers are external tools (`ast-grep`, `libcst`, `ts-morph`), none of which is
        a dependency, and a suite that needed one would be a suite that never ran. The buffer, the
        order and the conflict detection under test are engine-independent by construction.
        """
        return RewritePipeline(
            payload.rules,
            EngineRegistry.from_modules(payload.engines),
            max_passes=payload.max_passes,
            params=payload.params,
            tier=ctx.tier,
            repo_id=ctx.repo_id,
        )

    async def preconditions_hold(self, ctx: WorkerContext, payload: RewriteInput) -> bool:
        """Re-entry is admitted only for what is still owed, against a tree that still matches.

        Explicit, not inherited (§7.1): the checkpoint narrows the work to `remaining_units`, and
        a target that has vanished — or that sits outside the repo's own subtree, which no rewrite
        of this repo may write to — means the plan was computed against a different tree, so the
        phase re-runs from `phases.base_ref` rather than patching a tree it does not describe.
        """
        return _targets_are_present(
            Path(ctx.workdir),
            units_owed(payload.targets, payload.completed_units),
            payload.dest_path.rstrip("/"),
        )

    async def run(
        self, ctx: WorkerContext, payload: RewriteInput
    ) -> WorkerResult[RewriteOutput]:
        """The §3.2 step 6 sequence, once per unit: rewrite → validate → guard → apply → commit."""
        git = Git(ctx.workdir, deadline=ctx.deadline)
        pipeline = self.pipeline_for(ctx, payload)
        owed = units_owed(payload.targets, payload.completed_units)
        landed = list(payload.completed_units)
        output = RewriteOutput()
        usage = TokenUsage()
        root = Path(ctx.workdir)

        # §11.3, before the first rewrite pass: the fleet fills the volume as it runs, so a floor
        # tested at repo 1 says nothing about repo 180 (same reasoning as `clone.py`'s per-clone
        # check).
        try:
            require_free_space(
                root,
                payload.min_free_bytes,
                operation=f"rewrite {payload.dest_path}",
            )
        except DiskFloorBreached as breach:
            return WorkerResult[RewriteOutput](
                status="failed",
                output=output,
                remaining_units=owed,
                usage=usage,
                error=WorkerError(
                    failure_class=FailureClass.DISK_EXHAUSTED,
                    retryable=False,
                    stderr_tail=str(breach),
                    exception_type=f"{type(breach).__module__}.{type(breach).__qualname__}",
                ),
            )

        with scoped_tempdir(prefix="fleet-rewrite-") as patch_dir:
            for index, unit in enumerate(owed):
                now = loop_now()
                if ctx.expired(now) or ctx.cancelled():
                    # Cooperative, at a unit boundary: everything before this has a commit.
                    return self._interrupted(
                        timed_out=ctx.expired(now),
                        landed=landed,
                        remaining=owed[index:],
                        output=output,
                        usage=usage,
                    )

                source = (root / unit).read_text(encoding="utf-8")
                outcome = await pipeline.rewrite_file(unit, source)
                if outcome.conflicted:
                    return self._rule_conflict(outcome, landed, owed[index:], output, usage)

                patches: tuple[FilePatch, ...] = ()
                repair_evidence: tuple[FailureClass, str, str] | None = None
                if outcome.patch is None:
                    # The rules produced nothing. Git — not a guess — says whether that is because
                    # the work is already DONE or because no rule matched: §3.2 step 6.4 asks
                    # exactly one question, "is this task's commit on the branch?". Without it a
                    # crash between `git commit` and the row write comes back as `RULE_MISS`, and
                    # the ladder spends an LLM rung repairing a file that is already correct.
                    done = await find_task_commit(
                        git,
                        branch=payload.branch,
                        pre_commit_sha=payload.phase_pre_commit_sha,
                        task_id=task_id_for(ctx, self.phase, unit),
                    )
                    if done is not None:
                        landed.append(unit)
                        output.rewritten.append(unit)
                        output.skipped.append(unit)
                        output.commits.append(done)
                        continue
                    repair_evidence = (
                        FailureClass.RULE_MISS,
                        "deterministic rules",
                        f"no rule produced a change for {unit}; "
                        f"rules considered: {[r.id for r in pipeline.rules_for(unit)]}",
                    )
                else:
                    reason = check_diff(
                        outcome.patch.diff,
                        payload.dest_path,
                        max_bytes=payload.max_patch_bytes,
                        declared_path=outcome.patch.path,
                    )
                    if reason is not None:
                        # §3.2 step 6.6: rejected BEFORE `git apply`, so an out-of-tree write is
                        # never even intended. A rule that writes outside the subtree is a rule
                        # defect, and no rung can repair it.
                        return self._failed(
                            FailureClass.PATCH_REJECTED,
                            retryable=False,
                            detail=f"{unit}: {reason}",
                            landed=landed,
                            remaining=owed[index:],
                            output=output,
                            usage=usage,
                        )
                    patches = (outcome.patch,)

                if patches:
                    try:
                        commit = await land_patches(
                            git,
                            patches,
                            ctx=ctx,
                            phase=self.phase,
                            unit=unit,
                            subject=f"fleet(rewrite): {unit}",
                            branch=payload.branch,
                            phase_pre_commit_sha=payload.phase_pre_commit_sha,
                            patch_dir=patch_dir,
                        )
                    except (PatchApplyError, GitCommandError) as exc:
                        repair_evidence = (
                            FailureClass.PATCH_REJECTED,
                            "git apply --check",
                            await self._apply_stderr(git, patch_dir, unit, exc),
                        )
                    else:
                        landed.append(unit)
                        self._record(output, patches, commit)
                        continue

                # Unresolved after the deterministic rung: escalate, or carry the evidence out.
                repair = await self._repair(
                    ctx, payload, unit=unit, source=source, evidence=repair_evidence
                )
                usage = accumulate(usage, repair.usage) if repair is not None else usage
                # `abandon_recommended` is checked BEFORE the patches, not after: the schema makes
                # `files` min_length=1, so the last rung always carries a patch even when what it
                # is really saying is "a human has to look at this" (§5.4, ADR-0014).
                if repair is None or repair.abandon_reason is not None or not repair.patches:
                    failure, probe, stderr = _unwrap(repair_evidence)
                    if repair is not None and repair.abandon_reason is not None:
                        stderr = (
                            f"{stderr}\nthe escalation rung recommends a human: "
                            f"{repair.abandon_reason}"
                        )
                    output.unresolved.append(unit)
                    return self._failed(
                        failure,
                        retryable=repair is None or repair.abandon_reason is None,
                        detail=f"{unit} ({probe}): {stderr}",
                        landed=landed,
                        remaining=owed[index:],
                        output=output,
                        usage=usage,
                    )
                rejected = _rejected_patch(
                    repair.patches, payload.dest_path, payload.max_patch_bytes
                )
                if rejected is not None:
                    # The model cannot certify its own patch (`_as_patches` hard-codes
                    # `parse_probe_ok=False`), and nothing downstream of here checks size or
                    # path either — `apply_and_commit` runs an idempotency check and `git apply
                    # --check`, never `check_diff`. Gate it here, before `land_patches`, the same
                    # way the deterministic branch gates its own patch above.
                    path, reason = rejected
                    output.unresolved.append(unit)
                    return self._failed(
                        FailureClass.PATCH_REJECTED,
                        retryable=True,  # a later rung may propose a smaller or in-tree patch
                        detail=f"{unit} ({path}): {reason}",
                        landed=landed,
                        remaining=owed[index:],
                        output=output,
                        usage=usage,
                    )
                try:
                    commit = await land_patches(
                        git,
                        repair.patches,
                        ctx=ctx,
                        phase=self.phase,
                        unit=unit,
                        subject=f"fleet(rewrite,{ctx.tier}): {unit}",
                        branch=payload.branch,
                        phase_pre_commit_sha=payload.phase_pre_commit_sha,
                        patch_dir=patch_dir,
                    )
                except (PatchApplyError, GitCommandError) as exc:
                    output.unresolved.append(unit)
                    return self._failed(
                        FailureClass.PATCH_REJECTED,
                        retryable=True,
                        detail=await self._apply_stderr(git, patch_dir, unit, exc),
                        landed=landed,
                        remaining=owed[index:],
                        output=output,
                        usage=usage,
                    )
                landed.append(unit)
                self._record(output, repair.patches, commit)

        return WorkerResult(
            status="ok",
            output=output,
            completed_units=landed,
            usage=usage,
            evidence=output.commits,
        )

    # ------------------------------------------------------------------ the ladder's one call

    async def _repair(
        self,
        ctx: WorkerContext,
        payload: RewriteInput,
        *,
        unit: str,
        source: str,
        evidence: tuple[FailureClass, str, str] | None,
    ) -> _Repair | None:
        """Ask the rung's role for a patch through `ctx.llm`, §7.7's one call surface.

        `None` when — and only when — this rung is deterministic or nothing failed: those are the
        two states in which no rung is owed. There is deliberately no "was a client wired?" arm
        any more. `WorkerContext.llm` IS a `ModelClient`, so the rung either calls the model or
        raises; it can no longer report a wiring failure that silently makes rung 1 the whole
        ladder. A call that fails is `WorkerRepairError` below — loud, and never a second retry
        policy (§11.8 owns that).
        """
        if ctx.tier is TransformTier.DETERMINISTIC or evidence is None:
            return None
        client = ctx.llm
        failure, probe, stderr = evidence
        rendered = self._evidence(
            ctx, payload, unit=unit, source=source, failure=failure, probe=probe, stderr=stderr
        )
        try:
            if ctx.tier is TransformTier.LLM_ESCALATION:
                heavy = await escalate_repair(client, rendered, budget=ctx.budget)
                abandon = (
                    heavy.value.human_intervention_reason or "abandon_recommended"
                    if heavy.value.abandon_recommended
                    else None
                )
                repair = _Repair(
                    self._as_patches(heavy.value.files, ctx.tier), heavy.usage, abandon
                )
            else:
                light = await propose_repair(client, rendered, budget=ctx.budget)
                repair = _Repair(self._as_patches(light.value.files, ctx.tier), light.usage)
        except LlmError as exc:  # loud and typed; the ladder decides, not a bare except (Rule 11)
            raise WorkerRepairError(f"{unit}: {ctx.tier} rung failed: {exc}") from exc
        return repair

    def _evidence(
        self,
        ctx: WorkerContext,
        payload: RewriteInput,
        *,
        unit: str,
        source: str,
        failure: FailureClass,
        probe: str,
        stderr: str,
    ) -> dict[str, JsonValue]:
        """The rung's context, composed by `ContextPolicy` (§3.2 step 5, ADR-0021).

        Evidence is always carried; the rejected-approach summaries are carried only from
        `EVIDENCE_PLUS_REJECTED_APPROACHES` up; the previous proposal's **diff** is carried by no
        policy this worker implements, because re-showing a model its own rejected patch biases it
        toward tweaking an approach that is wrong at the approach level. `stderr` is the verbatim
        text of the failure being repaired right now — this worker holds no transcript to append.
        """
        rendered: dict[str, JsonValue] = {
            "repo_id": ctx.repo_id,
            "dest_path": payload.dest_path,
            "path": unit,
            "failure_class": str(failure),
            "probe": probe,
            "stderr": stderr,
            "current_content": source,
            "rules_considered": [rule.id for rule in payload.rules],
            "context_policy": str(ctx.context_policy) if ctx.context_policy else "",
        }
        if ctx.context_policy in (
            ContextPolicy.EVIDENCE_PLUS_REJECTED_APPROACHES,
            ContextPolicy.EVIDENCE_PLUS_PRIORS,
        ):
            summaries: list[JsonValue] = []
            for prior in payload.rejected_approaches:
                summary: dict[str, JsonValue] = {
                    "approach_signature": prior.approach_signature,
                    "reason": prior.reason,
                    "failure_class": str(prior.failure_class),
                    "attempt": prior.attempt,
                }
                summaries.append(summary)
            rendered["rejected_approaches"] = summaries
        return rendered

    # ------------------------------------------------------------------ result shaping

    @staticmethod
    def _as_patches(
        files: Sequence[ProposedFileEdit], tier: TransformTier
    ) -> tuple[FilePatch, ...]:
        """Lift `ProposedFileEdit`s into `FilePatch`es. `parse_probe_ok` is False because the
        model may not certify its own patch — the harness establishes that fact (§5.4)."""
        return tuple(
            FilePatch(path=edit.path, diff=edit.diff, tier=tier, parse_probe_ok=False)
            for edit in files
        )

    @staticmethod
    def _record(
        output: RewriteOutput, patches: Sequence[FilePatch], commit: CommitOutcome
    ) -> None:
        """Record what actually landed — every `patch.path`, not the deterministic target name.

        The LLM repair branch may land up to `ProposedFileEdit`'s cap of 64 distinct paths
        (`llm/schemas.py:199`) for a single unit; recording only the unit name would under-report
        the landed set to `_transform_criterion`'s §3.2 parse probe, which reads exactly
        `output.rewritten` (`cli.py`) — an unprobed landed file is a parse failure that ships. The
        deterministic RULE_MISS shortcut has no `FilePatch` to source paths from and keeps
        appending `unit` directly (`unit` *is* the path there, `root / unit`).
        """
        if commit.skipped:
            for patch in patches:
                output.skipped.append(patch.path)
        if commit.commit_sha is not None:
            output.commits.append(commit.commit_sha)
        for patch in patches:
            output.rewritten.append(patch.path)

    @staticmethod
    async def _apply_stderr(
        git: Git, patch_dir: Path, unit: str, exc: Exception
    ) -> str:
        """The verbatim refusal, re-read from git rather than paraphrased.

        `commits.guard` answers "does this apply?" with a boolean, so the text a repair rung needs
        is gone by the time the exception is raised. One extra `--check`, on the failure path only.
        """
        if isinstance(exc, GitCommandError):
            return exc.stderr or str(exc)
        patch_file = patch_dir / f"{_SAFE_NAME.sub('_', unit)}.patch"
        result = await git.exec(["apply", "--check", str(patch_file)], check=False)
        return result.stderr_tail.strip() or str(exc)

    def _interrupted(
        self,
        *,
        timed_out: bool,
        landed: list[str],
        remaining: list[str],
        output: RewriteOutput,
        usage: TokenUsage,
    ) -> WorkerResult[RewriteOutput]:
        """Deadline or cancel at a unit boundary. `partial` iff something landed — `partial` with
        nothing completed is `failed` wearing a friendlier name (§7.1)."""
        if landed:
            return WorkerResult(
                status="partial",
                output=output,
                completed_units=landed,
                remaining_units=remaining,
                usage=usage,
                evidence=output.commits,
            )
        return WorkerResult(
            status="timeout" if timed_out else "cancelled",
            remaining_units=remaining,
            usage=usage,
            error=WorkerError(
                failure_class=FailureClass.TIMEOUT if timed_out else FailureClass.TRANSIENT_INFRA,
                retryable=True,
                stderr_tail=f"{self.name}: stopped at a unit boundary with nothing landed",
            ),
        )

    def _rule_conflict(
        self,
        outcome: RewriteOutcome,
        landed: list[str],
        remaining: list[str],
        output: RewriteOutput,
        usage: TokenUsage,
    ) -> WorkerResult[RewriteOutput]:
        """Two rules claim overlapping spans: the file is unchanged and the ladder does not move.

        `retryable=False` is the mechanical expression of §7.4 rule 4 / `advance_ladder=False`:
        `RetryPolicy.decide` answers a non-retryable error with `TERMINATE`, whose
        `charges_attempt` is False, so no rung is spent and no `HEAVY` call is bought to
        rediscover that two YAML rules disagree. `RULE_MISS` is the closest §5 `FailureClass`;
        there is no `RULE_CONFLICT` member (*agent recommendation*, and a named SPEC gap).
        """
        conflicts = [f.payload for f in outcome.findings if f.kind == "RuleConflict"]
        output.unresolved.append(outcome.path)
        return self._failed(
            FailureClass.RULE_MISS,
            retryable=False,
            detail=f"RuleConflict on {outcome.path}: {conflicts}",
            landed=landed,
            remaining=remaining,
            output=output,
            usage=usage,
        )

    @staticmethod
    def _failed(
        failure: FailureClass,
        *,
        retryable: bool,
        detail: str,
        landed: list[str],
        remaining: list[str],
        output: RewriteOutput,
        usage: TokenUsage,
    ) -> WorkerResult[RewriteOutput]:
        """A failure that still reports the commits that landed before it: they are on the branch
        whatever this row says, and a result that hid them would have the next attempt replay
        them."""
        return WorkerResult(
            status="failed",
            output=output,
            completed_units=landed,
            remaining_units=[unit for unit in remaining if unit not in set(landed)],
            usage=usage,
            evidence=output.commits,
            error=WorkerError(failure_class=failure, retryable=retryable, stderr_tail=detail),
        )


def _unwrap(evidence: tuple[FailureClass, str, str] | None) -> tuple[FailureClass, str, str]:
    return evidence or (FailureClass.RULE_MISS, "deterministic rules", "no evidence captured")


def _rejected_patch(
    patches: Sequence[FilePatch], dest_path: str, max_bytes: int
) -> tuple[str, str] | None:
    """The first `check_diff` rejection among a repair rung's proposed patches, or `None`.

    A `(path, reason)` pair rather than a bare reason: a repair rung may propose several files in
    one call (`LlmPatchProposal.files`, `max_length=64`), and the failure detail should name which
    one is at fault, not just that one of them was.
    """
    for patch in patches:
        reason = check_diff(patch.diff, dest_path, max_bytes=max_bytes, declared_path=patch.path)
        if reason is not None:
            return patch.path, reason
    return None


def _targets_are_present(root: Path, units: Sequence[str], subtree: str) -> bool:
    """Sync filesystem probes, deliberately outside the async body (ruff ASYNC240)."""
    if not root.is_dir():
        return False
    for unit in units:
        if not (root / unit).is_file():
            return False
        if subtree and not unit.startswith(f"{subtree}/"):
            return False
    return True
