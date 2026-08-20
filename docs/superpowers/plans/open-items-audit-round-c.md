# Open-items audit — §39's "What is still NOT proven / left open"

**Audited against `main` = `1e78b9f`** (read-only; no edits to `src/`, `tests/` or any existing
document; **no pytest run**; no `docker` invoked; no `FLEET_*` exported).

`git status --short` at the start and end of this audit shows **only the untracked `.superpowers/`
scratch directory**. No lane had uncommitted edits at any point during it, so **nothing below
reports a sibling lane's work-in-progress as a defect**. In particular the lane the brief described
as editing `src/fleet/cli.py` and `tests/test_cli.py` *"right now"* had already landed: `da70221`
(the strings) and `e784573` (the bindings). Every `cli.py` reading below is of committed `main`.

**The tip has moved 12 commits past the `c24e7d2` anchor §39's amendment was measured at**
(`git rev-list --count c24e7d2..HEAD` → 12). Four of those twelve falsify something §39 records as
open. That is not a criticism of §39 — it disclosed exactly this and told the reader to check
`git log` first. This audit is that check.

## Classification of the 21

| # | Verdict | Evidence |
|---|---------|----------|
| 1 | **STALE (correctly self-closed)** | §39's own `87ed419` amendment marks it CLOSED and the closure is real: `_reap_orphan_worktrees` (`cli.py:10484`) and `_reap_orphan_containers` (`:10570`) exist and are called at `:10233-10234` under the `§11.5 step 2` comment at `:10214`. Tree clean. **Citation drift:** §39 cites `:10230-10231`; the calls are at `:10233-10234` after `da70221` added 18 lines above them. |
| 2 | **PARTIAL** | Principal claim closed, residual real. `wc -l tests/test_floor_rule_statements.py` → **511** (was 372); Layer D present at `:201`, `_EXPECTED_STOP_CONDITION_SENTENCES = 2` at `:234`, asserted at `:286`; `_RESIDUAL` item 4 exists and is itself pinned by an assertion at `:504-506` that fails if the scope note is deleted. The residual — Layer D is narrower than "every claim about the walk" — is **VALID and stated**. |
| 3 | **PARTIAL — and §39 has this one backwards at the tip** | **Defect half CLOSED**, not open. `grep -n "earliest phase whose precondition holds" src/fleet/cli.py` → **no match**; `grep -n "twelve workers" src/fleet/cli.py` → **no match**. Both were rewritten by `da70221`, bound by `e784573` (`tests/test_cli.py:1269`, `:2149` assert `"HIGHEST phase below the settled frontier" in result.output`). **Census half still VALID:** `_EXPECTED_SITES = {"docs/SPEC.md": 2, "docs/DECISIONS.md": 1}` (`:128`) — `grep -n "cli.py" tests/test_floor_rule_statements.py` returns **nothing**, so the two `cli.py` sites are still outside the census, exactly as next-task 1 required and the fix did not do. `grep -rn "@register_worker" src/fleet/workers/ \| wc -l` → **11**, re-measured. |
| 4 | **STALE (correctly self-closed)** | Verified independently: `grep -c '^## ADR-' docs/DECISIONS.md` → **84**, highest heading `ADR-0084`, and the consecutive-number gap scan over `grep -oP '(?<=^## ADR-)\d{4}'` prints **nothing**. Numbering is unbroken. |
| 5 | **PARTIAL, and it now carries a defect of its own kind** | The accurate parts re-verified: `D71` is genuinely unallocated (`grep -c '^\*\*D71\b\|^### D71\b'` → 0) and its `### D71 — UNUSED` record at `:4191` states the file's convention explicitly — *"a real record is a line beginning `**D<n> —` or `### D<n> —`"*. **But `D74`'s own heading is `## D74 — …` at `:4350` — two hashes.** By the register's own stated detector, `grep -c '^\*\*D74\b\|^### D74\b'` → **0**: the newest allocated defect is invisible to the instrument the entry immediately above it defines. Four "D71 is the next free number" sentences remain standing at `:3267`, `:4052`, `:4096`, `:4187`, as §39 says. Entries are also out of order (D73 `:4216` before D72 `:4295`). The "ten pre-existing defects still carry no numbers" clause I did not enumerate item by item. |
| 6 | **VALID** | `git grep phase_floor HEAD -- src/` and `git grep demote_to_floor HEAD -- src/` return only the definitions (`orchestrator/reentry.py:66`, `state/repository.py:453` Protocol + `:1340` impl), docstring prose, and — new since §39 — two occurrences inside the rewritten `ResumeIncompleteError` **string literal** (`cli.py:10081-10082`). A string is not a caller. Neither unit has one. |
| 7 | **VALID (actionable, conditional)** | Stated as a conditional boundary that flips to a defect the moment `--from-phase` (subtask 9) lands a writer that unsettles a phase beneath a completed one. The action it asks for — carry this into subtask 9's brief — is real work, so it belongs in the list rather than under Permanent limits. |
| 8 | **VALID** | `grep -rn "class FindingKind" src/fleet/` → **no match**; the enum that would remove the residual does not exist. `_worker_findings` is defined at `tests/test_findings_kinds.py:488` and used at `:417`, and the module's own `:50` states the narrower-instrument caveat. Unfixed and disclosed, as recorded. |
| 9 | **NOT-A-TASK** | A structural limit of a text-identity mechanism, stated by its own author "with no closure implied". Nothing to build. It sits in the backlog list while §39 *also* runs a "Permanent limits" section whose first entry ("a text-identity mechanism cannot distinguish a consistent lie from a consistent truth") is the same subject at a higher altitude. See "Structural limits" below. |
| 10 | **VALID** | The thrice-narrowed caveat is on `main` in both carriers, wrapped across lines: `src/fleet/state/schema.sql:286-288` and `docs/SPEC.md:4084-4086` both read `'UnmergedDependency' (no literal … settings.py:704 docstring)`. Found by whitespace-normalised read, not a single-line grep — the phrase spans a line break in both. Still no mechanism binds the DECLARED half. |
| 11 | **PARTIAL — the count is stale** | §39's amendment says *"Nine code reviews ran this round (`review-1.md` … `review-9.md`)"*. `ls .superpowers/sdd/design-resume-step5/ \| grep -i review` lists **`review-10.md`** as well — ten. The two findings §39 names as unverified (review 8's m-1, review 9's m-1) I also did not verify; neither is verifiable without a run. |
| 12 | **VALID — and its "1 of 5" re-measures exactly right** | `grep -rn "for_tier" src/fleet/` shows exactly one acquisition, `workers/classify.py:162` (`async with ctx.limits.for_tier(tier)`); the other hits are the definition (`budgets.py:1146`), config lookups (`settings.py:1258,1264,1504`), a construction (`budgets.py:1133`) and two comments. Neither `llm/client.py` nor `llm/calls.py` acquires anything (`calls.py:315` is a docstring). **Five** registered workers touch `ctx.llm` — see my own re-read below for the trap in that count. |
| 13 | **UNVERIFIABLE WITHOUT A TEST RUN** | The item's own text says so: all four questions need pytest or a real throttled endpoint. Not run, per the brief. Carried unresolved. |
| 14 | **VALID, and larger than the figure it records** | Re-measured from the round base to this tip: `git rev-list --count 6a5e534..HEAD` → **74** commits, **42** touching `src/` or `tests/`, `git diff --stat 6a5e534..HEAD -- src/ tests/` → **22 files, +5,495 / −69**. §39's amended figure was +5,022 / −42 over 20 files at 61 commits. **No full suite has been run against any of them** and none was run by this audit. Still the round's single largest unverified claim. |
| 15 | **PARTIAL — an accounting gap and a live cross-reference hazard** | Both flagged below in full. §38's open list has **17** items today; §39 accounts for **16** of them (13 carried + 3 closed). **Item 8 is dropped silently** — neither carried nor closed. The three closures §39 names are each correct against the current numbering, which I checked item by item. |
| 16 | **VALID** | The falsified measurement is still at `task-item17-report.md:110` (*"`grep -ni thinking src/ tests/` now returns **zero** hits"*) and is **marked, not edited** — the blockquote at `:119-120` records the nine-minutes-thirteen-seconds gap. Re-measured now: `grep -rni thinking src/ tests/ \| wc -l` → **1**, so the promoted zero is still false and the marker is still the right disposition. `task-mech-report.md` is at `.superpowers/sdd/design-resume-step5/` — **untracked scratch**, confirming "unpromoted"; its wrong count is at `:27` and `:130`, spelled *"sixteen"* rather than "16". |
| 17 | **STALE** | The ledger now records all three of the omissions this item names: `progress.md:604` *"RL1 lane: complete (a3ff0ae impl+tests; 431b02f one more test…)"*, `:665` *"FIX7 lane: complete (08ba8e2 D1, b1de36e D4, e4c1004 D3…)"*, and review 7 at `:641`, `:647`, `:682`. **Bounded:** this file is untracked scratch (mtime `2026-08-20 11:25:33`, after the last commit), so it changes outside git and I cannot show it was incomplete when §39 said so — only that it is not incomplete now, in the way described. The item's standing advice ("read `git log` first, the ledger second") remains sound regardless. |
| 18 | **VALID — all three legs re-verified in the code, not inherited** | **Registry:** `workers/clone.py:397-408` `_materialize_worktree` runs `git worktree add --detach` through `self._git(mirror, ctx)`, i.e. inside the per-repo **mirror**. **Name:** `orchestrator/context.py:206-208` `worktree()` returns `self.work_dir / repo_id` — no `fleet-<run_id>-` prefix, no attempt suffix. **Sole construction:** `grep -rn "WorktreeManager(" src/fleet/` → exactly one hit. `git log --oneline 6a5e534..HEAD -- src/fleet/orchestrator/context.py src/fleet/workers/clone.py` still returns **zero commits** — the two files the fix must change remain untouched by the entire round, now measured to the tip rather than to `87ed419`. **Citation drift:** the construction §39 cites at `cli.py:10466` is at **`:10472`**. |
| 19 | **VALID as amended (closed), one prose residual** | Closure verified in the code: `grep -rn "def list_by_prefix" src/` → **no match** (method deleted at `f10a863`); `workers/buildverify.py:1088` and `cli.py:10605`'s `--dry-run` branch both `await sandbox.list_with_verdict(prefix)`. The renamed pinning test is at `tests/test_sandbox.py`. **Residual the final sweep (`1e78b9f`) missed:** `workers/buildverify.py:1053` still reads *"The lenient `list_by_prefix` **returns** `[]` for a stopped daemon"* — present tense about a method that no longer exists. Historical narration, arguably; but it is the class `1e78b9f` was sweeping and it is the only present-tense survivor. |
| 20 | **VALID as recorded (closed)** | Both closures verified at the tip: `sandbox/container.py:196` now reads *"one general path and two conditional ones"* and enumerates against `src/` with the reason the old list was wrong; the m-2 pinning test exists at `tests/test_sandbox.py:1259` (`test_claims_spares_a_slug_colliding_repo_as_a_stated_boundary`). The item's own "what remains open is what I did not check" — review 8 m-1, review 9 m-1 — is still unchecked, by §39 and by me. |
| 21 | **VALID** | `_gc_impl`'s body contains no `reap`, `WorktreeManager` or `ContainerSandbox` reference (searched the function body, not the file). The only call sites of the two reap helpers in `src/` are `cli.py:10233-10234`, the resume path. `sandbox/worktree.py:56` still tells the operator `fleet-<run_id>-` is *"the glob `fleet resume` and `fleet gc` reap by (SPEC §11.5)"*. `fleet gc` reaps neither. |

