# Task 59 report — D111 Leg B: the `git revert -m 1` + `Fleet-*`-trailer primitive (standalone)

## Status: DONE

Leg B of §12.31/D111's 5-leg closure plan (A–E). Builds and proves, standalone, the git primitive
SPEC §3.1's hoist-rollback procedure names but that has no precedent anywhere in `src/fleet/vcs/`:
"revert its merge commit on the integration branch with `git revert -m 1`, carrying the standard
`Fleet-*` commit trailers like any other mutation" (`docs/SPEC.md:606-610`). Per the dispatch, this
task does **not** wire the primitive into any caller — that is future Legs C2/D, not yet
dispatched.

## Environment note (read this before trusting anything else in this report)

The worktree handed to me (`.claude/worktrees/agent-ac5035ac0270f02da`) was checked out at
`fa95469` on a stray branch `worktree-agent-ac5035ac0270f02da` — a round-L state with no
`.superpowers/` directory at all, not "`6844a04` or later on `main`" as instructed. I confirmed
`main` locally resolves to `6844a04` exactly and self-repaired: `git checkout -b
agent/roundvi-task59 6844a04`.

**`task-59-brief.md` and `research-32-report.md` do not exist in any git history on any local
ref** (`git log --all --oneline -- <path>` returns nothing for either). Sibling task 60 hit and
documented the identical gap for its own brief (`git log --all --oneline --diff-filter=A -- '...
task-60...'` → `1178b2a`) — both files apparently lived only as uncommitted scratch in a research
agent's own workspace and were never committed. Rather than report `NEEDS_CONTEXT` as task 60 did,
I judged this leg differently: the dispatch message itself (not the missing brief) already stated
the scope precisely ("a new git primitive (`git revert -m 1` plus `Fleet-*` trailer stamping) …
builds and proves the primitive standalone; it does not wire it into anything yet"), and I was
able to fully reconstruct the design constraints from primary sources already committed at
`6844a04`:
- `docs/SPEC.md:606-610` — the exact rollback procedure text and its `git revert -m 1` +
  `Fleet-*`-trailer requirement.
- `docs/CRITERIA_PLAN.md:1394-1396` and `docs/INTEGRATION_HONESTY.md` D111 — Leg B's definition
  ("no precedent anywhere in `src/fleet/vcs/`") and its place in the 5-leg breakdown.
- `src/fleet/vcs/commits.py`/`git.py` — the existing `apply_and_commit`/`guard`/`rebase`/
  `abort_rebase` patterns this primitive had to match stylistically (stage/commit split,
  boolean-conflict-vs-exception discrimination via a sentinel ref).

This is a scope where "build the mechanical primitive per an already-precise instruction, matching
this codebase's own established patterns" did not require the missing files to execute correctly,
unlike task 60's Leg C1 (a genuinely undecided design choice between two designs). Flagging this
judgment call explicitly per this project's directive-authority guardrail, since it departs from
the sibling's NEEDS_CONTEXT precedent.

## What was built

**`src/fleet/vcs/git.py`** — two new `Git` methods, placed beside `rebase`/`abort_rebase` (same
"destructive, and deliberately explicit" section) because they share the identical conflict
contract:

- `revert(sha, *, mainline=1) -> bool`: `git revert --no-commit -m <mainline> <sha>`. `True` on a
  clean stage (index holds the reverted tree, nothing committed). `False` on a settled conflict
  (`REVERT_HEAD` resolves) — caller must `abort_revert()`, mirroring `rebase()`'s own contract
  exactly. `GitCommandError` for anything else (unknown `sha`, a `mainline` the commit does not
  have, or an unsettled D42 probe).
- `abort_revert() -> None`: `git revert --abort`, mirrors `abort_rebase()`.

Two empirically-verified git behaviors this design leans on (git 2.43.0, this host):
1. `--no-commit` leaves `REVERT_HEAD` resolvable even on a **clean** stage — it is not, by itself,
   a conflict signal. `revert()` only reads it after git's own exit code says the revert did not
   settle cleanly, exactly the way `rebase()` reads `REBASE_HEAD` only after a non-zero exit.
2. `-m 1` on an ordinary **single-parent** commit succeeds (parent 1 is simply the only parent);
   git only refuses a mainline number the commit actually lacks ("does not have parent N", exit
   128). `revert()` does not pre-validate merge-vs-non-merge itself — git already does, and the
   ordinary `GitCommandError` path covers it.

**`src/fleet/vcs/commits.py`** — `RevertOutcome` (`commit_sha: str | None`, `conflicted: bool`)
and `revert_and_commit(git, *, sha, subject, trailers: FleetTrailers, mainline=1, body=None)`,
which stages via `git.revert` then, on success, commits via the existing `git.commit(...,
trailers=trailers.as_mapping())` — the same trailer-stamping call `apply_and_commit` already uses,
so the six `Fleet-*` trailers land identically. On conflict it makes **no commit** and returns
`RevertOutcome(commit_sha=None, conflicted=True)`, leaving the repo exactly as `Git.revert` alone
would (mid-flight, ready for `abort_revert()`). Deliberately does **not** carry `apply_and_commit`'s
`already_applied` idempotency guard — a revert has no `Fleet-Patch-Id` of its own to check against;
that question (what trailer proves "this rollback already landed") belongs to whichever future leg
wires this up, per the SPEC text's "the revert *is* the record" framing.

Both new names are re-exported from `src/fleet/vcs/__init__.py` alongside the existing
`apply_and_commit`/`CommitOutcome`/etc., with a one-line addition to the package docstring's
`commits.py` bullet.

## What was NOT built (explicitly out of scope, confirmed unaffected)

- No caller anywhere invokes `revert_and_commit` or `Git.revert` — `grep -rn
  "revert_and_commit\|\.revert(" src/fleet/ --include=*.py | grep -v vcs/` returns nothing outside
  `vcs/git.py`/`vcs/commits.py` themselves and their tests.
- `ContractStatus.FAILED` is still never assigned anywhere in `src/fleet/`.
- `graph/cycles.py::_hoist_contracts` is untouched.
- `--forbid-hoist` (`cli.py:2875`) is still stubbed.
- No unhoist blast-set, `HoistRollbackDemotion`, phase-demotion, or downstream-merge-refusal logic
  exists (Leg D).
- No `FILE_PATH`-collision join for "broke owner" detection exists (Leg C).

All of the above are correctly the remaining legs' scope, not this one's.

## Verification

Ran with the interpreter pinned per CLAUDE.md's worktree-import discipline (`env -i PATH=...
PYTHONPATH="$WT/src" <primary-checkout's .venv python>`, `sys.executable`/`fleet.__file__`
asserted to resolve inside THIS worktree before trusting any result — no `.venv` exists inside
this worktree itself):

