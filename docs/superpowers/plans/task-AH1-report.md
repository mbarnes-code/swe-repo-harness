# Task AH1 — D42 x rollback interaction (discard_task swallowed as PATCH_REJECTED)

## Verification of the claimed chain (done before any edit)

- `RollbackAnchorError(GitError)` in `src/fleet/vcs/commits.py:101` — confirmed it is a direct
  `GitError` subclass, **not** a `GitCommandError` subclass (`src/fleet/vcs/git.py:105`:
  `GitCommandError(GitError)` is a sibling, not a parent). So before D42, `resolve`/`is_ancestor`
  returning `None`/`False` on an unsettled probe made `discard_task` raise `RollbackAnchorError`,
  which `workers/rewrite.py`'s `except (PatchApplyError, GitCommandError)` genuinely does not
  catch — confirmed by reading both `land_patches` (`workers/rewrite.py:167-181`, unmodified) and
  `RewriteWorker.run`'s two `land_patches` call sites (`workers/rewrite.py:362-379`, `:430-452`).
- Post-D42 (`d37f4ba`), `Git.resolve`/`Git.is_ancestor` (`src/fleet/vcs/git.py:311-333`,
  `:571-585`) raise `GitCommandError` instead of returning `None`/`False` when
  `_require_settled` finds the probe never settled. `discard_task` (`commits.py:327`, `:333`, pre-
  fix) called these with no try/except, so that `GitCommandError` propagated straight out —
  caught by the exact `except (PatchApplyError, GitCommandError)` clauses above, misreported as
  `PATCH_REJECTED`, spending a repair rung on a tree never actually discarded. Confirmed
  `discard_task` has no other production caller (`grep -rn discard_task` → only
  `workers/rewrite.py:180` and `vcs/__init__.py`'s re-export).

## Fix (lane: `src/fleet/vcs/commits.py` + `tests/test_vcs.py` only; `workers/rewrite.py` untouched)

Added `RollbackIndeterminateError(GitError)` — a new, distinct exception, sibling to
`RollbackAnchorError`, **not** a `GitCommandError`/`PatchApplyError` subclass. `discard_task` now
wraps all three of its own git calls (`resolve`, `is_ancestor`, and the `reset_hard`+`clean` pair)
in `try/except GitCommandError`, re-raising as `RollbackIndeterminateError` (`from exc`, so the
verbatim stderr survives as `__cause__`).

Chose the "raise something the repair path does not swallow" option, because the alternative of
giving the rollback its own probe/deadline handling is explicitly undermined by the situation: the
rollback usually runs *because* the deadline already expired, so retrying or extending it isn't a
fix, only a delay of the same problem. `RollbackIndeterminateError` also covers the
"git genuinely cannot be consulted at all" case symmetrically: any `GitCommandError` from
`reset_hard`/`clean` (unsettled OR a genuine settled refusal) becomes the same type, because both
leave the tree's relationship to the anchor equally unproven — the caller's correct response
("don't trust this task's outcome; don't spend a repair rung on it") doesn't depend on which.

`RollbackAnchorError` (settled: anchor missing / not-an-ancestor) is left exactly as it was — it
already escaped `workers/rewrite.py`'s catch correctly and remains distinct in type from the new
"could not determine" case, per the project's four-state-collapse discipline (D29, D34-D45).

## Tests added (`tests/test_vcs.py`)

Two new `CommandRunner` test doubles (`DeadlineMidRollbackRunner`, `RefuseOneCommandRunner`) run
real git for every call except a named argv shape, which comes back unsettled or a settled refusal
respectively — letting a single test target one specific call inside `discard_task` against a real
repo.

- `test_discard_task_unsettled_resolve_probe_is_not_a_plain_GitCommandError`
- `test_discard_task_unsettled_is_ancestor_probe_is_not_a_plain_GitCommandError`
- `test_discard_task_a_failed_reset_is_also_indeterminate_not_a_plain_GitCommandError`
- `test_discard_task_distinguishes_a_settled_refusal_from_an_unsettled_probe` (both directions:
  settled-not-`RollbackIndeterminateError`, unsettled-not-`RollbackAnchorError`)

Reused the pre-existing `test_task_rollback_resets_to_the_task_anchor_and_keeps_earlier_work` to
prove no over-correction — it exercises the full success path (real `reset --hard`+`clean -fdx`)
unchanged.

## STATUS

DONE.

## Commit

`836fd5e4e5e8ca799dd1b2e0a14b7e9daa19429d`

## Test summary

`pytest tests/test_vcs.py -q` → **58 passed**, clean `bazel disk` line, no skips. `ruff check` on
both files: clean. `mypy --strict src/fleet`: clean (107 files, 0 errors). `mypy tests/test_vcs.py`:
1 error, at line 960, in `test_the_integration_mutex_admits_exactly_one_writer` — the pre-existing
known `unreachable` the task said to leave; unrelated to this change and present in code I didn't
touch.

## Concerns

- **`workers/rewrite.py` needed no change.** The fix is entirely inside `discard_task`
  (`commits.py`): converting its own `GitCommandError` into a type
  `except (PatchApplyError, GitCommandError)` was already not catching. Nothing left undone there.
- **`rollback_phase` (`commits.py:342`) has the same *shape* of exposure** — its own
  `git.resolve(phase_base_ref)` call is an unwrapped probe that would raise a bare
  `GitCommandError` on an unsettled deadline, exactly like `discard_task`'s used to. I left it
  unchanged: `grep -rn rollback_phase` shows no production caller today (only `vcs/__init__.py`'s
  re-export and `tests/test_vcs.py`), so there is no live `except (PatchApplyError,
  GitCommandError)` swallowing it — the interaction this task fixes doesn't currently manifest for
  it. Flagging it so whoever wires a caller onto `rollback_phase` knows to check this first.
- `RollbackIndeterminateError` is not (yet) exported from `src/fleet/vcs/__init__.py`'s
  `__all__` — consistent with `RollbackAnchorError` itself also not being exported there today; I
  left that file untouched since it's outside my lane and the omission pre-dates this change.
