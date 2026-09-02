"""SPEC §12 item 4 — checked-in golden-response fixtures, one per shipped backend + PROMPTED rung,
per LLM role. `docs/CRITERIA_PLAN.md` §4's revised Done bar requires this per **role** (12,
`config/models.yaml`), not just per backend — round II's task 3 landed `REPO_CLASSIFY` alone (1 of
12); round III task 3 added `PR_TITLE` and `PR_BODY` (3 of 12); round IV tasks 2 and 3 together
add `BUILD_DIAGNOSIS`, `DEP_DISAMBIGUATE`, `CYCLE_BREAK_PROPOSAL`, and `CONFLICT_RESOLUTION`
(7 of 12); round V tasks 1 and 2 together add `TRANSFORM_REPAIR`, `MANIFEST_EXTRACT`,
`API_INCOMPAT_REWRITE`, and `ESCALATION` (11 of 12); round V task 5 adds `BUILD_AUTHORING`
— the 12th and last of the 12 roles, closing §12.4 in full. `BuildFileProposal` (its declared
schema) is a TWO-LEVEL nested-object tuple: `targets` is a `tuple[BuildTargetProposal, ...]`, and
each `BuildTargetProposal` itself carries three sibling `tuple[str, ...]` fields (`srcs`/`deps`/
`visibility`) — structurally heavier than `TRANSFORM_REPAIR`/`API_INCOMPAT_REWRITE`/`ESCALATION`'s
`ProposedFileEdit` (2 scalar fields) or `MANIFEST_EXTRACT`'s `ExtractedDependency` (3 scalar
fields, no nested arrays). The anthropic fixture below carries 2 target entries with non-empty
`srcs`/`deps`/`visibility` on the first, so the round-trip exercises both the outer `targets` tuple
and every one of the inner object's own tuple fields at once; the mutation pair below deletes a
required field one level inside a tuple ENTRY's own tuple entry (`targets[0].name`) rather than at
the outer level, following `TRANSFORM_REPAIR`'s own precedent (round V task 1 mutated inside
`ProposedFileEdit`, not just `LlmPatchProposal` itself).

ADR-0013's contract layer declares the intent this closes: "every LLM prompt's declared response
schema validates against a stored golden sample." `tests/test_llm_roles.py`'s
`test_every_response_schema_round_trips_and_refuses_an_extra_field` already covers a DIFFERENT
half of that sentence — a hand-shaped Pydantic-model-literal sample (`_sample(model_cls)`)
round-trips through `model_dump_json`/`model_validate_json` — but it never touches a backend's own
wire shape or its real response-parsing code; the "golden sample" there is already in the model's
own vocabulary, not a recorded wire response. This file is the missing half: a RECORDED raw
wire-format response, one per (role, shipped backend), parsed through that backend's REAL
`parse_reply`/`_reply_from` (not a mock of the parsing step), then validated against its role's
declared schema through the same `client._validate` every live call runs.

Fixtures live in `tests/fixtures/llm/golden_responses/*.json`, named `<role>_<backend>_<rung>.json`
for every role but `REPO_CLASSIFY` (round II's fixtures predate the multi-role naming and are kept
as `<backend>_<rung>.json` — renaming them is out of this task's scope, they are still addressed by
name in `GOLDEN_CASES` below). Each fixture carries a `_provenance` key documenting where its shape
came from (the adapter's own parsing code, a vendor API doc, or an existing test helper it
matches) — popped before the fixture is handed to real parsing code, so what the assertion actually
consumes is exactly what a wire response would contain.

**Table-driven structure (round III task 3 refactor).** Round II's per-role shape was 4 near-
identical test functions (one per backend) plus 2 mutation/control tests, entirely hand-copied for
`REPO_CLASSIFY`. This round III research (`.superpowers/sdd/round-III-criteria-closure/
research-1-report.md` §4.2a) confirmed every shipped backend's wire envelope carries a role's
payload at exactly one insertion point, and nothing about that insertion point depends on which
role it is (`_TOOL_NAME = "emit_response"` is a role-agnostic constant in every tool-call backend
module; PROMPTED's `json.loads(text)` never introspects the decoded object's shape before handing
it to `response_model.model_validate`). That makes the parse-and-validate test genuinely
role-agnostic: `GoldenCase` below is the `Role → {backend/rung → fixture filename, expected-field
assertions}` table this observation makes possible, and `test_golden_response_parses_and_validates`
is the single parametrized test body every (role, backend) pair runs through. Adding role #4 is
now "add 4 fixtures + 4 `GoldenCase` rows," not "add 4 near-duplicate functions."

**REPO_CLASSIFY refactor is substance-preserving, not a rewrite.** The 4 `GoldenCase` rows for
`REPO_CLASSIFY` assert exactly what round II's 4 discrete functions asserted — the same 2 fields
(`ecosystem`, `is_library`) plus `finish_reason` per backend, against the same 4 fixture files,
through the same real parse/validate call chain — confirmed by running the suite before and after
this refactor (see this task's report for the exact before/after pass counts and `git diff
--stat`). Nothing about the refactor changes which fixtures exist, what they contain, or what is
checked against them; it only changes how the 4 near-identical checks are expressed.

**Rungs.** `anthropic`/`bedrock`/`vertex` are demonstrated at TOOL_CALL — their structurally
richer rung, a nested arguments object rather than bare text, and the rung their own
`_DECLARED.structured_output_modes` lists first. `openai_compatible` is demonstrated at
PROMPTED — the rung ADR-0013's contract layer names as required, and the ONLY rung this backend's
`declared_capabilities()` claims at all (`structured_output_modes == (PROMPTED,)`,
`supports_tools=False`), which makes it the cleanest place in this codebase to demonstrate it: no
other shipped backend has PROMPTED as its floor rather than its fallback. Every new role follows
the identical backend/rung split (`REPO_CLASSIFY`'s own, from round II): `anthropic`/`bedrock`/
`vertex` at `TOOL_CALL`, `openai_compatible` at `PROMPTED` — not a 4×2 rung matrix.

**Why TOOL_CALL parsing is reused via `parse_reply`/`_reply_from` rather than reimplemented.**
Those are the exact functions `AnthropicBackend.invoke` / the module-level `invoke()` on the other
three adapters call in production after a transport reply arrives — see the call sites in
`src/fleet/llm/backends/{anthropic,bedrock,vertex,openai_compatible}.py`. Nothing here mocks a
parsing step; only the network transport (never reached — these fixtures are already the
"decoded body") is absent. `_parse_anthropic` below exists only to give the anthropic backend the
same uniform `(raw, target) -> BackendReply` call signature the other three backends' `parse_reply`
already have — it is the SDK's own `Message.model_validate()` followed by the real `_reply_from`,
not a substitute for either.

**Why `bedrock`/`vertex` import through `tests.test_llm_backend_{bedrock,vertex}`.** Both
`boto3` and `google.auth` are `pyproject.toml` extras genuinely absent from this venv (verified:
`importlib.util.find_spec` returns `None` for both), and both adapter modules do an unguarded
module-scope `import boto3` / `google.auth` import (deliberately, per each module's own
docstring — `client.discover()`'s contract that "a backend whose SDK is not installed fails its
import here"). Those two test files already install a minimal, arity-correct SDK stub before
importing the adapter; importing `parse_reply`/`target` from them (an established pattern in this
suite — see `tests/test_llm_backend_anthropic.py`'s `from tests.test_cli import write_config`,
`tests/test_bazel.py`'s `from tests.conftest import ...`) reuses that stub rather than
duplicating it, and keeps this file working whether it is run alone or alongside those files.
`anthropic` and `openai_compatible`'s real SDKs ARE installed in this venv (verified the same
way), so those two adapters are imported directly.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from anthropic.types import Message as SdkMessage
from pydantic import ValidationError

from fleet.llm import client as client_module
from fleet.llm.backends import anthropic as anthropic_backend
from fleet.llm.backends import openai_compatible as oc_backend
from fleet.llm.client import BackendReply
from fleet.llm.roles import Role
from fleet.llm.schemas import (
    RESPONSE_SCHEMAS,
    ApiRewriteProposal,
    BuildFileProposal,
    BuildTargetProposal,
    CycleBreakProposal,
    DependencyDisambiguation,
    ExtractedDependency,
    FleetModel,
    LlmBuildDiagnosis,
    LlmEscalationProposal,
    LlmPatchProposal,
    ManifestExtraction,
    PrBody,
    ProposedFileEdit,
    PrTitle,
    RepoClassification,
    VersionConflictResolution,
)
from fleet.models.enums import BreakStrategy, Ecosystem, FailureClass, StructuredOutputMode
from fleet.models.tasks import BackendTarget, Price
from tests.test_llm_backend_bedrock import parse_reply as bedrock_parse_reply
from tests.test_llm_backend_bedrock import target as bedrock_target
from tests.test_llm_backend_vertex import parse_reply as vertex_parse_reply
from tests.test_llm_backend_vertex import target as vertex_target

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "llm" / "golden_responses"

# The concrete class, not just the `RESPONSE_SCHEMAS[Role.X]` lookup: the mapping's declared value
# type is `type[FleetModel]`, which would make every field access in `GOLDEN_CASES.expected` below
# untyped. The assertion ties each role to its class so a future `RESPONSE_SCHEMAS` remap (a
# different model class) fails loudly here instead of silently validating fixtures against the
# wrong schema.
assert RESPONSE_SCHEMAS[Role.REPO_CLASSIFY] is RepoClassification
assert RESPONSE_SCHEMAS[Role.PR_TITLE] is PrTitle
assert RESPONSE_SCHEMAS[Role.PR_BODY] is PrBody
assert RESPONSE_SCHEMAS[Role.BUILD_DIAGNOSIS] is LlmBuildDiagnosis
assert RESPONSE_SCHEMAS[Role.DEP_DISAMBIGUATE] is DependencyDisambiguation
assert RESPONSE_SCHEMAS[Role.CYCLE_BREAK_PROPOSAL] is CycleBreakProposal
assert RESPONSE_SCHEMAS[Role.CONFLICT_RESOLUTION] is VersionConflictResolution
assert RESPONSE_SCHEMAS[Role.API_INCOMPAT_REWRITE] is ApiRewriteProposal
assert RESPONSE_SCHEMAS[Role.ESCALATION] is LlmEscalationProposal
assert RESPONSE_SCHEMAS[Role.TRANSFORM_REPAIR] is LlmPatchProposal
assert RESPONSE_SCHEMAS[Role.MANIFEST_EXTRACT] is ManifestExtraction
assert RESPONSE_SCHEMAS[Role.BUILD_AUTHORING] is BuildFileProposal


def _load(name: str) -> dict[str, Any]:
    """Read one fixture and strip its documentation-only `_provenance` key. What is left is
    exactly the wire shape handed to the backend's real parsing code — no test-only additions."""
    raw: dict[str, Any] = json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))
    assert "_provenance" in raw, f"{name} is missing its required _provenance documentation key"
    raw.pop("_provenance")
    return raw


