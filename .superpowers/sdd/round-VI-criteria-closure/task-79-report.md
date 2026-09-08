# Task 79 report — D107 (label rewrite) + D104 (REVALIDATE claiming loop) + D108 (consumer
promotion), bundled per ADR-0128

**Branch:** `agent/roundvi-task79`. **Commits (in order):**
- `4ead8f9` — production code + tests + SPEC.md/CRITERIA_PLAN.md/schema.sql updates
- `0b78560` — `docs/INTEGRATION_HONESTY.md` status updates (D104/D107/D108 → FIXED, LANDED; new D129)
- `aabead1` — fix round: D107/D104(b) must not abort `fleet pr --sync`/`fleet resume` when the
  monorepo checkout is unavailable (found by running `tests/test_cli.py` whole-file, no `-k`)

Not merged to `main`, per SDD protocol — awaiting review.

## What landed

**D107** (`cli._rewrite_superseded_consumer_labels` / `_rewrite_one_consumer_label`): fired from
`_pr_sync_impl`'s existing T1 trigger (D102's own call site — no new signal), for every distinct
`consumer_repo_id` `supersede()` returns. Cuts a fresh worktree from `migrate/<consumer>`, rebases
onto the current `integration` tip, re-derives the `BuildUnit` via `_dest_sources`/
`_external_coordinates`/a fresh `_unit_deps` call (all reused unmodified), renders via
`BuildgenWorker` unmodified (`write_module_bazel=False` — an internal label swap never touches an
external coordinate, so MODULE.bazel cannot change; Agent Recommendation, not SPEC-mandated),
diffs the worktree against `HEAD`, and commits+pushes via `vcs/commits.py::guard`/
`apply_and_commit` with standard `Fleet-*` trailers, mirroring `_promote_one_pr`'s rebase +
`push_force_with_lease` shape.

