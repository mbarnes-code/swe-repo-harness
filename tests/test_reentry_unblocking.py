"""§11.5 step 6's pure half: `plan_unblocking`, and the fail-closed blocker predicate.

Every test here runs with **no database, no clock and no connection** — that is the property the
unit was built for, not an accident of the fixtures. The store method and the `_resume_impl`
wiring are other lanes' work; if anything in this file ever needs a `tmp_path`, the separation
has been lost.

**Each fixture case discriminates a different defect, and the mapping is stated rather than
implied.** The previous version of this file had two cases (a quarantined blocker and a bare
`SKIPPED` one) that were *input-identical* to the unit — the predicate read only one field and
both fixtures set it the same way — so one of them discriminated nothing and was green under
every mutation anyone ran. That is the two-anchor collapse CLAUDE.md records; it is fixed here by
making the anchors differ in what the predicate actually reads, not in what the fixture is called.

1. `quarantined-dep` → `acme-gated` = `{SKIPPED, SUCCEEDED}` — reddens **C3**: a lossy reduction
   of the vector to one status, or `any` where the predicate says `all`.
2. `abandoned-dep` → `acme-fixed` = `{SUCCEEDED}` — reddens a predicate that never removes
   anything.
3. `orphan-dep` → `contract:…`, **absent** from the mapping — reddens fail-**open** on a name that
   cannot be resolved.
4. `live-dep` → `acme-running` = `{SUCCEEDED, PENDING}` — reddens the **deny-list**: neither member
   is a "blocking" status, so *remove what is not shown to be blocking* clears this entry.
5. `norows-dep` → `acme-norows` = `frozenset()` — reddens `all(())` being `True`, which removes a
   blocker whose rows could not be found.

Cases 1 and 4 are the two that were missing when the deny-list shipped, and they are the two that
correspond to the review's Criticals. Case 4 also separates this predicate from the weaker fix of
"retain if **any** row is in a blocking set": under that rule `{SUCCEEDED, PENDING}` is removed.
"""

from __future__ import annotations

import pytest

from fleet.models.enums import Phase, RepoStatus
from fleet.orchestrator.reentry import (
    LANDED_STATUSES,
    BlockerState,
    Unblocking,
    plan_unblocking,
    still_blocking,
    stub_permits_removal,
)

_ROWS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("quarantined-dep", ("acme-gated",)),
    ("abandoned-dep", ("acme-fixed",)),
    ("orphan-dep", ("contract:acme.protos:1.4",)),
    ("live-dep", ("acme-running",)),
    ("norows-dep", ("acme-norows",)),
)

_STATUSES: dict[str, BlockerState] = {
    # Case 1 -- the C3 vector, taken from an EXECUTED run of the shipped `cli._quarantine_impl`
    # against a temp DB: a repo that finished SCAN and TRANSFORM and is then quarantined between
    # phases ends at `[(1, 'SKIPPED'), (2, 'SUCCEEDED')]`, with the `OperatorQuarantine` finding
    # written and its dependent `BLOCKED`. Not a hypothetical shape.
    "acme-gated": BlockerState(
        phase_statuses=frozenset({RepoStatus.SKIPPED, RepoStatus.SUCCEEDED})
    ),
    "acme-fixed": BlockerState(phase_statuses=frozenset({RepoStatus.SUCCEEDED})),
    # Case 4 -- nothing here is terminal-and-blocking; a deny-list of blocking statuses removes it.
    "acme-running": BlockerState(
        phase_statuses=frozenset({RepoStatus.SUCCEEDED, RepoStatus.PENDING})
    ),
    # Case 5 -- resolved, but the repo carries no `phases` rows at all.
    "acme-norows": BlockerState(phase_statuses=frozenset()),
}

_FLOORS: dict[str, Phase] = {
    "quarantined-dep": Phase.TRANSFORM,
    "abandoned-dep": Phase.BUILD,
    "orphan-dep": Phase.SCAN,
    "live-dep": Phase.TRANSFORM,
    "norows-dep": Phase.SCAN,
}


def _plan() -> dict[str, Unblocking]:
    return {
        entry.repo_id: entry
        for entry in plan_unblocking(
            blocked_by_rows=_ROWS, blocker_statuses=_STATUSES, floors=_FLOORS
        )
    }


# ---------------------------------------------------------------------------------------
# the five fixture cases
# ---------------------------------------------------------------------------------------


