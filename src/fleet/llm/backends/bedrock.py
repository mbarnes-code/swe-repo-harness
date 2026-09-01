"""ADR-0023 `bedrock` — the AWS Bedrock runtime as a `ModelBackend` (SPEC §7.7, §9, §13 row 36).

SPEC's shipped-backends table gives this transport one job and states it precisely: *"same models,
different transport"* — it "exists for failover, not for variety: a tier can list `anthropic` then
`bedrock` and survive one endpoint's outage without changing which model answers". Everything below
follows from that sentence. This adapter is not a second opinion about a model; it is a second road
to the same one, so its declared capabilities and its `FinishReason` vocabulary deliberately track
the native transport's rather than Bedrock's widest possible surface.

Four properties this module exists to keep true:

1. **The SDK is quarantined.** `_Boto3Transport` is the only object here that touches `boto3`;
   everything above it moves plain mappings, which is what lets the request builder and the reply
   parser be exercised without a socket, without credentials and without the SDK. The `import
   boto3` is at MODULE scope and unguarded on purpose: `client.discover()`'s contract is that "a
   backend whose SDK is not installed fails its import here and is simply not registered", so a
   `try`/`except ImportError` that registered a half-working adapter would convert a clean absence
   into a runtime failure on the first dispatch of a long run. `boto3` is the `bedrock` extra in
   `pyproject.toml`; on a host without it this module never registers and `settings.py` turns a
   profile that names it into a §13 row 36 startup error instead.
2. **`region` is mandatory.** §9 loader rule 2 says `bedrock`/`vertex` refuse a target with no
   `region`, and unlike `openai_compatible`'s `base_url` there is no vendor default to fall back
   on — a Bedrock model id is only meaningful in a region that hosts it.
3. **Credentials come from the environment, never from config.** The boto3 default credential
   chain (env vars, shared config, SSO, instance/task role) resolves them. That is why the shipped
   `default` profile's `bedrock` target in SPEC §9 carries `region` and no `api_key_env`: there is
   no single variable to name, and §9 rule 4 forbids secret material in `config/models.yaml`.
4. **It decides nothing.** No validation, no schema retry, no mode selection, no acting on
   `finish_reason` — those belong to `llm/client.py`, so every backend behaves identically at the
   boundary that matters (ADR-0002, `ModelBackend.invoke`).

No endpoint URL and no model id is written in this file. The only strings that identify a target
come off the `BackendTarget` the router resolved from `config/models.yaml` (§9, §12.40).

`boto3` is synchronous, so every call crosses into a worker thread via `asyncio.to_thread`.
Building the client inside that thread is deliberate too — `botocore` clients are not documented as
thread-safe to share, and the per-call cost is a credential-chain lookup, not a connection.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar, Final, Protocol, cast, get_args

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import BaseModel

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

#: The Bedrock service whose `Converse` operation this adapter speaks. Named once, because it is
#: the single vendor constant in the file and the grep in §12.40 should find it in exactly one
#: place.
_SERVICE_NAME: Final[str] = "bedrock-runtime"

#: The single tool the TOOL_CALL rung offers. One name, because the rung's whole job is to make
#: the model emit ONE arguments object matching the response schema.
_TOOL_NAME: Final[str] = "emit_response"
_TOOL_DESCRIPTION: Final[str] = (
    "Emit the response. Call this tool exactly once, with arguments matching its schema. "
    "It is the only way to answer."
)

#: SDK-native retries, matching §7.7's note on the sibling transport ("SDK-native retries left on
#: (`max_retries=4`, §11.8)"). `TransportError` documents itself as what SURVIVED the backend's own
#: transient layer, so the number belongs here rather than being left to botocore's default of 3 in
#: `legacy` mode. `standard` mode is chosen explicitly: it retries on the throttling and transient
#: error codes below, which is precisely the set that must NOT reach §11.8 on the first blip.
_SDK_RETRY: Final[Mapping[str, object]] = {"max_attempts": 4, "mode": "standard"}

_MAX_CONTEXT: Final[int] = 200_000
_MAX_OUTPUT_TOKENS: Final[int] = 64_000

_DECLARED: Final[ModelCapabilities] = ModelCapabilities(
    supports_tools=True,
    supports_json_schema=False,
    supports_system_prompt=True,
    supports_streaming=True,
    supports_constrained_decoding=False,
    max_context=_MAX_CONTEXT,
    max_output_tokens=_MAX_OUTPUT_TOKENS,
    structured_output_modes=(StructuredOutputMode.TOOL_CALL, StructuredOutputMode.PROMPTED),
)
"""Declared in code per backend (§13 row 36), merged with `capabilities_override`, never probed —
a run's plan must not depend on a network call.

