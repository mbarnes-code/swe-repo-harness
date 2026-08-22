> Round-E research artifact, produced by lane **R2**, promoted unchanged from untracked scratch
> `.superpowers/sdd/round-e/lanes/R2/report.md` by lane **W30**. **§5 is the live brief for subtask 9** (tasks 9a–9f), which is unimplemented at `1698de3`. Body below is byte-identical to the source; this header prepends 3 lines, so a citation of scratch `report.md:N` is line `N+3` here.

# R2 (research, read-only) — costing ADR-0079 so subtask 9 can be dispatched

**Refs.** Brief said HEAD `b49662c`; it had already moved. Everything below is anchored at
**`f4eade0`** (main) unless a ref is named. Intermediate measurements were taken at `69ff1d2`;
`git diff --stat 69ff1d2 f4eade0 -- src/fleet/cli.py docs/SPEC.md` is **empty**, so every `cli.py`
and `SPEC.md` claim measured at `69ff1d2` holds unchanged at `f4eade0`. `docs/DECISIONS.md` gained
176 lines (`7b85680`, ADR-0091 markers) — no ADR I cite was touched.

**Interpreter.** Every number from `.venv/bin/python` (`/home/redmage/swe repo harness/.venv`,
3.12). **Tests NOT run — suite lock.** No edits, no commits; a per-lane detached worktree
(`.superpowers/sdd/round-e/lanes/R2/wt-69ff1d2`) was created, used and **removed** (`git worktree
list` verified clean).

**Working tree is NOT `f4eade0`.** `src/fleet/cli.py` and `tests/test_cli.py` are modified and
`tests/test_resume_unblocking.py` is untracked — **subtask 8d is wired but uncommitted**. §1 below
reports both states separately, because they give different answers, and per CLAUDE.md guardrail 6
a read-only audit must not conflate landed with in-flight.

---

## 0. The short answer

**ADR-0079 is still needed — YES.** But it is **smaller than its reservation says**, and one of its
four sub-questions is now a different question than the one reserved.

| Sub-question | Verdict |
|---|---|
| `--from-phase` semantics (ambiguity 4) | **LIVE, undecided, and the options are measurably different.** This alone justifies the ADR. |
| `--repo` semantics | **Narrowed to a consistency ruling** by a five-verb precedent, with one genuinely open sub-question (does it scope the run-wide steps?). |
| `--reset-attempts` semantics | **Live but small**: one audit `findings.kind` to allocate and one row-set to name. |
| Do `--revalidation` / `--raise-revalidation-rounds` stay refused? | **DISSOLVED into a recital.** Three primary sources concur, none dissents. But the *reason the code gives* for refusing them is **false**, and that is a real edit ADR-0079 must order. |
| **NEW, created by subtask 8 landing** | **Should subtask 9 land at all before subtask 10?** With step 6 built, step 8 is the only absence left, and none of the three flags has a continuation to scope. This is a live sequencing decision that did not exist when §4.3 was written. |

---

## 1. Is `_refuse_unbuilt_resume_flags`' prose still true? — three separate answers

The orchestrator asked whether `c135c42`'s correction has gone stale in a new way. It has not gone
stale in the way feared, and it **is** false in a way that predates `c135c42` and that `c135c42`
made *more* specific rather than less.

### 1.1 The step-5 claim: CORRECT, and `c135c42` holds. Do not send anyone at it.

At `f4eade0` the docstring says step 5 **is** built and runs unconditionally. Verified against
`_resume_impl`, which calls `_demote_to_floors` with no flag guard. **Correct. Not subtask 9's.**

### 1.2 The step-6 claim: TRUE at `f4eade0`, and the in-flight 8d lane has already corrected it.

At `f4eade0`, `_resume_impl` contains no step 6 (grepped the whole function body for
`unblock|blocked_by|step 6`: zero). So "step 6 has no implementation" was **true** at `f4eade0`.

