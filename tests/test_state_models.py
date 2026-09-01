"""Contract tests for the state layer (SPEC §5, `SCHEMA_VERSION = 8`).

CLAUDE.md Rule 9: every test here says *why* the logic matters. Each one guards a named bug —
either a failure mode the SPEC exists to prevent, or a defect the adversarial review found in an
earlier cut of these models. A test that only proves a field exists is deleted, not kept.

What is load-bearing:

1. `migration_state.json` survives a crash-safe round trip unchanged — and so does *every* other
   durable model, because each one is a checkpoint payload somewhere.
2. Evidence is TRUNCATED, never rejected (ADR-0030): a 400 KB stderr that fails validation is an
   attempt that never persists, an `attempts` counter that never increments, and a repair loop
   that re-runs the identical failing build forever.
3. The status machine is mechanical: the third failed attempt is terminal, and terminal means no
   automatic path back — only the audited operator re-open.
4. Edges are addressed by content (`edge_key`), never by rowid.
5. A green build against a stub is never reported as equivalent to a green build against the
   real graph — and the derivation does not recurse.
6. The attempt ceiling is POLICY (runtime, per-task), not a type constraint.
7. A `SUCCEEDED` repo carrying an unresolved stub is unrepresentable.
8. Invalid input is rejected at the boundary; on-disk state and LLM output are untrusted text
   until they validate.
"""

from __future__ import annotations

import inspect
import json
import logging
import re
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

import fleet.models as models_pkg
from fleet.models import enums as enums_mod
from fleet.models.base import LOG_TAIL_BYTES, FleetModel
from fleet.models.build import (
    BuildPlan,
    BuildTarget,
    BuildUnit,
    GazelleConfig,
    InternalDep,
    SupportFile,
    ToolchainRequirement,
    WorkspaceDep,
)
from fleet.models.enums import (
    ALLOWED_TRANSITIONS,
    EQUIVALENCE_RANK,
    OPERATOR_REOPEN,
    PHASE_DEMOTED_KIND,
    RESUME_DEMOTE,
    TERMINAL_STATUSES,
    BreakStrategy,
    ContextPolicy,
    ContractKind,
    ContractStatus,
    Ecosystem,
    EdgeKind,
    Equivalence,
    FailureClass,
    ModelTier,
    NodeKind,
    Phase,
    PhaseDemotion,
    PrState,
    RepoStatus,
    StructuredOutputMode,
    StubFidelity,
    StubState,
    SymbolKind,
    TaskKind,
    TransformTier,
    demote,
    transition,
)
from fleet.models.graph import (
    CollisionFinding,
    ContractNode,
    CycleFinding,
    DependencyEdge,
    GraphNode,
    MigrationWave,
    SymbolRef,
    edge_key_for,
)
from fleet.models.repo import Coordinate, ManifestRef, RawDependency, RepoRecord
from fleet.models.state import SCHEMA_VERSION, MigrationState, PhaseRecord, RepoState
from fleet.models.tasks import (
    DEFAULT_LADDER,
    MAX_ATTEMPTS,
    BackendTarget,
    BuildAttempt,
    FilePatch,
    LlmCallRecord,
    ModelCapabilities,
    Price,
    PullRequestDraft,
    RejectedApproach,
    StubRecord,
    TokenUsage,
    TransformResult,
    TransformTask,
    VerificationReport,
)
from fleet.workers.base import TIER_LADDER, WorkerContext

FROZEN = datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)
RUN = UUID("00000000-0000-4000-8000-000000000001")
HEX_A = "a" * 64
HEX_B = "b" * 64
SHA1 = "0" * 40
COORD = Coordinate(
    ecosystem=Ecosystem.MAVEN, group="com.acme", name="commons", version_spec="1.4.0"
)
CONTRACT_ID = "proto:acme.billing.v1"
SCC_ID = "scc:" + "0" * 16
STUB_COORD = "maven:com.acme:commons"
# The §6 `phases.lease_owner` format — '{host}:{container_id}:{pid}:{boot_uuid}'. Declared there
# and only there; a bare pid is not an identity, because fresh PID namespaces reuse low pids.
LEASE_OWNER = "worker-3:9f2c1ab4de77:42:0d9f6c1e-3b6a-4a0e-9c11-5f2b7d8e4a10"


# =======================================================================================
# (1) Truncation, not rejection (ADR-0030)
# =======================================================================================

_HEAD = "OLDEST-LINE-DROPPED"
_TAIL = "LAST-LINE-THAT-EXPLAINS-THE-FAILURE"
HUGE = _HEAD + ("x" * (400_000 - len(_HEAD) - len(_TAIL))) + _TAIL


def _attempt(**over: Any) -> BuildAttempt:
    base: dict[str, Any] = {
        "run_id": RUN, "repo_id": "acme-commons", "phase": Phase.BUILD, "attempt": 1,
        "command": ["bazel", "build", "//..."], "exit_code": 1, "duration_ms": 12,
        "started_at": FROZEN, "finished_at": FROZEN,
    }
    return BuildAttempt(**(base | over))


def _result(**over: Any) -> TransformResult:
    base: dict[str, Any] = {
        "task_id": RUN, "repo_id": "acme-commons", "attempt": 1,
        "tier": TransformTier.DETERMINISTIC, "ok": False, "finished_at": FROZEN,
    }
    return TransformResult(**(base | over))


TRUNCATION_FIELDS = [
    ("BuildAttempt.stderr_tail", lambda s: _attempt(stderr_tail=s).stderr_tail),
    ("BuildAttempt.stdout_tail", lambda s: _attempt(stdout_tail=s).stdout_tail),
    ("TransformResult.error", lambda s: _result(error=s).error),
    ("PhaseRecord.last_error", lambda s: PhaseRecord(phase=Phase.BUILD, last_error=s).last_error),
    ("RepoState.last_error", lambda s: RepoState(last_error=s).last_error),
]


@pytest.mark.parametrize(
    ("label", "put"), TRUNCATION_FIELDS, ids=[f[0] for f in TRUNCATION_FIELDS]
)
def test_oversized_evidence_is_truncated_not_rejected(label: str, put: Any) -> None:
    """A 400 KB Gradle stderr must VALIDATE and come back tail-truncated.

    Why it matters: `max_length` on an evidence field REJECTS the very failures worth recording.
    The attempt is then never persisted, `attempts` never increments, and the repair loop re-runs
    the identical failing build forever — Rule 11 inverted into a silent infinite loop. The bound
    belongs on what is *stored*, not on what is *accepted*.
    """
    kept = put(HUGE)

    assert kept is not None
    assert kept.endswith(f"[truncated {len(HUGE) - LOG_TAIL_BYTES} bytes]"), (
        f"{label}: truncation must announce itself, or a reader mistakes a tail for the whole log"
    )
    # The LAST bytes are what a human triages on: the traceback is at the end, not the start.
    assert _TAIL in kept
    assert _HEAD not in kept
    assert len(kept.encode()) < len(HUGE)


def test_evidence_under_the_cap_is_kept_verbatim() -> None:
    """Truncation must not touch an ordinary log line: a mangled short stderr would send the
    failure classifier (ADR-0014) down the wrong branch."""
    short = "ERROR: /w/BUILD:3:1: no such target '//libs:commons'"
    assert _attempt(stderr_tail=short).stderr_tail == short


# =======================================================================================
# (2) Every model round-trips through its own JSON
# =======================================================================================


def _sample_state() -> MigrationState:
    return MigrationState(
        run_id=RUN,
        started_at=FROZEN,
        updated_at=FROZEN,
        config_sha256=HEX_A,
        harness_version="0.1.0",
        repos={"acme-commons": RepoState(phase=Phase.TRANSFORM, updated_at=FROZEN)},
        waves=[MigrationWave(wave_index=0, repo_ids=["acme-commons"], computed_at=FROZEN)],
        budget_remaining_usd=123.45,
    )