def _anthropic_target() -> BackendTarget:
    return BackendTarget(
        backend="anthropic",
        model_id="target-under-test",
        api_key_env="FLEET_TEST_GOLDEN_ANTHROPIC_KEY",
        price=Price(in_per_mtok=1.0, out_per_mtok=2.0),
    )


def _oc_target() -> BackendTarget:
    return BackendTarget(
        backend="openai_compatible",
        model_id="local-model",
        base_url="http://localhost:8000/v1",
        price="free",
    )


def _parse_anthropic(raw: dict[str, Any], target: BackendTarget) -> BackendReply:
    """Uniform `(raw, target) -> BackendReply` wrapper, matching the other 3 backends'
    `parse_reply` signature. `SdkMessage.model_validate` is the real Anthropic SDK's own
    wire-to-object parse; `_reply_from` is the real adapter parse — nothing here is a stand-in for
    either."""
    message = SdkMessage.model_validate(raw)
    return anthropic_backend._reply_from(target, message)


# ---------------------------------------------------------------------------------------------
# The table: one row per (role, backend). Every row runs the SAME real parse-then-validate chain
# — only the fixture, backend, rung, and expected fields differ per row.
# ---------------------------------------------------------------------------------------------

_BACKEND_PARSE: Mapping[str, Callable[[dict[str, Any], BackendTarget], BackendReply]] = {
    "anthropic": _parse_anthropic,
    "bedrock": bedrock_parse_reply,
    "vertex": vertex_parse_reply,
    "openai_compatible": oc_backend.parse_reply,
}

_BACKEND_TARGET: Mapping[str, Callable[[], BackendTarget]] = {
    "anthropic": _anthropic_target,
    "bedrock": bedrock_target,
    "vertex": vertex_target,
    "openai_compatible": _oc_target,
}


@dataclass(frozen=True)
class GoldenCase:
    """One (role, backend) golden-response case: which fixture to load, which backend/rung parses
    and validates it, and what a correctly-parsed-and-validated reply must show. `expected` maps
    an attribute name on the validated model to its expected value — checked via `getattr`, so a
    row states only the fields that matter for that fixture, exactly as round II's discrete
    functions did."""

    role: Role
    schema: type[FleetModel]
    backend: str
    rung: StructuredOutputMode
    fixture: str
    finish_reason: str
    expected: Mapping[str, object]


