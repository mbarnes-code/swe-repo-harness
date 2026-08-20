"""Behaviour tests for `src/fleet/orchestrator/reentry.py::phase_floor` (`fleet resume` §11.5
step 5, subtask 2).

`phase_floor` is pure: no DB handle, no `Git`, no filesystem, no `settings` object appears
anywhere below, on purpose (brief: "if you find you need one, that means the design's split is
wrong").

Two properties are pinned deliberately rather than incidentally:

**The search is a downward walk from the settled frontier, never a naive ascending scan of
`evidence`.** `test_fresh_repo_floor_is_scan_not_verify_under_naive_ascending_evidence` recreates
the exact trap the design doc names: a never-cloned repo (every `phases` row missing) paired with
an `evidence` mapping shaped like `rdepverify.preconditions_hold`'s documented `True`-on-missing-
BUILD-row behaviour (`evidence[VERIFY] = True`, everything earlier `False`). A naive walk that
picks "the phase whose evidence holds" would answer `VERIFY` — promoting an untouched repo to the
last phase. The downward-from-frontier algorithm answers `SCAN`, because the frontier for an
all-missing repo is `SCAN` regardless of what `evidence` says about later phases nobody has
reached yet.

**`DEGRADED` is a hard stop (ADR-0077 §5).** It is never chosen as the floor and the backward walk
never searches past it, even when an earlier phase's evidence does not hold.
"""

from __future__ import annotations

import itertools

import pytest

from fleet.models.enums import Phase, RepoStatus
from fleet.orchestrator.reentry import phase_floor
from fleet.state.repository import PhaseRow

_ALL_PHASES: tuple[Phase, ...] = (Phase.SCAN, Phase.TRANSFORM, Phase.BUILD, Phase.VERIFY)

_SETTLED = frozenset({RepoStatus.SUCCEEDED, RepoStatus.SKIPPED, RepoStatus.DEGRADED})


def _row(phase: Phase, status: RepoStatus) -> PhaseRow:
    return PhaseRow(
        run_id="run-1",
        repo_id="repo-1",
        phase=phase,
        status=status,
        attempts=0,
        max_attempts=3,
        lease_owner=None,
        lease_fence=0,
        lease_expires_at=None,
        last_error=None,
        updated_at="2026-08-20T00:00:00Z",
    )


def _rows_with(overrides: dict[Phase, PhaseRow | None]) -> dict[Phase, PhaseRow | None]:
    """Every phase defaults to a missing row (`None`) unless overridden."""
    return {phase: overrides.get(phase) for phase in _ALL_PHASES}


def _all_evidence(value: bool) -> dict[Phase, bool]:
    return {phase: value for phase in _ALL_PHASES}


# ---------------------------------------------------------------------------------------
# Table-driven: all 7 RepoStatus x 4 phase positions (28 cases).
#
# Shape of each case: phases 1..position-1 are SUCCEEDED (settled, so the frontier search moves
# past them); phase `position` carries the status under test; phases after `position` have no row
# at all (missing -> PENDING). `evidence` holds everywhere, which isolates the classification of
# `status_under_test` from the backward-search mechanics (those get their own dedicated tests
# below) -- with evidence uniformly True, `floor` can only ever equal whatever `frontier` the
# classification produces.
# ---------------------------------------------------------------------------------------


def _expected_floor(status_under_test: RepoStatus, position: Phase) -> Phase | None:
    if status_under_test is RepoStatus.REQUIRES_HUMAN_INTERVENTION:
        return None
    if status_under_test in _SETTLED:
        if position is Phase.VERIFY:
            return None  # every phase settled -- nothing left to re-enter
        return Phase(int(position) + 1)  # frontier moves past `position` to the next phase
    return position  # PENDING / RUNNING / BLOCKED: `position` itself is the frontier


@pytest.mark.parametrize(
    "status_under_test,position",
    list(itertools.product(list(RepoStatus), _ALL_PHASES)),
    ids=lambda v: v.value if isinstance(v, RepoStatus) else v.name,
)
def test_status_x_phase_table(status_under_test: RepoStatus, position: Phase) -> None:
    overrides: dict[Phase, PhaseRow | None] = {}
    for phase in _ALL_PHASES:
        if phase < position:
            overrides[phase] = _row(phase, RepoStatus.SUCCEEDED)
        elif phase == position:
            overrides[phase] = _row(phase, status_under_test)
        # phases after `position` are left missing (None) -> PENDING
    rows = _rows_with(overrides)
    evidence = _all_evidence(True)

    assert phase_floor(rows, evidence) == _expected_floor(status_under_test, position)


# ---------------------------------------------------------------------------------------
# Named acceptance-criteria cases (brief), each restated as its own assertion.
# ---------------------------------------------------------------------------------------


def test_requires_human_intervention_anywhere_yields_none_even_with_other_work_pending() -> None:
    rows = _rows_with(
        {
            Phase.SCAN: _row(Phase.SCAN, RepoStatus.SUCCEEDED),
            Phase.TRANSFORM: _row(Phase.TRANSFORM, RepoStatus.REQUIRES_HUMAN_INTERVENTION),
            Phase.BUILD: _row(Phase.BUILD, RepoStatus.PENDING),
            # Phase.VERIFY missing
        }
    )
    assert phase_floor(rows, _all_evidence(False)) is None


def test_all_settled_repo_yields_none() -> None:
    rows = _rows_with(
        {phase: _row(phase, RepoStatus.SUCCEEDED) for phase in _ALL_PHASES}
    )
    assert phase_floor(rows, _all_evidence(False)) is None


