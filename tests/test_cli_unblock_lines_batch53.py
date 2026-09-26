"""§15.1 item 3, Wave 7.9 batch 53 (group G10c): `cli.py`'s §11.5 step 6 helpers.

Scope (per the dispatch brief): `UnblockLookupError`, `_blocker_states`, `_read_blocker_states`,
`_resolvable_repo_ids`, `_refuse_unresolved_blockers`, `_unblocking_entry`, `_blocker_resolver`,
`_unblock_dependents`, `_unblock_lines` — `_apply_unblocking` is already proven and excluded.

Method: `tests/test_resume_unblocking.py` already drives the WHOLE of §11.5 step 6 end to end
through a real `fleet resume` invocation (§12 item 46(ii)'s acceptance test), and that file is
this batch's primary reaching test — it is not duplicated here. What it cannot cheaply
discriminate is called out function-by-function below, following the isolated-direct-call
precedent already in this tree at `tests/test_cli_resume_report_lines_batch51.py` (batch 51,
G10a) rather than growing the already-large end-to-end fixture further.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
import pytest

from fleet import cli as fleet_cli
from fleet.cli import (
    UnblockLookupError,
    _blocker_resolver,
    _blocker_states,
    _read_blocker_states,
    _refuse_unresolved_blockers,
    _resolvable_repo_ids,
    _unblock_lines,
    _unblocking_entry,
)
from fleet.models.enums import Phase, RepoStatus
from fleet.orchestrator.reentry import BlockerState, Unblocking
from fleet.state.db import StateDbError
from tests.test_cli import RUN_ID, fresh_db, seed_run, write_config

STAMP = "2026-08-08T12:00:00+00:00"


# ======================================================================================
# `_unblock_lines` — no test anywhere asserts this function's rendered text (a `grep` for
# "step 6: no repo carries", "still blocked by", "UNRESOLVED for" and "appended wave" across
# tests/test_cli.py and tests/test_resume_unblocking.py returns nothing outside this file; every
# existing step-6 test reads the `--json` `unblocked_dependents` payload or raw `phases` rows,
# never `result.output`). A mutation garbling any one of these sentences, dropping the dry/real
# verb split, or reordering the early-return so the wave lines leak past a `candidates == 0`
# report would pass every existing test.
# ======================================================================================


def _report(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "candidates": 1,
        "unblocked": [],
        "retained": [],
        "unresolved": [],
        "wave_index": None,
        "wave_error": None,
    }
    base.update(overrides)
    return base


def test_unblock_lines_zero_candidates_short_circuits_before_the_wave_lines() -> None:
    """The `candidates == 0` branch must return its fixed sentence ALONE — even when `wave_index`
    and `wave_error` are (impossibly, but the function does not know that) both populated, proving
    the early return runs before those two checks rather than merely happening to see them empty
    in the ordinary case."""
    result = {
        "unblocked_dependents": _report(candidates=0, wave_index=9, wave_error="should not print")
    }
    assert _unblock_lines(result, dry=False) == [
        "  step 6: no repo carries a `blocked_by` entry; nothing to recompute"
    ]


def test_unblock_lines_unblocked_entry_verb_and_floor_suffix() -> None:
    with_floor = {
        "unblocked_dependents": _report(
            unblocked=[
                {"repo_id": "acme-x", "removed": ["acme-b"], "remaining": [], "floor": "BUILD"}
            ]
        )
    }
    real = _unblock_lines(with_floor, dry=False)
    dry = _unblock_lines(with_floor, dry=True)
    assert real == [
        (
            "  step 6: cleared acme-b from acme-x — its `blocked_by` is empty, so it returns to "
            "PENDING at floor BUILD"
        )
    ]
    assert dry == [
        (
            "  step 6: would clear acme-b from acme-x — its `blocked_by` is empty, so it "
            "returns to PENDING at floor BUILD"
        )
    ]

    without_floor = {
        "unblocked_dependents": _report(
            unblocked=[{"repo_id": "acme-y", "removed": ["acme-b"], "remaining": [], "floor": None}]
        )
    }
    lines = _unblock_lines(without_floor, dry=False)
    assert lines == [
        "  step 6: cleared acme-b from acme-y — its `blocked_by` is empty, so it returns to PENDING"
    ], "a None floor must not append an ` at floor ...` suffix"


def test_unblock_lines_retained_entry_parenthetical_only_when_removed_is_non_empty() -> None:
    partial = {
        "unblocked_dependents": _report(
            retained=[
                {
                    "repo_id": "acme-mixed",
                    "removed": ["acme-a"],
                    "remaining": ["acme-b"],
                    "floor": None,
                }
            ]
        )
    }
    assert _unblock_lines(partial, dry=False) == [
        "  step 6: acme-mixed still blocked by acme-b (cleared acme-a)"
    ]

    untouched = {
        "unblocked_dependents": _report(
            retained=[
                {"repo_id": "acme-held", "removed": [], "remaining": ["acme-c"], "floor": None}
            ]
        )
    }
    assert _unblock_lines(untouched, dry=False) == [
        "  step 6: acme-held still blocked by acme-c"
    ], "an empty `removed` must not append a dangling '()' parenthetical"


def test_unblock_lines_unresolved_entries_render_the_repo_and_reason() -> None:
    result = {
        "unblocked_dependents": _report(
            unresolved=[{"repo_id": "acme-race", "reason": "moved before this write landed"}]
        )
    }
    assert _unblock_lines(result, dry=False) == [
        "  step 6: UNRESOLVED for acme-race — moved before this write landed"
    ]


def test_unblock_lines_wave_index_and_wave_error_are_independent_trailing_lines() -> None:
    both = {"unblocked_dependents": _report(wave_index=3, wave_error="append failed: budget")}
    lines = _unblock_lines(both, dry=False)
    assert "  step 6: freed repos moved into appended wave 3 (synthetic)" in lines
    assert "  step 6: the appended wave FAILED — append failed: budget" in lines

    neither = {"unblocked_dependents": _report()}
    lines2 = _unblock_lines(neither, dry=False)
    assert not any("appended wave" in line for line in lines2), (
        "wave_index=None, wave_error=None must render no wave line at all"
    )


# ======================================================================================
# `_unblocking_entry` — no test anywhere calls this pure formatter directly. Every existing
# assertion reads either the `--json` dict it PRODUCES (never checking `floor` is the enum's
# `.name`, not `.value`) or the `Unblocking` dataclass it consumes, never both in the same place.
# ======================================================================================


def test_unblocking_entry_renders_floor_as_name_and_copies_both_tuples_to_lists() -> None:
    with_floor = Unblocking(
        repo_id="acme-x", removed=("acme-a", "acme-b"), remaining=(), floor=Phase.BUILD
    )
    assert _unblocking_entry(with_floor) == {
        "repo_id": "acme-x",
        "removed": ["acme-a", "acme-b"],
        "remaining": [],
        "floor": "BUILD",
    }, "floor must be `Phase.BUILD.name` ('BUILD'), not `.value` (3) or the enum member itself"

    without_floor = Unblocking(repo_id="acme-y", removed=(), remaining=("acme-c",), floor=None)
    assert _unblocking_entry(without_floor) == {
        "repo_id": "acme-y",
        "removed": [],
        "remaining": ["acme-c"],
        "floor": None,
    }


# ======================================================================================
# `_blocker_states` — every existing exercise of this function is indirect, through
# `tests/test_resume_unblocking.py`'s multi-repo fixture, where every repo queried in one call
# happens to already differ in an outcome-relevant way. No test isolates the pure grouping itself:
# a mutation that folded every repo's rows into ONE shared status set (rather than keying by
# `repo_id`) would still pass that fixture as long as SOME repo in the batch carries a
# non-landed status (poisoning every OTHER repo in the same call into "retained" too, which the
# existing fixture cannot distinguish from those repos' own correct "retained" verdict).
# ======================================================================================


def test_blocker_states_keeps_each_repos_statuses_separate_in_one_call() -> None:
    rows = [
        ("acme-landed", 1, "SUCCEEDED"),
        ("acme-stuck", 1, "PENDING"),
    ]
    states = _blocker_states(rows)
    assert set(states) == {"acme-landed", "acme-stuck"}
    assert states["acme-landed"].phase_statuses == frozenset({RepoStatus.SUCCEEDED}), (
        "acme-landed's own row must not be contaminated by acme-stuck's PENDING row"
    )
    assert states["acme-stuck"].phase_statuses == frozenset({RepoStatus.PENDING}), (
        "acme-stuck's own row must not be contaminated by acme-landed's SUCCEEDED row"
    )


# ======================================================================================
# `_read_blocker_states` / `_resolvable_repo_ids` — both build a `repo_id IN (...)` filter over
# the CALLER-SUPPLIED `names`, but `tests/test_resume_unblocking.py`'s fixture always queries the
# union of every blocker referenced anywhere in the run, so a mutation dropping the `IN` filter
# (reading every row/repo in the run regardless of `names`) has never been exercised against a
# database that ALSO holds an unrelated repo outside the queried set.
# ======================================================================================


async def test_read_blocker_states_only_returns_the_given_names_names_arg(tmp_path: Path) -> None:
    db = fresh_db(tmp_path / "state" / "fleet.db")
    seed_run(db, repos=("acme-asked", "acme-not-asked"))
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        for repo, status in (("acme-asked", "SUCCEEDED"), ("acme-not-asked", "PENDING")):
            conn.execute(
                "INSERT INTO phases (run_id, repo_id, phase, status, updated_at) "
                "VALUES (?, ?, 1, ?, ?)",
                (RUN_ID, repo, status, STAMP),
            )
    finally:
        conn.close()

    async with aiosqlite.connect(db) as aconn:
        states = await _read_blocker_states(aconn, RUN_ID, {"acme-asked"})

    assert set(states) == {"acme-asked"}, (
        "acme-not-asked has a real `phases` row in this run but was never named — it must not "
        "appear, or a caller iterating `names` would read a state it never asked for"
    )


async def test_read_blocker_states_empty_names_returns_empty_without_erroring(
    tmp_path: Path,
) -> None:
    db = fresh_db(tmp_path / "state" / "fleet.db")
    seed_run(db, repos=("acme-a",))
    async with aiosqlite.connect(db) as aconn:
        assert await _read_blocker_states(aconn, RUN_ID, set()) == {}


async def test_resolvable_repo_ids_only_returns_the_given_names_names_arg(tmp_path: Path) -> None:
    db = fresh_db(tmp_path / "state" / "fleet.db")
    seed_run(db, repos=("acme-asked", "acme-not-asked"))
    async with aiosqlite.connect(db) as aconn:
        known = await _resolvable_repo_ids(aconn, {"acme-asked"})
    assert known == {"acme-asked"}, (
        "acme-not-asked is a real row in `repos` but was never named — reporting it resolvable "
        "would hide a genuinely unresolvable name elsewhere in the same `known - states` refusal"
    )


# ======================================================================================
# `_refuse_unresolved_blockers` — `tests/test_resume_unblocking.py`'s one loud-failure test seeds
# exactly ONE resolvable-but-missing name, so it cannot discriminate `known - set(states)` (the
# shipped direction) from its mirror `set(states) - known`, and it cannot prove the `sorted(...)`
# call — both read identically with a single missing name.
# ======================================================================================


def test_refuse_unresolved_blockers_names_only_the_missing_subset_in_sorted_order() -> None:
    states = {"acme-resolved": BlockerState(phase_statuses=frozenset({RepoStatus.SUCCEEDED}))}
    with pytest.raises(UnblockLookupError) as excinfo:
        _refuse_unresolved_blockers(RUN_ID, {"acme-z", "acme-resolved", "acme-m"}, states)
    message = str(excinfo.value)
    assert message.startswith(f"run {RUN_ID}: ['acme-m', 'acme-z'] appear"), message
    assert "acme-resolved" not in message.split("appear in")[0], (
        "a name that DID resolve must not be reported as missing"
    )
    assert isinstance(excinfo.value, StateDbError), (
        "UnblockLookupError must fall under the CLI's StateDbError funnel (`_mapped_errors`) so "
        "it maps to ExitCode.UNEXPECTED_ERROR rather than an uncaught traceback"
    )


def test_refuse_unresolved_blockers_is_silent_when_every_known_name_resolved() -> None:
    _refuse_unresolved_blockers(
        RUN_ID, {"acme-a"}, {"acme-a": BlockerState(phase_statuses=frozenset())}
    )  # must not raise


# ======================================================================================
# `_blocker_resolver` — its own docstring says the closure it returns is "the identical function"
# called by the outer `mode=ro` preview in `_unblock_dependents`, so that the plan an operator is
# shown and the plan `clear_blocked_by`'s transaction re-derives cannot be two rules. Every
# existing test of the loud lookup-miss path (`test_a_blocker_the_repos_table_knows_but_the_
# status_lookup_misses_is_loud`) trips the OUTER preview call at line ~19211, before
# `plan_unblocking`/`_apply_unblocking` ever run — the closure this function builds is never
# directly exercised, on either path, anywhere.
# ======================================================================================


async def test_blocker_resolver_resolve_returns_states_for_resolvable_names(
    tmp_path: Path,
) -> None:
    db = fresh_db(tmp_path / "state" / "fleet.db")
    seed_run(db, repos=("acme-a",))
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO phases (run_id, repo_id, phase, status, updated_at) "
            "VALUES (?, 'acme-a', 1, 'SUCCEEDED', ?)",
            (RUN_ID, STAMP),
        )
    finally:
        conn.close()

    resolve = _blocker_resolver(RUN_ID)
    async with aiosqlite.connect(db) as aconn:
        states = await resolve(aconn, frozenset({"acme-a"}))

    assert states["acme-a"].phase_statuses == frozenset({RepoStatus.SUCCEEDED})


async def test_blocker_resolver_resolve_raises_when_a_known_repo_has_no_phases_row(
    tmp_path: Path,
) -> None:
    """The transaction-side twin of the preview's loud check: a name the `resolve` function is
    asked about that is in `repos` but produced no `BlockerState` must abort THIS call too, not
    only the outer preview — see the module docstring's "BOTH routes" claim."""
    db = fresh_db(tmp_path / "state" / "fleet.db")
    seed_run(db, repos=("acme-ghost",))  # in `repos`, but no `phases` row at all
    resolve = _blocker_resolver(RUN_ID)
    async with aiosqlite.connect(db) as aconn:
        with pytest.raises(UnblockLookupError, match="acme-ghost"):
            await resolve(aconn, frozenset({"acme-ghost"}))


