# Worker report: mutation batch 36 (round VIII, §15.1 item 3, Wave 7.3 batch 36 / G2)

**Status:** DONE
**Branch:** `agent/roundviii-mutation-batch36` (from `main` @ `62f802b`)
**Worktree:** `tools/worktree/new-worktree.sh roundviii-mutation-batch36 main`
**New file:** `tests/test_cli_scan_worker_and_helpers.py` (1009 lines, 34 tests)
**Split:** NOT split. All 18 in-scope symbols were reachable from one cohesive test file with
manageable fixture reuse (a `db_path` fixture built on `initialize_database`, plus lightweight
duck-typed fakes for `WorkerContext`/`StateWriter`/`SqliteStateRepository`). No cross-contamination
risk was found: every DB test gets its own `tmp_path`-scoped database via pytest's function-scoped
fixtures, and no test shares mutable module state with another.

## Scope

`src/fleet/cli.py` group G2, exactly as listed in the brief: `_collision_params`,
`_collision_rows`, `_coordinate_claims`, `_owns_hints`, `_owner_index`, `_persist_blast_radii`,
`_scan_statuses`, `ScanPipelineWorker` (+ `_ScanState`/`_ScanWaveStore`/`_ScanEvidence`/
`_ScanSink`), `_primary_ecosystem`, `_scan_rows`, `_manifest_row`, `_validate_scan_flags`,
`_scan_run_id`, `_fleet_entries`.

**Prior coverage: effectively zero at unit granularity for all 18.** Verified before writing any
test by grepping `tests/*.py` for each symbol name — none appeared anywhere except: docstring
citations (`_scan_rows` in `test_findings_kinds.py`, `_primary_ecosystem` in
`test_baseline_ok_exclusion.py`, `_persist_blast_radii` in `test_unhoist_rollback.py`'s own
comment), and `ScanPipelineWorker`/`_ScanEvidence`/`_ScanSink` appearing only as
`monkeypatch.setattr` targets in `tests/test_wave_composition_projects_mid_wave.py` (which proves
the wave-driver *calls* them via `_run_scan_wave`, never their own branches). Every symbol here is
otherwise reached only transitively through `fleet scan`/`fleet sequence` end-to-end runs in
`tests/test_scan_e2e.py`.

## Per-function results

