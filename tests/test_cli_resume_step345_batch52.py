"""Round VIII, §15.1 item 3, Wave 7.9 batch 52 (group G10b): mutation-proof tests for cli.py's
stale-task reconciliation group — `_reset_stale_running`, `_unresolved`, `_recreate_phase_anchor`,
`_persist_arbitration`, `_demote_to_floors`, `_floor_reason`, `_apply_floor_demotions`.
`_reconcile_tasks_with_git` is out of scope (already proven).

Each test below calls its target function DIRECTLY rather than through the full `fleet resume`
CLI path, following the precedent `tests/test_d89_phase2_reconciliation.py` already set for this
same neighbourhood of `cli.py` (its `fx` fixture calls `_reconcile_tasks_with_git` directly). This
keeps each fixture to exactly the rows/args the function under test reads, so the assertion is
squarely on that function rather than on the whole resume pipeline's incidental behaviour.

New file (rather than adding to `tests/test_cli.py`) per this round's own established convention
(batch 51's `tests/test_cli_resume_report_lines_batch51.py`) — avoids a merge collision with
sibling lanes editing that file concurrently.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from fleet.cli import (
    _apply_floor_demotions,
    _demote_to_floors,
    _floor_reason,
    _persist_arbitration,
    _recreate_phase_anchor,
    _reset_stale_running,
    _unresolved,
)
from fleet.models.enums import Phase, RepoStatus
from fleet.settings import FleetSettings
from fleet.state.db import SCHEMA_PATH
from fleet.util.proc import run
from fleet.vcs.git import Git
from tests.test_cli import write_config

RUN_ID = "run-batch52"
NOW_ISO = "2026-09-15T12:00:00+00:00"
# (config_cutoff, now) -- see `cli.py:17271`. A heartbeat before the cutoff, with a huge gap to
# `now`, satisfies `_STALE_HEARTBEAT_PREDICATE`'s conjunction against the schema's default
# `heartbeat_ttl_seconds` (300) many times over.
HORIZONS = ("2025-01-01T00:00:00+00:00", "2030-01-01T00:00:00+00:00")
STALE_HEARTBEAT = "2020-01-01T00:00:00.000000+00:00"


def _fresh_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    finally:
        conn.close()


def _seed_run_and_repos(db_path: Path, repo_ids: tuple[str, ...]) -> None:
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO runs (run_id, started_at, config_sha256, harness_version) "
            "VALUES (?, ?, ?, ?)",
            (RUN_ID, NOW_ISO, "a" * 64, "0.1.0"),
        )
        for repo_id in repo_ids:
            conn.execute(
                "INSERT INTO repos (repo_id, name, url, updated_at) VALUES (?, ?, ?, ?)",
                (repo_id, repo_id, f"https://example.invalid/{repo_id}", NOW_ISO),
            )
    finally:
        conn.close()


# ==========================================================================================
# 1. `_reset_stale_running` -- the sweep must reclaim EVERY stale row, not only the first
# ==========================================================================================


def _put_phase(
    db_path: Path,
    *,
    repo_id: str,
    phase: int,
    heartbeat_at: str | None,
    lease_fence: int = 1,
) -> None:
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO phases (run_id, repo_id, phase, status, heartbeat_at, lease_owner, "
            "                    lease_fence, updated_at) "
            "VALUES (?, ?, ?, 'RUNNING', ?, 'host:cid:1:boot', ?, ?)",
            (RUN_ID, repo_id, phase, heartbeat_at, lease_fence, NOW_ISO),
        )
    finally:
        conn.close()


def _phase_lease_state(db_path: Path, repo_id: str, phase: int) -> tuple[str, str | None, int]:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT status, lease_owner, lease_fence FROM phases "
            " WHERE run_id = ? AND repo_id = ? AND phase = ?",
            (RUN_ID, repo_id, phase),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return str(row[0]), (None if row[1] is None else str(row[1])), int(row[2])


def test_reset_stale_running_reclaims_every_stale_row_in_one_sweep_not_only_the_first(
    tmp_path: Path,
) -> None:
    """§11.5 step 3's `UPDATE ... WHERE run_id = ? AND status = 'RUNNING'` carries no `LIMIT` and
    no per-repo scope -- every stale row in the run is reclaimed in the ONE sweep, not merely the
    first one a naive single-row implementation (or a mistakenly `LIMIT 1`-ed rewrite) would
    reclaim. Every existing test in `tests/test_cli.py` covering this function seeds exactly one
    stale `phases` row, so a regression that silently scoped the statement to one row would pass
    every one of them.

    Rule 12 manual mutation proof: appending `LIMIT 1` to `_RESET_RUNNING_TO_PENDING_SQL` in a
    throwaway copy of `cli.py` and re-running this test alone turns it RED (`reclaimed == 1`,
    one repo's fence left un-bumped) while every existing single-row step-3 test in
    `tests/test_cli.py` stays GREEN, because none of them can express two simultaneously-stale
    rows. `git diff` against the pre-mutation backup showed exactly the one appended token; the
    restored file was byte-identical to the backup afterward.
    """
    db_path = tmp_path / "state" / "fleet.db"
    _fresh_db(db_path)
    _seed_run_and_repos(db_path, ("acme-commons", "acme-billing"))
    _put_phase(
        db_path, repo_id="acme-commons", phase=1, heartbeat_at=STALE_HEARTBEAT, lease_fence=1
    )
    _put_phase(
        db_path, repo_id="acme-billing", phase=2, heartbeat_at=STALE_HEARTBEAT, lease_fence=7
    )

    reclaimed = asyncio.run(_reset_stale_running(db_path, RUN_ID, HORIZONS))

    assert reclaimed == 2, "the sweep reclaimed fewer stale rows than the fixture seeded"
    for repo_id, phase, expected_fence in (("acme-commons", 1, 2), ("acme-billing", 2, 8)):
        status, owner, fence = _phase_lease_state(db_path, repo_id, phase)
        assert (status, owner, fence) == ("PENDING", None, expected_fence), (
            f"{repo_id} phase {phase} was not reclaimed by the same sweep as its sibling"
        )


# ==========================================================================================
# 2. `_unresolved` -- the reason is redacted, not merely recorded
# ==========================================================================================

FAKE_GITHUB_PAT = "github_pat_" + "A1b2C3d4E5f6G7h8I9j0" + "K1l2M3n4O5p6Q7r8S9t0"


def test_unresolved_redacts_the_reason_before_it_reaches_the_report() -> None:
    """`_unresolved` is the ONE place every branch across step 4 and step 5 appends to
    `report["unresolved"]` from (its own docstring: "so no branch can forget the reason"). Every
    existing caller in production supplies a reason built from a worktree path, a ref name or
    `f"{type(exc).__name__}: {exc}"` -- and a live `GitCommandError`'s `stderr_tail` can carry
    whatever a subprocess wrote, including a credential embedded in a remote URL a misconfigured
    clone leaked into an error message. `redact_text` is called on every such string via
    `_unresolved`, not left to each of its ~8 call sites to remember individually.

    No existing test's fixture-generated reason strings ("no worktree at {worktree}", "{branch}
    does not exist in {worktree}", a fabricated `GitCommandError`'s deadline message) contain
    anything a redaction pattern matches, so none of them can tell a working `redact_text` call
    apart from one silently dropped. This constructs a reason a pattern DOES match.

    Rule 12 manual mutation proof: replacing `redact_text(reason)` with bare `reason` in a
    throwaway copy of `cli.py` and re-running this test alone turns it RED (the raw token
    surfaces verbatim in the report) while grepping `tests/test_cli.py` for every existing
    `_unresolved`-reached assertion shows none of their reason strings match any pattern in
    `fleet.obs.redact.PATTERNS`, so they would stay GREEN under the same mutation. Restored
    afterward; `git diff` against the backup was empty.
    """
    report: dict[str, object] = {"unresolved": []}
    reason = f"remote rejected the fetch: authorization failed for token {FAKE_GITHUB_PAT}"

    _unresolved(report, {"repo_id": "acme-commons"}, reason)

    entries = report["unresolved"]
    assert isinstance(entries, list) and len(entries) == 1
    entry = entries[0]
    assert entry["repo_id"] == "acme-commons"
    assert FAKE_GITHUB_PAT not in str(entry["reason"]), (
        "the raw secret reached the report -- redact_text was not applied"
    )
    assert "«redacted:github_pat:" in str(entry["reason"]), (
        "the reason was altered but not via the github_pat detector's own placeholder shape"
    )


# ==========================================================================================
# 3. `_recreate_phase_anchor` -- an unrecoverable anchor returns None and touches git NOT AT ALL
# ==========================================================================================


async def _sh(cwd: Path, *args: str) -> None:
    result = await run(["git", *args], cwd=cwd, timeout_s=30)
    assert result.exit_code == 0, result.stderr_tail


def _init_tiny_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)

    async def build() -> None:
        await _sh(path, "init", "--initial-branch=main", ".")
        await _sh(path, "config", "user.email", "fleet@example.invalid")
        await _sh(path, "config", "user.name", "Fleet Test")
        (path / "f.txt").write_text("v1\n", encoding="utf-8")
        await _sh(path, "add", "-A")
        await _sh(path, "commit", "-m", "initial")

    asyncio.run(build())


def test_recreate_phase_anchor_refuses_to_guess_when_there_is_nothing_to_recover_from(
    tmp_path: Path,
) -> None:
    """ "Returns ... `None` when there is nothing to recover from" (the function's own docstring).
    Every existing caller-level test that reaches this function (`tests/test_cli.py`'s step-4
    arbitration suite) seeds a RECOVERABLE anchor -- a real, resolvable `pre_commit_sha` behind
    the missing ref -- because that is the only shape their fixtures build. Nothing exercises the
    genuinely unrecoverable case this function's own `None` return exists for: a phase that never
    cut an anchor (`pre_commit_sha is None`) and a phase whose cached pointer no longer resolves
    at all (bit rot, or a corrupted column).

    Both are asserted here because a mutation that special-cased only one of the two conditions
    in the `or` (e.g. dropping the `git.resolve(...) is None` clause) would still pass a test
    that only tried the `None` half.

    Rule 12 manual mutation proof: replacing the guard `if pre_commit_sha is None or await
    git.resolve(pre_commit_sha) is None: return None` with `if pre_commit_sha is None: return
    None` in a throwaway copy of `cli.py` (dropping the resolve check) turned the *second*
    assertion below RED -- the function called `git.update_ref` with an unresolvable target,
    which raised `GitCommandError` inside the test rather than returning `None` -- while the
    first assertion (bare `pre_commit_sha=None`) stayed GREEN, confirming the two sub-cases are
    independent regressions. Restored afterward; `git diff` against the backup was empty.
    """
    worktree = tmp_path / "repo"
    _init_tiny_repo(worktree)
    git = Git(worktree)
    ref = f"refs/fleet/{RUN_ID}/acme-commons/phase-2/base"

    report_a: dict[str, object] = {"anchors_recreated": []}
    result_a = asyncio.run(
        _recreate_phase_anchor(
            git,
            report_a,
            run_id=RUN_ID,
            repo_id="acme-commons",
            phase=2,
            base_ref=ref,
            pre_commit_sha=None,
            dry_run=False,
        )
    )
    assert result_a is None
    assert report_a["anchors_recreated"] == []

    bogus_sha = "f" * 40
    report_b: dict[str, object] = {"anchors_recreated": []}
    result_b = asyncio.run(
        _recreate_phase_anchor(
            git,
            report_b,
            run_id=RUN_ID,
            repo_id="acme-commons",
            phase=2,
            base_ref=ref,
            pre_commit_sha=bogus_sha,
            dry_run=False,
        )
    )
    assert result_b is None
    assert report_b["anchors_recreated"] == []

    # Discriminating: neither call may have written the ref at all.
    for_each_ref = asyncio.run(
        run(["git", "for-each-ref", "--format=%(refname)", ref], cwd=worktree)
    )
    assert for_each_ref.stdout_tail.strip() == "", (
        "an unrecoverable anchor was recreated anyway, from a SHA nobody could resolve"
    )


# ==========================================================================================
# 4. `_persist_arbitration` -- picks the NEWEST `attempts` row, not merely a matching one
# ==========================================================================================


def _seed_task_with_two_attempts(db_path: Path, *, repo_id: str, phase: int, task_id: str) -> None:
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, run_id, repo_id, phase, kind, dest_path, status, "
            "                   created_at) "
            "VALUES (?, ?, ?, ?, 'RELOCATE', 'java/x', 'RUNNING', ?)",
            (task_id, RUN_ID, repo_id, phase, NOW_ISO),
        )
        conn.execute(
            "INSERT INTO phases (run_id, repo_id, phase, status, updated_at) "
            "VALUES (?, ?, ?, 'RUNNING', ?)",
            (RUN_ID, repo_id, phase, NOW_ISO),
        )
        # The STALE rung: attempt 1, never corrected -- must stay untouched.
        conn.execute(
            "INSERT INTO attempts (attempt_id, run_id, repo_id, task_id, phase, attempt, "
            "                      failure_class, started_at, finished_at) "
            "VALUES ('att-1', ?, ?, ?, ?, 1, 'ANCHORED_REPEAT', ?, ?)",
            (RUN_ID, repo_id, task_id, phase, NOW_ISO, NOW_ISO),
        )
        # The NEWEST rung: attempt 2 -- this is the one §11.5 step 4 must correct.
        conn.execute(
            "INSERT INTO attempts (attempt_id, run_id, repo_id, task_id, phase, attempt, "
            "                      failure_class, started_at, finished_at) "
            "VALUES ('att-2', ?, ?, ?, ?, 2, 'ANCHORED_REPEAT', ?, ?)",
            (RUN_ID, repo_id, task_id, phase, NOW_ISO, NOW_ISO),
        )
    finally:
        conn.close()


def _attempt_commit_sha(db_path: Path, attempt_id: str) -> str | None:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT commit_sha FROM attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return None if row[0] is None else str(row[0])


def test_persist_arbitration_corrects_the_newest_rungs_attempts_row_not_the_oldest(
    tmp_path: Path,
) -> None:
    """`_persist_arbitration`'s own docstring: "the newest rung ... fix which row ... BEFORE
    deciding what to do with it", selecting via `ORDER BY attempt DESC, revalidation_round DESC,
    retry_ordinal DESC LIMIT 1`. Every existing test reaching this function (the D87 fabricated-
    pointer test in `tests/test_cli.py`, and every `tests/test_d89_phase2_reconciliation.py` case)
    seeds exactly ONE `attempts` row per task -- so the `ORDER BY`'s tiebreak power is never
    exercised: a query with no `ORDER BY` at all, or one sorted the wrong direction, would return
    a row from a single-row table just as correctly.

    This seeds TWO rungs (a stale attempt 1 the retry ladder already moved past, and the live
    attempt 2) and asserts the correction lands on attempt 2 while attempt 1's `commit_sha`
    stays NULL -- the discriminating half no single-row fixture can express.

    Rule 12 manual mutation proof: changing `ORDER BY attempt DESC` to `ORDER BY attempt ASC` in
    a throwaway copy of `cli.py` and re-running this test alone turns it RED (attempt 1 is
    corrected instead of attempt 2) while `tests/test_d89_phase2_reconciliation.py` and the D87
    test in `tests/test_cli.py` both stay GREEN, because each seeds only one row and ASC/DESC
    pick the same one. Restored afterward; `git diff` against the backup was empty.
    """
    db_path = tmp_path / "state" / "fleet.db"
    _fresh_db(db_path)
    _seed_run_and_repos(db_path, ("acme-commons",))
    task_id = "11111111-1111-4111-8111-111111111111"
    _seed_task_with_two_attempts(db_path, repo_id="acme-commons", phase=2, task_id=task_id)
    sha = "b" * 40

    unwritten = asyncio.run(
        _persist_arbitration(
            db_path, RUN_ID, landed=[(task_id, "acme-commons", 2, sha)], discarded=(), partial=()
        )
    )

    assert unwritten == [], "the newest attempts row was not found at all"
    assert _attempt_commit_sha(db_path, "att-2") == sha, (
        "the newest rung (attempt 2) was not the row corrected to Git's answer"
    )
    assert _attempt_commit_sha(db_path, "att-1") is None, (
        "a STALE rung was corrected -- the ORDER BY is not selecting the newest attempt"
    )


# ==========================================================================================
# 5. `_demote_to_floors` -- a repo whose destination refuses to resolve is UNRESOLVED, not
#    silently dropped or crashed on
# ==========================================================================================


def _fleet_settings(tmp_path: Path) -> FleetSettings:
    write_config(tmp_path)
    return FleetSettings.load(tmp_path / "config")


def test_demote_to_floors_reports_a_repo_with_no_resolvable_destination_as_unresolved(
    tmp_path: Path,
) -> None:
    """`_demote_to_floors`' own docstring: "`None` only where `layout()` REFUSES a destination
    ... the caller reports that per repo" -- and the specific message is `"no destination
    resolves for this repo"` (`cli.py:18855`). No existing test (`tests/test_reentry_evidence.py`,
    `tests/test_resume_unblocking.py`, `tests/test_cli.py`'s step-5 suite, `tests/test_pr_e2e.py`,
    `tests/test_repository.py`, `tests/test_heavy_tier_outage_e2e.py`) seeds a `phases` row for a
    `repo_id` `_dest_paths` cannot resolve -- every one of them uses "acme-commons"/"acme-billing",
    both present in `REPOS_YAML` with a resolvable dest. So this branch, and the `continue` that
    skips it from ever reaching `resume_floor`/`evidence_holds`, has no coverage: a mutation that
    deleted the `if dest is None:` guard entirely (falling through to `RepoEvidence.for_repo`
    with `dest=None`) would pass every existing test in this project.

    A `phases` row for a repo absent from the `repos` table is the simplest way to reach it:
    `_repo_facts` (`cli.py:9319`) reads `FROM repos`, so a repo_id it never saw yields no
    `_RepoFacts` entry and `dests.get(repo_id)` is `None` -- the identical `None` `layout()`'s own
    refusal (`ReservedDestError`/`ValueError`) produces, without needing a reserved-namespace or
    root-escaping fixture.

    Rule 12 manual mutation proof: replacing the guard's condition with `if False:` in a
    throwaway copy of `cli.py` (so `dest=None` falls through to the rest of the loop instead of
    being reported) turned this test RED with a real, different failure --
    `sqlite3.IntegrityError: FOREIGN KEY constraint failed` (the demotion path went on to write a
    `PhaseDemoted` finding row for a `repo_id` that `repos` has no matching row for) -- and
    re-running the full step-5 suite in `tests/test_cli.py` (`-k resume_step_5`, 10 tests) stayed
    GREEN throughout, because none of those fixtures ever produce a `None` destination. Restored
    afterward; `git diff` against the backup was empty.
    """
    db_path = tmp_path / "state" / "fleet.db"
    _fresh_db(db_path)
    # Deliberately no `repos` row for "ghost-repo" -- only "acme-commons" exists in `repos.yaml`
    # and would resolve, so the ghost repo alone must land in `unresolved`.
    _seed_run_and_repos(db_path, ())
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO phases (run_id, repo_id, phase, status, updated_at) "
            "VALUES (?, 'ghost-repo', 3, 'SUCCEEDED', ?)",
            (RUN_ID, NOW_ISO),
        )
    finally:
        conn.close()

    settings = _fleet_settings(tmp_path)
    report, floors = asyncio.run(
        _demote_to_floors(settings, db_path, RUN_ID, dry_run=False, now=datetime.now(UTC))
    )

    assert report["candidates"] == 1
    assert report["demoted"] == []
    assert floors == {}, "a repo with no resolvable destination must not get a computed floor"
    unresolved = report["unresolved"]
    assert isinstance(unresolved, list) and len(unresolved) == 1
    assert unresolved[0] == {
        "repo_id": "ghost-repo",
        "reason": "no destination resolves for this repo",
    }

    # Discriminating: the phase row is untouched -- nothing was ever demoted or written.
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT status FROM phases WHERE run_id = ? AND repo_id = 'ghost-repo' AND phase = 3",
            (RUN_ID,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None and row[0] == "SUCCEEDED"


# ==========================================================================================
# 6. `_apply_floor_demotions` -- a repo that acquired REQUIRES_HUMAN_INTERVENTION between the
#    floor computation and the write is UNRESOLVED, and NOTHING is written for it
# ==========================================================================================


def test_apply_floor_demotions_refuses_a_repo_that_raced_to_requires_human_intervention(
    tmp_path: Path,
) -> None:
    """`_apply_floor_demotions`' own comment on the `applied != plan` branch: "The only way in:
    the repo acquired a REQUIRES_HUMAN_INTERVENTION row between the two reads, which
    `demote_to_floor` short-circuits to `()` rather than refusing. Reported, never printed as a
    demotion that happened." `tests/test_cli.py::test_resume_step_5_refuses_a_floor_whose_
    phase_rows_moved_under_it` covers the SIBLING branch (`FloorSnapshotStaleError`, a plain
    status mismatch against `observed`) but nothing covers THIS one -- and
    `state/repository.py`'s `demote_to_floor` checks `REQUIRES_HUMAN_INTERVENTION` in `rows`
    BEFORE ever comparing against `observed`, so the two branches are reached by genuinely
    different live-DB shapes, not two names for the same fixture.

    Calls `_apply_floor_demotions` directly with a `plans` entry computed from a stale
    `statuses` snapshot (as `_demote_to_floors` would have read it before the race), while the
    live `phases` row already carries `REQUIRES_HUMAN_INTERVENTION` -- the exact interleaving the
    comment describes, without needing two real concurrent processes.

    Rule 12 manual mutation proof: deleting the `if applied != plan:` guard (treating a `()`
    return as if it were simply "nothing to demote") in a throwaway copy of `cli.py` turned this
    test RED -- the repo silently vanished from `unresolved` and `report["demoted"]` kept the
    stale entry `kept.append(entry)` would otherwise have dropped it from, which is exactly the
    "a demotion that happened" the comment warns about. Re-running
    `test_resume_step_5_refuses_a_floor_whose_phase_rows_moved_under_it` under the same mutation
    stayed GREEN, because it never reaches this branch (`FloorSnapshotStaleError` is raised and
    caught one line earlier). Restored afterward; `git diff` against the backup was empty.
    """
    db_path = tmp_path / "state" / "fleet.db"
    _fresh_db(db_path)
    _seed_run_and_repos(db_path, ("acme-commons",))
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        # The race: by the time this transaction runs, BUILD has already gone to
        # REQUIRES_HUMAN_INTERVENTION -- but `statuses` below (what `_demote_to_floors` would
        # have read a moment earlier, before the race) still says SUCCEEDED.
        conn.execute(
            "INSERT INTO phases (run_id, repo_id, phase, status, updated_at) "
            "VALUES (?, 'acme-commons', ?, 'REQUIRES_HUMAN_INTERVENTION', ?)",
            (RUN_ID, int(Phase.BUILD), NOW_ISO),
        )
    finally:
        conn.close()

    statuses: dict[Phase, RepoStatus] = {Phase.BUILD: RepoStatus.SUCCEEDED}
    plan = (Phase.BUILD,)
    plans = [("acme-commons", Phase.BUILD, plan, statuses, "test reason")]
    report: dict[str, object] = {
        "demoted": [
            {
                "repo_id": "acme-commons",
                "floor": "BUILD",
                "phases": ["BUILD"],
                "evidence": {},
            }
        ],
        "unresolved": [],
        "applied": False,
    }

    asyncio.run(_apply_floor_demotions(db_path, RUN_ID, plans, report, now=datetime.now(UTC)))

    assert report["demoted"] == [], "a demotion that never applied was kept as if it happened"
    assert report["applied"] is False
    unresolved = report["unresolved"]
    assert isinstance(unresolved, list) and len(unresolved) == 1
    assert unresolved[0]["repo_id"] == "acme-commons"
    assert "nothing is claimed for this repo" in str(unresolved[0]["reason"])

    # Discriminating: the live REQUIRES_HUMAN_INTERVENTION row must be untouched -- no PENDING
    # write landed underneath the operator's own terminal status.
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT status FROM phases WHERE run_id = ? AND repo_id = 'acme-commons' "
            "  AND phase = ?",
            (RUN_ID, int(Phase.BUILD)),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None and row[0] == "REQUIRES_HUMAN_INTERVENTION"


# ==========================================================================================
# 7. `_floor_reason` -- the "none consulted" fallback when a hard stop ends the walk before any
#    evidence is gathered
# ==========================================================================================


def test_floor_reason_names_none_consulted_when_no_evidence_was_gathered() -> None:
    """`_floor_reason`'s own f-string: `f"Durable evidence read below the frontier: {read or
    'none consulted'}."`. `read` is empty exactly when `resume_floor` gathered no evidence at
    all -- reachable in production whenever the phase immediately below the settled frontier is
    a `DEGRADED`/`SKIPPED` hard stop, which `phase_floor` tests BEFORE consulting `evidence_holds`
    for that phase (`orchestrator/reentry.phase_floor`'s own docstring: "tested *before*
    evidence"). No existing test asserts on `_floor_reason`'s rendered STRING at all (the step-5
    suite in `tests/test_cli.py` asserts on `report['demoted'][...]['evidence']`, the structured
    dict, never the prose `reason` a `PhaseDemoted` finding actually carries) -- so the `or
    'none consulted'` fallback has never been exercised by anything.

    Rule 12 manual mutation proof: changing `{read or 'none consulted'}` to bare `{read}` in a
    throwaway copy of `cli.py` and re-running this test alone turns it RED (produces "... the
    frontier: ." instead of "... the frontier: none consulted."), while grepping
    `tests/test_cli.py` for every assertion reaching a demoted repo's evidence shows none of them
    read the `reason` string, so all would stay GREEN under the same mutation. Restored
    afterward; `git diff` against the backup was empty.
    """
    reason = _floor_reason(Phase.BUILD, {})

    assert reason == (
        "§11.5 step 5: this repo's re-entry floor recomputed to BUILD, so every SUCCEEDED "
        "phase at or above it is re-entry territory and returns to PENDING with its attempts "
        "retained. Durable evidence read below the frontier: none consulted."
    )
