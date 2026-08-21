"""§11.5 step 6's pure half: `plan_unblocking`, and the fail-closed blocker predicate.

Every test here runs with **no database, no clock and no connection** — that is the property the
unit was built for, not an accident of the fixtures. The store method and the `_resume_impl`
wiring are other lanes' work; if anything in this file ever needs a `tmp_path`, the separation
has been lost.

**What each test watches, and whether the defect could leave it unchanged.** The defect this
module exists to prevent is the *fail-open* recompute: erasing a `blocked_by` entry the predicate
could not positively resolve, which silently undoes an audited `OperatorQuarantine` and
permanently erases every entry written by one of SPEC §3.5's three zero-producer triggers
(ADR-0090 §2.4, ruling R2-CLOSED). The quantity that moves under it is **the membership of an
unresolvable name in `Unblocking.remaining` vs `Unblocking.removed`** — and it moves for
*unresolvable* names only. So:

* `test_a_quarantined_blockers_dependent_keeps_its_entry` and
  `test_an_rhi_blocker_re_run_to_succeeded_loses_its_entry` are the two obvious cases and
  **neither discriminates**: both of their blockers resolve, so a fail-open predicate answers them
  identically. They are here because they pin the predicate's two live producers, not because they
  catch the defect. Stating that is the point — a fixture set of those two alone is exactly the
  hurried version that certifies a fail-open recompute green.
* `test_an_entry_naming_a_string_that_resolves_to_no_repo_survives_untouched` is the
  **discriminating** one. Its blocker is absent from `blocker_statuses` entirely, so it is the only
  fixture whose verdict differs between the two polarities.
* `test_a_bare_skipped_blocker_is_retained_because_a_skipped_repo_has_not_landed` is case 4, and
  it discriminates a **different** defect: not the fail-open polarity but the narrowing of
  `BLOCKING_STATUSES` back to `{REQUIRES_HUMAN_INTERVENTION}` plus a quarantine-finding check,
  which is precisely the shape a reconciler reaches for. It is measured-unreachable in the shipped
  tree — no writer produces a `SKIPPED` blocker without an `OperatorQuarantine` finding — so this
  test is what keeps the branch from being deleted as dead logic.

**Anchors are kept distinct on purpose.** No two fixtures share a blocker name, and no fixture's
blocker appears in `blocker_statuses` under a second status — a fixture whose "resolvable" and
"unresolvable" anchors coincided could not express the defect at all, which is how eight tests in
this project once passed under the exact defect they existed to catch.
"""

from __future__ import annotations

import pytest

from fleet.models.enums import Phase, RepoStatus
from fleet.orchestrator.reentry import (
    BLOCKING_STATUSES,
    QUARANTINE_FINDING_KIND,
    BlockerState,
    Unblocking,
    plan_unblocking,
    still_blocking,
)

# The three cases the fixture must contain, built once so every test names the same anchors.
#
# `quarantined-dep` is blocked by a repo `fleet quarantine` SKIPPED under an audited finding;
# `abandoned-dep` is blocked by a repo that was RHI and has since been re-run to SUCCEEDED;
# `orphan-dep` is blocked by a string that resolves to no repo at all -- SPEC §3.5 mandates a
# `contract_id` in `phases.blocked_by` on `contracts.status='FAILED'`, and no code writes one, so
# this is the shape of an entry a live recompute can never re-derive.
_ROWS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("quarantined-dep", ("acme-gated",)),
    ("abandoned-dep", ("acme-fixed",)),
    ("orphan-dep", ("contract:acme.protos:1.4",)),
    ("excluded-dep", ("acme-excluded",)),
)

_STATUSES: dict[str, BlockerState] = {
    "acme-gated": BlockerState(
        status=RepoStatus.SKIPPED, finding_kinds=frozenset({QUARANTINE_FINDING_KIND})
    ),
    "acme-fixed": BlockerState(status=RepoStatus.SUCCEEDED, finding_kinds=frozenset()),
    # case 4: `SKIPPED` with NO quarantine finding. Measured-unreachable in the shipped tree, and
    # deliberately built as a *distinct* input from `acme-gated` so the retention is expressible.
    "acme-excluded": BlockerState(status=RepoStatus.SKIPPED, finding_kinds=frozenset()),
}

_FLOORS: dict[str, Phase] = {
    "quarantined-dep": Phase.TRANSFORM,
    "abandoned-dep": Phase.BUILD,
    "orphan-dep": Phase.SCAN,
    "excluded-dep": Phase.TRANSFORM,
}


def _plan() -> dict[str, Unblocking]:
    return {
        entry.repo_id: entry
        for entry in plan_unblocking(
            blocked_by_rows=_ROWS, blocker_statuses=_STATUSES, floors=_FLOORS
        )
    }


# ---------------------------------------------------------------------------------------
# the three fixture cases the fail-closed ruling requires
# ---------------------------------------------------------------------------------------


