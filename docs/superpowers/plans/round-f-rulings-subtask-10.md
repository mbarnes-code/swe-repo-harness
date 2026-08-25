> **Promoted from untracked scratch at round F close.** The orchestrator's rulings for resume step 5
> subtask 10, with each ruling's cost-if-wrong. Two rulings carry in-place dated CORRECTIONS made by the
> lanes that implemented them — read the corrections, not only the rulings.

# Orchestrator rulings — resume step 5, subtask 10 (round F)

Source: lane R1's decision brief, re-derived at `251cd30`. R1 re-measured **16** of R4's load-bearing
claims: **11 reproduce, 2 ROTTED, 1 does not reproduce, 1 right conclusion / wrong mechanism, 1
unverifiable as stated.** Every ruling below cites R1's measurement, not R4's.

**Read this before any subtask-10 brief. Where it contradicts
`docs/superpowers/plans/resume-step5-subtask-10-research.md` (R4), THIS FILE WINS — but check the
primary source yourself and report any further rot.**

---

## Facts that supersede R4

| R4 said | measured at `251cd30` | consequence |
|---|---|---|
| **27** non-dry-run exit-2 `resume` tests | **34.** Also **36** bare-`resume` tests, **38** bare call sites | 10e's scope is 34/36, not 27 |
| "22 of the 27" have unfit fixtures | **no predicate stated — unverifiable by construction** | use **30 of 34**, derived: 4 of the 34 are *about* the refusal (`test_cli.py:1396`, `:2294`, `:2329`, `:2423`); the other 30 are about steps 2/3/4/5, budgets, drift, PR re-poll |
| Row 10 says "the **three** composition roots" | **ROTTED** — neutralised at `f127680` to "the **phase** composition roots" | the sole surviving occurrence is **inside W35's own annotation, quoting the retired title deliberately**. A count-based sweep reads it as a survival; it is a retraction |
| Row 10's criterion mandates the defect ("exits 0") | **ROTTED** — now "It exits 0 only when no delegate halted" | **Seam B needs no ruling. It is already ruled. Do not re-rule it.** |
| Fix title + clause + SPEC §10 row "in ONE change" | **stale — at most TWO sentences, and which two depends on the ruling** | see ruling B |
| S8-1: the reap would delete the attempt-free cross-phase checkout | **conclusion right, mechanism wrong** | `_live_sandbox_names` never yields `checkout_name` (`sandbox/worktree.py:84-104`) for any status; and the worktree half of the reap is a **documented no-op today** (`cli.py:11704-11722`). The live justification is the **container** sweep. **Do not copy R4's mechanism sentence into an ADR.** |
| `stub_reconcile` absent = a ninth absence | **`stubs.reconcile` EXISTS**, `src/fleet/orchestrator/stubs.py:554`, pure, tested, imported by `tests/test_stubs.py:42`, **unwired from `src/`** | branch (b) of ruling A is a **driver**, not a state machine — materially cheaper than R4 implies |

**Verified and unchanged:** `phase_floor({}, {})` → `Phase.SCAN` (exercised, `reentry.py:66`).
`_LIVE_SANDBOX_PREDICATE` spares only `RUNNING` + heart-beating (`cli.py:9995-9997`) — S8-1's
*conclusion* stands and is satisfied by construction at the 10d position.

---

## Ruling A — `stub_reconcile` is a REAL obligation and is NOT in subtask 10

**Decided: §11.5's numbered list is authoritative for subtask 10's scope. `stub_reconcile` is carved
out into its own workstream, not folded in.**

Why: subtask 10 is already the round's largest item (10a–10j) and 10e alone is "the single largest
cost". D80 records that `fleet resume` has **two** absent steps; folding the second into the subtask
that cannot already fit one round guarantees a third round of not-fitting. The carve-out is a scope
decision, **not** a finding that the obligation is unreal — §10's row, §13 row 35 and §3.5.1 all
mandate it, and R1's §E.6 has now made it cheaper than anyone thought.

