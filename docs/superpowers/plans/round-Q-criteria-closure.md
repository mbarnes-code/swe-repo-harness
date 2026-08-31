# Round Q — SPEC §12 criteria closure

**Spec authority:** `docs/SPEC.md` §12 is binding. `docs/CRITERIA_PLAN.md` is the closure
backlog. `CLAUDE.md` Rules 1-14 bind every task.

**Why these three, why now (2026-08-31):** re-verified against current `HEAD` (`0e66d56`, post
round P + lint fix, full suite confirmed green: 1981 passed) immediately before writing this
plan. Three TEST-ONLY items, zero file overlap with each other. §12.24's ledger-sum sub-clause
was scoped out of this round — no existing e2e fixture drives `attempts.cost_usd` and
`budget_ledger.spent_usd` together through a real dispatch path (confirmed by grep: zero hits
for `attempts.cost_usd` combined with an e2e/CLI driver); routed to a parallel research dispatch
this round instead of a worker, to unblock round R's task brief rather than let a worker guess at
unbuilt plumbing.

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

## Task 1: Extend the ecosystem/contract-kind confinement gate to `ContractKind` and `match`/`case` (§12.6)

**Criterion:** SPEC.md §12 item 6 (`docs/SPEC.md:7421`) — the confinement invariant. Full done
bar: `docs/CRITERIA_PLAN.md` §6 entry.

**Current state (verified 2026-08-31):** `tests/test_ecosystems.py` implements the criterion's
gate (a) as five line-scanning tests: `test_no_ecosystem_branch_exists_outside_the_adapter_packages`,
`test_no_ecosystem_member_other_than_the_unknown_sentinel_is_named_outside_the_packages`,
`test_the_exemption_list_is_exactly_the_two_adapter_packages`,
`test_no_language_directory_is_hardcoded_in_any_driver`, and
`test_no_adapter_package_exception_is_named_outside_the_adapter_packages`. SPEC.md's literal text
for gate (a) is: "an AST test that walks every module under `src/fleet/` except those two packages
and fails on any `Compare`, `match`, or subscript node whose operand is an `Ecosystem` or
`ContractKind` member." The existing five tests only scan for `Ecosystem` — never `ContractKind` —
and use line-oriented regex scanning, never an AST walk that would catch a `match`/`case`
statement. Both gaps are vacuous today (no code currently does either), but a regression of
either shape would pass every existing test.

