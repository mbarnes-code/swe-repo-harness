# Round O — SPEC §12 criteria closure

**Spec authority:** `docs/SPEC.md` §12 is binding. `docs/CRITERIA_PLAN.md` is the closure
backlog. `CLAUDE.md` Rules 1-14 bind every task.

**Why these three, why now (2026-08-30):** re-verified against current `HEAD` (`8a7a06e`, post
round M/N, full suite confirmed green: 1972 passed) immediately before writing this plan. Three
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

## Task 1: Test cache-key tamper detection (§12.44)

**Criterion:** SPEC.md §12 item 44 (cache not poisoned across backends) — the tamper-detection
sub-clause. Full done bar: `docs/CRITERIA_PLAN.md` §12.44 entry.

**Current state (verified 2026-08-30):** `src/fleet/util/hashing.py:32` defines
`cache_key(*parts: str) -> str`, the real function that computes the `llm_cache.cache_key` column
(`state/schema.sql:492`, `sha256(role|tier|backend|model_id|effort|...)`). No test in
`tests/test_llm_cache.py` (confirmed via grep — zero references to `hashing.cache_key` or a
tamper-style test) recomputes `cache_key` from a *persisted row's own columns* and compares it to
the stored key. The existing coverage this criterion's own audit found only compares two freshly
*computed* keys against each other, never against a stored row — so a corrupted/tampered stored
row would not be caught.

**Task:** Read `src/fleet/util/hashing.py::cache_key` and `state/schema.sql`'s `llm_cache` table
definition (the column list `cache_key` is computed from). Write a test in `tests/test_llm_cache.py`
that: inserts (or uses an existing fixture helper to insert) a real `llm_cache` row, then
recomputes `cache_key` from that row's own stored column values using the real `cache_key()`
function, and asserts it matches the stored `cache_key`. Then mutate one component (e.g. tamper
with the stored `model_id` in the row directly) and assert the recomputed key now differs from
the stored one — this is what "tamper detection" means concretely: the recomputation must be
sensitive to the exact columns the SPEC criterion names.

**Report:** DONE / DONE_WITH_CONCERNS / NEEDS_CONTEXT / BLOCKED, commits, one-line test summary,
concerns.

---

## Task 2: Test determinism — clean re-run, byte-identical digest (§12.21)

**Criterion:** SPEC.md §12 item 21 (determinism). Full done bar: `docs/CRITERIA_PLAN.md` §12.21
entry.

**Current state (verified 2026-08-30):** `tests/test_cli.py:1099`,
`test_status_digest_is_the_run_equivalence_proof`, exists but only checks the digest's *shape*
(64 hex chars) — it does not run twice from a clean DB under `--llm-cache read-only` and assert
the digest is byte-identical across both runs. `--llm-cache read-only` hard-fail-on-cache-miss IS
already covered elsewhere (do not re-test that — this task is specifically the stability/
byte-identical claim). The `--llm-cache` flag is defined around `cli.py:823`, modes at
`cli.py:426-491`.

**Task:** Read `test_status_digest_is_the_run_equivalence_proof` in full to understand the
existing fixture/harness pattern it uses. Write a new test (or extend that one, your call — prefer
a new test if extending would make the existing one do two unrelated things) that: runs the
harness once from a clean DB with a *warm* LLM cache (so `--llm-cache read-only` can succeed
without hitting a real provider — look at how existing tests seed a warm cache, likely via
`tests/fixtures/llm/` or an in-test cache-priming helper), captures the `status` digest; wipes the
DB and re-runs identically under `--llm-cache read-only`; asserts the second run's digest is
byte-identical to the first.

**Report:** DONE / DONE_WITH_CONCERNS / NEEDS_CONTEXT / BLOCKED, commits, one-line test summary,
concerns. If seeding a warm cache cleanly for a from-scratch second run turns out to require
non-trivial harness plumbing beyond what an existing fixture already provides, STOP and report
BLOCKED rather than build new test infrastructure — that would exceed this task's TEST-ONLY scope.

---

## Task 3: Positive fixtures for submodule-bearing and LFS-bearing repos (§12.26)

**Criterion:** SPEC.md §12 item 26 (preflight gates rather than crashes) — the submodule/LFS
sub-clause. Full done bar: `docs/CRITERIA_PLAN.md` §12.26 entry.

**Current state (verified 2026-08-30):** `tests/test_scan_e2e.py` has a real, working pattern for
the empty-repo case (`test_an_empty_repo_is_skipped_with_a_finding_and_the_fleet_continues`,
line 302) — a real Git repo fixture with no commits, driven through real preflight, asserting
`SKIPPED`+`EmptyRepo`. No equivalent exists for a repo containing a real git submodule or real
Git LFS pointers (confirmed: `grep -rl 'submodule "' tests/` and `grep -rl 'filter=lfs' tests/`
both return nothing).

**Task:** Read `test_an_empty_repo_is_skipped_with_a_finding_and_the_fleet_continues` in full as
your template for how this file builds and drives a real preflight fixture. Build one fixture
repo with a real `.gitmodules` file and a real submodule reference (you don't need the submodule's
target to be clonable/resolvable — the point is preflight must gate cleanly on the *presence* of a
submodule reference, not crash), and one fixture repo with a real `.gitattributes` LFS filter
declaration and at least one LFS pointer file (a real Git LFS pointer file's content is a small
known text format — you don't need the actual LFS binary content, a real pointer file is
sufficient to trigger the presence check). Drive each through real preflight and assert it gates
cleanly (whatever specific status/finding this codebase's preflight logic actually produces for
these cases — read `workers/clone.py`'s preflight gate logic first to know what to assert, don't
guess the expected finding name).

**Report:** DONE / DONE_WITH_CONCERNS / NEEDS_CONTEXT / BLOCKED, commits, one-line test summary,
concerns. If `workers/clone.py`'s actual behavior for submodules/LFS turns out to be a crash
rather than a graceful gate (i.e., the property this criterion assumes does NOT actually hold),
STOP and report BLOCKED with what you found — do not paper over a real gap with a weakened
assertion.
