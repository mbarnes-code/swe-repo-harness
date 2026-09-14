# Worker report: §15.1 item 3, Wave 7.0 Batch 27 — `cli.py` hot-spot: `_build_impl` PASS 2 (disclosed zero-test gap), `_verify_impl` remainder

## Status: DONE

Branch: `agent/roundviii-mutation-batch27` (from `main` at `2daff33`)
Worktree: `/home/redmage/swe repo harness worktrees/wt-roundviii-mutation-batch27`
Commit: see bottom of this report (added after this file is written and committed).

`src/fleet/cli.py` is **unchanged** — every mutation below was applied, verified discriminating,
then restored byte-identical before moving to the next target (`git status` shows only two new
test files). No production code was touched.

## Scope (per brief, = research report's internal "Batch 24", renumbered)

`_verify_impl` (guard clause excluded, already proven), `_build_impl`'s PASS 2, `_run_build_wave`,
`_run_verify_wave`, `_gated_members`'s non-D126 branches, `_eligible_build_units`,
`_eligible_contract_units`.

## 1. `_build_impl` PASS 2 — **new test written, not just mutation-proof** (started at ZERO coverage)

`src/fleet/cli.py:12318-12390`. Confirmed via `research-wave7-scoping-report.md`'s own finding
(not re-derived): PASS 2 (fleet-wide `_wave_snapshot` + one `_plan_build` per ingested unit, gated
only by `if ingests:`) had no test at all — distinct from the three `_wave_is_breached` guard
clauses `tests/test_prepare_before_admit.py` proves for `_transform_impl`/`_verify_impl`/
`_build_impl`, and distinct from the DISCLOSED, out-of-scope missing-breach-guard defect that same
report's §3 names (D84's residue — I did not touch it; see "What I deliberately did not do" below).

**New file: `tests/test_build_pass2_snapshot.py`** (2 tests), confirming PASS 2's own positive
contract per its docstring (ADR-0055):

1. `test_build_impl_pass_2_plans_every_wave_zero_unit_from_one_shared_snapshot` — every wave-0
   unit is planned from the SAME fleet-wide snapshot (the fixture's 5-repo fleet puts
   `acme-lib-py`/`acme-lib-ts` in wave 0; asserted via `build_snapshot_ref`, which reads the
   persisted `attempts.integration_ref`, not the process-local `plans` dict).
   - **Mutation**: moved `_wave_snapshot` from once-before-the-loop to once-per-repo inside PASS
     2's `for repo_id, ingested in ingests.items():`.
   - `git diff --no-index --stat` against backup: non-empty (5 insertions/3 deletions).
   - Result: **1 failed / 1 passed** (this test reddened with the exact two-different-refs
     evidence in the assertion message; the sibling idempotency test stayed green — narrow,
     targeted, not a module-wide outage).
   - Restored from backup: `diff -q` identical; re-ran: **2/2 passed**.