**Task:** Read all five existing tests in `tests/test_ecosystems.py` in full — they are your
pattern precedent (the exemption-list constant, the module-walk helper, the assertion style).
Read `docs/SPEC.md:7421` for the exact clause text. Extend the pattern set so it also (1) fails on
a `ContractKind` member referenced via `Compare`, `match`, or subscript outside the two exempt
packages (`src/fleet/manifests/`, `src/fleet/ecosystems/`) and `src/fleet/models/enums.py`'s
existing exemption, and (2) fails on a `match`/`case` statement whose subject involves an
`Ecosystem` or `ContractKind` member outside those same packages. Decide whether to extend the
existing line-scanning tests with a `ContractKind` pattern (matching the current file's style) or
add new tests specifically for `match`/`case` (which line-scanning cannot reliably catch — an AST
walk is likely needed for that half; use Python's `ast` module, matching how other AST-based tests
in this codebase are structured, e.g. `tests/test_instruments_are_armed.py`). State which you
chose and why in your report.

**Mutation to prove discrimination:** temporarily add a `ContractKind` comparison (e.g.
`if x.kind == ContractKind.INTERFACE:`) and/or a `match ecosystem: case Ecosystem.PYTHON:`
statement to a driver module outside the exempt packages (e.g. a scratch copy, not committed),
confirm your new test(s) fail, then revert and confirm they pass again.

**Report:** DONE / DONE_WITH_CONCERNS / NEEDS_CONTEXT / BLOCKED, commits, one-line test summary,
concerns.

---

## Task 2: Test digest sensitivity to source mutation — the remaining §12.21 clause (§12.21 clause 3)

**Criterion:** SPEC.md §12 item 21 (determinism), third clause. Full done bar:
`docs/CRITERIA_PLAN.md` §12.21 entry. Closing this task closes the criterion in full — clauses 1
and 2 already landed (round O, `daf2a24`, and pre-existing coverage respectively).

**Current state (verified 2026-08-31):** `tests/test_cli.py` has
`test_status_digest_is_byte_identical_across_two_clean_db_runs_under_a_warm_llm_cache` (added
round O), proving the digest is *stable* when nothing changes between two clean runs. No test
proves the inverse: that the digest is actually *sensitive* to real content changes, not merely
stable by coincidence (e.g. if a digest section were accidentally computed from something that
never varies, this would go undetected).

**Task:** Read `test_status_digest_is_byte_identical_across_two_clean_db_runs_under_a_warm_llm_cache`
in full — it is your fixture/harness precedent (how it seeds a clean DB, warms the LLM cache, and
captures the `status` digest). Write a new test that: runs the harness once from a clean DB
exactly as that test does, captures the digest; then mutates a single real fixture source file
(any genuine content change — e.g. add a line to a fixture repo's source file that participates in
the scan/graph) between runs; re-runs identically (clean DB, same warm-cache approach); captures
the second digest; asserts the two digests **differ**. This is the direct inverse of the existing
test and must use the same rigor (a genuine content change, not a cosmetic no-op — confirm via
`git diff --numstat` inside the fixture that the mutation actually changed the file, per
`CLAUDE.md` Rule 12's zero-change gate discipline).

**Once this lands, update `docs/CRITERIA_PLAN.md`'s §12.21 entry to DONE** (all three clauses
closed) — this is part of the task, not a separate follow-up.

**Report:** DONE / DONE_WITH_CONCERNS / NEEDS_CONTEXT / BLOCKED, commits, one-line test summary,
concerns.

---

## Task 3: 12-node cycle fixture and a bounded no-hang test at the stated scale (§12.19)

**Criterion:** SPEC.md §12 item 19 (cycles broken at the stated scale). Full done bar:
`docs/CRITERIA_PLAN.md` §12.19 entry.

**Current state (verified 2026-08-31):** `tests/test_graph_cycles.py` (523 lines) has extensive
cycle-breaking coverage but all fixtures are 2-3 node cycles. SPEC.md's criterion names 3/12/41-node
cases specifically, plus a "no hang" property — zero timeout-bounded assertions exist anywhere in
the file (confirmed by grep: no `timeout` reference in the file).

**Task:** Read `test_scc_id_is_content_derived_over_the_member_set` and
`test_a_genuine_implementation_cycle_reaches_atomic_wave` in full as your fixture-construction
precedent (how this file builds a `PullRequestDraft`/edge set and drives `break_cycles` or the
SCC-detection entry point). Build a 12-node cycle fixture (a real SCC of 12 members, edges forming
a genuine cycle, not just a chain) and assert all 12 members share one `scc_id` per the criterion's
wording. Then add one timeout-bounded test proving the "no hang" property at the 41-node scale —
read the SPEC's exact wording for what "the stated scale" means for this clause before building
the fixture; if the algorithm's actual complexity class (check `graph/cycles.py`'s cycle-detection
implementation — is it polynomial or could a naive implementation be exponential in cycle size?)
makes a smaller adversarial case equally discriminating for the no-hang property, you may use that
instead of literally 41 nodes — state which you used and why in your report. Use a real wall-clock
timeout assertion (e.g. `pytest-timeout` if already a dependency, or a manual deadline check) —
confirm which mechanism this codebase already uses for timeout-bounded tests elsewhere before
introducing a new one.

**Mutation to prove discrimination:** temporarily revert the no-hang test's timeout to something
absurdly tight (e.g. 1ms) to confirm it can fail, then restore it — or, for the scc_id test,
temporarily break one member's edge so it's no longer part of the SCC, confirming the assertion
would catch a 12th member silently excluded.

**Report:** DONE / DONE_WITH_CONCERNS / NEEDS_CONTEXT / BLOCKED, commits, one-line test summary,
concerns.