def test_a_quarantine_between_phases_keeps_its_dependents_blocked() -> None:
    """**Case 1 — the reachable silent undo of an audited `OperatorQuarantine`.**

    Watches: whether `acme-gated`'s entry survives when the blocker's phase vector holds a
    `SKIPPED` row *below* a `SUCCEEDED` one.

    The sequence, every step shipped code and the vector executed rather than assumed:
    `dep` finishes SCAN and TRANSFORM; its BUILD and VERIFY rows do not exist yet (the phase row
    is created on admission). The operator runs `fleet quarantine dep --reason …` between phases.
    `_quarantine_impl`'s `movable` list excludes rows already in `TERMINAL_STATUSES`, and
    `SUCCEEDED` is one, so nothing is movable and the `else` branch stamps `SKIPPED` onto phase 1
    alone; dependents are blocked unconditionally, outside both branches. The result is
    `[(1, 'SKIPPED'), (2, 'SUCCEEDED')]` with the finding written and the dependent `BLOCKED`.

    Any reduction of that vector to one status by highest phase — the reduction
    `state/projection._fold_repos` uses — yields `SUCCEEDED`, and the entry is cleared on the next
    `fleet resume`. The audited quarantine is silently undone. Retaining the whole vector is what
    makes that unrepresentable here; `any`-instead-of-`all` re-opens it and reddens this test.
    """
    entry = _plan()["quarantined-dep"]
    assert entry.remaining == ("acme-gated",)
    assert entry.removed == ()
    assert still_blocking("acme-gated", _STATUSES) is True


def test_a_blocker_whose_every_phase_landed_loses_its_entry() -> None:
    """Case 2. Watches: `acme-fixed`'s membership in `removed`, and the list emptying.

    The direction that stops the fail-closed ruling being implemented as "retain everything": a
    predicate that never removes anything fails here, and it is the only case that fails on it.
    """
    entry = _plan()["abandoned-dep"]
    assert entry.removed == ("acme-fixed",)
    assert entry.remaining == ()


def test_an_entry_naming_a_string_that_resolves_to_no_repo_survives_untouched() -> None:
    """Case 3. Watches: an unresolvable name's side of the `removed`/`remaining` split.

    `contract:acme.protos:1.4` is absent from `blocker_statuses` altogether — SPEC §3.5 mandates a
    `contract_id` in `phases.blocked_by` on `contracts.status='FAILED'` and no code writes one, so
    this is the shape of an entry a live recompute can never re-derive. Under the fail-closed
    ruling it stays; under a fail-open predicate `orphan-dep` is admitted against a blocker nobody
    cleared. This is the only case whose verdict differs between the two polarities.
    """
    entry = _plan()["orphan-dep"]
    assert entry.remaining == ("contract:acme.protos:1.4",)
    assert entry.removed == ()


def test_an_unresolvable_repo_name_is_retained_for_the_same_reason_as_a_contract_id() -> None:
    """Case 3's second shape: a name that looks like a repo but was written by a trigger with no
    producer (§3.1 SCC members, §3.4 merge-timeout, §3.5 failed-contract descendants -- three of
    the five triggers `RepoState.blocked_by` enumerates). Unresolvable for the same reason, same
    answer, so the retention is not keyed to the `contract:` spelling.
    """
    assert still_blocking("acme-never-scanned", _STATUSES) is True


def test_a_blocker_with_an_unlanded_phase_is_retained_though_no_phase_is_blocking() -> None:
    """**Case 4 — the case a deny-list gets wrong.**

    Watches: `acme-running`, whose vector is `{SUCCEEDED, PENDING}`. Neither member is in any
    plausible set of "blocking" statuses, so a deny-list — *remove anything not positively shown
    to be blocking* — clears this entry and admits `live-dep` against a dependency that has not
    finished. Measured on the deny-list this replaced: **5 of 7 `RepoStatus` members were
    removable**, `PENDING` among them.

    It also separates this predicate from the weaker repair of "retain if **any** row is in a
    blocking set", which removes this entry too. Only *every row landed* keeps it.
    """
    entry = _plan()["live-dep"]
    assert entry.remaining == ("acme-running",)
    assert entry.removed == ()


def test_a_blocker_resolved_to_zero_phase_rows_is_retained() -> None:
    """**Case 5 — the `all(())` trap.**

    Watches: `acme-norows`, resolved to an empty vector. `all(status in LANDED_STATUSES for …)`
    over an empty set is `True`, so the natural spelling of this predicate *removes* a blocker
    whose rows could not be found. "No rows" is the absence of evidence, not evidence of landing:
    deleting the explicit guard reddens this and nothing else.
    """
    entry = _plan()["norows-dep"]
    assert entry.remaining == ("acme-norows",)
    assert entry.removed == ()
    assert still_blocking("acme-norows", _STATUSES) is True


# ---------------------------------------------------------------------------------------
# the whitelist itself
# ---------------------------------------------------------------------------------------


