"""SPEC §7.7 / ADR-0023 — the `bedrock` backend adapter (`src/fleet/llm/backends/bedrock.py`).

**Nothing here opens a socket and nothing here needs AWS credentials.** `boto3` is the `bedrock`
extra in `pyproject.toml` and is not installed in the harness venv, so the module is imported behind
a minimal stub installed into `sys.modules` at the top of this file. That is not a workaround: the
adapter is written so that `_Boto3Transport` is the ONLY object that touches the SDK, which means
the request builder, the reply parser and the error classifier — every behaviour the module actually
owns — are exercised for real through a fake `ConverseTransport`.

Coroutines are driven with `asyncio.run` rather than a plugin marker, matching `tests/conftest.py`
and `tests/test_llm_client.py`: the LLM boundary stays testable in a bare pydantic+pytest
environment.
"""

from __future__ import annotations

import asyncio
import importlib.util
import subprocess
import sys
import types
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

# ---------------------------------------------------------------------------------------------
# SDK stub — installed BEFORE the adapter is imported, and only when the real SDK is absent.
# ---------------------------------------------------------------------------------------------


class _StubClientError(Exception):
    """Mirrors `botocore.exceptions.ClientError`: the response dict is the whole contract the
    adapter reads (`Error.Code`, `ResponseMetadata.HTTPStatusCode`)."""

    def __init__(self, response: Mapping[str, object]) -> None:
        super().__init__(str(response))
        self.response = response


class _StubBotoCoreError(Exception):
    """Mirrors `botocore.exceptions.BotoCoreError`: endpoint/credential/connection faults."""


def _install_boto3_stub() -> None:
    boto3_mod = types.ModuleType("boto3")

    def _client(service: str, **kwargs: object) -> object:
        raise AssertionError(f"no test may build a real {service} client (kwargs={sorted(kwargs)})")

    boto3_mod.client = _client  # type: ignore[attr-defined]

    botocore = types.ModuleType("botocore")
    botocore.__path__ = []  # type: ignore[attr-defined]
    config_mod = types.ModuleType("botocore.config")

    class _Config:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

    config_mod.Config = _Config  # type: ignore[attr-defined]
    exceptions_mod = types.ModuleType("botocore.exceptions")
    exceptions_mod.ClientError = _StubClientError  # type: ignore[attr-defined]
    exceptions_mod.BotoCoreError = _StubBotoCoreError  # type: ignore[attr-defined]

    sys.modules.setdefault("boto3", boto3_mod)
    sys.modules.setdefault("botocore", botocore)
    sys.modules.setdefault("botocore.config", config_mod)
    sys.modules.setdefault("botocore.exceptions", exceptions_mod)


SDK_INSTALLED = importlib.util.find_spec("boto3") is not None
if not SDK_INSTALLED:
    _install_boto3_stub()

from fleet.llm import client as client_module  # noqa: E402
from fleet.llm.backends import bedrock as bedrock_module  # noqa: E402
from fleet.llm.backends.bedrock import (  # noqa: E402
    BedrockBackend,
    BedrockTargetMisconfigured,
    MissingRegion,
    build_request,
    parse_reply,
)
from fleet.llm.cache import CacheKeyParts  # noqa: E402
from fleet.llm.client import MalformedReply, Message, ModelBackend, TransportError  # noqa: E402
from fleet.models.enums import ModelTier, StructuredOutputMode  # noqa: E402
from fleet.models.tasks import BackendTarget, ModelCapabilities, Price  # noqa: E402

REGION = "us-east-1"
MODEL_ID = "model-under-test"
SCHEMA: dict[str, object] = {"type": "object", "properties": {"verdict": {"type": "string"}}}
TURNS = (Message(role="system", content="be terse"), Message(role="user", content="go"))


def target(**overrides: Any) -> BackendTarget:
    fields: dict[str, Any] = {
        "backend": "bedrock",
        "model_id": MODEL_ID,
        "region": REGION,
        "price": Price(in_per_mtok=5.0, out_per_mtok=25.0),
    }
    fields.update(overrides)
    return BackendTarget(**fields)


