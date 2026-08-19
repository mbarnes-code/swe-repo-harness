"""ADR-0023 `vertex` — Google Vertex AI as a `ModelBackend` (SPEC §7.7, §9, §13 row 36).

SPEC's shipped-backends table gives this transport the same one job as `bedrock`: *"same models,
different transport"*, existing "for failover, not for variety" — a tier lists `anthropic` then
`vertex` and survives one endpoint's outage without changing which model answers. SPEC §9's shipped
`default` profile routes WORKHORSE to exactly that pair, both naming the same model id.

Four properties this module exists to keep true:

1. **The SDK is quarantined.** `_AuthorizedSessionTransport` is the only object here that touches
   `google.auth`; everything above it moves plain mappings, which is what lets the request builder
   and the reply parser be exercised without a socket and without credentials. The imports are at
   MODULE scope and unguarded on purpose: `client.discover()`'s contract is that "a backend whose
   SDK is not installed fails its import here and is simply not registered", so a `try`/`except
   ImportError` that registered a half-working adapter would convert a clean absence into a runtime
   failure on the first dispatch of a long run.
2. **`region` is mandatory, and it is the ONLY thing config supplies.** §9 loader rule 2 says
   `bedrock`/`vertex` refuse a target with no `region`. The GCP project is deliberately not a
   `BackendTarget` field and must not become one: Application Default Credentials already resolve a
   project alongside the credentials (`google.auth.default()` returns both), which is precisely why
   SPEC §9's shipped `vertex` target carries `region` and nothing else — no `api_key_env`, no
   project. §9 rule 4 forbids secret material in `config/models.yaml` and a service-account JSON is
   exactly that (`redaction.patterns` even ships a `gcp_sa_key` pattern for it, §11.4).
3. **It decides nothing.** No validation, no schema retry, no mode selection, no acting on
   `finish_reason` — those belong to `llm/client.py`, so every backend behaves identically at the
   boundary that matters (ADR-0002, `ModelBackend.invoke`).
4. **No model id and no endpoint host is written as config here.** The host is derived from the
   target's `region`; the model id comes off the `BackendTarget` the router resolved (§9, §12.40).

**Why `google.auth` and the `rawPredict` surface rather than the Vertex AI SDK.** `pyproject.toml`
declares the extra as `google-cloud-aiplatform`, and that package's Python surface
(`aiplatform` / `vertexai`) covers Google's own model families; Anthropic publisher models on Vertex
are served through the `:rawPredict` endpoint, whose request body is the Messages API body plus an
`anthropic_version` discriminator. Since the whole point of this backend is "the same model as
`anthropic`, reached another way", that endpoint is the only one that satisfies the table row.
`google-auth` is a hard dependency of `google-cloud-aiplatform`, so the declared extra installs
everything imported below and no new dependency is added — but the mismatch between the extra's NAME
and the module actually imported is real, and is flagged rather than papered over (Rule 7).
*Agent Recommendation*: if the extra is ever narrowed, `google-auth` is the honest name for it.

`requests` is imported for its exception base ONLY, and needs no separate declaration: it is a hard
dependency of `google.auth.transport.requests`, so it is present exactly when the import above
succeeds and absent exactly when that import raises `ImportError` — which is the behaviour
`discover()` already relies on. Naming the real base is what keeps the transport catch from
swallowing a bug in this adapter as a failover trigger.

`AuthorizedSession` is synchronous, so every call crosses into a worker thread via
`asyncio.to_thread`; it is used rather than a hand-rolled bearer header because it owns token
refresh, and an access token that silently expires mid-run is a 401 forty minutes in.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar, Final, Protocol, cast

import google.auth
from google.auth.transport.requests import AuthorizedSession
from requests.exceptions import RequestException

from fleet.llm.client import (
    BackendReply,
    FinishReason,
    LlmError,
    Message,
    ModelBackend,
    TransportError,
    register_backend,
)
from fleet.models.enums import StructuredOutputMode
from fleet.models.tasks import BackendTarget, ModelCapabilities, TokenUsage

#: The scope Vertex prediction requires. One entry, because a token minted with more than it needs
#: is a credential that does more than this adapter does.
_SCOPES: Final[tuple[str, ...]] = ("https://www.googleapis.com/auth/cloud-platform",)

#: Vertex's discriminator for an Anthropic publisher-model body. It is a wire constant, not a model
#: id and not a version of anything the harness ships.
_ANTHROPIC_VERSION: Final[str] = "vertex-2023-10-16"

#: The publisher whose models this transport reaches. Same models as the `anthropic` backend — that
#: is the entire reason this file exists (§7.7 shipped-backends table).
_PUBLISHER: Final[str] = "anthropic"

#: The multi-region alias, whose host has no region prefix. Named rather than inlined so the one
#: special case in URL construction is greppable.
_GLOBAL_REGION: Final[str] = "global"

#: The single tool the TOOL_CALL rung offers. One name, because the rung's whole job is to make the
#: model emit ONE arguments object matching the response schema.
_TOOL_NAME: Final[str] = "emit_response"
_TOOL_DESCRIPTION: Final[str] = (
    "Emit the response. Call this tool exactly once, with arguments matching its schema. "
    "It is the only way to answer."
)

_MAX_CONTEXT: Final[int] = 200_000
_MAX_OUTPUT_TOKENS: Final[int] = 64_000

#: The HTTP class boundary between "the endpoint is sick" (§11.8 `SERVER_ERROR`, failover) and
#: "our request is wrong" (fails the task).
_SERVER_ERROR_STATUS: Final[int] = 500
_RATE_LIMIT_STATUS: Final[int] = 429
_OK_STATUS: Final[int] = 200

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

`supports_json_schema=False` is a deliberate under-declaration and matches the `anthropic` backend
target-for-target, which is the point: a failover pair whose two halves declare different rungs
would negotiate differently, and the operator's "same model, different transport" would quietly
become "same model, different structured-output contract" the moment the primary went down. The
underlying reason is the same one the native transport has — the schema mode constrains which JSON
Schema it accepts (every object needs `additionalProperties: false`; `minimum`/`maxLength` and
friends are unsupported), while `response_model.model_json_schema()` is Pydantic output that emits
neither restriction. Declaring the rung would make any `Field(ge=…)` response model a hard 4xx on
the first call instead of a negotiated fallback. TOOL_CALL submits the identical schema as a
mandatory tool's parameter object with no such restriction, and §7.7's rule that "validation is
ALWAYS Pydantic on our side, at every rung" means the rung we do not claim costs a hit-rate hint and
never a guarantee.

`max_context` / `max_output_tokens` are the FLOOR across the model family a `vertex` target may
name, not the ceiling of the largest one: `ModelCapabilities` is declared per BACKEND, so one pair
of numbers has to hold for every target routed through this transport, and declaring the smaller
means the cap `client.py` computes (`min(requested, caps.max_output_tokens)`) is always a request
the transport will accept. A profile routing only to a larger-window target raises both with
`capabilities_override` — the seam §13 row 36 provides.

Both halves of the declaration agree, so `promised_mode()` and `negotiate()` return the same rung
and a healthy target is never libelled with `CapabilityDrift` (§13 row 37). *Agent
Recommendation.*"""

