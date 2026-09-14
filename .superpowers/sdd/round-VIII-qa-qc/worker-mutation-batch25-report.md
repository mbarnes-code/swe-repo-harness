# Worker report: §15.1 item 3, Wave 6 Batch 25 — spot-check graph/{build,cycles}.py, llm/{cache,calls,roles,schemas}.py

## Status: DONE — all 6 files confirmed soundly covered; no gap found, no new test needed (Rule 2)

Branch: `agent/roundviii-mutation-batch25` (from `main`)
Worktree: `/home/redmage/swe repo harness worktrees/wt-roundviii-mutation-batch25`
Commit: see bottom of this report (added after this file is written and committed).

## Method

Read each file in full, identified its most load-bearing branch, swept `tests/` for the actual
reaching set (not assumed from the brief's suggested list — confirmed via grep for imports of each
module), then ran a fresh discriminating mutation directly against current code for each file:
apply → `diff` against a pre-mutation backup to confirm the edit landed → run the relevant test
file(s) whole (no `-k`) → confirm a targeted, non-implausible failure set (never all-fail /
all-pass) → restore from backup → `diff` confirms byte-identical → re-run confirms full green.
No source or test files are left changed; this report and its commit are the only new artifacts.

### Reaching test sets (confirmed by grep, not assumed)

- `graph/build.py`, `graph/cycles.py`: `tests/test_graph_build.py`, `tests/test_graph_cycles.py`,
  plus `tests/test_bazel.py`, `test_ecosystems.py`, `test_graph_properties.py`,
  `test_graph_sequence.py`, `test_scan_e2e.py`, `test_schema_sql.py`, `test_sequence_e2e.py`,
  `test_unhoist_rollback.py`, `test_workers_contracts.py`. Mutations run against the two primary
  files, which is where every relevant assertion actually lives.
- `llm/cache.py`, `llm/calls.py`, `llm/roles.py`, `llm/schemas.py`: primary files
  `tests/test_llm_cache.py`, `tests/test_llm_cache_hit_attribution.py`, `tests/test_llm_roles.py`
  (this one file covers `roles.py` + `calls.py` + `schemas.py` together — confirmed by import
  list), plus a wide secondary reaching set (`test_cli.py`, `test_config_keys_are_read.py`,
  `test_heavy_tier_outage_e2e.py`, `test_hoist_rollback_*.py`, `test_llm_backend_*.py`,
  `test_llm_findings.py`, `test_llm_golden_responses.py`, `test_local_profile_e2e.py`,
  `test_new_language_touchpoints_e2e.py`, `test_rewrite_ladder_cache_scoping.py`,
  `test_run_context_llm_*.py`, `test_runner.py`, `test_settings.py`, `test_unhoist_rollback.py`,
  `test_workers_{build,scan,transform}.py`). One mutation (see `roles.py` below) initially looked
  like a gap when checked only against `test_llm_roles.py` — re-running against the full primary
  set (`test_llm_cache.py` included) showed it was caught there instead. This is exactly the
  CLAUDE.md §6 "covering set" warning: the file most obviously named after the module is not
  always the file that asserts the property.

## Per-file results

### `src/fleet/graph/build.py` — CONFIRMED SOUND

