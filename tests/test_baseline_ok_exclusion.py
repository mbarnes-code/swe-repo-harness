"""§12.11's third and last disclosed gap (`docs/CRITERIA_PLAN.md`, criterion 11, "gap 3";
round VI task 54).

SPEC §12.11's literal last sentence (`docs/SPEC.md:7447`): "Repos with `baseline_ok IS NULL`
(baseline never measured, e.g. `preflight.baseline_build.enabled: false` or a pre-schema-7 run)
are excluded from the count assertion, not silently passed: the fixture run asserts the
exclusion set is empty under the shipped config." **Narrowed 2026-09-09 (ADR-0135,
`docs/DECISIONS.md`; `docs/SPEC.md`'s own dated marker at that sentence):** unsatisfiable as
literally written (an `EmptyRepo` never gets a worktree cut, so its baseline can never be
measured) -- corrected to *the exclusion set contains only repos matching one of §3.1 criterion
(c)'s five enumerated exemptions*, the same narrowing already applied to §12.9(a). Round VI
task 113 (Leg E) updates this file's own assertion to match that narrowed text (see the module
docstring below the test for what changed and why) and closes `D116`.

This file drives that assertion through the real CLI (`scan -> sequence -> transform -> build`,
`tests/test_transform_e2e.py`'s five-repo fixture, no `bazel`/`git-filter-repo` faked away by
anything this file adds) and reads `repos.baseline_ok` back out of the real SQLite database
`fleet build` wrote to. "The shipped config" means: `tests/test_transform_e2e.py`'s `FLEET_YAML`
sets `preflight.min_free_bytes` and nothing else under `preflight:` -- no
`baseline_build.enabled: false` anywhere in the fixture's `config/fleet.yaml`, so
`BaselineBuild.enabled`'s pydantic default (`settings.py:282`, `True`) governs, exactly as it
would for an operator who never wrote a `baseline_build:` block at all.

**Round VI task 113 (Leg E), what changed and why.** D116's original xfail asserted
`_baseline_ok_exclusion_set(fleet) == []` -- the LITERAL, unnarrowed sentence. Two things were
true by round VI task 111 (Leg C): (1) Leg B/C together now write a real, non-NULL `baseline_ok`
for every non-empty fixture repo (3 red via `BaselineRed`, 1 green), leaving only `acme-empty`
NULL; and (2) ADR-0135 had already narrowed the criterion's text so `acme-empty`'s NULL is
EXPECTED and CORRECT (it is `EmptyRepo`-exempt), not a defect the assertion should fail on. The
literal `== []` assertion was therefore stale relative to the criterion it was meant to prove --
it would have kept failing forever on a fixture that can never satisfy it by construction (ADR-
0135's own point). The corrected assertion checks the NARROWED claim instead, against REAL
production code rather than a re-implementation of §3.1(c)'s exemption rules: every
`baseline_ok IS NULL` repo must be absent from a real `fleet sequence`'s `wave_index_by_repo` (and
present in its `excluded` list) -- i.e. genuinely exempted by `graph.sequence.check_criteria`,
the same mechanism `tests/test_baseline_scan_e2e.py`'s own Leg C test already trusts for this
exact purpose. A `sequence` exit code of `SUCCESS` is only reachable if `_exemptions_for`
genuinely recognized every repo absent from the wave, so this ties the null set directly to a
real, already-verified exemption computation instead of asserting an exact literal list that
would go stale the moment the fixture fleet's exemption categories change for unrelated reasons.

**Round VI task 113 (Leg E), the strengthened fixture (Step 2, research-53's own explicit
requirement).** Before this task, every one of the four non-empty fixture repos had ZERO native
test files, so `baseline_test_count` was 0 for all of them -- including `acme-lib-py`, the one
genuinely GREEN repo. A baseline mechanism that always answers "green, 0 tests" would pass this
file's whole proof vacuously: the exact "green-and-empty" failure §12.11 exists to catch, one
level up, silently unexercised. `_add_real_native_test` (below) adds one real, pytest-
discoverable test file directly to THIS test's own clone of `acme-lib-py`'s source repo --
never to the shared `tests/test_scan_e2e.FIXTURE_REPOS` dict, which round VI task 111's own report
measured is referenced 251 times across 11 other test files (`test_pr_e2e.py`,
`test_hoist_rollback_wiring.py`, `test_stub_resolution_task79.py`, `test_cli.py`,
`test_workers_build.py`, `test_baseline_container.py`, `test_heavy_tier_outage_e2e.py`, among
others), several of which assert exact per-repo file/commit counts derived from that dict
(`test_transform_e2e.py::test_transform_lands_one_trailered_commit_per_task`'s
`len(FIXTURE_REPOS[repo_id])`). Editing the shared dict would risk regressing all of them for a
proof that only this file needs; adding a second, real git commit to THIS test's own
`tmp_path`-scoped clone (the exact path `tests.test_transform_e2e.fleet`'s own `_make_repo` call
built it at) reaches an identical real repo with zero risk to any other test's own, entirely
separate clone of the same fixture SPEC. `acme-lib-py` was chosen because it is the one repo that
is genuinely green today (Leg B's own `pip install -e .`/`pytest` probe already passes for it,
via pytest's exit-5 "no tests collected" case) -- the added test makes that green result
GENUINELY test-carrying (pytest now exits 0, `workers/baseline.py::_classify`'s own documented
"exit 0 -> count 1" branch) rather than accidentally vacuous.

For the broken-build (`baseline_ok = 0`) side, this file relies on the EXISTING, structural
native-build/test failures round VI task 111's own report measured and disclosed (not a new,
deliberately-broken fixture repo): `acme-app-py`/`acme-app-ts` each declare a dependency on a
fictional, never-published cross-repo package name (`acme-lib-py`/`@acme/lib` in their OWN
manifests' dependency lists, deliberately internal-only so `test_transform_e2e.py`'s cross-repo
edge inference has something to detect) that cannot resolve against the real PyPI/npm registries,
and `acme-lib-ts` declares no `"test"` script at all (`workers/baseline.py`'s own disclosed,
named misclassification of that specific case as a failure rather than "no native tests"). These
are structural, reproducible properties of how the fixture repos declare cross-repo dependencies
-- not flakes -- and are cheaper and more maintainable than adding a fourth kind of fixture repo
whose only job would be to fail on command. The test below names these THREE repos explicitly
(not "at least one fails"), so a future fix to the cross-repo package-name issue would fail this
assertion loudly rather than silently making the proof vacuous again.

**Finding, historically (D116, now closed):** the assertion was false for a long time because no
production code path ever measured a native baseline or wrote `baseline_ok`/
`baseline_test_count` at all. Filed as `D116` (`docs/INTEGRATION_HONESTY.md`); closed by Legs
B/C (round VI tasks 107/111) and this file's own corrected assertion (round VI task 113).
"""