GOLDEN_CASES: tuple[GoldenCase, ...] = (
    # --- REPO_CLASSIFY (round II) — same 4 fixtures, same fields checked, table-driven now. ---
    GoldenCase(
        role=Role.REPO_CLASSIFY,
        schema=RepoClassification,
        backend="anthropic",
        rung=StructuredOutputMode.TOOL_CALL,
        fixture="anthropic_tool_call.json",
        finish_reason="tool_call",
        expected={"ecosystem": Ecosystem.MAVEN, "is_library": True},
    ),
    GoldenCase(
        role=Role.REPO_CLASSIFY,
        schema=RepoClassification,
        backend="bedrock",
        rung=StructuredOutputMode.TOOL_CALL,
        fixture="bedrock_tool_call.json",
        finish_reason="tool_call",
        expected={"ecosystem": Ecosystem.NPM, "is_library": False},
    ),
    GoldenCase(
        role=Role.REPO_CLASSIFY,
        schema=RepoClassification,
        backend="vertex",
        rung=StructuredOutputMode.TOOL_CALL,
        fixture="vertex_tool_call.json",
        finish_reason="tool_call",
        expected={"ecosystem": Ecosystem.GRADLE, "is_library": True},
    ),
    GoldenCase(
        role=Role.REPO_CLASSIFY,
        schema=RepoClassification,
        backend="openai_compatible",
        rung=StructuredOutputMode.PROMPTED,
        fixture="openai_compatible_prompted.json",
        finish_reason="stop",
        expected={"ecosystem": Ecosystem.PYPI, "is_library": False},
    ),
    # --- PR_TITLE (round III task 3, new) ---
    GoldenCase(
        role=Role.PR_TITLE,
        schema=PrTitle,
        backend="anthropic",
        rung=StructuredOutputMode.TOOL_CALL,
        fixture="pr_title_anthropic_tool_call.json",
        finish_reason="tool_call",
        expected={"title": "Migrate acme-commons Maven module into the monorepo"},
    ),
    GoldenCase(
        role=Role.PR_TITLE,
        schema=PrTitle,
        backend="bedrock",
        rung=StructuredOutputMode.TOOL_CALL,
        fixture="pr_title_bedrock_tool_call.json",
        finish_reason="tool_call",
        expected={"title": "Add npm package left-pad to the monorepo"},
    ),
    GoldenCase(
        role=Role.PR_TITLE,
        schema=PrTitle,
        backend="vertex",
        rung=StructuredOutputMode.TOOL_CALL,
        fixture="pr_title_vertex_tool_call.json",
        finish_reason="tool_call",
        expected={"title": "Port gradle module widget-core into the monorepo"},
    ),
    GoldenCase(
        role=Role.PR_TITLE,
        schema=PrTitle,
        backend="openai_compatible",
        rung=StructuredOutputMode.PROMPTED,
        fixture="pr_title_openai_compatible_prompted.json",
        finish_reason="stop",
        expected={"title": "Vendor pypi package flask-utils into the monorepo"},
    ),
    # --- PR_BODY (round III task 3, new) ---
    GoldenCase(
        role=Role.PR_BODY,
        schema=PrBody,
        backend="anthropic",
        rung=StructuredOutputMode.TOOL_CALL,
        fixture="pr_body_anthropic_tool_call.json",
        finish_reason="tool_call",
        expected={
            "body": "Moves the acme-commons Maven module into the monorepo unchanged. No API "
            "surface or build output changes.",
            "highlights": ("no behaviour change", "coordinates unchanged"),
        },
    ),
    GoldenCase(
        role=Role.PR_BODY,
        schema=PrBody,
        backend="bedrock",
        rung=StructuredOutputMode.TOOL_CALL,
        fixture="pr_body_bedrock_tool_call.json",
        finish_reason="tool_call",
        expected={
            "body": "Vendors the left-pad npm package into the monorepo as a leaf dependency. "
            "No downstream consumers exist yet.",
            "highlights": ("no downstream consumers", "leaf dependency"),
        },
    ),
    GoldenCase(
        role=Role.PR_BODY,
        schema=PrBody,
        backend="vertex",
        rung=StructuredOutputMode.TOOL_CALL,
        fixture="pr_body_vertex_tool_call.json",
        finish_reason="tool_call",
        expected={
            "body": "Ports the widget-core gradle module into the monorepo. Package path and "
            "public API are unchanged.",
            "highlights": ("package path unchanged", "public API unchanged"),
        },
    ),
    GoldenCase(
        role=Role.PR_BODY,
        schema=PrBody,
        backend="openai_compatible",
        rung=StructuredOutputMode.PROMPTED,
        fixture="pr_body_openai_compatible_prompted.json",
        finish_reason="stop",
        expected={
            "body": "Vendors the flask-utils pypi package into the monorepo. No existing "
            "consumer is affected.",
            "highlights": ("no existing consumer affected",),
        },
    ),
    # --- BUILD_DIAGNOSIS (round IV task 2, new) ---
    GoldenCase(
        role=Role.BUILD_DIAGNOSIS,
        schema=LlmBuildDiagnosis,
        backend="anthropic",
        rung=StructuredOutputMode.TOOL_CALL,
        fixture="build_diagnosis_anthropic_tool_call.json",
        finish_reason="tool_call",
        expected={
            "failure_class": FailureClass.BUILD_ERROR,
            "root_cause": "pom.xml is missing a dependency declaration for commons-lang3, which "
            "src/main/java/com/acme/Util.java imports",
            "suspect_paths": ("pom.xml", "src/main/java/com/acme/Util.java"),
        },
    ),
    GoldenCase(
        role=Role.BUILD_DIAGNOSIS,
        schema=LlmBuildDiagnosis,
        backend="bedrock",
        rung=StructuredOutputMode.TOOL_CALL,
        fixture="build_diagnosis_bedrock_tool_call.json",
        finish_reason="tool_call",
        expected={
            "failure_class": FailureClass.TEST_FAILURE,
            "suggested_action": "change the default pad character parameter from '0' to ' ' in "
            "index.js",
        },
    ),
    GoldenCase(
        role=Role.BUILD_DIAGNOSIS,
        schema=LlmBuildDiagnosis,
        backend="vertex",
        rung=StructuredOutputMode.TOOL_CALL,
        fixture="build_diagnosis_vertex_tool_call.json",
        finish_reason="tool_call",
        expected={
            "failure_class": FailureClass.DEP_CONFLICT,
            "suspect_paths": ("widget-core/build.gradle", "gradle/libs.versions.toml"),
        },
    ),
    GoldenCase(
        role=Role.BUILD_DIAGNOSIS,
        schema=LlmBuildDiagnosis,
        backend="openai_compatible",
        rung=StructuredOutputMode.PROMPTED,
        fixture="build_diagnosis_openai_compatible_prompted.json",
        finish_reason="stop",
        expected={
            "failure_class": FailureClass.PARSE_ERROR,
            "confidence": 0.91,
        },
    ),
    # --- DEP_DISAMBIGUATE (round IV task 2, new) ---
    GoldenCase(
        role=Role.DEP_DISAMBIGUATE,
        schema=DependencyDisambiguation,
        backend="anthropic",
        rung=StructuredOutputMode.TOOL_CALL,
        fixture="dep_disambiguate_anthropic_tool_call.json",
        finish_reason="tool_call",
        expected={
            "chosen_coordinate": "maven:org.apache.commons:commons-lang3",
            "candidates_considered": (
                "maven:org.apache.commons:commons-lang3",
                "maven:commons-lang:commons-lang",
            ),
        },
    ),
    GoldenCase(
        role=Role.DEP_DISAMBIGUATE,
        schema=DependencyDisambiguation,
        backend="bedrock",
        rung=StructuredOutputMode.TOOL_CALL,
        fixture="dep_disambiguate_bedrock_tool_call.json",
        finish_reason="tool_call",
        expected={
            "chosen_coordinate": "npm::left-pad",
            "confidence": 0.93,
        },
    ),
    GoldenCase(
        role=Role.DEP_DISAMBIGUATE,
        schema=DependencyDisambiguation,
        backend="vertex",
        rung=StructuredOutputMode.TOOL_CALL,
        fixture="dep_disambiguate_vertex_tool_call.json",
        finish_reason="tool_call",
        expected={
            "chosen_coordinate": "gradle:com.acme.widget:widget-core",
            "candidates_considered": (
                "gradle:com.acme.widget:widget-core",
                "gradle:com.acme.widget:widget-core-legacy",
            ),
        },
    ),
    GoldenCase(
        role=Role.DEP_DISAMBIGUATE,
        schema=DependencyDisambiguation,
        backend="openai_compatible",
        rung=StructuredOutputMode.PROMPTED,
        fixture="dep_disambiguate_openai_compatible_prompted.json",
        finish_reason="stop",
        expected={
            # Deliberately exercises the nullable `chosen_coordinate` branch — "I cannot tell" is
            # a legitimate answer per the schema's own docstring (src/fleet/llm/schemas.py:99).
            "chosen_coordinate": None,
            "candidates_considered": ("pypi::flask-utils", "pypi::flask_utils2"),
        },
    ),
    # --- CYCLE_BREAK_PROPOSAL (round IV task 3, new) ---
    GoldenCase(
        role=Role.CYCLE_BREAK_PROPOSAL,
        schema=CycleBreakProposal,
        backend="anthropic",
        rung=StructuredOutputMode.TOOL_CALL,
        fixture="cycle_break_proposal_anthropic_tool_call.json",
        finish_reason="tool_call",
        expected={
            "strategy": BreakStrategy.CONTRACT_HOIST,
            "broken_edge_ids": (),
            "hoisted_contract_ids": ("proto:acme.v1",),
        },
    ),
    GoldenCase(
        role=Role.CYCLE_BREAK_PROPOSAL,
        schema=CycleBreakProposal,
        backend="bedrock",
        rung=StructuredOutputMode.TOOL_CALL,
        fixture="cycle_break_proposal_bedrock_tool_call.json",
        finish_reason="tool_call",
        expected={
            "strategy": BreakStrategy.EDGE_BREAK,
            "broken_edge_ids": ("e42",),
            "hoisted_contract_ids": (),
        },
    ),
    GoldenCase(
        role=Role.CYCLE_BREAK_PROPOSAL,
        schema=CycleBreakProposal,
        backend="vertex",
        rung=StructuredOutputMode.TOOL_CALL,
        fixture="cycle_break_proposal_vertex_tool_call.json",
        finish_reason="tool_call",
        expected={
            "strategy": BreakStrategy.ATOMIC_WAVE,
            "broken_edge_ids": (),
            "hoisted_contract_ids": (),
        },
    ),
    GoldenCase(
        role=Role.CYCLE_BREAK_PROPOSAL,
        schema=CycleBreakProposal,
        backend="openai_compatible",
        rung=StructuredOutputMode.PROMPTED,
        fixture="cycle_break_proposal_openai_compatible_prompted.json",
        finish_reason="stop",
        expected={
            "strategy": BreakStrategy.MANUAL,
            "broken_edge_ids": (),
            "hoisted_contract_ids": (),
        },
    ),
    # --- CONFLICT_RESOLUTION (round IV task 3, new) ---
    GoldenCase(
        role=Role.CONFLICT_RESOLUTION,
        schema=VersionConflictResolution,
        backend="anthropic",
        rung=StructuredOutputMode.TOOL_CALL,
        fixture="conflict_resolution_anthropic_tool_call.json",
        finish_reason="tool_call",
        expected={
            "coord_key": "maven:com.acme:commons-io",
            "proposed_version": "2.11.0",
            "mechanism": "bazel_dep",
            "violated_specs": ("[1.0,2.0)",),
        },
    ),
    GoldenCase(
        role=Role.CONFLICT_RESOLUTION,
        schema=VersionConflictResolution,
        backend="bedrock",
        rung=StructuredOutputMode.TOOL_CALL,
        fixture="conflict_resolution_bedrock_tool_call.json",
        finish_reason="tool_call",
        expected={
            "coord_key": "npm:left-pad",
            "proposed_version": "1.3.0",
            "mechanism": "single_version_override",
            "violated_specs": ("<1.2.0",),
        },
    ),
    GoldenCase(
        role=Role.CONFLICT_RESOLUTION,
        schema=VersionConflictResolution,
        backend="vertex",
        rung=StructuredOutputMode.TOOL_CALL,
        fixture="conflict_resolution_vertex_tool_call.json",
        finish_reason="tool_call",
        expected={
            "coord_key": "gradle:com.widget:widget-core",
            "proposed_version": "3.4.1",
            "mechanism": "bazel_dep",
            "violated_specs": ("[3.0,3.4)",),
        },
    ),
    GoldenCase(
        role=Role.CONFLICT_RESOLUTION,
        schema=VersionConflictResolution,
        backend="openai_compatible",
        rung=StructuredOutputMode.PROMPTED,
        fixture="conflict_resolution_openai_compatible_prompted.json",
        finish_reason="stop",
        expected={
            "coord_key": "pypi:flask-utils",
            "proposed_version": "0.9.2",
            "mechanism": "single_version_override",
            "violated_specs": ("==0.8.*",),
        },
    ),
    # --- API_INCOMPAT_REWRITE (round V task 2, new) ---
    GoldenCase(
        role=Role.API_INCOMPAT_REWRITE,
        schema=ApiRewriteProposal,
        backend="anthropic",
        rung=StructuredOutputMode.TOOL_CALL,
        fixture="api_incompat_rewrite_anthropic_tool_call.json",
        finish_reason="tool_call",
        expected={
            "coord_key": "maven:org.apache.commons:commons-io",
            "breaking_changes": ("IOUtils.copy(InputStream, OutputStream) removed in 2.11",),
        },
    ),
    GoldenCase(
        role=Role.API_INCOMPAT_REWRITE,
        schema=ApiRewriteProposal,
        backend="bedrock",
        rung=StructuredOutputMode.TOOL_CALL,
        fixture="api_incompat_rewrite_bedrock_tool_call.json",
        finish_reason="tool_call",
        expected={
            "coord_key": "npm:left-pad",
            "breaking_changes": (
                "leftPad(str, len) now requires an explicit third pad-character argument",
            ),
        },
    ),
    GoldenCase(
        role=Role.API_INCOMPAT_REWRITE,
        schema=ApiRewriteProposal,
        backend="vertex",
        rung=StructuredOutputMode.TOOL_CALL,
        fixture="api_incompat_rewrite_vertex_tool_call.json",
        finish_reason="tool_call",
        expected={
            "coord_key": "gradle:com.acme.widget:widget-core",
            "breaking_changes": ("WidgetFactory.create(String) static factory removed",),
        },
    ),
    GoldenCase(
        role=Role.API_INCOMPAT_REWRITE,
        schema=ApiRewriteProposal,
        backend="openai_compatible",
        rung=StructuredOutputMode.PROMPTED,
        fixture="api_incompat_rewrite_openai_compatible_prompted.json",
        finish_reason="stop",
        expected={
            "coord_key": "pypi:flask-utils",
            "breaking_changes": (
                "escape() renamed to markup_escape() with no backward-compatible alias",
            ),
        },
    ),
    # --- ESCALATION (round V task 2, new) ---
    GoldenCase(
        role=Role.ESCALATION,
        schema=LlmEscalationProposal,
        backend="anthropic",
        rung=StructuredOutputMode.TOOL_CALL,
        fixture="escalation_anthropic_tool_call.json",
        finish_reason="tool_call",
        expected={
            "abandon_recommended": True,
            "human_intervention_reason": "the correct charset cannot be determined from the "
            "repository alone; a human must confirm whether UTF-8 is safe for every caller of "
            "FileSync.java before this patch is trusted",
        },
    ),
    GoldenCase(
        role=Role.ESCALATION,
        schema=LlmEscalationProposal,
        backend="bedrock",
        rung=StructuredOutputMode.TOOL_CALL,
        fixture="escalation_bedrock_tool_call.json",
        finish_reason="tool_call",
        expected={
            "abandon_recommended": False,
            "human_intervention_reason": None,
        },
    ),
    GoldenCase(
        role=Role.ESCALATION,
        schema=LlmEscalationProposal,
        backend="vertex",
        rung=StructuredOutputMode.TOOL_CALL,
        fixture="escalation_vertex_tool_call.json",
        finish_reason="tool_call",
        expected={
            "abandon_recommended": True,
            "human_intervention_reason": "cannot confirm whether any caller outside this repo "
            "relied on the factory's old default description value without a human checking "
            "downstream consumers",
        },
    ),
    GoldenCase(
        role=Role.ESCALATION,
        schema=LlmEscalationProposal,
        backend="openai_compatible",
        rung=StructuredOutputMode.PROMPTED,
        fixture="escalation_openai_compatible_prompted.json",
        finish_reason="stop",
        expected={
            "abandon_recommended": False,
            "human_intervention_reason": None,
        },
    ),
    # --- TRANSFORM_REPAIR (round V task 1, new) — nested-object tuple field (`files`) ---
    GoldenCase(
        role=Role.TRANSFORM_REPAIR,
        schema=LlmPatchProposal,
        backend="anthropic",
        rung=StructuredOutputMode.TOOL_CALL,
        fixture="transform_repair_anthropic_tool_call.json",
        finish_reason="tool_call",
        expected={
            "files": (
                ProposedFileEdit(
                    path="pom.xml",
                    diff="--- a/pom.xml\n+++ b/pom.xml\n@@ -10,6 +10,10 @@\n"
                    "   <dependencies>\n+    <dependency>\n+      "
                    "<groupId>org.apache.commons</groupId>\n+      "
                    "<artifactId>commons-lang3</artifactId>\n+      "
                    "<version>3.12.0</version>\n+    </dependency>\n   </dependencies>\n",
                ),
                ProposedFileEdit(
                    path="src/main/java/com/acme/Util.java",
                    diff="--- a/src/main/java/com/acme/Util.java\n"
                    "+++ b/src/main/java/com/acme/Util.java\n@@ -1,6 +1,6 @@\n package com.acme;\n"
                    " \n-import com.acme.util.StringHelper;\n"
                    "+import org.apache.commons.lang3.StringUtils;\n \n public class Util {\n",
                ),
            ),
            "approach_summary": "Declare the missing commons-lang3 Maven dependency and repoint "
            "Util.java's import at it",
        },
    ),
    GoldenCase(
        role=Role.TRANSFORM_REPAIR,
        schema=LlmPatchProposal,
        backend="bedrock",
        rung=StructuredOutputMode.TOOL_CALL,
        fixture="transform_repair_bedrock_tool_call.json",
        finish_reason="tool_call",
        expected={
            "files": (
                ProposedFileEdit(
                    path="index.js",
                    diff="--- a/index.js\n+++ b/index.js\n@@ -1,7 +1,7 @@\n"
                    " function leftPad(str, len, ch) {\n-  ch = ch || '0';\n+  ch = ch || ' ';\n"
                    "   str = String(str);\n   if (str.length >= len) return str;\n"
                    "   return Array(len - str.length + 1).join(ch) + str;\n }\n",
                ),
                ProposedFileEdit(
                    path="test/index.test.js",
                    diff="--- a/test/index.test.js\n+++ b/test/index.test.js\n@@ -3,6 +3,6 @@\n"
                    " describe('leftPad', () => {\n"
                    "   it('pads with the default character', () => {\n"
                    "-    assert.equal(leftPad('1', 3), '001');\n"
                    "+    assert.equal(leftPad('1', 3), '  1');\n   });\n });\n",
                ),
            ),
            "approach_summary": "Change the default pad character from '0' to ' ' and update the "
            "matching test expectation",
        },
    ),
    GoldenCase(
        role=Role.TRANSFORM_REPAIR,
        schema=LlmPatchProposal,
        backend="vertex",
        rung=StructuredOutputMode.TOOL_CALL,
        fixture="transform_repair_vertex_tool_call.json",
        finish_reason="tool_call",
        expected={
            "files": (
                ProposedFileEdit(
                    path="widget-core/build.gradle",
                    diff="--- a/widget-core/build.gradle\n+++ b/widget-core/build.gradle\n"
                    "@@ -12,7 +12,7 @@\n dependencies {\n"
                    "-    implementation 'com.acme.widget:widget-core-legacy:2.9.0'\n"
                    "+    implementation 'com.acme.widget:widget-core:3.4.1'\n }\n",
                ),
                ProposedFileEdit(
                    path="gradle/libs.versions.toml",
                    diff="--- a/gradle/libs.versions.toml\n+++ b/gradle/libs.versions.toml\n"
                    '@@ -4,7 +4,7 @@\n [versions]\n-widget-core = "2.9.0"\n'
                    '+widget-core = "3.4.1"\n',
                ),
            ),
            "approach_summary": "Repoint widget-core/build.gradle and the version catalog at the "
            "current widget-core coordinate and version",
        },
    ),
    GoldenCase(
        role=Role.TRANSFORM_REPAIR,
        schema=LlmPatchProposal,
        backend="openai_compatible",
        rung=StructuredOutputMode.PROMPTED,
        fixture="transform_repair_openai_compatible_prompted.json",
        finish_reason="stop",
        expected={
            "files": (
                ProposedFileEdit(
                    path="setup.py",
                    diff="--- a/setup.py\n+++ b/setup.py\n@@ -1,6 +1,6 @@\n"
                    " from setuptools import setup\n \n"
                    '-setup(name="flask-utils", version=VERSION_UNRESOLVED)\n'
                    '+setup(name="flask-utils", version="0.9.2")\n',
                ),
                ProposedFileEdit(
                    path="flask_utils/__init__.py",
                    diff="--- a/flask_utils/__init__.py\n+++ b/flask_utils/__init__.py\n"
                    "@@ -1,4 +1,4 @@\n-from .compat import *  # noqa: F401,F403\n"
                    "+from .utils import *  # noqa: F401,F403\n \n"
                    ' __version__ = "0.9.2"\n',
                ),
            ),
            "approach_summary": "Pin setup.py's version literal and repoint the package's "
            "star-import at the renamed utils module",
        },
    ),
    # --- MANIFEST_EXTRACT (round V task 1, new) — nested-object tuple field (`dependencies`) ---
    GoldenCase(
        role=Role.MANIFEST_EXTRACT,
        schema=ManifestExtraction,
        backend="anthropic",
        rung=StructuredOutputMode.TOOL_CALL,
        fixture="manifest_extract_anthropic_tool_call.json",
        finish_reason="tool_call",
        expected={
            "ecosystem": Ecosystem.MAVEN,
            "dependencies": (
                ExtractedDependency(
                    coordinate="maven:org.apache.commons:commons-lang3",
                    version="3.12.0",
                    scope="compile",
                ),
                ExtractedDependency(coordinate="maven:junit:junit", version="4.13.2", scope="test"),
            ),
            "unparsed_reason": None,
        },
    ),
    GoldenCase(
        role=Role.MANIFEST_EXTRACT,
        schema=ManifestExtraction,
        backend="bedrock",
        rung=StructuredOutputMode.TOOL_CALL,
        fixture="manifest_extract_bedrock_tool_call.json",
        finish_reason="tool_call",
        expected={
            "ecosystem": Ecosystem.NPM,
            "dependencies": (
                ExtractedDependency(
                    coordinate="npm::left-pad", version="1.3.0", scope="dependencies"
                ),
                ExtractedDependency(
                    coordinate="npm::mocha", version="^9.0.0", scope="devDependencies"
                ),
            ),
        },
    ),
    GoldenCase(
        role=Role.MANIFEST_EXTRACT,
        schema=ManifestExtraction,
        backend="vertex",
        rung=StructuredOutputMode.TOOL_CALL,
        fixture="manifest_extract_vertex_tool_call.json",
        finish_reason="tool_call",
        expected={
            "ecosystem": Ecosystem.GRADLE,
            "dependencies": (
                ExtractedDependency(
                    coordinate="gradle:com.acme.widget:widget-core",
                    version="3.4.1",
                    scope="implementation",
                ),
                # Deliberately exercises `ExtractedDependency.version`'s nullable branch — a
                # BOM-managed coordinate whose version is inherited, not stated at this call site.
                ExtractedDependency(
                    coordinate="gradle:org.jetbrains.kotlin:kotlin-stdlib",
                    version=None,
                    scope="implementation",
                ),
            ),
        },
    ),
    GoldenCase(
        role=Role.MANIFEST_EXTRACT,
        schema=ManifestExtraction,
        backend="openai_compatible",
        rung=StructuredOutputMode.PROMPTED,
        fixture="manifest_extract_openai_compatible_prompted.json",
        finish_reason="stop",
        expected={
            "ecosystem": Ecosystem.PYPI,
            # Deliberately exercises the nullable `unparsed_reason` decline branch — an empty
            # `dependencies` tuple plus a stated reason is a legitimate answer per the schema's
            # own docstring (src/fleet/llm/schemas.py:139-144), not a degenerate one.
            "dependencies": (),
            "unparsed_reason": "setup.py computes the version and install_requires list via a "
            "subprocess call to a build-time script; static extraction cannot evaluate it and a "
            "guessed list would be unrefutable",
        },
    ),
    # --- BUILD_AUTHORING (round V task 5, new) — TWO-LEVEL nested-object tuple: `targets` is a
    # tuple of `BuildTargetProposal`, each carrying three sibling tuple[str, ...] fields of its
    # own (`srcs`/`deps`/`visibility`). Structurally the heaviest of the 12 roles. ---
    GoldenCase(
        role=Role.BUILD_AUTHORING,
        schema=BuildFileProposal,
        backend="anthropic",
        rung=StructuredOutputMode.TOOL_CALL,
        fixture="build_authoring_anthropic_tool_call.json",
        finish_reason="tool_call",
        expected={
            "package_path": "java/acme/legacy-shim",
            "targets": (
                BuildTargetProposal(
                    name="legacy-shim",
                    rule="java_library",
                    srcs=("StringHelper.java", "LegacyUtil.java"),
                    deps=("//java/acme/common:acme-base",),
                    visibility=("//java/acme:__subpackages__",),
                ),
                BuildTargetProposal(
                    name="legacy-shim_test",
                    rule="java_test",
                    srcs=("LegacyShimTest.java",),
                    deps=(":legacy-shim", "//third_party/junit"),
                    visibility=("//visibility:private",),
                ),
            ),
        },
    ),
    GoldenCase(
        role=Role.BUILD_AUTHORING,
        schema=BuildFileProposal,
        backend="bedrock",
        rung=StructuredOutputMode.TOOL_CALL,
        fixture="build_authoring_bedrock_tool_call.json",
        finish_reason="tool_call",
        expected={
            "package_path": "js/vendor/widgets-bundle",
            "targets": (
                BuildTargetProposal(
                    name="widgets-bundle",
                    rule="js_library",
                    srcs=("widgets.min.js",),
                    deps=(),
                    visibility=("//js:__subpackages__",),
                ),
            ),
        },
    ),
    GoldenCase(
        role=Role.BUILD_AUTHORING,
        schema=BuildFileProposal,
        backend="vertex",
        rung=StructuredOutputMode.TOOL_CALL,
        fixture="build_authoring_vertex_tool_call.json",
        finish_reason="tool_call",
        expected={
            "package_path": "gradle/tools/codegen-legacy",
            "targets": (
                BuildTargetProposal(
                    name="codegen-legacy",
                    rule="java_library",
                    srcs=("Codegen.java", "Templates.java"),
                    deps=("//gradle/tools/common:codegen-support",),
                    visibility=("//visibility:public",),
                ),
            ),
        },
    ),
    GoldenCase(
        role=Role.BUILD_AUTHORING,
        schema=BuildFileProposal,
        backend="openai_compatible",
        rung=StructuredOutputMode.PROMPTED,
        fixture="build_authoring_openai_compatible_prompted.json",
        finish_reason="stop",
        expected={
            "package_path": "python/tools/legacy_scripts",
            "targets": (
                BuildTargetProposal(
                    name="legacy_scripts",
                    rule="py_library",
                    srcs=("report.py", "cleanup.py"),
                    deps=(),
                    visibility=("//python/tools:__subpackages__",),
                ),
            ),
        },
    ),
)


