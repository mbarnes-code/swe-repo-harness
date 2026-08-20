> Promoted from `.superpowers/sdd/design-resume-step5/review-5.md` (round C scratch, lane Review-5), snapshot taken while `main` was at `1050e0a`. Examines FIX-PF, ADR82, CONSIST (six commits, anchored at `8ea1881`). Verdicts: FIX-PF CLEAN (1 Minor); ADR82 FINDINGS (1 Important); CONSIST FINDINGS (1 Critical, 1 Important, 2 Minor).

# Review 5 — FIX-PF, ADR82, CONSIST (six commits)

Read-only review. Nothing in the tree was changed by this review.

`git status --short` at review start (primary checkout):

```
 M src/fleet/cli.py          <- sibling lane, uncommitted; NOT reviewed, NOT reported
?? .superpowers/
```

All committed-state claims below are anchored at `8ea1881` (`main` tip at review time) unless a
ref is named. Every `cli.py` fact was read from the clean worktree at that ref, never from the
dirty working copy.

**Method.** All test execution and every mutation ran in a detached `git worktree` at
`/tmp/…/scratchpad/wt` (`git worktree add --detach … 8ea1881`), whose `conftest._bazel_root()`
resolves to `<worktree>/tools/bazel-test-root` — a private `BAZEL_ROOT`, so `pytest_sessionfinish`
could not reap the primary checkout's. One pytest session at a time; no `FLEET_*` exported;
`.venv/bin/python` throughout. Every mutation was confirmed non-no-op with `git diff --numstat`
**before** its result was read.

Verdicts: **FIX-PF CLEAN** (1 Minor) · **ADR82 FINDINGS** (1 Important) · **CONSIST FINDINGS**
(1 Critical, 1 Important, 2 Minor).

---

## 1. Mutation spot-checks — all four reproduce exactly as claimed

| Lane | Mutation | numstat | OLD tests | NEW tests | Claimed | Verdict |
|---|---|---|---|---|---|---|
| FIX-PF | `reentry.py:98` `in _HARD_STOPS` → `is RepoStatus.DEGRADED` | `1 1` | 36 passed | 3 failed, 38 passed — exactly the three SKIPPED cases | 36 green / 3 fail | **reproduced** |
| FIX-PF | `reentry.py:100` `evidence.get(phase, False)` → `True` | `1 1` | — | 2 failed, 39 passed — exactly the two sparse-mapping cases | 39 green / 2 fail | **reproduced** |
| CONSIST | delete `_DEMOTE_FINDING_SQL`'s whole `ON CONFLICT … DO UPDATE` tail (`repository.py:835-836`) | `0 2` | 38 passed | 1 failed (`IntegrityError: UNIQUE constraint failed: index 'ux_findings_ident'`), 39 passed | 38 green / 1 fail | **reproduced** |
| CONSIST | add `SKIPPED` to the carve-out (`repository.py:1452`) | `1 1` | 38 passed | 1 failed (`…_skipped_phase_keeps_its_status_but_still_loses_its_checkpoint`), 39 passed | 38 green / 1 fail | **reproduced** |

OLD = the pre-commit test file (`git show <commit>^:tests/…`) run against the mutated source; NEW =
the committed file. Both lanes' old-passes/new-fails discrimination is genuine, and in each case
the *only* failures were the newly-added tests. Scoped suite at `8ea1881` in the worktree:
`201 passed` — matches both lanes' claim.

ADR82 (`4ff8157`) introduces no behaviour and correctly claims no mutation of its own; its §6
verification table was checked statically instead (see §4).

---

## 2. CONSIST's listing counts — measured vs claimed

Re-measured independently. Listing sets were extracted **wrap-aware**: the `findings` CREATE-TABLE
block was sliced from each file, comment continuations (`\n\s*--\s*`) collapsed, all whitespace
normalised, then `'Name'` tokens matched — with an offset→line map so every hit is reportable at a
line. The emitted-kind set was enumerated independently from all 15 `INSERT INTO findings` sites in
`src/`, resolving every variable-passed `kind` back to its literals (`_abandon_repo`'s 8 callers,
`_note_finding`'s 2, the worker `findings` tuples through the scan persist, and the module
constants `PHASE_DEMOTED_KIND` / `PR_RECORD_KIND` / `VERIFICATION_KIND` / `CYCLE_FINDING_KIND`).