from __future__ import annotations

import json
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
from tests.test_scan_e2e import _git, scan, sequence
from tests.test_transform_e2e import fleet, query, transform  # noqa: F401  (`fleet` is a fixture)


@pytest.fixture
def baseline_build_yaml() -> str:
    """**Round VI task 111 fix round.** `tests/conftest.py`'s own session-wide `baseline_build_yaml`
    fixture now disables `preflight.baseline_build` BY DEFAULT for every e2e fixture fleet in this
    suite (`BASELINE_BUILD_DISABLED_YAML`, that fixture's own docstring explains why: most shared
    fixture repos across this suite were never vetted to succeed under a REAL native build, and
    §12.11 Leg C's red-path gate turns their pre-existing failures into a real `SKIPPED`,
    corrupting assertions that have nothing to do with baseline behavior).

    THIS file's entire premise is the opposite: proving §12.11's "the exclusion set is empty
    under the SHIPPED config" sentence, which requires `preflight.baseline_build` to be left
    COMPLETELY unconfigured (no override at all) so `BaselineBuild.enabled`'s pydantic default
    genuinely governs -- exactly what the module docstring above calls "the shipped config".
    Overriding this fixture locally (pytest resolves a fixture request against the CALLING test
    module's own fixture registry first, before falling back to `conftest.py`) is what lets this
    file keep that opposite requirement without forking `tests.test_transform_e2e.fleet`'s
    otherwise-identical setup (git repos, engine module, rules).

    This reintroduces Leg C's red-path gate for this file's own two tests, exactly as it fired
    before this fix round -- measured, unaffected: both tests still pass (1 passed, 1 xfailed),
    because neither reads `phases.status` or wave membership, only `repos.baseline_ok IS NULL`,
    which is insensitive to whether the red-gated repos are excluded from the wave plan.
    """
    return ""


_REAL_NATIVE_TEST_CONTENT = '''\
"""Round VI task 113 (§12.11 Leg E): a genuine, pytest-discoverable native test, added directly
to THIS test's own clone of acme-lib-py's source repo -- see the module docstring above
(`_add_real_native_test`) for why it is not added to the shared FIXTURE_REPOS dict. Its only job
is to make `baseline_test_count > 0` genuinely expressible for a repo whose native baseline is
green, rather than every fixture repo answering "green, 0 tests" vacuously.
"""

from acme_lib_py import normalize


def test_normalize_lowercases_a_coordinate_name() -> None:
    assert normalize("ACME") == "acme"
'''


