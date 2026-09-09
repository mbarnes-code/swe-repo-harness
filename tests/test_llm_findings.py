"""The LLM layer's findings actually reach SQLite (§7.7, §11.8, §13 rows 37 and 40).

`LadderModelClient` has always COMPUTED a `CapabilityDrift` and a `BackendFailover` correctly —
`_emit_drift` and `_emit_failover` are unit-tested in `test_llm_client.py` and pass. What was
missing is the other half: `orchestrator/context.py` built the client supplying NEITHER callback,
so both records hit the `is None` guard and were discarded. The defect was invisible precisely
because the computation was right.

That is why every assertion here is **a row read back out of the database**, never "the callback
was called". A mock-was-called assertion is exactly the assertion that would have passed
throughout the entire period the findings were being thrown away: the callback in `test_llm_client`
*was* called, by a client the test constructed itself with a sink it supplied itself. What no test
covered is that the client the FLEET runs is built with a sink at all, and that the sink writes.

Nothing here opens a socket. Real temp database, real `StateWriter`, real `RunContext` assembly,
fake backend.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, cast
from uuid import UUID

import aiosqlite
import pytest
from pydantic import BaseModel

from fleet.llm.cache import CachingModelClient
from fleet.llm.client import (
    BackendFailover,
    BackendReply,
    CallPolicy,
    CapabilityDrift,
    LadderModelClient,
    Message,
    StructuredOutputMode,
    TransportError,
)
from fleet.llm.roles import SPEC_ROLE_TIERS, LlmRouter
from fleet.models.enums import ModelTier, Phase
from fleet.models.tasks import BackendTarget, ModelCapabilities, Price, TokenUsage
from fleet.orchestrator.budgets import Ceilings, CostLedger, Limits
from fleet.orchestrator.context import RunContext, default_logger
from fleet.orchestrator.findings import (
    BACKEND_FAILOVER_EVENT,
    BACKEND_HEALTH_TRANSITION_EVENT,
    BACKEND_UNAVAILABLE,
    CAPABILITY_DRIFT,
    LlmFindingSink,
)
from fleet.orchestrator.retry import RetryPolicy
from fleet.settings import FleetConfig
from fleet.state import db as dbmod
from fleet.state.db import StateWriter, connect_ro, initialize_database
from fleet.state.repository import SqliteStateRepository

NOW = datetime(2026, 8, 18, 9, 0, 0, tzinfo=UTC)
RUN_ID = UUID("44444444-4444-4444-8444-444444444444")
RUN = str(RUN_ID)
ROLE = "transform_repair"


class Verdict(BaseModel):
    summary: str


#: Promises the top rung and can honour none of it — `promised_mode` reads
#: `structured_output_modes`, `negotiate` reads the booleans, and the gap between the two IS
#: `CapabilityDrift` (client.py:631-654). This is the shape of a local server that advertises
#: guided JSON in config and silently ignores the parameter.
DRIFTING_CAPS = ModelCapabilities(
    supports_json_schema=False,
    supports_tools=False,
    supports_constrained_decoding=False,
    max_output_tokens=4096,
    structured_output_modes=(StructuredOutputMode.JSON_SCHEMA, StructuredOutputMode.PROMPTED),
)

HONEST_CAPS = ModelCapabilities(
    supports_json_schema=True,
    max_output_tokens=4096,
    structured_output_modes=(StructuredOutputMode.JSON_SCHEMA,),
)


class ScriptedBackend:
    """One transport, offline. `script` entries are raised if they are exceptions, else returned."""

    name: ClassVar[str] = "fake"
    version: ClassVar[int] = 1

    def __init__(
        self,
        caps: ModelCapabilities,
        script: Sequence[BackendReply | Exception] | None = None,
    ) -> None:
        self._caps = caps
        self._script = list(script or [])
        self.calls: list[str] = []

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
        self.calls.append(target.model_id)
        if self._script:
            nxt = self._script.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt
        return BackendReply(
            text=Verdict(summary="answered offline").model_dump_json(),
            usage=TokenUsage(input_tokens=10, output_tokens=3, model_id=target.model_id),
            finish_reason="stop",
        )


def _target(model_id: str) -> BackendTarget:
    return BackendTarget(
        backend="fake", model_id=model_id, price=Price(in_per_mtok=1.0, out_per_mtok=2.0)
    )


def make_router(*model_ids: str) -> LlmRouter:
    targets = tuple(_target(m) for m in (model_ids or ("fake-1",)))
    return LlmRouter(dict(SPEC_ROLE_TIERS), dict.fromkeys(ModelTier, targets), profile="test")


@dataclass(slots=True)
class Harness:
    ctx: RunContext
    read_conn: aiosqlite.Connection
    backend: ScriptedBackend

    async def findings(self, kind: str) -> list[tuple[str | None, str, str, dict[str, object]]]:
        """`(repo_id, severity, fingerprint, payload)` per row — read RAW, so the assertion is
        about what an operator's `fleet report` would find and not about an in-memory object."""
        async with self.read_conn.execute(
            "SELECT repo_id, severity, fingerprint, payload FROM findings "
            " WHERE run_id = ? AND kind = ? ORDER BY finding_id",
            (RUN, kind),
        ) as cursor:
            rows = await cursor.fetchall()
        return [(r[0], str(r[1]), str(r[2]), dict(json.loads(r[3]))) for r in rows]

    async def events(self, event: str) -> list[dict[str, object]]:
        async with self.read_conn.execute(
            "SELECT payload FROM events WHERE run_id = ? AND event = ? ORDER BY seq",
            (RUN, event),
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(json.loads(r[0])) for r in rows]


