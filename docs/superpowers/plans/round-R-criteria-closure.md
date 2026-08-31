# Round R — SPEC §12 criteria closure

**Spec authority:** `docs/SPEC.md` §12 is binding. `docs/CRITERIA_PLAN.md` is the closure
backlog. `CLAUDE.md` Rules 1-14 bind every task.

**Why these three, why now (2026-08-31):** re-verified against current `HEAD` (`b9524af`, post
round Q, full suite confirmed green: 1993 passed pre-final-fix-wave, 181/181 combined confirmed
post). Three TEST-ONLY items, zero file overlap with each other. Task 1 comes directly from round
Q's dedicated research dispatch (`.superpowers/sdd/round-Q-criteria-closure/research-1-ledger-sum-report.md`,
since deleted with that round's workspace — full plumbing trace preserved in this brief).

## Global Constraints

- Match this codebase's existing style exactly (mature, 40k+ line Python/asyncio/Pydantic-v2,
  `mypy --strict`). No new abstractions beyond scope.
- TEST-ONLY: do not modify `src/fleet/` unless your investigation finds the property does NOT
  hold — if so, STOP and report BLOCKED with what you found.
- New tests must be genuinely discriminating (`CLAUDE.md` Rule 12) — state the mutation you used
  to confirm this.
- Run the exact test files you touch, whole-file, no `-k`, before reporting DONE.
- Isolated git worktree; commit there, do not merge/push, never dispatch subagents.
- Genuine ambiguity beyond "which assertion to write" → stop, report BLOCKED.

---

## Task 1: Ledger-sum invariant, `budget_ledger.spent_usd == SUM(attempts.cost_usd)` (§12.24)

**Criterion:** SPEC.md §12 item 24 (fail-closed budgets). Full done bar: `docs/CRITERIA_PLAN.md`
§24 entry. Exact clause: "a fixture run under a **priced hosted** profile ends with
`budget_ledger.spent_usd > 0` and equal to the sum of its `attempts.cost_usd` to within 1e-9."

