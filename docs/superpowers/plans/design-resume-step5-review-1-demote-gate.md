> Promoted from `.superpowers/sdd/design-resume-step5/review-1.md` (round C scratch, lane CR-1), snapshot taken while `main` was at `1050e0a`. Examines subtask 1, the `RESUME_DEMOTE` gate, commits `791b428..ebd1624`. Verdict: FINDINGS — 1 Critical, 1 Important, 4 Minor.

# CR1 — read-only review of `791b428..ebd1624` (DEM1, subtask 1: the `RESUME_DEMOTE` gate)

**Verdict: FINDINGS.** 1 Critical · 1 Important · 4 Minor.

---

## 0. Evidence of what was dirty when I read

```
$ git status --short
?? .superpowers/
```

The working tree was **clean** apart from the untracked `.superpowers/` scratch directory at
the moment of reading. No sibling lane's edits to `src/fleet/cli.py`,
`src/fleet/orchestrator/reentry.py`, `tests/test_reentry_floor.py` or `docs/DECISIONS.md` were
present in the tree, so every quotation below is committed state on `main`. `ebd1624` is an
ancestor of `HEAD` (`6a5e534`); `7a8bfbb`, the commit ADR-0077 anchors its citations to, is an
ancestor of the range base `791b428`.

Commits in range:

```
d0b1150 DEM1: ADR-0077 + SPEC §11.5 step 5 / Constraint 7 reconciled with the code
ea9ee57 DEM1 fix I-1: demote() refuses RUNNING/BLOCKED/PENDING, not just PENDING
e5b8b11 DEM1 fix I-2/minors: SPEC names demote(); ADR retracts the false guarantee
9d7160f DEM1 fix N-1/N-2/N-3: the tripwire asserts the silence it names
5e32e4d DEM1 fix N-1 (round 3): whitelist what transition() may name; bound the claim
ebd1624 DEM1 polish: the whitelist assertion now teaches the right fix when it trips
```

Files touched: `docs/DECISIONS.md`, `docs/SPEC.md`, `src/fleet/models/enums.py`,
`tests/test_state_models.py`. Note the brief names `tests/test_enums.py` as an artifact; **that
file does not exist** in this repo and never did — the tests landed in `tests/test_state_models.py`,
which is where the pre-existing `transition()` tests already lived. That is the right call
(§5 row 1 of the design doc named `tests/test_enums.py`, and the implementer correctly put the
tests beside the code they extend rather than creating a second home). Not a finding; recorded so
the next reader does not go looking.

I ran exactly one scoped pytest session, as permitted:

```
$ .venv/bin/python -m pytest tests/test_state_models.py -q
120 passed in 2.60s
peak 1.61 GiB (ceiling 6 GiB) · residual output bases 0 bytes · repository cache kept 1645 MiB
0 tests skipped this session — full collected coverage ran
```

No `FLEET_*` variable was exported at any point.

---

## Findings

### C-1 (Critical) — `docs/SPEC.md:186-187` still names `transition(..., resume=True)` as the demotion write, and falsely asserts that path emits the finding

`docs/SPEC.md:180-187`, the Constraint 7 bullet, **as it reads on `main` after the whole fix
sequence**:

> - **Resume validates evidence, never blind-replays** (Constraint 7): before re-entering a phase,
>   `fleet resume` re-checks each phase's durable evidence against SQLite + Git and demotes the
>   repo to the earliest phase whose evidence still holds (§11.5 step 5). **Two distinct
>   predicates, not one** (ADR-0077 §6): […] **The demotion write itself goes through
>   `transition(..., resume=True)` (`RESUME_DEMOTE`, §5.1) and emits a `PhaseDemoted` finding.**

Every other site in the tree says the opposite:

- `docs/SPEC.md:6873` — "by calling **`models.enums.demote()`** — never `transition(..., resume=True)`
  directly, which returns the status ALONE and would demote silently."
- `docs/SPEC.md:2094` (the §5.1 code listing) and `src/fleet/models/enums.py:127` — "DO NOT pass
  `resume=True` here. Call `demote()` instead […] emits NO `PhaseDemoted` finding."
- `src/fleet/models/enums.py:88-94` — `PhaseDemotion`'s `HONEST LIMIT` paragraph.
- `docs/DECISIONS.md:6905-6925` — ADR-0077 §4, which spends sixty lines on precisely this hazard.