async def _build(
    tmp_path: Path,
    backend: ScriptedBackend,
    router: LlmRouter,
    *,
    llm_policy: CallPolicy | None = None,
) -> AsyncIterator[Harness]:
    path = tmp_path / "state" / "fleet.db"
    await initialize_database(path)
    config = FleetConfig()
    async with StateWriter(path, owner="test-llm-findings") as writer:
        read_conn = await connect_ro(path)
        try:
            repo = SqliteStateRepository(writer=writer, read_conn=read_conn)
            await repo.upsert_run(
                RUN, started_at=NOW, config_sha256="a" * 64, harness_version="0.1.0"
            )
            await repo.open_budget_ledger(RUN, max_usd=100.0, now=NOW)
            await repo.upsert_repo(
                "repo-a", name="repo-a", url="https://example.invalid/a.git", now=NOW
            )
            ledger = CostLedger(
                repo,
                run_id=RUN,
                ceilings=Ceilings.from_settings(config.budgets, config.stubs),
                clock=lambda: NOW,
            )
            ctx = RunContext(
                run_id=RUN_ID,
                config=config,
                writer=writer,
                repository=repo,
                read_conn=read_conn,
                ledger=ledger,
                limits=Limits(
                    git_net=asyncio.Semaphore(8),
                    subprocess=asyncio.Semaphore(16),
                    docker=asyncio.Semaphore(4),
                    llm={},
                    cpu_pool=cast(Any, None),
                    ledger=ledger,
                ),
                llm=router,
                backends={"fake": backend},
                log=default_logger("test.findings"),
                work_dir=tmp_path / "work",
                lease_owner="test-host:test-cid:1:boot",
                clock=lambda: NOW,
                llm_policy=llm_policy or CallPolicy(max_targets_per_call=4),
                # Zero delay: a test proving an entire §11.8 backoff schedule is exhausted must
                # not spend the real wall-clock seconds that production-tuned schedule reuses.
                retry_policy=RetryPolicy(backoff_base_s=0.0, backoff_cap_s=0.0),
            )
            yield Harness(ctx=ctx, read_conn=read_conn, backend=backend)
        finally:
            await read_conn.close()


@pytest.fixture(autouse=True)
def _release_slot() -> AsyncIterator[None]:
    yield
    dbmod._release_write_slot()


# ======================================================================================
# §13 row 37 — CapabilityDrift
# ======================================================================================


async def test_a_drift_computed_by_the_client_becomes_a_findings_row(tmp_path: Path) -> None:
    """THE regression this lane exists for. The client computed this record correctly for its
    whole life and `RunContext` dropped it on the floor by supplying no `on_drift`.

    The assertion is a SELECT, not a spy: what was broken was persistence, and only persistence
    survives the process. `promised` / `actual` are both in the payload because a finding that
    says "drift" without saying *from what, to what* cannot tell an operator whether the profile
    over-promised or the endpoint under-delivered.
    """
    backend = ScriptedBackend(DRIFTING_CAPS)
    async for h in _build(tmp_path, backend, make_router()):
        response = await h.ctx.model_client.complete(
            ROLE, [Message(role="user", content="hi")], Verdict
        )
        assert response.mode is StructuredOutputMode.PROMPTED, (
            "the call itself must still succeed — drift is a finding, not a failure"
        )

        assert await h.findings(CAPABILITY_DRIFT) == [], (
            "nothing may be written from the synchronous callback: it runs on `complete()`'s hot "
            "path and a write there would be a fire-and-forget task racing the writer's close"
        )
        # 1 drift finding + 1 `llm_call` event (§12.18): the one `backend.invoke()` this call made
        # returned, so it is buffered and drained by this same `flush()` alongside the drift.
        assert await h.ctx.llm_findings.flush() == 2

        rows = await h.findings(CAPABILITY_DRIFT)
        assert len(rows) == 1
        repo_id, severity, _, payload = rows[0]
        assert repo_id is None, (
            "one client serves every repo in a wave concurrently, so attributing a drift to 'the "
            "current repo' would be a guess that is wrong whenever two repos are in flight"
        )
        assert severity == "warn"
        assert payload["role"] == ROLE
        assert payload["backend"] == "fake"
        assert payload["model_id"] == "fake-1"
        assert payload["promised"] == str(StructuredOutputMode.JSON_SCHEMA)
        assert payload["actual"] == str(StructuredOutputMode.PROMPTED)


async def test_repeated_drift_against_one_target_converges_on_one_row(tmp_path: Path) -> None:
    """§11.7: a finding is re-raised, never duplicated. A tier whose endpoint has quietly stopped
    honouring guided JSON drifts on EVERY call of a 250-repo run — without the idempotency key
    that is a quarter-million-row table saying one thing."""
    backend = ScriptedBackend(DRIFTING_CAPS)
    async for h in _build(tmp_path, backend, make_router()):
        for _ in range(5):
            await h.ctx.model_client.complete(ROLE, [Message(role="user", content="x")], Verdict)
        await h.ctx.llm_findings.flush()

        assert len(await h.findings(CAPABILITY_DRIFT)) == 1


async def test_an_honest_backend_writes_no_drift_finding(tmp_path: Path) -> None:
    """The negative half, and it is not filler: a sink that wrote unconditionally would make
    `CapabilityDrift` mean 'a call happened', which is worse than not emitting it at all."""
    backend = ScriptedBackend(HONEST_CAPS)
    async for h in _build(tmp_path, backend, make_router()):
        await h.ctx.model_client.complete(ROLE, [Message(role="user", content="x")], Verdict)
        # 0 drift findings, but the call itself is still 1 `llm_call` event (§12.18) — that event
        # is unconditional on every completed provider call, drift or no drift.
        assert await h.ctx.llm_findings.flush() == 1
        assert await h.findings(CAPABILITY_DRIFT) == []


