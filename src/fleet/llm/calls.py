"""The sanctioned per-role calls (ADR-0008): classify / extract / repair / diagnose / write_prose.

Models judge; code executes. Each helper below binds one `Role` to exactly two things — its
prompt template and its response schema — and then does nothing but `ModelClient.complete()`.
There is no branching, no retry and no provider knowledge here: all of that lives behind the §7.7
boundary, and a caller gets back a `ModelResponse[T]` whose `value` has already validated.

**Prompts are data.** `PROMPTS` is a table of `PromptTemplate` values, not f-strings spread
through the orchestration path, for three reasons: `prompt_template_version` is the cache's
invalidation knob (§5) and a version that lives beside prose nobody can find is a version nobody
bumps; a prompt embedded in a worker cannot be diffed when a run's quality changes; and §11.6's
`prompt_sha256` has to cover the *fully rendered* prompt, which requires one renderer.

**Rendering is deterministic, and that is a correctness property, not a nicety.** The cache key is
`sha256` over the rendered prompt, so a prompt that varies by dict order or by `set` iteration
does not produce a wrong answer — it produces a permanent cache miss, a re-paid corpus, and a
run whose cost triples for no visible reason. Hence: evidence is JSON with `sort_keys=True`,
`ensure_ascii=True` and no floating `NaN`; the only container types accepted are `JsonValue`, so
an unordered `set` cannot reach the renderer at all; and nothing here reads a clock, a UUID, an
environment variable or `id()`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Final

from pydantic import BaseModel, Field, JsonValue

from fleet.llm.client import CallBudget, Message, ModelClient, ModelResponse
from fleet.llm.roles import Role
from fleet.llm.schemas import (
    ApiRewriteProposal,
    BuildFileProposal,
    CycleBreakProposal,
    DependencyDisambiguation,
    LlmBuildDiagnosis,
    LlmEscalationProposal,
    LlmPatchProposal,
    ManifestExtraction,
    PrBody,
    PrTitle,
    RepoClassification,
    VersionConflictResolution,
)
from fleet.models.base import FleetModel
from fleet.models.enums import ModelTier
from fleet.obs.redact import redact_mapping
from fleet.util.hashing import sha256_text

__all__ = [
    "PROMPTS",
    "Evidence",
    "PromptTemplate",
    "author_build_file",
    "classify_repo",
    "diagnose_build",
    "disambiguate_dependency",
    "escalate_repair",
    "extract_manifest",
    "prompt_sha256",
    "prompt_template_version",
    "propose_cycle_break",
    "propose_repair",
    "render_prompt",
    "resolve_version_conflict",
    "rewrite_api_incompat",
    "write_pr_body",
    "write_pr_title",
]

type Evidence = Mapping[str, JsonValue]
"""What a caller hands a helper. `JsonValue` rather than `object` on purpose: it excludes `set`
(unordered ⇒ nondeterministic rendering) and any object whose `repr` carries an address, so the
determinism guarantee is enforced by the type rather than asked for in a comment."""

_EVIDENCE_HEADER: Final = "EVIDENCE (JSON, keys sorted):"


class PromptTemplate(FleetModel):
    """One role's prompt, as data.

    `version` IS `llm_cache.prompt_template_version` (§6): bumped by hand when the template
    changes *meaning*, which is the only sanctioned way to invalidate cached answers for a role.
    A typo fix that does not change meaning deliberately does not bump it — re-paying for a whole
    corpus because of a comma is exactly the cost §5 refuses to pay for `harness_version`.
    """

    role: Role
    version: int = Field(default=1, ge=1)
    system: str = Field(min_length=1)
    instruction: str = Field(min_length=1)


_RETURN_JSON: Final = (
    "Answer with the structured object the caller's schema describes, and nothing else. "
    "State uncertainty in the fields provided for it rather than in prose."
)

PROMPTS: Final[Mapping[Role, PromptTemplate]] = {
    Role.REPO_CLASSIFY: PromptTemplate(
        role=Role.REPO_CLASSIFY,
        system=(
            "You classify source repositories for a monorepo migration. You are given the file "
            "listing and manifest fragments a deterministic scanner could not resolve. "
            f"{_RETURN_JSON}"
        ),
        instruction=(
            "Decide which ecosystem this repository belongs to and whether it publishes a "
            "coordinate other repositories consume. Report calibrated confidence: a guess with "
            "confidence 0.4 is useful, a guess reported as 1.0 is not."
        ),
    ),
    Role.DEP_DISAMBIGUATE: PromptTemplate(
        role=Role.DEP_DISAMBIGUATE,
        system=(
            "You resolve one ambiguous dependency reference to at most one coordinate key. "
            f"{_RETURN_JSON}"
        ),
        instruction=(
            "Choose the candidate the reference actually names, or null if no candidate is "
            "defensible on the evidence given. Do not invent a coordinate that is not among the "
            "candidates."
        ),
    ),
    Role.PR_TITLE: PromptTemplate(
        role=Role.PR_TITLE,
        system=f"You write one-line pull-request titles for migration commits. {_RETURN_JSON}",
        instruction=(
            "Write a title of at most 120 characters naming the repository and what moved. "
            "No trailing period, no issue numbers, no emoji."
        ),
    ),
    Role.MANIFEST_EXTRACT: PromptTemplate(
        role=Role.MANIFEST_EXTRACT,
        system=(
            "You extract declared dependencies from a build manifest no parser could read. "
            f"{_RETURN_JSON}"
        ),
        instruction=(
            "List only dependencies the file actually declares, with versions and scopes exactly "
            "as written. If the file is unreadable or the declarations are conditional in a way "
            "you cannot resolve, say so in unparsed_reason and return no dependencies rather "
            "than guessing."
        ),
    ),
    Role.BUILD_DIAGNOSIS: PromptTemplate(
        role=Role.BUILD_DIAGNOSIS,
        system=(
            "You diagnose build and test failures from verbatim tool output. "
            f"{_RETURN_JSON}"
        ),
        instruction=(
            "Classify the failure using the closed vocabulary provided, name the most likely root "
            "cause, and list the files worth opening. Infrastructure noise is TRANSIENT_INFRA; "
            "do not classify it as a code defect."
        ),
    ),
    Role.PR_BODY: PromptTemplate(
        role=Role.PR_BODY,
        system=(
            "You write reviewer-facing pull-request bodies for an automated repository "
            f"migration. {_RETURN_JSON}"
        ),
        instruction=(
            "Describe what changed and what a reviewer should check. Do not assert that tests "
            "pass, that behaviour is equivalent, or that anything was verified: those statements "
            "are rendered from verification data by the harness, not written here."
        ),
    ),
    Role.TRANSFORM_REPAIR: PromptTemplate(
        role=Role.TRANSFORM_REPAIR,
        system=(
            "You repair a source file whose deterministic rewrite failed, given the failure "
            f"evidence and the current file content. {_RETURN_JSON}"
        ),
        instruction=(
            "Propose the smallest unified diff that fixes the reported failure. Change only what "
            "the evidence justifies. Summarise your approach in one line: that line, never the "
            "diff, is what a later attempt is shown if this one is refuted."
        ),
    ),
    Role.API_INCOMPAT_REWRITE: PromptTemplate(
        role=Role.API_INCOMPAT_REWRITE,
        system=(
            "You rewrite call sites against a dependency whose public surface changed. "
            f"{_RETURN_JSON}"
        ),
        instruction=(
            "Rewrite the affected call sites to the new surface as a unified diff, and list the "
            "breaking changes you are absorbing. If a call site has no equivalent in the new "
            "surface, leave it unchanged and say so in the rationale rather than approximating it."
        ),
    ),
    Role.ESCALATION: PromptTemplate(
        role=Role.ESCALATION,
        system=(
            "You are the final automated attempt on a task two earlier attempts failed. You are "
            "given the evidence and one-line summaries of the approaches already refuted — never "
            f"their diffs. {_RETURN_JSON}"
        ),
        instruction=(
            "Propose a materially different approach from the refuted ones. If the evidence shows "
            "the task needs a human decision, set abandon_recommended and say precisely what the "
            "human must decide: recommending that is a better answer than a patch you do not "
            "believe in."
        ),
    ),
    Role.BUILD_AUTHORING: PromptTemplate(
        role=Role.BUILD_AUTHORING,
        system=(
            "You propose Bazel targets for a package no ecosystem adapter could derive targets "
            f"for. {_RETURN_JSON}"
        ),
        instruction=(
            "Propose the minimum set of targets that builds this package's sources and tests. "
            "Emit target structure only — rule name, srcs, deps, visibility. Do not write "
            "Starlark text; the harness renders it."
        ),
    ),
    Role.CYCLE_BREAK_PROPOSAL: PromptTemplate(
        role=Role.CYCLE_BREAK_PROPOSAL,
        system=(
            "You propose how to break one dependency cycle in a repository graph. "
            f"{_RETURN_JSON}"
        ),
        instruction=(
            "Choose a break strategy from the closed vocabulary and name exactly the edges or "
            "contracts it touches. Your answer is a proposal: the harness re-validates it against "
            "the same edge set before anything is written."
        ),
    ),
    Role.CONFLICT_RESOLUTION: PromptTemplate(
        role=Role.CONFLICT_RESOLUTION,
        system=(
            "You resolve a third-party version conflict whose constraint set has an empty "
            f"intersection. Minimal version selection has already failed. {_RETURN_JSON}"
        ),
        instruction=(
            "Propose one version and the MODULE.bazel mechanism that pins it, and list every "
            "declared spec that version knowingly violates. An honest list of violations is the "
            "point: the harness validates the pin against all contributing specs before writing."
        ),
    ),
}

if set(PROMPTS) != set(Role):  # pragma: no cover - import-time totality gate
    _missing = sorted(str(r) for r in set(Role) - set(PROMPTS))
    raise RuntimeError(f"PROMPTS is not total over Role; missing: {_missing}")


# ---------------------------------------------------------------------------------------------
# Deterministic rendering
# ---------------------------------------------------------------------------------------------


def render_prompt(role: Role, evidence: Evidence) -> tuple[Message, ...]:
    """Template + evidence → the exact turns the backend will see.

    Byte-identical for identical inputs, in any process, under any `PYTHONHASHSEED`: the evidence
    is serialised with `sort_keys=True` (so nested mapping order cannot leak), `ensure_ascii=True`
    (so a locale cannot change the bytes) and `allow_nan=False` (so a `NaN` fails loudly here
    instead of producing JSON no parser accepts). Returns a tuple, because the messages are an
    input to a hash and a caller that could append to them would change what was hashed.
    """
    template = PROMPTS[role]
    body = json.dumps(
        redact_mapping(evidence),
        sort_keys=True,
        ensure_ascii=True,
        allow_nan=False,
        indent=2,
        separators=(",", ": "),
    )
    return (
        Message(role="system", content=template.system),
        Message(role="user", content=f"{template.instruction}\n\n{_EVIDENCE_HEADER}\n{body}"),
    )


def prompt_sha256(messages: Sequence[Message]) -> str:
    """§6's `prompt_sha256`: sha256 over the FULLY rendered prompt, roles included.

    Role and content are hashed together as a JSON array rather than concatenated, so a system
    turn ending in text that a user turn begins with cannot collide with the same bytes split the
    other way — a collision here would serve one call's answer to a different call.
    """
    payload = [[message.role, message.content] for message in messages]
    return sha256_text(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))


def prompt_template_version(role: Role) -> int:
    """The `llm_cache` invalidation knob for one role (§5)."""
    return PROMPTS[role].version


# ---------------------------------------------------------------------------------------------
# The helpers. One per role: bind the prompt, bind the schema, hand it to the client.
# ---------------------------------------------------------------------------------------------


async def _call[T: BaseModel](
    client: ModelClient,
    role: Role,
    response_model: type[T],
    evidence: Evidence,
    *,
    budget: CallBudget | None = None,
    tier_override: ModelTier | None = None,
    max_output_tokens: int | None = None,
    timeout_s: float | None = None,
) -> ModelResponse[T]:
    """The whole orchestration path of every helper below. Deliberately five lines: routing,
    negotiation, failover, repair and caching are the client's, so a new role costs a template,
    a schema and a two-line wrapper — and cannot accidentally acquire its own retry policy."""
    return await client.complete(
        str(role),
        render_prompt(role, evidence),
        response_model,
        tier_override=tier_override,
        max_output_tokens=max_output_tokens,
        timeout_s=timeout_s,
        budget=budget,
    )


async def classify_repo(
    client: ModelClient, evidence: Evidence, *, budget: CallBudget | None = None
) -> ModelResponse[RepoClassification]:
    """`repo_classify` → CHEAP (§3.1 step 2 fallback)."""
    return await _call(client, Role.REPO_CLASSIFY, RepoClassification, evidence, budget=budget)


async def disambiguate_dependency(
    client: ModelClient, evidence: Evidence, *, budget: CallBudget | None = None
) -> ModelResponse[DependencyDisambiguation]:
    """`dep_disambiguate` → CHEAP (§3.1 step 5b coordinate ladder)."""
    return await _call(
        client, Role.DEP_DISAMBIGUATE, DependencyDisambiguation, evidence, budget=budget
    )


async def write_pr_title(
    client: ModelClient, evidence: Evidence, *, budget: CallBudget | None = None
) -> ModelResponse[PrTitle]:
    """`pr_title` → CHEAP (§3.4)."""
    return await _call(client, Role.PR_TITLE, PrTitle, evidence, budget=budget)


async def extract_manifest(
    client: ModelClient, evidence: Evidence, *, budget: CallBudget | None = None
) -> ModelResponse[ManifestExtraction]:
    """`manifest_extract` → WORKHORSE (§7.3 fallback)."""
    return await _call(client, Role.MANIFEST_EXTRACT, ManifestExtraction, evidence, budget=budget)


async def diagnose_build(
    client: ModelClient, evidence: Evidence, *, budget: CallBudget | None = None
) -> ModelResponse[LlmBuildDiagnosis]:
    """`build_diagnosis` → WORKHORSE (§3.3)."""
    return await _call(client, Role.BUILD_DIAGNOSIS, LlmBuildDiagnosis, evidence, budget=budget)


async def write_pr_body(
    client: ModelClient, evidence: Evidence, *, budget: CallBudget | None = None
) -> ModelResponse[PrBody]:
    """`pr_body` → WORKHORSE (§3.4). The equivalence banner is rendered by code, not asked for."""
    return await _call(client, Role.PR_BODY, PrBody, evidence, budget=budget)


async def propose_repair(
    client: ModelClient, evidence: Evidence, *, budget: CallBudget | None = None
) -> ModelResponse[LlmPatchProposal]:
    """`transform_repair` → WORKHORSE, ADR-0014 attempt 2, `EVIDENCE_ONLY` (§3.2 step 5)."""
    return await _call(client, Role.TRANSFORM_REPAIR, LlmPatchProposal, evidence, budget=budget)


async def rewrite_api_incompat(
    client: ModelClient, evidence: Evidence, *, budget: CallBudget | None = None
) -> ModelResponse[ApiRewriteProposal]:
    """`api_incompat_rewrite` → HEAVY."""
    return await _call(
        client, Role.API_INCOMPAT_REWRITE, ApiRewriteProposal, evidence, budget=budget
    )


async def escalate_repair(
    client: ModelClient, evidence: Evidence, *, budget: CallBudget | None = None
) -> ModelResponse[LlmEscalationProposal]:
    """`escalation` → HEAVY, ADR-0014 attempt 3, `EVIDENCE_PLUS_REJECTED_APPROACHES`.

    The caller composes the rejected-approach summaries into `evidence`; this helper does not
    reach for them, because what was RENDERED is what the cache key digests (ADR-0021)."""
    return await _call(client, Role.ESCALATION, LlmEscalationProposal, evidence, budget=budget)


async def author_build_file(
    client: ModelClient, evidence: Evidence, *, budget: CallBudget | None = None
) -> ModelResponse[BuildFileProposal]:
    """`build_authoring` → HEAVY (§3.3 step 2 escape hatch)."""
    return await _call(client, Role.BUILD_AUTHORING, BuildFileProposal, evidence, budget=budget)


async def propose_cycle_break(
    client: ModelClient, evidence: Evidence, *, budget: CallBudget | None = None
) -> ModelResponse[CycleBreakProposal]:
    """`cycle_break_proposal` → HEAVY. Detection and `break_cost` remain deterministic (§3.1)."""
    return await _call(
        client, Role.CYCLE_BREAK_PROPOSAL, CycleBreakProposal, evidence, budget=budget
    )


async def resolve_version_conflict(
    client: ModelClient, evidence: Evidence, *, budget: CallBudget | None = None
) -> ModelResponse[VersionConflictResolution]:
    """`conflict_resolution` → HEAVY. Reached only on an empty intersection (§13 row 17)."""
    return await _call(
        client, Role.CONFLICT_RESOLUTION, VersionConflictResolution, evidence, budget=budget
    )
