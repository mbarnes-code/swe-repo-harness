> Round-D research artifact, produced by lane R3 (`design-resume-step5`), promoted unchanged from `.superpowers/` scratch by lane W9. **Live brief for subtask 8**: its §6 five costed decisions still await a controller ruling.

# R3 (round D) — source-verified brief for **subtask 8: §11.5 step 6 — recompute `blocked_by` and append the synthetic wave**

**Anchor.** Every finding below is measured at `HEAD = 4792b19f34228221afc82304c19c13b2064da4ed`
("docs/PROGRESS: re-anchor six rotted citations by symbol, close audit item 11, record D75 and D71"),
read from `git archive HEAD | tar -x` into this lane's scratch directory — **not** the working tree.
At the time of measurement `git status --porcelain` showed uncommitted edits from siblings in
`docs/DECISIONS.md`, `src/fleet/cli.py`, `src/fleet/orchestrator/reentry.py`, `tests/test_cli.py`,
plus an untracked `tests/test_reentry_evidence.py` (subtask 5 in flight). **`reentry.py` and
`cli.py` are moving under this brief**; treat every claim about them as "true at `4792b19`", and
re-verify at dispatch.

Runtime probes were run with `/home/redmage/swe repo harness/.venv/bin/python` with `src/` on
`PYTHONPATH` — the interpreter that will run the code. No pytest was run. No `FLEET_*` was exported.

---

## 0. Premises in the dispatch brief that measured **false** or need correction

| # | Premise as briefed | Measured at `4792b19` | Consequence |
|---|---|---|---|
| P1 | Design row 8's `graph/sequence.py:291 append_synthetic_waves` "exists and is the obvious answer" | The symbol exists **at exactly line 291** (the one design line citation this round that did **not** rot). But it is a **no-op for the entire subtask-8 population** — see §1.3. Verified by execution, not by reading. | Row 8's stated mechanism **cannot be used as-is**. This is the single most important finding in this brief. |
| P2 | `orchestrator/scheduler.py:255,411` | Both are off-by-one-to-two but land in the right symbols: `SqliteSchedulerStore.append_blocked_by` is `def` at **257** (255 is the last line of `blast_radii`); `WaveScheduler.propagate_blocked` is `def` at **410** (411 is its docstring's first line). | Resolve by symbol, as instructed; the numbers are near-misses, not rot. |
| P3 | Design §3 ambiguity 3: "the SPEC says so for `blocked_by` reversal (step 6), not for step-5 demotion … the SPEC does not authorise it here" | **Accurate, and still accurate.** But the brief's framing under-sells the other half: for **step 6 itself** — subtask 8's actual job — the SPEC authority is not merely load-bearing, it is *directly on point and unambiguous*. See §2. | Ambiguity 3 is a **step-5** problem that subtask 7 leaves on the floor and subtask 8 inherits. It is not a step-6 problem. |
| P4 | Row 8: "A repo whose blocking ancestor is now **SUCCEEDED** loses that entry" | Under-specified against the code. `blocked_by` entries are written by **three** distinct populations in the SPEC, only **two** of which have producers today, and the reversal condition for the second is *not* "ancestor is SUCCEEDED". See §1.2 and §3.3. | A literal implementation of row 8's criterion **silently un-blocks the dependents of every quarantined repo**. Highest-severity defect this brief found. |
| P5 | Implicit in row 8's "Files touched: `src/fleet/cli.py`" | There is **no** way to obtain a `WavePlan` at resume time. `record_plan` is the only writer, its only production caller is `_sequence_impl` (`cli.py:2756`), and **nothing in `src/` reconstructs a `WavePlan` from `waves`/`wave_members`**. | Row 8 is not an M. Either subtask 8 writes SQL directly against `waves`/`wave_members`, or it needs a new DB→`WavePlan` loader. Controller ruling needed — §6 option W. |

---

## 1. Q1 — What actually maintains `blocked_by` today

### 1.1 The write path, by symbol

Resolved by symbol (line numbers given for navigation only, at `4792b19`):

| Role | Symbol | File:line |
|---|---|---|
| **The one and only writer** | `SqliteSchedulerStore.append_blocked_by` | `src/fleet/orchestrator/scheduler.py:257` |
| Protocol declaration | `SchedulerStore.append_blocked_by` | `src/fleet/orchestrator/scheduler.py:125` |
| Policy wrapper (repo → its transitive dependents) | `WaveScheduler.propagate_blocked` | `src/fleet/orchestrator/scheduler.py:410` |
| Closure builder | `ordering_descendants` | `src/fleet/orchestrator/scheduler.py:429` |
| Production trigger 1 | `PhaseRunner._contain` (fires only on `RepoStatus.REQUIRES_HUMAN_INTERVENTION`) | `src/fleet/orchestrator/runner.py:1068` |
| Production trigger 2 | `fleet quarantine`'s impl — calls `append_blocked_by` directly, **not** via `propagate_blocked` | `src/fleet/cli.py:9789` |
| CLI test-double passthroughs | two `_Scan…`/`_Transform…` store wrappers | `src/fleet/cli.py:1390`, `src/fleet/cli.py:3592` |
| Ordering-pair source for the closure | `_ordering_pairs` (`edges` reversed, filtered by `graph.dag_edge_kinds` / `min_confidence` / `ordering_suppressed = 0`) | `src/fleet/cli.py:4059` |
| Reader (projection) | `projection.py:88` (SELECT), `projection.py:266` (`sorted(_json_list(row["blocked_by"]))`) | `src/fleet/state/projection.py` |
| Reader (scheduler gate) | `WaveScheduler.admit` — a `BLOCKED` member is diverted to `Admission.blocked` | `src/fleet/orchestrator/scheduler.py:369` |
| Model field | `RepoState.blocked_by: list[RepoId]` | `src/fleet/models/state.py:131` |
| Column | `phases.blocked_by TEXT NOT NULL DEFAULT '[]'` — *"JSON array of repo_id; set-union, reversible"* | `src/fleet/state/schema.sql:432` |

**Stored form.** A JSON array of strings in `phases.blocked_by`, **one row per (run, repo, phase)**.
Written as `json.dumps(sorted(names))`, read as `set(json.loads(...))`. Not a table, not a
normalised relation. There is no `UNIQUE` machinery — set-ness is enforced in Python by
`set(...).add(...)` in `append_blocked_by`.

**The critical negative result — measured, class result, two instruments.**

> **Predicate:** whole-file whitespace-normalised regex `blocked_by\s*=\s*\?` (offset→line mapped),
> over `src/**/*.py` + `src/**/*.sql`.
> **Result:** **exactly 1** assignment-shaped site in all of `src/` — the
> `"UPDATE phases SET blocked_by = ?, status = 'BLOCKED', updated_at = ? …"` inside
> `append_blocked_by` (`scheduler.py:280`).
> **Cross-check (different instrument, different question):** the unrestricted normalised token
> `blocked_by` occurs at **44** sites across `src/`; classifying all 44 by hand, none is a second
> writer — 2 are the column/field declarations, 1 is the SELECT in `append_blocked_by`, 1 is the
> projection SELECT, 1 the projection materialiser, 1 the `migration_state.json` emitter
> (`cli.py:9530`), and the remaining 38 are prose in docstrings/comments.
>
> **Class result: the class "code in `src/` that REMOVES an entry from `blocked_by`" has ZERO
> members.** `blocked_by` is append-only today. The column comment calls it "reversible" and SPEC
> §3.5 promises reversal; **nothing implements the reversal.** Subtask 8 writes the first remover
> in the harness.

Corollary the implementer must internalise: **there is no `remove_blocked_by` to extend, and no
"recompute" primitive.** Subtask 8 is a greenfield write path on the narrowest Protocol in the
codebase (`SchedulerStore`, whose docstring explicitly says it is "deliberately narrower than
`StateRepository`: a scheduler handed the full repository could insert symbols" —
`scheduler.py:104`). Adding the remover to `SchedulerStore` is the shape that respects Guardrail 3;
adding it to `StateRepository` is not.

### 1.2 What a `blocked_by` entry actually *means* — three populations, two producers

The dispatch brief and design row 8 both say "blocking ancestor". The source says three different
things:

1. **An RHI repo.** SPEC §3.5's propagation rule block (`docs/SPEC.md`, the indented `>` block
   beginning *"When `(repo r, any phase)` reaches `REQUIRES_HUMAN_INTERVENTION`…"*). Implemented:
   `runner.PhaseRunner._contain` → `propagate_blocked` → `append_blocked_by`. Reversal condition
   per SPEC: *"if `r` is later fixed and re-run to `SUCCEEDED`"*.
