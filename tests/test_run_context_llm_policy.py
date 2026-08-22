"""The §9 `llm:` knobs an operator sets actually reach the model client (§9, §11.8).

`RunContext.llm_policy` was declared and consumed and **never assigned**: `LadderModelClient` got
`policy=None` at every `RunContext(` site in `cli.py` (five, AST-counted), fell back to
`CallPolicy()`'s own
defaults, and every value under `llm.failover.*` was validated, digested, echoed back by
`fleet config` and read by nothing. `tests/test_config_keys_are_read.py` carried three of those
keys in `KNOWN_INERT` and said so in a comment.

Every assertion here is **a call log read off the backend** — how many targets the client actually
walked, how many times it actually re-asked — never `ctx.llm_policy == CallPolicy(...)`. A
field-equality assertion is precisely the assertion that would have passed throughout the period
the config was being discarded, because the field it compares is the one the client never saw. The
same reasoning applies one layer up: the existing `tests/test_llm_findings.py` fixture passes
`llm_policy=CallPolicy(max_targets_per_call=4)` **explicitly**, so it exercises the override arm
and is blind by construction to whether the derived arm exists at all.

Nothing here opens a socket: real temp database, real `StateWriter`, real `RunContext` assembly,
`FleetConfig` built in-process, scripted offline backend.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, cast
from uuid import UUID

import pytest
from pydantic import BaseModel

from fleet.llm.client import (
    BackendReply,
    CallPolicy,
    Message,
    SchemaUnsatisfied,
    StructuredOutputMode,
    TierUnavailable,
    TransportError,
)
from fleet.llm.roles import SPEC_ROLE_TIERS, LlmRouter
from fleet.models.enums import ModelTier
from fleet.models.tasks import BackendTarget, ModelCapabilities, Price, TokenUsage
from fleet.orchestrator.budgets import Ceilings, CostLedger, Limits
from fleet.orchestrator.context import RunContext, default_logger
from fleet.settings import FleetConfig
from fleet.state import db as dbmod
from fleet.state.db import StateWriter, connect_ro, initialize_database
from fleet.state.repository import SqliteStateRepository

NOW = datetime(2026, 8, 22, 9, 0, 0, tzinfo=UTC)
RUN_ID = UUID("22222222-2222-4222-8222-222222222222")
RUN = str(RUN_ID)
ROLE = "transform_repair"

HONEST_CAPS = ModelCapabilities(
    supports_json_schema=True,
    max_output_tokens=4096,
    structured_output_modes=(StructuredOutputMode.JSON_SCHEMA,),
)


class Verdict(BaseModel):
    summary: str


class LoggingBackend:
    """One offline transport. Every `invoke` is recorded, which is the only thing asserted."""

    name: ClassVar[str] = "fake"
    version: ClassVar[int] = 1

    def __init__(self, *, mode: str) -> None:
        self._mode = mode
        self.calls: list[str] = []

    def declared_capabilities(self, target: BackendTarget) -> ModelCapabilities:
        return HONEST_CAPS

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
        if self._mode == "dead":
            raise TransportError(f"{target.model_id} refused", trigger="CONNECTION")
        return BackendReply(  # never validates: forces the repair budget to be spent
            text=json.dumps({"not_the_schema": 1}),
            usage=TokenUsage(input_tokens=10, output_tokens=3, model_id=target.model_id),
            finish_reason="stop",
        )


def _target(model_id: str) -> BackendTarget:
    return BackendTarget(
        backend="fake", model_id=model_id, price=Price(in_per_mtok=1.0, out_per_mtok=2.0)
    )


def _router(*model_ids: str) -> LlmRouter:
    targets = tuple(_target(m) for m in model_ids)
    return LlmRouter(dict(SPEC_ROLE_TIERS), dict.fromkeys(ModelTier, targets), profile="test")


@asynccontextmanager
async def _context(
    tmp_path: Path,
    *,
    config: FleetConfig,
    backend: LoggingBackend,
    router: LlmRouter,
    llm_policy: CallPolicy | None = None,
) -> AsyncIterator[RunContext]:
    """A real `RunContext`, assembled with the **same kwarg set every `RunContext(` site in
    `cli.py` passes** — which notably does not include `llm_policy`. `llm_policy` is threaded here
    only so the override arm can be exercised too."""
    path = tmp_path / "state" / "fleet.db"
    await initialize_database(path)
    async with StateWriter(path, owner="test-llm-policy") as writer:
        read_conn = await connect_ro(path)
        try:
            repo = SqliteStateRepository(writer=writer, read_conn=read_conn)
            await repo.upsert_run(
                RUN, started_at=NOW, config_sha256="b" * 64, harness_version="0.1.0"
            )
            await repo.open_budget_ledger(RUN, max_usd=100.0, now=NOW)
            ledger = CostLedger(
                repo,
                run_id=RUN,
                ceilings=Ceilings.from_settings(config.budgets, config.stubs),
                clock=lambda: NOW,
            )
            kwargs: dict[str, Any] = {}
            if llm_policy is not None:
                kwargs["llm_policy"] = llm_policy
            yield RunContext(
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
                log=default_logger("test.llm-policy"),
                work_dir=tmp_path / "work",
                lease_owner="test-host:test-cid:1:boot",
                clock=lambda: NOW,
                harness_version="0.1.0",
                **kwargs,
            )
        finally:
            await read_conn.close()


def _config(**llm_overrides: Any) -> FleetConfig:
    """`FleetConfig` with the §9 `llm:` block an operator would have written."""
    base = FleetConfig()
    failover = base.llm.failover.model_copy(
        update={k: v for k, v in llm_overrides.items() if hasattr(base.llm.failover, k)}
    )
    llm = base.llm.model_copy(
        update={
            **{k: v for k, v in llm_overrides.items() if hasattr(base.llm, k)},
            "failover": failover,
        }
    )
    return base.model_copy(update={"llm": llm})


async def _walk(ctx: RunContext) -> None:
    await ctx.model_client.complete(ROLE, [Message(role="user", content="go")], Verdict)


@pytest.fixture(autouse=True)
def _release_slot() -> AsyncIterator[None]:
    yield
    dbmod._release_write_slot()


# ======================================================================================
# DISCRIMINATING — each fails if `RunContext` goes back to handing the client `None`
# ======================================================================================


async def test_failover_max_targets_per_call_bounds_the_walk(tmp_path: Path) -> None:
    """§9: "a single call never walks more than this many targets".

    DISCRIMINATES against the unassigned state: `CallPolicy()`'s default is 3, so the mutation
    walks all three targets and this asserts one. The tier deliberately offers THREE targets and
    the config allows ONE — two anchors that must differ, or the defect is a semantic no-op.
    """
    backend = LoggingBackend(mode="dead")
    async with _context(
        tmp_path,
        config=_config(max_targets_per_call=1),
        backend=backend,
        router=_router("t1", "t2", "t3"),
    ) as ctx:
        with pytest.raises(TierUnavailable) as raised:
            await _walk(ctx)
    assert backend.calls == ["t1"], (
        "llm.failover.max_targets_per_call: 1 must stop the ladder after ONE target; walking "
        f"{backend.calls} is CallPolicy()'s default of 3, i.e. the config reached nothing"
    )
    assert raised.value.targets_tried == ("fake:t1",)


async def test_failover_disabled_uses_only_the_first_target(tmp_path: Path) -> None:
    """§9: "false ⇒ a tier uses only its first target; a dead target is fatal".

    DISCRIMINATES, and against a strictly different mutation from the case above: here
    `max_targets_per_call` is left at its §9 default of 3, so dropping the `enabled` gate alone —
    while still reading `max_targets_per_call` from config — walks three targets and fails only
    this test.
    """
    backend = LoggingBackend(mode="dead")
    async with _context(
        tmp_path,
        config=_config(enabled=False),
        backend=backend,
        router=_router("t1", "t2", "t3"),
    ) as ctx:
        with pytest.raises(TierUnavailable):
            await _walk(ctx)
    assert backend.calls == ["t1"], (
        "llm.failover.enabled: false must make the tier use only its first target; "
        f"walked {backend.calls}"
    )


async def test_max_schema_repairs_zero_spends_no_repair(tmp_path: Path) -> None:
    """§9: "0 disables repair entirely"; §11.8 trigger 4 names `llm.max_schema_repairs`.

    DISCRIMINATES: `CallPolicy()`'s default is 1, so the mutation re-asks the single target once
    and this asserts it is asked exactly once in total. One target only, so the count cannot be
    confused with a failover hop.
    """
    backend = LoggingBackend(mode="invalid")
    async with _context(
        tmp_path,
        config=_config(max_schema_repairs=0),
        backend=backend,
        router=_router("only"),
    ) as ctx:
        with pytest.raises((SchemaUnsatisfied, TierUnavailable)):
            await _walk(ctx)
    assert backend.calls == ["only"], (
        "llm.max_schema_repairs: 0 must buy no repair re-ask; "
        f"{len(backend.calls)} calls means CallPolicy()'s default of 1 was used"
    )


# ======================================================================================
# NON-DISCRIMINATING for the unassigned state — kept for the OTHER direction
# ======================================================================================


async def test_an_explicit_policy_still_overrides_the_config(tmp_path: Path) -> None:
    """Does NOT discriminate against the unassigned state — it passes under that mutation, because
    the explicit arm is the arm that always worked.

    It discriminates against the opposite mutation: deriving from config unconditionally and
    ignoring an injected `llm_policy`. `tests/test_llm_findings.py` depends on that arm, and the
    two values are deliberately far apart (config says 1, the injection says 3) so the assertion
    cannot be satisfied by either default.
    """
    backend = LoggingBackend(mode="dead")
    async with _context(
        tmp_path,
        config=_config(max_targets_per_call=1),
        backend=backend,
        router=_router("t1", "t2", "t3"),
        llm_policy=CallPolicy(max_targets_per_call=3),
    ) as ctx:
        with pytest.raises(TierUnavailable):
            await _walk(ctx)
    assert backend.calls == ["t1", "t2", "t3"], (
        f"an explicitly injected CallPolicy must win over config; walked {backend.calls}"
    )
