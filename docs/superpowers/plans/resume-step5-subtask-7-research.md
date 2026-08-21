> Round-D research artifact, produced by lane R2 (`design-resume-step5`), promoted unchanged from `.superpowers/` scratch by lane W9. **§§1–3 are history — subtask 7 landed at `2f0db34`..`1d0e39c` and the I4 boundary and the dry-run D74 seam were both closed in it. §4 is the live half**: it is the subtask-**9** brief (the `--from-phase` refusal split and ambiguity 4's three options, costed, unpicked) and is what ADR-0079 must settle.

# R2 (round D) — source-verified brief for **subtask 7: wire §11.5 step 5 into `_resume_impl`**

**Everything below is measured at `HEAD = 6bf198f1b5cfe6e649dc508556238744823ce61d`**
(`6bf198f`, `main`). `git status --porcelain` at read time showed **one** entry, `?? .superpowers/`
— i.e. **no lane had uncommitted edits to `src/`, `tests/` or `docs/` while I read.** Every file was
read through `git show HEAD:<path>` into a lane-private export under
`…/scratchpad/R2/`, never through the working tree, so nothing here can be another agent's
in-flight edit. Sibling commits already in `HEAD`: `fe743e6` (subtask 4), `4cde582`, `f141af6`,
`92fefb7`, `b4567a5`, `6bf198f`.

Read-only lane. I wrote no repository file except this report.

---

## 0. Premises in the dispatch brief that measured **false** at `6bf198f`

| # | Premise as briefed | Measured |
|---|---|---|
| P1 | Design §4: `_resume_impl` ends with "`raise CommandUnavailableError(...)` naming step 5" | **False.** `resume()` raises **`ResumeIncompleteError`** (ADR-0076). `CommandUnavailableError` is not the resume path. |
| P2 | Design §4: step 5 goes "immediately after `_reset_stale_running` and immediately before `project_once`" | **Half false.** The *boundary pair* still exists, but the two are no longer adjacent: `_reconcile_tasks_with_git` (step 4, `fe743e6`) sits between them, and `HEAD` carries an explicit 3-line marker comment placing step 5 **after step 4**. See §1. |
| P3 | Design §4's numbered order of `_resume_impl` (7 items) | **Stale.** Steps 4 and 2 did not exist when it was written; step 2's reap now runs **below** `project_once` (ADR-0081). Full current order in §1. |
| P4 | Design §5 row 7: files touched are `cli.py` (`_resume_impl`, `_resume_lines`) + `tests/test_cli.py` | **Incomplete.** Subtask 7 must also touch the `ResumeIncompleteError` message text (see §4.4), which `tests/test_cli.py:2149` and `:1269` assert on verbatim. Whether `repository.py` is also touched depends on the D74 ruling in §3.3. |

Two premises I was told were established and which **re-measured true**: `phase_floor` has no caller
in `src/` (`git grep phase_floor HEAD -- src` → the definition and prose only), and `evidence_holds`
has **no definition** in `src/` (two docstring mentions in `reentry.py`). V1's finding **I4** is
accurate as written, including its own caveat that the per-row decisions *are* consistent — see §2.

**One message-routing note.** Mid-task I received a controller message addressed *"Controller →
W4"* about `evidence_holds` and ADR-0087 §4. I am R2, read-only; I did not act on it as W4. Its one
fact that bears on subtask 7 is folded into §5 as a **precondition subtask 7 inherits**, verified
against `docs/DECISIONS.md` at `HEAD`.

---

## 1. Q1 — the insertion point: still there, now better specified by the code itself

### 1.1 `_resume_impl`'s actual statement order at `6bf198f`

Cited by symbol, per the brief. Every item is a top-level statement of `_resume_impl` in source
order.

| # | Statement | §11.5 |
|---|---|---|
| 1 | `conn = await connect_ro(path)` … `try:` — `_resolve_run`, `SELECT config_digests FROM runs`, `settings.drifted_sections(baseline)`, `_earliest_open_wave` | step 1 |
| 2 | `_validate_accept_drift` → `UsageError` on unaccepted drift | step 1 |
| 3 | the `opts.profile is not None` refusal | step 1 (§10) |
| 4 | `await _refuse_exhausted_wave(...)`; `finally: await conn.close()` | step 1 |
| 5 | `_refuse_bad_raise_budget` through `_with_ro`, **read-only, in both modes** | — |
| 6 | `_record_drift_findings` — guarded `not dry_run and accepted` | — |
| 7 | `_raise_wave_ceiling` — guarded `not dry_run and …` | — |
| 8 | `_raise_run_ceiling` — guarded `not dry_run and …` | — |
| 9 | the `--repoll-prs` block: `_pr_sync_impl` or one of `not-requested` / `skipped-dry-run` / `failed` | §3.4 |
| 10 | **the `stub_reconcile` reservation comment** (no code; ST1's slot) | §13 row 45 |
| 11 | `now = _now()`; `horizons = (_iso(now - stale_after_s), _iso(now))` | step 3 prologue |
| 12 | `if dry_run: _count_stale_running(...) else: _reset_stale_running(...)` → `stale` | **step 3** |
| 13 | `arbitration = await _reconcile_tasks_with_git(settings, path, run_id, dry_run=dry_run)` | **step 4** |
| 14 | **the §11.5 step-5 marker comment** — three lines, verbatim: *"the demotion to each repo's re-entry floor — goes HERE, between step 4 and the projection"* | **step 5 slot** |
| 15 | `projection = None`; `if not dry_run: projection = str(await project_once(path, run_id=UUID(run_id), path=DEFAULT_PROJECTION_PATH))` | **step 7** |
| 16 | `live_names = _live_sandbox_names(...)`; `_reap_orphan_worktrees(...)`; `_reap_orphan_containers(...)` | **step 2** (ADR-0081) |
| 17 | `return { … }` — the payload dict |

### 1.2 Verdict: **yes, the insertion point exists, and both stated properties still hold** — with a third added

Put step 5 **at item 14**, i.e. after the `arbitration = await _reconcile_tasks_with_git(...)`
assignment and above the `projection: str | None = None` declaration. `HEAD` already reserves that
exact slot with a comment; **do not move it, and do not delete the comment** — replace it with the
implementation plus the same reasoning, the way item 13 did to its own slot.

- **Property 1 — after the stale sweep — HOLDS.** Item 12 still precedes it. The mechanism the
  design cited is intact and is now *provable* rather than argued: `_DEMOTE_PHASE_SQL` carries
  `AND status = 'SUCCEEDED'`, and `demote()` (`models/enums.py:149`) raises on any status outside
  `RESUME_DEMOTE`, so a `RUNNING` row is unreachable by the demotion write in either direction.
  The ordering is still what makes the *floor* right (a `RUNNING` row is not settled, so it becomes
  the frontier), not merely what avoids a collision.
- **Property 2 — before `project_once` — HOLDS**, unchanged. Item 15 is the only `project_once`
  call in `_resume_impl`.
- **Property 3 — NEW, and it is now the tighter constraint: after step 4.** `evidence_holds(r,2)`
  and `evidence_holds(r,3)` both resolve `phases.post_commit_sha`, and item 13 is what reconciles
  that column against Git. The `HEAD` marker comment states this itself. Step 5 above step 4 would
  demote on a pointer nobody has validated.

### 1.3 Two ordering worries I checked and can **rule out**

- **Step 5 → step 2 (item 16) interaction: none.** `_LIVE_SANDBOX_PREDICATE` is
  `" AND status = 'RUNNING' AND NOT (1" + _STALE_HEARTBEAT_PREDICATE + ")"`. `demote_to_floor`
  writes only `SUCCEEDED → PENDING`. A demotion therefore cannot add to or remove from
  `live_names`, so placing step 5 above item 16 cannot make the reap sweep a sandbox it would
  otherwise have spared. Measured from the predicate text, not reasoned from intent.
- **Step 5 → the `stub_reconcile` marker (item 10): unchanged and satisfied.** Step 5 at item 14
  is five statements *below* the marker, which is the direction §13 row 45 requires. Leave item 10
  untouched — design §5 row 7's success criterion says so and it remains correct.

---

## 2. Q2 — V1 finding **I4**: the transaction boundary on `demote_to_floor`

### 2.1 What is actually inside the transaction (measured, `state/repository.py::demote_to_floor`)

Inside the `unit` closure run under `StateWriter`'s `BEGIN IMMEDIATE`:

1. `_DEMOTE_SELECT_SQL` = `SELECT phase, status FROM phases WHERE run_id = ? AND repo_id = ?` —
   a fresh read of **all** statuses.
2. `if RepoStatus.REQUIRES_HUMAN_INTERVENTION in rows.values(): return ()`.
3. For each `phase` in `span = (p for p in Phase if p >= floor)`: skip unless the **in-transaction**
   status is `SUCCEEDED`; then `demote(...)` + `_DEMOTE_PHASE_SQL` (which itself re-guards
   `AND status = 'SUCCEEDED'`).
4. `checkpoints.delete_in_unit(...)` over `[p for p in span if rows.get(p) is not DEGRADED]`,
   conditional on `if demotions:`.
5. One `_DEMOTE_FINDING_SQL` per demotion, fingerprinted per phase.

So the *membership* decisions are all in-transaction. **The only value crossing the boundary is
`floor`** — the span's lower bound — and, transitively, the `evidence` that produced it. V1's I4
states this correctly and already concedes the per-row consistency point; I found nothing to
correct in it.

### 2.2 The concrete interleaving that produces a **wrong demotion** once subtask 7 wires a caller

Only one direction is reachable, and it is reachable **by design**, not adversarially.

> **Over-demotion of legitimately-landed work.** `_resume_impl` deliberately spares non-stale
> `RUNNING` rows (item 12 resets only rows past *both* horizons; `_LIVE_SANDBOX_PREDICATE`'s
> comment says in terms that such a row is "live"). Take repo *R* with phases 1–2 `SUCCEEDED` and
> phase 3 `RUNNING` under a live worker in **another process**.
> `phase_floor` reads through `mode=ro`: phase 3 is not in `_SETTLED_FOR_DEMOTION`, so
> `frontier = 3`; the backward walk finds phase-2 evidence holding, so `floor = 3`.
> `evidence_holds` then does git I/O over the whole fleet — seconds to minutes.
> In that window the live worker's `complete_phase` writes phase 3 → `SUCCEEDED`.
> `demote_to_floor(floor=Phase.BUILD)` now re-reads, finds phase 3 `SUCCEEDED` **in-transaction**,
> and demotes it: status → `PENDING`, the `checkpoints` rows for phases 3–4 deleted, a
> `PhaseDemoted` **warn** finding minted whose `reason` string describes evidence that never failed.
> Nothing in the method is wrong on its own terms — the row *was* `SUCCEEDED` in the span. The span
> is wrong, because `floor` was computed when phase 3 was the unsettled frontier.

Blast radius, stated rather than implied: one BUILD (or VERIFY) phase re-runs from its anchor;
`attempts` is **retained** (`_DEMOTE_PHASE_SQL`'s SET list omits the column; `complete_phase` is
the only writer of `phases.attempts` in `src/`), so no rung of ADR-0014's ladder is spent; and the
audit record misattributes the cause. Not corruption, but the single most expensive unit of work in
the harness, thrown away, with a finding that says it was thrown away for a reason that was false.

The three other interleavings are **not** hazards, measured:

| Interleaving | Outcome |
|---|---|
| A phase *loses* evidence between the reads (floor too high) | Under-demotion only; the next resume catches it. Nothing in `src/` un-succeeds a phase except this method. |
| RHI appears between the reads | Caught — the in-transaction RHI check returns `()`. |
| RHI disappears between the reads | Unreachable: `OPERATOR_REOPEN` has no caller in `src/`. |
| **Two concurrent `fleet resume`s** | **Safe.** The second's per-row `SUCCEEDED` guard matches zero rows and its `if demotions:` sweep does not fire. Idempotent, as ADR-0076's exit-2 message promises. |

### 2.3 Does existing `StateWriter` machinery close it? **No.** Measured, not assumed

- `state/db.py` module docstring, verbatim: *"The slot is process-wide module state"*.
  `_acquire_write_slot` / `_write_slot_owner` are a module global. `SingleWriterViolationError`
  therefore excludes a second writer **in this process only**. The hazard above needs two
  processes, which nothing here excludes.
- `BEGIN IMMEDIATE` (`db.py`, `_in_transaction`) serialises *transactions*. It carries no snapshot
  from the earlier `mode=ro` read — a different connection entirely (`connect_ro`, `?mode=ro`).
- **No file lock, no advisory lock, no run-level claim.** `grep` for `flock|fcntl|BEGIN EXCLUSIVE`
  over `state/db.py` at `HEAD` returns nothing but the `BEGIN IMMEDIATE` sites and `migrate-db`'s
  documented `BEGIN EXCLUSIVE` elsewhere.
- **A lease-based in-transaction guard cannot help, and this is the fact that kills the obvious
  fix.** `complete_phase`'s SQL sets `lease_owner = NULL, lease_expires_at = NULL` in the same
  statement that sets the status. So the moment the racing worker succeeds, the row has no lease.
  Adding `AND lease_owner IS NULL` to `_DEMOTE_PHASE_SQL` would be exactly blind to the one
  interleaving that matters. (`_LIVE_SANDBOX_PREDICATE`'s own comment already records the sibling
  reasoning: `lease_owner IS NOT NULL` "is deliberately NOT part of this".)

### 2.4 Recommendation, with the cost if it is wrong

**Recommended: pass the caller's observed snapshot into `demote_to_floor` and make the floor a
property of the write.** Concretely, add one keyword-only parameter — `observed: Mapping[Phase,
RepoStatus]`, the statuses `phase_floor` was given — and, inside the `unit` closure, immediately
after the existing `_DEMOTE_SELECT_SQL` read and the RHI check, compare `observed` against the
in-transaction `rows` over the span; on any difference, **return `()` without writing anything**.

Why this one:
- It is the *same argument the RHI paragraph in the method's own docstring already makes and wins*
  — "a refusal that lives only in the caller is a refusal the next caller can forget" — applied to
  the value V1 says is unprotected. It closes I4 by extending an existing mechanism rather than
  adding a second one.
- ~4 lines, pure SQL/pure Python, **no new I/O inside `BEGIN IMMEDIATE`** (the module docstring
  forbids network, git and LLM in a write unit; recomputing `evidence` inside the transaction
  would violate that, which is why V1's alternative — "hand `demote_to_floor` the `evidence`
  mapping and let it call `phase_floor` inside the unit" — is the **worse** of its own two
  suggestions unless `evidence` is precomputed and passed as a plain mapping).
- The caller already has the snapshot: subtask 7 must read `phases` once to build `rows` for
  `phase_floor`, and `PhaseRow` carries `status` (and `updated_at`, if a stricter witness is
  wanted).
- It is strictly stronger than today's per-row CAS, which can only catch `SUCCEEDED → other`; the
  live hazard is `RUNNING → SUCCEEDED`, which today's guard cannot see.

**Cost if this recommendation is wrong** (i.e. if no concurrent writer ever exists in practice):
a resume that raced any concurrent `phases` write becomes a **no-op for that repo** and the
operator must run `fleet resume` again. That is bounded by one extra invocation and is explicitly
sanctioned — ADR-0076's refusal text already states the verb "is safe and idempotent" to re-run,
and re-running costs nothing but local reads plus one forge call per open PR *only* under
`--repoll-prs`. The failure mode of the recommendation is a spurious retry; the failure mode of
doing nothing is a discarded BUILD phase with a false audit record.

**Do not confuse this with ADR-0082 §4.** That section discloses a *different* hazard — that
`demote_to_floor` accepts any `Phase` as `floor` from any caller and the coupling to `phase_floor`
"is a **docstring sentence** … not a check" — and deliberately does not patch it, on the Rule 12
stop rule (a normal author writing subtask 7 against `phase_floor` cannot reach it). I4 is the
*staleness* of a floor that **was** computed by `phase_floor`, and a normal author reaches it by
doing exactly what the design says. ADR-0082 §5 says in terms: *"If subtask 7's floor ever comes
from anywhere but `phase_floor`, §4's premises must be re-derived, not re-read."* Subtask 7's floor
does come from `phase_floor`, so §4 stands untouched — and I4 is orthogonal to it.

**I did not implement anything.** This is a recommendation for the controller to rule on; the
change lands in `state/repository.py` (subtask 6's file), so if it is taken it belongs either to
subtask 7's diff explicitly or to a follow-up, not to an implementer's discretion.

---

## 3. Q3 — `--dry-run`, and the shape that stops the two paths drifting

### 3.1 What `_resume_lines` does now

`_resume_lines(result)` is a pure renderer over the payload `Mapping` — it performs no I/O and
takes no `dry_run` argument of its own (it reads `result["dry_run"]`). Its structure, in emission
order:

```
[ header: "dry-run: run … is resumable"   |   "run …: N stale RUNNING row(s) reset …" ]
+ "  step 3: …"                (dry only)
+ _arbitration_lines(result, dry=dry)      # step 4
+ _reap_lines(result, dry=dry)             # step 2
+ _budget_lines(result)
+ _repoll_lines(result)
+ "  projection at …"          (non-dry only)
```

`_arbitration_lines` and `_reap_lines` are the pattern to copy: each takes `dry` and swaps only the
**verb** (`"would ask"`/`"asked"`, `"would adopt"`/`"adopted"`, `"would reap"`/`"reaped"`), while
the *content* of every line comes from the same payload entries on both paths. Both carry a D44
docstring: every entry in a failure/unresolved list is printed individually with its reason,
because a count collapses two opposite facts into one line. Step 5's lines owe the same
discipline — "3 repos demoted" cannot tell an operator whether landed work was discarded or a
frontier was merely confirmed.

**Where step-5 lines go:** immediately after `_arbitration_lines(...)` and before
`_reap_lines(...)`. That matches execution order (§1.1 items 13 → 14 → 16) and matches the existing
choice to print step 4 before step 2.

### 3.2 The two existing dry-run models, and which one to follow

| | step 3 | step 4 |
|---|---|---|
| Shape | **two functions** — `_count_stale_running` (ro) / `_reset_stale_running` (writer), branched at the call site | **one function** — `_reconcile_tasks_with_git(..., dry_run=)`, branched *internally, at the last statement* |
| What stops drift | a shared constant: both SQL texts are built from `_STALE_HEARTBEAT_PREDICATE` | nothing to share — there is only one computation; `if dry_run or not (landed or discarded): report["applied"] = False; return report` sits above the single `_persist_arbitration` call |
| Reports "computed" vs "written" | `stale` (a count) + `result["dry_run"]` | `report["applied"]`, an explicit boolean |

**Follow step 4, not step 3.** Step 3's two-function split needed a shared constant precisely
because it *is* two routes; step 4 has one route and therefore nothing to keep in agreement. Step 5
is naturally single-route: `phase_floor` is pure, `evidence_holds` is read-only, and only
`demote_to_floor` writes. The whole plan can be computed identically on both paths.

Recommended skeleton for subtask 7 (shape, not code to paste):

```
async def _demote_to_floors(settings, path, run_id, *, dry_run) -> dict[str, object]:
    rows_by_repo = <ONE ro read of every `phases` row for run_id>     # not 4×N get_phase calls
    report = {"candidates": 0, "demoted": [], "unchanged": [], "unresolved": [], "applied": False}
    for repo, rows in rows_by_repo:
        evidence = await evidence_holds_for(...)     # git READS only, both paths
        floor = phase_floor(rows, evidence)
        if floor is None: -> "unchanged" with the reason (RHI / all settled)
        plan = _demotable_phases(rows, floor)        # <-- see 3.3
        ...
    if dry_run or nothing planned: return report     # `applied` stays False
    <open ONE StateWriter, construct SqliteStateRepository, call demote_to_floor per repo>
    report["applied"] = True
    return report
```

Two mechanical points on that skeleton:

- **`SqliteStateRepository(writer=writer, read_conn=read_conn)`** is the established construction
  (six sites in `cli.py`: `:1744`, `:4333`, `:7878`, `:8323`, `:8932`, `:9255`). `_resume_impl`
  builds none today. The process-wide write slot means step 5's `StateWriter` must be opened and
  closed **outside** any other — items 12 and 13 each open and close their own sequentially, so a
  third sequential one is fine; a nested one raises `SingleWriterViolationError`.
- Read all four phases for all repos in **one** query. `get_phase` is a single-row read; 4×N calls
  per resume is the wrong shape and there is no batch accessor on the Protocol today.

### 3.3 **D74 flag — yes, the natural implementation computes the plan twice, and nothing enforces agreement**

This is the one place subtask 7 will reproduce D74's shape, and it is not hypothetical:

`demote_to_floor` returns `tuple[PhaseDemotion, ...]` — the demotions **actually applied**, derived
from *its own in-transaction* `rows`. Under `--dry-run` that method is never called, so the printed
plan must be derived some other way. The obvious other way is a second comprehension in `cli.py`
that re-implements the method's own membership rule:

```
span = (p for p in Phase if p >= floor)  and  rows[p] is SUCCEEDED
```

Two routes to the same set, in two files, with nothing binding them. Drift is then a one-line edit
away: any future change to `demote_to_floor`'s membership rule (e.g. if `SKIPPED` were ever added to
`RESUME_DEMOTE`, or if the recommendation in §2.4 lands and adds an abort path) silently makes the
dry-run preview a lie, while every test stays green because no test compares the two.

**Two ways to close it. The first is a mechanism; the second is a test.**

1. **(Mechanism — recommended.)** Extract the membership rule as a pure function in
   `orchestrator/reentry.py`, e.g. `demotable_phases(rows: Mapping[Phase, PhaseRow | None], floor:
   Phase) -> tuple[Phase, ...]`, and have **both** the dry-run planner and `demote_to_floor`'s
   `unit` closure call it — the only difference being *which* snapshot of `rows` each is handed.
   The two routes collapse into one. Cost: subtask 7 must touch `state/repository.py`, which design
   §5 row 7 does not list (see P4 in §0), and `reentry.py`'s current no-I/O purity must be
   preserved (this function is pure, so it is).
2. **(Test-only fallback.)** Keep the two routes and add a round-trip test: on one fixture, assert
   the `--dry-run` plan is **equal** to the set `demote_to_floor` returns on the same fixture with
   `--dry-run` off. This is weaker and CLAUDE.md Rule 12's stop rule is explicit about the
   difference — "never close a documentary gap with a convention wearing a mechanism's clothes."
   If it is taken anyway, the test must be a **mutation-verified** one: perturb the `cli.py`
   comprehension only (e.g. `>= floor` → `> floor`) and confirm the round-trip test goes red while
   every other resume test stays green. Confirm `git diff` reports non-zero changed lines for that
   mutation **before** reading the test result.

Either way, the payload must carry an explicit `"applied": bool` (step 4's key), not merely
`"dry_run"` at the top level: `raise_budget_applied` exists in the payload for exactly this reason
and its own comment records the defect that produced it — *"a payload that reported only the request
let `--dry-run --raise-budget 50 --json` assert a cleared halt beside `"dry_run": true`"*.

### 3.4 What `--dry-run` may and may not do here

`--dry-run` must make **no network call** (the whole reason `--repoll-prs` is skipped under it) and
**write nothing**. `evidence_holds` performs *local git reads* — the same category step 4's dry-run
already performs and documents ("every git READ, no git or SQL write"). So a dry-run step 5 is
consistent with the promise as `HEAD` states it. If `evidence_holds` as landed by W4 ever reaches a
remote, that promise breaks and `resume()`'s docstring — which currently says `--dry-run` "makes no
network call" — becomes false; subtask 7 should verify that against the landed `evidence_holds`
rather than inherit it.

---

## 4. Q4 — what the un-refusal in subtask 9 gates

### 4.1 The split, verified against `_refuse_unbuilt_resume_flags` at `HEAD`

The function refuses exactly five, from its `unbuilt` dict:

```
"--from-phase": from_phase is not None      -> subtask 9 REMOVES
"--repo": repo is not None                  -> subtask 9 REMOVES
"--reset-attempts": reset_attempts          -> subtask 9 REMOVES
"--revalidation": revalidation is not None                       -> STAYS
"--raise-revalidation-rounds": raise_revalidation_rounds is not None -> STAYS
```

**The split is correct**, and the "different workstream" claim re-measures true from primary
source, not from the design doc:

- `docs/SPEC.md` §10 gives `--revalidation eager|batched|manual` on **`fleet stubs resolve`** as
  well as on `fleet resume` — it is the §3.5.1 stub-lifecycle knob, and `resume()`'s parameter is
  typed `Revalidation | None`, the same enum.
- `docs/SPEC.md` §13 **row 34** is *"Revalidation storm"*, whose control is
  `max_revalid…`/`revalidation_key`/`repo_ledger.revalidation_usd` — the cost class those two flags
  raise. Nothing in row 34 is a re-entry floor.
- `--from-phase` / `--repo` / `--reset-attempts` genuinely scope or re-drive the floor computation.

### 4.2 One defect subtask 9 inherits, in the same function

`_refuse_unbuilt_resume_flags`'s **docstring** opens: *"Every flag below narrows or re-drives §11.5
step 5, the phase re-entry that is not built."* That sentence is **already false for two of the five
flags** by §4.1, and it becomes false for a second reason the moment subtask 7 lands (step 5 *is*
built). Guardrail 7 — "fix the code and its doc listing in the same change" — makes this the
un-refusal's to correct, not a later sweep's. Flagging it here so subtask 9's implementer does not
inherit it as a premise.

> **SUPERSEDED IN PART (2026-08-21), lane W13 — the docstring sentence quoted above no longer
> exists in `src/`, so the quotation is a RETRACTION QUOTATION from here on, not a live citation.
> The *first* of the two reasons this section gives is NOT superseded and §4 is NOT settled.**
> `c135c42` rewrote both `cli._refuse_unbuilt_resume_flags`' docstring and the `UsageError` that
> repeated it; the docstring now states that §11.5 step 5 **is** built and runs unconditionally,
> and that what is absent is steps 6 and 8. The quotation is kept as the record of what this brief
> was reacting to — it was verbatim and correct at this document's own anchors (`6bf198f`,
> re-anchored at `7275adb` in §7), which both predate `c135c42`. Anyone building subtask 9 must
> take the docstring's present wording from `src/fleet/cli.py` by symbol, not from here.
>
> **What `c135c42` closed is only the SECOND reason — that the sentence became false once step 5
> was built (`2f0db34`). The FIRST reason remains OPEN and is still subtask 9's:** §4.1 measures
> `--revalidation` and `--raise-revalidation-rounds` as §3.5.1 stub-lifecycle knobs rather than
> floor knobs, and `c135c42` did not change the refusal's membership — all five flags are still
> refused. Whether those two belong in `_refuse_unbuilt_resume_flags` at all is unresolved here
> and is **ADR-0079's to settle**; §4.1's split, §4.3's three unpicked options, §4.4 and §4.5's
> open conflict all still stand, and Guardrail 7's "same change" obligation above still binds
> subtask 9 for that first reason. This marker deliberately does **not** re-quote the dead
> sentence: a sweep for that sentence's text still finds it in exactly one passage of this file,
> the quotation above. (A *cue*-based sweep does gain a match here, on the substring `unbuilt`
> inside the symbol name `_refuse_unbuilt_resume_flags` — a name, not a claim.)

### 4.3 Ambiguity 4 — the three `--from-phase` options, with costs. **I am not picking one.**

First, three constraints that bind **all three** options, measured:

- **`--from-phase` is typed `int | None` with Typer `min=1, max=4`**, not a `Phase`. Whatever is
  ruled, the conversion `Phase(from_phase)` happens in `_resume_impl` or `resume()`.
- **Today the flag can only change *which rows are demoted*, never where anything re-runs.** Step 8
  ("continue") does not exist (design §5 row 10, subtask 10). `demote_to_floor`'s span is
  `phase >= floor` over `SUCCEEDED` rows only. So under a subtask-7+9 tree, all three options are
  observable *only* as different demotion sets. The semantic difference between them fully
  materialises at subtask 10. A ruling now is still the right call — it fixes what subtask 9's
  tests may assert — but the ruling's real consequence is deferred, and subtask 9's tests can only
  bind the demotion sets.
- **`SPEC.md` §10's `fleet resume` row already contains a sentence that one of the three
  contradicts** (see option A). Guardrail 7's "two edits, not one": whichever option is ruled, the
  SPEC sentence must be reconciled in the same change or it regenerates the defect.

---

#### Option A — **override**: `floor := Phase(from_phase)`, ignoring the computed floor

*What it buys.* The simplest thing to explain and the only one that does what an operator naively
expects from the flag's name. One line.

*What it costs.*
1. **It can point the floor ABOVE the computed floor**, i.e. leave un-demoted a phase whose durable
   evidence does not hold. That is precisely the failure Constraint 7 exists to prevent, converted
   into an operator-invocable footgun. (Today: the repo simply keeps a `SUCCEEDED` row it should
   not have. At subtask 10: it re-enters at a phase whose inputs are not there.)
2. **It can land the floor ON a `DEGRADED` or `SKIPPED` phase**, which `phase_floor` structurally
   refuses (`_HARD_STOPS`, tested *before* evidence). `demote_to_floor` would then not demote it —
   `demote()` raises on any status outside `RESUME_DEMOTE`, so the write is safe — but the phases
   *above* it are demoted with a floor the walk would never have produced, which is exactly the
   provenance hazard ADR-0082 §4 disclosed and declined to patch **on the stated premise that
   subtask 7's floor comes from `phase_floor`**. Ruling A **invalidates that premise**, and
   ADR-0082 §5 says in terms that §4's premises must then be *re-derived, not re-read*. So A's true
   cost includes reopening ADR-0082 §4 and deciding whether to patch the hazard it declined to.
3. **It contradicts a committed SPEC sentence.** `docs/SPEC.md` §10, `fleet resume` row: *"continue
   from each repo's re-entry floor (§11.5 step 5), **never the earliest incomplete phase**."* An
   override makes the verb continue from `from_phase`, which is not the re-entry floor. Options B
   and C preserve that sentence; A requires editing it.
4. It needs its own carve-out prose for the RHI case (`phase_floor` returns `None`; does
   `--from-phase` override that too? It must not — §12 item 46 (ii) — so A gains an exception the
   other two do not need).

*Cost if ruled and wrong:* the two classes ADR-0077 §5 and Constraint 7 were written to prevent —
a bypassed revalidation budget and a re-entry against a tree that does not match — reachable by an
ordinary operator typing an ordinary flag.

---

#### Option B — **clamp**: `floor := min(computed_floor, Phase(from_phase))`

*What it buys.* Monotone-safe by construction: the evidence-derived floor is a **ceiling** on how
high re-entry may begin, so B can never skip a phase whose evidence fails. Constraint 7 and the
SPEC sentence in A.3 both survive unedited. ADR-0082 §4's premise survives: the floor is still
`phase_floor`'s output, only lowered.

*What it costs.*
1. **It needs a hard-stop rule of its own, and this is the real cost.** `min` can push the floor
   *below* a `DEGRADED`/`SKIPPED` row that `phase_floor` deliberately refused to walk past. The
   walk's stop protects "a decision resume may not re-take — a budget nobody granted, or the
   operator's config" (ADR-0077 §5, ADR-0082 §2). Naive `min` re-runs excluded work and, for a
   `DEGRADED` row, demotes the phases above it on a floor below a budget boundary. The consistent
   fix is to clamp *within* the hard-stop-bounded interval — floor may go no lower than one above
   the nearest hard stop below the computed floor — which is an extra rule to write, to state in
   the ADR, and to test. It is **not** a rule `phase_floor` currently exposes: the walk `break`s and
   discards where it stopped, so subtask 7 would have to either re-derive that boundary or extend
   `phase_floor`'s return.
2. **`--from-phase 4` is almost always a silent no-op.** The computed floor is ≤ 4 by definition,
   so `min` returns the computed floor and the flag does nothing — the "operator believes they
   scoped the resume, and nothing tells them otherwise" defect `_refuse_unbuilt_resume_flags`'s own
   docstring names as its reason for existing. B is only honest if `_resume_lines` prints which
   bound won, per repo.
3. It is the option whose name least matches its behaviour: `--from-phase 2` on a repo whose
   computed floor is 1 resumes from **1**, not 2.

*Cost if ruled and wrong:* extra work — phases re-run that did not need to — bounded by one run's
compute, plus the reporting defect in B.2 if the clamp bound is not surfaced. Recoverable.

---

#### Option C — **filter**: the floor is untouched; `--from-phase` restricts *what is acted on*

*What it buys.* Never overrides evidence, never bypasses a hard stop, never edits the SPEC
sentence. Composes naturally with `--repo` (the other scoping flag subtask 9 un-refuses), which is
a genuine advantage: both then mean the same kind of thing — "act on this subset" — rather than two
unrelated semantics on one command.

*What it costs.*
1. **It has two plausible readings and the ADR must pick one, which is itself a cost.**
   (i) *Repo filter*: act only on repos whose computed floor is ≥ `from_phase` (skip repos that
   would be demoted lower). (ii) *Span cap*: demote no phase below `from_phase`, i.e.
   `span := (p for p in Phase if p >= max(floor, from_phase))` — which is arithmetically Option A's
   upward half without its downward half, and inherits A.1's cost in a narrower form.
2. **Under reading (i) it defers a Constraint 7 violation rather than preventing one.** A repo whose
   evidence demands floor 1 is simply left un-reconciled while the run continues. At subtask 10
   that repo re-enters at a phase its evidence does not support — unless step 8 is additionally
   made to refuse to continue for repos step 5 skipped, which is a coupling into subtask 10 that
   the other two options do not create.
3. It is the least like what an operator reading `--from-phase 1..4` in `--help` expects, so it
   carries the largest documentation burden: the SPEC §10 flag list, `resume()`'s docstring, and
   the un-refusal message all have to say what it filters.

*Cost if ruled and wrong:* under reading (i), a partially-reconciled ledger that looks fully
reconciled — the drift class §11.5's preamble says a resume removes — surfacing only at subtask 10.
Under reading (ii), Option A.1's cost at reduced scope.

---

### 4.4 What subtask 7 must edit that design §5 row 7 does not list

Both step-5 refusal messages in `cli.py` restate the floor rule **in full**, and both are asserted
verbatim by tests:

- Normalised sweep (predicate: case-insensitive `phase ABOVE the HIGHEST phase below the settled
  frontier`; normaliser: strip `"`, `'`, `\`, then collapse every maximal whitespace run to one
  space over the **whole file**, offsets mapped back to 1-based lines) — **class result: 2 sites,
  both in `src/fleet/cli.py`, at lines 10086 and 10474 at `6bf198f`; 0 sites elsewhere in the
  tracked tree.** A line-oriented grep for that phrase returns **0** in `cli.py`, because both
  copies are split across adjacent Python string literals — the failure mode CLAUDE.md warns about,
  reproduced here on the first attempt and corrected by the normaliser.
- `tests/test_floor_rule_statements.py` **excludes** these two by register: its docstring says the
  `cli.py` restatements "are outside the census by register and are bound in `tests/test_cli.py`
  instead: `_RESIDUAL` item 6", and records that `cli.py` contributes **0** sentences to Layer D.
  Its `_EXPECTED_SITES` is `{"docs/SPEC.md": 2, "docs/DECISIONS.md": 1,
  "src/fleet/orchestrator/reentry.py": 1}`.
- The live bindings are in `tests/test_cli.py`, at
  `test_resume_refuses_the_flags_whose_behaviour_does_not_exist` (`:1245`, asserting at `:1269`)
  and `test_resume_step_5_refusal_does_not_share_an_exit_code_with_a_crash` (`:2124`, asserting at
  `:2149`). Both assert three things in the rendered output: `"step 5" in output`,
  `"precondition" not in output`, `"HIGHEST phase below the settled frontier" in output`, and
  `"DEGRADED/SKIPPED hard stop" in output`.

**So: the moment subtask 7 lands, `ResumeIncompleteError`'s message becomes false where it says
step 5 "has no implementation", and two tests assert that message's text.** That is a required edit
in subtask 7's diff, not a follow-up.

### 4.5 An open conflict the controller should rule on before subtask 7 is dispatched

`docs/DECISIONS.md` ADR-0076, final bullet, verbatim: *"**This ADR is temporary by construction.**
When §11.5 step 5 lands, `ResumeIncompleteError` should be deleted, not repurposed. If it is still
here after step 5 exists, that is a defect."*

Design §5 row 10 puts that deletion at **subtask 10** (*"`CommandUnavailableError` is deleted from
`resume()`"* — itself mis-named, per P1). These conflict as literally written: subtask 7 lands step
5, but steps 6 and 8 remain absent, so the verb still cannot continue and exit 2 is still the
correct outcome.

The narrow reading that satisfies both without deleting a live refusal: subtask 7 **rewrites** the
message so it no longer asserts step 5 is unimplemented, and keeps exit 2 for steps 6 and 8 —
which is arguably "repurposed", the thing ADR-0076 forbids. This is a ruling, not a research
finding; I am flagging it rather than resolving it. Whichever way it goes, it changes text the two
tests in §4.4 assert on, so it must be decided **at dispatch**, not discovered mid-task.

---

## 5. Preconditions subtask 7 inherits from siblings (verify at dispatch, do not assume)

1. **Subtask 5 (`evidence_holds`) does not exist at `6bf198f`** — no definition in `src/`. Subtask
   7 cannot be dispatched before it lands, and its **signature is not yet fixed**; `phase_floor`
   only requires `evidence: Mapping[Phase, bool]` with an absent phase treated as not holding.
2. **ADR-0087 §4 (`fe743e6`'s adjudication) narrows step 4.** `docs/SPEC.md` §11.5 step 4 carries a
   second selector — reconcile also "any `phases` row whose `post_commit_sha` does not resolve on
   its branch" — which subtask 4 did **not** implement. Consequence for subtask 7: design §4's
   claim that step 4 is a satisfied prerequisite of step 5 holds **only for the reconciled class**.
   A `phases` row with a dangling `post_commit_sha` and no `RUNNING` task reaches `evidence_holds`
   unreconciled. The controller has ruled (to W4) that `evidence_holds` must answer `False` there
   rather than raise. Subtask 7 should confirm that behaviour is present in the landed
   `evidence_holds` before wiring it, since `phase_floor`'s conservative default — an absent phase
   is "does not hold" — means an *exception* propagating out of `evidence_holds` aborts the whole
   resume, while a `False` merely lowers one repo's floor.
3. **`PhaseDemotion.reason` is one string per call, stamped on every phase in the span** (V1's M1).
   Subtask 7 chooses that string. `enums.py`'s field comment reads it as *"why the evidence for
   `phase` no longer holds"*, which is accurate only for the floor phase. Word it as a
   repo-level cause ("re-entry floor recomputed to phase N: evidence at phase N did not hold"), or
   the finding rows assert something false about phases N+1…4.
4. **`_note_finding` fingerprints on `(run_id, repo_id, kind)` and UPSERTs.** `demote_to_floor`
   already handles this correctly with `_demotion_fingerprint(run_id, repo_id, record.phase)`, so
   subtask 7 inherits per-phase findings for free — but must **not** add a second, repo-level
   `PhaseDemoted` finding of its own, or it will collapse them.

---

## 6. What this brief cannot catch

- I read `HEAD` only. Any lane that commits between this ref and subtask 7's dispatch can move
  `cli.py` again; every claim here is cited by symbol, but the two line numbers in §4.4 (`10086`,
  `10474`) are measurements at `6bf198f` and will rot. Re-run the §4.4 normalised sweep at dispatch
  rather than trusting those two numbers.
- I did not run the test suite (four lanes live; `pytest_sessionfinish` reaps sibling
  `BAZEL_ROOT` children). Every claim about a test is read from its source text at `HEAD`, not from
  an observed pass or fail. In particular, §4.4's claim that subtask 7 will break
  `tests/test_cli.py:1269` and `:2149` is derived from the assertion text plus the message text,
  and is a prediction, not a measurement.
- §2.2's interleaving is derived from source, not reproduced. I did not build a two-process
  concurrency harness; CLAUDE.md Guardrail 6 is explicit that a reasoned hazard is weaker than a
  measured one. What **is** measured is every mechanical fact it rests on: the process-scope of the
  write slot, `complete_phase`'s lease NULLing, `_LIVE_SANDBOX_PREDICATE`'s `RUNNING` requirement,
  and the placement of the `_DEMOTE_SELECT_SQL` read inside the unit.

---

## 7. ADDENDUM — re-anchored at `7275adb` after two siblings landed mid-report

The tree moved while §§1–6 were being written. Re-measured; **every finding above survives**, and
one new hazard for subtask 7 appeared that did not exist at `6bf198f`.

**New commits:** `19d7fb0` (*"docs+tests: mark the step-5 plan's retracted quotes, bind item 1 of
reentry's docstring, land D75"*) and `7275adb` (*"docs/PROGRESS: annotate §39's five re-measured
open items and open the §40 round-D checkpoint"*). Files changed:
`docs/INTEGRATION_HONESTY.md`, `docs/PROGRESS.md`,
`docs/superpowers/plans/design-resume-step5{,-review-final}.md`,
`tests/test_floor_rule_statements.py` (+241).

**Working tree at re-anchor time carried lane W4's in-flight edits** — `M
src/fleet/orchestrator/reentry.py`, `?? tests/test_reentry_evidence.py` — which is exactly the
condition the dispatch brief warned about. Nothing above is contaminated by them: every read in
§§1–6 went through `git show <ref>:<path>` into a lane-private export, and §§1–6's ref (`6bf198f`)
predates those edits appearing.

### 7.1 Citation survival, verified by `git diff --quiet <ref> <ref> -- <path>`

`src/fleet/cli.py`, `src/fleet/state/repository.py`, `src/fleet/orchestrator/reentry.py`
(committed) and `tests/test_cli.py` are **byte-identical** between `6bf198f` and `7275adb`. So §1's
statement order, §2's mechanics, §3's dry-run models and §4.1–4.2 all stand unchanged. §4.4's
normalised sweep re-measured at `7275adb`: **still 2 sites, still `src/fleet/cli.py` [10086,
10474], still 0 elsewhere.** `_EXPECTED_SITES` is unchanged at `{"docs/SPEC.md": 2,
"docs/DECISIONS.md": 1, "src/fleet/orchestrator/reentry.py": 1}`, and the "`cli.py` contributes 0 /
bound in `tests/test_cli.py` `_RESIDUAL` item 6" claim is still in the file.

### 7.2 W2's annotations do **not** overlap §0's corrections

`19d7fb0` annotated `design-resume-step5.md` in four places — the two retracted SPEC quotes in §3,
ambiguities 1 and 5, and §6's contradiction — plus one correction to §5 **row 1**'s test-file names
(`tests/test_enums.py` → `test_state_models.py` + `test_schema_sql.py`). It used dated
`> **SUPERSEDED / SETTLED (2026-08-21), lane W2 — …**` markers beside the stale text rather than
rewriting it, per Guardrail 7.

**§4 "Order inside `_resume_impl`" and §5 row 7 were not touched.** They still carry the stale
seven-item order, the `CommandUnavailableError` name and the "immediately after
`_reset_stale_running`" adjacency claim. So §0's P1–P4 remain live, unreported by any other lane,
and the right remedy is W2's own convention: a dated marker beside §4's numbered list pointing at
§1.1 of this report, **not** a rewrite of the list.

### 7.3 NEW — the `_EXPECTED_STOP_CONDITION_SENTENCES` trap subtask 7 will walk into

`19d7fb0` landed a new layer in `tests/test_floor_rule_statements.py` with
`_EXPECTED_STOP_CONDITION_SENTENCES = 2`, asserted **before** the semantic check so a re-wording
cannot empty the layer vacuously. `_GOVERNED` is `(_SPEC, _DECISIONS, _ENUMS, _REENTRY, _CLI)` —
**`src/fleet/cli.py` is in it.**

The file's own residual note (item 6) records the present state and the future case verbatim:
*"`cli.py` is in `_GOVERNED`, so Layer D sweeps both — but swept is not considered: measured,
`cli.py` contributes **0** sentences that `_STOP_CONDITION` matches … The enrolment buys the future
case (a re-statement in the stop-condition shape would be considered)."*

**§4.4 of this report establishes that subtask 7 *must* rewrite the `ResumeIncompleteError`
message** (it asserts step 5 "has no implementation", which subtask 7 falsifies). If that rewrite
lands in the stop-condition shape — matching `_WALK_SUBJECT`
(`backward walk|the walk|phase_floor|searches downward|…`, `re.IGNORECASE`) together with
`_STOP_CONDITION` — then `cli.py` begins contributing considered sentences and the count assertion
fires at 3 against an expected 2. That is the *same* failure mode the controller has just
adjudicated for lane W4's `reentry.py` append, arriving by a second route.

Guidance for subtask 7, in the controller's own terms: this is a **census constant bound to landed
text**, so it may not be pre-adjusted, and it must never be tuned until green. Either (a) word the
replacement message so it states the floor without stating *where the walk stops* — note the two
`tests/test_cli.py` assertions require only `"HIGHEST phase below the settled frontier"` and
`"DEGRADED/SKIPPED hard stop"` in the rendered output, neither of which requires
`_WALK_SUBJECT`'s vocabulary — or (b) land the message first and flip the constant in a separate,
reasoned follow-up commit naming the SHA, as W4 was instructed to. **Option (a) is preferable and
probably free**, because the existing message already satisfies both `test_cli.py` assertions
without being a considered stop-condition sentence; keeping that register is a constraint on the
rewrite, not new work. Either way, subtask 7's brief must say so at dispatch: an implementer who
meets §4.4's requirement without knowing about this layer will land a red suite and reach for the
constant.

This hazard is a **prediction from source text, not an observed failure** — I did not run pytest
(four lanes live; `pytest_sessionfinish` reaps sibling `BAZEL_ROOT` children).