`supports_json_schema=False` is a statement about the TRANSPORT, not a guess about the model.
`Converse` has no server-side JSON-schema mode at all: the only structured surface it exposes is
`toolConfig`, and a schema submitted as a tool's `inputSchema.json` is the whole of it. Declaring
the rung would have `negotiate()` pick `JSON_SCHEMA`, `invoke` then have nowhere to put the schema,
and the first call of every run fail — so the honest declaration also happens to be the only
workable one. §7.7's rule that "validation is ALWAYS Pydantic on our side, at every rung" means the
rung we do not claim costs a hit-rate hint and never a guarantee.

`max_context` / `max_output_tokens` are the FLOOR across the model family a `bedrock` target may
name, not the ceiling of the largest one: `ModelCapabilities` is declared per BACKEND, so one pair
of numbers has to hold for every target routed through this transport, and declaring the smaller
means the cap `client.py` computes (`min(requested, caps.max_output_tokens)`) is always a request
the transport will accept. A profile routing only to a larger-window target raises both with
`capabilities_override`, which is exactly the seam §13 row 36 provides.

Both halves of the declaration agree — `structured_output_modes` promises what the booleans permit
— so `promised_mode()` and `negotiate()` return the same rung and a healthy target is never
libelled with `CapabilityDrift` (§13 row 37). *Agent Recommendation.*"""

_FINISH_REASONS: Final[Mapping[str, FinishReason]] = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "model_context_window_exceeded": "length",
    "tool_use": "tool_call",
    "content_filtered": "filtered",
    "guardrail_intervened": "filtered",
    "refusal": "refusal",
}
"""Exhaustive over `Converse`'s `stopReason`, with one decision worth stating.

`max_tokens` and `model_context_window_exceeded` map to `length` and to NOTHING else. §13 row 47
turns on `length` being distinguishable from a schema failure: a truncated reply is retried on the
SAME target with a raised cap and spends no repair and no failover, while a schema failure spends
the repair budget and then fails the target over. Collapsing either into a parse error would fail
one oversized call across three tiers to reproduce a single truncation.

