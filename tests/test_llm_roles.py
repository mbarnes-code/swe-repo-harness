"""SPEC §9 / §7.7 — role routing, the per-role response contracts, and deterministic prompts.

Nothing here opens a socket or constructs a backend: routing and rendering are pure functions of
config and inputs, and that is the property being pinned. Each test names the silent failure it
exists to catch — a role that resolves to the wrong tier costs money quietly, a role missing from
config raises in wave 7 instead of at startup, and a prompt that varies by dict order never fails
at all: it just stops hitting the cache and triples the bill.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from pydantic import ValidationError

from fleet.llm.calls import PROMPTS, prompt_sha256, prompt_template_version, render_prompt
from fleet.llm.client import Message, UnknownRole
from fleet.llm.roles import SPEC_ROLE_TIERS, LlmRouter, Role, TierNotConfigured, UnknownProfile
from fleet.llm.schemas import RESPONSE_SCHEMAS, response_schema_sha256
from fleet.models.enums import ModelTier
from fleet.models.tasks import BackendTarget, Price
from fleet.settings import ModelsConfig

SRC = Path(__file__).resolve().parents[1] / "src"


def target(backend: str, model_id: str, *, effort: str = "high") -> BackendTarget:
    return BackendTarget(
        backend=backend,
        model_id=model_id,
        effort=effort,
        price=Price(in_per_mtok=3.0, out_per_mtok=15.0),
    )


def profile() -> dict[ModelTier, tuple[BackendTarget, ...]]:
    """A realistic §9 profile: HEAVY with same-model transport failover, WORKHORSE with a local
    standby, CHEAP single-target. Shaped like §9's `hosted_failover` example rather than its
    `default`, which routes every tier through core backends so the example file is copyable."""
    return {
        ModelTier.HEAVY: (target("anthropic", "heavy-a"), target("bedrock", "heavy-a")),
        ModelTier.WORKHORSE: (
            target("anthropic", "workhorse-a"),
            target("openai_compatible", "local-workhorse", effort="medium"),
        ),
        ModelTier.CHEAP: (target("anthropic", "cheap-a", effort="low"),),
    }


def roles() -> dict[str, ModelTier]:
    return {str(role): tier for role, tier in SPEC_ROLE_TIERS.items()}


# ---------------------------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------------------------


def test_every_role_resolves_to_its_configured_tier_and_ordered_target_list() -> None:
    """The whole point of the two-level file: a role names a JOB, config decides which model
    answers it. If resolution silently picked a different tier, a CHEAP classification would be
    answered by the frontier model and the bill would be the only symptom."""
    r = LlmRouter(roles(), profile())
    for role, expected_tier in SPEC_ROLE_TIERS.items():
        route = r.resolve(str(role))
        assert route.tier is expected_tier
        assert tuple(t.model_id for t in route.targets) == tuple(
            t.model_id for t in profile()[expected_tier]
        ), "target ORDER is the failover order; a reordering is a different routing decision"


def test_an_undeclared_role_is_a_startup_error_naming_the_role() -> None:
    """A role the harness has a prompt and a schema for, missing from `models.yaml`, must fail
    while the operator is still looking at the terminal — not in wave 7 with 200 repos cloned.
    The message names the role because that is the operator's next edit (§13 row 36)."""
    incomplete = {k: v for k, v in roles().items() if k != str(Role.PR_TITLE)}
    with pytest.raises(UnknownRole) as excinfo:
        LlmRouter(incomplete, profile())
    assert "pr_title" in str(excinfo.value)


def test_resolving_a_role_nobody_declared_names_it_rather_than_defaulting() -> None:
    """`resolve` must never fall back to a default tier: a typo'd role silently answered by
    HEAVY is an invisible cost, and answered by CHEAP is an invisible quality regression."""
    with pytest.raises(UnknownRole) as excinfo:
        LlmRouter(roles(), profile()).resolve("summarise_everything")
    assert "summarise_everything" in str(excinfo.value)


def test_a_role_whose_tier_has_no_target_fails_loudly_at_startup() -> None:
    """§9 loader rule 1. The dangerous alternative is not a crash — it is a quiet fallback to a
    neighbouring tier, which is exactly the model substitution ADR-0023 spends a cache key
    component defending against."""
    empty_cheap = profile()
    empty_cheap[ModelTier.CHEAP] = ()
    with pytest.raises(TierNotConfigured) as excinfo:
        LlmRouter(roles(), empty_cheap, profile="thin")
    message = str(excinfo.value)
    assert "CHEAP" in message
    assert "thin" in message
    assert excinfo.value.role in {str(Role.REPO_CLASSIFY), str(Role.DEP_DISAMBIGUATE),
                                  str(Role.PR_TITLE)}


