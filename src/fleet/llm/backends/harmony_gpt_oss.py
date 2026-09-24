"""ADR-0145 `harmony_gpt_oss` — GPT-OSS-120b over Harmony + vLLM's token-id completions endpoint
(SPEC §7.7, `config/models.yaml`'s `pilot` profile).

GPT-OSS models are trained on OpenAI's Harmony envelope, not on chat-completions JSON
(`references/harmony/docs/format.md:1-4`), and are RL-trained to emit code edits via a single
`apply_patch` function tool whose payload is OpenAI's own patch-language text, not a unified diff
(`references/gpt-oss/gpt_oss/tools/apply_patch.md`). This module:

1. Renders Fleet's neutral `Message` sequence into a Harmony `Conversation` and encodes it to
   token ids via the `openai-harmony` SDK (Task 4).
2. Posts those token ids to vLLM's legacy completions endpoint (NOT chat-completions) via the
   `openai` SDK's `client.completions` resource — already a core dependency
   (`pyproject.toml:34`) — and gets token ids back (Task 6).
3. Parses the returned tokens back into Harmony `Message`s and, when the negotiated schema has a
   `[{path, diff}]`-shaped property, decodes an `apply_patch` tool call into that property via
   `fleet.vcs.apply_patch` (Task 5); otherwise falls back to the ordinary generic `emit_response`
   tool call every other TOOL_CALL backend already uses.

Three properties this module exists to keep true, matching `openai_compatible.py`'s own three
(module docstring, `src/fleet/llm/backends/openai_compatible.py:9-33`):

1. **The SDK is quarantined.** The `openai_harmony` import is at MODULE scope, unguarded — see
   this repo's Global Constraints on SDK quarantine. Everything above the transport moves plain
   Python values.
2. **The declared capability floor is fact-checked, not assumed.** See `declared_capabilities`'s
   own docstring for why this backend declares `TOOL_CALL` unconditionally where
   `openai_compatible.py` cannot.
3. **It decides nothing.** No validation, no schema retry, no mode selection — an ambiguous or
   missing model reply comes back as `tool_arguments=None`, never a raised exception, exactly like
   `openai_compatible.py::_tool_arguments`'s own "spending a repair on a mangled arguments string
   is a strictly better outcome than failing the target over" (§7.7).

`git apply` remains the harness's ONLY worktree writer (`src/fleet/vcs/git.py:435`) and
`FilePatch.diff` remains the ONLY patch representation (`src/fleet/models/tasks.py:204`)
everywhere outside this file: the `apply_patch`-format text this module decodes never crosses out
of `invoke()`.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import ClassVar, Final, Protocol, cast

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI, RateLimitError
from openai_harmony import (
    Author,
    Conversation,
    DeveloperContent,
    HarmonyEncodingName,
    ReasoningEffort,
    Role,
    SystemContent,
    ToolDescription,
    load_harmony_encoding,
)
from openai_harmony import (
    Message as HarmonyMessage,
)

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
from fleet.models.tasks import (
    BackendTarget,
    ModelCapabilities,
    TokenUsage,
)
from fleet.vcs.apply_patch import (
    DiffError,
    commit_to_file_edits,
    identify_files_needed,
    patch_to_commit,
    text_to_patch,
)

_MAX_CONTEXT: Final[int] = ModelCapabilities.model_fields["max_context"].default
_MAX_OUTPUT_TOKENS: Final[int] = ModelCapabilities.model_fields["max_output_tokens"].default

_DECLARED: Final[ModelCapabilities] = ModelCapabilities(
    supports_tools=True,
    supports_json_schema=False,
    supports_system_prompt=True,
    supports_streaming=False,
    supports_constrained_decoding=False,
    max_context=_MAX_CONTEXT,
    max_output_tokens=_MAX_OUTPUT_TOKENS,
    structured_output_modes=(StructuredOutputMode.TOOL_CALL, StructuredOutputMode.PROMPTED),
)
"""See `declared_capabilities`'s docstring for the fact-checked rationale (Agent Recommendation,
ADR-0145). `max_context`/`max_output_tokens` are the `ModelCapabilities` defaults, genuinely
unverified numbers pending a real Spark endpoint — raised per-target via `capabilities_override`
once measured, matching `config/models.yaml`'s `local` profile precedent."""


