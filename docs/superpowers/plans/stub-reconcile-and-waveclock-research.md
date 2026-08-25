> **Promoted from untracked scratch at round F close.** Written by lane R2 (research, read-only) in
> `.superpowers/sdd/handoff-round-f/lanes/R2/`. Body is byte-identical to the lane report apart from this
> banner. **Status change: this is now a tracked reference the next round acts on, not a working note.**
> Round E measured a promoted brief and found 3 of 14 claims false; assume the same rate and re-measure
> anything you act on.

# Lane R2 (research) — round F

**Base re-derived myself: `git rev-parse HEAD` = `12ac7842bea85d58cedd89de560d7e5c7a95aaa7`.**
(The lane protocol names `251cd30` as round F's base; my brief names `12ac784`. `12ac784` is what
`HEAD` actually is, and every `[probe @ 12ac784]` below was run against that working tree.)

**Interpreter discipline.** Every runtime number below comes from a driver run as
`env -i PATH=/usr/bin:/bin HOME="$HOME" PYTHONPATH="<repo>/src" .venv/bin/python <driver>`, and
every driver prints `sys.executable` and `fleet.__file__` and **asserts** `fleet.__file__` starts
with `<repo>/src` before printing any result. All four printed:

```
sys.executable = /home/redmage/swe repo harness/.venv/bin/python
fleet.__file__ = /home/redmage/swe repo harness/src/fleet/__init__.py
PIN OK
```

**No pytest session was run**, in the primary checkout or anywhere else. Nothing was mutated;
this lane is read-only and made no worktree. Scratch drivers live at
`/tmp/claude-1000/-home-redmage-swe-repo-harness/bd5b260e-.../scratchpad/roundf-R2/`
(`probe_breach.py`, `probe_crossphase.py`, `probe_raise.py`, `probe_stub_driver.py`,
`probe_merged_hazard.py`) — a per-lane subdirectory, never a shared filename.

---

# TASK 1 — S8-8 measured. **Ruling D is now rulable, and the premise it rests on is FALSE.**

## 1.1 `WaveScheduler.breached` and the wall-clock path, by `file:line`

| symbol | `file:line` | what it does |
|---|---|---|
| `WaveScheduler.breached` | `src/fleet/orchestrator/scheduler.py:422-429` | `wave_started_at is None → False`; else `elapsed_s(w) >= budgets.wave_max_wallclock_s` |
| `WaveScheduler.elapsed_s` | `scheduler.py:415-420` | `max(0, clock() - persisted wave_started_at)`. **Persisted**, so cumulative across processes |
| `WaveScheduler.wave_state` | `scheduler.py:406-413` | `CLOSED` if every member settled; else `PARTIAL` **iff** `breached`, else `OPEN` |
| `WaveScheduler.admit` | `scheduler.py:452-489` | on breach: `admitted=()`, `withheld=<every dispatchable member>`, `breached=True` |
| `WaveScheduler.may_admit` | `scheduler.py:491-494` | `not breached` — polled between admissions (`runner.py:366`) |
| `WaveScheduler.open_wave` | `scheduler.py:433-450` | raises `WaveNotReadyError` if **any** earlier wave is not `CLOSED` |
| `SqliteSchedulerStore.begin_wave` | `scheduler.py:223-245` | `UPDATE waves SET wave_started_at = COALESCE(wave_started_at, ?)` — **stamp-once, never reset** |
| `WAVE_WALLCLOCK_EXIT_CODE` | `src/fleet/orchestrator/runner.py:109` | `= 4` |
| `WaveReport.exit_code` | `runner.py:293-301` | `halt.exit_code` if halted; **else 4 iff `withheld` AND `state is PARTIAL`**; else `None` |
| `budgets=settings.config.budgets` | `cli.py:1929` (scan), `cli.py:4235` (transform) | the ceiling the delegate's scheduler reads comes from **settings**, not from a flag |

## 1.2 What a breached wave does — **exercised** (`probe_breach.py`, `[probe @ 12ac784]`)

Real temp SQLite DB, real `SqliteStateRepository` / `SqliteSchedulerStore`, statuses driven
through the real CAS pair, wall clock 60 s, three wave-0 members.

| state | `breached` | `elapsed_s` | `may_admit` | `wave_state` | `admitted` | `withheld` | `WaveReport.exit_code` |
|---|---|---|---|---|---|---|---|
| A. never opened (`wave_started_at IS NULL`) | `False` | `0.0` | `True` | `OPEN` | — | — | — |
| B. opened, clock +61 s | `True` | `61.0` | `False` | `PARTIAL` | `()` | all 3 | **`4`** |
| C. **new process, +10 days**, fresh scheduler, same DB | `True` | `864000.0` | — | `PARTIAL` | `()` | all 3 | **`4`** |
| E. same, but every member `SUCCEEDED` | `True` | — | — | `CLOSED` | `()` | `()` | `None` |
| F. step 6's appended (synthetic) wave, index 2 | **`False`** | — | — | — | — | — | — |

Per-repo, in state B: all three rows `status=PENDING attempts=0` — no attempt consumed, exactly
as `scheduler.py:459-462` promises.

**D.** With wave 0 `PARTIAL`, `open_wave(1)` raises
`WaveNotReadyError: wave 1 cannot open: wave 0 is PARTIAL …`.