def test_removal_requires_every_phase_to_be_landed_not_merely_one() -> None:
    """Watches: `all` versus `any`, over the four vectors that separate them.

    This is the C3 mechanism stated as a rule rather than as one fixture: a blocker with a landed
    row and an unlanded row has not landed.
    """
    verdicts = {
        "all-landed": still_blocking(
            "x", {"x": BlockerState(phase_statuses=frozenset({RepoStatus.SUCCEEDED}))}
        ),
        "landed-plus-skipped": still_blocking(
            "x",
            {
                "x": BlockerState(
                    phase_statuses=frozenset({RepoStatus.SUCCEEDED, RepoStatus.SKIPPED})
                )
            },
        ),
        "landed-plus-pending": still_blocking(
            "x",
            {
                "x": BlockerState(
                    phase_statuses=frozenset({RepoStatus.SUCCEEDED, RepoStatus.PENDING})
                )
            },
        ),
        "none-landed": still_blocking(
            "x", {"x": BlockerState(phase_statuses=frozenset({RepoStatus.PENDING}))}
        ),
    }
    assert verdicts == {
        "all-landed": False,
        "landed-plus-skipped": True,
        "landed-plus-pending": True,
        "none-landed": True,
    }


def test_the_landed_whitelist_is_exactly_succeeded_and_every_other_status_is_retained() -> None:
    """Watches: the verdict for **every** `RepoStatus` member, derived from the enum.

    This is the whitelist as an instrument rather than as a claim. The removable set is computed
    over `RepoStatus` itself, so a member added to the enum tomorrow lands on the **retained**
    side with nobody editing an expectation — which is what "fail-closed by default" has to mean
    to be worth anything. Widening `LANDED_STATUSES` is the deliberate act, and it reddens the
    second assertion.

    Under the deny-list this replaced, this test's first assertion read
    `{PENDING, RUNNING, SUCCEEDED, BLOCKED, DEGRADED}` — five of seven, including the `BLOCKED`
    that `WaveScheduler.admit` refuses to dispatch.
    """
    removable = {
        status
        for status in RepoStatus
        if not still_blocking("x", {"x": BlockerState(phase_statuses=frozenset({status}))})
    }
    assert removable == set(LANDED_STATUSES)
    assert set(LANDED_STATUSES) == {RepoStatus.SUCCEEDED}


def test_an_unresolvable_name_is_retained_whatever_the_whitelist_says() -> None:
    """Watches: that the absent-name branch is independent of `LANDED_STATUSES`.

    Widening the whitelist to every status must still not make an unresolvable name removable --
    the two fail-closed reasons are separate and a future edit to one must not silently take the
    other with it.
    """
    assert still_blocking("nobody", {}) is True
    assert still_blocking("nobody", dict(_STATUSES)) is True


# ---------------------------------------------------------------------------------------
# the planner's D74 properties
# ---------------------------------------------------------------------------------------


