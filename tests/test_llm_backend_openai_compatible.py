"""ADR-0023 — `src/fleet/llm/backends/openai_compatible.py` and the shipped `local` profile.

Every test here drives a FAKE `ChatTransport`. Nothing opens a socket, and the last test asserts
that mechanically rather than by convention: the transport seam exists precisely so the request
builder and the reply parser — which is where all this module's behaviour lives — are exercisable
without a server. The config tests read the SHIPPED `config/models.yaml`, because a `local`
profile that only ever loads in a fixture is a profile no operator can select.

Coroutines are driven with `asyncio.run`, matching `tests/test_llm_client.py`.
"""

from __future__ import annotations

import asyncio
import json
import textwrap
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import httpx
import pytest
import structlog.testing
from pydantic import BaseModel

from fleet.llm.backends import openai_compatible as oc
from fleet.llm.cache import (
    EMPTY_SHA256,
    CacheKeyParts,
    CachingModelClient,
    MemoryLlmCacheStore,
    _as_effort,
)
from fleet.llm.calls import prompt_sha256, prompt_template_version
from fleet.llm.client import (
    BackendReply,
    CallPolicy,
    LadderModelClient,
    LlmError,
    Message,
    TierRoute,
    TransportError,
    discover,
    merge_capabilities,
    negotiate,
    promised_mode,
)
from fleet.llm.roles import SPEC_ROLE_TIERS, LlmRouter, Role
from fleet.llm.schemas import RESPONSE_SCHEMAS, response_schema_sha256
from fleet.models.enums import ModelTier, StructuredOutputMode
from fleet.models.tasks import BackendTarget
from fleet.settings import ConfigValidationError, FleetSettings

REPO_ROOT = Path(__file__).resolve().parents[1]
SHIPPED_CONFIG = REPO_ROOT / "config"

SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {"verdict": {"type": "string"}},
    "required": ["verdict"],
}


class Answer(BaseModel):
    verdict: str


def target(**overrides: object) -> BackendTarget:
    fields: dict[str, object] = {
        "backend": "openai_compatible",
        "model_id": "local-model",
        "base_url": "http://localhost:8000/v1",
        "price": "free",
    }
    fields.update(overrides)
    return BackendTarget.model_validate(fields)


def body(
    *,
    content: str | None = "{}",
    finish_reason: object = "stop",
    tool_arguments: str | None = None,
    usage: object | None = None,
    model: str = "served-name",
) -> dict[str, object]:
    """A chat-completions body, in the shape a server actually returns."""
    message: dict[str, object] = {"role": "assistant", "content": content}
    if tool_arguments is not None:
        message["tool_calls"] = [
            {"id": "c1", "type": "function",
             "function": {"name": "emit_response", "arguments": tool_arguments}},
        ]
    return {
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7} if usage is None else usage,
    }


class FakeTransport:
    """Scripted bodies, and a record of every request. The record is the point: a payload test
    that cannot see what was sent proves nothing about the rung it claims to exercise."""

    def __init__(self, *replies: Mapping[str, object] | Exception) -> None:
        self._replies = list(replies) or [body()]
        self.requests: list[dict[str, object]] = []

    async def __call__(
        self,
        *,
        base_url: str,
        api_key: str,
        payload: Mapping[str, object],
        timeout_s: float,
    ) -> Mapping[str, object]:
        self.requests.append(
            {"base_url": base_url, "api_key": api_key, "payload": dict(payload),
             "timeout_s": timeout_s},
        )
        reply = self._replies.pop(0) if len(self._replies) > 1 else self._replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply


def invoke(
    backend: oc.OpenAICompatibleBackend,
    *,
    tgt: BackendTarget | None = None,
    messages: Sequence[Message] = (Message(role="user", content="hi"),),
    schema: dict[str, object] | None = None,
    mode: StructuredOutputMode = StructuredOutputMode.PROMPTED,
) -> BackendReply:
    return asyncio.run(
        backend.invoke(
            tgt or target(), messages, schema, mode,
            max_output_tokens=512, timeout_s=30.0,
        ),
    )


class _SingleTargetRouter:
    """A `RoleRouter` over one target. `CachingModelClient` reads the SAME router the ladder does
    — that shared route is what makes the read key and the write key comparable at all."""

    def __init__(self, tgt: BackendTarget, tier: ModelTier = ModelTier.CHEAP) -> None:
        self._tgt = tgt
        self._tier = tier

    def resolve(self, role: str, *, tier_override: ModelTier | None = None) -> TierRoute:
        return TierRoute(tier=tier_override or self._tier, targets=(self._tgt,))


# ---------------------------------------------------------------------------------------------
# The capability floor (§13 row 37)
# ---------------------------------------------------------------------------------------------


def test_declared_capabilities_are_the_prompted_floor() -> None:
    """WHY: `base_url` is opaque — the same backend name serves a 30B model behind vLLM with
    guided JSON and a 1.7B model behind llama.cpp with nothing at all. A backend that claimed
    JSON_SCHEMA by default would manufacture `CapabilityDrift` against every honest small server,
    turning the one signal §13 row 37 exists to raise into noise."""
    caps = oc.OpenAICompatibleBackend().declared_capabilities(target())

    assert caps.supports_json_schema is False
    assert caps.supports_tools is False
    assert caps.supports_constrained_decoding is False
    assert caps.structured_output_modes == (StructuredOutputMode.PROMPTED,)
    assert negotiate(caps) is StructuredOutputMode.PROMPTED
    # Floor promised == floor delivered: no drift is reportable against a target that claimed
    # nothing, which is what makes a no-structured-output model a legitimate CHEAP target.
    assert promised_mode(caps) is negotiate(caps)


