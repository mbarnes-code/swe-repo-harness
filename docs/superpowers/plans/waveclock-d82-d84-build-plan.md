# Lane R1 (RESEARCH — lands no code, no tracked doc) — the D82 wave clock and the D84 premature git mutation

**Base:** `main` at `5f14ca0` (re-derived: `git rev-parse HEAD` = `5f14ca05175fbf7d95fbee9fa46c749de0039441`).
**Working tree was byte-identical to `5f14ca0` at lane start** (`git diff --stat 5f14ca0` empty,
`git status --porcelain` empty) — but sibling lanes are live, so **every claim below is anchored on
`git show 5f14ca0:<path>`, never on a working-tree read.** Where a claim is about a sibling branch it
says so; none is.

**No pytest session was run**, in the primary checkout or anywhere else. CR1 holds the primary
session. No worktree was made. Nothing was mutated; this lane wrote only under
`.superpowers/sdd/handoff-round-h/lanes/r1/` and its own scratch subdirectory
`…/scratchpad/roundh-r1/` (lane-private, never a shared filename).

**Instrument discipline.** Every count below states its predicate and its normaliser, and every
number that matters is derived two genuinely different ways (a whole-file **whitespace-normalised
regex with offsets mapped back to line numbers**, and an **`ast` walk**). Where the two disagree the
disagreement is reported, not averaged.

---

# 0. TL;DR for the dispatcher

* **D84 does NOT require D82.** D84's own body rules they be taken together; that ruling **does not
  hold mechanically**. §3 gives the reason. D84 should land **first**, alone, and it is the cheaper
  and louder of the two.
* **The one real coupling is in the TEST direction, and it is a Rule 12 trap.** A D84 fixture that
  breaches its wave *cross-phase* becomes a **no-op** the day D82 lands. §3.3 states the mandate that
  avoids it.
* **8 subtasks** (§4). Three are D84 and land in the stated order; five are D82.
* **Two rulings needed before dispatch** (§5), with recommendation and cost-if-wrong.
* **Handed-down numbers: 8 confirmed, 6 corrected** (§6). The corrections change the cost of two
  subtasks and add one subtask that did not exist in the handoff.

---

# 1. The re-derived `begin_wave` site list — from a sweep for the claim, not inherited

## 1.1 What was swept, and how

Scope: **182 `.py` files under `src/` and `tests/`** at `5f14ca0` (enumerated by
`git ls-tree -r --name-only 5f14ca0`, filtered to `.py` and to those two prefixes). Driver:
`…/scratchpad/roundh-r1/sweep_beginwave.py`, run as
`env -i PATH=/usr/bin:/bin HOME="$HOME" python3 <driver>` (a pure-stdlib `ast`/`re` reader over
`git show` output — it never imports `fleet`, so the worktree/editable-install hazard in CLAUDE.md
Rule 12 does not apply to it).

* **Instrument A — normalised regex.** Every file's text collapsed to single spaces across the whole
  file (so a line-wrapped `async def begin_wave(` cannot hide), offsets mapped back to line numbers.
  Predicates: `(?:async\s+)?def\s+begin_wave\b` for declarations, `\bbegin_wave\s*\(` minus the `def`
  forms for calls.
* **Instrument B — `ast`.** `FunctionDef`/`AsyncFunctionDef` with `name == "begin_wave"`; `Call`
  whose `func` is a `Name` or `Attribute` resolving to `begin_wave`.

**Blind spots differ:** A cannot tell a definition from a string containing one; B cannot see a
dynamically-constructed call (`getattr(store, "begin_wave")`). They agree exactly on both sets, which
is the point of running both.

## 1.2 The result

**CLASS RESULT — declarations: 4. CLASS RESULT — call sites: 4. Both instruments agree exactly.**

| # | `file:line` @ `5f14ca0` | kind | what it is |
|---|---|---|---|
| 1 | `src/fleet/orchestrator/scheduler.py:129` | **declaration** | `SchedulerStore` Protocol |
| 2 | `src/fleet/orchestrator/scheduler.py:234` | **declaration** | `SqliteSchedulerStore` — the real implementation, the `COALESCE` |
| 3 | `src/fleet/cli.py:1401` | **declaration** | `_ScanWaveStore` — the **in-memory SCAN stub** |
| 4 | `src/fleet/cli.py:3649` | **declaration** | `_ScopedWaveStore` — delegating wrapper |
| 5 | `src/fleet/cli.py:3650` | **call** | the body of #4 (`return await self._inner.begin_wave(...)`) |
| 6 | `src/fleet/orchestrator/scheduler.py:460` | **call** | `WaveScheduler.open_wave` — **the only `src/` caller**, and where `self.phase` is in hand |
| 7 | `tests/test_scheduler.py:485` | **call** | direct store call in a test |
| 8 | `tests/test_scheduler.py:487` | **call** | direct store call in a test |

**The handoff's "4 sites" is the DECLARATION class and is CONFIRMED as that. It is incomplete as a
change cost.** A phase parameter is a protocol change to 4 declarations **and** 4 call sites — **8
sites in 3 files**. `ast` further reports that **not one of the four declarations carries `**kwargs`**
(`vararg=False, kwarg=False` at all four; every one is exactly
`(self, run_id, wave_index, *, now)`), so a **required** keyword-only `phase` is a hard `TypeError`
at any caller left unadapted. That is the `7b2d48e` shape CLAUDE.md Guardrail 6 records — a
keyword-only parameter with no `**kwargs`, swept for callers from a reading taken earlier.

## 1.3 The SCAN stub — verified, and it is narrower and *safer* than the handoff implies

`src/fleet/cli.py:1370` declares `class _ScanWaveStore`, docstring *"`SchedulerStore` for the ONE
synthetic scan wave"*. Its `begin_wave` (`:1401-1404`) is:

* **in-memory** — `self._started: datetime | None`, set once, returned thereafter;
* **it writes NO `waves` row at all**, by deliberate design. Its own docstring gives the reason:
  *"writing a scan pseudo-wave there would leave a stale `wave_members` row for every repo
  `fleet sequence` later excludes."*

**The handoff says this site "has no `phases` table". That is true but understates it: it has no
`waves` row either**, which is what actually matters — `wave_started_phase` is a `waves` column, so
the stub has nowhere to put it and needs only a sibling in-memory `self._started_phase`. Two
consequences the build plan turns on:

1. **The SCAN wave's clock never persists**, so a SCAN wave can never be cross-phase breached. D82's
   defect is structurally unreachable through this store. The stub is a **conformance** cost of the
   protocol change, not a semantic one.
2. It is nonetheless the site that makes the change **non-uniform**: the other three declarations
   either forward or write SQL; this one branches on in-process state. An author who writes the
   `phases`-join predicate (the *rejected* cheap form of §2) has **nothing to read here** and must
   invent a decision. The `wave_started_phase` form degrades cleanly. **This is a second, independent
   argument for the expensive form**, and it is mechanical rather than aesthetic.

## 1.4 `wave_started_at`'s writer set — re-derived, because the whole D82 argument rests on it