def test_a_tier_override_moves_the_tier_but_never_invents_the_role() -> None:
    """The operator escape hatch (§7.7) says WHERE to send a job, not that the job exists — so an
    unknown role stays an error even with an override, and an override at an unconfigured tier
    fails loudly instead of silently using the role's declared tier."""
    r = LlmRouter(roles(), profile())
    assert r.resolve(str(Role.PR_TITLE), tier_override=ModelTier.HEAVY).tier is ModelTier.HEAVY
    with pytest.raises(UnknownRole):
        r.resolve("not_a_role", tier_override=ModelTier.HEAVY)

    cheap_only = LlmRouter(
        {str(Role.PR_TITLE): ModelTier.CHEAP},
        {ModelTier.CHEAP: profile()[ModelTier.CHEAP]},
        required_roles=(),
    )
    with pytest.raises(TierNotConfigured):
        cheap_only.resolve(str(Role.PR_TITLE), tier_override=ModelTier.HEAVY)


def test_from_models_config_uses_the_named_profile_and_refuses_an_unknown_one() -> None:
    """`--profile` is the entire "swap the fleet from hosted to local" mechanism (§12.41): it must
    change which targets answer while leaving the role→tier map untouched."""
    raw: Mapping[str, object] = {
        "version": 2,
        "roles": {str(role): str(tier) for role, tier in SPEC_ROLE_TIERS.items()},
        "default_profile": "default",
        "profiles": {
            "default": {
                "HEAVY": [{"backend": "anthropic", "model_id": "heavy-a", "effort": "high",
                           "price": {"in_per_mtok": 5.0, "out_per_mtok": 25.0}}],
                "WORKHORSE": [{"backend": "anthropic", "model_id": "workhorse-a",
                               "effort": "high",
                               "price": {"in_per_mtok": 3.0, "out_per_mtok": 15.0}}],
                "CHEAP": [{"backend": "anthropic", "model_id": "cheap-a", "effort": "low",
                           "price": {"in_per_mtok": 1.0, "out_per_mtok": 5.0}}],
            },
            "local": {
                "HEAVY": [{"backend": "openai_compatible", "model_id": "local-heavy",
                           "effort": "high", "price": "free",
                           "base_url": "http://localhost:8001/v1"}],
                "WORKHORSE": [{"backend": "openai_compatible", "model_id": "local-workhorse",
                               "effort": "medium", "price": "free",
                               "base_url": "http://localhost:8001/v1"}],
                "CHEAP": [{"backend": "openai_compatible", "model_id": "local-cheap",
                           "effort": "low", "price": "free",
                           "base_url": "http://localhost:8001/v1"}],
            },
        },
    }
    models = ModelsConfig.model_validate(raw)

    hosted = LlmRouter.from_models_config(models)
    local = LlmRouter.from_models_config(models, profile="local")

    assert hosted.tier_for(str(Role.ESCALATION)) is local.tier_for(str(Role.ESCALATION))
    assert hosted.resolve(str(Role.ESCALATION)).targets[0].backend == "anthropic"
    assert local.resolve(str(Role.ESCALATION)).targets[0].backend == "openai_compatible"

    with pytest.raises(UnknownProfile) as excinfo:
        LlmRouter.from_models_config(models, profile="gpu-farm")
    assert "gpu-farm" in str(excinfo.value)


def test_routes_are_emitted_in_a_total_order() -> None:
    """`fleet models list` output and any digest computed over it must not depend on dict
    insertion order, or two identical configs would print (and hash) differently (§11.6)."""
    forward = LlmRouter(roles(), profile()).roles()
    backward = LlmRouter(dict(reversed(list(roles().items()))), profile()).roles()
    assert forward == backward == tuple(sorted(forward))


# ---------------------------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------------------------


