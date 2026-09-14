"""ADR-0023 / SPEC §7.7 — the `anthropic` backend adapter (`src/fleet/llm/backends/anthropic.py`).

**No test here opens a socket.** The transport is faked by monkeypatching `AsyncAnthropic` on the
vendor module the adapter actually holds, so the adapter's own `except anthropic.*` clauses stay
bound to the REAL exception classes — a stub exception hierarchy would let a mis-ordered handler
pass here and mis-classify a 429 as a permanent failure in production.

Coroutines are driven with `asyncio.run` rather than a plugin marker, matching `tests/conftest.py`
and `tests/test_llm_client.py`.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, get_args

import anthropic
import httpx
import pytest
from anthropic.types import Message as SdkMessage
from anthropic.types import StopReason
from pydantic import BaseModel

from fleet.llm import client as client_module
from fleet.llm.backends import anthropic as backend_module
from fleet.llm.backends.anthropic import (
    AnthropicBackend,
    AnthropicBackendError,
    MalformedReply,
)
from fleet.llm.cache import CachingModelClient, MemoryLlmCacheStore
from fleet.llm.client import (
    LadderModelClient,
    Message,
    TransportError,
    negotiate,
    promised_mode,
)
from fleet.llm.roles import LlmRouter
from fleet.models.enums import ModelTier, StructuredOutputMode
from fleet.models.tasks import BackendTarget, ModelCapabilities

KEY_ENV = "FLEET_TEST_ANTHROPIC_KEY"  # a NAME only; never a FLEET_* var read by settings.py


class Answer(BaseModel):
    """The typed handoff, matching the canned tool arguments in `sdk_message`."""

    verdict: str


def make_target(**overrides: Any) -> BackendTarget:
    fields: dict[str, Any] = {
        "backend": "anthropic",
        "model_id": "target-under-test",
        "api_key_env": KEY_ENV,
        "price": {"in_per_mtok": 1.0, "out_per_mtok": 2.0},
    }
    fields.update(overrides)
    return BackendTarget(**fields)


def sdk_message(
    *,
    stop_reason: str = "tool_use",
    content: list[dict[str, Any]] | None = None,
    model: str = "resolved-model-id",
    input_tokens: int = 11,
    output_tokens: int = 7,
    cache_read: int | None = 3,
) -> SdkMessage:
    """A real SDK response model, built offline. Validating through the vendor's own type is the
    point: a field this adapter reads that the SDK renames stops the test, not a production run."""
    blocks = content if content is not None else [
        {"type": "tool_use", "id": "toolu_1", "name": "emit_response", "input": {"verdict": "ok"}},
    ]
    return SdkMessage.model_validate({
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": cache_read,
        },
    })


class FakeMessages:
    def __init__(self, outcome: SdkMessage | Exception, recorder: dict[str, Any]) -> None:
        self._outcome = outcome
        self._recorder = recorder

    async def create(self, **kwargs: Any) -> SdkMessage:
        calls = self._recorder.get("calls", 0)
        self._recorder.update(kwargs)
        self._recorder["calls"] = calls + 1
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


class FakeAsyncAnthropic:
    """Stands in for the SDK client. Records construction kwargs so the §7.7 promises the adapter
    makes about `timeout_s` and `max_retries` are asserted rather than assumed."""

    def __init__(self, outcome: SdkMessage | Exception, recorder: dict[str, Any]) -> None:
        self._outcome = outcome
        self._recorder = recorder

    def __call__(self, **kwargs: Any) -> FakeAsyncAnthropic:
        self._recorder["__init__"] = kwargs
        return self

    @property
    def messages(self) -> FakeMessages:
        return FakeMessages(self._outcome, self._recorder)

    async def __aenter__(self) -> FakeAsyncAnthropic:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Install a fake transport and a key in the environment. Returns a callable that arms one
    outcome and hands back the recorder holding what the adapter actually sent."""
    monkeypatch.setenv(KEY_ENV, "not-a-real-key")

    def arm(outcome: SdkMessage | Exception) -> dict[str, Any]:
        recorder: dict[str, Any] = {}
        fake = FakeAsyncAnthropic(outcome, recorder)
        monkeypatch.setattr(backend_module.anthropic, "AsyncAnthropic", fake)
        return recorder

    return arm