def test_capabilities_override_raises_the_floor_per_target() -> None:
    """WHY: knowledge of an endpoint belongs in `config/models.yaml`, not in Python. The override
    is the operator's claim about THIS server; the backend must not pre-empt it either way."""
    backend = oc.OpenAICompatibleBackend()
    vllm = target(
        capabilities_override={"supports_json_schema": True, "max_context": 131072},
    )
    caps = merge_capabilities(backend.declared_capabilities(vllm), vllm)

    assert negotiate(caps) is StructuredOutputMode.JSON_SCHEMA
    assert caps.max_context == 131072


# ---------------------------------------------------------------------------------------------
# Request building — one rung at a time
# ---------------------------------------------------------------------------------------------


def test_prompted_rung_sends_no_structuring_field() -> None:
    """WHY: PROMPTED is the floor because it asks nothing of the server. A `response_format` or a
    `tools` array smuggled in anyway is a 400 from llama.cpp, i.e. the floor would not be a floor.
    The client passes `schema=None` at this rung and the payload must reflect that."""
    payload = oc.build_payload(
        target(), [Message(role="user", content="hi")], None,
        StructuredOutputMode.PROMPTED, 512,
    )

    assert payload["model"] == "local-model"
    assert payload["max_tokens"] == 512
    assert set(payload) == {"model", "messages", "max_tokens"}


def test_json_schema_rung_sends_the_schema_without_asserting_strict() -> None:
    """WHY: see `test_the_json_schema_rung_does_not_assert_strict_on_the_wire` — strict mode is a
    subset of JSON Schema that 9 of the 12 §9 role schemas do not satisfy (counted over the whole
    tree, `$defs` included), and we do not own those
    schemas. The absent key is the honest claim; `strict` defaults to false."""
    payload = oc.build_payload(
        target(), [Message(role="user", content="hi")], SCHEMA,
        StructuredOutputMode.JSON_SCHEMA, 512,
    )
    fmt = payload["response_format"]

    assert isinstance(fmt, dict)
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"] == {"name": "emit_response", "schema": SCHEMA}
    assert "tools" not in payload


def test_tool_call_rung_forces_the_one_function() -> None:
    """WHY: the rung's whole job is to make the model emit ONE arguments object. A `tools` array
    without a forcing `tool_choice` lets the model answer in prose instead, which lands as a
    `MalformedReply` the client then spends a repair on for no reason."""
    payload = oc.build_payload(
        target(), [Message(role="user", content="hi")], SCHEMA,
        StructuredOutputMode.TOOL_CALL, 512,
    )
    tools = payload["tools"]

    assert isinstance(tools, list)
    assert tools[0]["function"]["name"] == "emit_response"
    assert tools[0]["function"]["parameters"] == SCHEMA
    assert payload["tool_choice"] == {"type": "function", "function": {"name": "emit_response"}}


def test_constrained_rung_rides_in_extra_body() -> None:
    """WHY: `guided_json` is not part of the OpenAI request schema — it is vLLM's extension. Sent
    as a top-level field it is a 400 on a hosted endpoint, so it must travel in `extra_body`."""
    payload = oc.build_payload(
        target(), [Message(role="user", content="hi")], SCHEMA,
        StructuredOutputMode.CONSTRAINED, 512,
    )

    assert payload["extra_body"] == {"guided_json": SCHEMA}
    assert "response_format" not in payload


def test_the_tool_role_is_rendered_as_assistant() -> None:
    """WHY: `Message.role == "tool"` is the TOOL_CALL repair echo (`_repair_turns`). A real `tool`
    turn requires a `tool_call_id` bound to an assistant turn this transport was never handed, so
    an OpenAI-compatible server rejects the whole repair request — the client would then burn its
    repair budget on transport 400s instead of on the schema violation it is repairing."""
    payload = oc.build_payload(
        target(),
        [Message(role="user", content="q"), Message(role="tool", content='{"verdict": "x"}')],
        None, StructuredOutputMode.PROMPTED, 512,
    )
    messages = payload["messages"]

    assert isinstance(messages, list)
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[1]["content"] == '{"verdict": "x"}'


# ---------------------------------------------------------------------------------------------
# Reply parsing — reports, decides nothing
# ---------------------------------------------------------------------------------------------


def test_finish_reason_length_is_carried_out_verbatim() -> None:
    """WHY (§13 row 47): `length` is a truncated reply, not a schema violation. A backend that
    reported it as `stop` would hand truncated JSON to Pydantic and the client would fail the
    identical oversized call across three tiers to reproduce one truncation."""
    backend = oc.OpenAICompatibleBackend(FakeTransport(body(content="{", finish_reason="length")))
    reply = invoke(backend)

    assert reply.finish_reason == "length"


@pytest.mark.parametrize(
    ("reported", "expected"),
    [("stop", "stop"), ("max_tokens", "length"), ("eos_token", "stop"),
     ("tool_calls", "tool_call"), ("content_filter", "filtered")],
)
def test_local_server_stop_vocabularies_map_to_the_five_members(
    reported: str, expected: str,
) -> None:
    """WHY: llama.cpp and TGI answer with their own words. `max_tokens` read as anything but
    `length` loses the one signal that raises the token cap; `eos_token` read as `length` would
    grow the cap on a reply that already finished."""
    backend = oc.OpenAICompatibleBackend(
        FakeTransport(body(content='{"verdict": "ok"}', finish_reason=reported)),
    )

    assert invoke(backend).finish_reason == expected


