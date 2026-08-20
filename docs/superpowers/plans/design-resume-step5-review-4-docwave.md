> Promoted from `.superpowers/sdd/design-resume-step5/review-4.md` (round C scratch, lane CR-4), snapshot taken while `main` was at `1050e0a`. Examines the documentation-correctness wave: DOCFIX (`f02d124` `b7fc5ec` `4ad7c1f` `23a4396`) and HK17 (`c7f72c6`). Verdicts: DOCFIX FINDINGS — 1 Critical, 0 Important, 2 Minor; HK17 FINDINGS — 0 Critical, 2 Important, 1 Minor.

# CR4 — review of the documentation-correctness wave (read-only)

**Repo state.** `git status --short` at review start:

```
 M docs/superpowers/plans/design-resume-step5.md
 M src/fleet/cli.py
 M src/fleet/orchestrator/reentry.py
 M tests/test_reentry_floor.py
?? .superpowers/
```

`HEAD` was `c7f72c6` at start. Mid-review a sibling lane landed `4a1a184`, `8c00971`, `2c8dc79`,
`4ff8157`; at close `HEAD` = `4ff8157` and `git status --short` = ` M src/fleet/cli.py` + `?? .superpowers/`.
Every claim below is anchored at a named ref (`main` = the tip at the time of the measurement,
stated where it matters). No uncommitted edit of any lane is reported as a defect. Nothing was
edited or committed by this review.