Three consequences, each measured rather than argued:

1. **`breached` alone is not the exit-4 term; `withheld` is.** Row E is breached and exits `None`.
   `WaveReport.exit_code` needs `withheld` **and** `PARTIAL` (`runner.py:299-300`).
2. **A breached wave is breached FOREVER.** Row C. The only writer of `wave_started_at` in
   `src/` is `begin_wave`'s `COALESCE` (`scheduler.py:234`); `record_plan` deliberately omits the
   column from its upsert (`scheduler.py:164`) and `append_unblocked_wave` never touches an
   existing row (`scheduler.py:320-324`). Sweep: `grep -rn wave_started_at src/` → 20 hits, **zero**
   assign it `NULL` or a fresh value.
3. **Step 6's synthetic wave is the one live escape.** Row F: the appended wave has
   `wave_started_at = None`, so `breached` is `False`. Step 6 already runs in `_resume_impl`
   (`cli.py:10354`), so a repo whose `blocked_by` cleared **does** get a fresh clock today. A repo
   that merely sat in a breached wave does not.

## 1.3 The wall clock is per-**(run, wave)**, NOT per-phase — `[probe @ 12ac784]`, `probe_crossphase.py`

`store.wave_started_at(run_id, wave_index)` takes no phase (`scheduler.py:218`), and `waves` has no
phase column (`state/schema.sql:218-232`). Exercised: TRANSFORM opens wave 0 and burns its 60 s;
then a **brand-new scheduler for a phase that has never run**, over the same wave:

```
TRANSFORM breached(0)=True  elapsed=61.0   wave_started_at(0)=2026-08-09 12:00:00+00:00
BUILD     breached(0)=True  admitted=() withheld=('acme-auth','acme-commons') state=PARTIAL exit=4
VERIFY    breached(0)=True  admitted=() withheld=('acme-auth','acme-commons') state=PARTIAL exit=4
BUILD.open_wave(0) returned 2026-08-09T12:00:00+00:00  (COALESCE kept the TRANSFORM stamp: True)
```

**This is the load-bearing fact for step 8.** Step 8 delegates to *three* phase roots. If wave N's
clock was spent in TRANSFORM, then BUILD and VERIFY of wave N **can never admit a single repo**,
in this run or any later one — and `open_wave` in the later phase does not re-stamp. So the
"a fleet that cannot move" case is not exotic: it is the ordinary consequence of one exit-4 halt.

I did not find this stated anywhere in `src/`, `tests/` or `docs/`. `[UNVERIFIED]` that it is
intended rather than emergent — no ADR or SPEC sentence I found addresses cross-phase sharing of
`wave_started_at` either way.

## 1.4 The decision-relevant question, traced to an exit code

**Step 8 does not exist on `main`.** `_resume_impl` (`cli.py:10187-10420`) ends at the step-2 reap
and the payload; `cli.py:10105-10107` says so in the verb's own docstring — *"Step 8 does not
exist, so the verb reconciles the ledger and then refuses to continue with **exit 2**
(`ResumeIncompleteError`)"*. So this is a design question about the delegate contract, and the
delegate's half of it is fully determined:

**If step 8 delegates into `_transform_impl` over a wave whose clock is spent, the delegate returns
`exit_code = 4`, not 0.** The chain, all at `12ac784`:

1. `_open_transform_waves` → `_open_phase_waves` (`cli.py:4021-4051`) returns the wave: its members
   are `PENDING`, not in `('SUCCEEDED','SKIPPED','REQUIRES_HUMAN_INTERVENTION')`.
2. `_run_transform_wave` (`cli.py:4206-4254`) builds `WaveScheduler(budgets=settings.config.budgets)`
   and calls `runner.run_wave` → `scheduler.admit` → the row-B/row-C behaviour above.
3. `WaveReport.exit_code` = **4** (`runner.py:299-300`).
4. `_transform_impl` breaks the wave loop at `cli.py:4468-4469` and returns
   `"exit_code": 4` (`cli.py:4496-4501`).
5. `transform`'s wrapper (`cli.py:3207-3213`) / `_raise_for_phase` (`cli.py:8584-8593`) raises
   `FleetCliError(str(result["halt"]), exit_code=4)`.

**So the brief's premise — "does step 8 exit 0 on a fleet that cannot move?" — resolves to NO, on
the delegate side.** A step 8 that propagates its delegates' exit codes exits **4**, loudly.

**Two defects found on that path, both measured:**

* **The exit-4 message is the literal string `'None'`.** `result["halt"]` is
  `next((str(r.halt) for r in reports if r.halt is not None), None)` (`cli.py:4511`), and a
  wall-clock breach sets `WaveReport.halt = None` — `halt` is only populated from a `RunHalted`
  escaping the `TaskGroup` (`runner.py:363-364`). Exercised: `FleetCliError('None', exit_code=4)`
  (`probe_crossphase.py`). An operator gets exit 4 and the word `None`. `[probe @ 12ac784]`
