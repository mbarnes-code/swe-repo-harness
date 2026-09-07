# Task 68 report — §37 stub-creation, Leg 2: BUILD-phase render

**Status: DONE**

**Branch:** `agent/roundvi-task68` (from `main` @ `cb77005`, not merged)
**Commit:** `47df7e1` — "feat: §12.37/§3.5 Leg 2 -- BUILD-phase stub render: workspace_deps
union + EMPTY_FAILING target (round VI task 68)"

## Pre-flight

Task-67 had already landed and merged to `main` (`fe46f0c`, plus its fix rounds through
`6f5b0c3`) before this task started, exactly as the dispatch note said. Used the real `stubs`
table shape directly; no raw-SQL fallback needed for anything except test seeding (no worker in
this tree writes a `stubs` row yet — `--stub-blocked` stays refused everywhere per ADR-0113
condition 2 — so every test here seeds rows via SQL, same convention `tests/test_build_e2e.py`'s
`_insert_stub_row` already established for Blocker C).

Re-checked every cited line number against `HEAD` (`cb77005`) before editing; several had moved
since the brief was written (e.g. `_unit_deps`'s stub-redirect query is at `cli.py:8253-8345`,
not `7713-7800`). No content mismatch beyond line drift.

## What was built

### 1. `cli._stub_workspace_deps` (new, `src/fleet/cli.py:8524-8613`)
For every `ACTIVE`, `PUBLISHED_ARTIFACT`-fidelity `stubs` row (`pinned_version IS NOT NULL`),
dedupes by `stub_coord_key`, reconstructs the coordinate from `coordinates` (ecosystem/grp/name),
constructs the `BuildUnit` the brief specifies, and calls
`ecosystems.for_ecosystem(coord.ecosystem).workspace_deps(stub_unit)` — the exact SPEC §3.5
item 1 mechanism, with zero new code inside any adapter or `workers/buildgen.py`.

**Judgment calls, disclosed as Agent Recommendations (not directives from the brief):**
- **Own query, not a shared refactor of `_unit_deps`.** The brief's "reuse, do not re-derive"
  refers to the `state = 'ACTIVE'` predicate shape; I did NOT refactor `_unit_deps` itself to
  share code, because the brief's own "Explicitly out of scope" list names `_unit_deps`'s
  stub-redirect branch as "already landed, correct, do not touch." `_stub_workspace_deps` runs
  its own `SELECT stub_coord_key, pinned_version FROM stubs WHERE run_id = ? AND state =
  'ACTIVE'` — same predicate, zero risk to the landed function.
- **`BuildUnit.ecosystem` = the coordinate's own ecosystem** (`coordinates.ecosystem`), not
  `facts[provider_repo_id].ecosystem` as the brief's pseudocode sketched. SPEC §3.5 item 1 itself
  says `ecosystems.for_ecosystem(coord.ecosystem)` — the coordinate's ecosystem, precisely — which
  is also correct for a polyglot provider whose non-primary published coordinate differs from its
  primary one. This also avoided threading `_RepoFacts` into `_run_build_wave`, which does not
  currently have it in scope.
- **`resolved_version` stamped from `pinned_version` directly**, bypassing
  `bazel.generators.reconcile_versions`/`resolve_workspace_deps`. This was a correctness necessity,
  not a style choice: an abandoned provider's coordinate is internally owned and therefore never
  contributes an `ExternalRequirement` from anything in a run (`_external_coordinates` skips any
  `coordinate.key in owned`), so MVS has nothing to reconcile against. Left at `None`,
  `render_module_bazel`'s own unresolved-version guard would raise outright for every
  maven-ecosystem stub (maven's `workspace_deps()` sets neither `attrs` nor `resolved_version`
  itself, by that adapter's own design). Verified this is harmless for the four lockfile-dialect
  adapters (py/js/rust/go): their `attrs` are always non-empty and their render path never
  consults `resolved_version`.
- Fails loud (`ValueError`) if a `PUBLISHED_ARTIFACT` stub's `stub_coord_key` has no
  `coordinates` row — structurally shouldn't happen (task-67's `_create_stub_records` only ever
  sets `pinned_version` from a real `coordinates.version` read), but Rule 11 says raise rather
  than silently drop.