`content_filtered` and `guardrail_intervened` both map to `filtered` because a Bedrock guardrail
and the model's own filter are the same event from the client's side — `client.py` raises
`ModelRefused` for `refusal` and `filtered` alike, and inventing a distinction here would be a
distinction nothing downstream can act on."""

#: botocore error codes that mean "slow down". §11.8 trigger 3: a 429 is a rate limit and never on
#: its own an outage, so it is a failover trigger with its own name rather than a `SERVER_ERROR`.
_RATE_LIMIT_CODES: Final[frozenset[str]] = frozenset(
    {"ThrottlingException", "ThrottledException", "TooManyRequestsException",
     "ProvisionedThroughputExceededException", "ServiceQuotaExceededException"},
)

#: Codes that mean the endpoint (or the model behind it) is sick. §11.8 trigger 2 — the next
#: target may be perfectly healthy, so these fail over rather than failing the task.
_SERVER_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {"InternalServerException", "ServiceUnavailableException", "ModelNotReadyException",
     "ModelErrorException"},
)

#: The model took too long to answer. Structurally a connection-class fault, not a bad request.
_CONNECTION_CODES: Final[frozenset[str]] = frozenset({"ModelTimeoutException"})

#: The HTTP class boundary between "the endpoint is sick" and "our request is wrong", used when a
#: `ClientError` carries a status but a code none of the sets above name.
_SERVER_ERROR_STATUS: Final[int] = 500


# ---------------------------------------------------------------------------------------------
# Errors — typed, never bools (Rule 11). A misconfigured target and a sick endpoint have entirely
# different remedies (edit the profile vs. wait), so a caller must not have to parse a message.
# ---------------------------------------------------------------------------------------------


class BedrockTargetMisconfigured(LlmError):
    """A `BackendTarget` this transport cannot dispatch, or a request it refused as malformed.

    Deliberately NOT a `TransportError`. `TransportError` means "try the next target"; none of the
    faults raised as this class get better on the next target, so failing the task loudly beats
    spending the tier's whole ladder reproducing one bad request (Rule 11, §11.8).

    *Agent Recommendation*: `client.py` ships no error for "this backend refuses this target", and
    §13 row 36 gives that check to the backend. Subclassing `LlmError` keeps every caller's
    `except LlmError` correct without widening the shared hierarchy.
    """

    def __init__(self, target: BackendTarget, field: str, detail: str) -> None:
        super().__init__(
            f"bedrock target {target.backend}:{target.model_id} {detail} (field `{field}`)",
        )
        self.target = target
        self.field = field


class MissingRegion(BedrockTargetMisconfigured):
    """§9 loader rule 2 / §13 row 36, seen from the dispatch side.

    `settings.py` catches this at load and names the profile, the tier and the target index — the
    three things `ModelBackend` never receives, because neither of its methods is handed the
    enclosing chain and `BackendTarget` does not carry it. This is the same defect for a target
    built in code, and the message names the FIELD, which is the half the backend owns.
    """

    def __init__(self, target: BackendTarget) -> None:
        super().__init__(
            target,
            "region",
            "declares no region: a Bedrock model id is only meaningful in a region that hosts "
            "it, and this transport has no vendor default to fall back on",
        )


class UnmappedFinishReason(LlmError):
    """The transport reported a stop reason this adapter version does not know.

    A dedicated type, NOT `MalformedReply`. `MalformedReply` documents itself as "repairable
    exactly like a `ValidationError`", and `client.py` earns that by catching it around `_validate`
    — but an exception raised from `invoke` never reaches that catch. Raising it here would promise
    a repair that structurally cannot happen, so a reader tracing the failure would look for a
    repair budget that was never consulted.

    Not a `TransportError` either: failing over would have every target in the tier reproduce the
    same unreadable answer before reporting an outage, when the real defect is that this adapter is
    out of date. Loud, typed, terminal — and greppable when the vocabulary next changes (Rule 11).
    """

# ---------------------------------------------------------------------------------------------
# Transport seam
# ---------------------------------------------------------------------------------------------


class ConverseTransport(Protocol):
    """One `Converse` round trip, as a mapping in and a mapping out.

    The seam exists so the request builder and the reply parser — where every behaviour this module
    actually owns lives — are exercised without a socket, without AWS credentials and without the
    SDK. An implementation raises `TransportError` for throttling, connection and server failures
    and returns the decoded `Converse` response for everything else.
    """

    async def __call__(
        self,
        *,
        region: str,
        request: Mapping[str, object],
        timeout_s: float,
    ) -> Mapping[str, object]: ...


class _Boto3Transport:
    """The ONLY object in the harness that imports `boto3`. Everything it hands back is a plain
    mapping, so no SDK type escapes this class.

    The constructor argument defaults, so `BedrockBackend()` — and therefore `register_backend`'s
    `cls()` — still builds this with no arguments, no client and no credential lookup.
    """

    def __init__(self, retry: Mapping[str, object] | None = None) -> None:
        self._retry = dict(_SDK_RETRY if retry is None else retry)

    async def __call__(
        self,
        *,
        region: str,
        request: Mapping[str, object],
        timeout_s: float,
    ) -> Mapping[str, object]:
        return await asyncio.to_thread(self._converse, region, dict(request), timeout_s)

    def _converse(
        self,
        region: str,
        request: dict[str, object],
        timeout_s: float,
    ) -> Mapping[str, object]:
        """The blocking half, run in a worker thread. `timeout_s` is applied to BOTH botocore
        timeouts: the client's deadline is the caller's deadline, and a read timeout longer than it
        would let one target hold a slot past the point the runner already gave up on it."""
        config = BotoConfig(
            region_name=region,
            connect_timeout=timeout_s,
            read_timeout=timeout_s,
            retries=self._retry,
        )
        # `boto3.client` is untyped at the call site (botocore ships no stubs and the harness
        # declares no stub package); the response is re-read defensively by `parse_reply`, which
        # never trusts a key to be present.
        client: Any = boto3.client(_SERVICE_NAME, config=config)
        try:
            response: Any = client.converse(**request)
        except ClientError as exc:
            raise _from_client_error(region, exc) from exc
        except BotoCoreError as exc:
            # Endpoint resolution, connection and credential-chain faults all land here. They are
            # CONNECTION-class: the next target may be a different region or a different transport
            # entirely, so §11.8 gets its chance rather than the task dying on the first one.
            raise TransportError(f"{_SERVICE_NAME}/{region}: {exc}", trigger="CONNECTION") from exc
        if not isinstance(response, Mapping):
            raise TransportError(
                f"{_SERVICE_NAME}/{region}: Converse returned {type(response).__name__}, "
                "not an object",
                trigger="SERVER_ERROR",
            )
        return cast(Mapping[str, object], response)


#: The transport a default-constructed (registered) backend uses: the module's own retry
#: constant, nothing per-call. Static across every instance, so built ONCE at module scope rather
#: than per-instance (SPEC §12 item 47 — `register_backend`'s `cls()` must leave
#: `vars(inst) == {}`) — `_Boto3Transport.__call__` builds a fresh `boto3` client per call
#: regardless, so sharing this wrapper costs nothing and changes no behaviour.
_DEFAULT_TRANSPORT: Final[ConverseTransport] = _Boto3Transport()


def _from_client_error(region: str, exc: ClientError) -> LlmError:
    """A botocore `ClientError` → either a §11.8 failover trigger or a loud task failure.

    Read from the error CODE first and the HTTP status only as a fallback: the codes are the stable
    contract (`ThrottlingException` is a 429 today and would still mean "slow down" if it were not),
    and the status is absent from some botocore-synthesised errors entirely.
    """
    error = exc.response.get("Error", {}) if isinstance(exc.response, Mapping) else {}
    code = str(error.get("Code", "")) if isinstance(error, Mapping) else ""
    metadata = exc.response.get("ResponseMetadata", {}) if isinstance(exc.response, Mapping) else {}
    status = metadata.get("HTTPStatusCode", 0) if isinstance(metadata, Mapping) else 0

    trigger: FailoverTrigger | None = None
    if code in _RATE_LIMIT_CODES:
        trigger = "RATE_LIMIT"
    elif code in _SERVER_ERROR_CODES:
        trigger = "SERVER_ERROR"
    elif code in _CONNECTION_CODES:
        trigger = "CONNECTION"
    elif isinstance(status, int) and status >= _SERVER_ERROR_STATUS:
        trigger = "SERVER_ERROR"
    if trigger is not None:
        return TransportError(f"{_SERVICE_NAME}/{region}: {code or exc}", trigger=trigger)
    # Everything else in the 4xx range is OUR request being wrong — a bad model id for the region,
    # a schema the service rejected, denied access. The next target reproduces it exactly, so it
    # fails the task loudly instead of spending the tier's ladder (Rule 11).
    return LlmError(f"{_SERVICE_NAME}/{region} rejected the request: {code or ''} {exc}".strip())


# ---------------------------------------------------------------------------------------------
# The backend
# ---------------------------------------------------------------------------------------------


@register_backend
class BedrockBackend:
    """`ModelBackend` for the AWS Bedrock runtime. Stateless per call: the region comes off the
    TARGET, because two targets in one tier may be two different regions."""

    name: ClassVar[str] = "bedrock"
    version: ClassVar[int] = 1

    def __init__(self, transport: ConverseTransport | None = None) -> None:
        """`register_backend` constructs this with no arguments (`cls()`), so the collaborator is
        not stored on `self` in the registered case — `vars(inst) == {}` (SPEC §12 item 47) — and
        `invoke` resolves the shared default (`_DEFAULT_TRANSPORT`) lazily instead. Still
        injectable, which is how a test drives the adapter with no socket and no credentials
        (CLAUDE.md guardrail 3): passing a value stores it on `self`, but that instance was built
        for exactly one test and is never the registry's singleton."""
        if transport is not None:
            self._transport = transport

    def declared_capabilities(self, target: BackendTarget) -> ModelCapabilities:
        """Declared, never probed. Validates the target's own fields FIRST (§13 row 36), so a
        profile that names this backend with no `region` fails while the router is resolving the
        chain rather than in wave 7 with a repo already cloned."""
        _require_region(target)
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
        """One turn out, one turn back. See `ModelBackend.invoke`: no validation, no retry on a
        schema failure, no decision taken from `finish_reason`."""
        region = _require_region(target)
        request = build_request(target, messages, schema, mode, max_output_tokens)
        transport = getattr(self, "_transport", _DEFAULT_TRANSPORT)
        raw = await transport(region=region, request=request, timeout_s=timeout_s)
        return parse_reply(raw, target)


