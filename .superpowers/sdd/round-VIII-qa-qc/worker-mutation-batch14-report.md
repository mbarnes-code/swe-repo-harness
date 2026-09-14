# Worker report: §15.1 item 3, Wave 3 Batch 14 — cancelled-vs-timeout class re-verification

**Status:** COMPLETE for the files that actually belong to this bug class (4 of 10). The other 6
files in the batch's file list do NOT belong to this bug class — see "Class-sweep assumption"
below. No follow-up dispatch is needed for the cancelled-vs-timeout class itself; a NEW, separate
finding (a live unfixed instance of this exact bug in `workers/clone.py`) is flagged for the
controller to triage and D-number.

**Branch:** `agent/roundviii-mutation-batch14` (worktree `wt-roundviii-mutation-batch14`, created
from `main` via `tools/worktree/new-worktree.sh`)
**Commit:** `debe83a` — "test: add discriminating cancelled-vs-timeout coverage for
classify/interrogate/symbolindex/contracts (round VIII, Wave 3 Batch 14)"
**Not merged, not pushed**, per brief.

## Class-sweep assumption: did NOT hold

The brief named 10 files as one "cancelled-vs-timeout conflation fix" class-sweep from `334edeb`.
Reading `git show 334edeb -- <the 10 files>` and then grepping each file's CURRENT
`ctx.cancelled()`/`ctx.expired()` call sites shows only **4 of the 10** actually received this
fix in `334edeb`:

| File | Actually part of this class? | What `334edeb` really changed here |
|---|---|---|
| `workers/classify.py` | **YES** | conflated existing check → discriminates `timed_out` |
| `workers/interrogate.py` | **YES** | same, in `_interrupted()` |
| `workers/symbolindex.py` | **YES** | same, in `_interrupted()` |
| `workers/contracts.py` | **YES** (different sub-shape: a check that did not exist before at all, not a conflated one being fixed) | added the missing cancel/deadline check |
| `workers/base.py` | NO | pure refactor: `sha256` → `sha256_text`, inline `exception_type` f-string → `exception_type_name()`. No cancel/timeout logic touched. (The commit message's `workers/{symbolindex,interrogate,baseline,classify}.py` line refers to **`baseline.py`**, a different file that is not in this batch's list at all — confirmed `src/fleet/workers/baseline.py` exists separately and already carries the two golden tests for this exact shape, `test_cancelled_before_dispatch_reports_status_cancelled`/`test_expired_before_dispatch_reports_status_timeout` in `tests/test_workers_baseline.py:257,271`.) |
| `workers/clone.py` | NO | unrelated D42 fix (`_default_branch` raises on an indeterminate `symbolic-ref` instead of falling through) + `exception_type_name()` refactor |
| `workers/prwriter.py` | NO | unrelated `ready_failed`/`mark_ready` ordering fix + `exception_type_name()` refactor |
| `workers/rdepverify.py` | NO | `exception_type_name()` refactor only |
| `workers/buildgen.py` | NO | `exception_type_name()` refactor only |
| `llm/client.py` | NO | unrelated falsy-zero `max_output_tokens` fix |

This matches the pattern batch 3 already found ("2 of 3 'same fix' files actually had different
shapes") — the scope report's file list was built from "touched by the commit", not "received
this specific fix", and a broad `/code-review -high` commit like `334edeb` touches many files for
many reasons.

## Per-file results (the 4 real members)

For all four, the additional and more important finding is that **bucket A did not exist** —
despite the scope report's claim of "existing mutation-proof coverage to re-verify", none of the
four had ANY test exercising the "nothing landed/dispatched yet" branch where the cancelled-vs-
timeout discrimination actually lives. Existing tests only covered the "something already landed,
report `partial`" branch (`test_symbolindex_reports_partial_with_completed_units_when_the_deadline_expires`,
`test_symbolindex_stops_between_files_when_cancelled`, `test_classify_preconditions_and_reentry_spend_no_second_call`,
etc.) — none set up an empty `completed`/`landed` list before triggering cancel/expiry. So this
was step 4 of the brief ("write a new discriminating test") for all four, not step 3
("re-verify existing").

### `workers/classify.py`
- New tests: `test_classify_cancelled_before_dispatch_reports_status_cancelled_not_an_attempt`,
  `test_classify_expired_before_dispatch_reports_status_timeout_and_charges_an_attempt`
  (`tests/test_workers_scan.py`).
- Mutation: reverted `run()`'s check to the pre-fix shape (`status="cancelled"` unconditionally,
  `failure_class=FailureClass.TIMEOUT` unconditionally) — exactly `334edeb`'s diff, inverted.
- `git diff --numstat --no-index` against the backup: 2 files changed, non-empty (confirmed the
  mutation actually landed).
- Result: **both** new tests went RED (the cancelled-test on `failure_class` mismatch, the
  expired-test on `status` mismatch) — non-vacuous, both discriminate.
- Restored from backup; `git diff --numstat --no-index` against backup: empty (byte-identical).
  Both tests GREEN again.

