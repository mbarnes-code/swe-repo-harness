"""ADR-0145 — `src/fleet/llm/backends/harmony_gpt_oss.py`.

`openai-harmony` is a compiled (PyO3/maturin) extension. This file is written so it can run
whether or not the real package is installed, matching `tests/test_llm_backend_vertex.py`'s own
pattern: a minimal stub is installed into `sys.modules` BEFORE the adapter is imported, only when
the real package is genuinely absent (`find_spec`, never `sys.modules` membership — the two
answer different questions; see that file's `_absent()` docstring for why).

Coroutines are driven with `asyncio.run`, matching every other backend test file.
"""

from __future__ import annotations

import asyncio
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from fleet.llm import client as client_module
from fleet.llm.client import ModelBackend
from fleet.models.tasks import BackendTarget


def _absent(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is None
    except (ModuleNotFoundError, ValueError):
        return True


HARMONY_INSTALLED = not _absent("openai_harmony")


def target(**overrides: object) -> BackendTarget:
    fields: dict[str, object] = {
        "backend": "harmony_gpt_oss",
        "model_id": "model-under-test",
        "base_url": "http://pilot-spark.internal:8000/v1",
        "price": "free",
    }
    fields.update(overrides)
    return BackendTarget(**fields)


@pytest.mark.skipif(not HARMONY_INSTALLED, reason="requires the openai-harmony extra")
def test_discover_registers_harmony_gpt_oss_when_the_sdk_imports() -> None:
    from fleet.llm.backends.harmony_gpt_oss import HarmonyGptOssBackend

    found = client_module.discover()
    assert HarmonyGptOssBackend.name in found
    assert isinstance(found[HarmonyGptOssBackend.name], HarmonyGptOssBackend)


@pytest.mark.skipif(not HARMONY_INSTALLED, reason="requires the openai-harmony extra")
def test_name_and_version_are_class_attributes() -> None:
    from fleet.llm.backends.harmony_gpt_oss import HarmonyGptOssBackend

    assert HarmonyGptOssBackend.name == "harmony_gpt_oss"
    assert isinstance(HarmonyGptOssBackend.version, int)
    assert isinstance(HarmonyGptOssBackend(), ModelBackend)


def test_a_missing_sdk_leaves_the_backend_unregistered() -> None:
    """Driven in a fresh interpreter with `openai_harmony` blocked, matching
    `tests/test_llm_backend_vertex.py::test_a_missing_sdk_leaves_the_backend_unregistered` — the
    only honest way to test an import-time contract is at import time."""
    script = (
        "import sys\n"
        "class Block:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'openai_harmony' or name.startswith('openai_harmony.'):\n"
        "            raise ImportError('blocked')\n"
        "        return None\n"
        "sys.meta_path.insert(0, Block())\n"
        "from fleet.llm import client\n"
        "try:\n"
        "    import fleet.llm.backends.harmony_gpt_oss\n"
        "    print('IMPORTED')\n"
        "except ImportError:\n"
        "    print('IMPORT_ERROR')\n"
        "print('harmony_gpt_oss' in client.discover())\n"
    )
    out = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
        cwd=str(Path(__file__).resolve().parents[1]),
    ).stdout.split()
    assert out[0] == "IMPORT_ERROR", out
    assert out[1] == "False", out


@pytest.mark.skipif(not HARMONY_INSTALLED, reason="requires the openai-harmony extra")
def test_a_target_with_no_base_url_names_the_missing_field() -> None:
    from fleet.llm.backends.harmony_gpt_oss import HarmonyGptOssBackend, MissingBaseUrl

    with pytest.raises(MissingBaseUrl):
        HarmonyGptOssBackend().declared_capabilities(target(base_url=None))


@pytest.mark.skipif(not HARMONY_INSTALLED, reason="requires the openai-harmony extra")
def test_declared_capabilities_claim_tool_call_and_prompted_only() -> None:
    from fleet.llm.backends.harmony_gpt_oss import HarmonyGptOssBackend
    from fleet.models.enums import StructuredOutputMode

    caps = HarmonyGptOssBackend().declared_capabilities(target())
    assert caps.supports_tools is True
    assert caps.supports_json_schema is False
    assert set(caps.structured_output_modes) == {
        StructuredOutputMode.TOOL_CALL,
        StructuredOutputMode.PROMPTED,
    }


@pytest.mark.skipif(not HARMONY_INSTALLED, reason="requires the openai-harmony extra")
def test_the_declaration_is_a_copy_not_shared_module_state() -> None:
    from fleet.llm.backends.harmony_gpt_oss import HarmonyGptOssBackend

    backend = HarmonyGptOssBackend()
    first = backend.declared_capabilities(target())
    first.max_context = 999_999  # type: ignore[misc]  # FleetModel allows assignment
    second = backend.declared_capabilities(target())
    assert second.max_context != 999_999


