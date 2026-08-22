"""§11.5 step 6 — `fleet resume` recomputes `blocked_by` and appends the wave it frees.

**What this file is the acceptance test for.** `docs/SPEC.md` §12 item 46(ii) requires *a test
that drives every automatic sweep* — the reaper, `fleet resume`, `stub_reconcile`, `blocked_by`
recomputation — and shows none of them able to move a repo out of
`REQUIRES_HUMAN_INTERVENTION`, **nor to clear a `blocked_by` entry** naming a quarantined repo, a
repo `SKIPPED` for any other reason, or a string that resolves to no repo at all. Its fourth
sweep did not exist until `_unblock_dependents` landed: ADR-0090 §2.3 measured the class *"code in
`src/` that REMOVES an entry from `blocked_by`"* at **zero** members, so the clause was
unassertable rather than unasserted. This file drives it.

**The three additions are not restatements, and the fixture is built around why.** The quantity
46(ii)'s original clause watches is *whether a sweep moves a repo out of RHI*. A step-6 recompute
that empties a **quarantined** blocker's dependents never touches the quarantined repo (it is
`SKIPPED`) and never moves any repo out of RHI (the dependents it frees were `BLOCKED`), so that
quantity reads **identically** before and after the defect. Every assertion below therefore names
the quantity it watches, and `test_the_original_rhi_clause_alone_cannot_see_the_quarantine_undo`
*measures* the blindness rather than asserting it.
"""

from __future__ import annotations

import ast
import inspect
import json
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

import pytest
from typer.testing import CliRunner

from fleet.cli import ExitCode, app
from tests.test_cli import RUN_ID, base_args, fresh_db, seed_run, write_config

runner = CliRunner()

STAMP = "2026-08-08T12:00:00+00:00"

QUARANTINED = "acme-gated"
"""`fleet quarantine`'s shape: every non-terminal phase `SKIPPED`, plus an audited finding."""

REOPENED = "acme-fixed"
"""The RHI blocker of ADR-0090 §2's reversal clause, re-run and now past its old frontier."""

QUARANTINED_MID = "acme-halfgated"
"""Quarantined BETWEEN phases: `{SCAN: SKIPPED, TRANSFORM: SUCCEEDED}`.

Reachable, not contrived. `cli._quarantine_impl` moves only rows whose status is not in
`TERMINAL_STATUSES`, so a repo quarantined while it held one phase row keeps that row `SKIPPED`
while a later admission can complete a phase ABOVE it. The shape exists to pin the defect
`cli._blocker_states` carried until this commit — a highest-phase-wins fold projects `SUCCEEDED`
here and re-admits an audited quarantine's dependents on every resume.
"""

CONTAINED = "acme-broken"
"""Still `REQUIRES_HUMAN_INTERVENTION`. The population 46(ii)'s ORIGINAL clause watches."""

UNRESOLVABLE = "acme-retired"
"""A name in nobody's `repos` row. THE case that discriminates — see the module docstring.

**Repo-SHAPED on purpose, and the reason is a boundary the tree already records.** §12 item
46(ii)'s widened text names *"a `contract_id`"* as the canonical unresolvable entry, and §3.5 does
mandate one in the SQL column on `contracts.status='FAILED'`. It cannot be driven through the
whole sweep: `models.state.RepoState.blocked_by` is `list[RepoId]`, whose pattern
`^[a-z0-9][a-z0-9._-]{0,99}$` rejects a colon, so §11.5 **step 7** — `project_once`, which runs
in the same command — raises `ValidationError` before the resume returns. That field's own
description states the boundary ("this `list[RepoId]` annotation would reject it, so the column
and this field are not interchangeable"). `test_a_contract_id_is_retained_by_step_6_but_stops_at_
the_projection` drives the `contract_id` as far as it can go and pins where it stops.
"""

CONTRACT_ID = "contract:acme.protos:1.4"
"""§3.5's failed-contract trigger's mandated form. Zero producers; step 7 cannot carry it."""

FREED = "dep-freed"
RETAINED_Q = "dep-quarantined"
RETAINED_C = "dep-contract"
MIXED = "dep-mixed"
RETAINED_RHI = "dep-contained"
RETAINED_MID = "dep-halfgated"

REPOS = (
    QUARANTINED, QUARANTINED_MID, REOPENED, CONTAINED,
    FREED, RETAINED_Q, RETAINED_C, MIXED, RETAINED_RHI, RETAINED_MID,
)


# --------------------------------------------------------------------------------------
# the fixture
# --------------------------------------------------------------------------------------


def _phase(conn: sqlite3.Connection, repo: str, phase: int, status: str,
           blocked_by: str = "[]") -> None:
    conn.execute(
        "INSERT INTO phases (run_id, repo_id, phase, status, attempts, blocked_by, updated_at) "
        "VALUES (?, ?, ?, ?, 2, ?, ?)",
        (RUN_ID, repo, phase, status, blocked_by, STAMP),
    )


