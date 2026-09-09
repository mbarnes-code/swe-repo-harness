# Round VI, Task 103 — §12.39 case (i): `STUB_DIVERGED` through the real REVALIDATE claiming loop

**Status: DONE (case (i) only) / case (ii) explicitly NOT attempted, blocked on §12.39-B.**
Commit range on `agent/roundvi-task103`: to be created by this task's own commit (see below);
branched off `main` at `13aeb55c22789d8acba52bb3abc6edf1d55e1a0c`.
Worktree: `/tmp/claude-1000/-home-redmage-swe-repo-harness/168ce0b2-2e48-46a6-837c-4cb29f1a8475/scratchpad/task103/wt`.
Not merged, not pushed.

## Scope, per the brief

`docs/SPEC.md` §12 item 39 has 3 cases. Case (iii) is out of scope (already closed, brief says
not to touch it). This task's scope was case (i) (`STUB_DIVERGED`) "in full, wired for real",
plus case (ii) (`BUDGET_EXHAUSTED`) "only if driveable without inventing a price."

research-52 (read in full before starting) established, and this task independently re-verified
against the actual checked-out tree at `13aeb55` (not inherited):

- `src/fleet/cli.py`'s only production caller of `settle_revalidation`
  (`_run_one_revalidation_task`, formerly cited as `_revalidate_one`) never passed
  `failure_class=` — confirmed by reading the call site directly before editing anything.
- `SpendKind.REVALIDATION` has zero production constructors anywhere in `src/`. Re-verified
  independently here: `grep -rn "SpendKind.REVALIDATION\|SpendScope(" src/` outside
  `budgets.py` returns exactly one hit, `orchestrator/runner.py:417`'s `SpendScope(...)`
  construction, which passes no `kind=` and therefore defaults to `SpendKind.NORMAL`. Confirmed
  also that `_run_one_revalidation_task`'s `WorkerContext` carries `budget=CallBudget(
  remaining_tokens=0, remaining_usd=0.0, ...)` and holds no `CostLedger` at all — there is
  nothing for a revalidation round to spend, and no seam through which to charge it, without
  building new plumbing research-52 explicitly scoped OUT of this task (§12.39-B, its own
  design pass).

**Case (ii) was not attempted.** Driving `BUDGET_EXHAUSTED` without inventing a price would
require either (a) declaring a per-round dollar figure with no SPEC backing (a Rule 1 violation
— an undisclosed assumption entering `docs/`), or (b) building the `CostLedger`-in-
`_run_one_revalidation_task` plumbing research-52 named as its own open design question. Both are
explicitly out of this task's scope per the brief ("Do NOT touch `SpendKind.REVALIDATION`
production constructors or attempt §12.39-B's pricing design"). Reported here as deferred/blocked
on §12.39-B, not as a failure of this task — case (i) is a complete, real result on its own.

## Case (i): the fix

**`src/fleet/cli.py`**, new function `_revalidation_round_stub_diverged` (placed immediately
after `_round_index_from_revalidation_key`, before `_run_one_revalidation_task`): derives
§12.39(i)'s two-halved differential from real state, rather than accepting it as a parameter.

- **Half A** — the immediately preceding stub-limited verification for this consumer passed.
  Read from the single-slot `VerificationReport` finding row (`VERIFICATION_KIND`,
  `_record_verification`'s own upsert target) BEFORE this round's own call to
  `_record_verification` overwrites it. This ordering dependency is the same fragility
  research-52 §1.3 flagged ("usable, but fragile") — disclosed in the function's own docstring,
  not solved here (a future reordering, or a second revalidation round reusing the same slot,
  would destroy the evidence).
- **Half B** — the round's real commit set on `migrate/<consumer>` contains only Phase-3
  (`Fleet-Phase: 3`) emissions. Reuses the EXACT computation `_rewrite_superseded_consumer_
  labels` already runs (`monorepo.log(f"{base}..{branch}", trailer_keys=[PHASE_TRAILER])` /
  `str(int(Phase.BUILD))`), not re-derived. This is narrower than SPEC's literal "only the
  dependency-label rewrite" — disclosed, matching research-52 §1.3's second named narrowing: a
  legitimate BUILDGEN re-emission on this branch would also pass this check.

The one caller (`_run_one_revalidation_task`) now computes `failure_class` before the
`settle_revalidation` loop: `FailureClass.STUB_DIVERGED` when `report.verdict != "PASS"` AND
`_revalidation_round_stub_diverged(...)` is true, else `None` — and passes it into every
`settle_revalidation(...)` call for this round's stub records (a batched round covers several
`stubs` rows; the same derived classification applies to all of them, matching the existing
`sibling_states` computation's own once-per-round shape).

Everything downstream of this classification was already built (research-52 §1.2's table):
`StubRot` finding, `stubs.state='ABANDONED'`/`abandon_reason='STUB_DIVERGED'`, consumer →
`REQUIRES_HUMAN_INTERVENTION` via `apply_stub_consumer_status`, and `phases.attempts`
untouched by construction (`_run_one_revalidation_task` never writes an `attempts` row). No
change was needed to any of that.

## Test — real, not a direct call

**`tests/test_stub_resolution_task79.py`**, new test
`test_t3_stub_diverged_is_reached_through_the_real_claiming_loop_not_a_direct_call`, on the
task-79 fixture per the brief. Shares steps 1-4 verbatim with the sibling
`test_the_full_stub_lifecycle_resolves_through_the_real_cli_end_to_end` (real STUB_LIMITED
consumer verify, real provider retry/build/verify, a real provider PR merged and discovered by
`fleet pr --sync`, D107's real synchronous label rewrite) and diverges only at step 5: a
DIFFERENT `FakeBazel` answers the consumer's own `bazel build` with a non-zero exit code, which
is the genuine build failure §12.39(i) requires. Both halves of the differential are real facts
present in the fixture's own topology already (nothing else touches `migrate/acme-app-py` between
the ordinary Phase-3 label-generation commit and D107's rewrite, and step 1's STUB_LIMITED verify
genuinely passed) — neither is handed in.

Assertions, all read back from real DB state after a real `fleet resume`: the claiming loop's own
outcome string (`"settled: verdict=FAIL decisions=1"`), `stubs.state='ABANDONED'` +
`abandon_reason='STUB_DIVERGED'`, the consumer's Phase-4 row moving to
`REQUIRES_HUMAN_INTERVENTION` with `attempts` UNCHANGED from its pre-round value (captured before
and compared after, not merely asserted zero), and a `StubRot` finding row.

**Disclosed, not asserted**: this fixture's `acme-app-py` has no dependent repo anywhere in this
suite's fixtures (`tests/test_transform_e2e.py::DESTINATIONS` names only the four acme repos, no
`X -> acme-app-py` edge), so §12.39(i)'s `blocked_by` propagation clause has nothing to propagate
to here. research-52 §1.4 traces that propagation to a SEPARATE cross-invocation sweep
(`cli.py:6739-6790`) that only runs on a LATER phase dispatch — out of this test's scope, not
silently assumed to hold.

## Verification run (measured, not inherited)

Both pytest runs below used `env -i PATH=/usr/bin:/bin HOME="$HOME"
PYTHONPATH="$WT/src" .venv/bin/python`, with `sys.executable`/`fleet.__file__` confirmed resolving
inside this worktree before trusting any result (CLAUDE.md's pinned-interpreter discipline).

- **Pre-mutation, full covering set** (`tests/test_stub_resolution_task79.py
  tests/test_stubs.py`, whole files, no `-k`): **61 passed in 157.65s**, clean `bazel disk` line
  (peak 1.68 GiB / ceiling 6 GiB, 0 residual output bases), 0 tests skipped.
- **`mypy` (no path args, package-scoped via `pyproject.toml`)**: clean, 130 source files.
- **`ruff check` on both touched files**: clean.

## Rule 12 mutation proof

Mutation: reverted the caller's `failure_class=failure_class` kwarg back to the pre-fix call
shape (`settle_revalidation(stub, report, sibling_states=sibling_states)`), leaving the new
`_revalidation_round_stub_diverged` derivation function itself untouched (dead code under the
mutation). Confirmed the mutation actually changed the file BEFORE reading any test result:
`git diff --numstat --no-index <backup> <mutated>` → `2 2` (non-zero), and independently
confirmed the running interpreter loaded the mutated source (`inspect.getsource
(cli._run_one_revalidation_task)` contains the mutation's own marker comment) before trusting the
red result.

- **New test alone, under the mutation**: **1 failed** — falls back to `"another_round:
  verdict=FAIL decisions=0"` (no decision at all, since `rounds_spent=1 < max_revalidation_rounds`
  in this fixture — not even the pre-fix `ROUNDS_EXHAUSTED` fallback research-52 predicted, an
  even earlier fallback, but still a clean discriminating failure).