def status_error(code: int) -> anthropic.APIStatusError:
    """A real SDK status error — constructed offline, never fetched."""
    request = httpx.Request("POST", "http://transport.invalid/")
    response = httpx.Response(code, request=request)
    cls = anthropic.RateLimitError if code == 429 else anthropic.APIStatusError
    return cls(f"http {code}", response=response, body=None)


# ---------------------------------------------------------------------------------------------
# (a) discover() finds the backend
# ---------------------------------------------------------------------------------------------


def test_discover_registers_the_backend_when_the_sdk_imports() -> None:
    """§7.7: "a new provider is ONE file under `llm/backends/` + one @register_backend". The
    `pkgutil` walk in `discover()` has to actually reach it — a `backends/__init__.py` that failed
    to expose `__path__`, or a module that registered under a different key than
    `BackendTarget.backend` carries, would leave `config/models.yaml`'s `anthropic` targets
    unresolvable at RunContext construction (§13 row 36) with no import error to point at."""
    registry = client_module.discover()
    assert "anthropic" in registry, sorted(registry)
    assert isinstance(registry["anthropic"], AnthropicBackend)
    assert AnthropicBackend.name == "anthropic"


def test_the_registered_object_satisfies_the_modelbackend_protocol() -> None:
    """`ModelBackend` is `@runtime_checkable`, so this is the cheap mechanical check that the two
    method signatures did not drift apart from the protocol the client codes against."""
    assert isinstance(client_module.discover()["anthropic"], client_module.ModelBackend)


def test_the_package_does_not_import_a_vendor_sdk_at_package_scope() -> None:
    """`discover()`'s contract — "a backend whose SDK is not installed fails its import here and is
    simply not registered" — is only true if the PACKAGE itself is vendor-free. If
    `backends/__init__.py` imported a vendor SDK, one missing optional extra would take out every
    backend in the package, including the ones whose SDK is installed."""
    source = Path(backend_module.__file__).parent / "__init__.py"
    forbidden = ("anthropic", "openai", "boto3", "google")
    probe = (
        "import sys; import fleet.llm.backends; "
        f"print([m for m in {forbidden!r} if m in sys.modules])"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("FLEET_")}
    env["PYTHONPATH"] = str(Path(source).parents[3])
    out = subprocess.run(  # noqa: S603
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True, env=env,
    )
    assert out.stdout.strip() == "[]", out.stdout


# ---------------------------------------------------------------------------------------------
# (b) target-field validation
# ---------------------------------------------------------------------------------------------


def test_a_target_without_api_key_env_is_rejected_by_name() -> None:
    """§13 row 36 / §9 loader rule 2: "each backend validates its own target fields". The message
    has to name the MISSING FIELD, because the operator's next action is editing that key in
    `config/models.yaml` — "invalid target" sends them reading the whole profile."""
    with pytest.raises(AnthropicBackendError) as caught:
        AnthropicBackend().declared_capabilities(make_target(api_key_env=None))
    message = str(caught.value)
    assert "api_key_env" in message
    assert "target-under-test" in message


def test_validation_happens_before_dispatch_not_on_the_first_call(transport: Any) -> None:
    """The whole point of validating in `declared_capabilities` is that the router resolves every
    chain at RunContext construction, "before a repo is cloned" (§13 row 36). Asserting it on
    `invoke` too keeps a direct caller honest."""
    transport(sdk_message())
    with pytest.raises(AnthropicBackendError, match="api_key_env"):
        asyncio.run(_invoke(make_target(api_key_env=None)))


