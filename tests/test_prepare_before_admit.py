"""D84 — a wave whose wall clock is already spent must not mutate git before admission.

`_transform_impl`, `_verify_impl` and `_build_impl` all prepare members' git before `_run_*_wave`
composes the `WaveScheduler` that decides whether anything can be admitted at all. On a breached
wave `admit` returns `admitted=()`, every member stays `PENDING` with no attempt consumed, and the
verb exits 4 — but by then the preparation has already cut branches, phase anchors and worktrees
for work that will never run.

**What is watched, and why it is this and not a digest.** A git ref counted per member worktree
(`refs/fleet/<run>/<repo>/phase-2/base`) for TRANSFORM, the per-member worktree directory under
`work/verify/` for VERIFY, and for BUILD two independent quantities: `_plan_build` invocations per
member (the seam `cli.PLAN_BUILD_HOOK`, which sits at the head of the only function running
`worktree remove --force` / `rmtree` / `worktree prune` / `worktree add`), and the
`refs/fleet/<run>/integration/<seq>` refs on disk, of which `_wave_snapshot` cuts exactly one per
re-cut. The two are derived through genuinely different machinery — a process-local seam and git's
own ref store — and both moved by exactly one re-cut. `_prepare_repo`'s step 4 is the only creator
of that ref inside a transform (`cli.py`'s other `update_ref` for it is `fleet resume`'s step-4
anchor recreation, which this fixture never enters), and `_prepare_verify`'s `worktree add` is the
only creator of that directory. Both quantities therefore move with the ordering and cannot move
for another reason. A state digest was refused on purpose: a digest over rows that carry an
`updated_at` reports "the phase ran", not "git was mutated", which is the wrong quantity for this
defect.

**Why the breach is produced in the SAME phase each case then drives.** A cross-phase fixture
(stamp the clock in TRANSFORM, drive BUILD) is correct today and becomes a semantic no-op the day
D82's re-stamp predicate lands: the driven phase's `open_wave` re-stamps the wave, `breached` reads
`False`, and the case then passes under the exact defect it exists to catch. Each case here stamps
the clock immediately before driving that same phase, and asserts the exit-4 halt FIRST — so if a
future re-stamp does dissolve the breach, the run succeeds and this file fails loudly instead of
going quietly green.

**Per case, the mutation that reddens it** (each case is the unique discriminator of one):

* `test_a_breached_transform_wave_cuts_no_phase_anchor` — reddens under deleting (or inverting to a
  constant false) the `_wave_is_breached` guard in `_transform_impl`. The VERIFY case stays green
  under that mutation.
* `test_a_breached_verify_wave_cuts_no_worktree` — reddens under deleting the `if breached:
  continue` guard in `_verify_impl`. The TRANSFORM case stays green under that mutation.
* `test_a_breached_build_wave_re_cuts_no_worktree` — reddens under deleting the
  `not await _wave_is_breached(...)` conjunct from `_build_impl`'s `if published and snapshot is
  not None` re-cut condition. The TRANSFORM and VERIFY cases stay green under that mutation: they
  never drive a second BUILD wave.
* All three redden under `_wave_is_breached` returning a constant `False`, and all redden on the
  pre-fix tree, where neither guard exists. The pre-fix tree is the old-passes/new-fails input:
  no existing transform, verify or build test asserts on work performed before an empty
  admission, so the whole covering set behaves identically on it while these three cases fail.

**PASS 2 in `_build_impl` is a DEFERRED DEFECT — not a stated boundary — and nothing here tests
it.** `_build_impl` has a
second pre-admission git mutation — one `_wave_snapshot` and one `_plan_build` per ingested unit,
fleet-wide over `ingests.items()` and gated only by `if ingests:`, with no wave index to ask
`breached` about. It takes no guard, and that is measured rather than argued: the only predicate
that fits such a site, "every wave is breached", was implemented and run against this file's
fixture with every `waves` row stamped, and it leaves `plans` empty so
`_check_root_file_domain`'s coverage half raises `RootFileDomainDriftError` naming all four repos
— exit 1, not the exit 4 the breached-wave contract requires. It is also not in these cases'
class: PASS 2 is the only populator of `plans`, which PASS 3, PASS 4 and
`_check_root_file_domain` read regardless of admission. **Its reachability is disclosed, not
denied.** `waves` has no phase column, so TRANSFORM stamps every wave — measured non-NULL after
`fleet transform`, NULL only before any phase has run — and a build started more than
`budgets.wave_max_wallclock_s` after its transform reaches PASS 2 already breached, on a fresh
run: transform before lunch, build after. Being reachable, it is an open defect this change did
not fix, not an adversarial-only escape that may be documented and left. These cases do not cover
that residue.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from pathlib import Path

import pytest

from fleet import cli
from fleet.cli import ExitCode
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
    verify,
    verify_worktree,
)
from tests.test_transform_e2e import (  # noqa: F401  (fixtures are used by injection)
    fleet,
    git,
    query,
    scanned,
    transform,
    worktree,
)

#: Older than `budgets.wave_max_wallclock_s`, whose default is 14 400s and which this fixture's
#: config does not lower. A fixed instant rather than `now - delta`: the assertion is "the ceiling
#: is spent", and a wall clock that only just breached is the one shape a slow test host could
#: un-breach between the stamp and the read.
LONG_SPENT = "2001-01-01T00:00:00.000000+00:00"


def run_id_of(root: Path) -> str:
    rows = query(root, "SELECT run_id FROM runs")
    assert len(rows) == 1, rows
    return str(rows[0][0])


def spend_the_wave_clock(root: Path, *, wave_index: int | None = None) -> None:
    """Stamp planned waves as having started long ago — `waves.wave_started_at` is exactly what
    `WaveScheduler.breached` reads, through `elapsed_s`. Every wave by default; `wave_index`
    narrows it to one, which the BUILD case needs because its FIRST wave must publish.

    Written directly rather than through `begin_wave`, because `begin_wave` is `COALESCE`d on
    purpose (a resume must not reset the cumulative clock) and so cannot move a stamp backwards.
    That `COALESCE` is also what makes the narrowed form survive the phase it is aimed at: the
    driven phase's `open_wave` cannot overwrite the stamp this wrote.
    """
    conn = sqlite3.connect(root / "state" / "fleet.db")
    try:
        with conn:
            if wave_index is None:
                conn.execute("UPDATE waves SET wave_started_at = ?", (LONG_SPENT,))
            else:
                conn.execute(
                    "UPDATE waves SET wave_started_at = ? WHERE wave_index = ?",
                    (LONG_SPENT, wave_index),
                )
        assert conn.execute(
            "SELECT COUNT(*) FROM waves WHERE wave_started_at = ?", (LONG_SPENT,)
        ).fetchone()[0] > 0, "no wave row was stamped — the fixture would prove nothing"
    finally:
        conn.close()


def wave_members(root: Path, wave_index: int) -> list[str]:
    return [
        str(row[0])
        for row in query(
            root,
            "SELECT node_id FROM wave_members WHERE wave_index = ? AND node_kind = 'REPO' "
            " ORDER BY node_id",
            (wave_index,),
        )
    ]


def phase_anchors(root: Path, run: str, repos: list[str]) -> list[str]:
    """Every `refs/fleet/<run>/<repo>/phase-2/base` that really exists, read out of the member's
    own worktree. `for-each-ref` over a namespace that holds nothing exits 0 with no output, so
    this counts refs rather than exit codes."""
    found: list[str] = []
    for repo in repos:
        tree = worktree(root, repo)
        if not tree.exists():
            continue
        listing = git(
            tree, "for-each-ref", "--format=%(refname)", f"refs/fleet/{run}/{repo}/phase-2/"
        )
        found += [line for line in listing.splitlines() if line.strip()]
    return found


def test_a_breached_transform_wave_cuts_no_phase_anchor(fleet: Path) -> None:  # noqa: F811
    """The wave's clock is spent before `transform` is invoked, so `admit` admits nobody — and
    `_prepare_repo` must therefore never run.

    The halt is asserted before the git quantity on purpose: `admitted == 0` with the members
    still `PENDING` is the precondition that makes "0 anchors" mean "the preparation was skipped"
    rather than "the wave was never breached in the first place".
    """
    scanned(fleet)
    run = run_id_of(fleet)
    members = wave_members(fleet, 0)
    assert members, "the sequenced plan put no repo in wave 0"
    spend_the_wave_clock(fleet)

    result = transform(fleet)

    assert result.exit_code == ExitCode.WAVE_WALL_CLOCK_EXHAUSTED, result.output
    rows = dict(query(fleet, "SELECT repo_id, status FROM phases WHERE phase = 2"))
    # D123/ADR-0127 (round VI task 76): TRANSFORM now pre-seeds EVERY open wave's members upfront,
    # before any wave dispatches -- not only the driven wave's -- so `rows` also carries wave 1's
    # members here (also freshly PENDING: the breach halts the dispatch loop before wave 1 is ever
    # reached, so nothing in it advances past its own pre-seeded row either). This test's own claim
    # is about wave 0 specifically -- every wave-0 member got a phase row and none of them advanced
    # past PENDING -- not that wave 0's members are the only rows that exist.
    assert set(members) <= set(rows), rows
    assert {rows[repo_id] for repo_id in members} == {"PENDING"}, rows
    assert set(rows.values()) == {"PENDING"}, rows
    assert [
        row for row in query(fleet, "SELECT repo_id, attempts FROM phases WHERE phase = 2")
        if row[1] != 0
    ] == []

    assert phase_anchors(fleet, run, members) == []
    assert [
        row
        for row in query(fleet, "SELECT repo_id, base_ref FROM phases WHERE phase = 2")
        if row[1] is not None
    ] == []


def test_a_breached_verify_wave_cuts_no_worktree(
    fleet: Path, monorepo: Path, bazel: FakeBazel  # noqa: F811
) -> None:
    """Same class, same remedy, the other driver. The clock is spent after Phase 3 lands and
    immediately before `verify` is invoked, so the breach belongs to the phase being driven.

    `_prepare_verify` takes an integration snapshot under the mutex and then runs
    `worktree remove --force` / `rmtree` / `worktree prune` / `worktree add` per member. The
    directory it adds is the watched quantity.
    """
    transformed(fleet)
    assert build(fleet, "--no-sandbox").exit_code == ExitCode.SUCCESS
    members = wave_members(fleet, 0)
    assert members, "the sequenced plan put no repo in wave 0"
    spend_the_wave_clock(fleet)

    result = verify(fleet, "--rdeps-limit", "3")

    assert result.exit_code == ExitCode.WAVE_WALL_CLOCK_EXHAUSTED, result.output
    statuses = dict(query(fleet, "SELECT repo_id, status FROM phases WHERE phase = 4"))
    assert set(statuses.values()) <= {"PENDING"}, statuses

    assert [repo for repo in members if verify_worktree(fleet, repo).exists()] == []


def integration_refs(monorepo_path: Path, run: str) -> dict[str, str]:
    """`refs/fleet/<run>/integration/<seq>` → sha, read out of git's own ref store.

    `_wave_snapshot` cuts exactly one of these per call and never moves one afterwards, so this
    dict is a durable record of how many snapshots the run took. It is deliberately NOT read from
    the JSON payload's `integration_refs`, which is a projection of the process-local `plans` — a
    second instrument sharing the first one's blind spot would agree with it on a wrong number.
    """
    listing = git(
        monorepo_path,
        "for-each-ref",
        "--format=%(refname) %(objectname)",
        f"refs/fleet/{run}/integration/",
    )
    out: dict[str, str] = {}
    for line in listing.splitlines():
        if line.strip():
            ref, sha = line.split()
            out[ref] = sha
    return out


def test_a_breached_build_wave_re_cuts_no_worktree(
    fleet: Path,  # noqa: F811
    monorepo: Path,  # noqa: F811
    bazel: FakeBazel,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The third driver, and the only one where the mutation is a RE-cut rather than a first cut.

    `_build_impl`'s wave loop re-plans a wave's members whenever an earlier wave published, so
    their worktrees carry its packages. That branch is `_wave_snapshot` plus one `_plan_build` per
    member, and `_plan_build` runs `worktree remove --force` / `shutil.rmtree` / `worktree prune` /
    `worktree add --detach --force`. On a breached wave `admit` admits nobody, so all of it is git
    mutation for work that cannot run.

    **The breach is IN-PHASE.** Only wave 1 is stamped, and it is stamped after `transform` has
    finished and immediately before `build` is invoked, with nothing in between — so the spent
    clock belongs to the BUILD invocation that reads it. Wave 0 is left alone precisely because it
    must SUCCEED: `published` is what opens the branch under test, and a fixture that breached
    both waves could never reach it. `waves` carries no phase column today, which is exactly why
    the stamp is written for the phase being driven rather than inherited from an earlier one.

    **Expressibility, asserted and not assumed** (the four-of-twelve shape): the plan must put the
    repos in more than one wave, wave 0's members must reach SUCCEEDED, `payload["waves"]` must
    show wave 1 was actually driven, and the branch tip must have MOVED past the PASS-2 snapshot.
    Without that last one the ref instrument reads the same value under the defect and under the
    fix, and would certify the defect green.

    The halt is asserted before either git quantity, for the reason the sibling cases give: it is
    the precondition that makes "nothing was re-cut" mean "the preparation was skipped" rather
    than "the wave was never breached".
    """
    transformed(fleet)
    run = run_id_of(fleet)
    first, second = wave_members(fleet, 0), wave_members(fleet, 1)
    assert first and second, (first, second)
    spend_the_wave_clock(fleet, wave_index=1)

    planned: list[str] = []
    monkeypatch.setattr(cli, "PLAN_BUILD_HOOK", planned.append)
    result = build(fleet, "--no-sandbox")

    assert result.exit_code == ExitCode.WAVE_WALL_CLOCK_EXHAUSTED, result.output
    statuses = dict(query(fleet, "SELECT repo_id, status FROM phases WHERE phase = 3"))
    assert [repo for repo in first if statuses.get(repo) != "SUCCEEDED"] == [], statuses
    assert [repo for repo in second if statuses.get(repo) != "PENDING"] == [], statuses
    assert [
        row for row in query(fleet, "SELECT repo_id, attempts FROM phases WHERE phase = 3")
        if row[0] in second and row[1] != 0
    ] == []
    assert payload(result)["waves"] == [0, 1], payload(result)

    # First instrument: the seam at the head of the only function that mutates a worktree.
    assert [repo for repo, n in Counter(planned).items() if n > 1] == [], planned

    # Second instrument, through git's ref store instead: `_wave_snapshot` cuts a ref AT THE TIP,
    # so a re-cut after wave 0 published leaves a snapshot naming the tip. None may.
    refs = integration_refs(monorepo, run)
    tip = git(monorepo, "rev-parse", "integration").strip()
    # PASS 2's snapshot, read off the persisted `attempts` row of a wave-0 member rather than off
    # `refs` — wave 0 is the FIRST wave, so `published` is False when it is planned and the ref it
    # built from is the fleet-wide one PASS 2 cut.
    pass_2 = build_snapshot_ref(fleet, first[0])
    assert pass_2 in refs, (pass_2, refs)
    # The precondition, asserted rather than assumed: wave 0 published, so the tip is no longer
    # where PASS 2 cut. Without this the instrument reads the same value under the defect and
    # under the fix, and would certify the defect green.
    assert refs[pass_2] != tip, (
        f"the branch tip is still PASS 2's snapshot {pass_2}, so wave 0 published nothing and a "
        "re-cut would be indistinguishable from no re-cut"
    )
    assert tip not in set(refs.values()), (
        f"a snapshot ref names the branch tip {tip}, so one was cut after wave 0 published: {refs}"
    )
