"""SPEC §7.7 / ADR-0023 — the model-agnostic LLM boundary (`src/fleet/llm/client.py`).

Every test drives a FAKE `ModelBackend`. Nothing here opens a socket, and nothing here imports a
vendor SDK — which is the property the last test asserts mechanically rather than by convention.

Coroutines are driven with `asyncio.run` rather than a plugin marker, matching `tests/conftest.py`:
the LLM boundary must stay testable in a bare `pydantic` + `pytest` environment.
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from itertools import pairwise
from pathlib import Path
from typing import ClassVar

import pytest
from pydantic import BaseModel

from fleet.llm import client as client_module
from fleet.llm.client import (
    BackendFailover,
    BackendReply,
    BudgetExhausted,
    CallBudget,
    CallPolicy,
    CapabilityDrift,
    LadderModelClient,
    Message,
    ModelResponse,
    StructuredOutputMode,
    TierUnavailable,
    TransportError,
    UnknownRole,
    UnpricedTarget,
    estimate_cost_usd,
)
from fleet.models.enums import ModelTier
from fleet.models.tasks import BackendTarget, ModelCapabilities, Price, TokenUsage

ROLE = "transform_repair"


class Answer(BaseModel):
    """The typed handoff. Nothing leaves the boundary that has not validated into one of these."""

    verdict: str
    score: int


# ---------------------------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------------------------


class FakeBackend:
    """One transport, scripted. Records every turn so a test can assert WHICH target was called
    and with what token cap — the truncation test is meaningless without that record."""

    name: ClassVar[str] = "fake"
    version: ClassVar[int] = 1

    def __init__(
        self,
        script: Sequence[BackendReply | Exception],
        caps: ModelCapabilities | None = None,
        *,
        gate: asyncio.Event | None = None,
    ) -> None:
        self._script = list(script)
        self._caps = caps or ModelCapabilities(supports_json_schema=True, max_output_tokens=8192)
        self._gate = gate
        self.calls: list[dict[str, object]] = []

    def declared_capabilities(self, target: BackendTarget) -> ModelCapabilities:
        return self._caps

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
        self.calls.append(
            {
                "model_id": target.model_id,
                "messages": list(messages),
                "schema": schema,
                "mode": mode,
                "max_output_tokens": max_output_tokens,
            },
        )
        if self._gate is not None:
            await self._gate.wait()
        if not self._script:
            raise AssertionError("fake backend called more times than the script allows")
        nxt = self._script.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


class FakeRouter:
    def __init__(self, tier: ModelTier, targets: Sequence[BackendTarget]) -> None:
        self._route = client_module.TierRoute(tier=tier, targets=tuple(targets))

    def resolve(
        self,
        role: str,
        *,
        tier_override: ModelTier | None = None,
    ) -> client_module.TierRoute:
        if role != ROLE:
            raise UnknownRole(role)
        return self._route


def target(model_id: str, *, in_price: float = 1.0, out_price: float = 2.0) -> BackendTarget:
    return BackendTarget(
        backend="fake",
        model_id=model_id,
        price=Price(in_per_mtok=in_price, out_per_mtok=out_price),
    )


def free_target(model_id: str) -> BackendTarget:
    return BackendTarget(backend="fake", model_id=model_id, price="free")


def ok_reply(*, output_tokens: int = 40) -> BackendReply:
    return BackendReply(
        text=json.dumps({"verdict": "migrate", "score": 7}),
        usage=TokenUsage(input_tokens=100, output_tokens=output_tokens),
        finish_reason="stop",
    )


def truncated_reply() -> BackendReply:
    return BackendReply(
        text='{"verdict": "mig',
        usage=TokenUsage(input_tokens=100, output_tokens=1024),
        finish_reason="length",
    )


def invalid_reply() -> BackendReply:
    """Schema-valid JSON, model-invalid content: `score` is not an int. A genuine violation."""
    return BackendReply(
        text=json.dumps({"verdict": "migrate", "score": "not-an-int"}),
        usage=TokenUsage(input_tokens=100, output_tokens=20),
        finish_reason="stop",
    )


def build(
    backend: FakeBackend,
    targets: Sequence[BackendTarget],
    *,
    policy: CallPolicy | None = None,
    drifts: list[CapabilityDrift] | None = None,
    failovers: list[BackendFailover] | None = None,
    clock: float = 0.0,
) -> LadderModelClient:
    return LadderModelClient(
        FakeRouter(ModelTier.WORKHORSE, targets),
        {"fake": backend},
        policy=policy or CallPolicy(),
        on_drift=None if drifts is None else drifts.append,
        on_failover=None if failovers is None else failovers.append,
        clock=lambda: clock,
    )


def call(client: LadderModelClient, **kwargs: object) -> ModelResponse[Answer]:
    messages = [Message(role="user", content="does this repo migrate?")]
    return asyncio.run(client.complete(ROLE, messages, Answer, **kwargs))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------------------------


def test_valid_reply_is_returned_as_a_validated_typed_response() -> None:
    """WHY: the boundary's whole promise is that no unvalidated payload escapes it. A caller that
    received a dict would have to re-validate, and every caller would do it differently."""
    backend = FakeBackend([ok_reply()])
    response = call(build(backend, [target("m1")]))

    assert isinstance(response.value, Answer)
    assert response.value.score == 7
    assert response.mode is StructuredOutputMode.JSON_SCHEMA
    assert response.finish_reason == "stop"
    assert response.repairs == 0
    # The usage is attributed to the target that actually answered and priced from its own rate,
    # because §11.2 writes that row in four places and must not reconstruct the caller.
    assert response.usage.backend == "fake"
    assert response.usage.model_id == "m1"
    assert response.usage.tier is ModelTier.WORKHORSE
    assert response.usage.cost_usd == pytest.approx((1.0 * 100 + 2.0 * 40) / 1e6)


def test_truncation_retries_the_same_target_and_never_fails_over() -> None:
    """WHY (§13 row 47 — the regression this file exists for): `finish_reason == "length"` used to
    be misdiagnosed as a schema violation, so the identical oversized call was re-issued across
    three backends to reproduce one truncation and libelled three healthy targets with
    `CapabilityDrift`. Truncation is a property of the REQUEST: same target, raised cap, no repair
    spent, zero failovers, zero drift findings."""
    backend = FakeBackend([truncated_reply(), ok_reply()])
    failovers: list[BackendFailover] = []
    drifts: list[CapabilityDrift] = []
    client = build(
        backend,
        [target("m1"), target("m2"), target("m3")],  # a ladder that must NOT be walked
        failovers=failovers,
        drifts=drifts,
    )

    response = call(client, max_output_tokens=1024)

    assert len(backend.calls) == 2
    assert [c["model_id"] for c in backend.calls] == ["m1", "m1"]  # the SAME target, twice
    assert failovers == []  # the failover counter is zero
    assert drifts == []  # truncation never counts toward CapabilityDrift
    assert response.repairs == 0  # and never spends a repair
    first, second = (int(c["max_output_tokens"]) for c in backend.calls)  # type: ignore[call-overload]
    assert second > first == 1024  # retried with a RAISED cap, not the identical request


def test_exhausting_the_truncation_raise_is_a_budget_error_not_a_backend_fault() -> None:
    """WHY: when the cap cannot be raised further the request was oversized — that is
    `FailureClass.BUDGET_EXHAUSTED`, not evidence against the endpoint, so it still must not fail
    over and must still name the truncation as its cause."""
    caps = ModelCapabilities(supports_json_schema=True, max_output_tokens=1024)
    backend = FakeBackend([truncated_reply()], caps)
    failovers: list[BackendFailover] = []
    client = build(backend, [target("m1"), target("m2")], failovers=failovers)

    with pytest.raises(BudgetExhausted) as excinfo:
        call(client, max_output_tokens=1024)

    assert isinstance(excinfo.value.__cause__, client_module.OutputTruncated)
    assert failovers == []
    assert len(backend.calls) == 1


def test_schema_violation_buys_one_repair_then_fails_the_target_over() -> None:
    """WHY: a model that cannot produce the schema at all should be routed AROUND, not re-asked
    into the ledger — and this path must stay visibly distinct from truncation, which does the
    opposite. Here the failover counter is 1 and the trigger is named."""
    backend = FakeBackend([invalid_reply(), invalid_reply(), ok_reply()])
    failovers: list[BackendFailover] = []
    client = build(backend, [target("m1"), target("m2")], failovers=failovers)

    response = call(client)

    # target m1: initial + exactly one repair (llm.max_schema_repairs default 1), then failover.
    assert [c["model_id"] for c in backend.calls] == ["m1", "m1", "m2"]
    assert len(failovers) == 1
    assert failovers[0].trigger == "SCHEMA_UNSATISFIED"
    assert (failovers[0].from_model_id, failovers[0].to_model_id) == ("m1", "m2")
    assert response.usage.model_id == "m2"
    # ADR-0107, §12.43(i): one hop was spent inside THIS call before it succeeded on m2.
    assert response.usage.llm_failovers == 1


def test_a_transport_failover_also_counts_as_one_hop_on_the_returned_usage() -> None:
    """WHY (§12.43 case (i)'s exact shape): a CONNECTION-triggered failover must count the same
    as a SCHEMA_UNSATISFIED one — `llm_failovers` is about backend hops, not about which of the
    two failover triggers caused them."""
    backend = FakeBackend([TransportError("connection refused"), ok_reply()])
    failovers: list[BackendFailover] = []
    client = build(backend, [target("m1"), target("m2")], failovers=failovers)

    response = call(client)

    assert [c["model_id"] for c in backend.calls] == ["m1", "m2"]
    assert len(failovers) == 1
    assert failovers[0].trigger == "CONNECTION"
    assert response.usage.backend == "fake"
    assert response.usage.model_id == "m2"
    assert response.usage.llm_failovers == 1


def test_a_repair_that_succeeds_is_counted_and_carries_the_validator_error_verbatim() -> None:
    """WHY: `ModelResponse.repairs > 0` is the §13 row 37 drift signal, and guardrail 5 requires
    the re-ask to carry fresh verbatim error text rather than a transcript."""
    backend = FakeBackend([invalid_reply(), ok_reply()])

    response = call(build(backend, [target("m1")]))

    assert response.repairs == 1
    repair_turns: list[Message] = backend.calls[1]["messages"]  # type: ignore[assignment]
    assert repair_turns[-1].role == "user"
    assert "not-an-int" in repair_turns[-1].content or "int_parsing" in repair_turns[-1].content


def test_budget_exhausted_raises_before_the_backend_is_invoked() -> None:
    """WHY (§11.2): the ceiling used to be observable only after the money was spent. A budget
    checked after dispatch is a budget that has already been broken — so the fake must record
    ZERO calls."""
    backend = FakeBackend([ok_reply()])
    client = build(backend, [target("m1", in_price=1000.0, out_price=1000.0)])

    with pytest.raises(BudgetExhausted):
        call(
            client,
            budget=CallBudget(remaining_tokens=1_000_000, remaining_usd=0.000_001, deadline=99.0),
        )

    assert backend.calls == []


def test_a_passed_deadline_refuses_before_dispatch() -> None:
    """WHY: the deadline is on the same clock as the worker lease; dispatching past it spends money
    on a result the runner has already stopped waiting for."""
    backend = FakeBackend([ok_reply()])
    client = build(backend, [free_target("m1")], clock=100.0)

    with pytest.raises(BudgetExhausted):
        call(client, budget=CallBudget(remaining_tokens=10_000, remaining_usd=5.0, deadline=99.0))

    assert backend.calls == []


def test_budget_is_rechecked_against_each_failover_target() -> None:
    """WHY (§7.7): the next target in a tier may be DEARER than the one that just failed, so a
    budget cleared once is not a budget cleared for the ladder. m1 is free and dies on transport;
    m2 is expensive and must be refused BEFORE it is dialled."""
    backend = FakeBackend([TransportError("connection refused"), ok_reply()])
    client = build(backend, [free_target("m1"), target("m2", in_price=9e5, out_price=9e5)])

    with pytest.raises(BudgetExhausted) as excinfo:
        call(client, budget=CallBudget(remaining_tokens=100_000, remaining_usd=0.01, deadline=9e9))

    assert excinfo.value.target is not None
    assert excinfo.value.target.model_id == "m2"
    assert [c["model_id"] for c in backend.calls] == ["m1"]  # m2 was never invoked


def test_tool_role_messages_round_trip_through_the_boundary() -> None:
    """WHY (§7.7): the TOOL_CALL rung is a two-turn protocol — the reply's arguments OBJECT has to
    travel back in as a turn. Without the `tool` member it would be re-flattened into a user
    message, which is the exact shape the negotiator refuses to record as PROMPTED."""
    caps = ModelCapabilities(supports_tools=True, max_output_tokens=8192)
    bad = BackendReply(
        tool_arguments={"verdict": "migrate", "score": "not-an-int"},
        usage=TokenUsage(input_tokens=10, output_tokens=5),
        finish_reason="tool_call",
    )
    good = BackendReply(
        tool_arguments={"verdict": "migrate", "score": 7},
        usage=TokenUsage(input_tokens=10, output_tokens=5),
        finish_reason="tool_call",
    )
    backend = FakeBackend([bad, good], caps)
    client = build(backend, [target("m1")])

    inbound = [
        Message(role="user", content="does this repo migrate?"),
        Message(role="tool", content='{"probe": "ok"}'),  # a caller-supplied tool result
    ]
    response = asyncio.run(client.complete(ROLE, inbound, Answer))

    assert response.mode is StructuredOutputMode.TOOL_CALL
    assert response.value == Answer(verdict="migrate", score=7)
    # The caller's tool turn survived the trip out...
    first_turns: list[Message] = backend.calls[0]["messages"]  # type: ignore[assignment]
    assert any(m.role == "tool" and "probe" in m.content for m in first_turns)
    # ...and the rejected arguments object travelled back in as a `tool` turn, not as user text.
    repair_turns: list[Message] = backend.calls[1]["messages"]  # type: ignore[assignment]
    echoed = [m for m in repair_turns if m.role == "tool" and "not-an-int" in m.content]
    assert len(echoed) == 1


def test_stream_yields_progress_events_without_a_completed_response() -> None:
    """WHY (§11.5): a 900 s HEAVY call emitted no heartbeat, so the liveness reaper killed a
    healthy worker. The first event must arrive BEFORE the call can have finished, counts must be
    cumulative and monotonic, and no partial text may be exposed."""

    async def drive() -> list[client_module.StreamEvent]:
        gate = asyncio.Event()
        backend = FakeBackend([ok_reply(output_tokens=40)], gate=gate)
        client = build(backend, [target("m1")], policy=CallPolicy(heartbeat_interval_s=0.0))
        events: list[client_module.StreamEvent] = []
        stream = client.stream(ROLE, [Message(role="user", content="hi")], Answer)
        async for event in stream:
            events.append(event)
            if len(events) == 3:  # proof of liveness while the call is still in flight
                gate.set()
        return events

    events = asyncio.run(drive())

    assert len(events) >= 4
    assert events[0].output_tokens == 0  # progress, not content
    assert all(a.output_tokens <= b.output_tokens for a, b in pairwise(events))
    assert events[-1].output_tokens == 40
    assert not any(hasattr(e, "text") for e in events)


def test_every_target_failing_is_tier_unavailable_not_a_silent_downgrade() -> None:
    """WHY (§11.8): failing closed is the point — a HEAVY call quietly served by another tier is
    exactly the substitution the cache key exists to prevent."""
    backend = FakeBackend([TransportError("refused"), TransportError("refused")])
    failovers: list[BackendFailover] = []
    client = build(backend, [target("m1"), target("m2")], failovers=failovers)

    with pytest.raises(TierUnavailable) as excinfo:
        call(client)

    assert excinfo.value.targets_tried == ("fake:m1", "fake:m2")
    assert len(failovers) == 1  # one hop; the last target has nowhere to fail over to


def test_a_rung_below_the_declared_promise_is_capability_drift() -> None:
    """WHY (§13 row 37): a local server that silently drops guided-JSON support must show up as a
    finding, not as a slow rise in repair counts."""
    caps = ModelCapabilities(
        supports_json_schema=False,
        structured_output_modes=(StructuredOutputMode.JSON_SCHEMA, StructuredOutputMode.PROMPTED),
    )
    backend = FakeBackend([ok_reply()], caps)
    drifts: list[CapabilityDrift] = []
    response = call(build(backend, [target("m1")], drifts=drifts))

    assert response.mode is StructuredOutputMode.PROMPTED
    assert len(drifts) == 1
    assert drifts[0].promised is StructuredOutputMode.JSON_SCHEMA
    assert drifts[0].actual is StructuredOutputMode.PROMPTED
    # PROMPTED renders the schema into the prompt; the backend is handed no schema argument.
    assert backend.calls[0]["schema"] is None


def test_an_unpriced_target_is_a_config_error_not_a_free_call() -> None:
    """WHY (§9 rule 5): a hosted target nobody priced would price a 250-repo run at $0.00 and let
    the cost assertions pass while the invoice arrived anyway. `model_construct` stands in for a
    loader path that bypassed validation."""
    unpriced = BackendTarget.model_construct(backend="fake", model_id="m1", price=None)

    with pytest.raises(UnpricedTarget):
        estimate_cost_usd(unpriced, 100, 100)

    assert estimate_cost_usd(free_target("m1"), 1_000_000, 1_000_000) == 0.0


def test_register_backend_refuses_a_duplicate_name() -> None:
    """WHY (§7.7 / §12.42): "a new backend costs one file + one registry line" only holds if a
    SECOND file claiming an already-taken line fails LOUD at import time — otherwise whichever
    backend happens to import last silently wins the name and the other's `BackendTarget.backend`
    references dispatch to the wrong transport with no error anywhere.

    Two freshly-defined decoy classes sharing one synthetic name that is not a shipped backend's,
    never a real name collision, so this cannot perturb `discover()`'s live registry state for any
    other test in the session — and the entry is removed again in `finally` regardless of outcome,
    same reasoning as the worktree-registry isolation CLAUDE.md requires of concurrent lanes."""
    decoy_name = "duplicate_decoy_for_register_backend_test"
    assert decoy_name not in client_module.registry()  # never a shipped backend's name

    class FirstDecoy:
        name: ClassVar[str] = decoy_name
        version: ClassVar[int] = 1

        def declared_capabilities(self, target: BackendTarget) -> ModelCapabilities:
            raise NotImplementedError

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
            raise NotImplementedError

    class SecondDecoy:
        name: ClassVar[str] = decoy_name
        version: ClassVar[int] = 1

        def declared_capabilities(self, target: BackendTarget) -> ModelCapabilities:
            raise NotImplementedError

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
            raise NotImplementedError

    try:
        client_module.register_backend(FirstDecoy)
        assert decoy_name in client_module.registry()

        with pytest.raises(RuntimeError) as excinfo:
            client_module.register_backend(SecondDecoy)
        assert decoy_name in str(excinfo.value)

        # the refused registration must not have replaced or removed the first entry
        assert isinstance(client_module.registry()[decoy_name], FirstDecoy)
    finally:
        client_module._BACKENDS.pop(decoy_name, None)
    assert decoy_name not in client_module.registry()  # teardown actually held


def test_no_vendor_sdk_crosses_the_boundary() -> None:
    """WHY (CLAUDE.md guardrail 3): this module is the reason the harness is model-agnostic. Two
    honest checks: (1) an AST scan of the module's own source for a literal vendor import at ANY
    scope, and (2) a fresh interpreter that imports the module and reports which vendor packages
    landed in `sys.modules` — which catches a vendor pulled in transitively by a first-party
    import, something the AST scan alone cannot see."""
    vendors = {"anthropic", "openai", "boto3", "google", "litellm", "langchain", "langgraph"}

    source = Path(client_module.__file__).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    assert imported & vendors == set(), f"vendor SDK imported at module scope: {imported & vendors}"

    src = Path(client_module.__file__).resolve().parents[2]  # .../src, the import root
    probe = (
        "import sys, fleet.llm.client;"
        f"print([m for m in {sorted(vendors)!r} if m in sys.modules])"
    )
    result = subprocess.run(  # noqa: S603 - fixed argv, sys.executable, no shell
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "PYTHONPATH": str(src)},
    )
    assert result.stdout.strip() == "[]", result.stdout
