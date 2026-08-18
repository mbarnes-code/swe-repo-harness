"""ADR-0023 / SPEC §7.7 — the native Anthropic Messages API as a `ModelBackend`.

This is the first adapter behind the §7.7 boundary, and the ONLY module in `src/fleet/` that may
import this vendor SDK (§12.40 asserts exactly that with a grep). Nothing here decides anything:
the adapter renders a `Sequence[Message]` into one Messages-API request, reports what the
transport said, and lets `llm/client.py` own negotiation, validation, repair, truncation and
failover. It never validates a schema and never picks its own rung.

No model id and no endpoint URL appears in this file. The only strings that identify a target come
off the `BackendTarget` the router resolved from `config/models.yaml` (§9, §12.40).

Import-time contract: the `import anthropic` below is deliberately at module scope and deliberately
unguarded. `client.discover()` documents that "a backend whose SDK is not installed fails its
import here and is simply not registered", so a `try`/`except ImportError` that registered a
half-working adapter would convert a clean absence into a runtime failure on the first call.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Any, ClassVar, Final

import anthropic

from fleet.llm.client import (
    BackendReply,
    FinishReason,
    LlmError,
    MalformedReply,
    Message,
    TransportError,
    register_backend,
)
from fleet.models.enums import StructuredOutputMode
from fleet.models.tasks import BackendTarget, ModelCapabilities, TokenUsage


class AnthropicBackendError(LlmError):
    """A fault that belongs to THIS adapter's own contract rather than to the §11.8 trigger set:
    a `config/models.yaml` target this transport cannot dispatch (no `api_key_env`, an unset key,
    a rung it does not offer), or a 4xx the API rejected the request with.

    Deliberately NOT a `TransportError`. `TransportError` means "try the next target"; none of the
    faults above get better on the next target, so failing the task loudly beats spending the
    tier's whole ladder reproducing one malformed request (Rule 11, §11.8).

    *Agent Recommendation*: `client.py` ships no error for "this backend refuses this target", and
    §13 row 36 gives that check to the backend. Subclassing `LlmError` here keeps every caller's
    `except LlmError` correct without widening the shared hierarchy; promoting it to `client.py`
    once a second adapter needs the same shape is the obvious follow-up.
    """


# ---------------------------------------------------------------------------------------------
# Declared capabilities (§13 row 36: declared in code per backend, merged with
# `capabilities_override`; never probed, because a run's plan must not depend on a network call).
# ---------------------------------------------------------------------------------------------

_MAX_CONTEXT: Final = 200_000
_MAX_OUTPUT_TOKENS: Final = 64_000
"""The FLOOR across the model family the shipped `default` profile declares, not the ceiling of the
largest one. `ModelCapabilities` is declared per BACKEND, not per target, so a single number has to
hold for every target routed through this transport; declaring the smallest of them means the cap
`client.py` computes (`min(requested, caps.max_output_tokens)`) is always a request the transport
will accept. A profile that routes only to a larger-window target raises both with
`capabilities_override` — which is precisely the seam §13 row 36 provides."""

_DECLARED: Final = ModelCapabilities(
    supports_tools=True,
    supports_json_schema=False,
    supports_system_prompt=True,
    supports_streaming=True,
    supports_constrained_decoding=False,
    max_context=_MAX_CONTEXT,
    max_output_tokens=_MAX_OUTPUT_TOKENS,
    structured_output_modes=(StructuredOutputMode.TOOL_CALL, StructuredOutputMode.PROMPTED),
)
"""`supports_json_schema=False` is a deliberate under-declaration, not an oversight.

The API's native JSON-schema mode constrains the schema it will accept — every object needs
`additionalProperties: false`, and numeric/string constraints (`minimum`, `maxLength`, …) are not
supported. `response_model.model_json_schema()` is Pydantic output, which emits neither the former
nor omits the latter, so declaring the rung would make any `Field(ge=…)` response model a hard 4xx
on the first call rather than a negotiated fallback. `TOOL_CALL` submits the identical schema as a
mandatory tool's parameter object with no such restriction, and §7.7's own rule — "validation is
ALWAYS Pydantic on our side, at every rung" — means the rung we lose is a hit-rate hint, never a
guarantee. An operator whose response models happen to be schema-clean turns it on per target with
`capabilities_override: {supports_json_schema: true}`; the `JSON_SCHEMA` branch of `invoke` is
implemented for exactly that path.

