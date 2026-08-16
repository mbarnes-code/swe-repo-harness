"""The Phase 3/4 workers: what a green build is allowed to claim (SPEC §3.3, §3.4).

Every test here is about a failure with a cost attached, not about a shape:

* a **sampled** rdeps closure reported as an unqualified pass is 40 000 untested targets called
  green — so `rdeps_truncated` must force `CLOSURE_SAMPLED` *and* force the PR to a draft;
* a model-proposed version pin written without re-derivation is a `MODULE.bazel` that quietly
  breaks 30 repos' declared upper bound and reaches a reviewer looking exactly like one that
  breaks none;
* a 400 KB stderr **rejected** by validation is an attempt that never reaches SQLite, an
  `attempts` counter that never increments, and a repair loop re-running the identical failing
  build forever — the truncation is the fix, and it has to be exercised;
* `MERGED` assumed rather than ingested is the deadlock that stranded the fleet after wave 0;
* a build recorded against "the integration branch" is a `BUILD_ERROR` that is a property of
  scheduling luck, which is why the attempt carries an immutable `integration_ref`.

`bazel`, `docker` and `gh` are absent on this host and none of them is needed: every subprocess
goes through an injected `CommandRunner` and every model call through a fake `ModelClient`, so
the argv that WOULD have run is what gets asserted. The one test that genuinely needs the real
binary skips loudly.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import UUID

import pytest

from fleet import ecosystems
from fleet.bazel.query import rdeps_query, sample_seed_for
from fleet.cli import (
    BuildInput,
    BuildPipelineWorker,
    RootFileConflictError,
    _BuildPlan,
    _module_inputs,
    _resolve_support_files,
)
from fleet.llm.client import CallBudget, ModelResponse, TierUnavailable
from fleet.llm.schemas import BuildFileProposal, PrBody, PrTitle, VersionConflictResolution
from fleet.models.build import BuildTarget, BuildUnit, SupportFile, WorkspaceDep
from fleet.models.enums import (
    ContextPolicy,
    Ecosystem,
    Equivalence,
    FailureClass,
    ModelTier,
    Phase,
    PrState,
    RepoStatus,
    StubFidelity,
    StubState,
)
from fleet.models.repo import Coordinate
from fleet.models.tasks import TokenUsage, VerificationReport
from fleet.orchestrator.retry import LadderState, RetryAction, RetryPolicy
from fleet.settings import BuildSection
from fleet.state.repository import PhaseRow
from fleet.util.proc import ProcResult
from fleet.vcs import build_forge
from fleet.vcs.forge import Forge
from fleet.vcs.github import GitHubCli
from fleet.workers.base import WorkerContext, implements_preconditions
from fleet.workers.buildgen import (
    BuildgenInput,
    BuildgenWorker,
    ExternalRequirement,
)
from fleet.workers.buildverify import (
    BUILD_UNIT,
    C_TOOLCHAIN_PROBE,
    CACHE_MOUNT_ROOT,
    NO_TESTS_FOUND,
    TEST_UNIT,
    BuildverifyInput,
    BuildverifyWorker,
    CacheMount,
    LoggedRunner,
    classify_build_failure,
)
from fleet.workers.prwriter import (
    STUB_BANNER,
    DependencyPr,
    PrwriterInput,
    PrwriterWorker,
)
from fleet.workers.rdepverify import (
    CLOSURE_UNIT,
    RDEPS_TEST_UNIT,
    RdepverifyInput,
    RdepverifyWorker,
)

RUN_ID = UUID("00000000-0000-4000-8000-0000000000b3")
REPO = "acme-widget"
OWNER = "host:container:99:boot"
FENCE = 3
SNAPSHOT = "refs/fleet/00000000-0000-4000-8000-0000000000b3/integration/7"


# =======================================================================================
# fakes
# =======================================================================================


class FakeDb:
    """`ReadOnlyRepository` narrowed to the one row these workers read: the phase's status."""

    def __init__(self, phase_status: RepoStatus | None = RepoStatus.SUCCEEDED) -> None:
        self.phase_status = phase_status

    async def get_phase(self, run_id: str, repo_id: str, phase: Phase) -> PhaseRow | None:
        if self.phase_status is None:
            return None
        return PhaseRow(
            run_id=run_id,
            repo_id=repo_id,
            phase=phase,
            status=self.phase_status,
            attempts=1,
            max_attempts=3,
            lease_owner=OWNER,
            lease_fence=FENCE,
            lease_expires_at=None,
            last_error=None,
            updated_at="2026-08-09T00:00:00+00:00",
        )


class RecordingRunner:
    """A `CommandRunner` that records argv and replies from a scripted rule list.

    An instance rather than a closure so the worker can hold it in a `__slots__` seam — which is
    also how the production default (`LoggedRunner`) is shaped.
    """

    def __init__(self, rules) -> None:
        self.rules = rules  # list[(predicate, ProcResult factory)]
        self.calls: list[tuple[str, ...]] = []

    async def __call__(self, argv, *, cwd=None, env=None, deadline=None, timeout_s=None):
        parts = tuple(argv)
        self.calls.append(parts)
        for predicate, reply in self.rules:
            if predicate(parts):
                return reply(parts)
        return ProcResult(
            argv=parts, exit_code=0, stdout_tail="", stderr_tail="", duration_ms=1, timed_out=False
        )

    def argv_for(self, needle: str) -> tuple[str, ...] | None:
        for call in self.calls:
            if needle in call:
                return call
        return None


def read(path) -> str:
    """Read a file from inside an async test.

    A sync helper on purpose: `ASYNC240` flags `pathlib` calls inside a coroutine (a rule aimed
    at trio's blocking-IO discipline), and a one-line `read_text` in a test is not worth a thread.
    """
    return Path(path).read_text(encoding="utf-8")


def ok(stdout: str = "") -> object:
    return lambda parts: ProcResult(
        argv=parts, exit_code=0, stdout_tail=stdout, stderr_tail="", duration_ms=5, timed_out=False
    )


class FakeModelClient:
    """A `ModelClient` whose answers are scripted per role. Never touches a network."""

    def __init__(self, answers: dict[str, object]) -> None:
        self.answers = answers
        self.roles: list[str] = []

    async def complete(
        self,
        role,
        messages,
        response_model,
        *,
        tier_override=None,
        max_output_tokens=None,
        timeout_s=None,
        budget=None,
    ):
        self.roles.append(str(role))
        value = self.answers[str(role)]
        return ModelResponse(
            value=value,
            usage=TokenUsage(role=str(role), input_tokens=10, output_tokens=5, cost_usd=0.001),
            mode="JSON_SCHEMA",
            finish_reason="stop",
        )

    def stream(self, role, messages, response_model, *, budget=None):  # pragma: no cover
        raise NotImplementedError("these workers use complete(), never stream()")

    async def capabilities(self, role):  # pragma: no cover
        raise NotImplementedError


class UnavailableModelClient:
    """A `ModelClient` every one of whose targets is unhealthy (§11.8).

    The default for `make_ctx`, because the context ALWAYS carries a client now: "this run has no
    model" is no longer spelled `Worker(model=None)`, it is spelled "the call fails". Every one of
    these workers treats an `LlmError` as a degradation — no prose, no diagnosis, no authored
    targets — so this fake exercises exactly the path the old `model=None` constructor did.
    """

    async def complete(
        self, role, messages, response_model, **kwargs
    ) -> ModelResponse[Any]:
        raise TierUnavailable(ModelTier.WORKHORSE, ("fake:fake-workhorse",))

    def stream(self, role, messages, response_model, **kwargs):  # pragma: no cover
        raise NotImplementedError("these workers use complete(), never stream()")

    async def capabilities(self, role):  # pragma: no cover
        raise NotImplementedError


def make_ctx(
    workdir: Path,
    *,
    db: FakeDb | None = None,
    attempt: int = 1,
    context_policy: ContextPolicy | None = None,
    seconds_left: float = 600.0,
    model: object | None = None,
) -> WorkerContext:
    """A context with a REAL deadline on the loop's clock and inert collaborators.

    `monotonic()` because that IS `loop.time()` for the default event loop (conftest does the
    same): a deadline expressed in wall-clock time breaks the moment NTP steps the host.

    `model` is §7.1's ONE call surface. It is a context field rather than a worker constructor
    argument, so a test cannot wire a client the production registry could never supply.
    """
    deadline = monotonic() + seconds_left
    sentinel: object = object()
    return WorkerContext(
        run_id=RUN_ID,
        repo_id=REPO,
        attempt=attempt,
        workdir=str(workdir),
        lease_owner=OWNER,
        lease_fence=FENCE,
        deadline=deadline,
        cancel=asyncio.Event(),
        budget=CallBudget(remaining_tokens=100_000, remaining_usd=10.0, deadline=deadline),
        db=db or FakeDb(),  # type: ignore[arg-type]
        llm=UnavailableModelClient() if model is None else model,  # type: ignore[arg-type]
        router=sentinel,  # type: ignore[arg-type]
        limits=sentinel,  # type: ignore[arg-type]
        log=sentinel,  # type: ignore[arg-type]
        context_policy=context_policy,
    )


def a_report(**overrides) -> VerificationReport:
    """A PASSing report; `equivalence` is always DERIVED, never passed in."""
    fields = {
        "run_id": RUN_ID,
        "repo_id": REPO,
        "build_ok": True,
        "test_ok": True,
        "rdeps_ok": True,
        "rdeps_query": rdeps_query("java/com/acme/widget"),
        "rdeps_target_count": 12,
        "rdeps_tested": 12,
        "verdict": "PASS",
    }
    fields.update(overrides)
    return VerificationReport(**fields)


def a_pr_payload(**overrides) -> PrwriterInput:
    fields = {
        "report": a_report(),
        "wave_index": 2,
        "branch": "migrate/acme-widget",
        "source_url": "https://github.example/acme/widget",
        "source_sha": "f" * 40,
    }
    fields.update(overrides)
    return PrwriterInput(**fields)


PR_VIEW_MERGED = json.dumps(
    {"state": "MERGED", "mergedAt": "2026-08-01T10:00:00Z", "mergeCommit": {"oid": "abc1234"}}
)
PR_VIEW_OPEN = json.dumps({"state": "OPEN", "mergedAt": None, "mergeCommit": None})


def gh_runner(view_payload: str = PR_VIEW_MERGED) -> RecordingRunner:
    return RecordingRunner(
        [
            (lambda p: "view" in p, ok(view_payload)),
            (lambda p: "create" in p, ok("https://github.example/acme/monorepo/pull/41\n")),
            (lambda p: "ready" in p, ok("")),
        ]
    )


# =======================================================================================
# (1) the four classes exist, and they claim their preconditions
# =======================================================================================


def test_all_four_phase_3_4_workers_are_instantiable_and_claim_re_entry() -> None:
    """`preconditions_hold` is abstract precisely so a worker cannot inherit blind replay (§7.1).

    Why it matters: a defaulted `return True` re-admits a phase unconditionally, which is how a
    relocation re-runs over an already-renamed tree and produces `java/java/com/x`. These four
    were 17–22 line stubs overriding only `run()`, so they were abstract and *could not be
    constructed at all* — the registry held four classes the runner could never instantiate.
    """
    for cls in (BuildgenWorker, BuildverifyWorker, RdepverifyWorker, PrwriterWorker):
        assert implements_preconditions(cls), cls.__name__
        instance = cls()
        assert getattr(instance, "__dict__", {}) == {}, (
            f"{cls.__name__} is a shared singleton and must carry no instance dict"
        )


# =======================================================================================
# (2) the rdeps closure: disclosed reduction, never a silent one
# =======================================================================================


def _closure_runner(total: int, direct: int = 2) -> RecordingRunner:
    """A `bazel query` that answers the full closure, the depth-1 closure, and then `bazel test`."""
    return RecordingRunner(
        [
            (
                lambda p: p[1] == "query" and p[-1].endswith(", 1)"),
                ok("".join(f"//pkg{i:04d}:lib\n" for i in range(direct))),
            ),
            (
                lambda p: p[1] == "query",
                ok("".join(f"//pkg{i:04d}:lib\n" for i in range(total))),
            ),
            (lambda p: p[1] == "test", ok("")),
        ]
    )


async def test_a_truncated_closure_forces_closure_sampled_and_drafts_the_pr(tmp_path) -> None:
    """§3.4: `rdeps_truncated` ⇒ `CLOSURE_SAMPLED` ⇒ the PR is a DRAFT, with the numbers named.

    Why it matters: the alternative reading of §3.4's success criterion reports 40 000 untested
    targets as green. The two readings are one criterion only because the reduction is disclosed —
    so the flag, the equivalence and the draft must travel together, from the query all the way
    to `gh pr create --draft`. This test walks that whole chain with no `bazel` and no `gh`.
    """
    runner = _closure_runner(total=300, direct=2)
    worker = RdepverifyWorker(runner=runner)
    result = await worker.run(
        make_ctx(tmp_path),
        RdepverifyInput(
            dest="java/com/acme/widget",
            integration_ref=SNAPSHOT,
            log_dir=str(tmp_path / "logs"),
            rdeps_limit=10,
            rdeps_sample_n=4,
        ),
    )

    assert result.status == "ok"
    out = result.output
    assert out is not None and out.rdeps_truncated is True
    assert out.rdeps_target_count == 300
    assert out.rdeps_tested == 6, "every direct rdep plus the sampled remainder"
    assert out.report.equivalence is Equivalence.CLOSURE_SAMPLED
    assert out.rdeps_sample_seed == sample_seed_for(out.rdeps_query)

    gh = gh_runner()
    pr = await PrwriterWorker(runner=gh).run(
        make_ctx(tmp_path),
        a_pr_payload(
            report=out.report,
            rdeps_sample_seed=out.rdeps_sample_seed,
            ready=True,  # asking for ready must NOT beat a disclosed reduction
            log_dir=str(tmp_path / "logs"),
        ),
    )
    assert pr.status == "ok"
    assert pr.output is not None and pr.output.draft is True
    assert pr.output.pr is not None and pr.output.pr.state is PrState.DRAFTED
    assert pr.output.pr.equivalence is Equivalence.CLOSURE_SAMPLED
    create = gh.argv_for("create")
    assert create is not None and "--draft" in create
    assert gh.argv_for("ready") is None, "a sampled verification is cleared by a human, not by us"
    body = read(pr.output.body_path)
    assert "300" in body and out.rdeps_sample_seed in body, "the banner names count and seed"


async def test_an_under_bound_closure_is_tested_whole(tmp_path) -> None:
    """Under `rdeps_limit` the closure is tested in full and `rdeps_truncated` is false (§3.4).

    Why it matters: sampling that engaged when it was not needed would put a `CLOSURE_SAMPLED`
    banner — and a mandatory human clearance — on every ordinary PR in the fleet, which is how a
    disclosure that means something becomes noise everybody clicks through.
    """
    runner = _closure_runner(total=12)
    result = await RdepverifyWorker(runner=runner).run(
        make_ctx(tmp_path),
        RdepverifyInput(
            dest="java/com/acme/widget",
            integration_ref=SNAPSHOT,
            log_dir=str(tmp_path / "logs"),
            rdeps_limit=2000,
        ),
    )
    out = result.output
    assert out is not None
    assert out.rdeps_truncated is False
    assert out.rdeps_target_count == out.rdeps_tested == 12
    assert out.report.equivalence is Equivalence.FULL
    assert out.report.verdict == "PASS"
    queries = [c for c in runner.calls if c[1] == "query"]
    assert len(queries) == 1, "the depth-1 query is bought only when the closure needs sampling"
    pattern_file = Path(out.target_pattern_file)
    assert read(pattern_file).count("\n") == 12
    assert f"--target_pattern_file={pattern_file}" in out.test_command


async def test_a_failed_rdeps_query_is_never_read_as_a_clean_closure(tmp_path) -> None:
    """Rule 11: an empty target list from a failed query and a genuinely empty closure look the
    same, and one of them is a green PR over an untested blast radius."""
    runner = RecordingRunner(
        [
            (
                lambda p: p[1] == "query",
                lambda parts: ProcResult(
                    argv=parts, exit_code=1, stdout_tail="", stderr_tail="ERROR: bad query",
                    duration_ms=1, timed_out=False,
                ),
            )
        ]
    )
    result = await RdepverifyWorker(runner=runner).run(
        make_ctx(tmp_path),
        RdepverifyInput(
            dest="java/com/acme/widget", integration_ref=SNAPSHOT, log_dir=str(tmp_path / "logs")
        ),
    )
    assert result.status == "failed"
    assert result.error is not None and result.error.exit_code == 1
    assert result.output is not None and result.output.report.verdict == "FAIL"


async def test_rdepverify_refuses_re_entry_when_phase_3_did_not_succeed(tmp_path) -> None:
    """§3.4's precondition is "Phase 3 SUCCEEDED", and it is READ from the phase row.

    Why it matters: a resumed run that inferred it from the payload it also carries would test a
    blast radius around a package that never built, and report the result as a Phase 4 verdict.
    """
    worker = RdepverifyWorker()
    payload = RdepverifyInput(dest="java/com/acme/widget", integration_ref=SNAPSHOT)
    assert await worker.preconditions_hold(
        make_ctx(tmp_path, db=FakeDb(RepoStatus.SUCCEEDED)), payload
    )
    assert not await worker.preconditions_hold(
        make_ctx(tmp_path, db=FakeDb(RepoStatus.REQUIRES_HUMAN_INTERVENTION)), payload
    )
    assert not await worker.preconditions_hold(
        make_ctx(tmp_path),
        RdepverifyInput(dest="java/com/acme/widget", integration_ref="integration"),
    ), "a branch name is not an immutable snapshot; building against it is the §3.3 race"


# =======================================================================================
# (3) buildgen: deterministic emission, and a model pin code refuses
# =======================================================================================


def _unit(dest: str = "java/com/acme/widget") -> BuildUnit:
    return BuildUnit(
        unit_id=REPO,
        ecosystem=Ecosystem.MAVEN,
        dest=dest,
        srcs=["src/main/java/Widget.java"],
        published=Coordinate(ecosystem=Ecosystem.MAVEN, group="com.acme", name="widget"),
    )


def _dep(version_spec: str | None = None) -> WorkspaceDep:
    return WorkspaceDep(
        ruleset="rules_jvm_external",
        extension="maven.install",
        coordinate=Coordinate(
            ecosystem=Ecosystem.MAVEN, group="com.acme", name="commons", version_spec=version_spec
        ),
        repo_name="maven",
    )


