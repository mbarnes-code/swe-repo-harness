# Handoff — round H

**Written at the close of round G. Base for round H: `main` at `a76cc80`.**

---

## 0. How to read this document

| tag | meaning |
|---|---|
| **[suite @ SHA]** | certified by the whole-tree suite at that anchor |
| **[probe @ SHA]** | re-derived from the code or tree by a named probe at that anchor |
| **[ledger]** | reported by the lane that measured it; not independently re-derived here |
| **[decision]** | a ruling or deliberate deferral, not a measurement |

**Round G's reading rule.** Round F's was *"pre-formatted plausibility is the risk factor"*. Round G's is
narrower and costlier:

> **Ten numbers were corrected in round G. Every one originated with the orchestrator relaying a figure
> without attributing it — and three of those were SITE LISTS inherited from how a review or a commit
> had GROUPED its work rather than from a sweep for the claim.**

The grouping error is the expensive one, and it recurred three times in two rounds:
- a review's *routing grouping* taken as its finding (`cli.py:603`, which turned out to be a different defect class);
- a rationale sentence read as a site list (the bare-`off` YAML hazard — *"only one of the three sites spells it; routing the fix would have had a lane edit two correct sentences"*);
- and a test-file list built from an AST predicate whose **own disclosed blind spot** was dropped in transit (11 bare-`resume` sites behind a helper).

**A commit groups by file ownership. A review groups by where it looked. Neither is a claim.**

---

## 1. Read these first, in order

1. **This document.**
2. **`docs/superpowers/plans/round-f-rulings-subtask-10.md`** — subtask 10's rulings, **with two in-place
   dated corrections made by the lanes that implemented them.**
3. **`CLAUDE.md`** — amended twice in round G (`5bcc3aa`, corrected at `12d3527`). **§6 below is the
   proposed third amendment and it is the most load-bearing item in this handoff.**
4. **`docs/DECISIONS.md`: ADR-0080** (`fleet resume` continues) and **ADR-0093** (ruff pinned + gated,
   annotated at `b76c041`).
5. **`docs/INTEGRATION_HONESTY.md`: D81 (`PARTLY ADDRESSED`), D82, D84, D85** — and **D62's ALL-HIT
   ruling**.
6. **The four promoted research documents**, two from each round:
   `stub-reconcile-and-waveclock-research.md`, `llm-cache-hit-attribution-research.md`,
   `waveclock-phase-signal-research.md`, `citation-instrument-baseline.md`.

---

## 2. Verified state at `a76cc80`

| claim | how verified | tag |
|---|---|---|
| **`1912 passed`, 0 failed, **0 skipped**, xfail 0**, clean `bazel disk` (peak 3.72 GiB, residual 0), 803s | `pytest -q` — **no `-k`, no node IDs, no path arguments**, quiesced tree, anchor stamped before and **verified unchanged after with a clean worktree** | **[suite @ a76cc80]** |
| `python -m mypy`, **no path arguments** → **115 files, no issues** | `packages = ["fleet"]` + `strict`, so **`tests/` is NOT covered** | **[probe @ 3dd500d]** |
| `ruff check .` whole-repo → clean, **and now gated** | `tests/test_lint_gate.py`, ADR-0093 | **[suite @ a76cc80]** |
| Round G = **11 commits** off `635a83c` | `git log --oneline` | **[probe @ a76cc80]** |
| Test delta **+63** (1849 → 1912) | 1 + 5 + 4 + 2 + 1 + 50 | **[probe @ a76cc80]** |

**One term in that delta is inferred, not measured: the `+2` for `f6a2e4e`.** It is derived from the
total rather than per-file. Stated because round G corrected ten numbers that were plausible arithmetic.

---

## 3. What round G landed

| commit | what |
|---|---|
| `8b40498` | D83 — a wall-clock breach printed `'None'` |
| `4cab272` | The four-composition-roots test |
| `12d3527` | Three round-F cross-commit falsifications + 8 real `ruff check` errors |
| `3dc3a98` | Ledger: D83 fixed, D79(ii)/D82/D62 annotated |
| `352c514` | `ruff==0.16.2` pinned and `ruff check` gated (ADR-0093) |
| **`f6a2e4e`** | **`fleet resume` continues** — subtask 10d+10e, ADR-0080 |
| `b76c041` | The lint gate could not pass in a worktree |
| `4398358`, `f5a188a` | D81's SPEC half corrected; its SHA substituted |
| `3dd500d` | The breach message named a **stale** elapsed; 63 citations re-swept |
| **`a76cc80`** | **The citation instrument** |

