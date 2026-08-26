"""SPEC §11.6 / §11.2 / D62 — `attempts.llm_cache_hit`, and why it is ALL-hit.

`state/schema.sql`'s column comment is an implication, not a label: `1 => cost_usd = 0`. Since
`workers/base.py::accumulate` **sums** `cost_usd`, an attempt that made three LLM calls and
missed on one has `cost_usd > 0`, so an any-hit flag would publish a row contradicting the
schema. That is the whole content of "ALL-hit": it is derived from the schema, not chosen.

The signal is carried on `TokenUsage` — the only object that already flows from the call that
made it to the `attempts` row that bills it — as two summed counters rather than one boolean.
The counters are load-bearing: `accumulate` is used as a running fold seeded with a zero
`TokenUsage()` at five sites (measured in `test_two_replayed_calls_accumulate_to_one_all_hit_
attempt` below), so a boolean AND over that seed is `False` for every attempt those sites
produce, and a boolean OR is any-hit.

Which mutation reddens which case (Rule 12). The battery, its zero-change gates and its cosmetic
control live in `.superpowers/sdd/handoff-round-h/lanes/w2/mutate_w2.py`; every gate is read
before the result and the reflow control stays green. Every case below is the **unique**
discriminator of at least one mutation — that is the whole membership rule, and two cases that
could not meet it were deleted rather than kept (their verdicts are in that lane's report).

* zero-lookup case      -- M1 ONLY. Drop `llm_cache_lookups > 0` from the property: `all()` over
                           nothing is `True`, so every rung that never called an LLM
                           self-reports as fully cached.
* two-replays case      -- M14 ONLY. M14 is the ALL-hit rule written as an `all()` over the
                           fold's arguments instead of as a comparison of two sums — i.e. the
                           `AND` formulation, in the counters' own vocabulary. It is green
                           everywhere else and red here, because this is the only case whose
                           fold includes the zero `TokenUsage()` seed as a real argument. That
                           is the measurement behind ADR-0094's claim that `AND` is
                           unimplementable, and deleting this case deletes the evidence for it.
                           Also red under M3/M4.
* real-client case      -- M5 ONLY (stop counting the MISS) and M6 ONLY (stop stamping the hit
                           in `_replay`). Also red under M2.
* attempt-writer case   -- M9 ONLY (spread the flag onto every step row). Also red under
                           M7/M8/M12/M13. (M8, a constant `False` inside `_AttemptWriter.record`,
                           was unique to this case until the build/verify case below was added;
                           it now reddens both, because both reach the same writer. Recorded so
                           the matrix is not read as older than it is.)
* transform-sink case   -- M10 ONLY (constant `False`) and M11 ONLY (derive the flag from
                           `cost_usd == 0`, which only the free-local sub-case catches; the
                           other two sub-cases are blind to it by construction). Also red under
                           M1-M4, M7, M12, M13.
* build/verify-sink     -- M15 ONLY (`_BuildSink.__call__` passes a constant) and M16 ONLY
                           (`_VerifySink.__call__` passes a constant). Added after a reviewer
                           measured both mutations as INEXPRESSIBLE against the fixtures that
                           existed: they left this file, `test_cli.py`, `test_runner.py` and
                           `test_workers_build.py` entirely green. A mutation no fixture can
                           express reports a pass that means nothing.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
import pytest
from pydantic import BaseModel

from fleet.cli import (
    BuildOutput,
    TransformOutput,
    VerifyOutput,
    _AttemptWriter,
    _BuildEvidence,
    _BuildSink,
    _TransformEvidence,
    _TransformSink,
    _VerifyEvidence,
    _VerifySink,
)
from fleet.llm.cache import CachingModelClient, MemoryLlmCacheStore
from fleet.llm.calls import render_prompt
from fleet.llm.client import (
    CallBudget,
    Message,
    ModelCapabilities,
    ModelResponse,
    StreamEvent,
)
from fleet.llm.roles import LlmRouter, Role
from fleet.llm.schemas import RepoClassification
from fleet.models.enums import ModelTier, Phase, StructuredOutputMode
from fleet.models.tasks import BackendTarget, Price, TokenUsage
from fleet.state import db as dbmod
from fleet.state.db import StateWriter, connect_ro, initialize_database
from fleet.state.repository import SqliteStateRepository
from fleet.workers.base import WorkerResult, accumulate
from fleet.workers.buildverify import StepRecord

RUN = "run-w2-cachehit"
REPO = "acme-commons"
NOW = datetime(2026, 8, 25, 12, 0, 0, tzinfo=UTC)


def hit_usage(cost_usd: float = 0.0) -> TokenUsage:
    """A `TokenUsage` shaped exactly as `cache._replay` returns one."""
    return TokenUsage(
        role="repo.classify",
        backend="anthropic",
        model_id="cheap-a",
        input_tokens=1_000,
        output_tokens=200,
        cost_usd=cost_usd,
        llm_cache_lookups=1,
        llm_cache_hits=1,
    )


def miss_usage(cost_usd: float = 0.002) -> TokenUsage:
    """A `TokenUsage` shaped as `CachingModelClient.complete` returns one on a miss."""
    return TokenUsage(
        role="repo.classify",
        backend="anthropic",
        model_id="cheap-a",
        input_tokens=1_000,
        output_tokens=200,
        cost_usd=cost_usd,
        llm_cache_lookups=1,
        llm_cache_hits=0,
    )


_ROLE = str(Role.REPO_CLASSIFY)
_MESSAGES = render_prompt(Role.REPO_CLASSIFY, {"repo_id": REPO, "files": ["pom.xml"]})
_TARGET = BackendTarget(
    backend="anthropic",
    model_id="cheap-a",
    effort="low",
    price=Price(in_per_mtok=1.0, out_per_mtok=5.0),
)
_ANSWER = RepoClassification(
    ecosystem="maven", is_library=True, confidence=0.9, rationale="pom.xml declares a groupId"
)


def _router() -> LlmRouter:
    return LlmRouter({_ROLE: ModelTier.CHEAP}, {ModelTier.CHEAP: (_TARGET,)}, required_roles=())


class _CountingClient:
    """A `ModelClient` with no transport at all: if the cache misses when it should hit, `calls`
    says so. Built here rather than imported from a sibling test module — nothing enforces a
    borrowed fixture's shape, and this one has to stamp NO cache counters for the assertion below
    to be about `CachingModelClient` rather than about the fake."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def complete[T: BaseModel](
        self,
        role: str,
        messages: Sequence[Message],
        response_model: type[T],
        *,
        tier_override: ModelTier | None = None,
        max_output_tokens: int | None = None,
        timeout_s: float | None = None,
        budget: CallBudget | None = None,
    ) -> ModelResponse[T]:
        self.calls.append(role)
        return ModelResponse(
            value=response_model.model_validate_json(_ANSWER.model_dump_json()),
            usage=TokenUsage(
                role=role,
                tier=ModelTier.CHEAP,
                backend=_TARGET.backend,
                model_id=_TARGET.model_id,
                input_tokens=1_000,
                output_tokens=200,
                cost_usd=0.002,
            ),
            mode=StructuredOutputMode.JSON_SCHEMA,
            finish_reason="stop",
        )

    def stream[T: BaseModel](
        self,
        role: str,
        messages: Sequence[Message],
        response_model: type[T],
        *,
        budget: CallBudget | None = None,
    ) -> AsyncIterator[StreamEvent]:
        raise NotImplementedError

    async def capabilities(self, role: str) -> ModelCapabilities:
        return ModelCapabilities()


