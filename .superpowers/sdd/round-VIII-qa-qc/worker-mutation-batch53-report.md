# Worker report: round VIII, §15.1 item 3, Wave 7.9 batch 53 (group G10c)

Status: **DONE**

Branch: `agent/roundviii-mutation-batch53`
Commit: `a3af41c6cb48402a988464de7c9cbba4b1c8ac61`
Worktree: `../swe repo harness worktrees/wt-roundviii-mutation-batch53`, based on `main` at `5de4485`.

## Scope

Group G10c per the brief: `UnblockLookupError`, `_blocker_states`, `_read_blocker_states`,
`_resolvable_repo_ids`, `_refuse_unresolved_blockers`, `_unblocking_entry`, `_blocker_resolver`,
`_unblock_dependents`, `_unblock_lines` in `src/fleet/cli.py`. `_apply_unblocking` excluded per
brief (already proven).

New file: `tests/test_cli_unblock_lines_batch53.py` (15 tests). No production code changed.

## Method

`tests/test_resume_unblocking.py` already drives the whole of §11.5 step 6 end to end through a
real `fleet resume` invocation (it is `docs/SPEC.md` §12 item 46(ii)'s acceptance test), so it is
this batch's primary reaching test and is not duplicated. For each of the 9 in-scope names, I read
the function, read every test file that mentions or could reach it (`grep` census below), and
where an existing e2e assertion could not discriminate a plausible one-line mutation, wrote a
direct/isolated call against the function itself — following the precedent already in this tree at
`tests/test_cli_resume_report_lines_batch51.py` (batch 51, G10a) and, further back,
`tests/test_d89_phase2_reconciliation.py:621` (`_arbitration_lines` called directly).

For every new test, I applied the discriminating mutation directly to `src/fleet/cli.py` from a
byte-identical backup (`/tmp/.../scratchpad/cli_backup_batch53.py`), confirmed `diff -q` showed a
real change, ran the target test and confirmed it reddened, then `cp` the backup back over
`cli.py` and confirmed `diff -q` reported the files byte-identical again before moving to the next
mutation. All 8 mutations reddened their target test (one test — the `StateDbError` isinstance
check — was verified by the `UnblockLookupError` base-class mutation; see the per-item table). None
of the mutations are present in the committed tree — `git status`/`git diff` on
`src/fleet/cli.py` are clean, confirmed after the last restore.

## Per-item results

| Item | New test | Untested branch / gap found |
|---|---|---|
| `UnblockLookupError` | `test_refuse_unresolved_blockers_names_only_the_missing_subset_in_sorted_order` (isinstance assertion) | Its only behaviour beyond being a docstring is being a `StateDbError` subclass, so `_mapped_errors` funnels it to `ExitCode.UNEXPECTED_ERROR` instead of an uncaught traceback. No existing test asserts the class relationship directly (the e2e loud-failure test only checks the CLI's *observable* exit code/message, which a `CliRunner` can mask under some exception shapes). Mutated the base class to `RuntimeError`; the isinstance assertion reddened. |
| `_blocker_states` | `test_blocker_states_keeps_each_repos_statuses_separate_in_one_call` | Every existing exercise is indirect through the multi-repo `test_resume_unblocking.py` fixture, where the repos queried together always happen to differ in an outcome-relevant way already. Isolated the pure grouping: two repos, one landed one not, queried in a single call. Mutated `grouped.setdefault(repo_id, set())` to a single shared key (`"_all"`); reddened (`set(states) == {'_all'}`). |
| `_read_blocker_states` | `test_read_blocker_states_only_returns_the_given_names_names_arg` (+ a cheap empty-`names` smoke test) | The `repo_id IN (...)` filter by caller-supplied `names` has never been exercised against a database ALSO holding an unrelated repo outside the queried set (the e2e fixture always queries the union of every blocker referenced anywhere in the run). Mutated the SQL to `... AND (repo_id IN (...) OR 1=1)`; reddened (the unrelated repo leaked into the result). |
| `_resolvable_repo_ids` | `test_resolvable_repo_ids_only_returns_the_given_names_names_arg` | Same shape as above, mirrored for the `repos` table query. Mutated `WHERE repo_id IN ({marks})` to `... OR 1=1`; reddened. |
| `_refuse_unresolved_blockers` | `test_refuse_unresolved_blockers_names_only_the_missing_subset_in_sorted_order` (+ a silent-when-resolved smoke test) | The one existing e2e loud-failure case has exactly one missing name, so it cannot discriminate `known - set(states)` (shipped) from its mirror `set(states) - known`, nor prove `sorted(...)`. Built a case with 2 missing + 1 resolved name and asserted the exact sorted `missing` list and that the resolved name is absent. Mutated to the mirrored subtraction; reddened (`DID NOT RAISE` — with this fixture the mirrored expression is empty). |
| `_unblocking_entry` | `test_unblocking_entry_renders_floor_as_name_and_copies_both_tuples_to_lists` | No test calls this pure formatter directly; existing coverage reads only the `--json` dict it produces, in fixtures where `floor` is always `None` or where a stray `.value` vs `.name` mix-up would not be checked against the enum member's actual name string. Mutated `plan.floor.name` to `plan.floor.value`; reddened (`'floor': 3` vs `'BUILD'`). |
| `_blocker_resolver` | `test_blocker_resolver_resolve_returns_states_for_resolvable_names`, `test_blocker_resolver_resolve_raises_when_a_known_repo_has_no_phases_row` | The closure this function builds is used ONLY inside `clear_blocked_by`'s transaction (via `_apply_unblocking`, excluded here); the existing loud-lookup-miss e2e test (`test_a_blocker_the_repos_table_knows_but_the_status_lookup_misses_is_loud`) trips the OUTER `mode=ro` preview check in `_unblock_dependents` (before `plan_unblocking` even runs) and never reaches this closure at all. Called `resolve()` directly against a bare `aiosqlite` connection. Mutated the closure to drop its `_refuse_unresolved_blockers(...)` call; the raise-path test reddened (`DID NOT RAISE`). |
| `_unblock_dependents` | `test_unblock_dependents_skips_apply_unblocking_when_no_plan_has_a_removal` | Every existing real (non-dry-run) test frees at least one repo, so `if not dry_run and any(plan.removed for plan in plans):` is always taken on the true branch there. Built a fixture where the only blocker is still `PENDING` (nothing landed, nothing removed) and monkeypatched `cli._apply_unblocking` to raise if called. Mutated the guard to `if not dry_run:`; reddened (the monkeypatched raise fired — the write path was entered for a genuine no-op resume). |
| `_unblock_lines` | `test_unblock_lines_zero_candidates_short_circuits_before_the_wave_lines`, `test_unblock_lines_unblocked_entry_verb_and_floor_suffix`, `test_unblock_lines_retained_entry_parenthetical_only_when_removed_is_non_empty`, `test_unblock_lines_unresolved_entries_render_the_repo_and_reason`, `test_unblock_lines_wave_index_and_wave_error_are_independent_trailing_lines` | No test anywhere asserts this function's rendered text — every existing step-6 test reads the `--json` payload or raw `phases` rows, never `result.output` (confirmed by `grep` for "step 6: no repo carries", "still blocked by", "UNRESOLVED for", "appended wave" across `tests/test_cli.py` and `tests/test_resume_unblocking.py`: no hits outside the new file). Covered the `candidates==0` early return (and that it fires BEFORE the wave lines), the dry/real verb split with and without a floor, the retained-entry parenthetical's conditional suffix, the unresolved-entry line, and the two independent trailing wave lines. Mutated the `candidates==0` early `return` into an `out.append` (removing the short-circuit); the first test reddened (extra wave lines leaked through). |

## Reaching test file(s) run for verification, and why

Ran together as one covering set (`pytest tests/test_cli_unblock_lines_batch53.py
tests/test_resume_unblocking.py tests/test_reentry_unblocking.py tests/test_step6_wave_write.py
tests/test_cli.py -q`): **353 passed**, 0 failed, 0 skipped, 64.0s wall.

- `tests/test_cli_unblock_lines_batch53.py` — the new file; every direct/isolated test for all 9
  in-scope names.
- `tests/test_resume_unblocking.py` — §12 item 46(ii)'s acceptance test; drives all 9 names
  through a real `fleet resume` end to end (the primary reaching test this batch does not
  duplicate).
- `tests/test_reentry_unblocking.py` — the pure `plan_unblocking`/`still_blocking` predicate
  `_unblock_dependents` drives and `_unblocking_entry`/`_blocker_states` supply inputs to
  (`BlockerState`, `Unblocking`); confirms no regression on the collaborator types this batch's
  tests construct directly.
- `tests/test_step6_wave_write.py` — the SQL-surface whitelist over `_unblock_dependents`'s (and
  the excluded `_apply_unblocking`'s) writes; confirms the guard change explored during mutation
  testing left no residual difference in step 6's write shape.
- `tests/test_cli.py` — the general CLI suite, including the step-6 wave-membership DB assertions
  around `RUN_ID`/`base_args`/`fresh_db`/`seed_run` this batch's new file imports from; also the
  broadest net for any accidental cross-function regression in the same module.

Not run: `tests/test_pr_e2e.py`, `tests/test_build_e2e.py`, `tests/test_stub_resolution_task79.py`
— each mentions `_unblock_dependents` only in docstring/context prose around a much larger
multi-hour e2e scenario (real monorepo + fake bazel) whose primary subject is elsewhere (stub
resolution, PR sync); no production code changed in this batch, so these were judged out of the
covering set for cost reasons, matching batch 51's precedent of scoping to files with a direct
reaching relationship to the in-scope functions.

## Quality gates

- `ruff check tests/test_cli_unblock_lines_batch53.py src/fleet/cli.py`: clean.
- `ruff format --check tests/test_cli_unblock_lines_batch53.py`: clean (one auto-reformat applied
  before the mutation round; re-ran the file afterward — still 15/15 passed, then proceeded to
  mutation testing on the reformatted version).
- `python -m mypy` (no path arguments — the project's own `packages = ["fleet"]` scoping):
  `Success: no issues found in 132 source files`. No production code changed, so this is
  unaffected by this batch by construction; run anyway to confirm the worktree is clean.

## Disclosures

- No production code was touched; `src/fleet/cli.py` is byte-identical to `main` at `5de4485`
  after every mutation cycle restored it (verified `git status --short` is empty after the final
  restore, and `git diff` produces no output).
- All 9 in-scope names got at least one new mutation-proof test; none were judged "no branch to
  mutate" (contrast batch 51's `_validate_resume_flags`).
- The `_blocker_resolver` and `_unblock_dependents` tests call the target functions directly
  against a hand-built `aiosqlite`/`sqlite3` database rather than through a full CLI invocation,
  disclosed in each test's own docstring/comment block, matching this project's existing precedent
  (`tests/test_cli_resume_report_lines_batch51.py`'s `stub_reconcile`-subsystem tests).
