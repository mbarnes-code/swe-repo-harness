"""SPEC §11.8 / §12.43(i) / D62 / ADR-0106 — `attempts.llm_backend`, `llm_failovers`,
`input_tokens`, `output_tokens`: the four D62 columns `llm_cache_hit` (ADR-0094) left open.

Sibling to `tests/test_llm_cache_hit_attribution.py`, same shape and same reason for existing:
before this task `AttemptRow` had none of these four fields, `record_attempt`'s `INSERT` could
not name any of them, and grepping the suite for `llm_backend`/`llm_failovers` (declaration sites
aside) returned nothing — this half of D62 had zero fixture coverage anywhere.

`llm_backend` is the one field here that needed a RULING, not just wiring (ADR-0106):
`workers/base.py::accumulate` drops `backend` by explicit, reasoned design (a ladder rung can
answer from more than one tier), so "stop dropping it" has to say which value wins when an
attempt's accumulated usage spans more than one backend. ADR-0106 rules LAST-NON-EMPTY-WINS,
order-preserving over the fold's call order. `llm_failovers` needed a new counter field on
`TokenUsage` (no semantics ambiguity — see `tasks.py`'s docstring) stamped by
`LadderModelClient.complete()`'s target loop. `input_tokens`/`output_tokens` are pure wiring
(`TokenUsage` already had both, `accumulate` already summed both) — covered here too so the four
columns this task closes are proven together, in the shape §6 of the round Y task 4 research
report lists.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
import pytest

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
from fleet.models.enums import Phase
from fleet.models.tasks import TokenUsage
from fleet.state import db as dbmod
from fleet.state.db import StateWriter, connect_ro, initialize_database
from fleet.state.repository import SqliteStateRepository
from fleet.workers.base import WorkerResult, accumulate
from fleet.workers.buildverify import StepRecord

RUN = "run-w-roundy-task4-backend-failover"
REPO = "acme-commons"
NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)


def usage(
    *,
    backend: str = "",
    input_tokens: int = 0,
    output_tokens: int = 0,
    llm_failovers: int = 0,
) -> TokenUsage:
    return TokenUsage(
        backend=backend,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        llm_failovers=llm_failovers,
    )


# ---------------------------------------------------------------------------------------------
# the derivation -- accumulate() (ADR-0106)
# ---------------------------------------------------------------------------------------------


def test_accumulate_takes_the_last_non_empty_backend_in_call_order() -> None:
    """ADR-0106: two usages with different non-empty `backend` values fold to the LAST one, not
    the first — `_stamp`'s own per-call semantics extended forward, and the reading that survives
    a mid-rung schema-repair failover: the LAST target is the one whose output the attempt
    actually shipped, not the one it discarded.
    """
    attempt = accumulate(usage(backend="anthropic"), usage(backend="openai_compatible"))
    assert attempt.backend == "openai_compatible"


def test_accumulate_keeps_the_last_non_empty_backend_even_when_the_final_usage_is_a_bare_seed() -> (
    None
):
    """Discriminates LAST-NON-EMPTY from plain LAST: if `accumulate` took the final argument's
    `backend` unconditionally, folding a trailing zero-seed `TokenUsage()` (`backend == ""`) after
    a real answer would erase it. ADR-0106 rules "last NON-EMPTY", not "last" — this is the case
    that tells the two apart.
    """
    attempt = accumulate(
        usage(backend="anthropic"), usage(backend="openai_compatible"), TokenUsage()
    )
    assert attempt.backend == "openai_compatible"


def test_accumulate_sums_llm_failovers_across_the_fold() -> None:
    """ADR-0106, §12.43(i): total hops across the whole attempt, summed exactly like the
    `llm_cache_lookups`/`llm_cache_hits` counters `accumulate` already sums — no identity element
    to poison, matching `schema.sql`'s `llm_failovers` comment ("backend hops spent inside THIS
    attempt") directly.
    """
    attempt = accumulate(TokenUsage(), usage(llm_failovers=2), usage(llm_failovers=1))
    assert attempt.llm_failovers == 3


def test_a_zero_seed_folded_first_contributes_no_backend_and_no_failovers() -> None:
    """Mirrors `test_two_replayed_calls_accumulate_to_one_all_hit_attempt`
    (`tests/test_llm_cache_hit_attribution.py`): the zero `TokenUsage()` seed every real caller
    folds through (`base.py::execute`, `buildgen.py::run`/`::_module_bazel`,
    `prwriter.py::_compose`, `rewrite.py::run`) must not itself count as an answering backend or
    a spent hop.
    """
    seed = TokenUsage()
    attempt = accumulate(seed, usage(backend="fake", llm_failovers=1))
    assert attempt.backend == "fake"
    assert attempt.llm_failovers == 1


def test_accumulate_sums_input_and_output_tokens() -> None:
    """§6a — pure wiring, no ruling needed, kept beside the counters above rather than asserted
    bare so a reader sees it is part of the same fold."""
    attempt = accumulate(
        usage(input_tokens=100, output_tokens=20), usage(input_tokens=50, output_tokens=5)
    )
    assert attempt.input_tokens == 150
    assert attempt.output_tokens == 25


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
    async with StateWriter(db_path, owner="w-roundy-task4-writer") as writer:
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


async def test_the_transform_sink_round_trips_all_four_new_fields(wired: _Wired) -> None:
    """Mirrors `test_the_transform_sink_derives_the_flag_from_the_usage_it_bills`
    (`tests/test_llm_cache_hit_attribution.py`): a real `_TransformSink.__call__` with a
    `TokenUsage` carrying a non-trivial value on all four new fields, read back off disk via
    `iter_attempts` and checked field-for-field — proving the sink's real derivation
    (`llm_backend=result.usage.backend or None`, `llm_failovers=result.usage.llm_failovers`,
    `input_tokens=result.usage.input_tokens`, `output_tokens=result.usage.output_tokens`) rather
    than asserting on a hand-built `AttemptRow`.
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
    billed = TokenUsage(
        backend="anthropic", model_id="cheap-a", input_tokens=1000, output_tokens=200,
        llm_failovers=2,
    )

    await sink(
        repo_id=REPO,
        phase=Phase.TRANSFORM,
        fence=1,
        result=WorkerResult[TransformOutput](
            status="ok",
            output=TransformOutput(repo_id=REPO, attempt=1),
            usage=billed,
        ),
    )

    rows = [r async for r in repo.iter_attempts(RUN)]
    assert len(rows) == 1
    row = rows[0]
    assert row.llm_backend == "anthropic"
    assert row.llm_failovers == 2
    assert row.input_tokens == 1000
    assert row.output_tokens == 200