# ---------------------------------------------------------------------------------------------
# the derivation
# ---------------------------------------------------------------------------------------------


def test_an_attempt_that_consulted_the_cache_zero_times_is_not_a_hit() -> None:
    """A DETERMINISTIC rung, an `--llm-cache off` run and a default `TokenUsage()` all arrive with
    no lookups, and `all()` over nothing is `True`.

    Why it matters, and why this is not a formality: without the `llm_cache_lookups > 0` guard the
    column becomes `1` on every rung that never dispatched an LLM call at all — the exact
    `cost_usd == 0` conflation `models/tasks.py` and §11.2 exist to forbid, arriving through the
    property instead of through the ledger.
    """
    assert TokenUsage().all_served_from_llm_cache is False
    assert TokenUsage(backend="openai_compatible", cost_usd=0.0).all_served_from_llm_cache is False


def test_two_replayed_calls_accumulate_to_one_all_hit_attempt() -> None:
    """The zero `TokenUsage()` seed is passed EXPLICITLY, and that is the point of the case.

    Why: `accumulate` is used as a running fold seeded with a zero `TokenUsage()`. Measured with
    `ast`, per enclosing function rather than by line proximity: **7** functions in `src/fleet/`
    seed `usage = TokenUsage()`, and **5** of those go on to fold it with `accumulate(usage, ...)`
    — `base.py::execute`, `buildgen.py::run` and `::_module_bazel`, `prwriter.py::_compose`,
    `rewrite.py::run`. (`buildverify.py::run` and `prwriter.py::run` seed without folding; the two
    numbers are different quantities and must not be reported as one.) So the seed is not a
    fixture convenience, it is the shape of every attempt those five produce.

    Any formulation of ALL-hit that asks whether *every argument* of the fold was a hit — a
    boolean `AND`, in any spelling — answers `False` here, because the seed is a hit of nothing.
    Two summed counters compared afterwards have no identity element to poison.
    ADR-0094 rests on this case; it is why the flag is not a bool.
    """
    attempt = accumulate(TokenUsage(), hit_usage(), hit_usage())

    assert attempt.llm_cache_lookups == 2, "the zero seed contributes no lookup"
    assert attempt.llm_cache_hits == 2
    assert attempt.cost_usd == pytest.approx(0.0)
    assert attempt.all_served_from_llm_cache is True


