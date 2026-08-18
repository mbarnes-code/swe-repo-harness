"""ADR-0023 `openai_compatible` — the "any `base_url`" transport (SPEC §7.7, §9, §13 row 36).

One adapter for every endpoint that speaks the OpenAI chat-completions shape: local vLLM,
Ollama, LM Studio, llama.cpp's server, TGI, and hosted OpenAI-compatible providers. What
distinguishes it from every other backend is that the *endpoint is config*, not a vendor
constant — which is exactly why `BackendTarget.base_url` is mandatory here and why a target
without one is a startup error rather than a connection refused in wave 7.

Three properties this module exists to keep true:

1. **The SDK is quarantined.** `_SdkTransport` is the only object in the harness that touches
   `openai`; everything above it moves plain mappings. That is what lets the request builder and
   the reply parser be tested without a socket. The SDK import is at MODULE scope, not deferred
   into the call, because `discover()`'s contract is that an uninstalled SDK fails this import and
   leaves the backend unregistered — which `settings.py` then turns into a §13 row 36 startup
   error naming the profile, instead of an untyped `ImportError` on the first dispatch of a long
   run. It costs one import at startup.
2. **The declared capability floor is the honest one.** `base_url` may point at a 1B model behind
   llama.cpp, so this backend declares `ModelCapabilities()` — PROMPTED only. A higher rung is a
   fact about one operator's endpoint, so it is declared per target as `capabilities_override` in
   `config/models.yaml` and merged by `merge_capabilities`. Claiming JSON_SCHEMA by default would
   manufacture `CapabilityDrift` against every honest small server (§13 row 37).
3. **It decides nothing.** No validation, no schema retry, no mode selection, no reading of
   `finish_reason` — those are the client's, so every backend behaves identically at the boundary
   (ADR-0002, `ModelBackend.invoke`).

`BackendTarget.effort` is deliberately not rendered into the request: it is a hosted
reasoning-model knob (`reasoning_effort`), and a local llama.cpp server answers an unknown field
with a 400. An endpoint that wants it can carry it in the profile as a capability-neutral concern
when a future target needs it. (*Agent Recommendation*, not a directive.)
"""

from __future__ import annotations

import json
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, ClassVar, Final, Protocol, cast

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    RateLimitError,
)

from fleet.llm.client import (
    BackendReply,
    FailoverTrigger,
    FinishReason,
    LlmError,
    Message,
    ModelBackend,
    TransportError,
    register_backend,
)
from fleet.models.enums import StructuredOutputMode
from fleet.models.tasks import BackendTarget, ModelCapabilities, TokenUsage

#: Sent when a target declares no `api_key_env`. The OpenAI SDK refuses to construct without an
#: api_key, and a local vLLM/Ollama/LM Studio server ignores whatever arrives — so "no key
#: required" has to be spelled as a placeholder, never as an empty string.
_PLACEHOLDER_API_KEY: Final[str] = "not-required"

#: The single function the TOOL_CALL rung offers. One name, because the rung's whole job is to
#: make the model emit ONE arguments object matching the response schema.
_TOOL_NAME: Final[str] = "emit_response"

#: Transport strings → the five `FinishReason` members. Wider than OpenAI's own set on purpose:
#: llama.cpp and TGI answer with their own vocabulary, and mapping their stop signal to `length`
#: would have the client re-ask an identical oversized request until the retry budget was spent.
_FINISH_REASONS: Final[Mapping[str, FinishReason]] = {
    "stop": "stop",
    "eos_token": "stop",
    "end_turn": "stop",
    "stop_sequence": "stop",
    "length": "length",
    "max_tokens": "length",
    "tool_calls": "tool_call",
    "function_call": "tool_call",
    "content_filter": "filtered",
    "refusal": "refusal",
}

#: The SDK's own transient layer. `TransportError` documents itself as what survives it, so the
#: number belongs here rather than being left to whatever the SDK's default happens to be.
_SDK_TRANSIENT_RETRIES: Final[int] = 2


# ---------------------------------------------------------------------------------------------
# Errors — typed, never bools (Rule 11). The two have two different remedies: edit the profile
# vs. export a variable, so a caller must not have to parse a message to tell them apart.
# ---------------------------------------------------------------------------------------------


class TargetMisconfigured(LlmError):
    """A `BackendTarget` this transport cannot dispatch. A config error, never a failover trigger:
    moving to the next target would not fix a missing field, it would only hide it."""

    def __init__(self, target: BackendTarget, field: str, detail: str) -> None:
        super().__init__(
            f"openai_compatible target {target.backend}:{target.model_id} {detail} "
            f"(field `{field}`)",
        )
        self.target = target
        self.field = field


