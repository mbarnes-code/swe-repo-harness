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
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, cast
from uuid import UUID

import aiosqlite
import pytest
from pydantic import BaseModel

from fleet.llm.client import (
    BackendReply,
    CallPolicy,
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
    BACKEND_UNAVAILABLE,
    CAPABILITY_DRIFT,
)
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
    tmp_path: Path, backend: ScriptedBackend, router: LlmRouter
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
                llm_policy=CallPolicy(max_targets_per_call=4),
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
        assert await h.ctx.llm_findings.flush() == 1

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
        assert await h.ctx.llm_findings.flush() == 0
        assert await h.findings(CAPABILITY_DRIFT) == []


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
        assert await h.ctx.llm_findings.flush() == 1

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
        assert payload["failover_triggers"] is None
        assert payload["failover_triggers_recorded"] is False, (
            "`TierUnavailable` carries no per-target `FailoverTrigger`, and `_emit_failover` "
            "never fires for the LAST target, so the set cannot be reconstructed — the row must "
            "say it does not know rather than infer"
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
    """
    backend = ScriptedBackend(HONEST_CAPS)
    async for h in _build(tmp_path, backend, make_router()):
        client = h.ctx.model_client
        assert isinstance(client, LadderModelClient)
        sink = h.ctx.llm_findings
        assert client._on_drift == sink.on_drift
        assert client._on_failover == sink.on_failover
