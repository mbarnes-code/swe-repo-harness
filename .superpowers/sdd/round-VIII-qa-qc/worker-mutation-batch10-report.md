# Worker report: Wave 3 Batch 10 — re-verify mutation proofs for graph/collisions.py, graph/infer.py

Status: DONE. Branch `agent/roundviii-mutation-batch10`, isolated worktree
`/home/redmage/swe repo harness worktrees/wt-roundviii-mutation-batch10` (created via
`tools/worktree/new-worktree.sh roundviii-mutation-batch10 main`), interpreter pinned and
asserted (`fleet.__file__` under the worktree) before any test result was read, per Rule 12's
worktree-isolation guardrail.

## What `334edeb` changed in these two files

`git show 334edeb -- src/fleet/graph/collisions.py src/fleet/graph/infer.py`:

- **`graph/collisions.py`**: added PEP 440 `~=` (compatible-release) support to `_VERSION_ATOM`
  and `_bounds`, and fixed a major-only-tilde precision bug — `_parse` now returns
  `(padded_version, precision)` so `_tilde_ceiling`/`_compatible_release_ceiling` (both now
  routed through a shared `_widen_ceiling`) know how many components the spec actually named,
  instead of being unable to tell `~1` from `~1.0.0` after padding.
- **`graph/infer.py`**: two independent changes — (a) added Gradle's five test-configuration
  scope literals (`testImplementation` etc.) to `TEST_SCOPES`, since `RawDependency.scope` for
  Gradle is the configuration name "as written", not normalized to Maven's literal `"test"`; (b)
  added the missing `MODIFIERS["ambiguous"]` confidence-factor discount to `_dynamic_ref_edges`
  when a `DYNAMIC_REF` symbol resolves to more than one target repo — `_manifest_edges` and
  `_import_edges` already applied this discount for their own multi-owner cases, but the
  DYNAMIC_REF rule's `factors` dict never got the same check.

## Existing proofs for `collisions.py` — RE-VERIFIED, STILL HOLD

Per the scope report's reach mapping (`tests/test_graph_sequence.py`), found two tests written
directly against this fix:

- `test_a_pep440_compatible_release_spec_is_parsed_and_can_conflict` (line ~828)
- `test_a_bare_major_only_tilde_widens_the_major_not_the_minor` (line ~848)

Re-ran the full Rule-12 discipline (backup / edit / `git diff --numstat --no-index` verify
nonempty / test / restore / re-verify-identical) against CURRENT code for each:

1. Mutated `_VERSION_ATOM` back to the pre-fix regex (dropped `~=`). Diff confirmed 1 line
   changed. `test_a_pep440_compatible_release_spec_is_parsed_and_can_conflict` **FAILED**
   (`assert not True` — the `~=2.28` vs `>=3.0` conflict went undetected again), the sibling
   tilde test still passed (mutation is specific). Restored; `git diff --stat` empty; both tests
   pass again.
2. Mutated `_tilde_ceiling` to call `_widen_ceiling(..., floor_precision=0)` (replicates the
   pre-fix "always widen the minor" behavior for the `~`/`~>` path only, leaving `~=` untouched).
   Diff confirmed 1 line changed. `test_a_bare_major_only_tilde_widens_the_major_not_the_minor`
   **FAILED** (`~1` spuriously conflicted with `>=1.5` again), the `~=` test still passed.
   Restored; `git diff --stat` empty; both tests pass again.

Both existing discriminators still target current code exactly — no staleness found. No new
test needed for `collisions.py`.

## Existing proofs for `infer.py` — GAP FOUND, NEW TESTS WRITTEN

The scope report's bucket-A reach for `infer.py` is via `tests/test_sequence_e2e.py` and
`tests/test_workers_contracts.py` (import-string grep), but neither file contains any test
touching `TEST_SCOPES`, Gradle scope literals, `_dynamic_ref_edges`, or the `"ambiguous"`
confidence factor — confirmed by grep (zero hits for `TEST_SCOPES|DYNAMIC_REF|ambiguous` in both
files, and in every other test file). The actual primary test file for `graph/infer.py` is
`tests/test_graph_build.py` (imports `infer_edges` directly, has a dedicated confidence-factors
section), and it too had no test for either specific `334edeb` change to this file — the closest
existing test (`test_confidence_is_reconstructible_from_confidence_factors_alone`) exercises
`TEST_SCOPES` only via the pre-existing literal `"test"`, which a mutant deleting the five new
Gradle literals would not touch. This is exactly the staleness Rule 12 asks to check for: the
file-level "bucket A" reach claim did not imply fix-level discriminating coverage here.

