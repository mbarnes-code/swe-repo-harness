# Task Z1 — FilePatch.path / diff cross-check in check_diff

## STATUS: DONE (with one flagged limitation — see Concerns)

## Verification before change

- `check_diff` (`src/fleet/rewrite/apply.py`) validated containment only against
  `diff_paths(diff)` — the paths in the diff's own `---`/`+++` headers — and never compared
  `FilePatch.path`. Confirmed by reading the function; no `patch.path`/`declared_path`
  parameter existed anywhere in the module before this change.
- `ProposedFileEdit.path` and `.diff` (`src/fleet/llm/schemas.py:183-189`) are two independent,
  unconstrained model-supplied fields (no cross-field validator on `ProposedFileEdit`).
- `_as_patches` (`src/fleet/workers/rewrite.py:557-565`) lifts them 1:1 into
  `FilePatch(path=edit.path, diff=edit.diff, ...)` with no consistency check.
- By contrast, every **deterministic** producer of a `FilePatch` (`rewrite/pipeline.py:397-409`
  `_finish`, `rewrite/astgrep.py:109-114` `AstGrepRewriter.apply`) builds `patch.path` and the
  diff from the *same* local `path` variable passed into `make_unified_diff(path, ...)` — those
  are self-consistent by construction and were never actually at risk.
- Confirmed commit `9a7148c` (today) made `_record` append every landed `FilePatch.path` into
  `output.rewritten`, and `cli._transform_criterion` (`src/fleet/cli.py:4202-4224`, another
  agent's lane) probes exactly those paths — so a model-declared `path` that disagrees with its
  own diff means the file `git apply` actually writes goes unprobed while an untouched path gets
  falsely certified. `9a7148c`'s own message states "`check_diff` does not compare `patch.path`
  against unit" — this task closes that specific gap.
- Confirmed `check_diff` is called on both landing branches inside `workers/rewrite.py`
  (line 340, deterministic; line 701 via `_rejected_patch`, LLM repair) as well as by
  `apply_patch` in my own lane — but see Concerns below re: which of these actually got wired.

## The fix

Added an optional keyword-only `declared_path: str | None = None` to `check_diff`. When given,
it must satisfy `declared_path in diff_paths(diff)` (membership, not equality) or `check_diff`
returns the same kind of one-line rejection reason it already uses for a subtree escape — no new
failure mode, so it still routes through the existing `PATCH_REJECTED` path in
`workers/rewrite.py` wherever it's already wired to a `check_diff` call.

**Placement decision:** inside `check_diff`, not at the `FilePatch` construction site
(`_as_patches`). `check_diff` is the one function every landing path in `workers/rewrite.py`
already calls before `land_patches`/`git apply` runs (both the deterministic gate and
`_rejected_patch` for the LLM branch), so a check placed there travels for free to every current
and future caller that passes `declared_path`. A construction-site check in `_as_patches` (or a
schema validator on `ProposedFileEdit`/`FilePatch`) protects only that one call site and is also
explicitly out of my lane (`llm/schemas.py` and `workers/rewrite.py` are off-limits for this
task). I also wired `apply_patch` (my lane) to pass `declared_path=patch.path` — that is the
function whose post-apply probe (`rewrite/apply.py:358-368`) the task description calls out by
name, and it's the one call site I can both change and verify end-to-end.

**Multi-path reality check:** a single `FilePatch`/`ProposedFileEdit` is never observed to carry
a genuinely multi-file diff anywhere in `src/` or `tests/` today — `LlmPatchProposal.files`
composes multi-file changes as *several* `FilePatch` objects (`_rejected_patch`'s own docstring:
"a repair rung may propose several files in one call"), not as one multi-path diff string. But
`ProposedFileEdit.diff` has no schema constraint forcing it to be single-file, so I used
membership (`in`) rather than equality on principle and pinned it with a synthetic multi-file
diff test — the shape the schema permits, not one the pipeline currently produces.

**Rename handling:** `FileDiff.path` (the thing `diff_paths` returns) already resolves to the
POST-image path for a rename (`new_path` unless it's `/dev/null`), per its own docstring: "what
gets written". So `declared_path in diff_paths(diff)` correctly accepts a rename whose declared
path is the destination and correctly rejects one declared as the stale source path. Verified
with a synthetic `--- a/pkg/old.py` / `+++ b/pkg/new.py` diff.

## Tests (tests/test_rewrite.py)

Added 6 tests, all passing:
- `test_check_diff_rejects_a_declared_path_that_disagrees_with_its_own_diff` — the core gap.
- `test_check_diff_accepts_a_declared_path_that_matches_its_own_diff` — the self-consistent
  shape every deterministic call site actually produces; proves no over-tightening.
- `test_check_diff_accepts_a_rename_diffs_destination_as_the_declared_path` — proves the naive
  `path == the only diff path` version would have been wrong, and this one isn't.
- `test_check_diff_accepts_any_path_a_multi_file_diff_actually_writes` — proves membership was
  the right call, not equality, against the schema-permitted (if unobserved) multi-file shape.
- `test_apply_patch_rejects_a_filepatch_whose_declared_path_disagrees_with_its_diff` — end-to-end
  through `apply_patch` against a real repo/`git apply`: mismatched patch is rejected and the
  file its diff would have written is left on disk untouched (`a\n`, not `b\n`); a matching patch
  still lands normally.

## Verification run

- `pytest tests/test_rewrite.py -q` → 45 passed (single session, no `FLEET_*` exported).
- `mypy --strict src/fleet` → Success: no issues found in 107 source files.
- `ruff check src/fleet/rewrite/apply.py tests/test_rewrite.py` → All checks passed.

## Concerns

1. **The real production landing path for the LLM repair branch is not yet closed, and I cannot
   close it from this lane.** `workers/rewrite.py`'s two `check_diff(...)` calls (line 340 and
   `_rejected_patch` line 701) both pass only `patch.diff` — neither passes the new
   `declared_path` parameter, because I'm not permitted to edit that file for this task. Backward
   compatibility means adding the parameter as optional-with-default couldn't have been made
   mandatory without breaking those call sites (and `mypy --strict` / the file itself are off
   limits), so today, right after this change, `check_diff` inside `workers/rewrite.py` still
   performs no path/diff cross-check — the gap as filed remains open on the path that actually
   lands `output.rewritten` entries. What I've done: (a) built the capability into the one
   chokepoint every landing branch already calls, so wiring it up is a one-line change at each of
   those two call sites (`declared_path=patch.path`), and (b) proven it end-to-end through
   `apply_patch`, the other real caller. **Someone needs a follow-up task in the
   `workers/rewrite.py` lane to add `declared_path=patch.path` to both `check_diff` calls** —
   otherwise this fix is inert on the path the reviewer actually flagged.
2. `apply_patch` itself does not appear to be called from anywhere in `src/` today (only
   exported and exercised by tests) — the real landing path uses `land_patches` →
   `apply_and_commit` (`vcs/commits.py`), which applies a concatenated patch file directly via
   `git.apply_check`/`git.apply` and never calls `check_diff` with a `FilePatch` in hand. That
   reinforces concern 1: my `apply_patch` wiring proves the check is correct, but isn't itself on
   the hot path.
3. No legitimate patch shape was rejected by the rule as shipped — rename-destination and
   (hypothetical) multi-file-diff both pass, verified by tests 3 and 4 above.
4. `validate_diff` was deliberately left untouched — it's diff-only (no `FilePatch`/declared-path
   concept), unused by any production caller (grep confirms only a test references it), and nothing
   in the task's failure chain runs through it.