_TOOL_NAME: Final[str] = "emit_response"
"""The ordinary generic tool name for a non-diff-shaped schema — matches
`openai_compatible.py:72` and `vertex.py:92` exactly, so a reader who knows either backend
recognises this one immediately."""

_APPLY_PATCH_TOOL_NAME: Final[str] = "apply_patch"

_REASONING_EFFORT: Final[Mapping[str, ReasoningEffort]] = {
    "low": ReasoningEffort.LOW,
    "medium": ReasoningEffort.MEDIUM,
    "high": ReasoningEffort.HIGH,
}

# Vendored VERBATIM from references/gpt-oss/gpt_oss/tools/apply_patch.md (read-only reference;
# this is our own copy under src/, per CLAUDE.md guardrail "never reference references/ at
# runtime" — see this module's Global Constraints entry). Keep in sync by hand if that file's
# prose ever changes upstream; nothing here auto-syncs it.
_APPLY_PATCH_INSTRUCTIONS: Final[str] = """When requested to perform coding-related tasks, you \
MUST adhere to the following criteria when executing the task:

- Use `apply_patch` to edit files.
- If completing the user's task requires writing or modifying files:
  - Your code and final answer should follow these _CODING GUIDELINES_:
    - Avoid unneeded complexity in your solution. Minimize program size.
    - Keep changes consistent with the style of the existing codebase. Changes should be \
minimal and focused on the task.
    - NEVER add copyright or license headers unless specifically requested.
- Never implement function stubs. Provide complete working implementations.

§ `apply_patch` Specification

Your patch language is a stripped-down, file-oriented diff format designed to be easy to \
parse and safe to apply. You can think of it as a high-level envelope:

*** Begin Patch
[ one or more file sections ]
*** End Patch

Within that envelope, you get a sequence of file operations.
You MUST include a header to specify the action you are taking.
Each operation starts with one of three headers:

*** Add File: <path> - create a new file. Every following line is a + line (the initial contents).
*** Delete File: <path> - remove an existing file. Nothing follows.
*** Update File: <path> - patch an existing file in place (optionally with a rename).

May be immediately followed by *** Move to: <new path> if you want to rename the file.
Then one or more “hunks”, each introduced by @@ (optionally followed by a hunk header).
Within a hunk each line starts with:

- for inserted text,
* for removed text, or
  space ( ) for context.
  At the end of a truncated hunk you can emit *** End of File.

A full patch can combine several operations:

*** Begin Patch
*** Add File: hello.txt
+Hello world
*** Update File: src/app.py
*** Move to: src/main.py
@@ def greet():
-print("Hi")
+print("Hello, world!")
*** Delete File: obsolete.txt
*** End Patch

It is important to remember:

- You must include a header with your intended action (Add/Delete/Update)
- You must prefix new lines with `+` even when creating a new file
"""

_APPLY_PATCH_TOOL: Final[ToolDescription] = ToolDescription.new(
    _APPLY_PATCH_TOOL_NAME,
    "Patch a file",
    parameters={
        "type": "string",
        "description": "Formatted patch code",
        "default": "*** Begin Patch\n*** End Patch\n",
    },
)
"""Mirrors `references/gpt-oss/gpt_oss/chat.py:114-124` exactly: `apply_patch` takes ONE raw
string argument (the patch text), not an object with a `patch` property — this is a fact about
how GPT-OSS was trained to call this specific tool, not a Fleet convention."""


_LOCAL_DEFS_PREFIX: Final[str] = "#/$defs/"