def _blocked(conn: sqlite3.Connection, repo: str, names: Sequence[str]) -> None:
    """A dependent, in exactly the shape `SqliteSchedulerStore.append_blocked_by` leaves.

    The blocker is written into EVERY non-`SUCCEEDED` phase and each of those rows is marked
    `BLOCKED` — which is why `plan_unblocking` takes one entry per `phases` row and unions them
    itself. A fixture that wrote one row could not express the union at all.
    """
    payload = json.dumps(sorted(names))
    _phase(conn, repo, 1, "PENDING")
    for phase in (2, 3, 4):
        _phase(conn, repo, phase, "BLOCKED", payload)


@pytest.fixture
def fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Four blocker shapes, five dependents, three waves — two of them `CLOSED`.

    **Why each blocker shape is here, and what it can and cannot express**, is the audit in this
    module's docstring made concrete; `test_every_fixture_case_can_express_its_own_defect`
    re-derives the table from this fixture rather than restating it.
    """
    write_config(tmp_path)
    from fleet.settings import FleetSettings

    settings = FleetSettings.load(tmp_path / "config")
    db = tmp_path / "state" / "fleet.db"
    fresh_db(db)
    seed_run(
        db,
        repos=REPOS,
        config_digests=json.dumps(dict(settings.section_digests), sort_keys=True),
    )
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        # --- the blockers -----------------------------------------------------------
        # Quarantined: `cli._quarantine_impl` sets EVERY non-terminal row `SKIPPED` and writes
        # the audited finding in the same transaction.
        for phase in (1, 2, 3, 4):
            _phase(conn, QUARANTINED, phase, "SKIPPED")
        conn.execute(
            "INSERT INTO findings (run_id, repo_id, kind, severity, fingerprint, payload, "
            "                      created_at) "
            "VALUES (?, ?, 'OperatorQuarantine', 'warn', 'fp-quarantine', ?, ?)",
            (RUN_ID, QUARANTINED, json.dumps({"repo_id": QUARANTINED, "operator": True}), STAMP),
        )
        # Quarantined between phases — see `QUARANTINED_MID`.
        # Every row settled, so §11.5 step 5 leaves this repo alone and the ONLY thing that can
        # move its dependent's entry is step 6 — see `test_step_5_writes_nothing_against_this_
        # fixture`. `SUCCEEDED` at phases 2-4 above a `SKIPPED` phase 1 is what makes the case
        # DISCRIMINATE: the retired highest-phase fold reads `SUCCEEDED` here and removes.
        _phase(conn, QUARANTINED_MID, 1, "SKIPPED")
        for phase in (2, 3, 4):
            _phase(conn, QUARANTINED_MID, phase, "SUCCEEDED")
        conn.execute(
            "INSERT INTO findings (run_id, repo_id, kind, severity, fingerprint, payload, "
            "                      created_at) "
            "VALUES (?, ?, 'OperatorQuarantine', 'warn', 'fp-quarantine-mid', ?, ?)",
            (RUN_ID, QUARANTINED_MID,
             json.dumps({"repo_id": QUARANTINED_MID, "operator": True}), STAMP),
        )
        # Re-run past its old frontier: `runner._contain` blocked from a TRANSFORM row that read
        # RHI; the operator fixed it, `fleet retry` re-opened it and it has since SUCCEEDED, so
        # the HIGHEST row this repo carries is BUILD/SUCCEEDED. The two anchors — the phase the
        # block was written from and the phase the reversal is visible at — are DIFFERENT rows,
        # which is what makes the reversal expressible at all.
        for phase in (1, 2, 3, 4):
            _phase(conn, REOPENED, phase, "SUCCEEDED")
        # Still contained. Nothing in this run may move it.
        _phase(conn, CONTAINED, 1, "SUCCEEDED")
        _phase(conn, CONTAINED, 2, "REQUIRES_HUMAN_INTERVENTION")

        # --- the dependents ---------------------------------------------------------
        _blocked(conn, FREED, [REOPENED])
        _blocked(conn, RETAINED_Q, [QUARANTINED])
        _blocked(conn, RETAINED_C, [UNRESOLVABLE])
        _blocked(conn, RETAINED_RHI, [CONTAINED])
        _blocked(conn, RETAINED_MID, [QUARANTINED_MID])
        _blocked(conn, MIXED, [QUARANTINED, REOPENED, UNRESOLVABLE])

        # --- the waves --------------------------------------------------------------
        # Three waves with pairwise-distinct ceilings, so a write that copied one row onto
        # another moves an observable value. Waves 0 and 1 hold only settled members and are
        # therefore CLOSED; the brief requires at least one, and there are two.
        for index, ceiling, started in ((0, 24.0, STAMP), (1, 16.0, STAMP), (2, 8.0, None)):
            conn.execute(
                "INSERT INTO waves (run_id, wave_index, computed_at, wave_started_at, synthetic, "
                "                   max_usd) VALUES (?, ?, ?, ?, 0, ?)",
                (RUN_ID, index, STAMP, started, ceiling),
            )
        members = {
            0: (QUARANTINED, QUARANTINED_MID, REOPENED),
            1: (CONTAINED,),
            2: (FREED, RETAINED_Q, RETAINED_C, MIXED, RETAINED_RHI, RETAINED_MID),
        }
        for index, ids in members.items():
            for node_id in ids:
                conn.execute(
                    "INSERT INTO wave_members (run_id, wave_index, node_kind, node_id) "
                    "VALUES (?, ?, 'REPO', ?)",
                    (RUN_ID, index, node_id),
                )
    finally:
        conn.close()
    monkeypatch.chdir(tmp_path)
    yield tmp_path


# --------------------------------------------------------------------------------------
# readers — every assertion below names the quantity it watches
# --------------------------------------------------------------------------------------


def _rows(db: Path) -> dict[tuple[str, int], tuple[str, list[str]]]:
    """(status, blocked_by) per `phases` row. THE quantity step 6 is allowed to move."""
    conn = sqlite3.connect(db)
    try:
        return {
            (str(repo), int(phase)): (str(status), sorted(json.loads(str(names) or "[]")))
            for repo, phase, status, names in conn.execute(
                "SELECT repo_id, phase, status, blocked_by FROM phases WHERE run_id = ?",
                (RUN_ID,),
            )
        }
    finally:
        conn.close()


def _blocked_by(db: Path, repo: str) -> list[str]:
    """The union across the repo's rows, so a partial edit cannot hide behind one row."""
    return sorted({n for (r, _p), (_s, names) in _rows(db).items() if r == repo for n in names})