#: One valid instance per exported model. The registry test below fails loudly when a model is
#: exported without an entry here, so a model added later cannot slip through uncovered.
SAMPLES: dict[str, FleetModel] = {
    "FleetModel": FleetModel(),
    # ---- §5.2 inventory ----
    "Coordinate": COORD,
    "RawDependency": RawDependency(
        raw_id="com.acme:commons:1.4.0", version_spec="1.4.0", scope="compile", source_line=12
    ),
    "ManifestRef": ManifestRef(
        manifest_id=1, repo_id="acme-commons", path="pom.xml", ecosystem=Ecosystem.MAVEN,
        adapter="maven", adapter_version=1, sha256=HEX_A, publishes=COORD,
        dependency_count=3, parsed_at=FROZEN,
    ),
    "RepoRecord": RepoRecord(
        repo_id="acme-commons", name="commons", url="https://git.invalid/acme/commons.git",
        head_sha=SHA1, ecosystems=[Ecosystem.MAVEN], primary_coordinate=COORD,
        dest_path="libs/com/acme/commons", kind="library", size_bytes=10, commit_count=2,
        last_commit_at=FROZEN, cloned_at=FROZEN, blast_radius=7, updated_at=FROZEN,
    ),
    # ---- §5.3 graph ----
    "GraphNode": GraphNode(kind=NodeKind.CONTRACT, node_id=CONTRACT_ID),
    "DependencyEdge": DependencyEdge(
        edge_id=17, edge_key=HEX_A, src_id="acme-portal", dst_coordinate=COORD,
        dst_id="acme-commons", kind=EdgeKind.DECLARED_DEP, base_confidence=1.0, confidence=0.9,
        confidence_factors={"open_range": 0.9}, evidence_path="pom.xml", evidence_line=42,
        detected_at=FROZEN,
    ),
    "SymbolRef": SymbolRef(
        symbol_id=3, repo_id="acme-commons", fqn="com.acme.Commons", kind=SymbolKind.CLASS,
        path="src/Commons.java", line=9, language="java", is_definition=True, exported=True,
    ),
    "ContractNode": ContractNode(
        contract_id=CONTRACT_ID, kind=ContractKind.PROTO, identifier="acme.billing.v1",
        owning_repo_id="acme-billing",
        source_paths=[{"repo_id": "acme-billing", "path": "proto/b.proto", "blob_sha": SHA1}],
        consumer_repo_ids=["acme-portal"], extractable=True, extraction_confidence=0.9,
        confidence_factors={"divergent": 0.5}, content_sha256=HEX_B,
        hoist_target_path="contracts/acme/billing/v1", status=ContractStatus.HOISTED,
        detected_at=FROZEN,
    ),
    "CycleFinding": CycleFinding(
        scc_id=SCC_ID, members=["acme-commons", "acme-portal"], edges=[HEX_A, HEX_B],
        feedback_edge_keys=[HEX_B], broken_edge_keys=[HEX_B],
        break_strategy=BreakStrategy.EDGE_BREAK, rationale="cheapest INTERNAL_IMPORT edge",
    ),
    "CollisionFinding": CollisionFinding(
        collision_id=2, kind="COORDINATE", key=STUB_COORD,
        repo_ids=["acme-commons", "acme-legacy"], severity="error", detected_at=FROZEN,
    ),
    "MigrationWave": MigrationWave(
        wave_index=1, repo_ids=["acme-portal"], contract_ids=[CONTRACT_ID],
        depends_on_waves=[0], atomic_scc_ids=[SCC_ID], synthetic=True,
        wave_started_at=FROZEN, computed_at=FROZEN,
    ),
    # ---- §5.4 work ----
    "TokenUsage": TokenUsage(
        role="transform_repair", tier=ModelTier.WORKHORSE, backend="openai_compatible",
        model_id="qwen3", input_tokens=10, output_tokens=5, cache_read_tokens=2, cost_usd=0.01,
    ),
    "ModelCapabilities": ModelCapabilities(
        supports_tools=True, supports_json_schema=True, max_context=32_768,
        structured_output_modes=(StructuredOutputMode.TOOL_CALL, StructuredOutputMode.PROMPTED),
    ),
    "Price": Price(in_per_mtok=3.0, out_per_mtok=15.0),
    "BackendTarget": BackendTarget(
        backend="openai_compatible", model_id="qwen3", base_url="http://localhost:8000/v1",
        api_key_env="OPENAI_API_KEY", effort="high",
        price=Price(in_per_mtok=3.0, out_per_mtok=15.0),
        capabilities_override={"max_context": 32_768}, weight=50,
    ),
    "TransformTask": TransformTask(
        task_id=RUN, run_id=RUN, repo_id="acme-commons", phase=Phase.TRANSFORM,
        kind=TaskKind.REWRITE, target_paths=["src/Commons.java"], rule_ids=["java-imports"],
        dest_path="libs/com/acme/commons", pre_commit_sha=SHA1, created_at=FROZEN,
    ),
    "FilePatch": FilePatch(
        path="src/Commons.java", diff="--- a\n+++ b\n@@ -1 +1 @@\n-import x;\n+import y;",
        tier=TransformTier.LLM_REPAIR, parse_probe_ok=True, rule_id="java-imports",
    ),
    "RejectedApproach": RejectedApproach(
        approach_signature=HEX_A, reason="rewrote the import root; symbol still unresolved",
        failure_class=FailureClass.RULE_MISS, attempt=2, tier=TransformTier.LLM_REPAIR,
    ),
    "TransformResult": _result(
        revalidation_round=1, context_policy=ContextPolicy.EVIDENCE_ONLY,
        patches=[FilePatch(path="a.java", diff="--- a\n+++ b", tier=TransformTier.DETERMINISTIC,
                           parse_probe_ok=True)],
        approach_signature=HEX_A,
        rejected_approaches=[RejectedApproach(
            approach_signature=HEX_B, reason="anchored on the prior diff",
            failure_class=FailureClass.ANCHORED_REPEAT, attempt=2, tier=TransformTier.LLM_REPAIR,
        )],
        anchored=True, reasks=1, patch_id=HEX_B, commit_sha=SHA1, files_changed=4,
        unresolved_files=["src/Other.java"], failure_class=FailureClass.RULE_MISS,
        error="ast-grep: rule miss on 4 files", duration_ms=980,
    ),
    "BuildAttempt": _attempt(
        attempt_id=RUN, phase=Phase.VERIFY, revalidation_round=1,
        integration_ref="refs/fleet/integration/3", context_policy=ContextPolicy.EVIDENCE_ONLY,
        approach_signature=HEX_A, container_id="c-1", worktree_path="/w/acme-commons",
        stdout_tail="INFO: Build completed", stderr_tail="ERROR: 1 target failed",
        log_path="artifacts/logs/run/attempt.log", failure_class=FailureClass.BUILD_ERROR,
    ),
    "StubRecord": StubRecord(
        stub_id=RUN, run_id=RUN, coord_key=STUB_COORD, provider_repo_id="acme-commons",
        consumer_repo_ids=["acme-portal"], fidelity=StubFidelity.PUBLISHED_ARTIFACT,
        pinned_version="1.4.0", state=StubState.SUPERSEDED, max_revalidation_rounds=2,
        rounds_spent=1, created_at=FROZEN, state_changed_at=FROZEN,
    ),
    "VerificationReport": VerificationReport(
        run_id=RUN, repo_id="acme-portal", build_ok=True, test_ok=True,
        rdeps_query="rdeps(//..., //libs/com/acme/commons:commons)", rdeps_target_count=120,
        rdeps_tested=100, rdeps_ok=True, rdeps_truncated=True, attempt_ids=[RUN],
        verdict="PASS", verified_against_stubs=[STUB_COORD],
        stub_fidelity={STUB_COORD: StubFidelity.PUBLISHED_ARTIFACT},
        revalidation_round=1, generated_at=FROZEN,
    ),
    "PullRequestDraft": PullRequestDraft(
        run_id=RUN, repo_id="acme-portal", contract_id=CONTRACT_ID, scc_id=SCC_ID,
        member_repo_ids=["acme-portal", "acme-commons"], wave_index=1, branch="migrate/acme-portal",
        title="Migrate acme-portal", body="Stub-limited: see banner.",
        depends_on_repos=["acme-commons"], depends_on_prs=["https://git.invalid/pr/1"],
        stubbed_deps=[STUB_COORD], equivalence=Equivalence.STUB_LIMITED,
        unresolved_stub_states={STUB_COORD: StubState.ACTIVE}, revalidation_round=1,
        weak_edges=[HEX_A], source_url="https://git.invalid/acme/portal.git", source_sha=SHA1,
        state=PrState.DRAFTED, created_at=FROZEN,
    ),
    "LlmCallRecord": LlmCallRecord(
        cache_key=HEX_A, prompt_template_version=2, role="transform_repair",
        tier=ModelTier.WORKHORSE, backend="openai_compatible", model_id="qwen3",
        structured_output_mode=StructuredOutputMode.TOOL_CALL, effort="medium",
        context_policy=ContextPolicy.EVIDENCE_ONLY, rejected_approach_digest=HEX_B,
        prompt_sha256=HEX_A, response_schema_sha256=HEX_B, response_json='{"ok": true}',
        hit_count=3, created_at=FROZEN,
    ),
    # ---- §5.5 projection ----
    "PhaseRecord": PhaseRecord(
        phase=Phase.TRANSFORM, status=RepoStatus.RUNNING, attempts=2, transient_retries=1,
        failure_class=FailureClass.BUILD_ERROR, last_error="1 target failed",
        heartbeat_at=FROZEN, heartbeat_ttl_seconds=120, lease_owner=LEASE_OWNER, lease_fence=4,
        lease_expires_at=FROZEN, started_at=FROZEN, base_ref="refs/fleet/r/acme/phase-2/base",
        pre_commit_sha=SHA1, post_commit_sha="1" * 40, updated_at=FROZEN,
    ),
    "RepoState": RepoState(
        phase=Phase.BUILD, status=RepoStatus.DEGRADED, attempts=1,
        last_error="stub in play", depends_on=["acme-commons"],
        depends_on_contracts=[CONTRACT_ID], blast_radius=3, stubbed_deps=[STUB_COORD],
        stub_states={STUB_COORD: StubState.ACTIVE}, revalidation_rounds=1, revalidation_usd=0.5,
        wave_index=1, dest_path="apps/acme/portal", updated_at=FROZEN,
        phases={Phase.BUILD: PhaseRecord(phase=Phase.BUILD, updated_at=FROZEN)},
    ),
    "MigrationState": _sample_state(),
    # ---- §5.6 build emission ----
    "BuildUnit": BuildUnit(
        unit_id="acme-commons", ecosystem=Ecosystem.MAVEN, dest="libs/com/acme/commons",
        srcs=["src/Commons.java"], test_srcs=["test/CommonsTest.java"], resources=["res/app.yaml"],
        published=COORD,
        internal_deps=[
            InternalDep(label="//libs/com/acme/base:base", dest="libs/com/acme/base")
        ],
        external_coordinates=[Coordinate(ecosystem=Ecosystem.MAVEN, group="org.slf4j", name="api")],
        contract_deps=[CONTRACT_ID],
    ),
    "InternalDep": InternalDep(
        label="//libs/com/acme/base:base", dest="libs/com/acme/base", published=COORD,
    ),
    "BuildTarget": BuildTarget(
        package="libs/com/acme/commons", name="commons", rule="java_library",
        load_from="@rules_java//java:defs.bzl", srcs=["Commons.java"], deps=["@maven//:api"],
        attrs={"javacopts": ["-Werror"], "tags": ["manual"]}, testonly=False,
    ),
    "WorkspaceDep": WorkspaceDep(
        ruleset="rules_jvm_external", extension="maven.install", coordinate=COORD,
        resolved_version="1.4.0", repo_name="maven", attrs={"repositories": ["https://repo1"]},
    ),
    "SupportFile": SupportFile(
        path="requirements.lock",
        carry_from=["py/acme_svc/requirements.lock", "py/acme_svc/requirements.txt"],
        content="requests>=2.31\n",
    ),
    "GazelleConfig": GazelleConfig(
        directives=["# gazelle:prefix github.com/acme/commons"], prefix="github.com/acme/commons",
        exclude=["vendor"], args=["-r"],
    ),
    "ToolchainRequirement": ToolchainRequirement(
        ruleset="rules_java", extension="java_toolchains.toolchain", name="remotejdk21",
        version="21", attrs={"nogc": "false"},
        repo_names=["remotejdk21_linux", "remotejdk21_macos"],
    ),
    "BuildPlan": BuildPlan(
        unit_id="acme-commons", dest="libs/com/acme/commons", generated_by="adapter",
        targets=[BuildTarget(package="libs/com/acme/commons", name="commons", rule="java_library")],
        workspace_deps=[WorkspaceDep(ruleset="rules_jvm_external", extension="maven.install",
                                     coordinate=COORD, repo_name="maven")],
        toolchains=[ToolchainRequirement(
            ruleset="rules_java", extension="java_toolchains.toolchain",
            name="remotejdk21", version="21", repo_names=["remotejdk21_linux"],
        )],
        unbound_contract_kinds=[(CONTRACT_ID, Ecosystem.NPM)],
    ),
}


