# Lane R1 (round I, RESEARCH) — can `docs/DECISIONS.md` get a citation instrument?

**Base ref: `924b159` on `main`** (`git rev-parse 924b159` →
`924b15947e6624fda91eaecec85a21557b45a3ba`). Every number below is anchored at that ref unless a
different ref is named in the same sentence. **I landed nothing** — no code, no tracked document, no
citation repointed. Worktrees (read-only, detached):
`/tmp/claude-1000/-home-redmage-swe-repo-harness/roundi-r1/at-924b159` and `…/at-3dd500d`.

The primary checkout was read only by `git`, and never written to. Sibling lanes are live, so the
primary's working tree is NOT `main`; nothing here was measured against it.

---

## 0. The three instruments, and why they are genuinely different

Guardrail 6 requires any number that matters to be derived two genuinely different ways, because two
instruments sharing a blind spot agree on a wrong number. Three were built.

| | **Probe A** | **Probe B** | **Probe C** |
|---|---|---|---|
| script | `probe_a.py` / `probe_unres.py` | `probe_b.py` | `probe_history.py` |
| what it is | the **landed** module's own machinery (`tests/test_integration_honesty_citations.py`, loaded by path, `_LEDGER_REL` repointed) | an **independent** re-implementation sharing no code | **git history** — not the current tree at all |
| recogniser | one monolithic `_CITATION` / `_ANCHORED` regex | backtick **token stream**: split the block on backticks, classify each inline-code token | reuses A's site list; asks a different question of each site |
| normaliser | collapse all whitespace **document-wide**, map offsets back | join physical lines **within a blank-line-delimited block** | n/a |
| path universe | `rglob` of `src/` + `tests/` + `docs/` with an extension whitelist (**263** files) | `git ls-tree -r 924b159` — every **tracked** path (**291** files) | the tree at the *introducing* commit |
| anchor rule | one adjacency regex requiring parentheses | previous inline-code token, gap ≤ 40 non-backtick chars | — |
| span rule | `_logical_span` (decorator / blank / comment widening, no constant) | raw AST span ± 3 | raw AST span ± 3, at the intro commit |
| question | "is the cited range inside the symbol's span **today**?" | same, different machinery | "**was it inside the span at the commit that wrote the citation?**" |

Probe C is the one that matters. No current-tree instrument can ask its question, and it is what
answers question 2.

**Probe C validated four ways (Guardrail 6, all four checks):**

| check | how | result |
|---|---|---|
| **(a) fires on known-bad** | run it over `docs/INTEGRATION_HONESTY.md`'s 46 and compare with lane W10's *independent* history derivation (git line-tracking, `docs/superpowers/plans/citation-instrument-baseline.md` §1) | W10's verified **genuine-drift** pair — `.on_exhausted` (`settings.py:427-428`) and `.clone_timeout_s` (`settings.py:267-268`) — both read **TRUE_WHEN_WRITTEN**, spans `(428,428)` / `(268,268)` at intro `4846fb0c1`. W10's **verbatim-record** class (the five `go.py` citations at ledger `:787`) and its **usage-site** class (`response.usage`, `buildverify.py:1158`) all read **FALSE_WHEN_WRITTEN**. Two lanes, two unrelated methods, same partition. |
| **(b) silent on the swept case** | run it over the **14** `docs/DECISIONS.md` citations that resolve today | **14/14 TRUE_WHEN_WRITTEN**, zero false alarms |
| **(c) synthetic fault in a clean site** | take the clean `WorkerOutput.checkpoint_is_current` (`workers/base.py:256-259`, span `(257,259)` at intro `c10526d17`) and shift the cited range | −100 → **FALSE**; +40 → **FALSE**. Fires in both directions. |
| **(d) cosmetic control** | same site, range varied within the widening (258-259) | **TRUE** — a reflow-scale change does not move the verdict |

**Probe C's stated blind spot:** it locates the introducing commit with
`git log --reverse -S<citation text> -- <doc>`, so where the *same citation text* occurs more than
once in the document, all occurrences inherit the **first** introduction. That is exactly right for a
verbatim quotation (which is the point) and wrong for an independent later re-citation of the same
line. It is a floor on TRUE_WHEN_WRITTEN, not a census.

---

## 1. The real numbers (question 1)

Everything below reproduced **identically at `3dd500d` and at `924b159`**, across a `+118`-line
change to the file (`f9ff651`, ADR-0094, which added zero pathed citations). So the handed figures
are not stale — they are re-derived-equal, not merely copied forward.

### 1.1 Raw totals, per probe, with the predicate beside each

| quantity | **probe A** (universe `src`/`tests`/`docs`, `_ANCHORED` adjacency) | **probe B** (universe = tracked files, gap ≤ 40 anchor) |
|---|---|---|
| pathed citations | **346** | **346** |
| resolve to exactly one file | 303 | 305 |
| ambiguous basename | **12** | **12** |
| path resolves to no file | **31** | **29** |
| commit-bound (`` (`:N` at `sha`) ``) | **0** | — |
| out of range (past EOF) | **0** | — |
| anchored `.py` citations | **66** | **100** |
| anchored and resolving | 14 | 29 |
| anchored and **not** resolving | **52** | **71** |