Per brief step 4, wrote two new tests in `tests/test_graph_build.py` (after
`test_an_undeclared_import_is_an_edge_but_a_declared_one_is_not_doubled`, before the "(7) graph
assembly hygiene" section), added `MODIFIERS` to the existing `fleet.graph.infer` import:

- `test_a_gradle_test_configuration_scope_gets_the_test_scope_discount` — asserts a dependency
  with `scope="testImplementation"` gets `confidence_factors["test_scope"] == MODIFIERS
  ["test_scope"]` and the corresponding discounted `confidence`.
- `test_a_dynamic_ref_matching_two_owners_is_marked_ambiguous` — builds two repos publishing the
  identical coordinate and a `DYNAMIC_REF` symbol resolving to both; asserts the single
  deduplicated edge (edges dedup by `edge_key`, which is keyed on the coordinate, not the
  resolved owner — so two candidate owners collide onto one row with `dst_candidate_repo_ids`
  carrying both, discovered while writing the test, not assumed) carries `.ambiguous == True`
  (unchanged by this fix — driven by `len(targets) > 1` same as before) AND
  `confidence_factors["ambiguous"] == MODIFIERS["ambiguous"]` (the actual fix) with the
  correctly discounted `confidence`.

Both pass on original code. Applied Rule-12 discipline per mutation:

1. Mutated `TEST_SCOPES` back to the pre-fix 4-literal set. Diff confirmed nonempty (16 lines
   removed). `test_a_gradle_test_configuration_scope_gets_the_test_scope_discount` **FAILED**
   (`confidence_factors.get("test_scope")` was `None`), sibling test unaffected. Restored;
   `git diff --stat` empty; both tests pass again.
2. Mutated `_dynamic_ref_edges` to drop the `if len(targets) > 1: factors["ambiguous"] = ...`
   two lines (leaving `.ambiguous`/`candidates` computation, driven by the same `len(targets) >
   1`, untouched — isolating exactly the confidence-factor omission the fix closed). Diff
   confirmed nonempty (2 lines removed). `test_a_dynamic_ref_matching_two_owners_is_marked_
   ambiguous` **FAILED** (`confidence_factors.get("ambiguous")` was `None` while `edge.ambiguous`
   stayed `True` — reproducing the exact pre-fix defect shape: correctly flagged, not
   discounted), sibling test unaffected. Restored; `git diff --stat` empty; both tests pass
   again.

Final state: `git diff main` shows only `tests/test_graph_build.py` changed (+59 lines, 0
removed); `src/fleet/graph/{collisions,infer}.py` are byte-identical to `main`.

## Verification

- `tests/test_graph_build.py` + `tests/test_graph_sequence.py`: **66 passed** (full files, no
  `-k` filter), run with the environment replaced (`env -i PATH=... PYTHONPATH="$WT/src"
  .venv/bin/python`) and `fleet.__file__` asserted under the worktree before reading any result.
- `python -m mypy` (repo's own `[tool.mypy]` scope: `packages = ["fleet"]`, i.e. `src/` only —
  tests are outside its scope, consistent with the rest of this project): **Success: no issues
  found in 132 source files**. No `src/` files were touched by this task, so this is unchanged
  from `main`.
- `python -m ruff check tests/test_graph_build.py`: **All checks passed!**
- `python -m ruff format --check tests/test_graph_build.py`: reports the file "would be
  reformatted", but the reported hunks are all pre-existing drift unrelated to this change —
  verified by running the same check against `git show main:tests/test_graph_build.py`, which
  fails identically at the same locations. `ruff format --diff` shows zero hunks touching either
  new test. Not fixed here per Rule 3 (surgical changes; not this task's mess, and CLAUDE.md's
  own round VIII history shows ruff-format-pin drift is tracked and re-pinned as its own
  dedicated task, not bundled into unrelated work).

## Commit

Committed on `agent/roundviii-mutation-batch10` (from `main`). NOT merged, NOT pushed.
