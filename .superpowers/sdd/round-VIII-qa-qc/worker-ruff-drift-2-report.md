# Report: close ruff-format pinned-baseline drift (125 -> 126), round VIII Wave 13

## Status: DONE (as amended below). Pin is now **122**;
`tests/test_lint_gate.py::test_ruff_format_check_dirty_count_matches_the_pinned_baseline` passes,
measured AFTER the final commit lands.

## Amendment (same branch, follow-up commit, round VIII Wave 14 review response)

Task review (`review-ruff-drift-2-report.md`) found the original commit (`d4e5db1`) did not
actually restore the pin: the fix reformatted `worker-citation-drift-fix-report.md`, but that
SAME commit also added this report's own "Root cause" section, which quotes the pre-fix
unformatted snippet verbatim inside a ` ```python ` fence — `ruff format` treats that fence like
any other, so this report file itself went dirty and net-cancelled the fix (126, not 125). The
"7 passed" claim below was measured before this report's final "Root cause" content (with the
quotation) was written to disk and committed.

The coordinator directed a durable structural fix rather than a third round of one-file
reformatting: add `.superpowers/` to `pyproject.toml`'s `[tool.ruff]` `exclude` (round-ledger
coordination scratch, never project source — controller ruling, not re-litigated here).

**This changes the correct pin value, measured, not assumed.** The 3 ADR-0116-permanently-
excluded historical-quotation files (`.superpowers/sdd/round-VI-criteria-closure/
task-{78,80,105}-report.md`) also live under `.superpowers/`, so excluding the whole directory
removes them from `ruff format`'s scan entirely too — they were part of the prior 125 pin's dirty
count. Re-measured directly: `ruff format --check --no-cache --output-format=concise .` →
**122**, not 125. `comm`-diffed the post-exclude per-file dirty list against the prior 125-file
list in both directions: **zero newly-dirty, exactly those 3 files newly-clean-by-exclusion, no
other file's status changed.** 125 - 3 = 122 exactly. The coordinator's instruction expected "back
to 125" — that expectation did not hold once the exclude's own scope effect on the 3 pre-existing
excluded files was measured; reporting the actual number rather than forcing the expected one.

Changes in the follow-up commit:
- `pyproject.toml`: added `exclude = [".superpowers"]` under `[tool.ruff]`, with a comment
  explaining why.
- `tests/test_lint_gate.py`: appended a dated correction to the comment trail above
  `_RUFF_FORMAT_DIRTY_BASELINE` (annotate-never-rewrite convention) and changed the pin from
  `125` to `122`.
- `docs/DECISIONS.md`: appended a second dated addendum to ADR-0116 recording this scope change
  and the corrected pin value.
- This report: this amendment section.

Verification performed AFTER the follow-up commit landed (not before, per the review's own
finding about the prior commit):
- `ruff format --check --no-cache .` → 122 dirty / 227 clean (matches the new pin).
- `ruff check --no-cache .` → All checks passed!
- `mypy --strict src/fleet/` → Success: no issues found in 132 source files.
- `pytest tests/test_lint_gate.py -q`, run fresh with the worktree at the follow-up commit's
  `HEAD` (no uncommitted changes at the time of the run) → all passed, see the "Test output"
  section below (updated).

## Original fix (Wave 13), superseded in count by the amendment above but still correct in kind

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

## Test output confirming the pin passes (Wave 13, at commit `d4e5db1`)

**Annotated 2026-09-13, per the annotate-never-rewrite convention: this run does NOT reproduce at
`d4e5db1` and its "back to the pinned baseline" conclusion is WRONG, as the Wave 14 review
(`review-ruff-drift-2-report.md`) found.** It was measured before this report's own "Root cause"
section (with the unformatted-snippet quotation) was in its final form on disk, and that section
made the report itself newly dirty in the same commit, net-cancelling the fix (126, not 125).
Left in place as a historical record rather than edited; see the Amendment section above and the
fresh, correct measurement below for what actually holds at the follow-up commit.

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

Re-measured whole-repo dirty count directly after the fix (also stale, see annotation above):
`ruff format --check --no-cache --output-format=concise .` →
"125 files would be reformatted, 259 files already formatted" — did not reproduce at the
committed `HEAD`; the review's independent re-measurement got 126, matching the review's own
diagnosis.

## Test output confirming the pin passes (Wave 14 follow-up, at the follow-up commit — the
current, correct measurement)

Measured fresh, AFTER the follow-up commit (adding `.superpowers/` to `pyproject.toml`'s ruff
exclude and re-pinning to 122) landed — no uncommitted changes in the worktree at the time of this
run:

```
$ ruff format --check --no-cache --output-format=concise .
...
122 files would be reformatted, 227 files already formatted

$ ruff check --no-cache .
All checks passed!

$ mypy --strict src/fleet/
Success: no issues found in 132 source files

$ pytest tests/test_lint_gate.py -q
.......                                                                 [100%]
7 passed, 1 warning in <measured wall time>
```

(Exact pytest wall-clock time captured in the shell history for this run; the pass/fail outcome
and count, not the timing, are what this report certifies. See the follow-up commit message and
the Amendment section above for the full reasoning.)

## Commit

Two commits on `agent/roundviii-ruff-drift-2` (from `main` at `d9c4479`):
1. `d4e5db1` — Wave 13 fix (reformatted `worker-citation-drift-fix-report.md`; incomplete, per the
   Wave 14 review).
2. Follow-up commit (Wave 14 review response) — `pyproject.toml` `.superpowers/` exclude,
   `tests/test_lint_gate.py` pin correction to 122, `docs/DECISIONS.md` ADR-0116 second addendum,
   this report's amendment.

Not merged to `main`, not pushed.