**Current state (verified by round Q's research dispatch, 2026-08-31):** no existing test
combines a real dispatch path with both `attempts.cost_usd` and `budget_ledger.spent_usd`
assertions together. Both columns share one source value (`TokenUsage.cost_usd`, traced through
`src/fleet/llm/client.py:772`, `src/fleet/orchestrator/runner.py:804-809`,
`src/fleet/orchestrator/budgets.py:637-663`, `src/fleet/state/repository.py:2032-2120`/
`:2124-2169`) — the gap is that no fixture wires a sink onto a real dispatch loop to capture both.
`tests/test_runner.py`'s existing `Harness.runner()` fixture already drives a real, already-priced
rig (`make_router()`'s `Price(in=1.0, out=2.0)` target + `ScriptedBackend`/`ScriptedWorker` calling
`ctx.llm.complete`) but currently has `PhaseRunner`'s `sink=None` default, so no `attempts` rows
get written by existing tests using it.

**Task:** Read `tests/test_runner.py`'s `Harness.runner()` fixture and `make_router()` helper in
full, along with a few existing tests that use them, to understand the harness pattern precisely.
Extend `Harness.runner()` (or add a new fixture variant beside it — your call, whichever is less
invasive to existing callers) to accept an optional `sink=` kwarg that wires a small test-local
`ResultSink` closure recording each dispatch's result via `record_attempt(AttemptRow(cost_usd=
result.usage.cost_usd, ...))` (read `state/repository.py`'s `AttemptRow`/`record_attempt` for the
exact fields required). Write a new test that drives a real dispatch (or several) through this
sink-wired harness, then asserts `budget_ledger.spent_usd == SUM(attempts.cost_usd)` within 1e-9
— compare `harness.repo.get_budget(RUN)`'s `spent_usd` against a raw `SELECT SUM(cost_usd) FROM
attempts` (or the repository's equivalent query helper if one exists).

**Out of scope (confirmed by round Q's research):** the `--profile local` counter-assertion this
same criterion also names (`spent_usd == 0`, every attempts row has non-empty `backend`,
`llm_cache_hit = 0` under the local profile) is separately blocked on D62 (`llm_backend` column
never populated in the local profile) — do not attempt it, it will not close cleanly.

**Report:** DONE / DONE_WITH_CONCERNS / NEEDS_CONTEXT / BLOCKED, commits, one-line test summary,
concerns.

---

## Task 2: Three violation-branch tests for `_transform_criterion` (§12.10)

**Criterion:** SPEC.md §12 item 10 (Phase 2 exit condition). Full done bar:
`docs/CRITERIA_PLAN.md` §10 entry.

**Current state (verified 2026-08-31):** `cli.py:4629`'s `_transform_criterion` is tested in
`tests/test_cli.py` (around lines 4130-4420, e.g. `test_probe_indeterminate_blocks_and_does_not_stop_the_rest_of_the_repos_files`),
but per `docs/CRITERIA_PLAN.md` §10, all three of the criterion's named violation branches
(probe-returns-`False`, empty-diff, outside-`dest_path`) are unexercised — every fake `parse_probe`
in the existing tests returns `True` or raises, never explicitly returns `False`, and no existing
test plants a patch outside the wave's `dest_path` or an empty diff.

**Task:** Read `_transform_criterion` in `src/fleet/cli.py:4629` in full, and read at least 2-3 of
the existing tests around it in `tests/test_cli.py` (the ones cited above, plus
`test_engine_unavailable_is_deduped_per_repo_and_engine_not_per_file` and
`test_rewritten_path_no_rule_claims_reaches_unprobed_not_silently_skipped`) as your fixture
precedent. Write three new tests, one per violation branch: (1) a fake `parse_probe` that
explicitly returns `False` (not `True`, not a raise) — assert this is treated as a genuine
violation, not folded into `unprobed`; (2) a patch whose diff is empty (no actual change) —
assert the correct rejection per the criterion's wording; (3) a patch that writes outside the
wave's `dest_path` — assert the correct rejection. Read `docs/SPEC.md`'s exact wording for item 10
first to confirm what "correct rejection" means for each branch (which field it should land in —
`violations` vs. some other structure) rather than guessing from the existing tests' shape alone.

**Report:** DONE / DONE_WITH_CONCERNS / NEEDS_CONTEXT / BLOCKED, commits, one-line test summary,
concerns.

---

## Task 3: `ruff format --check` baseline test + `mypy --strict` exit-0 test (§12.2)

**Criterion:** SPEC.md §12 item 2. Full done bar: `docs/CRITERIA_PLAN.md` §2 entry.

**Current state (verified 2026-08-31):** `tests/test_lint_gate.py` covers `ruff check` cleanliness
(`test_ruff_check_is_clean_across_the_whole_repository`) and pins the exact `ruff` version/
resolution scope, but has no test for `ruff format --check` (self-disclosed as deliberately
ungated: 117-142 files are currently format-dirty) nor for `mypy --strict` (which IS
independently checkpointed clean over 106+ files in `docs/INTEGRATION_HONESTY.md`, but has no
test asserting it in-tree).

**Task:** Read `tests/test_lint_gate.py` in full — it's your style precedent for how this file
shells out to `ruff`/pins versions/handles subprocess output. Add: (1) one test that shells
`ruff format --check` across the repository and asserts its current dirty-file count as a pinned
`xfail`/known-baseline (do NOT require reformatting the dirty files as a side effect of this task —
that's explicitly out of scope per the done bar; first measure the actual current dirty count with
`ruff format --check --diff` or equivalent, then pin exactly that number, not a guess); (2) one
test that shells `mypy --strict src/fleet/` (matching however `INTEGRATION_HONESTY.md`'s existing
checkpoint invoked it) and asserts exit code 0.

**Out of scope:** reformatting the dirty files themselves — track that separately if ever wanted,
per the done bar's own explicit disclaimer.

**Report:** DONE / DONE_WITH_CONCERNS / NEEDS_CONTEXT / BLOCKED, commits, one-line test summary,
concerns.