Predicate: literal `wave_started_at`, whole-file whitespace-normalised, over `src/**/*.py` and
`src/**/*.sql` at `5f14ca0`. **Raw total: 24 occurrences.** Assignment-shaped contexts
(`SET wave_started_at =` or `wave_started_at =` not followed by `=`, within ±90 normalised chars):
**2 matches, both the same statement** — `src/fleet/orchestrator/scheduler.py:244`,
`UPDATE waves SET wave_started_at = COALESCE(wave_started_at, ?)`.

**CLASS RESULT: exactly ONE assignment site for `wave_started_at` in `src/`, and it is
first-write-wins. CONFIRMED.**

**The raw total does not reproduce and is not propagated.** The ledger records **20** at `b5f7760`
(line-oriented `grep -rn`), `git grep -c` over `src/` at `5f14ca0` sums to **22** by line, and this
lane's normalised occurrence count is **24**. Three predicates, three totals, one class result. Per
Guardrail 6 the class is the number that carries information.

---

# 2. D82 — what the change actually is, and its true blast radius

## 2.1 The column is additive-nullable-no-rebuild. **CONFIRMED, with precedent.**

`src/fleet/migrations/v007_logical_keys.py:59` is literally
`"ALTER TABLE waves  ADD COLUMN wave_started_at TEXT"` — the same table, the same shape, nullable, no
rebuild, no backfill, in a rung that also adds `synthetic` and `max_usd` to `waves`. So
`ALTER TABLE waves ADD COLUMN wave_started_phase INTEGER` is precedented **on this exact table**.
`src/fleet/migrations/_support.py` exposes `rebuild_table` for the hard cases; a v009 of this shape
never calls it.

## 2.2 But the CHANGE is not the column. It is a `SCHEMA_VERSION` bump, and that has three costs the handoff does not carry.

Adding any rung forces `SCHEMA_VERSION` 8 → 9, because
`tests/test_migrations.py:378` (`test_registry_is_strictly_ordered_contiguous_and_ends_at_the_baseline`)
asserts `versions == list(range(EARLIEST_MIGRATABLE_VERSION + 1, LATEST_VERSION + 1))` and
`tests/test_migrations.py:392` (`test_registry_target_matches_the_schema_sql_baseline`) asserts a
fresh `schema.sql` database reads `current_version == LATEST_VERSION`.

**Cost 1 — every command refuses until `fleet migrate-db`.** `src/fleet/cli.py:629-637`
(`_check_schema_version`) raises `UsageError` on `version != SCHEMA_VERSION` for *every* verb but
`migrate-db`. This one is loud, documented, and correct.

**Cost 2 — the bump SILENTLY invalidates every existing checkpoint. Nothing in the tree states
this.** Traced end to end at `5f14ca0`:

* `checkpoints.save` stamps the envelope with the compiled-in `SCHEMA_VERSION`
  (`src/fleet/state/checkpoints.py:106`, `:174` — both default `schema_version: int = SCHEMA_VERSION`).
* `checkpoints.load` returns
  `LoadedCheckpoint(rejection=CheckpointRejection.SCHEMA_VERSION_MISMATCH, …)` when
  `stored_version != schema_version` (`checkpoints.py:206-211`).
* `PhaseRunner._load_checkpoint` (`src/fleet/orchestrator/runner.py:1130-1138`) ends
  `return loaded.payload` — **it discards `loaded.rejection` entirely.**
* The caller (`runner.py:516`) therefore sees `checkpoint = None` and the repo **re-does every
  completed unit**, with no log line and no finding.
* **No migration rewrites checkpoint envelopes**: `git grep -n 'checkpoint' 5f14ca0 --
  src/fleet/migrations/` returns **0 hits**.

So "no backfill" is true of the *column* and false of the *change*: the rework is proportional to how
much work was checkpointed, it is invisible, and it is reachable by an operator following the
harness's own instruction (`Run \`fleet migrate-db\``). Under CLAUDE.md Rule 12's stop rule this is
**accidentally reachable, not adversarial**. It gets its own subtask (§4, D82-5) and it is the one
item in this plan that the handoff did not know about.

**Cost 3 — `docs/SPEC.md` embeds the constant.** `docs/SPEC.md:3447` carries
`SCHEMA_VERSION = 8   # == PRAGMA user_version (§6): 2 ADR-0019, 3 ADR-0021, …` — a **code listing
inside the SPEC**, which CLAUDE.md Guardrail 7 names as *"what the next author reconciles against"*.
It must move in the same commit as `src/fleet/models/state.py:16`. (Disclosed and out of scope:
`docs/SPEC.md:10` still says `schema_version = 4`. That is stale at `5f14ca0` independently of this
work — **report it, do not edit it**; it is not this change's class and editing a correct-when-written
sentence because it matched a grep is the mirror-image error CLAUDE.md records.)

## 2.3 The model boundary — a decision the author will hit, ruled here so they do not have to

`MigrationWave` (`src/fleet/models/graph.py:372`) **does** carry `wave_started_at`
(`graph.py:395`), and `src/fleet/state/projection.py` reads it twice. So the row and the model are
mirrored today and an author will reasonably ask whether `wave_started_phase` needs a model field.

**Agent Recommendation (not a directive): NO.** `MigrationWave` is the *plan* projection that
`migration_state.json` is built from; the phase that burned a wave's clock is *execution* state.
CLAUDE.md Guardrail 4 restricts databases to *"task queues, step statuses, worker heartbeats, and
timestamp logging"* and reserves the projection for the plan. Adding it to `MigrationWave` widens the
projection's contract for a fact no consumer of `migration_state.json` needs. **State the decision in
the ADR** so the next reconciler does not add the field to make the model match the row.

## 2.4 The SPEC prose that must move — anchored, and it is three sites plus a DDL listing

`docs/SPEC.md` forbids reset-on-resume in **three** places, all of which become *narrower* (per
`(run, wave, phase)` rather than per `(run, wave)`) rather than false:

| `file:line` @ `5f14ca0` | what it says |
|---|---|
| `docs/SPEC.md:1473` | §3.4 budget-table row — *"cumulative across resumes — a resume continues the wave's clock, it never restarts it"* |
| `docs/SPEC.md:1483-1491` | §3.4 prose (`f54dac8`) — *"Re-entry is therefore not re-admission"*; `wave_started_at` *"is stamped once and inherited"* |
| `docs/SPEC.md:4101-4103` | the `waves` DDL listing — *"PERSISTED, so `wave_max_wallclock_s` (§3.6) is cumulative across resumes rather than restarted by one"* |

