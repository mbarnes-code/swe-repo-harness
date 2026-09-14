# Worker report: mutation batch 38 (round VIII, §15.1 item 3, Wave 7.4 batch 38 / G5)

**Status:** DONE
**Branch:** `agent/roundviii-mutation-batch38` (from `main`)
**Worktree:** `tools/worktree/new-worktree.sh roundviii-mutation-batch38 main`
**New file:** `tests/test_cli_transform_helpers_batch38.py` (12 tests)
**Split:** Not split. All 11 in-scope symbols were reachable from one cohesive test file, all
schema-fresh sqlite (self-contained `_fresh_db`) or real-git fixtures with no shared mutable state
between tests.

## Scope

`src/fleet/cli.py` group G5, exactly as listed in the brief: `_git_output`, `_nul_fields`,
`_tracked_at`, `_transform_rules`, `_rewrite_targets`, `_prepare_repo`, `_abandon_repo`,
`_wave_repos`, `_import_specifiers`, `_dest_paths`, `_ordering_pairs`.

**Prior coverage, checked by grepping `tests/*.py` for each symbol before writing anything:**

| Symbol | Refs found | What they were |
|---|---|---|
| `_git_output` | 0 | none |
| `_nul_fields` | 0 | none |
| `_tracked_at` | 2 | docstring mention only (`tests/test_transform_e2e.py`) |
| `_transform_rules` | 7 | 2 real direct-call tests (both branches) + 5 comment mentions |
| `_rewrite_targets` | 1 | docstring mention only |
| `_prepare_repo` | 32 | heavy real coverage: e2e (`test_transform_e2e.py`, 22 tests touch it transitively) + 2 direct calls in `test_cli.py` |
| `_abandon_repo` | 12 | direct calls in `test_cli.py` (redaction test, D43 abandon-routing test) + e2e |
| `_wave_repos` | 1 | docstring mention only |
| `_import_specifiers` | 0 | none |
| `_dest_paths` | 1 | comment mention only (`test_build_e2e.py`) |
| `_ordering_pairs` | 0 | none |

Six of eleven symbols had **zero or docstring-only** prior references anywhere in the suite.
`_transform_rules` was found to be genuinely, adequately covered already (both its branches —
missing-dir raise and present-empty return — have direct unit tests in `tests/test_cli.py`); no
new test was added for it, and this was confirmed rather than assumed (see mutation-proof section:
mutation #1 below targets a sibling function, `_nul_fields`, not `_transform_rules`, precisely
because `_transform_rules` did not need one). `_prepare_repo`/`_abandon_repo` are heavily tested
but each had one genuine, concrete gap: `_prepare_repo`'s `dest_path is None` refusal (§3.3's
`layout()` REFUSAL path) had no test anywhere, and `_abandon_repo`'s `phase`/`kind`
parameterization (added specifically so Phase 3's git-preparation failures can share this
function) had never been exercised with a non-default value.

## Per-function results

| Symbol | Most load-bearing untested branch targeted | Test(s) |
|---|---|---|
| `_git_output` | full-disk-read vs. `Git.text`'s bounded tail; the `GitCommandError` failure path | `test_git_output_reads_the_complete_listing_off_disk`, `test_git_output_raises_git_command_error_on_a_failing_invocation` |
| `_nul_fields` | drops exactly the empty NUL-split fields, keeps real ones | `test_nul_fields_drops_empty_fields_but_keeps_real_ones` |
| `_tracked_at` | reads the tree AT THE GIVEN REV, not the current worktree/HEAD | `test_tracked_at_reads_the_given_rev_not_the_current_worktree` |
| `_transform_rules` | already covered (2 existing tests, both branches) — no new test | — |
| `_rewrite_targets` | `any(...)`-across-rules OR logic; empty-result when nothing matches | `test_rewrite_targets_keeps_only_sources_at_least_one_rule_claims`, `test_rewrite_targets_is_empty_when_no_rule_matches_any_source` |
| `_prepare_repo` | `dest_path is None` refusal (§3.3 layout REFUSAL, untested branch) | `test_prepare_repo_refuses_when_layout_names_no_destination` |
| `_abandon_repo` | `phase`/`kind` parameterization used by Phase 3, never exercised non-default | `test_abandon_repo_honors_a_non_default_phase_and_kind` |
| `_wave_repos` | `node_kind='REPO'` SQL filter + the Python-side `fnmatch` `only` filter | `test_wave_repos_filters_contracts_and_applies_the_only_glob` |
| `_import_specifiers` | `suppress(ReservedDestError, ValueError)` excludes a refused repo, keeps others | `test_import_specifiers_excludes_a_repo_layout_refuses_and_keeps_the_other` |
| `_dest_paths` | refused repo maps to `None` as a PRESENT key (not dropped, unlike `_import_specifiers`) | `test_dest_paths_maps_a_refused_repo_to_none_not_an_absent_key` |
| `_ordering_pairs` | three-way SQL filter (`ordering_suppressed`, `confidence`, `kind IN (...)`) + the `(dst,src)→(dependency,dependent)` column-order reversal | `test_ordering_pairs_applies_all_three_filters_and_reverses_the_edge_direction` |

