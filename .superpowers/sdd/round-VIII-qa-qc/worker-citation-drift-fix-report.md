# Report: citation-drift fix (D137 regression) — round VIII, Wave 10

## Status: DONE. All 70 tests in `tests/test_integration_honesty_citations.py` pass.

Branch: `agent/roundviii-citation-drift-fix` (from `main` at `c85de57`), in worktree
`/home/redmage/swe repo harness worktrees/wt-roundviii-citation-drift-fix`. Not merged, not pushed.

## Root cause (confirmed independently)

Commit `b76eb94` (`fix(D137): RewriteWorker._repair() scopes the llm_cache client per rung
(ADR-0021)`) inserted code into `src/fleet/workers/rewrite.py` (+2 lines before
`pipeline_for`, from two new imports) and `src/fleet/llm/cache.py` (+18 lines before
`CachingModelClient`, from a new `ScopedModelClient` Protocol class), shifting both symbols
downward. Two anchored citations in `docs/INTEGRATION_HONESTY.md` were not repointed.

## Fix: exact old/new line ranges

1. `docs/INTEGRATION_HONESTY.md:1350` (D20 entry)
   - Citation: `` `RewriteWorker.pipeline_for` (`workers/rewrite.py:370`) ``
   - Verified `pipeline_for`'s def line pre-`b76eb94` was 371 (`git show b76eb94^:...`), so the
     old citation was already off-by-one from the def line (pointing at the blank line above it,
     matching this doc's convention of citing one line above `def` for some entries) — not new
     drift, just the base that then drifted.
   - Current def line (verified via `grep -n "def pipeline_for" src/fleet/workers/rewrite.py`):
     **373**. Logical span reported by the test's own AST resolver: `(372, 388)`.
   - New citation: `workers/rewrite.py:373` — points at the `def` line itself, contained in
     `(372, 388)`.

2. `docs/INTEGRATION_HONESTY.md:11467` (inside the D137 entry itself — the fix's own citation of
   the mechanism it wired)
   - Citation: `` `CachingModelClient.scoped()` (`llm/cache.py:436-463`) ``
   - Verified there are two `def scoped` in `src/fleet/llm/cache.py`: one on the
     `ScopedModelClient` Protocol (line 397, new in this same commit) and one on
     `CachingModelClient` (line 455) — disambiguated by checking `class` boundaries
     (`CachingModelClient` starts at line 406, so line 455 is the one inside it; line 397 belongs
     to the Protocol at line 388).
   - Current method span (verified via `Read`): body lines 455–482, matching the test's own
     "defined at [(454, 482)]" (454 is the blank line immediately preceding `def scoped`).
   - New citation: `llm/cache.py:455-482` — same 28-line width as the original citation
     (`436-463`), now pointing at the method's actual body, contained in `(454, 482)`.

## Census number: re-derived from scratch, not incremented

The census sentence lives in **`tests/test_integration_honesty_citations.py:54`** itself (its own
module docstring), not in `docs/INTEGRATION_HONESTY.md`'s prose — I checked this directly by
reading the failing assertion's file:line (`test_integration_honesty_citations.py:54`) rather than
assuming the brief's "docs/INTEGRATION_HONESTY.md's own prose" framing was the literal location.
The sentence: *"every resolvable citation is in range and 56 anchored citations are nonetheless
unresolved"*.

**Predicate and method used**, independent of trusting pytest's own pass/fail: I imported
`tests/test_integration_honesty_citations.py` as a module in a throwaway script, called its own
`build_survey(root, profile)` for the `INTEGRATION_HONESTY` profile directly (the same function
`surveys()` fixture calls), and computed:

```python
unresolved = [a for a in survey.anchored if not a.resolves]
len(survey.anchored)  # -> 87
len(unresolved)  # -> 56
```

This is the same quantity `test_every_census_number_this_module_states_is_the_number_it_derives`
checks (`derived = sum(1 for a in surveys[profile.name].anchored if not a.resolves)`), but computed
by me directly against the survey object rather than reading it off a passing assertion. **Result:
56 — the number already in the prose is correct after the two citation fixes; no edit to the
census sentence was needed.** (Before the fix, this was 58: the pre-existing 56 legitimately-pinned
unresolved anchored citations plus the 2 D137-caused drift citations, which were unpinned and thus
also counted by `derived` even though they were failing a *different* assertion for a different
reason.) I printed all 56 unresolved citations and cross-checked their `key`s against
`profile.pins` — every one of the 56 is a declared pin (`test_each_pinned_citation_is_still_unresolved`
covers all 56 as parametrized cases, all passing), confirming none of the two fixed citations
belongs to that set and none of the 56 is accidentally unpinned drift.

## Broader sweep for the same class of drift

`test_no_unpinned_anchored_citation_fails_to_resolve` parses the **entire** document text
(`ledger_text = (survey.root / profile.doc_rel).read_text(...)`) for the anchored-citation shape,
not a specific section — so the full pytest run before my fix (2 failures, and only those 2) is
already a comprehensive sweep of every *anchored*-format citation in the whole 11.5k-line document,
not just the two named in the brief. I confirmed this by re-deriving the survey directly (above)
and inspecting every citation, not just trusting the earlier pytest summary line.

**One additional, pre-existing drift found, NOT caused by D137 and NOT fixed here (out of
scope):** `docs/INTEGRATION_HONESTY.md:1352` states *"The only `probe=` in `src/` is
`workers/rewrite.py:456`"*. This is a **pathed**, not anchored, citation (no backtick-identifier
immediately precedes it), so it is checked only for "file exists" and "line is within file
bounds" — both trivially still true in a file that has only grown — and is invisible to the
anchored-resolution test. I verified independently:
- `grep -rn "probe=" src/` today shows the sole occurrence at `workers/rewrite.py:747`, not 456.
- This was **already wrong before `b76eb94`**: `git show b76eb94^:src/fleet/workers/rewrite.py |
  grep -n "probe=probe"` shows it was at line **713** in the pre-D137 tree, not 456. `git log -S`
  on the citation text shows it has read `:456` since the initial commit (`a1178f7`) and was never
  updated as the surrounding code grew.
- This is a different, older defect (wrong citation format entirely for a genuinely useful line
  reference, present since project inception) unrelated to this round's D137 regression, so per
  CLAUDE.md's Rule 3 ("Surgical Changes — touch only what you must") and the brief's scope (drift
  "that this round's D137 fix may have shifted"), I did not fix it. Flagging it here for the
  controller to decide whether it merits its own D-number/task.

No other citation referencing `rewrite.py` or `cache.py` anywhere in the document (checked via
`grep -n "rewrite\.py\|cache\.py" docs/INTEGRATION_HONESTY.md`, ~70 hits) is in the anchored,
currently-unpinned-and-unresolved state — every other hit is either a pathed citation, a
commit-bound (`Measured at <sha>:`) historical record (correctly exempt), or a pinned anchored
citation already covered by the passing parametrized pin test.

## Test results

```
$ .venv/bin/python -m pytest tests/test_integration_honesty_citations.py -q
......................................................................   [100%]
70 passed in 1.21s
```

`git diff --stat`: only `docs/INTEGRATION_HONESTY.md` changed (2 lines), no `src/` or `tests/`
changes — mypy/ruff not run since nothing executable was touched.

## Commit

Branch `agent/roundviii-citation-drift-fix`, one commit on top of `main` (`c85de57`). Not merged,
not pushed.
