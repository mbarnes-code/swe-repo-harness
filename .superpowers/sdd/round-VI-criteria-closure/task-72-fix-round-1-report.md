# Task 72 fix round 1 report — I2 production-safety regression + doc staleness + cleanup

**Status: DONE**

**Branch:** `agent/roundvi-task72`. Started from `d69c029` (confirmed `git status` clean and
`git rev-parse HEAD == d69c029` before starting, per the brief). Fix-round commit: `a471b6e`.

## I2 — the production-safety regression (fixed)

**The defect, as the review traced it and I confirmed.** `_ordered_revert_shas` finds its anchor
by scanning every PR record for `draft.contract_id == contract_id`. No production code path ever
constructs a `PullRequestDraft` with `contract_id` set (D122) — `workers/prwriter.py:333` only
*reads* `payload.contract_id`; neither `PrwriterInput(` construction site in `cli.py` populates
it. So on a real fleet the anchor is always `None`, and `execute_hoist_rollback` raises
`RollbackAnchorError` for any contract whose blast set is non-empty. `_reconcile_hoist_rollbacks`
did not catch this, so the exception propagated out of `_build_impl` and aborted the entire
`fleet build`/`fleet resume` invocation — every other, unrelated repo in the same run included.
Before task 72, the identical real scenario recorded a `HoistBrokeOwner` finding and the build
finished normally; task 72's own wiring turned that into a whole-run crash. My own e2e tests never
caught this because they hand-seed `contract_id`, which no production code path does.

**Fix, exactly as ruled.**
1. Each contract's `unhoist_contract` → `execute_hoist_rollback` sequence now runs in its OWN
   `try`/`except` inside `_reconcile_hoist_rollbacks`'s per-contract loop (not one try/except
   around the whole loop) — a failure on contract A cannot prevent contract B's rollback from
   being attempted.
2. Catches `(GitError, WorktreeError)` — the union of this git-mechanics pipeline's own documented
   failure surface (`RollbackAnchorError`, `HoistRollbackConflictError`, and the underlying
   `GitCommandError`/`GitRefError`/`ForgeError`/`LockTimeoutError`/`WorktreeError` any real
   git/forge/worktree call inside it can raise). A genuinely unexpected exception outside that
   surface (a real programming bug, corrupted DB state) is still not caught and still aborts the
   whole pass, exactly as before — this is Rule 11 preserved, not relaxed.
3. **New finding kind `HoistRollbackFailed`** (`src/fleet/cli.py`, constant
   `HOIST_ROLLBACK_FAILED_FINDING_KIND`, writer `_write_hoist_rollback_failed_finding`) — checked
   first whether `HoistBrokeOwner`/`HoistRollbackDemotion`/`HoistRollbackRefused` fit (none do:
   the first two describe successful states, the third a settled REFUSED decision — none fits "an
   exception was raised mid-rollback"), so this is a real, disclosed addition, not a reused
   wrong-shaped kind. `repo_id` is `NULL`, mirroring `HoistRollbackRefused`'s own rationale (a
   rollback attempt failing is a property of the whole contract, not one repo). UPSERTs on
   `ux_findings_ident`, safe to re-run: the fingerprint/payload are pure functions of
   `contract_id`/`type(exc)`/`str(exc)`, none of which carries per-call noise.
4. `HoistRollbackReconciliationEntry` widened: `unhoist: UnhoistOutcome | None` (populated with
   the settled `APPLIED` outcome when the raise happens in `execute_hoist_rollback` — the
   realistic case today; `None` only if `unhoist_contract` itself raised before returning), new
   `error: str | None = None` field. D44 precedent held: the shape names which calls actually
   completed, never a sentinel buried inside a settled value.
5. Added `docs/SPEC.md`/`schema.sql` CAVEAT-listing entries for the new kind, following this
   project's existing pattern exactly (a `Final` constant + both listings + an origin-story
   paragraph matching `HoistRollbackRefused`'s own). Watched the apostrophe-parsing hazard
   `_QUOTED = re.compile(r"'([^']+)'")` in `tests/test_findings_kinds.py` (already documented in
   `docs/PROGRESS.md` as having bitten task-66's fix round and task-67's merge): kept every new
   sentence near a quoted kind name apostrophe-free. `tests/test_findings_kinds.py`: 4/4 pass.

