# Task C1 report — D50 ledger entry for the 26 inert config keys

## Status

DONE.

## D-number assigned

**D50** (D49 was the last entry present at base commit `9644406`; confirmed via
`grep -n "^#### D4\|D47\b\|D48\b\|D49\b" docs/INTEGRATION_HONESTY.md` before writing).

## What was verified directly (not transcribed from `KNOWN_INERT`)

- `llm_policy`: `grep -rn llm_policy src/` returns exactly two lines — `orchestrator/context.py:140`
  (field declaration) and `:152` (its one use, inside `RunContext.__post_init__`, passed to
  `LadderModelClient(..., policy=self.llm_policy)`). Confirmed all five `RunContext(` call sites in
  `cli.py` (`:1805`, `:4059`, `:7481`, `:7553`, `:9100`) never pass `llm_policy=`, so
  `LadderModelClient` always falls back to `CallPolicy()`'s own defaults (`llm/client.py:478`).
  `llm/client.py` never references `.failover` or `.rate_limit` as attributes at all — the word
  `failover` appears only in prose/event names.
- One timeout key: `grep -rn 'build_timeout_s\|clone_timeout_s' src/fleet/ | grep -v settings.py`
  is empty; both names (`settings.py:267-268`) exist nowhere outside their declaration.
- One `DECLARATIVE` correction, for contrast: `concurrency.llm.workhorse` (`settings.py:224`) is
  genuinely read via `LlmConcurrency.for_tier` (`settings.py:227-230`), called by
  `orchestrator/budgets.py:980` — confirmed exact line numbers.
- Spot-checked the remaining groups: `preflight.baseline_build`/`BaselineBuild` (zero code hits,
  one prose comment in `state/schema.sql:83`), `graph.max_edges`, `run.reaper_interval_s`,
  `run.projection_hz` (zero hits, and no periodic reaper-cadence loop found in
  `orchestrator/runner.py` to serve as a hidden hard-coded substitute), and all six single-value
  policy keys in Group 5 (`stubs.on_budget_exhausted`, `build.fail_on_missing_adapter`,
  `build.openapi_generator`, `scan.unknown_ecosystem_dest`, `pr.reviewers_from`,
  `llm.cache_path` — the last confirmed absent from `llm/cache.py` entirely, which takes its path
  from the caller).
- Re-derived exact `cli.py` line ranges for the `--context-policy` (`:3141-3147`),
  `--no-anchoring-guard` (`:3148-3154`) and `--stub-blocked` (`:3155-3160`) refusal blocks that
  motivate the asymmetry framing, rather than trusting my first-pass line guesses (sed output has
  no line numbers by default — I re-ran with `grep -n` to pin them before citing).

No contradiction with the brief found — every `KNOWN_INERT` line I sampled is still inert as
described, and the two representative checks the brief specifically demanded (`llm_policy`, a
timeout key, a `DECLARATIVE` correction) all confirmed as claimed.

## What the entry says (structure, matching D47–D49 house style)

1. Bolded thesis: the 26 keys are a config-surface instance of D47's class (declared, validated,
   echoed back, consumed by nothing); 12 of the 26 trace to one root cause (`llm_policy` never
   wired into any `RunContext(` call site).
2. Names the asymmetry the test docstring itself states: `cli.py` refuses three flags
   (`--context-policy`, `--no-anchoring-guard`, `--stub-blocked`) at the flag layer for missing
   machinery, but `fleet.yaml` has no equivalent refusal — `fleet config` echoes any of these 26
   values back unconditionally.
3. Verification paragraph (the three required checks plus extras, see above).
4. Five groups, each with settings.py line citations and a severity call:
   - **Group 1** (`llm.rate_limit` × 9 + `llm.failover` × 3 = 12 keys) — the sharp one: latent
     because defaults describe a limiter/circuit-breaker that doesn't exist, so nothing is
     "wrong" until an operator, being throttled, edits one of these expecting backpressure and
     gets the same fixed-backoff retry as always.
   - **Group 2** (`transform.anchoring` × 2) — latent unless the ladder loops; bites when an
     operator sets a tighter per-rung reask cap than `transform.max_attempts` already provides.
   - **Group 3** (`preflight.baseline_build` × 1) — the one entry that is NOT latent: default
     `enabled: True` implies the native build gate always runs; zero code reads the section, so it
     never runs, on every config, not only a hand-edited one.
   - **Group 4** (bare timeouts/ceilings × 5) — confirmed inert but consequence not fully traced
     (I explicitly declined to claim clones/builds hang forever — buildverify has its own
     unrelated hard-coded probe timeout, and no reaper cadence loop was found either way).
   - **Group 5** (single-value policy knobs × 6, including the gentler `llm.cache_path` case) —
     latent, bites only when an operator picks a non-default value expecting a branch that isn't
     there.
5. "Would a test catch it?" — partially: `test_every_config_key_is_read` /
   `test_known_inert_keys_are_still_inert` are real regression guards (that's why this is D50 and
   not D50–D75), but no test is behavioral. Also surfaces, per the brief's instruction not to
   overstate, that the test's own scan additionally misses `llm.max_schema_repairs` and
   `llm.failover.enabled`/`.max_targets_per_call` (same root cause, but unfalsifiable by the
   scan because they share exact spelling with unrelated `CallPolicy` fields in `llm/client.py`)
   — flagged explicitly as NOT part of this entry's 26 and NOT a new D-number claim, just a noted
   gap so it isn't mistaken for absence of a defect.
6. "Not fixed here" — wiring `llm_policy` would touch `cli.py`, which two other agents are editing
   concurrently in this pass; Rule 3 confines this lane to the ledger file. `baseline_build` needs
   an actual worker, not a wiring fix. The ADR-level rule question (how a repaired knob should
   behave) is left open per Rule 7.

## Concerns

- None functionally. The one thing worth flagging to the orchestrator: Group 4's severity claim
  is deliberately hedged (I could not find what actually bounds build/clone duration or reaper
  cadence today, if anything) — a future pass could trace that further, but doing so was out of
  scope for a docs-only lane and I did not want to assert a stronger claim than I could verify.
- `src/fleet/workers/rewrite.py` and `src/fleet/cli.py` show as modified by other agents in
  `git status` during this task, per the brief's note; I did not read or touch either beyond
  read-only `grep`/`sed` for citation verification.

## Commit

Committed on the current branch as a docs-only change to `docs/INTEGRATION_HONESTY.md`.