def test_all_settled_repo_with_skipped_and_degraded_mix_yields_none() -> None:
    rows = _rows_with(
        {
            Phase.SCAN: _row(Phase.SCAN, RepoStatus.SUCCEEDED),
            Phase.TRANSFORM: _row(Phase.TRANSFORM, RepoStatus.SKIPPED),
            Phase.BUILD: _row(Phase.BUILD, RepoStatus.DEGRADED),
            Phase.VERIFY: _row(Phase.VERIFY, RepoStatus.SUCCEEDED),
        }
    )
    assert phase_floor(rows, _all_evidence(False)) is None


def test_missing_row_is_treated_as_pending_not_as_settled() -> None:
    """A repo with SCAN succeeded and every later row absent must behave identically to the same
    repo with those later rows explicitly written as PENDING -- the missing-row-is-PENDING rule
    must not be a lucky side effect of `dict.get` defaulting, but an intentional equivalence."""
    missing_rows = _rows_with({Phase.SCAN: _row(Phase.SCAN, RepoStatus.SUCCEEDED)})
    explicit_pending_rows = _rows_with(
        {
            Phase.SCAN: _row(Phase.SCAN, RepoStatus.SUCCEEDED),
            Phase.TRANSFORM: _row(Phase.TRANSFORM, RepoStatus.PENDING),
            Phase.BUILD: _row(Phase.BUILD, RepoStatus.PENDING),
            Phase.VERIFY: _row(Phase.VERIFY, RepoStatus.PENDING),
        }
    )
    evidence = _all_evidence(True)
    assert phase_floor(missing_rows, evidence) == Phase.TRANSFORM
    assert phase_floor(missing_rows, evidence) == phase_floor(explicit_pending_rows, evidence)


# ---------------------------------------------------------------------------------------
# Downward-search mechanics: evidence gaps push the floor further back than the frontier.
# ---------------------------------------------------------------------------------------


def test_backward_search_demotes_past_multiple_settled_phases_when_evidence_is_missing() -> None:
    # SCAN, TRANSFORM, BUILD all SUCCEEDED; VERIFY still PENDING -> frontier is VERIFY.
    # evidence fails to hold at BUILD and TRANSFORM but holds at SCAN -> floor should land on
    # TRANSFORM (the first phase, walking backward, where evidence stops failing one step later).
    rows = _rows_with(
        {
            Phase.SCAN: _row(Phase.SCAN, RepoStatus.SUCCEEDED),
            Phase.TRANSFORM: _row(Phase.TRANSFORM, RepoStatus.SUCCEEDED),
            Phase.BUILD: _row(Phase.BUILD, RepoStatus.SUCCEEDED),
            Phase.VERIFY: _row(Phase.VERIFY, RepoStatus.PENDING),
        }
    )
    evidence = {Phase.SCAN: True, Phase.TRANSFORM: False, Phase.BUILD: False, Phase.VERIFY: True}
    assert phase_floor(rows, evidence) == Phase.TRANSFORM


def test_backward_search_stops_as_soon_as_evidence_holds() -> None:
    rows = _rows_with(
        {
            Phase.SCAN: _row(Phase.SCAN, RepoStatus.SUCCEEDED),
            Phase.TRANSFORM: _row(Phase.TRANSFORM, RepoStatus.SUCCEEDED),
            Phase.BUILD: _row(Phase.BUILD, RepoStatus.SUCCEEDED),
            Phase.VERIFY: _row(Phase.VERIFY, RepoStatus.PENDING),
        }
    )
    evidence = {Phase.SCAN: True, Phase.TRANSFORM: True, Phase.BUILD: False, Phase.VERIFY: True}
    assert phase_floor(rows, evidence) == Phase.BUILD


def test_degraded_phase_is_a_hard_stop_it_is_not_demoted_and_search_does_not_pass_it() -> None:
    # BUILD is DEGRADED; SCAN's evidence does not hold, but the search must never reach SCAN,
    # and it must never move the floor onto BUILD itself (ADR-0077 §5).
    rows = _rows_with(
        {
            Phase.SCAN: _row(Phase.SCAN, RepoStatus.SUCCEEDED),
            Phase.TRANSFORM: _row(Phase.TRANSFORM, RepoStatus.SUCCEEDED),
            Phase.BUILD: _row(Phase.BUILD, RepoStatus.DEGRADED),
            Phase.VERIFY: _row(Phase.VERIFY, RepoStatus.PENDING),
        }
    )
    evidence = {Phase.SCAN: False, Phase.TRANSFORM: False, Phase.BUILD: False, Phase.VERIFY: True}
    assert phase_floor(rows, evidence) == Phase.VERIFY


def test_fresh_repo_floor_is_scan_not_verify_under_naive_ascending_evidence() -> None:
    """The trap named in the design doc: `rdepverify.preconditions_hold` returns True when the
    BUILD row is missing, so an algorithm that walks evidence ascending and stops at the first
    True would answer VERIFY for a repo that has never been cloned. The downward-from-frontier
    algorithm must answer SCAN instead, because with every row missing the frontier is SCAN and
    there is nothing earlier to search."""
    rows = _rows_with({})  # every phase missing -> PENDING
    evidence = {
        Phase.SCAN: False,
        Phase.TRANSFORM: False,
        Phase.BUILD: False,
        Phase.VERIFY: True,  # mirrors rdepverify's True-on-missing-BUILD-row default
    }
    assert phase_floor(rows, evidence) == Phase.SCAN
