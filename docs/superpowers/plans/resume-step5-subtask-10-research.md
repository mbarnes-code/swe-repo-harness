> **Promoted from `.superpowers/sdd/round-e/lanes/R4/report.md` (round E, lane R4) at round-E close.**
> Body byte-identical to the scratch original below this banner; subtask 10 is unimplemented, so this is a live brief.

# R4 (round E, RESEARCH, read-only) — costing subtask 10 (§11.5 step 8) so it is dispatchable

**Anchor.** `main` at **`f36c9ad`**. My dispatch named `b286dc4`; HEAD had moved two commits by the
time I read it (`a9e9640`… → `47df73d` → `b286dc4` → `f36c9ad`, the last being *"docs: record D79
(§11.6's LLM cache is inert) and promote round E's two live research briefs"*). **Every number
below is measured at `f36c9ad`**, and `git status --short` showed **only** `?? .superpowers/` —
no tracked file was dirty, so the working tree and `HEAD` agree for every `src/`, `docs/` and
`tests/` claim here.

**Interpreter.** Every runtime number is from **`.venv/bin/python`** with
`PYTHONPATH=/home/redmage/swe repo harness/src`, and every probe asserted
`fleet.__file__ == /home/redmage/swe repo harness/src/fleet/__init__.py` **before** reading a
result (the editable-install hazard the dispatch names). Probes that only parse text used `ast` on
the file bytes and needed no import.

**tests NOT run — suite lock.** No `pytest` session. Two test *callables* were driven directly
(`tests.test_floor_rule_statements._stop_condition_sentences`), which reads files and imports no
fixture.

---

## 1. What "step 8 — continue" actually requires

### 1.1 The primary source, verbatim

`docs/SPEC.md`, heading **"11.5 Single writer, ordering, and the resume contract"**, the closing
paragraph of the numbered list:

> …(7) regenerate `migration_state.json` from
> SQLite; (8) continue. Steps 1–7 make no network call and invoke no model, so a resume is free
> and can be run as a dry-run health check (`fleet resume --dry-run`).

**That is the whole of it. §11.5 spends one word on step 8.** The second sentence is the only other
thing §11.5 says about it, and it is a *negative* specification of enormous consequence: by
excluding step 8 from "no network call and invokes no model", §11.5 makes step 8 **the entire
non-free, non-idempotent, model-invoking half of the verb**, and it is the reason
`fleet resume --dry-run` can be promised as free.

The **positive** content of step 8 is not in §11.5 at all. It is in `docs/SPEC.md` **§10**'s command
table, row `fleet resume` (heading *"10. CLI Surface"*), which describes the verb as a single
sentence of activities:

> Reconcile after a crash: verify config/version, reap stale worktrees and containers, reset stale
> `RUNNING` rows, **ask Git whether each ambiguous task's commit landed and correct the row to
> match** (Git wins; discard the worktree when it did not — §11.5 step 4), demote each repo to its
> re-entry floor on durable evidence (§11.5 step 5), never a `preconditions_hold` re-check,
> recompute `blocked_by`, **re-run `stub_reconcile`** (§3.5.1: …), regenerate
> `migration_state.json`, **continue from each repo's re-entry floor (§11.5 step 5), never the
> earliest incomplete phase.**

So the requirement, stated once across two sections: **drive each repo forward starting at the
floor step 5 computed for it, never at the earliest incomplete phase, and stop at the first halt.**

### 1.2 What the shipped code does instead — one sentence

`cli._resume_impl` runs steps 1–7 and the step-2 reap and returns a payload; `cli.resume` emits that
payload and then **unconditionally raises `ResumeIncompleteError` (exit 2)** naming step 8 as
absent — so the verb reconciles the ledger completely and continues nothing.

### 1.3 ⚠ **§10's row names a NINTH absence that "step 8 is the sole remaining absence" does not cover**

**This is a contradiction between two primary sources and I am reporting it rather than resolving
it.** ADR-0079's title, ADR-0079 §6, design row 9 and design row 10 all rest on the premise *"with
step 8 the sole remaining absence…"*. That premise is true **of §11.5's numbered list** and false
**of §10's row** and **of §13 row 35**, both of which put `stub_reconcile` inside `fleet resume`:

- §10's row: *"regenerate `migration_state.json`"* is preceded by *"**re-run `stub_reconcile`**"*.
- §13 row 35 (*"A stub is never resolved and the fleet ships it anyway"*): *"`stub_reconcile` runs
  before the final checkpoint **and again in `fleet resume`**"*.
- §3.5.1's paragraph: *"when the run writes its final checkpoint it executes a `stub_reconcile`
  step (also part of `fleet resume`'s …)"*.
- `cli._resume_impl` carries a 15-line committed comment saying `stub_reconcile` **"BELONGS HERE —
  immediately below the re-poll and above the step-3 sweep — and nowhere earlier"**, and ends *"It
  does not exist on `main` yet; when it lands, it goes on the next line."*

**Measured at `f36c9ad`** (AST over all **115** `.py` blobs under `src/`, `.venv/bin/python`): calls
named `reconcile` or `stub_reconcile` = **0**; imports of any module whose name contains `stubs` =
**0**. This independently reproduces ADR-0079 §7a's identical measurement at `f4eade0` — **two refs,
same method, same answer**, so the absence is stable across the round, not a snapshot.

**Why this matters to subtask 10 and not merely to bookkeeping.** ADR-0079 §5's *reason* for keeping
`--revalidation` / `--raise-revalidation-rounds` refused is precisely that `stub_reconcile` is
unwired. So the moment subtask 10 lands, `fleet resume` will still be missing a step its own §10 row
names, and **the argument for deleting `ResumeIncompleteError` — "everything the verb owes now
runs" — will not be true**, on §10's reading. Either:

- (a) §11.5's numbered list is authoritative for "what the verb owes" and §10's row is prose — in
  which case say so in the ADR, because three sources currently read the other way; or
- (b) `stub_reconcile` is a ninth obligation, and `ResumeIncompleteError` survives subtask 10 with
  its message rewritten a second time to name `stub_reconcile` instead of step 8.

**I do not decide this. It is the single highest-leverage ruling the orchestrator can make before
dispatch**, because (a) and (b) produce *different subtask-10 file sets and different tests*, and
because ADR-0076's standing bullet ("if it is still here after step 5 exists, that is a defect") is
adjudicated differently under each.

---

## 2. `ResumeIncompleteError`'s two obligations, re-measured

### 2.1 Is the class still defined, still raised, and on which paths?

**Measured by `ast` over all 115 `src/` blobs at `f36c9ad`** (predicate: `ast.ClassDef` named
`ResumeIncompleteError`; `ast.Raise` whose `exc` is a `Call` on that `ast.Name`; every `ast.Name`
load of it anywhere):

| quantity | count | where |
|---|---|---|
| class definitions | **1** | `src/fleet/cli.py`, `class ResumeIncompleteError(FleetCliError)` |
| `ast.Raise` sites | **1** | `src/fleet/cli.py`, inside `resume` |
| all `ast.Name` occurrences in `src/` | **1** | the same raise |

**Path, exercised in structure rather than read:** the raise sits in `resume` **after** `_emit`,
**after** the `if dry_run: return`, and **after** the `PrEmissionError` branch. So it fires on
**every** non-`--dry-run` `fleet resume` that gets past `_refuse_unbuilt_resume_flags`, the config
drift gate, the wave-ceiling gate and the schema check, and does **not** fire under `--dry-run`.
There is no conditional on it at all — it is the function's last statement.

**Verdict: deletion is mechanically clean in `src/`.** One class, one raise, no other reference,
nothing subclasses it, nothing catches it by name. Nothing has grown a *code* dependency on it.

### 2.2 …but the deletion is **not** clean in `tests/`, and this is the cost nobody has costed

`ResumeIncompleteError` appears by name only **once** in `tests/test_cli.py` and **once** in
`tests/test_floor_rule_statements.py` (both in prose). **The dependency is on its exit code, not on
its name**, and that dependency is large.

**Class result, measured by `ast` over `tests/*.py` at `f36c9ad`.** Predicate: a `test_*` function
whose source segment contains the token `"resume"` (or `'resume'`) **and** matches
`exit_code\s*==\s*2|ExitCode\.USAGE`. Normaliser: none needed — the predicate is over
`ast.get_source_segment`, so a wrapped assertion is inside one segment and cannot be split by a line
boundary (this is why I did not use `grep`).

| class | count |
|---|---|
| `test_*` functions invoking `resume` **at all** | **55**, across **3** files (`test_cli.py` 53, `test_resume_unblocking.py` 1, `test_state_models.py` 1) |
| …of those, asserting exit 2 / `ExitCode.USAGE` | **39**, all in `tests/test_cli.py` |
| …of those 39, whose body contains `--dry-run` | **12** |
| …of those 39, whose body does **not** contain `--dry-run` | **27** |

**Those 27 are the blast radius, and the danger is not the exit code — it is that they will start
executing a continuation.** Today each of them drives a real `fleet resume` against a seeded
fixture and asserts exit 2. The moment step 8 exists and the refusal is deleted, every one of those
27 invocations **runs the phase drivers** against its fixture: `PhaseRunner` assembly, worktree
cuts, container starts, `bazel`, and — for any repo whose floor is ≤ TRANSFORM — **model calls**.
Twenty-two of the 27 are tests of steps 2/3/4/5 that have nothing to do with continuation and whose
fixtures were never written to survive one.

This is the single largest cost in subtask 10 and **row 10's criterion does not mention it**. Its
"Files touched" names `src/fleet/cli.py`, `tests/test_cli.py`, `docs/DECISIONS.md`; that is
accurate as a *file* list and severely understates the *work* inside `tests/test_cli.py`.

**Agent Recommendation (not a directive):** subtask 10 ships a way for a test — and an operator — to
get the reconciliation without the continuation. The cheapest shape that does not invent a
mechanism: **`--dry-run` already is that flag** for previews, but it also suppresses the *writes*,
which those 27 tests need. So either (i) a new `--no-continue` flag on `fleet resume` (exit 0,
reconciles and stops — which is *precisely today's behaviour minus the exit-2 refusal*, so it is a
rename of an existing path, not new machinery), applied to the 22 tests that are not about step 8;
or (ii) accept that 27 fixtures each grow a continuation-suppressing seam. **(i) is much cheaper and
is also an operator feature** — "reconcile my crashed run, do not spend money yet" is the exact
thing an operator wants after a crash, and it is what every one of those 27 tests is implicitly
asserting today. **(i) needs an ADR and a §10 row edit**, so the orchestrator must allocate.

### 2.3 Is ADR-0076's bullet still annotated as recorded? **Yes — verified, not inherited**

`docs/DECISIONS.md`, ADR-0076's final bullet, is present **unrewritten**:

> - **This ADR is temporary by construction.** When §11.5 step 5 lands, `ResumeIncompleteError`
>   should be deleted, not repurposed. If it is still here after step 5 exists, that is a defect.

and immediately beneath it, in the file's block-quote editorial register, the annotation:

> **AMENDED 2026-08-21 (round D, lane W7) — deletion is DEFERRED to subtask 10, and the bullet
> above is left standing rather than rewritten.** … **The obligation survives.** Design
> `docs/superpowers/plans/design-resume-step5.md` §5 row 10 carries the deletion. If
> `ResumeIncompleteError` is still raised once steps 6 and 8 both exist, the bullet above applies
> unamended and that IS the defect it names.

ADR-0089 §6's first bullet concurs (*"It does not delete `ResumeIncompleteError`. That is subtask
10's, and ADR-0076's bullet stands."*). **Three sources agree and none dissents.** The obligation is
live, correctly recorded, and correctly routed to subtask 10.

**One thing the annotation's closing condition does not survive contact with §1.3.** It says the
bullet applies *"once steps 6 and 8 both exist"*. Step 6 exists. If §10's `stub_reconcile` is a real
obligation, then "the verb can now continue" is still false after step 8, and ADR-0076's bullet
would be discharged by a *second* rewrite rather than a deletion. That is the (b) branch of §1.3.

---

## 3. The un-refusal set, exactly

**What I inherited, attributed.** My dispatch says an earlier orchestrator brief called the
un-refusal set *"five"* and that measurement corrected it to **two**. Design row 10's own
`*(Extended 2026-08-22, W27)*` marker attributes the same correction to itself: it records the
`unbuilt` dict as having **five** keys, of which row 10 removes **two**, measured at `bac5069` by
parsing the dict out of a `git show` blob with `ast` — and states plainly that *"The dispatch
routing this fix said the row must own removing all five; **ADR-0079 §5 and §8 falsify that**"*.

**What I measured, independently and by a different method.** I did not parse the dict; I
**exercised the function** at `f36c9ad` under `.venv/bin/python`, calling
`cli._refuse_unbuilt_resume_flags` once per parameter with that parameter set and the other four at
their default:

| flag | probe value | outcome |
|---|---|---|
| `--from-phase` | `2` | **refused**, `UsageError`, `exit_code == 2` |
| `--repo` | `"r"` | **refused**, `UsageError`, `exit_code == 2` |
| `--reset-attempts` | `True` | **refused**, `UsageError`, `exit_code == 2` |
| `--revalidation` | first `Revalidation` member | **refused**, `UsageError`, `exit_code == 2` |
| `--raise-revalidation-rounds` | `3` | **refused**, `UsageError`, `exit_code == 2` |
| *(none set)* | — | **accepted**, returns `None` |

**Refusal set size, exercised: 5.** `inspect.signature` reports exactly those five keyword-only
parameters and no others.

**Against ADR-0079 as it now stands:**

- **§2** rules `--from-phase` = option C(i), a repo filter. **Removed by row 10.**
- **§4** rules `--repo` a consistency ruling, scoping §11.5 steps 5, 6 and (later) 8, never the
  run-wide 2, 3, 4, 7. **Removed by row 10.**
- **§5** is a *recital*: `--revalidation` and `--raise-revalidation-rounds` **stay refused**, on
  three concurring primary sources (§10's `fleet stubs resolve` also takes `--revalidation`; §13
  row 34 *"Revalidation storm"* contains no re-entry floor; design row 9). **Not removed.**
- **§8** expressly does **not** rule `--reset-attempts`' semantics, and notes the audited-flag
  precedent implies a `findings.kind` that does not exist. **Not removed** — and row 10's criterion
  says so in its own words: *"no row owns removing it until an ADR does."*

**Against design row 10 as it now stands:** its criterion names `--from-phase` and `--repo` and
says *"The other three entries do not move with them"*. **Confirmed.**

### **Row 10 removes exactly TWO of five. The "five" figure is correct as the size of the dict and wrong as the size of the removal; both numbers are live and they are not the same number.**

**One residual I am reporting rather than editing** (CLAUDE.md's mirror-image rule): ADR-0079 §7a's
class result — *"a surface of `fleet resume` states a reason for refusing `--revalidation` /
`--raise-revalidation-rounds` that is false for them"*, **2 sites, both inside
`_refuse_unbuilt_resume_flags`** — is **still live at `f36c9ad`**. I re-read both sites. The
docstring now correctly says step 5 and step 6 are built and step 8 is absent, and the `UsageError`
says the same; **neither mentions `stub`, `revalidation`, `3.5.1`, `storm` or `row 34`.** So an
operator typing only `--revalidation batched` is still told the flag scopes a Phases 1–4
continuation. ADR-0079 §7a assigns this to *"subtask 9d"*; **design row 9 as rewritten no longer
contains a 9d and no row owns it.** Subtask 10 is the natural home because it edits that exact
function anyway — but it needs assigning, not assuming.

---

## 4. What step 8 must not break — the ordering facts, each with its probe

This is the section the dispatch asked to be strongest. Round E's step-6 work turned on one ordering
fact (step 6 between step 5 and `project_once`). Step 8 has **six**, and they do not all point the
same way.

### The current order in `_resume_impl`, measured

Read off `src/fleet/cli.py::_resume_impl` at `f36c9ad`, by symbol, in execution order:

`_resolve_run` → `_earliest_open_wave` → `_refuse_exhausted_wave` → `_refuse_bad_raise_budget` →
`_record_drift_findings` → `_raise_wave_ceiling` → `_raise_run_ceiling` → **`_pr_sync_impl`**
(`--repoll-prs` only) → *(the marked `stub_reconcile` insertion point)* → **step 3**
`_reset_stale_running` → **step 4** `_reconcile_tasks_with_git` → **step 5** `_demote_to_floors` →
**step 6** `_unblock_dependents` → **step 7** `project_once` → **step 2** `_reap_orphan_worktrees` /
`_reap_orphan_containers` → `return payload`.

Then in `cli.resume`: `_emit(payload)` → `if dry_run: return` → `PrEmissionError` if the re-poll
failed → **`raise ResumeIncompleteError`**.

---

**S8-1 — Step 8 must run AFTER the step-2 reap, and this is the direct analogue of step 6's fact.**

*Why.* `_LIVE_SANDBOX_PREDICATE` is, verbatim, `" AND status = 'RUNNING' AND NOT (1" +
_STALE_HEARTBEAT_PREDICATE + ")"`. A sandbox is spared **only** if its `phases` row is `RUNNING`
and heart-beating. A continuation that has completed a phase leaves that repo `SUCCEEDED`, not
`RUNNING` — so a reap ordered *after* step 8 would delete the cross-phase repo checkout
`fleet-<run_id>-<repo>` (ADR-0085's attempt-free form) that the *next* phase of the same
continuation reads, because Phase 2 reads the tree Phase 1 cloned.

*Probe.* Read `_LIVE_SANDBOX_PREDICATE` and `_reap_orphan_worktrees`' `live_names` argument; then
seed a fixture with a `SUCCEEDED` Phase-2 row and its `fleet-<run_id>-<repo>` worktree present, run
`_live_sandbox_names`, and assert the worktree's name is **absent** from the returned set.

*Status.* **Satisfied by construction if step 8 is appended after `_resume_impl` returns** — the
reap is already the last thing in the function. It is broken by any implementation that inserts
step 8 at §11.5's numbering position (between 7 and the reap). **The tempting "match the SPEC's
numbering" move is the wrong one, exactly as ADR-0081 already found for step 2.**

---

**S8-2 — Step 8 must run AFTER `_emit`, or a crashing continuation destroys the reconciliation
report.**

*Why.* The reconciliation is durable but the *report* is not: it exists only on stdout. Today
`_emit` runs before both the `PrEmissionError` and the `ResumeIncompleteError` raises, and
`tests/test_cli.py::test_the_reconciliation_payload_is_emitted_before_the_step_5_refusal` pins
that — ADR-0076 records it as *"deliberately the only `--json` resume test that does NOT pass
`--dry-run`"*. If step 8 goes **inside** `_resume_impl`, the payload is emitted only after the
continuation, and a continuation that exits 1 emits nothing at all: the operator loses the
step-4 arbitration verdicts and the step-5 demotion report for a crash that happened afterwards.

*Probe.* Monkeypatch the step-8 entry point to raise, run `fleet resume --json`, assert stdout
carries a parseable payload containing `git_arbitration` and `reentry_floors`.

*Consequence for placement.* **S8-1 and S8-2 together fix the position: step 8 goes in `cli.resume`,
at the statement that today reads `raise ResumeIncompleteError(...)`.** That is after the reap
(S8-1), after `_emit` (S8-2), after the `if dry_run: return` (S8-4). It is a one-statement
substitution, which is the strongest single argument that the *wiring* half of subtask 10 is small.

---

**S8-3 — The projection ordering inverts relative to step 6's, and the inversion is safe only
because the delegates re-project.**

*Why.* Step 6's fact was "above step 7, or the projection is stale". Step 8's is the mirror: step 8
runs **below** step 7 by §11.5's own numbering, so `migration_state.json` as step 7 writes it is
**pre-continuation by design**. That is only tolerable because each delegate republishes.

*Measured.* AST over `src/fleet/cli.py` at `f36c9ad`: `project_once` has **7** call sites, whose
enclosing functions are `_scan_impl`, `_transform_impl`, `_build_impl`, `_verify_impl`,
`_quarantine_impl`, `_abort_impl`, `_resume_impl`. **All four composition roots re-project.**

*The residue, and it is real.* If the continuation drives **zero** repos, or halts before its first
delegate returns, the published `migration_state.json` is step 7's — correct. If it halts *between*
delegates, the published file is the last delegate's — also correct. **There is no window in which a
stale projection is published**, and that is a property of the delegates, not of step 8. A step-8
implementation that bypassed the `_*_impl` roots and drove `PhaseRunner` directly would lose it.

*Probe.* Assert the mtime/content of `migration_state.json` changes across a continuation that
drives ≥1 repo, and does not change across one that drives 0.

---

**S8-4 — `--dry-run` must not reach step 8, and today's structure gives that for free.**

*Why.* §11.5's own sentence — *"Steps 1–7 make no network call and invoke no model, so a resume is
free and can be run as a dry-run health check"* — is a promise about **steps 1–7 only**. Step 8
spends money.

*Measured asymmetry worth knowing:* `_transform_impl` takes a `dry_run` parameter; **`_build_impl`
and `_verify_impl` do not** (signatures read by AST: `_build_impl(opts, settings, path, *, run_id,
wave, only, timeout_s, sandboxed)`, `_verify_impl(…, run_id, wave, only, rdeps_limit,
rdeps_sample_n, affected_only)`). So step 8 **cannot** be previewed even in principle for Phases 3
and 4. The `if dry_run: return` in `resume` before the raise is therefore load-bearing and must
stay above step 8.

*Probe.* `fleet resume --dry-run` with a step-8 entry point monkeypatched to raise: assert exit 0
and that the patch never fired.

---

**S8-5 — Step 8 makes `fleet resume` a `blocked_by` WRITER for the first time, after step 6 has just
recomputed it.**

*Why.* `orchestrator/scheduler.py::SchedulerStore.append_blocked_by` executes
`UPDATE phases SET blocked_by = ?, status = 'BLOCKED', updated_at = ?`, and it is reached from
`orchestrator/runner.py`'s containment path (the `blocked_by_propagated` event site, `§3.5: an
abandoned repo marks exactly its transitive dependents blocked_by`). Step 8 instantiates
`PhaseRunner`s. So a repo that reaches `REQUIRES_HUMAN_INTERVENTION` **during step 8** will have
`blocked_by` written **after** step 6 emptied it — which is correct behaviour, but it means the
step-6 report already emitted (S8-2) describes a `blocked_by` state the same command then changed.

*Probe.* Drive a continuation in which one repo fails to RHI; assert (i) its dependents carry
`blocked_by` afterwards, and (ii) the `unblocked_dependents` block in the emitted payload does
**not** claim them as freed.

*Recommendation (Agent Recommendation).* The payload's `unblocked_dependents` key needs a sentence
in `_resume_lines` saying it is a **step-6-time** fact, not an end-of-command one. One line of
prose; no mechanism.

---

**S8-6 — Delegating at the `_*_impl` level silently drops three guards the phase verbs run.**

*Measured by AST at `f36c9ad`* — for each Typer command and for `_resume_impl`, the set of guard
calls reachable in its own body:

| verb | guards it calls |
|---|---|
| `scan` | `_load_settings`, `_require_db`, `_check_schema_version`, `_refuse_concurrent_mirror_run`, **`_require_disk_headroom`** |
| `transform` | **`_phase_preflight`**, `_validate_transform_flags`, **`_check_wave_budget`** |
| `build` | **`_phase_preflight`**, `_validate_build_flags`, **`_check_wave_budget`**, **`_require_disk_headroom`**, **`_raise_for_phase`** |
| `verify` | **`_phase_preflight`**, `_validate_verify_flags`, **`_check_wave_budget`**, **`_require_disk_headroom`**, **`_raise_for_phase`** |
| `migrate_repos` | `_phase_preflight`, `_check_wave_budget` |
| `resume` | `_load_settings`, `_require_db`, `_check_schema_version` |
| `_resume_impl` | `_refuse_exhausted_wave` |

Three gaps, each with its own answer:

1. **`_require_disk_headroom` — a real gap.** §11.3/§12.22's ENOSPC refusal (exit 9). `resume` never
   calls it; `build` and `verify` do, with the committed reason *"A phase that consumes gigabytes
   does not get to discover ENOSPC inside a write transaction."* **Step 8 must call it**, once,
   before the first delegate. *Probe:* set `preflight.min_free_bytes` above free space, run a
   continuation, assert exit 9 and that no delegate ran.
2. **`_phase_preflight`'s mirror mutex — a real gap.** `_phase_preflight` supplies §10's mirror
   mutex, which `resume` does not take. A continuation is phase work and should hold it.
   *Probe:* hold the mutex, run `fleet resume`, assert the continuation refuses while steps 1–7
   still complete. **Needs a ruling**: refusing the whole verb would subtract steps a plain resume
   does today, which the code's own `PrEmissionError` comment forbids in terms
   (*"a flag added to do MORE must never subtract the steps a plain `fleet resume` would have
   done"*). My reading is the continuation refuses and the reconciliation stands.
3. **`_check_wave_budget` — NOT a gap.** `_check_wave_budget` is a two-line synchronous wrapper
   around `_refuse_exhausted_wave`, which `_resume_impl` already calls on the earliest open wave.
   Later waves' ceilings are frozen in `waves.max_usd` and enforced inside the runner. *I checked
   this expecting a gap and did not find one; recording the negative result so the next lane does
   not re-check it.*

---

**S8-7 — Two facts I expected to be hazards and measured as safe. Recorded so nobody re-litigates
them.**

- **The wave re-opening (ADR-0089 §4) does NOT cross phases.** I expected step 5's demotion, which
  re-opens a closed wave, to make step 8's *earlier-phase* drivers re-admit a repo whose floor is
  higher. It cannot: `WaveScheduler.status_of` reads `self.db.get_phase(run_id, repo_id,
  **self.phase**)` — the scheduler is constructed **per driving phase**, so `wave_state` is
  per-phase and a Phase-3 demotion leaves the Phase-2 scheduler's view `SUCCEEDED` → `settled` →
  not admitted (`WaveScheduler.admit`). **The re-opening is scoped exactly to the phase the floor
  names, which is what step 8 wants.** *Probe:* seed a repo `SUCCEEDED` at Phase 2 and demoted at
  Phase 3; assert `wave_state` is `CLOSED` for a Phase-2 scheduler and `OPEN` for a Phase-3 one.
- **The single-writer slot does not collide.** `state/db.py` holds a process-global
  `_write_slot_owner` and `_acquire_write_slot` raises `SingleWriterViolationError` on a second
  writable connection. Every step-5/6/3 helper opens its `StateWriter` in an `async with` and
  closes it; `_resume_impl` holds no writer when it returns. Step 8 at the S8-2 position therefore
  opens the slot cleanly. *Probe:* assert `state.db.write_slot_owner() is None` immediately after
  `_resume_impl` returns.

---

**S8-8 — The wall clock is cumulative across resumes, and step 8 can therefore succeed at doing
nothing, forever.**

`docs/SPEC.md` §3.4's budget table: *"Per-wave wall clock | `budgets.wave_max_wallclock_s` (default
14 400), measured from the persisted `waves.wave_started_at` and **cumulative across resumes** — a
resume continues the wave's clock, it never restarts it, or a crash-loop buys unbounded time."*
`WaveScheduler.breached` reads that persisted start; `wave_state` returns `PARTIAL` on a breach;
`open_wave` refuses any later wave while an earlier one is not `CLOSED`.

So a resume of a run whose wave breached its wall clock will drive step 8, admit nothing, and — on
row 10's criterion — **exit 0**. `fleet resume` has `--raise-budget` and `--raise-wave-budget` (both
cost) and **no wall-clock raise**. *Probe:* seed `waves.wave_started_at` older than
`wave_max_wallclock_s`, run the continuation, assert what it exits with. **This needs a ruling**:
either step 8 reports the breach as a halt with a non-zero code, or `fleet resume` grows a
wall-clock raise, or the "exits 0" in row 10 is qualified. **Doing nothing here ships a green exit
on a fleet that cannot move.**

---

## 5. The seams — where step 8 regenerates a D74 shape, and how to close each by construction

D74's shape is *two independent computations of one value with nothing enforcing agreement*. Subtask
8 closed one by construction — `_unblock_dependents`'s docstring records it: *"`floors` is passed in
and never re-derived"*, and `Unblocking` returns `removed` **and** `remaining` *"so that neither
route re-derives 'did this list empty?'"*. Step 8 can regenerate the class in three places.

### Seam A — "which phase does repo *r* re-enter at?" ⚠ **highest risk**

Step 5 computes it (`_demote_to_floors` returns `tuple[dict[str, object], dict[str, Phase]]`; the
second element is `computed_floors`). Step 6 already **takes it in** (`_unblock_dependents(...,
floors: Mapping[str, Phase], ...)`). If step 8 instead re-reads `phases` and re-derives the floor —
the obvious implementation, since the rows have moved twice since — that is D74 verbatim, and it is
*worse* than D74 because the two derivations would disagree in a way no test seeds.

**Close by construction:** step 8 takes `computed_floors` as a parameter, exactly as step 6 does.
`_resume_impl` already has it in scope at the point step 8 is wired.

**Is `computed_floors` still valid after step 6 has written? Measured — yes.** Step 6's only status
write is `BLOCKED → PENDING`. `phase_floor`'s frontier is the lowest phase whose status is **not**
in `_SETTLED_FOR_DEMOTION` = `{SUCCEEDED, SKIPPED, DEGRADED}`. `BLOCKED` and `PENDING` are both
outside that set, so the frontier — and therefore the floor — is **invariant** under step 6's write.
And `_unblock_dependents`' own docstring states the complement: *"Step 6 REPORTS the floor and does
not apply it."* *Probe:* call `phase_floor` twice on identical `rows` differing only in
`BLOCKED` vs `PENDING` at the frontier phase and assert the returned `Phase` is equal.

### Seam B — "what exit code does a phase result imply?"

**Measured class result, AST at `f36c9ad`.** Predicate: a function containing an `ast.Raise` of
`HumanInterventionError` paired with an `ast.Raise` of `FleetCliError(exit_code=…)`. **3 sites** in
`src/fleet/cli.py`: `scan`, `transform`, and `_raise_for_phase` (which `build` and `verify` call).
`transform` and `scan` each carry an **inline copy** of `_raise_for_phase`'s body — the class is
already at 3 and `_raise_for_phase`'s docstring calls itself *"One exit-code mapping for both phase
verbs"*, which is true of the two that call it and false of the tree.

**Step 8 must not become site 4.** Close by construction: **step 8 calls `_raise_for_phase` on each
delegate's payload.** Writing its own mapping — which row 10's *"stops at the first halt, and exits
0"* invites — is the defect.

⚠ **And row 10's criterion, read literally, mandates the defect.** `_raise_for_phase` raises on
exit codes 3 (run budget), 4 (wave wall clock), 7 (humans needed), 8 (backend unavailable), 10
(wave budget). A step 8 that *"stops at the first halt, and exits 0"* **suppresses all five**, and
ships a green exit for a fleet that halted. I read *"exits 0"* as meaning *"the absence of step 8 is
no longer itself a non-zero exit"*, but the sentence does not say that. **This needs the
orchestrator to rule on the wording, and the ruling belongs in the ADR, not in a worker's head.**

### Seam C — "which repos does this run act on?" (`--repo` / `--from-phase`)

ADR-0079 §4 scopes `--repo` to steps 5, 6 and 8. Step 5 and step 6 will each need to honour it, and
step 8 passes it down as the delegates' `only` parameter (measured: `_transform_impl`,
`_build_impl`, `_verify_impl` and `_scan_impl` all take `only: str | None`). If step 8 filters
`computed_floors` by `--repo` **and** passes `only=repo` to the delegates, that is two filters and
they can disagree. **Close by construction:** one filter — apply `--repo` when computing
`computed_floors` in step 5, and let step 8 pass `only=repo` purely as a defence-in-depth argument
the delegates already understand, with a test asserting the two agree on a fixture where they
could differ.

### Seam D — a **fourth** composition root that row 10 does not name

⚠ **Row 10 says "the three composition roots". `phase_floor` can return `Phase.SCAN`, and there is
no third root for it.**

*Exercised, not read,* at `f36c9ad` under `.venv/bin/python`: `phase_floor({}, {})` returns **`1`**
(`Phase.SCAN`), and so does `phase_floor({p: None for p in Phase}, {})`. §11.5's own step-5 sentence
says *"and `SCAN` only if there is no such phase (Constraint 7)"*, and `phase_floor`'s docstring
repeats it. So a repo whose evidence does not hold anywhere — a repo whose worktree the reaper
removed, which `tests/test_cli.py::test_resume_step_5_demotes_a_repo_whose_worktree_the_reaper_
removed_all_the_way_to_scan` pins by name — gets floor `SCAN`, and `_transform_impl` /
`_build_impl` / `_verify_impl` cannot serve it. `_scan_impl` exists and takes `only`.

**Three options, costed, no recommendation between the first two:**

| option | cost | what it gives up |
|---|---|---|
| **D-1** four roots: `_scan_impl` → `_transform_impl` → `_build_impl` → `_verify_impl` | one more delegate; `_scan_impl` takes no `wave` and its flag surface (`skip_symbols`/`skip_classify`/`skip_contracts`/`concurrency`) has no resume equivalent, so step 8 must choose defaults **an operator never wrote** — the exact defect `src/fleet/models/tasks.py:100-102` records | nothing |
| **D-2** three roots; a floor of `SCAN` is **reported and skipped** | free | the repo silently does not continue; §10's *"continue from each repo's re-entry floor"* is then false for it |
| **D-3** three roots; a floor of `SCAN` **refuses the continuation** with a named exit | one guard | the fleet's other repos do not continue either |

**Agent Recommendation: D-2 with a loud payload key**, because a `SCAN` floor means the repo's clone
is gone and re-cloning it inside a *reconciliation* verb is a large, unbudgeted side effect an
operator did not ask for; and because D-1's flag defaults are a known defect shape. **But this is a
ruling, not a measurement, and row 10's "three" currently makes it silently by omission.**

---

## 6. What instruments already constrain this path, and which will redden

| instrument | does step 8 redden it? | measured basis |
|---|---|---|
| `tests/test_blocked_by_writer_statements.py` — one-front cap **per sink file** | **Not by wiring; YES if step 8 adds a caller inside `scheduler.py`.** Step 8 writes no `blocked_by` itself. But S8-5 shows it makes `scheduler.append_blocked_by` reachable from `fleet resume` through `runner`. The cap counts *functions in the sink file that call the primitive*, so a new caller **in `src/fleet/orchestrator/scheduler.py`** trips it; a caller elsewhere does not. | read the cap's assertion (`assert len(fronts) <= 1`, per `sink_file`) and its docstring's own *"The cap is per SINK FILE, not per tree — measured, not assumed"* |
| `tests/test_step6_wave_write.py` — `STEP6_SQL_TARGETS`, a `(verb, table)` whitelist over the `cli._unblock_dependents` window | **No, and that is the disclosure.** The window is opened by monkeypatching `cli._unblock_dependents`; step 8 executes entirely outside it. W28's disclosure — *a write relocated to step 5 or 7 leaves that window* — generalises: **step 8 is a whole new window with no whitelist over it at all.** | read the fixture: it wraps `cli._unblock_dependents` and records only while that call is on the stack |
| `tests/test_floor_rule_statements.py` Layer D (`_EXPECTED_STOP_CONDITION_SENTENCES`) | **No.** Deleting `ResumeIncompleteError` does not move the constant. | **Verified, not inherited.** Drove `_stop_condition_sentences()` directly at `f36c9ad`: **3** considered sentences — `docs/SPEC.md` ×1, `docs/DECISIONS.md` ×1, `src/fleet/orchestrator/reentry.py` ×1 — and **`src/fleet/cli.py` contributes 0**, exactly as that module's `_RESIDUAL` item 6 claims. Constant is 3; census returns 3 |
| `tests/test_floor_rule_statements.py` `_RESIDUAL` item 6's **prose** | **YES — it goes stale.** It says two `cli.py` sites state the rule in full, *"the operator-facing `ResumeIncompleteError` and `UsageError` messages"*, bound by two named `tests/test_cli.py` assertions. Deleting the class takes that from **2 sites to 1** and kills one of the two bindings. | read item 6; ADR-0089 §5 independently measured the same 2 sites, *"byte-identical, 185 normalised characters, at both"* |
| `tests/test_cli.py::test_resume_step_5_refusal_does_not_share_an_exit_code_with_a_crash` | **Deleted with the class** — its subject ceases to exist | it asserts on `ResumeIncompleteError`'s rendered output |
| `tests/test_cli.py::test_the_reconciliation_payload_is_emitted_before_the_step_5_refusal` | **Deleted or rewritten** — ADR-0076 records it as the *only* non-`--dry-run` `--json` resume test, so whatever replaces it must keep that property or the emit-ordering guarantee (S8-2) goes unguarded | ADR-0076's bullet, verified present |
| `tests/test_cli.py::test_the_step_5_refusal_does_not_call_step_2_absent_beside_its_own_step_2_lines` | **Deleted with the class.** ADR-0079 §7b already measured it **fail-open on step 6** (*"There is no step-6 clause at all"*) — so nothing is lost that was working | ADR-0079 §7b |
| `tests/test_cli.py::test_resume_refuses_the_flags_whose_behaviour_does_not_exist` | **YES, reddens** — it invokes `resume --from-phase 2` and asserts exit 2, and `--from-phase` is one of the two flags row 10 un-refuses. Row 10's criterion names this test for exactly that reason. **Additionally**, ADR-0079 §7b measured its built-steps assertion as *"a tripwire on the fix, not on the defect"* — a fixed substring `"steps 2, 3, 4, 5 and 7"` that **passes** under the defect and **fails** under the correction. Whoever edits this test must fix that too, or ship a third generation of the same fail-open | ADR-0079 §7b, whose measurement I did not re-run (see §8) |
| `tests/test_instruments_are_armed.py` | **Unknown — not measured.** See §8 | — |

---

## 7. Dispatch-ready decomposition of subtask 10

Assumes the orchestrator has ruled §1.3 (`stub_reconcile`), Seam D (`SCAN` floor), Seam B's *"exits
0"* wording, and S8-6's mirror-mutex question. **Every task below is one logical unit for one
worker.** Tasks marked ⚑ need an ADR or D-number the orchestrator allocates **at dispatch**; no
worker takes a number.

**The ordering fact the decomposition depends on, measured.** In `cli.resume`, the statement
`raise ResumeIncompleteError(...)` sits after `_emit`, after `if dry_run: return`, and after the
`PrEmissionError` branch; in `_resume_impl`, the step-2 reap is the last statement before `return`.
**Step 8 replaces that `raise` and goes nowhere else** — that single position satisfies S8-1, S8-2
and S8-4 simultaneously. Anchor by symbol, never by line: *the final statement of `cli.resume`*.

| # | Task | Files | Success criterion | Test file |
|---|---|---|---|---|
| **10a** | `cli._continue_from_floors` — the pure **plan**: `(floors: Mapping[str, Phase], only: str \| None) -> tuple[_Continuation, ...]`, one frozen entry per `(Phase, repos)` group in ascending phase order. **No I/O, no delegate call, no connection.** `floors` is passed **in** (Seam A); nothing re-reads `phases`. | `src/fleet/cli.py` | The same call on the same inputs returns the same tuple; a repo at floor `SCAN` appears in the plan with its own disposition rather than being dropped silently (Seam D); `--repo` filters in exactly one place (Seam C) | `tests/test_resume_continue.py` (new) |
| **10b** | The **delegation** unit: drive 10a's plan through `_transform_impl` / `_build_impl` / `_verify_impl` (+ `_scan_impl` iff Seam D rules D-1), passing `only`, stopping at the first halt, and calling **`_raise_for_phase`** on each payload — never a fourth copy of the exit-code mapping (Seam B). **No new `PhaseRunner` instantiation** (measured: 4 sites today, in `_run_scan_wave` / `_run_transform_wave` / `_run_build_wave` / `_run_verify_wave`). | `src/fleet/cli.py` | AST sweep: `PhaseRunner(` count is **4** before and after; the `HumanInterventionError`+`FleetCliError(exit_code=)` pairing class is **3** before and after; a delegate returning `exit_code=4` makes `fleet resume` exit **4** | `tests/test_resume_continue.py` |
| **10c** | The **guards** step 8 inherits (S8-6): `_require_disk_headroom` once before the first delegate, and the mirror mutex per the orchestrator's ruling. | `src/fleet/cli.py` | With `preflight.min_free_bytes` above free space, `fleet resume` exits **9** and **no delegate ran** (assert by a spy, not by output); steps 1–7 still committed | `tests/test_resume_continue.py` |
| **10d** | **Wire it**: replace `cli.resume`'s final `raise ResumeIncompleteError(...)` with the 10b call; **delete the class**; delete the two tests whose subject it was (`test_resume_step_5_refusal_does_not_share_an_exit_code_with_a_crash`, `test_the_step_5_refusal_does_not_call_step_2_absent_beside_its_own_step_2_lines`) and **re-home** `test_the_reconciliation_payload_is_emitted_before_the_step_5_refusal`'s emit-ordering guarantee onto the new non-`--dry-run` `--json` path (S8-2). | `src/fleet/cli.py`, `tests/test_cli.py` | AST over 115 `src/` blobs: `ResumeIncompleteError` class defs **1 → 0**, `ast.Raise` sites **1 → 0**, all `ast.Name` occurrences **1 → 0**; a `--json` resume whose continuation raises still emits a parseable payload carrying `git_arbitration` and `reentry_floors` | `tests/test_cli.py` |
| **10e** ⚑ | **The 27-test migration.** Whatever the orchestrator rules in §2.2 — a `--no-continue` flag (**needs an ADR + a §10 row edit**) or 27 fixture seams. **Measured scope: 27 `test_*` functions in `tests/test_cli.py` invoke `resume` without `--dry-run` and assert exit 2**; 22 of them are tests of steps 2/3/4/5 and are not about continuation. **One author, one commit, exact-match replacer that aborts on mismatch** (CLAUDE.md: a multi-site correction split across authors ships partial wording). | `src/fleet/cli.py`, `tests/test_cli.py`, `docs/SPEC.md` §10 row | Class result re-measured **against the artefact the fix produced**: "`test_*` invoking `resume` without `--dry-run`, asserting exit 2, and now running a real continuation" = **0**. Re-run the same AST predicate, not a grep | `tests/test_cli.py` |
| **10f** | **Un-refuse exactly two.** `_refuse_unbuilt_resume_flags`' `unbuilt` dict loses `--from-phase` and `--repo`; the other **three** stay (ADR-0079 §5 for the revalidation pair, §8 for `--reset-attempts`). Rewrite the `UsageError` body so it no longer says the refused flags scope a continuation **that now exists** — and, per ADR-0079 §7a, so it stops giving the revalidation pair a reason that is false for them (**this is §7a's 2-site class; nobody currently owns it — see §3**). Repair `test_resume_refuses_the_flags_whose_behaviour_does_not_exist`'s fixed-substring assertion, which ADR-0079 §7b measured as a tripwire on the fix rather than on the defect. | `src/fleet/cli.py`, `tests/test_cli.py` | Exercised per flag, **5 → 3** bodies refuse; `--from-phase 2` and `--repo x` now reach `_resume_impl`; the remaining message contains no occurrence of `PhaseRunner` or `Phases 1–4` **as a reason for the revalidation pair**; ADR-0079 §7a's class = **2 → 0** | `tests/test_cli.py` |
| **10g** | Honour the two un-refused flags end to end: `--repo` scopes steps 5, 6 and 8 and **not** 2, 3, 4, 7 (ADR-0079 §4); `--from-phase` is **option C(i)** — the floor is never overridden, clamped or capped, and a repo whose floor sits **below** the flag is **skipped, not re-aimed** (ADR-0079 §2). | `src/fleet/cli.py` | A repo whose computed floor is 2 under `--from-phase 3` is **absent** from the continuation and **present** in the payload as skipped; the reap and the projection are unaffected by `--repo` | `tests/test_resume_continue.py` |
| **10h** ⚑ | Record the ADR (orchestrator numbers it) and rewrite design row 10. It must carry: the §1.3 ruling; the Seam-D `SCAN` ruling; the Seam-B *"exits 0"* wording; the S8-6 mirror-mutex ruling; the S8-8 wall-clock disposition; and the §2.2 ruling. **Correct row 10's own criterion at the root, not only the sentence** — *"the three composition roots"* and *"exits 0"* are both edits, and CLAUDE.md's Guardrail 7 records that a remedy scoped to its reported site regenerates the class. | `docs/DECISIONS.md`, `docs/superpowers/plans/design-resume-step5.md` | Row 10 no longer says "three" if Seam D ruled D-1, no longer says "exits 0" unqualified, and names the real file set | n/a |
| **10i** | Update `resume.__doc__` and `docs/SPEC.md`'s §10 `fleet resume` row in the **same commit** as 10d (Guardrail 7: *"Fix the code and its doc listing in the same change"*). The docstring's *"Step 8 does not exist"* paragraph and its exit-2 sentence both become false at 10d. | `src/fleet/cli.py`, `docs/SPEC.md` | Normalised whole-file sweep (whitespace collapsed across the file, offsets mapped back to lines — **not** a line-oriented grep): the class *"a tracked file says `fleet resume` cannot continue / step 8 has no implementation"* goes to **0**, with quotations inside dated retraction markers subtracted **by rule** | re-run the sweep as evidence |
| **10j** | The whitelist-shaped instrument for the new window (§6's second row: step 8 has no `STEP6_SQL_TARGETS` equivalent). A `(verb, table)` capture over the step-8 window, plus the **four** Rule-12 checks: fires on known-bad (a continuation that writes a `waves` row directly), silent on a clean tree, fires on a **synthetic fault** injected into a clean one, and a **control** — a cosmetic reflow of the delegated SQL — stays **green**. | `tests/test_resume_continue.py` | All four checks reported **with their numbers**; the disclosed residue stated (a side effect routed only through whitelisted pairs passes) | itself |

**The pytest command a later lane must run** (I did not run it — suite lock), with **no `-k`
filter**:

```
.venv/bin/python -m pytest tests/test_cli.py tests/test_resume_continue.py \
  tests/test_floor_rule_statements.py tests/test_blocked_by_writer_statements.py \
  tests/test_step6_wave_write.py tests/test_resume_unblocking.py tests/test_state_models.py \
  tests/test_instruments_are_armed.py tests/test_reentry_floor.py tests/test_reentry_evidence.py \
  tests/test_scheduler.py tests/test_projection.py
```

and **`python -m mypy` with no path arguments**, so `pyproject.toml`'s manifest sets the scope
(`strict` + `packages = ["fleet"]`).

---

## 8. What this report cannot catch

- **No suite run.** Every runtime number is from a standalone `.venv/bin/python` script with
  `PYTHONPATH` pinned and `fleet.__file__` asserted. **Exercised:** the five-flag refusal (per
  flag), `phase_floor`'s `SCAN` return, and Layer D's census callable. **Read from source, not
  exercised:** `_LIVE_SANDBOX_PREDICATE`'s effect on a real reap, `open_wave`'s refusal,
  `status_of`'s per-phase scoping (I read that it takes `self.phase`; I did not build a
  two-phase-scheduler fixture to prove S8-7's first bullet), and `_raise_for_phase`'s behaviour on a
  real delegate payload. **S8-7's first bullet is the weakest link in this report** and deserves a
  fixture before it is written into an ADR.
- **I did not re-run ADR-0079 §7b's two instrument measurements.** The "tripwire on the fix" and
  "fail-open on step 6" findings in §6's table are **inherited from ADR-0079 §7b at `f4eade0`**, not
  re-measured at `f36c9ad`. Per CLAUDE.md's rot rule, **task 10f must re-measure them at the moment
  it acts** — a finding true when filed can be false when you act on it.
- **`tests/test_instruments_are_armed.py` is unexamined.** I ran out of budget before checking
  whether it guards anything on this path. It has been red on `main` before, in exactly the way it
  exists to detect in others (`f1e9686`). **A lane should run it at a clean tree before 10d.**
- **The 27-test class is a lower bound.** The predicate is `"resume"` in the source segment **and**
  an exit-2 assertion **and** no `--dry-run` substring in the body. A test that reaches `resume`
  through a helper, or that passes `--dry-run` on one invocation and not another (12 such bodies
  exist), is counted on the wrong side. The **class** statement I stand behind is *"at least 27
  currently-exit-2 non-dry-run resume tests will start executing a continuation"*; the raw total is
  the part that will not reproduce.
- **§1.3 is a reading of three SPEC sources against one.** §10's row, §13 row 35 and §3.5.1 put
  `stub_reconcile` in `fleet resume`; §11.5's numbered list does not. I quote all four at length
  above precisely so the orchestrator checks the reading rather than inherits it. The load-bearing
  move is that *"step 8 is the sole remaining absence"* is a claim scoped to one section, stated
  without that scope in an ADR title.
- **Seam D's recommendation (D-2) is a judgement, not a measurement.** The measurement is only that
  `phase_floor` returns `Phase.SCAN` and that row 10 names three roots.
