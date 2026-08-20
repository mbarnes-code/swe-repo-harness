> Promoted from `.superpowers/sdd/design-resume-step5/review-6.md` (round C scratch, lane Review-6), snapshot taken while `main` was at `1050e0a`. Examines MECH, REC, DOCSTALE, QUANT (eight commits, reviewed ref `7670fc2`). Verdict: 1 Critical, 3 Important, 2 Minor.

# Review 6 — MECH · REC · DOCSTALE · QUANT (eight commits)

**Reviewed ref:** `7670fc2` (the tip of the eight commits under review). All measurements below were
taken in a **detached worktree pinned at `7670fc2`**, outside the shared scratchpad, with a private
`BAZEL_ROOT` (`conftest._bazel_root()` digests the checkout path, and the worktree path has no space,
so it resolved to the worktree's own `tools/bazel-test-root`). Both audit worktrees were removed at
the end; `worktrees/wt-WT1-example` was never touched.

**`git status --short` at review start** (primary checkout):

```
 M src/fleet/cli.py
?? .superpowers/
```

**`main` moved during the review** — `60d400b` and `e0404b0` (the live APPLY lane) landed while this
audit was running, and `a58fc1c` at the end. Nothing below is measured against those; the pinned
worktree is why. `src/fleet/cli.py` is dirty in the primary checkout throughout, so every `cli.py`
citation here is from the pinned worktree's committed content, never the working copy.

---

## Verdicts

| Lane | Commits | Verdict |
|---|---|---|
| **MECH** | `b995458`, `6e0a5fa` | **Accept with one Critical.** `6e0a5fa` is clean and fully verified. `b995458`'s detector is real, its 27/19/21 reproduce exactly, and its Rule 12 discrimination is properly done — **but the gate it is built around can be silently defeated by a whitespace-level reformat of an existing site.** |
| **REC** | `37fa292` | **Clean.** Every citation and both re-measurements reproduce. |
| **DOCSTALE** | `ae4b923`, `5be5064` | **Accept with one Important.** All four promoted files are byte-identical past their headers; the annotate-don't-rewrite ruling is right. One promoted file carries a measurement the immediately preceding commit corrected, with no in-file marker. |
| **QUANT** | `71dc184`, `7670fc2` | **Clean.** The replacement quantifier is broader than what it replaced, not narrower, and every clause of it was exercised against the shipped function. |

**Count by severity: 1 Critical, 3 Important, 2 Minor.**

---

## Priority 2 — measured vs. claimed (MECH's 27 / 19 / 21)

Reproduced by loading `tests/test_findings_kinds.py` as a module and calling `_emitters()` /
`_listing()` directly — a third method, independent of both the pytest assertions and the manual
sweep MECH describes.

| | claimed | measured | |
|---|---|---|---|
| emitted kinds, current tree | 27 | **27** | ✅ |
| emitted kinds, pre-fix tree (`8ea1881^` = `704e52f`) | 27 | **27** | ✅ |
| missing from `schema.sql`, pre-fix | 19 | **19** | ✅ |
| missing from `docs/SPEC.md`, pre-fix | 21 | **21** | ✅ |
| drift on the current tree | 0 / 0 | **0 / 0** | ✅ |

The 19 and the 21 name the same set bar two: `BackendUnavailable` and `CapabilityDrift` were in
`schema.sql` and not in the SPEC — exactly the pair the module's docstring cites as the reason
property 1 exists. `tests/test_findings_kinds.py` + `tests/test_llm_cache.py` scoped: **24 passed**.

Contrast with two failed reproductions earlier this round: **all four of MECH's numbers reproduce
exactly.** The lane's arithmetic is trustworthy. Its *instrument* is the problem.

---

## Priority 1 — can the detector be silently defeated? **Yes.**

**Answer: yes, by an accidentally-reachable, semantics-preserving reformat.** The module's
load-bearing claim (`tests/test_findings_kinds.py:19-23`, restated at `:397-404`) is that *"an
`INSERT INTO findings` whose `kind` this module cannot resolve to a literal is a hard failure naming
the site, unless the site is declared in `_INDIRECT_SITES`."* That claim holds **only for sites the
module already recognises**, and recognition is a silent blacklist-shaped pre-filter, not a
whitelist.

### C-1 (Critical) — `tests/test_findings_kinds.py:288` and `:129-137`: recognition is a silent pre-filter, so the loud gate is never reached

```python
sql = src.sql_of(node.args[0])
if sql is None or "INSERT INTO findings" not in sql:
    continue                     # ← silent. Never reaches the `unresolved` path.
```

`_Src.sql_of` (`:129-137`) resolves only a string literal or a **module-level, single-valued**
constant, and the substring test runs against the **raw** literal — whitespace is normalised only
later, inside `_kind_slot` (`:166`).

**Measured.** Seven synthetic emitters injected one at a time into `src/fleet/orchestrator/findings.py`
in the clean pinned worktree, each byte-diff-confirmed to have changed the file before its result was
read:

| injected form | detector result |
|---|---|
| new literal kind (positive control) | **loud** — 28 emitted, `SynthNewKindV6` reported missing from both listings |
| `kind` from an unresolvable call, SQL inline | **loud** — `unresolved kind at INSERT INTO findings …` |
| **SQL bound to a local variable** first | **SILENT** — 27, `unresolved == ()` |
| **`INSERT INTO\n    findings`** (triple-quoted, wrapped) | **SILENT** — 27, `unresolved == ()`, *and its `kind` was unresolvable* |
| **`INSERT OR REPLACE INTO findings`** | **SILENT** — 27, `unresolved == ()` |
| `kind` reassigned under an `if` | **SILENT under-report** — see I-1 |
| SQL passed through a `_run_sql(conn, sql, params)` helper | **SILENT** — 27, `unresolved == ()` |

**Then the same thing on a real, existing site**, which is what makes this Critical rather than
adversarial. `src/fleet/cli.py:9747-9751` (pinned worktree) writes the `OperatorQuarantine` finding as
five implicitly-concatenated string literals. Reformatting it into a triple-quoted block — a change
that alters no behaviour, no SQL, no `kind`, and would pass any reviewer:

```
git diff --numstat  →  4  3  src/fleet/cli.py
detector            →  EMITTED_COUNT 26   UNRESOLVED ()
pytest tests/test_findings_kinds.py  →  4 passed
```

The site **disappeared**. Emitted dropped 27 → 26, nothing was reported unresolved, and all four tests
stayed green. Both directions were blind: the reverse test missed it too, because `OperatorQuarantine`
sits in the `Shipped:`/DECLARED half rather than under the EMITTED heading.

**Failure scenario.** `cli.py` holds 13 of the 16 findings-INSERT sites and is under active edit. A
future author reformats one of them (or writes the next one as `INSERT OR REPLACE INTO findings`, or
hoists its SQL to a local so the `execute` call fits the line budget). That site leaves the detector's
view permanently and without a sound. Every kind added there afterwards is invisible, all four tests
stay green, and the listings resume the exact drift that produced the 19-kind gap — while a reviewer
now reads "this is mechanical" and stops checking by hand. Per Rule 12's stop rule this is
accidentally reachable, not adversarial-only, so it is a defect rather than a stated boundary; and per
the same rule, a gate whose recognition step is a blacklist while its docstring describes a whitelist
is the "convention wearing a mechanism's clothes" case.

**Remedy shape** (Rule 12: invert the enumeration into a whitelist). Add a second, independent,
text-level instrument and assert the two agree: whitespace-normalise each `src/fleet/**/*.py` and
count `insert\s+(or\s+\w+\s+)?into\s+["'\s]*findings`, then assert the set of files it finds equals the
set the AST walk recognised. Measured today for calibration: **16 AST-recognised sites** (cli.py 13,
`orchestrator/findings.py` 2 — one module constant used twice — `state/repository.py` 1) against **16
textual occurrences** (cli.py 13, findings.py 1, `orchestrator/stubs.py:147` 1 *prose in a docstring*,
repository.py 1). The mapping is deliberately not 1:1, so compare per-file presence (with the
stubs.py prose hit explicitly excluded and named), not raw totals.

### I-1 (Important) — `tests/test_findings_kinds.py:196-203`: the resolver is not conservative, and the docstring says it is

`_binding` returns the **first** assignment `ast.walk` reaches in the enclosing function, not a union
over all of them. Measured, on an injected emitter:

```python
kind = "SynthFirstV4"
if flag:
    kind = "SynthSecondV4"
```

→ 28 emitted, only `SynthFirstV4` reported. **`SynthSecondV4` is silently dropped and both listing
tests pass.** The module docstring `:25-29` states the residual as *"the resolver is conservative —
every form it does not understand returns 'unresolved' and trips property 3 — so the realistic
residual is narrow"*, and `task-mech-report.md:126-130` repeats it. A conditionally reassigned local is
not an exotic form; it is the ordinary alternative to the `if`-expression the resolver *does* handle
correctly (`ast.IfExp`, `:259-262`). So the stated boundary understates the real one — which under
Rule 12 is the worse half of the honest-disclosure-vs-fake-mechanism trade, because the disclosure is
what a future reader trusts instead of re-checking. Minimal honest fix: have `_binding` collect every
assignment and either union them or return unresolved when there is more than one, and correct the
docstring sentence in the same change (Guardrail 7).

### I-2 (Important, measurement) — `tests/test_findings_kinds.py:29`: "cli.py's sixteen call sites"

Measured with the module's own `_Src`: **cli.py has 13** recognised findings-INSERT sites. **16 is the
whole-`src/` total** (cli.py 13 + `orchestrator/findings.py` 2 + `state/repository.py` 1). The number is
correct for `src/` and wrong for the file it is attached to. Repeated at
`.superpowers/sdd/design-resume-step5/task-mech-report.md:27` and `:130`. Guardrail 6 — an unmeasured
number in a committed artifact. Failure scenario: whoever scopes the `FindingKind`-enum change the
docstring recommends budgets against a file-scoped 16, and misses `findings.py` and `repository.py`
entirely — the two modules whose sites the enum would also have to cover.

### M-1 (Minor) — `tests/test_findings_kinds.py:319-340`: the worker channel is also a narrow blacklist

`_worker_findings` collects only `findings.append(<literal>)` where the receiver is a **bare `Name`
spelled `findings`**. `self.findings.append(...)`, `out.findings.append(...)`, `findings.extend([...])`
and `findings += [...]` are all silent. This is the channel that the single `_INDIRECT_SITES` exemption
(`:65-68`) depends on, so a silent miss here is a silent miss inside the one site the module
deliberately does not gate. Lower severity than C-1 because the exemption is narrow and its scope is
documented (`:320-325`), but it is the same shape.

### Confirmed working, for the record

`6e0a5fa` is **clean and fully verified**, independently of the lane's own report:

* **Rule 12 discrimination.** Deleting the two annotation lines from `src/fleet/state/schema.sql:504-505`
  (diff-confirmed): **old (semantic) tests 19 passed · new marker test failed** at
  `tests/test_llm_cache.py:688`. Old-passes / new-fails on the same input, which is the shape Rule 12
  requires and which the two mutations MECH cites also produce.
* **Reflow tolerance holds.** Re-wrapping the same words across three lines with different indentation:
  **1 passed.** So it fires on a change of words, not of layout — as claimed.
* **Reword sensitivity holds.** `absence needs no migration` → `absence requires no migration` in one
  copy only: **failed.** The "same words in both copies" half is genuinely bound.

`b995458`'s own Rule 12 discrimination is likewise sound and was not merely asserted: on the reverted
pre-fix listings, `tests/test_schema_sql.py` (the only pre-existing binding) is **16 passed** while
`tests/test_findings_kinds.py` is **3 failed** — the old suite is demonstrably blind to a 19-kind gap.
C-1 is not a challenge to that; it is a hole in the *third* property, the completeness gate.

---

## Priority 3 — the narrower-successor check (what each fix *wrote*)

Every lane's replacement text was re-exercised against the shipped behaviour it describes, not merely
compared against the text it deleted.

### QUANT `71dc184` — `src/fleet/models/enums.py:64-70` — **not narrower; strictly broader. Clean.**

The new sentence was run against `phase_floor` (`src/fleet/orchestrator/reentry.py:66-104`), clause by
clause, with `rows` all `SUCCEEDED` except `VERIFY` `PENDING`:

| clause | scenario | measured |
|---|---|---|
| "the phase ABOVE the HIGHEST holder below the frontier" | evidence `{SCAN:T, TRANSFORM:T, BUILD:F, VERIFY:T}` | `phase_floor` → **BUILD (3)**; one above the *earliest* holder would be TRANSFORM (2). The old wording named a rung the function never returns. |
| "SCAN if there is no such phase" | no phase holds | → **SCAN (1)** ✅ |
| "whenever **two or more** phases hold … never reaches the earliest" | only SCAN holds | → **TRANSFORM (2)**; earliest and highest coincide, no divergence — the scoping the commit message says it added after catching its own first-draft overclaim is **necessary and correct** ✅ |
| "(or which is a DEGRADED/SKIPPED hard stop)" — a clause the old comment did **not** have | BUILD `DEGRADED`, no evidence | → **VERIFY (4)** ✅ ; BUILD `SKIPPED` → **VERIFY (4)** ✅ |

This is the round's counter-example to the narrower-successor pattern: the replacement adds a clause
(hard stops) the deleted text lacked, and every clause of it is exercised. The lane's decision to add
no test is also right — replacing the holder `break` with `continue` turns
`test_backward_search_stops_as_soon_as_evidence_holds` red, so a new case would earn nothing under
Rule 12. Re-running QUANT's own detector against its own output: `grep -n earliest
src/fleet/models/enums.py` returns **one** hit, inside the new sentence, where it is used correctly
("NOT the EARLIEST holder"). The file is clean. Remaining sites in `docs/SPEC.md`, `docs/DECISIONS.md`
and `src/fleet/cli.py` are the already-adjudicated live-lane set and are **not re-raised**.

### QUANT `7670fc2` — `docs/superpowers/plans/design-resume-step5.md:255-267` — **clean.**

Every factual claim in the replacement verified:
`test_a_skipped_phase_keeps_its_status_but_still_loses_its_checkpoint` exists at
`tests/test_repository.py:1475` ✅ · `RepoStatus.SKIPPED in TERMINAL_STATUSES` → `True` and
`ALLOWED_TRANSITIONS[SKIPPED]` → `frozenset()` ✅ · `ALLOWED_TRANSITIONS[DEGRADED]` retains a live edge
to `RUNNING` ✅ · `demote_to_floor`'s carve-out at `src/fleet/state/repository.py:1452` excludes
`DEGRADED` only ✅. The lane's decision **not** to edit the four block-quoted occurrences of the wrong
quantifier at `:180`, `:184-189`, `:192`, `:440` is correct and is Guardrail 7's mirror-image rule
applied properly: they are accurate quotations of wrong text, reported rather than edited.

### REC `37fa292` — **clean.** Every citation and both re-measurements reproduce.

* `grep -rni thinking src/ tests/` → **exactly 1** hit, and it **is** `tests/test_llm_cache.py:4`, the
  corrected line. REC's re-measurement is right and the report it corrects was wrong. ✅
* `src/fleet/cli.py:9710-9716` — `movable` is every non-terminal phase row, with `transition(state,
  SKIPPED)` run over each as the gate ✅ · `:9754-9759` — an `executemany` writing `SKIPPED` to **all**
  of them ✅ · `:9763-9769` — the phase-1 `INSERT` is guarded by `if movable: … return`, so it is
  reachable only when there are no phase rows: **a fallback, exactly as the correction says** ✅.
  The original "written only to phase 1" was indeed false.
* The correction's survival argument checks out too, and is not merely asserted:
  `ALLOWED_TRANSITIONS[RUNNING]` does **not** contain `SKIPPED`, so the `:9715-9716` gate raises before
  any write — a phase reaching `SKIPPED` was never `RUNNING` and never held a checkpoint. §4's
  conclusion genuinely survives its corrected premise (confirming the two lanes' independent finding;
  not re-raised as new).
* `src/fleet/models/enums.py:33-34` (`PENDING → {RUNNING, BLOCKED, SKIPPED}`) and `:41`
  (`BLOCKED → {PENDING, SKIPPED}`) ✅ · `schema.sql:504` ✅ · `SPEC.md:4297` ✅.
* Re-running REC's own sweep as a whole-file whitespace-normalised scan over every tracked `docs/`
  file: both surviving PROGRESS.md carriers (`:5735` in open item 14, `:5840` in housekeeping item 9)
  are annotated/struck, and D66's `is still on main, unamended` sentence carries REC's blockquote
  directly beneath it. No uncorrected live claim of this class remains in `docs/` **except** the one
  in I-3 below.

---

## Priority 5 — DOCSTALE's promotion and its ledger ruling

### `5be5064` — the four promotions are faithful. ✅

`diff` of each promoted file against its scratch original, skipping the two-line promotion header:

| scratch original | promoted to | result |
|---|---|---|
| `research-1.md` | `resume-step5-subtasks-4-6-research.md` | **byte-identical** |
| `research-2.md` | `rate-limiting-scope-research.md` | **byte-identical** |
| `task-6-report.md` | `design-resume-step5-task6-demotion-writer-report.md` | **byte-identical** |
| `task-item17-report.md` | `task-item17-report.md` | **byte-identical** |

The rename is justified and was checked before committing: `docs/superpowers/plans/task-6-report.md`
already exists as an unrelated earlier-round file, and the original is untouched. The commit's
disclosure that it did not repoint the `DECISIONS.md` / `INTEGRATION_HONESTY.md` citations it does not
own is **accurate as committed at `5be5064`** — I verified the header text at that ref rather than at
`HEAD`, because the live APPLY lane rewrote that same header in `e0404b0` to say the citations were
repointed. Had this been read at `HEAD` it would have looked like a self-contradicting claim; it is
not one. Both repoints have since landed and are out of scope.

### `ae4b923` — the annotate-don't-rewrite ruling is **right**, and the arithmetic is **right**.

Rewriting `ledger-sdd-backlog-b.md:1996` would have falsified what round B actually recorded at
`5feb1e7`; annotating preserves it as evidence and points forward. That is Guardrail 7 applied
correctly, and it matches how the same file's sibling entries are handled. The counts table
re-verified: VALID `1,2,3,4,5,7,8,9,10,11,13,15,16,19` = **14** ✅ · STALE `17,18` = **2** ✅ ·
NOT-A-TASK 3 · DUPLICATE 1 · total **20** ✅, and the dependent prose ("Fourteen of twenty") was moved
in the same edit. The added distinction — item 18 went stale *outside* git, item 17 *inside* it — is a
real and useful one.

### I-3 (Important) — `docs/superpowers/plans/task-item17-report.md:110` now asserts a measurement the previous commit corrected, in the tracked tree, with no in-file marker

The promoted file reads: *"`grep -ni thinking src/ tests/` now returns **zero** hits."* `37fa292` —
which landed **nine minutes earlier**, and which DOCSTALE's own header cites as the reason for
promoting this very file — established that it returns **one** (re-measured above: one, and it is the
corrected line itself). The promotion header names the dangling-citation problem and says nothing
about the false number.

At the reviewed ref this leaves the tracked tree asserting **zero** with no inbound pointer from the
correction at all: D66's blockquote cites the artifact by its *old scratch path*
(`.superpowers/sdd/design-resume-step5/task-item17-report.md`, described as "untracked"), split across
a line break so a line-oriented `grep` misses it — which is how I nearly missed it too, and is the
third instance of that miss class this round.

"Promote evidence unchanged" is the right instinct and the substance should stay as measured. But
DOCSTALE wrote a header anyway, so the no-touch constraint is not what stopped this: one clause in the
header it was already writing — *"§ line 110's 'zero' was corrected to one by D66 (`37fa292`); the
report's conclusion is unaffected"* — costs nothing and is exactly Guardrail 7's "documents are inputs
to future edits."

**Failure scenario.** An author sweeping the `thinking` retraction opens the tracked report, reads
"zero", finds one hit, and either deletes the negative assertion in `tests/test_llm_cache.py:4` to make
reality match the record — destroying the very correction the round landed and taking
`test_the_effort_column_carries_its_adr_0075_annotation_in_both_copies`'s sibling docstring with it —
or reopens the item and burns a round re-deriving what `37fa292` already settled. This is the round's
recurring shape: a stale listing restores the defect on the next reconciliation.

### M-2 (Minor) — `docs/superpowers/plans/open-items-audit-round-b.md` mixes anchor refs

The document states its anchor up front (`:30` "Working tree is clean, so file contents read below are
`main`'s"; `:73` "Assessed … against `main` = `25af323`"), and row 17 now describes a state that only
exists at a later ref. The row discloses this inline ("landed after this row was written",
"Re-verified this session via `git show main:…`"), which is why this is Minor rather than Important,
but a reader reconciling the table against `25af323` will find row 17 alone does not reproduce. A
one-line note at the table head naming the two refs would close it.

---

## Clean-lane statements

* **REC (`37fa292`) is clean.** No findings. Both corrections are right, both are scoped correctly
  (the entry-edges premise and §4's conclusion were left alone because they were already true), and
  the "re-measured, two agreeing methods" claims reproduce under a third.
* **QUANT (`71dc184`, `7670fc2`) is clean.** No findings. The lane caught its own first-draft
  overclaim before committing and the scoping it added is provably necessary — this is Guardrail 6's
  "re-run the original detector against the fix" working as intended.
* **`6e0a5fa` (MECH's second commit) is clean.** No findings; Rule 12 satisfied under an independently
  reproduced discriminating mutation.

---

## Summary

| ID | Sev | Site | One line |
|---|---|---|---|
| **C-1** | Critical | `tests/test_findings_kinds.py:288`, `:129-137` | Reformatting a real existing site (`cli.py:9747`) into a triple-quoted block drops it from the detector — 27 → 26 emitted, `unresolved == ()`, **4 passed** — so the "hard failure naming the site" gate is reachable only for sites an unnormalised substring pre-filter already recognised. |
| **I-1** | Important | `tests/test_findings_kinds.py:196-203` | `_binding` returns the first assignment, not a union: a conditionally reassigned `kind` silently reports one of its two literals, contradicting the docstring's "the resolver is conservative" at `:25-29`. |
| **I-2** | Important | `tests/test_findings_kinds.py:29` | "cli.py's sixteen call sites" — measured, cli.py has **13**; 16 is the whole-`src/` total (13 + 2 + 1). |
| **I-3** | Important | `docs/superpowers/plans/task-item17-report.md:110` | Promoted into the tracked tree asserting `grep -ni thinking` returns **zero** nine minutes after `37fa292` measured **one**, with no in-file marker and no inbound pointer from D66 at this ref. |
| **M-1** | Minor | `tests/test_findings_kinds.py:319-340` | The worker channel backing the one `_INDIRECT_SITES` exemption sees only `findings.append(<literal>)` on a bare `Name`; `self.findings.append`, `.extend`, `+=` are silent. |
| **M-2** | Minor | `docs/superpowers/plans/open-items-audit-round-b.md:30`, `:73` | Row 17 now describes a post-`25af323` state in a table that declares `25af323` as its anchor; disclosed inline, but the table head does not say so. |

**Not re-raised (confirmed still present, all already adjudicated):** the wrong quantifier at several
`docs/SPEC.md` / `docs/DECISIONS.md` / `src/fleet/cli.py` sites and the scratch-path citations (live
APPLY lane — `60d400b`/`e0404b0` landed mid-review and address these); `cli.py`'s rejected algorithm
and "All twelve workers" (measured 11; blocked on the lane owning that file); ADR-0082 §4's conclusion
surviving its corrected premise (independently confirmed here — see the REC section).