def _statuses(db: Path, repo: str) -> set[str]:
    return {status for (r, _p), (status, _n) in _rows(db).items() if r == repo}


def _waves(db: Path) -> list[tuple[object, ...]]:
    conn = sqlite3.connect(db)
    try:
        return [tuple(row) for row in conn.execute(
            "SELECT wave_index, computed_at, wave_started_at, synthetic, max_usd FROM waves "
            " WHERE run_id = ? ORDER BY wave_index", (RUN_ID,)
        )]
    finally:
        conn.close()


def _membership(db: Path) -> dict[str, int]:
    conn = sqlite3.connect(db)
    try:
        return {
            str(node_id): int(index)
            for node_id, index in conn.execute(
                "SELECT node_id, wave_index FROM wave_members "
                " WHERE run_id = ? AND node_kind = 'REPO'", (RUN_ID,)
            )
        }
    finally:
        conn.close()


def _resume(fleet: Path, *extra: str) -> tuple[int, Mapping[str, object]]:
    result = runner.invoke(app, [*base_args(fleet), "--json", "resume", *extra])
    if not result.stdout.strip():  # pragma: no cover - only on an unexpected early exit
        raise AssertionError(f"resume produced no JSON (exit {result.exit_code}): {result.output}")
    return result.exit_code, json.loads(result.stdout)


# --------------------------------------------------------------------------------------
# §12 item 46(ii) — the sweep-driving test the criterion names
# --------------------------------------------------------------------------------------


