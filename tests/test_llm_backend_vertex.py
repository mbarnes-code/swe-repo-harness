"""SPEC §7.7 / ADR-0023 — the `vertex` backend adapter (`src/fleet/llm/backends/vertex.py`).

**Nothing here opens a socket and nothing here needs Application Default Credentials.**
`google-auth` arrives with the `vertex` extra in `pyproject.toml` and is not installed in the
harness venv, so the module is imported behind a minimal stub installed into `sys.modules` at the
top of this file. That is not a workaround: the adapter is written so that
`_AuthorizedSessionTransport` is the ONLY object that touches the SDK, which means the request
builder, the reply parser, the URL builder and the status classifier — every behaviour the module
actually owns — are exercised for real through a fake `RawPredictTransport`.

Coroutines are driven with `asyncio.run` rather than a plugin marker, matching `tests/conftest.py`
and `tests/test_llm_client.py`.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import pathlib
import subprocess
import sys
import types
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel

# ---------------------------------------------------------------------------------------------
# SDK stub — installed BEFORE the adapter is imported, and only when the real SDK is absent.
# ---------------------------------------------------------------------------------------------


def _absent(name: str) -> bool:
    """Is `name` genuinely not INSTALLED?

    `find_spec`, never `name in sys.modules`. The two answer different questions: `sys.modules`
    reports what has been imported SO FAR, so a stub guarded on it shadows a real package that is
    installed but simply not imported yet — session-wide, for every later test. That is the same
    wrong-probe shape as a stub whose arity matches the docs instead of the shipped class, and as
    reading `pyproject.toml` instead of the venv.

    `find_spec` raises rather than returning `None` when a PARENT package is absent, and `google`
    is a namespace package that may be absent entirely, so the probe is guarded.
    """
    try:
        return importlib.util.find_spec(name) is None
    except (ModuleNotFoundError, ValueError):
        return True


def _install_google_auth_stub() -> None:
    if _absent("google"):
        google_mod = types.ModuleType("google")
        google_mod.__path__ = []  # type: ignore[attr-defined]
        sys.modules["google"] = google_mod
    else:
        # A REAL `google` namespace package is installed (several unrelated libraries publish into
        # it). Import it rather than replacing it, so only the missing `auth` subpackage is stubbed
        # and nothing else under `google` is shadowed.
        google_mod = importlib.import_module("google")

    auth_mod = types.ModuleType("google.auth")
    auth_mod.__path__ = []  # type: ignore[attr-defined]

    def _default(**kwargs: object) -> tuple[object, str]:
        raise AssertionError(f"no test may resolve real ADC (kwargs={sorted(kwargs)})")

    auth_mod.default = _default  # type: ignore[attr-defined]

    transport_mod = types.ModuleType("google.auth.transport")
    transport_mod.__path__ = []  # type: ignore[attr-defined]
    requests_mod = types.ModuleType("google.auth.transport.requests")

    class _AuthorizedSession:
        def __init__(self, credentials: object) -> None:
            self.credentials = credentials

        def post(self, url: str, **kwargs: object) -> object:
            raise AssertionError(f"no test may POST to {url}")

    requests_mod.AuthorizedSession = _AuthorizedSession  # type: ignore[attr-defined]

    # `requests` is a hard dependency of `google.auth.transport.requests`, so it is present
    # exactly when that import succeeds. The adapter imports its exception BASE to keep the
    # transport catch from swallowing its own bugs, so the stub must supply a real class.
    if _absent("requests"):
        requests_pkg = types.ModuleType("requests")
        requests_pkg.__path__ = []  # type: ignore[attr-defined]
        exceptions_mod = types.ModuleType("requests.exceptions")

        class _RequestException(OSError):
            """Mirrors `requests.exceptions.RequestException` (which subclasses `IOError`)."""

        exceptions_mod.RequestException = _RequestException  # type: ignore[attr-defined]
        requests_pkg.exceptions = exceptions_mod  # type: ignore[attr-defined]
        sys.modules["requests"] = requests_pkg
        sys.modules["requests.exceptions"] = exceptions_mod

    google_mod.auth = auth_mod  # type: ignore[attr-defined]
    auth_mod.transport = transport_mod  # type: ignore[attr-defined]
    transport_mod.requests = requests_mod  # type: ignore[attr-defined]
    sys.modules.setdefault("google.auth", auth_mod)
    sys.modules.setdefault("google.auth.transport", transport_mod)
    sys.modules.setdefault("google.auth.transport.requests", requests_mod)


SDK_INSTALLED = not _absent("google.auth")
if not SDK_INSTALLED:
    _install_google_auth_stub()

from fleet.llm import client as client_module  # noqa: E402
from fleet.llm.backends import vertex as vertex_module  # noqa: E402
from fleet.llm.backends.vertex import (  # noqa: E402
    MissingRegion,
    UnmappedFinishReason,
    VertexBackend,
    VertexTargetMisconfigured,
    build_body,
    endpoint_url,
    parse_reply,
)
from fleet.llm.cache import CacheKeyParts  # noqa: E402
from fleet.llm.client import (  # noqa: E402
    Message,
    ModelBackend,
    TierUnavailable,
    TransportError,
)
from fleet.models.enums import ModelTier, StructuredOutputMode  # noqa: E402
from fleet.models.tasks import BackendTarget, ModelCapabilities, Price  # noqa: E402

REGION = "us-east5"
MODEL_ID = "model-under-test"
SCHEMA: dict[str, object] = {"type": "object", "properties": {"verdict": {"type": "string"}}}
TURNS = (Message(role="system", content="be terse"), Message(role="user", content="go"))


def target(**overrides: Any) -> BackendTarget:
    fields: dict[str, Any] = {
        "backend": "vertex",
        "model_id": MODEL_ID,
        "region": REGION,
        "price": Price(in_per_mtok=3.0, out_per_mtok=15.0),
    }
    fields.update(overrides)
    return BackendTarget(**fields)


def raw_predict_body(
    *,
    stop_reason: str = "end_turn",
    text: str | None = "ok",
    tool_input: Mapping[str, object] | None = None,
    served_model_id: str | None = None,
) -> dict[str, object]:
    """One `:rawPredict` response. `served_model_id` is the dated snapshot id this transport really
    returns in `model`, and the whole point of the cache regression below is that it must never
    reach `TokenUsage.model_id`."""
    content: list[dict[str, object]] = []
    if text is not None:
        content.append({"type": "text", "text": text})
    if tool_input is not None:
        content.append(
            {"type": "tool_use", "name": "emit_response", "input": dict(tool_input)},
        )
    body: dict[str, object] = {
        "type": "message",
        "role": "assistant",
        "content": content,
        "stop_reason": stop_reason,
        "usage": {"input_tokens": 11, "output_tokens": 7, "cache_read_input_tokens": 3},
    }
    if served_model_id is not None:
        body["model"] = served_model_id
    return body


class FakeTransport:
    """A `RawPredictTransport` that records the call and replays a scripted body. This is the seam
    that makes every test in this file socket-free."""

    def __init__(self, body: Mapping[str, object] | Exception) -> None:
        self._body = body
        self.calls: list[dict[str, object]] = []

    async def __call__(
        self,
        *,
        region: str,
        model_id: str,
        body: Mapping[str, object],
        timeout_s: float,
    ) -> Mapping[str, object]:
        self.calls.append(
            {"region": region, "model_id": model_id, "body": dict(body), "timeout_s": timeout_s},
        )
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def invoke(
    backend: VertexBackend,
    tgt: BackendTarget,
    *,
    schema: dict[str, object] | None = None,
    mode: StructuredOutputMode = StructuredOutputMode.PROMPTED,
    messages: Sequence[Message] = TURNS,
) -> Any:
    return asyncio.run(
        backend.invoke(tgt, messages, schema, mode, max_output_tokens=1024, timeout_s=30.0),
    )


# ---------------------------------------------------------------------------------------------
# Registration (§7.7 registry; `discover()` is the pkgutil walk)
# ---------------------------------------------------------------------------------------------


def test_discover_registers_vertex_when_the_sdk_imports() -> None:
    """The registry is populated by IMPORT, and `discover()` is the only thing that imports the
    package. Asserting on `discover()` rather than on `registry()` is the difference between
    proving the decorator ran and proving the pkgutil walk actually finds this file — a backend
    that registers only when a unit test imports it directly is the failure mode §13 row 36 turns
    into `UnknownBackend` on the first real call."""
    found = client_module.discover()
    assert VertexBackend.name in found
    assert isinstance(found[VertexBackend.name], VertexBackend)


def test_name_and_version_are_class_attributes() -> None:
    """`register_backend` reads `cls.name` BEFORE instantiating, and constructs with `cls()` — so
    both must exist at class-definition time and `__init__` must take no required argument. The
    SDK client is built lazily inside the transport, never injected through the constructor."""
    assert VertexBackend.name == "vertex"
    assert isinstance(VertexBackend.version, int)
    assert isinstance(VertexBackend(), ModelBackend)


def test_a_missing_sdk_leaves_the_backend_unregistered() -> None:
    """`discover()` catches `ImportError` and NOTHING else (client.py). The adapter's
    `import google.auth` is therefore deliberately unguarded at module scope: a host without the
    `vertex` extra must see the backend simply absent, not a half-registered adapter that fails on
    the first dispatch of a 250-repo run.

    Driven in a fresh interpreter with `google.auth` blocked, because the only honest way to test
    an import-time contract is at import time."""
    script = (
        "import sys\n"
        "class Block:\n"
        "    def find_module(self, name, path=None):\n"
        "        return None\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'google' or name.startswith('google.'):\n"
        "            raise ImportError('blocked')\n"
        "        return None\n"
        "sys.meta_path.insert(0, Block())\n"
        "from fleet.llm import client\n"
        "try:\n"
        "    import fleet.llm.backends.vertex\n"
        "    print('IMPORTED')\n"
        "except ImportError:\n"
        "    print('IMPORT_ERROR')\n"
        "print('vertex' in client.discover())\n"
    )
    out = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
        cwd=str(pathlib.Path(__file__).resolve().parents[1]),
    ).stdout.split()
    assert out[0] == "IMPORT_ERROR", out
    assert out[1] == "False", out


# ---------------------------------------------------------------------------------------------
# Target-field validation (§9 rule 2, §13 row 36)
# ---------------------------------------------------------------------------------------------


def test_a_target_with_no_region_names_the_missing_field() -> None:
    """§9 rule 2: `bedrock`/`vertex` refuse a target with no `region`. The backend owns the FIELD
    half of the message; the loader owns profile/tier/index, which no `ModelBackend` method is
    handed."""
    with pytest.raises(MissingRegion) as caught:
        VertexBackend().declared_capabilities(target(region=None))
    assert caught.value.field == "region"
    assert "region" in str(caught.value)
    assert MODEL_ID in str(caught.value)


def test_the_region_check_fires_before_any_transport_call() -> None:
    """Startup, not wave 7. A missing field must never reach the wire."""
    transport = FakeTransport(raw_predict_body())
    with pytest.raises(MissingRegion):
        invoke(VertexBackend(transport), target(region=None))
    assert transport.calls == []


def test_no_project_field_is_demanded_of_config() -> None:
    """The GCP project comes from ADC (`google.auth.default()` returns it alongside the
    credentials), which is why SPEC §9's shipped `vertex` target carries `region` and nothing
    else. §9 rule 4 forbids secret material in `config/models.yaml`, and a service-account JSON is
    exactly that — `redaction.patterns` even ships a `gcp_sa_key` pattern for it (§11.4)."""
    caps = VertexBackend().declared_capabilities(target())
    assert caps.supports_tools is True
    assert "project" not in BackendTarget.model_fields


# ---------------------------------------------------------------------------------------------
# Declared capabilities (§13 rows 36/37)
# ---------------------------------------------------------------------------------------------


def test_declared_capabilities_match_the_anthropic_failover_partner() -> None:
    """"Same models, different transport." A failover pair whose two halves declare different rungs
    would negotiate differently, so the operator's "same model, different transport" would quietly
    become "same model, different structured-output contract" the moment the primary went down."""
    caps = VertexBackend().declared_capabilities(target())
    assert caps.supports_tools is True
    assert caps.supports_json_schema is False
    assert caps.supports_constrained_decoding is False
    assert caps.supports_system_prompt is True
    assert caps.max_context > 0
    assert caps.max_output_tokens > 0


def test_the_two_capability_declarations_agree() -> None:
    """`negotiate()` reads the booleans; `promised_mode()` reads `structured_output_modes`. A gap
    between them is `CapabilityDrift` (§13 row 37) — so a HEALTHY target answering exactly as
    declared must not produce one."""
    caps = VertexBackend().declared_capabilities(target())
    assert client_module.negotiate(caps) is client_module.promised_mode(caps)
    assert StructuredOutputMode.PROMPTED in caps.structured_output_modes


def test_capabilities_override_is_merged_over_the_declaration() -> None:
    """"Declared in code per backend, merged with `capabilities_override`" (§7.7)."""
    backend = VertexBackend()
    tgt = target(capabilities_override={"max_output_tokens": 8192})
    merged = client_module.merge_capabilities(backend.declared_capabilities(tgt), tgt)
    assert merged.max_output_tokens == 8192
    assert backend.declared_capabilities(target()).max_output_tokens != 8192


def test_the_declaration_is_a_copy_not_shared_module_state() -> None:
    first = VertexBackend().declared_capabilities(target())
    second = VertexBackend().declared_capabilities(target())
    assert first is not second
    assert first == second
    assert isinstance(first, ModelCapabilities)


# ---------------------------------------------------------------------------------------------
# Request building
# ---------------------------------------------------------------------------------------------


def test_the_body_carries_the_version_discriminator_and_no_model_key() -> None:
    """On this transport the model is in the URL path; `anthropic_version` is the discriminator
    that replaces it in the body."""
    body = build_body(target(), TURNS, None, StructuredOutputMode.PROMPTED, 512)
    assert body["anthropic_version"]
    assert "model" not in body
    assert body["max_tokens"] == 512
    assert body["system"] == "be terse"
    assert body["messages"] == [{"role": "user", "content": "go"}]


def test_prompted_sends_no_structuring_field() -> None:
    """`client.py` passes `schema=None` under PROMPTED and renders the schema into the prompt
    itself. A backend must not second-guess that."""
    body = build_body(target(), TURNS, None, StructuredOutputMode.PROMPTED, 512)
    assert "tools" not in body
    assert "tool_choice" not in body


def test_tool_call_submits_the_schema_verbatim_and_forces_the_tool() -> None:
    body = build_body(target(), TURNS, SCHEMA, StructuredOutputMode.TOOL_CALL, 512)
    tools = body["tools"]
    assert isinstance(tools, list)
    assert tools[0]["input_schema"] == SCHEMA
    assert body["tool_choice"] == {"type": "tool", "name": tools[0]["name"]}


def test_no_strict_flag_is_asserted_over_a_schema_we_did_not_write() -> None:
    """The schema is whatever `response_model.model_json_schema()` produced. Strict/enforced
    subsets require every property in `required` and forbid the numeric and string constraints
    Pydantic emits — 9 of the 12 §9 roles violate the subset across all three tiers, and strict
    applies to the whole schema TREE, not just the root. Omitted rather than set false: an absent
    key is the honest statement that we make no claim about a schema we do not control."""
    body = build_body(target(), TURNS, SCHEMA, StructuredOutputMode.TOOL_CALL, 512)
    assert "strict" not in repr(body)
    assert "additionalProperties" not in repr(body)


def test_an_undeclared_rung_is_refused_loudly() -> None:
    """`_DECLARED` claims neither JSON_SCHEMA nor CONSTRAINED, so `negotiate()` reaches them only
    through a `capabilities_override` that is simply false (Rule 11)."""
    for mode in (StructuredOutputMode.JSON_SCHEMA, StructuredOutputMode.CONSTRAINED):
        with pytest.raises(VertexTargetMisconfigured) as caught:
            build_body(target(), TURNS, SCHEMA, mode, 512)
        assert caught.value.field == "capabilities_override"


def test_effort_is_omitted_when_the_target_declares_none() -> None:
    """A sibling lane is making `BackendTarget.effort` optional (`str | None`), where `None` means
    "do not send the parameter" — the current non-optional `"medium"` default transmits a value the
    operator never wrote. The omit-on-`None` branch is implemented and asserted here NOW so it is
    correct the moment that lands; today's `BackendTarget` cannot hold `None`, so the builder is
    driven with a stand-in target object rather than by weakening the model."""

    class NoEffortTarget:
        backend = "vertex"
        model_id = MODEL_ID
        region = REGION
        effort = None

    body = build_body(
        NoEffortTarget(),  # type: ignore[arg-type]
        TURNS,
        None,
        StructuredOutputMode.PROMPTED,
        512,
    )
    assert "output_config" not in body


def test_effort_is_sent_when_the_target_declares_it() -> None:
    """`effort` is already part of the `llm_cache` key (`CacheKeyParts.effort`), so a cached row
    claims the call was made at the declared effort — discarding it would make that claim false."""
    body = build_body(target(effort="high"), TURNS, None, StructuredOutputMode.PROMPTED, 512)
    assert body["output_config"] == {"effort": "high"}


def test_a_conversation_with_no_user_turn_is_refused() -> None:
    with pytest.raises(VertexTargetMisconfigured):
        build_body(
            target(),
            (Message(role="system", content="only a system turn"),),
            None,
            StructuredOutputMode.PROMPTED,
            512,
        )


def test_a_conversation_whose_first_non_system_turn_is_not_user_is_refused() -> None:
    """A DIFFERENT guard than the one above: this turn list is non-empty (it survives `_render`'s
    "no turns at all" check) but opens on 'assistant', which this transport requires to be 'user'.
    Sending it anyway would have the endpoint reject a well-formed-looking body outright, instead of
    failing loudly here where the field can be named (Rule 11)."""
    with pytest.raises(VertexTargetMisconfigured, match="assistant"):
        build_body(
            target(),
            (
                Message(role="assistant", content="premature"),
                Message(role="user", content="go"),
            ),
            None,
            StructuredOutputMode.PROMPTED,
            512,
        )


# ---------------------------------------------------------------------------------------------
# Endpoint construction
# ---------------------------------------------------------------------------------------------


def test_the_endpoint_is_derived_from_the_region_not_hard_coded() -> None:
    url = endpoint_url(REGION, "some-project", MODEL_ID)
    assert url.startswith(f"https://{REGION}-aiplatform.googleapis.com/")
    assert f"/locations/{REGION}/" in url
    assert url.endswith(f"/{MODEL_ID}:rawPredict")


def test_the_global_alias_carries_no_location_prefix() -> None:
    """`global` is the documented multi-region alias and is the single host with no prefix."""
    url = endpoint_url("global", "some-project", MODEL_ID)
    assert url.startswith("https://aiplatform.googleapis.com/")
    assert "/locations/global/" in url


# ---------------------------------------------------------------------------------------------
# Finish-reason mapping (§13 row 47)
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stop_reason", "expected"),
    [
        ("end_turn", "stop"),
        ("stop_sequence", "stop"),
        ("max_tokens", "length"),
        ("model_context_window_exceeded", "length"),
        ("tool_use", "tool_call"),
        ("refusal", "refusal"),
    ],
)
def test_finish_reason_mapping(stop_reason: str, expected: str) -> None:
    reply = parse_reply(raw_predict_body(stop_reason=stop_reason), target())
    assert reply.finish_reason == expected


def test_truncation_is_length_and_length_is_not_a_failover_trigger() -> None:
    """§13 row 47's whole content. `length` must be distinguishable from a schema failure: a
    truncated reply is retried on the SAME target with a raised cap, spending no repair and no
    failover, while a schema failure spends the repair budget and then fails the target over.
    Collapsing the two would fail one oversized call across three tiers to reproduce a single
    truncation, and this asserts the type system agrees — `length` is not a `FailoverTrigger`."""
    reply = parse_reply(raw_predict_body(stop_reason="max_tokens"), target())
    assert reply.finish_reason == "length"
    triggers = client_module.FailoverTrigger.__args__  # type: ignore[attr-defined]
    assert "length" not in triggers
    assert "LENGTH" not in triggers


def test_pause_turn_is_not_silently_mapped() -> None:
    """`pause_turn` means a server-side tool loop paused mid-turn. This adapter declares no
    server-side tools, so seeing it means the request was not the one we built — raised, never
    given a rung-visible reason it did not earn."""
    with pytest.raises(UnmappedFinishReason):
        parse_reply(raw_predict_body(stop_reason="pause_turn"), target())


def test_an_unmapped_stop_reason_is_raised_never_guessed() -> None:
    with pytest.raises(UnmappedFinishReason):
        parse_reply(raw_predict_body(stop_reason="not-a-real-stop-reason"), target())


class _Answer(BaseModel):
    verdict: str


class _RaisingBackend:
    """A `ModelBackend` whose `invoke` raises, so the CLIENT's handling is what gets measured."""

    name: ClassVar[str] = "vertex"
    version: ClassVar[int] = 1

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def declared_capabilities(self, target: BackendTarget) -> ModelCapabilities:
        del target
        return ModelCapabilities()

    async def invoke(self, *args: object, **kwargs: object) -> Any:
        del args, kwargs
        raise self._exc


