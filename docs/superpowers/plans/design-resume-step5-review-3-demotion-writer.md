> Promoted from `.superpowers/sdd/design-resume-step5/review-3.md` (round C scratch, lane CR-3), snapshot taken while `main` was at `1050e0a`. Examines subtask 6, the demotion writer, commits `16879fe`, `5488157`, `38107e9`, `9e5c093`. Verdict: FINDINGS — 0 Critical, 2 Important, 4 Minor.

# CR3 — review of subtask 6, the demotion writer (`16879fe`, `5488157`, `38107e9`, `9e5c093`)

**Read-only review. Nothing was edited or committed outside this file.**

## Repo state

`git status --short` at review start (concurrent lanes active):

```
 M docs/SPEC.md
 M src/fleet/cli.py
 M src/fleet/state/schema.sql
 M tests/test_llm_cache.py
?? .superpowers/
```
`HEAD` = `4ad7c1f`. During the review two sibling lanes landed (`23a4396`, `c7f72c6`) and
`git status --short` narrowed to `M src/fleet/cli.py` / `?? .superpowers/`. Every committed-state
claim below was read with `git show main:<path>` or, once the file was clean again, from the tree;
**no uncommitted sibling edit is reported as a defect**.

Mutation work ran in a **detached worktree** at `4ad7c1f`
(`…/scratchpad/wt-review3`, removed afterwards), with `PYTHONPATH=<worktree>/src` shadowing the
editable install (verified: `fleet.state.repository.__file__` resolved inside the worktree). The
primary checkout was never mutated and no `FLEET_*` var was exported. Baseline in the worktree:
`38 passed`.

## Verdict

**FINDINGS** — 0 Critical, 2 Important, 4 Minor.

The acceptance criteria are met and the Rule 12 evidence I spot-checked reproduces exactly as the
implementer reported it. Both Important findings are gaps in what the committed tests/doc listings
bind, not defects in `demote_to_floor`'s behaviour.

---

## 1. Acceptance criteria (design doc §5 row 6) — verified against the code

| Criterion | Verdict | Evidence |
|---|---|---|
| frontier 4, floor 2 ⇒ phases 2-4 `PENDING` | PASS | `src/fleet/state/repository.py:1418-1434`; span = `tuple(p for p in Phase if p >= floor)` and `Phase` tops out at `VERIFY=4` (`src/fleet/models/enums.py:8-12`), so the span cannot overrun |
| every `attempts` retained | PASS | `_DEMOTE_PHASE_SQL` (`repository.py:822-825`) names only `status` and `updated_at`. Independently re-verified the load-bearing premise: the only writer of `phases.attempts` in `src/` is `complete_phase` (`repository.py:1311`). I swept every `UPDATE phases` in `src/` (`cli.py:1993,2022,3674,3878,3933,6081,8812,9756,9851`, `runner.py:936,1010`, `scheduler.py:280`) — none touches the column. `demote_to_floor` does not call `complete_phase` directly or transitively |
| assertions on named columns, not `SELECT *` | PASS | `_phase_state` (`tests/test_repository.py:1127-1138`) selects `phase, status, attempts`; the docstring states why (`updated_at` moves) |
| `checkpoints` rows for the demoted phases deleted | PASS | `repository.py:1436-1442` via `checkpoints.delete_in_unit` |
| one `StateWriter` unit | PASS, and genuinely bound — see §2 |
| an `REQUIRES_HUMAN_INTERVENTION` row leaves the repo untouched | PASS | guard at `repository.py:1414-1415`, read **inside** the transaction; test at `tests/test_repository.py:1328-1364` asserts statuses, checkpoints and findings all unchanged |

`checkpoints.delete_in_unit` (`src/fleet/state/checkpoints.py:138-164`) takes a connection, not a
`StateWriter`, and the docstring gives the reason (offering a `StateWriter` overload would offer
the split atomicity exists to forbid). Exported in `__all__`. Correct.