def _exported_models() -> dict[str, type[FleetModel]]:
    """The model registry: every `FleetModel` subclass `fleet.models` exports."""
    out: dict[str, type[FleetModel]] = {}
    for name in models_pkg.__all__:
        obj = getattr(models_pkg, name)
        if isinstance(obj, type) and issubclass(obj, FleetModel):
            out[name] = obj
    return out


EXPORTED = _exported_models()


@pytest.mark.parametrize("name", sorted(EXPORTED))
def test_every_exported_model_round_trips_through_its_own_json(name: str) -> None:
    """`M.model_validate(json.loads(inst.model_dump_json())) == inst`, for every exported model.

    Why it matters: this is what makes resume possible. `model_dump_json()` emits every
    `@computed_field`, and `extra="forbid"` used to reject the model's own output on the way back
    in — "Extra inputs are not permitted" on every checkpoint, which is a run that can be written
    but never resumed. Driven off the exported registry rather than a hand-listed set, so a model
    added later is covered the moment it is exported.
    """
    model = EXPORTED[name]
    if name not in SAMPLES:
        pytest.fail(f"{name} is exported by fleet.models but has no round-trip sample in SAMPLES")
    inst = SAMPLES[name]
    assert isinstance(inst, model), f"SAMPLES[{name}] is not a {name}"

    payload = json.loads(inst.model_dump_json())

    # §12.46(i)'s literal text: a round-trip that re-supplies a computed field is a schema bug.
    # `model_dump_json()` DOES emit every `@computed_field` (that is exactly what makes this
    # assertion mean something rather than being vacuously true for models with none), so any
    # computed key must actually be present in the raw payload before we can claim the reload
    # exercised dropping it.
    computed = set(model.model_computed_fields)
    if computed:
        assert computed <= payload.keys(), (
            f"{name}'s computed fields {computed} never appeared in its own model_dump_json() "
            "output, so this test would not exercise §12.46(i)'s 'computed fields dropped' clause"
        )

    reloaded = model.model_validate(payload)

    # Compared via `model_fields`, per §12.46(i)'s literal wording — NOT via object equality.
    # `model_fields` is the FleetModel's own declared-field registry, disjoint from
    # `model_computed_fields` by construction, so iterating it can never re-admit a computed key
    # into what is being asserted.
    for field_name in model.model_fields:
        original_value = getattr(inst, field_name)
        reloaded_value = getattr(reloaded, field_name)
        assert reloaded_value == original_value, (
            f"{name}.{field_name} did not round-trip: {reloaded_value!r} != {original_value!r}"
        )

    # A genuinely unknown key must still be a hard error: `extra="forbid"` is what stops a
    # renamed field from being silently dropped on the way in.
    with pytest.raises(ValidationError):
        model.model_validate(payload | {"totally_unknown": 1})


def test_a_build_plan_written_before_repo_names_existed_still_loads() -> None:
    """`ToolchainRequirement.repo_names` (D11) is ADDITIVE with a default, so `SCHEMA_VERSION`
    does not move — and this is the assertion that entitles us to say so.

    Why it matters: `BuildPlan` is a persisted checkpoint (`checkpoints.load` compares the stored
    `schema_version` against the loader's and rejects the whole payload on a mismatch). Bumping
    `SCHEMA_VERSION` for a new *Pydantic* field would invalidate every in-flight checkpoint of
    every model and refuse every existing database until `fleet migrate-db` ran — a full re-run of
    every phase, to add a field that no SQL column and no migration-ladder step is affected by.
    The bump is warranted only when an old payload can no longer be READ, so that is what is
    checked here, against a payload with the key genuinely absent rather than set to its default.
    """
    plan = SAMPLES["BuildPlan"]
    assert isinstance(plan, BuildPlan)
    legacy = json.loads(plan.model_dump_json())
    for toolchain in legacy["toolchains"]:
        del toolchain["repo_names"]

    reloaded = BuildPlan.model_validate(legacy)

    assert reloaded.toolchains[0].repo_names == [], "absent ⇒ 'this plan imports no repos'"
    assert reloaded == plan.model_copy(
        update={"toolchains": [t.model_copy(update={"repo_names": []}) for t in plan.toolchains]}
    ), "nothing else in the plan shifted"


def test_the_checkpoint_survives_a_crash_safe_write_and_reload(
    sample_state: MigrationState, tmp_path: Any
) -> None:
    """A checkpoint must be identical after being written to disk and read back.

    Why it matters: `fleet resume` re-derives its position from durable state. A field that
    silently disappears (nested `phases`, `blocked_by`, `stubbed_deps`, `stub_states`) turns a
    resume into a re-run or a skip — precisely the failure the checkpoint exists to prevent.
    """
    path = tmp_path / "migration_state.json"
    path.write_text(sample_state.model_dump_json(), encoding="utf-8")

    reloaded = MigrationState.model_validate_json(path.read_text(encoding="utf-8"))

    assert reloaded == sample_state
    assert reloaded.schema_version == SCHEMA_VERSION == 8
    assert reloaded.repos["acme-commons"].phases[Phase.TRANSFORM].transient_retries == 2
    assert reloaded.repos["acme-billing"].blocked_by == ["acme-commons"]
    assert reloaded.repos["acme-portal"].stubbed_deps == [STUB_COORD]
    assert reloaded.repos["acme-portal"].stub_states == {STUB_COORD: StubState.ACTIVE}
    assert reloaded.started_at.tzinfo is not None


