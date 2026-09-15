# Worker report: round VIII, §15.1 item 3, Wave 7.8 Batch 49

## Status: DONE

Branch: `agent/roundviii-mutation-batch49`
Original commit: `bd16b40` (worktree: `/home/redmage/swe repo harness worktrees/wt-roundviii-mutation-batch49`)
**Correction commit (2026-09-15): `338dc63`** — see "Correction (2026-09-15)" near the end of this
report; the `abort()` item below (originally: mutation only in the `--now` direction) was found by
review to leave the drain-direction wording unverified and has been fixed.
Not merged, not pushed, per brief. `src/fleet/cli.py` is byte-identical to `main` (every mutation
applied during verification was reverted before the next one; only `tests/test_cli.py` changed).

## Scope (G8+G9+G11 merged)

`status()`, `_parse_filters`, `_test_count_report`, `_status_once`, `_metrics`, `_prometheus`,
`_dot`, `quarantine()` (thin wrapper only), `retry()`, `_retry_impl`, `abort()` (thin wrapper
only), `_count_running_phases`, `gc()`, `_duration`, `_gc_impl`, `_require_disk_headroom` (minus
`_gc_disk`) in `src/fleet/cli.py`. `_quarantine_impl`, `_abort_impl`, `_gc_disk` excluded per
brief, already proven.

## Per-item results

### `status()` — new test

`test_status_filter_and_sort_are_actually_wired_through_to_the_rendered_rows`. Most load-bearing
untested branch: `status()` must actually THREAD the parsed `selectors`/`--sort` into
`_status_once` rather than silently dropping them — the same "accepted but discarded flag" shape
`quarantine()`'s `--stub-blocked` turns out to have (see Notes). No existing test in this file
ever passes `--filter` or `--sort` to `status`.

Mutation: `_status_once(..., selectors=selectors, ...)` → `selectors={}` at the call site inside
`status()`. `git diff` = 1 line. Test failed (`--filter status=RUNNING` returned both repos
instead of one). Restored, empty diff, test green.

### `_parse_filters` — new test

`test_parse_filters_rejects_an_unknown_key_and_accepts_the_documented_ones`. Direct unit test:
accepts `status=X`/`wave=N`, rejects an unknown key and a bare value with no `=`.

Mutation: `if not sep or key not in {"status", "wave"}:` → `if not sep:`. `git diff` = 1 line.
Test failed (`DID NOT RAISE UsageError` for `repo=acme-commons`). Restored, empty diff, green.

### `_status_once` — two new tests (two independent branches)

Same test as `status()` above also proves `_status_once`'s own `--sort blast-radius` branch: two
repos whose alphabetical order is the OPPOSITE of their blast-radius-descending order (so a
removed sort is observable, unlike every other status test's coincidentally-alphabetical fixture).
Mutation: `if sort == "blast-radius":` → `if sort == "nonsense-value":`. `git diff` = 1 line. Test
failed (order unchanged). Restored, empty diff, green.

`test_status_format_dot_renders_the_graph_including_edge_coloring` covers the DOT dispatch
(`if output_format is OutputFormat.DOT: ... return`), never reached by any other status test.
Mutation: `OutputFormat.DOT` → `OutputFormat.JSON` at that `if`. `git diff` = 1 line. Test failed
(table text emitted instead of `digraph fleet {...}`). Restored, empty diff, green.

### `_metrics` — new test

`test_metrics_maps_the_budget_ledger_columns_to_the_right_metric_names`. Most load-bearing
untested branch (§11.2 fail-closed cost accounting): the `spent_usd`/`reserved_usd`/`max_usd`
SELECT must map to the correspondingly-named metrics in that order — three distinct sentinel
values (10.0/2.0/99.0) catch a swapped column order, which equal values could not. No existing
test asserts on `_metrics`' returned values at all (only that a `--metrics-out` file exists and
starts with `"# TYPE"`).