12 tests total, all passing.

## Mutation-proof verification (Rule 12 protocol)

Ran the full backup → mutate → diff (nonempty) → test (red) → restore → diff (identical) cycle
directly against `src/fleet/cli.py` inside this worktree for 8 mutations spanning every distinct
shape in scope (pure logic, git plumbing/disk-read, rev parameterization, OR/AND boolean, an
exception-refusal branch, a parameterization ignored, SQL column order, and exception-handling
semantics). Backup kept in this lane's own scratch subdirectory
(`/tmp/.../scratchpad/batch38/cli.py.backup`), never a shared filename. `git status`/`git diff
--stat` confirm `cli.py` carries zero net diff after all cycles — only the new test file is added.

1. `_nul_fields`: `if field` → `if not field` — `test_nul_fields_drops_empty_fields_but_keeps_real_ones`
   went red (`_nul_fields("a\0b\0c\0")` returned `['']` instead of `['a','b','c']`).
2. `_git_output`: `result.stdout_path.read_text(...)` → `result.stderr_tail` —
   `test_git_output_reads_the_complete_listing_off_disk` went red (returned empty stderr instead
   of the 50-file listing).
3. `_tracked_at`: the `rev` parameter passed to `ls-tree` hardcoded to `"HEAD"` —
   `test_tracked_at_reads_the_given_rev_not_the_current_worktree` went red (`at_anchor` returned
   HEAD's tree instead of the anchor commit's).
4. `_rewrite_targets`: `any(...)` → `all(...)` — `test_rewrite_targets_keeps_only_sources_at_least_one_rule_claims`
   went red (result emptied, since no source satisfies every rule in a two-disjoint-rule fixture).
