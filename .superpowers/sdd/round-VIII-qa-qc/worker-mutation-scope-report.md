# §15.1 item 3 — Mutation-audit sweep scope (planning only)

Status: DONE (scoping only — no mutations run, no test code written or modified).

## 0. Method and caveats

- Inventory: `git log --follow --name-only -- src/` deduplicated, cross-checked against a live
  `find src/fleet -name '*.py'` listing. **133 files total under `src/fleet/`**: 132 `.py` files +
  1 non-Python file (`src/fleet/state/schema.sql`, DDL — not itself mutation-testable by Rule 12's
  method, but it is the end-state schema that `test_migrations.py`/`test_db.py` validate
  structurally). 18 of the 132 `.py` files are `__init__.py` with no branching logic — excluded
  from the mutation census as N/A (nothing to mutate). No vendored or generated code exists under
  `src/` — the only hits for "generated"/"vendored"/"DO NOT EDIT" are `settings.py`'s own
  generated-file *detector* literals and prose uses of the word "vendored" in `cli.py` docstrings,
  both false positives, verified by reading the surrounding lines.
- "Mutation-proof evidence" was **not** inferred from "a test file with a plausible name exists."
  Per Rule 12 and the brief, I grepped `tests/*.py` for actual Rule-12 discipline language (`Rule
  12`, `mutation test`, `mutation-proof`, `old-passes/new-fails`, `discriminating mutation`,
  `adversarial mutation`, `mutant surviv*`) and read every hit (102 lines, 41 files) to confirm each
  is genuine mutation-testing prose and not, e.g., a reference to the *database* concept of a
  "mutation" (`state/mutations.py`-style usage) or an incidental use of "discriminat-". All 41 files
  in the confirmed set are genuine. This is file-level evidence of *some* discriminating-mutation
  discipline having been applied somewhere in that test file — it is **not** a claim that every
  function in every src file that test file reaches has been individually mutation-proven. That
  finer-grained claim is exactly what the actual sweep (not this scoping pass) must verify per
  function, per Rule 12's "coverage in cases is not coverage in discrimination."
- Per-file "does any test reach this file" was computed by grepping `tests/` for each file's dotted
  module path (e.g. `fleet.state.repository`) as a literal substring. This under-counts files
  reached only via `from fleet.x import y` (no contiguous dotted match) or via a registry/dispatch
  pattern rather than a direct import string. Three such false negatives were caught and corrected
  by manual inspection (see §2): `obs/log.py`, `ecosystems/jvm.py`, `ecosystems/rust.py`. Any
  worker picking up a bucket-C file from this report should re-verify reach before trusting it —
  per Rule 12/Guardrail-6 discipline, this census is a hypothesis, not a certified absence.

## 1. File inventory summary

| Category | Count |
|---|---|
| Total files under `src/fleet/` (incl. schema.sql) | 133 |
| `__init__.py` (no logic, excluded) | 18 |
| Non-Python (schema.sql, excluded from mutation method, covered by DB tests structurally) | 1 |
| **Files in scope for the mutation-audit sweep** | **114** |

## 2. Buckets

### Bucket A — mutation-proof evidence found directly (64 files)

A confirmed Rule-12-discipline test file (one of the 41 in §0) reaches this src file by direct
import-string match, OR (obs/log.py, migrations v007/v008/v011) by manual verification after the
import-string grep produced a false negative.

