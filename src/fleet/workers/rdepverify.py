"""Phase 4 steps 2–3: the rdeps closure, the verified target set, and the report (SPEC §3.4).

`bazel query 'rdeps(//..., //<dest>/...)'` → the affected target set → `bazel test <targets>`. This
is the blast-radius check, and the only thing that makes it *honest* is disclosure.

**The verified target set has two readings and they are one criterion.** §3.4's success criterion
asks for the FULL closure; its bounds table caps the tested set at a deterministic seeded sample.
Both, because a repo whose closure exceeds `rdeps_limit` must neither stall forever nor report
40 000 untested targets as green. What reconciles them is that the reduction is *declared*:
`rdeps_truncated = true` forces `VerificationReport.equivalence = CLOSURE_SAMPLED` — derived by the
model, never supplied by an optimistic caller — and `CLOSURE_SAMPLED` forces the PR to a draft
(`workers/prwriter.py`). A sampled verification is a disclosed reduction that a human clears,
never a silent pass.

The sampling itself lives in `bazel/query.py` (`select_tested_targets`) and is pure, so the
property this module depends on is tested byte-for-byte offline. What this worker adds is the
plumbing that cannot be pure: running the queries under `ctx.deadline` through an injected
`CommandRunner`, writing the tested labels to a `--target_pattern_file` rather than exploding
them into argv, and turning a non-zero `bazel test` into a structured, persistable failure.

**A failed query is never read as an empty closure** — `rdeps_closure` raises, and this module
converts that into a loud `WorkerError` rather than a green report over zero targets (Rule 11).

**The exit codes are `buildverify`'s, and this module owns none of them.** The table above
`classify_build_failure` is the one place Bazel's exit codes are written down, and every row in it
was reproduced against the real binary. This worker's `bazel test` over the verified target set is
read through that table — `no_test_targets` and `error_from_proc` with `unit=TEST_UNIT`, which is
what the step IS, rather than with this worker's own unit *name*. Passing `unit="rdeps_test"` was
the defect: it matched no row keyed on the test step, so exit 3 (a real test failure in the blast
radius) classified as `BUILD_ERROR` and exit 4 (nothing in the closure is a test) was a retryable
failure that burned all three ADR-0014 rungs. A second copy of the mapping here would have drifted
from the first the same way.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import ClassVar

from pydantic import Field

from fleet.bazel.query import (
    DEFAULT_RDEPS_LIMIT,
    DEFAULT_SAMPLE_N,
    BazelQueryError,
    RdepsClosure,
    bazel_test_argv,
    rdeps_closure,
    write_target_pattern_file,
)
from fleet.models.enums import Equivalence, FailureClass, Phase, RepoStatus, StubFidelity
from fleet.models.tasks import VerificationReport
from fleet.orchestrator.registry import register_worker
from fleet.sandbox.container import ContainerSandbox
from fleet.sandbox.worktree import sandbox_name
from fleet.util.errors import exception_type_name
from fleet.util.proc import CommandRunner, run
from fleet.workers.base import (
    BaseWorker,
    WorkerBudget,
    WorkerContext,
    WorkerError,
    WorkerInput,
    WorkerOutput,
    WorkerResult,
    loop_now,
)
from fleet.workers.buildverify import (
    TEST_UNIT,
    CacheMount,
    LoggedRunner,
    dirs_present,
    error_from_proc,
    no_test_targets,
)

__all__ = [
    "CLOSURE_UNIT",
    "RDEPS_TEST_UNIT",
    "RdepverifyInput",
    "RdepverifyOutput",
    "RdepverifyWorker",
]

CLOSURE_UNIT = "rdeps_closure"
RDEPS_TEST_UNIT = "rdeps_test"


class RdepverifyInput(WorkerInput):
    """The blast-radius run. The Phase 3 verdict arrives as data because §3.4 step 3 assembles the
    report from `attempts` rows only — this worker adds the rdeps columns, it does not re-judge
    the repo's own build."""

    dest: str = Field(min_length=1)
    integration_ref: str = Field(
        min_length=1,
        description="§3.4 step 1: a FRESH snapshot ref cut at this task's start — current, and "
        "still immutable for the build's duration. Never the branch name.",
    )
    build_ok: bool = Field(default=True, description="Phase 3's `bazel build`, from its attempt")
    test_ok: bool = Field(default=True, description="Phase 3's own `bazel test`, from its attempt")
    worktree: str | None = None
    log_dir: str = "artifacts/logs"
    affected_only: bool = Field(
        default=True,
        description="`verify.affected_only`; false restores the full closure for a final gate run",
    )
    rdeps_limit: int = Field(default=DEFAULT_RDEPS_LIMIT, ge=1)
    rdeps_sample_n: int = Field(default=DEFAULT_SAMPLE_N, ge=0)
    rdeps_sample_seed: str | None = Field(
        default=None, description="Pin the seed to reproduce a prior run's sample exactly (§11.6)"
    )
    jobs: int | None = None
    keep_going: bool = True
    build_event_json: bool = True
    cache_mounts: list[CacheMount] = Field(
        default_factory=list,
        description="`verify.disk_cache`/`verify.repository_cache`, the SAME role-tagged objects "
        "Phase 3 was handed. This worker never containerises, so they are flags only — the "
        "read-write bind mount half of `CacheMount` has no consumer here.",
    )
    extra_args: list[str] = Field(default_factory=list)
    verified_against_stubs: list[str] = Field(
        default_factory=list,
        description="coord_keys whose `stubs` row was ACTIVE throughout. Non-empty ⇒ STUB_LIMITED, "
        "which outranks CLOSURE_SAMPLED (EQUIVALENCE_RANK, §5.1) — this worker supplies the fact "
        "and `VerificationReport` applies the precedence.",
    )
    stub_fidelity: dict[str, StubFidelity] = Field(default_factory=dict)
    revalidation_round: int = Field(default=0, ge=0)


