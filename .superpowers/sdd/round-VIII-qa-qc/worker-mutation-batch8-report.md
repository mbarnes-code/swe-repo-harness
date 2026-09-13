# Worker report: §15.1 item 3, Wave 2 Batch 8 — mutation-proof `cli.py::_apply_stub_decisions`

## Scope
Touched only `_apply_stub_decisions`'s test coverage in `tests/test_cli.py`. `src/fleet/cli.py`
is unmodified in the final commit (the fix hunk was reverted, tested, and restored byte-identical
as part of the mutation proof — see below).

## What `334edeb` fixed
`_apply_stub_decisions`'s per-decision `UPDATE stubs ... WHERE revalidation_round = ? AND state =
?` is a CAS against a `records` snapshot read moments earlier over a separate connection. Before
the fix, the function ignored `cursor.rowcount` entirely: if a concurrent writer (another `fleet
resume`/`--sync`, or a REVALIDATE claim) had already moved that exact `(consumer, coord_key)`
row's `revalidation_round`/`state` between the read and this write, the CAS silently matched zero
rows and the function still reported the consumer as touched, wrote the decision's finding, and
(for T1) cleared the paired `UnmergedDependency` finding — a no-op transition reported as success.
The fix adds `if cursor.rowcount == 0: continue`, skipping the finding writes and the
`consumer_ids` append for that decision.

## Existing-test discrimination check
Confirmed existing tests do **not** discriminate this mutation (matching the pattern the brief
flagged in 2 of the prior 3 cli.py batches). With the rowcount check reverted, ran the full
`-k stub` subset of `tests/test_cli.py` (24 tests before my addition): all 23 existing stub tests
still PASSED under the mutation — none of them construct the concurrent-writer race the fix
guards against, so the CAS always matches in those fixtures (single sequential write, no second
writer). Only the new test below discriminates.

## New test
`test_apply_stub_decisions_reports_no_touched_consumer_on_cas_race` (`tests/test_cli.py`, inserted
after `test_resume_stub_reconcile_never_moves_a_repo_out_of_requires_human_intervention`, before
the `# ---- --raise-budget ----` section).

Drives `_apply_stub_decisions` directly (not through either production caller) so the race is
constructed rather than hoped for:
1. `_put_stub` seeds a `stubs` row: `acme-commons` -> `acme-billing@1.0.0`, `ACTIVE`,
   `revalidation_round=0`.
2. `original` (a `StubRecord`) is built to match that snapshot (`rounds_spent=0`).
3. A second, independent `sqlite3` connection bumps the live row's `revalidation_round` to `1`
   — exactly what a REVALIDATE claim's own write would do — *between* the snapshot and the call.
4. `_apply_stub_decisions` is invoked (via `StateWriter`) with a T4 decision
   (`ACTIVE -> ABANDONED`, `abandon_reason=END_OF_RUN`, `finding=UnresolvedStub`) built from the
   stale `original`.

Assertions: `consumer_ids == []` (not reported as touched); the live row is unchanged from what
the concurrent writer left it (`state="ACTIVE"`, `revalidation_round=1`); no `UnresolvedStub`
finding was written.

## Mutation proof (Rule 12: backup / edit / diff-verify-nonempty / test / restore / re-verify)
1. **Backup**: `cli.py` copied to a lane-scratch dir before any edit.
2. **Mutation**: removed the `if cursor.rowcount == 0: continue` block (8 lines) from
   `_apply_stub_decisions`, reverting exactly the `334edeb` fix hunk for this function.
3. **Diff-verify-nonempty**: `git diff --stat -- src/fleet/cli.py` → `1 file changed, 8
   deletions(-)` (non-empty, confirms the mutation actually changed the file).
4. **Test — mutant**: `pytest tests/test_cli.py -k
   test_apply_stub_decisions_reports_no_touched_consumer_on_cas_race` → **FAILED**:
   `AssertionError: ... the consumer must not be reported as touched, got ['acme-commons']` (fails
   on the first, discriminating assertion — a genuine behavior change, not a module-load crash).
   Also ran the full `-k stub` subset under the mutant: 23 passed / 1 failed (only the new test
   reddens — confirms the existing-test non-discrimination finding above under the *same* mutant
   run).
5. **Restore**: copied the backup back over `src/fleet/cli.py`.
6. **Re-verify-identical**: `diff -q backup src/fleet/cli.py` → identical (no output); `git diff
   --stat -- src/fleet/cli.py` → empty.
7. **Test — original**: `pytest tests/test_cli.py -k
   test_apply_stub_decisions_reports_no_touched_consumer_on_cas_race` → **1 passed**.
   `pytest tests/test_cli.py -k stub` → **24 passed**.

Old-passes/new-fails is proven on the exact mutation reverting the CAS rowcount check: the
original passes, the mutant fails on the discriminating assertion, and the mutant genuinely
changed the file (non-empty diff) without destroying the module (only the target test reddened;
23 sibling stub tests stayed green under the same mutant run, ruling out a module-wide outage
masquerading as a discriminator).

## Static checks
- `ruff check src/fleet/cli.py tests/test_cli.py` → all checks passed (after fixing two pre-existing
  E501 line-length violations introduced by my own first draft: shortened the test function name
  and rewrapped its docstring).
- `mypy src/fleet/cli.py` → Success: no issues found.
- `mypy tests/test_cli.py` → 57 pre-existing errors, all outside my inserted test's line range
  (4249-4351) and unrelated to `_apply_stub_decisions`/`StubRecord`/`StubDecision` (e.g.
  `ExitCode` literal-overlap checks around lines 6356-9097, pre-existing on `main`). Zero new
  errors introduced by this change.

## Result
- Branch: `agent/roundviii-mutation-batch8` (from `main` at `16e55df`).
- Commit: contains only `tests/test_cli.py` (+112 lines, one new test function).
- `src/fleet/cli.py`: unmodified (mutation applied and fully reverted during the proof; final
  working tree is byte-identical to `main`).
- Not merged, not pushed, per brief.