**Method.** All dirty files read via `git show <ref>:<path>`. Sweeps run with a **wrap-aware**
detector (`scratchpad/sweep2.py`): each file read whole, every run of whitespace *and* the
string-concatenation characters `"`/`'`/`\` collapsed to a single space with a per-character
offset table retained, the pattern matched against the flat text, the offset mapped back to a real
line. That normalisation — not the whitespace-only one — is what sees `src/fleet/cli.py:10043-10044`,
where the claim is split across two adjacent string literals. Mutation work ran in a detached git
worktree (`scratchpad/wt4`, space-free path ⇒ private `BAZEL_ROOT` under `tools/bazel-test-root`,
removed afterwards, `git worktree list` clean). `PYTHONPATH=<wt>/src` was proven to win over the
editable install before any mutation result was read. Scoped pytest only, one session at a time,
never the full suite, no `FLEET_*` exported, `.venv/bin/python` throughout.

---

## Verdict

| Lane | Commits | Verdict | Critical | Important | Minor |
|---|---|---|---|---|---|
| DOCFIX | `f02d124` `b7fc5ec` `4ad7c1f` `23a4396` | **FINDINGS** | 1 | 0 | 2 |
| HK17 | `c7f72c6` | **FINDINGS** | 0 | 2 | 1 |

---

## C-1 (Critical, DOCFIX / `23a4396`) — the frontier fix asserts a property its own named mechanism does not provide, and omits `SKIPPED` entirely

**Site.** `docs/SPEC.md:6915-6918` (at `main`; verified still present at `HEAD` = `4ff8157`), text
written by `23a4396`:

> Step 5 therefore neither demotes a `DEGRADED` row nor searches past one: it is excluded from the
> frontier search itself, and it is not a `RESUME_DEMOTE` key, so `demote()` refuses it outright
> (ADR-0077 §5)

Two mechanisms are named. Neither delivers the "nor searches past one" half:

1. **`_SETTLED_FOR_DEMOTION` membership does the opposite of "excluded from the frontier search".**
   `src/fleet/orchestrator/reentry.py:47-52, 88-92` — the forward scan returns the first phase whose
   status is **not** in `_SETTLED_FOR_DEMOTION`. Putting `DEGRADED` in that set means the forward
   scan *passes over* a `DEGRADED` row rather than stopping on it; it is excluded from being
   *selected as* the frontier, which is not the same claim.
2. **`demote()`'s refusal raises; it does not stop a walk.** `enums.demote(DEGRADED, …)` raises
   `ValueError`. A search that reaches a `DEGRADED` row and calls `demote()` crashes the resume — it
   does not decline to search past it.

The property actually comes from a third constant the SPEC never names: `_HARD_STOPS`
(`reentry.py:54-58`, consumed at `:99-100`), which breaks the **backward** walk at `DEGRADED`
**or `SKIPPED`** without moving the floor onto it. Measured: `git grep -n "_HARD_STOPS" main -- docs/`
returns **zero** — the constant appears nowhere in `docs/`.

**And the `SKIPPED` half is missing altogether.** The SPEC's backward walk, unchanged by this
commit, reads "walk *backwards* asking `evidence_holds(repo, p)` … and stop at the first phase
whose evidence holds" (`docs/SPEC.md:6897-6900`). There is no stop rule for either hard status.
`23a4396` corrected the *frontier* definition to include `DEGRADED` and stopped there.

**Concrete failure.** A reconciler implements step 5 from the SPEC. Repo rows: Phase 1 `SUCCEEDED`,
Phase 2 `SKIPPED` (a config exclusion), Phase 3 `PENDING`. Frontier = Phase 3 (1 and 2 are settled).
Backward walk: `evidence_holds(repo, Phase 2)` — an excluded phase never produced durable evidence,
so `False` — floor moves onto Phase 2 and the walk continues to Phase 1, whose green `SUCCEEDED`
row is demoted. On every resume. `reentry.py`'s own module docstring (`:26-31`) names this outcome
and says the stop "is not a nicety: an excluded phase can never produce holding evidence, so a walk
that passed over it would demote every repo with an excluded middle phase all the way to `SCAN`, on
every resume."

**Why this is Critical and why it is the wave's own pattern.** This is the narrower-successor shape
the brief flagged, in the fix rather than in what it replaced: the remedy scoped itself to the
status it was reported about (`DEGRADED`), asserted the full property, and pointed the reader at the
one of two landed constants that does not implement it. `phase_floor` landed in `011b16d`, **before**
`23a4396`, so both constants were readable when the sentence was written. `docs/SPEC.md` is the
artifact the demotion writer reconciles against, and it names neither `phase_floor` nor
`demote_to_floor` anywhere (`git grep` at `main`: zero hits for both).

**Not a duplicate of landed work.** Siblings have since fixed the code (`4a1a184`, "SKIPPED stops
the backward walk exactly as DEGRADED does"), the plan pseudocode (`8c00971`) and written ADR-0082
(`4ff8157`). `docs/SPEC.md` §11.5 step 5 is untouched by all three — re-measured at `HEAD` =
`4ff8157`, the backward-walk sentence still carries no hard stop and still names only
`_SETTLED_FOR_DEMOTION`.

---

## M-1 (Minor, DOCFIX / `4ad7c1f`) — "Only the SELECTED profile's targets are checked" is measured false for §9 rule 5

**Site.** `docs/SPEC.md:6449-6452`, new text in rule 2: "**Only the SELECTED profile's targets are
checked**, so a profile naming an optional-extra backend costs an unrelated operator nothing…"

True for rule 2 (`settings._check_routing` loops over the selected profile's tiers only,
`src/fleet/settings.py:1396-1401`). **False for rule 5**: `_validate_models_config`
(`src/fleet/settings.py:1343-1361`) iterates `for profile_name, tiers in profiles.items()` — every
profile in the file — and raises `UnpricedTargetError`.

**Measured, not reasoned.** Extracted the §9 fence at `main` (`docs/SPEC.md:6341-6440`) to a scratch
config dir, appended an *unselected* profile whose first target declares no `price`, loaded with
`llm.profile=default`:

```
default: UnpricedTargetError exit_code=2 :: profiles.probe_unselected.HEAVY[0] (anthropic:x):
declares no `price` …
```

**Failure scenario.** An author adds a fifth illustrative profile to `config/models.yaml`, reads the
bolded sentence as licence to leave `price:` off targets nobody selects, and the harness exits 2 at
startup on a profile that is never used. Minor because the sentence's surrounding clause is about
backends and the shipped example is compliant; it is still a bolded, unqualified claim in a
five-rule list where one sibling rule contradicts it.

## M-2 (Minor, DOCFIX) — the "~103 raw matches" sweep figure does not reproduce

See "Sweep counts" below. Reported as Minor under Guardrail 6 (a number entering a report as a
measurement should be reproducible from the technique described). The **class-level** results all
reproduced; only the raw total did not.

---

## I-1 (Important, HK17 / `c7f72c6`) — "`grep -ni thinking src/ tests/` now returns zero" is false; it returns one, and the hit is HK17's own new line

**Claim.** `task-item17-report.md` §3, sweep table: "`grep -ni thinking src/ tests/` now returns
**zero** hits."

**Measured** (literal command, and independently with the wrap-aware detector over `src/ tests/
config/` at `main`):

```
tests/test_llm_cache.py:4:harness pins no sampling controls — no `temperature`, `seed`, `top_p`
or `thinking` key is built
```

Exactly **one** hit, and it is the replacement docstring `c7f72c6` itself wrote. The line is
**correct** — it is the code-backed negative enumeration, not the retracted premise — so there is no
tree defect; the defect is the certification.

**Failure scenario.** The next sweeper of this class runs the command the report certifies as
returning zero, gets a hit, and takes one of the two wrong branches: concludes the class regressed
(and re-opens a closed item), or "fixes" a true sentence — the mirror-image error this project names
explicitly. Guardrail 6's follow-on rule applies literally: the detector was not re-run against its
own fix, and the fix is what trips it.

The rest of the sweep reproduced. Pre-wave (`c7f72c6^`), `git grep -ni thinking -- src tests config`
returns exactly **one** live carrier (`tests/test_llm_cache.py:4`) — the one fixed. The five
left-alone sites were spot-checked and are correct, including the two most likely to be
"fixed" by a future grep: `src/fleet/llm/__init__.py:6` ("makes a re-run reproducible without pinning
a temperature" — asserts the cache achieves reproducibility *without* a pinned temperature, not that
anything forbids one) and `src/fleet/models/tasks.py:492` ("the harness pins no sampling controls
(§11.6)"). Mirror-image rule correctly applied at both.

## I-2 (Important, HK17) — the disclosed M1 limit is accidentally reachable, not adversarial-only, and the "only honest mechanism" dichotomy is false

**Verified first.** M1 reproduced in the detached worktree: deleting the two annotation lines from
`src/fleet/state/schema.sql:474-475` leaves `tests/test_llm_cache.py` at **19 passed**. The
disclosure is honest and accurate.

**Assessment against Rule 12's stop rule.** The rule asks whether a normal author would trip the
escape. Here no adversary is required at all: the defect that *created* open item 17 was precisely a
comment-only state — `schema.sql` carried no annotation while the SPEC listing did. Anyone
re-wrapping the `llm_cache` comment block, or trimming it in a future column addition, re-creates it.
That is accidentally reachable, and the stop rule says fix it rather than document it.

**The stated alternative is not the only one.** The report argues the only honest mechanised form is
a doc-listing extractor diffing the whole SPEC fence against `schema.sql`, and rejects it because
that would fire on 22 pre-existing intentional condensations (a measurement I reproduced — see
below). A third form exists and was not considered: assert the **marker** rather than the text — that
the `llm_cache` `effort` column region of `schema.sql` names `ADR-0075`. It catches deletion, is
immune to reflow and re-wrapping, does not compare the two listings, and touches none of the 22
condensations. Because a cheap mechanism exists, "the next drift of the comment alone will still be
silent" is a gap to close, not a boundary to state.

Reported, not fixed, per this review's charter.

## M-3 (Minor, HK17) — "passes any string-comparison binding" is overbroad

`task-item17-report.md` §2: "a `CHECK (effort IN ('low','medium','high'))` added to `schema.sql` —
the exact change the annotation exists to forbid — leaves the *comment* untouched and so passes any
string-comparison binding." True of a comment-only comparison; **false** of the obvious narrow text
binding, a normalised comparison of the `effort` *column-declaration line* between the SPEC fence and
`schema.sql` — under M2 that line becomes `effort TEXT NOT NULL CHECK (effort IN (…)),` in one file
and not the other, and the comparison fails. The conclusion (choose the semantic binding) is
unaffected and correct; the supporting argument is stronger than the fact. Minor: it lives in a
report, not in the tree.

---

## Sweep counts — measured vs claimed

### DOCFIX: claimed ~103 raw matches · 17 wrong · 13 fixed · 4 reported-not-fixed · ~86 left alone

Re-implemented the four patterns exactly as the report states them, run wrap-aware over
`docs/ src/ tests/ config/`:

| | pre-wave (`16879fe`) | post (`main`) |
|---|---|---|
| `transition(…)` within 120 chars of `emit\|writ\|record\|mint` | 49 | 55 |
| demotion-goes-through-`transition(` | 7 | 8 |
| D67 (`earliest phase whose … precondition`, `(lowest\|earliest) phase that is not`) | 13 | 13 |
| frontier-status (`not SUCCEEDED/SKIPPED`) | 1 | **0** |
| **total raw matches** | **70** | **76** |

**Raw total does not reproduce: 70 measured vs ~103 claimed.** The gap is a methodology gap, not
evidence of a missed site — the report's prose does not pin the window arithmetic or the
per-directory file filter, and a plausible variant of each pattern moves the total by tens. The
"13 fixed" figure is likewise not reconstructible: counting distinct claim-sites actually changed
across `f02d124`+`b7fc5ec`+`4ad7c1f`+`23a4396` gives **≥15** (Constraint 7; PROGRESS item 13; D68;
ADR-0077 §4 item 1; §4 item 4's citation; the test rename; the ledger note; the M-2 deletion; five
M-3 citations; M-4; the §11.5 frontier; ADR-0076 §1; the §9 example; `test_llm_roles.py`'s
docstring). The 17 = 13 + 4 arithmetic is internally tidy but does not map onto the diffs. Recorded
as Minor M-2 above.

**What did reproduce, and it is what matters:**
- The frontier-status class went **1 → 0**. Fixed.
- The D67 class is **13 on `main`**, of which **11** are labelled-historical or corrected-with-a-note
  doc quotations (`DECISIONS.md:6718/6818/7062`, `INTEGRATION_HONESTY.md:3861`, `PROGRESS.md:5349`,
  `design-resume-step5.md:180/184/192/433`, `ledger-sdd-backlog-b.md:489`, and the corrected
  `SPEC.md:6895`), and **2** are the known, deliberately-unfixed `src/fleet/cli.py` sites —
  `:10043-10044` (**wrapped**; invisible to a single-line grep, seen only by the quote-stripping
  normalisation) and `:10274`. Exactly the two the ADR-0076 note claims. Confirmed, not re-raised.
- `git grep -n "resume=True" main -- docs src`: **no site instructs a writer to call it.** The
  post-fix matches are the `DO NOT pass` warnings, `demote()`'s internal call
  (`enums.py:161`) and past-tense corrections. Reproduced.
- `git grep -n "runner.py:214-218" main` → **empty**. All five M-3 re-citations verified against the
  file: `runner.py:212` starts "`True` means…", `:213` starts "`False` means…", `:214` carries
  "**Neither verdict ever means \"skip the work\".**" alone. `DECISIONS.md:7050`→`212-214` (quote
  spans all three), `design-resume-step5.md:510`→`213-214` (quote is the `False` half + the bolded
  sentence), `INTEGRATION_HONESTY.md:3876` / `PROGRESS.md:5804` / `ledger:485`→`214` (each quotes the
  one sentence). All five correct.
- Old test name `…_or_reaching_a_sink` survives at exactly two sites at `main`: `CLAUDE.md:68`
  (known, routed — and since fixed by the sibling `2c8dc79`) and `ledger-sdd-backlog-b.md:1330`,
  which keeps it deliberately as round-B history with the second narrowing appended. No dangling
  citation.

### HK17: claimed 6 found / 1 fixed / 5 left alone; `grep -ni thinking src/ tests/` = 0

- **6/1/5 reproduces** in substance: one live carrier pre-wave in `src/ tests/ config/`, fixed; the
  remaining table rows are correct sentences correctly left alone (spot-checked, see I-1).
- **The zero-hits claim does not reproduce: measured 1.** See I-1.

---

## Rubric items discharged

**Rule 12 spot-check of a discriminating mutation (M2), reproduced independently.** Detached
worktree at `c7f72c6`, `PYTHONPATH` proven dominant. Baseline **19 passed**. Applied
`CHECK (effort IN ('low','medium','high'))` to `schema.sql:474`; `git diff --numstat` = `1 1` and the
mutated line printed, so the mutation demonstrably changed the file. Then:

| file | result under M2 |
|---|---|
| `old` = `tests/test_llm_cache.py` at `23a4396` | **18 passed** |
| `new` = `tests/test_llm_cache.py` at `c7f72c6` | **1 failed** (`sqlite3.IntegrityError: CHECK constraint failed`), 18 passed |

Old passes, new fails, same input, one clause. **Discriminating — claim upheld.** M1 also
reproduced (annotation deleted ⇒ 19 passed): the disclosed limit is real, and assessed in I-2.
Worktree removed; `git worktree list` shows only the primary checkout and the pre-existing
`wt-WT1-example`.

**HK17's binding-shape decision (22 hunks + the CHECK argument).** Fence extracted from
`docs/SPEC.md:3823-4616` (794 lines, exactly as reported) and diffed against
`src/fleet/state/schema.sql` (833 lines): **22 hunks** under plain `diff` (17 under `diff -u`, which
coalesces). **Reproduced exactly.** The rejection of a whole-listing text binding is sound: the
condensations are real and intentional (e.g. the SPEC renders "sha256 over the signatures RENDERED in
the prompt" where `schema.sql` has "in prompt"). The CHECK sub-argument is overbroad — M-3 above.
Direction of travel verified SPEC→code: the annotation in `schema.sql:474-475` is the SPEC's wording
at `:4254-4255`, re-indented to the column-51 gutter, and `docs/SPEC.md` is untouched by `c7f72c6`.

**Deliverable 4 — no gate was widened.** `4ad7c1f` touches `docs/SPEC.md` and
`tests/test_llm_roles.py` only; `src/fleet/settings.py` and `src/fleet/llm/` are not in the diff, and
`_check_routing`'s `if target.backend not in known_backends` gate (`settings.py:1401`) is byte-identical
to its pre-wave form. Re-verified by loading the extracted fence under `.venv/bin/python` with
`known_backends=sorted(discover())` = `['anthropic', 'openai_compatible']`:

```
default: OK (exit 0) · local: OK (exit 0) · mixed: OK (exit 0)
hosted_failover: UnresolvedReferenceError exit_code=2 ::
  profiles.hosted_failover.HEAVY[1]: backend 'bedrock' is not in the §7.7 registry
```

Independently reproduces DOCFIX's measurement. ADR-0078's regression is not present; rule 2's new
prose forbids it in the listing itself, which is the right place.

**Guardrail 7 — stale listings.** No listing in `docs/SPEC.md` of anything these commits changed
shows pre-change text. `SPEC.md:2084-2105` carries `transition()`'s docstring **verbatim** against
`src/fleet/models/enums.py:117-139`, `DO NOT pass resume=True here` included; `SPEC.md:2110-2118`'s
`demote()` docstring is a faithful condensation of `enums.py:141-158`, retaining the "demotes
SILENTLY; nothing enforces that it is not done" limit; `SPEC.md:2062-2069`'s `RESUME_DEMOTE` comment
matches `enums.py:61-73`. `SPEC.md:4254-4255` and `schema.sql:474-475` now agree on the `effort`
annotation. Neither `phase_floor` nor `demote_to_floor` is listed in the SPEC at all, so neither can
be stale — but the absence is what C-1 is about.

**Mirror-image check on the ~86 left-alone sites.** Sampled `SPEC.md:4139` / `schema.sql:359`
("`transition()` is THE single gate for every status write" — still true, `demote()` delegates to it
at `enums.py:161`), the ten labelled historical D67 quotations, ADR-0077 §4's conditional framing,
and HK17's five `temperature` rows. **No correct sentence was edited because it matched a grep.** The
one edit that touched an already-true sentence — `tests/test_llm_roles.py:43-45`'s "a realistic §9
`default` profile" — was true *before* `4ad7c1f` and made false *by* `4ad7c1f`, and was corrected in
that same commit. Guardrail 7 satisfied.

**Known items confirmed, not re-raised.** `src/fleet/cli.py:10043-10044` and `:10274` still ship the
rejected D67 predicate (measured at `main`; `:10043-10044` only via the wrap-aware detector) —
confirmed present, deliberately not fixed, queued to the owning lane. `CLAUDE.md:68`'s stranded
citation confirmed present at `main` and since fixed by the sibling `2c8dc79`. **HK17's
`CapabilityDrift` ruling is upheld, not refuted:** `git grep -n "CapabilityDrift" main -- docs/SPEC.md`
returns **9 lines** (11 occurrences), so the listing's omission is comment condensation, not a
semantic gap. Not a defect.

**Checked and found NOT to be defects** (recorded so they are not re-raised):
- `docs/SPEC.md:6910` "The finding is not optional" — reads as a normative obligation on the writer,
  which is exactly what ADR-0077 §4 keeps ("The audit obligation is a convention, deliberately
  reinforced"); it does not re-assert the retracted mechanical guarantee ("no call yields the
  demoted status without the record it owes"). Constraint 7's new sentence at `SPEC.md:187-190`
  likewise stops short of it and matches the landed
  `SqliteStateRepository.demote_to_floor` (`repository.py:1340-1370`) clause for clause, including
  "in the same `StateWriter` unit as the status change".
- M-2's deletion is sound: `assert result is RepoStatus.PENDING` strictly implies the deleted
  conjunct, so no mutation can fail the deleted line while the retained one holds.
- `src/fleet/llm/__init__.py:3-6` says "Four modules, one job each" and then names five. Real, but
  pre-existing, outside every commit under review, and outside the classes swept. Noted only.