LLM_PATCH_PROPOSAL_SCHEMA: dict[str, object] = {
    "type": "object",
    "title": "LlmPatchProposal",
    "properties": {
        "files": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "diff": {"type": "string"},
                },
                "required": ["path", "diff"],
            },
        },
        "approach_summary": {"type": "string"},
        "rationale": {"type": "string"},
    },
    "required": ["files", "approach_summary", "rationale"],
}

REPO_CLASSIFICATION_SCHEMA: dict[str, object] = {
    "type": "object",
    "title": "RepoClassification",
    "properties": {
        "ecosystem": {"type": "string"},
        "is_library": {"type": "boolean"},
        "confidence": {"type": "number"},
        "rationale": {"type": "string"},
    },
    "required": ["ecosystem", "is_library", "confidence", "rationale"],
}


@pytest.mark.skipif(not HARMONY_INSTALLED, reason="requires the openai-harmony extra")
def test_file_edits_property_finds_the_diff_shaped_array() -> None:
    from fleet.llm.backends.harmony_gpt_oss import _file_edits_property

    assert _file_edits_property(LLM_PATCH_PROPOSAL_SCHEMA) == "files"


@pytest.mark.skipif(not HARMONY_INSTALLED, reason="requires the openai-harmony extra")
def test_file_edits_property_is_none_for_an_ordinary_schema() -> None:
    from fleet.llm.backends.harmony_gpt_oss import _file_edits_property

    assert _file_edits_property(REPO_CLASSIFICATION_SCHEMA) is None


@pytest.mark.skipif(not HARMONY_INSTALLED, reason="requires the openai-harmony extra")
@pytest.mark.parametrize(
    "model_name", ["LlmPatchProposal", "ApiRewriteProposal", "LlmEscalationProposal"],
)
def test_file_edits_property_finds_files_in_the_real_pydantic_schemas(model_name: str) -> None:
    """The REAL `model_json_schema()` output, not a hand-inlined fixture: Pydantic emits
    `files.items` as `{"$ref": "#/$defs/ProposedFileEdit"}`, which the hand-inlined
    `LLM_PATCH_PROPOSAL_SCHEMA` above never exercised — the diff-shaped path was dead code in
    production while that fixture's test passed."""
    from fleet.llm import schemas
    from fleet.llm.backends.harmony_gpt_oss import _file_edits_property

    schema = getattr(schemas, model_name).model_json_schema()
    assert schema["properties"]["files"]["items"] == {"$ref": "#/$defs/ProposedFileEdit"}
    assert _file_edits_property(schema) == "files"


@pytest.mark.skipif(not HARMONY_INSTALLED, reason="requires the openai-harmony extra")
def test_emit_response_renders_a_nested_models_real_shape_not_any() -> None:
    """Harmony's tool-parameter renderer does not follow `$ref`; unresolved, a nested model's
    field reaches the model as `any[]`."""
    from openai_harmony import HarmonyEncodingName, load_harmony_encoding
    from pydantic import BaseModel

    from fleet.llm.backends.harmony_gpt_oss import build_conversation, render_for_completion
    from fleet.llm.client import Message

    class Inner(BaseModel):
        name: str

    class Outer(BaseModel):
        items: list[Inner]

    schema = Outer.model_json_schema()
    assert "$defs" in schema
    convo = build_conversation(target(), (Message(role="user", content="hi"),), schema)
    rendered = load_harmony_encoding(HarmonyEncodingName.HARMONY_GPT_OSS).decode(
        render_for_completion(convo),
    )
    assert "items: any[]" not in rendered
    assert "name: string" in rendered


@pytest.mark.skipif(not HARMONY_INSTALLED, reason="requires the openai-harmony extra")
def test_build_conversation_renders_to_a_nonempty_token_sequence() -> None:
    from fleet.llm.backends.harmony_gpt_oss import build_conversation, render_for_completion
    from fleet.llm.client import Message

    messages = (
        Message(role="system", content="Be terse."),
        Message(role="user", content="Rewrite src/app.py to fix the bug."),
    )
    convo = build_conversation(target(), messages, LLM_PATCH_PROPOSAL_SCHEMA)
    tokens = render_for_completion(convo)
    assert isinstance(tokens, list)
    assert len(tokens) > 0
    assert all(isinstance(t, int) for t in tokens)


@pytest.mark.skipif(not HARMONY_INSTALLED, reason="requires the openai-harmony extra")
def test_build_conversation_for_an_ordinary_schema_offers_no_apply_patch_tool() -> None:
    """The developer message's rendered tokens must not contain the literal `apply_patch`
    identifier when the schema is not diff-shaped — proven by encoding that literal string and
    checking it is not a substring of the decoded developer-message text, not by inspecting SDK
    internals."""
    from openai_harmony import HarmonyEncodingName, load_harmony_encoding

    from fleet.llm.backends.harmony_gpt_oss import build_conversation, render_for_completion
    from fleet.llm.client import Message

    messages = (Message(role="user", content="Classify this repo."),)
    convo = build_conversation(target(), messages, REPO_CLASSIFICATION_SCHEMA)
    tokens = render_for_completion(convo)
    encoding = load_harmony_encoding(HarmonyEncodingName.HARMONY_GPT_OSS)
    rendered_text = encoding.decode(tokens)
    assert "apply_patch" not in rendered_text
    assert "emit_response" in rendered_text