def test_the_projection_carries_the_operator_triage_lists(sample_state: MigrationState) -> None:
    """The computed fields are the operator's triage surface (§5.5): they must be IN the JSON a
    human reads, and must never be accepted back as input (they are derived, not stored)."""
    payload = json.loads(sample_state.model_dump_json())

    assert payload["needs_human"] == ["acme-commons"]
    assert payload["degraded"] == ["acme-portal"]
    assert payload["counts"][RepoStatus.BLOCKED.value] == 1
    assert payload["unresolved_stubs"] == {"acme-portal": [STUB_COORD]}
    assert MigrationState.model_validate(payload) == sample_state


# =======================================================================================
# (3) The status machine: illegal raises, legal does not, terminal is terminal
# =======================================================================================


def test_a_finished_repo_cannot_be_re_admitted() -> None:
    """`SUCCEEDED -> RUNNING` must raise.

    Why it matters: a crash-recovery sweep that resets statuses without consulting the machine
    would re-admit a repo whose PR is already open, producing a second writer on migrate/<repo>.
    """
    with pytest.raises(ValueError, match="illegal status transition"):
        transition(RepoStatus.SUCCEEDED, RepoStatus.RUNNING)


@pytest.mark.parametrize("new", sorted(RepoStatus))
@pytest.mark.parametrize("terminal", sorted(TERMINAL_STATUSES))
def test_no_automatic_path_leaves_a_terminal_status(terminal: RepoStatus, new: RepoStatus) -> None:
    """Terminal is mechanical, not prose: `ALLOWED_TRANSITIONS[terminal]` is empty, so no sweep
    can resurrect an abandoned repo. The one exception is a same-status rewrite, which keeps an
    idempotent replay (§11.7) from being an error."""
    if new is terminal:
        assert transition(terminal, new) is new
        return
    with pytest.raises(ValueError, match="illegal status transition"):
        transition(terminal, new)


@pytest.mark.parametrize(
    ("old", "new"),
    [(old, new) for old, allowed in ALLOWED_TRANSITIONS.items() for new in sorted(allowed)],
)
def test_every_declared_transition_is_accepted(old: RepoStatus, new: RepoStatus) -> None:
    """The gate must not be stricter than the table it documents, or a legal recovery path
    (RUNNING -> PENDING after a stale lease) becomes an unrecoverable crash."""
    assert transition(old, new) is new


def test_abandoned_is_reachable_only_through_the_audited_operator_door() -> None:
    """`REQUIRES_HUMAN_INTERVENTION -> PENDING` requires `operator=True` (§12.14).

    Why it matters: that flag is the difference between "a human re-ran it with `fleet retry`"
    and "the reaper lost track of it". An automatic sweep must not be able to spend the ladder
    budget again on a repo an operator already triaged.
    """
    rhi = RepoStatus.REQUIRES_HUMAN_INTERVENTION
    with pytest.raises(ValueError, match="illegal status transition"):
        transition(rhi, RepoStatus.PENDING)
    assert transition(rhi, RepoStatus.PENDING, operator=True) is RepoStatus.PENDING

    # The door opens onto PENDING and nothing else — never straight back into RUNNING...
    with pytest.raises(ValueError, match="illegal status transition"):
        transition(rhi, RepoStatus.RUNNING, operator=True)
    # ...and it is not a master key: SUCCEEDED stays terminal even for an operator.
    assert OPERATOR_REOPEN.keys() == {rhi}
    with pytest.raises(ValueError, match="illegal status transition"):
        transition(RepoStatus.SUCCEEDED, RepoStatus.PENDING, operator=True)


def test_a_resume_demotion_is_impossible_without_the_resume_flag() -> None:
    """`SUCCEEDED -> PENDING` is refused on every path that does not say `resume=True` (ADR-0077).

    Why it matters: this is the property the empty `ALLOWED_TRANSITIONS[SUCCEEDED]` set exists to
    defend, and §11.5 step 5 is the ONLY caller allowed to spend it. If the default path could
    demote, then the crash sweep, the worktree reaper and every `_on_breach` handler could
    silently un-finish landed, green work — and "SUCCEEDED is terminal" would go back to being
    prose. The flag is what keeps the guarantee mechanical for everyone else.
    """
    with pytest.raises(ValueError, match="illegal status transition"):
        transition(RepoStatus.SUCCEEDED, RepoStatus.PENDING)
    # ...including the OTHER audited door: `fleet retry`'s operator key is not a resume key.
    with pytest.raises(ValueError, match="illegal status transition"):
        transition(RepoStatus.SUCCEEDED, RepoStatus.PENDING, operator=True)

    assert transition(RepoStatus.SUCCEEDED, RepoStatus.PENDING, resume=True) is RepoStatus.PENDING


def test_the_resume_door_opens_onto_pending_from_succeeded_and_nothing_else() -> None:
    """`resume=True` is not a master key: it demotes settled work and re-opens nothing.

    Why it matters: §12 item 46 (ii) requires that a test driving every automatic sweep — the
    reaper, **`fleet resume`**, `stub_reconcile`, `blocked_by` recomputation — finds none of them
    able to move a repo out of `REQUIRES_HUMAN_INTERVENTION`. `fleet resume` is named there by
    name, so the flag that makes step 5 writable must not also make it an operator. DEGRADED is
    excluded for the same shape of reason: it leaves the machine only through a budgeted
    revalidation round (§3.5.1), and a demotion to PENDING would spend that budget by the back
    door with no round recorded.
    """
    assert RESUME_DEMOTE.keys() == {RepoStatus.SUCCEEDED}

    for held in (
        RepoStatus.REQUIRES_HUMAN_INTERVENTION,  # only an operator un-abandons (§12.14)
        RepoStatus.DEGRADED,                     # only a budgeted revalidation round (§3.5.1)
        RepoStatus.SKIPPED,                      # a config exclusion, not a resume's to un-decide
    ):
        with pytest.raises(ValueError, match="illegal status transition"):
            transition(held, RepoStatus.PENDING, resume=True)

    # And the one open key leads to PENDING only. The loop is DERIVED from `RepoStatus` rather
    # than listed, because a hand-written list is airtight on keys and leaky on values: an earlier
    # cut named three of the five reachable targets, so widening RESUME_DEMOTE to admit
    # SUCCEEDED -> SKIPPED passed this test and the key assertion above. Every status except
    # PENDING (the one legal target) and SUCCEEDED itself (an idempotent no-op, §11.7) must raise.
    # Adding a `RepoStatus` member enlarges this loop automatically but does NOT pass silently:
    # the count below is a deliberate stop, because whether a new status belongs in RESUME_DEMOTE
    # is a decision, not a default. Whoever adds the member makes it here and edits the count.
    elsewhere = [s for s in RepoStatus if s not in (RepoStatus.PENDING, RepoStatus.SUCCEEDED)]
    assert len(elsewhere) == 5, (
        "RepoStatus has grown: every non-PENDING target must be covered, not a chosen subset. "
        "Decide whether the new member belongs in RESUME_DEMOTE, then update this count to match."
    )
    for target in elsewhere:
        with pytest.raises(ValueError, match="illegal status transition"):
            transition(RepoStatus.SUCCEEDED, target, resume=True)