* **The delegate does per-repo git work BEFORE it discovers it can admit nothing.** In
  `_transform_impl`'s wave loop, `upsert_phase` and `_prepare_repo` run for **every member**
  (`cli.py:4425-4450`) and `_run_transform_wave` — which is where `admit` happens — runs only at
  `cli.py:4452`. `_prepare_repo` (`cli.py:3864-3900`) checks out `migrate/<repo>`, **discards a
  crashed predecessor's dirty worktree**, and creates the phase anchor ref. So a step 8 that
  "just delegates" into a breached wave mutates every member's git state and then withholds every
  member. `[probe @ 12ac784 — source order, NOT exercised end-to-end]` I did not verify the same
  shape in `_build_impl`/`_verify_impl`; `[UNVERIFIED]` there.

**The other branch — a genuine exit-0-having-done-nothing — exists but is honest.** If
`_open_phase_waves` returns `()` (every member `SUCCEEDED`/`SKIPPED`/`RHI`), `reports` is empty,
`halt` is `None`, and `exit_code` is `ExitCode.SUCCESS` = 0. That is "nothing left to do", not
"cannot move".

## 1.5 **The premise "`fleet resume` has no wall-clock raise" is FALSE as an operator statement**

`[probe @ 12ac784]`, `probe_raise.py` — exercised, not read:

```
baseline budgets.wave_max_wallclock_s = 14400
raised   budgets.wave_max_wallclock_s = 86400
drifted_sections(baseline)            = ('budgets',)
_validate_accept_drift(raised, ['budgets']) = ('budgets',)
```

`wave_max_wallclock_s` lives in `BudgetsSection` (`settings.py:269`), `budgets` is a §10 drift
section (`settings.py:85`), and the delegate's scheduler takes `budgets=settings.config.budgets`
(`cli.py:4235`). So the live operator path to un-breach a wave is:

> edit `budgets.wave_max_wallclock_s` in `config/fleet.yaml`, then
> `fleet resume --accept-drift budgets`

It is not a **flag** — there is no `--raise-wave-wallclock`, and `--raise-wave-budget` raises
`waves.max_usd` only (`cli.py:11937`, `_raise_wave_ceiling`). But the capability exists today, is
audited (each accepted section writes its own `ConfigDrift` finding, `cli.py:10237-10238`), and it
is *fleet-wide* rather than per-wave, which is a real difference from `--raise-wave-budget`.

**Attribution note:** the premise as R4/R1 stated it ("`fleet resume` has no wall-clock raise") is
true of the flag surface and false of the operator surface. Neither lane's sentence distinguished
them. Correcting it is what changes the disposition space below.

## 1.6 A contradiction in `src/` that a step-8 ADR must not inherit

`src/fleet/orchestrator/runner.py:107-108`:

> *"§3.4: a per-wave wall-clock breach is the same fail-closed path as a budget breach, and it is
> a RESUMABLE state — the breached wave is left `PARTIAL` and **re-admitted by `fleet resume`**."*

`tests/test_scheduler.py:15-16` and the docstring of
`test_a_wall_clock_breach_withholds_members_as_pending_and_leaves_the_wave_partial`
(`tests/test_scheduler.py:300-304`) make the same claim: *"the wave is `PARTIAL` so `fleet resume`
re-admits exactly them in the same order."*

Both are **false at `12ac784`**, on two independent counts: step 8 does not exist (nothing
re-admits at all), and even once it does, §1.2 row C shows the same breach recurs on every resume
unless `budgets` drift is accepted. `docs/SPEC.md:1473` is the honest statement and does **not**
make the resume claim — it says only *"exits with code 4 — the same fail-closed path as a budget
breach"*, and separately that the clock is *"cumulative across resumes — a resume continues the
wave's clock, it never restarts it."* **The SPEC and the code agree; two comments in `src/` and
`tests/` do not.** Per CLAUDE.md Guardrail 7, these are the sentences a step-8 author would
reconcile against, so they are two edits the step-8 change owes — I have **not** made them.

---

## 1.7 Candidate dispositions for S8-8, with costs — and what each depends on

I am **not** ruling. Three of these turn on facts the tree does not settle; I name which.

### The undetermined part, stated exactly

The delegate's contract is determined (§1.4: it returns 4). What is **not** determined is step 8's
**aggregation** rule across three roots, and there is no precedent in the tree to copy: every
existing exit-code aggregation is *within one phase* over a list of waves
(`cli.py:4511`, `cli.py:8322` — `next((r.exit_code for r in reports if r.exit_code is not None), None)`).
Step 8 is the first caller that fans out across phases. `[UNVERIFIED]` that any SPEC sentence
constrains it — I found none; `docs/SPEC.md:6646`'s §10 row says only *"continue from each repo's
re-entry floor"* and names no exit code for the continuation.

### D-4a — **Step 8 propagates the first non-zero delegate exit code**

Cost: `fleet resume` on a fleet with one breached wave exits **4** with the message `'None'`
(§1.4), after having mutated every member's git state (§1.4). The operator's remedy is §1.5's
config edit. Depends on: nothing further — this is what the delegates already do. It is the
cheapest to implement and the one that inherits both §1.4 defects.

### D-4b — **Step 8 refuses to enter a breached wave, before any delegate runs**

