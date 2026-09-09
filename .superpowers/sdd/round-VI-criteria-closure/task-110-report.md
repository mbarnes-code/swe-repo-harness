# Task 110 report — hotfix: repoint 6 drifted citations, `main` was red

Branch: `agent/roundvi-task110` (off `main` @ `4ea9726`). Single commit: `800fbc6`.

## What was wrong

`tests/test_integration_honesty_citations.py` had 3 failing tests on `main`:
- `test_no_unpinned_anchored_citation_fails_to_resolve[INTEGRATION_HONESTY]` (4 unresolved
  anchored citations)
- `test_no_unpinned_anchored_citation_fails_to_resolve[CRITERIA_PLAN]` (2 unresolved anchored
  citations)
- `test_every_census_number_this_module_states_is_the_number_it_derives` (stated 56, survey
  derived 60 at brief-writing time)

Ran the failing tests first (per instructions), confirmed the brief's 6-site list matched the
current tree exactly (same target real-definition spans reported by the test itself), then fixed
each.

## The 6 citations, before -> after

All re-derived by reading the actual current source (`ast`-based span, cross-checked against
`git show <commit>:<file>` at the citation's last-known-correct commit), never by trusting the
brief's own numbers or a naive offset add.

1. **`docs/INTEGRATION_HONESTY.md:7704`** — `` `_TransformClaimHook` `` `cli.py:5703` → `cli.py:5814`
   (+109). Confirmed **pure insertion**: `diff` of the class body at the task-96 commit (`6256fd9`)
   vs. `HEAD` is byte-identical (`diff` exit 0). Shift traced to two commits between task 96 and
   `HEAD` that touch `src/fleet/cli.py` above this class: round VI task 109 (`dc37da0`, +9 lines)
   and round VI task 107 (`121665e`, +100 lines) — verified by checking the class's line number in
   each intermediate commit's tree.
2. **`docs/INTEGRATION_HONESTY.md:9253`** — `` `cli._committed_contracts` `` `cli.py:2569-2606` →
   `cli.py:2678-2715` (+109, same two contributing commits as #1, same file region). Confirmed pure
   insertion the same way (function body byte-identical at both endpoints).
3. **`docs/INTEGRATION_HONESTY.md:9883`** — `` `BaselineBuild.enabled` `` `settings.py:282` →
   `settings.py:308` (+26). Traced via `git blame`: round VI task 107 (`121665e`) grew
   `BaselineBuild`'s class docstring from 1 line to 27 lines — a pure insertion above the
   `enabled` field, confirmed against the original task-54 commit (`7e31a49`) where the field sat
   at exactly `:282` with a 1-line docstring above it.
4. **`docs/INTEGRATION_HONESTY.md:10904`** — `` `_run_one_revalidation_task` `` `cli.py:14038` →
   `cli.py:14285` (+245). **Disclosed as NOT a pure insertion at the function level** — unlike the
   other 5 sites, this function's own AST span *grew* (357 → 463 lines) because round VI task 109
   (`dc37da0`) added a new `run_ctx: RunContext` parameter and a `run_ctx.ledger`/`SpendScope`
   dispatch (ADR-0136/D135), documented in the function's own updated docstring. I did not use the
   "pure insertion" wording here since it would be false; I instead confirmed the one thing this
   single-line citation actually needs — the cited `async def` line's own text is unchanged — and
   said so plainly, naming all 4 contributing tasks (103, 106, 109, 107) whose commits collectively
   moved this function down the file.
5. **`docs/CRITERIA_PLAN.md:1406`** — `` `validate_memory_budget` `` `settings.py:1323-1335` →
   `settings.py:1360-1372` (+37). Same root cause as #3 (task 107's `BaselineBuild` docstring
   growth plus other task-107 additions before this function) — confirmed pure insertion via
   line-for-line diff of the function body at the task-73 commit (`7690bb7`) vs. `HEAD`. This site
   pre-existed in an older, non-"repointed +N" wording style ("moved by task 73's ... and before
   that by task 58's ..."); per the brief's explicit instruction to follow the `repointed +N` /
   `pure insertion` / `superseded, not deleted` convention, I converted it to that form while
   preserving the prior citation's own text (including its two-generation history) inside the new
   "superseded" clause, rather than deleting it.
6. **`docs/CRITERIA_PLAN.md:3008`** — `` `_run_one_revalidation_task` `` `cli.py:14040` →
   `cli.py:14285` (+245). Same function/shift as #4 (different citing sentence in a different
   doc), same disclosed not-pure-insertion wording.

For sites #1, #2, #3, #5 I dropped the second-generation history reference that existed in the
prior annotation (e.g. "round VI task 95's own repoint (`cli.py:5688`)") rather than nesting a
third generation — this mirrors the file's own existing precedent at
`docs/INTEGRATION_HONESTY.md:9254`, where a repoint-of-a-repoint keeps exactly one prior generation
visible and the file's full history remains recoverable via `git log`/`git blame` regardless.

## The census number (3rd failing test)

Per the brief: fix the 6 citations first, then re-measure. After the 6 repoints, I ran the
module's own survey directly (`build_survey` against the `INTEGRATION_HONESTY` profile) and the
derived anchored-unresolved count is **exactly 56** — matching what
`tests/test_integration_honesty_citations.py:54` already states. No edit to the test file was
needed or made; this is not a "which case applies" judgment call, it's a direct re-measurement
that came out matching.

## Verification

- `.venv/bin/python -m pytest tests/test_integration_honesty_citations.py -q` → **70 passed, 0
  failed** (was 67 passed / 3 failed on `main`).
- `.venv/bin/python -m ruff check tests/test_integration_honesty_citations.py` → clean (file was
  not touched — no edit was needed).
- `git diff --stat` on the commit: only `docs/CRITERIA_PLAN.md` and `docs/INTEGRATION_HONESTY.md`
  changed (31 insertions / 20 deletions total), and every hunk is confined to the named citation
  parentheticals — no other prose in either file was touched.

## Concerns for the controller (not fixed, out of scope per brief)

1. **`docs/INTEGRATION_HONESTY.md:9876`** and **`:9887`** contain two more raw, *unanchored*
   references to `settings.py:282` and `settings.py:280` respectively (in prose like
   "`BaselineBuild.enabled`'s pydantic default is `True`, `settings.py:282`)" and "`settings.py:280`'s
   `BaselineBuild` docstring"). These are not flagged by the citation-drift test (they don't match
   the anchored-citation regex — the symbol name isn't immediately adjacent to the parenthetical),
   but they are now numerically stale for the same reason item #3 was: `settings.py:282`/`:280` no
   longer point at what they once did after task 107's docstring growth. Left untouched per the
   brief's explicit scope boundary ("Do not touch any other citation in either file").
2. Item #4/#6's function (`_run_one_revalidation_task`) genuinely changed (new `run_ctx` parameter,
   new ledger-dispatch behavior from task 109/ADR-0136/D135) since the citing sentence's
   surrounding prose was last written. I spot-checked the one behavioral claim the surrounding
   sentence rests on — "`VerifyPipelineWorker`... never dispatches a phase-2/`apply_and_commit`-
   shaped step at all" — and it still holds in the current function body (only `VerifyPipelineWorker`
   is dispatched in that span; no `apply_and_commit` call appears). I did not do a full re-audit of
   the rest of that entry's claims against the now-larger function — that would be substantive
   content review, out of scope for this citation hotfix.

## Status

**DONE.**
