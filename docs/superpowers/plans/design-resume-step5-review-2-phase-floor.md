> Promoted from `.superpowers/sdd/design-resume-step5/review-2.md` (round C scratch, lane CR-2), snapshot taken while `main` was at `1050e0a`. Examines subtask 2 (`phase_floor`, `011b16d`) and ADR-0078 + gate test (`ddc16a6`). Verdicts: `011b16d` FINDINGS — 0 Critical, 4 Important, 3 Minor; `ddc16a6` FINDINGS — 0 Critical, 1 Important, 2 Minor.

# Review CR2 — audit of `011b16d` and `ddc16a6`

Read-only. Nothing in the working tree was edited by this review. Two detached worktrees were
created under the session scratchpad and removed; `git worktree list` is back to its prior two
entries.

## Working-tree state at the start of the review

```
$ git status --short          # main = ddc16a6
 M src/fleet/cli.py
 M src/fleet/state/checkpoints.py
 M src/fleet/state/repository.py
?? .superpowers/
```

At the end of the review `main` had advanced to `38107e9` (`16879fe`, `f02d124`, `5488157`,
`38107e9` landed from sibling lanes while this ran) and the tree had grown `M docs/DECISIONS.md`,
`M docs/INTEGRATION_HONESTY.md`, `M docs/PROGRESS.md`, `M docs/superpowers/plans/*`,
`M src/fleet/models/enums.py`, `M tests/test_state_models.py`, `?? tests/test_zz_oldprobe.py`.
**Every dirty file below was read at a commit ref, never from the working tree.** No finding here
is derived from an uncommitted edit.

## Verdicts

| Commit | Verdict | Critical | Important | Minor |
|---|---|---|---|---|
| `011b16d` — `phase_floor` | **FINDINGS** | 0 | 4 | 3 |
| `ddc16a6` — ADR-0078 + gate test | **FINDINGS** | 0 | 1 | 2 |

---

## Part 0 — what was verified and reproduced (no finding)

### Rule 12 spot-checks — **both implementers' mutation claims reproduce**

Both were re-run by me in **detached worktrees** at the respective commits, never in the primary
checkout. This is safe for a reason worth recording: `tests/conftest.py:216-224` keys `BAZEL_ROOT`
on `sha256(REPO_ROOT)`, so a worktree at a different path gets a **different** Bazel root and
`pytest_sessionfinish`'s reap cannot touch a sibling lane's output base. The worktree pattern is
not merely tidy here, it is what makes a concurrent pytest run safe at all.

**`task-2-report.md`'s naive-ascending mutation** (worktree at `011b16d`):

| step | result |
|---|---|
| baseline | `36 passed in 0.16s` — matches the report's 36 |
| mutation applied (frontier + backward walk replaced by an ascending scan of `evidence`) | `git diff --numstat` = `2 14` — the mutation **demonstrably changed the file**, it did not no-op |
| under mutant | `22 failed, 14 passed` — **exactly the "22 of 36" the report claims** |
| failures include | `test_fresh_repo_floor_is_scan_not_verify_under_naive_ascending_evidence` and `test_backward_search_stops_as_soon_as_evidence_holds` — the two named discriminators |
| revert | `git diff --stat` empty |

I ran a second, narrower mutation the report does not claim: deleting **only** the two-line
`DEGRADED` stop from the backward walk (`git diff --numstat` = `0 2`). Exactly one test fails —
`test_degraded_phase_is_a_hard_stop_it_is_not_demoted_and_search_does_not_pass_it`. That rule is
genuinely bound, and bound by the test whose name claims it.

**`task-adr0078-report.md`'s discriminating mutation** (worktree at `ddc16a6`):

| step | result |
|---|---|
| baseline | `3 passed in 1.16s` — matches the report |
| `cli.py:558` → `tuple(discover()) + ("bedrock",)`, `git diff --numstat` = `1 1` | file really changed |
| under mutant | `2 failed, 1 passed` — the **old** disjunction at `test_llm_backend_anthropic.py:662` **PASSES**, both new tests **FAIL** |
| revert | clean |

That is the literal Rule 12 shape: same input, old assertion green, new assertion red. The
implementer's stated reason for *not* using `+ ("bedrock", "vertex")` as the discriminator (it
reconstructs `SHIPPED_BACKENDS`, so the old assertion fails too and proves nothing) is correct.