# ======================================================================================
# `_unblock_dependents` — its own docstring states the `--dry-run`/real split is "the same
# `plan_unblocking` with the same inputs; the branch is the terminal persist and nothing above
# it", and separately that `_apply_unblocking` (excluded from this batch) opens a real
# `StateWriter`. What is UNPROVEN is the guard that decides whether to open it at all:
# `if not dry_run and any(plan.removed for plan in plans)`. Every existing real (non-dry-run) test
# in `tests/test_resume_unblocking.py` frees at least one repo, so `_apply_unblocking` is always
# called on the real-run path there — a real run in which NO plan has anything to remove (every
# blocker still outstanding) has never been driven, so a mutation weakening this guard to `if not
# dry_run:` alone (opening a write transaction for a no-op resume) would pass every existing test.
# ======================================================================================


async def test_unblock_dependents_skips_apply_unblocking_when_no_plan_has_a_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_config(tmp_path)
    from fleet.settings import FleetSettings

    settings = FleetSettings.load(tmp_path / "config")
    db = tmp_path / "state" / "fleet.db"
    fresh_db(db)
    seed_run(
        db,
        repos=("acme-blocker", "acme-dep"),
        config_digests=json.dumps(dict(settings.section_digests), sort_keys=True),
    )
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        # the blocker is still PENDING everywhere -> not landed -> `still_blocking` retains it
        conn.execute(
            "INSERT INTO phases (run_id, repo_id, phase, status, blocked_by, updated_at) "
            "VALUES (?, 'acme-blocker', 1, 'PENDING', '[]', ?)",
            (RUN_ID, STAMP),
        )
        payload = json.dumps(["acme-blocker"])
        for phase, status in ((1, "PENDING"), (2, "BLOCKED")):
            conn.execute(
                "INSERT INTO phases (run_id, repo_id, phase, status, blocked_by, updated_at) "
                "VALUES (?, 'acme-dep', ?, ?, ?, ?)",
                (RUN_ID, phase, status, payload, STAMP),
            )
    finally:
        conn.close()

    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError(
            "`_apply_unblocking` must not run when no plan has a removal, even on a real "
            "(non-dry-run) call — nothing to write means no writer should be opened at all"
        )

    monkeypatch.setattr(fleet_cli, "_apply_unblocking", boom)

    report = await fleet_cli._unblock_dependents(
        settings,
        db,
        RUN_ID,
        floors={},
        dry_run=False,
        now=datetime.now(UTC),
    )

    assert report["applied"] is False
    assert report["wave_index"] is None
    assert [entry["repo_id"] for entry in report["retained"]] == ["acme-dep"]
    assert report["unblocked"] == []
