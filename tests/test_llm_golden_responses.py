"""SPEC §12 item 4 — checked-in golden-response fixtures, one per shipped backend + PROMPTED rung.

ADR-0013's contract layer declares the intent this closes: "every LLM prompt's declared response
schema validates against a stored golden sample." `tests/test_llm_roles.py`'s
`test_every_response_schema_round_trips_and_refuses_an_extra_field` already covers a DIFFERENT
half of that sentence — a hand-shaped Pydantic-model-literal sample (`_sample(model_cls)`)
round-trips through `model_dump_json`/`model_validate_json` — but it never touches a backend's own
wire shape or its real response-parsing code; the "golden sample" there is already in the model's
own vocabulary, not a recorded wire response. This file is the missing half: a RECORDED raw
wire-format response, one per shipped backend, parsed through that backend's REAL
`parse_reply`/`_reply_from` (not a mock of the parsing step), then validated against its role's
declared schema through the same `client._validate` every live call runs.

Fixtures live in `tests/fixtures/llm/golden_responses/*.json`. Each carries a `_provenance` key
documenting where its shape came from (the adapter's own parsing code, a vendor API doc, or an
existing test helper it matches) — popped before the fixture is handed to real parsing code, so
what the assertion actually consumes is exactly what a wire response would contain.

**Rungs.** `anthropic`/`bedrock`/`vertex` are demonstrated at TOOL_CALL — their structurally
richer rung, a nested arguments object rather than bare text, and the rung their own
`_DECLARED.structured_output_modes` lists first. `openai_compatible` is demonstrated at
PROMPTED — the rung ADR-0013's contract layer names as required, and the ONLY rung this backend's
`declared_capabilities()` claims at all (`structured_output_modes == (PROMPTED,)`,
`supports_tools=False`), which makes it the cleanest place in this codebase to demonstrate it: no
other shipped backend has PROMPTED as its floor rather than its fallback.

**Why TOOL_CALL parsing is reused via `parse_reply`/`_reply_from` rather than reimplemented.**
Those are the exact functions `AnthropicBackend.invoke` / the module-level `invoke()` on the other
three adapters call in production after a transport reply arrives — see the call sites in
`src/fleet/llm/backends/{anthropic,bedrock,vertex,openai_compatible}.py`. Nothing here mocks a
parsing step; only the network transport (never reached — these fixtures are already the
"decoded body") is absent.

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
from pathlib import Path
from typing import Any

import pytest
from anthropic.types import Message as SdkMessage
from pydantic import ValidationError

from fleet.llm import client as client_module
from fleet.llm.backends import anthropic as anthropic_backend
from fleet.llm.backends import openai_compatible as oc_backend
from fleet.llm.roles import Role
from fleet.llm.schemas import RESPONSE_SCHEMAS, RepoClassification
from fleet.models.enums import Ecosystem, StructuredOutputMode
from fleet.models.tasks import BackendTarget, Price
from tests.test_llm_backend_bedrock import parse_reply as bedrock_parse_reply
from tests.test_llm_backend_bedrock import target as bedrock_target
from tests.test_llm_backend_vertex import parse_reply as vertex_parse_reply
from tests.test_llm_backend_vertex import target as vertex_target

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "llm" / "golden_responses"

# The concrete class, not just the `RESPONSE_SCHEMAS[Role.REPO_CLASSIFY]` lookup: the mapping's
# declared value type is `type[FleetModel]`, which would make every `value.ecosystem` /
# `value.is_library` access below untyped. The assertion ties the two together so a future
# `Role.REPO_CLASSIFY` remap (a different model class) fails loudly here instead of silently
# validating fixtures against the wrong schema.
assert RESPONSE_SCHEMAS[Role.REPO_CLASSIFY] is RepoClassification
REPO_CLASSIFY_SCHEMA = RepoClassification


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


# ---------------------------------------------------------------------------------------------
# One recorded raw response per shipped backend, parsed through REAL adapter code, validated
# through the REAL client._validate — the function every live call's schema check runs through.
# ---------------------------------------------------------------------------------------------


def test_anthropic_golden_tool_call_response_parses_and_validates() -> None:
    raw = _load("anthropic_tool_call.json")
    message = SdkMessage.model_validate(raw)  # the real SDK's own wire-to-object parse
    reply = anthropic_backend._reply_from(_anthropic_target(), message)  # the real adapter parse

    value = client_module._validate(reply, REPO_CLASSIFY_SCHEMA, StructuredOutputMode.TOOL_CALL)

    assert value.ecosystem is Ecosystem.MAVEN
    assert value.is_library is True
    assert reply.finish_reason == "tool_call"


def test_bedrock_golden_tool_call_response_parses_and_validates() -> None:
    raw = _load("bedrock_tool_call.json")
    reply = bedrock_parse_reply(raw, bedrock_target())

    value = client_module._validate(reply, REPO_CLASSIFY_SCHEMA, StructuredOutputMode.TOOL_CALL)

    assert value.ecosystem is Ecosystem.NPM
    assert value.is_library is False
    assert reply.finish_reason == "tool_call"


def test_vertex_golden_tool_call_response_parses_and_validates() -> None:
    raw = _load("vertex_tool_call.json")
    reply = vertex_parse_reply(raw, vertex_target())

    value = client_module._validate(reply, REPO_CLASSIFY_SCHEMA, StructuredOutputMode.TOOL_CALL)

    assert value.ecosystem is Ecosystem.GRADLE
    assert value.is_library is True
    assert reply.finish_reason == "tool_call"


def test_openai_compatible_golden_prompted_response_parses_and_validates() -> None:
    """The REQUIRED rung (ADR-0013's contract layer, this closure's `PROMPTED` clause):
    `openai_compatible` is the cleanest backend to demonstrate it on because PROMPTED is the ONLY
    rung its own `declared_capabilities()` claims — every other shipped backend falls back to it,
    this one is defined by it."""
    raw = _load("openai_compatible_prompted.json")
    reply = oc_backend.parse_reply(raw, _oc_target())

    value = client_module._validate(reply, REPO_CLASSIFY_SCHEMA, StructuredOutputMode.PROMPTED)

    assert value.ecosystem is Ecosystem.PYPI
    assert value.is_library is False
    assert reply.finish_reason == "stop"


# ---------------------------------------------------------------------------------------------
# Rule 12 — at least one fixture must be a genuine discriminator: mutate it to violate the
# schema and confirm validation genuinely fails, not merely "the test ran".
# ---------------------------------------------------------------------------------------------


def test_a_tool_call_reply_missing_a_required_field_fails_schema_validation() -> None:
    """Mutation of the bedrock fixture: delete `confidence`, a `RepoClassification` field with no
    default (`Field(ge=0.0, le=1.0)`, no `default=`). If schema validation above were a no-op —
    e.g. `_validate` silently accepting anything shaped like a dict — this would pass instead of
    raising, and the four "validates against its declared schema" assertions above would prove
    nothing beyond "code ran without crashing"."""
    raw = _load("bedrock_tool_call.json")
    del raw["output"]["message"]["content"][0]["toolUse"]["input"]["confidence"]
    reply = bedrock_parse_reply(raw, bedrock_target())

    with pytest.raises(ValidationError, match="confidence"):
        client_module._validate(reply, REPO_CLASSIFY_SCHEMA, StructuredOutputMode.TOOL_CALL)


def test_the_unmutated_fixture_still_validates_so_the_mutation_above_is_what_broke_it() -> None:
    """Old-passes/new-fails discriminator (Rule 12): the identical fixture, unmutated, parsed and
    validated the same way, must succeed — proving the failure above is caused by the deleted
    `confidence` field and not by some unrelated defect in the fixture, the parse path, or
    `_validate` itself. This is `test_bedrock_golden_tool_call_response_parses_and_validates`
    restated as the control half of the mutation pair, kept next to it deliberately."""
    raw = _load("bedrock_tool_call.json")
    reply = bedrock_parse_reply(raw, bedrock_target())

    client_module._validate(reply, REPO_CLASSIFY_SCHEMA, StructuredOutputMode.TOOL_CALL)