def _resolve_refs(schema: Mapping[str, object]) -> dict[str, object]:
    """`schema` with every local `{"$ref": "#/$defs/X"}` replaced by a copy of `$defs.X` (nested
    refs included), and the top-level `$defs` dropped once nothing points into it.

    Pydantic v2's `model_json_schema()` emits a nested model as a `$ref` into `$defs` — which is
    how every diff-bearing Fleet schema arrives here (`files.items` is
    `{"$ref": "#/$defs/ProposedFileEdit"}`). Neither this module's structural detection nor
    Harmony's tool-parameter TypeScript renderer follows a `$ref` (the renderer shows the field as
    `any`), so both must see the inlined shape. Sibling keys next to a `$ref` (Pydantic puts a
    field's `description` there) are merged over the resolved copy. A ref that is not a local
    `$defs` pointer, names a missing entry, or is recursive (already being expanded on the current
    path) is left in place untouched: this backend reports, it never raises on a schema's shape."""
    defs_value = schema.get("$defs")
    defs = defs_value if isinstance(defs_value, Mapping) else {}
    unresolved: list[str] = []

    def walk(node: object, expanding: frozenset[str]) -> object:
        if isinstance(node, list):
            return [walk(item, expanding) for item in node]
        if not isinstance(node, Mapping):
            return node
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith(_LOCAL_DEFS_PREFIX):
            name = ref[len(_LOCAL_DEFS_PREFIX) :]
            target = defs.get(name)
            if isinstance(target, Mapping) and name not in expanding:
                # walk() maps a Mapping to a dict, always.
                resolved = cast(dict[str, object], walk(target, expanding | {name}))
                siblings = {k: walk(v, expanding) for k, v in node.items() if k != "$ref"}
                return {**resolved, **siblings}
            unresolved.append(ref)
        return {k: walk(v, expanding) for k, v in node.items()}

    top = {k: v for k, v in schema.items() if k != "$defs"}
    resolved_top = cast(dict[str, object], walk(top, frozenset()))
    if unresolved and defs:
        # A recursive local ref survived: keep `$defs` so it still points somewhere.
        resolved_top["$defs"] = dict(defs)
    return resolved_top


def _file_edits_property(schema: Mapping[str, object]) -> str | None:
    """The name of the one top-level property shaped like `[{path: string, diff: string, ...}]`
    — the `ProposedFileEdit` shape every diff-bearing response schema uses
    (`src/fleet/llm/schemas.py:176-200`, `215-225`, `227-235`) — or `None` for an ordinary
    schema. Structural detection, not a hard-coded property name: this module never imports
    `fleet.llm.schemas` (no backend does; a backend only ever sees the resolved JSON Schema).
    Refs are resolved first — the real schemas carry `items` as a `$ref`, see `_resolve_refs`."""
    properties = _resolve_refs(schema).get("properties")
    if not isinstance(properties, Mapping):
        return None
    for name, prop in properties.items():
        if not isinstance(prop, Mapping) or prop.get("type") != "array":
            continue
        items = prop.get("items")
        if not isinstance(items, Mapping):
            continue
        item_properties = items.get("properties")
        if not isinstance(item_properties, Mapping):
            continue
        path_prop = item_properties.get("path")
        diff_prop = item_properties.get("diff")
        if (
            isinstance(path_prop, Mapping)
            and path_prop.get("type") == "string"
            and isinstance(diff_prop, Mapping)
            and diff_prop.get("type") == "string"
        ):
            return str(name)
    return None


def _remaining_schema(schema: Mapping[str, object], file_edits_property: str) -> dict[str, object]:
    """`schema` with `file_edits_property` removed from both `properties` and `required` — the
    part of the response the model must still answer in plain JSON on the `final` channel,
    because `apply_patch` supplies the file-edits property instead (Task 5 merges the two)."""
    properties_value = schema.get("properties")
    properties = dict(properties_value) if isinstance(properties_value, Mapping) else {}
    properties.pop(file_edits_property, None)
    required_value = schema.get("required")
    required = (
        [r for r in required_value if r != file_edits_property]
        if isinstance(required_value, list)
        else []
    )
    remaining = {k: v for k, v in schema.items() if k not in ("properties", "required")}
    remaining["properties"] = properties
    remaining["required"] = required
    return remaining


def _response_format_block(remaining_schema: Mapping[str, object]) -> str:
    """`# Response Formats` developer-message block, per
    `references/harmony/docs/format.md:478-491`'s documented convention."""
    rendered = json.dumps(dict(remaining_schema), sort_keys=True)
    return f"# Response Formats\n\n## response\n\n{rendered}"