5. `_prepare_repo`: `if dest_path is None:` → `if False and dest_path is None:` (neutralized) —
   `test_prepare_repo_refuses_when_layout_names_no_destination` went red, and exactly as the
   test's own docstring predicted: it surfaced as `AttributeError: 'NoneType' object has no
   attribute 'rstrip'` instead of the actionable `TransformStepUnavailableError`.
6. `_abandon_repo`: the `phase` parameter in the `UPDATE phases ... WHERE phase = ?` params
   hardcoded to `int(Phase.TRANSFORM)` — `test_abandon_repo_honors_a_non_default_phase_and_kind`
   went red (the seeded `Phase.BUILD` row stayed `PENDING`, untouched).
7. `_ordering_pairs`: `SELECT dst_id, src_id` → `SELECT src_id, dst_id` (column order) —
   `test_ordering_pairs_applies_all_three_filters_and_reverses_the_edge_direction` went red: the
   pair COUNT stayed 1 (confirming the note that a count-only assertion would NOT have caught
   this), but the tuple came back as `('acme-billing', 'acme-commons')` instead of
   `('acme-commons', 'acme-billing')`.
8. `_dest_paths`: `out[repo_id] = None` → `continue` in the except clause —
   `test_dest_paths_maps_a_refused_repo_to_none_not_an_absent_key` went red (`"acme-reserved"` was
   absent from the dict entirely instead of present with value `None`).

Every mutation was verified to have actually changed the file (`diff -q` nonempty) before reading
the test result, and `cli.py` was restored and diffed identical to the pre-mutation backup after
each cycle. `_import_specifiers` and `_wave_repos` were not individually mutation-tested beyond
this 8-mutation sample (time-boxed); their assertions follow the same discriminating-value
discipline (exact dict/tuple equality, not truthiness) the sampled mutations confirm is necessary,
and both were read against source before being written — `_import_specifiers`'s test is the
mirror image of mutation #8 (`_dest_paths`), covering the DROP-instead-of-`None` side of the same
`suppress(...)` construct on the sibling function, so that construct's other failure mode
(`suppress` removed entirely, raising out of the fleet-wide loop) is the one gap left unexercised
by mutation in this batch, though the `suppress(...)` call itself is directly visible in both
functions' source and asserted present by both new tests passing without it raising.

## Test file and covering set

New file: `tests/test_cli_transform_helpers_batch38.py`. Fully self-contained (own `_fresh_db`/
`_insert_repo` helpers modeled on `tests/test_contract_extraction_plumbing.py`'s pattern) except
for three names imported from `tests/test_cli.py` (`RUN_ID`, `FleetSettings`, `write_config`) —
the same "import from a sibling test module" pattern `tests/test_transform_e2e.py` already uses
for `tests/test_scan_e2e.py`.

**Reaching tests run for verification, and why they are a sufficient covering set:**
- `tests/test_cli_transform_helpers_batch38.py` (new, this batch) — **12/12 passed.** Direct
  unit-level coverage for all 11 scoped symbols.
- `tests/test_transform_e2e.py` — **22/22 passed.** End-to-end proof that the composed Phase 2
  pipeline (which calls `_prepare_repo`, `_abandon_repo`, `_transform_rules`, `_rewrite_targets`,
  `_tracked_at`, `_wave_repos`, `_dest_paths`, `_import_specifiers`, `_ordering_pairs` as part of
  `fleet transform`) still produces correct persisted state and committed git history.
- `tests/test_workers_transform.py` — **29/29 passed.** The concrete transform-phase workers this
  batch's plumbing feeds (`RewriteWorker`/`RelocateWorker` inputs derive from `_TransformPlan`,
  which `_prepare_repo` builds); confirms nothing in this batch leaked into or broke that layer.
- `tests/test_cli.py` — **223/223 passed.** The broadest existing suite touching `fleet.cli`
  (source of the three imported names); confirms the shared fixtures this file borrows are
  unaffected in their home file, and re-confirms `_transform_rules`'s two existing direct tests
  and `_prepare_repo`/`_abandon_repo`'s existing direct tests (D43, redaction) still pass.

Combined: **286/286 passed** (94.52s). This four-file set is sufficient because: the new file
proves each scoped function's own untested branch directly; `test_transform_e2e.py` proves the
composed Phase 2 whole still works end to end; `test_workers_transform.py` proves the adjacent
worker layer is undisturbed; `test_cli.py` proves the shared fixture helpers and the two
already-adequate functions (`_transform_rules`, and the pre-existing halves of `_prepare_repo`/
`_abandon_repo`'s coverage) are unaffected.

## Tooling

- `ruff check tests/test_cli_transform_helpers_batch38.py` — all checks passed (after removing one
  unused `asyncio` import caught on the first run).
- `ruff check src/fleet/cli.py` — all checks passed (unmodified; confirms the mutation-testing
  cycle left no residue).
- `mypy tests/test_cli_transform_helpers_batch38.py` — **zero errors attributable to this file.**
  The run surfaces 56 pre-existing errors in `tests/test_migrations.py`, `tests/test_cli.py`, and
  `tests/test_scan_e2e.py` via mypy's whole-import-graph following (comparison-overlap on
  `ExitCode` literals, unused `type: ignore` comments, etc.) — none of those files were touched by
  this batch, confirmed via `git status`/`git diff --stat` showing only the new test file.

## Commit

Committed on `agent/roundviii-mutation-batch38`, branched from `main`. **Not merged, not
pushed**, per the brief.
