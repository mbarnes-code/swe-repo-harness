# Handoff — round J

**Written at the close of round I. Base for round J: `main` at `ecce634`.**

---

## 0. How to read this document

| tag | meaning |
|---|---|
| **[suite @ SHA]** | certified by the whole-tree suite at that anchor |
| **[probe @ SHA]** | re-derived from the code or tree by a named probe at that anchor |
| **[ledger]** | reported by the lane that measured it; not independently re-derived here |
| **[decision]** | a ruling or deliberate deferral, not a measurement |

**Round I's reading rule.** Round H's was *"five orchestrator claims corrected, none of them invention"*.
Round I's is the same mechanism located precisely:

> **The unmeasured sentence is never the conclusion. It is the REASON offered for the conclusion.**
> Seven instances across two rounds, in seven distinct venues: a handoff paragraph, an ADR clause, a code
> comment, a test docstring, a promoted plan's adjudication rationale, a method claim about instrument
> independence, and a shared-assumption list. **Every one sat inside work that was otherwise correct and
> well measured.**

Conclusions get measured because a mutation targets them. **Reasons ride along.** This is now
`CLAUDE.md` Guardrail 6's final bullet (`2b522a1`).

**The corollary round I added, from a lane that fixed one instance and produced the next inside the fix:**
**a derivation closes the class it ranges over and nothing else.** A lane replaced an unmeasured
shared-assumption list with a *derived* one for characters; the list's non-character half rode along
unmeasured, and an extensionless path stayed invisible to both probes — including the citation the
document was about.

---

## 1. Read these first, in order

1. **This document.**
2. **`CLAUDE.md`** — amended at `2b522a1`. Guardrail 6 gained the reason-is-unmeasured bullet; §6's
   "state what you ran, including what you excluded" gained the covering-set clause **and** the truncation
   clause. **Nothing renumbered** — all five line-anchored citations of that file are byte-identical.
3. **`docs/DECISIONS.md`: ADR-0095** (the `references/` block, and what it does not achieve).
4. **`docs/superpowers/plans/decisions-citation-instrument.md`** — promoted this round. **S1 and S2 are
   landed; S3–S7 remain**, and its §2 numbers were corrected twice by later lanes (see §5).
5. **`docs/superpowers/plans/waveclock-d82-d84-build-plan.md`** — **D84-1 and D84-2 landed; six subtasks
   remain.** Its adjudication rationale was **corrected by measurement** this round (see §4), and **24 of
   its 78 line-anchored citations had rotted at `924b159`** — re-anchor before quoting.

---

## 2. Verified state at `ecce634`

| claim | how verified | tag |
|---|---|---|
| **`1942 passed`, 0 failed, **0 skipped**, xfail 0**, clean `bazel disk` (peak 3.72 GiB, residual 0 bytes, cache 1130 MiB), 877.68s | `.venv/bin/python -m pytest -q` — **no `-k`, no node IDs, no path arguments** — primary checkout, **`git status --porcelain` EMPTY at stamp**, **every lane finished before the run began** | **[suite @ ecce634]** |
| Anchor **identical before and after** on `HEAD`, tracked tree, porcelain **and seven per-blob hashes** | `diff` of the two stamps; the only differences are a descriptive label and the timestamp — **no measured field** | **[probe @ ecce634]** |
| Round I = **7 commits** off `924b159` | `git log --oneline` | **[probe @ ecce634]** |
| Test total **1936 → 1942** | both endpoints measured | **[probe @ ecce634]** |

**Two things about that certification are deliberate.** It ran with **no lane live** — round H's only
qualified run was crossed by my own landing and was **silent about the very commit that crossed it**. And
it stamps **per-blob hashes** as well as the tree, a distinction round H's reviewer taught: **a tree anchor
proves no COMMIT landed; per-blob anchors prove no uncommitted sibling edit crossed the run.**

**The `+6` is NOT decomposed per commit.** Both endpoints are measured; the decomposition is not, and is
not claimed. Round H's reviewer showed a measured decomposition is obtainable by `--collect-only` per
commit if round J wants one.

**`ruff` and `mypy` were run per-lane and clean, but not re-run at `ecce634`.** Same disclosure as round
H, same reason: the last commits touch files neither tool reads under its manifest scope. **Argued, not
measured.**

