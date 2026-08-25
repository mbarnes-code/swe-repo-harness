> **Promoted from untracked scratch at round G close.** Lane R2 (research, read-only),
> `.superpowers/sdd/handoff-round-g/lanes/R2/`. Answers the one mechanical question blocking D82
> Disposition B. Body byte-identical apart from this banner. **Now a tracked reference the next round
> acts on, not a working note — re-measure anything you act on.**

# Lane R2 (research) — round G

**Base:** `main` at `8b40498` (re-derived myself). **main advanced to `4cab272` mid-lane**; the
only file in `8b40498..4cab272` is `tests/test_wave_composition_projects_mid_wave.py`, so **none of
the files I cite moved** and every `file:line` below is valid at *both* SHAs. `[probe @ 8b40498]`

**Worktree:** `/tmp/claude-1000/-home-redmage-swe-repo-harness/roundg-R2`, pinned detached at
`8b40498`. **Scratch:** `…/roundg-R2-scratch` (lane-private). No pytest session was run anywhere.
I wrote nothing outside this report file.

---

# THE ANSWER

## **YES — a row-level signal exists.** It is `phases.started_at`, and it needs no schema change.

`src/fleet/state/schema.sql:470` (`started_at TEXT` on `phases`), written by
`acquire_phase_lease` at **`src/fleet/state/repository.py:1293`**:

```
started_at = COALESCE(started_at, ?)
```

— the *same* first-admission-COALESCE semantics `waves.wave_started_at` has, but keyed on
`(run_id, repo_id, phase)` instead of `(run_id, wave_index)`. Joined through `wave_members`, it
answers exactly the question Disposition B needs:

```sql
SELECT COUNT(*) FROM phases p
  JOIN wave_members m ON m.run_id = p.run_id AND m.node_id = p.repo_id AND m.node_kind = 'REPO'
 WHERE p.run_id = ? AND m.wave_index = ? AND p.phase = ? AND p.started_at IS NOT NULL
```

`0` ⇒ no member of this wave has ever *begun* this phase ⇒ phase transition ⇒ re-stamp.
`>0` ⇒ this phase already burned this wave's clock ⇒ resume ⇒ keep. **This is not the forbidden
reset wearing a hat**: on a resume at the phase that burned the clock the predicate returns
`>0` and keeps the stamp. It never restarts the clock for the phase that spent it.

---

# 1. Why this differs from R1's null result — a refinement, not a contradiction

R1 wrote: *"I could not find any signal in `waves` or `phases` that distinguishes them.
`[UNVERIFIED — I did not find one; I am not asserting none exists]`"* (R1 report line 132-133),
and its open question 2 repeats it. **R1's reasoning is correct and I am not overturning it.**
R1's line 130-131 is the load-bearing observation: *"a resumed `fleet build` after a TRANSFORM
breach **is** a phase transition and **is** a resume."*

That is true, and it is why no signal can classify the *event* into two exclusive categories.
**But Disposition B does not need that classification.** It needs one decision — *may this
wave's clock be re-stamped now?* — and the correct predicate for it is not "which of the two
happened" but **"has the driving phase ever begun on this wave?"**. R1 searched for the former
(genuinely absent); the latter is present. The two lanes agree on every measured fact.

---

# 2. Derived two genuinely different ways

## Derivation 1 — dynamic differential, real scheduler, real DB `[probe @ 8b40498]`

`roundg-R2-scratch/d82_probe.py`. Real `initialize_database` (`user_version=8`), real
`StateWriter`, real `SqliteStateRepository`, real `SqliteSchedulerStore`, real `WaveScheduler`,
real `BudgetsSection()` (`wave_max_wallclock_s = 14400`, `settings.py:269`). TRANSFORM (`Phase`
= 2) opens wave 0 at T0 and both members take `acquire_phase_lease`. Clock advanced to
T0+18000s. **The halted DB is then copied byte-for-byte into two branches**, so both see
identical rows:

```
--- A_RESUME (scheduler.phase = TRANSFORM) ---
  CANDIDATE SIGNAL  phases.started_at NOT NULL @ this phase : 2
  admission  breached=True admitted=() withheld=('repo-a','repo-b') elapsed_s=18000.0
  FULL-DB DIFF vs halted state: 0 cell change(s)

--- B_TRANSITION (scheduler.phase = BUILD) ---
  CANDIDATE SIGNAL  phases.started_at NOT NULL @ this phase : 0
  admission  breached=True admitted=() withheld=('repo-a','repo-b') elapsed_s=18000.0
  FULL-DB DIFF vs halted state: 0 cell change(s)

  full-DB diffs identical across the two branches : True
  candidate signal differs across the two branches: True (2 vs 0)
  D82 reproduced (never-ran BUILD is breached)    : True
```