The sentence is not merely stale wording; it is **factually false about the code**. `transition()`
(`enums.py:117-139`) returns a bare `RepoStatus`. It constructs no `PhaseDemotion`, and
`PHASE_DEMOTED_KIND` appears nowhere in its body. There is no input under which
`transition(..., resume=True)` "emits a `PhaseDemoted` finding".

**Failure scenario, concrete.** Subtask 6 is "the demotion writer",
`src/fleet/state/repository.py`. I verified that file calls `transition()` at **zero** sites today
(`grep -rn "demote(" src/ | grep -v models/enums.py` is empty; `RESUME_DEMOTE` appears only in
`enums.py` and as a re-export in `models/__init__.py:26,104`). Its author therefore starts from the
SPEC. Constraint 7 sits at `SPEC.md:180`, in §3's common phase contract — the canonical, top-of-
document statement of the very constraint they are implementing, and far more likely to be read
than §11.5 step 5 at line 6862. It tells them the write "goes through `transition(..., resume=True)`"
**and** reassures them that doing so "emits a `PhaseDemoted` finding". They write
`transition(row.status, RepoStatus.PENDING, resume=True)`, emit nothing, and every demotion in
`fleet resume` is silent — landed green work discarded with no `findings` row, invisible to whoever
reads the wave. That is D68's remedy defeated by D68's own SPEC edit.

ADR-0077 §4 names this failure in the imperative and then does not check for it:

> `state/repository.py` is subtask 6's file and calls `transition()` at zero sites today, so its
> author starts from the SPEC — and **if the SPEC named `transition(..., resume=True)` as the
> demotion path, they would call it, emit no finding, and demotions would go silent** […]
> 1. **`docs/SPEC.md` §11.5 step 5 names `demote()`** […]

The remedy was scoped to §11.5 step 5 and never swept to Constraint 7 — even though **the same
commit (`d0b1150`) rewrote both paragraphs**, and even though the commit subject says "SPEC §11.5
step 5 / **Constraint 7** reconciled with the code". This is CLAUDE.md Guardrail 7 verbatim ("'The
SPEC says X but the code cannot do X' is two edits, not one") compounded by Guardrail 6's sweep
rule ("Sweep for the class, not the reported site").

**This is also the answer to the brief's standing question about the last fix.** The
narrower-overclaim pattern is present, but it is not inside `ebd1624` — `ebd1624`'s four narrowings
are all accurate (verified in M-note below). It is in the **I-2 round (`e5b8b11`)**: the fix
narrowed its own scope to "§11.5 step 5" and left the sibling sentence, then the narrowed scope was
written up as complete.

**Remedy (do not apply — reported only):** `SPEC.md:186-187` should read that the demotion write
goes through `models.enums.demote()`, which returns the status together with the `PhaseDemotion`
the caller writes as a `PhaseDemoted` finding — never `transition(..., resume=True)`, which returns
the status alone. And the fix should be re-run against the detector that found it: `grep -rn
"resume=True" docs/ src/` must leave no site that instructs a *writer* to call it.

---

### I-1 (Important) — two debt records assert the C-1 remedy landed; both are false on `main`

Out of the reviewed range (both files were last edited in `e3cae3f`), but reported because they are
the artifacts a future reader consults to decide whether C-1 is already handled, and because
Guardrail 6 requires re-running a detector against the fix.

1. `docs/PROGRESS.md:5632-5634`:

   > 13. **`docs/SPEC.md` named `transition()`, not `demote()`, as the demotion path** — so the
   >     author of the demotion writer would have followed the SPEC, emitted no finding, and demoted
   >     silently. **Corrected on DEM1 and landed (`e5b8b11`).**

2. `docs/INTEGRATION_HONESTY.md:3921-3924`, inside D68's "**Addressed, landed**" paragraph:

   > `transition(..., resume=True)` remains public and would demote *silently*, so `SPEC.md` §11.5
   > step 5 names `demote()` and says so (`e5b8b11`) […]

Record 1 states the defect at exactly the right generality — "`docs/SPEC.md` named `transition()`,
not `demote()`, as the demotion path" — and then marks it **Corrected**. It is not corrected;
`SPEC.md:186` still does exactly what the sentence describes. Record 2 is technically true as
written (it claims only that §11.5 step 5 names `demote()`) but sits under an "Addressed, landed"
header whose scope a reader will take as the whole defect.

