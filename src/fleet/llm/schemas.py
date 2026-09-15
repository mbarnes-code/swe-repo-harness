"""Per-role response contracts — one Pydantic model per `Role`, and the registry binding them.

One declaration, two jobs (ADR-0002): the schema handed to the model *is* the validator applied
to its reply. LLM output is untrusted text until it validates, so every model here inherits
`FleetModel` and therefore `extra="forbid"` — a reply carrying a field nobody declared is a
rejected reply, not a silently-dropped key. That matters more here than anywhere else in the
harness: an invented field is precisely how a model smuggles a claim past a reviewer.

**Shape rules these models follow, and why:**

* Sequences are `tuple[...]`, never `list`, because a response is a value that gets hashed into
  a digest and compared across runs (§11.6); a mutable field invites a caller to edit the model's
  answer in place and lose the distinction between what was said and what was decided.
* Free prose is length-bounded. A `rationale` is a reviewer's one-liner, not a place for the model
  to restate the evidence — and an unbounded string is an unbounded row.
* Nothing here has a field a *harness* fact would go in. `FilePatch.parse_probe_ok` is the worked
  example: it is the outcome of a probe **we** run, so a model that could assert it could assert
  its own patch parses. That is why `ProposedFileEdit` exists instead of reusing `FilePatch`.
* Enum-valued judgments reuse the existing closed vocabularies (`Ecosystem`, `FailureClass`,
  `BreakStrategy`), so a model cannot invent a nineteenth failure class and no mapping table has
  to be maintained beside `models/enums.py`.

Field *sets* below are an **Agent Recommendation** (CLAUDE.md guardrail 1): SPEC §9 fixes the
role list and §7.7 fixes that every reply is a validated Pydantic object, but it does not
enumerate each role's fields. They are chosen to be the minimum the consuming code needs.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Annotated, Final

from pydantic import BaseModel, Field

from fleet.llm.roles import Role
from fleet.models.base import FleetModel
from fleet.models.build import BAZEL_RULE_PATTERN
from fleet.models.enums import BreakStrategy, Ecosystem, FailureClass
from fleet.util.hashing import sha256_text

__all__ = [
    "RESPONSE_SCHEMAS",
    "ApiRewriteProposal",
    "BuildFileProposal",
    "BuildTargetProposal",
    "CycleBreakProposal",
    "DependencyDisambiguation",
    "ExtractedDependency",
    "LlmBuildDiagnosis",
    "LlmEscalationProposal",
    "LlmPatchProposal",
    "ManifestExtraction",
    "PrBody",
    "PrTitle",
    "ProposedFileEdit",
    "RepoClassification",
    "VersionConflictResolution",
    "response_model_for",
    "response_schema_sha256",
]

Rationale = Annotated[
    str,
    Field(
        min_length=1,
        max_length=600,
        description="Why, in a reviewer's terms. Not a restatement of the evidence.",
    ),
]
"""One reusable annotated type rather than a shared `Field(...)` object: a `FieldInfo` instance
handed to several models is shared mutable state, and `Annotated` is the supported way to say
"this constraint, everywhere"."""


# ---------------------------------------------------------------------------------------------
# CHEAP tier — classification and disambiguation (ADR-0008: cheapest model that can be right)
# ---------------------------------------------------------------------------------------------


class RepoClassification(FleetModel):
    """`repo_classify`: what ecosystem a repo belongs to when the manifest scan is ambiguous.

    `confidence` is mandatory and bounded because §3.1 gates on it: a low-confidence
    classification becomes a weak edge surfaced to a reviewer rather than a fact, and a model that
    could omit the number would have every guess treated as certainty."""

    ecosystem: Ecosystem
    is_library: bool = Field(
        description="True if the repo publishes a coordinate others consume; False for a leaf "
        "application. Drives whether an unresolved consumer is a blocker or a leaf.",
    )
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: Rationale


class DependencyDisambiguation(FleetModel):
    """`dep_disambiguate`: one raw dependency string, several candidate coordinates, pick one.

    `chosen_coordinate` is nullable on purpose. "I cannot tell" is a legitimate, useful answer
    that keeps the edge weak; forcing a non-null choice manufactures confidence the fleet then
    treats as an ordering constraint."""

    chosen_coordinate: str | None = Field(
        default=None,
        max_length=512,
        description="A `Coordinate.key`, or None when no candidate is defensible.",
    )
    candidates_considered: tuple[str, ...] = Field(default=(), max_length=32)
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: Rationale


class PrTitle(FleetModel):
    """`pr_title`: `max_length` matches `PullRequestDraft.title` exactly, so a reply that
    validates here cannot fail validation one layer later where the fix is a re-ask nobody
    budgeted for."""

    title: str = Field(min_length=1, max_length=120)


# ---------------------------------------------------------------------------------------------
# WORKHORSE tier — extraction, diagnosis, prose, and the attempt-2 repair
# ---------------------------------------------------------------------------------------------


class ExtractedDependency(FleetModel):
    """One dependency read out of a manifest the deterministic adapters could not parse."""

    coordinate: str = Field(min_length=1, max_length=512)
    version: str | None = Field(default=None, max_length=128)
    scope: str | None = Field(
        default=None,
        max_length=64,
        description="Ecosystem-native scope/configuration verbatim (`test`, `devDependencies`, "
        "…). Not normalised by the model: mapping it is `ManifestAdapter`'s job (§7.3).",
    )


class ManifestExtraction(FleetModel):
    """`manifest_extract`: the §7.3 fallback when no `ManifestAdapter` can parse a build file.

    `unparsed_reason` exists so the model can decline. An extraction that invents plausible
    coordinates from an unreadable file is worse than no extraction: it produces graph edges
    nothing can refute."""

    ecosystem: Ecosystem
    dependencies: tuple[ExtractedDependency, ...] = Field(default=(), max_length=2048)
    unparsed_reason: str | None = Field(default=None, max_length=600)


class LlmBuildDiagnosis(FleetModel):
    """`build_diagnosis`: turn a build/test failure into a typed classification plus a lead.

    `failure_class` reuses `FailureClass` rather than free text because the retry ladder branches
    on it (ADR-0014) — `TRANSIENT_INFRA` does not increment `attempts` and `BUDGET_EXHAUSTED` is
    terminal, so a model emitting a new string would silently land in the `UNKNOWN` bucket."""

    failure_class: FailureClass
    root_cause: str = Field(min_length=1, max_length=600)
    suspect_paths: tuple[str, ...] = Field(default=(), max_length=64)
    suggested_action: str = Field(min_length=1, max_length=600)
    confidence: float = Field(ge=0.0, le=1.0)


class PrBody(FleetModel):
    """`pr_body`: reviewer-facing prose only. It carries NO field that could contradict the
    verification report — the stub banner, equivalence and dependency list are rendered from
    `VerificationReport` data by code (§3.4), because a disclosure a model can decline to write
    is not a disclosure."""

    body: str = Field(min_length=1, max_length=20_000)
    highlights: tuple[str, ...] = Field(default=(), max_length=16)


class ProposedFileEdit(FleetModel):
    """One file's proposed edit as the model stated it.

    Deliberately NOT `FilePatch`: that model carries `tier` and `parse_probe_ok`, which are facts
    the *harness* establishes after the fact. A schema that let a model set `parse_probe_ok` would
    let it certify its own patch. Code lifts these into `FilePatch` once the probe has actually
    run (§3.2 step 4)."""

    path: str = Field(min_length=1, max_length=1024)
    diff: str = Field(
        min_length=1,
        description="Unified diff, the ONLY accepted patch representation (`FilePatch.diff`). "
        "Unbounded on purpose: a length cap here rejects the large legitimate rewrites this "
        "tier exists for.",
    )


class LlmPatchProposal(FleetModel):
    """`transform_repair` (ADR-0014 attempt 2): a unified diff plus its rationale.

    `approach_summary` is mandatory and short because ADR-0021's anti-anchoring loop feeds exactly
    this line — never the diff — into a later rung as a `RejectedApproach.reason`, whose own limit
    is 280 characters."""

    files: tuple[ProposedFileEdit, ...] = Field(min_length=1, max_length=64)
    approach_summary: str = Field(
        min_length=1,
        max_length=280,
        description="One line, approach level: what this attempt does. Carries into "
        "`RejectedApproach.reason` verbatim if the attempt is refuted, so no diff text.",
    )
    rationale: Rationale


# ---------------------------------------------------------------------------------------------
# HEAVY tier — the judgments the fleet cannot get wrong cheaply
# ---------------------------------------------------------------------------------------------


class ApiRewriteProposal(LlmPatchProposal):
    """`api_incompat_rewrite`: call sites rewritten against a dependency whose surface moved.

    Extends the patch shape rather than repeating it, and adds the two facts a reviewer needs to
    judge one: which coordinate moved, and which breaking changes the rewrite believes it is
    absorbing. A distinct class (not a reused one) because the `llm_cache` key includes the
    response schema hash — two roles sharing a class would share cache identity."""

    coord_key: str = Field(min_length=1, max_length=512)
    breaking_changes: tuple[str, ...] = Field(default=(), max_length=64)


class LlmEscalationProposal(LlmPatchProposal):
    """`escalation` (ADR-0014 attempt 3): the last automated rung, shown evidence plus the
    signatures of refuted approaches.

    `abandon_recommended` is why this is not just another patch proposal. Attempt 3 is the last
    one, so "this needs a human" must be expressible as a validated answer — otherwise the only
    way to say it is a bad patch, and Rule 11's `REQUIRES_HUMAN_INTERVENTION` gets reached by
    burning the retry budget instead of by being told."""

    abandon_recommended: bool = False
    human_intervention_reason: str | None = Field(default=None, max_length=600)


class BuildTargetProposal(FleetModel):
    """One Bazel target the model proposes. Rendered to Starlark by `bazel/render.py` — the model
    never emits build-file text, so the emitted file's shape stays deterministic (§3.3)."""

    name: str = Field(min_length=1, max_length=200)
    rule: str = Field(
        min_length=1,
        max_length=100,
        pattern=BAZEL_RULE_PATTERN,
        description="Bazel rule name, e.g. java_library, ts_project, go_test, filegroup. A "
        "conservative identifier pattern, not a closed set: real Bazel rule names are "
        "open-ended, but this is rendered verbatim as the head of a Starlark call, so anything "
        "that isn't a bare identifier is a Starlark-injection primitive.",
    )
    srcs: tuple[str, ...] = Field(default=(), max_length=4096)
    deps: tuple[str, ...] = Field(default=(), max_length=1024)
    visibility: tuple[str, ...] = Field(default=(), max_length=32)


