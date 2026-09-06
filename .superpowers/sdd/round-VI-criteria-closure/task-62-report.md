# Task 62 report — D120: close §12.6's two live confinement violations in `cli.py`

## Status: DONE

## Environment note (read this before trusting anything else in this report)

The worktree handed to me was checked out at `fa95469`, on a stray branch
`worktree-agent-a50308052b6db0928` with no `.superpowers/` directory at all and no round VI
history — a round-L state, not "`728cb1b` or later on `main`" as instructed. Confirmed
`refs/heads/main` locally resolves to `728cb1b`. `.superpowers/sdd/round-VI-criteria-closure/
task-62-brief.md` does not exist anywhere in local git history (`git log --all --oneline -- <path>`
returns nothing) — the same missing-brief gap task 59's report documented for its own brief.
Self-repaired per the dispatch's own instruction: `git checkout -b agent/roundvi-task62 main`
(`main` itself could not be reused as a worktree branch name — already checked out in the shared
checkout — so `-b` from it). The controller's dispatch message reconstructs the brief's content in
full (D120's exact two sites, ADR-0100's precedent, the reproduction command), so I proceeded on
that rather than reporting `NEEDS_CONTEXT`, matching task 59's precedent for this exact situation.

## What D120 was

`728cb1b`'s own commit message and `docs/INTEGRATION_HONESTY.md` D120 (OPEN at dispatch) name two
already-landed commits this session that each add a bare `Ecosystem`/`ContractKind` member branch
in `src/fleet/cli.py`, outside the two exempt adapter packages, violating §12.6(a)'s AST-walk
invariant with "no allowance whatsoever":