@pytest.mark.skipif(not HARMONY_INSTALLED, reason="requires the openai-harmony extra")
def test_build_conversation_for_a_diff_shaped_schema_offers_apply_patch() -> None:
    from openai_harmony import HarmonyEncodingName, load_harmony_encoding

    from fleet.llm.backends.harmony_gpt_oss import build_conversation, render_for_completion
    from fleet.llm.client import Message

    messages = (Message(role="user", content="Fix the bug in src/app.py."),)
    convo = build_conversation(target(), messages, LLM_PATCH_PROPOSAL_SCHEMA)
    tokens = render_for_completion(convo)
    encoding = load_harmony_encoding(HarmonyEncodingName.HARMONY_GPT_OSS)
    rendered_text = encoding.decode(tokens)
    assert "apply_patch" in rendered_text
    assert "Begin Patch" in rendered_text  # from _APPLY_PATCH_INSTRUCTIONS


@pytest.mark.skipif(not HARMONY_INSTALLED, reason="requires the openai-harmony extra")
@pytest.mark.parametrize(
    ("schema", "tool_name"),
    [(LLM_PATCH_PROPOSAL_SCHEMA, "apply_patch"), (REPO_CLASSIFICATION_SCHEMA, "emit_response")],
)
def test_a_repair_turns_tool_message_renders_attributed_to_the_offered_tool(
    schema: dict[str, object], tool_name: str,
) -> None:
    """`client.py::_repair_turns` sends a bare `Message(role="tool", ...)` (Fleet's `Message` has no
    name field). Rendered nameless, the real SDK raises `HarmonyError: Tools should have a name!`
    — a RuntimeError, not an `LlmError`, so it escaped `complete()` uncaught."""
    from openai_harmony import HarmonyEncodingName, load_harmony_encoding

    from fleet.llm.backends.harmony_gpt_oss import build_conversation, render_for_completion
    from fleet.llm.client import BackendReply, Message, _repair_turns
    from fleet.models.enums import StructuredOutputMode
    from fleet.models.tasks import TokenUsage

    reply = BackendReply(
        text=None, tool_arguments={"x": 1}, usage=TokenUsage(), finish_reason="tool_call",
    )
    messages = (
        Message(role="user", content="do it"),
        *_repair_turns(reply, StructuredOutputMode.TOOL_CALL, "field required"),
    )
    assert messages[1].role == "tool"
    tokens = render_for_completion(build_conversation(target(), messages, schema))
    rendered = load_harmony_encoding(HarmonyEncodingName.HARMONY_GPT_OSS).decode(tokens)
    assert (
        f"<|start|>functions.{tool_name} to=assistant<|channel|>commentary"
        f'<|message|>{{"x": 1}}<|end|>'
    ) in rendered


@pytest.mark.skipif(not HARMONY_INSTALLED, reason="requires the openai-harmony extra")
def test_effort_none_omits_the_reasoning_line_entirely() -> None:
    """Mirrors `vertex.py`'s `effort` rule: `None` means the operator wrote no preference, and
    this backend must send no `Reasoning:` line at all rather than the SDK's own MEDIUM default."""
    from openai_harmony import HarmonyEncodingName, load_harmony_encoding

    from fleet.llm.backends.harmony_gpt_oss import build_conversation, render_for_completion
    from fleet.llm.client import Message

    messages = (Message(role="user", content="hi"),)
    convo = build_conversation(target(effort=None), messages, REPO_CLASSIFICATION_SCHEMA)
    tokens = render_for_completion(convo)
    encoding = load_harmony_encoding(HarmonyEncodingName.HARMONY_GPT_OSS)
    assert "Reasoning:" not in encoding.decode(tokens)


@pytest.mark.skipif(not HARMONY_INSTALLED, reason="requires the openai-harmony extra")
def test_effort_high_renders_the_reasoning_line() -> None:
    from openai_harmony import HarmonyEncodingName, load_harmony_encoding

    from fleet.llm.backends.harmony_gpt_oss import build_conversation, render_for_completion
    from fleet.llm.client import Message

    messages = (Message(role="user", content="hi"),)
    convo = build_conversation(target(effort="high"), messages, REPO_CLASSIFICATION_SCHEMA)
    tokens = render_for_completion(convo)
    encoding = load_harmony_encoding(HarmonyEncodingName.HARMONY_GPT_OSS)
    # Deviation from the brief's literal assertion (documented in task-4-5-report.md): the
    # brief's `"Reasoning: high" in decode(tokens).lower() or "Reasoning: High" in decode(tokens)`
    # is unsatisfiable for ANY rendering — the first branch lowercases the haystack so a
    # capital-R needle can never match, and the measured SDK output is "Reasoning: high" (capital
    # R, lowercase "high"), which the second branch also misses. Rewritten as a single
    # case-insensitive check that preserves the test's intent (the reasoning line is present and
    # names "high") without guessing the SDK's exact capitalization.
    assert "reasoning: high" in encoding.decode(tokens).lower()