### Guardrail 6 — every measured number in ADR-0078 re-measured, under `.venv/bin/python`

`env | grep -c '^FLEET_'` → `0` before every run; no `FLEET_*` was exported at any point.

| ADR claim | measured | verdict |
|---|---|---|
| `find_spec("anthropic")` found | found | ✅ |
| `find_spec("openai")` found | found | ✅ |
| `find_spec("boto3")` not found | not found | ✅ |
| `find_spec("requests")` not found | not found | ✅ |
| `find_spec("google")` **raises `ModuleNotFoundError`** | returns `None`, raises nothing | ❌ — finding F8 |
| `discover()` returns exactly two names | `['anthropic', 'openai_compatible']` | ✅ |
| `SHIPPED_BACKENDS` = four names | `('anthropic','openai_compatible','bedrock','vertex')` | ✅ |
| `cli.py:558 / :560 / :574` at `6a5e534` | `backends = tuple(discover())`; both `known_backends=backends` call sites | ✅ all three |
| `settings.py:107 / :1171 / :1401` | the constant; the `known_backends is None` fallback; `if target.backend not in known_backends:` | ✅ all three |
| `SPEC.md:5679-5685`, `:7188` (§13 row 36), `:6365-6371` (bedrock/vertex illustrative profile) | all present as described | ✅ |
| `tests/test_cli.py:985-995` skipif; `test_llm_backend_anthropic.py:662` disjunction; `test_llm_backend_bedrock.py:62` stub | all present as described | ✅ |
| ADR count 78; ADR-0078 at `DECISIONS.md:7135` | `grep -c '^## ADR-'` = 78; header at 7135 | ✅ |
| "No production path reaches the `SHIPPED_BACKENDS` fallback" | the only two `FleetSettings.load(` call sites in `src/` are `cli.py:559` and `:573`, **both** pass `known_backends=` | ✅ |

**Rubric item 3 — is the host-scoping stated honestly?** Yes, and twice. §2 says "Two is a property
of this host, not of the harness: install both extras and `discover()` returns four and nothing is
narrowed. The ADR records the *rule*, and two is today's measurement of it." §1 states the rule
("is this backend **registered**?" not "is this name spelled like one we ship?") independently of
the count, and `test_a_shipped_name_the_live_registry_lacks_is_refused_at_startup` **replaces
`discover` with a one-name registry precisely so the assertion holds on a four-extra host** — so
the test binds the rule, not the host measurement. This is correct and is the strongest thing in
either commit.

### Rubric item 2 — names asserting an absence

Checked every added test name against its body. All six absence-shaped names are backed:

- `..._never_a_superset` — the body asserts `set(passed) == set(discover())`, an equality, which is
  exactly the shape a superset fails. Reproduced under mutation 1 above.
- `test_degraded_phase_is_a_hard_stop_it_is_not_demoted_and_search_does_not_pass_it` — asserts
  `== VERIFY`, which is falsified by *either* half (returning `BUILD` = demoted, returning `SCAN` =
  searched past). Verified by the DEGRADED-stop deletion mutation.
- `test_missing_row_is_treated_as_pending_not_as_settled` — if a missing row read as settled the
  fixture would be all-settled and return `None`, not `TRANSFORM`. Detected.
- `test_requires_human_intervention_anywhere_yields_none_even_with_other_work_pending`,
  `test_backward_search_stops_as_soon_as_evidence_holds`, `test_fresh_repo_floor_is_scan_not_verify...`
  — bodies match names.

No finding under rubric item 2.

### Already-adjudicated items — confirmed, not re-litigated

- `demote()` having no caller: **now resolved by a sibling lane** — `16879fe` landed
  `SqliteStateRepository.demote_to_floor` as its first caller. Not a finding either way.
