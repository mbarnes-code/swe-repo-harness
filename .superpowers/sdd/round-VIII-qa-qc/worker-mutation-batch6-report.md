# Report: §15.1 item 3, Wave 2 Batch 6 — mutation-proof cli.py::_abort_impl

## Scope

Touched only `_abort_impl` (read/analysis) in `src/fleet/cli.py` — no production code changes
were needed (see below). No test file changes were needed either. `src/fleet/cli.py` and
`tests/test_cli.py` are both unchanged from `main` (verified with `git diff --numstat`, zero
diff, after the mutation experiment restore).

## The fix under test (334edeb)

`334edeb` fixed `cli.py::_abort_impl`: `--drain` behaved identically to `--now`, discarding
in-flight work instead of waiting up to `budgets.wave_drain_timeout_s` for RUNNING phases to
finish naturally. The fix added:

- `_ABORT_DRAIN_POLL_S` (module constant) and `_count_running_phases()` (helper) above
  `_abort_impl`.
- Inside `_abort_impl`, after recording the abort finding and before the immediate
  reset-to-PENDING unit, a new `if drain:` block that polls `_count_running_phases` in a loop
  until either zero RUNNING phases remain or `budgets.wave_drain_timeout_s` elapses.

`--now` takes no branch here at all and falls straight through to the immediate reset, as before.

## Existing coverage already discriminates this condition

`tests/test_cli.py` already carries two tests written expressly for this fix (both present on
`main` before this task started):

- `test_abort_checkpoints_and_regenerates_the_projection` — uses `ABORT_DRAIN_YAML`
  (`budgets.wave_drain_timeout_s: 1`), puts a repo phase into RUNNING that never naturally
  resolves, invokes plain `fleet abort` (drain defaults on), and asserts `elapsed >= 1.0`: a real
  drain must burn roughly the configured 1s before the reset lands. Its own docstring names the
  exact defect this guards: "`--drain` behaving exactly like `--now`."
- `test_abort_now_skips_the_drain_wait` — same RUNNING setup, `fleet abort --now`, asserts
  `elapsed < 5.0` against the shipped 900s default, proving `--now` takes no wait at all.

Unlike batch 5's `_gc_disk` finding (existing tests fixed the input that made both branches of the
mutated conditional evaluate identically), these two tests directly exercise the drain/now
distinction via wall-clock timing, so they were checked rather than assumed sufficient — and they
are.

## Mutation-proof procedure (Rule 12: backup / edit / diff-verify-nonempty / test / restore /
re-verify-identical)

1. **Backup**: `cli.py` copied to a per-lane scratch path before any edit.
2. **Edit**: reverted the fix in place — changed the guard from `if drain:` to `if False and
   drain:`, so the drain wait loop is never entered regardless of the `--drain`/`--now` flag
   (collapsing both to the pre-fix "always reset immediately" behavior, without touching
   `_count_running_phases`/`_ABORT_DRAIN_POLL_S`, which stay dead code under the mutant).
3. **Diff-verify-nonempty**: `git diff --numstat -- src/fleet/cli.py` → `3  4` (mutation actually
   landed).
4. **Test** (mutant), scoped to the two abort tests:
   - `test_abort_checkpoints_and_regenerates_the_projection` — **FAILED**: `AssertionError: a
     real drain of 1s must be observed, took 0.055s` (exactly the predicted failure mode — the
     mutant never waits).
   - `test_abort_now_skips_the_drain_wait` — **PASSED** (expected: `--now` never exercised the
     drain branch even pre-mutation, so it cannot discriminate this particular mutant; it is
     `test_abort_checkpoints_and_regenerates_the_projection` that carries the discriminating
     power here).
5. **Restore**: copied the backup back over `src/fleet/cli.py`.
6. **Re-verify-identical**: `git diff -- src/fleet/cli.py` → empty output (byte-identical to
   pre-mutation).
7. Re-ran the same two tests against the restored/original code: **2 passed**.

This is the old-passes/new-fails discriminator Rule 12 requires, on a mutation confirmed to have
actually changed the file (not a no-op), with no test-file changes required because the existing
coverage already kills the mutant.

## Verification (full)

- `ruff check src/fleet/cli.py tests/test_cli.py` → All checks passed.
- `ruff format --check src/fleet/cli.py tests/test_cli.py` → 2 files would be reformatted, but
  this is **pre-existing on `main`** (same finding as batch 5's report) — unrelated to this task,
  untouched.
- `mypy` (whole package, no path args, per `pyproject.toml`'s `packages = ["fleet"]`) →
  `Success: no issues found in 132 source files`.
- Targeted run `tests/test_cli.py -k "test_abort_checkpoints_and_regenerates_the_projection or
  test_abort_now_skips_the_drain_wait"`: 2 passed (both before mutation and after restore).

## Result

- Status: **complete**.
- Branch: `agent/roundviii-mutation-batch6` (from `main`, worktree
  `/home/redmage/swe repo harness worktrees/wt-roundviii-mutation-batch6`).
- Files changed: none in `src/` or `tests/` — existing coverage for `_abort_impl` already proved
  mutation-proof. This report is the only tracked-tree change.
- Mutation result: `test_abort_checkpoints_and_regenerates_the_projection` kills the mutant
  (drain-vs-now distinction collapsed) and passes on the original; the sibling `--now` test is
  confirmed non-discriminating for this specific mutant (expected — it never enters the drain
  branch either way) but unaffected.
- Not merged, not pushed (per brief — sequential batch, merged by controller before batch 7
  starts).