def test_an_unset_key_variable_is_a_typed_error_that_never_echoes_the_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§9 rule 4 / §11.4: `config/models.yaml` carries a variable NAME, never a value. An unset
    variable must fail loudly with the name, and the message must not carry key material — there is
    none to carry, which is exactly the property being pinned."""
    monkeypatch.delenv(KEY_ENV, raising=False)
    with pytest.raises(AnthropicBackendError) as caught:
        asyncio.run(_invoke(make_target()))
    assert KEY_ENV in str(caught.value)


def test_a_base_url_on_the_target_is_ignored_not_rejected() -> None:
    """`BackendTarget.base_url` is documented as "required by `openai_compatible`; ignored by
    others". Refusing it here would be this adapter inventing a stricter contract than the model
    declares, and would make a shared profile unloadable for an undocumented reason."""
    caps = AnthropicBackend().declared_capabilities(make_target(base_url="http://ignored.invalid"))
    assert isinstance(caps, ModelCapabilities)


# ---------------------------------------------------------------------------------------------
# (c) finish-reason mapping
# ---------------------------------------------------------------------------------------------


def test_the_finish_reason_map_is_exhaustive_over_the_sdk_stop_reasons() -> None:
    """The mapping is only "exhaustive" if it is exhaustive against the TRANSPORT's own vocabulary.
    Pinning it to `get_args(StopReason)` means an SDK upgrade that adds a member fails here rather
    than reaching `_reply_from` at run time, where the only remaining option is to raise."""
    mapped = set(backend_module._FINISH_REASONS)
    declared = set(get_args(StopReason))
    assert mapped <= declared, mapped - declared
    assert declared - mapped == {"pause_turn"}, declared - mapped


@pytest.mark.parametrize(
    ("stop_reason", "expected"),
    [
        ("end_turn", "stop"),
        ("stop_sequence", "stop"),
        ("max_tokens", "length"),
        ("model_context_window_exceeded", "length"),
        ("tool_use", "tool_call"),
        ("refusal", "refusal"),
    ],
)
def test_each_stop_reason_maps_to_its_finish_reason(
    transport: Any, stop_reason: str, expected: str,
) -> None:
    transport(sdk_message(stop_reason=stop_reason))
    reply = asyncio.run(_invoke(make_target()))
    assert reply.finish_reason == expected


def test_a_truncated_reply_is_length_and_is_never_reported_as_a_parse_failure(
    transport: Any,
) -> None:
    """§13 row 47 is the reason `length` exists as its own member: a truncation reported as a
    schema failure fails the identical oversized call across three tiers to reproduce ONE
    truncation and writes `CapabilityDrift` against three healthy targets. So the adapter must
    surface `length` on a turn whose CONTENT is also unparseable — the two are independent, and
    `client.py` checks `length` first, before the reply ever reaches Pydantic."""
    transport(sdk_message(
        stop_reason="max_tokens",
        content=[{"type": "text", "text": '{"verdict": "ok", "sco'}],
    ))
    reply = asyncio.run(_invoke(make_target(), mode=StructuredOutputMode.PROMPTED, schema=None))
    assert reply.finish_reason == "length"
    assert reply.text == '{"verdict": "ok", "sco'
    assert reply.tool_arguments is None


def test_an_unmappable_stop_reason_fails_loud_instead_of_guessing(transport: Any) -> None:
    """`pause_turn` means a server-side tool loop paused. This adapter declares no server-side
    tools, so seeing it means the request was not the one we built. Rule 11: raise, naming the
    reason, rather than folding it into `stop` — a silent `stop` would hand a half-finished turn to
    Pydantic and blame the model for the resulting schema failure."""
    transport(sdk_message(stop_reason="pause_turn"))
    with pytest.raises(MalformedReply, match="pause_turn"):
        asyncio.run(_invoke(make_target()))


# ---------------------------------------------------------------------------------------------
# (d) capability declaration
# ---------------------------------------------------------------------------------------------


def test_declared_capabilities_are_internally_consistent() -> None:
    """The two overlapping declarations in `ModelCapabilities` have one job each: the booleans say
    what we may ATTEMPT, `structured_output_modes` says what the profile PROMISED, and the gap
    between them is `CapabilityDrift` (§13 row 37). Declaring them inconsistently would libel a
    perfectly healthy target on every single call."""
    caps = AnthropicBackend().declared_capabilities(make_target())
    assert caps.supports_tools is True
    assert caps.supports_system_prompt is True
    assert caps.supports_constrained_decoding is False
    assert negotiate(caps) is StructuredOutputMode.TOOL_CALL
    assert promised_mode(caps) is negotiate(caps)
    assert StructuredOutputMode.PROMPTED in caps.structured_output_modes


def test_the_declared_output_cap_is_a_floor_the_transport_will_accept() -> None:
    """`client.py` computes `min(requested, caps.max_output_tokens)` and sends it. Declaring the
    LARGEST family member's ceiling would make that arithmetic produce a request the smallest
    target rejects — a 4xx on every CHEAP call. The declared numbers must therefore be the floor."""
    caps = AnthropicBackend().declared_capabilities(make_target())
    assert caps.max_output_tokens <= caps.max_context
    assert caps.max_output_tokens == backend_module._MAX_OUTPUT_TOKENS
    assert caps.max_context == backend_module._MAX_CONTEXT


def test_capabilities_override_is_the_documented_way_to_raise_the_declaration() -> None:
    """§13 row 36: declared in code per backend, MERGED with `capabilities_override`. The
    under-declared JSON-schema rung is only defensible if the operator's escape hatch works."""
    target = make_target(capabilities_override={"supports_json_schema": True})
    merged = client_module.merge_capabilities(
        AnthropicBackend().declared_capabilities(target), target,
    )
    assert negotiate(merged) is StructuredOutputMode.JSON_SCHEMA


