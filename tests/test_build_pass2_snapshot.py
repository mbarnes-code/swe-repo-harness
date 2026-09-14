"""`_build_impl` PASS 2 — previously ZERO test coverage (round VIII, §15.1 item 3, Wave 7.0).

`research-wave7-scoping-report.md` §3 disclosed this as a byproduct of a different sweep:

> **PASS 2 in `_build_impl` is a DEFERRED DEFECT — not a stated boundary — and nothing here tests
> it.** `_build_impl` has a second pre-admission git mutation — one `_wave_snapshot` and one
> `_plan_build` per ingested unit, fleet-wide over `ingests.items()` and gated only by
> `if ingests:`, with no wave index to ask `breached` about.

That disclosure is about the MISSING BREACH GUARD (D84's residue, tracked separately —
`src/fleet/cli.py`'s own comment at the PASS-2-adjacent wave-loop re-cut site, "Filed OPEN; D84 is
PARTLY ADDRESSED, not fixed"). This file does not fix or re-litigate that residue: it is out of
this batch's scope (worker-mutation-batch27-brief.md), and patching a disclosed, reachable defect
without a dispatched decision would be exactly the kind of unscoped fix CLAUDE.md's Rule 3
("touch only what you must") warns against.

What this file DOES cover, for the first time, is PASS 2's own POSITIVE contract — the behaviour
`_build_impl`'s docstring states it exists to provide (ADR-0055):

    2. PLAN every one of them from ONE snapshot cut after the last of those ingests. Every
       worktree therefore contains every `dest` in the fleet, which is ADR-0053's intra-wave
       property widened to the whole run.

and the idempotency half of the same docstring:

    Skipped entirely when this invocation drives no wave: a fleet whose every wave is settled
    must do NOTHING, which is the idempotency `fleet build` already had...

**Why these two properties and not something else.** They are PASS 2's entire reason to exist as
written (`src/fleet/cli.py:12318-12390`, "PASS 2: ONE snapshot over the whole fleet, and a plan
for every unit"): a version of PASS 2 that cut a fresh snapshot per repo would silently reintroduce
the cross-repo staleness ADR-0053/ADR-0055 close (a worktree that does not contain every `dest` in
the fleet), and a version that cut a snapshot even when nothing was ingested would turn a routine,
already-settled `fleet build` re-invocation into an unbounded git-ref generator.

**Why `build_snapshot_ref`, not a live read of `plans`.** `plans` is process-local, exactly the
quantity `_build_impl`'s own docstring names as the thing that must NOT be trusted across
invocations ("`plans` is process-local and was never rehydrated from SQLite"). `build_snapshot_ref`
(`tests/test_build_e2e.py`) reads `attempts.integration_ref` instead — the persisted row §3.3
promises a `BUILD_ERROR` is a property of — so this asserts what the run actually recorded doing,
not what one in-memory dict happened to hold.

**Wave-0 fixture choice.** `wave_members(fleet, 0)` has three independent leaves in the shared
5-repo fixture (`acme-lib-py`, `acme-lib-ts`, `acme-empty` — none declares an internal dependency,
so all three are eligible for wave 0 and none is re-planned by the wave loop's own re-cut branch,
which only fires for a NON-first wave after an earlier wave publishes). That makes wave 0 the
right place to observe PASS 2 in isolation: every wave-0 member's plan comes from PASS 2's cut and
nothing else, exactly as `tests/test_prepare_before_admit.py`'s own
`test_a_breached_build_wave_re_cuts_no_worktree` docstring notes ("wave 0 is the FIRST wave, so
`published` is False when it is planned and the ref it built from is the fleet-wide one PASS 2
cut").
"""

from __future__ import annotations

from pathlib import Path

from fleet import cli
from tests.test_build_e2e import (  # noqa: F401  (fixtures are used by injection)
    FakeBazel,
    bazel,
    build,
    build_snapshot_ref,
    filter_repo,
    gazelle,
    monorepo,
    payload,
    resolver,
    transformed,
)
from tests.test_prepare_before_admit import integration_refs, run_id_of, wave_members
from tests.test_transform_e2e import fleet  # noqa: F401  (fixture is used by injection)


def test_build_impl_pass_2_plans_every_wave_zero_unit_from_one_shared_snapshot(
    fleet: Path, monorepo: Path, bazel: FakeBazel  # noqa: F811
) -> None:
    """PASS 2 cuts ONE snapshot for the whole ingested fleet, not one per repo.

    Reddens under moving the `_wave_snapshot` call from once-before-the-loop to once-per-repo
    inside PASS 2's `for repo_id, ingested in ingests.items():` loop (`src/fleet/cli.py:12324`
    onward) — each repo would then build from a DISTINCT integration ref, and this fixture's three
    independent wave-0 leaves (`acme-lib-py`, `acme-lib-ts`, `acme-empty`) would no longer agree.
    """
    transformed(fleet)
    wave0 = wave_members(fleet, 0)
    assert len(wave0) >= 2, (
        f"need at least two independent wave-0 repos to distinguish ONE shared snapshot from "
        f"one-per-repo: {wave0}"
    )

    result = build(fleet, "--no-sandbox")
    assert result.exit_code == cli.ExitCode.SUCCESS, result.output

    refs = {repo: build_snapshot_ref(fleet, repo) for repo in wave0}
    assert len(set(refs.values())) == 1, (
        "PASS 2 must plan every wave-0 unit from the SAME fleet-wide snapshot — these wave-0 "
        f"repos built from different integration refs, so a fresh snapshot was cut per repo "
        f"instead of once for the whole ingested set: {refs}"
    )


def test_build_impl_pass_2_cuts_no_new_snapshot_when_nothing_is_ingested(
    fleet: Path, monorepo: Path, bazel: FakeBazel  # noqa: F811
) -> None:
    """PASS 2 is gated on `if ingests:` — a second, fully-settled `fleet build` invocation must
    ingest nothing and therefore cut no new `refs/fleet/<run>/integration/<seq>` ref at all.

    Reddens under deleting (or unconditionally executing past) that `if ingests:` guard so
    `_wave_snapshot` runs even when `ingests` is empty — the second invocation below would then
    cut a fresh, unused integration ref on every re-run.
    """
    transformed(fleet)
    run = run_id_of(fleet)

    first = build(fleet, "--no-sandbox")
    assert first.exit_code == cli.ExitCode.SUCCESS, first.output
    assert payload(first)["waves"] != [], "the first build drove no wave, so it proves nothing"
    before = integration_refs(monorepo, run)
    assert before, "the first build cut no snapshot ref at all, so this test proves nothing"

    second = build(fleet, "--no-sandbox")
    assert second.exit_code == cli.ExitCode.SUCCESS, second.output
    assert payload(second)["waves"] == [], (
        "the fleet is already fully built, so a second invocation must drive no wave — "
        "otherwise PASS 2 running again would be legitimate and this asserts the wrong thing"
    )

    after = integration_refs(monorepo, run)
    assert after == before, (
        "PASS 2 must do nothing when nothing was ingested — a second, idempotent `fleet build` "
        f"invocation cut new ref(s): {set(after) - set(before)}"
    )