# ======================================================================================
# §12.43 case (iii) — schema exhaustion AND capability drift, in ONE induced scenario
# ======================================================================================
#
# research-51 / round VI task 99: `docs/CRITERIA_PLAN.md` §43 recorded case (iii)'s two halves
# ("a target that never returns schema-valid output exhausts `llm.max_schema_repairs`, fails
# over on `SCHEMA_UNSATISFIED`, and writes a `CapabilityDrift` finding when the achieved rung is
# below the profile's declared one") as proven only SEPARATELY — schema exhaustion in
# `test_llm_client.py::test_schema_violation_buys_one_repair_then_fails_the_target_over`, drift
# in `test_a_drift_computed_by_the_client_becomes_a_findings_row` above, neither in the same
# induced call as the other. This section closes that residual: ONE `complete()` call whose
# first target exhausts its repairs and whose SECOND target (the one the failover lands on)
# under-delivers its own declared promise.


class TieredScriptedBackend:
    """Like `ScriptedBackend` above, but capabilities and script are PER TARGET (keyed by
    `model_id`) rather than uniform across the whole backend name. Case (iii)'s combined
    scenario needs two different capability profiles behind one backend: target 1 never
    validates (exhausts schema repairs) and target 2's ACTUAL capabilities fall a rung below its
    declared `structured_output_modes` PROMISE (the drift `negotiate()`/`promised_mode` compare).
    """

    name: ClassVar[str] = "fake"
    version: ClassVar[int] = 1

    def __init__(
        self,
        per_target: Mapping[str, tuple[ModelCapabilities, Sequence[BackendReply | Exception]]],
    ) -> None:
        self._caps = {model_id: caps for model_id, (caps, _script) in per_target.items()}
        self._scripts = {model_id: list(script) for model_id, (_caps, script) in per_target.items()}
        self.calls: list[str] = []

    def declared_capabilities(self, target: BackendTarget) -> ModelCapabilities:
        return self._caps[target.model_id]

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
        self.calls.append(target.model_id)
        script = self._scripts[target.model_id]
        if not script:
            raise AssertionError(f"{target.model_id} called more times than scripted")
        nxt = script.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def _schema_invalid_reply() -> BackendReply:
    """Schema-VALID JSON, model-INVALID content — `summary` must be a `str` and an `int` fails
    Pydantic validation, a genuine schema violation (matches `test_llm_client.py`'s own
    `invalid_reply()` shape)."""
    return BackendReply(
        text=json.dumps({"summary": 123}),
        usage=TokenUsage(input_tokens=10, output_tokens=3),
        finish_reason="stop",
    )


async def test_schema_exhaustion_and_capability_drift_together_in_one_induced_call(
    tmp_path: Path,
) -> None:
    """§12.43 case (iii), both halves, ONE `complete()` call. `fake-1` never returns schema-valid
    output and exhausts `llm.max_schema_repairs` (default 1: initial + one repair, both invalid),
    raising `SchemaUnsatisfied` and failing over with trigger `SCHEMA_UNSATISFIED`. `fake-2` then
    answers — the call itself succeeds — but its ACTUAL capabilities (`DRIFTING_CAPS`:
    `supports_json_schema=False`) fall a rung below what it PROMISES
    (`structured_output_modes=(JSON_SCHEMA, PROMPTED)`), so `negotiate()` lands on `PROMPTED`
    where `JSON_SCHEMA` was promised — exactly `CapabilityDrift`, on the SAME call the failover
    happened in, not a second one."""
    backend = TieredScriptedBackend(
        {
            "fake-1": (HONEST_CAPS, [_schema_invalid_reply(), _schema_invalid_reply()]),
            "fake-2": (
                DRIFTING_CAPS,
                [
                    BackendReply(
                        text=Verdict(summary="fake-2 answered under drift").model_dump_json(),
                        usage=TokenUsage(input_tokens=10, output_tokens=3, model_id="fake-2"),
                        finish_reason="stop",
                    )
                ],
            ),
        }
    )
    async for h in _build(tmp_path, backend, make_router("fake-1", "fake-2")):
        response = await h.ctx.model_client.complete(
            ROLE, [Message(role="user", content="does this repo migrate?")], Verdict
        )
        assert response.value.summary == "fake-2 answered under drift"
        # fake-1: initial + one repair (both invalid) = 2 calls, then failover to fake-2 = 1 call.
        assert backend.calls == ["fake-1", "fake-1", "fake-2"]

        await h.ctx.llm_findings.flush()

        drift_rows = await h.findings(CAPABILITY_DRIFT)
        assert len(drift_rows) == 1, (
            "the drift must be attributed to fake-2 (the target that actually answered under "
            "drift), not fake-1 (which never got far enough to negotiate a rung)"
        )
        repo_id, severity, _, payload = drift_rows[0]
        assert repo_id is None  # fleet-level, same reasoning as every sibling drift row above
        assert severity == "warn"
        assert payload["role"] == ROLE
        assert payload["model_id"] == "fake-2"
        assert payload["promised"] == str(StructuredOutputMode.JSON_SCHEMA)
        assert payload["actual"] == str(StructuredOutputMode.PROMPTED)

        failover_events = await h.events(BACKEND_FAILOVER_EVENT)
        assert len(failover_events) == 1, (
            "the schema-exhaustion half of this same call must ALSO be visible, as the "
            "backend_failover event §11.8 records for any failover trigger"
        )
        assert failover_events[0]["trigger"] == "SCHEMA_UNSATISFIED"
        assert failover_events[0]["from_model_id"] == "fake-1"
        assert failover_events[0]["to_model_id"] == "fake-2"
        assert failover_events[0]["role"] == ROLE


# ======================================================================================
# §11.8 — backend_failover
# ======================================================================================