def build_conversation(
    target: BackendTarget,
    messages: Sequence[Message],
    schema: Mapping[str, object] | None,
) -> Conversation:
    """Fleet's neutral `Message`s -> a Harmony `Conversation`: one `system` message (reasoning
    effort, never fabricated when `target.effort is None` — mirrors `vertex.py`'s identical
    `effort` rule), one `developer` message (instructions plus whichever tool this call offers),
    then the rest of `messages` mapped role-for-role (`system` turns beyond the first are folded
    into the developer instructions, matching how `client.py::_prepare_messages` already folds
    multiple system turns for a backend with `supports_system_prompt=False`)."""
    from openai_harmony import Message as HMessage

    system_content = SystemContent.new()
    system_content.reasoning_effort = (
        _REASONING_EFFORT[target.effort] if target.effort is not None else None
    )

    convo_messages = [HMessage.from_role_and_content(Role.SYSTEM, system_content)]

    systems = [m.content for m in messages if m.role == "system"]
    rest = [m for m in messages if m.role != "system"]

    instructions = "\n\n".join(systems) if systems else None
    # Refs resolved once, up front: Harmony's tool-parameter renderer does not follow `$ref`, so
    # an unresolved nested model would reach the model as `any` (see `_resolve_refs`).
    resolved = _resolve_refs(schema) if schema is not None else None
    file_edits_property = _file_edits_property(resolved) if resolved is not None else None

    developer_content = DeveloperContent.new()
    if resolved is not None and file_edits_property is not None:
        text = _APPLY_PATCH_INSTRUCTIONS
        if instructions:
            text = f"{instructions}\n\n{text}"
        remaining = _remaining_schema(resolved, file_edits_property)
        if remaining["properties"]:
            text = f"{text}\n\n{_response_format_block(remaining)}"
        developer_content = developer_content.with_instructions(text).with_function_tools(
            [_APPLY_PATCH_TOOL],
        )
    elif resolved is not None:
        text = instructions or ""
        developer_content = developer_content.with_instructions(text).with_function_tools(
            [
                ToolDescription.new(
                    _TOOL_NAME,
                    "Return the answer as this function's arguments.",
                    parameters=resolved,
                ),
            ],
        )
    else:
        developer_content = developer_content.with_instructions(instructions or "")

    convo_messages.append(HMessage.from_role_and_content(Role.DEVELOPER, developer_content))
    # Fleet's `Message` carries no tool name, and Harmony refuses to render a nameless tool turn
    # (`HarmonyError: Tools should have a name!`, a RuntimeError that no `LlmError` handler
    # catches). A `tool` turn here is always `client.py::_repair_turns` answering the one tool
    # THIS call offers, so it is attributed to that tool.
    offered_tool = _APPLY_PATCH_TOOL_NAME if file_edits_property is not None else _TOOL_NAME
    for m in rest:
        if m.role == "tool":
            convo_messages.append(_tool_result(offered_tool, m.content))
            continue
        role = {"user": Role.USER, "assistant": Role.ASSISTANT}[m.role]
        convo_messages.append(HMessage.from_role_and_content(role, m.content))

    return Conversation.from_messages(convo_messages)


def _tool_result(tool_name: str, content: str) -> HarmonyMessage:
    """A Harmony tool-result turn, per `references/harmony/docs/format.md`'s
    `<|start|>functions.{name} to=assistant<|channel|>commentary<|message|>...` convention."""
    return (
        HarmonyMessage.from_author_and_content(
            Author.new(Role.TOOL, f"functions.{tool_name}"), content,
        )
        .with_channel("commentary")
        .with_recipient("assistant")
    )


def render_for_completion(conversation: Conversation) -> list[int]:
    """`Conversation` -> token ids ready to post as `prompt` to vLLM's completions endpoint."""
    encoding = load_harmony_encoding(HarmonyEncodingName.HARMONY_GPT_OSS)
    # openai_harmony ships no py.typed marker (see this module's import-untyped note), so the SDK
    # call resolves to Any; cast matches the sibling backends' own convention for an untyped SDK
    # boundary (e.g. openai_compatible.py:257, vertex.py:346).
    tokens = encoding.render_conversation_for_completion(conversation, Role.ASSISTANT)
    return cast(list[int], tokens)