## 2. Rule 12 spot-checks — reproduced, in the worktree

**M3 (the one the controller required).** Neutralised the findings loop
(`for record in demotions:` → `for record in []:`), `git diff --stat` = `1 insertion(+), 1
deletion(-)` (non-no-op, confirmed before running):

```
FAILED tests/test_repository.py::test_a_demotion_writes_one_phasedemoted_finding_per_phase_and_never_a_silent_one
FAILED tests/test_repository.py::test_a_degraded_phase_in_the_span_keeps_both_its_status_and_its_checkpoint
2 failed, 36 passed
```

`test_demoting_to_a_floor_pends_the_span_keeps_every_attempt_and_drops_its_checkpoints` is
**GREEN** while every demotion goes unrecorded. **The obligation the controller added is
discharged**: the `PhaseDemoted` finding is bound by an assertion that reads the `findings` row
itself (`tests/test_repository.py:1250-1262`), independently of the status write — this is the
old-passes/new-fails discrimination, not a "the new test fails" demonstration. (See Minor 1 for a
small inaccuracy in how the report tabulates this.)

**M8 (`await conn.commit()` mid-unit, before the checkpoint delete).** `git diff --stat` =
`1 insertion(+)`:

```
FAILED tests/test_repository.py::test_the_demotion_is_one_write_unit_so_a_failure_leaves_no_half_demoted_repo
1 failed, 37 passed
```

The failure lands at `tests/test_repository.py:1318` — the **rollback** assertion — meaning
`assert submits == 1` at line 1300 still passed. So the name "is one write unit" is bound by more
than a submit count, exactly as the report claims (rubric 3 satisfied for that name).

Names asserting an absence, checked individually:
- `…_keeps_every_attempt_…` — the body enumerates the whole domain the name quantifies over (all
  four phases plus an untouched control repo). The implementer's admission that only an
  adversarial mutation (bump `attempts` for `repo_id`s no test uses) falsifies the name is
  accurate, and under this project's stop rule that is a boundary, not a defect.
- `…never_a_silent_one` — bound, by M3 above.
- `…leaves_the_frontiers_checkpoint_alone` / `…also_drops_the_frontiers_own_checkpoint` — the two
  share `_frontier_at_verify` and differ only in the floor, so each is discriminating for the
  other's direction. Good construction.
- `test_the_checkpoint_assertion_is_silent_on_a_clean_repo_and_fires_on_an_injected_row` is a real
  three-check instrument validation (silent on swept, fires on injected fault; the known-bad check
  is M6, recorded as a mutation). This is the Guardrail-6 discipline applied correctly.

## 3. Findings

### Important 1 — the §11.7 idempotency claim on the findings UPSERT is unbound; the `ON CONFLICT` clause has zero test coverage

`tests/test_repository.py:1224-1268`. The test docstring and the trailing comment claim
"§11.7: re-running the same demotion re-raises the same findings without duplicating them" and
assert `len(await _demotion_findings(read_conn, REPO)) == 3` after a second `demote_to_floor`.
**The second call demotes nothing** — phases 2-4 are already `PENDING`, so
`rows.get(phase) is not RepoStatus.SUCCEEDED` skips every one of them (`repository.py:1421-1422`),
`demotions` is empty, and the `findings` INSERT never executes. The assertion is satisfied by the
absence of any write, not by the UPSERT.

Measured, not argued (worktree, `git diff --stat` = `1 insertion(+), 3 deletions(-)`): deleting the
entire `ON CONFLICT (run_id, IFNULL(repo_id, ''), kind, fingerprint) DO UPDATE …` tail from
`_DEMOTE_FINDING_SQL` (`repository.py:833-839`) leaves **all 38 tests green**. Old assertions pass
under a mutation that removes the mechanism they name — the Rule 12 discriminating case is absent.

That the case is real, and not a hypothetical: I added a probe that demotes, re-acquires leases and
drives phases 2-4 back to `SUCCEEDED`, then demotes again. Under the mutation:

