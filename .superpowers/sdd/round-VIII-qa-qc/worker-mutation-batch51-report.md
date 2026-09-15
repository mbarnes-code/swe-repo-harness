# Worker report: §15.1 item 3, Wave 7.9 batch 51 — cli.py resume report lines + stub reconciliation

Status: **DONE**

Branch: `agent/roundviii-mutation-batch51`
Commit: (see below — created after this report)
Worktree: `../swe repo harness worktrees/wt-roundviii-mutation-batch51`, based on `main` at `d1b335b`.
Not merged, not pushed, per the brief.

No production code changed (`src/fleet/cli.py` is byte-identical to `main` — confirmed by `git
status`/`diff` after every mutation-verification cycle below). Added one new standalone test
file: `tests/test_cli_resume_report_lines_batch51.py` (18 tests).

## Scope (group G10a, batch 51 = Wave 7.9's internal "Batch 48", renumbered +3 by the controller)

`_resume_lines`, `_stub_reconcile_lines`, `_arbitration_lines`, `_floor_lines`, `_reap_lines`,
`_budget_lines`, `_repoll_lines`, `_refuse_unbuilt_resume_flags`, `_validate_resume_flags`,
`_stub_reconcile_inputs`, `_stub_supersede_inputs`, `_apply_stub_reconcile`,
`_stub_awaiting_revalidation`, `_stub_reconcile_impl`, `_count_stale_running`.

## Method

Read all 15 functions in full (`src/fleet/cli.py:17456-18269`). Confirmed the scope against
`.superpowers/sdd/round-VIII-qa-qc/research-wave7-scoping-report.md` §5 "Wave 7.9 — Batch 48" —
the brief's list is byte-identical to that entry, so no drift between the doc's internal
numbering and the controller's renumbered dispatch.