### 1.2 Verdict on each handed figure

| handed `[ledger]` | verdict | why |
|---|---|---|
| **346 citations** | **CONFIRMED** | two recognisers that share no code and no normaliser return the same integer. This is the one raw total I would put in a document. |
| **31 dead paths** | **QUALIFIED — predicate-dependent, and the class result is the opposite of what the number implies** | 31 holds **only** under `_SEARCH_DIRS = ("src","tests","docs")`. Under the tracked-file universe it is **29**; the two extra are `pyproject.toml:33` (`:300`) and `pyproject.toml:33-36` (`:5588`), a tracked repo-root file the landed instrument's index deliberately excludes. Under a universe that also includes `references/` on disk it is **5**. See §2. |
| **52 anchored-unresolved** | **CONFIRMED under the landed anchor predicate, and it is a FLOOR not a census** | probe B's looser anchor rule finds **100** anchored citations and **71** unresolved. Quote it as *"52 under `_ANCHORED`"*; never as "the number of broken citations in `DECISIONS.md`". |
| **max ADR = 0094** | **CONFIRMED** | re-derived form-agnostically: `ADR-[0-9]{4}` union over `docs/` + `src/` + `tests/` + `CLAUDE.md` → max `ADR-0094`; the highest heading in the file is `## ADR-0094`. |

Nothing handed to me was **falsified**. One (31) is *right under its predicate and misleading without
it*, which is the failure mode Guardrail 6 names.

### 1.3 Class results — these are the ones to carry forward

Guardrail 6 prefers a class result to a raw total, and round H had raw totals fail to reproduce six
times while every class result held. Six class results, each stated with its predicate:

* **C1 — Every pathed citation in `docs/DECISIONS.md` points at a line its file still has: the
  out-of-range class is 0 of 303.** Predicate: for each uniquely-resolved citation, `1 ≤ lo` and
  `hi ≤ len(file.splitlines())`. Identical for `docs/PROGRESS.md` (0 of 229).
* **C2 — Every ambiguous citation is the same basename: 12 of 12 are `base.py`, and all four
  candidates are in-tree** (`src/fleet/{ecosystems,manifests,models,workers}/base.py`). Reading the
  prose at `:5717` and `:2489` shows the full path is given in the *same paragraph*
  (`src/fleet/workers/base.py:740-742`, `src/fleet/ecosystems/base.py`) and the bare form is a
  **continuation citation**. This class is not drift and not debt; it is a citation *form*.
* **C3 — The dead-path class is dominated by out-of-tree reference material: 24 of 29 point into
  `references/`.** Predicate: suffix-resolve each dead path against the whole disk excluding
  `.git`/`.venv`/`bazel-*`. 20 resolve uniquely under `references/`, 1 ambiguously (`sandbox.py:974`,
  three candidates), 3 carry a literal `...` elision in the written path
  (`libs/code/.../local_shell.py`, `libs/code/.../hooks/runner.py`,
  `libs/code/.../integrations/sandbox_factory.py`) whose basename resolves under `references/`. **Only
  5 remain**: 3 name Bazel `rules_js` internals (`npm/private/npm_translate_lock_generate.bzl:207-225`,
  `npm_translate_lock_helpers.bzl:612-621`, `npm_translate_lock_generate.bzl:418`) which are on no
  disk here at all, and 2 are the `src/fleet/state/migrations/__init__.py:242` pair discussed in §2.3.
* **C4 — `docs/DECISIONS.md` contains zero commit-bound citations.** Predicate:
  `` \(`:\d+(-\d+)?` at `[0-9a-f]{7,40}`\) `` over the whitespace-normalised whole file → **0**
  (`docs/INTEGRATION_HONESTY.md`: 2). The looser form `` at `<sha>` `` occurs **183** times, so the
  document *does* stamp claims with commits — just never inside a citation.
* **C5 — The 52 do not share a drift offset: they occupy 39 distinct `(file, offset)` pairs across
  17 files.** Predicate: offset = `span_start_today − cited_start`. `cli.py` alone holds 16 citations
  at **13** distinct offsets spanning −8900 to +1158. Compare the sibling file, where *"six
  `settings.py` citations share that one offset"*. **There is no bulk repair here.**
* **C6 — 31 of the 52 never resolved at the commit that introduced them.** Probe C: **21
  TRUE_WHEN_WRITTEN / 31 FALSE_WHEN_WRITTEN**. This is the finding the whole document turns on.

---

## 2. Question 2 — one class or several? **Several, and a different several.**

Short answer: **`docs/DECISIONS.md` does not have `docs/INTEGRATION_HONESTY.md`'s structure.** The
sibling baseline names three causes (genuine drift · usage-site citations · verbatim records) and says
no rule separates them. Here there are **at least five**, the largest is one the sibling does not have
at all, and — the useful part — **two of them ARE separable by rule**, by an instrument nobody has
built yet.

### 2.1 The five causes, measured