def _tool_call_message(
    parsed: Sequence[HarmonyMessage], recipient: str,
) -> HarmonyMessage | None:
    for message in parsed:
        if message.recipient == recipient:
            return message
    return None


def _final_channel_json(parsed: Sequence[HarmonyMessage]) -> dict[str, object] | None:
    for message in parsed:
        if message.channel != "final" or message.recipient is not None:
            continue
        if not message.content:
            continue
        text = message.content[0].text if hasattr(message.content[0], "text") else None
        if not isinstance(text, str):
            continue
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None
    return None


def _plain_text(parsed: Sequence[HarmonyMessage]) -> str | None:
    for message in parsed:
        if message.channel == "final" and message.recipient is None and message.content:
            content = message.content[0]
            text = content.text if hasattr(content, "text") else None
            if isinstance(text, str):
                return text
    return None


def _call_text(message: HarmonyMessage) -> str:
    content = message.content[0]
    text = content.text if hasattr(content, "text") else ""
    if isinstance(text, str) and text.startswith("{"):
        # Matches references/gpt-oss/gpt_oss/chat.py:199-206's own unwrap: some servers wrap a
        # single-string argument as {"<arg name>": "<value>"} JSON.
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return text
        if isinstance(parsed, dict) and len(parsed) == 1:
            return str(next(iter(parsed.values())))
    return text if isinstance(text, str) else ""


def _decode_apply_patch(
    patch_text: str, pre_images: Mapping[str, str],
) -> list[dict[str, str]] | None:
    """`apply_patch`-format text -> `[{"path", "diff"}]`, or `None` on ANY failure — a malformed
    or unresolvable patch is reported as "no answer", never raised (§7.7: a backend decides
    nothing, including never treating a mangled model reply as a hard failure)."""
    needed_paths = identify_files_needed(patch_text)
    if any(path not in pre_images for path in needed_paths):
        return None
    orig = {path: pre_images[path] for path in needed_paths}
    try:
        patch, _fuzz = text_to_patch(patch_text, orig)
        commit = patch_to_commit(patch, orig)
        return commit_to_file_edits(commit)
    except DiffError:
        return None


def parse_completion(
    tokens: Sequence[int],
    schema: Mapping[str, object] | None,
    *,
    pre_images: Mapping[str, str],
) -> tuple[str | None, dict[str, object] | None, FinishReason]:
    """Completion token ids -> `(text, tool_arguments, finish_reason)`. Reports, decides nothing:
    an unparseable or ambiguous reply comes back as `tool_arguments=None`, never an exception."""
    encoding = load_harmony_encoding(HarmonyEncodingName.HARMONY_GPT_OSS)
    try:
        parsed = encoding.parse_messages_from_completion_tokens(
            tokens, Role.ASSISTANT, strict=False,
        )
    except (RuntimeError, ValueError) as exc:
        raise MalformedHarmonyStream(
            f"completion token stream did not parse as Harmony messages: {exc}",
        ) from exc

    file_edits_property = _file_edits_property(schema) if schema is not None else None

    if file_edits_property is not None:
        call = _tool_call_message(parsed, "functions.apply_patch")
        if call is None:
            return _plain_text(parsed), None, "stop"
        edits = _decode_apply_patch(_call_text(call), pre_images)
        if edits is None:
            return None, None, "tool_call"
        other_fields = _final_channel_json(parsed) or {}
        remaining = _remaining_schema(dict(schema), file_edits_property)  # type: ignore[arg-type]
        required_value = remaining.get("required")
        required = set(required_value) if isinstance(required_value, list) else set()
        if not required.issubset(other_fields):
            return None, None, "tool_call"
        tool_arguments = {**other_fields, file_edits_property: edits}
        return None, tool_arguments, "tool_call"

    if schema is not None:
        call = _tool_call_message(parsed, f"functions.{_TOOL_NAME}")
        if call is None:
            return _plain_text(parsed), None, "stop"
        try:
            arguments = json.loads(_call_text(call))
        except json.JSONDecodeError:
            return None, None, "tool_call"
        return None, (arguments if isinstance(arguments, dict) else None), "tool_call"

    return _plain_text(parsed), None, "stop"