### Counts

| Verdict | Count | Items |
|---|---|---|
| **VALID** | **11** | 6, 7, 8, 10, 12, 14, 16, 18, 19, 20, 21 |
| **PARTIAL** | **5** | 2, 3, 5, 11, 15 |
| **STALE** | **3** | 1, 4, 17 |
| **NOT-A-TASK** | **1** | 9 |
| **UNVERIFIABLE WITHOUT A RUN** | **1** | 13 |
| **DUPLICATE** | **0** | — |

**This is a markedly healthier list than §38's was, and for a reason visible in its own text.**
Round B found ~80% of §38's *carried* items stale. Here, the only three STALE entries are items §39
itself marks CLOSED (1 and 4) plus one whose subject is an untracked file (17). Nothing in §39 was
false when written that I could find. The staleness that exists is **twelve commits' worth**, and
§39 predicted it in prose twice and told the reader to check `git log`. The discipline round B
recommended — carry an item only with a re-verification note naming its anchor — is visibly in
force: per-item `AMENDED at <sha>` clauses, and an explicit statement that an item without one was
not re-measured.

---

## Items whose status differs from what §39 claims

**1. Item 3's defect half is closed; §39 says "still open".** `da70221` and `e784573` landed after
§39's anchor. The operator-facing predicate and the "All twelve workers" quantifier are both gone
from `cli.py`, and both replacements are bound by assertions in `tests/test_cli.py`. §39 could not
have known — it names `87ed419` as its anchor and these are 10 commits later. **The census half is
still open**, so the item survives, halved.

