> Promoted from `.superpowers/sdd/design-resume-step5/review-7.md` (round C scratch, lane CR-7), snapshot taken while `main` was at `1050e0a`. Examines lanes APPLY (`60d400b` `e0404b0` `a58fc1c` `d0de310`), FIVESITE (`f466287`), DETECTOR (`363ecc1`). Verdict: 1 Critical, 5 Important, 2 Minor.

# Review 7 — lanes APPLY, FIVESITE, DETECTOR

Read-only. Nothing in the repository was modified. `git status --short` at entry and exit:
`M src/fleet/cli.py`, `?? .superpowers/` — identical. `worktrees/wt-WT1-example` untouched.
All defeat/mutation work ran in a **detached worktree at `363ecc1` outside the shared scratchpad**
(`/home/redmage/rev7-wt/probe`), with a private `BAZEL_ROOT` (`/home/redmage/rev7-wt/bzl`), every
`cd` guarded with `|| exit 1`, `.venv/bin/python` throughout, no `FLEET_*` exported, scoped pytest
only, one session at a time. `src/fleet/cli.py` was never modified in the primary checkout. The
probe worktree was verified clean (`git status --short` empty) and removed.

Review anchor: `main` at `bf7206d` when I started. A sibling lane committed `f84edcb` during the
review; all `git show main:` reads below were taken before that and re-checked after — none of the
cited lines moved.

## Verdicts

| lane | commits | verdict |
|---|---|---|
| APPLY | `60d400b` `e0404b0` `a58fc1c` `d0de310` | **DONE_WITH_FINDINGS** — 2 Important |
| FIVESITE | `f466287` | **DONE_WITH_FINDINGS** — 1 Critical, 1 Important |
| DETECTOR | `363ecc1` | **ACCEPT** — closure verified, counts exact; 2 Important refinements to its disclosure and its false-positive surface |

Severity count: **1 Critical, 5 Important, 2 Minor.**

---

## 1. DETECTOR (`363ecc1`) — the closure holds; I could not defeat it with a form it claims to catch

### 1.1 Counts: measured, exactly as claimed

Measured in the probe worktree with the module's own `_Src`:

```
AST execute-sites by executing file: cli.py 13, findings.py 2, repository.py 1   (total 16)
AST distinct literal spans:                                                       15
TEXT sites:                          cli.py 13, findings.py 1, repository.py 1   (total 15)
KINDS 27, UNRESOLVED (), 4 passed
```

`cli.py` = **13**. Whole `src/fleet` = **16** (13/2/1). Text = **15**. The one-to-two difference is
`orchestrator/findings.py:126` `_INSERT_FINDING`, one constant executed at `:301` and `:459`.
**Claimed 16 / 15 / 13 · measured 16 / 15 / 13 — no discrepancy.** I-2's docstring rewrite naming
the scope of each number is correct.

Confirmed (Minor, low priority, git-ignored scratch): `.superpowers/sdd/design-resume-step5/task-mech-report.md:130`
reads "`FindingKind` enum at `cli.py`'s sixteen call sites" — wrong file attribution, `cli.py` is 13.
`:27` ("a change to sixteen") is the whole-`src/` figure and is fine in context. Not DETECTOR's file.

### 1.2 The closure — verified in both directions

**C-1 form now stays resolved.** I reproduced review-6's defeat in the worktree: `cli.py:9747`'s
implicitly-concatenated INSERT rewritten as a triple-quoted block splitting `INSERT INTO` from
`findings` across a newline (`git diff --numstat` → `5 5`, diff-confirmed). New detector: **27 kinds,
`unresolved ()`, 4 passed** — the site never leaves the walk's view, and the benign reformat does
*not* fire. That is the right behaviour, and it is the half the report was right to state plainly.

**Rule 12 discriminating shape, on my own mutation, both detectors in the same tree on the same
input:** same reformat with the kind changed to the unlisted `SynthQuarantineV1` —

* old detector (`d0de310`, staged into the worktree as `tests/test_findings_kinds_OLD.py`): **4 passed**;
* new detector: **1 failed, 3 passed**, naming `'SynthQuarantineV1'`.

