# Task 68 report — §37 stub-creation, Leg 2: BUILD-phase render

**Status: DONE** (original landing below; superseded/corrected by the fix-round section at
the end of this file after a task-scoped review found a real scope gap — see there for the
current, accurate status)

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

---

## Fix round (controller review) — DONE

**Commit:** `474d1bd` — "fix round: §12.37/§3.5 Leg 2 -- materialize the stub PACKAGE, fix label
convention, real-Bazel proof (round VI task 68, controller review)"

The review's verdict was correct: "BUILD-phase render" was not actually done. SPEC §3.5 item 1
names two halves in one sentence — the `MODULE.bazel` `workspace_deps()` declaration (built) and
a real `BUILD.bazel` PACKAGE at `stub_dest(coord_key)` (missing). Without the second half,
`_unit_deps`'s already-landed redirect substituted a label that resolved to nothing under real
Bazel — invisible because every stub test, including this leg's own first landing, ran only under
`FakeBazel`.

### I1 (major) — the missing package, built and wired

New `cli._stub_package_files(conn, run_id) -> tuple[SupportFile, ...]`, using a new shared
projection `cli._active_stub_facts` (also used by the rewritten `_stub_workspace_deps`, so the two
functions can never disagree about which `stubs` rows are live). For every `ACTIVE` stub row,
keyed on `stub_fidelity`:

- `PUBLISHED_ARTIFACT` → `bazel.generators.stub_alias_target` (new): a generic native `alias`
  forwarding to `EcosystemAdapter.external_labels(stub_unit)`'s real external label — the SAME
  machinery `EcosystemAdapter.dep_labels()` builds an ordinary package's `deps` from. No new
  per-ecosystem rendering logic.
- `EMPTY_FAILING` → the existing `stub_failing_target` (genrule).

Wired into `_run_build_wave` (computed once per wave, alongside the existing `hoist_watch`/
`stub_workspace_deps` shape) → `_build_payloads` (new `stub_package_files` parameter) → a new
`cli._union_support_files` merges it into every dispatch's `root_files`/`support_files`, the same
fleet-wide mechanism that already materializes `MODULE.bazel`/`//:requirements.lock` into every
worktree. Live and reachable on every `fleet build` today; still a no-op in production because
nothing writes an `ACTIVE` `stubs` row (ADR-0113 condition 2 — all three `--stub-blocked`
refusals still stand).

### I2 — one label convention, not two

`stub_failing_target` and the new `stub_alias_target` both take `name` as a caller-supplied
parameter now, rather than `stub_failing_target` inventing its own (previously hardcoded)
`"stub"`. `cli._stub_package_files` derives `name`/`package` from `_internal_label(stub_dest(
coord_key))` — the exact same function `_create_stub_records` uses for `stubs.bazel_label` and
`_unit_deps`'s redirect reads back — so there is exactly one label-construction convention across
all three call sites, never a second one recomputed.

### `stub_failing_target` wired for real, and the reason corrected

The original report justified leaving it unwired with "dependents of an `EMPTY_FAILING` provider
stay `BLOCKED`, so there's no live caller." The review traced this on the landed tree and found it
**false**: `orchestrator.reentry.stub_permits_removal` has no fidelity check at all (`REQUIRES_
HUMAN_INTERVENTION` at any phase + the `stub_blocked` flag is sufficient); `state.repository.
stub_degrade_transform` leaves an `EMPTY_FAILING`-only consumer at `SUCCEEDED`, not `BLOCKED`;
and `_eligible_build_units` would admit a TRANSFORM-`SUCCEEDED` repo into BUILD. So the moment
task-69 removes the three CLI refusals, an `EMPTY_FAILING` provider's dependent would reach BUILD
with no failing target wired, contradicting SPEC. Verified this correction against the actual
source (not inherited from the ruling) before accepting it. The failing target is now materialized
for real (item 1 above covers both fidelities uniformly); the true, narrower reason it was safe to
leave unwired until now is simply that all three refusals still stand today.

### New real-Bazel proof, and a genuine pre-existing defect it surfaced

