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

from typing import ClassVar, Final

from openai_harmony import (  # noqa: F401  (re-exported names used by Task 4/5)
    Author,
    Conversation,
    DeveloperContent,
    HarmonyEncodingName,
    Role,
    SystemContent,
    ToolDescription,
    load_harmony_encoding,
)
from openai_harmony import (
    Message as HarmonyMessage,  # noqa: F401  (re-exported name used by Task 4/5)
)

from fleet.llm.client import (
    BackendReply,
    FinishReason,  # noqa: F401  (unused until Task 4/6 wire invoke(); see module docstring)
    LlmError,
    Message,  # noqa: F401  (unused until Task 4/6 wire invoke(); see module docstring)
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
