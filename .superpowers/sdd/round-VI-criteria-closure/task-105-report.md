# Task 105 report — §12.11 Leg A: adapter-side native-baseline capability

Round VI, thirty-seventh wave. Branch `agent/roundvi-task105`, off `main` at `6ae84c7`.

## What was built

**`NativeBaseline` model** (`src/fleet/models/build.py`, added before `BuildPlan`): a pure data
declaration, mirroring `Resolution`'s existing "adapter declares, driver executes" shape.

```python
class NativeBaseline(FleetModel):
    build_argv: list[str]   # default []; the native build/install command, argv only
    test_argv: list[str]    # min_length=1; the native test command, argv only
    test_unit_count: int    # ge=0; per-BuildUnit granularity, matching test_targets()
```

Both commands are documented as running with the unit's own **pre-migration** repo root as cwd
(never `unit.dest`, which doesn't exist yet when a baseline runs — §3.1 step 1, before Phase 2).

**`EcosystemAdapter.native_baseline(unit: BuildUnit) -> NativeBaseline | None`**
(`src/fleet/ecosystems/base.py`): a **non-abstract** default method returning `None`, added next
to `resolution()` in the "defaults" section. Non-abstract per the brief/ADR-0135's own reasoning:
`tests/fixtures/adapters/ruby_ecosystem.py` (§12.34's fixture) must keep working with zero edits.

**Shared helper `native_test_unit_count(test_targets: Sequence[BuildTarget]) -> int`**
(`src/fleet/ecosystems/base.py`, exported in `__all__`): counts only entries whose `rule` ends in
`"_test"` — the actual Bazel test-rule convention every adapter's test macro uses
(`py_test`/`java_test`/`js_test`/`rust_test`). This is the fix for a hazard I found while
implementing: `JsAdapter.test_targets()` returns a compile-only `ts_project` alongside its one
`js_test`, so a naive `len(test_targets(unit))` would report **2** test units for something
`bazel query 'tests(//<dest>/...)'` would only ever call **1** — reintroducing ADR-0135 ruling
1's exact false-fire hazard (H1 in research-53) one level down, inside the very fix meant to
prevent it. `native_test_unit_count` is ecosystem-neutral (lives in `base.py`, not duplicated per
adapter) and ready for `rust.py`/`jvm.py` to reuse if/when they implement this capability.

**Implemented for PyPI and NPM** (`src/fleet/ecosystems/py.py`, `src/fleet/ecosystems/js.py`) —
confirmed against current `HEAD` to be exactly the two ecosystems
`tests/test_scan_e2e.py::FIXTURE_REPOS` (reused by `tests/test_transform_e2e.py` and
`tests/test_baseline_ok_exclusion.py`) exercises, plus one empty repo:

* **PyPI**: `build_argv=["python3", "-m", "pip", "install", "-e", "."]`,
  `test_argv=["python3", "-m", "pytest"]`.
* **NPM**: `build_argv=["npm", "install"]`, `test_argv=["npm", "test"]` — plain `npm`,
  deliberately, not the `pnpm` this adapter's `resolution()` uses for the *migrated* monorepo
  build (ADR-0135 ruling 3: a native baseline probes the repo's own original tooling).

MAVEN/GRADLE/GO/CARGO/UNKNOWN are left on the inherited default (`None`) — a disclosed residual
per ADR-0135, not built speculatively (Rule 2/YAGNI).

## Tests added (`tests/test_ecosystems.py`, all at the adapter level, no CLI/e2e)

1. `test_py_native_baseline_reports_a_plausible_build_test_and_count` — unit with 3 test files:
   `test_unit_count == 1 == len(test_targets(unit))`.
2. `test_py_native_baseline_reports_zero_for_a_unit_with_no_native_tests`.
3. `test_js_native_baseline_does_not_double_count_the_compile_only_ts_project` — asserts
   `len(test_targets(unit)) == 2` (the hazard's precondition) while
   `native_baseline(unit).test_unit_count == 1` (the fix).
4. `test_js_native_baseline_reports_zero_for_a_unit_with_no_native_tests`.
5. `test_an_unimplemented_adapter_inherits_the_default_none_native_baseline` — parametrized over
   `sorted(set(Ecosystem) - {PYPI, NPM})` (5 cases: cargo/go/gradle/maven/unknown).
6. `test_a_fixture_adapter_for_a_brand_new_language_inherits_the_default_none_native_baseline` —
   instantiates `tests/fixtures/adapters/ruby_ecosystem.py`'s
   `make_ruby_ecosystem_adapter(Ecosystem.PYPI)` directly (no registration, no decoy-enum
   monkeypatch needed since nothing calls `register()`), proving the exact guarantee the
   non-abstract design protects — zero changes to that fixture file, confirmed by `git diff
   --stat` showing it untouched.

`tests/test_ecosystems.py` full file: **102 → 108 tests, all pass.**
`tests/test_new_language_touchpoints_e2e.py` (§12.34's own zero-`src/fleet/`-diff assertion over
its fixed historical SHA range, unaffected by this change since it diffs a past commit range, not
`HEAD`): **36/36 pass**, confirming the ruby fixture path is unaffected end-to-end too.
`tests/test_config_keys_are_read.py` (H4's ratchet, unaffected by Leg A): **42/42 pass.**

`ruff check`: clean on all 5 changed files. `ruff format --check`: the changed files show only
ONE pre-existing reformat opportunity (`tests/test_ecosystems.py`, verified via `git stash` to
predate this task's edit, at a line far from anything I added) — nothing introduced by this task.
`mypy` (whole-manifest, no path args): **clean, 130 source files.**

## Scope discipline (per the brief's boundaries)

Did NOT touch: `SCAN_UNITS`, any worker, `_scan_rows`, the `BaselineRed` finding kind,
container/toolchain provisioning, or `tests/test_baseline_ok_exclusion.py`'s xfail — all
confirmed absent from `git diff --stat` (5 files: 2 ecosystem adapters, the shared ABC/helper,
the model, and the adapter test file).

## Concerns for Leg B (the struct shape it depends on)

**`NativeBaseline` fields, exact and final as landed:**

```python
build_argv: list[str]     # default []; argv only, run with the repo's PRE-migration root as cwd
test_argv: list[str]      # min_length=1
test_unit_count: int      # ge=0
```

* `test_unit_count` is **NOT** a raw test-case or test-file count. It is derived via
  `native_test_unit_count(self.test_targets(unit))` — i.e., it is deliberately computed by
  reusing the ADAPTER'S OWN migrated-side `test_targets()` method (which already knows the exact
  per-`BuildUnit` collapsing convention), not by independently inspecting the native source tree.
  This means today's implementation cannot distinguish "0 native tests" from "the native repo
  genuinely has a different test-suite shape than what would be migrated" — it always answers
  with the SAME shape the migrated side will produce, by construction. This is deliberately safe
  (it can never false-fire `test_count_regressed`) but means Leg C/E's red-path fixture (research-
  53's own note: "a fixture with a genuinely broken native build" so `baseline_ok=0` is
  expressible) will need `native_baseline`'s **execution** result (actually running `test_argv`
  in a container and reading exit code / real output), not this static declaration, to ever
  disagree with a healthy migration. I did not build that — it's explicitly Leg B/C's job — but
  flagging it now since it means `test_unit_count` as declared here is a ceiling/expected-shape
  value, and Leg B's actual `repos.baseline_test_count` write should come from the CONTAINER RUN's
  observed count (parsed from `test_argv`'s output), falling back to this declared value only if
  the run cannot report one such as a crash before any test collects.
* `build_argv`/`test_argv` intentionally carry NO cwd/path information (not even `unit.dest`) —
  Leg B's worker owns deciding what directory to run them in (the repo's own cloned worktree
  root, per §3.1 step 1, before any relocation).
* Neither field carries a timeout or env overlay (unlike `Resolution`, which has both) —
  deliberately deferred to Leg D's config surface (`preflight.baseline_build.timeout_s` already
  exists in `settings.py`; a per-command env was judged out of scope for Leg A, YAGNI).

## Status

**DONE.**

Commit range: `agent/roundvi-task105` branch, one commit on top of `main`@`6ae84c7`
(see `git log agent/roundvi-task105 -1` in the worktree for the exact SHA — not yet created at
the time this report was drafted; committed immediately after).

Test summary: `tests/test_ecosystems.py` 108/108 pass (6 new tests, one parametrized ×5 cases);
`tests/test_new_language_touchpoints_e2e.py` 36/36 pass; `tests/test_config_keys_are_read.py`
42/42 pass; ruff clean; mypy clean (130 files, whole-manifest).

Concerns: see "Concerns for Leg B" above — the struct shape is landed and stable, but its
`test_unit_count` field is a static, migrated-side-matching declaration, not yet a real measured
native test count (that requires actually running `test_argv`, which is Leg B/C's job).