def converse_body(
    *,
    stop_reason: str = "end_turn",
    text: str | None = "ok",
    tool_input: Mapping[str, object] | None = None,
    served_model_id: str | None = None,
) -> dict[str, object]:
    """One `Converse` response. `served_model_id` is what a real Bedrock call echoes back — an
    inference-profile ARN or a `us.`-prefixed cross-region id — and the whole point of the cache
    regression below is that it must never reach `TokenUsage.model_id`."""
    content: list[dict[str, object]] = []
    if text is not None:
        content.append({"text": text})
    if tool_input is not None:
        content.append({"toolUse": {"name": "emit_response", "input": dict(tool_input)}})
    body: dict[str, object] = {
        "output": {"message": {"role": "assistant", "content": content}},
        "stopReason": stop_reason,
        "usage": {"inputTokens": 11, "outputTokens": 7, "cacheReadInputTokens": 3},
    }
    if served_model_id is not None:
        body["modelId"] = served_model_id
    return body


class FakeTransport:
    """A `ConverseTransport` that records the request and replays a scripted body. This is the
    seam that makes every test in this file socket-free."""

    def __init__(self, body: Mapping[str, object] | Exception) -> None:
        self._body = body
        self.calls: list[dict[str, object]] = []

    async def __call__(
        self,
        *,
        region: str,
        request: Mapping[str, object],
        timeout_s: float,
    ) -> Mapping[str, object]:
        self.calls.append({"region": region, "request": dict(request), "timeout_s": timeout_s})
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def invoke(
    backend: BedrockBackend,
    tgt: BackendTarget,
    *,
    schema: dict[str, object] | None = None,
    mode: StructuredOutputMode = StructuredOutputMode.PROMPTED,
    messages: Sequence[Message] = TURNS,
) -> Any:
    return asyncio.run(
        backend.invoke(
            tgt,
            messages,
            schema,
            mode,
            max_output_tokens=1024,
            timeout_s=30.0,
        ),
    )


# ---------------------------------------------------------------------------------------------
# Registration (§7.7 registry; `discover()` is the pkgutil walk)
# ---------------------------------------------------------------------------------------------


def test_discover_registers_bedrock_when_the_sdk_imports() -> None:
    """The registry is populated by IMPORT, and `discover()` is the only thing that imports the
    package. Asserting on `discover()` rather than on `registry()` is the difference between
    proving the decorator ran and proving the pkgutil walk actually finds this file — a backend
    that registers only when a unit test imports it directly is the failure mode §13 row 36 turns
    into `UnknownBackend` on the first real call."""
    found = client_module.discover()
    assert BedrockBackend.name in found
    assert isinstance(found[BedrockBackend.name], BedrockBackend)


def test_name_and_version_are_class_attributes() -> None:
    """`register_backend` reads `cls.name` BEFORE instantiating, and constructs with `cls()` — so
    both must exist at class-definition time and `__init__` must take no required argument."""
    assert BedrockBackend.name == "bedrock"
    assert isinstance(BedrockBackend.version, int)
    assert isinstance(BedrockBackend(), ModelBackend)


def test_a_missing_sdk_leaves_the_backend_unregistered() -> None:
    """`discover()` catches `ImportError` and NOTHING else (client.py). The adapter's `import
    boto3` is therefore deliberately unguarded at module scope: a host without the `bedrock` extra
    must see the backend simply absent, not a half-registered adapter that fails on the first
    dispatch of a 250-repo run.

    Driven in a fresh interpreter with `boto3` blocked, because the only honest way to test an
    import-time contract is at import time."""
    script = (
        "import sys\n"
        "class Block:\n"
        "    def find_module(self, name, path=None):\n"
        "        return None\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'boto3' or name.startswith('botocore'):\n"
        "            raise ImportError('blocked')\n"
        "        return None\n"
        "sys.meta_path.insert(0, Block())\n"
        "from fleet.llm import client\n"
        "try:\n"
        "    import fleet.llm.backends.bedrock\n"
        "    print('IMPORTED')\n"
        "except ImportError:\n"
        "    print('IMPORT_ERROR')\n"
        "print('bedrock' in client.discover())\n"
    )
    out = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
        cwd=str(__import__("pathlib").Path(__file__).resolve().parents[1]),
    ).stdout.split()
    assert out[0] == "IMPORT_ERROR", out
    assert out[1] == "False", out


