# Round N — SPEC §12 criteria closure

**Spec authority:** `docs/SPEC.md` §12 (Success Criteria) is binding. `docs/CRITERIA_PLAN.md`
is the closure backlog. `CLAUDE.md` Rules 1-14 and the "Architectural & Subagent Guardrails"
section bind every task.

**Why these three, why now (2026-08-30):** round M closed 4 of 48 criteria and landed three
WIRING-tier tasks. These are the next tier: TEST-ONLY criteria (an already-correct property that
simply has no test proving it), re-verified against current `HEAD` (`edc343c`) immediately before
this plan was written, chosen for zero file overlap with each other and with round M's landed
changes.

## Global Constraints

- Match this codebase's existing style exactly: mature, 40k+ line Python/asyncio/Pydantic-v2
  codebase, `mypy --strict`. Read the surrounding module before writing a line. No new
  abstractions beyond the task's literal scope (`CLAUDE.md` Rule 2, Rule 3).
- These are TEST-ONLY tasks: the property under test is already believed correct. Do NOT modify
  production code (`src/fleet/`) unless your investigation finds the property does NOT actually
  hold — if so, STOP and report BLOCKED with what you found rather than silently fixing it; a
  found-wrong property is a bigger deal than a missing test and needs controller attention.
- Every new test must be genuinely discriminating (`CLAUDE.md` Rule 12): write it so it fails
  against a plausible mutation of the property and passes against the real code. State the
  mutation you used to confirm this in your report.
- Run the exact test files you touch — whole files, no `-k` filter — before reporting DONE.
- You are in an isolated git worktree. Commit your work there. Do not merge/push — the controller
  integrates your branch. Never dispatch your own subagents, including a reviewer.
- If you hit a genuine ambiguity beyond "which exact assertion to write," stop and report BLOCKED.

---

## Task 1: Widen the forbidden-construct scope to all of `src/fleet/` (§12.5)

**Criterion:** `docs/SPEC.md` §12 item 5: "No forbidden constructs. `grep -rn "import pickle\|ThreadPoolExecutor\|subprocess.run" src/fleet/` returns nothing." Full done bar:
`docs/CRITERIA_PLAN.md` §12.5 entry.

**Current state (verified 2026-08-30):** `tests/test_proc.py::test_no_shell_anywhere_in_the_subprocess_boundary` exists but only scopes 3 files (`proc.py`, `sandbox/worktree.py`, `sandbox/container.py`), not the `src/fleet/` tree the criterion names. No test for `pickle` or `ThreadPoolExecutor` exists at all (both are used safely in test code, which is fine and out of scope — the criterion only forbids them in `src/fleet/`).

**Task:** Add or widen a test in `tests/test_proc.py` (or a new test if that file's existing test's scope/shape doesn't fit widening cleanly — your call, but prefer widening if it fits) that greps the literal command the criterion names — `import pickle`, `ThreadPoolExecutor`, `subprocess.run` — across all of `src/fleet/`, and asserts zero hits. Run it against the current tree first to confirm it actually passes today (the criterion's own premise is that the property already holds) before finalizing.

**Report:** DONE / DONE_WITH_CONCERNS / NEEDS_CONTEXT / BLOCKED, commits, one-line test summary, concerns.

---

## Task 2: Test `register_backend`'s duplicate-name refusal (§12.42)

**Criterion:** `docs/SPEC.md` §12 item 42 (new backend costs one file + one registry line — the duplicate-name sub-clause). Full done bar: `docs/CRITERIA_PLAN.md` §12.42 entry.

**Current state (verified 2026-08-30):** `src/fleet/llm/client.py` around line 377 defines `register_backend`, a decorator that raises `RuntimeError` on a duplicate backend name. Every existing reference to `register_backend` in `tests/` (`test_llm_backend_bedrock.py`, `test_llm_backend_vertex.py`, `test_llm_backend_anthropic.py`, `test_llm_backend_openai_compatible.py`) is a docstring mention or source-string parse — none actually constructs a duplicate to trigger the raise.

**Task:** Read `register_backend`'s implementation in `src/fleet/llm/client.py` in full. Write one test (in whichever existing `test_llm_backend_*.py` file fits best, or a new small test file if none fits — your call) that registers a backend with a name already claimed by a shipped backend (or two backends sharing a synthetic decoy name — check whether `register_backend` is easiest to exercise via a real shipped name collision or via two freshly-defined decoy classes; prefer whichever is less invasive to existing registry state) and asserts the `RuntimeError` is raised with the duplicate name in the message.

**Report:** DONE / DONE_WITH_CONCERNS / NEEDS_CONTEXT / BLOCKED, commits, one-line test summary, concerns.

---

## Task 3: Test the decoy-`Ecosystem`-member import-time raise (§12.32)

**Criterion:** `docs/SPEC.md` §12 item 32 (adapter registries total, delegation honest — the decoy-member sub-clause). Full done bar: `docs/CRITERIA_PLAN.md` §12.32 entry.

**Current state (verified 2026-08-30):** `src/fleet/ecosystems/base.py`'s `discover()` function (around line 622-656) raises `RuntimeError` at import/discovery time if any `Ecosystem` enum member has no registered adapter — "no `EcosystemAdapter` is registered for {missing}... the registry [must be] a total bijection over `Ecosystem`". This branch exists and is real, but `tests/test_ecosystems.py` has zero references to a "decoy" scenario (confirmed via `grep -rn "decoy" tests/test_ecosystems.py` returning nothing) — the branch is never triggered by any test.

**Task:** Read `discover()`'s full implementation in `src/fleet/ecosystems/base.py`. Write a test in `tests/test_ecosystems.py` that adds a decoy `Ecosystem` member with no corresponding registered adapter (you'll likely need to monkeypatch or construct a scoped variant of the `Ecosystem` enum / the registry rather than permanently modifying the real one — look at how nearby tests in this file handle similar registry-manipulation needs, e.g. the existing decoy-double-claim test mentioned in `docs/CRITERIA_PLAN.md`'s §12.32 entry, for the established pattern in this file) and asserts `discover()` raises `RuntimeError` naming the missing member.

**Report:** DONE / DONE_WITH_CONCERNS / NEEDS_CONTEXT / BLOCKED, commits, one-line test summary, concerns.