Cost if wrong: `fleet resume` ships continuing-but-not-reconciling for one more round, and §10's row
stays two-thirds true. Mitigated: D80 stays `OPEN` and gains R1's §E.6 finding, so the next round
starts from "write a driver over a tested pure module" rather than re-deriving that.

## Ruling B — Seam D: **D-2.** A `SCAN` floor is reported and skipped, never served

**Decided: step 8 keeps the three roots. A repo whose floor is `Phase.SCAN` is NOT continued; it is
named in a loud payload key. `docs/SPEC.md:6646` is qualified in the SAME change.**

Why D-2 over D-1: `_scan_impl` (`cli.py:1755`) takes **five** resume-less flags and **no** `wave` —
so serving a `SCAN` floor means step 8 inventing five defaults an operator never wrote. That is
character-for-character the recorded `effort: low` defect (`src/fleet/models/tasks.py:100-102`), the
one CLAUDE.md Guardrail 6 exists to stop. Why D-2 over D-3: halting the whole verb because one repo
never scanned punishes every other repo in the fleet, and the exit code is unchosen.

Why the SPEC edit is part of the ruling, not a follow-up: D-2 makes §10's *"continue from each repo's
re-entry floor"* false for the skipped repo, and "the SPEC says X but the code cannot do X" is **two
edits, not one** — correcting the implementation without the sentence regenerates the defect.
**Under D-2 the edit is `docs/SPEC.md:6646` col 697 ONLY.** The row-10 title and clause need **no**
edit — they were already neutralised at `f127680`, and editing them would be the "editing a correct
sentence because it matched your grep" error.

**Scope of the skip, which is larger than R4 framed it:** `computed[repo_id] = floor` is written
(`cli.py:11185`) *before* `demotable_phases` runs, so `computed_floors` retains repos step 5 did NOT
demote. The `SCAN` population therefore includes **every repo that has never successfully scanned**,
not just R4's exotic reaper case. On a fleet with un-started repos, step 8's input is `SCAN`-heavy —
which is exactly why the payload key must be loud rather than a debug line.

**Explicitly deferred, with its evidence:** R1 established the two `SCAN` routes mean different
things — frontier-`SCAN` = *"never started"*, walk-fell-through = *"durable evidence is gone"*
(`reentry.py:108-131`; the second is pinned by `tests/test_cli.py:4543`). **v1 does not distinguish
them in the payload.** Distinguishing requires changing `phase_floor`'s return or re-deriving frontier
state at the call site, and no consumer needing the distinction has been identified. Recorded as an
open question with R1's measurement attached, per Rule 2 — not built speculatively.

Cost if wrong: an operator runs `fleet resume` on a fleet of un-started repos and nothing migrates.
The loud payload key is what makes that a five-second diagnosis instead of a silent no-op, so the key
is load-bearing and 10b's success criterion must assert on it.

## Ruling C — the mirror mutex: step 8 TAKES it, once, before the first delegate

**Decided: the continuation acquires `_phase_preflight`'s mirror mutex once before the first delegate,
and refuses the continuation if it cannot — the step-5 reconciliation still stands and is still
reported.**

Why: the guard is taken by `transform`/`build`/`verify`/`migrate_repos` (`cli.py:2494`, `:2535`,
`:2574`, `:3166`, `:8778`) — the **commands**, not the `_impl`s — so delegating straight to
`_transform_impl`/`_build_impl`/`_verify_impl` **bypasses it**. A continuation runs the same work on
the same mirrors as the commands that take it; there is no principled reason it is exempt. **Once,
not per delegate**: per-delegate acquire/release opens a window between phases for exactly the racing
run the mutex exists to exclude, and matches no other guard's granularity. This is the same shape as
ruling 10c already carries for `_require_disk_headroom` (`cli.py:12281`; called at `:1011`, `:2498`,
`:2539`; **never reachable from `resume`**).