async def test_buildgen_renders_both_files_from_code_and_resolves_versions_by_mvs(
    tmp_path,
) -> None:
    """§3.3 steps 2–3 are CODE (Rule 5), and MVS selects the minimum version satisfying ALL specs.

    Why it matters: the rule this replaces — "the highest version satisfying the most specs" — is
    a plurality vote wearing MVS's name, and it ships a build that violates the minority's
    declared upper bound silently.
    """
    ctx = make_ctx(tmp_path)
    result = await BuildgenWorker().run(
        ctx,
        BuildgenInput(
            unit=_unit(),
            targets=[
                BuildTarget(
                    package="java/com/acme/widget",
                    name="widget",
                    rule="java_library",
                    srcs=["Widget.java"],
                )
            ],
            workspace_deps=[_dep()],
            requirements=[
                ExternalRequirement(
                    coord_key="maven:com.acme:commons", repo_id="acme-a", version_spec=">=1.2"
                ),
                ExternalRequirement(
                    coord_key="maven:com.acme:commons", repo_id="acme-b", version_spec=">=1.5"
                ),
            ],
            ruleset_versions={"rules_jvm_external": "6.0"},
        ),
    )
    assert result.status == "ok"
    assert result.completed_units == ["build_bazel", "module_bazel"]
    out = result.output
    assert out is not None
    assert out.selected_versions == {"maven:com.acme:commons": "1.5"}, "max of the lower bounds"
    build_text = read(out.build_bazel_path)
    assert "java_library(" in build_text and 'name = "widget"' in build_text
    module_text = read(out.module_bazel_path)
    assert 'bazel_dep(name = "rules_jvm_external", version = "6.0")' in module_text
    assert '"1.5"' in module_text


async def test_a_model_pin_that_hides_a_violated_spec_is_rejected_before_it_is_written(
    tmp_path,
) -> None:
    """`validate_override` re-derives the pin against EVERY contributing spec, in code (§7.7).

    Why it matters: on an empty intersection every pin violates something, so "violates a spec"
    cannot be the rejection rule — *disclosure* is. A proposal that breaks `31` while declaring it
    broke nothing is written into `MODULE.bazel` verbatim by any implementation that trusts the
    model, and reaches a reviewer looking exactly like a pin that broke nothing.
    """
    model = FakeModelClient(
        {
            "conflict_resolution": VersionConflictResolution(
                coord_key="maven:com.acme:commons",
                proposed_version="33",
                mechanism="single_version_override",
                violated_specs=(),  # the lie: pinning 33 breaks the repo that declared 31
                rationale="33 is what everyone else uses",
            )
        }
    )
    ctx = make_ctx(
        tmp_path, attempt=2, context_policy=ContextPolicy.EVIDENCE_ONLY, model=model
    )
    payload = BuildgenInput(
        unit=_unit(),
        targets=[BuildTarget(package="java/com/acme/widget", name="w", rule="java_library")],
        workspace_deps=[_dep()],
        requirements=[
            ExternalRequirement(
                coord_key="maven:com.acme:commons", repo_id="acme-a", version_spec="31"
            ),
            ExternalRequirement(
                coord_key="maven:com.acme:commons", repo_id="acme-b", version_spec="33"
            ),
        ],
        ruleset_versions={"rules_jvm_external": "6.0"},
    )
    result = await BuildgenWorker().run(ctx, payload)

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.failure_class is FailureClass.DEP_CONFLICT
    out = result.output
    assert out is not None and len(out.rejected_overrides) == 1
    rejected = out.rejected_overrides[0]
    assert rejected.proposed_version == "33"
    assert rejected.undisclosed == ["31"], "the spec the model did not admit breaking"
    assert not (tmp_path / "MODULE.bazel").exists(), "a refused pin never reaches the file"
    assert model.roles == ["conflict_resolution"]


async def test_a_disclosed_override_is_accepted_and_rendered(tmp_path) -> None:
    """The model MAY choose which promise to break — it may not misreport which it kept.

    Why it matters: rejecting every pin on a genuinely unsatisfiable coordinate would make the
    `conflict_resolution` role useless and leave the run stuck; the rule is disclosure, and that
    distinction is the whole design of `validate_override`.
    """
    model = FakeModelClient(
        {
            "conflict_resolution": VersionConflictResolution(
                coord_key="maven:com.acme:commons",
                proposed_version="33",
                mechanism="single_version_override",
                violated_specs=("31",),
                rationale="acme-a is scheduled to absorb the break this quarter",
            )
        }
    )
    ctx = make_ctx(
        tmp_path, attempt=2, context_policy=ContextPolicy.EVIDENCE_ONLY, model=model
    )
    result = await BuildgenWorker().run(
        ctx,
        BuildgenInput(
            unit=_unit(),
            targets=[BuildTarget(package="java/com/acme/widget", name="w", rule="java_library")],
            workspace_deps=[_dep()],
            requirements=[
                ExternalRequirement(
                    coord_key="maven:com.acme:commons", repo_id="acme-a", version_spec="31"
                ),
                ExternalRequirement(
                    coord_key="maven:com.acme:commons", repo_id="acme-b", version_spec="33"
                ),
            ],
            ruleset_versions={"rules_jvm_external": "6.0"},
        ),
    )
    assert result.status == "ok"
    out = result.output
    assert out is not None and out.accepted_overrides == {"maven:com.acme:commons": "33"}
    module_text = read(out.module_bazel_path)
    assert 'single_version_override(module_name = "maven:com.acme:commons", version = "33")' in (
        module_text
    )


async def test_a_conflict_at_rung_1_stays_retryable_so_the_ladder_can_reach_the_model(
    tmp_path,
) -> None:
    """`DEP_CONFLICT` is non-retryable by DEFAULT, and that default at rung 1 ends the ladder.

    Why it matters: §3.3 nominates `conflict_resolution` as the legitimate LLM slot for exactly
    this failure. A worker that took the default would terminate the repo at the deterministic
    rung and the nominated rung would never run — the flag is set from mechanical evidence (which
    rung is executing), never from the message.
    """
    payload = BuildgenInput(
        unit=_unit(),
        targets=[BuildTarget(package="java/com/acme/widget", name="w", rule="java_library")],
        workspace_deps=[_dep()],
        requirements=[
            ExternalRequirement(
                coord_key="maven:com.acme:commons", repo_id="a", version_spec="31"
            ),
            ExternalRequirement(
                coord_key="maven:com.acme:commons", repo_id="b", version_spec="33"
            ),
        ],
        ruleset_versions={"rules_jvm_external": "6.0"},
    )
    model = FakeModelClient({})
    rung1 = await BuildgenWorker().run(make_ctx(tmp_path, attempt=1, model=model), payload)
    assert rung1.error is not None and rung1.error.retryable is True

    exhausted = FakeModelClient(
        {
            "conflict_resolution": VersionConflictResolution(
                coord_key="maven:com.acme:commons",
                proposed_version="33",
                mechanism="single_version_override",
                violated_specs=(),
                rationale="still undisclosed",
            )
        }
    )
    rung2 = await BuildgenWorker().run(
        make_ctx(
            tmp_path,
            attempt=2,
            context_policy=ContextPolicy.EVIDENCE_ONLY,
            model=exhausted,
        ),
        payload,
    )
    assert rung2.error is not None and rung2.error.retryable is False


async def test_build_authoring_is_reached_only_after_the_deterministic_path_fails(
    tmp_path,
) -> None:
    """§3.3's escape hatch: a target no generator template covers. Code still writes the file.

    Why it matters: an implementation that asked the model first would spend HEAVY tokens on
    every one of 250 repos, and would make the emitted text non-deterministic — §11.6 requires
    generated BUILD files to be byte-identical across processes.
    """
    model = FakeModelClient(
        {
            "build_authoring": BuildFileProposal(
                package_path="java/com/acme/widget",
                targets=(
                    {"name": "widget", "rule": "java_library", "srcs": ("Widget.java",)},
                ),
                rationale="no template covers a mixed resource/source layout",
            )
        }
    )
    empty = BuildgenInput(unit=_unit(), write_module_bazel=False)
    rung1 = await BuildgenWorker().run(make_ctx(tmp_path, model=model), empty)
    assert rung1.status == "failed"
    assert rung1.error is not None and rung1.error.failure_class is FailureClass.RULE_MISS
    assert model.roles == [], "rung 1 renders no prompt at all"

    rung2 = await BuildgenWorker().run(
        make_ctx(
            tmp_path, attempt=2, context_policy=ContextPolicy.EVIDENCE_ONLY, model=model
        ),
        empty,
    )
    assert rung2.status == "ok"
    assert rung2.output is not None and rung2.output.authored_by_model is True
    assert "java_library(" in read(rung2.output.build_bazel_path)
    assert model.roles == ["build_authoring"]


async def test_buildgen_refuses_the_reserved_scc_namespace(tmp_path) -> None:
    """`_scc/` belongs to the §3.1 6e coarsened targets, and the reservation is enforced.

    Why it matters: a repo generating there occupies the coarsening namespace, and the §3.1 step-8
    audit that was supposed to catch it has already run — so this is the last place to refuse.
    """
    worker = BuildgenWorker()
    payload = BuildgenInput(
        unit=_unit(dest="java/_scc/scc_0f3a1b2c3d4e5f60"),
        targets=[BuildTarget(package="java/_scc/x", name="x", rule="java_library")],
    )
    assert not await worker.preconditions_hold(make_ctx(tmp_path), payload)


# =======================================================================================
# (4) buildverify: the immutable ref, and a failure that survives being persisted
# =======================================================================================


async def test_buildverify_records_the_integration_ref_it_built_against(tmp_path) -> None:
    """§3.3 step 1: every build reads an IMMUTABLE snapshot, and the attempt names it.

    Why it matters: without the recorded ref a `BUILD_ERROR` is a property of scheduling luck —
    merges landing mid-build change the tree under a 20-minute build — and no later phase can
    tell whether the failure describes the tree anyone reviewed.
    """
    (tmp_path / "java/com/acme/widget").mkdir(parents=True)
    (tmp_path / "java/com/acme/widget/BUILD.bazel").write_text("# generated\n", encoding="utf-8")
    runner = RecordingRunner([(lambda p: True, ok(""))])
    payload = BuildverifyInput(
        dest="java/com/acme/widget", integration_ref=SNAPSHOT, log_dir=str(tmp_path / "logs")
    )
    worker = BuildverifyWorker(runner=runner)
    ctx = make_ctx(tmp_path)

    assert await worker.preconditions_hold(ctx, payload)
    result = await worker.run(ctx, payload)

    assert result.status == "ok"
    assert result.completed_units == ["build", "test"]
    out = result.output
    assert out is not None and out.integration_ref == SNAPSHOT
    assert SNAPSHOT in result.evidence
    assert runner.calls[0] == (
        "bazel", "build", "//java/com/acme/widget/...", "--keep_going",
        "--build_event_json_file=bazel-build-events.json",
    )
    assert runner.calls[1][1] == "test"

    branchy = BuildverifyInput(dest="java/com/acme/widget", integration_ref="integration")
    assert not await worker.preconditions_hold(ctx, branchy), (
        "a moving branch is exactly what the snapshot ref exists to replace"
    )


async def test_a_build_failure_persists_a_truncated_stderr_with_its_exit_code_and_log_path(
    tmp_path,
) -> None:
    """A 400 000-character stderr is TRUNCATED, never rejected (§11.3, `models.base`).

    Why it matters — and this is the defect the field type exists for: a `max_length` bound made
    validation reject the very failures worth recording. The attempt was never persisted, the
    `attempts` counter never incremented, and the repair loop re-ran the identical failing build
    forever. So: the tail is bounded, the exit code is real, and the FULL stream stays on disk at
    `artifact_ref` — which is what the ADR-0021 repair rung reads verbatim.
    """
    (tmp_path / "java/com/acme/widget").mkdir(parents=True)
    (tmp_path / "java/com/acme/widget/BUILD.bazel").write_text("# generated\n", encoding="utf-8")
    huge = "ERROR: java compilation failed\n" + ("x" * 400_000)
    full_log = tmp_path / "logs" / "build.stderr.log"
    full_log.parent.mkdir(parents=True, exist_ok=True)
    full_log.write_text(huge, encoding="utf-8")

    runner = RecordingRunner(
        [
            (
                lambda p: True,
                lambda parts: ProcResult(
                    argv=parts,
                    exit_code=1,
                    stdout_tail="",
                    stderr_tail=huge,
                    duration_ms=90_000,
                    timed_out=False,
                    stderr_bytes=len(huge),
                    stderr_path=full_log,
                ),
            )
        ]
    )
    result = await BuildverifyWorker(runner=runner).run(
        make_ctx(tmp_path),
        BuildverifyInput(
            dest="java/com/acme/widget", integration_ref=SNAPSHOT, log_dir=str(tmp_path / "logs")
        ),
    )

    assert result.status == "failed"
    error = result.error
    assert error is not None
    assert error.exit_code == 1, "the exit code IS the verdict (§3.3)"
    assert error.failure_class is FailureClass.BUILD_ERROR and error.retryable is True
    assert 0 < len(error.stderr_tail) < len(huge), "truncated, not rejected and not whole"
    assert error.stderr_tail.endswith("bytes]"), "the truncation announces itself"
    assert error.artifact_ref == str(full_log)
    assert read(error.artifact_ref) == huge, (
        "the repair prompt reads the FULL stream, never the tail"
    )
    assert result.output is not None and result.output.integration_ref == SNAPSHOT


async def test_a_test_failure_and_an_oom_are_not_the_same_failure(tmp_path) -> None:
    """`retry.py` branches on `retryable`, never on message text: exit 1 is a repair prompt and
    exit 137 from the OOM killer is a re-queue that must not be classed as the repo's fault."""
    (tmp_path / "ts/acme").mkdir(parents=True)
    (tmp_path / "ts/acme/BUILD.bazel").write_text("# generated\n", encoding="utf-8")

    def runner_for(exit_code: int) -> RecordingRunner:
        return RecordingRunner(
            [
                (lambda p: p[1] == "build", ok("")),
                (
                    lambda p: p[1] == "test",
                    lambda parts: ProcResult(
                        argv=parts, exit_code=exit_code, stdout_tail="", stderr_tail="boom",
                        duration_ms=10, timed_out=False,
                    ),
                ),
            ]
        )

    payload = BuildverifyInput(
        dest="ts/acme", integration_ref=SNAPSHOT, log_dir=str(tmp_path / "logs")
    )
    failed = await BuildverifyWorker(runner=runner_for(1)).run(make_ctx(tmp_path), payload)
    assert failed.error is not None
    assert failed.error.failure_class is FailureClass.TEST_FAILURE
    assert failed.completed_units == ["build"], "the green build is landed work, and is recorded"

    oomed = await BuildverifyWorker(runner=runner_for(137)).run(make_ctx(tmp_path), payload)
    assert oomed.error is not None
    assert oomed.error.failure_class is FailureClass.TRANSIENT_INFRA


async def test_the_sandboxed_command_is_network_none_and_named_after_the_attempt(
    tmp_path,
) -> None:
    """ADR-0010: `--network=none` is what makes a green build evidence that the deps are complete,
    and the container name is what a crashed run's reaper has to work with."""
    (tmp_path / "py/acme").mkdir(parents=True)
    (tmp_path / "py/acme/BUILD.bazel").write_text("# generated\n", encoding="utf-8")
    runner = RecordingRunner([(lambda p: True, ok(""))])
    await BuildverifyWorker(runner=runner).run(
        make_ctx(tmp_path, attempt=2),
        BuildverifyInput(
            dest="py/acme",
            integration_ref=SNAPSHOT,
            image="fleet/build:latest",
            run_tests=False,
            log_dir=str(tmp_path / "logs"),
        ),
    )
    # `calls[-1]`, not `calls[0]`: a sandboxed run probes the image for a C compiler first (see
    # `_c_toolchain_gate` and the two tests below), and this test is about the BUILD's argv.
    argv = runner.calls[-1]
    assert argv[0:2] == ("docker", "run")
    assert "--network=none" in argv
    assert any(a.startswith("--name=fleet-") and a.endswith("-2") for a in argv)
    assert argv[-5:-1] == ("bazel", "build", "//py/acme/...", "--keep_going")
    assert argv[-1].startswith("--build_event_json_file=")


