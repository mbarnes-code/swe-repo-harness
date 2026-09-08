# Task 78 report — D125: confirm or refute `_verify_impl`'s cross-wave `blocked_by` gap

**Status: DONE**

**Branch:** `agent/roundvi-task78`, from `main` @ `09f8a5a`. This is a measurement task only — no
fix was designed or built, per the brief's explicit scope boundary.

## Answer, up front

**CONFIRMED.** `_verify_impl`'s wave loop has the same byte-for-byte lazy, per-wave `upsert_phase`
shape `_transform_impl` had before ADR-0127's fix. A real fixture (no hand-seeding) reproduces the
identical D123-shaped symptom for VERIFY: a provider abandoned in an earlier wave does not
propagate `blocked_by` to a direct dependent scheduled into a later wave, within one `fleet
verify` invocation.

## Step 1 — re-verified `_verify_impl`'s wave loop against current `HEAD` fresh

The brief's cited line range (`cli.py:11687-11702`, from research-43/ADR-0127) has shifted to
`cli.py:11968-11983` at this task's `HEAD` (`09f8a5a`) — re-located by grepping
`async def _verify_impl` (found at `cli.py:11932`) and reading forward. The shape is unchanged:

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
    ...
    report = await _run_verify_wave(...)   # containment (`_contain`) can fire IN HERE
