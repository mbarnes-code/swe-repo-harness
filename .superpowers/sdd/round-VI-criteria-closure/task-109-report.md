# Task 109 report — §12.39-B1: cost instrumentation on the REVALIDATE path

Branch `agent/roundvi-task109`, off `main` @ `4fb783e27395f48b8fd0d18d13e01f79b57b6d1c`. Worktree
provisioned via `tools/worktree/provision-existing.sh` (confirmed `fleet.__file__` resolves inside
the worktree before any test ran).

## What was implemented

1. **`_run_one_revalidation_task` (`src/fleet/cli.py`)** now dispatches the REVALIDATE round
   through a real `CostLedger`, at a model-talking rung BY CONSTRUCTION (ADR-0136's R3 ruling):
   - `attempt = 2` (not the previous hardcoded `1`); `context_policy_for_attempt(2)` /
     `tier_for_attempt(2)` resolve to `EVIDENCE_ONLY` / `TransformTier.LLM_REPAIR` off the
     module's `DEFAULT_LADDER` — the first rung `BuildverifyWorker._diagnose` (`ctx.context_policy
     is None` gate) can ever fire on.
   - `scope = SpendScope(repo_id=repo_id, task_id=task_id, kind=SpendKind.REVALIDATION)` — the
     first production constructor of this kind anywhere in `src/` (previously 5 consumers, 0
     constructors, per research-54 §0).
   - `estimate = TokenEstimator().estimate(target, role="build_diagnosis", tier=WORKHORSE)`,
     `target` resolved from `run_ctx.llm.resolve("build_diagnosis").targets[0]` — verified in the
     running interpreter (see below) to resolve to $0.027 under the shipped default profile,
     exactly research-54 §4.3's measurement.
   - The round now runs inside `run_ctx.ledger.dispatch(estimate, scope=scope, deadline=deadline)`
     — `WorkerContext.llm`/`.router` are `run_ctx.model_client`/`run_ctx.llm` (a real
     `LadderModelClient`/`CachingModelClient`, previously `cast(Any, None)`), `.budget` comes from
     the ledger's own `call_budget()` (previously a zeroed `CallBudget`).
   - `reservation.record(CostEstimate(...result.usage...))` on success — the real settle-side
     number `TokenEstimator`/`CostLedger` machinery has always supported but nothing fed it for
     this path.
   - `RevalidationBudgetExhausted` (raised by `reserve()` **before** the worker runs, per
     `orchestrator/runner.py::_dispatch`'s own documented property) is caught and routed to
     `settle_revalidation(..., budget_breach=breach)` — the kwarg D116/research-52 found was
     **always `None`** in production; this is the first caller ever to pass it. A `_fail_report()`
     helper (factored out of the pre-existing "pipeline produced no report" branch) stands in for
     the missing `VerificationReport` on a breach, matching research-54 §5.3(d)'s own note.
   - Avoided an `assert` (ruff `S101`, and this file has no other bare `assert`) by threading
     `report: VerificationReport | None` through the try/except instead of a post-hoc
     `result is not None` assertion, with a fail-loud `RuntimeError` guard for the (unreachable)
     case neither branch set it (Rule 11).

2. **`_run_revalidation_claims_impl`** now builds the `CostLedger` + `RunContext` (with a real
   `Limits`/cpu-pool, mirroring `_emit_prs`'s own construction shape at `cli.py:15257` verbatim —
   `Ceilings.from_settings`, `llm_router(settings)`, `HARNESS_VERSION`) **once per call**, passed
   into every `_run_one_revalidation_task` invocation in the loop — never per task, never a second
   ledger per process. Unlike `_emit_prs` (which constructs a ledger but never dispatches through
   it — research-54 §9 finding 4), this call site actually dispatches through it.

3. **`VerifyPipelineWorker.run`'s success-path `usage=` omission (`cli.py:8457-8462` pre-change,
   D135/ADR-0136's required same-commit companion fix)** is fixed: the success return now carries
   `usage=accumulate(buildverify_usage, result.usage)`, accumulating BUILDVERIFY_STEP's own usage
   (previously dropped even when non-zero) with RDEPVERIFY_STEP's. **Measured, not assumed inert**:
   `BuildverifyWorker._diagnose` only ever fires on a failing unit (`buildverify.py:1035-1037`),
   and a failing unit always routes through `VerifyPipelineWorker._handoff` (which already carried
   `usage=step.usage`), never through the success-path constructor this task fixes — so today,
   with the code as shipped, this specific omission is **inert** (as research-54 §9 finding 1 and
   D135's own text say), and neither of this task's two new tests exercises it (both need a
   FAILING build to reach `_diagnose` at all, which routes through the already-correct `_handoff`
   path, not the success path). The fix is made anyway because ADR-0136 requires it in this same
   commit regardless of current reachability, and it closes the gap the moment anything ever
   drops usage into the success path in the future (D135 stays open for the broader, fleet-wide
   `TokenEstimator`-zero-constructors gap this does not touch).

## Guardrail 6 — verified in the running interpreter, not inherited from research-54

Re-ran the same probe research-54 used (worktree `.venv`, `fleet.__file__` confirmed inside this
worktree before the check): `TokenEstimator().estimate(WORKHORSE_target, role="build_diagnosis",
tier=WORKHORSE)` → `CostEstimate(in_tokens=4000, out_tokens=1000, usd=0.027)` against
`revalidation_max_cost_usd` default `2.0`. Same result research-54 measured at `main`'s prior
commit; unaffected by the intervening `_run_one_revalidation_task` rename (task 103) since none of
that touched `config/models.yaml` or `settings.py`.

## Tests

Two new tests in `tests/test_stub_resolution_task79.py`, both calling
`cli._run_one_revalidation_task` **directly** (via a new `_direct_revalidation_dispatch` test
helper) rather than through `_run_revalidation_claims_impl`, so a `FakeBackend` (`ModelBackend`
test double already used by `tests/test_llm_client.py`) can be injected via `RunContext.backends`
— this is what lets the round genuinely spend a real, priced dollar amount (or genuinely breach a
sub-ceiling) with no live `ANTHROPIC_API_KEY` and no network call. Per the brief's own permission,
this is more direct than the full-CLI fixtures already in this file (which prove the round
MECHANICS — settle/D108/STUB_DIVERGED — under the real, inert, no-API-key `anthropic` backend and
are unaffected by this change, confirmed below).

- `test_d135_a_revalidate_round_under_budget_records_a_real_nonzero_cost`: a genuinely failing
  consumer build (`FakeBazel(fail={...}: 1)`) reaches `_diagnose`, the `FakeBackend` answers with
  a scripted `LlmBuildDiagnosis` at exactly the `TokenEstimator` floor (4000 in / 1000 out tokens),
  and `repo_ledger.revalidation_usd` grows by exactly `$0.027` (`pytest.approx`), `0.0 <
  revalidation_usd <= 2.0`, and `spent_usd >= revalidation_usd` (same dollars, two ceilings, per
  research-54 §3). Asserts the fake backend was actually called once (proves the rung is
  model-talking, not silent).
- `test_d135_a_revalidate_round_over_budget_raises_and_routes_through_settle_revalidation`: ceiling
  overridden to `$0.01` (below the $0.027 estimate) via a `Ceilings` override in the test helper
  (never lowers the *durable* row — `open_repo_ledger`'s own "raise, never lower" contract — so the
  override works through the in-process `_check_subceilings` pre-check instead, which is what
  actually raises `RevalidationBudgetExhausted` before `reserve_repo_budget`'s durable CAS is even
  attempted). Asserts: outcome starts `"budget_exhausted: verdict=FAIL"`; `FakeBazel` and the
  `FakeBackend` were **never called** (the breach fires before the worker runs, matching
  `runner.py::_dispatch`'s own documented "nothing was dispatched and nothing was spent"); the stub
  row reaches `("ABANDONED", "BUDGET_EXHAUSTED")`; no promotion to `SUCCEEDED`; a
  `RevalidationBudgetExhausted` (`StubFinding.REVALIDATION_BUDGET_EXHAUSTED` — the string value is
  the class name `"RevalidationBudgetExhausted"`, confirmed from `orchestrator/stubs.py`) finding
  is written.

### Full test results (all run inside this worktree, `.venv`, `fleet.__file__` confirmed local)

- `tests/test_stub_resolution_task79.py` — **19 passed** (17 pre-existing + 2 new), 125.64s. This
  is the full covering set the brief named for `_run_revalidation_claims_impl`/
  `settle_revalidation`/`VerifyPipelineWorker`, and it includes
  `test_t3_stub_diverged_is_reached_through_the_real_claiming_loop_not_a_direct_call` — a genuinely
  FAILING real-`FakeBazel`-driven round through the real `fleet resume` CLI — confirming the new
  `context_policy`/`ctx.llm` wiring does not change that test's `STUB_DIVERGED` classification (the
  scripted `AnthropicBackendError` — no `ANTHROPIC_API_KEY` in this environment — is caught by
  `_diagnose`'s own `except LlmError: return TokenUsage()`, so the net observable effect is
  unchanged; this is the same "inert without a key" pattern `tests/test_heavy_tier_outage_e2e.py`
  already documents for this exact backend/role).
- `tests/test_budgets.py`, `tests/test_pr_e2e.py`, `tests/test_wave_composition_projects_mid_wave.py`
  — **64 passed**, 136.69s (covers `CostLedger`/`SpendScope`/`_emit_prs`'s own construction shape,
  the `_emit_prs` precedent this task mirrors, and another `VerifyPipelineWorker` consumer).
- `tests/test_build_e2e.py` — **87 passed**, 1057.31s (this file's real-Bazel fixtures are what
  `test_stub_resolution_task79.py` imports and builds on; also exercises `VerifyPipelineWorker`'s
  ordinary Phase-4 success path through `PhaseRunner`, proving the `usage=` fix's success-path
  construction doesn't regress the common all-green case).
- `ruff check src/fleet/cli.py tests/test_stub_resolution_task79.py` — clean.
- `mypy` (whole-package scope, run with `cwd` at the worktree root, `mypy_path = "src"` resolving
  from cwd per `pyproject.toml`) — `Success: no issues found in 130 source files`.

## Rule 12 — mutation proof

Mutated `scope = SpendScope(repo_id=repo_id, task_id=task_id, kind=SpendKind.REVALIDATION)` →
`kind=SpendKind.NORMAL` (the brief's own first example: "reverting the `SpendScope` wiring to a
no-op" — `NORMAL` kind bypasses both the sub-ceiling check in `_check_subceilings` and the
`revalidation_usd`-growing branch of `settle()`, which is exactly what "no-op" means for this
wiring). Confirmed the mutation genuinely changed the file (`diff` against a pre-mutation backup
copy, not `git diff` against `HEAD`, per Guardrail 12's zero-change-gate discipline — `209
insertions(+), 75 deletions(-)` reported by `git diff --stat` at the time, i.e. the mutation landed
inside an already-dirty tree, so the backup-diff check is what actually proves it, not a `HEAD`
comparison).

Both new tests went RED under the mutation:
- `test_..._records_a_real_nonzero_cost`: `AssertionError: (0.0, 0.0)` — `revalidation_usd` never
  moved (settled through the `NORMAL`, not `REVALIDATION`, CAS branch).
- `test_..._raises_and_routes_through_settle_revalidation`: `AssertionError: 'settled:
  verdict=PASS decisions=1'` — no breach ever fired (the sub-ceiling check is gated on
  `scope.kind is SpendKind.REVALIDATION`), and since this fixture's `FakeBazel` carries no `fail=`
  entries, the round ran for real and passed.

