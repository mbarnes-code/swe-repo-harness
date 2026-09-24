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
from collections.abc import Mapping, Sequence
from typing import ClassVar, Final, cast

from openai_harmony import (
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
    TransportError,  # noqa: F401  (unused until Task 6 wires the completions transport)
    register_backend,
)
from fleet.models.enums import StructuredOutputMode
from fleet.models.tasks import (
    BackendTarget,
    ModelCapabilities,
    TokenUsage,  # noqa: F401  (unused until Task 6 wires the completions transport)
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


def _file_edits_property(schema: Mapping[str, object]) -> str | None:
    """The name of the one top-level property shaped like `[{path: string, diff: string, ...}]`
    — the `ProposedFileEdit` shape every diff-bearing response schema uses
    (`src/fleet/llm/schemas.py:176-200`, `215-225`, `227-235`) — or `None` for an ordinary
    schema. Structural detection, not a hard-coded property name: this module never imports
    `fleet.llm.schemas` (no backend does; a backend only ever sees the resolved JSON Schema)."""
    properties = schema.get("properties")
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
    file_edits_property = _file_edits_property(schema) if schema is not None else None

    developer_content = DeveloperContent.new()
    if file_edits_property is not None:
        text = _APPLY_PATCH_INSTRUCTIONS
        if instructions:
            text = f"{instructions}\n\n{text}"
        remaining = _remaining_schema(schema, file_edits_property)  # type: ignore[arg-type]
        if remaining["properties"]:
            text = f"{text}\n\n{_response_format_block(remaining)}"
        developer_content = developer_content.with_instructions(text).with_function_tools(
            [_APPLY_PATCH_TOOL],
        )
    elif schema is not None:
        text = instructions or ""
        developer_content = developer_content.with_instructions(text).with_function_tools(
            [
                ToolDescription.new(
                    _TOOL_NAME,
                    "Return the answer as this function's arguments.",
                    parameters=dict(schema),
                ),
            ],
        )
    else:
        developer_content = developer_content.with_instructions(instructions or "")

    convo_messages.append(HMessage.from_role_and_content(Role.DEVELOPER, developer_content))
    for m in rest:
        role = {"user": Role.USER, "assistant": Role.ASSISTANT, "tool": Role.TOOL}[m.role]
        convo_messages.append(HMessage.from_role_and_content(role, m.content))

    return Conversation.from_messages(convo_messages)


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


@register_backend
class HarmonyGptOssBackend:
    """`ModelBackend` for GPT-OSS over Harmony. Stateless per call: the endpoint comes off the
    TARGET, because two targets in one tier may be two different vLLM hosts."""

    name: ClassVar[str] = "harmony_gpt_oss"
    version: ClassVar[int] = 1

    def declared_capabilities(self, target: BackendTarget) -> ModelCapabilities:
        """Declared, never probed. Validates the target's own required field FIRST (§13 row 36),
        matching `vertex.py::declared_capabilities`'s identical ordering."""
        _require_base_url(target)
        return _DECLARED.model_copy()

    async def invoke(
        self,
        target: BackendTarget,
        messages: object,
        schema: dict[str, object] | None,
        mode: StructuredOutputMode,
        *,
        max_output_tokens: int,
        timeout_s: float,
    ) -> BackendReply:
        raise NotImplementedError("wired in Task 4/5/6")


def _require_base_url(target: BackendTarget) -> str:
    base_url = target.base_url
    if not base_url:
        raise MissingBaseUrl(target)
    return base_url


def _protocol_conformance(backend: HarmonyGptOssBackend) -> ModelBackend:
    """Compile-time only: `mypy --strict` fails here if this adapter drifts from `ModelBackend`."""
    return backend
