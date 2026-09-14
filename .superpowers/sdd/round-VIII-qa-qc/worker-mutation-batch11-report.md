# Report: §15.1 item 3, Wave 3 Batch 11 — re-verify mutation proofs for
`orchestrator/runner.py`, `orchestrator/stubs.py`

## Scope

Both files touched by `334edeb` (budget-ledger wiring, `reservation_ttl_s`). Read the exact diff
(`git show 334edeb -- src/fleet/orchestrator/runner.py src/fleet/orchestrator/stubs.py`, 9 hunks,
60/10 lines runner.py, 2/2 stubs.py) before deriving anything from the commit message alone (the
message's "wave runners never wired a real cost estimate" refers to the `estimate:
Callable[[str], CostEstimate]` constructor parameter and its call sites, which this commit did NOT
touch in these two files — that parameter and its `ZERO_COST` default already existed pre-commit.
What 334edeb actually changed in these two files is: (a) distinguishing settle-time
`ReservationRefusedError` (re-raise) from every other settle-time `BudgetRefusedError` (route
through `_on_breach`) in `PhaseRunner._dispatch`, (b) reloading `LadderState.transient_retries`
from the persisted phase row instead of always 0, (c) threading that same `ladder` into `_on_breach`
/ `_record_diagnostics` instead of a fresh `LadderState()`, (d) `getattr(breach, "exit_code",
None)` instead of `breach.exit_code` (needed because `state.repository.BudgetRefusedError` has no
`exit_code` attribute, unlike `orchestrator.budgets.LedgerBreach`), (e) the `exception_type_name()`
refactor (cosmetic, `util/errors.py` de-duplication), and (f) `stubs.py`'s `sha256_text()` refactor
(cosmetic, `util/hashing.py` de-duplication).

## Method (Rule 12: backup / edit / diff-verify-nonempty / test / restore / re-verify-identical)

Baseline: `tests/test_runner.py` + `tests/test_stubs.py` = 101 tests, 3.1s, all green before any
mutation. Backups of both source files taken to a per-lane scratchpad before any edit; every
mutation below was verified nonempty via `git diff --stat` against the backup before running
tests, and the file was restored + re-verified byte-identical (`git status --short` empty) + green
before moving to the next mutation.

### Mutation 1 — re-verify the EXISTING cited proof (runner.py)

The source itself cites the existing proof inline (`runner.py`'s comment on the
`except ReservationRefusedError: raise` clause): `tests/test_runner.py::
test_a_runner_minted_reservation_names_its_owner_so_the_reaper_can_fence_it`.

Mutation: deleted the `except ReservationRefusedError: raise` clause (12 lines, comment included),
so a `ReservationRefusedError` falls through to `except BudgetRefusedError as breach: return
_Dispatched(breach=breach)` (valid, since `ReservationRefusedError` subclasses `BudgetRefusedError`).

Result: **1 failed, 56 passed** — exactly the cited test failed
(`report.outcomes["repo-a"].failure_class` became `BUDGET_EXHAUSTED` instead of `UNKNOWN`).
Restored → 57 passed. **Verdict: the existing proof still discriminates current code. HOLDS.**

### Mutation 2 — `getattr(breach, "exit_code", None)` (runner.py, `_on_breach`)

Reverted to `breach.exit_code` (direct attribute access, as it read before 334edeb — but 334edeb
also widened the parameter type to accept a bare `BudgetRefusedError`, which has no `exit_code`).

Result against the ORIGINAL 57 tests: **57 passed, 0 failed** — no existing test drives a
settle-time `BudgetRefusedError` (as opposed to a reserve-time `LedgerBreach`) through
`_on_breach`, so this line had **no proof at all**. Confirmed gap, not staleness — the line is new
in 334edeb and nothing exercises it.

### Mutation 3 — `transient_retries=0 if row is None else row.transient_retries` (runner.py, `_drive`)

Reverted to `transient_retries=0,` unconditionally (the pre-334edeb behaviour the commit's own
"D-closing" comment describes as the bug: resetting the transient-retry counter on every
re-admission instead of reloading it).

Result against the ORIGINAL 57 tests: **57 passed, 0 failed** — same shape of gap: new code
introduced by 334edeb, no existing test reaches it (every existing breach-path test starts a fresh
repo at `transient_retries=0`, so the mutation and the fix are indistinguishable to them).

### Gap closure — two new discriminating tests added to `tests/test_runner.py`

1. **`test_a_repo_scoped_breach_preserves_the_reloaded_transient_retries`** — pre-seeds
   `phases.transient_retries=2` via raw SQL (mirroring the existing `Harness.set_attempts` pattern),
   triggers a repo-scoped `LedgerBreach` (same construction as
   `test_a_repo_scoped_ceiling_does_not_halt_the_fleet`), and asserts the post-breach row still
   reads `transient_retries == 2`. Re-ran mutation 3 with this test present: **1 failed (this test),
   58 passed** — confirmed discriminating. Restored → 58 passed.

2. **`test_a_settle_time_budget_refusal_is_recorded_as_a_repo_scoped_breach_not_a_crash`** —
   monkeypatches `CostLedger.settle` (bound method, via `pytest`'s `monkeypatch` fixture) to raise
   `RepoBudgetRefusedError` for one repo on the way out of `ledger.dispatch()`'s `finally`, then
   asserts the run does not halt, the repo lands at `REQUIRES_HUMAN_INTERVENTION` /
   `BUDGET_EXHAUSTED`, and `last_error` carries the breach message (not an `AttributeError`
   traceback). Re-ran mutation 2 with this test present: **1 failed (this test), 58 passed** —
   confirmed discriminating (the mutation reproduces the exact `AttributeError:
   'RepoBudgetRefusedError' object has no attribute 'exit_code'` the fix exists to prevent,
   escaping `_on_breach` and getting recorded as `FailureClass.UNKNOWN` by the generic per-repo
   `except Exception` handler instead). Restored → 58 passed.
   The CAS mechanics that produce a genuine `RepoBudgetRefusedError` from `settle_repo_budget` are
   not re-derived here — that is `tests/test_repository.py`'s job; this test is scoped to
   `PhaseRunner`'s handling of the exception once raised.

Both tests pass on current (unmutated) code; both were run against the ORIGINAL mutation before the
other test existed to confirm they, and only they, catch it (no other test in the 101/103-test
baseline flips under either mutation).

A first draft of `flaky_settle` closed over a loop-scoped `real_settle` local and was flagged by
`ruff check` (B023, unbound loop variable in a closure); fixed with the same default-argument
binding pattern `Harness._seed`'s own loop-scoped closure already uses in this file
(`unit(conn, rid: str = repo_id)`).

### Mutation 4 — re-verify the EXISTING proof (stubs.py)

`revalidation_key`'s digest (`stubs.py`'s only production change in 334edeb: `hashlib.sha256(...)`
→ `sha256_text(...)`, a pure refactor to `util/hashing.py`'s shared helper) is proven by
`tests/test_stubs.py::test_batched_coalesces_one_consumer_into_one_keyed_round`, which computes an
independent expected digest with its own `from hashlib import sha256` import (not the
implementation's `sha256_text` helper) — a genuine external oracle, not a copy of the code under
test.

Mutation: changed the join separator from `"\n".join(ids)` to `",".join(ids)`.

Result: **1 failed, 43 passed** — the cited test failed on the digest mismatch (`r1:225214ed... !=
r1:491e64ba...`). Restored → 44 passed. **Verdict: the existing proof still discriminates current
code. HOLDS.**

## Verification

- `tests/test_runner.py` + `tests/test_stubs.py`: **103 passed** (101 original + 2 new), 3.1s.
- `git diff --stat -- src/fleet/orchestrator/runner.py src/fleet/orchestrator/stubs.py` against
  `main`: **empty** — no production code changed; both fixes are correct as shipped.
- `python -m mypy` (no path args, full-package scope per `pyproject.toml`'s `packages = ["fleet"]`):
  `Success: no issues found in 132 source files` (tests/ is out of mypy's declared scope; the only
  change here is in `tests/`).
- `ruff check .`: `All checks passed!`
- `ruff format --check .`: `122 files would be reformatted, 227 files already formatted` — matches
  the currently pinned/disclosed drift baseline from `adeb50b`/`5d82140`/`d4e5db1` (re-pinned to
  122 after excluding `.superpowers/`); confirmed this round introduces **no new** drift by diffing
  `ruff format --check` hunk-counts for `tests/test_runner.py` before vs. after this round's edit
  (19 dash-separators each, same content, only line-shifted) — the file's pre-existing format drift
  (5 unrelated hunks elsewhere in the file, present on `main` already) is untouched by this change.

## Summary

| File | Existing cited proof | Status |
|---|---|---|
| `orchestrator/runner.py` (`ReservationRefusedError` re-raise) | `test_a_runner_minted_reservation_names_its_owner_so_the_reaper_can_fence_it` | **HOLDS**, re-verified |
| `orchestrator/stubs.py` (`revalidation_key` digest) | `test_batched_coalesces_one_consumer_into_one_keyed_round` | **HOLDS**, re-verified |
| `orchestrator/runner.py` (`_on_breach`'s `getattr(breach, "exit_code", None)`) | none found | **gap — closed** with `test_a_settle_time_budget_refusal_is_recorded_as_a_repo_scoped_breach_not_a_crash` |
| `orchestrator/runner.py` (`_drive`'s `transient_retries` reload + `_on_breach`'s `ladder=ladder` threading) | none found | **gap — closed** with `test_a_repo_scoped_breach_preserves_the_reloaded_transient_retries` |

Net: the two mutation-proofs the batch was scoped to re-verify both hold unchanged. Two further
lines changed by the same commit in the same files had no proof at all (new code, not stale code —
Bucket A's file-level "some Rule-12-discipline test reaches this file" does not imply per-line
coverage, exactly as `worker-mutation-scope-report.md` §0 flags); both gaps are now closed with new
discriminating tests following the same backup/edit/diff-verify/test/restore/re-verify discipline.

## Files changed

- `tests/test_runner.py` — two new tests + three import additions (`Reservation` from
  `orchestrator.budgets`; `RepoBudgetRefusedError` added to the existing `state.repository` import).
  No other file changed.

## Branch / commit

Branch: `agent/roundviii-mutation-batch11` (from `main`, worktree
`/home/redmage/swe repo harness worktrees/wt-roundviii-mutation-batch11`). **Not merged, not
pushed**, per brief.