**2. Item 17 no longer holds.** The controller's ledger records RL1, FIX7 and review 7. Bounded as
above: untracked scratch, so this is a reading of a live file, not of history.

**3. Item 11's "nine reviews" is ten.** `review-10.md` exists in the lane directory.

**4. Item 14's figure has grown** from +5,022/−42 over 61 commits to **+5,495/−69 over 74 commits,
42 of them touching `src/` or `tests/`**, measured from the round base to this tip.

---

## New — found while auditing, not on §39's list

**A. `ResumeIncompleteError` now tells the operator that §11.5 step 2 is absent, after running it.**
`cli.py:10085-10086` reads *"Steps 2 (orphan **reap**), 4 … and 6 … are **absent too**."* But step 2
landed this round: `_resume_impl` executes the reap at `cli.py:10233-10234` and `resume()` awaits
`_resume_impl`, emits its result, and *then* raises this refusal. So the operator watches the
orphan sweep run and report, and is then told it does not exist.

This is **the round's own dominant failure mode, recurring inside the fix for it**: `da70221`
corrected the step-5 predicate and quantifier in this very string literal and left the step-2 clause
standing — a narrower successor to the claim it corrected. It is also the exact shape §39 item 3 was
about (a retracted claim surviving in the one surface an operator actually reads), and it is
**outside `_EXPECTED_SITES`**, so the census cannot see it either. Two related sentences at
`cli.py:10083-10084` naming what is "missing" are correct and should not be touched.