---

## 3. What round I landed

| commit | what |
|---|---|
| `05553e9` | a `BEGIN` behind a SQL comment escaped the read-transaction instrument — **three forms, not the one filed** |
| `96ec623` | **D84-1** — a breached wave mutated every member's git before admission could refuse it |
| `96468b2` | **S1** — the citation instrument could not carry a second document without its assertions going vacuous |
| `2b522a1` | **`CLAUDE.md` 6.1 + 6.2**, and the §6.2 annotation of a claim a lane falsified |
| `9b61878` | **S2 / ADR-0095** — the `references/` block named 2 of 4 corpora, and the 2 it omitted carry every citation |
| `f7b62bf` | **D84-2** — BUILD's in-loop re-cut, and the fleet-wide pass **filed OPEN rather than closed** |
| `ecce634` | the citation census re-derives unchanged after ADR-0095, and **why the raw count moved while the by-rule one did not** |

---

## 4. D84 — 2 of 8, and the plan's own rationale was corrected

**`f7b62bf` filed the fleet-wide PASS-2 block `OPEN`, and D84 is `PARTLY ADDRESSED`, not fixed.**

**The correction that matters for the six remaining subtasks:** the plan recommends disclosing PASS 2
rather than patching it, **because guarding it needs a weaker predicate.** That conclusion is right and
**that reason is not the operative one.** Implementing the rejected option as a throwaway gives **exit 1,
not 4** — `RootFileDomainDriftError`, no build plans at all — because **PASS 2 is the ONLY populator of
`plans`**, which PASS 3, PASS 4 and `_check_root_file_domain` all read regardless of admission. Measured
twice, by the lane and independently by its reviewer with its own guard **[probe @ 96ec623]**.

**And PASS 2 is a DEFERRED DEFECT, not a stated boundary.** The stop rule sorts on **reachability**, and
the path is ordinary: `waves` has no phase column, so TRANSFORM stamps every wave, and the wallclock budget
is **cumulative across phases** — transform before lunch, build after, and PASS 2 is reached already
breached **on a fresh run with no resume**. *"exit 4 → exit 1" is a result about ONE candidate fix;
whether any fix exists was never measured.*

**Load-bearing advisory for D82-3 and D84-2's successors** **[probe @ 924b159]**: `only=` is inert, so the
guards' safety rests on **`breached` being monotone within an invocation** — the payload builders do a bare
`plans[repo_id]`, and a skip-then-admit would `KeyError`.

**And the Rule 12 trap still stands: a CROSS-PHASE D84 fixture becomes a semantic no-op once D82 lands**,
passing under the exact defect it exists to catch. Breach in the phase you drive.

---

## 5. Open, with owners named or explicitly absent

* **D84-3 next**, and it inherits three things: file the PASS-2 residue `OPEN`, set D84 `PARTLY
  ADDRESSED`, and **the D84 ledger entry carries a `Still NOT fixed, and correctly so` marker recording a
  superseded "wait for D82" ruling** which **goes false on landing** **[ledger]**. **Unowned.**
* **S3–S7 of the citation plan.** S1's seam is in and S2 landed. **S3's scope line is the risk the
  research names: a bare "skip what does not resolve" lands green while asserting nothing about 305
  citations.**
* **`_BEGIN`'s mode grammar** — `BEGIN DEFERRED TRANSACTION` is valid SQLite, opens a read transaction, and
  `_BEGIN` does not classify it. **A misdiagnosis, not an escape** (it fails RED identically before and
  after `05553e9`), **accidentally reachable, unreached today, currently loud.** Not a land blocker.
  **Unowned.**
* **`.gitignore:87` is cited at four sites** (`docs/DECISIONS.md:5170`, `:5741`, `docs/PROGRESS.md:4632`,
  `:4829`); git reported it at `:95` before this round and `9b61878` moved it to `:100`. **Pre-existing
  drift; no lane touched the four.** **[ledger]**
* **Three further live-reading `346` sites** in `docs/superpowers/plans/` (baseline `:177`/`:181`,
  handoff-h `:202`, handoff-i `:126`) — **all still CORRECT under the by-rule subtraction**, swept and
  reported, not edited **[probe @ 9b61878]**.
