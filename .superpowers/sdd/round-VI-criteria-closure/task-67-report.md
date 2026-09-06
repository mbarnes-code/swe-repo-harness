# Task 67 report — §37 Leg 1: TRANSFORM-phase stub-creation DECISION

## Status
**DONE_WITH_CONCERNS** — see "Concern: the DEGRADED write bypasses `transition()`" below. Steps 1,
2, and 4 (doc fix) are clean with no open questions; step 3 (the `RUNNING → DEGRADED` correction)
is implemented and tested exactly as scoped, but the implementation necessarily bypasses
`models.enums.transition()`'s ordinary CAS gate, which I did not self-adjudicate — flagged for the
controller/reviewer per CLAUDE.md's Central Number Allocation / "do not force a fit" guidance.

## Branch / commit
Branch `agent/roundvi-task67`, NOT merged to `main`. One commit (see below) covering all of:
`src/fleet/orchestrator/stubs.py`, `src/fleet/cli.py`, `tests/test_stubs.py`, `tests/test_cli.py`,
`docs/SPEC.md`, `docs/CRITERIA_PLAN.md`.

## What was built

### Step 1 — trigger detection
- `src/fleet/orchestrator/stubs.py`: new `StubTrigger` dataclass and `detect_stub_triggers(...)`
  — a pure predicate (no DB), same split as the module's existing five transition functions and
  as `orchestrator.reentry.stub_permits_removal`. Takes the wave's dispatched repo ids, an
  already-filtered `(provider_id, dependent_id)` edge iterable (the exact shape
  `_ordering_pairs` returns), and a `Mapping[str, BlockerState]` of provider phase-statuses;
  returns the `(consumer, provider)` pairs needing a stub.
- `src/fleet/cli.py`: new `_detect_transform_stub_triggers(conn, settings, run_id,
  dispatched_repo_ids)` — the async DB-touching caller. It calls the REAL `_ordering_pairs` (not
  a re-derived copy of its filter) and the REAL `_read_blocker_states`, then delegates to the
  pure predicate above.
