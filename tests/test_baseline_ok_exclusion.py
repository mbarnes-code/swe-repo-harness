"""§12.11's third and last disclosed gap (`docs/CRITERIA_PLAN.md`, criterion 11, "gap 3";
round VI task 54).

SPEC §12.11's literal last sentence (`docs/SPEC.md:7447`): "Repos with `baseline_ok IS NULL`
(baseline never measured, e.g. `preflight.baseline_build.enabled: false` or a pre-schema-7 run)
are excluded from the count assertion, not silently passed: the fixture run asserts the
exclusion set is empty under the shipped config."

This file drives that literal assertion through the real CLI (`scan -> sequence -> transform ->
build`, `tests/test_transform_e2e.py`'s five-repo fixture, no `bazel`/`git-filter-repo` faked
away by anything this file adds) and reads `repos.baseline_ok` back out of the real SQLite
database `fleet build` wrote to. "The shipped config" means: `tests/test_transform_e2e.py`'s
`FLEET_YAML` sets `preflight.min_free_bytes` and nothing else under `preflight:` — no
`baseline_build.enabled: false` anywhere in the fixture's `config/fleet.yaml`, so
`BaselineBuild.enabled`'s pydantic default (`settings.py:282`, `True`) governs, exactly as it
would for an operator who never wrote a `baseline_build:` block at all.

**Finding, not a passing test:** the assertion is false today, and weakening it to pass would be
the exact failure mode the task brief and CLAUDE.md's Rule 12 warn against ("do not force the
assertion to pass by weakening it"). `grep -rn "baseline_ok" src/fleet/ src/fleet/**/*.py` (and,
form-agnostically, every `UPDATE repos SET` / `INSERT INTO repos` site in `src/fleet/cli.py`) has
zero site that ever writes `repos.baseline_ok` or `repos.baseline_test_count` — the column is
read in several places (`_RepoFacts.baseline_ok`, `cli.py:7368`'s `SELECT`,
`sequence._exemptions_for`) and written nowhere. `settings.py:282`'s `BaselineBuild.enabled`
field itself is read nowhere outside `settings.py` (`grep -rn "baseline_build" src/fleet/`
returns only its own declaration at `settings.py:295`). So under the shipped default config, no
production code path ever measures a native baseline or writes `baseline_ok`/
`baseline_test_count` at all — every repo's `baseline_ok` stays SQL NULL for the life of a run,
regardless of how far the pipeline is driven. §12.11's "the exclusion set is empty" sentence is
therefore false for the shipped default config as it exists today: the exclusion set is the
WHOLE fleet, not the empty set the sentence requires. Filed as `D116`
(`docs/INTEGRATION_HONESTY.md`).

No prior test covered this (verified before this file was added):
`grep -rn "exclusion set" tests/` returned nothing.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from fleet.cli import ExitCode
from tests.test_build_e2e import (  # noqa: F401  (`bazel`/`filter_repo`/etc. are fixtures, used
    bazel,  # by injection -- `gazelle`/`resolver` are transitive deps of
    build,  # the `bazel` fixture and must be importable here too)
    filter_repo,
    gazelle,
    monorepo,
    resolver,
    transformed,
)
from tests.test_transform_e2e import fleet, query  # noqa: F401  (`fleet` is a fixture)


def _baseline_ok_exclusion_set(root: Path) -> list[str]:
    """`repos.baseline_ok IS NULL` rows -- the set SPEC §12.11's last sentence
    (`docs/SPEC.md:7447`) requires the fixture run to prove empty under the shipped config."""
    return sorted(
        str(row[0]) for row in query(root, "SELECT repo_id FROM repos WHERE baseline_ok IS NULL")
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "D116 (docs/INTEGRATION_HONESTY.md): nothing in src/fleet/ ever writes "
        "repos.baseline_ok or repos.baseline_test_count -- BaselineBuild.enabled "
        "(settings.py:282) is read nowhere outside settings.py, so a real fleet build under the "
        "shipped default config (enabled=True, no config/*.yaml override) leaves EVERY repo's "
        "baseline_ok NULL. strict=True pins the target state SPEC 12.11's last sentence "
        "describes -- deleting this xfail (never loosening it) is how the eventual fix proves "
        "itself."
    ),
)
def test_the_baseline_ok_exclusion_set_is_empty_under_the_shipped_config(
    fleet: Path,  # noqa: F811
    monorepo: Path,  # noqa: F811
    bazel: object,  # noqa: F811
    filter_repo: object,  # noqa: F811
) -> None:
    """SPEC §12.11's literal last sentence, proven (or, today, disproven) against real state: run
    `scan -> sequence -> transform -> build` through the real CLI with no
    `preflight.baseline_build.enabled: false` override anywhere in the fixture config (i.e. under
    "the shipped config"), then assert directly against the database that no repo's
    `baseline_ok` is NULL. This is the literal SPEC sentence, not a paraphrase of it -- see the
    module docstring for why it currently fails.
    """
    transformed(fleet)
    result = build(fleet, "--no-sandbox")
    assert result.exit_code == ExitCode.SUCCESS, result.output

    nulls = _baseline_ok_exclusion_set(fleet)
    assert nulls == [], (
        f"baseline_ok IS NULL for {nulls} after a real build under the shipped default config "
        "-- SPEC 12.11's exclusion set is not empty (see D116)"
    )


def test_the_exclusion_set_assertion_discriminates_real_db_state_and_is_not_a_tautology(
    fleet: Path,  # noqa: F811
    monorepo: Path,  # noqa: F811
    bazel: object,  # noqa: F811
    filter_repo: object,  # noqa: F811
) -> None:
    """Mutation proof for the xfail above (CLAUDE.md Rule 12: "a genuine assertion against real
    DB state ... not a mock").

    The task brief's literal recipe -- "seed one fixture repo with `baseline_ok = NULL` directly
    (bypassing normal flow) and confirm your test goes red; then remove the seed and confirm
    green" -- assumes an UNMODIFIED pipeline run is already green. It is not: D116 (see the xfail
    test above) means every repo is NULL already, with no seed needed. So this test proves
    `_baseline_ok_exclusion_set` is a real, non-tautological read of `repos.baseline_ok` by
    driving the SAME real database in both directions:

    1. the real, unmodified post-build state is RED (every repo's `baseline_ok` is NULL) --
       reproducing the xfail above from a second, independent assertion path;
    2. writing a real non-NULL `baseline_ok` to every row directly (bypassing normal flow --
       standing in for what the still-unbuilt write path would eventually produce) flips the SAME
       assertion to GREEN;
    3. nulling exactly one row back out (the brief's literal seed) reproduces a single-repo RED,
       proving the assertion is sensitive to WHICH rows are NULL, not merely to whether any
       `UPDATE` ran at all.

    A helper that always returned `[]` (a mock/tautology) would pass step 1 by accident but could
    never fail step 3; a helper that always returned every repo id would pass step 1 and 3 by
    accident but could never pass step 2. Only a genuine `SELECT ... WHERE baseline_ok IS NULL`
    passes all three.
    """
    transformed(fleet)
    result = build(fleet, "--no-sandbox")
    assert result.exit_code == ExitCode.SUCCESS, result.output

    all_repo_ids = sorted(str(row[0]) for row in query(fleet, "SELECT repo_id FROM repos"))
    assert all_repo_ids, "fixture produced no repos rows -- this proof needs at least one"

    db = fleet / "state" / "fleet.db"

    # 1. Real, unmodified state, AS OF round VI task 107 (§12.11/D116 Leg B): `workers/
    # baseline.py` now measures 4 of this fixture's 5 repos for real (a fast, placeholder-image
    # container refusal under the shipped config -- `settings.py::BaselineBuild.container_image`'s
    # own docstring -- still a real, non-fabricated `baseline_ok=0`, not a mock). Only
    # `acme-empty` stays NULL: `cli._primary_ecosystem` returns `None` for it (no manifest ever
    # publishes a coordinate for a repo with no commit at all), so `BaselineWorker.run()` takes
    # its documented "nothing to measure" skip -- exactly ADR-0135 ruling 2's target shape, and
    # exactly the scenario this test's own comment already anticipated ("if this changed, D116
    # may already be fixed"). This is NOT Leg E's closure (the `strict=True` xfail immediately
    # above is untouched and still correctly XFAILs on its own, narrower-but-still-false premise
    # -- the exclusion set is no longer the WHOLE fleet, but it is still not EMPTY). This
    # assertion only needed to stop hard-coding the pre-Leg-B premise it was testing against.
    assert _baseline_ok_exclusion_set(fleet) == ["acme-empty"], (
        "expected only acme-empty NULL in the real, unmodified post-Leg-B state -- if this "
        "changed, re-check both this comment and the xfail test above, not just this assertion"
    )

    # 2. Bypass normal flow: write a real non-NULL value to every row (never mocked -- a real
    # write through a real sqlite3 connection against the same database file the CLI wrote).
    conn = sqlite3.connect(db)
    try:
        conn.execute("UPDATE repos SET baseline_ok = 1")
        conn.commit()
    finally:
        conn.close()
    assert _baseline_ok_exclusion_set(fleet) == [], (
        "assertion stayed non-empty after every row was made non-NULL -- not a real DB read"
    )

    # 3. Seed exactly one row back to NULL (the brief's literal recipe) and confirm red again.
    seeded = all_repo_ids[0]
    conn = sqlite3.connect(db)
    try:
        conn.execute("UPDATE repos SET baseline_ok = NULL WHERE repo_id = ?", (seeded,))
        conn.commit()
    finally:
        conn.close()
    assert _baseline_ok_exclusion_set(fleet) == [seeded], (
        "assertion did not go red for the single seeded NULL row -- not a real DB read"
    )