def _encode_assistant_reply(convo_messages, encoding) -> list[int]:
    """Build a fixture token sequence via the SDK's own encode, never hand-written raw token ints.
    `convo_messages` are the ASSISTANT-authored messages only.

    Deviation from the brief's literal docstring claim (documented in task-4-5-report.md,
    measured directly against the installed `openai-harmony` 0.0.8):
    `encoding.render_conversation` does NOT omit a leading new-turn role marker for the first
    message — it renders the FULL `<|start|>assistant<|channel|>...` header, identical to what
    `render_conversation_for_completion` would put at the END of a prompt. A real vLLM completions
    call never echoes that prompt-side
    `<|start|>assistant` back in its returned completion tokens, so `parse_completion`'s
    `encoding.parse_messages_from_completion_tokens(tokens, Role.ASSISTANT, ...)` (which tells the
    parser "the first message's role marker is missing, assume assistant") corrupts on a token
    stream that still has that marker attached (measured: the two-message fixture raised
    `MalformedHarmonyStream`, and the one-message fixture silently produced a garbage
    `recipient='<|start|>assistant'`). Strip exactly the tokens `render_conversation_for_completion`
    would have appended for an empty, about-to-speak assistant turn -- computed, not hard-coded, so
    it tracks the installed encoding rather than assuming a token count."""
    from openai_harmony import Conversation, Role

    full = encoding.render_conversation(Conversation.from_messages(convo_messages))
    prompt_prefix = encoding.render_conversation_for_completion(
        Conversation.from_messages([]), Role.ASSISTANT,
    )
    return full[len(prompt_prefix) :]


@pytest.mark.skipif(not HARMONY_INSTALLED, reason="requires the openai-harmony extra")
def test_parse_completion_decodes_an_ordinary_tool_call() -> None:
    from openai_harmony import HarmonyEncodingName, Role, load_harmony_encoding
    from openai_harmony import Message as HMessage

    from fleet.llm.backends.harmony_gpt_oss import parse_completion

    encoding = load_harmony_encoding(HarmonyEncodingName.HARMONY_GPT_OSS)
    reply_json = (
        '{"ecosystem": "python", "is_library": false, "confidence": 0.9, "rationale": "obvious"}'
    )
    reply = (
        HMessage.from_role_and_content(Role.ASSISTANT, reply_json)
        .with_channel("commentary")
        .with_recipient("functions.emit_response")
    )
    tokens = _encode_assistant_reply([reply], encoding)

    _, tool_arguments, finish_reason = parse_completion(
        tokens, REPO_CLASSIFICATION_SCHEMA, pre_images={},
    )
    assert tool_arguments == {
        "ecosystem": "python",
        "is_library": False,
        "confidence": 0.9,
        "rationale": "obvious",
    }
    assert finish_reason == "tool_call"


@pytest.mark.skipif(not HARMONY_INSTALLED, reason="requires the openai-harmony extra")
def test_a_single_property_schema_decodes_its_one_key_object() -> None:
    """The REAL `PrTitle` schema (`pr_title` role). `apply_patch`'s 1-key-object unwrap once ran
    on this path too and turned `{"title": ...}` into a bare string that decoded to `None`."""
    from openai_harmony import Message as HMessage
    from openai_harmony import Role

    from fleet.llm.backends.harmony_gpt_oss import parse_completion
    from fleet.llm.schemas import PrTitle

    call = (
        HMessage.from_role_and_content(Role.ASSISTANT, '{"title": "Migrate to Bazel"}')
        .with_channel("commentary")
        .with_recipient("functions.emit_response")
    )
    _, tool_arguments, _ = parse_completion(
        _sampled_completion([call]), PrTitle.model_json_schema(), pre_images={},
    )
    assert tool_arguments == {"title": "Migrate to Bazel"}


@pytest.mark.skipif(not HARMONY_INSTALLED, reason="requires the openai-harmony extra")
def test_an_apply_patch_call_wrapped_as_a_one_key_object_is_still_unwrapped() -> None:
    import json

    from fleet.llm.backends.harmony_gpt_oss import parse_completion

    wrapped = _apply_patch_call(json.dumps({"input": _PATCH_TEXT}))
    _, tool_arguments, _ = parse_completion(
        _sampled_completion([wrapped]),
        LLM_PATCH_PROPOSAL_SCHEMA,
        pre_images={"src/app.py": 'def greet():\n    print("Hi")\n'},
    )
    assert tool_arguments is not None
    assert tool_arguments["files"][0]["path"] == "src/app.py"