def test_demote_pairs_the_finding_and_is_stricter_than_transition() -> None:
    """`demote()` returns the new status AND the `PhaseDemoted` finding as one value, and refuses
    every status `RESUME_DEMOTE` does not open.

    Why it matters: a demotion throws away landed, green work — strictly more than a
    `checkpoint_rejected`, which `runner.py` already argues must be visible to whoever reads the
    wave — so the record travels with the status rather than depending on the writer remembering
    it. This does NOT make the finding unavoidable; `transition(..., resume=True)` still demotes
    silently, which the following test pins deliberately (ADR-0077 §4). What it does make
    unavoidable is the *strictness*: `RUNNING` and `BLOCKED` both reach `PENDING` through
    `ALLOWED_TRANSITIONS` before the resume branch is consulted, so a `demote()` that delegated
    its guard to `transition()` would mint a finding claiming green work was discarded when none
    ran. The payload carries `phase` because `_note_finding` fingerprints on
    `(run_id, repo_id, kind)` and would otherwise UPSERT three demoted phases into one row.
    """
    status, finding = demote(
        RepoStatus.SUCCEEDED,
        repo_id="acme/billing",
        phase=Phase.BUILD,
        reason="BUILD.bazel absent on the integration ref",
    )
    assert status is RepoStatus.PENDING
    assert isinstance(finding, PhaseDemotion)
    assert PHASE_DEMOTED_KIND == "PhaseDemoted"
    assert finding.payload() == {
        "repo_id": "acme/billing",
        "phase": int(Phase.BUILD),
        "from_status": "SUCCEEDED",
        "to_status": "PENDING",
        "reason": "BUILD.bazel absent on the integration ref",
    }

    # `demote()` accepts a RESUME_DEMOTE key and NOTHING else — deliberately stricter than
    # `transition()`, which is not a sufficient guard here.
    for refused in (
        # These three reach PENDING through ALLOWED_TRANSITIONS, matched BEFORE the resume
        # branch is consulted, so delegating the guard to `transition()` would let all three
        # through and mint a finding claiming green work was discarded when none ran.
        RepoStatus.RUNNING,    # the crash sweep's own edge (§11.5)
        RepoStatus.BLOCKED,    # step 5 runs BEFORE step 6's `blocked_by` recompute, so subtask
                               #   6 will genuinely meet a still-BLOCKED Phase-2 row
        RepoStatus.PENDING,    # an idempotent no-op (§11.7); nothing to discard
        # ...and these are illegal at `transition()` too, but must fail with the same message,
        # so the reason a caller is refused does not depend on which guard caught it.
        RepoStatus.REQUIRES_HUMAN_INTERVENTION,
        RepoStatus.DEGRADED,
        RepoStatus.SKIPPED,
    ):
        with pytest.raises(ValueError, match="is not a demotion"):
            demote(refused, repo_id="acme/billing", phase=Phase.BUILD, reason="not a demotion")

    # The refusals are `demote()`'s own, not inherited: RUNNING and BLOCKED remain perfectly
    # legal at the gate, and a caller that wants them wants no finding.
    assert transition(RepoStatus.RUNNING, RepoStatus.PENDING) is RepoStatus.PENDING
    assert transition(RepoStatus.BLOCKED, RepoStatus.PENDING) is RepoStatus.PENDING


def _enums_mutable_module_state() -> dict[str, str]:
    """Every mutable container bound at `enums` module level, by `repr`."""
    return {
        name: repr(value)
        for name, value in vars(enums_mod).items()
        if isinstance(value, dict | list | set | frozenset)
    }


TRANSITION_GLOBALS: frozenset[str] = frozenset(
    {"ALLOWED_TRANSITIONS", "OPERATOR_REOPEN", "RESUME_DEMOTE",
     "get", "frozenset", "ValueError", "value"}
)
"""Every global and attribute name `transition()`'s body is allowed to reference.

This is a WHITELIST, not a list of forbidden things, and that inversion is the point: a side
effect has to name something to reach it, so a NEW name a future edit adds — `warnings`, `print`,
an imported module, an attribute sink, a `.append` — enlarges `co_names` and trips the assertion
without anyone having predicted that particular form. It does NOT catch a side effect routed
through a name already listed here; ADR-0077 §4.2 records that boundary and why it is not closed.

Editing `transition()` legitimately will also trip it. **Widening this set to go green is the
wrong response** unless the new name is genuinely incapable of recording anything — see the
assertion's own failure message.
"""