**Failure scenario.** A later reviewer or reconciler greps the ledgers for whether the
SPEC-names-`transition()` hazard is open, reads "Corrected on DEM1 and landed", and closes the
question. C-1 survives to the moment subtask 6 is written, which is the one moment it does damage.
A false "landed" label is worse than no label — Guardrail 6.

---

### Minor findings

**M-1 — `tests/test_state_models.py:706`: the test's *name* asserts an absence its body cannot
detect.** The name is
`test_transition_demotes_without_writing_a_record_or_reaching_a_sink`. ADR-0077 §4.2
(`DECISIONS.md:6969-6980`) enumerates **seven verified escapes** under which `transition()` reaches
a recording sink while this test stays green — a `dict` subclass whose `get()` appends to an
instance attribute, a `frozenset` subclass with a recording `__contains__` inside
`RESUME_DEMOTE[SUCCEEDED]`, `RepoStatus.__hash__`, `sys.setprofile`, a wrapper over the
`models/__init__.py` re-export, and two more. §4.2 and the docstring both state the boundary
honestly ("catches a side effect that introduces a **NEW name**"), and the test's own docstring at
`:730-739` repeats it. The **name** does not carry the qualifier, and the name is what appears in
`pytest -v` output, in `pytest -k` greps, and in the CLAUDE.md Rule-12 passage that cites this test
by name. CLAUDE.md: "When a name asserts an *absence* […] the name needs its own proof." Suggested:
`..._without_writing_a_record_or_naming_a_new_sink`. Low real risk — the docstring is thirty lines
of correction and the ADR is explicit — hence Minor, not Important.

**M-2 — `tests/test_state_models.py:746` is a non-discriminating assertion.**

```python
assert result is RepoStatus.PENDING                                  # :744
# ...a bare status, never a `(status, PhaseDemotion)` pair the caller could audit with.
assert isinstance(result, RepoStatus) and not isinstance(result, tuple)   # :746
```

`RepoStatus` is a `StrEnum`; no instance of it is ever a `tuple`, so the second conjunct can never
fail while the first holds — there is no mutation that distinguishes it. And the whole of `:746` is
subsumed by `:744`: under the "return a `(status, record)` pair" mutation, `result is
RepoStatus.PENDING` already fails. ADR-0077 §8 test 4 lists "a bare status returned" among the
pins; the pin is real, but it lives at `:744`, and `:746` earns nothing (Rule 12: "A test that a
mutation cannot distinguish earns nothing").

**M-3 — `docs/DECISIONS.md:7034` mis-cites `runner.py:214-218` for a quotation that spans
`runner.py:212-214`.** Re-measured at `7a8bfbb` **and** at `main` (identical): line 212 is
`bare bool at a call site: \`True\` means "the checkpoint describes…`, 213 is `re-enter for
\`remaining_units\` alone", and \`False\` means…`, 214 is `from its anchor". **Neither verdict ever
means "skip the work".**`. The cited range omits the first two thirds of the quoted passage and
adds 215-218, which the ADR does not quote. The citation was **inherited verbatim** from
`docs/superpowers/plans/design-resume-step5.md:492` rather than re-measured — Guardrail 6, "check
it, don't inherit it". The quotation itself is exact and the argument built on it is sound.

**M-4 — `tests/test_state_models.py:614-616`: comment and assertion disagree.** The comment ends
"…and adding a `RepoStatus` member enlarges this loop automatically," and the next line is
`assert len(elsewhere) == 5, "every non-PENDING target must be covered, not a chosen subset"`. The
loop does enlarge automatically, but the count assertion makes any new `RepoStatus` member a hard
test failure demanding a manual edit — the opposite of what "automatically" promises to whoever
adds the member, and the assertion's message does not tell them the count is the thing to update.
The design is right (a deliberate stop is exactly what you want when the enum grows); only the
prose overstates it.

---

## Rubric items checked and found sound — reported so they are not re-derived

**1. Spec compliance against `design-resume-step5.md` §5 row 1 and §6 — including *which line*
discharges the audit obligation.**

§6's recommendation reads (`design-resume-step5.md:456-459`):

> Take option B: a `RESUME_DEMOTE` map gated on a new keyword-only `resume: bool = False` parameter
> to `transition()`, **plus a `PhaseDemoted` finding written in the same `StateWriter` unit as the
> status change.**

The landed code discharges this as far as subtask 1 *can*, and the discharge is at two specific
lines:

- `src/fleet/models/enums.py:162` — `return new, PhaseDemotion(repo_id=repo_id, phase=phase,
  from_status=old, reason=reason)`. This is the line that binds the record to the status, so the
  path the SPEC names cannot yield one without the other.
- `docs/SPEC.md:6874-6876` — "the caller writes it as a `PhaseDemoted` finding **in the same
  `StateWriter` unit as the status change**, retaining `attempts` and dropping the phase's
  `checkpoints` row." This is the "same `StateWriter` unit" half, correctly handed to subtask 6,
  which owns `state/repository.py`.

No `findings` row can be written in this subtask — `demote()` touches no database — and the ADR
says so plainly in §7. That scoping is correct, not a gap. **The obligation is discharged, and C-1
is the one crack in the handoff**: §6 says "there is exactly one such caller (task 6's repository
method)", and `SPEC.md:186` invites that exact caller to take the other door.