def test_the_blocked_by_recompute_clears_only_what_it_can_show_is_no_longer_blocking(
    fleet: Path,
) -> None:
    """§12 item 46(ii), all four clauses, driven through the real `fleet resume` sweep.

    **Quantity watched:** the `(status, blocked_by)` pair of every `phases` row in the run, read
    from SQLite after the command. Not a count of removals: a recompute that cleared the right
    number of entries from the wrong repos moves no count.

    Each assertion states whether the defect it hunts could leave that quantity unchanged:

    * `FREED` — the reversal. A recompute that never removes anything leaves this pair unchanged,
      so this assertion is the one that fails on an inert step 6, and the ONLY one that does.
    * `RETAINED_Q` — the quarantine clause. A fail-open recompute moves this pair; 46(ii)'s
      original RHI clause does not, which is the point of the addition.
    * `RETAINED_C` — the unresolvable clause. The name is in no `repos` row, so it can only be
      retained by the fail-closed polarity, never re-derived.
    * `RETAINED_RHI` — the original clause, still asserted: a blocker that is still RHI holds.
    * `MIXED` — the union. Its three names split 1 removed / 2 retained across the SAME row, so
      "removes the planned name" and "clears the row" are distinguishable; a fixture whose repos
      each carry one name cannot tell them apart.
    """
    db = fleet / "state" / "fleet.db"
    before = _rows(db)
    code, payload = _resume(fleet)
    assert code == ExitCode.USAGE, payload  # step 8 is still absent; steps 1-7 committed

    assert _blocked_by(db, FREED) == [], "the reversal did not happen: nothing was removed"
    assert _statuses(db, FREED) == {"PENDING"}, (
        "an emptied `blocked_by` must return the row to PENDING — `BLOCKED -> PENDING`, "
        "`ALLOWED_TRANSITIONS`' 'an unblocked dependency'"
    )
    assert _blocked_by(db, RETAINED_Q) == [QUARANTINED], (
        "an audited OperatorQuarantine was silently undone — the defect 46(ii)'s RHI clause "
        "cannot see, which is why the criterion was widened"
    )
    assert _statuses(db, RETAINED_Q) == {"PENDING", "BLOCKED"}
    assert _blocked_by(db, RETAINED_C) == [UNRESOLVABLE], (
        "a `contract_id` was erased: R2-OPEN's polarity, which ADR-0090 §2.4 rules against "
        "because no live state can ever re-derive it"
    )
    assert _blocked_by(db, RETAINED_RHI) == [CONTAINED]
    assert _blocked_by(db, RETAINED_MID) == [QUARANTINED_MID], (
        "a repo `fleet quarantine` abandoned BETWEEN phases was read as landed and its "
        "dependent re-admitted. This is the defect the highest-phase fold in `cli._blocker_"
        "states` carried: rows {SCAN: SKIPPED, TRANSFORM+: SUCCEEDED} project SUCCEEDED under it"
    )
    assert _statuses(db, RETAINED_MID) == {"PENDING", "BLOCKED"}
    assert _blocked_by(db, MIXED) == [QUARANTINED, UNRESOLVABLE], (
        "the union lost the wrong names: only the planned removal may leave the row"
    )
    assert _statuses(db, MIXED) == {"PENDING", "BLOCKED"}

    # The original clause, unweakened: no sweep moves a repo OUT of RHI, and the quarantined
    # blocker keeps both its status and its audit row.
    assert _statuses(db, CONTAINED) == {"SUCCEEDED", "REQUIRES_HUMAN_INTERVENTION"}
    assert _statuses(db, QUARANTINED) == {"SKIPPED"}
    assert _statuses(db, QUARANTINED_MID) == {"SKIPPED", "SUCCEEDED"}
    conn = sqlite3.connect(db)
    try:
        audited = sorted(
            str(row[0])
            for row in conn.execute(
                "SELECT repo_id FROM findings WHERE run_id = ? AND kind = 'OperatorQuarantine'",
                (RUN_ID,),
            )
        )
    finally:
        conn.close()
    assert audited == sorted((QUARANTINED, QUARANTINED_MID)), (
        "the audit rows the quarantines rest on are not intact after the sweep"
    )

    # **A class result, not a spot check.** Step 5 writes nothing against this fixture (every
    # repo is `unchanged` — the dependents have no `SUCCEEDED` phase at or above their floor, the
    # blockers have settled or RHI rows), so EVERY row that differs is step 6's, and the whole
    # `phases` table is the scope. `test_step_5_writes_nothing_against_this_fixture` pins that
    # premise rather than leaving it as an argument.
    moved = {key for key, value in _rows(db).items() if before[key] != value}
    assert {repo for repo, _phase in moved} == {FREED, MIXED}, (
        f"step 6 wrote rows it did not plan: {sorted(moved)}"
    )
    assert moved == {(FREED, 2), (FREED, 3), (FREED, 4), (MIXED, 2), (MIXED, 3), (MIXED, 4)}, (
        "the write reached a phase row that carried no `blocked_by` entry"
    )


def test_the_original_rhi_clause_alone_cannot_see_the_quarantine_undo(fleet: Path) -> None:
    """The blindness ADR-0090 §6 names, MEASURED against this fixture rather than asserted.

    **Quantity watched:** the set of repos whose status is `REQUIRES_HUMAN_INTERVENTION`, before
    and after the sweep — exactly what 46(ii)'s original clause constrains.

    The quarantine-undo defect empties `RETAINED_Q`'s list. This test asserts that doing so would
    leave the RHI set **unchanged**, which is why a criterion watching only that set certifies the
    defect green. It is the same shape as W16's measurement that a fixture of its cases 1+2
    passes under a fail-open predicate: the instrument has to be shown blind, not assumed sharp.
    """
    db = fleet / "state" / "fleet.db"
    rhi = lambda: {  # noqa: E731
        repo for (repo, _p), (status, _n) in _rows(db).items()
        if status == "REQUIRES_HUMAN_INTERVENTION"
    }
    before = rhi()
    conn = sqlite3.connect(db, isolation_level=None)
    try:  # the defect, applied by hand: the quarantined blocker's dependents are re-admitted
        conn.execute(
            "UPDATE phases SET blocked_by = '[]', status = 'PENDING' "
            " WHERE run_id = ? AND repo_id = ? AND status = 'BLOCKED'",
            (RUN_ID, RETAINED_Q),
        )
    finally:
        conn.close()
    assert rhi() == before == {CONTAINED}, (
        "the quantity 46(ii)'s original clause watches MOVED under the quarantine-undo defect — "
        "if that ever becomes true, the widening in §12 item 46(ii) is no longer load-bearing "
        "and this test, not the criterion, is what needs re-deciding"
    )
    assert _blocked_by(db, RETAINED_Q) == [], "the defect this test injects did not apply"