class _TwoTargetRouter:
    def __init__(self, targets: Sequence[BackendTarget]) -> None:
        self._route = client_module.TierRoute(tier=ModelTier.HEAVY, targets=tuple(targets))

    def resolve(self, role: str, *, tier_override: object = None) -> object:
        del role, tier_override
        return self._route


def _complete_with(exc: Exception) -> None:
    backend = _RaisingBackend(exc)
    client = client_module.LadderModelClient(
        _TwoTargetRouter([target(), target()]),  # type: ignore[arg-type]
        {_RaisingBackend.name: backend},  # type: ignore[dict-item]
    )
    asyncio.run(
        client.complete("transform_repair", [Message(role="user", content="x")], _Answer),
    )


def test_an_unmapped_finish_reason_propagates_out_of_the_client_untouched() -> None:
    """**Asserts `client.py`'s real handling, not a subclass relation.**

    The previous version asserted `UnmappedFinishReason` is a subclass of neither
    `MalformedReply` nor `TransportError` — true, but it would stay green if `client.py`'s repair
    catch ever widened to `except LlmError`, which is the change that would actually break the
    contract the name claims. So the real `LadderModelClient` is driven with a backend that raises
    it, and the exception is asserted to arrive at the caller AS ITSELF: not repaired (the repair
    catch only wraps `_validate`, which an exception from `invoke` never reaches), and not failed
    over (`complete`'s failover catch is `(SchemaUnsatisfied, TransportError)` only).

    Two targets are configured deliberately — if the client treated this as a failover trigger it
    would try the second and raise `TierUnavailable`, so a green result here means the very first
    failure reached the caller.
    """
    with pytest.raises(UnmappedFinishReason):
        _complete_with(UnmappedFinishReason("stop reason this adapter does not know"))