async def test_a_sandboxed_build_refuses_before_bazel_when_the_image_has_no_c_compiler(
    tmp_path,
) -> None:
    """The dependency the harness never wrote down: **no C compiler, no Go targets at all.**

    `rules_cc`'s `cc_configure_extension` generates `local_config_cc` by resolving `gcc` (or the
    image's `CC`) on `PATH`, and `@@rules_go+//:stdlib` depends on `local_config_cc//:cc-compiler-
    k8` — so a compiler-less image fails for a PURE-Go package with no `import "C"` anywhere.
    `test_build_e2e.test_the_pure_go_tree_fails_to_analyse_when_no_c_compiler_is_discoverable`
    proves that against real Bazel over the harness's own generated tree; this test is about what
    the worker does with it.

    Without the gate the symptom is a bare exit 1 that `classify_build_failure` calls a RETRYABLE
    `BUILD_ERROR` — three ADR-0014 rungs, two of them prompting a model to repair a `BUILD.bazel`
    that is fine, then `REQUIRES_HUMAN_INTERVENTION`. So the two things asserted are the two that
    cost: `bazel` is never invoked, and the error is not retryable.

    **Unit level, and that is a real limit, not a shortcut.** The image here is a fake string and
    the runner is a fake: this test proves the WORKER's logic and the operator-facing message,
    and proves nothing about any real image. The image side is a separate, separately-gated
    question — `docker/fleet-build.Dockerfile` builds the image
    `settings.verify.container_image` names, and
    `test_sandbox.test_the_fleet_build_image_satisfies_the_c_toolchain_probe` runs the real probe
    inside it — but that test needs a daemon and a locally-built tag, and this one must hold
    where neither exists.
    """
    dest = a_package(tmp_path, "go/acme_digest_go")
    runner = RecordingRunner(
        [
            (
                lambda p: p[-3:-1] == ("sh", "-c") and "command -v gcc" in p[-1],
                lambda parts: ProcResult(
                    argv=parts, exit_code=1, stdout_tail="", stderr_tail="",
                    duration_ms=40, timed_out=False,
                ),
            ),
            (lambda p: True, ok("")),
        ]
    )
    result = await BuildverifyWorker(runner=runner).run(
        make_ctx(tmp_path),
        BuildverifyInput(
            dest=dest,
            integration_ref=SNAPSHOT,
            image="fleet/build:latest",
            log_dir=str(tmp_path / "logs"),
        ),
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.failure_class is FailureClass.BUILD_ERROR
    assert result.error.retryable is False, (
        "re-running an identical rung cannot install a compiler, exactly as it cannot install "
        "`bazel` (exit 127) — a retryable verdict here spends the whole ladder learning that"
    )
    assert runner.argv_for("build") is None, "bazel ran anyway; the gate bought nothing"
    assert result.remaining_units == ["build", "test"], "neither unit was attempted"

    # The message is the deliverable: an operator reading it must not have to rediscover
    # `//:stdlib` from a stderr about a repository fetch.
    detail = str(result.error.stderr_tail)
    assert "fleet/build:latest" in detail, detail
    assert "local_config_cc" in detail, detail
    assert "@@rules_go+//:stdlib" in detail, detail
    assert "cc-compiler-k8" in detail, detail
    assert "not a cgo-only requirement" in detail.lower() or "NOT a cgo-only" in detail, detail
    assert "Found 0 targets" in detail, detail


async def test_a_probe_that_never_started_is_not_reported_as_a_missing_compiler(
    tmp_path,
) -> None:
    """Exit 125 is `docker run` refusing to START a container — the image is absent locally and
    unpullable, the daemon is unreachable, or a flag/resource value is invalid. Nothing inside
    the image ran, so "no C compiler in the sandbox image" is a claim the probe cannot make.

    It matters because the two verdicts send an operator to different files:
    `settings.verify.container_image` now names a LOCAL tag (this fleet has no registry), so an
    unbuilt `docker/fleet-build.Dockerfile` is the most likely 125 there is, and reporting it as
    a compiler-less image sends them to audit a Dockerfile that is correct.

    The classification is unchanged and deliberately so: still `BUILD_ERROR`, still
    non-retryable, because re-running an identical rung cannot build an image.
    """
    dest = a_package(tmp_path, "go/acme_digest_go")
    runner = RecordingRunner(
        [
            (
                lambda p: p[-3:-1] == ("sh", "-c") and "command -v gcc" in p[-1],
                lambda parts: ProcResult(
                    argv=parts,
                    exit_code=125,
                    stdout_tail="",
                    stderr_tail=(
                        "Unable to find image 'fleet-build:9.2.0-bookworm' locally\n"
                        "docker: Error response from daemon: pull access denied"
                    ),
                    duration_ms=30,
                    timed_out=False,
                ),
            ),
            (lambda p: True, ok("")),
        ]
    )

    result = await BuildverifyWorker(runner=runner).run(
        make_ctx(tmp_path),
        BuildverifyInput(
            dest=dest,
            integration_ref=SNAPSHOT,
            image="fleet-build:9.2.0-bookworm",
            log_dir=str(tmp_path / "logs"),
        ),
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.failure_class is FailureClass.BUILD_ERROR
    assert result.error.retryable is False
    assert result.error.exit_code == 125
    assert runner.argv_for("build") is None, "bazel ran against an image that would not start"

    detail = str(result.error.stderr_tail)
    assert "could not be RUN" in detail, detail
    assert "docker/fleet-build.Dockerfile" in detail, detail
    assert "no C compiler" not in detail, (
        "125 means the container never started, so the image's compiler was never consulted; "
        "saying otherwise is the misattribution this test exists for"
    )


async def test_an_unsandboxed_build_never_pays_for_the_c_compiler_probe(tmp_path) -> None:
    """`payload.image is None` is the host path, and the gate deliberately does not fire on it.

    Two reasons, both mechanical rather than stylistic: the very next command is `bazel` off the
    same `PATH` the compiler would come from, so the probe would spend a `docker run` to learn
    what that invocation learns for free; and there may be no daemon at all — the offline tests
    and `fleet build --no-sandbox` both run here, and a gate that shelled out to `docker` would
    fail them for the absence of something they never asked for.
    """
    dest = a_package(tmp_path, "go/acme_digest_go")
    runner = RecordingRunner([(lambda p: True, ok(""))])
    result = await BuildverifyWorker(runner=runner).run(
        make_ctx(tmp_path),
        BuildverifyInput(dest=dest, integration_ref=SNAPSHOT, log_dir=str(tmp_path / "logs")),
    )

    assert result.status == "ok"
    assert all(call[0] != "docker" for call in runner.calls), runner.calls
    assert [call[1] for call in runner.calls] == ["build", "test"], runner.calls


async def test_the_c_compiler_probe_asks_the_one_question_bazel_asks(tmp_path) -> None:
    """The probe mirrors `_find_generic(ctx, "gcc", "CC", …)` — Bazel's ONLY compiler lookup.

    In `bazelbuild/rules_cc`'s `cc/private/toolchain/unix_cc_configure.bzl`, `find_cc` resolves
    `overridden_tools` → `$CC` (stripped; non-empty REPLACES the default) → the literal `"gcc"` →
    `repository_ctx.which`, with an absolute value short-circuiting `which`. So `cc` is never
    searched and `clang` is never a candidate — it appears only in `_is_clang`, classifying a
    binary already found. An earlier three-name probe (`command -v cc || command -v gcc ||
    command -v clang`) got both directions wrong; the two tests below pin those directions.
    `command -v` is POSIX `sh`, so no `which(1)` need exist in the image.

    The probe container is deliberately NOT named after the attempt — `on_cancel` force-removes
    that name, and a probe sharing it would race the build it precedes.
    """
    dest = a_package(tmp_path, "go/acme_digest_go")
    runner = RecordingRunner([(lambda p: True, ok("/usr/bin/gcc\n"))])
    await BuildverifyWorker(runner=runner).run(
        make_ctx(tmp_path, attempt=3),
        BuildverifyInput(
            dest=dest,
            integration_ref=SNAPSHOT,
            image="fleet/build:latest",
            run_tests=False,
            log_dir=str(tmp_path / "logs"),
        ),
    )

    probe = runner.calls[0]
    assert probe[0:2] == ("docker", "run")
    assert probe[-3:] == ("sh", "-c", C_TOOLCHAIN_PROBE)
    assert probe[-4] == "fleet/build:latest"
    assert C_TOOLCHAIN_PROBE == (
        "cc=${CC:-}; "
        'cc=${cc#"${cc%%[![:space:]]*}"}; cc=${cc%"${cc##*[![:space:]]}"}; '
        'case "$cc" in '
        '"") command -v gcc ;; '
        '/*) [ -x "$cc" ] ;; '
        '*) command -v "$cc" ;; '
        "esac"
    )
    assert "command -v cc " not in C_TOOLCHAIN_PROBE, "`cc` is not a name Bazel ever looks for"
    assert "clang" not in C_TOOLCHAIN_PROBE, "`clang` is not a name Bazel ever looks for"
    name = next(a for a in probe if a.startswith("--name="))
    assert name.endswith("-3-cc-probe"), name
    build_name = next(a for a in runner.calls[-1] if a.startswith("--name="))
    assert name != build_name, "the probe would race the build container it precedes"
    assert "--network=none" in probe, "the probe is a local filesystem question; it needs no net"


# =======================================================================================
# (4b) buildverify: the shared Bazel caches — mounted AND named
# =======================================================================================


def a_cache(tmp_path: Path, name: str) -> Path:
    path = tmp_path / "cache" / "bazel" / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def volume_targets(argv: tuple[str, ...]) -> dict[str, str]:
    """`{host source: container target}` read back out of the emitted `--volume=` flags.

    The cache assertions below compare the `--disk_cache=`/`--repository_cache=` VALUES against
    this dict rather than against a literal `/cache/...`, so a rename of the mount target that
    forgot the flag (or vice versa) fails the test instead of silently pointing bazel at a
    directory the container does not have.
    """
    pairs: dict[str, str] = {}
    for flag in argv:
        if not flag.startswith("--volume="):
            continue
        source, target = flag.split("=", 1)[1].split(":")[:2]
        pairs[source] = target
    return pairs


def cache_flags(argv: tuple[str, ...]) -> list[str]:
    return [a for a in argv if a.startswith(("--disk_cache=", "--repository_cache="))]


async def test_the_sandboxed_bazel_is_told_about_the_caches_mounted_into_it(tmp_path) -> None:
    """The mounts were inert: bind-mounted read-write and never named on the command line.

    Why it costs the whole phase rather than a cache hit: `verify.network` is `none`, so a cold
    container with no `--repository_cache` has nothing to resolve `go_sdk.download` or any BCR
    `bazel_dep` FROM and no network to fall back to — it cannot fetch a single module, and the
    failure arrives as an ordinary retryable `BUILD_ERROR` that spends all three ADR-0014 rungs
    on a tree that is fine. The flag values are compared against the mount TARGETS read back out
    of the argv, so the two cannot drift apart in a later rename.
    """
    dest = a_package(tmp_path, "go/acme_digest_go")
    disk, repo = a_cache(tmp_path, "disk"), a_cache(tmp_path, "repo")
    runner = RecordingRunner([(lambda p: True, ok(""))])
    await BuildverifyWorker(runner=runner).run(
        make_ctx(tmp_path),
        BuildverifyInput(
            dest=dest,
            integration_ref=SNAPSHOT,
            image="fleet/build:latest",
            run_tests=False,
            log_dir=str(tmp_path / "logs"),
            cache_mounts=[
                CacheMount(role="disk", path=str(disk)),
                CacheMount(role="repository", path=str(repo)),
            ],
        ),
    )

    argv = runner.calls[-1]  # calls[0] is the C-toolchain probe
    mounts = volume_targets(argv)
    assert mounts[str(disk)] == "/cache/disk" and mounts[str(repo)] == "/cache/repo"
    assert cache_flags(argv) == [
        f"--disk_cache={mounts[str(disk)]}",
        f"--repository_cache={mounts[str(repo)]}",
    ]
    assert f"--disk_cache={disk}" not in argv, (
        "the HOST path inside a container names a directory that is not there; bazel would "
        "create an unshared one under the container filesystem and throw it away on --rm"
    )
    image_at = argv.index("fleet/build:latest")
    assert all(argv.index(flag) > image_at for flag in cache_flags(argv)), (
        "these are bazel's flags, not docker's — before the image they would configure the "
        "wrong program"
    )


async def test_an_unsandboxed_bazel_is_told_the_host_cache_paths(tmp_path) -> None:
    """The same worker runs with `image=None` — `fleet build --no-sandbox`, every offline test —
    and there is no container there, so `/cache/<name>` is the path that does not exist.

    Backwards, this is silent: bazel would happily create `/cache/disk` (or fail on a read-only
    root) and the fleet's shared caches would stay empty while every run paid a cold fetch.
    """
    dest = a_package(tmp_path, "go/acme_digest_go")
    disk, repo = a_cache(tmp_path, "disk"), a_cache(tmp_path, "repo")
    runner = RecordingRunner([(lambda p: True, ok(""))])
    await BuildverifyWorker(runner=runner).run(
        make_ctx(tmp_path),
        BuildverifyInput(
            dest=dest,
            integration_ref=SNAPSHOT,
            image=None,
            run_tests=False,
            log_dir=str(tmp_path / "logs"),
            cache_mounts=[
                CacheMount(role="disk", path=str(disk)),
                CacheMount(role="repository", path=str(repo)),
            ],
        ),
    )

    argv = runner.calls[-1]
    assert argv[0:2] == ("bazel", "build"), "no container, so no docker argv to unwrap"
    assert cache_flags(argv) == [f"--disk_cache={disk}", f"--repository_cache={repo}"]
    assert not any(a.startswith("--volume=") for a in argv), "nothing to mount without an image"
    assert not any(f"={CACHE_MOUNT_ROOT}/" in a for a in argv), (
        "the container-side path has no meaning on the host"
    )


async def test_a_cache_the_payload_does_not_carry_gets_no_flag(tmp_path) -> None:
    """A flag for an absent mount is worse than no flag: `--repository_cache=/cache/repo` with no
    `/cache/repo` mounted is bazel writing into the container's own filesystem, which `--rm`
    deletes — a cache that appears configured, costs a full fetch every attempt, and never fills.
    """
    dest = a_package(tmp_path, "go/acme_digest_go")
    disk = a_cache(tmp_path, "disk")

    async def argv_for(mounts: list[CacheMount]) -> tuple[str, ...]:
        runner = RecordingRunner([(lambda p: True, ok(""))])
        await BuildverifyWorker(runner=runner).run(
            make_ctx(tmp_path),
            BuildverifyInput(
                dest=dest,
                integration_ref=SNAPSHOT,
                image="fleet/build:latest",
                run_tests=False,
                log_dir=str(tmp_path / "logs"),
                cache_mounts=mounts,
            ),
        )
        return runner.calls[-1]

    only_disk = await argv_for([CacheMount(role="disk", path=str(disk))])
    assert cache_flags(only_disk) == ["--disk_cache=/cache/disk"]
    assert volume_targets(only_disk).get(str(disk)) == "/cache/disk"

    neither = await argv_for([])
    assert cache_flags(neither) == []
    assert all(CACHE_MOUNT_ROOT not in a for a in neither), neither


async def test_a_hand_written_cache_flag_in_extra_args_still_wins(tmp_path) -> None:
    """Emitting the flags must not break the one workaround that existed before they were emitted.

    Hand-writing `--disk_cache=` into `extra_args` was the ONLY way to reach the mounted cache, so
    an operator's config may carry one today. Bazel keeps the LAST occurrence of a
    non-`allowMultiple` option — checked against the vendored 9.2.0 rather than recalled:
    `bazel canonicalize-flags --for_command=build -- --disk_cache=/a --disk_cache=/b` prints
    `--disk_cache=/b`, and `--for_command=test` agrees — so `extra_args` going last means the
    operator's value keeps winning, with a duplicate on the command line rather than an error.
    """
    dest = a_package(tmp_path, "go/acme_digest_go")
    disk = a_cache(tmp_path, "disk")
    runner = RecordingRunner([(lambda p: True, ok(""))])
    await BuildverifyWorker(runner=runner).run(
        make_ctx(tmp_path),
        BuildverifyInput(
            dest=dest,
            integration_ref=SNAPSHOT,
            image="fleet/build:latest",
            run_tests=False,
            log_dir=str(tmp_path / "logs"),
            cache_mounts=[CacheMount(role="disk", path=str(disk))],
            extra_args=["--disk_cache=/cache/disk/operator"],
        ),
    )

    argv = runner.calls[-1]
    assert cache_flags(argv) == ["--disk_cache=/cache/disk", "--disk_cache=/cache/disk/operator"]
    assert argv[-1] == "--disk_cache=/cache/disk/operator", (
        "last wins in bazel's option parser, so the operator's value must be the last one"
    )


def an_image(tmp_path: Path, *binaries: str) -> Path:
    """A stand-in for an image's `PATH`: one directory of executable stubs, nothing else."""
    bin_dir = tmp_path / "image-bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name in binaries:
        stub = bin_dir / name
        stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        stub.chmod(0o755)
    return bin_dir


def run_probe_program(program: str, env: dict[str, str]):
    """Execute the probe's shell program against a controlled environment.

    A sync helper called from a coroutine, the same shape and for the same reason as `read` above:
    `ASYNC221` flags a blocking `subprocess.run` inside an `async def`, and a `/bin/sh` that exits
    in microseconds is not worth a thread. `check=False` because the exit code IS the answer.
    """
    return subprocess.run(  # noqa: S603 — a fixed argv, no `shell=True`; the program under test IS sh
        ["/bin/sh", "-c", program],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


class ProbeAgainstFakeImage:
    """A `CommandRunner` that ANSWERS the C-compiler probe by really running it.

    The probe is a shell program, so a fake replying with a hand-chosen exit code would only test
    the fake — it could not tell a clang-only image from a gcc one, which is the entire question.
    This runner instead lifts the `sh -c <program>` tail off the `docker run` argv the worker
    built and executes that program under `/bin/sh` with `PATH` pointed at a directory of stubs
    and with the "image's" `ENV`. No daemon, no image, no compiler: just the shell semantics the
    probe is made of, evaluated against a filesystem the test controls. Every other command
    (`bazel build`, `bazel test`) is answered green, so a refusal can only have come from the gate.
    """

    def __init__(self, image_bin: Path, env: dict[str, str] | None = None) -> None:
        self.image_bin = image_bin
        self.image_env = dict(env or {})
        self.calls: list[tuple[str, ...]] = []
        self.probes: list[tuple[str, ...]] = []

    async def __call__(self, argv, *, cwd=None, env=None, deadline=None, timeout_s=None):
        parts = tuple(argv)
        self.calls.append(parts)
        if parts[-3:-1] != ("sh", "-c"):
            return ProcResult(
                argv=parts,
                exit_code=0,
                stdout_tail="",
                stderr_tail="",
                duration_ms=5,
                timed_out=False,
            )
        self.probes.append(parts)
        done = run_probe_program(parts[-1], {"PATH": str(self.image_bin), **self.image_env})
        return ProcResult(
            argv=parts,
            exit_code=done.returncode,
            stdout_tail=done.stdout,
            stderr_tail=done.stderr,
            duration_ms=7,
            timed_out=False,
        )

    def argv_for(self, needle: str) -> tuple[str, ...] | None:
        for call in self.calls:
            if needle in call:
                return call
        return None


async def test_a_clang_only_image_is_refused_because_bazel_never_looks_for_clang(
    tmp_path,
) -> None:
    """**Gap A, the false green.** The old probe passed exactly the image Bazel then rejects.

    The image here is the realistic clang-only one: `clang` on `PATH`, and `cc` present as a
    symlink to it — the layout Debian/Alpine leave behind when clang is the only compiler
    installed — with no binary named `gcc` anywhere. `command -v cc || command -v gcc ||
    command -v clang` answered 0 for it and let the build through, whereupon Bazel's `find_cc`
    (which searches `gcc` and `$CC` and NOTHING else) failed the `local_config_cc` fetch with
    `Cannot find gcc or CC` — the bare exit 1 that costs all three ADR-0014 rungs. That is the
    precise failure this gate exists to prevent, so the gate must refuse this image.

    `clang` is not a lookup candidate upstream: it appears only inside `_is_clang(…)`, which
    classifies a compiler the lookup already found. A `cc` symlink is not one either — `cc` is
    never searched at all.
    """
    dest = a_package(tmp_path, "go/acme_digest_go")
    image_bin = an_image(tmp_path, "clang")
    (image_bin / "cc").symlink_to(image_bin / "clang")
    runner = ProbeAgainstFakeImage(image_bin)

    result = await BuildverifyWorker(runner=runner).run(
        make_ctx(tmp_path),
        BuildverifyInput(
            dest=dest,
            integration_ref=SNAPSHOT,
            image="fleet/build:latest",
            log_dir=str(tmp_path / "logs"),
        ),
    )

    assert runner.probes, "the sandboxed run never probed the image at all"
    assert result.status == "failed", (
        "a clang-only image passed the gate and would now spend three rungs discovering that "
        "Bazel does not look for clang"
    )
    assert result.error is not None
    assert result.error.retryable is False
    assert runner.argv_for("build") is None, "bazel ran anyway; the gate bought nothing"
    detail = str(result.error.stderr_tail)
    assert "`cc` and `clang` are never" in detail, detail


@pytest.mark.parametrize(
    ("cc_value", "why"),
    [
        ("{bin}/gcc-13", "an absolute CC short-circuits `which` and is used verbatim"),
        ("gcc-13", "a relative CC is what `repository_ctx.which` is handed instead of `gcc`"),
        ("  gcc-13  ", "Starlark strips CC before using it, so padding is not a refusal"),
    ],
)
async def test_an_image_whose_env_cc_resolves_is_accepted(tmp_path, cc_value, why) -> None:
    """**Gap B, the false refusal.** A working image was being turned away, non-retryably.

    `ENV CC=…` is how an image names a compiler that is not called `gcc` — a versioned
    `gcc-13`, or a toolchain under `/opt`. Upstream `_find_generic` strips `$CC` and, if
    anything remains, uses it INSTEAD of the default name; so every image below builds. The old
    probe asked only for the three fixed names, found none, and returned a `retryable=False`
    refusal of a perfectly good image — the most expensive verdict this worker can reach, since
    `RetryPolicy.decide` answers `TERMINATE` and no later rung revisits it.

    Only an image-level `ENV` counts, which is why the value is injected as the image's
    environment and not the harness's: `buildverify` builds both `ContainerSpec`s without `env`,
    and `docker_run_argv` emits `--env` only from `spec.env`, so a `CC` set beside `fleet verify`
    never reaches the container. The operator message says so for that reason.
    """
    dest = a_package(tmp_path, "go/acme_digest_go")
    image_bin = an_image(tmp_path, "gcc-13")  # deliberately no plain `gcc`
    runner = ProbeAgainstFakeImage(image_bin, env={"CC": cc_value.format(bin=image_bin)})

    result = await BuildverifyWorker(runner=runner).run(
        make_ctx(tmp_path),
        BuildverifyInput(
            dest=dest,
            integration_ref=SNAPSHOT,
            image="fleet/build:latest",
            log_dir=str(tmp_path / "logs"),
        ),
    )

    assert runner.probes, "the sandboxed run never probed the image at all"
    assert result.status == "ok", f"{why}: {result.error}"
    assert runner.argv_for("build") is not None, "the gate refused an image Bazel would accept"
    assert runner.argv_for("test") is not None


async def test_an_image_with_neither_gcc_nor_a_resolvable_cc_is_still_refused(tmp_path) -> None:
    """The other half of Gap B: `CC` is not a magic word, it has to resolve to something.

    `ENV CC=gcc-13` on an image that does not carry `gcc-13` fails `repository_ctx.which` exactly
    as a missing `gcc` does, and an absolute `CC` pointing at nothing fails the very next thing
    `configure_unix_toolchain` does with it (`execute([cc, "-E", …])`). Both must still be
    refusals, or the new probe would have traded a false green for a wider one.
    """
    dest = a_package(tmp_path, "go/acme_digest_go")
    empty = an_image(tmp_path)
    for absent in (f"{empty}/gcc", "gcc-13", "", "   "):
        runner = ProbeAgainstFakeImage(empty, env={"CC": absent})
        result = await BuildverifyWorker(runner=runner).run(
            make_ctx(tmp_path),
            BuildverifyInput(
                dest=dest,
                integration_ref=SNAPSHOT,
                image="fleet/build:latest",
                log_dir=str(tmp_path / "logs"),
            ),
        )
        assert result.status == "failed", f"CC={absent!r} resolved to a compiler that is not there"
        assert result.error is not None and result.error.retryable is False
        assert runner.argv_for("build") is None, f"CC={absent!r}"


# =======================================================================================
# (4c) rdepverify: the SAME caches, on the widest build in the pipeline
# =======================================================================================
# §3.4's bounds table puts the persistent build cache in the Phase 4 section and names
# `bazel/query.py` among its enforcement points — and Phase 4's `bazel test` over the blast
# radius was the one bazel invocation in the harness that emitted neither flag. Phase 3 got the
# mounts and the flags; this step, which tests every reverse dependency of the package Phase 3
# just built, ran cold.


def rdeps_test_argv(runner: RecordingRunner) -> tuple[str, ...]:
    """The `bazel test` over the verified target set — the last thing this worker runs."""
    tests = [call for call in runner.calls if call[1] == "test"]
    assert len(tests) == 1, runner.calls
    return tests[0]


async def test_the_blast_radius_test_is_told_about_the_shared_caches(tmp_path) -> None:
    """Phase 4's `bazel test` names `--disk_cache=`/`--repository_cache=`, with the HOST paths.

    Why the host paths unconditionally, rather than `buildverify`'s `sandboxed=payload.image is
    not None`: `RdepverifyInput` has no `image`, this worker builds no container and emits no
    `--volume=`, so there is no `/cache/<name>` anywhere for a container-side path to name. Bazel
    handed one would create it at the HOST filesystem root — or fail on a read-only root — and
    either way the caches `_cache_mounts` created would stay empty. Containerising Phase 4 is a
    separate change that would have to add the image, the mounts and the `sandboxed=` argument
    together; until then `False` is the only value that is not silently wrong.

    And the cost of the gap was not a missed cache hit. This is the WIDEST build in the pipeline —
    every reverse dependency of the changed package — running immediately after Phase 3 filled a
    disk cache it was then never told about, so it re-executed those actions and re-fetched every
    module from cold, on every attempt, for every repo in the fleet.
    """
    disk, repo = a_cache(tmp_path, "disk"), a_cache(tmp_path, "repo")
    runner = _closure_runner(total=12)
    result = await RdepverifyWorker(runner=runner).run(
        make_ctx(tmp_path),
        RdepverifyInput(
            dest="java/com/acme/widget",
            integration_ref=SNAPSHOT,
            log_dir=str(tmp_path / "logs"),
            rdeps_limit=2000,
            cache_mounts=[
                CacheMount(role="disk", path=str(disk)),
                CacheMount(role="repository", path=str(repo)),
            ],
        ),
    )

    assert result.status == "ok"
    argv = rdeps_test_argv(runner)
    assert cache_flags(argv) == [f"--disk_cache={disk}", f"--repository_cache={repo}"]
    assert not any(f"={CACHE_MOUNT_ROOT}/" in a for a in argv), (
        "there is no container here, so the container-side path names nothing on this host"
    )
    assert not any(a.startswith("--volume=") for a in argv), "this worker containerises nothing"
    # The recorded command is what a reviewer reads back off the `attempts` row, so the flags
    # have to be on THAT copy too — not only on the tuple the runner happened to receive.
    assert result.output is not None
    assert cache_flags(tuple(result.output.test_command)) == cache_flags(argv)


async def test_the_rdeps_cache_flags_are_the_ones_the_cli_configures(tmp_path) -> None:
    """The values come from `verify.disk_cache`/`verify.repository_cache` via `cli._cache_mounts`,
    not from a second copy of the paths living in this worker.

    Pinned against `_cache_mounts` itself rather than against literal strings so that a settings
    rename breaks this test instead of quietly leaving Phase 4 pointed at a directory Phase 3 no
    longer uses — two caches that disagree cost exactly what having none costs. The configured
    names here are deliberately role-less (`cache/one`, `cache/two`): an implementation that
    recovered the role from the directory NAME cannot pass.
    """
    from fleet.cli import _cache_mounts
    from fleet.settings import FleetSettings
    from tests.test_cli import FLEET_YAML, write_config

    write_config(
        tmp_path,
        fleet=FLEET_YAML + "verify:\n  disk_cache: cache/one\n  repository_cache: cache/two\n",
    )
    mounts = _cache_mounts(FleetSettings.load(tmp_path / "config"))

    runner = _closure_runner(total=12)
    await RdepverifyWorker(runner=runner).run(
        make_ctx(tmp_path),
        RdepverifyInput(
            dest="java/com/acme/widget",
            integration_ref=SNAPSHOT,
            log_dir=str(tmp_path / "logs"),
            rdeps_limit=2000,
            cache_mounts=mounts,
        ),
    )

    assert cache_flags(rdeps_test_argv(runner)) == [
        f"--disk_cache={(tmp_path / 'cache/one').resolve()}",
        f"--repository_cache={(tmp_path / 'cache/two').resolve()}",
    ]


async def test_a_cache_the_rdeps_payload_does_not_carry_gets_no_flag(tmp_path) -> None:
    """No mounts, no flags — the same rule `buildverify` follows.

    A flag for a directory nothing created is worse than no flag: `--repository_cache=` pointed at
    a path that does not exist is a cache that appears configured, is written to somewhere nobody
    reads, and costs a full fetch on every attempt forever. The default is empty, so every one of
    this file's other rdeps tests exercises this branch — which is exactly why it is asserted
    once, explicitly, rather than left as an unnamed property of the fixtures.
    """
    runner = _closure_runner(total=12)
    await RdepverifyWorker(runner=runner).run(
        make_ctx(tmp_path),
        RdepverifyInput(
            dest="java/com/acme/widget",
            integration_ref=SNAPSHOT,
            log_dir=str(tmp_path / "logs"),
            rdeps_limit=2000,
        ),
    )

    argv = rdeps_test_argv(runner)
    assert cache_flags(argv) == []
    assert all(CACHE_MOUNT_ROOT not in a for a in argv), argv


def rdeps_query_argvs(runner: RecordingRunner) -> list[tuple[str, ...]]:
    """Every `bazel query` this worker ran — the closure query, and the depth-1 one when sampled."""
    queries = [call for call in runner.calls if call[1] == "query"]
    assert queries, runner.calls
    return queries


async def test_the_rdeps_queries_are_told_about_the_repository_cache_only(tmp_path) -> None:
    """`bazel query` gets `--repository_cache=` and deliberately NOT `--disk_cache=`.

    `rdeps_closure` loads the same module graph the phase's `bazel test` is about to load, and it
    ran with no cache flag at all: every attempt re-fetched every BCR module before it could even
    name the blast radius. So the repository cache goes on, with the HOST path, for the reason the
    `bazel test` wiring already gives — no `image`, no container, no `--volume=`, so a
    `/cache/<name>` value would name a directory that exists nowhere on this host.

    The disk cache is left off, and that is a measurement rather than a caution. Both flags PARSE
    for `query` on the vendored 9.2.0 — `canonicalize-flags --for_command=query --
    --disk_cache=/a --repository_cache=/r` echoes both back, so neither would be the exit-2
    `COMMAND_LINE_ERROR` that burns an attempt. But a probe `bazel query 'deps(//:all)'` over a
    workspace with one `bazel_dep` wrote 2.3 MB into the repository cache and zero files into the
    disk cache: `query` executes no actions, and actions are the only thing a disk cache holds.
    A flag that cannot hit implies a cache that is working, which is worse than an absent one.

    Both queries are checked because the depth-1 query runs exactly when a repo is over the limit
    — the widest repos, reloading the graph the first query just fetched.
    """
    disk, repo = a_cache(tmp_path, "disk"), a_cache(tmp_path, "repo")
    runner = _closure_runner(total=12)
    result = await RdepverifyWorker(runner=runner).run(
        make_ctx(tmp_path),
        RdepverifyInput(
            dest="java/com/acme/widget",
            integration_ref=SNAPSHOT,
            log_dir=str(tmp_path / "logs"),
            rdeps_limit=4,
            rdeps_sample_n=2,
            cache_mounts=[
                CacheMount(role="disk", path=str(disk)),
                CacheMount(role="repository", path=str(repo)),
            ],
        ),
    )

    assert result.status == "ok"
    queries = rdeps_query_argvs(runner)
    assert len(queries) == 2, "the sampled closure runs the depth-1 query too"
    for argv in queries:
        assert cache_flags(argv) == [f"--repository_cache={repo}"], argv
        assert not any(f"={CACHE_MOUNT_ROOT}/" in a for a in argv), (
            "there is no container here, so the container-side path names nothing on this host"
        )
    # The `bazel test` keeps BOTH: it executes actions, so the disk cache is the whole point there.
    assert cache_flags(rdeps_test_argv(runner)) == [
        f"--disk_cache={disk}",
        f"--repository_cache={repo}",
    ]


async def test_the_rdeps_query_cache_flag_is_the_one_the_cli_configures(tmp_path) -> None:
    """The query's `--repository_cache=` value comes from `verify.repository_cache` via
    `cli._cache_mounts` — the same object Phase 3 and the phase's own `bazel test` were handed.

    Pinned against `_cache_mounts` rather than a literal so a settings rename breaks this test
    instead of quietly pointing the query at a directory nothing else uses. The configured names
    are role-less (`cache/one`, `cache/two`) so an implementation that recovered "which cache is
    this" from the directory NAME — instead of from `CacheMount.role` — cannot pass.
    """
    from fleet.cli import _cache_mounts
    from fleet.settings import FleetSettings
    from tests.test_cli import FLEET_YAML, write_config

    write_config(
        tmp_path,
        fleet=FLEET_YAML + "verify:\n  disk_cache: cache/one\n  repository_cache: cache/two\n",
    )
    mounts = _cache_mounts(FleetSettings.load(tmp_path / "config"))

    runner = _closure_runner(total=12)
    await RdepverifyWorker(runner=runner).run(
        make_ctx(tmp_path),
        RdepverifyInput(
            dest="java/com/acme/widget",
            integration_ref=SNAPSHOT,
            log_dir=str(tmp_path / "logs"),
            rdeps_limit=2000,
            cache_mounts=mounts,
        ),
    )

    assert cache_flags(rdeps_query_argvs(runner)[0]) == [
        f"--repository_cache={(tmp_path / 'cache/two').resolve()}"
    ]


async def test_a_query_cache_the_rdeps_payload_does_not_carry_gets_no_flag(tmp_path) -> None:
    """No mounts, no flags on the query either — the same rule the `bazel test` follows.

    `--repository_cache=` pointed at a path nobody created is a cache that appears configured, is
    written where nothing reads it, and costs a full module fetch on every attempt forever.
    """
    runner = _closure_runner(total=12)
    await RdepverifyWorker(runner=runner).run(
        make_ctx(tmp_path),
        RdepverifyInput(
            dest="java/com/acme/widget",
            integration_ref=SNAPSHOT,
            log_dir=str(tmp_path / "logs"),
            rdeps_limit=2000,
        ),
    )

    argv = rdeps_query_argvs(runner)[0]
    assert cache_flags(argv) == []
    assert all(CACHE_MOUNT_ROOT not in a for a in argv), argv


async def test_a_hand_written_rdeps_cache_flag_in_extra_args_still_wins(tmp_path) -> None:
    """Cache flags first, `extra_args` last — the order `buildverify._bazel_argv` settled on.

    Bazel keeps the LAST occurrence of a non-`allowMultiple` option, checked against the vendored
    9.2.0 rather than recalled: `bazel canonicalize-flags --for_command=test -- --disk_cache=/a
    --disk_cache=/b` prints `--disk_cache=/b`. Hand-writing the flag into `extra_args` was the
    only way to reach a cache from Phase 4 before this, so an operator's config may carry one
    today and it must keep winning. Resolving the same flag differently in Phase 3 and Phase 4 —
    which is what any other order here would do — is worse than either answer alone.
    """
    disk = a_cache(tmp_path, "disk")
    runner = _closure_runner(total=12)
    await RdepverifyWorker(runner=runner).run(
        make_ctx(tmp_path),
        RdepverifyInput(
            dest="java/com/acme/widget",
            integration_ref=SNAPSHOT,
            log_dir=str(tmp_path / "logs"),
            rdeps_limit=2000,
            cache_mounts=[CacheMount(role="disk", path=str(disk))],
            extra_args=["--disk_cache=/cache/disk/operator"],
        ),
    )

    argv = rdeps_test_argv(runner)
    assert cache_flags(argv) == [f"--disk_cache={disk}", "--disk_cache=/cache/disk/operator"]
    assert argv[-1] == "--disk_cache=/cache/disk/operator", (
        "last wins in bazel's option parser, so the operator's value must be the last one"
    )


# =======================================================================================
# (4b) "no test targets" — Bazel exit 4, and what it must NOT cost (§3.3, §12.11)
# =======================================================================================


REGISTRY_UNREACHABLE = (
    "Could not resolve",
    "Connection",
    "Error accessing registry",
    "Connect timed out",
    "Read timed out",
)
"""How a Bazel that could not reach BCR says so (the same enumeration `test_bazel.py` uses)."""


def bazel_exits(code: int, *, unit: str = TEST_UNIT, stderr: str = "") -> RecordingRunner:
    """A runner whose `build` is green and whose `unit` step exits `code`."""
    return RecordingRunner(
        [
            (
                lambda p: p[1] == unit,
                lambda parts: ProcResult(
                    argv=parts,
                    exit_code=code,
                    stdout_tail="INFO: Build completed successfully, 4 total actions\n",
                    stderr_tail=stderr,
                    duration_ms=1200,
                    timed_out=False,
                ),
            ),
            (lambda p: True, ok("")),
        ]
    )


def a_package(tmp_path: Path, dest: str = "py/acme_lib_py") -> str:
    (tmp_path / dest).mkdir(parents=True, exist_ok=True)
    (tmp_path / dest / "BUILD.bazel").write_text("# generated\n", encoding="utf-8")
    return dest


async def drive_the_ladder(
    payload: BuildverifyInput, runner: RecordingRunner, workdir: Path
) -> tuple[int, RepoStatus]:
    """Run the worker through the REAL ADR-0014 ladder until it stops; report `(attempts, status)`.

    Asserting on `FailureClass` alone cannot express the defect this section exists for. The
    misclassification was only the first domino: what the fleet actually pays is `phases.attempts`
    and what a human pays is the terminal status, and both are decided in
    `orchestrator/retry.py`. So the real `RetryPolicy` is driven here rather than re-implemented,
    because a hand-rolled loop would happily agree with a broken one.
    """
    policy = RetryPolicy(rng=random.Random(0), backoff_base_s=0.0, backoff_cap_s=0.0)
    state = LadderState()
    while True:
        result = await BuildverifyWorker(runner=runner).run(
            make_ctx(workdir, attempt=state.attempts + 1), payload
        )
        if result.error is None:
            assert result.status == "ok", result.status
            return state.attempts, RepoStatus.SUCCEEDED
        decision = policy.decide(state, result.error)
        state = decision.state
        if decision.action is RetryAction.TERMINATE:
            assert decision.terminal_status is not None
            return state.attempts, decision.terminal_status


async def test_a_repo_with_no_tests_costs_no_attempt_and_reaches_no_human(tmp_path) -> None:
    """Defect D9: `acme-lib-py` built clean, `bazel test` exited 4, and the repo escalated.

    Why it matters, and why the assertion is on the counters rather than on the class: "this
    library has no tests" is a completely ordinary state for a repo in a 250-repo fleet. The old
    classifier mapped every non-OOM, non-127 exit to a retryable `TEST_FAILURE`, so exit 4 spent
    all three ADR-0014 rungs — two of them LLM-bearing, at `BUILD_DIAGNOSIS` prices — and then
    parked a repo that *built perfectly* in `REQUIRES_HUMAN_INTERVENTION`, where it also blocks
    every dependent through §3.5's `blocked_by` propagation. Multiply by the library repos in a
    fleet and the harness spends its budget and its operator on nothing at all.

    `baseline_test_count = 0` is what makes this the benign case: this repo never had tests, so
    there is no §12.11 floor to fall below.
    """
    dest = a_package(tmp_path)
    payload = BuildverifyInput(
        dest=dest,
        integration_ref=SNAPSHOT,
        log_dir=str(tmp_path / "logs"),
        baseline_test_count=0,
    )
    runner = bazel_exits(
        NO_TESTS_FOUND, stderr="ERROR: No test targets were found, yet testing was requested\n"
    )

    attempts, status = await drive_the_ladder(payload, runner, tmp_path)

    assert attempts == 0, "an empty test set must not spend a rung of the repo's ladder"
    assert status is RepoStatus.SUCCEEDED, (
        "a repo that built cleanly and has nothing to test never reaches a human"
    )

    result = await BuildverifyWorker(runner=runner).run(make_ctx(tmp_path), payload)
    assert result.error is None and result.status == "ok"
    assert result.completed_units == [BUILD_UNIT, TEST_UNIT], "the test step ran to completion"
    assert not result.remaining_units, "re-entry owes nothing"
    out = result.output
    assert out is not None
    assert out.build_ok is True
    assert out.no_test_targets is True, "its own recorded outcome, not a silent pass"
    assert out.tests_ran is False, "nothing ran"
    assert out.test_ok is False, "and nothing passed — `no_test_targets` is what tells them apart"
    assert out.tests_lost is False and out.green is True
    assert out.steps[-1].exit_code == 4, "the exit code is still recorded verbatim on the attempt"
    assert out.diagnosis == "", "no failure, so no model was asked to explain one"


async def test_a_real_test_failure_still_fails_still_charges_and_still_escalates(
    tmp_path,
) -> None:
    """Bazel exit 3 — `TESTS_FAILED`. The fix must not turn every red suite into a pass.

    Why it matters: the cheapest way to make D9 "go away" is to stop treating a non-zero `bazel
    test` as a failure, which would migrate 250 repos with their test suites red and report them
    all green. Exit 3 and exit 4 are one digit apart and mean opposite things, so the separation
    has to be asserted from both sides.
    """
    dest = a_package(tmp_path, "ts/acme/app")
    payload = BuildverifyInput(
        dest=dest, integration_ref=SNAPSHOT, log_dir=str(tmp_path / "logs")
    )
    runner = bazel_exits(3, stderr="FAIL: //ts/acme/app:app_test (see …/test.log)\n")

    result = await BuildverifyWorker(runner=runner).run(make_ctx(tmp_path), payload)

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.failure_class is FailureClass.TEST_FAILURE
    assert result.error.retryable is True, "a red suite is exactly what the repair rung is for"
    out = result.output
    assert out is not None and out.tests_ran is True and out.no_test_targets is False
    assert out.green is False

    attempts, status = await drive_the_ladder(payload, runner, tmp_path)
    assert attempts == 3, "a genuine failure still costs the full ladder"
    assert status is RepoStatus.REQUIRES_HUMAN_INTERVENTION


async def test_a_repo_that_HAD_tests_and_now_has_none_is_the_regression_12_11_refuses(
    tmp_path,
) -> None:
    """§12.11's green-and-empty: `baseline_test_count > 0` and zero migrated test targets.

    Why it matters: this is the failure mode the D9 fix could easily have created. A `filegroup`
    emitted where a `*_test` belonged builds clean and "tests" clean at zero targets — it passes
    §3.3's `bazel build` half while deleting the repo's entire safety net, and it is
    indistinguishable from the benign case by exit code alone. The pre-migration count is the
    only evidence that separates them, which is why it is an input and not an inference.

    `BUILD_ERROR` rather than `TEST_FAILURE` because no test failed: the generator emitted the
    wrong target, and the repair rung that can fix that is the one that regenerates `BUILD.bazel`.
    """
    dest = a_package(tmp_path, "java/com/acme/widget")
    payload = BuildverifyInput(
        dest=dest,
        integration_ref=SNAPSHOT,
        log_dir=str(tmp_path / "logs"),
        baseline_test_count=14,
    )
    runner = bazel_exits(
        NO_TESTS_FOUND, stderr="ERROR: No test targets were found, yet testing was requested\n"
    )

    result = await BuildverifyWorker(runner=runner).run(make_ctx(tmp_path), payload)

    assert result.status == "failed", "14 native tests became 0; that is not a pass"
    assert result.error is not None
    assert result.error.failure_class is FailureClass.BUILD_ERROR
    assert result.error.retryable is True
    assert result.error.exit_code == 4
    out = result.output
    assert out is not None
    assert out.build_ok is True, "the build really was green — that is what makes this insidious"
    assert out.no_test_targets is True and out.tests_lost is True
    assert out.green is False, "green may not be claimed by a migration that lost the tests"
    assert out.baseline_test_count == 14, "§12.11 reports (repo, baseline, migrated) triples"

    attempts, status = await drive_the_ladder(payload, runner, tmp_path)
    assert attempts == 3 and status is RepoStatus.REQUIRES_HUMAN_INTERVENTION, (
        "a deleted safety net is exactly the thing a human should be asked about"
    )


def test_every_bazel_exit_code_this_classifier_reads_was_verified_against_the_binary() -> None:
    """The whole table, one row per code, reproduced against `tools/bin/bazel` (Bazel 9.2.0).

    Why a table and not one special case: D9 was not "exit 4 was forgotten", it was "the exit
    codes were never checked". Exit 2 (a malformed flag the harness itself built) and exit 9
    (another `bazel` holding the output-base lock) were both retryable `TEST_FAILURE`s too — the
    first burning three rungs to re-submit a byte-identical argv, the second charging a repo for
    the fleet's own scheduling. `retry.py` branches on `retryable` and never on message text, so
    these two columns are the entire policy.
    """
    def classify(code: int, *, unit: str = TEST_UNIT) -> tuple[FailureClass, bool]:
        result = ProcResult(
            argv=("bazel", unit, "//py/acme_lib_py/..."),
            exit_code=code,
            stdout_tail="",
            stderr_tail="",
            duration_ms=1,
            timed_out=False,
        )
        return classify_build_failure(result, unit=unit)

    # (code, unit, class, retryable) — see the table above `classify_build_failure` in src.
    assert classify(1, unit=BUILD_UNIT) == (FailureClass.BUILD_ERROR, True), "BUILD_FAILURE"
    assert classify(1) == (FailureClass.TEST_FAILURE, True), "the build under `test` failed"
    assert classify(2) == (FailureClass.BUILD_ERROR, False), (
        "COMMAND_LINE_ERROR: the next rung would submit the identical argv"
    )
    assert classify(3) == (FailureClass.TEST_FAILURE, True), "TESTS_FAILED"
    assert classify(4) == (FailureClass.BUILD_ERROR, True), (
        "NO_TESTS_FOUND reaches the classifier only as the §12.11 regression; the worker returns "
        "success for the benign case before ever calling it"
    )
    assert classify(8) == (FailureClass.TRANSIENT_INFRA, True), (
        "INTERRUPTED — §3.4's wave drain kills builds, and the fleet's clock may not spend a "
        "repo's three chances"
    )
    assert classify(9) == (FailureClass.TRANSIENT_INFRA, True), "the output-base lock was held"
    assert classify(36) == (FailureClass.TRANSIENT_INFRA, True), "LOCAL_ENVIRONMENTAL_ERROR"
    assert classify(127) == (FailureClass.BUILD_ERROR, False), "bazel absent from the image"
    assert classify(137) == (FailureClass.TRANSIENT_INFRA, True), "cgroup OOM kill"
    assert classify(-9) == (FailureClass.TRANSIENT_INFRA, True), "SIGKILL as a negative status"


def test_no_test_targets_is_read_only_off_the_test_step_and_only_from_a_finished_process() -> None:
    """A timeout and a never-started process have no exit code worth reading.

    Why: `ProcResult.exit_code` defaults are not evidence. A build killed at the wall clock could
    carry any status, and reading 4 out of one would report "no tests" for a build that never got
    far enough to have any — turning a `TIMEOUT` into a green repo.
    """
    def proc(**overrides) -> ProcResult:
        fields = {
            "argv": ("bazel", "test", "//x/..."),
            "exit_code": 4,
            "stdout_tail": "",
            "stderr_tail": "",
            "duration_ms": 1,
            "timed_out": False,
        }
        fields.update(overrides)
        return ProcResult(**fields)  # type: ignore[arg-type]

    assert classify_build_failure(proc(timed_out=True), unit=TEST_UNIT) == (
        FailureClass.TIMEOUT,
        True,
    )
    assert classify_build_failure(proc(started=False), unit=TEST_UNIT) == (
        FailureClass.TRANSIENT_INFRA,
        True,
    )
    assert classify_build_failure(proc(), unit=BUILD_UNIT) == (FailureClass.BUILD_ERROR, True), (
        "`bazel build` cannot report NO_TESTS_FOUND; a 4 from it is an ordinary build failure"
    )


DISK_FLOOR_NO_VOLUME_CLEARS = 2**62
"""~4.6 EB. The gate below is therefore driven by a REAL `statvfs` of a real directory, not by a
patched `disk_usage`: the free-space number the harness reports is the one the kernel gave it."""


async def test_buildverify_refuses_to_launch_bazel_below_the_disk_floor(tmp_path) -> None:
    """§11.3: the free-space floor is checked BEFORE the first `bazel` process and before the
    container start, not after Bazel has met ENOSPC halfway through an action.

    Why it matters: a `bazel build` is the other operation in this harness that consumes
    gigabytes without asking — a disk cache, an action cache and an `external/` tree per output
    base. Meeting ENOSPC mid-build leaves a *poisoned* action cache (entries whose files were
    truncated) and, far worse, takes the state DB's `BEGIN IMMEDIATE` down with it, which is the
    write the §10 checkpoint guarantee rests on (§13 row 42). This project's own suite proved the
    hazard is not theoretical: it filled this host's volume to 0 bytes free.

    The assertion is that NOTHING RAN. A worker that launched Bazel and then reported a disk
    failure would satisfy a weaker test and none of the reasoning above.
    """
    dest = a_package(tmp_path)
    payload = BuildverifyInput(
        dest=dest,
        integration_ref=SNAPSHOT,
        log_dir=str(tmp_path / "logs"),
        min_free_bytes=DISK_FLOOR_NO_VOLUME_CLEARS,
    )
    runner = bazel_exits(0)

    result = await BuildverifyWorker(runner=runner).run(make_ctx(tmp_path), payload)

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.failure_class is FailureClass.DISK_EXHAUSTED
    assert result.error.retryable is False, (
        "retrying a full volume three times is three more chances to fill it"
    )
    detail = result.error.stderr_tail
    assert str(DISK_FLOOR_NO_VOLUME_CLEARS) in detail, detail
    assert str(shutil.disk_usage(tmp_path).free) in detail, detail
    assert not runner.calls, f"bazel was launched anyway: {runner.calls}"
    assert result.output is not None and result.output.steps == []
    assert result.remaining_units == [BUILD_UNIT, TEST_UNIT], "the whole phase is still owed"


async def test_a_build_is_not_gated_by_a_floor_the_volume_clears(tmp_path) -> None:
    """The negative control: a real check against a real volume, which passes.

    Without it, a gate that refused unconditionally would pass the test above while stopping the
    harness from ever building anything.
    """
    dest = a_package(tmp_path)
    payload = BuildverifyInput(
        dest=dest, integration_ref=SNAPSHOT, log_dir=str(tmp_path / "logs"), min_free_bytes=1
    )
    result = await BuildverifyWorker(runner=bazel_exits(0)).run(make_ctx(tmp_path), payload)
    assert result.status == "ok", result.error


@pytest.mark.integration
@pytest.mark.skipif(
    shutil.which("bazel") is None,
    reason="bazel is not installed on this host; the exit codes are asserted against recorded "
    "ProcResults instead, which proves the classifier but not what Bazel actually returns",
)
def test_real_bazel_exit_4_means_no_tests_and_exit_1_dominates_it(
    bazel_workspace: Path, bazel_startup_argv: tuple[str, ...]
) -> None:
    """The premise the whole classification rests on, taken from the binary rather than from memory.

    Two facts, and the second is the load-bearing one:

    * `bazel test` over a package with no test rule exits **4** after a *successful* build;
    * a `bazel test` whose build FAILED exits **1** — and prints "No test targets were found" as
      well. So the message is ambiguous and the exit code is not: 1 dominates 4, which is what
      makes "exit 4 ⇒ the tree is fine, there was simply nothing to run" mechanical. Had that
      gone the other way, treating exit 4 as success would have hidden real build errors.

    Deliberately dependency-free — one `genrule` per package, no `bazel_dep` — so it needs no
    network and cannot fail for a reason that is not the one under test.
    """
    (bazel_workspace / "MODULE.bazel").write_text('module(name = "probe")\n', encoding="utf-8")
    (bazel_workspace / "pkg").mkdir()
    (bazel_workspace / "pkg" / "BUILD.bazel").write_text(
        'genrule(name = "ok", outs = ["ok.txt"], cmd = "echo hi > $@")\n', encoding="utf-8"
    )
    (bazel_workspace / "bad").mkdir()
    (bazel_workspace / "bad" / "BUILD.bazel").write_text(
        'genrule(name = "broken", outs = ["b.txt"], cmd = "exit 1")\n', encoding="utf-8"
    )

    def bazel(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603
            [*bazel_startup_argv, *args],
            cwd=bazel_workspace,
            capture_output=True,
            text=True,
            check=False,
        )

    built = bazel("build", "//pkg/...")
    # Even a dependency-free MODULE.bazel resolves `bazel_tools`' own deps through BCR, so an
    # unreachable registry must SKIP rather than be reported as a wrong exit code — the two are
    # not remotely the same finding. The markers match `test_bazel.py`'s enumerated list rather
    # than "any non-zero exit", which would turn this test into a no-op exactly when it matters.
    if any(marker in built.stderr for marker in REGISTRY_UNREACHABLE):
        pytest.skip(f"Bazel Central Registry unreachable from this host: {built.stderr[-400:]}")
    assert built.returncode == 0, built.stderr[-2000:]

    empty = bazel("test", "//pkg/...")
    assert empty.returncode == NO_TESTS_FOUND, empty.stderr[-2000:]
    assert "No test targets were found" in empty.stderr
    assert "Build completed successfully" in empty.stderr, (
        "exit 4 is reported only after a GREEN build; that is why it is safe to call it success"
    )

    broken = bazel("test", "--keep_going", "//bad/...")
    assert broken.returncode == 1, broken.stderr[-2000:]
    assert "No test targets were found" in broken.stderr, (
        "the message alone cannot tell the two apart — only the exit code can"
    )


# =======================================================================================
# (5) prwriter: merge state is INGESTED, and a draft is a verdict
# =======================================================================================


async def test_prwriter_ingests_merge_state_and_holds_an_unmerged_dependency(tmp_path) -> None:
    """§3.4 step 5: `MERGED` is a fact about GitHub, and until something reads it back nothing
    writes it.

    Why it matters: three gates consume `MERGED` — the stacking precondition, the `blocked_by`
    release and the T1 stub trigger — and no worker can honestly produce it. A prwriter that
    trusted `PullRequestDraft.state` deadlocked the fleet at the first wave boundary. So the sync
    runs FIRST, and a dependency the forge does not call MERGED holds this PR rather than letting
    it be opened and marked ready.
    """
    held_gh = gh_runner(PR_VIEW_OPEN)
    payload = a_pr_payload(
        dependencies=[
            DependencyPr(repo_id="acme-commons", url="https://github.example/x/pull/7"),
            DependencyPr(repo_id="acme-core", url=None),
        ],
        ready=True,
        log_dir=str(tmp_path / "logs"),
    )
    held = await PrwriterWorker(runner=held_gh).run(make_ctx(tmp_path), payload)

    assert held.status == "partial", "held work is owed work, not a failure and not a pass"
    assert held.completed_units == ["pr_sync"]
    assert "pr_create" in held.remaining_units
    out = held.output
    assert out is not None and out.held is True
    assert out.unmerged_dependencies == ["acme-commons", "acme-core"]
    assert out.observed_states["acme-commons"] is PrState.OPEN, "the FORGE's answer, ingested"
    assert out.pr is None
    assert held_gh.argv_for("create") is None, "no PR is opened over an unmerged dependency"
    view = held_gh.argv_for("view")
    assert view is not None and view[-1] == "state,mergedAt,mergeCommit"

    merged_gh = gh_runner(PR_VIEW_MERGED)
    opened = await PrwriterWorker(runner=merged_gh).run(make_ctx(tmp_path), payload.model_copy(
        update={"dependencies": [
            DependencyPr(repo_id="acme-commons", url="https://github.example/x/pull/7")
        ]}
    ))
    assert opened.status == "ok"
    assert opened.output is not None
    assert opened.output.observed_states["acme-commons"] is PrState.MERGED
    assert opened.output.merge_commits == {"acme-commons": "abc1234"}
    assert opened.output.pr is not None and opened.output.pr.state is PrState.OPEN
    assert merged_gh.argv_for("ready") is not None, "nothing forced a draft, so --ready is honoured"


async def test_a_degraded_repos_pr_is_a_draft_carrying_the_stub_banner(tmp_path) -> None:
    """§3.5/§3.5.1: a stub-limited PR says so, in the body, rendered from data — and is a draft.

    Why it matters: a green build against a stub proves the consumer compiles against the stub's
    surface, not that it works against the real dependency. Opening that as ready hands a reviewer
    a verdict the verification never earned.
    """
    gh = gh_runner()
    report = a_report(
        verified_against_stubs=["maven:com.acme:commons"],
        stub_fidelity={"maven:com.acme:commons": StubFidelity.PUBLISHED_ARTIFACT},
    )
    assert report.equivalence is Equivalence.STUB_LIMITED

    result = await PrwriterWorker(runner=gh).run(
        make_ctx(tmp_path),
        a_pr_payload(
            report=report,
            repo_status=RepoStatus.DEGRADED,
            stub_states={"maven:com.acme:commons": StubState.ACTIVE},
            ready=True,
            log_dir=str(tmp_path / "logs"),
        ),
    )
    assert result.status == "ok"
    out = result.output
    assert out is not None and out.draft is True
    assert out.pr is not None and out.pr.state is PrState.DRAFTED
    assert out.pr.equivalence is Equivalence.STUB_LIMITED
    assert out.pr.stubbed_deps == ["maven:com.acme:commons"]
    create = gh.argv_for("create")
    assert create is not None and "--draft" in create
    assert gh.argv_for("ready") is None

    body = read(out.body_path)
    assert body.startswith(f"> **{STUB_BANNER}**")
    assert "maven:com.acme:commons" in body and "PUBLISHED_ARTIFACT" in body
    assert "--body-file" in create and str(out.body_path) in create, (
        "a body quoting build logs goes through a file, never the process table"
    )


async def test_prwriter_prose_is_optional_and_the_verdict_never_comes_from_the_model(
    tmp_path,
) -> None:
    """§3.4's LLM slot is title/body prose; the verdict fields are rendered from the report.

    Why it matters: with the model failing, the PR must still be complete and honest, and no model
    output may ever change what the body claims about the build. The second half used to express
    "no model" as `PrwriterWorker()` with nothing injected; a client is now structurally always
    present (§7.1), so the honest spelling of that case is a call that raises — which is the state
    a real run actually reaches, and the one the fallback exists for.
    """
    gh = gh_runner()
    model = FakeModelClient(
        {
            "pr_body": PrBody(body="Vendored the widget service.", highlights=()),
            "pr_title": PrTitle(title="migrate acme-widget"),
        }
    )
    result = await PrwriterWorker(runner=gh).run(
        make_ctx(tmp_path, model=model), a_pr_payload(log_dir=str(tmp_path / "logs"))
    )
    out = result.output
    assert out is not None and out.pr is not None
    assert out.pr.title == "migrate acme-widget"
    body = out.pr.body
    assert "Vendored the widget service." in body
    assert "- Verdict: **PASS**" in body and "`FULL`" in body
    assert sorted(model.roles) == ["pr_body", "pr_title"]

    bare = await PrwriterWorker(runner=gh_runner()).run(
        make_ctx(tmp_path), a_pr_payload(log_dir=str(tmp_path / "logs2"))
    )  # make_ctx's default client raises TierUnavailable on every role
    assert bare.output is not None and bare.output.pr is not None
    assert "- Verdict: **PASS**" in bare.output.pr.body
    assert bare.output.pr.title.startswith("[fleet wave 2]")


async def test_prwriter_refuses_a_failed_verification_and_a_foreign_report(tmp_path) -> None:
    """A PR opened on a `FAIL` report presents a red verification as reviewable work; a PR
    rendered from another repo's report is the one error a reviewer cannot catch by reading it."""
    worker = PrwriterWorker()
    ctx = make_ctx(tmp_path)
    assert await worker.preconditions_hold(ctx, a_pr_payload())
    assert not await worker.preconditions_hold(
        ctx, a_pr_payload(report=a_report(verdict="FAIL", rdeps_ok=False))
    )
    assert not await worker.preconditions_hold(
        ctx, a_pr_payload(report=a_report(repo_id="acme-other"))
    )


# =======================================================================================
# (5b) the forge is a payload field, not a hardcoded client
# =======================================================================================
#: A decoy shaped like a Gitea token, written into a throwaway `curl -K` file. Never a real
#: credential and valid nowhere — but it is what the argv scan below needs to be able to find.
FAKE_GITEA_TOKEN = "0011223344556677889900aabbccddeeff001122"  # noqa: S105 - a decoy


def gitea_runner() -> RecordingRunner:
    """A `curl` that answers every Gitea call with an open PR and an HTTP 200 trailer."""
    body = json.dumps(
        {
            "html_url": "http://gitea.invalid:3001/redmage/monorepo/pulls/3",
            "state": "open",
            "merged": False,
            "merged_at": None,
            "merge_commit_sha": None,
            "title": "migrate acme-widget",
        }
    )
    return RecordingRunner([(lambda parts: True, ok(f"{body}\n200"))])


def test_the_default_payload_still_selects_the_github_driver(tmp_path) -> None:
    """WHY: this seam is additive. 250 repos already depend on the `gh` path, and a payload that
    names no forge must keep getting `GitHubCli` — which must still satisfy the Protocol the
    worker now depends on, or the refactor broke the only path that was ever in production."""
    assert a_pr_payload().forge == "github"
    assert a_pr_payload().forge_token_config == "", "no token config is needed to use `gh`"
    github = build_forge("github")
    assert isinstance(github, GitHubCli)
    assert isinstance(github, Forge), "GitHubCli must still satisfy the Forge Protocol"


async def test_prwriter_opens_the_pr_through_the_forge_its_payload_names(tmp_path) -> None:
    """WHY: `vcs/gitea.py` existing gives a self-hosted operator nothing; the WORKER reaching it
    does. `gh` speaks the GitHub API and cannot talk to Gitea, so a worker that constructed
    `GitHubCli` regardless of `pr.forge` would return `ok`, record a PR url, and have pointed the
    whole of §3.4 at a host the operator never configured.

    The token assertion is the non-negotiable half (§11.4). `attempts.command` persists argv
    verbatim, so the credential travels as the PATH of a mode-600 `curl -K` file and is read by
    curl from disk after `execve`. The control is the first assertion: the token really is in the
    file the argv names, so a fixture that forgot to write one could not make this pass vacuously.
    """
    config = tmp_path / "gitea-curl.conf"
    config.write_text(f'header = "Authorization: token {FAKE_GITEA_TOKEN}"\n', encoding="utf-8")
    config.chmod(0o600)
    assert FAKE_GITEA_TOKEN in read(config), "the control: the token is in the file"

    curl = gitea_runner()
    result = await PrwriterWorker(runner=curl).run(
        make_ctx(tmp_path),
        a_pr_payload(
            forge="gitea",
            forge_url="http://gitea.invalid:3001",
            forge_owner="redmage",
            forge_token_config=str(config),
            gh_repo="monorepo",
            log_dir=str(tmp_path / "logs"),
        ),
    )
    assert result.status == "ok", result.error
    assert result.output is not None and result.output.pr is not None
    assert result.output.pr.url == "http://gitea.invalid:3001/redmage/monorepo/pulls/3"

    create = curl.argv_for("POST")
    assert create is not None and create[0] == "curl", curl.calls
    assert create[-1] == "http://gitea.invalid:3001/api/v1/repos/redmage/monorepo/pulls", create
    assert "-K" in create and create[create.index("-K") + 1] == str(config), create
    for call in curl.calls:
        assert not any(FAKE_GITEA_TOKEN in part for part in call), call
    # The body travels as a file too, for the same §11.4 reason.
    assert "--data-binary" in create and create[create.index("--data-binary") + 1].startswith("@")


async def test_a_gitea_failure_is_the_same_transient_requeue_a_gh_failure_is(tmp_path) -> None:
    """WHY: `PrwriterWorker` catches ONE exception type. Two unrelated error trees would make the
    Gitea path fall through as an unhandled crash instead of the `TRANSIENT_INFRA` re-queue the
    GitHub path gets — a forge outage would then burn the repo's attempts and strand it."""
    refuse = RecordingRunner(
        [(lambda parts: True, ok('{"message":"token does not have permission"}\n403'))]
    )
    result = await PrwriterWorker(runner=refuse).run(
        make_ctx(tmp_path),
        a_pr_payload(
            forge="gitea",
            forge_url="http://gitea.invalid:3001",
            forge_owner="redmage",
            forge_token_config=str(tmp_path / "absent.conf"),
            gh_repo="monorepo",
            dependencies=[DependencyPr(repo_id="acme-commons", url="http://x.invalid/a/b/pulls/1")],
            log_dir=str(tmp_path / "logs"),
        ),
    )
    assert result.status == "failed"
    assert result.error is not None
    assert result.error.failure_class is FailureClass.TRANSIENT_INFRA
    assert result.error.retryable is True
    assert "gitea" in result.error.exception_type.lower(), result.error.exception_type


# =======================================================================================
# (6) the one test that needs the real binary
# =======================================================================================


@pytest.mark.integration
@pytest.mark.skipif(
    shutil.which("bazel") is None,
    reason="bazel is not installed on this host; the argv and the closure logic are asserted "
    "offline through the injected CommandRunner instead",
)
async def test_the_default_runner_leaves_the_full_stream_where_artifact_ref_points(
    tmp_path, bazel_cache_home: Path
) -> None:
    """`LoggedRunner` really launches `bazel` and really leaves its stream at the recorded path.

    Why it matters: `WorkerError.artifact_ref` is the only copy of a failing build's output that
    survives the 32 KiB tail, and a runner that kept no file would make the repair rung prompt on
    a summary — the thing Constraint 5 forbids.

    The bytes are asserted, not just the file's existence: a runner that created an empty log and
    reported the path would satisfy `is_file()` and lose the build output all the same. `bazel
    --version` is a trivial input on purpose — this test is about the runner's plumbing, not
    about Bazel — but the plumbing is only exercised if a real process really wrote to it.

    `XDG_CACHE_HOME` is redirected because even `bazel --version` extracts Bazel's ~700 MB
    install base into the output user root, and a test suite does not get to grow a developer's
    home directory by that much as a side effect of checking a log file. The `bazel_cache_home`
    fixture points it at the suite's one shared Bazel root — which is reaped when the test ends —
    rather than at a per-test directory, so that 700 MB is paid once and released, not once per
    test and retained.
    """
    log_dir = tmp_path / "logs"
    result = await LoggedRunner(log_dir, stem="bazel-version")(["bazel", "--version"])
    assert result.started and result.exit_code == 0, result.stderr_tail
    assert result.stdout_path is not None and result.stdout_path.is_file()
    assert result.stdout_path.parent == log_dir
    on_disk = result.stdout_path.read_text(encoding="utf-8")
    assert on_disk.startswith("bazel "), on_disk[:200]
    assert on_disk.strip() in result.stdout_tail, "the tail and the file disagree about the run"


# =======================================================================================
# (7) D10 + the three wiring gaps: what the generated files NAME must exist, and the
#     mechanisms that produce it must actually be reached in production
# =======================================================================================

_ROOT_LABEL = re.compile(r'"//:([^"]+)"')
"""`//:<path>` — a label into the ROOT package of the MAIN repository, which Bazel resolves to a
file at the monorepo root. Every one of them is a promise `MODULE.bazel` makes about the tree."""

_SAME_PACKAGE_LABEL = re.compile(r'"(?::)([A-Za-z0-9_.+-]+)"')
"""`":<name>"` — a label into the package the BUILD file itself declares."""

_TARGET_NAME = re.compile(r'^ {4}name = "([^"]+)",$', re.MULTILINE)


def _source_tree(root: Path, dest: str) -> None:
    """One package holding a plausible file of every language, so the same unit can be handed to
    every adapter and each picks the sources its own rules accept — no per-ecosystem branch in
    the test, which is the same rule §13 imposes on `src/`."""
    package = root / dest
    (package / "src").mkdir(parents=True, exist_ok=True)
    for name in _POLYGLOT_SRCS:
        path = package / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("// fixture\n", encoding="utf-8")


_POLYGLOT_SRCS = (
    "Widget.java",
    "index.ts",
    "widget.py",
    "widget.go",
    "src/lib.rs",
    "README.md",
)


_RESOLVED_STANDIN = "# the resolver's output; `cli._run_resolution` computes the real one\n"
"""What `_adapter_payload` puts in a declared `Resolution`'s `lock_path` (see below).

Not a lockfile of any dialect and not trying to be: nothing in this file parses one, and a
plausible-looking fake would invite exactly the "the floor happened to look right" confusion the
resolver seam exists to end. `tests/test_build_e2e.py`'s `FAKE_LOCKS` is where a resolution-SHAPED
answer belongs, because that file asserts on the content."""


def _declared_dependency(eco: Ecosystem) -> Coordinate:
    """One external coordinate shaped the way `eco`'s OWN manifests would have recorded it.

    Maven's `org.other:dep:1.2.3` served every adapter until Go's root `go.mod` became both the
    file that lands at `//:go.mod` and the file `go mod download all` reads. `go` rejects `1.2.3`
    as a module version and `org.other/dep` parses only by luck, so `go.py` refuses to write
    either and raises naming the coordinate (ADR-0050 step 2). That loud failure is asserted in
    `test_ecosystems.py`; what this helper keeps is the *structural* contract — every generated
    reference has a file behind it — which is what the callers below are about.
    """
    if eco is Ecosystem.GO:
        return Coordinate(
            ecosystem=eco, group="github.com/other", name="dep", version_spec="v1.2.3"
        )
    return Coordinate(ecosystem=eco, group="org.other", name="dep", version_spec="1.2.3")


def _adapter_payload(root: Path, eco: Ecosystem, dest: str) -> tuple[BuildgenInput, BuildUnit]:
    """The payload the §3.3 driver builds for one unit, read entirely off the adapter."""
    adapter = ecosystems.for_ecosystem(eco)
    unit = BuildUnit(
        unit_id=REPO,
        ecosystem=eco,
        dest=dest,
        srcs=list(_POLYGLOT_SRCS),
        published=Coordinate(ecosystem=eco, group="com.acme", name="widget"),
        external_coordinates=[_declared_dependency(eco)],
    )
    support = _resolve_support_files(
        root, [*adapter.workspace_files([unit]), *adapter.package_files(unit)]
    )
    # …plus the one case the carry step alone cannot answer. `cli._resolved_support_files` is
    # carry → resolve → floor; this helper called only the carry step, which was invisible while
    # every declared lock also had a non-empty floor behind it. Go's `go.sum` has none on purpose
    # — a synthesized checksum is either absent or WRONG — so the resolver is its ONLY source,
    # and skipping the resolve left `materialize` writing a **0-byte `go.sum`** into the tree
    # with nothing to catch it: a file that exists and says nothing passes an existence check.
    # Resolving is a network operation, so the stand-in above stands in for it. Deliberately
    # narrow: a lock that DOES have a floor keeps it, because
    # `test_a_derived_requirements_file_is_built_from_what_phase_1_recorded` is an assertion
    # about that floor. The real command runs against the injected runner in
    # `tests/test_build_e2e.py`.
    plan = adapter.resolution([unit])
    if plan is not None:
        support = tuple(
            f.model_copy(update={"content": _RESOLVED_STANDIN})
            if f.path == plan.lock_path and not f.content
            else f
            for f in support
        )
    return (
        BuildgenInput(
            unit=unit,
            targets=[*adapter.generate_targets(unit), *adapter.test_targets(unit)],
            gazelle=adapter.gazelle_config(unit),
            workspace_deps=adapter.workspace_deps(unit),
            toolchains=adapter.toolchain_requirements(),
            support_files=list(support),
            requirements=[
                ExternalRequirement(
                    coord_key=c.key, repo_id=REPO, version_spec=c.version_spec or "1.0.0"
                )
                for c in unit.external_coordinates
            ],
            ruleset_versions=dict(BuildSection().ruleset_versions),
        ),
        unit,
    )


@pytest.mark.parametrize("eco", sorted(Ecosystem, key=lambda e: e.value))
async def test_every_file_the_generated_files_name_exists_after_the_phase_that_writes_them(
    eco: Ecosystem, tmp_path
) -> None:
    """D10, asserted GENERALLY: walk what the generated `MODULE.bazel` and `BUILD.bazel` name and
    require every one of it to be on disk when Phase 3 step 2/3 is done.

    **Why:** the defect was never "we forgot `requirements.lock`". It was that emitting a
    reference and creating the file it names were two different phases with no contract between
    them, so `pip.parse(requirements_lock = "//:requirements.lock")`, `npm_translate_lock`'s
    `pnpm_lock` and `verify_node_modules_ignored`, `crate.from_cargo`'s `cargo_lockfile` and
    `ts_project(tsconfig = ":tsconfig")` all named things nothing wrote — and real Bazel was the
    first thing in the whole harness to notice (`Error in read: Unable to load package for
    //:requirements.lock`). A test that asserted those four names would go stale the moment a
    seventh ruleset is added, and the next missing file would again be found by Bazel. So the
    references are DERIVED from the emitted text, and the assertion is that the set of promises
    the text makes is empty of unkept ones — which is a check a new ruleset cannot slip past.

    Parameterized over every `Ecosystem` rather than over the interesting ones, so an adapter
    added tomorrow is covered by construction (§7.5's registry is a total bijection).
    """
    ecosystems.discover()
    adapter = ecosystems.for_ecosystem(eco)
    dest = f"{adapter.monorepo_dir}/acme/widget"
    _source_tree(tmp_path, dest)
    payload, _ = _adapter_payload(tmp_path, eco, dest)

    result = await BuildgenWorker().run(make_ctx(tmp_path), payload)
    assert result.status == "ok", result.error
    out = result.output
    assert out is not None

    # An EMPTY file is not a kept promise, and the reference-derived check below cannot say so:
    # `materialize` writes every `SupportFile.content` unconditionally, so a support file with
    # no carry, no floor and no resolved content lands as a 0-byte file that satisfies every
    # `is_file()` in this test. That is not hypothetical — it is what Go's `go.sum` did until
    # `_adapter_payload` above started applying the resolve step. Some of these files are named
    # by no label at all (`go.sum` is read by Gazelle because it sits BESIDE the `go.mod` a label
    # names), so this is the only check that covers them.
    hollow = sorted(
        path
        for path in out.support_file_paths
        if not (tmp_path / path).read_text(encoding="utf-8").strip()
    )
    assert hollow == [], (
        f"{eco.value}: {hollow} were materialized empty — an empty lockfile is indistinguishable "
        f"from 'this repo has no dependencies', which is exactly how a missing resolution stays "
        f"invisible until a ruleset reports it as its own error"
    )

    module = read(out.module_bazel_path)
    unkept = sorted(ref for ref in _ROOT_LABEL.findall(module) if not (tmp_path / ref).is_file())
    assert unkept == [], (
        f"{eco.value}: MODULE.bazel names {unkept} at the monorepo root and no phase created "
        f"them — this is D10, and Bazel reports it as the RULESET's error, not as ours\n{module}"
    )

    build_text = read(out.build_bazel_path)
    defined = set(_TARGET_NAME.findall(build_text))
    dangling = sorted(
        name
        for name in _SAME_PACKAGE_LABEL.findall(build_text)
        if name not in defined and not (tmp_path / dest / name).exists()
    )
    assert dangling == [], (
        f"{eco.value}: BUILD.bazel references {dangling} in its own package, which is neither a "
        f"target it declares nor a file in the tree\n{build_text}"
    )
    for target in payload.targets:
        for attr in ("src", "srcs"):
            value = target.attrs.get(attr)
            named = [value] if isinstance(value, str) else list(value or [])
            for path in named:
                assert (tmp_path / dest / path).exists(), (
                    f"{eco.value}: {target.rule}({target.name}) names {path!r} in `{attr}` and "
                    f"the file is not in the package"
                )


async def test_a_real_lockfile_in_the_source_repo_is_carried_over_not_reinvented(
    tmp_path,
) -> None:
    """A `Cargo.lock` that the source repo really shipped survives into the monorepo root, byte
    for byte — and only a repo that shipped none gets a synthesized floor.

    **Why:** a lockfile IS a resolution — transitive versions, integrity hashes, the feature
    unification a resolver already did. Nothing in this harness can reconstruct one, so a
    synthesized lock would produce a monorepo that installs different packages than the repo it
    was migrated from, while looking entirely normal in review. Phase 2 had already moved the
    repo's own lock to `<dest>/Cargo.lock`; it was never dropped, it was simply never promoted to
    the root that `crate.from_cargo` names. Carrying it is the whole fix, and this test is what
    stops the cheaper "write a plausible empty lock" from coming back.

    **Rust and not npm, because the npm answer changed and the reason is worth stating.** This
    test used to be written against `pnpm-lock.yaml`, and ADR-0048 falsified that: with one pnpm
    importer per JS repo the root lock is a resolution of the WORKSPACE, which no single repo's
    lock is, so `JsAdapter.workspace_files()` deliberately declares no `carry_from` for it and
    the resolver always runs. The invariant this test guards is unchanged and Rust still states
    it — `crate.from_cargo` has no resolver at all, so a carried lock is the only honest source.
    The npm half is asserted as its own fact below rather than deleted.
    """
    ecosystems.discover()
    adapter = ecosystems.for_ecosystem(Ecosystem.CARGO)
    dest = f"{adapter.monorepo_dir}/widget"
    _source_tree(tmp_path, dest)
    real = (
        "# This file is automatically @generated by Cargo.\n"
        "version = 4\n\n[[package]]\nname = \"widget\"\n"
    )
    (tmp_path / dest / "Cargo.lock").write_text(real, encoding="utf-8")

    payload, _ = _adapter_payload(tmp_path, Ecosystem.CARGO, dest)
    result = await BuildgenWorker().run(make_ctx(tmp_path), payload)
    assert result.status == "ok", result.error

    carried = read(tmp_path / "Cargo.lock")
    assert carried == real, "the repo's own resolution, not one we made up"
    assert "GENERATED BY fleet" not in carried

    # The control: the identical adapter, a tree with no lock in it, gets the floor — and the
    # floor SAYS it is one, so a reviewer can tell the two apart in the monorepo.
    bare = tmp_path / "bare"
    _source_tree(bare, dest)
    bare_payload, _ = _adapter_payload(bare, Ecosystem.CARGO, dest)
    bare_result = await BuildgenWorker().run(make_ctx(bare), bare_payload)
    assert bare_result.status == "ok", bare_result.error
    assert "GENERATED BY fleet" in read(bare / "Cargo.lock")


def test_the_root_pnpm_lock_declares_no_carry_so_the_resolver_cannot_be_skipped() -> None:
    """The npm half of the rule above, inverted on purpose (ADR-0048).

    **Why this is an assertion and not a comment.** `cli._resolved_support_files` short-circuits
    on `carry_from` — a repo that ships the file gets it verbatim and **no resolver runs at
    all**. That is right for a lock that resolves one package's dependency graph and wrong for a
    pnpm workspace lock, which resolves N importers' graphs together: promoting one repo's
    single-importer lock to the monorepo root describes a monorepo that does not exist, and does
    it silently, because the second repo's `//<dest>:node_modules/<pkg>` labels then name
    packages the root lock never mentions. So `carry_from` must stay EMPTY here, and a
    well-meaning "carry it if the repo has one" is a one-line regression that no build-level test
    would attribute back to this file.

    `pnpm-workspace.yaml` and `.bazelignore` are the same case one step further out: their
    content is computed from the fleet's importer list, so a carried copy of either would be one
    repo's old layout at its old paths.
    """
    ecosystems.discover()
    adapter = ecosystems.for_ecosystem(Ecosystem.NPM)
    unit = BuildUnit(
        unit_id=REPO,
        ecosystem=Ecosystem.NPM,
        dest="ts/acme/widget",
        srcs=["index.ts"],
        published=Coordinate(ecosystem=Ecosystem.NPM, group="@acme", name="widget"),
        external_coordinates=[Coordinate(ecosystem=Ecosystem.NPM, name="left-pad")],
    )
    files = {f.path: f for f in adapter.workspace_files([unit])}
    assert set(files) == {"pnpm-lock.yaml", "pnpm-workspace.yaml", ".bazelignore"}
    for path, support in sorted(files.items()):
        assert support.carry_from == [], f"{path} would let the carry short-circuit skip a resolve"
    plan = adapter.resolution([unit])
    assert plan is not None and plan.lock_path == "pnpm-lock.yaml"


def test_the_root_go_sum_declares_no_carry_so_the_resolver_cannot_be_skipped() -> None:
    """The Go half of the same rule, and the `go.mod` beside it as the counter-example (ADR-0050).

    **Why the JS rule and not the Python one.** `py.py` lets one contributing repo's
    `requirements.lock` be carried, on the grounds that its resolution covers exactly the set it
    must; ADR-0050 measured that reasoning against Go and it does not transfer. A `go.sum` is a
    list of content hashes valid **only** against the `go.mod` sitting beside it, and the root
    `go.mod` is a fleet-owned file. Carrying one repo's sums next to another repo's `go.mod`
    would be missing hashes for what that `go.mod` requires and stale for what it does not —
    which manufactures a **checksum mismatch**, a failure that reads as a supply-chain
    compromise. Adopting the Python rule would work at one contributor and became a silent
    regression the moment the `go.mod` union landed, because the carried sums would then cover a
    strict subset of the unioned requires.

    `carry_from == []` is also load-bearing mechanically, exactly as it is for the pnpm lock
    above: `cli._carried` short-circuits the resolver for any file with a real candidate behind
    it, so a `carry_from` here would skip `go mod download all` entirely.

    **The `go.mod` beside it now declares no carry either, and that is the ADR-0050 step 2
    change.** It used to be carried, on the grounds that Go's MVS had already run into the repo's
    own file — true of one repo and false of a fleet: the root `go.mod` is the monorepo's own
    module over every Go repo's requirements, and no single repo's file is that. Both root files
    are fleet-owned; the repo's own `go.mod` is still at `<dest>/go.mod` where Phase 2 left it.
    """
    ecosystems.discover()
    adapter = ecosystems.for_ecosystem(Ecosystem.GO)
    unit = BuildUnit(
        unit_id=REPO,
        ecosystem=Ecosystem.GO,
        dest="go/commons",
        srcs=["commons.go"],
        published=Coordinate(ecosystem=Ecosystem.GO, group="github.com/acme", name="commons"),
        external_coordinates=[
            Coordinate(
                ecosystem=Ecosystem.GO,
                group="github.com/stretchr",
                name="testify",
                version_spec="v1.9.0",
            )
        ],
    )
    files = {f.path: f for f in adapter.workspace_files([unit])}
    assert set(files) == {"go.mod", "go.sum"}
    assert files["go.sum"].carry_from == [], "a carry here skips the resolve entirely"
    assert files["go.sum"].content == "", (
        "an `h1:` line is a hash of a module zip, so a synthesized floor is either absent or "
        "WRONG, and a wrong sum fails as a security check"
    )
    assert files["go.mod"].carry_from == [], "the fleet's root module is no repo's own go.mod"

    plan = adapter.resolution([unit])
    assert plan is not None
    assert plan.lock_path == "go.sum"
    assert plan.argv == ["go", "mod", "download", "all"], (
        "`go mod tidy` deletes the require block and writes no go.sum in a scratch dir with no "
        "sources; bare `go mod download` writes only the /go.mod hashes and no h1: zip hash, "
        "which is non-empty enough to pass the driver's empty-lock guard and still unusable"
    )
    # The resolver reads the SAME bytes that land at `//:go.mod` — not a carried file, and not a
    # second rendering. A `go.sum` is a set of hashes taken against one exact `go.mod`, so an
    # input that differs from what lands is a checksum mismatch inside Bazel in which every
    # individual hash is correct. This replaces the carry-only input the seam shipped with, which
    # ADR-0050 fact 3 chose because the OLD synthesized text rendered `v0.0.0` and `left-pad
    # ^1.3.0`; `_require_line` now refuses to write either and names the coordinate instead.
    (source,) = plan.inputs
    assert source.path == "go.mod"
    assert source.carry_from == []
    assert source.content == files["go.mod"].content
    assert "require (\n\tgithub.com/stretchr/testify v1.9.0\n)" in source.content, source.content


def test_the_go_sum_is_resolved_against_the_union_go_mod_that_actually_lands() -> None:
    """With two Go units, the `go.mod` the resolver reads IS the union that lands at the root —
    both repos' requirements, byte for byte, in either argument order.

    **Why this is the assertion, and why it changed.** The resolver's input used to be *carry-only*
    — one unit's relocated `go.mod`, picked by re-deriving `union_workspace_files`' first-writer
    sort — because ADR-0050 fact 3 measured the old synthesized text and found it unusable
    (`v0.0.0`, `left-pad ^1.3.0`). That was consistent only while the root file was also one
    repo's. Once the root is a union, the carry-only input and the union are in direct conflict:
    the sums would be hashes of repo A's module graph written beside a `go.mod` that also requires
    repo B's modules, so `sums_from_go_mod` finds no entry for half of what it must verify. Every
    hash in the file would be individually correct and the file as a whole would be wrong — the
    checksum mismatch the test above says the harness must never invent, arriving by a back door.
    Composing both from the same two pure functions is what makes the property hold by
    construction instead of by a selection rule someone has to keep in step.

    Asserted in BOTH argument orders, because a resolver whose answer depends on the order the
    driver happened to group the units in is the §11.6 defect regardless of which answer it gives.
    """
    ecosystems.discover()
    adapter = ecosystems.for_ecosystem(Ecosystem.GO)

    def _go_unit(name: str, dest: str, module: str, version: str) -> BuildUnit:
        return BuildUnit(
            unit_id=name,
            ecosystem=Ecosystem.GO,
            dest=dest,
            srcs=["main.go"],
            published=Coordinate(ecosystem=Ecosystem.GO, group="github.com/acme", name=name),
            external_coordinates=[
                Coordinate(
                    ecosystem=Ecosystem.GO,
                    group="github.com/stretchr",
                    name=module,
                    version_spec=version,
                )
            ],
        )

    first = _go_unit("commons", "go/commons", "testify", "v1.9.0")
    second = _go_unit("server", "go/server", "objx", "v0.5.2")
    rendered: set[str] = set()
    for units in ([first, second], [second, first]):
        merged = {f.path: f for f in adapter.workspace_files(units)}
        plan = adapter.resolution(units)
        assert plan is not None
        (source,) = plan.inputs
        assert source.carry_from == merged["go.mod"].carry_from == [], (
            "one repo's go.mod is not the fleet's, and a carry would skip the resolve entirely"
        )
        assert source.content == merged["go.mod"].content, (
            "the sums would hash a module graph that never reaches the monorepo root"
        )
        for module in ("github.com/stretchr/testify v1.9.0", "github.com/stretchr/objx v0.5.2"):
            assert f"\t{module}" in source.content, (
                f"{module} is absent from the file the sums are taken against: {source.content}"
            )
        rendered.add(source.content)
    assert len(rendered) == 1, f"the union depends on the argument order: {rendered}"


async def test_the_root_cargo_manifest_on_disk_is_a_workspace_naming_both_rust_repos(
    tmp_path,
) -> None:
    """Two Rust repos through the REAL `BuildgenWorker`: the bytes at `<root>/Cargo.toml` are a
    `[workspace]` whose `members` holds both dests, and each member's own manifest is on disk.

    **Why this is asserted off the filesystem and not off the adapter.** The defect was never
    visible in a return value: `_workspace_manifest` was rendered per unit and
    `union_workspace_files`' `setdefault` dropped the loser, so the *file that got written* named
    one crate while both repos' `crate.from_cargo` tags pointed at it — and because
    `_fleet_support_files` hands every plan of an ecosystem the identical tuple, no conflict check
    anywhere could see two candidates. The only place the loss was observable was the tree, which
    is where this reads it.

    The member half is asserted here too, because the two are one decision: a `members` entry
    naming a directory with no loadable `Cargo.toml` fails the WHOLE workspace in `cargo metadata`,
    so unioning the members without writing the manifests would trade a silent drop for a hard
    failure. A repo that shipped its own manifest keeps it byte for byte (the carry is the path
    itself, where relocation already put it); a repo that shipped none gets the `[package]` floor.

    **What this does NOT prove:** no `cargo metadata` runs, nothing is spliced, no crate is
    fetched and no Rust is compiled — cargo is not installed on this host. It proves the bytes
    this harness writes, which is the half this harness owns.
    """
    ecosystems.discover()
    rust = ecosystems.for_ecosystem(Ecosystem.CARGO)
    store = _rust_plan_unit("acme-store", "rust/acme-store", "serde")
    core = _rust_plan_unit("acme-core", "rust/acme-core", "anyhow")
    for unit in (store, core):
        _source_tree(tmp_path, unit.dest)
    shipped = '[package]\nname = "acme-store"\nversion = "3.1.4"\nedition = "2018"\n'
    (tmp_path / store.dest / "Cargo.toml").write_text(shipped, encoding="utf-8")

    # The driver computes the root files ONCE per ecosystem over BOTH units and hands every plan
    # the same tuple (`_fleet_support_files`); the package files stay per unit.
    shared = rust.workspace_files([store, core])
    for unit in (store, core):
        support = _resolve_support_files(
            tmp_path, [*shared, *rust.package_files(unit)]
        )
        result = await BuildgenWorker().run(
            make_ctx(tmp_path),
            BuildgenInput(
                unit=unit,
                targets=[*rust.generate_targets(unit), *rust.test_targets(unit)],
                gazelle=rust.gazelle_config(unit),
                workspace_deps=rust.workspace_deps(unit),
                toolchains=rust.toolchain_requirements(),
                support_files=list(support),
                requirements=[
                    ExternalRequirement(
                        coord_key=c.key,
                        repo_id=str(unit.unit_id),
                        version_spec=c.version_spec or "1.0.0",
                    )
                    for c in unit.external_coordinates
                ],
                ruleset_versions=dict(BuildSection().ruleset_versions),
            ),
        )
        assert result.status == "ok", result.error

    root = read(tmp_path / "Cargo.toml")
    assert "[workspace]" in root and 'resolver = "2"' in root, root
    assert 'members = ["rust/acme-core", "rust/acme-store"]' in root, (
        "the second Rust repo is not in the workspace its own crate.from_cargo tag splices"
    )
    # Neither repo's own `[package]` manifest was promoted to the root: crate_universe's
    # `SplicerKind::new` dispatches on the `[workspace]` table, and a carried package manifest
    # sends it down the Package branch where only one crate's dependencies reach `@crates`.
    assert root != shipped and "GENERATED BY fleet" in root, root

    assert read(tmp_path / store.dest / "Cargo.toml") == shipped, (
        "a member that shipped a manifest keeps it — the carry is that path itself"
    )
    floor = read(tmp_path / core.dest / "Cargo.toml")
    assert "[package]" in floor and "[workspace]" not in floor, floor
    assert 'name = "acme_core"' in floor and 'edition = "2021"' in floor, floor


async def test_a_derived_requirements_file_is_built_from_what_phase_1_recorded(
    tmp_path,
) -> None:
    """Python's lock is the one that is legitimately DERIVED: Phase 1 already recorded every
    declared dependency and its spec, and a requirements file is that list written out.

    **Why:** `pip.parse` needs *a* file, and for Python the harness genuinely has the input to
    produce one — so a repo shipping no lock still gets a hub containing the distributions it
    actually declared, rather than a `pip.parse` pointed at nothing. A real `requirements.txt`
    still outranks it, because a resolution is a fact and a reconstruction is an inference.
    """
    ecosystems.discover()
    adapter = ecosystems.for_ecosystem(Ecosystem.PYPI)
    dest = f"{adapter.monorepo_dir}/acme/widget"
    _source_tree(tmp_path, dest)
    payload, unit = _adapter_payload(tmp_path, Ecosystem.PYPI, dest)
    assert await BuildgenWorker().run(make_ctx(tmp_path), payload) is not None

    derived = read(tmp_path / "requirements.lock")
    declared = unit.external_coordinates[0]
    assert f"{declared.name}{declared.version_spec}" in derived, derived
    assert "GENERATED BY fleet" in derived, "a derived lock must never pass for a real one"

    # A real requirements.txt in the tree wins over the derivation, without any change of shape.
    real = "requests==2.31.0 --hash=sha256:ab\n"
    (tmp_path / dest / "requirements.txt").write_text(real, encoding="utf-8")
    (carried,) = _resolve_support_files(tmp_path, adapter.workspace_files([unit]))
    assert carried.content == real


# -- wiring gap 1: the D6 mechanism is actually reached ---------------------------------


async def test_buildgen_passes_targets_so_a_load_only_ruleset_gets_its_bazel_dep(
    tmp_path,
) -> None:
    """D6's fix is `render_module_bazel(targets=…)` deriving a `bazel_dep` from a target's
    `load_from`, and this asserts the WORKER hands it over.

    **Why:** the derivation was written, tested at the renderer, and never reached from
    production — `workers/buildgen.py` omitted the argument, so the only `bazel_dep`s a real run
    ever emitted were the ones a `WorkspaceDep` or a `ToolchainRequirement` declared. A package
    with no external npm coordinates whose `js_binary` loads `@aspect_rules_js//js:defs.bzl` got
    nothing, and Bazel answered `Unable to find package for @@[unknown repo 'aspect_rules_js']`.
    This project has already found four "the mechanism exists but nothing invokes it" defects;
    the assertion is therefore on a `bazel_dep` that ONLY a `load_from` could have produced —
    there is no workspace dep and no toolchain in this payload at all.
    """
    load_only = BuildgenInput(
        unit=_unit(dest="ts/acme/app"),
        targets=[
            BuildTarget(
                package="ts/acme/app",
                name="app_bin",
                rule="js_binary",
                load_from="@aspect_rules_js//js:defs.bzl",
            )
        ],
        workspace_deps=[],
        toolchains=[],
        ruleset_versions={"aspect_rules_js": "3.4.0"},
    )
    result = await BuildgenWorker().run(make_ctx(tmp_path), load_only)
    assert result.status == "ok", result.error
    assert result.output is not None
    module = read(result.output.module_bazel_path)
    assert 'bazel_dep(name = "aspect_rules_js", version = "3.4.0")' in module, module

    # The control: the identical payload with the load stripped emits no such dep, so the line
    # above cannot be satisfied by anything except the target's `load_from`.
    stripped = load_only.model_copy(
        update={"targets": [load_only.targets[0].model_copy(update={"load_from": None})]}
    )
    control = await BuildgenWorker().run(make_ctx(tmp_path / "control"), stripped)
    assert control.status == "ok"
    assert control.output is not None
    assert "aspect_rules_js" not in read(control.output.module_bazel_path)


def test_the_driver_hands_buildgen_the_fleets_load_labels_and_its_support_files() -> None:
    """The production path: `_module_inputs` collects the fleet's `load_from` labels and root
    files, and `_buildgen_input` forwards both.

    **Why:** the worker-level test above proves the argument is used; this proves it is supplied,
    which is the half that was missing. MODULE.bazel is ONE file every dispatch of a wave writes
    and merges, so it is fed the FLEET's targets and not this repo's — feeding it per-repo targets
    would make two repos render two different root files and conflict on the integration branch.
    """
    ecosystems.discover()
    js = ecosystems.for_ecosystem(Ecosystem.NPM)
    dest = f"{js.monorepo_dir}/acme/widget"
    unit = BuildUnit(
        unit_id="acme-ui",
        ecosystem=Ecosystem.NPM,
        dest=dest,
        srcs=["index.ts"],
        published=Coordinate(ecosystem=Ecosystem.NPM, group="@acme", name="ui"),
        external_coordinates=[Coordinate(ecosystem=Ecosystem.NPM, name="left-pad")],
    )
    plan = _a_build_plan(unit, js)
    deps, toolchains, requirements, module_targets, root_files = _module_inputs({"acme-ui": plan})
    assert {t.load_from for t in module_targets} == {
        t.load_from for t in plan.targets if t.load_from is not None
    }
    declared = sorted(f.path for f in plan.workspace_files)
    assert declared, "the JS adapter's root files are what this assertion is made of"
    # The adapter's root files, PLUS the root package that turns their `//:` names into labels.
    # Materialising `pnpm-lock.yaml` was only half of D10: with no `BUILD.bazel` at the monorepo
    # root, `//:pnpm-lock.yaml` is not a label at all and real Bazel reports "Unable to load
    # package for //:… : BUILD file not found" for a file that is sitting right there.
    assert [f.path for f in root_files] == sorted([*declared, "BUILD.bazel"])
    root_package = next(f for f in root_files if f.path == "BUILD.bazel")
    for path in declared:
        assert f'"{path}"' in root_package.content, (path, root_package.content)
    # …and the RULES the adapter needs in that package, not only the exports. The pnpm virtual
    # store every `node_modules/<pkg>` link resolves THROUGH is emitted under `if is_root:`, so a
    # root package that exported the lock but skipped the macro would answer every external label
    # with "no such target '//:.aspect_rules_js/node_modules/left-pad@1.3.0'".
    for target in plan.root_targets:
        assert f"{target.rule}(" in root_package.content, root_package.content
        assert target.load_from is not None
        assert f'load("{target.load_from}"' in root_package.content, root_package.content
    # The LINKS, though, are per pnpm importer (ADR-0048): with one importer per JS repo the
    # labels this repo depends on are declared by the macro in this repo's OWN package, not at
    # the root — so the macro and the labels that resolve through it are still one decision, and
    # the package they agree on is `unit.dest`.
    importer_macros = [
        t for t in plan.targets if t.rule == "npm_link_all_packages" and t.package == unit.dest
    ]
    assert [t.name for t in importer_macros] == ["node_modules"], plan.targets
    assert {f"//{unit.dest}:{t.name}" for t in importer_macros} == {
        label.rsplit("/", maxsplit=1)[0]
        for label in js.external_labels(unit)
    }, "the importer macro and the labels that resolve through it are one decision"
    assert deps and toolchains is not None and requirements is not None

    payload = _a_build_input(plan, module_targets, [*root_files, *plan.package_files])
    forwarded = BuildPipelineWorker()._buildgen_input(payload)
    assert forwarded.module_targets == module_targets, "the D6 derivation has nothing to read"
    assert [f.path for f in forwarded.support_files] == [
        f.path for f in payload.support_files
    ], "D10: the files MODULE.bazel names never reach the worker that writes them"


def test_two_repos_offering_one_root_path_with_the_same_bytes_dedupe_quietly() -> None:
    """The normal case after ADR-0048: every plan of one ecosystem carries the identical tuple.

    **Why it is asserted rather than assumed.** `_fleet_support_files` computes the root files
    once per ecosystem and hands the same objects to every plan of it, so the union below has
    nothing to decide — and the test that follows this one turns *divergence* into a hard error.
    Without this control that error would be indistinguishable from "the union now rejects the
    ordinary two-JS-repo fleet", which is the shape of over-correction that would break every
    multi-repo build.
    """
    ecosystems.discover()
    js = ecosystems.for_ecosystem(Ecosystem.NPM)
    shared = js.workspace_files(
        [_js_plan_unit("acme-app", "ts/acme/app"), _js_plan_unit("acme-report", "ts/acme/report")]
    )
    plans = {
        "acme-app": replace(
            _a_build_plan(_js_plan_unit("acme-app", "ts/acme/app"), js),
            workspace_files=tuple(shared),
        ),
        "acme-report": replace(
            _a_build_plan(_js_plan_unit("acme-report", "ts/acme/report"), js),
            workspace_files=tuple(shared),
        ),
    }
    *_rest, root_files = _module_inputs(plans)
    assert sorted(f.path for f in root_files) == sorted(
        {*(f.path for f in shared), "BUILD.bazel"}
    )
    # One entry per path, whatever the fleet's size — the whole point of "the root holds ONE file
    # per path".
    assert len({f.path for f in root_files}) == len(root_files)


def test_two_repos_offering_one_root_path_with_different_bytes_is_a_loud_failure() -> None:
    """ADR-0048's defect, at the exact line that used to hide it (Rule 11).

    **Why this is a hard error and not a tie-break.** `_module_inputs` unioned the fleet's root
    files with `support.setdefault(file.path, file)` over `sorted(plans)`, so of two competing
    `//:pnpm-lock.yaml` resolutions the lower `repo_id`'s was kept and the other repo's external
    dependency was dropped from the monorepo **silently** — real Bazel then answered `no such
    target '//ts/acme/report:node_modules/ms'` while `fleet build` had exited 0, and nothing
    re-admits a settled wave to notice. Two candidates for one root path is never a preference;
    it is a root file that was computed per repo while describing the whole fleet. The message
    has to name the path AND every contributing repo, because the operator's next question is
    "which repos disagree" and a winner-picking union answers it with a diff between two runs.
    """
    ecosystems.discover()
    js = ecosystems.for_ecosystem(Ecosystem.NPM)
    app = _js_plan_unit("acme-app", "ts/acme/app")
    report = _js_plan_unit("acme-report", "ts/acme/report")
    plans = {
        # Each repo answering "what is at `//:pnpm-lock.yaml`" from inside its own unit — which
        # is precisely what the per-repo resolve used to do.
        "acme-app": replace(
            _a_build_plan(app, js), workspace_files=tuple(js.workspace_files([app]))
        ),
        "acme-report": replace(
            _a_build_plan(report, js), workspace_files=tuple(js.workspace_files([report]))
        ),
    }
    with pytest.raises(RootFileConflictError) as caught:
        _module_inputs(plans)
    assert caught.value.path in {"pnpm-workspace.yaml", ".bazelignore"}, caught.value.path
    assert set(caught.value.repo_ids) == {"acme-app", "acme-report"}, caught.value.repo_ids
    assert "acme-app" in str(caught.value) and "acme-report" in str(caught.value)


def test_two_rust_repos_each_computing_the_root_workspace_is_a_loud_failure() -> None:
    """The same guard, stated for Rust: two plans disagreeing on `//:Cargo.toml` raises.

    **What it proves for Rust TODAY.** `_fleet_support_files` computes the root files once per
    ecosystem, so in production both Rust plans carry the identical `Cargo.toml` and this error
    cannot fire — which is exactly why the per-unit manifest was lost *silently* and why no
    existing test caught it. This constructs the per-unit view by hand to prove the remaining
    property: the two views genuinely differ (`members` names one crate in each), so a Rust root
    file that ever again becomes a function of one repo is a hard error naming the path and both
    repos, not a coin toss resolved by `setdefault`. The union is not "now rejecting a two-Rust
    fleet" either — the control above (same bytes, quiet dedupe) is what separates the two, and
    the fleet-wide call that produces those same bytes is asserted on disk earlier in this file.

    Both Rust root paths diverge under a per-unit view and either may be the one reported —
    `Cargo.lock`'s `carry_from` names the offering unit's own `dest`, `Cargo.toml`'s `members`
    names the offering unit's crate — so the raise is asserted on the pair and the Rust-specific
    fact (the two manifests really are different files) is asserted directly beside it.
    """
    ecosystems.discover()
    rust = ecosystems.for_ecosystem(Ecosystem.CARGO)
    store = _rust_plan_unit("acme-store", "rust/acme-store", "serde")
    core = _rust_plan_unit("acme-core", "rust/acme-core", "anyhow")
    per_unit = [
        next(f for f in rust.workspace_files([unit]) if f.path == "Cargo.toml").content
        for unit in (store, core)
    ]
    assert per_unit[0] != per_unit[1], per_unit
    assert 'members = ["rust/acme-store"]' in per_unit[0], per_unit[0]
    assert 'members = ["rust/acme-core"]' in per_unit[1], per_unit[1]

    plans = {
        # Each repo answering "what is the monorepo's Cargo workspace" from inside its own unit —
        # which is precisely what the per-unit `_workspace_manifest` used to do.
        "acme-store": replace(
            _a_build_plan(store, rust), workspace_files=tuple(rust.workspace_files([store]))
        ),
        "acme-core": replace(
            _a_build_plan(core, rust), workspace_files=tuple(rust.workspace_files([core]))
        ),
    }
    with pytest.raises(RootFileConflictError) as caught:
        _module_inputs(plans)
    assert caught.value.path in {"Cargo.toml", "Cargo.lock"}, caught.value.path
    assert set(caught.value.repo_ids) == {"acme-store", "acme-core"}, caught.value.repo_ids
    assert "acme-store" in str(caught.value) and "acme-core" in str(caught.value)


def _rust_plan_unit(unit_id: str, dest: str, dep: str) -> BuildUnit:
    """A Rust crate with one crates.io dependency of its own — the fixture the Rust tests need."""
    return BuildUnit(
        unit_id=unit_id,
        ecosystem=Ecosystem.CARGO,
        dest=dest,
        srcs=list(_POLYGLOT_SRCS),
        published=Coordinate(ecosystem=Ecosystem.CARGO, name=dest.rsplit("/", maxsplit=1)[-1]),
        external_coordinates=[
            Coordinate(ecosystem=Ecosystem.CARGO, name=dep, version_spec="1.0")
        ],
    )


def _js_plan_unit(unit_id: str, dest: str) -> BuildUnit:
    """A JS unit with one registry dependency of its own — the fixture both tests above need."""
    return BuildUnit(
        unit_id=unit_id,
        ecosystem=Ecosystem.NPM,
        dest=dest,
        srcs=["index.ts"],
        published=Coordinate(
            ecosystem=Ecosystem.NPM, group="@acme", name=dest.rsplit("/", maxsplit=1)[-1]
        ),
        external_coordinates=[
            Coordinate(ecosystem=Ecosystem.NPM, name=f"dep-{unit_id}", version_spec="^1.0.0")
        ],
    )


# -- wiring gap 2: baseline_test_count reaches buildverify ------------------------------


async def test_a_repo_that_had_tests_and_lost_them_is_caught_through_the_real_input(
    tmp_path,
) -> None:
    """§12.11's green-and-empty failure, reached the way production reaches it.

    **Why:** `buildverify` distinguishes "this library never had tests" (Bazel exit 4, ordinary
    and not a failure) from "this migration DELETED the repo's tests" (exit 4 with a positive
    `repos.baseline_test_count`, a real defect) by comparing against a number the CLI has to pass
    it. `_buildverify_input` did not pass it, so every repo in the fleet arrived carrying the
    default 0 and the second case was indistinguishable from the first — a `filegroup` emitted
    where a `*_test` belonged would build clean, "test" clean at zero targets, and ship.
    """
    (tmp_path / "java/com/acme/widget").mkdir(parents=True)
    (tmp_path / "java/com/acme/widget/BUILD.bazel").write_text("", encoding="utf-8")
    build_payload = _a_verify_pipeline_input(baseline=7)
    verify_input = BuildPipelineWorker()._buildverify_input(build_payload)
    assert verify_input.baseline_test_count == 7, "the number never left the CLI"

    runner = RecordingRunner(
        [
            (
                lambda p: p[1] == TEST_UNIT,
                lambda parts: ProcResult(
                    argv=parts,
                    exit_code=NO_TESTS_FOUND,
                    stdout_tail="",
                    stderr_tail="ERROR: No test targets were found, yet testing was requested",
                    duration_ms=3,
                    timed_out=False,
                ),
            )
        ]
    )
    lost = await BuildverifyWorker(runner=runner).run(make_ctx(tmp_path), verify_input)
    assert lost.status == "failed", "a migration that deleted a repo's whole suite is a defect"
    assert lost.output is not None and lost.output.tests_lost is True
    assert lost.output.green is False

    # The control: the SAME exit code on a repo that never had tests is not a failure at all.
    never = await BuildverifyWorker(runner=runner).run(
        make_ctx(tmp_path),
        BuildPipelineWorker()._buildverify_input(_a_verify_pipeline_input(baseline=0)),
    )
    assert never.status == "ok"
    assert never.output is not None and never.output.tests_lost is False


# -- wiring gap 3: the rdeps test run is classified off buildverify's table --------------


def _rdeps_runner(exit_code: int) -> RecordingRunner:
    return RecordingRunner(
        [
            (lambda p: p[1] == "query", ok("//pkg0000:lib\n//pkg0001:lib\n")),
            (
                lambda p: p[1] == "test",
                lambda parts: ProcResult(
                    argv=parts,
                    exit_code=exit_code,
                    stdout_tail="",
                    stderr_tail="FAIL: //pkg0000:lib_test",
                    duration_ms=4,
                    timed_out=False,
                ),
            ),
        ]
    )


async def test_an_rdeps_test_failure_is_classified_as_a_test_failure(tmp_path) -> None:
    """A failing test in the blast radius is a `TEST_FAILURE`, not a `BUILD_ERROR`.

    **Why:** `classify_build_failure` keys the distinction on the step that failed, and it
    recognizes exactly one test step name. `rdepverify` passed its own unit name (`rdeps_test`),
    so every Phase 4 blast-radius failure fell through to `BUILD_ERROR` — a different §13 row and
    a different repair prompt, which sent the ladder off to regenerate a `BUILD.bazel` that was
    never the problem. The exit-code table lives in `buildverify.py` and is the authority; this
    worker now reads it instead of keeping a second, drifting copy.
    """
    result = await RdepverifyWorker(runner=_rdeps_runner(3)).run(
        make_ctx(tmp_path),
        RdepverifyInput(
            dest="java/com/acme/widget",
            integration_ref=SNAPSHOT,
            log_dir=str(tmp_path / "logs"),
        ),
    )
    assert result.status == "failed"
    assert result.error is not None
    assert result.error.failure_class is FailureClass.TEST_FAILURE, result.error
    assert result.error.retryable is True
    assert classify_build_failure(
        ProcResult(argv=(), exit_code=3, stdout_tail="", stderr_tail="", duration_ms=1,
                   timed_out=False),
        unit=TEST_UNIT,
    ) == (FailureClass.TEST_FAILURE, True), "the authority this worker now defers to"


async def test_no_tests_in_the_rdeps_closure_is_not_a_retryable_failure(tmp_path) -> None:
    """Bazel exit 4 over the verified target set means the closure holds no test target — not
    that a test failed, and not something a second attempt can change.

    **Why:** the same reasoning §3.3 already applies to a library with no tests of its own, one
    phase later. Treated as a retryable failure it burns all three ADR-0014 rungs and escalates a
    repo whose blast radius simply contains no tests to `REQUIRES_HUMAN_INTERVENTION`. It is
    disclosed rather than silently folded into a PASS, because a reviewer reading a green Phase 4
    over zero executed tests is entitled to know that is what happened.
    """
    result = await RdepverifyWorker(runner=_rdeps_runner(NO_TESTS_FOUND)).run(
        make_ctx(tmp_path),
        RdepverifyInput(
            dest="java/com/acme/widget",
            integration_ref=SNAPSHOT,
            log_dir=str(tmp_path / "logs"),
        ),
    )
    assert result.status == "ok", "nothing to run is not a failure, and a retry cannot fix it"
    assert result.error is None
    out = result.output
    assert out is not None
    assert out.rdeps_no_test_targets is True, "and it is DISCLOSED, not folded into the verdict"
    assert out.test_exit_code == NO_TESTS_FOUND
    assert out.report.rdeps_ok is True
    assert result.completed_units == [CLOSURE_UNIT, RDEPS_TEST_UNIT]


def _a_build_plan(unit: BuildUnit, adapter) -> _BuildPlan:
    """One repo's prepared Phase 3 plan, built the way `_plan_build` builds it: everything
    per-language read off the adapter, nothing spelled here."""
    return _BuildPlan(
        repo_id=str(unit.unit_id),
        dest=unit.dest,
        worktree=Path("/nonexistent"),
        integration_ref=SNAPSHOT,
        integration_sha="a" * 40,
        merge_sha="b" * 40,
        source_sha="c" * 40,
        already_ingested=False,
        unit=unit,
        targets=tuple(adapter.generate_targets(unit)),
        workspace_deps=tuple(adapter.workspace_deps(unit)),
        toolchains=tuple(adapter.toolchain_requirements()),
        workspace_files=tuple(adapter.workspace_files([unit])),
        package_files=tuple(adapter.package_files(unit)),
        root_targets=tuple(adapter.root_targets(unit)),
        requirements=(),
        gazelle=adapter.gazelle_config(unit),
        baseline_test_count=0,
        adapter_name=adapter.name,
        adapter_degraded=adapter.degraded,
    )


def _a_build_input(
    plan: _BuildPlan,
    module_targets: list[BuildTarget],
    support_files: list[SupportFile],
) -> BuildInput:
    return BuildInput(
        repo_id=plan.repo_id,
        dest=plan.dest,
        unit=plan.unit,
        targets=list(plan.targets),
        module_targets=module_targets,
        gazelle=plan.gazelle,
        workspace_deps=list(plan.workspace_deps),
        toolchains=list(plan.toolchains),
        support_files=support_files,
        requirements=list(plan.requirements),
        integration_ref=plan.integration_ref,
        integration_worktree="/nonexistent",
        lock_dir="/nonexistent",
    )


def _a_verify_pipeline_input(*, baseline: int) -> BuildInput:
    """The Phase 3 dispatch payload as `_build_payloads` assembles it, carrying the one column
    §12.11 needs and nothing else that matters here."""
    return BuildInput(
        repo_id=REPO,
        dest="java/com/acme/widget",
        unit=_unit(),
        integration_ref=SNAPSHOT,
        integration_worktree="/nonexistent",
        lock_dir="/nonexistent",
        baseline_test_count=baseline,
    )