def test_a_quarantined_blockers_dependent_keeps_its_entry() -> None:
    """Case 1. Watches: `acme-gated`'s membership in `remaining`.

    Does NOT discriminate a fail-open predicate — `acme-gated` resolves, so both polarities answer
    it the same way. It pins the `SKIPPED` + `OperatorQuarantine` half of the predicate instead:
    without it, narrowing the predicate to `BLOCKING_STATUSES` alone would go green.
    """
    entry = _plan()["quarantined-dep"]
    assert entry.remaining == ("acme-gated",)
    assert entry.removed == ()


def test_an_rhi_blocker_re_run_to_succeeded_loses_its_entry() -> None:
    """Case 2. Watches: `acme-fixed`'s membership in `removed`, and the list emptying.

    Also does not discriminate. It is the direction that stops the ruling being implemented as
    "retain everything": a predicate that never removes anything would fail here.
    """
    entry = _plan()["abandoned-dep"]
    assert entry.removed == ("acme-fixed",)
    assert entry.remaining == ()


def test_an_entry_naming_a_string_that_resolves_to_no_repo_survives_untouched() -> None:
    """Case 3 -- **the discriminating one**. Watches: an unresolvable name's side of the split.

    `contract:acme.protos:1.4` is absent from `blocker_statuses` altogether. Under the fail-closed
    ruling it stays in `remaining` and `orphan-dep` is NOT admitted; under a fail-open predicate it
    moves to `removed`, `remaining` empties, and `orphan-dep` is admitted against a blocker nobody
    ever cleared. Both assertions below flip, and the two tests above stay green either way.
    """
    entry = _plan()["orphan-dep"]
    assert entry.remaining == ("contract:acme.protos:1.4",)
    assert entry.removed == ()


def test_an_unresolvable_repo_name_is_retained_for_the_same_reason_as_a_contract_id() -> None:
    """Case 3's second shape: a name that looks like a repo but was written by a trigger with no
    producer (§3.1 SCC members, §3.4 merge-timeout, §3.5 failed-contract descendants -- three of
    the five triggers `RepoState.blocked_by` enumerates). It is unresolvable for the same reason
    and gets the same answer, so the retention is not keyed to the `contract:` spelling.
    """
    assert still_blocking("acme-never-scanned", _STATUSES) is True


def test_a_bare_skipped_blocker_is_retained_because_a_skipped_repo_has_not_landed() -> None:
    """Case 4. Watches: `acme-excluded`'s side of the split, and `excluded-dep`'s admissibility.

    `acme-excluded` is `SKIPPED` with **no** `QUARANTINE_FINDING_KIND`. It resolves, so the
    fail-open polarity answers it exactly as fail-closed does and **M1 cannot express this case** —
    it discriminates a different mutation: narrowing `BLOCKING_STATUSES` back to
    `{REQUIRES_HUMAN_INTERVENTION}` and re-adding a quarantine-finding check, which is the change a
    reconciler makes to satisfy a criterion phrased only about RHI and quarantine.

    Why it is retained rather than removed: a `SKIPPED` repo **has not landed**, whatever excluded
    it, so clearing the entry admits `excluded-dep` to migrate against a dependency that never ran.
    The reason for the exclusion is not the question this predicate asks.

    Measured-unreachable today, which is why the assertion is what keeps the branch alive: the only
    non-delegating writers of `blocked_by` are `runner._contain` (`REQUIRES_HUMAN_INTERVENTION`)
    and `cli._quarantine_impl`, and the latter writes its `OperatorQuarantine` finding in the same
    command, so nothing in the shipped tree produces this shape. Without this test the branch reads
    as dead logic to the next author.
    """
    entry = _plan()["excluded-dep"]
    assert entry.remaining == ("acme-excluded",)
    assert entry.removed == ()
    assert still_blocking("acme-excluded", _STATUSES) is True


# ---------------------------------------------------------------------------------------
# the predicate itself
# ---------------------------------------------------------------------------------------


def test_the_predicate_retains_every_skipped_shape_and_rhi_and_nothing_else() -> None:
    """Watches: the whole verdict table, all five shapes at once.

    Every `SKIPPED` shape is retained -- audited quarantine, config exclusion, some other finding
    -- because none of them has landed and the *reason* is not what this predicate asks about.
    `SUCCEEDED` is the one shape that resolves to removable. Narrowing `BLOCKING_STATUSES` back to
    `{REQUIRES_HUMAN_INTERVENTION}` flips three of these five verdicts.
    """
    rhi = BlockerState(status=RepoStatus.REQUIRES_HUMAN_INTERVENTION, finding_kinds=frozenset())
    quarantined = BlockerState(
        status=RepoStatus.SKIPPED, finding_kinds=frozenset({QUARANTINE_FINDING_KIND})
    )
    config_skipped = BlockerState(status=RepoStatus.SKIPPED, finding_kinds=frozenset())
    other_finding = BlockerState(
        status=RepoStatus.SKIPPED, finding_kinds=frozenset({"BaselineRed"})
    )
    resolved = {
        "a": rhi,
        "b": quarantined,
        "c": config_skipped,
        "d": other_finding,
        "e": BlockerState(status=RepoStatus.SUCCEEDED, finding_kinds=frozenset()),
    }
    verdicts = {name: still_blocking(name, resolved) for name in resolved}
    assert verdicts == {"a": True, "b": True, "c": True, "d": True, "e": False}
    assert set(BLOCKING_STATUSES) == {
        RepoStatus.REQUIRES_HUMAN_INTERVENTION,
        RepoStatus.SKIPPED,
    }