class HarmonyTargetMisconfigured(LlmError):
    """A `BackendTarget` this transport cannot dispatch. A config error, never a failover
    trigger — moving to the next target would not fix a missing field (matches
    `openai_compatible.py::TargetMisconfigured`'s identical reasoning)."""

    def __init__(self, target: BackendTarget, field: str, detail: str) -> None:
        super().__init__(
            f"harmony_gpt_oss target {target.backend}:{target.model_id} {detail} (field `{field}`)",
        )
        self.target = target
        self.field = field


class MissingBaseUrl(HarmonyTargetMisconfigured):
    """§13 row 36. This backend talks to vLLM's completions endpoint at an operator-configured
    `base_url` — there is no vendor default, exactly like `openai_compatible.py::MissingBaseUrl`."""

    def __init__(self, target: BackendTarget) -> None:
        super().__init__(
            target,
            "base_url",
            "declares no endpoint: harmony_gpt_oss has no vendor default to fall back on",
        )


class MalformedHarmonyStream(LlmError):
    """The completion's token stream did not parse as Harmony messages even under permissive
    (`strict=False`) parsing. Loud, typed, terminal — NOT a `TransportError`: the next target
    would reproduce the same unreadable stream from this adapter's own decode logic or a
    genuinely garbled model output, and failing over would hide which one it was (Rule 11,
    matching `openai_compatible.py::UnmappedFinishReason`'s identical reasoning)."""


class TokenCompletionTransport(Protocol):
    """One round trip to vLLM's completions endpoint, token ids in and out. The seam exists so
    the encode/decode logic above is exercised without a socket, matching
    `openai_compatible.py::ChatTransport`'s identical reasoning — the ONLY difference from that
    Protocol is that this one speaks token ids, never a chat-completions JSON body."""

    async def __call__(
        self,
        *,
        base_url: str,
        api_key: str,
        model_id: str,
        prompt_token_ids: Sequence[int],
        stop_token_ids: Sequence[int],
        max_tokens: int,
        timeout_s: float,
    ) -> Mapping[str, object]: ...


_PLACEHOLDER_API_KEY: Final[str] = "not-required"


class _VllmCompletionsTransport:
    """The ONLY object in this module that imports `openai` for the completions call. Reuses the
    `openai` SDK's LEGACY `client.completions` resource — NOT `client.chat.completions`, which
    speaks chat-message JSON — because that resource's `prompt` accepts a list of integers. See
    this task's module-level design note for exactly what is verified vs. assumed about vLLM's
    own wire contract."""

    async def __call__(
        self,
        *,
        base_url: str,
        api_key: str,
        model_id: str,
        prompt_token_ids: Sequence[int],
        stop_token_ids: Sequence[int],
        max_tokens: int,
        timeout_s: float,
    ) -> Mapping[str, object]:
        client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=timeout_s, max_retries=2)
        create = cast(
            Callable[..., Awaitable[object]],
            client.completions.create,
        )
        try:
            response = await create(
                model=model_id,
                prompt=list(prompt_token_ids),
                max_tokens=max_tokens,
                extra_body={"stop_token_ids": list(stop_token_ids), "skip_special_tokens": False},
            )
        except APITimeoutError as exc:
            raise TransportError(f"{base_url}: {exc}", trigger="CONNECTION") from exc
        except APIConnectionError as exc:
            raise TransportError(f"{base_url}: {exc}", trigger="CONNECTION") from exc
        except RateLimitError as exc:
            raise TransportError(f"{base_url}: {exc}", trigger="RATE_LIMIT") from exc
        except APIStatusError as exc:
            if exc.status_code >= 500:
                raise TransportError(
                    f"{base_url}: HTTP {exc.status_code}: {exc}", trigger="SERVER_ERROR",
                ) from exc
            raise LlmError(f"{base_url}: HTTP {exc.status_code}: {exc}") from exc
        finally:
            await client.close()

        choice = response.choices[0]  # type: ignore[attr-defined]
        text = choice.text or ""
        encoding = load_harmony_encoding(HarmonyEncodingName.HARMONY_GPT_OSS)
        token_ids = encoding.encode(text, allowed_special="all")
        finish_reason = "length" if choice.finish_reason == "length" else "stop"
        return {"token_ids": token_ids, "finish_reason": finish_reason, "stop_reason": None}