# ---------------------------------------------------------------------------------------------
# Target-field validation (§9 rule 2, §13 row 36)
# ---------------------------------------------------------------------------------------------


def test_a_target_with_no_region_names_the_missing_field() -> None:
    """§9 rule 2: `bedrock`/`vertex` refuse a target with no `region`. The backend owns the FIELD
    half of the message; the loader owns profile/tier/index, which no `ModelBackend` method is
    handed."""
    with pytest.raises(MissingRegion) as caught:
        BedrockBackend().declared_capabilities(target(region=None))
    assert caught.value.field == "region"
    assert "region" in str(caught.value)
    assert MODEL_ID in str(caught.value)


def test_the_region_check_fires_before_any_transport_call() -> None:
    """Startup, not wave 7. A missing field must never reach the wire."""
    transport = FakeTransport(converse_body())
    with pytest.raises(MissingRegion):
        invoke(BedrockBackend(transport), target(region=None))
    assert transport.calls == []


def test_a_present_base_url_is_not_rejected() -> None:
    """`BackendTarget` documents `base_url` as "required by `openai_compatible`; ignored by
    others". Being stricter than a primary reference would make a shared profile unloadable for a
    reason the model does not document."""
    caps = BedrockBackend().declared_capabilities(target(base_url="https://example.invalid"))
    assert caps.supports_tools is True


# ---------------------------------------------------------------------------------------------
# Declared capabilities (§13 rows 36/37)
# ---------------------------------------------------------------------------------------------


def test_declared_capabilities_are_the_transports_real_surface() -> None:
    """`Converse` has no server-side JSON-schema mode: `toolConfig` is its only structured
    surface. Declaring `supports_json_schema` would have `negotiate()` pick a rung `invoke` has
    nowhere to put the schema for, and every first call of every run would fail."""
    caps = BedrockBackend().declared_capabilities(target())
    assert caps.supports_tools is True
    assert caps.supports_json_schema is False
    assert caps.supports_constrained_decoding is False
    assert caps.supports_system_prompt is True
    assert caps.max_context > 0
    assert caps.max_output_tokens > 0


def test_the_two_capability_declarations_agree() -> None:
    """`negotiate()` reads the booleans; `promised_mode()` reads `structured_output_modes`. A gap
    between them is `CapabilityDrift` (§13 row 37) — so a HEALTHY target answering exactly as
    declared must not produce one."""
    caps = BedrockBackend().declared_capabilities(target())
    assert client_module.negotiate(caps) is client_module.promised_mode(caps)
    assert StructuredOutputMode.PROMPTED in caps.structured_output_modes


def test_capabilities_override_is_merged_over_the_declaration() -> None:
    """"Declared in code per backend, merged with `capabilities_override`" (§7.7). The merge is
    `client.merge_capabilities`; this asserts the declaration is a value it can actually merge
    over, and that the returned object is a copy the caller cannot mutate into the module state."""
    backend = BedrockBackend()
    tgt = target(capabilities_override={"max_output_tokens": 8192})
    merged = client_module.merge_capabilities(backend.declared_capabilities(tgt), tgt)
    assert merged.max_output_tokens == 8192
    assert backend.declared_capabilities(target()).max_output_tokens != 8192


# ---------------------------------------------------------------------------------------------
# Request building
# ---------------------------------------------------------------------------------------------


