> Round-C task report (originally `.superpowers/sdd/design-resume-step5/task-6-report.md`; renamed on promotion to avoid colliding with the pre-existing, unrelated `docs/superpowers/plans/task-6-report.md` from an earlier round), promoted unchanged from `.superpowers/` scratch by lane DOCSTALE because it is cited by committed `docs/DECISIONS.md` (ADR-0082, §3/§5/§6 item 1) and would otherwise dangle if the scratch directory were deleted. DOCSTALE does not own DECISIONS.md and has not repointed its citation — the path there still reads `.superpowers/sdd/design-resume-step5/task-6-report.md`.

# Task 6 report — the demotion writer (`fleet resume` §11.5 step 5)

**Status: DONE_WITH_CONCERNS** (three design choices made under open ambiguity, one of them a
divergence from the plan document; all three are stated below with reasoning and are pinned by
tests. Nothing is blocked.)

## Commits (all on `main`, explicit paths only)

| SHA | What |
|---|---|
| `16879fe` | `demote_to_floor` + `checkpoints.delete_in_unit` + 6 tests |
| `5488157` | split the status and attempts assertions so each mutation lands on its own line |
| `38107e9` | re-point the SPEC note after a sibling lane corrected Constraint 7 at `f02d124` |
| `9e5c093` | the conditional span-wide checkpoint sweep + 2 tests (see §3) |

Files touched: `src/fleet/state/repository.py`, `src/fleet/state/checkpoints.py`,
`tests/test_repository.py`. Nothing else. `src/fleet/cli.py`, `docs/DECISIONS.md`,
`docs/SPEC.md`, `docs/PROGRESS.md`, `src/fleet/orchestrator/reentry.py` and
`tests/test_state_models.py` were modified by sibling lanes during this work and were left alone.

## 1. What landed

`SqliteStateRepository.demote_to_floor(run_id, repo_id, *, floor, reason, now)` →
`tuple[PhaseDemotion, ...]`, added to the `StateRepository` Protocol. One `StateWriter` unit:

1. `SELECT phase, status FROM phases` for the repo, **inside** the transaction.
2. Any `REQUIRES_HUMAN_INTERVENTION` row → return `()`, write nothing.
3. For each phase in `floor..VERIFY` whose status is `SUCCEEDED`: call **`demote()`** and
   `UPDATE phases SET status = ?, updated_at = ? … AND status = 'SUCCEEDED'`. `attempts` is not
   in the SET list.
4. If anything was demoted: delete the `checkpoints` rows for the span, excluding `DEGRADED`.
5. One `findings` row per demoted phase, kind `PhaseDemoted`, fingerprinted **per phase**.

`checkpoints.delete_in_unit(conn, *, run_id, repo_id, phases)` is the new export. It takes a
**connection**, not a `StateWriter`, deliberately — a `StateWriter` overload would offer exactly
the split that the atomicity requirement exists to forbid.