*Agent Recommendation.* Both halves of the pair are declared consistently, so `promised_mode` and
`negotiate` agree and a healthy target is never libelled with `CapabilityDrift` (§13 row 37)."""


# ---------------------------------------------------------------------------------------------
# Transport constants
# ---------------------------------------------------------------------------------------------

_MAX_RETRIES: Final = 4
"""SPEC §7.7's shipped-backends table: "SDK-native retries left on (`max_retries=4`, §11.8)". The
SDK's transient layer is the "own transient layer" `TransportError`'s docstring refers to — what
this adapter raises is what SURVIVED it, which is what makes a §11.8 failover meaningful rather
than a first-blip flinch."""

_SERVER_ERROR_STATUS: Final = 500
"""The HTTP class boundary between "the endpoint is sick" (§11.8 `SERVER_ERROR`, failover) and
"our request is wrong" (fails the task). Named rather than inlined so the split is greppable."""

_TOOL_NAME: Final = "emit_response"
_TOOL_DESCRIPTION: Final = (
    "Emit the response. Call this tool exactly once, with arguments matching its schema. "
    "It is the only way to answer."
)

_FINISH_REASONS: Final[dict[str, FinishReason]] = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "model_context_window_exceeded": "length",
    "tool_use": "tool_call",
    "refusal": "refusal",
}
"""Exhaustive over the transport's `StopReason`, with two deliberate decisions.

`max_tokens` and `model_context_window_exceeded` both map to `length` and NOTHING else: §13 row 47
turns on `length` being distinguishable from a schema failure, because a truncated reply is retried
on the SAME target with a raised cap while a schema failure spends the repair budget and then fails
the target over. Collapsing either into a parse error would fail one oversized call across three
tiers to reproduce one truncation.