A pre-flight `breached(wave)` check per (phase, wave) at the top of step 8, refusing with a payload
key naming the wave, its `elapsed_s`, the ceiling, and the `--accept-drift budgets` remedy.
Cost: one extra read per wave; **no** git mutation on a wave that cannot admit; the exit code is
step 8's own choice rather than a delegate's. This is the disposition §1.3 argues for most strongly:
under D-4a a fleet whose wave 0 breached in TRANSFORM would have BUILD and VERIFY each prepare and
then withhold every repo, three times over. Depends on: choosing an exit code (see below) and on
whether the orchestrator wants step 8 to own a refusal at all — ruling C already set the precedent
that step 8 takes guards the `_impl`s bypass, so this is the same shape.

### D-4c — **Step 8 skips a breached wave, reports it loudly, and continues to the next**

Cost: cannot work, and this is measured, not argued. `open_wave` refuses to open **any** later
wave while an earlier one is not `CLOSED` (§1.2 row D, `scheduler.py:437-448`) — that is the whole
ordering guarantee. So "skip and continue" is unavailable at the wave level. It is only available
*per phase*: skip BUILD of wave 0 and still run, say, TRANSFORM of wave 0 — which §1.3 shows is
also breached. **I recommend the orchestrator not spend a round on this one.**

### The exit code for D-4b, and why I am not choosing it

Four candidates, none of which the tree settles:

* **4** — matches `WAVE_WALLCLOCK_EXIT_CODE` and what the delegate would have returned anyway.
  Argument against: `docs/SPEC.md:1473` scopes 4 to *"the runner stops admitting new repos, drains
  in-flight work … checkpoints, and exits"*, and step 8's refusal drains nothing and checkpoints
  nothing. Reusing it makes "exit 4" mean two different things.
