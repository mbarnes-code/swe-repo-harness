# Handoff — round G

**Written at the close of round F. Base for round G: `main` at `17553b5`.**

---

## 0. How to read this document

Every claim carries how it was verified. **Use the tag, not the sentence.**

| tag | meaning |
|---|---|
| **[suite @ SHA]** | certified by the whole-tree suite at that anchor |
| **[probe @ SHA]** | re-derived from the code or tree by a named probe at that anchor |
| **[ledger]** | reported by the lane that measured it; not independently re-derived here |
| **[decision]** | a ruling or deliberate deferral, not a measurement |

**Round F's reading rule, and it is not the same as round E's.** Round E's lesson was that
pre-formatted plausibility is a risk factor. Round F's is sharper and more specific:

> **Five claim-corrections this round originated with the orchestrator, and lanes caught all five.**
> Not one was caught by the sender. The failure was never invention — it was **relaying a number
> without attributing it**, and then a lane inheriting a *grouping* as if it were a *finding*.

The single most expensive shape, verbatim from lane W13: *"the dispatcher inherited the review's
routing **grouping**, not its finding."* A commit groups by file ownership; a review groups by where
it looked. **Neither is a claim.** Derive site lists from a sweep for the claim.

---

## 1. Read these first, in order

1. **This document.**
2. **`docs/superpowers/plans/round-f-rulings-subtask-10.md`** — the rulings for resume step 5
   subtask 10, **with two in-place dated corrections made by the lanes that implemented them**. Read
   the corrections, not only the rulings.
3. **`CLAUDE.md`** — amended once in round F (`5bcc3aa`). **It was measured and deliberately NOT
   consolidated** (§6 below). 43,136 → 44,491 bytes.
4. **`docs/DECISIONS.md`: ADR-0092** (`12ac784`, repaired at `01ac4ca`) and the **ADR-0086
   placeholder** (`b5f7760`).
5. **`docs/INTEGRATION_HONESTY.md`: D79 status change, D81, D82, D83, D84**, and the **D62
   annotations** (two, from different lanes).
6. **`docs/superpowers/plans/stub-reconcile-and-waveclock-research.md`** and
   **`llm-cache-hit-attribution-research.md`** — promoted at round F close.

---

## 2. Verified state at `17553b5`

| claim | how verified | tag |
|---|---|---|
| **`1849 passed`, 0 failed, **0 skipped**, xfail 0, clean `bazel disk` (peak 3.72 GiB, residual 0)**, 793.66s | `.venv/bin/python -m pytest -q` — **no `-k`, no node IDs, no path arguments**; anchor stamped before, **tree clean and anchor unchanged after** | **[suite @ 17553b5]** |
| `python -m mypy`, **no path arguments** → **115 files, no issues** | `pyproject.toml`'s `packages = ["fleet"]` + `strict` set the scope, so **`tests/` is NOT covered** | **[probe @ 17553b5]** |
| Round F = **10 commits** off `251cd30` | `git log --oneline 251cd30..HEAD` | **[probe @ 17553b5]** |
| Test delta **+25**, and it reconciles exactly | W2 +7 · W4 +3 · W10 +13 · W12 +1 · W14 +1 = 25; 1824 + 25 = 1849 | **[probe @ 17553b5]** |

**The base was re-certified before work began.** Round F's first act was to run the full suite at
`251cd30` rather than inherit round E's certification of `0b3fbae`: **1824 passed, every number
identical**, and `src/`+`tests/` proven byte-identical across the two refs. **[suite @ 251cd30]**

---

## 3. What round F landed

| commit | what |
|---|---|
| `12ac784` | `PRAGMA` classified by **name**, not by verb + **ADR-0092** |
| `cac537d` | SPEC §11.6's LLM cache **wired** — it had shipped entirely inert (D79 C1–C4) |
| `b5f7760` | Six documentary repairs: ADR-0086 placeholder, 4 rotted citations, 3 stale plan claims |
| `5bcc3aa` | `CLAUDE.md`: a false claim about its own probe, the two-budget contradiction, a third measured immunity |
| `01ac4ca` | **ADR-0092's four false claims repaired** |
| `6a8fafd` | Resume step 5 **subtask 10a + 10b** — the continuation plan and its delegation, **dormant** |
| `ee1ddc8` | D79 status → `FIXED, LANDED`; **D82–D84**; three re-admit comment corrections |
| `f54dac8` | SPEC §11.6's cache key (**7 of 7 agreeing**) + §3.4's self-contradiction + a prose-driven instrument |
| `53e5d8d` | Four citation defects the cache commit shipped, fixed **with a mechanism** |
| `17553b5` | **Projector Arm A** — `RunContext.projector` was never constructed + **D81** + D62 annotation |

**The round's two substantive results:**

1. **SPEC §11.6's LLM cache was entirely inert and now is not.** Measured behaviourally: the shipped
   `RunContext` **billed the same call twice and wrote 0 rows**; a control arm with a store injected
   billed once and wrote 1. **[ledger]**
