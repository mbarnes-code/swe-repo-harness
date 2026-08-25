# Handoff — round F

**Written 2026-08-25 at the close of round E. Base for round F: `main` at `0b3fbae`.**

---

## 0. How to read this document

Every claim carries how it was verified and when. **Use the tag, not the sentence.**

| tag | meaning |
|---|---|
| **[suite @ SHA]** | certified by the whole-tree suite run at that anchor |
| **[probe @ SHA]** | re-derived from the code or tree by a named probe at that anchor |
| **[ledger]** | reported by the lane that measured it; recorded, not independently re-derived here |
| **[decision]** | a ruling or deliberate deferral, not a measurement |

**Round E's own hardest-won reading rule, from lane W32, first person:**

> *"this one reached me because I verified the numbers I doubted and accepted the one that came
> pre-formatted as a measured triple — the 'parts you accept are the unexamined ones' failure, from
> the inside."*

**Pre-formatted plausibility is itself the risk factor.** A bare assertion invites checking;
`(+782, +1416, +302)` looks already-checked. That exact triple travelled three hops unverified —
lane report → orchestrator brief → **`CLAUDE.md`**, the file loaded into every session — and was false.
**Fourteen claim-corrections were made in round E; ten originated with the orchestrator.** Every one was
caught by the lane that *received* it, never by the sender.

---

## 1. Read these first, in order

1. **This document.**
2. **`CLAUDE.md`** — amended twice in round E (`81b3b55`, `acc99fc`, corrected at `e93cbe3`). It is now
   **~42 KB, loaded every session**. **A measured consolidation pass is a round-F item (§6).**
3. **`docs/DECISIONS.md`: ADR-0079, ADR-0090, ADR-0091** — round E's three new records.
4. **`docs/INTEGRATION_HONESTY.md`: D76, D77, D78, D79, D80** — five new entries, all `OPEN, recorded
   only`.
5. **`docs/superpowers/plans/resume-step5-subtask-9-research.md`** and
   **`llm-cache-inert-research.md`** — promoted out of untracked scratch at `f36c9ad`; the second
   carries dated corrections at `5db4b2e`.
6. **`docs/superpowers/plans/design-resume-step5.md` rows 9 and 10** — both rewritten in round E. **Read
   the dated corrections, not only the row.**

---

## 2. Verified state at `0b3fbae`

| claim | how verified | tag |
|---|---|---|
| **Full suite: `1824 passed`, 0 failed, **0 skipped**, xfail 0, clean `bazel disk` (peak 3.72 GiB, residual 0)** | `.venv/bin/python -m pytest -q` — **no `-k`, no node IDs, no path arguments**; anchor stamped before the run and **verified unchanged after** | **[suite @ 0b3fbae]** |
| Round E = **44 commits** off `47df73d` | `git log --oneline 47df73d..HEAD \| wc -l` | **[probe @ 0b3fbae]** |
| Delta **36 files, +8,163 / −184**; `src/`+`tests/` **25 files, +5,025 / −132** | `git diff --shortstat` | **[probe @ 0b3fbae]** |
| `python -m mypy`, **no path arguments** → **115 files, no issues** | run by many lanes; `pyproject.toml`'s `packages = ["fleet"]` + `strict` set the scope, so **`tests/` is NOT covered** | **[ledger]** |
| **`main` was RED for most of round E** and is green now | `tests/test_cli.py` 101/0 at `f4eade0`, **100/1 at `209e760`**, fixed at `4268228` | **[ledger]** |

**The red survived because a lane ran its tests through a name-substring filter (`"resume"`, 39 of 106
names) and the failing test's name contained no `resume`. An undisclosed scoping.** That is why every
round-F brief should require **unfiltered runs and full pass/fail counts**.

---

## 3. Resume step 5 — **8 of 10 subtasks landed**

| subtask | state |
|---|---|
| 1–7 | landed before round E |
| **8** — step 6, recompute `blocked_by` + synthetic wave | **COMPLETE in round E** (8a–8g) |
| **9** — the step-5 flags | **SETTLED, NOT IMPLEMENTED.** ADR-0079 (`bd35a54`) records the semantics; **the un-refusal itself ships with subtask 10, by ruling** |
| **10** — step 8 "continue" | **NOT STARTED. Does not fit one round** (§4) |

Subtask 8's commits: `4902938`, `da45a43`, `227db4c`, `1d0c8f6`, `227e7ba`, `34d6f82`, `ae58e18`,
`0d7c8b8`, `b286dc4`. **[probe @ 0b3fbae]**