Mutation: `SELECT spent_usd, reserved_usd, max_usd` → `SELECT reserved_usd, spent_usd, max_usd`.
`git diff` = 1 line. Test failed (`fleet_budget_spent_usd` read 2.0, not 10.0). Restored, empty
diff, green.

### `_prometheus` — new test

`test_prometheus_output_is_sorted_by_metric_name`. Direct unit test: two out-of-order metric
names must render sorted, not in insertion order (so a `--metrics-out` file diffs cleanly across
runs).

Mutation: `sorted(measurements.items())` → `measurements.items()`. `git diff` = 1 line. Test
failed (`z_metric` rendered before `a_metric`). Restored, empty diff, green.

### `_dot` — new test

Same `test_status_format_dot_renders_the_graph_including_edge_coloring` above also covers `_dot`'s
own attribute logic: a suppressed edge, a retargeted edge, and a low-confidence edge, asserting
`color=red`/`color=blue`/`style=dashed` respectively — no existing test exercises `_dot` at all.

Mutation: `if bool(suppressed):` → `if False:`. `git diff` = 1 line. Test failed (`color=red`
missing; `color=blue`/`style=dashed` still present, confirming those two branches are independent
and unaffected). Restored, empty diff, green.

### `_test_count_report` — new test

`test_test_count_report_never_regresses_against_a_red_baseline`. Most load-bearing untested
branch: `baseline_ok_value is True and ...` — a repo whose OWN baseline never went green
(`baseline_ok=0`) must never read as regressed even when `migrated_test_count` is numerically
smaller than `baseline_test_count`. Distinct from the two existing tests, which only exercise
`baseline_ok=1` (regressed/not-regressed) and `migrated_test_count IS NULL`.

Mutation: removed `baseline_ok_value is True and` from the `regressed` expression. `git diff` = 2
lines (1 deletion collapsing 3→2 source lines). Test failed (`test_count_regressed` read `True`).
Restored, empty diff, green.

### `quarantine()` — new test

`test_quarantine_human_readable_line_reflects_dry_run_and_the_real_counts`. The wrapper's own
logic (`_quarantine_impl` excluded, already proven): the human-readable line's `'would
quarantine' if dry_run else 'quarantined'` wording and its `phases_skipped`/`dependents_blocked`
interpolation had never been asserted — every existing quarantine test reads the DB or `--json`,
never `result.output`'s plain-text line.

Mutation: `'would quarantine' if dry_run else 'quarantined'` → `'quarantined'`. `git diff` = 1
line. Test failed (dry-run line read "quarantined", not "would quarantine"). Restored, empty
diff, green.

### `retry()` — new test

`test_retry_human_readable_line_reflects_dry_run_and_the_reopened_phase`. Same shape as
quarantine's: the `'would reopen' if dry_run else 'reopened'` wording and `result['phase']`
interpolation, never asserted.

Mutation: `'would reopen' if dry_run else 'reopened'` → `'reopened'`. `git diff` = 1 line. Test
failed. Restored, empty diff, green.

### `_retry_impl` — new test (genuine gap found)

`test_retry_refuses_two_rhi_phase_rows_for_the_same_repo_as_a_structural_surprise`. `_retry_impl`
carries its OWN `len(rhi_rows) > 1` pre-check (a `connect_ro` read, before any
`StateWriter`/repository call) — distinct from `SqliteStateRepository.reopen_to_pending`'s later,
transactional guard for the same fact
(`tests/test_repository.py::test_reopen_to_pending_raises_when_multiple_rhi_rows_exist`). No
existing CLI test ever seeds two REQUIRES_HUMAN_INTERVENTION rows for one repo, so this CLI-level
early check had never fired through `fleet retry`.

