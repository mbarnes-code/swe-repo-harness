# Worker report: D138 fix — clone.py's timeout/cancellation conflation (round VIII, Wave 18)

**Status:** COMPLETE. All three files fixed and mutation-proof tested.

**Branch:** `agent/roundviii-d138-fix` (worktree `wt-roundviii-d138-fix`, created from `main` via
`tools/worktree/new-worktree.sh`)
**Commit:** `0b8a192` — "fix: D138 -- clone.py._interrupted() conflates timeout/cancellation;
narrower failure_class fix in rdepverify.py/buildgen.py"
**Doc commit:** (this report is committed alongside the `docs/INTEGRATION_HONESTY.md` heading
update, see below)
**Not merged, not pushed**, per brief.

## Read first (Rule 8)

- `git show 334edeb -- src/fleet/workers/classify.py src/fleet/workers/interrogate.py
  src/fleet/workers/symbolindex.py src/fleet/workers/contracts.py` — the established shape: thread
  a `timed_out` bool through to the "nothing landed" branch, `status="timeout"`/
  `FailureClass.TIMEOUT` when true, `status="cancelled"`/`FailureClass.TRANSIENT_INFRA` otherwise.
- `worker-mutation-batch14`'s report and its commit `debe83a` (both live only on unmerged branches
  `agent/roundviii-mutation-batch14` / `agent/roundviii-review-mutation-batch14`, read via
  `git show <sha>:<path>`) — the naming convention (`test_<worker>_cancelled_before_dispatch_
  reports_status_cancelled_not_an_attempt` / `test_<worker>_expired_before_dispatch_reports_
  status_timeout_and_charges_an_attempt`) and the mutation-proof protocol (backup, mutate to
  pre-fix shape, confirm non-empty diff, confirm new test(s) RED, restore, confirm byte-identical,
  confirm GREEN).

## Fix 1: `workers/clone.py::_interrupted()`

**Before:** took `(completed, owed)`, and when `not done` (nothing landed) unconditionally
returned `status="cancelled"` / `FailureClass.TIMEOUT`.

**After:** takes `(completed, owed, *, timed_out: bool = False)`; when `not done`, returns
`status="timeout" if timed_out else "cancelled"` and `failure_class=FailureClass.TIMEOUT if
timed_out else FailureClass.TRANSIENT_INFRA`.

Two call sites in `run()`:
- `clone.py:311` — `if ctx.cancelled(): return self._interrupted(completed, owed)` — only ever
  checks `ctx.cancelled()` (no `ctx.expired()` check exists at this site, before this fix or
  after), so it passes no `timed_out` and keeps the `False` default. This is unchanged behavior;
  the D138 finding's second call site is the one that discriminates.
- `clone.py:328-330` — was `if ctx.expired(loop_now()) or ctx.cancelled(): return
  self._interrupted(completed, owed)`. Now captures `now = loop_now()` once, checks
  `ctx.expired(now) or ctx.cancelled()`, and passes `timed_out=ctx.expired(now)` through.

## Fix 2: `workers/rdepverify.py::_refused()`

**Before:** `status="cancelled" if ctx.cancelled() else "timeout"` (already correct) but
`failure_class=FailureClass.TIMEOUT` unconditionally in both branches.

**After:** computes `cancelled = ctx.cancelled()` once and uses it for both `status` and
`failure_class=FailureClass.TRANSIENT_INFRA if cancelled else FailureClass.TIMEOUT`.

`_refused()` is called from the very top of `run()` (`if self._stopped(ctx): return
self._refused(ctx, payload, [CLOSURE_UNIT, RDEPS_TEST_UNIT])`), before any bazel work, so it is
directly reachable through `run()` with a minimal payload.

## Fix 3: `workers/buildgen.py::_interrupted()`

Same shape as rdepverify.py: `status` already discriminated correctly via `ctx.cancelled()`;
`failure_class` was unconditionally `FailureClass.TIMEOUT`. Now keys off the same `cancelled`
value computed once.

`_interrupted()`'s "nothing landed" branch (`if not landed:` — actually `if completed:` guards
`partial`, so the branch this fix touches is the trailing `return` when `completed` is empty) is
reached at `buildgen.py:325-326`, `run()`'s step-2 check, which fires before anything is appended
to `completed` whenever `payload.ingest is None` — the only value `cli.py` ever constructs (see
the NOTE in `buildgen.py`'s own source above `run()`). Directly reachable through `run()` with a
minimal `BuildgenInput(unit=_unit())`.

## Mutation-proof evidence

Protocol for each file: copy the post-fix file to a backup, edit the file in place back to its
pre-fix conflated shape, confirm `diff` against the backup is non-empty (the mutation actually
changed something), run the new test(s) and confirm they go RED, restore from backup, confirm
`diff` against the backup is empty (byte-identical restore), re-run the new test(s) and confirm
GREEN.

### `workers/clone.py`

New test: `test_clone_interrupted_with_nothing_landed_discriminates_cancelled_from_timeout`
(`tests/test_workers_scan.py`).

Calls `CloneWorker()._interrupted(...)` **directly** rather than driving it through `run()`. Read
`run()` closely: `UNITS = ("mirror", "worktree")`, and at both of `run()`'s call sites (`:311` and
`:328`) the `"mirror"` unit is *always* already present in `completed` by the time either check
runs — either it was just appended after `_materialize_mirror` succeeded (the only way to reach
`:311` when `"mirror"` was owed), or it was never owed to begin with (meaning it was already
completed in a prior attempt, so the initial `completed = [unit for unit in UNITS if unit not in
owed]` already contains it). So `_interrupted()`'s `not done` (nothing landed) branch — the one
this whole defect lives in — is structurally unreachable via `run()`'s current two call sites with
an empty `completed`. Calling the private method directly is the correct instrument here, and
matches this file's own existing precedent of testing `worker._error_for` directly (see
`tests/test_workers_scan.py:799,832`).