* **`mypy>=1.13` unpinned and ungated**; **`ruff format --check` still fails and is deliberately not
  gated.** Both carried unchanged from round H.
* **Nothing binds `docs/INTEGRATION_HONESTY.md`'s prose**, and nothing binds `CLAUDE.md`'s prose. Round I
  measured `CLAUDE.md`'s **citations** at 0-of-109 broken and repaired two code fragments; **the prose is
  unbound by design and disclosed as such.**

---

## 6. Proposed `CLAUDE.md` amendments

### 6.1 Whitespace normalisation does not join a per-line continuation marker — take this first

This project has been bitten by line-oriented sweeps **at least five times** and has answered every time
with *"normalise whitespace across the whole file"*. **That answer is incomplete and nobody had noticed.**

**Whitespace-flattening does not join a Python block comment**, because the continuation `#` is not
whitespace: the flat text reads `runs before # any wave opens`. Measured with a gate read first — flatten
finds **1 of 2**; **stripping `#\s?` per line before flattening finds 2 of 2** — and corroborated on real
un-injected text **[probe @ 2b522a1]**. **Generalised by the reviewer: the same defeats `--`, `*`, `>` and
`//`.** A lane's own sweep missed its own comment.

### 6.2 A monotonically growing self-referential corpus admits no publishable raw total

A lane published a per-line citation count; a reviewer got a different one; the lane got a **third**,
minutes later. Cause: **the sweep counts the untracked round artefacts written ABOUT the correction while
it is being measured** — 12 → 17 → 19, attributed file by file, and a fourth reading reproduced 19 exactly
because no new artefact had been written since **[probe @ 96468b2]**. **The stable predicate is
`git ls-files` membership**, which two lanes reproduced digit for digit.

### 6.3 Two smaller ones, both measured

- **Assert a staged set by MEMBERSHIP AND COUNT, never by a sorted string.** My own landing guard aborted
  twice on **collation order** — a correct set, rejected because a locale sorts `docs/…` before
  `.gitignore`. **An instrument whose verdict depends on the environment is the D85 class**, arriving in
  the recipe `CLAUDE.md` §6 recommends.
- **Version lane patch files.** Two scoped re-reviewers this round **could not diff the fix against the
  version they were reviewing**, because the lane overwrote its patch in place. Both substituted a weaker
  check and **said what it proves and does not** — the right response — but `<lane>-r<N>.patch` is free.
  Two lanes adopted it spontaneously once told.

---

## 7. Traps still live

1. **The reason is the unmeasured sentence.** Seven instances, seven venues. §0.
2. **A derivation closes the class it ranges over and nothing else.**
3. **"Two instruments sharing no code" is a claim.** Round I caught **two false** independence claims —
   one sharing the verbatim character class that decides the outcome, one whose probe was built on **the
   very function under test** — and **verified one true** one. The difference every time was whether
   anyone ran it.
4. **A retraction is a member of its own class.** Three occurrences this round: a correction quoting the
   citation it retired, a §10 paragraph quoting its own retired patterns, and an ADR counting its own
   quotations. **Subtract by rule, or publish no total.**
5. **A covering test set derived from where code LIVES is wrong in both directions.** Measured three times
   by three lanes on three functions: a symbol grep names files with **zero** executions and **misses**
   files that drive the site. Mechanism: those files `monkeypatch` the impls away. **Instrument once to
   derive the set (~422 s), then run the set (~30 s) — a ~14× ratio; the absolutes move with the machine.**
6. **`.superpowers/` is IGNORED, not merely untracked.** `git clean -fdx` deletes it. It holds rounds D–I.

---

## 8. What round I could not verify

- **The `+6` test delta is not decomposed per commit** (§2), deliberately.
- **`ruff` and `mypy` were not re-run at `ecce634`** (§2) — argued, not measured.
- **No pin's upstream reachability and no shallow-clone byte reproduction** were verified for ADR-0095:
  both need the network, which is barred. The ADR says so in its own §6.
- **Whether any fix exists for PASS 2** was never measured — only that one candidate fails (§4).
- **Seven of one lane's mutations** were never independently re-run and remain `[ledger]`.
- **Seven orchestrator claims were corrected by lanes this round**, every one caught by the lane rather
  than by me. Two of them were rulings a reviewer then judged wrong on inspection. **Assume the same rate
  applies to this document.**
