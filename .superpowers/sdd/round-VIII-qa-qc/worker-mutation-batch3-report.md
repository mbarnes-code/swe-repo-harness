# Worker report: §15.1 item 3, Wave 1 Batch 3 — mutation-proof ecosystems/{py,jvm,rust}.py

## Status: DONE

Branch `agent/roundviii-mutation-batch3`, isolated worktree at
`/home/redmage/swe repo harness worktrees/wt-roundviii-mutation-batch3`. No merge, no push.

## What changed

`tests/test_ecosystems.py` only — 9 new tests, 0 production code changes. Confirmed by
`git diff --stat` (252 insertions, 1 file) and, separately, a byte-for-byte identity check of
`src/fleet/ecosystems/{py,jvm,rust}.py` against their pre-mutation-testing backups after every
mutation cycle (`diff` reported no differences each time — see mutation log below).

## Scoping the fix (`334edeb`)

`git show 334edeb -- src/fleet/ecosystems/{py,jvm,rust}.py` shows the shared bug class (a stray
coordinate whose `ecosystem` doesn't match the adapter's own could reach that adapter's Bazel
workspace deps / root files) applied with **different site counts per file** — confirmed by
reading each file in full before writing any test:

| File     | Fix sites | Guard shape                                    | Reachable via public API? |
|----------|-----------|-------------------------------------------------|----------------------------|
| jvm.py   | 1         | `if coordinate.ecosystem in self.ecosystems`     | yes |
| py.py    | 4         | `... is Ecosystem.PYPI` (hardcoded literal, not `self.ecosystems`) | yes (all 4) |
| rust.py  | 4         | `... in self.ecosystems` / `any(... in self.ecosystems ...)` | 3 of 4 — see below |