Old passes, new fails, same diff-confirmed input. The claim is earned. (Note for the record: a
triple-quoted reformat that keeps `INSERT INTO findings` contiguous does *not* defeat the old
detector — I hit that first and it failed both. Only the newline-inside-the-phrase form
discriminates. The report's row 1/row 2 are right; the form matters.)

**Reverse branch (calibration) fires.** Verified independently below (§1.4) — it is not an
unobserved branch.

**I-1 union verified.** Injected `kind = "SynthFirstV1"` / `if flag: kind = "SynthSecondV1"` at a
real writer: **29 kinds, both literals collected, `unresolved ()`.** The union is correct and it is
the right call — a `kind` reassigned under an `if` is the ordinary form, and the pre-fix behaviour
(first assignment `ast.walk` reached) was a *silent under-report*, the exact failure class the
module exists to remove. Treating `>1` bindings as unresolved in `_params_elements` is also right:
a params **tuple** has a *shape*, and two candidate shapes cannot be unioned into one `?`-index, so
the only safe answers are "guess" or "route to the loud gate". It routes to the loud gate. Correct.

**Prose subtraction is by rule, not by hand exemption — confirmed.** `_text_insert_sites` subtracts
exactly two things: the line span of a bare string `ast.Expr` and everything from a `#` column
onward (`tokenize`). `orchestrator/stubs.py:147` (a `StubFinding.STUB_ABANDONED` attribute docstring
quoting a `cli.py` INSERT) falls out with no entry naming it. There is no exemption list in the
cross-check; the only exemption list in the module is `_INDIRECT_SITES`, which is the pre-existing
worker channel, unchanged. Claim accurate.

### 1.3 The stated residual — accurate in the direction it admits, under-specified in one place

**Confirmed it cannot catch what it says it cannot catch.** Injected a real writer using an
f-string table name:

```python
await conn.execute(f"INSERT INTO {table} (…) VALUES (?,?,?,?,?,?,?)", params)   # kind SynthFStringV1
```
→ **27 kinds, `unresolved ()`, silent.** The admitted residual is real, not a hedge. Good.

**And I confirmed the form it says it catches is caught.** Inline `+` of two adjacent literals:
```python
await conn.execute("INSERT INTO " + "findings (…) VALUES (…)", params)
```
→ fires: `findings.py:467: a findings INSERT the AST walk never recognised …`. Exactly as the
docstring's "`_TEXT_INSERT` … tolerates … an explicit `+`" promises.

**IMPORTANT — I-4. The `+` sentence is true only for *adjacent literals*, and the docstring does not
say so.** The same statement split at the table-name token across a *named* constant is silent in
**both** instruments:

```python
_SPLIT_HEAD: Final = "INSERT INTO "
_SPLIT_TAIL: Final = "findings (run_id, repo_id, kind, …) VALUES (?, ?, ?, ?, ?, ?, ?)"
await conn.execute(_SPLIT_HEAD + _SPLIT_TAIL, params)      # kind "SynthSplitV1"
```
Measured: **27 kinds, `unresolved ()`, 4 passed** — a live findings writer emitting an unlisted kind,
completely invisible. Same result for the one-constant variant `"INSERT INTO " + _TAIL2`
(`SynthPlusNameV1`): 27 kinds, silent.

Adjudication, per Rule 12's stop rule: this is a **boundary, not a defect to patch.** The headline
of the residual bullet — "SQL whose source text never spells the phrase" — does cover it, and the
silence requires the split to land *exactly* at the `findings` token; the natural refactors do fire
loudly (`"INSERT INTO findings (cols) " + _CONFLICT_CLAUSE` keeps the phrase and trips the walk-lost
branch). A normal author does not trip it. **The fix is one sentence, not a mechanism:** the
docstring's `+` tolerance should read "an explicit `+` *between adjacent literals*", because as
written it invites a reader to conclude `+` concatenation is covered when a named operand defeats
it. `tests/test_findings_kinds.py` module docstring, the "SQL whose source text never spells the
phrase" bullet and the `_TEXT_INSERT` comment (`:_TEXT_INSERT` block).

So: **residual honest in both directions at the headline level; one sub-claim over-promises.**

### 1.4 IMPORTANT — I-5. Two semantics-preserving edits DO fire the reverse branch

The report says: "on a purely semantics-preserving reformat my version does **not** fail". Its
controls (row 3 re-break + reindent, row 4 prose re-wrap) pass. Two other purely semantics-preserving
edits do **not**:

| edit (diff-confirmed) | result |
|---|---|
| a `# columns must match _row below` comment inserted **between** the concatenated fragments of `orchestrator/findings.py:126` `_INSERT_FINDING` | **fires**: `findings.py:127: the AST walk recognised a findings INSERT that the text cross-check cannot see — the two instruments have drifted apart` |
| `"INSERT INTO findings (…)"` → `"INSERT INTO [findings] (…)"` (bracket-quoted identifier; `_TABLE` allows `[`, `_TEXT_INSERT`'s interposable class does not) | **fires**, same message |
| control: the same comment placed *above* the literal | silent, correct |

Both are fail-**loud** and fail-**safe** — the kind stays counted, nothing goes silent — so this is
not a hole. But the message misdiagnoses a layout edit as instrument drift, and the report itself
names the risk precisely: "a detector that fires on layout gets disabled by the next lane it blocks."
The claim "does not fail on a purely semantics-preserving reformat" is therefore **overbroad as
written** (Guardrail 6: the claim is wider than the measurement behind it). Cheapest correction is
one character class: add `[`, `]` and `#` to `_TEXT_INSERT`'s interposable set (`tests/test_findings_kinds.py`,
the `_TEXT_INSERT` definition), plus a sentence in the report/docstring scoping the control to
"reformats that do not interpose a non-string token".

**Everything else in the DETECTOR report checks out.** Its self-reported instrument-validation
defect (the harness loading detectors by path so `REPO_ROOT` pointed at an empty tree, caught by the
baseline row reading 0 instead of 27) is the Guardrail-6 third check working, and it is disclosed
rather than buried. `_INDIRECT_SITES` keying on normalised SQL is fail-safe as described. M-1
(`_worker_findings`) is disclosed, out of brief, not re-raised here.

---

## 2. FIVESITE (`f466287`)

### 2.1 The identity claim: re-measured independently, and it holds

I ran my own wrap-aware sweep (whole-file whitespace collapse with an offset→1-based-line map) over
every tracked `.py .md .sql .txt .yaml .yml .toml .rst .sh .json .cfg .ini .bzl .bazel` + `BUILD`
file, excluding `references/`, `tools/go`, `tools/bazelisk`:

```
docs/DECISIONS.md:6713  len=336
docs/SPEC.md:182        len=336
docs/SPEC.md:6952       len=336
distinct canonical texts: 1 | lengths: [336]
```

**3 sites · 336 characters each · `len(set()) == 1`. Claimed and measured agree exactly.**

False-fallback re-sweep (`SCAN if no phase below the frontier holds`, wrap-aware): **one live hit,
`docs/INTEGRATION_HONESTY.md:4057`**, which is APPLY's disclosure quoting the clause in order to call
it false. Correct as a quotation. **Zero live statements retain the false fallback — confirmed.**

FIVESITE's correction of the brief (§5 D/E, `schema.sql` and the SPEC CAVEAT, do not state the floor
rule) is right: I read both blocks (`schema.sql:275-300`, `docs/SPEC.md:4074-4096`) and they are the
`findings.kind` CAVEAT, containing no floor rule, no `SCAN`, no `phase_floor`. Refusing to touch them
was correct. `src/fleet/models/enums.py:64-67` is a fourth statement, already correct including the
hard stop; leaving it alone is the mirror-image rule applied correctly.

