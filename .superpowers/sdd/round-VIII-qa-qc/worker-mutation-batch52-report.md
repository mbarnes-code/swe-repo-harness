# Worker report: round VIII, §15.1 item 3, Wave 7.9 batch 52 (group G10b)

## Status: DONE

## Branch / commit
`agent/roundviii-mutation-batch52`, committed on top of `main` (`5de4485`). See commit for SHA
(`git log -1` in this worktree after commit). `src/fleet/cli.py` is byte-identical to `main` --
every mutation applied during Rule 12 proof was applied to the working tree and then restored
from a backup taken before the first mutation; `git status`/`git diff --stat` show only the new
test file.

## Scope

Group G10b, exactly as scoped: `_reset_stale_running`, `_unresolved`, `_recreate_phase_anchor`,
`_persist_arbitration`, `_demote_to_floors`, `_floor_reason`, `_apply_floor_demotions`.
`_reconcile_tasks_with_git` excluded (already proven per the brief).

New file: `tests/test_cli_resume_step345_batch52.py` (7 tests, one per function), following
batch 51's precedent of a dedicated new file rather than appending to `tests/test_cli.py`, to
avoid a merge collision with sibling lanes editing that file concurrently.

## Per-item results

For each function I audited every existing caller-reaching test (`tests/test_cli.py`'s step-3/
step-4/step-5 suites, `tests/test_d89_phase2_reconciliation.py`,
`tests/test_d89_phase2_claim_lifecycle.py`, `tests/test_reentry_evidence.py`,
`tests/test_resume_unblocking.py`, `tests/test_repository.py`, `tests/test_pr_e2e.py`,
`tests/test_heavy_tier_outage_e2e.py`) to find each function's branches, then picked the one
genuinely untested branch with no existing coverage (not merely "under-asserted") and wrote a
test that calls the function DIRECTLY (not through the full `fleet resume` CLI path), mirroring
`test_d89_phase2_reconciliation.py`'s own precedent of calling `_reconcile_tasks_with_git`
directly. This keeps each fixture to exactly the rows/args the function under test reads.

1. **`_reset_stale_running`** -- every existing test seeds exactly one stale `phases` row, so the
   SQL's lack of a `LIMIT`/per-repo scope is unexercised. New test seeds two stale rows across
   two repos and asserts BOTH are reclaimed in one sweep (both fences bumped, both leases
   cleared).
2. **`_unresolved`** -- the shared helper's `redact_text(reason)` call had no test proving
   redaction actually happens (every existing reason string is fixture-generated and matches no
   redaction pattern). New test passes a reason containing a fake `github_pat_...` token and
   asserts the reported entry carries the `«redacted:github_pat:...»` placeholder, not the raw
   token.