class BuildFileProposal(FleetModel):
    """`build_authoring`: the §3.3 escape hatch when no `EcosystemAdapter` can derive targets."""

    package_path: str = Field(min_length=1, max_length=1024)
    targets: tuple[BuildTargetProposal, ...] = Field(min_length=1, max_length=256)
    rationale: Rationale


class CycleBreakProposal(FleetModel):
    """`cycle_break_proposal`: how to break one SCC.

    Cycle *detection* and the deterministic `break_cost` choice take no model input (§3.1 step 5);
    this role is asked only when the deterministic strategy is refused, and its answer is a
    proposal that code re-validates against the same edge set before anything is written."""

    strategy: BreakStrategy
    broken_edge_ids: tuple[str, ...] = Field(default=(), max_length=256)
    hoisted_contract_ids: tuple[str, ...] = Field(default=(), max_length=256)
    rationale: Rationale


class VersionConflictResolution(FleetModel):
    """`conflict_resolution`: the unsatisfiable-version-set role (§13 row 17).

    MVS decides every satisfiable case without a model. This runs only on an empty intersection,
    and its output is a *proposal*: `bazel/module.py` validates `proposed_version` against every
    contributing spec before writing it, so a pin that violates a declared upper bound is caught
    by code rather than shipped."""

    coord_key: str = Field(min_length=1, max_length=512)
    proposed_version: str = Field(min_length=1, max_length=128)
    mechanism: str = Field(
        pattern=r"^(bazel_dep|single_version_override)$",
        description="The MODULE.bazel construct to write. A closed pattern, not free text: these "
        "are the only two `bazel/module.py` knows how to emit.",
    )
    violated_specs: tuple[str, ...] = Field(
        default=(),
        max_length=64,
        description="Specs this pin knowingly breaks — the disclosure a reviewer needs when no "
        "version can satisfy everyone.",
    )
    rationale: Rationale