- `tests/test_llm_backend_anthropic.py:662` weak disjunction: **confirmed named in ADR-0078 §5**,
  quoted in full with the reason it is insufficient, and named again in
  `task-adr0078-report.md` §7 item 2. Not re-raised. (It is also confirmed weak by measurement: it
  passes under both of the ADR's mutations, as its own table says.)
- `docs/SPEC.md:186-187` Constraint 7: not re-reported. The additional sites of that same class are
  F7 and F10 below.

### Rubric item 4 — the narrower-overclaim follow-on

Searched ADR-0078 for a scope line that excludes the claim the ADR exists to make. Four scope lines
exist: "**Supersedes nothing.**"; "Nothing here should be cited as a SPEC requirement."; "this ADR
must not be read as having adjudicated that listing" (of `SPEC.md:6365-6371`); and the closing
"Not asserted here, and deliberately: `BackendReply.usage.model_id`…". Three of the four are
correctly scoped and exclude only things genuinely outside the ADR. The second is F9 below, at
Minor. **The most reliable form of this defect — a scope line that would neutralise the ADR's own
rule — is not present**; §1's rule statement and §5's binding test both survive every disclaimer.

### Sweep results that are correct and were NOT edited or flagged

`git grep "earliest phase whose"` at `main` returns 15 hits. Ten of them are **correct**: the
quotations inside `DECISIONS.md:6712/6809/7046`, `INTEGRATION_HONESTY.md:3861/3888`,
`PROGRESS.md:5349`, `design-resume-step5.md:180/184/415` and `ledger-sdd-backlog-b.md:489` are all
citing the *defective* wording as the thing being recorded or corrected. They must keep the old
words to remain true. Reported here as correct, per the brief's mirror-image warning.

`git grep SHIPPED_BACKENDS` over `docs/` and `src/` at `main`: no live site still claims the
constant is the gate. `ledger-sdd-backlog-b.md:34` describes the pre-fix state as the item's
subject and is correct as written.

---

## Part 1 — `011b16d` (`phase_floor`) — FINDINGS: 0 Critical, 4 Important, 3 Minor

### F1 — Important — `SKIPPED` gets no backward-walk stop, so the floor can land on, and search past, a phase the operator excluded by config
`src/fleet/orchestrator/reentry.py:83-86` (at `011b16d`)

`_SETTLED_FOR_DEMOTION` contains `{SUCCEEDED, SKIPPED, DEGRADED}`, so `SKIPPED` is settled **for
frontier location**. The backward walk then special-cases `DEGRADED` and nothing else. `SKIPPED` is
the other member of that set that ADR-0077 §5 declares non-demotable, in the same list and for a
stated reason: *"**`SKIPPED`.** A config exclusion. Resume does not re-decide the operator's
config."*

Failure scenario, executed against the landed function:

```
rows     = {SCAN: SUCCEEDED, TRANSFORM: SKIPPED, BUILD: SUCCEEDED, VERIFY: PENDING}
evidence = {SCAN: True, TRANSFORM: False, BUILD: False, VERIFY: True}
phase_floor(rows, evidence)  ->  Phase.TRANSFORM      # the SKIPPED phase itself

evidence = {SCAN: False, TRANSFORM: False, BUILD: False, VERIFY: True}
phase_floor(rows, evidence)  ->  Phase.SCAN           # searched straight past the SKIPPED phase
```

`demote(SKIPPED, ...)` raises `ValueError("SKIPPED is not a demotion: only SUCCEEDED may be
demoted…")` — verified by running it. The sibling lane's writer (`16879fe`,
`repository.py:1411`) happens to guard on `is not RepoStatus.SUCCEEDED`, so it will not raise; but
its checkpoint drop at `:1426` excludes only `DEGRADED`, so a `SKIPPED` phase in the span **does**
lose its `checkpoints` row, and re-entry begins at a phase the config excluded. In the second case
above the whole repo is demoted to Phase 1 because a `SKIPPED` phase can never produce holding
evidence — a resume that re-runs everything, every time, for any repo with an excluded middle
phase.

**And the behaviour is unbound in either direction.** I mutated the stop to
`in (RepoStatus.DEGRADED, RepoStatus.SKIPPED)` — i.e. applied the fix — and **all 36 tests still
pass** (`git diff --numstat` = `1 1`, so the mutation really landed). Nothing in
`tests/test_reentry_floor.py` reaches this: the 28-case table runs with `evidence` uniformly `True`,
which by its own comment "isolates the classification … from the backward-search mechanics", so the
walk never traverses a `SKIPPED` row. The one `SKIPPED`+`DEGRADED` test is the all-settled case,
which returns before the walk.

This is a decision that was not made rather than one made wrongly — the design doc's pseudocode has
the same gap. It needs either the stop or an ADR-0077-style "hazard handed forward" paragraph naming
it. Silence is what makes it a finding.