def test_prompted_sends_no_structuring_field() -> None:
    """`client.py` passes `schema=None` under PROMPTED and renders the schema into the prompt
    itself. A backend must not second-guess that."""
    request = build_request(target(), TURNS, None, StructuredOutputMode.PROMPTED, 512)
    assert "toolConfig" not in request
    assert request["modelId"] == MODEL_ID
    assert request["inferenceConfig"] == {"maxTokens": 512}
    assert request["system"] == [{"text": "be terse"}]
    assert request["messages"] == [{"role": "user", "content": [{"text": "go"}]}]


def test_tool_call_submits_the_schema_verbatim_and_forces_the_tool() -> None:
    request = build_request(target(), TURNS, SCHEMA, StructuredOutputMode.TOOL_CALL, 512)
    config = request["toolConfig"]
    assert isinstance(config, dict)
    spec = config["tools"][0]["toolSpec"]
    assert spec["inputSchema"] == {"json": SCHEMA}
    assert config["toolChoice"] == {"tool": {"name": spec["name"]}}


def test_no_strict_flag_is_asserted_over_a_schema_we_did_not_write() -> None:
    """The schema is whatever `response_model.model_json_schema()` produced. Strict/enforced
    subsets require every property in `required` and forbid the numeric and string constraints
    Pydantic emits — 9 of the 12 §9 roles violate the subset across all three tiers, and strict
    applies to the whole schema TREE, not just the root. Omitted rather than set false: an absent
    key is the honest statement that we make no claim about a schema we do not control."""
    request = build_request(target(), TURNS, SCHEMA, StructuredOutputMode.TOOL_CALL, 512)
    assert "strict" not in repr(request)


def test_an_undeclared_rung_is_refused_loudly() -> None:
    """`_DECLARED` claims neither JSON_SCHEMA nor CONSTRAINED, so `negotiate()` reaches them only
    through a `capabilities_override` that is simply false. A loud refusal naming the rung beats a
    silently unstructured call whose reply then burns the whole repair budget (Rule 11)."""
    for mode in (StructuredOutputMode.JSON_SCHEMA, StructuredOutputMode.CONSTRAINED):
        with pytest.raises(BedrockTargetMisconfigured) as caught:
            build_request(target(), TURNS, SCHEMA, mode, 512)
        assert caught.value.field == "capabilities_override"


def test_effort_is_omitted_when_the_target_declares_none() -> None:
    """A sibling lane is making `BackendTarget.effort` optional (`str | None`), where `None` means
    "do not send the parameter" — the current non-optional `"medium"` default transmits a value the
    operator never wrote. The omit-on-`None` branch is implemented and asserted here NOW so it is
    correct the moment that lands; today's `BackendTarget` cannot hold `None`, so the builder is
    driven with a stand-in target object rather than by weakening the model."""

    class NoEffortTarget:
        backend = "bedrock"
        model_id = MODEL_ID
        region = REGION
        effort = None

    request = build_request(
        NoEffortTarget(),  # type: ignore[arg-type]
        TURNS,
        None,
        StructuredOutputMode.PROMPTED,
        512,
    )
    assert "additionalModelRequestFields" not in request


def test_effort_rides_in_additional_model_request_fields_when_declared() -> None:
    """The `Converse` request schema is model-agnostic and rejects a provider knob at top level;
    `additionalModelRequestFields` is the documented pass-through. `effort` is already part of the
    `llm_cache` key (`CacheKeyParts.effort`), so a cached row claims the call was made at the
    declared effort — discarding it would make that claim false."""
    request = build_request(target(effort="high"), TURNS, None, StructuredOutputMode.PROMPTED, 512)
    assert request["additionalModelRequestFields"] == {"output_config": {"effort": "high"}}


def test_a_conversation_with_no_user_turn_is_refused() -> None:
    with pytest.raises(BedrockTargetMisconfigured):
        build_request(
            target(),
            (Message(role="system", content="only a system turn"),),
            None,
            StructuredOutputMode.PROMPTED,
            512,
        )