def test_the_declaration_is_not_a_shared_mutable_singleton() -> None:
    """`merge_capabilities` returns the declared object unchanged when a target carries no
    override, so handing out one module-level instance would let any caller's mutation follow every
    later target through the registry."""
    backend = AnthropicBackend()
    first = backend.declared_capabilities(make_target())
    assert first is not backend.declared_capabilities(make_target())


# ---------------------------------------------------------------------------------------------
# Request rendering and transport-error classification
# ---------------------------------------------------------------------------------------------


def test_the_tool_call_rung_forces_the_single_mandatory_tool(transport: Any) -> None:
    """§7.7 rung 2: "the schema is submitted as the parameter object of a single mandatory tool".
    Mandatory is the operative word — an unforced tool leaves the model free to answer in prose,
    which is the PROMPTED rung wearing a TOOL_CALL label in `LlmCallRecord`."""
    recorder = transport(sdk_message())
    schema: dict[str, object] = {"type": "object", "properties": {"verdict": {"type": "string"}}}
    reply = asyncio.run(_invoke(make_target(), schema=schema))

    assert recorder["tool_choice"] == {"type": "tool", "name": "emit_response"}
    assert [t["name"] for t in recorder["tools"]] == ["emit_response"]
    assert recorder["tools"][0]["input_schema"] is schema
    assert reply.tool_arguments == {"verdict": "ok"}
    # NOT "resolved-model-id": see test_usage_model_id_echoes_the_config_string_not_the_
    # resolved_snapshot for why the config string has to win here.
    assert reply.usage.model_id == "target-under-test"
    assert (reply.usage.input_tokens, reply.usage.output_tokens) == (11, 7)
    assert reply.usage.cache_read_tokens == 3


def test_system_turns_are_hoisted_and_the_tool_role_travels_as_assistant(transport: Any) -> None:
    """`supports_system_prompt` is declared True, so `client.py` leaves system turns in place and
    this adapter hoists them into the request's own `system` field. The `tool` role exists because
    the TOOL_CALL rung's repair sends the offending ARGUMENTS back as a turn; on this transport
    those were produced by an assistant turn, so that is what they go back as."""
    recorder = transport(sdk_message())
    asyncio.run(_invoke(
        make_target(),
        messages=[
            Message(role="system", content="rule one"),
            Message(role="system", content="rule two"),
            Message(role="user", content="ask"),
            Message(role="tool", content='{"verdict": "bad"}'),
            Message(role="user", content="repair"),
        ],
    ))
    assert recorder["system"] == "rule one\n\nrule two"
    assert recorder["messages"] == [
        {"role": "user", "content": "ask"},
        {"role": "assistant", "content": '{"verdict": "bad"}'},
        {"role": "user", "content": "repair"},
    ]


