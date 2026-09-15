# Worker report: §15.1 item 3, Wave 7.5 Batch 42 — cli.py VerifyPipelineWorker

## Status: DONE

## Branch / commit
- Worktree: `tools/worktree/new-worktree.sh roundviii-mutation-batch42 main` (from `main` @ `e48fea3`)
- Branch: `agent/roundviii-mutation-batch42`
- Commit: see `git log -1` after commit below (not merged, not pushed, per brief)

## Scope
`src/fleet/cli.py`: `VerifyInput`, `VerifyOutput` (incl. `equivalence` property), the
`VerifyPipelineWorker` class as a whole (`__init__`, `preconditions_hold`, `run`, `_own_tests`,
`_rdeps`, `_absorb`, `_handoff`), and the module-level `_worker()` factory function.

## Reaching tests read
- `tests/test_build_e2e.py` — end-to-end coverage of `fleet verify` over `FakeBazel`. Drives
  `VerifyPipelineWorker.run()` for real (both `BuildverifyWorker` and `RdepverifyWorker` behind
  it), including a real buildverify failure (`test_a_verify_provider_reaching_rhi_in_an_earlier_
  wave_blocks_its_later_wave_dependent`), cache-mount threading through `_rdeps`
  (`test_phase_four_reuses_the_bazel_caches_phase_three_just_filled`), and sampled/full closure
  reporting (`_absorb`'s happy path). It never isolates `preconditions_hold`, `_handoff`'s
  synthetic-error branch, `_absorb`'s empty-`test_command` guard, `VerifyOutput.equivalence`
  standalone, the "already-landed, skip buildverify" resume branch, or `_worker()`'s two call
  shapes.
- `tests/test_cli.py` — had zero existing unit tests naming `VerifyPipelineWorker`,
  `VerifyInput`, or `VerifyOutput` before this batch (confirmed by grep). Established sibling
  pattern for this kind of composite-worker unit test already lives here (see
  `BuildPipelineWorker`/`TransformPipelineWorker` tests a few hundred lines up:
  `worker._workers[STEP] = <stub>` then `await worker.run(worker_ctx, payload)`), which this
  batch follows.
- `tests/test_stub_resolution_task79.py`, `tests/test_wave_composition_projects_mid_wave.py`,
  `tests/test_llm_backend_failover_attribution.py`, `tests/test_llm_cache_hit_attribution.py` —
  reference `VerifyPipelineWorker`/`VerifyOutput` only incidentally (revalidation-loop dispatch
  ordering, sink/payload-builder naming, `WorkerResult[VerifyOutput]` construction for LLM
  attribution tests) and add no coverage of this worker's own branches. Not exercised further
  here; nothing in this batch's diff could regress them (no `cli.py` changes) and this was
  confirmed by running the two files above in full.

## Why `tests/test_cli.py` + `tests/test_build_e2e.py` are the sufficient covering set
`tests/test_cli.py` is where the new unit tests live and is the direct, fast-running check on
every touched branch. `tests/test_build_e2e.py` is the one file that drives
`VerifyPipelineWorker.run()` through the REAL registered `BuildverifyWorker`/`RdepverifyWorker`
pair (via `get_worker`/`_worker`) end-to-end, so it is the regression check that the new stubbed
unit tests — which replace `worker._workers[...]` with fakes — have not silently diverged from
the real workers' actual output shapes (field names, `WorkerResult` validity, etc.). Together:
unit tests for the branches, e2e for the real integration. Both ran to completion, full files, no
`-k` filter.

## Untested branches identified and closed (Rule 12)
1. `preconditions_hold`: `remaining_units is None` → `False`, checked BEFORE the `is_dir` probe.
2. `preconditions_hold`: the `is_dir` probe is a REAL check, not a stand-in constant, once
   `remaining_units` is set.
3. `run()`: "already landed" resume path — `VERIFY_UNIT` not in `owed` must skip buildverify
   entirely (dispatch only rdepverify), never re-run `bazel build`/`bazel test`.
4. `run()`: `own is not None` guard — a buildverify `WorkerResult` with `status="ok"` and
   `output=None` (a `WorkerOutput | None` contract any `BaseWorker` implementer may return) must
   not crash on `own.steps`.
5. `_handoff`: `partial` → `failed` status remap, AND the synthetic `WorkerError` it fabricates
   when the step supplied none (`WorkerResult` validates `partial` may carry `error=None`; only
   `failed`/`timeout` require one).
6. `_handoff`: a REAL error from a `timeout`/`failed` step must be forwarded unchanged, never
   overwritten by the synthetic one.
7. `_handoff`: correct `remaining_units`/`completed_units` bookkeeping in both the
   before-VERIFY-lands and after-VERIFY-lands failure shapes.
8. `_absorb`: `StepRecord` appended only when `closure.test_command` is non-empty; a `None`
   `test_exit_code` maps to `-1`, never `0`.
9. `VerifyOutput.equivalence`: `FULL` when `report is None`; otherwise always the report's own
   derived value (never re-derived).
10. `_worker()`: `factory()` (no kwarg) when `runner is None`, `factory(runner=runner)` otherwise
    — verified via the constructed worker's own `_runner` attribute, not just "no exception".

## Mutation testing (Rule 12: backup / edit / diff-verify-nonempty / test / restore / re-verify-identical)
Backed up `src/fleet/cli.py` to a scratch copy, then applied 11 single-line mutations by exact
line-number + expected-current-line-text anchor (NOT a whole-file `str.replace`, precisely
because the run()/`_handoff`/`preconditions_hold` bodies here are near-duplicates of
`ScanPipelineWorker`/`BuildPipelineWorker`/`TransformPipelineWorker`'s own copies elsewhere in
`cli.py` — a first pass using unanchored text matches accidentally mutated
`TransformPipelineWorker`'s copy at line ~1340 instead and produced two false "mutation did not
redden" results; re-anchored on line number + exact-text assertion, verified unique via `grep -c`
first). Every mutation: (a) asserted the pre-mutation line text before writing, (b) asserted
`git diff --no-index` against the backup is non-empty after mutating, (c) ran the full new-test
selector in `tests/test_cli.py`, (d) restored from backup and asserted byte-identical via a
second `git diff --no-index` (all 11 confirmed `restore identical to backup: True`).

| # | Mutation | Result |
|---|----------|--------|
| 1 | `preconditions_hold`: `remaining_units is None` → `is not None` | RED — `..._checks_the_real_workdir...` fails on `present_ctx` |
| 2 | `preconditions_hold`: `is_dir` probe → constant `True` | RED — same test fails on `missing_ctx` |
| 3 | `run()`: `landed` filter `not in owed` → `in owed` | RED — 3 tests fail |
| 4 | `run()`: `VERIFY_UNIT in owed` → `not in owed` | RED — 4 tests fail |
| 5 | `run()`: `if own is not None:` → `if True:` | RED — `AttributeError: 'NoneType' object has no attribute 'steps'` |
| 6 | `_handoff`: `partial`→`failed` remap deleted | RED — `WorkerResult` `ValidationError` (partial with no completed units after remap loss) |
| 7 | `_handoff`: drops `error is None` from the synthesis guard | RED — real error replaced by synthetic, `is` identity assertion fails |
| 8 | `_absorb`: `if closure.test_command:` inverted | RED — 2 tests fail (phantom step / missing step) |
| 9 | `_absorb`: `exit_code=-1 if None else …` → `0 if None else …` | RED — `assert 0 == -1` |
| 10 | `VerifyOutput.equivalence`: ternary branches swapped | RED — 2 tests fail, `CLOSURE_SAMPLED` vs `FULL` |
| 11 | `_worker()`: ternary collapsed to always `factory()` | RED — `assert None is <sentinel>` |

All 11/11 mutations reddened at least one new test; zero were "unimported" (each asserted its
own exact pre-mutation line before writing, so a silent no-op is structurally impossible here)
and zero produced an implausible all-fail-every-test result (each mutation failed only the
directly-relevant new test(s), 6-10 out of the 10 new tests staying green throughout — ruling out
a module-import-time outage as the source of any red).

## Verification run
- `tests/test_cli.py -k "verify_preconditions or verify_run_skips or verify_run_tolerates or verify_handoff or verify_absorb or verify_output_equivalence or worker_factory_passes"`: **10 passed**
- `tests/test_cli.py` (whole file, no `-k`): **238 passed**
- `tests/test_build_e2e.py` (whole file, no `-k`): **89 passed**, clean `bazel disk` line, 0 xfail
- `ruff check tests/test_cli.py`: clean
- `mypy tests/test_cli.py`: identical error set to the pre-change baseline (58 errors, all
  pre-existing in unrelated code — e.g. `ExitCode` `Literal` comparison-overlap warnings and
  `PullRequestDraft(**dict)` kwargs-unpacking, none touching this batch's diff); zero new errors
  introduced by this batch (diffed baseline vs. post-change error lists directly, not by count
  alone)

## Diff
Only `tests/test_cli.py` changed (+388 lines, 10 new tests). `src/fleet/cli.py` is untouched —
this batch closes a mutation-coverage gap, not a behavior change.
