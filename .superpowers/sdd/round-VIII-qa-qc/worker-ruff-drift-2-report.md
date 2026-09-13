# Report: close ruff-format pinned-baseline drift (125 -> 126), round VIII Wave 13

## Status: DONE. Pin remains 125; `tests/test_lint_gate.py::test_ruff_format_check_dirty_count_matches_the_pinned_baseline` passes.

Branch: `agent/roundviii-ruff-drift-2` (from `main` at `d9c4479`), in worktree
`/home/redmage/swe repo harness worktrees/wt-roundviii-ruff-drift-2`. Not merged, not pushed.

## Measurement method

Direct measurement, not carried forward from the brief's framing:

1. In the isolated worktree (checked out from `main` at `d9c4479`), ran
   `ruff format --check --no-cache --output-format=concise .` (the pinned test's own exact
   command) → **126 files would be reformatted, 258 files already formatted**, confirming the
   brief's premise.
2. Checked out the 125-pin commit itself (`ed34c9d`) into a second, separate scratch worktree
   (`git worktree add --detach <scratch>/wt ed34c9d`) and ran the identical command there, using
   the SAME ruff binary (`ruff 0.16.2`, from the round VIII worktree's `.venv`, matched against
   the pinned version in `pyproject.toml`) → **125 files would be reformatted, 250 files already
   formatted** — reproducing the existing pin exactly.
3. Extracted the per-file dirty path list from both runs (`_FORMAT_PER_FILE`-equivalent regex)
   and diffed them (`comm -23` / `comm -13`):
   - Newly dirty (in current, not in pin-125 set): **exactly one file** —
     `.superpowers/sdd/round-VIII-qa-qc/worker-citation-drift-fix-report.md`
   - Newly clean (in pin-125 set, not in current): **none**

This is a clean, single-file +1 drift — no net cancellation, no second file involved.

## Root cause

`.superpowers/sdd/round-VIII-qa-qc/worker-citation-drift-fix-report.md` is a report committed by
this round's own `agent/roundviii-citation-drift-fix` lane (merged at `5fe412d`/`d9c4479`). It
contains a small illustrative Python snippet in a fenced ` ```python ` block:

```python
unresolved = [a for a in survey.anchored if not a.resolves]
len(survey.anchored)      # -> 87
len(unresolved)           # -> 56
```

`ruff format` formats Python code fences inside Markdown files (not just `.py`/`.pyi`/`.ipynb`),
and this snippet's manually-aligned trailing comments (`# -> 87` / `# -> 56`, padded with extra
spaces to align the `#`) are not ruff's canonical single-space-before-comment style — hence
"unformatted."

## Confirmed safe to reformat (not one of the 3 ADR-0116-excluded files)

Checked `docs/DECISIONS.md`'s ADR-0116 addendum and `tests/test_lint_gate.py`'s own comment trail
above `_RUFF_FORMAT_DIRTY_BASELINE` first. The exclusion list there is exactly three files:
`.superpowers/sdd/round-VI-criteria-closure/task-{78,80,105}-report.md`, excluded because they
contain verbatim historical code quotations (e.g. `Before (pre-fix, `b4bc8be`):` fenced blocks)
that reformatting would rewrite, contrary to the annotate-never-rewrite convention.

`worker-citation-drift-fix-report.md` is a **different** file, not on that list, and its Python
fence is not a historical quotation of production source at a named commit — it is a throwaway
illustrative snippet the report's own text introduces as "computed... in a throwaway script" to
show a live REPL-style computation, not a quotation of any file's actual historical content.
Reformatting it changes nothing it documents: the values it reports (`87`, `56`) are untouched;
only inter-token whitespace before the trailing comments changed.

## Fix applied

`ruff format --no-cache .superpowers/sdd/round-VIII-qa-qc/worker-citation-drift-fix-report.md`

Resulting diff (verified via `git diff`, whitespace-only):

```diff
 unresolved = [a for a in survey.anchored if not a.resolves]
-len(survey.anchored)      # -> 87
-len(unresolved)           # -> 56
+len(survey.anchored)  # -> 87
+len(unresolved)  # -> 56
```

No behavior change: the file is a Markdown report, not code that executes or is imported. `ruff
check .` and `mypy --strict src/fleet/` do not scope this file at all (it's outside `src/`/
`tests/`); both were re-run as part of the full `tests/test_lint_gate.py` suite below and remain
clean, confirming no collateral effect.

## Test output confirming the pin passes

```
tests/test_lint_gate.py::test_every_declared_ruff_requirement_pins_one_exact_version PASSED [ 14%]
tests/test_lint_gate.py::test_the_ruff_that_will_run_is_the_ruff_that_is_pinned PASSED [ 28%]
tests/test_lint_gate.py::test_ruff_resolves_only_this_projects_own_files PASSED [ 42%]
tests/test_lint_gate.py::test_ruff_check_is_clean_across_the_whole_repository PASSED [ 57%]
tests/test_lint_gate.py::test_ruff_format_check_dirty_count_matches_the_pinned_baseline PASSED [ 71%]
tests/test_lint_gate.py::test_mypy_strict_is_clean_over_src_fleet PASSED [ 85%]
tests/test_lint_gate.py::test_uv_sync_frozen_is_exit_0_offline_on_py312 PASSED [100%]

7 passed, 1 warning in 11.07s
```

(The one warning is `test_ruff_resolves_only_this_projects_own_files`'s own pre-existing,
disclosed scope-check limitation in a fresh detached worktree — unrelated to this fix.)

Re-measured whole-repo dirty count directly after the fix:
`ruff format --check --no-cache --output-format=concise .` →
**125 files would be reformatted, 259 files already formatted** — back to the pinned baseline.

## Commit

Single commit on `agent/roundviii-ruff-drift-2` (from `main` at `d9c4479`). Not merged to `main`,
not pushed.