def _require_region(target: BackendTarget) -> str:
    """The one field this transport cannot dispatch without (§9 rule 2, §13 row 36).

    `base_url` and `api_key_env` are NOT rejected when present. `BackendTarget` documents
    `base_url` as "required by `openai_compatible`; ignored by others", and contradicting a primary
    reference to be stricter would make a shared profile unloadable for a reason the model does not
    document.
    """
    region = target.region
    if not region:
        raise MissingRegion(target)
    return region


# ---------------------------------------------------------------------------------------------
# Request building / reply parsing — module functions, so both are testable without the class
# ---------------------------------------------------------------------------------------------


def build_request(
    target: BackendTarget,
    messages: Sequence[Message],
    schema: dict[str, object] | None,
    mode: StructuredOutputMode,
    max_output_tokens: int,
) -> dict[str, object]:
    """The `Converse` request for one negotiated rung.

    PROMPTED adds no structuring field at all — `client.py` has already rendered the schema into
    the prompt and passes `schema=None` (`_call_target`), which is precisely what makes the floor
    reachable on a target that supports nothing.
    """
    system, turns = _render(target, messages)
    request: dict[str, object] = {
        "modelId": target.model_id,
        "messages": turns,
        "inferenceConfig": {"maxTokens": max_output_tokens},
    }
    if system:
        request["system"] = [{"text": system}]

    effort = _effort_to_send(target)
    if effort is not None:
        # Bedrock passes `additionalModelRequestFields` through to the model provider's native
        # request body unchanged, which is the only place a provider-specific knob may ride: the
        # `Converse` request schema itself is model-agnostic and would reject the key at top level.
        request["additionalModelRequestFields"] = {"output_config": {"effort": effort}}

    if schema is None:
        return request
    if mode is StructuredOutputMode.TOOL_CALL:
        request["toolConfig"] = {
            "tools": [
                {
                    "toolSpec": {
                        "name": _TOOL_NAME,
                        "description": _TOOL_DESCRIPTION,
                        "inputSchema": {"json": schema},
                    },
                },
            ],
            "toolChoice": {"tool": {"name": _TOOL_NAME}},
        }
        return request
    # JSON_SCHEMA and CONSTRAINED are both un-declared by `_DECLARED`, so `negotiate()` never picks
    # them for this backend unless a `capabilities_override` claimed them. That override would be
    # false — `Converse` offers neither — and a loud refusal naming the rung beats a silently
    # unstructured call whose reply then burns the whole repair budget (Rule 11).
    raise BedrockTargetMisconfigured(
        target,
        "capabilities_override",
        f"was dispatched at the {mode} rung, which the Bedrock Converse API does not offer; "
        f"this transport's only structured surface is toolConfig (the TOOL_CALL rung)",
    )