- Mutation: reverted `_interrupted()`'s signature (dropped `*, timed_out: bool = False`) and body
  (unconditional `status="cancelled"` / `FailureClass.TIMEOUT`) to the pre-fix shape; also reverted
  the `:328-330` call site to `if ctx.expired(loop_now()) or ctx.cancelled(): return
  self._interrupted(completed, owed)` (no `timed_out` kwarg).
- `diff` against backup: non-empty (7 insertions / 20 deletions — confirmed the mutation actually
  landed).
- Result: RED — `TypeError: CloneWorker._interrupted() got an unexpected keyword argument
  'timed_out'` (the test calls `_interrupted([], [...], timed_out=False)` and
  `_interrupted([], [...], timed_out=True)`; the interface itself no longer accepts the argument
  under the reverted signature, so the test fails outright rather than merely asserting wrong
  values — this is a valid, non-vacuous RED for a signature-level revert of the fix).
- Restored from backup; `diff` against backup: empty (byte-identical). Test: GREEN (1 passed).

### `workers/rdepverify.py`

New tests: `test_rdepverify_cancelled_before_dispatch_reports_status_cancelled_not_an_attempt`,
`test_rdepverify_expired_before_dispatch_reports_status_timeout_and_charges_an_attempt`
(`tests/test_workers_build.py`), driven through `RdepverifyWorker().run(ctx, payload)` with
`ctx.cancel.set()` / `make_ctx(tmp_path, seconds_left=-1.0)` respectively.

- Mutation: reverted `_refused()` to the pre-fix shape (`failure_class=FailureClass.TIMEOUT`
  unconditionally, `status` still correctly discriminating via `ctx.cancelled()` inline).
- `diff` against backup: non-empty (2 insertions / 6 deletions).
- Result: the **cancelled** test went RED (`failure_class` mismatch: got `TIMEOUT`, expected
  `TRANSIENT_INFRA`); the **expired** test stayed GREEN (its assertions match the mutated
  unconditional-`TIMEOUT` branch exactly) — expected and non-vacuous, matching batch14's own
  `contracts.py` result: the mutation only removes the *cancelled* half of the discrimination
  here, since `status` was already correct and only `failure_class` regressed.
- Restored from backup; `diff` against backup: empty (byte-identical). Both tests: GREEN (2
  passed).

### `workers/buildgen.py`

New tests: `test_buildgen_cancelled_before_dispatch_reports_status_cancelled_not_an_attempt`,
`test_buildgen_expired_before_dispatch_reports_status_timeout_and_charges_an_attempt`
(`tests/test_workers_build.py`), driven through `BuildgenWorker().run(ctx,
BuildgenInput(unit=_unit()))` with `ctx.cancel.set()` / `make_ctx(tmp_path, seconds_left=-1.0)`.

- Mutation: reverted `_interrupted()`'s trailing `return` to the pre-fix shape
  (`failure_class=FailureClass.TIMEOUT` unconditionally).
- `diff` against backup: non-empty (2 insertions / 6 deletions).
- Result: the **cancelled** test went RED (same shape as rdepverify.py — `failure_class` mismatch);
  the **expired** test stayed GREEN — expected, non-vacuous, same reasoning as rdepverify.py.
- Restored from backup; `diff` against backup: empty (byte-identical). Both tests: GREEN (2
  passed).

## Full test runs, mypy, ruff

- `tests/test_workers_scan.py`: 42 passed.
- `tests/test_workers_build.py`: 97 passed.
- `mypy` (full package, no path args): `Success: no issues found in 132 source files`.
- `ruff check` on all 5 touched files (`src/fleet/workers/{clone,rdepverify,buildgen}.py`,
  `tests/test_workers_{scan,build}.py`): `All checks passed!`.
- `ruff format --check` on all 5 touched files: each file reports pre-existing drift (verified by
  line number against `git diff` hunks — none overlap any line this task touched: clone.py's drift
  is at lines 414/505-509/542-546/592-596/611-615/643-647, my edits are at 328-330 and 657-680;
  rdepverify.py's drift is at 260/299-301/419-421, my edit is at 443-457; buildgen.py's drift is at
  190-193/286+, my edit is at 660-672 with none reported in that range; test_workers_build.py's
  drift is scattered across lines 1878-4625, my insertions are at ~549-578 and ~655-687). Not
  touched, per Rule 3 (surgical changes only) and this project's own tracked ruff-format-drift
  pin issue (`d4c479a`/`5d82140`/`adeb50b`).

## `docs/INTEGRATION_HONESTY.md`

D138's heading moved from `OPEN` to `FIXED, LANDED (`0b8a192`, round VIII Wave 18, branch
`agent/roundviii-d138-fix`, not yet merged to `main`)` — disclosing the branch explicitly per this
project's own convention note (§3527: a landed SHA should be the post-rebase `main` commit, not an
unmerged `agent/*` branch SHA, since a rebase changes it; since this branch is not merged, the
current branch commit is the best available citation and the controller should update it to the
post-merge SHA once this lands). The body is left untouched; a dated note is appended below it
per the file's own convention (see the D49 example at line 2830), summarizing the fix and pointing
at this report.

## Report path

`.superpowers/sdd/round-VIII-qa-qc/worker-d138-fix-report.md` (this file).
