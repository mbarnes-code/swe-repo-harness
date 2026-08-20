> Promoted from `.superpowers/sdd/design-resume-step5/review-final.md` (round C scratch, lane REVIEW-FINAL), snapshot taken while `main` was at `1ae3ffc`. The whole-round review of `6a5e534..d123035`, reviewed at `d123035`. Verdict: land, with a documentation-debt punch list — 0 Critical, 12 Important, 8 Minor, plus a ranked list for round D.

# Final round review — `6a5e534..d123035` (resume step 5, round C/§39)

**Reviewed at** `d123035`. Working tree clean at review start (`git status --short` → only untracked
`.superpowers/`), so every citation below is HEAD, not a sibling's uncommitted edit. Two commits
(`94d3778`, `d123035`) landed while this review ran; findings I8 and I3 are about text those
commits touched and were re-read at `d123035`.

**Rubric**: `CLAUDE.md` at HEAD (125 lines, amended four times this round).
**Suite**: not re-run (per brief). See I8 — the recorded green result does not anchor at HEAD.

---

## Verdict

**Land, with a documentation-debt punch list.** The code that landed this round is correct as far
as I could exercise it, and the round's honesty discipline is the best I have seen in this tree —
`docs/INTEGRATION_HONESTY.md:3405-3440` (D55) is a model adjudication: it corrects its own premise,
verifies `grep -rn '\.resize(' src/fleet/` returns zero call sites, and holds the verdict at
"premise corrected, defect fully open, **0% closed** by this work". Nothing in the round claims the
AIMD controller or §11.8 rate limiting is built.

What did not hold is the round's own stated failure mode. **A wrong-or-narrower successor survives in
roughly a dozen places, four of them inside artifacts written expressly to stop it** — Layer D's own
failure message (I3a), a mechanism's site census (I2, I4), and the honesty ledger twice (I5, I12).
None changes runtime behaviour today; all are inputs the next author reconciles against, which is
exactly the damage `CLAUDE.md` guardrail 7 describes. The three highest-cost ones (I11, I12, I9) each
instruct a future author to *undo* work this round landed.

The single most instructive pattern: **three of these survived a reflow rather than a rewrite.**
`8ea1881` re-wrapped `enums.py`'s false demotion-path clause onto its own line without reading it
(I11); `1e857f3` and `704e52f` each grew a docstring and invalidated citations into it (I3a, I3b).
`CLAUDE.md:122`'s wrapped-match lesson was learned for *detection*; it has not been learned for
*authorship*.

### Counts by severity

| Severity | Count |
|---|---|
| Critical | **0** |
| Important | **12** |
| Minor | **8** |

No Critical: I could not construct any input or state that produces a wrong *runtime* outcome from
code that landed this round. Every Important finding is a claim a future author acts on.

---

## 1. The three new mechanisms

### `tests/test_findings_kinds.py` — **REAL, narrower than stated**

*What it inspects*: `_Src.__init__` (`:170`) AST-parses every `src/fleet/**/*.py` by `rglob` — not a
hardcoded list — walks `.execute`/`.executemany` calls (`:380-383`), resolves arg0 through literals,
module constants and single locals (`:188-209`), then resolves the `kind` slot. A brand-new module
or subpackage emitting a new kind **is** caught (verified by synthetic injection). `363ecc1`'s
`_recognition_gap` genuinely fails in **both** directions — both branches (`:475-478`, `:481-484`)
were fired experimentally.

*The blind spot it could never see*: the mechanism is two instruments (AST + raw text) that share a
normaliser, and **a `/* */` SQL comment between `INTO` and `findings` defeats both at once**
(`:159-161` text class, `:133` AST `_TABLE`). Verified: `INSERT INTO /* the findings table */
findings (...)` emitting `SynthSqlCommentV1` yields `unresolved=0`, the kind absent from the emitted
set, and **all four tests green**. That is the recognition-gap failure `CLAUDE.md` guardrail 6 was
amended to prevent, one normalisation layer deeper.

*Stated residuals vs actual*: `:32-52` discloses f-string/concat/builder forms and the bare-name
worker channel. It does **not** cover findings I6a–I6d below, and `:42-43` residual 2 is **false as
written**.

### `tests/test_floor_rule_statements.py` — **REAL, narrower than stated**

*What it inspects*: four census sites located by a whitespace-flexed anchor, not line numbers
(`_CENSUS`, `:164`; `_EXPECTED_SITES`, `:191-195`). Layer B genuinely parses the hard-stop status
set, the cited symbol and the fallback phase **out of the prose** and feeds them to the live
`phase_floor` — prose really does drive the assertion. Reword the anchor and it **fails loud** (site
count mismatch at `:220`), verified experimentally; it does not silently census zero. Layers D and E
are likewise count-guarded.