**What subtask 8 actually closed, and it is the round's substantive result:** a **reachable,
end-to-end silent undo of an audited `OperatorQuarantine`**. Quarantine a repo *between phases* and it
carried `{SCAN: SKIPPED, TRANSFORM+: SUCCEEDED}`; the highest-phase projection read `SUCCEEDED`; the
`blocked_by` entry was removed on the next resume. Found by review lane CR6, **reproduced end-to-end on
the real chain** by W16, closed at `227e7ba` by inverting the predicate to a **whitelist of landed
statuses over the blocker's SET of phase statuses**. **[ledger]**

---

## 4. Subtask 10 — costed, and it does NOT fit one round

**Source: `.superpowers/sdd/round-e/lanes/R4/report.md` (618 lines) — UNTRACKED. §9 below.**

The wiring is **one statement** (`cli.resume`'s final `raise`, pinned by three measured ordering
facts). What breaks the one-round assumption:

1. **27 tests** in `tests/test_cli.py` invoke `resume` without `--dry-run` and assert **exit 2** (AST
   predicate over source segments, not grep). Deleting the refusal makes each **execute a real
   continuation**, and **22 have fixtures never built for that**. R4 recommends a `--no-continue` flag;
   **needs an ADR and a §10 row edit.** **[ledger]**
2. **Seam D is unruled** — needs an orchestrator ruling before dispatch. **[decision]**
3. **"The three composition roots" is an OPEN question**, not a defect to fix: `phase_floor` can return
   `Phase.SCAN` (**exercised**: `phase_floor({},{})` → `SCAN`), and the three named roots cannot serve
   it. W35 recorded options **D-1/D-2/D-3 with costs and invented no semantics**. Whoever rules it must
   fix the row title, the clause, **and `docs/SPEC.md` §10's row in ONE change.** **[decision]**

**The ordering fact to carry (S8-1):** step 8 must sit **after** the step-2 reap, because
`_LIVE_SANDBOX_PREDICATE` spares only `RUNNING` rows, so a reap after a continuation **deletes the
cross-phase checkout the next phase reads**. **[ledger]** R4 also recorded **two hypotheses it expected
to be hazards and measured safe** — recording the negatives is what makes the positives trustworthy.

---

## 5. Open by decision — five new register entries, all `OPEN, recorded only`

* **D76** — SPEC §3.5's "current integration tip" clause is **satisfied by shipped code at the function
  level (executed)** but **unbound**; the caller-level property is **source-read, not exercised**
  (`_verify_impl` memoises `plans` outside the wave loop, mitigated by `wave_members`' PRIMARY KEY).
  **That executed/source-read split is the entry's whole value — do not flatten it.**
* **D77** — `append_blocked_by` takes a `DEGRADED` phase to `BLOCKED` via raw SQL, bypassing
  `ALLOWED_TRANSITIONS`. Confirmed by **executing against a real temp SQLite DB**.
* **D78** — `record_backend_unavailable`'s `tier=` arm has **zero producers**. **Do not "fix" by
  deleting it**: removal kills `_CAVEAT_TIER`, collapses `_caveat`, orphans a narrowing arm, turns three
  persisted fields into constants, and **deletes the regression test for a real contamination bug**.
  **SPEC and code already agree** — §11.8/§12.43(iv)/§13 row 40 mandate *naming the tier*, not the
  parameter, and that is already met. **A reconciler "fixing" this would grow a requirement that never
  existed.**
* **D79** — **SPEC §11.6's LLM cache is ENTIRELY INERT.** No `LlmCacheStore` is constructed anywhere in
  `src/`; the shipped `RunContext` **billed the same call twice and wrote 0 rows** while a control arm
  with a store injected billed once and wrote 1. Complete C1–C7 brief in the promoted
  `llm-cache-inert-research.md`. **Ruled out of round E as a new workstream, not a defect to squeeze in.**
* **D80** — SPEC §10's `fleet resume` row orders a **`stub_reconcile`** step no code performs (**0 calls,
  0 imports over 115 `src/` blobs, at two refs**). **So `fleet resume` has TWO absent steps, not one** —
  "step 8 is the sole remaining absence" is true of §11.5's numbered list and **false of §10's row**.
  **Unowned.**

**ADR-0086 remains a permanent reserved gap. ADR-0080 remains reserved-unwritten. Next free D-number is
D81** — and note **a `^### D<n>` probe returns D75 and is WRONG**: D76–D80 use `##`. **[probe @ 0b3fbae]**

---

## 6. Round-F work, in the order I would take it