@pytest.mark.parametrize("case", GOLDEN_CASES, ids=lambda c: f"{c.role.value}-{c.backend}")
def test_golden_response_parses_and_validates(case: GoldenCase) -> None:
    """One recorded raw response per (role, shipped backend), parsed through REAL adapter code,
    validated through the REAL `client._validate` — the function every live call's schema check
    runs through."""
    raw = _load(case.fixture)
    reply = _BACKEND_PARSE[case.backend](raw, _BACKEND_TARGET[case.backend]())

    value = client_module._validate(reply, case.schema, case.rung)

    for attr, expected in case.expected.items():
        assert getattr(value, attr) == expected, attr
    assert reply.finish_reason == case.finish_reason


# ---------------------------------------------------------------------------------------------
# Rule 12 — at least one fixture per role added in this task must be a genuine discriminator:
# mutate it to violate the schema and confirm validation genuinely fails, not merely "the test
# ran". Round II's own established pattern (mutate one field with no default, confirm
# `ValidationError`, confirm the unmutated fixture still validates as the control).
# ---------------------------------------------------------------------------------------------


def test_a_tool_call_reply_missing_a_required_field_fails_schema_validation() -> None:
    """Mutation of the bedrock REPO_CLASSIFY fixture: delete `confidence`, a `RepoClassification`
    field with no default (`Field(ge=0.0, le=1.0)`, no `default=`). If schema validation above
    were a no-op — e.g. `_validate` silently accepting anything shaped like a dict — this would
    pass instead of raising, and the "validates against its declared schema" assertions above
    would prove nothing beyond "code ran without crashing"."""
    raw = _load("bedrock_tool_call.json")
    del raw["output"]["message"]["content"][0]["toolUse"]["input"]["confidence"]
    reply = bedrock_parse_reply(raw, bedrock_target())

    with pytest.raises(ValidationError, match="confidence"):
        client_module._validate(reply, RepoClassification, StructuredOutputMode.TOOL_CALL)


