"""§12 item 45 — "No code state is persisted outside Git" (ADR-0024), the fixture-run sub-clauses.

`docs/CRITERIA_PLAN.md` (search `## 45.`) named 5 of item 45's 7 sub-clauses as still uncovered.
This file closes 3 of those 5, all sharing a single "real fixture run has completed" state, and
all driven through the real CLI over real git repositories rather than a hand-seeded database:

* the 40-hex-format half of clause (i)'s enumeration — `docs/SPEC.md` §12 item 45(i) names the
  five git-SHA-shaped columns (`repos.head_sha`, `phases.pre_commit_sha`/`post_commit_sha`,
  `tasks.pre_commit_sha`, `attempts.commit_sha`) and requires each non-NULL value be a real,
  resolvable commit. A sibling task (`tests/test_migrations.py`, disjoint file, not touched here)
  proves *resolvability* (`git cat-file -e <sha>^{commit}`) against a hand-seeded database; this
  file proves the *format* half — every non-NULL value in each of the five columns matches
  `[0-9a-f]{40}` — against a REAL pipeline's output, which is a distinct check (a resolvable ref
  need not be 40 lowercase hex characters; a 40-hex string need not resolve).
* clause (iii)'s sufficiency claim — deleting `artifacts/` entirely and re-running `fleet resume`
  reproduces the same `SUCCEEDED` repo set and the same `run_digest`, proving `artifacts/` is a
  derived export and not part of the state a resume needs.
* clause (iii)'s six-trailer claim — a real commit on a `migrate/<repo>` branch, parsed with the
  SPEC-literal mechanism (`git interpret-trailers --parse`, not the `git log --format=` mechanism
  every other trailer test in this tree uses), carries all six `Fleet-*` trailers.

**The shared fixture.** `tests/test_transform_e2e.py` defines the `fleet` pytest fixture (five
real local git repos, a config bundle, a fresh empty DB) and the `scanned`/`transform` driver
functions; `_primed(fleet)` below is this file's one call site for "drive `scan` → `sequence` →
`transform` to completion" and every test function calls it, so there is exactly one code path
that produces the "completed fixture run" state the three tests below share.

**Known limitation, carried forward from the round-BB brief rather than worked around.**
`artifacts/` is not fully implemented in production code today: `grep -rn "artifacts"
src/fleet/*.py` shows only `artifacts/logs` is ever named, by the Phase 3 (`build`) and Phase 4
(`verify`) payload builders — Phase 2 (`transform`), which is as far as `_primed` drives the
fixture, writes nothing under `artifacts/` at all. So the delete-then-resume test below
manufactures a stand-in `artifacts/logs/` file before deleting it — there is nothing there yet to
delete in real output at this phase — and states so plainly rather than implying
`artifacts/diffs/`/`/plans/`/`/graph/`/`/build/` are being exercised. The property under test (a
resume does not need `artifacts/` to reproduce its result) does not depend on which phase
populated it.

**A finding this file's implementation surfaced independently, and — corrected before merge by
task review — is already tracked as `docs/INTEGRATION_HONESTY.md`'s D91, not a new gap.**
Investigating clause (i)'s format check found `tasks.pre_commit_sha` is NEVER written by any code
path in `src/fleet` — `record_task_anchor()` (`src/fleet/vcs/commits.py`) computes the value and
`workers/rewrite.py:182` uses it only locally, to seed a same-task rollback anchor on a failed
`apply_and_commit`; no `UPDATE tasks ... SET pre_commit_sha = ...` exists anywhere in
`src/fleet/state/repository.py` or `src/fleet/cli.py`. This contradicts
`src/fleet/workers/rewrite.py:164`'s and `src/fleet/cli.py:12026`'s own comments, which describe it
as "written in the same transaction that moves the task row to RUNNING" (§3.2 step 6.5). **This
gap was already OPEN as D91 at this task's own base commit** — task review traced the same code
independently and found D91 both pre-existing and more precise (it names the exact two
`if task_anchor is None:` consumer sites, `cli.py:12244`/`:12283`, and narrows the consequence to
"the discard path hangs `RUNNING` forever," not the broader claim this file's own earlier draft
made). No new D-number is warranted; cite D91 for the underlying defect. Against a real, successful
fixture run the column is NULL on every row, so the 40-hex format check is *vacuously* true for
it — the test below says so at the assertion site rather than silently reporting a pass that
covers nothing, and does not require the column to be non-empty the way the other four are
required to be (see `_NEEDS_NONEMPTY` below).

**Correction to the round-BB brief's citation for `run_digest`.** The brief pointed at
`cli.py:10481-10545` and called it `fleet resume --digest`. Reading that function (and `fleet
resume`'s own option list, `cli.py:11029` onward) shows `--digest` is a `fleet status` option
(`cli.py:10481`, `_status_once`) — `fleet resume` has no `--digest` flag at all. This file uses
`fleet status --digest` (`--json` for a parseable payload), which is the function the brief's line
range actually names.

**Fix-wave-1 addition (F1, round BB final-review response).** Clause (ii)'s content scan was
already covered — by `tests/test_migrations.py`'s hand-seeded, 5-row fixture, touching only 5 of
22 tables. This file adds a fourth test that reuses the same column-enumeration helper and
self-validation shape against `_primed`'s REAL completed pipeline run instead, closing the
coverage gap the hand-seeded fixture left. It does not close a new sub-clause — clause (ii) was
already counted as covered before this addition — it strengthens an existing one from a
hand-seeded stand-in to real production output.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from fleet.cli import app
from tests.test_migrations import _HUNK_HEADER_LIKE, _all_text_blob_columns
from tests.test_transform_e2e import (  # noqa: F401  (`fleet` is a fixture, used by injection)
    DESTINATIONS,
    anchor_of,
    base_args,
    fleet,
    query,
    scanned,
    transform,
    worktree,
)

runner = CliRunner()

_SHA_RE = re.compile(r"[0-9a-f]{40}")

#: (table, column) for the five git-SHA-shaped columns §12 item 45(i) names.
_SHA_COLUMNS: tuple[tuple[str, str], ...] = (
    ("repos", "head_sha"),
    ("phases", "pre_commit_sha"),
    ("phases", "post_commit_sha"),
    ("tasks", "pre_commit_sha"),
    ("attempts", "commit_sha"),
)

#: `tasks.pre_commit_sha` is excluded — see the module docstring's disclosed finding: it is never
#: written by any code path today, so a real fixture run leaves it NULL on every row and a
#: non-empty assertion on it would fail for a reason unrelated to this test's own subject.
_TASKS_PRE_COMMIT_SHA: tuple[str, str] = ("tasks", "pre_commit_sha")
_NEEDS_NONEMPTY: frozenset[tuple[str, str]] = frozenset(_SHA_COLUMNS) - {_TASKS_PRE_COMMIT_SHA}

#: The six trailers every harness commit carries (`src/fleet/vcs/commits.py`'s `FleetTrailers`).
_ALL_SIX_TRAILERS: frozenset[str] = frozenset(
    {
        "Fleet-Run-Id",
        "Fleet-Repo-Id",
        "Fleet-Phase",
        "Fleet-Task-Id",
        "Fleet-Attempt",
        "Fleet-Patch-Id",
    }
)


def _primed(fleet_root: Path) -> None:
    """`scan` → `sequence` → `transform`, to completion, over the real fixture repos.

    The one call site every test in this file shares — see the module docstring's "shared
    fixture" paragraph.
    """
    scanned(fleet_root)
    result = transform(fleet_root)
    assert result.exit_code == 0, result.output


def _status_json(fleet_root: Path, *extra: str) -> dict[str, Any]:
    result = runner.invoke(
        app, [*base_args(fleet_root), "--json", "status", *extra], catch_exceptions=False
    )
    assert result.exit_code == 0, result.output
    return dict(json.loads(result.stdout))


def _succeeded_repos(fleet_root: Path) -> frozenset[str]:
    """The `SUCCEEDED` repo set §12 item 45(iii) compares before and after the deletion."""
    return frozenset(
        row["repo"] for row in _status_json(fleet_root)["repos"] if row["status"] == "SUCCEEDED"
    )


def _run_digest(fleet_root: Path) -> str:
    """The §11.6 `run_digest`, via `fleet status --digest` (see the module docstring's citation
    correction — `fleet resume` carries no `--digest` flag)."""
    return str(_status_json(fleet_root, "--digest")["digest"])


def _commit_message(worktree_path: Path, sha: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(worktree_path), "log", "-1", "--format=%B", sha],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _parsed_trailers(message: str) -> dict[str, str]:
    """§12 item 45(iii)'s literal mechanism: `git interpret-trailers --parse`.

    Distinct from every existing trailer test in this tree (`commits_on_branch` in
    `tests/test_transform_e2e.py`, and every `git log --format=` trailer read elsewhere), which
    all use `%(trailers:key=...)` — a different, looser-but-equivalent mechanism the SPEC text
    does not name for this clause.
    """
    parsed = subprocess.run(
        ["git", "interpret-trailers", "--parse"],  # noqa: S607
        input=message,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    trailers: dict[str, str] = {}
    for line in parsed.splitlines():
        if not line.strip():
            continue
        key, sep, value = line.partition(":")
        if sep:
            trailers[key.strip()] = value.strip()
    return trailers


# ---------------------------------------------------------------------------------------
# §12 item 45(i) — the 40-hex format half
# ---------------------------------------------------------------------------------------


def test_the_five_named_sha_columns_are_40_hex_where_populated(fleet: Path) -> None:  # noqa: F811
    """Every non-NULL value in the five git-SHA-shaped columns §12 item 45(i) names is exactly
    40 lowercase hex characters, against a REAL completed pipeline run.

    **Quantity watched:** the string shape of each column's populated values, not whether they
    resolve (the sibling task in `tests/test_migrations.py` owns resolvability, against a
    hand-seeded DB — a different check: a ref can resolve without being 40-hex, e.g. `HEAD~1` or
    an abbreviated SHA, and a string can be 40-hex without resolving to anything, e.g. a stale
    pointer after history was rewritten). Four of the five columns are additionally asserted
    non-empty, so this is not a vacuous pass over an all-NULL table; `tasks.pre_commit_sha` is the
    disclosed exception — see the module docstring.
    """
    _primed(fleet)

    for table, column in _SHA_COLUMNS:
        rows = query(fleet, f"SELECT {column} FROM {table}")  # noqa: S608 - fixed column allowlist above
        values = [value for (value,) in rows if value is not None]
        if (table, column) in _NEEDS_NONEMPTY:
            assert values, f"{table}.{column}: no non-NULL values in a completed real run"
        for value in values:
            assert _SHA_RE.fullmatch(value), f"{table}.{column} = {value!r} is not 40-hex"


# ---------------------------------------------------------------------------------------
# §12 item 45(iii) — deleting `artifacts/` changes nothing a resume needs
# ---------------------------------------------------------------------------------------


def test_deleting_artifacts_then_resuming_reproduces_the_same_succeeded_set_and_digest(
    fleet: Path,  # noqa: F811
) -> None:
    """`artifacts/` is an export, not state: delete it, `fleet resume`, and the `SUCCEEDED` repo
    set and `run_digest` are unchanged.

    **Manufactured stand-in, disclosed in the module docstring:** this fixture run only drives
    through `transform` (Phase 2), and production code writes nothing under `artifacts/` before
    Phase 3 (`build`). A `logs/` file is created here so the deletion has real, if synthetic,
    content to remove — matching what the brief calls "today: just `logs/`", the only path
    production code ever writes under `artifacts/`. The property under test — that `fleet resume`
    does not need `artifacts/` to reproduce its result — does not depend on which phase populated
    the directory.
    """
    _primed(fleet)

    before_succeeded = _succeeded_repos(fleet)
    before_digest = _run_digest(fleet)
    assert before_succeeded, "the fixture run produced no SUCCEEDED repo — nothing to compare"

    artifacts_dir = fleet / "artifacts"
    (artifacts_dir / "logs").mkdir(parents=True, exist_ok=True)
    (artifacts_dir / "logs" / "stand-in.log").write_text("synthetic build log\n", encoding="utf-8")
    assert artifacts_dir.exists()

    shutil.rmtree(artifacts_dir)
    assert not artifacts_dir.exists()

    result = runner.invoke(
        app, [*base_args(fleet), "--json", "resume", "--no-continue"], catch_exceptions=False
    )
    assert result.exit_code == 0, result.output

    after_succeeded = _succeeded_repos(fleet)
    after_digest = _run_digest(fleet)
    assert after_succeeded == before_succeeded
    assert after_digest == before_digest


# ---------------------------------------------------------------------------------------
# §12 item 45(iii) — all six `Fleet-*` trailers, on the SAME commit, via the SPEC-literal
# mechanism
# ---------------------------------------------------------------------------------------


def test_every_fixture_commit_carries_all_six_fleet_trailers_via_interpret_trailers(
    fleet: Path,  # noqa: F811
) -> None:
    """`git interpret-trailers --parse` — the SPEC-literal mechanism for §12 item 45(iii), and one
    no existing test in this tree uses (`Fleet-Task-Id`/`Fleet-Patch-Id` are read by several tests
    and `Fleet-Run-Id`/`Fleet-Repo-Id` by one each, but always via `git log`/`%(trailers:...)`, and
    `Fleet-Phase`/`Fleet-Attempt` were asserted by zero tests anywhere before this one — confirmed
    by a literal grep for each trailer name across `tests/*.py` before writing this test).

    Every commit `transform` added to every `migrate/<repo>` branch is checked, not just one — a
    strict superset of the brief's minimum ("ONE test... on that single commit"), and the form
    `docs/SPEC.md` §12 item 45(iii) itself asks for ("Every fixture commit... is asserted to carry
    all six").
    """
    _primed(fleet)

    checked = 0
    for repo_id in DESTINATIONS:
        tree = worktree(fleet, repo_id)
        # Scoped to `anchor..migrate/<repo>` — the range TRANSFORM actually added — not the whole
        # branch, which also carries the fixture repo's pre-existing (untrailered) history.
        commit_range = f"{anchor_of(fleet, repo_id)}..migrate/{repo_id}"
        shas = subprocess.run(  # noqa: S603
            ["git", "-C", str(tree), "log", "--format=%H", commit_range],  # noqa: S607
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split()
        assert shas, f"{repo_id}: {commit_range} has no commits"
        for sha in shas:
            trailers = _parsed_trailers(_commit_message(tree, sha))
            missing = _ALL_SIX_TRAILERS - trailers.keys()
            assert not missing, f"{repo_id}@{sha[:12]}: missing trailers {sorted(missing)}"
            checked += 1

    assert checked > 0, "no commit was checked — the fixture run produced nothing to assert on"


# ---------------------------------------------------------------------------------------
# §12 item 45(ii) — real-fixture strengthening of the hunk-header content scan (F1, round BB
# final-review fix wave)
# ---------------------------------------------------------------------------------------


def test_no_persisted_value_in_a_real_completed_run_contains_a_diff_hunk_header(
    fleet: Path,  # noqa: F811
) -> None:
    """§12 item 45(ii)'s content scan, driven over a REAL completed `scan`→`sequence`→`transform`
    run rather than `tests/test_migrations.py`'s hand-seeded 5-row fixture (5 of 22 tables, 47
    non-NULL TEXT/BLOB values — roughly 10% of what a real run produces: 71 rows across 15 tables,
    487 non-NULL TEXT/BLOB values across 105 distinct columns, as measured by round BB's final
    review). Does not remove or weaken the hand-seeded scan — that one stays as a cheap, fast,
    self-contained check; this closes the real-fixture coverage gap alongside it, so
    `edges`/`findings`/`manifests`/`symbols`/`coordinates`/`waves`/`wave_members`/`reservations`/
    `budget_ledger`/`repo_ledger` — the payload-carrying tables the hand-seeded fixture never
    touches — are actually swept.
    """
    _primed(fleet)
    db = fleet / "state" / "fleet.db"

    columns = _all_text_blob_columns(db)
    assert columns, "the column walk returned nothing — that is broken, not clean"
    touched_tables = {table for table, _ in columns}
    assert len(touched_tables) >= 15, (
        f"only {len(touched_tables)} tables carry TEXT/BLOB columns in a real completed run — "
        "expected at least 15 (the real-fixture coverage this test exists to prove)"
    )

    # Rule 12: prove the scan is a real discriminator BEFORE trusting a zero count on real data —
    # same shape as tests/test_migrations.py's hand-seeded version of this scan.
    poisoned = db.parent / "poisoned.db"
    shutil.copyfile(db, poisoned)
    conn = sqlite3.connect(poisoned, isolation_level=None)
    try:
        conn.execute(
            "UPDATE phases SET last_error = ?",
            ("@@ -1,4 +1,4 @@\n unrelated context\n",),
        )
        poisoned_count = conn.execute(
            'SELECT COUNT(*) FROM "phases" WHERE "last_error" LIKE ?', (_HUNK_HEADER_LIKE,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert poisoned_count > 0, "the scan failed to catch a synthetic hunk header planted in it"

    conn = sqlite3.connect(db)
    try:
        for table, column in columns:
            count = conn.execute(
                f'SELECT COUNT(*) FROM "{table}" WHERE "{column}" LIKE ?',  # noqa: S608
                (_HUNK_HEADER_LIKE,),
            ).fetchone()[0]
            assert count == 0, f"{table}.{column} contains a diff hunk header"
    finally:
        conn.close()