class RdepverifyOutput(WorkerOutput):
    """The report plus the sampling evidence a reviewer needs to audit the reduction."""

    report: VerificationReport
    integration_ref: str = ""
    rdeps_query: str = ""
    rdeps_target_count: int = Field(default=0, ge=0)
    rdeps_tested: int = Field(default=0, ge=0)
    rdeps_truncated: bool = False
    rdeps_sample_seed: str = ""
    rdeps_sample_n: int = Field(default=0, ge=0)
    direct_rdeps: int = Field(default=0, ge=0)
    target_pattern_file: str = ""
    test_command: list[str] = Field(default_factory=list)
    test_exit_code: int | None = None
    test_log_path: str | None = None
    rdeps_no_test_targets: bool = Field(
        default=False,
        description="`bazel test` over the verified target set exited 4: the closure was built "
        "and NOTHING in it is a test. Its own recorded outcome, neither a pass nor a failure — "
        "and disclosed rather than folded into `rdeps_ok`, because a reviewer reading a PASS over "
        "a blast radius that ran zero tests is entitled to know that is what happened.",
    )

    @property
    def equivalence(self) -> Equivalence:
        """Always the report's: one derivation, so the PR and the report cannot disagree."""
        return self.report.equivalence


@register_worker
class RdepverifyWorker(BaseWorker[RdepverifyInput, RdepverifyOutput]):
    """`rdeps` → the verified target set → `bazel test`, with the reduction disclosed (§3.4)."""

    __slots__ = ("_runner",)

    name: ClassVar[str] = "rdepverify"
    phase: ClassVar[Phase] = Phase.VERIFY
    input_model: ClassVar[type[WorkerInput]] = RdepverifyInput
    output_model: ClassVar[type[WorkerOutput]] = RdepverifyOutput
    budget: ClassVar[WorkerBudget] = WorkerBudget(wall_clock_s=3600, max_subprocesses=4)

    def __init__(self, *, runner: CommandRunner | None = None) -> None:
        """No `ModelClient`: §3.4 step 3 says the report is assembled from `attempts` rows only —
        no prose, no model input. The one Phase 4 LLM slot is the PR body, and it lives next door
        in `prwriter.py`."""
        self._runner = runner

    # ------------------------------------------------------------------ preconditions

    async def preconditions_hold(self, ctx: WorkerContext, payload: RdepverifyInput) -> bool:
        """§3.4's precondition, checked rather than assumed: Phase 3 `SUCCEEDED`.

        Consulted through `ctx.db` — the persisted phase row — because "the previous phase went
        green" is exactly the kind of fact a resumed run must re-read rather than infer from a
        payload it also carries. A BUILD row in any other state means re-entering here would test
        a blast radius around a package that never built.

        `integration_ref` must still be an immutable `refs/…` ref for the same reason it must be
        in Phase 3: §3.4 step 1's "current integration branch tip" is a *fresh snapshot*, not the
        moving branch.
        """
        if not payload.integration_ref.startswith("refs/"):
            return False
        if not dirs_present(Path(payload.worktree or ctx.workdir)):
            return False
        row = await ctx.db.get_phase(str(ctx.run_id), ctx.repo_id, Phase.BUILD)
        if row is None:
            # No BUILD row at all is a first admission by the runner, not evidence of a failure;
            # refusing here would deadlock a fresh run on a row nothing has written yet.
            return True
        return row.status is RepoStatus.SUCCEEDED

    # ------------------------------------------------------------------ the work

    async def run(
        self, ctx: WorkerContext, payload: RdepverifyInput
    ) -> WorkerResult[RdepverifyOutput]:
        worktree = Path(payload.worktree or ctx.workdir)
        runner = self._runner_for(ctx, payload)

        if self._stopped(ctx):
            return self._refused(ctx, payload, [CLOSURE_UNIT, RDEPS_TEST_UNIT])

        try:
            closure = await rdeps_closure(
                payload.dest,
                runner=runner,
                cwd=worktree,
                deadline=ctx.deadline,
                affected_only=payload.affected_only,
                limit=payload.rdeps_limit,
                sample_n=payload.rdeps_sample_n,
                seed=payload.rdeps_sample_seed,
                cache_flags=self._query_cache_flags(payload),
            )
        except BazelQueryError as exc:
            # Rule 11: an empty target list from a failed query is indistinguishable from a clean
            # closure of zero, and one of those two is a green PR over an untested blast radius.
            return WorkerResult[RdepverifyOutput](
                status="failed",
                output=self._output(payload, ctx, closure=None, tested_ok=False),
                remaining_units=[CLOSURE_UNIT, RDEPS_TEST_UNIT],
                error=WorkerError(
                    failure_class=FailureClass.BUILD_ERROR,
                    retryable=True,
                    exit_code=exc.result.exit_code,
                    stderr_tail=str(exc),
                    artifact_ref=(
                        None if exc.result.stderr_path is None else str(exc.result.stderr_path)
                    ),
                    exception_type=exception_type_name(exc),
                ),
                evidence=[payload.integration_ref],
            )

        if self._stopped(ctx):
            # The closure is landed work: re-entry owes the test run, not the query.
            return WorkerResult[RdepverifyOutput](
                status="partial",
                output=self._output(payload, ctx, closure=closure, tested_ok=False),
                completed_units=[CLOSURE_UNIT],
                remaining_units=[RDEPS_TEST_UNIT],
                evidence=[payload.integration_ref],
            )

        pattern_file = write_target_pattern_file(
            closure.tested, self._pattern_path(ctx, payload)
        )
        argv = bazel_test_argv(
            pattern_file=pattern_file,
            build_event_json_file=(
                worktree / f"bazel-rdeps-{ctx.attempt}-events.json"
                if payload.build_event_json
                else None
            ),
            jobs=payload.jobs,
            keep_going=payload.keep_going,
            extra=[
                # §3.4's bounds table puts the persistent build cache in THIS section, and this
                # `bazel test` over the blast radius used to run with neither flag: the caches
                # `_cache_mounts` creates and Phase 3 fills were named on Phase 3's argv and on
                # nothing here, so the widest build in the pipeline re-executed every action and
                # re-fetched every module from cold. `sandboxed=False` is not a default to revisit
                # but the only correct value: this worker has no `image`, builds no container and
                # emits no `--volume=`, so `/cache/<name>` names nothing — bazel would create it
                # at the HOST filesystem root (or fail on a read-only one) and the shared caches
                # would stay empty while every attempt paid a cold fetch. Containerising Phase 4
                # is a separate change; it would have to add the image, the mounts and the
                # `sandboxed=` argument together, exactly as `buildverify._bazel_argv` does.
                *(cache.flag(sandboxed=False) for cache in payload.cache_mounts),
                # Last, so an operator's hand-written `--disk_cache=` keeps winning: Bazel's
                # parser takes the LAST occurrence of a non-`allowMultiple` option (checked on the
                # vendored 9.2.0 with `canonicalize-flags --for_command=test`). Same order as
                # `buildverify._bazel_argv`, because a repo whose Phase 3 and Phase 4 resolved the
                # same flag differently is worse than either answer.
                *payload.extra_args,
            ],
        )
        result = await runner(argv, cwd=worktree, deadline=ctx.deadline)
        # `buildverify`'s exit-code table is the authority and this step is a `bazel test`, so it
        # is read with `unit=TEST_UNIT` rather than with this worker's unit NAME. Passing
        # `unit="rdeps_test"` — which is what shipped — fell through every table row keyed on the
        # test step: exit 3 (TESTS_FAILED) classified as `BUILD_ERROR`, so a Phase 4 blast-radius
        # regression arrived at the ladder as a build defect and was repaired by regenerating a
        # `BUILD.bazel` that was fine; and exit 4 (NO_TESTS_FOUND) was a retryable failure that
        # burned all three attempts on a closure that simply contains no test target.
        nothing_to_test = no_test_targets(result, unit=TEST_UNIT)
        output = self._output(
            payload, ctx, closure=closure, tested_ok=result.ok or nothing_to_test
        )
        output.target_pattern_file = str(pattern_file)
        output.test_command = list(argv)
        output.test_exit_code = result.exit_code
        output.test_log_path = None if result.stderr_path is None else str(result.stderr_path)
        output.rdeps_no_test_targets = nothing_to_test

        if not result.ok and not nothing_to_test:
            return WorkerResult[RdepverifyOutput](
                status="timeout" if result.timed_out else "failed",
                output=output,
                completed_units=[CLOSURE_UNIT],
                remaining_units=[RDEPS_TEST_UNIT],
                error=error_from_proc(result, unit=TEST_UNIT),
                evidence=[payload.integration_ref, str(pattern_file)],
            )
        return WorkerResult[RdepverifyOutput](
            status="ok",
            output=output,
            completed_units=[CLOSURE_UNIT, RDEPS_TEST_UNIT],
            evidence=[payload.integration_ref, str(pattern_file)],
        )

    async def on_cancel(self, ctx: WorkerContext) -> None:
        """Remove the attempt's container by name: `CancelledError` never reaches a child (§11.1),
        and killing the `docker run` client does not stop what it started."""
        sandbox = ContainerSandbox(runner=self._runner or run)
        with contextlib.suppress(OSError, ValueError):
            await sandbox.remove(sandbox_name(ctx.run_id, ctx.repo_id, ctx.attempt))

    # ------------------------------------------------------------------ internals

    def _runner_for(self, ctx: WorkerContext, payload: RdepverifyInput) -> CommandRunner:
        """The default runner KEEPS its logs, and that is not a convenience.

        `bazel/query.query_stdout` reads the closure from `ProcResult.stdout_path`: a
        40 000-target closure is megabytes and `stdout_tail` is capped at 32 KiB, so a runner
        without a log directory would silently drop most of the blast radius — or, since that
        module refuses to guess, raise. Either way the log directory is load-bearing.
        """
        if self._runner is not None:
            return self._runner
        return LoggedRunner(
            Path(payload.log_dir) / str(ctx.run_id), stem=f"{self.name}-{ctx.attempt}"
        )

    @staticmethod
    def _query_cache_flags(payload: RdepverifyInput) -> list[str]:
        """The shared caches a `bazel query` can actually USE: the repository cache, and only it.

        **Both flags parse for `query`; only one of them does anything.** On the vendored 9.2.0,
        `bazel canonicalize-flags --for_command=query -- --disk_cache=/a --repository_cache=/r`
        echoes both back unchanged, so neither is the exit-2 `COMMAND_LINE_ERROR` that
        `UNREPEATABLE_EXIT_CODES` would burn an attempt on. Accepting a flag is not using it,
        though, so it was measured: a `bazel query 'deps(//:all)'` over a workspace with one
        `bazel_dep` wrote **2.3 MB into `--repository_cache`** and **not one file into
        `--disk_cache`** — and a rerun from a fresh `--output_user_root` against that same
        repository cache answered the query with `--repository_disable_download` set, i.e. the
        cache was read back and the whole module graph resolved with the network refused.

        That asymmetry is what `--disk_cache` is documented to be: "a path to a directory where
        Bazel can read and write actions and action outputs". `query` runs the loading and
        analysis-free half of a build and executes no actions at all, so there is nothing for it
        to read or write there. So the disk cache is deliberately NOT emitted here — not because
        Bazel rejects it, but because there is no measurement in which it helps, and a flag on the
        line implies a cache is doing work.

        The repository cache is a different story and is the fetch this phase kept repeating:
        `rdeps_closure` loads the same module graph Phase 3 just fetched, and under `verify.
        network = "none"` (should Phase 4 ever be containerised) a cold fetch is not a slow query
        but an impossible one.

        `sandboxed=False`, unconditionally and for exactly the reason the `bazel test` wiring
        below gives: this worker has no `image`, builds no container and emits no `--volume=`, so
        `/cache/<name>` names nothing on this host and bazel handed one would create it at the
        filesystem root while the real cache stayed empty.
        """
        return [
            cache.flag(sandboxed=False)
            for cache in payload.cache_mounts
            if cache.role == "repository"
        ]

    def _pattern_path(self, ctx: WorkerContext, payload: RdepverifyInput) -> Path:
        return (
            Path(payload.log_dir)
            / str(ctx.run_id)
            / f"{ctx.repo_id}-{ctx.attempt}-rdeps-targets.txt"
        )

    @staticmethod
    def _stopped(ctx: WorkerContext) -> bool:
        return ctx.cancelled() or ctx.expired(loop_now())

    def _output(
        self,
        payload: RdepverifyInput,
        ctx: WorkerContext,
        *,
        closure: RdepsClosure | None,
        tested_ok: bool,
    ) -> RdepverifyOutput:
        """Assemble the report. `equivalence` is DERIVED by `VerificationReport`, never passed in.

        `verdict` is `PASS` only when every mechanical fact says so — Phase 3's build and test,
        and the rdeps run over the verified target set. A truncated closure does not make the
        verdict `FAIL`; it makes the *equivalence* `CLOSURE_SAMPLED`, which is a different claim
        and the one that forces the PR to a draft.
        """
        truncated = closure.truncated if closure is not None else False
        report = VerificationReport(
            run_id=ctx.run_id,
            repo_id=ctx.repo_id,
            build_ok=payload.build_ok,
            test_ok=payload.test_ok,
            rdeps_query=closure.query if closure is not None else "",
            rdeps_target_count=closure.total_count if closure is not None else 0,
            rdeps_tested=closure.tested_count if closure is not None else 0,
            rdeps_ok=tested_ok,
            rdeps_truncated=truncated,
            verdict=(
                "PASS" if (payload.build_ok and payload.test_ok and tested_ok) else "FAIL"
            ),
            verified_against_stubs=list(payload.verified_against_stubs),
            stub_fidelity=dict(payload.stub_fidelity),
            revalidation_round=payload.revalidation_round,
        )
        return RdepverifyOutput(
            report=report,
            integration_ref=payload.integration_ref,
            rdeps_query=report.rdeps_query,
            rdeps_target_count=report.rdeps_target_count,
            rdeps_tested=report.rdeps_tested,
            rdeps_truncated=truncated,
            rdeps_sample_seed=closure.seed if closure is not None else "",
            rdeps_sample_n=closure.sample_n if closure is not None else 0,
            direct_rdeps=len(closure.direct) if closure is not None else 0,
        )

    def _refused(
        self, ctx: WorkerContext, payload: RdepverifyInput, owed: list[str]
    ) -> WorkerResult[RdepverifyOutput]:
        return WorkerResult[RdepverifyOutput](
            status="cancelled" if ctx.cancelled() else "timeout",
            output=self._output(payload, ctx, closure=None, tested_ok=False),
            remaining_units=owed,
            error=WorkerError(
                failure_class=FailureClass.TIMEOUT,
                retryable=True,
                stderr_tail=f"{self.name}: stopped before {owed}",
            ),
            evidence=[payload.integration_ref],
        )