```
src/fleet/bazel/generators.py          <- tests/test_build_e2e.py
src/fleet/bazel/layout.py              <- tests/test_build_e2e.py
src/fleet/bazel/lockfile.py            <- tests/test_build_e2e.py, tests/test_cli.py
src/fleet/bazel/query.py               <- tests/test_workers_build.py
src/fleet/cli.py                       <- 27 confirmed test files (see below)
src/fleet/graph/build.py               <- tests/test_graph_cycles.py, tests/test_workers_contracts.py
src/fleet/graph/cycles.py              <- tests/test_graph_cycles.py, tests/test_sequence_e2e.py, tests/test_workers_contracts.py
src/fleet/graph/infer.py               <- tests/test_sequence_e2e.py, tests/test_workers_contracts.py
src/fleet/graph/sequence.py            <- tests/test_graph_cycles.py, tests/test_runner.py
src/fleet/llm/cache.py                 <- tests/test_llm_cache_hit_attribution.py, tests/test_workers_build.py
src/fleet/llm/calls.py                 <- tests/test_llm_cache_hit_attribution.py
src/fleet/llm/client.py                <- tests/test_cli.py, tests/test_heavy_tier_outage_e2e.py, tests/test_llm_cache_hit_attribution.py, tests/test_llm_golden_responses.py, tests/test_llm_openai_stub_server_e2e.py, tests/test_runner.py, tests/test_scan_e2e.py, tests/test_transform_e2e.py, tests/test_workers_build.py, tests/test_workers_contracts.py, tests/test_workers_scan.py
src/fleet/llm/roles.py                 <- tests/test_cli.py, tests/test_heavy_tier_outage_e2e.py, tests/test_hoist_rollback_git.py, tests/test_hoist_rollback_wiring.py, tests/test_llm_cache_hit_attribution.py, tests/test_llm_golden_responses.py, tests/test_local_profile_e2e.py, tests/test_runner.py, tests/test_workers_scan.py
src/fleet/llm/schemas.py               <- tests/test_llm_cache_hit_attribution.py, tests/test_llm_golden_responses.py, tests/test_workers_build.py, tests/test_workers_scan.py
src/fleet/migrations/_support.py       <- tests/test_migrations.py
src/fleet/migrations/v007_logical_keys.py   <- tests/test_migrations.py (direct: `v007_logical_keys.upgrade(conn)` etc., line ~694)
src/fleet/migrations/v008_reservations.py   <- tests/test_migrations.py (direct: `v008_reservations.LEGACY_ID_PREFIX`, `.INDEX`, line ~810/847)
src/fleet/migrations/v011_migrated_test_count.py <- tests/test_migrations.py (direct, its own docstring cited, line ~856/866/915)
src/fleet/models/base.py               <- tests/test_state_models.py
src/fleet/models/build.py              <- tests/test_build_e2e.py, tests/test_cli.py, tests/test_state_models.py, tests/test_workers_build.py
src/fleet/models/enums.py              <- 26 confirmed test files (widely used enum module)
src/fleet/models/graph.py              <- tests/test_cli.py, tests/test_graph_cycles.py, tests/test_migrations.py, tests/test_repository.py, tests/test_runner.py, tests/test_state_models.py, tests/test_workers_contracts.py
src/fleet/models/repo.py               <- tests/test_build_e2e.py, tests/test_graph_cycles.py, tests/test_state_models.py, tests/test_workers_build.py, tests/test_workers_contracts.py
src/fleet/models/state.py              <- tests/test_blocked_by_writer_statements.py, tests/test_build_e2e.py, tests/test_cli.py, tests/test_heavy_tier_outage_e2e.py, tests/test_runner.py, tests/test_scan_e2e.py, tests/test_state_models.py
src/fleet/models/tasks.py              <- 14 confirmed test files
src/fleet/obs/log.py                   <- tests/test_obs.py, tests/test_event_stream_wiring.py (manual: import-string grep false-negatived on `from fleet.obs import log as logmod`)
src/fleet/obs/redact.py                <- tests/test_repository.py
src/fleet/orchestrator/budgets.py      <- tests/test_budgets.py, tests/test_cli.py, tests/test_runner.py, tests/test_stubs.py
src/fleet/orchestrator/context.py      <- tests/test_cli.py, tests/test_runner.py
src/fleet/orchestrator/findings.py     <- tests/test_runner.py
src/fleet/orchestrator/reentry.py      <- tests/test_floor_rule_statements.py, tests/test_integration_honesty_citations.py, tests/test_stubs.py
src/fleet/orchestrator/retry.py        <- tests/test_runner.py, tests/test_workers_build.py, tests/test_workers_scan.py
src/fleet/orchestrator/runner.py       <- tests/test_graph_cycles.py, tests/test_integration_honesty_citations.py, tests/test_runner.py
src/fleet/orchestrator/scheduler.py    <- tests/test_cli.py, tests/test_integration_honesty_citations.py, tests/test_repository.py, tests/test_runner.py
src/fleet/orchestrator/stubs.py        <- tests/test_cli.py, tests/test_stubs.py
src/fleet/rewrite/apply.py             <- tests/test_cli.py, tests/test_transform_e2e.py
src/fleet/rewrite/approach.py          <- tests/test_rewrite_approach.py
src/fleet/rewrite/rules.py             <- tests/test_cli.py, tests/test_transform_e2e.py
src/fleet/sandbox/container.py         <- tests/test_cli.py, tests/test_sandbox.py
src/fleet/sandbox/worktree.py          <- tests/test_build_e2e.py, tests/test_cli.py, tests/test_hoist_rollback_git.py, tests/test_sandbox.py, tests/test_workers_build.py
src/fleet/settings.py                  <- 12 confirmed test files
src/fleet/state/db.py                  <- 14 confirmed test files
src/fleet/state/projection.py          <- tests/test_local_profile_e2e.py, tests/test_read_transaction_statements.py
src/fleet/state/repository.py          <- 14 confirmed test files
src/fleet/util/fs.py                   <- tests/test_sandbox.py, tests/test_transform_e2e.py
src/fleet/util/hashing.py              <- tests/test_contracts_criterion_scale.py
src/fleet/util/proc.py                 <- 8 confirmed test files
src/fleet/vcs/commits.py               <- tests/test_build_e2e.py, tests/test_hoist_rollback_git.py, tests/test_no_state_outside_git.py
src/fleet/vcs/filter_repo.py           <- tests/test_hoist_rollback_git.py
src/fleet/vcs/forge.py                 <- tests/test_cli.py, tests/test_hoist_rollback_git.py, tests/test_hoist_rollback_wiring.py, tests/test_workers_build.py
src/fleet/vcs/git.py                   <- tests/test_build_e2e.py, tests/test_cli.py, tests/test_hoist_rollback_git.py, tests/test_vcs.py, tests/test_workers_build.py
src/fleet/vcs/github.py                <- tests/test_workers_build.py
src/fleet/workers/base.py              <- tests/test_cli.py, tests/test_workers_build.py, tests/test_workers_contracts.py, tests/test_workers_scan.py + 5 more
src/fleet/workers/buildgen.py          <- tests/test_build_e2e.py, tests/test_workers_build.py
src/fleet/workers/buildverify.py       <- tests/test_build_e2e.py, tests/test_cli.py, tests/test_llm_cache_hit_attribution.py, tests/test_sandbox.py, tests/test_workers_build.py, tests/test_workers_scan.py
src/fleet/workers/classify.py          <- tests/test_cli.py, tests/test_workers_scan.py
src/fleet/workers/clone.py             <- tests/test_cli.py, tests/test_workers_scan.py
src/fleet/workers/contracts.py         <- tests/test_contracts_criterion_scale.py, tests/test_workers_contracts.py
src/fleet/workers/interrogate.py       <- tests/test_workers_contracts.py, tests/test_workers_scan.py
src/fleet/workers/prwriter.py          <- tests/test_cli.py, tests/test_workers_build.py
src/fleet/workers/rdepverify.py        <- tests/test_workers_build.py
src/fleet/workers/relocate.py          <- tests/test_cli.py
src/fleet/workers/rewrite.py           <- tests/test_d89_phase2_reconciliation.py, tests/test_no_state_outside_git.py
src/fleet/workers/symbolindex.py       <- tests/test_workers_contracts.py, tests/test_workers_scan.py
```