async def test_the_transform_sink_writes_null_backend_for_a_deterministic_rung(
    wired: _Wired,
) -> None:
    """A DETERMINISTIC rung makes no LLM call, so `usage.backend == ""` — the sink must write
    `NULL`, not the empty string, matching `schema.sql`'s own comment
    ("llm_backend ... NULL for DETERMINISTIC rows") rather than a value `_opt_str`'s NULL-vs-empty
    convention elsewhere in this module would read back as a name.
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

    await sink(
        repo_id=REPO,
        phase=Phase.TRANSFORM,
        fence=1,
        result=WorkerResult[TransformOutput](
            status="ok",
            output=TransformOutput(repo_id=REPO, attempt=1),
            usage=TokenUsage(),
        ),
    )

    rows = [r async for r in repo.iter_attempts(RUN)]
    assert len(rows) == 1
    assert rows[0].llm_backend is None
    assert rows[0].llm_failovers == 0


async def test_the_attempt_writer_puts_the_new_fields_on_the_row_that_carries_the_cost(
    wired: _Wired,
) -> None:
    """Mirrors `test_the_attempt_writer_puts_the_flag_on_the_row_that_carries_the_cost`
    (`tests/test_llm_cache_hit_attribution.py`): a rung is many step rows but one LLM spend, and
    `_AttemptWriter.record` already attributes `cost_usd`/`llm_cache_hit` to the FIRST row only.
    The four new fields must ride the same row — a later step row that independently repeated the
    same backend/token counts would read as if it made its own separate call.
    """
    writer, read_conn, repo = wired
    sink = _AttemptWriter(
        writer=writer, repository=repo, read_conn=read_conn, run_id=RUN, clock=lambda: NOW
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
        llm_cache_hit=False,
        llm_backend="openai_compatible",
        llm_failovers=1,
        input_tokens=500,
        output_tokens=80,
    )

    rows = {row.attempt_id: row for row in [r async for r in repo.iter_attempts(RUN)]}
    assert len(written) == 2
    first, second = rows[written[0]], rows[written[1]]
    assert first.llm_backend == "openai_compatible"
    assert first.llm_failovers == 1
    assert first.input_tokens == 500
    assert first.output_tokens == 80
    assert second.llm_backend is None, (
        "the second step row carries no cost, so it must carry no backend attribution either"
    )
    assert second.llm_failovers == 0
    assert second.input_tokens == 0
    assert second.output_tokens == 0


async def test_the_build_and_verify_sinks_forward_all_four_new_fields_they_were_billed_on(
    wired: _Wired,
) -> None:
    """Mirrors `test_the_build_and_verify_sinks_forward_the_flag_they_were_billed_on`
    (`tests/test_llm_cache_hit_attribution.py`): `_BuildSink`/`_VerifySink` reach
    `_AttemptWriter.record` through a real `WorkerResult` rather than a direct kwarg, which is the
    only way a mutation that swapped either sink's forwarded values for constants would be
    reddened — that file's own header explains why the attempt-writer case alone cannot stand in
    for this one.
    """
    writer, read_conn, repo = wired
    attempts = _AttemptWriter(
        writer=writer, repository=repo, read_conn=read_conn, run_id=RUN, clock=lambda: NOW
    )
    billed = TokenUsage(
        backend="anthropic", model_id="heavy-a", input_tokens=2000, output_tokens=400,
        llm_failovers=1,
    )
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
            usage=billed,
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
            usage=billed,
        ),
    )

    rows = {row.phase: row async for row in repo.iter_attempts(RUN)}
    for phase in (Phase.BUILD, Phase.VERIFY):
        row = rows[phase]
        assert row.llm_backend == "anthropic", (
            f"{phase} sink must derive llm_backend from the usage it billed, not a constant"
        )
        assert row.llm_failovers == 1
        assert row.input_tokens == 2000
        assert row.output_tokens == 400
