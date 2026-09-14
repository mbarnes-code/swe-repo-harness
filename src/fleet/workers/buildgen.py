"""Phase 3 steps 1–3: ingest, `BUILD.bazel` generation, `MODULE.bazel` reconciliation (§3.3).

Three deterministic steps, in the order §3.3 fixes them, each its own resumable unit:

1. **Ingest** — `git-filter-repo` the relocation plan into *history*, merge it into `integration`
   with the ADR-0011 trailers, and cut the immutable `refs/fleet/<run_id>/integration/<seq>`
   snapshot the build will actually read. All of it under `IntegrationMutex`, because the
   integration branch is one mutable resource every repo in a wave wants to write and concurrent
   `--allow-unrelated-histories` merges racing one ref update orphan all but one merge commit.
   The snapshot ref is this worker's most load-bearing output: `buildverify` builds against *it*,
   never against the branch.
2. **BUILD generation** — `bazel/generators.py` renders the text. This is CODE, not a prompt
   (Rule 5): the driver hands in targets, the renderer sorts everything explicitly, and the same
   inputs produce byte-identical files across processes (§11.6). The model is reached only when
   there are no targets at all and the deterministic path has already failed once — §3.3's
   `build_authoring` escape hatch — and even then code renders the file.
3. **MODULE.bazel** — real MVS over `WorkspaceDep`s: the **minimum version satisfying ALL specs**.
   An empty intersection is a `VersionConflict` at error severity and *only then* may
   `conflict_resolution` have an opinion — whose pin `validate_override` re-derives against every
   contributing spec before a line of it is written. **The model's own `violated_specs` is never
   believed.** A pin whose damage is understated reaches a reviewer as safe, which is the one
   failure mode a version override has.

Why the conflict failure is reported *retryable* on rung 1 and not on rung 2: `DEP_CONFLICT` is in
`NON_RETRYABLE` by default, and a worker that accepted that default at rung 1 would end the ladder
before the `conflict_resolution` rung that §3.3 says is the legitimate slot for exactly this. The
mechanical evidence is the rung itself — `ctx.context_policy is None` means no prompt has been
rendered yet — so the flag is set from that, not from the message.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import ClassVar

from pydantic import Field

from fleet.bazel.generators import (
    VersionConflict,
    reconcile_versions,
    render_build_bazel,
    render_gazelle_build,
    render_module_bazel,
    resolve_workspace_deps,
    validate_override,
)
from fleet.bazel.layout import is_reserved_dest
from fleet.graph.collisions import VersionRequirement
from fleet.llm.calls import Evidence, author_build_file, resolve_version_conflict
from fleet.llm.client import LlmError
from fleet.models.base import FleetModel
from fleet.models.build import (
    BuildPlan,
    BuildTarget,
    BuildUnit,
    GazelleConfig,
    SupportFile,
    ToolchainRequirement,
    WorkspaceDep,
)
from fleet.models.enums import FailureClass, Phase
from fleet.models.tasks import TokenUsage
from fleet.orchestrator.registry import register_worker
from fleet.util.errors import exception_type_name
from fleet.util.fs import DiskFloorBreached, require_free_space
from fleet.util.proc import CommandRunner
from fleet.vcs.filter_repo import IngestError, IntegrationMutex, SourceProvenance, ingest
from fleet.vcs.git import Git, GitError
from fleet.workers.base import (
    BaseWorker,
    WorkerBudget,
    WorkerContext,
    WorkerError,
    WorkerInput,
    WorkerOutput,
    WorkerResult,
    accumulate,
    loop_now,
)
from fleet.workers.buildverify import dirs_present

__all__ = [
    "BUILD_BAZEL_UNIT",
    "INGEST_UNIT",
    "MODULE_BAZEL_UNIT",
    "BuildgenInput",
    "BuildgenOutput",
    "BuildgenWorker",
    "ExternalRequirement",
    "IngestSpec",
    "RejectedOverride",
]

INGEST_UNIT = "ingest"
BUILD_BAZEL_UNIT = "build_bazel"
MODULE_BAZEL_UNIT = "module_bazel"


class ExternalRequirement(FleetModel):
    """One repo's requirement on one external coordinate, as a durable payload field.

    A mirror of `graph.collisions.VersionRequirement` — which is a frozen dataclass and therefore
    cannot ride on a `FleetModel` — converted at the boundary by `as_requirement()`. Two shapes
    for one fact is a cost; the alternative was a pydantic model in `graph/`, which would make the
    pure sequencer depend on the durable-model layer to answer a question about semver.
    """

    coord_key: str = Field(min_length=1)
    repo_id: str = Field(min_length=1)
    version_spec: str = Field(min_length=1)

    def as_requirement(self) -> VersionRequirement:
        return VersionRequirement(
            coord_key=self.coord_key, repo_id=self.repo_id, version_spec=self.version_spec
        )


class IngestSpec(FleetModel):
    """§3.3 step 1's inputs. Absent ⇒ the history is already on the branch and only a snapshot is
    owed; present ⇒ the filtered throwaway clone at `source_dir` is merged under the mutex."""

    source_dir: str = Field(min_length=1, description="The ALREADY-filtered throwaway clone")
    integration_worktree: str = Field(
        min_length=1, description="A worktree with the integration branch checked out"
    )
    source_sha: str = Field(min_length=1, description="`Source-Sha:` trailer — the origin commit")
    source_repo_id: str | None = Field(
        default=None, description="`Source-Repo:` trailer; defaults to `ctx.repo_id`"
    )
    contract_id: str | None = Field(
        default=None, description="`Hoisted-Contract:` trailer for a hoisted contract node"
    )
    integration_branch: str = "integration"
    source_ref: str = "HEAD"
    lock_dir: str | None = Field(
        default=None, description="Where the `flock` lives; defaults to the repo's git common dir"
    )


class RejectedOverride(FleetModel):
    """A model-proposed pin that code refused, kept as evidence rather than dropped.

    Recorded because the rejection is the interesting event: it is the difference between a
    reviewer seeing "we pinned 33 and broke 30 repos' declared bound" and seeing nothing at all.
    """

    coord_key: str
    proposed_version: str
    mechanism: str
    violated: list[str] = Field(default_factory=list)
    undisclosed: list[str] = Field(default_factory=list)
    reason: str = ""


class BuildgenInput(WorkerInput):
    """One unit's emission plan. Targets arrive as data: the ecosystem adapters own their shape
    (ADR-0020) and this worker owns none of it — `unit.ecosystem` never appears in a branch here."""

    unit: BuildUnit
    targets: list[BuildTarget] = Field(
        default_factory=list, description="`adapter.generate_targets() + test_targets()` output"
    )
    gazelle: GazelleConfig | None = Field(
        default=None,
        description="Set iff `adapter.uses_gazelle`: Gazelle writes the BUILD files and we write "
        "its config. Delegation is a real shipped case (rules_go), not a hypothetical (§3.3).",
    )
    module_targets: list[BuildTarget] = Field(
        default_factory=list,
        description="The FLEET's targets, for MODULE.bazel's ruleset set only — a target's "
        "`load_from` is a claim on a ruleset that nothing else declares (D6), and MODULE.bazel is "
        "one root file every dispatch must render IDENTICALLY, so the per-repo `targets` above "
        "cannot be what feeds it. Empty ⇒ fall back to `targets`, which is right for a caller "
        "rendering one unit's module on its own.",
    )
    workspace_deps: list[WorkspaceDep] = Field(default_factory=list)
    toolchains: list[ToolchainRequirement] = Field(default_factory=list)
    support_files: list[SupportFile] = Field(
        default_factory=list,
        description="Files the two generated files NAME and that no phase created (D10): the "
        "`pip.parse` lock, the `npm_translate_lock` pnpm lock and `.bazelignore`, the `ts_config` "
        "source. Declared by the adapters, RESOLVED against the repo's tree by the driver, and "
        "written here — so `MODULE.bazel` and the files it references land in one commit.",
    )
    requirements: list[ExternalRequirement] = Field(
        default_factory=list, description="Every contributing spec MVS must satisfy (§3.3 step 3)"
    )
    ruleset_versions: dict[str, str] = Field(
        default_factory=dict, description="`build.ruleset_versions` (§9); an unpinned dep is an "
        "error, because it resolves differently on the next run"
    )
    module_name: str = "monorepo"
    module_version: str = "0.0.0"
    module_bazel_path: str = "MODULE.bazel"
    write_module_bazel: bool = True
    ingest: IngestSpec | None = None
    log_dir: str = "artifacts/logs"
    min_free_bytes: int = Field(
        default=0,
        ge=0,
        description="`preflight.min_free_bytes`, re-checked before THIS unit's generated-file "
        "write rather than once at startup (§11.3): the fleet fills the volume as it runs, so a "
        "floor tested at repo 1 says nothing about repo 180. `0` disables the gate and is the "
        "default only because a payload built by hand in a test has no `config/fleet.yaml` "
        "behind it; `fleet build` always passes the configured value.",
    )


class BuildgenOutput(WorkerOutput):
    """The checkpoint: the plan, the files that landed, and the snapshot the build must read."""

    plan: BuildPlan | None = Field(
        default=None,
        description="`None` until the targets are decided: `BuildPlan` refuses to exist with no "
        "targets and no Gazelle config (Rule 11), so an ingest-only failure carries no half-plan",
    )
    build_bazel_path: str = ""
    module_bazel_path: str = ""
    support_file_paths: list[str] = Field(
        default_factory=list,
        description="Worktree-relative paths materialized for `support_files`, in the order they "
        "were written — the checkpoint's record of which files the generated text's references "
        "were kept by. The publish step stages them from the payload rather than from here, "
        "because a re-entry that skipped this unit still owes the commit.",
    )
    integration_ref: str = Field(
        default="",
        description="`refs/fleet/<run_id>/integration/<seq>` cut at the post-merge tip. This is "
        "what `buildverify` builds against; the branch is never read directly (§3.3 step 1).",
    )
    integration_sha: str = ""
    merge_sha: str = ""
    already_ingested: bool = False
    selected_versions: dict[str, str] = Field(default_factory=dict)
    accepted_overrides: dict[str, str] = Field(default_factory=dict)
    rejected_overrides: list[RejectedOverride] = Field(default_factory=list)
    authored_by_model: bool = Field(
        default=False, description="§3.3's `build_authoring` escape hatch produced the targets"
    )


@register_worker
class BuildgenWorker(BaseWorker[BuildgenInput, BuildgenOutput]):
    """Ingest → `BUILD.bazel` → `MODULE.bazel`, as three separately-resumable units (§3.3)."""

    __slots__ = ("_runner",)

    name: ClassVar[str] = "buildgen"
    phase: ClassVar[Phase] = Phase.BUILD
    input_model: ClassVar[type[WorkerInput]] = BuildgenInput
    output_model: ClassVar[type[WorkerOutput]] = BuildgenOutput
    budget: ClassVar[WorkerBudget] = WorkerBudget(wall_clock_s=1800)

    def __init__(self, *, runner: CommandRunner | None = None) -> None:
        """`runner` is the subprocess seam. The `ModelClient` is NOT a constructor argument: it
        arrives on `ctx.llm` (§7.1), assembled once per run by `RunContext`. A worker the registry
        instantiates with no arguments cannot be handed one here, which is how the LLM rungs came
        to be silently unreachable."""
        self._runner = runner

    # ------------------------------------------------------------------ preconditions

    async def preconditions_hold(self, ctx: WorkerContext, payload: BuildgenInput) -> bool:
        """Admit re-entry only where re-running is a no-op or real work is genuinely owed.

        `_scc/` is reserved for the §3.1 6e coarsened targets and the step-8 audit already rewrote
        any repo that claimed it; a `dest` under it reaching Phase 3 means the audit was skipped,
        and generating there would silently occupy the coarsening namespace.

        The rest is the §7.1 contract read literally: if the checkpoint's referenced paths no
        longer exist the phase re-runs from `phases.base_ref`, so a worktree the reaper has already
        removed refuses re-entry instead of writing a `BUILD.bazel` into a resurrected directory.
        """
        if is_reserved_dest(payload.unit.dest):
            return False
        if not dirs_present(Path(ctx.workdir)):
            return False
        if payload.ingest is not None and not dirs_present(Path(payload.ingest.source_dir)):
            return False
        # A rung that renders no prompt (`context_policy is None`) has no escape hatch left, so
        # nothing is owed; from rung 2 the `build_authoring` slot is genuinely reachable through
        # `ctx.llm`. This used to read `self._model is not None`, which asked whether the process
        # had been wired rather than whether work remained.
        return (
            payload.targets != []
            or payload.gazelle is not None
            or ctx.context_policy is not None
        )

    # ------------------------------------------------------------------ the work

    async def run(
        self, ctx: WorkerContext, payload: BuildgenInput
    ) -> WorkerResult[BuildgenOutput]:
        dest = payload.unit.dest.strip("/")
        units = ([INGEST_UNIT] if payload.ingest is not None else []) + [BUILD_BAZEL_UNIT]
        if payload.write_module_bazel:
            units.append(MODULE_BAZEL_UNIT)
        completed: list[str] = []
        usage = TokenUsage()
        output = BuildgenOutput()

        # -- step 1: ingest, under the single-writer mutex ------------------------------
        # NOTE (D96, round DD): `_ingest` is itself disk-consuming (a real `git fetch`+`merge`)
        # and runs BEFORE the `min_free_bytes` check below, unprotected. Currently dead in
        # production — `cli.py`'s only `BuildgenInput` construction site always passes
        # `ingest=None` — so this is a latent gap, not a reachable one, and not closed here.
        if payload.ingest is not None:
            if self._stopped(ctx):
                return self._interrupted(ctx, output, completed, units)
            failure = await self._ingest(ctx, payload.ingest, output)
            if failure is not None:
                return WorkerResult[BuildgenOutput](
                    status="failed",
                    output=output,
                    completed_units=completed,
                    remaining_units=units,
                    error=failure,
                )
            completed.append(INGEST_UNIT)
            units = units[1:]

        # -- step 2: BUILD.bazel --------------------------------------------------------
        if self._stopped(ctx):
            return self._interrupted(ctx, output, completed, units)
        targets = list(payload.targets)
        if not targets and payload.gazelle is None:
            authored, authoring_usage = await self._author(ctx, payload, dest)
            usage = accumulate(usage, authoring_usage)
            if authored is None:
                return WorkerResult[BuildgenOutput](
                    status="failed",
                    output=output,
                    completed_units=completed,
                    remaining_units=units,
                    usage=usage,
                    error=WorkerError(
                        failure_class=FailureClass.RULE_MISS,
                        # Rung 2 is `build_authoring` (§3.3): a template miss at rung 1 is exactly
                        # what the escape hatch exists for, so the ladder must be allowed to reach
                        # it. Once the model has answered and still produced nothing, it is not.
                        retryable=ctx.context_policy is None,
                        stderr_tail=(
                            f"{dest}: no adapter targets and no gazelle config; nothing to render"
                        ),
                    ),
                )
            targets = authored
            output.authored_by_model = True

        output.plan = BuildPlan(
            unit_id=payload.unit.unit_id,
            dest=dest,
            generated_by="gazelle" if payload.gazelle is not None else "adapter",
            targets=targets,
            workspace_deps=list(payload.workspace_deps),
            toolchains=list(payload.toolchains),
            gazelle=payload.gazelle,
        )
        text = (
            render_gazelle_build(payload.gazelle)
            if payload.gazelle is not None
            else render_build_bazel(targets)
        )
        build_path = Path(ctx.workdir) / dest / "BUILD.bazel"
        # §11.3, before the generated-file write: the fleet fills the volume as it runs, so a
        # floor tested at repo 1 says nothing about repo 180 (same reasoning as `clone.py`'s
        # per-clone check).
        try:
            require_free_space(
                build_path.parent,
                payload.min_free_bytes,
                operation=f"write BUILD.bazel for {dest}",
            )
        except DiskFloorBreached as breach:
            return WorkerResult[BuildgenOutput](
                status="failed",
                output=output,
                completed_units=completed,
                remaining_units=units,
                usage=usage,
                error=WorkerError(
                    failure_class=FailureClass.DISK_EXHAUSTED,
                    retryable=False,
                    stderr_tail=str(breach),
                    exception_type=f"{type(breach).__module__}.{type(breach).__qualname__}",
                ),
            )
        _write(build_path, text)
        output.build_bazel_path = str(build_path)
        # D10: the files the generated text NAMES, written in the same unit that writes the text.
        # Anywhere else and the two can land in different commits — which is the failure mode,
        # not a refinement of it.
        output.support_file_paths = materialize(Path(ctx.workdir), payload.support_files)
        completed.append(BUILD_BAZEL_UNIT)
        units = [u for u in units if u != BUILD_BAZEL_UNIT]

        # -- step 3: MODULE.bazel, after real MVS ---------------------------------------
        if payload.write_module_bazel:
            if self._stopped(ctx):
                return self._interrupted(ctx, output, completed, units)
            module_result = await self._module_bazel(ctx, payload, output, targets=targets)
            usage = accumulate(usage, module_result[1])
            if module_result[0] is not None:
                return WorkerResult[BuildgenOutput](
                    status="failed",
                    output=output,
                    completed_units=completed,
                    remaining_units=units,
                    usage=usage,
                    error=module_result[0],
                )
            completed.append(MODULE_BAZEL_UNIT)

        return WorkerResult[BuildgenOutput](
            status="ok",
            output=output,
            completed_units=completed,
            usage=usage,
            evidence=[p for p in (output.build_bazel_path, output.module_bazel_path) if p]
            + ([output.integration_ref] if output.integration_ref else []),
        )

    # ------------------------------------------------------------------ step 1

    async def _ingest(
        self, ctx: WorkerContext, spec: IngestSpec, output: BuildgenOutput
    ) -> WorkerError | None:
        """Merge one node's rewritten history and cut the immutable snapshot (§3.3 step 1)."""
        git = (
            Git(spec.integration_worktree, runner=self._runner, deadline=ctx.deadline)
            if self._runner is not None
            else Git(spec.integration_worktree, deadline=ctx.deadline)
        )
        mutex = (
            IntegrationMutex(spec.lock_dir, ctx.run_id, timeout_s=max(ctx.time_left(loop_now()), 1))
            if spec.lock_dir is not None
            else None
        )
        source = SourceProvenance(
            repo_id=spec.source_repo_id or str(ctx.repo_id),
            sha=spec.source_sha,
            contract_id=spec.contract_id,
        )
        try:
            result = await ingest(
                git,
                source_dir=Path(spec.source_dir),
                source=source,
                run_id=ctx.run_id,
                integration_branch=spec.integration_branch,
                source_ref=spec.source_ref,
                mutex=mutex,
            )
        except (IngestError, GitError) as exc:
            return WorkerError(
                failure_class=FailureClass.TRANSIENT_INFRA,
                retryable=True,
                stderr_tail=str(exc),
                exception_type=exception_type_name(exc),
            )
        output.merge_sha = result.merge_sha
        output.already_ingested = result.already_present
        output.integration_ref = result.snapshot.ref
        output.integration_sha = result.snapshot.sha
        return None

    # ------------------------------------------------------------------ step 2 escape hatch

    async def _author(
        self, ctx: WorkerContext, payload: BuildgenInput, dest: str
    ) -> tuple[list[BuildTarget] | None, TokenUsage]:
        """§3.3's `build_authoring` slot: a target no generator template covers.

        Rung 1 renders no prompt at all (`ctx.context_policy is None`), so the deterministic path
        gets its chance first and the expensive rung is reached only after it demonstrably failed.
        The model proposes target *shapes*; `render_build_bazel` still writes the file, so the
        emitted text stays byte-identical across processes (§11.6).
        """
        if ctx.context_policy is None:
            return None, TokenUsage()
        evidence: Evidence = {
            "dest": dest,
            "ecosystem": payload.unit.ecosystem.value,
            "srcs": list(payload.unit.srcs),
            "test_srcs": list(payload.unit.test_srcs),
            "internal_deps": [dep.label for dep in payload.unit.internal_deps],
        }
        try:
            response = await author_build_file(ctx.llm, evidence, budget=ctx.budget)
        except LlmError as exc:
            if getattr(exc, "failure_class", None) is not None:
                # D133: `BudgetExhausted`/`TierUnavailable` are the two `LlmError`s that declare
                # their own `failure_class` (`llm/client.py`) — both §11.2/§11.8 fail-closed
                # conditions, not "the model tried and produced nothing usable". Swallowing
                # either into the `None` fallback below used to report a run-terminal outage as
                # a retryable `RULE_MISS` (the caller's fallback classification, `run()` above)
                # and never halt the run. Re-raised, it escapes to `_run_bounded`'s generic
                # handler, which `classify_exception`/`error_from_exception` now classify
                # correctly (including the tier) instead of guessing here.
                raise
            return None, TokenUsage()
        package = response.value.package_path.strip("/") or dest
        targets = [
            BuildTarget(
                package=package,
                name=proposed.name,
                rule=proposed.rule,
                srcs=list(proposed.srcs),
                deps=list(proposed.deps),
                visibility=list(proposed.visibility) or ["//visibility:public"],
            )
            for proposed in response.value.targets
        ]
        return (targets or None), response.usage

    # ------------------------------------------------------------------ step 3

    async def _module_bazel(
        self,
        ctx: WorkerContext,
        payload: BuildgenInput,
        output: BuildgenOutput,
        *,
        targets: Sequence[BuildTarget],
    ) -> tuple[WorkerError | None, TokenUsage]:
        """MVS, then — only on an empty intersection — a model pin that code re-derives.

        `targets` is passed through to `render_module_bazel` and that is load-bearing (D6): the
        renderer derives a `bazel_dep` from every ruleset a target's `load_from` reads out of, and
        a repo whose only claim on `aspect_rules_js` is a `js_binary` load gets no `bazel_dep` at
        all without it — `Unable to find package for @@[unknown repo 'aspect_rules_js' …]`. The
        mechanism existed and this call site was not passing the argument, so it was reachable
        only from tests; that is the defect, not the missing feature.

        `module_targets` outranks the per-repo list when the driver supplies it, because
        MODULE.bazel is ONE root file every dispatch of the wave writes: rendering it from this
        repo's targets would give two repos two different files and a merge conflict where the
        identical superset is a no-op.
        """
        requirements = [r.as_requirement() for r in payload.requirements]
        resolution = reconcile_versions(requirements)
        selected = dict(resolution.selected)
        overrides: dict[str, str] = {}
        usage = TokenUsage()

        for conflict in resolution.conflicts:
            decision_usage, accepted = await self._resolve_conflict(
                ctx, conflict, requirements, output
            )
            usage = accumulate(usage, decision_usage)
            if accepted is None:
                return (
                    WorkerError(
                        failure_class=FailureClass.DEP_CONFLICT,
                        # See the module docstring: `DEP_CONFLICT` defaults to non-retryable, and
                        # accepting that at rung 1 would end the ladder before the very rung §3.3
                        # nominates for this failure.
                        retryable=ctx.context_policy is None,
                        stderr_tail=str(conflict),
                    ),
                    usage,
                )
            coord_key, version, mechanism = accepted
            selected[coord_key] = version
            if mechanism == "single_version_override":
                overrides[coord_key] = version

        deps = resolve_workspace_deps(payload.workspace_deps, selected)
        text = render_module_bazel(
            deps,
            module_name=payload.module_name,
            ruleset_versions=payload.ruleset_versions,
            module_version=payload.module_version,
            toolchains=payload.toolchains,
            targets=payload.module_targets or list(targets),
            single_version_overrides=overrides or None,
        )
        module_path = Path(ctx.workdir) / payload.module_bazel_path
        _write(module_path, text)
        output.module_bazel_path = str(module_path)
        output.selected_versions = selected
        output.accepted_overrides = overrides
        if output.plan is not None:
            output.plan.workspace_deps = list(deps)
        return None, usage

    async def _resolve_conflict(
        self,
        ctx: WorkerContext,
        conflict: VersionConflict,
        requirements: Sequence[VersionRequirement],
        output: BuildgenOutput,
    ) -> tuple[TokenUsage, tuple[str, str, str] | None]:
        """Ask `conflict_resolution`, then **re-derive the answer against every spec**.

        `validate_override` is the load-bearing line, and it is code: the model chooses which
        promise to break, and it may not tell us which promises it kept. A pin whose violations
        are understated is rejected outright — never written, never rendered around — because a
        `MODULE.bazel` that quietly breaks 30 repos' declared upper bound reaches a reviewer
        looking exactly like one that breaks none.
        """
        if ctx.context_policy is None:
            return TokenUsage(), None
        evidence: Evidence = {
            "coord_key": conflict.coord_key,
            "specs": list(conflict.specs),
            "repo_ids": list(conflict.repo_ids),
            "reason": conflict.reason,
        }
        try:
            response = await resolve_version_conflict(ctx.llm, evidence, budget=ctx.budget)
        except LlmError as exc:
            if getattr(exc, "failure_class", None) is not None:
                # D133: same reasoning as `_author`'s sibling site — a declared-`failure_class`
                # `LlmError` is a fail-closed §11.2/§11.8 condition, not an unresolved conflict.
                # Swallowing it here used to report a run-terminal outage as a retryable
                # `DEP_CONFLICT` and never halt the run.
                raise
            return TokenUsage(), None
        decision = validate_override(response.value, requirements)
        if not decision.accepted:
            output.rejected_overrides.append(
                RejectedOverride(
                    coord_key=decision.coord_key,
                    proposed_version=decision.version,
                    mechanism=decision.mechanism,
                    violated=list(decision.violated),
                    undisclosed=list(decision.undisclosed),
                    reason=decision.reason,
                )
            )
            return response.usage, None
        return response.usage, (decision.coord_key, decision.version, decision.mechanism)

    # ------------------------------------------------------------------ internals

    @staticmethod
    def _stopped(ctx: WorkerContext) -> bool:
        return ctx.cancelled() or ctx.expired(loop_now())

    def _interrupted(
        self,
        ctx: WorkerContext,
        output: BuildgenOutput,
        completed: list[str],
        remaining: list[str],
    ) -> WorkerResult[BuildgenOutput]:
        """Interrupted between units. With units landed this is `partial`, and the checkpoint's
        `remaining_units` is what re-entry owes — a full replay would re-merge a merged history."""
        owed = [unit for unit in remaining if unit not in completed]
        if completed:
            return WorkerResult[BuildgenOutput](
                status="partial",
                output=output,
                completed_units=completed,
                remaining_units=owed,
            )
        # A genuine `ctx.cancelled()` is an operator decision, not a chargeable attempt, so its
        # `failure_class` must not read `TIMEOUT` -- that misdescribes an operator cancel as a
        # real expiry for retry/backoff accounting purposes.
        cancelled = ctx.cancelled()
        return WorkerResult[BuildgenOutput](
            status="cancelled" if cancelled else "timeout",
            output=output,
            remaining_units=owed,
            error=WorkerError(
                failure_class=FailureClass.TRANSIENT_INFRA if cancelled else FailureClass.TIMEOUT,
                retryable=True,
                stderr_tail=f"{self.name}: stopped before {owed}",
            ),
        )


def _write(path: Path, text: str) -> None:
    """Write a generated file, creating its package directory. Generated, never hand-edited."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def materialize(root: Path, files: Sequence[SupportFile]) -> list[str]:
    """Put every file the generated text NAMES on disk, and report what landed (D10).

    Ordered by `path` and written unconditionally from `SupportFile.content`, because the content
    is already the *resolved* one: the driver read the repo's real lockfile at plan time and put
    its bytes here, so writing is deterministic and does not depend on which snapshot this
    worktree was cut from. A worker that re-read the tree instead would give two repos of one wave
    two different `pnpm-lock.yaml`s and a merge conflict on the integration branch (§11.6).

    An identical rewrite is the point of `git add` staging nothing on re-entry — the publish step
    reads that as "already published" rather than minting an empty commit.

    **Public, and called twice per dispatch on purpose.** `cli.BuildPipelineWorker._publish` calls
    it again immediately before `git add`, because between this call and that one a *build system*
    has run in the same worktree and some of them write to it (see there). Determinism has to be
    re-established at the point the bytes are staged, not only at the point they are generated.
    """
    written: list[str] = []
    for support in sorted(files, key=lambda f: f.path):
        _write(root / support.path, support.content)
        written.append(support.path)
    return written