def test_a_null_finish_reason_with_content_reads_as_stop() -> None:
    """WHY: several local servers omit `finish_reason` entirely. Failing a target that answered
    correctly would take a working endpoint out of the tier over a missing field."""
    backend = oc.OpenAICompatibleBackend(
        FakeTransport(body(content='{"verdict": "ok"}', finish_reason=None)),
    )

    assert invoke(backend).finish_reason == "stop"


def test_an_unreadable_body_is_a_loud_terminal_failure_not_an_empty_reply() -> None:
    """WHY (Rule 11): guessing `stop` on a body with no recognisable stop signal AND no content
    hands the client an empty reply, which it spends its repair budget re-asking a server that
    never answered. Failing over would only have every other target reproduce the same unmapped
    vocabulary before reporting an outage, so this raises `UnmappedFinishReason` — terminal, not a
    §11.8 failover trigger — matching `bedrock.py`/`vertex.py`'s identical type for the identical
    condition."""
    backend = oc.OpenAICompatibleBackend(
        FakeTransport(body(content=None, finish_reason="who-knows")),
    )
    with pytest.raises(oc.UnmappedFinishReason):
        invoke(backend)


def test_missing_usage_does_not_lose_an_otherwise_good_reply() -> None:
    """WHY: `TokenUsage` bounds its counts `ge=0`, and several local servers omit `usage`. A
    `ValidationError` there would throw away a correct answer over a bookkeeping field."""
    backend = oc.OpenAICompatibleBackend(
        FakeTransport(body(content='{"verdict": "ok"}', usage={})),
    )
    reply = invoke(backend)
    usage = reply.usage

    assert (usage.input_tokens, usage.output_tokens, usage.cache_read_tokens) == (0, 0, 0)
    assert usage.cost_usd == 0.0  # pricing is the target's declaration; `_stamp` applies it


def test_usage_echoes_the_config_model_id_not_the_served_name() -> None:
    """WHY (the cache): `CachingModelClient` builds its READ key from the config string
    (`_key_parts`, from `route.targets[0].model_id`) and its WRITE key from `usage.model_id`
    (`_store_response`), and `_stamp` lets the backend's value win. A local server routinely
    answers under a different name than it was asked for — a vLLM `--served-model-name`, an
    Ollama tag, a quantised build — so passing that through would make write key != read key on
    EVERY call: a permanent, silent, total cache miss. `attempts.llm_cache_hit` would simply stay
    0, which is indistinguishable from a cold cache.

    The "resolution" language around this field invites the other choice; the cache is the
    tiebreaker, and this test — not the neighbouring prose — is what holds the invariant.
    """
    backend = oc.OpenAICompatibleBackend(
        FakeTransport(body(content='{"verdict": "ok"}', model="qwen3-1.7b-q4-served")),
    )
    usage = invoke(backend, tgt=target(model_id="qwen3-1.7b")).usage

    assert usage.model_id == "qwen3-1.7b"  # the CONFIG string, verbatim
    assert (usage.input_tokens, usage.output_tokens) == (11, 7)


def test_a_server_renaming_the_model_still_produces_one_cache_key() -> None:
    """WHY (§11.6): the end-to-end form of the test above, through the real `CachingModelClient`.
    Two identical calls against a server that reports a name the profile never mentions must be a
    miss then a HIT. If the write key were built from the served name the second call would miss
    too, forever, and the only visible symptom would be a bill."""
    tgt = target(model_id="qwen3-1.7b", api_key_env=None)
    fake = FakeTransport(body(content='{"verdict": "ok"}', model="totally-different-name"))
    router = _SingleTargetRouter(tgt)
    store = MemoryLlmCacheStore()
    hits: list[object] = []
    client = CachingModelClient(
        LadderModelClient(router, {"openai_compatible": oc.OpenAICompatibleBackend(fake, env={})}),
        router,
        store,
        on_hit=hits.append,
    )

    async def two_calls() -> tuple[str, str]:
        first = await client.complete("repo_classify", [Message(role="user", content="q")], Answer)
        second = await client.complete("repo_classify", [Message(role="user", content="q")], Answer)
        return first.value.verdict, second.value.verdict

    assert asyncio.run(two_calls()) == ("ok", "ok")
    assert len(fake.requests) == 1, "the second call re-dispatched: read key != write key"
    assert len(hits) == 1
    assert len(store) == 1  # one key, not one per served name


def test_tool_arguments_are_parsed_into_an_object() -> None:
    backend = oc.OpenAICompatibleBackend(
        FakeTransport(body(content=None, finish_reason="tool_calls",
                           tool_arguments='{"verdict": "ok"}')),
    )

    assert invoke(backend).tool_arguments == {"verdict": "ok"}


def test_unparseable_tool_arguments_stay_repairable() -> None:
    """WHY: a mangled arguments string is the model's mistake, not the endpoint's. Returning
    `None` lets the client's `_validate` raise `MalformedReply` and spend ONE repair; raising a
    transport error instead would libel a healthy target and fail it over."""
    backend = oc.OpenAICompatibleBackend(
        FakeTransport(body(content=None, finish_reason="tool_calls",
                           tool_arguments='{"verdict": ')),
    )

    assert invoke(backend).tool_arguments is None


