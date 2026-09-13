# Report: §15.1 item 3, Wave 2 Batch 5 — mutation-proof cli.py::_gc_disk

## Scope

Touched ONLY `_gc_disk`'s test coverage in `tests/test_cli.py`. `src/fleet/cli.py` is unchanged
(restored byte-identical to `main` after the mutation experiment — verified with
`git diff --numstat --no-index`, zero diff).

## The fix under test (334edeb)

`334edeb` fixed an inverted `dry_run` condition in `_gc_disk`'s `free_bytes` computation:

```python
# before (bug):
free_bytes = disk_free_bytes(cache_dir) + (0 if dry_run else freed)
# after (fix):
free_bytes = disk_free_bytes(cache_dir) + (freed if dry_run else 0)
```

On a real run (`dry_run=False`), the eviction loop above has already unlinked the freed files by
the time `disk_free_bytes` is read, so it already reflects the reclaimed space — adding `freed`
again double-counts it, overstating how much room is free and weakening the §11.3 ENOSPC
preflight guard.

## Existing coverage did not discriminate this condition

`tests/test_cli.py` already has four tests exercising `_gc_disk`/`_require_disk_headroom` around
the disk floor (`test_a_phase_refuses_to_start_below_the_disk_floor_with_exit_9`,
`test_transform_refuses_to_start_below_the_disk_floor_with_exit_9`,
`test_the_floor_is_checked_even_when_there_is_no_cache_directory_to_evict`,
`test_a_reachable_floor_lets_the_phase_proceed`). All four run against an empty or absent
`cache_dir`, so `freed` is always `0` — under `0` either branch of the mutated conditional
evaluates the same, and none of the four can tell the fixed code from the reverted mutant. This
matches the brief's instruction to write a new test rather than assume existing coverage reaches
the line.

## New test

Added `test_gc_disk_real_run_free_bytes_excludes_the_already_reclaimed_space`
(`tests/test_cli.py`, calls `_gc_disk` directly with `dry_run=False`):

- Creates two 700 MB sparse files in `cache_dir` against `budgets.max_disk_gb: 1` (limit
  ≈1.074 GiB), forcing the eviction loop to evict exactly one file (`freed == 700_000_000`,
  `remaining == 700_000_000 <= limit`, so the `remaining > limit` branch of the raise is never
  the trigger).
- Monkeypatches `fleet.cli.disk_free_bytes` to a fixed `1_000_000`, with
  `preflight.min_free_bytes: 5000000` — only a wrongly re-added `freed` could push the reported
  `free_bytes` back over the floor.
- Asserts `DiskExhaustedError` IS raised, and that the message reports free_bytes as exactly
  `1000000` (not `701000000`).

## Mutation-proof procedure (Rule 12: backup / edit / diff-verify-nonempty / test / restore /
re-verify-identical)

1. **Backup**: `cli.py` copied to a per-lane scratch path before any edit.
2. **Edit**: reverted the fix in place — `free_bytes = disk_free_bytes(cache_dir) + (0 if
   dry_run else freed)`.
3. **Diff-verify-nonempty**: `git diff --numstat --no-index <backup> src/fleet/cli.py` → `1  1`
   (one line changed, confirming the mutation actually landed and the file wasn't corrupted).
4. **Test** (mutant):
   - New test: **FAILED** — `Failed: DID NOT RAISE DiskExhaustedError` (exactly the predicted
     failure mode: the mutant's inflated `free_bytes` clears the floor).
   - The four pre-existing disk-floor tests: all **4 passed** under the same mutant, confirming
     they are non-discriminating for this specific condition (as expected — not itself a defect
     in those tests, just proof the new test earns its place).
5. **Restore**: `cp` the backup back over `src/fleet/cli.py`.
6. **Re-verify-identical**: `git diff --numstat --no-index <backup> src/fleet/cli.py` → empty
   output (byte-identical).
7. Re-ran the same 5 tests (4 existing + 1 new) against the restored/original code:
   **5 passed**.

This is the old-passes/new-fails discriminator Rule 12 requires, on a mutation that was confirmed
to have actually changed the file (not a no-op).

## Verification (full)

- `ruff check src/fleet/cli.py tests/test_cli.py` → All checks passed.
- `ruff format --check src/fleet/cli.py tests/test_cli.py` → 2 files would be reformatted, but
  this is **pre-existing on `main`** (identical `ruff format --check` output measured against
  the primary checkout before any edit in this task) — unrelated to this change, not touched.
- `mypy` (whole package, no path args, per `pyproject.toml`'s `packages = ["fleet"]`) →
  `Success: no issues found in 132 source files`.
- Full `tests/test_cli.py` (no `-k` filter): **208 passed**.

## Result

- Status: **complete**.
- Branch: `agent/roundviii-mutation-batch5` (from `main`, worktree
  `/home/redmage/swe repo harness worktrees/wt-roundviii-mutation-batch5`).
- Files changed: `tests/test_cli.py` only (+49 lines, one new test). `src/fleet/cli.py`
  unchanged.
- Mutation result: new test kills the mutant (DID NOT RAISE), passes on original; 4 pre-existing
  related tests confirmed non-discriminating on the same mutant, unaffected by the added test on
  the original code.
- Not merged, not pushed (per brief — sequential batch, merged by controller before batch 6
  starts).
