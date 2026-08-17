# Task 2 (Worker B) report — D26, D27, and I5

Base commit: `68a41ff`. Scope owned: `src/fleet/cli.py` (the `_publish` / `_publish_module_lock` /
`_captured_module_lock` path), `src/fleet/vcs/git.py`, `tests/test_cli.py`. One out-of-lane,
orchestrator-authorized addition: `src/fleet/cli.py:3634` (`_git_output`), per the I5 addendum
message.

No `pytest` was run at any point, per the hard constraint. Every claim below was verified either
by static tooling (`ruff`, `mypy --strict`) or by a standalone `asyncio.run()` script executed
directly with the project's `.venv` python, entirely outside `tests/` and never importing
`pytest` or touching `tests/conftest.py` — so it cannot trip `pytest_sessionfinish`'s reaper. That
script (kept in my scratchpad, not part of the deliverable) exercises the exact same git
sequences and calls the exact same production methods (`BuildPipelineWorker._publish`,
`._publish_module_lock`) that the new `tests/test_cli.py` tests call, and reported:

```
D26: PASS (result is None, already_published=True)
D27: PASS (lock reached the branch, content matches)
```

I also hand-verified, with raw `git` commands (not pytest, not the harness), that the **old**
code's mechanics fail exactly as D26/D27 describe: `git commit` on an index a pathspec-scoped
`git add` staged nothing into exits 1 ("nothing added to commit but untracked files present"),
and a freshly-`materialize`d, uncommitted file compares byte-equal to itself and would have
returned before ever calling `git add`/`git commit`.

## D26 — fixed, first (as required)

**Defect.** `_publish`'s idempotence guard asked `git.is_dirty()` — `git status --porcelain` over
the **whole worktree** — instead of the pathspec it had just staged. Any worktree dropping outside
`paths` (a real Bazel's own `--build_event_json_file=bazel-<unit>-events.json`, a convenience
symlink, `MODULE.bazel.lock`) made a re-entry that staged nothing read as dirty, sent
`git commit` at an empty index, and failed the whole dispatch — non-idempotently, on every
subsequent re-entry, since PUBLISH is the sole recovery route (`fleet resume` does not exist).

**Fix.** `git.diff_stat(staged=True, paths=paths)` replaces `git.is_dirty()` at `cli.py:5182`
(now the pathspec-scoped `git diff --cached -- paths`, already implemented in `git.py` and unused
in production until now). `published = bool(staged.files)`. No new method needed in `git.py` for
this half. Updated the two large inline comment blocks in `_publish` that described `is_dirty()`'s
semantics, since they would otherwise have gone stale and misleading.

**Why D26 has to land first.** With D26 broken, the scenario D27 needs (re-entry after a crash
between `materialize` and `commit`) never reaches `_publish_module_lock` at all: `is_dirty()` sees
the crash's own droppings, forces `published=True`, and `git.commit()` on an empty index raises
before the mutex/integration section is ever entered. Confirmed directly: my D26 verification
script's *pre-fix* trace (reasoned from the raw-git reproduction, not executed against reverted
code — see Verification note below) never reaches `_publish_module_lock`.

## D27 — fixed, second

**Defect.** `_publish_module_lock` compared the lock's content against the **file on disk** in the
integration worktree (`_read_text_or_none(root / MODULE_LOCK_PATH) == lock.content`), not against
what `integration_branch` actually carries. A dispatch dying between `materialize` (writes the
bytes) and `commit` (lands them) left correct bytes sitting on disk, uncommitted; every later
re-entry read that same file, found it self-identical, and returned — `module_lock_published =
True` on the output while the branch shipped with no lock at all. A `--network=none` container
build of that tree exits 32 at `Error computing the main repository mapping`.

**Fix.** Two new `Git` methods (`src/fleet/vcs/git.py`):

- `Git.hash_object(path) -> str` — `git hash-object -- <path>`: the blob SHA a file's bytes
  would have if committed as-is, regardless of tracked/staged/untracked state.
- `Git.blob_at(rev, path) -> str | None` — `git rev-parse --verify -q <rev>:<path>`: the blob SHA
  actually committed at that path on that ref, or `None` if it isn't there.

`_publish_module_lock` now: materializes unconditionally (idempotent, same principle already used
above it in `_publish`), computes `local_sha = hash_object(path)` and
`branch_sha = blob_at(payload.integration_branch, path)`, and compares the two SHAs instead of
content. `payload.integration_branch` was already plumbed through `BuildInput` (default
`"integration"`) but had zero readers before this fix — it is the field that names the branch, so
using it is not new plumbing, just a first consumer.

**Why not read the branch's content directly?** `util.proc.run` tail-truncates every capture to
32 KiB (SPEC §11.3, confirmed by reading `src/fleet/util/proc.py`). A `git show <rev>:<path>`
comparison would silently compare a truncated tail for any lock file past that size — plausible
for a fleet with enough external dependencies. Comparing two small, fixed-size SHAs sidesteps that
ceiling entirely, so the fix is correct at any lock size, not just the ones tested.

**Why not `git diff --quiet <rev> -- <path>`?** Tried first, empirically wrong: `git diff <rev> --
<path>` is blind to untracked files — it never shows a brand-new untracked path as an addition, so
it would have reported "no diff" for the exact crash state D27 describes (materialized, never
staged). Verified this directly with raw `git` before writing the fix; that trace is why the final
design uses `hash-object`/`rev-parse` instead.

Updated the docstring's stale idempotence paragraph and added a code comment; both now describe
comparing against the branch rather than the file, and name D27.