- `pytest tests/test_vcs.py` (whole file, no `-k`): **70 passed, 5 skipped** (the 5 skips are
  pre-existing environmental skips — `git-filter-repo`/`gh` not installed on this host — unrelated
  to this change). 7 of the 70 are new:
  `test_git_revert_stages_a_clean_revert_without_committing`,
  `test_git_revert_on_a_conflicting_target_leaves_it_mid_flight_and_abort_restores_the_tip`,
  `test_git_revert_accepts_mainline_1_on_an_ordinary_non_merge_commit`,
  `test_git_revert_on_an_unknown_mainline_raises_rather_than_reporting_a_conflict`,
  `test_abort_revert_with_nothing_in_progress_raises`,
  `test_revert_and_commit_stages_and_stamps_fleet_trailers_without_rewriting_history`,
  `test_revert_and_commit_on_a_conflict_makes_no_commit_and_leaves_it_for_the_caller`. All run
  against a real temp git repository (a genuine two-parent merge commit and a genuine content
  conflict on revert), matching this file's own stated policy that git-primitive properties are
  proven against real git, not mocks.
- **Mutation testing (Rule 12).** Two mutations to `Git.revert`, each verified to actually change
  the file via a backup-diff gate before trusting the test result:
  1. Hardcoding `mainline` to `"1"` regardless of the caller's argument →
     `test_git_revert_on_an_unknown_mainline_raises_rather_than_reporting_a_conflict` went RED
     ("DID NOT RAISE GitCommandError"); the other 6 stayed green.
  2. Dropping `--no-commit` → `test_git_revert_stages_a_clean_revert_without_committing` and
     `test_revert_and_commit_stages_and_stamps_fleet_trailers_without_rewriting_history` both went
     RED (a real commit landed where none should have; the second commit then failed outright
     because git refused a second revert of an already-applied change). Both mutations were
     reverted byte-for-byte afterward (`diff` against the pre-mutation backup confirmed identical).
- `ruff check` on the 4 touched files: clean.
- `mypy` (whole-package, no path args, cwd inside this worktree — the scoping CLAUDE.md's §6 note
  requires disclosing): `Success: no issues found in 129 source files`.
- `tests/test_lint_gate.py` (all 7): PASS, including
  `test_ruff_check_is_clean_across_the_whole_repository` and
  `test_mypy_strict_is_clean_over_src_fleet`.
- Sanity import: `import fleet.cli` plus `fleet.vcs.revert_and_commit`/`RevertOutcome` both resolve
  through the package's public surface.

Did not run the full ~15-minute suite: this change is purely additive (two new `Git` methods, one
new `commits.py` function/dataclass, all newly exported, zero call sites elsewhere), so the blast
radius outside `tests/test_vcs.py` is zero by construction — no existing function's behavior
changed. `tests/test_vcs.py`'s own "no shell anywhere in this package" source-text check
(`test_the_package_never_uses_a_shell`) also passed, confirming the new methods don't introduce a
shell.

## Docs updated in this change

- `docs/INTEGRATION_HONESTY.md` D111: heading stays `OPEN` (Leg B does not close case (ii)); a
  dated in-body marker records Leg B's landing, matching the existing convention Leg A used.
- `docs/CRITERIA_PLAN.md` §31: a dated update paragraph, same pattern.
- `src/fleet/vcs/__init__.py`: one-line docstring addition naming the new primitive and its
  D111/Leg-B provenance.
- `docs/SPEC.md` §12.31's own text was not touched — it already correctly describes the target
  mechanism this leg partially implements; no adjudication (Rule 14) was needed since no
  criterion's wording changed.

## Commits

On branch `agent/roundvi-task59`, based on `main@6844a04`. Not merged, not pushed, per dispatch.