```

Still lazy, still per-wave, still inside the dispatch loop — **not** yet given `_build_impl`'s
upfront PASS 1 pre-seed. It has not been changed since research-43 read it.

## Step 2 — fixture built

`tests/test_build_e2e.py::test_a_verify_provider_reaching_rhi_in_an_earlier_wave_blocks_its_later_wave_dependent`
(added after `test_a_degraded_repo_at_phase_four_with_no_rhi_repo_exits_7`, which is where VERIFY's
own e2e tests live in this tree — there is no separate `test_verify_e2e.py`). It mirrors D123's own
discovery fixture (`tests/test_transform_e2e.py::
test_a_provider_failing_in_an_earlier_wave_blocks_its_later_wave_dependent_in_one_run`) as closely
as `fleet verify`'s own PASS structure allows:

- `acme-lib-py` (wave 0) reaches `REQUIRES_HUMAN_INTERVENTION` through a REAL VERIFY dispatch — no
  hand-seeding — via the injected `FakeBazel` seam this file's Phase-3 failure test already uses
  (`bazel.fail[("build", "py/acme_lib_py")] = 34`), injected only AFTER Phase 3's own `fleet build`
  completed clean (`transformed(fleet)` then `build(fleet, "--no-sandbox")`, asserted `SUCCEEDED`),
  so `_gated_members`'s predecessor=BUILD gate admits every repo into VERIFY.
- `acme-app-py`, `acme-lib-py`'s real direct dependent, is scheduled into wave 1 (the same ordering
  graph `tests/test_scan_e2e.py`'s `waves["acme-lib-py"] < waves["acme-app-py"]` assertion already
  proves, since `wave_members` is phase-independent).
- ONE `fleet verify --rdeps-limit 3` call drives both waves — no `--wave` scoping.
- Assertion is on `phases.status`/`blocked_by` for every repo at `phase = 4`.

## Step 3 — run, actual measured result

First run (assertion written for the SPEC-correct/fixed shape, no xfail yet):

```
AssertionError: {'acme-lib-py': ('REQUIRES_HUMAN_INTERVENTION', []), 'acme-lib-ts': ('SUCCEEDED', []),
'acme-app-py': ('SUCCEEDED', []), 'acme-app-ts': ('SUCCEEDED', [])}
assert ('SUCCEEDED', []) == ('BLOCKED', ['acme-lib-py'])
```

`acme-app-py` reads `SUCCEEDED` / `blocked_by == []` where §12.14's blast-containment clause
requires `BLOCKED` / `['acme-lib-py']`. This is byte-for-byte the same wrong shape ADR-0127's own
discovery fixture measured for TRANSFORM before its fix
(`('acme-app-py','SUCCEEDED','[]')`). **CONFIRMED** — not merely a structural suspicion.

The two TypeScript-ecosystem repos (`acme-lib-ts`, `acme-app-ts`) correctly read
`('SUCCEEDED', [])` — an unrelated dependency chain, proving the fixture's `blocked_by` failure is
specific to the real `acme-app-py -> acme-lib-py` edge and not a fixture-wide breakage.

## Step 4 — landed as a known-failing regression test, not a fix

Per the brief's CONFIRMED branch: the test above is now `@pytest.mark.xfail(strict=True,
reason="D125: ...")`, following this project's own precedent
(`tests/test_baseline_ok_exclusion.py`'s D116 marker). Re-run:

```
tests/test_build_e2e.py::test_a_verify_provider_reaching_rhi_in_an_earlier_wave_blocks_its_later_wave_dependent XFAIL
1 xfailed in 8.93s
```

`strict=True` means it hard-fails the instant `_verify_impl` is fixed without deleting the marker.

## Step 5 — `docs/INTEGRATION_HONESTY.md` updated per Guardrail 7

D125's heading changed from `OPEN, NOT YET MEASURED WITH A FIXTURE` to `OPEN, CONFIRMED (round VI
task 78)`. The original suspicion text (found-by, the read shape, "why disclosed as OPEN", "not yet
built") is left completely unedited, per Guardrail 7's annotate-never-rewrite convention — a new
dated paragraph block is appended below it (root cause restated for VERIFY, the fixture built, the
measured result, consequence, the regression test now landed, and what remains not-yet-built: the
fix itself, and whether D126's separate-invocation residual also applies to VERIFY — explicitly
out of scope, not investigated).

## Step 6 — whole-file / whole-suite verification

- `tests/test_integration_honesty_citations.py`: **70 passed** (whole file, no `-k`) — the D125
  heading/paragraph edit did not break any citation binding.
- `tests/test_build_e2e.py` (whole file, no `-k`, includes real-Bazel section 7): **70 passed, 1
  xfailed, 7 failed in 976.36s**. The 7 failures are ALL inside section 7's real-Bazel tests
  (`test_build_against_a_real_bazel`, `test_the_unknown_ecosystem_filegroup_builds_under_a_real_bazel`,
  `test_two_js_repos_with_different_npm_dependencies_both_build`,
  `test_a_python_test_target_runs_and_passes_under_a_real_bazel`,
  `test_the_resolved_lock_carries_the_transitive_closure`,
  `test_re_resolving_the_same_specs_produces_a_byte_identical_lock`,
  `test_a_real_bazel_lock_publish_and_a_real_sandboxed_build_happen_in_the_same_run`) —
  network/registry-resolution tests entirely unrelated to this task's diff (different fixture
  repos, different code path: real `bazel`/npm/PyPI resolution, not my `FakeBazel`-driven VERIFY
  fixture). **Verified pre-existing and not caused by this change:** a fresh, unmodified detached
  worktree of `main` (no changes of mine at all) reproduces `test_build_against_a_real_bazel`'s
  identical failure shape (`3 repo(s) ended REQUIRES_HUMAN_INTERVENTION: acme-app-ts, acme-lib-py,
  acme-lib-ts`, exit 7 instead of 0) — this worktree was created and destroyed solely for this
  verification and left no trace on `main`. The 70 passed / 1 xfailed are the tests this task's
  diff can affect; none of them regressed.
- `ruff check tests/test_build_e2e.py`: All checks passed.
- `mypy` (no path args, whole manifest): Success, no issues found in 129 source files.

## Commits

- `<see git log on this branch>`: docs — D125 heading flipped to `OPEN, CONFIRMED` with a dated
  paragraph; test — the new `xfail(strict=True)` regression fixture in `tests/test_build_e2e.py`.

## Scope discipline

Did not touch `_transform_impl`, `WaveScheduler`, `ALLOWED_TRANSITIONS`, or
`orchestrator/reentry.py`, per the brief's constraints. No fix was designed or attempted for
`_verify_impl` itself — that remains a separate, undispatched future task, exactly as this entry's
own "Not yet built" paragraph now states.

## Files touched

- `docs/INTEGRATION_HONESTY.md` — D125 heading + dated confirmation paragraph.
- `tests/test_build_e2e.py` — new `xfail(strict=True)` regression fixture.
