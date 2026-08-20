# SDD Backlog B — Unbuilt subsystems

Round focus: the ~13 acceptance criteria with **no implementation at all**. Four lanes:

- **BK — model backends** (`src/fleet/llm/backends/` absent; no local profile). §13 rows 36,37,38,39,40,43; §12.41, §12.42, §12.43.
- **ST — stub / revalidation machinery per ADR-0022** (`StubRot` has zero hits repo-wide; `orchestrator/stubs.py` absent). §13 rows 33,34,35; §3.5.1; §12.39.
- **RS — fleet resume** (`cli.py:9879` / `9875` are `_unavailable`). §13 row 45; §12.45.
- **FD — findings that exist only as SQL comments** (declared in migrations/DDL prose, never emitted by Python).

## Global Constraints

1. **Worktree isolation is mandatory.** Every worker runs `tools/worktree/new-worktree.sh <task-id>`
   and works ONLY inside `/home/redmage/swe repo harness worktrees/wt-<task-id>` on branch
   `agent/<task-id>`. The pre-commit hook (ADR-0074) enforces the branch/checkout correspondence.
   Never `git add -A`; stage explicit paths only. Never `--no-verify`.
2. **No `FLEET_*` env vars** in any shell that runs the harness or its tests (`settings.py` pairs
   `env_prefix="FLEET_"` with `extra="forbid"` → exit 2).
3. **Never run two pytest sessions concurrently.** Run only the test files covering your own lane,
   and only when no sibling lane is mid-run — `pytest_sessionfinish` reaps `BAZEL_ROOT` children.
   Each worktree has its own `BAZEL_ROOT`, which is what makes lane-scoped runs safe.
4. **`mypy --strict src/fleet` must stay clean** across your commits.
5. **No vendor SDK, no `base_url`, no model id may appear in Python outside `llm/backends/`.**
   `config/models.yaml` is the only place a model id or endpoint is declared (§9, ADR-0023,
   CLAUDE.md guardrail 3).
6. **Interface-first (CLAUDE.md guardrail 3).** Backends are injected; nothing reaches for a global.
7. **Measurement discipline (CLAUDE.md guardrail 6).** Never put an unmeasured number in a report,
   ADR, or docstring. Re-measure before it enters `docs/`.
8. **Directive lineage (CLAUDE.md guardrail 1).** Do not label your own design choice "by directive"
   or "mandatory". Agent-originated choices are *Agent Recommendations*.
9. **Fail loud (Rule 11).** No silent degradation, no bool where a typed error belongs.
10. **Surgical changes (Rule 3).** Touch only your lane's files. Do not reformat adjacent code.

## Wave 1

### Task BK1 — `llm/backends/` package + `anthropic.py`
Create `src/fleet/llm/backends/__init__.py` and `src/fleet/llm/backends/anthropic.py`.
`anthropic.py` implements the `ModelBackend` protocol from `src/fleet/llm/client.py` for the
native Anthropic Messages API and registers itself with `@register_backend`.
Requirements:
- Read `src/fleet/llm/client.py` FIRST. `ModelBackend`'s protocol surface, `ModelCapabilities`,
  `BackendTarget`, `Price`, `TokenUsage`, `FinishReason`, `FailoverTrigger` and the error
  hierarchy (`LlmError` subclasses) are already defined there. Implement against them verbatim;
  do not redefine or widen them.
- `discover()` in `client.py` does a `pkgutil` walk of `fleet.llm.backends`. Your `__init__.py`
  must make that walk work and must NOT eagerly import vendor SDKs at package level.
- **The `anthropic` SDK may not be installed.** Per `client.py`'s own docstring at `discover()`,
  "a backend whose SDK is not installed fails its import here and is simply not registered."
  Honour that: the module-level SDK import is what fails. Do not add a try/except that registers
  a half-working backend.
- Declare `ModelCapabilities` for the target in code (§13 row 36: "declared in code per backend,
  merged with `capabilities_override`"). Validate the target's own fields (§13 row 36: each
  backend validates its own target fields, naming profile/tier/target-index/missing field).
- `FinishReason` mapping must be exhaustive and must distinguish `length` from a schema failure
  (§13 row 47 — `length` is NOT a failover trigger).
- Tests: a new test file covering (a) `discover()` finds the backend when the SDK is importable,
  (b) target-field validation rejects a malformed target with a message naming the missing field,
  (c) finish-reason mapping, (d) capability declaration. Transport must be faked — **no network
  call in any test**.
Success: new tests pass; `mypy --strict src/fleet` clean; `grep -rn "anthropic" src/fleet --include=*.py`
shows vendor imports only inside `llm/backends/anthropic.py`.