def test_the_prompted_floor_sends_no_tools_and_no_schema(transport: Any) -> None:
    """PROMPTED is the floor that always exists; `client.py` has already rendered the schema into
    the prompt, and `invoke` is handed `schema=None`. Sending a tool anyway would silently promote
    the rung above what was recorded."""
    recorder = transport(sdk_message(
        stop_reason="end_turn", content=[{"type": "text", "text": '{"verdict": "ok"}'}],
    ))
    reply = asyncio.run(_invoke(make_target(), mode=StructuredOutputMode.PROMPTED, schema=None))
    assert "tools" not in recorder
    # `.get(..., {})`, not a subscript: `output_config` is CONDITIONAL. It exists today only
    # because `make_target()` inherits the non-optional `effort` default; once ADR-0075 lands and
    # that default is `None`, this rung sends no `output_config` at all and a subscript would
    # raise KeyError -- reddening CI on main from a file the config lane never touched, and
    # pointing the bisect at the wrong commit. The assertion's intent is "no schema was sent",
    # which is true whether the key is absent or merely `format`-free.
    assert "format" not in recorder.get("output_config", {})
    assert reply.text == '{"verdict": "ok"}'


def test_the_constrained_rung_is_refused_rather_than_silently_downgraded(transport: Any) -> None:
    """`supports_constrained_decoding` is False, so the negotiator never picks CONSTRAINED — but a
    `capabilities_override` typo could. Rule 11: refuse, rather than quietly send a PROMPTED
    request and let `LlmCallRecord` claim a rung that never happened."""
    transport(sdk_message())
    with pytest.raises(AnthropicBackendError, match="CONSTRAINED"):
        asyncio.run(_invoke(make_target(), mode=StructuredOutputMode.CONSTRAINED))


def test_a_conversation_whose_first_non_system_turn_is_not_user_is_refused(
    transport: Any,
) -> None:
    """`_render`'s second guard, mirrored verbatim in `bedrock.py`/`vertex.py`'s own `_render`
    (both of which have a test for the "no non-system turns at all" case but, like this file
    before this test, none for THIS one): a turn list that survives the emptiness check but still
    opens on 'assistant' must never reach the transport. Sending it anyway would have the vendor
    API reject a well-formed-looking payload with a confusing 400 (or silently accept an ordering
    nothing upstream intended), instead of failing loudly at the one place that can name the
    field (Rule 11)."""
    transport(sdk_message())  # armed so a failure-to-validate would surface as a shape mismatch,
    # not merely "no transport configured" -- proving the guard fired, not that it was unreachable.
    with pytest.raises(AnthropicBackendError, match="assistant"):
        asyncio.run(_invoke(
            make_target(),
            messages=[
                Message(role="assistant", content="premature"),
                Message(role="user", content="hi"),
            ],
        ))


def test_the_transport_is_built_with_the_callers_timeout_and_the_spec_retry_count(
    transport: Any,
) -> None:
    """SPEC §7.7's shipped-backends table: "SDK-native retries left on (`max_retries=4`, §11.8)".
    That layer is what `TransportError`'s docstring means by "survived its own transient layer" —
    without it every network blip would spend a §11.8 failover."""
    recorder = transport(sdk_message())
    asyncio.run(_invoke(make_target(), timeout_s=31.5))
    assert recorder["__init__"]["timeout"] == 31.5
    assert recorder["__init__"]["max_retries"] == backend_module._MAX_RETRIES


@pytest.mark.parametrize(
    ("outcome", "trigger"),
    [
        (anthropic.APIConnectionError(request=httpx.Request("POST", "http://t.invalid/")),
         "CONNECTION"),
        (anthropic.APITimeoutError(request=httpx.Request("POST", "http://t.invalid/")),
         "CONNECTION"),
        (status_error(429), "RATE_LIMIT"),
        (status_error(503), "SERVER_ERROR"),
    ],
)
def test_failover_triggers_are_classified_per_spec_11_8(
    transport: Any, outcome: Exception, trigger: str,
) -> None:
    """§11.8's triggers 1-3, and only those. The client acts on `trigger` — reporting a 429 as
    CONNECTION or a 500 as RATE_LIMIT would move the call to the next target under the wrong label
    in the `backend_failover` event, which is the only record of why the tier walked."""
    transport(outcome)
    with pytest.raises(TransportError) as caught:
        asyncio.run(_invoke(make_target()))
    assert caught.value.trigger == trigger


