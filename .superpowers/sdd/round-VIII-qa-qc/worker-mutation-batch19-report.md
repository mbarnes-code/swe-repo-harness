# Worker report: §15.1 item 3, Wave 5 Batch 19 — mutation-proof ecosystems/{base,go,js,unknown}.py

## Status: DONE

Branch `agent/roundviii-mutation-batch19`, commit `b7339e4`, isolated worktree at
`/home/redmage/swe repo harness worktrees/wt-roundviii-mutation-batch19`. No merge, no push.

## What changed

`tests/test_ecosystems.py` only — 4 new tests, 0 production code changes. Confirmed by
`git diff --stat` (192 insertions, 1 deletion — the deletion is the one line moved during a
self-correction, see below) and, separately, a byte-for-byte identity check of
`src/fleet/ecosystems/{base,go,js,unknown}.py` against their pre-mutation-testing backups after
every mutation cycle (`diff` reported no differences each time).

**Self-correction during authoring:** my first append accidentally dropped the final line of the
pre-existing `test_the_registry_and_its_output_are_identical_across_processes` test
(`assert parsed["dirs"]["npm"] == "ts"`) because my read of the file's tail used a line count one
short of the true end. Caught immediately by running the full suite (a `NameError` inside my own
new test, from stray code left over in the wrong place) before it was ever committed; fixed by
restoring that line to the original test and re-verifying `git diff` was purely additive plus the
one import-line change.

## Symmetry check (per the brief's explicit instruction not to assume it)

**These four files do NOT share one mutation shape with each other, or with the
py.py/jvm.py/rust.py shape batch 3 (`agent/roundviii-mutation-batch3`) found.** A third, distinct
shape turned up:

| File | Shape |
|------|-------|
| `base.py` | Ecosystem-NEUTRAL shared infrastructure (no adapter identity, no `ecosystems` filter). Its own load-bearing untested branch is a **preference-order** bug in `select_entrypoint()`, used by all four target-emitting adapters (py/jvm/rust/js). |
| `go.py` | Already carries the `_go_requires()` ecosystem-exclusion filter that `334edeb` copied into py.py/jvm.py/rust.py ("matching the existing go.py fix" — confirmed by `git log -p` on `go.py`: only one occurrence of `coordinate.ecosystem is Ecosystem.GO` ever, unchanged since). Untested gap: no existing test ever mixes ecosystems inside one Go unit's `external_coordinates`. |
| `js.py` | **Has NO ecosystem filter anywhere** — a genuine, disclosed asymmetry (see Finding below), not a shape to test around. Its own load-bearing untested branch is instead `_needs_npm_hub()`'s OR-with-`_first_party` disjunct. |
| `unknown.py` | The simplest adapter; almost everything is a default `[]`. Its one non-trivial piece of logic is the three-way set-union/dedupe in `generate_targets()`, untested because no existing fixture ever overlapped `srcs`/`test_srcs`/`resources`. |

`base.py`'s own hint in the brief ("likely shared infrastructure — check what it defines before
assuming it needs its own adapter-style test") held: it needed a **direct function test**, not an
adapter round-trip test, and the function it needed to test (`select_entrypoint`) is a helper
several *other* adapters delegate to, not something `base.py`'s own (nonexistent) concrete adapter
uses.

## Finding: js.py lacks the ecosystem-exclusion filter its four siblings all have

`grep -n "self.ecosystems\|Ecosystem\.\|ecosystem is\|ecosystem ==\|ecosystem in" src/fleet/ecosystems/js.py`
returns only the class's own `ecosystems: ClassVar[...] = frozenset({Ecosystem.NPM})` declaration
— **zero** filtering of `unit.external_coordinates` by ecosystem anywhere in `workspace_deps()` or
`_unit_package_json()`. `cli._external_coordinates` (confirmed by reading it in
`src/fleet/cli.py:10059-10090`) dispatches per **manifest file**, not per unit ecosystem, so a
JS-primary repo that also ships e.g. a `go.mod` or `requirements.txt` would carry those foreign
coordinates in `unit.external_coordinates` exactly as `_go_requires`'s own docstring describes for
Go — this is the same class of defect `334edeb` fixed for py.py/jvm.py/rust.py, "matching the
existing go.py fix", but js.py was not in that commit's file list and has never received the fix.