# ---------------------------------------------------------------------------------------------
# Target fields: base_url and the API key
# ---------------------------------------------------------------------------------------------


def test_a_target_without_base_url_cannot_be_dispatched() -> None:
    """WHY (§13 row 36): this is the ANY-`base_url` transport and has no vendor default. The
    loader catches this at startup for a configured target; a target built in code must not slip
    past into a connection-refused in wave 7."""
    fake = FakeTransport()
    backend = oc.OpenAICompatibleBackend(fake)
    with pytest.raises(oc.MissingBaseUrl) as excinfo:
        invoke(backend, tgt=target(base_url=None))

    assert "base_url" in str(excinfo.value)
    assert fake.requests == []  # refused BEFORE any transport call


def test_a_target_naming_no_api_key_env_still_dispatches() -> None:
    """WHY: a keyless local server is the ordinary case, and the OpenAI SDK refuses to construct
    without an api_key — so "none required" has to be spelled as a non-empty placeholder rather
    than as an empty string or a crash."""
    fake = FakeTransport(body(content='{"verdict": "ok"}'))
    backend = oc.OpenAICompatibleBackend(fake, env={})
    invoke(backend, tgt=target(api_key_env=None))

    assert fake.requests[0]["api_key"] == oc._PLACEHOLDER_API_KEY
    assert fake.requests[0]["base_url"] == "http://localhost:8000/v1"


def test_a_named_api_key_env_is_read_from_the_environment_by_name() -> None:
    """WHY (§11.4, §9): `api_key_env` is a variable NAME, never a key. Nothing in config may
    supply the value."""
    fake = FakeTransport(body(content='{"verdict": "ok"}'))
    backend = oc.OpenAICompatibleBackend(fake, env={"LOCAL_LLM_API_KEY": "dummy-value"})
    invoke(backend, tgt=target(api_key_env="LOCAL_LLM_API_KEY"))

    assert fake.requests[0]["api_key"] == "dummy-value"


def test_a_named_but_unset_api_key_env_fails_loud_naming_the_variable() -> None:
    """WHY (Rule 11): an operator who wrote the variable name meant the key to be there. Silently
    substituting the placeholder would turn a missing credential into a 401 mid-run, attributed to
    the endpoint rather than to the shell that never exported it."""
    fake = FakeTransport()
    backend = oc.OpenAICompatibleBackend(fake, env={"LOCAL_LLM_API_KEY": ""})
    with pytest.raises(oc.MissingApiKey) as excinfo:
        invoke(backend, tgt=target(api_key_env="LOCAL_LLM_API_KEY"))

    assert "LOCAL_LLM_API_KEY" in str(excinfo.value)
    assert fake.requests == []


# ---------------------------------------------------------------------------------------------
# The shipped `local` profile (config/models.yaml)
# ---------------------------------------------------------------------------------------------


def test_the_shipped_config_still_defaults_to_the_hosted_profile() -> None:
    """WHY: adding `profiles.local`/`profiles.pilot` must not re-point anybody's run.
    `default_profile` is the only thing that decides which models a `fleet run` with no flags
    actually bills."""
    settings = FleetSettings.load(SHIPPED_CONFIG, env={})

    assert settings.models.default_profile == "default"
    assert settings.profile == "default"
    assert sorted(settings.models.profiles) == ["default", "local", "pilot"]


def test_the_local_profile_loads_and_resolves_every_role() -> None:
    """WHY (§9 rules 1-2, §13 row 36): a profile whose role→tier→target chain only resolves for
    some roles fails in wave 7, not at startup. `LlmRouter` requires EVERY declared `Role`."""
    settings = FleetSettings.load(SHIPPED_CONFIG, env={}, cli_overrides={"llm.profile": "local"})
    router = LlmRouter.from_models_config(settings.models, profile="local")
    routes = router.routes()

    assert settings.profile == "local"
    assert len(routes) == 12  # §9's roles table, in full
    for _role, route in routes:
        assert route.targets
        for tgt in route.targets:
            assert tgt.backend == "openai_compatible"
            assert tgt.base_url  # §13 row 36: required by this backend, on every target
            assert tgt.price == "free"  # declared, not derived


def test_the_local_profile_reaches_the_prompted_floor_at_cheap() -> None:
    """WHY (§13 row 37, ADR-0002): the point of this profile is that a model with NO structured
    output support is still a usable CHEAP target. If CHEAP negotiated above PROMPTED the floor
    would be untested by the shipped config, and validation is Pydantic on our side regardless."""
    settings = FleetSettings.load(SHIPPED_CONFIG, env={}, cli_overrides={"llm.profile": "local"})
    router = LlmRouter.from_models_config(settings.models, profile="local")
    backend = oc.OpenAICompatibleBackend()

    rungs = {}
    for tier in (ModelTier.HEAVY, ModelTier.WORKHORSE, ModelTier.CHEAP):
        first = router.resolve("escalation", tier_override=tier).targets[0]
        rungs[tier] = negotiate(merge_capabilities(backend.declared_capabilities(first), first))

    assert rungs[ModelTier.CHEAP] is StructuredOutputMode.PROMPTED
    assert rungs[ModelTier.HEAVY] is StructuredOutputMode.JSON_SCHEMA
    assert rungs[ModelTier.WORKHORSE] is StructuredOutputMode.TOOL_CALL