def test_transition_demotes_without_writing_a_record_or_naming_a_new_sink(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`transition(SUCCEEDED, PENDING, resume=True)` demotes, and does not record it (ADR-0077 §4).

    Why it matters: the audit obligation is a CONVENTION, not a mechanism. `transition()` is
    public and exported, and `resume=True` demotes with no finding and no error. An earlier draft
    claimed "no call yields the demoted status without the record"; that was false, and a false
    guarantee is worse than an admitted convention because subtask 6 would have trusted it. This
    test keeps the gap visible in the suite rather than contradicted by it — if anyone closes the
    door, it fails and is deleted deliberately, which is the correct way to find out.

    Why the body looks like this: two earlier cuts of this test were named for a guarantee they
    did not check. The first asserted only the return value; a reviewer defeated it by binding a
    side effect into `transition()` with the return untouched. The second added a module-state
    snapshot and `caplog`, and a reviewer defeated THAT six ways — a function attribute, a mutable
    default argument, a **function-local** `from fleet...` import (invisible to a line-start
    source scan), `warnings.warn`, a `ClassVar` on `PhaseDemotion`, and `print()`. Each new
    enumeration of routes was defeated by a route not enumerated, because enumerating exits is the
    wrong shape of check.

    So the load-bearing assertion is now a WHITELIST of what `transition()` may name at all
    (`TRANSITION_GLOBALS`), which catches side effects by construction rather than by anticipating
    their form. All six forms above now fail it. The test's NAME is scoped to exactly that —
    "naming a NEW sink", not "reaching" one. An earlier name claimed the broader absence, which
    ADR-0077 §4.2's seven verified escapes defeat while this body stays green; a name asserting an
    absence needs its own proof, and the proof here reaches only as far as the whitelist does
    (CLAUDE.md Rule 12).

    It is strong and it is NOT a proof, which is recorded here rather than discovered later: a side
    effect routed entirely through names already on the whitelist passes every assertion below.
    Verified, not hypothesised — binding `RESUME_DEMOTE` to a `dict` subclass whose `get()` appends
    to an INSTANCE attribute records every demotion while this test stays green, because the body
    is byte-identical (so `co_names` is unchanged) and `dict.__repr__` shows only mapping contents
    (so the module-state snapshot cannot see it either). What this pins: a side effect added to
    `transition()`'s own body. What it does not: one hidden inside an object it already names.
    ADR-0077 §4.2 says why that boundary is where it is.
    """
    before = _enums_mutable_module_state()
    with caplog.at_level(logging.DEBUG):
        result = transition(RepoStatus.SUCCEEDED, RepoStatus.PENDING, resume=True)

    # A bare status, never the `(status, PhaseDemotion)` pair a caller could audit with — and the
    # identity check IS that assertion, because a `(status, record)` tuple is not
    # `RepoStatus.PENDING`. An `isinstance(result, RepoStatus) and not isinstance(result, tuple)`
    # line used to follow this one and was deleted: `RepoStatus` is a `StrEnum`, so no instance of
    # it is ever a `tuple`, and no mutation could fail it while this line held (Rule 12).
    assert result is RepoStatus.PENDING

    # The whitelist: `transition()` may reference these names and no others. A sink of any kind —
    # module, attribute, builtin, deferred import — must be named to be reached.
    assert set(transition.__code__.co_names) == TRANSITION_GLOBALS, (
        "transition() references a name TRANSITION_GLOBALS does not list. If you added an audit "
        "side-effect — a log call, a findings write, an import, an attribute sink — you have "
        "CLOSED the documented gap in ADR-0077 §4, which is a real design change: update §4, "
        "delete this test deliberately, and give demote() a reason to exist. Do NOT simply add "
        "the name to TRANSITION_GLOBALS to go green; that closes the door silently, which is the "
        "exact failure this gate exists to prevent."
    )
    # ...no state smuggled in as a default argument, and none captured from an enclosing scope,
    assert transition.__kwdefaults__ == {"operator": False, "resume": False}
    assert transition.__defaults__ is None
    assert transition.__code__.co_freevars == ()
    # ...no function-attribute sink hung off `transition` itself,
    assert vars(transition) == {}
    # ...nothing accumulated in any module-level dict/list/set/frozenset,
    assert _enums_mutable_module_state() == before
    # ...and nothing was logged.
    assert caplog.records == []

    # Belt and braces on the one route subtask 6 would realistically take: a deferred, function-
    # local `from fleet...` import is the ONLY way to hand `enums.py` a `StateWriter` without a
    # module-scope cycle. `.strip()` matters — an earlier cut scanned with a line-start
    # `startswith`, so an INDENTED import was invisible to exactly the check written to catch it.
    source = inspect.getsource(enums_mod)
    assert not [
        line
        for line in source.splitlines()
        if line.strip().startswith(("import ", "from ")) and "fleet" in line
    ], "enums.py gained a fleet import — re-check whether transition() can now reach a sink"


#: Every stance in which `RESUME_DEMOTE`'s own comment may NAME `transition(...)`: as the call a
#: demotion writer must not make. A whitelist of *stances*, not a blacklist of wrong sentences —
#: a sentence naming `transition()` in any other register ("reachable only through", "the path
#: is", "call it with") matches none of these and trips the assertion without anyone having
#: predicted its wording (CLAUDE.md Rule 12's whitelist inversion).
_TRANSITION_DISAVOWALS: tuple[str, ...] = ("never", "not ", "instead of", "rather than")

_TRANSITION_MENTION = re.compile(r"transition\s*\(")


def _resume_demote_comment_block() -> str:
    """`RESUME_DEMOTE`'s attached comment, whitespace-normalised into ONE string.

    Normalised whole before matching, deliberately: a line-oriented check cannot see a claim a
    reflow has split across a newline, and that is not hypothetical here — see the test below.
    """
    lines = inspect.getsource(enums_mod).splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("RESUME_DEMOTE"))
    block: list[str] = []
    for line in lines[start:]:
        if not line.strip():          # the first blank line ends the statement and its comment
            break
        block.append(line)
    return " ".join(" ".join(block).split())


def test_resume_demote_s_comment_names_demote_and_only_ever_disavows_transition() -> None:
    """The comment authorising the demotion write must send its reader to `demote()`.

    Why it matters: `enums.py`'s `transition()` docstring, `state/repository.py`'s
    `demote_to_floor` and `docs/SPEC.md` Constraint 7 all say the same thing — the demotion write
    goes through `demote()`, never `transition(..., resume=True)` directly, because `transition()`
    returns the status ALONE and a demotion made through it emits no `PhaseDemoted` finding and is
    invisible to whoever reads the run. `RESUME_DEMOTE`'s own comment is the one an author writing
    §11.5 step 5 opens FIRST, and for two commits it said the opposite: that a demotion "is
    reachable only through `transition(..., resume=True)`". Acting on it yields a fleet whose every
    demotion is silent with every status assertion still green.

    Why the body looks like this: the false clause was corrected once, at its root, and came back —
    a later commit re-wrapped the line onto its own row without reading it. A formatting pass
    re-opened a closed defect, so the check normalises the whole block before matching and asserts
    a WHITELIST of stances rather than grepping for the one sentence that was wrong.

    What this pins: the comment names `demote()`, and every mention of `transition(` in it is a
    disavowal. What it does NOT pin: that the rest of the block is true, or that the other three
    sites still agree — nothing here reads them. It is a guard on one comment, not a proof of
    consistency (CLAUDE.md Rule 12: a name asserting more than the body checks is the defect).
    """
    block = _resume_demote_comment_block()
    # Anti-vacuity: an edit that empties or renames the block must fail loudly, not silently
    # leave this test with nothing to consider.
    assert "SUCCEEDED" in block and len(block) > 500, (
        f"RESUME_DEMOTE's comment block did not parse as expected ({len(block)} chars) — this "
        "test is now inspecting the wrong text and would pass vacuously"
    )
    assert "`demote()`" in block, (
        "RESUME_DEMOTE's comment no longer names `demote()`. It is the first thing a step-5 "
        "author reads; if it does not send them to demote(), they will call "
        "transition(SUCCEEDED, PENDING, resume=True) and every demotion in the fleet becomes "
        "silent — no PhaseDemoted finding, and every status assertion still green."
    )
    mentions = list(_TRANSITION_MENTION.finditer(block))
    assert mentions, (
        "RESUME_DEMOTE's comment no longer names transition(...) at all, so the 'never call it "
        "directly' instruction has gone. Re-state it or delete this test deliberately."
    )
    undisavowed = [
        block[max(0, m.start() - 70) : m.end() + 10]
        for m in mentions
        if not any(word in block[max(0, m.start() - 70) : m.start()].lower()
                   for word in _TRANSITION_DISAVOWALS)
    ]
    assert not undisavowed, (
        f"RESUME_DEMOTE's comment names transition(...) other than to disavow it: {undisavowed}. "
        "The demotion write goes through `demote()` — never `transition(..., resume=True)` "
        "directly, which opens the same door but returns the status ALONE and would demote "
        "silently (ADR-0077 §4). Do NOT widen _TRANSITION_DISAVOWALS to go green: that is how "
        "this claim came back the first time."
    )


def test_the_third_failed_attempt_is_terminal() -> None:
    """Escalate, escalate, then abandon — a 4th attempt is never issued (ADR-0014).

    Why it matters: an unbounded retry loop is the harness failure mode this policy exists to
    prevent. `exhausted()` reads the ladder THIS phase actually ran under, so the terminal point
    is per-task policy rather than a compiled-in constant.
    """
    rec = PhaseRecord(phase=Phase.TRANSFORM, status=RepoStatus.RUNNING, attempts=MAX_ATTEMPTS - 1)
    assert rec.exhausted() is False

    rec.attempts = MAX_ATTEMPTS
    assert rec.exhausted() is True
    # A longer per-run ladder is not exhausted at 3 — the ceiling is the task's, not the module's.
    assert rec.exhausted(max_attempts=8) is False

    rec.status = transition(rec.status, RepoStatus.REQUIRES_HUMAN_INTERVENTION)
    assert rec.status is RepoStatus.REQUIRES_HUMAN_INTERVENTION
    with pytest.raises(ValueError, match="illegal status transition"):
        transition(rec.status, RepoStatus.RUNNING)


def test_a_phase_record_holds_every_repostatus_and_nothing_else() -> None:
    """`PhaseRecord.status` is the FULL `RepoStatus` domain — there is no narrower phase-level
    sub-vocabulary.

    Why it matters: §6's `phases.status` CHECK is derived from this enum
    (`test_phases_status_domain_is_exactly_the_repostatus_enum`), and that derivation is only
    sound while the model applies no narrowing of its own. If a validator here started rejecting,
    say, `SKIPPED` on a phase row, the DDL would be widened past what the model can hold and the
    §5↔§6 domains would drift apart again from the other side. `BLOCKED` is the member the pre-7
    CHECK omitted, so it is the one a regression would drop first.
    """
    for status in RepoStatus:
        rec = PhaseRecord(phase=Phase.TRANSFORM, status=status)
        assert PhaseRecord.model_validate_json(rec.model_dump_json()).status is status
    assert PhaseRecord(phase=Phase.TRANSFORM, status=RepoStatus.BLOCKED).status.value == "BLOCKED"
    # 'FAILED' was in the pre-7 CHECK domain and has never been a RepoStatus member.
    with pytest.raises(ValidationError):
        PhaseRecord(phase=Phase.TRANSFORM, status="FAILED")


def test_a_leased_phase_record_carries_its_fence_through_a_round_trip() -> None:
    """`lease_fence` is representable in §5, not only in §6.

    Why it matters: §6 makes fencing normative — every worker write carries `AND lease_fence = ?`
    and the reaper bumps it — so a `PhaseRecord` without the field cannot round-trip its own row.
    A resume would then rebuild the row with the fence silently dropped to its default, and the
    old holder's writes would start matching again: the reclaimed lease would be un-invalidatable.
    """
    rec = PhaseRecord(
        phase=Phase.TRANSFORM, status=RepoStatus.RUNNING, lease_owner=LEASE_OWNER, lease_fence=4,
        heartbeat_at=FROZEN, heartbeat_ttl_seconds=300, lease_expires_at=FROZEN,
    )
    assert PhaseRecord.model_validate_json(rec.model_dump_json()).lease_fence == 4

    # A fresh row starts at 0, matching the DDL's `NOT NULL DEFAULT 0` — not at NULL/None, which
    # would make the very first fenced UPDATE compare against nothing.
    assert PhaseRecord(phase=Phase.TRANSFORM).lease_fence == 0
    # Monotonic: a reclaim only ever bumps it, so a negative fence is not a state the reaper can
    # produce and is rejected rather than stored.
    with pytest.raises(ValidationError):
        PhaseRecord(phase=Phase.TRANSFORM, lease_fence=-1)
    # And the ttl the reaper measures against is never None — §6 gives it NOT NULL DEFAULT 300.
    with pytest.raises(ValidationError):
        PhaseRecord(phase=Phase.TRANSFORM, heartbeat_ttl_seconds=0)


# =======================================================================================
# (4) `edge_key` is content-derived: stable across a rebuild, independent of rowid
# =======================================================================================


def _edge_key(
    *, src_id: str, dst_ref: str, kind: EdgeKind, evidence_path: str, evidence_line: int
) -> str:
    """`edge_key_for` — CALLED, not re-implemented.

    This helper used to restate the recipe, which made it a fourth copy alongside the model, the
    inference module and §6's DDL; a test that re-implements what it is testing agrees with a
    drifted implementation just as happily as with a correct one.
    """
    return edge_key_for(
        src_kind=NodeKind.REPO, src_id=src_id, dst_kind=NodeKind.REPO, dst_ref=dst_ref,
        kind=kind, evidence_path=evidence_path, evidence_line=evidence_line,
    )


def _edge(
    *, edge_id: int | None = None, src_id: str = "acme-portal", coord: Coordinate = COORD,
    kind: EdgeKind = EdgeKind.DECLARED_DEP, evidence_path: str = "pom.xml", evidence_line: int = 42,
    detected_at: datetime = FROZEN,
) -> DependencyEdge:
    return DependencyEdge(
        edge_id=edge_id,
        edge_key=_edge_key(src_id=src_id, dst_ref=coord.key, kind=kind,
                           evidence_path=evidence_path, evidence_line=evidence_line),
        src_id=src_id, dst_coordinate=coord, dst_id="acme-commons", kind=kind,
        base_confidence=1.0, confidence=0.9, evidence_path=evidence_path,
        evidence_line=evidence_line, detected_at=detected_at,
    )


def test_edge_key_is_stable_across_a_rebuild_and_ignores_the_rowid() -> None:
    """The same semantic tuple yields the same `edge_key`, whatever rowid SQLite hands out.

    Why it matters: `edges` is rebuilt from scratch on resume (ADR-0004) and rowids are
    reassigned. A cycle-break decision recorded against a rowid therefore silently repoints at an
    unrelated edge — unauditable and un-rollbackable. Content addressing is what fixes it.
    """
    first_scan = _edge(edge_id=None)
    after_resume = _edge(edge_id=9_001, detected_at=datetime(2026, 9, 1, tzinfo=UTC))

    assert first_scan.edge_key == after_resume.edge_key
    assert first_scan.edge_id != after_resume.edge_id
    # A CycleFinding names edges by key, so the decision still resolves after the rebuild.
    finding = CycleFinding(
        scc_id=SCC_ID, members=["acme-portal", "acme-commons"], edges=[first_scan.edge_key],
        broken_edge_keys=[first_scan.edge_key], break_strategy=BreakStrategy.EDGE_BREAK,
    )
    assert finding.broken_edge_keys == [after_resume.edge_key]


@pytest.mark.parametrize(
    ("label", "other"),
    [
        ("different source", {"src_id": "acme-billing"}),
        ("different destination coordinate",
         {"coord": Coordinate(ecosystem=Ecosystem.MAVEN, group="com.acme", name="other")}),
        ("different edge kind", {"kind": EdgeKind.INTERNAL_IMPORT}),
        ("different evidence path", {"evidence_path": "build.gradle"}),
        ("different evidence line", {"evidence_line": 43}),
    ],
)
def test_a_different_semantic_tuple_is_a_different_edge(label: str, other: dict[str, Any]) -> None:
    """Every component of the tuple must move the key: two distinct edges that collide onto one
    key would let a break of the first suppress the second, silently and invisibly."""
    assert _edge().edge_key != _edge(**other).edge_key, label


# =======================================================================================
# (5) VerificationReport derives equivalence, without recursing
# =======================================================================================


def _report(**over: Any) -> VerificationReport:
    return VerificationReport(
        run_id=RUN, repo_id="acme-portal", build_ok=True, test_ok=True, rdeps_ok=True,
        verdict="PASS", generated_at=FROZEN, **over,
    )


@pytest.mark.parametrize(
    ("label", "kwargs", "expected"),
    [
        ("clean run", {}, Equivalence.FULL),
        ("sampled closure", {"rdeps_truncated": True}, Equivalence.CLOSURE_SAMPLED),
        ("stub in play",
         {"verified_against_stubs": [STUB_COORD],
          "stub_fidelity": {STUB_COORD: StubFidelity.PUBLISHED_ARTIFACT}},
         Equivalence.STUB_LIMITED),
        ("stub wins over a sampled closure",
         {"rdeps_truncated": True, "verified_against_stubs": [STUB_COORD],
          "stub_fidelity": {STUB_COORD: StubFidelity.EMPTY_FAILING}},
         Equivalence.STUB_LIMITED),
    ],
)
def test_equivalence_is_derived_with_a_fixed_precedence(
    label: str, kwargs: dict[str, Any], expected: Equivalence
) -> None:
    """STUB_LIMITED > CLOSURE_SAMPLED > FULL, computed from the evidence and never supplied.

    Why it matters: a green build against a stub is not the same claim as a green build against
    the real dependency graph (§3.5.1). Deriving it is what stops an honest-looking PASS from
    being assembled by an optimistic caller — so an explicitly passed FULL is overruled.
    """
    report = _report(equivalence=Equivalence.FULL, **kwargs)  # a caller's optimistic claim
    assert report.equivalence is expected, label
    ranked = EQUIVALENCE_RANK
    assert ranked[Equivalence.STUB_LIMITED] > ranked[Equivalence.CLOSURE_SAMPLED]
    assert ranked[Equivalence.CLOSURE_SAMPLED] > ranked[Equivalence.FULL]


def test_deriving_equivalence_does_not_recurse() -> None:
    """Constructing and re-validating a report must terminate.

    Why it matters: the derivation used to be an after-validator assigning to `self`, and under
    `validate_assignment=True` that re-enters the same validator — a RecursionError on the very
    first Phase 4 verdict. Running it in `mode="before"` is what makes both paths finite.
    """
    report = _report(rdeps_truncated=True)
    assert report.equivalence is Equivalence.CLOSURE_SAMPLED

    report.rdeps_ok = False                       # assignment re-validates; must not recurse
    round_tripped = VerificationReport.model_validate(json.loads(report.model_dump_json()))
    assert round_tripped.equivalence is Equivalence.CLOSURE_SAMPLED


def test_a_stub_must_declare_its_fidelity() -> None:
    """Naming a stub without its fidelity tier makes the disclosure unreadable: the reviewer
    cannot tell a signature-honest published artifact from an empty failing shim."""
    with pytest.raises(ValidationError, match="stub_fidelity must name exactly"):
        _report(verified_against_stubs=[STUB_COORD])


# =======================================================================================
# (6) The attempt ceiling is policy, not a type constraint
# =======================================================================================


@pytest.mark.parametrize(
    ("label", "build"),
    [
        ("TransformResult", lambda n: _result(attempt=n).attempt),
        ("BuildAttempt", lambda n: _attempt(attempt=n).attempt),
        ("RejectedApproach", lambda n: RejectedApproach(
            approach_signature=HEX_A, reason="still unresolved",
            failure_class=FailureClass.RULE_MISS, attempt=n, tier=TransformTier.LLM_ESCALATION,
        ).attempt),
    ],
)
def test_an_attempt_above_the_default_ceiling_still_validates(label: str, build: Any) -> None:
    """`attempt` is bounded `ge=1` and nothing more.

    Why it matters: `le=MAX_ATTEMPTS` made a longer-than-3 ladder unrepresentable — breaking the
    SPEC's own promise that the ladder is per-run configurable (§9) — and made a LOWERED constant
    retroactively unable to load historical rows. The ceiling is enforced at runtime against the
    owning `TransformTask.max_attempts`, which is where policy belongs.
    """
    assert build(MAX_ATTEMPTS + 5) == MAX_ATTEMPTS + 5, label
    with pytest.raises(ValidationError):
        build(0)  # attempt numbering starts at 1; 0 is a counter that never incremented


@pytest.mark.parametrize("max_attempts", [0, 9])
def test_the_configured_ladder_length_has_a_sanity_rail(max_attempts: int) -> None:
    """`TransformTask.max_attempts` is bounded `ge=1, le=8`: config typos are caught at load,
    while any ladder a human would actually configure remains expressible."""
    with pytest.raises(ValidationError):
        TransformTask(
            run_id=RUN, repo_id="acme-commons", phase=Phase.TRANSFORM, kind=TaskKind.REWRITE,
            dest_path="libs/x", max_attempts=max_attempts,
            ladder=(None,) * max(max_attempts, 1),
        )


def test_a_longer_ladder_is_representable_and_must_match_its_task() -> None:
    """A 4-rung ladder is legal; a ladder whose length disagrees with `max_attempts` is not.

    Why it matters: rung index == attempt number (ADR-0021). A ladder shorter than the ceiling
    means the last attempts silently re-run the top rung — the identical-retry loop again.
    """
    task = TransformTask(
        run_id=RUN, repo_id="acme-commons", phase=Phase.TRANSFORM, kind=TaskKind.REWRITE,
        dest_path="libs/x", max_attempts=4,
        ladder=(None, ContextPolicy.EVIDENCE_ONLY,
                ContextPolicy.EVIDENCE_PLUS_REJECTED_APPROACHES,
                ContextPolicy.EVIDENCE_PLUS_PRIORS),
    )
    assert len(task.ladder) == task.max_attempts == 4

    with pytest.raises(ValidationError, match="one context policy per attempt"):
        TransformTask(run_id=RUN, repo_id="acme-commons", phase=Phase.TRANSFORM,
                      kind=TaskKind.REWRITE, dest_path="libs/x", max_attempts=4,
                      ladder=DEFAULT_LADDER)
    with pytest.raises(ValidationError, match="attempt 1 is deterministic"):
        TransformTask(run_id=RUN, repo_id="acme-commons", phase=Phase.TRANSFORM,
                      kind=TaskKind.REWRITE, dest_path="libs/x", max_attempts=3,
                      ladder=(ContextPolicy.EVIDENCE_ONLY, *DEFAULT_LADDER[1:]))


# =======================================================================================
# (7) Stub lifecycle, and the RepoState stub invariant
# =======================================================================================


def _stub(**over: Any) -> StubRecord:
    kwargs: dict[str, Any] = {
        "run_id": RUN, "coord_key": STUB_COORD, "provider_repo_id": "acme-commons",
        "consumer_repo_ids": ["acme-portal"], "fidelity": StubFidelity.PUBLISHED_ARTIFACT,
        "pinned_version": "1.4.0", "max_revalidation_rounds": 2,
    }
    return StubRecord(**(kwargs | over))


def test_a_stub_at_its_round_cap_cannot_buy_another_round() -> None:
    """`rounds_spent` may reach `max_revalidation_rounds` and never exceed it.

    Why it matters: the cap is a budget (§3.5.1). Without the model enforcing it, a consumer
    that keeps failing revalidation quietly spends unbounded rework money; with it, the only
    representable outcomes at the cap are RESOLVED or ABANDONED — never one more silent try.
    """
    at_cap = _stub(rounds_spent=2, state=StubState.ABANDONED)
    assert at_cap.rounds_spent == at_cap.max_revalidation_rounds

    with pytest.raises(ValidationError, match="rounds_spent exceeds its own cap"):
        _stub(rounds_spent=3)


def test_a_published_artifact_stub_must_name_the_version_it_impersonates() -> None:
    """PUBLISHED_ARTIFACT without a `pinned_version` is a promise of behavioural honesty with
    nothing behind it; EMPTY_FAILING is the honest alternative and fails loudly at build time."""
    with pytest.raises(ValidationError, match="needs a pinned_version"):
        _stub(pinned_version=None)
    assert _stub(fidelity=StubFidelity.EMPTY_FAILING, pinned_version=None).pinned_version is None


@pytest.mark.parametrize(
    ("label", "kwargs"),
    [
        ("succeeded while still carrying a stub",
         {"status": RepoStatus.SUCCEEDED, "stubbed_deps": [STUB_COORD],
          "stub_states": {STUB_COORD: StubState.ACTIVE}}),
        ("degraded with nothing stubbed", {"status": RepoStatus.DEGRADED}),
        ("lifecycle rows that do not match the projection",
         {"status": RepoStatus.DEGRADED, "stubbed_deps": [STUB_COORD],
          "stub_states": {"maven:com.acme:other": StubState.ACTIVE}}),
    ],
)
def test_a_repo_cannot_ship_green_with_an_unresolved_stub(
    label: str, kwargs: dict[str, Any]
) -> None:
    """`bool(stubbed_deps) == (status is DEGRADED)` and `set(stub_states) == set(stubbed_deps)`.

    Why it matters: `RepoState(status=SUCCEEDED, stubbed_deps=[...])` used to validate cleanly,
    and that is a PR shipped against a dependency that is still a stub. Prose invariants are not
    invariants — the combination has to be unrepresentable.
    """
    with pytest.raises(ValidationError):
        RepoState(**kwargs)


def test_the_degraded_projection_is_clearable_on_resolution() -> None:
    """The legal shapes: DEGRADED with matching stub rows, and a clean SUCCEEDED once every row
    reached RESOLVED and the projection was cleared together with it (§3.5.1)."""
    degraded = RepoState(
        status=RepoStatus.DEGRADED, stubbed_deps=[STUB_COORD],
        stub_states={STUB_COORD: StubState.ACTIVE},
    )
    assert degraded.stub_states[STUB_COORD] is StubState.ACTIVE
    assert RepoState(status=RepoStatus.SUCCEEDED).stubbed_deps == []


# =======================================================================================
# (8) The ladder escalates rather than repeating
# =======================================================================================


def test_the_default_ladder_escalates_rather_than_repeating() -> None:
    """Rung index == attempt number, rung 0 is the deterministic non-LLM attempt, and no rung
    repeats (ADR-0021).

    Why it matters: identical retries are how the reference material's infinite-loop failure mode
    begins. Each rung must change what the attempt is ALLOWED to see, not merely re-run it.
    """
    assert len(DEFAULT_LADDER) == MAX_ATTEMPTS == 3
    assert DEFAULT_LADDER[0] is None, "attempt 1 is deterministic: no prompt is rendered at all"
    assert all(rung is not None for rung in DEFAULT_LADDER[1:])
    assert len(set(DEFAULT_LADDER)) == len(DEFAULT_LADDER), "a repeated rung is a repeated attempt"
    assert DEFAULT_LADDER[1] is ContextPolicy.EVIDENCE_ONLY
    assert DEFAULT_LADDER[2] is ContextPolicy.EVIDENCE_PLUS_REJECTED_APPROACHES


def test_the_worker_tier_ladder_tracks_the_context_ladder(worker_ctx: WorkerContext) -> None:
    """One tier per context rung, escalating, clamped at the top.

    Why it matters: the tier is a consequence of which rung is running (SPEC §7.1). Declaring it
    separately lets the two drift, so a run could pay HEAVY prices on the deterministic attempt —
    or, worse, re-run the cheap rung three times and call it an escalation.
    """
    assert len(TIER_LADDER) == len(DEFAULT_LADDER)
    assert TIER_LADDER[0] is TransformTier.DETERMINISTIC
    assert len(set(TIER_LADDER)) == len(TIER_LADDER)
    assert TIER_LADDER == (
        TransformTier.DETERMINISTIC, TransformTier.LLM_REPAIR, TransformTier.LLM_ESCALATION
    )
    # The clamp: an attempt past the last rung re-runs the TOP rung rather than raising IndexError.
    assert TIER_LADDER[min(99, len(TIER_LADDER) - 1)] is TIER_LADDER[-1]

    # A context is stamped with the rung its attempt runs at; attempt N reads rung N - 1.
    assert worker_ctx.tier is TransformTier.DETERMINISTIC
    escalated = [replace(worker_ctx, attempt=n, tier=TIER_LADDER[n - 1]).tier for n in (1, 2, 3)]
    assert escalated == list(TIER_LADDER), "attempts must escalate, not repeat"


# =======================================================================================
# (9) Invalid input is rejected at the boundary
# =======================================================================================


def test_naive_datetimes_are_rejected() -> None:
    """A naive timestamp cannot be compared across hosts, so heartbeat staleness — and therefore
    crash detection (§11.5) — would be silently wrong. Reject at the boundary."""
    with pytest.raises(ValidationError, match="tz-aware"):
        MigrationState(run_id=uuid4(), started_at=datetime(2026, 8, 8, 12, 0, 0))


def test_self_edges_are_rejected() -> None:
    """A repo depending on itself is a phantom cycle: the sequencer would never place it in a
    wave, and the SCC pass would report a cycle no human can break."""
    with pytest.raises(ValidationError, match="self-edge"):
        DependencyEdge(
            edge_key=HEX_A, src_id="acme-commons", dst_coordinate=COORD, dst_id="acme-commons",
            kind=EdgeKind.DECLARED_DEP, base_confidence=1.0, confidence=1.0,
            evidence_path="pom.xml",
        )


@pytest.mark.parametrize(
    ("label", "kwargs", "match"),
    [
        ("EDGE_BREAK that broke nothing",
         {"break_strategy": BreakStrategy.EDGE_BREAK, "broken_edge_keys": []},
         "detection, not breaking"),
        ("CONTRACT_HOIST that hoisted nothing",
         {"break_strategy": BreakStrategy.CONTRACT_HOIST}, "detection, not breaking"),
        ("ATOMIC_WAVE with no wave",
         {"break_strategy": BreakStrategy.ATOMIC_WAVE}, "requires atomic_wave_index"),
    ],
)
def test_a_cycle_finding_must_actually_break_the_cycle(
    label: str, kwargs: dict[str, Any], match: str
) -> None:
    """A finding that only *describes* a cycle leaves a cyclic ordering graph behind, and the
    sequencer never terminates. `break_strategy` has to be substantiated by its own evidence."""
    with pytest.raises(ValidationError, match=match):
        CycleFinding(scc_id=SCC_ID, members=["acme-commons", "acme-portal"],
                     edges=[HEX_A, HEX_B], **kwargs)


def test_an_attempt_that_executed_nothing_must_say_why() -> None:
    """`command` and `exit_code` are present together or absent together, and the only legal
    "neither" is an ADR-0021 anchoring rejection.

    Why it matters: a missing exit code is otherwise indistinguishable from a crashed probe, and
    a verdict inferred from an absence is a green that nothing ever proved.
    """
    assert _attempt(command=[], exit_code=None,
                    failure_class=FailureClass.ANCHORED_REPEAT).ok is False
    # A command with no exit code is a probe whose verdict was lost, not a rejection.
    with pytest.raises(ValidationError, match="both be present or both be absent"):
        _attempt(exit_code=None, failure_class=FailureClass.BUILD_ERROR)
    # ...and nothing executed without the anchoring reason is an unexplained empty attempt.
    with pytest.raises(ValidationError, match="must be ANCHORED_REPEAT"):
        _attempt(command=[], exit_code=None)


def test_a_revalidation_round_belongs_to_phase_four() -> None:
    """A `revalidation_round` outside VERIFY is a mis-attributed first-pass build: it would land
    in the `attempts` primary key alongside a first-pass row and make the evidence unreadable."""
    with pytest.raises(ValidationError, match="requires phase VERIFY"):
        _attempt(phase=Phase.BUILD, revalidation_round=1)
