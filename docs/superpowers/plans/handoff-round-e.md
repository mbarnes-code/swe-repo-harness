# Handoff — round E

**Written 2026-08-21 at the close of round D. Base for round E: `main` at `1d0e39c`.**

This is the entry document for round E. It carries what round D established, what it left open, and
what it learned about carrying things forward.

---

## 0. How to read this document — two rules round D paid for

**Every claim below is marked with how it was verified and when.** Use the tag, not the sentence:

| tag | meaning |
|---|---|
| **[probe @ SHA]** | re-derived from the code or the tree by a named probe at that anchor |
| **[ledger]** | reported by the lane that measured it, recorded in round D's ledger, not independently re-derived here |
| **[decision]** | a ruling or a deliberate deferral, not a measurement |

**Every "still live" or "still open" claim in this document carries the SHA it was measured at, and
that is a requirement, not a courtesy.** See this section's last rule for why.

**Re-measure, do not distrust, and do not inherit.** Round B's open-items audit found roughly four
in five of the items *it* inherited were stale. Round D re-measured the same class and found the
carried subtask status **correct** — independently re-verified rather than assumed. So the
instruction is *re-measure*, not *assume rot*: the point of the tag is to tell you what a
re-measurement would be re-measuring.

**Round C's handoff is the reason this section exists.** It told round D that D71 was free "and
explicitly recorded as such". A lane checked it against the register and **the claim needed
correcting** — the register entry's *body* is a disclaimer of itself, so a heading match is
necessary and not sufficient. Separately, an orchestrator ruling issued on a *different* misreading
of that same entry had to be rescinded after a worker read the primary source and refused to comply.
Both are the same failure: a confident label surviving a hop.

**A routed finding is perishable.** Round D measured a review finding that was **true when filed and
false by the time the implementer acted**, through the same citation rot the finding was about;
acting on it verbatim would have landed a fresh false citation inside the fix for a false-citation
finding. **Every finding carried below has an anchor, and every one must be re-measured at the
moment of action, not at dispatch.**

**A handoff is where the *reporter's* side of that bites.** Three times in round D a lane measured
"still live at HEAD", a sibling's fix landed **between the measurement and the report**, and the
stale reading shipped as current — **this document did it twice in its own first version** (§8 item
6), and two of the three instances were caught only because the orchestrator re-ran the check. The
rule above covers the **implementer**. This is the **reporter's** half, and a handoff is the worst
place for it precisely because **every claim here is read days later, by someone who will not
re-measure unless told to.** Hence the SHA requirement: a claim of the form "X is still broken" is
only as good as the SHA beside it, and a reader who finds none should assume it has rotted.

---

## 1. Read these first, in order

1. **`docs/PROGRESS.md` §40**, including its **"Round close — amendment measured at `1d0e39c`"**
   block. That block supersedes several of §40's own earlier readings and says which. Read §39's
   amendments too — they carry the last whole-tree suite anchor and why it is stale.
2. **`CLAUDE.md`.** A round-close lane amended it with round D's measured lessons in the same
   session as this handoff. Re-read it rather than working from a remembered version.
3. **`docs/superpowers/plans/design-resume-step5.md` §5** — the ten-row task breakdown. Rows 8, 9
   and 10 are what remains. **Row 8's stated mechanism is known not to work as written** (§5 below).
   Several rows carry dated in-file corrections from round D; read the corrections, not only the row.
4. **`docs/DECISIONS.md`: ADR-0085, ADR-0087, ADR-0088, ADR-0089.** ADR-0089 is the one to read
   first for step 5's wiring — it records four rulings, the closed-wave disclosure, and the
   `ResumeIncompleteError` deferral annotated onto ADR-0076's own bullet.
5. **`docs/INTEGRATION_HONESTY.md`: D71 (free), D72, D74, D55, D75.** Read D71's *body and its
   marker*, not its heading.
6. **`docs/superpowers/plans/resume-step5-subtask-8-research.md`** — the source-verified brief for
   subtask 8: five costed decisions, four D74 seams, the `append_synthetic_waves` identity result and
   the quarantine-undo hazard. **Promoted into the tracked tree by lane W9** from `.superpowers/`
   scratch, unchanged but for a provenance banner (§9).
7. **`docs/superpowers/plans/resume-step5-subtask-7-research.md`** — promoted alongside it. **Its
   §§1–3 are history**: subtask 7 has landed, and the I4 transaction boundary and the dry-run D74
   seam were both closed inside it. **Read §4 — that half is the subtask-9 brief**, and it is what
   ADR-0079 must settle.