_FINISH_REASONS: Final[Mapping[str, FinishReason]] = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "model_context_window_exceeded": "length",
    "tool_use": "tool_call",
    "refusal": "refusal",
}
"""Exhaustive over the publisher's `stop_reason`, with two decisions worth stating.

`max_tokens` and `model_context_window_exceeded` map to `length` and to NOTHING else. §13 row 47
turns on `length` being distinguishable from a schema failure: a truncated reply is retried on the
SAME target with a raised cap and spends no repair and no failover, while a schema failure spends
the repair budget and then fails the target over. Collapsing either into a parse error would fail
one oversized call across three tiers to reproduce a single truncation.

`pause_turn` is absent on purpose — it means a server-side tool loop paused mid-turn, and this
adapter declares no server-side tools, so seeing it means the request was not the one we built and
`_finish_reason` raises rather than inventing a rung-visible reason for it. `filtered` never appears
on the right-hand side for the same honesty reason: this publisher reports a policy decline as
`refusal`, and `client.py` already treats the two identically (`ModelRefused`)."""


# ---------------------------------------------------------------------------------------------
# Errors — typed, never bools (Rule 11). A misconfigured target and a sick endpoint have entirely
# different remedies (edit the profile vs. wait), so a caller must not have to parse a message.
# ---------------------------------------------------------------------------------------------