§5 row 1's four success criteria all hold: `transition(SUCCEEDED, PENDING, resume=True)` returns
`PENDING` (`enums.py:136`); `transition(SUCCEEDED, PENDING)` still raises (`ALLOWED_TRANSITIONS`
`enums.py:47` is still `frozenset()`); `transition(RHI, PENDING, resume=True)` still raises
(`RESUME_DEMOTE` has one key, `enums.py:61-62`); and `tests/test_schema_sql.py:342` still derives
the CHECK domain as `expected = {s.value for s in RepoStatus}` — verified by reading, and safe by
construction because this range adds **no** `RepoStatus` member, so the DDL is untouched. (I did
not run `test_schema_sql.py`: the brief limits me to one scoped session and it was spent on
`test_state_models.py`.)

**2. Rule 12 — discrimination of each new/changed test.** Three of the four are genuinely
discriminating, with an old-passes/new-fails mutation each:

- `test_the_resume_door_opens_onto_pending_from_succeeded_and_nothing_else` (`:588`). Mutation:
  widen `RESUME_DEMOTE[SUCCEEDED]` to `frozenset({PENDING, SKIPPED})`. The **old** hand-written
  loop `for elsewhere in (RUNNING, BLOCKED, DEGRADED)` passes (SKIPPED was not in it) and the
  `.keys()` assertion above it passes; the **new** derived loop fails on `SKIPPED`. Discriminating.
- `test_demote_pairs_the_finding_and_is_stricter_than_transition` (`:622`). Mutation: revert
  `demote()`'s guard to the old `if old is RepoStatus.PENDING` (the I-1 defect). The **old**
  assertion — `pytest.raises(ValueError, match="illegal status transition")` on RHI only — passes,
  because RHI still raises from `transition()`. The **new** loop fails on `RUNNING` and `BLOCKED`,
  which reach `PENDING` through `ALLOWED_TRANSITIONS[RUNNING]` (`enums.py:36-40`) and
  `ALLOWED_TRANSITIONS[BLOCKED]` (`enums.py:41`) *before* the resume branch at `:136` is consulted.
  Discriminating, and the pair of positive assertions at the end (`transition(RUNNING, PENDING)`
  and `transition(BLOCKED, PENDING)` still legal) correctly proves the strictness is `demote()`'s
  own rather than inherited — so the mutation "tighten `ALLOWED_TRANSITIONS[RUNNING]` instead"
  is also killed.
- `test_transition_demotes_without_writing_a_record_or_reaching_a_sink` (`:706`). The whitelist
  inversion at `:749` is the strongest assertion in the range and the one the brief's "narrower
  overclaim" question most concerned. I checked its shape rather than trusting the ADR: the
  assertion is an **equality** (`set(transition.__code__.co_names) == TRANSITION_GLOBALS`), not a
  subset, so the whitelist is tight in both directions — a name removed from the body trips it too,
  and the session's 120-passed proves it is currently exact. Each of the six defeats of cut 2
  enlarges `co_names` and therefore fails it: `print`/`warnings.warn` are builtins/globals; a
  function-local `from fleet.models import base` puts its name in `co_names` via `IMPORT_NAME`; a
  `transition.audit` sink adds `transition`/`audit`/`append` (and is separately caught by
  `vars(transition) == {}` at `:757`); a `ClassVar` on `PhaseDemotion` adds `PhaseDemotion`/`log`;
  and a mutable-default `_audit=[]` is caught by `__kwdefaults__`/`__defaults__` at `:753-754`.
  Cut 2 passes all of these and cut 3 fails them — old-passes/new-fails, exactly as required.
  Its only defect is the *name* (M-1), not the body.

