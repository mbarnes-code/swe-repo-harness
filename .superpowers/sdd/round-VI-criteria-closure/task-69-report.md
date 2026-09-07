# Task 69 report — §37 stub-creation, Leg 3: wire it live end to end

**Status: DONE_WITH_CONCERNS** (see "Concerns" and "What remains" below — the mechanical wiring is
real and proven; two genuine, pre-existing gaps were discovered while proving it, neither
introduced nor fixed by this task)

**Branch:** `agent/roundvi-task69` (from `main` @ `c01fe46`, not merged)

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
touches VERIFY's payload construction). No caller ever read the `stubs` table to fill them, so
`VerificationReport.equivalence` could never resolve to `STUB_LIMITED` even with a fully correct,
live stub redirect — confirmed empirically: before this fix, my end-to-end fixture's own
`VerificationReport` read `"equivalence": "FULL"`, `"verified_against_stubs": []` despite a real,
live `ACTIVE` stub redirect being in effect. Added:
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
    instruction not to re-prove reconciliation, the REAL `StubRecord` this run created (read back
    from step 6's row) is fed to `orchestrator.stubs.supersede` (D80, completely unmodified) —
    exactly the shape `tests/test_stubs.py::test_t1_fires_on_succeeded_and_merged` feeds its own
    hand-built fixture. `supersede(record, ProviderFacts("acme-lib-py", RepoStatus.SUCCEEDED,
    PrState.MERGED))` returns exactly one `StubTransition.T1` decision, `ACTIVE -> SUPERSEDED`,
    `consumer_status == DEGRADED` — proving the CREATION side genuinely lands in the shape D80's
    own tests already start from.

**§12.37 clauses this fixture proves:** clause 1 in full (C reaches DEGRADED; one `stubs` row
`state=ACTIVE`, `stub_fidelity=PUBLISHED_ARTIFACT`; a `VerificationReport` with
`equivalence=STUB_LIMITED` naming P's coordinate) — for the first time ever, through the real
`--stub-blocked` CLI path. The first half of clause 2 (the CREATION side reaches a state
`supersede()` accepts and correctly transitions) — proven directly against the real created
`StubRecord`.

**§12.37 clauses this fixture does NOT prove, and why (both pre-existing, both disclosed, neither
mine to fix):**
- **"Re-running P to SUCCEEDED with its PR MERGED"** cannot be driven through any real, built CLI
  mechanism today. `models/enums.py`'s `OPERATOR_REOPEN` map (`{REQUIRES_HUMAN_INTERVENTION:
  {PENDING}}`) documents `fleet retry` as "the one documented exception" for reopening an RHI
  repo — but `fleet retry` does not exist (`grep -n "def retry\b" src/fleet/cli.py` returns
  nothing), confirmed also by `docs/DECISIONS.md:7158`'s own aside ("`fleet retry`'s documented
  escape is itself still unwired"). `--reset-attempts` on `fleet resume` is separately, explicitly
  refused (`_refuse_unbuilt_resume_flags`: "name machinery with no implementation in `src/`").
  `RepoStatus.REQUIRES_HUMAN_INTERVENTION` maps to the empty set in `ALLOWED_TRANSITIONS` — it is
  mechanically terminal by construction, with `OPERATOR_REOPEN` the only door and no CLI key to
  it. This is why `supersede()` is fed a **constructed** `ProviderFacts`, not a live-driven one.
- **"produces zero new phase-2 commits... an `already_applied` event... C becomes SUCCEEDED,
  equivalence becomes FULL."** Blocked by **D104** (`docs/INTEGRATION_HONESTY.md`, OPEN,
  pre-existing, found by research-3 before this bundle): `TaskKind.REVALIDATE` has zero
  execution/dispatch path anywhere in `src/`, and `settle_revalidation` (T2/T3) has zero
  production callers — `tests/test_stubs.py` is its only caller anywhere. D104's own text names
  this exact clause of §12.37 as unreachable independent of the stub-creation gap this bundle
  closed. Not part of task-67/68/69's scope (D104 predates this whole bundle and is explicitly out
  of scope per the brief's own "Transitive stub stacking... still deferred" framing — this is the
  same "genuinely new production logic, do not attempt as a one-shot" category `docs/
  CRITERIA_PLAN.md` §37 already used for the stub-creation worker itself).

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

No D-number self-allocated for this finding, per CLAUDE.md's Central Number Allocation rule —
flagging it here for the controller to allocate one if it judges the class worth tracking
separately from D104.

## Test proof (Rule 12)

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
  **Verified pre-existing, not a regression**: stashed `src/fleet/cli.py` and every test-file
  change, re-ran `test_build_against_a_real_bazel` alone against unmodified `main` content — it
  fails **identically** (same two repos, same exit code 7, same shape), confirming an
  environmental issue in this sandbox (real Bazel + a JS toolchain reaching an external registry)
  unrelated to this task. Restored all changes immediately after. None of the 10 failures touch a
  file this task modified or a code path this task's wiring reaches (`--stub-blocked` is not
  passed by any of the 10 failing tests). All tests in `test_stubs.py`, `test_pr_e2e.py`,
  `test_resume_continue.py`, and `test_resume_unblocking.py` passed, including every test this
  task added or modified in those files.
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
- `src/fleet/cli.py` — the six wiring changes above.
- `tests/test_pr_e2e.py` — new end-to-end test + fixtures + imports.
- `tests/test_build_e2e.py` — refusal test replaced.
- `tests/test_transform_e2e.py` — refusal test updated + replacement added.
- `tests/test_resume_unblocking.py` — refusal test replaced.
- `tests/test_resume_continue.py` — `_drive` helper threads `stub_blocked`.
- `tests/test_config_keys_are_read.py` — stale citations to the (now-removed) refusal text
  corrected, dated, per this file's own citation-currency convention. No behavioral change; the
  `KNOWN_INERT` verdict for `fleet.yaml:transform.stub_blocked` is unchanged (this leg never added
  a `config.transform.stub_blocked` field — only the CLI flag/parameter of the same bare name).

## Is §12.37 (and §12.14/§12.39) now closable?

**Not yet, and this fixture is the first evidence to say precisely why not**, per the brief's own
instruction not to self-certify. What is now true, proven for real rather than asserted: the
CREATION half (trigger detection → `StubRecord` → `stubs` INSERT → `RUNNING → DEGRADED`, all three
prior legs) and the render half (task-68) are wired together and reachable through the real
`--stub-blocked` CLI surface end to end through VERIFY, producing a real `STUB_LIMITED`
`VerificationReport` for the first time. What remains, both pre-existing and both disclosed above
rather than newly created by this task: (1) `fleet retry`/`OPERATOR_REOPEN` has no CLI surface, so
"a repo re-runs to SUCCEEDED" is not drivable for real; (2) D104 (REVALIDATE has no dispatch path)
blocks the reconciliation side from ever completing T2/RESOLVED in production. Both are
independent, both predate this bundle, and closing either is "genuinely new production logic, not
wiring" in exactly the sense `docs/CRITERIA_PLAN.md` §37 already used for the stub-creation worker
itself — recommend each get its own tracked item (D104 already has one; the `fleet retry` gap does
not, and I have not self-allocated one for it) rather than being folded into a future §37 leg
sized like this one.

§12.14 (an `EMPTY_FAILING` stub unblocks nothing, no repo becomes DEGRADED for it) is unaffected
by this leg either way — `stub_degrade_transform`'s own qualifying-fidelity check was ADR-0124's
work, untouched here, and this leg only adds NEW call sites of it, all subject to the same check.

§12.39 not independently investigated this round; out of this task's assigned scope.