Reverted immediately after. `diff` against the pre-mutation backup copy of `cli.py` now reports
**no difference** (byte-identical), and `git diff --stat -- src/` reflects only this task's actual
feature commit. Re-ran `ruff`/`mypy` clean post-revert.

**Disclosed limit, per Guardrail 6 ("audit mutations for expressibility, not only pass/fail")**:
the brief's *second* example mutation — "reverting your `WorkerResult.usage=` fix" — was not
separately attempted, because (per the measurement above) neither new test's fixture can express
it: both drive a FAILING build to reach `_diagnose` at all, and a failing unit always returns
through the already-correct `_handoff` path, never through the success-path constructor this
task's companion fix touches. The `SpendScope`-kind mutation above is the one mutation this task's
tests actually discriminate; the `usage=` fix's own correctness rests on inspection (`_handoff`'s
existing precedent, `accumulate`'s existing semantics) and on it being currently unreachable dead
code with a real name and a real invariant, not on a red/green pair.

## Scope discipline

- Did not touch §12.11 or Legs B/D (sibling tasks, out of scope).
- Did not build the full real two-round `BUDGET_EXHAUSTED` CLI fixture (B2, a separate future
  task per ADR-0136's two-leg split) — this task's tests construct the scenario directly instead,
  per the brief's own explicit permission.
- Did not touch D135's broader fleet-wide `TokenEstimator`-zero-constructors gap outside the one
  `VerifyPipelineWorker` instance named in the brief — `PhaseRunner(estimate=)` at its four
  `cli.py` call sites remains unwired, `SpendKind.NORMAL` dispatches remain `ZERO_COST` reservations
  (D135 stays `OPEN`).
- Rule 3 (surgical changes): did not "improve" any other `WorkerContext`/`RunContext` construction
  site, did not rename anything not required by the new signature
  (`_run_one_revalidation_task` gained one new positional parameter, `run_ctx`).

## §12.39 status

Per ADR-0136/research-54 §7: B1 (this task) alone does not move §12.39 from unmet to met — B2 (the
real two-round CLI fixture) is required too, and is out of scope for this task. §12.39 stays out of
the `<n> of 48` count until both land; this report makes no claim otherwise.
