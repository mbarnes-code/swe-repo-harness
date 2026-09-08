"""D89 Phase 2 Task A (ADR-0102) — the TRANSFORM coarse `tasks` row's real claim lifecycle.

D89 Phase 1 (`tests/test_d89_phase1_task_lifecycle.py`, ADR-0101) minted a coarse `tasks` row per
`(run_id, repo_id, phase)` and stamped its id onto `attempts.task_id`, but deliberately never let
that row leave `status='PENDING'` — so D87's `_ARBITRATED_TASKS_SQL` (`status = 'RUNNING'`) never
saw it. Task A of Phase 2 (`.superpowers/sdd/round-U-criteria-closure/research-1-d89-phase2-plan.md`
§4) gives the row a real `PENDING -> RUNNING -> {DONE, PENDING}` lifecycle for TRANSFORM ONLY,
without touching `_reconcile_tasks_with_git`/`_persist_arbitration` (Task B, unbuilt).

This file proves, at the level BELOW `PhaseRunner` ordering (`tests/test_runner.py`'s new
`pre_dispatch` section covers that layer):

* `SqliteStateRepository.set_task_target_paths`/`claim_task_by_id` (`state/repository.py`) —
  the two new primitives.
* `_TransformClaimHook` (`cli.py`) — populates `target_paths` and claims the row RUNNING, reusing
  `_coarse_task_id` unchanged.
* `_TransformSink.__call__`'s new happy-path resolution — `status="ok"` -> DONE, anything else ->
  back to re-claimable PENDING with `fence_token` bumped.
* The load-bearing safety property: BUILD/VERIFY/SCAN's `PhaseRunner(...)` sites in `cli.py` pass
  no `pre_dispatch` and must never leave a coarse row's status where Phase 1 didn't.
"""

from __future__ import annotations

import ast
import subprocess
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
import pytest

from fleet.cli import (
    TransformInput,
    TransformOutput,
    _TransformClaimHook,
    _TransformEvidence,
    _TransformSink,
)
from fleet.models.enums import Phase
from fleet.state import db as dbmod
from fleet.state.db import StateWriter, connect_ro, initialize_database
from fleet.state.repository import SqliteStateRepository
from fleet.workers.base import WorkerError, WorkerResult

RUN = "run-d89-phase2-task-a"
REPO = "acme-commons"
NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)
OWNER = "test-host:test-cid:1:boot"

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
    async with StateWriter(db_path, owner="d89-phase2-task-a-test-writer") as writer:
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


async def _task_row(
    read_conn: aiosqlite.Connection,
) -> tuple[str, str, str | None, str | None, int, str]:
    """`(task_id, status, claimed_by, lease_expires_at, fence_token, target_paths)`."""
    async with read_conn.execute(
        "SELECT task_id, status, claimed_by, lease_expires_at, fence_token, target_paths "
        "  FROM tasks WHERE run_id = ? AND repo_id = ? AND phase = ?",
        (RUN, REPO, int(Phase.TRANSFORM)),
    ) as cursor:
        rows = await cursor.fetchall()
    assert len(rows) == 1, "exactly one coarse tasks row for this (run, repo, phase)"
    row = rows[0]
    return str(row[0]), str(row[1]), row[2], row[3], int(row[4]), str(row[5])


def _init_transform_worktree(work_dir: Path, repo_id: str = REPO) -> Path:
    """D91 fix: `_TransformClaimHook` now reads a REAL git anchor (`record_task_anchor`) at claim
    time, so every test that constructs the hook needs a real `migrate/<repo_id>` checkout at
    `work_dir/repo_id` — mirroring `TransformPipelineWorker`'s own `Git(ctx.workdir)` shape
    (`ctx.workdir` IS `work_dir / repo_id`). Returns `work_dir` for convenience at call sites.
    """
    repo_path = work_dir / repo_id
    repo_path.mkdir(parents=True, exist_ok=True)

    def run(*args: str) -> None:
        subprocess.run(  # noqa: S603
            ["git", "-C", str(repo_path), *args],  # noqa: S607 - `git` from PATH, as every suite does
            check=True,
            capture_output=True,
        )

    run("init", "--initial-branch=main")
    run("config", "user.email", "fleet@example.invalid")
    run("config", "user.name", "Fleet Test")
    (repo_path / "README").write_text("x\n")
    run("add", "--all")
    run("commit", "-m", "initial")
    run("checkout", "-b", f"migrate/{repo_id}")
    return work_dir