@pytest.mark.skipif(not HARMONY_INSTALLED, reason="requires the openai-harmony extra")
def test_parse_completion_decodes_an_apply_patch_call_into_the_file_edits_property() -> None:
    """One completion carries ONLY the `apply_patch` half — it stops at `<|call|>`. (This test
    previously also put a `final` message in the same completion, built via
    `render_conversation`, which rewrites `<|return|>` to `<|end|>`: a shape no sampler can emit.
    The remaining fields come from `invoke()`'s second round trip, tested above.)"""
    from fleet.llm.backends.harmony_gpt_oss import parse_completion

    tokens = _sampled_completion([_apply_patch_call()])
    _, tool_arguments, finish_reason = parse_completion(
        tokens,
        LLM_PATCH_PROPOSAL_SCHEMA,
        pre_images={"src/app.py": 'def greet():\n    print("Hi")\n'},
    )
    assert tool_arguments is not None
    assert set(tool_arguments) == {"files"}
    files = tool_arguments["files"]
    assert isinstance(files, list) and len(files) == 1
    assert files[0]["path"] == "src/app.py"
    assert "Hello, world!" in files[0]["diff"]
    assert finish_reason == "tool_call"


@pytest.mark.skipif(not HARMONY_INSTALLED, reason="requires the openai-harmony extra")
def test_parse_completion_returns_none_arguments_when_apply_patch_names_a_missing_pre_image() \
        -> None:
    from openai_harmony import HarmonyEncodingName, Role, load_harmony_encoding
    from openai_harmony import Message as HMessage

    from fleet.llm.backends.harmony_gpt_oss import parse_completion

    encoding = load_harmony_encoding(HarmonyEncodingName.HARMONY_GPT_OSS)
    patch_text = "*** Begin Patch\n*** Update File: missing.py\n@@\n-a\n+b\n*** End Patch"
    call = (
        HMessage.from_role_and_content(Role.ASSISTANT, patch_text)
        .with_channel("commentary")
        .with_recipient("functions.apply_patch")
    )
    tokens = _encode_assistant_reply([call], encoding)

    _, tool_arguments, finish_reason = parse_completion(
        tokens, LLM_PATCH_PROPOSAL_SCHEMA, pre_images={},
    )
    assert tool_arguments is None
    assert finish_reason == "tool_call"


@pytest.mark.skipif(not HARMONY_INSTALLED, reason="requires the openai-harmony extra")
def test_parse_completion_reads_the_final_channel_as_plain_text_when_no_tool_call() -> None:
    from openai_harmony import HarmonyEncodingName, Role, load_harmony_encoding
    from openai_harmony import Message as HMessage

    from fleet.llm.backends.harmony_gpt_oss import parse_completion

    encoding = load_harmony_encoding(HarmonyEncodingName.HARMONY_GPT_OSS)
    final = HMessage.from_role_and_content(Role.ASSISTANT, "Hello, human.").with_channel("final")
    tokens = _encode_assistant_reply([final], encoding)

    text, tool_arguments, finish_reason = parse_completion(tokens, None, pre_images={})
    assert text == "Hello, human."
    assert tool_arguments is None
    assert finish_reason == "stop"


class _FakeTransport:
    """Records what it was called with; returns a canned RawCompletion-shaped mapping."""

    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    async def __call__(
        self,
        *,
        base_url: str,
        api_key: str,
        model_id: str,
        prompt_token_ids,
        stop_token_ids,
        max_tokens: int,
        timeout_s: float,
    ) -> dict[str, object]:
        self.calls.append(
            {
                "base_url": base_url,
                "api_key": api_key,
                "model_id": model_id,
                "prompt_token_ids": list(prompt_token_ids),
                "stop_token_ids": list(stop_token_ids),
                "max_tokens": max_tokens,
                "timeout_s": timeout_s,
            },
        )
        return self.response


def _fixture_completion_tokens(assistant_messages) -> list[int]:
    """Deviation from the brief's literal body (documented here, matching the same-shaped
    deviations recorded in task-4-5-report.md): the brief's literal
    `encoding.render_conversation(Conversation.from_messages(assistant_messages))` reproduces the
    exact leading-`<|start|>assistant`-header bug this file's own `_encode_assistant_reply`
    docstring already diagnoses and fixes above — measured directly: it raised
    `MalformedHarmonyStream` (`unexpected tokens remaining in message header:
    Some("<|start|>assistant")`) rather than round-tripping. Delegates to the already-fixed
    `_encode_assistant_reply` instead of re-introducing the bug a second time in this file."""
    from openai_harmony import HarmonyEncodingName, load_harmony_encoding

    encoding = load_harmony_encoding(HarmonyEncodingName.HARMONY_GPT_OSS)
    return _encode_assistant_reply(assistant_messages, encoding)