def _add_real_native_test(tmp_path: Path, repo_id: str, filename: str, content: str) -> None:
    """Adds one real, pytest-discoverable test file to `repo_id`'s OWN pre-migration source repo
    -- `tmp_path / "sources" / repo_id`, the exact path `tests.test_transform_e2e.fleet`'s own
    `_make_repo` call already built it at by the time this is called -- and commits it as a
    SECOND, real git commit on top of the `fleet` fixture's own "fixture" commit.

    Deliberately NOT a change to the shared `tests.test_scan_e2e.FIXTURE_REPOS` dict: `tmp_path`
    is unique per test function, so this reaches only the ONE real git repository THIS test's own
    `fleet` fixture invocation built -- every other test file that also builds its own repos from
    `FIXTURE_REPOS` (round VI task 111's report: 251 references across 11 files) gets its own,
    entirely separate clone and never sees this commit. `scan` (invoked afterward, inside
    `transformed()`) clones its mirror from this repo's CURRENT head at scan time, so the added
    commit is picked up exactly like any other real, pre-existing repo history would be.
    """
    repo = tmp_path / "sources" / repo_id
    (repo / filename).write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "round VI task 113: add a real native test")


def _baseline_ok_exclusion_set(root: Path) -> list[str]:
    """`repos.baseline_ok IS NULL` rows -- the set SPEC §12.11's last sentence
    (`docs/SPEC.md:7447`) requires the fixture run to prove empty under the shipped config."""
    return sorted(
        str(row[0]) for row in query(root, "SELECT repo_id FROM repos WHERE baseline_ok IS NULL")
    )


def test_the_baseline_ok_exclusion_set_is_empty_under_the_shipped_config(
    fleet: Path,  # noqa: F811
    tmp_path: Path,
    monorepo: Path,  # noqa: F811
    bazel: object,  # noqa: F811
    filter_repo: object,  # noqa: F811
) -> None:
    """SPEC §12.11's last sentence, as narrowed by ADR-0135, proven against real state: run
    `scan -> sequence -> transform -> build` through the real CLI with no
    `preflight.baseline_build.enabled: false` override anywhere in the fixture config (i.e. under
    "the shipped config"), then assert -- against the real database AND a real second `fleet
    sequence` invocation -- that every repo whose `baseline_ok` is NULL is genuinely exempted
    from the wave plan, and that the mechanism producing that state is not vacuous (a real green
    repo with real, nonzero native tests; three named repos with a real, structural native-build
    failure). See the module docstring for the full "what changed and why".
    """
    _add_real_native_test(tmp_path, "acme-lib-py", "test_normalize.py", _REAL_NATIVE_TEST_CONTENT)

    # Driven manually (not via `transformed()`/`scanned()`) so this test can capture `fleet
    # sequence`'s own JSON payload BEFORE `transform` moves repos past Phase 1 -- a real
    # `fleet sequence` invoked again AFTER transform has landed is correctly REFUSED
    # (`SEQUENCE_REFUSED`, "N repo(s) in flight"): resequencing a repo whose Phase 2 already
    # succeeded is not a legal request, and this test does not need it to be -- the JSON payload
    # from Phase 1's own sequencing is all that's needed here.
    assert scan(fleet).exit_code == ExitCode.SUCCESS

    # ADR-0135's narrowed claim, checked against REAL production code (`graph.sequence.
    # check_criteria`, via a real `fleet sequence`) rather than a local re-implementation of
    # §3.1(c)'s exemption rules: a repo absent from `wave_index_by_repo` (and present in
    # `excluded`) is exactly what (c) already verified matches one of the five enumerated
    # exemptions -- a `sequence` exit code of SUCCESS is only reachable if `_exemptions_for`
    # genuinely recognized every excluded repo.
    seq_result = sequence(fleet)
    assert seq_result.exit_code == ExitCode.SUCCESS, seq_result.output
    seq_payload = json.loads(seq_result.output)

    assert transform(fleet).exit_code == ExitCode.SUCCESS
    result = build(fleet, "--no-sandbox")
    assert result.exit_code == ExitCode.SUCCESS, result.output

    nulls = _baseline_ok_exclusion_set(fleet)

    still_in_wave = sorted(r for r in nulls if r in seq_payload["wave_index_by_repo"])
    assert not still_in_wave, (
        f"baseline_ok IS NULL for {still_in_wave} even though {still_in_wave} still participate "
        "in the wave plan (not excluded via any §3.1(c) exemption) -- ADR-0135's narrowed 12.11 "
        "sentence requires every baseline_ok-NULL repo to be exempt, not merely present (D116)"
    )
    not_excluded = sorted(r for r in nulls if r not in seq_payload["excluded"])
    assert not not_excluded, (nulls, seq_payload["excluded"])

    # Step 2's expressibility requirement (research-53 / CLAUDE.md Guardrail 6): the mechanism
    # this assertion trusts must be able to report BOTH a genuine pass-with-real-tests and a
    # genuine failure -- never "green, 0 tests" for everyone, which would pass the checks above
    # vacuously. acme-lib-py: real native test added above by THIS test, never gated --
    # genuinely baseline_ok=1 with a nonzero test count.
    baseline_ok = dict(
        query(
            fleet,
            "SELECT repo_id, baseline_ok FROM repos WHERE repo_id IN "
            "('acme-lib-py', 'acme-app-py', 'acme-app-ts', 'acme-lib-ts')",
        )
    )
    test_count = dict(
        query(
            fleet,
            "SELECT repo_id, baseline_test_count FROM repos WHERE repo_id = 'acme-lib-py'",
        )
    )
    assert baseline_ok["acme-lib-py"] == 1, baseline_ok
    assert test_count["acme-lib-py"] > 0, (
        f"baseline_test_count={test_count.get('acme-lib-py')} for acme-lib-py -- the real native "
        "test this test added should have made pytest exit 0 (count 1), not exit 5 (count 0); "
        "see workers/baseline.py::_classify"
    )

    # The broken-build side: three SPECIFIC, named repos (not "at least one"), so a future fix to
    # the cross-repo package-name issue that accidentally resolves these dependencies would fail
    # this assertion loudly rather than silently making the proof vacuous again. See the module
    # docstring for why these three are structural, not flaky.
    assert baseline_ok["acme-app-py"] == 0, baseline_ok
    assert baseline_ok["acme-app-ts"] == 0, baseline_ok
    assert baseline_ok["acme-lib-ts"] == 0, baseline_ok