## Rule-12 old-fails/new-passes discriminator

New test: `tests/test_hoist_rollback_wiring.py::
test_a_hoist_rollback_that_cannot_find_its_anchor_fails_loud_for_one_contract_only`. Real
`fleet scan`/`sequence`/`transform`/`build`, a real bazel failure seam, a real `HoistBrokeOwner`
finding — but, unlike every other test in this module, the owner's own `PullRequestDraft` is
seeded with NO `contract_id` (the real production shape per D122). Asserts: (a) `build()` (this
suite's `catch_exceptions=False` helper) does not raise; (b) `contracts.status` still reads
`FAILED`; (c) no revert commit lands; (d) exactly one `HoistRollbackFailed` finding, payload
`contract_id`/`error_type == "RollbackAnchorError"`; (e) an UNRELATED repo (`acme-lib-ts`, given no
edge to the contract at all, so never a blast-set member regardless of what
`execute_hoist_rollback` does) still reaches BUILD `SUCCEEDED`.

**Old-fails**, measured directly (no `git stash`, per this fix round's own global constraint —
backed up `src/fleet/cli.py` to a scratch file, overwrote it with `git show d69c029:src/fleet/
cli.py`, confirmed `git diff HEAD -- src/fleet/cli.py` empty and the file genuinely differing from
the backup):

```
E   fleet.vcs.commits.RollbackAnchorError: contract 'openapi:acme.shared': no MERGED PR draft
    resolvable for the contract's own hoist migration -- ...
src/fleet/cli.py:4510: RollbackAnchorError
1 failed, 8 deselected in 8.40s
```

Exactly the predicted symptom: the exception propagates uncaught through `_reconcile_hoist_
rollbacks` → `_build_impl` → `runner.invoke(..., catch_exceptions=False)`, failing the test with a
raw traceback instead of a `Result`.

**New-passes**, fix restored from the scratch backup (confirmed byte-identical via `diff -q`
before re-running): `1 passed, 8 deselected in 7.55s`.

## Full whole-file test results (no `-k`)

| File | Before (`d69c029`) | After (this commit) |
|---|---|---|
| `tests/test_hoist_rollback_wiring.py` | 8 passed | 9 passed (+1 new discriminator) |
| `tests/test_cli.py` | 194 passed | 194 passed |

`tests/test_findings_kinds.py` + `tests/test_integration_honesty_citations.py` (re-run per this
task's new findings kind): **74 passed** (4 + 70).

## mypy / ruff

- `mypy --strict src/fleet/`: **Success: no issues found in 129 source files.**
- `ruff check src/fleet/` and `tests/test_hoist_rollback_wiring.py`: **All checks passed.**
- `ruff format --check` on both: flags the same pre-existing reformat opportunities that already
  exist on `d69c029` (verified by running the identical command against a `d69c029` checkout of
  each file) — nothing introduced by this fix round.

## I1 — 5 stale "not yet wired" claims (corrected)

All 5 named sites corrected to state the wiring exists (task 72), each disclosing the exact
residual (I2's anchor gap — no production writer sets `contract_id` yet, D122) rather than
overclaiming full end-to-end production readiness:
1. `src/fleet/cli.py` — module banner above `unhoist_contract`.
2. `src/fleet/cli.py` — `unhoist_contract`'s own docstring.
3. `src/fleet/cli.py` — banner above `execute_hoist_rollback`.
4. `docs/SPEC.md:7665` (§3.1 row 28) — corrected in place with an inline dated marker (a table
   cell, so the correction is compact but still names what changed and why).
5. `docs/DECISIONS.md`, ADR-0122 Consequences — **appended** a dated correction paragraph rather
   than rewriting the original sentence, per this project's "annotate, never rewrite" convention:
   the original paragraph correctly recorded what was true when `task-65-brief.md` was written and
   is left in place as that historical record.

## I3 — ordering disclosure (added, no design change)

One-sentence comment added immediately before the `_phase_statuses` read in `_build_impl`: a
demoted repo's `phases` rows (BUILD included) can be rolled back to `PENDING` by `_reconcile_
hoist_rollbacks` before that read, so the build-criterion check reflects post-rollback status for
any repo this pass demoted. Re-reading the ordering myself, I agree with the review's judgment
that this is the correct order (reporting stale pre-rollback `SUCCEEDED` statuses would be worse)
— no design change.

## M1 — wave-loop exception gap (attempted, set aside, disclosed)

Attempted wrapping the wave loop (`_build_impl`, `for index in waves:`, ~300 lines) in a
`try`/`finally` so `_reconcile_hoist_rollbacks` still runs when the loop itself raises (e.g.
`WaveNotReadyError` escaping its own narrower catch, `RootFileDomainDriftError`). Set aside as a
real structural risk, per the brief's own permission to decline:

- The loop is a single large, carefully sequenced block that already contains its own nested
  `except WaveNotReadyError` for one specific precondition failure, and several comments inside it
  already flag a structurally similar residual as a **disclosed gap, not a stated boundary** (the
  unguarded PASS-2 git mutation, a few lines above the loop) — the same shape I am now asked to
  introduce a second instance of.
- The exact set of exception types the wave loop can currently raise is not enumerated anywhere in
  this function (it is whatever bubbles up from `_run_build_wave`, `_wave_is_breached`,
  `_gated_members`, `repository.stub_degrade_transform`, and more) — auditing that set is a wider
  scope than this fix round's I2 mandate.
- Running `execute_hoist_rollback`'s own real git mutation (revert commits under
  `IntegrationMutex`) from a `finally` at the exact moment the run is failing for a **different,
  unaudited** reason risks a git mutation against a monorepo checkout whose state relative to that
  original failure was never characterized.

Disclosed instead: a code comment at the `_reconcile_hoist_rollbacks` call site names exactly this
gap and why forcing the fix was set aside (full text in the diff). Recovery today still depends on
a future `fleet resume` re-entering `Phase.BUILD`, which is not structurally guaranteed.

## M2 — report-reasoning correction (no code change)

Mutation-3's actual cause was SQL operator precedence, not what my original report said. `run_id
= ? OR 1=1 AND kind = 'HoistBrokeOwner'` parses as `run_id = ? OR (1=1 AND kind = ...)` — `AND`
binds tighter than `OR` — so the clause in parentheses reduces to `kind = 'HoistBrokeOwner'`
(the `1=1` conjunct is a no-op), and the WHOLE predicate becomes "match this run_id, OR match this
kind regardless of run_id." The **`run_id` filter** is what became the no-op (any row of the right
kind matches, from any run), not the kind filter as my original report claimed. That is exactly
why the mutation produced a `KeyError` on 4 of 8 fixture cases (cross-run kind matches pulling in
unrelated rows the fixture never expected) rather than a narrower same-run scoping mismatch on 1 of
8. The measured result itself (4/8 `KeyError`) was correct and needs no change — only the
originally-reported *reason* was wrong. I have no prior report file in this worktree to edit in
place (task 72's landing commit, `d69c029`, never committed one — the original report was
delivered only in the dispatching conversation), so this section is the corrected record.

## M3 — dead disjunct (fixed)

`tests/test_hoist_rollback_wiring.py`'s e2e test asserted
`forge.calls == sorted([owner_url, consumer_url]) or set(forge.calls) == {owner_url,
consumer_url}` — the second disjunct subsumes the first (a set equality check is satisfied by any
ordering the sorted-list check would also accept, and more). Collapsed to the set comparison
alone.

## M4 — no action needed

Not re-touched in this fix round; noted in the brief as "fix only if you re-touch it."

## Concerns

- M1's gap is real and disclosed, not hidden — see above and the code comment at the call site.
- No production call site was added that constructs a `PullRequestDraft` with `contract_id` set
  (D122's residual gap is unchanged by this fix round, as scoped) — closing it is still a future
  task's job.