def test_the_unmutated_fixture_still_validates_so_the_mutation_above_is_what_broke_it() -> None:
    """Old-passes/new-fails discriminator (Rule 12): the identical fixture, unmutated, parsed and
    validated the same way, must succeed — proving the failure above is caused by the deleted
    `confidence` field and not by some unrelated defect in the fixture, the parse path, or
    `_validate` itself. This is the bedrock `REPO_CLASSIFY` row of `GOLDEN_CASES` restated as the
    control half of the mutation pair, kept next to it deliberately."""
    raw = _load("bedrock_tool_call.json")
    reply = bedrock_parse_reply(raw, bedrock_target())

    client_module._validate(reply, RepoClassification, StructuredOutputMode.TOOL_CALL)


def test_a_pr_body_reply_missing_its_required_body_field_fails_schema_validation() -> None:
    """Mutation of the bedrock `PR_BODY` fixture: delete `body`, the one `PrBody` field with no
    default (`Field(min_length=1, max_length=20_000)`; `highlights` has `default=()` and would NOT
    discriminate — deleting it leaves a still-valid payload). Same discriminator shape as the
    `REPO_CLASSIFY` mutation above, applied to one of the two roles this task adds."""
    raw = _load("pr_body_bedrock_tool_call.json")
    del raw["output"]["message"]["content"][0]["toolUse"]["input"]["body"]
    reply = bedrock_parse_reply(raw, bedrock_target())

    with pytest.raises(ValidationError, match="body"):
        client_module._validate(reply, PrBody, StructuredOutputMode.TOOL_CALL)