def test_the_local_profile_prices_a_free_target_as_the_literal_free(tmp_path: Path) -> None:
    """WHY (§9 rule 5, `BackendTarget._free_is_declared_not_derived`): a zero-RATE `Price` is
    rejected outright, so a locally-served model's $0.00 is something an operator wrote down. The
    ledger then reads `cost_usd = 0.0` with a non-empty `backend` as a legitimate free call and
    not as a fully-cached run (§11.2, §11.6)."""
    with pytest.raises(ValueError, match="free"):
        target(price={"in_per_mtok": 0.0, "out_per_mtok": 0.0})

    # And the profile is refused if the price is simply omitted, rather than priced at $0.00.
    models = (SHIPPED_CONFIG / "models.yaml").read_text(encoding="utf-8")
    with pytest.raises(Exception, match="price"):
        FleetSettings.load(
            _write_config(tmp_path, models.replace("price: free", "effort: low")),
            env={},
            cli_overrides={"llm.profile": "local"},
        )


def test_a_local_target_without_base_url_is_refused_at_load_naming_the_field(
    tmp_path: Path,
) -> None:
    """WHY (§13 row 36): the operator's next action is to edit one line, so the error has to say
    WHICH line — profile, tier, target index, and the missing field."""
    models = (SHIPPED_CONFIG / "models.yaml").read_text(encoding="utf-8")
    broken = models.replace(
        "          base_url: 'http://localhost:8000/v1',\n"
        "          capabilities_override: { max_output_tokens: 8192 } }\n",
        "          capabilities_override: { max_output_tokens: 8192 } }\n",
    )
    assert broken != models  # the replacement actually matched

    with pytest.raises(ConfigValidationError) as excinfo:
        FleetSettings.load(
            _write_config(tmp_path, broken), env={}, cli_overrides={"llm.profile": "local"},
        )
    message = str(excinfo.value)

    assert "base_url" in message
    assert "profiles.local.CHEAP[0]" in message


def _write_config(tmp_path: Path, models: str) -> Path:
    """The shipped `fleet.yaml`/`repos.yaml` beside a doctored `models.yaml`."""
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    for name in ("fleet.yaml", "repos.yaml"):
        (config_dir / name).write_text(
            (SHIPPED_CONFIG / name).read_text(encoding="utf-8"), encoding="utf-8",
        )
    (config_dir / "models.yaml").write_text(models, encoding="utf-8")
    return config_dir


# ---------------------------------------------------------------------------------------------
# End to end through the client, and the no-socket property
# ---------------------------------------------------------------------------------------------


def test_a_prompted_only_target_answers_a_validated_value_through_the_client() -> None:
    """WHY (ADR-0002): the floor has to actually WORK end to end, not merely be declarable. A
    server that enforces nothing still yields a validated `Answer` because validation is Pydantic
    on our side at every rung — that is the invariant that makes an all-local profile viable."""
    fake = FakeTransport(body(content='```json\n{"verdict": "ok"}\n```'))
    backend = oc.OpenAICompatibleBackend(fake, env={})
    tgt = target(model_id="qwen3-1.7b", api_key_env=None)

    client = LadderModelClient(
        _SingleTargetRouter(tgt),
        {"openai_compatible": backend},
    )
    response = asyncio.run(
        client.complete("repo_classify", [Message(role="user", content="q")], Answer),
    )
    sent = fake.requests[0]["payload"]

    assert response.value.verdict == "ok"
    assert response.mode is StructuredOutputMode.PROMPTED
    assert response.usage.backend == "openai_compatible"
    assert response.usage.cost_usd == 0.0  # `price: free`, declared
    assert isinstance(sent, dict)
    assert "response_format" not in sent and "tools" not in sent
    # The schema reached the model the only way PROMPTED can carry it: inside the prompt.
    rendered = json.dumps(sent["messages"])
    assert "verdict" in rendered


def test_a_local_profile_run_through_the_cache_reports_a_miss_not_conflated_with_free(
) -> None:
    """SPEC §12.44 sub-clause F (§11.2, §11.6). `TokenUsage.all_served_from_llm_cache` is the
    value `attempts.llm_cache_hit` actually takes (`models/tasks.py`), and it must not go True
    merely because `cost_usd` is 0.0: a free local target and a cache hit are two different
    reasons for the same number, and conflating them would make a fully-local, fully FRESH run
    look fully cached in the ledger. Driven through the SHIPPED `local` profile
    (`config/models.yaml`), not a fixture target, for the same reason the module docstring gives:
    a profile that only ever loads in a fixture is a profile no operator can select.
    """
    settings = FleetSettings.load(SHIPPED_CONFIG, env={}, cli_overrides={"llm.profile": "local"})
    router = LlmRouter.from_models_config(settings.models, profile="local")
    fake = FakeTransport(body(content='```json\n{"verdict": "ok"}\n```'))
    backend = oc.OpenAICompatibleBackend(fake, env={})
    inner = LadderModelClient(router, {"openai_compatible": backend})
    store = MemoryLlmCacheStore()
    client = CachingModelClient(inner, router, store)
    messages = [Message(role="user", content="q")]

    response = asyncio.run(
        client.complete("escalation", messages, Answer, tier_override=ModelTier.CHEAP),
    )

    assert response.value.verdict == "ok"
    assert response.usage.backend == "openai_compatible"
    assert response.usage.cost_usd == 0.0  # `price: free`, declared
    assert response.usage.all_served_from_llm_cache is False  # a FRESH call, not a hit

    tgt = router.resolve("escalation", tier_override=ModelTier.CHEAP).targets[0]
    key = CacheKeyParts(
        role="escalation",
        tier=ModelTier.CHEAP,
        backend=tgt.backend,
        model_id=tgt.model_id,
        effort=tgt.effort,
        prompt_sha256=prompt_sha256(messages),
        prompt_template_version=prompt_template_version(Role.ESCALATION),
        response_schema_sha256=response_schema_sha256(Answer),
    ).compute()
    row = asyncio.run(store.get(key))
    assert row is not None
    assert row.record.backend == "openai_compatible"
    assert row.record.usage.cost_usd == 0.0  # persisted row: the same two zeros, together