def test_the_control_a_real_failover_trigger_does_walk_the_tier() -> None:
    """The positive control that makes the test above meaningful. Without it, "the exception
    propagated" could equally mean the client never got as far as its failover logic. A
    `TransportError` over the same two targets is caught, walks both, and ends in
    `TierUnavailable` — so the harness demonstrably WOULD have intercepted a trigger-shaped
    failure, and did not intercept the one above."""
    with pytest.raises(TierUnavailable):
        _complete_with(TransportError("endpoint sick", trigger="SERVER_ERROR"))


# ---------------------------------------------------------------------------------------------
# Reply parsing
# ---------------------------------------------------------------------------------------------


def test_tool_arguments_are_delivered_as_an_object_not_scraped_from_text() -> None:
    reply = parse_reply(
        raw_predict_body(stop_reason="tool_use", text=None, tool_input={"verdict": "ok"}),
        target(),
    )
    assert reply.tool_arguments == {"verdict": "ok"}
    assert reply.text is None


def test_a_missing_usage_block_reads_as_zero_rather_than_crashing() -> None:
    """`TokenUsage` bounds its counters `ge=0`. Under-reporting is visible in the ledger; a
    `ValidationError` on an otherwise good reply loses the answer."""
    body = raw_predict_body()
    del body["usage"]
    reply = parse_reply(body, target())
    assert reply.usage.input_tokens == 0
    assert reply.usage.output_tokens == 0


