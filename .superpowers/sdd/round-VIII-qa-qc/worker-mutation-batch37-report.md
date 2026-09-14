# Worker report: mutation batch 37 (round VIII, §15.1 item 3, Wave 7.4 batch 37 / G5)

**Status:** DONE
**Branch:** `agent/roundviii-mutation-batch37` (from `main` @ `bfbe13c`)
**Worktree:** `tools/worktree/new-worktree.sh roundviii-mutation-batch37 main`
**New file:** `tests/test_cli_transform_pipeline_worker.py` (10 tests)
**Split:** NOT split. All 8 in-scope symbols/classes were reachable from one cohesive test file,
reusing the existing D89 Phase 2 fixture scaffolding (`tests/test_d89_phase2_claim_lifecycle.py`'s
`wired`/`db_path`/`_sink`/`_payload`/`_init_transform_worktree`/`_task_row`, imported read-only —
this file never edits that module) for the two DB-backed groups (`_TransformClaimHook`,
`_TransformSink`), plus lightweight local fakes/fixtures for the other six.

## Scope

`src/fleet/cli.py` group G5, exactly as listed in the brief: `TransformPipelineWorker` (the class
itself), `TransformInput`/`TransformOutput`, `_transform_units`, `_TransformPlan`,
`_TransformEvidence`, `_ScopedWaveStore`, `_TransformClaimHook`, `_TransformSink` (**excluding**
`_TransformSink.__call__`'s task-resolution clause — the `if task_id is not None: ... resolve_task
...` block — which is already proven by `tests/test_d89_phase2_claim_lifecycle.py`'s "3.
`_TransformSink.__call__`'s new happy-path resolution" section; verified present and passing
before starting).

**Prior coverage was uneven, not zero** — checked per symbol before writing anything, by grepping
`tests/*.py` for each name:

- `TransformPipelineWorker.run()`/`.preconditions_hold()`: **never driven with fake step workers**
  in isolation anywhere; only exercised transitively through the real end-to-end driver in
  `tests/test_transform_e2e.py`, which never forces a checkpoint mismatch.
- `TransformInput`/`TransformOutput`: constructed directly in only two other files
  (`tests/test_d89_phase2_claim_lifecycle.py`, `tests/test_d89_phase2_reconciliation.py`), always
  with a syntactically valid `phase_pre_commit_sha`; no test exercises the `Field(pattern=...)`
  rejection path.
- `_transform_units`: never called directly by any existing test.
- `_TransformPlan`: constructed and read in `tests/test_cli.py`'s `_transform_criterion` tests, but
  never mutated — `frozen=True` is asserted by nothing.
- `_TransformEvidence.record()`: **two existing tests** in `tests/test_transform_e2e.py` cover the
  `prior is not None` merge branch precisely for `unresolved`/`rewritten`, but both always pass an
  empty `commits`/`skipped` on the second `record()` call, so `prior.commits.extend(...)` and
  `prior.skipped.extend(...)` are reached with no observable effect.
- `_ScopedWaveStore`: **never instantiated by any test** (confirmed by grep across `tests/`) — only
  mentioned in prose comments about a *different* class's `append_blocked_by`.
- `_TransformClaimHook`: well covered by three tests in `tests/test_d89_phase2_claim_lifecycle.py`
  (mint/claim/target_paths, idempotent row reuse, non-TRANSFORM refusal), but all three only assert
  `lease_expires_at is not None`, never that it equals `clock() + lease_ttl_s`.
- `_TransformSink.__call__` (minus the excluded clause): the happy-path attempt-row write and the
  post_commit_sha-before-task-resolution ordering are covered by
  `tests/test_d89_phase2_claim_lifecycle.py`, but **all three ADR-0021 evidence loops**
  (`anchored_rejections` → `attempts`, `failed_approaches` → `rejected_approaches`, `guard_off` →
  `findings`) are exercised by **zero** existing tests anywhere in the repo — confirmed by grepping
  for those three field names outside `src/`; the only other hits are `RewriteWorker`-level tests
  proving the *worker* populates the lists, never that the *sink* persists them.

## Per-symbol results

| Symbol | Most load-bearing untested branch targeted | Test(s) |
|---|---|---|
| `TransformPipelineWorker.run()` | the checkpoint-rejected `owed = set(units)` reset when a resumed dispatch's relocate-step precondition fails (§7.1) | `test_run_resets_owed_to_the_full_unit_set_when_the_checkpoint_no_longer_holds` |
| `TransformPipelineWorker.preconditions_hold()` | `remaining_units is None` short-circuit vs. the worktree-exists check, kept independent | `test_preconditions_hold_requires_both_a_checkpoint_and_a_live_worktree` |
| `TransformInput` | `phase_pre_commit_sha`'s `Field(pattern=r"^[0-9a-f]{40}$")` rejects wrong-length/non-hex values | `test_transform_input_rejects_a_pre_commit_sha_that_is_not_forty_lowercase_hex_chars` |
| `TransformOutput` | `tier`/`context_policy` defaults satisfy §6's `(tier='DETERMINISTIC')=(context_policy IS NULL)` pairing | `test_transform_output_context_policy_defaults_to_none_matching_the_deterministic_rung` |
| `_transform_units` | relocate-before-rewrite ordering and namespace prefixing | `test_transform_units_lists_every_relocate_unit_before_any_rewrite_unit_with_its_namespace` |
| `_TransformPlan` | `frozen=True` actually prevents post-construction mutation | `test_transform_plan_is_frozen_and_cannot_be_mutated_after_construction` |
| `_TransformEvidence.record()` | cross-attempt `commits`/`skipped` merge on the `prior is not None` branch | `test_transform_evidence_record_merges_commits_and_skipped_across_attempts` |
| `_ScopedWaveStore.wave_members` | `only=None` passthrough vs. `only=[...]` narrowing | `test_scoped_wave_store_narrows_to_only_when_given_and_passes_through_when_none` |
| `_TransformClaimHook.__call__` | `lease_ttl_s=self._lease_ttl_s` actually threads through to `lease_expires_at`, not a hardcoded duration | `test_claim_hook_computes_lease_expires_at_from_its_own_configured_ttl` |
| `_TransformSink.__call__` | the three ADR-0021 evidence loops: `anchored_rejections`→`attempts`(ANCHORED_REPEAT), `failed_approaches`→`rejected_approaches`, `guard_off`→`findings` | `test_sink_writes_one_row_per_anchored_rejection_failed_approach_and_guard_off_event` |

10 tests total, all passing.

## Mutation-proof verification (Rule 12 protocol)

For **every** function/branch above (not a sample — the scope was small enough to cover
exhaustively), I ran the full backup → mutate → diff (nonempty) → test (red) → restore → diff
(identical) cycle directly against `src/fleet/cli.py` inside this worktree, using a private
per-lane backup at `/tmp/.../scratchpad/batch37/cli.py.orig.bak` (never the shared scratchpad
root, and never `git diff`/`git stash`, per this round's own worktree-hygiene rules). For
`_TransformSink`, three independent mutations were run in sequence (one per loop), each restored
before the next, since one test exercises all three loops:

| # | Mutation | Diff nonempty? | Target test result | Restored & re-verified identical? |
|---|---|---|---|---|
| 1 | Removed `owed = set(units)` reset in the relocate checkpoint-rejected branch | yes (1 line) | **FAILED** — `assert ['a.py'] == []` | yes, 10/10 green |
| 2 | Dropped the `remaining_units is None` guard in `preconditions_hold` (returned live `is_dir()` unconditionally) | yes (2→1 lines) | **FAILED** — `assert True is False` on the fresh-payload-but-real-worktree case | yes, 10/10 green |
| 3 | Loosened `phase_pre_commit_sha` pattern from `{40}$` to `+$` | yes (1 line) | **FAILED** — `DID NOT RAISE ValidationError` | yes, 10/10 green |
| 4 | Changed `TransformOutput.tier` default from `DETERMINISTIC` to `"LLM_ESCALATION"` | yes (1 line) | **FAILED** — `'LLM_ESCALATION' == 'DETERMINISTIC'` | yes, 10/10 green |
| 5 | Swapped `_transform_units`'s relocate/rewrite emission order | yes (2 lines) | **FAILED** — units list order mismatch | yes, 10/10 green |
| 6 | Dropped `frozen=True` from `_TransformPlan`'s `@dataclass(...)` | yes (1 line) | **FAILED** — `DID NOT RAISE FrozenInstanceError` | yes, 10/10 green |
| 7 | Removed `prior.commits.extend(...)`/`prior.skipped.extend(...)` in `_TransformEvidence.record()` | yes (1→2 lines) | **FAILED** — `assert ['c1'] == ['c1', 'c2']` | yes, 10/10 green |
| 8 | Made `_ScopedWaveStore.wave_members` return `members` unfiltered | yes (1 line) | **FAILED** — narrowed-store assertion got all 3 members instead of 2 | yes, 10/10 green |
| 9 | Hardcoded `lease_ttl_s=600` in `_TransformClaimHook.__call__`'s claim call | yes (1 line) | **FAILED** — lease expiry off by design (600s vs. the test's 4321s) | yes, 10/10 green |
| 10 | Disabled the `anchored_rejections` loop (`for rejection in ():`) | yes (1 line) | **FAILED** — `assert 0 == 1` on the ANCHORED_REPEAT attempts row | yes, 10/10 green |
| 11 | Disabled the `failed_approaches` loop (`for failed in ():`) | yes (1 line) | **FAILED** — `assert 0 == 1` on the rejected_approaches row | yes, 10/10 green |
| 12 | Disabled the `guard_off` loop (`for event in ():`) | yes (1 line) | **FAILED** — `assert 0 == 1` on the findings row | yes, 10/10 green |

Every mutation was confirmed genuinely applied via `git diff --numstat --no-index` against the
private backup before reading the test result (Rule 12's zero-change gate), and `cli.py` is
byte-identical to `main` after the final restore (`git status --porcelain -- src/fleet/cli.py`
prints nothing).

One incidental finding while designing mutation #9: the three existing
`_TransformClaimHook` tests in `tests/test_d89_phase2_claim_lifecycle.py` only assert
`lease_expires_at is not None`, so they stay green under mutation #9 (confirmed:
`test_claim_hook_populates_target_paths_and_claims_running_before_dispatch` still passes with
`lease_ttl_s` hardcoded) — this batch's new test is what actually pins the value.

## Reaching test files run for verification

Ran, whole files, no `-k` filter (Guardrail 6's "state what you ran" / "derive from what executes
the changed line", applied here as: derive the covering set from what actually imports/exercises
the 8 in-scope symbols, not from where their text sits):

- `tests/test_cli_transform_pipeline_worker.py` (new, this batch) — 10 tests, direct unit coverage
  of every in-scope symbol.
- `tests/test_d89_phase2_claim_lifecycle.py` — the only other file directly constructing
  `_TransformClaimHook`/`_TransformSink`; re-run to confirm no interaction with the new tests
  (they share fixtures by import, not by mutation of shared state — each gets its own `tmp_path`
  SQLite DB via the `db_path`/`wired` fixture chain).
- `tests/test_transform_e2e.py` — the real `fleet scan → fleet sequence → fleet transform` CLI
  driver; the only reaching test for `TransformPipelineWorker.run()`'s *happy* path and for
  `_TransformEvidence`'s `unresolved`-dedup logic (the two tests this batch's
  `_TransformEvidence` gap analysis is built on live here).
- `tests/test_workers_transform.py` — the real `RelocateWorker`/`RewriteWorker` unit-level tests;
  confirms the two real step workers `TransformPipelineWorker` chains still behave as this batch's
  fake-step tests assume (`preconditions_hold` semantics, `RelocateInput`/`RewriteInput` shapes).

Combined run: `pytest tests/test_cli_transform_pipeline_worker.py tests/test_d89_phase2_claim_lifecycle.py tests/test_transform_e2e.py tests/test_workers_transform.py`
→ **74 passed** in ~49s. `tests/test_cli.py`'s own `_transform_criterion`/`_TransformPlan` tests
were spot-run too (unaffected; they read `_TransformPlan` fields, never mutate them, so they carry
no discriminating power for this batch's `_TransformPlan` mutation and were correctly not relied
on for it).

## Static checks

- `ruff check tests/test_cli_transform_pipeline_worker.py` → All checks passed.
- `ruff format --check tests/test_cli_transform_pipeline_worker.py` → already formatted.
- `python -m mypy` (project's real invocation — no path args; `pyproject.toml`'s
  `[tool.mypy]` scopes strict mode to `packages = ["fleet"]`, i.e. `src/fleet` only, so `tests/`
  is outside the gate by project convention) → `Success: no issues found in 132 source files`.
  (An exploratory `mypy tests/test_cli_transform_pipeline_worker.py` direct-path run — which force-
  applies strict mode outside its normal scope — was also cleaned up opportunistically: fixed
  `WorkerResult[Any]` generics, `TransformInput.model_validate(...)` instead of `**dict[str,
  object]`, and `type: ignore[dict-item]`/`[arg-type]` on the intentionally duck-typed fake-worker
  wiring. Two pre-existing errors from the *imported* `tests/test_d89_phase2_claim_lifecycle.py`
  module remain under that same non-standard direct-path invocation and are out of this batch's
  scope — that file is unmodified by this batch and untouched by the project's real mypy gate
  either way.)

## Notes for the controller

- No `src/fleet/cli.py` changes — this batch is test-only, as scoped.
- `_TransformSink.__call__`'s excluded resolution-step clause was re-verified present and covered
  before starting (not re-tested here), per the brief.
- Did not merge, did not push. Branch `agent/roundviii-mutation-batch37` sits on top of `main` @
  `bfbe13c` with one commit adding the new test file.