*The blind spot it could never see*: **`reentry.py`'s module docstring** (`:12-27`) states the same
rule in its own words and is bound by nothing. Layer D's `_STOP_CONDITION` matches **0** of its
sentences (measured). `21d6a87` — the commit that "closed the measured blind spot" — enrolled
`reentry.py` with **one** expected site, the `phase_floor` docstring at `:71`, and left the module
docstring seventeen lines above it ungoverned. See I2: that is a narrower successor inside the fix.

*Stated residuals vs actual*: `_RESIDUAL` items 3, 4 and 6 are accurate and were confirmed by
experiment. Item 1 scopes the residual to "a falsehood outside those three claims" — but I1's
accepted falsehood is **inside** claim one (the hard-stop set), so the residual excludes the real
gap.

### `tests/test_instruments_are_armed.py` — **REAL, materially narrower than its own headline**

*What it inspects*: not a hardcoded list — `_test_sources()` (`:70-73`) rglobs `tests/`, classes come
from `ast.walk` (`:150`), the only hardcoded list is a one-entry exemption (`NOT_OVERRIDES`,
`:57-62`). The rename scenario **really works**: a synthetic
`EcosystemAdapter.import_specifier` → `…_RENAMED_AWAY` produced `orphans=['Intruder.…']` against a
control of `orphans=[]`, because it resolves the base by *import* and an unresolvable base is a hard
failure (`:181-184`), not a skip. Coverage is 18 classes / 34 methods, with an anti-vacuity floor of
30 (`:186`) that has only 4 methods of margin — loud, not silent.

*The blind spots it could never see* — three green-while-false shapes, all reproduced:
`assignment-form` overrides (`x = _fake` in a class body, `:154-159`); a `def` nested under
`if`/`try` inside a class body (`:154` iterates `node.body` shallowly); duplicate `ClassDef` names
where the last `ast.walk` hit wins (`:150`, `:224`) — and **two duplicate names already exist**
(`tests/test_budgets.py:696`/`:1272`, `tests/test_llm_backend_bedrock.py:400`/`:513`), latent only
because both are currently base-less.

*Stated residuals vs actual*: `:37-43` states three real limits, none of which is any of the three
above; `:25` states the property as "every method **defined on** a test-local subclass", which an
assignment-bound or `if`-nested method satisfies and the body never sees.

**Verdict on all three: real mechanisms, not conventions in mechanism's clothing.** Each fires on a
real disarming edit and each fails loud on the shape it targets. All three overstate their reach in
their own docstrings, which is the honest-disclosure half `CLAUDE.md` Rule 12 asks for and did not
get.

---

## 2. Is anything half-landed or presented as more complete than it is?

**No — with one live-code exception (I9).** This is the round's strongest area.