- `cli.py:8834` (pre-fix) — `if ecosystem is not Ecosystem.PYPI:` inside `_partition_test_srcs`
  (round VI task 53, D112's Python-only test-src scoping).
- `cli.py:9529` (pre-fix) — `cnode.kind is ContractKind.SHARED_LIB` inside `_build_impl`'s PASS 2b
  loop (round VI task 56, ADR-0119's `SHARED_LIB` skip).

Reproduced pre-fix exactly as D120 states:
`pytest tests/test_ecosystems.py -k "no_ecosystem_branch or no_ecosystem_member_other or
no_bare_compare"` → `3 failed, 87 deselected`.

## Precedent read and matched: ADR-0100 / `workers/contracts.py::KIND_MODIFIERS`

Read `docs/DECISIONS.md`'s ADR-0100 and `src/fleet/workers/contracts.py`'s `KIND_MODIFIERS`
table (`ContractKind.OPENAPI` → the one entry, guarded with `kind in KIND_MODIFIERS`, never
`kind is ContractKind.OPENAPI`) before writing anything. The reasoning I matched: a module-scope
table lookup is the compliant "table, not branch" shape §1/§12.6 mandate — a `Compare`/`Subscript`
is only forbidden when a bare `Ecosystem`/`ContractKind` member is a direct operand, and `x in
TABLE` names no such operand on either side (`TABLE` is a bare `Name`, not a member literal).

**One complication ADR-0100's own case did not have.** `test_ecosystems.py` also runs a *separate*,
purely textual regex (`test_no_ecosystem_member_other_than_the_unknown_sentinel_is_named_outside_
the_packages`, pattern `Ecosystem\.([A-Z_]+)`) over every line of every file outside the adapter
packages — including comments and docstrings, and independent of AST context. There is no
equivalent regex for `ContractKind`. This means:

- The `ContractKind.SHARED_LIB` site could use the exact same-file `KIND_MODIFIERS` shape (a
  module-scope table defined right in `cli.py`, next to the branch it replaces) — nothing bans a
  `ContractKind` member literal appearing anywhere in `cli.py`.
- The `Ecosystem.PYPI` site could **not** use a same-file table: any literal `Ecosystem.PYPI` text
  anywhere in `cli.py` (even inside a table definition, even inside a docstring) fails the regex
  test independent of AST shape. The table had to move into one of the two exempt adapter
  packages, with `cli.py` importing it by name only.

## The fix

**Site 1 (`Ecosystem.PYPI`, `_partition_test_srcs`).** Added
`TEST_SRC_PARTITIONED_ECOSYSTEMS: Final[frozenset[Ecosystem]] = frozenset({Ecosystem.PYPI})` to
`src/fleet/ecosystems/base.py` (an adapter package, fully exempt from both §12.6 tests), in its
existing "Registry-derived data, for callers that must stay ecosystem-free" section — the section
this exact class of problem already has an idiom for (`monorepo_dirs()`, `library_rules()`, etc.).
Exported via `__all__`. `cli.py`'s existing `from fleet.ecosystems.base import EcosystemAdapter,
path_segment` import gained the new name; `_partition_test_srcs`'s branch became `if ecosystem not
in TEST_SRC_PARTITIONED_ECOSYSTEMS:`. Also rewrote the function's docstring, which itself named
`` `Ecosystem.PYPI` `` in prose and was therefore a second offender under the textual regex — no
occurrence of `Ecosystem.<member other than UNKNOWN>` remains anywhere in `cli.py`.

**Site 2 (`ContractKind.SHARED_LIB`, `_build_impl` PASS 2b).** Added `_BUILD_PASS_2B_SKIPPED_KINDS:
Final[frozenset[ContractKind]] = frozenset({ContractKind.SHARED_LIB})` as a module-scope constant
in `cli.py` itself, directly above `_eligible_contract_units` (same file as the branch, matching
`KIND_MODIFIERS`'s own placement style). The branch became `if cnode.kind in
_BUILD_PASS_2B_SKIPPED_KINDS:`. The original inline comment (the full ADR-0119 rationale) was moved
verbatim into the constant's docstring and replaced at the call site with a one-line pointer, to
avoid two independent copies of the same rationale drifting apart (CLAUDE.md §7's documents-are-
inputs-to-future-edits rule).

Both tables are `frozenset`, not `Mapping`, because neither site needs a value — only membership —
and `_module_scope_table_names` (the test's `Subscript`-exemption helper) is irrelevant here since
`in` produces a `Compare` node, not a `Subscript`; nothing in the AST test's `Compare` branch
inspects what the table datatype is, only whether either operand is itself a bare member literal
or a tuple/list/set literal containing one (neither table's *usage* is; the table's own *definition*
is an `ast.Call`/`ast.Set`, which the `Compare`/`Subscript` walk never visits).

## Proof of behavior preservation (Rule 12 discipline)

No existing test imports `_partition_test_srcs` or `_BUILD_PASS_2B_SKIPPED_KINDS` directly by name,
and no test currently exercises PASS 2b's contract loop at all (grepped `tests/*.py` for
`_eligible_contract_units`/`contract_units`/`HOISTED`/`MIGRATED` in `test_build_e2e.py` and
`test_cli.py` — zero hits; §12.31/D111's promotion legs that would populate a HOISTED/MIGRATED
wave-member contract are still open per `728cb1b`'s own commit message). So beyond the tests below,
correctness for both sites rests on an exhaustive equivalence check run in the actual worktree
interpreter (pinned per CLAUDE.md Rule 12: `fleet.__file__` asserted to start with the worktree
path before reading any result):

```
for eco in Ecosystem:      # all 7 members
    assert (eco is not Ecosystem.PYPI) == (eco not in TEST_SRC_PARTITIONED_ECOSYSTEMS)
for kind in ContractKind:  # all 5 members
    assert (kind is ContractKind.SHARED_LIB) == (kind in cli._BUILD_PASS_2B_SKIPPED_KINDS)
```
Both held for every member — the new table-membership test is provably identical, member-for-
member, to the `Compare` it replaces. This is stronger than a handful of mutation samples for a
5–7-element closed enum: it is the complete truth table.

## Tests run

- `pytest tests/test_ecosystems.py -k "no_ecosystem_branch or no_ecosystem_member_other or
  no_bare_compare"` — pre-fix `3 failed, 87 deselected`; post-fix **`3 passed, 87 deselected`**.
- `pytest tests/test_ecosystems.py` (whole file, no `-k`) — **90 passed** (was 87 pass / 3 fail).
- `pytest tests/test_ecosystems_contracts_shared_lib.py
  tests/test_new_language_touchpoints_e2e.py` — **98 passed** combined with the file above (no
  regression in the ContractKind/SHARED_LIB-adjacent suites this touches).
- `pytest tests/test_build_e2e.py::test_a_python_repo_with_a_real_test_file_gets_a_real_py_test_
  target` — **1 passed**. This is D112's own discriminator test (its docstring documents the exact
  revert-and-rerun that proves it distinguishes the fixed/broken behavior of
  `_partition_test_srcs`), run over `FakeBazel` (no real Bazel invocation) — the single existing
  test that actually exercises the site-1 branch's real effect end to end.
- `pytest tests/test_cli.py` (whole file, no `-k`) — **189 passed**.
- `pytest tests/test_build_e2e.py` (whole file, 73 tests, real-Bazel-backed) — launched in the
  background; result pending at time of writing this section, appended below once it lands.
- `python -m mypy` (no path args, full manifest scope per CLAUDE.md §6) — **Success: no issues
  found in 129 source files**.
- `ruff check src/fleet/cli.py src/fleet/ecosystems/base.py` — one `RUF022` (`__all__` not sorted)
  from my own edit, fixed; **all checks passed** on re-run.
- `ruff format --check` on both files — flagged 3 pre-existing formatting diffs elsewhere in
  `cli.py` (lines ~14986/15130/15362/15630, far from either edited region) that predate this task
  and are out of scope (Rule 3: touch only what you must); zero diffs inside either region I
  touched.

All commands run with the interpreter pinned to this worktree
(`PYTHONPATH=<worktree>/src <shared .venv>/bin/python3`, `sys.executable`/`fleet.__file__` asserted
before trusting any result, per CLAUDE.md Rule 12's worktree-import-isolation discipline — this
worktree carries no `.venv` of its own).

## Files changed

- `src/fleet/cli.py` — import line extended; `_partition_test_srcs` branch + docstring; new
  `_BUILD_PASS_2B_SKIPPED_KINDS` constant; PASS 2b branch.
- `src/fleet/ecosystems/base.py` — `Final` import added; new `TEST_SRC_PARTITIONED_ECOSYSTEMS`
  constant + `__all__` entry.

## Concerns / follow-ups for the controller

- Site 2's branch (`_BUILD_PASS_2B_SKIPPED_KINDS`) is presently untested by any suite in the repo
  beyond the exhaustive equivalence check above — not a regression I introduced (it was equally
  untested before, as a bare `Compare`), but worth flagging since D111/§12.31's promotion legs
  (Leg C1 etc., still open per `728cb1b`) are what will eventually make PASS 2b's contract loop
  reachable by an integration test.
- D120 should be marked fixed in `docs/INTEGRATION_HONESTY.md` with this commit's SHA — left to the
  controller per this project's number/ledger-allocation discipline (CLAUDE.md §3).

## Verification round (dispatched separately, post-report — closes the pending `test_build_e2e.py` item)

The full-file real-Bazel run this report flagged as pending was completed and investigated before
committing.

**First attempt (narrow env) misdiagnosed as a regression, then disproved.** Pinning the
interpreter per CLAUDE.md Rule 12 with `env -i PATH=/usr/bin:/bin PYTHONPATH=$WT/src` (the literal
recipe quoted in CLAUDE.md's Guardrail 6) gave `11 failed, 51-54 passed` depending on how much of
`.venv/bin`/the login `PATH` was restored. Every one of the 11 failures traced to a **missing
external tool**, not to this task's diff — three separate causes, all pre-existing:

1. `tools/bin/{gazelle,go,cargo,rustc}` are wrapper *scripts*, tracked in git, but the real
   binaries/SDKs they `cd` into (`tools/go/sdk`, `tools/rust/cargo`, etc.) are git-ignored and are
   normally provisioned into a fresh worktree by `tools/worktree/new-worktree.sh` as symlinks to
   the primary checkout's copies. This worktree was never created by that script (per this
   report's own "Environment note" above — it arrived as a stray `fa95469` checkout with no
   `.superpowers/` and no round VI history), so `tools/go`, `tools/rust`, and
   `tools/bin/{bazel,ast-grep,gh}` were simply absent — 8 of 11 failures (`gazelle`/`go mod
   download` "can't cd to .../tools/bin/../go").
2. `uv` and `pnpm` are real binaries this host does not put on a bare `PATH=/usr/bin:/bin` (`uv`
   lives only in `.venv/bin`; `pnpm` at `~/.local/share/pnpm/pnpm`) — 3 of 11 failures
   (`test_two_rust_repos_in_one_wave_both_build`,
   `test_two_python_repos_with_different_pypi_dependencies_both_build`,
   `test_re_resolving_the_same_specs_produces_a_byte_identical_lock`).

**Disproved as a regression, not just asserted.** For one failure of each cause (`gazelle`'s
"can't cd", and the two `real_build`-produces-no-output failures), `git stash` (reverting both
`cli.py`/`base.py` edits back to the bare `Compare`/`is` forms) and re-running under the *same*
narrow env reproduced the **identical** failure — proving the diff is not the cause.

**Then provisioned the worktree correctly, per its own tooling's documented contract, and got a
real green.** `tools/worktree/new-worktree.sh` symlinks `tools/bin/{bazel,ast-grep,gh}` and the
children of `tools/go`/`tools/rust`/`tools/bazelisk` from the primary checkout into a fresh
worktree (all git-ignored paths — `git status` confirmed clean before and after). Applying that
same provisioning here (plus `.venv/bin` and `~/.local/share/pnpm` on `PATH` for `uv`/`pnpm`,
which the script's venv-clone step and the login shell's `PATH` respectively would have supplied
had this worktree been created normally) and re-running the full file with the interpreter still
pinned to this worktree (`fleet.__file__` asserted before trusting the result) gave:

```
73 passed in 1100.56s (0:18:20)
0 tests skipped this session — full collected coverage ran
```

Zero skips (every external tool now resolves — bazel, git-filter-repo, gazelle, uv, pnpm) and zero
failures. This is a materially stronger result than the report's own pending item asked for: not
just "the 72 other tests didn't regress" but a complete, nothing-skipped pass of the whole file.

**No source file was touched by this provisioning** — only symlinks under already-git-ignored
`tools/` paths, matching exactly what the project's own `new-worktree.sh` does for every other
worktree.