class VertexTargetMisconfigured(LlmError):
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
            f"vertex target {target.backend}:{target.model_id} {detail} (field `{field}`)",
        )
        self.target = target
        self.field = field


class MissingRegion(VertexTargetMisconfigured):
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
            "declares no region: a Vertex publisher-model endpoint is addressed per location and "
            "this transport has no vendor default to fall back on",
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


class RawPredictTransport(Protocol):
    """One `:rawPredict` round trip, as a mapping in and a mapping out.

    The seam exists so the request builder and the reply parser — where every behaviour this module
    actually owns lives — are exercised without a socket and without ADC. An implementation raises
    `TransportError` for connection, rate-limit and server failures and returns the decoded response
    body for everything else.
    """

    async def __call__(
        self,
        *,
        region: str,
        model_id: str,
        body: Mapping[str, object],
        timeout_s: float,
    ) -> Mapping[str, object]: ...


def endpoint_url(region: str, project: str, model_id: str) -> str:
    """The `:rawPredict` URL for one publisher model in one location.

    A module function rather than an f-string inside the transport so the one piece of vendor URL
    shape in this file is testable without ADC. `global` is the documented multi-region alias and
    is the single host that carries no location prefix.
    """
    host = (
        "aiplatform.googleapis.com"
        if region == _GLOBAL_REGION
        else f"{region}-aiplatform.googleapis.com"
    )
    return (
        f"https://{host}/v1/projects/{project}/locations/{region}"
        f"/publishers/{_PUBLISHER}/models/{model_id}:rawPredict"
    )


class _AuthorizedSessionTransport:
    """The ONLY object in the harness that imports `google.auth`. Everything it hands back is a
    plain mapping, so no SDK type escapes this class.

    Both constructor arguments default, so `VertexBackend()` — and therefore `register_backend`'s
    `cls()` — still builds this with no arguments, no session and no credential lookup. Credentials
    and the project are resolved ONCE and then cached on the instance: `google.auth.default()` reads
    the filesystem or the metadata server, and doing that per call would add a syscall storm to a
    250-repo run. `AuthorizedSession` refreshes the access token itself, so the cache is of the
    credential object, never of a token.
    """

    def __init__(self, session: object | None = None, project: str | None = None) -> None:
        self._session = session
        self._project = project

    def _resolve(self) -> tuple[Any, str]:
        """ADC → (session, project). `google.auth.default()` returns the project alongside the
        credentials, which is why `BackendTarget` needs no project field (see the module
        docstring). A credential set with no associated project cannot address an endpoint, so it
        is a loud failure rather than a URL with an empty path segment."""
        if self._session is None:
            credentials, project = google.auth.default(scopes=list(_SCOPES))
            if not project:
                raise LlmError(
                    "vertex: Application Default Credentials resolved no GCP project; set "
                    "GOOGLE_CLOUD_PROJECT or use credentials that carry one (§9 rule 4 forbids "
                    "putting it in config/models.yaml)",
                )
            self._session = AuthorizedSession(credentials)
            self._project = project
        return self._session, self._project or ""

    async def __call__(
        self,
        *,
        region: str,
        model_id: str,
        body: Mapping[str, object],
        timeout_s: float,
    ) -> Mapping[str, object]:
        return await asyncio.to_thread(self._post, region, model_id, dict(body), timeout_s)

    def _post(
        self,
        region: str,
        model_id: str,
        body: dict[str, object],
        timeout_s: float,
    ) -> Mapping[str, object]:
        """The blocking half, run in a worker thread."""
        session, project = self._resolve()
        url = endpoint_url(region, project, model_id)
        try:
            response: Any = session.post(url, json=body, timeout=timeout_s)
        except RequestException as exc:
            # `RequestException` is the common base of the transport family `requests` raises —
            # connection refused, read timeout, SSL, chunked-encoding. All CONNECTION-class: the
            # next target may be a different region or a different transport entirely, so §11.8
            # gets its chance (trigger 1).
            #
            # Deliberately NOT `except Exception`. A bare catch here would relabel a bug in this
            # adapter — a `TypeError` in the body we just built, an `AttributeError` on a renamed
            # SDK member — as a transport fault, and §11.8 would then walk the whole tier
            # reproducing it before reporting an outage that never happened. A programming error
            # must surface as itself (Rule 11).
            raise TransportError(
                f"vertex/{region}: {type(exc).__name__}: {exc}",
                trigger="CONNECTION",
            ) from exc
        status = int(getattr(response, "status_code", 0))
        if status != _OK_STATUS:
            raise _from_status(region, status, str(getattr(response, "text", "")))
        decoded: object = response.json()
        if not isinstance(decoded, Mapping):
            raise TransportError(
                f"vertex/{region}: rawPredict returned {type(decoded).__name__}, not an object",
                trigger="SERVER_ERROR",
            )
        return cast(Mapping[str, object], decoded)