# --------------------------------------------------------------------------------------
# the ordering — step 6 between step 5 and step 7
# --------------------------------------------------------------------------------------


def test_step_6_runs_between_step_5_and_the_projection_it_must_precede(fleet: Path) -> None:
    """The published `migration_state.json` carries step 6's writes, not the state before them.

    **Quantity watched:** the `blocked_by`, `status` and `wave_index` the PROJECTION reports for
    the freed repo — read out of the file step 7 writes, never out of SQLite.

    This is the discriminating assertion for the ordering: moving `_unblock_dependents` below
    `project_once` leaves every SQLite assertion in this file green (the writes still happen) and
    only this one red, because §11.5 step 7 regenerates the file from rows that have not been
    recomputed yet. Asserting the source order instead would be asserting the arrangement;
    `test_the_source_order_matches_the_behaviour_above` does that too, as a cheaper tripwire that
    names the symbols, and it is deliberately NOT the primary assertion.
    """
    code, _payload = _resume(fleet)
    assert code == ExitCode.USAGE
    state = json.loads((fleet / "migration_state.json").read_text(encoding="utf-8"))
    freed = state["repos"][FREED]
    assert freed["blocked_by"] == [], (
        "`migration_state.json` still shows the entry step 6 cleared — the projection was "
        "regenerated from pre-un-blocking rows, which is the drift class §11.5 says a resume "
        "REMOVES"
    )
    assert freed["status"] == "PENDING"
    assert freed["wave_index"] == 3, "the freed repo is still published in its old wave"
    assert state["repos"][RETAINED_Q]["blocked_by"] == [QUARANTINED]


def test_the_source_order_matches_the_behaviour_above(fleet: Path) -> None:
    """A cheap tripwire on `_resume_impl`'s statement order, by symbol, not by line.

    **Quantity watched:** the index, within `_resume_impl`'s body, of the statement that calls
    each of the three functions. It cannot catch a reordering that preserves source order (there
    is none — these are three sequential `await`s), and it is not what proves the ordering
    matters; the projection test above is. It exists because a failure here names the symbol that
    moved, while the projection test names a JSON key.
    """
    from fleet import cli as fleet_cli

    body = ast.parse(inspect.getsource(fleet_cli._resume_impl)).body[0]
    assert isinstance(body, ast.AsyncFunctionDef)
    seen: dict[str, int] = {}
    for index, statement in enumerate(body.body):
        for node in ast.walk(statement):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                seen.setdefault(node.func.id, index)
    for name in ("_demote_to_floors", "_unblock_dependents", "project_once"):
        assert name in seen, f"{name} is not called from `_resume_impl` at all"
    assert seen["_demote_to_floors"] < seen["_unblock_dependents"] < seen["project_once"], (
        f"§11.5's steps 5 -> 6 -> 7 are out of order in `_resume_impl`: {seen}"
    )


# --------------------------------------------------------------------------------------
# one plan, two routes
# --------------------------------------------------------------------------------------


def test_dry_run_and_the_write_report_the_same_plan(fleet: Path) -> None:
    """The D74 seam this design closes by construction (`Unblocking` carries BOTH halves).

    **Quantity watched:** the `unblocked`/`retained`/`candidates` sub-report, verbatim, from the
    two routes. The write-only keys (`applied`, `wave_index`) are compared separately rather than
    excluded silently — an excluded key is how a preview and a write drift unnoticed.
    """
    _code, preview = _resume(fleet, "--dry-run")
    _code2, written = _resume(fleet)
    plan = lambda report: {  # noqa: E731
        key: report[key] for key in ("candidates", "unblocked", "retained", "unresolved")
    }
    previewed = plan(preview["unblocked_dependents"])
    applied = plan(written["unblocked_dependents"])
    assert previewed == applied, (
        "the preview and the write reported different plans — they must be the same "
        "`plan_unblocking` call over the same inputs, not two routes kept in agreement"
    )
    assert preview["unblocked_dependents"]["applied"] is False
    assert preview["unblocked_dependents"]["wave_index"] is None
    assert written["unblocked_dependents"]["applied"] is True
    assert written["unblocked_dependents"]["wave_index"] == 3
    assert _waves(fleet / "state" / "fleet.db")[-1][0] == 3


def test_the_dry_run_writes_nothing_at_all(fleet: Path) -> None:
    """**Quantity watched:** every `phases`, `waves` and `wave_members` row, before and after."""
    db = fleet / "state" / "fleet.db"
    before = (_rows(db), _waves(db), _membership(db))
    code, _payload = _resume(fleet, "--dry-run")
    assert code == ExitCode.SUCCESS
    assert (_rows(db), _waves(db), _membership(db)) == before