def test_a_4xx_is_not_a_failover_trigger(transport: Any) -> None:
    """A malformed request reproduces EXACTLY on the next target, so failing it over spends the
    tier's whole ladder to learn nothing and ends in `TierUnavailable` — a backend-availability
    verdict on a fleet of healthy endpoints. Fail the task loudly instead (Rule 11)."""
    transport(status_error(400))
    with pytest.raises(AnthropicBackendError) as caught:
        asyncio.run(_invoke(make_target()))
    assert not isinstance(caught.value, TransportError)
    assert "400" in str(caught.value)


# ---------------------------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------------------------


async def _invoke(
    target: BackendTarget,
    *,
    messages: list[Message] | None = None,
    schema: dict[str, object] | None = None,
    mode: StructuredOutputMode = StructuredOutputMode.TOOL_CALL,
    timeout_s: float = 30.0,
) -> Any:
    if schema is None and mode is StructuredOutputMode.TOOL_CALL:
        schema = {"type": "object", "properties": {"verdict": {"type": "string"}}}
    return await AnthropicBackend().invoke(
        target,
        messages if messages is not None else [Message(role="user", content="ask")],
        schema,
        mode,
        max_output_tokens=256,
        timeout_s=timeout_s,
    )


# ---------------------------------------------------------------------------------------------
# FIX ROUND 1 — regressions for the three defects the review found
# ---------------------------------------------------------------------------------------------


def test_usage_model_id_echoes_the_config_string_not_the_resolved_snapshot(
    transport: Any,
) -> None:
    """The single highest-consequence line in the adapter.

    `_stamp` resolves `usage.model_id or target.model_id` — backend-reported WINS. The `llm_cache`
    READ key takes `target.model_id` (`_key_parts`) while the WRITE key takes `usage.model_id`
    (`_store_response`). This transport returns a DATED snapshot id in `message.model`, so
    reporting it would make read key != write key on every call: a permanent 100% cache miss
    across the whole fleet, silent, indistinguishable from a cold cache because
    `attempts.llm_cache_hit` merely stays 0. The "resolution" language around this field invited
    exactly that reading in three independent lanes, so the invariant is pinned by this test
    rather than left to the neighbouring prose."""
    transport(sdk_message(model="resolved-snapshot-20260101"))
    target = make_target(model_id="config-string")
    reply = asyncio.run(_invoke(target))
    assert reply.usage.model_id == target.model_id == "config-string"
    assert reply.usage.model_id != "resolved-snapshot-20260101"


def test_a_cache_write_and_the_next_read_produce_the_same_key(transport: Any) -> None:
    """The end-to-end form of the assertion above, driven through the real `CachingModelClient`
    over a real `LadderModelClient` wrapping this adapter.

    Two identical calls. If the write key and the read key disagree for ANY reason, the second
    reaches the transport again and `recorder["calls"]` is 2 — which is exactly the fleet-wide
    silent-miss failure, reproduced in one dict and one assertion. Nothing else in the repo
    covers it: `test_llm_cache.py` drives a `FakeClient` that sets `usage.model_id` to the config
    string by convention, so it can never observe a backend that does not."""
    target = make_target(model_id="config-string")
    router = LlmRouter(
        {"repo_classify": ModelTier.CHEAP}, {ModelTier.CHEAP: (target,)}, required_roles=(),
    )
    recorder = transport(sdk_message(model="resolved-snapshot-20260101"))
    client = CachingModelClient(
        LadderModelClient(router, {"anthropic": AnthropicBackend()}),
        router,
        MemoryLlmCacheStore(),
    )
    messages = [Message(role="user", content="classify")]

    first = asyncio.run(client.complete("repo_classify", messages, Answer))
    second = asyncio.run(client.complete("repo_classify", messages, Answer))

    assert first.value.verdict == second.value.verdict == "ok"
    assert recorder["calls"] == 1, "the second call re-hit the transport: write key != read key"


