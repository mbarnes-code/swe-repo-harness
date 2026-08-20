# SDD ledger — plan: docs/superpowers/plans/sdd-backlog-b.md

ROUND B — unbuilt subsystems. BASE=7a8bfbb99657ddf3a5c24a660b06022826bd91c0

## Wave 1 dispatched (5 agents, concurrent)
- BK1 (worker, opus) — llm/backends/__init__.py + anthropic.py. Worktree agent/BK1.
- BK2 (worker, opus) — llm/backends/openai_compatible.py + config/models.yaml `local` profile. Worktree agent/BK2.
- ST1 (worker, opus) — orchestrator/stubs.py, §3.5.1 T1-T4 + stub_reconcile. Worktree agent/ST1.
- R1 (research, read-only) — research-B1.md: criteria scoreboard, SQL-comment-only findings,
  resume blast radius, ModelBackend contract vs SPEC §7.7 divergence check.
- CR1 (review, read-only) — review-B1.md: pre-flight defects in client.py / budgets.py /
  cli.py _unavailable surface / models.yaml loader.

Wave 2 queued (briefs written from R1's output): ST2 StubRot differential; RS1 fleet resume;
FD1 SQL-comment-only findings -> Python emitters.

## Wave 1 results
R1 (research) DONE — could NOT write its own file (Explore agent is read-only, no Write tool);
  orchestrator persisted `research-B1.md` from its return. LESSON: use general-purpose, not
  Explore, for any agent expected to produce a file artifact.
CR1 (review) DONE — `review-B1.md`. Critical 5 / Important 5 / Minor 3.
  CR1 and R1 INDEPENDENTLY converged on the same three defects. Strong corroboration.

### Corrected scoreboard (R1): 11 in-family §13 rows, not ~13 criteria
  Rows 33,34,35,36,37,38,39,40,43,45 + **47**. Fully built: **2** (row 39 cache-poisoning,
  row 47 truncation) — nothing to do on either. Partial/unbuilt: 9, spanning 25 distinct
  absent artifacts. Row 34's ENTIRE ledger side is already built.

### THE THREE TRAPS (routed to lanes)
T1 `llm.client.discover()` has ZERO call sites in src/. cli.py:763 calls `ecosystems.discover()`.
   context.py:151 leaves `backends=None` -> client.py:477 falls back to `registry()`, populated
   only by import side effect. Nothing imports `fleet.llm.backends`. A backend lands, its own
   test imports it directly, ships green, then `UnknownBackend` at wave 7. CR1 added the second
   half: settings.py:1163 validates `backend:` against hard-coded SHIPPED_BACKENDS, not the
   registry. ASSIGNED TO BK1 ONLY (collision control).
T2 `usage.model_id` cache-key split. Read key from config string (cache.py:473), write key from
   `usage.model_id` (cache.py:578-587), and `_stamp` lets backend-reported WIN. A backend echoing
   the resolved id => permanent 100% cache miss, silent, indistinguishable from a cold cache.
   **CONFIRMED LIVE IN BK2's COMMIT** at openai_compatible.py:454 — orchestrator verified by
   direct grep rather than relying on the reviewer. schema.sql:434 and models/tasks.py:47 both
   actively INVITE the wrong choice; BK2 was not careless.
T3 `on_drift`/`on_failover` accepted at client.py:472-473 and supplied by NOBODY (context.py:151).
   CapabilityDrift + BackendFailover fully computed, discarded. -> FD1's lane.

### Also established
- SPEC §7.7 vs client.py diffed term-by-term: **byte-identical, no protocol divergence.**
- SPEC §8 names llm/routing.py, negotiate.py, capabilities.py — none exists, none should be
  created; function lives at roles.py:111 / client.py:398,411 / client.py:418-427.
- `_unavailable`'s "still raises NotImplementedError" is FALSE for all five modules it names.
  `git grep NotImplementedError HEAD -- src/` = 4 hits, none in workers/. All twelve workers
  implement `preconditions_hold`. Never trust an `_unavailable` string as evidence.
- `--repoll-prs` is implementable TODAY in ~5 lines, needs nothing unbuilt. Cheapest win on board.
- HARD ORDERING: --repoll-prs must run BEFORE stub_reconcile or exit 7 inverts meaning.

### Lane status
ST1 DONE_WITH_CONCERNS — dd3f4a5 on agent/ST1. 26 tests, mypy clean. Resolved the §13 row 45 /
  §3.5.1 tension from SPEC §12.38 itself rather than escalating: row 45 is a carve-out on step 1,
  not a contradiction; a round only runs after T1 (MERGED required) so no PR facts reach
  settle_revalidation. Concern worth keeping: cli.py:10493 already encodes T4 in raw SQL.
  -> review dispatched.
BK1 DONE_WITH_CONCERNS — 3665d1d on agent/BK1. 30 tests. -> fix round 1 sent (T1+T2+effort).
  Orchestrator DECIDED its SPEC `effort` conflict rather than blocking: honour target.effort,
  verify shipped ids against the claude-api skill, do not extend ModelCapabilities.
BK2 DONE_WITH_CONCERNS — 9d942ae on agent/BK2. 31 tests. -> fix round 1 sent (T2 confirmed at :454).
  NOTE: first SendMessage raced its completion and was not absorbed; re-sent with the verified
  line number. Check for this race whenever resuming a just-finished agent.

## Wave 2 dispatched
RS1 (worker) — resume --repoll-prs + step 3 stale-lease reset + step 7 projection. Step 5
  (multi-phase PhaseRunner assembly) explicitly OUT of scope; it is the only large piece.
FD1 (worker) — wire on_drift/on_failover at context.py:151 + BackendUnavailable at runner.py:579.
  Told explicitly that RevalidationBudgetExhausted is an exception, NOT a finding kind.
ST1-review (reviewer) — review-ST1.md.

## BK2 fix round 1 — DONE_WITH_CONCERNS, commits 9d942ae..5a32bdb
T2 FIXED and the fix was PROVEN: BK2 restored the old expression, observed both regression tests
  fail, restored the fix, observed both pass. That is the standard — a regression test nobody has
  seen fail is not evidence.
BK2 FOUND A SECOND CONSEQUENCE the orchestrator's finding did NOT name: `cache._target_for`
  (llm/cache.py:611-618) ALSO matches (backend, model_id) against configured targets to recover
  the answering target's `effort` — itself a cache-key component — so a served name silently fell
  back to the PRIMARY target's effort. One cause, two silent defects.
CORRECTION TO MY OWN FINDING 2: I told BK2 that `profiles.local` might need `openai_compatible`
  added to SHIPPED_BACKENDS and that this would be a BK1 dependency. WRONG — it was already in the
  tuple at settings.py:107 at HEAD. BK2 verified by executing the settings load rather than taking
  my framing. No sequencing needed. (Guardrail 2: I cited a dependency I had not checked.)
STILL TRUE and worth keeping: because `known_backends` defaults to that hard-coded tuple rather
  than the registry, `profiles.local` loads WHETHER OR NOT the backend actually registers. Until
  BK1's settings.py:1163 fix lands, a broken backend import still passes startup.
NEW GAP CREATED BY THE FIX (BK2 disclosed it rather than leaving it): the served model name is now
  discarded entirely — correct for the cache, wrong for observability. A server quietly serving a
  different build is undetectable. Needs `TokenUsage.served_model_id` + a schema change. UNOWNED.
OPEN, material: `_SdkTransport` has zero test coverage. No request has ever left that module.
  Deliberately NOT papered over with an HTTP-mock dependency added on agent authority.
-> BK2 review dispatched (review-pkg-BK2.md, 1195 lines).

## In flight (5): BK1 fix round, ST1-review, BK2-review, RS1, FD1

## ST1 review — spec ❌ / not approved. Critical 1, Important 3, Minor 4. -> fix round 1 sent.
VERIFIED MET by reading (not just by the implementer's claim): T1 both-halves, T2 conjunction,
  T3's three triggers, T4, append-only trail, budgets.py exception reuse, no-SQL/no-LLM/no-conn.
  The state machine is sound; every finding is about its interface with the schema.
CRITICAL stubs.py:690 `_consumer_of` returns consumer_repo_ids[0]. ST1's report called the
  list-vs-one-row-per-consumer mismatch PRE-EXISTING; the reviewer checked schema.sql:289-292 and
  it is NOT — a StubRecord IS the aggregate of one row per consumer. Taking element 0 drops C2/C3:
  they survive the final checkpoint ACTIVE, emit no UnresolvedStub, and are missing from
  migration_state.json#unresolved_stubs. Exactly the silent lie row 35 exists to prevent.
  **26 green tests missed it because no test used more than one consumer.**
IMPORTANT stubs.py:640+:323 apply() bumps rounds_spent on T1 but revalidation_round is a PK
  component and T1's SQL doesn't touch it -> duplicate round-1 insert or a no-op update. Reviewer
  REPRODUCED LIVE with max_revalidation_rounds:0 (legal per settings.py:602): model_copy bypasses
  the validator, record then fails the CHECK and the §12.46 round-trip.
IMPORTANT stubs.py:185 pr_open includes PrState.HELD, but enums.py:307 says HELD is entered ONLY
  by stub_reconcile. Since reconcile re-runs in fleet resume, a held row is re-held FOREVER and can
  never reach ABANDONED. Also §12.38 scopes the carve-out to inside merge_wait_timeout_s but
  ProviderFacts has no timestamp -> a 3-week-stale draft is held indefinitely.
IMPORTANT stubs.py:609 emits UnresolvedStub for an operator abandon; committed cli.py:10502 emits
  StubAbandoned (a kind absent from StubFinding AND from the SPEC). Deliberately-abandoned stubs
  would report as ones the fleet failed to resolve.
ADJUDICATION OF ST1's OWN CONCERNS: concern 2 (cli.py:10493 second raw-SQL T4 encoding) VERIFIED
  TRUE and worse than stated — the two encodings disagree on finding KIND. Concern 3 (consumer_ids
  mismatch is pre-existing) VERIFIED FALSE — and that error is the root of the Critical.
  Lesson: an implementer's "pre-existing defect" claim is exactly the claim a reviewer must check.

## BK1 fix round 1 — commits 3665d1d..c8060b8. BOTH criticals closed, BOTH mutation-checked.
T1 FIXED: `cli._load_settings` now calls `discover()` and threads its keys into
  `FleetSettings.load(known_backends=...)` — **the parameter already existed and was never
  supplied**. One call fixes all three readers of `_BACKENDS`: §9 rule 2's gate, `models check`'s
  registry(), and RunContext(backends=None). Guarded by a FRESH-INTERPRETER test that imports only
  fleet.cli, asserts the registry is empty, then drives real startup. Mutation: remove discover()
  -> 2 failed.
T2 FIXED: BK1 HAD the bug (`model_id=message.model`). Now target.model_id. Two-call round trip
  through the real CachingModelClient asserts the transport is hit ONCE. Mutation: reintroduce -> 3 failed.
  BOTH backend lanes independently made the same mistake. schema.sql:434 + models/tasks.py:47 are
  actively misleading and should be reworded — UNOWNED TASK.

### `effort` verdict (BK1, cited to the claude-api skill's own source, not memory)
  claude-opus-5 `high` VALID (shared/models.md:73). claude-sonnet-5 `high` VALID
  (shared/model-migration.md:1196). claude-haiku-4-5 `low`: **THE SKILL CONTRADICTS ITSELF** —
  SKILL.md's effort table says the PARAMETER errors on Haiku 4.5; shared/model-migration.md:297
  says only the `max` LEVEL errors there. Under the stricter reading EVERY CHEAP CALL 4xxs with
  the shipped default profile. Param name confirmed `output_config: {effort: ...}`, nested.
  ORCHESTRATOR DECISION: drop `effort: low` from the haiku CHEAP target rather than pick a reading
  of a self-contradicting doc — omitting the param is strictly safer and loses nothing. The block
  is DUPLICATED in tests/test_cli.py:80-99 and test_settings.py, so this is 3 sites.
  BATCHED INTO BK2 FIX ROUND 2 (BK2 owns config/models.yaml) — deliberately NOT dispatched now,
  because BK2's review is in flight and editing the file mid-review would make that review stale.

### CROSS-LANE INTEGRATION ARTIFACT — orchestrator owns this, no single lane can fix it
BK1's now-real §9 rule 2 gate correctly REFUSES `profiles.local`, because `openai_compatible`
  is not registered in BK1's worktree — BK2's file is not there. So
  `test_profile_flag_selects_the_profile_every_role_resolves_through` exits 2 in BK1's lane and
  passes in BK2's. BK1 added a skipif on a LIVE `"openai_compatible" not in discover()` probe
  (correct idiom — not an xfail, and it self-heals). **MUST VERIFY AFTER BOTH LAND that the skip
  turns back into a PASS.** This is the first hard proof that BK1+BK2 must land together and that
  a post-land integration run is mandatory, not optional.

## Wave 3 dispatched
R2 (research, general-purpose so it can WRITE) — scope §13 row 40 (failover.py/BackendHealth),
  row 43 (token bucket + AIMD; 429 must never produce DOWN), row 38 (ContextTruncated + whether
  anything sizes against max_context). Build-size verdict required per row.

## UNOWNED / QUEUED
- `TokenUsage.served_model_id` + schema change (observability gap BK2's fix created).
- Reword schema.sql:434 + models/tasks.py:47 — they invited the same bug in two lanes.
- `_SdkTransport` has no test; no request has ever left that module.
- ST1's minors (4), deferred to final review triage.

## BK2 review — spec ✅ NO GAPS / quality APPROVED. 0 Critical, 1 Important, 5 Minor.
All four of BK2's self-reported claims INDEPENDENTLY VERIFIED: claim 1 TRUE (:475 reads
  target.model_id, body `model` never read), claim 3 HOLDS (both regression tests genuinely fail
  under the old expression; e2e is real CachingModelClient+MemoryLlmCacheStore+LadderModelClient,
  only HTTP faked, and `len(fake.requests)==1` is the assertion carrying it — `len(store)==1`
  would have passed anyway), claim 4 TRUE both halves.
CLAIM 2 PARTIALLY CORRECTED: `_target_for` IS at cache.py:611-618 and feeds effort at :577-585,
  but `parts.effort` comes from targets[0], so it mis-attributes ONLY on failover to a standby
  with a different effort. Real but second-order, not the independent defect BK2 framed.
IMPORTANT I1 openai_compatible.py:319 hard-codes `"strict": True` over a raw Pydantic schema from
  client.py:498. MEASURED: DependencyDisambiguation emits required=['confidence','rationale'] for
  four properties -> violates the strict subset -> every dep_disambiguate call 400s against a
  strict-enforcing hosted endpoint -> CONNECTION -> tier-wide failover -> permanent TierUnavailable.
  **vLLM IGNORES `strict`, which is exactly why all 32 tests stayed green.** The test suite could
  not have caught this; only a real wire payload assertion could.
REVIEWER REFUTED BK2's STATED BLOCKER on the untested-transport gap: httpx 0.28.1 with
  MockTransport is ALREADY installed as a hard transitive of `openai`, and AsyncOpenAI accepts
  `http_client`. So the gap is lane-sized, needs no new dependency — and WOULD HAVE CAUGHT I1.
  Lesson: "no tool exists for this" is a claim a reviewer must check like any other.
REVIEWER OVERRODE MY served_model_id CALL, and is right: a deduplicated structlog warning from
  parse_reply when the body's `model` != target.model_id gives detection with NO schema change and
  NO cache-key risk. Adopted; the queued schema task is CANCELLED.
MINOR M3 worth noting: CHEAP inherits max_output_tokens=4096 so _raise_cap (client.py:766-773)
  raises BudgetExhausted on FIRST truncation -> §13 row 47's guarantee is unavailable on that
  target. A shipped guarantee silently absent on the shipped local profile.
MINOR M4 settings.py:1400 uses `is None`, so `base_url: ''` PASSES startup. Real, outside BK2's
  lane (settings.py contention) -> QUEUED, not dispatched.
-> BK2 fix round 2 sent: I1 + MockTransport gap + effort:low removal (3 sites) + structlog warning
   + M3. M1/M2/M5/M6 deferred to final-review triage.

## R2 research (research-B2.md) — build sizes + a SHIPPED defect
Row 40 failover.py/BackendHealth = MEDIUM. Row 43 rate limiting = LARGE (AIMD has no home: 429
  signal is in llm/, semaphore is budgets.py:962, its ONLY acquisition is workers/classify.py:162
  = 1 of 12 workers, and asyncio.Semaphore has NO resize API. §11.8 acknowledges none of this).
  *(Premise re-measured 2026-08-20: row 43 is still LARGE and still NOT BUILT, but do not scope a
  resizable ceiling — Limits.for_tier already returns ADR-0083's ResizableLimiter, which resizes
  safely while held. asyncio.Semaphore the stdlib class is unchanged; what changed is which object
  sits there. Count is 1 of 5, not 1 of 12. Per D55: premise corrected, defect FULLY OPEN, 0%
  closed — resize() has no production caller and no AIMD controller reads 429s.)*
Row 38 ContextTruncated = MEDIUM, greenfield (max_context has 5 hits, all declaration/startup gate,
  ZERO runtime sizing — B1's claim independently confirmed).

### HIGHEST-RISK FINDING OF THE ROUND — row 43's disaster is ALREADY SHIPPED
runner.py:582-586 raises RunHalted "every backend target for {repo_id}'s tier is DOWN" — and DOWN
  has ZERO representation in src/. A pure 429 reaches it in three hops: client.py:532 never
  inspects `exc.trigger` so RATE_LIMIT fails a target over exactly like a connection failure ->
  tier exhausts -> TierUnavailable -> classify.py:243-257 makes it non-retryable.
  So §13 row 43's named scenario (sustained throttling mistaken for an outage, exit 8 at hour 30
  when the right action was concurrency 2) is LIVE IN THE CODE TODAY, wearing row 40's vocabulary.
  Rows 40 and 43 are therefore coupled and must be built together.
  -> ROUTED TO FD1 MID-FLIGHT: do NOT codify the misdiagnosis. Its BackendUnavailable finding must
     REPORT what was observed (tier, targets_tried, per-target FailoverTrigger) not ASSERT a cause;
     if triggers can't be plumbed cheaply, say so honestly rather than assert the strong claim.
     Explicitly told NOT to fix the underlying misclassification (LARGE, out of scope).

### CORRECTIONS R2 made to prior docs (both mine)
- B1's "only max_targets_per_call is read" was TOO GENEROUS: **no llm.failover.* field reaches the
  client at all.** client.py:499 reads a CallPolicy field of the same NAME, and CallPolicy() is
  built with all defaults at client.py:478 because **RunContext.llm_policy is never assigned** —
  the repo's own tests/test_config_keys_are_read.py:219-231 already says so. Routed to FD1.
- The brief I wrote told FD1/R2 that `rewrite/context.py` is the ADR-0021 composer. **It does not
  exist at HEAD.** Real composer is `render_prompt` at calls.py:258-279, and workers/classify.py:213
  BYPASSES it. My error — I cited a file from the SPEC tree listing without checking it existed.
- review-B1's "§9 rule 3 never fires" verified, but sharpened: rule 3 cannot consult declared
  capabilities AT ALL (capabilities= passed at neither cli.py:521 nor :533); it fires only on an
  operator-supplied capabilities_override.max_context.
- record_attempt's INSERT (repository.py:1678-1683) omits llm_failovers AND llm_backend,
  input_tokens, output_tokens, llm_cache_hit. Five dead columns, not one.

## ST1 fix round 1 — dd3f4a5..92648f5. ALL FOUR FINDINGS FIXED, CRITICAL MUTATION-PROVEN.
ST1 reverted ONLY the return to (consumer_repo_ids[0],), left everything else fixed, ran the four
  new tests: **4 failed, 28 deselected**, incl. InvalidStubTransition naming the wrong consumer.
  Restored, green. That is the proof standard for this round.
C1 _consumers_of returns every row; supersede -> tuple; reconcile fans out per consumer;
  settle_revalidation selects the row the REPORT names (C2's report had been rejected as a caller
  bug — a second latent defect the fix exposed); abandon_by_operator takes explicit consumer;
  held_for_merge per-row via new HeldStub.
I1 apply() bumps rounds_spent only on T2/T3, builds via model_validate not model_copy;
  max_revalidation_rounds=0 round-trip pinned.
I2 HELD dropped from pr_open; carve-out bounded by now + open_pr_max_age_s via new
  ProviderFacts.pr_created_at.
I3 StubFinding.STUB_ABANDONED added, matching cli.py's committed kind.
ST1 CONCERN WORTH A DECISION: it named the arg `open_pr_max_age_s` not `merge_wait_timeout_s`
  because the KNOWN_INERT ratchet's reader-detector is a BARE-WORD REGEX over src/ — using the
  real name would have closed a ledger entry falsely, since nothing passes a value yet. Correct
  call. **The wiring lane MUST remove that KNOWN_INERT line when it passes the real setting.**
-> scoped re-review dispatched.

## RS1 — DONE_WITH_CONCERNS, commit 4a519b3. Built --repoll-prs + steps 3 and 7.
_pr_sync_impl reused UNMODIFIED (no second sync path; needed no restructuring, as predicted).
  Re-poll placed after §11.5's refusals, before the step-3 sweep, skipped under --dry-run.
  Step 3 gated on heartbeat_at < now - run.stale_after_s, attempts RETAINED, lease_fence bumped,
  SQL factored out of abort which now executes the shared constant.
  stub_reconcile insertion point marked in-code with the exit-7 rationale — honours the ordering.
**LATENT DEFECT FOUND AND FIXED (not in scope, found while there): --raise-budget now raises
  budget_ledger.max_usd and clears halted=0 as ONE audited CAS. Before this, NOTHING in the harness
  could clear the sticky halt — so §10's documented recovery from exit 3 was a PERMANENT NO-OP.**
  Five previously-DISCARDED flags now exit 2 instead of being silently ignored.
Resume still refuses, but via CommandUnavailableError naming the missing per-phase PhaseRunner
  assembly — not _unavailable's false text.
CONCERNS: (1) 4 _unavailable sites still carry false text — reported, not reworded, needs a ticket.
  (2) settings.py:211's claim that heartbeat_ttl_seconds is captured from stale_after_s is FALSE —
  claim_phase never writes it; two liveness horizons can disagree. Recorded in INTEGRATION_HONESTY.
  (3) resume writes durably then exits 1, so CI can't tell "reconciled" from "crashed" until step 5.
  (4) ruff format --check failure on cli.py is PRE-EXISTING (verified via stash).
-> task review dispatched.

## BK2 fix round 2 — 5a32bdb..c23a08a. All five fixes applied.
HEADLINE PROOF: with `"strict": True` restored, the new wire test FAILS while all 36 pre-existing
  fake-transport tests stay GREEN. Round 1's blind spot reproduced exactly. That is what makes the
  MockTransport work worth its cost — the old suite structurally could not see this class of bug.
FIX 1 WAS WORSE THAN THE REVIEW REPORTED: BK2 re-derived it rather than taking the reviewer's
  number and found **8 of 12 roles violate the strict subset across all three tiers**
  (HEAVY 4/5, WORKHORSE 3/4, CHEAP 1/3) — not just dep_disambiguate. Fixed by OMITTING the key
  rather than setting it false.
FIX 2 — BK2's own post-mortem on its wrong blocker, worth keeping as a round lesson in its words:
  "I checked the declared dependency list and stopped, never checking what was INSTALLED. Same
  error shape as FIX 1 — verifying a proxy instead of the question." The BK2 re-reviewer was asked
  to hunt for any REMAINING claim with that same shape.
FIX 3 applied at all three sites, recorded in-config as an ORCHESTRATOR decision, not BK2's
  recommendation (directive-lineage discipline held). BK2 FLAGGED A CONSEQUENCE I had not
  considered: `effort` is a cache-key component, so the change ORPHANS every existing CHEAP entry
  on the default profile — a one-time cold-cache event that will READ AS a hit-rate regression.
  Needs an operator-facing note at land time so nobody diagnoses it as a bug.
FIX 4 — BK2 WITHDREW its own schema proposal and accepted the reviewer's cheaper structlog warning,
  citing a point it had not weighed: no second model-name string one refactor away from the cache
  key it had just fixed.
FIX 5 confirmed arithmetically, fixed in config, test proven to fail without it.
BK2 ACCEPTED my correction to its round-1 claim 2 (overstated _target_for).
-> scoped re-review dispatched.

## LAND1 dispatched (read-only planner, no landing authority)
Must produce: conflict matrix from REAL diffs (three branches touch cli.py/test_cli.py — main
  risk), land order, per-step verification, per-step rollback, and the cross-lane integration
  checks no single lane could run. Told explicitly where the first FULL-SUITE run belongs — it
  has not run once this round.

## In flight (5): FD1, ST1 re-review, RS1 review, BK2 re-review, LAND1

## ST1: COMPLETE (commits dd3f4a5..92648f5, re-review CLEAN — first lane fully closed)
All four findings ADDRESSED. Re-reviewer verified the mutation claim ARITHMETICALLY rather than
  taking it: exactly four tests fail under (consumer_repo_ids[0],) — three fan-out assertions plus
  the settle membership check; the operator-abandon test survives because it uses `acme-app`, i.e.
  INDEX 0. The quoted error renders list(consumers) as ['acme-app'], which ONLY the mutated build
  can produce. 27 functions x 2 parametrizations = 32 collected = 4 selected + 28 deselected. The
  numbers reconcile exactly — that is what verifying a claim looks like.
Re-reviewer also checked the ONE thing I would have missed: the only `set()` in the module is
  `sorted(set(provider_repo_ids))` at :288 inside revalidation_key — the hash's SPECIFIED dedup,
  not a consumer path that could silently collapse rows.
I1 nuance worth keeping: `next_round_record` (:760) still uses model_copy, but is untouched by the
  diff and pre-checks the cap at :754-759 — and the cap is the ONLY invariant StubRecord validates,
  so it cannot build an invalid row. Correctly NOT flagged as a defect.
ST1's concern (a) ADJUDICATED CORRECT by the re-reviewer: the KNOWN_INERT ratchet is a bare-word
  regex with docstrings blanked, and the key is not in QUALIFIED_MATCH_KEYS, so using the real
  name would have closed a ledger entry for a still-unread key. Naming it open_pr_max_age_s was right.
DEFERRED to final triage: bound is opt-in (both args default None); the past-bound sweep emits
  UnresolvedStub where §13 row 45 describes BLOCKED/UnmergedDependency (wiring lane's call);
  stale "returns None" docstrings.

## FD1 — DONE_WITH_CONCERNS, commits a581357..dabfd50. Handled the honesty constraint correctly.
Built orchestrator/findings.py::LlmFindingSink (buffers on the sync hot path, flush() drains
  through the run's ONE writer); context.py wires on_drift/on_failover; runner.py flushes per
  dispatch + per wave and writes the row-40 finding before the exit-8 halt. **llm/client.py
  UNMODIFIED** — it supplied the existing callbacks rather than changing the client, as instructed.
11 new tests, ALL asserting a persisted SQLite row (not a mock call). Mutation-verified: removing
  the two callback args + the record_backend_unavailable call fails 6 of them.
**IT DECLINED TO ASSERT A CAUSE IT COULD NOT DETERMINE, AND SAID SO IN THE ROW.** TierUnavailable
  carries tier + targets_tried but no per-target FailoverTrigger, and — its structural finding —
  **_emit_failover never fires for the LAST target**, so the trigger set is unreconstructable.
  Payload: `observed` (verbatim, naming tier + every target in order) plus machine-readable
  asserts_outage:false, failover_triggers:null, failover_triggers_recorded:false, and a caveat
  naming throttling. retry.py:196's "DOWN" vocabulary is no longer passed at all, with a test
  asserting no data field carries it. This is the right answer to a question with no honest
  confident form. -> review dispatched to verify the _emit_failover claim, which is load-bearing.
CONCERNS: (1) halt strings in runner.py/retry.py:196/enums.py:289 still assert DOWN — finding
  fixed, strings left with a comment. (2) attempts.llm_failovers NOT written: attribution is
  impossible from a wave-shared client. (3) RunContext.llm_policy still unassigned — needs a
  CallPolicy.from_config builder, cli.py edits (off-limits to it), invented failover.enabled
  semantics, and three KNOWN_INERT deletions.

## R3 dispatched — design-only scoping of §11.5 step 5 (the per-phase PhaseRunner assembly),
   the one genuinely LARGE remaining piece. Must produce an executable task breakdown.

## In flight (5): RS1 review, BK2 re-review, FD1 review, LAND1, R3

## RS1 review — spec ✅ met (against its assigned subset) / approve with fixes. 0C / 4I / 4M.
All 7 claims verified against committed code, NONE accepted on report. Tests satisfy Rule 9: both
  assertions I demanded exist and READ STATE BACK — attempts==2 and fence==5 in
  test_resume_reclaims_a_stale_lease_without_charging_an_attempt, plus a negative case asserting a
  LIVE lease's full 4-tuple is unmoved.
CLAIM 4 VERIFIED BOTH HALVES — the round's most valuable single find. `git grep` finds exactly ONE
  `halted` writer at base: repository.py:1410 `SET halted = 1`, whose own docstring says no argument
  writes 0 — while the reservation CAS at :562 carries `AND halted = 0`. So SPEC.md:6445-6450's
  exit-3 recovery was a PERMANENT NO-OP and a budget-halted run was unrecoverable without
  hand-editing SQLite. New writer puts the UPDATE + RunBudgetRaised insert in one StateWriter unit;
  state/db.py:458-465 wraps every unit in BEGIN IMMEDIATE->COMMIT. Genuinely atomic.
I1 cli.py:9990-9992 PrEmissionError propagates BEFORE the sweep+projection at :10005-10014, so
  `resume --repoll-prs` with expired gh creds reconciles NOTHING while plain `resume` would have.
  A flag that ADDS a step must not be able to SUBTRACT the steps that would otherwise run.
I2 cli.py:10005 the gate reads live run.stale_after_s, NOT the per-row heartbeat_ttl_seconds that
  models/state.py:119 compares and settings.py:212 says exists so config cannot RETROACTIVELY
  declare a live worker dead. RS1's concern 2 VERIFIED TRUE: repository.py:1164-1178 claim_phase
  never writes that column. Lowering the key reclaims LIVE leases -> two writers on migrate/<repo>.
I3 cli.py:9982/:9991/:10021 `--dry-run --raise-budget 50 --json` exits 0 emitting raise_budget:50.0
  beside dry_run:true while halted is still 1 — the SAME defect class _refuse_unbuilt_resume_flags
  was written to kill, in the SAME commit.
I4 exit 1 covers reconciled, forge-failed AND crashed. ORCHESTRATOR DECISION SENT: "reconciled but
  cannot continue" must NOT share an exit code with "crashed"; reuse an existing ExitCode member if
  semantically correct, else add one + an ADR. Delegated the VALUE (RS1 has the enum, I don't), not
  the decision.
M1 — RS1's OWN CLAIM WAS WRONG: it reported the ruff format failure as "identical" pre-existing,
  verified via stash. Reviewer MEASURED it: 58 hunks at HEAD vs 59 at 4a519b3. RS1 added one (the
  json.dumps literal at :10238). Pre-existence stands; "identical" did not.
  **LESSON FOR THE ROUND: verifying a failure EXISTS before and after is not verifying it is
  UNCHANGED.** Same shape as BK2's "verified a proxy instead of the question."
M2 :10222 --raise-budget SETS rather than raises -> will silently LOWER a ceiling. One-word footgun.
-> fix round 1 sent (I1-I4 + M1 + M2; M3 comment amend; M4 stays report-only).

## BK2 re-review — APPROVE WITH FINDINGS. All five fixes ADDRESSED at code level.
1 strict ADDRESSED (:352 emits {"name","schema"}; key OMITTED not falsified — correct, `strict`
  defaults false and vLLM ignores it). 2 _SdkTransport ADDRESSED (six tests drive REAL AsyncOpenAI
  over httpx.MockTransport, wire bytes asserted). 3 FIX3 sites ADDRESSED. 4 FIX4 ADDRESSED
  (_warn_if_renamed :386 deduped via _RENAME_WARNED :95; served name never reaches _usage or the
  cache key). 5 FIX5 ADDRESSED (re-derived independently: cap 4096 -> want=4096 <= cap ->
  BudgetExhausted at client.py:769; 8192 raisable).

### N2 — MY FIX 3 DECISION DID NOT ACHIEVE ITS GOAL. Orchestrator error, caught by the re-reviewer.
I told BK2 to drop `effort: low` so the parameter would not be sent. **BackendTarget.effort is
  NON-OPTIONAL with default "medium" (models/tasks.py:91)** — verified live, all CHEAP roles now
  resolve effort='medium'. Removing the YAML line did not remove the parameter; it SUBSTITUTED a
  value the operator never wrote. Under the strict reading I was hedging against, a backend sending
  target.effort unconditionally STILL 4xxs every CHEAP call — and I had already told BK1 to honour
  target.effort "when non-empty", which it now always is.
  Same silent-degradation class this round keeps finding: a fabricated default transmitted as if
  the operator had asked for it. I checked the YAML text instead of the RESOLVED VALUE — the exact
  proxy error I had just finished naming in two other lanes.
DECISION: effort becomes `str | None = None`, None meaning "do not send the parameter". There is no
  honest default for "the operator did not say". Routed to BK2 (owns models.yaml + the payload
  builder; models/tasks.py is uncontended now BK1 is out of it). BK1 will be told separately what
  to do with a None effort — BK2 must NOT edit anthropic.py.
  NOTE: effort is a CACHE-KEY COMPONENT, so this re-orphans CHEAP keys a SECOND time. One combined
  operator note, not two.

### N1 — "8 of 12" is WRONG, it is 9 of 12. HEAVY is 5/5.
build_authoring's ROOT is strict-clean but $defs.BuildTargetProposal leaves deps/srcs/visibility
  optional, and **strict applies to the whole schema TREE, not just the root.** Concrete harm: an
  auditor reads the comment, concludes build_authoring is clean, re-enables strict on HEAVY ->
  400 -> CONNECTION -> permanent TierUnavailable.

### N3 — docs/DECISIONS.md:299 and docs/SPEC.md:6292 STILL say `effort: low`.
Unrecorded, so a config reconciliation silently REVERTS the fix. ADR now assigned to BK2.

### BK2's headline claim: conclusion holds, numbers don't.
Round 1 shipped **32** items not 36; restoring strict on c23a08a fails **2** not 1 (the second is a
  pre-existing build_payload test its `-k` filter excluded); and round 1 did not merely MISS the
  bug — 5a32bdb:205 ASSERTED "strict": True. Blind-spot conclusion stands, arithmetic did not.
### THE PROXY PATTERN, COUNTED: three times in BK2's work (root schema for whole tree, -k selection
  for the round-1 suite, YAML text for the resolved value) — all conservative, none harmful. Plus
  RS1's ("identical" ruff failure) and MINE (N2). **This is the round's dominant error shape:
  verifying a stand-in instead of the thing itself.** Worth a CLAUDE.md entry at round close.

## FD1 review — spec ✅ / three error-path defects, two of which REINSTATE the bug class. -> fix sent.
Spec verified NOT on report: llm/client.py genuinely unmodified (six files in the diff, none is
  client.py; LadderModelClient( has exactly ONE construction site). RevalidationBudgetExhausted not
  added (grep count 0). All OUT items untouched.
F3 findings.py:161-172 flush() swaps buffers BEFORE writing with no try/finally -> a failed write
  destroys the records permanently. That is "computed then discarded" — the exact bug FD1 was
  dispatched to eliminate, reintroduced one layer up.
F2 runner.py:472-481 flush sits between _dispatch and outcome handling, so a writer error escapes
  into _run_repo's `except Exception` -> a SUCCESSFUL repo is recorded UNKNOWN/PENDING and its
  execution discarded. An observability path must never be able to take down the thing it observes.
F1 findings.py:270 CLAIM (a) ADJUDICATED: **TRUE for the full set, FALSE for the partial set.**
  client.py:539-541's `if index + 1 < len(targets)` guard is real — the LAST target's trigger is
  never emitted and a single-target tier emits none, so the row is right to refuse a diagnosis.
  BUT triggers for 1..N-1 ARE emitted, exc.trigger distinguishes RATE_LIMIT cleanly, and FD1's own
  lane now persists them. So `failover_triggers_recorded: false` is itself now inaccurate: N-1
  RATE_LIMIT triggers land in `events` while the finding says none were recorded. Told FD1 to
  report the PARTIAL set (tri-valued, or carry the N-1 triggers) while KEEPING the refusal to
  assert an outage.
(c) ADJUDICATED: asserts_outage is INERT as a query target — zero readers in src/; projection.py:135
  and digest.py:110 filter to CycleDetected. Its real value is the two regression tripwires. Keep
  it; stop describing it as something operators will query.
SPEC GAP: row-40 coverage is dispatch-only — cli.py:477 exits 8 with NO finding. Assigned to FD1.

## LAND1 plan (land-plan.md). NOT EXECUTED — landing is not yet authorized and reviews are open.
LAND ORDER: **ST1 -> BK2 -> BK1 -> RS1 -> FD1**
ORDERING INSIGHT I MISSED: land BK2 BEFORE BK1 and the skipif NEVER HAPPENS. BK1's skipif is a live
  discover() probe evaluated at COLLECTION, so with openai_compatible already on main the test
  passes on its very first post-land session — no commit exists where coverage silently drops.
HIGHEST RISK: llm/backends/__init__.py add/add, whole file (BK1's 14-line docstring vs BK2's
  9-line; bodies below are byte-identical). Needs a HUMAN PICK, not a merge -> keep BK1's.
  **ADR-0074's hook does NOT fire on rebase or ff-only merge, so nothing catches a wrong pick.**
  Also note the --ours/--theirs inversion during rebase.
All three cli.py/test_cli.py overlaps are CLEAN — closest approach 25 lines and 36 lines.
FIRST FULL-SUITE RUN: **before step 1, on unmodified main at 7a8bfbb** — without a baseline the
  first post-land failure is unattributable, and main is KNOWN-NOT-CLEAN (RS1's pre-existing ruff
  format failure). Then mandatory again after BK1 and after FD1. Three runs, SERIALIZED.
UNANTICIPATED REGRESSION: **BK1 narrows accepted backend names from 4 to 2.** The gate switches
  from SHIPPED_BACKENDS to the live registry, so configs naming bedrock/vertex (both with declared
  region validation at settings.py:112-113) now exit 2. NO TEST CATCHES IT. -> BK3 dispatched to
  build bedrock.py + vertex.py, which both closes SPEC's four-shipped-backends claim and removes
  the regression. Still wants an ADR.
BLOCKER: primary checkout has `M CLAUDE.md` (tracked, pre-existing, USER'S EDIT — not mine).
  land-worktree.sh refuses a dirty primary, so step 1 dies before it starts. NOT resolving this
  unilaterally; surfacing to the user.
MY TABLE WAS WRONG TWICE: BK1 does NOT touch settings.py (known_backends= already existed at :1112),
  and its cli.py hunks are at 77/511/518/530, not 763. LAND1 derived from real diffs and caught it.
LAND1 pre-scanned all five lanes against every KNOWN_INERT/qualified key: **no lane flips the
  ratchet.** ST1 confirmed self-contained (StubFinding.STUB_ABANDONED defined inside stubs.py:140,
  no enums.py edit); RS1's stub_reconcile marker is a COMMENT not an import, so RS1 has NO hard
  ST1 dependency.

## BK3 dispatched — bedrock.py + vertex.py (SPEC's remaining two shipped transports).
## In flight (5): BK2 fix3, RS1 fix, FD1 fix, R3 design, BK3

## R3 design (design-resume-step5.md, 512 lines) — CAUGHT A DESIGN-LEVEL DEFECT BEFORE IT SHIPPED
**THE SAVE: a naive ascending walk PROMOTES fresh repos to Phase 4.** `rdepverify.py:199` returns
  True when no BUILD row exists, so "walk phases ascending, demote to the earliest whose
  precondition holds" — the phrasing in SPEC §11.5 AND in my own brief to RS1 — would have marked
  never-built repos as ready for Phase 4. The algorithm must search **DOWNWARD from the settled
  frontier** using a NEW `evidence_holds` predicate, **never `preconditions_hold`**.
  runner.py:214 says it outright: "Neither verdict ever means 'skip the work'".
  Had we implemented from B1's framing (which I propagated into the RS1 brief) we would have
  shipped a resume that silently promotes unbuilt repos. This is why step 5 was scoped OUT of RS1.
**SECOND CONTRADICTION: demotion is literally unwritable today.** SPEC.md:180-182/6777 says demote
  to the earliest phase whose precondition holds; enums.py:45 has `RepoStatus.SUCCEEDED:
  frozenset()` — "terminal mechanically rather than by prose". Writing PENDING over a SUCCEEDED
  phase row is forbidden by `transition()`.
R3's RECOMMENDATION (accepted): add a `RESUME_DEMOTE` map gated on a keyword-only `resume=True`,
  mirroring the EXISTING `OPERATOR_REOPEN` precedent, plus a `PhaseDemoted` finding — rather than
  opening SUCCEEDED->PENDING to every automatic path, or silently skipping settled rows.

### CORRECTIONS R3 made to the round record (two of them to MY ledger)
- **11 workers implement preconditions_hold, not 12** (+4 cli composites = 15 impls). B1 said 12
  and I repeated it in three briefs.
- **`prwriter.preconditions_hold` is at :209 and does NOT check MERGED** — that check lives in
  `run()`. B1 cited :272 as the precondition; I propagated that into the RS1 brief and the ledger.
- `fleet gc` reaps no worktrees (B1 suggested factoring step 2's reap from it).
  All three are the same failure: B1's index was taken as authoritative by later briefs instead of
  being re-verified. Same proxy shape as the rest of the round.

### Decomposition for next round (dependency order 1->2; 3->4->5; {1,2}->6; {2,5,6}->7->8->9->10)
1 demotion transition (enums.py/SPEC/ADR) S — GATE, do first and ALONE
2 phase_floor pure frontier+demotion computation (new orchestrator/reentry.py) S
3 step 2 reap orphans (sandbox/worktree.py:303, container.py:323 already exist) S
4 step 4 Git-as-arbiter (vcs/commits.py:298,312,344 already exist) M
5 evidence_holds — four per-phase DURABLE predicates M
6 the demotion writer (state/repository.py, state/checkpoints.py) S
7 wire step 5 into _resume_impl (between _reset_stale_running and project_once) S
8 step 6 recompute blocked_by + synthetic wave M
9 un-refuse --from-phase/--repo/--reset-attempts S
10 step 8 continue — delegate to _transform/_build/_verify_impl L
Nothing touches runner.py -> no collision with FD1's lane.

## DEM1 dispatched — subtask 1, the gate.

## BK2 fix round 3 — c23a08a..37a9e90. 466 tests pass.
N2 CLOSED: `effort` reaches **7 sites, not the 3 I estimated**. Now optional through models/tasks.py
  AND cache.py; absence keys as "" and was VERIFIED DISTINCT from "medium"; column stays NOT NULL
  so **no migration**. cli.py needed no change and was not touched.
N1 CLOSED: 9/12, HEAVY 5/5, corrected in code + both docstrings and **pinned by a test that
  RECOMPUTES the count from the schemas** rather than hard-coding it — so the number cannot rot.
N3 CLOSED: ADR-0075 written, both stale doc sites annotated superseded, one combined operator note.
BK2's ANSWER for anthropic.py (routed to BK1 verbatim): omit the effort parameter ENTIRELY when
  target.effort is None; never substitute a level of its own. "None means the operator declared no
  preference — not a cue to pick a default. Re-introducing one below the config layer restores the
  exact defect ADR-0075 removes, one layer further from view."
ROUND-1 NUMBERS RE-MEASURED BY COLLECTING AT EACH COMMIT: shipped **31**, ended **32** — not 36.
  The 36 was a `-k` selection over the ROUND-2 44-test file, "describing a set that existed at no
  commit". Restored-strict fails **2** not 1. And round 1 did not MISS the strict bug — 5a32bdb:205
  **ASSERTED** it, in a test named `..._sends_a_strict_response_format`. BK2's words: "Worse than a
  blind spot; I called it a gap." Every number in its first two reports was wrong in the safe
  direction; the conclusions all held.
OPEN: cli.py prints `effort=None` (one `or "-"`) -> routed to BK1, which owns cli.py this round.

## FD1 fix round 1 — dabfd50..607c12e. Mutation: neutering the 4 new guards fails exactly 9 tests.
F3 CLOSED: flush() re-buffers AT THE FRONT under `except BaseException`, tracking how far it got so
  a retry restores only what did NOT land. Whole-batch re-buffer would mint DUPLICATE events rows
  (event_uid is a fresh uuid4) — a second-order defect FD1 reasoned about rather than tripping over.
F2 CLOSED: new _drain_llm_findings logs at error with buffer depth and returns; `except Exception`
  so cancellation still propagates. Precedent cited: obs/events.py makes the same trade for the
  same reason — swallowed AND surfaced.
**THIRD INSTANCE OF THE SAME PATTERN, found by FD1 while writing the F5 test:** the halt-path
  record_backend_unavailable was ALSO unguarded — and worse, the exception escaped BEFORE
  `raise RunHalted`, so `report.halt` was None and **the tier outage vanished entirely.** Now
  isolated; also closes F5. Three instances of one shape in one lane: an unguarded observability
  write on an error path.
F1 RESOLVED WELL: on_failover now records {"<backend>:<model_id>": trigger}, keyed by target,
  run-scoped (the grain and lifetime SPEC §11.8 gives BackendHealth). Row carries the actual map,
  a three-valued failover_triggers_recorded — "none"/"partial", **never "complete"** since the
  exhausting target's trigger is structurally unemitted — and a derived throttling_observed.
  `throttling_observed` is DELIBERATELY NOT the negation of asserts_outage: both false means
  "we do not know", pinned by a test. That is the honest encoding of a genuinely partial signal.
F4 NOT closed, reported: _mapped_errors() is a zero-arg funnel at 19 sites with no run_id/writer,
  and unreachable today (every complete() caller in src/ is a worker under a PhaseRunner).
-> scoped re-review dispatched, tasked with verifying the unreachability claim specifically.

## RS1 fix round — DONE. 4a519b3..eedc24b. 171 passed.
All 4 Important + M1-M3 fixed. EXIT CODE: **2 (ExitCode.USAGE) via a new ResumeIncompleteError.**
  Reasoning accepted: the unifying property of §10's existing exit-2 cases (unpriced target,
  pr --ready vs unresolved stub, mirror mutex) is *deliberate refusal, identical on re-invocation*
  — which is exactly this case and exactly what defuses I4's retry loop. Rejected a 12th code
  (would falsify SPEC §10's table and outlive a temporary state), exit 7 (means "run completed,
  human needed" — the same corruption §13 row 45 warns of), exit 11, exit 0. Exit 1 RETAINED for a
  real forge failure and RANKED ABOVE the step-5 refusal. ADR records a CI branch table and says
  the class should be DELETED when step 5 lands.
I2 RESOLVED, and the column turned out USABLE: heartbeat_ttl_seconds is NOT NULL DEFAULT 300 and
  readable — only its WRITER is missing. Gate now requires a row to outlive BOTH horizons,
  evaluated inside the writer's statement. Lowering stale_after_s can no longer reclaim a live
  lease; raising it only makes resume more conservative. Mutation-checked.
M1 correction accepted and measured: 58 / 59 / 58 hunks (base / 4a519b3 / now).

## LAND BLOCKER FOUND BY ORCHESTRATOR (not by any lane): **ADR NUMBER COLLISION.**
BK2 and RS1 BOTH wrote ADR-0075 — main is at 0074 and each took "the next number" in isolation.
Neither could have seen the other; this is a structural hazard of parallel lanes writing to a
shared append-only doc, and NO agent could have caught it. Verified by diffing docs/DECISIONS.md
across all five worktrees. Land order gives BK2 precedence -> RS1 renumbering to **ADR-0076**.
**PROCESS FIX FOR NEXT ROUND: assign ADR numbers from the orchestrator at dispatch time.**

## === SESSION LIMIT HIT — five agents killed mid-flight. State survey before restart. ===
The ledger did its job: I resurveyed the WORKTREES rather than trusting my own recollection of
what each agent had reported. Actual state, verified by `git log -1` + `git status --porcelain`:
  BK1   c8060b8 committed + UNCOMMITTED cli.py/anthropic.py/test (fix round 2, partial)
  BK2   37a9e90 CLEAN — fix round 3 done, awaiting re-review
  ST1   92648f5 CLEAN — COMPLETE (re-review already clean)
  RS1   f385b19 CLEAN — **the ADR renumber 0075->0076 DID land before the kill**
  FD1   607c12e CLEAN — fix round done, awaiting re-review
  BK3   NOTHING COMMITTED — untracked src/fleet/llm/backends/ + test file, still at base
  DEM1  NOTHING COMMITTED — modified DECISIONS.md, SPEC.md, models/__init__.py, enums.py, test
No work was lost. Two lanes (BK3, DEM1) had produced real code that existed only in the working
tree — a reminder that "commit early" is not style advice when sessions can die.

## Restart: 5 dispatched
BK1 resume (finish fix round 2: omit-on-None effort + cli.py effort=None print)
BK3 resume (told explicitly: nothing committed, commit early, and DELETE any backends/__init__.py
  it created — that file already has an add/add conflict between BK1 and BK2)
DEM1 resume (**assigned ADR-0077** — 0075 and 0076 are both taken now)
FD1 re-review (re-dispatched; original died before producing output)
RS1 re-review (told the branch has advanced one commit past the diff for the pre-approved renumber,
  so it does not flag it or treat the diff as stale)

## PROCESS FIXES ADOPTED THIS ROUND
1. **ADR numbers are assigned by the orchestrator at dispatch time.** Parallel lanes appending to
   a shared doc each take "the next number" in isolation. No agent can catch this; only the
   orchestrator can. Already cost one collision (BK2/RS1 both took 0075).
2. **Research agents must be `general-purpose`, not `Explore`** — Explore is read-only and cannot
   write its report file; R1's output had to be persisted by hand from its return value.
3. **Re-survey worktrees after any interruption.** Agent reports describe intent at the time they
   were written; `git status` describes reality.

## DEM1 COMPLETE — commits fc760f9, 5a86610 on agent/DEM1. 135 passed, mypy clean.
Built RESUME_DEMOTE + keyword-only resume=True on transition(), PHASE_DEMOTED_KIND, PhaseDemotion,
  demote(), exports, 3 tests; ADR-0077 + SPEC Constraint 7 / §11.5 step 5 / §5.1 code listing.
**OPERATOR_REOPEN VERDICT: matched the design on EVERY axis** — enums.py:52-56, typed
  dict[RepoStatus, frozenset[RepoStatus]], single key RHI->{PENDING}, gated on keyword-only
  `operator: bool = False` (:59, consulted :66 via .get(old, frozenset())), and genuinely no caller
  in src/. enums.py:45 is exactly `RepoStatus.SUCCEEDED: frozenset(),`. **Nothing I told it was
  off** — the first brief this round with a clean citation record.
JUDGMENT CALL (assess at review): demote() returns (status, PhaseDemotion) as ONE value, so the
  finding is not obtainable separately from the status change. ~12 lines. DEM1's argument: it makes
  the audit obligation MECHANICAL rather than something subtask 6 can forget, and makes "prove
  PhaseDemoted is emitted" testable in a task that ships no caller.
MUTATION-TESTED, not asserted: reintroducing rejected alternative A (unconditional
  SUCCEEDED->PENDING) fails test 1; dropping demote()'s no-op guard fails test 3. Both reverted.
ADR renumber to 0077 touched **8 cross-references, not just the heading** — including code comments
  and a test docstring. Confirms the process fix was needed and shows renumbering is not a one-line
  edit. NEW RULE: after any renumber, grep the whole tree for the old number.
DEM1 CONCERN, REAL AND MINE TO HANDLE: **docs/DECISIONS.md will NOT auto-merge** — three lanes
  (BK2/RS1/DEM1) appended to the same file tail. Mechanical but manual at land time.
DEM1 CONCERN 3, worth keeping honest: BOTH audited doors (OPERATOR_REOPEN and now RESUME_DEMOTE)
  ship with no caller in src/. Correct for a gate — but must NOT be read as shipped capability if
  subtasks 6/7 slip. Any future brief claiming "resume can demote" would be false today.

## BK1 fix round 2 — 3665d1d -> c8060b8 -> 90fb4c0. 386 passed, 1 skipped, 0 xfail.
BASE STATE EXERCISED, NOT READ: BackendTarget.model_fields['effort'] -> Literal['low','medium',
  'high'], default 'medium', and BackendTarget(effort=None) RAISES ValidationError. BK2's change
  has not reached BK1's base. Implemented omit-on-None anyway.
FIX 1 — and BK1 found the detail that makes this non-trivial: **`format` is a SIBLING under
  `output_config`**, so the naive shortcut (drop output_config when effort is None) would silently
  drop the JSON_SCHEMA rung's schema **while LlmCallRecord still labelled the call JSON_SCHEMA** —
  a call recorded at a rung it did not achieve, exactly the silent-drift class §13 row 37 exists to
  catch. output_config is now built incrementally and attached only if non-empty. Separate test.
FIX 2 in reach and fixed: cli.py:10593 `effort={row['effort'] or '-'}`; JSON branch deliberately
  left as `null` (machine consumers want null, humans want -). Tested by driving the REAL
  models_list render over a REAL LlmRouter. BK1 disclosed honestly that this is ~10k lines from its
  other hunk and NOT a region it had already touched.
MUTATION-CHECKED: revert FIX 2 -> 1 fail; substitute a default -> 2 fails; send effort:null ->
  2 fails. "Counts collected, not recalled (39)."
BK1's OWN POST-MORTEM: its round-1 "when non-empty" gate claim was wrong because the type system
  made it unconditional — **it read the field's DECLARATION instead of exercising it.**
  That is the round's fifth instance of the stand-in error, across four lanes plus me.
OPEN: post-BK2-merge behaviour is REASONED, NOT EXECUTED. Needs one confirming run after merge.
  -> added to the post-land integration checklist alongside the skipif->pass check.

## In flight (5): BK3, FD1 re-review, RS1 re-review, DEM1 review, BK1 re-review
## QUEUED: BK2 fix-round-3 re-review (37a9e90) — no slot yet

## BK3 COMPLETE — commits 4a2b4de (bedrock), 2551e1c (vertex) on agent/BK3. 81 tests (40+41).
BK3 AVOIDED THE ROUND'S DOMINANT ERROR, in a form I had not anticipated: it probed the VENV
  directly rather than pyproject.toml, and found **the SYSTEM python3 HAS boto3 while the venv does
  NOT.** Checking `pyproject.toml` OR the wrong interpreter would both have produced a wrong answer.
  Neither SDK installed (boto3 NO, google/google-auth NO; anthropic/openai yes). No dependency
  added. Per trap 3 both modules fail to register — **asserted in a SUBPROCESS with the SDK
  blocked**, not reasoned. Tests stub sys.modules only when the real SDK is absent, which works
  because each adapter quarantines its SDK behind one transport class.
All four traps closed. The cache regression test drives a fake transport reporting a DIFFERENT
  model name — a Bedrock inference-profile ARN and a Vertex snapshot id, i.e. the REAL shapes these
  APIs return — and compares two real CacheKeyParts asserting read == write != poisoned.
effort omit-on-None implemented via an annotated `str | None` local; field still non-optional at
  its base, so it needs NO edit when BK2 lands.

### CONCERNS, all disclosed rather than buried
1. `backends/__init__.py` exists on disk (BK1's byte-identical copy) but is **deliberately
   UNSTAGED** — without it nothing imports and no test runs. So BK3's 81 passing tests DEPENDED ON
   AN UNTRACKED FILE. Correct call for conflict hygiene, but it means a fresh clone of agent/BK3
   ALONE cannot run its own tests. Flagged to the reviewer to state the CI consequence plainly.
2. pyproject.toml gained one additive mypy-overrides block for the uninstalled/unstubbed SDKs.
   Minor conflict surface at land.
3. **Vertex uses `google.auth` + `:rawPredict`, NOT the Vertex AI SDK.** google-cloud-aiplatform's
   surface covers Google's OWN models; Anthropic publisher models go through rawPredict.
   google-auth is a hard dep of the declared extra so nothing new installs — but the extra's NAME
   vs the module actually imported is a genuine mismatch. Flagged, not papered over.
4. **The BK1 regression is LATENT, not LIVE** — shipped config/models.yaml names only `anthropic`,
   so post-BK1 a bedrock/vertex target on a host without the extras STILL exits 2, which is what
   SPEC intends. So BK3 does NOT fully "close" the regression; it makes the transports exist. The
   residual is that the error message should NAME THE MISSING EXTRA. Not BK3's file. -> QUEUED.
   **This corrects my dispatch framing: I told BK3 its lane would remove the regression. It
   removes the missing-implementation half; the missing-SDK half is correct behaviour by design.**

## In flight (5): FD1 re-review, RS1 re-review, DEM1 review, BK1 re-review, BK3 review
## QUEUED, no slot: BK2 fix-3 re-review (37a9e90); "name the missing extra" in the config-gate error

## FD1 re-review — 3 of 4 closed; F4 REOPENS as a LIVE bug; F1 replacement has a scoping defect.
F3 ADDRESSED. Reviewer VERIFIED FD1's reasoning rather than accepting it: event_uid is minted at
  findings.py:252 (per attempt), so whole-batch re-buffering WOULD defeat ON CONFLICT
  (run_id, event_uid) at repository.py:1803; `unsent` increments only after a successful await, so
  failovers[unsent:] is exactly the un-landed tail — no drop, no double-count.
F2 ADDRESSED. The obs/events.py precedent FD1 cited is REAL and analogous (events.py:215-222 wraps
  the same append_event in except Exception, surfaced via _surface at error). PEP 695 syntax pins
  >=3.12, so CancelledError (a BaseException) genuinely propagates.
F1 mechanism ADDRESSED: "complete" unreachable BY CONSTRUCTION (findings.py:338 is a two-branch
  expression); both-false pinned at test_llm_findings.py:510-545.
THIRD INSTANCE ADDRESSED: original defect confirmed — the escape hit _isolated's except Exception
  at runner.py:414 so RunHalted was NEVER CONSTRUCTED. Fix at runner.py:624-637. No fourth site.
MUTATION CLAIM HOLDS — nine distinct tests matching FD1's list. Caveat recorded: the halt-path
  guard is MASKED in the combined run (the drain mutation already kills its test) though it does
  kill it individually. A mutation that is only individually detectable is worth knowing about.

### N2 (Important) — FD1's F4 UNREACHABILITY PREMISE IS FALSE. The bug is LIVE in a shipped command.
cli.py:9149-9226 (`fleet pr`) builds a RunContext at :9183 and runs PrwriterWorker DIRECTLY at
  :9299 with NO PhaseRunner; prwriter.py:413/421 reach client.complete(). That path buffers drift
  and failover findings and NEVER FLUSHES — discarded at exit. FD1's own bug class, live, shipped.
  WHY IT LOOKED RIGHT: the conclusion survives only because BaseWorker.run converts every Exception
  to a WorkerResult (base.py:912) — a mechanism FD1's report never cited. **FD1 reasoned from
  IMPORTER IDENTITY to CALL PATH.** That is the round's SIXTH instance of the stand-in error, and
  the reviewer confirmed it is the ONLY one in FD1's fix report; everything else verified.

### N1 (Important) — the F1 replacement is honest at its own grain, WRONG GRAIN for the row.
on_failover's trigger map is RUN-SCOPED ACROSS ALL THREE TIERS (roles.py:65-77) but is embedded
  UNFILTERED in a SINGLE-TIER finding, and _CAVEAT claims it describes that tier's targets.
  Concrete: a CHEAP-tier 429 sets throttling_observed:true on a HEAVY-tier outage row — telling an
  operator to lower concurrency when the real condition was a HEAVY outage. A fix for an honesty
  defect that introduced a subtler honesty defect.
MINORS: cancellation at `await future` can duplicate an events row — NEW WITH THE FIX, so it is
  FD1's; last-write-wins on the trigger map; **test_runner.py:1626's `pending == 1` is TAUTOLOGICAL
  ON A STUB** — a test that cannot fail. Told FD1 to make it real or delete it, not leave a
  decoration.
-> fix round 2 sent. N2's fix is a localized cli.py edit in the `fleet pr` region (~9149-9299);
   sibling cli.py hunks are at ~77/511/518/530, ~9800-10250, and ~10593 — all disjoint.

## RS1: COMPLETE (4a519b3 -> eedc24b -> f385b19 -> 781a42f, re-review approved)
Re-review: ALL SEVEN findings ADDRESSED. Reviewer VERIFIED rather than accepted throughout —
  checked the column definition itself (schema.sql:377 and v007_logical_keys.py:74 both NOT NULL
  DEFAULT 300 CHECK (>0), so no NULL can silently kill the gate), confirmed julianday parses RS1's
  exact _iso rendering (SQLite 3.45.1: 300s -> 299.877), confirmed both ?s bind inside ONE UPDATE
  under BEGIN IMMEDIATE (no read-then-write race), and confirmed CommandUnavailableError + the four
  _unavailable sites are BYTE-IDENTICAL to 7a8bfbb. Independently ran pytest/mypy/ruff.
  Sharp reviewer note: RS1's 60s/30s test WOULD BE VACUOUS without the config write — RS1's
  mutation check is exactly what proves it isn't. That is the argument for mutation-testing.
N1 (the 7th stand-in instance): ADR claimed the stdout-before-raise ordering "is asserted by
  tests/test_cli.py" — it was NOT; every --json resume test also passed --dry-run.
  **RS1 TOOK THE TEST ROUTE, not the weaken-the-claim route.** New test runs --json resume with NO
  --dry-run, asserts exit 2, parses stdout for stale_running_reset==1 and the projection path.
  MUTATION-PROVEN: moving _emit inside the `if dry_run:` branch fails it with JSONDecodeError on
  empty stdout; passes once restored. ADR-0076 now NAMES the test and records why it is the only
  non-dry-run one — so the next person cannot re-introduce the gap invisibly. 95 passed.

## BK1 re-review — APPROVE with one required follow-up.
FIX 1 ADDRESSED (anthropic.py:189-213). **BK1's `format`-sibling hazard CONFIRMED REAL, not
  rhetorical:** _rung's JSON_SCHEMA branch returns `format` as the ONLY schema payload, so the
  naive shortcut would have emitted an UNCONSTRAINED call recorded as JSON_SCHEMA — §13 row 37's
  exact silent-drift case. Avoided STRUCTURALLY (emptiness tested AFTER the merge), and the test
  asserts exact equality so an invented default cannot hide beside it.
Test strategy sound: model_copy bypasses validation and, since nothing sets revalidate_instances,
  the effort=None object also survives TierRoute's Pydantic boundary. Not vacuous in either world.
FIX 2 ADDRESSED (cli.py:10593; llm_router is a module-global at :10564 so the monkeypatch drives
  the REAL renderer). Mutations hold (1/2/2); 39 = 36 pre-existing + 3 added. No regression:
  usage.model_id still echoes target.model_id; no __init__ at all; discover() still wired.
IMPORTANT (real, routed): tests/test_llm_backend_anthropic.py:412 subscripts
  recorder["output_config"], which BK1's diff made CONDITIONAL -> post-ADR-0075 it raises KeyError.
  **CI would go red on main in a file the sibling lane never touched, misdirecting the bisect.**
  Falsifies BK1's "the tests do not change" claim. One line, BK1's.
**SECOND IMPORTANT FINDING RETRACTED BY ORCHESTRATOR — and the cause is instructive.** The reviewer
  flagged cache.py:123/:524 as a non-optional Literal that would ValidationError post-ADR-0075.
  I CHECKED agent/BK2 MYSELF: cache.py:123 is ALREADY `Literal[...] | None = None`, keyed as "" at
  :147, with an _as_effort narrowing helper at :367. **The reviewer anchored to `git show HEAD:` —
  exactly as MY BRIEF INSTRUCTED — and HEAD is main, where BK2 has not landed.** My own
  anti-hazard instruction produced a false cross-lane finding. Reviewer followed it correctly.
  **PROCESS FIX: for any claim about a file a SIBLING LANE owns, the brief must name the sibling's
  BRANCH as the ref, not HEAD.** Anchoring to HEAD is right for main-state claims and wrong for
  cross-lane ones, and I had not distinguished the two.
BK1's "reasoned, not executed" concern is UNDER-stated: it verified the ADAPTER against both worlds
  and generalised to the TREE. Finding 1 is the concrete counterexample. -> post-merge confirming
  run promoted from nice-to-have to REQUIRED GATE on the land checklist.

## DEM1 review — spec ✅ (all four criteria, no gaps) / ships with fixes. 0C / 2I / 3M.
DEM1's claim 1 FULLY CONFIRMED, every axis, NO LINE DRIFT — and `git grep operator=True HEAD --
  src/` is empty, so OPERATOR_REOPEN genuinely has no caller. Claims 2/4/5 also verified: `resume`
  is after the `*`; the only src/ transition() call is cli.py:9636 with no flags and no kwargs
  forwarding; both mutations genuinely kill :576 and :651; 8 ADR-0077 refs, zero stale, and 0077 is
  unclaimed on every other branch.
**I-2 (Important) — THE GATE'S OWN GUARANTEE IS FALSE.** ADR §4 and enums.py:84 claim "no call
  yields the demoted status without the record". But transition(..., resume=True) is PUBLIC and
  EXPORTED — and **SPEC.md:6807 names transition(), NOT demote(), as the demotion path.**
  state/repository.py (subtask 6's file) calls transition() at ZERO sites today, so its author will
  follow the SPEC, emit no finding, and demotions go SILENT — while DEM1's test named "cannot be
  taken without the finding" keeps passing. The exact failure the gate exists to prevent, arriving
  through the door left open. Routed: close the door, or amend the claim. Not left standing.
I-1 (Important): demote() accepts RUNNING and BLOCKED — both reach PENDING via ALLOWED_TRANSITIONS
  (HEAD:enums.py:35,39) BEFORE the resume branch. Subtask 6 demoting a still-BLOCKED Phase-2 row
  (legal — step 5 runs before step 6's blocked_by recompute) mints a PhaseDemoted finding claiming
  green work was discarded when NONE RAN. The :139 PENDING guard's own rationale covers it and is
  not applied; the :137 docstring states the opposite.
CLAIM 3 ADJUDICATED: keep the (status, PhaseDemotion) coupling — PhaseDemotion is I/O-free and
  imposes no transaction/ordering/call-shape constraint, so subtask 6 can still fold per-repo
  demotions (which it MUST: cli.py:7057,7066 fingerprint on (run_id, repo_id, kind) and UPSERT).
  Twelve lines buys a real behavioural test where the alternative is asserting a constant equals a
  string. Only the "mechanical, not a convention" CLAIM overreaches — that is I-2.
MINOR: §5.1 listing omits demote()/PhaseDemotion/PHASE_DEMOTED_KIND despite a "re-synced verbatim"
  claim; **`§13 row 46 (ii)` is a MIS-CITATION x4** — it is §12 item 46 (ii) at SPEC.md:7066, while
  §13 starts at :7072 and its row 46 is about rewrite rules; ADR §7 both disclaims and decides
  attempts retention.

## In flight (5): FD1 fix2, DEM1 fix1, BK1 follow-up, BK3 review, BK2 fix3 re-review

## BK1: COMPLETE (3665d1d -> c8060b8 -> 90fb4c0 -> 77b273c, re-review approved + follow-up closed)
Fixed the conditional-output_config KeyError as `recorder.get("output_config", {})` at what is now
  :418. **It checked the OTHER SIX output_config sites rather than generalising from the one the
  reviewer named** — all either pass an explicit effort or use a membership test, so that was
  genuinely the only breaking subscript. That is the correct response to a reported instance:
  look for the class, not just the case.
PROVEN IN BOTH WORLDS, EXERCISED NOT REASONED: with effort non-optional (its base) 39 passed; with
  the field made optional locally to mirror agent/BK2 (BackendTarget and CacheKeyParts -> `| None
  = None`, absent keyed as ""), 39 passed WITH the fix and **1 failed with the predicted
  `KeyError: 'output_config'` WITHOUT it**. Full lane green in both simulations (386 passed,
  1 skipped); simulation reverted, worktree clean.
BK1 SCOPED ITS OWN CLAIM HONESTLY, unprompted: it exercised its lane's eight test files against a
  LOCAL RECONSTRUCTION of ADR-0075, not against agent/BK2 itself, and its simulation touched only
  `effort` while BK2 reports 7 sites. So the post-merge gate REMAINS REQUIRED. That is the right
  way to report a simulation — name what it did not cover.

## CLEAN1 dispatched — three self-description defects
1. schema.sql:434 ("RESOLVED id") + models/tasks.py:47 ("as the backend reported it") — **these two
   comments caused the SAME bug in THREE independent lanes this round.** Two implemented it; the
   third avoided it only because it was warned explicitly. A documentation defect with a MEASURED
   cost. Comment-only fix; _stamp's behaviour explicitly NOT to change (several lanes depend on it).
2. settings.py:1400 `is None` -> `base_url: ''` and whitespace-only survive startup, violating
   §13 row 36's "fail at startup naming profile/tier/index/field".
3. The registry-gate error should NAME THE MISSING EXTRA (from BK3's residual). Most likely to be
   blocked by agent/BK1's hunks — CLEAN1 told to read agent/BK1's branch (NOT HEAD) and to report
   a collision rather than edit a region another branch is rewriting. **First brief written under
   the new cross-lane ref rule.**

## In flight (5): FD1 fix2, DEM1 fix1, BK3 review, BK2 fix3 re-review, CLEAN1
## COMPLETE: ST1, RS1, BK1

## BK2 fix-3 re-review — all three ADDRESSED, one new Important (NN1). -> fix round 4 sent.
N2 ADDRESSED: models/tasks.py:91/:516 and llm/cache.py:123 are `| None = None`; EVERY consumer in
  src/ (cache.py:147,302,367,534,593,606) is Optional-clean; cli.py genuinely untouched.
N1 ADDRESSED: corrected at openai_compatible.py:346 and both test docstrings (:208, :671);
  no "8 of the 12" survives anywhere.
N3 ADDRESSED: DECISIONS.md:299 struck as superseded; SPEC.md:6291-6293 de-`effort`ed with a
  do-not-re-add comment; remaining `effort: low` hits are local-profile openai_compatible targets,
  correctly untouched.
CACHE-KEY DISTINCTNESS VERDICT: HOLDS, and the reviewer checked the MECHANISM — compute() |-joins
  and hashes, so "" sits in its own DELIMITED FIELD and cannot alias "medium"; the test calls the
  REAL CacheKeyParts.compute() that cache.py:529/:595 use. "No migration needed" ALSO verified at
  source: state/schema.sql:440 is TEXT NOT NULL with NO CHECK, so '' inserts.
RECOMPUTE-THE-COUNT TEST VERDICT: GENUINE — :835 walks the full schema tree INCLUDING $defs and
  fails with "comment says 9; schemas say N". **The reviewer wrote an INDEPENDENT WALKER and got
  the same answer** (9/12; HEAVY 5/5, WORKHORSE 3/4, CHEAP 1/3; build_authoring root clean,
  $defs.BuildTargetProposal dirty; no untyped-object nodes escape the type guard). That test cannot
  rot the way the hard-coded 8 did — the right fix for a number that was already wrong once.
ALL FOUR SELF-CORRECTIONS CONFIRMED by AST-expanded collection per commit: 31 -> 32 -> 44 -> 50;
  two assertions break under restored strict (c23a08a:211 and :684); 5a32bdb:205 does assert
  "strict": True in test_json_schema_rung_sends_a_strict_response_format.
**NO VERIFY-A-STAND-IN SHAPE IN THIS ROUND'S BK2 CLAIMS** — schema-tree, cache-key, no-migration
  and wire claims each check the thing itself. Clean sheet after three instances in earlier rounds.

### NN1 (Important) — the SPEC still carries the value BK2 removed, where it would be restored FROM
SPEC.md:2889 still declares `effort: Literal[...] = "medium"` and :3294 still shows it
  non-optional. BK2 reconciled only the YAML EXAMPLE (:6291-6293), not the MODEL LISTING — and the
  listing is what someone reconciles models/tasks.py AGAINST. A spec-driven reconciliation would
  restore the fabricated default and **re-key the CHEAP cache a THIRD time.** Same failure mode as
  N3 one layer up: struck the stale config example, missed the stale type declaration.
  Told BK2 to SWEEP SPEC.md for every `effort` declaration rather than spot-fix — it has now been
  caught twice by a stale copy in a document it had already edited.

### LAND CONSTRAINT RECORDED (from the reviewer's judgment on the operator note)
One combined operator note is ADEQUATE **only because both commits are unlanded**, so an upgrading
  operator crosses `low -> medium -> absent` in ONE step and sees ONE cache dip, not two.
  **THEREFORE: BK2's effort commits MUST LAND TOGETHER.** If they are split across releases the
  single note becomes wrong and an operator sees an undocumented second dip. Added to land-plan.

## DEM1 fix round 1 — COMPLETE pending re-review. Commits 1d5744e, 221447d. 136 passed (+1).
I-2 ROUTE: **amended the claim, and checked closability FIRST rather than assuming.** DEM1's
  reasoning, which I accept: transition() is public and exported, and every candidate for closing
  the door (leading underscore, module-private alias, sentinel token) is "a convention wearing a
  mechanism's clothes" — building one would have RESTATED THE OVERSTATEMENT IN CODE, which is worse
  because it would LOOK enforced. So instead: SPEC §11.5 step 5 now names demote() and says
  transition(..., resume=True) "returns the status ALONE and would demote silently" — that was the
  ACTUAL defect, since subtask 6's author starts from the SPEC. ADR §4 retitled and explicitly
  RETRACTS the sentence. transition()'s docstring says DO NOT pass resume=True. PhaseDemotion
  carries an HONEST LIMIT paragraph.
**THE FALSE TEST IS DELETED.** It asserted a property that did not hold and would have kept passing
  while demotions went silent. A new test now PINS THE OPEN DOOR, so the gap is VISIBLE IN THE
  SUITE instead of contradicted by it. That is the right disposition for a test that lied.
I-1 confirmed exactly as reported; replaced with one `old not in RESUME_DEMOTE` condition. Six
  refusals tested, PLUS two POSITIVE assertions that transition() still accepts RUNNING/BLOCKED ->
  PENDING — so the strictness is PROVABLY demote()'s own, not inherited. Mutation: old guard
  fails :655.
M-2 verified BOTH DIRECTIONS: item 46 is at :7066 inside §12 (:7015); §13 starts :7072. The two
  surviving `§13 row 46` refs (PROGRESS.md:941, test_rewrite.py:221) are PRE-EXISTING, about
  rewrite-rule oscillation, CORRECTLY cited, left alone. Distinguishing "my mis-citation" from
  "someone else's correct citation of a different thing" is exactly the check that is easy to get
  backwards, and it did it.

### *** THE ROUND'S MOST GENERALIZABLE FINDING (DEM1's own closing concern) ***
"An overstated claim survived FOUR self-checks AND a mutation test — **mutations verify the
  IMPLEMENTATION, not the truth of a test's own NAME.**"
This is a genuine limit of the mutation discipline this round has been leaning on hard. A test
  named "cannot be taken without the finding" can pass, and pass under mutation, while the property
  in its name is false — because the mutation perturbs only the path the test exercises, never the
  OTHER doors to the same state. Mutation testing proves a test is not vacuous; it does NOT prove
  the test's name is true.
  -> Added to the DEM1 re-review brief as a first-class question, and a CLAUDE.md candidate at
     round close. This is the eighth distinct instance of the round's stand-in family, and the
     first one that a mutation check could not have caught.

## In flight (5): FD1 fix2, BK2 fix4, BK3 review, CLEAN1, DEM1 re-review
## COMPLETE: ST1, RS1, BK1

## BK2 fix round 4 — commit 2f5bce2 (DOCS ONLY). Sweep found 7 stale sites, not the 2 I named.
33 `effort` occurrences (21 SPEC.md, 12 DECISIONS.md), 7 stale. Beyond my two: SPEC.md:4170
  (annotated '' = declared none, no CHECK, no migration); SPEC.md:699 (prose "(haiku, low effort)"
  still asserted the removed value); SPEC.md:5589 (anthropic backend row now states the parameter
  is OMITTED ENTIRELY when effort is None); DECISIONS.md:291 (ADR-0009's "explicit effort per
  role" qualified); DECISIONS.md:1025 (ADR-0023's target shape effort -> effort?, since its
  NEIGHBOURS already carried `?` so its absence READ AS REQUIRED).
  **VINDICATES "sweep, don't spot-fix."** I named 2; there were 7. Left alone as accurate:
  cache-key composition prose, explicit effort: on openai_compatible example targets, models list.
CROSS-LANE SPEC EDIT TO WATCH: BK2 amended SPEC.md:5589 — the ANTHROPIC backend row — which
  describes a contract implemented in agent/BK1's file. It disclosed this and did NOT touch
  anthropic.py, arguing "leaving it silent is how the default gets reintroduced a layer down."
  I think that is right, but the re-reviewer is tasked with comparing it against what agent/BK1
  ACTUALLY implements. If they disagree it is land-blocking.
BK2 acknowledged and recorded the land constraint (c23a08a + 37a9e90 land together).

## BK3 review — spec ✅ no gaps / quality sound. 0C / 2I / 5M.
**REVIEWER MUTATION-TESTED ALL FOUR TRAPS in a `git archive agent/BK3` scratch tree** (baseline 81
  passed): served-id plumbed through -> echo test fails; model_id= kwarg deleted -> same test
  fails; strict injected -> fails in both files; required ctor arg -> collection TypeError at
  register_backend's cls(); guarded `import boto3` -> subprocess prints IMPORTED/True and fails.
  Traps hold under ATTACK, not just under reading. This is the verification bar for the round.
I1 tests/test_llm_backend_bedrock.py:32-38/:537/:548 — stub ClientError takes 1 arg, botocore's
  takes 2. **Once fleet[bedrock] is installed the stub is SKIPPED and the file goes 7 failed /
  33 passed** (reviewer reproduced with a faithful botocore fake). The tests pass everywhere EXCEPT
  the only machine that can actually run the backend.
I2 bedrock.py:409-414 — effort is non-optional at HEAD, so additionalModelRequestFields.
  output_config rides on EVERY call; a provider rejection is a 400 -> bare LlmError at :311-314,
  NOT a failover -> every call on that target fails its task outright. Omit-on-None is correct for
  the post-ADR-0075 world but at HEAD `None` NEVER OCCURS. Wire shape never live-verified.
**CLAIM 1 CORRECTED, and my own concern was unfounded:** `backends/` resolves as a **PEP 420
  NAMESPACE PACKAGE**, so a fresh clone of agent/BK3 alone DOES import — discover() works, 81 pass,
  mypy --strict clean and checking both adapters, hatchling still packages it. CI on a fresh clone
  is GREEN. I had recorded this as a CI hazard; it is not.
**LANDING NOTE (critical, orchestrator's to enforce):** whichever `__init__.py` wins the add/add
  conflict **MUST STAY IMPORT-FREE**, or client.py:363-365 swallows the resulting ImportError and
  returns an **EMPTY REGISTRY — silently.** The exact failure mode the round started with.
Claim 4 correct, naming mismatch safely deferrable (rawPredict + anthropic_version with no `model`
  key is genuinely the only endpoint reaching Anthropic publisher models; google-auth is a hard dep
  of the declared extra). Residual cosmetic, no failure scenario.
BK3's residual assessment AGREED and SMALLER than stated: settings.py:1393-1398 already emits
  "if it ships as an extra, install it" keyed to profile/tier/index; only the extra NAME is
  missing. Taken OFF the critical path — it does not gate BK1.

## In flight (5): CLEAN1, DEM1 re-review, FD1 fix2 re-review, BK2 fix4 re-review, BK3 fix1

## CLEAN1 COMPLETE — commits 8cea1f6 (comments), 088e5b2 (gate + tests). 92 passed.
Item 1: schema.sql:434 and models/tasks.py:47 reworded to state the INVARIANT AND THE REASON.
  Also noted _target_for matches on (backend, model_id) so effort silently degrades to the
  primary's, and that the served id needs a separate field or log line. _stamp UNTOUCHED as required.
Item 2: **REPRODUCED FIRST** — '', '   ', and a valid URL all loaded clean pre-fix. Gate now rejects
  blank/whitespace as well as None, reusing the existing message and `key` shape verbatim.
Item 3: **NOT BLOCKED.** CLEAN1 checked agent/BK1's branch (the new cross-lane ref rule, used
  correctly): BK1 touches cli.py only at 77/512-548/10590 and does NOT touch settings.py at all;
  the gate message is settings.py:1393. No collision. Message now names the extra
  (`pip install 'fleet[bedrock]'`) when the backend ships behind one — but **a genuine TYPO keeps
  the old wording**, because telling someone to `pip install anthropik` sends them after a
  nonexistent package. Thoughtful distinction; flagged to the reviewer to check it cannot misfire.
CLEAN1's VERIFICATION RECORD, in response to the round's stand-in warning — it checked the things
  themselves: 6 new tests confirmed collected **BY NAME**, not inferred from a count; the 4
  behaviour-changing assertions confirmed **FAILING against pre-fix settings.py**; schema.sql
  **EXECUTED INTO SQLITE** (19 cols, user_version 8) rather than eyeballed. Self-corrected mid-task:
  it first wrote a test against `settings.config_models`, which does not exist; real path is
  `settings.targets_for_tier()`.

## BK2 fix-4 re-review — NN1 ADDRESSED; two new, one LAND-BLOCKING.
NN1 verified hard: SPEC.md:2889 is **byte-identical (cat -A)** to agent/BK2:models/tasks.py:91; the
  second site MOVED to :3300 (BK2's own SQL-comment insert shifted it from :3294) and matches
  tasks.py:516 including default=None. Broader sweep surfaces only ADR-0075's narration and
  legitimate explicit YAML. All three "left alone" calls genuinely accurate — explicit `effort:` on
  openai_compatible targets is still correct, since **optional means MAY omit, not MUST**.
CROSS-LANE SPEC EDIT ADJUDICATED CORRECT: BK2's appended clause at :5601 matches agent/BK1 exactly
  (anthropic.py:191-213 omits the key entirely when target.effort is None — not null, not a
  default — and suppresses even an empty output_config). Editing the shared SPEC without touching
  anthropic.py, WITH DISCLOSURE, was right.

### OO1 (Important, LAND-BLOCKING) — and it closes a loop open since the round's FIRST report.
docs/SPEC.md:5601's surviving PRE-IMAGE clause asserts two behaviours NO lane implements:
  (1) "adaptive thinking" — no `thinking` key is ever constructed; git grep -i thinking on
      agent/BK1 hits only PROSE in llm/cache.py:4 and models/tasks.py:478.
  (2) "effort where the target's declared capabilities carry it" — ModelCapabilities
      (tasks.py:57-72) has NO effort field, and invoke forwards on `declared_effort is not None`
      alone, consulting NO capabilities.
  Failure: a reconciler adds `supports_effort` to make code match spec, thereby SUPPRESSING an
  explicitly declared `effort: high` and re-keying the cache a FOURTH time.
**THIS IS MY ERROR, AND IT IS THE ROUND'S CLEANEST LOOP.** These are EXACTLY BK1's round-1
  concerns 1 and 5 — it told me §7.7's table says the backend uses effort "where the target's
  declared capabilities carry it" while ModelCapabilities has no field that can carry it, and that
  forced tool_choice + default-on thinking was untested. **I adjudicated the IMPLEMENTATION
  question and never corrected the SPEC TEXT THAT RAISED IT.** The false claim stood all round.
  LESSON: when a worker reports "the SPEC says X but the code cannot do X", deciding the code is
  only HALF the adjudication. The SPEC sentence must be corrected in the same breath or it
  regenerates the defect.
OO2 (Minor) DECISIONS.md:301 `role -> {id, effort}` — the same required-shape assertion BK2 fixed
  at :1025, TEN LINES BELOW its own :291 edit. How it survived: BK2 swept for the TOKEN and fixed
  the sites it recognised, but this one encodes required-ness through SHAPE, not the word.

## In flight (5): DEM1 re-review, FD1 fix2 re-review, BK3 fix1, CLEAN1 review, BK2 fix5

## DEM1 fix-1 re-review — I-1, I-2, M-2, M-3 ADDRESSED; M-1 partial. 0C / 0I / 3M. "Ships."
AMEND-VS-CLOSE VERIFIED CORRECT: transition() is already re-exported; every closure candidate is a
  rename with breaking blast radius that still stops nobody, and the identified failure path was
  **DOCUMENTARY** — fixing the SPEC closes it, a sentinel would not have. DEM1's "convention
  wearing a mechanism's clothes" reasoning held under scrutiny.
THE FALSE TEST IS GONE, verified by absence: the old test name returns EMPTY from git grep across
  agent/DEM1 — renamed AND rewritten, false docstring paragraph removed, nothing still asserts it.
I-1 verified by RUNTIME MUTATION IN BOTH DIRECTIONS: restoring the old guard -> DID NOT RAISE;
  tightening ALLOWED_TRANSITIONS[RUNNING] instead -> fails at :665. So the positive assertions
  genuinely isolate the strictness as demote()'s own. The six refusals match on demote()'s OWN
  message ("is not a demotion"), which transition()'s message would fail — the discrimination is
  real, not incidental.
M-2 verified BOTH DIRECTIONS again: item 46 at SPEC.md:7094 inside §12 (:7043-:7100); all four
  sites now say §12 item 46 (ii); the two survivors (PROGRESS.md:941, test_rewrite.py:221) DO cite
  §13's row 46 ("Two rewrite rules fight over the same span", :7153) CORRECTLY. Not backwards.

### *** N-1: DEM1's OWN THESIS DEMONSTRATED ON DEM1's OWN TEST, BY CONSTRUCTION ***
test_state_models.py:669-683 — the name promises *SILENTLY*; the body asserts only the RETURN VALUE.
**The reviewer then CLOSED THE DOOR** — bound an audit side-effect into transition(), return value
  unchanged — **AND THE TEST STILL PASSED, while its name became false.**
That is an empirical proof of "mutations verify the implementation, not the truth of a test's own
  name", produced by construction rather than argument, on the very code that coined it.
Consequence: **ADR §4 bullet 4's tripwire promise (DECISIONS.md:6736) is PARTLY FALSE** — DEM1 wrote
  that the new test pins the open door; it pins the RETURN VALUE, not the SILENCE. An overclaim
  about an overclaim. Routed: prove the silence, or rename to what it proves and fix the bullet.

### N-3 — a stale SPEC CODE LISTING undermining the very fix that depends on it
SPEC.md:2073-2074's §5.1 listing keeps the PRE-CHANGE transition() docstring, dropping the
  DO-NOT-pass-resume=True warning that DEM1's ENTIRE I-2 remedy rests on. Listing order also
  differs from enums.py. **Same class as BK2's OO1/NN1, third occurrence this round:** a stale SPEC
  code listing regenerating the defect, because the listing is what code gets reconciled AGAINST.
  THREE lanes have now been bitten by this exact shape. Strong CLAUDE.md candidate.
N-2 :585 `..._and_nothing_else` loops THREE OF FIVE targets (:607), so widening to
  SUCCEEDED->SKIPPED passes both tests. "Keys are airtight; values are not."
DECISION: fixing all three Minors rather than deferring — nine subtasks build on this gate, so a
  test that overclaims HERE propagates into all of them.
ADR §8's numbers verified REAL (136 passed, 120 in test_state_models.py, mypy --strict clean).

## In flight (5): FD1 fix2 re-review, BK3 fix1, CLEAN1 review, BK2 fix5, DEM1 fix2

## BK3 COMPLETE — fix round 1, commit 2d527cf. 88 lane tests + 43 against a faithful botocore fake.
I1 FIXED AND REPRODUCED BEFORE ASSERTING: BK3 transcribed botocore's REAL exceptions.py (true
  MSG_TEMPLATE, _get_retry_info, signatures) onto PYTHONPATH so find_spec("boto3") succeeds and its
  own stub is SKIPPED. Pre-fix via git show: **7 failed / 33 passed — exactly the reviewer's
  independent numbers.** Post-fix 43 passed. Two agents converging on the same failure count from
  different fakes is strong evidence the fake is faithful.
I2 ROUTE: **do not send output_config at all**, and the argument against the alternative is the
  better half — making a rejection a failover trigger is wrong because _from_client_error's OWN
  docstring says a non-throttle 4xx means OUR REQUEST is wrong, so the next target reproduces it
  identically; failing over walks the whole tier into TierUnavailable and **disguises a wire-shape
  bug as an outage, poisoning the §13 rows 40/43 health signals with a fault no endpoint has.**
  IMPLEMENTATION WORTH COPYING: `_effort_to_send()` gated on a PREDICATE OVER
  `BackendTarget.model_fields["effort"]`, not a hard-coded flag — shut while the field is a
  non-optional Literal, permanently open once ADR-0075 lands, so it **SELF-REMOVES WITH NO
  LAND-TIME EDIT**. Verified live: "output_config sent at HEAD: False".
  Deliberately NOT applied to vertex — its body IS the Messages API body BK1 ships, so gating it
  would put a FAILOVER PAIR OUT OF STEP. Correct reasoning about a pair, not a file.
Three Minors fixed: vertex's except Exception narrowed to RequestException (an adapter bug must not
  masquerade as a transport failover); MalformedReply -> a dedicated UnmappedFinishReason(LlmError)
  in both files, since client.py only catches the former around _validate and it was PROMISING A
  REPAIR THAT CANNOT HAPPEN; mypy override narrowed off the google.* wildcard.
HANDOFF: settings.py:1400's is None also lets `region: ""` reach wave 7 — _REQUIRED_TARGET_FIELDS
  drives both from ONE TABLE, so it should be the same edit. -> routed to CLEAN1.

## BK2 fix round 5 — commit ddf52d9. OO1 swept, FOUR MORE instances found.
Beyond :5601: SPEC.md:6810 ("the harness uses it" — outright false); SPEC.md:3274 **plus BOTH src/
  MIRRORS (tasks.py:484, cache.py:4)** — "adaptive thinking forbids temperature pinning" used as
  the **PREMISE FOR CACHE DETERMINISM**; SPEC.md:7073 same premise; and **DECISIONS.md:290
  (ADR-0009), the ROOT the SPEC row derived from** — fixing :5601 alone would have left the loop
  open one layer up.
  **THE FALSE CLAIM WAS LOAD-BEARING FOR A DIFFERENT SUBSYSTEM'S REASONING.** The cache's
  determinism rationale rested on a model behaviour that does not exist. Re-reviewer tasked with
  answering whether the determinism conclusion still stands once the premise is removed.
ADR-0009 TREATMENT (assess at re-review): the original decision paragraph is PRESERVED, because an
  ADR records history, immediately followed by a `NOT IMPLEMENTED — do not reconcile code to this
  paragraph` block naming what ships instead. Rewriting an ADR's history is its own defect; a
  preserved false paragraph is what caused OO1. The marker's prominence is the whole question.
BK2 verified its own two claims FIRST: no temperature/seed/top_p/thinking key is constructed
  anywhere (the `seed` hits are Bazel query sampling); `.stream(` has ONE site, cache.py:504.
The rewritten row carries the guard OO1 predicted: ModelCapabilities must NOT grow supports_effort.

## CLEAN1 review — spec ✅ / sound. 0C / 1I / 5M. -> fix round 1 sent (+ the region handoff).
CLEAN1's four verification claims ALL HOLD: 6-test count reconciles BY NAME (3 parametrize ids +
  3 functions); the 4 pre-fix failures reconcile EXACTLY (3 blank ids + the extra-name test, with
  the two guard tests correctly passing BOTH sides); the self-correction is real (targets_for_tier
  at settings.py:1255; config_models exists nowhere). _stamp confirmed untouched.
**I1 — CLEAN1 fixed the two comments that lied but NOT the one an adapter author actually reads.**
  client.py:865 is THE INVITATION SITE: `usage.model_id or target.model_id` reads as "the backend
  may supply this", and _stamp's docstring says "attribute to the target that actually answered".
  An author greps for where model_id is SET, lands there, never opens tasks.py or schema.sql —
  so the fix misses the exact path that produced the bug three times. No lane touches client.py.
M2+M3 — the typo-vs-extra distinction is right ONE direction, gapped the other: a typo can never be
  misclassified as an extra (lookup keys on the literal string), BUT **anthropic and openai are
  CORE deps**, so a broken install yields "if it ships as an extra, install it" — sending someone
  after a `fleet[anthropic]` extra that does not exist. And _BACKEND_EXTRAS duplicates pyproject
  with NO TEST BINDING THEM.
M1 — a test overclaims its name: test_a_typod_backend_is_not_reported_as_a_missing_extra asserts
  only `"pip install" not in message`, so ANY unrelated UnresolvedReferenceError satisfies it.
REVERSE CHECK ON ITEM 1 CAME BACK CLEAN — the comment asserts a REQUIREMENT ON ADAPTERS, not an
  enforced property, and SAYS SO. That is the correct shape, and the distinction that bit 3 lanes.

## DEM1 COMPLETE — commit 04864af. N-1/N-2/N-3 fixed. 136 passed.
**N-1 RESOLVED BY PROVING THE SILENCE, NOT BY RENAMING** — and the argument is the round's best.
  DEM1 refused to concede a property that turned out to be ASSERTABLE, and it was assertable
  because of a fact it had already established for another reason: **enums.py imports STDLIB ONLY,
  so transition() can reach NO SINK.** An audit side-effect therefore has exactly two destinations,
  and both are now observed (every mutable module-level container snapshotted before/after;
  caplog empty), plus the return shape, **plus an assertion that no `fleet` import exists — which
  converts "there are only two routes" from a claim in its head into an assertion in the suite.**
  VERIFIED AGAINST THE REVIEWER'S OWN CONSTRUCTION: it ran both closure forms (audit registry
  inside the resume branch; a logging call), return value untouched — **both passed under the old
  test, both FAIL under the new one.** Renamed to
  test_transition_demotes_without_recording_anything_and_that_gap_is_known; ADR §4 bullet 4
  corrected; new §4.2 records the falsification AND the lesson.
  -> final re-review tasked with TRYING TO DEFEAT the new test with a THIRD closure form.
N-2 loop now DERIVED from RepoStatus with a len==5 assertion, so a new member enlarges it
  automatically; the {PENDING, SKIPPED} widening now fails at :618.
N-3 — DEM1 agreed this was the serious one: the listing carried the pre-change docstring, deleting
  the warning I-2 rests on, IN THE ONE ARTIFACT SUBTASK 6's AUTHOR RECONCILES AGAINST. Synced
  verbatim, order matched to enums.py, **verified programmatically after the edit, not by eye.**
ADR §8 is now a MUTATION TABLE: six mutations, all rejected designs or reviewed defects, six tests
  killed. That is the right shape for an ADR that governs nine downstream subtasks.

## FD1 fix-2 re-review — N1/N2/N3/N5 ADDRESSED, N4 adequate. 0C / 0I / 4 new Minor.
Reviewer EXECUTED its mutations (rsync copy into scratch, worktree's interpreter, worktree never
  written). cli.py:9251 genuinely inside `async with StateWriter(...)` at :9202; the e2e drives the
  REAL Typer app via run_pr and reads findings OFF DISK — deleting the drain fails it (assert []).
  WorkerError (base.py:359-374) really carries no tier, so scope:"run" is NECESSARY not defensive.
  N5's replacement is load-bearing: each guard killed by its OWN test individually, and pending==1
  now fails when flush()'s re-buffer is removed. Fix1's masking caveat is GONE.
null-vs-false confirmed "the only defensible choice" — false would be N1 sign-flipped and ACTIONABLE.
**N8 (Minor by severity, FIRST by importance): `tier=` has NO PRODUCTION CALLER — every shipped row
  is scope:"run" with throttling_observed:null.** The narrowed arm, the entire point of N1, is
  exercised only by tests. Combined with N9 (the production arm is untested), **the only arm that
  ships is the one with no test.** Routed: wire a caller or disclose it as unreachable.
N6 the docstring overclaims ("shape identical so consumers never branch" — false; throttling_observed
  is bool|None and failover_triggers_scope exists SO they can branch). N7 the drain sits AFTER the
  loop, so a mid-loop _write_pr_record raise still discards the buffer — N2's original defect
  surviving inside N2's fix.
TWO MORE TESTS OVERCLAIM THEIR NAMES: test_a_cheap_tier_throttle_does_not_contaminate_a_heavy_tier_
  outage_row (:695) **writes no row at all** — pure accessor test, unused tmp_path; and
  test_a_retried_failover_does_not_duplicate_its_events_row (:794) **has no events table**, so the
  named ON CONFLICT never executes. FD1's "4 tests across two files" claim was itself accurate.

## In flight (5): BK3 re-review, BK2 fix5 re-review, CLEAN1 fix1, FD1 fix3, DEM1 final re-review
## COMPLETE: ST1, RS1, BK1

## BK2 fix-5 re-review — OO1 + OO2 ADDRESSED. 3 new (PP1 Important, PP2/PP3 Minor).
OO1 verified: all five sites fixed INCLUDING both src/ mirrors (cache.py:4, tasks.py:478).
  `git grep -niE 'thinking|temperature|top_p' agent/BK2 -- src/` returns ONE hit — cache.py:6's
  "not a temperature=0", true and not a behaviour claim. All `seed` hits are bazel/query.py rdeps
  sampling. `.stream(` has one PRODUCTION site (the delegating call is cache.py:514; :504 is the
  `def` — the reviewer corrected BK2's own line number) plus one test; no phase consumes it.

### *** CACHE-DETERMINISM SURVIVES — the question I was most worried about ***
The argument had TWO INDEPENDENT LEGS. The removed one ("thinking forbids pinning") was false AND
  **never load-bearing**. Leg 2 survives at SPEC.md:6812-6814 and is CODE-BACKED: a role may be
  answered by a different backend next call, so no sampler story holds across transports — and
  backend/model_id ARE cache-key components. Modality weakens from "cannot pin" to "does not pin";
  the conclusion is NOT left unsupported.
  LESSON WORTH KEEPING: removing a false premise without checking what rested on it is how a
  CORRECT conclusion gets orphaned. Asking the re-reviewer this explicitly was worth it.

### *** PP1 (Important) — A MARKDOWN DEFECT THAT INVERTS THE FIX IT LIVES IN ***
DECISIONS.md:302-303: the blockquote's last line is UNTERMINATED, so **lazy continuation absorbs
  the following text — "No other provider…" AND the caption "Role->model assignment:" — into the
  NOT-IMPLEMENTED block.** Consequence is exactly backwards from intent: a reconciler reads the
  **AUTHORITATIVE TIER TABLE as dead**, deletes config/models.yaml:30/:34's `effort: high`, and
  **CAUSES the fourth cache re-key that OO1 exists to prevent.**
  The marker added to STOP the defect would PRODUCE it. A rendering-layer bug creating a semantic
  one — invisible in the source, which is exactly why "verify by rendering, not by reading" was
  the instruction I sent back.
PP2 DECISIONS.md:329-330's "stands unedited … three claims do not survive" is now FALSE and
  contradicts the new marker.
PP3 **BK2's own NEW sentence overclaims**: SPEC.md:6811 says no `seed` key is built "anywhere in
  src/", but bazel/query.py:213-289 builds one. Risk: a reconciler deletes the rdeps sample seed.
  Ninth instance of the round's overclaim family — this time introduced BY a fix FOR an overclaim.
ADR-0009 PRESERVE-AND-MARK ADJUDICATED RIGHT IN PRINCIPLE: content prominent and unmissable in
  isolation, and **NOT applying the pattern in SPEC/src/ was correct — annotation is precisely
  what a reconciler skips.** Only the execution had defects.

### LAND CONSTRAINT (orchestrator's): agent/BK1, agent/BK3 and main STILL CARRY THE FALSE TEXT.
Landing order matters. BK2's doc corrections must not be stranded behind branches that reassert
  what they remove.

## CLEAN1 fix round 1 — commit b45c0c2. All 6 findings + the handed-over region item. 96 passed.
**M1's EVIDENCE IS THE STRONGEST FORM SEEN THIS ROUND — it demonstrated the GAP, not just the fix.**
  Under Mutation A (an earlier check raises first so the rule-2 typo branch is never reached — the
  exact scenario the review named), the message becomes "profiles.default.HEAVY[0]: some other gate
  fired first", **the OLD assertion `"pip install" not in msg` PASSES, and the NEW `"anthropik" in
  msg` FAILS.** An old-passes/new-fails pair is the ONLY proof that a rewritten test is genuinely
  STRONGER rather than merely different. Mutation B (_BACKEND_EXTRAS.get(b,b)) also fails it.
  **This is the answer to the round's overclaim family: don't show the new test fails — show the
  old one passed on the same input.**
I1 CLOSED: _stamp's docstring PLUS an inline note AT THE ASSIGNMENT, naming the `or` as a default
  for an unset field rather than an invitation. Behaviour untouched.
M3 CLOSED: three causes split and ALL EXERCISED END-TO-END — extra absent
  (`pip install 'fleet[bedrock]'`), core dep absent ("CORE dependency… module failed to import;
  check the install"), typo (unchanged).
M2 CLOSED: test_backend_extras_matches_pyproject binds BOTH DIRECTIONS via tomllib; mutation-tested
  by dropping `vertex`. A one-way check would have let a NEW pyproject extra silently regress to
  the vague wording — i.e. the original defect.
M4 CLOSED: both constant exit_code assertions dropped (class constant, could not fail).
REGION ITEM: already closed by round 1's shared-loop edit, exactly as BK3 predicted —
  **and CLEAN1 VERIFIED rather than assumed**: against 7a8bfbb both blanks LOADED CLEAN; at HEAD
  both refused; test added. The cross-lane prediction and the verification both held.

## M5 ROUTED TO BK1 (cross-lane, orchestrator-brokered)
agent/BK1:llm/backends/anthropic.py:364 quotes the OLD comment VERBATIM ("the RESOLVED model id, as
  the backend reported it") — the very text CLEAN1 deleted. After both land, the quotation refers to
  a string not in the tree; a reader greps for it, finds nothing, concludes the docstring's
  rationale describes a fixed-and-gone condition, and **treats the surrounding warning as stale** —
  discarding the caution that is still load-bearing. Reviewer judged it cheaper for BK1 to drop the
  quote than for CLEAN1 to preserve wording it just proved harmful. Agreed and routed.
  A verbatim quotation of another module's comment is a cross-file coupling nothing enforces —
  worth remembering as its own hazard class.

## In flight (5): BK3 re-review, FD1 fix3, DEM1 final re-review, BK2 fix6, CLEAN1 re-review
##   (+ BK1 M5, trivial)

## BK1: COMPLETE AGAIN — commit 3f57b33 (M5 handoff closed).
BK1 found a **SECOND quotation** at test_llm_backend_anthropic.py:517, WRAPPED ACROSS TWO LINES so
  it escaped the single-line grep — **found by grepping its own files for the FRAGMENTS rather
  than trusting the reported site.** The right response to a reported instance, again.
**BK1 ALSO FOUND A FOURTH COPY OF THE OFFENDING TEXT: docs/SPEC.md:2845** carries the identical
  wording ("the RESOLVED model id, as the backend reported it"). ORCHESTRATOR VERIFIED DIRECTLY —
  it is still there on main, and CLEAN1's branch has correctly rewritten tasks.py:47 but NOT the
  SPEC copy. **If only tasks.py and schema.sql are fixed, the SPEC copy RE-SEEDS THE SAME BUG on
  the next read.** -> PENDING for CLEAN1, batched until its re-review lands (editing mid-review is
  the staleness trap I already avoided twice).
  So the comment that caused this bug in three lanes exists in FOUR places. Each lane that touched
  it found one more. Worth remembering: a wrong comment propagates by COPY, so fixing "the" site is
  almost always fixing one of N.

## FD1 fix round 4 — commit 0e4b5e7. 387 pass across 14 files.
N8 DISCLOSED, NOT WIRED — and the reasoning is structural, not effort: the only production call
  site is PhaseRunner's halt path, WorkerError (base.py:359-374) carries no tier, and `observed`
  must not be parsed; wiring means changing WorkerError or TierUnavailable's raise sites, the SAME
  cross-lane change already deferred for attempts.llm_failovers. **Disclosure in THREE places, not
  just the report**: a block-capital docstring paragraph naming the missing fields, the `_CAVEAT`
  text THE OPERATOR ACTUALLY READS, and the test names.
N9 FIXED — at run scope failover_triggers_recorded is now "unknown", matching throttling_observed:
  null; the row hands over the raw tier-keyed map and DECLINES EVERY DERIVED CLAIM.
  **FD1's own test-design insight, unprompted:** the shipped arm is tested through the real halt
  path **WITH A CONTAMINATED MAP**, because an EMPTY one would make "unknown" and "none"
  INDISTINGUISHABLE and hide the regression. That is the same reasoning as the old-passes/new-fails
  standard, arrived at independently.
N6 docstring corrected; N7 drain moved into a `finally` with an e2e injecting a mid-loop
  _write_pr_record failure and finding the row on disk anyway.
BOTH OVERCLAIMING TESTS FIXED: the contamination name now belongs to the test that WRITES AND READS
  the row (accessor test renamed, unused tmp_path gone); the uid test now PROXIES THE REAL
  REPOSITORY so the ON CONFLICT it is named for actually executes, asserting row count.

## BK3 fix-1 re-review — all five ADDRESSED, but I2's central EVIDENCE is not. 3 new Important.
I1 verified by BYPASSING BK3's fake entirely: real botocore 1.34.46 on PYTHONPATH gives pre-fix
  7 failed/33 passed, post-fix 43 passed. **Three independent measurements now agree on that count.**
  BK3's stub is also faithful on everything it touches (2-arg init, .response, .operation_name).
**N1 (Important) — "self-removes with no land-time edit" is FALSE.** With the sibling's real
  tasks.py+cache.py dropped in: **2 failed, 86 passed** — two tests go red on main **WITH NO MERGE
  CONFLICT TO EXPLAIN THEM**, the worst shape for a post-land failure. The predicate MECHANISM is
  sound (flips correctly, cannot flip early, stuck-shut only on a sentinel refactor); it is the
  TESTS that do not survive the transition the design was built for.
**O1 (Important) — the load-bearing claim is the ONE THING UNCHECKED.** At :415, replacing the
  predicate with a hard-coded `return False` — THE EXACT THING THE TEST'S NAME DENIES — leaves
  43 passed. The test asserts the OUTCOME, never that the outcome comes from a predicate.
**N2 (Important) — BK3 REINTRODUCED THE PROBE ERROR IT HAD JUST FIXED.** vertex test :64's new
  `requests` stub guards on `"requests" not in sys.modules` instead of find_spec, so it SHADOWS a
  real installed requests SESSION-WIDE (reproduced: hasattr(requests,"get") False after import).
  I1 was declared-vs-installed; this is imported-so-far-vs-installed. Same family, same lane, one
  round apart — evidence that naming an error class does not inoculate against its variants.
VERTEX ASYMMETRY CONFIRMED CORRECT, and for a BETTER reason than BK3 gave: agent/BK1:anthropic.py
  :196-212 is already ADR-0075-shaped, so **at HEAD anthropic ALSO sends output_config.effort on
  100% of calls** — vertex is in step with its failover partner both today AND after.

## BK2 fix round 6 — commit 3bbab1f (docs only). 298 passed.
**PP1 FIXED AND VERIFIED BY RENDERING — and BK2 VALIDATED THE PARSER AGAINST THE KNOWN-BAD STATE
  FIRST**, where markdown_it correctly reported line 303 absorbed. Only then did it trust the
  post-fix result: blockquote spans 295-309, line 310 blank, and Role->model assignment: (311), the
  tier table (313) and the following paragraph (319) all render TOP LEVEL.
  That is the correct use of a tool as evidence: prove the instrument detects the known fault
  before citing it as proof of absence.
CAUSE OWNED: round 5's replacement string stopped MID-LINE, so the original tail was appended
  inside the new blockquote. The restructure returns "No other provider…" to the Decision
  paragraph, splits the marker into scope + detail, and now states OUTRIGHT that the tier table
  below is authoritative and its `effort: high` values are LIVE CONFIG — refusing PP1's predicted
  misreading AT THE POINT IT WOULD OCCUR.
Swept both docs with the same parser: DECISIONS.md has **0** lazy-continuation absorptions.
PP2 reconciled ("preserved, not untouched", six surviving-failure claims not three). PP3 scoped to
  src/fleet/llm/ with bazel/query.py's rdeps seed NAMED so it is not deleted.
**PRE-EXISTING DEFECT FOUND WHILE SWEEPING, not BK2's: SPEC.md:475-477**, present since a1178f7 —
  a `> 1` meaning GREATER THAN is parsed as a blockquote and absorbs two lines. -> orchestrator's
  to route, pending confirmation from the re-review.

## In flight (5): CLEAN1 re-review, DEM1 fix3, FD1 fix4 re-review, BK3 fix2, BK2 fix6 re-review
## PENDING for CLEAN1 (batched): SPEC.md:2845 fourth copy; SPEC.md:475-477 pre-existing blockquote

## DEM1 fix round 3 — commit 483d950. COMPLETE pending final re-review. 136 passed.
Scan now catches the function-local import TWO INDEPENDENT WAYS: `.strip().startswith(...)` as text,
  and `co_names` STRUCTURALLY (IMPORT_NAME records the module name regardless of indentation).
**DEM1 DID NOT EXTEND THE ENUMERATION — IT INVERTED IT INTO A WHITELIST**, saying extending "would
  have been the same mistake a fourth time." TRANSITION_GLOBALS is asserted to be the COMPLETE set
  of names transition()'s body may reference, checked against co_names, plus __kwdefaults__,
  __defaults__, co_freevars, vars(transition). The insight: **a side effect must NAME something to
  reach it, so unpredicted forms trip it too.** That closes the space instead of enumerating
  escapes — the correct structural answer to "your test can be defeated N ways".
  All six of the reviewer's forms re-run as mutations; ALL SIX NOW FAIL.
BOUNDED WORDING states exactly what is pinned AND that "the enumeration is deliberately not claimed
  exhaustive", with §4.2 recording what is NOT pinned.
**AND IT WENT LOOKING FOR AN ESCAPE FROM ITS OWN FIX, AND VERIFIED ONE:** RESUME_DEMOTE bound to a
  `dict` SUBCLASS recording into an INSTANCE ATTRIBUTE keeps the test green while recording every
  demotion. **Documented in §4.2 as the boundary, DELIBERATELY NOT PATCHED**, with a principled
  reason: patching means freezing every whitelisted global's identity, "at which point the test
  asserts the absence of an ADVERSARY rather than a PROPERTY."
  That distinction — adversarial-only escape = documented limit; accidentally-reachable escape =
  defect — is the right frame, and I put it to the final reviewer explicitly.
Test renamed to `..._without_writing_a_record_or_reaching_a_sink`. (Narrowed again in the CR1
round to `..._without_writing_a_record_or_naming_a_new_sink`: "reaching a sink" is the
absence the seven §4.2 escapes defeat, and "naming a new sink" is the one the whitelist
equality actually proves. The name above is the round-B state, kept as history.)

## CLEAN1 fix-1 re-review — ACCEPTED. I1/M1/M2/M3/region all ADDRESSED. 4 Minor.
**M1's DISCRIMINATION INDEPENDENTLY VERIFIED**: under Mutation A the VERBATIM PRE-FIX BODY PASSES
  and the new body FAILS with exactly "profiles.default.HEAVY[0]: some other gate fired first".
  **REVIEWER'S CAVEAT WORTH KEEPING: Mutation B fails BOTH bodies, so B ALONE PROVES NOTHING** —
  A carries the verdict. When two mutations are cited as evidence, only the DISCRIMINATING one
  counts. A refinement on the old-passes/new-fails standard, not a contradiction of it.
M2 bidirectionality verified by FOUR mutations (drop extra from pyproject; add unmapped `azure`;
  drop mapping key; misspell key) — all fail. M3's three branches reachable and separately tested;
  anthropic/openai confirmed in [project.dependencies]; extras keys ⊆ SHIPPED so no extra-backed
  backend can fall into the core branch. Region verified BY EXECUTION (revert only the predicate ->
  both blank-region and all three blank-base_url cases fail).
M4 NOT ADDRESSED — sweep incomplete: `exit_code: Final = 2` is on BASE ConfigError
  (settings.py:139) and two identical unfailable assertions remain at tests/test_settings.py:252
  and :436 — **:436 is the SAME EXCEPTION CLASS as one already removed.** CLEAN1 removed the
  instances it had in hand rather than the CLASS. Told it to sweep on the CONSTANT.
THREE OVERCLAIMS, both directions: settings.py:1415's "# not a name we ship: a typo" is false for a
  registered-elsewhere third-party backend (neither shipped nor a typo); settings.py:112 asserts
  SHIPPED-minus-extras "is a core dependency" with NO TEST BINDING IT (the exact thing CLEAN1 did
  bind for _BACKEND_EXTRAS); tests/test_settings.py:529-546's docstring names `vertex` and claims
  it pins "the ones nobody wrote a test for" while exercising BEDROCK ONLY.

## CLEAN1 fix round 2 dispatched — 4 Minors + the two held items
  (SPEC.md:2845 fourth copy of the bug-causing comment; SPEC.md:475-477 pre-existing `> 1`
   blockquote absorption). Told to VALIDATE THE PARSER AGAINST THE KNOWN-BAD STATE FIRST, per the
   discipline the docs lane established, and to sweep SPEC.md the way DECISIONS.md was swept.

## In flight (5): FD1 fix4 re-review, BK3 fix2, BK2 fix6 re-review, DEM1 final re-review, CLEAN1 fix2

## FD1 fix-4 re-review — N6/N7/N8/N9 + both test-name defects ADDRESSED. 0C / 1I / 3M.
N8 verified: `grep record_backend_unavailable src/` = ONE caller (runner.py:625-628), no tier=;
  WorkerError (base.py:359-374) genuinely has no tier field. Disclosure present in ALL THREE claimed
  channels (findings.py:382-393 block-capital docstring, :87-89 _CAVEAT, test names at
  test_runner.py:1590/:1535). Disclose-over-wire stands.
N9 verified: `recorded` initialised "unknown" OUTSIDE the guard (:398-402); the shipped-arm test
  plants a REAL CHEAP RATE_LIMIT and asserts the map non-empty (test_runner.py:1630); _triggers is
  never cleared by flush(), so contamination SURVIVES to the row; regression to "none"/"partial"
  fails TWO INDEPENDENT assertions.
N7 verified: drain in `finally` at cli.py:9259 with the writer STILL OPEN; _drain_llm_findings
  catches Exception not BaseException so CancelledError propagates; the e2e drives the REAL Typer
  app, doubling only `registry` and `_write_pr_record` (module-global lookup, so the patch lands).
**O1 (Important) — THE CAVEAT NOW CONTRADICTS ITSELF, and it is a new overclaim inside the fix for
  an overclaim.** findings.py:423 writes the caveat UNCONDITIONALLY, but its text asserts "every row
  … has scope 'run'". A tier-scoped row therefore ships carrying a caveat DENYING ITS OWN `scope`
  FIELD. Latent today (nothing passes tier=) — **live the moment the sibling wires it**, i.e.
  exactly the future N8's disclosure exists for. One-line branch on `tier is None`.
O2 — FD1's report cites FALSE mutation evidence (task-FD1-report.md:499). The reviewer found both
  rewritten tests DO meet old-passes/new-fails ANALYTICALLY, but **neither was DEMONSTRATED**, and
  the cited evidence for (b) does not show what FD1 said. Also: test (a) was a **PURE RENAME SWAP**
  — bodies byte-identical, nothing got stronger; the name merely moved onto the row-writing test.
  A legitimate fix for a misnamed test, but not a strengthening, and it should not be described as
  one. Test (b) IS a real rewrite with strictly superset assertions.
O3 — `…fails_partway` never gets partway (fails on the FIRST candidate).

## BK2 fix-6 re-review — PP1/PP2/PP3 ADDRESSED, render INDEPENDENTLY CONFIRMED. 1I / 3M.
Reviewer re-rendered with markdown_it 3.0.0 **after validating the detector against ddf52d9**,
  where it reported exactly one absorption (L303). Post-fix token map confirms BK2's boundaries TO
  THE LINE: blockquote 295..309, line 310 blank, Role->model assignment: (311), tier table (313-317)
  and following paragraph (319-320) all at level==0. Cause confirmed. Marker judged good ON MERITS:
  names the table BY POSITION, calls it authoritative, forbids deleting its effort: high, and the
  values match config/models.yaml:30/34/53-56.
**PP4 (Important) — THE MARKER'S SCOPE LINE REINTRODUCES PP1'S RISK.** DECISIONS.md:296 scopes the
  NOT-IMPLEMENTED marker to "thinking and streaming ONLY", which **explicitly EXCLUDES :291's
  per-role-unconditional `effort` claim**. A reconciler reads the marker, sees effort is OUTSIDE its
  scope, treats :291 as live, and re-keys the CHEAP cache — the same misreading PP1 existed to
  prevent, arriving through the SCOPE SENTENCE OF THE FIX FOR PP1.
PP5/PP6/PP7 Minor: "two inline annotations" (there are THREE, :319); the "Six" closed count omits
  the now-unmarked shipped-false "no OpenAI-compatible shim"; and **"streaming … implemented
  nowhere in src/" is CONTRADICTED by client.py:274/:544, cache.py:504, tasks.py:66** — a NEW false
  claim about src/, in the document whose false claims about src/ took three rounds to remove.
CORRECTION TO BK2's OWN REPORT: it said it swept "both docs" with 0 absorptions. Independent sweep:
  DECISIONS.md 9 blockquotes / 0 absorptions; **SPEC.md 4 / 2 (:476, :477)** — which are BK2's OWN
  reported pre-existing instance. So a WRONG SUMMARY SENTENCE, not a missed defect.
PRE-EXISTING SPEC.md:475-477 CONFIRMED, blamed entirely to a1178f7, reproduced in that commit's
  tree. Damage is specific: the `> 1` threshold is lost ("any component of size" ends bare), the
  orphaned "1 becomes a CycleFinding" reads as *size == 1* — **INVERTING THE RULE** — and the
  tie-break ordering renders as a quotation, i.e. non-authoritative. Already routed to CLEAN1.

### PATTERN, now unmistakable and worth a CLAUDE.md line
**A fix for an overclaim keeps introducing a narrower overclaim.** BK2 (PP3, then PP4+PP7), FD1
  (N6, then O1), BK3 (I1's probe error, then N2's). In every case the new instance is a SCOPE or
  PROBE narrowing of the one just fixed. The corrective is not "be careful" — it is to re-run the
  ORIGINAL detector against the FIX, which is exactly what caught PP4 and O1.

## In flight (5): BK3 fix2, DEM1 final re-review, CLEAN1 fix2, FD1 fix5, BK2 fix7

## BK3 fix round 2 — commit d390dc7. 91 lane tests GREEN IN FOUR ENVIRONMENTS.
(bare venv / faithful botocore installed / real requests installed / both). mypy --strict + ruff
clean; `git diff` against client.py, tasks.py, cache.py EMPTY — it stayed in its lane.

**EVERY FIX CARRIES AN OLD-PASSES / NEW-FAILS PAIR ON THE SAME INPUT.** BK3 tabulated them:
  N1 sibling's real tasks.py+cache.py dropped in -> old **2 failed / 86 passed (matching the
     reviewer's measurement EXACTLY)**, new 91 passed
  O1 predicate -> hard-coded `return False` -> old 43 passed, new 1 failed
  O2 load-bearing monkeypatch deleted -> old passed, new 1 failed
  O3 client.py:532 widened to `except LlmError` -> old passed, new 2 failed
  N2 real requests on PYTHONPATH -> old shadowed (hasattr False), new True
  This is the discipline the round converged on, applied without being asked twice.
N1 also verified the gate GENUINELY FLIPS rather than merely satisfying tests: post-ADR-0075 open
  -> `effort: high` sent, unset omitted; at HEAD shut. Both effort shapes reconstructed locally,
  the ADR-0075 one TRANSCRIBED from agent/BK2:models/tasks.py:91 rather than approximated.

### BK3 CORRECTED MY BRIEF, and I want this on the record
I relayed a suggested mutation at `client.py:730`. **It cannot bite** — that `try` wraps `_validate`
  ONLY, so an exception raised from `invoke` never reaches it. The catch actually carrying the
  contract is **client.py:532's failover tuple**, which is what BK3 mutated instead. Its new test
  drives the REAL LadderModelClient over two targets with a `TransportError` CONTROL proving the
  failover path WAS reachable. Routed to the final reviewer to confirm or refute; if BK3 is right,
  I passed along a reviewer's suggestion without checking it — the round's own error, from me,
  again.

### N2 — THE PROBE ERROR RECURRED A THIRD TIME, AND BK3 FIXED THE CLASS
Audit found the same shape in the bedrock installer's four `sys.modules.setdefault` calls (botocore
  ships SEPARATELY from boto3). Both files now share ONE `_absent()` helper naming the three-way
  distinction explicitly:
      **find_spec = INSTALLED · sys.modules = IMPORTED SO FAR · pyproject.toml = DECLARED**
  That triple is the durable artifact of this whole sub-thread — three lanes lost time to
  conflating two of those three. Strong CLAUDE.md candidate, more useful than "check what's
  installed".
N3 REPORTED, NOT TOUCHED (sibling's file): cache.py:519-522 partitions bedrock rows by an `effort`
  that never reaches the wire. Harmless in the sense that it SPLITS rather than COLLIDES — but two
  identical calls differing only in declared-but-unsent effort MISS EACH OTHER. Reviewer asked
  whether "harmless" is the right word or the miss rate is material.

## In flight (5): DEM1 final re-review, CLEAN1 fix2, FD1 fix5, BK2 fix7, BK3 final re-review

## BK2 fix round 7 — commit a640500 (docs only). 182 passed.
PP4 FIXED: the marker now scopes to the WHOLE Decision paragraph "without exception", names all
  four claims individually, and quotes :291's phrase VERBATIM (`output_config.effort` per role) so
  the reference cannot be missed — mechanically confirmed present at :291 and named at :297. The
  authoritative table is now ITS OWN PARAGRAPH so the contrast cannot be skimmed past.
**BK2 NAMED THE PATTERN ITSELF, unprompted:** "PP4 was the second round running where the mechanism
  I added to prevent a misreading was the thing that would cause it." That is the round-wide
  pattern stated by the lane that produced three instances of it.
PP5/PP6/PP7 fixed, each verified first: annotations two->THREE; closed claims six->SEVEN,
  enumerated, the omission being "no OpenAI-compatible shim" — **shipped-false by this lane's OWN
  deliverable**; and the streaming claim corrected AT A FINER GRANULARITY, which is the right call:
  **the protocol IS implemented (client.py:274/:544, cache.py:504, tasks.py:66); what is false is
  that the anthropic backend STREAMS or that any phase CONSUMES it.** A coarse "implemented
  nowhere" would have been a new false claim about src/ in the very document whose false claims
  about src/ took three rounds to remove.
Re-rendered: blockquote 295-318, caption (320), table (322) and following paragraphs top level;
  0 absorptions.
**BK2 CORRECTED ITSELF UNPROMPTED:** round 6's "swept both docs, 0 absorptions" was wrong —
  SPEC.md is 2, the pre-existing instance it had reported in that same message. A lane catching its
  own summary error one round later, without being asked, is the behaviour this round has been
  trying to instil.
-> final re-review dispatched, with an explicit instruction to give a plain done/not-done verdict
   and NOT to manufacture an eighth round for a Minor that changes no reader's behaviour.

## FD1 fix round 5 — commit dc14db6. 388 pass across 14 files.
O1 FIXED: `_caveat(tier_known)` composes head + per-scope clause + tail — the run arm says
  "scope 'run'", the tier arm says "scope 'tier'" and states the map describes ONLY the exhausted
  tier; the row-43 refusal and the lower-concurrency advice are retained in BOTH.
  New test `test_the_caveat_never_contradicts_the_scope_field_of_its_own_row` asserts each arm's
  prose against THAT ROW'S OWN scope — **including the currently UNREACHABLE arm, "the one that
  goes live without anyone revisiting the file."** Testing the arm that cannot yet run is the
  correct instinct: an untested arm there would repeat the defect one layer out.
  Headline mutation: reinstating `_caveat(False)` unconditionally fails the new caveat test AND
  NOTHING ELSE — so the new test is genuinely the thing catching it, not a bystander.

### O2 — FD1 MEASURED ITS OWN FALSE CLAIM RATHER THAN RETRACTING IT
It reconstructed the OLD body from b05f243, ran it BESIDE the new one ON THE SAME INPUT, and found:
  **the fresh-uid mutation kills BOTH bodies, so it discriminates NOTHING** — which is exactly what
  round 3 wrongly cited as evidence. The DISCRIMINATING mutation is removing the
  (run_id, event_uid) dedup: old passes, new fails `assert 2 == 1`.
  It also confirmed test (a) was a **PURE RENAME SWAP** — bodies byte-identical, nothing
  strengthened — and struck the false sentence INLINE in the report rather than quietly editing.
  This is the CLEAN1 reviewer's caveat ("when two mutations are cited, only the DISCRIMINATING one
  counts") applied by a different lane to its own prior reporting, unprompted.
O3 fixed: _write_pr_record now delegates for the FIRST candidate and raises on the SECOND; the test
  asserts two calls AND that the first record landed — so it genuinely fails PARTWAY.

## In flight (5): DEM1 final re-review, CLEAN1 fix2, BK3 final re-review, BK2 fix7 re-review,
##                FD1 fix5 final re-review
## COMPLETE: ST1, RS1, BK1

## CLEAN1 fix round 2 — commit a868462. 98 passed. Strongest instrument discipline of the round.
**DETECTOR VALIDATED THREE WAYS BEFORE BEING TRUSTED:** fires on the known-bad state; SILENT on the
  already-swept DECISIONS.md; and **fires on a SYNTHETIC ABSORPTION INJECTED INTO A CLEAN FILE.**
  That third check is the one nobody else did — a detector that has never been shown to fire on a
  FRESH instance can still be silently broken. Post-fix `docs/*.md` all sweep to 0.
SPEC.md:475-477 FIXED by REWRAPPING so `>` is not line-initial — **identical words, identical
  order**, which matters because a silent reword there would change a NORMATIVE RULE (the lost `> 1`
  threshold made the orphaned "1 becomes a CycleFinding" read as size == 1, INVERTING it).
**AN UNFLAGGED FIFTH COPY FOUND: SPEC.md:4164**, the llm_cache DDL listing carrying `RESOLVED id`
  exactly as schema.sql did. So the comment that caused this bug in three lanes existed in **FIVE**
  places: models/tasks.py:47, schema.sql:434, SPEC.md:2845, SPEC.md:4164, plus two verbatim
  QUOTATIONS in agent/BK1. Every lane that touched it found one more.
**MERGE SAFETY VERIFIED BY EXECUTION, NOT LINE ARITHMETIC:** a sibling has a hunk whose leading
  context is the TAIL of the very comment block CLEAN1 rewrote at :4164, so the rewrite STOPS SHORT,
  leaving that lane's three context lines byte-identical — confirmed with `git merge-tree` against
  BOTH agent/BK2 and agent/DEM1 returning exit 0.
**THE MUTATION-B CAVEAT PAID OFF IMMEDIATELY, in a new form:** CLEAN1's first mutation of the new
  core-dep binding **SILENTLY NO-OP'D** — the regex missed a trailing comment — so its "pass" proved
  NOTHING. It caught this, re-ran against a VERIFIED-CHANGED file, and both mutations now genuinely
  fail. **New rule, generalising the caveat: a mutation must be shown to have ACTUALLY CHANGED THE
  CODE before its result means anything.** "The mutation passed" is worthless if the mutation
  didn't apply.
M4 swept ON THE CONSTANT, not on remembered sites: ~200 exit_code assertions examined, only :252
  and :436 match the unfailable class-constant pattern; both removed.
**CORRECT NON-EDIT, and the discrimination that matters:** SPEC.md:3275 and tasks.py:494 also say
  "RESOLVED model id" — but THERE it means the id the role ROUTED TO, and the sentence is CORRECT.
  Reported, not edited. **Editing a correct sentence because it matches a grep is the mirror-image
  error of missing a wrong one**, and this round has now seen both.

## In flight (5): DEM1, BK3, BK2, FD1, CLEAN1 — all five are FINAL re-reviews.
## Each carries an explicit plain done/not-done instruction and a warning not to manufacture
## another round for a Minor that changes no reader's behaviour.
## COMPLETE: ST1, RS1, BK1

## DEM1: DONE (verdict from final re-review). 0C / 0I / 4M, all one-line polish.
All six prior escape forms now FAIL. The two anti-import mechanisms confirmed **GENUINELY
  INDEPENDENT**, not redundant: revert the text scan to line-start and `co_names` still kills it;
  disable `co_names` and the text scan still kills it. And `importlib.import_module("fleet…")` is
  INVISIBLE to the scan, caught ONLY by co_names — a real second mechanism.
**DOCUMENT-DON'T-PATCH UPHELD, MORE STRONGLY THAN DEM1 ARGUED.** Reviewer found six MORE escapes —
  ALLOWED_TRANSITIONS as a side-effecting dict subclass; RESUME_DEMOTE[SUCCEEDED] as a FROZENSET
  SUBCLASS with side-effecting __contains__ (a value INSIDE a whitelisted global, identity
  untouched); a side effect on the ARGUMENT TYPE (RepoStatus.__hash__), which transition() never
  names; sys.setprofile at import; an audited wrapper over the PACKAGE RE-EXPORT in
  models/__init__.py (the test pins enums.transition, not the exported name); and a sink via an
  already-whitelisted attribute name in the body. **ALL ADVERSARIAL-ONLY** — and four bypass
  whitelisted-global IDENTITY entirely, so freezing identities **would not have closed the hole;
  it would have bought THE APPEARANCE OF CLOSURE.** DEM1's "cut 4 would repeat cuts 1-2" was right.
**THE ONE ACCIDENTALLY-REACHABLE WEAKNESS is of a different kind and is the sharpest catch:**
  test_state_models.py:747 is a bare set-equality assert **WITH NO FAILURE MESSAGE**, so an author
  who trips it while legitimately adding `logging` can widen TRANSITION_GLOBALS to go green —
  **closing the door silently, and going green while doing it.** The test's failure mode INVITES
  THE WRONG FIX. Routed as polish with an instruction to say so in the message.

## BK2: DONE (verdict from final re-review, round 7). 1 Minor, behaviour-neutral.
Reviewer re-rendered AFTER validating its detector against ddf52d9 (reproduces the known-bad L303):
  boundaries EXACT — blockquote 295..318, caption 320, table 322-326, next para 328-329, all
  level==0. **NO THIRD REGRESSION — the two-rounds-running pattern BK2 named is broken.**
Counts RE-DERIVED not accepted: three annotations, confirmed from a1178f7 as exactly three edits
  inside ADR-0009, with the `effort?` at :1067 CORRECTLY EXCLUDED as ADR-0017's. Seven claims,
  seven enumerated.
Streaming claim verified MEMBER BY MEMBER and is exactly right: client.py:274 protocol member,
  :544 LadderModelClient's real body feeding _heartbeats, cache.py:504 CachingModelClient
  delegating at :514, tasks.py:66 supports_streaming — while the anthropic backend has ZERO hits
  for `def stream|StreamEvent|stream=True` and calls sdk.messages.create NON-streaming, and
  `grep -rn "\.stream(" src/` yields ONE internal delegation with NO phase consuming it.
PM1 (Minor) — **an over-disclaim, the MIRROR IMAGE of what this lane spent four rounds removing:**
  DECISIONS.md:298-299's "treat the whole paragraph as historical" sweeps in three things TRUE AS
  SHIPPED (anthropic>=0.69, AsyncAnthropic, max_retries=4). Risk: a reconciler deletes
  max_retries=_MAX_RETRIES. Mitigated by :296-297's enumeration and :317's route to §7.7, which the
  code itself quotes at anthropic.py:100-101 — hence behaviour-neutral and the DONE verdict.
  Routed as final polish anyway: the lane has been fixing sentences claiming MORE than the code
  does; this one claims LESS.

## COMPLETE: ST1, RS1, BK1, DEM1, BK2 (5 of 8)
## In flight: BK3 / FD1 / CLEAN1 final re-reviews; DEM1 + BK2 polish

## DEM1 fully closed — commit fb45acc. 136 passed, mypy clean.
The co_names assertion now carries a failure message telling an author that tripping it means they
  CLOSED THE DOCUMENTED GAP and that widening TRANSITION_GLOBALS to go green is THE WRONG RESPONSE
  — **verified by mutation that it fires AND reads correctly.** Testing the wording of a failure
  message is unusual and correct here: the message's whole job is to prevent a specific wrong fix.
Plus the container-type qualifier, §4.2's "NEW name" narrowing (noting that freezing global
  identity would not have closed FOUR of the SEVEN escapes), and the two test-file generalisations.

## BK2 fully closed — commit dd64086.
Narrowed "historical" to the four enumerated false claims and NAMED the true remainder
  (anthropic>=0.69 at pyproject.toml:33, AsyncAnthropic at anthropic.py:215, max_retries=4 at
  :100/:218) with an explicit do-not-delete. Re-rendered: blockquote 295..321, line 322 blank,
  caption 323, table 325, following paragraphs 331/334 all top level; 9 blockquotes, 0 absorptions.

## BK3 final re-review — NOT DONE, "by three lines of text, no code."
**REVIEWER RAN EVERY MUTATION ON BOTH TREES** rather than accepting the pairs. Four of five hold
  exactly, including N1's 2-failed/86-passed -> 91-passed. Adr0075EffortShape is CHARACTER-EXACT to
  agent/BK2:tasks.py:91; the gate flips for real (effort=high -> {'output_config': {'effort':
  'high'}}, unset omitted, shut at HEAD); BOTH `return False` AND `return True` turn :435 red;
  `_absent` is at every probe site and the botocore third occurrence reproduces (old loses real
  botocore.exceptions, new keeps it).
**O2's PAIR DOES NOT HOLD: old = 1 failed/42 passed, new = 1 failed/44 passed.** The test O2
  flagged (:389) is BYTE-IDENTICAL old<->new, and the site BK3 actually mutated fails in the OLD
  tree too. Coverage IS closed at :488 — but there is no discriminating pair, and the report
  presents two mutations on two different tests as one. -> P1/P2/P3, text only.
**MY BRIEF WAS WRONG AND BK3 WAS RIGHT — CONFIRMED:** `invoke` is at client.py:704, TWENTY-FOUR
  LINES ABOVE the `try` at :728, so widening :730 to `except LlmError` yields 91 passed. It cannot
  bite. I relayed a reviewer's suggested mutation without checking it; BK3 checked and mutated :532
  instead. Second time this round I passed along an unverified claim.
N3 "HARMLESS" UPHELD, do not route: config/models.yaml at HEAD ships three anthropic targets and
  ZERO bedrock, so the miss needs two same-model_id targets differing only in effort. Residual is
  RECORD ACCURACY (llm_cache.effort rows claiming an untransmitted value), not a cache defect.

## LAND2 dispatched — the first land plan is STALE (5 branches at earlier commits; now 8, with
##   SPEC.md/DECISIONS.md/tasks.py/cache.py/settings.py/cli.py/schema.sql/pyproject/test_settings
##   all multi-claimant). Told to re-derive from REAL DIFFS and to say which of plan 1's
##   conclusions no longer hold.
## LESSONS dispatched — drafting the round's durable rules as a PROPOSAL file for the user to
##   apply to CLAUDE.md. Explicitly forbidden from editing CLAUDE.md (user's uncommitted edit).

## COMPLETE: ST1, RS1, BK1, DEM1, BK2 (5 of 8)
## In flight: FD1 + CLEAN1 final re-reviews; BK3 text polish; LAND2; LESSONS

## CLEAN1: DONE (final re-review verdict, no fourth round).
F4/M4 verified ON THE CONSTANT, not on remembered sites: test_settings.py now has ZERO exit_code
  refs; settings.py:140 is the ONLY `Final` exit_code in src/ with no subclass override. The other
  **216** assertions are correctly NOT the pattern — budgets.py:138/171/179 and cli.py:305-341 are
  ClassVars that DIFFER per subclass (they discriminate WHICH CLASS RAISED); the rest read runtime
  int|None fields. Distinguishing "constant, cannot fail" from "constant, discriminates" is the
  check that makes this sweep correct rather than destructive.
F1/F2/F3 ADDRESSED and genuinely bound. **Mutations discriminate, EACH WITH PROOF-OF-CHANGE FIRST**
  — and the reviewer's own first `openai` edit hit the SAME trailing-comment trap CLEAN1 had hit;
  the re-parsed dep list is what proved it landed. Extra mutation: deleting `"vertex": ("region",)`
  fails EXACTLY the two vertex ids and passes both bedrock, so F2's fix is real and targeted.
**RESOLVED-ID DISCRIMINATION CORRECT IN BOTH DIRECTIONS.** No surviving "as the backend reported
  it" anywhere; all FIVE copies rewritten; SPEC.md:2841's TokenUsage is identical to tasks.py:43-62.
  And SPEC.md:3284/tasks.py:494 mean the id routing RESOLVED TO — correct as written, so reporting
  rather than editing was right. **Doubly right: editing :3284 would have COLLIDED with BK2's :3291
  hunk.** The restraint that avoided a false fix also avoided a merge conflict.
RENDER: pre-fix renders `size 1 becomes a CycleFinding` (RULE INVERTED); post-fix `size > 1`.
  `o.split() == n.split()` -> **True**, i.e. a PURE LINE-BREAK MOVE — the words and their order are
  provably unchanged, which is what a normative rule requires. All **26** docs *.md sweep to 0.
Detector validated three ways INDEPENDENTLY by the reviewer, including the synthetic `> 2` injection
  into clean DECISIONS.md firing at L63-64.
merge-tree: BK2 exit 0, DEM1 exit 0 — and those are the COMPLETE SPEC.md sibling set. BK2's
  `@@ -4167` leading context is exactly the block tail CLEAN1 stopped short of.
Noted-not-flagged: SPEC.md:4182 drops "(or a log line)" that schema.sql/tasks.py keep — forced by
  the BK2 context constraint, nothing enforced either way.

## COMPLETE: ST1, RS1, BK1, DEM1, BK2, CLEAN1 (6 of 8)
## In flight: FD1 final re-review; BK3 text polish; LAND2; LESSONS; CKPT (PROGRESS.md draft)

## BK3: DONE — commit 366dbf9. 7 of 8 lanes complete.
It did NOT just fix the three named lines. It **swept every remaining pair claim in the report**:
  the four verified rows (N1, O1, O3, N2) and the I1 reproduction stand, and the blanket sentence
  "standard applied to every test touched" is now scoped to FOUR OF FIVE. Told about one false
  claim, it audited the class of claim — the same move BK1 made on output_config sites.
It re-measured O2 ITSELF rather than taking the reviewer's numbers: the flagged test is
  byte-identical old<->new; deleting its patch gives 1 failed/42 passed OLD vs 45 passed NEW; and
  the line round 2 actually deleted **is absent from the old tree entirely**. P3 verified the same
  way — `return True` left that test green, so the "today" claim was genuinely unasserted.
91 green in four environments, mypy/ruff clean, client.py/tasks.py/cache.py untouched.

## LESSONS proposal — CLAUDE-md-proposal.md, 180 lines. 12 accepted / 11 rejected.
Rejected 5 as ALREADY COVERED by CLAUDE.md and 6 as below-bar — and it named its two most arguable
  rejections rather than burying them, which is the behaviour I asked for.
  I ACCEPT BOTH ITS REJECTIONS: "use general-purpose not Explore for file-writing agents" is
  dispatch mechanics for the SDD template, not a durable engineering rule — it belongs in this
  ledger (where it already is), not in CLAUDE.md. "Commit early" is right that NO WORK WAS LOST and
  that Rule 10 covers checkpointing in a different medium.
It ADDED two beyond my candidate list: the general stand-in rule (9 counted instances, distinct
  from the probe triple) and the sweep-the-class rule split out from stale-listings — each too
  large to share four sentences with its neighbour. Correct calls; my list under-split them.

## Coordination artifacts dispatched (all read-only, none touch the primary checkout)
LAND2  — re-derive the land sequence from REAL DIFFS; plan 1 is stale (5 branches -> 8).
CKPT   — draft the docs/PROGRESS.md round checkpoint, with an explicit instruction not to soften
         the "not proven" section and to hold every sentence to what this ledger establishes.
READY  — pre-land state audit: verify every lane's self-report against actual repo state
         (worktree cleanliness, file lists, the __init__.py IMPORT-FREE check, ADR numbering,
         and each lane's "I did not touch X" claim). Explicitly FORBIDDEN from running pytest.
DLEDGER— draft INTEGRATION_HONESTY.md D-numbered entries for the PRE-EXISTING defects this round
         uncovered, excluding round churn. Told to account for agent/RS1's existing entry.

## FULL SUITE: still not run. Deliberately HELD until FD1's re-review completes and no other agent
## is active — concurrent pytest sessions reap each other's BAZEL_ROOT children. It is mandatory
## before the first land, and landing is blocked on the primary's uncommitted CLAUDE.md regardless.

## COMPLETE: ST1, RS1, BK1, DEM1, BK2, CLEAN1, BK3 (7 of 8)

## FD1: DONE (final re-review verdict). **ALL EIGHT LANES COMPLETE.**
O1 verified: findings.py:82-123 splits the constant into head/per-scope/tail via _caveat(tier_known),
  and the payload takes `_caveat(tier is not None)` at :452 — **THE SAME PREDICATE that writes
  failover_triggers_scope at :450**, so the caveat and the scope field cannot diverge by construction.
**THE UNREACHABLE ARM IS GENUINELY ASSERTED**: the test writes a REAL tier=ModelTier.HEAVY row,
  READS IT BACK OUT OF SQLITE (:887), asserts "scope 'tier'" present and "scope 'run'" absent,
  mirrored on the run row — and the two rows do not collide on the upsert key (fingerprint is
  phase+observed, :437).
HEADLINE MUTATION EXECUTED, not read: reinstating `_caveat(False)` gives **1 failed, 1193 passed,
  5 skipped**, and the SOLE failure is the new caveat test at :889. A mutation that trips exactly
  one test is the cleanest possible evidence that the test is the thing catching it.
O2 REPLACEMENT CLAIM VERIFIED BY RE-RUNNING IT: old body reconstructed from b05f243 beside the new
  — baseline 2 passed; the fresh-event_uid mutation makes **both fail** (non-discriminating, as FD1
  now says); removing ON CONFLICT (repository.py:1803) + UNIQUE (schema.sql:745) makes **old PASS,
  new FAIL `assert 2 == 1`**. The discriminating pair exists and is now correctly identified.
O3 verified end-to-end (test_pr_e2e.py:1038-1053): delegates on call 1, raises on call 2;
  instrumented run shows acme-lib-py WITH a pr_url and acme-lib-ts WITHOUT, two CapabilityDrift
  rows persisted.
The three known-open items are **disclosed in code an operator or maintainer reads**, not only in
  the report — which is the standard that decided the DONE verdict.

## === FULL SUITE BASELINE ON UNMODIFIED main @ 7a8bfbb: GREEN ===
**1305 passed in 546.94s (9:06). 0 failed, 0 skipped.**
`0 tests skipped this session — full collected coverage ran`
`bazel disk: peak 4.22 GiB (ceiling 6 GiB) · residual output bases 0 bytes · repository cache kept 1643 MiB`
Green per CLAUDE.md §6: xfail 0 AND a clean bazel disk line. **This is the mandatory pre-land
baseline, and it is the first full-suite run of the entire round.** Log:
scratchpad/fullsuite-main-baseline.log. Every post-land run now has something to be attributed
against — which was LAND1's whole argument for running it before step 1.

## SECOND SESSION-LIMIT KILL — four coordination agents died.
SURVIVED (written before the kill): land-plan-2.md (860 lines, complete), CLAUDE-md-proposal.md.
PRODUCED NOTHING, restarting: READY (pre-land audit), DLEDGER (defect ledger), CKPT (PROGRESS draft).
land-plan-2 measured **all 28 pairs with git merge-tree** and states its order:
  **ST1 -> CLEAN1 -> BK2 -> BK1 -> BK3 -> RS1 -> DEM1 -> FD1**
  Lane tips: ST1 92648f5, CLEAN1 a868462, BK2 dd64086, BK1 3f57b33, BK3 d390dc7, RS1 781a42f,
  DEM1 fb45acc, FD1 dc14db6. All eight clean of TRACKED modifications; wt-BK3 carries one
  UNTRACKED file (the deliberately-unstaged backends/__init__.py).
  Its H-12 is the hazard I would most likely have gotten wrong: **serialization is not just a
  pytest rule, it is the ROLLBACK PRECONDITION** — each lane rebases onto whatever main currently
  is, so landing the "safe" lanes back-to-back means three more rebases have happened before the
  first suite run can tell you the earliest one was fine.
  H-13: `FLEET_ALLOW_RAW` is the ONE legitimate FLEET_* variable (settings.py:1314-1320).

## PRE-LAND AUDIT (pre-land-audit.md) — three contradictions found, one is a real land hazard.

### *** CORRECTION: BK3's TIP IS 366dbf9, NOT d390dc7 ***
**BOTH I AND land-plan-2.md RECORDED THE STALE TIP.** 366dbf9 ("BK3 polish: scope one overclaiming
  docstring (P3)") is one commit ahead, ancestor-verified. It touches only
  tests/test_llm_backend_bedrock.py — exclusive to BK3, so no pair result changes — **but the
  plan's 28-pair matrix never measured it, and landing d390dc7 would have SILENTLY DROPPED IT.**
  I have corrected land-plan-2.md in place (verified: line 14 now reads 366dbf9).
  ROOT CAUSE, and it is mine: I recorded BK3's tip when its fix round landed, then dispatched a
  polish round, then never re-read the tip. The land plan inherited my stale number because I gave
  it the branch list. **A tip recorded before a later round is a stale tip** — the same
  verify-the-thing-not-the-stand-in error, applied to my own bookkeeping.
  This is exactly what a pre-land audit is for, and it justifies having run one.

### *** __init__.py IMPORT-FREE VERDICT: PASS, EITHER VERSION — the round's opening risk is closed ***
BK1 (14 lines, blob dd90aae6) and BK2 (9 lines, blob 2d0e2ab3) differ in DOCSTRING PROSE ONLY;
  below it both are a single `from __future__ import annotations` — **a COMPILER DIRECTIVE that
  cannot raise ImportError.** Neither imports a vendor SDK nor re-exports a sibling. So
  client.py:362-365's silent-empty-registry path is **UNREACHABLE by either version**. The add/add
  pick is therefore cosmetic, not load-bearing — which retires the hazard LAND1 flagged as the one
  nothing would catch (ADR-0074's hook does not fire on rebase or ff-only merge).
  wt-BK3's single untracked file is that same `__init__.py`, `git hash-object` =
  dd90aae66d183be1e00ce624469537633e9ae87c — **byte-identical to BK1's committed blob.**
  Deliberately unstaged, not lost work.

### Other verdicts
All eight worktrees merge-base to 7a8bfbb EXACTLY; seven return an empty status --porcelain.
ADR numbering CLEAN: 0075 BK2, 0076 RS1, 0077 DEM1, main at 0074 (74 headings). No collision,
  no gap, no other branch adds an ADR — the central-assignment fix worked.
PRIMARY: ` M CLAUDE.md` is the ONLY modified tracked file; untracked are the two plans under
  docs/superpowers/plans/. Landing cannot start until CLAUDE.md is resolved by the user.
CONTRADICTION 2: BK2's "did not touch cli.py/settings.py" holds only for **src/** — it modifies
  tests/test_cli.py (shared with RS1 and BK1) and tests/test_settings.py (shared with CLEAN1).
  Not a defect; a scope qualifier the land matrix already accounts for, but the CLAIM was
  unqualified and I had recorded it unqualified.
CONTRADICTION 3: a868462's commit MESSAGE cites settings.py:139 for `exit_code: Final = 2`; the
  actual line is **132**. Cosmetic, in a commit message, unfixable without a rewrite. Recorded.
VERIFIED TRUE: BK3's client/tasks/cache untouched claim; CLEAN1's `_stamp` behaviour unchanged
  (+28/-1, ALL docstring/comment); and client.py being CLEAN1-only across all eight branches.

## CKPT CAUGHT FOUR ERRORS IN MY FRAMING. All four accepted; ledger corrected below.
1. **BK3's tip** — it found the stale d390dc7 INDEPENDENTLY of the pre-land audit. Two agents
   converging on the same bookkeeping error from different directions.
   It further noted, correctly, that **BK3's "complete" is WEAKER than the other seven**: the
   ledger records no closing verdict for P1/P2, because I told BK3 "no further review round".
   -> **GAP NOW CLOSED BY ORCHESTRATOR VERIFICATION, not by asserting parity.** I read
   `git diff d390dc7..366dbf9` myself: **one file, tests/test_llm_backend_bedrock.py, +9/-4,
   DOCSTRING ONLY, no code.** It rewrites the P3 claim from "no output_config at all today" to
   "with the gate shut…", and — the part that makes it right — explains BOTH why patching the gate
   shut is correct (it keeps the branch tested after the sibling lands instead of silently becoming
   a no-op) AND why that same patching is why the claim must be scoped to the branch. It names the
   two tests that DO cover the mechanism and the real model's answer. Verified, and the asymmetry
   is closed at the level it existed: a text claim, checked as text.
2. **"orchestrator/stubs.py has no caller"** is TRUE but is NOT in progress.md — it comes from
   task-ST1-report.md:180,:365. CKPT re-verified independently (no src/ file on agent/ST1 imports
   it) and SAID SO rather than passing my framing through. Correct handling of an unsourced claim.
3. **"resume steps 2,4,5,6,8 remain unbuilt" is INFERENCE, not record.** The ledger states RS1
   built --repoll-prs + steps 3 and 7, and design-resume-step5.md decomposes the rest; it never
   enumerates those step numbers as unbuilt. My phrasing invented a precision the record lacks.
4. **"five places"** — the ledger says FIVE and then enumerates FOUR in-tree copies plus TWO
   quotations. CKPT used the ENUMERATION rather than the COUNT. That is the right call every time:
   **when a summary count and its own enumeration disagree, the enumeration is the evidence.**
   My "five" was a number I carried forward without re-deriving it from the list underneath it.

## STATUS: ROUND B COMPLETE. 8/8 lanes done, 42 commits across eight agent/* branches.
## Baseline green on main (1305 passed). Land plan v2 written and tip-corrected. Pre-land audit
## clean apart from the three recorded contradictions. ADR numbering clean.
## BLOCKED ON THE USER: ` M CLAUDE.md` in the primary — land-worktree.sh refuses a dirty primary.
## NOT PROVEN, and it is the headline: **no branch has ever been exercised by a full-suite run.**
##   The 1305 baseline is main-only. 42 commits, zero cross-lane regression evidence.

## DEFECT LEDGER DRAFT — 17 entries, D54-D70. INTEGRATION-HONESTY-draft.md. Primary untouched.
D54 is the most serious pre-existing defect: nothing in the harness could clear
  `budget_ledger.halted`. repository.py:1410 was the SOLE writer and only ever wrote 1, while the
  reservation CAS at :562 carries `AND halted = 0`. The documented recovery was a permanent no-op
  and a budget-halted run was TERMINAL absent hand-edited SQLite. Fixed on agent/RS1 (781a42f),
  **unlanded** — and the draft says "unlanded", which is the distinction I asked for.

### *** FOUR MORE CORRECTIONS TO CLAIMS I PROPAGATED. All mine. ***
1. **"client.py:532 never inspects exc.trigger" is WRONG — it DOES read it (:536-538) to LABEL the
   failover event; it never BRANCHES on it.** I stated the stronger, false version in at least
   three briefs and in this ledger. The defect is real and unchanged in consequence — a RATE_LIMIT
   still fails a target over exactly like a connection failure — but the mechanism I described was
   not the mechanism in the code. Corrected in-entry rather than dropped, which is right.
2. **"five places" for the cache comment: FOUR are pre-existing.** The fifth is two quotations in
   code BK1 WROTE THIS ROUND — i.e. round churn, not ledger material. This independently confirms
   CKPT's enumeration-over-count catch, from a different agent reading different evidence.
3. **"_unavailable names five modules": five CALL SITES, FOUR distinct modules.**
4. **The D54 anchor was wrong:** SPEC.md:6445-6450 is the `--raise-wave-budget` paragraph; the
   actual exit-3 recovery is at **:6594**. I supplied the wrong line range in the brief.
   All four were corrected IN-ENTRY rather than silently dropped — the ledger records the true
   claim, not a gap where a false one stood.
FOUR ENTRIES BEYOND MY LIST: D57 (`capabilities=` unwired), D60 (_emit_failover skips the
  EXHAUSTING target), D64 (`is None` blank-config gate), D66 ("adaptive thinking" premise),
  D69 (StubAbandoned), D70 (ruff).

## ============================ ROUND B CLOSED ============================
## 8/8 lanes complete. 42 commits across eight agent/* branches, none landed.
## Baseline GREEN on main @ 7a8bfbb: 1305 passed, 0 failed, 0 skipped, residual output bases 0 B.
## Artifacts: land-plan-2.md (tip-corrected) · pre-land-audit.md · CLAUDE-md-proposal.md (12
##   lessons) · PROGRESS-draft.md (25 open items) · INTEGRATION-HONESTY-draft.md (D54-D70).
## HEADLINE UNPROVEN: no branch has ever been exercised by a full-suite run.
## BLOCKED ON THE USER: ` M CLAUDE.md` in the primary checkout.
##
## ORCHESTRATOR ERROR TALLY, recorded because the round's whole subject was claim discipline:
##   two unverified reviewer claims relayed to workers (both caught by the worker);
##   one wrong effort decision (YAML text checked instead of the resolved value);
##   one stale branch tip propagated into the land plan;
##   one SPEC sentence left standing after adjudicating only the code;
##   four claim-precision errors caught by the ledger and checkpoint drafts.
##   Every one was caught by a subagent, which is the argument for the review structure.

## ================== LAND SEQUENCE IN PROGRESS (user authorised) ==================
00f68ed  CLAUDE.md committed (user's edit: full-suite estimate 9 -> 15 min). Primary clean.
  NOTE, measured today and worth reconciling later: the baseline run took **546.94s = 9:06** with
  a WARM repository cache (kept 1643 MiB). The 15-min figure is plausibly a COLD-cache number —
  CLAUDE.md itself records 179s vs 17s for a cold repository cache. Not corrected; it is the
  user's edit and both figures can be true under different cache states.

STEP 1 ST1     -> main 5c52ee5. Rebase clean. VERIFIED: test_stubs.py 32 passed, `import
                  fleet.orchestrator.stubs` clean, diffstat names exactly the two new files.
                  (Anchors not recorded for this step — my miss; recoverable from reflog. Recorded
                  for every step after.)
STEP 2 CLEAN1  -> main a9afe9d. Rebase clean. VERIFIED: settings+schema_sql+migrations 82 passed;
                  SCHEMA_VERSION still 8 (CLEAN1's schema.sql edit is comment-only, as claimed).
STEP 3 BK2     -> main eabfcdb. Rebase clean, as predicted (__init__.py did not yet exist on main).
                  VERIFIED: 194 passed across openai_compatible+settings+cli+llm_cache; config
                  probe prints profiles ['default','local'] and default 'default'; **both BK2 fix
                  rounds present on main = the together-or-not-at-all constraint satisfied by
                  landing the branch as ONE invocation**, exactly as the plan required.
STEP 4 BK1     -> main 6d5a4a8. **Rebase HALTED on the add/add for llm/backends/__init__.py,
                  exactly where land-plan-2 said it would.** Resolved IN THE WORKTREE (never by
                  hand-committing in the primary — ADR-0074's hook refuses that) by copying BK1's
                  blob saved BEFORE the rebase rewrote the branch. **Copying the saved file rather
                  than picking --ours/--theirs sidesteps the inversion the plan warned about.**
                  git hash-object = dd90aae66d183be1e00ce624469537633e9ae87c both before and after
                  -> BK1's version, as decided. Rebase then clean through all 5 commits.
                  POST-LAND IDENTITY VERIFIED ON main: __init__.py hashes dd90aae6, and its only
                  import line is `from __future__ import annotations` (line 8 is docstring prose).
                  **The empty-registry hazard is closed on main, not just on a branch.**
                  FULL SUITE RUNNING (mandatory here — BK1 alters the startup path of every CLI
                  invocation). Log: scratchpad/suite-04-bk1.log

STEP 4 VERIFY   FULL SUITE GREEN: **1439 passed in 867.47s (14:27)**, 0 failed, 0 skipped,
                  `bazel disk peak 4.22 GiB · residual 0 bytes · cache kept 1645 MiB`.
                  Baseline was 1305 -> **+134 tests, all green.**
                  **THE USER'S CLAUDE.md EDIT WAS RIGHT AND MY NOTE ABOVE WAS WRONG.** The 9:06
                  baseline ran with warm Bazel OUTPUT BASES; my own lane-scoped verification runs
                  between land steps reaped them (pytest_sessionfinish deletes every BAZEL_ROOT
                  child except repos/), so this run rebuilt them — 14:27. **~15 min is the honest
                  figure for a suite run in a normal working session**, which is exactly the
                  condition the doc describes. Retracting my "plausibly a cold-cache number".
  IC-1 PASS  test_profile_flag_selects_the_profile_every_role_resolves_through = **PASSED**, not
             SKIPPED. Landing BK2 BEFORE BK1 meant the skip NEVER EXISTED on main for even one
             commit — the ordering insight from land-plan-2 §2, confirmed empirically.
  IC-2 PASS  `before: []` then `after: ['anthropic', 'openai_compatible']`. **The empty first line
             is the proof that matters**: the registry is populated by the pkgutil WALK, not by an
             accidental eager import. That is trap T1 — discover() having zero call sites — closed
             and demonstrated on main.
  IC-3 PASS  AST parse of __init__.py: exactly ONE non-docstring statement,
             `from __future__ import annotations`. Import-free, so client.py:363-365's
             silent-empty-registry path is unreachable.

STEP 5 BK3     -> main 36984cb. **land-worktree.sh REFUSED at first** — wt-BK3 carried the
                  untracked __init__.py it had deliberately never staged. Correct refusal.
                  Resolved by VERIFYING IDENTITY FIRST: wt-BK3's copy hashes
                  dd90aae66d183be1e00ce624469537633e9ae87c, byte-identical to main's (BK1's)
                  committed blob, so removing it is lossless — BK1 now supplies the file.
                  Removed, worktree clean, land succeeded.
                  **THE STALE-TIP CORRECTION PAID OFF:** main's tip is 36984cb "BK3 polish: scope
                  one overclaiming docstring (P3)" — the commit both I and land-plan-2 had
                  originally omitted. Had I landed the recorded d390dc7 it would have been dropped
                  silently. Verified present by grep, not assumed.
                  BACKEND REGISTRY ON MAIN: all FOUR backend files present
                  (anthropic/openai_compatible/bedrock/vertex); discover() registers
                  ['anthropic','openai_compatible'] ONLY — **correct, not a defect**: find_spec
                  confirms boto3=False and google absent, so those two modules fail import and are
                  simply not registered (client.py:366-369, trap 3 as designed).
                  FULL SUITE RUNNING. Log: scratchpad/suite-05-bk3.log

STEP 5 VERIFY   FULL SUITE GREEN: **1530 passed in 1186.74s (19:46)**, 0 failed, 0 skipped, bazel
                  disk clean. BK3's +91 all green, and the reason this suite was MANDATORY — BK3's
                  test modules install stub boto3/google.auth entries into sys.modules at import
                  time, which a lane-scoped run cannot expose — produced **no leak**: the coverage
                  line still reads "0 tests skipped this session".
STEP 6 RS1     -> main 74dc7bc. Rebase stopped on docs/DECISIONS.md, as predicted.
                  **A SUBTLETY THE PLAN DID NOT ANTICIPATE, and the rebase queue settled it:** the
                  conflicting commit was `eedc24b` (RS1 fix round 1), which still carried the ADR
                  labelled **0075** — the renumber to 0076 lives in a LATER commit, `f385b19`.
                  I checked `.git/worktrees/wt-RS1/rebase-merge/git-rebase-todo` and confirmed
                  f385b19 was still queued BEFORE resolving, so keeping both blocks (transiently
                  two 0075 headings) was correct — the next commit renumbers it. Resolution was
                  mechanical: delete the three marker lines, keep BK2's block then RS1's.
                  VERIFIED after: headings read 0075 then **0076**, in order; cli +
                  config_keys_are_read 152 passed (the KNOWN_INERT deletion is a free integrity
                  check on the rebase itself); `fleet resume --help` exit 0.
STEP 7 DEM1    -> main ebd1624. Rebase stopped on docs/DECISIONS.md again. HEAD side carried
                  0075+0076, DEM1's side only 0077 behind a `---` separator — clean disjoint
                  append, resolved the same way. VERIFIED: 0075, 0076, 0077 strictly ascending,
                  exactly one 0077; test_state_models.py 120 passed.
STEP 8 FD1     -> main 6a41840. **Rebase CLEAN, no conflict** — the only lane that needed none.

## ==================== LAND SEQUENCE COMPLETE ====================
## main = 6a41840, **43 commits** landed from 7a8bfbb. Primary clean.
## FINAL FULL SUITE: **1575 passed in 1077.73s (17:57)**, 0 failed, 0 skipped,
##   `bazel disk peak 4.22 GiB (ceiling 6) · residual output bases 0 bytes · cache kept 1645 MiB`.
##   Baseline 1305 -> 1575 = **+270 tests, every one green.**
## All four backend files on main (anthropic, openai_compatible, bedrock, vertex).
## 77 ADRs, numbering unbroken. Eight agent/* branches merged; ten worktrees still present.
##
## WHAT THE PLAN GOT RIGHT, and it was nearly all of it:
##   - the ST1-first rehearsal (the mechanism's dirty-primary gate bit at BK3, where it was cheap)
##   - CLEAN1 before BK1, so main never carried the registry narrowing without its remedy text
##   - BK2 before BK1, so BK1's skipif NEVER EXISTED as a skip on main — IC-1 passed first try
##   - BK2 landed as ONE invocation, satisfying together-or-not-at-all automatically
##   - the add/add halting exactly where predicted, and copying the saved blob rather than picking
##     --ours/--theirs sidestepping the inversion it warned about
##   - three DECISIONS.md stops, all mechanical, none needing prose reconciliation
## WHAT IT MISSED: the ADR-renumber-in-a-later-commit interaction at step 6 (harmless, and the
##   rebase todo resolved it), and BK3's untracked file blocking its own land (also harmless).

## === POST-LAND: GITEA PUSH + TEARDOWN (user authorised) ===
GITEA STATE, read WITHOUT auth via the documented bare-repo trick
  (`git -c safe.directory='*' --git-dir=/mnt/storage/gitea/data/git/repositories/redmage/
   swe-repo-harness.git`): container up 7 days, default branch `main`, remote main = **32365cf**.
RELATIONSHIP TO LOCAL: 32365cf **IS an ancestor** of local main -> clean fast-forward.
  **91 commits ahead, 0 behind.** That is round A's backlog plus round B's 43 — the remote was
  never pushed after round A either. No divergence, so no force, no merge, no history rewrite.
AUTH: origin is http://localhost:3001/... with NO credential helper configured; a probe with
  GIT_TERMINAL_PROMPT=0 fails with "could not read Username" rather than hanging — which is the
  safe way to establish that fact (round A's ledger records a bare `git fetch origin` HANGING on
  the prompt). Push therefore needs the documented mint -> push -> revoke cycle.
TEARDOWN SAFETY PRE-VERIFIED: all NINE agent/* branches (ST1, CLEAN1, BK2, BK1, BK3, RS1, DEM1,
  FD1, and the leftover WT1-example) are ancestors of main. Nothing unmerged is at risk.
  Note the branches were REBASED IN PLACE by land-worktree.sh, so each agent/* now points INTO
  main's history — they are redundant refs, not independent work. Pushing them would add nothing.
ORDER CHOSEN: refresh docs -> commit docs -> ONE push covering everything -> then teardown.
  Rationale: teardown is the only destructive step, and it comes after the remote has the work.
  The two untracked round plans (docs/superpowers/plans/sdd-backlog-{a,b}.md) go in the docs
  commit — they are the round's own specification and belong with the record.

## === ROUND B FULLY CLOSED ===
DOCS REFRESHED AND COMMITTED -> 5feb1e7. The refresh was NOT a copy of the drafts: landing
  falsified EIGHT of their claims, and the agent re-measured rather than carrying them forward.
  Open items 25 -> **20**: seven closed by landing (nothing-landed, no-full-suite, the dirty-tree
  block, the two manual merges, BK1's reasoned-not-executed caveat, BK3's incomplete P1/P2
  verdict, BK3's never-observed sys.modules leak) and TWO ADDED (stale worktrees, the ruff delta).
  D-entries: **12 changed status** — nine flipped from "FIXED ON agent/<lane> — NOT LANDED" to
  "FIXED, LANDED (<post-rebase sha>)", three partial sub-statuses relanded. D57/D58/D60/D62/D70
  stay OPEN, **re-verified at 6a41840** rather than assumed still-open.
  NUMBERS IT CORRECTED BY MEASURING: D70's ruff hunks 58 -> **60** on main; "four _unavailable
  sites" -> **three, two modules**; "19 _mapped_errors sites" -> **22**; the KNOWN_INERT key is
  `pr.merge_wait_timeout_s`, NOT `open_pr_max_age_s`; §13 row 38's "max_context has 5 hits" -> 11.
  AND A SIXTH COPY OF THE COMMENT: D66's "all five sites corrected" holds for docs/ and src/, but
  **tests/test_llm_cache.py:4 still carries the clause.** Every single pass over that comment has
  found one more copy. Recorded as open rather than quietly fixed.
  > **Editorial note (2026-08-20, round C, lane DOCSTALE).** This entry is a historical record of
  > round B's close and is left as written — it was true at 5feb1e7. It no longer describes `main`:
  > `c7f72c6` (round C) rewrote `tests/test_llm_cache.py:4`'s docstring to state the retraction
  > itself rather than the false premise. See `docs/INTEGRATION_HONESTY.md` (D66) and
  > `docs/PROGRESS.md` open item 14 for the matching corrections on those two live documents.
  §38 numbering: it also resolved the §37f reservation of "38" for the research-38/review-38
  thread, confirming by `git log` that the thread has no commit past 32365cf, releasing the number,
  and demoting the pending reconciliation to open item 20 instead of a held slot.

GITEA PUSH: **32365cf..5feb1e7 main -> main**, clean fast-forward of 91 commits, exit 0.
  Verified INDEPENDENTLY by reading the bare repo (no auth): remote main == local main == 5feb1e7.
  Auth handled by the documented one-shot cycle: minted a `write:repository` token, wrote it to a
  600 cred file in the scratchpad, pushed with `-c credential.helper="store --file=…"` so the
  token never entered a URL or the reflog, then backed up the Gitea DB in-container and revoked
  by name (rows 1 -> 0) and shredded the cred file. Push output was sed-scrubbed of any
  `user:pass@` form as a second guard. **The token never appeared in any output.**

TEARDOWN: all eight round-B worktrees removed with --delete-branch; `git worktree prune` run;
  primary tree clean (0 entries) at 5feb1e7.
  **LEFT DELIBERATELY: wt-WT1-example / agent/WT1-example** — a round-A tooling-test leftover at
  4b22f3b, merged into main and therefore lossless to remove, but NOT this round's artifact and
  not clearly in scope for "tear down the worktrees". Flagged to the user rather than assumed.
  It is also open item 20-adjacent in §38's list, so it is on the record either way.