**3. Guardrail 6 — every number and citation in the range, re-measured.** All of the following were
measured by me, not taken from the ADR:

| Claim (ADR-0077 / SPEC) | Measured on `main` unless noted | Verdict |
|---|---|---|
| `pytest tests/test_state_models.py` → 120 passed | 120 passed in 2.60s | **exact** |
| `transition()` documented as "THE single gate…"; `enums.py:59-68` at `7a8bfbb` | `def transition` at `:59`, operator branch at `:66` at `7a8bfbb` | **exact** |
| `RepoStatus.SUCCEEDED: frozenset()` at `enums.py:45`, comment at `:48-49` (at `7a8bfbb`) | `:45` and `:48` at `7a8bfbb` | **exact** |
| `OPERATOR_REOPEN` at `enums.py:52`, `dict[RepoStatus, frozenset[RepoStatus]]`, RHI-keyed (at `7a8bfbb`) | `:52`, exactly that annotation, one key | **exact** |
| `ALLOWED_TRANSITIONS` routes `RUNNING -> PENDING` at `enums.py:35`, `BLOCKED -> PENDING` at `:39` (at `7a8bfbb`) | `:35` is the `PENDING` member of the RUNNING set; `:39` is the BLOCKED row | **exact** |
| `findings.kind` is free text by schema design — `state/schema.sql:253` | `:253` is `kind TEXT NOT NULL, -- FREE TEXT, deliberately` | **exact** |
| `test_schema_sql.py:342` derives the CHECK domain from `{s.value for s in RepoStatus}` | `:342` is that line | **exact** |
| `cli._note_finding` fingerprints on `(run_id, repo_id, kind)` and UPSERTs | `_note_finding` defined `cli.py:7084`; the hazard note at `enums.py:102-107` matches | **holds** |
| "§13 row 46 (ii)" corrected to "**§12 item 46 (ii)**" | item 46 is at `SPEC.md:7139`; §12 spans `:7088`–`:7144`; §13 starts `:7145`. The (ii) clause names `fleet resume` by name | **the correction is right**, and all four in-scope sites (`SPEC.md:2066`, `DECISIONS.md:7019`, `enums.py:69`, `test_state_models.py:591`) now agree. The two surviving `§13 row 46` citations elsewhere in the tree refer to the *Failure Modes* table row 46 (rewrite-rule conflicts) and are correct — **not** flagged, per the mirror-image rule |
| "**fifteen** implementations [of `preconditions_hold`], including **all four** phase composites" | 16 `preconditions_hold` definitions in `src/`; one is the abstract declaration at `workers/base.py:620`, leaving **15** implementations. `base.py:626` independently says "implemented by all eleven workers" — 11 + 4 composites = 15. The four composites are `cli.py:1112/3329/5062/5612` | **exact** |
| "ten of the fifteen return `False` precisely when there is nothing to resume" | Verified individually for 9 (the 4 composites all `return False` when `payload.remaining_units is None`; `classify.py:135`, `symbolindex.py:214`, `interrogate.py:279`, `clone.py`, `rewrite.py` — the last three corroborated by `runner.py:215-216`, which names `interrogate`, `symbolindex`, `rewrite` and `clone`). `relocate.py`/`buildverify.py` are the tenth depending on how one reads them | **in the right place; "ten" is defensible, not flagged** |
| "`rdepverify.preconditions_hold` returns `True` when the BUILD row is **missing**" | `workers/rdepverify.py:199-201`: `if row is None: … return True`, with the comment the ADR quotes | **exact** — the "promotes a never-cloned repo to Phase 4" argument holds |
| "`operator=True` is in the same position — `fleet retry`'s escape is itself still unwired" | `grep -rn "operator=True" src/` → one hit, and it is a docstring (`enums.py:123`). Zero call sites | **exact** |
| `runner.py:214-218` for the `preconditions_hold` contract quote | quote spans `:212-214` | **M-3** |

