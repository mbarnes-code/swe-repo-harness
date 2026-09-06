# Task 54 report — §12.11 Gap 3: `baseline_ok IS NULL` exclusion set under the shipped config

**Status: DONE_WITH_CONCERNS** (test-only work fully delivered and green; the "concern" is the
finding itself — a real, previously-partially-disclosed SPEC gap, now proven and filed as `D116`,
not a defect in this task's own deliverable).

**Note on this file's location:** the task brief asked for this report at
`.superpowers/sdd/round-VI-criteria-closure/task-54-report.md` relative to repo root. This
session's worktree isolation refused a write to that path under the shared checkout
(`/home/redmage/swe repo harness/.superpowers/...`), so this report lives at the same relative
path **inside this worktree**
(`/home/redmage/swe repo harness/.claude/worktrees/agent-ac3413816de5110d9/.superpowers/sdd/round-VI-criteria-closure/task-54-report.md`).
The controller will need to copy it into the shared location, or read it from here.

## What was asked

SPEC §12.11's literal last sentence (`docs/SPEC.md:7447`): "Repos with `baseline_ok IS NULL`
(baseline never measured, e.g. `preflight.baseline_build.enabled: false` or a pre-schema-7 run)
are excluded from the count assertion, not silently passed: the fixture run asserts the exclusion
set is empty under the shipped config." Write a fixture-driven test that runs the pipeline under
the shipped default config far enough that every fixture repo's `repos.baseline_ok` gets a real
value, then assert directly against the database that no row has `baseline_ok IS NULL`. If the
shipped default actually leaves repos NULL under ordinary conditions, report that as a finding
rather than weakening the assertion to pass.

## What was found

Before writing anything, I traced every reference to `baseline_ok`/`baseline_test_count` and
`preflight.baseline_build` in `src/fleet/`:

- `src/fleet/settings.py:279-283`: `BaselineBuild.enabled: bool = True` (the pydantic default —
  confirmed by reading the class directly per the brief's instruction, not assumed).
- `config/*.yaml` in this repo carries no `baseline_build:`/`preflight.baseline_build` override,
  and neither does the test fixture's `FLEET_YAML` (`tests/test_transform_e2e.py:66-90`, which
  sets only `preflight.min_free_bytes`). So "the shipped config" really does mean
  `BaselineBuild.enabled = True` with nothing overriding it, both in production `config/` and in
  the e2e fixture.
- `grep -rn "baseline_ok" src/fleet/*.py src/fleet/**/*.py` returns every read site
  (`_RepoFacts.baseline_ok`/`baseline_test_count` in `cli.py`, the `SELECT` at `cli.py:7368`,
  `graph/sequence.py::_exemptions_for`'s `baseline_ok.get(repo_id) == 0`) and **zero** write
  sites — no `UPDATE repos SET baseline_ok` / `INSERT INTO repos (..., baseline_ok, ...)`
  anywhere.
- `grep -rn "baseline_build" src/fleet/` returns exactly one hit: the field's own declaration at
  `settings.py:295`. Nothing reads `settings.baseline_build.enabled` to decide whether to run a
  native baseline build.
- Empirically confirmed (not just by static grep) with a throwaway probe run before writing the
  real test: drove `scan -> sequence -> transform -> build` through the real CLI over the
  five-repo fixture (`tests/test_transform_e2e.py`'s fixture, imported by `tests/test_build_e2e.
  py`) with the shipped config, then read `repos.baseline_ok` back from the real SQLite database.
  Result for all five repos (`acme-lib-ts`, `acme-app-ts`, `acme-lib-py`, `acme-app-py`,
  `acme-empty`): `baseline_ok = NULL`, `baseline_test_count = 0`. The probe file was deleted
  before committing; the same drive is what the committed xfail test now runs.

This matches — and sharpens — what `docs/CRITERIA_PLAN.md`'s own criterion-11 "gap 3" prose
already said in general terms ("nothing writes `repos.baseline_ok` under the shipped config
today"). What this task adds is a fixture-driven, mutation-proven test that proves it concretely
against real state, and a root-cause trace showing it isn't merely "not yet wired" — the
`baseline_build.enabled` config flag that would gate it is read **nowhere** in production code, so
there is no code path at all, gated or not, that ever performs a native baseline build.

Per the brief's explicit contingency ("If you find that the shipped default actually does leave
some repos with `baseline_ok IS NULL` under ordinary conditions ... report that as a finding ...
That would be a real, disclosed SPEC gap, not a bug in your test"), I did not weaken the
assertion. I filed it as **`D116`** in `docs/INTEGRATION_HONESTY.md` (verified `D115` as the
highest allocated number via a form-agnostic `grep -oE '\bD[0-9]+\b' docs/*.md` sweep before
allocating) and updated `docs/CRITERIA_PLAN.md`'s criterion-11 gap-3 bullet to reference it.

## What was built (TEST-ONLY — no production code touched)

New file `tests/test_baseline_ok_exclusion.py`, two tests:

1. **`test_the_baseline_ok_exclusion_set_is_empty_under_the_shipped_config`** — the literal SPEC
   assertion. Runs `scan -> sequence -> transform -> build` through the real CLI (importing the
   real fixtures from `tests/test_transform_e2e.py`/`tests/test_build_e2e.py`: five real git
   repos, real merges, `FakeBazel`/`FakeFilterRepo`/`FakeResolver`/`FakeGazelle` — the same seam
   discipline `test_build_e2e.py`'s own module docstring documents, so every git operation and
   state transition is real and only the external toolchains are faked), then asserts
   `SELECT repo_id FROM repos WHERE baseline_ok IS NULL` returns nothing. Marked
   `@pytest.mark.xfail(strict=True, reason="D116: ...")` — this is currently false, and
   `strict=True` means the moment a future fix makes it true, the xfail becomes an *unexpected
   pass* and fails CI, forcing the marker's own deletion (same pattern as D97,
   `tests/test_sequence_e2e.py`, documented in `CLAUDE.md` §6).

2. **`test_the_exclusion_set_assertion_discriminates_real_db_state_and_is_not_a_tautology`** —
   the Rule 12 mutation proof for test 1's assertion helper (`_baseline_ok_exclusion_set`). The
   brief's literal recipe ("seed one fixture repo with `baseline_ok = NULL` directly, confirm
   red, remove the seed, confirm green") assumes an unmodified run starts green. It doesn't — see
   the finding above, the unmodified state is already red for every repo, with no seed needed. So
   this test inverts the recipe against the **same real database** the same real pipeline run
   just produced:
   - step 1: confirms the real, unmodified state is red (every repo NULL) — a second, independent
     read reproducing test 1's failure;
   - step 2: writes a real non-NULL `baseline_ok` to every row directly via `sqlite3` (bypassing
     normal flow — standing in for what the still-unbuilt write path would eventually produce),
     and confirms the *same* assertion helper now reads green;
   - step 3: nulls exactly one row back out (the brief's literal single-repo seed) and confirms
     the assertion goes red again, for exactly that one repo.

   This is not xfail — it always passes today, because it is a positive proof that the helper is
   a genuine, non-tautological `WHERE baseline_ok IS NULL` read (a helper that always returned
   `[]` would fail step 3; one that always returned every repo id would fail step 2), run against
   real database state, never mocked.

No prior test covered this: `grep -rn "exclusion set" tests/` returned nothing before this file
was added (re-verified after, confined to this file as expected).

## Documentation changes (in scope: disclosure, not production code)

- `docs/INTEGRATION_HONESTY.md`: new `D116` entry (appended after `D114`, matching this file's
  existing non-monotonic-position, chronological-append convention — `D115` sits earlier in the
  file, at a different insertion point).
- `docs/CRITERIA_PLAN.md`: new "Gap 3 measured, not closed" paragraph under criterion 11,
  cross-referencing `D116` and the new test file, and correcting the `<n> of 48` guidance to note
  gap 3 is now *proven* open (not merely disclosed) while gap 1 (`D112`) remains the only gap
  without its own fixture-driven proof.

## Verification run (interpreter/worktree note)

Ran with `.venv/bin/python` from the **primary checkout** (`/home/redmage/swe repo
harness/.venv`), since this worktree has no `.venv` of its own — this is one of CLAUDE.md's Rule
12 "three structural immunities": `pytest` run with `cwd` inside this worktree resolves `fleet`
against **this worktree's** `src/`, not the primary's, because `pyproject.toml`'s
`pythonpath = ["src"]` and `tests/conftest.py`'s own `SRC = REPO_ROOT / "src"` insertion (`REPO_
ROOT` derived from `conftest.py`'s own location, which is in this worktree) both resolve
relative to the worktree, ahead of the editable-install `.pth` entry. Confirmed directly before
relying on it:

```
$ .venv/bin/python -c "import sys; sys.path.insert(0,'src'); import fleet; print(fleet.__file__)"
/home/redmage/swe repo harness/.claude/worktrees/agent-ac3413816de5110d9/src/fleet/__init__.py
```

— resolves inside this worktree, not the primary checkout. No `FLEET_*` env vars were exported in
any shell used.

Exact commands run and results, full files, no `-k` filter:

```
$ .venv/bin/python -m pytest tests/test_baseline_ok_exclusion.py -v
tests/test_baseline_ok_exclusion.py::test_the_baseline_ok_exclusion_set_is_empty_under_the_shipped_config XFAIL
tests/test_baseline_ok_exclusion.py::test_the_exclusion_set_assertion_discriminates_real_db_state_and_is_not_a_tautology PASSED
1 passed, 1 xfailed in 11.79s
```

```
$ .venv/bin/python -m pytest tests/test_lint_gate.py tests/test_integration_honesty_citations.py \
    tests/test_baseline_ok_exclusion.py -q
79 passed, 1 xfailed, 1 warning in 15.06s
```

(`test_lint_gate.py` includes `test_ruff_check_is_clean_across_the_whole_repository`,
`test_ruff_format_check_dirty_count_matches_the_pinned_baseline` — both green, confirming the new
file introduced zero ruff format/lint drift after one `ruff check --fix` + `ruff format` pass on
the new file only — and `test_mypy_strict_is_clean_over_src_fleet`, green, confirming the
production `--strict` mypy gate is unaffected since no `src/` file was touched. An ad hoc
`mypy tests/test_baseline_ok_exclusion.py` run does surface ~75 pre-existing errors, but every one
is attributed to transitively-imported files this task did not touch — `tests/test_cli.py`,
`tests/test_scan_e2e.py`, `tests/test_transform_e2e.py`, `tests/test_build_e2e.py` — and zero are
attributed to `test_baseline_ok_exclusion.py` itself; `tests/` is not in the enforced mypy gate's
scope (`mypy src/fleet/ --strict` only), so these are pre-existing, out-of-scope noise, not a
regression.)

Additionally ran the full `tests/test_build_e2e.py` (the file my new test imports fixtures from,
and the file Task 53 (D112) is dispatched against — I never edited it) to confirm no collection or
fixture-sharing regression:

```
$ .venv/bin/python -m pytest tests/test_build_e2e.py -q
11 failed, 50 passed, 9 skipped in 238.29s (0:03:58)
```

All 11 failures and all 9 skips are pre-existing environment gaps in **this worktree**, unrelated
to this task's change (this task touched no `src/` file and did not edit `test_build_e2e.py`):
the 9 skips are the file's own real-`bazel`/real-`git-filter-repo` tests, each with an explicit
skip message stating the binary "is not installed on this host" (this worktree has neither on
`PATH`, unlike the primary checkout); the 11 failures are all real-Gazelle/real-generator tests
that fail identically with `.../tools/bin/gazelle: 33: cd: can't cd to
.../tools/bin/../go: No such file or directory` — this worktree has no `go/` directory beside
`tools/bin/` (a vendored toolchain path present in the primary checkout but not copied into this
worktree). None of these tests import, exercise, or are reachable from
`tests/test_baseline_ok_exclusion.py`, `docs/INTEGRATION_HONESTY.md`, or `docs/CRITERIA_PLAN.md`
— the causal path from this task's diff to any of them does not exist. Flagged for the controller
rather than silently accepted: **I have not independently re-confirmed these same 11+9 fail/skip
identically on a clean worktree with no changes**, so "pre-existing" here is inferred from the
failure signatures (missing-binary/missing-toolchain-path messages, not assertion failures) and
from the absence of any causal path from my diff, not from an A/B re-run against unmodified
`HEAD`. A reviewer with a fully-provisioned worktree (real `bazel`/`git-filter-repo`/`go`) should
see 0 failed here.

## Concerns for the controller

1. **This is a real, previously partially-disclosed SPEC gap, now proven.** `D116` is a strictly
   larger gap than `D112` (gap 1) and the already-closed gap 2: it shows `baseline_ok`/
   `baseline_test_count` are never written at all under the shipped config, which gaps 1/2 both
   implicitly assumed would eventually happen. No production fix was made (correctly out of scope
   for this TEST-ONLY task) — `docs/CRITERIA_PLAN.md` still does not count §12.11 as `DONE`.
2. **File overlap check:** this task touched exactly `tests/test_baseline_ok_exclusion.py` (new),
   `docs/INTEGRATION_HONESTY.md`, `docs/CRITERIA_PLAN.md`. It did **not** touch
   `tests/test_build_e2e.py` (Task 53/D112's file) or any `src/` file. `git status --short`
   confirms exactly these three paths changed.
3. The brief's mutation-proof recipe had to be inverted (documented in-line in the test's own
   docstring and above) because the premise "an unmodified run is green" turned out false. I
   believe the substitute (drive the same real DB through red→green→red directly) satisfies the
   same Rule 12 intent — a second reviewer should confirm.
4. **Report location:** written inside this worktree, not the shared `.superpowers/` checkout —
   see the note at the top of this file.