class MissingBaseUrl(TargetMisconfigured):
    """§13 row 36. `settings.py` catches this at load and names profile, tier and target index;
    this is the same defect seen from the dispatch side, for a target built in code."""

    def __init__(self, target: BackendTarget) -> None:
        super().__init__(
            target,
            "base_url",
            "declares no endpoint: `openai_compatible` is the ANY-base_url transport and has no "
            "vendor default to fall back on",
        )


class MissingApiKey(TargetMisconfigured):
    """The target NAMES an `api_key_env` and the environment does not hold it. Loud at the point
    of use, exactly as `SecretRegistry.require` is (§9: keys come from the environment only). A
    target that needs no key omits `api_key_env` entirely and gets the placeholder."""

    def __init__(self, target: BackendTarget, env_name: str) -> None:
        super().__init__(
            target,
            "api_key_env",
            f"names environment variable {env_name}, which is unset or empty; export it, or drop "
            "`api_key_env` if this endpoint needs no key",
        )
        self.env_name = env_name


# ---------------------------------------------------------------------------------------------
# Transport seam
# ---------------------------------------------------------------------------------------------


class ChatTransport(Protocol):
    """One HTTP round trip, as a mapping in and a mapping out.

    The seam exists so the request builder and the reply parser — where every behaviour this
    module owns actually lives — are exercised without a socket and without the SDK. An
    implementation raises `TransportError` for connection, rate-limit and server failures and
    returns the decoded chat-completions body for everything else.
    """

    async def __call__(
        self,
        *,
        base_url: str,
        api_key: str,
        payload: Mapping[str, object],
        timeout_s: float,
    ) -> Mapping[str, object]: ...


class _SdkTransport:
    """The ONLY object in the harness that imports `openai`. Everything it hands back is a plain
    mapping, so no SDK type escapes this class."""

    async def __call__(
        self,
        *,
        base_url: str,
        api_key: str,
        payload: Mapping[str, object],
        timeout_s: float,
    ) -> Mapping[str, object]:
        body = {key: value for key, value in payload.items() if key != "extra_body"}
        extra_body = payload.get("extra_body")
        client = AsyncOpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=timeout_s,
            max_retries=_SDK_TRANSIENT_RETRIES,
        )
        # The SDK's `create` is an overloaded TypedDict-keyed signature; the payload is built
        # above from `StructuredOutputMode`, not from user input, so it is passed through one
        # cast rather than re-declaring the vendor's request type inside the harness.
        create = cast(Callable[..., Awaitable[Any]], client.chat.completions.create)
        try:
            response = await create(**body, extra_body=extra_body)
        except APITimeoutError as exc:
            raise TransportError(f"{base_url}: {exc}", trigger="CONNECTION") from exc
        except APIConnectionError as exc:
            raise TransportError(f"{base_url}: {exc}", trigger="CONNECTION") from exc
        except RateLimitError as exc:
            raise TransportError(f"{base_url}: {exc}", trigger="RATE_LIMIT") from exc
        except APIStatusError as exc:
            # 5xx is the endpoint's fault and worth failing over; a 4xx that is not a 429 is this
            # request's fault, but it is still this TARGET that rejected the shape we sent — a
            # sibling target may well accept it, so both stay failover triggers and neither is
            # swallowed.
            trigger: FailoverTrigger = (
                "SERVER_ERROR" if exc.status_code >= 500 else "CONNECTION"
            )
            raise TransportError(
                f"{base_url}: HTTP {exc.status_code}: {exc}",
                trigger=trigger,
            ) from exc
        finally:
            await client.close()
        dumped: object = response.model_dump()
        if not isinstance(dumped, Mapping):
            raise TransportError(
                f"{base_url}: chat-completions body was {type(dumped).__name__}, not an object",
                trigger="SERVER_ERROR",
            )
        return cast(Mapping[str, object], dumped)


# ---------------------------------------------------------------------------------------------
# The backend
# ---------------------------------------------------------------------------------------------