def test_the_registry_holds_this_backend_under_its_config_name() -> None:
    """WHY: `BackendTarget.backend` in `config/models.yaml` is matched against the registry key by
    string. A rename here without a rename there is an `UnknownBackend` at startup."""
    assert "openai_compatible" in discover()
    assert oc.OpenAICompatibleBackend.name == "openai_compatible"


def test_no_test_in_this_module_can_reach_a_socket() -> None:
    """WHY: every test above injects `FakeTransport`, which is a convention a future edit could
    break silently. `_SdkTransport` is the ONLY path to a socket, so asserting the seam exists and
    that the default backend is the only thing holding one is the mechanical form of that claim."""
    assert isinstance(oc.OpenAICompatibleBackend()._resolve_transport(), oc._SdkTransport)
    assert not isinstance(
        oc.OpenAICompatibleBackend(FakeTransport())._resolve_transport(), oc._SdkTransport
    )

    source = textwrap.dedent(Path(oc.__file__).read_text(encoding="utf-8"))
    sdk_class = source.split("class _SdkTransport", 1)[1].split("\n@register_backend", 1)[0]

    assert "AsyncOpenAI(" in sdk_class
    assert source.count("AsyncOpenAI(") == 1  # one construction site, inside the seam


# ---------------------------------------------------------------------------------------------
# `_SdkTransport` for real, under `httpx.MockTransport` — still no network
# ---------------------------------------------------------------------------------------------


def wire(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    schema: dict[str, object] | None = SCHEMA,
    mode: StructuredOutputMode = StructuredOutputMode.JSON_SCHEMA,
    max_retries: int = 0,
) -> Mapping[str, object]:
    """Drive the REAL `_SdkTransport` — real `AsyncOpenAI`, real request serialisation — against
    an in-memory `httpx` transport. Nothing is faked below the HTTP boundary."""
    transport = oc._SdkTransport(httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                                 max_retries=max_retries)
    payload = oc.build_payload(
        target(), [Message(role="user", content="hi")], schema, mode, 512,
    )
    return asyncio.run(
        transport(base_url="http://localhost:8000/v1", api_key="k",
                  payload=payload, timeout_s=5.0),
    )


def test_the_sdk_transport_puts_the_negotiated_request_on_the_wire() -> None:
    """WHY: every other test in this file stops at `ChatTransport`, so the actual serialised
    request — the thing a server rejects — was never asserted on. This is the one test that reads
    the bytes."""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=body(content='{"verdict": "ok"}'))

    decoded = wire(handler)
    sent = seen["body"]

    assert seen["url"] == "http://localhost:8000/v1/chat/completions"
    assert seen["auth"] == "Bearer k"
    assert isinstance(sent, dict)
    assert sent["model"] == "local-model"
    assert sent["max_tokens"] == 512
    assert sent["messages"] == [{"role": "user", "content": "hi"}]
    assert decoded["choices"]  # the decoded body comes back as a plain mapping


def test_the_json_schema_rung_does_not_assert_strict_on_the_wire() -> None:
    """WHY: strict mode is a SUBSET of JSON Schema — every property must be `required` — and the
    schema is whatever `response_model.model_json_schema()` produced, which we do not control.
    9 of the 12 §9 roles declare optional fields somewhere in their schema TREE (HEAVY 5/5,
    WORKHORSE 3/4, CHEAP 1/3) — strict applies to `$defs` too. A hosted endpoint that enforces
    strict answers 400, which this backend raises as a loud, non-retryable `LlmError` (the next
    target would reproduce the same 400 for the same malformed request, so failing over would
    only spend the tier's ladder for nothing).

    vLLM ignores the flag entirely, which is exactly why a local-only fake-transport suite stayed
    green over it. Asserting on the wire is what makes this catchable at all."""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=body(content='{"verdict": "ok"}'))

    partial: dict[str, object] = {
        "type": "object",
        "properties": {"verdict": {"type": "string"}, "note": {"type": "string"}},
        "required": ["verdict"],  # `note` optional — exactly what strict mode forbids
    }
    wire(handler, schema=partial)
    sent = seen["body"]

    assert isinstance(sent, dict)
    fmt = sent["response_format"]
    assert fmt["type"] == "json_schema"
    assert "strict" not in fmt["json_schema"], "strict asserted over a schema we do not control"
    assert fmt["json_schema"]["schema"] == partial