def _sampled_completion(assistant_messages) -> list[int]:
    """What a real sampler returns: every message rendered by the SDK's own `render` (never
    `render_conversation`, which drops an `analysis` message that precedes a `final` one and
    rewrites a trailing `<|return|>` to `<|end|>` as it would for stored history), minus the
    prompt's `<|start|>assistant`, with a `final` message ending in its decode-time `<|return|>`
    (a tool call already renders ending in `<|call|>`)."""
    from openai_harmony import HarmonyEncodingName, load_harmony_encoding

    encoding = load_harmony_encoding(HarmonyEncodingName.HARMONY_GPT_OSS)
    tokens: list[int] = []
    for message in assistant_messages:
        tokens += encoding.render(message)
    start = encoding.encode("<|start|>assistant", allowed_special="all")
    assert tokens[: len(start)] == start
    tokens = tokens[len(start) :]
    if assistant_messages[-1].channel == "final":
        end, ret = encoding.encode("<|end|><|return|>", allowed_special="all")
        assert tokens[-1] == end
        tokens[-1] = ret
    return tokens


def _final(text: str):
    from openai_harmony import Message as HMessage
    from openai_harmony import Role

    return HMessage.from_role_and_content(Role.ASSISTANT, text).with_channel("final")


@pytest.mark.skipif(not HARMONY_INSTALLED, reason="requires the openai-harmony extra")
def test_invoke_round_trips_through_a_fake_transport() -> None:
    from openai_harmony import Message as HMessage
    from openai_harmony import Role

    from fleet.llm.backends.harmony_gpt_oss import HarmonyGptOssBackend
    from fleet.llm.client import Message
    from fleet.models.enums import StructuredOutputMode

    reply_json = (
        '{"ecosystem": "python", "is_library": false, "confidence": 0.9, "rationale": "obvious"}'
    )
    reply = (
        HMessage.from_role_and_content(Role.ASSISTANT, reply_json)
        .with_channel("commentary")
        .with_recipient("functions.emit_response")
    )
    token_ids = _fixture_completion_tokens([reply])
    transport = _FakeTransport(
        {"token_ids": token_ids, "finish_reason": "stop", "stop_reason": 200012},
    )

    backend = HarmonyGptOssBackend(transport=transport)
    result = asyncio.run(
        backend.invoke(
            target(),
            (Message(role="user", content="Classify this repo."),),
            REPO_CLASSIFICATION_SCHEMA,
            StructuredOutputMode.TOOL_CALL,
            max_output_tokens=512,
            timeout_s=30.0,
        ),
    )
    assert result.tool_arguments == {
        "ecosystem": "python",
        "is_library": False,
        "confidence": 0.9,
        "rationale": "obvious",
    }
    assert result.finish_reason == "tool_call"
    assert len(transport.calls) == 1
    assert transport.calls[0]["model_id"] == target().model_id
    assert transport.calls[0]["max_tokens"] == 512


class _SequencedTransport(_FakeTransport):
    """Returns `responses[i]` on the i-th call, so a test can see BOTH of a diff-shaped
    `invoke()`'s round trips happen in order."""

    def __init__(self, responses: list[dict[str, object]]) -> None:
        super().__init__({})
        self.responses = responses

    async def __call__(self, **kwargs: object) -> dict[str, object]:  # type: ignore[override]
        self.response = self.responses[len(self.calls)]
        return await super().__call__(**kwargs)  # type: ignore[arg-type]


_PATCH_TEXT = (
    "*** Begin Patch\n*** Update File: src/app.py\n@@ def greet():\n"
    '-    print("Hi")\n+    print("Hello, world!")\n*** End Patch'
)
_PRE_IMAGE_MESSAGE = 'Fix the greeting.\n```path:src/app.py\ndef greet():\n    print("Hi")\n```'


def _apply_patch_call(patch_text: str = _PATCH_TEXT):
    from openai_harmony import Message as HMessage
    from openai_harmony import Role

    return (
        HMessage.from_role_and_content(Role.ASSISTANT, patch_text)
        .with_channel("commentary")
        .with_recipient("functions.apply_patch")
    )


def _invoke_diff_shaped(transport):
    from fleet.llm import schemas
    from fleet.llm.backends.harmony_gpt_oss import HarmonyGptOssBackend
    from fleet.llm.client import Message
    from fleet.models.enums import StructuredOutputMode

    return asyncio.run(
        HarmonyGptOssBackend(transport=transport).invoke(
            target(),
            (Message(role="user", content=_PRE_IMAGE_MESSAGE),),
            schemas.LlmPatchProposal.model_json_schema(),
            StructuredOutputMode.TOOL_CALL,
            max_output_tokens=512,
            timeout_s=30.0,
        ),
    )