`cli.py`'s 27 confirmed reaching test files: test_baseline_ok_exclusion, test_blocked_by_writer_statements,
test_build_e2e, test_cli, test_contracts_criterion_scale, test_d89_phase1_task_lifecycle,
test_d89_phase2_claim_lifecycle, test_d89_phase2_reconciliation, test_event_stream_wiring,
test_floor_rule_statements, test_graph_cycles, test_heavy_tier_outage_e2e, test_hoist_rollback_git,
test_hoist_rollback_wiring, test_integration_honesty_citations, test_llm_cache_hit_attribution,
test_local_profile_e2e, test_no_state_outside_git, test_prepare_before_admit,
test_read_transaction_statements, test_resume_continue, test_scan_e2e, test_sequence_e2e,
test_stub_resolution_task79, test_transform_e2e, test_workers_build, test_workers_contracts.

**Important qualifier**: bucket A means "this file is reached by a test file that demonstrably
practices Rule-12 discipline somewhere in it" — at 20,633 lines, `cli.py` has dozens of
subsystems (scan/transform/build/verify wave commands, `_gc_disk`, `_abort_impl`,
`_quarantine_impl`, `_apply_stub_decisions`, `_emit_one_pr`, PR emission, resume/continue, etc.).
Being in bucket A does **not** mean every one of those subsystems has its own discriminating
mutation — it means the file is a plausible, evidenced starting point, not a certified-clean file.
`cli.py` needs its own internal decomposition (see §4).