**This is the first real caller of `demote()`** (audit item 8's prediction confirmed). Nothing
blocked it. `transition(..., resume=True)` is not called anywhere in this change.

**The line that discharges the `PhaseDemoted` finding obligation:** the
`await conn.execute(_DEMOTE_FINDING_SQL, …)` at `src/fleet/state/repository.py:1445`, fed by
`record.payload()` from the `PhaseDemotion` that `demote()` returned at line 1426 — in the same
unit as the status `UPDATE` at lines 1429-1432.

**Per-phase fingerprint, not per-repo** (`_demotion_fingerprint`, `repository.py:840`). This is
the hazard ADR-0077 §4.1 handed forward: `cli._note_finding` fingerprints on
`(run_id, repo_id, kind)` and UPSERTs, so three per-phase calls through it collapse into one row
and two demotions disappear. Pinned by mutation M4.

## 2. Choices made where the design left the question open

**(a) `DEGRADED` rows keep their status *and* their checkpoint.** ADR-0077 §5 says step 5 neither
demotes a `DEGRADED` phase nor searches past it, because it leaves the machine only through a
budgeted revalidation round (§3.5.1). Deleting the payload that round resumes from spends the same
budget one level down, so the deletion skips it too. **The residual hazard, stated not hidden:** a
`DEGRADED` phase above the floor keeps a checkpoint built on output the demotion regenerates, so
the round that revalidates it resumes from a stale anchor. ADR-0077 §5 forbids the alternative;
this does not resolve the tension, it records it. The docstring says so.

**(b) No lease fence.** Resume holds no lease and step 3 has already reclaimed every stale one and
bumped its fence, so a fence nobody granted would be a fence in name only. Same reasoning the
research lane recommended for subtask 4.

**(c) The RHI refusal is re-read inside the transaction** rather than trusted from `phase_floor`.
`phase_floor` does return `None` for such a repo, but it reads the `mode=ro` handle outside this
transaction, and §12 item 46 (ii) names `fleet resume` among the sweeps that must be unable to move
a repo out of RHI. A refusal that lives only in the caller is one the next caller forgets.

## 3. A conflict between the plan and the SPEC — surfaced, not averaged (Rule 7)

`docs/superpowers/plans/design-resume-step5.md` §3 drops the `checkpoints` row for **every** phase
in `floor..4` unconditionally. `docs/SPEC.md` §11.5 step 5 (and Constraint 7 as corrected at
`f02d124`) ties the drop to the **demoted row**. Each reading alone is unsafe, in opposite
directions:

- *demoted-rows-only* leaves the frontier's partial payload in place after the phases beneath it
  were rewritten. `checkpoints.load()` reports it `usable`, so VERIFY resumes against a BUILD
  output the same call just discarded — a green verdict for a build nobody performed.
- *unconditional* has no room for the ordinary case: `phase_floor` returns the frontier itself
  whenever the evidence below it holds, which is what an interrupted-but-healthy resume looks
  like. Sweeping there deletes the in-progress checkpoint §8 exists to preserve, on every
  `fleet resume`, with nothing invalidated to justify it.

**Chosen: span-wide, conditional on at least one phase actually having been demoted.** A demotion
invalidates everything above it; a no-op stays a no-op. Two tests share one realistic fixture
(phases 1-3 `SUCCEEDED`, frontier `PENDING` at 4, checkpoints at 1 and 4) and differ only in the
floor. Both directions are pinned by mutations (M10, M11).

**This may deserve an ADR — I did not write one and took no number, per instructions.** If the
orchestrator wants the reconciliation recorded, the material is §3 here verbatim.

## 4. Test run (only this file, never the full suite, never concurrent sessions)

```
$ .venv/bin/python -m pytest tests/test_repository.py -q
......................................                                   [100%]
---------------------------------- bazel disk ----------------------------------
peak 1.61 GiB (ceiling 6 GiB) · residual output bases 0 bytes · repository cache kept 1645 MiB
-------------------------------- test coverage ---------------------------------
0 tests skipped this session — full collected coverage ran

38 passed in 3.39s
```

`ruff check` clean on all three files; `mypy` clean on both `src/` files.

Eight new tests: the floor/attempts/checkpoint acceptance case, the per-phase findings case
(plus §11.7 idempotency on a second call), the one-unit/rollback case, the RHI no-op, the
`DEGRADED` carve-out, the detector-validation case, and the two §3 fixture-shared cases.

## 5. Mutation evidence (Rule 12)

Every mutation was applied by script against the committed baseline, `git diff --stat` was checked
for a **non-zero** change before the run (the harness aborts and reports `NO-OP MUTATION — result
is worthless` otherwise, so no reported pass or fail below comes from a mutation that did not
land), then the file was restored from a byte copy and the diff re-checked empty.

| # | Mutation (all in `repository.py`) | diff | Result |
|---|---|---|---|
| M1 | split the unit: checkpoint deletion moved to its own `self._writer.submit` | `+10 −7` | **FAIL** `assert 2 == 1` at the submit count |
| M2 | add `attempts = attempts + 1` to `_DEMOTE_PHASE_SQL` | `+1 −1` | **FAIL** on the attempts-retention line (1209) |
| M3 | drop the `findings` INSERT loop, status write untouched | `+1 −1` | **FAIL** findings test only — status/attempts/checkpoint test **PASSES** |
| M4 | fingerprint without the phase (per-repo, as `_note_finding` does) | `+1 −1` | **FAIL** — 1 row instead of 3 |
| M5 | remove the `REQUIRES_HUMAN_INTERVENTION` guard | `−2` | **FAIL** RHI test |
| M6 | remove the checkpoint deletion entirely | `−6` | **FAIL** — the deletion detector fires |
| M7 | include `DEGRADED` in the deletion span | `+1 −1` | **FAIL** DEGRADED test |
| M8 | `await conn.commit()` mid-unit — still ONE submit | `+1` | **FAIL** on the rollback assertion (1316), count assertion still passes |
| M9 | bump `attempts` on span rows the demotion does not rewrite | `+5` | **FAIL** DEGRADED test (`("DEGRADED", 3)`) |
| M10 | make the checkpoint sweep unconditional | `−1` | **FAIL** the no-op test |
| M11 | narrow the sweep to `[record.phase for record in demotions]` | `+1 −1` | **FAIL** the frontier-checkpoint test |

### 5.1 The discriminating pair the controller asked for

M3 is the one that matters, and it was run as an explicit pair:

```
$ .venv/bin/python -m pytest tests/test_repository.py -k "demoting_to_a_floor or phasedemoted_finding"
FAILED …::test_a_demotion_writes_one_phasedemoted_finding_per_phase_and_never_a_silent_one
1 failed, 1 passed, 34 deselected
```

The status/attempts/checkpoint test is **green** while every demotion goes unrecorded. That is the
measured form of the reviewer's prediction: a test that checks only the resulting status cannot
see a missing finding, and the assertion on the `findings` row is the only thing that can.

### 5.2 Names that assert an absence, and whether they bind

- **"…is one write unit…"** — binds. M8 is the proof: it keeps `submits == 1` (so the count
  assertion still passes) while making the unit no longer one transaction, and the rollback
  assertion fails. The name is not carried by the count alone.
- **"…keeps every attempt…"** — binds for the rows a plausible bug would touch. M9 (bump the
  `attempts` of span rows the demotion does *not* rewrite) is caught by the `DEGRADED` test, and
  M2 by the retention line. **Honestly: I could not construct a non-adversarial mutation that
  makes the name false while the whole file passes**, because the body enumerates the entire
  domain the name quantifies over — all four phases of the demoted repo, plus an untouched control
  repo. An adversarial one exists (bump `attempts` only for `repo_id` values no test uses); per
  CLAUDE.md's stop rule that is a boundary, not a defect, and is not patched.
- **"…never a silent one"** — binds, by §5.1.

### 5.3 Instrument validation for the checkpoint-deletion assertion (Guardrail 6)

The detector is `_checkpoint_phases(read_conn, repo_id)` compared against an expected set. All
three checks, as required:

1. **Fires on the known-bad state.** M6 removes the deletion; the assertion fails in three tests.
2. **Silent on an already-swept fixture.** `test_the_checkpoint_assertion_is_silent_on_a_clean_repo_and_fires_on_an_injected_row` runs a demotion over a repo with only a below-floor checkpoint and asserts `== {1}` — green, no false positive. It is also green under M6, correctly: there is nothing above the floor for M6 to fail to delete.
3. **Fires on a synthetic fault injected into the clean fixture.** The same test then plants a checkpoint at phase 3 *after* the sweep and asserts the detector no longer reads `{1}` (and reads `{1, 3}`) — so it is reading live rows, not a cached or trivially-empty result.

## 6. Concerns / handed forward

1. **The §3 plan-vs-SPEC reconciliation may want an ADR.** Say the word and I will write it to a
   number you allocate. I did not take one.
2. **The `DEGRADED` stale-anchor hazard (§2a)** is documented in the method docstring and not
   resolved. It is a real, if narrow, correctness gap that ADR-0077 §5 forecloses the obvious fix
   for.
3. **`PhaseRow`/`get_phase` expose neither `post_commit_sha` nor `base_ref`** (research finding 3).
   This writer needed neither, so nothing was widened.
4. **`demote_to_floor` was added to the `StateRepository` Protocol.** Any other structural
   implementer would now need it; `SqliteStateRepository` is the only one in the tree today
   (`tests/test_repository.py:131` is the only `isinstance` check).
5. **Subtask 7 must not call `transition(..., resume=True)`.** The SPEC now says so at Constraint 7
   and at §11.5 step 5, and the method docstring says so; but ADR-0077 §4 is right that nothing
   enforces it.