**4. Guardrail 7 — SPEC listings.** `docs/SPEC.md` contains exactly **one** `def transition(`
listing (`:2084`, §5.1 Enums) and one `ALLOWED_TRANSITIONS`/`OPERATOR_REOPEN` block (`:2032`,
`:2054`); there is no second, stale copy anywhere in the tree. The §5.1 listing carries the
post-change `transition()` docstring **verbatim**, including the load-bearing "DO NOT pass
`resume=True` here" warning at `:2094` — so the specific failure CLAUDE.md names ("one §5.1 listing
kept a pre-change docstring and so deleted the very warning the remedy rested on") did **not**
recur here. The listing abridges `PhaseDemotion`'s `HONEST LIMIT` docstring to a one-line comment
and paraphrases `demote()`'s, which is consistent with the listing's existing style (`def
payload(self) -> dict[str, object]: ...`) and loses nothing the remedy rests on. The §11.5 step-5
sentence at `:6862-6878` describes only things the code can do or that subtask 6 owns; I found no
sentence there the code contradicts. **C-1 is the sole Guardrail-7 failure, and it is in Constraint
7, not in a listing.**

**5. The open-item claim (`open-items-audit-round-b.md` item 8) — re-verified on `main`:
CONFIRMED, and correctly expected.**

```
$ grep -rn "demote(" src/ | grep -v models/enums.py     → (empty)
$ grep -rn "RESUME_DEMOTE" src/
  src/fleet/models/enums.py:61,124,136,146,155,158
  src/fleet/models/__init__.py:26,104              ← re-export only
$ grep -rn "resume=True" src/
  src/fleet/models/enums.py:66,89,123,127,161      ← :161 is demote()'s own call; rest are prose
$ grep -rn "PhaseDemotion\|PHASE_DEMOTED_KIND" src/
  src/fleet/models/enums.py:76,83,102,143,162
  src/fleet/models/__init__.py:25,40,103,141       ← re-export only
```

The gate is unreached. `models/__init__.py` is a re-export, not a caller, exactly as item 8 says.
Subtask 6 is the writer. **Confirming, not flagging** — and ADR-0077 §7 and
`INTEGRATION_HONESTY.md:3918-3921` both state it plainly, including the warning that it "must not
be read as shipped capability".

**6. Sweep results.** Beyond C-1's own sweep, I swept for: the `§13 row 46` mis-citation class (all
four in-scope sites corrected; two correct out-of-scope hits left alone); duplicate `transition()`
listings in `SPEC.md` (none); and any other site instructing a writer to use
`transition(..., resume=True)` (`SPEC.md:186` is the only one; `SPEC.md:2064` and `enums.py:65` say
the *map* is reachable only via that flag, which is mechanically true — **correct sentences, not
flagged**).

**7. On the brief's standing question — did the last fix narrow the overclaim again?** Checked
`ebd1624`'s four edits individually; all four are accurate narrowings, none introduces a new
overclaim:
(1) the `co_names` assertion message now tells a tripping author that widening `TRANSITION_GLOBALS`
is the wrong response — a real improvement, and the ADR does not claim it enforces anything;
(2) "accumulates nothing in ANY module-level container" → "…`dict`/`list`/`set`/`frozenset` (the
four types the snapshot helper filters to)" matches `_enums_mutable_module_state`'s
`isinstance(value, dict | list | set | frozenset)` exactly, and "logs nothing" → "logs nothing
through `logging`" matches what `caplog` can see;
(3) §4.2's narrowing from "added to `transition()`'s own body" to "introduces a **NEW name** into
`transition()`'s body" is correct, and its further claim that freezing whitelisted-global identity
would not close the gap checks out — I enumerated the seven escapes and confirmed that four of them
(the `frozenset` subclass inside a value, `RepoStatus.__hash__` as an argument type,
`sys.setprofile`, and the `models/__init__` re-export wrapper) leave every whitelisted global's
identity untouched;
(4) the test-file prose alignment is accurate.
**The pattern the brief predicted is present, but one round earlier: it is `e5b8b11`'s
scope-narrowing to "§11.5 step 5" that produced C-1 and I-1.**

---

## Summary

The mechanism itself is sound and the test that guards it is, as far as I can tell, the strongest
assertion in this repository — the whitelist inversion is the right shape of check and its stated
boundary is honest and verified. `demote()`'s strictness over `transition()` is real, correctly
motivated, and correctly proven to be its own rather than inherited. The ADR's retraction of its own
false guarantee is exemplary.

The single serious defect is documentary and is precisely the class CLAUDE.md warns about most:
**`SPEC.md:186-187` still points the next author at the door ADR-0077 exists to steer them away
from, and tells them that door emits the finding it does not emit** — with two debt records
asserting the correction landed.
