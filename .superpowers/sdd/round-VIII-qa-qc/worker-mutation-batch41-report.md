# Worker report: round VIII, §15.1 item 3, Wave 7.5 batch 41 — cli.py `BuildPipelineWorker`

## Status: DONE

- Branch: `agent/roundviii-mutation-batch41` (from `main`, worktree
  `/home/redmage/swe repo harness worktrees/wt-roundviii-mutation-batch41`)
- Commit: `e3dfe57` — "round VIII: §15.1 item 3 Wave 7.5 batch 41 -- mutation-proof cli.py
  BuildPipelineWorker (G5)"
- Not merged, not pushed, per the brief.

## Scope

`BuildPipelineWorker` (`src/fleet/cli.py:8005-8540`), all its methods: `__init__`,
`preconditions_hold`, `run`, `_buildgen_input`, `_buildverify_input`, `_publish`,
`_publish_module_lock`, `_attribute_hoist_break`, `_evidence` (staticmethod), `_handoff`.

## Reaching tests found, and what they already covered

- `tests/test_cli.py`: `_publish` (D26 idempotence-vs-worktree-droppings), `_publish_module_lock`
  (D27 crash-between-materialize-and-commit), and one narrow `run()` happy path exercising only
  `VERIFY_UNIT` remaining with a stubbed `buildverify` (the `migrated_test_count` copy-through
  test).
- `tests/test_workers_build.py`: `_attribute_hoist_break` (both the match and no-op cases,
  thoroughly), `_buildgen_input`, `_buildverify_input`.
- `tests/test_wave_composition_projects_mid_wave.py`: mentions `BuildPipelineWorker` only to
  monkeypatch the whole class out with a `_StubWorker` (`{"worker": "BuildPipelineWorker", ...}`
  dict fed to a patch helper) — it exercises the wave-composition driver's registration/dispatch
  plumbing, never this class's own internals. Excluded from the covering set for that reason.
- `tests/test_build_e2e.py`: grepped for `BuildPipelineWorker`, `_run_build_wave`,
  `BUILDGEN_STEP`/`Phase.BUILD` — every hit is about `stub_degrade_transform(phase=Phase.BUILD)`
  or generic phase bookkeeping, never a call into this class. Excluded.

**Covering set for verification: `tests/test_workers_build.py` + `tests/test_cli.py`.** These are
the only two files with a direct, non-stubbed call into `BuildPipelineWorker`'s own code; the
third file that names the class stubs it out entirely, and the fourth never names it at all.

## Untested branches identified and closed

None of the following were reachable through any existing test before this batch:

1. **`preconditions_hold` itself** (distinct from the two registered steps' own per-step
   preconditions, which the class's docstring calls out explicitly) — the `remaining_units is
   None` short-circuit, and the `Path(ctx.workdir).is_dir()` check for a genuine re-entry.
2. **`run()`'s `GENERATE_UNIT` failure short-circuit** — a failed generate must return via
   `_handoff` immediately, with `VERIFY_UNIT` never attempted.
3. **`run()`'s `build_checkpoint_rejected` regenerate branch**, both sub-branches: the checkpoint
   describes a tree `buildverify.preconditions_hold` rejects, so `buildgen` is asked to
   regenerate first — (a) regenerate succeeds and the real `buildverify.run` proceeds, (b)
   regenerate itself fails and `_handoff` must carry *that* failure, with `buildverify.run` never
   reached.
4. **`run()`'s `PUBLISH_UNIT` branch** — a publish-only re-entry (`GENERATE_UNIT`/`VERIFY_UNIT`
   already landed, neither owed) reports `partial` on a publish failure and `ok` on success.
5. **`_handoff`'s default-error synthesis** — a step reaching `_handoff` with no structured
   `WorkerError` must get one synthesized (`FailureClass.UNKNOWN`, `retryable=True`).

### A finding surfaced while writing test 5

`_handoff`'s `if error is None and status in ("failed", "timeout"):` synthesis branch turns out
to be **unreachable for `status in ("failed", "timeout")` specifically**: `WorkerResult`'s own
`_status_matches_its_evidence` validator (`src/fleet/workers/base.py`) already refuses to
construct a `WorkerResult` with `status` in `("failed", "timeout")` and `error=None` — so no such
`step` can ever reach `_handoff` in the first place. The branch *is* reachable, but only via a
`status="partial"` step carrying no error (partial has no such validator requirement), which
`_handoff` remaps to `"failed"` on the very next line before the synthesis check runs. The test
(`test_handoff_synthesizes_an_error_when_a_failed_step_carries_none`) and its docstring record
this exactly — first attempt (constructing with `status="failed"`) failed at
`WorkerResult(...)` construction with a `pydantic_core.ValidationError`, which is what surfaced
it.

## New tests (all in `tests/test_workers_build.py`, inserted after the `_attribute_hoist_break`
tests, before `test_the_sandboxed_command_is_network_none_and_named_after_the_attempt`)

1. `test_build_pipeline_preconditions_hold_requires_a_re_entry_and_a_live_worktree`
2. `test_run_generate_unit_failure_short_circuits_before_verify_ever_runs`
3. `test_run_checkpoint_rejected_regenerates_before_verify_on_a_stale_reentry`
4. `test_run_checkpoint_rejected_handoff_carries_the_regenerate_failure_not_verifys`
5. `test_run_publish_only_reentry_reports_partial_on_failure_and_ok_on_success`
6. `test_handoff_synthesizes_an_error_when_a_failed_step_carries_none`

## Mutation-proof discipline (Rule 12): backup / edit / diff-verify-nonempty / test / restore /
re-verify-identical

Backup taken once: `cp src/fleet/cli.py <scratchpad>/cli.py.orig`. Five mutations, each applied
directly to `src/fleet/cli.py`, diffed against the backup (confirmed exactly one non-empty,
targeted hunk each time), run against the relevant new test(s), then restored via `cp` from the
backup and diff-confirmed byte-identical before the next mutation:

| # | Mutation | Targeted test(s) | Result |
|---|----------|------------------|--------|
| 1 | `preconditions_hold`: `return False` → `return True` on `remaining_units is None` | test 1 | RED — `assert not True` |
| 2 | Checkpoint-rejected condition: dropped the `not` on `worker.preconditions_hold(...)` | tests 3, 4 | RED (both) — stub's `AssertionError("must not run...")` fired, proving the branch structure inverted |
| 3 | `"partial" if landed else "failed"` → always `"failed"` | test 5 | RED — `assert 'failed' == 'partial'` |
| 4 | `_handoff` synthesis: `retryable=True` → `retryable=False` | test 6 | RED — `assert False is True` |
| 5 | `if result.status != "ok":` → `if result.status == "ok":` (GENERATE_UNIT branch) | test 2 | RED — stub's `AssertionError("buildverify.run must not be called...")` fired |

After each restore, `diff -q` against the backup reported no differences. Final state of
`src/fleet/cli.py`: unmodified (`git status` shows only `tests/test_workers_build.py` changed).

## Verification run

- `tests/test_workers_build.py` + `tests/test_cli.py` (whole files, no `-k`/node-id filtering):
  **331 passed, 0 skipped**, `bazel disk` clean (peak 0.47 GiB / ceiling 6 GiB, 0 residual output
  bases).
- `python -m mypy` (no path arguments — the authoritative scope per `pyproject.toml`'s
  `packages = ["fleet"]`, which excludes `tests/`): `Success: no issues found in 132 source
  files`.
- `python -m ruff check tests/test_workers_build.py`: `All checks passed!`
- An informational ad-hoc `mypy tests/test_workers_build.py` (outside the project's actual gate,
  since `tests.*` sits under a `disallow_untyped_defs = false` override that an explicit-path
  invocation does not fully honor) showed 175 pre-existing errors on `main`'s copy of the file
  before this change and 175 after the fixups below — i.e. this change introduced zero net new
  ad-hoc-mypy noise once the generic-type-argument and `type: ignore[index]` (vs. the copy-pasted
  `[assignment]`) issues in the six new tests were corrected.

## Files touched

- `/home/redmage/swe repo harness worktrees/wt-roundviii-mutation-batch41/tests/test_workers_build.py`
  (+281 lines, six new tests; `src/fleet/cli.py` untouched — restored to `main` after every
  mutation).

## Correction (2026-09-14, post-review, appended per CLAUDE.md's "annotate, never rewrite")

**This does not overturn the spec-compliance/code-quality verdicts, the six new tests, or any of
the five mutation results above — only one stated *rationale* below was found false by review,
not by me.**

The original text above ("Reaching tests found, and what they already covered") claims for
`tests/test_build_e2e.py`:

> grepped for `BuildPipelineWorker`, `_run_build_wave`, `BUILDGEN_STEP`/`Phase.BUILD` — every hit
> is about `stub_degrade_transform(phase=Phase.BUILD)` or generic phase bookkeeping, never a call
> into this class. Excluded.

The reviewer (`.superpowers/sdd/round-VIII-qa-qc/review-mutation-batch41-report.md`) found this
conclusion does not follow from the grep: `test_build_e2e.py`'s `build()` helper (line 680) is
`runner.invoke(app, [..., "build", ...])` — a full, unmocked CLI dispatch — called at **86 sites**
across the file. `fleet build`'s command handler drives `_run_build_wave` (`cli.py:12721`), which
directly constructs and dispatches `BuildPipelineWorker(bazel_runner=BAZEL_RUNNER)` into
`WaveScheduler`. So every one of those 86 `build(...)` calls **does** run this class's real
`run()`/`_handoff`/`_publish` machinery in-process (with a `FakeBazel` swapped in underneath at
the command-execution layer, but the composite worker's own branching logic is real, unstubbed
code) — the class name and helper function name simply never appear as literal tokens in the test
file, because the wiring lives inside `cli.py`'s own command handler, not in the test. This is
exactly the "derive from the body, not from where the text sits" trap CLAUDE.md's Guardrail 6
names repeatedly: grep-for-symbol-name is not equivalent to reachability analysis when the call
happens through an indirect layer (here, the CLI entry point) that never spells out the callee's
name.

**Why this doesn't change the practical outcome for this batch:** no production code shipped in
this commit (`src/fleet/cli.py` is restored to `main` state, confirmed by `git show e3dfe57
--stat`), so there was nothing in `cli.py` for `test_build_e2e.py` to catch a regression in this
round. The reviewer also checked for and did not find any `test_build_e2e.py` scenario keyed to
the specific re-entry/checkpoint/`remaining_units` vocabulary the six new tests cover, and
separately confirmed via `src/fleet/orchestrator/runner.py:948-950` that a *fresh* `build()` e2e
call — which is what most of the 86 call sites are — never exercises `preconditions_hold`'s body
at all (the driver only calls it once a checkpoint already exists). So the covering-set decision
itself (`test_workers_build.py` + `test_cli.py`, for verifying this batch's new tests) is still
adequate in outcome. What was wrong is narrower and more consequential than it sounds: the
*stated reason* ("never a call into this class") is false, and a future round that touches this
class's production code must not reuse that sentence to justify skipping `test_build_e2e.py` — it
should instead reason (as the reviewer did) about which specific branches/scenarios that file's 86
`build()` calls do or don't exercise.

**Caught by review, not by me.** I grepped for literal symbol names and stopped there without
asking whether an indirect layer (the CLI command handler) could reach the class without ever
spelling its name in the test file — the same category of error CLAUDE.md's Guardrail 6 already
names for citation/derivation problems, here applied to a coverage-exclusion claim instead.
Flagging per the same measurement discipline: an unmeasured "no reaching test" claim, even one
whose practical consequence review judged low for this specific test-only batch, is not validated
by that low consequence, and is corrected here rather than left standing for a future round to
inherit.