**Judgment-call-6 precondition — corrected from the ADR's own literal pseudocode, disclosed.**
ADR-0128 (and SPEC §3.5.1 step 2) describe comparing `migrate/<consumer>`'s tree to its own "last
Phase-2 commit"'s tree. Empirically this does not correspond to what `filter_repo.py::ingest()`
(D115) actually does: `ingest()` force-moves `migrate/<repo>` to point at Phase 3's own MERGE
commit on every ingest (`vcs/filter_repo.py:466`), not a Phase-2 commit — so the literal
trailer-walk check would report "drift" on every single consumer, every time, including the
never-before-touched common case. The mechanically equivalent check for this codebase: every
commit unique to `migrate/<consumer>` relative to `integration` must be either none (the ordinary
case: ingest's merge is already an ancestor) or one of this function's own prior rewrites
(identified by its own `Fleet-Phase: <BUILD>` trailer) — anything else is undiscussed drift and
fails loud. This is a mechanical correction to *how* the check is expressed, not a change to the
ADR's *intent* ("assert no unexplained drift since ingest, fail loud otherwise, don't guess"). Full
reasoning is in `_rewrite_one_consumer_label`'s own docstring and in the `docs/
INTEGRATION_HONESTY.md` D107 entry.

**D104(a)** (`VerifyInput.revalidation_round`, threaded into `RdepverifyInput` of the same name):
previously the field existed on `RdepverifyInput` but nothing above it ever set it — a REVALIDATE
re-run would always report round 0, defeating `settle_revalidation`'s
`max(stub.rounds_spent, report.revalidation_round)`.

**D104(b)** (`cli._run_revalidation_claims_impl` / `_run_one_revalidation_task`): a claiming loop
over `tasks WHERE kind = 'REVALIDATE' AND status = 'PENDING'` via `claim_task_by_id` (NOT a
`WaveScheduler` wave, per judgment call 7). Cuts a detached worktree from the consumer's
(now-rewritten) `migrate/<consumer>` tip, builds a `VerifyInput` with `verified_against_stubs`/
`stub_fidelity` from `_active_stubs_by_consumer` (unmodified — `state = 'ACTIVE'` only), calls
`VerifyPipelineWorker.run()` directly (unmodified), builds/reads the resulting
`VerificationReport`, gathers the `StubRecord`s this task's round covers
(`stubs.revalidation_task_id`, D103 gap 2) and their siblings' current states, and calls
`settle_revalidation` per coord_key. Wired into `fleet resume` as a new, unnumbered step between
step 6 (unblocking) and step 7 (projection regen) — matching the existing `stub_reconcile`
insertion's own convention (unnumbered, to avoid the citation-rot hazard CLAUDE.md §7 names for
renumbering a cited list) — and, unlike `stub_reconcile`, skipped under `--dry-run` (it performs
real git/Bazel I/O; Agent Recommendation, not pinned in the ADR).

**Deliberate `preconditions_hold` omission for `VerifyPipelineWorker`, worth reviewer attention:**
`VerifyPipelineWorker.preconditions_hold` exists for `PhaseRunner._re_entry`'s CHECKPOINT
re-entry question only (`_re_entry`'s own "if checkpoint is None: return ReEntry.FRESH" skips it
entirely for a fresh dispatch) and reads `if payload.remaining_units is None: return False` — the
correct value for "every unit owed" on a fresh dispatch, which this method would otherwise
misread as a re-entry payload missing its remaining units and refuse every claim this loop ever
makes. Confirmed empirically: calling it unconditionally made every real-Bazel claim in this
task's own headline test fail with "preconditions do not hold" until removed. `_run_one_
revalidation_task`'s own docstring/comment explains this in full.

**D108** (`SqliteStateRepository.apply_stub_consumer_status`, new): mirrors
`stub_degrade_transform`'s (ADR-0124) transaction shape in the opposite direction — one `BEGIN
IMMEDIATE`, a CAS `UPDATE phases SET status = ?` guarded on `status = 'DEGRADED'` (the row's
expected prior status), a `StubConsumerStatusApplied` findings row. Called from the REVALIDATE
loop for every decision whose `consumer_status is not RepoStatus.DEGRADED` — in production, only
T2-all_clear and T3-`STUB_DIVERGED` ever carry one. `RepoStatus.DEGRADED`'s `ALLOWED_TRANSITIONS`
entry already names both `SUCCEEDED` and `REQUIRES_HUMAN_INTERVENTION` unconditionally (§3.5.1's
own "DEGRADED is resolvable" invariant), so no new `transition()` door was needed — unlike
`stub_degrade_transform`/`reopen_to_pending`, which each open one.

**D129 (new, found and fixed in the same commit):** `bazel/query.py::rdeps_query`'s
`affected_only=True` branch rendered `rdeps(//..., set(kind(rule, //<dest>/...)))` — `set()` fed a
nested query expression, which is invalid Bazel query syntax. Confirmed against a real `bazel
query` in a throwaway single-package workspace with zero harness code involved (`ERROR: ... syntax
error at '( rule ,'`); fixed to `rdeps(//..., kind(rule, //<dest>/...))`. This had never been
exercised under real Bazel anywhere in this tree (no real-Bazel Phase-4 `verify` test existed
before this task) and would have broken every real-Bazel Phase-4 `verify` dispatch using the
CONFIGURED DEFAULT (`affected_only: true`), not only stub revalidation. Fixed here because it
directly blocked this task's own required real-Bazel proof (CLAUDE.md Rule 11 disclosure, not a
scope expansion); `tests/test_bazel.py`'s two affected string assertions updated to match. Full
`docs/INTEGRATION_HONESTY.md` D129 entry landed.

**Fix round (`aabead1`):** running `tests/test_cli.py` whole-file (no `-k`, per CLAUDE.md's own
"derive the covering set" discipline) surfaced 3 real regressions: `_rewrite_superseded_consumer_
labels`/`_run_revalidation_claims_impl` both called `_monorepo_checkout` unconditionally once T1
fired / a REVALIDATE task existed, and 3 pre-existing D102/D105/D106 tests drive T1 against a
lightweight, monorepo-less fixture — `MonorepoUnavailableError` propagated and aborted the WHOLE
`fleet pr --sync`/`fleet resume` command. Fixed by catching it and reporting per-consumer/per-task,
matching ADR-0128 judgment call 1's own "async follow-on work" framing (T1's own effect and every
OTHER repo's merge-detection must not depend on this follow-on step's own preconditions).

## Docs updated in the same commits (Rule 13/14)

- `docs/SPEC.md` §11.5: a dated `Marker` block (matching the existing `stub_reconcile`-insertion
  convention) documenting the new REVALIDATE-claiming step's insertion point, unnumbered.