def _from_status(region: str, status: int, detail: str) -> LlmError:
    """A non-200 → either a §11.8 failover trigger or a loud task failure.

    429 is its own trigger and is never a `SERVER_ERROR`: §13 row 43's rule that a rate limit alone
    can never mean "the endpoint is down" only holds if the two arrive under different names. 5xx is
    the endpoint being sick, which the next target may not be. Everything else in the 4xx range is
    OUR request being wrong — the next target reproduces it exactly, so it fails the task loudly
    instead of spending the tier's ladder (Rule 11).
    """
    if status == _RATE_LIMIT_STATUS:
        return TransportError(f"vertex/{region}: HTTP {status}: {detail}", trigger="RATE_LIMIT")
    if status >= _SERVER_ERROR_STATUS:
        return TransportError(f"vertex/{region}: HTTP {status}: {detail}", trigger="SERVER_ERROR")
    return LlmError(f"vertex/{region} rejected the request with HTTP {status}: {detail}")


# ---------------------------------------------------------------------------------------------
# The backend
# ---------------------------------------------------------------------------------------------


@register_backend
class VertexBackend:
    """`ModelBackend` for Anthropic publisher models on Vertex AI. Stateless per call: the region
    comes off the TARGET, because two targets in one tier may be two different locations."""

    name: ClassVar[str] = "vertex"
    version: ClassVar[int] = 1

    def __init__(self, transport: RawPredictTransport | None = None) -> None:
        """`register_backend` constructs this with no arguments (`cls()`), so the collaborator
        defaults — but it is injectable, which is how a test drives the adapter with no socket and
        no credentials (CLAUDE.md guardrail 3)."""
        self._transport: RawPredictTransport = (
            _AuthorizedSessionTransport() if transport is None else transport
        )

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
        body = build_body(target, messages, schema, mode, max_output_tokens)
        raw = await self._transport(
            region=region,
            model_id=target.model_id,
            body=body,
            timeout_s=timeout_s,
        )
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