2. **`RunContext.projector` was never constructed anywhere in `src/`.** `Projector(` sites = 0,
   `projector=` kwargs = 0, all three `ctx.project()` calls permanent no-ops. SPEC §6 accepts
   "staleness of up to one second"; shipped staleness was bounded by `wave_max_wallclock_s` =
   **14,400 s**. Mid-wave refreshes **0 → 4**. **[probe @ 53e5d8d]**

---

## 4. Resume step 5 — subtask 10 is **2 of 10 sub-tasks landed**, and the rest is decomposed

`10a` (the pure plan `_continue_from_floors`) and `10b` (delegation through the three roots) landed
at `6a8fafd` and are **DORMANT**: `cli.resume` still raises `ResumeIncompleteError`.

**Remaining: 10c–10j.** The decomposition is `docs/superpowers/plans/resume-step5-subtask-10-research.md`
**as corrected by `round-f-rulings-subtask-10.md`.** Take them in this order:

* **10d — the wiring.** Replace `cli.resume`'s final `raise` with 10b and delete the class. **The
  `docs/SPEC.md:6646` qualification lands HERE, by ruling** — 10a/10b are dormant, so §10's sentence
  is no more false than before.
* **10e — the largest cost.** **34** tests issue a bare `resume` and assert exit 2; **36** issue one
  at all; **38** bare call sites. **R4's "27" does not reproduce and its "22 of 27" states no
  predicate and is unverifiable by construction** — use **30 of 34**, derived. **[probe @ 251cd30]**
  **Its ADR is `ADR-0080`, reserved for exactly this since allocation — do NOT take a fresh number.**
* **10c, 10f–10j** — see the decomposition.

**Ruling B is implemented and its criterion is asserted:** a `SCAN` floor is **reported and skipped,
never served**, via the loud payload key `scan_floor_not_continued` — named route-neutrally because
`never_scanned` would be **false** of the fell-through-walk route, which is **deliberately not
distinguished in v1**. **[decision]**

**Two rulings were corrected in place by the lanes that implemented them. Read them:**
- **Ruling C**: `_refuse_concurrent_mirror_run` is a **non-blocking probe, not an acquisition**, so
  ADR-0080 must say *"takes the same §10 refusal the phase commands take"*, **never** "acquires the
  mutex". The conclusion stands on different ground than the ruling gave.
- **Ruling D** resolved **NO**: a delegate over a breached wave already returns **exit 4** through
  `_raise_for_phase`. **Step 8 needs no wall-clock semantics of its own.**

---

## 5. Open, with owners named or explicitly absent

* **D81** — `docs/SPEC.md:5027` credits `PRAGMA wal_checkpoint(TRUNCATE)` to "the single projector
  task"; **`wal_checkpoint` has 0 occurrences under `src/`** by two instruments. The entry
  **deliberately declines both candidate remedies** — the deciding measurement (does the long-lived
  `mode=ro` connection hold an open read transaction?) **was not taken**. **Unowned.**
* **D82 — the load-bearing one.** The wave wall clock is **per-`(run, wave)`, not per-phase**;
  `wave_started_at` has no phase column. TRANSFORM burning wave 0's clock leaves a never-run BUILD
  and VERIFY reading `breached=True`, `admitted=()`, `exit=4`, and `open_wave` keeps the old stamp
  via `COALESCE`. **One exit-4 halt makes every later phase on that wave un-admittable, permanently.**
  **Nothing in the tree addresses whether cross-phase sharing is intended.** **Unowned.**
* **D83** — exit 4 on a wall-clock breach carries the literal message `'None'`. **D84** —
  `_prepare_repo` mutates every member's git **before** admission discovers it can admit nothing.
* **D62 — `attempts.llm_cache_hit` still reads 0**, and its *stated reason* is now obsolete while the
  symptom lives for a new cause. **`on_hit` alone cannot fix it**: `record_attempt`'s `INSERT` names
  **24 of 31** declared columns — **seven** unwritable, D62's five **plus `integration_ref` and
  `container_id`**. The route is costed in the promoted
  `llm-cache-hit-attribution-research.md`; it needs `cache.py` + `workers/base.py` + `cli.py` +
  `repository.py` **in one commit by one author**, and multi-call-attempt semantics are **unsettled
  in SPEC §11.6 and need a ruling**. **A test landed this round (`53e5d8d`,
  `test_run_context_llm_cache.py:376`, asserting `worker.llm is ctx.model_client`) forecloses the
  natural attribution seam. That test is correct AND it is a constraint — decide deliberately
  whether to relax it; do not discover it by surprise.**