# ---------------------------------------------------------------------------------------------
# Finish-reason mapping (§13 row 47)
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stop_reason", "expected"),
    [
        ("end_turn", "stop"),
        ("stop_sequence", "stop"),
        ("max_tokens", "length"),
        ("model_context_window_exceeded", "length"),
        ("tool_use", "tool_call"),
        ("content_filtered", "filtered"),
        ("guardrail_intervened", "filtered"),
        ("refusal", "refusal"),
    ],
)
def test_finish_reason_mapping(stop_reason: str, expected: str) -> None:
    reply = parse_reply(converse_body(stop_reason=stop_reason), target())
    assert reply.finish_reason == expected


def test_truncation_is_length_and_length_is_not_a_failover_trigger() -> None:
    """§13 row 47's whole content. `length` must be distinguishable from a schema failure: a
    truncated reply is retried on the SAME target with a raised cap, spending no repair and no
    failover, while a schema failure spends the repair budget and then fails the target over.
    Collapsing the two would fail one oversized call across three tiers to reproduce a single
    truncation, and this asserts the type system agrees — `length` is not a `FailoverTrigger`."""
    reply = parse_reply(converse_body(stop_reason="max_tokens"), target())
    assert reply.finish_reason == "length"
    triggers = client_module.FailoverTrigger.__args__  # type: ignore[attr-defined]
    assert "length" not in triggers
    assert "LENGTH" not in triggers


def test_an_unmapped_stop_reason_is_raised_never_guessed() -> None:
    """Guessing `stop` would hand `client.py` a reply it would validate; guessing `length` would
    have it re-ask an identical oversized request until the truncation budget was spent."""
    with pytest.raises(MalformedReply):
        parse_reply(converse_body(stop_reason="not-a-real-stop-reason"), target())


def test_a_body_with_no_output_message_fails_over(monkeypatch: pytest.MonkeyPatch) -> None:
    """A structurally unreadable body is the ENDPOINT's fault, so it is a §11.8 `SERVER_ERROR`
    trigger and the next target gets its chance."""
    del monkeypatch
    with pytest.raises(TransportError) as caught:
        parse_reply({"stopReason": "end_turn"}, target())
    assert caught.value.trigger == "SERVER_ERROR"


# ---------------------------------------------------------------------------------------------
# Reply parsing
# ---------------------------------------------------------------------------------------------


def test_tool_arguments_are_delivered_as_an_object_not_scraped_from_text() -> None:
    reply = parse_reply(
        converse_body(stop_reason="tool_use", text=None, tool_input={"verdict": "ok"}),
        target(),
    )
    assert reply.tool_arguments == {"verdict": "ok"}
    assert reply.text is None


def test_a_missing_usage_block_reads_as_zero_rather_than_crashing() -> None:
    """`TokenUsage` bounds its counters `ge=0`. Under-reporting is visible in the ledger; a
    `ValidationError` on an otherwise good reply loses the answer."""
    body = converse_body()
    del body["usage"]
    reply = parse_reply(body, target())
    assert reply.usage.input_tokens == 0
    assert reply.usage.output_tokens == 0


def test_cost_is_not_guessed_by_the_backend() -> None:
    """Pricing is the target's declaration and `_stamp` applies it: a $0.00 call comes from
    `price: free`, never from a backend inventing a number (§11.2, §9 rule 5)."""
    assert parse_reply(converse_body(), target()).usage.cost_usd == 0.0


# ---------------------------------------------------------------------------------------------
# THE cache-key regression (§11.6, §13 row 39)
# ---------------------------------------------------------------------------------------------