_DEFAULT_TRANSPORT: Final[TokenCompletionTransport] = _VllmCompletionsTransport()


@register_backend
class HarmonyGptOssBackend:
    """`ModelBackend` for GPT-OSS over Harmony. Stateless per call: the endpoint comes off the
    TARGET, because two targets in one tier may be two different vLLM hosts."""

    name: ClassVar[str] = "harmony_gpt_oss"
    version: ClassVar[int] = 1

    def __init__(self, transport: TokenCompletionTransport | None = None) -> None:
        """`register_backend` constructs this with `cls()`, so the collaborator is NOT stored on
        `self` in the registered case (`vars(inst) == {}`, SPEC §12 item 47) — `invoke` resolves
        the shared default lazily instead, matching `vertex.py.__init__`'s identical pattern."""
        if transport is not None:
            self._transport = transport

    def declared_capabilities(self, target: BackendTarget) -> ModelCapabilities:
        """Declared, never probed. Validates the target's own required field FIRST (§13 row 36),
        matching `vertex.py::declared_capabilities`'s identical ordering."""
        _require_base_url(target)
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
        base_url = _require_base_url(target)
        conversation = build_conversation(target, messages, schema)
        prompt_token_ids = render_for_completion(conversation)
        encoding = load_harmony_encoding(HarmonyEncodingName.HARMONY_GPT_OSS)
        transport = getattr(self, "_transport", _DEFAULT_TRANSPORT)
        raw = await transport(
            base_url=base_url,
            api_key=_PLACEHOLDER_API_KEY,
            model_id=target.model_id,
            prompt_token_ids=prompt_token_ids,
            stop_token_ids=encoding.stop_tokens_for_assistant_actions(),
            max_tokens=max_output_tokens,
            timeout_s=timeout_s,
        )
        token_ids = raw.get("token_ids")
        if not isinstance(token_ids, Sequence):
            raise TransportError(
                f"{base_url}: harmony_gpt_oss transport returned no token_ids",
                trigger="SERVER_ERROR",
            )
        pre_images = _extract_pre_images(messages)
        text, tool_arguments, decoded_finish_reason = parse_completion(
            list(token_ids), schema, pre_images=pre_images,
        )
        finish_reason: FinishReason = (
            "length" if raw.get("finish_reason") == "length" else decoded_finish_reason
        )
        return BackendReply(
            text=text,
            tool_arguments=tool_arguments,
            usage=TokenUsage(backend=HarmonyGptOssBackend.name, model_id=target.model_id),
            finish_reason=finish_reason,
        )


def _extract_pre_images(messages: Sequence[Message]) -> dict[str, str]:
    """Pull `` ```path:<path>\\n<content>\\n``` `` fenced blocks out of every message's content —
    see this task's design note on where `apply_patch` pre-images come from. This is a Fleet-side
    convention this plan introduces (no existing worker emits it yet); confirm or replace it in a
    follow-up review before wiring a real diff-bearing role at this backend (CLAUDE.md's
    directive-authority rule: this is an Agent Recommendation, not a directive)."""
    import re

    pattern = re.compile(r"```path:(?P<path>[^\n]+)\n(?P<content>.*?)```", re.DOTALL)
    images: dict[str, str] = {}
    for message in messages:
        for match in pattern.finditer(message.content):
            images[match.group("path")] = match.group("content")
    return images


def _require_base_url(target: BackendTarget) -> str:
    base_url = target.base_url
    if not base_url:
        raise MissingBaseUrl(target)
    return base_url


def _protocol_conformance(backend: HarmonyGptOssBackend) -> ModelBackend:
    """Compile-time only: `mypy --strict` fails here if this adapter drifts from `ModelBackend`."""
    return backend