def test_the_dry_run_route_and_the_write_route_get_the_identical_tuple() -> None:
    """The success criterion, as a test rather than an aspiration.

    Watches: equality of two `plan_unblocking` results over the same inputs. One computation
    called twice; a second comprehension written beside it for the preview is D74's shape, and
    this is paired with the ordering assertion below, which pins the determinism the equality
    rests on.
    """
    dry_run = plan_unblocking(blocked_by_rows=_ROWS, blocker_statuses=_STATUSES, floors=_FLOORS)
    write = plan_unblocking(blocked_by_rows=_ROWS, blocker_statuses=_STATUSES, floors=_FLOORS)
    assert dry_run == write
    # The two routes do not read `phases` in the same order -- one is a `mode=ro` preview, the
    # other a re-read inside the write transaction -- so equality must survive a reordered input
    # or it is only asserting that a function is deterministic within one call.
    reordered = plan_unblocking(
        blocked_by_rows=tuple(reversed(_ROWS)), blocker_statuses=_STATUSES, floors=_FLOORS
    )
    assert reordered == write


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
    an operator needs in order to know why a resume did not admit it.
    """
    assert set(_plan()) == {
        "quarantined-dep",
        "abandoned-dep",
        "orphan-dep",
        "live-dep",
        "norows-dep",
    }


def test_unblocking_is_frozen_so_a_caller_cannot_edit_the_plan_it_was_handed() -> None:
    entry = _plan()["orphan-dep"]
    with pytest.raises(AttributeError):
        entry.removed = ("contract:acme.protos:1.4",)  # type: ignore[misc]


# ---------------------------------------------------------------------------------------
# ADR-0113 (§37 Blocker A) — `stub_permits_removal`, a SEPARATE predicate from `still_blocking`,
# combined at the `plan_unblocking` call site via OR-logic. `still_blocking` above is untouched by
# every line below (ADR-0113 condition 1) -- these tests exercise ONLY the new predicate and its
# threading through `plan_unblocking`, never a rewritten `still_blocking` case.
# ---------------------------------------------------------------------------------------

# A blocker abandoned at ONE phase (here, phase 2) with another phase landed -- the ANY-match
# shape `stub_permits_removal` requires (never `all`, unlike `still_blocking`): RHI at any single
# row is already the permanent dead end, so waiting for every row to read RHI would miss this case.
_RHI_BLOCKER: dict[str, BlockerState] = {
    "acme-broken": BlockerState(
        phase_statuses=frozenset({RepoStatus.SUCCEEDED, RepoStatus.REQUIRES_HUMAN_INTERVENTION})
    )
}


def test_stub_permits_removal_is_off_by_default_even_for_an_rhi_blocker() -> None:
    """`stub_blocked=False` (the default) is a hard `False` for every input -- the caller's
    explicit policy switch, never derived from the blocker's own statuses. A stub-eligible
    blocker stays retained absent the flag: no surprise removals (ADR-0113, the brief's own
    "default-off, no surprise removals" requirement).
    """
    assert stub_permits_removal("acme-broken", _RHI_BLOCKER, stub_blocked=False) is False


def test_stub_permits_removal_frees_a_blocker_carrying_any_rhi_row_when_enabled() -> None:
    """The positive case: `stub_blocked=True` and the blocker has (at least) one RHI row."""
    assert stub_permits_removal("acme-broken", _RHI_BLOCKER, stub_blocked=True) is True


@pytest.mark.parametrize(
    "status", [RepoStatus.RUNNING, RepoStatus.PENDING, RepoStatus.DEGRADED, RepoStatus.SUCCEEDED]
)
def test_stub_permits_removal_never_frees_a_blocker_that_is_not_rhi(status: RepoStatus) -> None:
    """A stub only substitutes for a repo that will NEVER produce real work -- never one still in
    flight (RUNNING/PENDING/DEGRADED) or one that landed cleanly (SUCCEEDED, `still_blocking`'s
    own job). `stub_blocked=True` alone must not free any of these.
    """
    states = {"acme-live": BlockerState(phase_statuses=frozenset({status}))}
    assert stub_permits_removal("acme-live", states, stub_blocked=True) is False


def test_stub_permits_removal_is_false_for_a_name_it_cannot_resolve() -> None:
    """An unresolvable name has no RHI row to find -- `still_blocking`'s fail-closed retention is
    the only vote that applies, and this predicate must not fail OPEN on the same input.
    """
    assert stub_permits_removal("nobody", {}, stub_blocked=True) is False


def test_stub_permits_removal_is_false_for_a_blocker_resolved_to_zero_rows() -> None:
    states = {"acme-norows": BlockerState(phase_statuses=frozenset())}
    assert stub_permits_removal("acme-norows", states, stub_blocked=True) is False


def test_plan_unblocking_ors_the_two_predicates_at_the_call_site() -> None:
    """The wiring item, not the predicate: `plan_unblocking(stub_blocked=True)` frees an
    RHI-only blocker `still_blocking` alone retains, while a repo blocked by BOTH an RHI blocker
    and a live one (`still_blocking` retains both, `stub_permits_removal` only the RHI one) stays
    blocked -- the OR is per-NAME, not "stub_blocked frees the whole repo".
    """
    live = {"acme-live": BlockerState(phase_statuses=frozenset({RepoStatus.RUNNING}))}
    statuses = {**_RHI_BLOCKER, **live}
    rows = (
        ("rhi-only-dep", ("acme-broken",)),
        ("mixed-dep", ("acme-broken", "acme-live")),
    )
    plan = {
        entry.repo_id: entry
        for entry in plan_unblocking(
            blocked_by_rows=rows, blocker_statuses=statuses, floors={}, stub_blocked=True
        )
    }
    assert plan["rhi-only-dep"].removed == ("acme-broken",)
    assert plan["rhi-only-dep"].remaining == ()
    assert plan["mixed-dep"].removed == ("acme-broken",)
    assert plan["mixed-dep"].remaining == ("acme-live",)

    # And `stub_blocked=False` (the default `plan_unblocking` call, unchanged) retains the same
    # RHI-only blocker byte-for-byte -- the default-off guarantee, driven through the planner.
    off = {
        entry.repo_id: entry
        for entry in plan_unblocking(blocked_by_rows=rows, blocker_statuses=statuses, floors={})
    }
    assert off["rhi-only-dep"].removed == ()
    assert off["rhi-only-dep"].remaining == ("acme-broken",)