```
sqlite3.IntegrityError: UNIQUE constraint failed: index 'ux_findings_ident'
```

and unmutated it passes (3 rows, `reason == "r2"`). The index is real
(`src/fleet/state/schema.sql:291-292`).

**Failure scenario.** A future edit that "simplifies" the INSERT — or a copy of it into subtask 7's
wiring — drops the conflict clause; the second demotion of any phase in the same run raises
`IntegrityError` inside the write unit, the whole demotion rolls back, and `fleet resume` dies
partway through step 5 for a repo that has simply been demoted twice. Nothing in the suite catches
it. The fix is one test, not a code change: the code is correct.

### Important 2 — Guardrail 7: `PhaseDemoted` is now emitted by a live INSERT and is missing from both `findings.kind` listings

`src/fleet/state/schema.sql:253-283` carries a curated `Shipped:` enumeration of `findings.kind`
values, with an explicit CAVEAT distinguishing kinds that are **DECLARED** from kinds that are
**emitted**, and it annotates which two "each have a live INSERT behind them". `docs/SPEC.md:4050-4058`
carries a copy of the same listing. `16879fe` added the second class of live INSERT
(`_DEMOTE_FINDING_SQL`, kind `PhaseDemoted`) and updated neither listing.

This is the class the guardrail names: the listing is not documentation, it is what the next author
reconciles against — and the new entry is wrong in a direction the CAVEAT does not even contemplate
(emitted but not declared).

**Failure scenario.** A maintainer auditing "which finding kinds does the harness actually write"
— the exact question the CAVEAT was written to answer — reads the list and concludes `PhaseDemoted`
is not a shipped kind; a reporting/`findings` dashboard or a `kind` validator derived from the list
silently omits every demotion. Report only; `docs/SPEC.md` belongs to a live lane.

### Minor 1 — the mutation table's M3 row overstates the isolation

`.superpowers/sdd/design-resume-step5/task-6-report.md` §5 says M3 gives "**FAIL** findings test
only". Across the whole file **two** tests fail — the DEGRADED test
(`tests/test_repository.py:1392-1394`) also asserts on the finding rows. The §5.1 write-up, which
scopes the run with `-k "demoting_to_a_floor or phasedemoted_finding"`, is accurate; the table row
is not. The load-bearing claim (status/attempts/checkpoint test green under M3) reproduces exactly.

### Minor 2 — a demoted row keeps the discarded attempt's Git columns, and `migration_state.json` reports them

`_DEMOTE_PHASE_SQL` rewrites `status` and `updated_at` only, so `post_commit_sha`,
`pre_commit_sha`, `last_error` and `failure_class` survive the demotion.
`src/fleet/state/projection.py:90,159` selects and emits `post_commit_sha` into the projection that
§11.5 step 7 regenerates.

**Failure scenario.** After a resume, `migration_state.json` shows a `PENDING` VERIFY phase
carrying the previous run's `post_commit_sha` and the previous run's `last_error`; an operator (or
a downstream reader of the projection) reads it as work that landed. No `src/` code branches on
these columns for a `PENDING` row today, which is why this is Minor rather than Important — but the
retention is silent, and neither the docstring nor a test says it is intended.

### Minor 3 — rubric 5: the DEGRADED stale-anchor residual is **not accidentally reachable today**; documented boundary is the right call

Assessed rather than accepted. `phase_floor` (`src/fleet/orchestrator/reentry.py:79-87`) breaks on
a `DEGRADED` row **without moving the floor onto it**, and treats `DEGRADED` as settled when
locating the frontier (`reentry.py:38-43,72-75`). Consequently, for any floor this function
produces, a `DEGRADED` row below the frontier can never be inside `floor..VERIFY`, and a `DEGRADED`
row cannot be the frontier. The only states that put a `DEGRADED` row inside the span need an
unsettled (`RUNNING`/`BLOCKED`) row *beneath* a `DEGRADED` one, which the phase ordering does not
produce — or a floor from somewhere other than `phase_floor`.