`pause_turn` is absent on purpose. It means a server-side tool loop paused mid-turn, and this
adapter declares no server-side tools, so seeing it means the request was not the one we built —
`_reply_from()` raises rather than guessing a rung-visible reason for it. `filtered` never appears
on the right-hand side for the same honesty reason: this transport reports a policy decline as
`refusal`, and `client.py` already treats the two identically (`ModelRefused`)."""


@register_backend
class AnthropicBackend:
    """One transport: the native Messages API. See the module docstring for what it will not do."""

    name: ClassVar[str] = "anthropic"
    version: ClassVar[int] = 1

    def declared_capabilities(self, target: BackendTarget) -> ModelCapabilities:
        """Declared, never probed. Validates the target's own fields first (§13 row 36), so a
        profile that names this backend with no `api_key_env` fails while the router is resolving
        the chain rather than in wave 7 with a repo already cloned."""
        _validate_target(target)
        return _DECLARED.model_copy()

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
        """Return the raw turn plus usage AND `finish_reason`. Decides nothing (see the module
        docstring): the reason is reported as the transport gave it and `client.py` acts on it.

        `target.effort` is sent as `output_config.effort` on EVERY request. `BackendTarget.effort`
        is a `Literal["low", "medium", "high"]` with a default, so it is never absent — and it is
        already part of the `llm_cache` key (`CacheKeyParts.effort`), which means a cached row
        already claims the call was made at that effort. Reading the field is therefore the only
        honest option: discarding an operator's declared routing parameter while the cache records
        it as honoured is a silent degradation of exactly the kind Rule 11 forbids, and a loud 4xx
        from a target that rejects the parameter is strictly better than a run that quietly bills
        at the wrong tier.

        The parameter NAME and nesting are not from memory: `output_config: {effort: ...}`, inside
        `output_config` rather than top-level. Not every model this transport can reach accepts it
        — see the fix-round report for the citation and for the one shipped target whose declared
        value the vendor documentation says will be refused. That is a `config/models.yaml`
        question, and this adapter is deliberately not the place it gets papered over."""
        _validate_target(target)
        api_key = _api_key(target)
        system, turns = _render(target, messages)

        request: dict[str, Any] = {
            "model": target.model_id,
            "max_tokens": max_output_tokens,
            "messages": turns,
            "output_config": {"effort": target.effort},
        }
        if system is not None:
            request["system"] = system
        for key, value in _rung(target, schema, mode).items():
            # `output_config` is MERGED, never replaced: `effort` and the JSON_SCHEMA rung's
            # `format` are siblings under it, and overwriting the key would silently drop the
            # operator's declared effort on exactly the rung that carries a schema.
            if key == "output_config":
                request["output_config"].update(value)
            else:
                request[key] = value

        async with anthropic.AsyncAnthropic(
            api_key=api_key,
            timeout=timeout_s,
            max_retries=_MAX_RETRIES,
        ) as sdk:
            try:
                reply = await sdk.messages.create(**request)
            except anthropic.APIConnectionError as exc:  # timeouts subclass this
                raise TransportError(f"{target.backend}: {exc}", trigger="CONNECTION") from exc
            except anthropic.RateLimitError as exc:
                raise TransportError(f"{target.backend}: {exc}", trigger="RATE_LIMIT") from exc
            except anthropic.APIStatusError as exc:
                raise _from_status(target, exc) from exc
        return _reply_from(target, reply)


# ---------------------------------------------------------------------------------------------
# Target validation (§13 row 36: "each backend validates its own target fields")
# ---------------------------------------------------------------------------------------------


def _validate_target(target: BackendTarget) -> None:
    """The one field this transport cannot dispatch without. Exactly parallel to §9 loader rule 2's
    other two cases — `openai_compatible` refuses a target with no `base_url`, `bedrock`/`vertex`
    one with no `region`.

    `base_url` is NOT rejected when present: `BackendTarget` declares it "required by
    `openai_compatible`; ignored by others", and contradicting a primary reference to be stricter
    would make a shared profile unloadable for a reason the model does not document.

    The message names the missing FIELD and the offending target. It cannot name the profile, tier
    or target index: neither `ModelBackend` method receives them, and `BackendTarget` does not carry
    them. §13 row 36 puts those three on the loader, which owns the enclosing chain — see the
    `BackendTarget.price` docstring, which says the loader "surfaces it as exit 2 naming the
    profile, tier, and target index".
    """
    if not target.api_key_env:
        raise AnthropicBackendError(
            f"target {target.backend}:{target.model_id} is missing required field 'api_key_env' — "
            f"this transport reads its key from the environment only (§9 rule 4, §11.4)",
        )


def _api_key(target: BackendTarget) -> str:
    """Read at CALL time, by name, never persisted and never echoed (§9, §11.4). An empty value is
    the same startup error as an absent one: a blank key is a 401 forty minutes into a run."""
    name = target.api_key_env or ""
    value = os.environ.get(name, "")
    if not value:
        raise AnthropicBackendError(
            f"target {target.backend}:{target.model_id} names api_key_env {name!r}, which is "
            f"unset or empty in the environment",
        )
    return value


# ---------------------------------------------------------------------------------------------
# Request rendering
# ---------------------------------------------------------------------------------------------


def _render(
    target: BackendTarget,
    messages: Sequence[Message],
) -> tuple[str | None, list[dict[str, str]]]:
    """Neutral `Message`s → one system string plus the turn list.

    `tool` turns are rendered as `assistant`. That member exists because the TOOL_CALL rung is a
    two-turn protocol and the reply's ARGUMENTS have to travel back in as a turn; on this transport
    the arguments were produced by an assistant turn, so sending them back as one shows the model
    what it actually emitted. Consecutive same-role turns are legal here and are combined by the
    API, so no interleaving filler is invented.
    """
    system = "\n\n".join(m.content for m in messages if m.role == "system")
    turns = [
        {"role": "user" if m.role == "user" else "assistant", "content": m.content}
        for m in messages
        if m.role != "system"
    ]
    if not turns:
        raise AnthropicBackendError(
            f"target {target.backend}:{target.model_id} received no non-system turns; "
            f"this transport needs at least one",
        )
    if turns[0]["role"] != "user":
        raise AnthropicBackendError(
            f"target {target.backend}:{target.model_id} received a conversation whose first "
            f"non-system turn is {turns[0]['role']!r}; this transport requires 'user'",
        )
    return (system or None, turns)


def _rung(
    target: BackendTarget,
    schema: dict[str, object] | None,
    mode: StructuredOutputMode,
) -> dict[str, Any]:
    """The rung-specific half of the request. `PROMPTED` adds nothing — `client.py` has already
    rendered the schema into the prompt, which is the floor that always exists."""
    if mode is StructuredOutputMode.PROMPTED:
        return {}
    if mode is StructuredOutputMode.CONSTRAINED:
        raise AnthropicBackendError(
            f"target {target.backend}:{target.model_id} was dispatched at the CONSTRAINED rung, "
            f"which this transport does not offer (supports_constrained_decoding is False)",
        )
    if schema is None:
        raise AnthropicBackendError(
            f"target {target.backend}:{target.model_id} was dispatched at the {mode} rung "
            f"with no schema",
        )
    if mode is StructuredOutputMode.TOOL_CALL:
        return {
            "tools": [
                {
                    "name": _TOOL_NAME,
                    "description": _TOOL_DESCRIPTION,
                    "input_schema": schema,
                },
            ],
            "tool_choice": {"type": "tool", "name": _TOOL_NAME},
        }
    return {"output_config": {"format": {"type": "json_schema", "schema": schema}}}


# ---------------------------------------------------------------------------------------------
# Response reading
# ---------------------------------------------------------------------------------------------


def _from_status(target: BackendTarget, exc: anthropic.APIStatusError) -> LlmError:
    """5xx is the §11.8 `SERVER_ERROR` trigger — the endpoint is sick, the next one may not be.
    Everything else in the 4xx range is OUR request being wrong, which the next target reproduces
    exactly, so it is raised as this adapter's own error and fails the task loudly (Rule 11)."""
    if exc.status_code >= _SERVER_ERROR_STATUS:
        return TransportError(f"{target.backend}: {exc}", trigger="SERVER_ERROR")
    return AnthropicBackendError(
        f"target {target.backend}:{target.model_id} rejected the request with HTTP "
        f"{exc.status_code}: {exc}",
    )