* **`stub_reconcile`** — carved out of subtask 10 by ruling. `src/fleet/orchestrator/stubs.py:554`
  defines a complete, pure, tested `reconcile(...)` imported by **`tests/test_stubs.py:42` and by no
  module in `src/` at all**. Branch (b) is **"a driver PLUS a merged-provider guard"** — `reconcile`
  **abandons a stub whose provider PR is MERGED** and T1 has never fired anywhere. **S1–S7
  decomposition in the promoted `stub-reconcile-and-waveclock-research.md`.** **Unowned.**
* **No landed test covers the four composition roots — none ever did, which is how the projector gap
  survived.** Criterion for the missing test, named by the lane that found it: assert the **state
  vector moved mid-wave**, never `ctx.projector is not None`. Natural harness is
  `tests/test_runner.py`.
* **`ruff format --check src/fleet/cli.py` FAILS.** **Stated as a class result, integer deliberately
  dropped** — `ruff>=0.8` is unpinned and installed is 0.16.2, so hunk decomposition is
  formatter-version-dependent and **not a ratchetable quantity**. Pinning `ruff` is a real
  improvement, ruled **out of round F** because it changes the declared toolchain and needs its own
  certification.

---

## 6. `CLAUDE.md` was measured and deliberately NOT consolidated

The round-F handoff made a consolidation pass its item 1. **It was done, and the answer was don't.**

**Defensible cut: 157 bytes (0.36%).** Four independent measurements: **81.8% of claim bytes** carry
a SHA, a measured number, a path citation or a disclosed boundary; max pairwise bigram overlap across
**72 claims** is **0.068**; **zero** repeated 7-word phrases in 6,748 words; all 8 shingle-flagged
pairs **complementary on reading**. **[probe @ 251cd30]**

**The split proposal was rejected by ruling** (move Rule 12 + Guardrail 6 on-demand: −17,865 B,
−41.4%, price **102 of 350 citations**). Those two rules are what caught round F's real defects.
**The always-loaded cost is the mechanism.** **[decision]**

**The hazard, measured at maximum amplitude:** with `CLAUDE.md` **truncated to zero bytes**, the
prose-binding modules still return **178 passed** and the suite still collects **1,824 tests**.
**Deleting the file entirely is invisible to the suite.** Its **350 citations across 94 files** —
**144 prose-anchored** — all rot silently. **[probe @ 251cd30]**

---

## 7. Method results round F actually produced — these are the reusable part

1. **An instrument can pass three of four validation checks under the WRONG QUANTITY.** A projector
   detector keyed on `sha256(whole file)` had its **control** read 4 where it must read 0, because
   `MigrationState.updated_at` is `default_factory=utcnow` — so the digest detected *"the projector
   ran"*, not *"the state moved"*. **The cosmetic control, the check that looks most like a
   formality, is what caught it.**
2. **A symbol-keyed sweep is structurally blind to prose describing that symbol's EFFECT.** A correct
   sweep on `model_client` missed two docstrings the same commit falsified. **Sweep on the
   participants in the construction.** Both sites were *also* line-wrapped — two independent
   mechanisms hiding one class.
3. **Annotate-never-rewrite makes a naive after-count structurally wrong.** A lane published
   "1 before, 0 after" and the tree returned **5**. Two lanes independently measured the corrected
   form: state the after-count **by rule**, subtracting quotations inside editorial-correction
   blocks. Raw goes **up**; the class that matters goes to 0.
4. **A blocked lane that proves the block is worth more than a forced patch.** W15 returned BLOCKED
   with two independent derivations and **declined to fabricate a mutation battery for a patch that
   does not exist**.
5. **Check whether a finding already has a number before allocating one.** D81 was allocated for a
   finding **D79 already recorded**; writing it would have left D79 `OPEN` over its own fixed defect.
   The lane refused and set the status field instead.
6. **The D-number census was measured by four lanes and disagreed four times.** Every failing
   predicate keyed on **heading shape**; the one that held is **form-agnostic** (`\bD8[1-4]\b`).
   Entries are headed **three** ways and **D12/D18/D19/D75 carry two forms each**.
7. **A detached worktree cannot run the full suite green** — 24 failures, all
   missing-`tools/`-toolchain. Certification must happen in the primary checkout on a **quiesced**
   tree. A suite run while a sibling lane edits a tracked test file is **qualified, not a
   certification** (this happened once in round F, by orchestrator error, and was re-run).

---

## 8. `.superpowers/` is now IGNORED, not merely untracked

Round E's handoff warned that `git clean -fdx` destroys the process record. **That warning is now
sharper:** the SDD workspace script wrote **`.superpowers/sdd/.gitignore` containing `*`**, so the
directory no longer appears in `git status` at all. `git clean -fdx` still deletes it — `-x` is
exactly the flag that removes ignored files — and **the one signal a human might have noticed
beforehand is gone.**

It holds round D, round E (48 lane directories) and **round F's 16 lane reports plus the
orchestrator ledger with every ruling and its cost-if-wrong**. The three load-bearing round-F
artifacts **were promoted** at round F close; the rest is not.