1. **A measured `CLAUDE.md` consolidation pass.** It is ~42 KB loaded every session, having absorbed
   eight lessons in one round. **Deliberate and measured — not incidental trimming during other work**,
   and nothing removed without proving it is covered elsewhere.
2. **Rule Seam D and the three-roots question**, then dispatch subtask 10 from R4's decomposition.
3. **D79's C1–C7** — the inert cache. A complete brief already exists.
4. **Discharge W29 §5's audit residuals** (§7 below).
5. **Two disclosed instrument residuals:** the prose-binding module's failure is an **ERROR, not a
   FAIL**; and **`PRAGMA` stays in `_NON_WRITE_VERBS` while `PRAGMA optimize` writes** — its internal
   `ANALYZE` is now caught, but removing `PRAGMA` would redden `main` (12 of 37 captured statements).
6. **`ruff format --check` on `cli.py`** — deferred past subtask 10 by ruling; **66 hunks and growing**
   (58 → 60 → 66).

---

## 7. What round E could NOT verify — read this before trusting §2

**A detached worktree is NOT import isolation.** `.venv` carries an editable install pointing at the
**primary** checkout, so a mutation applied in a worktree **may never be imported**, and **the
zero-change gate cannot catch it** — the file genuinely changed; the *import* resolves elsewhere.
Reproduced **three times independently** (R3, W29, W32) and now in `CLAUDE.md` Rule 12.

**The risk is DIRECTIONAL — do not over-correct.** A mutation that fired **RED is genuine**; greens
sharing **one interpreter invocation** with a red are genuine too. **Only pure-green conclusions are
exposed.** An audit (W29) found **all 8 worktree-mutation lanes were pinned** and **no landed decision
rests on a false green** — but named its own gaps: **1 of 8 batteries re-executed**; two lanes' `cwd`
**unrecoverable and cleared by a structural argument, not a measurement**; and **the largest uninspected
surface is non-mutation runtime probes in worktrees**, which the red-certifies lever does not protect.

---

## 8. Traps still live

1. **`git apply --cached` stages WITHOUT touching the worktree.** After a correct commit,
   `git rev-parse :<path>` == `HEAD:<path>` **passes while the worktree holds the pre-commit blob**, and
   a sibling's next `git add` **silently reverts you**. Reproduced end-to-end. **Verify the worktree
   too**; a **worktree-sourced patch** (`git diff -- <path>`) cannot open the gap at all.
2. **An undisclosed test scoping hid a red on `main` for most of a round.** Run unfiltered; report full
   counts; **state what you excluded**.
3. **A correction is the most dangerous text you will write.** Round E produced **six** corrections
   shipping a fresh false claim inside themselves — and **ten caught by their own authors**. Every lane
   that re-read its own corrective text found one. **Assume you have too, and go and look.**
4. **Prefer a class result to a raw total.** Round E confirmed this **seven** more times, including two
   predicates that **disagreed on the denominator and agreed on the numerator**.
5. **A retraction and its quarry are textually identical to a count.** Round E's sharpest instance: **4
   mentions, exactly 1 genuine false claim** — the others a deliberate retraction quotation, a true
   distinguishing sentence, and a historically-anchored line. **A count-based sweep would have "fixed"
   three correct passages.**
6. **Instruments go blind in ways their own passing cases cannot show.** Round E found: a whitelist
   keyed on **where text sits** rather than what code does; a bound that **rotted into a total
   import-time outage**; a fix claiming "closed by construction" that **kept the same outage by another
   trigger**; and a tokenizer that ate `-- ANALYZE …` whole, so **the only writing statement was the one
   classified harmless.** Guardrail 6's **third** check (synthetic fault into a *clean* file) caught
   three of these.

---

## 9. **`.superpowers/` is UNTRACKED and holds round E's entire process record**

The orchestrator ledger (~every ruling with its cost-if-wrong) and **39 lane reports** live in
`.superpowers/sdd/round-e/`. **`git clean -fdx` destroys all of it.** The **two load-bearing research
briefs were promoted** at `f36c9ad`; **R1's and R4's were not.** **R4's 618-line subtask-10
decomposition is the single most valuable unpromoted artifact — promote it before cleaning scratch, or
round F re-derives it.**

**A promoted document changes status.** Once tracked it is a **reference the next round acts on**, not a
lane's working note. W34 re-measured 14 claims in the promoted cache brief and found **3 false**,
annotating each. Its verdict — **"concentrated, not uniform"**: all three cluster in one narrative
thread, **none changes any task's success criterion**, and everything C1–C4 depends on held at 100%.
**Trust C1–C6 as measured.**