*Round D: this is a defect, not a stale doc line — fix it with the census-gap half of item 3, in one
change.*

**B. `D74`'s heading is invisible to the ledger's own detector.** `docs/INTEGRATION_HONESTY.md:4350`
opens the newest allocated defect with `## D74 — …`. The `### D71 — UNUSED` record at `:4191`
defines the file's convention as `**D<n> —` or `### D<n> —` and *uses that detector to prove D71
unallocated*. Run against D74 it returns **0**. A future "what is the highest allocated D-number"
check that follows the documented convention will conclude D74 does not exist and re-allocate it —
which is precisely the collision §39 item 5 exists to prevent, and the reason the brief warned about
reading a mention as a definition.

**C. The ledger still says the two `cli.py` strings are open.**
`docs/INTEGRATION_HONESTY.md:4181-4183` reads *"re-verified STILL PRESENT and STILL OPEN at
`f9cb3f9`"* and gives `:10078` / `:10390`. Both were fixed at `da70221`; `grep -rn "da70221" docs/`
returns nothing. §39's next-task 1 explicitly required the fixing lane to *"repoint the ledger's
citation in the same change"*. It did not. The surrounding sentence *"deliberately outside the new
test's `_EXPECTED_SITES` … That is a gap in coverage"* is still true and should stay.

**D. §39 item 15 drops §38's item 8.** §38's open list has 17 items. §39 carries 13
(1,2,3,4,5,6,9,10,11,12,15,16,17) and closes 3 (7,13,14) — **16**. The missing one is **§38 item 8,
"`fleet resume` is still refused, and the refusal names its own gaps"**, which names
`ResumeIncompleteError` at `cli.py:10041` and `_refuse_unbuilt_resume_flags` at `:10264-10266`.
It is neither carried nor closed, and it is **the item finding A lands on** — both strings it cites
are the ones `da70221` rewrote. Its substance survives (`ResumeIncompleteError` is still raised, at
`cli.py:10076`; the class is at `:362`) and its line numbers have all moved.

**E. Cross-reference hazard: `open-items-audit-round-b.md` numbers §38's list 1–20; §38's list is now
17 items.** After round B landed (`b754aac`), the three items it classified **NOT-A-TASK** — old 6
(`failover_triggers_recorded`), old 12 (the `effort` cache-key partition) and old 14 (DEM1's seven
adversarial escapes) — were hoisted out of the open list into §38's "Permanent limits" section, and
old 18 was rewritten as new 15. Verified by diffing the list at round B's anchor:
`git show 25af323:docs/PROGRESS.md` has 20 numbered items, `main` has 17, and the mapping is
old→new: 7→6, 8→7, 9→8, 10→9, 11→10, 13→11, 15→12, 16→13, 17→14, 18→15, 19→16, 20→17.

§39 item 15 cites the **new** numbering (correctly — I checked its three closures against it one by
one) while telling round D the items are *"carried on §38's own re-verification at `b754aac`"*,
i.e. pointing at round B's table. **A round-D reader who looks up "§38 item 10" in round B's table
gets row 10, which is a different item.** Every reference from 6 upward is off by one or more. This
is the "true text, moved evidence" class the brief asked me to flag, and it is the highest-leverage
one here because item 15 is the single entry that hands 13 items to round D.