def test_the_exclusion_set_assertion_discriminates_real_db_state_and_is_not_a_tautology(
    fleet: Path,  # noqa: F811
    monorepo: Path,  # noqa: F811
    bazel: object,  # noqa: F811
    filter_repo: object,  # noqa: F811
) -> None:
    """Mutation proof for the assertion above (CLAUDE.md Rule 12: "a genuine assertion against
    real DB state ... not a mock").

    The task brief's literal recipe -- "seed one fixture repo with `baseline_ok = NULL` directly
    (bypassing normal flow) and confirm your test goes red; then remove the seed and confirm
    green" -- assumes an UNMODIFIED pipeline run is already green. It is not fully: D116's own
    original xfail (now closed, see module docstring) meant every repo was NULL with no seed
    needed; today, after Legs B/C, only `acme-empty` is NULL. So this test proves
    `_baseline_ok_exclusion_set` is a real, non-tautological read of `repos.baseline_ok` by
    driving the SAME real database in both directions:

    1. the real, unmodified post-build state has exactly `acme-empty` NULL -- reproducing the
       measured state the closure test above depends on, from a second, independent assertion
       path;
    2. writing a real non-NULL `baseline_ok` to every row directly (bypassing normal flow --
       standing in for what the write path already produces) flips the SAME assertion to an empty
       set;
    3. nulling exactly one row back out (the brief's literal seed) reproduces a single-repo
       non-empty result, proving the assertion is sensitive to WHICH rows are NULL, not merely to
       whether any `UPDATE` ran at all.

    A helper that always returned `[]` (a mock/tautology) would pass step 2 by accident but could
    never fail step 3; a helper that always returned every repo id would pass step 3 by accident
    but could never pass step 2. Only a genuine `SELECT ... WHERE baseline_ok IS NULL` passes all
    three.
    """
    transformed(fleet)
    result = build(fleet, "--no-sandbox")
    assert result.exit_code == ExitCode.SUCCESS, result.output

    all_repo_ids = sorted(str(row[0]) for row in query(fleet, "SELECT repo_id FROM repos"))
    assert all_repo_ids, "fixture produced no repos rows -- this proof needs at least one"

    db = fleet / "state" / "fleet.db"

    # 1. Real, unmodified state, AS OF round VI task 111 (§12.11/D116 Leg C): `workers/
    # baseline.py` measures 4 of this fixture's 5 repos for real (a real, networked container
    # build/test -- `settings.py::BaselineBuild.container_image`'s own docstring). Only
    # `acme-empty` stays NULL: `cli._primary_ecosystem` returns `None` for it (no manifest ever
    # publishes a coordinate for a repo with no commit at all), so `BaselineWorker.run()` takes
    # its documented "nothing to measure" skip -- exactly ADR-0135 ruling 2's target shape. This
    # test does NOT add the real native test file the closure test above adds (that is this
    # file's own fixture strengthening, scoped to that one test); `acme-lib-py`'s
    # `baseline_test_count` is 0 here, which is irrelevant to this test's own claim (it never
    # reads that column).
    assert _baseline_ok_exclusion_set(fleet) == ["acme-empty"], (
        "expected only acme-empty NULL in the real, unmodified post-Leg-C state -- if this "
        "changed, re-check both this comment and the closure test above, not just this assertion"
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
