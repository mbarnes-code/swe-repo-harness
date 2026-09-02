"""SPEC §12.42: "A new backend costs one file and one registry line."

This is that file, deliberately living OUTSIDE `src/fleet/` — `tests/fixtures/llm/`, not
`src/fleet/llm/backends/`. It is therefore NOT found by `llm.client.discover()`'s `pkgutil` walk
of the `fleet.llm.backends` package (`src/fleet/llm/client.py`, `discover()`): that walk only
ever sees production transports.

The registration mechanism is plain Python import machinery, not `discover()`. `register_backend`
(`src/fleet/llm/client.py`) is a decorator that runs at class-definition time — the moment this
module is imported, it writes `EchoBackend` straight into the module-level `_BACKENDS` dict.
`tests/test_llm_backend_fixture_e2e.py` proves this end to end: it imports this module (nothing
else), and after that import alone `client_module.registry()` already contains `"echo"` — no
`discover()` call, no file under `src/fleet/` touched or created.

Kept deliberately minimal: it does not parse or reason about `messages`, and it always answers
the same fixed, schema-valid text. Proving the registration → routing → invocation MECHANISM
needs nothing more than an echo; a backend that actually reasoned would be a second feature, not
a fixture.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from fleet.llm.client import (
    BackendReply,
    Message,
    StructuredOutputMode,
    register_backend,
)
from fleet.models.tasks import BackendTarget, ModelCapabilities, TokenUsage

#: What every `invoke()` call returns, verbatim. Fixed rather than input-derived: the point being
#: proved is registration and routing, not answer content, and the driving test picks a response
#: model this payload satisfies.
ECHO_REPLY_TEXT: str = '{"ok": true}'


@register_backend
class EchoBackend:
    """`ModelBackend` (`src/fleet/llm/client.py`), satisfied minimally. `declared_capabilities`
    claims JSON_SCHEMA so `negotiate()` picks the JSON_SCHEMA rung rather than PROMPTED — the
    reply text is JSON either way, but this keeps the negotiated mode the one a real structured
    backend would use.
    """

    name: ClassVar[str] = "echo"
    version: ClassVar[int] = 1

    def __init__(self) -> None:
        # `register_backend` instantiates this class exactly once (`_BACKENDS[cls.name] =
        # cls()`), so the one instance living in the registry accumulates every call across a
        # whole test run — which is what lets a driving test assert "invoke was called for
        # every role" by reading this list back out through `client_module.registry()["echo"]`.
        self.calls: list[str] = []

    def declared_capabilities(self, target: BackendTarget) -> ModelCapabilities:
        return ModelCapabilities(
            supports_json_schema=True,
            structured_output_modes=(StructuredOutputMode.JSON_SCHEMA,),
        )

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
        self.calls.append(messages[-1].content if messages else "")
        return BackendReply(
            text=ECHO_REPLY_TEXT,
            usage=TokenUsage(input_tokens=1, output_tokens=1),
            finish_reason="stop",
        )