def _branch_tip(work_dir: Path, repo_id: str = REPO) -> str:
    """The real `migrate/<repo_id>` tip `record_task_anchor` should read at claim time."""
    result = subprocess.run(  # noqa: S603
        ["git", "-C", str(work_dir / repo_id), "rev-parse", f"migrate/{repo_id}"],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


async def _task_pre_commit_sha(read_conn: aiosqlite.Connection) -> str | None:
    async with read_conn.execute(
        "SELECT pre_commit_sha FROM tasks WHERE run_id = ? AND repo_id = ? AND phase = ?",
        (RUN, REPO, int(Phase.TRANSFORM)),
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None, "the coarse tasks row must exist for this test's own seeding to hold"
    return None if row[0] is None else str(row[0])


def _payload() -> TransformInput:
    return TransformInput(
        repo_id=REPO,
        branch=f"migrate/{REPO}",
        phase_pre_commit_sha="a" * 40,
        dest_path="libs/com/acme/commons",
        sources=("java/com/acme/A.java", "java/com/acme/B.java"),
        targets=("libs/com/acme/commons/A.java", "libs/com/acme/commons/B.java"),
    )


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


async def _dispatch(
    sink: _TransformSink,
    *,
    status: str = "ok",
    attempt: int = 1,
    fence: int = 1,
    commits: list[str] | None = None,
) -> None:
    error = (
        None
        if status == "ok"
        else WorkerError(failure_class="TRANSIENT_INFRA", retryable=True, stderr_tail="boom")
    )
    await sink(
        repo_id=REPO,
        phase=Phase.TRANSFORM,
        fence=fence,
        result=WorkerResult[TransformOutput](
            status=status,  # type: ignore[arg-type]
            output=TransformOutput(
                repo_id=REPO, attempt=attempt, commits=commits if commits is not None else []
            ),
            error=error,
        ),
    )


# ======================================================================================
# 1. state/repository.py's two new primitives
# ======================================================================================


async def test_set_task_target_paths_writes_the_json_column(wired: _Wired) -> None:
    _writer, read_conn, repo = wired
    task_id = await repo.upsert_task(
        "t-1",
        run_id=RUN,
        repo_id=REPO,
        phase=Phase.TRANSFORM,
        kind=__import__("fleet.models.enums", fromlist=["TaskKind"]).TaskKind.REWRITE,
        dest_path="libs/com/acme/commons",
        created_at=NOW,
    )
    await repo.set_task_target_paths(task_id, ["a/x.py", "a/y.py"])

    async with read_conn.execute(
        "SELECT target_paths FROM tasks WHERE task_id = ?", (task_id,)
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None
    assert row[0] == '["a/x.py", "a/y.py"]'


async def test_claim_task_by_id_moves_pending_to_running_and_bumps_fence(wired: _Wired) -> None:
    from fleet.models.enums import TaskKind

    _writer, read_conn, repo = wired
    task_id = await repo.upsert_task(
        "t-1",
        run_id=RUN,
        repo_id=REPO,
        phase=Phase.TRANSFORM,
        kind=TaskKind.REWRITE,
        dest_path="libs/com/acme/commons",
        created_at=NOW,
    )

    won = await repo.claim_task_by_id(task_id, worker=OWNER, now=NOW, lease_ttl_s=600)

    assert won is True
    async with read_conn.execute(
        "SELECT status, claimed_by, fence_token FROM tasks WHERE task_id = ?", (task_id,)
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None
    assert (str(row[0]), str(row[1]), int(row[2])) == ("RUNNING", OWNER, 1), (
        "claim_task_by_id must move PENDING -> RUNNING (not claim_next_task's 'CLAIMED') "
        "so _ARBITRATED_TASKS_SQL's status='RUNNING' scan can see it, and must bump the fence"
    )


async def test_claim_task_by_id_is_a_cas_a_second_claim_on_a_non_pending_row_loses(
    wired: _Wired,
) -> None:
    from fleet.models.enums import TaskKind

    _writer, read_conn, repo = wired
    task_id = await repo.upsert_task(
        "t-1",
        run_id=RUN,
        repo_id=REPO,
        phase=Phase.TRANSFORM,
        kind=TaskKind.REWRITE,
        dest_path="libs/com/acme/commons",
        created_at=NOW,
    )
    first = await repo.claim_task_by_id(task_id, worker=OWNER, now=NOW, lease_ttl_s=600)
    second = await repo.claim_task_by_id(
        task_id, worker="a-different-owner", now=NOW, lease_ttl_s=600
    )

    assert (first, second) == (True, False), (
        "the row is RUNNING after the first claim; a second CAS against it must lose, not "
        "silently re-claim under a different owner"
    )
    async with read_conn.execute(
        "SELECT claimed_by FROM tasks WHERE task_id = ?", (task_id,)
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None and row[0] == OWNER, "the losing CAS must not overwrite the owner"


# ======================================================================================
# 2. _TransformClaimHook — the PreDispatchHook wired only into TRANSFORM
# ======================================================================================


async def test_claim_hook_populates_target_paths_and_claims_running_before_dispatch(
    wired: _Wired, tmp_path: Path
) -> None:
    """A real hook call, exactly as `_run_transform_wave` wires it, against a fresh repo with NO
    prior coarse row: it must mint one (via `_coarse_task_id`, reused unchanged), populate
    `target_paths` with the payload's raw sources+targets, and claim it RUNNING — all before any
    `attempts` row exists.

    D91 (`docs/INTEGRATION_HONESTY.md`): also the primary proof that `tasks.pre_commit_sha` gets a
    real production writer. Before this fix `_TransformClaimHook.__call__` never touched the
    column at all, so this same claim always left it NULL; the assertion below reads it back
    against the REAL `migrate/<repo_id>` tip a fresh `git rev-parse` reports, not a mocked value —
    a real per-unit anchor, matching what `record_task_anchor`'s own docstring promises.
    """
    _writer, read_conn, repo = wired
    work_dir = _init_transform_worktree(tmp_path / "work")
    hook = _TransformClaimHook(
        repository=repo,
        read_conn=read_conn,
        run_id=RUN,
        owner=OWNER,
        clock=lambda: NOW,
        lease_ttl_s=600,
        work_dir=work_dir,
    )
    payload = _payload()

    await hook(repo_id=REPO, phase=Phase.TRANSFORM, payload=payload)

    _task_id, status, claimed_by, lease_expires_at, fence_token, target_paths = await _task_row(
        read_conn
    )
    assert status == "RUNNING"
    assert claimed_by == OWNER
    assert lease_expires_at is not None
    assert fence_token == 1
    assert target_paths == (
        '["java/com/acme/A.java", "java/com/acme/B.java", '
        '"libs/com/acme/commons/A.java", "libs/com/acme/commons/B.java"]'
    )
    async with read_conn.execute("SELECT COUNT(*) FROM attempts") as cursor:
        row = await cursor.fetchone()
    assert row is not None and row[0] == 0, (
        "the hook runs BEFORE the worker executes; no attempts row can exist yet"
    )
    pre_commit_sha = await _task_pre_commit_sha(read_conn)
    assert pre_commit_sha == _branch_tip(work_dir), (
        "D91: the claim CAS must write tasks.pre_commit_sha to the REAL migrate/<repo_id> tip "
        "read at claim time, not leave it NULL"
    )


async def test_claim_hook_reuses_the_same_row_the_sink_later_writes_attempts_against(
    wired: _Wired, tmp_path: Path
) -> None:
    """`_coarse_task_id`'s idempotent UPSERT is what makes calling it twice per dispatch (once
    from the hook, once from the sink) safe: both calls must resolve to the SAME task_id."""
    writer, read_conn, repo = wired
    work_dir = _init_transform_worktree(tmp_path / "work")
    hook = _TransformClaimHook(
        repository=repo,
        read_conn=read_conn,
        run_id=RUN,
        owner=OWNER,
        clock=lambda: NOW,
        lease_ttl_s=600,
        work_dir=work_dir,
    )
    await hook(repo_id=REPO, phase=Phase.TRANSFORM, payload=_payload())
    task_id_from_hook, *_rest = await _task_row(read_conn)

    sink = _sink(writer, repo, read_conn)
    await _dispatch(sink, status="ok")

    async with read_conn.execute(
        "SELECT task_id FROM attempts WHERE run_id = ? AND repo_id = ?", (RUN, REPO)
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None and row[0] == task_id_from_hook, (
        "the sink's attempts.task_id must point at the SAME row the hook claimed, not a "
        "second row minted independently"
    )


async def test_claim_hook_ignores_non_transform_phases(wired: _Wired, tmp_path: Path) -> None:
    """Defensive: the hook is wired only into the TRANSFORM `PhaseRunner`, but its own body also
    refuses a non-TRANSFORM phase rather than trusting the call site alone (Rule 11)."""
    _writer, read_conn, repo = wired
    work_dir = _init_transform_worktree(tmp_path / "work")
    hook = _TransformClaimHook(
        repository=repo,
        read_conn=read_conn,
        run_id=RUN,
        owner=OWNER,
        clock=lambda: NOW,
        lease_ttl_s=600,
        work_dir=work_dir,
    )

    await hook(repo_id=REPO, phase=Phase.BUILD, payload=_payload())

    async with read_conn.execute("SELECT COUNT(*) FROM tasks") as cursor:
        row = await cursor.fetchone()
    assert row is not None and row[0] == 0, "a non-TRANSFORM phase must mint/claim nothing"


# ======================================================================================
# 3. _TransformSink.__call__'s new happy-path resolution
# ======================================================================================


async def test_sink_resolves_a_running_row_to_done_on_ok_status(
    wired: _Wired, tmp_path: Path
) -> None:
    writer, read_conn, repo = wired
    work_dir = _init_transform_worktree(tmp_path / "work")
    hook = _TransformClaimHook(
        repository=repo,
        read_conn=read_conn,
        run_id=RUN,
        owner=OWNER,
        clock=lambda: NOW,
        lease_ttl_s=600,
        work_dir=work_dir,
    )
    await hook(repo_id=REPO, phase=Phase.TRANSFORM, payload=_payload())
    _, status_before, _, _, fence_before, _ = await _task_row(read_conn)
    assert (status_before, fence_before) == ("RUNNING", 1)

    sink = _sink(writer, repo, read_conn)
    await _dispatch(sink, status="ok")

    _, status_after, claimed_by_after, lease_after, fence_after, _ = await _task_row(read_conn)
    assert status_after == "DONE"
    assert claimed_by_after is None
    assert lease_after is None
    assert fence_after == 1, "DONE is terminal and does not need a fresh fence"


async def test_sink_resolves_a_running_row_back_to_pending_with_fence_bump_on_non_ok(
    wired: _Wired, tmp_path: Path
) -> None:
    writer, read_conn, repo = wired
    work_dir = _init_transform_worktree(tmp_path / "work")
    hook = _TransformClaimHook(
        repository=repo,
        read_conn=read_conn,
        run_id=RUN,
        owner=OWNER,
        clock=lambda: NOW,
        lease_ttl_s=600,
        work_dir=work_dir,
    )
    await hook(repo_id=REPO, phase=Phase.TRANSFORM, payload=_payload())

    sink = _sink(writer, repo, read_conn)
    await _dispatch(sink, status="failed")

    task_id, status_after, claimed_by_after, lease_after, fence_after, _ = await _task_row(
        read_conn
    )
    assert status_after == "PENDING"
    assert claimed_by_after is None
    assert lease_after is None
    assert fence_after == 2, (
        "PENDING must be RE-CLAIMABLE: the fence bumps so a stale claimant's writes are fenced "
        "out, mirroring claim_task_by_id's own CAS in reverse"
    )
    # And it is genuinely re-claimable:
    won_again = await repo.claim_task_by_id(task_id, worker=OWNER, now=NOW, lease_ttl_s=600)
    assert won_again is True


async def test_sink_without_a_prior_claim_still_resolves_safely(wired: _Wired) -> None:
    """A dispatch whose `pre_dispatch` hook never ran (e.g. a future non-TRANSFORM caller of
    `_TransformSink`, hypothetically) must not raise: the resolution UPDATEs are gated on
    `status = 'RUNNING'`, so a row that is still `PENDING` (Phase 1's own shape) is left alone —
    this is what keeps the sink safe to call from a hookless path without a second gate.
    """
    writer, read_conn, repo = wired
    sink = _sink(writer, repo, read_conn)

    await _dispatch(sink, status="ok")

    _, status_after, _, _, fence_after, _ = await _task_row(read_conn)
    assert (status_after, fence_after) == ("PENDING", 0), (
        "with no prior RUNNING claim, the resolve UPDATE must match zero rows and the coarse "
        "row must be left exactly as Phase 1 (ADR-0101) always left it"
    )


async def test_the_done_assertion_is_not_vacuous_a_row_never_resolved_is_not_done(
    wired: _Wired, tmp_path: Path
) -> None:
    """Rule 12 discriminator for the two happy-path tests above: a row the hook claimed but that
    the sink's resolution step never touched (what a mutation deleting that step would produce)
    must NOT read `DONE`. This is what proves `test_sink_resolves_a_running_row_to_done_on_ok_
    status`'s `status_after == "DONE"` is a real assertion and not one a `RUNNING` row would
    already satisfy by coincidence.

    A source-level mutation of `_TransformSink.__call__` (deleting the `if task_id is not None:`
    resolution block added by this change) was additionally run by hand against this exact test
    file: `test_sink_resolves_a_running_row_to_done_on_ok_status` and `test_sink_resolves_a_
    running_row_back_to_pending_with_fence_bump_on_non_ok` both turned RED (status stayed
    `RUNNING` in both), and no other test in this module or in `tests/test_d89_phase1_task_
    lifecycle.py` changed verdict — see the implementation report for the exact before/after.
    """
    _writer, read_conn, repo = wired
    work_dir = _init_transform_worktree(tmp_path / "work")
    hook = _TransformClaimHook(
        repository=repo,
        read_conn=read_conn,
        run_id=RUN,
        owner=OWNER,
        clock=lambda: NOW,
        lease_ttl_s=600,
        work_dir=work_dir,
    )
    await hook(repo_id=REPO, phase=Phase.TRANSFORM, payload=_payload())

    _, status, _, _, _, _ = await _task_row(read_conn)
    assert status == "RUNNING" and status != "DONE", (
        "a row whose resolution step never ran must read RUNNING, not DONE — if it already "
        "read DONE here, the happy-path test's DONE assertion would prove nothing"
    )


async def _phase_post_commit_sha(read_conn: aiosqlite.Connection) -> str | None:
    async with read_conn.execute(
        "SELECT post_commit_sha FROM phases WHERE run_id = ? AND repo_id = ? AND phase = ?",
        (RUN, REPO, int(Phase.TRANSFORM)),
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None, "the phases row must exist for this test's own seeding to be valid"
    return None if row[0] is None else str(row[0])


class _CrashAfterFirstSubmit:
    """Wraps a real `StateWriter` and raises before its SECOND `submit()` call, simulating a
    process crash landing between the two writer units `_TransformSink.__call__` now submits.
    Proves the corrected order (`phases.post_commit_sha` write first, coarse `tasks` resolution
    second) leaves the row RUNNING with the commit pointer already written, rather than the
    pre-fix order's falsely-DONE-with-a-NULL-pointer hazard this task corrects.
    """

    def __init__(self, real: StateWriter) -> None:
        self._real = real
        self.calls = 0

    async def submit(self, unit: object) -> object:
        self.calls += 1
        if self.calls == 2:
            raise RuntimeError("simulated crash between the two writer units")
        return await self._real.submit(unit)  # type: ignore[arg-type]


async def test_a_crash_between_the_two_writer_units_leaves_post_commit_sha_written_and_task_running(
    wired: _Wired, tmp_path: Path
) -> None:
    """The ordering fix itself (found reviewing D89 Phase 2 Task B, corrected 2026-09-01):
    `_TransformSink.__call__` used to resolve the coarse row to DONE in a separate writer unit
    BEFORE writing `phases.post_commit_sha`, so a crash in that window left `tasks.status='DONE'`
    (invisible to `_ARBITRATED_TASKS_SQL`, which only asks about RUNNING rows) with the commit
    pointer still NULL — a landed commit whose pointer was lost and whose row would never be
    re-examined. This proves the fix: with a real commit and a writer that fails on its second
    `submit()` (simulating the crash), the FIRST unit to run must be the `phases.post_commit_sha`
    write, so it lands before the crash, and `tasks.status` must still read RUNNING — correctly
    re-arbitrated by D89 Phase 2 Task B's own sweep — not falsely DONE.
    """
    writer, read_conn, repo = wired
    await repo.upsert_phase(RUN, REPO, Phase.TRANSFORM, now=NOW)
    work_dir = _init_transform_worktree(tmp_path / "work")
    hook = _TransformClaimHook(
        repository=repo,
        read_conn=read_conn,
        run_id=RUN,
        owner=OWNER,
        clock=lambda: NOW,
        lease_ttl_s=600,
        work_dir=work_dir,
    )
    await hook(repo_id=REPO, phase=Phase.TRANSFORM, payload=_payload())
    _, status_before, _, _, _, _ = await _task_row(read_conn)
    assert status_before == "RUNNING", "the claim hook must have run before the crash scenario"
    assert await _phase_post_commit_sha(read_conn) is None, "nothing has landed yet"

    crashy = _CrashAfterFirstSubmit(writer)
    sink = _TransformSink(
        writer=crashy,  # type: ignore[arg-type]
        repository=repo,
        read_conn=read_conn,
        run_id=RUN,
        evidence=_TransformEvidence(),
        clock=lambda: NOW,
    )
    sha = "c" * 40

    with pytest.raises(RuntimeError, match="simulated crash"):
        await _dispatch(sink, status="ok", fence=0, commits=[sha])

    assert crashy.calls == 2, (
        "the crash must land between the two submits, not before the first ran at all — "
        "otherwise this proves nothing about ORDER"
    )
    assert await _phase_post_commit_sha(read_conn) == sha, (
        "the commit pointer write must be the FIRST unit submitted, so it survives the crash"
    )
    _, status_after, _, _, fence_after, _ = await _task_row(read_conn)
    assert status_after == "RUNNING", (
        "the coarse row must NOT have been resolved DONE — the crash landed before that write, "
        "and a falsely-DONE row here is exactly the hazard this ordering fix closes"
    )
    assert fence_after == 1, "the claim's own fence is untouched by the aborted resolution"


# ======================================================================================
# 4. BUILD/VERIFY/SCAN regression — the load-bearing safety property
# ======================================================================================


def _phase_runner_call_sites() -> list[ast.Call]:
    """Every `PhaseRunner(...)` call in `cli.py`, found structurally (AST), not by grepping text
    that could rot — CLAUDE.md's "bind prose to code" guardrail applied to a call-site census."""
    src_path = Path(__file__).resolve().parents[1] / "src" / "fleet" / "cli.py"
    tree = ast.parse(src_path.read_text(encoding="utf-8"), filename=str(src_path))
    calls: list[ast.Call] = []

    class Visitor(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call) -> None:
            if isinstance(node.func, ast.Name) and node.func.id == "PhaseRunner":
                calls.append(node)
            self.generic_visit(node)

    Visitor().visit(tree)
    return calls


def test_exactly_four_phaserunner_call_sites_and_only_one_passes_pre_dispatch() -> None:
    """The load-bearing safety property, checked structurally: BUILD/VERIFY/SCAN's
    `PhaseRunner(...)` sites (three of the four) must pass NO `pre_dispatch` keyword — their
    coarse rows, if any exist, must never leave `PENDING`, exactly as D89 Phase 1 (ADR-0101)
    guaranteed. Only the TRANSFORM site may pass one.

    A future accidental `pre_dispatch=` addition to a BUILD/VERIFY/SCAN site — or the accidental
    REMOVAL of the one on TRANSFORM — turns this test red, without relying on any wording in a
    comment staying in sync with the code (the wording rots; the AST does not).
    """
    calls = _phase_runner_call_sites()
    assert len(calls) == 4, (
        f"expected exactly 4 PhaseRunner(...) call sites in cli.py, found {len(calls)} — "
        "this test's own count needs updating if a phase was legitimately added or removed"
    )
    with_pre_dispatch = [
        call for call in calls if any(kw.arg == "pre_dispatch" for kw in call.keywords)
    ]
    assert len(with_pre_dispatch) == 1, (
        f"expected exactly ONE PhaseRunner(...) site to pass pre_dispatch (TRANSFORM), found "
        f"{len(with_pre_dispatch)} at lines {[c.lineno for c in with_pre_dispatch]}"
    )


async def test_a_coarse_row_minted_before_a_hookless_dispatch_never_leaves_pending(
    wired: _Wired,
) -> None:
    """The functional twin of the AST check above, at the row level: mirrors
    `test_d89_phase1_task_lifecycle.py`'s scenario E, generalized to ANY coarse row minted the
    way Phase 1 always mints one (`upsert_task` alone, no claim) and never touched by a
    `pre_dispatch` hook — exactly BUILD/VERIFY/SCAN's shape today. `_ARBITRATED_TASKS_SQL` must
    still see zero candidates.
    """
    from fleet.cli import _ARBITRATED_TASKS_SQL
    from fleet.models.enums import TaskKind

    _writer, read_conn, repo = wired
    await repo.upsert_task(
        "t-1",
        run_id=RUN,
        repo_id=REPO,
        phase=Phase.BUILD,
        kind=TaskKind.BUILDGEN,
        dest_path="libs/com/acme/commons",
        created_at=NOW,
    )

    horizon = NOW.astimezone(UTC).isoformat(timespec="microseconds")
    async with read_conn.execute(_ARBITRATED_TASKS_SQL, (RUN, horizon, horizon, RUN)) as cursor:
        candidates = await cursor.fetchall()
    assert candidates == [], (
        "a coarse row minted with no pre_dispatch claim (BUILD/VERIFY/SCAN's exact shape) must "
        "stay invisible to the RUNNING-status arbitration sweep"
    )
