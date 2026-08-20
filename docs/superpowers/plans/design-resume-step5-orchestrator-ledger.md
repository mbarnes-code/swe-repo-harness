> Round-C orchestrator (controller) SDD ledger for plan `docs/superpowers/plans/design-resume-step5.md`; promoted substantively unchanged from `.superpowers/sdd/design-resume-step5/progress.md` (git-ignored scratch), **snapshot taken 2026-08-20, while `main` was at `056b547`** — the scratch original kept changing outside git, so this is that file at that instant and not a claim it was ever tracked. Body below is byte-identical to the snapshot; **this header adds 2 lines, so a citation of scratch `progress.md:N` is line `N+2` here** (e.g. the RL1 entry cited as `:604` is `:606`).

# SDD ledger — plan: docs/superpowers/plans/design-resume-step5.md

Round C. Controller: main session. Base at round start: 6a5e534 (main).
Central ADR allocation (orchestrator-assigned, never taken by a worker):
  ADR-0078 -> backend-name narrowing (next-task 2, lane BK-ADR)
  ADR-0079 -> RESERVED: resume flag semantics for subtask 9 (ambiguity 4)
  ADR-0080 -> RESERVED: step-8 delegation decision (subtask 10)
Dependency order fixed by the design doc: {1,2}->6; 3->4->5; {2,5,6}->7->8->9->10.
Subtask 1 (RESUME_DEMOTE / demote()) landed on main pre-round (791b428..ebd1624).

## Wave 1 dispatched (5 agents, concurrent, file-disjoint on one checkout)
- W-A  subtask 2  phase_floor           -> NEW src/fleet/orchestrator/reentry.py, tests/test_reentry_floor.py
- W-B  subtask 3  step-2 orphan reap    -> src/fleet/cli.py, tests/test_cli.py
- W-C  next-task2 ADR-0078 + bind test  -> docs/DECISIONS.md (sole writer), llm test file
- R-1  research   Q1 evidence_holds / Q2 step-4 attempts-identity / Q3 live_names -> research-1.md (read-only)
- CR-1 review     subtask 1 range 791b428..ebd1624 -> review-1.md (read-only)
Controller ambiguity rulings issued at dispatch:
- W-A: if ADR-0077 does not settle DEGRADED, treat DEGRADED as evidence-bearing only when
  evidence[phase] is True, else demote through it. Record the choice.
- W-B: if live_names is not cleanly derivable from `phases`, do NOT invent a shadow lease table;
  implement the closest correct derivation and state the gap.

Task 2: complete (commit 011b16d, phase_floor + tests/test_reentry_floor.py; 156 passed;
  Rule-12 mutation proof done — ascending-walk mutation broke 22/36 incl. the two discriminating tests).
Task 2: brief errata corrected BY the worker (both confirmed by controller):
  - `PhaseRow` lives at src/fleet/state/repository.py:179, NOT src/fleet/models/.
  - `tests/test_enums.py` does not exist; enums/demote() tests are in tests/test_state_models.py.
  - DEGRADED was NOT open: ADR-0077 §5 (docs/DECISIONS.md:7024-7028) settles it — step 5 neither
    demotes DEGRADED nor searches past it. Controller fallback ruling never triggered; correct.
  ==> Propagate all three into every later brief. NOT-a-defect: Pyright flags
      _SETTLED_FOR_DEMOTION unused; it is used at reentry.py:73 (IDE false positive).

R-1 research: COMPLETE -> research-1.md. Load-bearing results, routed live to the two running lanes:
  Q1 (feeds subtask 5): all four evidence_holds rows implementable from durable state, BUT
     - design-doc "ambiguity 1" is STALE: demote()/RESUME_DEMOTE/PHASE_DEMOTED_KIND already exist.
     - PhaseRow/get_phase do NOT expose post_commit_sha/base_ref -> raw SQL needed.
     - row 3 clause 2 NOT implementable as written: buildverify checks a worktree file, not a ref;
       the ref it needs is attempts.integration_ref, not a `phases` column. Subtask 5 must restate it.
     - return False on absent row (safe: downward-only search) but PROPAGATE GitCommandError.
  Q2 (feeds subtask 4): complete_phase is the ONLY writer of phases.attempts in src/ -> byte-identity
     follows from not calling it. discard_task is pure git, no SQL. One StateWriter.submit ==
     one BEGIN IMMEDIATE. base_ref recreation exists ONLY in cli._prepare_repo, hardcoded to Phase 2
     and UNREACHABLE from resume -> subtask 4 must build it. Assert named columns, not SELECT *.
  Q3 (routed to lane W-B mid-flight): live_names = status='RUNNING' post-sweep +
     sandbox_name(run_id, repo_id, attempts + 1); the +1 is REQUIRED.
