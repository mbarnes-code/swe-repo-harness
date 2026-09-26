"""§15.1 item 3, Wave 7.9 batch 51 (group G10a): `cli.py`'s resume-report line formatters and the
`stub_reconcile` read/write plumbing.

Scope (per the dispatch brief): `_resume_lines`, `_stub_reconcile_lines`, `_arbitration_lines`,
`_floor_lines`, `_reap_lines`, `_budget_lines`, `_repoll_lines`, `_refuse_unbuilt_resume_flags`,
`_validate_resume_flags`, `_stub_reconcile_inputs`, `_stub_supersede_inputs`,
`_apply_stub_reconcile`, `_stub_awaiting_revalidation`, `_stub_reconcile_impl`,
`_count_stale_running`.

Method: every function above already has SOME reaching test (`tests/test_cli.py`,
`tests/test_d89_phase2_reconciliation.py`, `tests/test_stub_resolution_task79.py`,
`tests/test_pr_e2e.py`) that drives it through a real `fleet resume`/`fleet pr` invocation and
asserts the resulting DB state or `--json` payload — but a real-CLI invocation's cost makes it
impractical to hit every branch of every one of these 15 functions through that route alone, and a
`grep` census (see each test's docstring below) shows several branches genuinely never asserted
anywhere: not merely "no test file mentions this function" but "no test anywhere asserts this
exact rendered text" or "no fixture ever puts two rows in the shape this loop needs to prove it
does not collapse them". Each test below is a direct, isolated call against the target function —
following the precedent already in this tree at
`tests/test_d89_phase2_reconciliation.py:621` (`_arbitration_lines({"git_arbitration": report},
dry=False)`) for the pure formatters, and using `tests.test_cli`'s own `fresh_db`/`seed_run`
helpers plus a bare `aiosqlite` connection for the `stub_reconcile` subsystem's async functions —
rather than a full `fleet resume` invocation, so each test isolates exactly the branch it targets
instead of also depending on (and being confounded by) the other 14 functions' behaviour.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import aiosqlite
import pytest

from fleet.cli import (
    PR_RECORD_KIND,
    UsageError,
    _apply_stub_reconcile,
    _arbitration_lines,
    _budget_lines,
    _count_stale_running,
    _fingerprint,
    _floor_lines,
    _iso,
    _reap_lines,
    _refuse_unbuilt_resume_flags,
    _repoll_lines,
    _resume_lines,
    _stub_awaiting_revalidation,
    _stub_reconcile_impl,
    _stub_reconcile_inputs,
    _stub_reconcile_lines,
    _stub_supersede_inputs,
    _validate_resume_flags,
)
from fleet.models.enums import RepoStatus, StubFidelity, StubState
from fleet.models.tasks import StubRecord
from fleet.orchestrator.stubs import AbandonReason, StubDecision, StubTransition
from fleet.settings import FleetSettings
from tests.test_cli import RUN_ID, fresh_db, seed_run, write_config

STAMP = "2026-08-08T12:00:00.000000+00:00"


# ======================================================================================
# `_resume_lines` — the top-level assembly. Every sub-report below is neutralised (each
# sub-`_lines` call made to return []) so these two tests isolate exactly `_resume_lines`' OWN
# headline/step-3/projection logic, which no test anywhere asserts by text (a `grep` for
# "is resumable", "stale RUNNING row(s)" and "projection at" across tests/test_cli.py returns
# nothing — every existing resume test reads `--json`'s `stale_running_reset` instead).
# ======================================================================================


def _neutral_result(**overrides: object) -> dict[str, object]:
    """Every key `_resume_lines` and its seven `.extend()`-ed sub-calls read, tuned so every
    sub-call's own branch returns `[]` — isolating `_resume_lines`' own assembly logic."""
    base: dict[str, object] = {
        "run_id": RUN_ID,
        "stale_running_reset": 0,
        "dry_run": False,
        "projection": "workspace/migration_state.json",
        "stub_reconcile": {"abandoned": [], "held_for_merge": []},
        "git_arbitration": {
            "candidates": 0,
            "spared_live": [],
            "provenance_missing": [],
            "landed": [],
            "discarded": [],
            "partially_landed": [],
            "unresolved": [],
            "anchors_recreated": [],
        },
        "reentry_floors": {"demoted": [], "unchanged": [], "unresolved": [], "candidates": 0},
        "unblocked_dependents": {
            "candidates": 0,
            "unblocked": [],
            "retained": [],
            "unresolved": [],
        },
        "reaped_worktrees": None,
        "reaped_containers": None,
        "raise_budget": None,
        "budget_ledger_before": None,
        "repoll_prs": "not-requested",
    }
    base.update(overrides)
    return base


def test_resume_lines_dry_run_headline_and_step3_preview_with_no_projection_line() -> None:
    result = _neutral_result(dry_run=True, stale_running_reset=3)
    del result["projection"]  # the dry branch must never read this key
    lines = _resume_lines(result)
    assert lines[0] == f"dry-run: run {RUN_ID} is resumable"
    assert "  step 3: 3 stale RUNNING row(s) would reset to PENDING (attempts retained)" in lines
    assert not any("projection at" in line for line in lines), (
        "a dry-run must not claim a projection was written"
    )


def test_resume_lines_real_run_headline_and_projection_with_no_step3_preview() -> None:
    result = _neutral_result(dry_run=False, stale_running_reset=0)
    lines = _resume_lines(result)
    assert lines[0] == (
        f"run {RUN_ID}: 0 stale RUNNING row(s) reset to PENDING (attempts retained)"
    )
    assert lines[-1] == "  projection at workspace/migration_state.json"
    assert not any(line.strip().startswith("step 3:") for line in lines), (
        "a real run's headline already reports the reclaim; step 3's preview-only line "
        "must not also appear"
    )


# ======================================================================================
# `_stub_reconcile_lines` — no test anywhere asserts this function's rendered text (a `grep` for
# "stub_reconcile:" across tests/test_cli.py returns nothing; every stub_reconcile e2e test reads
# `payload["stub_reconcile"]["abandoned"]`/`["held_for_merge"]` instead of `result.output`).
# ======================================================================================


def test_stub_reconcile_lines_renders_abandoned_count_entries_and_held_entries() -> None:
    report = {"abandoned": ["acme-a→acme-p@1.0.0"], "held_for_merge": ["acme-b→acme-p@2.0.0"]}
    real = _stub_reconcile_lines({"stub_reconcile": report}, dry=False)
    assert real == [
        "  stub_reconcile: abandoned 1 open stub(s) (UnresolvedStub, exit 7)",
        "  stub_reconcile: abandoned acme-a→acme-p@1.0.0",
        (
            "  stub_reconcile: acme-b→acme-p@2.0.0 held — provider's PR is still open within "
            "pr.merge_wait_timeout_s"
        ),
    ]
    dry = _stub_reconcile_lines({"stub_reconcile": report}, dry=True)
    assert dry[0] == "  stub_reconcile: would abandon 1 open stub(s) (UnresolvedStub, exit 7)"


# ======================================================================================
# `_arbitration_lines` — `tests/test_d89_phase2_reconciliation.py` already proves the
# `partially_landed` rendering directly against a real report. The `discarded` entry's own
# rendered text is never asserted anywhere (every discard test in tests/test_cli.py reads
# `--json`'s `report["discarded"]`, never `result.output`), and neither is the `dry`/real verb
# distinction on it ("would discard" vs "discarded").
# ======================================================================================


def test_arbitration_lines_discarded_entry_verb_differs_between_dry_and_real() -> None:
    report = {
        "candidates": 1,
        "spared_live": [],
        "landed": [],
        "discarded": [{"repo_id": "acme-x", "phase": 3, "task_id": "t-1"}],
        "partially_landed": [],
        "unresolved": [],
        "provenance_missing": [],
        "anchors_recreated": [],
    }
    real = " ".join(_arbitration_lines({"git_arbitration": report}, dry=False))
    dry = " ".join(_arbitration_lines({"git_arbitration": report}, dry=True))
    assert (
        "step 4: acme-x phase 3 task t-1 did NOT land — discarded the worktree back to its "
        "task anchor, row PENDING, no attempt charged" in real
    )
    assert (
        "step 4: acme-x phase 3 task t-1 did NOT land — would discard the worktree back to "
        "its task anchor, row PENDING, no attempt charged" in dry
    )


# ======================================================================================
# `_floor_lines` — the `demoted` branch's own docstring names the evidence appendage as the
# reason this function exists ("a silent count ... refuses to print"), but no test anywhere
# asserts on "evidence read" (a `grep` across tests/test_cli.py returns nothing).
# ======================================================================================


def test_floor_lines_appends_evidence_read_only_when_evidence_is_non_empty() -> None:
    with_evidence = {
        "demoted": [
            {
                "repo_id": "acme-y",
                "floor": "BUILD",
                "phases": ["BUILD", "VERIFY"],
                "evidence": {"scan_ok": True, "contracts_ok": False},
            }
        ],
        "unchanged": [],
        "unresolved": [],
    }
    lines = _floor_lines({"reentry_floors": with_evidence}, dry=False)
    assert lines == [
        (
            "  step 5: demoted acme-y to floor BUILD — BUILD, VERIFY back to PENDING "
            "(attempts retained); evidence read scan_ok=True, contracts_ok=False"
        )
    ]

    without_evidence = {
        "demoted": [{"repo_id": "acme-y", "floor": "BUILD", "phases": ["BUILD"], "evidence": {}}],
        "unchanged": [],
        "unresolved": [],
    }
    lines2 = _floor_lines({"reentry_floors": without_evidence}, dry=False)
    assert "; evidence read" not in lines2[0], (
        "an empty evidence mapping must not append a dangling '; evidence read' suffix"
    )


# ======================================================================================
# `_reap_lines` — the "no sweep configured" (`skipped`) and "the sweep did not run" (`error`)
# branches are never asserted by text anywhere: a `grep` for "no .* sweep —" and "sweep did not
# run —" across tests/test_cli.py returns nothing (existing tests check the `--json` `"skipped"`
# key on a DIFFERENT report, `repoll_prs`, never on `reaped_worktrees`/`reaped_containers`).
# ======================================================================================


def test_reap_lines_renders_the_skipped_and_error_branches_by_kind() -> None:
    result = {
        "reaped_worktrees": {
            "skipped": "no worktree manager configured",
            "error": None,
            "namespace": "ns1",
            "reaped": [],
            "failed": [],
        },
        "reaped_containers": {
            "skipped": None,
            "error": "docker ps failed: exit 1",
            "namespace": "ns2",
            "reaped": [],
            "failed": [],
        },
    }
    lines = _reap_lines(result, dry=False)
    assert "  step 2: no worktree sweep — no worktree manager configured" in lines
    assert "  step 2: the container sweep did not run — docker ps failed: exit 1" in lines


# ======================================================================================
# `_budget_lines` — no test anywhere asserts on `result.output` for `--raise-budget`
# (`tests/test_cli.py`'s two `--raise-budget` tests both read DB state / findings payload only).
# The dollar formatting, the "raised"/"WOULD raise" verb split and the "and clear the sticky
# halt" suffix are all consequently unproven at the text level.
# ======================================================================================


def test_budget_lines_renders_verb_and_halt_suffix_and_dollar_formatting() -> None:
    real_halted = {
        "raise_budget": 50.0,
        "raise_budget_applied": True,
        "budget_ledger_before": {"max_usd": 10.0, "halted": True},
    }
    assert _budget_lines(real_halted) == [
        "  --raise-budget: raised max_usd $10.00 -> $50.00 and clear the sticky halt"
    ]

    dry_not_halted = {
        "raise_budget": 50.0,
        "raise_budget_applied": False,
        "budget_ledger_before": {"max_usd": 10.0, "halted": False},
    }
    assert _budget_lines(dry_not_halted) == [
        "  --raise-budget: WOULD raise (nothing written) max_usd $10.00 -> $50.00"
    ]


# ======================================================================================
# `_repoll_lines` — the "failed" match arm IS reached by
# `tests/test_cli.py::test_a_forge_failure_under_repoll_prs_still_reconciles_and_then_reports`
# (its docstring: "the forge error ... is in the output"), but that test never asserts the exact
# rendered text — only the exit code, phase row, and that `migration_state.json` exists. A
# mutation garbling this exact string would pass every existing test.
# ======================================================================================


def test_repoll_lines_renders_the_failed_case_with_the_real_error_text() -> None:
    result = {"repoll_prs": "failed", "pr_sync_error": "gh: auth required"}
    assert _repoll_lines(result) == ["  --repoll-prs: FAILED — gh: auth required"]


def test_repoll_lines_skipped_dry_run_case_is_a_fixed_sentence() -> None:
    result = {"repoll_prs": "skipped-dry-run"}
    lines = _repoll_lines(result)
    assert len(lines) == 1
    assert lines[0].startswith("  --repoll-prs: SKIPPED under --dry-run")


# ======================================================================================
# `_refuse_unbuilt_resume_flags` — only `--from-phase` alone is exercised anywhere
# (`tests/test_cli.py:3307`). The `sorted(...)`/`', '.join(...)` combination logic that a
# single-flag test cannot discriminate — `--repo`, `--reset-attempts` and
# `--raise-revalidation-rounds` together — is untested.
# ======================================================================================


def test_refuse_unbuilt_resume_flags_names_every_given_flag_in_sorted_order() -> None:
    with pytest.raises(UsageError) as excinfo:
        _refuse_unbuilt_resume_flags(
            from_phase=None,
            repo="acme-x",
            reset_attempts=True,
            revalidation=None,
            raise_revalidation_rounds=3,
        )
    message = str(excinfo.value)
    assert message.startswith(
        "--raise-revalidation-rounds, --repo, --reset-attempts cannot be honoured"
    ), message
    assert "--from-phase" not in message.split("cannot be honoured")[0], (
        "a flag that was NOT passed must not be named as given"
    )


def test_refuse_unbuilt_resume_flags_is_silent_when_nothing_unbuilt_is_given() -> None:
    _refuse_unbuilt_resume_flags(
        from_phase=None,
        repo=None,
        reset_attempts=False,
        revalidation=None,
        raise_revalidation_rounds=None,
    )  # must not raise


# ======================================================================================
# `_validate_resume_flags` — DISCLOSED, no new test. Its body is exactly `_ = stub_blocked`: a
# true no-op left in place only for the docstring's historical record (round VI task 69 removed
# the refusal ADR-0113 condition 2 required). There is no branch to mutate — every input takes
# the same path (return None) — the same judgment call already applied to `_PrCandidate` in
# batch 48 (nothing to mutate) rather than writing a test that could not discriminate any change.
# ======================================================================================


def test_validate_resume_flags_is_a_documented_no_op_for_either_value() -> None:
    assert _validate_resume_flags(stub_blocked=True) is None
    assert _validate_resume_flags(stub_blocked=False) is None


# ======================================================================================
# `stub_reconcile` subsystem (§3.5.1) — direct async calls against a bare aiosqlite connection
# over a `fresh_db`/`seed_run` database, following `tests.test_cli`'s own helpers rather than a
# full `fleet resume` invocation, to isolate each function's own branch from the other 14.
# ======================================================================================


def _put_stub_row(
    conn: sqlite3.Connection,
    *,
    stub_id: str,
    consumer: str,
    provider: str,
    coord_key: str,
    state: str = "ACTIVE",
    revalidation_task_id: str | None = None,
    resolved_at: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO stubs (stub_id, run_id, repo_id, stub_coord_key, consumer_repo_id, "
        "                   provider_repo_id, pinned_version, bazel_label, state, "
        "                   stub_fidelity, revalidation_round, max_revalidation_rounds, "
        "                   revalidation_task_id, resolved_at, state_changed_at, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PUBLISHED_ARTIFACT', 0, 2, ?, ?, ?, ?)",
        (
            stub_id,
            RUN_ID,
            consumer,
            coord_key,
            consumer,
            provider,
            "1.0.0",
            f"//third_party/stubs/{provider}",
            state,
            revalidation_task_id,
            resolved_at,
            STAMP,
            STAMP,
        ),
    )


def _put_revalidate_task(
    conn: sqlite3.Connection, *, task_id: str, repo_id: str, status: str, revalidation_key: str
) -> None:
    conn.execute(
        "INSERT INTO tasks (task_id, run_id, repo_id, phase, kind, revalidation_key, "
        "                   dest_path, status, created_at) "
        "VALUES (?, ?, ?, 4, 'REVALIDATE', ?, '', ?, ?)",
        (task_id, RUN_ID, repo_id, revalidation_key, status, STAMP),
    )


async def test_stub_reconcile_inputs_reads_the_providers_highest_numbered_phase_status(
    tmp_path: Path,
) -> None:
    """`_stub_reconcile_inputs` builds `ProviderFacts.status` from `phases` `ORDER BY repo_id,
    phase DESC` + `setdefault` (first-seen wins) — i.e. the provider's HIGHEST phase, not its
    lowest or its first-inserted row. No existing test seeds a provider with more than one
    `phases` row, so this specific selection was never proven — a mutation to `ASC` or to
    dropping the `ORDER BY` entirely would silently read the wrong phase and no test would catch
    it.
    """
    db = fresh_db(tmp_path / "state" / "fleet.db")
    seed_run(db, repos=("acme-consumer", "acme-provider"))
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO phases (run_id, repo_id, phase, status, updated_at) "
            "VALUES (?, 'acme-provider', 2, 'SUCCEEDED', ?)",
            (RUN_ID, STAMP),
        )
        conn.execute(
            "INSERT INTO phases (run_id, repo_id, phase, status, updated_at) "
            "VALUES (?, 'acme-provider', 4, 'DEGRADED', ?)",
            (RUN_ID, STAMP),
        )
        _put_stub_row(
            conn,
            stub_id="22222222-2222-4222-8222-222222222222",
            consumer="acme-consumer",
            provider="acme-provider",
            coord_key="acme-provider@1.0.0",
        )
    finally:
        conn.close()

    async with aiosqlite.connect(db) as aconn:
        _records, providers = await _stub_reconcile_inputs(aconn, RUN_ID)

    assert providers["acme-provider"].status == RepoStatus.DEGRADED, (
        "must read phase 4 (the HIGHEST phase), not phase 2 (SUCCEEDED)"
    )


async def test_stub_supersede_inputs_folds_two_consumers_of_one_stub_id_into_one_grouped_record(
    tmp_path: Path,
) -> None:
    """The function's own docstring: "taking only the first would supersede one row and leave
    the other[s] ACTIVE". No existing test ever seeds two consumers of the same `stub_id` sharing
    one provider — every `fleet pr --sync` T1 e2e test in `tests/test_pr_e2e.py` uses exactly one
    consumer per provider — so the `grouped` fold (`existing.model_copy(update={...})`) is never
    exercised.
    """
    db = fresh_db(tmp_path / "state" / "fleet.db")
    seed_run(db, repos=("acme-a", "acme-b", "acme-provider"))
    stub_id = "33333333-3333-4333-8333-333333333333"
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        _put_stub_row(
            conn,
            stub_id=stub_id,
            consumer="acme-a",
            provider="acme-provider",
            coord_key="acme-provider@1.0.0",
        )
        _put_stub_row(
            conn,
            stub_id=stub_id,
            consumer="acme-b",
            provider="acme-provider",
            coord_key="acme-provider@1.0.0",
        )
    finally:
        conn.close()

    async with aiosqlite.connect(db) as aconn:
        records, grouped = await _stub_supersede_inputs(aconn, RUN_ID, "acme-provider")

    assert set(records.keys()) == {
        ("acme-a", "acme-provider@1.0.0"),
        ("acme-b", "acme-provider@1.0.0"),
    }
    assert list(grouped.keys()) == [stub_id]
    assert sorted(grouped[stub_id].consumer_repo_ids) == ["acme-a", "acme-b"], (
        "both consumer rows must fold into ONE grouped StubRecord, not overwrite each other"
    )


async def test_stub_awaiting_revalidation_excludes_only_rows_whose_task_has_not_settled(
    tmp_path: Path,
) -> None:
    """D106: a `SUPERSEDED` row is protected from the ordinary sweep only while its REVALIDATE
    task is still outstanding (`t.status NOT IN ('DONE', 'FAILED')`). Every existing test of this
    function (`tests/test_stub_resolution_task79.py`) proves the PENDING/protected side within
    one call; none of them ever advance the task to DONE and re-check — the row's own state
    changes to RESOLVED via a different path (`settle_revalidation`) before reconcile gets a
    second chance in that fixture. This proves the negative side directly: once the task is
    DONE, `_stub_awaiting_revalidation` must stop naming the row, or a stub that already
    resolved would be protected from the ordinary sweep forever.
    """
    db = fresh_db(tmp_path / "state" / "fleet.db")
    seed_run(db, repos=("acme-consumer", "acme-provider"))
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        _put_revalidate_task(
            conn,
            task_id="t-done",
            repo_id="acme-consumer",
            status="DONE",
            revalidation_key="r1:aaa",
        )
        _put_revalidate_task(
            conn,
            task_id="t-pending",
            repo_id="acme-consumer",
            status="PENDING",
            revalidation_key="r1:bbb",
        )
        _put_stub_row(
            conn,
            stub_id="44444444-4444-4444-8444-444444444444",
            consumer="acme-consumer",
            provider="acme-provider",
            coord_key="acme-provider@settled",
            state="SUPERSEDED",
            revalidation_task_id="t-done",
            resolved_at=STAMP,
        )
        _put_stub_row(
            conn,
            stub_id="55555555-5555-4555-8555-555555555555",
            consumer="acme-consumer",
            provider="acme-provider",
            coord_key="acme-provider@pending",
            state="SUPERSEDED",
            revalidation_task_id="t-pending",
            resolved_at=STAMP,
        )
    finally:
        conn.close()

    async with aiosqlite.connect(db) as aconn:
        excluded = await _stub_awaiting_revalidation(aconn, RUN_ID)

    assert ("acme-consumer", "acme-provider@pending") in excluded, (
        "a row whose REVALIDATE task is still PENDING must stay protected"
    )
    assert ("acme-consumer", "acme-provider@settled") not in excluded, (
        "a row whose REVALIDATE task is already DONE must no longer be protected"
    )


async def test_stub_reconcile_impl_unions_same_call_and_earlier_call_exclusions(
    tmp_path: Path,
) -> None:
    """D105 (`exclude_this_call`) and D106 (`_stub_awaiting_revalidation`) are combined with `|`
    — `exclude = exclude_this_call | awaiting_revalidation`. No existing test drives both
    exclusion sources in the SAME call alongside a row that is NOT excluded at all: each is
    proven separately (D105 in `test_resume_repoll_prs_does_not_undo_t1_in_the_same_call`, D106
    in the task-79 file), so the union itself — and that an ordinary ACTIVE row is still swept
    when neither exclusion names it — is unproven.
    """
    db = fresh_db(tmp_path / "state" / "fleet.db")
    write_config(tmp_path)
    settings = FleetSettings.load(tmp_path / "config")
    seed_run(db, repos=("acme-x", "acme-y", "acme-z", "acme-provider"))
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        # acme-x: ordinary ACTIVE row, provider never opened a PR -> must be swept (T4 abandon).
        _put_stub_row(
            conn,
            stub_id="66666666-6666-4666-8666-666666666666",
            consumer="acme-x",
            provider="acme-provider",
            coord_key="acme-provider@x",
        )
        # acme-y: SUPERSEDED, its REVALIDATE task is still PENDING -> D106 must exclude it.
        _put_revalidate_task(
            conn,
            task_id="t-y",
            repo_id="acme-y",
            status="PENDING",
            revalidation_key="r1:y",
        )
        _put_stub_row(
            conn,
            stub_id="77777777-7777-4777-8777-777777777777",
            consumer="acme-y",
            provider="acme-provider",
            coord_key="acme-provider@y",
            state="SUPERSEDED",
            revalidation_task_id="t-y",
            resolved_at=STAMP,
        )
        # acme-z: SUPERSEDED, passed via exclude_this_call (simulating D105's same-call T1).
        _put_stub_row(
            conn,
            stub_id="88888888-8888-4888-8888-888888888888",
            consumer="acme-z",
            provider="acme-provider",
            coord_key="acme-provider@z",
            state="SUPERSEDED",
            resolved_at=STAMP,
        )
    finally:
        conn.close()

    result = await _stub_reconcile_impl(
        db,
        settings,
        RUN_ID,
        now=datetime.now(UTC),
        dry_run=True,
        exclude_this_call=frozenset({("acme-z", "acme-provider@z")}),
    )

    assert result["abandoned"] == ["acme-x→acme-provider@x"], (
        "the un-excluded ACTIVE row must still be swept"
    )
    assert result["excluded_awaiting_revalidation"] == ["acme-y→acme-provider@y"]
    assert result["excluded_superseded_this_call"] == ["acme-z→acme-provider@z"]


async def test_count_stale_running_counts_every_stale_running_row_and_excludes_other_statuses(
    tmp_path: Path,
) -> None:
    """`tests/test_cli.py::test_resume_dry_run_previews_the_sweep_and_writes_nothing` proves this
    for exactly ONE stale `RUNNING` row. That cannot discriminate `COUNT(*)` from a mutation that
    only ever returns 0 or 1 (e.g. `EXISTS`-shaped logic), and no existing test seeds a
    stale-heartbeat row that is NOT `RUNNING` to prove the `status = 'RUNNING'` filter in the
    query — as opposed to the heartbeat predicate alone — is what excludes it.
    """
    db = fresh_db(tmp_path / "state" / "fleet.db")
    seed_run(db, repos=("acme-a", "acme-b", "acme-c"))
    now = datetime.now(UTC)
    stale_heartbeat = _iso(now - timedelta(seconds=1000))
    horizons = (_iso(now - timedelta(seconds=300)), _iso(now))
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        for repo, status in (
            ("acme-a", "RUNNING"),
            ("acme-b", "RUNNING"),
            ("acme-c", "PENDING"),
        ):
            conn.execute(
                "INSERT INTO phases (run_id, repo_id, phase, status, heartbeat_at, updated_at) "
                "VALUES (?, ?, 1, ?, ?, ?)",
                (RUN_ID, repo, status, stale_heartbeat, STAMP),
            )
    finally:
        conn.close()

    async with aiosqlite.connect(db) as aconn:
        count = await _count_stale_running(aconn, RUN_ID, horizons)

    assert count == 2, (
        "must count both stale RUNNING rows and exclude the stale-heartbeat PENDING row"
    )


async def test_apply_stub_reconcile_skips_a_consumer_with_no_pr_record_without_crashing(
    tmp_path: Path,
) -> None:
    """`_apply_stub_reconcile` marks each abandoned decision's consumer PR record `HELD` via
    `pr_records.get((consumer_repo_id, None))`, `continue`-ing when no draft exists ("a consumer
    absent from the run's PR records ... has nothing to mark and is silently skipped"). Every
    existing abandon test (`test_resume_reconciles_an_open_stub_unconditionally_even_without_
    repoll`) seeds exactly one consumer and it always HAS a PR record — the `draft is None`
    guard, which stands between this and an unconditional `AttributeError` on `None.model_copy`,
    is never exercised.
    """
    db = fresh_db(tmp_path / "state" / "fleet.db")
    seed_run(db, repos=("acme-with-pr", "acme-without-pr", "acme-provider"))
    now = datetime.now(UTC)
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        _put_stub_row(
            conn,
            stub_id="99999999-9999-4999-8999-999999999999",
            consumer="acme-with-pr",
            provider="acme-provider",
            coord_key="acme-provider@1.0.0",
        )
        _put_stub_row(
            conn,
            stub_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            consumer="acme-without-pr",
            provider="acme-provider",
            coord_key="acme-provider@2.0.0",
        )
        pr_payload = json.dumps(
            {
                "run_id": RUN_ID,
                "repo_id": "acme-with-pr",
                "wave_index": 0,
                "branch": "migrate/acme-with-pr",
                "base": "integration",
                "title": "t",
                "body": "b",
                "source_url": "https://example.invalid/acme-with-pr",
                "source_sha": "a" * 40,
                "state": "DRAFTED",
                "url": "https://example.invalid/pr/1",
                "created_at": STAMP,
            }
        )
        conn.execute(
            "INSERT INTO findings (run_id, repo_id, kind, severity, fingerprint, payload, "
            "                      created_at) VALUES (?, ?, ?, 'info', ?, ?, ?)",
            (
                RUN_ID,
                "acme-with-pr",
                PR_RECORD_KIND,
                _fingerprint(RUN_ID, "acme-with-pr", PR_RECORD_KIND, ""),
                pr_payload,
                STAMP,
            ),
        )
    finally:
        conn.close()

    records: dict[tuple[str, str], StubRecord] = {
        ("acme-with-pr", "acme-provider@1.0.0"): StubRecord(
            stub_id=UUID("99999999-9999-4999-8999-999999999999"),
            run_id=UUID(RUN_ID),
            coord_key="acme-provider@1.0.0",
            provider_repo_id="acme-provider",
            consumer_repo_ids=["acme-with-pr"],
            fidelity=StubFidelity.PUBLISHED_ARTIFACT,
            pinned_version="1.0.0",
            state=StubState.ACTIVE,
        ),
        ("acme-without-pr", "acme-provider@2.0.0"): StubRecord(
            stub_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
            run_id=UUID(RUN_ID),
            coord_key="acme-provider@2.0.0",
            provider_repo_id="acme-provider",
            consumer_repo_ids=["acme-without-pr"],
            fidelity=StubFidelity.PUBLISHED_ARTIFACT,
            pinned_version="2.0.0",
            state=StubState.ACTIVE,
        ),
    }
    decisions = [
        StubDecision(
            coord_key="acme-provider@1.0.0",
            consumer_repo_id="acme-with-pr",
            provider_repo_id="acme-provider",
            transition=StubTransition.T4,
            from_state=StubState.ACTIVE,
            to_state=StubState.ABANDONED,
            consumer_status=RepoStatus.DEGRADED,
            detail="test abandon",
            abandon_reason=AbandonReason.END_OF_RUN,
        ),
        StubDecision(
            coord_key="acme-provider@2.0.0",
            consumer_repo_id="acme-without-pr",
            provider_repo_id="acme-provider",
            transition=StubTransition.T4,
            from_state=StubState.ACTIVE,
            to_state=StubState.ABANDONED,
            consumer_status=RepoStatus.DEGRADED,
            detail="test abandon",
            abandon_reason=AbandonReason.END_OF_RUN,
        ),
    ]

    # Must not raise (the discriminating assertion: an AttributeError on the `None` draft under
    # the mutation "drop the `if draft is None: continue` guard").
    await _apply_stub_reconcile(db, RUN_ID, records, decisions, (), now=now)

    conn = sqlite3.connect(db)
    try:
        with_pr_state = conn.execute(
            "SELECT payload FROM findings WHERE run_id = ? AND repo_id = 'acme-with-pr' "
            "  AND kind = ?",
            (RUN_ID, PR_RECORD_KIND),
        ).fetchone()
        without_pr_row = conn.execute(
            "SELECT payload FROM findings WHERE run_id = ? AND repo_id = 'acme-without-pr' "
            "  AND kind = ?",
            (RUN_ID, PR_RECORD_KIND),
        ).fetchone()
    finally:
        conn.close()

    assert json.loads(with_pr_state[0])["state"] == "HELD", (
        "the consumer WITH a PR record must be re-persisted as PrState.HELD"
    )
    assert without_pr_row is None, (
        "a consumer absent from the run's PR records must have no PR row created for it"
    )