**The round's headline: `fleet resume` actually continues.** Resume step 5 subtask 10 now has **10a, 10b,
10d, 10e landed**.

---

## 4. Resume step 5 — subtask 10 is 4 of 10, and **10c is a LIVE gap**

**Take 10c first.** `_require_disk_headroom` is **still off the continuation path**. Before `f6a2e4e` that
was latent; now `fleet resume` runs real phase work without it. **[ledger]**

Then **10f–10j** from `resume-step5-subtask-10-research.md`, **as corrected by
`round-f-rulings-subtask-10.md`.** Note for whoever writes 10f: it inherits findings that were **never
re-measured** and must re-measure at the moment it acts.

---

## 5. Open, with owners named or explicitly absent

* **D82 — Disposition B is ACCEPTED IN PRINCIPLE and unbuilt.** The signal exists: `phases.started_at`,
  no schema change, reading **2 on a resume vs 0 on a phase transition**. **But the cheap form is
  REJECTED** — it is correct in **3 of 4** cases, and the hole (a crash between `begin_wave` and the
  first claim) is reachable without adversarial construction, so Rule 12's stop rule makes it a defect.
  **Build the 4-of-4 form**: one additive nullable `waves.wave_started_phase`, no rebuild, no backfill.
  Costs a `begin_wave` protocol change across 4 sites, one being an in-memory SCAN stub with no `phases`
  table. **Needs an ADR — it is a design change, since SPEC never scopes the clock to a phase (0 of 31
  normalised hits).** **Unowned.**
* **D84** — `_prepare_repo` mutates every member's git **before** admission discovers it can admit
  nothing. Exercised: breached wave, exit 4, 0 admitted, **2 of 2 worktrees carrying anchor refs**.
  **3 of 3 phases**, not 2. Its body rules it be taken **with D82**. **Unowned.**
* **D62 — ruled ALL-HIT**, because `schema.sql:729` states `llm_cache_hit = 1 ⇒ cost_usd = 0` and
  `accumulate` **sums** cost. **The promoted route's `OR` in `accumulate()` is therefore WRONG — it must
  be `AND`**; `OR` *is* any-hit. Costed in `llm-cache-hit-attribution-research.md`; needs `cache.py` +
  `workers/base.py` + `cli.py` + `repository.py` **in one commit**. A test landed at `53e5d8d`
  forecloses the natural attribution seam — **relaxing it is measured narrow, not a loss.**
* **D81 — `PARTLY ADDRESSED`.** SPEC corrected. Open leg measured: the pragma half **is** bound
  (`tests/test_db.py:147`, `:160-166`), **nothing binds "the tree's only read transaction"**.
* **D85 — `OPEN`, fix landed.** The certification and working environments diverge and an instrument can
  report the divergence as the defect. Its live residue: the lint gate's **scope check has zero
  discriminating power in a worktree** (182 vs 182), disclosed by a `UserWarning`, not patched.
* **The 46 pinned citations.** `tests/test_integration_honesty_citations.py` pins them as a
  both-directions ratchet. **Three causes, not separable by rule** — real drift, usage-site citations,
  verbatim records — so a rule-based sweep would repoint records and destroy what they record. **Read
  `citation-instrument-baseline.md` before touching them.**
* **`stub_reconcile`** — `stubs.py:554` is complete, pure, tested, imported by **`tests/test_stubs.py:42`
  and by no module in `src/`**. Branch (b) is **"a driver PLUS a merged-provider guard"** — `reconcile`
  **abandons a stub whose provider PR is MERGED**. S1–S7 in `stub-reconcile-and-waveclock-research.md`.
* **`mypy>=1.13` is unpinned and ungated** — structurally identical to the ruff hazard, one file away,
  and `strict` adds checks between minor releases. Deliberately deferred: pinning changes what `--strict`
  enforces across the whole `packages = ["fleet"]` surface.
* **`ruff format --check` still fails** — **116 of 253 files dirty at the base**, round F added exactly
  one. Class result, no integer: the count is formatter-version-dependent. **Deliberately NOT gated.**

---

## 6. The proposed `CLAUDE.md` amendment, and why it is item 1

**Guardrail 6's four-check battery can pass unanimously while measuring nothing. Round G measured this
twice, independently, in lanes auditing their own work:**