*Round D: when re-auditing the carried thirteen (next-task 10), take the numbering from
`docs/PROGRESS.md` as it stands, not from round B's table, and note that three of round B's twenty
are now Permanent limits rather than backlog.*

---

## Structural limits, not tasks

Only **one** of §39's 21 is a limit rather than work: **item 9**, what
`tests/test_floor_rule_statements.py` cannot bind. Its own text says "stated by its own author with
no closure implied" — correctly labelled, wrongly located. It sits in a backlog list where a reader
scanning for work will read three concrete unbound behaviours as three tasks.

§39 already maintains a **"Permanent limits, recorded so they are not mistaken for backlog"**
section, and that section's first entry — *"a text-identity mechanism cannot distinguish a
consistent lie from a consistent truth"* — is item 9's general case. The two overlap in subject
without being verbatim duplicates: the limit states the property, item 9 enumerates three
consequences of it. **Item 9 belongs under Permanent limits.** Moving it makes §39's own separation
do the work it was built for, and it is why this audit's NOT-A-TASK count is 1 where round B's was 3.

Item 7 is the near miss worth naming: it *reads* like a limit ("a conditional boundary, not a
settled one") but carries a real action — get it into subtask 9's brief before `--from-phase` turns
it into a defect. It is correctly in the backlog.

---

## Duplicates

**None.** Item 15 is a carry-container for §38's list rather than a 22nd finding, and it declares
itself as one — the same shape round B classified DUPLICATE at §38's item 20. I have not repeated
that verdict here because its problem is not duplication but the accounting gap and cross-reference
hazard above, which are substantive. §39 explicitly does not re-list §38's items, which is the
improvement round B asked for.

---

## What my own re-read turned up

Re-read the draft against the code a second time before committing, per the mandate. **Three
corrections, two of them overclaims of exactly the narrower-successor kind this round has been
counting.**

**1. I nearly reported item 12's "five workers touch `ctx.llm`" as a miscount of six.**
`grep -rln "ctx\.llm" src/fleet/workers/` returns **six** files, and `contracts.py` carries
`@register_worker` at `:228`, so the arithmetic looked wrong. It is not: `contracts.py`'s single
`ctx.llm` occurrence is at `:10`, inside a module docstring, and reads *"so `ctx.llm` is **never
touched** here"*. A mention, and one that asserts the opposite of what a bare token grep implies.
**Five is correct and §39 is right.** This is the definition-vs-mention trap the brief named,
sprung on me by the very grep it warned about — and had I not opened the file, this audit's headline
finding would have been false.

**2. My first draft graded item 3 STALE.** It is PARTIAL. The strings are fixed but the
`_EXPECTED_SITES` census gap — which is what the item's own text is actually *about* — is untouched.
Grading it STALE would have deleted a live coverage gap from round D's inputs on the strength of a
fix to a different half. A narrower successor to the claim I was correcting.

**3. My first draft asserted item 17 was "false when written".** I cannot show that. The ledger is
untracked scratch with an mtime later than the last commit; I can show only what it says now.
Bounded to "does not hold at the state I read", with the mtime given.

Also corrected in passing: item 14's growth figure, which I initially carried from the
`c24e7d2..HEAD` diff (+656/−118, 8 files) — that is the *whole* diff including `docs/`, not the
`src/`+`tests/` delta the item measures. Re-measured from the round base with the item's own
pathspec: **+5,495 / −69 across 22 files**.

---

## Concerns

**1. Finding A is a live operator-facing falsehood and should be round D's first fix**, bundled with
item 3's census half so the census gains the site that would have caught it. It recurred *inside*
the commit that corrected the same string — which is the strongest available evidence that the
`_EXPECTED_SITES` gap is load-bearing and not bookkeeping.

**2. The §38 renumbering (finding E) will mislead round D silently.** Nothing in the tree records
that round B's table and §38's list no longer share a numbering. Next-task 10 sends round D straight
into it.

**3. Nothing in this round has been suite-verified — now 74 commits and +5,495 lines from the round
base.** Item 14 is right to call itself the largest unverified claim, and it grew by 13 commits
while this audit ran and the previous one was written. The queued full-suite run is the correct next
action and I deliberately did not touch it.

**4. Three of §39's items resolve only against untracked `.superpowers/` scratch** (11, 16, 17).
Those readings are not reproducible from git and will not be reproducible for round D if the
directory is cleared. Items whose evidence lives outside version control should say so where they
are written.
