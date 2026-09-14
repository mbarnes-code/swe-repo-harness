# Report: §15.1 item 3, Wave 3 Batch 12 — re-verify mutation proofs for vcs/git.py, vcs/commits.py

## Scope

Touched two files: `tests/test_vcs.py` (existing test rewritten, `PROBE_NAMES` extended) and this
report. No production code changes — `src/fleet/vcs/git.py` and `src/fleet/vcs/commits.py` are
byte-identical to `main` (verified with `git diff --stat`, empty, after every mutation experiment
restore).

## The fix under test (`334edeb`)

`334edeb` touched `src/fleet/vcs/git.py` and `src/fleet/vcs/commits.py` in two unrelated ways:

1. **git.py — D42 bug class, three MORE sites**: added `self._require_settled(result)` right
   after a `check=False` probe in `current_branch` (line 368), `remote_url` (line 389), and
   `blob_at` (line 567) — the same "an indeterminate git failure (never started / killed at
   deadline) must not be reported as the probe's ordinary negative answer" fix already applied
   to `resolve`/`ref_exists`/`apply_check`/`is_ancestor` in an earlier commit.
2. **git.py — rename notation**: added `_RENAME_BRACE_RE` / `_resolve_numstat_path()` and wired
   it into `_parse_numstat()`, so `git diff --numstat`'s rename compression (`common/{old =>
   new}` or bare `old => new`) resolves to the file's real current path instead of being taken
   verbatim as `parts[-1]`.
3. **commits.py — pure refactor**: `patch_id()`'s two inline
   `hashlib.sha256(...).hexdigest()` calls were replaced with the new `fleet.util.hashing
   .sha256_text()` helper. `sha256_text(text) == sha256_bytes(text.encode("utf-8")) ==
   hashlib.sha256(text.encode("utf-8")).hexdigest()` — read `src/fleet/util/hashing.py:19-20`
   directly to confirm this is byte-for-byte the same computation, not a behavior change. No
   mutation is warranted for a change that provably does not alter output; `patch_id`'s existing
   general-property test (`test_patch_id_is_a_pure_function_of_content`, `tests/test_vcs.py`)
   still passes unmodified.

## Finding: the rename-notation "existing proof" did not discriminate the fix

`tests/test_vcs.py::test_diff_stat_resolves_a_renamed_files_real_path` was the citation for fix
(2). Re-running the exact mutation (`_resolve_numstat_path` body replaced with `return raw`,
i.e. the pre-fix behavior of taking the numstat field verbatim) against the ORIGINAL test body:

- **Diff-verify-nonempty**: `git diff --numstat --no-index <backup> <mutated>` → `0  5` (5 lines
  removed — the mutation genuinely landed).
- **Test**: `pytest tests/test_vcs.py -k test_diff_stat_resolves_a_renamed_files_real_path` →
  **1 passed** under the mutant. The test did NOT discriminate.

Root cause, confirmed empirically (`/tmp/test_repo`, plain `git` outside pytest): the old test
called `git mv` then read `git.diff_stat()` — the DEFAULT worktree-vs-index diff. But `git mv`
itself already stages the rename into the index, so the remaining worktree-vs-index diff is a
same-path content diff (`git diff --numstat` after `git mv a.txt renamed.txt` prints `1␉0␉
renamed.txt`, no `=>` at all). The rename arrow/brace notation only appears when diffing the
rename ITSELF — `git diff --cached --numstat` (index vs `HEAD`) — confirmed both forms:
`a.txt => renamed.txt` (no common affix) and `common/{old.txt => new.txt}` (shared affix). The
old test's two assertions were satisfied by the trivial identity behavior `_resolve_numstat_path`
was written to replace, so `_resolve_numstat_path`'s actual logic was never exercised — a stale
proof per Rule 12 ("mutation testing verifies the implementation, not the truth of the test's
name").

## Fix: rewrote the test to actually exercise both notations

New body of `test_diff_stat_resolves_a_renamed_files_real_path` (`tests/test_vcs.py`): commits a
`common/old.txt` file first, then stages (uncommitted) two simultaneous renames — `a.txt ->
renamed.txt` (bare form) and `common/old.txt -> common/new.txt` (brace form) — and reads
`git.diff_stat(staged=True)` (index vs `HEAD`), which is where git actually emits the rename
notation. (Both renames must be read while still staged-but-uncommitted: committing either one
first folds it into `HEAD` and drops it out of a subsequent `staged=True` diff — hit this
ordering bug once while iterating and documented it in the test's own docstring.)

**Mutation-proof procedure** (Rule 12: backup / edit / diff-verify-nonempty / test / restore /
re-verify-identical), against the NEW test body:

1. Backup: `src/fleet/vcs/git.py` → `/tmp/git.py.backup2`.
2. Edit: same mutation as before (`_resolve_numstat_path` body → `return raw`).
3. Diff-verify-nonempty: `git diff --numstat --no-index` → `0  5` (mutation landed).
4. Test (mutant): **FAILED** —
   `assert {'a.txt => renamed.txt', 'common/{old.txt => new.txt}'} ==
   {'common/new.txt', 'renamed.txt'}` — the raw, unresolved compound strings, exactly the
   pre-fix defect.
5. Restore: backup copied back; `git diff --stat src/fleet/vcs/git.py` → empty.
6. Re-verify-identical: same test against restored code → **1 passed**.

Old-passes/new-fails is trivially satisfied (old test passed under the mutant; new test fails
under the identical mutant), and the mutation is confirmed to have actually changed the file
before either result was read.

## Finding: no proof existed at all for the three `_require_settled()` additions

`current_branch`, `remote_url`, and `blob_at` are the same shape as the D42-class methods already
covered by the parametrized `PROBE_NAMES` suite (`resolve`, `ref_exists`, `apply_check`,
`is_ancestor`), but `grep -n "current_branch\|blob_at" tests/` showed zero test references to
either name outside a happy-path use in `tests/test_cli.py`, and `remote_url` had only the
PAT-redaction test (unrelated to settledness). Re-running the D42 mutation battery
(`ScriptedRunner(exit_code=124, started=False, timed_out=True)` / `exit_code=-15, started=True,
timed_out=True`) against current code confirmed these three sites had never been exercised
against an unsettled `ProcResult` — the `_require_settled()` calls 334edeb added to them shipped
with zero discriminating coverage.

## Fix: extended `PROBE_NAMES`

Added `"current_branch"`, `"remote_url"`, `"blob_at"` to `PROBE_NAMES` and `_run_probe()` in
`tests/test_vcs.py`. All three return `str | None` (same as `resolve`), so the three existing
generic parametrized tests apply unchanged — no new test bodies needed:
- `test_d42_probe_never_started_raises_naming_it_never_started`
- `test_d42_probe_killed_at_deadline_raises_naming_the_kill`
- `test_d42_probe_genuine_no_still_returns_the_old_answer_without_raising`

**Mutation-proof procedure**, against the extended `PROBE_NAMES`:

1. Backup: `src/fleet/vcs/git.py` → `/tmp/git.py.backup3`.
2. Edit: removed all three `self._require_settled(result)` lines (from `current_branch`,
   `remote_url`, `blob_at`).
3. Diff-verify-nonempty: `git diff --numstat --no-index` → `0  3` (3 lines removed, mutation
   landed).
4. Test (mutant): `pytest tests/test_vcs.py -k test_d42_probe` → **6 failed, 15 passed** — exactly
   the 3 new probes × the 2 discriminating tests (`never_started`, `killed_at_deadline`); the
   3 new probes' `genuine_no` cases correctly stayed green (a settled non-zero exit is a real
   answer, not the defect under test), matching the pre-existing 4-probe pattern exactly.
5. Restore: backup copied back; `git diff --stat src/fleet/vcs/git.py` → empty.
6. Re-verify-identical: `pytest tests/test_vcs.py -k test_d42_probe` against restored code →
   **21 passed** (7 probes × 3 tests).

## Verification (full)

- `git status --short` / `git diff --stat` (worktree, after all restores) → `src/fleet/vcs/git.py`
  and `src/fleet/vcs/commits.py` both empty (byte-identical to `main`); only
  `tests/test_vcs.py` and this report differ.
- `python -m mypy` (whole package, no path args, per `pyproject.toml`'s `packages = ["fleet"]`) →
  `Success: no issues found in 132 source files`.
- `ruff check tests/test_vcs.py` → All checks passed.
- `ruff format --check tests/test_vcs.py` → 1 file would be reformatted, but this is
  **pre-existing on `main`** (re-measured directly against `HEAD` via `git stash` — identical
  3-hunk finding at lines 1683/1883/1903, all inside unrelated, untouched test bodies far from
  this task's edits at lines ~392-475) — not introduced by this change.
- `pytest tests/test_vcs.py` (whole file, no `-k`) → **88 passed** (original code, after final
  restore).
- Every interpreter invocation above ran under `env -i PATH=/usr/bin:/bin HOME="$HOME"
  PYTHONPATH="$WT/src" .venv/bin/python`, with `fleet.__file__` asserted to start with the
  worktree path before trusting any result (Rule 12's worktree-pin guardrail).

## Result

- Status: **complete**.
- Branch: `agent/roundviii-mutation-batch12` (from `main`, worktree
  `/home/redmage/swe repo harness worktrees/wt-roundviii-mutation-batch12`).
- Existing proof status:
  - `patch_id`/commits.py refactor: existing test held (no behavior change to discriminate;
    verified the refactor is byte-identical by reading `util/hashing.py` directly).
  - Rename-notation fix (git.py): existing proof was **STALE** — did not discriminate the fix at
    all (root cause: read the wrong diff mode, `worktree-vs-index` instead of `staged-vs-HEAD`).
    Replaced with a discriminating test; both old and new bodies mutation-tested to confirm the
    old one didn't kill the mutant and the new one does.
  - `_require_settled()` additions to `current_branch`/`remote_url`/`blob_at` (git.py): **NO
    proof existed** — never added to the `PROBE_NAMES` parametrized suite that covers the same
    bug class for sibling methods. Added them; confirmed the extension kills the mutant (6/6
    expected failures) and passes on original code (21/21).
- Not merged, not pushed — controller reviews and merges.