# ---------------------------------------------------------------------------------------------
# the real client — the counters as the cache itself sets them
# ---------------------------------------------------------------------------------------------


def test_the_real_client_counts_the_miss_it_served_from_the_backend() -> None:
    """One miss then one hit, through a real `CachingModelClient` and a real store.

    Why the miss must be counted: without it a two-call attempt reports one lookup and one hit and
    is indistinguishable from a single fully-cached call, so the first call's money vanishes from
    the flag while staying in `cost_usd` — a row that says "free from cache" over a real charge.
    """
    inner = _CountingClient()
    client = CachingModelClient(inner, _router(), MemoryLlmCacheStore(), now=lambda: NOW)
    first = asyncio.run(client.complete(_ROLE, _MESSAGES, RepoClassification))
    second = asyncio.run(client.complete(_ROLE, _MESSAGES, RepoClassification))

    assert inner.calls == [_ROLE], "the second call must not reach the backend"
    assert (first.usage.llm_cache_lookups, first.usage.llm_cache_hits) == (1, 0)
    assert (second.usage.llm_cache_lookups, second.usage.llm_cache_hits) == (1, 1)
    assert second.usage.all_served_from_llm_cache is True

    attempt = accumulate(first.usage, second.usage)
    assert attempt.all_served_from_llm_cache is False, "the attempt paid for its first call"
    assert attempt.cost_usd > 0.0


# ---------------------------------------------------------------------------------------------
# the writer and the reader
# ---------------------------------------------------------------------------------------------


_Wired = tuple[StateWriter, aiosqlite.Connection, SqliteStateRepository]


@pytest.fixture(autouse=True)
def _clean_write_slot() -> AsyncIterator[None]:
    yield
    dbmod._release_write_slot()


@pytest.fixture
async def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "state" / "fleet.db"
    await initialize_database(path)
    return path


@pytest.fixture
async def wired(db_path: Path) -> AsyncIterator[_Wired]:
    """One writer and one `mode=ro` handle, wired as the runner wires them (§11.5)."""
    async with StateWriter(db_path, owner="w2-test-writer") as writer:
        read_conn = await connect_ro(db_path)
        try:
            repo = SqliteStateRepository(writer=writer, read_conn=read_conn)
            await repo.upsert_run(
                RUN, started_at=NOW, config_sha256="a" * 64, harness_version="0.1.0"
            )
            await repo.upsert_repo(
                REPO, name=REPO, url=f"https://example.invalid/{REPO}.git", now=NOW
            )
            yield writer, read_conn, repo
        finally:
            await read_conn.close()


# ---------------------------------------------------------------------------------------------
# the producers
# ---------------------------------------------------------------------------------------------


async def test_the_transform_sink_derives_the_flag_from_the_usage_it_bills(
    wired: _Wired,
) -> None:
    """Three usages through the real sink: all-hit, one-miss, and a free LOCAL call.

    Why the third is here: it is the only one that discriminates a sink deriving the flag from
    `cost_usd == 0`. A `price: free` target spends `0.0` with the ledger fully live (§11.2), so
    under that defect a local profile reports every row as a cache hit — which is exactly the
    conflation §12.44 asserts against and the reason the column exists at all.
    """
    writer, read_conn, repo = wired
    sink = _TransformSink(
        writer=writer,
        repository=repo,
        read_conn=read_conn,
        run_id=RUN,
        evidence=_TransformEvidence(),
        clock=lambda: NOW,
    )
    free_local = TokenUsage(backend="openai_compatible", model_id="local-cheap", cost_usd=0.0)

    cases = {
        1: accumulate(hit_usage(), hit_usage()),
        2: accumulate(hit_usage(), miss_usage()),
        3: accumulate(free_local, free_local),
    }
    for attempt, usage in cases.items():
        await sink(
            repo_id=REPO,
            phase=Phase.TRANSFORM,
            fence=1,
            result=WorkerResult[TransformOutput](
                status="ok",
                output=TransformOutput(repo_id=REPO, attempt=attempt),
                usage=usage,
            ),
        )

    rows = {row.attempt: row for row in [r async for r in repo.iter_attempts(RUN)]}
    assert rows[1].llm_cache_hit is True and rows[1].cost_usd == pytest.approx(0.0)
    assert rows[2].llm_cache_hit is False and rows[2].cost_usd > 0.0
    assert rows[3].llm_cache_hit is False and rows[3].cost_usd == pytest.approx(0.0), (
        "a free local call costs 0 and is NOT a cache hit"
    )