- `phase_floor` and `demote_to_floor` have **zero production callers**; `docs/PROGRESS.md:5915-5931`
  says so in those words ("four of ten are done (1, 2, 3, 6)… **Neither of this round's two new units
  has a caller**").
- Protocol safety confirmed: `demote_to_floor` is declared on `StateRepository` (`:453`), **not** on
  `ReadOnlyRepository`, and every consumer that could be handed a non-Sqlite object types against the
  read half (`orchestrator/scheduler.py:302`, `workers/base.py:312`). `SqliteStateRepository` is the
  only implementation in `src/`. Nothing assumes an unbuilt subtask.
- `ResizableLimiter` is honestly recorded as **inert**: ADR-0083's title and §3
  (`docs/DECISIONS.md:7742-7754`) say "runtime no-op until a later subtask calls `resize()`" and
  recommend reverting the primitive if R5 never lands; ADR-0084 (`:7986-7991`) disclaims deciding R5;
  D55 holds at 0% closed. **No sentence anywhere describes the AIMD controller as built.**
  (Precision the brief did not have: the *class* is live — `Limits.build` constructs it per tier and
  `for_tier` returns it — while `resize()` is what has no caller. The docs get this right; I9 is
  where the code comment does not.)
- `cli.py`'s "what is built today" enumeration verified claim by claim: step 1 config-digest half
  (`:10121`) with an accurate `harness_version` disclaimer (`:10031` — all seven `harness_version`
  sites in `cli.py` are run-start writers), step 2 (`:10240-10241`), step 3 (`:10214`), step 7
  (`:10218-10220`), steps 4/6/8 genuinely absent. `design-resume-step5.md:403-427`'s task table has
  no status column and cannot overstate.
- The `ResumeIncompleteError` message's "step 5 has no implementation… `phase_floor` already
  computes that floor" is an **honest disclosure, not a contradiction**: both halves answer the
  operator's real question (did this run demote? no) and the second names exactly what is missing.
  See m4 for the residual, which is an implementer hazard, not an operator one.

---

## 3. Findings

### Important

**I1 — `tests/test_floor_rule_statements.py:433`, `:561`: Layer B/C parse the hard-stop set through a
closed four-name whitelist, so a *superset* clause and an *inverted ordering* clause both pass.**
Rewrite the canonical clause at all four census sites to `…which is a
DEGRADED/SKIPPED/RUNNING hard stop (…, tested *after* evidence…)`: the regex
`(DEGRADED|SKIPPED|SUCCEEDED|PENDING)` yields `{DEGRADED, SKIPPED}` == `_HARD_STOPS`, `_CENSUS`,
`_CLAUSE` and `_HARD_STOP_NAMED` all still match, and **all 12 cases pass** (verified). Both halves
are false: `reentry.py:107-108` breaks on `_HARD_STOPS` **before** the evidence test at `:109-110`.
*Failure scenario*: a reconciler implements the prose and moves the evidence test first. For a repo
whose BUILD row is `DEGRADED` and whose BUILD evidence does not hold, `phase_floor` then sets
`floor = BUILD` and walks below it — landing the re-entry floor **on** a `DEGRADED` phase, the one
outcome ADR-0077 §5 forbids, and re-running phases the operator's budget never granted.
*(`:428-430`'s "editing the prose to name a different set — or to drop one — fails here" is the
overclaim: drop fails, superset does not.)*

**I2 — `src/fleet/orchestrator/reentry.py:12-27` states the floor rule and is bound by nothing;
`21d6a87`'s fix is a narrower successor.** `21d6a87` enrolled `reentry.py` in `_CENSUS_FILES` with
`_EXPECTED_SITES["src/fleet/orchestrator/reentry.py"] == 1` — the `phase_floor` docstring at `:71`.
Measured: 5 sentences of the module docstring match `_WALK_SUBJECT`, **0** match `_STOP_CONDITION`,
so Layer D considers none of them. *Failure scenario*: edit `reentry.py:21` from "the walk may not
continue *below* one" to "the walk continues below one" — false against `:107-108`, no site count
moves, all 12 cases green; the next author implements the docstring and demotes every repo with an
excluded middle phase to `SCAN` on every resume, which is precisely the defect the sentence exists to
warn about. The file's own `:134-135` ("Layer D does now cover both — `cli.py` joined `_GOVERNED`")
conflates *swept* with *considered*. Also stating the rule and ungoverned:
`docs/superpowers/plans/design-resume-step5.md:250-253`, `docs/INTEGRATION_HONESTY.md:4134-4138`.

**I3 — line-anchored citations rotted in eleven places across three independent lanes, and one of
them is a mechanism's own failure message.** Three separate instances of one class:

*(a) `reentry.py`'s `_HARD_STOPS` break — seven sites.* `1e857f3` grew `phase_floor`'s
docstring by nine lines with no citation sweep. Verified at HEAD: `:98-99` is the **frontier** loop
(`if _status_of(rows.get(phase)) not in _SETTLED_FOR_DEMOTION: frontier = phase`); the `_HARD_STOPS`
break is at `:107-108`. Stale sites: `tests/test_floor_rule_statements.py:348`, `:369`;
`docs/DECISIONS.md:7525`, `:7597`, `:7663`; `docs/INTEGRATION_HONESTY.md:4134` (`:96-103`), `:4137`
(`:80-81`, introduced by `27cb03b` *as a measurement fix*). (`docs/DECISIONS.md:7663`'s companion
`:54` is correct — that really is `_HARD_STOPS`.) *Failure scenario*: `test_floor_rule_statements.py:369`
is **Layer D's failure message**. An author whose doc edit trips Layer D opens `reentry.py:98-99` as
instructed, finds the `_SETTLED_FOR_DEMOTION` test, and writes the sentence the message demanded as
"the walk stops at the first phase not in `_SETTLED_FOR_DEMOTION`" — which names `DEGRADED`/`SKIPPED`,
so `_HARD_STOP_NAMED` and the census both pass, while the rule now says a `PENDING` phase below the
frontier ends the walk. A reconciler implementing that returns the frontier's predecessor for every
repo and never demotes past an unstarted phase.

*(b) ADR-0082's verification table — three sites, off by exactly 12 lines.* `4ff8157` wrote
`repository.py:1410` (`span`), `:1435` (`if demotions:`) and `:1440` (the `DEGRADED` carve-out)
correctly; `704e52f` then added exactly 12 docstring lines ("behaviour unchanged — docstring only").
At HEAD those are `:1422`, `:1447`, `:1452`. *Failure scenario*: `:1435-1441` now spans the
**demotion** loop, whose filter is `if rows.get(phase) is not RepoStatus.SUCCEEDED` (`:1436`). An
author checking the ADR row "the carve-out names `DEGRADED` only — `SKIPPED` is absent from the
filter" lands on that filter and adds `SKIPPED` to it, silently changing which phases get demoted
while `test_a_skipped_phase_keeps_its_status_but_still_loses_its_checkpoint` stays green (it asserts
the checkpoint, not the status filter).

*(c) `src/fleet/sandbox/container.py:206`* cites `cli.py:10608` for the `reap()` call that makes its
backstop list real; `8df4af8` measured it correctly and `da70221`/`821277a`/`f94e488` then grew
`_reap_orphan_containers`'s docstring above it. The call is at `cli.py:10643`; `:10608` now lands
mid-docstring, on prose about `list_by_prefix`. *Failure scenario*: this is the exact citation an
author verifying "is `reap()` really reached?" follows — and finding 2's stale ledger entry (I12)
tells them it is not.

**I4 — `docs/INTEGRATION_HONESTY.md:4190-4192`: "the true count is **four statements**… which is
exactly the set the new test binds" is false at HEAD.** Introduced by `bf7206d` as a correction of a
previous miscount; falsified by `21d6a87`, which made `_EXPECTED_SITES` SPEC×2 + DECISIONS×1 +
`reentry.py`×1 (four prose) **plus** the `enums.py` paraphrase — five. `:4174` likewise still says
"the **three** prose copies". *Failure scenario*: `_EXPECTED_SITES`'s own failure message instructs
the author to "update `_EXPECTED_SITES` in the same change". An author reconciling against this
register counts five, reads "the true count is four… exactly the set the new test binds", and drops
`src/fleet/orchestrator/reentry.py` as a site that crept in — reopening the exact blind spot
`21d6a87` closed (all nine cases passed while `phase_floor`'s own docstring carried the retracted
"earliest" quantifier).

**I5 — `docs/INTEGRATION_HONESTY.md:4227-4229` records as "a separate, **currently open** defect" a
defect fixed 87 seconds later.** Written at `96cf597` (11:46:58); `821277a` (11:48:25) made the
refusal read "Steps 4…, 6… and 8… are absent too" with step 2 explicitly named as **not** absent
(`src/fleet/cli.py:10088-10089`), pinned by `tests/test_cli.py:2159`
(`…does_not_call_step_2_absent…`, asserting `"2 (" not in absent`). *Failure scenario*: this register
is the open-defect backlog. A round-D author working it greps the refusal, finds no step-2 clause,
treats the register as authoritative over the code, and either re-adds step 2 to the absent-step
enumeration (tripping `test_cli.py:2159`) or spends a lane re-fixing a closed defect.

**I6 — `tests/test_findings_kinds.py` has four undisclosed silent-miss classes and one false
residual.** All reproduced with synthetic injection:
- **(a) `:159-161` / `:133`** — a `/* */` comment between `INTO` and `findings` is invisible to
  *both* instruments (`/*` is outside the text class `[\s'"()+\\]`; `INTO\s+findings` fails after
  whitespace normalisation). Emitting `SynthSqlCommentV1` that way: `unresolved=0`, kind absent, all
  four tests pass. Residual 1's premise ("SQL whose source text never spells the phrase") does not
  cover it — here the source text *does* spell it.
- **(b) `:241-253` `_kind_slot`** — a non-`?` VALUES slot is taken as the kind literal. Named-parameter
  style `VALUES (:run, :repo, :kind, …)` emits the kind `':kind'`. Test 2 fails with the *wrong name*;
  an author who "fixes" it by adding `':kind'` to both listings silences that site permanently.
- **(c) `:501` `_worker_findings`** scopes by `path.parent.name != "workers"` while `:491-493` claims
  "Scoped to `src/fleet/workers/`". `src/fleet/workers/adapters/extra.py` appending a finding kind is
  invisible: 0 unresolved, 0 unlisted.
- **(d) `:90-93` `_INDIRECT_SITES`** is the hand-maintained exemption `CLAUDE.md` warns about, keyed by
  `(basename, normalised SQL)` — a *blanket*, not a per-site pass. It already matches two sites; a
  third `cli.py` site pasting the same SQL with an unrelated variable `kind` is silently exempted, and
  `path.name` means any future module named `cli.py` inherits the exemption.
- **(e) `:42-43` residual 2 is false**: "Every form the resolver does not understand returns
  'unresolved' and trips property 3" — `_kind_slot` short-circuits at `:252` and `_resolve` is never
  reached for (b).

**I7 — `tests/test_instruments_are_armed.py`: three green-while-false shapes, none disclosed.**
(i) assignment-form override (`:154-159`): rewrite `tests/test_workers_base.py:150`'s `UnitWorker.run`
as `run = _scripted_run`, then rename `BaseWorker.run` — `checked=0, orphans=[]`, the override is dead
and this file is green. (ii) a `def` wrapped in `if sys.version_info >= (3,12):` or `try:` inside a
class body (`:154` iterates `node.body` shallowly) leaves coverage silently — a natural,
non-adversarial edit. (iii) duplicate `ClassDef` names (`:150`, `:224`): give either
`tests/test_budgets.py`'s two `_NullExecutor`s or `tests/test_llm_backend_bedrock.py`'s two
`NoEffortTarget`s a base and a sibling fake vouches for a real orphan; the same flaw exempts *both*
classes for one `NOT_OVERRIDES` key. Separately, `:202-205`'s setattr gate matches
`endswith("setattr")` with a `raising=False` keyword, so bare builtin `setattr(worker, "_drain",
fake)` — which never raises on a rename, the exact silent kind the test claims to exclude — passes,
as does `raising=bool(0)` and `raising=RAISING` (all measured).

**I8 — the round's green-suite record does not anchor at HEAD, and `docs/PROGRESS.md` open item 14
understates the gap at its own commit.** The result is anchored (honestly, as inferred) at
`0559f2c`. Item 14, written at `94d3778`, says "**8 further commits have landed since**: current tip
`6b5d287`" — but `git rev-list --count 0559f2c..94d3778` is **11**, and two of the three it omits
touch code: `1e857f3` (`src/fleet/orchestrator/reentry.py`) and `21d6a87` (`tests/test_cli.py`,
`tests/test_floor_rule_statements.py`). *Failure scenario*: a round-D author reads item 14 —
explicitly the document's largest-unverified-claim register — concludes HEAD is suite-verified, and
skips the re-run. `1e857f3` edited precisely the docstring `test_floor_rule_statements.py` Layer A
**byte-compares** (336 normalised characters) and `test_cli.py` asserts phrases from. Scoped evidence
exists for `test_floor_rule_statements.py` (12 passed at HEAD); `tests/test_cli.py` at HEAD has no
recorded run.

**I9 — `src/fleet/orchestrator/budgets.py:1146` is a live, false claim this round edited and did not
correct.** `"""The limiter every `ModelClient.complete` on this tier must hold."""` — the sole
acquisition in `src/` is `src/fleet/workers/classify.py:162`; neither `llm/client.py` nor
`llm/calls.py` acquires anything. The disclosure exists
(`docs/superpowers/plans/design-resume-step5-rl1-limiter-report.md:181-186`, "true of one of five
callers… §5 assigns that sweep to R4") but sits in a plan doc, not beside the code, and this round
rewrote the sentence (`semaphore` → `limiter`) without fixing the falsehood in it. *Failure
scenario*: the R5/AIMD implementer reads `for_tier`'s docstring while wiring `resize()` to the 429
signal, concludes every LLM call is bounded by the tier ceiling, and ships a controller that halves a
limiter three of the five LLM paths never touch — leaving §13 row 43's named disaster (throttling
misread as `DOWN`) live while the ADR records it closed.

**I10 — the D55 premise correction was swept in `INTEGRATION_HONESTY.md` and not in the two documents
that *scope future work*.** `53e3d47` corrected "`asyncio.Semaphore` has no resize API" in place at
`docs/INTEGRATION_HONESTY.md:3411`; three mirrors survive: `docs/PROGRESS.md:5835` (§38's
**next-work** list — "…and `asyncio.Semaphore` cannot be resized"), `docs/PROGRESS.md:5575`, and
`docs/superpowers/plans/ledger-sdd-backlog-b.md:202`. This is the mirror-image direction: the object
at that acquisition point **is** resizable now. *Failure scenario*: the lane that picks up "§13 row
43 — rate limiting (LARGE)" scopes from §38's next-work list — that list's stated purpose — budgets a
task for making the ceiling resizable, and either re-implements `ResizableLimiter` or re-opens
ADR-0083's explicitly rejected "swap the semaphore object" option. Work already landed at `a3ff0ae`.
(`docs/PROGRESS.md:5573-5575` also carries the stale "1 of 12 workers" figure that §39 item 12
re-measured as 1 of 5.)

**I11 — cross-lane: the demotion entry point is stated two ways inside one file, and the wrong way
is the sentence a new caller reads first.** `src/fleet/models/enums.py:70-71`, attached to
`RESUME_DEMOTE` itself: "A demotion … **is reachable only through `transition(..., resume=True)`**".
Twenty-three lines below, `:134`: "**DO NOT** pass `resume=True` here. Call `demote()` instead: this
function returns the status alone, so a demotion made through it emits NO `PhaseDemoted` finding and
is invisible to whoever reads the run. Nothing enforces that — it is a convention." Same instruction
at `src/fleet/state/repository.py:1356-1358` and `docs/SPEC.md:193`. The first sentence is
mechanically *true* — `demote()` calls `transition(old, PENDING, resume=True)` at `enums.py:168` —
but it names the wrong entry point in the one comment that authorises the write. Lineage:
`f02d124` + `b7fc5ec` corrected exactly this claim in SPEC Constraint 7 and fixed "the scoping root
so it cannot regenerate"; `8ea1881` then re-wrapped the surviving `enums.py` clause onto its own line
**without correcting it**, so the correction's own root-scoping lesson was defeated by a reflow one
lane over. *Failure scenario*: step 5 still ships no caller. The author who writes it opens
`RESUME_DEMOTE` — the map that authorises the write — reads line 71, and calls
`transition(SUCCEEDED, PENDING, resume=True)`. Every demotion in the fleet is then silent, with no
`PhaseDemoted` finding and every status assertion still green. That is precisely the failure
`f02d124` landed to prevent.

**I12 — `ContainerSandbox.reap()`'s "zero production callers" is still asserted in the honesty
ledger after `e915b93` landed the caller.** Stale: `docs/INTEGRATION_HONESTY.md:1583` ("which, with
`list_by_prefix`, has **zero call sites in `src/`**; §11.5 step 2's reaper is implemented and never
invoked") and `:3215` ("`grep -rn '\.reap(' src/fleet/ | grep -v test` is empty"). Corrected in the
same round at `src/fleet/sandbox/container.py:205-207` and `docs/PROGRESS.md:5242-5243`; `056b547`
swept the class but its diff carries no `reap` hunk in `INTEGRATION_HONESTY.md`. The real call is
`src/fleet/cli.py:10643`. *Failure scenario*: D32's "would a test catch it? only an integration one"
rests on the named absence of a reaper. An author triaging container leaks reads `:1583`, concludes
the backstop is unreachable, and re-implements a sweep that already runs on every `fleet resume` —
or leaves D32 open on evidence that no longer exists. Compounded by I3(c), which points the
verification jump at the wrong line.

### Minor

- **m1** — `tests/test_floor_rule_statements.py:3-7` says the rule is "restated in full in **six**
  places" and then enumerates **seven** (SPEC×2, DECISIONS, `reentry.py`, `enums.py`, `cli.py`×2);
  `_RESIDUAL` item 6 in the same file says seven. A quantifier error in the file written to bind
  quantifiers.
- **m2** — `tests/test_findings_kinds.py:566`
  `test_every_findings_writer_in_src_is_one_this_module_resolved` asserts only `unresolved == ()`;
  `findings.append`-channel writers are writers it never enumerates, so the name over-claims.
- **m3** — `tests/test_instruments_are_armed.py:143`
  `test_no_test_subclass_defines_a_method_its_base_no_longer_has` and `:192`
  `test_no_setattr_in_the_suite_opts_out_of_pytest_s_existence_check` are both falsifiable while
  green (I7). `:214` `test_the_allowlist_has_not_gone_stale` is accurate — both directions really are
  checked.
- **m4** — `src/fleet/cli.py:10035` ("Steps 4, 5, 6 and 8 do not exist") is what `resume --help`
  renders; the richer, accurate disclosure lives only in the error message at `:10084-10088`. An
  implementer taking subtask 7 from `--help` may write a fresh inline walk in `_resume_impl` instead
  of calling `reentry.phase_floor`, re-deriving the earliest-vs-highest rule this round corrected six
  times.
- **m5** — `src/fleet/state/repository.py:830-836` (`_DEMOTE_FINDING_SQL`) duplicates the UPSERT
  string at `src/fleet/orchestrator/findings.py:125-131` verbatim, including the
  `ON CONFLICT (run_id, IFNULL(repo_id, ''), kind, fingerprint)` target. Both match
  `ux_findings_ident` (`schema.sql:335-336`) today; nothing enforces that they stay equal.
- **m6** — stale nouns after the `asyncio.Semaphore` → `ResizableLimiter` swap:
  `src/fleet/workers/base.py:323`, `src/fleet/orchestrator/context.py:126` and `:327` still say
  "semaphore"; `tests/test_workers_scan.py:188` still annotates
  `FakeLimits.for_tier(...) -> asyncio.Semaphore` (duck-typed, outside `packages = ["fleet"]`, so it
  passes; disclosed at `rl1-limiter-report.md:171-175`).
- **m7** — `tests/test_cli.py:1091`'s section header still reads "§11.5 — what `fleet resume`
  reconciles today (**steps 1, 3, 7**, --repoll-prs, --raise-budget)". `821277a` corrected the
  byte-identical phrase in `_refuse_unbuilt_resume_flags` and `f94e488` then scoped step 1 to its
  config-digest half in both `cli.py` sites; this fourth site was missed by both. An author adding
  reap coverage reads the header and files the test outside this block.
- **m8** — `src/fleet/workers/buildverify.py:1053` describes the deleted `list_by_prefix` in the
  **present** tense ("returns `[]` for a stopped daemon…"), written by `5ed4e47` while it still
  existed. `f10a863` deleted it; the sibling statements at `cli.py:10605` and `buildverify.py:1017`
  correctly say "was deleted"/"was". A reader greping the symbol cannot tell whether `:1053`
  documents a live lenient path still to be fixed or a closed one.

---

## 4. Areas I exercised and found clean

Stated plainly rather than padded into findings.

- **`ResizableLimiter`'s admission gate is correct.** I built an instrument that records every charge
  event with the ceiling live *at charge time* (`Watch.__setattr__` on `_borrowed`), then validated it
  three ways per `CLAUDE.md` guardrail 6: it fires **292/400 seeds** on a known-bad drain that does
  not re-read the ceiling (the pre-`99862a9` shape), and reads **0/400** on HEAD under random
  `resize` up *and* down plus random `Task.cancel`, and **0/400** under grow-only resizes where
  `borrowed > capacity` is impossible even transiently. Zero leaked slots and zero stranded waiters in
  all 800 seeds. (My *first* instrument reported 271/600 "over-admissions" and was invalid — it read
  the counter after a shrink, which is documented legitimate behaviour. Recording that here because
  the invalid version is the one that looks like a finding.)
- **The container reap predicate is right, and its "the CONTAINER half does work" claim is true.**
  Verified end to end: `_container_prefix` = `sandbox_name(run_id, repo_id, ctx.attempt) + "-t"`
  (`buildverify.py:351`), `ctx.attempt = phases.attempts + 1`, and `_live_sandbox_names`
  (`cli.py:10450-10466`) emits both `attempts` and `attempts + 1`, so `claims()`'s `-`-delimited
  prefix match covers every live container. The attempt-1-vs-attempt-10 boundary really is closed by
  construction (`0` is not `-`). The slug-collision over-spare is a **correctly** stated boundary, not
  a patched-over one — `CLAUDE.md` Rule 12's stop rule applied properly.
- **`_LIVE_SANDBOX_PREDICATE`'s NULL claim holds** (`cli.py:9928-9929`). `NOT (1 <predicate>)` is an
  exact set complement here because `heartbeat_at IS NOT NULL` leads the conjunction (so a NULL
  heartbeat yields FALSE, not NULL) and `heartbeat_ttl_seconds` is `INTEGER NOT NULL DEFAULT 300`
  (`schema.sql:440`), removing the only other NULL source. Step 2 and step 3 cannot disagree about
  which rows are live.
- **`demote_to_floor`'s UPSERT conflict target matches the expression index** `ux_findings_ident`
  (`schema.sql:335-336`), and the per-phase fingerprint (`repository.py:840-842`) genuinely prevents
  the three-phases-into-one-row collapse its comment describes.
- **D55's adjudication** (`docs/INTEGRATION_HONESTY.md:3405-3440`) is the standard the rest of the
  round should be held to: premise corrected in place, verdict unchanged, "0% closed" stated, the
  zero-caller `grep` re-run this session, and ADR-0083's own preference for *reverting* the primitive
  if R5 never lands recorded rather than left as an unused stand-in.
- **`CLAUDE.md`'s own citations resolve at HEAD**: `test_transition_demotes_without_writing_a_record_or_naming_a_new_sink`
  (`tests/test_state_models.py:711`), `TRANSITION_GLOBALS` (`:693`, asserted at `:762`),
  `tests/test_settings.py:589-593`, `src/fleet/models/tasks.py:100-102`. `2c8dc79`'s re-point was
  correct.
- **No unbuilt-subtask assumption anywhere in `src/`** (see §2).
- **Cross-lane checks that came back clean**: the dry-run vs `reap()` filter is *not* a duplication —
  `cli.py:10633-10636` imports `claims` deliberately and applies the same two filters, documented at
  `:10600-10602`; the conditional checkpoint sweep and its tests (`test_repository.py:1565`, `:1600`)
  agree with the code; the "`complete_phase` is the only writer of `phases.attempts`" claim holds
  (`repository.py:1311` is the only `attempts = attempts + 1` in `src/`); every `_wake_next` survivor
  is historical; all nine census sites of the earliest-vs-highest floor wording agree; D71's ledger
  status is correct.

### Cross-lane incoherence, summarised

Six sites where two lanes were individually correct and jointly wrong: **I11** (`enums.py` demotion
path vs three sites saying the opposite), **I12** (`reap()` "zero callers" swept in two files, left
in two others), **I3b** (`704e52f`'s docstring growth vs `4ff8157`'s ADR-0082 citations), **I3c**
(`da70221`/`821277a`/`f94e488`'s docstring growth vs `8df4af8`'s measured line), **m7**
(`821277a`/`f94e488` swept three "steps 1, 3, 7" sites and missed the test file's), **m8**
(`5ed4e47`'s present tense vs `f10a863`'s deletion). Every one is a *documentation* divergence —
I found no case of two lanes' **code** being jointly wrong, and no test pinning behaviour a later
commit changed.

---

## 5. Ranked list for round D

Grounded in what I found, not in what the documents say.

1. **Re-run the full suite at a quiet tree and stamp the SHA into `docs/PROGRESS.md`.** I8: the only
   whole-tree evidence anchors at `0559f2c`, three commits back, and two of the intervening commits
   touch `src/fleet/orchestrator/reentry.py` and two test files. Record the SHA directly this time —
   the current anchor is *inferred from commit timing*, which the document admits.
2. **Fix `src/fleet/models/enums.py:70-71` (I11) — the single highest-cost sentence in the tree.**
   It sits on `RESUME_DEMOTE`, it is the first thing subtask 7's author will read, and acting on it
   produces a fleet whose every demotion is silent with a green suite. One line, and it should say
   `demote()`. Sweep for the class while you are there: `8ea1881` reflowed it without reading it.
3. **Fix `budgets.py:1146` and sweep the D55 scoping mirrors (I9, I10).** These are the two findings
   that can cause a *wrong build* rather than a wrong document: one tells the AIMD implementer the
   gate is universal when it covers 1 of 5 call paths; the other tells the scoping lane to rebuild a
   primitive that already exists. Do both in one commit with an exact-match replacer, per
   `CLAUDE.md` guardrail 6's split-across-commits lesson.
4. **Sweep the rotted line citations as one class — eleven sites, three lanes (I3a/b/c).** One is a
   mechanism's own failure message actively pointing a reconciler at the wrong code. Then apply the
   real fix rather than re-measuring: **stop citing line numbers into `reentry.py` and
   `repository.py` at all** — cite `_HARD_STOPS`, the `break`, `span` and the carve-out **by symbol**,
   as `docs/DECISIONS.md:7663`'s `:54` companion effectively already does. Two lanes invalidated a
   citation this round by adding docstring lines and nothing else; a symbol anchor survives that,
   a line anchor cannot.
5. **Correct `docs/INTEGRATION_HONESTY.md:1583` and `:3215` (I12)** in the same pass — the ledger
   currently tells round D that step 2's reaper is never invoked, which is the load-bearing premise
   of an open defect (D32).
6. **Close I2 before anything else touches `reentry.py`.** Enroll the *module* docstring in Layer D
   (or fold its rule statement into the governed clause), and correct `:134-135`'s "covers both".
   Until then the file that binds the floor rule leaves the rule's longest statement unbound.
7. **Correct the two remaining honesty-ledger entries (I4, I5)** — both are backlog entries a round-D
   author will act on, and both instruct that author to *undo* correct work. Use the file's
   editorial-correction convention (dated in-file marker), not a rewrite, per `CLAUDE.md` guardrail 7.
8. **Harden the two mechanisms where the fix is cheap and real, and *disclose* the rest (I1, I6, I7).**
   Cheap and real: `_kind_slot`'s non-`?` slot (I6b) and `_worker_findings`' directory scope (I6c) are
   one-line fixes; `_INDIRECT_SITES`' blanket key (I6d) should carry site identity. The rest —
   assignment-form overrides, `if`-nested `def`s, SQL comments — should be **written into the
   residual lists**, not patched. `CLAUDE.md`'s stop rule applies: an honest disclosure beats a
   patched blacklist. Also fold I1's ordering half in: assert the *order* of the two tests in
   `phase_floor`, which is the only half of I1 with a real behavioural consequence.
9. **Then resume step-5 subtask 5 (`evidence_holds`) — the one input both landed units are waiting
   on.** Subtasks 1, 2, 3 and 6 are complete and caller-less by design; 5 is what turns them on. Do
   *not* start subtask 7 from `resume --help` (m4).
10. **Leave D72, D74 and D55 open.** All three are correctly recorded, correctly scoped, and none is
   ready: D72's fix needs `clone.py` and `context.py` to adopt `sandbox_name` plus a migration
   decision for worktrees already on disk; D74 is expected to be touched by that same fix.