def build_body(
    target: BackendTarget,
    messages: Sequence[Message],
    schema: dict[str, object] | None,
    mode: StructuredOutputMode,
    max_output_tokens: int,
) -> dict[str, object]:
    """The `:rawPredict` body for one negotiated rung.

    `model` is absent by design — on this transport the model is in the URL path, and
    `anthropic_version` is the discriminator that replaces it in the body.

    PROMPTED adds no structuring field at all: `client.py` has already rendered the schema into the
    prompt and passes `schema=None` (`_call_target`), which is what makes the floor reachable.
    """
    system, turns = _render(target, messages)
    body: dict[str, object] = {
        "anthropic_version": _ANTHROPIC_VERSION,
        "messages": turns,
        "max_tokens": max_output_tokens,
    }
    if system:
        body["system"] = system

    # `effort` is OMITTED when it is `None`. `None` means "the operator wrote no effort for this
    # target", and transmitting a value nobody wrote is exactly the silent degradation Rule 11
    # forbids — the more so here, because `effort` is already a component of the `llm_cache` key
    # (`CacheKeyParts.effort`), so a cached row would claim the call was made at an effort the
    # profile never declared. Written against `str | None` on purpose: `BackendTarget.effort` is
    # still a non-optional `Literal` with a `"medium"` default at this commit, and a sibling lane is
    # making it optional. The narrowing below is correct under both.
    effort: str | None = target.effort
    if effort is not None:
        body["output_config"] = {"effort": effort}

    if schema is None:
        return body
    if mode is StructuredOutputMode.TOOL_CALL:
        body["tools"] = [
            {"name": _TOOL_NAME, "description": _TOOL_DESCRIPTION, "input_schema": schema},
        ]
        body["tool_choice"] = {"type": "tool", "name": _TOOL_NAME}
        return body
    # JSON_SCHEMA and CONSTRAINED are both un-declared by `_DECLARED`, so `negotiate()` never picks
    # them unless a `capabilities_override` claimed them. A loud refusal naming the rung beats a
    # silently unstructured call whose reply then burns the whole repair budget (Rule 11).
    raise VertexTargetMisconfigured(
        target,
        "capabilities_override",
        f"was dispatched at the {mode} rung, which this transport does not offer; its only "
        f"structured surface is the mandatory-tool (TOOL_CALL) rung",
    )


def _render(target: BackendTarget, messages: Sequence[Message]) -> tuple[str, list[dict[str, str]]]:
    """Neutral `Message`s → one system string plus the turn list.

    `tool` turns are rendered as `assistant`. That member exists because the TOOL_CALL rung is a
    two-turn protocol and the reply's ARGUMENTS have to travel back in as a turn; on this transport
    the arguments were produced by an assistant turn, so sending them back as one shows the model
    what it actually emitted. Consecutive same-role turns are combined by the API, so no
    interleaving filler is invented.
    """
    system = "\n\n".join(m.content for m in messages if m.role == "system")
    turns = [
        {"role": "user" if m.role == "user" else "assistant", "content": m.content}
        for m in messages
        if m.role != "system"
    ]
    if not turns:
        raise VertexTargetMisconfigured(
            target,
            "messages",
            "received no non-system turns; this transport needs at least one",
        )
    if turns[0]["role"] != "user":
        raise VertexTargetMisconfigured(
            target,
            "messages",
            f"received a conversation whose first non-system turn is {turns[0]['role']!r}; "
            f"this transport requires 'user'",
        )
    return system, turns


def parse_reply(raw: Mapping[str, object], target: BackendTarget) -> BackendReply:
    """Decoded `:rawPredict` body → `BackendReply`. Reports, decides nothing."""
    blocks = _content_blocks(raw)
    text = "".join(
        str(block["text"])
        for block in blocks
        if block.get("type") == "text" and isinstance(block.get("text"), str)
    )
    return BackendReply(
        text=text or None,
        tool_arguments=_tool_arguments(blocks),
        usage=_usage(raw, target),
        finish_reason=_finish_reason(raw, target),
    )


def _content_blocks(raw: Mapping[str, object]) -> list[Mapping[str, object]]:
    content = raw.get("content")
    if not isinstance(content, Sequence) or isinstance(content, str | bytes):
        return []
    return [block for block in content if isinstance(block, Mapping)]