def _effort_is_expressible_as_absent(model: type[BaseModel] = BackendTarget) -> bool:
    """Can `model`'s `effort` field represent "the operator wrote no effort"?

    Today it cannot: the field is a non-optional `Literal["low","medium","high"]` defaulting to
    `"medium"`, so EVERY target carries a value and this returns `False`. ADR-0075 (a sibling lane)
    makes it `Literal["low","medium","high"] | None = None`, after which it returns `True`
    permanently and this gate stops doing anything. It is written as a predicate over the model
    rather than a hard-coded flag precisely so it self-removes rather than needing an edit at land
    time.

    `model` is a parameter, defaulting to the real `BackendTarget`, ONLY so a test can put both
    shapes in front of it. A test that asserted the answer for whichever shape happens to be on
    disk would prove nothing about the mechanism — a hard-coded `return False` would satisfy it
    today and then fail silently after the sibling lands, which is the one failure mode this
    predicate exists to prevent.
    """
    field = model.model_fields.get("effort")
    if field is None:
        return False
    return type(None) in get_args(field.annotation)


def _effort_to_send(target: BackendTarget) -> str | None:
    """The `effort` this request should carry, or `None` to omit the parameter entirely.

    Two independent reasons converge on sending NOTHING while `effort` is non-optional:

    1. **We cannot tell a declaration from a default.** With `"medium"` defaulted in, a target the
       operator never gave an effort is indistinguishable from one they did. Transmitting the
       default asserts a routing parameter nobody wrote — and `effort` is already a component of
       the `llm_cache` key (`CacheKeyParts.effort`), so a cached row would claim the call was made
       at an effort the profile never declared.
    2. **The wire shape has never been live-verified, and a rejection here is unusually
       expensive.** `additionalModelRequestFields` is an opaque pass-through: Bedrock does not
       validate its contents, the model provider does, and a provider that rejects
       `output_config` answers 400. `_from_client_error` correctly classifies a non-throttle 4xx as
       OUR request being wrong and raises a bare `LlmError` rather than a failover trigger — so an
       unverified field attached to EVERY call would fail every task on that target outright.

    The alternative considered and rejected was making that rejection a `TransportError` so it
    fails over. It is the wrong fix: every target in the tier would reproduce the identical bad
    request, walk the whole ladder and end in `TierUnavailable` (exit 8) — exactly the waste
    `_from_client_error`'s docstring exists to prevent — and it would additionally disguise a
    wire-shape bug as an endpoint outage, contaminating the §13 rows 40/43 health signals with a
    fault no endpoint has.

    Once ADR-0075 lands, `None` is the default and only an operator who explicitly wrote `effort:`
    gets the parameter — at which point a 400 IS their config error and failing loudly is right.
    Note the asymmetry with `vertex`: there the body IS the Messages API body BK1 already ships
    against, so this gate would put a failover pair out of step for no gain.
    """
    if not _effort_is_expressible_as_absent():
        return None
    effort: str | None = target.effort
    return effort


