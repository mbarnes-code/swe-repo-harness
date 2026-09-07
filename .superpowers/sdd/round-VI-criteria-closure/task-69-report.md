# Task 69 report — §37 stub-creation, Leg 3: wire it live end to end

**Status: DONE** (fix round complete — see "Fix round" section immediately below for the
task-scoped review's findings and this round's response to each; the original landing's own
findings, still accurate, are the "Concerns"/closability sections further down)

**Branch:** `agent/roundvi-task69`. Original landing: `dbb6eb7` (from `main` @ `c01fe46`). Fix
round: see `git log agent/roundvi-task69` for the fix-round commit (this file is committed in the
same commit as the fix-round code/doc changes it describes).

## Fix round (task-scoped review response)

The original landing (`dbb6eb7`) was task-scoped reviewed given the stakes; verdict "Needs
fixes," Critical/Important/Minor findings. Responses:

- **C1 (Critical — regression, misattributed in my original report).** My unconditional
  `stub_degrade_transform(phase=Phase.BUILD)` correction silently regressed 3 of task-68's own
  acceptance tests in `tests/test_build_e2e.py`
  (`test_an_active_stub_redirects_the_consumers_generated_dependency_to_the_stubs_label`,
  `test_an_active_published_artifact_stubs_workspace_dep_reaches_module_bazel`,
  `test_active_stubs_package_files_are_materialized_into_every_dispatchs_worktree`) — each hand-
  inserts an `ACTIVE`/`PUBLISHED_ARTIFACT` stub row and asserted the OLD, now-superseded end
  state (`phases(BUILD).status == 'SUCCEEDED'`/`exit_code == SUCCESS`). My original report's "none
  of the 10 failures touch a file this task modified" was wrong on these 3 — they are `test_build_
  e2e.py`, a file I modified, and the reviewer correctly found my own already-written §5 disclosed
  WHY (the degrade is unconditional) while I failed to connect that to these 3 tests. **Fixed**:
  updated all 3 tests' assertions to the new, correct end state
  (`DEGRADED`/`ExitCode.REQUIRES_HUMAN_INTERVENTION`), with a dated "Corrected 2026-09-07"
  docstring paragraph in each explaining the behavior change, and old-fails/new-passes verified
  for all 3 (old assertions against current code: 3 failed; new assertions: 3 passed). **The real,
  production-visible consequence, confirmed intended**: `fleet build`/`fleet verify` over any
  fleet carrying an `ACTIVE`/`PUBLISHED_ARTIFACT` stub row now exits 7 (not 0) for that consumer,
  unconditionally, with no flag required — required by `models.state.RepoState._stub_invariants`
  (a live stub with a `SUCCEEDED` consumer status would violate an already-enforced Pydantic
  invariant), confirmed correct on the merits independently by the reviewer.
- **C2 (Critical — stale docs).** `docs/SPEC.md` §3.5 item 1 and `docs/CRITERIA_PLAN.md`'s §14
  entry both still described the pre-task-69 refused state as current, in paragraphs explicitly
  written to be updated once this leg landed. **Fixed**: both carry a dated update now (see the
  diffs), per Guardrail 7 ("fix the code and its doc listing in the same change").
- **I3 (Important — false claim in my own report and test comment).** I wrote the `StubRecord`
  fed to `supersede()` was "read back from step 6's row" — false; it is constructed from module
  constants. **Fixed**: corrected the claim in this report (see the "transition into
  reconciliation" section below) and in the test's own comment, and added the two previously-
  unasserted fields (`max_revalidation_rounds`, `revalidation_round`) to the query/assertion
  against the real row, so all seven constructed fields are now separately verified equal to it.
- **I4 (Important — 4 stale docstrings).** Fixed all four: `state/repository.py::
  stub_degrade_transform`'s docstring now discloses it is called for BUILD/VERIFY too (name kept —
  see the docstring's own reasoning for why a rename was judged not worth the blast radius);
  `models/enums.py`'s `STUB_DEGRADE` comment no longer says "a TRANSFORM phase" exclusively;
  `orchestrator/stubs.py`'s stale "never invoked... while `--stub-blocked` stays refused" comment
  corrected with a dated note; the 4th (`tests/test_build_e2e.py:3195-3197`'s "still REFUSES"
  docstring) was fixed as part of C1's own update to that same test.
- **M5 (Minor — overbroad claim).** Narrowed "`VerificationReport.equivalence` could never
  resolve to `STUB_LIMITED`" to the accurate, narrower claim: the **persisted Phase-4** report
  (the `findings` row VERIFY itself writes, what §12.37's criterion actually names) could never
  be `STUB_LIMITED` — `cli._report_with_stubs` already re-derives it fresh from live `stubs` rows
  at `fleet pr` time, independent of this gap. Fixed in both the `cli.py` docstring and this
  report.
- **M6 (Minor — disclose, no fix).** `fleet quarantine` also declares and silently discards a
  `--stub-blocked` flag — pre-existing, out of this task's scope, disclosed in its own section
  below.
- **M7 (Minor — incomplete blocker list).** My report named D104 as the sole blocker for §12.37's
  "P re-runs to SUCCEEDED" clause. Updated throughout this report to name all five: D104, D107,
  D108, D123, D124 (D123/D124 allocated by the controller's own task-69 review, `cfe82bb`, for
  exactly the two gaps my original report disclosed without a number).

Verification for this fix round: see "Fix-round test proof" at the end of this report.

## Pre-flight

Confirmed both prerequisites merged to `main` before starting:
```
c01fe46 merge agent/roundvi-task68 (round VI task 68): §37/§14/§39 Leg 2 ...
fe46f0c merge agent/roundvi-task67 (round VI task 67): §12.37/§12.14/§12.39 Leg 1 ...
```
Re-verified every cited line number against `HEAD` rather than the brief — all had drifted
(task-68's fix round alone added ~300 lines). `_eligible_build_units` is at `cli.py:10905-10930`
today, not `9338-9368`. Confirmed via `grep` that task-68's stub-package materialization
(`_stub_package_files`/`_union_support_files`) is fully landed, as the dispatch note said.

## What was built

### 1. `_eligible_build_units` widening (`src/fleet/cli.py:~10905`)
Widened `p.status = 'SUCCEEDED'` to `p.status IN ('SUCCEEDED', 'DEGRADED')`. Docstring updated.
No ADR (confirmed against `docs/CRITERIA_PLAN.md` §37's own text: "does not need its own ADR (a
mechanical domain-widening, not a guarded-invariant change)").

### 2. `_gated_members` ALSO widened (`src/fleet/cli.py:~10869`) — swept beyond the brief's literal
one-site citation
The brief named only `_eligible_build_units`. A sweep for the same predicate shape
(`status = 'SUCCEEDED'` gating a phase-to-phase admission) found `_gated_members` — the function
that actually decides whether a wave's members get dispatched into BUILD (`predecessor=Phase.
TRANSFORM`) or VERIFY (`predecessor=Phase.BUILD`) — carries an **identical**, separate hard-coded
`'SUCCEEDED'`-only gate. Without widening it too, a `DEGRADED` TRANSFORM row would be *included*
in `_eligible_build_units`'s domain (root files, ingest) but *withheld* from actual wave dispatch
by `_gated_members` — included in the count, excluded from the work: the exact "silently strand"
outcome the brief's own point 1 warns about, just at a different call site than the one it named.
Widened identically, one shared SQL predicate change fixes both the BUILD-admission and the
VERIFY-admission uses of this one function. **Mutation-tested**: reverting either widening alone
(one at a time) makes the end-to-end test below fail — `acme-app-py` is reported `withheld` from
Phase 3 with `_gated_members` reverted, and excluded from `_eligible_build_units`'s domain (BUILD
exits 0 instead of the expected 7) with that one reverted. Both restored; test passes again with
both in place.

### 3. Three `--stub-blocked` refusal sites removed
- `_validate_transform_flags` (`cli.py:~4790`): raise removed.
- `_validate_build_flags` (`cli.py:~6975`): raise removed. This flag is now accepted and does
  **nothing distinguishable** — see docstring: task-68's BUILD render and this leg's
  `stub_degrade_transform(phase=BUILD)` are both unconditional/data-driven off the `stubs` table,
  never gated on this flag.
- `_validate_resume_flags` (`cli.py:~15112`): raise removed, per ADR-0113 condition 2's own
  instruction ("whichever task lands second must remove the refusal").

### 4. TRANSFORM-phase stub creation wired live (`_transform_impl`, `cli.py:~6328`)
New required `stub_blocked: bool` parameter. After each wave completes, if set: runs task-67's
`_detect_transform_stub_triggers` + `_create_stub_records` for that wave's dispatched members,
then `SqliteStateRepository.stub_degrade_transform(phase=Phase.TRANSFORM)` for each — a no-op
unless the repo now carries a qualifying `ACTIVE`/`PUBLISHED_ARTIFACT` stub row. Threaded from
`transform()`'s own flag and from `_continue_impl`'s `Phase.TRANSFORM` delegate (see #6).

### 5. BUILD/VERIFY `stub_degrade_transform` correction — **beyond the brief's literal 3-item
scope, but load-bearing for §12.37's own criterion text**
Discovered necessary while building the end-to-end fixture: `models.state.RepoState.
_stub_invariants` is a real, already-enforced Pydantic invariant — `stubbed_deps` must be
non-empty **iff** `status is DEGRADED` — and `state.projection._fold_repos` folds the **highest
phase reached** to the top-level projected status. If BUILD/VERIFY's own phase rows stayed plain
`SUCCEEDED` after building/verifying against an ACTIVE stub, the projection would silently report
a repo with an unresolved stub as plain `SUCCEEDED` once BUILD or VERIFY became its highest
phase — exactly what §3.5.1 exists to prevent, and directly contradicts §12.37's own criterion
text ("`C` is `DEGRADED`"). Added the identical `stub_degrade_transform(phase=Phase.BUILD)` /
`(phase=Phase.VERIFY)` call, unconditional (data-driven, not gated on the flag — same shape as
task-68's own BUILD render), right after each wave's dispatch in `_build_impl`/`_verify_impl`.
Proven for real by the end-to-end test: `acme-app-py`'s BUILD and VERIFY phase rows both read
`DEGRADED` after this leg, not `SUCCEEDED`.

### 6. VERIFY-phase `verified_against_stubs`/`stub_fidelity` wiring — **a second, independent gap
found beyond the brief's scope, also load-bearing for §12.37's own criterion text**
`workers/rdepverify.py`'s `RdepverifyInput.verified_against_stubs`/`.stub_fidelity` fields have
existed, **unpopulated**, since before this bundle (not task-67's or task-68's code — neither
touches VERIFY's payload construction). No caller ever read the `stubs` table to fill them.
**Narrowed in the fix round (M5): the original claim here ("equivalence could never resolve to
STUB_LIMITED") was overbroad** — `cli._report_with_stubs` already re-derives `equivalence` fresh
from the live `stubs` table at `fleet pr` time, independent of this gap, so a PR body could
already show `STUB_LIMITED` before this fix. What this gap actually blocked: the **persisted
Phase-4 `VerificationReport`** — the `findings` row VERIFY itself writes, the exact artifact
§12.37's own criterion text names — could never carry `STUB_LIMITED`, even with a live,
correctly-rendered stub redirect, because nothing populated the fields it derives from —
confirmed empirically: before this fix, my end-to-end fixture's own PERSISTED report read
`"equivalence": "FULL"`, `"verified_against_stubs": []` despite a real, live `ACTIVE` stub
redirect being in effect. Added:
- `_active_stubs_by_consumer` (`cli.py:~8989`, new): `repo_id -> {stub_coord_key: fidelity}` for
  every `ACTIVE` stubs row — the consumer-keyed shape `_active_stub_facts` (coord-key-keyed, for
  BUILD's render) cannot supply.
- `VerifyInput` gained `verified_against_stubs`/`stub_fidelity` fields (it had none before —
  `RdepverifyInput` had the fields, `VerifyInput` upstream of it did not).
- `_verify_payloads` gained an optional `stub_facts` parameter (default `None`/`{}`, so every
  pre-existing caller is unaffected byte-for-byte) that populates the new fields per repo.
- `_run_verify_wave` reads `_active_stubs_by_consumer` once per wave (same pattern as `_run_build_
  wave`'s own `stub_workspace_deps`/`stub_package_files` reads immediately above it) and passes it
  through.
- `VerifyPipelineWorker._rdeps()` threads the two fields straight into `RdepverifyInput`, never
  recomputing them.

Proven for real: the end-to-end fixture's `VerificationReport` now reads `"equivalence":
"STUB_LIMITED"`, `"verified_against_stubs": ["pypi::acme-lib-py"]`, `"stub_fidelity":
{"pypi::acme-lib-py": "PUBLISHED_ARTIFACT"}`.

**Why #5 and #6 were done rather than reported-only:** both are small, mechanical, data-driven
threading of an already-existing field/table into an already-existing caller — the same character
as `_eligible_build_units`'s own widening, which the brief itself scoped as "no ADR needed." Doing
them was the only way to make §12.37's own literal criterion text ("C is DEGRADED... a
VerificationReport whose equivalence == STUB_LIMITED") even partially reachable; leaving them as
gaps-only would have made the whole leg's stated purpose ("the first point this criterion becomes
even partially testable") false. Neither touches task-67's or task-68's own algorithms (trigger
detection, `StubRecord` construction, `workspace_deps()`, `EMPTY_FAILING` rendering) — disclosed
here in full per the brief's "if you find a defect... report it, don't silently patch" instruction,
even though these are not defects IN task-67/68's code specifically.

## The end-to-end fixture — §12.37's own literal scenario

`tests/test_pr_e2e.py::test_stub_blocked_creation_reaches_degraded_through_the_real_cli_and_feeds_t1_for_real`
(new, ~130 lines with docstrings).

**Fixture identity:** `acme-lib-py` (provider) / `acme-app-py` (consumer) — the same pair
`tests/test_build_e2e.py`'s "§37 Blocker C" fixture uses (`_STUB_PROVIDER`/`_STUB_CONSUMER`),
reused as the brief instructed, though the actual construction (see below) turned out to need its
own topology-driving code rather than a direct reuse of `_insert_stub_row`.

**What is real, step by step:**
1. `write_rules(fleet, _PROVIDER_FAILS_RULE)` — a RULE_MISS rule targeting `acme-lib-py`'s own
   module (the provider-side mirror of `test_transform_e2e.py`'s existing `PY_MISSING_RULE`,
   which targets the consumer).
2. `scanned(fleet)` — real scan + sequence; real `edges`/`coordinates` rows, including
   `acme-lib-py`'s real published `pyproject.toml` version (`2.0.1`).
3. `transform(fleet, "--wave", "0")` — **real** dispatch. `acme-lib-py` genuinely exhausts 3 real
   `RULE_MISS` attempts and reaches `REQUIRES_HUMAN_INTERVENTION` for real (real `attempts` rows,
   real finding).
4. `acme-app-py`'s TRANSFORM row is hand-seeded `BLOCKED` with `blocked_by=["acme-lib-py"]` — see
   "Concerns" below for exactly why, and the measurement proving a real dispatch cannot reach this
   state here.
5. **ONE** `fleet resume --stub-blocked` call. Real step 6 (`_unblock_dependents`) frees
   `acme-app-py` (`unblocked_dependents.unblocked == [{"repo_id": "acme-app-py", "removed":
   ["acme-lib-py"], "remaining": [], "floor": "TRANSFORM"}]`, asserted verbatim). Real step 8
   re-enters `_transform_impl(stub_blocked=True)` in the SAME invocation: `acme-app-py` transforms
   for real, task-67's trigger detection fires for real against the real edge/coordinate rows,
   creates a real `stubs` row, and this leg's `stub_degrade_transform` fires for real.
6. Asserted: `acme-app-py`'s TRANSFORM row is `DEGRADED`; the `stubs` row is exactly
   `('ACTIVE', 'PUBLISHED_ARTIFACT', '2.0.1', 'acme-app-py', 'acme-lib-py')`.
7. `fleet build --no-sandbox` (real `FakeBazel`/`FakeFilterRepo`, no further seeding): exit 7
   (a `DEGRADED` repo alone forces "needs human" per D93/§3.5.1 point 5 — `acme-lib-py` itself
   never reaches BUILD at all, confirmed absent from its phase-3 rows). `acme-app-py`'s BUILD row
   is `DEGRADED`.
8. `fleet verify` (real): exit 7, same reason. `acme-app-py`'s VERIFY row is `DEGRADED`.
9. The real, persisted `VerificationReport` for `acme-app-py`: `equivalence == "STUB_LIMITED"`,
   `verified_against_stubs == ["pypi::acme-lib-py"]`, `stub_fidelity == {"pypi::acme-lib-py":
   "PUBLISHED_ARTIFACT"}`.
10. **"The transition into the already-proven reconciliation path."** Per the brief's own
    instruction not to re-prove reconciliation, a `StubRecord` is fed to `orchestrator.stubs.
    supersede` (D80, completely unmodified) — exactly the shape `tests/test_stubs.py::
    test_t1_fires_on_succeeded_and_merged` feeds its own hand-built fixture.
    **Correction (fix round, I3): this `StubRecord` is CONSTRUCTED from module constants, not
    read back from the row via a query** — my original report claimed "read back from step 6's
    row," which is false; the accurate description is "constructed to match the row's asserted
    values." All seven fields it sets (`coord_key`, `provider_repo_id`, `consumer_repo_ids`,
    `fidelity`, `pinned_version`, `max_revalidation_rounds`, `rounds_spent`) are now separately
    asserted equal to the real row immediately above this call in the test (the fix round added
    the `max_revalidation_rounds`/`revalidation_round` columns to that query and assertion — they
    were previously asserted nowhere). `supersede(record, ProviderFacts("acme-lib-py",
    RepoStatus.SUCCEEDED, PrState.MERGED))` returns exactly one `StubTransition.T1` decision,
    `ACTIVE -> SUPERSEDED`, `consumer_status == DEGRADED` — proving the CREATION side genuinely
    lands in the shape D80's own tests already start from.

**§12.37 clauses this fixture proves:** clause 1 in full (C reaches DEGRADED; one `stubs` row
`state=ACTIVE`, `stub_fidelity=PUBLISHED_ARTIFACT`; a `VerificationReport` with
`equivalence=STUB_LIMITED` naming P's coordinate) — for the first time ever, through the real
`--stub-blocked` CLI path. The first half of clause 2 (a `StubRecord` matching every field of the
real row this run created is accepted and correctly transitioned by `supersede()`) — proven
against a constructed record whose fields are all separately asserted equal to the real row, not
against a literal read-back.

**§12.37 clauses this fixture does NOT prove, and why (all pre-existing, all disclosed, none
mine to fix — updated in the fix round (M7) to name every independent blocker, not only D104):**
- **"Re-running P to SUCCEEDED with its PR MERGED"** cannot be driven through any real, built CLI
  mechanism today. `models/enums.py`'s `OPERATOR_REOPEN` map (`{REQUIRES_HUMAN_INTERVENTION:
  {PENDING}}`) documents `fleet retry` as "the one documented exception" for reopening an RHI
  repo — but `fleet retry` does not exist (`grep -n "def retry\b" src/fleet/cli.py` returns
  nothing), confirmed also by `docs/DECISIONS.md:7158`'s own aside ("`fleet retry`'s documented
  escape is itself still unwired"). `--reset-attempts` on `fleet resume` is separately, explicitly
  refused (`_refuse_unbuilt_resume_flags`: "name machinery with no implementation in `src/`").
  `RepoStatus.REQUIRES_HUMAN_INTERVENTION` maps to the empty set in `ALLOWED_TRANSITIONS` — it is
  mechanically terminal by construction, with `OPERATOR_REOPEN` the only door and no CLI key to
  it. **This is now D124** (allocated by the controller's own task-69 review, `cfe82bb`: "no
  `fleet retry` CLI surface and no `ALLOWED_TRANSITIONS` edge out of `REQUIRES_HUMAN_
  INTERVENTION`" — shared with §12.14's identically-shaped clause). This is why `supersede()` is
  fed a **constructed** `ProviderFacts`, not a live-driven one.
- **"produces zero new phase-2 commits... an `already_applied` event... C becomes SUCCEEDED,
  equivalence becomes FULL."** Blocked by **three independent, pre-existing, already-OPEN
  entries in `docs/INTEGRATION_HONESTY.md`**, not D104 alone: **D104** — `TaskKind.REVALIDATE`
  has zero execution/dispatch path anywhere in `src/`, and `settle_revalidation` (T2/T3) has zero
  production callers (`tests/test_stubs.py` is its only caller anywhere); **D107** — nothing
  rewrites a consumer's `BUILD.bazel` dependency label from the stub target back to the real one
  once the stub resolves, so `verified_against_stubs` can never actually clear on a real tree
  even if D104 were fixed; **D108** — `StubDecision.consumer_status` has zero production readers,
  so even a fixed D104 producing a T2 `RESOLVED` decision would not by itself promote the
  consumer's `phases` row to `SUCCEEDED`. All three predate this bundle (D104 found by research-3,
  D107/D108 found by research-14, all before task-67 started) and are independent of the
  stub-creation gap task-67/68/69 closed — fixing all three is "genuinely new production logic, do
  not attempt as a one-shot," the same category `docs/CRITERIA_PLAN.md` §37 already used for the
  stub-creation worker itself.

## Concerns — a genuine, disclosed, pre-existing gap in `_transform_impl` (NOT introduced or fixed
by this task)

While constructing the fixture, discovered that `_transform_impl`'s cross-wave `blocked_by`
propagation **does not work today**, for ANY reason a provider might be abandoned, independent of
stubs. `_transform_impl`'s wave loop creates each wave's `phases` rows **lazily**, at the top of
that wave's own iteration (`for index in waves: ... upsert_phase(...) ... await _run_transform_
wave(...)`). `PhaseRunner._contain` → `propagate_blocked` → `SqliteSchedulerStore.
append_blocked_by` only `UPDATE`s an **existing** phase row ("every non-`SUCCEEDED` phase of a
repo"). So by the time a provider abandoned in wave N is detected (at the end of wave N's own
dispatch), a dependent in wave N+1 has no phase row yet for the UPDATE to touch — and once wave
N+1 opens moments later, the dependent gets a **fresh `PENDING` row with `blocked_by=[]`** and
transforms normally, unblocked, against a provider that will never publish.

**Measured directly, twice**, with a real `_PROVIDER_FAILS_RULE`-driven failure of `acme-lib-py`
(no stub involved at all — this is orthogonal to §37): once with both waves driven in one `fleet
transform` call, once across two separate `fleet transform --wave N` calls. `acme-app-py` reached
`SUCCEEDED` with `blocked_by == '[]'` both times, never `BLOCKED`.

**Why BUILD does not have this defect** (and why `tests/test_build_e2e.py`'s "Blocker C" fixture
can drive the analogous shape with zero seeding): `_build_impl` does an upfront, whole-domain
`upsert_phase` pass (`_eligible_build_units`'s own INGEST pass, "PASS 1... before the first
build") — every eligible repo's BUILD phase row exists **before any wave dispatches**, so a
same-invocation `append_blocked_by` always finds a row to touch. `_transform_impl` has no
equivalent upfront pass; it creates rows wave-by-wave, lazily.

**Not fixed here** — this is squarely `_transform_impl`'s own pre-existing wave-admission
architecture, unrelated to the three items this task was scoped to touch, and a real fix (an
upfront domain-wide `upsert_phase` pass for TRANSFORM, mirroring BUILD's) would be its own
task-sized change with its own review. **Worked around** in the fixture by hand-seeding
`acme-app-py`'s `BLOCKED` row directly (an `INSERT`, not an `UPDATE` — the row does not exist yet
at that point in the fixture either) — the same raw-SQL convention `tests/test_pr_e2e.py::
degrade`/`tests/test_build_e2e.py::_insert_stub_row` already use for states no current CLI
mechanism can reach. `acme-lib-py` itself is **not** hand-seeded — it reaches RHI through a
genuinely real dispatch, so `fleet resume`'s step 5 (`_demote_to_floors`) reads real evidence
(real `attempts` rows, a real `RULE_MISS` finding) and correctly leaves it at RHI rather than
re-deriving a fresh floor for it. (An earlier draft of this fixture hand-seeded `acme-lib-py`'s
RHI status too, with no backing evidence — step 5 correctly, from its own perspective, demoted it
back to a fresh floor and it simply succeeded on retry, silently invalidating that draft. Caught
by re-measuring rather than trusting the first "green" run — see `_seed_blocked`'s own docstring
in the test file for the full account.)

**This finding is now D123** (allocated by the controller's own task-69 review, `cfe82bb`:
"cross-wave `blocked_by` propagation gap (the wave loop's lazy per-wave `upsert_phase` means a
not-yet-dispatched dependent's `blocked_by` column never gets set)") — not self-allocated here,
per CLAUDE.md's Central Number Allocation rule; the number above is quoted from the controller's
own commit, not chosen by this report.

## A fourth `--stub-blocked` surface (M6, found by the fix-round reviewer, not fixed — disclosed
per the reviewer's own instruction)

`fleet quarantine` also declares a `--stub-blocked` flag and silently discards it: `src/fleet/
cli.py`'s `quarantine()` command takes `stub_blocked: Annotated[bool, typer.Option("--stub-
blocked")] = False` and its body reads only `_ = stub_blocked` — no validation, no refusal, no
effect of any kind. Pre-existing (not introduced by task-67/68/69), and absent from this task's
brief's 3-site list (`transform`/`build`/`resume`). Left as found: fixing it is out of this fix
round's scope (the reviewer flagged it as a found-but-out-of-scope item, not a required fix), and
the controller will decide whether it needs its own D-number.

## Original-landing test proof (Rule 12) — see "Fix-round test proof" at the end of this
report for the corrected, re-measured numbers

**Old-fails/new-passes, all three refusal removals, verified by `git stash` of `src/fleet/cli.py`
against each test:**
- `tests/test_transform_e2e.py::test_stub_blocked_no_op_when_nothing_is_abandoned_does_not_raise_usage_error`
  (new, replaces the `--stub-blocked` case removed from `test_transform_refuses_the_flags_it_cannot_honour`'s
  refused-flags tuple) — pre-task-69: `AssertionError`, `exit_code == USAGE`. Post: passes,
  `exit_code == SUCCESS`, same shape as a flagless `fleet transform` on this fixture.
- `tests/test_build_e2e.py::test_stub_blocked_on_build_is_a_no_op_when_no_stub_exists` (new,
  replaces `test_stub_blocked_is_refused_rather_than_silently_ignored`) — pre-task-69:
  `exit_code == USAGE` with the old refusal message. Post: passes, matches a flagless `fleet
  build`'s exit code exactly, and a second `--stub-blocked` build re-dispatches nothing (same
  `BUILD.bazel` bytes before/after).
- `tests/test_resume_unblocking.py::test_stub_blocked_frees_an_rhi_blocked_repo_at_step_6` (new,
  replaces `test_stub_blocked_is_refused_on_resume_rather_than_silently_ignored`) — pre-task-69:
  the CLI runner's own JSON-parse assertion fails (non-JSON `UsageError` output). Post: passes,
  `RETAINED_RHI`'s `blocked_by` genuinely empties via step 6.
- The full end-to-end test itself (see above) — pre-task-69: fails at the `resume --stub-blocked`
  call with the old `UsageError` message, `exit_code == USAGE` where `REQUIRES_HUMAN_INTERVENTION`
  is expected.

**`_eligible_build_units`/`_gated_members` widening — discriminating mutation, both sites:**
reverted each widening in turn (one at a time), re-ran the end-to-end test: both mutations make it
fail (`acme-app-py` withheld from Phase 3 with `_gated_members` reverted; excluded from the
`_eligible_build_units` domain entirely, changing BUILD's exit code from 7 to 0, with that one
reverted). Both restored; confirmed green again.

**mypy --strict src/fleet/**: `Success: no issues found in 129 source files`.

**ruff check** (changed files: `cli.py`, `test_pr_e2e.py`, `test_build_e2e.py`,
`test_transform_e2e.py`, `test_resume_unblocking.py`, `test_resume_continue.py`,
`test_config_keys_are_read.py`): `All checks passed!` (after fixing 3 `F811`s from re-used fixture
parameter names, standard `# noqa: F811` pattern already used elsewhere in these files).

**ruff format --check**: verified no NEW formatting drift — diffed `ruff format --check` output on
`main`'s copy of each changed file against my copy; the same pre-existing dirty spots appear at
the same relative locations in both (e.g. `cli.py`'s one pre-existing dirty function,
`_contract_detail`, is the only reported spot in both `main` and my tree). Did not run the full
`tests/test_lint_gate.py::test_ruff_format_check_dirty_count_matches_the_pinned_baseline` (a
pytest test; see below).

**Full required test files, whole, no `-k`:**
- `tests/test_build_e2e.py tests/test_stubs.py tests/test_pr_e2e.py tests/test_resume_continue.py
  tests/test_resume_unblocking.py` run together (1034 s, section 7's real-Bazel fixtures dominate
  the wall clock): **10 failed, 164 passed.** All 10 failures are in `test_build_e2e.py`'s
  real-Bazel section 7, every one naming `acme-app-ts`/`acme-lib-ts` (the TypeScript fixture
  repos) reaching `REQUIRES_HUMAN_INTERVENTION`, e.g. `test_build_against_a_real_bazel`.
  **Verified pre-existing, not a regression, for 7 of the 10** (`test_build_against_a_real_bazel`
  and its real-Bazel-JS siblings): stashed `src/fleet/cli.py` and every test-file change, re-ran
  `test_build_against_a_real_bazel` alone against unmodified `main` content — it fails
  **identically** (same two repos, same exit code 7, same shape), confirming an environmental
  issue in this sandbox (real Bazel + a JS toolchain reaching an external registry) unrelated to
  this task. **CORRECTED (fix round, C1): the other 3 of the 10 were WRONGLY included in that
  blanket "pre-existing, none touch a file this task modified" claim.** The reviewer measured
  precisely: base `c01fe46` is 11 failed/55 passed/11 skipped on `test_build_e2e.py` alone; this
  branch (pre-fix-round) was 14 failed/52 passed/11 skipped — the extra 3 were exactly task-68's
  own acceptance tests, regressed by this leg's own new, unconditional
  `stub_degrade_transform(phase=Phase.BUILD)` call (see the "Fix round" section at the top of this
  report, C1). My original sentence here — "None of the 10 failures touch a file this task
  modified... `--stub-blocked` is not passed by any of the 10 failing tests" — was therefore
  FALSE for those 3 (they do not need `--stub-blocked` passed; the correction that broke them is
  unconditional). Fixed in this fix round; see "Fix-round test proof" below for the re-measured,
  corrected numbers.
- `tests/test_cli.py tests/test_lint_gate.py` run together, separately (never two pytest sessions
  concurrently): **1 failed, 200 passed.** Every `test_cli.py` test passed. The one failure is
  `test_lint_gate.py::test_ruff_format_check_dirty_count_matches_the_pinned_baseline`: whole-repo
  `ruff format --check .` now reports **124** dirty files against a pinned baseline of **123**.
  **Verified NOT caused by this task**: ran the identical whole-repo `ruff format --check .`
  directly against the PRIMARY checkout (`/home/redmage/swe repo harness`, zero involvement from
  this branch) and it independently reports the same **124** — some other, unrelated commit on
  `main` already drifted this count before this task started; not one of the 124 listed dirty
  files is one this task touched (`src/fleet/cli.py` and every changed test file were each
  individually confirmed dirty-before/dirty-after at the SAME spot, never a new one — see the
  per-file `ruff format --check` comparisons above). This is the ADR-0116 baseline ratchet
  (`docs/SPEC.md` §12 item 2) and its pin needs updating by whoever's commit moved it, not by this
  task.

## Files touched
- `src/fleet/cli.py` — the six wiring changes above; plus (fix round, M5) a narrowed claim in
  `_active_stubs_by_consumer`'s docstring.
- `tests/test_pr_e2e.py` — new end-to-end test + fixtures + imports; plus (fix round, I3) two
  added assertion columns and a corrected in-test comment.
- `tests/test_build_e2e.py` — refusal test replaced; plus (fix round, C1/I4) 3 of task-68's own
  tests updated to the new, correct `DEGRADED`/exit-7 end state, each with a dated docstring
  correction.
- `tests/test_transform_e2e.py` — refusal test updated + replacement added.
- `tests/test_resume_unblocking.py` — refusal test replaced.
- `tests/test_resume_continue.py` — `_drive` helper threads `stub_blocked`.
- `tests/test_config_keys_are_read.py` — stale citations to the (now-removed) refusal text
  corrected, dated, per this file's own citation-currency convention. No behavioral change; the
  `KNOWN_INERT` verdict for `fleet.yaml:transform.stub_blocked` is unchanged (this leg never added
  a `config.transform.stub_blocked` field — only the CLI flag/parameter of the same bare name).
- `src/fleet/models/enums.py` — (fix round, I4) `STUB_DEGRADE`'s comment corrected to not claim
  TRANSFORM-only.
- `src/fleet/state/repository.py` — (fix round, I4) `stub_degrade_transform`'s docstring now
  discloses it fires for BUILD/VERIFY too, and explains why the name was kept.
- `src/fleet/orchestrator/stubs.py` — (fix round, I4) a stale "never invoked" comment corrected
  with a dated note.
- `docs/SPEC.md` — (fix round, C2) §3.5 item 1's stale "refusals still stand" paragraph replaced
  with a dated update naming what actually landed and what still doesn't work.
- `docs/CRITERIA_PLAN.md` — (fix round, C2) §14's stale "(c)/(d) actively refused" sentence
  corrected with a dated update; done bar reworded to name D104/D107/D108/D123/D124 explicitly.

## Is §12.37 (and §12.14/§12.39) now closable?

**Confirmed by the controller's own task-scoped review: not yet, for either §12.37, §12.14, or
§12.39** (`cfe82bb`, allocating D123/D124 and correcting a false "§14's done bar is identical to
§37's" claim in `docs/CRITERIA_PLAN.md`) — this fixture is the first evidence to say precisely
why not, per the brief's own instruction not to self-certify. What is now true, proven for real
rather than asserted: the CREATION half (trigger detection → `StubRecord` → `stubs` INSERT →
`RUNNING → DEGRADED`, all three prior legs) and the render half (task-68) are wired together and
reachable through the real `--stub-blocked` CLI surface end to end through VERIFY, producing a
real, persisted `STUB_LIMITED` `VerificationReport` for the first time. What remains, all
pre-existing and all disclosed above rather than newly created by this task, each independently
tracked with its own D-number: **D124** (no `fleet retry` CLI surface, no `ALLOWED_TRANSITIONS`
edge out of `REQUIRES_HUMAN_INTERVENTION` — "a repo re-runs to SUCCEEDED" is not drivable for
real), **D104** (REVALIDATE has no dispatch path), **D107** (nothing rewrites a consumer's
redirected `BUILD.bazel` label back to the real one once the stub resolves), **D108**
(`StubDecision.consumer_status` has zero production readers), and **D123** (the cross-wave
`blocked_by` propagation gap this leg's own fixture had to work around — orthogonal to stubs,
also blocks §12.14 per the controller's correction). All five are "genuinely new production
logic, not wiring" in exactly the sense `docs/CRITERIA_PLAN.md` §37 already used for the
stub-creation worker itself — each is its own tracked item; none should be folded into a future
§37/§14 leg sized like this one.

§12.14: the controller's review found "(a)/(b) fully covered" was ALSO false (D123 — a direct
dependent of an RHI repo does not become BLOCKED at the TRANSFORM phase) and corrected
`docs/CRITERIA_PLAN.md`'s claim that its done bar is "identical to §37's" (it needs D124 — shared
with §12.37 — plus an undesigned transitive stub-stacking mechanism, beyond the Leg 1-3 bundle).
`stub_degrade_transform`'s own qualifying-fidelity check (an `EMPTY_FAILING` stub unblocks
nothing, no repo becomes DEGRADED for it) was ADR-0124's work, untouched here — this leg only
adds NEW call sites of it, all subject to the same check.

§12.39 not independently investigated this round; out of this task's assigned scope.

## Fix-round test proof (fresh measurements, this commit)

**`tests/test_build_e2e.py`, whole file, no `-k`: 7 failed, 70 passed.** Every one of the 7
failures is in the real-Bazel section (7) naming `acme-app-ts`/`acme-lib-ts` reaching
`REQUIRES_HUMAN_INTERVENTION` — e.g. `test_build_against_a_real_bazel`,
`test_two_js_repos_with_different_npm_dependencies_both_build`. **Confirmed exact parity with
unmodified `main`, measured in THIS SAME sandbox rather than assumed from the reviewer's own
numbers**: ran the identical `pytest tests/test_build_e2e.py -q` directly against the PRIMARY
checkout (`/home/redmage/swe repo harness`, commit `cfe82bb`, zero task-69 involvement) — it also
reports **7 failed, 70 passed**, the same 7 test names, the same shape. (The reviewer's own
baseline measurement of "11 failed/55 passed/11 skipped" reflects a different environment — 0
tests are skipped in this sandbox on either `main` or this branch, most likely because this
sandbox's network/registry reachability to `npm` differs from theirs; the class of failure is the
same real-Bazel-JS-toolchain issue either way, and the controlled, same-sandbox comparison is what
actually proves no regression.) This confirms C1's fix genuinely restores task-68's 3 tests to
green without altering the underlying, intentional `DEGRADED`/exit-7 behavior change — the 3 tests
this round fixed are absent from both failure lists.

**`tests/test_cli.py`, whole file, no `-k`: 194 passed.**

**`tests/test_integration_honesty_citations.py`: found and fixed one genuine citation-drift
regression from this fix round's own edits, then 70 passed.** `stub_degrade_transform`'s
docstring expansion (I4) added lines to `src/fleet/state/repository.py` ABOVE `record_attempt`,
shifting its definition from `2483-2551` to `2495-2563` — `docs/INTEGRATION_HONESTY.md:7406`'s
anchored citation of `record_attempt` (`state/repository.py:2483-2551`) stopped resolving as a
result, caught by this exact test (`test_no_unpinned_anchored_citation_fails_to_resolve
[INTEGRATION_HONESTY]`) and by its sibling census check (`test_every_census_number_this_module_
states_is_the_number_it_derives`, which read 57 unresolved anchored citations against the
module's own stated 56). Repointed the citation to `2495-2563`; both tests pass clean afterward
(confirmed no other citation in the tree drifted from this round's edits — the full 70-test module
is green, not just the two that failed).

**`python -m mypy` (no path args, repo root)**: `Success: no issues found in 129 source files`.

**`ruff check .`**: `All checks passed!`

## Final commit

Fix round committed on `agent/roundvi-task69`. Not merged to `main`.