def _sample(model_cls: type) -> Mapping[str, object]:
    """A minimal valid payload per role, written out rather than generated: a generator that
    derived these from the schema would pass even if the schema said something absurd."""
    samples: dict[str, Mapping[str, object]] = {
        "RepoClassification": {"ecosystem": "maven", "is_library": True, "confidence": 0.9,
                               "rationale": "pom.xml declares a groupId"},
        "DependencyDisambiguation": {"chosen_coordinate": "maven:com.acme:x", "confidence": 0.5,
                                     "candidates_considered": ["maven:com.acme:x"],
                                     "rationale": "closest match"},
        "PrTitle": {"title": "migrate acme-commons into the monorepo"},
        "ManifestExtraction": {"ecosystem": "npm",
                               "dependencies": [{"coordinate": "npm:left-pad",
                                                 "version": "1.3.0", "scope": "dependencies"}]},
        "LlmBuildDiagnosis": {"failure_class": "BUILD_ERROR", "root_cause": "missing dep",
                              "suspect_paths": ["BUILD.bazel"], "suggested_action": "add dep",
                              "confidence": 0.7},
        "PrBody": {"body": "Moves acme-commons.", "highlights": ["no behaviour change"]},
        "LlmPatchProposal": {"files": [{"path": "a.java", "diff": "--- a\n+++ b\n"}],
                             "approach_summary": "rewrite import", "rationale": "package moved"},
        "ApiRewriteProposal": {"files": [{"path": "a.java", "diff": "--- a\n+++ b\n"}],
                               "approach_summary": "adopt builder API",
                               "rationale": "constructor removed",
                               "coord_key": "maven:com.acme:x", "breaking_changes": ["ctor gone"]},
        "LlmEscalationProposal": {"files": [{"path": "a.java", "diff": "--- a\n+++ b\n"}],
                                  "approach_summary": "invert the dependency",
                                  "rationale": "both prior approaches anchored",
                                  "abandon_recommended": False},
        "BuildFileProposal": {"package_path": "java/acme",
                              "targets": [{"name": "acme", "rule": "java_library",
                                           "srcs": ["A.java"], "deps": [], "visibility": []}],
                              "rationale": "single source set"},
        "CycleBreakProposal": {"strategy": "CONTRACT_HOIST", "broken_edge_ids": ["e1"],
                               "hoisted_contract_ids": ["proto:acme.v1"],
                               "rationale": "the proto is the shared surface"},
        "VersionConflictResolution": {"coord_key": "maven:com.acme:x",
                                      "proposed_version": "2.0.0", "mechanism": "bazel_dep",
                                      "violated_specs": ["[1.0,2.0)"],
                                      "rationale": "nothing satisfies every spec"},
    }
    return samples[model_cls.__name__]


def test_every_role_has_a_prompt_and_a_response_schema() -> None:
    """Totality over `Role`, asserted from outside the modules that assert it at import: a role
    with no schema would be a call whose reply nothing validates."""
    assert set(RESPONSE_SCHEMAS) == set(Role)
    assert set(PROMPTS) == set(Role)


@pytest.mark.parametrize("role", list(Role), ids=str)
def test_every_response_schema_round_trips_and_refuses_an_extra_field(role: Role) -> None:
    """Two halves of one property (ADR-0002). Round-trip: a validated reply survives being
    persisted and re-read, which is what `llm_cache` does to every one of them. Extra-field
    refusal: a model that invents a field — `verified: true`, `tests_pass: true` — must be a
    validation failure and a repair re-ask, not a silently-dropped key nobody sees."""
    model_cls = RESPONSE_SCHEMAS[role]
    value = model_cls.model_validate(_sample(model_cls))
    assert model_cls.model_validate_json(value.model_dump_json()) == value

    with pytest.raises(ValidationError):
        model_cls.model_validate({**_sample(model_cls), "verified_by_model": True})


def test_schema_digests_are_stable_and_distinct_per_role() -> None:
    """`response_schema_sha256` is a cache key component. Stable across calls or every call is a
    miss; distinct per role or two roles share cache identity and one serves the other's answer."""
    digests = {role: response_schema_sha256(cls) for role, cls in RESPONSE_SCHEMAS.items()}
    again = {role: response_schema_sha256(cls) for role, cls in RESPONSE_SCHEMAS.items()}
    assert digests == again
    assert len(set(digests.values())) == len(digests)


# ---------------------------------------------------------------------------------------------
# Deterministic prompt construction
# ---------------------------------------------------------------------------------------------

EVIDENCE: dict[str, object] = {
    "repo_id": "acme-commons",
    "failure_class": "BUILD_ERROR",
    "stderr_tail": "error: package com.acme.util does not exist\n",
    "files": ["src/main/java/A.java", "src/main/java/B.java"],
    "relocation_map": {"com.acme.util": "acme.util", "com.acme.io": "acme.io"},
}

_CHILD = """
import json, sys
sys.path.insert(0, sys.argv[1])
from fleet.llm.calls import prompt_sha256, render_prompt
from fleet.llm.roles import Role

evidence = json.loads(sys.argv[2])
if sys.argv[3] == "reversed":
    evidence = dict(reversed(list(evidence.items())))
messages = render_prompt(Role.TRANSFORM_REPAIR, evidence)
print(prompt_sha256(messages))
"""


def _child_digest(*, hashseed: str, key_order: str) -> str:
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = hashseed
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", _CHILD, str(SRC), json.dumps(EVIDENCE), key_order],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return result.stdout.strip()


def test_prompt_construction_is_byte_identical_for_identical_inputs() -> None:
    """In-process baseline for the cross-process test below."""
    first = render_prompt(Role.TRANSFORM_REPAIR, EVIDENCE)
    second = render_prompt(Role.TRANSFORM_REPAIR, dict(reversed(list(EVIDENCE.items()))))
    assert [m.content for m in first] == [m.content for m in second]
    assert prompt_sha256(first) == prompt_sha256(second)