### Bucket A− — exercised only transitively, no per-step discriminator citation (7 files)

`src/fleet/migrations/v002_node_kind.py`, `v003_anti_anchoring.py`, `v004_stub_lifecycle.py`,
`v005_backend_identity.py`, `v006_mutations_deleted.py`, `v009_coordinate_version.py`,
`v010_file_blobs.py`.

All seven are applied by `test_migrations.py`'s default `migrate(db)` calls (default
`steps=STEPS`, the full ladder — confirmed at `src/fleet/migrations/__init__.py:269`), and that
file is Rule-12-disciplined overall. But unlike v007/v008/v011, none of these seven is named
individually in a discriminating-mutation docstring in `test_migrations.py` — they are exercised
as part of the ladder's end-to-end effect, which per Rule 12's own "audit mutations for
expressibility, not only pass/fail" is not the same as proof that a targeted mutation to *this
step's own logic* would be caught. Needs a short, targeted check (mutate one field/branch in each
step's `upgrade()`, confirm `migrate(db)` — or a narrower `steps=_STEPS_THROUGH(n)` call — actually
reddens) before being promoted to bucket A.

### Bucket B — has reaching tests, no confirmed mutation-proof evidence (41 files)

```
src/fleet/ecosystems/base.py                    <- tests/test_bazel.py, tests/test_ecosystems.py
src/fleet/ecosystems/contracts/avro.py          <- tests/test_ecosystems_contracts_avro.py
src/fleet/ecosystems/contracts/base.py          <- tests/test_ecosystems_contracts_base.py
src/fleet/ecosystems/contracts/openapi.py       <- tests/test_ecosystems_contracts_openapi.py
src/fleet/ecosystems/contracts/proto.py         <- tests/test_ecosystems_contracts_proto.py
src/fleet/ecosystems/contracts/shared_lib.py    <- tests/test_ecosystems_contracts_shared_lib.py
src/fleet/ecosystems/contracts/thrift.py        <- tests/test_ecosystems_contracts_thrift.py
src/fleet/ecosystems/go.py                      <- tests/test_bazel.py
src/fleet/ecosystems/js.py                      <- tests/test_bazel.py, tests/test_ecosystems.py
src/fleet/ecosystems/jvm.py                     <- tests/test_bazel.py, tests/test_ecosystems.py (manual correction: reached via ecosystem registry, not a literal dotted import)
src/fleet/ecosystems/py.py                      <- tests/test_bazel.py
src/fleet/ecosystems/rust.py                    <- tests/test_bazel.py, tests/test_ecosystems.py (manual correction, same reason as jvm.py)
src/fleet/ecosystems/unknown.py                 <- tests/test_ecosystems.py
src/fleet/graph/collisions.py                   <- tests/test_bazel.py, tests/test_graph_sequence.py
src/fleet/graph/query.py                        <- tests/test_graph_build.py
src/fleet/llm/backends/anthropic.py             <- tests/test_llm_backend_anthropic.py
src/fleet/llm/backends/bedrock.py               <- tests/test_llm_backend_bedrock.py
src/fleet/llm/backends/openai_compatible.py     <- tests/test_llm_backend_openai_compatible.py, tests/fixtures/llm/stub_openai_server.py
src/fleet/llm/backends/vertex.py                <- tests/test_llm_backend_vertex.py
src/fleet/llm/failover.py                       <- tests/test_llm_failover.py
src/fleet/manifests/base.py                     <- tests/test_manifests.py
src/fleet/manifests/cargo.py                    <- tests/test_manifests.py
src/fleet/manifests/gomod.py                    <- tests/test_manifests.py
src/fleet/manifests/gradle.py                   <- tests/test_manifests.py
src/fleet/manifests/maven.py                    <- tests/test_manifests.py
src/fleet/manifests/npm.py                      <- tests/test_ecosystems.py, tests/test_manifests.py
src/fleet/manifests/python.py                   <- tests/test_manifests.py
src/fleet/manifests/unknown.py                  <- tests/test_manifests.py
src/fleet/obs/events.py                         <- tests/test_obs.py
src/fleet/orchestrator/memory_guard.py          <- tests/test_memory_guard.py, tests/test_memory_guard_e2e.py
src/fleet/orchestrator/registry.py              <- tests/test_registries_stateless.py
src/fleet/rewrite/astgrep.py                    <- tests/test_new_language_touchpoints_e2e.py, tests/test_rewrite.py, tests/test_settings.py
src/fleet/rewrite/libcst_py.py                  <- tests/test_rewrite.py
src/fleet/rewrite/pipeline.py                   <- tests/test_rewrite.py, tests/test_workers_transform.py
src/fleet/rewrite/tsmorph.py                    <- tests/test_rewrite.py
src/fleet/sandbox/containerstats.py             <- tests/test_containerstats.py, tests/test_memory_guard.py, tests/test_memory_guard_e2e.py
src/fleet/state/checkpoints.py                  <- tests/test_checkpoints.py
src/fleet/state/digest.py                       <- tests/test_digest.py
src/fleet/util/cgroup.py                        <- tests/test_cgroup.py
src/fleet/vcs/gitea.py                          <- tests/test_gitea.py
src/fleet/workers/baseline.py                   <- tests/test_baseline_container.py, tests/test_workers_baseline.py
```