| Claim | Claimed | Measured | Verdict |
|---|---|---|---|
| distinct emitted finding kinds in `src/` | 27 | **27** | ✅ |
| absent from `schema.sql` before (`704e52f`) | 19 | **19** (pre-set = 20 names; all 19 absent) | ✅ |
| absent from `docs/SPEC.md` before | 21 | **21** (pre-set = 18 names; all 21 absent) | ✅ |
| emitted kinds already listed in `schema.sql` | 8 | **8** (`BackendUnavailable`, `CapabilityDrift`, `ConfigDrift`, `CycleDetected`, `OperatorQuarantine`, `OversizeBlob`, `SymbolBudgetExceeded`, `no-manifest`) | ✅ |
| emitted kinds already listed in `SPEC.md` | 6 | **6** (the 8 above minus `CapabilityDrift`, `BackendUnavailable`) | ✅ |
| the two copies now carry identical name sets | 39 each, identical | **39 each, set-identical** (`schema.sql:249-313`, `SPEC.md:4047-4108`; symmetric difference empty both ways) | ✅ |

Arithmetic closes both ways: 20 + 19 = 39 and 18 + 21 = 39. The independent emitted-kind
enumeration produced the same 19-name emitted-but-undeclared set with **no additions and no
subtractions**. Unlike the two earlier waves whose sweep counts failed to reproduce, **every
CONSIST count reproduces.**

One presentational caveat, Minor: `no-manifest` is counted inside the 39 in both copies. It is a
genuine emitted kind (`workers/interrogate.py:336` → the scan persist INSERT at `cli.py:1582`), so
the number is defensible; a reader should just know one of the 39 is lowercase-hyphen and appears
in the table's header prose line rather than in the `| 'Name'` column.

---

## 3. FIX-PF — `4a1a184`, `8c00971` — **CLEAN**

The defect is real and the fix is right. `_HARD_STOPS = {DEGRADED, SKIPPED}`
(`src/fleet/orchestrator/reentry.py:54`) is tested at `:98`, before the evidence branch; the floor
can therefore never land on either status, and the walk cannot cross either. Verified by running
`phase_floor` directly, not only through the suite. The `PhaseRow` move under `TYPE_CHECKING`
(`:40-43`) is safe: `from __future__ import annotations` is active at `:37` and no runtime
reference to `PhaseRow` survives.

`8c00971`'s pseudocode now matches the implementation line for line — `settled` gains `DEGRADED`
(matching `_SETTLED_FOR_DEMOTION`), `hard_stop` is used by the walk rather than defined and
orphaned, and step 3 states the conditional, `DEGRADED`-sparing sweep that `demote_to_floor`
actually landed.

**Checked for a narrower successor and found none.** The two-sided reading it wrote ("the floor may
not land *on* a `SKIPPED` phase, and the walk may not continue *below* one",
`reentry.py:21-30`) is exactly what the code does on both halves; I re-ran the lane's own detector
(the reverting mutation) against the fix and it fails only the new cases.

### M-1 (Minor) — `tests/test_reentry_floor.py:24`

> "the 28-case table cannot reach either, because it runs with `evidence` uniformly `True` and so
> never enters the backward walk."

The backward `for` loop **is** entered; it breaks on its first iteration (at the hard stop when
`frontier-1` is `DEGRADED`/`SKIPPED`, otherwise at `evidence` → `True`). The conclusion — that the
table cannot discriminate either hard stop, `floor` always equalling `frontier` — is correct and is
what the sentence is there to say. Wording only.

---

## 4. ADR82 — `2c8dc79`, `4ff8157` — **FINDINGS (1 Important)**

`2c8dc79` is correct and verified against collection, not against spelling:
`grep -n "test_transition_demotes_without" tests/test_state_models.py` → `:711`
`def test_transition_demotes_without_writing_a_record_or_naming_a_new_sink`. `CLAUDE.md:68` now
names a node id pytest can collect. The three other citations it triaged are as it describes.

Every factual claim in ADR-0082 §3 and §6 that I probed holds, checked by loading the enums in the
interpreter that runs the code rather than by reading declarations:

- `SKIPPED in TERMINAL_STATUSES` → `True`; `ALLOWED_TRANSITIONS[SKIPPED]` → `frozenset()` ✅
- `DEGRADED in TERMINAL_STATUSES` → `False`; `ALLOWED_TRANSITIONS[DEGRADED]` → `{RUNNING, SUCCEEDED, REQUIRES_HUMAN_INTERVENTION}` — the live `RUNNING` edge §3 rests on ✅
- `ALLOWED_TRANSITIONS[SUCCEEDED]` → `frozenset()` (§4 premise 2) ✅
- `RESUME_DEMOTE` keys → `[SUCCEEDED]`; `demote(DEGRADED, …)` and `demote(SKIPPED, …)` both raise `ValueError` ✅ (this is the claim `SPEC.md` §11.5 makes about `demote()` — it is true)
- statuses carrying a `SKIPPED` edge → `PENDING`, `BLOCKED` only ✅
- `:1435`/`:1440` anchors at `8c00971` resolve to `:1447`/`:1452` at `8ea1881` (a +12 shift from `704e52f`'s docstring insert) — the ADR names its ref, so the anchors are honest.

**§4's conclusion survives its known-false premise.** The premise under repair by a live lane
("`SKIPPED` is written only to phase 1") sits in the *SKIPPED* aside, not in the DEGRADED induction
the ruling rests on. And the aside is not load-bearing in the first place: `SKIPPED` has **no**
carve-out, so a `SKIPPED` row anywhere in the span loses its checkpoint with everything else and
cannot leave a stale anchor at all. The adversarial-only ruling for the `DEGRADED` hazard stands on
premises 1–3, which I verified independently. Two things worth recording for whoever repairs the
premise: (a) the aside's second leg — "whose only entry edges are from `PENDING` and `BLOCKED`" —
is true of `ALLOWED_TRANSITIONS` but is **not** enforcement for the real writer, since
`fleet quarantine` writes `UPDATE phases SET status = 'SKIPPED'` as raw SQL that never consults the
table (`cli.py:9747-9760` at `8ea1881`; CONSIST's own `_quarantine_phase` helper says as much at
`tests/test_repository.py:1127-1135`); (b) neither leg needs to be repaired for §4 to hold, so the
honest repair is to *delete* the aside rather than narrow it.

### A-1 (Important) — the plan still calls a decided question open

`docs/superpowers/plans/design-resume-step5.md:258-260` (at `8ea1881`, i.e. **after** all six
commits):

> **Open, and deliberately not decided here:** that exclusion names `DEGRADED` only, so a `SKIPPED`
> row above the floor still loses its `checkpoints` row. Subtask 6 owns that question; ADR-0077 §5
> speaks to the status, not to the checkpoint.

ADR-0082 §3 decided that question — its own words: *"The open question
`docs/superpowers/plans/design-resume-step5.md:258-260` handed to subtask 6 is answered here."* —
and `704e52f` pinned the decision with
`test_a_skipped_phase_keeps_its_status_but_still_loses_its_checkpoint`. The plan was not edited in
either commit, and `grep -n "0082" docs/superpowers/plans/design-resume-step5.md` returns nothing.

Concrete failure: the plan is the artifact subtask 6's author reads — it is addressed to them by
name. That author opens a settled question, and the two live directions are both wrong. Either they
re-adjudicate and pick "spare `SKIPPED` too" (the reading `8c00971`'s own prose leans toward, since
the sentence pairs `SKIPPED` with `DEGRADED` everywhere else), which turns
`test_a_skipped_phase_keeps_its_status_but_still_loses_its_checkpoint` red and contradicts
ADR-0082 §3 — or they burn a round re-deriving an answer that already exists. This is Guardrail 7's
first bullet exactly: fix the code and its doc listing in the same change. The ADR asserted the
plan's question was answered without making the plan say so.

Fix (one edit, not a re-decision): replace the "Open" paragraph with the decision and a pointer to
ADR-0082 §3 and the test that binds it.

---

## 5. CONSIST — `704e52f`, `8ea1881` — **FINDINGS (1 Critical, 1 Important, 2 Minor)**

`704e52f` is the strongest work in the round. The diagnosis is exactly right and is measured, not
argued: the old tail asserted `len(...) == 3` after a **no-op** second call (phases 2–4 already
`PENDING`, the `is not RepoStatus.SUCCEEDED` filter at `repository.py:1436` skips every one, the
INSERT never runs), so the row count was held by the absence of a write. I confirmed the
old-passes case myself — with the entire `ON CONFLICT` clause deleted the old file is 38/38 green.
The replacement re-earns `SUCCEEDED` first, so the INSERT genuinely re-executes, and it asserts
the second `reason` lands, which kills `DO NOTHING` as well as the missing clause. The renamed
tail assertion (`== ()`) now states the property the situation really has.

Names checked against bodies (priority 4): `…_and_never_a_silent_one` is detectable — a demotion
without its finding shows up as a missing row in the per-phase assertion.
`…_does_not_hold_and_so_does_not_stop_the_walk`, `…_but_an_absent_key_one_rung_above_it_does_not`
and `…_keeps_its_status_but_still_loses_its_checkpoint` each assert a value that the named absence
would change. **No test in either commit carries a name its body cannot detect.**

### C-1 (Critical) — the narrower successor: the off-by-one fix kept the wrong quantifier, at all three sites it edited

`8ea1881` corrected "demote to the earliest phase whose evidence still holds" → "the phase
**above** the earliest one whose evidence still holds". The off-by-one half is fixed. **The
quantifier is still wrong, and the sentence now reads as authoritative** ("never that phase
itself").

The walk breaks at the **first phase it meets going down** from the frontier — i.e. the
*highest/latest* phase below the frontier whose evidence holds, not the *earliest*.

Sites, all written or rewritten by this commit:

- `docs/SPEC.md:182` (Constraint 7): "demotes the repo to its re-entry floor — the phase **above**
  the earliest one whose evidence still holds, never that phase itself"
- `docs/SPEC.md:6936` (§11.5 step 5): "which is *not* the earliest phase whose durable evidence
  still holds: that phase is precisely where the backward search below **stops**, and the floor is
  the phase above it"
- `src/fleet/models/enums.py:64-66` (`RESUME_DEMOTE`): "the phase ABOVE the earliest one whose
  EVIDENCE still holds, never that phase itself: that phase is where `reentry.phase_floor`'s
  backward walk STOPS"

Measured against the shipped function (all four phases `SUCCEEDED` except `VERIFY` `PENDING`;
`evidence = {SCAN: True, TRANSFORM: True, BUILD: False, VERIFY: False}`):

```
actual phase_floor(...)                                   -> Phase.BUILD (3)
SPEC's formula: phase above the EARLIEST holder (SCAN)    -> Phase.TRANSFORM (2)
```

The suite already pins the true behaviour against the new SPEC sentence:
`tests/test_reentry_floor.py:187-197`, `test_backward_search_stops_as_soon_as_evidence_holds`, uses
that exact mapping and asserts `Phase.BUILD`. A second probe rules out the "monotone evidence makes
them the same phase" defence — with `{SCAN: True, TRANSFORM: False, BUILD: True}` the walk stops at
`BUILD` and returns `VERIFY`, even though `SCAN` also holds. Under the traversal's own monotonicity
assumption the holders form a prefix `1..k`, and the stop is at `k`, the **latest** of them; the
*earliest* is always `SCAN`, so the SPEC's formula degenerates to "the floor is always `TRANSFORM`".

Concrete failure: a reconciler implementing §11.5 step 5 or Constraint 7 literally demotes to phase
2 on every resume that has any holding evidence — re-running `TRANSFORM` and `BUILD` for every repo
in the fleet. That is materially the same class of damage CR-2 F10 was raised to prevent ("would
re-run every phase between"), rebuilt one rung over. It is also the pattern this round is hunting:
a fix that corrects half a false claim and leaves a narrower false successor behind, written by the
lane that had just been asked to look for it.

Correct wording: *the floor is the lowest phase below the frontier whose evidence does **not** hold
— equivalently, the phase immediately above the **highest** phase below the frontier whose evidence
still holds — or the frontier itself if the phase directly beneath it already holds.*

Two further occurrences of the phrase exist and are **correctly left alone**:
`docs/DECISIONS.md:6713` and `docs/INTEGRATION_HONESTY.md:3888` both record what an earlier SPEC
revision said, as history (the latter quotes `d0b1150` verbatim). Reporting, not editing, per
Guardrail 7's mirror-image warning.

### C-2 (Important) — the new CAVEAT sentence asserts an emission that does not exist, in both copies

`8ea1881` added to `src/fleet/state/schema.sql:283-284` and to the `docs/SPEC.md` copy
(`:4076-4077`):

> "; every other name in the DECLARED list is emitted from cli.py."

This is a new universal quantifier, written into the one comment whose stated job is to answer
which kinds are actually emitted. Of the declared names the CAVEAT's exclusion list does not
already cover, **four have no writer anywhere in `src/`**:

- `UnmergedDependency` — zero string literals in `src/**/*.py`; the only occurrences tree-wide are
  a docstring (`src/fleet/settings.py:704`) and the listing itself (`schema.sql:263`).
- `VersionConflict` — built as a `GraphFinding` at `src/fleet/bazel/generators.py:414` and never
  handed to any of the 15 `INSERT INTO findings` sites (none accepts a `GraphFinding`).
- `CoarseTarget` — same shape, `src/fleet/graph/cycles.py:935`.
- `RuleOscillation` — a `RewriteFinding` at `src/fleet/rewrite/pipeline.py:263`, read in-process at
  `src/fleet/workers/rewrite.py:651`, never persisted.

The pre-existing CAVEAT was merely *incomplete* about these (it enumerated `BaselineRed`,
`PreflightFailed`, `RuleConflict`, `WeakEdge` and the four Contract/Hoist kinds). The commit
converted that silence into an explicit false claim — the narrower-successor pattern again, this
time strengthening rather than narrowing.

Concrete failure: an operator or a future author reads the corrected CAVEAT, believes
`SELECT … WHERE kind = 'UnmergedDependency'` can return rows, and builds a §3.4 step-5 report or a
dashboard panel on a query that is structurally empty — the precise question the CAVEAT exists to
answer, now answered wrongly with the authority of a just-audited listing.

Fix: add the four names to the CAVEAT's never-written enumeration, in **both** copies, and weaken
the new sentence to "every other name in the DECLARED list that is emitted at all is emitted
through an INSERT in `cli.py`". (That weaker form is true: `OversizeBlob` and `SymbolBudgetExceeded`
carry literals in `workers/`, but reach the database through the scan-persist INSERT at
`cli.py:1582`.)

### C-3 (Minor) — `PhaseDemoted` is listed as a live writer with no production call path

The new listing's header — "Each of these has a live writer in `src/`" — is true of the INSERT
statement (`src/fleet/state/repository.py:833`, executed at `:1457`), but
`grep -rn "demote_to_floor" src/` returns only the Protocol declaration (`:453`) and the
implementation (`:1340`): no production caller exists yet, so no run can currently emit the kind.
ADR-0082 §6 discloses this honestly ("`demote_to_floor` has no production caller"); the listing
does not. A one-clause note ("subtask 7 is the first caller") would keep the two artifacts from
disagreeing.

### C-4 (Minor) — see §2: `no-manifest` inside the 39.

---

## 6. Guardrail 7 sweep — is `docs/SPEC.md` now consistent with `reentry.py`?

**On the hard stops: yes, and it names the real mechanism.** `SPEC.md:6961-6975` now (a) covers
`SKIPPED` as well as `DEGRADED`, (b) separates the two properties, and (c) attributes each to the
construct that actually delivers it — `demote()` raising for "never demoted" (verified: raises
`ValueError` on both), `_SETTLED_FOR_DEMOTION` for the forward frontier scan, and
`orchestrator/reentry._HARD_STOPS` for "never searched past". That last name appeared nowhere in
`docs/` before this commit, and it is the only thing in the tree that enforces the property.
CR-4's Critical is genuinely closed, and the SPEC's account of the `checkpoints` carve-out
(`SPEC.md:6936-6940`) matches `repository.py:1447-1452` on all three properties: conditional,
span-wide, `DEGRADED`-only.

**On the floor definition: no** — see C-1. `docs/SPEC.md` at `:182` and `:6936`, and the
`enums.py:64` comment, describe a floor the shipped `phase_floor` does not compute, and the
existing test at `tests/test_reentry_floor.py:187-197` contradicts them directly.

---

## 7. Already-adjudicated — confirmed, not re-raised

- `src/fleet/cli.py:10073` / `:10367` rejected algorithm, and the stale "All twelve workers"
  (measured 11): still present, still blocked on the live lane owning that file. Not re-reported.
- ADR-0082 §4's "`SKIPPED` is written only to phase 1" premise: known false, live lane repairing.
  Not re-reported; its conclusion is separately assessed in §4 above and **survives**.
- The `findings.kind` listing being a snapshot that will drift again: known, live lane mechanising.
  Not re-reported. C-2 is a different defect — a false statement inside the snapshot, not the
  snapshot's staleness.