- **Full covering set, under the same mutation**: **1 failed, 60 passed in 105.19s** — every
  other test touching `settle_revalidation` (including `tests/test_stubs.py::
  test_t3_on_stub_diverged_sends_the_consumer_to_a_human` and `::
  test_t3_on_budget_exhaustion_reuses_the_ledger_breach_and_never_promotes`, both cited in the
  brief) stayed green. Only this task's own new test discriminates.
- Mutation reverted (`cp` from the pre-mutation backup); `git diff --stat src/` back to 79
  insertions / 1 deletion — the actual fix only, no mutation residue. Re-ran the new test alone
  post-revert: **1 passed in 7.35s**.

## Concerns / disclosed narrowings (carried from research-52, not new)

1. Half A's read is ordering-fragile (see above) — flagged in the new function's own docstring,
   not fixed here; out of this task's scope.
2. Half B accepts any Phase-3 (BUILD) emission on `migrate/<consumer>`, not only the specific
   label-rewrite commit — narrower than SPEC's literal wording, disclosed in the same docstring.
3. §12.39(i)'s `blocked_by` propagation clause is not exercised by this fixture (no dependent
   repo) — disclosed above, not silently assumed.
4. **Case (ii) BUDGET_EXHAUSTED was NOT attempted** — confirmed, independently of research-52,
   that `SpendKind.REVALIDATION` has zero production constructors and `_run_one_revalidation_task`
   holds no `CostLedger`. Driving it would require either inventing an undisclosed price (Rule 1
   violation) or building the plumbing §12.39-B's own design pass is meant to scope. This remains
   open work for a future task, gated on that design pass as research-52 §1.6 lays out (four
   options, Option A recommended).
5. §12.39 as a whole is **narrowed, not closed**, by this task — case (i) alone. Rule 13/14
   honesty: this round's own checkpoint should say "§12.39 case (i) closed; case (ii) blocked on
   §12.39-B" rather than "§12.39 closed."