Cost if wrong: a `fleet resume` refuses where it could have proceeded, when another phase run holds
the mirror. That is the safe direction, and the reconciliation is preserved either way.

> **CORRECTION to ruling C (round F, lane W10, `[probe @ b5f7760]`) — the conclusion stands, my
> rationale was wrong, and the ADR must not repeat it.** `_refuse_concurrent_mirror_run`
> (`cli.py:750`) takes `flock(LOCK_EX|LOCK_NB)` and then `LOCK_UN` + `close()` **inside the same
> call**. It is a **non-blocking probe, not an acquisition**. So "acquire once before the first
> delegate" is implementable only as "**check** once" — which is exactly what the phase commands
> already do via `_phase_preflight` — and my stated reason ("per-delegate acquire/release opens a
> window between phases for the racing run the mutex excludes") **does not follow**: this guard
> closes no window at any granularity, in `fleet build` either.
>
> The ruling's **conclusion is unaffected** and rests on the real ground: the `_impl`s never take
> the refusal, so delegating straight to them bypasses it. Once-before-first-delegate is what
> landed at `6a8fafd`. **Whoever writes ADR-0080 must say "takes the same §10 refusal the phase
> commands take", NOT "acquires the mirror mutex".** W10 checked the primary source, found it
> contradicted the routed ruling, implemented what the source supports, and reported the
> correction — which is the behaviour the directives ask for, and the third orchestrator error a
> lane has caught this round.

## Ruling D — S8-8 (wall clock): NOT RULED. Blocked on a measurement, and routed

**Not decided.** The question — a wave past `wave_max_wallclock_s` admits nothing, and `fleet resume`
has no wall-clock raise, so does step 8 exit 0 on a fleet that cannot move? — rests on
`WaveScheduler.breached`, which **neither R4 nor R1 read**. R1 tagged it `[UNVERIFIED]`.

Ruling on it now would be inventing semantics from an unread symbol, which is the one thing R1 and W35
both refused to do. Routed to the next research lane as a measurement task. **10a/10b are not blocked
on it**; only step 8's exit-code contract is.

Cost of the delay: 10b may need a follow-up commit for the exit-code path. Cost of ruling blind:
a fabricated contract in an ADR, which is far worse and much harder to retract.

---

## Consequent dispatch order

1. **10a + 10b** (pure plan + delegation) — unblocked by rulings B and C. **Blocked on nothing else.**
2. **10c** — unblocked by ruling C.
3. **10e** — the 34/36-test migration. Needs an ADR: use **ADR-0080**, which
   `docs/DECISIONS.md:7809` has held RESERVED for exactly this work since it was allocated
   ("Allocated to §11.5 step-5 subtask 10, 'Step 8 — continue'"). **Do NOT take a fresh number.**
   ADR-0092 went to W2's PRAGMA classification; the next free number after that is ADR-0093.

   **Ruling: subtask 10's ADR is ADR-0080, not a new number.** — The number was reserved for this
   exact subtask and reserving it was the point; taking a fresh one while 0080 sits RESERVED
   recreates the hole the placeholder convention exists to prevent. — Cost if wrong: none; the
   placeholder is replaced by the record it was reserved for.

   **Note for whoever writes it:** ADR-0080's placeholder body is now stale in two clauses — it says
   subtask 10 "depends on subtask 9, not landed" (subtask 9 is SETTLED by ADR-0079) and it describes
   step 8 as delegating to "the three composition roots" (neutralised at `f127680`, and ruling B keeps
   three roots but skips `SCAN` rather than serving it). Per Guardrail 7 that body is a record of what
   was true at its own commit: **annotate with a dated marker, do not rewrite it**, and let the ADR
   proper carry the current semantics.
4. **10d, 10f–10j** — after the above.
5. `stub_reconcile` — separate workstream, not this subtask (ruling A).