def test_cost_is_not_guessed_by_the_backend() -> None:
    """Pricing is the target's declaration and `_stamp` applies it: a $0.00 call comes from
    `price: free`, never from a backend inventing a number (§11.2, §9 rule 5)."""
    assert parse_reply(raw_predict_body(), target()).usage.cost_usd == 0.0


# ---------------------------------------------------------------------------------------------
# THE cache-key regression (§11.6, §13 row 39)
# ---------------------------------------------------------------------------------------------


def test_usage_model_id_echoes_the_config_string_even_when_vertex_reports_another() -> None:
    """**The highest-consequence assertion in this file.**

    `_stamp` resolves `usage.model_id or target.model_id` — backend-reported WINS. The `llm_cache`
    READ key is built from the config string (`_key_parts`, from `route.targets[0].model_id`) while
    the WRITE key is built from `usage.model_id` (`_store_response`). This transport returns a
    dated snapshot id in `model`, and the "resolution" language around this field invited passing
    that through in three independent lanes. Doing so makes read key != write key on EVERY call:
    a permanent, total cache miss across the whole fleet, silent and indistinguishable from a cold
    cache because `attempts.llm_cache_hit` simply stays 0. The invariant is pinned by this test
    rather than left to whatever the neighbouring prose says.

    So the transport is faked reporting a DIFFERENT model name, and the two real `CacheKeyParts`
    are computed and compared.
    """
    served = "some-model-20260401"
    tgt = target()
    transport = FakeTransport(raw_predict_body(served_model_id=served))
    reply = invoke(VertexBackend(transport), tgt)

    assert reply.usage.model_id == tgt.model_id
    assert reply.usage.model_id != served
    assert reply.usage.backend == VertexBackend.name

    common: dict[str, Any] = {
        "role": "transform_repair",
        "tier": ModelTier.WORKHORSE,
        "backend": tgt.backend,
        "effort": tgt.effort,
        "prompt_sha256": "0" * 64,
        "response_schema_sha256": "1" * 64,
    }
    read_key = CacheKeyParts(model_id=tgt.model_id, **common).compute()
    write_key = CacheKeyParts(model_id=reply.usage.model_id or tgt.model_id, **common).compute()
    poisoned = CacheKeyParts(model_id=served, **common).compute()
    assert read_key == write_key
    assert read_key != poisoned