def test_usage_model_id_echoes_the_config_string_even_when_bedrock_reports_another() -> None:
    """**The highest-consequence assertion in this file.**

    `_stamp` resolves `usage.model_id or target.model_id` — backend-reported WINS. The `llm_cache`
    READ key is built from the config string (`_key_parts`, from `route.targets[0].model_id`) while
    the WRITE key is built from `usage.model_id` (`_store_response`). Bedrock echoes back an
    inference-profile ARN or a `us.`-prefixed cross-region id, and both `TokenUsage.model_id`'s own
    comment ("the RESOLVED model id, as the backend reported it") and `state/schema.sql`'s
    ("RESOLVED id") invite passing it through. Doing so makes read key != write key on EVERY call:
    a permanent, total cache miss across the whole fleet, silent and indistinguishable from a cold
    cache because `attempts.llm_cache_hit` simply stays 0.

    So the transport is faked reporting a DIFFERENT model name, and the two real `CacheKeyParts`
    are computed and compared.
    """
    served = "arn:aws:bedrock:us-east-1:000000000000:inference-profile/us.something-else"
    tgt = target()
    transport = FakeTransport(converse_body(served_model_id=served))
    reply = invoke(BedrockBackend(transport), tgt)

    assert reply.usage.model_id == tgt.model_id
    assert reply.usage.model_id != served
    assert reply.usage.backend == BedrockBackend.name

    common: dict[str, Any] = {
        "role": "transform_repair",
        "tier": ModelTier.HEAVY,
        "backend": tgt.backend,
        "effort": tgt.effort,
        "prompt_sha256": "0" * 64,
        "response_schema_sha256": "1" * 64,
    }
    read_key = CacheKeyParts(model_id=tgt.model_id, **common).compute()
    write_key = CacheKeyParts(model_id=reply.usage.model_id or tgt.model_id, **common).compute()
    poisoned = CacheKeyParts(model_id=served, **common).compute()
    assert read_key == write_key
    assert read_key != poisoned


# ---------------------------------------------------------------------------------------------
# Error classification (§11.8 triggers)
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "status", "trigger"),
    [
        ("ThrottlingException", 429, "RATE_LIMIT"),
        ("ProvisionedThroughputExceededException", 429, "RATE_LIMIT"),
        ("InternalServerException", 500, "SERVER_ERROR"),
        ("ServiceUnavailableException", 503, "SERVER_ERROR"),
        ("ModelTimeoutException", 408, "CONNECTION"),
        ("SomeUnknown5xx", 502, "SERVER_ERROR"),
    ],
)
def test_transient_faults_become_failover_triggers(code: str, status: int, trigger: str) -> None:
    """§11.8 triggers 1-3. A rate limit keeps its own name: §13 row 43 turns on a 429 alone never
    being readable as an outage, which only holds if the two arrive under different triggers."""
    exc = bedrock_module.ClientError(
        {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}},
    )
    error = bedrock_module._from_client_error(REGION, exc)
    assert isinstance(error, TransportError)
    assert error.trigger == trigger


def test_a_bad_request_fails_the_task_instead_of_walking_the_tier() -> None:
    """A 4xx that is not a throttle is OUR request being wrong. The next target reproduces it
    exactly, so failing loudly beats spending the tier's whole ladder (Rule 11, §11.8)."""
    exc = bedrock_module.ClientError(
        {"Error": {"Code": "ValidationException"}, "ResponseMetadata": {"HTTPStatusCode": 400}},
    )
    error = bedrock_module._from_client_error(REGION, exc)
    assert not isinstance(error, TransportError)
    assert "ValidationException" in str(error)


# ---------------------------------------------------------------------------------------------
# Boundary hygiene
# ---------------------------------------------------------------------------------------------


def test_no_model_id_or_endpoint_is_hard_coded_in_the_adapter() -> None:
    """§12.40: `config/models.yaml` is the only place a model id may appear. The adapter's model
    string comes off the `BackendTarget` and nothing else."""
    source = bedrock_module.__file__
    assert source is not None
    text = __import__("pathlib").Path(source).read_text(encoding="utf-8")
    body = text.split('"""', 2)[-1]  # drop the module docstring, which quotes SPEC prose
    for forbidden in ("claude-", "gpt-", "amazonaws.com", "https://"):
        assert forbidden not in body, f"{forbidden!r} is hard-coded in bedrock.py"


def test_the_declaration_is_a_copy_not_shared_module_state() -> None:
    """Two targets must not be able to mutate each other's capabilities through the module-level
    declaration."""
    first = BedrockBackend().declared_capabilities(target())
    second = BedrockBackend().declared_capabilities(target())
    assert first is not second
    assert first == second
    assert isinstance(first, ModelCapabilities)