# --------------------------------------------------------------------------------------
# the wave
# --------------------------------------------------------------------------------------


def test_the_freed_repo_moves_upward_only_and_no_existing_wave_row_is_touched(
    fleet: Path,
) -> None:
    """**Quantities watched:** (a) every `waves` row as a value tuple, (b) every repo's
    `wave_index`.

    (a) is the absence claim, read off an enumeration rather than asserted: the three seeded rows
    carry pairwise-distinct `max_usd` (24/16/8) and one NULL `wave_started_at`, so a write that
    re-budgeted or restarted an existing wave moves a value. (b) is the movement claim: exactly
    the repos the plan freed change index, and the index they change to is above every prior one.
    """
    db = fleet / "state" / "fleet.db"
    before_waves, before_members = _waves(db), _membership(db)
    code, payload = _resume(fleet)
    assert code == ExitCode.USAGE
    after_waves, after_members = _waves(db), _membership(db)

    assert after_waves[: len(before_waves)] == before_waves, (
        "an existing `waves` row moved — `append_unblocked_wave` may only INSERT"
    )
    assert len(after_waves) == len(before_waves) + 1
    index, _computed, started, synthetic, ceiling = after_waves[-1]
    assert index == max(row[0] for row in before_waves) + 1 == 3, "the wave is not above every"
    assert (started, synthetic) == (None, 1)
    assert ceiling == pytest.approx(8.0), "one member × `budgets.wave_max_cost_usd_per_repo`"

    moved = {repo for repo, wave in after_members.items() if before_members[repo] != wave}
    assert moved == {FREED}, f"the wrong members moved: {sorted(moved)}"
    assert after_members[FREED] == 3
    assert all(after_members[repo] >= before_members[repo] for repo in after_members), (
        "a member moved DOWNWARD; §11.5 step 6 appends above every existing index"
    )
    assert payload["unblocked_dependents"]["wave_error"] is None


def test_a_freed_repo_the_sequencer_never_planned_is_reported_not_admitted(fleet: Path) -> None:
    """`append_unblocked_wave` MOVES members; a repo with no membership row is refused.

    **Quantity watched:** `wave_error` in the payload, plus the `waves` row count. The refusal
    must not be silent (Rule 11) and must not roll back the `blocked_by` write, which is correct
    on its own.
    """
    db = fleet / "state" / "fleet.db"
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        conn.execute(
            "DELETE FROM wave_members WHERE run_id = ? AND node_id = ?", (RUN_ID, FREED)
        )
    finally:
        conn.close()
    before = len(_waves(db))
    code, payload = _resume(fleet)
    assert code == ExitCode.USAGE
    error = payload["unblocked_dependents"]["wave_error"]
    assert isinstance(error, str) and FREED in error, "the wave failure was swallowed"
    assert len(_waves(db)) == before, "a wave row was written by a call that raised"
    assert _blocked_by(db, FREED) == [], (
        "the `blocked_by` write was rolled back to tidy the report; it was correct and committed"
    )


# --------------------------------------------------------------------------------------
# the in-transaction guard, and the loud lookup
# --------------------------------------------------------------------------------------


