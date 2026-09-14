# Worker report: D139 fix — `ecosystems/js.py` missing ecosystem filter

## Status: FIXED, not merged (branch `agent/roundviii-d139-fix`, not pushed)

- Fix commit: `e22fe32` — `fix: D139 -- js.py has no ecosystem filter on external_coordinates`
- Doc commit: `7f7f881` — `docs: D139 heading OPEN -> FIXED, LANDED (e22fe32)`
- Branch: `agent/roundviii-d139-fix`, created from `main` at `7cd4dd1`
- Worktree: `/home/redmage/swe repo harness worktrees/wt-roundviii-d139-fix` (per
  `tools/worktree/new-worktree.sh roundviii-d139-fix main`)

## What was wrong

`docs/INTEGRATION_HONESTY.md` D139 named `ecosystems/js.py`'s `workspace_deps()`
(`js.py:573-593` at the time D139 was filed) as spreading `*unit.external_coordinates`
unfiltered into the `npm_translate_lock` coordinate list, unlike `go.py`/`py.py`/`jvm.py`/
`rust.py`, all of which filter foreign-ecosystem coordinates out first (`334edeb`).

Per the brief's instruction to sweep for other sites rather than assume `workspace_deps()` was
the only one, I grepped every use of `unit.external_coordinates` in `js.py` and checked every
`_needs_npm_hub`-gated call site. Three real gaps, not one:

1. **`workspace_deps()`** (confirmed site) — `coordinates = [*unit.external_coordinates, ...]`
   spread every coordinate on the unit into the `npm_translate_lock` `WorkspaceDep` list with no
   ecosystem check.