def test_constrained_extra_body_is_merged_into_the_wire_body() -> None:
    """WHY: `extra_body` is an SDK-level argument, not a wire field. If it were sent as a literal
    `extra_body` key the server would ignore it and the CONSTRAINED rung would silently degrade to
    an unconstrained call — a rung that reports success while enforcing nothing."""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=body(content='{"verdict": "ok"}'))

    wire(handler, mode=StructuredOutputMode.CONSTRAINED)
    sent = seen["body"]

    assert isinstance(sent, dict)
    assert sent["guided_json"] == SCHEMA  # top level, not nested under `extra_body`
    assert "extra_body" not in sent


@pytest.mark.parametrize(
    ("status", "trigger"),
    [(500, "SERVER_ERROR"), (503, "SERVER_ERROR"), (429, "RATE_LIMIT")],
)
def test_http_failures_map_to_the_documented_failover_triggers(
    status: int, trigger: str,
) -> None:
    """WHY (§11.8): the trigger decides whether the ladder moves on. A 429 read as `CONNECTION`
    would retry a rate limit as if the socket were broken; a 500 read as anything but
    `SERVER_ERROR` misreports which endpoint is unhealthy."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": "nope"}})

    with pytest.raises(TransportError) as excinfo:
        wire(handler)

    assert excinfo.value.trigger == trigger


@pytest.mark.parametrize("status", [400, 401, 404])
def test_a_bad_request_fails_the_task_instead_of_walking_the_tier(status: int) -> None:
    """A 4xx that is not a 429 is OUR request being wrong (a bad model id, a schema the endpoint
    rejected, an invalid key) — the next target reproduces it exactly, so failing loudly beats
    spending the tier's whole ladder (Rule 11, §11.8), matching `anthropic.py`/`vertex.py`/
    `bedrock.py`'s identical rule and this module's own `TargetMisconfigured` docstring: "a config
    error, never a failover trigger.\""""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": "nope"}})

    with pytest.raises(LlmError) as excinfo:
        wire(handler)

    assert not isinstance(excinfo.value, TransportError)
    assert str(status) in str(excinfo.value)


def test_a_broken_socket_is_a_connection_trigger() -> None:
    """WHY: a refused connection to a local server that is simply not running is the single most
    common failure of this backend, and it must fail the target over rather than crash the run."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(TransportError) as excinfo:
        wire(handler)

    assert excinfo.value.trigger == "CONNECTION"