def test_the_declared_effort_is_sent_under_output_config(transport: Any) -> None:
    """§9 declares `effort:` per target and `CacheKeyParts.effort` already records it as part of
    the cache identity, so a cached row CLAIMS the call was made at that effort. Discarding the
    field while the cache says otherwise is a silent degradation (Rule 11). The nesting matters —
    `effort` is a key inside `output_config`, not a top-level request parameter."""
    recorder = transport(sdk_message())
    asyncio.run(_invoke(make_target(effort="low")))
    assert recorder["output_config"]["effort"] == "low"
    assert "effort" not in recorder


def test_effort_and_the_json_schema_rung_coexist_under_output_config(transport: Any) -> None:
    """`effort` and the JSON_SCHEMA rung's `format` are siblings under `output_config`. Building
    the rung fragment by assignment rather than merge would silently drop the operator's declared
    effort on exactly the rung that carries a schema — and the cache would still record it."""
    recorder = transport(sdk_message(
        stop_reason="end_turn", content=[{"type": "text", "text": "{}"}],
    ))
    schema: dict[str, object] = {"type": "object", "properties": {}}
    asyncio.run(_invoke(
        make_target(effort="high"), mode=StructuredOutputMode.JSON_SCHEMA, schema=schema,
    ))
    assert recorder["output_config"]["effort"] == "high"
    assert recorder["output_config"]["format"] == {"type": "json_schema", "schema": schema}


STARTUP_PROBE = '''
import json, pathlib, sys, tempfile

import fleet.cli as cli
from fleet.llm.client import registry

# Snapshot BEFORE anything touches the registry. `import fleet.cli` alone must not register a
# backend -- if it did, this probe would pass for the wrong reason.
before = {"imported": "fleet.llm.backends.anthropic" in sys.modules, "registry": sorted(registry())}

from tests.test_cli import write_config  # writes three YAML files; imports no backend

config = write_config(pathlib.Path(tempfile.mkdtemp()))
settings = cli._load_settings(cli.GlobalOptions(config_path=config))

print(json.dumps({
    "before": before,
    "after": sorted(registry()),
    "profile_loaded": settings.profile,
}))
'''


def test_the_cli_startup_path_calls_discover_without_anyone_importing_the_backend() -> None:
    """The defect this guards is the one a backend's own unit tests structurally cannot see.

    `@register_backend` fires on IMPORT. This test module imports the adapter directly, so every
    other test here observes a populated registry no matter what the CLI does. In a real run
    nothing imported `fleet.llm.backends`: `discover()` had zero call sites in `src/`,
    `RunContext.backends` defaulted to `None` so `LadderModelClient` fell back to an empty
    `registry()`, and §9 rule 2 validated `backend:` against the hard-coded `SHIPPED_BACKENDS`
    tuple rather than the live registry. Net effect: startup passed clean, `fleet models check
    --strict` reported UNREGISTERED, and the first `complete()` raised `UnknownBackend` in wave 7
    with repos already cloned — vacuously satisfying §13 row 36's "checked at RunContext
    construction".

    So this runs in a FRESH interpreter that imports only `fleet.cli`, asserts the registry is
    empty at that point, and then drives the real `_load_settings` startup path. Delete the
    `discover()` call and `after` comes back empty.
    """
    root = Path(backend_module.__file__).parents[4]
    env = {k: v for k, v in os.environ.items() if not k.startswith("FLEET_")}
    env["PYTHONPATH"] = os.pathsep.join([str(root), str(root / "src")])
    out = subprocess.run(  # noqa: S603
        [sys.executable, "-c", STARTUP_PROBE],
        capture_output=True, text=True, check=True, env=env, cwd=str(root),
    )
    result = json.loads(out.stdout.strip().splitlines()[-1])

    assert result["before"]["imported"] is False, "fleet.cli imported the adapter by itself"
    assert result["before"]["registry"] == [], result["before"]["registry"]
    assert "anthropic" in result["after"], (
        "the CLI startup path did not populate the §7.7 registry; every target routed through "
        "this backend is unreachable and row 36's startup gate is vacuous"
    )


