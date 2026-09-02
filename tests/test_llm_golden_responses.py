"""SPEC §12 item 4 — checked-in golden-response fixtures, one per shipped backend + PROMPTED rung,
per LLM role. `docs/CRITERIA_PLAN.md` §4's revised Done bar requires this per **role** (12,
`config/models.yaml`), not just per backend — round II's task 3 landed `REPO_CLASSIFY` alone (1 of
12); this file's round III task 3 adds `PR_TITLE` and `PR_BODY` (3 of 12; 9 remain, tracked in
`docs/CRITERIA_PLAN.md` §4, not flipped DONE here).

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
from fleet.llm.schemas import RESPONSE_SCHEMAS, FleetModel, PrBody, PrTitle, RepoClassification
from fleet.models.enums import Ecosystem, StructuredOutputMode
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
