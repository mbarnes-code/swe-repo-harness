# Report: §15.1 item 3, Wave 2 Batch 9 (LAST of 5) — mutation-proof cli.py::_emit_one_pr

## Scope

Touched only `_emit_one_pr` (read/analysis) in `src/fleet/cli.py` — no production code changes
were needed (see below). No test file changes were needed either. `src/fleet/cli.py` and
`tests/test_cli.py`/`tests/test_pr_e2e.py` are all unchanged from `main` (verified with
`git status --short` / `git diff --stat`, empty output, after the mutation experiment restore).

**Correction to the brief's scoping**: the brief says "its tests in `tests/test_cli.py`", but
`_emit_one_pr` has no tests in `tests/test_cli.py` at all (`grep -n "_emit_one_pr" tests/test_cli.py`
and `grep -n "_emit_prs\b" tests/test_cli.py` both return nothing). Its actual coverage lives in
`tests/test_pr_e2e.py`, which exercises it end-to-end through the real `fleet pr` CLI. Re-derived
this at the moment of use per CLAUDE.md's "primary source over inherited claims" — the brief's file
name was an unverified guess, not measured.

## The fix under test (334edeb)

`334edeb` fixed `cli.py::_emit_one_pr` (src/fleet/cli.py:15849-15855 on `main`): when
`worker.run()` returns a non-ok result whose `output.pr is not None` — i.e. `gh.create_pr`
(or the Gitea equivalent) succeeded but a later unit (`mark_ready`) failed — the function now
returns the `PrwriterOutput` itself (so `_emit_prs` persists `output.pr`'s url) instead of
collapsing straight to a bare error string:

```python
    error = result.error
    if error is None:  # pragma: no cover - a non-ok result always carries its error
        return f"forge {payload.forge!r} failed with no error attached"
    if output.pr is not None:
        # ... (fix)
        return output
    return f"{error.exception_type}: {error.stderr_tail}"
```

Before the fix, that `if output.pr is not None: return output` block did not exist, so any
non-ok result — including one where `gh pr create` had already landed a real PR — fell straight
through to `return f"{error.exception_type}: {error.stderr_tail}"`, a bare string with no `pr`
field. `_emit_prs` (the caller) can only persist `outcome.pr.url` when `outcome` is a
`PrwriterOutput`; a bare string means the PR record is lost, and a retry calls `gh pr create`
again for a branch that already has an open PR on the forge (which the real forge refuses).

## Existing coverage already discriminates this defect

`tests/test_pr_e2e.py::test_a_failed_mark_ready_does_not_lose_the_pr_or_recreate_it` (line 769)
was written expressly for this fix: it makes `FakeForge` fail `gh pr ready` for a specific repo
after `gh pr create` has already succeeded, runs `fleet pr --ready --repo acme-lib-py` through the
real CLI, and asserts:
- the run reports `failed` (exit code `UNEXPECTED_ERROR`),
- `gh pr create` was called **exactly once**,
- the PR state persisted in SQLite is `OPEN` (i.e. the `PullRequestDraft` record was NOT lost),
- a retry (`fleet pr --ready --repo acme-lib-py` again) does **not** call `gh pr create` a second
  time, and reports the repo as `already_open`.

This directly exercises the `output.pr is not None` branch this fix added.

## Mutation-proof procedure (Rule 12: backup / edit / diff-verify-nonempty / test / restore /
re-verify-identical)

1. **Backup**: `src/fleet/cli.py` copied to a per-lane scratch path before any edit
   (`/tmp/.../scratchpad/batch9/cli.py.bak`).
2. **Edit**: reverted the fix in place — deleted the `if output.pr is not None: ... return output`
   block (7 lines, including its explanatory comment), leaving only
   `return f"{error.exception_type}: {error.stderr_tail}"` after the `error is None` guard —
   exactly the pre-`334edeb` shape.
3. **Diff-verify-nonempty**: `git diff --numstat --no-index <backup> <mutated>` → `0  7` (7 lines
   removed, mutation actually landed, not a no-op).
4. **Test** (mutant): `pytest tests/test_pr_e2e.py::test_a_failed_mark_ready_does_not_lose_the_pr_or_recreate_it`
   → **FAILED**:
   ```
   assert pr_states(fleet)["acme-lib-py"] == PrState.OPEN.value, pr_states(fleet)
   KeyError: 'acme-lib-py'
   ```
   i.e. under the mutant, no `PullRequestDraft` record was persisted at all — exactly the
   pre-fix "PR record lost" defect.
5. **Restore**: copied the backup back over `src/fleet/cli.py`; `git diff --stat src/fleet/cli.py`
   → empty (byte-identical to pre-mutation / `main`).
6. **Re-verify-identical**: re-ran the same test against the restored/original code:
   **1 passed**.

This is the old-passes/new-fails discriminator Rule 12 requires, on a mutation confirmed to have
actually changed the file (not a no-op), with the existing e2e test as the sole discriminator —
no new test was needed.

## Verification (full)

- `git status --short` / `git diff --stat` (worktree, after restore) → empty; no files changed
  from `main`.
- `mypy` (whole package, no path args, per `pyproject.toml`'s `packages = ["fleet"]`) →
  `Success: no issues found in 132 source files`.
- `ruff check src/fleet/cli.py tests/test_pr_e2e.py tests/test_cli.py` → All checks passed.
- `ruff format --check src/fleet/cli.py tests/test_pr_e2e.py tests/test_cli.py` → 3 files would
  be reformatted, but this is **pre-existing on `main`** (re-measured directly: the same command
  against the unmodified primary checkout at `/home/redmage/swe repo harness` reports the
  identical 3-file finding) — unrelated to this task, untouched, and none of the reformatting
  hunks fall inside `_emit_one_pr` or the discriminating test.
- `pytest tests/test_pr_e2e.py` (whole file, no `-k`) → **26 passed** (original code).
- `pytest tests/test_cli.py` (whole file, no `-k`) → **210 passed** (original code).

## Result

- Status: **complete**.
- Branch: `agent/roundviii-mutation-batch9` (from `main`, worktree
  `/home/redmage/swe repo harness worktrees/wt-roundviii-mutation-batch9`).
- Files changed: none in `src/` or `tests/` — existing coverage for `_emit_one_pr` (in
  `tests/test_pr_e2e.py`, not `tests/test_cli.py` as the brief guessed) already proved
  mutation-proof. This report is the only tracked-tree change.
- Mutation result: `test_a_failed_mark_ready_does_not_lose_the_pr_or_recreate_it` kills the mutant
  (the `output.pr is not None` early-return reverted) and passes on the original.
- Not merged, not pushed (per brief — this is the LAST of the 5 sequential batches; controller
  merges after review).