async def test_a_failover_becomes_a_backend_failover_event_row(tmp_path: Path) -> None:
    """§11.8. A hop between targets is an EVENT — the fleet recovered — so it lands in `events`,
    which is the ordered stream `fleet report` and `jq` read, rather than in `findings`.

    Both endpoints are named. A failover record that says only "we failed over" cannot answer the
    question an operator actually has, which is *away from what, and onto what*.
    """
    backend = ScriptedBackend(
        HONEST_CAPS,
        script=[
            TransportError("connection refused by fake-1", trigger="CONNECTION"),
            BackendReply(
                text=Verdict(summary="second target answered").model_dump_json(),
                usage=TokenUsage(input_tokens=10, output_tokens=3, model_id="fake-2"),
                finish_reason="stop",
            ),
        ],
    )
    async for h in _build(tmp_path, backend, make_router("fake-1", "fake-2")):
        response = await h.ctx.model_client.complete(
            ROLE, [Message(role="user", content="x")], Verdict
        )
        assert response.value.summary == "second target answered"

        assert await h.events(BACKEND_FAILOVER_EVENT) == [], "the callback must only buffer"
        # 1 `backend_failover` + 1 `llm_call` (§12.18): `fake-1` raised `TransportError` before
        # any `BackendReply` existed, so it produces no `llm_call` (see `LlmCall`'s docstring —
        # there is nothing to report metrics from); `fake-2` answered, so it produces exactly one.
        assert await h.ctx.llm_findings.flush() == 2

        events = await h.events(BACKEND_FAILOVER_EVENT)
        assert len(events) == 1
        assert events[0]["from_model_id"] == "fake-1"
        assert events[0]["to_model_id"] == "fake-2"
        assert events[0]["trigger"] == "CONNECTION"
        assert events[0]["role"] == ROLE


async def test_a_clean_call_emits_no_failover_event(tmp_path: Path) -> None:
    backend = ScriptedBackend(HONEST_CAPS)
    async for h in _build(tmp_path, backend, make_router("fake-1", "fake-2")):
        await h.ctx.model_client.complete(ROLE, [Message(role="user", content="x")], Verdict)
        await h.ctx.llm_findings.flush()
        assert await h.events(BACKEND_FAILOVER_EVENT) == []


# ======================================================================================
# §11.8 (ADR-0132) — backend_health_transition
# ======================================================================================


