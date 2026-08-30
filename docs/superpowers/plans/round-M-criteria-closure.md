# Round M — SPEC §12 criteria closure

**Spec authority:** `docs/SPEC.md` §12 (Success Criteria) is binding. `docs/CRITERIA_PLAN.md`
is the closure backlog this plan draws from — each task below cites its criterion's exact
"Done bar" entry there. `CLAUDE.md` Rules 1-14 and the "Architectural & Subagent Guardrails"
section bind every task.

**Why these three, why now (2026-08-30):** re-verified against current `HEAD` (`fa95469`)
immediately before this plan was written — a prior round (round L, untracked in
`docs/PROGRESS.md`) landed `EventEmitter`/`events_jsonl_path` wiring and COORDINATE-detector
wiring since `docs/CRITERIA_PLAN.md` was authored, which reclassified §12.18 from NEW-MECHANISM
to WIRING and closed §12.27's COORDINATE leg entirely (see `docs/CRITERIA_PLAN.md`'s addendum).
These three tasks are the highest-confidence, lowest-file-overlap WIRING-tier items remaining:
an already-correct, already-unit-tested piece of logic with no real caller. None require new
design judgment about *what* the code should do — only *where* it should be called from, which
each task's brief pins down from a fresh grep, not from the (possibly stale) CRITERIA_PLAN.md
prose alone.

## Global Constraints

- Match this codebase's existing style exactly: this is a mature, 40k+ line, heavily-reviewed
  Python codebase (`asyncio`, Pydantic v2, `mypy --strict`). Read the surrounding module before
  writing a line. Do not introduce new abstractions, helper layers, or "improvements" beyond the
  task's literal scope (`CLAUDE.md` Rule 2, Rule 3).
- **Never invent a magic bound, silently loosen an existing check, or add a `# type: ignore`
  to satisfy `mypy --strict` — fix the actual type issue.**
- Every new/changed test must be a real, discriminating test: write it so it fails against the
  pre-fix code and passes against the fix (`CLAUDE.md` Rule 12 — "old-passes/new-fails on the
  same input"). State in your report which specific mutation (reverting your own fix) you used
  to confirm the test discriminates.
- Run the exact test files you touch or that exercise your changed code — whole files, no `-k`
  filter — before reporting DONE. Name the command and its output in your report.
- Do not touch `docs/SPEC.md` §12's criterion text. If you find the criterion's own wording is
  wrong (not just unmet), STOP and report it as a concern rather than editing the SPEC yourself —
  `CLAUDE.md` Rule 14 requires that go through the controller.
- Do not run `pytest` for the whole suite (≈15 min, and `CLAUDE.md` §6 warns against concurrent
  pytest sessions reaping each other's output — three workers may run concurrently in isolated
  worktrees this round). Scope your own test runs to the files you touched or that cover your
  changed function.
- You are in an isolated git worktree for this task. Commit your work there. Do not attempt to
  merge, push, or touch the primary checkout — the controller integrates your branch.
- If you hit a design decision beyond straightforward wiring (e.g., the exact call-site
  signature isn't as expected, or two plausible integration points exist), STOP and report
  BLOCKED with your specific question rather than guessing. A research agent is investigating
  the likely integration points for Tasks 1 and 2 in parallel with your work — the controller
  will relay findings if relevant.
- Never dispatch your own subagents, including a reviewer. Report back to the controller.

---

## Task 1: Wire `check_criteria()` into the real Phase-1-exit runtime path (§12.9)

**Criterion:** `docs/SPEC.md` §12 item 9 (Phase 1 exit condition). Full done bar:
`docs/CRITERIA_PLAN.md` §12.9 entry.

**Current state (verified 2026-08-30):** `src/fleet/graph/sequence.py:589` defines
`check_criteria()`, composing four sub-checks (`check_criterion_a` through `_d`). It has exactly
one caller in the entire tree: `tests/test_graph_sequence.py:513`. No CLI or runtime code path
invokes it — the Phase 1 exit condition described in `docs/SPEC.md` §3 exists only as a library
property. Read `src/fleet/graph/sequence.py` in full (the checks and their signatures) and
`src/fleet/cli.py`'s `scan`/`plan` commands (search for where Phase 1's completion is currently
determined or asserted, if at all) before deciding the call site.

**Task:**
1. Determine where in the real CLI path Phase 1's exit condition should be enforced (likely the
   end of `fleet plan` or wherever the graph/wave-plan is finalized before Phase 2 can begin —
   verify against `docs/SPEC.md` §3's phase-boundary description, do not guess from the name
   alone).
2. Call `check_criteria()` (or its composed sub-checks) from that real path. If the check fails,
   the CLI must refuse to proceed to Phase 2 with a clear error naming which sub-criterion failed
   — follow this codebase's existing fail-loud error conventions (see other `UnresolvedFindingsError`-
   style raises in `cli.py` for the pattern).
3. Add one e2e fixture that plants all five of §12.9's named exemption cases *simultaneously*
   (not one exemption per synthetic `WavePlan`, which is how the existing unit tests do it) and
   asserts Phase 1 exit condition holds there. Check `tests/test_graph_sequence.py`'s existing
   `ExemptionCase` tests for the five cases' shapes.
4. Do not modify `check_criteria()`'s or its sub-checks' internal logic — they are already
   correct and unit-tested. This is exclusively a caller-wiring task plus one new fixture.

