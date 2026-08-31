# Round P — SPEC §12 criteria closure

**Spec authority:** `docs/SPEC.md` §12 is binding. `docs/CRITERIA_PLAN.md` is the closure
backlog. `CLAUDE.md` Rules 1-14 bind every task.

**Why these three, why now (2026-08-31):** re-verified against current `HEAD` (`1cdf823`, post
round M/N/O, full suite confirmed green: 1976 passed) immediately before writing this plan. Three
TEST-ONLY items, zero file overlap with each other.

## Global Constraints

- Match this codebase's existing style exactly (mature, 40k+ line Python/asyncio/Pydantic-v2,
  `mypy --strict`). No new abstractions beyond scope.
- TEST-ONLY: do not modify `src/fleet/` unless your investigation finds the property does NOT
  hold — if so, STOP and report BLOCKED with what you found.
- New tests must be genuinely discriminating (`CLAUDE.md` Rule 12) — state the mutation you used
  to confirm this.
- Run the exact test files you touch, whole-file, no `-k`, before reporting DONE.
- Isolated git worktree; commit there, do not merge/push, never dispatch subagents.
- Genuine ambiguity beyond "which assertion to write" → stop, report BLOCKED.

---

## Task 1: Fabricated reverse-disagreement fixture for git arbitration (§12.15)

**Criterion:** SPEC.md §12 item 15 (crash safety, Git is the arbiter) — the fabricated
reverse-disagreement sub-clause. Full done bar: `docs/CRITERIA_PLAN.md` §12.15 entry.

**Current state (verified 2026-08-31):** `tests/test_cli.py` has a well-established helper,
`_arbitration_worktree(workspace, repo_id="acme-commons")` (around line 3390), used by existing
tests proving §11.5 step 4's git arbitration: `test_resume_step4_adopts_the_commit_git_says_landed_without_charging_an_attempt`
(~3506) and `test_resume_step4_discards_the_worktree_of_a_task_whose_commit_never_landed` (~3547).
Those tests cover the *forward* direction (does `attempts.commit_sha` correctly get adopted or
discarded based on what Git actually shows). What's missing: the *reverse* direction — a
fabricated disagreement where `attempts.commit_sha` is hand-edited to point somewhere off the
real branch (simulating corruption, or a discrepancy nothing produced organically), and the
arbitration logic must still resolve it correctly by trusting Git over the SQLite pointer (per
SPEC §11.5's authority rule: "On any disagreement between SQLite and Git about whether a change
landed, Git is authoritative and the SQLite row is corrected. Never the reverse.").

**Task:** Read `_arbitration_worktree` and the two existing tests around it in full — they are
your pattern precedent, follow their fixture-construction style exactly. Build a new test that:
constructs the arbitration worktree as those tests do, then directly hand-edits (via raw SQL, not
through the normal write path) an `attempts.commit_sha` (or the relevant pointer column the
arbitration logic reads) to a commit SHA that is real but off the `migrate/<repo>` branch entirely
— not just "not yet landed" (the discard test's scenario) but pointing to unrelated, divergent
history. Drive `fleet resume` and assert the arbitration logic correctly falls back to what Git
actually shows for that repo/phase, per the SPEC's authority rule, and that the SQLite row is
corrected to match Git rather than the reverse.

**Report:** DONE / DONE_WITH_CONCERNS / NEEDS_CONTEXT / BLOCKED, commits, one-line test summary,
concerns.

---

## Task 2: Redaction coverage for `phases.last_error` and `llm_cache.response_json` (§12.20)

**Criterion:** SPEC.md §12 item 20 (secrets never leak). Full done bar: `docs/CRITERIA_PLAN.md`
§12.20 entry.

**Current state (verified 2026-08-31):** `src/fleet/obs/redact.py` defines `redact_text`/`redact`
(the redaction primitives, well-tested elsewhere). Two specific columns this criterion names have
no redaction test: `phases.last_error` (written at `src/fleet/state/repository.py:957`/`1386` and
`src/fleet/cli.py:4369`) and `llm_cache.response_json` (written at `src/fleet/llm/cache.py:308`).
Confirmed via grep: zero hits for either column name inside a redaction-focused test. The
PR-body `«redacted:…»` placeholder is also unverified per the criterion's done bar — check
whether it's now covered by round M's Task 3 work (`tests/test_pr_e2e.py`, which added redaction
coverage for a different leak) before treating it as still open; if it's already covered, narrow
your scope to just the two DB columns.

**Task:** Read `src/fleet/obs/redact.py` in full to understand the redaction primitives and their
existing test patterns (`tests/test_obs.py` has the established style — follow it). Construct a
fixture that plants a real secret shape (an API key pattern, a PAT, or similar — check
`redact.py`'s own patterns for what it's designed to catch) into a scenario that would write to
`phases.last_error` (e.g. an error message containing a credential), and a second scenario that
would write to `llm_cache.response_json` (a cached LLM response containing a credential-shaped
string). Drive each through the real write path and assert the persisted column value has been
redacted, not the raw secret.

**Report:** DONE / DONE_WITH_CONCERNS / NEEDS_CONTEXT / BLOCKED, commits, one-line test summary,
concerns.

---

## Task 3: Positive fixture for `trunk`-default-branch repos (§12.26, 5th case)

**Criterion:** SPEC.md §12 item 26's fifth named fixture category (default-branched to `trunk`,
not `main`). Full done bar: `docs/CRITERIA_PLAN.md` §12.26 entry (updated 2026-08-30 to name this
as the remaining gap after round O closed submodule/LFS).

**Current state (verified 2026-08-31):** Zero fixture repos anywhere in `tests/` use `trunk` as
their default branch name (confirmed via grep). `src/fleet/workers/clone.py`'s preflight logic
reads a repo's actual default branch dynamically (`default_branch`/`default_branch_source` fields,
set around lines 345-346, 419-426) rather than assuming `main` — so this should be a
straightforward fixture-construction task, not a design question, but read that logic in full
before writing the assertion rather than assuming what it does with a non-`main` name.

**Task:** Read `tests/test_scan_e2e.py`'s existing empty-repo/shallow-repo fixture tests (and
round O's newly-landed submodule/LFS tests, same file) as your pattern precedent for how this file
constructs and drives a real preflight fixture. Build one fixture repo whose default branch is
literally named `trunk` (not `main`) — a real git repo with commits, `git symbolic-ref HEAD
refs/heads/trunk` or equivalent, no submodule/LFS/emptiness involved, isolating this one variable.
Drive it through real preflight and assert it proceeds (or gates, matching the criterion's own
"either proceed or produce a `PreflightFailed` finding" wording) with `repos.default_branch ==
'trunk'` and the correct `default_branch_source` recorded.

**Report:** DONE / DONE_WITH_CONCERNS / NEEDS_CONTEXT / BLOCKED, commits, one-line test summary,
concerns.