@pytest.mark.skipif(not HARMONY_INSTALLED, reason="requires the openai-harmony extra")
def test_a_diff_shaped_invoke_makes_two_round_trips_and_merges_them() -> None:
    """Call 1 stops at `<|call|>` with the patch; call 2 is sent the call back plus a tool result
    and answers the remaining fields on `final`. Uses the REAL `LlmPatchProposal` schema, and the
    merged arguments must validate against that model."""
    from openai_harmony import HarmonyEncodingName, Role, load_harmony_encoding
    from openai_harmony import Message as HMessage

    from fleet.llm import schemas

    encoding = load_harmony_encoding(HarmonyEncodingName.HARMONY_GPT_OSS)
    analysis = HMessage.from_role_and_content(Role.ASSISTANT, "Patch it.").with_channel("analysis")
    transport = _SequencedTransport(
        [
            {"token_ids": _sampled_completion([analysis, _apply_patch_call()]),
             "finish_reason": "stop"},
            {"token_ids": _sampled_completion(
                [_final('{"approach_summary": "fix greeting", "rationale": "typo"}')]),
             "finish_reason": "stop"},
        ],
    )
    result = _invoke_diff_shaped(transport)

    assert len(transport.calls) == 2
    second_prompt = encoding.decode(transport.calls[1]["prompt_token_ids"])
    assert second_prompt.startswith(encoding.decode(transport.calls[0]["prompt_token_ids"]))
    # The CoT before a tool call is passed back (format.md), the call keeps its `<|call|>` as the
    # SDK renders it, and the tool result is attributed to apply_patch.
    assert second_prompt.endswith(
        "<|start|>assistant<|channel|>analysis<|message|>Patch it.<|end|>"
        "<|start|>assistant to=functions.apply_patch<|channel|>commentary<|message|>"
        f"{_PATCH_TEXT}<|call|>"
        "<|start|>functions.apply_patch to=assistant<|channel|>commentary<|message|>"
        "patch parsed<|end|><|start|>assistant",
    )
    assert result.finish_reason == "tool_call"
    assert result.tool_arguments is not None
    proposal = schemas.LlmPatchProposal.model_validate(result.tool_arguments)
    assert proposal.approach_summary == "fix greeting"
    assert [f.path for f in proposal.files] == ["src/app.py"]
    assert "Hello, world!" in proposal.files[0].diff


@pytest.mark.skipif(not HARMONY_INSTALLED, reason="requires the openai-harmony extra")
def test_a_diff_shaped_invoke_sums_both_round_trips_usage() -> None:
    transport = _SequencedTransport(
        [
            {"token_ids": _sampled_completion([_apply_patch_call()]), "finish_reason": "stop",
             "usage": {"prompt_tokens": 100, "completion_tokens": 20}},
            {"token_ids": _sampled_completion(
                [_final('{"approach_summary": "fix greeting", "rationale": "typo"}')]),
             "finish_reason": "stop",
             "usage": {"prompt_tokens": 130, "completion_tokens": 7}},
        ],
    )
    result = _invoke_diff_shaped(transport)
    assert result.tool_arguments is not None
    assert (result.usage.input_tokens, result.usage.output_tokens) == (230, 27)
    assert result.usage.model_id == target().model_id


@pytest.mark.skipif(not HARMONY_INSTALLED, reason="requires the openai-harmony extra")
def test_the_vllm_transport_carries_the_servers_usage_through_to_the_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Through the REAL `openai` SDK parsing a vLLM-shaped completions body (only the socket is
    replaced, by an `httpx.MockTransport`), so the `usage` shape is the SDK's, not a guess."""
    import httpx
    from openai import AsyncOpenAI

    from fleet.llm.backends import harmony_gpt_oss
    from fleet.llm.client import Message
    from fleet.models.enums import StructuredOutputMode

    body = {
        "id": "cmpl-1", "object": "text_completion", "created": 0, "model": "served-name",
        "choices": [{"index": 0, "text": "<|channel|>final<|message|>hi<|return|>",
                     "finish_reason": "stop", "logprobs": None}],
        "usage": {"prompt_tokens": 321, "completion_tokens": 12, "total_tokens": 333},
    }

    def factory(**kwargs: object) -> AsyncOpenAI:
        mock = httpx.MockTransport(lambda request: httpx.Response(200, json=body))
        return AsyncOpenAI(**kwargs, http_client=httpx.AsyncClient(transport=mock))  # type: ignore[arg-type]

    monkeypatch.setattr(harmony_gpt_oss, "AsyncOpenAI", factory)
    result = asyncio.run(
        harmony_gpt_oss.HarmonyGptOssBackend().invoke(
            target(), (Message(role="user", content="hi"),), None,
            StructuredOutputMode.PROMPTED, max_output_tokens=64, timeout_s=30.0,
        ),
    )
    assert result.text == "hi"
    assert (result.usage.input_tokens, result.usage.output_tokens) == (321, 12)
    assert result.usage.model_id == target().model_id


@pytest.mark.skipif(not HARMONY_INSTALLED, reason="requires the openai-harmony extra")
def test_a_diff_shaped_invoke_whose_patch_fails_to_decode_makes_no_second_call() -> None:
    bad = _apply_patch_call(_PATCH_TEXT.replace("src/app.py", "missing.py"))
    transport = _SequencedTransport(
        [{"token_ids": _sampled_completion([bad]), "finish_reason": "stop"}],
    )
    result = _invoke_diff_shaped(transport)
    assert len(transport.calls) == 1
    assert result.tool_arguments is None