### F2 — Important — the docstring's conservative `evidence` default is documented and unbound
`src/fleet/orchestrator/reentry.py:66-68` (docstring) and `:85` (`evidence.get(phase, False)`)

The docstring states a contract: *"a phase absent from `evidence` is treated as not holding (the
conservative default: search further back rather than stop early)"*. The same docstring tells the
caller it need only supply *"`evidence_holds(repo, phase)` for phases below the frontier"* — so a
sparse mapping is not hypothetical, it is the documented calling convention for subtask 5.

Mutation: `evidence.get(phase, False)` → `evidence.get(phase, True)` (`git diff --numstat` = `1 1`,
verified changed). **36 passed.** Every fixture in the file builds a total mapping over all four
phases via `_all_evidence` or an explicit four-key dict, so the default branch is never taken.

Failure scenario: subtask 5 returns `{BUILD: False}` for a repo whose frontier is `VERIFY` — legal
under the documented convention. Correct code walks back to `SCAN`; a regression to a `True` default
stops at `BUILD` and leaves `TRANSFORM`/`SCAN` undemoted, so a repo whose scan artefacts are gone
re-enters at `BUILD` against a tree that no longer supports it. No test in the file distinguishes
the two. One case with a genuinely sparse `evidence` mapping closes this.

### F3 — Important — Guardrail 7: `docs/SPEC.md` §11.5 step 5 states a frontier rule the landed code contradicts
`docs/SPEC.md:6867` (present identically at `ddc16a6` and at `38107e9`)

> locate the lowest phase that is not `SUCCEEDED`/`SKIPPED`

The landed `_SETTLED_FOR_DEMOTION` is `{SUCCEEDED, SKIPPED, **DEGRADED**}`, per ADR-0077 §5's
explicit instruction that *"subtask 2's `phase_floor` must classify it accordingly"*. `DEGRADED`
appears nowhere in the §11.5 step-5 paragraph (`grep` over lines 6860-6885 finds only `SUCCEEDED`
and `SKIPPED`).

Failure scenario — this is the D67 shape exactly, one ADR later. A reconciler doing "make the code
match the SPEC" reads 6867, removes `DEGRADED` from the settled set, and a `DEGRADED` BUILD row
becomes the frontier. `demote_to_floor` then either demotes it (spending the stub-revalidation
budget through the side door with no round recorded — the precise harm ADR-0077 §5 exists to
prevent) or, through `enums.demote()`, raises `ValueError` and aborts the resume. The reconciler has
SPEC on their side and no test to stop them except
`test_degraded_phase_is_a_hard_stop...`, which they would read as the thing to change.

This is reportable per the brief's Guardrail-7 item and is **not** the known `SPEC.md:186-187`
Critical — it is a different sentence, in a different section, about a different predicate.
`docs/SPEC.md` is a live lane's file; **not edited**.

### F4 — Important — Guardrail 7: the design doc defines `held()` for `DEGRADED` and never uses it
`docs/superpowers/plans/design-resume-step5.md:214-226` (at `ddc16a6`)

```
settled(row)  :=  row.status in {SUCCEEDED, SKIPPED}
terminal(row) :=  row.status is REQUIRES_HUMAN_INTERVENTION
held(row)     :=  row.status is DEGRADED          # see ambiguity 5
...
    for p from frontier-1 down to 1:
        if not evidence_holds(r, p):  floor := p
        else:                         break
```

`held` is defined and then referenced by no line of the pseudocode; the frontier uses `settled`
(which excludes `DEGRADED`) and the backward loop has no `DEGRADED` stop at all. The implementation
is **stronger** than the doc it was built from — correctly, because ADR-0077 §5 requires it — which
means the doc now instructs the weaker algorithm. The `# see ambiguity 5` pointer is also dangling
inside this file: `grep -n "ambiguity 5"` over the doc returns only line 216 itself (the resolution
lives in ADR-0077 §5, under the name "design ambiguity 5"). Subtasks 3-10 of this same plan are
still being executed against this listing, so the next author reconciles against text that drops the
DEGRADED stop. Report only; not edited.

### F5 — Minor — the purity claim is scoped to direct imports; the module cannot be imported without executing `settings`, `sqlite3` and `vcs.git`