### Task BK2 — `openai_compatible.py` + the local profile
Create `src/fleet/llm/backends/openai_compatible.py` and add a `local` profile to
`config/models.yaml`.
Requirements:
- Same protocol-first rule as BK1: read `client.py` first, implement its `ModelBackend` verbatim.
- This is "any `base_url`": local vLLM / Ollama / LM Studio / llama.cpp / TGI, plus hosted
  OpenAI-compatible endpoints. **`base_url` is required** — a target without one is a startup
  error naming profile, tier, target index, and the missing field (§13 row 36).
- Capability negotiation floor: this backend must be able to declare that it supports *no*
  structured-output mode. `PROMPTED` is a legitimate rung (§13 row 37); a model with no
  structured-output support is a legitimate `CHEAP` target. Validation is always Pydantic on our
  side regardless of rung (ADR-0002).
- `config/models.yaml`: add `profiles.local` routing all three tiers at `openai_compatible`
  targets. Per `src/fleet/models/tasks.py`'s `BackendTarget._free_is_declared_not_derived`,
  a free (locally-served) target must **declare** it free rather than have it inferred — read
  that validator and satisfy it exactly. §9 rule 5 requires a declared price on every target.
  A locally-served model has `cost_usd = 0.0` with a NON-EMPTY `backend`, which must not read as
  a fully-cached run (see the comment at `src/fleet/models/tasks.py` around `cost_usd`).
- `api_key_env` names an env var, never a key value. Many local servers need a dummy key —
  handle a missing/empty key without crashing if the target declares none required.
- Tests: config loader accepts `profiles.local` and resolves every role→tier→target chain;
  a `base_url`-less target is rejected at load with a field-naming message; the `PROMPTED` floor
  is reachable. No network calls.
Success: new tests pass; `mypy --strict src/fleet` clean; `config/models.yaml` still loads with
`default_profile: default` unchanged.

### Task ST1 — `orchestrator/stubs.py`: the §3.5.1 state machine
Create `src/fleet/orchestrator/stubs.py` implementing the four-transition stub state machine.
**No model participates** (SPEC §3.5.1 line: "`orchestrator/stubs.py`; no model participates").
Requirements:
- Read SPEC §3.5.1 (search `docs/SPEC.md` for `### 3.5.1` / the `StubState` transition table T1–T4)
  and `src/fleet/models/enums.py`'s `StubState` before writing anything. The states
  `ACTIVE / SUPERSEDED / RESOLVED / ABANDONED` and transitions T1–T4 are spec'd verbatim; transcribe,
  do not invent.
- Pure functions over explicit inputs. **Do not** open a DB connection inside the transition logic;
  take the rows/records you need as arguments and return the decision. State persistence belongs to
  `state/repository.py` (CLAUDE.md guardrail 4: DB is execution state only; Git is code truth).
- T1 `ACTIVE → SUPERSEDED` requires provider `RepoStatus.SUCCEEDED` **and** its
  `PullRequestDraft.state == 'MERGED'` — both, per ADR-0011 stacking. Under
  `stubs.revalidation: manual` the trigger is the operator's `fleet stubs resolve` instead.
- T2 `SUPERSEDED → RESOLVED` requires `VerificationReport.verdict == 'PASS'` **and**
  `verified_against_stubs == []`.
- T3 `SUPERSEDED → ABANDONED` on `FailureClass.STUB_DIVERGED`, or rounds hitting
  `stubs.max_revalidation_rounds`, or revalidation budget exhausted.
- T4: read the spec for the fourth transition; do not guess it.
- The audit trail is **append-only**: a new revalidation round is a NEW round, never a resurrected
  old one (§3.5.1).
- `stub_reconcile`: every still-open row at end of run → `ABANDONED` + `UnresolvedStub` finding;
  consumers **stay** `DEGRADED` — there is no config value that promotes them (§13 row 35).
  `stub_reconcile` must NOT abandon a stub whose provider has an open PR (§13 row 45).
- Tests: one test per transition including the negative case (T1 with SUCCEEDED-but-not-MERGED must
  NOT fire), plus `stub_reconcile`'s open-PR carve-out and its no-promotion invariant.
Success: new tests pass; `mypy --strict src/fleet` clean; no SQL and no LLM call in the new module.

## Wave 2 (dispatched after wave 1 lands)

### Task ST2 — StubRot / `STUB_DIVERGED` differential detection
### Task RS1 — `fleet resume`: precondition validation, never blind replay
### Task FD1 — findings declared only in SQL comments → real Python emitters

Briefs written at dispatch time from the wave-1 research report.
