# Task 80 report — D125 fix: `_verify_impl` pre-seeds every gated wave's VERIFY `phases` row

**Status: DONE_WITH_CONCERNS** (concern is an environmental/infrastructure issue unrelated to
the fix — see "Concern" section at the end; all fix-relevant proof is clean).

Branch: `agent/roundvi-task80`. Worktree: `/tmp/claude-1000/-home-redmage-swe-repo-harness/
b47df65c-cc7a-4ced-adce-2c7fe42e6669/scratchpad/roundVI-task80/wt`. **Not merged to `main`.**

Commits:
- `74aedc4` — fix: D125, `_verify_impl` pre-seed (code + xfail removal)
- `69acb2f` — docs: ledger + criteria-plan updates

## What changed

`src/fleet/cli.py::_verify_impl` (function starts at line 11992 fresh in this worktree's `HEAD`),
between `waves = await _open_phase_waves(...)` and the `for index in waves:` dispatch loop.

**Before** (pre-fix, `b4bc8be`):
```python
for index in waves:
    members, blocked = await _gated_members(
        read_conn, run_id, index, only, predecessor=Phase.BUILD
    )
    withheld.update(blocked)
    if not members:
        continue
    driven.append(index)
    for repo_id in members:
        await repository.upsert_phase(
            run_id, repo_id, Phase.VERIFY, now=_now(), max_attempts=MAX_ATTEMPTS,
        )
    ... # _wave_is_breached, _dest_for, _prepare_verify, _run_verify_wave, etc.
```

**After** (`74aedc4`): the same `_gated_members` call is hoisted out and memoized into
`gated_by_wave: dict[int, tuple[tuple[str, ...], tuple[str, ...]]]`, computed once per open wave;
a second loop upserts the VERIFY `phases` row for every wave's `members` half only (never
`blocked`) before any wave dispatches; the original dispatch loop now reads
`members, blocked = gated_by_wave[index]` and its own redundant `upsert_phase` block is deleted.
`withheld.update(blocked)`, `if not members: continue`, and `driven.append(index)` are unchanged
in position and behavior. Everything downstream (`_wave_is_breached`, `_dest_for`,
`_prepare_verify`, `_run_verify_wave`, `stub_degrade_transform`, final statuses/exit-code) is
untouched — full diff is in the commit (`git show 74aedc4 -- src/fleet/cli.py`).

This is the exact shape ADR-0129 (`docs/DECISIONS.md`, landed `b4bc8be`) specifies, adapted from
ADR-0127's `_transform_impl` fix to `_gated_members`'s predecessor-phase (BUILD) gate: the
pre-seed only ever upserts the `members` half, never `blocked`, preserving `_gated_members`'s
documented invariant that a repo not yet BUILD-`SUCCEEDED`/`DEGRADED` gets **no** VERIFY phase row
at all.

Re-confirmed by reading fresh in this worktree, per the brief's "do not touch" list:
`WaveScheduler.admit()`/`status_of()` (scheduler.py:426,478) read `phases.status` fresh with no
wave-scoping/caching to invalidate; `ALLOWED_TRANSITIONS[PENDING]` already contains `BLOCKED`;
`orchestrator/reentry.py` untouched. None of these needed a change — ADR-0129 judgment call 2's
"no" is confirmed, not merely inherited.

`tests/test_build_e2e.py`: the `@pytest.mark.xfail(strict=True, reason="D125: ...")` marker
(previously at lines 1444–1459) removed from
`test_a_verify_provider_reaching_rhi_in_an_earlier_wave_blocks_its_later_wave_dependent`; no
assertion in the test body was touched.

## Old-fails/new-passes discriminator proof (Rule 12)