async def test_a_backend_health_transition_becomes_an_event_row(tmp_path: Path) -> None:
    """Finding 4 (review round 1): `on_health_transition` — the controller-mandated observability
    callback `LadderModelClient` gains alongside `on_drift`/`on_failover`/`on_llm_call` — had zero
    test coverage through the real sink; `test_llm_failover.py` only asserted the raw callback
    LIST, never that a real `RunContext`/`LlmFindingSink` persists it. This module's own docstring
    names D59 (an event type nobody wired a sink for, silently discarded) as the exact defect
    class this exists to prevent — an untested fourth sink is that same shape with a different
    name. Drives a REAL `open_after_failures=1` breaker open through a real `RunContext` and
    `model_client`, exactly like the other three event-type tests above, and reads the row back
    out of SQLite rather than off an in-memory list.
    """
    exhausted_backoff = [TransportError("429 slow down", trigger="RATE_LIMIT")] * (
        RetryPolicy().max_transient_retries + 1
    )
    backend = ScriptedBackend(
        HONEST_CAPS,
        script=[
            *exhausted_backoff,
            BackendReply(
                text=Verdict(summary="second target answered").model_dump_json(),
                usage=TokenUsage(input_tokens=10, output_tokens=3, model_id="fake-2"),
                finish_reason="stop",
            ),
        ],
    )
    async for h in _build(
        tmp_path,
        backend,
        make_router("fake-1", "fake-2"),
        llm_policy=CallPolicy(max_targets_per_call=4, open_after_failures=1),
    ):
        response = await h.ctx.model_client.complete(
            ROLE, [Message(role="user", content="x")], Verdict
        )
        assert response.value.summary == "second target answered"

        assert await h.events(BACKEND_HEALTH_TRANSITION_EVENT) == [], (
            "the callback must only buffer, exactly like the other three sinks"
        )
        await h.ctx.llm_findings.flush()

        events = await h.events(BACKEND_HEALTH_TRANSITION_EVENT)
        assert len(events) == 1
        assert events[0]["backend"] == "fake"
        assert events[0]["model_id"] == "fake-1"
        assert events[0]["tier"] == str(ModelTier.WORKHORSE)
        assert events[0]["from_state"] == "UP"
        assert events[0]["to_state"] == "DOWN"

        # `level` is validated against `obs/events.py`'s `_LEVELS` allowlist by `emitter.emit` —
        # confirming it landed as "warning" (not silently coerced to "info") is what the false
        # docstring claim this fix corrected would otherwise have hidden.
        async with h.read_conn.execute(
            "SELECT level FROM events WHERE run_id = ? AND event = ?",
            (RUN, BACKEND_HEALTH_TRANSITION_EVENT),
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "warning"


# ======================================================================================
# §13 row 40 — BackendUnavailable
# ======================================================================================


async def test_backend_unavailable_names_the_tier_and_every_target_tried(tmp_path: Path) -> None:
    """§13 row 40. Written before the exit-8 halt, carrying `TierUnavailable`'s own message —
    which is `tier <T> exhausted after targets: <a>, <b>` (client.py:151-155) and therefore names
    the tier and, in order, every target the ladder spent.

    Severity is `error`, not the column default `warn`: this finding is the reason the run
    stopped, and a report that ranked it beside a `CoarseTarget` warning would bury it.
    """
    backend = ScriptedBackend(HONEST_CAPS)
    async for h in _build(tmp_path, backend, make_router("fake-1", "fake-2")):
        await h.ctx.llm_findings.record_backend_unavailable(
            repo_id="repo-a",
            phase=Phase.TRANSFORM,
            observed="tier WORKHORSE exhausted after targets: fake:fake-1, fake:fake-2",
        )

        rows = await h.findings(BACKEND_UNAVAILABLE)
        assert len(rows) == 1
        repo_id, severity, _, payload = rows[0]
        assert repo_id == "repo-a", "this one DOES know its repo — the runner holds it"
        assert severity == "error"
        assert payload["phase"] == "TRANSFORM"
        observed = str(payload["observed"])
        assert "WORKHORSE" in observed
        assert "fake:fake-1" in observed and "fake:fake-2" in observed, (
            "EVERY target, not just the last one: an operator deciding whether to fail over a "
            "whole profile needs to know the fallback was tried too"
        )


async def test_backend_unavailable_refuses_to_assert_an_outage(tmp_path: Path) -> None:
    """§13 row 43. The finding must not diagnose what the harness did not measure.

    A pure 429 reaches this emission site today: `client.py:532` catches `TransportError` without
    inspecting `exc.trigger`, so a `RATE_LIMIT` retires a target exactly like a refused
    connection, three of them exhaust the tier, and `classify.py:243-258` makes the result
    non-retryable. Row 43 forbids the inference flatly — `DOWN` requires a connection-level
    failure or a 5xx, never throttling alone — and `BackendHealth.DOWN` has no representation in
    `src/` at all.

    So the row carries three machine-readable honesty fields rather than a diagnosis. Asserted
    mechanically, not by grepping the caveat prose: a later reader deciding whether to trust this
    finding must not have to parse English, and a future edit that quietly starts asserting an
    outage must fail here.
    """
    backend = ScriptedBackend(HONEST_CAPS)
    async for h in _build(tmp_path, backend, make_router("fake-1", "fake-2")):
        await h.ctx.llm_findings.record_backend_unavailable(
            repo_id="repo-a",
            phase=Phase.TRANSFORM,
            observed="tier WORKHORSE exhausted after targets: fake:fake-1, fake:fake-2",
        )
        _, _, _, payload = (await h.findings(BACKEND_UNAVAILABLE))[0]

        assert payload["asserts_outage"] is False, (
            "a finding claiming the backend is DOWN turns a correctable throttle into a durable "
            "false record — and `DOWN` is a state nothing in `src/` computes"
        )
        assert payload["failover_triggers"] == {}
        assert payload["failover_triggers_scope"] == "run"
        assert payload["failover_triggers_recorded"] == "unknown", (
            "at run scope even 'none' is a claim about the exhausted tier, and the row cannot "
            "identify that tier — the honest answer is that it does not know"
        )
        assert payload["throttling_observed"] is None, (
            "the runner cannot say WHICH tier died (`TierUnavailable.tier` is lost at the "
            "exception -> WorkerError boundary), so a boolean here would be a claim about a tier "
            "this row cannot identify"
        )
        assert "429" in str(payload["caveat"]) or "RATE_LIMIT" in str(payload["caveat"]), (
            "the operator-facing text has to name the alternative explanation, or the honesty "
            "flags above are unactionable"
        )
        others = {k: v for k, v in payload.items() if k != "caveat"}
        assert "down" not in json.dumps(others).lower(), (
            "no DATA field may carry the DOWN vocabulary — `retry.py:196`'s "
            "'every target for the tier is DOWN' must not be copied into the row, which is why "
            "`decision.reason` is deliberately not passed. Only the caveat may say the word, and "
            "only to forbid the inference"
        )


async def test_a_repeated_outage_for_one_repo_converges_on_one_row(tmp_path: Path) -> None:
    """§11.7 again, and it matters more here: `fleet resume` re-drives the same repo into the same
    dead tier, and the second exit 8 must update the finding rather than mint a second one."""
    backend = ScriptedBackend(HONEST_CAPS)
    async for h in _build(tmp_path, backend, make_router()):
        for _ in range(3):
            await h.ctx.llm_findings.record_backend_unavailable(
                repo_id="repo-a",
                phase=Phase.TRANSFORM,
                observed="tier WORKHORSE exhausted after targets: fake:fake-1",
            )
        assert len(await h.findings(BACKEND_UNAVAILABLE)) == 1


# ======================================================================================
# the wiring itself
# ======================================================================================


async def test_run_context_supplies_both_callbacks_to_the_client(tmp_path: Path) -> None:
    """The mechanical guard against the exact regression: `LadderModelClient` accepts `on_drift`
    and `on_failover` as OPTIONAL, so dropping the wiring again would break nothing loudly — the
    findings would simply stop existing, silently, forever.

    Asserted against the client's own attributes rather than through behaviour so the failure
    message points at the wiring rather than at whichever finding happened to go missing.

    The unwrapping step is not incidental. Since §11.6's cache was actually installed
    (2026-08-25), `RunContext.model_client` is the `CachingModelClient` DECORATOR on every run,
    and the two callbacks live on the ladder client it wraps. Both `isinstance` checks are kept
    deliberately rather than relaxed to one: a decorator that stopped delegating to a
    `LadderModelClient` at all would satisfy a looser assertion vacuously, and this test's whole
    reason for existing is that these callbacks are OPTIONAL, so losing them is silent.
    """
    backend = ScriptedBackend(HONEST_CAPS)
    async for h in _build(tmp_path, backend, make_router()):
        client = h.ctx.model_client
        assert isinstance(client, CachingModelClient)
        inner = client._inner
        assert isinstance(inner, LadderModelClient)
        sink = h.ctx.llm_findings
        assert inner._on_drift == sink.on_drift
        assert inner._on_failover == sink.on_failover


# ======================================================================================
# F1 — the partial trigger set: refuse to diagnose, but do not understate what is known
# ======================================================================================


async def test_the_finding_reports_the_partial_trigger_set_it_actually_holds(
    tmp_path: Path,
) -> None:
    """The first cut said `failover_triggers_recorded: false` unconditionally. That was a second
    false statement, in the opposite direction from the one it was fixing.

    For an N-target tier, `client.py:539-541` emits the first N-1 triggers and this same sink both
    persists them as `backend_failover` events and keeps them. An operator who read "none
    recorded", concluded the throttle-vs-outage question was unanswerable, and went to repair
    infrastructure would have been doing so while a `RATE_LIMIT` row for the same run sat in
    `events`. The row must report what the run holds.

    It still refuses `"complete"`: the target that EXHAUSTS the tier never reports its own
    trigger, so completeness is structurally unreachable until `TierUnavailable` carries them.

    **Post-ADR-0132: a single 429 no longer fails over at all** — `_call_target`'s own backoff arm
    absorbs it on the SAME target. This test now scripts fake-1 returning `RATE_LIMIT` through its
    ENTIRE backoff schedule (`RetryPolicy().max_transient_retries + 1` = 5 raises: the initial
    attempt plus every retry the default policy allows) before fake-2 answers — that exhaustion IS
    the "qualifying failure" §12.43 case (ii) names, and is what still produces exactly one
    `RATE_LIMIT` failover trigger for fake-1.
    """
    exhausted_backoff = [TransportError("429 slow down", trigger="RATE_LIMIT")] * (
        RetryPolicy().max_transient_retries + 1
    )
    backend = ScriptedBackend(
        HONEST_CAPS,
        script=[
            *exhausted_backoff,
            BackendReply(
                text=Verdict(summary="ok").model_dump_json(),
                usage=TokenUsage(input_tokens=10, output_tokens=3, model_id="fake-2"),
                finish_reason="stop",
            ),
        ],
    )
    async for h in _build(tmp_path, backend, make_router("fake-1", "fake-2")):
        await h.ctx.model_client.complete(ROLE, [Message(role="user", content="x")], Verdict)
        await h.ctx.llm_findings.record_backend_unavailable(
            repo_id="repo-a",
            phase=Phase.TRANSFORM,
            observed="tier WORKHORSE exhausted after targets: fake:fake-1, fake:fake-2",
            tier=ModelTier.WORKHORSE,
        )
        _, _, _, payload = (await h.findings(BACKEND_UNAVAILABLE))[0]

        assert payload["failover_triggers"] == {"WORKHORSE": {"fake:fake-1": "RATE_LIMIT"}}, (
            "keyed by TIER first, so every entry is self-describing even when the row cannot "
            "name the tier that died"
        )
        assert payload["failover_triggers_recorded"] == "partial", (
            "'partial' is the only true value: N-1 triggers are held, the Nth is unknowable"
        )
        assert payload["failover_triggers_recorded"] != "complete"
        assert payload["failover_triggers_scope"] == "tier"
        assert payload["throttling_observed"] is True, (
            "THE field that changes the operator's next action — run slower, do not repair"
        )
        assert payload["asserts_outage"] is False, (
            "knowing the triggers still does not license the DOWN claim: the exhausting target's "
            "trigger is unknown, so an outage cannot be ruled in"
        )


async def test_throttling_observed_is_not_the_negation_of_asserts_outage(tmp_path: Path) -> None:
    """Both false means "we do not know", which is a third state and must stay reachable. A
    CONNECTION-triggered failover is genuine evidence of unreachability — and still not enough to
    assert `DOWN`, because `BackendHealth` is computed nowhere in `src/`."""
    backend = ScriptedBackend(
        HONEST_CAPS,
        script=[
            TransportError("connection refused", trigger="CONNECTION"),
            BackendReply(
                text=Verdict(summary="ok").model_dump_json(),
                usage=TokenUsage(input_tokens=10, output_tokens=3, model_id="fake-2"),
                finish_reason="stop",
            ),
        ],
    )
    async for h in _build(tmp_path, backend, make_router("fake-1", "fake-2")):
        await h.ctx.model_client.complete(ROLE, [Message(role="user", content="x")], Verdict)
        await h.ctx.llm_findings.record_backend_unavailable(
            repo_id="repo-a",
            phase=Phase.TRANSFORM,
            observed="tier WORKHORSE exhausted",
            tier=ModelTier.WORKHORSE,
        )
        _, _, _, payload = (await h.findings(BACKEND_UNAVAILABLE))[0]

        assert payload["failover_triggers"] == {"WORKHORSE": {"fake:fake-1": "CONNECTION"}}
        assert payload["throttling_observed"] is False
        assert payload["asserts_outage"] is False


# ======================================================================================
# F3 — a failing writer must not destroy what was computed
# ======================================================================================


class BrokenWriter:
    """A `StateWriter` whose `submit` refuses. Models the two real cases `flush()` can meet:
    `StateWriterClosedError` at shutdown and an exhausted SQLITE_BUSY retry budget."""

    def __init__(self) -> None:
        self.attempts = 0
        self.broken = True

    async def submit(self, unit: object) -> object:
        self.attempts += 1
        if self.broken:
            raise RuntimeError("writer is closed")
        return None


class BrokenRepository:
    def __init__(self) -> None:
        self.broken = True
        self.events: list[object] = []

    async def append_event(self, row: object) -> int:
        if self.broken:
            raise RuntimeError("writer is closed")
        self.events.append(row)
        return len(self.events)


def _sink(writer: object, repository: object) -> LlmFindingSink:
    return LlmFindingSink(
        run_id=RUN,
        writer=cast(Any, writer),
        repository=cast(Any, repository),
        clock=lambda: NOW,
    )


def _drift() -> CapabilityDrift:
    return CapabilityDrift(
        role=ROLE,
        tier=ModelTier.WORKHORSE,
        backend="fake",
        model_id="fake-1",
        promised=StructuredOutputMode.JSON_SCHEMA,
        actual=StructuredOutputMode.PROMPTED,
    )


def _failover(model_id: str = "fake-1") -> BackendFailover:
    return BackendFailover(
        role=ROLE,
        tier=ModelTier.WORKHORSE,
        from_backend="fake",
        from_model_id=model_id,
        to_backend="fake",
        to_model_id="fake-2",
        trigger="RATE_LIMIT",
    )


async def test_a_failed_flush_keeps_the_records_instead_of_destroying_them() -> None:
    """F3, and it is the lane's own bug class one layer up.

    `flush()` detaches the buffers before writing. If the write then fails and the records are not
    put back, they are gone forever — "computed, then discarded", reinstated on the error path,
    which is the path most likely to be carrying interesting drift. A later successful flush would
    write nothing and the table would look clean.

    The proof is that the SAME records land once the writer recovers, not merely that `pending` is
    non-zero: retention that cannot be drained is not retention.
    """
    writer, repository = BrokenWriter(), BrokenRepository()
    sink = _sink(writer, repository)
    sink.on_drift(_drift())
    sink.on_failover(_failover())
    assert sink.pending == 2

    with pytest.raises(RuntimeError):
        await sink.flush()

    assert sink.pending == 2, "the records were detached and dropped — the exact defect"

    writer.broken = False
    repository.broken = False
    assert await sink.flush() == 2
    assert sink.pending == 0
    assert writer.attempts == 2, "the drift retried; it was not silently skipped"
    assert len(repository.events) == 1


async def test_a_partly_failed_flush_re_buffers_only_what_did_not_land() -> None:
    """Retention must not become duplication. The drifts write in one `executemany`; the failovers
    write one row at a time, so a failure mid-loop has to put back the remainder and NOT the ones
    already committed — otherwise recovering from a transient busy timeout mints duplicate
    `events` rows, which are not deduped (`event_uid` is a fresh uuid4 per emit)."""

    class HalfBrokenRepository:
        def __init__(self) -> None:
            self.written: list[object] = []

        async def append_event(self, row: object) -> int:
            if len(self.written) >= 2:
                raise RuntimeError("busy")
            self.written.append(row)
            return len(self.written)

    writer, repository = BrokenWriter(), HalfBrokenRepository()
    writer.broken = False
    sink = _sink(writer, repository)
    sink.on_drift(_drift())
    for model_id in ("t1", "t2", "t3", "t4"):
        sink.on_failover(_failover(model_id))

    with pytest.raises(RuntimeError):
        await sink.flush()

    assert len(repository.written) == 2
    assert sink.pending == 2, (
        "the two events that landed must NOT be re-buffered, and the two that did not must be"
    )
    assert writer.attempts == 1, "the drift committed; a retry must not re-write it"


async def test_the_buffer_survives_cancellation() -> None:
    """`BaseException`, not `Exception`: a wave cancelled mid-flush would otherwise lose whatever
    the client had already computed, and cancellation is exactly when a run is being torn down
    for a reason worth recording."""

    class CancellingWriter:
        async def submit(self, unit: object) -> object:
            raise asyncio.CancelledError

    sink = _sink(CancellingWriter(), BrokenRepository())
    sink.on_drift(_drift())

    with pytest.raises(asyncio.CancelledError):
        await sink.flush()

    assert sink.pending == 1


async def test_the_trigger_map_narrows_to_one_tier_and_keys_every_entry_by_tier() -> None:
    """The ACCESSOR only — no row is written here, and the name says so.

    `observed_triggers` is what the cross-tier contamination fix rests on, so it is pinned
    directly: narrowing must exclude other tiers, and the unnarrowed view must stay keyed by tier
    so every entry is self-describing. The *row* built on top of this is asserted, against SQLite,
    in `test_a_cheap_tier_throttle_does_not_contaminate_a_heavy_tier_outage_row` below — this test
    deliberately does not claim to prove anything about a finding.
    """
    sink = _sink(BrokenWriter(), BrokenRepository())
    sink.on_failover(
        BackendFailover(
            role="repo_classify",
            tier=ModelTier.CHEAP,
            from_backend="fake",
            from_model_id="cheap-1",
            to_backend="fake",
            to_model_id="cheap-2",
            trigger="RATE_LIMIT",
        )
    )
    sink.on_failover(
        BackendFailover(
            role="build_author",
            tier=ModelTier.HEAVY,
            from_backend="fake",
            from_model_id="heavy-1",
            to_backend="fake",
            to_model_id="heavy-2",
            trigger="CONNECTION",
        )
    )

    heavy = sink.observed_triggers(ModelTier.HEAVY)
    assert heavy == {"HEAVY": {"fake:heavy-1": "CONNECTION"}}, (
        "the CHEAP 429 must not appear in a HEAVY view — it is a fact about a different tier"
    )
    assert sink.observed_triggers(ModelTier.CHEAP) == {"CHEAP": {"fake:cheap-1": "RATE_LIMIT"}}
    assert set(sink.observed_triggers()) == {"CHEAP", "HEAVY"}, (
        "the unnarrowed view still holds both, keyed so each entry says which tier it is about"
    )


async def test_a_cheap_tier_throttle_does_not_contaminate_a_heavy_tier_outage_row(
    tmp_path: Path,
) -> None:
    """N1, as a persisted ROW read back out of SQLite — the scenario, not the accessor.

    The reviewer's scenario, made executable: early in a long run a CHEAP role fails a target over
    on a 429; hours later HEAVY exhausts on genuine `CONNECTION` failures. With a flat
    target-keyed map the HEAVY row came out carrying `throttling_observed: true` and a target that
    was never in the HEAVY tier — telling the operator to lower concurrency while a dead HEAVY
    endpoint went unrepaired. That is the false-statement class this row exists to avoid, pointed
    the other way.

    `throttling_observed` must therefore answer for the tier that DIED, not for the run.
    """
    backend = ScriptedBackend(HONEST_CAPS)
    async for h in _build(tmp_path, backend, make_router()):
        sink = h.ctx.llm_findings
        sink.on_failover(
            BackendFailover(
                role="repo_classify",
                tier=ModelTier.CHEAP,
                from_backend="fake",
                from_model_id="cheap-1",
                to_backend="fake",
                to_model_id="cheap-2",
                trigger="RATE_LIMIT",
            )
        )
        sink.on_failover(
            BackendFailover(
                role="build_author",
                tier=ModelTier.HEAVY,
                from_backend="fake",
                from_model_id="heavy-1",
                to_backend="fake",
                to_model_id="heavy-2",
                trigger="CONNECTION",
            )
        )
        await sink.record_backend_unavailable(
            repo_id="repo-a",
            phase=Phase.BUILD,
            observed="tier HEAVY exhausted after targets: fake:heavy-1, fake:heavy-2",
            tier=ModelTier.HEAVY,
        )
        _, _, _, payload = (await h.findings(BACKEND_UNAVAILABLE))[0]

        assert payload["failover_triggers"] == {"HEAVY": {"fake:heavy-1": "CONNECTION"}}
        assert payload["failover_triggers_scope"] == "tier"
        assert payload["throttling_observed"] is False, (
            "the only RATE_LIMIT in this run belongs to CHEAP, which is not the tier that died"
        )
        assert payload["asserts_outage"] is False, (
            "a CONNECTION trigger is real evidence of unreachability and STILL does not license "
            "the DOWN claim — the exhausting target's own trigger is never emitted"
        )


async def test_a_retried_failover_does_not_duplicate_its_events_row(tmp_path: Path) -> None:
    """N3, against a REAL `events` table, so the `ON CONFLICT` that does the deduping executes.

    `StateWriter.submit` queues the unit and then awaits its future, so a `CancelledError` at that
    await leaves the unit queued — it still commits — while `flush()` re-buffers the record. If
    the retry minted a FRESH `event_uid`, `ON CONFLICT (run_id, event_uid) DO NOTHING`
    (`repository.py:1803`) could not recognise the row that already landed and the hop would be
    counted twice.

    The repository proxy commits through the real `SqliteStateRepository` and *then* raises, which
    is exactly the committed-but-not-acknowledged shape. The load-bearing assertion is the ROW
    COUNT afterwards — a uid-equality assertion against a fake (which is what this test used to
    be) never runs the conflict clause it is named for.
    """

    class CommitsThenCancels:
        """Delegates to the real repository, then raises — the row lands, the caller never hears."""

        def __init__(self, inner: Any) -> None:
            self._inner = inner
            self.explode = True
            self.uids: list[str] = []

        async def append_event(self, row: Any) -> int:
            self.uids.append(str(row.event_uid))
            seq = int(await self._inner.append_event(row))
            if self.explode:
                raise asyncio.CancelledError
            return seq

    backend = ScriptedBackend(HONEST_CAPS)
    async for h in _build(tmp_path, backend, make_router()):
        proxy = CommitsThenCancels(h.ctx.repository)
        sink = LlmFindingSink(
            run_id=RUN,
            writer=h.ctx.writer,
            repository=cast(Any, proxy),
            clock=lambda: NOW,
        )
        sink.on_failover(_failover())

        with pytest.raises(asyncio.CancelledError):
            await sink.flush()
        assert sink.pending == 1, "the record must survive the cancellation to be retried at all"
        assert len(await h.events(BACKEND_FAILOVER_EVENT)) == 1, (
            "precondition: the cancelled attempt really did commit its row"
        )

        proxy.explode = False
        assert await sink.flush() == 1

        assert proxy.uids[0] == proxy.uids[1], "the uid must be minted once, at buffer time"
        assert len(await h.events(BACKEND_FAILOVER_EVENT)) == 1, (
            "the retry inserted a SECOND backend_failover row: a fresh uuid4 on retry slips past "
            "ON CONFLICT (run_id, event_uid) and double-counts the hop"
        )


async def test_the_caveat_never_contradicts_the_scope_field_of_its_own_row(
    tmp_path: Path,
) -> None:
    """O1. The caveat is prose an operator acts on, sitting in the same row as the `scope` field it
    describes; if the two disagree, the row is self-refuting and the more persuasive half wins.

    The single unconditional caveat asserted "every row this harness writes today has scope 'run'".
    That was true of every row that could then be produced and false of the row the `tier=` arm
    produces — latent only because nothing passes `tier=` yet, and live the moment the sibling lane
    wires it, which is precisely the future the N8 disclosure is written for. An overclaim inside
    the fix for an overclaim.

    Both arms are asserted here, and the unreachable one deliberately so: it is the arm that goes
    live without anyone revisiting this file.
    """
    backend = ScriptedBackend(HONEST_CAPS)
    async for h in _build(tmp_path, backend, make_router()):
        await h.ctx.llm_findings.record_backend_unavailable(
            repo_id="repo-a", phase=Phase.TRANSFORM, observed="tier WORKHORSE exhausted"
        )
        _, _, _, run_row = (await h.findings(BACKEND_UNAVAILABLE))[0]
        assert run_row["failover_triggers_scope"] == "run"
        assert "scope 'run'" in str(run_row["caveat"])
        assert "scope 'tier'" not in str(run_row["caveat"])

        await h.ctx.llm_findings.record_backend_unavailable(
            repo_id="repo-a",
            phase=Phase.BUILD,
            observed="tier HEAVY exhausted",
            tier=ModelTier.HEAVY,
        )
        rows = {r[3]["phase"]: r[3] for r in await h.findings(BACKEND_UNAVAILABLE)}
        tier_row = rows["BUILD"]
        assert tier_row["failover_triggers_scope"] == "tier"
        assert "scope 'tier'" in str(tier_row["caveat"]), (
            "a tier-scoped row shipping the run-scoped caveat denies its own `scope` field"
        )
        assert "scope 'run'" not in str(tier_row["caveat"])

        for row in (run_row, tier_row):
            assert "a 429 alone can never mean DOWN" in str(row["caveat"]), (
                "the row-43 refusal is the load-bearing half and belongs in BOTH arms"
            )
            assert "lower concurrency" in str(row["caveat"])