`task-2-report.md` claims *"Pure — no DB handle, `Git`, filesystem, or `settings` import anywhere in
the module."* True of `reentry.py`'s own import block. Measured transitively:

```
importlib.import_module("fleet.orchestrator.reentry")
  -> fleet.settings, fleet.state.db, sqlite3, aiosqlite, fleet.vcs.git,
     fleet.sandbox.container, fleet.orchestrator.runner  (47 fleet modules)
```

**In fairness this is entirely the package, not the module**: `fleet/orchestrator/__init__.py`
already imports `context`/`runner`/`scheduler`, and `import fleet.orchestrator` alone pulls the same
47. The delta attributable to `reentry.py` is exactly `['fleet.orchestrator.reentry']`. The
*function* is pure in the sense that matters — it reads only its two arguments and calls nothing but
`Mapping.get` and `Phase()` — and I found no path from its body to I/O. Recorded because the
rubric asks about transitive reachability and the honest answer is "the function yes, the import
no", not the unqualified claim in the report.

### F6 — Minor — `PhaseRow` is a runtime import used only in annotations
`src/fleet/orchestrator/reentry.py:34`

`from __future__ import annotations` is active and `PhaseRow` appears only inside annotations, so
this could be a `TYPE_CHECKING` import. As written it is the module's only structural dependency on
the state layer. Given F5 it changes nothing today; it matters if anyone ever tries to make this
module standalone-importable, which is the property the report claims for it.

### F7 (numbered under `ddc16a6`) — see Part 2.

### F10 — Minor — the "earliest phase whose evidence still holds" phrasing is off by one against the landed function; two sites beyond the known Constraint 7 one
`docs/SPEC.md:6865`, `src/fleet/models/enums.py:64`

D67 (`INTEGRATION_HONESTY.md:3861-3893`) records the fix as swapping *precondition* → *evidence*,
and both of these carry the corrected predicate name. But the clause is still off by one against
what `phase_floor` returns. `test_backward_search_stops_as_soon_as_evidence_holds` is the proof:
evidence holds at `SCAN` and `TRANSFORM`, fails at `BUILD`, and the floor is **`BUILD`** — the
earliest phase whose evidence does **not** hold. "The earliest phase whose evidence still holds"
names `SCAN`, one rung too low, which re-runs `TRANSFORM` and `BUILD` for nothing.

`SPEC.md:6865`'s own following sentences describe the traversal correctly ("stop at the first phase
whose evidence holds, because the phases below it are covered by it") — it is only the headline
clause that misnames the result, and `enums.py:64` carries the clause with no correction after it.
Minor rather than Important because the corrective prose sits immediately below one of them and
because the same clause in the Constraint 7 bullet is already a live lane's Critical; raised only
because the brief asks for *additional* sites and these two are additional. Report only; both files
were dirty at review end and were read at `ddc16a6` / `HEAD`.

---

## Part 2 — `ddc16a6` (ADR-0078 + `test_backend_registry_gate.py`) — FINDINGS: 0 Critical, 1 Important, 2 Minor