def test_step_6_refuses_a_repo_whose_blocker_moved_under_the_plan(
    fleet: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard is re-read INSIDE the transaction — `demote_to_floor`'s precedent.

    **Quantity watched:** the refused repo's `(status, blocked_by)` rows, and the `unresolved`
    bucket. A guard evaluated only outside the transaction leaves the rows CHANGED and the bucket
    EMPTY, which is the read-then-write race stated as an observable.

    The interleaving is not adversarial: the reversal is judged through a `mode=ro` handle, and
    `state/db.py`'s writer slot is process-wide module state, so a `fleet retry` or a live worker
    re-contaminating the blocker in that window is ordinary. The fixture writes it from inside
    `plan_unblocking`, which is the last read before the write.
    """
    from fleet import cli as fleet_cli

    db = fleet / "state" / "fleet.db"
    real = fleet_cli.plan_unblocking
    fired: list[int] = []

    def racing(**kwargs: object) -> object:
        if not fired:  # only the preview call; the in-transaction re-read must see the new row
            fired.append(1)
            conn = sqlite3.connect(db, isolation_level=None)
            try:
                conn.execute(
                    # the repo's HIGHEST row, which is the one the reduction reads
                    "UPDATE phases SET status = 'REQUIRES_HUMAN_INTERVENTION' "
                    " WHERE run_id = ? AND repo_id = ? AND phase = 4",
                    (RUN_ID, REOPENED),
                )
            finally:
                conn.close()
        return real(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("fleet.cli.plan_unblocking", racing)
    code, payload = _resume(fleet)
    assert code == ExitCode.USAGE
    report = payload["unblocked_dependents"]
    unresolved = {str(entry["repo_id"]) for entry in report["unresolved"]}
    assert unresolved == {FREED, MIXED}, f"the race was applied silently: {report}"
    assert "moved before this write" in str(report["unresolved"][0]["reason"])
    assert _blocked_by(db, FREED) == [REOPENED], "the stale plan was applied anyway"
    assert _statuses(db, FREED) == {"PENDING", "BLOCKED"}
    assert report["applied"] is False
    assert len(_waves(db)) == 3, "a wave was appended for a repo whose write was refused"


def test_a_blocker_the_repos_table_knows_but_the_status_lookup_misses_is_loud(
    fleet: Path,
) -> None:
    """A resolvable name with no `BlockerState` aborts; it is not folded into the retained set.

    **Quantity watched:** the exit code and the message. This is the one failure the fail-closed
    polarity CANNOT surface on its own: a lookup returning nothing for every name retains
    everything and reports "nothing to remove", which is byte-identical to a healthy fleet whose
    blockers all still block. The `repos` row is what tells the two apart.
    """
    db = fleet / "state" / "fleet.db"
    conn = sqlite3.connect(db, isolation_level=None)
    try:  # the blocker is still in `repos`, but has no `phases` row to reduce
        conn.execute("DELETE FROM phases WHERE run_id = ? AND repo_id = ?", (RUN_ID, REOPENED))
    finally:
        conn.close()
    result = runner.invoke(app, [*base_args(fleet), "resume"])
    assert result.exit_code == ExitCode.UNEXPECTED_ERROR, result.output
    assert REOPENED in result.output
    assert "no BlockerState" in result.output
    assert _blocked_by(db, FREED) == [REOPENED], "a write happened before the refusal"


# --------------------------------------------------------------------------------------
# expressibility — asked of the fixture, not of the assertions
# --------------------------------------------------------------------------------------


def test_every_fixture_case_can_express_its_own_defect(fleet: Path) -> None:
    """The anchors-that-coincide audit, re-derived from the fixture rather than restated.

    Four properties, each of which a hurried fixture loses:

    1. **The reversal's two anchors differ.** `REOPENED`'s highest phase is not the phase the
       block was propagated from. A fixture whose blocker carries one row cannot distinguish
       "re-run past the frontier" from "the frontier is still RHI" at all.
    2. **No two blockers share a status.** `SKIPPED`, `SUCCEEDED`, `RHI` and *absent* are four
       distinct inputs, so a predicate that collapsed any two of them moves a row.
    3. **`MIXED` splits within a single row.** Removal and retention are expressible on the SAME
       `phases` row, so "clears the row" and "removes the planned name" are distinguishable.
    4. **The waves are pairwise distinct and two are CLOSED.** A cross-row `waves` write has
       somewhere to land and something to move; with one seeded wave the absence claim would be
       near-vacuous.
    """
    db = fleet / "state" / "fleet.db"
    rows = _rows(db)

    reopened = {phase: status for (repo, phase), (status, _n) in rows.items() if repo == REOPENED}
    assert set(reopened) == {1, 2, 3, 4} and set(reopened.values()) == {"SUCCEEDED"}, (
        "`REOPENED` must be settled at every phase, or §11.5 step 5 demotes it to PENDING before "
        "step 6 ever reads it and the fixture stops expressing 're-run to SUCCEEDED' at all"
    )
    contained = {phase: status for (repo, phase), (status, _n) in rows.items() if repo == CONTAINED}
    assert contained == {1: "SUCCEEDED", 2: "REQUIRES_HUMAN_INTERVENTION"}, (
        "`CONTAINED`'s rows must DISAGREE, or the highest-phase reduction is not discriminated "
        "from a lowest-phase or an any-phase one"
    )

    # The between-phases case must DISAGREE with itself top-to-bottom, or it cannot discriminate
    # the highest-phase fold from the lowest-non-SUCCEEDED one.
    mid = {phase: status for (repo, phase), (status, _n) in rows.items() if repo == QUARANTINED_MID}
    assert mid[max(mid)] == "SUCCEEDED" and mid[min(mid)] == "SKIPPED", (
        "`QUARANTINED_MID` no longer distinguishes the two folds: under the retired one it must "
        "read SUCCEEDED (and re-admit), under the shipped one SKIPPED (and retain)"
    )
    tops = {
        repo: rows[(repo, max(p for r, p in rows if r == repo))][0]
        for repo in (QUARANTINED, REOPENED, CONTAINED)
    }
    assert sorted(tops.values()) == ["REQUIRES_HUMAN_INTERVENTION", "SKIPPED", "SUCCEEDED"]
    conn = sqlite3.connect(db)
    try:
        known = {str(row[0]) for row in conn.execute("SELECT repo_id FROM repos")}
    finally:
        conn.close()
    assert UNRESOLVABLE not in known and UNRESOLVABLE not in {repo for repo, _p in rows}, (
        "the unresolvable case is only unresolvable while nothing resolves it"
    )

    mixed = {tuple(names) for (repo, _p), (_s, names) in rows.items() if repo == MIXED and names}
    assert mixed == {tuple(sorted((QUARANTINED, REOPENED, UNRESOLVABLE)))}, (
        "MIXED must carry a removable name, a retained-resolvable one and an unresolvable one on "
        "the SAME row, or removal and row-clearing are not distinguishable"
    )

    waves = _waves(db)
    ceilings = [row[4] for row in waves]
    assert len(set(ceilings)) == len(ceilings) == 3, "the waves must be pairwise distinguishable"
    assert sum(1 for row in waves if row[2] is not None) == 2


def test_a_contract_id_is_retained_by_step_6_but_stops_at_the_projection(fleet: Path) -> None:
    """§12 item 46(ii)'s `contract_id` case, driven as far as `fleet resume` can carry it.

    **Quantity watched:** the entry's presence in the step-6 plan's `remaining`, from the
    `--dry-run` route. It is the dry-run route because §11.5 **step 7** runs in the same command
    as step 6 and `models.state.RepoState.blocked_by` is `list[RepoId]`, whose pattern rejects a
    colon — so a real `fleet resume` over this row raises `ValidationError` inside `project_once`
    AFTER step 6's write has committed. That is not a defect this test may hide behind a
    narrower probe: it is a **limit on the criterion**, recorded here because 46(ii) names the
    `contract_id` by name and the sweep it asks for cannot reach it end to end.

    What this DOES prove is the part step 6 owns: the recompute retains it. What it does NOT
    prove, stated rather than implied: that the entry survives a full resume — it cannot, and
    `RepoState.blocked_by`'s own description has said so since `31484d5`.
    """
    db = fleet / "state" / "fleet.db"
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        conn.execute(
            "UPDATE phases SET blocked_by = ? WHERE run_id = ? AND repo_id = ? AND status = ?",
            (json.dumps([CONTRACT_ID]), RUN_ID, RETAINED_C, "BLOCKED"),
        )
    finally:
        conn.close()
    code, payload = _resume(fleet, "--dry-run")
    assert code == ExitCode.SUCCESS, payload
    entry = next(
        item
        for item in payload["unblocked_dependents"]["retained"]
        if item["repo_id"] == RETAINED_C
    )
    assert entry["remaining"] == [CONTRACT_ID] and entry["removed"] == [], (
        "a `contract_id` was planned for removal: it resolves to no repo, so R2-CLOSED retains it"
    )


def test_step_5_writes_nothing_against_this_fixture(fleet: Path) -> None:
    """The premise the class result above rests on, pinned rather than argued.

    **Quantity watched:** step 5's own report — `applied`, and the bucket every repo lands in.
    If a later edit to this fixture gives some repo a `SUCCEEDED` phase at or above its floor,
    step 5 starts writing and
    `test_the_blocked_by_recompute_clears_only_what_it_can_show_is_no_longer_blocking`'s
    "every row that differs is step 6's" stops being true — silently, and in the direction that
    makes it pass. This test turns that into a failure that names the repo.
    """
    _code, payload = _resume(fleet, "--dry-run")
    floors = payload["reentry_floors"]
    assert floors["demoted"] == [] and floors["applied"] is False, (
        f"step 5 now writes against this fixture: {floors['demoted']}"
    )
    assert {str(entry["repo_id"]) for entry in floors["unchanged"]} == set(REPOS)
    assert floors["unresolved"] == []


def test_the_floor_step_6_reports_comes_from_step_5_even_when_step_5_demoted_nothing(
    fleet: Path,
) -> None:
    """`plan_unblocking`'s `floors` input is step 5's mapping, NOT its report.

    **Quantity watched:** the `floor` key of the freed repo's step-6 entry, against the repos
    step 5's report lists under `demoted`.

    The two disagree here by construction and that is the whole point: step 5 computed a floor of
    `SCAN` for every dependent and demoted none of them, so its report carries **no** floor for
    any of them. A wiring that recovered `floors` by parsing `reentry_floors["demoted"]` would
    report `floor: null` — which `Unblocking`'s docstring defines as "step 5 computed no floor",
    a different and false fact. That is why `_demote_to_floors` returns the mapping beside the
    report.
    """
    _code, payload = _resume(fleet, "--dry-run")
    assert payload["reentry_floors"]["demoted"] == []
    entries = {
        str(item["repo_id"]): item["floor"]
        for item in (
            *payload["unblocked_dependents"]["unblocked"],
            *payload["unblocked_dependents"]["retained"],
        )
    }
    assert entries[FREED] == "SCAN", (
        "the freed repo's floor was lost: step 5 computed SCAN for it and demoted nothing, so a "
        "floor read out of step 5's `demoted` list is `None` here"
    )
    assert set(entries.values()) == {"SCAN"}