def _render(target: BackendTarget, messages: Sequence[Message]) -> tuple[str, list[dict[str, Any]]]:
    """Neutral `Message`s → one system string plus the `Converse` turn list.

    `tool` turns are rendered as `assistant`. That member exists because the TOOL_CALL rung is a
    two-turn protocol and the reply's ARGUMENTS have to travel back in as a turn; a real Bedrock
    `toolResult` block needs a `toolUseId` bound to an assistant turn this adapter was never
    handed, and the only `tool` message `client.py` sends is the repair echo, whose whole purpose
    is to show the model what it just emitted (`_repair_turns`).
    """
    system = "\n\n".join(m.content for m in messages if m.role == "system")
    turns: list[dict[str, Any]] = [
        {
            "role": "user" if m.role == "user" else "assistant",
            "content": [{"text": m.content}],
        }
        for m in messages
        if m.role != "system"
    ]
    if not turns:
        raise BedrockTargetMisconfigured(
            target,
            "messages",
            "received no non-system turns; Converse requires at least one",
        )
    if turns[0]["role"] != "user":
        raise BedrockTargetMisconfigured(
            target,
            "messages",
            f"received a conversation whose first non-system turn is {turns[0]['role']!r}; "
            f"Converse requires 'user'",
        )
    return system, turns


def parse_reply(raw: Mapping[str, object], target: BackendTarget) -> BackendReply:
    """Decoded `Converse` response → `BackendReply`. Reports, decides nothing."""
    blocks = _content_blocks(raw, target)
    text = "".join(
        str(block["text"]) for block in blocks if isinstance(block.get("text"), str)
    )
    return BackendReply(
        text=text or None,
        tool_arguments=_tool_arguments(blocks),
        usage=_usage(raw, target),
        finish_reason=_finish_reason(raw, target),
    )


def _content_blocks(
    raw: Mapping[str, object],
    target: BackendTarget,
) -> list[Mapping[str, object]]:
    output = raw.get("output")
    message = output.get("message") if isinstance(output, Mapping) else None
    if not isinstance(message, Mapping):
        raise TransportError(
            f"{_SERVICE_NAME}/{target.region}: Converse body carried no `output.message` object",
            trigger="SERVER_ERROR",
        )
    content = message.get("content")
    if not isinstance(content, Sequence) or isinstance(content, str | bytes):
        return []
    return [block for block in content if isinstance(block, Mapping)]


