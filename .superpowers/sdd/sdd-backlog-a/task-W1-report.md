# Task W1 — D49 third-leg regression in `_TransformEvidence.record()`

## STATUS: DONE

## Verification performed (before touching code)

- Confirmed `9a7148c` did change `RewriteWorker._record` (`src/fleet/workers/rewrite.py:567-586`)
  to append every `patch.path` to `output.rewritten`, and that the *deterministic RULE_MISS /
  `find_task_commit` idempotency shortcut* (`rewrite.py:321-332`) is a separate call site that
  still appends the bare `unit` (no `FilePatch` to source a path from there).
- Confirmed the multi-file collateral-edit path is real: `RewriteWorker._as_patches`
  (`rewrite.py:556-565`) lifts `ProposedFileEdit.path` for every file the model proposes (schema
  cap 64, `llm/schemas.py:199`), and `_record` appends `patch.path` for every one of them
  (`rewrite.py:585-586`) — so one unit's repair can add an unrelated sibling's path to
  `rewritten`.
- Confirmed `completed_units` (the `WorkerResult.completed_units` field, `workers/base.py:390`)
  is populated on **every** path that legitimately resolves a unit in `RewriteWorker.run`: the
  deterministic land (`landed.append(unit)`, `rewrite.py:380`), the idempotent
  `find_task_commit` shortcut (`rewrite.py:328`), and the repair-rung land (`rewrite.py:452`) —
  all three append the loop's own `unit`, never a collateral path. `TransformPipelineWorker.run`
  then re-namespaces these as `f"rewrite:{unit}"` / `f"relocate:{unit}"` into its own
  `WorkerResult.completed_units` (`cli.py:3329`, `3349`). This confirms `completed_units` is the
  right identity key and is unaffected by the D49 change — it was never sourced from
  `output.rewritten`.
- Confirmed `TransformOutput.unresolved` is populated from exactly one source,
  `rewritten.unresolved` (`cli.py:3348`), i.e. always raw (un-namespaced) rewrite-target unit
  names — never relocate units, never namespaced strings.

## The fix

`src/fleet/cli.py` — `_TransformEvidence`:
- Added `_resolved: dict[str, set[str]]`, a per-repo accumulated set of rewrite-unit identities
  (i.e. `WorkerResult.completed_units` entries namespaced `rewrite:`, prefix stripped),
  accumulated across every `record()` call for that repo (not reset between attempts, since
  `_TransformEvidence` itself lives for one CLI invocation, `cli.py:4252`).
- `record()` now takes an additional `completed_units: Sequence[str] = ()` parameter and dedupes
  `output.unresolved` against `_resolved[repo_id]` instead of `set(prior.rewritten)`.
- `_TransformSink.__call__` now calls `self._evidence.record(output, result.completed_units)`
  (`cli.py:3583`) — `result` is the `WorkerResult[TransformOutput]` the sink already receives, so
  no new plumbing was needed to reach the identity data, only to thread it through.
- Did not revert `9a7148c`; `output.rewritten` still holds landed `FilePatch.path`s, which
  `_transform_criterion`'s parse probe (`cli.py:4187-4195`) still needs unchanged.
- `_TransformEvidence.record`'s other three call sites in `tests/test_cli.py` (off-limits, not
  touched) call it with a single positional `output` argument; the new parameter's default `()`
  keeps them behaving exactly as before (their fixtures pass `unresolved=[]`, so the dedup set
  is irrelevant either way).

## Tests (Rule 9) — `tests/test_transform_e2e.py`, new section 6

Two tests added, calling `_TransformEvidence.record()` directly (same pattern `test_cli.py`
already uses for `_transform_criterion`) rather than driving a full multi-attempt `fleet
transform` run — reproducing the exact deadline/repair timing through the real CLI would pin the
scenario far less precisely than constructing the two `TransformOutput`s the worked example
describes.

