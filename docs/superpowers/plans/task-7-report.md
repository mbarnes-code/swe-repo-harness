# Task 7 report — Worker G

**Status: DONE.**

**`build_diagnosis` reader count: 0, independently established.** Re-ran `grep -rn "diagnosis"
src/` myself (not trusting research-36's count) and traced all three egress paths by hand:
`cli.py:4983-4989`/`:5480-5486` (`_handoff` field lists omit it), the sole
`checkpoints.save` (`orchestrator/runner.py:1005-1011`, persists only `PhaseCheckpoint`), and
`state/projection.py`/`models/state.py` (no `diagnosis` field). Only `response.usage`
(`buildverify.py:1050`) is consumed. Confirmed zero readers.

**Situation found: 2 — SPEC-mandated.** `docs/SPEC.md:1390-1392` (LLM slots list) and `:6266`
(`build_diagnosis: WORKHORSE` in the roles table) both name it as a shipped feature. Removing it
would desync code and SPEC, so per CLAUDE.md's ruling I **kept the code** and recorded the gap as
**D47** in `docs/INTEGRATION_HONESTY.md`. Also flagged (not fixed, per Rule 7): SPEC.md:1391-1392's
prose claims the model "proposes an edit, code applies it" — that describes `transform_repair`,
not `build_diagnosis` (`LlmBuildDiagnosis` has no diff field). Logged in ADR-0068
(`docs/DECISIONS.md`).

**`CacheMiss` bug: fixed.** It no longer subclasses `LlmError` (`src/fleet/llm/cache.py`), so the
bare `except LlmError:` degrade-and-continue pattern at every advice-call site (`buildverify.py`,
`buildgen.py` x2, `prwriter.py` x2 — none of which I touched) can no longer swallow it. It still
fails loud via `BaseWorker._run_one`'s existing `except Exception:` boundary
(`workers/base.py:897`), which classifies and records every escape. Verified this changes no
caller's depended-upon behavior (no `isinstance(x, LlmError)` anywhere; `.complete()` is only
called from worker `run()`; `classify.py` and `cli.py:489`'s funnel land on identical outcomes
before/after) — recorded in ADR-0068, so no report-before-change was needed.

**Files changed:**
- `src/fleet/llm/cache.py` — `CacheMiss(Exception)` instead of `CacheMiss(LlmError)`, docstring
  explains why; removed now-unused `LlmError` import.
- `tests/test_llm_cache.py` — new `test_cache_miss_is_not_an_llm_error`, pins the non-inheritance
  and that a bare `except LlmError:` no longer catches it.
- `docs/DECISIONS.md` — ADR-0068.
- `docs/INTEGRATION_HONESTY.md` — D47.

`buildverify.py` was not touched (owned by another worker). `ruff check src/ tests/` and
`mypy src/fleet/ --strict` both clean. `pytest` was not run per hard constraint.