**First mutation attempt was a false pass, corrected before landing.** The two guards' raised
messages share both `"has N REQUIRES_HUMAN_INTERVENTION phase rows"` and `"expected exactly one"`
(the repository's message reuses that exact phrasing), so a test asserting only those substrings
cannot tell which layer actually raised. Mutating `> 1` to `> 2` initially left the test GREEN —
the CLI's own guard was defeated, but the repository's own guard fired instead (defense in depth),
producing an externally-identical exit code and near-identical message. Fixed by adding
`assert "refused:" not in result.output` — only `_retry_impl`'s `except RepositoryError ->
UsageError` translation path prefixes `"fleet retry '<repo>' refused:"`; the CLI's own direct
raise does not. Re-verified: baseline (unmutated) passes; `> 1` → `> 2` mutation now fails on the
new assertion specifically (`'refused:' is contained here: ... fleet retry 'acme-commons'
refused: reopen_to_pending: ...`). Restored, empty diff, green.

### `abort()` — new test

`test_abort_human_readable_line_reflects_drain_vs_now_and_the_reset_count`. Thin wrapper only
(`_abort_impl` excluded, already proven by the two existing drain/now tests — which only ever
check DB state and timing, never `result.output`'s text). The `'drained' if drain else
'cancelled'` wording and `running_reset`/`projection` interpolation had never been asserted.

Mutation: `'drained' if drain else 'cancelled'` → `'drained'`. `git diff` = 1 line. Test failed
(`--now` line read "aborted (drained)"). Restored, empty diff, green.

**Correction (2026-09-15, commit `338dc63`):** this original mutation only exercised the
`--now` (cancelled) direction. Review correctly found that the test never invoked `abort()`'s
default (drain) path at all, so a mutation collapsing the ternary to unconditional `'cancelled'`
passed silently — inconsistent with the sibling `quarantine()`/`retry()` tests in the same diff,
which do exercise both branches. Fixed by extending the same test to also drive the drain path
(reusing `ABORT_DRAIN_YAML`, in a separate workspace subdirectory) and mutation-verifying the
drain direction specifically (`'drained' if drain else 'cancelled'` → unconditional `'cancelled'`;
`git diff` = 1 line; test failed on the new drain-path assertion; restored, byte-identical). See
the correction section near the end of this report for the full record.

### `_count_running_phases` — new test

`test_count_running_phases_counts_only_running_rows_for_the_named_run`. Direct unit test: must
count only `status='RUNNING'` rows for the one `run_id` asked about, excluding other statuses in
the same run and RUNNING rows in a different run. Every existing exercise of this function is
indirect, through the excluded `_abort_impl`'s drain-wait loop, which only ever proves ">0 vs 0"
for a single run — never the per-run isolation.

Mutation: `AND status = 'RUNNING'` → `AND 1=1`. `git diff` = 1 line. Test failed (count read 2
instead of 1, picking up the other run's RUNNING row and the same run's PENDING row). Restored,
empty diff, green.

### `gc()` and `_gc_impl` — one new test, two independent mutations (genuine gap found)

`test_gc_real_run_deletes_events_for_non_kept_runs_using_a_custom_keep_flag`. **Every existing gc
test in this suite only ever used `--dry-run`** (`test_gc_refuses_to_evict_under_live_work`'s
"forced" call included) — the real `DELETE FROM events/attempts/llm_cache` statements had zero
coverage before this batch, and no test ever passed a non-default `--events-keep-runs`. Two runs
seeded (older `RUN_ID`, a newer one); `--events-keep-runs 1` should keep only the newer run.

Mutation A (`gc()`'s own flag-threading): `settings.config.gc.events_keep_runs if
events_keep_runs is None else events_keep_runs` → unconditionally `settings.config.gc.
events_keep_runs`. `git diff` = 1 line (default is 5, test passes 1). Test failed
(`keep_runs` included both runs instead of only the newer one). Restored, empty diff, green.

Mutation B (`_gc_impl`'s real-deletion path): `if dry_run:` → `if True:` (forces the function to
always behave as dry-run regardless of the flag). `git diff` = 1 line. Test failed (the older
run's event row was never actually deleted). Restored, empty diff, green.

### `_duration` — new test

`test_duration_parses_and_translates_a_parse_failure_into_a_usage_error`. Direct unit test: valid
parse (`"2h"` → 7200) and the `ValueError -> UsageError` translation naming `--cache-max-age`
specifically. No test in the suite ever passes a non-default `--cache-max-age`, so neither branch
had executed.

Mutation: removed the `try/except` translation entirely (bare `return parse_duration_s(text)`).
`git diff` = 4 lines. Test failed with an uncaught `ValueError` instead of `UsageError`. Restored,
empty diff, green.

### `_require_disk_headroom` — new test

`test_require_disk_headroom_actually_evicts_files_rather_than_only_simulating_it`. `_require_disk_
headroom` calls `_gc_disk(settings, dry_run=False)` with a HARDCODED literal (`_gc_disk` itself
excluded, already proven) — every existing `_require_disk_headroom` test fixes an empty/absent
cache dir, where `dry_run` changes nothing observable. This test forces a real eviction (two
700 MB sparse files against a 1 GiB `max_disk_gb`, oldest-atime-first) and checks the oldest file
was actually unlinked from disk.

Mutation: `dry_run=False` → `dry_run=True` in the wrapper's call to `_gc_disk`. `git diff` = 1
line. Test failed (the file that should have been deleted still existed). Restored, empty diff,
green.

## Test file(s) run for verification, and why they're a sufficient covering set

**`tests/test_cli.py`** (273 passed, up from 259) is the sufficient covering set for this group.
Grep-verified against every file under `tests/` for each of the 16 scoped names:
- `_parse_filters`, `_test_count_report`, `_metrics`, `_prometheus`, `_dot`, `quarantine(`,
  `_retry_impl`, `abort(` (the cli.py commands), `_count_running_phases`, `_duration(`, `_gc_impl`
  — zero hits outside `tests/test_cli.py`.
- `_status_once` — one hit, `tests/test_no_state_outside_git.py:69`, a **pre-existing stale
  line-number citation** (already flagged by the scoping report for whichever batch touches that
  file next — not this one — and unrelated to `_status_once`'s behavior).
- `_require_disk_headroom` — hits in `tests/test_resume_continue.py` (monkeypatches it to a no-op
  stub for an unrelated resume-continuation fixture, does not test its own behavior),
  `tests/test_transform_e2e.py` (prose reference to the phase-entry-gate concept, doesn't call it),
  `tests/test_integration_honesty_citations.py` (a citation-drift detector checking a doc's cited
  line number still points at this function's definition — unaffected since `src/fleet/cli.py` was
  never actually changed in the final commit).
- `status(`/`retry(`/`gc(` as bare substrings hit many unrelated files (`RepoStatus(...)`
  constructor calls, `budgets.gc` config attribute access, etc.) — checked individually, none are
  real reaching tests of the CLI commands.

## Genuine gaps found beyond the brief's own leads

1. **`_gc_impl`'s real (non-`--dry-run`) deletion path had zero test coverage** before this batch.
   Now covered by `test_gc_real_run_deletes_events_for_non_kept_runs_using_a_custom_keep_flag`.
2. **`_retry_impl`'s own `len(rhi_rows) > 1` pre-check had never been exercised through the CLI**
   — only the repository layer's later, redundant guard for the same fact was tested. Now covered,
   with the discriminator specifically isolating which layer raised (see above).
3. **`quarantine()`'s `--stub-blocked` flag is parsed and silently discarded** (`_ = stub_blocked`
   at the `quarantine()` wrapper; `_quarantine_impl` has no `stub_blocked` parameter at all), while
   `docs/SPEC.md:6890`'s command-flag table documents `fleet quarantine <repo> --reason TEXT
   --stub-blocked --dry-run` as a supported combination. Grep-verified: no existing D-number in
   `docs/INTEGRATION_HONESTY.md` covers this specific quarantine+stub_blocked mismatch (the
   existing quarantine-related entries at lines 4456/6871/6889/8041-8051 are about a different
   defect — `_quarantine_impl`'s raw-SQL status write bypassing `transition()`'s guard). **Not
   fixed here** — out of scope for a test-writing batch, and per CLAUDE.md's Central Number
   Allocation rule, D-numbers are the orchestrator's to assign. Flagging for controller triage:
   either wire `--stub-blocked` into `_quarantine_impl` (matching the SPEC row), or correct the
   SPEC row to drop it and disclose the narrowing per the "§12 wording changes only by disclosed
   adjudication" pattern (this flag is CLI scaffolding, not a §12 criterion, so the narrower
   Guardrail-7 "fix the code and its doc listing together" applies, not Rule 14).

## Static checks

- `ruff check tests/test_cli.py`: all checks passed (one line-length fix applied during
  development: the quarantine dry-run assertion line was reflowed to fit under 100 columns).
- `mypy tests/test_cli.py`: 58 errors across 3 files (`tests/test_cli.py`, `tests/test_migrations.
  py`, `tests/test_scan_e2e.py`). Diffed against `main`'s `tests/test_cli.py` (swapped in-place
  inside this worktree, mypy re-run, swapped back) with line numbers stripped from every message:
  **identical 58-error set, byte-for-byte** — none introduced by this change.

## Notes

- Followed CLAUDE.md's worktree-isolation and "never run two pytest sessions concurrently" rules:
  all work done inside the dedicated worktree; `git stash` never used (a single backup copy of
  `src/fleet/cli.py` at `/tmp/cli.py.backup.batch49`, restored via `cp` after every mutation, plus
  `git diff --stat`/`git status --porcelain` checked clean before each subsequent mutation —
  mutations were never live concurrently).
- Every mutation's `git diff` was inspected before running the test (non-empty, exactly the
  intended line(s)) and the restore was verified byte-identical (`git status --porcelain` empty,
  and for the final restore, `diff -q` against the backup) before moving to the next mutation.
- `src/fleet/cli.py` is untouched in the final commit — byte-identical to `main` — confirmed via
  `git status --porcelain` showing only `tests/test_cli.py`.

## Correction (2026-09-15, commit `338dc63`)

Review of the original commit (`bd16b40`) returned spec verdict ✅ and confirmed all three
disclosures above accurate (including the quarantine `--stub-blocked` finding's filing as a
D-number by the controller), but found one genuine Important gap:
`test_abort_human_readable_line_reflects_drain_vs_now_and_the_reset_count` only ever invoked
`abort --now`. Mutating the ternary to unconditional `'cancelled'` passed silently, because
nothing in the suite ever rendered the default (drain) path's own human-readable line — the
`abort()` item's write-up above claimed the mutation "proved" the branch but the mutation actually
only exercised one arm of it, an asymmetry with the sibling `quarantine()`/`retry()` tests in the
same original commit, both of which do exercise both branches of their own ternaries.

Fixed by extending the same test (not adding a new one) to also invoke plain `abort --reason
operator` (the default, drain path) against a separate workspace built with the existing
`ABORT_DRAIN_YAML` short-timeout fixture (`budgets.wave_drain_timeout_s: 1`, already used by
`test_abort_checkpoints_and_regenerates_the_projection`), asserting `"aborted (drained); ..."`
appears and `"aborted (cancelled)"` does not.

Mutation-verified in the drain direction specifically: `'drained' if drain else 'cancelled'` →
unconditional `'cancelled'`. `git diff` = 1 line (same call site as the original mutation, applied
in the opposite direction this time). Test failed on the new drain-path assertion (`"aborted
(drained); 1 RUNNING row(s) reset, projection at"` not found; message read `"aborted (cancelled);
..."` instead). Restored via `cp` from a fresh backup (`/tmp/cli.py.backup.batch49.fix1`),
`diff -q` confirmed byte-identical.

Full covering set re-run after the fix: `tests/test_cli.py`, 273 passed (unchanged count — an
existing test was extended in place, not a new one added). `ruff check`: all checks passed.
`mypy tests/test_cli.py`: 58 errors, same set as reported above (line numbers shifted by the
insertion, message set unchanged).