Import pin printed and asserted **before** any result:
`sys.executable = …/.venv/bin/python`, `fleet.__file__ = <WT>/src/fleet/__init__.py`, run under
`env -i … PYTHONPATH=$WT/src`.

**Two facts, not one.** (i) **D82 reproduces**: a BUILD that never ran reads `breached=True`,
`admitted=()`, `elapsed_s=18000` on wave 0. (ii) **`open_wave` writes nothing** on an
already-stamped wave — the full-DB diff is `0` in *both* branches. So there is **no signal in
what `open_wave` does**; the signal is only in what it could *read*, and the candidate reads
`2` vs `0`.

The snapshot enumerates **every row of every table** via `sqlite_master` + `SELECT *`, so it
cannot miss a column I failed to think of. That is its blind spot's opposite: it *can* miss a
column no exercised code path writes — which is what Derivation 2 covers.

## Derivation 2 — static full-column census, independent of any code path `[probe @ 8b40498]`

`roundg-R2-scratch/d82_validate.py`, via `PRAGMA table_info` on a freshly initialised DB (so it
reflects `schema.sql` *as applied*, not as read):

* **22 tables.**
* **6 tables carry a `phase` column** — `attempts`, `checkpoints`, `events`, `phases`,
  `reservations`, `tasks`.
* **2 tables carry `wave_index`** — `waves`, `wave_members`.
* **Tables carrying BOTH `phase` and `wave_index`: NONE.** ← this is D82's root, measured.
* `waves` columns: `run_id, wave_index, computed_at, wave_started_at, synthetic, max_usd`
  — **no phase column, no counter, no monotonic sequence.**
* `runs` columns: `run_id, started_at, finished_at, config_sha256, config_digests,
  monorepo_branch, harness_version` — **no resume counter and no resume timestamp.**

The two derivations have genuinely different blind spots (values-not-written vs
columns-not-exercised) and agree: nothing *direct* exists; the discriminator is necessarily an
**indirect join** from `wave_members` into a phase-keyed table.

## Corroborating sweep — no resume marker anywhere `[probe @ 8b40498]`

* Whole-DDL sweep of `src/fleet/migrations/v002…v008` for `ADD COLUMN`/`CREATE TABLE`: **no
  migration adds a phase column to `waves`, and none adds a resume counter.** `v007_logical_keys.py:59`
  is where `wave_started_at` was introduced — phase-less from birth.
* `grep -oE '\b[A-Za-z]*Resum[A-Za-z]*\b' src/ --include=*.py` → only `ResumeIncompleteError` (7)
  and `Resume` (3). **There is no `RunResumed` event and no `WaveOpened` event**, so the
  `events` table — despite carrying `phase` — records nothing at `open_wave` time to key on.

---

# 3. Instrument validation (all four checks) `[probe @ 8b40498]`

A "0 cell changes" result from a silently-broken differ looks identical to a true one, so the
differ was validated before its clean result was trusted:

| check | what was run | result |
|---|---|---|
| (b) silent on unchanged | snapshot twice, nothing between | `diff = []` ✓ |
| (d) cosmetic control | pure reads `breached()` + `elapsed_s()`, no write | `diff = []` ✓ |
| (c) **synthetic fault into a clean DB** | one `UPDATE waves SET wave_started_at='2099-…'` | `["waves.wave_started_at: '2026-01-01T00:00:00.000000+00:00' -> '2099-01-01T00:00:00+00:00'"]` — **exactly one cell** ✓ |
| (a) fires on known-bad | **same cell as (c) by construction** — the first-ever `open_wave` NULL→stamp write is the identical column. Disclosed rather than claimed as a separate check. | ✓ (qualified) |

Check (c) is the one that matters: the differ demonstrably sees a `waves.wave_started_at` write,
so the `0` it reports for both branches is a real absence, not a dead instrument.

---

# 4. The predicate's ONE measured hole — and it is real

**`[probe @ 8b40498]`** Scenario: the process dies **between `begin_wave` and the first
`acquire_phase_lease`** — `wave_started_at` is stamped and the clock is running, but no member
ever claimed the phase, so every `phases.started_at` is still `NULL`.

```
waves.wave_started_at        = 2026-01-01T00:00:00.000000+00:00  (clock IS running)
CANDIDATE SIGNAL @ TRANSFORM = 0
signal says 'never begun this phase' -> would RE-STAMP on a pure RESUME: True
=> FALSE POSITIVE.
```