---

## 2. Verified state at `1d0e39c`

| claim | how verified | tag |
|---|---|---|
| Round D = **26 commits** off `0e945b8`; 19 touch `src/`/`tests/`, 7 are docs-only | `git rev-list --count 0e945b8..1d0e39c`, cross-checked against `git log --oneline \| wc -l`, and again with a `-- src/ tests/` pathspec | **[probe @ 1d0e39c]** |
| Code delta **13 files, +3,920 / −59** | `git diff --shortstat 0e945b8..1d0e39c -- src/ tests/` | **[probe @ 1d0e39c]** |
| **No full suite has run against any round-D commit** | stated by every lane; no run recorded anywhere | **[ledger]** |
| Last whole-tree anchor **`1692 passed` at `1ae3ffc`** is **STALE** | `git diff --shortstat 1ae3ffc..1d0e39c -- src/ tests/` → 13 files, +3,920 / −59 over **19** commits touching those trees — non-empty, so the anchor cannot certify HEAD | **[probe @ 1d0e39c]** |
| `python -m mypy`, **no path arguments** → **115 files, no issues** | `pyproject.toml`'s `packages = ["fleet"]` + `strict` set the scope; run twice | **[ledger]** |
| `pytest tests/test_cli.py`, **no `-k` filter** → **126 passed** | scope disclosed rather than quietly relied on | **[ledger]** |
| **216 passed** on a pristine checkout — `test_reentry_evidence` (31) + `test_reentry_floor` + `test_floor_rule_statements` + `test_instruments_are_armed` + `test_findings_kinds` + `test_state_models` | own `BAZEL_ROOT`, pristine checkout of `1d0e39c` | **[ledger]** |
| `pytest tests/test_instruments_are_armed.py -q` → **3 passed** | run by the orchestrator at a clean tree | **[ledger]** |
| **No red instrument at HEAD** | one lane reported that module failing at HEAD; re-run at a clean tree gave 3 passed, so the report was stale — settled by running it, not by preferring a source | **[ledger]** |
| `docs/DECISIONS.md` high-water = **ADR-0089** | `grep -nE "^## ADR-00[89][0-9]" docs/DECISIONS.md` | **[probe @ 1d0e39c]** |
| **ADR-0086 reserved and unused**; ADR-0079, ADR-0080 still `RESERVED, not yet written` | zero occurrences of `ADR-0086`; the other two carry literal RESERVED headings | **[probe @ 1d0e39c]** |
| **D71 and D78 are free**; D76, D77 allocated at dispatch and returned unused | zero occurrences of `D76`/`D77`/`D78` in `docs/INTEGRATION_HONESTY.md`; D71's body disclaims its own allocation | **[probe @ 1d0e39c]** |
| `checkout_name` has **no production call site** | every `src/` occurrence outside `sandbox/worktree.py`'s `def` is an import, an `__all__` entry, or a docstring mention | **[probe @ 1d0e39c]** |
| `b1de826` is an **empty commit** whose message asserts it corrected two sites | `git show --numstat --format="" b1de826` emits nothing | **[probe @ 1d0e39c]** |

---

## 3. Landed vs open — with the probe to re-verify each by absence

### Resume step 5: **7 of 10 subtasks landed.** Critical path is now `8 → 9 → 10`.