def _tool_arguments(blocks: Sequence[Mapping[str, object]]) -> dict[str, object] | None:
    """The TOOL_CALL rung's answer, as an object.

    Arguments that are absent or not an object come back as `None` rather than as an exception:
    `client.py`'s `_validate` turns exactly that into a repairable `MalformedReply`, and spending
    one repair on a mangled arguments object is a strictly better outcome than failing the target
    over (§7.7). `Converse` decodes `toolUse.input` for us, so unlike the chat-completions shape
    there is no JSON string to re-parse here.
    """
    for block in blocks:
        use = block.get("toolUse")
        if not isinstance(use, Mapping) or use.get("name") != _TOOL_NAME:
            continue
        arguments = use.get("input")
        if isinstance(arguments, Mapping):
            return {str(key): value for key, value in arguments.items()}
        return None
    return None


def _finish_reason(raw: Mapping[str, object], target: BackendTarget) -> FinishReason:
    """`stopReason` → one of the five members, or a loud failure.

    An unmapped `stopReason` is raised, never guessed. `Converse`'s `stopReason` is a closed enum,
    so a value outside `_FINISH_REASONS` means the response is not the shape this adapter was
    written against — and guessing `stop` there would hand `client.py` a reply it would validate,
    or guessing `length` would have it re-ask an identical oversized request until the truncation
    budget was spent. `MalformedReply` rather than `TransportError` because the next target would
    reproduce it exactly (Rule 11).
    """
    reported = raw.get("stopReason")
    mapped = _FINISH_REASONS.get(reported) if isinstance(reported, str) else None
    if mapped is None:
        raise UnmappedFinishReason(
            f"{target.backend}:{target.model_id} returned stopReason {reported!r}, which this "
            f"adapter does not map to a FinishReason",
        )
    return mapped


def _usage(raw: Mapping[str, object], target: BackendTarget) -> TokenUsage:
    """Counts as Bedrock reported them, under the CONFIG's `model_id`.

    **`model_id` echoes `target.model_id` VERBATIM.** It is the single highest-consequence line in
    this file. The language of "resolution" that surrounds this field — in its declaration on
    `TokenUsage`, in the `llm_cache.model_id` column of `state/schema.sql`, and in `_stamp` itself
    — reads as licence to report whatever name Bedrock answered under, and three lanes
    independently followed it into the same bug. Those two comments now spell the invariant out,
    but no prose enforces it. The mechanism below does, so check it against the code:

    `_stamp` resolves `usage.model_id or target.model_id` — backend-reported WINS. The `llm_cache`
    READ key is built from the config string (`_key_parts`, from `route.targets[0].model_id`) while
    the WRITE key is built from `usage.model_id` (`_store_response`). Report anything else — the
    inference-profile ARN Bedrock echoes back, a cross-region `us.`-prefixed id, a dated snapshot —
    and read key != write key on EVERY call: a permanent, total cache miss across the whole fleet,
    silent and indistinguishable from a cold cache because `attempts.llm_cache_hit` simply stays 0
    (§11.6). Nothing detects it; the bill is the only symptom.

    It also breaks failover attribution: `cache._target_for` matches `(backend, model_id)` against
    the route's configured targets to recover the answering target's `effort`, itself a key
    component, and a resolved id matches nothing.

    The resolved id is genuine provenance and is genuinely lost here. Recovering it needs its own
    field and a schema migration; it must not be smuggled through `model_id`.

    `cost_usd` is likewise NOT set: pricing is the target's declaration and `_stamp` applies it, so
    a $0.00 call comes from `price: free` and never from a backend guessing (§11.2, §9 rule 5).
    """
    usage = raw.get("usage")
    usage_map: Mapping[str, object] = usage if isinstance(usage, Mapping) else {}
    return TokenUsage(
        backend=BedrockBackend.name,
        model_id=target.model_id,
        input_tokens=_count(usage_map.get("inputTokens")),
        output_tokens=_count(usage_map.get("outputTokens")),
        cache_read_tokens=_count(usage_map.get("cacheReadInputTokens")),
    )


def _count(value: object) -> int:
    """Missing, null or negative all read as 0. `TokenUsage` bounds these `ge=0`, and a response
    that omits a counter must not turn into a `ValidationError` on an otherwise good reply:
    under-reporting is visible in the ledger, while a crash loses the answer."""
    return max(value, 0) if isinstance(value, int) and not isinstance(value, bool) else 0


def _protocol_conformance(backend: BedrockBackend) -> ModelBackend:
    """Compile-time only: `mypy --strict` fails here if this adapter drifts from `ModelBackend`."""
    return backend