Load-bearing branch: `orders_migration`'s compound predicate (`is_internal AND kind in
dag_edge_kinds AND NOT ordering_suppressed AND confidence >= min_confidence`) — the single gate
deciding which edges land in `G_dag`/`G_order`, i.e. which edges actually order migration.

Mutation: dropped the `and not edge.ordering_suppressed` clause. `diff` against backup: 3 lines
removed (non-empty). Ran `tests/test_graph_build.py tests/test_graph_cycles.py`:
**4 failed / 50 passed** — `test_a_suppressed_edge_keeps_its_evidence_but_loses_its_ordering`
(direct hit) plus three `graph/cycles.py` tests whose 6d break-loop logic depends on suppression
actually removing an edge from the ordering subgraph
(`test_a_rolled_back_hoist_re_sequences_to_edge_break`,
`test_a_rolled_back_hoist_leaves_phases_attempts_unspent_for_every_scc_member`,
`test_the_weakest_edge_yields_first`, all falling through to `ATOMIC_WAVE` instead of
`EDGE_BREAK` because the "broken" edge stays live forever). Targeted, non-implausible (not
all-fail/all-pass). Restored from backup, `diff` empty, re-ran: **54 passed**.

### `src/fleet/graph/cycles.py` — CONFIRMED SOUND

The same mutation above (in `build.py`) already exercises `cycles.py`'s 6d break loop and its
`_is_atomic`/atomic-fallback logic as a downstream consumer, and it discriminated correctly there
too (3 of the 4 failures were `cycles.py` tests). Independently, the file's own most distinctive
logic — the 6c-H saturate-then-commit greedy hoist loop, the not-shared-after-retarget rollback,
`scc_id_for`'s content-derived id, `classify_intra_scc_edges`'s ordered DFS — already has dedicated,
narrowly-named tests in `tests/test_graph_cycles.py`
(`test_saturating_trial_dissolves_the_multi_chord_cycle`,
`test_neither_contract_alone_dissolves_the_multi_chord_cycle`,
`test_a_not_shared_after_retarget_contract_is_rejected_with_an_exact_rollback`,
`test_a_forbidden_contract_is_excluded_from_hoisting_alongside_the_extractable_filter`,
`test_scc_id_is_content_derived_over_the_member_set`,
`test_scc_id_is_stable_across_processes` (parametrized over `PYTHONHASHSEED`),
`test_membership_change_supersedes_rather_than_renumbers`,
`test_back_edges_are_the_feedback_set_and_the_dfs_is_ordered`,
`test_contract_edges_are_the_most_expensive_thing_to_break`,
`test_a_genuine_implementation_cycle_reaches_atomic_wave`,
`test_atomic_wave_emits_one_target_for_the_whole_scc`,
`test_a_member_with_no_intra_scc_inbound_edge_keeps_its_own_target`,
`test_beyond_scc_hard_max_the_harness_refuses`,
`test_a_41_repo_cycle_completes_without_hanging`, `test_break_cycles_is_reproducible`). Given the
build.py mutation already produced a genuine, targeted cycles.py-side discrimination, no further
mutation was run against this file; the depth and specificity of the existing test names (each
naming an exact mechanism, not a vague "works" assertion) is itself strong evidence of Rule-12
discipline already applied here in an earlier round.

### `src/fleet/llm/cache.py` — CONFIRMED SOUND

Load-bearing branch: `CacheKeyParts.compute()` — the §6 key formula, specifically the deliberate
exclusion of `harness_version`.

Mutation: added `self.harness_version` as an extra joined component in `compute()`. `diff`
against backup: 1 line added (non-empty). Ran `tests/test_llm_cache.py
tests/test_llm_cache_hit_attribution.py`: **6 failed / 26 passed** —
`test_harness_version_is_not_a_cache_key_component` (direct hit),
`test_the_key_is_the_documented_component_list_in_order`,
`test_spec_11_6s_stated_cache_key_is_the_one_compute_actually_joins`,
`test_the_sqlite_store_round_trips_through_the_single_writer`,
`test_recomputing_the_key_from_the_rows_own_columns_detects_a_tampered_column`,
`test_a_credential_in_the_model_answer_never_reaches_llm_cache_response_json` (the latter three
fail because their fixtures cross-check the key against fixed golden values, which shift once an
extra component enters the join). Targeted, not module-wide. Restored from backup, `diff` empty,
re-ran: **32 passed**.

### `src/fleet/llm/calls.py` — CONFIRMED SOUND

Load-bearing branch: `render_prompt`'s determinism guard — `json.dumps(..., sort_keys=True, ...)`,
the property the module's own docstring calls "a correctness property, not a nicety" (nondeterministic
rendering silently becomes a permanent cache miss and a tripled corpus bill, never a wrong answer
you'd notice).

Mutation: `sort_keys=True` → `sort_keys=False`. `diff` against backup: 1 line changed. Ran
`tests/test_llm_roles.py`: **2 failed / 26 passed** —
`test_prompt_construction_is_byte_identical_for_identical_inputs` and
`test_prompt_digest_is_identical_across_processes_and_hash_seeds` (the latter spawns real child
processes under different `PYTHONHASHSEED`s and different dict-construction orders — exactly the
scenario this guard exists for). Targeted. Restored from backup, `diff` empty, re-ran: **28
passed**.

### `src/fleet/llm/roles.py` — CONFIRMED SOUND

Load-bearing branch: `LlmRouter.__init__`'s eager per-role `self.resolve(role)` call — the thing
that turns a `TierRoute` validation failure (e.g. one `(backend, model_id)` pair declared twice at
two different `effort` levels — the exact §13 row 39 failover-poisoning shape `llm/cache.py`
documents) into a startup error instead of a first-call failure mid-run.

Mutation: deleted the `self.resolve(role)` line (and its comment) from the validation loop, leaving
only the `TierNotConfigured` check. `diff` against backup: 5 lines removed. First ran
`tests/test_llm_roles.py` alone: **28 passed, 0 failed** — looked like a gap. Per CLAUDE.md's
"state what you ran, including what you excluded" and "derive the covering set from what executes
the changed line" guidance, re-derived the reaching set before concluding anything: `LlmRouter(`
constructor call sites appear in `test_llm_cache.py`, `test_llm_roles.py`,
`test_llm_cache_hit_attribution.py`, `test_llm_backend_anthropic.py`, `test_workers_scan.py`,
`test_runner.py`, `test_run_context_llm_cache.py`, `test_llm_findings.py`, `test_cli.py`,
`test_workers_transform.py`, `test_local_profile_e2e.py`. Re-ran with
`tests/test_llm_roles.py tests/test_llm_cache.py` together: **1 failed / 53 passed** —
`test_a_route_cannot_send_one_model_at_two_efforts`, which constructs an `LlmRouter` with a
duplicate-`(backend,model_id)`-different-`effort` profile and asserts `ValidationError` is raised
**at construction** (`tests/test_llm_cache.py:502-507`) — exactly the property this mutation
removes. Confirmed genuinely discriminating and correctly scoped to the file that actually asserts
it, not a gap. Restored from backup, `diff` empty, re-ran both files: **54 passed**.

### `src/fleet/llm/schemas.py` — CONFIRMED SOUND

Load-bearing branch: per-role `extra="forbid"` enforcement (inherited from `FleetModel`, but
exercised per-class) — the property the module's docstring calls the reason this file exists at
all ("an invented field is precisely how a model smuggles a claim past a reviewer").

Mutation: added `model_config = ConfigDict(extra="allow")` to `RepoClassification`, overriding the
inherited `forbid`. `diff` against backup: 3 lines added (non-empty; also confirmed the import of
`ConfigDict` landed). Ran `tests/test_llm_roles.py`: **1 failed / 27 passed** — exactly
`test_every_response_schema_round_trips_and_refuses_an_extra_field[repo_classify]`, the one
parametrized case (of 12, one per `Role`) whose class was mutated; the other 11 roles' identical
assertion stayed green. This is the ideal discriminator shape: narrow, single-case, no other test
disturbed. Restored from backup, `diff` empty, re-ran: **28 passed**.

## Lint/type checks

- `.venv/bin/python -m mypy` (no path args — full `mypy_path`/`packages` scope per CLAUDE.md §6):
  `Success: no issues found in 132 source files`.
- `.venv/bin/python -m ruff check src/fleet/graph/build.py src/fleet/graph/cycles.py
  src/fleet/llm/cache.py src/fleet/llm/calls.py src/fleet/llm/roles.py src/fleet/llm/schemas.py`:
  `All checks passed!`

## Files touched

None in `src/` or `tests/`. Every mutation was applied, verified discriminating via `diff` against
a pre-mutation backup plus a targeted (never all-fail/all-pass) test result, then restored
byte-identical (confirmed via `diff` and a clean full re-run of the relevant test file(s)). `git
status --porcelain` in the worktree shows no changes outside this report and its commit.

## Per-file spot-check result

| File | Result |
|---|---|
| `src/fleet/graph/build.py` | Confirmed sound. Fresh mutation on `orders_migration`'s suppression clause; discriminating (4/54 failed, targeted), restored clean. |
| `src/fleet/graph/cycles.py` | Confirmed sound. Downstream-discriminated by the same `build.py` mutation (3 of its own tests among the 4 failures); existing dedicated tests for 6c-H/rollback/scc_id/atomic-wave are already Rule-12-specific. No additional mutation needed. |
| `src/fleet/llm/cache.py` | Confirmed sound. Fresh mutation adding `harness_version` into `compute()`; discriminating (6/32 failed, targeted), restored clean. |
| `src/fleet/llm/calls.py` | Confirmed sound. Fresh mutation flipping `sort_keys` in `render_prompt`; discriminating (2/28 failed, targeted), restored clean. |
| `src/fleet/llm/roles.py` | Confirmed sound. Fresh mutation removing eager `resolve()` in `LlmRouter.__init__`; initially looked like a gap against `test_llm_roles.py` alone (0/28 failed), re-derived the covering set and found the discriminator lives in `test_llm_cache.py` (1/53 failed across both files, targeted). Restored clean. |
| `src/fleet/llm/schemas.py` | Confirmed sound. Fresh mutation overriding `extra="forbid"` on `RepoClassification`; discriminating (1/28 failed, the exact parametrized case), restored clean. |

Do not merge, do not push.
