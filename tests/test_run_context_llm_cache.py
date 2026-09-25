"""§11.6's LLM response cache is actually installed on a shipped run (§11.6, §12 item 21).

`RunContext.llm_cache` was declared and consumed and **never assigned**: `__post_init__` wrapped
the ladder client in `CachingModelClient` only `if self.llm_cache is not None`, and all five
`RunContext(` sites in `cli.py` pass nothing. No `LlmCacheStore` was constructed anywhere in
`src/` at all, so the branch was dead at every site — the same call, issued twice on a shipped
run, was billed twice and wrote no `llm_cache` row, while `--llm-cache read-only` (§11.6's replay
mode, whose whole point is that a miss is a hard error) could not raise `CacheMiss` because
nothing built the client that raises it. `fleet gc --cache-max-age` was evicting from a table no
production path had ever written to.

Every assertion here is **a call log read off the backend and a row count read out of the
database** — never `ctx.llm_cache is not None` and never `isinstance(ctx.model_client, ...)` on
its own. A field or type comparison is precisely the assertion that would have passed throughout
the entire period the cache was dead, because a wrapped client that is never consulted and an
unwrapped one are indistinguishable by inspection; they differ only in what the backend is asked
and what lands on disk.

The fixture deliberately passes **no** `llm_cache` and **no** `llm_cache_mode`. Those two fields
are the subject; a test that injected either would exercise the override arm and be blind by
construction to whether the derived arm exists — which is exactly how `tests/test_llm_cache.py`,
which drives `CachingModelClient` and both stores hard, stayed green for the whole life of the
defect.

Nothing here opens a socket: real temp database, real `StateWriter`, real `SqliteLlmCacheStore`
built by `__post_init__` itself, real `RunContext` assembly, `FleetConfig` built in-process,
scripted offline backend.
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

from fleet.llm.cache import CacheMiss, CachingModelClient
from fleet.llm.client import BackendReply, CallBudget, Message, StructuredOutputMode
from fleet.llm.roles import SPEC_ROLE_TIERS, LlmRouter
from fleet.models.enums import ModelTier, Phase
from fleet.models.tasks import BackendTarget, ModelCapabilities, Price, TokenUsage
from fleet.orchestrator.budgets import Ceilings, CostLedger, Limits
from fleet.orchestrator.context import RunContext, default_logger
from fleet.settings import FleetConfig
from fleet.state import db as dbmod
from fleet.state.db import StateWriter, connect_ro, initialize_database
from fleet.state.repository import SqliteStateRepository

NOW = datetime(2026, 8, 25, 9, 0, 0, tzinfo=UTC)
RUN_ID = UUID("33333333-3333-4333-8333-333333333333")
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
    """One offline transport that answers schema-valid. Every `invoke` is recorded, and the
    record is what every test here asserts on: a cache hit is invisible at the `ModelClient`
    boundary by design — that is `CachingModelClient`'s own stated contract (its class docstring
    in `src/fleet/llm/cache.py`), and the one difference §11.6 intends is cost (`cost_usd = 0`),
    not content. So the only honest instrument is the question the backend was or was not
    asked."""

    name: ClassVar[str] = "fake"
    version: ClassVar[int] = 1

    def __init__(self) -> None:
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
        return BackendReply(
            text=json.dumps({"summary": "ok"}),
            usage=TokenUsage(input_tokens=10, output_tokens=3, model_id=target.model_id),
            finish_reason="stop",
        )


def _router() -> LlmRouter:
    targets = (
        BackendTarget(
            backend="fake", model_id="only", price=Price(in_per_mtok=1.0, out_per_mtok=2.0)
        ),
    )
    return LlmRouter(dict(SPEC_ROLE_TIERS), dict.fromkeys(ModelTier, targets), profile="test")


def _config(**llm_overrides: Any) -> FleetConfig:
    """`FleetConfig` with the §9 `llm:` block an operator would have written."""
    base = FleetConfig()
    return base.model_copy(update={"llm": base.llm.model_copy(update=llm_overrides)})


@asynccontextmanager
async def _context(
    tmp_path: Path,
    *,
    config: FleetConfig,
    backend: LoggingBackend,
    llm_cache_mode: str | None = None,
) -> AsyncIterator[RunContext]:
    """A real `RunContext`.

    The kwarg set is the one all five `RunContext(` sites in `cli.py` pass, plus exactly the two
    an offline test must inject — `backends`, so no socket is opened, and `lease_owner`, so the
    row does not carry this host's name. It passes **no** `llm_cache` and **no** `llm_cache_mode`
    unless a case is deliberately exercising the explicit-override arm, because those two fields
    being unpassed at every production site is the whole condition under test.
    """
    path = tmp_path / "state" / "fleet.db"
    await initialize_database(path)
    async with StateWriter(path, owner="test-llm-cache") as writer:
        read_conn = await connect_ro(path)
        try:
            repo = SqliteStateRepository(writer=writer, read_conn=read_conn)
            await repo.upsert_run(
                RUN, started_at=NOW, config_sha256="c" * 64, harness_version="0.1.0"
            )
            await repo.open_budget_ledger(RUN, max_usd=100.0, now=NOW)
            ledger = CostLedger(
                repo,
                run_id=RUN,
                ceilings=Ceilings.from_settings(config.budgets, config.stubs),
                clock=lambda: NOW,
            )
            kwargs: dict[str, Any] = {}
            if llm_cache_mode is not None:
                kwargs["llm_cache_mode"] = llm_cache_mode
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
                llm=_router(),
                backends={"fake": backend},
                log=default_logger("test.llm-cache"),
                work_dir=tmp_path / "work",
                lease_owner="test-host:test-cid:1:boot",
                clock=lambda: NOW,
                harness_version="0.1.0",
                **kwargs,
            )
        finally:
            await read_conn.close()


async def _ask(ctx: RunContext) -> None:
    """The call surface every case here drives. `ctx.model_client` is the object
    `RunContext.worker_context()` hands down as `WorkerContext.llm` — but that is an
    implementation fact (`orchestrator/context.py`, the `llm=` argument of the `WorkerContext`
    it builds). An earlier version of this docstring said `tests/test_runner.py` asserted that
    identity; it does not, and never did — that file drives `ctx.llm` behaviourally and names
    `model_client` nowhere.

    So driving `model_client` rather than a worker fixture keeps this file free of the worker
    machinery, and `test_the_client_handed_to_a_worker_is_the_one_this_file_drives` below is
    what makes that substitution legitimate instead of assumed. Without it, re-routing the
    hand-down to an unwrapped client would leave every case here green while no worker on any
    shipped run consulted the cache."""
    await ctx.model_client.complete(ROLE, [Message(role="user", content="go")], Verdict)


async def _rows(ctx: RunContext) -> int:
    async with ctx.read_conn.execute("SELECT COUNT(*) FROM llm_cache") as cursor:
        row = await cursor.fetchone()
    assert row is not None
    return int(row[0])


@pytest.fixture(autouse=True)
def _release_slot() -> AsyncIterator[None]:
    yield
    dbmod._release_write_slot()


# ======================================================================================
# The defect itself: the shipped assembly billed twice and wrote nothing
# ======================================================================================


async def test_two_identical_calls_bill_the_backend_once_and_write_one_row(
    tmp_path: Path,
) -> None:
    """§11.6 makes the cache — not a sampling control the harness does not pin — the thing that
    lets a run be re-run and reviewed; `fleet.llm.cache`'s module docstring states the same rule
    beside the code that implements it. The second identical call must be served from
    `llm_cache`.

    DISCRIMINATES against the unassigned state directly, and it is the assertion the old
    `tests/test_cli.py::test_llm_cache_flag_reaches_the_caching_client` could not make: delete
    `__post_init__`'s cache branch and the backend is asked twice and no row lands, which is what
    every shipped run actually did. The two anchors that must differ are the two `complete()`
    calls' *identity* — same role, same messages, same response model — so a miss on the second
    is a real miss and not a different key.
    """
    backend = LoggingBackend()
    async with _context(tmp_path, config=_config(), backend=backend) as ctx:
        assert isinstance(ctx.model_client, CachingModelClient), (
            "the type is checked only to make a failure legible; the two assertions below are "
            "the ones that carry the property"
        )
        await _ask(ctx)
        await _ask(ctx)
        written = await _rows(ctx)
    assert backend.calls == ["only"], (
        "two IDENTICAL calls must reach the backend once: the second is a cache hit. "
        f"{backend.calls} means no cache was consulted — the shipped defect"
    )
    assert written == 1, f"the first call must persist one llm_cache row; found {written}"


# ======================================================================================
# The mode is RESOLVED from config, not defaulted — each case a different mutation
# ======================================================================================


async def test_cache_mode_off_in_fleet_yaml_bills_every_call_and_writes_nothing(
    tmp_path: Path,
) -> None:
    """§11.6's `off` exists so an operator can deliberately re-roll a decision.

    DISCRIMINATES against the mode never being derived from config — against
    `llm_cache_mode` keeping a non-optional `= "read-write"` default, which is the `effort: low`
    failure shape: the operator wrote `off` and the field's own default silently overrides it.
    Under that mutation the second call is served from cache and this fails. It is blind to the
    branch-deleted mutation above, which also gives 2 calls and 0 rows — which is why the case
    above exists separately.
    """
    backend = LoggingBackend()
    async with _context(tmp_path, config=_config(cache_mode="off"), backend=backend) as ctx:
        await _ask(ctx)
        await _ask(ctx)
        written = await _rows(ctx)
    assert backend.calls == ["only", "only"], (
        f"llm.cache_mode: off must re-roll every call; {backend.calls} means the config value "
        "never reached CachingModelClient"
    )
    assert written == 0, f"`off` must write no llm_cache row; found {written}"


async def test_cache_mode_read_only_in_fleet_yaml_makes_a_miss_fatal(tmp_path: Path) -> None:
    """§11.6's `read-only` mode raises on a miss instead of falling through to a backend — the
    property that lets a re-run's "no new model output" be checked rather than asserted
    (§12 item 21).

    DISCRIMINATES uniquely: this is the only case where the *absence* of a fresh backend call is
    the property. Under any mutation that leaves the mode at `read-write` — the config not read,
    or the field's old default restored — the call simply succeeds against the backend and no
    `CacheMiss` is raised. It is also the only case that fails if the cache is wired but
    `CacheMiss` never reaches the caller.
    """
    backend = LoggingBackend()
    async with _context(tmp_path, config=_config(cache_mode="read-only"), backend=backend) as ctx:
        with pytest.raises(CacheMiss):
            await _ask(ctx)
        written = await _rows(ctx)
    assert backend.calls == [], (
        f"llm.cache_mode: read-only must not fall through to the backend; called {backend.calls}"
    )
    assert written == 0


# ======================================================================================
# The other direction — an explicit field still wins over config
# ======================================================================================


async def test_an_explicit_llm_cache_mode_still_overrides_the_config(tmp_path: Path) -> None:
    """The UNIQUE discriminator against resolving the mode from `config.llm` unconditionally and
    dropping the explicit field: the two values are deliberately opposed (config says `off`, the
    injection says `read-write`), so the assertion cannot be satisfied by either one alone.

    It also reddens under the pre-fix guard being restored, and under the unconditional wrap
    being deleted. An earlier version of
    this docstring said the case "does NOT discriminate against the unassigned state"; that is
    false, because the fixture passes `llm_cache_mode` but deliberately **not** `llm_cache`, so
    the pre-fix `if self.llm_cache is not None:` never wrapped and the mode never arrived. The
    claim is corrected here rather than left standing, since a later lane pruning
    "non-discriminating" cases would have deleted a case that discriminates three ways.
    """
    backend = LoggingBackend()
    async with _context(
        tmp_path,
        config=_config(cache_mode="off"),
        backend=backend,
        llm_cache_mode="read-write",
    ) as ctx:
        await _ask(ctx)
        await _ask(ctx)
        written = await _rows(ctx)
    assert backend.calls == ["only"], (
        f"an explicit llm_cache_mode must win over config.llm.cache_mode; called {backend.calls}"
    )
    assert written == 1


# ======================================================================================
# The hand-down: the object a worker calls is the object this file drives
# ======================================================================================


async def test_the_client_handed_to_a_worker_is_the_one_this_file_drives(
    tmp_path: Path,
) -> None:
    """`WorkerContext.llm` must BE `RunContext.model_client`, not a second client.

    Why it matters, and why it is here rather than in `tests/test_runner.py`: every other case
    in this file drives `ctx.model_client` (see `_ask`), while a real worker only ever touches
    `WorkerContext.llm`. Nothing in the tree asserted those were the same object — the token
    `model_client` does not occur in `tests/test_runner.py` at all — so the substitution the
    whole file rests on was an unchecked implementation fact.

    UNIQUELY DISCRIMINATES against re-routing the hand-down: keep `__post_init__`'s wrap but
    hand a worker the pre-cache ladder client (`llm=self.model_client._inner`). Measured: every
    other case in this file still passes under it — they call `model_client` directly — and all
    of `tests/test_runner.py` still passes, because the ladder client completes perfectly well.
    Only this assertion moves, and what it catches is D79 returning through the one door this
    file does not otherwise use: no worker on a shipped run consulting the cache.

    It also reddens under the pre-fix guard being restored and under the wrap being deleted (via
    the `isinstance` half). It stays GREEN under the mode-derivation mutations — measured
    against the non-optional `llm_cache_mode` default — which leave the identity intact.
    """
    async with _context(tmp_path, config=_config(), backend=LoggingBackend()) as ctx:
        worker = ctx.worker_context(
            repo_id="repo-a",
            phase=Phase.TRANSFORM,
            attempt=1,
            lease_fence=1,
            cancel=asyncio.Event(),
            budget=CallBudget(
                remaining_tokens=100_000,
                remaining_usd=5.0,
                deadline=asyncio.get_running_loop().time() + 60,
            ),
        )

        # ADR-0149 (2026-09-25): the hand-down is now a `for_repo` VIEW of `ctx.model_client`
        # (bound to the worker's repo for §12.51 replica affinity), so object identity no longer
        # holds by design. What the identity guarded is restated component-wise: the SAME cache
        # store and the SAME ladder client state (its breaker), wrapped by the caching layer.
        assert isinstance(worker.llm, CachingModelClient), (
            "a worker must be handed the cache-wrapped client; an unwrapped ladder client here "
            "means the cache is installed on a surface no worker uses"
        )
        assert isinstance(ctx.model_client, CachingModelClient)
        assert worker.llm._store is ctx.model_client._store, (
            "a worker must be handed a view of the SAME assembled client this file drives, not a "
            "second client with its own store"
        )
        assert worker.llm._inner._health is ctx.model_client._inner._health  # type: ignore[attr-defined]
