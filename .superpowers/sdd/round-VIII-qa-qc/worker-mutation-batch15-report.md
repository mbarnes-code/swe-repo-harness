# Worker report: round VIII, Wave 4, batch 15 — mutation-proof v002–v010

## Scope

Bucket A− (`worker-mutation-scope-report.md`): `v002_node_kind.py`, `v003_anti_anchoring.py`,
`v004_stub_lifecycle.py`, `v005_backend_identity.py`, `v006_mutations_deleted.py`,
`v009_coordinate_version.py`, `v010_file_blobs.py` — each previously exercised only
transitively via the full `migrate(db, steps=STEPS)` ladder in `tests/test_migrations.py`, with
no per-step discriminator.

## What was found already covered

`v002_node_kind` already had a genuine per-step discriminator:
`test_the_first_rung_renames_columns_without_touching_rows` (line ~965) runs
`migrate(db, steps=STEPS[:1])` against a hand-built `user_version=1` fixture and asserts the
post-step column names/values. No new test was added for it; instead it was used as one of the
three Rule-12 genuine-mutation proofs below (see "Mutation results").

## What was added

Six new tests in `tests/test_migrations.py`, one per remaining step, each following the existing
file's established shape (a minimal hand-built fixture at `user_version = step.version - 1`,
`migrate(db, steps=STEPS[i:i+1])`, before/after assertions):

| Step | Test | What it proves per-step (not just "ladder didn't crash") |
|---|---|---|
| v003 | `test_the_second_rung_adds_anti_anchoring_columns_and_the_rejected_table` | back-fill defaults on a pre-existing `attempts`/`llm_cache` row (`approach_signature=''`, `rejected_approach_digest=sha256('')`); `rejected_approaches`'s `attempt BETWEEN 1 AND 3` CHECK is real (insert with `attempt=4` raises `IntegrityError`), not just a same-named table |
| v004 | `test_the_third_rung_adds_stub_lifecycle_and_renames_the_provider_column` | `stub_repo_id` is genuinely gone (not merely superseded) after rename to `provider_repo_id`; `consumer_repo_id` is back-filled from `repo_id` on the pre-existing row in the same transaction as the ALTER; `tasks.revalidation_key` added |
| v005 | `test_the_fourth_rung_adds_backend_identity_columns` | `llm_cache` back-fill defaults (`tier='WORKHORSE'`, `backend='anthropic'`, `structured_output_mode='JSON_SCHEMA'`) land on the pre-existing row; `attempts.llm_backend` stays NULL (nullable, no default) while `llm_failovers` defaults to 0 |
| v006 | `test_the_fifth_rung_deletes_the_mutations_journal_and_backfills_base_ref` | `base_ref` back-fill is **conditional** — a phase row with `pre_commit_sha IS NOT NULL` gets the ref string, a row with NULL stays NULL; `mutations` table is actually dropped, not just superseded |
| v009 | `test_the_eighth_rung_adds_coordinates_version_nullable_no_backfill` | `coordinates.version` is NULL (not `''` or `0`) for a pre-existing row — additive-only, no backfill, exactly as the docstring claims |
| v010 | `test_the_ninth_rung_creates_file_blobs_matching_the_baseline` | the migrated `file_blobs` shape is compared column-for-column against a **freshly baselined** database's `file_blobs` (via `_table_shape`), so a hand-copied/stale DDL would diverge even though "table exists" would pass |
| v010 | `test_the_ninth_rung_refuses_to_run_twice` | the module's own re-run guard (`SqlTextError` if `file_blobs` already exists) is exercised directly against `upgrade()` — real Python logic, not DDL, independent of the runner's `user_version` gate |

All 8 relevant tests pass; full `tests/test_migrations.py` run: **34 passed**.

## Mutation results (Rule 12 full discipline: backup → edit → diff-verify-nonempty → test →
restore → diff-verify-identical)

Backups taken to `/tmp/.../scratchpad/batch15-mutations/*.bak` before editing. Each mutation
verified non-vacuous via `git diff --numstat --no-index <backup> <mutated>` (1 changed line each,
confirmed before trusting any test result), then `pytest tests/test_migrations.py -k <target>`
run, then the file restored via `cp` from backup and re-verified byte-identical via `cmp`.

1. **v002** — changed `edges.src_kind`'s `DEFAULT 'REPO'` to `DEFAULT 'NODE'`.
   → `test_the_first_rung_renames_columns_without_touching_rows` **reddened**
   (`('repo-a', 'repo-b', 'NODE', 'REPO', None) != (..., 'REPO', 'REPO', ...)`).
2. **v006** — removed the `WHERE pre_commit_sha IS NOT NULL` guard from the `base_ref` back-fill
   `UPDATE`, making it unconditional.
   → `test_the_fifth_rung_deletes_the_mutations_journal_and_backfills_base_ref` **reddened**
   (the NULL-`pre_commit_sha` row got a `base_ref` it must not have).
3. **v010** — inverted the re-run guard's condition (`if _support.table_exists(...)` →
   `if not _support.table_exists(...)`), so it raises when the table is absent and silently
   no-ops when it already exists.
   → `test_the_ninth_rung_refuses_to_run_twice` **reddened** (`DID NOT RAISE SqlTextError`).

In all three cases exactly the targeted test(s) failed (3 failed total across all three separate
mutation runs, 1 per mutation) — no other test in the file was affected, confirming each
discriminator is specific to its own step. After restoring each file: `cmp` reported byte-identical
to the pre-mutation backup, and the full suite re-ran green (34 passed).

## Verification

- `ruff check tests/test_migrations.py` — clean.
- `ruff format --check tests/test_migrations.py` — reports pre-existing hunks; confirmed by
  running the same check against `main`'s unmodified copy of the file (also reports hunks, at
  different, pre-existing lines) that this is **pre-existing formatting debt in the file, not
  something introduced by this change** (also consistent with `docs/CRITERIA_PLAN.md` §2's
  disclosed whole-repo `ruff format` drift, ADR-0116). New lines follow the file's existing
  hand-wrapped style rather than reformatting untouched code (Rule 3).
- `python -m mypy` (no path args, manifest-scoped): `Success: no issues found in 132 source
  files`. `pyproject.toml`'s `[tool.mypy]` scopes to `packages = ["fleet"]` (i.e. `src/fleet`
  only) — `tests/` is outside mypy's configured scope, so this is the correct full check for the
  one file touched.
- `git status`/`git diff --stat` in the worktree: only `tests/test_migrations.py` changed (223
  insertions, 0 deletions); the three migration source files used for mutation testing are
  confirmed restored byte-identical.

## Not merged, not pushed

Committed on `agent/roundviii-mutation-batch15`, branched from `main` at `c53c6f7`. No merge, no
push.