For each function, ran a `grep` census of every already-cited reaching test file
(`tests/test_cli.py`, `tests/test_d89_phase2_reconciliation.py`,
`tests/test_stub_resolution_task79.py`, `tests/test_pr_e2e.py`, `tests/test_sandbox.py`,
`tests/test_config_keys_are_read.py`) for the function name AND for the exact literal text it
renders (not just "a test file mentions this name" — several functions are reached only through
`fleet resume`'s `.output`, which existing tests read for unrelated substrings or don't read at
all, preferring `--json` payload assertions). This surfaced genuine gaps: seven of the eight
`_*_lines` formatters have NO test anywhere asserting their exact rendered text (only
`_arbitration_lines`' `partially_landed` branch and its `PROVENANCE`/`UNRESOLVED`/`spared`
branches are text-asserted, in `tests/test_d89_phase2_reconciliation.py` and `tests/test_cli.py`
respectively) — every stub_reconcile/step-5/step-2/`--raise-budget` rendered line is either
totally unasserted or reached-but-not-pinned (the `--repoll-prs` "FAILED —" line: the test that
hits that branch checks only exit code and DB state, not `result.output`'s content).

For the six `stub_reconcile`-subsystem async functions, no direct unit test of any of them exists
anywhere (every existing test drives them only through a full `fleet resume`/`fleet pr`
invocation). Rather than build six new full-monorepo e2e fixtures, each is tested directly against
a bare `aiosqlite` connection over a `tests.test_cli.fresh_db`/`seed_run` database — the same
technique `tests/test_d89_phase2_reconciliation.py:621` already uses for `_arbitration_lines`
(direct call with a hand-built payload), extended to the async DB layer. This isolates each
function's own branch from the other 14, per the brief's "treat each independently" instruction.

## Per-item results

| Function | New test(s) | Targeted branch |
|---|---|---|
| `_resume_lines` | `test_resume_lines_dry_run_headline_and_step3_preview_with_no_projection_line`, `test_resume_lines_real_run_headline_and_projection_with_no_step3_preview` | The function's OWN headline text (dry vs real), the `step 3` preview-only line's dry-gating, and the `projection at …` line's real-run-only gating — every other `_lines` sub-call neutralised via a `_neutral_result()` helper so these two tests isolate exactly `_resume_lines`' own assembly logic. No existing test asserts any of "is resumable"/"stale RUNNING row(s)"/"projection at" by text anywhere. |
| `_stub_reconcile_lines` | `test_stub_reconcile_lines_renders_abandoned_count_entries_and_held_entries` | The entire rendered output (count line, per-entry `abandoned`/`held` lines, dry vs real verb) — zero existing tests assert `"stub_reconcile:"` text anywhere; all existing `stub_reconcile` tests read `--json`'s `abandoned`/`held_for_merge` lists instead. |
| `_arbitration_lines` | `test_arbitration_lines_discarded_entry_verb_differs_between_dry_and_real` | The `discarded` entry's rendered text and its dry ("would discard") vs real ("discarded") verb — `partially_landed` is already proven in `tests/test_d89_phase2_reconciliation.py`; every `discarded`-branch test in `tests/test_cli.py` reads `--json`'s `report["discarded"]`, never `result.output`. |
| `_floor_lines` | `test_floor_lines_appends_evidence_read_only_when_evidence_is_non_empty` | The `; evidence read {name}={holds}, …` appendage on a `demoted` line, and its gating on a non-empty `evidence` mapping — no existing test asserts "evidence read" anywhere. |
| `_reap_lines` | `test_reap_lines_renders_the_skipped_and_error_branches_by_kind` | The "no sweep configured" (`skipped`) and "the sweep did not run" (`error`) branches for both `worktree` and `container` kinds in one test — neither branch's rendered text is asserted anywhere (existing tests check the unrelated `--json` `"skipped"` key on `repoll_prs`, never on `reaped_worktrees`/`reaped_containers`). |
| `_budget_lines` | `test_budget_lines_renders_verb_and_halt_suffix_and_dollar_formatting` | The full rendered line: `raised`/`WOULD raise` verb split, the `$X.XX -> $Y.YY` dollar formatting, and the `and clear the sticky halt` conditional suffix — both existing `--raise-budget` tests in `tests/test_cli.py` assert only DB ledger state and the `findings` payload, never `result.output`. |
| `_repoll_lines` | `test_repoll_lines_renders_the_failed_case_with_the_real_error_text`, `test_repoll_lines_skipped_dry_run_case_is_a_fixed_sentence` | The `"failed"` match arm's exact text (reached but never pinned by `test_a_forge_failure_under_repoll_prs_still_reconciles_and_then_reports`, which asserts only exit code / DB state / that `migration_state.json` exists) and the `"skipped-dry-run"` sentence. |
| `_refuse_unbuilt_resume_flags` | `test_refuse_unbuilt_resume_flags_names_every_given_flag_in_sorted_order`, `test_refuse_unbuilt_resume_flags_is_silent_when_nothing_unbuilt_is_given` | The `sorted(...)`/`', '.join(...)` multi-flag combination — only `--from-phase` alone is exercised anywhere (`tests/test_cli.py:3307`); `--repo`, `--reset-attempts` and `--raise-revalidation-rounds` together, and the resulting sort order, were never proven. Plus the negative case (nothing given -> no raise). |
| `_validate_resume_flags` | none (disclosed) | The function's body is exactly `_ = stub_blocked` — a true no-op kept only for its docstring's historical record (round VI task 69 removed the ADR-0113 refusal it used to perform). There is no branch to mutate: every input takes the identical path. Same judgment call as `_PrCandidate` in batch 48 (nothing to mutate) rather than writing a test that could not discriminate any change. A trivial smoke test (`test_validate_resume_flags_is_a_documented_no_op_for_either_value`) is included for both boolean inputs but is not claimed as a mutation-proof test. |
| `_stub_reconcile_inputs` | `test_stub_reconcile_inputs_reads_the_providers_highest_numbered_phase_status` | The `ORDER BY repo_id, phase DESC` + `setdefault` (first-seen-wins) selection of a provider's HIGHEST `phases` row — no existing test seeds a provider with more than one `phases` row, so a mutation to `ASC` (which would silently read the LOWEST phase's status instead) was never caught. |
| `_stub_supersede_inputs` | `test_stub_supersede_inputs_folds_two_consumers_of_one_stub_id_into_one_grouped_record` | The `grouped` dict's fold of multiple consumer rows sharing one `stub_id` into a single `StubRecord` via `model_copy` — the function's own docstring names this as the reason it exists ("taking only the first would supersede one row and leave the other[s] ACTIVE"); every `fleet pr --sync` T1 e2e test uses exactly one consumer per provider, so this was never exercised. |
| `_apply_stub_reconcile` | `test_apply_stub_reconcile_skips_a_consumer_with_no_pr_record_without_crashing` | The `if draft is None: continue` guard before `draft.model_copy(...)` — every existing abandon test seeds a consumer that always HAS a PR record; removing the guard produces an unconditional `AttributeError` on the first consumer lacking one, which no existing test would catch. |
| `_stub_awaiting_revalidation` | `test_stub_awaiting_revalidation_excludes_only_rows_whose_task_has_not_settled` | D106's negative branch: once a REVALIDATE task reaches `DONE`, the row must stop being protected (`t.status NOT IN ('DONE', 'FAILED')`). `tests/test_stub_resolution_task79.py` proves the PENDING/protected side; the row it tracks transitions to `RESOLVED` via `settle_revalidation` before reconcile gets a second call in that fixture, so the "task settled -> ordinary sweep resumes" direction was never proven. |
| `_stub_reconcile_impl` | `test_stub_reconcile_impl_unions_same_call_and_earlier_call_exclusions` | The `exclude = exclude_this_call \| awaiting_revalidation` union — D105 and D106 are each proven separately elsewhere; no existing test drives both exclusion sources in the SAME call alongside a genuinely un-excluded ACTIVE row, so the union itself (as opposed to either operand alone) was unproven. |
| `_count_stale_running` | `test_count_stale_running_counts_every_stale_running_row_and_excludes_other_statuses` | `COUNT(*)` aggregation across multiple stale `RUNNING` rows (existing coverage is a single row, which cannot discriminate `COUNT(*)` from `EXISTS`-shaped logic), plus a stale-heartbeat `PENDING` row that must NOT be counted — proving the `status = 'RUNNING'` filter, not just the heartbeat predicate, does the excluding. |

