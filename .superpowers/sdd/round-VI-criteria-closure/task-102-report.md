# Task 102 report — §12.43's last two residuals (TEST-ONLY)

Round VI, thirty-sixth wave. Branch `agent/roundvi-task102`, worktree at
`/home/redmage/swe repo harness worktrees/wt-roundvi-task102`.

## Scope recap

`docs/CRITERIA_PLAN.md` §43 named exactly two residuals left before §12.43 counts toward
`<n> of 48`:

1. Cases (i)-(iii)'s "on the fixture fleet" framing clause — previously proven only at
   unit/component level (`tests/test_llm_failover.py`, `tests/test_llm_findings.py`).
2. Case (iv)'s own paired `--deterministic-only` clause, re-run against
   `tests/test_heavy_tier_outage_e2e.py`'s own broken-HEAVY config.

Both are TEST-ONLY. `src/fleet/` is untouched — `git diff --stat src/` is empty at every commit
on this branch and after every mutation trial below (each mutation was applied, run, and
reverted in the same investigation, never committed).

## What was built

All four new tests live in `tests/test_heavy_tier_outage_e2e.py` (grew from 411 to 874 lines),
reusing that file's existing two-repo fixture-fleet skeleton (`_make_workspace`, `_write_config`,
`FIXTURE_REPOS`, `_classify_responder`) per the brief. One shared test-fixture file
(`tests/fixtures/llm/stub_openai_server.py`) gained a small, backward-compatible addition:
`StatusReply` (a frozen dataclass carrying an explicit HTTP status code), because the shipped
stub server only ever answered HTTP 200 — case (ii) needs a genuine 429 (the `openai` SDK's
`RateLimitError` path, distinct from the plain-body 5xx path a bare `Mapping` reply cannot
reach). Every existing caller of `queue_reply`/`set_responder` is unaffected: a bare `Mapping`
still means "200, this body", exactly as before.

### Case (i) — `test_case_i_connection_failover_on_the_fixture_fleet`

Reuses `_make_workspace` UNCHANGED: `heavy_a` = the same unbound loopback port arm 2 already
uses (`127.0.0.1:1`), `heavy_b` = a real stub server. `fleet scan` (real classify, no
`--skip-classify`) completes for both repos; asserts one `backend_failover` event per repo
(trigger `CONNECTION`, naming both targets), the `llm_cache` row's second-target attribution,
and `phases.attempts`/`transient_retries` unchanged (1, 0) for both repos — read back from real
SQLite state after a real CLI invocation, not a directly-constructed `LadderModelClient`.

### Case (ii) — `test_case_ii_backend_health_breaker_opens_on_the_fixture_fleet`