| subtask | state | commits / re-verify by |
|---|---|---|
| 1 demotion transition | landed before round D | — |
| 2 `phase_floor` | landed before round D | — |
| 3 step 2 reap | landed before round D, **with a disclosed limitation** (ADR-0081: the worktree half reaps nothing in a real run — that is D72's territory) | — |
| 4 step 4 Git-as-arbiter | **landed round D** | `fe743e6`, `6bf198f` (ADR-0087), `7d8f916`, `b1de826` |
| 5 `evidence_holds` | **landed round D** | `abd009b` (ADR-0088), `7baaac3`, `7b2d48e`, `7500005` |
| 6 demotion writer | landed before round D | — |
| 7 wire step 5 into `_resume_impl` | **landed round D** | `2f0db34`, `42a4369`, `1ce1901` (ADR-0089), `64512f2`, `1d0e39c` |
| 8 step 6 — recompute `blocked_by`, synthetic wave | **NOT STARTED** | **class result: the class "code in `src/` that removes an entry from `blocked_by`" has ZERO members.** The only writer is `SqliteSchedulerStore.append_blocked_by`, reached from `WaveScheduler.propagate_blocked` and directly from `fleet quarantine`; the column is append-only today despite the SPEC calling it reversible **[probe @ 1d0e39c]** |
| 9 un-refuse the step-5 flags | **NOT STARTED** | `_refuse_unbuilt_resume_flags` still defined in `src/fleet/cli.py` and still called from `_resume_impl` **[probe @ 1d0e39c]** |
| 10 step 8 "continue" | **NOT STARTED** | `ResumeIncompleteError` still defined and still raised on the `fleet resume` path **[probe @ 1d0e39c]** |

**Two obligations subtask 10 inherits, both already written down rather than promised:**
`ADR-0089` **rewrote** `ResumeIncompleteError`'s message rather than deleting the class, and
annotated ADR-0076's "should be deleted, not repurposed" bullet beside itself recording the deferral
to subtask 10 and why. And `_refuse_unbuilt_resume_flags`' docstring is now false for a **second**
reason (step 5 *is* built) — correctly left for subtask 9, whose scope it is. **[decision]**

### Other landed work

* **D72 tasks 1 and 2** (`eaa112f`, ADR-0085) — the fleet has **two** worktree name forms, and
  `checkout_name` implements the attempt-free one. **D72 is NOT closed** (§4).
* **The round-C whole-round review punch list is discharged** across `8dee485`, `b526f47`,
  `ed7e768`, `4f353e3`, `1ae3ffc`, `92fefb7`, `b4567a5`, `f141af6`, `4cde582`. **[ledger]**

---

## 4. Open **by design** — three items left open deliberately, not by neglect

Do not "clean these up" without a decision. Each is correctly recorded and correctly scoped in
`docs/INTEGRATION_HONESTY.md`, and none is ready. **[decision]**

* **D72 — the worktree namespace.** Tasks 1 and 2 landed; **tasks 3–7 remain**, and
  **`checkout_name` has no production call site yet** [probe @ 1d0e39c] — the helper exists and
  nothing calls it. Two consequences round D disclosed rather than patched, because no caller can
  reach them today: `slug(repo_id) == repo_id` is **false for 228 of 1600 legal `RepoId`s** (all one
  class — a trailing hyphen, erased by `slug`'s closing `.strip("-")`), and dropping the attempt
  makes `checkout_name(r, "log4j-2") == sandbox_name(r, "log4j", 2)`. Both become reachable the
  moment task 3a lands, and both bear on task 4's `live_names`. **[ledger]** Five further sites in
  three files were reported and not edited, notably `cli.py`'s KNOWN-LIMITATION docstring, which
  names the **wrong helper** — `context.py` must adopt `checkout_name`, not `sandbox_name`.
* **D74 — two independent computations of one value, nothing enforcing agreement.** Round D found
  four fresh seams of this shape on subtask 8's path (§5) and closed one of them **by construction**
  in subtask 7 rather than by convention, by extracting the membership rule so the preview and the
  write cannot state different rules (ADR-0089).
* **D55** — open, scoped, and its premise fix has reached the scoping lists (`b526f47`).

Also open by decision: **ADR-0086 is a reserved, unwritten gap. Do not re-use the number.**
**ADR-0079 is still unwritten and is still required before subtask 9.**

---

## 5. Decisions awaiting a ruling — subtask 8

Round D's research lane costed these and **explicitly declined to pick**. They are **decisions, not
findings**: a round-E orchestrator must rule on each *at dispatch* and allocate the ADR number
centrally. Options and costs below are the research lane's; verify them against the primary source
before ruling.

### W — how subtask 8 reaches `waves` / `wave_members`

* **W1 — extend `append_synthetic_waves`** to accept already-scheduled nodes. *Cost:* changes a
  function whose only test pins the opposite behaviour; still leaves seams S2/S4 open because the
  caller still needs a `WavePlan` that does not exist at resume time. Cheapest-looking, most likely
  to regenerate the class.
* **W2 — a new narrow `SchedulerStore.append_unblocked_wave`**, SQL-only, no `WavePlan`. *Cost:* +1
  Protocol method × 3 implementations; a second writer of `waves`; the topological layering must be
  re-implemented rather than reusing `layer()`. Closes S2 and S4 by construction.
* **W3 — rebuild a full `WavePlan` at resume time** and go through `record_plan`. *Cost:* the most
  expensive step in `fleet sequence` on every `fleet resume`; `_sequence_impl` itself refuses this
  mid-run; opens three seams at once; renumbers waves if the graph moved. The research lane
  recommends against it, **as an Agent Recommendation, not a directive**.

### R — which reading of §11.5 step 6 governs

**The SPEC contradicts itself here**, between §3.5's "reversal" and §11.5's "recompute", and the two
give **opposite answers** when step 5 demotes a blocker out of `SUCCEEDED` before step 6 runs.

* **R1 — reversal:** remove `r` only when `r` is `SUCCEEDED`. *Cost:* a step-5-demoted blocker
  leaves its dependents blocked forever.
* **R2 — recompute** from current `phases` + `edges`. *Cost:* un-blocks `d` while `r` sits demoted
  at a floor in a closed wave, so `d` migrates against a dependency that has not landed; and this is
  the reading that walks straight into the quarantine defect below unless the blocker predicate is
  widened.
* **R3 — recompute, but treat a demoted blocker as still blocking.** *Cost:* a fourth `blocked_by`
  population with no SPEC sentence behind it; needs an ADR; and "was demoted this run" is a
  `findings`-derived predicate, i.e. a new coupling between step 6 and step 5's audit trail.

**Whichever loses, its SPEC sentence must be corrected in the same change.** CLAUDE.md Guardrail 7:
"the SPEC says X but the code cannot do X" is two edits, not one. An unannotated contradiction is
the exact mechanism that produced this project's six-round `supports_effort` land blocker.

### D — who owns "no closed wave is re-opened" for **step-5 demotions**

* **D-a — subtask 8 owns it** (step 6 also moves demoted repos into the synthetic wave). *Cost:*
  requires placing a demoted repo in a synthetic wave, which **no SPEC sentence authorises**; needs
  an ADR. The only option that leaves `fleet resume` with all waves consistent.
* **D-b — subtask 7 owns it.** *Cost:* subtask 7 has landed; reopening it is not the sizing that was
  planned. Same missing SPEC authority.
* **D-c — nobody owns it; disclose it.** *Cost:* step 8 then cannot open any wave past the lowest
  demoted one — which **may in fact be correct**, since a demoted repo genuinely must re-run before
  its dependents. Cheapest honest position, but design row 8's criterion becomes false as stated for
  the demoted population, so row 8's wording must change with it.

### A — is step 6's un-blocking audited?

Step 5's demotion mints a mandatory `PhaseDemoted` finding *because a demotion discards landed, green
work*. Step 6's un-blocking goes through a plain `ALLOWED_TRANSITIONS` edge with **no finding**, and
it moves a repo across waves — which the projection is explicitly supposed to be able to explain.
*Options:* no finding (the SPEC is silent, cheapest); a `BlockedByCleared` finding, symmetric with
step 5 — **note that `tests/test_findings_kinds.py`'s `_recognition_gap` census will need the new
kind**; or rely on `waves.synthetic = 1` alone. **No SPEC sentence requires any of these.**

### T — the orphaned §3.5 "current integration tip" clause

*"Their Phase 4 runs against the current integration tip — a fresh snapshot ref per §3.3 step 1, not
the tip their original wave saw — and any Phase 3 merge is rebased onto it."* Design row 8's success
criterion has no clause for it, and row 10 does not mention it. *Options:* assign to subtask 8 (but
it has no phase-runner surface); assign to subtask 10; or record it as a known gap with a D-number.
**Currently owned by nobody.**

---

## 6. Load-bearing facts for subtask 8 — re-measure each at the moment of action

Three findings that change what subtask 8 can be briefed to do. All were measured in round D; all
carry the perishability warning in §0.

1. **`append_synthetic_waves` cannot be used as-is.** It filters
   `fresh = [ref for ref in node_refs if ref not in plan.wave_index_by_node]`, and **subtask 8's
   entire population is repos that already have a wave index.** Exercised in `.venv` on the shipped
   `chain_report()` fixture: an already-scheduled repo returns the **input plan by identity**, zero
   synthetic waves appended. The one shipped test pins the *opposite* case. It also has **zero
   production callers** — an AST instrument over `src/` returns 0, with a positive control firing on
   the one test site. **[ledger; re-verified here that every `src/` occurrence is a re-export, an
   `__all__` entry, or the definition — probe @ 1d0e39c]**
2. **Subtask 7 already re-opens closed waves, and subtask 8 cannot honour design row 8's
   invariant.** `WaveState` is **computed, not stored** — CLOSED iff every `wave_members` row's
   status is in `SETTLED_STATUSES` — so re-opening a closed wave is an emergent property of *any*
   status write, and `demote_to_floor` writes `SUCCEEDED → PENDING` on members of closed waves while
   touching nothing in `waves`/`wave_members`. **Subtask 7 breaches row 8's "no closed wave is
   re-opened" first, by a door subtask 8 cannot see.** Disclosed in **ADR-0089** and pinned by a
   test that records what the computed `wave_state` *becomes*, deliberately **not** asserting a
   desired end state — and the implementing lane was explicitly forbidden from writing "subtask 8
   will fix it", because a promise no mechanism enforces is a shape that has bitten this project
   repeatedly. **[ledger]**
3. **A literal "recompute `blocked_by` from RHI ancestors" would silently undo an audited
   `OperatorQuarantine` on every `fleet resume`** — it empties the `blocked_by` of every QUARANTINED
   repo's dependents and re-admits them. `fleet quarantine` writes `SKIPPED` and propagates
   identically, and §3.5 puts contract ids in the same field. The root cause is a **false sentence in
   the code**: `src/fleet/models/state.py`'s `blocked_by` field description claims RHI is *"the ONE
   status 'abandoned' names"*. **This is a class, not a site** — the research lane was read-only and
   did not edit it. **[ledger; sentence re-verified present at `src/fleet/models/state.py` — probe @
   `ab3aaad`]** **A lane was fixing it as this handoff was written, so treat the sentence itself as
   IN-FLIGHT and re-check `git log -S"the ONE status" -- src/fleet/models/state.py` before acting.
   The hazard does not depend on the sentence surviving** — the quarantine population is real
   whatever the docstring says, and that is the part subtask 8 must design against.

Design row 8 also asserts **"Files touched: `cli.py`"**, which is not achievable: nothing in `src/`
reconstructs a `WavePlan` from the DB, and `record_plan` — the whole-plan writer, sole caller
`_sequence_impl` — is the only `waves` writer. Row 8 is not an M. **[ledger]**

---

## 7. Traps that are still live

1. **`git commit` commits the SHARED index. Staging "by explicit path" does not protect you.** One
   round-D commit contains work its author did not write, because a sibling ran `git add` between
   that lane's staging and its commit. Two lanes independently narrowed the hazard to the **pathspec
   form `git commit -- <paths>`**, which commits from the working tree and bypasses the index;
   staging with `git add` / `git apply --cached` and then a bare `git commit` commits *the index*, so
   index == commit by construction. **The load-bearing verification is
   `git rev-parse :<path>` == `git rev-parse HEAD:<path>` after the commit** — not the `git add`.
   The same race produced an **empty commit** (`b1de826`) whose message asserts work it does not
   contain, and swept one lane's uncommitted edits into another's commit in the opposite direction.
   **On shared files, `git add <path>` is not sufficient granularity in either direction.**
2. **Line citations rot, and being *correct when written* is no protection.** One correct citation
   moved **three times in one day** (`:1589 → :1590 → :1597`). A commit whose entire purpose was
   re-anchoring citations by symbol **rotted a citation inside the docstring it was re-anchoring**,
   three lines below its own text diagnosing that exact mechanism. **Cite by symbol; never by line.**
3. **A line-oriented `grep` certifies a class as fixed when it is not.** A wrapped match defeated a
   line-oriented sweep at least five separate times in one round, and in one case the *reviewer's*
   own raw total was itself an instance of the wrapped-match miss it was reviewing for. **Normalise
   whitespace across the whole file and map offsets back to line numbers**, and state the predicate
   and the normaliser beside every count.
4. **Prefer a class result to a raw match total.** Round D produced a **fourth** independent
   confirmation: every *class* result reproduced under re-measurement; **four** raw totals did not.
5. **A fix reliably ships a narrower successor to the claim it corrected.** It happened repeatedly in
   round D, three times *inside text authored expressly to stop it*. The sharpened rule the round
   produced: **re-run the check against the ARTEFACT THE FIX PRODUCED, not against the finding.**
   Twice a lane caught this on itself by doing exactly that.
6. **A validated instrument can be blind to the defect you are hunting.** Round D found a tracked,
   twice-reviewed instrument named for *ordering* that was a **deletion detector**: it read the
   returned floor, which is provably invariant under the swap for every input, because both loop
   exits are bare `break`s. **No number of cases can move a quantity identical under the defect.**
   The remedy was to instrument the **probe**, not the return value. Round D also found a
   **fixture-shape** version of the same class: a two-anchor test whose two anchors coincide cannot
   express the defect at all — eight tests passed under the exact defect they existed to catch.
7. **The instrument-checker itself was RED on `main`** for part of the round, failing *and* leaving
   the class it was meant to check unexamined. Fixed at `f1e9686`; **3 passed** at a clean tree at
   HEAD. Check it early, at a clean tree.
8. **Guardrail 6's third check earns its place.** Two lanes' detectors passed checks 1 and 2 (fires
   on known-bad, silent on the swept file) and **failed check 3** (fire on a synthetic fault injected
   into a clean file) — one blind to a whitespace run split by a stripped quote, one case-sensitive.
   Add the **cosmetic-reflow control** as the fourth, and honour it by *reading the normaliser*.
9. **A review finding is a hypothesis, including the parts you agree with.** A lane correctly refused
   one review item against the schema, then implemented a sibling item's **rationale** without the
   same check; the code was right and the argument was false in three places, and had to be retracted
   across a docstring, an ADR and a commit message. Two further review findings were killed outright
   by primary-source verification in the same round.
10. **A dispatcher's number propagates further when a worker agrees with it than when it disagrees.**
    An orchestrator invented a count, propagated it two hops, and a worker spent effort contesting a
    claim nobody had made. The asymmetry: a worker who **disagrees** goes and measures, which is
    self-correcting; a worker who **agrees** simply propagates.
11. **Route messages by re-reading the agent id from the dispatch record.** Two rulings were sent to
    the wrong lane from a remembered id. The receiving lane reported the misdelivery instead of
    acting on it, which is the only reason nothing wrong landed.
12. **Cite the ref, not `HEAD`.** `HEAD` is `main`, where a sibling lane has not landed. Uncommitted
    sibling work is **in-flight, not history** — report it as such and never count it as landed.

---

## 8. Suggested order for round E

1. **Run the full suite at a quiet tree, with every lane held, and stamp the SHA.** Capture the
   anchor with `git rev-parse HEAD` **before** the run and confirm it unchanged after. **This is the
   first task and it blocks any claim about round D's 26 commits.** The last whole-tree anchor —
   `1692 passed / 0 failed / 0 skipped` at `1ae3ffc` — **is stale**: `git diff --shortstat
   1ae3ffc..1d0e39c -- src/ tests/` reports **13 files, +3,920 / −59** over **19** commits touching
   those trees, so it cannot certify HEAD. Green = `xfail: 0` **and** a clean `bazel disk` line, and
   the run takes ~15 min. **Never run two pytest sessions concurrently** — `pytest_sessionfinish`
   reaps sibling output bases.
2. **Rule on §5's five decisions and allocate the ADR number centrally**, then dispatch **subtask 8**
   with the §6 facts re-measured at dispatch. Whichever reading of step 6 loses, its SPEC sentence is
   corrected in the same change.
3. **Subtask 9**, gated on 8. **`ADR-0079` must be written before it.** Its scope includes
   `_refuse_unbuilt_resume_flags`' docstring, now false for a second reason.
4. **Subtask 10**, gated on 9. It must discharge ADR-0076's annotated deferral of the
   `ResumeIncompleteError` deletion.
5. **Re-audit §38's thirteen carried open items** rather than inheriting them (§40 open item 4,
   untouched). Round B's audit of a comparable carried set found roughly four in five stale.
6. **One live item, and two that were fixed underneath this handoff — read the correction, not the
   original.** *(Corrected 2026-08-21, lane W9, at `0ea771e`: two of the three items this section
   originally listed as "still live at HEAD" had already been fixed when I measured them. My basis
   was "no commit after `42a4369` addresses them", derived from a `git log` taken earlier in the
   session; `0ea771e` landed after that reading and before my commit. Both are struck below rather
   than deleted, because a round-E lane sent to fix an already-fixed site would **edit correct
   text** — the mirror-image error `CLAUDE.md` names by that name.)*
   * **LIVE, and the important one.** `src/fleet/models/state.py`'s `blocked_by` field description
     claims RHI is *"the ONE status 'abandoned' names"*. **False** — `fleet quarantine` writes
     `SKIPPED` and propagates identically, and §3.5 puts contract ids in the same field. This is
     **not cosmetic**: it is the false premise behind §6 item 3, the quarantine-undo hazard on
     subtask 8's path, and it is a **class, not a site**. **[probe @ `ab3aaad`: sentence present]**
     **A lane was fixing it as this handoff was written — treat it as IN-FLIGHT, not open: check
     `git log -S"the ONE status" -- src/fleet/models/state.py` before dispatching anyone at it.**
     What survives whatever that lane lands is the *reason* it matters, which is §6 item 3.
   * ~~`src/fleet/sandbox/container.py`'s `claims()` docstring cites `ContainerSandbox.run()`'s
     `finally` at `(:330-331)`.~~ **FIXED at `0ea771e`.** The parenthetical is gone, and that lane
     swept the whole docstring afterwards and removed a **second** line reference the finding never
     named. **[probe @ `ab3aaad`: zero line references of either form remain in that file.]** Do not
     re-open it.
   * ~~`docs/DECISIONS.md`'s ADR-0082 marker states *"before this change: 16 citations. After it,
     15"*, which re-measures to 16.~~ **FIXED at `0ea771e`, and the fix is better than the finding.**
     That lane re-measured **with the scope axis stated** and found the pair was impossible rather
     than merely wrong: *"16 before, 15 after"* had taken **one number from each variant**
     (blockquote-inclusive 16 → 16, blockquote-exclusive 15 → 14). The marker now states the
     predicate, gives the class result, and prints **both** variant totals rather than presenting
     either as *the* number. **[probe @ `ab3aaad`: `grep -n 'After it' docs/DECISIONS.md` returns
     nothing.]** Do not re-open it.
7. **A pre-existing `mypy` note, carried and not fixed:** 9 errors measured in `tests/`, identical in
   code and message at two round-D anchors, in a name-shadowing artifact; `mypy` does not gate
   `tests/` here (`pyproject.toml` sets `packages = ["fleet"]`). The fix is renaming the `JoinedStr`
   branch's `out`. **[ledger]**

---

## 9. What this handoff cannot catch — and one path warning

**The round-D lane reports and orchestrator ledger are in an UNTRACKED directory.**
`.superpowers/` is untracked (`git status --short` → `?? .superpowers/`), so everything in
`.superpowers/sdd/round-d/` — the orchestrator ledger, seven lane reports and eight reviews —
**vanishes when that scratch directory is cleaned, and nothing in the tracked tree reconstructs it.**

**The two load-bearing files have been promoted** into `docs/superpowers/plans/` by lane W9,
following the precedent of `1050e0a`, `6b5d287` and `0e945b8`. Both destinations were checked absent
first — one `cp` in this project silently clobbered an unrelated committed report, caught only by
`git status` before staging — and both bodies were verified **byte-identical** to their scratch
originals after a two-line provenance banner:

* `resume-step5-subtask-8-research.md` (was `r3-research.md`) — **subtask 8's entire brief.**
* `resume-step5-subtask-7-research.md` (was `r2-research.md`) — promoted **because its §4 is not
  history.** Subtask 7 has landed, so §§1–3 record a question already answered; but §4 is the
  **subtask-9** brief — the `--from-phase` refusal split verified against
  `_refuse_unbuilt_resume_flags`, a defect subtask 9 inherits in the same function, and ambiguity 4's
  three options costed and deliberately unpicked. That is exactly what **ADR-0079** has to settle,
  and ADR-0079 is still unwritten. A brief whose live half gates an unwritten ADR is not history.

**The rest of `.superpowers/sdd/round-d/` was NOT promoted, and that is a judgement, not an
oversight**: the ledger and the lane reports are round D's *process* record, and their durable
conclusions already live in `docs/PROGRESS.md` §40's round-close block, in ADRs 0085/0087/0088/0089,
and in this handoff. If round E needs to audit round D's **method** rather than consume its
conclusions, that material must be promoted **before** the scratch directory is deleted.

**This document cannot catch a consistent rewrite of every copy of a claim into one false sentence.**
Its tags say how each claim was verified; they do not make the claims self-checking. The **[ledger]**
rows in §2 are lane-reported and were not independently re-derived here — that is disclosed, not
hidden, and it is exactly the set a round-E lane should re-measure first.

**Nothing in round D is suite-verified.** Every test number in §2 is a scoped selection. Treat the
whole of §2's test column as unproven at the whole-tree level until task 1 in §8 has run.