So: **adversarial-only today → correctly documented and not patched**, per this project's stop
rule. Two riders worth handing to whoever owns subtasks 7 and 9:

1. the carve-out at `repository.py:1441` and its test are *defensive*, not exercising a reachable
   production state, and the method docstring's hazard paragraph does not say so;
2. `fleet resume --from-phase 1..4` (`docs/SPEC.md:6506`, subtask 9) is an **operator-supplied
   floor** that bypasses `phase_floor` entirely. That is the path that would make the residual
   accidentally reachable, and it is the point at which the hazard should be re-adjudicated rather
   than re-documented.

### Minor 4 — the sweep excludes only `DEGRADED`, so a `SKIPPED` phase in the span loses its checkpoint while keeping its status

`repository.py:1441`: `phases=[p for p in span if rows.get(p) is not RepoStatus.DEGRADED]`.
`SKIPPED` is settled-for-demotion (`reentry.py:38-40`) and is deliberately not a `RESUME_DEMOTE`
key (ADR-0077 §5, "resume does not re-decide the operator's config"), yet its checkpoint is swept
with the rest of the span. **Failure scenario:** a repo with a `SKIPPED` phase above the floor and
a checkpoint on it loses that payload on any resume that demotes something, for a phase resume was
told not to re-decide. Narrow (a `SKIPPED` phase with a checkpoint is unusual) and arguably
harmless, but it is an asymmetry the docstring's DEGRADED paragraph does not cover.

## 4. Rubric items that came back clean

- **Rubric 6 (Guardrail 6, the narrower-overclaim follow-on).** Checked both fixes; neither
  introduced one.
  - `38107e9` re-points two docstrings to "`docs/SPEC.md` Constraint 7 read the other way until
    `f02d124`". Verified at the ref, not assumed: `git show f02d124 -- docs/SPEC.md` replaces
    exactly the `transition(..., resume=True)` … "emits a `PhaseDemoted` finding" sentence, and the
    committed `docs/SPEC.md:186-189` now names `models.enums.demote()`. The past tense is true and
    the anchor is real.
  - `9e5c093`'s `if demotions:` guard does **not** exclude the case it exists to cover. The
    span-wide sweep's stated purpose is the frontier's partial payload after a demotion beneath it;
    `demotions` is non-empty in exactly that case, so the frontier checkpoint is swept
    (`tests/test_repository.py:1447-1480`), and the no-op path leaves it
    (`tests/test_repository.py:1482-1505`). I walked the states the guard suppresses (floor below
    the frontier with nothing `SUCCEEDED` in the span): nothing was rewritten there, so nothing
    above was invalidated, and the suppression is correct.
- **Rubric 7 (SPEC listings of the changed functions).** `demote_to_floor` and `delete_in_unit`
  appear in no `docs/SPEC.md` code listing, so there is no pre-change text to reconcile. The one
  stale listing is the `findings.kind` enumeration — Important 2. The §11.5 step-5 / demoted-row
  checkpoint-scope disagreement is the known ADR-0082 item and is **not** re-raised here.
- **Rubric 8 (Protocol widening).** `StateRepository` (`repository.py:382`) gained
  `demote_to_floor` at `repository.py:453-461`. `SqliteStateRepository` is the only implementer in
  the tree: no other class in `src/` or `tests/` defines `complete_phase`/`acquire_phase_lease`,
  the two other test call sites pass a real `SqliteStateRepository`
  (`tests/test_llm_findings.py:194`, `tests/test_runner.py:565`), and the remaining
  `repository=cast(Any, …)` stand-ins are outside structural checking by construction. The one
  `isinstance(repo, StateRepository)` check (`tests/test_repository.py:134`) still passes.
- **Known/adjudicated, confirmed not re-raised:** `demote()` having had no caller before this
  commit; the plan-vs-SPEC checkpoint-scope disagreement.