**Partial closure, disclosed precisely — not the full case (ii) lifecycle.** Both HEAVY targets
point at ONE real stub server (same `base_url`, differentiated only by the `model` field in the
request, since `BackendHealth`'s key is `f"{backend}:{model_id}"`, not the URL).
`fixture-heavy-a` answers every request with a genuine HTTP 429 (`StatusReply`); `open_after_
failures` is lowered to 1 via a legal `llm.failover.*` config override (research-51's own
load-bearing fact: `SPEC_ROLE_TIERS`/this config block is "documentation and a default... never
authority", so this is an operator config edit, not a monkeypatch). This closes the OPEN-
transition/failover-to-second-target half: a real `backend_health_transition` event to `DOWN`
citing `open_after_failures=1`, a `backend_failover` event per repo with trigger `RATE_LIMIT`,
and both repos' `llm_cache` rows attributed to `fixture-heavy-b`.

**What it does NOT close, and why (measured, not assumed):** the cooldown/`HALF_OPEN`-recovery
half. `BackendHealth` is deliberately per-run, never persisted (§11.8), so recovery needs a
SECOND classify call against the SAME target within the SAME client instance — meaning within
ONE `fleet scan` invocation. This fixture's two repos are dispatched from one
`asyncio.TaskGroup` (`orchestrator/runner.py`'s own docstring: "one task per repo") with no
ordering guarantee. I measured this directly with a throwaway responder logging arrival
timestamps: across five runs the two repos' classify calls to the same target landed 0.6 ms to
21 ms apart — too close and too variable to build a non-flaky cooldown/probe test on. The one
config knob that could force serialization, `concurrency.llm.heavy`
(`settings.py::LlmConcurrency`), is read by nothing in `src/` (`grep -rn "llm_concurrency("
src/fleet/` returns only the method's own declaration) — a genuine, disclosed production gap,
not touched here (out of this TEST-ONLY task's scope). The recovery half remains proven only at
component level (`tests/test_llm_failover.py::test_cooldown_lets_exactly_one_half_open_probe_
through_then_success_resets_to_up`), unchanged from before this task. This is stated in both the
test's own module-level comment and this report, per CLAUDE.md's "never close a documentary gap
with a convention wearing a mechanism's clothes".

### Case (iii) — `test_case_iii_schema_exhaustion_and_capability_drift_on_the_fixture_fleet`

Fully closed, both halves in one induced call, on the real fixture fleet. One real stub server
serves both HEAVY targets. `fixture-heavy-a` always answers schema-valid-but-model-invalid JSON
(`confidence: 5.0`, outside Pydantic's `[0, 1]`), exhausting `llm.max_schema_repairs` (shipped
default 1) and failing over with trigger `SCHEMA_UNSATISFIED`. `fixture-heavy-b`'s config
(`_MODELS_YAML_TEMPLATE_DRIFT`) promises `structured_output_modes: [JSON_SCHEMA, PROMPTED]` but
withholds `supports_json_schema`, so `negotiate()` can only reach PROMPTED — `CapabilityDrift`,
driven entirely by config, no response-content trickery. Asserts the failover event, exactly ONE
`CapabilityDrift` finding (fleet-level, deduplicated across both repos' identical drift — a
genuine dedup on `(run_id, kind, fingerprint)`, confirmed by reading `orchestrator/findings.py`'s
`ON CONFLICT` clause, not a missed write), and both repos' `llm_cache` rows landing at PROMPTED
via `fixture-heavy-b`. Deterministic and concurrency-safe regardless of call order, since both
targets' scripted behavior is stateless.

### Case (iv)'s deterministic-only clause —
`test_deterministic_only_dispatches_no_llm_call_against_the_broken_heavy_config`

Reuses `_make_workspace` with arm 2's exact broken ports. `fleet scan --skip-classify` →
`fleet sequence` → `fleet transform --deterministic-only`, asserting exit 0, `succeeded == 2`,
zero `llm_cache` rows, `attempts.tier == DETERMINISTIC` for both repos' phase-2 rows, and a
monkeypatched `LadderModelClient.complete` that raises if ever reached (never is). A **measured,
disclosed scope note** is in the test's own docstring: these two Python-only repos have no
cross-repo import needing a textual rewrite, so both resolve as a pure relocation with zero
identified rewrite units — confirmed by mutating `cli.py`'s deterministic cap away entirely
(`_validate_transform_flags`'s `return (1 if deterministic_only else max_attempts), policies`)
and observing byte-identical output. That mutation is a confirmed no-op on this fixture and is
NOT reported as this test's discriminator (per Rule 12: "if a mutation ... turns out to be a
no-op ... say so and try a different one").

## Rule 12 mutation-testing summary

One mutation per case, each applied, confirmed to redden the specific new assertion (not the
whole module), then reverted. `git diff --stat src/` was empty before, and confirmed empty
after every trial and at the end of this task.

| Case | Mutation | Result |
|---|---|---|
| (i) | `orchestrator/findings.py::_write_failover` made a no-op (`return` before the write) | Reddens cases (i)/(ii)/(iii) (all check `backend_failover`), NOT the whole module — arm 1 control, arm 2 outage, and the deterministic-only test still passed (3/6 red, 3/6 green) |
| (ii) | `llm/failover.py::record_failure`'s `consecutive_failures >= open_after_failures` bumped to `+ 1000` (never opens) | Reddens ONLY case (ii); the other 5 tests stayed green |
| (iii) | `llm/client.py::_emit_drift` made an unconditional no-op (`return` before the rank check) | Reddens ONLY case (iii); the other 5 tests stayed green |
| deterministic-only | `workers/base.py::_TIER_FOR_RUNG[None]` changed from `TransformTier.DETERMINISTIC` to `TransformTier.LLM_REPAIR` | Reddens the test's `exit_code == SUCCESS` assertion via a real `sqlite3.IntegrityError` (`attempts`' own `CHECK ((tier='DETERMINISTIC') = (context_policy IS NULL))`), caught by per-repo `TaskGroup` isolation as `repo_task_escaped`, surfacing as a wave-open refusal (`ExitCode.USAGE`) |

## Verification run

```
tests/test_heavy_tier_outage_e2e.py .......... 6 passed
tests/test_llm_failover.py, tests/test_llm_findings.py,
tests/test_transform_e2e.py, tests/test_llm_openai_stub_server_e2e.py,
tests/test_local_profile_e2e.py .......... 67 passed total (combined run)
```

`ruff check` on both changed files: clean. `mypy` (whole-manifest scope, `mypy_path=src`,
cwd inside the worktree per CLAUDE.md's own mypy gotcha): introduces zero new errors — the one
hit inside `test_heavy_tier_outage_e2e.py` (a chained `ExitCode.X == 8`-style "non-overlapping
equality" false positive) is pre-existing at the same source line in the file at `HEAD`,
confirmed by running mypy against the unmodified `HEAD` copy and getting the identical 57-error
total.

## What this closes and what remains

- Residual 1 (framing clause): closed for cases (i) and (iii) in full; case (ii) closed for its
  OPEN-transition/failover half only, with the cooldown/recovery half's non-closure disclosed and
  measured (see above) — not a NEW gap, the SAME gap `docs/CRITERIA_PLAN.md` already named as
  open at the component level, now additionally confirmed non-closable on the real fixture fleet
  without either a production concurrency fix or an admittedly-flaky test.
- Residual 2 (case (iv)'s deterministic-only clause): closed on this exact fixture, with a
  disclosed scope note that the fixture's own shape (no rule-ladder touchpoints) makes the
  deterministic cap itself unreachable code here — the property proven is "genuinely zero LLM
  calls, backed by a real broken-HEAVY config", which is what the SPEC sentence asks for.

Per the brief, `docs/CRITERIA_PLAN.md`'s §43 entry is left for the controller to update.

## Concerns for the controller

1. Case (ii)'s cooldown/`HALF_OPEN` recovery leg is NOT proven on the real fixture fleet, and I
   believe it cannot be, without either (a) wiring `concurrency.llm.*` to an actual semaphore in
   `src/` (a production change, out of this task's scope) or (b) accepting a timing-flaky test
   (which CLAUDE.md Rule 12 forbids). This should be weighed when the controller decides whether
   residual 1 counts as fully closed, partially closed, or needs a disclosed Rule-14 adjudication
   narrowing the criterion's "on the fixture fleet" framing for case (ii) specifically.
2. The deterministic-only test's own natural mutation candidate (removing the cap in `cli.py`) is
   a confirmed no-op on this exact two-repo fixture — disclosed in the test's docstring and this
   report rather than hidden. The reported mutation (`workers/base.py`'s tier-ladder mapping)
   discriminates the test but via a different mechanism (a schema CHECK constraint) than "the cap
   correctly refuses an escalation that would otherwise happen" — worth the controller's own
   read before treating this sub-clause as airtight.

---

## Fix round (post-review): case (ii)'s cooldown/HALF_OPEN-recovery half — now closed

**The review finding was correct, and my original blocker claim was wrong.** I had grepped for
`llm_concurrency(` (a literal method name) and found only its own unused declaration
(`settings.py:1305`), and concluded no config knob could serialize HEAVY-tier dispatch. The
reviewer traced the actual data flow, which does not go through that method at all:

- `settings.py:228-238` — `LlmConcurrency.heavy` (default 2), `for_tier()`.
- `orchestrator/budgets.py:1131-1140` — `Limits.create()` builds a `ResizableLimiter` per tier,
  sized from `concurrency.llm.for_tier(tier)`.
- `workers/classify.py:162` — `async with ctx.limits.for_tier(tier):` wraps the ENTIRE
  `ctx.llm.complete(...)` call in `ClassifyWorker` — the exact worker every test in this file
  drives.

Setting `concurrency.llm.heavy: 1` in the fixture's `fleet.yaml` (a legal, already-declared
operator config path — `FLEET_YAML` already has a `concurrency:` block) genuinely serializes the
two repos' classify dispatch. **Measured directly, not assumed:** a responder logging arrival
order showed `fixture-heavy-a`'s full 15-raw-request retry-and-failover sequence (repo 1)
complete entirely before repo 2's first request to that target ever arrived — no interleaving,
confirmed by inspecting the raw call log, the same way the earlier (wrong) 0.6-21ms-apart
measurement was obtained, just with the config leaf added.

### What was added

`tests/test_heavy_tier_outage_e2e.py::test_case_ii_cooldown_and_half_open_recovery_on_the_fixture_fleet`,
placed directly after the existing `test_case_ii_backend_health_breaker_opens_on_the_fixture_fleet`
(left completely unmodified, still proving the OPEN-transition/failover half with a large
`cooldown_s` that deliberately never recovers). The new test:

- Serializes dispatch with `concurrency.llm.heavy: 1`.
- Sets `open_after_failures: 1` (repo 1's single qualifying failure opens the breaker) and
  `cooldown_s: 0` — the schema's own integer floor (`llm.failover.cooldown_s: int`, a fractional
  value is rejected at config load, confirmed directly: `Input should be a valid integer, got a
  number with a fractional part`). `cooldown_s: 0` makes `may_call`'s elapsed check
  (`self.clock() - state.down_since < self.cooldown_s`) unconditionally false regardless of real
  wall-clock timing (`elapsed < 0` can never hold), so recovery does not depend on racing repo 2's
  start against repo 1's finish — it is eligible to probe the instant it checks.
- `fixture-heavy-a`'s responder answers 429 for its own first 20 raw requests (a threshold
  measured and set with margin above repo 1's exact, deterministic 15-request exhaustion count —
  `max_transient_retries=4` × up to `_SDK_TRANSIENT_RETRIES=2` retries + 1 initial SDK attempt =
  5 × 3 = 15, governed by hardcoded module constants, not jitter) and succeeds after that, so
  whichever request turns out to be repo 2's `HALF_OPEN` probe lands on a real success.
- Asserts the full lifecycle from real `backend_health_transition` events:
  `UP -> DOWN` ("N consecutive qualifying failures"), `DOWN -> HALF_OPEN` ("cooldown elapsed;
  probing"), `HALF_OPEN -> UP` ("probe succeeded"); exactly one `backend_failover` event (repo
  1's, trigger `RATE_LIMIT`); `llm_cache` showing ONE row on `fixture-heavy-b` (repo 1's
  failover) and ONE on the RECOVERED `fixture-heavy-a` (repo 2's probe) — not two on
  `fixture-heavy-b`, which is what a silently-failed recovery would read as; and a bounded raw
  call-log length (16-35 requests total), the busy-loop-avoidance half of case (ii)'s own
  sentence, over the SAME recorded call log the unit-level test already uses this technique for.

### Rule 12 mutation for the new test

`llm/failover.py::record_success` mutated to return immediately (before clearing
`consecutive_failures`'s transition / ever calling `_transition(..., "UP", ...)`) — reddens ONLY
the new recovery test (6/7 tests in the file still passed); the assertion that fails is the
`UP`-transition check in the lifecycle list, exactly the property this mutation removes. Reverted;
`git diff --stat src/` confirmed empty both before and after.

### Verification (fix round)

- `tests/test_heavy_tier_outage_e2e.py`: 7/7 passed (the 6 from the original round + the new
  recovery test).
- Full covering set (`test_heavy_tier_outage_e2e.py`, `test_llm_failover.py`,
  `test_llm_findings.py`, `test_transform_e2e.py`, `test_llm_openai_stub_server_e2e.py`,
  `test_local_profile_e2e.py`): 68/68 passed.
- `ruff check` on both changed files: clean.
- `mypy` (whole-manifest scope): identical 57-error total as before this fix round (zero new
  errors introduced).
- `git diff --stat src/`: empty at the end of the fix round.

### Case (ii) status: now FULLY closed on the real fixture fleet

Both halves — the OPEN-transition/failover-to-second-target half (proven by the original,
unmodified test) and the cooldown/`HALF_OPEN`-recovery half (proven by the new test) — are now
driven through the real CLI on the real two-repo fixture fleet. Residual 1 of
`docs/CRITERIA_PLAN.md` §43 (the "on the fixture fleet" framing clause) is therefore closed in
full for cases (i), (ii), and (iii) — no partial-closure caveat remains for case (ii).