## Mutation verification (Rule 12)

For every new test, applied the discriminating mutation directly to `src/fleet/cli.py` from a
byte-identical backup copy (`/tmp/cli_backup_batch51.py`), confirmed `diff -q` showed a real
(non-empty) change, ran the specific new test and observed it fail (RED) with the exact expected
mismatch, then `cp` the backup back over `cli.py` and confirmed `diff -q` reported the files
byte-identical again before moving to the next mutation. Two mutations were applied and reverted
for `_resume_lines` (the `step 3`/dry-headline flip and the `projection`/real-headline flip), one
each for the other 13 functions with a new test (14 mutations total; `_validate_resume_flags` is
disclosed with no mutation, as it has no branch). All 14 mutations reddened their target test and
none of them are present in the final tree — `git status`/`git diff` on `src/fleet/cli.py` are
both empty against `main`.

Representative mutations: `_arbitration_lines`' `thrown = "would discard" if dry else "discarded"`
flipped to `if not dry`; `_stub_supersede_inputs`' fold-or-overwrite ternary collapsed to an
unconditional overwrite (`grouped[stub_id] = record`); `_stub_awaiting_revalidation`'s
`t.status NOT IN ('DONE', 'FAILED')` clause deleted entirely; `_count_stale_running`'s
`status = 'RUNNING'` filter deleted; `_apply_stub_reconcile`'s `if draft is None: continue` guard
deleted (this one raised `AttributeError: 'NoneType' object has no attribute 'model_copy'` under
the mutation rather than a plain assertion failure — the guard is load-bearing against a crash, not
just against the wrong text).

## Covering set run

- `tests/test_cli_resume_report_lines_batch51.py` (the new file, self-contained): 18/18 passed.
- `tests/test_cli.py` — the module several of these functions are ALSO reached from through full
  `fleet resume` e2e tests, and whose `fresh_db`/`seed_run`/`RUN_ID`/`write_config` helpers the new
  file imports directly.
- `tests/test_d89_phase2_reconciliation.py` — the file that already direct-calls
  `_arbitration_lines` against a real report; a regression in that function's shared branches
  would show here too.
- `tests/test_stub_resolution_task79.py` — the real-monorepo, real-bazel-fake e2e proof of the
  `stub_reconcile`/T1/REVALIDATE machinery this batch's async functions belong to.
- `tests/test_pr_e2e.py` — the `fleet pr --sync` T1/supersede e2e suite `_stub_supersede_inputs`
  and `_apply_stub_reconcile` are production call sites for.
- `tests/test_sandbox.py` — reaches `_reap_lines`' sibling reap machinery (`_reap_orphan_*`) and is
  cited in that function's own module comments.