| # | cause | size | separable by rule? |
|---|---|---|---|
| **1** | **Citations into `references/` — a deliberately gitignored, read-only third-party corpus** | **24** of the 29 dead paths | **Not by any rule available today** — see §2.2. This class does not exist in the sibling file. |
| **2** | Citations into upstream that is on **no** disk here (Bazel `rules_js` internals) | 3 | Only by enumeration |
| **3** | **Genuine drift** — true when written, moved since | **21** of the 52 anchored | **YES — by probe C.** |
| **4** | **Never resolved**: usage-site citations, name collisions, and citations copied inside verbatim quotations | **31** of the 52 anchored | **YES, as a group** — probe C separates 4 from 3 cleanly. It does **not** separate usage-site from verbatim-quotation; both are FALSE_WHEN_WRITTEN. |
| **5** | **A citation quoted inside its own retraction** | 2 (one original, one quotation) | By reading; not by count. |

### 2.2 Why the `references/` class is the hard one, and why a repair would be a disaster

`.gitignore:91-95` reads:

    # ---- Third-party reference corpora (their own git repos) ----
    # Cited by path in docs/DECISIONS.md; re-fetch to fact-check those citations:
    #   https://github.com/All-The-Vibes/Agent-Harness.git              @ d1aab58
    #   https://github.com/visa/visa-vulnerability-agentic-harness.git  @ 3d972f6
    references/*/

So the project has **already decided** these citations point out of tree, and has already recorded how
to fact-check them. **Any instrument that demands they resolve is asserting the opposite of a landed
decision, and any sweep that "repairs" them destroys correct citations.** This is the single most
important thing in this document.

Three further measurements make it concrete:

* **The class is environment-dependent, exactly as D85 predicts.** `references/*/` is gitignored, so
  the four reference sub-repos (`Agent-Harness`, `deepagents`, `open-swe`,
  `visa-vulnerability-agentic-harness`) are **present in the primary checkout and absent from a
  detached worktree** — measured directly: `ls -d references/*/` returns 4 in the primary and **0** in
  `…/roundi-r1/at-924b159`, whose `references/` holds only the four tracked `.md` files. An instrument
  that widens `_SEARCH_DIRS` to include `references/` would give **different verdicts in the two
  trees**, which is the failure the landed module's `_SEARCH_DIRS` comment exists to prevent.
* **A prefix rule cannot work**, because the citations are written as bare suffixes
  (`backends/sandbox.py:962-968`, `agent/server.py:906-914`), not as `references/...` paths.
* **The `.gitignore` comment is incomplete, and it is incomplete in exactly the repos that carry the
  citations.** Attributing the dead paths by basename: **deepagents 12, open-swe 11, visa 3** — and
  neither `deepagents` nor `open-swe` is listed in that block with a URL and a pinned SHA. The
  fact-check instruction the comment promises does not cover the majority of what it is about.

### 2.3 The in-tree pair, and why it must not be swept

The only two dead paths naming an in-tree file are both
`src/fleet/state/migrations/__init__.py:242` — at `docs/DECISIONS.md:10930` and `:11031`. Reading them:

* `:10930` is the original claim. It is **wrong** — the real path is
  `src/fleet/migrations/__init__.py:242`, with no `state/`.
* `:11031` is inside an **editorial-correction blockquote in the same file** that already diagnoses it
  ("*The citation `src/fleet/state/migrations/__init__.py:242` does not resolve; the path is
  `src/fleet/migrations/__init__.py:242`, with no `state/`*"), quoting the wrong path verbatim so a
  reader can see what was retracted. It **must not be repointed** — that is CLAUDE.md's
  "a retraction and its quarry are textually indistinguishable to a count-based detector", live in
  this file. The same correction records that `12ac784`'s **commit message** carries the same wrong
  path and is landed history, annotated rather than rewritten.

**One in-tree citation is wrong and one is a correct record of it being wrong. A count-based
instrument sees two identical defects.**

### 2.4 No structural container separates the classes — measured three ways

Every cheap "just exempt the historical parts" rule was tested and every one fails:

* **Blockquote (`>`) membership**: only **4 of 52** unresolved anchored citations sit on a blockquote
  line; **0 of 14** greens do. The rule exempts 8% of what it needs to and would be a fig leaf.
* **ADR membership, for the anchored class**: the 52 span 14 ADRs and the 14 greens span 9 — and they
  **overlap**. ADR-0068 holds 4 unresolved *and* 4 green; ADR-0070 holds 4 and 2; ADR-0049, ADR-0073
  and ADR-0077 each hold both.
* **ADR membership, for the dead-path class**: 29 dead paths across 6 ADRs, and two of those ADRs are
  citation-rich in-tree — **ADR-0070** (2 dead, 10 in-tree pathed citations) and **ADR-0092** (2 dead,
  10 in-tree). Exempting a whole ADR blinds the instrument to 20 real in-tree citations.

The only rules that separate anything are **content** rules: probe C's history question (causes 3 vs 4)
and a declared manifest of reference-project file paths (cause 1). Both are buildable. Neither is free.

### 2.5 The answer, stated plainly

> **The sibling's finding does not transfer, and that is good news in one direction and bad news in
> the other.** Bad: `DECISIONS.md` has a fifth cause the sibling does not — 24 correct citations into a
> gitignored corpus — and it is the *largest* dead-path class, so a rule-based sweep here would not
> merely repoint records, it would **corrupt correct citations into a corpus this repo has decided not
> to vendor**. Good: for the anchored class, git history **does** separate genuine drift (21) from
> never-resolved (31), by rule, reproducibly, and validated against a second lane's independent
> derivation on the sibling file. `docs/DECISIONS.md` is a decision **record**, so the historical
> reading is right — but "historical" is not a synonym for "unrepairable": **a citation that was true
> when written has a correct repair that is not repointing**, namely conversion to the commit-bound
> form the sibling file already uses (§3.4).

**An automated repair keyed on "does it resolve today" is the wrong shape. An automated
*classification* keyed on "did it resolve when written" is the right one, and it is the only rule in
this document that survived validation.**

---

## 3. Question 3 — what a day-one-green landing looks like

### 3.0 Which of the landed module's checks are already green here

Measured, not assumed, by pointing the landed machinery at `docs/DECISIONS.md`:

| landed check | on `docs/DECISIONS.md` at `924b159` |
|---|---|
| `test_every_pathed_citation_names_a_file_that_exists` | **RED** — 31 |
| `test_every_pathed_citation_points_at_a_line_the_file_still_has` | **GREEN** — 0 |
| `test_no_unpinned_anchored_citation_fails_to_resolve` | **RED** — 52 unless pinned |
| `test_a_commit_bound_citation_is_never_offered_for_resolution` | **RED** — it asserts `commit_bound` is non-empty and `DECISIONS.md` has **0** (class result C4) |

That last row is the landing trap: naively parameterising the module over a second document turns a
check that *proves an exemption is still exercised* into a check that fails for a document that never
had the exempted form. It must become a per-document declaration, not a global assertion.

### 3.1 Option A — pin the whole current unresolved set as a ratchet (83 pins)

* **Cost:** one round. The machinery exists; the pin block is generated.
* **Gives up:** honesty. 24 of the 31 dead paths **are not broken**. Pinning them publishes
  *"this citation does not resolve"* about a citation that is correct and deliberately out of tree.
  That is not honest disclosure and it is not a rubber stamp either — it is worse than both: it is a
  **false claim carrying a mechanism's authority**, which CLAUDE.md's Rule-12 stop rule calls a
  convention wearing a mechanism's clothes. **Reject A as stated.**
* The brief asks whether a ratchet pinning 52 unresolved anchors is honest disclosure or a rubber
  stamp. For the **52** it is genuine disclosure — every one of them really does fail to resolve, and
  the sibling module's both-directions design means the list cannot rot silently. For the **31** it is
  a rubber stamp, because the premise of the pin is false for 24 of them. The two halves need
  different treatment, which is why A-as-a-whole fails and B does not.

### 3.2 Option B — scope the instrument to what is genuinely live *(recommended)*

Instrument only citations whose path resolves **in-tree** (303 under probe A, 305 under probe B), and
**declare** the out-of-tree class as not-applicable with its reason, rather than pinning it as debt.

* Day one: existence check **not applicable, declared**; in-range check **green (0)**; anchored check
  **pinned at 52**, split by cause (§3.4).
* **Cost:** two rounds (S1 + S3, then S4 + S5).
* **Gives up:** no mechanical coverage of the 29 out-of-tree citations. That is the right trade —
  they are covered instead by the `.gitignore` pinned-SHA record, which S2 completes. Say so in the
  module docstring rather than implying closure.
* **The risk, named:** an undisclosed or over-broad "not applicable" is how a green claim outlives what
  it claimed about. If the scope line is written as "skip citations that do not resolve" it also skips
  any future *genuinely* broken in-tree path — the check becomes vacuous and stays green forever.
  It must be written as an **allowlist of declared out-of-tree suffixes derived from the reference
  manifest**, so a new in-tree dead path still fails. S1's covering test exists for exactly this.

### 3.3 Option C — repair first, instrument after

* **Cost:** high, and unbounded: 39 distinct `(file, offset)` pairs (C5) means per-citation manual work
  with no bulk offset, for 52 citations, in a file where an author must read the surrounding ADR to
  know whether the citation is about the symbol or about what that line said at the time.
* **Gives up:** the record. For the **21** TRUE_WHEN_WRITTEN citations, repointing replaces a true
  statement about a past tree with a statement about today, inside a document whose entire purpose is
  to record what was decided and why. For the **31** FALSE_WHEN_WRITTEN ones, repointing is either
  meaningless (a usage site has no definition span to point at) or actively destructive (a verbatim
  quotation).
* **Reject C as a bulk strategy.** Accept it for exactly **one** citation: `docs/DECISIONS.md:10930`
  (§2.3), whose repair is already specified by the file's own correction.

### 3.4 The recommendation

**B, with the pin split by probe C's class, plus a single targeted repair.**

1. Land the instrument scoped to in-tree-resolvable citations (B).
2. Pin the 52, but **as two named sets, not one**:
   * `_PINNED_DRIFTED` (**21**, TRUE_WHEN_WRITTEN) — debt with a **known correct repair that is not
     repointing**: convert to the commit-bound form `` (`:N` at `<intro-sha>`) `` the sibling file
     already uses and this module already exempts. Probe C *emits the intro sha for each one*. That
     repair preserves the record **and** is drift-proof, so the entry leaves the pin permanently.
   * `_PINNED_NEVER_RESOLVED` (**31**, FALSE_WHEN_WRITTEN) — usage sites and verbatim quotations.
     **Do not repoint.** Each entry carries its intro sha as evidence that it never resolved.
   This is what turns the ratchet from a rubber stamp into a **classified debt register**: each pin
   states not just *that* it is unresolved but *which of two mutually exclusive things* it is, and the
   claim is falsifiable by re-running probe C.
3. Repair `docs/DECISIONS.md:10930` only; leave `:11031` (the retraction's quotation) untouched.
4. Complete the `.gitignore` reference-corpus block with `deepagents` and `open-swe` (S2).

**Cost if this recommendation is wrong.** The failure mode is the scope line in step 1 being written
as a bare "skip what does not resolve". The instrument then lands **green** and asserts nothing about
the 305 in-tree citations — an outage wearing a green suite's clothes, undetectable by the suite,
which is how round E's undisclosed `-k` filter hid a red on `main` for most of a round. Concretely:
**one wasted wave to land it, plus however many rounds pass before someone re-derives the census** —
and the citations it was supposed to protect drift for all of them. S1's covering test
(`test_every_profile_declares_every_assertion_explicitly`) is the whole mitigation and must land
*before* S3, not with it.

---

## 4. Question 4 — decomposition into one-round subtasks

Seven. **S1 → S3 and S4 → S5 are ordered; S2, S6, S7 are independent.** Every ADR number is written
`ADR-XXXX`; the orchestrator allocates at dispatch (measured max at `924b159` is **0094**).

### S1 — Make the instrument document-**profiled**, so no assertion can go silently vacuous
* **Change:** `tests/test_integration_honesty_citations.py` — replace the `_LEDGER_REL` constant with
  a frozen per-document profile: doc path, search dirs, the declared out-of-tree suffix allowlist, the
  pin sets, and an **explicit disposition for every aggregate assertion** (`assert` | `not_applicable`
  with a one-line reason string). No new document is covered by this subtask.
* **Success criterion:** `pytest tests/test_integration_honesty_citations.py` (no `-k`) is still
  **50 passed**, and the `INTEGRATION_HONESTY` profile reproduces the landed census
  **byte-identically**: 414 / 409 unique / 5 ambiguous / 2 commit-bound / 60 anchored / 46 unresolved
  / 0 unpinned failures / 0 stale pins.
* **Covering test:** `test_every_profile_declares_every_assertion_explicitly` — enumerate the
  aggregate checks **from the module** (not from a hand list) and fail by name if a profile omits one
  or gives `not_applicable` without a reason.
* **Rule 12 mutation that must redden it:** delete one assertion key from a profile → the covering
  test fails naming the key. **Old-passes/new-fails:** under that mutation the pre-S1 module has no
  such concept and passes; the covering test fails. Second mutation: change a `not_applicable` reason
  to the empty string → fails.

> **CORRECTED (2026-08-27), lane R1 — S1's success criterion says "is still **50 passed**". The
> figure is now **51**, and the sentence was self-contradictory the moment it was written.**
> Re-measured by this lane rather than taken on relay: `pytest
> tests/test_integration_honesty_citations.py -q` in a worktree detached at `96ec623` →
> **51 passed**. Corroborated a second, genuinely different way — from file content, counting
> non-parametrised `def test_` plus `_PINNED_UNRESOLVED` entries: `a76cc80` → 4 + 46 = **50**;
> `041ee60`, whose subject line is *"the citation instrument contradicted itself"* and which added
> `test_every_census_number_this_module_states_is_the_number_it_derives`, → 5 + 46 = **51**;
> unchanged at `924b159` and `96ec623`. **The 50 was true at its own source and rotted after it** —
> `docs/superpowers/plans/citation-instrument-baseline.md` §6 measured 50 at `3dd500d`, a commit at
> which this module was not yet in the tree at all, and `041ee60` made it 51. This is drift, not an
> error in the source, which is why the sentence above is **left exactly as written** and this is an
> annotation beside it. **The sharper half, and it is the S1 lane's observation, not mine: "still 50
> passed" and "add a covering test" cannot both hold in the same subtask** — a covering test adds
> collected cases, so the criterion contradicts the bullet three lines below it. That is available to
> a careful reader with no re-measurement at all, and §1.2 of this very document says a carried
> number must be re-derived before it is built on; I carried this one. **Relayed and NOT verified by
> this lane:** the S1 lane reports **54** after S1 lands. S1 is not in the tree at `96ec623`, so I
> could not measure it; whoever lands S1 must re-derive it rather than inherit it from here.

### S2 — Complete the reference-corpus record (`ADR-XXXX` + `.gitignore`)
* **Change:** `.gitignore:91-95` and one ADR. Add `deepagents` and `open-swe` with upstream URL and a
  pinned SHA, matching the existing two entries' form; the ADR records **why citations into
  `references/` are correct, must never be repointed, and are out of scope for any citation
  instrument** — with the C3 numbers and the worktree-vs-primary presence measurement as evidence.
* **Success criterion:** the block names **4** corpora, each with a URL and a resolvable SHA; the ADR
  states the C3 class result with its predicate; `git status` clean apart from the two files.
* **Covering test:** `test_every_gitignored_reference_corpus_is_recorded_with_a_pinned_sha` — derive
  the corpus roots from `.gitignore`'s `references/*/` rule **plus** the set of directories the
  comment lists, and fail by name in **both** directions.
* **Note for the author:** the covering test must not read the filesystem — `references/*/` is absent
  in a worktree. Derive from the tracked text only.

### S3 — Land `tests/test_decisions_citations.py`, day-one green, scoped and disclosed
* **Depends on S1.**
* **Change:** one new profile + module for `docs/DECISIONS.md`. Existence check `not_applicable`
  (reason: reference corpora, §2.2) but replaced by a **narrower live check** — a citation whose path
  resolves to nothing **and** whose suffix is not in the declared out-of-tree allowlist still fails.
  In-range check asserted (green, 0). Anchored check pinned at 52 (flat, for now; S5 splits it).
* **Success criterion:** the new module passes with **no `-k` filter**; the census stated in its
  docstring is parsed back out and checked against the survey, as the sibling does;
  `python -m mypy` with **no path arguments** clean, plus an explicitly-disclosed
  `mypy tests/test_decisions_citations.py`; the meta-instruments
  (`test_instruments_are_armed.py test_findings_kinds.py test_config_keys_are_read.py
  test_blocked_by_writer_statements.py test_manifests.py test_lint_gate.py`) still pass.
* **Covering test:** the narrower live check itself. **Rule 12 mutation:** introduce a fabricated
  in-tree dead citation (`src/fleet/does_not_exist.py:1`) into a scratch copy of the doc → must fail
  by `docs/DECISIONS.md:<line>`. **Control:** reflow the paragraph containing a real citation → green.
* **The named trap:** if the allowlist is written as "skip anything that does not resolve", that
  mutation still **passes**. Read the mutation result before believing the green.

### S4 — Land the history classifier as a `tools/` script, not a suite test
* **Change:** one script under `tools/` reproducing probe C, plus its validation harness. **Not** a
  pytest test — it runs `git show` once per citation and is far too slow for the suite.
* **Success criterion:** re-runs `21 TRUE_WHEN_WRITTEN / 31 FALSE_WHEN_WRITTEN` at `924b159`, and
  reproduces all four validation checks from §0 with their numbers printed **before** each verdict:
  W10's two verified drift citations → TRUE; W10's five `go.py` verbatim-record citations and
  `response.usage` → FALSE; 14/14 of today's greens → TRUE; the ±shift synthetic fault on
  `checkpoint_is_current` → FALSE both directions; the within-widening control → TRUE.
* **Covering test:** a fast unit test over a **fixture repo** (a few commits built in a tmpdir), not
  over this repo's history — three fixtures: true-when-written, false-when-written, and
  quoted-verbatim-later.
* **Must state its blind spot in the docstring:** the `-S` first-introduction heuristic (§0).

### S5 — Split the `DECISIONS.md` pin into `_PINNED_DRIFTED` (21) / `_PINNED_NEVER_RESOLVED` (31)
* **Depends on S3 and S4.**
* **Change:** replace the flat pin tuple with two named tuples, each entry carrying its intro sha as a
  comment. Docstring states the repair rule per set: **drifted → convert to the commit-bound form,
  never repoint; never-resolved → do not touch**.
* **Success criterion:** `21 + 31 = 52`, the two sets are disjoint, their union equals the flat pin
  set S3 landed, and every entry's intro sha is a real commit reachable from `main`.
* **Covering test:** `test_the_two_pin_sets_partition_the_unresolved_set` — union and disjointness
  checked against the **live survey**, not against a copy of the flat list.
* **Rule 12 mutation:** move one entry from `_PINNED_DRIFTED` to `_PINNED_NEVER_RESOLVED` → the
  partition test still passes (it is a partition either way) but a second check
  `test_every_drifted_pin_resolved_at_its_intro_sha` must fail. **That second check is the one that
  earns its place; the partition test alone is not sufficient and the author must not stop at it.**

### S6 — Repair the one wrong in-tree citation, and only it
* **Change:** `docs/DECISIONS.md:10930` → `src/fleet/migrations/__init__.py:242`. **Leave `:11031`
  exactly as it is** — it is inside the correction that diagnoses `:10930` and quotes it verbatim.
* **Success criterion:** an exact-match replacer that **aborts on mismatch**, one occurrence changed,
  `git diff --numstat` reports `1 1 docs/DECISIONS.md`; the sweep for
  `state/migrations` afterwards returns **1** (the quotation inside the correction) and that residue
  is settled **by reading**, not by count — state it that way in the commit message.
* **Covering test:** S3's narrower live check goes from 1 in-tree dead path to 0 of that class.
* **Re-measure at the moment of the change** — this citation's line number will have moved if a
  sibling lane appended an ADR.

### S7 — Prose binding for `docs/DECISIONS.md`: the honest scope, or an honest "no"
* **Change:** either one narrow instrument (see §5 — the 20 inline `grep` commands are the best
  candidate) **or** a stated NOT-IMPLEMENTED disclosure in the ADR from S2.
* **Success criterion:** if an instrument, it fires on a known-bad (mutate one `grep` claim's stated
  result) and stays green on a reflow control. If a disclosure, its scope line must **not** exclude
  the claim it exists to neutralise — the narrower-overclaim failure this project hit five times in
  one round.
* **This subtask is allowed to return "no tractable binding" as its result.** That is a landing, not
  a failure.

---

## 5. The prose question (the brief's "also answer")

**Is `docs/DECISIONS.md`'s prose bound by anything today? Essentially no — one sentence out of 94
ADRs and 11,381 lines.** Measured: three test modules mention the file
(`tests/test_floor_rule_statements.py`, `tests/test_sandbox.py`, `tests/test_cli.py`); only the first
*reads* it, and its `_EXPECTED_SITES` maps `"docs/DECISIONS.md": 1` — the single restatement of the
§11.5-step-5 floor rule in ADR-0076 §1. The other two mention it in a docstring. So round H's §8
finding transfers: a false **sentence** in `DECISIONS.md` is uncatchable today, and the ratio is worse
here than in the sibling file.

**Does `tests/test_floor_rule_statements.py`'s pattern generalise? Not in general — and saying so is
the useful answer.** That module works because its target sentence names three things a machine can
resolve: a status *set* (checked against `reentry._HARD_STOPS`), a cited *symbol* (resolved on the
module), and a *fallback phase* (fed to `phase_floor`). The load-bearing sentence of a typical ADR is
a **rationale**, and the reason half of a rationale is usually a counterfactual about an alternative
that was rejected and therefore never built. There is no artefact to check it against, and building a
mechanism that pretends otherwise is precisely the fake mechanism this project ranks below an honest
disclosure.

**But three sub-classes are tractable, and they are the honest scope for S7.** Sizes measured over the
whitespace-normalised whole file (predicate beside each), with `docs/INTEGRATION_HONESTY.md` alongside
for calibration:

| shape | predicate | `DECISIONS.md` | `INTEGRATION_HONESTY.md` | verdict |
|---|---|---|---|---|
| inline `grep`/`rg` command | `` `grep…` `` inline-code span | **20** | 47 | **Best candidate.** The claim *is* the command; re-run it and compare. Highest value per unit of cost, and it fires on a real class — this project has repeatedly published a "`grep` returns zero" that re-measured to one. |
| bolded absence claim | `**zero**` / `**no**` | **27** | 24 | **Tractable but narrow.** Only the ones paired with a resolvable symbol are checkable; the rest are prose absences. |
| symbol-set enumeration | ≥3 backticked identifiers joined by `/`, `,`, `and`, `or` | **89** | 53 | **Tractable, Layer-B shape.** Parse the set out, resolve each name against the module named beside it. Highest count, but the "module named beside it" step is where it will get expensive. |
| bolded numeric claim | `**<digits>**` | **119** | 108 | **Worst candidate — do not build this.** Most are measurements of a past tree at an unnamed commit. A live check on them manufactures the annotate-never-rewrite conflict at 119 sites and would push authors toward *editing the record* to make the suite green, which is the outcome CLAUDE.md forbids most explicitly. |

**Recommendation for S7: build the `grep`-command re-runner (20 sites) or nothing.** If it is not
built, the disclosure must say *"nothing binds this file's prose except the single ADR-0076 §1
restatement bound by `tests/test_floor_rule_statements.py`"* — naming the exception, because a scope
line that excludes the one thing that *is* covered is the narrower-overclaim failure again.

---

## 6. Question 5 — Rule 12 guidance for whoever builds it

### 6.1 The quantity, and why the defect cannot leave it unchanged

For the anchored check, keep the sibling's quantity — **whether the cited range is contained in the
logical span of the identifier named beside it** — for the reason its baseline gives: drift *is* motion
of that span while the cited range stands still.

For the **new** part of this instrument (the pin split, S5), the watched quantity is different and must
be stated separately:

> **whether the citation resolved at the commit that introduced it.**

A citation cannot both have resolved and not resolved at a fixed commit, so the classification defect —
putting a never-resolved citation in `_PINNED_DRIFTED`, thereby telling a future author to "repair" a
usage-site citation by repointing it — cannot leave that quantity unchanged. It is measured against a
frozen tree, so it is also **immune to drift**, which is the property that makes it a durable pin
comment rather than a rotting one.

### 6.2 Quantities to reject, with the measurements that reject them

Round G rejected three by measurement and all three rejections **re-measure true here**:

* **"the file exists"** — invariant under drift, and *worse than useless here*: it is RED on day one
  for 24 citations that are correct. Rejected twice over.
* **"the line is in range"** — invariant under drift: **0 of 303** out of range while **52** anchored
  citations are wrong (class result C1 against the 52). Keep it as a *different* check for the
  deletion/truncation case, and say in the module that it is a different quantity, not a weaker one.
* **"the name occurs as a token in the cited range"** — certifies by coincidence.
* **A fourth, specific to this file: "the offset is small".** Do not build a tolerance. `cli.py`'s 16
  citations occupy 13 distinct offsets from −8900 to +1158 (C5); a tolerance that admits +1158 admits
  everything, and one that rejects −8900 rejects the **usage-site** citations, which are correct in
  their own terms. There is no bound here that derives, so per Rule 11 there must be no bound.

### 6.3 Mutations that must redden the proposed instrument

Per case, which mutation reddens it — never a count of cases:

| # | mutation | must redden | why it discriminates |
|---|---|---|---|
| M1 | insert `src/fleet/does_not_exist.py:1` into a scratch copy of the doc | S3's narrower live check | the *only* mutation that distinguishes a real allowlist from "skip what does not resolve"; a bare skip passes it |
| M2 | rewrite one dead reference citation (`backends/sandbox.py:962-968`) to a path that resolves in-tree | S3's live check must stay **green**, and the allowlist census must **drop by one** | catches an allowlist keyed on the wrong thing |
| M3 | shift a green anchored citation by −3 (round G's own defect shape) | S3's anchored check, by `docs/DECISIONS.md:<line>` | the sibling's validated check (c) |
| M4 | move one entry `_PINNED_DRIFTED` → `_PINNED_NEVER_RESOLVED` | `test_every_drifted_pin_resolved_at_its_intro_sha` — **not** the partition test | the partition test is blind to it; this is the discriminating pair |
| M5 | delete one assertion key from a profile | S1's `test_every_profile_declares_every_assertion_explicitly` | the vacuous-green guard |
| M6 | reflow a paragraph holding a real citation to width 72 | **nothing** — green | the **control**. Round F: the cosmetic control is what caught an instrument passing three of four checks under the wrong quantity. |
| M7 | append an unparseable line to a `.py` file cited by exactly one pin | exactly **one** case; the module still imports | the fixture-vs-`parametrize` containment property (Rule 11's second half) |

### 6.4 Harness discipline, non-negotiable

* Gate every mutation on `git diff --numstat --no-index BACKUP MUTATED` — **backup-relative, never
  `HEAD`-relative**, because sibling lanes make the working tree differ from `HEAD` — and **print the
  gate before the test result**. The sibling lane's gate fired for real once, on a mutation string
  gone stale after `ruff format`.
* These modules import no `fleet` code, so the editable-install trap is not reachable through them —
  but S4's `tools/` script and any standalone driver **is** exposed. Run it as
  `env -i PATH=/usr/bin:/bin HOME="$HOME" PYTHONPATH="$WT/src" .venv/bin/python <driver>` and print
  `sys.executable` and the resolved module path, asserting the latter is under `$WT`, before any
  result. Everything in *this* document was measured with `env -i` and an asserted instrument path,
  which is why §0's table names the loaded file.
* `pytest` with `cwd` inside a worktree is structurally immune (pyproject `pythonpath=["src"]` +
  conftest `sys.path.insert`) and is the right way to run S1/S3/S5.

---

## 7. Scoping disclosed

* **No pytest session was run by this lane at all** — the primary's is reserved for the review lane
  and a detached worktree cannot run the full suite green (round F result 7). Every number here comes
  from a standalone probe under `env -i`, run against a worktree detached at a named ref. **This is a
  survey, not a certification**, and no claim here should be read as one.
* Probes A and B were run against **both** `924b159` and `3dd500d`; every census figure is identical
  at both. Probe C was run against `924b159` only.
* The whole-disk resolution in class result C3 was run in the **primary checkout**, because
  `references/*/` is gitignored and absent from the worktree — that read is stated rather than hidden
  precisely because it is the environment dependence the finding is about.
* The reference-repo attribution in §2.2 (deepagents 12 / open-swe 11 / visa 3) is by **basename**, so
  it over-attributes where a basename is shared (`__init__.py`). It is a distribution, not a census;
  the census is C3's 24.
* Nothing was edited. `docs/DECISIONS.md`, `docs/INTEGRATION_HONESTY.md`, `tests/`, `src/` and
  `CLAUDE.md` are untouched by this lane; no citation was repointed.

## 8. What this research still cannot tell you

* Whether any of the **21** TRUE_WHEN_WRITTEN citations is *about the line's then-content* rather than
  about the symbol. Probe C answers "did it resolve", never "does the surrounding sentence still mean
  what it meant". S5's author must read each ADR sentence before converting it, and the conversion in
  §3.4 is safe **because** it does not change the claim — that is the whole argument for it.
* Whether probe C's 31/21 split holds under a different anchor predicate. It was computed over probe
  A's 52. Probe B's 71 were not classified; the split is a property of the 52, stated as such.
* Whether the 3 Bazel `rules_js` citations were ever correct. No copy of that ruleset exists on this
  disk at any version, so no instrument here can check them, and S2's ADR should say so rather than
  pretending the reference-corpus rule covers them.