The ADR itself is the strongest document in this round: every one of its ~20 file:line citations
re-verified, the ref-anchoring discipline (`— **line numbers as of `6a5e534`**; a concurrent lane
had uncommitted edits … so cite the ref or cite the symbol") is exactly right and should be copied,
the host-scoping is honest in both directions, and its Rule 12 table reproduces. The findings below
are one sweep miss and two wording defects.

### F7 — Important — sweep for the class: `src/fleet/cli.py` still ships the D67 false claim in two user-facing error messages
`src/fleet/cli.py:10043-10044` and `src/fleet/cli.py:10274` (verified at `ddc16a6` and still at `38107e9`)

```
:10043  "phase's declared preconditions and demote each repo to the earliest phase whose "
:10044  "precondition holds — has no implementation. ..."
:10274  "(re-check preconditions and demote to the earliest phase whose precondition holds), "
```

This is verbatim the algorithm D67 recorded as a **design defect** and ADR-0077 §6 demolished — the
ascending scan over `preconditions_hold` that promotes a never-cloned repo to Phase 4, because
`rdepverify.preconditions_hold` returns `True` on a missing BUILD row. D67's "Fix, and its limits"
paragraph names `SPEC.md:180-182` and `SPEC.md:6777` as the sites it corrected; **these two `src/`
copies were never swept.**

Failure scenario, live today: an operator runs `fleet resume --repo X` and
`ResumeIncompleteError` / `UsageError` tells them, in the product's own voice, that step 5
"re-check[s] every phase's declared preconditions and demote[s] each repo to the earliest phase
whose precondition holds". That is the wrong algorithm and a wrong predicate; the next implementer
who reads an error message to learn what is missing builds the ascending walk. It also directly
contradicts `SPEC.md:182`, `ADR-0077 §6` and the module docstring `011b16d` just landed.

Worth flagging as a mechanism, not just a site: **`:10043` is line-wrapped**, so
`git grep "earliest phase whose precondition holds"` finds only `:10274` and a single-line sweep
would report the class as one-site-and-fixed. The wrapped copy is found only by grepping
`"demote each repo to the earliest phase whose"`. This is the exact trap CLAUDE.md §Guardrail 7
records from the cache-key comment.

Attribution: this is a *pre-existing* miss, not something `ddc16a6` introduced. It is filed under
this commit because `ddc16a6` is the commit whose brief called for the class sweep, and because the
ADR's own §3 argues from `§13 row 36`'s vacuity — the same family of "the shipped text describes an
algorithm nobody can run". `src/fleet/cli.py` is a live lane's file; read at a ref, **not edited**.

### F8 — Minor — Guardrail 6: one table cell in ADR-0078 §2 does not reproduce
`docs/DECISIONS.md` ADR-0078 §2, the `google` row

The ADR records `google` → *"**raises `ModuleNotFoundError`**"*. Measured under
`.venv/bin/python`:

```
find_spec("google")       -> None          (no exception)
find_spec("google.auth")  -> ModuleNotFoundError: No module named 'google'
```

The behaviour is real but belongs to `google.auth`, which is what the row's own "Imported by"
column cites (`llm/backends/vertex.py:58-59`, confirmed: line 58 is `import google.auth`). The
module *name* in the first column is the imprecise part. Failure scenario: a future author
re-running this table to check whether the host changed gets `None` for the cell that says "raises",
concludes the ADR's measurement has gone stale, and re-opens a settled question — or, worse,
"corrects" the row by asserting `find_spec("google")` raises, which never becomes true. The fix is
one word in the first column.

Everything else in that table and in §2's `discover()` claim reproduces exactly.

### F9 — Minor — the provenance paragraph's closing sentence disclaims more than it means to
`docs/DECISIONS.md` ADR-0078, Provenance paragraph, final sentence

The paragraph correctly splits policy from mechanism: the policy *is* SPEC
(`SPEC.md:5679-5685`, `:7188`), the mechanism (threading `discover()` through `known_backends=`, and
the equality shape of the test) is an Agent Recommendation. It then closes: **"Nothing here should
be cited as a SPEC requirement."**

Read literally, "nothing here" also covers the SPEC-grounded policy the same paragraph just
anchored two sentences earlier — i.e. the sentence disclaims the one claim in the ADR that *is* a
SPEC requirement. Failure scenario: a reconciler who wants to widen the gate back to
`SHIPPED_BACKENDS` (the widening F7's error messages would encourage) cites this sentence as the
ADR's own statement that its rule carries no SPEC authority. Narrowing it to "the mechanism above
should not be cited as a SPEC requirement" removes the reading. Minor because the intended
antecedent is recoverable from the preceding sentence.

---

## Method notes

- Both commits read via `git show <sha>` into scratch files; every dirty file read at a ref
  (`git show main:<path>` / `git show <sha>:<path>` / `git grep <ref>`), never from the working
  tree.
- Four mutations executed in detached worktrees under the session scratchpad, each proven to have
  changed the file with `git diff --numstat` before its result was read, each reverted and the
  worktree re-diffed clean. Worktrees removed; `git worktree list` restored.
- pytest sessions were scoped to node ids/files and run strictly one at a time, in worktrees whose
  `BAZEL_ROOT` digest differs from the primary checkout's, so no sibling lane's Bazel output base
  was reachable by `pytest_sessionfinish`. Full suite not run. No `FLEET_*` exported
  (`env | grep -c '^FLEET_'` = 0, checked).
- Nothing in the repository was modified by this review except the creation of this file.