## I5 (orchestrator addendum, mid-task) — `GitCommandError.started` forwarding

`docs/superpowers/plans/review-36.md:282` found one of three `GitCommandError` construction sites
(`src/fleet/cli.py:3634`, inside `_git_output`) omitting `started=`, silently defaulting to `True`
and rendering a never-spawned `git` process as `"timed out"`. The other two sites
(`vcs/git.py:252`, `workers/clone.py:241`) already forwarded it.

**Fix, in the two places authorized:**
- `src/fleet/cli.py:3634` — added `started=result.started`.
- `src/fleet/vcs/git.py:105-111` — rewrote `GitCommandError`'s docstring, which previously
  asserted "every other raise site here is a git process that demonstrably ran and exited" (true
  only of `git.py` itself, but written where a reader would generalize it). It now states plainly
  that the default is for hand-constructed raises only, that every `ProcResult`-derived
  construction site must forward the flag explicitly, and names both sites that do it correctly.

**Signature left untouched, per instruction.** I did not make `started` a required keyword.
Judgment call, stated rather than acted on: the review's own evidence (2 of 3 sites already
correct, one silent regression) argues *for* a required keyword being the more honest design — a
default that is "safe" at exactly 2 of 3 call sites is a trap for the next one — but that touches
construction sites in `workers/clone.py`, outside my lane, so I left it as the orchestrator
directed and flagged it here instead of acting unilaterally.

**Test added, `tests/test_cli.py::test_every_gitcommanderror_construction_forwards_started`:** an
AST scan (matching the repo's own precedent in `test_llm_client.py`/`test_ecosystems.py`) over
every `.py` file under `src/fleet/`, asserting every `ast.Call` to a bare-name `GitCommandError`
carries a `started=` keyword. Verified standalone (not via pytest): empty offenders list against
the fixed tree, and a synthetic snippet missing `started=` is correctly flagged — so the detector
has both a true-negative and a true-positive check behind it, not just a clean run.

## Files changed

- `src/fleet/cli.py` — D26 (`_publish`, line ~5182 and two comment blocks), D27
  (`_publish_module_lock`, its docstring and its comparison block), I5 (`_git_output`, line 3634).
- `src/fleet/vcs/git.py` — new `Git.hash_object`, new `Git.blob_at`; `GitCommandError`'s docstring
  corrected for I5. `is_dirty()` itself is untouched — it is still correct for its other call site
  (`cli.py:3755`, Phase 2 worktree recovery), which legitimately needs the whole-worktree question
  D26 exists to stop asking in `_publish`.
- `tests/test_cli.py` — imports extended (`ast`, `dataclasses.replace`, `typing.Any`,
  `fleet.bazel.lockfile.MODULE_LOCK_PATH`, `fleet.cli.{BuildInput,BuildOutput,
  BuildPipelineWorker}`, `fleet.models.build.{BuildUnit,SupportFile}`, `fleet.models.enums.
  Ecosystem`, `fleet.vcs.git.Git`, `fleet.workers.base.WorkerContext`); one helper class
  (`_StubLog`); three new tests (below). Nothing removed or renamed.

## Tests added

- `test_publish_is_not_blocked_by_worktree_droppings_outside_the_pathspec` (D26) — real git
  worktree, generated file already committed, one untracked dropping outside `paths`; asserts
  `_publish` returns `None` (not a `WorkerError`) and `already_published is True`.
- `test_publish_module_lock_survives_a_crash_between_materialize_and_commit` (D27) — real git
  worktree, `MODULE.bazel.lock` written to disk but never committed (the crash state); asserts
  `_publish_module_lock` lands it on the branch (`Git.blob_at` resolves, `git show` content
  matches) rather than reading the pre-existing file as already published.
- `test_every_gitcommanderror_construction_forwards_started` (I5) — AST source-text invariant
  over `src/fleet/`.

All three use real `git` subprocesses via the production `Git` class (no injected `CommandRunner`)
because both defects are about *which git question* is asked, not about argv construction — a
fake runner would only replay whatever answer the test wrote into it, proving nothing about the
actual regression. `worker_ctx` (`tests/conftest.py`) supplies the inert `WorkerContext`
collaborators; `_StubLog` stands in for `log` only where a test's worktree legitimately carries no
lockfile (the `module_lock_absent` warning path).

## Static verification performed (no pytest)

- `ruff check src/fleet/vcs/git.py src/fleet/cli.py tests/test_cli.py` → clean.
- `mypy --strict src/fleet/vcs/git.py src/fleet/cli.py` → clean.
- `mypy tests/test_cli.py` (project config, `disallow_untyped_defs=false` for `tests.*`) → 14
  pre-existing errors, all on unmodified lines (`ExitCode` literal-overlap comparisons and one
  `type: ignore` syntax issue that predate this change) — zero new errors after fixing one I
  introduced (`_StubLog` needed an `Any`-typed local, matching `conftest.py`'s own
  `sentinel: Any = object()` idiom for the same field).
- Standalone `asyncio.run()` script (`.venv` python, no pytest, no `tests/` import) exercising the
  exact D26/D27 test bodies against the real fix: both pass. A second standalone script exercised
  the I5 AST scan logic directly: empty offenders against the fixed tree, one detected offender
  against a synthetic pre-fix snippet.

## Question to route to research

None. `payload.integration_branch` existed, was correctly threaded through `BuildInput` already,
and had zero readers before this fix — no new plumbing or design decision was needed for D27
beyond picking it as the comparison target, which its own field name and default (`"integration"`)
already state unambiguously.