def test_prompt_digest_is_identical_across_processes_and_hash_seeds() -> None:
    """The cache key is `sha256` over the rendered prompt, so nondeterminism here does not
    produce a wrong answer — it produces a permanent cache MISS. A fleet that re-pays for every
    prompt because a dict iterated differently in a pool child is a tripled bill with no error
    message anywhere, which is why this is asserted across processes and hash seeds rather than
    with one in-process comparison."""
    digests = {
        _child_digest(hashseed="0", key_order="forward"),
        _child_digest(hashseed="1", key_order="forward"),
        _child_digest(hashseed="12345", key_order="reversed"),
    }
    assert len(digests) == 1
    assert digests == {prompt_sha256(render_prompt(Role.TRANSFORM_REPAIR, EVIDENCE))}


def test_prompt_digest_covers_role_boundaries_not_just_concatenated_text() -> None:
    """Hashing `role + content` concatenated would let a system turn ending in text a user turn
    begins with collide with the same bytes split the other way — and a collision serves one
    call's answer to a different call."""
    a = [Message(role="system", content="ab"), Message(role="user", content="c")]
    b = [Message(role="system", content="a"), Message(role="user", content="bc")]
    assert prompt_sha256(a) != prompt_sha256(b)


def test_prompt_template_versions_start_at_one_and_are_declared_per_role() -> None:
    """`prompt_template_version` is THE cache invalidation knob (§5). It has to live beside the
    template it versions, or nobody bumps it when the template's meaning changes."""
    for role in Role:
        assert prompt_template_version(role) == PROMPTS[role].version >= 1


def test_evidence_rendering_refuses_a_value_json_cannot_state_deterministically() -> None:
    """`NaN` renders as bare `NaN`, which is not JSON and which no backend parses the same way.
    Failing here is loud; letting it through is a reply nobody can validate."""
    with pytest.raises(ValueError, match="Out of range float"):
        render_prompt(Role.PR_TITLE, {"score": float("nan")})


def test_prompts_never_hard_code_a_model_or_an_endpoint() -> None:
    """§12.40, applied where prose is most likely to leak one: a prompt naming a vendor makes the
    profile swap (§12.41) a lie, because the local model is told it is the hosted one."""
    banned = ("claude", "gpt-", "gemini", "llama", "mistral", "http://", "https://")
    for role, template in PROMPTS.items():
        blob = f"{template.system}\n{template.instruction}".lower()
        for token in banned:
            assert token not in blob, f"{role} prompt mentions {token!r}"


def test_render_prompt_returns_an_immutable_turn_sequence() -> None:
    """The turns are an input to a hash. A caller that could append to them after rendering would
    change what was sent without changing what was keyed."""
    messages: Sequence[object] = render_prompt(Role.PR_TITLE, {"repo": "x"})
    assert isinstance(messages, tuple)


def test_render_prompt_redacts_a_secret_shaped_string_in_evidence() -> None:
    """SECURITY_REVIEW.md finding #4: `render_prompt` builds every outbound LLM request by
    `json.dumps`-serializing the caller's evidence directly, with no call anywhere to
    `redact()`/`redact_mapping()` — the one gap in `obs/redact.py`'s own stated design goal of a
    single redaction call wired into EVERY egress boundary. Concretely,
    `workers/rewrite.py::_evidence()` puts the raw, unredacted current file content into evidence
    as `current_content`; if that file holds a checked-in secret, it reached the LLM backend
    verbatim. Same PAT-shaped fixture `tests/test_pr_body_redaction.py` uses for the sibling
    PR-body/title egress boundary.
    """
    pat = "github_pat_11ABCDEFG0abcdefghijklmnopqrstuvwxyz0123456789ABCDEF"
    evidence = {
        "repo_id": "acme-widget",
        "current_content": f'API_TOKEN = "{pat}"\nprint("hello world")\n',
    }
    messages = render_prompt(Role.TRANSFORM_REPAIR, evidence)
    rendered = "\n".join(message.content for message in messages)
    assert pat not in rendered, f"a live PAT reached the outbound LLM message: {rendered!r}"
    assert "github_pat_" not in rendered
    # `render_prompt` serializes with `ensure_ascii=True` (its own docstring: "a locale cannot
    # change the bytes"), so the `«»` placeholder delimiters are the `\uXXXX`-escaped form here.
    assert "\\u00abredacted:" in rendered, "the placeholder must survive, or debugging is blind"
    # The control: ordinary source code around the secret must pass through unredacted (JSON-
    # escaped, like the rest of the rendered evidence).
    assert 'print(\\"hello world\\")' in rendered
    assert "acme-widget" in rendered