def test_the_unmutated_pr_body_fixture_still_validates_so_the_mutation_above_is_what_broke_it() -> (
    None
):
    """Control half of the `PR_BODY` mutation pair: the identical fixture, unmutated, must still
    validate — proving the failure above is caused by the deleted `body` field, not by an
    unrelated defect in the fixture, the parse path, or `_validate` itself."""
    raw = _load("pr_body_bedrock_tool_call.json")
    reply = bedrock_parse_reply(raw, bedrock_target())

    client_module._validate(reply, PrBody, StructuredOutputMode.TOOL_CALL)


def test_a_build_diagnosis_reply_missing_root_cause_fails_schema_validation() -> None:
    """Mutation of the bedrock `BUILD_DIAGNOSIS` fixture: delete `root_cause`, one of the three
    `LlmBuildDiagnosis` fields with no default (`failure_class`, `root_cause`,
    `suggested_action`, `confidence` all lack a `default=`; `suspect_paths` alone defaults to `()`
    and would NOT discriminate — deleting it leaves a still-valid payload). Same discriminator
    shape as the `REPO_CLASSIFY`/`PR_BODY` mutations above, applied to one of the two roles this
    task adds (round IV task 2)."""
    raw = _load("build_diagnosis_bedrock_tool_call.json")
    del raw["output"]["message"]["content"][0]["toolUse"]["input"]["root_cause"]
    reply = bedrock_parse_reply(raw, bedrock_target())

    with pytest.raises(ValidationError, match="root_cause"):
        client_module._validate(reply, LlmBuildDiagnosis, StructuredOutputMode.TOOL_CALL)