def test_the_target_model_id_is_what_reaches_the_url_not_a_served_name() -> None:
    """The same discipline one layer down: the transport is addressed with the CONFIG's model id."""
    tgt = target()
    transport = FakeTransport(raw_predict_body(served_model_id="something-else"))
    invoke(VertexBackend(transport), tgt)
    assert transport.calls[0]["model_id"] == tgt.model_id
    assert transport.calls[0]["region"] == REGION


# ---------------------------------------------------------------------------------------------
# Status classification (§11.8 triggers)
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "trigger"),
    [(429, "RATE_LIMIT"), (500, "SERVER_ERROR"), (503, "SERVER_ERROR")],
)
def test_transient_statuses_become_failover_triggers(status: int, trigger: str) -> None:
    """§11.8 triggers 2-3. A rate limit keeps its own name: §13 row 43 turns on a 429 alone never
    being readable as an outage, which only holds if the two arrive under different triggers."""
    error = vertex_module._from_status(REGION, status, "detail")
    assert isinstance(error, TransportError)
    assert error.trigger == trigger


@pytest.mark.parametrize("status", [400, 403, 404])
def test_a_bad_request_fails_the_task_instead_of_walking_the_tier(status: int) -> None:
    """A 4xx that is not a 429 is OUR request being wrong. The next target reproduces it exactly,
    so failing loudly beats spending the tier's whole ladder (Rule 11, §11.8)."""
    error = vertex_module._from_status(REGION, status, "detail")
    assert not isinstance(error, TransportError)
    assert str(status) in str(error)


