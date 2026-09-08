"""D89 Phase 2 Task B (this task) — the REWRITE per-unit reconciliation rebuild.

D89 Phase 2 Task A (`tests/test_d89_phase2_claim_lifecycle.py`, ADR-0102) gave the coarse
TRANSFORM `tasks` row a real `PENDING -> RUNNING -> {DONE, PENDING}` lifecycle with `target_paths`
populated, but explicitly left `_reconcile_tasks_with_git`/`_persist_arbitration` untouched.
Before this task, a candidate REWRITE row reaching `_ARBITRATED_TASKS_SQL` (a genuine possibility
only since Task A) would be asked about its OWN `task_id` — a trailer nothing ever writes for this
kind, only the per-unit synthetic ids `task_id_for`/`task_id_for_ids` derive are — so `git` would
always answer "no", and `discard_task`'s `reset --hard` + `clean -fdx` would run and delete any
per-unit commits that DID land during the dispatch. That is the hazard Task A's own commit
deliberately left open and this task closes.

This file proves, against a REAL git repository (real `apply_and_commit` calls, real
`Fleet-Task-Id` trailers derived from `task_id_for_ids` — never raw SQL fabrication, CLAUDE.md
Rule 9):

* every unit's commit present -> the row goes `DONE`, `attempts.commit_sha` is the LAST landed
  unit's sha, `phases.post_commit_sha` is written (the existing single-`find_task_commit` DONE
  path, reached via the new per-unit loop).
* zero units' commits present -> the existing discard path fires unchanged, `discard_task` IS
  called, the row goes back to `PENDING`.
* some-but-not-all units' commits present -> `discard_task` is NEVER called (proven by asserting
  the landed units' commits still resolve on the branch afterward, not by mocking), the row goes
  back to re-claimable `PENDING` (fence bumped), and the report's `partially_landed` entry carries
  the correct landed/missing split.
* `--dry-run` computes the identical partial verdict and writes nothing (no SQL write, no git
  write, `discard_task` still never called).

**Rule 12 mutation proof (manual, not an automated test in this file — mirrors
`tests/test_d89_phase2_claim_lifecycle.py`'s own precedent for its happy-path mutation).** The
"all landed" branch's condition in `cli.py`'s `_reconcile_tasks_with_git` is:

    if units and not missing_units:
        ...
    elif not landed_units:
        ...
    else:
        ...  # partially landed

Mutating the first line's `not missing_units` to the literal `False` (so the branch can never be
taken) was applied to a throwaway copy of `cli.py` inside this task's worktree and the full file
was re-run: `test_all_units_landed_marks_done_...` turned RED (the row landed in the
`partially_landed` branch instead of `DONE`, because `landed_units` is non-empty while the
mutated condition is always false), and no other test in this module changed verdict — in
particular `test_zero_units_landed_uses_existing_discard_path` and
`test_some_units_landed_never_calls_discard_task_...` stayed GREEN, because neither reaches the
mutated line's TRUE branch to begin with. `git diff` against the pre-mutation copy showed exactly
the one changed line (non-zero, the CLAUDE.md Rule-12 zero-change guard). See this task's report
for the exact commands and pre/post pytest output.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from fleet.cli import (
    TransformInput,
    _arbitration_lines,
    _reconcile_tasks_with_git,
    _TransformClaimHook,
)
from fleet.models.enums import Phase
from fleet.settings import FleetSettings
from fleet.state.db import SCHEMA_PATH, StateWriter, connect_ro
from fleet.state.repository import SqliteStateRepository
from fleet.util.proc import ProcResult, run
from fleet.vcs import commits as C
from fleet.vcs.git import Git
from fleet.workers.rewrite import task_id_for_ids
from tests.test_cli import write_config

RUN_ID = "run-d89-phase2-task-b"
REPO = "acme-commons"
PHASE = Phase.TRANSFORM
UNIT_A = "libs/com/acme/commons/A.java"
UNIT_B = "libs/com/acme/commons/B.java"
NOW_ISO = "2026-09-01T12:00:00+00:00"
HORIZONS = ("2020-01-01T00:00:00+00:00", "2030-01-01T00:00:00+00:00")


# ======================================================================================
# real-repository helpers (mirrors tests/test_vcs.py's own fixtures; kept local rather than
# imported so this file's git-repo shape — TWO tracked files, one per unit — is explicit here)
# ======================================================================================


async def _sh(cwd: Path, *args: str) -> ProcResult:
    return await run(["git", *args], cwd=cwd, timeout_s=60)


def _write_files(path: Path, files: Mapping[str, str]) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)


def _swap_text(target: Path, new_text: str) -> str:
    original = target.read_text()
    target.write_text(new_text)
    return original


async def _init_repo(path: Path, *, files: Mapping[str, str]) -> None:
    _write_files(path, {})  # sync mkdir, kept out of the async body (ruff ASYNC240)
    await _sh(path, "init", "--initial-branch=main", ".")
    await _sh(path, "config", "user.email", "fleet@example.invalid")
    await _sh(path, "config", "user.name", "Fleet Test")
    _write_files(path, files)
    await _sh(path, "add", "--all")
    await _sh(path, "commit", "-m", "initial")


async def _build_patch(git_: Git, rel: str, new_text: str, patch_path: Path) -> Path:
    target = git_.path / rel
    original = _swap_text(target, new_text)
    diff = await git_.text(["diff", "--", rel])
    _write_files(patch_path.parent, {patch_path.name: diff + "\n"})
    _swap_text(target, original)
    return patch_path


@dataclass(frozen=True, slots=True)
class _Patch:
    path: str
    diff: str


async def _land_unit(
    git_: Git, *, unit: str, phase_anchor: str, patch_dir: Path, ordinal: int
) -> str:
    """Real `apply_and_commit`, real `Fleet-Task-Id` trailer via `task_id_for_ids` — the exact
    formula `land_patches` uses to write it (Rule 9: no raw SQL fabrication of the git side)."""
    patch = await _build_patch(git_, unit, f"v2-{ordinal}\n", patch_dir / f"p{ordinal}.patch")
    pid = C.patch_id([_Patch(unit, patch.read_text())])
    synth_id = task_id_for_ids(RUN_ID, REPO, int(PHASE), unit)
    outcome = await C.apply_and_commit(
        git_,
        patch=patch,
        subject=f"fleet: rewrite {unit}",
        trailers=C.FleetTrailers(
            run_id=RUN_ID,
            repo_id=REPO,
            phase=int(PHASE),
            task_id=synth_id,
            attempt=1,
            patch_id=pid,
        ),
        branch=f"migrate/{REPO}",
        pre_commit_sha=phase_anchor,
    )
    assert outcome.committed and outcome.commit_sha is not None
    return outcome.commit_sha


# ======================================================================================
# DB seeding — raw sqlite3, mirroring tests/test_cli.py's own fresh_db/seed_run convention
# (synchronous seeding of fixture state, not the async write path under test)
# ======================================================================================


def _fresh_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    finally:
        conn.close()


def _seed(
    db_path: Path,
    *,
    task_id: str,
    task_anchor: str,
    phase_anchor: str,
    target_paths: list[str],
    attempt_id: str = "att-1",
) -> None:
    """Runs, repo, one PENDING phase row (a step-3-reset shape — the row a crashed phase leaves
    behind before step 4 ever runs), one RUNNING REWRITE task row with `target_paths` populated
    (Task A's own claim-lifecycle shape), and one `attempts` row (`ANCHORED_REPEAT`/no `exit_code`
    — a placeholder rung; only `attempt_id`/`task_id` scoping matters to `_persist_arbitration`).
    """
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO runs (run_id, started_at, config_sha256, harness_version) "
            "VALUES (?, ?, ?, ?)",
            (RUN_ID, NOW_ISO, "a" * 64, "0.1.0"),
        )
        conn.execute(
            "INSERT INTO repos (repo_id, name, url, updated_at) VALUES (?, ?, ?, ?)",
            (REPO, REPO, f"https://example.invalid/{REPO}", NOW_ISO),
        )
        conn.execute(
            "INSERT INTO phases (run_id, repo_id, phase, status, pre_commit_sha, max_attempts, "
            "                    updated_at) "
            "VALUES (?, ?, ?, 'PENDING', ?, 3, ?)",
            (RUN_ID, REPO, int(PHASE), phase_anchor, NOW_ISO),
        )
        conn.execute(
            "INSERT INTO tasks (task_id, run_id, repo_id, phase, kind, dest_path, target_paths, "
            "                   status, claimed_by, lease_expires_at, fence_token, "
            "                   pre_commit_sha, ladder, created_at) "
            "VALUES (?, ?, ?, ?, 'REWRITE', ?, ?, 'RUNNING', 'test-owner', ?, 1, ?, "
            '        \'[null,"EVIDENCE_ONLY","EVIDENCE_PLUS_REJECTED_APPROACHES"]\', ?)',
            (
                task_id,
                RUN_ID,
                REPO,
                int(PHASE),
                "libs/com/acme/commons",
                json.dumps(target_paths),
                NOW_ISO,
                task_anchor,
                NOW_ISO,
            ),
        )
        conn.execute(
            "INSERT INTO attempts (attempt_id, run_id, repo_id, task_id, phase, attempt, "
            "                      failure_class, started_at, finished_at) "
            "VALUES (?, ?, ?, ?, ?, 1, 'ANCHORED_REPEAT', ?, ?)",
            (attempt_id, RUN_ID, REPO, task_id, int(PHASE), NOW_ISO, NOW_ISO),
        )
    finally:
        conn.close()


def _seed_run_repo_phase(db_path: Path, *, phase_anchor: str) -> None:
    """The `runs`/`repos`/`phases` third of `_seed` above, WITHOUT a `tasks`/`attempts` row —
    for D91's downstream-consequence test, which needs the coarse `tasks` row (and its
    `pre_commit_sha`) minted by a REAL `_TransformClaimHook` claim, not fabricated by this file's
    raw-SQL fixture (CLAUDE.md Rule 9)."""
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO runs (run_id, started_at, config_sha256, harness_version) "
            "VALUES (?, ?, ?, ?)",
            (RUN_ID, NOW_ISO, "a" * 64, "0.1.0"),
        )
        conn.execute(
            "INSERT INTO repos (repo_id, name, url, updated_at) VALUES (?, ?, ?, ?)",
            (REPO, REPO, f"https://example.invalid/{REPO}", NOW_ISO),
        )
        conn.execute(
            "INSERT INTO phases (run_id, repo_id, phase, status, pre_commit_sha, max_attempts, "
            "                    updated_at) "
            "VALUES (?, ?, ?, 'PENDING', ?, 3, ?)",
            (RUN_ID, REPO, int(PHASE), phase_anchor, NOW_ISO),
        )
    finally:
        conn.close()


def _phases_row(db_path: Path) -> tuple[str, str | None]:
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        row = conn.execute(
            "SELECT status, post_commit_sha FROM phases WHERE run_id = ? AND repo_id = ? "
            "  AND phase = ?",
            (RUN_ID, REPO, int(PHASE)),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return str(row[0]), (None if row[1] is None else str(row[1]))


def _tasks_row(db_path: Path) -> tuple[str, str | None, int]:
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        row = conn.execute(
            "SELECT status, claimed_by, fence_token FROM tasks WHERE run_id = ? AND repo_id = ? "
            "  AND phase = ?",
            (RUN_ID, REPO, int(PHASE)),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return str(row[0]), (None if row[1] is None else str(row[1])), int(row[2])


def _attempts_commit_sha(db_path: Path, attempt_id: str = "att-1") -> str | None:
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        row = conn.execute(
            "SELECT commit_sha FROM attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return None if row[0] is None else str(row[0])


@dataclass(frozen=True, slots=True)
class _Fixture:
    settings: FleetSettings
    db_path: Path
    git: Git
    branch: str
    phase_anchor: str
    task_id: str
    patch_dir: Path


@pytest.fixture
def fx(tmp_path: Path) -> _Fixture:
    write_config(tmp_path)
    settings = FleetSettings.load(tmp_path / "config")
    db_path = tmp_path / "state" / "fleet.db"
    _fresh_db(db_path)

    work_root = (settings.root / settings.config.run.work_dir).resolve()
    worktree = work_root / REPO
    branch = f"migrate/{REPO}"

    async def build() -> Git:
        await _init_repo(worktree, files={UNIT_A: "v1\n", UNIT_B: "v1\n"})
        await _sh(worktree, "checkout", "-b", branch)
        return Git(worktree, timeout_s=60)

    git_ = asyncio.run(build())
    phase_anchor = asyncio.run(git_.rev_parse(branch))
    task_id = str(UUID(int=0xD89))

    return _Fixture(
        settings=settings,
        db_path=db_path,
        git=git_,
        branch=branch,
        phase_anchor=phase_anchor,
        task_id=task_id,
        patch_dir=tmp_path / "patches",
    )


# ======================================================================================
# 1. all units landed
# ======================================================================================


async def test_all_units_landed_marks_done_with_last_unit_sha_and_writes_post_commit_sha(
    fx: _Fixture,
) -> None:
    sha_a = await _land_unit(
        fx.git, unit=UNIT_A, phase_anchor=fx.phase_anchor, patch_dir=fx.patch_dir, ordinal=1
    )
    sha_b = await _land_unit(
        fx.git, unit=UNIT_B, phase_anchor=fx.phase_anchor, patch_dir=fx.patch_dir, ordinal=2
    )
    _seed(
        fx.db_path,
        task_id=fx.task_id,
        task_anchor=fx.phase_anchor,
        phase_anchor=fx.phase_anchor,
        target_paths=[UNIT_A, UNIT_B],
    )

    report = await _reconcile_tasks_with_git(
        fx.settings, fx.db_path, RUN_ID, horizons=HORIZONS, dry_run=False
    )

    assert report["candidates"] == 1
    assert report["discarded"] == []
    assert report["partially_landed"] == []
    landed = report["landed"]
    assert isinstance(landed, list) and len(landed) == 1
    assert landed[0]["commit_sha"] == sha_b != sha_a, (
        "the LAST landed unit's sha is what attempts.commit_sha/phases.post_commit_sha get — "
        "the same convention _TransformSink uses for output.commits[-1], not the first unit's"
    )

    status, claimed_by, _fence = _tasks_row(fx.db_path)
    assert (status, claimed_by) == ("DONE", None)
    _phase_status, post_commit_sha = _phases_row(fx.db_path)
    assert post_commit_sha == sha_b
    assert _attempts_commit_sha(fx.db_path) == sha_b


# ======================================================================================
# 2. zero units landed — existing discard path, unchanged
# ======================================================================================


async def test_zero_units_landed_uses_existing_discard_path(fx: _Fixture) -> None:
    _seed(
        fx.db_path,
        task_id=fx.task_id,
        task_anchor=fx.phase_anchor,
        phase_anchor=fx.phase_anchor,
        target_paths=[UNIT_A, UNIT_B],
    )

    report = await _reconcile_tasks_with_git(
        fx.settings, fx.db_path, RUN_ID, horizons=HORIZONS, dry_run=False
    )

    assert report["landed"] == []
    assert report["partially_landed"] == []
    discarded = report["discarded"]
    assert isinstance(discarded, list) and len(discarded) == 1

    status, claimed_by, fence = _tasks_row(fx.db_path)
    assert (status, claimed_by) == ("PENDING", None)
    assert fence == 2, "discard_task's fence bump: claimed at 1, reset bumps to 2"
    # the worktree tip is back at the task anchor — discard_task really ran
    tip = await fx.git.rev_parse(fx.branch)
    assert tip == fx.phase_anchor


async def test_a_real_claim_hook_anchor_lets_the_discard_path_fire_instead_of_hanging_unresolved(
    fx: _Fixture,
) -> None:
    """D91 (`docs/INTEGRATION_HONESTY.md`) — the downstream consequence, proven end to end rather
    than by seeding `tasks.pre_commit_sha` directly (as `test_zero_units_landed_uses_existing_
    discard_path` above does): before this fix, NOTHING in production ever wrote the column, so a
    crashed REWRITE coarse row with zero units landed had `task_anchor is None` and
    `_reconcile_tasks_with_git` always took its `_unresolved` branch ("the task row carries no
    `pre_commit_sha` to reset to") — the row stayed `RUNNING` forever, never reaching `discarded`.

    This drives the REAL `_TransformClaimHook` (the one place D91's fix landed) to claim the row,
    exactly as `_run_transform_wave` wires it, and only THEN calls `_reconcile_tasks_with_git` —
    no unit's commit exists yet, so this is the zero-landed crash scenario, and the real anchor
    the hook wrote is what should let the discard path fire instead of `unresolved`.
    """
    _seed_run_repo_phase(fx.db_path, phase_anchor=fx.phase_anchor)
    work_root = (fx.settings.root / fx.settings.config.run.work_dir).resolve()
    now = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)

    async with StateWriter(fx.db_path, owner="d91-consequence-test-writer") as writer:
        read_conn = await connect_ro(fx.db_path)
        try:
            repo = SqliteStateRepository(writer=writer, read_conn=read_conn)
            hook = _TransformClaimHook(
                repository=repo,
                read_conn=read_conn,
                run_id=RUN_ID,
                owner="test-owner",
                clock=lambda: now,
                lease_ttl_s=600,
                work_dir=work_root,
            )
            payload = TransformInput(
                repo_id=REPO,
                branch=fx.branch,
                phase_pre_commit_sha=fx.phase_anchor,
                dest_path="libs/com/acme/commons",
                sources=(UNIT_A,),
                targets=(UNIT_B,),
            )
            await hook(repo_id=REPO, phase=PHASE, payload=payload)
        finally:
            await read_conn.close()

    status_before, claimed_by_before, fence_before = _tasks_row(fx.db_path)
    assert (status_before, claimed_by_before, fence_before) == ("RUNNING", "test-owner", 1), (
        "the real hook must have actually claimed the row before reconciliation runs"
    )

    report = await _reconcile_tasks_with_git(
        fx.settings, fx.db_path, RUN_ID, horizons=HORIZONS, dry_run=False
    )

    assert report["unresolved"] == [], (
        "D91: with a real per-task anchor now on the row, this must NOT fall into `unresolved` "
        "— that is the exact hang this fix closes"
    )
    assert report["landed"] == []
    assert report["partially_landed"] == []
    discarded = report["discarded"]
    assert isinstance(discarded, list) and len(discarded) == 1, (
        "zero units landed + a real anchor must reach the discard branch, a genuine terminal "
        "verdict, instead of leaving the row exactly as it was"
    )

    status, claimed_by, fence = _tasks_row(fx.db_path)
    assert (status, claimed_by) == ("PENDING", None), (
        "discarded is re-claimable PENDING, not the RUNNING-forever hang D91 describes"
    )
    assert fence == 2, "discard_task's fence bump: claimed at 1, reset bumps to 2"
    tip = await fx.git.rev_parse(fx.branch)
    assert tip == fx.phase_anchor, "the worktree tip is back at the task anchor — discard_task ran"


# ======================================================================================
# 3. some-but-not-all units landed — the NEW partially-landed verdict
# ======================================================================================


async def test_some_units_landed_never_calls_discard_task_and_resets_to_pending(
    fx: _Fixture,
) -> None:
    sha_a = await _land_unit(
        fx.git, unit=UNIT_A, phase_anchor=fx.phase_anchor, patch_dir=fx.patch_dir, ordinal=1
    )
    _seed(
        fx.db_path,
        task_id=fx.task_id,
        task_anchor=fx.phase_anchor,
        phase_anchor=fx.phase_anchor,
        target_paths=[UNIT_A, UNIT_B],
    )

    report = await _reconcile_tasks_with_git(
        fx.settings, fx.db_path, RUN_ID, horizons=HORIZONS, dry_run=False
    )

    assert report["landed"] == []
    assert report["discarded"] == [], (
        "discard_task must NEVER be invoked for a partially-landed row — it would delete unit "
        "A's real, durable commit"
    )
    partial = report["partially_landed"]
    assert isinstance(partial, list) and len(partial) == 1
    entry = partial[0]
    assert entry["task_id"] == fx.task_id
    assert entry["landed_units"] == [UNIT_A]
    assert entry["missing_units"] == [UNIT_B]

    status, claimed_by, fence = _tasks_row(fx.db_path)
    assert (status, claimed_by) == ("PENDING", None), "re-claimable, not DONE and not left RUNNING"
    assert fence == 2, "the same fence bump discard_task's reset gets — the hazard is identical"

    # The proof that discard_task never ran: unit A's commit still resolves on the branch, and
    # the tip is STILL sha_a, not reset back to phase_anchor.
    tip = await fx.git.rev_parse(fx.branch)
    assert tip == sha_a
    assert (
        await C.find_task_commit(
            fx.git,
            branch=fx.branch,
            pre_commit_sha=fx.phase_anchor,
            task_id=task_id_for_ids(RUN_ID, REPO, int(PHASE), UNIT_A),
        )
        == sha_a
    )

    # And the row is genuinely re-claimable, exactly as Task A's claim CAS expects.
    conn = sqlite3.connect(fx.db_path, isolation_level=None)
    try:
        won = conn.execute(
            "UPDATE tasks SET status = 'RUNNING' WHERE task_id = ? AND status = 'PENDING' "
            "RETURNING task_id",
            (fx.task_id,),
        ).fetchone()
    finally:
        conn.close()
    assert won is not None


async def test_partial_landing_survives_a_third_unit_untouched_all_around(fx: _Fixture) -> None:
    """A regression the two-unit fixture above cannot express: with three units and one missing,
    the landed set can have more than one member, and both must be reported and both must survive
    (not just "the first one")."""
    sha_a = await _land_unit(
        fx.git, unit=UNIT_A, phase_anchor=fx.phase_anchor, patch_dir=fx.patch_dir, ordinal=1
    )
    unit_c = "libs/com/acme/commons/C.java"
    _write_files(fx.git.path, {unit_c: "v1\n"})
    await _sh(fx.git.path, "add", "--all")
    await _sh(fx.git.path, "commit", "-m", "seed unit C")
    sha_c = await _land_unit(
        fx.git, unit=unit_c, phase_anchor=fx.phase_anchor, patch_dir=fx.patch_dir, ordinal=2
    )
    _seed(
        fx.db_path,
        task_id=fx.task_id,
        task_anchor=fx.phase_anchor,
        phase_anchor=fx.phase_anchor,
        target_paths=[UNIT_A, UNIT_B, unit_c],
    )

    report = await _reconcile_tasks_with_git(
        fx.settings, fx.db_path, RUN_ID, horizons=HORIZONS, dry_run=False
    )

    entry = report["partially_landed"][0]  # type: ignore[index]
    assert sorted(entry["landed_units"]) == sorted([UNIT_A, unit_c])
    assert entry["missing_units"] == [UNIT_B]
    tip = await fx.git.rev_parse(fx.branch)
    assert tip == sha_c
    assert await fx.git.resolve(sha_a) == sha_a


async def test_partially_landed_verdict_renders_a_human_readable_line_not_only_json(
    fx: _Fixture,
) -> None:
    """`_arbitration_lines` (cli.py) had no case for `partially_landed` — the function's own
    docstring says a silent count must not let a partial reconciliation read as a complete one,
    and printing nothing for this verdict was exactly that gap. The other tests in this file
    assert the `--json` payload's `report["partially_landed"]`; this proves the rendered,
    human-readable CLI line surfaces the same verdict too, against the real report
    `_reconcile_tasks_with_git` produces (not a hand-built stand-in for it).
    """
    await _land_unit(
        fx.git, unit=UNIT_A, phase_anchor=fx.phase_anchor, patch_dir=fx.patch_dir, ordinal=1
    )
    _seed(
        fx.db_path,
        task_id=fx.task_id,
        task_anchor=fx.phase_anchor,
        phase_anchor=fx.phase_anchor,
        target_paths=[UNIT_A, UNIT_B],
    )

    report = await _reconcile_tasks_with_git(
        fx.settings, fx.db_path, RUN_ID, horizons=HORIZONS, dry_run=False
    )

    lines = _arbitration_lines({"git_arbitration": report}, dry=False)
    joined = " ".join(lines)
    assert f"task {fx.task_id} PARTIALLY landed" in joined
    assert "1 of 2 units" in joined
    assert UNIT_B in joined, "the missing unit must be named, not just counted"


# ======================================================================================
# 4. --dry-run computes the same verdict, writes nothing
# ======================================================================================


async def test_dry_run_computes_the_same_partial_verdict_and_writes_nothing(
    fx: _Fixture,
) -> None:
    await _land_unit(
        fx.git, unit=UNIT_A, phase_anchor=fx.phase_anchor, patch_dir=fx.patch_dir, ordinal=1
    )
    _seed(
        fx.db_path,
        task_id=fx.task_id,
        task_anchor=fx.phase_anchor,
        phase_anchor=fx.phase_anchor,
        target_paths=[UNIT_A, UNIT_B],
    )
    status_before, claimed_by_before, fence_before = _tasks_row(fx.db_path)

    report = await _reconcile_tasks_with_git(
        fx.settings, fx.db_path, RUN_ID, horizons=HORIZONS, dry_run=True
    )

    assert report["applied"] is False
    partial = report["partially_landed"]
    assert isinstance(partial, list) and len(partial) == 1
    assert partial[0]["landed_units"] == [UNIT_A]
    assert partial[0]["missing_units"] == [UNIT_B]

    status_after, claimed_by_after, fence_after = _tasks_row(fx.db_path)
    assert (status_after, claimed_by_after, fence_after) == (
        status_before,
        claimed_by_before,
        fence_before,
    ), "dry-run must write nothing to `tasks`"
    tip = await fx.git.rev_parse(fx.branch)
    assert tip != fx.phase_anchor, "dry-run must not have called discard_task either"


# ======================================================================================
# 5. non-REWRITE kinds keep the single-find_task_commit path byte-for-byte (regression guard)
# ======================================================================================


async def test_non_rewrite_kind_ignores_target_paths_and_uses_the_coarse_task_id(
    fx: _Fixture,
) -> None:
    """HOIST/BUILDGEN/RDEP_VERIFY/PR_EMIT/REVALIDATE must be completely unaffected: the row's own
    `task_id` (not any per-unit id) is the commit identity, exactly as before this task.
    """
    coarse_id = task_id_for_ids(RUN_ID, REPO, int(PHASE), "coarse-marker")
    patch = await _build_patch(fx.git, UNIT_A, "v2\n", fx.patch_dir / "coarse.patch")
    pid = C.patch_id([_Patch(UNIT_A, patch.read_text())])
    outcome = await C.apply_and_commit(
        fx.git,
        patch=patch,
        subject="fleet: buildgen",
        trailers=C.FleetTrailers(
            run_id=RUN_ID,
            repo_id=REPO,
            phase=int(PHASE),
            task_id=coarse_id,
            attempt=1,
            patch_id=pid,
        ),
        branch=fx.branch,
        pre_commit_sha=fx.phase_anchor,
    )
    assert outcome.commit_sha is not None

    conn = sqlite3.connect(fx.db_path, isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO runs (run_id, started_at, config_sha256, harness_version) "
            "VALUES (?, ?, ?, ?)",
            (RUN_ID, NOW_ISO, "a" * 64, "0.1.0"),
        )
        conn.execute(
            "INSERT INTO repos (repo_id, name, url, updated_at) VALUES (?, ?, ?, ?)",
            (REPO, REPO, f"https://example.invalid/{REPO}", NOW_ISO),
        )
        conn.execute(
            "INSERT INTO phases (run_id, repo_id, phase, status, pre_commit_sha, max_attempts, "
            "                    updated_at) "
            "VALUES (?, ?, ?, 'PENDING', ?, 3, ?)",
            (RUN_ID, REPO, int(Phase.BUILD), fx.phase_anchor, NOW_ISO),
        )
        conn.execute(
            "INSERT INTO tasks (task_id, run_id, repo_id, phase, kind, dest_path, target_paths, "
            "                   status, claimed_by, lease_expires_at, fence_token, "
            "                   pre_commit_sha, ladder, created_at) "
            "VALUES (?, ?, ?, ?, 'BUILDGEN', ?, '[]', 'RUNNING', 'test-owner', ?, 1, ?, "
            '        \'[null,"EVIDENCE_ONLY","EVIDENCE_PLUS_REJECTED_APPROACHES"]\', ?)',
            (
                str(coarse_id),
                RUN_ID,
                REPO,
                int(Phase.BUILD),
                "libs/com/acme/commons",
                fx.phase_anchor,
                NOW_ISO,
                NOW_ISO,
            ),
        )
    finally:
        conn.close()

    report = await _reconcile_tasks_with_git(
        fx.settings, fx.db_path, RUN_ID, horizons=HORIZONS, dry_run=False
    )

    landed = report["landed"]
    assert isinstance(landed, list) and len(landed) == 1
    assert landed[0]["task_id"] == str(coarse_id)
    assert landed[0]["commit_sha"] == outcome.commit_sha
    assert report["partially_landed"] == []