**Symmetry check (per the brief's explicit instruction not to assume it): the three files do NOT
share one mutation shape.** jvm.py has exactly one call site (`workspace_deps`); py.py and rust.py
each have four (`workspace_deps`, the `workspace_files` contributor gate, a second/third
per-content or per-unit filter, and — rust only — `package_files`). py.py's guard is written as a
direct `Ecosystem.PYPI` literal comparison (PyAdapter serves exactly one ecosystem) while
jvm.py/rust.py use `in self.ecosystems` (both serve ≥1 ecosystem via a frozenset, jvm two:
MAVEN+GRADLE). A single shared-shape mutation (e.g. one script grepping for `in self.ecosystems`
and reverting it) would silently miss all four py.py sites. **One test per file was not enough
either** — each file needed one test per site, not one test per file, because py.py's and rust.py's
four sites each guard a materially different failure mode (see mutation log).

**A fourth finding, specific to rust.py's site 3 (`_unit_workspace_files`):** this private helper
is only ever invoked by `workspace_files()` on units that already passed the outer contributors
gate (`any(c.ecosystem in self.ecosystems for c in unit.external_coordinates)`), which guarantees
any unit reaching it already carries ≥1 real Cargo coordinate (hence non-empty
`external_coordinates`). Mutating this site's guard back to bare `if not unit.external_coordinates`
therefore produces **zero observable difference in `workspace_files()`'s output** — it is
unreachable through that one caller. Per Rule 12 ("audit mutations for expressibility, not only
pass/fail"), I did not paper over this: `test_rust_unit_workspace_files_returns_no_lock_candidate_for_a_non_cargo_only_unit`
calls the method directly (`adapter._unit_workspace_files(stray)`, `# type: ignore[attr-defined]`
for mypy) rather than through `workspace_files()`, and the test's own docstring states why direct
invocation is necessary. The guard is still real defense-in-depth on the method's own contract
(nothing stops a future caller from invoking it on an unfiltered unit) so it was kept and tested,
not deleted.

## New tests (9)

- `test_jvm_workspace_deps_excludes_a_coordinate_from_another_ecosystem`
- `test_py_workspace_deps_excludes_a_coordinate_from_another_ecosystem`
- `test_py_workspace_files_contributor_gate_ignores_a_unit_with_only_non_pypi_coordinates`
- `test_py_workspace_files_content_excludes_a_non_pypi_coordinate_from_a_real_contributor`
- `test_py_resolution_excludes_a_non_pypi_coordinate_from_the_resolver_input`
- `test_rust_workspace_deps_excludes_a_coordinate_from_another_ecosystem`
- `test_rust_workspace_files_contributor_gate_ignores_a_unit_with_only_non_cargo_coordinates`
- `test_rust_unit_workspace_files_returns_no_lock_candidate_for_a_non_cargo_only_unit`
- `test_rust_package_files_returns_no_member_manifest_for_a_non_cargo_only_unit`

None of the pre-existing 102 tests in `tests/test_ecosystems.py` exercised a coordinate whose
`ecosystem` mismatches its unit's adapter, so all 9 sites were previously unguarded by any test
(confirmed by inspecting every existing `_unit(...)`/`_rust_unit(...)` fixture call in the file —
none mixes ecosystems in one unit's `external_coordinates`).

## Mutation results per file (backup / edit / diff-verify-nonempty / test / restore / re-verify-identical, per Rule 12)

Each site was mutated **in isolation** (one file, one site reverted to its exact pre-`334edeb`
text, the file's other fix sites left intact) and tested against **all of that file's new tests
together**, to confirm each mutation reddens only its own test and leaves the others green (no
cross-contamination, no coincidental pass).

### jvm.py — 1 site

| Site (method) | Mutant result | Original result |
|---|---|---|
| `workspace_deps` | FAIL — `{'left-pad','slf4j-api','guava'}` (npm leaked in) | PASS |

Diff before restore was non-empty (1 line removed); after restore, `diff` against the pre-mutation
backup was byte-identical, `git diff --stat` on the file was empty.

### py.py — 4 sites, each discriminated independently

| Site (method) | Test that reddens | Other 3 py tests |
|---|---|---|
| `workspace_deps` | FAIL — `{'left-pad','requests'}` leaked | PASS |
| `workspace_files` contributor gate | FAIL — a unit with only an npm coordinate produced a `SupportFile` instead of `[]` | PASS |
| `workspace_files` content filter | FAIL — rendered lock content contained `left-pad` | PASS |
| `resolution()` filter | FAIL — `resolution([stray_only])` returned a `Resolution` instead of `None` | PASS |

Each mutation cycle: backup → single-site revert → non-empty diff shown → the 4 py tests run
together (1 failed / 3 passed each time, and it was always the *matching* test) → restore →
byte-identical diff to backup → all 4 pass again.

### rust.py — 4 sites, 3 reachable via the public API, 1 reachable only by direct call

| Site (method) | Test that reddens | Other 3 rust tests |
|---|---|---|
| `workspace_deps` | FAIL — `{'left-pad','serde'}` leaked | PASS |
| `workspace_files` contributor gate | FAIL — a non-cargo-only unit produced a `Cargo.toml` naming it as a workspace member | PASS |
| `_unit_workspace_files` (direct call) | FAIL — direct call on a non-cargo-only unit produced a `Cargo.lock` carry candidate instead of `[]` | PASS |
| `package_files` | FAIL — a non-cargo-only unit got a member `Cargo.toml` | PASS |

Same backup/revert/diff/test/restore/re-verify cycle for all 4; final identity check confirmed
`src/fleet/ecosystems/{py,jvm,rust}.py` are all byte-identical to their pre-mutation-testing
backups, and `git status --short` shows only `tests/test_ecosystems.py` modified.

## Verification after restoring all three files

- `pytest tests/test_ecosystems.py -q` → **111 passed** (102 pre-existing + 9 new).
- `mypy tests/test_ecosystems.py` → 6 pre-existing errors (identical set, same messages, verified
  against `git show HEAD:tests/test_ecosystems.py` run through the same mypy invocation), 0 new.
  One new `# type: ignore[attr-defined]` needed for the direct `_unit_workspace_files` call
  (base-class-typed `adapter` doesn't statically know the `RustAdapter` subclass method); precedent
  for this pattern already exists in the file (`adapter.leaked = ...` at line 216).
- `ruff check tests/test_ecosystems.py` → all checks passed.
- `ruff format --check tests/test_ecosystems.py` → same "1 file would be reformatted" complaint as
  `git show HEAD:tests/test_ecosystems.py` run through the identical check (two pre-existing
  drift sites, both outside the range I touched) — pre-existing, not introduced here, left alone
  per Rule 3 (surgical changes).
- `mypy`/`ruff check`/`ruff format --check` on `src/fleet/ecosystems/{py,jvm,rust}.py`: clean
  except the same pre-existing `ruff format` drift in `py.py` (unrelated to this change, file is
  untouched — `git diff` against `HEAD` for these three files is empty).

## Branch / commit

Branch: `agent/roundviii-mutation-batch3` (from `main`, HEAD `7218d06` at worktree creation).
Committed the single test-file change; not merged, not pushed.