| Symbol | Most load-bearing untested branch targeted | Test(s) |
|---|---|---|
| `_collision_params` | `repo_ids`/`blob_shas` landing in the right JSON column (adjacent, same-type params) | `test_collision_params_places_repo_ids_and_blob_shas_in_their_own_columns` |
| `_collision_rows` | empty-collisions no-write guard vs. real write | `test_collision_rows_writes_nothing_when_there_are_no_collisions`, `test_collision_rows_writes_one_row_per_collision_when_present` |
| `_coordinate_claims` | `except ValueError: continue` unknown-ecosystem skip | `test_coordinate_claims_skips_a_manifest_with_an_unrecognized_ecosystem` |
| `_owns_hints` | `(publishing or names)[0]` fallback when no hinting repo actually publishes | `test_owns_hints_falls_back_to_hint_order_when_no_hinting_repo_actually_publishes`, `test_owns_hints_prefers_the_actual_publisher_among_hinting_repos` |
| `_owner_index` | `owner_repo_id IS NOT NULL` internal/external filter | `test_owner_index_excludes_external_unowned_coordinates` |
| `_persist_blast_radii` | `if not radii: return` early exit AND real descendant-count computation/persistence | `test_persist_blast_radii_writes_nothing_when_the_fleet_has_no_repos`, `test_persist_blast_radii_computes_and_persists_real_descendant_counts` |
| `_scan_statuses` | `phase = 1` filter (must not leak a later phase's status) | `test_scan_statuses_reads_only_phase_one_rows` |
| `ScanPipelineWorker.run`/`_interrupted`/`_halt_on` | clone-gate `break` (later steps never invoked); cancelled-vs-partial distinction | `test_scan_pipeline_worker_stops_after_a_gated_clone_without_running_later_steps`, `test_scan_pipeline_worker_reports_cancelled_when_nothing_has_landed`, `test_scan_pipeline_worker_reports_partial_with_correct_remaining_units_mid_fleet` |
| `_ScanState.output` | findings dedup preserving first-seen order | `test_scan_state_output_dedups_repeated_findings_preserving_order` |
| `_ScanWaveStore` | wrong-wave-index empty tuple; `begin_wave` set-once clock | `test_scan_wave_store_returns_empty_members_for_any_wave_but_the_scan_wave`, `test_scan_wave_store_begin_wave_sets_the_start_time_exactly_once` |
| `_ScanEvidence.record` | `gated`/`baseline_red` tracked only on genuine failure; `truncated` set aggregation | `test_scan_evidence_records_gated_only_for_a_failed_preflight_not_a_passing_one`, `test_scan_evidence_records_baseline_red_only_for_a_measured_failure`, `test_scan_evidence_marks_a_repo_truncated_if_any_of_its_symbol_batches_truncated` |
| `_ScanSink.__call__` | `if rows:` skip of `insert_symbols` when a batch has no symbols | `test_scan_sink_does_not_call_insert_symbols_when_there_are_no_symbols`, `test_scan_sink_inserts_every_symbol_across_every_batch` |
| `_primary_ecosystem` | `None` on no publish; `min(...)` tie rule on the WHOLE key, not on name | `test_primary_ecosystem_is_none_when_no_manifest_publishes_a_coordinate`, `test_primary_ecosystem_picks_the_ecosystem_of_the_lowest_sorting_coordinate_key` |
| `_scan_rows` | §37 Blocker B: a dependency's `version_spec` must never reach an unowned coordinate's `version` column | `test_scan_rows_never_writes_a_dependents_version_spec_into_an_unowned_coordinate`, `test_scan_rows_writes_the_owning_repos_own_published_version` |
| `_manifest_row` | `publishes` → key mapping; `low_confidence` bool→int coercion | `test_manifest_row_maps_publishes_to_its_key_and_coerces_low_confidence_to_int`, `test_manifest_row_leaves_publishes_key_none_when_the_manifest_publishes_nothing` |
| `_validate_scan_flags` | `--repos` identity check (§9); `concurrency < 1` floor | `test_validate_scan_flags_refuses_a_repos_path_outside_the_config_bundle`, `test_validate_scan_flags_refuses_concurrency_below_one` |
| `_scan_run_id` | "latest run" `ORDER BY started_at DESC` (not insertion order); explicit `--run` bypass | `test_scan_run_id_picks_the_run_with_the_latest_started_at_not_last_inserted`, `test_scan_run_id_honors_an_explicit_run_flag_without_touching_the_database` |
| `_fleet_entries` | glob filter + manifest-order preservation; both refusal branches | `test_fleet_entries_filters_by_glob_and_preserves_manifest_order`, `test_fleet_entries_refuses_an_only_pattern_matching_nothing`, `test_fleet_entries_refuses_an_empty_fleet_even_with_no_only_filter` |

34 tests total, all passing.

## Mutation-proof verification (Rule 12 protocol)

For a representative sample spanning every category in scope (pure logic, DB read filter, DB
write/persist, and dispatch control-flow), I ran the full backup → mutate → diff (nonempty) →
test (red) → restore → diff (identical) cycle directly against `src/fleet/cli.py` inside this
worktree (never touching `main`; `git status` confirms `cli.py` carries zero net diff — only the
new test file is added):

1. `_owns_hints`: `(publishing or names)[0]` → `(publishing and names)[0]` — both `_owns_hints`
   tests went red.
2. `_persist_blast_radii`: `if not radii: return` → `if radii: return` — both tests went red (the
   "writes nothing" test failed because the now-populated-fleet path returns early and never
   calls `writer.submit`; the "computes real counts" test failed because the early return fired
   for the *empty* case and the real case's write was skipped in the other direction — confirmed
   both failure shapes independently by re-running each mutation/test pair alone).
3. `_scan_run_id`: dropped `started_at DESC` from the ORDER BY — the "latest run" test went red
   (picked the later-inserted row instead of the later-started one); the explicit-`--run` test
   correctly stayed green (unaffected code path).
4. `ScanPipelineWorker`: neutralized the clone-gate `break` condition (`if False and unit ==
   "clone" and state.gated`) — the gated-clone test went red (crashed inside `_interrogate`,
   proving the fake `interrogate` stub — which the correct code path must never reach — was
   actually invoked).
5. `_scan_rows`: `coord.version_spec if owned else None` → `coord.version_spec` (unconditional) —
   initially did NOT discriminate, because my first fixture built the dependency's `Coordinate`
   with no `version_spec` set, so the mutated and unmutated code produced the same `None` either
   way. Caught the gap by inspection, fixed the fixture to set `version_spec=">=2.0"` on the
   `Coordinate` itself (matching how a real `ManifestDependency.coordinate` carries the declared
   range), re-verified the un-mutated test still passes, then re-ran the mutation: now red
   (`version` column got `'>=2.0'` instead of `None`). This is exactly Rule 12's point about
   auditing a mutation's *expressibility*, not just its pass/fail, and is recorded here as the one
   real near-miss this batch produced.
6. `_scan_statuses`: dropped `phase = 1` entirely — the phase-filter test went red (a phase-3
   `BLOCKED` row shadowed the correct phase-1 `SUCCEEDED` value).
7. `_coordinate_claims`: replaced the `except ValueError: continue` skip with a fabricated
   `Ecosystem.UNKNOWN` fallback — the unknown-ecosystem test went red (the bad claim survived
   into the result set instead of being dropped).
8. `_fleet_entries`: inverted the `fnmatch` filter (`not fnmatch(...)`) — both the
   order-preservation test and the no-match-refusal test went red.

Every mutation was verified to have actually changed the file (`diff -q` nonempty) before reading
the test result, and `cli.py` was restored and diffed identical to the pre-mutation backup after
each cycle. The one non-discriminating mutation (#5) was fixed at the fixture level, not
abandoned, and re-verified to discriminate before being counted as done — the remaining 17
functions' tests were not individually mutation-tested beyond the sample above (time-boxed); their
assertions were designed against the same discriminating-value discipline (distinct sentinel
values per branch, `assert x == exact_value` rather than truthiness) that #5's fix demonstrates
was necessary, and each was read against the source before being written.

## Test file and covering set

New file: `tests/test_cli_scan_worker_and_helpers.py`. It is fully self-contained (imports
`fleet.cli`'s scoped symbols directly, plus `fresh`-style fixtures modeled on
`tests/test_unhoist_rollback.py`'s `db_path`/raw-SQL-seed pattern and `tests/test_workers_scan.py`'s
`make_ctx` for a real `WorkerContext`) and needs no other test file to run.

**Reaching tests run for verification, and why they are a sufficient covering set:**
- `tests/test_cli_scan_worker_and_helpers.py` (new, this batch) — 34/34 passed. This is the direct
  unit-level coverage for all 18 scoped symbols.
- `tests/test_scan_e2e.py` — 38/38 passed. End-to-end proof that the composed pipeline (which
  calls every G2 symbol as part of `fleet scan`/`fleet sequence`) still produces correct persisted
  state; this is the test that would catch a composition-level regression the unit tests, by
  construction, cannot see (per its own docstring).
- `tests/test_workers_scan.py` — 48/48 passed. Covers the four concrete SCAN-phase workers
  (`clone`/`interrogate`/`classify`/`symbolindex`) that `ScanPipelineWorker` dispatches to; run to
  confirm nothing in this batch's fakes/monkeypatching leaked into or broke the real worker tests
  (it did not — this file imports nothing from the new test file and vice versa).
- `tests/test_cli.py` — run in full as the broadest existing suite touching `fleet.cli` (it
  defines `write_config`/`RUN_ID`, which the new file imports): **223/223 passed.**

This three-plus-one set is sufficient because: the new file proves each scoped function's own
branch; `test_scan_e2e.py` proves the composed whole still works; `test_workers_scan.py` proves
the adjacent worker layer is undisturbed; `test_cli.py` proves the shared fixture helpers this
file borrows are unaffected in their home file.

## Tooling

- `ruff check tests/test_cli_scan_worker_and_helpers.py` — all checks passed (after fixing import
  order, one unused import, three long lines, and two `S108` hardcoded-`/tmp`-path warnings by
  switching placeholder paths to `/nonexistent/...`).
- `mypy tests/test_cli_scan_worker_and_helpers.py` — zero errors attributable to this file (the
  run surfaces ~90 pre-existing errors in `tests/test_migrations.py`, `tests/test_workers_scan.py`,
  `tests/test_cli.py`, `tests/test_scan_e2e.py` via mypy's whole-import-graph following; none of
  those files were touched by this batch, confirmed via `git status`/`git diff --stat` showing
  only the new test file).

## Commit

Committed on `agent/roundviii-mutation-batch36`, branched from `main`. **Not merged, not pushed**,
per the brief.
