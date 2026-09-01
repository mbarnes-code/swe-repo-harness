"""D89 Phase 1 (ADR-0101) — `attempts.task_id` populated at a coarse per-dispatch grain.

D89 (`docs/INTEGRATION_HONESTY.md`) is OPEN: `attempts.task_id` was never populated by any
production write site, which made D87's git-arbitration mechanism (`_reconcile_tasks_with_git`)
permanently inert — it can only ever look at a `tasks` row through an `attempts.task_id` pointer
(or the row's own git trailer identity), and nothing wrote one. This file is the sink-level
integration proof for Phase 1's fix: `_TransformSink.__call__` and `_AttemptWriter.record` (shared
by `_BuildSink`/`_VerifySink`) now mint/reuse one coarse `tasks` row per `(run_id, repo_id, phase)`
via `_coarse_task_id` (`src/fleet/cli.py`) and stamp its id onto every `attempts` row the dispatch
writes.

Phase 1 is deliberately narrow (`.superpowers/sdd/round-T-criteria-closure/
research-1-d89-phase1-plan.md`): it mints the `tasks` row through `upsert_task` ONLY, which never
writes anything but the schema default `status = 'PENDING'`. It never calls `claim_next_task` or
any other status-mutating path. Per-unit REWRITE/RELOCATE reconciliation (Phase 2) is unbuilt —
this file proves Phase 1's narrower claim, not D89's full closure.

Scenario E is the most important test here: it is not an argument that a Phase-1 row is invisible
to `_ARBITRATED_TASKS_SQL`, it is a direct execution of that production query object against a
row Phase 1 actually wrote, asserting zero candidates. A future regression that made
`_coarse_task_id` (or anything it calls) write `status = 'RUNNING'` would turn this test red.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
import pytest

from fleet.cli import _ARBITRATED_TASKS_SQL, TransformOutput, _TransformEvidence, _TransformSink
from fleet.models.enums import Phase
from fleet.state import db as dbmod
from fleet.state.db import StateWriter, connect_ro, initialize_database
from fleet.state.repository import SqliteStateRepository
from fleet.workers.base import WorkerResult

RUN = "run-d89-phase1"
REPO = "acme-commons"
NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)

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
    async with StateWriter(db_path, owner="d89-phase1-test-writer") as writer:
        read_conn = await connect_ro(db_path)
        try:
            repo = SqliteStateRepository(writer=writer, read_conn=read_conn)
            await repo.upsert_run(
                RUN, started_at=NOW, config_sha256="a" * 64, harness_version="0.1.0"
            )
            await repo.upsert_repo(
                REPO,
                name=REPO,
                url=f"https://example.invalid/{REPO}.git",
                now=NOW,
                dest_path="libs/com/acme/commons",
            )
            yield writer, read_conn, repo
        finally:
            await read_conn.close()


def _sink(
    writer: StateWriter, repo: SqliteStateRepository, read_conn: aiosqlite.Connection
) -> _TransformSink:
    return _TransformSink(
        writer=writer,
        repository=repo,
        read_conn=read_conn,
        run_id=RUN,
        evidence=_TransformEvidence(),
        clock=lambda: NOW,
    )


async def _dispatch(sink: _TransformSink, attempt: int) -> None:
    await sink(
        repo_id=REPO,
        phase=Phase.TRANSFORM,
        fence=1,
        result=WorkerResult[TransformOutput](
            status="ok",
            output=TransformOutput(repo_id=REPO, attempt=attempt),
        ),
    )


async def test_a_real_dispatch_populates_a_tasks_row_and_stamps_its_id_onto_the_attempt(
    wired: _Wired,
) -> None:
    """Scenario C: one real `_TransformSink` dispatch mints a `tasks` row and the `attempts` row
    it writes in the same call carries that row's `task_id` — not `NULL`."""
    writer, read_conn, repo = wired
    sink = _sink(writer, repo, read_conn)

    await _dispatch(sink, attempt=1)

    async with read_conn.execute(
        "SELECT task_id, kind, status FROM tasks WHERE run_id = ? AND repo_id = ? AND phase = ?",
        (RUN, REPO, int(Phase.TRANSFORM)),
    ) as cursor:
        task_rows = await cursor.fetchall()
    assert len(task_rows) == 1, "exactly one coarse tasks row for this (run, repo, phase)"
    task_id, kind, status = task_rows[0]
    assert kind == "REWRITE"
    assert status == "PENDING"

    attempts = [row async for row in repo.iter_attempts(RUN)]
    assert len(attempts) == 1
    assert attempts[0].task_id == task_id, "attempts.task_id must point at the minted tasks row"