- One lane's detector keyed on `sha256(whole file)`; its **cosmetic control read 4 where it must read
  0**, because `MigrationState.updated_at` is `default_factory=utcnow` — the digest detected *"the
  projector ran"*, not *"the state moved"*. **Three of four checks had passed under the wrong quantity.**
- Another lane's WAL probe wrote an **identical payload** every transaction, so SQLite dirtied no page,
  every arm came out flat, and **all four checks passed vacuously**. Caught by reading the WAL header's
  `ckpt_seq` (0 throughout).

> **[Editorial correction — 2026-08-26, round-H lane W3. Every word above is left exactly as its
> author wrote it; this marker records what falsified one of its claims and where that claim came
> from.]** The framing *"Round G measured this twice, independently"* does not hold for the **first**
> bullet. That `sha256(whole file)` incident is **round F's**, lane **W13**: round F's `progress.md`
> records it as *"W13's first instrument was wrong and its own control caught it"*, and **both** round
> G's and round H's `LANE-PROTOCOL.md` carry it under the heading **"Round F's measured results"**,
> item 1. Round G's W3 never ran that instrument — its report calls the digest *"round F's result
> 1, re-derived here as a landed check"* and deliberately chose a different quantity, which is the
> fifth check **working**, not a second failure. Only the **second** bullet (the WAL probe, `ckpt_seq`
> 0 throughout) is a round-G finding, lane **W8**.
>
> **The class is unaffected and the amendment rests on it unchanged**: the four-check battery passed
> unanimously over nothing **once in round F and once in round G** — one instance per round, in
> consecutive rounds, each found by the lane auditing its **own** instrument. What does not survive is
> the raw total, *"twice, independently"*, attributed to a single round. The `CLAUDE.md` amendment this
> section proposes carries the per-round attribution instead.

**The fifth check the rules do not yet require: name the quantity the instrument watches, and say why
the defect could not leave it unchanged.** A third lane then applied it unprompted and rejected three
candidate quantities by measurement — *file exists* and *line in range* are **invariant under drift**
(409 of 409 in range while **46 are wrong**), *token in range* certified a method green by coincidence.

**A second amendment candidate**: the zero-change gate has a **second** blind spot beside the unimported
mutation — **a mutation that changes the file and destroys the module**. One lane's mutation made all 5
cases fail in **0.49 s**; the gate read 4 in both forms and could not see it. **Runtime and blast radius
are the tell.**

---

## 7. Traps still live

1. **A commit groups by file ownership; a review groups by where it looked. Neither is a claim.** Three
   recurrences in two rounds; the worst would have had a lane **edit two correct sentences**.
2. **Citations drift inside a single round.** Two repointed at `f5a188a` had **already re-drifted** by
   `3dd500d` — moved by the commit that repointed their siblings. The instrument now catches this class
   for one file only.
3. **An assertion that passes for the wrong reason.** Six of 34 tests stayed green through a real
   continuation because their floor was `BUILD` and the delegate refused with a `UsageError` — **exit 2,
   the same code the refusal used**.
4. **A test's two anchors coinciding.** A round-G mutation came back GREEN the first time because the
   fixture's anchors coincided and the case passed **under the exact defect it existed to catch**.
5. **An instrument's verdict can depend on which checkout runs it.** `6,970` is not a property of the
   tree: **182** in a worktree, **6,975** in the primary, 5,302 of that `.venv` paths. Three lanes passed
   that number around.
6. **`.superpowers/` is IGNORED, not merely untracked** — `git clean -fdx` still deletes it (`-x` is
   exactly that flag) and it no longer appears in `git status`. It holds rounds D–G, including round G's
   16 lane reports and the ledger with every ruling and its cost-if-wrong.

---

## 8. What round G could not verify

- **The `+2` term in the test delta** is inferred from the total, not measured per-file.
- **`6,970`/`6,971` in ADR-0093** are not re-derivable outside the primary and are bounded, not asserted.
- **No test constructs a path to `docs/INTEGRATION_HONESTY.md`'s prose** — the new instrument resolves its
  *citations*, but nothing can catch a **false sentence** written there.
- **The citation instrument covers ONE file.** `docs/DECISIONS.md` is costed and **goes red on day one**:
  346 citations, **31 naming a path that resolves to no file**, plus 52 pins. `docs/SPEC.md` is free
  (0 citations in this form).