def test_the_unmutated_build_diagnosis_fixture_still_validates_so_the_mutation_broke_it() -> None:
    """Control half of the `BUILD_DIAGNOSIS` mutation pair: the identical fixture, unmutated,
    must still validate — proving the failure above is caused by the deleted `root_cause` field,
    not by an unrelated defect in the fixture, the parse path, or `_validate` itself."""
    raw = _load("build_diagnosis_bedrock_tool_call.json")
    reply = bedrock_parse_reply(raw, bedrock_target())

    client_module._validate(reply, LlmBuildDiagnosis, StructuredOutputMode.TOOL_CALL)


def test_a_conflict_resolution_reply_with_an_invalid_mechanism_fails_schema_validation() -> None:
    """Mutation of the bedrock `CONFLICT_RESOLUTION` fixture: `mechanism`
    (`VersionConflictResolution`, `src/fleet/llm/schemas.py:281-285`) carries
    `pattern=r"^(bazel_dep|single_version_override)$"` — a closed enumeration expressed as a
    string field, not a missing-field constraint. This mutates it to `"override_all"`: a non-empty
    string that satisfies every OTHER constraint on the field (it is a `str`, present, non-empty)
    and would pass a schema-shape-only check, but fails the closed pattern. That is a genuinely
    different failure mode than the missing-required-field mutations above — this is the one
    Rule 12 exists to force onto a pattern-constrained field."""
    raw = _load("conflict_resolution_bedrock_tool_call.json")
    raw["output"]["message"]["content"][0]["toolUse"]["input"]["mechanism"] = "override_all"
    reply = bedrock_parse_reply(raw, bedrock_target())

    with pytest.raises(ValidationError, match="mechanism"):
        client_module._validate(reply, VersionConflictResolution, StructuredOutputMode.TOOL_CALL)