A pure resume at the *same* phase would re-stamp, discarding however much real wall clock
elapsed while nothing ran. Ten days idle ⇒ ten days forgiven. **This is the forbidden
reset-on-resume, reachable through a narrow crash window.** It is not adversarial — an operator
`Ctrl-C` in that window reaches it. Under CLAUDE.md Rule 12's stop rule that makes it a
**defect, not a stated boundary**.

Scored over both scenarios (`roundg-R2-scratch/d82_holefree.py`): the `phases.started_at`
predicate is **correct in 3 of 4 cases, wrong in 1**.

---

# 5. The cheapest thing that CREATES a hole-free signal — exercised, not asserted

**One additive nullable column: `ALTER TABLE waves ADD COLUMN wave_started_phase INTEGER`**
(a `v009`, additive-only, no table rebuild — the same shape as every `v007` line), stamped in
the *same* `COALESCE` statement as `wave_started_at` so the two can never disagree:

```sql
UPDATE waves SET wave_started_at    = COALESCE(wave_started_at, ?),
                 wave_started_phase = COALESCE(wave_started_phase, ?)
 WHERE run_id = ? AND wave_index = ?
```

Re-stamp iff `wave_started_phase != <driving phase>`. Exercised against **both** scenarios:

```
### NORMAL: TRANSFORM actually ran (members claimed)
    RESUME  (same phase)     phases.started_at  -> keep      OK
                             wave_started_phase -> keep      OK
    TRANSITION (new phase)   phases.started_at  -> RE-STAMP  OK
                             wave_started_phase -> RE-STAMP  OK

### EDGE: crash between begin_wave and first claim
    RESUME  (same phase)     phases.started_at  -> RE-STAMP  ** WRONG **
                             wave_started_phase -> keep      OK
    TRANSITION (new phase)   phases.started_at  -> RE-STAMP  OK
                             wave_started_phase -> RE-STAMP  OK
```

**4 of 4 correct, including the case the existing signal gets wrong.** It records *which phase
burned the clock*, which is precisely the fact D82 says the schema is missing — so it closes the
defect at its root rather than inferring around it.

This is strictly cheaper than the two options R1 costed at its lines 115-117 (four
`wave_started_at_<phase>` columns, or a new `wave_phase_clock` table + a join): **one nullable
column, no new table, no rebuild, no backfill** (`NULL` = "stamped before v009", which the
`COALESCE` handles by treating the next opener as the owner).

---

# 6. Costs of Disposition B, stated precisely `[probe @ 8b40498]`

**Signal access — already present, no widening needed for the read.**
`WaveScheduler` is documented *"One instance per (run, phase)"* (`scheduler.py:370-372`) and
already holds `self.phase`; `status_of` (`scheduler.py:405-409`) already calls
`self.db.get_phase(self.run_id, repo_id, self.phase)`. The driving phase is in hand at
`open_wave` time. **`self.phase` is in-process state, not a row** — which is exactly why the
`waves` row alone can never answer the question.

**The write side IS a protocol change.** `begin_wave` takes no phase argument. Adding one
touches **4 sites**, all measured:

| site | what it is |
|---|---|
| `src/fleet/orchestrator/scheduler.py:124` | `SchedulerStore` Protocol declaration |
| `src/fleet/orchestrator/scheduler.py:229` | `SqliteSchedulerStore` — the real implementation |
| `src/fleet/cli.py:1420` | an **in-memory SCAN-wave stub** (`self._started`, no `phases` table) |
| `src/fleet/cli.py:3668` | a delegating wrapper (`return await self._inner.begin_wave(...)`) |

⚠️ **`cli.py:1420` is the one to watch**: it is an in-memory store with no `phases` table at all,
so a `phases`-join predicate has nothing to read there. The `wave_started_phase` variant degrades
cleanly (the stub just keeps its `self._started`); the `phases.started_at` variant needs an
explicit decision at that stub.

**What it does to a resume that legitimately follows a phase transition.** Both variants: the
first `open_wave` *at the new phase* re-stamps, and every subsequent resume *at that same phase*
keeps the new stamp (`COALESCE`/equality both hold). So a BUILD that is re-stamped, runs 3h,
crashes, and resumes still reads 3h — cumulative-across-resumes is preserved **within** a phase.
What changes is only that the property becomes per-`(run, wave, phase)` rather than
per-`(run, wave)`.