# ---------------------------------------------------------------------------------------------
# The session catch is narrow (a bug must not masquerade as an outage)
# ---------------------------------------------------------------------------------------------


class _FakeSession:
    def __init__(self, outcome: object) -> None:
        self._outcome = outcome

    def post(self, url: str, **kwargs: object) -> object:
        del url, kwargs
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


def _transport(outcome: object) -> Any:
    return vertex_module._AuthorizedSessionTransport(
        session=_FakeSession(outcome),
        project="some-project",
    )


def test_a_requests_transport_fault_becomes_a_connection_failover() -> None:
    """§11.8 trigger 1. `RequestException` is the common base of connection-refused, read-timeout,
    SSL and chunked-encoding faults — the next target may be a different region entirely."""
    from requests.exceptions import RequestException

    with pytest.raises(TransportError) as caught:
        asyncio.run(
            _transport(RequestException("connection refused"))(
                region=REGION, model_id=MODEL_ID, body={}, timeout_s=1.0,
            ),
        )
    assert caught.value.trigger == "CONNECTION"


@pytest.mark.parametrize("boom", [TypeError("bad body"), AttributeError("renamed member")])
def test_an_adapter_bug_is_not_relabelled_as_a_transport_failure(boom: Exception) -> None:
    """**The reason the catch is `RequestException` and not `Exception`.**

    A `TypeError` in the body this adapter just built, or an `AttributeError` on a renamed SDK
    member, is a bug in THIS module. Catching it as a `CONNECTION` trigger would have §11.8 walk
    the entire tier reproducing it and then report an outage that never happened — a fault no
    endpoint has, contaminating the §13 rows 40/43 health signals. It must surface as itself
    (Rule 11).
    """
    with pytest.raises(type(boom)):
        asyncio.run(
            _transport(boom)(region=REGION, model_id=MODEL_ID, body={}, timeout_s=1.0),
        )


# ---------------------------------------------------------------------------------------------
# Boundary hygiene
# ---------------------------------------------------------------------------------------------


def test_no_model_id_is_hard_coded_in_the_adapter() -> None:
    """§12.40: `config/models.yaml` is the only place a model id may appear. The adapter's model
    string comes off the `BackendTarget` and nothing else. The host is DERIVED from the target's
    region (asserted above), which is why a `googleapis.com` literal is legitimate here and a
    model id is not."""
    source = vertex_module.__file__
    assert source is not None
    text = pathlib.Path(source).read_text(encoding="utf-8")
    body = text.split('"""', 2)[-1]  # drop the module docstring, which quotes SPEC prose
    for forbidden in ("claude-", "gpt-", "gemini-"):
        assert forbidden not in body, f"{forbidden!r} is hard-coded in vertex.py"