def test_blocker_state_has_no_default_for_its_finding_kinds() -> None:
    """Watches: whether `finding_kinds` can be omitted at construction.

    **The rationale this test used to carry is retired and is written out here rather than left
    standing.** It was: with a default of `frozenset()`, a caller that forgot to read `findings`
    would hand every quarantined blocker in as an ordinary `SKIPPED` and un-quarantine it. Since
    `BLOCKING_STATUSES` holds `SKIPPED` outright that mistake changes no verdict, so this is no
    longer a guard against a silent audit reversal and must not be cited as one.

    What it still holds is that the two `SKIPPED` shapes stay distinct inputs: a defaulted field
    no verdict reads would be passed nowhere, and case 4's fixture -- the only place the bare shape
    is expressible -- would stop being distinguishable from the audited one.
    """
    with pytest.raises(TypeError):
        BlockerState(status=RepoStatus.SKIPPED)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------------------
# the planner's D74 properties
# ---------------------------------------------------------------------------------------


def test_the_dry_run_route_and_the_write_route_get_the_identical_tuple() -> None:
    """The success criterion, as a test rather than an aspiration.

    Watches: equality of two `plan_unblocking` results over the same inputs. There is one
    computation, called twice; a second comprehension written beside it for the preview is D74's
    shape exactly, and this is what would still be green if someone added one -- so the assertion
    is deliberately paired with the ordering assertion below, which pins the *determinism* the
    equality rests on (a set-ordered result compares equal to itself and not to a re-run in
    another process).
    """
    dry_run = plan_unblocking(blocked_by_rows=_ROWS, blocker_statuses=_STATUSES, floors=_FLOORS)
    write = plan_unblocking(blocked_by_rows=_ROWS, blocker_statuses=_STATUSES, floors=_FLOORS)
    assert dry_run == write


def test_the_result_is_sorted_by_repo_and_by_name_so_it_is_reproducible() -> None:
    """Watches: the order of both axes, over inputs presented in a different order.

    `append_blocked_by` persists `json.dumps(sorted(names))`, so sorted names are what a caller
    diffs against the column.
    """
    shuffled = (
        ("zeta", ("m-two", "m-one")),
        ("alpha", ("m-three",)),
    )
    plan = plan_unblocking(blocked_by_rows=shuffled, blocker_statuses={}, floors={})
    assert [entry.repo_id for entry in plan] == ["alpha", "zeta"]
    assert plan[1].remaining == ("m-one", "m-two")


def test_removed_and_remaining_partition_the_union_of_every_phase_row() -> None:
    """Watches: two things at once, both D74 shapes.

    (a) a repo's four `phases` rows are unioned HERE, so the store method and the preview do not
    each own a copy of that union; (b) `removed` and `remaining` together are exactly the union,
    so no caller re-derives one from the other to answer "did this list empty?".
    """
    rows = (
        ("multi", ("acme-gated",)),
        ("multi", ("acme-fixed", "acme-gated")),
        ("multi", ("contract:acme.protos:1.4",)),
        ("multi", ()),
    )
    (entry,) = plan_unblocking(blocked_by_rows=rows, blocker_statuses=_STATUSES, floors={})
    assert entry.removed == ("acme-fixed",)
    assert entry.remaining == ("acme-gated", "contract:acme.protos:1.4")
    assert set(entry.removed) | set(entry.remaining) == {
        "acme-gated",
        "acme-fixed",
        "contract:acme.protos:1.4",
    }
    assert set(entry.removed) & set(entry.remaining) == set()


def test_the_floor_is_reported_from_the_mapping_and_never_re_derived() -> None:
    """Watches: whether `Unblocking.floor` tracks `floors` or something recomputed.

    The fixture hands in a floor no rule here could have produced from these inputs (there are no
    `phases` rows in this call at all), so a re-derivation could not return `VERIFY` by accident.
    A repo absent from `floors` reports `None` rather than a guess.
    """
    plan = plan_unblocking(
        blocked_by_rows=(("has-floor", ()), ("no-floor", ())),
        blocker_statuses={},
        floors={"has-floor": Phase.VERIFY},
    )
    assert {entry.repo_id: entry.floor for entry in plan} == {
        "has-floor": Phase.VERIFY,
        "no-floor": None,
    }


def test_a_repo_with_nothing_removed_is_still_reported() -> None:
    """Watches: whether the result is a change-list or a per-repo report (D44).

    A count of removals cannot carry "this repo is still blocked, by these names", which is what
    an operator needs to know why a resume did not admit it.
    """
    plan = _plan()
    assert set(plan) == {"quarantined-dep", "abandoned-dep", "orphan-dep", "excluded-dep"}


def test_unblocking_is_frozen_so_a_caller_cannot_edit_the_plan_it_was_handed() -> None:
    entry = _plan()["orphan-dep"]
    with pytest.raises(AttributeError):
        entry.removed = ("contract:acme.protos:1.4",)  # type: ignore[misc]