### 2. `cli._union_workspace_deps` (new) + wiring
Merges `_stub_workspace_deps`'s output into the fleet's aggregate `workspace_deps`
(`_module_inputs(plans)`'s output), keyed identically (`ruleset, extension, repo_name,
coordinate.key`). Wired into `_run_build_wave` (computed once per wave, same shape as the
existing `hoist_watch`) → `_build_payloads` (new `stub_workspace_deps` parameter) → the `deps`
list every dispatch's `BuildInput.workspace_deps` carries. **Live and reachable in production
today, currently a no-op** — nothing yet writes an `ACTIVE` `stubs` row outside tests.

### 3. `bazel.generators.stub_failing_target` (new, `src/fleet/bazel/generators.py`)
**Bazel primitive chosen: a native `genrule`.** It needs no `load()` and renders identically for
every ecosystem, so it satisfies research-7 §5's preference for a single generic shim over a
per-adapter branch — no sixth `EcosystemAdapter` method was needed, so this did not need to be
reported BLOCKED. `cmd` echoes an explicit message naming the abandoned provider and its
coordinate to stderr, then `exit 1`, before ever touching the declared (never-produced) `outs`
file — a genuine build-time action failure, never a runtime-silent stand-in.

**Not wired into the live `_build_impl` dispatch pipeline this round.** Dependents of an
`EMPTY_FAILING` provider stay `BLOCKED` (SPEC §3.5 item 2, confirmed against task-67's write —
see below) and are never admitted into a BUILD wave, so there is no live caller for a
`third_party/stubs/<coord>` package materialized this way yet; wiring it in would mean
inventing a consumer this leg's SPEC scope does not name. Built and directly tested instead —
the same pattern task-67 used for its own not-yet-wired pieces (its docstring: "Built and
unit-tested directly instead, exactly as Blockers A/B/C's own predicates were before their CLI
surface existed"). Flagging this explicitly as an Agent Recommendation / scope call, not a
requirement I was given — the controller may want a future task to wire it once a real
`--stub-blocked` consumer path exists.

## Confirmed, not modified: task-67's DEGRADED-write fidelity gate

Per the brief's request, checked `state/repository.py::stub_degrade_transform` (task-67's landed
`RUNNING → DEGRADED` correction): it queries `_STUB_DEGRADE_ACTIVE_FIDELITY_SQL`, filters to
`stub_fidelity = 'PUBLISHED_ARTIFACT'` explicitly, and only degrades on a non-empty qualifying
set — an `EMPTY_FAILING`-only consumer is correctly left `SUCCEEDED` as-is (with its own comment
noting this "should never happen" per §3.5 item 2, since an `EMPTY_FAILING` target fails at build
time, but the method re-checks fidelity itself rather than trusting that invariant). **No defect
found; nothing patched.**

## Test proof (Rule 12)

- `tests/test_build_e2e.py::test_an_active_published_artifact_stubs_workspace_dep_reaches_module_bazel`
  (new): seeds an `ACTIVE`, `PUBLISHED_ARTIFACT` stub for `acme-commons-java`'s real
  `com.acme:commons` maven coordinate, pinned at a version (`9.9.9`) deliberately different from
  the provider's own scanned version (`1.2.0`), runs a real `fleet build --no-sandbox` (FakeBazel
  seam, real render), and asserts the generated `MODULE.bazel` contains
  `maven.install(name = "commons", version = "9.9.9")` and does NOT contain `"1.2.0"`. Maven was
  chosen deliberately: it is the one ecosystem whose `workspace_deps()` pins a version per
  artifact directly in the rendered tag, so "the stub's pinned_version reached MODULE.bazel" is
  checkable by substring — py/js/rust/go are lockfile-dialect and version-free in the tag itself.
  **Mutation proof:** commented out the `deps = _union_workspace_deps(...)` call (verified via
  `git diff --numstat --no-index` against a backup: 2 insertions/1 deletion — a genuine change,
  not a no-op); the test failed (RED) with `"commons"`/`"9.9.9"` absent from the rendered
  MODULE.bazel (only `guava` remained); reverted, re-ran, GREEN again. Old-passes/new-fails
  confirmed.
- `tests/test_bazel.py::test_real_bazel_fails_an_empty_failing_stub_target_with_an_explicit_message`
  (new, `@pytest.mark.integration`): builds `stub_failing_target`'s output for a synthetic
  never-published npm provider, writes it to a real `bazel_workspace`, runs a REAL
  `bazel build //<stub_dest>/...`, and asserts `returncode != 0`, the provider id and coordinate
  both appear in stderr, and the declared (never-produced) output file does not exist on disk.
  Passed against real Bazel 9.2.0.

## Exact test files run whole (no `-k`), pass counts

- `tests/test_workers_build.py` — 91 passed
- `tests/test_bazel.py` — 68 passed (includes the new real-bazel test)
- `tests/test_build_e2e.py` — 75 passed (includes the new stub-render e2e test; ~14m45s, real
  Bazel + real git)
- `tests/test_cli.py` — 194 passed (requested by the controller mid-task as extra assurance
  beyond the brief's required list, since `cli.py` was touched directly)
- `tests/test_lint_gate.py` — 7 passed, 1 pre-existing disclosed `UserWarning` (worktree-vs-
  populated-checkout scope caveat already documented in that file; unrelated to this change)
- `python -m mypy` (no path args, manifest-scoped): `Success: no issues found in 129 source
  files`
- `ruff check .`: `All checks passed!`
- Never exported `FLEET_*`; every pytest/mypy/ruff invocation ran under
  `env -i PATH="$PATH" HOME="$HOME"` with no stray env leakage.

## Files/functions touched

- `src/fleet/cli.py` — new section "§37 Leg 2 (round VI task 68)": `_stub_workspace_deps`,
  `_union_workspace_deps`; `_build_payloads` gained a `stub_workspace_deps` parameter and now
  merges it into `deps` before constructing `BuildInput`; `_run_build_wave` computes
  `stub_workspace_deps` once per wave and passes it through. `_unit_deps` and every other
  out-of-scope function: byte-for-byte untouched (confirmed by the diff — no other function's
  body appears in the commit).
- `src/fleet/bazel/generators.py` — new `stub_failing_target` (+ two module-level constants),
  added to `__all__`.
- `src/fleet/bazel/__init__.py` — re-exports `stub_failing_target`, matching the package's
  existing convention.
- `tests/test_build_e2e.py` — new helper `_insert_published_artifact_stub_row` + one new test.
- `tests/test_bazel.py` — new imports (`stub_dest`, `stub_failing_target`) + one new
  `@pytest.mark.integration` real-bazel test.

No `docs/` files touched (per the dispatch note's instruction to keep doc edits narrowly scoped
to what the brief asks for; the brief did not ask for a `docs/CRITERIA_PLAN.md` update, so none
was made — leaving that annotation to the controller).

## Concerns for the controller

1. **`stub_failing_target` is real, tested, but not wired into any live dispatch path.** This
   mirrors task-67's own precedent but is worth a controller decision: does §37's eventual
   closure need `EMPTY_FAILING` packages actually materialized into a fleet's monorepo tree
   (requiring a design decision about which dispatch scope — fleet-wide like root files, or
   something else), or is "renders correctly, real-bazel-tested standalone" sufficient for this
   leg given `--stub-blocked` stays refused regardless?
2. **Two deliberate deviations from the brief's literal pseudocode** (own query instead of
   sharing `_unit_deps`'s; coordinate-derived ecosystem instead of `_repo_facts`-derived) are
   documented above and in the code's own docstrings — flagging both explicitly since CLAUDE.md's
   Directive Authority rule requires agent judgment calls be labeled, not presented as the
   brief's own requirement.
3. No new §12 criterion closed by this leg alone (Leg 2 is a component of §37's still-open
   "stub-creation logic" bundle per `docs/CRITERIA_PLAN.md`'s `## 37.` entry) — task-69 (refusal-
   site removal, `_eligible_build_units` widening) is the remaining piece before §37/§12.37 can
   be re-measured.