2. **A quarantined repo, which is `SKIPPED`, not RHI.** `fleet resume`'s sibling verb `fleet
   quarantine` "writes an audited `findings` row of kind `OperatorQuarantine` with the required
   reason, sets `RepoStatus.SKIPPED`, and propagates `blocked_by` to its dependents **exactly as an
   abandonment does**" (SPEC §10 command table, `fleet quarantine` row; implemented at
   `cli.py:9767` (the `SKIPPED` write) and `cli.py:9789` (the propagation loop)). **There is no
   reversal condition** — a quarantine is an operator decision resume may not re-take.
3. **A `FAILED` contract, whose `contract_id` (not a repo_id) goes into `phases.blocked_by`.**
   SPEC §3.5, the *"A failed contract propagates like a failed repo"* paragraph: *"setting every
   non-terminal `d ∈ D` to `BLOCKED` with the `contract_id` appended to `phases.blocked_by`, in the
   same transaction as the status write."*
   **NOT IMPLEMENTED at `4792b19`.** Predicate: normalised sweep for
   `ContractStatus\.FAILED|status\s*=\s*'FAILED'` over `src/**/*.py` + `src/**/*.sql` → 2 hits,
   both in `src/fleet/migrations/v007_logical_keys.py` and both about the *`phases.status`*
   `'FAILED'`→`'REQUIRES_HUMAN_INTERVENTION'` rewrite, neither about contracts. Class result:
   **0 producers of contract-sourced `blocked_by` entries.**

**Therefore `models/state.py:131`'s field description is FALSE as written**:

> "Set union of transitive ancestors in `RepoStatus.REQUIRES_HUMAN_INTERVENTION` — **that is the ONE
> status 'abandoned' names** …"

Population 2 is live in shipped `fleet quarantine` and writes `SKIPPED` ancestors into the same
column; population 3 is SPEC-mandated and would write non-repo ids into a field typed
`list[RepoId]`. I am read-only and have not edited it. Per CLAUDE.md §"Documents Are Inputs to
Future Edits", this is a **class**, not a site: the same "the ONE status" claim should be swept for
before it is corrected, and the correcting commit should take every site at once.

### 1.3 `append_synthetic_waves` — exists, right signature, **zero production callers, and a no-op on subtask 8's population**

Signature at `graph/sequence.py:291`, exactly as design row 8 assumes:

```
def append_synthetic_waves(plan: WavePlan, graph: FleetGraph, node_refs: Sequence[NodeRef]) -> WavePlan
```

**Production callers: 0.** Two genuinely different instruments, with a positive control:

- **Instrument A (AST).** `ast.parse` every `src/**/*.py`; collect `ast.Call` nodes whose `func` is
  a `Name`/`Attribute` spelled `append_synthetic_waves`. Result: **0**.
  *Positive control:* the same instrument over `tests/**/*.py` returns **1**
  (`tests/test_graph_sequence.py:538`) — the detector fires on a known-present site.
- **Instrument B (normalised text).** Whole-file whitespace normalisation with an offset→line map,
  regex `append_synthetic_waves\s*\(`, over `src/**/*.py`. Result: **1**, and it is the `def` at
  `sequence.py:291` itself.

The instruments disagree by exactly one, and the disagreement is the blind-spot check working: A
excludes definitions by construction, B cannot. Net production call sites: **0**.

**And now the finding that breaks design row 8.** `append_synthetic_waves` opens with:

```
fresh = [ref for ref in sorted(set(node_refs)) if ref not in plan.wave_index_by_node]
if not fresh:
    return plan
```

Every repo that was in the original plan **is** in `plan.wave_index_by_node`. Subtask 8's entire
population — repos that were assigned wave *N*, went `BLOCKED`, and are now un-blocked — is exactly
the population this filter excludes.

**Exercised in `.venv`, not reasoned about** (the shipped `chain_report()` fixture,
`acme-a → acme-b → acme-c`):

| Case | Setup | `repo_wave_index` before | after `append_synthetic_waves(plan, graph, [("REPO","acme-a")])` | waves |
|---|---|---|---|---|
| 1 — the shipped test's case | `assign_waves(rep, gated_repo_ids=["acme-a"])`, so `acme-a` is **absent** from the plan | `{b:1, c:0}` | `{a:2, b:1, c:0}` | 2 → 3, new wave `synthetic=True` |
| 2 — **subtask 8's case** | `assign_waves(rep)`, so `acme-a` is **present** at wave 2 | `{a:2, b:1, c:0}` | `{a:2, b:1, c:0}` — unchanged | 3 → 3, **0 synthetic waves appended**, and the returned object is the input **by identity** (`result is plan` → `True`) |

The one shipped test, `test_a_freed_repo_is_appended_at_max_wave_plus_one`
(`tests/test_graph_sequence.py:528`), asserts `"acme-a" not in plan.repo_wave_index` in its second
line — i.e. it pins case 1 and only case 1. The function's docstring describes its real domain:
*"repos freed by a late resolution"*, i.e. the §3.5.1 stub lifecycle, where the freed node was never
in the plan.

**Consequence for the implementer:** you cannot satisfy row 8's criterion by calling
`append_synthetic_waves`. Three ways out, costed in §6 (option W).

### 1.4 The DB side of "the synthetic wave"

Row 8's criterion is about **rows** (`waves.synthetic = 1`), not about a `WavePlan`. The DB path:

| Role | Symbol | Note |
|---|---|---|
| The only `waves`/`wave_members` writer | `SqliteSchedulerStore.record_plan` (`scheduler.py:150`) | Takes a whole `WavePlan`. Its only production caller is `_sequence_impl` (`cli.py:2756`). |
| DDL | `schema.sql:218` (`waves`), `schema.sql:238` (`wave_members`) | **`wave_members` PRIMARY KEY is `(run_id, node_kind, node_id)`** — a node belongs to exactly **one** wave. |
| Membership upsert | `"… ON CONFLICT (run_id, node_kind, node_id) DO UPDATE SET wave_index = excluded.wave_index"` (`scheduler.py:187`) | So appending a repo to a synthetic wave necessarily **MOVES it out of its original wave**. It cannot be a member of both. |

There is **no** DB→`WavePlan` loader anywhere in `src/`. Predicate: normalised token `WavePlan`
across `src/` → 8 sites, all of which are the definition, two re-exports, three signature
annotations on `record_plan`, and one import. Rebuilding a plan at resume time therefore means
re-running `_sequence_impl`'s pipeline (`_graph_nodes` → `_graph_edges` → `build_graph` →
`break_cycles` → `assign_waves`), which is local and model-free (so §11.5's "steps 1–7 make no
network call and invoke no model" survives), but which `_sequence_impl` itself refuses mid-run
("… not landed (§3.1). Pass --force-resequence to override, or `fleet abort` first." —
`cli.py:2700`).

---

## 2. Q2 — Is the SPEC's authority for the synthetic wave load-bearing here?

**Short answer: yes, and more directly than the brief supposes — but design ambiguity 3's reading is
still accurate about what it actually claims.**

### 2.1 The SPEC text, resolved by content

`SPEC.md:1564–1580` (§3.5), two consecutive paragraphs. Reproduced because the exact scoping is the
whole question:

> Propagation is idempotent … and **reversible**: if `r` is later fixed and re-run to `SUCCEEDED`,
> `fleet resume` removes `r` from every `blocked_by`, and any `d` whose `blocked_by` becomes empty
> returns to `PENDING` **at its re-entry floor (§11.5 step 5)**, never the earliest incomplete
> phase. Nothing is permanently lost because a dependency once failed.
>
> **Wave re-entry — un-blocking never re-opens a closed wave.** "Returns to `PENDING`" is only
> meaningful once it is said *where in the schedule* the repo returns … The rule: **closed waves are
> never re-opened.** Instead the un-blocked set is appended as a **synthetic wave** at
> `wave_index = max(waves) + 1`, its internal order recomputed by topological layering over the
> un-blocked set alone (so an un-blocked chain of four still migrates in dependency order, across
> four synthetic waves if needed). Rows are written to `waves` / `wave_members` like any other wave
> and carry `waves.synthetic = 1` so the projection can say why a repo migrated late. Their Phase 4
> runs against the **current** integration tip — a fresh snapshot ref per §3.3 step 1, not the tip
> their original wave saw — and any Phase 3 merge is rebased onto it, because everything that closed
> in between has already landed.

### 2.2 What this does and does not settle

**It settles, for subtask 8, without ambiguity:**
- the removal trigger for population 1 (`r` re-run to `SUCCEEDED`);
- that an emptied `blocked_by` ⇒ `PENDING`;
- **that the phase is `§11.5 step 5`'s re-entry floor** — the SPEC explicitly delegates the *phase*
  question to step 5 and owns the *wave* question itself. So subtask 8 must call the same
  `orchestrator/reentry.phase_floor` that subtask 7 calls (see §4);
- the wave index (`max(waves) + 1`), the internal layering rule, `waves.synthetic = 1`, and that
  rows go to **both** `waves` and `wave_members`;
- a **fifth requirement design row 8 omits entirely**: *"Their Phase 4 runs against the current
  integration tip — a fresh snapshot ref per §3.3 step 1 … and any Phase 3 merge is rebased onto
  it."* Row 8's success criterion has no clause for this. It is arguably step 8's job (the phase
  runners), not step 6's, but nothing in the design says so and nothing today enforces it. Flagging
  it as an unowned SPEC clause; see §6 option T.

**It does not settle — and design ambiguity 3 is right about this:**
- The paragraph's own headline scopes it: *"**Wave re-entry — un-blocking** never re-opens a closed
  wave."* Every sentence in it is about the un-blocked set. It says nothing about a repo demoted by
  step 5.
- §11.5's own step list is even barer. Step 6 in full is: *"(6) recompute `blocked_by` from `phases`
  + `edges` so a since-fixed dependency unblocks its subtree"* (`SPEC.md:7013`). **No wave. No
  synthetic wave. No `max(waves)+1`.** The synthetic-wave rule for step 6 exists *only* in §3.5.
- §11.5 step 5, read in full (`SPEC.md:6957–7015`), is entirely about *which phase*. It never
  mentions `waves`, `wave_members`, or wave membership at all.

**Verdict on the design's sentence.** "the SPEC says so for `blocked_by` reversal (step 6), not for
step-5 demotion" — **accurate at `4792b19`.** "the SPEC does not authorise it here [step 5]" —
**accurate**; I found no sentence anywhere in `docs/SPEC.md` placing a *demoted* repo in a wave.
Normalised sweep of the whole SPEC for `synthetic wave` → 4 sites (§3.5 ×2, §3.5's contract-rollback
sentence, and the `waves.max_usd` DDL comment); for `max\(waves\)` → 3 sites (§3.5, the
`MigrationWave.synthetic` field description, the `waves` DDL comment). **None is in §11.5.** None
mentions demotion.

**I am not resolving the ambiguity — that ruling is yours.** But §3 below shows it is no longer
academic: at `4792b19`, subtask 7 as designed will re-open closed waves whether or not anyone rules
on it.

---

## 3. Q3 — Demotion × `blocked_by`, in both directions

This is where the design is wrong, and the brief was right to suspect it.

### 3.1 Direction A — does demoting a repo re-block its dependents? **No. And that is correct.**

A demoted repo's phase rows go to `PENDING` (`state/repository.demote_to_floor` →
`models.enums.demote` → `transition(SUCCEEDED, PENDING, resume=True)`). `PENDING` is in none of the
three populations of §1.2: it is not RHI, not quarantine-`SKIPPED`, and not a `FAILED` contract.
`append_blocked_by` is never called by `demote_to_floor` (verified: `demote_to_floor`'s body at
`state/repository.py:1424–1470` touches `phases`, `checkpoints` and `findings` only). So a step-5
demotion adds **no** `blocked_by` entry. Correct behaviour: a repo that must re-run is not an
abandoned repo, and blocking its dependents would be a category error.

**But there is a real hazard on the same axis, and it is a defect.** Consider `d` blocked by `r`,
where `r` was RHI, was fixed, and reached `SUCCEEDED`. Step 5 runs **before** step 6 (SPEC §11.5's
own numbering; `models/enums.py:159` states the ordering explicitly: *"step 5 runs BEFORE step 6's
`blocked_by` recompute, so a still-`BLOCKED` Phase-2 row is a case subtask 6 will genuinely
encounter"*). If step 5 demotes `r` from `SUCCEEDED` back to `PENDING`, then by the time step 6 runs:

- `r` is no longer `SUCCEEDED`, so SPEC §3.5's stated reversal trigger ("re-run to `SUCCEEDED`") is
  **not** satisfied — a literal implementation leaves `d` blocked by a repo that is now merely
  PENDING, forever, which is exactly the failure `ordering_descendants`' docstring names
  (*"a cached closure that outlived one `blocked_by` reversal is exactly how a fixed repo stays
  blocked forever"*, `scheduler.py:436`);
- but a **recompute-from-scratch** implementation ("recompute `blocked_by` from `phases` + `edges`",
  SPEC §11.5 step 6, which is a *derivation*, not a *reversal*) sees `r` is not RHI, drops the
  entry, empties `d`'s list, and returns `d` to `PENDING` in a synthetic wave — **while `r`'s own
  work has just been discarded and `r` sits at a floor inside a closed wave that will never
  re-open.** `d` would then migrate against a dependency that has not landed, which
  `WaveNotReadyError`'s message calls "the failure the topological sequencer exists to prevent".

The two readings of step 6 — *reversal* (§3.5's wording) and *recompute* (§11.5's wording) — give
opposite answers on this input. **This is a genuine SPEC-internal contradiction that subtask 8 must
have a ruling on before it is built.** See §6 option R.

### 3.2 Direction B — does step 5's demotion re-open a closed wave? **Yes. Measured. Today.**

This is the finding I would put first if the brief allowed one.

`WaveState` is **computed, never stored** (`WaveScheduler.wave_state`, `scheduler.py:319`):

```
members = await self.store.wave_members(self.run_id, wave_index)
for repo_id in members:
    if await self.status_of(repo_id) not in SETTLED_STATUSES:
        return WaveState.PARTIAL if await self.breached(wave_index) else WaveState.OPEN
return WaveState.CLOSED
```

with `SETTLED_STATUSES = TERMINAL_STATUSES ∪ {BLOCKED, DEGRADED}` (`scheduler.py:62`) and
`TERMINAL_STATUSES = {SUCCEEDED, REQUIRES_HUMAN_INTERVENTION, SKIPPED}`
(`models/enums.py:25`, exercised in `.venv`).

**So "re-opening a closed wave" is not a write anybody performs. It is an emergent property of any
status write.** A wave is CLOSED iff every one of its `wave_members` is settled; move *any* member
off a settled status and that wave is OPEN again, with no code having named a wave at all.

Enumerating what moves a member off `SETTLED_STATUSES`:

- **`SUCCEEDED → PENDING`** — `demote_to_floor` (subtask 6, landed). A demoted repo is a member of
  its original, closed wave. `demote_to_floor` touches `phases`, `checkpoints`, `findings` — and
  **nothing in `waves`/`wave_members`.** So the moment subtask 7 wires it up, `fleet resume` will
  flip closed waves back to OPEN. Nothing detects it, nothing forbids it, and `open_wave`
  (`scheduler.py:344`) will then raise `WaveNotReadyError` for every later wave — including, if the
  synthetic wave is appended at `max(waves)+1`, for the synthetic wave subtask 8 just created.
- **`BLOCKED → PENDING`** — subtask 8's own un-blocking write. `BLOCKED` is settled; `PENDING` is
  not. Un-blocking `d` re-opens `d`'s original wave **unless `d`'s `wave_members` row is moved in
  the same transaction.** This is precisely why the SPEC prescribes the synthetic wave, and it means
  **the wave move is not decoration — it is what makes the criterion true.**

That is the mechanism, and it explains why row 8's three clauses are one atomic obligation rather
than three: `blocked_by` edit + status write + `wave_members` move must be **one** `StateWriter`
unit, or the intermediate state is a re-opened closed wave.

**Design row 8 was written before subtasks 6 and 7 existed, and it shows: it assigns the "no closed
wave is re-opened" obligation to subtask 8, but subtask 7 breaches it first, by a different door,
with no wave move available to it at all** (a demoted repo is not "freed"; the SPEC gives it no
destination — that is design ambiguity 3, arriving as a defect rather than as a question).

### 3.3 A third interaction the design does not mention — quarantine

Population 2 of §1.2 has **no reversal condition**. A "recompute `blocked_by` from `phases` +
`edges`" that enumerates only RHI ancestors will find no RHI ancestor for a quarantined repo's
dependents, empty their `blocked_by`, and re-admit them into a synthetic wave — **un-doing an
operator's audited `OperatorQuarantine` decision on the next `fleet resume`.** SPEC §12 item 46 (ii)
(`SPEC.md:7272`) is adjacent and binding: *"a test that drives every automatic sweep — the reaper,
`fleet resume`, `stub_reconcile`, **`blocked_by` recomputation** — finds none of them able to move a
repo out of `REQUIRES_HUMAN_INTERVENTION`."* It names `blocked_by` recomputation by name, but only
for RHI; the quarantine case is the same shape one status over and is **not** covered by that
sentence or by any test I found (`tests/test_state_models.py:588
test_the_resume_door_opens_onto_pending_from_succeeded_and_nothing_else` covers the *transition
table*, not the recompute).

**Mitigation the implementer should take as the default:** derive the blocker set as
`{repo : any phase row's status ∈ (RHI ∪ {SKIPPED-with-an-OperatorQuarantine-finding})}`, or — much
simpler and more honest — **do not re-derive the blocker set at all**; see §4.2 option M2.

### 3.4 A pre-existing defect on subtask 8's path — I checked it rather than inheriting it

`append_blocked_by` writes `status = 'BLOCKED'` in **raw SQL**, bypassing `transition()`. Its guard
is `if current is RepoStatus.SUCCEEDED or current in TERMINAL_STATUSES: continue` — which does
**not** exclude `DEGRADED`, because `DEGRADED ∉ TERMINAL_STATUSES` (exercised: `False`). But
`transition(DEGRADED, BLOCKED)` **raises** (exercised: `illegal status transition DEGRADED ->
BLOCKED`; `ALLOWED_TRANSITIONS[DEGRADED] = {RUNNING, SUCCEEDED, REQUIRES_HUMAN_INTERVENTION}`).

So `append_blocked_by` can perform a transition the state machine forbids. Pre-existing at
`4792b19` and **not** subtask 8's to fix — but subtask 8 re-runs this code path on every resume,
which converts a rare race into a per-resume event. Recommend a D-number rather than a fix inside
subtask 8. (Per CLAUDE.md Guardrail 6, I did not accept "pre-existing" as a reason not to verify it:
both halves were exercised in `.venv`, not read off the declaration.)

---

## 4. Q4 — D74's shape for subtask 8

**D74 recap (as the brief states it, and as I verified it applies):** a unit computes a value that
another unit recomputes independently with nothing enforcing agreement. The sibling instance for
subtask 7 is real and is in `r2-research.md` §3.3 — `demote_to_floor` returns its demotions from its
own in-transaction rows, so a `--dry-run` cannot get the plan from it.

Subtask 8 has **four** independent derivations, of which two are new and two are shared with
subtask 7.

### 4.1 The four seams

| # | Value | Derivation A | Derivation B | Nothing enforces agreement because… |
|---|---|---|---|---|
| **S1** | **The re-entry floor** of each un-blocked repo | subtask 7 computes it for the step-5 population via `reentry.phase_floor` + `evidence_holds` | subtask 8 needs it for the step-6 population — and **that population is disjoint from subtask 7's**, because `demote()` refuses `BLOCKED` (`enums.py:161`, and the docstring says so in as many words), so step 5 skipped every repo step 6 un-blocks | Two call sites of the same computation, on disjoint inputs, in the same command. Easy to get right — and easy to get wrong by writing a second, SQL-side notion of "floor". |
| **S2** | **`max(waves) + 1`** | `append_synthetic_waves` computes `offset = max(w.wave_index for w in plan.waves, default=-1) + 1` from an **in-memory `WavePlan`** | `SqliteSchedulerStore.wave_indices(run_id)` reads the **DB** | A `WavePlan` rebuilt at resume time by re-running `assign_waves` **will not contain prior synthetic waves** (they exist only as rows; §1.4: no DB→`WavePlan` loader). So on the *second* resume that appends a synthetic wave, A returns a value that collides with an existing row, and `record_plan`'s `ON CONFLICT (run_id, wave_index) DO UPDATE` **silently merges the two waves** rather than failing. This is a data-loss-shaped bug, not a cosmetic one. |
| **S3** | **The un-blocked set** | the `blocked_by` recompute, which knows which lists emptied — inside its transaction | the `node_refs` argument the wave append needs | If `node_refs` is re-derived by a second query after the write, the two can disagree across an interleaving; if it is derived *before*, it can disagree with what the write actually did. Structurally identical to r2's subtask-7 finding. |
| **S4** | **`waves.max_usd`** | frozen per the DDL comment: *"FROZEN here at the wave's first admission. Persisted because a resume must enforce the same number the halt was measured against"* (`schema.sql:227`) | `record_plan` recomputes it as `max_usd_per_repo × (len(repo_ids) + len(contract_ids))` and **overwrites it on conflict** (`scheduler.py:182`: `max_usd = excluded.max_usd`) | Moving a member out of wave *N* into a synthetic wave changes wave *N*'s member count. If subtask 8 reaches the wave tables through `record_plan`, it **silently re-budgets every closed wave** — a direct contradiction of the DDL comment, and another door in the Q5 sense. |

S2 and S4 are the two that will bite. They both come from the same root: **`record_plan` is a
whole-plan writer and subtask 8 needs an incremental one.**

### 4.2 Recommended mechanism — a shared pure function, not a convention

The shape that closes S1–S3 mechanically:

```
# src/fleet/orchestrator/reentry.py  (module already exists; subtask 5 is editing it)

@dataclass(frozen=True, slots=True)
class Unblocking:
    repo_id: str
    removed: tuple[str, ...]     # blockers dropped from blocked_by
    remaining: tuple[str, ...]   # blockers that survive
    floor: Phase | None          # None iff `remaining` is non-empty, or RHI

def plan_unblocking(
    *,
    blocked_by_rows: Mapping[tuple[str, Phase], frozenset[str]],
    blocker_statuses: Mapping[str, RepoStatus],
    floors: Mapping[str, Phase | None],
) -> tuple[Unblocking, ...]:
    """Pure. No I/O, no clock, no connection. Given the current `blocked_by` rows, the
    current status of every named blocker, and each candidate's floor, return exactly
    what step 6 must write."""
```

**Why this shape and not another:**

- It is **pure**, so `--dry-run` and the real path call the *same* function on the *same* inputs and
  cannot drift — which is exactly the D74 remedy, and exactly the shape `phase_floor` already has
  (`reentry.phase_floor(rows, evidence)` — no I/O, per design §5 row 2's success criterion). It puts
  subtask 8's plan on the same footing subtask 7's plan needs, rather than inventing a second idiom
  in the same command.
- `floors` is passed **in**, not computed inside, so S1 is closed by construction: the caller must
  obtain floors from `reentry.phase_floor`, and there is no second place a floor could come from.
- It returns `removed` **and** `remaining`, so the write path never re-derives "did this list
  empty?" — closing S3.
- It lives in `orchestrator/reentry.py`, which already exists and is already the home of the
  resume-owned pure computations. It does **not** go in `scheduler.py`, whose `SchedulerStore`
  Protocol is deliberately narrow.

For S2 and S4, the mechanism is different in kind: **do not route the wave write through
`record_plan`.** Add one narrow method to `SchedulerStore`:

```
async def append_unblocked_wave(
    self, run_id: str, *, node_ids: Sequence[str], layering: Mapping[str, int],
    now: datetime, max_usd_per_repo: float,
) -> tuple[int, ...]:
    """Allocate synthetic wave indices from `SELECT MAX(wave_index) FROM waves` **inside the
    write transaction** and move the named members into them. Existing waves' rows are not
    touched — no `computed_at`, no `max_usd`, no membership."""
```

Reading `MAX(wave_index)` inside the same `BEGIN IMMEDIATE` that writes closes S2 against the
in-memory/DB skew *and* against a concurrent writer; not touching existing `waves` rows closes S4 by
construction. The internal topological layering over the un-blocked set alone (SPEC's "across four
synthetic waves if needed") is a pure computation over the ordering pairs `_ordering_pairs` already
returns — it does **not** need a `FleetGraph`, and it should not pull one in.

### 4.3 What this costs — stated, not hidden

1. **A new method on the narrowest Protocol in the codebase.** `SchedulerStore` grows from 8 methods
   to 9, and both CLI passthrough wrappers (`cli.py:1366ff`, `cli.py:3569ff`) must grow it too —
   they are structural, `@runtime_checkable`-checked doubles, so omitting either is a runtime
   failure, not a type error. That is 3 edit sites for one method. **Not counted in design row 8's
   "Files touched".**
2. **A second wave-writing path.** `record_plan` and `append_unblocked_wave` both write `waves`. Two
   writers of one table is precisely the shape that produced the `blocked_by`/`transition()`
   bypass in §3.4. The mitigation is that `append_unblocked_wave` is strictly append-and-move and
   never rewrites an existing wave row — which is *assertable* (see §5.3), so it can be a mechanism
   rather than a convention.
3. **`plan_unblocking` cannot see the DB.** Its purity is what makes it testable and is also its
   limit: it cannot know that a blocker's status changed between the read that built
   `blocker_statuses` and the write. The transaction must re-read, exactly as `demote_to_floor`
   re-reads the RHI check inside `BEGIN IMMEDIATE` rather than trusting `phase_floor`
   (`state/repository.py:1385–1391` argues this at length: *"a refusal that lives only in the caller
   is a refusal the next caller can forget"*). Subtask 8 owes the same re-read. **That re-read is
   itself a fifth D74 seam** — the honest position is that it is a re-read *of a guard*, not a
   re-derivation *of a plan*, and it is allowed to disagree by returning `()`.
4. **It does not close S1 across commands**, only within `fleet resume`. If step 8 (subtask 10) later
   computes a floor to decide what to run, that is a third site.

---

## 5. Q5 — What would make subtask 8's tests real

### 5.1 The absence in the name

Row 8's criterion ends *"**no closed wave is re-opened**"*. Per CLAUDE.md Rule 12: *"When a name
asserts an absence … the name needs its own proof"*, and *"a mutation perturbs only the path the
test walks, never the other doors to the same state."*

**The doors, enumerated from source rather than imagined.** A wave is CLOSED iff every
`wave_members` row for it names a repo whose status is in `SETTLED_STATUSES`
(`WaveScheduler.wave_state`, §3.2). So every door is a write that changes one of the two operands:

| # | Door | Reachable how | Accidental or adversarial? |
|---|---|---|---|
| D1 | A **member's status** leaves `SETTLED_STATUSES` — `BLOCKED → PENDING` | subtask 8's own un-blocking write. `transition(BLOCKED, PENDING)` is an **ordinary** `ALLOWED_TRANSITIONS` edge (`enums.py:41`) — no gate, no `resume=True`, **no audit record**, unlike step 5's `demote()`. | **Accidental. This is the main door.** |
| D2 | A **member's status** leaves `SETTLED_STATUSES` — `SUCCEEDED → PENDING` | `demote_to_floor`, i.e. **subtask 7**, on a repo that is a member of a closed wave, with no wave move performed. | **Accidental, and live at `4792b19` the moment subtask 7 lands.** Not subtask 8's write, but the same state. |
| D3 | **Membership changes** — a repo is moved *into* a wave index ≤ the highest closed one | `record_plan`'s `ON CONFLICT … DO UPDATE SET wave_index = excluded.wave_index`. A `WavePlan` rebuilt from a graph that changed since `fleet sequence` renumbers waves. | **Accidental**, if subtask 8 reaches the tables via `record_plan`. |
| D4 | **Membership changes** — a repo is moved *out of* a closed wave, changing that wave's `max_usd` denominator | S4 in §4.1. Doesn't re-open the wave, but silently re-budgets it. | Accidental. |
| D5 | A **new `waves` row** is inserted at an index that is not `> max(existing)` | S2 in §4.1 — an in-memory `max()` that has not seen prior synthetic waves. | **Accidental, and silent**: `ON CONFLICT (run_id, wave_index) DO UPDATE` merges rather than raising. |
| D6 | `begin_wave` is called on an already-closed index, resetting `wave_started_at` | `record_plan` deliberately omits `wave_started_at` from its `DO UPDATE` (`scheduler.py:154–158` explains why), so this needs a direct `begin_wave` call. | Adversarial. |
| D7 | A `wave_members` row is **deleted** (FK `ON DELETE CASCADE` from `waves`), so a wave with un-settled members becomes vacuously CLOSED | Deleting a `waves` row. No production code deletes one. | Adversarial. |

D1–D5 are **accidentally reachable**; per CLAUDE.md Rule 12's stop rule those must be *fixed*.
D6–D7 require a deliberately misbehaving caller and should be **documented as a stated boundary and
not patched**.

### 5.2 The existing whitelist inversion, cited by symbol

The project's example of "assert what the code may *name* at all, rather than enumerating forbidden
sinks" is:

- **`TRANSITION_GLOBALS`** — a module-level `frozenset[str]` in `tests/test_state_models.py:694`;
- consumed by **`test_transition_demotes_without_writing_a_record_or_naming_a_new_sink`**
  (`tests/test_state_models.py:712`), whose load-bearing line is
  `assert set(transition.__code__.co_names) == TRANSITION_GLOBALS` (`:763`).

Read its docstring before writing anything for subtask 8 — it is the best 40 lines in the repo on
this exact problem. Three things to carry over verbatim:

1. **Why the inversion.** *"a reviewer defeated THAT six ways — a function attribute, a mutable
   default argument, a function-local `from fleet…` import (invisible to a line-start source scan),
   `warnings.warn`, a `ClassVar` on `PhaseDemotion`, and `print()`. Each new enumeration of routes
   was defeated by a route not enumerated, because enumerating exits is the wrong shape of check."*
2. **The name is scoped to what the assertion reaches** — "naming a NEW sink", not "reaching" one.
   Subtask 8 must do the same: not `test_no_closed_wave_is_reopened`, but a name scoped to what the
   whitelist actually pins.
3. **The disclosed residue.** *"a side effect routed entirely through names already on the whitelist
   passes every assertion below. Verified, not hypothesised."* Subtask 8 owes the equivalent
   sentence.

### 5.3 What a whitelist-shaped assertion looks like *here*

`co_names` is the wrong operand for subtask 8 — the sinks are SQL statements and table names, not
Python globals. The analogue is a **whitelist over what the step-6 write unit may name in SQL**.
Three assertions, in ascending strength:

**(a) Table/statement whitelist — the direct analogue.** With
`aiosqlite`/`sqlite3`'s `Connection.set_trace_callback`, capture every statement the step-6 unit
executes, normalise, and extract the `(verb, table)` pairs:

```
STEP6_SQL_TARGETS: frozenset[tuple[str, str]] = frozenset({
    ("SELECT", "phases"), ("SELECT", "edges"), ("SELECT", "waves"), ("SELECT", "wave_members"),
    ("UPDATE", "phases"),          # blocked_by + status, the un-blocking write
    ("INSERT", "waves"),           # the synthetic wave rows
    ("INSERT", "wave_members"),    # the membership MOVE (upsert)
    ("INSERT", "findings"),        # if the ruling in §6 option A says step 6 audits
})
assert observed == STEP6_SQL_TARGETS, (...)
```

This catches by construction: a `DELETE FROM waves` (D7), an `UPDATE waves` (D4/D6), a write to any
table nobody predicted. It fires on forms nobody enumerated, which blacklists cannot.

**(b) A monotonicity invariant on the wave index — closes D3 and D5 mechanically.** Snapshot
`SELECT wave_index, computed_at, wave_started_at, synthetic, max_usd FROM waves` and
`SELECT node_id, wave_index FROM wave_members` before and after. Assert:

- every pre-existing `waves` row is **byte-identical** after (this is what D4/D6 trip);
- every newly inserted `wave_index` is `> max(pre-existing)` **and** carries `synthetic = 1`;
- every `wave_members` row whose `wave_index` changed moved **upward**, and its `node_id` is in the
  un-blocked set — nothing else moved.

This is stronger than the criterion's wording, and it is the assertion that actually *means* "no
closed wave is re-opened", because it constrains the operands `wave_state` reads rather than
re-implementing `wave_state`.

**(c) The property assertion, computed by the production code — the one that binds the name.**
Enumerate every wave index that was `CLOSED` before the unit ran (by calling
`WaveScheduler.wave_state`, not by re-deriving it) and assert each is still `CLOSED` after. This is
the criterion, literally. **It is the weakest of the three on its own** — it walks exactly one path,
and D2 (subtask 7's demotion) is invisible to it because subtask 7's write is outside the unit —
which is why (a) and (b) exist. Use all three.

### 5.4 Rule 12's four instrument checks, for whichever detector you build

Before trusting a clean result from (a)/(b)/(c), all four:

1. **Fires on known-bad.** Construct a fixture where a repo is un-blocked with **no** `wave_members`
   move — (b) and (c) must both go red. This is the literal state design row 8 forbids.
2. **Silent on a clean tree.** The full step-6 unit on a fixture with nothing to un-block writes
   nothing and passes.
3. **Fires on a synthetic fault injected into a clean fixture.** Hand-write an
   `UPDATE waves SET max_usd = …` into the unit; (a) and (b) must both go red. This is the check
   that catches a trace callback silently not attached — the exact failure mode CLAUDE.md records
   for the mutation harness that was not mutating.
4. **Control stays green.** Reflow/reindent the SQL string literals in the step-6 unit — the
   normalising extractor in (a) must not care. If it does, (a) is asserting layout.

Plus the mutation discipline from Rule 12: an old-passes/new-fails pair on the same input, with the
mutation harness aborting when `git diff` reports zero changed lines, **and that abort check read
before the test result.**

### 5.5 What these tests still cannot catch — state it, do not imply closure

- **D2.** Subtask 7's demotion re-opens closed waves through a write that is not in subtask 8's unit.
  No assertion scoped to step 6 can see it. It needs its own gate in subtask 7, or a ruling
  (§6 option D).
- **The `wave_state` re-derivation.** (b) constrains the operands; if `SETTLED_STATUSES` itself is
  later widened (say, to include `PENDING`), every assertion here stays green while the property
  becomes vacuous. A `SETTLED_STATUSES == frozenset({...})` pin — the same shape as
  `assert RESUME_DEMOTE.keys() == {RepoStatus.SUCCEEDED}` at `tests/test_state_models.py:598`, and
  the same shape as that file's deliberate `assert len(elsewhere) == 5` stop — is the cheap
  countermeasure.
- **Ordering.** Nothing above asserts that `d`'s synthetic wave lands *after* any wave `r` was moved
  to. §3.1's hazard survives all three assertions.

---

## 6. Needs a controller ruling at dispatch — options with costs. **I am not picking one.**

### Option W — how subtask 8 reaches `waves` / `wave_members`, given §1.3

- **W1 — extend `append_synthetic_waves`** to accept already-scheduled nodes (drop or parameterise
  the `fresh` filter).
  *Cost:* changes a function whose only test pins the opposite behaviour
  (`test_a_freed_repo_is_appended_at_max_wave_plus_one` asserts `"acme-a" not in
  plan.repo_wave_index` as a precondition). Still leaves S2/S4 open, because the caller still needs a
  `WavePlan` that does not exist at resume time (§1.4, P5). Cheapest-looking, most likely to
  regenerate the class.
- **W2 — a new narrow `SchedulerStore.append_unblocked_wave`** (§4.2), SQL-only, no `WavePlan`.
  *Cost:* +1 Protocol method × 3 implementations; a second writer of `waves` (mitigated by (a)/(b)
  in §5.3); the topological layering must be re-implemented over `_ordering_pairs` rather than
  reusing `layer()` on a `FleetGraph`. Closes S2 and S4 by construction.
- **W3 — rebuild a full `WavePlan` at resume time** (`_graph_nodes`→`build_graph`→`break_cycles`→
  `assign_waves`) and go through `record_plan`.
  *Cost:* the most expensive step in `fleet sequence` on every `fleet resume`; `_sequence_impl`
  itself refuses this mid-run (`cli.py:2700`); opens D3, D4 **and** D5 simultaneously; renumbers
  waves if the graph moved. I do not recommend it, and say so as an *Agent Recommendation*, not a
  directive.

### Option R — which reading of step 6 governs (§3.1)

- **R1 — reversal** (SPEC §3.5's wording): remove `r` only when `r` is `SUCCEEDED`.
  *Cost:* a step-5-demoted blocker leaves its dependents blocked forever — the exact failure
  `ordering_descendants`' docstring names.
- **R2 — recompute** (SPEC §11.5 step 6's wording): derive `blocked_by` from current `phases` +
  `edges`.
  *Cost:* un-blocks `d` while `r` sits demoted at a floor in a closed wave — `d` migrates against a
  dependency that has not landed. Also the reading that walks straight into the quarantine defect
  (§3.3) unless the blocker predicate is widened.
- **R3 — recompute, but treat a demoted blocker as still blocking** (`r` blocks `d` if `r` is RHI,
  quarantine-`SKIPPED`, *or* was demoted this run).
  *Cost:* introduces a fourth `blocked_by` population with no SPEC sentence behind it; needs an ADR;
  and "was demoted this run" is a `findings`-derived predicate (`PhaseDemoted`), i.e. a new coupling
  between step 6 and step 5's audit trail.

The SPEC contradicts itself between R1 and R2 across §3.5 and §11.5 (§2.2). **Whichever is chosen,
CLAUDE.md §"Documents Are Inputs to Future Edits" applies: "the SPEC says X but the code cannot do X"
is two edits, and the losing SPEC sentence must be corrected in the same change or a reconciler
regenerates the defect.**

### Option D — who owns "no closed wave is re-opened" for **step-5 demotions** (§3.2, D2)

- **D-a — subtask 8 owns it**: step 6 also moves demoted repos into the synthetic wave.
  *Cost:* requires resolving design ambiguity 3 in the affirmative — placing a demoted repo in a
  synthetic wave — which §2.2 confirms **no SPEC sentence authorises**. Needs an ADR. But it is the
  only option that leaves `fleet resume` with all waves in a consistent state.
- **D-b — subtask 7 owns it**: `demote_to_floor`'s caller moves the repo's `wave_members` row.
  *Cost:* subtask 7 is sized **S** and is in flight next; this is not S work. Same missing SPEC
  authority.
- **D-c — nobody owns it; disclose it.** Document that `fleet resume` leaves closed waves re-opened
  after a demotion, and that `open_wave` will refuse later waves until those repos re-settle.
  *Cost:* `fleet resume` step 8 ("continue") then cannot open any wave past the lowest demoted one —
  which may in fact be **correct** (a demoted repo genuinely must re-run before its dependents), and
  is the cheapest honest position. But it makes row 8's criterion false as stated for the demoted
  population, so row 8's wording must change with it.

### Option A — is step 6's un-blocking audited?

Step 5's demotion mints a mandatory `PhaseDemoted` finding *"because a demotion discards landed,
green work and must be at least as loud as a `checkpoint_rejected`"* (SPEC §11.5 step 5). Step 6's
un-blocking goes through a plain `ALLOWED_TRANSITIONS` edge with no finding (§5.1 D1) — and it moves
a repo across waves, which the projection is explicitly supposed to be able to explain
(*"`waves.synthetic = 1` so the projection can say why a repo migrated late"*, SPEC §3.5).
*Options:* no finding (SPEC is silent, cheapest); a `BlockedByCleared` finding (symmetric with
step 5, +1 finding kind — note `tests/test_findings_kinds.py`'s `_recognition_gap` census will need
the new kind); or rely on `waves.synthetic` alone. **No SPEC sentence requires any of these.**

### Option T — the unowned §3.5 clause (§2.2, fifth bullet)

*"Their Phase 4 runs against the current integration tip — a fresh snapshot ref per §3.3 step 1, not
the tip their original wave saw — and any Phase 3 merge is rebased onto it."* Design row 8's success
criterion has no clause for it and design row 10 (step 8) does not mention it either.
*Options:* assign to subtask 8 (but it has no phase-runner surface); assign to subtask 10; or record
it as a known gap with a D-number. **Currently owned by nobody.**

---

## 7. Preconditions to re-verify at dispatch — do not inherit them from this brief

1. `src/fleet/orchestrator/reentry.py` and `src/fleet/cli.py` had **uncommitted sibling edits** at
   `4792b19`. Everything here about `phase_floor`, `evidence_holds`, `_resume_impl` and
   `_refuse_unbuilt_resume_flags` is HEAD-state, not sibling-branch state. Name the ref.
2. `r2-research.md` (subtask 7's brief) re-anchored itself at `7275adb`; this brief is at `4792b19`,
   one commit later. `git diff --quiet 7275adb 4792b19 -- <path>` on each cited path before relying
   on a cross-brief claim.
3. Subtask 7 must land before subtask 8 (`5 → 7 → 8`). If subtask 7 resolves option D, §3.2 and
   §5.1 D2 change shape.
4. `tests/test_reentry_evidence.py` was untracked at `4792b19` (subtask 5). Its landed form may
   constrain the `evidence` mapping S1 depends on.

## 8. What this brief cannot catch

- I did not run the suite (four lanes live; `pytest_sessionfinish` reaps `BAZEL_ROOT`). Every runtime
  claim here comes from a standalone `.venv` script, not from a test run. The
  `append_synthetic_waves` no-op (§1.3) and the `DEGRADED → BLOCKED` illegality (§3.4) were
  **exercised**; everything else about behaviour is read from source.
- The `blocked_by`-writer class result (§1.1) is a sweep of `src/`. A writer reached through
  dynamically-constructed SQL would be invisible to both instruments. I saw no string-built SQL on
  this path, but "I saw none" is weaker than "there is none".
- §1.2's claim that population 3 has 0 producers rests on a predicate keyed to `'FAILED'`. A
  contract-failure path that spells the status differently would escape it.
- §2's SPEC readings are mine. The two sentences in tension (§3.5's "reversal" and §11.5's
  "recompute") are quoted at length above precisely so the controller can check my reading rather
  than inherit it.