**This is reported, not silently fixed and not silently tested around.** Writing a test that
asserts js.py *correctly* excludes a foreign coordinate would fail against current code (there is
no guard to pass); writing one that asserts the *current, unguarded* behavior is "correct" would
enshrine a real defect in a test. Per this batch's scope (test-writing only — the brief's four
steps are read/compare/write-tests/lint+commit, with no instruction to alter production code) and
CLAUDE.md Rule 3 (Surgical Changes), I did not add a filter to js.py. **Recommend a small
follow-up batch, analogous to `334edeb`, adding the same `coordinate.ecosystem is Ecosystem.NPM`
(or `in self.ecosystems`) guard to `workspace_deps()` and `_unit_package_json()`,** with a
mutation-proof test modeled on this batch's `test_go_root_module_excludes_a_coordinate_from_
another_ecosystem`.

## New tests (4)

1. **`test_select_entrypoint_prefers_the_declared_candidate_order_not_srcs_order`** (base.py) —
   direct test of `ecosystems.base.select_entrypoint`. Every existing `generate_targets` test
   supplies at most one entrypoint-shaped filename per unit, so "first candidate present" and
   "first srcs entry matching some candidate" have never been forced to disagree. Constructed
   `srcs=["src/Application.java","src/Main.java"]` against jvm.py's real preference order
   `("Main.java","Application.java","App.java")` — alphabetically `Application.java` sorts first,
   but the correct answer is `Main.java`.

2. **`test_go_root_module_excludes_a_coordinate_from_another_ecosystem`** (go.py) — a Go unit
   whose `external_coordinates` include one real Go coordinate and one **Go-module-SHAPED**
   stray NPM coordinate (`github.com/evil/pkg v1.0.0`). Deliberately shape-valid so the test
   isolates the ecosystem filter from `_require_line`'s separate shape-validation guard (already
   covered by the pre-existing `test_a_go_coordinate_that_cannot_be_a_require_line_fails_loudly`,
   whose own "polyglot" case happens to be shape-INVALID and so would still raise even with the
   ecosystem filter removed — confirmed during mutation testing, see below).

3. **`test_js_root_targets_and_generate_targets_wire_the_hub_for_a_sibling_only_unit`** (js.py) —
   a unit with `external=[]` and exactly one linkable first-party sibling. Every existing test
   that exercises `_first_party`'s branch of `_needs_npm_hub`'s OR also gives the unit an external
   dependency, so the two disjuncts have never been forced to disagree.

4. **`test_unknown_filegroup_srcs_dedupe_a_path_declared_as_both_a_source_and_a_resource`**
   (unknown.py) — a unit whose one file is listed in both `srcs` and `resources`. The existing
   test never overlaps `sources()`/`test_sources()`/`non_source_files()`, so the method's
   `sorted({*a,*b,*c})` set-union dedupe (vs. a list-concatenation that would duplicate the label)
   has never been exercised.

## Mutation results (backup / edit / diff-verify-nonempty / test / restore / re-verify-identical)

Each site was mutated in isolation, the FULL `tests/test_ecosystems.py` suite (115 tests) was run
each time to confirm no cross-contamination, then the file was restored and `diff` against the
pre-mutation-testing backup confirmed byte-identical.

| File | Mutation | Result |
|---|---|---|
| `base.py::select_entrypoint` | Iterate `sorted(srcs)` and return the first entry whose basename is in a `set(candidates)`, dropping preference order | 1 failed (the new test) / 114 passed |
| `go.py::_go_requires` | `if coordinate.ecosystem is Ecosystem.GO and coordinate.name` → `if coordinate.name` | 2 failed (the new test, AND the pre-existing `test_a_go_coordinate_that_cannot_be_a_require_line_fails_loudly` — expected: that test's own "polyglot" npm coordinate is shape-invalid so it still raises under this mutation, independently confirming the new test adds real, non-redundant coverage for the shape-VALID case) / 113 passed |
| `js.py::_needs_npm_hub` | `bool(unit.external_coordinates or _first_party(unit))` → `bool(unit.external_coordinates)` | 1 failed (the new test, a `ValueError: not enough values to unpack` on `root_targets`) / 114 passed |
| `unknown.py::generate_targets` | `sorted({*a,*b,*c})` → `sorted([*a,*b,*c])` | 1 failed (the new test, `['deploy.sh','deploy.sh'] != ['deploy.sh']`) / 114 passed |

Every mutation's diff against its pre-mutation-testing backup was confirmed non-empty (`diff`
exit code 1) before running tests, and every restore was confirmed byte-identical (`diff` exit
code 0, no output) before moving to the next file.

## Verification after restoring all four files

- `pytest tests/test_ecosystems.py -q` → **115 passed** (111 pre-existing + 4 new).
- `mypy tests/test_ecosystems.py` → 6 pre-existing errors (identical set, same messages, verified
  by running mypy against `git show HEAD:tests/test_ecosystems.py` through the identical
  invocation), 0 new.
- `ruff check tests/test_ecosystems.py` → all checks passed (one `E501` I introduced during
  docstring authoring was caught and fixed before commit).
- `ruff format --check tests/test_ecosystems.py` → same "1 file would be reformatted" complaint
  (one pre-existing drift site at `test_an_adapters_unrenderable_coordinate_error_...`, verified
  identical against the pre-edit file run through the same check) — pre-existing, not introduced
  here, left alone per Rule 3.
- `mypy`/`ruff check` on `src/fleet/ecosystems/{base,go,js,unknown}.py`: clean (`Success: no
  issues found in 4 source files`; `All checks passed!`) — unsurprising since `git diff` for these
  four files against `HEAD` is empty.
- `git status --short` shows only `tests/test_ecosystems.py` modified.

## Branch / commit

Branch: `agent/roundviii-mutation-batch19` (from `main`, HEAD `c53c6f7` at worktree creation).
Commit: `b7339e4`. Not merged, not pushed.