async def test_the_attempt_writer_puts_the_flag_on_the_row_that_carries_the_cost(
    wired: _Wired,
) -> None:
    """A rung is many step rows but one LLM spend, and the sink already attributes that spend to
    the FIRST row. The flag must ride the same row.

    Why: `schema.sql` couples the two (`1 => cost_usd = 0`). Spreading the flag over every step
    row would flag rows whose `cost_usd` is 0 only because the first row took it — each of which
    then reads as a cached step that never made an LLM call.
    """
    writer, read_conn, repo = wired
    sink = _AttemptWriter(
        writer=writer,
        repository=repo,
        read_conn=read_conn,
        run_id=RUN,
        clock=lambda: NOW,
    )
    written = await sink.record(
        repo_id=REPO,
        phase=Phase.BUILD,
        attempt=1,
        tier="DETERMINISTIC",
        context_policy=None,
        integration_ref="",
        steps=[
            StepRecord(unit="build", command=["bazel", "build", "//..."], exit_code=0, ok=True),
            StepRecord(unit="test", command=["bazel", "test", "//..."], exit_code=0, ok=True),
        ],
        error=None,
        cost_usd=0.0,
        llm_cache_hit=True,
    )

    rows = {row.attempt_id: row for row in [r async for r in repo.iter_attempts(RUN)]}
    assert len(written) == 2
    assert rows[written[0]].llm_cache_hit is True
    assert rows[written[1]].llm_cache_hit is False, (
        "the second step row carries no cost, so it must carry no cache flag either"
    )


async def test_the_build_and_verify_sinks_forward_the_flag_they_were_billed_on(
    wired: _Wired,
) -> None:
    """The two producers that reach `_AttemptWriter` through a `WorkerResult` rather than a kwarg.

    Why this case exists and why the attempt-writer case above cannot stand in for it: that case
    passes `llm_cache_hit=True` **straight into** `_AttemptWriter.record`, so it never observes
    where the argument came from. A reviewer mutated both sinks to a constant `False` and every
    fixture in this file, plus `test_cli.py`, `test_runner.py` and `test_workers_build.py`, stayed
    green — the defect was **inexpressible**, which is Rule 12's "audit mutations for
    expressibility, not only for pass/fail". This case makes it expressible: each sink is driven
    with a real `WorkerResult` whose usage is all-hit, and the row it wrote is read back off disk.

    Reddened by M15 (`_BuildSink.__call__` passes a constant) and M16 (`_VerifySink.__call__`
    passes a constant), each of which reddens no other case.
    """
    writer, read_conn, repo = wired
    attempts = _AttemptWriter(
        writer=writer, repository=repo, read_conn=read_conn, run_id=RUN, clock=lambda: NOW
    )
    all_hit = accumulate(hit_usage(), hit_usage())
    step = StepRecord(unit="build", command=["bazel", "build", "//..."], exit_code=0, ok=True)

    build = _BuildSink(
        attempts=attempts, writer=writer, run_id=RUN, evidence=_BuildEvidence(), clock=lambda: NOW
    )
    await build(
        repo_id=REPO,
        phase=Phase.BUILD,
        fence=1,
        result=WorkerResult[BuildOutput](
            status="ok",
            output=BuildOutput(repo_id=REPO, attempt=1, steps=[step]),
            usage=all_hit,
        ),
    )

    verify = _VerifySink(
        attempts=attempts, evidence=_VerifyEvidence(), writer=writer, run_id=RUN, clock=lambda: NOW
    )
    await verify(
        repo_id=REPO,
        phase=Phase.VERIFY,
        fence=1,
        result=WorkerResult[VerifyOutput](
            status="ok",
            output=VerifyOutput(repo_id=REPO, attempt=1, steps=[step]),
            usage=all_hit,
        ),
    )

    rows = {row.phase: row async for row in repo.iter_attempts(RUN)}
    assert rows[Phase.BUILD].llm_cache_hit is True, (
        "_BuildSink must derive the flag from the usage it billed, not pass a constant"
    )
    assert rows[Phase.VERIFY].llm_cache_hit is True, (
        "_VerifySink must derive the flag from the usage it billed, not pass a constant"
    )