def test_the_unmutated_conflict_resolution_fixture_still_validates_after_the_mutation() -> None:
    """Control half of the `CONFLICT_RESOLUTION` mutation pair: the identical fixture, unmutated,
    must still validate — proving the failure above is caused by the invalid `mechanism` value,
    not by an unrelated defect in the fixture, the parse path, or `_validate` itself."""
    raw = _load("conflict_resolution_bedrock_tool_call.json")
    reply = bedrock_parse_reply(raw, bedrock_target())

    client_module._validate(reply, VersionConflictResolution, StructuredOutputMode.TOOL_CALL)


def test_an_api_rewrite_reply_with_an_oversized_coord_key_fails_schema_validation() -> None:
    """Mutation of the bedrock `API_INCOMPAT_REWRITE` fixture: `coord_key`
    (`ApiRewriteProposal`, `src/fleet/llm/schemas.py:222`) carries `max_length=512` — a
    SUBCLASS-specific constraint, not one inherited from `LlmPatchProposal`. This mutates it to a
    600-character string, past the bound, while leaving every inherited base field
    (`files`/`approach_summary`/`rationale`) untouched and valid. If schema validation only
    checked `LlmPatchProposal`'s own base fields — e.g. a `_validate` call that resolved the base
    class instead of the declared subclass — this mutation would pass instead of raising, proving
    the subclass's own added constraints are genuinely checked and not just inherited ones."""
    raw = _load("api_incompat_rewrite_bedrock_tool_call.json")
    raw["output"]["message"]["content"][0]["toolUse"]["input"]["coord_key"] = "n" * 600

    reply = bedrock_parse_reply(raw, bedrock_target())

    with pytest.raises(ValidationError, match="coord_key"):
        client_module._validate(reply, ApiRewriteProposal, StructuredOutputMode.TOOL_CALL)


def test_the_unmutated_api_rewrite_fixture_still_validates_after_the_mutation_above() -> None:
    """Control half of the `API_INCOMPAT_REWRITE` mutation pair: the identical fixture,
    unmutated, must still validate — proving the failure above is caused by the oversized
    `coord_key`, not by an unrelated defect in the fixture, the parse path, or `_validate`
    itself."""
    raw = _load("api_incompat_rewrite_bedrock_tool_call.json")
    reply = bedrock_parse_reply(raw, bedrock_target())

    client_module._validate(reply, ApiRewriteProposal, StructuredOutputMode.TOOL_CALL)


def test_a_transform_repair_reply_missing_a_nested_diff_field_fails_schema_validation() -> None:
    """Mutation of the bedrock `TRANSFORM_REPAIR` fixture: delete `files[0].diff`, the one
    `ProposedFileEdit` field with no default (`Field(min_length=1)`; `ProposedFileEdit.path` also
    lacks a default and would discriminate too, but `diff` is targeted deliberately — it is the
    field this task's nested-object-tuple investigation flagged as the genuinely more complex
    shape (a required field one level INSIDE a tuple entry, not a top-level required field like
    every mutation above). If `_validate`'s Pydantic call only checked the outer `LlmPatchProposal`
    shape (e.g. "is `files` a non-empty tuple") without descending into each entry, this would pass
    instead of raising."""
    raw = _load("transform_repair_bedrock_tool_call.json")
    del raw["output"]["message"]["content"][0]["toolUse"]["input"]["files"][0]["diff"]
    reply = bedrock_parse_reply(raw, bedrock_target())

    with pytest.raises(ValidationError, match="diff"):
        client_module._validate(reply, LlmPatchProposal, StructuredOutputMode.TOOL_CALL)


def test_the_unmutated_transform_repair_fixture_still_validates_after_the_mutation() -> None:
    """Control half of the `TRANSFORM_REPAIR` mutation pair: the identical fixture, unmutated
    (both `files` entries intact), must still validate — proving the failure above is caused by
    the deleted nested `diff` field, not by an unrelated defect in the fixture, the parse path, or
    `_validate` itself."""
    raw = _load("transform_repair_bedrock_tool_call.json")
    reply = bedrock_parse_reply(raw, bedrock_target())

    client_module._validate(reply, LlmPatchProposal, StructuredOutputMode.TOOL_CALL)


def test_a_build_authoring_reply_missing_a_nested_target_field_fails_schema_validation() -> None:
    """Mutation of the bedrock `BUILD_AUTHORING` fixture: delete `targets[0].name`, one of the two
    `BuildTargetProposal` fields with no default (`name`/`rule` both lack a `default=`;
    `srcs`/`deps`/`visibility` all default to `()` and would NOT discriminate — deleting any of
    them leaves a still-valid payload). `BuildFileProposal.targets` is a TWO-LEVEL nested-object
    tuple — `targets` is `tuple[BuildTargetProposal, ...]`, and this mutation reaches one field
    inside one tuple ENTRY, not the outer `targets` tuple itself (deleting the whole `targets` list
    or emptying it would only prove the OUTER `min_length=1` constraint, already exercised by every
    other role's outer-tuple mutation). If `_validate`'s Pydantic call only checked the outer
    `BuildFileProposal` shape (e.g. "is `targets` a non-empty tuple") without descending into each
    entry's own required fields, this would pass instead of raising — following
    `TRANSFORM_REPAIR`'s own precedent (round V task 1 mutated inside `ProposedFileEdit`, not just
    `LlmPatchProposal` itself)."""
    raw = _load("build_authoring_bedrock_tool_call.json")
    del raw["output"]["message"]["content"][0]["toolUse"]["input"]["targets"][0]["name"]
    reply = bedrock_parse_reply(raw, bedrock_target())

    with pytest.raises(ValidationError, match="name"):
        client_module._validate(reply, BuildFileProposal, StructuredOutputMode.TOOL_CALL)


def test_the_unmutated_build_authoring_fixture_still_validates_after_the_mutation() -> None:
    """Control half of the `BUILD_AUTHORING` mutation pair: the identical fixture, unmutated (the
    single `targets` entry's `name` intact), must still validate — proving the failure above is
    caused by the deleted nested `name` field, not by an unrelated defect in the fixture, the
    parse path, or `_validate` itself."""
    raw = _load("build_authoring_bedrock_tool_call.json")
    reply = bedrock_parse_reply(raw, bedrock_target())

    client_module._validate(reply, BuildFileProposal, StructuredOutputMode.TOOL_CALL)
