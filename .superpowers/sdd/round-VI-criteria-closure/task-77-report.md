# Task 77 report — 3 parked minor findings from task-72's review

**Status: DONE**

Branch `agent/roundvi-task77`, worktree off `main` at `09f8a5a`.

## Minor 1 — `HoistRollbackFailed` payload now discloses the half-rolled-back state

`src/fleet/cli.py::_write_hoist_rollback_failed_finding` gained a required keyword-only
`unhoist: UnhoistOutcome | None` parameter (the settled outcome `_reconcile_hoist_rollbacks`'s own
loop already holds at its one call site) and two new payload keys:

- `unhoist_decision`: `unhoist.decision` (`"APPLIED"`/`"REFUSED"`) when known, `""` when
  `unhoist_contract` itself raised before returning (payload values are `str`-typed, so `None`
  isn't representable directly — documented in the docstring).
- `db_demoted_repo_ids`: comma-joined `unhoist.demoted_repo_ids`, `""` when `unhoist` is `None` or
  nothing was demoted.

An operator reading a bare `HoistRollbackFailed` row can now tell "DB demoted, git not yet
reverted" from "nothing happened for this contract" without cross-referencing sibling
`HoistRollbackDemotion` rows, exactly as the brief asked.

**Proof, not just construction:** strengthened the existing real end-to-end integration test
`tests/test_hoist_rollback_wiring.py::
test_a_hoist_rollback_that_cannot_find_its_anchor_fails_loud_for_one_contract_only` (the one test
in the suite that already exercises the real production shape — no `PullRequestDraft` carries
`contract_id`, so `execute_hoist_rollback` raises `RollbackAnchorError` after `unhoist_contract`
already committed). Added assertions: `failed_payload["unhoist_decision"] == "APPLIED"`, and
`failed_payload["db_demoted_repo_ids"]` equals the `repo_id` recorded by the sibling
`HoistRollbackDemotion` row for the same run (queried independently and cross-checked, not just
asserted against itself).

## Minor 2 — `HoistRollbackReconciliationEntry`'s fields now have a reader

Chose option (a) from the brief (small, natural addition, matching D44's own `_xxx_lines`
pattern used throughout `cli.py` for other run-level reports):

- `_build_impl` now captures `_reconcile_hoist_rollbacks`'s return value (previously discarded)
  into `hoist_reconciliations` and adds it to the returned dict as
  `"hoist_rollback_reconciliations": [asdict(entry) for entry in hoist_reconciliations]` —
  `asdict` recursively flattens the nested `UnhoistOutcome`/`HoistRollbackOutcome` dataclasses so
  the JSON output (`--json`) survives.
- `_build_lines` (the `fleet build` human-rendering function) reads it and, when non-empty,
  appends a line summarizing `N contract(s) reconciled, M without error[, K failed (see
  HoistRollbackFailed finding): <ids>]` — a count of failed vs. succeeded reconciliations, as the
  brief suggested.

**Proof:** the same strengthened integration test now captures the `Result` from `build()` and
asserts `payload(result)["hoist_rollback_reconciliations"]` contains exactly the one real
contract's entry, with `unhoist.decision == "APPLIED"`, `rollback is None`, and `error` set —
proven end to end through the real CLI JSON output, not merely constructed by hand. Also
cross-checked `reconciled["error"] == failed_payload["detail"]` (same exception's `str()`,
surfaced two different ways).

## Minor 3 — stale "not yet wired" claims in `docs/CRITERIA_PLAN.md` corrected by annotation

Per the project's annotate-never-rewrite convention, added a new dated paragraph
(`**Update, 2026-09-08 (round VI task 77)**`) immediately after the existing 2026-09-06
"round VI task 65" dated block (criterion 31, before the `## 32.` heading) — the existing text is
untouched. The new paragraph:

- Names the two stale claims verbatim (`cli.unhoist_contract` "zero call sites outside its own
  tests"; "the production wiring (task-67, not yet scoped)").
- States what actually landed since: task 71 (`293688e`) built the git-mechanics revert-series
  execution, task 72 (`a96e8d7`, with a fix round) built the production wiring itself.
- Explicitly does **not** claim criterion 31/case (ii) is now DONE: discloses the residual gap
  (confirmed still open by D122's own task-75 fix, `a9636b8`, in `docs/INTEGRATION_HONESTY.md` —
  "wiring one remains a future task's job") that `execute_hoist_rollback` still raises
  `RollbackAnchorError` on any real fleet today, caught per-contract and recorded as a
  `HoistRollbackFailed` finding rather than crashing the build.
- Cross-checked against `docs/SPEC.md`'s own §12.31/row-28 cell, which already carries an
  identical dated 2026-09-08 correction — consistent, not contradicting.
- Did not touch `docs/INTEGRATION_HONESTY.md` D111 (also stale on this same point, but outside
  this task's named scope — disclosed in the new paragraph itself as a pointer, not fixed here).

## Verification run

- `mypy` (no path args, manifest-scoped `packages = ["fleet"]`): `Success: no issues found in 129
  source files`.
- `ruff check` and `ruff format --check` on both touched files: clean. (Whole-file `ruff format
  --check src/fleet/cli.py` reports pre-existing drift across the file unrelated to this task's
  edits — confirmed identical on primary `main` before any edit; every line this task added or
  touched sits inside `ruff format`'s green regions, checked by locating my edits' current line
  numbers inside the format-diff and finding zero hunks there.)
- `tests/test_hoist_rollback_wiring.py` (whole file, no `-k`): 9 passed.
- `tests/test_integration_honesty_citations.py` (whole file, no `-k` — covers both
  `docs/INTEGRATION_HONESTY.md` and `docs/CRITERIA_PLAN.md` profiles): 70 passed. Verified my new
  CRITERIA_PLAN.md paragraph introduces no new pathed/anchored citation (no backtick-quoted
  `path.ext:line` shape appears in it) before relying on this.
- `tests/test_findings_kinds.py` (whole file, no `-k`): 4 passed — not touched (no finding *kind*
  was added, only payload fields on an existing kind) but run per the brief's instruction.
- Extra diligence beyond the brief: ran `tests/test_build_e2e.py` whole (77 tests, the other
  consumer of `_build_impl`/`_build_lines`). Result: 63 passed, 14 failed. **All 14 failures are
  pre-existing environment breakage, not caused by this task's changes**: every one is a
  `@pytest.mark.integration` test that drives real `bazel`/real disk I/O, and the run environment
  is at 100% disk capacity (`df -h /tmp`: 137M free of 591G) — most failures are a literal `OSError:
  [Errno 28] No space left on device` from `tempfile.mkdtemp`, the rest (real-bazel builds) end
  `REQUIRES_HUMAN_INTERVENTION` consistent with the same disk pressure. Confirmed unrelated to my
  edits: grepped the full failure output for any traceback frame inside the functions this task
  touched (`_write_hoist_rollback_failed_finding`, `_reconcile_hoist_rollbacks`'s call site,
  `_build_impl`'s return dict, `_build_lines`) and for `KeyError`/`AttributeError` on the new
  fields — zero hits. The one hoist-rollback-relevant test in that file
  (`test_a_real_build_failure_naming_a_hoisted_contracts_package_is_attributed_and_terminal`, which
  does NOT need real bazel) passed both in the full run and re-run standalone. This is a
  shared-machine resource constraint (many concurrent round VI worktrees), not something this task
  should or safely could remediate — flagged here per Guardrail 6 rather than silently excluded.

## Files touched

- `src/fleet/cli.py`
- `tests/test_hoist_rollback_wiring.py`
- `docs/CRITERIA_PLAN.md`

## Not touched (deliberately, out of scope)

- `docs/INTEGRATION_HONESTY.md` D111 — also carries the same stale "not yet built" framing for
  Leg D's git-mechanics/wiring slices, but the brief scoped Minor 3 to `docs/CRITERIA_PLAN.md`
  only. Disclosed, not fixed, in the new CRITERIA_PLAN.md paragraph.