async def test_two_dispatches_of_the_same_repo_phase_share_one_task_id(wired: _Wired) -> None:
    """Scenario D (retry-stability): a second dispatch of the same repo+phase (a second ladder
    rung) reuses the SAME `task_id` and does not mint a second `tasks` row — the grain is
    per-phase-dispatch-cycle, not per-rung."""
    writer, read_conn, repo = wired
    sink = _sink(writer, repo, read_conn)

    await _dispatch(sink, attempt=1)
    await _dispatch(sink, attempt=2)

    async with read_conn.execute(
        "SELECT task_id FROM tasks WHERE run_id = ? AND repo_id = ? AND phase = ?",
        (RUN, REPO, int(Phase.TRANSFORM)),
    ) as cursor:
        task_rows = await cursor.fetchall()
    assert len(task_rows) == 1, "a retry rung must not mint a second tasks row"

    attempts = {row.attempt: row for row in [r async for r in repo.iter_attempts(RUN)]}
    assert len(attempts) == 2
    assert attempts[1].task_id == attempts[2].task_id == task_rows[0][0]


async def test_a_phase1_tasks_row_is_invisible_to_the_arbitration_sweep(wired: _Wired) -> None:
    """Scenario E — the D87-safety proof, the most important test in this file.

    Directly executes the PRODUCTION `_ARBITRATED_TASKS_SQL` query (the same object
    `_reconcile_tasks_with_git` runs) against a real Phase-1 dispatch's row and asserts it returns
    ZERO candidates. This is not an argument that Phase 1 cannot regress D87 — it is that argument
    made falsifiable: a future change that makes `_coarse_task_id`/`upsert_task` ever write
    `status = 'RUNNING'` turns this test red (see the mutation proof below in this module's
    sibling assertion, `test_a_row_forced_to_running_status_IS_visible_to_the_same_query`, which
    demonstrates the query is not vacuously always-empty).
    """
    writer, read_conn, repo = wired
    sink = _sink(writer, repo, read_conn)
    await _dispatch(sink, attempt=1)

    horizon = NOW.astimezone(UTC).isoformat(timespec="microseconds")
    async with read_conn.execute(_ARBITRATED_TASKS_SQL, (RUN, horizon, horizon, RUN)) as cursor:
        candidates = await cursor.fetchall()
    assert candidates == [], (
        "a Phase-1-written tasks row must never be selected by the RUNNING-status arbitration "
        "sweep — if this fires, _coarse_task_id (or upsert_task) regressed to writing a "
        "status other than PENDING, which is the exact D87 misfire ADR-0101 exists to prevent"
    )


async def test_a_row_forced_to_running_status_IS_visible_to_the_same_query(wired: _Wired) -> None:
    """Mutation-proof (Rule 12) for scenario E: `_ARBITRATED_TASKS_SQL` is not vacuously empty.

    This directly emulates the regression scenario E exists to catch — Phase 1 (or a future
    refactor) writing `status = 'RUNNING'` onto a coarse tasks row via a path OTHER than
    `upsert_task` (which structurally cannot do it) — via a raw UPDATE, then re-runs the exact
    same production query and asserts the row now IS a candidate. Together with scenario E this
    proves the query genuinely discriminates PENDING (Phase 1's real state) from RUNNING (the
    state that would expose a row to `discard_task`), rather than scenario E passing because the
    query can never return anything.
    """
    writer, read_conn, repo = wired
    sink = _sink(writer, repo, read_conn)
    await _dispatch(sink, attempt=1)

    async def force_running(conn: aiosqlite.Connection) -> None:
        await conn.execute(
            "UPDATE tasks SET status = 'RUNNING' WHERE run_id = ? AND repo_id = ? AND phase = ?",
            (RUN, REPO, int(Phase.TRANSFORM)),
        )

    await writer.submit(force_running)

    horizon = NOW.astimezone(UTC).isoformat(timespec="microseconds")
    async with read_conn.execute(_ARBITRATED_TASKS_SQL, (RUN, horizon, horizon, RUN)) as cursor:
        candidates = await cursor.fetchall()
    assert len(candidates) == 1, (
        "the same query must select a RUNNING row with a dead lease — proving scenario E's "
        "zero-candidates result discriminates PENDING from RUNNING rather than being vacuous"
    )
