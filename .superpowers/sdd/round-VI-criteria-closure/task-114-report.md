# Task 114 report -- hotfix: sweep ALL literal `preflight:` blocks for the missing baseline_build override

Branch: `agent/roundvi-task114`, forked from `main` at `fe698bc`.
Commit: `1efe226`.

## Sweep method

Task 111's own sweep (grep for files REQUESTING the shared `baseline_build_yaml` conftest
fixture) structurally cannot find a `preflight:` block that is a MODULE-LEVEL STRING LITERAL
consumed by a plain function (not a pytest fixture) -- that is exactly the shape of the missed
file, `tests/test_build_e2e.py::_HOIST_INGEST_FLEET_YAML`.

My sweep instead grepped for the bare substring `preflight:` (no quote-character anchor, no
"only files that already request the fixture" filter) across every `tests/*.py` file:

    grep -rn "preflight:" tests/*.py

This is demonstrably more thorough than a quote-anchored variant: an initial attempt anchored on
`"preflight:` / `'preflight:` (a quote immediately before the token) found only 5 files and
MISSED `tests/test_build_e2e.py:7145` entirely, because that occurrence sits on its own line
inside a triple-quoted string, with no quote character adjacent to it. Dropping the quote anchor
is what surfaced it.

**This confirms my method would have caught `_HOIST_INGEST_FLEET_YAML` had I run it before this
session's Leg C work** -- the quote-anchored variant reproduces the exact miss task 111 made; the
bare-substring variant does not.

The bare sweep returned hits across 16 files. Each was individually triaged:
1. Is it inside a real config block (vs. a docstring/comment mentioning "preflight:")?
2. Does it already carry `baseline_build: enabled: false`?
3. If not: does the test using this block actually drive a real `fleet scan`/CLI pipeline over a
   native-baseline-capable (npm/PyPI) fixture repo never vetted to succeed a real native build --
   i.e. is the override load-bearing, or would it be vacuous (unreachable given this fixture's
   ecosystems -- e.g. Ruby, which has no `native_baseline()` support at all)?

## Files fixed

- **`tests/test_build_e2e.py::_HOIST_INGEST_FLEET_YAML`** (~line 7133) -- the file/site the
  review named. Module-level literal read by `_write_hoist_ingest_config`, a plain function.
  Added the literal `baseline_build:\n    enabled: false`, same inline-literal pattern already
  used by `tests/test_scan_e2e.py`'s own `FLEET_YAML` (also a plain constant, not a fixture).
- **`tests/test_cli.py`** -- TWO previously-unknown sites (identical "acme-lib"/"acme-app" real
  npm dependency-edge fixture), at `test_status_digest_is_byte_identical_across_two_clean_db_
  runs_under_a_warm_llm_cache` and `test_status_digest_differs_when_a_fixture_source_file_
  mutates_between_two_clean_db_runs`. The FIRST was independently, actually failing on `main`
  (full-file run: 205 passed / 1 failed before my fix, 206/206 after) -- confirmed by direct DB
  inspection: both repos end phase 1 `SKIPPED` with a `BaselineRed` finding, emptying
  `wave_members` and tripping this test's own anti-vacuity assertion (`wave_count > 0`). The
  SECOND currently passes (its assertions don't check wave/edge counts) but is driven by the
  same unvetted npm fixture and genuinely produces `BaselineRed`/`SKIPPED` too (verified via DB
  inspection) -- fixed for consistency with the sibling test and to remove a latent landmine.
- **`tests/test_new_language_touchpoints_e2e.py::_FLEET_YAML_CLAUSE_B`** (Clause B fixture
  only). Reuses `CYCLE_FLEET`'s real npm `acme-identity`/`acme-billing` (already overridden in
  the sibling `tests/test_workers_contracts.py`); confirmed via DB inspection both repos DO go
  `BaselineRed`/`SKIPPED` here too. The test currently still passes only because a third repo in
  this fixture (`acme-gem`, Ruby) stays non-exempt and gives `sequence` something to admit --
  fixed to remove that latent coincidence-dependence and for consistency with
  `test_workers_contracts.py`'s own override of the identical repo pair.

## Checked and left alone (with reason)

- **`tests/test_new_language_touchpoints_e2e.py::_FLEET_YAML`** (Clause A, Ruby-only) --
  deliberately NOT given the override. Its only repo is Ruby, and `RubyEcosystemAdapter` does
  not override `EcosystemAdapter.native_baseline()` (`src/fleet/ecosystems/base.py`'s default
  returns `None`; only `py.py`/`js.py` currently override it). `evidence.baseline_red` can never
  contain this repo -- the override would be vacuous by construction, not merely untriggered.
- **`tests/test_hoist_rollback_wiring.py`, `tests/test_hoist_rollback_git.py`,
  `tests/test_unhoist_rollback.py`** -- all three named as "plausible candidates" by the review,
  verified individually: none of their `preflight:` blocks are ever consumed by a test that goes
  through the CLI/`scan` pipeline (`test_hoist_rollback_wiring.py`'s own block is used only by
  Layer-1 unit tests calling `_reconcile_hoist_rollbacks` directly; its real e2e tests import
  `test_build_e2e.py`'s own `fleet` fixture, already covered by task 111; the other two files
  never invoke `CliRunner`/`app` at all). Full suites run: 26/26 passed, unmodified.
- **`tests/test_collisions_wiring.py`, `tests/test_contracts_criterion_scale.py`,
  `tests/test_workers_contracts.py`, `tests/test_sequence_e2e.py`, `tests/test_scan_e2e.py`,
  `tests/test_transform_e2e.py`** -- already covered by task 111 (confirmed present). 54/54
  passed unmodified (first five); scan/transform re-verified via the other runs below.
- **`tests/test_baseline_scan_e2e.py`, `tests/test_baseline_ok_exclusion.py`** -- deliberately
  excluded by design per `conftest.py`'s own docstring (these are where the red path/shipped
  default is supposed to be genuinely exercised). Left untouched, 54/54-passed run above
  includes both.
- **The remaining `tests/test_cli.py` `preflight:` blocks** (module-level `FLEET_YAML`,
  `CONTINUE_KNOBS_YAML`, `IMPATIENT_STALE_YAML`, `REAP_YAML`, `DISK_FLOOR_YAML`,
  `BREACHING_MEMORY_YAML`, the MemTotal-leg fixture) -- checked individually; these drive `fleet
  resume`/settings-load/disk-floor/memory-budget tests against hand-seeded DB rows or a bare
  `acme-commons` fixture, never a real `scan` over an npm/PyPI manifest. Confirmed empirically:
  full `tests/test_cli.py` run was 206/206 after adding the two real fixes above -- no other site
  in this file is currently broken by this hazard.

## Verification

- **Old-fails/new-passes** on the 3 named review tests (`test_a_hoisted_contracts_content_is_
  really_merged_with_the_trailer`, `test_a_hoisted_contracts_carrier_path_lands_on_integration_
  exactly_once`, `test_a_hoist_rollback_targets_the_real_contract_merge_not_the_owners_own_
  merge`): against a pristine `fe698bc` copy of `test_build_e2e.py`, all 3 fail with "expected
  exactly one hoist merge, found []"; against my fixed copy, all 3 pass.
- `tests/test_cli.py` full suite: 206/206 passed after (205/206 before -- the exact failure named
  above, reproduced deterministically twice before the fix, root-caused via direct sqlite
  inspection of `phases`/`findings` in the failing run's own DB).
- `tests/test_new_language_touchpoints_e2e.py` **could not be cleanly verified on this branch
  alone**: `main` moved substantially during this session (round VI tasks 115/116 merged, plus a
  further docs+test commit) and task 115's own hotfix (a `types.UnionType` bug in this same
  file, explicitly out of scope for me per the brief) is not present on my `fe698bc`-forked
  branch, so 3 of 4 tests in this file fail on my branch for a reason that has nothing to do with
  my fix and is already fixed on `main`. Confirmed my edit region is disjoint from task
  115/116's changes (`git diff fe698bc main -- tests/test_new_language_touchpoints_e2e.py`
  touches only `_substitute_ecosystem`/`_decoy_ruby_ecosystem`'s bodies, ~lines 320-430; mine is
  ~120-250). To verify cleanly I applied my 91-line diff (confirmed zero conflicts) to a
  disposable, provisioned worktree at current `main` tip (`7f6ba2d`, deleted after use) and ran
  the file there: **4/4 passed**. `test_cli.py` (206/206) and `test_build_e2e.py`'s 3 named hoist
  tests (3/3) were re-verified the same way in that same worktree for full confidence.
- `tests/test_build_e2e.py` FULL suite (on my own branch): **80 passed, 7 failed.** The 7
  failures (`test_build_against_a_real_bazel`, `test_the_unknown_ecosystem_filegroup_builds_
  under_a_real_bazel`, `test_two_js_repos_with_different_npm_dependencies_both_build`,
  `test_a_python_test_target_runs_and_passes_under_a_real_bazel`, `test_the_resolved_lock_
  carries_the_transitive_closure`, `test_re_resolving_the_same_specs_produces_a_byte_identical_
  lock`, `test_a_real_bazel_lock_publish_and_a_real_sandboxed_build_happen_in_the_same_run`) are
  **PRE-EXISTING and unrelated to this hotfix** -- confirmed two ways: (a) 2 of the 7 run against
  the pristine, unmodified `fe698bc` copy of the file fail identically there too, with the same
  "no BUILD.bazel generated"/`REQUIRES_HUMAN_INTERVENTION` symptom, not a hoist/baseline-red
  symptom; (b) the SAME 7 (name for name) fail identically on the disposable current-`main`-tip
  worktree's full-suite run too (80 passed, 7 failed, 55m38s). These are real-Bazel/real-npm-
  resolution tests, plausibly environment-dependent (registry reachability) in this sandbox,
  and are not touched by `preflight.baseline_build` or the hoist-ingest fixture at all.
- `ruff check` on all 3 touched files: clean. `ruff format --check`: pre-existing, unrelated
  drift exists elsewhere in `test_build_e2e.py` and `test_cli.py` (confirmed already present on
  the pristine `fe698bc` copies, with hunk line ranges nowhere near the lines I touched);
  `test_new_language_touchpoints_e2e.py` is fully clean. `mypy` (whole-project, no path args, cwd
  inside worktree): "Success: no issues found in 131 source files."

## Concerns for the controller

- `main` moved (tasks 115, 116, plus a further docs+test commit) during this task's own
  execution. My branch still forks from `fe698bc` and was NOT rebased (edits are in disjoint
  regions from everything that landed since, verified by diff, and this branch has no commits
  besides the one hotfix commit, so a normal merge should apply cleanly) -- but the merge
  reviewer should re-run `tests/test_new_language_touchpoints_e2e.py`'s full suite post-merge
  once both task 114's and task 115's fixes are combined on `main`, since that is the one file
  neither task alone could verify clean in isolation.
- The `tests/test_build_e2e.py` 7 pre-existing real-Bazel/npm failures are unrelated to this task
  and were NOT fixed here (out of scope); flagging them explicitly since they were not previously
  named in this task's brief and may need their own ticket.