def _reply_from(target: BackendTarget, message: anthropic.types.Message) -> BackendReply:
    """One transport turn → `BackendReply`. `text` and `tool_arguments` stay separate because the
    TOOL_CALL rung's answer is an arguments OBJECT and scraping it back out of a string is a parser
    the harness would then own.

    **`usage.model_id` echoes `target.model_id` VERBATIM, and must never carry the dated snapshot
    id this transport returns in `message.model`.** It is the single highest-consequence line in
    this file, and both `TokenUsage.model_id`'s own comment ("the RESOLVED model id, as the backend
    reported it") and `schema.sql`'s ("RESOLVED id") actively invite the other choice. Here is why
    they must not be followed:

    `_stamp` (§7.7) resolves `usage.model_id or target.model_id` — backend-reported WINS. The
    `llm_cache` READ key is built from the config string (`_key_parts` takes `target.model_id`),
    while the WRITE key is built from `usage.model_id` (`_store_response`). Report the snapshot id
    and the two keys differ on every single call: a permanent 100% cache miss across the entire
    fleet, silent, indistinguishable from a cold cache because `attempts.llm_cache_hit` simply
    stays 0. Nothing in the harness detects it, and the bill is the only symptom.

    The snapshot id is genuinely useful provenance and is genuinely lost here. Recovering it needs
    its own field and a schema migration; it must not be smuggled through the cache key."""
    finish = _FINISH_REASONS.get(message.stop_reason or "")
    if finish is None:
        raise MalformedReply(
            f"{target.backend}:{target.model_id} returned stop_reason "
            f"{message.stop_reason!r}, which this adapter does not map to a FinishReason",
        )

    text = "".join(block.text for block in message.content if block.type == "text")
    tool_arguments: dict[str, object] | None = None
    for block in message.content:
        if block.type == "tool_use" and block.name == _TOOL_NAME:
            if not isinstance(block.input, dict):
                raise MalformedReply(
                    f"{target.backend}:{target.model_id} returned tool {_TOOL_NAME!r} with "
                    f"arguments of type {type(block.input).__name__}, not an object",
                )
            tool_arguments = {str(key): value for key, value in block.input.items()}
            break

    usage = TokenUsage(
        model_id=target.model_id,  # the CONFIG string, verbatim — see below
        input_tokens=message.usage.input_tokens,
        output_tokens=message.usage.output_tokens,
        cache_read_tokens=message.usage.cache_read_input_tokens or 0,
    )
    return BackendReply(
        text=text or None,
        tool_arguments=tool_arguments,
        usage=usage,
        finish_reason=finish,
    )