A normalised whole-file sweep of the tracked tree at `69ff1d2` (predicate: `step[s]? 6` within 200
non-`.` chars of `absent|do not exist|no implementation|not built|unbuilt|missing`, either order,
case-insensitive; normaliser: strip `"`, `'`, `\`, then collapse every maximal whitespace run to one
space over the **whole file**, offsets mapped back to 1-based lines; corpus: all 268 UTF-8-decodable
files from `git ls-tree -r --name-only`) gave the **class result: 14 sites**, of which **5 in
`src/fleet/cli.py`** — `resume.__doc__`, the `ResumeIncompleteError` raise, the
`_refuse_unbuilt_resume_flags` docstring, its `UsageError`, and one in a `build`-side docstring
(`cli.py:7897` at `69ff1d2`, "steps 6 and 8 have no implementation").

**Instrument validated four ways before its result was believed:** fires on the known-bad file
(cli.py, 5 hits); silent on an already-clean file (`orchestrator/scheduler.py`, 0); fires on a
synthetic wrapped fault injected into that clean file (1); and stays green under a cosmetic reindent
of cli.py (5, unchanged) — so it asserts meaning, not layout. A line-oriented `grep` on the same
synthetic wrapped fault returns **0**, which is why the normaliser is load-bearing.

**Re-measured against the uncommitted working tree**, all five phrasings return **0** and the built-
steps list now reads `steps 2, 3, 4, 5, 6 and 7`. **The 8d lane took the whole class in its own
commit.** Nothing for subtask 9 to inherit here — and it must not "fix" it again.

### 1.3 The claim that IS false, for 2 of the 5 flags — the finding

Both surfaces state one reason covering all five flags: *"each one scopes or re-drives the
**CONTINUATION** this verb cannot perform"*, with the absence named as step 8 (working tree) /
steps 6 and 8 (`f4eade0`).

**Exercised, not read.** Driving `_refuse_unbuilt_resume_flags` directly at `69ff1d2` for each flag
in isolation: all five refuse, and there is exactly **1 distinct message body** — the same text for
every flag. Probing that body:

| probe | result |
|---|---|
| `step 6`, `step 8`, `CONTINUATION`, `PhaseRunner`, `Phases 1–4` | **NAMES** |
| `stub`, `revalidation`, `3.5.1`, `storm`, `row 34` | **SILENT** |

So an operator who types only `--revalidation batched` is told the flag scopes a Phase 1–4
continuation and that `PhaseRunner` assembly is what is missing. Both are false for that flag. The
real reason is that the resume's `stub_reconcile` / revalidation re-enqueue step is unwired — and
the message never mentions it.

**Verified from primary source that these two are a different workstream** (§4.1's claim,
re-measured at `69ff1d2`, all three confirmed):
- `docs/SPEC.md` §10 gives `--revalidation eager|batched|manual` on **`fleet stubs resolve`** too
  (row `fleet stubs`, `resolve` flag cell) — it is a §3.5.1 stub-lifecycle knob, and `resume()`'s
  parameter is typed `Revalidation | None`, the same enum.
- `docs/SPEC.md` §13 **row 34** is *"Revalidation storm"*; its controls are
  `max_revalidation_rounds` / `revalidation_max_cost_usd` / `repo_ledger.revalidation_usd` /
  `tasks.revalidation_key`. Nothing in row 34 is a re-entry floor.
- `orchestrator/stubs.reconcile` (its docstring literally names itself `stub_reconcile`, §3.5.1,
  §13 row 35) **exists and has zero callers in `src/`** — the same pure-but-unwired shape as
  `plan_unblocking`. So the absence is real; it is just not step 8.

**`--reset-attempts` is a weaker case and I judge the message defensible for it**: attempts only
bind a *re-run*, so the flag is continuation-adjacent. The split is **2 of 5 false, 3 of 5 true** —
exactly §4.1's split, now independently confirmed by exercising rather than by reading.

**The correction made it narrower, not gone.** At `f4eade0` the message blamed steps 6 *and* 8; the
8d lane's rewrite blames **step 8 alone**. For `--revalidation` the offered justification is now a
single, precisely wrong claim. This is CLAUDE.md's "the fix reliably introduces a narrower overclaim"
pattern, reproduced inside a commit whose purpose was fixing the adjacent clause.

**Class result:** *"the refusal states a reason that is false for `--revalidation` /
`--raise-revalidation-rounds`"* — **2 sites, both in `src/fleet/cli.py`** (`_refuse_unbuilt_resume_
flags` docstring, and its `UsageError`), **0 elsewhere**. Unchanged by `c135c42` and by 8d.

### 1.4 Both prose instruments FAIL-OPEN on step 6 — measured

The two tests that exist to catch exactly this class do not catch it. Measured by evaluating each
assertion against the **unchanged** message text (which is precisely the defect's signature):

- `tests/test_cli.py::test_resume_refuses_the_flags_whose_behaviour_does_not_exist` asserts
  `"steps 2, 3, 4, 5 and 7" in built`. With step 6 built and the prose untouched → **PASSES
  (fail-open)**. With the prose correctly updated → **FAILS**. It is a tripwire on the *fix*, not on
  the defect. Its own docstring claims *"a successor that builds step 6 and forgets to add it trips
  too"* — **that sentence is measurably false.**
- `tests/test_cli.py::test_the_step_5_refusal_does_not_call_step_2_absent_beside_its_own_step_2_lines`
  parses the absent-step list out of `ResumeIncompleteError` and asserts `"2 (" not in absent`,
  `"4 (" not in absent`, `"8 (" in absent`. There is **no step-6 clause**, so
  `Steps 6 (recompute blocked_by) and 8 (...) are absent too` stays green forever. **Fail-open.**

The 8d lane fixed the prose anyway, so no harm landed — but the guard rail did not hold, and it will
not hold for step 8 either. **Agent Recommendation:** replace the substring check with an assertion
that derives the built-step list from the steps `_resume_impl` actually runs, so the prose is bound
to the code rather than to a hand-maintained literal.

---

## 2. The five refused flags, by symbol

All five live in `_refuse_unbuilt_resume_flags`' `unbuilt` dict and in `resume()`'s signature.

| Flag (symbol) | What it would do | What implements / fails to | Changed by subtask 8? | Refused because… |
|---|---|---|---|---|
| `from_phase: int \| None` (Typer `min=1, max=4`) | Scope or relocate the re-entry floor | `reentry.phase_floor` computes the floor; `demotable_phases` filters the span; **nothing reads `from_phase`** | **No** — step 6 is repo-level, not phase-indexed, so `--from-phase` has no meaning for it | **unbuilt** — and undecided (§3) |
| `repo: str \| None` | Restrict the resume to one repo | `_demote_to_floors` iterates all candidate repos; no repo filter anywhere | **Yes, in scope** — once step 6 runs, `--repo` also scopes `_unblock_dependents` and the appended synthetic wave | **unbuilt** |
| `reset_attempts: bool` | Zero `phases.attempts` | `_DEMOTE_PHASE_SQL` deliberately omits `attempts` from its SET list; `complete_phase` is the only writer of that column in `src/` | No | **unbuilt** |
| `revalidation: Revalidation \| None` | Select §3.5.1's storm policy for this resume | `stubs.reconcile` exists, **zero callers in `src/`**; `_resume_impl` carries a comment saying the step "does not exist on `main` yet" | No | **a DIFFERENT reason** — §3.5.1 / §13 row 34, not the floor and not step 8 |
| `raise_revalidation_rounds: int \| None` | Raise the per-consumer `max_revalidation_rounds` sub-ceiling | same | No | **a DIFFERENT reason** — same as above |

---

## 3. Ambiguity 4 — the three options, verbatim, then measured

Verbatim from `docs/superpowers/plans/resume-step5-subtask-7-research.md` §4.3:

- **Option A — override**: *"`floor := Phase(from_phase)`, ignoring the computed floor"*
- **Option B — clamp**: *"`floor := min(computed_floor, Phase(from_phase))`"*
- **Option C — filter**: *"the floor is untouched; `--from-phase` restricts *what is acted on*"*,
  with two readings — **(i) repo filter**: *"act only on repos whose computed floor is ≥
  `from_phase`"*; **(ii) span cap**: *"demote no phase below `from_phase`"*.

### 3.1 The measurement §4.3 does not contain

Everything in §4.3 is the round-D lane's **costing**. I drove the shipped `reentry.phase_floor` and
`reentry.demotable_phases` at `69ff1d2` over six repo states × `--from-phase ∈ {2,4}`. Cells read
`floor N -> [phases actually written]`.

| fixture | fp | base | A | B | C(i) | C(ii) |
|---|---|---|---|---|---|---|
| S,S,PEND,PEND; evidence holds at 1 | 2 | 2 → [2] | 2 → [2] | 2 → [2] | 2 → [2] | 2 → [2] |
| S,S,PEND,PEND; evidence holds at 1 | 4 | 2 → [2] | **4 → []** | 2 → [2] | *repo skipped* | **4 → []** |
| S,S,PEND,PEND; no evidence | 2 | 1 → [1,2] | 2 → [2] | **1 → [1,2]** | *repo skipped* | 2 → [2] |
| S,S,PEND,PEND; no evidence | 4 | 1 → [1,2] | **4 → []** | 1 → [1,2] | *repo skipped* | **4 → []** |
| S,**DEGRADED**,S,PEND | 2 | 3 → [3] | **floor 2 = ON the hard stop** → [3] | **floor 2 = ON the hard stop** → [3] | 3 → [3] | 3 → [3] |
| S,**SKIPPED**,S,PEND | 2 | 3 → [3] | **floor 2 = ON the hard stop** → [3] | **floor 2 = ON the hard stop** → [3] | 3 → [3] | 3 → [3] |
| RHI at phase 3 | any | None | None* | None* | None | None* |

\* only because my harness short-circuits on `phase_floor` returning `None`. A bare
`floor = Phase(from_phase)` that never consults `phase_floor` would demote an RHI repo.

`_HARD_STOPS = ['DEGRADED','SKIPPED']`; `_SETTLED_FOR_DEMOTION = ['DEGRADED','SKIPPED','SUCCEEDED']`.

### 3.2 Every §4.3 cost, checked against the primary source

| §4.3 claim | Verdict at `69ff1d2` |
|---|---|
| Constraint 1: `--from-phase` is `int \| None`, Typer `min=1, max=4` | **TRUE** (`resume()` signature) |
| Constraint 2: step 8 absent; `demote_to_floor`'s span is `phase >= floor` over `SUCCEEDED` only | **TRUE** (`demotable_phases` returns `phase >= floor and statuses.get(phase) is SUCCEEDED`; `_DEMOTE_PHASE_SQL` carries `AND status = 'SUCCEEDED'`) |
| Constraint 3: a committed SPEC sentence is contradicted by one option | **TRUE** — `docs/SPEC.md` §10, `fleet resume` row: *"continue from each repo's re-entry floor (§11.5 step 5), never the earliest incomplete phase."* |
| **A.1** can point the floor ABOVE the computed floor | **TRUE, and understated.** Measured: at `--from-phase 4` A writes **nothing at all** in 3/3 non-`None` fixtures. `fleet resume --from-phase 4` becomes a zero-write resume that still exits reporting success. |
| **A.2** can land the floor ON a `DEGRADED`/`SKIPPED` phase | **TRUE, measured.** The *write* is safe (`demotable_phases`' SUCCEEDED filter returns `[3]`, not `[2,3]`), but the **reported floor is 2 — the hard-stop row itself**, a floor `phase_floor` can never return. ADR-0082 §4's provenance hazard was disclosed *on the premise that the floor comes from `phase_floor`*; A invalidates that premise, and ADR-0082 §5 requires §4's premises to be **re-derived, not re-read**. **A's true cost includes reopening ADR-0082 §4.** |
| **A.3** contradicts the SPEC §10 sentence | **TRUE**, quoted above |
| **A.4** needs an RHI carve-out | **TRUE and mandatory.** `phase_floor` returns `None` when any phase is RHI; `docs/SPEC.md` §12 item 46(ii) requires a test driving **`fleet resume`** to find it unable to move a repo out of RHI. A without the guard **breaches a committed acceptance criterion.** |
| **B.1** naive `min` can push the floor past a hard stop, and `phase_floor` does not expose the boundary | **TRUE, with one correction.** Measured: at `fp=2` against a `DEGRADED` phase-2 row the floor lands **ON** the stop, not below it; *below* needs `fp=1`. The structural half is exactly right — `phase_floor` `break`s and **discards** where it stopped, returning only `Phase \| None`, so B must extend `phase_floor`'s return or re-derive the boundary in the caller. |
| **B.2** `--from-phase 4` is almost always a silent no-op | **TRUE, and stronger than stated.** Measured: identical floor *and* identical demotion set to the no-flag base in **3/3** non-`None` fixtures. |
| **B.3** `--from-phase 2` on a repo whose floor is 1 resumes from 1 | **TRUE, measured** (row 3: base `1 → [1,2]`, B `1 → [1,2]`) |
| **C.1(ii)** is "A's upward half without its downward half" | **TRUE, measured** — identical to A at `fp=4`; differs at `fp=2` on the hard-stop fixtures, where C(ii) keeps the computed floor 3 and A drops to 2 |
| **C.2** reading (i) defers rather than prevents | **TRUE** as a logical consequence; the subtask-10 half is unmeasurable (step 8 does not exist) |
| **C.3** largest documentation burden | **NOT VERIFIABLE** — a judgement about operator expectation, not a measurable property. Report it as such. |
| §4.4's line numbers `10086` / `10474` (at `6bf198f`) | **ROTTEN**, as §6 of that document predicted. Cite by symbol. |
| §4.4's *"a line-oriented grep returns 0"* | Already corrected to **1** by ADR-0089 §5. Do not re-inherit the 0. |

**Net: every §4.3 cost survives contact with the primary source. Two are understated (A.1, B.2),
one is slightly imprecise (B.1), one is unverifiable (C.3), and one line-number pair is dead.**

### 3.3 Which are still live at HEAD — one line each

- **Option A (override) — LIVE, and it is the most expensive.** Measured: silently writes nothing at
  `--from-phase 4`, lands the reported floor on a hard-stop row, reopens ADR-0082 §4, requires an RHI
  carve-out to satisfy §12 item 46(ii), and forces an edit to the committed SPEC §10 sentence.
  **Agent Recommendation: reject.**
- **Option B (clamp) — LIVE, and the safest of the two that keep the flag meaningful**, but it is
  **not one line**: it needs `phase_floor` to return where its walk stopped, a clamp-within-the-
  hard-stop-interval rule, and per-repo reporting of which bound won (or B.2's silent no-op is a
  fresh instance of the exact defect this refusal exists to prevent). **Agent Recommendation:
  acceptable, at the stated cost.**
- **Option C(i) (repo filter) — LIVE, and it is the only option that never touches the floor.**
  Measured: it either returns the computed floor unchanged or skips the repo — it can never land on
  a hard stop, never point above the evidence, never reopen ADR-0082 §4, and never edit SPEC §10.
  Its cost is a *reporting* problem (a partially reconciled ledger that reads as reconciled), which
  is cheaper than the *state* problems A and B buy. It also composes with `--repo`, the other
  scoping flag in the same subtask. **Agent Recommendation: prefer this one.**
- **Option C(ii) (span cap) — LIVE but dominated.** Measured identical to A at `fp=4` (writes
  nothing) while buying only part of A's simplicity. **Agent Recommendation: reject** — it inherits
  A.1 without inheriting A's one advantage.

### 3.4 For each, the SPEC sentence that must move in the same change (Guardrail 7)

- **A**: `docs/SPEC.md` §10, `fleet resume` row — *"continue from each repo's re-entry floor (§11.5
  step 5), **never the earliest incomplete phase**"* must be rewritten, because an override makes the
  verb continue from `from_phase`, which is not the re-entry floor. **Cite by symbol/row, not line:**
  a second copy of the *"never the earliest"* wording lives in §3.5's propagation block and is about
  un-blocking, **not** this flag — it must **not** be edited. Two sentences, one phrase; the sweep
  finds both, only one moves.
- **B**: SPEC §10 survives unedited. What must be added is the clamp's hard-stop rule, and
  `reentry.phase_floor`'s **docstring** must stop promising a `Phase | None` return if the return
  grows a boundary. `tests/test_floor_rule_statements.py` parses the floor rule out of prose and
  checks it against `reentry._HARD_STOPS` — **a floor-rule reword will move that census**, so B's
  diff must include it.
- **C(i)**: SPEC §10 survives unedited. One sentence must be **added** to the `fleet resume` row
  saying what `--from-phase` filters (repos, not phases), and `resume()`'s docstring and the
  `--help` text must say the same, or C.3's documentation cost lands as a defect.
- **All options**: the `_refuse_unbuilt_resume_flags` docstring and `UsageError` shrink to two flags,
  and §1.3's false reason must be corrected **in the same commit** — the ADR should say so, because
  the two prose instruments will not force it (§1.4).

### 3.5 New finding kind / register entries

- **`--from-phase` / `--repo`: none.** No new `findings.kind`. Neither is "audited" in SPEC §10.
- **`--reset-attempts`: one new kind is required.** SPEC §10 marks it *"(explicit, audited)"*, and
  the audited-flag precedent in the same row is `--raise-budget` / `--raise-wave-budget` →
  `RunBudgetRaised` / `WaveBudgetRaised`. No kind covering an attempts reset exists: the declared set
  in `src/fleet/state/schema.sql`'s `findings.kind` comment block has no member for it. A new kind
  (e.g. `AttemptsReset`) must be **declared there in the same commit that emits it**, or
  `tests/test_findings_kinds.py`'s recognition census fires by `file:line`.
- **D-number: next free is D79.** D76/D77/D78 exist under `## ` (two hashes), not `### ` — a
  `^### D<n>` probe returns D75 as the maximum and is **wrong**. State the predicate; this is the
  CLAUDE.md "read the entry body" hazard in a new spelling.
- **ADR-0079's reservation text needs correcting in the same change.** `docs/DECISIONS.md`'s
  `## ADR-0079 — RESERVED` block says *"Subtask 9 has not started — it depends on subtasks 7 and 8,
  **neither of which has landed**."* Subtask 7 landed at `2f0db34`, and subtask 8 is landing now.
  That sentence is already false and will be more false by the time the ADR is written.

---

## 4. Is ADR-0079 still needed? — **YES**

Not a manufactured decision. Three independent tests:

1. **No primary source settles ambiguity 4.** `docs/SPEC.md` §10 lists `--from-phase 1..4` and gives
   no semantics (grep for `from.phase` over SPEC: **1 hit**, the flag list). ADR-0078 §7 says in
   terms *"It does not decide … `--from-phase` semantics (ambiguity 4, subtask 9)"*. ADR-0089 §6
   does not decide it. `design-resume-step5.md` names it as an open ambiguity.
2. **The options are not cosmetically different.** §3.1 measures them producing **different demotion
   sets on ordinary fixtures**, including two (A and C(ii) at `--from-phase 4`) that turn a real
   reconciliation into a **zero-write run**. A decision that changes what gets written to `phases` is
   not a documentary decision.
3. **Two of A's costs reach outside subtask 9** — reopening ADR-0082 §4, and breaching SPEC §12 item
   46(ii) without a carve-out. That is exactly what an ADR is for.

**What has dissolved**, and should be recorded as recitals rather than deliberated:
- **`--revalidation` / `--raise-revalidation-rounds` stay refused.** Three concurring primary sources
  (`design-resume-step5.md` row 9 states it as the criterion; SPEC §10 puts `--revalidation` on
  `fleet stubs resolve`; SPEC §13 row 34 is a cost class with no floor in it) and **no dissenting
  source**. ADR-0089 §6 calls it *"still subtask 9's question"* — true that it is unratified, but
  there is nothing to weigh. **One sentence, not a section.** What is *not* dissolved is §1.3: the
  message's stated reason for refusing them is false and must be fixed.
- **`--repo` is a consistency ruling, not a design question.** `--repo NAME` already means "act on
  this one repo" on **six other verbs** — `plan`, `build`, `verify`, `migrate_repos`, `transform`,
  `pr` — measured by walking `cli.py`'s AST at `f4eade0` for a `repo` parameter whose annotation
  contains the Typer option string `"--repo"`: **7 declarations, 6 of them not `resume`**, all
  `str | None`. (A raw `grep '"--repo"'` returns **9**; two are not declarations — one
  `json.dumps(["fleet","transform","--repo",repo_id])` and the refusal dict's own key. Stated
  because the raw total and the class result disagree.) The only genuinely open part: **does it scope the run-wide steps?**
  Steps 2 (orphan reap) and 3 (stale-lease sweep) are run-scoped by construction — a repo-scoped
  reap would leave orphans standing — and step 7's projection is regenerated whole from SQLite.
  **Agent Recommendation: `--repo` scopes steps 5, 6 and (later) 8 only; 2, 3, 4 and 7 stay
  run-wide, and the payload must say so.** That is one paragraph.

**What is NEW and is the strongest reason to write the ADR now rather than reuse §4.3:**

> **With step 6 built, none of the three flags has a continuation to scope until subtask 10.**
> §4.3's constraint 2 already noted that all three options are observable *only* as different
> demotion sets while step 8 is absent. Subtask 8's landing sharpens this: step 8 is now the *sole*
> remaining absence, so un-refusing `--from-phase` in subtask 9 ships a flag whose entire observable
> effect is "which rows got demoted" — and whose operator-facing name promises something else.
> **Agent Recommendation:** rule the semantics now (so subtask 9's tests bind), but consider
> **landing the un-refusal with subtask 10** rather than before it, or landing subtask 9 with the
> flags accepted and their scoping asserted **only** over the demotion set, with the `--help` text
> saying so explicitly. Do not ship a flag that reads as "resume from here" while nothing resumes.
> This is a ruling, not a research finding — I flag it rather than resolve it.

---

## 5. Dispatch-ready decomposition of subtask 9

Under the recommended options **C(i) for `--from-phase`** · **`--repo` scopes steps 5/6/8 only** ·
**`--reset-attempts` audited via a new `AttemptsReset` kind**. One logical unit per worker.

**Ordering fact the decomposition depends on, measured.** In `_resume_impl` the scoping flags must
be threaded through **before** `_demote_to_floors`, because `_demote_to_floors` builds the candidate
repo set itself and `_unblock_dependents` (uncommitted 8d) consumes the floors it returns. Anchor by
symbol: the `floors, computed_floors = await _demote_to_floors(...)` statement and the
`_unblock_dependents(...)` call immediately below it. **9a must land before 9b.**

| # | Task | Files | Success criterion | Test file |
|---|---|---|---|---|
| **9a** | Thread `repo: str \| None` into `_demote_to_floors` and `_unblock_dependents` as a candidate-set filter. Pure plumbing: no floor arithmetic, no new semantics. Steps 2, 3, 4 and 7 stay run-wide and the payload gains a `scoped_to_repo` key so "0 demoted" and "not looked at" cannot read as each other (the D44 shape `_demote_to_floors`' own docstring records). | `src/fleet/cli.py` | With `--repo X`, exactly the rows of `X` are considered by steps 5 and 6; `reaped_containers`, `stale_running_reset` and `projection` are **byte-identical** to the unscoped run on the same fixture | `tests/test_cli.py` |
| **9b** | `--from-phase` as **Option C(i)**: a repo whose computed floor is `< from_phase` is reported in a new `skipped_by_from_phase` bucket and **not demoted**. The floor is never recomputed, never clamped, never overridden. `phase_floor` is not touched. | `src/fleet/cli.py` | On the §3.1 fixture set, the demotion set for every repo is either **identical to the no-flag run** or **empty**; never a third value. An RHI repo is skipped by `phase_floor` returning `None` **before** the filter is consulted | `tests/test_reentry_floor.py` + `tests/test_cli.py` |
| **9c** | `--reset-attempts`: zero `phases.attempts` for exactly the rows step 5 demoted (**not** the whole run), inside the same `StateWriter` unit, plus one `AttemptsReset` finding. Declare the kind in `schema.sql`'s `findings.kind` comment block **in this commit**. Fingerprint per `(run_id, repo_id, phase)` — the `_demotion_fingerprint` precedent — or three phases of one repo collapse into one row. | `src/fleet/cli.py`, `src/fleet/state/repository.py`, `src/fleet/state/schema.sql` | Without the flag, `attempts` is unchanged on every row (the §11.5 steps 3/4/5 invariant). With it, `attempts = 0` on **exactly** the demoted rows and unchanged elsewhere; the census in `tests/test_findings_kinds.py` recognises the new kind with **zero** recognition gap in both directions | `tests/test_findings_kinds.py`, `tests/test_cli.py` |
| **9d** | Shrink the refusal to two flags **and fix §1.3's false reason in the same commit**. The remaining message must name §3.5.1 / §13 row 34 and `stub_reconcile`'s absence — **not** step 8 — as why `--revalidation` and `--raise-revalidation-rounds` are refused. Correct `resume.__doc__` and the `ResumeIncompleteError` message in the same diff if either restates the flag set. | `src/fleet/cli.py` | Exercising the refusal per flag yields **two** message bodies, not one: the revalidation pair's names stubs/§3.5.1 and is **silent on** `PhaseRunner` and `Phases 1–4`; the other three flags no longer refuse at all | `tests/test_cli.py` |
| **9e** | Re-bind the two fail-open prose instruments (§1.4). Derive the built-step list from the steps `_resume_impl` actually runs rather than a literal, and add the missing step-6/step-8 clause to the absent-step assertion, failing by `file:line`. | `tests/test_cli.py` | Four Rule-12 checks reported with numbers: fires on known-bad (step 8 landed, prose unchanged), silent on a clean tree, fires on a synthetic fault, and a **control** — a cosmetic reflow of the message literals — stays green | itself |
| **9f** | Write ADR-0079 to the orchestrator's ruling; correct the `## ADR-0079 — RESERVED` block's *"neither of which has landed"* sentence; correct the SPEC §10 `fleet resume` row **only if option A is ruled** (it is not, under the recommendation). Mark `resume-step5-subtask-7-research.md` §4.3 as settled with a dated marker — **annotate, never rewrite**. | `docs/DECISIONS.md`, `docs/superpowers/plans/resume-step5-subtask-7-research.md`, `docs/superpowers/plans/design-resume-step5.md` row 9 | §4.3 carries a dated marker naming the ADR; design row 9's criterion matches what landed; the ADR states the two recitals (§4) as recitals | n/a |

**The pytest command a later lane must run** (I did **not** run it — suite lock), with **no `-k`
filter**:

```
.venv/bin/python -m pytest tests/test_cli.py tests/test_reentry_floor.py \
  tests/test_reentry_evidence.py tests/test_findings_kinds.py \
  tests/test_floor_rule_statements.py tests/test_state_models.py \
  tests/test_schema_sql.py tests/test_resume_unblocking.py
```

plus `python -m mypy` with **no path arguments** (the manifest sets the scope: `pyproject.toml`
pairs `strict` with `packages = ["fleet"]`).

---

## 6. What this report cannot catch

- **No suite run.** Every runtime claim comes from standalone `.venv` scripts. The refusal's
  per-flag behaviour, `phase_floor`, `demotable_phases` and both prose instruments' assertions were
  **exercised**. `_demote_to_floors`' write path, `demote_to_floor`'s transaction and
  `_unblock_dependents` were **read from source**, not exercised — no database fixture was built.
- **The three-option matrix uses six hand-chosen fixtures, not an exhaustive enumeration.** It
  demonstrates that the options differ and how; it does not bound the difference. A `7^4`-style
  sweep of the kind `_resume_impl`'s step-5 comment records (17,680 inputs) would, and is cheap.
- **The working tree is mid-commit.** Subtask 8d is wired and uncommitted; §1.2's "the 8d lane fixed
  the class" is a claim about a tree that has no SHA and can still change before it lands. Re-run the
  §1.2 sweep at 8d's actual commit before relying on it.
- **§1.3's judgement that `--reset-attempts`' stated reason is "defensible" is mine**, not a
  measurement. If the orchestrator reads attempts as orthogonal to the continuation, the split is
  3-of-5 false rather than 2-of-5, and 9d's message grows a third case.
- **The step6-absent sweep is keyed to one predicate family.** A site spelling the claim a different
  way ("step 6 is a stub", "no `blocked_by` recompute exists") escapes it. It is a class result for
  the phrasings it names, not a proof of absence.
- **C.3 is unverifiable and I have said so** rather than passing the round-D lane's judgement on as
  a cost.
