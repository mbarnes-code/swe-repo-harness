"""D84 — a wave whose wall clock is already spent must not mutate git before admission.

`_transform_impl` and `_verify_impl` both prepare EVERY member's git before `_run_*_wave`
composes the `WaveScheduler` that decides whether anything can be admitted at all. On a breached
wave `admit` returns `admitted=()`, every member stays `PENDING` with no attempt consumed, and the
verb exits 4 — but by then the preparation has already cut branches, phase anchors and worktrees
for work that will never run.

**What is watched, and why it is this and not a digest.** A git ref counted per member worktree
(`refs/fleet/<run>/<repo>/phase-2/base`) for TRANSFORM, and the per-member worktree directory
under `work/verify/` for VERIFY. `_prepare_repo`'s step 4 is the only creator of that ref inside a
transform (`cli.py`'s other `update_ref` for it is `fleet resume`'s step-4 anchor recreation, which
this fixture never enters), and `_prepare_verify`'s `worktree add` is the only creator of that
directory. Both quantities therefore move with the ordering and cannot move for another reason. A
state digest was refused on purpose: a digest over rows that carry an `updated_at` reports "the
phase ran", not "git was mutated", which is the wrong quantity for this defect.

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
* Both redden under `_wave_is_breached` returning a constant `False`, and both redden on the
  pre-fix tree, where neither guard exists. The pre-fix tree is the old-passes/new-fails input:
  no existing transform or verify test asserts on work performed before an empty admission, so
  the whole covering set behaves identically on it while these two cases fail.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fleet.cli import ExitCode
from tests.test_build_e2e import (  # noqa: F401  (fixtures are used by injection)
    FakeBazel,
    bazel,
    build,
    filter_repo,
    gazelle,
    monorepo,
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


def spend_the_wave_clock(root: Path) -> None:
    """Stamp every planned wave as having started long ago — `waves.wave_started_at` is exactly
    what `WaveScheduler.breached` reads, through `elapsed_s`.

    Written directly rather than through `begin_wave`, because `begin_wave` is `COALESCE`d on
    purpose (a resume must not reset the cumulative clock) and so cannot move a stamp backwards.
    """
    conn = sqlite3.connect(root / "state" / "fleet.db")
    try:
        with conn:
            conn.execute("UPDATE waves SET wave_started_at = ?", (LONG_SPENT,))
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
    assert set(rows) == set(members), rows
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