def test_the_startup_gate_checks_the_live_registry_not_the_shipped_name_tuple(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§9 rule 2 must mean "this backend is REGISTERED", not "this name is spelled like one we
    ship". `SHIPPED_BACKENDS` lists four names; only the ones whose SDK actually imported are
    reachable, so validating against the constant passes a profile naming a backend that can
    never answer. `FleetSettings.load` already takes `known_backends=` for exactly this — it was
    simply never supplied."""
    from fleet import cli as cli_module
    from fleet.settings import SHIPPED_BACKENDS

    seen: dict[str, Any] = {}

    def spy(config_dir: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        raise _Stop

    monkeypatch.setattr(cli_module.FleetSettings, "load", spy)
    with pytest.raises(_Stop):
        cli_module._load_settings(cli_module.GlobalOptions())

    passed = seen["known_backends"]
    assert passed is not None, "known_backends was not supplied; the gate falls back to a constant"
    assert "anthropic" in passed
    assert tuple(passed) != SHIPPED_BACKENDS or set(passed) == set(client_module.discover())


class _Stop(Exception):
    """Stops `_load_settings` once the spy has recorded what it was called with."""


# ---------------------------------------------------------------------------------------------
# FIX ROUND 2 — ADR-0075: `effort` is optional, and absent means absent
# ---------------------------------------------------------------------------------------------


def target_without_effort() -> BackendTarget:
    """A target whose `effort` is `None` — the post-ADR-0075 shape.

    `model_copy(update=...)` rather than the validating constructor because on a base where
    `BackendTarget.effort` is still `Literal["low","medium","high"] = "medium"`,
    `BackendTarget(effort=None)` raises `ValidationError`. `model_copy` does not re-validate, so
    this produces the exact object shape ADR-0075 introduces, today. Once `effort: str | None`
    lands the validating constructor accepts `None` directly and this helper keeps returning an
    identical object — the tests below do not change.
    """
    return make_target().model_copy(update={"effort": None})


def test_an_undeclared_effort_sends_no_effort_key_at_all(transport: Any) -> None:
    """ADR-0075's whole point, at this layer.

    `None` means the operator declared no preference. It is NOT a cue for this adapter to pick a
    level of its own: doing so would restore the exact defect ADR-0075 removes — a value nobody
    wrote, transmitted to the transport and recorded in `CacheKeyParts.effort` as though it had
    been chosen — one layer further from view than the config default it replaced.

    The assertion is deliberately three-part, because "no effort" has three wrong spellings and
    only one right one: not `effort: None` (a null the API would have to interpret), not
    `effort: "medium"` (an invented default), not an empty `output_config` shell.
    """
    recorder = transport(sdk_message())
    reply = asyncio.run(_invoke(target_without_effort()))

    assert "output_config" not in recorder, recorder.get("output_config")
    assert "effort" not in recorder
    assert reply.finish_reason == "tool_call"  # the call still went through


def test_an_undeclared_effort_still_carries_the_json_schema_format(transport: Any) -> None:
    """The omission must be surgical. `effort` and `format` are siblings under `output_config`, so
    a naive "skip output_config when there is no effort" would silently drop the schema and
    downgrade the JSON_SCHEMA rung to an unconstrained call that `LlmCallRecord` still labels
    JSON_SCHEMA."""
    recorder = transport(sdk_message(
        stop_reason="end_turn", content=[{"type": "text", "text": "{}"}],
    ))
    schema: dict[str, object] = {"type": "object", "properties": {}}
    asyncio.run(_invoke(
        target_without_effort(), mode=StructuredOutputMode.JSON_SCHEMA, schema=schema,
    ))
    assert recorder["output_config"] == {"format": {"type": "json_schema", "schema": schema}}
    assert "effort" not in recorder["output_config"]


def test_a_declared_effort_is_still_forwarded_verbatim(transport: Any) -> None:
    """The other half of ADR-0075: optional does not mean ignored. A declared level must reach the
    transport unchanged, or the cache key records an effort the call never used."""
    recorder = transport(sdk_message())
    asyncio.run(_invoke(make_target(effort="low")))
    assert recorder["output_config"] == {"effort": "low"}