@register_backend
class OpenAICompatibleBackend:
    """`ModelBackend` for any OpenAI-shaped endpoint. Stateless per call: the transport is built
    from the TARGET, because two targets in one tier may be two different servers."""

    name: ClassVar[str] = "openai_compatible"
    version: ClassVar[int] = 1

    def __init__(
        self,
        transport: ChatTransport | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        """`register_backend` constructs this with no arguments, so both collaborators default —
        but both are injectable, which is how a test drives it with no socket and no environment
        (CLAUDE.md guardrail 3)."""
        self._transport: ChatTransport = _SdkTransport() if transport is None else transport
        self._env = env

    def declared_capabilities(self, target: BackendTarget) -> ModelCapabilities:
        """The floor, always: PROMPTED and nothing else.

        `base_url` is opaque — the same backend name serves a 235B model behind vLLM with guided
        JSON and a 1B model behind llama.cpp with neither tools nor grammars. There is no honest
        default that covers both, and `negotiate()` already treats PROMPTED as a legitimate rung
        for a legitimate CHEAP target. An operator who knows their endpoint raises it in
        `config/models.yaml` via `capabilities_override`, which is merged over this and is the
        one declaration `promised_mode` then holds the endpoint to.
        """
        return ModelCapabilities()

    async def invoke(
        self,
        target: BackendTarget,
        messages: Sequence[Message],
        schema: dict[str, object] | None,
        mode: StructuredOutputMode,
        *,
        max_output_tokens: int,
        timeout_s: float,
    ) -> BackendReply:
        """One turn out, one turn back. See `ModelBackend.invoke`: no validation, no retry on a
        schema failure, no decision taken from `finish_reason`."""
        base_url = target.base_url
        if not base_url:
            raise MissingBaseUrl(target)
        payload = build_payload(target, messages, schema, mode, max_output_tokens)
        raw = await self._transport(
            base_url=base_url,
            api_key=self._api_key(target),
            payload=payload,
            timeout_s=timeout_s,
        )
        return parse_reply(raw, target)

    def _api_key(self, target: BackendTarget) -> str:
        """`api_key_env` is a variable NAME (§11.4). Absent means "this endpoint needs no key" and
        gets the placeholder; present-but-unset is a loud failure naming the variable, because an
        operator who wrote the name meant the key to be there."""
        env_name = target.api_key_env
        if not env_name:
            return _PLACEHOLDER_API_KEY
        environ = os.environ if self._env is None else self._env
        value = environ.get(env_name, "")
        if not value:
            raise MissingApiKey(target, env_name)
        return value


# ---------------------------------------------------------------------------------------------
# Request building / reply parsing — module functions, so both are testable without the class
# ---------------------------------------------------------------------------------------------


def build_payload(
    target: BackendTarget,
    messages: Sequence[Message],
    schema: dict[str, object] | None,
    mode: StructuredOutputMode,
    max_output_tokens: int,
) -> dict[str, object]:
    """The chat-completions request for one negotiated rung.

    PROMPTED sends no structuring field at all — the client has already rendered the schema into
    the prompt and passes `schema=None`, which is precisely what makes the floor reachable on a
    server that supports nothing.
    """
    payload: dict[str, object] = {
        "model": target.model_id,
        "messages": [_render(message) for message in messages],
        "max_tokens": max_output_tokens,
    }
    if schema is None:
        return payload
    if mode is StructuredOutputMode.JSON_SCHEMA:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": _TOOL_NAME, "schema": schema, "strict": True},
        }
    elif mode is StructuredOutputMode.TOOL_CALL:
        payload["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": _TOOL_NAME,
                    "description": "Return the answer as this function's arguments object.",
                    "parameters": schema,
                },
            },
        ]
        payload["tool_choice"] = {"type": "function", "function": {"name": _TOOL_NAME}}
    elif mode is StructuredOutputMode.CONSTRAINED:
        # vLLM / llama.cpp server-side grammar. It rides in `extra_body` because it is not part
        # of the OpenAI request schema, which is also why an endpoint must DECLARE
        # `supports_constrained_decoding` before the negotiator will pick this rung.
        payload["extra_body"] = {"guided_json": schema}
    return payload


def _render(message: Message) -> dict[str, str]:
    """`Message` → one chat-completions turn. The neutral `tool` role is rendered as `assistant`:
    a real `tool` turn requires a `tool_call_id` bound to an assistant turn that this transport was
    never handed, and the only `tool` message the client sends is the TOOL_CALL repair echo, whose
    whole purpose is to show the model what it just emitted (`_repair_turns`)."""
    role = "assistant" if message.role == "tool" else message.role
    return {"role": role, "content": message.content}


def parse_reply(raw: Mapping[str, object], target: BackendTarget) -> BackendReply:
    """Decoded body → `BackendReply`. Reports, decides nothing."""
    message = _first_message(raw, target)
    text = message.get("content")
    tool_arguments = _tool_arguments(message)
    return BackendReply(
        text=text if isinstance(text, str) else None,
        tool_arguments=tool_arguments,
        usage=_usage(raw, target),
        finish_reason=_finish_reason(raw, target, text, tool_arguments),
    )


def _choice(raw: Mapping[str, object], target: BackendTarget) -> Mapping[str, object]:
    choices = raw.get("choices")
    if not isinstance(choices, Sequence) or isinstance(choices, str) or not choices:
        raise TransportError(
            f"{target.base_url}: chat-completions body carried no `choices`",
            trigger="SERVER_ERROR",
        )
    first = choices[0]
    if not isinstance(first, Mapping):
        raise TransportError(
            f"{target.base_url}: choices[0] was {type(first).__name__}, not an object",
            trigger="SERVER_ERROR",
        )
    return cast(Mapping[str, object], first)


