# Worker report: §15.1 item 3, Wave 5 Batch 21 — mutation-proof 6 small unrelated files

## Status: DONE

Branch `agent/roundviii-mutation-batch21`, commit `f4ed435`, isolated worktree at
`/home/redmage/swe repo harness worktrees/wt-roundviii-mutation-batch21`. No merge, no push.

## What changed

Six test files, one new test each — `tests/test_memory_guard.py`, `tests/test_registries_stateless.py`,
`tests/test_checkpoints.py`, `tests/test_cgroup.py`, `tests/test_graph_build.py`, `tests/test_obs.py`.
0 production code changes (`git status --short` after commit shows only these six test files;
`git diff --stat` for the commit is `6 files changed, 205 insertions(+)`, no deletions).

Per the brief's instruction, each of the 6 files was treated as its own independent task — read
the source file and its own test(s), and **do not assume any two share a mutation shape.** They
did not: each file's own most load-bearing untested branch was a distinct kind of gap (a numeric
boundary, a registration invariant, a redundant defensive check, a malformed-input check, an
ordering guarantee, and an exception-wrapping branch — see table below).

## Per-file results (all 6, independent)

| File | Load-bearing untested branch found | New test | Mutation applied | Result |
|---|---|---|---|---|
| `orchestrator/memory_guard.py` | `_tick()`'s own docstring: "`>=`, not `>`, for both" ceilings. Every existing test used readings comfortably above or below a ceiling, never exactly AT one. | `test_a_sample_sitting_exactly_at_either_ceiling_is_a_breach` | Flip both `total_bytes >= self._host_ceiling_bytes` and `own_bytes >= self._own_ceiling_bytes` to `>` | 1 failed (new test) / 14 passed |
| `orchestrator/registry.py` | `register_worker`'s duplicate-name guard — the module's own docstring calls it out ("two workers answering to one name means the run's provenance is a lie") — had **zero** coverage; every existing sweep walks the real registry, which is unique by construction and cannot express a collision. | `test_register_worker_rejects_a_duplicate_name_from_a_different_class` | `if existing is not None and existing is not cls:` → `if False:` | 1 failed (new test) / 6 passed |
| `state/checkpoints.py` | `load()` carries TWO independent `MODEL_MISMATCH` checks — one against the SQL `model_name` column (covered), one against the `model` field embedded in the JSON envelope itself (uncovered; unreachable via `save()`+`load()` alone since both are always written identically). | `test_an_envelope_model_name_disagreeing_with_the_column_invalidates` | Delete the `if envelope["model"] != expected:` block | 1 failed (new test) / 8 passed |
| `util/cgroup.py` | `resolve_own_cgroup_v2_relpath`'s final check (path after `0::` must start with `/`) was untested — the adjacent line-count check (`!= 1`) is exercised both directions (0 and 2 lines), but never with a well-formed count (exactly 1) and a malformed path. | `test_a_v2_line_whose_path_does_not_start_with_a_slash_is_malformed` | Delete the `if not relpath.startswith("/"):` block | 1 failed (new test) / 8 passed |
| `graph/query.py` | `blast_radii`'s own docstring: "Insertion-ordered by sorted `NodeRef`, so the write order is reproducible." The only existing assertion is dict `==`, which Python defines to ignore key order — so this reproducibility property was never actually checked. | `test_blast_radii_is_insertion_ordered_by_sorted_node_ref` | `for ref in graph.node_refs()` → `for ref in reversed(graph.node_refs())` | 1 failed (new test) / 25 passed |
| `obs/events.py` | `emit()`'s `except Exception` wrapper around `_build_row()` — specifically the naive-datetime rejection inside it — was the one failure branch nothing exercised; every other failure test injects a broken SINK (jsonl/SQL) after a row was already built, none makes row-*building* itself raise. | `test_a_naive_now_fails_the_build_step_and_is_reported_without_raising` | Delete the `if moment.tzinfo is None: raise ValueError(...)` check | 1 failed (new test) / 38 passed |

`memory_guard.py`'s own e2e fuzz suite (`tests/test_memory_guard_e2e.py`, 4 tests) was read per the
brief's explicit instruction to check it for genuine discrimination. It is: each test targets a
specific documented property from §12.22/§11.3 (a breach confined to pool children/containers that
a `getrusage`-only sampler provably cannot catch, a low-reading pass-through, an orchestrator-RSS-only
breach) rather than being vacuous presence checks. No changes made there — out of scope (batch targets
the 6 files listed in the brief, and this file's own gap was in the unit-level `test_memory_guard.py`).

## Finding, disclosed not fixed (obs/events.py)

While writing the naive-datetime test, found that `emit()`'s `except Exception` branch around
`_build_row()` returns `EmitResult(...)` **without** a `failures=` argument — unlike the jsonl/sql
failure paths later in the same function, which both pass `failures=tuple(failures)`. So for a
build failure specifically, the returned `EmitResult.failures` is `()` and `EmitResult.ok` reads
`True`, even though the failure genuinely occurred. It is **not silently dropped** — the emitter's
own `failed_emits`/`failures` deque are updated and `_surface()` logs it — but the per-call return
value disagrees with the emitter's own bookkeeping about whether that one call failed. The new test
asserts current behavior as-is (`result.failures == ()`) with an inline `NOTE` explaining this, and
separately asserts the emitter-level bookkeeping (`emitter.failed_emits == 1`,
`emitter.failures[-1].sink == "build"`) to prove the failure is not actually hidden. Not fixed here:
out of this task's scope (test-writing only) and Rule 3 (surgical changes) — flagging for the
controller to decide whether `EmitResult`'s omission is a defect worth its own D-number.

## Mutation results (backup / edit / diff-verify-nonempty / test / restore / re-verify-identical)

Each of the 6 source files was backed up individually, mutated in isolation, checked with
`git diff --numstat --no-index <backup> <mutated>` for a non-empty diff, then the corresponding
test file was run (only the new test reddened in every case; all pre-existing tests in that file
stayed green — see table above for per-file pass counts), then the file was restored from its
backup and `diff <backup> <restored>` confirmed byte-identical (empty output) before moving to the
next file. All six restores confirmed identical. After all six mutation cycles, `git status
--porcelain` in the worktree showed only the six test files modified — no source-file drift.

Final full run of the 6 test files together (plus `test_memory_guard_e2e.py` for completeness):
**109 passed** (`tests/test_memory_guard.py`, `tests/test_memory_guard_e2e.py`,
`tests/test_registries_stateless.py`, `tests/test_checkpoints.py`, `tests/test_cgroup.py`,
`tests/test_graph_build.py`, `tests/test_obs.py`).

## Verification

- `pytest tests/test_memory_guard.py tests/test_memory_guard_e2e.py tests/test_registries_stateless.py tests/test_checkpoints.py tests/test_cgroup.py tests/test_graph_build.py tests/test_obs.py -q`
  → **109 passed**.
- `ruff check` on all six touched test files → all checks passed (one `E501` introduced during
  authoring, caught and fixed before commit).
- `mypy` (no path arguments, so `pyproject.toml`'s `packages = ["fleet"]` sets the scope) →
  `Success: no issues found in 132 source files`. No `src/` files were touched, so this is
  confirmatory rather than load-bearing here.
- `git status --short` after commit: clean (only the commit itself).

## Branch / commit

Branch: `agent/roundviii-mutation-batch21` (from `main`, HEAD `4f57005` at worktree creation).
Commit: `f4ed435`. Not merged, not pushed.