def _tool_arguments(blocks: Sequence[Mapping[str, object]]) -> dict[str, object] | None:
    """The TOOL_CALL rung's answer, as an object.

    Arguments that are absent or not an object come back as `None` rather than as an exception:
    `client.py`'s `_validate` turns exactly that into a repairable `MalformedReply`, and spending
    one repair on a mangled arguments object is a strictly better outcome than failing the target
    over (§7.7).
    """
    for block in blocks:
        if block.get("type") != "tool_use" or block.get("name") != _TOOL_NAME:
            continue
        arguments = block.get("input")
        if isinstance(arguments, Mapping):
            return {str(key): value for key, value in arguments.items()}
        return None
    return None


def _finish_reason(raw: Mapping[str, object], target: BackendTarget) -> FinishReason:
    """`stop_reason` → one of the five members, or a loud failure.

    An unmapped `stop_reason` is raised, never guessed: guessing `stop` would hand `client.py` a
    reply it would then validate, and guessing `length` would have it re-ask an identical oversized
    request until the truncation budget was spent. `MalformedReply` rather than `TransportError`
    because the next target would reproduce it exactly (Rule 11).
    """
    reported = raw.get("stop_reason")
    mapped = _FINISH_REASONS.get(reported) if isinstance(reported, str) else None
    if mapped is None:
        raise UnmappedFinishReason(
            f"{target.backend}:{target.model_id} returned stop_reason {reported!r}, which this "
            f"adapter does not map to a FinishReason",
        )
    return mapped


def _usage(raw: Mapping[str, object], target: BackendTarget) -> TokenUsage:
    """Counts as the endpoint reported them, under the CONFIG's `model_id`.

    **`model_id` echoes `target.model_id` VERBATIM, and the body's own `model` field — which this
    transport returns as a dated snapshot id — is deliberately discarded.** It is the single
    highest-consequence line in this file, and both `TokenUsage.model_id`'s own comment ("the
    RESOLVED model id, as the backend reported it") and `state/schema.sql`'s ("RESOLVED id")
    actively invite the other choice. Here is why they must not be followed:

    `_stamp` resolves `usage.model_id or target.model_id` — backend-reported WINS. The `llm_cache`
    READ key is built from the config string (`_key_parts`, from `route.targets[0].model_id`) while
    the WRITE key is built from `usage.model_id` (`_store_response`). Report the snapshot id and
    read key != write key on EVERY call: a permanent, total cache miss across the whole fleet,
    silent and indistinguishable from a cold cache because `attempts.llm_cache_hit` simply stays 0
    (§11.6). Nothing detects it; the bill is the only symptom.

    It also breaks failover attribution: `cache._target_for` matches `(backend, model_id)` against
    the route's configured targets to recover the answering target's `effort`, itself a key
    component, and a snapshot id matches nothing.

    The snapshot id is genuine provenance and is genuinely lost here. Recovering it needs its own
    field and a schema migration; it must not be smuggled through `model_id`.

    `cost_usd` is likewise NOT set: pricing is the target's declaration and `_stamp` applies it, so
    a $0.00 call comes from `price: free` and never from a backend guessing (§11.2, §9 rule 5).
    """
    usage = raw.get("usage")
    usage_map: Mapping[str, object] = usage if isinstance(usage, Mapping) else {}
    return TokenUsage(
        backend=VertexBackend.name,
        model_id=target.model_id,
        input_tokens=_count(usage_map.get("input_tokens")),
        output_tokens=_count(usage_map.get("output_tokens")),
        cache_read_tokens=_count(usage_map.get("cache_read_input_tokens")),
    )


def _count(value: object) -> int:
    """Missing, null or negative all read as 0. `TokenUsage` bounds these `ge=0`, and a response
    that omits a counter must not turn into a `ValidationError` on an otherwise good reply:
    under-reporting is visible in the ledger, while a crash loses the answer."""
    return max(value, 0) if isinstance(value, int) and not isinstance(value, bool) else 0


def _protocol_conformance(backend: VertexBackend) -> ModelBackend:
    """Compile-time only: `mypy --strict` fails here if this adapter drifts from `ModelBackend`."""
    return backend