Plus the source-of-truth copy of that last one: **`src/fleet/state/schema.sql:222-224`**, the inline
comment on the `wave_started_at` column itself, which says the same sentence. `docs/SPEC.md:5061`
(the `wave_members` lifecycle row, *"a rewrite PRESERVES `wave_started_at`, or a resequence would
restart a wave's wall clock"*) is a **different subject** — resequencing, not resuming — and is
**disclosed here rather than counted**; it needs no edit.

## 2.5 The SPEC never scopes the wave clock to a phase — re-derived, class result CONFIRMED

Predicate `wave_max_wallclock_s|wave_started_at|wall.?clock` (case-insensitive), **whole-file
whitespace-normalised** across `docs/SPEC.md` at `5f14ca0`, offsets mapped back to line numbers
(`…/scratchpad/roundh-r1/spec_sweep.py`).

* **TOTAL: 31 hits.** ← the handoff's `31` **CONFIRMED**, exactly, at a different SHA and by an
  independently written instrument.
* Hits carrying a `phase` token within ±220 normalised characters: **7**, over **6 distinct lines**
  (`:4101`, `:4688`, `:5061`, `:6172` ×2, `:6782`, `:7408`). **All 7 read individually:**
  * `:4101` — the `waves` DDL listing. No phase scope; it is the very sentence §2.4 says must move.
  * `:4688` — the `events` table's `ts TEXT -- wall clock, for humans only`, sitting beside an
    unrelated `phase INTEGER` column. Out of class.
  * `:5061` — the `wave_members` resequence row. Different subject (§2.4).
  * `:6172` ×2 and `:6782` — **`budgets.task_max_wallclock_s`**, a *different* budget that **is**
    per-phase by declaration (`scan: 600 / transform: 1800 / build: 1800`). It is the wave clock's
    per-*task* sibling, and it is per-phase on purpose.
  * `:7408` — §13's verify risk row, citing `budgets.wave_max_wallclock_s` with no phase scope.

**CLASS RESULT: 0 of 31 SPEC hits scope the WAVE clock to a phase, while its per-TASK sibling is
per-phase by declaration — so the shape is chosen, not overlooked. CONFIRMED, and this is what makes
D82 a DESIGN CHANGE requiring an ADR rather than a bug fix.**

**The sub-count does not reproduce and is not propagated.** The round-G ledger annotation reports
**8** phase-adjacent hits at `8b40498`; the identical stated predicate at `5f14ca0` yields **7** over
6 distinct lines. Guardrail 6's exact asymmetry — the class survives, the raw sub-total does not.

---

# 3. The D82/D84 ordering question, SETTLED

## 3.1 D84's body rules they be taken together. That ruling does not hold mechanically.

D84's closing paragraph reads: *"Whether preparation belongs per-admitted-repo rather than per-member
touches the same `admit` contract D82 records as undecided, and should be ruled on **with** D82, not
before it."* Checked against the primary source (the code), it does not:

1. **D84's guard reads a predicate D82 does not remove or reshape.** `WaveScheduler.breached`
   (`src/fleet/orchestrator/scheduler.py:432-441`) is
   `wave_started_at is None → False`, else `elapsed_s(wave) >= budgets.wave_max_wallclock_s`. D82
   adds a *column* and changes **when the stamp is refreshed**; it does not change `breached`'s
   signature, its inputs, or its existence. The guard compiles and means the same thing before and
   after.
2. **D84 survives D82.** After D82, a wave whose clock is spent **in the phase currently being
   driven** is still breached, and `_transform_impl` still prepares every member before `admit` runs.
   D82 shrinks D84's trigger set (it removes the cross-phase route); it does not close the defect.
   A defect that survives the other fix is not blocked on it.
3. **The dependency runs the other way, weakly.** D82's covering test wants a wave that is breached
   and *stays* breached across a phase change — which is easier to construct once D84's guard exists
   and gives a visible, countable effect. That is a convenience, not a requirement.

**Ruling (mechanical, not preferential): D84 lands FIRST and ALONE.** It needs no schema change, no
version bump, no ADR, and no migration; it is a guard in one file. Waiting for D82 costs at least two
waves and leaves the git mutation live throughout.

## 3.2 The mechanical fact the "cheap fix" description gets wrong — and it changes D84-1's cost

The ledger's round-G annotation says: *"The primitive the cheap fix needs already exists and is
already public: `WaveScheduler.breached` (`scheduler.py:430-437`). Nothing is missing from
`scheduler.py` or `runner.py`; the fix is a guard in `cli.py`."*

**The second sentence is correct. The first is incomplete, and the gap is at the call site.**
`ast` sweep of `src/fleet/cli.py` at `5f14ca0` for `WaveScheduler(` constructions, each resolved to
its enclosing function:

| line | enclosing function | `phase=` |
|---|---|---|
| `1929` | `_run_scan_wave` | `Phase.SCAN` |
| `4242` | `_run_transform_wave` | `Phase.TRANSFORM` |
| `7716` | `_run_build_wave` | `Phase.BUILD` |
| `7793` | `_run_verify_wave` | `Phase.VERIFY` |

**All four schedulers are constructed INSIDE the `_run_*_wave` helpers — which are called AFTER the
prepare loops they would have to guard.** `_transform_impl` calls `_prepare_repo` at `cli.py:4455`
and `_run_transform_wave` at `:4470`; `_verify_impl` calls `_prepare_verify` at `:8477` and
`_run_verify_wave` at `:8497`. **There is no scheduler object in scope at `:4455` or `:8477`.**

So the guard is *not* a one-line `if await scheduler.breached(index): continue`. It is one of:

* **(a) construct the breach reader in the `_impl`.** Verified feasible: `_transform_impl` already
  holds `writer` (`cli.py:4412`), `read_conn` (`:4413`), `repository` (`:4415`), plus `settings`,
  `run_id` and `only` — every input `WaveScheduler(...)` takes at `:4242`. **Recommended.**
* **(b) re-derive `elapsed >= ceiling` inline in `cli.py`.** **Rejected**, and the rejection belongs
  in the subtask brief: it makes a second copy of one predicate, and CLAUDE.md's whole
  wrong-text-propagates-by-copy class is exactly this.

**Concretely: add one module-level helper in `cli.py`** —
`async def _wave_is_breached(settings, *, writer, read_conn, repository, run_id, wave_index, phase, only) -> bool`
— which constructs the same `WaveScheduler` the `_run_*_wave` helper will construct and returns
`await scheduler.breached(wave_index)`. One definition, two call sites, **zero** duplicated formula.

## 3.3 The one real coupling, and it is a Rule 12 trap — MANDATORY for D84's fixtures

**A D84 fixture that produces its breach CROSS-PHASE becomes a no-op the day D82 lands.** Stamp the
clock in TRANSFORM, drive BUILD, assert the guard fires: correct today, and after D82's re-stamp
predicate lands, BUILD's `open_wave` re-stamps the wave, `breached` reads `False`, the guard never
fires, and **the test passes under the exact defect it exists to catch**. That is CLAUDE.md Rule 12's
*"two-anchor test whose two anchors coincide"* shape, arriving from the future rather than from the
fixture.

**MANDATE for D84-1 and D84-2: breach the wave in the SAME phase the fixture then drives.** Stamp
`waves.wave_started_at` far enough in the past against `budgets.wave_max_wallclock_s`, and drive
*that* phase. That predicate is D82-invariant in both dispositions: a same-phase resume never
re-stamps under either the `phases.started_at` form or the `wave_started_phase` form (both keep, per
the round-G 4-of-4 table). **A D84 subtask that lands a cross-phase fixture must be sent back.**

---

# 4. The decomposition — 8 subtasks, each landable by one subagent in one round

**Dispatch constraint that overrides parallelism.** D84-1, D84-2, D82-3 and D82-5 all edit
`src/fleet/cli.py`. Per CLAUDE.md §6, `git add <path>` is not sufficient granularity under concurrent
lanes, and four lanes measured four separate faces of that hazard on this file. **Never dispatch two
of these in the same wave.** The ordering below is a dependency order *and* a serialization order.

Each row's success criterion is a **behavioural, exercised** check — never a declaration read.

## Track D84 — lands first, independent of D82

### D84-1 — the breach guard in `_transform_impl` and `_verify_impl`, one commit, one author

* **Goal.** Add `_wave_is_breached(...)` (§3.2 form (a)) to `src/fleet/cli.py` and gate **both**
  unconditional per-member prepare loops on it: `_transform_impl`'s `_prepare_repo` loop
  (`cli.py:4455`, inside the loop opened at `:4452`) and `_verify_impl`'s `_prepare_verify` loop
  (`cli.py:8477`). On a breached wave, skip the preparation and fall straight through to
  `_run_*_wave`, which then produces the same exit-4 `WaveReport` it produces today.
* **Why both in ONE commit.** They are one class with one remedy. CLAUDE.md: *"one author takes every
  site in one commit, with an exact-match replacer that aborts on mismatch"* — a multi-site
  correction split across authors ships partial wording.
* **Explicitly NOT in scope.** Changing what `admit` returns, changing the exit code, or touching
  `scheduler.py`/`runner.py`. The observable behaviour of a breached wave (exit 4, `admitted=()`,
  members `PENDING`, `attempts=0`) must be **byte-identical** before and after. Only the git side
  effects disappear.
* **Files.** `src/fleet/cli.py`; `tests/test_prepare_before_admit.py` (**new file** — see
  expressibility below).
* **Success criterion (exercised, git-side).** Reproduce W2's round-G repro over
  `tests/test_transform_e2e.py`'s fixture with `waves.wave_started_at` stamped in the past **in
  TRANSFORM, driving TRANSFORM** (§3.3): the run still exits **4** with **0 admitted** and both
  members `PENDING`, **and** the count of member worktrees carrying
  `refs/fleet/<run>/<repo>/phase-2/base` reads **0** where it read **2** before the fix, with
  `phases.base_ref` NULL for both. The same shape for VERIFY over the verify fixture, watching the
  worktrees `_prepare_verify` would have created.
* **The watched quantity, and why it is the right one.** A **git ref counted per worktree**, not a
  database digest. `_prepare_repo`'s step 4 is its sole creator, so it moves 2→0 exactly with the
  ordering and cannot move for any other reason. (This is W2's choice and it is correct; the round-F
  lesson about a `sha256(whole file)` digest reading 4 where it must read 0 is precisely why a
  digest is refused here.)
* **Rule 12 — the discriminating mutation.** Delete the guard (or invert it to `return False`). The
  *old* assertions — every existing transform/verify e2e test — **pass** under that mutation, because
  none of them asserts on work performed before an empty admission. The *new* assertion **fails**,
  reading 2 where it must read 0. That is old-passes/new-fails on one input. **Gate the mutation
  harness on `git diff --numstat --no-index BACKUP MUTATED` reporting non-zero, and read that gate
  BEFORE the test result.**
* **Expressibility — measured, and it is why this is a new file.** No existing test can express this.
  `ast` sweep of `tests/` at `5f14ca0` for `WaveScheduler(`: **5 construction sites across 4 files**
  (`test_cli.py:4989` `Phase.BUILD`; `test_runner.py:418` `PHASE`; `test_scheduler.py:142` and `:240`
  `PHASE`; `test_step6_wave_write.py:356` `Phase.TRANSFORM`), and **0 of the 4 files construct more
  than one distinct `phase` expression.** But the *primitive* a fixture needs is already reachable:
  `tests/test_scheduler.py:485` calls `await store.begin_wave(RUN, 0, now=clock())` directly, so a
  fixture can stamp a wave's clock without going through a phase at all.
* **Depends on.** Nothing.

### D84-2 — `_build_impl`: adjudicate the two pre-admission blocks, then implement or disclose

* **Goal.** BUILD is in D84's class but **differently**, and the ledger's annotation names only half
  of it. `ast` at `5f14ca0` finds **two** pre-admission git-mutating blocks in `_build_impl`
  (def `cli.py:7898-8364`):

  | block | lines | position | guard |
  |---|---|---|---|
  | fleet-wide "PASS 2" | `_wave_snapshot` `:8074`, `_plan_build` `:8079` | **OUTSIDE the wave loop** — once, over `ingests.items()`, before any wave opens | none (`if ingests:`) |
  | in-loop re-cut | `_wave_snapshot` `:8237`, `_plan_build` `:8246` | inside the wave loop, before `_run_build_wave` `:8298` | `if published and snapshot is not None` (`:8231`) |

  `_plan_build` (`cli.py:7383-7477`) runs `worktree remove --force`, a `shutil.rmtree` fallback,
  `worktree prune` and `worktree add --detach --force` — real git mutation, both times.
* **The adjudication this subtask owns.** The in-loop block takes the same `breached(index)` guard as
  D84-1. **The PASS-2 block does not fit that shape at all** — it is not per-wave, so there is no
  `index` to ask about. Either (i) guard it on *"every wave is breached"* (a different, weaker
  predicate), or (ii) **document it as a stated boundary and do not patch it**. CLAUDE.md's stop
  rule: ask whether a normal author trips it. The plan's Agent Recommendation is **(ii) plus the
  in-loop guard** — a fleet-wide preparation pass before any wave opens is not "prepare a wave that
  cannot admit", it is "prepare the fleet", and conflating them buys the appearance of closure.
* **Files.** `src/fleet/cli.py`; `tests/test_prepare_before_admit.py` (extend D84-1's file).
* **Success criterion.** For the in-loop block: a two-wave build fixture where wave 0 publishes and
  wave 1's clock is spent **in BUILD** — the wave-1 re-cut does not run (`worktree add` count for
  wave-1 members reads 0 where it read N), exit code and admission unchanged. For PASS 2: **a
  disclosure sentence in the ADR and in the D84 closure**, not a patch, naming the block by
  `file:line` and saying why it is out of class.
* **Rule 12.** The in-loop guard's discriminating mutation is deleting it; the *old* assertions pass
  (no test drives a breached second build wave) and the new `worktree add` count fails. **Audit for
  expressibility first**: the fixture must reach `published and snapshot is not None`, which needs a
  *first* wave that actually published. A fixture that cannot reach that branch reports a pass that
  means nothing — this is the four-of-twelve shape CLAUDE.md records. **If the fixture cannot be
  built, return BLOCKED with the derivation; do not fabricate the battery.**
* **Depends on.** D84-1 (same file, same helper, same test module).

### D84-3 — close D84 in the prose and the ledger

* **Goal.** Update `_prepare_repo`'s and `_prepare_verify`'s docstrings to state the new
  precondition; set `docs/INTEGRATION_HONESTY.md`'s **D84 heading** — a *field*, per CLAUDE.md
  Guardrail 7 — to `FIXED, LANDED (<sha>)` if D84-2 patched the in-loop block, or **`PARTLY
  ADDRESSED`** if the PASS-2 block was disclosed rather than patched (which is the expected outcome,
  and `PARTLY ADDRESSED` is then the *true* value — D58 is the landed precedent). Add a **dated
  in-file marker** in the body; **rewrite not one word of the body.**
* **Files.** `src/fleet/cli.py` (docstrings only), `docs/INTEGRATION_HONESTY.md`.
* **Success criterion.** The heading field carries a fix status; a dated marker names the commit; a
  `git diff` of the body shows **only additions**. Re-run the check **against the artefact the fix
  produced**, not against the finding.
* **Depends on.** D84-1, D84-2.

## Track D82 — the wave clock scoped to a phase

### D82-1 — write `ADR-XXXX`

* **Goal.** The design decision, as a new ADR appended to `docs/DECISIONS.md`. **The number is left
  as the literal `ADR-XXXX` until the orchestrator allocates it at dispatch.** Heading form matches
  the file: `## ADR-XXXX — <title>`, then `**Status:**` / `**Context.**` / `**Decision.**` /
  `**Consequences.**` / `**Rejected.**` (the shape of `ADR-0093`, `docs/DECISIONS.md:11156`).
* **What the body must contain, and each of these is a fact this lane measured:** the §2.5 class
  result (0 of 31, and the per-*task* sibling being per-phase by declaration) as the reason this is a
  design change and not a bug fix; the §2.4 three SPEC sites that narrow; the §2.3 ruling that
  `MigrationWave` does **not** gain a field, with the Guardrail-4 reason; the §2.2 checkpoint cost;
  and **Rejected:** the `phases.started_at`-only form, with the round-G measurement that it is
  correct in **3 of 4** cases and the §1.3 second reason (the SCAN stub has nothing to read).
* **Files.** `docs/DECISIONS.md`.
* **Success criterion.** **Run the tree's own prose-binding instruments against the CANDIDATE text
  before writing it** — import the modules and call their helpers on a candidate copy, or run them
  through a symlink shadow tree. Specifically `tests/test_floor_rule_statements.py` and
  `tests/test_blocked_by_writer_statements.py`, and any test whose `_summary_sources()` reads
  `docs/DECISIONS.md` with a pinned expected count. A draft ADR that quotes a SPEC summary verbatim
  into `docs/DECISIONS.md` has already failed a pinned-at-zero equality once. This costs one import
  and no pytest session.
* **Depends on.** Ruling 2 (§5.2).

### D82-2 — the schema rung: `v009`, `SCHEMA_VERSION` 8→9, and the SPEC listing

* **Goal.** `src/fleet/migrations/v009_<slug>.py` with
  `"ALTER TABLE waves ADD COLUMN wave_started_phase INTEGER"` (§2.1 precedent); register it in
  `STEPS` (`src/fleet/migrations/__init__.py:109-133`); add the column to
  `src/fleet/state/schema.sql`'s `waves` DDL (`:218-238`) **and amend its neighbouring
  `wave_started_at` comment at `:222-224`** so the source-of-truth comment does not keep asserting
  the property the change narrows; bump `src/fleet/models/state.py:16` to `9` with the new line in
  its ADR index; and edit the **code listing** at `docs/SPEC.md:3447` in the same commit.
* **Files.** `src/fleet/migrations/v009_*.py` (new), `src/fleet/migrations/__init__.py`,
  `src/fleet/state/schema.sql`, `src/fleet/models/state.py`, `docs/SPEC.md`, `tests/test_migrations.py`.
* **Success criterion (exercised).** A v8 database populated with `waves` rows migrates to 9 with
  **every pre-existing cell byte-identical** and `wave_started_phase` NULL on every existing row; a
  freshly-initialised `schema.sql` database and a migrated one have **identical
  `PRAGMA table_info('waves')`** (the shape `test_the_reservations_table_a_migration_builds_matches_a_fresh_one`
  already uses, `tests/test_migrations.py:818`); `PRAGMA foreign_key_check` clean;
  `test_registry_is_strictly_ordered_contiguous_and_ends_at_the_baseline` and
  `test_registry_target_matches_the_schema_sql_baseline` both green.
* **Watch for.** `tests/test_migrations.py:328`'s `_v6_database(..., up_to: int = LATEST_VERSION - 1)`
  is parameterised on `LATEST_VERSION`; bumping it silently changes that default from 7 to 8. Read
  every use of that fixture before assuming green.
* **Depends on.** D82-1 (see Ruling 2 — this is the ADR's commit-mate).

### D82-3 — the `begin_wave` protocol change and the re-stamp predicate

* **Goal.** Thread the driving phase to the stamp, and re-stamp iff it differs. **All 8 sites of
  §1.2 in ONE commit** (4 declarations + 4 call sites); the `SqliteSchedulerStore` statement becomes
  the round-G two-column `COALESCE` so the two can never disagree; `WaveScheduler.open_wave`
  (`scheduler.py:460`) passes `self.phase`; `_ScanWaveStore` gains an in-memory `self._started_phase`;
  `_ScopedWaveStore` forwards. Re-stamp iff `wave_started_phase != <driving phase>`.
* **Signature form — decide and state it.** A **required** keyword-only `phase` gives a hard
  `TypeError` at any missed caller (§1.2: no declaration carries `**kwargs`) — loud, and per
  CLAUDE.md Rule 11 that is the right direction. An optional one with a default silently stamps a
  phase nobody wrote, which is the `effort: low` failure CLAUDE.md records verbatim. **Recommend
  required.**
* **Files.** `src/fleet/orchestrator/scheduler.py`, `src/fleet/cli.py`, `tests/test_scheduler.py`,
  `tests/test_wave_clock_is_phase_scoped.py` (**new file** — §1.2 and the expressibility measurement
  below).
* **Success criterion (exercised, and it is the round-G 4-of-4 table, re-run).** Two fixtures ×
  two events:
  * **NORMAL** (members claimed the phase): resume at the same phase → **keep**; transition to a new
    phase → **re-stamp**.
  * **EDGE** (`begin_wave` ran, **no** `acquire_phase_lease` ever did): resume at the same phase →
    **keep**; transition → **re-stamp**.
  All four correct. Plus: **`elapsed_s` within one phase is still cumulative across resumes** — a
  BUILD re-stamped, run 3 h, crashed and resumed still reads ≈3 h.
* **Rule 12 — per case, which mutation reddens it.** Report it in exactly this form; never report a
  case count.

  | mutation | NORMAL/resume | NORMAL/transition | EDGE/resume | EDGE/transition |
  |---|---|---|---|---|
  | predicate → the rejected `phases.started_at` COUNT form | green | green | **RED** | green |
  | `!=` → `==` in the re-stamp predicate | **RED** | **RED** | **RED** | green |
  | drop the second `COALESCE` (stamp phase unconditionally) | green | **RED** | green | **RED** |

  **The EDGE/resume case is the unique discriminator of mutation 1 — it is the entire reason the
  expensive form was chosen, and a fixture set without it certifies the rejected form GREEN.** That
  case is **expressible**: `await store.begin_wave(RUN, 0, now=T0)` with no lease acquisition leaves
  `waves.wave_started_at` non-NULL and every `phases.started_at` NULL, and
  `tests/test_scheduler.py:485` proves the primitive is already reachable from a fixture. Keep no
  case that is not the unique discriminator of at least one mutation.
* **Expressibility — the new file is mandatory, measured.** **5 `WaveScheduler(` sites across 4 test
  files at `5f14ca0`; 0 files construct more than one distinct `phase` expression** (D84-1's table).
  D82's defect needs two schedulers at different phases over **one** wave and **one** store. Adding
  cases to any existing file cannot reach it — this is a statement about expressibility, not about
  case count.
* **Depends on.** D82-2 (the column must exist).

### D82-4 — the SPEC prose reconciliation, one commit, one author, exact-match replacer

* **Goal.** The three §2.4 sites (`docs/SPEC.md:1473`, `:1483-1491`, `:4101-4103`) narrowed from
  per-`(run, wave)` to per-`(run, wave, phase)`. **Do not touch `:5061`** — different subject,
  disclosed in §2.4. **Do not touch `docs/SPEC.md:10`** — stale independently of this change; report
  it, do not edit it.
* **Files.** `docs/SPEC.md`.
* **Success criterion — a CLASS result, not a count.** Every SPEC sentence asserting the wave clock
  is cumulative across resumes now says *within a phase*; **0** sentences assert an unqualified
  cross-phase cumulative clock. Anchor the census on text **older than the correction**; fail by
  `file:line`, never by count; and **re-run it against the artefact the fix produced, not against the
  finding.** Expect the raw match total to go **up** if any correction quotes what it retires —
  subtract quotations-inside-retractions **by rule** and read the residue.
* **Use an exact-match replacer that ABORTS on mismatch.** These line numbers were re-derived at
  `5f14ca0` and `docs/SPEC.md` moves; re-anchor on the sentence text at the moment of the edit, per
  CLAUDE.md's re-measure-when-you-act rule.
* **Depends on.** D82-3.

### D82-5 — the checkpoint invalidation: measure it, then make it LOUD

* **Goal.** This is the item the handoff did not know about (§2.2 cost 2). `_load_checkpoint`
  (`src/fleet/orchestrator/runner.py:1130-1138`) returns `loaded.payload` and **discards
  `loaded.rejection`**, so a `SCHEMA_VERSION_MISMATCH` — which every `SCHEMA_VERSION` bump creates
  for every stored checkpoint — is indistinguishable from "no checkpoint", and the repo silently
  re-does its completed units.
* **First: exercise it, do not inherit it from this document.** Save a checkpoint under
  `schema_version=8`, load it with the loader at 9, and read what `_load_checkpoint` returns and what
  (if anything) is logged. **If the trace does not reproduce, report that and stop** — a finding true
  when filed can be false when you act on it.
* **Then: fail loud, per CLAUDE.md Rule 11.** Log the rejection reason (it is already carried in
  `LoadedCheckpoint.detail`) and/or write a finding, rather than silently returning `None`.
* **Files.** `src/fleet/orchestrator/runner.py`, `tests/test_checkpoints.py` or a new module;
  `docs/INTEGRATION_HONESTY.md` (a **new** entry — check first whether an existing entry already
  records it; round F allocated a D-number for a finding an existing entry already held).
* **Success criterion.** With a stored v8 envelope and a v9 loader, the run emits a distinguishable
  signal naming `SCHEMA_VERSION_MISMATCH` and the repo id. **Discriminating mutation:** revert to
  `return loaded.payload`; the old assertion (`checkpoint is None`) passes under both, the new one
  (the signal was emitted) fails only under the mutation.
* **Depends on.** Nothing mechanically. **But it must land in the SAME WAVE as D82-2**, because
  D82-2 is what makes the defect reachable in the field.

---

# 5. The two rulings the orchestrator must make

## 5.1 RULING 1 — Is `waves.wave_started_phase` additive-nullable-no-backfill *in fact*?

**Question.** The handoff asserts *"one additive nullable `waves.wave_started_phase`, no rebuild, no
backfill."* Checked against the **migration machinery**, not the assertion — does that hold?

**Measured answer: the COLUMN yes, the CHANGE no.**
* Yes, for the column: `v007_logical_keys.py:59` is the identical shape on the identical table
  (`ALTER TABLE waves ADD COLUMN wave_started_at TEXT`), additive, nullable, no `rebuild_table`, no
  data touched.
* No, for the change: it forces `SCHEMA_VERSION` 8→9 (`tests/test_migrations.py:378`, `:392` make the
  ladder contiguous and terminal), which (i) makes every verb but `migrate-db` refuse until the
  operator migrates (`cli.py:629-637` — loud and correct), (ii) **silently invalidates every stored
  checkpoint** (§2.2 cost 2 — the rejection is discarded at `runner.py:1130-1138`; **0** migrations
  touch checkpoint envelopes), and (iii) requires editing the SPEC's embedded code listing at
  `docs/SPEC.md:3447` in the same commit.

**Recommendation.** **Rule it IN — the expensive 4-of-4 form is the right one — and require D82-5 in
the same wave as D82-2.** Two independent mechanical arguments, not one: the round-G measurement that
the cheap form is wrong in 1 of 4 cases through an ordinary crash window, **and** §1.3's finding that
the cheap form has **nothing to read at the `_ScanWaveStore` site** and would force an invented
decision there.

**Cost if the recommendation is wrong** (i.e. if you rule "additive means free" and drop D82-5):
the first operator to run `fleet migrate-db` mid-run silently loses every repo's checkpoint and
re-executes completed units. No error, no log line, no finding — rework proportional to fleet size,
discoverable only by noticing repeated work. The ledger would then carry a "no-backfill" claim that
is **true of the column and false of the change**, which is the exact shape CLAUDE.md's measurement
guardrail exists to stop. Recovery costs at least one round, because nothing in the tree points at
it.

**Cost if the recommendation is wrong the other way** (rule the cheap `phases.started_at` form in
anyway, to avoid the bump): you ship a fix that is wrong in 1 of 4 cases through a reachable crash
window — a **defect** under Rule 12's stop rule, not a stated boundary — plus an undecided branch at
`_ScanWaveStore`. That is one wave to build and at least one more to retract.

## 5.2 RULING 2 — Must `ADR-XXXX` land BEFORE, WITH, or AFTER the code?

**Question.** Where in the sequence does the ADR go?

**Recommendation: WITH — specifically, `ADR-XXXX` in the SAME COMMIT as D82-2 (the schema rung and
version bump).** Not before, not after.

**Mechanical reason, not a preference.** The code line the bump must edit **cites the ADR number**.
`src/fleet/models/state.py:16-23` is an inline ADR index:
`SCHEMA_VERSION = 8   # == PRAGMA user_version (§6): 2 ADR-0019, 3 ADR-0021, 4 ADR-0022, 5 ADR-0023 …`,
mirrored into `docs/SPEC.md:3447`. Landing code first means writing `9 ADR-XXXX` against an ADR that
does not exist, or leaving the index short and hoping someone returns. Landing the ADR first means an
**ACCEPTED** ADR describing a schema version the tree does not have — the *"the SPEC says X but the
code cannot do X"* shape CLAUDE.md Guardrail 7 names, and the thing a reconciler then "fixes" in the
wrong direction.

**Disclosed counter-evidence, because it weakens the recommendation and you should have it.** That
index cites ADRs for versions **2, 3, 4, 5 and 6 only — versions 7 and 8 carry none.** So
"an ADR per rung" is a **convention with two exceptions**, not a rule, and nothing in `docs/SPEC.md`
§6 requires one. This recommendation is therefore an **Agent Recommendation** under CLAUDE.md's
directive-lineage guardrail, not a constraint I am entitled to call binding. What *is* measured, and
what does not depend on the convention, is §2.5: **0 of 31 SPEC hits scope the wave clock to a
phase**, so this is a design change to a SPEC-stated property, and those need a decision record
wherever the project's habit lies.

**Cost if wrong — ADR lands AFTER the code.** D82-3's author reconciles against a `docs/` that still
forbids what the code now does: the three sites at `docs/SPEC.md:1473`, `:1483-1491` and `:4101-4103`
all say the clock is cumulative across resumes and never restarts. A reconciler reading code-vs-spec
reverts the re-stamp to make code match spec — the `supports_effort` failure CLAUDE.md records, which
survived **six rounds** into a land blocker. Cost: the whole D82 track, plus the retraction.

**Cost if wrong — ADR lands BEFORE, as its own wave.** One wasted wave, and an `ACCEPTED` ADR with no
implementation, which the ledger then carries as a fifth open item. Cheaper than the other direction,
and recoverable, but it is a wave.

---

# 6. Handed-down numbers: what this lane confirmed and what it corrected

## CONFIRMED (re-derived at `5f14ca0` by an instrument written for this lane)

| claim | source | verdict |
|---|---|---|
| `begin_wave` protocol change costs **4 sites** | handoff §5 | **CONFIRMED as the DECLARATION class** — 4, two instruments agreeing exactly. See the correction below for what it omits. |
| **0 of 31** SPEC hits scope the wave clock to a phase | handoff §5 / D82 W4 marker | **CONFIRMED**, total *and* class, by an independently written normalised sweep. All 7 phase-adjacent hits read individually. |
| **3 of 3** wave-driving phase impls prepare before admit | D84 W4 marker | **CONFIRMED** by `ast` — `_transform_impl` `:4455`→`:4470`, `_verify_impl` `:8477`→`:8497`, `_build_impl` `:8237`/`:8246`→`:8298`. See the correction below for BUILD's shape. |
| one of the `begin_wave` sites is an **in-memory SCAN stub** | handoff §5 | **CONFIRMED** — `_ScanWaveStore` (`cli.py:1370`), `begin_wave` at `:1401`, `self._started`. Line number rotted from the handoff's `:1420`; the claim holds. |
| the column is **additive, nullable, no rebuild** | round-G research §5 | **CONFIRMED** — `v007_logical_keys.py:59` is the identical shape on the identical table. |
| **no test file** can express D82's cross-phase defect | D82 body | **CONFIRMED** — 5 `WaveScheduler(` sites / 4 test files / **0** files with more than one distinct `phase` expression. |
| `wave_started_at` has exactly **one** assignment in `src/` | D82 body | **CONFIRMED as a class result** — `scheduler.py:244`, first-write-wins `COALESCE`. |
| the ADR maximum in `docs/DECISIONS.md` is **0093** | orchestrator, `[probe @ 5f14ca0]` | **CONFIRMED** — form-agnostic `ADR-[0-9]{4}` union, 93 distinct values, max `ADR-0093` at `docs/DECISIONS.md:11156`. |

## CORRECTED

1. **"4 sites" is the declaration class and is incomplete as a change cost.** The same sweep returns
   **4 call sites** — `cli.py:3650`, `scheduler.py:460` (`WaveScheduler.open_wave`, the only `src/`
   caller and where `self.phase` lives), `tests/test_scheduler.py:485` and `:487`. **8 sites in 3
   files**, and `ast` reports **no declaration carries `**kwargs`**, so a required keyword-only
   `phase` is a hard `TypeError` at any missed caller. This is the `7b2d48e` shape CLAUDE.md records
   under "re-derive the caller set at the moment of the change".
2. **"The primitive the cheap fix needs already exists and is already public" is true about the
   primitive and incomplete about the CALL SITE.** All 4 `WaveScheduler(` constructions live inside
   the `_run_*_wave` helpers (`cli.py:1929`/`4242`/`7716`/`7793`), each called **after** the prepare
   loop it must guard. There is **no scheduler in scope** at `cli.py:4455` or `:8477`. D84-1's cost
   is a helper plus two guards, not a one-line poll. (The annotation's second sentence — *"the fix is
   a guard in `cli.py`"* — is untouched and correct.)
3. **"No backfill" is true of the column and understates the change.** The `SCHEMA_VERSION` 8→9 bump
   silently invalidates every stored checkpoint (§2.2 cost 2), and **no migration rewrites checkpoint
   envelopes** (0 hits). This adds subtask **D82-5**, which the handoff does not contain.
4. **BUILD has TWO pre-admission mutation blocks, not one.** The D84 annotation names only the
   in-loop one (`cli.py:8237`/`:8246`, guarded on `published and snapshot is not None`). `ast` at
   `5f14ca0` finds a second at `cli.py:8074`/`:8079` — the fleet-wide "PASS 2" block, **outside the
   wave loop entirely**, which the `breached(index)` guard shape does not fit. D84-2 owns the
   adjudication.
5. **The "8 phase-adjacent SPEC hits" sub-count re-measures to 7** (over 6 distinct lines) under the
   *identical stated predicate* at `5f14ca0`. Guardrail 6's asymmetry exactly: the class result
   survives in a stronger form, the raw sub-total does not. **Not propagated.**
6. **Citation drift from the handoff, re-anchored at `5f14ca0`:** `scheduler.py:124`→**`:129`**,
   `:229`→**`:234`**; `cli.py:1420`→**`:1401`**, `:3668`→**`:3649`**. Content unchanged at all four.
   D84's fix sites likewise: `_prepare_repo` loop `:4470-4487`→**`:4455`** (call),
   `_run_transform_wave` `:4489`→**`:4470`**, `_prepare_verify` `:8490-8498`→**`:8477`**,
   `_run_verify_wave` `:8510`→**`:8497`**.

---

# 7. Claims this lane could NOT settle — `[UNVERIFIED]`

1. `[UNVERIFIED]` **Whether cross-phase sharing of `wave_started_at` is INTENDED.** §2.5 measures
   that the SPEC never scopes it either way; that is a measurement of absence, not of intent, and it
   is why D82-1 is an ADR rather than a bug fix. Nothing in the tree supplies the intent.
2. `[UNVERIFIED]` **Every runtime figure attributed to round F/G lanes** — R2's `elapsed=864000.0`,
   the 4-of-4 disposition table, W2's `2 of 2` worktrees carrying anchor refs. **This lane ran no
   scheduler, no `fleet` command and no pytest session.** Those are cited, not re-executed; every
   figure I state as my own is a `git show`-backed `ast`/normalised-text read.
3. `[UNVERIFIED]` **The checkpoint-invalidation trace of §2.2 is source-derived, not exercised.** It
   is a five-hop read (`save` → envelope → `load` → `_load_checkpoint` → caller). D82-5's first step
   is to run it, **and to report a non-reproduction rather than implement this document.**
4. `[UNVERIFIED]` **Whether the D84-2 BUILD fixture is constructible.** Reaching
   `published and snapshot is not None` needs a first wave that actually published. If it is not
   reachable, that subtask should return **BLOCKED with the derivation**, not a fabricated battery.
5. `[UNVERIFIED]` **Whether any prose-binding test currently reads `docs/DECISIONS.md` with a pinned
   count** that `ADR-XXXX` would break. D82-1's success criterion is written to *find out by running
   the instruments against the candidate*, which is cheaper than the correction it replaces — but
   this lane did not enumerate those instruments.
6. `[UNVERIFIED]` **`docs/SPEC.md:10` (`schema_version = 4`) versus `:3447` (`SCHEMA_VERSION = 8`).**
   Observed in passing and **reported, not edited** — it is stale independently of this work and is
   not this change's class.

---

# Appendix A — draft body for `ADR-XXXX` (D82-1's deliverable)

**DRAFT ONLY. This lane landed no tracked document.** The number is deliberately the literal
`ADR-XXXX`: the orchestrator allocates every append-only shared identifier at dispatch, and none is
allocated this wave. **Do not "take the next number"** — the measured maximum at `5f14ca0` is
`ADR-0093`, which is a costing input, not an allocation. Before this text is written into
`docs/DECISIONS.md`, run D82-1's success criterion: **execute the tree's prose-binding instruments
against this candidate text first**, not after.

Heading and section shape follow `ADR-0093` (`docs/DECISIONS.md:11156`).

---

## ADR-XXXX — The wave wall clock is scoped to the phase that spent it: `waves` gains one additive nullable `wave_started_phase`, re-stamped on a phase transition and never on a resume, because the clock a TRANSFORM breach spends must not be the clock BUILD and VERIFY are charged

**Status:** ACCEPTED. Landed round H.

**Context.** `WaveScheduler` documents itself *"One instance per (run, phase)"* and carries a `phase`
field it uses on every status read, but its store does not: **none of the 8 `SchedulerStore` methods
takes a phase**, `waves` has no phase column (`src/fleet/state/schema.sql:218`), and the clock is
measured from one shared persisted stamp. `wave_started_at` has **exactly one assignment site in
`src/`** — `SqliteSchedulerStore.begin_wave`'s first-write-wins
`UPDATE waves SET wave_started_at = COALESCE(wave_started_at, ?)` — and nothing ever clears it. So
one exit-4 wall-clock breach in TRANSFORM leaves **BUILD and VERIFY of that wave permanently
un-admittable**, in that run and every later one, and the only remedy is raising a **fleet-wide
scalar** ceiling whose required multiplier grows with real elapsed time rather than with work done.

**This is a design consequence, not a SPEC violation.** Whole-file whitespace-normalised sweep of
`docs/SPEC.md`, predicate `wave_max_wallclock_s|wave_started_at|wall.?clock` (case-insensitive),
offsets mapped back to line numbers: **31 hits, of which 0 scope the wave clock to a phase** — while
its per-*task* sibling `budgets.task_max_wallclock_s` is per-phase **by declaration**
(`scan: 600 / transform: 1800 / build: 1800`). The shape was chosen, not overlooked. That is what
makes this an ADR and not a bug fix.

**Decision.** One additive nullable column, `ALTER TABLE waves ADD COLUMN wave_started_phase INTEGER`
(a `v009` rung, additive-only, no table rebuild, no backfill — the identical shape `v007` used to add
`wave_started_at` to this same table), stamped in the **same statement** as `wave_started_at` so the
two can never disagree:

```sql
UPDATE waves SET wave_started_at    = COALESCE(wave_started_at, ?),
                 wave_started_phase = COALESCE(wave_started_phase, ?)
 WHERE run_id = ? AND wave_index = ?
```

The clock is re-stamped **iff `wave_started_phase != <the driving phase>`**. `begin_wave` gains a
**required** keyword-only `phase`, threaded from `WaveScheduler.open_wave`'s `self.phase`, across
**4 declarations and 4 call sites** in 3 files. Required, not defaulted: no declaration carries
`**kwargs`, so a missed caller is a loud `TypeError` rather than a value the operator never wrote.

**Why not the cheaper form.** A predicate keyed on `phases.started_at` needs no migration and is
**correct in 3 of 4 cases**. Its hole is a crash between `begin_wave` and the first
`acquire_phase_lease`: the clock is running and every `phases.started_at` is still NULL, so a pure
resume at the *same* phase re-stamps and forgives however much wall clock elapsed while nothing ran.
An operator `Ctrl-C` in that window reaches it — accidentally reachable, not adversarial — which
under CLAUDE.md Rule 12's stop rule makes it a **defect, not a stated boundary**. A second,
independent reason: `_ScanWaveStore` is an in-memory `SchedulerStore` with **no `waves` row and no
`phases` table**, so a `phases`-join predicate has nothing to read there and would force an invented
decision at that site; `wave_started_phase` degrades cleanly to an in-memory `self._started_phase`.

**Consequences.**

(i) **The property narrows, it does not disappear.** Within a phase the clock is still cumulative
across resumes: a BUILD re-stamped, run 3 h, crashed and resumed still reads ≈3 h. What changes is
that the property is per-`(run, wave, phase)` rather than per-`(run, wave)`. Three `docs/SPEC.md`
sentences state the old scope (`:1473`, `:1483-1491`, `:4101-4103`) and are narrowed in the same
workstream, along with the source-of-truth comment on the column itself
(`src/fleet/state/schema.sql:222-224`). `docs/SPEC.md:5061` forbids a **resequence** from restarting
the clock — a different subject, untouched.

(ii) **`SCHEMA_VERSION` moves 8 → 9, and that is the real cost, not the column.** Every verb but
`migrate-db` refuses until the operator migrates — loud, and correct. But the bump also **silently
invalidates every stored checkpoint**: `checkpoints.load` rejects on
`stored_version != schema_version`, and `PhaseRunner._load_checkpoint` returns `loaded.payload` while
**discarding the rejection**, so a repo re-does its completed units with no log line and no finding.
No migration rewrites checkpoint envelopes. That silence is fixed in the same wave under CLAUDE.md
Rule 11; this ADR records that the bump is what makes it reachable in the field.

(iii) **`MigrationWave` does NOT gain a field.** The model is the *plan* projection behind
`migration_state.json`; which phase burned a wave's clock is *execution* state, and CLAUDE.md's
state-management boundary keeps execution state in the database. Recorded here so a later reconciler
does not add `wave_started_phase` to the model to make it mirror the row.

(iv) **No existing test can express the defect this closes.** `ast` sweep of `tests/`: **5
`WaveScheduler(` construction sites across 4 files, and 0 files construct more than one distinct
phase.** The covering test is therefore a new module that drives two schedulers at different phases
over one wave and one store — a statement about expressibility, not about case count.

**Rejected.**

* **The `phases.started_at` predicate alone** — correct in 3 of 4 cases, with a reachable crash
  window and an undecided branch at `_ScanWaveStore`. See "Why not the cheaper form".
* **Four `wave_started_at_<phase>` columns, or a new `wave_phase_clock` table with a join** — both
  strictly more expensive than one nullable column and neither records anything the single column
  does not.
* **Clearing `wave_started_at` on resume** — forbidden in three separate `docs/SPEC.md` places and
  the exact defect the `COALESCE` was written to prevent ("two orchestrators racing a resume must not
  each decide the wave started now").
* **Leaving it and telling operators to raise `budgets.wave_max_wallclock_s`** — that remedy is
  normative and stays available, but it is a **fleet-wide scalar** with no run, wave or phase
  dimension, so un-breaching one wave raises the ceiling for every wave, and the multiplier it needs
  grows with idle wall-clock time without bound.