`tests/test_bazel.py::test_real_bazel_resolves_a_redirected_consumer_through_a_published_artifact_stub_alias`
(new, real subprocess, mirrors the file's own direct-invocation pattern): a "consumer" `py_library`
depends on the EXACT label `_internal_label(stub_dest(coord_key))` produces; the stub package
(`stub_alias_target`'s real output) aliases to `@pypi//six`; a real `requirements.lock` resolves
`six==1.16.0` from PyPI for real; `bazel build` succeeds and `bazel query 'deps(<redirect label>)'`
confirms the alias resolves to `@pypi//six:six`.

**First attempt used Maven/`guava` and surfaced a genuine, previously-undetected, pre-existing
defect, unrelated to this leg**, disclosed here rather than fixed: `JvmAdapter.workspace_deps()`
emits `maven.install(name=…, version=…)` per artifact via `bazel/generators.py`'s generic
`_tag_attrs` fallback (that adapter deliberately sets no `attrs`), but real `rules_jvm_external`'s
`install` tag class has **no** per-artifact `name`/`version` attributes — only an `artifacts`
string-list (`"group:artifact:version"` tuples) or the separate `maven.artifact(...)` tag class.
Real Bazel: `Error: in 'install' tag, unknown attribute 'version' provided`. **No test anywhere in
this tree had ever run a real `bazel build`/`mod graph` against a generated `maven.install` tag
before this attempt** — every existing maven assertion (including
`test_module_bazel_carries_the_workspace_deps_the_adapters_declare`) is `FakeBazel`-seamed. Fixing
this correctly means changing how every maven-ecosystem repo's `MODULE.bazel` renders — well
beyond this leg's blast radius. Switched the real-Bazel proof to Python/PyPI instead (also
sidestepped a second, separate, disclosed pre-existing gap: `rules_java` has no
`build.ruleset_versions` pin, per `JvmAdapter.toolchain_requirements()`'s own docstring).

New `tests/test_build_e2e.py::test_active_stubs_package_files_are_materialized_into_every_dispatchs_worktree`
(FakeBazel, wiring proof): seeds one `PUBLISHED_ARTIFACT` and one `EMPTY_FAILING` `ACTIVE` stub row
in the same run, runs `fleet build`, and confirms `_stub_package_files`'s rendered `BUILD.bazel`
lands on disk at each stub's exact `stub_dest(coord_key)` path with an `alias(...)` / `genrule(...)`
respectively.

**Mutation proof (Rule 12):** commented out `root_files = _union_support_files(...)`, confirmed the
mutation genuinely changed the file (`git diff --numstat --no-index` against a backup: 2
insertions/1 deletion), ran the new wiring test → RED (`FileNotFoundError`, the package file
genuinely absent), reverted, reran → GREEN.

### M1/M2/M3 — all fixed

- M1: `_stub_workspace_deps` now filters on an explicit `fidelity != StubFidelity.PUBLISHED_
  ARTIFACT.value` check (via the shared `_active_stub_facts`), not merely `pinned_version is
  None`.
- M2: dropped the non-discriminating `assert not (package / "UNBUILDABLE").exists()` (Bazel never
  writes a declared `genrule` output into the source package regardless of outcome, so it could
  not have failed either way) rather than replace it with another check that turned out to be
  equally non-discriminating on inspection (a `bazel info bazel-bin` + existence check would be
  implied by the `returncode != 0` assertion already present).
- M3: `_insert_published_artifact_stub_row`'s `bazel_label` now computed via `cli._internal_label(
  stub_dest(coord_key))`, matching the one real convention instead of a third, hand-rolled
  spelling.

### I5 — `docs/SPEC.md` marker corrected

§3.5 item 1's "task-68's BUILD-phase render half does not exist yet" sentence is now false;
replaced in the same commit as the code with a dated update stating the render half's real, wired
status and the narrowed reason `--stub-blocked` still refuses everywhere (the TRANSFORM-phase
decision side has no production call site yet — task-69's job).

### I3 — investigated thoroughly; not hand-edited, and here is why

Ran `tests/test_integration_honesty_citations.py` whole file, no `-k`: **70/70 clean**, both
profiles (`_INTEGRATION_HONESTY` and — confirmed by reading the test module — `_CRITERIA_PLAN`;
`docs/CRITERIA_PLAN.md` IS covered by this instrument, not just `docs/INTEGRATION_HONESTY.md`).
That is the ruling's own explicit confirmation bar, and it is met.

`docs/SPEC.md` has **zero** `cli.py:N` citations with `N ≥ 8000` — the entire affected range —
confirmed by direct grep; nothing to fix there.

`docs/DECISIONS.md` has no automated citation check at all (it is not one of the two profiled
docs). I derived the EXACT line-shift mapping this leg's insertion produces by parsing the real
diff hunks against `cb77005` (not by re-deriving a fixed offset blindly): `+6` from line 64
onward (an import-block reflow), stepping up through several small internal wiring hunks to a
final `+301` from roughly line 9942 onward, with the single largest jump (`+285`) landing right
after the new §37 Leg 2 section itself. I then sampled every `cli.py:N` citation with `N ≥ 8000`
across `docs/DECISIONS.md` (the full affected range) and, for each, read the ACTUAL content at
its cited line in the **pre-task-68 baseline** (`cb77005`, not my branch) to check whether the
citation was even correct before my insertion. **Every single one I checked was already pointing
at unrelated content in `cb77005`** — e.g. `_RESET_RUNNING_TO_PENDING_SQL` is cited at
`cli.py:9857-9861`, which at `cb77005` is inside `_build_payloads`'s `return build` tail; the
constant's real definition is at line 13289, a drift of ~3400 lines, an order of magnitude beyond
anything this leg's ~300-line insertion could cause. I found no case in my sample of "correct at
`cb77005`, broken by my shift" — every drift I could verify was pre-existing and chronic,
consistent with this file's own repeated, dedicated citation-sweep history (the round-G "63
citations" resweep the citation test module's own docstring records).

**I did not hand-edit `docs/DECISIONS.md`.** A blind mechanical shift would not restore accuracy
for a citation that was already wrong — it would just point the SAME wrong content at a
different line number, and manufacturing a "correction" that looks authoritative while still
being wrong is exactly the failure CLAUDE.md's own guardrails warn against. Repointing these to
their ACTUAL current referents (as the ruling's literal instruction asks) would mean tracking
down, for dozens of citations, what each ORIGINALLY MEANT — a large, separate, chronic-drift-sweep
task in its own right, well beyond a single leg's fix round, and one this project's own history
shows gets dispatched as its own dedicated effort. Flagging this precisely for the controller's
own scoping decision rather than either ignoring the finding or performing a token, non-fixing
edit to appear compliant.

### Final verification (whole files, no `-k`)

- `tests/test_workers_build.py` — 91 passed
- `tests/test_build_e2e.py` — 76 passed (~12 min, real Bazel + real git; includes the new wiring
  test)
- `tests/test_bazel.py` — 69 passed (includes the new real-Bazel alias-resolution test)
- `tests/test_cli.py` — 194 passed
- `tests/test_integration_honesty_citations.py` — 70 passed
- `tests/test_lint_gate.py` — 7 passed, 1 pre-existing disclosed `UserWarning` (unrelated,
  documented in that file already)
- `python -m mypy` (no path args): `Success: no issues found in 129 source files`
- `ruff check .`: `All checks passed!`

### Concerns carried forward for the controller

1. **The maven/`rules_jvm_external` `MODULE.bazel` rendering defect is real and unfixed.** Every
   maven-ecosystem repo's generated `MODULE.bazel` in this tree would fail a real `bazel build`
   today (`maven.install(name=…, version=…)` is not a valid tag shape). This is unrelated to §37
   and was never caught because no test ran real Bazel against it before this round. Recommend a
   dedicated task/ADR: `rules_jvm_external`'s `install` tag wants ONE call with an `artifacts`
   string-list, or per-artifact `maven.artifact(...)` tags — a real per-ecosystem rendering
   decision, not a one-line fix.
2. **`docs/DECISIONS.md`'s `cli.py` citations in the 8000+ range are chronically drifted**,
   independent of this leg, confirmed by direct sampling against the pre-task-68 baseline. Not
   hand-edited (see I3 above) — flagged for the controller to scope as its own sweep if wanted.
3. Items 1–3 from the original landing's "Concerns" section above still apply where not
   superseded by this fix round (concern 1 — whether `stub_failing_target` needed live wiring —
   is now resolved: it is wired, for the corrected reason stated above).
