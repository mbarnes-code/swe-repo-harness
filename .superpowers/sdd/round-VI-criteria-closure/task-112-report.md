# Task 112 report — §12.39-B2: real two-round CLI fixture proving `BUDGET_EXHAUSTED`

Branch `agent/roundvi-task112`, off `main` @ `a01e004`. Worktree provisioned via
`tools/worktree/new-worktree.sh roundvi-task112 main` (confirmed `fleet.__file__` resolves inside
the worktree before any test ran).

## What was implemented

TEST-ONLY, per ADR-0136's two-leg split and this task's own scope boundary. `git diff --stat --
src/` is empty; the only change is one new test in `tests/test_stub_resolution_task79.py`
(`test_t3_budget_exhausted_is_reached_through_the_real_claiming_loop_not_a_direct_call`, +201
lines), added right after task 103's own case (i) `STUB_DIVERGED` test
(`test_t3_stub_diverged_is_reached_through_the_real_claiming_loop_not_a_direct_call`), which it
uses verbatim as its template for steps 1-4.

**No production code change was needed** — B1 (task 109) already built everything this proof
needs; confirmed by `git diff --stat -- src/` being empty and by mypy/ruff running clean against
`src/fleet/cli.py` unmodified.

### The new test, step by step

1. **Before `_reach_active_stub_state` runs** (i.e. before `fleet scan`/`transform` cuts the run
   whose `runs.config_digests` becomes this run's own §10 baseline, `cli.py:2338`), the test
   appends one section to `config/fleet.yaml`:
   ```yaml
   stubs:
     revalidation_max_cost_usd: 0.0135
   ```
   This is load-bearing timing, not cosmetic: `stubs` is one of the 15 §10 drift sections
   (`settings.py`'s `CONFIG_SECTIONS`), and `_resume_impl` (`cli.py:16948`) refuses a resume whose
   config drifted since the run's own baseline was recorded, unless `--accept-drift`/
   `--force-config-drift` is passed. Lowering the ceiling before the run starts makes it the run's
   own baseline from the start, so step 5's plain `fleet resume` never trips that refusal — an
   earlier draft of this test lowered the ceiling AFTER step 4 and would have needed
   `--accept-drift stubs` to reach the REVALIDATE round at all; this task avoided that mechanism
   entirely by reordering instead.
2. Steps 1-4 are byte-for-byte the same real stub-lifecycle steps as case (i)'s test: a real
   consumer `fleet verify` (`STUB_LIMITED`/`DEGRADED`), a real provider `retry`+`build`+`verify`
   to `SUCCEEDED`, a real provider `fleet pr`, and a real `forge.merge` + `fleet pr --sync` (D107's
   synchronous label rewrite, asserted via `_git_show` on the committed `migrate/<consumer>` tree
   exactly as case (i) does).
3. Step 5: a real `fleet resume` (no `--accept-drift`, no direct call, no hand-constructed
   `budget_breach=`) runs the REVALIDATE claiming loop under a plain `FakeBazel` with no `fail=`
   entries. `RevalidationBudgetExhausted` fires inside `CostLedger.reserve()`, **before**
   `VerifyPipelineWorker` (and hence any bazel build for this consumer's REVALIDATE round) ever
   runs — the round's own build outcome is irrelevant, matching task 109's own over-budget
   direct-dispatch test's finding.
4. Assertions, all read back from real DB state (never a hand-constructed call):
   - `resume_payload["revalidation_claims"]["outcomes"]` has exactly one entry, starting
     `"budget_exhausted: verdict=FAIL"`.
   - `stubs.state`/`abandon_reason` == `("ABANDONED", "BUDGET_EXHAUSTED")`.
   - The consumer's Phase 4 row is byte-identical before/after the round (`settle_revalidation`'s
     budget branch always sets `consumer_status=DEGRADED`, and `_run_one_revalidation_task`'s own
     `apply_stub_consumer_status` write is skipped for exactly that value — D108's "only
     T2/T3-STUB_DIVERGED write" rule — so nothing here is a promotion to `SUCCEEDED`).
   - A `RevalidationBudgetExhausted` finding is written for the consumer.

### Guardrail 6 — the $0.027/$ 0.0135 numbers, re-measured in this worktree's own interpreter

Did not trust ADR-0136/research-54/task 109's prior $0.027 figure. Re-derived it two ways in this
worktree:

1. **Arithmetic, from the config values actually in this tree**: `tests/test_cli.py`'s
   `MODELS_YAML` (imported transitively by the `fleet` fixture via `tests/test_transform_e2e.py`)
   prices WORKHORSE at `in_per_mtok=3.0`, `out_per_mtok=15.0`; `cli.py:14582`'s
   `TokenEstimator()` construction passes no floor override, so `DEFAULT_ROLE_FLOOR` (4_000 in +
   1_000 out tokens, `orchestrator/budgets.py:107`) applies uncontested for the first REVALIDATE
   dispatch of a fresh run (0 samples, below `min_samples`). `4_000 * 3.0 / 1e6 + 1_000 * 15.0 /
   1e6 == 0.027` exactly.
2. **Live, in the running interpreter**: a scratch pytest test (`tests/
   test_scratch_measure_cost_task112.py`, written, run once, and deleted before this commit —
   never landed) built the same `fleet` fixture, loaded settings via `cli._load_settings`, resolved
   `router.resolve("build_diagnosis").targets[0]`, and called `TokenEstimator().estimate(...)`
   directly. Printed output: `MEASURED_ESTIMATE_USD= 0.027`, `MEASURED_IN_TOKENS= 4000`,
   `MEASURED_OUT_TOKENS= 1000`.

Both derivations agree with each other and with the prior sessions' number, but neither was
inherited — both were performed fresh in this worktree's `.venv` against this tree's own
`config/models.yaml`/`orchestrator/budgets.py`.

**Threshold convention**: per the brief's own suggestion ("set the ceiling to half that value"),
`stubs.revalidation_max_cost_usd: 0.0135` is exactly half of the measured $0.027 — below the real
estimate, same convention this file's own sibling test
(`test_d135_a_revalidate_round_over_budget_raises_and_routes_through_settle_revalidation`, task
109) used with a different constant ($0.01).

## Tests

- `tests/test_stub_resolution_task79.py` — **20 passed** (19 pre-existing + 1 new), full file.
- `tests/test_stub_resolution_task79.py tests/test_stubs.py` together (the brief's own covering
  set) — **64 passed**, 170.12s, 0 skipped.
- `ruff check src/fleet/cli.py tests/test_stub_resolution_task79.py` — clean.
- `mypy` (whole-package scope, `cwd` at the worktree root) — `Success: no issues found in 131
  source files`.

## Rule 12 — mutation proof

Mutated `cli.py:14580`'s `scope = SpendScope(repo_id=repo_id, task_id=task_id, kind=SpendKind.
REVALIDATION)` → `kind=SpendKind.NORMAL` — the brief's own first example, and the same mutation
task 109 used for its own two direct-dispatch tests. Confirmed the mutation genuinely changed the
file: `git diff --numstat --no-index` against a pre-mutation backup copy (not `git diff` against
`HEAD`, per Guardrail 12's zero-change-gate discipline) reported `1 1` (one changed line).

The new test went RED under the mutation: `AssertionError: 'settled: verdict=PASS decisions=1'`
(no breach ever fired — `_check_subceilings`'s REVALIDATION-only branch is gated on `scope.kind is
SpendKind.REVALIDATION`, so under `NORMAL` the round ran for real against the lowered-ceiling-but-
now-irrelevant config and PASSed on its own merits, since the fixture's builds are all genuinely
green by this point in the lifecycle).

Reverted immediately after: `diff` against the pre-mutation backup copy of `cli.py` reports no
difference (byte-identical), confirmed live (`diff <backup> <worktree cli.py>` produced no output).
`git diff --stat -- src/` is empty. Re-ran the new test standalone — green again (10.17s) — and
re-ran `ruff`/`mypy` clean post-revert.

**Disclosed, not asserted, one thing this test does NOT check**: `fake_bazel.calls` is not
asserted empty after step 5, unlike task 109's own direct-dispatch sibling test. A real `fleet
resume` against this fixture's multi-repo config (the `.ts` repos alongside the `.py` stub pair)
advances those other repos' own ordinary Phase 3/4 work in the same call, so `fake_bazel` is
genuinely invoked for repos unrelated to this consumer's REVALIDATE round — an early draft of this
test asserted `fake_bazel.calls == []` and failed for exactly this reason (4 real docker-run
invocations for `acme-lib-ts`/`acme-app-ts`), which is disclosed in the test's own docstring rather
than worked around by narrowing the fixture. The DB-state assertions (no promotion, `ABANDONED`/
`BUDGET_EXHAUSTED`) are what actually prove the breach fired before this consumer's own worker
dispatch — the same property task 109's direct-dispatch test proves via the emptier
`fake_bazel.calls == []` assertion, which is only expressible there because that test drives
exactly one task with no sibling repos in scope.

## Scope discipline

- Did not touch §12.11/Leg C (task 111's own scope).
- Did not touch B1's own production wiring in `cli.py` — no production defect was found blocking
  this proof; `git diff --stat -- src/` is empty for the entire task.
- Did not re-derive or re-litigate case (i) — task 103's own test is untouched, still 1 of the 20
  tests in the file's passing count.
- Rule 3 (surgical changes): the new test's setup (steps 1-4) is a close copy of case (i)'s own
  test rather than a new abstraction, per Rule 2 (simplicity first) — a shared helper factoring
  steps 1-4 out of both tests was considered and rejected as premature abstraction for two
  call sites that already diverge meaningfully at step 5 (a divergence a shared helper would need
  a parameter to express, buying nothing over two straight-line tests reading closely together in
  the file, `test_t3_stub_diverged_...` immediately above `test_t3_budget_exhausted_...`).

## §12.39 status

Per `docs/CRITERIA_PLAN.md` §39's own text (verified directly, not assumed): "§12.39 as a whole
stays OUT of the `<n> of 48` count until B1 and B2 both land — case (i) and (iii) are closed, case
(ii) is not." B1 landed at round VI task 109. **This task (B2) is what makes case (ii) closed** —
the same real-CLI-loop proof case (i) already has, now extended to `BUDGET_EXHAUSTED`. Per that
same entry's own logic, **landing this task closes §12.39 as a whole** (all three cases (i)/(ii)/
(iii) covered) — but this report does not itself edit `docs/CRITERIA_PLAN.md`/`docs/SPEC.md`'s
`<n> of 48` tally or file an ADR recording the closure; that is left to the controller per this
project's central-allocation convention (CLAUDE.md §3), since this task's own brief did not ask
for those doc edits and Rule 14 requires any §12 criterion-status change to be a disclosed,
attributed decision at dispatch/integration time, not something a single TEST-ONLY task silently
asserts into the tracked docs on its own branch.