**Report:** DONE / DONE_WITH_CONCERNS / NEEDS_CONTEXT / BLOCKED, commits, one-line test summary,
concerns. If BLOCKED on the exact Phase-1-exit call site, say so specifically — a research agent
may already have the answer.

---

## Task 2: Wire `orchestrator/stubs.py`'s state machine into the real build/resume path (§12.37)

**Criterion:** `docs/SPEC.md` §12 item 37 (stub lifecycle). Full done bar:
`docs/CRITERIA_PLAN.md` §12.37 entry. Related tracked defect: D69 (see
`docs/INTEGRATION_HONESTY.md`) — read D69's entry before starting, it names this same gap.

**Current state (verified 2026-08-30):** `src/fleet/orchestrator/stubs.py` implements the full
§3.5.1 stub-lifecycle state machine and is covered by a pure-function test suite
(`tests/test_stubs.py`), but has **zero importers anywhere in `src/`**
(`grep -rn "orchestrator.stubs\|from .stubs\|import stubs" src/fleet/` returns nothing outside
the file itself, re-checked immediately before this plan was written). `--stub-blocked` on
`fleet build` is unimplemented and refused with exit USAGE — and that refusal is itself what the
current test suite treats as passing (i.e., there's a test asserting the refusal, not a test
proving the feature works).

**Task:**
1. Read `src/fleet/orchestrator/stubs.py` in full (the state machine, `reconcile`, the four
   transitions) and `tests/test_stubs.py` to understand its public interface.
2. Read `src/fleet/cli.py`'s `build` and `resume` commands and find where `--stub-blocked` is
   currently refused (search for the refusal string / exit-2 raise). Determine the real
   integration point: `--stub-blocked` should reach `orchestrator/stubs.py`'s state machine
   instead of being refused.
3. Wire it: remove the refusal for the case the state machine can actually handle, call into
   `orchestrator/stubs.py` from the real build/resume path, and ensure a stub-blocked repo's
   phase transitions actually go through this state machine (write a real row/task/finding, not
   just an in-memory check).
4. Add one e2e fixture driving a real stub-blocked scenario through the wired CLI path (not
   `orchestrator/stubs.py`'s functions called directly) and assert the correct end state.
5. Do not change the state machine's own transition logic — D69 and the existing pure-function
   tests already establish it's correct. This is exclusively a wiring task.

**Report:** DONE / DONE_WITH_CONCERNS / NEEDS_CONTEXT / BLOCKED, commits, one-line test summary,
concerns. If the exact CLI integration point is ambiguous, say so specifically.

---

## Task 3: Emit the `llm_call` event and split `logs/errors-<run_id>.jsonl` (§12.18 remainder)

**Criterion:** `docs/SPEC.md` §12 item 18 (observability). Full done bar:
`docs/CRITERIA_PLAN.md` §12.18 entry (reclassified WIRING 2026-08-30).

**Current state (verified 2026-08-30):** commit `01b64d3` wired `EventEmitter`/
`events_jsonl_path` end-to-end — every CLI entry point already passes `json_path=`, and
`logs/events-<run_id>.jsonl` is a real, produced artifact. That commit's own message explicitly
scoped OUT the `llm_call` event (carrying `latency_ms`) and the `logs/errors-<run_id>.jsonl`
split as "separate later tasks" — this task is those two remaining pieces. Read `01b64d3`'s full
diff (`git show 01b64d3`) first — it is the direct precedent for how to use `EventEmitter`
correctly in this codebase (redaction handling, the "emit() never raises except for `pr_merged`"
carve-out, the `_Sink` eager-open behavior) and you should follow its conventions, not invent new
ones.

**Task:**
1. Read `src/fleet/obs/events.py` (the `EventEmitter`/`events_jsonl_path`/`_Sink` machinery) and
   `src/fleet/llm/client.py`'s `LadderModelClient.complete`/`.invoke` (confirmed 2026-08-30 as
   where every real LLM call passes through — verify this is still current before relying on it).
2. Emit an `llm_call` event from that call site carrying at minimum `latency_ms` (measure around
   the actual provider call) and enough identifying fields to be useful (model/backend/role —
   check `docs/SPEC.md` §12.18's literal wording and any ADR-0012 text in `docs/DECISIONS.md`
   for the exact fields expected, do not invent a schema from scratch).
3. Route error-level entries into a separate `logs/errors-<run_id>.jsonl` sink using the same
   `events_jsonl_path`-style derivation pattern `01b64d3` established (one derivation, don't
   duplicate the literal path construction) — "split or filter" per the done bar, your choice
   which, state which you picked and why in the report.
4. Follow `01b64d3`'s redaction discipline: if any field could carry a secret (a URL, an error
   message with embedded credentials), it must go through the same redaction path other events
   use — do not introduce a new unredacted egress path (this is a security-sensitive detail,
   `01b64d3`'s own commit message notes it fixed exactly this kind of leak once already).
5. Write tests in the style of `tests/test_event_stream_wiring.py` (drive the real CLI, then
   inspect the filesystem) — each new test should name the specific leg it discriminates, per
   that file's existing convention.

**Report:** DONE / DONE_WITH_CONCERNS / NEEDS_CONTEXT / BLOCKED, commits, one-line test summary,
concerns.