### `workers/interrogate.py`
- New tests: `test_interrogate_cancelled_before_any_manifest_reports_status_cancelled`,
  `test_interrogate_expired_before_any_manifest_reports_status_timeout`.
- Mutation: reverted `_interrupted()`'s `not completed` branch to the pre-fix shape.
- Same protocol as above: non-empty diff, both new tests RED under mutation, restored to
  byte-identical, both GREEN.

### `workers/symbolindex.py`
- New tests: `test_symbolindex_cancelled_before_any_file_reports_status_cancelled`,
  `test_symbolindex_expired_before_any_file_reports_status_timeout`.
- Mutation: reverted `_interrupted()`'s `not landed` branch to the pre-fix shape.
- Same protocol: non-empty diff, both new tests RED under mutation, restored to byte-identical,
  both GREEN.

### `workers/contracts.py`
- New tests: `test_cancelled_before_dispatch_reports_status_cancelled_not_an_attempt`,
  `test_expired_before_dispatch_reports_status_timeout_and_charges_an_attempt`
  (`tests/test_workers_contracts.py`), plus a `_ctx_with_deadline()` helper since the file's
  existing `_ctx()` hardcodes `deadline=1e12` and never sets `cancel`.
- Mutation: reverted `run()`'s check to a pre-fix-style unconditional
  `status="cancelled"`/`FailureClass.TRANSIENT_INFRA` (contracts.py's fix is "add the missing
  check", so there is no literal pre-fix conflated form in history — this mutation removes the
  `timed_out` discrimination the same way the other three files' pre-fix code did).
- Non-empty diff, expired-test went RED (cancelled-test stayed GREEN, since its assertions matched
  the mutated unconditional branch — this is expected and non-vacuous: the mutation only removes
  the *timeout* half of the discrimination).
- Restored to byte-identical; both tests GREEN.

All 8 new tests pass together on current code; ran `tests/test_workers_scan.py` (75 tests) and
`tests/test_workers_contracts.py` in full — all green, no regressions from the insertions.
`mypy` (full package, no path args): clean. `ruff check` on both touched files: clean. `ruff
format --check` on both touched files: pre-existing drift unrelated to my insertions (specific
line numbers verified against `git diff` hunks — none overlap my added code); not touched, per
Rule 3 (surgical changes only) and the project's own tracked ruff-format-drift issue.

## New finding: `workers/clone.py` has a LIVE, unfixed instance of this exact bug class

Not part of `334edeb` and not requested by this brief, but directly on-topic and worth flagging
loudly (Rule 11): `workers/clone.py::_interrupted()` (lines ~657–673) still does:

```python
if not done:
    return WorkerResult[CloneOutput](
        status="cancelled",
        error=WorkerError(failure_class=FailureClass.TIMEOUT, ...),
    )
```

unconditionally — it never checks `ctx.expired()` to pick `"timeout"` vs `"cancelled"`, unlike
the fixed siblings. This is called from `run()` at line 328 when `ctx.expired(loop_now()) or
ctx.cancelled()` fires before the worktree is cut. Per `workers/base.py::execute()`'s dispatch
(`if result.status == "cancelled": ... # not an attempt`), a clone that always times out cutting
the worktree would be reported `"cancelled"` and NEVER charge an attempt — looping forever
instead of escalating to `REQUIRES_HUMAN_INTERVENTION` after `MAX_ATTEMPTS`, which is exactly the
defect class `334edeb` fixed elsewhere. `rdepverify.py::_refused()` and `buildgen.py::_interrupted()`
have a narrower version of the same smell (status DOES discriminate `"cancelled"` vs `"timeout"`
correctly there, but `failure_class` is unconditionally `FailureClass.TIMEOUT` in both branches —
harmless for the attempt-charging bug specifically, since `execute()`'s dispatch keys on `status`
not `failure_class`, but still a `FailureClass` that misdescribes a genuine operator cancel as a
timeout). Recommend the controller allocate a D-number and dispatch a small follow-up fix + test
for `clone.py` specifically; I did not fix it here since it is outside this brief's stated scope
(verify existing mutation-proof coverage for `334edeb`'s fix, not hunt new bugs) and Rule 3 says
touch only what you must.

## Files needing follow-up

- **`workers/clone.py`**: needs its own fix (the bug above) + discriminating test, once
  D-numbered by the controller. Not "follow-up to this batch" in the sense of more verification —
  it needs an actual code change.
- **`workers/base.py`, `workers/prwriter.py`, `workers/rdepverify.py`, `workers/buildgen.py`,
  `llm/client.py`**: their real `334edeb` fixes (D42 in clone.py is separate from this; the
  `exception_type_name()` refactor; `ready_failed` reordering; falsy-zero `max_output_tokens`)
  do not appear to be assigned to ANY other batch in `worker-mutation-scope-report.md` — I grepped
  the whole report for each filename and batch 14 is the only batch line naming them. If their
  mutation-proof coverage for those *actual* fixes needs verifying, that needs a fresh batch scoped
  to the real fix each file received, not this cancelled-vs-timeout class.

## Report path

`.superpowers/sdd/round-VIII-qa-qc/worker-mutation-batch14-report.md` (this file).