Method: backed up `src/fleet/cli.py` to a scratch file (not `git stash` — `refs/stash` is shared
repo-wide across worktrees per this round's own standing guardrail), reverted `cli.py` to
`git checkout HEAD --` (byte-identical to pre-fix `main`), ran the now-un-xfail'd test, confirmed
the failure reproduces D125's own recorded symptom, then restored the fix from the backup and
verified the restore was byte-identical (`md5sum` match + `diff` empty) before re-running.

**Pre-fix run (actual output, un-xfail'd test against `git checkout HEAD -- src/fleet/cli.py`):**
```
tests/test_build_e2e.py:1488: AssertionError
E       AssertionError: {'acme-lib-py': ('REQUIRES_HUMAN_INTERVENTION', []), 'acme-lib-ts': ('SUCCEEDED', []), 'acme-app-py': ('SUCCEEDED', []), 'acme-app-ts': ('SUCCEEDED', [])}
E       assert ('SUCCEEDED', []) == ('BLOCKED', ['acme-lib-py'])
E         At index 0 diff: 'SUCCEEDED' != 'BLOCKED'
FAILED tests/test_build_e2e.py::test_a_verify_provider_reaching_rhi_in_an_earlier_wave_blocks_its_later_wave_dependent
============================== 1 failed in 8.16s ===============================
```
`acme-app-py` reads `('SUCCEEDED', [])` — byte-for-byte the shape D125's ledger entry and the
xfail's own first red run recorded.

**Restore verification:** `md5sum` of the restored file matched the pre-revert backup exactly
(`30d6c37d80193474ff28c86859b3998e` both), and `diff` between them produced no output.

**Post-restore run (actual output, fix back in place):**
```
tests/test_build_e2e.py::test_a_verify_provider_reaching_rhi_in_an_earlier_wave_blocks_its_later_wave_dependent PASSED [100%]
============================== 1 passed in 8.09s ===============================
```

Same test, same assertions, pre-fix code fails with D125's exact recorded symptom / post-fix code
passes — the discriminating proof Rule 12 requires.

## D126-for-VERIFY measurement (required, step 3 — actually driven, not reasoned about)

Method: added a temporary test function to `tests/test_build_e2e.py` (same fixture as the landed
regression test — `transformed`, clean `build(fleet, "--no-sandbox")`, then
`bazel.fail[("build", DESTINATIONS["acme-lib-py"])] = 34`), but drove it across **two separate**
CLI invocations, `verify(fleet, "--wave", "0", "--rdeps-limit", "3")` then a second, separate
`verify(fleet, "--wave", "1", "--rdeps-limit", "3")`, printed the measured rows, ran it once, then
deleted it (mirroring round VI task-76 fix round 1's own one-off measurement for the
TRANSFORM-side D126, which was likewise never landed as a permanent test).

**Actual measured output (fixed code, two separate `fleet verify` invocations):**
```
TASK80_D126_MEASUREMENT FIRST_EXIT 7 SECOND_EXIT 7 ROWS [('acme-app-py', 'SUCCEEDED', '[]'), ('acme-app-ts', 'SUCCEEDED', '[]'), ('acme-lib-py', 'REQUIRES_HUMAN_INTERVENTION', '[]'), ('acme-lib-ts', 'SUCCEEDED', '[]')]
```

**Result: REPRODUCES, confirming ADR-0129's prediction — a VERIFY-side D126 residual exists.**
`acme-app-py` ends `SUCCEEDED` with `blocked_by == '[]'` after the second, separate `--wave 1`
invocation, never `BLOCKED`, even though `acme-lib-py` reached `REQUIRES_HUMAN_INTERVENTION` for
real in the first invocation. This is the identical shape D126 already names for TRANSFORM, now
independently confirmed for VERIFY by direct measurement rather than left as optional reasoning.
Per the brief's explicit instruction, **this is NOT fixed** — it is disclosed in
`docs/INTEGRATION_HONESTY.md`'s D125 entry as an open question for the controller, with no new
D-number self-allocated (mirrors how ADR-0127 flagged D125 itself as a controller question rather
than self-allocating it).

## Breach-test confirmation

`tests/test_prepare_before_admit.py::test_a_breached_verify_wave_cuts_no_worktree` run before and
after the fix, both times against a full pytest invocation (not just this one test) to catch any
interaction:

- **Pre-fix** (`git checkout HEAD -- src/fleet/cli.py`): `1 passed in 6.63s`
- **Post-fix** (restored, verified byte-identical via `diff`): `1 passed in 6.56s` (and again
  `1 passed` in the combined final run below)

ADR-0129's prediction holds: the test's `assert set(statuses.values()) <= {"PENDING"}, statuses`
is a subset check, unaffected by whether the pre-seed pass creates rows for one wave or every
gate-ready open wave.

## Full whole-file test results (no `-k` filter, CLAUDE.md §6 discipline)

**`tests/test_prepare_before_admit.py` — clean:**
```
tests/test_prepare_before_admit.py ...                                   [100%]
---------------------------------- bazel disk ----------------------------------
peak 1.03 GiB (ceiling 6 GiB) · residual output bases 0 bytes · repository cache kept 1054 MiB
-------------------------------- test coverage ---------------------------------
0 tests skipped this session — full collected coverage ran
============================== 3 passed in 13.46s ===============================
```
3/3 passed, `xfail: 0`, clean bazel-disk line.

**`tests/test_build_e2e.py` — 78 collected; the D125-relevant tests are green; 6-8 unrelated
real-Bazel tests failed on a pre-existing, worsening, shared-host disk-space exhaustion (see
"Concern" below), NOT a regression from this fix:**

Run 1 (first full-file run, mid-fix verification): `6 failed, 72 passed in 932.88s`.
Run 2 (re-run for a clean untruncated capture, ~15 min later): `8 failed, 70 passed in 709.76s`.

Failing test names, both runs, all confirmed by direct source read to (a) NOT call `verify()` /
`_verify_impl` anywhere in their body, and (b) NOT use the `bazel: FakeBazel` fixture (they use
`tmp_path`/`bazel_cache_home`/`bazel_registry` — real Bazel, real network/toolchain fetch):
`test_build_against_a_real_bazel`, `test_the_unknown_ecosystem_filegroup_builds_under_a_real_bazel`,
`test_two_rust_repos_in_one_wave_both_build`,
`test_two_js_repos_with_different_npm_dependencies_both_build`,
`test_two_python_repos_with_different_pypi_dependencies_both_build`,
`test_a_python_test_target_runs_and_passes_under_a_real_bazel`,
`test_the_resolved_lock_carries_the_transitive_closure`,
`test_a_dependencys_generated_package_is_on_the_branch_before_its_dependents_snapshot`.

The failure signature in every case is identical: `bazel`'s toolchain fetch/extract dying with
`java.io.IOException ... No space left on device` (one run also produced a downstream
`FileNotFoundError` on a file the same ENOSPC condition silently failed to write). `df -h /`
showed `591G size, 563-564G used, 2.0-2.1G avail, 100%` throughout — stable-to-worsening across
both runs, consistent with concurrent sibling lanes (this round has others landing on `main`
concurrently, per the dispatch brief) competing for the same host's disk, not with anything this
task's `_verify_impl` change touches. The specific failing set differed between the two runs
(6 vs 8, with the extra 2 in run 2 being additional real-Bazel tests, not new symptoms) — that
non-determinism is itself evidence of shared-resource exhaustion rather than a deterministic code
regression, which would fail the same way every time.

The single test this fix's regression-proof requires
(`test_a_verify_provider_reaching_rhi_in_an_earlier_wave_blocks_its_later_wave_dependent`) is
**not** among either run's failures — confirmed PASSED standalone multiple times above.

## Concern (why DONE_WITH_CONCERNS, not DONE)

`tests/test_build_e2e.py`'s full-file run could not be made to reach 78/78 green in this
environment due to host-wide disk exhaustion (2.0-2.1 GB free on a 591 GB volume, ~563-564 GB
used by processes outside this task's own worktree/workspace). Per CLAUDE.md §2's workspace
containment ("Read and Write operations are strictly bounded to this project directory") and this
round's cross-lane guardrails (never touch a sibling lane's scratch), I did not delete other
sessions'/lanes' `/tmp` state to try to force headroom, and pruning my own worktree's Bazel
repository cache would not have helped (the ENOSPC occurs during toolchain *extraction*, which
needs its own headroom regardless of what's already cached). This is an infrastructure condition
outside this task's remit to fix, not a code defect — evidenced by: none of the 8 failing tests
exercising `_verify_impl` or `verify()` at all, all 8 requiring real (non-`FakeBazel`) Bazel
dispatch, the identical ENOSPC signature in every failure, and the failing-test-count changing
between two consecutive runs purely as ambient disk pressure worsened. Recommend the controller
either re-run `tests/test_build_e2e.py` once host disk pressure clears, or treat this as a
standing environmental note for the round.

## Ledger/criteria-plan edits made

- `docs/INTEGRATION_HONESTY.md`'s `## D125` entry: heading moved from `OPEN, CONFIRMED (round VI
  task 78)` to `FIXED, LANDED (round VI task 80, `74aedc4`)`. Appended (not rewritten) a dated
  `**FIXED, 2026-09-08 (round VI task 80, ADR-0129).**` paragraph describing the fix and the
  discriminator proof, and a further dated paragraph disclosing the VERIFY-side D126 residual as
  CONFIRMED (measured rows included), named as an open controller question with no new D-number
  self-allocated — mirrors D123's own entry's disclosure of D126 for TRANSFORM.
- `docs/CRITERIA_PLAN.md`'s `## 14.` entry: appended a dated correction paragraph noting D125 was
  never previously listed among this entry's remaining gaps (re-derived fresh rather than trusted
  from the file's prior wording, per the brief's own caution), stating D125 is now fixed for the
  single-invocation case, and restating §12.14's remaining named gaps as: D124, D126
  (TRANSFORM-side), the now-confirmed VERIFY-side sibling of D126 (no D-number yet), and the
  still-undesigned transitive-stub-stacking mechanism. No prior text was rewritten — annotate-in-
  place convention followed throughout.
- No `docs/SPEC.md` §12 criterion wording was touched (Rule 14 does not apply here — this is a
  bugfix against an existing criterion, not a change to the criterion's text).