### Bucket C — no tests reaching the file's logic, or logic too trivial to need mutation (2 files)

- `src/fleet/util/errors.py` — **genuine gap.** Brand new in `334edeb` (the code-review fix commit,
  9 lines, `exception_type_name()`). Zero references anywhere in `tests/` (checked both the
  function name and the module path directly). This is the single highest-priority item in the
  whole sweep: newest code, explicitly extracted to consolidate 9 duplicated inline expressions
  across `cli.py`/`runner.py`/workers, and currently has no test at all, let alone a mutation
  proof.
- `src/fleet/__main__.py` — 3-line `main()` delegating straight to `fleet.cli.main`, no branching
  logic. Flag as N/A for mutation testing (nothing to mutate that isn't `cli.py`'s own surface),
  not a real gap.

## 3. Dispatch order and batch sizing

Highest-risk-first, per the brief's heuristic (files touched by the most recent `/code-review
-high` fix commit `334edeb`, `git show --stat 334edeb`: 33 `src/` files + 12 test files changed,
873 insertions / 173 deletions across 45 files). Batches are sized to one logical task each — one
subsystem or one file group a single subagent can mutation-test end-to-end in one dispatch,
consistent with "never more than one logical task per subagent."

**Wave 0 — zero coverage, newest code (1 batch, highest priority)**
- Batch 1: `util/errors.py` alone. Needs a *new* test file (or an addition to an existing
  `tests/test_util_*` if one exists — none currently does) before any mutation proof is possible.
  Small (9 lines) but currently completely unverified.

**Wave 1 — 334edeb-touched files with no confirmed mutation-proof evidence (bucket B/A− ∩ 334edeb) (3 batches)**
- Batch 2: `llm/backends/openai_compatible.py`, `llm/failover.py` (both touched by the fix commit's
  4xx-misclassification and down_since-reset fixes; both bucket B).
- Batch 3: `ecosystems/py.py`, `ecosystems/jvm.py`, `ecosystems/rust.py` (external_coordinates
  ecosystem-filter fix, matching pattern across all three; natural single logical task since the
  fix is one pattern applied three times).
- Batch 4: `sandbox/containerstats.py`, `vcs/gitea.py`, `workers/baseline.py`, `state/digest.py`
  (TOCTOU retry fix, pagination fix, timeout/cancelled-retry fix, digest allow_nan fix — four
  independent small fixes, grouped only for batch-size efficiency; a worker could split further if
  it finds cross-contamination risk).

**Wave 2 — `cli.py`'s freshly-fixed functions (own dedicated treatment, do not fold into Wave 1)**
`cli.py` is 20,633 lines and was touched for 5 distinct fixes in `334edeb`
(`_gc_disk`, `_abort_impl`, `_quarantine_impl`, `_apply_stub_decisions`, `_emit_one_pr`). Each is
independently mutation-testable against its own existing test coverage (`tests/test_cli.py` is
confirmed bucket-A/Rule-12-disciplined) without touching the rest of the file.
- Batch 5: `_gc_disk` (inverted dry_run condition).
- Batch 6: `_abort_impl` (`--drain` vs `--now`).
- Batch 7: `_quarantine_impl` (status guard / lease-fence).
- Batch 8: `_apply_stub_decisions` (CAS UPDATE rowcount check).
- Batch 9: `_emit_one_pr` (PR-record-lost-on-partial-failure).

**Wave 3 — remaining 334edeb-touched files already in bucket A (re-verify existing proofs still
target current code, per Rule 12's "re-measure a routed finding at the moment you act on it" — a
prior discriminator may have been written against pre-fix behavior) (5 batches)**
- Batch 10: `graph/collisions.py`, `graph/infer.py` (PEP 440 `~=` fix; sequence/collision fixes).
- Batch 11: `orchestrator/runner.py`, `orchestrator/stubs.py` (budget-ledger wiring, `reservation_ttl_s`).
- Batch 12: `vcs/git.py`, `vcs/commits.py` (`_require_settled()` fix, rename-notation fix).
- Batch 13: `state/repository.py`, `state/projection.py`, `settings.py`, `migrations/_support.py`
  (last_error-clearing, canonical_json allow_nan, `_require_settled()` chain).
- Batch 14: `workers/base.py`, `workers/classify.py`, `workers/clone.py`, `workers/interrogate.py`,
  `workers/symbolindex.py`, `workers/contracts.py`, `workers/prwriter.py`, `workers/rdepverify.py`,
  `workers/buildgen.py`, `llm/client.py` (cancelled-vs-timeout conflation fix, common pattern across
  workers — one logical task since the fix is one shared bug class fixed the same way N times, per
  Rule 12's guidance to treat a class-sweep as one unit; a worker should split this if the class
  turns out not to share one discriminating mutation).

**Wave 4 — bucket A− (migrations, transitive-only coverage) (1 batch)**
- Batch 15: `migrations/v002_node_kind.py` through `v010_file_blobs.py` (the 7 files listed in
  §2's bucket A−) — one batch since these are small, structurally similar DDL-migration steps; the
  task is to add one targeted per-step discriminating mutation each, confirming `migrate(db)` (or a
  `steps=_STEPS_THROUGH(n)` slice) actually reddens.

**Wave 5 — bucket B files never touched by 334edeb, need genuine new mutation-proof work (6-7
batches, grouped by subsystem for locality)**
- Batch 16: `llm/backends/anthropic.py`, `llm/backends/bedrock.py`, `llm/backends/vertex.py`
  (sibling backends to the already-covered openai_compatible.py — natural to test all three
  against the same discriminating-mutation shape).
- Batch 17: `ecosystems/contracts/{avro,base,openapi,proto,shared_lib,thrift}.py` (6 files, one
  contract-parsing subsystem).
- Batch 18: `manifests/{base,cargo,gomod,gradle,maven,npm,python,unknown}.py` (8 files, one
  manifest-parsing subsystem).
- Batch 19: `ecosystems/base.py`, `ecosystems/go.py`, `ecosystems/js.py`, `ecosystems/unknown.py`
  (remaining ecosystem adapters).
- Batch 20: `rewrite/{astgrep,libcst_py,pipeline,tsmorph}.py` (4 files, one rewrite-engine
  subsystem).
- Batch 21: `orchestrator/memory_guard.py`, `orchestrator/registry.py`, `state/checkpoints.py`,
  `util/cgroup.py`, `graph/query.py`, `obs/events.py` (6 small, otherwise-unrelated files grouped
  purely for batch-size efficiency — a worker should treat each as its own discriminating-mutation
  task within the batch, not assume they share one).

**Wave 6 — bucket A files never touched by 334edeb, lower-priority spot-check (not full
re-verification; project history shows sustained Rule-12 discipline here, so sample rather than
exhaustively re-run) (3-4 batches)**
- Suggest sampling the largest/most load-bearing of the remaining ~40 bucket-A files:
  `orchestrator/budgets.py` (has its own admission-fuzz instrument per `test_budgets.py` — worth
  confirming the fuzz's own validity per Rule 12's "validate what it measures, not only that it
  fires"), `orchestrator/scheduler.py`, `models/enums.py`, `models/tasks.py`, `sandbox/worktree.py`,
  `bazel/*.py`. Batch into 3-4 groups of ~4-5 files.

**Wave 7 — `cli.py`'s remaining, non-334edeb surface**
Out of scope for a firm batch count in this pass. `cli.py` is 20,633 lines covering scan/transform/
build/verify wave commands, resume/continue, PR emission, and more, reached by 27 test files. A
full Rule-12 sweep of the rest of it is a substantial follow-on effort in its own right (rough
order-of-magnitude estimate: 6-10 further batches, one per command-group) and should be scoped as
its own item once Waves 0-6 land, rather than guessed at here.

**Proposed total: ~28 dispatch batches for the immediately scopeable work (Waves 0-6), plus a
disclosed, not-yet-sized Wave 7 for the rest of `cli.py`.**

## 4. Worktree-isolation flag (Rule 12's detached-worktree import-isolation gotcha)

Per CLAUDE.md Rule 12, the risk is specifically: a bare `.venv/bin/python <script>` run from
outside a worktree resolves `import fleet` to the **primary** checkout's `src/` via the editable
install's `.pth` file, silently testing unmutated code. The rule also documents three **safe**
(structurally immune) patterns: (1) `pytest` run with `cwd` inside the worktree (both
`pyproject.toml`'s `pythonpath = ["src"]` and `tests/conftest.py`'s `sys.path` insert resolve from
the worktree), (2) a standalone driver that pins `sys.path` absolutely or relatively with `cwd` ==
the mutated tree, and (3) `mypy` run with `cwd` inside the worktree (`mypy_path = "src"` resolves
from cwd).

Findings for this sweep:
- **No standalone (non-pytest) mutation-driver scripts exist yet** under `tests/` — I checked every
  `if __name__ == "__main__":` block (`tests/test_floor_rule_statements.py:1356`,
  `tests/test_workers_scan.py:1718`); both are `pytest.main([__file__, "-q"])` convenience
  wrappers, not custom drivers, so they inherit pytest's structural immunity.
- **Every batch above should still run inside its own fresh worktree** (per Rule 6's per-lane
  isolation requirement, not because pytest itself is unsafe) — this is for concurrency isolation
  between simultaneously-dispatched batches, not an import-isolation workaround. Provision via
  `tools/worktree/new-worktree.sh` (or `provision-existing.sh` for one already created) so
  `tools/bin/*` and `.venv` exist.
- **If any batch's worker writes a custom fuzz/battery driver instead of using bare `pytest`**
  (plausible for Wave 6's `orchestrator/budgets.py`, which already has a seeded-fuzz instrument per
  `test_budgets.py` — see its own "validate what it measures" warning cited in CLAUDE.md) — that
  driver MUST follow the full pin-and-assert protocol: run under a stripped environment (`env -i
  PATH=... PYTHONPATH="$WT/src"`), print and assert `fleet.__file__` starts with the worktree path
  BEFORE reading any result, and assert the mutation marker appears in
  `inspect.getsource(<mutated function>)`. This applies to any Wave that ends up needing a
  non-pytest driver, not to a specific file list, since none currently exists.
- **Safe in-place** (no worktree needed at all, if a worker is doing read-only reconnaissance
  rather than applying a mutation): reading source/tests, running the existing suite un-mutated.
  Any actual mutation write, however small, should go in a worktree per Rule 6's shared-mutable-
  state hazards (concurrent lanes, `pytest_sessionfinish`'s `BAZEL_ROOT` reaping, shared stash,
  etc.) even where import-isolation itself isn't the risk.

## 5. What this scoping pass explicitly did NOT do

- Did not run any mutation.
- Did not write or modify any test code.
- Did not verify, function-by-function, that bucket-A's existing "discriminating mutation" prose
  actually still discriminates against the *current* code (several of the cited files were
  modified again by `334edeb` after their mutation-proof docstrings were written — Wave 3 exists
  precisely to re-check this).
- Did not size Wave 7 (`cli.py`'s non-334edeb surface) — flagged as needing its own scoping pass.