**What it costs that is NOT a code cost.** `docs/SPEC.md:1473`, `:2880-2884` and `:5054` (R1's
citations, which I did not independently re-sweep — `[ledger]`, R1's measurement) describe the
clock as per-wave and cumulative across resumes. Disposition B **changes a SPEC-stated
property**, so per CLAUDE.md §7 *"'The SPEC says X but the code cannot do X' is two edits, not
one"* — the SPEC sentences must move in the same change, or a later reconciler restores the
defect.

---

# 7. Secondary finding, offered not pressed — the withholding leaves NO trace

The full-DB diff across a breached `admit()` is **0 cell changes in both branches**
`[probe @ 8b40498]`. A wave that withholds every member writes **no event row, no phase row,
nothing**. Combined with §2's sweep finding no `WaveOpened`/`RunResumed` event, an operator
querying the DB after an exit-4 halt cannot tell from `events` that a later phase was ever
withheld. This is adjacent to D83 (`8b40498`, `WaveReport.halt` population) and may already be
in W4's scope; I did not investigate further and make no claim about whether it is intended.

---

# 8. Settled after first draft: `phases.started_at` has TWO writers, and that HELPS `[probe @ 8b40498]`

I listed "is `acquire_phase_lease` the only writer?" as unverified, then checked it, because the
predicate's reliability turns on it. **A second writer exists** — found by a whole-file normalised
sweep (`re.sub(r'\s+',' ')` across every `src/**/*.py` mentioning `phases`, so a line-wrapped
`UPDATE` could not hide, per CLAUDE.md §7). The complete writer set for `phases.started_at`:

| site | scope | semantics |
|---|---|---|
| `src/fleet/state/repository.py:1293` | `acquire_phase_lease`, **any** phase | `started_at = COALESCE(started_at, ?)` |
| `src/fleet/cli.py:3981` | `fleet transform` anchor cut, **hard-scoped to `int(Phase.TRANSFORM)`** | `started_at = COALESCE(started_at, ?)` |

`cli.py:3981`'s `WHERE` clause binds `int(Phase.TRANSFORM)` as a literal parameter
(`cli.py:3976`), so **it can never write any phase but 2.** Both writers use the identical
first-write-wins `COALESCE`, and both mean the same thing — *this repo began this phase*.

**Consequences, and they run in the predicate's favour:**

* For every phase **other than TRANSFORM**, `acquire_phase_lease` is the sole writer and my
  original assumption holds exactly.
* For **TRANSFORM**, the second writer fires at *anchor-cut* time, which is **earlier** than
  lease acquisition. It therefore sets `started_at` sooner, **narrowing** the §4 crash window for
  the one phase most likely to burn a wave's clock in the first place.
* **The §4 hole is not closed by it.** `begin_wave` still stamps `wave_started_at` at
  `open_wave`, strictly before any per-repo transform work reaches `cli.py:3981`. A crash inside
  that window still leaves every `started_at` NULL with the clock running. §4's measurement
  stands and §5's recommendation is unchanged.
* No writer sets `started_at` non-`COALESCE`, and none clears it to NULL — **so the signal is
  monotonic**: once a phase has begun on a repo it stays begun. That is the property the
  predicate needs, and it is now derived rather than assumed.

---

# Claims I could NOT settle — `[UNVERIFIED]`

1. **Whether cross-phase sharing of `wave_started_at` is INTENDED.** My question was the
   mechanical one and I answer only that. R1 swept SPEC normalised and found nothing deciding it;
   I did not re-derive that sweep and I inherit no conclusion from it. **The ruling still needs
   this and nothing in the tree supplies it.** `[ledger — R1's measurement, not re-derived by me]`
2. ~~I did not enumerate every writer of `phases.started_at`.~~ **SETTLED — see §8 below. A
   second writer DOES exist and it strengthens the predicate rather than falsifying it.**
3. **I did not exercise the `cli.py:1420` SCAN-wave stub.** Its behaviour under either predicate
   is reasoned from its source, not run. §6's ⚠️ is a source read, not a probe.
4. **I ran no pytest and no whole-suite run.** Nothing here certifies the tree is green. Every
   number above comes from three standalone pinned drivers, each printing and asserting
   `fleet.__file__` before any result.
5. **Two members, one wave, one run.** I did not vary fleet size, `synthetic` waves, `BLOCKED`
   members, or SCC/`ATOMIC_WAVE` membership. The signal is a `COUNT(*)` over wave members so I
   expect it to scale, but that is reasoning, not measurement.

---

# Agent Recommendation (explicitly NOT a requirement — the ruling is the orchestrator's)

If Disposition B is ruled in, I would key it on **`waves.wave_started_phase`** (§5) rather than on
`phases.started_at` (§THE ANSWER). Both are real signals; the existing one needs no migration but
is **measurably wrong in 1 of 4 cases**, and that case is an ordinary crash window, not an
adversarial one. One additive nullable column buys 4-of-4 and records the missing fact directly.
Disposition B remains blocked on open question 1 above (intent), which is not a measurement.