CONTROLLER RULINGS issued mid-flight to W-B (subtask 3):
  - ORDERING DEPARTURE AUTHORISED: place step 2 AFTER _reset_stale_running. As numbered in §11.5
    (reap before sweep) the reap is a total no-op on exactly the crashed runs it exists for.
    ADR-0081 ALLOCATED for this; W-B drafts to adr-0081-draft.md, does NOT touch DECISIONS.md.
    Controller lands the draft after W-C (sole DECISIONS.md writer) finishes.
  - SCOPE EXPANDED to src/fleet/sandbox/container.py: ContainerSandbox.reap spares by EQUALITY while
    buildverify names containers with a random token suffix -> a live build container gets
    force-removed. Latent only because .reap( has no caller; subtask 3 makes it the first caller,
    which makes it LIVE. Fix + bind with a prefix test.
  - NOT defects: WorktreeManager never instantiated; .reap( has no caller. Both expected.
ADR ledger update: 0078=backend narrowing (W-C), 0079=RESERVED subtask 9, 0080=RESERVED subtask 10,
  0081=step-2 reap ordering departure (W-B, draft only).

CR-1 review of subtask 1 (791b428..ebd1624): FINDINGS — 1 Critical, 1 Important, 4 Minor.
  C-1 CRITICAL: docs/SPEC.md:186-187 (Constraint 7) says the demotion write goes through
    transition(..., resume=True) and emits PhaseDemoted. BOTH FALSE (enums.py:127 / SPEC:6873 /
    SPEC:2094 say never call transition() directly; transition() emits nothing). d0b1150 rewrote two
    paragraphs; the e5b8b11 fix scoped itself to "§11.5 step 5" and never swept the sibling.
    -> The reviewer's failure scenario WAS the live subtask-6 lane. Warned it mid-flight: use demote(),
       plus a NEW obligation — prove the PhaseDemoted finding is emitted, Rule-12 style (a mutation
       dropping the finding while keeping the status write must fail a test).
  I-1: PROGRESS.md:5632-5634 and INTEGRATION_HONESTY.md:3921-3924 both record C-1's remedy as
    "landed". It was not. Stale closure records.
  Verified SOUND (do not re-litigate): §6 audit obligation discharged at enums.py:162 + SPEC:6874-6876;
    co_names whitelist is an equality, kills all six cut-2 defeats; 120 passed re-measured;
    gate confirmed unreached (re-exports only) = expected.
Task ADR-0078: complete (commit ddc16a6, docs/DECISIONS.md + tests/test_backend_registry_gate.py).
  KEY CORRECTION: the narrowing is in cli._load_settings (c36160e), NOT discover() as my brief
  guessed; discover() is unchanged. Rule-12 mutation run in a DETACHED WORKTREE at 6a5e534 to avoid
  the concurrent lanes — good practice, adopt for later lanes.
  Deferred minor: weak sibling assertion tests/test_llm_backend_anthropic.py:662 passes under both
  mutations; left in place, named in ADR-0078 §5.
  Owed at land: full suite (forbidden mid-round with lanes live).
DOCFIX lane dispatched to discharge C-1 + I-1 + 4 minors + the SPEC:6365-6371 exit-2 example.

R-2 research (rate limiting, §13 row 43): COMPLETE -> research-2.md (524 lines). MEASURED, not inherited:
  - Registered workers = 11, NOT 12 (grep @register_worker; base.py is the ABC). §38's "12" is WRONG,
    and cli.py:10044 at main still says "All twelve workers" -> QUEUED defect, cli.py is owned by a
    live lane this round; route next round.
  - Semaphore acquirers = 1 (workers/classify.py:161-162, the only limits.for_tier acquisition in src/).
    Sharper framing: 1 of 5 — only classify, buildgen, rewrite, buildverify, prwriter touch ctx.llm,
    and NEITHER llm/client.py NOR llm/calls.py acquires anything. Both heavy consumers unbounded.
  - Recommendation: Option C, a hand-rolled counting ResizableLimiter in budgets.py (~50 lines).
    anyio.CapacityLimiter measured working (4.14.2, find_spec under .venv) BUT is UNDECLARED in
    pyproject.toml and has zero existing uses in the tree -> Rule 5 favours the module that already
    owns Limits. CONTROLLER RULING: do NOT declare anyio; Option C avoids the dependency entirely.
    Option D (decline AIMD, document it) recorded as the honest fallback.
  - 7 subtasks proposed (R1-R7); R2->R3 flagged as the slice that alone closes D55's causal hop.
  - Could NOT answer (needs pytest / a real throttled endpoint / botocore absent under .venv):
    whether rewrite's WorkerRepairError path burns phases.attempts; the right throttle-retry bound;
    whether bedrock's ResponseMetadata.HTTPHeaders carries Retry-After; whether --accept-drift
    concurrency (shown mechanically reachable) is actually tested.

CR-2 review of 011b16d (phase_floor) + ddc16a6 (ADR-0078): FINDINGS.
  011b16d: 0 Critical, 4 Important, 3 Minor.
    I-A reentry.py:83-86 — backward walk stops on DEGRADED but NOT SKIPPED. ADR-0077 §5 makes
        SKIPPED non-demotable too. Repro: {SCAN:SUCCEEDED, TRANSFORM:SKIPPED, BUILD:SUCCEEDED,
        VERIFY:PENDING} -> floor TRANSFORM. Adding the stop leaves 36/36 green => UNBOUND EITHER WAY.
    I-B reentry.py:66-68/85 — documented "absent from evidence => not holding" default has NO test;
        mutating it to True leaves 36/36 green.
    I-C docs/SPEC.md:6867 — frontier defined as "not SUCCEEDED/SKIPPED", omits DEGRADED. Distinct
        from the known 186-187 site. ROUTED to the DOCFIX lane (sole SPEC.md writer).
    I-D design-resume-step5.md:214-226 — held(row) := DEGRADED defined and NEVER USED; the pseudocode
        walk has no DEGRADED stop, so subtasks 3-10 reconcile against the WEAKER algorithm. LIVE
        HAZARD for every lane still to be dispatched -> fix lane FIX-PF dispatched.
  ddc16a6: 0 Critical, 1 Important, 2 Minor.
    I-E cli.py:10043-10044 and :10274 still ship D67's REJECTED claim ("earliest phase whose
        PRECONDITION holds") in operator-facing error text. D67 swept SPEC only. :10043 is
        LINE-WRAPPED, so a single-line grep reported the class as fixed — second instance this round
        of a wrapped line defeating a sweep. QUEUED: cli.py owned by the live subtask-3 lane.
        NOTE: cli.py:10044 also carries R-2's stale "All twelve workers" (measured: 11). One lane
        fixes both next round.
  VERIFIED SOUND (do not re-litigate): both Rule 12 claims REPRODUCED in detached worktrees by the
    reviewer — naive-ascending -> 22/36 failed incl. both discriminators; cli.py:558 + ("bedrock",)
    -> old disjunction PASSES, both new tests fail. All ~20 ADR-0078 citations re-measured correct
    except a Minor: it is google.auth, not google, that raises. Host-scoping stated honestly and
    bound by the RULE, not the count.

Task 6: complete (commits 16879fe, 5488157, 38107e9, 9e5c093; 38 passed; DONE_WITH_CONCERNS).
  SqliteStateRepository.demote_to_floor(...) -> tuple[PhaseDemotion, ...] + checkpoints.delete_in_unit.
  IS the first real caller of demote() (audit item 8 prediction CONFIRMED; nothing blocked it).
  PhaseDemoted obligation discharged at repository.py:1445 in the same unit as the UPDATE (1429-1432).
  11 mutations, each proven landed via git diff --stat (harness ABORTS on a no-op — adopt this).
  The discriminating pair I demanded after CR-1's C-1: dropping the findings INSERT leaves the
  status/attempts/checkpoint test GREEN and fails only the findings test => the finding is bound
  independently of the status write. conn.commit() mid-unit keeps submits==1 while failing the
  rollback assertion => "one unit" is bound by more than a submit count.
  CONCERN 1 -> ADR-0082 ALLOCATED (checkpoint-drop scope: plan says demoted-rows, SPEC says span).
    Worker chose span-wide CONDITIONAL on a demotion having happened, pinned both directions.
  CONCERN 2: DEGRADED keeps its checkpoint per ADR-0077 §5; residual stale-anchor hazard documented,
    NOT resolved -> carry to §39 open items.
  CONCERN 3: demote_to_floor added to the StateRepository Protocol.
ADR ledger: 0078 taken (ddc16a6) · 0079 RESERVED subtask 9 · 0080 RESERVED subtask 10 ·
  0081 step-2 reap ordering (W-B draft) · 0082 checkpoint-drop scope (task 6). 0081+0082 to be
  LANDED by one lane once W-B returns; DECISIONS.md is currently unowned and free.

DOCFIX lane: complete (f02d124, b7fc5ec, 4ad7c1f, 23a4396; 120 passed + 148 passed).
  CLASS SWEEP RESULT: ~103 matches examined across 4 wrap-aware patterns; 17 wrong; 13 fixed;
  4 reported-not-fixed; ~86 matched-and-correct and LEFT ALONE (mirror-image rule honoured).
  TECHNIQUE THAT WORKED, adopt as project standard: whole-file whitespace normalisation with an
  offset->line map, NOT line-oriented grep. That is what surfaced the two-literal wrap at cli.py:10043.
  C-1's class was 6 SITES, NOT 2. ROOT CAUSE FOUND: ADR-0077 §4 item 1 scoped the remedy to
  "§11.5 step 5"; e5b8b11 inherited that scoping. Fixed at the root, else it regenerates.
  M-3 (the runner.py line-range mis-cite) was 5 COPIES, each corrected to what it actually quotes.
  Deliverable 4 resolved by MOVING the example, not widening any gate (ADR-0078 regression avoided);
  verified by loading the extracted fence: default/local/mixed exit 0, hosted_failover exit 2.
  NO ACTION (already handled): repository.py:1361 + test_repository.py:1232 carried claims falsified
  by f02d124; the owning lane had already fixed both in 38107e9. Cross-lane collision avoided.
OPEN, ROUTED NEXT:
  - CLAUDE.md:68 cites a test by its pre-rename name (stranded by DOCFIX's M-1 rename).
    One-line swap: ..._or_reaching_a_sink -> ..._or_naming_a_new_sink. Same class as 6a5e534.
  - cli.py:10043-10044 and :10274 (at 4ad7c1f) still ship the rejected D67 predicate in
    OPERATOR-FACING text; plus cli.py:10044's stale "All twelve workers" (measured: 11).
    Blocked on the live subtask-3 lane owning cli.py. Dispatch the moment it returns.

HK17 lane: complete (commit c7f72c6; 19 passed, was 18; DONE_WITH_CONCERNS).
  BINDING SHAPE (b) assert-the-semantic, chosen by MEASUREMENT not preference: diffed the SPEC's
  794-line CREATE TABLE fence against schema.sql -> 22 hunks. The SPEC listing is a deliberately
  condensed rendering, so a text binding would have been FALSE ON ARRIVAL. Decisive argument:
  adding CHECK (effort IN (...)) — the exact change the annotation forbids — leaves the comment
  untouched and passes any string comparison. The test writes a no-effort record through the real
  SqliteLlmCacheStore onto the real schema, asserting each clause separately: '' written (not a
  fabricated default), '' accepted (no CHECK), NULL rejected (NOT NULL enforced, not just declared).
  DISCLOSED LIMIT (why the status is qualified): a semantic binding does NOT catch literal
  comment-only re-drift. Mutation M1 shows the test passing with the annotation deleted again.
  Argued in the report rather than papered over — this is the behaviour we want.
  CLASS SWEEP: 6 found / 1 fixed / 5 left alone as correct. Retraction cited: D66,
  INTEGRATION_HONESTY.md:3798-3860 (87884d7, verified 6a41840), which itself named
  test_llm_cache.py:4 as the known sixth carrier. `grep -ni thinking src/ tests/` now returns zero.
  Mutations M2/M3/M4 each discriminating, file-change proven by numstat, run in a detached worktree
  with a PRIVATE BAZEL_ROOT (good: avoids the pytest_sessionfinish reap of sibling output bases).
  NOT DEFECTS, do not action: SPEC's listing omits CapabilityDrift but SPEC names it 9x elsewhere;
  llm/__init__.py:6 is correct and will keep matching `temperature` sweeps.
  MINOR for §39: the round-B audit's one-line quote of SPEC:4254 is stale — it is two lines.

FIX-PF lane: complete (4a1a184 reentry.py+tests; 8c00971 plan algorithm block; 36 -> 41 passed).
  D1 SKIPPED hard stop: chose the TWO-SIDED reading of ADR-0077 §5 (never demoted ONTO, never
  searched PAST). Reasoning: an excluded phase can never produce holding evidence, so the
  transparent reading would demote every repo with an excluded middle phase to phase 1 on EVERY
  resume. Discriminating mutation: revert the stop -> 3 failed / 38 passed, the 3 being exactly the
  new SKIPPED cases and the 38 including all 36 pre-existing. M3 (_HARD_STOPS minus DEGRADED) ->
  1 failed, the DEGRADED test alone => the refactor kept that binding too.
  D2 evidence default: get(phase, False) -> get(phase, True) -> 2 failed / 39 passed, the 2 being
  exactly the new sparse-mapping cases => the two deliverables' evidence is NOT double-counted.
  D3 plan doc: held() -> hard_stop(row) := {DEGRADED, SKIPPED}; settled gains DEGRADED; the dangling
  "ambiguity 5" pointer now names ADR-0077 §5; step 3 restated as demote_to_floor landed it.
CR-3 review of subtask 6 (16879fe..9e5c093): FINDINGS — 0 Critical, 2 Important, 4 Minor.
  I-1 tests/test_repository.py:1224-1268 — the "re-raises the same findings without duplicating
      them" test is UNBOUND: the second demote_to_floor call demotes nothing (rows already PENDING),
      so the findings INSERT never runs and _DEMOTE_FINDING_SQL's ON CONFLICT ... DO UPDATE
      (repository.py:833-839) has ZERO coverage. Deleting the whole clause leaves all 38 green;
      a probe that re-succeeds phases 2-4 then demotes again raises
      IntegrityError: UNIQUE constraint failed: ux_findings_ident. CODE IS CORRECT, THE TEST CLAIM
      IS NOT -> fix is a test, not a code change.
  I-2 Guardrail 7 — PhaseDemoted is now written by a live INSERT but is ABSENT from the curated
      findings.kind "Shipped:" listing at schema.sql:253-283 and its copy at SPEC.md:4050-4058,
      whose own CAVEAT exists precisely to answer "which kinds are actually emitted".
  DISCHARGED: the controller's added obligation verified by reproduction in a detached worktree —
      M3 (drop the findings loop) fails the findings test while the status/attempts/checkpoint test
      stays GREEN => finding bound independently of the status write. M8 reproduced too.
  RULING INHERITED: the DEGRADED residual is NOT accidentally reachable today (phase_floor cannot
      put a DEGRADED row in the span), so documenting rather than patching is correct.
      WATCH: --from-phase (subtask 9) is the path that would change that. Carry into subtask 9's brief.
CROSS-LANE INCONSISTENCY FOUND (FIX-PF concern 2): demote_to_floor's checkpoint sweep excludes
  DEGRADED ONLY, so a SKIPPED row above the floor still loses its checkpoint — while phase_floor now
  treats SKIPPED and DEGRADED identically. Two lanes, two answers, same status.
  -> decision routed to the live ADR-0082 lane; implementation routed to lane CONSIST.
ALSO ROUTED to CONSIST: CR-2 F10's off-by-one clause at src/fleet/models/enums.py:64 (FIX-PF
  concern 1, out of its lane).

ADR82 lane: complete (2c8dc79 CLAUDE.md citation; 4ff8157 ADR-0082 at DECISIONS.md:7290 + SPEC.md).
  DEGRADED hazard RULING: adversarial-only -> stated boundary, documented, NOT patched. Basis:
  ALLOWED_TRANSITIONS[SUCCEEDED] is the empty set (enums.py:47), so "a DEGRADED row strictly above
  the frontier" has no base case. §4 names the three modules the induction spans and states plainly
  that nothing binds it to demote_to_floor's signature.
  Citation sweep: 4 found / 1 fixed / 3 left alone as correct. CLAUDE.md:68 fixed; DECISIONS.md:6940
  already correct; two in ledger-sdd-backlog-b.md are round-B HISTORY recording the renames as they
  happened -> correctly left alone. The wrap-aware technique earned itself again: the :1330 pair
  splits both names across THREE lines. Resolved against pytest --collect-only.
  Guardrail 7 second edit forced: SPEC:6907-6909 stated the demoted-rows-only reading that the
  lane's own mutation M11 pins as FAILING; corrected in the same commit.

*** CONTROLLER ERROR, CAUGHT AND REVERSED ***
  I ruled that demote_to_floor's checkpoint sweep should treat SKIPPED like DEGRADED, on symmetry
  with phase_floor's hard stop, and dispatched CONSIST to implement it. ADR-0082 §3 concluded the
  OPPOSITE and is right. Verified myself at enums.py:32-51 before reversing:
    ALLOWED_TRANSITIONS[SKIPPED]   = frozenset()  -> terminal, no exits
    ALLOWED_TRANSITIONS[SUCCEEDED] = frozenset()  -> terminal, no exits
    ALLOWED_TRANSITIONS[DEGRADED]  = {RUNNING (budgeted revalidation, §3.5.1), SUCCEEDED, RHI}
  The two carve-outs answer DIFFERENT questions and the asymmetry is principled:
    - the WALK's hard stop protects a DECISION (never demote onto / search past an excluded phase)
      -> SKIPPED and DEGRADED both belong; FIX-PF's 4a1a184 STANDS.
    - the SWEEP's carve-out protects a PAYLOAD A FUTURE ROUND RESUMES FROM -> only DEGRADED has such
      a round. A SKIPPED phase can never re-enter RUNNING, so its checkpoint is unreadable forever.
  Reversal sent to CONSIST mid-flight; Deliverables 2/3/4 unchanged. Asked CONSIST to convert the
  error into coverage: pin that a SKIPPED checkpoint IS dropped, discriminating mutation = the
  carve-out I wrongly ordered. LESSON: I issued a symmetry ruling without checking whether the two
  sites answer the same question. Second controller ruling this round overturned by a worker
  (first: DEGRADED already settled by ADR-0077 §5).
OPEN, needs a number next round: ADR82 concern 4 — §4's boundary may be ledger-worthy but no
  D-number was allocated to that lane. ADR82 concern 2: the DEGRADED ruling rests ENTIRELY on
  SUCCEEDED having no other exit; name a writer that unsettles a phase beneath a completed one and
  it flips from boundary to defect. Carry both into §39.

CR-4 review of the doc-correctness wave (f02d124, b7fc5ec, 4ad7c1f, 23a4396, c7f72c6):
  DOCFIX: 1 Critical, 2 Minor. HK17: 2 Important, 1 Minor.
  *** SWEEP COUNTS DO NOT REPRODUCE — correcting my own ledger entry above. ***
    DOCFIX claimed ~103 raw / 17 wrong / 13 fixed / 4 reported. CR-4 measured, with a
    quote-AND-whitespace-collapsing normaliser plus offset->line map: 70 raw pre-wave, 76 on main.
    The raw total does NOT reproduce and "13 fixed" is NOT reconstructible (>=15 claim-sites
    actually changed). The CLASS results DID reproduce: frontier-status class 1->0; D67 class 13 on
    main (11 labelled-historical, 2 the known cli.py sites); runner.py:214-218 empty and all five
    re-citations verified. => Trust DOCFIX's class findings; do NOT carry its raw counts forward.
    HK17 claimed 6/1/5 + "grep -ni thinking src/ tests/ = zero". 6/1/5 reproduces; THE ZERO DOES
    NOT — measured 1.
  C-1 CRITICAL, docs/SPEC.md:6915-6918, WRITTEN BY 23a4396 — i.e. by the DOCFIX fix itself:
    asserts step 5 "neither demotes a DEGRADED row nor searches past one" but (a) the SKIPPED half
    is ABSENT ENTIRELY, so a SPEC-only reconciler demotes a green Phase-1 row below every SKIPPED
    phase on EVERY resume; and (b) neither named mechanism delivers "searches past" —
    _SETTLED_FOR_DEMOTION membership makes the scan PASS OVER it and demote() RAISES rather than
    stops. The real property lives in reentry._HARD_STOPS, which appears ZERO times in docs/.
    THE PATTERN: the lane that had just proven a too-narrowly-scoped remedy regenerates the defect
    then scoped ITS OWN remedy to the reported status. Still true at HEAD 4ff8157 — siblings fixed
    code, plan and ADR; nobody fixed the SPEC. -> routed to CONSIST as Deliverable 5.
  I-1 HK17's "grep returns zero" is FALSE (returns 1; the hit is HK17's own new CORRECT line).
    Next sweeper either re-opens the item or "fixes" a true sentence. -> queued.
  I-2 HK17's disclosed M1 limit is ACCIDENTALLY REACHABLE (it IS the defect item 17 fixed), and its
    "only honest mechanism is a whole-fence extractor" dichotomy is FALSE — a marker assertion
    closes it without touching the 22 condensations. => Overturns CR-4's-predecessor's acceptance
    of that boundary. -> queued (needs schema.sql, owned by CONSIST until it returns).
  VERIFIED GOOD: M2 discriminating (old 18 passed / new 1 failed, detached worktree, private
    BAZEL_ROOT); the 22-hunk measurement EXACT; Deliverable 4 reproduced (default/local/mixed exit 0,
    hosted_failover exit 2, no gate widened); CapabilityDrift ruling upheld (9 lines).

CONSIST lane: complete (704e52f tests+docstring; 8ea1881 docs/listings; 201 passed, baseline 199).
  D1 CORRECTLY NOT IMPLEMENTED — the lane reached my reversal INDEPENDENTLY from ADR-0082 §3 and
    re-verified its premises at enums.py:25-26,42-49. Converted my error into coverage as asked:
    a test pinning that a SKIPPED row keeps its status and LOSES its checkpoint. Mutation = add
    SKIPPED to the carve-out (13+/1-): OLD 38 passed, NEW 1 failed. My wrong ruling is now a
    failing test for the next person who reasons from symmetry.
  D2 code untouched, test replaced. Mutation = delete the ON CONFLICT tail (12+/2-): OLD 38 passed,
    NEW 1 failed on IntegrityError: UNIQUE constraint failed: ux_findings_ident. Bound at last.
  D3 THE REAL SIZE OF THE LISTING GAP: 27 emitted kinds found · 19 added to schema.sql, 21 to
    SPEC.md · 8/6 already listed. CR-3 reported ONE missing kind (PhaseDemoted); the class was 19-21.
    Both copies now carry identical 39-name sets. Detector validated: fires on HEAD, silent after.
  D4 CR-2's F10 CONFIRMED correct and fixed at THREE sites (F10 named two).
  D5 (CR-4's Critical) done: _HARD_STOPS named, both statuses covered, demote() corrected to "raises".
OPEN -> dispatched now:
  - ADR-0082 §4 premise is WRONG: it says SKIPPED is "written only to phase 1", but `fleet quarantine`
    writes it to any non-terminal phase (cli.py:9717-9767). The CONCLUSION survives; the premise needs
    correcting by the DECISIONS.md owner. -> lane REC.
  - CONSIST concern 2: the EMITTED listing is a SNAPSHOT and will drift again; a membership test would
    make it mechanical. -> lane MECH (with CR-4's I-2, same theme).
  - CR-4 I-1: HK17's "grep returns zero" claim is false (returns 1). -> lane REC, record correction.
STILL BLOCKED on the live subtask-3 lane (48m, largest scope of the round): CR-2 F7 /
  cli.py:10073 + :10367 shipping D67's rejected algorithm, and cli.py's stale "All twelve workers".

REC lane: complete (commit 37fa292; DECISIONS.md + INTEGRATION_HONESTY.md + PROGRESS.md).
  D1: ADR-0082 §4's premise CONFIRMED FALSE at main. Root: the ADR's citation quoted only the
    PHASE-1 FALLBACK BRANCH of `fleet quarantine` and missed the general `executemany` above it —
    a citation that read one branch of the code it cited. Corrected in place via the file's own
    "Editorial correction" blockquote convention; no history rewrite.
    *** §4's CONCLUSION SURVIVES *** and never rested on "phase 1 only": it rests on SKIPPED's entry
    edges being PENDING/BLOCKED-only (still true) plus §2's carve-out excluding only DEGRADED.
    The §4 hazard's actual subject (DEGRADED, stated/unpatched boundary) never touched the SKIPPED aside.
  D2: re-measured TWO ways (line-oriented grep AND a whitespace-normalised whole-file scan with an
    offset->line map): both agree the answer is 1, not 0. The single hit is tests/test_llm_cache.py:4
    — the correcting lane's own CORRECT docstring line. Corrected two now-stale "still unamended"
    claims that predated c7f72c6 landing (INTEGRATION_HONESTY.md D66; PROGRESS.md items 14 and 9).
  Record sweep: 9 found / 4 corrected / 5 left alone. Left alone correctly: 2 untracked scratch
    originals carrying the literal false claim but cited by no committed doc; 3 untracked scratch
    entries that ALREADY flag the falsehood; and the correct test_llm_cache.py:4 line itself.
  FLAGGED NOT EDITED (not owned): docs/superpowers/plans/open-items-audit-round-b.md:33,75 and
    ledger-sdd-backlog-b.md:1996 are also stale from the same c7f72c6 fix. -> lane DOCSTALE.

CR-5 review of FIX-PF + ADR82 + CONSIST (4a1a184, 8c00971, 2c8dc79, 4ff8157, 704e52f, 8ea1881):
  FIX-PF CLEAN (1 Minor) · ADR82 FINDINGS (1 Important) · CONSIST FINDINGS (1 Critical, 1 Important,
  2 Minor). Scoped suite 201 passed.
  ALL FOUR MUTATION CLAIMS RE-RAN AND REPRODUCED EXACTLY (detached worktree, private BAZEL_ROOT,
  each confirmed non-no-op by numstat FIRST): hard-stop revert, evidence default, ON CONFLICT
  deletion, SKIPPED-in-carve-out. No lane overstated a mutation this wave.
  CONSIST'S LISTING COUNTS ALL REPRODUCE EXACTLY: 27 emitted / 19 missing schema.sql / 21 missing
  SPEC.md / 8-6 already listed / 39-name sets set-identical. Arithmetic closes both ways
  (20+19, 18+21). Contrast the earlier wave, where two claimed sweep totals did NOT reproduce.
  *** CRITICAL (CONSIST) — THE NARROWER SUCCESSOR, 4th OCCURRENCE THIS ROUND, again written by the
  lane hunting it: docs/SPEC.md:182, :6936 and src/fleet/models/enums.py:64 fixed the floor
  off-by-one but KEPT THE WRONG QUANTIFIER. The walk stops at the HIGHEST holding phase below the
  frontier, NOT the "earliest". Measured: actual floor BUILD, the SPEC's formula gives TRANSFORM.
  tests/test_reentry_floor.py:187-197 ALREADY CONTRADICTS the new sentence. A reconciler demotes to
  phase 2 on every resume. NOTE: "earliest" is inherited from SPEC §11.5 step 5's original wording —
  the design doc fixed the PREDICATE (preconditions -> evidence) and nobody ever fixed the QUANTIFIER.
  IMPORTANT (CONSIST): the new CAVEAT clause "every other name in the DECLARED list is emitted from
  cli.py" (schema.sql:283, SPEC.md:4076) is FALSE for UnmergedDependency, VersionConflict,
  CoarseTarget, RuleOscillation — no writer reaches any INSERT for those four.
  IMPORTANT (ADR82): design-resume-step5.md:258-260 still calls the SKIPPED-checkpoint question OPEN
  and assigns it to subtask 6, though ADR-0082 §3 decided it and 704e52f pinned it.
  CONFIRMED: ADR-0082 §4's conclusion SURVIVES its known-false premise (SKIPPED has no carve-out, so
  it cannot anchor a stale payload) — independently agreeing with lane REC.
ROUTING: SPEC.md:182/:6936 + schema.sql:283/SPEC.md:4076 are owned by the live MECH lane -> QUANT
  authors the replacement wording, controller routes it. enums.py:64 + design-resume-step5.md:258-260
  are free -> lane QUANT owns them.

MECH lane: complete (b995458 D1; 6e0a5fa D2; 40 passed scoped; DONE_WITH_CONCERNS).
  D1 tests/test_findings_kinds.py (new): AST enumeration of every INSERT INTO findings in src/,
    resolving `kind` through module constants, local bindings, IfExp, f-strings and function
    parameters (default + every call-site keyword). Four assertions; the LOAD-BEARING one is that any
    site whose kind cannot be RESOLVED is a hard failure naming it, exemptable only via an explicit
    _INDIRECT_SITES entry — that gate is what stops the detector rotting into silent brittleness.
    A registry/enum would be stronger but changes cli.py's 16 call sites; named in the docstring
    with the residual, per Rule 2 rather than built speculatively.
    *** INDEPENDENT CORROBORATION: it reports 27 emitted kinds — CONSIST's hand-measured count,
    reached by a completely unrelated method. Third independent agreement on that number. ***
    THREE-WAY VALIDATION: fires on 8ea1881~1 naming 19 missing in schema.sql and 21 in SPEC
    (CONSIST's exact numbers); silent on the swept tree; fires on FOUR synthetic injections — fake
    worker kind, deleted listing entry, renamed writer (both directions), unresolvable kind form.
    Discriminating: on the reverted tree test_schema_sql.py = 16 passed while the new file = 3 failed
    => no prior test bound the listing at all.
  D2 marker PLUS mirror in tests/test_llm_cache.py: extracts the llm_cache.effort comment region from
    both files, whitespace-normalised, asserts each cites ADR-0075 AND that the two carry the same
    words. MORE than the marker CR-4 proposed, and the lane justified why: a marker alone passes on a
    region that kept the citation and lost every clause. Touches none of the 22 condensations;
    semantic assertions retained. Three-way: fires at c7f72c6~1, silent clean, fires on annotation
    deletion AND on a SPEC-only reword; CONTROL: a pure reflow passes (no false positive on format).
    => CR-4's I-2 ruling (accidentally reachable, not a boundary) is discharged with a real mechanism.
*** PROCESS INCIDENT, no work lost, controller-verified ***
  A worktree created INSIDE the shared scratchpad was deleted mid-run by something outside that lane.
  The lane's `cd` into it failed; `set -e` did NOT abort the compound command; so one
  `git checkout <ref> -- <paths>` executed IN THE PRIMARY CHECKOUT. The lane caught and restored it in
  the same block. Controller verified after the fact: git log intact through 6e0a5fa, no stash,
  worktrees/wt-WT1-example PRESENT, dirty files are exactly the two live lanes' in-progress edits.
  STANDING RULE ADDED, propagated to the live lane: detached worktrees must live OUTSIDE the shared
  scratchpad, and every `cd` must be guarded with an explicit `|| exit 1` — `set -e` does not cover it.
  A cd that fails silently turns a mutation into an edit of main.
  ALSO WARNED the live cli.py lane: b995458's detector now reads cli.py and will fail an unresolvable
  or unlisted findings writer. Told it that is the mechanism working, and NOT to weaken or exempt it
  unilaterally — report to me instead.
OPEN: MECH concern 3 — task-item17-report.md's M1 disclosure is now stale (needs an
  INTEGRATION_HONESTY.md ledger entry, no ADR). MECH concern 4 — the DECLARED half stays unbound in
  the still-emitted direction by its own CAVEAT; related to CR-5's false-CAVEAT Important, whose
  replacement wording lane QUANT is authoring.

DOCSTALE lane: complete (ae4b923 D1; 5be5064 D2).
  D1 sweep: 5 found / 2 corrected / 1 annotated / 2 already-fixed-by-a-sibling (skipped) / 2 unrelated
    false positives left untouched. Wrap-aware normalised scan with offset->line map.
    RULING on ledger-sdd-backlog-b.md:1996: HISTORICAL — it sits inside the "ROUND B FULLY CLOSED"
    narrative recording what was true at 5feb1e7, so ANNOTATED with an editorial blockquote naming
    c7f72c6 rather than rewritten. Correct call: annotating history, not falsifying it.
  D2 promoted OUT of git-ignored scratch into docs/superpowers/plans/ (the dangling-reference risk
    the user flagged from last round):
      resume-step5-subtasks-4-6-research.md      (was research-1.md)
      rate-limiting-scope-research.md            (was research-2.md)
      design-resume-step5-task6-demotion-writer-report.md (was task-6-report.md — RENAMED)
      task-item17-report.md                      (a genuine FOURTH dangling ref, cited by
                                                  INTEGRATION_HONESTY.md; found by the lane, not by me)
  NEAR-MISS worth keeping: the first promotion `cp` SILENTLY CLOBBERED an unrelated pre-existing
    committed task-6-report.md. Caught via `git status` BEFORE staging, reverted, re-promoted under a
    distinct name, no data lost. General hazard for any scratch-promotion task: check the destination
    exists before cp.
  STILL DANGLING (not owned by that lane): DECISIONS.md:7300, :7466 and INTEGRATION_HONESTY.md:3859
    still cite the old .superpowers/ scratch paths. -> lane APPLY.
QUANT lane: DIED on an API 529 mid-report — but BOTH deliverables committed first and the 17KB report
  was written. Commits 71dc184 (models/enums: floor is above the HIGHEST holder below the frontier,
  not the earliest) and 7670fc2 (plans/design-resume-step5: SKIPPED-checkpoint question decided).
  Its §5 carries the exact replacement text for the sites it did not own — A/B (SPEC.md:182-183,
  :6936-6938), D (schema.sql:283 CAVEAT), and a FIFTH site it found that my brief missed and it calls
  the highest-value of the set: DECISIONS.md:6712-6713, a live decision record stating the rule.
  It also corrected a stale line anchor in the review it was discharging (cycles.py :935 -> :923).
  NOT re-dispatched: its own work is done; the handoff is an apply-job -> lane APPLY.

APPLY lane: complete (60d400b, e0404b0, a58fc1c, d0de310; 65 passed; DONE_WITH_CONCERNS).
  All FIVE sites located BY TEXT with an exact-match replacer that ABORTS on mismatch; all five
  matched the handoff character for character including indentation. It applied §5 E as well
  (my brief listed only A/B/C/D) and justified it: same false CAVEAT in SPEC.md's listing copy, and
  Guardrail 7 requires code and doc listing in ONE change. Correct call.
  CAVEAT verified INDEPENDENTLY by four probes before applying (UnmergedDependency = one docstring
  hit only; GraphFinding/RewriteFinding importers hold no INSERT; OversizeBlob/SymbolBudgetExceeded
  reach cli.py:1582 via `for kind in output.findings`).
  .superpowers/ sweep over 9,737 files: 23 found / 4 repointed / 19 left alone; re-sweep leaves 6,
  ALL historical, ZERO live. The INTEGRATION_HONESTY.md citation SPANNED A LINE BREAK and also
  mislabelled its target "untracked" — both fixed. Quantifier re-sweep: 46 `earliest` hits, ZERO live
  statements of the floor rule with the wrong quantifier.
  *** 5th NARROWER/WRONG SUCCESSOR OF THE ROUND — this time in the handoff text authored expressly to
  stop the drift. The clause "and SCAN if no phase below the frontier holds" is FALSE: phase_floor
  breaks on _HARD_STOPS BEFORE consulting evidence. Measured: BUILD DEGRADED => floor VERIFY;
  TRANSFORM SKIPPED => BUILD. The lane did NOT re-author it — fixing a subset restores the drift —
  and disclosed it in d0de310 instead. Also found §5 C omits the clause, so the five were never
  identical. -> lane FIVESITE, one author, all five sites, one commit. ***
CR-6 review of MECH/REC/DOCSTALE/QUANT: REC clean · QUANT clean · MECH accept w/ 1 Critical ·
  DOCSTALE accept w/ 1 Important. Totals 1 Critical, 3 Important, 2 Minor.
  MECH's 27/19/21 REPRODUCED EXACTLY by a THIRD method (loading the module, calling _emitters()).
  Current tree 27 emitted / 0 drift; pre-fix 704e52f: 27 / 19 missing schema.sql / 21 missing SPEC.
  Rule 12 discrimination holds independently (old test_schema_sql.py 16 passed vs new 3 failed,
  identical tree). => the 27 is now corroborated by four independent methods.
  *** C-1 THE DETECTOR CAN BE SILENTLY DEFEATED — and NOT by an exotic `kind` form: that path is
  genuinely loud, as MECH claimed. The hole is the RECOGNITION step BEFORE it, an unnormalised
  substring pre-filter (tests/test_findings_kinds.py:288, :129-137). Reformatting a REAL existing
  site — cli.py:9747's OperatorQuarantine INSERT — from implicit string concat into a triple-quoted
  block takes emitted 27->26 with unresolved == () and 4 PASSED. Site gone, no sound. Same silence
  for SQL hoisted to a local var and for `INSERT OR REPLACE INTO findings`. ***
  I-1 :196-203 _binding returns the FIRST assignment, not a union, so a conditionally reassigned kind
      reports one of two literals — while the docstring at :25-29 claims the resolver is conservative.
  I-2 :29 says "cli.py's sixteen call sites"; measured: cli.py has 13. 16 is the whole-src/ total.
  I-3 docs/superpowers/plans/task-item17-report.md:110 was PROMOTED INTO THE TRACKED TREE asserting
      `grep -ni thinking` returns ZERO — nine minutes after 37fa292 measured ONE — with no in-file
      marker, and D66's pointer to it uses the OLD SCRATCH PATH and is line-wrapped.

FIVESITE lane: complete (single commit f466287; SPEC.md + DECISIONS.md; 65 passed).
  Floors re-measured independently by calling phase_floor directly: BUILD DEGRADED => VERIFY;
  TRANSFORM SKIPPED => BUILD. Both match. It ALSO measured the two values its own new wording
  asserts (no-holder-no-hard-stop => SCAN; frontier-is-SCAN => SCAN) rather than asserting them.
*** CONTROLLER ERROR #3, caught by the lane: MY BRIEF'S SITE LIST WAS WRONG. Only THREE of the five
  named sites state the floor rule. §5 D (schema.sql) and §5 E (SPEC.md copy) are the findings.kind
  CAVEAT — a paragraph about undeclared/unemitted finding kinds containing no SCAN, no floor, no
  phase_floor. ROOT CAUSE: I inherited the list from APPLY's COMMIT GROUPING rather than from a
  sweep. A commit groups by file ownership, not by claim. The lane verified by reading both sites and
  checking QUANT's §5 D/E handoff blocks, then acted on its measurement and left the CAVEAT alone.
  LESSON: never derive a site list from how a previous lane grouped its commits; derive it from a
  sweep for the claim. (Earlier controller errors: DEGRADED already settled by ADR-0077 §5;
  the SKIPPED-symmetry ruling.) ***
  Hard-stop property was ALREADY BOUND — no test added, correctly. Discriminating mutation: delete
  the _HARD_STOPS break (numstat 0/2, proven changed BEFORE the result was read) => 4 failed /
  37 passed. The two discriminating tests are the ones where nothing below holds, so a naive walk
  reaches SCAN: test_degraded_phase_is_a_hard_stop... (VERIFY->SCAN) and
  test_search_does_not_pass_a_skipped_phase_even_when_nothing_earlier_holds (BUILD->SCAN).
  Re-sweep: all three sites carry a BYTE-IDENTICAL 336-char normalised clause; zero live statements
  retain the false fallback. A line-oriented grep found only 1 of the 3 sites — the wrap-aware
  technique earned itself a FIFTH time this round.
  ASYMMETRY ELIMINATED: ADR-0076 §1's omission of the clause is fixed.
OPEN -> dispatched to lane BIND:
  - A FOURTH correct statement of the rule at enums.py:64 (different register); NOTHING BINDS the
    four copies to each other. They agree today by hand, not by mechanism.
  - INTEGRATION_HONESTY.md:4055 disclosure now stale (needs its owner).
  - CR-6 I-3: docs/superpowers/plans/task-item17-report.md:110 entered the TRACKED tree asserting
    `grep -ni thinking` = zero, nine minutes after 37fa292 measured ONE. Faithful promotion of
    evidence that had been falsified elsewhere -> needs an in-file correction marker, NOT an edit to
    the evidence. D66's pointer to it also uses the old scratch path and is line-wrapped.
STILL BLOCKED on the live reap lane (1h20m): cli.py:10043/:10274 ship the rejected algorithm to
  OPERATORS, plus cli.py's stale "All twelve workers" (measured: 11).

DETECTOR lane: complete (commit 363ecc1, tests/test_findings_kinds.py +190/-30).
  C-1 closed with TWO changes, because a cross-check alone would have fired on layout:
   (1) recognition widened to a whitespace-normalised match for INSERT [OR <verb>] INTO
       [<schema>.]findings, plus sql_of resolving a singly-assigned local — so the reviewer's
       reformats now stay RESOLVED, not merely flagged;
   (2) _recognition_gap re-derives sites from source TEXT and matches them against the span of each
       recognised literal, failing loudly by file:line in BOTH directions.
   Prose is subtracted BY RULE (bare string statements + tokenize comments), so the stubs.py hit
   needs NO hand exemption — a hand exemption is what would have rotted.
   Calibration on the clean tree: 16 AST sites vs 15 text sites (one constant executed twice).
  RESIDUAL, stated in the docstring, no closure implied: both instruments read Python source text, so
   both are blind to the same class — f"INSERT INTO {table}", a name split INSIDE the word across
   fragments, a query builder, SQL from a data file; matching is line-granular, not column. Only a
   FindingKind enum removes it.
  THREE-WAY + CONTROLS, all in a detached worktree, every injection diff-confirmed, old-vs-new on
   identical input: reviewer's reformat — old 26 kinds/site VANISHED/4 passed, new 27/site retained;
   with the kind changed to an unlisted SynthQuarantineV1, OLD 4 PASSED / NEW 1 FAILED naming it.
   Clean tree silent (also in the primary checkout with the live lane's cli.py present). Injections:
   hoisted local and INSERT OR REPLACE both silent on old, both fail loudly on new; a _run_sql helper
   fires _recognition_gap itself. CONTROLS: cosmetic re-break/reindent of the real statement AND a
   re-wrap of the prose mentioning it — both green. FOURTH check: narrowing its own text regex made
   the reverse branch fire, silent on clean.
  I-1 resolved CODE-OVER-DOCSTRING (union), with the right reasoning: a conditionally reassigned kind
   is the ORDINARY form, so narrowing the docstring would have documented the common case as a hole.
   Measured old 28 kinds/one literal -> new 29/both.
  I-2 measured: cli.py = 13, whole src/fleet = 16 (13/2/1).
  OPEN: task-mech-report.md:27,:130 repeat the wrong 16-for-cli.py count (scratch file, not promoted —
   low priority, fix if it is ever promoted). M-1's _worker_findings blacklist unfixed but DISCLOSED.

BIND lane: complete (5f052f2 mechanism; bf7206d two records; 8 tests, 69 with siblings).
  tests/test_floor_rule_statements.py — THREE LAYERS, no marker injected into SPEC/DECISIONS/enums
  (structural anchoring sufficed, so the brief's conditional edit permission went unused — good):
   A identity over the 3 prose copies: a census on the QUANTIFIER PHRASE, deliberately anchored on
     text OLDER than the correction so a reverted site is still FOUND rather than silently missing;
     then a per-site anchored, whitespace-normalised clause match. Failures name file and LINE, not
     a count.
   B *** THE PROSE DRIVES THE ASSERTIONS *** — the hard-stop status set, the cited
     orchestrator/reentry symbol and the fallback phase are PARSED OUT OF THE CLAUSE and checked
     against _HARD_STOPS, the module, and phase_floor. Editing the prose changes what is asserted.
     This is the strongest construction produced this round; prefer it as the pattern.
   C enums paraphrase: a whitelist of seven load-bearing distinctions plus that same parsed-out set —
     a whitelist because both prose and paraphrase legitimately contain "earliest".
  CANNOT BIND, stated plainly, no closure implied: a CONSISTENT rewrite of all three copies to one
   false sentence; Layer C's token whitelist against a false rewrite; a site re-worded PAST the census
   anchor (reads as deleted); and cli.py:10043/:10274, outside _EXPECTED_SITES.
  VALIDATION (detached worktree, own BAZEL_ROOT by digest, every mutation proved by numstat BEFORE
   its result was read): known-bad — SPEC Constraint 7 reverted to pre-f466287 => 4 failed, NAMING
   docs/SPEC.md:182. Clean => 8 passed. Synthetic fault in a DIFFERENT copy — "durable" dropped from
   ADR-0076 §1 => 1 failed naming docs/DECISIONS.md:6713. Two extra: SUCCEEDED added to _HARD_STOPS
   => 3 failed via B and C; "hard stop" dropped from enums => 1 failed.
   CONTROL: SPEC §11.5 reflowed @50 AND enums @70 simultaneously => 8 passed, with word-identity
   asserted IN THE INJECTOR rather than inferred from `git diff -w`.
   Discriminating: known-bad tree WITHOUT the new file => 61 passed; same tree WITH it => 4 failed.
  *** CONTROLLER ERROR #3 PROPAGATED INTO A COMMITTED DOC, now fixed by this lane: the honesty entry
   said "five sites". schema.sql has NEVER held a floor statement (two probes) and 60d400b never
   touched DECISIONS.md. My wrong site list reached docs/ before FIVESITE caught it. Corrected in
   place by bf7206d. ***
  SELF-CAUGHT BY FAULT INJECTION: the lane's own first extractor used an unbounded `.*?` that matched
   into the NEXT site. Found only by the synthetic-fault check, fixed with a measured 600-char bound.
   This is precisely why Guardrail 6's third check exists — a detector correct on the known-bad state
   and silent on the clean one was still broken.
  Concern: D66's pointer was ALREADY repointed by e0404b0 — my brief's premise was stale.

LESSONS lane: complete (commit f84edcb, CLAUDE.md only, +10/-5; 118 -> 123 lines, ceiling ~140).
  ADOPTED as 5 new lines: §3 "A Ruling in a Brief Is a Fallback; a Site List Comes From a Sweep"
  (all three controller errors in ONE bullet, incl. that a commit groups by file ownership, not by
  claim) · §6 detached worktrees outside the shared scratchpad, private BAZEL_ROOT, `cd || exit 1` ·
  G6 "a detector's recognition step is a separate attack surface from its resolver" (363ecc1) ·
  G7 "bind prose to code by making the prose drive the assertion" (5f052f2 Layer B) ·
  G7 "a record of what was true then is history: annotate it, never rewrite it".
  ADOPTED as in-place SHARPENINGS at zero net lines — the right instinct: G7's class sweep now
  FORBIDS the line-oriented grep it used to prescribe; Rule 12's changed-the-code proof becomes
  mechanical and read FIRST; G6 gains measured-vs-reproducible, a control as the fourth check, and
  the two structural causes of the narrower successor.
  DROPPED candidates 4 and 5 as standalone rules — already in the file; a second bullet restating
  them is the exact drift this round spent effort undoing. Folded as clauses instead. Correct call.
  *** ITS OWN NARROWER-SUCCESSOR RE-READ FOUND FIVE OVERCLAIMS IN ITS OWN TEXT — calling a
  correctly-conditional ruling "overturned"; inventing --numstat as THE one flag; "two sweeps" when
  only ONE raw total failed to reproduce; a backwards scope-inheritance sentence; narrowing the
  worktree rule to mutation runs. Re-running the check ON THOSE CORRECTIONS found a SIXTH, INSIDE a
  correction: it implied the root-scope fix closed the class, when the very commit making that fix
  wrote a fresh narrower one. Six instances, in a lane whose subject was that failure mode. This is
  the round's single best evidence that the re-read is a mechanism, not ceremony. ***
  Also caught a probe error of its own: the space in the repo directory name made its first `ls`
  probe the wrong path; it re-probed with `git worktree list` and confirmed wt-WT1-example intact.
  CONCERN for round D: G6 is now SEVEN bullets and is the section most at risk of bloat — SPLIT it,
  do not append. The 27/19 counts now in CLAUDE.md are illustrative snapshots bound by no test.

CR-7 review of APPLY + FIVESITE + DETECTOR: 1 Critical, 5 Important, 2 Minor.
  DETECTOR: ACCEPT, NO HOLE. The reviewer could NOT defeat it with any form it claims to catch;
  reproduced review-6's C-1 defeat and the site now stays RESOLVED (27 kinds, 4 passed, no layout
  false-fire), with the Rule 12 shape intact — OLD detector 4 passed, NEW 1 failed naming
  SynthQuarantineV1 on the same diff-confirmed input. I-1's union verified (29 kinds, both literals);
  >1 => unresolved judged CORRECT for a params tuple, whose SHAPE cannot be unioned.
  RESIDUAL HONEST AT THE HEADLINE, over-promising in ONE sub-claim: the admitted f-string form does
  slip through as stated, but "_TEXT_INSERT tolerates an explicit +" holds only for ADJACENT
  literals — "INSERT INTO " + _TAIL and a split across two module constants are silent in BOTH
  instruments. Adversarial (the split must land exactly at the table token; natural + splits fire),
  so a boundary under Rule 12's stop rule — but the sentence needs "between adjacent literals".
  Counts measured vs claimed: cli.py 13, whole src/fleet 16 (13/2/1), text 15 — EXACT match.
  *** CRITICAL, docs/SPEC.md:6969 — THE SIXTH NARROWER SUCCESSOR, and it exposes a LIMIT IN THE
  MECHANISM: "stop at the first phase whose evidence holds, because the phases below it are covered
  by it" sits SEVENTEEN LINES BELOW the clause FIVESITE corrected, and contradicts it. _HARD_STOPS
  breaks first (reentry.py:98-99). A reconciler following it rebuilds exactly the walk FIVESITE's own
  mutation shows returns SCAN instead of VERIFY/BUILD.
  WHY EVERY SWEEP MISSED IT: the sweeps — and BIND's Layer A census — anchor on the QUANTIFIER
  PHRASE. This site states the same rule in DIFFERENT WORDS, so it is invisible to a phrase-anchored
  search no matter how wrap-aware. Wrap-awareness was the wrong axis; semantic restatement is the gap. ***
  IMPORTANT: (1) DECISIONS.md:7333-7334 states the same stop condition with the same omission.
  (2) schema.sql:286 / SPEC.md:4084 ship "UnmergedDependency (no literal anywhere in src/)" while
      settings.py:704 HOLDS the literal — the COMMIT MESSAGE got it right and the SHIPPED TEXT
      narrowed it, a narrower successor between message and artifact.
  (3) INTEGRATION_HONESTY.md:4063 quotes a phase_floor docstring sentence that is NOT in reentry.py —
      the "never quote another module's text" rule, live again.
  (4) DETECTOR's +-tolerance sentence needs "between adjacent literals".
  (5) DETECTOR's "no failure on a purely semantics-preserving reformat" is OVERBROAD: a comment
      interposed inside _INSERT_FINDING's concatenation, and INSERT INTO [findings], both hard-fail
      the reverse branch. Fail-safe, but layout-triggered.

RL1 lane: complete (a3ff0ae impl+tests; 431b02f one more test; 29 passed / 116 scoped; mypy strict clean).
  §5's decomposition as FOUND (7 subtasks, recorded here so round D need not re-derive it):
    R1 (S, no deps) resizable limiter primitive in budgets.py; retype Limits.llm/for_tier  [DONE]
    R2 (S, no deps) TransportError carries retry_after_s; all four backends populate it
    R3 (M, <-R2)    429 stops producing TierUnavailable; branch on exc.trigger, emit on_throttle. Closes D55
    R4 (M, <-R1)    widen acquisition into LadderModelClient, delete classify.py:161-162
    R5 (M, <-R1,R3) the AIMD controller; emits the rate_limited event
    R6 (M, <-R3)    per-target rpm/tpm token bucket
    R7 (S, <-R3,R5) narrow the DOWN vocabulary, reconcile SPEC, close D55
  Implemented R1 ONLY — scope discipline held. ResizableLimiter (counting, FIFO, resizable while
  held) + retype. Nothing wired.
  Seven mutations, OLD always the PRE-EXISTING test_limits_key_llm_semaphores_by_tier_not_by_backend,
  each proven to change the file via numstat before results were read; all NEW-fail.
  *** M7 INITIALLY DID NOT DISCRIMINATE — charging at resume PASSED the contention test, so the
  lane's own "pinned by tests" docstring was FALSE. It caught this itself and 431b02f adds a test
  constructing the two-releases-no-await window and stepping an arrival's coroutine by hand; M7 then
  failed. Rule 12 self-applied, and the exact failure the rule exists to catch. ***
  ADR-0083 ALLOCATED for R1's decision (hand-rolled limiter; anyio deliberately NOT declared).
  OPEN ASSUMPTION to settle in R5: the limiter's ceiling defaults to STARTING capacity, so a lowered
  concurrency_override cannot be grown back — differs from R5's literal
  [aimd.floor, concurrency.llm.for_tier(tier)]. `ceiling=` is a constructor arg if R5 disagrees.
  NOTE: the anyio ruling AGREED with §4.3 and its escape hatch did not fire — no argument against it.
  HONEST: R1 is a runtime NO-OP until R4/R5. If §6.3's R2+R3-only path is taken, REVERT this commit
  rather than leave dead code.
  Also confirmed independently: cli.py:10074 still says "All twelve workers" (committed, NOT the
  sibling lane's edit) — left for R4.
ADR ledger: 0078 · 0081 · 0082 taken · 0079/0080 RESERVED (step-5 subtasks 9/10) · 0083 = R1 limiter.

CHECKPOINT lane: complete (22ac0cf, docs/PROGRESS.md §39, +160 lines; DONE_WITH_CONCERNS).
  Commit count 37, via `git rev-list --count 6a5e534..HEAD` cross-checked against
  `git log --oneline | wc -l`. It MOVED THREE TIMES while the lane worked (35 -> 36 -> 37); §39 is
  anchored at b1de36e and SAYS IN ITS OWN TEXT that the figure may already be stale. Correct handling.
  SUBTASK STATE, verified by git grep at HEAD, not from my ledger: 3 done (1, 2, 6), 1 in flight (3),
  6 not started. demote() now has a REAL caller (repository.py:1438), closing §38 item 7; neither
  phase_floor nor demote_to_floor has one yet. Subtask 3 has +251 UNCOMMITTED lines, container.py
  UNTOUCHED, 0 commits.
  NARROWER SUCCESSORS: it refused my flat "five or six" and carried THREE ATTESTED plus THREE
  INFERRED. #5 is attested in a COMMITTED doc (d0de310); #6 is review-7's own title and was CLOSED at
  08ba8e2 mid-draft. #1-#3 carry no ordinal anywhere — my ledger's numbering starts at "4th", i.e.
  I invented the earlier ordinals. Plus two unnumbered, a seventh at b1de36e inside a REVIEWER'S fix
  suggestion, and LESSONS' six in its own text.
  *** ITS OWN RE-READ FOUND NINE OVERCLAIMS IN ITS OWN DRAFT. The three that mattered: "every count
  was re-measured" sitting beside a sentence conceding several were not; a CITED COMMAND IT NEVER RAN
  (sed -n '6969p'); and — most valuable — it REFUTED an inherited review-7 finding. The claim that a
  quotation attributed to phase_floor's docstring is not in the file is WRONG: it is at
  reentry.py:80-82, and the review had measured a DIFFERENT docstring. It would have shipped a
  "defect" that was not one. I had already routed that finding to FIX7; FIX7 independently reached
  the same conclusion and narrowed the claim at b1de36e instead of implementing it. Two lanes,
  independently, declined to fix a non-defect. ***
*** CONTROLLER POLICY DEFECT, found by CHECKPOINT: MY CENTRAL ADR ALLOCATION CREATED HOLES.
  Verified: docs/DECISIONS.md has 79 headings and runs ...0077, 0078, then jumps to 0082.
  0079/0080 were reserved for step-5 subtasks 9/10 (not started); 0081 was allocated to the reap lane,
  which was told to draft to the workspace and has not committed; 0083 was allocated to RL1, which
  correctly declined to take it. Central allocation stopped the round-B collision but a number
  allocated to a lane that then does not write it leaves a GAP a reader cannot distinguish from a
  deleted decision. FIX: reservations must be VISIBLE IN THE FILE, not only in my ledger.
  -> queued behind FIX7 (which owns DECISIONS.md): write ADR-0083 (RL1's decision is landed at
     a3ff0ae, so it is writable now) and record 0079/0080/0081 as explicitly RESERVED entries. ***
  ALSO FLAGGED: the ledger omits four things that happened, two with commits on main; §38's thirteen
  carried items are marked INHERITED, not re-verified; no full suite has run against any of the 37.

FIX7 lane: complete (08ba8e2 D1, b1de36e D4, e4c1004 D3, 27cb03b Guardrail-6 self-fixes; 54 passed).
  Floors measured across five configurations, incl. the negative control: a walk re-implemented
  WITHOUT the _HARD_STOPS break returns SCAN for BUILD DEGRADED and TRANSFORM SKIPPED.
  D2 SEMANTIC-AXIS SWEEP, two passes because pass 1's subject list is ITSELF A VOCABULARY BET:
    pass 1 subject x predicate co-occurrence in a +/-280-char window -> 119 raw / 105 distinct sites;
    pass 2 subject-FREE concept phrases -> 58 sites. Result: 2 defects (both already named), 0 further
    wrong statements, 3 reported-not-edited, 100 correct sentences matched and NONE edited.
  D3 EXTENDED, NOT DISCLOSED — Layer D: a sentence binding a stop verb to a stop CONDITION must name
    a hard stop. 2 considered / 2 flagged pre-fix; 2 / 0 after. *** It MEASURED two wider variants and
    REJECTED both: wide = 6 false positives on a correct tree; wide + case-insensitive = SILENT ON THE
    CRITICAL DEFECT, because the sentence contains the ordinary English word "skipped". A checker made
    more permissive became blind to the very defect it was built for. *** Discriminators: old file
    8 passed / new 1 failed on the same tree; re.IGNORECASE reopens the hole.
  ITS OWN TWO-AXIS RE-SWEEP FOUND THREE OVERCLAIMS IN ITS OWN CORRECTIONS (27cb03b): it had widened
    "an excluded phase can never hold evidence" to include DEGRADED (false — it RAN, and
    evidence_holds does not exist yet); called a hard stop below the frontier "a healthy interrupted
    run"; cited :80-82 where the sentence ends at :81.
*** TWO REVIEW FINDINGS WERE THEMSELVES WRONG — the round's last big lesson. review-7 I-7's premise
  is false (the quotation IS in reentry.py:80-81, phase_floor's docstring, differing only by a capital
  T; the reviewer measured the MODULE docstring) and CHECKPOINT reached the same refutation
  independently. I-6's proposed REMEDY is also false (schema.sql is under src/), so FIX7 used the
  commit message's src/**/*.py scope instead. I ROUTED BOTH TO A LANE AS FACT.
  => STANDING CORRECTION TO MY OWN PRACTICE: reviewer claims need the same verification standard as
  implementer claims. I have been forwarding review findings to workers as established; two were not.
  Both were caught only because the receiving lanes verified before implementing. ***
  Concern: _EXPECTED_STOP_CONDITION_SENTENCES = 2 is a NEW way a docs edit can fail the suite —
  intended, and it fails loudly.
STILL OPEN, three rounds old: cli.py:10073 / :10367 ship the retracted algorithm to OPERATORS.
  Blocked on the reap lane, which still holds cli.py with 0 commits.

ADRFIX lane: complete (f76da41, docs/DECISIONS.md only, +171).
  ADR-0083 written from the LANDED decision (a3ff0ae/431b02f), verified against code not the report:
  anyio measured installed (4.14.2) but undeclared in pyproject.toml, ZERO uses in src//tests/, and
  per-borrower (would raise on double-acquire). Recorded honestly: R1 is a runtime no-op today
  (`grep -rn '\.resize(' src/` -> zero hits) with the implementer's own revert-rather-than-dead-code
  recommendation; and the lowered-concurrency_override ceiling assumption flagged as a LIVE
  DISAGREEMENT with R5's text, explicitly not adjudicated unilaterally. Correct restraint.
  Reservations 0079/0080/0081 added IN NUMERIC ORDER between 0078 and 0082 — an initial draft
  appended them after 0082 and the lane CAUGHT AND FIXED IT BEFORE COMMIT.
  Sequence verified continuous ADR-0001..0083 by a gap-DETECTING command, not by eyeball:
    grep -oP '(?<=^## ADR-)\d{4}' docs/DECISIONS.md | awk 'NR==1{p=$0;next}{if($0!=p+1)print "GAP:",p,"->",$0;p=$0}'
  -> no output. My allocation-gap policy defect is closed.

REAP LANE (subtask 3) — CONTROLLER ESCALATION at 2h30m:
  State measured: +251 uncommitted insertions in src/fleet/cli.py, ZERO commits,
  src/fleet/sandbox/container.py UNTOUCHED, no reply to my earlier status request.
  Actions taken: (1) instructed it to run scoped tests and COMMIT the cli.py work by explicit path
  even if incomplete, with an honest message — a partial reviewed commit beats 251 lines living only
  in a working tree while ten lanes landed around it; (2) SCOPE REDUCED — container.py withdrawn and
  to be routed to its own lane, so the ContainerSandbox.reap equality-vs-prefix Critical is not
  blocked behind this lane; (3) told it "stuck" is an acceptable answer and silence is not.

CR-8 review of RL1 + LESSONS + CHECKPOINT: 0 Critical, 2 Important, 5 Minor. All three approved.
  ResizableLimiter judged SOUND under contention and cancellation. FIFO is real (_wake_next skips
  done() futures; the finally removes before the except runs, so a recovery cannot re-select its own
  future). The fast path cannot barge — every headroom-creating path charges SYNCHRONOUSLY with no
  intervening await. FUZZ: 20 tasks, random resize+cancel, 800 seeds -> 0 over-admissions, 0 leaks,
  0 orphaned waiters, AND THE INSTRUMENT WAS VALIDATED (M1 fires 800/800, M6 deadlocks) rather than
  trusted. mypy strict clean across 115 files; 29 tests pass.
  *** I-1 IMPORTANT, budgets.py:1035-1039 — a REAL concurrency defect: the woken-then-cancelled
  recovery calls _wake_next() UNGUARDED, unlike release() at :1046. Reproduced: shrink to 1 with 2
  borrowed, cancel the woken waiter -> the slot is passed on, keeping 2 in flight over a HALVED
  ceiling. Latent today; LIVE the moment R5 lands, and R5's 429 path is exactly where it fires.
  One-line fix. -> lane LIMFIX. ***
  M7 spot-check REPRODUCED: numstat read first (1/1); OLD passes; the contention test passes
  (non-discriminating, exactly as RL1 disclosed); the new test fails. Deleting its bookkeeping assert
  shows the hand-stepped arrival.send(None) discriminates INDEPENDENTLY — both halves earn their place.
  RL1 dead-code honesty upheld: Limits.create DOES construct it, so it is live-but-inert; resize()
  has no caller in src/. Recorded in three places. Nothing half-wired.
  §39 MEASURED VS CLAIMED — 37 commits at b1de36e, 21/16 split, +2,998/-25, 3/10 subtasks done with
  no caller for phase_floor/demote_to_floor, 11 workers, thinking->1, 79 ADRs, D70/D71, 2 worktrees:
  ALL REPRODUCE. No suite claimed.
  I-2 IMPORTANT: four figures (PROGRESS.md:5853, :5982, :6008) were measured at 08ba8e2, NOT the
  stated anchor b1de36e — 566/1,385 (really 593/1,412), "36 commits" (37), "+2,970" (+2,998).
  This FALSIFIES §39's own measurement-window sentence. -> lane FIG.

FIG lane: complete (51815bd, docs/PROGRESS.md only).
  Four figures re-measured INDEPENDENTLY at the stated anchor b1de36e, each with its command recorded,
  none copied from the review: 566 -> 593; 1,385 -> 1,412 (593+372+318+129); 36 -> 37 commits
  (`git rev-list --count 6a5e534..b1de36e`, cross-checked against `git log | wc -l`); +2,970 -> +2,998
  (`git diff --shortstat 6a5e534..b1de36e -- src/ tests/`).
  IMPORTANT NUANCE the review did not state: the section HEADER's own +2,998 clause was ALREADY
  CORRECT — only the two later echoes and the "36 commits" were stale. A blanket "fix the figure"
  would have edited a correct number.
  §39 SWEEP BEYOND THE REPORTED SITES: 28 further measurable claims checked, 24 reproduced exactly
  (the 21/16 split, 11 workers, ADR and D-number counts, worktree list, git log -S"floor",
  grep thinking -> 1, SPEC phrase closure, the reentry.py:80-82 quote, the 27/39/19 finding-kind
  figures, 5-of-11 workers touching ctx.llm — INCLUDING ruling out contracts.py as a grep
  false-positive BY READING ITS TEXT — and the 9m13s timestamp delta).
  1 figure legitimately UNREPRODUCIBLE and correctly left alone: CONSIST's 19/21 hand count at
  8ea1881~1, because the mechanised test module did not exist at that commit. Already disclosed
  in-text as hand-agreed.
  Its own re-read found NO narrower successor, and it VERIFIED THE TWO DISCLOSURE CAVEATS (drift,
  no-full-suite) are BYTE-IDENTICAL to before — i.e. it proved it had not quietly weakened the very
  honesty statements it was working around. That check is worth copying.
  Reported not fixed (out of lane): review m-4, predicate-mixing in item 18's budgets.py churn number
  — independently confirmed reproducible under EITHER predicate, so a different defect class;
  m-2/m-3 in CLAUDE.md; I-1 in budgets.py (LIMFIX owns it).

MINORS lane: complete (3e91a43). CLAUDE.md 123 -> 123 lines (wc -l) — the no-growth constraint held.
  m-2 CLAUDE.md:120's docs/SPEC.md:5671 cite was stale; the line DRIFTED TWICE THIS ROUND
    (5671 -> 5733 -> 5734). Re-anchored on SENTENCE TEXT instead of a line number — the durable fix.
  m-3 CLAUDE.md:68 said "the harness" for mutation aborting, IMPLYING AN EXISTING MECHANISM. The lane
    confirmed no committed mutation harness exists in tools/, src/ or tests/ and reworded to "your
    mutation harness must abort…". CLAUDE.md was asserting infrastructure the repo does not have.
  m-4 PROGRESS.md item 18 predicate mixing: restated under ONE predicate (--numstat), command cited
    inline, re-measured (+131/-4, +319/-0).
  m-5 three `..HEAD` commands inside a section with a stated fixed anchor, re-pinned to
    6a5e534..b1de36e; the lane FOUND AND FIXED A SAME-CLASS SIBLING at open item 14 the review never
    named, and left two other HEAD mentions alone as correct.
  Its own re-read caught a narrower successor in its own draft: the m-4 command lacked a path filter,
  so run literally it returns 27 files instead of the 2 cited. Fixed before commit.
  m-1 and I-1 correctly declined as not-my-file (budgets.py / tests/, owned by the live LIMFIX lane).

CONTAINER lane: complete (aa16846, container.py + tests/test_sandbox.py; 117 passed, ruff+mypy clean).
  *** THE DEFECT IS WORSE THAN RESEARCH REPORTED. Not "a live build container can be force-removed"
  but ZERO LIVE CONTAINERS ARE SPARABLE — the sets are DISJOINT BY CONSTRUCTION. live_names can only
  carry sandbox_name(run_id, repo, attempt), the sole identity derivable from a `phases` row, while
  _invocation_name names every docker run <sandbox_name>-t<8 hex>[-cc-probe]. Driven through a fake
  emulating docker's real regex filter: 4/4 candidates reaped INCLUDING BOTH LIVE ONES. ***
  Predicate chosen: claims() — equal, or extending the live name by a `-`-DELIMITED segment. The
  separator IS the boundary: a bare startswith lets live attempt 1 spare ...-10-t<token> forever.
  Rejected matching -t<hex> as tighter but coupling the sandbox layer to a worker convention.
  ERRED TOWARD SPARING, deliberately and with the asymmetry stated: a crashed and a live same-rung
  container are indistinguishable from a `phases` row, so both are spared — that leaks disk with
  three backstops (run()'s finally, on_cancel, the next idempotent sweep), while the other direction
  destroys a build unrecoverably.
  M1 (claims() -> `name in live`): new tests fail, EVERY PRE-EXISTING reap test passes. M2, M3 each
  fail exactly one targeted test. Each mutation diffed against a pristine copy first.
  Three-way validation complete. SAFETY PROVEN THE RIGHT WAY: reap now re-checks run_prefix LOCALLY
  (docker's filter is daemon-side regex), and the test uses A REAL GIT WORKTREE LITERALLY NAMED
  wt-WT1-example surviving reap(live_names=set()) — the protected artifact as its own test fixture.
  Verified intact at 4b22f3b.
  worktree.py sweep: N/A and NOT edited — no worktree ever carries a suffix (create/path_for use
  sandbox_name verbatim) and its reap already had the prefix floor. Correct restraint.
  *** CONCERN FOR THE STEP-2 LANE: OrchestratorContext.worktree returns work_dir/<repo_id> with NO
  fleet-<run_id>- prefix, so worktrees on disk may sit OUTSIDE the namespace WorktreeManager.reap
  sweeps. Committed, pre-existing, squarely in the step-2 caller's path. Flagged, untouched. ***

LIMFIX lane: complete (d44b94f fix + 2 tests; 05571c9 review-8 m-1, kept separate so it reverts alone).
  Over-admission REPRODUCED standalone pre-fix: capacity 2, both held, 2 waiters; release once (slot
  charged to `first`, unresumed), resize(1), cancel `first` -> `second` admitted with 2 IN FLIGHT
  AGAINST A CEILING OF 1. Post-fix: no breach, and the withheld slot still goes out on the drain
  rather than being swallowed — both directions checked.
  Discriminating mutation done PROGRAMMATICALLY with `assert s != before` and the mutated lines
  printed, proving the file changed (56 bytes): the same-recovery-line neighbour
  test_a_waiter_cancelled_after_being_woken_hands_its_slot_on PASSED, as did 3 other neighbours;
  only the new test FAILED. Textbook old-passes/new-fails.
  Sweep of ALL THREE _wake_next() sites: :1046 acquire cancel-recovery UNGUARDED (the defect);
  :1055 release guarded; :1067 resize guarded by the while short-circuit. The only other charge site
  (acquire fast path) is guarded by locked(). The other 4 CancelledError blocks in src/ hand no
  resource to a waiter. mypy --strict clean, 115 files, before and after, with an apples-to-apples
  baseline proving zero errors added.
*** THE ROUND'S DEEPEST FINDING — A VALIDATED INSTRUMENT WAS STILL BLIND. CR-8's fuzz (20 tasks,
  800 seeds, random resize+cancel) reported 0 over-admissions AND was validated: a known-bad mutation
  fired 800/800. It was still a FALSE NEGATIVE on this defect, and LIMFIX reproduced why: `borrowed`
  is IDENTICAL before and after the over-admission, so the measured quantity cannot see it, and a
  driver that awaits per-op can NEVER REACH the window. LIMFIX's replacement uses bursts plus an
  ADMISSION instrument: validated 68/400 known-bad, 0/400 fixed, 400/400 synthetic fault.
  LESSON: validating that an instrument CAN fire proves only that it can fire on the fault you
  injected. The QUANTITY MEASURED and the DRIVER SHAPE each independently bound what it can ever see.
  Guardrail 6's three-way check does not cover this; it needs the fourth question "could this
  instrument observe THIS defect at all?" ***
  CONCERN NEEDING A RULING: _wake_next charges UNCONDITIONALLY and pushes the ceiling check onto its
  callers — precisely the shape that produced this defect. R5 adds callers. -> lane WAKESHAPE,
  ADR-0084 ALLOCATED.
  SECOND SCRATCHPAD COLLISION THIS ROUND: a sibling lane OVERWROTE this lane's script in the shared
  scratchpad and it briefly read the sibling's output as its own. First incident was a deleted
  worktree; this one is silent cross-contamination of RESULTS. -> per-lane scratch subdirectories
  must be a standing rule.
ADR ledger: 0078/0081/0082/0083 taken · 0079/0080 reserved · 0084 = _wake_next charge/check shape.

CLAUDE2 lane: complete (0214840; CLAUDE.md 123 -> 124 lines).
  Lesson 1 (validated-but-blind instrument) placed as a new bullet INSIDE Guardrail 6, not as a new
  top-level guardrail. REASON, and it is a good one I had not considered: the lane found ~80 EXTERNAL
  CITATIONS of specific Guardrail NUMBERS across docs/DECISIONS.md, docs/PROGRESS.md and
  docs/superpowers/plans/*.md — all files it was forbidden to touch — so renumbering would have
  SILENTLY BROKEN citations it could not repoint. My brief had suggested splitting the section;
  splitting was the wrong call and the lane was right to decline it. Net +1 line.
  Lesson 2 (scratch isolation) SHARPENED the existing worktree bullet in place, broadened to cover
  every scratch file and script output, with a falsifiable check (per-lane subdirectory, never a
  shared generic filename). Net 0 new lines.
*** CORRECTION TO MY OWN RECORD, caught by CLAUDE2. I wrote above — and said so to the user — that
  CR-8's fuzz "reported 0 over-admissions" and that the blind quantity was `borrowed`. Checking
  review-8.md:74-86, CLAUDE2 found its shrink-including runs were LEAK-CHECKED ONLY (over-admission
  was not tested there) and the exact quantity it measured is NOT independently confirmable from the
  review. The defensible statement is: CR-8's fuzz REPORTED CLEAN, and the two-reasons analysis of
  WHY it could not have seen the defect is LIMFIX's own investigation, not an established fact about
  CR-8's internals. The lesson in CLAUDE.md is unaffected — LIMFIX reproduced the blindness directly
  — but my attribution was stronger than the evidence. Amended here; the ledger entry above stands
  corrected by this paragraph. ***
  Its own re-read caught that same overclaim in its draft before commit.
  Flagged for a future round: the new bullet is dense (bundles narrative with checklist); if
  Guardrail 6 is ever split, the citation-repointing cost across ~80 sites must be budgeted.

REAP LANE (subtask 3): STOPPED BY CONTROLLER at ~3h, 0 commits, 3 unanswered status requests.
  Its final transcript line was "Now a quick smoke run of the existing resume tests before adding new
  ones" — so it was STILL WORKING, not deadlocked; it simply never processed any of my three messages
  (they queue for the next tool round, and it evidently ran long stretches without one). Judgement:
  the lane was slow, not stuck, and my messages never reached it in a usable window. That is a
  limitation of mid-flight messaging I should not have relied on three times.
  ITS WORK IS NOT LOST: +251 insertions to src/fleet/cli.py remain in the working tree and are
  captured at docs/superpowers/plans/design-resume-step5-reap-uncommitted.diff.
  -> lane REAP2 takes over from the working tree.
  LESSON for round D: a lane with no commit after ~45 minutes should be asked to commit a WIP, and if
  a second check finds still nothing, taken over. Three hours of unreviewable work in a shared
  checkout blocked five queued items and forced a scope withdrawal (container.py) that another lane
  then did in 10 minutes.

WAKESHAPE lane: complete (99862a9 shape+tests, 3fe9ed7 ADR-0084, ac7786c docstring sweep).
  _wake_next -> _drain (budgets.py:1074-1095): the charge at :1091 is reachable ONLY under the
  `while self._borrowed < self._capacity` test at :1088. All three call sites become a bare
  self._drain() — INTENT, not precondition. It also closes a mirror hazard (a caller waking one where
  headroom was two). Behaviour preserved and argued: resize is exactly the old loop; release and the
  recovery still transfer one, because with waiters present the first charge restores
  _borrowed == _capacity. FIFO and the synchronous charge untouched. Rule 2 satisfied — the change
  REMOVES code (three caller-side tests -> one; the bool return is gone).
  Instrument measures the ADMISSION EVENT (the ceiling in force at the instant a parked waiter's slot
  is charged and its future settled) — explicitly NOT `borrowed` (blind: reads 2 either side of the
  breach) and NOT resume-time (false-positives on a post-wake shrink). It also REFUSES TO RUN BLIND
  if waiters pre-exist the window; the lane's own first draft made exactly that mistake.
*** THIRD-ORDER INSTANCE OF THE INSTRUMENT LESSON, and the round's most alarming single finding:
  the OLD instrument overrode `_wake_next` by name. The rename to `_drain` SILENTLY DISARMED IT and
  THE FUZZ PASSED VACUOUSLY — 33 green over a rewritten class. A test whose instrument hooks a
  private method by name is disarmed by any refactor that renames it, and NOTHING IN THE SUITE
  CATCHES THAT. The replacement hooks the loop's create_future so it survives the refactor and
  mutations reversing it. -> lane INSTRSWEEP dispatched to find the class across tests/. ***
  M1 (the shape alone, behaviourally identical to d44b94f): 32 passed / 1 failed — only the new
  guarantee, "the gate charged a slot it had no room for: [(3, 1)]". Both of d44b94f's tests pass
  under it. M2 (literal pre-d44b94f) reported as NOT discriminating but used to validate the new
  instrument on the known-bad state: 71 events / 68 of 400 seeds; M3 synthetic fault 3,531 / 400 —
  BOTH REPRODUCING THE LIMFIX LANE'S INDEPENDENTLY-HOOKED FIGURES EXACTLY. Two lanes, two hooks,
  identical numbers.
  mypy --strict clean, 115 files, identical before/after.
  Concern: docs/PROGRESS.md's ledger line for ADR-0084 wants flipping to "taken" (outside its lane).

REAP2 lane: LANDED e915b93 "cli: land §11.5 step 2 (orphan reap) from an inherited working tree,
  corrected" — subtask 3 is COMMITTED and the working tree is CLEAN for the first time in ~3h.
  Full report pending; the takeover was the right call.

LINT lane: complete (cf85c8b; tests/test_budgets.py + tests/test_reentry_floor.py; 74 passed).
  Both errors re-measured before fixing. E501 fixed by WRAPPING ONLY — assertion text byte-identical,
  before/after quoted in the report, so the concurrency diagnostic keeps its information.
  C420 -> dict.fromkeys VERIFIED SEMANTICALLY SAFE by checking the value expression: it is an
  immutable bool, so one shared object is identical to the comprehension's behaviour. This is the
  check that matters — dict.fromkeys would have been WRONG for a distinct mutable per key, and the
  auto-fix would have applied it silently.
  Found and correctly did NOT fix: 6 S603/S607 in tests/test_cli.py, which git status shows as a live
  lane's uncommitted work. Reported, out of scope. -> relayed to REAP2 as a lint regression to
  resolve before its final commit, with an explicit noqa-with-reason allowed but "leave it red" not.
STATE CHECK: ADR sequence continuous 0001..0084 EXCEPT 0079/0080/0081 which are explicit RESERVED
  placeholders. 0081 is REAP2's to replace with the real ordering-departure ADR; relayed.

REAP2 lane: COMPLETE — SUBTASK 3 IS DONE (e915b93 reap, 0b0db5c 8 test cases, 3440b12 comment,
  ead96e6 ADR-0081). tests/test_cli.py 104 passed, test_findings_kinds.py 4 passed, ruff clean,
  mypy clean on cli.py.
  VERDICT ON THE INHERITED 251 LINES: PARTLY KEPT — skeleton kept (payload shape, per-entry `failed`
  printing, dry-run split, attempts + 1), FOUR replacements. The serious one: the container half
  claimed ContainerSandbox.reap spares by exact membership — FALSE since aa16846 — so it listed
  containers itself and passed a pre-computed spared set. That restated logic claims() already has
  AND opened a window it does not have: a container started BETWEEN the caller's listing and reap()'s
  own would be force-removed with a live row claiming it. Also fixed _reap_lines printing "no orphan
  worktrees" ABOVE two FAILED lines.
*** CONTROLLER ERROR #4: my finding-1 ruling (live_names from status='RUNNING' after the sweep) FAILS
  UNDER --dry-run, which never runs the sweep — so status='RUNNING' alone previews sparing every
  crashed run's own orphans. The lane kept my ordering AND added the negated predicate. It also
  restored `lease_owner IS NOT NULL`, which my earlier relay had it drop; dropping it narrows "live",
  which is the DANGEROUS direction. Four rulings wrong this round, every one caught by a lane
  checking the code rather than building around it. ***
*** FINDING 4 HOLDS AND IS WORSE — the worktree half of step 2 is a CORRECT SWEEP OVER AN EMPTY
  NAMESPACE. Two independent mismatches: (a) OrchestratorContext.worktree has no `fleet-` prefix
  (context.py:206-208); (b) clone cuts worktrees INSIDE the per-repo mirror (clone.py:397-407), so
  WorktreeManager interrogates a git dir that never registered them. WorktreeManager is constructed
  nowhere else in src/. The CONTAINER half works. Needs clone.py + context.py — correctly reported,
  not reached across. -> D72 ALLOCATED. ***
  ITS OWN FOURTH-QUESTION ANSWER, and it is the right kind: "my container test could NOT observe its
  defect." M4 passed — the quantity (removals from a static inventory) does not move, because the
  defect lives BETWEEN two listings. It added case 8 to open that window; M4 now fails it. A lane
  applying the new CLAUDE.md rule to itself and finding its own test blind.
  CONCERNS: container.py:356 list_by_prefix returns [] on non-zero exit — the SAME error-collapsing
  class as D44 -> D73 ALLOCATED, fix it. The resume tests were HITTING THE REAL DOCKER DAEMON (now
  patched autouse) — worth its own note. `fleet gc` still reaps nothing.
D ledger: highest existing = D71 (verified across INTEGRATION_HONESTY.md and PROGRESS.md).
  D72 = worktree-namespace mismatch (step 2's worktree half sweeps an empty namespace).
  D73 = container.py:356 list_by_prefix collapses a non-zero exit to [].

CR-9 review of the concurrency/sandbox wave: 0 Critical, 1 Important, 3 Minor, 2 Nits. All accept.
  "Could any test survive its subject being deleted? NO." Eight mutations, each proved non-no-op
  BEFORE its result was read, incl. two of the reviewer's own: a worktree-floor mutation (the wave's
  only swept-but-unmutated subject — it went red) and an INSTRUMENT-DISARM mutation rewriting
  acquire's loop.create_future() as asyncio.Future(loop=...). That last FAILS LOUDLY at
  test_budgets.py:1116 => the rename-disarm class that killed the last two instruments is now caught
  for this instrument. The reviewer closed the hole it was asked to look for.
  _drain BEHAVIOUR-PRESERVATION: HOLDS, and PROVEN not asserted — a 600-seed DIFFERENTIAL against
  99862a9^ on an RNG-only op stream: 0 divergences in admission trace, entry order, and final
  borrowed/capacity, WITH A CONTROL showing 25/600 divergence on the known-bad. Zero waiters,
  grow-by-3, and shrink-3->1-with-3-holders probed directly. FIFO, done()-skip,
  finally-before-except, and no-await-between-headroom-and-charge all survived. It also upgraded the
  ADR's PREMISE to an INVARIANT: waiters are appended only under locked() and every decrement or
  capacity move drains synchronously, so _borrowed >= _capacity - 1 on entry from release/recovery.
  *** INDEPENDENT REPLICATION: it reconstructed the synthetic fault FROM A ONE-LINE DESCRIPTION,
  BEFORE READING ANY LANE'S SCRIPT, and got 3,531 events — matching exactly. Also 68/400 seeds,
  71 events known-bad, 0/400 swept. Three lanes, three hooks, identical numbers. ***
  IT CORRECTED ITSELF IN SITU: its first draft of I-1 said "the finally does not exist" — WRONG,
  it had grepped the wrong run(). Corrected before submission and container.py:284 confirmed correct.
  I-1 IMPORTANT: the container leak's SOLE stated justification names ContainerSandbox.run()'s
    `finally` as a backstop — that method has ZERO callers in src/ and is not on the path
    (buildverify builds its own argv at :792/:1133), while the two paths that CAN fire
    (buildverify.py:829/:958, deadline-kill only) go UNNAMED. The justification cites the wrong
    backstop. -> queued behind the live HONEST lane, which owns container.py.
  m-2 MINOR BUT REAL: slug() maps `/` -> `-`, so a live `acme/commons` attempt 1 spares repo
    `acme/commons/1` FOREVER. The CONTAINER lane's "disjoint by construction" covers the ATTEMPT
    segment only — the repo-id segment can collide. -> queue with I-1.
  m-1: the fuzz never asserts it recorded anything (4,111 admissions live today) — a vacuous-pass
    guard it lacks. m-3: failed `docker ps` -> complete=True on an empty sweep — that is D73, already
    being fixed by the live HONEST lane.

INSTRSWEEP lane: complete (d2e0090, new tests/test_instruments_are_armed.py; no src/ change needed).
  CLASS MEASURED shape by shape at ead96e6, each with its command: subclass overrides 18 classes /
  34 methods, ZERO private (1 at 99862a9^); monkeypatch.setattr 83 sites, 7 naming a private symbol,
  ZERO with raising=False; direct private assignment onto a production object 3; unittest.mock ZERO
  uses in the whole suite; getattr-with-default probes ZERO.
  DEMONSTRATED 1 vacuous pass, SUSPECTED 0 — and demonstrated it properly: restoring
  99862a9^:tests/test_budgets.py over today's src/ gives 31 passed; same M3 fault, same 400 seeds,
  disarmed instrument 1 PASSED, HEAD instrument 1 FAILED. Old-passes/new-fails applied to the
  INSTRUMENT rather than to the code.
  SAFE AND LEFT ALONE: all the rest. Nine renames across src/ produced nine loud AttributeError
  failures — including _drain, the exact rename that disarmed the old fuzz. The mirror-image error
  was avoided: nothing correct was "fixed".
  Nothing needed fixing, so it added THE GATE THE CLASS LACKED: a subclass method absent from its base
  fails; an unresolvable base FAILS RATHER THAN SKIPS; raising=False is pinned at zero; and the single
  exemption SELF-EXPIRES. Validated four ways (fires on known-bad naming Watched._wake_next, silent
  clean, fires on 4/4 injected renames, green under a cosmetic control). An earlier heuristic draft
  was REJECTED ON MEASUREMENT — it missed the import_specifier injection.
  *** ITS FOURTH-QUESTION ANSWER IS THE BEST EPISTEMIC WORK OF THE ROUND. What it can never see:
  an override disarmed with NO rename at all (production keeps the name but stops calling it) —
  reachable by ordinary cleanup, documented as a boundary and deliberately not patched; semantic
  orphaning where the quantity moves; instruments binding no name; a base GROWING a name, which makes
  the checker QUIETER; silent skips; anything outside tests/. And the killer: "§2 is a taxonomy I
  authored — an unlisted shape reads as 'population 0' indistinguishably from 'not considered.'"
  That is the limit of every sweep this round, stated plainly by the lane doing the sweeping. ***
  Concern: the negative result DECAYS; only the gate survives. checked >= 30 is a floor from today's 34.

*** SESSION LIMIT HIT — two lanes terminated mid-work. Repo verified intact: ruff clean, history
  through d2e0090, no stash, wt-WT1-example present. ***
  HONEST (D73 fix + D72/D74 records): died at "Now I'll implement the fix in container.py" but had
  ALREADY WRITTEN +246 lines across src/fleet/sandbox/container.py (+110) and tests/test_sandbox.py
  (+143), uncommitted. Snapshot at honest-uncommitted.diff. -> lane HONEST2 takes over from the
  working tree, same pattern as REAP2.
  WTDESIGN (D72 namespace design): died at its first tool call with nothing written. -> re-dispatch
  clean, no takeover needed.
  APPLYING THE ROUND'S OWN LESSON: the reap lane cost three hours by holding uncommitted work. Both
  takeovers are dispatched immediately rather than left to sit, and both briefs order early commits.

WTDESIGN2 lane: complete (c6bdd26, docs/superpowers/plans/design-worktree-namespace.md).
  ALL THREE LEGS OF D72 HELD, verified at main@d2e0090 — and one was verified by VALUE not by reading:
  a probe under .venv/bin/python showed slug(repo_id) == repo_id for every legal RepoId, so the gap is
  PURELY the missing affixes, not a slug transform. That is the Guardrail-6 "exercise it, don't read
  the declaration" discipline applied to a design question.
   1 name: context.py:206-208 returns work_dir/<repo_id>; reap() filters on run_prefix at
     worktree.py:330.
   2 registry: clone.py:397-407 runs `worktree add` through a Git bound to the MIRROR
     <cache/git>/<repo>.git, while WorktreeManager._git_run always does -C repo_dir =
     run.monorepo_path. Two different git dirs.
   3 constructed once only, at cli.py:10466 (the reap seam). create() is dead code in src/;
     sandbox_name is used elsewhere only for CONTAINER names.
  *** A FOURTH LEG NOBODY HAD REPORTED: Phase 3/4 worktrees (cli.py:7321, :7416) are cut FROM THE
  MONOREPO — the right registry — so a rename ALONE makes them reapable. Their path is also computed
  TWICE INDEPENDENTLY (cli.py:7321 vs context.py:208 via work_dir=build_root) with nothing enforcing
  agreement. That duplication is a latent defect in its own right. -> D74 ALLOCATED, queued behind
  the live HONEST2 lane which owns INTEGRATION_HONESTY.md. ***
  RECOMMENDATION: Option A — run-scope the directory names at the four producer sites and make the
  reap registry-plural (monorepo + each mirror). Main cost: all four producers must move in ONE
  commit, and an ADR is needed for an attempt-free checkout name, which docs/SPEC.md:1408 currently
  FORBIDS — i.e. the SPEC blocks the fix and must move with it (Guardrail 7: two edits, not one).
  7 subtasks; tasks 1 and 6 flagged as needing an ADR -> ADR-0085 and ADR-0086 ALLOCATED for round D.
  3a/3b split for dispatch but MUST land in one commit.
  Honestly could not determine: the cost of N+1 mirror listings at ~250 repos (unmeasured, and
  DELIBERATELY kept out of the ADR rather than guessed), and whether any consumer beyond the three
  payload models persists a worktree path.
ADR ledger: 0078, 0081-0084 taken · 0079/0080 reserved (step-5 subtasks 9/10) · 0085/0086 reserved
  (worktree-namespace tasks 1 and 6, round D).
D ledger: D72 (worktree namespace), D73 (list_by_prefix collapse), D74 (Phase 3/4 worktree path
  computed twice, nothing enforcing agreement).

HONEST2 lane: complete (cfd89c7 inherited D73 fix, 8df4af8 I-1+m-2+boundary test, 9955583 records,
  87ed419 cross-ref). Verdict on the inherited 246 lines: KEPT WHOLE and COMMITTED BEFORE EXTENDING —
  it was not mid-edit; ContainerListing, list_with_verdict, the lenient wrapper, the reap() error
  branch and four tests were all coherent, ruff/mypy/39 tests clean, and it already followed the
  file's `no_verdict` convention. The takeover pattern worked twice today.
  Callers enumerated (3 in src/): reap() now reads list_with_verdict — an error becomes ONE `failed`
  entry, complete=False. buildverify._sweep_containers:1053 and cli's --dry-run :10594 left unchanged
  DELIBERATELY (raising would break a cancellation sweep / a preview), recorded as D73's residual and
  PINNED BY A TEST rather than left as prose.
  Mutation: reap() back on list_by_prefix, with count(old)==1 asserted BEFORE writing and the hunk
  confirmed by git diff -U1 => 2 failed / 38 passed, real-empty-inventory control stayed green.
  FOURTH QUESTION, answered exactly right: the test measures `complete` and `failed` — A VERDICT, NOT
  A COUNT. Under the mutation pytest fails on `assert result.complete is False`, which proves the
  `reaped == []` and no-docker-rm assertions ABOVE it executed and PASSED under the defect. They are
  kept in the test as LABELLED CONTROLS. That is the blindness lesson turned into test structure.
  I-1 verified INDEPENDENTLY (ContainerSandbox.run() has zero src/ callers; all three
  ContainerSandbox( sites use only list_by_prefix/remove/reap; DECISIONS.md:5238 corroborates).
  Docstring now names one general backstop and two conditional. Class-swept: two other sites say the
  phrase CORRECTLY — reported, not edited.
  m-2: NARROWED THE CLAIM rather than tightening the predicate, with the reason — the only tightening
  is matching -t<hex>, which claims() refuses on purpose because it errs in the unrecoverable
  direction. Made executable in test_claims_spares_a_slug_colliding_repo_as_a_stated_boundary.
*** CONTROLLER ERROR #5, and it is this round's own signature failure committed by me: I told lanes
  "highest existing D-number is D71". IT IS D70. My grep matched PROSE REFERENCES to D71 (four
  disclosures saying D71 is free) and I inferred a DEFINITION from a REFERENCE — the precise mistake
  of counting matches instead of distinguishing definition from citation. Verified now:
  INTEGRATION_HONESTY.md defines D54-D70 and then D72-D73; there is NO D71 entry. Consequence: D71 is
  free and a GAP now sits between D70 and D72 — the same hole my ADR allocation created earlier, from
  the same cause. D72/D73 are committed and cited, so renumbering is worse than recording.
  -> lane DNUM records D71's status explicitly and adds D74. ***

DNUM lane: complete (c24e7d2, docs/INTEGRATION_HONESTY.md only, +62).
  *** THE COMMAND THAT MY ERROR LACKED, and it should be the project's standard for any numbered
  register: `grep -n '^\*\*D71\b\|^### D71\b'` — ANCHORED to the document's own entry convention
  (a real entry's line STARTS with `**D<n> —` or `### D<n> —`). A bare `grep D71` matches six
  mid-sentence prose mentions ("D71 is the next free number") and is precisely the mistake.
  Anchored output: EMPTY, exit 1 => D71 has no definition anywhere. Distinguish definitions from
  citations by anchoring on the register's own syntax, not by counting matches. ***
  D74 both legs verified at HEAD: leg A (registry) holds — Phase 3/4 worktrees at cli.py:7321/:7416
  are cut through the MONOREPO Git and WorktreeManager's sole construction site (cli.py:10464-10471)
  also targets the monorepo, so unlike D72's mirror-registered Phase 1/2 worktrees a rename ALONE
  would make these reapable. Leg B (duplication) holds — cli.py:7321 computes build_root/repo_id
  while cli.py:7625 hands work_dir=build_root to OrchestratorContext, whose worktree()
  (context.py:206-208) RECOMPUTES self.work_dir/repo_id. Nothing binds them. Latent only.
  Continuity re-check: unbroken 1..74, highest 74, no gap.
  Its self-re-read caught a stray-quote typo AND a shaky analogy — one I had put in its brief,
  conflating D72/D73's not-renumbered status with the ADR-0079-0081 RESERVED treatment. It corrected
  my framing rather than inheriting it. Sixth time this round a lane has corrected the controller.
OPEN for round D (from HONEST2 concern 1): D73's residual in buildverify._sweep_containers:1053 is
  the path that runs on EVERY BUILD and still swallows a failed listing; it needs buildverify's owner.
  -> lane SWEEPFIX dispatched.

CP2 lane: complete (ec0f205 amendment, f9cb3f9 re-anchor forced by a sixth commit landing under it).
  Anchor c24e7d2. 62 commits from 6a5e534, cross-checked two ways; 25 after b1de36e; 34 touch
  src//tests/; +5,022/-42 across 20 files. The anchor MOVED FOUR TIMES while it wrote
  (d2e0090 -> cfd89c7 -> 87ed419 -> c24e7d2) and the text says so.
  SUBTASKS: 4 of 10 done (1, 2, 3, 6), verified IN CODE not from reports — and the not-started ones
  PROVEN BY ABSENCE: no evidence_holds definition, no phase_floor caller, ResumeIncompleteError still
  raised. Proving absence rather than asserting it is the right standard for a checkpoint.
  All EIGHT existing disclosures survived VERBATIM — asserted by extracting each from
  `git show ec0f205~1:` and confirming presence, plus a difflib dump confirming all 11 hunks fall
  inside §39. That is the "prove you did not quietly weaken the caveats" check, done properly.
  ITS RE-READ FOUND NINE OVERCLAIMS in its own draft (a ruff result quoted at a MOVED anchor;
  "each of four commits" true of three). Re-running the check then caught c24e7d2 falsifying its own
  "D74 is not allocated" — f9cb3f9 fixes it AND switches three ledger citations from LINE NUMBERS to
  HEADINGS. Durable anchors over line numbers: adopt.
  New open items recorded: 18 D72 · 19 D73's residual · 20 review-9 I-1/m-2 (open when it began,
  CLOSED at 8df4af8 mid-amendment) · 21 `fleet gc` reaps no sandbox while run_prefix's docstring says
  it does. Closed: items 1, 2 (Layer D at e4c1004), 4 (ADR numbering repaired, 84 headings, no gaps).
  *** CONCERN THAT NOW GATES THE ROUND: THE FULL SUITE IS 62 COMMITS OVERDUE. No full run has
  happened this round — correctly, since lanes were live throughout and two pytest sessions must
  never overlap. It must run once the last lane is quiet. ***
  Also open: INTEGRATION_HONESTY.md:4133 still cites the MOVED cli.py:10043/:10274; item 21 has no
  owner; the design doc is NOT the D72 fix and must not be mistaken for it.

CR-10 review of REAP2 + INSTRSWEEP: 1 Critical, 1 Important, 3 Minor. REAP2 land-with-correction;
  INSTRSWEEP land. Anchored at 87ed419, all mutations RE-CONFIRMED at 5ed4e47 after two sibling
  commits landed mid-review.
  *** THE KEY QUESTION ANSWERED WELL: nothing in the reap wave claims the worktree half works. The
  commit message, the _reap_orphan_worktrees docstring (cli.py:10491-10507), and ADR-0081's title and
  §4 all state it sweeps an empty namespace AND name the two modules that would fix it. Better still,
  _reap_lines PRINTS THE SEARCHED NAMESPACE on every line, so silence cannot read as cleanliness.
  The round's signature failure — a test passing over an empty namespace — was avoided by disclosure
  built into the output. ***
  INSTRUMENT GATE HOLDS within its stated scope: fires on known-bad (renaming preconditions_hold),
  silent on clean, fires on a synthetic orphan injected into a clean file, green on a cosmetic reflow
  control. `checked` is really 34 against a floor of 30, and breaking the walk to one file gave
  "only 8 examined" rather than a pass — the floor works. The exemption self-expires (adding reset()
  to BaseWorker fails the allowlist test). Its documented blind spot is REAL and reproduced: deleting
  the only `await self.on_cancel(ctx)` call while KEEPING the name leaves all three gate tests green.
  Could any test survive its subject being deleted? NO — stubbing _reap_orphan_worktrees fails
  exactly cases 1-5, stubbing _reap_orphan_containers fails exactly 6-8. Clean 5/3 partition.
  C-1 CRITICAL: ADR-0081 §2 AND the lane report both claim tests/test_cli.py:1745 "fails if the
    negation goes". MEASURED FALSE — neutering NOT (1 ...) -> NOT (0 ...), and the realistic
    delete-plus-drop-params refactor, each pass all 104 tests, because that test builds NO `phases`
    rows so the liveness predicate cannot move it. A throwaway probe (stale RUNNING row + --dry-run)
    DOES catch it => missing coverage, not an unreachable property. An ADR asserting a test binds a
    property the test cannot see.
  I-1 IMPORTANT: BOTH --dry-run preview filters are UNPINNED. Dropping the worktree prefix filter
    (cli.py:10528) and dropping claims() from the container preview (cli.py:10614) each leave 104
    green, while the docstring asserts containment "in EITHER branch". A widened preview would name
    wt-WT1-example or a live build as reapable — AND THE OPERATOR IS THE REMOVAL PATH. The preview is
    precisely the surface where a false positive gets acted on by a human.
  -> both queued behind the live SWEEPFIX lane, which owns cli.py and tests/test_cli.py.

SWEEPFIX lane: complete (5ed4e47 source, b5bbb31 buildverify tests, a65a305 cli test, 2ce3533 format).
  BOTH D73 residual call sites closed. Shape for _sweep_containers: reads list_with_verdict and on
  error emits ctx.log.warning("container_sweep_listing_failed", ...) and RETURNS — never raises.
  It CHECKED THE ALTERNATIVES BEFORE CHOOSING, which is why the choice is defensible: a worker CANNOT
  record a finding (ctx.db is ReadOnlyRepository, ADR-0016), and both run() call sites are
  mid-construction of a WorkerError that is a verdict ABOUT THE BUILD — folding a daemon outage into
  its stderr_tail would MISATTRIBUTE THE OUTAGE TO THE REPO. ctx.log.warning is the fleet's existing
  convention for trouble a component must report and cannot act on (22 sites in src/). Rule 8 applied.
  It also logged the OSError spawn fault (no `docker` on PATH) that contextlib.suppress swallowed whole.
  --dry-run: fixed by REUSING the returned dict's existing `error` key, which the worktree half
  already uses and _reap_lines already prints as "the container sweep did not run", gating the clean
  headline. No new machinery — the reporting shape already existed.
  Mutations both proved applied (byte delta and hunk printed; the script ABORTS on a non-match) before
  results were read. Sweep test fails at the verdict assertion with events() == [], its two removal
  controls having EXECUTED AND PASSED; the preview test fails with the output reading
  "step 2: no orphan containers" VERBATIM during a docker outage — the exact false reassurance.
  FOURTH QUESTION: removals read 0 in BOTH worlds, so no count can see this. Both tests assert on the
  verdict; counts kept as labelled controls; the fixture keeps the orphan in `present` — docker has it
  and cannot say so.
  CONCERNS -> routed: list_by_prefix now has ZERO src/ callers (dead code question), and FIVE
  documents/docstrings still describe the residual as OPEN — container.py, INTEGRATION_HONESTY.md D73,
  PROGRESS.md §19, and two tests/test_sandbox.py docstrings. A narrower gap (remove()'s bool still
  uninspected) is documented in the method, not patched — correct under the stop rule.

CITE lane: complete (18a1fc6, docs only).
  Population measured, not guessed: 811 occurrences / 623 unique `<file>.py:<line>` citations across
  the three records. HAND-PROBED 64 (`git show HEAD:<path> | sed -n '<n>p'` plus grep for the claimed
  text): 42 CORRECT and left alone · 5 stale at HEAD but CORRECT AT THE REF THE RECORD ITSELF NAMES
  (ADR-0078/0082 already say "cite the ref, not the line") — left alone under the mirror-image rule ·
  16 DRIFTED, 15 repaired · 1 UNVERIFIABLE (enums.py:272-299, ADR-0070) — referent not established,
  AND IT DID NOT GUESS.
  It stated exactly what it did NOT check: 467 citations into files this round never touched, all
  FIXED/CLOSED narrative, ADRs 0055-0077, 63 references/-tree paths. Scale judgment made explicit
  rather than a claim to completeness.
  Durable anchors installed (symbol names beside the number, not instead of it):
  FailureClass.BACKEND_UNAVAILABLE · record_attempt · _unavailable + its three calls ·
  stubs_abandon() · resume()/_refuse_unbuilt_resume_flags() · BuildverifyWorker._sweep_containers.
  *** D70 §4 VERDICT: STILL OPEN, not merely mis-cited. `git show HEAD:src/fleet/cli.py |
  grep -n "earliest phase whose"` -> :10078, :10390; identical strings at 6a5e534:10043/:10274.
  It fixed the ANCHOR and left the STATUS untouched — refusing to close a defect as a side effect of
  repairing its citation, which was the trap named in its brief. Routed to the live REAPFIX lane,
  which owns cli.py. This is the one surface the corrected rule never reached: OPERATOR-FACING TEXT. ***
  ITS RE-READ CAUGHT THREE OF ITS OWN OVERCLAIMS — and one of them uncovered that the ORIGINAL D69
  citation was itself mis-aimed (it claimed cli.py:11015@6a41840 was the ABANDONED UPDATE; it is the
  finding write nine lines below). A citation audit finding the record it was auditing had been wrong
  from the start.
  Honest about its own instrument: the mechanical drift map was A CANDIDATE FINDER ONLY — it
  false-flagged all of ADR-0081 and mispredicted :10390 as :10380. Hand-probing is what settled each.
  CONCERNS -> routed: D73's two "unchanged — residual" cells look closed by 5ed4e47 (the RESIDUAL lane
  is already carrying that); D55's "asyncio.Semaphore has no resize API" is OVERTAKEN BY ADR-0083.
  Neither adjudicated — correctly, since closing is not re-anchoring.
  *(2026-08-20: D55 has since been adjudicated at INTEGRATION_HONESTY.md:3405-3440. "OVERTAKEN"
  applies to the PREMISE ONLY and is not a closure: the stdlib class still has no resize API, what
  changed is that `Limits.for_tier` returns ADR-0083's `ResizableLimiter` instead. Verdict there:
  premise corrected, defect FULLY OPEN, 0% closed — `resize()` has no production caller.)*

RESIDUAL lane: complete (f10a863, container.py + tests/test_sandbox.py, +30/-51).
  Residual verified closed IN CODE not from the report: buildverify.py:1088 and cli.py:10605 both call
  list_with_verdict and report failures; `grep -rn '\.list_by_prefix(' src/` returns nothing.
  Class sweep (whitespace-normalised, offset->line map): 3 stale "still open" sites found and fixed —
  container.py's list_by_prefix docstring and two test_sandbox.py docstrings. Rest checked and
  confirmed already correct or historical.
  DELIVERABLE 2 — list_by_prefix DELETED, with the reasoning stated: zero production callers (only 3
  direct test calls), a one-line delegator with no independent logic since re.escape lives in
  list_with_verdict. Rule 2 says delete rather than keep a dead wrapper. Rewired the 3 tests.
  Discriminating mutation: stripped re.escape from list_with_verdict's filter and confirmed the
  REWIRED escaping test still fails — i.e. it proved the escaping property survived the rewiring
  rather than assuming it. 40 passed before and after.
  CONTROLLER CHECK: a Pyright diagnostic claimed `re` was now unused in container.py. VERIFIED FALSE —
  re.escape is live at container.py:428 inside list_with_verdict. Stale IDE analysis, not a regression.
  Checking rather than trusting the diagnostic is what settled it.
  ROUTED: stale "lenient list_by_prefix" prose in 4 unowned files — cli.py and tests/test_cli.py
  relayed to the live REAPFIX lane; buildverify.py and tests/test_workers_build.py -> lane STALEREF.
  docs sites INTEGRATION_HONESTY.md:4233-4242 and PROGRESS.md:6005 reported for routing; the former is
  the live ADJUD lane's, the latter -> STALEREF. It also flagged that ITS OWN test rename invalidates
  test-name citations in both docs — a second-order consequence it noticed unprompted.
  ALSO: ruff is red (1 error) in the live REAPFIX lane's uncommitted tests/test_cli.py — relayed.

ADJUD lane: complete (53e3d47, docs/INTEGRATION_HONESTY.md only).
  *** D55: STILL OPEN — premise corrected, verdict NOT changed, and the three-way distinction it drew
  is the model for this kind of adjudication:
    (a) the SENTENCE is still literally true — asyncio.Semaphore itself is unchanged and still has no
        resize method;
    (b) but the OBJECT at Limits.for_tier (budgets.py:1146) is no longer a Semaphore — it is
        ADR-0083/0084's ResizableLimiter, whose resize() is safe while slots are held;
    (c) and NONE OF THAT TOUCHES THE DEFECT: `grep -rn '\.resize(' src/fleet/` returns ZERO callers,
        no AIMD controller reads 429s, and runner.py:640 still raises the unqualified "is DOWN" halt.
  Net: premise corrected, defect 0% closed. A lazier read would have closed the entry because related
  work landed — the exact overclaim this project keeps catching. ***
  D73 residual: CLOSED, verified by reading buildverify._sweep_containers and cli.py's --dry-run
  branch directly rather than the commit messages.
  It handled `main` moving under it mid-session (18a1fc6 -> f10a863, a sibling deleting list_by_prefix
  and renaming a test) by RE-VERIFYING EVERY CITATION AGAINST THE NEW TIP BEFORE COMMITTING, correcting
  drifted numbers twice. That is the discipline the round has been converging on.
  Its re-read caught three of its own issues pre-commit, including a quote-marked paraphrase of
  ADR-0083 that was NOT verbatim (reworded — the never-quote-another-module rule applied to an ADR),
  and an initial claim that list_by_prefix was "kept, unused" when it had actually been deleted.
  ROUTED: docs/PROGRESS.md:6005 (item 19) and :5894 (item 24, describes list_by_prefix as "kept" —
  it was deleted) -> relayed to the live STALEREF lane, which owns that file.

STALEREF lane: complete (1e78b9f, docs/PROGRESS.md + tests/test_workers_build.py; buildverify.py
  needed NO changes — it was checked and found already correct, not assumed).
  Sweep: line-grep CROSS-CHECKED against a whole-file whitespace-normalised offset->line-map sweep;
  zero line-break-spanning matches this time, and it reported that rather than implying the normalised
  pass had found extra. Found 12 · fixed 6 · CHECKED-CORRECT-LEFT-ALONE 6.
  The two PROGRESS.md fixes were made as ADDITIVE "AMENDED-at-commit" notes PRESERVING the checkpoint
  history rather than rewriting it — the annotate-don't-falsify rule, applied unprompted.
  Second-order handled: only item 19 cited the pre-rename test name; repointed to
  test_list_with_verdict_carries_dockers_own_words_on_a_failed_listing.
  *** ITS RE-READ CAUGHT IT ABOUT TO LAUNDER ANOTHER LANE'S VERIFICATION AS ITS OWN: a draft sentence
  copied "verified by reading ... directly" from the DELETION COMMIT'S MESSAGE as though the lane had
  done that reading. It had not. It fixed this by actually reading both call sites — and in doing so
  found the line had drifted to :10613 — then dropped the stale citation. That is Guardrail 1's
  no-laundering rule applied to EVIDENCE rather than to directives, caught by the lane on itself. ***
  Its second catch: an edit had split a backticked identifier across a line break — the EXACT hazard
  the wrap-aware technique exists to find, nearly introduced by the lane fixing wrap hazards.

REAPFIX lane: complete (da70221 cli.py, e784573 tests/test_cli.py; 108 passed; ruff clean).
  C-1 REPRODUCED BEFORE FIXING, as instructed: on the unmutated tree both the neutered negation
  (numstat 1/1) and the realistic delete-and-drop-the-params refactor (2/2) left 105 PASSED.
  ADR-0081 §2's sentence was false.
  *** MY RE-MEASURE INSTRUCTION PAID OFF: I-1 re-measured at HEAD with a65a305 in — BOTH mutations
  still leave 105 green, so NEITHER was closed by the sibling's commit. The reason is precise and
  would have been invisible to an assumption either way: a65a305's fixture sets ps_fails, so its
  listing is EMPTY and claims() never receives an input. No duplication, no wasted test. ***
  Four discriminating mutations, each sha-verified applied with the hunk read: neuter-negation,
  realistic refactor, drop-worktree-prefix-filter, drop-claims() — each 1 failed / 107 passed.
  Deliverable 3 (the operator-facing strings): reverting both -> 2 failed / 106, failing at
  `"precondition" not in` with the exit-code and "step 5" assertions PASSING above it; quantifier-only
  and hard-stop-only mutations each fail exactly their own assertion. Three properties, separately bound.
  FOURTH QUESTION ANSWERED PER TEST, and each answer names the state the test builds: (1) builds the
  `phases` row the predicate is over, so the live set flips empty<->non-empty; (2) registers two
  neighbours in the repo the preview interrogates, so the prefix filter HAS INPUT TO REJECT — an
  equality assertion, since the defect ADDS names; (3) puts a live rung-3 container plus a
  heart-beating row in front of claims(), which the only prior --dry-run case (empty inventory) never did.
  HANDED OFF (I own the routing): ADR-0081 §2 replacement text is verbatim in its report — the
  negation is really pinned by test_resume_dry_run_does_not_read_a_dead_workers_row_as_a_claim_on_its_sandbox,
  plus an explicit RETRACTION that ..._names_the_orphans_it_would_reap_and_removes_none does NOT pin
  it. Same correction needed in task-reap2-report.md. Cite symbols, not lines.
  NEW, flagged not edited: a THIRD quantifier site at cli.py:10020 ("continue from the earliest
  incomplete phase") MIRRORS docs/SPEC.md:6566/:1567 — fixing cli alone would DESYNC THE PAIR, and
  SPEC was not its file. Correct restraint; needs one lane owning both.
  Note: `ruff format --check` is red repo-wide on main and is NOT a delta from this round.

FULL SUITE STARTED (background) at a clean tree, 70+ commits overdue. This is the round's gate:
  green = 0 failed AND xfail 0 AND a clean bazel disk line. Nothing may edit the tree until it lands.

AUDIT lane (§39 open items): complete (0559f2c, docs/superpowers/plans/open-items-audit-round-c.md).
  Tip audited: 1e78b9f, 12 commits past §39's c24e7d2 anchor — and it SAID SO rather than implying
  §39's anchor. 21 items: VALID 11 · PARTIAL 5 · STALE 3 · NOT-A-TASK 1 · unverifiable-without-a-run 1
  · DUPLICATE 0. Compare round B's audit of §38: 15 VALID / 1 STALE / 3 NOT-A-TASK / 1 DUPLICATE.
  §39 holds up much better than §38 did, which is the point of auditing while the evidence is fresh.
  Differing from §39: item 3's DEFECT half is closed (both retracted strings gone AND bound by tests)
  though its _EXPECTED_SITES census gap is open; item 17 no longer holds; item 11's "nine reviews" is
  ten; item 14 grew to +5,495/-69 over 74 commits. Items §39 marks closed (1, 4, 19, 20) VERIFIED
  genuinely closed rather than accepted. D72 (all three legs), D74 (both) and D55 (`.resize(` zero
  callers, runner.py:640 unqualified halt) all re-verified open exactly as adjudicated.
*** MATERIAL NEW FINDING — THE ROUND'S DOMINANT FAILURE MODE, INSIDE THE COMMIT THAT FIXED ITS TWIN:
  ResumeIncompleteError (cli.py:10085-10086) tells the operator that §11.5 step 2 is "absent too" —
  but _resume_impl RUNS THE REAP at :10233-10234 and emits its lines. The string is false, and it sits
  in da70221, the very commit that corrected the other half of the same message. A fix correcting one
  clause of a sentence and leaving the adjacent clause false. -> round D's first fix, bundled with the
  census gap that would have caught it. ***
  Four more, unreported before: D74's `## D74 —` heading is INVISIBLE to the detector that the
  `### D71 — UNUSED` record itself defines (the register's own convention is now internally
  inconsistent); INTEGRATION_HONESTY.md:4181-4183 still calls the cli.py strings open; §39 item 15
  accounts for 16 of §38's 17 items (drops item 8); and round-B's table numbers §38's list 1-20 while
  it is now 17 items, so item 15's cross-references are off from 6 up — mapping recorded.
  ITS RE-READ CAUGHT THE SAME TRAP I FELL INTO WITH D71: a near-miss "six workers touch ctx.llm" that
  is FIVE, because contracts.py:10 is a DOCSTRING SAYING IT IS NEVER TOUCHED. Mention counted as
  definition. Also regraded its own item 3 from STALE to PARTIAL and struck an unprovable claim.
  Held: no dispatches while the full suite runs. Nothing may edit the tree until it lands.

*** FULL SUITE: GREEN. The round's gate is met. ***
  1686 passed in 581.31s (9:41) · 0 failed · 0 skipped
  bazel disk: peak 4.22 GiB (ceiling 6 GiB) · residual output bases 0 bytes · repository cache kept
    1645 MiB  -> clean disk line, no ceiling breach, nothing to prune
  "0 tests skipped this session — full collected coverage ran"
  Baseline at round start (§38, landed main at 6a5e534): 1575 passed. Now 1686 => +111 tests,
  every one green, across ~75 commits. Run at a clean tree with no lane editing.
  NOTE for the record: this is the FIRST full-suite run of round C. It was correctly deferred the
  whole way — lanes were live continuously and two pytest sessions must never overlap — but it means
  every intermediate claim of "green" in this ledger was SCOPED, and only this line is the whole suite.

LEDGER lane: complete (96cf597, INTEGRATION_HONESTY.md + PROGRESS.md).
  D1: `## D74` -> `### D74`. Verified by LOOPING the detector over D1..D74 and reporting no MISSING
  lines — i.e. it proved the whole register is visible, not just the one entry it fixed.
  D2: a sibling lane began editing cli.py mid-session, so it RE-ANCHORED EVERY CHECK to
  `git show HEAD:` rather than the working tree — refusing to credit unlanded work. And it recorded
  as fixed ONLY the two strings it verified, explicitly leaving the "step 2 ... absent too" clause
  STATED AS OPEN because that one is still false at HEAD. Precise scoping under a moving tree.
  D3: re-derived the round-B -> current item mapping BY CONTENT COMPARISON rather than trusting the
  audit's table (7->6, 8->7, ... 20->17), recorded as an AMENDED note. Nothing renumbered — the third
  time this round recording beat renumbering, for the same reason each time.
  D4: item 3 -> PARTIAL; reviews 9 -> 10; item 14 re-measured AT A NAMED ANCHOR (main@0559f2c):
  75 commits, 42 touching src//tests/, +5,495/-69 across 22 files.
  *** A BETTER PROOF THAN I ASKED FOR: I asked it to verify §39's disclosures survived by extracting
  each and confirming presence. It instead proved it STRUCTURALLY — every edit is a pure append, shown
  by `git diff | grep "^-[^-]"` confirming every removed line is an exact prefix of its replacement.
  That establishes the property for the whole file at once, not just the eight sentences I named. ***
  Its re-read caught an INHERITED line-number error it had copied from the audit (ResumeIncompleteError's
  raise site :10076 -> :10079), re-measured rather than passed along.
  NEW, reported not fixed (out of its scope): *** the D71 record's own text claims its detector
  "returns nothing" — but D71's heading now MATCHES the pattern, so the detector returns 1. A record
  about distinguishing definitions from mentions contains a claim falsified by its own existence.
  Pre-existing, self-referential. -> lane D71FIX. ***

LASTSITES lane: complete (821277a D1+D2 with D2's three sites together, 8951e48 D3, ef66a0d reflow,
  f94e488 an overclaim it found on its own re-read). 119 passed (110+9, was 108+9); ruff clean.
  *** FOUR sites carried the step-absent falsehood, not the one I briefed: the ResumeIncompleteError
  clause, the `resume` DOCSTRING (which ALSO said "exit 1" against ExitCode.USAGE), and
  _refuse_unbuilt_resume_flags's built-steps list. And it measured what is actually built rather than
  accepting my framing: §11.5 steps 1 (CONFIG-DIGEST HALF ONLY — runs.harness_version is read nowhere
  in cli.py), 2, 3, 7 built; 4, 5, 6, 8 absent. ***
  D2a/b/c: reverting the cli help line and each SPEC mirror -> 1 failed / 118 each, with ALL NINE
  test_floor_rule_statements.py cases PASSING. It named that blindness as THE discriminating part —
  the floor-rule mechanism built earlier this round does NOT cover these three sites. An honest
  statement of a mechanism's boundary, measured rather than assumed.
  Re-grep (whitespace-normalised, offset->line map): the D2b anchor REALLY DID STRADDLE A NEWLINE —
  the wrap-aware technique earning itself yet again. No false quantifier, predicate or step-absent
  claim survives in cli.py, SPEC.md or DECISIONS.md. Checked-and-left-alone with reasons:
  _earliest_open_wave; SPEC:6978; four ADR-0076/0077 HISTORICAL quotes; and SPEC:1509/:6624/
  cli.py:10158, whose "re-validating preconditions" is TRUE of step 8's PhaseRunner._re_entry call
  site and not of step 5 — a distinction a blunter sweep would have destroyed.
  It DEVIATED from the handed-off ADR text once, correctly: the verbatim wording said the false claim
  "stood for four commits", which was false at land time (20 since ead96e6), so it re-anchored on
  commits. Applying handoff text verbatim EXCEPT where the handoff is itself false.
  Its re-read found THREE of its own overclaims, incl. that its corrected enumerations still called
  step 1 "built" when it is half-built (fixed in f94e488).
*** CONTROLLER ERROR #6: my brief asserted the false clause "sits in da70221, the very commit that
  fixed the other half of the same message". `git log -S` shows it was BORN in c45db53 and WENT STALE
  at e915b93; da70221 only left it standing. I inferred provenance from adjacency instead of running
  git log -S. The defect was real; my story about how it got there was not. ***
  OPEN, in-class and flagged not fixed (not its file): reentry.py:69 — phase_floor's OWN docstring
  says "the earliest phase this repo must re-enter at", while :19 beside it is correct. The wrong
  quantifier survives in the module that implements the rule. -> lane FLOORLAST.

D71FIX lane: work done, NOT COMMITTED on first pass — it read my brief's "You own X. Commit by
  explicit path" as not authorising a commit. MY AMBIGUITY, not its error; resumed it with an explicit
  instruction. Future briefs must say "commit your own work" in the imperative.
  Detector before: 1 hit (the record's OWN heading), contradicting its "returns nothing" claim.
  After: still 1 hit, and the record now STATES that the single hit is itself and explains why that is
  correct rather than hiding it. The right fix — the record demonstrates the distinction it teaches.
  CLASS SWEEP with a whitespace-normalised offset->line scanner over ~10,400 lines of both docs:
  45 candidates, each non-pinned-commit claim manually re-verified against current code and tests.
  *** FIXED 5, and FOUR ARE THE SAME PATHOLOGY THIS ROUND OPENED WITH: D21, D22, D26 and D27 in
  INTEGRATION_HONESTY.md were marked OPEN while actually CLOSED in 32365cf/8464dc6 — the day AFTER
  they were written. Verified against current gitea.py, cli.py and passing regression tests. That is
  the §38 problem (roughly 80% of carried items stale, one false when written riding five checkpoints)
  present in the defect ledger too, not just the checkpoint. ***
  Fifth: PROGRESS.md's Y1 entry claimed .reap() had ZERO production callers — falsified by TODAY's
  e915b93 landing two real ones. A claim that went stale inside this very round.
  Also fixed a duplicate site: PROGRESS.md had self-corrected the "twelve workers"/"earliest phase"
  claim in one place while a sibling site at :5980-5981 still said "Open".
  Checked-correct-left-alone: ~14, each named.
  ITS RE-READ CAUGHT A FABRICATED TEST FUNCTION NAME in its own draft, corrected to the real
  test_publish_is_not_blocked_by_worktree_droppings_outside_the_pathspec. A citation to a test that
  does not exist is worse than no citation — it READS as verified.
D71FIX committed: 056b547