- **Finding, corrected from the brief's literal wording**: there is no `repos.status` column
  (checked `schema.sql`'s `repos` table — confirmed absent). A repo's status is a `phases`-table
  fact. "the provider's `repos.status = REQUIRES_HUMAN_INTERVENTION`" is implemented the same way
  the already-landed `stub_permits_removal` (Blocker A) reads the identical fact: ANY of the
  provider's `phases` rows reading RHI (not ALL — RHI is mechanically terminal), via the existing
  `_read_blocker_states`/`BlockerState.phase_statuses`. This is a re-derivation of an inherited
  finding at the point of implementation, per CLAUDE.md's "a finding perishes between filing and
  fix" — I did not implement a nonexistent column.
- `_repo_facts`/`_unit_deps` (cli.py:7601-7641/7713-7804 in the brief's citations) were treated as
  **READ-ONLY reference** exactly as the dispatcher's message anticipated: I call `_ordering_pairs`
  and `_read_blocker_states` (separate, pre-existing functions) and did not edit `_repo_facts` or
  `_unit_deps` themselves. My new code is a physically separate block inserted between the end of
  `_unit_deps` and the start of `_owned_coordinate_keys` (current `HEAD`'s line ~7804-7811),
  chosen specifically because it is outside both task-65's cited ranges (~3858-3902, ~11502-11514)
  and task-66's (~6244-6444, ~9367-9391).

### Step 2 — `StubRecord` construction + the `stubs` INSERT
- `orchestrator/stubs.py`: `build_stub_record(...)` — pure constructor; `pinned_version is None`
  is the entire fidelity decision (`EMPTY_FAILING` vs `PUBLISHED_ARTIFACT`), re-validated by
  `StubRecord._fidelity_matches_pin` at construction.
- `cli.py`: `_create_stub_records(conn, writer, *, run_id, triggers, facts,
  max_revalidation_rounds, now)` — for each trigger, reads `coord_key`/`pinned_version` off
  `facts[provider].published` (Blocker B's landed `version_spec`), computes `bazel_label =
  _internal_label(stub_dest(coord_key))` (byte-identical to what `_unit_deps`'s Blocker-C stub
  redirect branch expects to read back), and INSERTs into `stubs` matching every column/CHECK in
  `schema.sql:410-445`. **Idempotent by check-then-skip** at the schema's own PRIMARY KEY
  `(run_id, repo_id, stub_coord_key, revalidation_round=0)` — a repeat call over an unchanged
  database inserts nothing a second time (verified by test). A trigger whose provider has no
  published `Coordinate` at all is silently skipped (defensive; not expected to fire per
  `_unit_deps`'s own resolution guarantee — documented in the docstring, not asserted as
  unreachable).

### Step 3 — the `RUNNING → DEGRADED` correction
- `orchestrator/stubs.py`: `stub_completion_correction(*, current_status, has_active_stub) ->
  RepoStatus | None` — pure decision: `DEGRADED` iff `current_status is SUCCEEDED and
  has_active_stub`, else `None`.
- `cli.py`: `_correct_transform_status_for_stubs(conn, writer, *, run_id, repo_id, now)` — reads
  the repo's current TRANSFORM `phases.status` and whether it has an `ACTIVE` `stubs` row, applies
  the decision above, and if it fires, writes `phases.status = 'DEGRADED'` via raw SQL.

**Chosen site, and why (the brief's own "genuinely open" item):** a **follow-up write**, intended
to run once a wave's stub set is known (i.e., after `_run_transform_wave` returns for the wave and
`_create_stub_records` has run for it) — **not** an edit to `workers.base.WorkerBase.execute` or
`orchestrator.runner.PhaseRunner._dispatch`, both generic and shared by every phase/worker type,
which the brief explicitly puts out of scope. I traced why no other site works:
- `WorkerResult` has no status literal that the generic dispatch loop maps to `RepoStatus.DEGRADED`
  at all today (`"ok"` → `SUCCEEDED`, `"partial"` → `PENDING`, everything else → the retry ladder).
- A within-fence write timed to race the generic completion doesn't survive it:
  `ALLOWED_TRANSITIONS[RepoStatus.DEGRADED]` legally permits `DEGRADED → SUCCEEDED`, so the
  generic dispatch's own subsequent `_complete(repo_id, fence, RepoStatus.SUCCEEDED, None)` call
  (same fence, since `complete_phase`'s UPDATE never bumps `lease_fence`) would silently win and
  undo the correction.
- The only tractable site is therefore strictly after the wave's dispatch loop has finished with
  that repo's fence for good — a genuine follow-up, unfenced write.

## Concern: the DEGRADED write bypasses `transition()`
`ALLOWED_TRANSITIONS[RepoStatus.SUCCEEDED]` is the empty set (terminal, checked in
`models/enums.py`). By the time the follow-up write runs, `phases.status` already reads
`SUCCEEDED` (the generic completion loop wrote it), so there is **no legal CAS** this correction
can go through — `transition(SUCCEEDED, DEGRADED)` would correctly raise `ValueError`. I did not
route the write through `transition()`; `_correct_transform_status_for_stubs` writes
`phases.status` directly via raw SQL, precedented by `cli._quarantine_impl`'s own existing raw-SQL
write of `status = 'SKIPPED'` for a different reason (an operator action outside any worker's
lease) — but `_quarantine_impl` never actually violates `ALLOWED_TRANSITIONS` in practice (its own
pre-write `transition()` check would refuse a currently-`RUNNING` row; the schema comment confirms
quarantine is only ever exercised against `PENDING`/`BLOCKED` rows). My correction is different:
it deliberately targets a row already at the terminal `SUCCEEDED` status, which `transition()` has
no edge for. This is disclosed in both docstrings (`stub_completion_correction` and
`_correct_transform_status_for_stubs`) rather than silently patched around, per CLAUDE.md's stop
rule ("a fake mechanism is worse than an honest disclosure"). I did **not** self-allocate an ADR
or extend `ALLOWED_TRANSITIONS` — whether `SUCCEEDED → DEGRADED` should become a modelled edge (a
state-machine change) is a design decision I'm surfacing, not making. This is exactly the shape
CLAUDE.md's brief-completion escape valve names ("the DEGRADED-correction site turns out to need a
state-machine change this brief did not anticipate") — I chose to implement and disclose rather
than report whole-task BLOCKED, since steps 1/2/4 are unaffected and complete, and the brief itself
invited "report which site you chose and why" as an acceptable outcome. Flagging this explicitly
for controller attention.

## Step 4 — doc fix (same commit)
- `docs/SPEC.md` §3.5 item 1: added one clarifying sentence immediately before the
  `workers/buildgen.py` emission sentence, naming `cli.py`'s `_detect_transform_stub_triggers`/
  `_create_stub_records` as the TRANSFORM-phase decision site. The buildgen.py sentence itself is
  unchanged (correct for the render step, which is Leg 2 / task-68).
- `docs/CRITERIA_PLAN.md` §37: added a dated "Update, round VI task 67 (2026-09-06) — resolved."
  paragraph immediately after research-36's "Disclosed, not resolved, by this same pass" paragraph
  (kept intact, unedited, per the "annotate, never rewrite" ledger discipline), citing this task's
  commit and the landed function names.
- No Rule-14 process, no ADR: this is a documentation-currency correction of an already-disclosed
  tension, not a §12 criterion wording change, per the brief.

## Not touched (out of scope, confirmed)
- `_validate_transform_flags`/`_validate_build_flags`/`_validate_resume_flags` — all three
  `--stub-blocked` refusals are untouched; `--stub-blocked` still refuses unconditionally. None of
  this leg's new functions are wired into `_transform_impl`/`_run_transform_wave`'s real
  production path — exactly as the brief's flag-gating note directs ("Build and unit-test your
  trigger-detection/creation function directly ... not through the CLI"). I extended that same
  reasoning to step 3 as well: since no `stubs` row is ever created in production this round, a
  live call site for the correction would be unreachable and untestable end to end before task-69
  exists.
- `_eligible_build_units`, `workers/buildgen.py`, `BuildgenWorker`, `workspace_deps()` wiring,
  `EMPTY_FAILING` target rendering — all confirmed untouched (task-68's territory).
- `orchestrator/stubs.py`'s five existing functions (`supersede`, `plan_revalidation`,
  `settle_revalidation`, `reconcile`, `next_round_record`) — untouched; only new functions added.
- Transitive stub stacking — untouched, correctly deferred per research-7 §5.

## Tests run (whole files, no `-k`, in the worktree's own `.venv`)
- `tests/test_stubs.py` — 40 passed (5 new: trigger-detection positive + 3 discriminating
  negatives + an absent-provider case, `build_stub_record` x2, `stub_completion_correction` truth
  table).
- `tests/test_workers_transform.py` — 46 passed (unaffected; zero regressions).
- `tests/test_transform_e2e.py` — 13 passed (unaffected; zero regressions).
- `tests/test_cli.py` — 193 passed (4 new: `_detect_transform_stub_triggers` positive + 3
  discriminating mutations run through the REAL `_ordering_pairs`/`_read_blocker_states` queries
  including the confidence-threshold case the pure predicate alone cannot reach;
  `_create_stub_records` for both fidelities + idempotency, verified via a plain `sqlite3`
  connection reading the CHECK-constrained row back; `_correct_transform_status_for_stubs` firing
  only with an active stub).
- `tests/test_lint_gate.py` — 7 passed (`ruff check --no-cache .` clean across the whole repo,
  including the new test/source additions after fixing line-length, unsorted-import, and a missing
  local `aiosqlite` import in two test helpers).
- `python -m mypy --strict src/fleet/` (no path narrowing) — `Success: no issues found in 129
  source files`.

## Mutation proof (Rule 12), in a fresh env-isolated interpreter pinned to this worktree
Ran via `env -i PATH=/usr/bin:/bin HOME="$HOME" PYTHONPATH="<worktree>/src" <worktree>/.venv/bin/
python`, asserting `fleet.orchestrator.stubs.__file__` resolves under the worktree path **before**
trusting any result, per CLAUDE.md's import-isolation guardrail.

1. **Trigger predicate.** Mutated `detect_stub_triggers`'s RHI check (`if state is None:` instead
   of `if state is None or RHI not in state.phase_statuses:`). `git diff` against a pre-mutation
   backup copy (not `HEAD`) confirmed a genuine, non-zero change. Result: exactly
   `test_detect_stub_triggers_does_not_fire_when_the_provider_is_not_rhi` reddened (1 failed, 4
   passed in that file's trigger tests) — a real discriminator, not a module-wide outage. Reverted
   from the backup; `git diff` against it returned to zero; full `test_stubs.py` re-ran green
   (40/40).
2. **DEGRADED write.** Mutated `stub_completion_correction` (`if current_status is
   RepoStatus.SUCCEEDED:` instead of `... and has_active_stub:`). Backup-diff confirmed a genuine
   change. Reddened both the pure-function test
   (`test_stub_completion_correction_fires_only_on_succeeded_with_an_active_stub`) AND the DB-layer
   test (`test_correct_transform_status_for_stubs_flips_succeeded_to_degraded`), proving the
   discrimination survives through the actual SQL write, not only in the pure decision function.
   Reverted from backup; both tests green again; full `test_stubs.py` + the four new `test_cli.py`
   tests re-ran green (5/5).

## Concerns summary (for the controller)
1. **Primary concern**: the `SUCCEEDED → DEGRADED` correction bypasses `models.enums.transition()`
   because `ALLOWED_TRANSITIONS[SUCCEEDED]` is empty. Implemented, tested, and disclosed in both
   docstrings; not self-adjudicated. Recommend the controller decide whether this needs its own
   ADR (extending `ALLOWED_TRANSITIONS`, or formally blessing the raw-SQL bypass as this leg's
   design) before task-69 wires the bundle end to end.
2. None of the three new async cli.py functions are reachable from the CLI this round (by design,
   matching the brief's flag-gating instruction) — task-69 is expected to wire trigger
   detection/creation into the real per-wave loop and decide the actual call-site wiring for the
   DEGRADED correction against whatever the controller decides on concern 1.
3. No ADR or D-number was needed or self-allocated — the doc fix (step 4) is a documentation-
   currency correction of an already-disclosed tension, not a §12 criterion wording change.