1. `test_a_units_own_failure_survives_a_siblings_collateral_rewrite` — the worked scenario
   verbatim: attempt 1 lands unit `B` whose repair collaterally rewrites sibling `dest/x.py`
   (`rewritten=["dest/b.py","dest/x.py"]`, `completed_units=["rewrite:dest/b.py"]`); attempt 2's
   unit `A`, whose own canonical name **is** `dest/x.py`, genuinely fails
   (`unresolved=["dest/x.py"]`). Asserts the final `unresolved` still contains `dest/x.py`.
2. `test_a_units_own_completion_still_clears_it_from_unresolved` — the case the fix must not
   break: unit `C` resolves under its own identity on attempt 1
   (`completed_units=["rewrite:dest/c.py"]`); attempt 2 (spuriously) re-lists `dest/c.py`
   alongside a genuinely-unresolved `dest/d.py`. Asserts the final `unresolved` is exactly
   `["dest/d.py"]` — `C`'s prior completion clears it, `D` is not swept away with it.

**Confirmed both directions, honestly:**
- Against pre-fix `HEAD` (`git stash` on `src/fleet/cli.py` only, tests left in place): both
  tests **fail** — with a `TypeError: record() got an unexpected keyword argument
  'completed_units'`, not an assertion mismatch. This is inherent to the fix, not a weakness of
  the test: no design that lets `record()` see unit *identity* (as opposed to unit *path*) can
  avoid changing what reaches `record()`, since that identity information never reached
  `_TransformEvidence` before this fix at all — its absence is the root cause. A signature-level
  `TypeError` is exactly the failure a revert of this fix would reintroduce.
- Against post-fix code: both tests **pass**, along with the pre-existing 12 tests in the file
  (`14 passed`).

Full `tests/test_transform_e2e.py` run (only this file, no other pytest session running
concurrently, no `FLEET_*` exported): `14 passed in 13.24s`.

`mypy --strict src/fleet`: `Success: no issues found in 107 source files` (baseline held).
`ruff check src/fleet/cli.py tests/test_transform_e2e.py`: `All checks passed!`.

## Commit

`ee5c22b` — `fix(D49): key _TransformEvidence's unresolved dedup on completed_units, not paths`
(touches only `src/fleet/cli.py` and `tests/test_transform_e2e.py`; nothing else was staged).

## Concerns

- **Did the regression reproduce as described?** Yes, mechanically confirmed via direct
  construction of the two `TransformOutput`s from the worked scenario against the pre-fix
  `record()` logic (traced by hand and by running the stashed pre-fix code): `prior.rewritten`
  after attempt 1 is `["dest/b.py", "dest/x.py"]`; attempt 2's `unresolved=["dest/x.py"]` filters
  against `set(prior.rewritten)`, which contains `"dest/x.py"` purely from `B`'s collateral edit,
  dropping `A`'s genuine failure. Reachability within a single run (no resumption) matches the
  brief: `_TransformEvidence` is created once per CLI invocation (`cli.py:4252`) and `record()` is
  called once per `PhaseRunner._drive` attempt into the same instance.
- **Is the §3.2 gate now sound?** For this specific defect, yes: the dedup now keys on unit
  identity (`completed_units`, populated on every legitimate resolution path, confirmed above),
  not on a coincidental filename match. I did not re-audit the other three clauses of
  `_transform_criterion` (empty diff, outside-subtree writes, parse probe) — those were out of
  scope for this task and I have no evidence of a defect in them from this investigation.
- **Scope check on `tests/test_cli.py`:** I did not conclude the regression test belongs there. It
  exercises `_TransformEvidence.record()` directly, which `test_transform_e2e.py` docstring
  already claims coverage of ("§3.2's success criterion is checked against git for every repo the
  run calls `SUCCEEDED`"), and the file already contains a "6." style section boundary consistent
  with the rest of the file's numbered sections. I did not touch `tests/test_cli.py`.
- I did not modify `src/fleet/vcs/git.py`, which appeared as locally modified during my session
  (another agent's concurrent edit) — confirmed via targeted `git stash push -- src/fleet/cli.py`
  and a scoped `git add` at commit time so only my lane's two files were staged.