@pytest.mark.skipif(not HARMONY_INSTALLED, reason="requires the openai-harmony extra")
def test_a_diff_shaped_invoke_missing_a_required_field_in_call_two_is_no_answer() -> None:
    transport = _SequencedTransport(
        [
            {"token_ids": _sampled_completion([_apply_patch_call()]), "finish_reason": "stop"},
            {"token_ids": _sampled_completion([_final('{"approach_summary": "x"}')]),
             "finish_reason": "stop"},
        ],
    )
    result = _invoke_diff_shaped(transport)
    assert len(transport.calls) == 2
    assert result.tool_arguments is None


@pytest.mark.skipif(not HARMONY_INSTALLED, reason="requires the openai-harmony extra")
def test_invoke_stops_only_on_call_and_return_never_on_end() -> None:
    """`<|end|>` closes the `analysis` message GPT-OSS always emits first; stopping on it
    (`encoding.stop_tokens()`) would cut every reply off before its answer. Pinned to the actual
    ids (`<|return|>` = 200002, `<|call|>` = 200012; `<|end|>` = 200007 must be absent), and
    shown on a realistic analysis-then-answer stream truncated the way vLLM would."""
    from openai_harmony import HarmonyEncodingName, Role, load_harmony_encoding
    from openai_harmony import Message as HMessage

    from fleet.llm.backends.harmony_gpt_oss import HarmonyGptOssBackend, parse_completion
    from fleet.llm.client import Message
    from fleet.models.enums import StructuredOutputMode

    encoding = load_harmony_encoding(HarmonyEncodingName.HARMONY_GPT_OSS)
    transport = _FakeTransport(
        {"token_ids": _sampled_completion([_final("hi")]), "finish_reason": "stop"},
    )
    asyncio.run(
        HarmonyGptOssBackend(transport=transport).invoke(
            target(), (Message(role="user", content="hi"),), None,
            StructuredOutputMode.PROMPTED, max_output_tokens=64, timeout_s=30.0,
        ),
    )
    stops = set(transport.calls[0]["stop_token_ids"])
    assert stops == {200002, 200012}
    assert {encoding.decode([t]) for t in stops} == {"<|return|>", "<|call|>"}

    analysis = HMessage.from_role_and_content(Role.ASSISTANT, "Think.").with_channel("analysis")
    stream = _sampled_completion([analysis, _final("the answer")])
    cut = next(i for i, t in enumerate(stream) if t in stops)
    text, _, _ = parse_completion(stream[: cut + 1], None, pre_images={})
    assert text == "the answer"


@pytest.mark.skipif(not HARMONY_INSTALLED, reason="requires the openai-harmony extra")
def test_invoke_maps_a_length_finish_reason() -> None:
    """Deviation from the brief's literal hardcoded `token_ids: [200006, 173781]` (documented
    here, same class of deviation as `_fixture_completion_tokens` above): those two raw ints do
    not form even a truncated-but-valid Harmony message — measured directly, they raise
    `MalformedHarmonyStream` ("Unexpected EOS while waiting for message header to complete"), not
    a parseable partial reply. A real vLLM `length` truncation cuts a token stream off before its
    terminal `<|end|>`/`<|return|>` token while everything before that is well-formed, so the
    fixture here builds a real one-message stream via the SDK and drops exactly its trailing stop
    token — confirmed to still parse correctly under `strict=False`."""
    from openai_harmony import Message as HMessage
    from openai_harmony import Role

    from fleet.llm.backends.harmony_gpt_oss import HarmonyGptOssBackend
    from fleet.llm.client import Message
    from fleet.models.enums import StructuredOutputMode

    reply = HMessage.from_role_and_content(Role.ASSISTANT, "hello world").with_channel("final")
    full_tokens = _fixture_completion_tokens([reply])
    truncated_tokens = full_tokens[:-1]  # drop the trailing stop token: a "length" cutoff

    transport = _FakeTransport(
        {"token_ids": truncated_tokens, "finish_reason": "length", "stop_reason": None},
    )
    backend = HarmonyGptOssBackend(transport=transport)
    result = asyncio.run(
        backend.invoke(
            target(),
            (Message(role="user", content="hi"),),
            None,
            StructuredOutputMode.PROMPTED,
            max_output_tokens=8,
            timeout_s=30.0,
        ),
    )
    assert result.finish_reason == "length"


@pytest.mark.skipif(not HARMONY_INSTALLED, reason="requires the openai-harmony extra")
def test_a_default_constructed_backend_leaves_no_instance_state() -> None:
    """SPEC §12 item 47: `register_backend`'s `cls()` call must leave `vars(inst) == {}`."""
    from fleet.llm.backends.harmony_gpt_oss import HarmonyGptOssBackend

    assert vars(HarmonyGptOssBackend()) == {}