- `docs/SPEC.md` + `schema.sql`: both findings-kind listings gain `'StubConsumerStatusApplied'`
  (`tests/test_findings_kinds.py` enforces the two stay in sync — re-verified clean).
- `docs/CRITERIA_PLAN.md` §37: a dated update recording D104/D107/D108 landed by this task,
  explicitly NOT claiming §37/§12.37 itself is DONE — D123/D124 (out of this task's scope, per
  §14's own citation of a shared done bar) are neither confirmed nor denied as additional
  prerequisites; left for a future re-measurement against SPEC's literal §12 item 37 text.
- `docs/INTEGRATION_HONESTY.md`: D104/D107/D108 headings → `FIXED, LANDED (round VI task 79,
  4ead8f9)`, with dated body annotations appended (existing OPEN-era prose never rewritten, per
  ledger discipline) explaining what fixed each. New D129 entry.

## Required test-proof accounting (brief's numbered items)

All in `tests/test_stub_resolution_task79.py` unless noted.

1. **Real consumer→provider edge, provider RHI, stub ACTIVE, consumer's committed BUILD.bazel
   names the stub label.** `_reach_active_stub_state()` — exact reuse of `test_build_e2e.py::
   test_an_active_stub_redirects_the_consumers_generated_dependency_to_the_stubs_label`'s own
   fixture (`acme-app-py` → `acme-lib-py`, `FakeBazel` only for the ORIGINAL provider-failure
   setup, matching that test's own precedent). Verified via `_git_show(monorepo,
   "integration:<dest>/BUILD.bazel")` in `test_d107_rewrites_the_committed_migrate_branch_off_
   the_stub_label` — names the stub label; `migrate/<consumer>` itself has no `BUILD.bazel` yet
   (D115's `ingest()` topology, see above).

2. **Provider fixed, PR reaches MERGED.** Driven two ways: (a) the D107/idempotency/negative-proof
   tests supersede the stub row directly (T1's own DB effect, mirroring `test_a_superseded_stub_
   leaves_the_consumers_generated_dependency_on_the_real_label`'s own hand-seed convention — this
   file drives the label-rewrite/REVALIDATE mechanisms as UNITS, not the full T1 CLI trigger,
   exactly as that existing test drives label computation as a unit); (b) the real-Bazel headline
   test additionally drives `fleet retry acme-lib-py` (§12.14) + a REAL `fleet build --repo
   acme-lib-py` (no seam) to get a genuinely `SUCCEEDED` provider with a real `py/acme_lib_py/
   BUILD.bazel` on `integration` before superseding. **The full T1 CLI trigger itself (`fleet pr
   --sync` observing a real `gh`/forge MERGE) is proven by D102's own existing
   `tests/test_pr_e2e.py::test_pr_sync_fires_t1_and_enqueues_a_revalidate_task_for_a_merged_
   providers_stub` and 2 siblings, all still green (25/25 in `test_pr_e2e.py`, no `-k`) — this
   task's own wiring into that same call site is proven by `test_cli.py`'s 3 tests this fix round
   restored (they exercise T1 firing through the real code path with `_rewrite_superseded_
   consumer_labels` genuinely invoked, catching `MonorepoUnavailableError` gracefully).**

3. **Assert the ACTUAL committed file off `migrate/<consumer>`, not the `stubs` table / `_unit_
   deps`.** `test_d107_rewrites_the_committed_migrate_branch_off_the_stub_label`: reads `git show
   migrate/acme-app-py:<dest>/BUILD.bazel` after calling `_rewrite_superseded_consumer_labels`
   directly — asserts the real provider label present, stub label absent. PASSED.

4. **REVALIDATE task through the new claiming loop, REAL Bazel build+test, `verified_against_
   stubs == []` reflecting a build that actually ran against the real target — real Bazel
   introspection, not a second `stubs` read.**
   `test_d104b_claiming_loop_resolves_the_stub_under_a_real_bazel_build_and_test` — `@pytest.mark.
   integration`, `cli.BAZEL_RUNNER = None` (no seam) for the whole provider-rebuild +
   rewrite + REVALIDATE sequence. Asserts: (a) the persisted `VerificationReport` findings row has
   `build_ok=True` and `verified_against_stubs=[]`; (b) a REAL `bazel query
   deps(py/acme_app_py/...)` over a fresh worktree cut from the post-revalidation
   `migrate/acme-app-py` tip names the real provider label and NOT the stub label. **Passed,
   confirmed real Bazel ran** (bazel disk report: peak 1.22 GiB, real bazel server startup in
   logs). One fixture limitation disclosed rather than hidden: `acme-app-py`/`acme-lib-py` (this
   suite's real, minimal Python fixture packages) declare no `py_test` target, so a real `bazel
   test` genuinely reports "no test targets" (a real, nonzero Bazel exit) — the outcome assertion
   accepts `settled:`/`another_round:` (never `FAILED:`, which would mean the LOOP itself erred)
   rather than requiring `verdict=PASS` in THIS specific real-Bazel test. T2/D108's own PASS-driven
   promotion is proven separately (item 5, under `FakeBazel`, explicitly permitted by the brief for
   auxiliary checks) rather than silently dropped.
   Also found and fixed while building this: **D129** (see above) — without it this test could
   never get past a real `bazel query` syntax error, on ANY fixture.

5. **Consumer's `phases` row reaches SUCCEEDED (D108), `stubs.state == 'RESOLVED'`.**
   `test_d108_promotes_the_consumer_once_a_revalidation_round_genuinely_passes` — driven under
   `FakeBazel` (all-green, no `fail=` entries) so the claiming loop's build+test genuinely PASSes
   deterministically. Asserts `stubs.state == 'RESOLVED'`, `phases` VERIFY row `== 'SUCCEEDED'`,
   and a `StubConsumerStatusApplied` finding exists. PASSED.

6. **Negative-proof mutation (Rule 12): D107 skipped ⇒ `verified_against_stubs` false-empty.**
   `test_a_revalidate_round_without_d107s_rewrite_reads_false_empty_verified_against_stubs` —
   supersedes the stub via T1's DB effect only (D107's rewrite deliberately never called), proves
   via `git show` that `migrate/<consumer>` never received even a first commit (D107 never ran)
   while `integration`'s own copy still names the stub label, then reads
   `_active_stubs_by_consumer` directly (the exact input a REVALIDATE dispatch's `VerifyInput`/
   `RdepverifyInput.verified_against_stubs` would be built from) and asserts it is `{}` — i.e.
   false-empty, purely from the `state = 'SUPERSEDED'` DB row, with no real build ever having
   touched the real dependency. This is ADR-0128's whole safety argument made falsifiable, per the
   brief's own "if no defensive check is designed" fallback (no defensive check was added — the
   safety property is structural, from D107 always landing first in the SAME trigger, not from a
   runtime guard in the REVALIDATE loop). PASSED.

7. **Idempotency: replay, no duplicate commit, no duplicate REVALIDATE row, `already_applied`
   correctly reported.** `test_d107_is_idempotent_on_replay` — calls `_rewrite_superseded_
   consumer_labels` twice on the same superseded state; second call reports `"already_applied"`
   and `git rev-list --count migrate/acme-app-py` is unchanged. PASSED. (REVALIDATE-task-row
   idempotency is D102's own already-tested `ux_tasks_ident`/`revalidation_key` mechanism, reused
   unmodified — not re-tested here per the brief's own "reused unmodified" scope boundary.)

## Verification run (this session)

- `python -m mypy` (no path args, manifest-scoped `packages = ["fleet"]`): **clean, 129 files**,
  run after every production-code edit round.
- `ruff check` (`src/`, `tests/test_stub_resolution_task79.py`, `tests/test_bazel.py`): **clean**.
- `tests/test_stub_resolution_task79.py` (5/5, whole file, no `-k`): **passed**, including the
  real-Bazel test.
- `tests/test_bazel.py` (whole file, no `-k`): 71/71 passed on a first run; the 2 unrelated
  real-Bazel failures on a LATER run were traced to shared-host disk exhaustion (`df` showed 755M
  free / 591G total on this multi-tenant box) — re-ran clean (73/73) after freeing this session's
  own scratch `tools/bazel-test-root` (confirmed NOT caused by this task's changes).
- `tests/test_pr_e2e.py` (whole file, no `-k`, `-m "not integration"`): **25/25 passed**.
- `tests/test_repository.py` + `tests/test_state_models.py`: **197/197 passed**.
- `tests/test_build_e2e.py` (`-m "not integration"`): **55/55 passed** (22 real-Bazel tests
  deselected to conserve the shared host's disk — not re-run this session beyond the two already
  confirmed clean above and this task's own new real-Bazel test).
- `tests/test_stubs.py`, `tests/test_resume_continue.py`, `tests/test_resume_unblocking.py`,
  `tests/test_workers_build.py` (`-m "not integration"`): **160/160 passed**.
- `tests/test_cli.py` (whole file, no `-k`, `-m "not integration"`): **205/205 passed** — this is
  what caught the `MonorepoUnavailableError` regression fixed in `aabead1`.
- `tests/test_integration_honesty_citations.py` + `tests/test_findings_kinds.py` (whole file, no
  `-k`): **74/74 passed**, re-run after every `docs/INTEGRATION_HONESTY.md`/`schema.sql`/
  `docs/SPEC.md` edit. Three pre-existing citations (`EdgeRow`, `record_attempt`,
  `cli._committed_contracts`) drifted from unrelated line-number shifts my own insertions caused
  elsewhere in `cli.py`/`repository.py` — repointed to their correct current ranges (mechanical,
  not a substance change).
- **Not re-run this session:** the full `tests/` suite (~15 min per CLAUDE.md's own estimate) —
  scoped instead to every file plausibly reached by this diff (`cli.py`, `state/repository.py`,
  `models/enums.py`, `bazel/query.py`, `state/schema.sql`, `docs/*`), each run WHOLE, no `-k`,
  per CLAUDE.md §6's "derive from the body, run the covering set" discipline. Not derived via the
  instrumented whole-tree probe that discipline's own bullet describes as the rigorous method —
  a time/effort tradeoff, disclosed rather than silently assumed sufficient.

## Explicitly out of scope, not attempted (named per the brief)

- SPEC §3.5.1 step 2's "does the branch need a full Phase-2 re-run" case (ADR-0128 judgment call
  6) — `_rewrite_one_consumer_label` fails loud (raises `LabelRewriteError`, caught and reported
  per-consumer) on any commit unique to `migrate/<consumer>` it does not recognize as its own
  prior rewrite, rather than attempting the re-run.
- `_unit_deps`'s redirect logic, `settle_revalidation`, `supersede`/`plan_revalidation` — all
  reused unmodified, confirmed by not editing `orchestrator/stubs.py` at all in this task's diff.
- `fleet stubs resolve`'s manual-trigger path — not touched; it reaches `supersede()` the same way
  T1's automatic path does (unchanged call graph), so it reaches D107's new code with no
  additional plumbing needed, per the brief's own note.
- Enqueuing a NEW REVALIDATE round when `settle_revalidation` returns `None` (another round
  needed, stub stays SUPERSEDED) — the claimed task is marked DONE either way (this round's
  execution finished); no mechanism exists yet to mint a follow-up REVALIDATE task for a
  still-open round. Not named in the brief's own scope, but disclosed here since it is a genuine,
  if narrow, gap: a stub that fails one revalidation round and has budget for another currently
  has no automatic path to a second attempt. Flagged for the controller to size separately if
  `stubs.max_revalidation_rounds > 1` is meant to be reachable automatically (today it is only
  reachable via a fresh, externally-triggered T1-equivalent event).
- §37/§12.37's own DONE verdict — explicitly not claimed; see `docs/CRITERIA_PLAN.md`'s dated
  update.

## Agent Recommendations (not SPEC-mandated, disclosed per CLAUDE.md Guardrail 1)

- `write_module_bazel=False` for D107's re-render (an internal label swap cannot change an
  external coordinate, so MODULE.bazel is provably unaffected; avoids needing fleet-wide
  MODULE.bazel inputs for a single-consumer, off-cycle re-render).
- The REVALIDATE claiming step is skipped under `--dry-run` (unlike `stub_reconcile`) because it
  performs real git/Bazel I/O, breaking the "steps 1-7 make no network call, free as a health
  check" property `stub_reconcile`'s own insertion explicitly preserves.
- `_rewrite_one_consumer_label`'s judgment-call-6 precondition check (see above) — a mechanical
  correction to the ADR's literal pseudocode, not a re-litigation of its intent.
- The ancestor-check's failure mode (a foreign commit landing between rounds) is reported as a
  per-consumer `LabelRewriteError`, never raised past the batch loop — consistent with how every
  other per-consumer failure in this task is handled.