# ---------------------------------------------------------------------------------------------
# The registry: total over `Role`, checked at import
# ---------------------------------------------------------------------------------------------

RESPONSE_SCHEMAS: Final[Mapping[Role, type[FleetModel]]] = {
    Role.REPO_CLASSIFY: RepoClassification,
    Role.DEP_DISAMBIGUATE: DependencyDisambiguation,
    Role.PR_TITLE: PrTitle,
    Role.MANIFEST_EXTRACT: ManifestExtraction,
    Role.BUILD_DIAGNOSIS: LlmBuildDiagnosis,
    Role.PR_BODY: PrBody,
    Role.TRANSFORM_REPAIR: LlmPatchProposal,
    Role.API_INCOMPAT_REWRITE: ApiRewriteProposal,
    Role.ESCALATION: LlmEscalationProposal,
    Role.BUILD_AUTHORING: BuildFileProposal,
    Role.CYCLE_BREAK_PROPOSAL: CycleBreakProposal,
    Role.CONFLICT_RESOLUTION: VersionConflictResolution,
}

if set(RESPONSE_SCHEMAS) != set(Role):  # pragma: no cover - import-time totality gate
    missing = sorted(str(r) for r in set(Role) - set(RESPONSE_SCHEMAS))
    raise RuntimeError(f"RESPONSE_SCHEMAS is not total over Role; missing: {missing}")


def response_model_for(role: Role | str) -> type[FleetModel]:
    """The schema a role's reply must validate against. Unknown role is loud, like everywhere
    else on this boundary."""
    try:
        return RESPONSE_SCHEMAS[Role(role)]
    except ValueError as exc:
        raise KeyError(f"no response schema for role {role!r}") from exc


def response_schema_sha256(model: type[BaseModel]) -> str:
    """The `llm_cache` key component §6 names `response_schema_sha256`.

    Hashed over the JSON Schema with sorted keys and no whitespace, so the digest is a function of
    the schema's *meaning* and not of Pydantic's dict insertion order — otherwise a field
    reordering nobody meant as a change would invalidate the entire cache, and the "what actually
    invalidates a cached answer" story in §5 would be false.
    """
    schema = model.model_json_schema(mode="serialization")
    return sha256_text(json.dumps(schema, sort_keys=True, separators=(",", ":")))
