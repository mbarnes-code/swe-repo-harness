# Round S — SPEC §12 criteria closure

**Spec authority:** `docs/SPEC.md` §12 is binding. `docs/CRITERIA_PLAN.md` is the closure
backlog. `CLAUDE.md` Rules 1-14 bind every task.

**Why these three, why now (2026-08-31):** re-verified against current `HEAD` (`3c7a9d0`, post
round R, full suite confirmed green: 1999 passed). Three TEST-ONLY items, zero file overlap with
each other (confirmed by grep before writing this plan: none of `uv sync`, `new_cpu_pool`, or the
re-scan idempotency test currently exist in any of the three target files' counterparts).

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

## Task 1: `uv sync --frozen` offline exit-0 test (§12.1)

**Criterion:** SPEC.md §12 item 1. Full done bar: `docs/CRITERIA_PLAN.md` §1 entry.

**Current state (verified 2026-08-31):** zero references to `uv sync` anywhere in `tests/`
(confirmed by grep). Every existing `uv` reference drives the Python ecosystem *adapter* running
`uv pip compile` against target repos, never `uv sync` on this harness's own environment.

**Task:** Read `tests/test_lint_gate.py` in full — it's your style precedent for how this file
shells subprocess commands, resolves tool paths, and asserts exit codes (it gained exactly this
shape for `ruff`/`mypy` in round R; add `uv sync --frozen` alongside them, matching the same
resolution/invocation conventions). Add one test that shells `uv sync --frozen` against the
committed `uv.lock`, with the subprocess environment configured so networking is disabled (check
how other tests in this codebase disable network access for a subprocess — if no existing
precedent exists, use `uv`'s own `--offline` flag or an environment variable it documents, and
cite which you used and why), and asserts exit code 0. Add a second assertion (can be the same
test or a separate one) that `sys.version_info[:2] == (3, 12)`, matching the criterion's second
clause. Confirm your test does NOT mutate the shared `.venv` — read `uv sync --frozen`'s actual
semantics against an already-synced environment before assuming it's a no-op; if it would need to
run against a throwaway environment instead of the shared one to be safe, use a throwaway one and
say so.

**Report:** DONE / DONE_WITH_CONCERNS / NEEDS_CONTEXT / BLOCKED, commits, one-line test summary,
concerns.

---

## Task 2: Idempotent re-scan (remaining 2 tables) + 9-repo vendored-contract fixture (§12.23)

**Criterion:** SPEC.md §12 item 23 (idempotency — re-scan, re-transform). Full done bar:
`docs/CRITERIA_PLAN.md` §23 entry.

**Current state (verified 2026-08-31):** re-transform idempotency is fully covered. Re-scan
idempotency exists in `tests/test_scan_e2e.py` (confirmed by grep for re-scan-shaped tests) but
per `docs/CRITERIA_PLAN.md` §23 covers only 6 of 8 named tables — read `docs/SPEC.md`'s exact
item-23 wording to find the full list of 8 named tables, then read the existing re-scan test in
`tests/test_scan_e2e.py` to see which 6 it already asserts idempotent, and identify the 2 missing
ones by diffing against SPEC's list. No 9-repo vendored-contract fixture exists anywhere
(confirmed by grep for a 9-repo-scale fixture).

**Task:** Read the existing re-scan idempotency test(s) in `tests/test_scan_e2e.py` in full as
your fixture precedent. Extend the assertion to cover the 2 missing tables (read each table's
schema in `state/schema.sql` to know what "idempotent" means for it — e.g. row count unchanged,
or specific column values unchanged, across two identical scans). Separately, build the 9-repo
vendored-contract fixture SPEC's item 23 names (read the exact wording — it likely describes N
repos each vendoring a copy of the same contract, testing that re-scanning doesn't duplicate or
diverge the contract's `ContractNode`) and drive it through a real re-scan, asserting the
idempotency property SPEC describes.

**Report:** DONE / DONE_WITH_CONCERNS / NEEDS_CONTEXT / BLOCKED, commits, one-line test summary,
concerns. If the 9-repo fixture turns out to require infrastructure beyond what this file already
provides (e.g., a new fixture-generation helper of significant size), that's still in scope — this
criterion is explicitly SCALE-FIXTURE tier — but if you hit a genuine design ambiguity about what
"vendored-contract" means precisely, stop and report BLOCKED rather than guess.

---

## Task 3: Process-pool has no DB handle + closer-to-scale coroutine test (§12.28)

**Criterion:** SPEC.md §12 item 28 (single writer, pool children have no DB handle). Full done
bar: `docs/CRITERIA_PLAN.md` §28 entry.

**Current state (verified 2026-08-31):** the single-writer half is covered. `new_cpu_pool` in
`src/fleet/orchestrator/budgets.py:933-949` constructs a `ProcessPoolExecutor` passing only
`max_workers=`/`mp_context=` (no DB-handle-bearing kwarg) — true in the tree today by direct
inspection, but zero tests reference `new_cpu_pool` at all (confirmed by grep). The
200-repo-scale zero-`SQLITE_BUSY` property is untested at that scale; the largest existing
concurrency test in this codebase is 50 coroutines.

**Task:** Read `new_cpu_pool` (`src/fleet/orchestrator/budgets.py:933-949`) in full, and read
`tests/test_budgets.py`'s existing style precedent (it's the file most of `budgets.py`'s other
tests live in). Write one test that calls `new_cpu_pool` (or inspects its call to
`ProcessPoolExecutor` via a mock/spy — your call on the cleanest approach matching this codebase's
existing testing conventions) and asserts the actual keyword arguments passed contain no
DB-handle-bearing value (no `initializer`/`initargs` carrying a connection, no shared file handle
kwarg — read what the criterion's evidence clause specifically requires before deciding exactly
what to assert). Separately, find the existing ~50-coroutine concurrency test in this file (or
wherever the SQLITE_BUSY-avoidance property is tested) and extend it toward the criterion's
stated 200-repo scale — if 200 real coroutines is impractical (timeout, resource cost), you may
use a smaller number, but state the number you chose, why 200 wasn't used, and why your chosen
number is still a genuine test of the same property (not merely "smaller and hoping it still
proves something") — this is a disclosed adjudication, not a silent narrowing.

**Report:** DONE / DONE_WITH_CONCERNS / NEEDS_CONTEXT / BLOCKED, commits, one-line test summary,
concerns.
