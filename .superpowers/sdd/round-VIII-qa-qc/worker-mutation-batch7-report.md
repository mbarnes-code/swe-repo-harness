# Report: §15.1 item 3, Wave 2 Batch 7 — mutation-proof cli.py::_quarantine_impl

## Scope

Touched only `_quarantine_impl` (read/analysis) in `src/fleet/cli.py` and its tests in
`tests/test_cli.py`. No production code changes were needed — the existing tests did not
discriminate this fix, so one new test was added. `src/fleet/cli.py` is unchanged from `main`
(verified with `git diff --numstat`, zero diff, after the mutation experiment restore).

## The fix under test (334edeb)

`334edeb` fixed `cli.py::_quarantine_impl`'s SKIPPED write: no status guard / lease-fence bump on
the `UPDATE phases SET status = 'SKIPPED', ...`. `_quarantine_impl` reads each phase's status
ONCE via a separate read-only connection and validates the SKIPPED move through `transition()`
against that stale read. A live worker can claim the lease and move a phase to RUNNING in the
window between that read and the later write inside `StateWriter`; RUNNING has no SKIPPED edge in
`ALLOWED_TRANSITIONS` (only a crash sweep may move it, and only to PENDING). The fix:

- Added `AND status != 'RUNNING'` to the `UPDATE ... WHERE` clause, so the write no longer trusts
  the stale read.
- Added `lease_owner = NULL, lease_expires_at = NULL, lease_fence = lease_fence + 1` to the SET
  clause for the rows that DO move, so a live worker's own later fenced write
  (`_complete`/`_record_diagnostics`) is invalidated instead of silently overwriting the operator's
  verdict — mirroring `_RESET_RUNNING_TO_PENDING_SQL`/`demote_to_floor`/`clear_blocked_by`/
  `_raise_run_ceiling` elsewhere in the file.
- Changed the return value from `len(movable)` (a count from the stale read) to
  `int(cursor.rowcount)` (the actual number of rows the guarded UPDATE touched).

## Existing coverage does NOT discriminate this fix

`tests/test_cli.py` carried four pre-existing `quarantine` tests
(`test_quarantine_writes_a_finding_and_skips_without_touching_config`,
`test_quarantine_requires_a_reason`, `test_quarantine_propagates_blocked_by_to_dependents`,
`test_quarantine_dry_run_changes_nothing`). None of them puts a phase into RUNNING at the moment
of the UPDATE, and none reads `lease_fence`/`lease_owner` afterward — all four run the ordinary
single-threaded "no race" path, where the guard and the fence bump are both no-ops relative to
`skipped`/`status` assertions the tests actually make. Confirmed empirically below (all four still
pass identically under the reverted mutant).

Reproducing the actual race genuinely — rather than pre-seeding a RUNNING row, which `transition()`
would refuse before the guarded UPDATE is ever reached, raising before any write happens — needs a
hook between the initial read and the later write. `ordering_descendants` runs synchronously in
exactly that window (after `phase_rows` is read and `transition()` validated, before `StateWriter`
opens), so the new test (`test_quarantine_never_overwrites_a_phase_that_started_running_after_the_read`)
monkeypatches `cli.ordering_descendants` to first flip the row to RUNNING (simulating a worker
claiming the lease) via a second, autocommit `sqlite3` connection, then delegate to the real
function — the same class of connection-level monkeypatch
`test_retry_translates_a_repository_error_into_a_usage_error` already uses in this file for an
equivalent TOCTOU it cannot reach directly through the CLI.

## Mutation-proof procedure (Rule 12: backup / edit / diff-verify-nonempty / test / restore /
re-verify-identical)

1. **Backup**: `src/fleet/cli.py` copied to a per-lane scratch path before any edit.
2. **Edit**: reverted the fix in place — restored the pre-334edeb UPDATE (`status = 'SKIPPED',
   updated_at = ?` with no `lease_owner`/`lease_expires_at`/`lease_fence` in SET, no `AND status
   != 'RUNNING'` in WHERE) and the pre-fix `return len(movable)`.
3. **Diff-verify-nonempty**: `git diff --no-index --numstat <backup> <mutated>` → `4  6` (mutation
   actually landed, not a no-op).
4. **Test** (mutant), scoped to `quarantine`:
   - New test `test_quarantine_never_overwrites_a_phase_that_started_running_after_the_read` —
     **FAILED**: `AssertionError: ... assert 'SKIPPED' == 'RUNNING'` — the mutant silently
     overwrote the worker's RUNNING phase with SKIPPED, exactly the pre-334edeb defect.
   - The four pre-existing `quarantine` tests — **all still PASSED** under the mutant, confirming
     they do not discriminate this fix (as predicted above).
5. **Restore**: copied the backup back over `src/fleet/cli.py`; `diff -q` confirmed byte-identical.
6. **Re-verify-identical**: re-ran all 7 `quarantine`-scoped tests against the restored/original
   code: **7 passed** (including the new test).
7. Ran the full `tests/test_cli.py` file against the restored original: **209 passed**.

This is the old-passes/new-fails discriminator Rule 12 requires, on a mutation confirmed to have
actually changed the file (not a no-op), with the new test as the sole discriminator — the four
pre-existing tests are non-discriminating for this defect but remain green throughout, so nothing
regressed.

## Verification (full)

- `ruff check src/fleet/cli.py tests/test_cli.py` → All checks passed.
- `ruff format --check src/fleet/cli.py tests/test_cli.py` → 2 files would be reformatted, but this
  is **pre-existing on `main`** (confirmed by running the same check against the unmodified
  checkout at `/home/redmage/swe repo harness`, same two files, same finding) — unrelated to this
  task, untouched, and none of the reformatting hunks fall inside the new test or inside
  `_quarantine_impl`.
- `mypy` (whole package, no path args, per `pyproject.toml`'s `packages = ["fleet"]`) →
  `Success: no issues found in 132 source files`.
- `pytest tests/test_cli.py -k quarantine` → 7 passed (original code).
- `pytest tests/test_cli.py` (whole file, no `-k`) → 209 passed (original code).

## Result

- Status: **complete**.
- Branch: `agent/roundviii-mutation-batch7` (from `main`, worktree
  `/home/redmage/swe repo harness worktrees/wt-roundviii-mutation-batch7`).
- Files changed: `tests/test_cli.py` only (one new test, +67 lines). `src/fleet/cli.py` unchanged
  from `main`.
- Mutation result: new test `test_quarantine_never_overwrites_a_phase_that_started_running_after_the_read`
  kills the mutant (status guard / lease-fence bump reverted) and passes on the original; the four
  pre-existing `quarantine` tests are confirmed non-discriminating for this specific mutant
  (expected — none of them exercises the race window) but unaffected and still green throughout.
- Not merged, not pushed (per brief — sequential batch, merged by controller before batch 8
  starts).