def test_the_sdk_transport_round_trips_through_the_backend_to_a_validated_value() -> None:
    """WHY: the full stack with only the socket replaced — `LadderModelClient` → backend →
    `_SdkTransport` → real SDK → wire → parse → Pydantic. If the pieces only fit together through
    `FakeTransport`, this is the test that says so."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body(content='{"verdict": "ok"}'))

    tgt = target(api_key_env=None)
    backend = oc.OpenAICompatibleBackend(
        oc._SdkTransport(httpx.AsyncClient(transport=httpx.MockTransport(handler)), max_retries=0),
        env={},
    )
    client = LadderModelClient(_SingleTargetRouter(tgt), {"openai_compatible": backend})
    response = asyncio.run(
        client.complete("repo_classify", [Message(role="user", content="q")], Answer),
    )

    assert response.value.verdict == "ok"
    assert response.usage.model_id == "local-model"  # config string, not the served name


# ---------------------------------------------------------------------------------------------
# Served-model observability, and the shipped truncation-retry headroom
# ---------------------------------------------------------------------------------------------


def test_a_renaming_server_is_reported_once_per_distinct_pair() -> None:
    """WHY: `_usage` discards the served name to keep the cache key stable, which leaves the
    operator blind — a vLLM `--served-model-name`, a swapped Ollama tag or a bumped hosted
    snapshot means the profile says one thing and the GPU runs another, and every artefact in the
    run attributes the output to a model nobody called.

    Deduplicated on purpose: undeduplicated this fires on every call of a 250-repo run, and a
    signal repeated thousands of times is a signal nobody reads."""
    oc._RENAME_WARNED.clear()
    fake = FakeTransport(body(content='{"verdict": "ok"}', model="qwen3-1.7b-q4-served"))
    backend = oc.OpenAICompatibleBackend(fake, env={})
    tgt = target(model_id="qwen3-1.7b", api_key_env=None)

    with structlog.testing.capture_logs() as logs:
        for _ in range(3):
            invoke(backend, tgt=tgt)

    renames = [line for line in logs if line.get("event") == "served_model_mismatch"]

    assert len(renames) == 1, "warned per call, not per distinct pair"
    assert renames[0]["declared_model_id"] == "qwen3-1.7b"
    assert renames[0]["served_model_id"] == "qwen3-1.7b-q4-served"
    assert renames[0]["log_level"] == "warning"


def test_a_server_answering_under_the_declared_name_is_silent() -> None:
    """WHY: the warning is only useful if the ordinary case does not trip it."""
    oc._RENAME_WARNED.clear()
    backend = oc.OpenAICompatibleBackend(
        FakeTransport(body(content='{"verdict": "ok"}', model="qwen3-1.7b")), env={},
    )

    with structlog.testing.capture_logs() as logs:
        invoke(backend, tgt=target(model_id="qwen3-1.7b", api_key_env=None))

    assert [line for line in logs if line.get("event") == "served_model_mismatch"] == []


def test_every_local_tier_can_actually_retry_a_truncated_reply() -> None:
    """WHY (§13 row 47): the truncation retry raises `max_output_tokens` on the SAME target and
    bounds the raise by the target's declared maximum. A target whose declared maximum equals
    `llm.default_max_output_tokens` cannot be raised at all, so `_raise_cap` turns the FIRST
    truncated reply into `BudgetExhausted` — the retry the spec promises is not merely unused on
    that target, it is unreachable. The backend's declared floor is exactly 4096, so a local
    target with no `max_output_tokens` override has this defect silently."""
    settings = FleetSettings.load(SHIPPED_CONFIG, env={}, cli_overrides={"llm.profile": "local"})
    router = LlmRouter.from_models_config(settings.models, profile="local")
    backend = oc.OpenAICompatibleBackend()
    policy = CallPolicy()

    for tier in (ModelTier.HEAVY, ModelTier.WORKHORSE, ModelTier.CHEAP):
        first = router.resolve("escalation", tier_override=tier).targets[0]
        caps = merge_capabilities(backend.declared_capabilities(first), first)
        cap = min(policy.default_max_output_tokens, caps.max_output_tokens)
        raised = min(int(cap * policy.truncation_growth), caps.max_output_tokens)

        assert raised > cap, f"{tier.value} cannot raise its cap: truncation retry is unreachable"


def test_the_strict_incompatibility_count_in_the_docs_is_still_true() -> None:
    """WHY: the reason `strict` is omitted lives in a code comment carrying a number, and a number
    in a comment with no test is how the previous count ("8 of 12", derived from ROOT schemas
    only) survived a review. Strict mode applies to every object in the schema tree, `$defs`
    included: `build_authoring`'s root is strict-clean while its `$defs.BuildTargetProposal`
    leaves three properties optional. A root-only audit therefore reports HEAVY as 4/5 and invites
    someone to re-enable strict on it, which is a permanent `TierUnavailable` for that role.

    This test recomputes the claim from the schemas themselves so the comment cannot drift."""

    def optional_somewhere(node: object, hits: list[str] | None = None) -> list[str]:
        found = [] if hits is None else hits
        if isinstance(node, dict):
            if node.get("type") == "object" and isinstance(node.get("properties"), dict):
                missing = set(node["properties"]) - set(node.get("required", []))
                found.extend(sorted(missing))
            for value in node.values():
                optional_somewhere(value, found)
        elif isinstance(node, list):
            for value in node:
                optional_somewhere(value, found)
        return found

    violating = {
        str(role)
        for role, model in RESPONSE_SCHEMAS.items()
        if optional_somewhere(model.model_json_schema())
    }
    per_tier = Counter(
        tier.value for role, tier in SPEC_ROLE_TIERS.items() if str(role) in violating
    )

    assert len(violating) == 9, f"the comment says 9 of 12; schemas say {len(violating)}"
    assert (per_tier["HEAVY"], per_tier["WORKHORSE"], per_tier["CHEAP"]) == (5, 3, 1)

    # The specific trap: root-clean, tree-dirty. If this ever flips, the tree-walk above is what
    # keeps the count honest — not the root comparison that missed it the first time.
    build_authoring = RESPONSE_SCHEMAS["build_authoring"].model_json_schema()
    root_only = set(build_authoring.get("properties", {})) - set(
        build_authoring.get("required", []),
    )

    assert root_only == set(), "build_authoring root is expected to be strict-clean"
    assert "build_authoring" in violating, "the $defs violation must still be detected"


@pytest.mark.parametrize("declared", [None, "low", "medium", "high"])
def test_effort_never_reaches_the_wire_whatever_the_target_declares(declared: str | None) -> None:
    """WHY: `effort` is a hosted reasoning-model knob. A local llama.cpp or TGI server answers an
    unknown request field with a 400, which this backend maps to `CONNECTION` — a healthy endpoint
    failed over for a parameter it never needed. `None` (the operator said nothing) and an
    explicit level must both send nothing here."""
    payload = oc.build_payload(
        target(effort=declared), [Message(role="user", content="hi")], None,
        StructuredOutputMode.PROMPTED, 512,
    )

    assert "effort" not in payload
    assert "reasoning_effort" not in payload
    assert set(payload) == {"model", "messages", "max_tokens"}


def test_an_unstated_effort_keys_differently_from_an_explicit_medium() -> None:
    """WHY (ADR-0075): `effort` is a cache-key component, and `None` exists precisely to mean "the
    operator did not say". If absence hashed the same as `"medium"`, the fabricated default this
    change removes would be back inside the cache key — one layer further from view, where the
    config no longer shows it. `""` is the stored spelling of absence, which also keeps the
    `llm_cache.effort` column `TEXT NOT NULL` and needs no migration.

    Lives here rather than in `tests/test_llm_cache.py` only to keep this round's edit contained;
    it is a `cache.py` invariant and belongs beside the other key-component tests eventually."""
    def key(effort: str | None) -> str:
        return CacheKeyParts(
            role="repo_classify", tier=ModelTier.CHEAP, backend="anthropic", model_id="m",
            effort=effort, prompt_sha256=EMPTY_SHA256, response_schema_sha256=EMPTY_SHA256,
        ).compute()

    assert key(None) != key("medium"), "absence and an explicit medium must not collide"
    assert key(None) != key("low")
    assert key(None) == key(None)  # and it is stable

    # The DB spelling round-trips, and a junk value is still loud rather than silently narrowed.
    assert _as_effort("") is None
    assert _as_effort("low") == "low"
    with pytest.raises(ValueError, match="not one of"):
        _as_effort("HIGH")