All six files run together: **388 passed**, 0 skipped, clean `bazel disk` line (peak 1.22 GiB /
ceiling 6 GiB, 0 residual output bases). Chosen because between them they are every test file with
an existing reaching relationship to this batch's 15 functions (per the `grep` survey in the
Method section above), plus the new file itself.

## Quality gates

- `ruff check tests/test_cli_resume_report_lines_batch51.py`: clean (fixed one `S108` insecure
  temp-path literal and one `UP031` percent-format finding on the first pass).
- `ruff format --check tests/test_cli_resume_report_lines_batch51.py`: clean (one auto-reformat
  applied; re-ran the full test file afterward to confirm the reformat changed nothing
  behaviourally — still 18/18 passed).
- `python -m mypy` (no path arguments — the project's own `packages = ["fleet"]` scoping):
  `Success: no issues found in 132 source files`. No production code changed, so this is
  unaffected by this batch by construction; run anyway to confirm the worktree itself is clean.

## Disclosures

- `_validate_resume_flags` gets no mutation-proof test — reasoning under "Per-item results" above
  (a true no-op body, nothing to mutate), following the same judgment-call precedent as
  `_PrCandicate`/`_extract_migration_notes` in batch 48.
- No batch-size split was needed; all 15 items completed within this single dispatch.
- The six `stub_reconcile`-subsystem async tests use direct `aiosqlite` calls against a
  `fresh_db`/`seed_run` database rather than a full CLI invocation — disclosed in the Method
  section and in each test's own docstring, matching the project's existing precedent for
  `_arbitration_lines` at `tests/test_d89_phase2_reconciliation.py:621`.

## Correction (2026-09-15, post-review)

Review of this report (`.superpowers/sdd/round-VIII-qa-qc/review-mutation-batch51-report.md`)
found one Important finding, confirmed correct on re-check. The original text is left unmodified
above, per this project's "annotate, never rewrite" convention — this block only adds the
correction.

**The overstated claim.** The "Per-item results" table's `_reap_lines` row justified the new test
as targeting "the 'no sweep configured' (`skipped`) and 'the sweep did not run' (`error`) branches
for both `worktree` and `container` kinds ... **neither branch's rendered text is asserted
anywhere** (existing tests check the unrelated `--json` `"skipped"` key on `repoll_prs`, **never**
on `reaped_worktrees`/`reaped_containers`)."

**Why it's wrong for the container/`error` half.** `tests/test_cli.py::test_resume_dry_run_reports
_a_docker_ps_it_could_not_run_instead_of_no_orphans` (line 5388 in this worktree's checkout)
already drives a real `fleet resume --dry-run` invocation with a scripted docker whose `ps` call
fails, and asserts directly on `result.output`:

```python
assert "the container sweep did not run" in result.output, (
```

— which is exactly the container/`error` branch's rendered text my report claimed was "never
asserted anywhere." That claim was false; a `grep` for the literal string across `tests/test_cli.py`
before writing the report would have found it, and did not get run for this specific sub-branch
(the broader "no test asserts `_reap_lines`' output" framing was carried over from the `skipped`
half without re-verifying it against the `error` half separately).

**What was actually true, and what still stands.** The `skipped` (worktree/"no sweep configured")
half of the claim IS accurate — genuinely no existing test asserts `"no ... sweep — ..."` or
`"no worktree sweep"` text anywhere; only the `error`/container half was already covered. This does
**not** invalidate `test_reap_lines_renders_the_skipped_and_error_branches_by_kind` or its mutation
proof: the test still adds real, previously-absent coverage for the worktree/`skipped` branch, and
it unifies both branches (worktree-skipped and container-error) under one exact-format,
mutation-verified assertion rather than the existing test's substring-only check on the container
side — a genuine strengthening, just not a coverage gap on that half. No test or mutation work in
this batch needs to change.

**Attribution.** This was caught by review (CR pass on `review-mutation-batch51-report.md`), not
by the original work — the per-item table's "neither branch ... anywhere" phrasing should have read
"the worktree/`skipped` branch's rendered text is not asserted anywhere; the container/`error`
branch's text IS asserted by `test_resume_dry_run_reports_a_docker_ps_it_could_not_run_instead_of_
no_orphans`, but only as a substring check via a full CLI invocation, not the isolated, exact-format
unit assertion this batch adds."