2. **`_unit_package_json()`** — its `dependencies` dict comprehension iterated
   `unit.external_coordinates` unconditionally, so a stray coordinate would render into the
   emitted `package.json` and, via `resolution()`, into the pnpm resolver's scratch-tree input
   (the same class `334edeb` closed in `py.py`'s `_requirements_text` input and
   `resolution()`'s coordinate list).
3. **`_needs_npm_hub()`** — the JS analog of `py.py`'s "contributors" gate that `334edeb` also
   fixed there. Before: `bool(unit.external_coordinates or _first_party(unit))`, truthy for ANY
   non-empty list regardless of ecosystem. This function gates whether a unit becomes a
   `pnpm-workspace.yaml` importer (`_importer_dests`, line ~211), whether `resolution()` writes a
   `package.json` for it into the scratch tree (line ~568), and whether `root_targets()` emits
   the `npm_link_all_packages` macro for it (line ~507). A unit carrying only a foreign
   coordinate (e.g. a `go.mod` sharing a JS unit's directory — `external_coordinates` is
   re-read from every manifest the repo ships, not filtered by the unit's own ecosystem, per
   `go.py`'s own documented comment on `_go_requires`) would be wrongly treated as an npm
   contributor: seeded as an importer with an empty manifest, and would trigger
   `npm_link_all_packages` at a directory `npm_translate_lock` never wired an `@npm` entry for.

I did **not** touch `go.py`'s own `workspace_deps()` (line ~220-240 there), which also spreads
`unit.external_coordinates` unfiltered — that is a pre-existing, separate gap in a different
file, out of D139's named scope (D139 is `js.py`-specific and `go.py`'s `_go_requires()` was
already confirmed filtered by `worker-mutation-batch19`). Flagging it here rather than silently
fixing it, since expanding scope beyond the brief without disclosure is exactly what Rule 7
warns against; a separate D-number would be the right vehicle if the controller wants it chased.

## The fix

All three sites now filter on `coordinate.ecosystem is Ecosystem.NPM`, matching `py.py`'s
literal-style filter (both `_unit_package_json` and `_needs_npm_hub` are module-level functions
in `js.py`, like `py.py`'s equivalents, not methods with `self.ecosystems` the way
`jvm.py`/`rust.py`'s are — `workspace_deps()` IS a method, but I kept the literal
`Ecosystem.NPM` there too rather than `self.ecosystems`, since `JsAdapter.ecosystems` is a
single-member frozenset `{Ecosystem.NPM}` and the file's own free-function siblings already use
the literal form; both spellings are behaviorally identical here).

Diff summary (`git diff --stat` of `e22fe32`):
```
src/fleet/ecosystems/js.py | 28 ++++++++++++++--
tests/test_ecosystems.py   | 84 ++++++++++++++++++++++++++++++++++++++++++++++
2 files changed, 110 insertions(+), 2 deletions(-)
```

## Tests added (`tests/test_ecosystems.py`)

Mirroring the existing `py.py` filter tests
(`test_py_workspace_deps_excludes_a_coordinate_from_another_ecosystem`,
`test_py_workspace_files_contributor_gate_ignores_a_unit_with_only_non_pypi_coordinates`,
`test_py_workspace_files_content_excludes_a_non_pypi_coordinate_from_a_real_contributor`):

- `test_js_workspace_deps_excludes_a_coordinate_from_another_ecosystem` — a JS unit with one real
  NPM coordinate and one stray `Ecosystem.GO` coordinate; asserts `workspace_deps()` names only
  the NPM one.
- `test_js_resolution_contributor_gate_ignores_a_unit_with_only_non_npm_coordinates` — a unit
  with only a `Ecosystem.GO` coordinate; asserts `resolution([stray])` is `None` and
  `root_targets(stray) == []`.
- `test_js_unit_package_json_excludes_a_non_npm_coordinate_from_a_real_contributor` — a real
  contributor (one NPM + one stray GO coordinate); asserts the rendered `package.json`
  `dependencies` in `resolution()`'s scratch-tree input contains only the NPM entry.

## Mutation-proof evidence (Rule 12 discipline)

1. **Backup** taken before editing: `js.py.bak` (pre-fix), later `js.py.fixed` (post-fix), both
   under the scratchpad.
2. **Edit** applied to the worktree copy.
3. **Diff-verify-nonempty**: `git diff --numstat --no-index js.py.bak js.py` (worktree) →
   `26 insertions / 2 deletions` before the test additions; final `git diff --stat` on the
   committed fix commit shows `28 insertions(+), 2 deletions(-)` in `js.py` — genuinely changed,
   not a no-op.
4. **Test (green, fixed code)**: `pytest tests/test_ecosystems.py -q` → `114 passed in 1.59s`
   (111 pre-existing + 3 new).
5. **Restore (revert to backup)**: copied `js.py.bak` back over the worktree file; re-ran the
   three new tests with `-k`:
   ```
   FAILED test_js_workspace_deps_excludes_a_coordinate_from_another_ecosystem
   FAILED test_js_resolution_contributor_gate_ignores_a_unit_with_only_non_npm_coordinates
   FAILED test_js_unit_package_json_excludes_a_non_npm_coordinate_from_a_real_contributor
   3 failed, 3 passed, 108 deselected
   ```
   All three fail against the pre-fix code (genuine RED — a stray Go coordinate was found
   reaching `workspace_deps()`, `resolution()` produced a `Resolution` for a foreign-only unit,
   and `github.com/acme/widget` was found in the rendered `dependencies` dict), proving the tests
   actually discriminate the defect rather than passing vacuously.
6. **Re-verify-identical**: copied `js.py.fixed` back over the worktree file; `diff` against the
   saved `.fixed` copy reported no differences (byte-identical restore); re-ran the full suite:
   `114 passed in 1.51s`, confirming the restore did not silently alter anything.

## mypy / ruff

- `ruff check src/fleet/ecosystems/js.py tests/test_ecosystems.py` → `All checks passed!`
- `ruff format --check` on both files reports pre-existing formatting drift (quote style in
  `_DEFAULT_TSCONFIG`, several long lines/parenthesizations across the test file) — verified by
  stashing my changes and re-running the same command against unmodified `main` (`7cd4dd1`):
  **identical** set of reported line ranges, none overlapping the lines I added or changed. This
  is pre-existing repo drift, not something introduced here.
- `mypy` (whole package, no path arguments — per this project's mypy-scoping rule): `Success: no
  issues found in 132 source files`.

## Full `tests/test_ecosystems.py` run (final, fixed state)

```
........................................................................ [ 63%]
..........................................                               [100%]
114 passed in 1.59s
```

## Scope note

Per the brief, I swept `js.py` for every site iterating/checking `unit.external_coordinates`
rather than assuming `workspace_deps()` was the only one, and found two more real gaps
(`_unit_package_json()`, `_needs_npm_hub()`) — all three are fixed and each has its own
discriminating test. `go.py`'s own unfiltered `workspace_deps()` (a separate, pre-existing gap
in a different adapter, outside D139's named scope) was left untouched and is disclosed above
for the controller's awareness rather than fixed silently.