def _first_message(raw: Mapping[str, object], target: BackendTarget) -> Mapping[str, object]:
    message = _choice(raw, target).get("message")
    if not isinstance(message, Mapping):
        raise TransportError(
            f"{target.base_url}: choices[0] carried no `message` object",
            trigger="SERVER_ERROR",
        )
    return cast(Mapping[str, object], message)


def _tool_arguments(message: Mapping[str, object]) -> dict[str, object] | None:
    """The TOOL_CALL rung's answer, as an object.

    Arguments that are absent, unparseable, or not an object all come back as `None` rather than
    as an exception: the client's `_validate` turns exactly that into a repairable
    `MalformedReply`, and spending a repair on a mangled arguments string is a strictly better
    outcome than failing the target over (§7.7).
    """
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, Sequence) or isinstance(tool_calls, str) or not tool_calls:
        return None
    first = tool_calls[0]
    if not isinstance(first, Mapping):
        return None
    function = first.get("function")
    if not isinstance(function, Mapping):
        return None
    arguments = function.get("arguments")
    if not isinstance(arguments, str):
        return None
    try:
        parsed: object = json.loads(arguments)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _finish_reason(
    raw: Mapping[str, object],
    target: BackendTarget,
    text: object,
    tool_arguments: dict[str, object] | None,
) -> FinishReason:
    """Transport vocabulary → the five members, or a loud failover.

    A body with an unrecognised stop signal AND no content is an endpoint we cannot read; guessing
    `stop` there would hand the client an empty reply and burn the repair budget re-asking a
    server that never answered. A recognised-shaped body with content but a null signal is the
    ordinary llama.cpp/TGI case and is read as `stop`.
    """
    reported = _choice(raw, target).get("finish_reason")
    if isinstance(reported, str):
        mapped = _FINISH_REASONS.get(reported)
        if mapped is not None:
            return mapped
    if (isinstance(text, str) and text) or tool_arguments is not None:
        return "stop"
    raise TransportError(
        f"{target.base_url}: finish_reason {reported!r} is unrecognised and the turn carried "
        "no content",
        trigger="SERVER_ERROR",
    )


def _usage(raw: Mapping[str, object], target: BackendTarget) -> TokenUsage:
    """Counts as the endpoint reported them, under the CONFIG's `model_id`.

    `model_id` echoes `target.model_id` verbatim and the body's own `model` field is deliberately
    discarded, which matters most exactly here. Local servers routinely answer under a different
    name than they were asked for — a vLLM `--served-model-name`, an Ollama tag, a quantised
    build, a hosted snapshot id — and two things key off this string:

      * **The cache.** `CachingModelClient` builds the READ key from the config string
        (`_key_parts`, from `route.targets[0].model_id`) and the WRITE key from
        `usage.model_id or parts.model_id` (`_store_response`). `_stamp` prefers whatever the
        backend put here. Passing the served name through would make write key != read key on
        every single call: a permanent, total cache miss that nothing detects, because
        `attempts.llm_cache_hit` stays 0 and a cache that never hits is indistinguishable from a
        cold one (§11.6). The fleet silently re-pays for every call.
      * **Failover attribution.** `cache._target_for` matches `(backend, model_id)` against the
        route's configured targets to recover the answering target's `effort`, itself a key
        component. A served name matches nothing and silently falls back to the primary's.

    Preserving what the server actually served is a legitimate want, but it needs its own field
    and a schema change; it must not be smuggled through `model_id`.

    `cost_usd` is likewise NOT set here: pricing is the target's declaration and `_stamp` applies
    it, so a local target's legitimate $0.00 comes from `price: free` and never from a backend
    guessing (§11.2).
    """
    usage = raw.get("usage")
    usage_map: Mapping[str, object] = usage if isinstance(usage, Mapping) else {}
    details = usage_map.get("prompt_tokens_details")
    details_map: Mapping[str, object] = details if isinstance(details, Mapping) else {}
    return TokenUsage(
        backend=OpenAICompatibleBackend.name,
        model_id=target.model_id,
        input_tokens=_count(usage_map.get("prompt_tokens")),
        output_tokens=_count(usage_map.get("completion_tokens")),
        cache_read_tokens=_count(details_map.get("cached_tokens")),
    )


def _count(value: object) -> int:
    """Missing, null or negative all read as 0. `TokenUsage` bounds these `ge=0`, and a server
    that omits `usage` — several local ones do — must not turn into a `ValidationError` on an
    otherwise good reply. Under-reporting is visible in the ledger; a crash loses the answer."""
    return max(value, 0) if isinstance(value, int) and not isinstance(value, bool) else 0


def _protocol_conformance(backend: OpenAICompatibleBackend) -> ModelBackend:
    """Compile-time only: `mypy --strict` fails here if this adapter drifts from `ModelBackend`."""
    return backend