### 2.2 CRITICAL — C-1. The sixth successor is 17 lines below the fix, in the same paragraph

**`docs/SPEC.md:6969`** (§11.5 step 5, the *same paragraph* `f466287` corrected):

> … then walk *backwards* asking `evidence_holds(repo, p)` … and **stop at the first phase whose
> evidence holds, because the phases below it are covered by it.**

`phase_floor` (`src/fleet/orchestrator/reentry.py:98-99`) breaks on `_HARD_STOPS` **before** it ever
reaches the `evidence.get` break at `:100-101`. So the walk does not stop only at the first holder,
and for a hard stop the trailing rationale is false twice over: the phases below a `SKIPPED` row are
*not* "covered by it" — the walk stops because the operator excluded that phase, and an excluded
phase can never produce holding evidence at all.

This directly contradicts the clause `f466287` wrote 17 lines above at `:6952`, which says the floor
is the phase above the highest holder **"or which is a `DEGRADED`/`SKIPPED` hard stop"**. The
corrected clause then *points at* this passage — "That phase is precisely where the backward search
below **stops**" — so the canonical sentence now cites a description that denies it.

`f466287` did fix the intervening sentence for exactly this reason ("the *first* holder **or hard
stop** the walk meets going down", `:6963-6964`) and stopped one sentence short.

**Failure scenario.** A reconciler implementing §11.5 step 5 from the operative algorithm — the
"walk backwards … stop at the first phase whose evidence holds" sentence, which is the one that
describes *how*, not *what* — writes the walk without the `_HARD_STOPS` break. That is precisely the
mutation FIVESITE ran in §4: it returns `SCAN` where the code returns `VERIFY` (BUILD `DEGRADED`) and
`BUILD` (TRANSFORM `SKIPPED`), and it demotes every repo with an excluded middle phase to `SCAN` on
every resume — the defect `reentry.py`'s docstring calls out as "not a nicety". `f466287` claimed
"one wording, every site that states it"; this site states it, in the same paragraph, and was missed.

**Remedy** (one edit, in the wording style already established): `:6969` → "stop at the first phase
whose evidence holds **or which is a `DEGRADED`/`SKIPPED` hard stop (tested first)** — where evidence
holds, because the phases below it are covered by it; at a hard stop, because ADR-0077 §5 forbids
passing it."

### 2.3 IMPORTANT — I-3. A fifth statement of the stop condition, same omission

**`docs/DECISIONS.md:7333-7334`** (ADR-0082 §1, "Unconditional" bullet):

> `phase_floor` legitimately returns the frontier itself with nothing below it to demote: **the
> backward walk breaks on the first phase whose evidence holds**
> (`src/fleet/orchestrator/reentry.py:100-101`) …

The cited lines are accurate for the sentence as written (`:100-101` *is* the evidence break), but
the sentence states the walk's stop condition and omits the `_HARD_STOPS` break at `:98-99`. The
bullet's *conclusion* survives (a hard stop directly below the frontier also returns the frontier),
so this is Important rather than Critical — but it is the same claim class, and it is a live ADR
rationale, not history.

**Root cause worth recording for the round.** FIVESITE's re-sweep detector was the *false-fallback
phrase*. Guardrail 7 says sweep for the **class**, not the reported site. The class here is "any
sentence stating where `phase_floor`'s backward walk stops", and a phrase-shaped detector cannot see
it. My own class-shaped sweep (`frontier` within 260 chars of `still holds` / `whose evidence` /
`earliest|highest|lowest phase`, wrap-aware) surfaced both C-1 and I-3 and nothing else — 7 hits
total, 4 canonical/correct (`SPEC:180`, `SPEC:6951`, `DECISIONS:6711`, `enums.py:64`), 1 unrelated
(`reentry.py:18`, see Minor 1), and these 2.

---

## 3. APPLY (`60d400b`, `e0404b0`, `a58fc1c`, `d0de310`)

### 3.1 What checks out

* **Citation repointing (`e0404b0`) is clean.** `docs/DECISIONS.md:7301`/`:7467` and
  `docs/INTEGRATION_HONESTY.md:3859-3860` now name
  `docs/superpowers/plans/design-resume-step5-task6-demotion-writer-report.md` and
  `docs/superpowers/plans/task-item17-report.md`; both verified `git ls-files --error-unmatch` →
  TRACKED. A wrap-aware scan of all of `docs/**/*.md` finds **no** remaining `.superpowers/sdd/`
  citation outside promoted report *bodies* (history) and one pre-existing hit in
  `docs/superpowers/plans/rate-limiting-scope-research.md`, which is out of scope for these commits.
  The "untracked" wording, which stopped being true at `5be5064`, is gone.
* **The CAVEAT's three line citations are accurate**: `bazel/generators.py:414` → `kind="VersionConflict"`,
  `graph/cycles.py:923` → `CoarseTarget(`, `rewrite/pipeline.py:263` → `kind="RuleOscillation"`.
* **Guardrail 7 satisfied on the CAVEAT**: both copies (`src/fleet/state/schema.sql:275-297` and
  `docs/SPEC.md:4074-4096`) carry the same corrected body in the same commit.
* **`a58fc1c`** — the item17/M1 retirement. The detector validation it describes is the three-check
  shape, each mutation `git diff --numstat`-proven non-no-op, and the entry closes by naming what the
  marker still does not cover. No overclaim found. Clean.
* **`d0de310`** is the round's best artifact: it caught its own commit's narrower successor by
  re-running the detector against the applied text, and disclosed instead of patching a subset. The
  measured table (`BUILD DEGRADED ⇒ VERIFY`, `TRANSFORM SKIPPED ⇒ BUILD`, otherwise `SCAN`) matches
  `phase_floor` exactly, as re-derived by FIVESITE and by me from `reentry.py:95-103`. Its wrong
  "five sites" enumeration was already flagged by FIVESITE and has since been corrected in place by
  lane BIND's editorial block at `docs/INTEGRATION_HONESTY.md:4085+` — **not re-raised**.

### 3.2 IMPORTANT — I-6. The CAVEAT ships a *narrower* claim than the commit that measured it

`60d400b`'s commit message states the measurement correctly:

> `UnmergedDependency` (no literal in `src/**/*.py` outside `settings.py:704`'s docstring)

The text it **shipped** dropped the exception, in both copies:

* `src/fleet/state/schema.sql:286-287` — `'UnmergedDependency' (no literal anywhere in src/)`
* `docs/SPEC.md:4084-4085` — identical wording

Measured: `grep -rn UnmergedDependency src/` → **`src/fleet/settings.py:704`** holds the literal
(inside a docstring), plus the two schema.sql listing lines themselves. So "no literal anywhere in
`src/`" is **false as written**, in the same commit whose message got it right — the Guardrail 6
pattern (a fix shipping a narrower/wrong successor) landing inside the parenthetical rather than the
headline. Note the wrap: the clause breaks across `:286`/`:287`, so a line-oriented grep for
`no literal anywhere in src` finds nothing in either file.

**Failure scenario.** The next author reconciling the CAVEAT greps `src/` for `UnmergedDependency`,
gets a hit, concludes the CAVEAT is stale, and "corrects" it by moving the kind out of the
never-emitted group — restoring the exact overclaim (`Every other name … is emitted from cli.py`)
that `60d400b` existed to remove. The substantive claim (nothing *emits* it) is true; only the
literal-grep sub-claim is wrong, which is what makes it dangerous — it is falsifiable by the
one-command check a reconciler will actually run.

**Remedy:** both copies → `'UnmergedDependency' (no literal in src/ outside settings.py:704's docstring)`.

### 3.3 IMPORTANT — I-7. A quotation attributed to `phase_floor`'s docstring is not in the file

`docs/INTEGRATION_HONESTY.md:4063-4064` (added by `d0de310`):

> The function's own docstring says so **("the backward search stops at a `DEGRADED` or `SKIPPED`
> row without ever moving the floor onto it", ADR-0077 §5)**; the new prose does not.

Measured against `git show main:src/fleet/orchestrator/reentry.py`, whitespace-normalised: that
string **does not occur**. The actual sentence (`reentry.py:32-33`) is:

> "if the backward walk reaches either, it stops there without moving the floor onto it."

The *semantic* claim is true — `reentry.py:23-33` does state the hard-stop rule, so `d0de310`'s
argument is sound. What is not true is that the quoted words are a quotation. CLAUDE.md §3 names this
exactly: "**Never quote another module's comment verbatim**: nothing enforces the copy, and one
reword leaves the quotation pointing at a string no longer in the tree." Here the copy was wrong on
arrival, in the file whose entire job is being right about what is and is not verified.

**Failure scenario.** A later lane greps `reentry.py` for the quoted sentence to check the disclosure
is still current, finds nothing, and either deletes the disclosure as stale or "restores" the
sentence into `reentry.py`'s docstring — adding a second, differently-worded statement of the rule to
the module that is currently the one unambiguous source for it.

**Remedy:** replace the quotation marks with a paraphrase + line cite: *the function's own docstring
says so (`reentry.py:23-33`, ADR-0077 §5)*.

---

## 4. Guardrail 7 — do the four documents agree with `reentry.py`?

`phase_floor` (`src/fleet/orchestrator/reentry.py:95-103`): frontier = first phase not in
`_SETTLED_FOR_DEMOTION`; walk down from `frontier-1`; `break` on `_HARD_STOPS` **first**; then `break`
on `evidence`; else `floor = phase`. Return `floor`, initialised to `frontier`.

| statement | agrees with the code? |
|---|---|
| `docs/SPEC.md:182` Constraint 7 (336-char clause) | **yes** |
| `docs/SPEC.md:6952` §11.5 step 5 (same 336 chars) | **yes** |
| `docs/DECISIONS.md:6713` ADR-0076 §1 (same 336 chars) | **yes** |
| `src/fleet/models/enums.py:64-67` | **yes** — different register, same rule, hard stop included |
| `docs/SPEC.md:6963-6964` ("first holder **or hard stop**") | **yes** — fixed by `f466287` |
| **`docs/SPEC.md:6969`** ("stop at the first phase whose evidence holds") | **NO — C-1** |
| **`docs/DECISIONS.md:7333-7334`** ("breaks on the first phase whose evidence holds") | **NO — I-3** |

So: the *canonical clause* is consistent across four sites and matches the code. The *operative
description of the walk* is inconsistent at two further sites, one of them 17 lines from the fix.

Not re-raised, per the brief: nothing mechanically binds the copies (a live lane is building it, and
`f84edcb`/`5f052f2` appear to be that work); `src/fleet/cli.py:10043`/`:10274`'s rejected algorithm
and the stale "All twelve workers"; DETECTOR's enum-shaped residual as a disclosed boundary.

## 5. Minor

1. **`src/fleet/orchestrator/reentry.py:18`** — "walks backward from there, asking **only** whether
   `evidence` holds at each earlier phase." Read alone this is the C-1 claim; read with numbered
   point 2 immediately below it (which states the hard stops in full) it is scoped to the contrast
   with `preconditions_hold`. Pre-existing, not touched by any commit under review, and the module is
   the one place the rule is stated completely. Flagged only because it is the file everything else
   cites; a four-word narrowing ("asking, evidence aside, only whether…") would remove the ambiguity.
   **Reported, not a defect.**
2. **`task-mech-report.md:130`** — "`cli.py`'s sixteen call sites"; measured 13. Confirmed as
   DETECTOR reported. Git-ignored scratch, not DETECTOR's file, low priority.

## 6. Evidence index

* Baseline / counts: `_Src`-driven measurement in the detached worktree at `363ecc1` — 16 AST execute
  sites, 15 distinct spans, 15 text sites, 27 kinds, `unresolved ()`, `4 passed in 1.99s`.
* Defeat matrix (all diff-confirmed by `git diff --numstat` before reading any result; every file
  restored from a pristine byte-copy and re-verified `git diff --numstat` empty):
  C-1 triple-quote reformat → resolved, 4 passed · same + unlisted kind → old **4 passed**, new
  **1 failed** naming `SynthQuarantineV1` · inline `+` of two literals → **fires** · `+` with a named
  constant (`SynthPlusNameV1`) → **silent** · split across two module constants (`SynthSplitV1`) →
  **silent** · f-string table (`SynthFStringV1`) → **silent** (admitted) · comment interposed in the
  concatenation → **fires (reverse branch)** · `INSERT INTO [findings]` → **fires (reverse branch)** ·
  comment above the literal (control) → silent · conditional `kind` reassignment → **29 kinds, both
  literals**.
* Wrap-aware sweeps: canonical clause 3 hits × 336 chars, 1 distinct; false fallback 1 live hit
  (a quotation); class-shaped floor-rule sweep 7 hits, 2 wrong.