* **2 (`ResumeIncompleteError`)** — `cli.py:10105-10107` already reserves exit 2 for
  "reconciled, and deliberately did not continue", and its docstring says why it is *"deliberately
  not exit 1"* (nothing failed). A breached wave is exactly "reconciled, cannot continue". This is
  the closest existing semantics. Argument against: 34 tests already assert exit 2 from `resume`
  (per ruling A's re-measurement of R4), so adding a *second* cause of exit 2 makes those tests
  less discriminating unless the payload key is asserted on.
* **0 with a loud payload key** — parallel to ruling B's D-2 for `SCAN`. Argument against: this
  is literally the "exits 0 on a fleet that cannot move" the brief set out to avoid, and unlike
  the `SCAN` case there is nothing else in the fleet that *can* move (§1.3: every phase of that
  wave is breached, and every later wave is refused by `open_wave`).
* **A new exit code.** Argument against: `cli.ExitCode` stops at 7 and `runner.py:110-113` records
  the project's own rule against inventing members into another module's enum from outside.

**My reading, offered as an Agent Recommendation and not a ruling:** D-4b with exit **2** and a
loud payload key is the disposition most consistent with what is already in the tree — it reuses
the exit code `resume` already owns for "reconciled, did not continue", it avoids the
prepare-then-withhold waste, and it puts the `--accept-drift budgets` remedy in front of the
operator. The fact it depends on that the tree does **not** settle: whether the orchestrator is
willing to have two causes of `resume` exit 2, which is a scope question about 10e's 34/36-test
migration, not a fact I can measure.

### One thing that is NOT open

Whatever disposition is chosen, **the step-8 change owes the two comment corrections in §1.6.**
`runner.py:107-108` and `tests/test_scheduler.py:15-16` assert that `fleet resume` re-admits a
breached wave. Under D-4a that becomes true-ish only after the operator raises the ceiling; under
D-4b and D-4c it stays false. "The SPEC says X but the code cannot do X is two edits, not one"
applies to a *comment* the same way — and here the SPEC is already right, so the edits are in
`src/` and `tests/`, not in `docs/`.

---

# TASK 2 — the `stub_reconcile` workstream, costed

## 2.1 R1's framing, verified independently — and it is right, with **one material correction**

**Verified, two ways.** (a) `grep -rn` for any import of `fleet.orchestrator.stubs` across `src/`
and `tests/` returns **exactly one** site: `tests/test_stubs.py:42`. Zero in `src/`. (b) `grep -rn
'\breconcile\b' src/` returns 15 hits, of which **every one inside `src/fleet/orchestrator/stubs.py`
is a definition or a docstring** and every one outside it is unrelated prose
(`vcs/commits.py:303`, `orchestrator/budgets.py:674`, `runner.py:308`, `cli.py:10254`, `:10949`).
`[probe @ 12ac784]` This reproduces D80's own table (`docs/INTEGRATION_HONESTY.md:5313`).

**`reconcile` is complete and pure.** `src/fleet/orchestrator/stubs.py:554-646`. It takes
`(rows: Iterable[StubRecord], providers: Mapping[RepoId, ProviderFacts], *, now, open_pr_max_age_s)`
and returns a `ReconcileOutcome` of `decisions` / `held_for_merge` / `degraded_consumers`. No SQL,
no config read, no clock of its own.

**I built the driver R1 describes and ran it end to end** (`probe_stub_driver.py`,
`[probe @ 12ac784]`). Real schema from `src/fleet/state/schema.sql`, four `stubs` rows under two
`stub_id`s, two `PullRequest` findings rows, read through `cli._pr_records` (`cli.py:8844`) and
`cli._rows`:

```
open StubRecords loaded: 3
  com.acme:widget  state=ACTIVE      consumers=['cons-a'] round=0
  com.acme:widget  state=SUPERSEDED  consumers=['cons-b'] round=1
  com.acme:gizmo   state=ACTIVE      consumers=['cons-a'] round=0
ProviderFacts from `_pr_records` (cli.py:8844): ['prov-open', 'prov-stale']

reconcile(...) -> decisions=1  held_for_merge=2  degraded=['cons-a']
  DECISION T4 cons-a com.acme:gizmo ACTIVE->ABANDONED reason=END_OF_RUN finding=UnresolvedStub
  HELD cons-a com.acme:widget (provider prov-open)
  HELD cons-b com.acme:widget (provider prov-open)
```

The whole read half is ~35 lines. **R1's "materially cheaper than R4 implies" reproduces.**

### The material correction: it is a driver **plus one guard**, not a bare driver

`[probe @ 12ac784]`, `probe_merged_hazard.py` — the same `reconcile`, one `ACTIVE` stub, provider
PR state varied:

| provider PR state | `merged` | `pr_open` | `reconcile` verdict |
|---|---|---|---|
| `DRAFTED` | False | True | held |
| `OPEN` | False | True | held |
| **`MERGED`** | **True** | **False** | **ABANDONED T4** |
| `HELD` | False | False | ABANDONED T4 |
| `None` (no PR) | False | False | ABANDONED T4 |
| provider absent from `providers` | — | — | ABANDONED T4 |

**`reconcile` abandons a stub whose provider PR is MERGED.** That is correct *only if* T1 has
already fired — a merged provider's rows should have moved `ACTIVE → SUPERSEDED → RESOLVED` and
would no longer be open. But **T1 has never fired anywhere**: `supersede` (`stubs.py:300`) has zero
`src/` callers, same as `reconcile`, and the `TaskKind.REVALIDATE` task `plan_revalidation`
(`stubs.py:365`) produces has **no worker** — `fleet stubs resolve` is
`_unavailable("stubs resolve", "src/fleet/workers/buildverify.py")` (`cli.py:12517`).

So a driver that wires **only** `reconcile` into `fleet resume` would, on a fleet where a
provider's PR merged, write `ABANDONED / END_OF_RUN / UnresolvedStub` and drive the run to exit 7
for a consumer whose dependency **actually landed** — the "libel a working fleet" failure §13 row
45 exists to prevent, arriving by the door the carve-out does not guard. The carve-out guards
`pr_open`; this arrives through `merged`.

**Consequence for ruling A's cost:** ruling A's carve-out is still right, and R1's §E.6 finding is
still right. But branch (b) is **"driver + a merged-provider guard"**. The guard is small — skip a
stub whose `ProviderFacts.merged` is true and report it in a `pending_t1` payload key — but it is
not optional, and pretending it is would ship a correct-looking fix that libels a working fleet.
This is the finding I would most want the orchestrator to route back to R1 for a second
measurement.

### Two further loader hazards, both exercised

* **The loader must filter to `state IN ('ACTIVE','SUPERSEDED')` and group by `(stub_id, state,
  revalidation_round)`.** `reconcile` emits one decision per entry in `consumer_repo_ids`
  (`stubs.py:583-586`, `_consumers_of` at `:777`). A loader that aggregates *all* consumers of a
  `stub_id` into one `StubRecord` makes `reconcile` emit a decision for a consumer whose row is
  already `RESOLVED`. Measured: `decisions=3 consumers=['cons-a','cons-b','cons-done']`, where
  `cons-done` is `RESOLVED` in `stubs` and would be re-`ABANDONED` by the writer.
* **`ACTIVE` and `SUPERSEDED` rows under one `stub_id` take different transitions** (T4 vs T3,
  `stubs.py:597-599`), and one `StubRecord` carries one `state`. Grouping by `stub_id` alone
  cannot express both. My prototype's grouping key is
  `(stub_id, provider, coord, state, fidelity, pin, round, cap)` and produced the correct
  3-record split above.

### What `reconcile` reads from `ProviderFacts` — and what it does not

`_awaiting_merge` (`stubs.py:793-808`) reads only `pr_open` (i.e. `pr_state`) and `pr_created_at`.
**`ProviderFacts.status` is unread on the `reconcile` path.** So the driver's `providers` map needs
only what `_pr_records` already returns; a `status` must still be supplied truthfully (the field is
required) but must not be fabricated as `SUCCEEDED` to make anything happen.

### The config key, and the ratchet it releases — `[probe @ 12ac784]`

`reconcile`'s bound parameter is `open_pr_max_age_s` (`stubs.py:559`). The config key it must be
fed from is **`pr.merge_wait_timeout_s`** (`settings.py:702`, default `172_800`). There is **no**
`pr.open_pr_max_age_s` key: a whole-tree sweep for that string returns 8 hits, all of them the
*parameter* name plus three `docs/superpowers/plans/` notes recording that the parameter was named
that way **deliberately**, so the "config key is read" ratchet would not be tripped by a docstring
(`docs/PROGRESS.md:5754`).

**But `src/fleet/orchestrator/stubs.py:184` writes it as a config path** — *"so `reconcile` can
bound the §13 row 45 carve-out by `pr.open_pr_max_age_s`"* — naming a key that does not exist,
while `stubs.py:576` five hundred lines later gets it right (*"the `pr` section's merge-wait
timeout"*). That is a one-line false citation in `src/`, found by this lane and **not fixed**
(read-only lane; and it is not mine to sweep).

**The ratchet.** `tests/test_config_keys_are_read.py:196` carries
`"fleet.yaml:pr.merge_wait_timeout_s"` in `KNOWN_INERT`, and
`test_known_inert_keys_are_still_inert` **fails the day someone reads it**. So the driver commit
that passes `settings.config.pr.merge_wait_timeout_s` **must delete line 196 in the same commit**
or the suite goes red. The comment above that entry has partially rotted: it cites
*"a docstring (`cli.py:9027`)"*; a whitespace-normalised whole-file sweep finds the one `cli.py`
occurrence at **`cli.py:9211`**, not 9027. The claim (docstring-only, never a checked deadline)
still holds; the line number does not. `[probe @ 12ac784]`

## 2.2 Where it belongs in `_resume_impl`, with the ordering argument measured

**Position: immediately after the `--repoll-prs` block (`cli.py:10256-10265`) and above the step-3
sweep (`cli.py:10284-10290`).** The tree already argues this in a committed comment block at
`cli.py:10266-10282`, and that comment is **correct**. What follows is the measured half it does
not have.

**Why below the re-poll — measured, not asserted.** `_pr_records` (`cli.py:8844`) reads
`findings WHERE kind = 'PullRequest'`; `_pr_sync_impl` is the only writer of those rows
(`cli.py:8870`, `cli.py:9006`). The §2.1 table is the measurement: moving the driver above the
re-poll changes `providers[p].pr_state` from `MERGED` to `OPEN` for any provider merged since the
last observation, and the table shows that flips the verdict from **ABANDONED T4** to **held** —
i.e. it flips exit 7 on and off. This is the quantity the ordering moves, and it is not zero.

**Why above the step-3 sweep — measured, and the answer is: it does not matter, and the comment
should say so.** Step 3 (`_reset_stale_running`, `cli.py:10288-10292`) writes only `phases` rows;
`reconcile`'s inputs are `stubs` rows and `findings` rows of kind `PullRequest`, and
`ProviderFacts.status` is **unread** on the `reconcile` path (§2.1). So no quantity `reconcile`
consumes can differ either side of step 3. The position is right for readability and for §10's
"between step 6 and step 7" ordering, **not** because a value changes — and per the two retractions
already inside `_resume_impl` for exactly this error shape (`cli.py:10300`,
`cli.py:10317`), whoever lands it must say so rather than inherit an ordering argument that
does not hold. `[probe @ 12ac784]`

**The conditional the comment flags and does not settle** (`cli.py:10273-10282`): `repoll` is
`"not-requested"` on a plain `fleet resume`, `"skipped-dry-run"` under `--dry-run`, `"failed"` when
the forge refused. The §2.1 table shows what each means concretely: with stale PR state, a provider
that merged an hour ago still reads `OPEN` → **held**, not abandoned. So the **fail-safe direction
is to hold**, and gating the abandon half on `repoll == "polled"` is the conservative choice; making
`--repoll-prs` implied would break §11.5's "steps 1–7 make no network call" promise that
`--dry-run` rests on. That is a recommendation, not a ruling.

## 2.3 SPEC edits — "two edits, not one", by `file:line` and column

Measured column offsets on the single-line §10 table row (1-indexed; ruling B's "col 697" for the
re-entry-floor clause reads as 0-indexed against my 698 — same anchor, different convention):

| site | `file:line` col | text | needs an edit? |
|---|---|---|---|
| §10 `fleet resume` row | `docs/SPEC.md:6646` col **423** | *"**re-run `stub_reconcile`**"* | **No** once the driver lands — this half becomes true |
| §10 `fleet resume` row | `docs/SPEC.md:6646` col **529** | *"and **re-enqueue any revalidation round lost to the crash** under its `revalidation_key` — idempotent, so a resume never doubles the rework"* | **YES.** `reconcile` does not enqueue anything; enqueueing is `plan_revalidation` (`stubs.py:365`), whose `TaskKind.REVALIDATE` task **no worker consumes** (`cli.py:12517` refuses `fleet stubs resolve` as unavailable). Landing the driver without this edit leaves §10 asserting a capability the change does not deliver, and a reconciler would then *add* the enqueue to make code match spec — the `supports_effort` failure shape CLAUDE.md records |
| §13 row 35 | `docs/SPEC.md:7402` col **269** | *"`stub_reconcile` runs before the final checkpoint **and again in `fleet resume`**"* | **Split.** The `fleet resume` half becomes true. The *"before the final checkpoint"* half stays false — nothing calls `reconcile` at end of run either. If the workstream is resume-only, this sentence needs the end-of-run half marked as still absent, or D80's successor must say the row is now **half** delivered |
| §3.5.1 | `docs/SPEC.md:1837` | *"Before the runner writes its final checkpoint it executes a `stub_reconcile` step (also part of `fleet resume`'s reconciliation, §11.5)"* | **Same split as row 35.** The parenthetical becomes true; the main clause stays false |
| §11.5's numbered list | `docs/SPEC.md` §11.5 closing paragraph (*"… (7) regenerate `migration_state.json`; (8) continue"*) | — | **Open question, and D80 §"the one site that does not" is the record of it.** §11.5 has no stub step. Either the list gains one (renumbering step 8, which collides with subtask 10) or §10's row is scoped as "activities, not numbered steps". **I recommend the second and I am not ruling it** — renumbering step 8 mid-subtask-10 is the expensive option |

## 2.4 Dispatch-ready decomposition — S1–S7

Shape follows `docs/superpowers/plans/llm-cache-inert-research.md` §7's C1–C7 table, plus the
dependencies column the brief asked for. Every success criterion is a behavioural/exercised check.
**ADR and D numbers are the orchestrator's to allocate; I have named none.**

| # | Goal | Files | Success criterion (behavioural — exercised, never a declaration read) | Test file | Depends on |
|---|---|---|---|---|---|
| **S1** | **The loader.** `_open_stub_records(conn, run_id) -> list[StubRecord]`: read `stubs WHERE run_id = ? AND state IN ('ACTIVE','SUPERSEDED')`, group by `(stub_id, state, revalidation_round)`, aggregate `consumer_repo_id` into `consumer_repo_ids`. Read-only; no writer. | `src/fleet/cli.py` (beside `_pr_records`, `cli.py:8844`) | Seed the §2.1 four-row fixture — one `stub_id` with an `ACTIVE`, a `SUPERSEDED` and a `RESOLVED` consumer row. The loader returns **exactly 3** records, and **no returned `consumer_repo_ids` contains the `RESOLVED` consumer**. The discriminating mutation is **dropping the `state IN (…)` clause**: the count goes 3 → 4 and `cons-done` appears — assert on *which consumer is absent*, never on the count alone, because the count also moves under the grouping-key mutation. | `tests/test_resume_stub_reconcile.py` (new) | none |
| **S2** | **The provider facts.** `_provider_facts(conn, run_id) -> dict[RepoId, ProviderFacts]` from `_pr_records` + the provider's `phases` status. Do **not** fabricate `SUCCEEDED`. | `src/fleet/cli.py` | With two `PullRequest` findings rows (`OPEN` 3 h old, `OPEN` 9 days old) the returned map has `pr_state`/`pr_created_at` matching the persisted drafts byte-for-byte, and `reconcile(..., now, open_pr_max_age_s=172_800)` **holds the fresh one and sweeps the stale one** — the §12.38 bound exercised through the real values, not through a literal. | `tests/test_resume_stub_reconcile.py` | S1 |
| **S3** | **The merged-provider guard (§2.1).** Skip any open stub whose `ProviderFacts.merged` is true; return them in a `pending_t1` list rather than abandoning. This is the correction to ruling A's "it is just a driver". | `src/fleet/cli.py` | With provider `status=SUCCEEDED, pr_state=MERGED`, the driver emits **0 `UnresolvedStub` findings, 0 `ABANDONED` rows**, and the payload's `pending_t1` names the consumer. **Old-passes/new-fails:** remove the guard and the same fixture writes `ABANDONED/END_OF_RUN` — which is `probe_merged_hazard.py`'s measured row 3, so the mutation is already known to discriminate. | `tests/test_resume_stub_reconcile.py` | S1, S2 |
| **S4** | **The writer.** One `StateWriter` unit applying each `StubDecision`: `UPDATE stubs SET state, abandon_reason, state_changed_at, resolved_at = COALESCE(resolved_at, ?)` + an idempotent `INSERT INTO findings … ON CONFLICT … DO UPDATE`. **Copy the shape of `fleet stubs abandon` (`cli.py:12532-12600`), which already does exactly this** — including the `resolved_at` COALESCE that `schema.sql:392`'s CHECK demands. Honour `dry_run`. | `src/fleet/cli.py` | **Idempotency, exercised:** run the driver **twice** over the same DB; after the second run the `stubs` rows and the `findings` rows are **byte-identical** to after the first (compare `state`, `abandon_reason`, `state_changed_at`, and the finding `payload`), and no `UNIQUE` violation is raised. And `--dry-run` leaves **0** rows changed while the payload reports the same decision list. | `tests/test_resume_stub_reconcile.py` | S1–S3 |
| **S5** | **Wire it into `_resume_impl`** on the line the committed comment reserves (`cli.py:10283`), gated on `repoll == "polled"` for the abandon half per §2.2. **Replace** the comment block with one that (a) keeps the below-the-re-poll argument, (b) states that the above-step-3 position moves **no** quantity, per §2.2, and (c) records the gating decision explicitly rather than leaving it open. Add payload keys `stub_reconcile` (decisions), `stub_reconcile_held` and `pending_t1`. | `src/fleet/cli.py` | Drive `fleet resume --json --repoll-prs` end to end over the §2.1 fixture and assert **on the payload**: the abandoned consumer, the held consumer, the `pending_t1` consumer. Then re-run **without** `--repoll-prs` and assert the abandon half did **not** run while the payload still reports why. Assert exit **7** appears only when an `UnresolvedStub` was actually written. | `tests/test_resume_stub_reconcile.py` + `tests/test_cli.py` | S1–S4; **and ruling on 10e's exit-code scope** if exit 7 now becomes reachable from `resume`, which 34 existing `resume` exit-2 tests do not expect |
| **S6** | **Release the ratchet and fix the citations.** Delete `"fleet.yaml:pr.merge_wait_timeout_s"` (`tests/test_config_keys_are_read.py:196`) in the **same commit** as S5. Correct `src/fleet/orchestrator/stubs.py:184`'s `pr.open_pr_max_age_s` → the `pr` section's merge-wait timeout (matching `stubs.py:576`). Re-anchor the KNOWN_INERT comment's `cli.py:9027` → `cli.py:9211`. | `tests/test_config_keys_are_read.py`, `src/fleet/orchestrator/stubs.py` | `test_every_config_key_is_read` **and** `test_known_inert_keys_are_still_inert` both pass with `pr.merge_wait_timeout_s` reported *read* by `cli.py`. **Old-passes/new-fails:** re-run the scan against pre-S5 `cli.py` and the key must come back unexplained. Sweep for `open_pr_max_age_s` as a **config path** (`pr.` prefix) across the whole tree, normalised, and report the class result. | itself | S5 |
| **S7** | **The SPEC edits of §2.3, in one commit, by one author, with an exact-match replacer that aborts on mismatch.** `docs/SPEC.md:6646` col 529 (the re-enqueue clause), `:7402` col 269 and `:1837` (both split into a delivered half and a still-absent half). **Do not** touch `:6646` col 423. | `docs/SPEC.md` | Class result: every SPEC sentence asserting `stub_reconcile` runs in `fleet resume` is true after S5, and **every** sentence asserting it enqueues revalidation or runs at end of run is marked as not delivered. Anchor the census on text **older than the correction**, fail by `file:line`, and re-run it **against the artefact the fix produced**, not against the finding. | consider a prose-binding test in the `tests/test_floor_rule_statements.py` shape | S5 |

**Explicitly out of scope, and why:** `supersede`/T1, `plan_revalidation`, `settle_revalidation`
and the `TaskKind.REVALIDATE` worker. Wiring T1 without a revalidation worker moves stubs
`ACTIVE → SUPERSEDED` and then S4's next pass T3-abandons them — strictly worse than S3's guard,
which reports `pending_t1` and touches nothing.

## 2.5 What D80 should say once this is owned — **draft text only; do not apply**

Per CLAUDE.md Guardrail 7, D80's **body** is a record of what was true at `9bf15bb`/`f36c9ad` and
must be annotated, never rewritten. The **heading** is a field and moves on fix. I have **not**
edited `docs/INTEGRATION_HONESTY.md`; D-numbers and ledger edits are the orchestrator's.

Suggested, for the orchestrator to adjudicate:

* **Heading, while the workstream is open:** stays `OPEN`, but drop **`and UNOWNED`** — ruling A
  gives it an owner (its own workstream). That is a heading edit, not a body rewrite.
* **A dated in-file marker in the body**, naming this lane's commit, carrying three things:
  1. **R1's §E.6 finding, upgraded from "a driver" to "a driver plus a merged-provider guard"**,
     with `probe_merged_hazard.py`'s five-row table as the evidence — so the next round starts from
     the true cost rather than re-deriving it.
  2. **The two loader hazards** of §2.1 (filter to open rows; group by `(stub_id, state, round)`),
     because a driver written without them ships a correct-looking fix that re-abandons resolved
     rows.
  3. **The ratchet dependency**: `tests/test_config_keys_are_read.py:196` must be deleted in the
     same commit that reads `pr.merge_wait_timeout_s`, or the suite goes red. D80's cost section
     currently does not mention it.
* **On fix, the heading becomes `PARTLY ADDRESSED`, not `FIXED, LANDED`** — S5 delivers §10's
  *"re-run `stub_reconcile`"* clause and leaves both the **end-of-run** call (§13 row 35, §3.5.1)
  and the **re-enqueue** clause (§10 col 529) undelivered. `PARTLY ADDRESSED` is the true value and
  the ledger's vocabulary block already provides it; D58 is the landed precedent (two of five
  `llm.failover.*` leaves wired, three still `KNOWN_INERT`).
* **The open question D80 declines to answer** (`repoll != "polled"`) is answered by S5's gating
  decision and by §2.2's measurement, so the marker should say which way it went and why the
  fail-safe direction is *hold*.

---

# Claims I could not settle — stated as `[UNVERIFIED]`

1. `[UNVERIFIED]` Whether the cross-phase sharing of `wave_started_at` (§1.3) is intended. I found
   no SPEC sentence, ADR or comment addressing it either way. It is the single most
   decision-relevant fact for step 8 and nothing in the tree discusses it.
2. `[UNVERIFIED]` Whether `_build_impl` / `_verify_impl` have the same prepare-before-admit shape
   `_transform_impl` has (§1.4). I read only `_transform_impl`'s loop.
3. `[UNVERIFIED]` Whether any SPEC sentence constrains step 8's exit code across three delegates.
   I found none; `docs/SPEC.md:6646` names no exit code for the continuation.
4. `[UNVERIFIED]` Whether the `pending_t1` payload key S3 proposes has a consumer. It is a
   reporting-only key by design; nothing reads it today and nothing needs to.
5. `[ledger]` Not independently re-derived: D80's `115 .py blobs` / `62 tests blobs` figures. I
   re-derived only its **class** results (zero `src/` importers, one `tests/` importer), which
   reproduced exactly.