3. **`_recreate_phase_anchor`** -- every existing test seeds a RECOVERABLE anchor (a real,
   resolvable `pre_commit_sha`); nothing exercises the function's own `None` return for a
   genuinely unrecoverable one. New test covers both sub-cases of the `or` (`pre_commit_sha is
   None`, and a non-`None` SHA that doesn't resolve) and asserts neither call ever writes the
   ref.
4. **`_persist_arbitration`** -- every existing test (D87 fabricated-pointer test, every D89
   Phase 2 reconciliation case) seeds exactly one `attempts` row per task, so the `ORDER BY
   attempt DESC, revalidation_round DESC, retry_ordinal DESC LIMIT 1` tiebreak is never actually
   exercised. New test seeds two rungs (attempt 1 stale, attempt 2 live) and asserts the
   correction lands on attempt 2 while attempt 1 stays untouched.
5. **`_demote_to_floors`** -- no existing fixture reaches the `dest is None` branch (`"no
   destination resolves for this repo"`); every test uses "acme-commons"/"acme-billing", both
   resolvable. New test seeds a `phases` row for a repo absent from `repos` (so `_dest_paths`
   cannot resolve it) and asserts it lands in `unresolved` with the exact reason, untouched, with
   no computed floor.
6. **`_apply_floor_demotions`** -- the sibling `FloorSnapshotStaleError` branch is covered
   (`tests/test_cli.py::test_resume_step_5_refuses_a_floor_whose_phase_rows_moved_under_it`), but
   the `applied != plan` branch (a repo that acquired `REQUIRES_HUMAN_INTERVENTION` between the
   floor computation and the write, which `demote_to_floor` checks BEFORE ever comparing against
   `observed`) had no coverage. New test constructs the race directly: a live `REQUIRES_HUMAN_
   INTERVENTION` row against a stale `statuses` snapshot claiming `SUCCEEDED`, and asserts the
   repo is reported `unresolved` with nothing written and dropped from `demoted`.
7. **`_floor_reason`** -- no test asserts on the rendered reason STRING at all (only on the
   structured `evidence` dict); the `{read or 'none consulted'}` fallback for the hard-stop case
   (zero evidence gathered) was untested. New test calls the pure function directly with empty
   evidence and asserts the exact string.

## Rule 12 mutation proofs (all seven; manual, backup/edit/diff-verify/test/restore/re-verify)

Backed up `src/fleet/cli.py` once before starting, then for each function: applied one targeted
mutation, ran `diff` against the backup to confirm exactly one non-empty hunk, ran the new test
(RED), ran the relevant existing control test(s) (GREEN, confirming they cannot express the
defect), restored from the backup, and re-diffed to confirm byte-identical restoration before
moving to the next mutation.

| # | Function | Mutation | New test | Control test(s) | Control result |
|---|---|---|---|---|---|
| 1 | `_reset_stale_running` | appended `" LIMIT 1"` to the step-3 UPDATE | RED (`reclaimed == 1`, not 2) | `test_resume_reclaims_a_stale_lease_without_charging_an_attempt` | GREEN |
| 2 | `_unresolved` | `redact_text(reason)` -> bare `reason` | RED (raw token in report) | n/a (no existing test's reason matches a pattern) | -- |
| 3 | `_recreate_phase_anchor` | dropped the `git.resolve(pre_commit_sha) is None` half of the guard | RED (`GitCommandError` from `update_ref` on a bogus SHA, exactly as predicted) | 8 tests under `-k resume_step4` | GREEN |
| 4 | `_persist_arbitration` | `ORDER BY attempt DESC` -> `ORDER BY attempt ASC` | RED (attempt 1 corrected instead of attempt 2) | `test_resume_step4_corrects_a_fabricated_attempts_commit_sha_pointing_off_branch`, `test_resume_step4_reports_a_landed_commit_no_attempts_row_could_record`, all 8 of `tests/test_d89_phase2_reconciliation.py` | GREEN |
| 5 | `_demote_to_floors` | `if dest is None:` -> `if False:` | RED -- but NOT the failure mode my first docstring draft predicted (`AttributeError`); actual measured failure is `sqlite3.IntegrityError: FOREIGN KEY constraint failed` (the finding insert references a `repo_id` absent from `repos`). Docstring corrected to the measured failure per CLAUDE.md's "never pass an unmeasured number/claim" discipline. | 10 tests under `-k resume_step_5` | GREEN |
| 6 | `_apply_floor_demotions` | `if applied != plan:` -> `if False:` | RED (`report["demoted"]` kept the entry that never applied) | 10 tests under `-k resume_step_5` (includes the `FloorSnapshotStaleError` sibling test) | GREEN |
| 7 | `_floor_reason` | `{read or 'none consulted'}` -> `{read}` | RED (trailing `.` instead of `none consulted.`) | 10 tests under `-k resume_step_5` | GREEN |

Note on #5: the first draft of that test's docstring guessed the wrong exception type before
running the mutation. Per CLAUDE.md's measurement discipline, I re-measured and corrected the
docstring to name the actual observed failure (`IntegrityError`) rather than leaving the
unmeasured guess in a committed file.

## Which test files I ran for verification, and why they're a sufficient covering set

- **`tests/test_cli_resume_step345_batch52.py`** (new, this batch): the 7 new tests, run directly
  against every mutation and against the restored baseline. This is the primary evidence -- each
  test executes the exact changed line of its target function (confirmed by the RED result under
  that function's own mutation).
- **`tests/test_cli.py -k resume_step4`** (8 tests) and **`-k resume_step_5`** (10 tests): these
  are the existing caller-level suites for step 4 (`_recreate_phase_anchor`,
  `_persist_arbitration` are reached from `_reconcile_tasks_with_git`, which these drive through
  the real `fleet resume` CLI path) and step 5 (`_demote_to_floors`, `_floor_reason`,
  `_apply_floor_demotions`). Run as controls under every mutation to confirm they do NOT
  discriminate the defects I found (proving the gap was real, not merely unexercised by my
  search) and to confirm no regression after restoring.
- **`tests/test_d89_phase2_reconciliation.py`** (8 tests) and
  **`tests/test_d89_phase2_claim_lifecycle.py`**: the other direct-call suite reaching
  `_persist_arbitration` via `_reconcile_tasks_with_git`; run as an additional control for
  mutation #4 (confirmed GREEN, single-attempts-row fixtures cannot discriminate) and as a
  regression check on the restored baseline.
- Did not run the full ~15-minute suite for this batch: the touched surface is one new,
  self-contained test file with zero production-code changes (`git diff --stat` against `main`
  shows nothing under `src/`), so the covering set above (functions' own new tests + every
  existing test that reaches the same call chain) is sufficient to prove both "the new tests
  discriminate real gaps" and "nothing regressed."

## mypy / ruff

- `ruff check tests/test_cli_resume_step345_batch52.py`: all checks passed (after two line-length
  fixes during authoring).
- `mypy tests/test_cli_resume_step345_batch52.py`: zero errors attributed to this file. The
  command reports pre-existing errors in `tests/test_cli.py`, `tests/test_migrations.py`, and
  `tests/test_scan_e2e.py` (unrelated files this batch did not touch) because `pyproject.toml`'s
  `mypy_path` resolves the whole test tree from run `cwd`; verified none of them cite the new
  file.

## Report path

`.superpowers/sdd/round-VIII-qa-qc/worker-mutation-batch52-report.md` (this file).

## CORRECTION (dated 2026-09-15, filed after independent review)

This is an appended correction, not a rewrite — the original text above (including the item-1 row
of the mutation-proofs table and the "Which test files I ran" section) is left exactly as
originally written, and is factually **wrong** in the two respects corrected below. **This was
caught by review** (`.superpowers/sdd/round-VIII-qa-qc/review-mutation-batch52-report.md`), not by
me, and unlike the item-5 `IntegrityError` self-correction in the body above (which I caught and
fixed before committing), I did not catch this one myself. I have since independently re-run the
mutation in a properly interpreter-pinned worktree (this same branch's worktree,
`sys.executable`/`fleet.__file__` confirmed to resolve under
`/home/redmage/swe repo harness worktrees/wt-roundviii-mutation-batch52` before trusting any
result) and confirmed both of the reviewer's findings reproduce exactly.

**What the original report claimed (item 1, `_reset_stale_running`, the `" LIMIT 1"` mutation):**

> RED (`reclaimed == 1`, not 2)

and the control:

> `test_resume_reclaims_a_stale_lease_without_charging_an_attempt` ... GREEN

**What is actually measured, independently reproduced:**

- The mutation as literally coded (appending `" LIMIT 1"` to the end of the full concatenated
  expression `_RESET_RUNNING_TO_PENDING_SQL + _STALE_HEARTBEAT_PREDICATE` at the call site,
  cli.py:18284) is syntactically valid SQL and *does* produce `reclaimed == 1` — that is what my
  original manual test actually exercised and is not itself wrong.
- But that is **not the mutation the prose in this report's own docstring describes**
  ("appending `LIMIT 1` to `_RESET_RUNNING_TO_PENDING_SQL`"), which reads naturally as mutating
  the named constant itself (cli.py:16708-16712) rather than the call-site concatenation. The
  reviewer applied the mutation as the prose describes it — appending `" LIMIT 1"` directly onto
  `_RESET_RUNNING_TO_PENDING_SQL`'s own closing quote, i.e. *before* `_STALE_HEARTBEAT_PREDICATE`
  is concatenated on — and that is the version I re-ran and confirmed:

  ```
  _RESET_RUNNING_TO_PENDING_SQL: Final = (
      "UPDATE phases SET status = 'PENDING', lease_owner = NULL, heartbeat_at = NULL, "
      "       lease_expires_at = NULL, lease_fence = lease_fence + 1, updated_at = ? "
      " WHERE run_id = ? AND status = 'RUNNING' LIMIT 1"
  )
  ```

  Concatenated with `_STALE_HEARTBEAT_PREDICATE` (`" AND heartbeat_at IS NOT NULL AND ..."`), the
  resulting text is `... WHERE run_id = ? AND status = 'RUNNING' LIMIT 1 AND heartbeat_at IS NOT
  NULL AND ...`. SQLite's `LIMIT` clause accepts an arbitrary expression as its value (this build
  has `ENABLE_UPDATE_DELETE_LIMIT` compiled in — confirmed via `PRAGMA compile_options`), so `1
  AND heartbeat_at IS NOT NULL AND ...` is parsed as ONE boolean expression standing in for the
  limit value, evaluated with no row/column context available — hence the error names a column
  (`heartbeat_at`) that is real in the table but unreachable from where the parser is evaluating
  it, rather than silently limiting the UPDATE to one row.
- Independently reproduced, running my own new test against this exact mutation (backup → edit →
  `diff --no-index` confirmed one non-empty hunk → test → restore → `diff`/`sha256sum` confirmed
  byte-identical restoration):

  ```
  sqlite3.OperationalError: no such column: heartbeat_at
  ```

  — exactly the reviewer's claimed failure mode, not `reclaimed == 1`.
- Independently reproduced the control-test claim as well, running
  `tests/test_cli.py::test_resume_reclaims_a_stale_lease_without_charging_an_attempt` (the real
  `fleet resume --no-continue` CLI path) against the same mutation:

  ```
  AssertionError: assert 1 == <ExitCode.SUCCESS: 0>
   +  where 1 = <Result OperationalError('no such column: heartbeat_at')>.exit_code
  ```

  The control test goes **RED**, not GREEN as originally claimed. This is because the mutated SQL
  is broken for *every* invocation of `_reset_stale_running` — it is a SQL syntax/evaluation
  defect, not a row-count defect — so it cannot discriminate "does the sweep reclaim more than one
  stale row" (the property my new test targets) from "is this SQL well-formed at all." Any test
  that reaches `_reset_stale_running` at all goes red under this particular mutation, single-row
  fixture or not.

**Why the two mutations differ:** the call-site concatenation order matters. Appending `" LIMIT
1"` strictly *after* the full concatenated `_RESET_RUNNING_TO_PENDING_SQL + _STALE_HEARTBEAT_
PREDICATE` string (what my terminal session actually ran) puts the `LIMIT` clause last, which is
syntactically the normal place for it and works as an ordinary row-limiting `LIMIT`. Appending it
onto the `_RESET_RUNNING_TO_PENDING_SQL` constant's own last line (what the report's prose
describes, and what the reviewer correctly applied on that reading) puts the `LIMIT` clause in the
*middle* of the final concatenated string, before the predicate, which is a different and broken
mutation. I did not notice this discrepancy between what I wrote and what I ran before committing
the original report.

**Does this affect the new test's coverage value? No.** The underlying gap the new test
(`test_reset_stale_running_reclaims_every_stale_row_in_one_sweep_not_only_the_first`) proves —
that no existing fixture before this batch seeded two simultaneously-stale `phases` rows, so a
regression that silently scoped the sweep to one row would have passed every existing test — is
still real and still correctly demonstrated by that test's own assertions on the unmutated code
path. What is wrong is only this report's *specific claimed mutation outcome and control result*
for item 1, which I have now corrected above to what is actually measured. Per the reviewer's
Minor note, the "Which test files I ran for verification, and why they're a sufficient covering
set" section's framing for item 1 — that the control test "confirm[s] they do NOT discriminate the
defect I found" — **also does not hold for this specific mutation** (the control goes red for a
different, non-discriminating reason: broken SQL, not a preserved single-row scope) and should be
read as superseded by this correction rather than relied upon. No sound mutation that (a)
redistributes the reclaim to a single row and (b) leaves the single-row control passing was found
by either the reviewer or myself; none was required to close this finding, only this correction to
the report's claimed measurements.