2. `test_build_impl_pass_2_cuts_no_new_snapshot_when_nothing_is_ingested` — a second, fully-settled
   `fleet build` invocation (`waves == []`) cuts no new integration ref.
   - **Mutation**: removed the `if ingests:` guard (`if True:` instead), so `_wave_snapshot` always
     runs.
   - `git diff --no-index --stat`: non-empty (1 insertion/1 deletion).
   - Result: **1 failed / 1 passed** — the OTHER test this time (the shared-snapshot test stayed
     green, confirming the two mutations are genuinely independent discriminators for two
     different pieces of PASS 2's logic).
   - Restored from backup: identical; re-ran: **2/2 passed**.

Both mutations target real PASS-2-only logic (verified against `research-wave7-scoping-report.md`
§3's own citation of the exact code region before writing anything), and each is the unique
discriminator of one of the two new tests.

## 2. `_verify_impl` remainder — confirmed sound, no new test

Guard clause (`if breached: continue`) already proven, excluded per brief. The remaining
load-bearing logic is D125's pre-seed pass (lines ~12958-12972): it upserts a Phase.VERIFY row for
`members` **and never for `blocked`** — the same "no phase row at all for a withheld repo"
invariant `_gated_members`'s own docstring states.

- **Mutation**: changed `for repo_id in members:` to `for repo_id in (*members, *_blocked):` in the
  pre-seed loop.
- `git diff --stat`: non-empty (2 insertions/1 deletion).
- Target: `tests/test_build_e2e.py::test_phase_four_withholds_a_repo_whose_phase_three_did_not_succeed`.
- Result: **reddened** — `acme-app-ts` (Phase-3-failed) got a stray `PENDING` Phase-4 row instead
  of none (`verified["acme-app-ts"] == "PENDING"` where the test asserts absence).
- Restored; re-ran: **1/1 passed**.

D125/D126's own re-propagation logic for VERIFY is separately and thoroughly covered by
`tests/test_build_e2e.py::test_a_verify_provider_reaching_rhi_in_an_earlier_wave_blocks_its_later_wave_dependent`
and `test_a_verify_provider_rhi_in_an_earlier_invocation_blocks_a_dependent_in_a_later_invocation`
(both genuine old-fails/new-passes per their own docstrings) — read, not re-mutated, since the
brief's scope is the "remainder" and these are already bucket-(a)-quality.

**Verdict: sound. No gap, no new test.**

## 3. `_run_build_wave` / `_run_verify_wave` — investigated; no new test; residual noted

Both are composition roots ("Compose the run and drive ONE Phase N wave. The composition root, and
nothing else" — their own docstrings) with no conditional branching of their own beyond a few
`X or Y` seam-default picks (`CONTAINER_STATS_RUNNER or proc_run`, etc. — generic injection points
exercised identically to `BAZEL_RUNNER` across the whole build/verify suite, so not a fresh gap).

I traced the one parameter that looked plausibly load-bearing: `WaveScheduler(phase=Phase.BUILD, …)`
vs `Phase.VERIFY`. Mutating it to the wrong phase did **not** discriminate against
`test_repo_and_wave_narrow_dispatch_and_never_the_root_file_domain` (a 2-wave full-build smoke
test) — traced why, rather than shrugging at the pass: `PhaseRunner.phase` (which actually decides
what phase an attempt gets *persisted* under) comes from `worker.phase`
(`src/fleet/orchestrator/runner.py:388`), not from the scheduler's own `phase` field — so the
persisted `phases`/`attempts` rows are unaffected by this wiring. And `_ScopedWaveStore.wave_members`
filters EVERY wave's membership through the CURRENT wave's own `only` set, so an earlier wave's
`wave_state()` check (used by `open_wave`'s "is the previous wave CLOSED" gate) reads an empty
member list for any wave that is not this call's own — vacuously `CLOSED` regardless of `self.phase`.
The scheduler's `phase` field is therefore only ever read via `status_of`/`admit()` against the
**current** wave's own members, before any of them have a row at all (uniformly `PENDING` either
way on a fresh dispatch) — so the field's only observable effect is a narrow, same-wave,
repeated-invocation edge case (a repo already `BLOCKED` within its own wave from a prior partial
invocation of that exact wave would be mis-read as `PENDING` and wrongly re-admitted). No test in
the current suite constructs that specific scenario, and manufacturing one to chase this single
wiring parameter felt like exactly the "premature/contrived test for its own sake" Rule 2 and the
CLAUDE.md stop-rule caution against — this is disclosed as an **Agent Recommendation / residual**,
not fixed, not asserted as covered, and not a directive claim that it must be fixed.

**Verdict: reached (every e2e build/verify test exercises this composition); no fresh mutation
found for the one non-trivial wiring parameter within the existing suite's shape; residual noted
for the controller to route if it wants that scheduling edge case covered.**

## 4. `_gated_members`'s non-D126 branches — confirmed sound, no new test

D126 (DEGRADED-widening) branch excluded per brief, already proven
(`tests/test_pr_e2e.py::test_stub_blocked_creation_reaches_degraded_through_the_real_cli_and_feeds_t1_for_real`,
asserting the DEGRADED row is admitted into BUILD/VERIFY). The non-D126 branch is the baseline
"withhold anyone not SUCCEEDED/DEGRADED" gate.

- **Mutation**: `return (tuple(repo for repo in members if repo in ready), tuple(... if repo not in
  ready))` → `return (tuple(members), ())` (admit everyone unconditionally).
- `git diff --stat`: non-empty (2 insertions/2 deletions).
- Target: `tests/test_build_e2e.py::test_phase_four_withholds_a_repo_whose_phase_three_did_not_succeed`.
- Result: **reddened** — `acme-app-ts` (Phase-3-failed) got admitted into Phase 4 and actually ran,
  producing `exit 7 (RHI)` instead of the expected clean `exit 0` / no-Phase-4-row for it.
- Also ran `test_stub_blocked_creation_reaches_degraded_through_the_real_cli_and_feeds_t1_for_real`
  under the same mutation as a control: stayed green, confirming it (correctly) tests a different
  gate (`_eligible_build_units`'s domain exclusion of the RHI provider, not `_gated_members`'s
  wave-dispatch withholding) rather than accidentally overlapping.
- Restored; re-ran both: **2/2 passed**.

**Verdict: sound. No gap, no new test.**

## 5. `_eligible_build_units` — confirmed sound, no new test

Existing coverage found and read, not re-authored: DEGRADED widening
(`tests/test_pr_e2e.py:2084`), the whole-fleet/no-`--repo`-narrowing property
(`tests/test_build_e2e.py::test_repo_and_wave_narrow_dispatch_and_never_the_root_file_domain`), and
settled-repos-stay-in idempotency (`tests/test_build_e2e.py` line ~2703 area).

- **Mutation**: `AND p.status IN ('SUCCEEDED', 'DEGRADED')` → `AND p.status IN ('SUCCEEDED')` (drop
  the DEGRADED widening) in `_eligible_build_units`'s own SQL (line 12018 specifically — left the
  sibling query at line 11974, `_wave_repos`'s unrelated status filter, untouched).
- `git diff --stat`: non-empty (1 insertion/1 deletion).
- Target: `tests/test_pr_e2e.py::test_stub_blocked_creation_reaches_degraded_through_the_real_cli_and_feeds_t1_for_real`.
- Result: **reddened** — `acme-app-py` (DEGRADED at TRANSFORM) fell out of the BUILD domain
  entirely, `built.exit_code` became `0` instead of the expected `7`.
- Restored; re-ran: **1/1 passed**.

**Verdict: sound. No gap, no new test.**

## 6. `_eligible_contract_units` — **new test written** (partial gap: inclusion was tested, exclusion was not)

Existing coverage (`tests/test_build_e2e.py::test_a_hoisted_contracts_content_is_really_merged_with_the_trailer`
and its two neighbours) proves a `HOISTED` contract really ingests — but every one of those fixtures
has exactly one contract, always eligible, so none of them can distinguish "every wave-member
contract is eligible" from "only HOISTED/MIGRATED ones are." That exclusion case was genuinely
untested.

**New file: `tests/test_eligible_contract_units.py`** (1 test):
`test_a_failed_contract_is_never_re_ingested_by_build` — reuses `_make_hoist_ingest_workspace`
verbatim (Rule 8), hand-stamps the contract to `ContractStatus.FAILED` by direct SQL (same
isolation technique `test_a_degraded_repo_with_no_rhi_repo_exits_7` uses), then asserts a
subsequent `fleet build` neither re-ingests it (no `Hoisted-Contract:` merge, no
`ContractIngestFailed` finding) nor changes its status.

- **Mutation**: widened the status filter to `(ContractStatus.HOISTED, ContractStatus.MIGRATED,
  ContractStatus.FAILED)`.
- `git diff --stat`: non-empty (3 insertions/1 deletion).
- Result: **reddened** — a second `Hoisted-Contract:` merge commit appeared on `integration` for
  the same contract, which the new test explicitly refuses.
- Restored; re-ran: **1/1 passed**.

## What I deliberately did not do

- Did **not** fix PASS 2's disclosed, out-of-scope missing-breach-guard defect (D84's residue,
  `src/fleet/cli.py`'s own "Filed OPEN; D84 is PARTLY ADDRESSED, not fixed" comment) — out of this
  batch's scope per the brief, and an unscoped fix to a disclosed-but-reachable defect is exactly
  what CLAUDE.md's Rule 3 and the "surface conflicts, don't average them" discipline warn against
  doing without a dispatched decision.
- Did **not** manufacture a test for `_run_build_wave`/`_run_verify_wave`'s narrow, same-wave
  repeated-invocation `phase=` wiring edge case (see §3) — traced and disclosed instead.

## Verification: which test file(s) I ran, and why sufficient

Final combined run (after every mutation above was restored and `git status` showed `src/`
untouched):

```
tests/test_build_pass2_snapshot.py            (new — PASS 2, both tests)
tests/test_eligible_contract_units.py         (new — the one test)
tests/test_prepare_before_admit.py            (whole file — sibling D84-guard coverage my new
                                                PASS-2 file's docstring references and reuses
                                                helpers from; confirms no interference)
tests/test_build_e2e.py::test_phase_four_withholds_a_repo_whose_phase_three_did_not_succeed
                                                (mutation-proof target for _gated_members AND
                                                 _verify_impl's pre-seed pass)
tests/test_pr_e2e.py::test_stub_blocked_creation_reaches_degraded_through_the_real_cli_and_feeds_t1_for_real
                                                (mutation-proof target for _eligible_build_units)
```

Result: **8 passed in 33.49s**.

**Why this set and not the whole `tests/` directory (or all of `test_build_e2e.py`/`test_pr_e2e.py`):**
no production code changed (`src/fleet/cli.py` is byte-identical to `main`), so this is not a
regression sweep over unrelated code — it is confirming (a) the two new test files are correct and
pass standalone and alongside their closest sibling/helper-sharing file, and (b) every existing
test I used as a mutation-proof target is still green after every mutation was reverted. Running
the two 6000+/2000+-line host files (`test_build_e2e.py`, `test_pr_e2e.py`) in full would cost
several minutes for zero additional signal about *this* change, since nothing in them was touched;
running only the specific node IDs I mutated against is the covering set for what I actually did.
**Excluded, deliberately:** the rest of `test_build_e2e.py`/`test_pr_e2e.py`/the whole `tests/`
tree — no code in those files changed, and CLAUDE.md's own "state what you excluded" rule applies
here in the direction of not padding a no-op regression sweep onto a test-only diff.

## Lint / type checks

- `.venv/bin/python -m ruff check tests/test_build_pass2_snapshot.py tests/test_eligible_contract_units.py`:
  initially 3 import-order/unused-noqa findings, fixed with `--fix`; re-checked: **All checks
  passed!**
- `.venv/bin/python -m mypy` (no path args — full `mypy_path`/`packages` scope per CLAUDE.md §6;
  the project's mypy config scopes to `packages = ["fleet"]`, i.e. `src/`, so this is unaffected by
  a tests-only change and reports the same baseline): **Success: no issues found in 132 source
  files**.

## Files touched

- `tests/test_build_pass2_snapshot.py` — new (2 tests, `_build_impl` PASS 2)
- `tests/test_eligible_contract_units.py` — new (1 test, `_eligible_contract_units`)
- `src/fleet/cli.py` — **unchanged** (every mutation applied during this session was reverted;
  `git status` confirms no diff)

## Per-function summary

| Function | Result |
|---|---|
| `_build_impl` PASS 2 | **New tests written** (zero coverage → 2 tests), both mutation-proven with two independent discriminators, restored clean. |
| `_verify_impl` remainder | Confirmed sound via fresh mutation against existing test; no new test. |
| `_run_build_wave` / `_run_verify_wave` | Investigated; composition roots with effectively no independently-testable branching reachable through the current suite's shape; one narrow residual disclosed (not fixed, not asserted covered). |
| `_gated_members` non-D126 branches | Confirmed sound via fresh mutation against existing test; no new test. |
| `_eligible_build_units` | Confirmed sound via fresh mutation against existing test; no new test. |
| `_eligible_contract_units` | **New test written** (inclusion was tested; exclusion was not), mutation-proven, restored clean. |

Do not merge, do not push.
