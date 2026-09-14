# Worker report: §15.1 item 3, Wave 7.1 Batch 28 — `cli.py` sequence/graph subsystem (G4)

## Status: DONE

Branch: `agent/roundviii-mutation-batch28` (from `main` at `408ce8a`)
Worktree: `/home/redmage/swe repo harness worktrees/wt-roundviii-mutation-batch28`
Commit: `73efa1e` (tests), this report added in a follow-up commit on the same branch.

`src/fleet/cli.py` is **unchanged** — every mutation below was applied, verified to produce a
non-empty diff (`git diff --stat` against a byte-identical backup taken before any mutation),
then restored byte-identical (`diff -q` against the backup) before moving to the next target.
`git status` on the branch shows only two test files touched. No production code was touched.

## Scope (per brief, = research report's internal "Batch 25", renumbered +3 to 28)

`_sequence_impl` (proper sequencing logic, excluding already-proven helpers), `_sequence_graph_config`,
`_phase1_exit_report`, `_persist_cycle_findings`, `_persist_contract_not_shared_findings`,
`_refuse_unresolved_collisions`, `_graph_nodes`, `_sequence_contracts` — all in `src/fleet/cli.py`
(§10's `fleet sequence` command, §3.1 steps 6–8).

## Per-function results

### 1. `_sequence_graph_config` (`cli.py:3288-3361`) — **new tests written, zero prior coverage of the refusal branches**

The function's docstring names three of §10's cycle flags (`--break-cycles manual`,
`--accept-breaks`, `--force-hoist`) that parse but have no matching `break_cycles()` parameter, and
are therefore REFUSED with exit 2 rather than silently dropped. Grepped every reaching test file
(`test_graph_cycles.py`, `test_sequence_e2e.py`, `test_cli.py`) for `break.cycles.manual`,
`accept.breaks`, `force.hoist`: **zero hits outside the flag's own `typer.Option` declaration in
`cli.py`** — the refusal path had no coverage at all, at any level (unit or e2e).

**New tests, `tests/test_cli.py`** (direct unit-level calls to `_sequence_graph_config`, matching
the precedent `test_the_findings_writer_never_mislabels_a_different_kind_in_report_findings`
already sets in this codebase for testing a `cli.py` private helper directly):

- `test_sequence_graph_config_refuses_break_cycles_manual`
- `test_sequence_graph_config_refuses_accept_breaks`
- `test_sequence_graph_config_refuses_force_hoist`
- `test_sequence_graph_config_accepts_forbid_hoist_and_the_other_five_threaded_flags` (control:
  every flag that IS threaded, all three refused flags at non-refusing defaults, must still work)

**Mutations** (one per refusal `if`, `if X:` → `if False:`/`if False and X:`):
- `break_cycles_mode is not AUTO` guard: diff non-empty (1 line); **reddened**
  `test_sequence_graph_config_refuses_break_cycles_manual` (`DID NOT RAISE UsageError`); the other
  three tests stayed green (narrow, not a module-wide outage). Restored; re-ran: 4/4 pass.
- `if accept_breaks:` guard: diff non-empty; **reddened** only
  `test_sequence_graph_config_refuses_accept_breaks`. Restored; re-ran: 4/4 pass.
- `if force_hoist:` guard: diff non-empty; **reddened** only
  `test_sequence_graph_config_refuses_force_hoist`. Restored; re-ran: 4/4 pass.
- Control mutation on the accepted path (`if forbid_hoist:` → `if forbid_hoist and False:`): diff
  non-empty; **reddened** only the control test (`result.forbidden_contract_ids == ()` instead of
  the expected tuple). Restored; re-ran: 4/4 pass.

Four independent discriminators, one per branch, each targeted narrowly (no cross-test bleed).

### 2. `_sequence_impl` (`cli.py:3496-3703`) — mostly confirmed sound via existing tests; **one new test for a genuinely uncovered inline branch**

Read the whole function and traced every distinct branch against the existing suite before writing
anything new, per the brief's "not any already-proven helper" instruction:

- in-flight refusal / `--force-resequence` override: proven by `test_cli.py`'s exit-11 section
  (`_put_in_flight`, `test_force_resequence_overrides_the_refusal`).
- contracts threading (`hoist_contracts` gate on `_sequence_contracts`), `min_confidence`
  threading, `min_consumers` threading: proven by `test_sequence_e2e.py`'s
  `test_the_configured_min_consumers_value_actually_reaches_the_6c_h_check` and the
  `not_shared_fleet`/`cycle_fleet` family (baseline-first, mutually-exclusive-outcome tests — read,
  not re-mutated, already Rule-12 quality by their own docstrings).
- freshly-audited collision block (`audit_collisions` call, `_collision_rows` write, the
  `collisions.blocking` exit-6 raise, persisted-before-raise ordering): proven end to end by
  `tests/test_collisions_wiring.py::test_a_divergent_ecosystem_contest_exits_6_with_the_row_already_written`
  (injected-fault fixture, asserts both the exit code and that the row is durable before the
  process exits — read, not re-mutated).
- **the `manual` SCC exit-6 raise (lines 3544-3551)** — inline logic, not delegated to any named
  helper — had **no coverage above the direct `graph/cycles.py::break_cycles()` unit level**
  (`test_graph_cycles.py::test_beyond_scc_hard_max_the_harness_refuses`,
  `test_a_41_repo_cycle_completes_without_hanging` — neither goes through `cli.py` at all).

**New test, `tests/test_sequence_e2e.py`**:
`test_a_scc_beyond_hard_max_refuses_sequencing_with_exit_6_through_the_real_cli`, plus a new
fixture `cycle_fleet_low_scc_hard_max` (identical to `cycle_fleet` but `fleet.yaml` sets
`graph.scc_hard_max: 1`, below the real 2-repo SCC's member count) — real git repos, real scan,
real `fleet sequence` through the CLI (`--skip-contracts` so the SCC resolves via the edge-break/
atomic-wave/MANUAL ladder rather than contract hoisting).

- **Mutation**: `if manual:` → `if False and manual:` around the `raise UnresolvedFindingsError`.
  `git diff --stat`: non-empty (1 line).
- Result: **reddened** — with the guard disabled, the run instead fails a *different* check
  further down (`_phase1_exit_report`'s criterion (b)/(c), `SequenceCriterionError`, message text
  `"MANUAL members not REQUIRES_HUMAN_INTERVENTION"` / `"ungated repos vs ... REPO wave members"`)
  — the new test's exact-substring assertion (`"resolved MANUAL and can never be sequenced"`) does
  not match that message and fails, proving the assertion targets this function's own inline raise
  specifically and not merely "some exit-6 error fires somewhere downstream". (Both errors happen
  to share `exit_code=6` — `UnresolvedFindingsError` and `SequenceCriterionError` both map to
  `ExitCode.UNRESOLVED_FINDINGS` — so the exit-code assertion alone is not the discriminator here;
  the message-substring assertion is, and it is checked before the exit-code assertion is trusted.)
- Restored; re-ran: 1/1 pass (real message: `"1 SCC(s) resolved MANUAL and can never be sequenced:
  scc:af29c2c43b344c73. §10 exit 6 — resolve them, or exclude their members."`).

**Verdict: `_sequence_impl` sound overall; one genuine coverage gap (the inline MANUAL-SCC exit
path) closed with a new mutation-proven test.**

### 3. `_phase1_exit_report` (`cli.py:3364-3493`) — confirmed sound, no new test

Traced the `never_manifest_discovered` re-scoping (excludes `EmptyRepo`/`PreflightFailed`/
config-skipped repos from criterion (a)'s unfiltered-repo assumption, per the function's own
docstring on why the naive composition is unsatisfiable for a real fleet).

- **Mutation**: `never_manifest_discovered = set(config_skipped_repo_ids) | {...}` →
  `never_manifest_discovered: set[str] = set()`. `git diff --stat`: non-empty (1 net line).
- Target: `tests/test_scan_e2e.py::test_a_library_is_sequenced_before_the_app_that_depends_on_it`
  (fixture includes `acme-empty`, a real `EmptyRepo`).
- Result: **reddened** — `fleet sequence` exits `UNRESOLVED_FINDINGS` with `"(a) no manifest and no
  'no-manifest' finding: ['acme-empty']"` instead of `SUCCESS`.
- Restored; re-ran: 1/1 pass.

The `known_paths`/`evidence_exists` wiring (D114 (b)) is separately proven by
`tests/test_scan_e2e.py::test_scan_captures_file_blobs_and_sequence_resolves_every_edges_evidence_path`
and its discriminator sibling — read, not re-mutated (already genuine old-fails/new-passes per
their own docstrings).

**Verdict: sound. No gap, no new test.**

### 4. `_graph_nodes` (`cli.py:3919-3938`) — confirmed sound, no new test

The `status IN ('HOISTED','MIGRATED')` filter (excludes `EXTRACTABLE`/`REJECTED`/`FORBIDDEN`
contracts from becoming graph nodes, per the function's own §3.1 5b (viii) docstring).

- **Mutation**: widened the SQL to `status IN ('HOISTED','MIGRATED','EXTRACTABLE')`.
  `git diff --stat`: non-empty (1 line).
- Ran the whole `tests/test_sequence_e2e.py` + `tests/test_scan_e2e.py` (45 tests) under the
  mutation: **3 failed, 42 passed** —
  `test_the_same_fleet_stays_cyclic_when_contracts_are_skipped`,
  `test_a_forbid_hoist_veto_survives_a_real_re_scan_and_re_sequence`,
  `test_a_not_shared_after_retarget_contract_is_rejected_through_the_real_cli` all reddened (each
  via `_phase1_exit_report`'s criterion (c): "0 HOISTED/MIGRATED contracts vs 1 CONTRACT wave
  members" or the equivalent).
- Restored; re-ran the same 45: 45/45 pass.

**Verdict: sound. No gap, no new test.**

### 5. `_sequence_contracts` (`cli.py:3941-3984`) — confirmed sound, no new test

The function's docstring calls out `source_paths`/`generated_paths` as load-bearing (what
`cycles._materialize` matches `edges.evidence_path` against — an omitted one "silently retargets
nothing").

- **Mutation**: `source_paths=list(json.loads(str(row[4]) or "[]"))` → `source_paths=[]`.
  `git diff --stat`: non-empty (1 line).
- Target: `tests/test_sequence_e2e.py` (12 tests, real scan+sequence over the contract-cycle
  fixture).
- Result: **reddened** —
  `test_the_hoisted_contract_produces_real_contract_impl_and_consume_edges_in_the_table` failed
  with `KeyError: ('CONTRACT_IMPL', 'acme-identity')` (no `CONTRACT_IMPL` edge materialized at all
  once the owner's source path is dropped).
- Restored; re-ran: 12/12 pass.

**Verdict: sound. No gap, no new test.**

### 6. `_persist_cycle_findings` (`cli.py:3722-3771`) — confirmed sound, no new test

DELETE-then-INSERT under one unit, keyed on `(run_id, kind)` — the stale-row-clearing half the
function's docstring commits to ("an SCC a re-scan dissolved must not leave a stale row behind").

- **Mutation**: removed the `DELETE FROM findings WHERE run_id = ? AND kind = ?` statement from the
  `unit()` closure, keeping only the `INSERT`. `git diff --stat`: non-empty (3 lines removed).
- Target: `tests/test_pr_e2e.py::test_sequence_writes_cycle_findings_and_the_projection_reports_them`
  (planted cycle, `fleet sequence` run **twice**, asserts the projection's cycle count is unchanged
  across the re-sequence — "re-derives rather than accumulating a second opinion", §11.7).
- Result: **reddened** — the second `fleet sequence` invocation crashed with
  `sqlite3.IntegrityError: UNIQUE constraint failed: index 'ux_findings_ident'` (the schema's own
  uniqueness constraint on the finding's identity caught the duplicate insert the missing DELETE
  would otherwise have silently accumulated).
- Restored; re-ran: 1/1 pass.

**Verdict: sound. No gap, no new test.**

### 7. `_persist_contract_not_shared_findings` (`cli.py:3783-3836`) — confirmed sound, no new test

Existing coverage already at Rule-12 quality: `tests/test_sequence_e2e.py::
test_the_findings_writer_never_mislabels_a_different_kind_in_report_findings` unit-tests the
`own_kind = [f for f in findings if f.kind == CONTRACT_NOT_SHARED_FINDING_KIND]` filter directly
(own-kind vs. foreign-kind `GraphFinding`s, ahead of what the real producer can supply today), and
`test_a_not_shared_after_retarget_contract_is_rejected_through_the_real_cli` proves the real
DELETE-then-insert path end to end through the CLI.

- **Mutation 1** (filter): `own_kind = [f for f in findings if f.kind == ...]` → `own_kind =
  list(findings)`. `git diff --stat`: non-empty. **Reddened**
  `test_the_findings_writer_never_mislabels_a_different_kind_in_report_findings` (foreign-kind
  finding leaked into the `findings` table under the wrong kind). Restored; re-ran: 1/1 pass.

**Verdict: sound. No gap, no new test.**

### 8. `_refuse_unresolved_collisions` (`cli.py:3902-3916`) — **new tests written, genuine gap found**

The SQL reads `severity = 'error' AND resolution IS NULL`. Every collision kind actually wired
into production `fleet sequence` today is `COORDINATE` only (`cli._sequence_impl`'s
`CollisionInput(coordinates=coordinate_claims, owns_hints=...)` call passes no `contracts=`/
`dests=`/`files=`/`versions=` — confirmed by grep, zero other call sites construct `CollisionInput`
in `cli.py`), and for `COORDINATE`, `graph/collisions.py::_coordinate_collisions` keeps
`severity='error'` in exact lockstep with `resolution is None` (divergent ecosystem case). So
`resolution IS NULL` is currently redundant with `severity='error'` for anything a real fleet can
ever produce — **and confirmed genuinely untested**: ran the mutated SQL (dropped `AND resolution
IS NULL`) against the full combined suite `tests/test_sequence_e2e.py tests/test_cli.py
tests/test_collisions_wiring.py tests/test_pr_e2e.py` (255 tests) — **all 255 passed**, i.e. no
existing test anywhere distinguishes the two predicates.

But `graph/collisions.py::_contract_collisions` (CONTRACT kind) and `_dest_collisions` (DEST_PATH
kind, explicit-override case) both allow `severity='error'` alongside a **non-NULL** `resolution` —
disclosed elsewhere in this same file as "unwired — a separate, later task" (`_phase1_exit_report`'s
own docstring). The `resolution IS NULL` clause is there specifically so that when either detector
is wired in, an already-resolved error-severity collision does not spuriously block every
subsequent `fleet sequence`. This is load-bearing defensive logic with zero test coverage,
expressible today only by hand-inserting a row ahead of what the current real producer can supply —
the same pattern this codebase already uses for
`test_the_findings_writer_never_mislabels_a_different_kind_in_report_findings`.

**New tests, `tests/test_sequence_e2e.py`** (direct unit-level calls to
`_refuse_unresolved_collisions` against a hand-seeded db):
- `test_a_resolved_error_severity_collision_does_not_refuse_the_run` — `severity='error'`,
  `resolution='owner:acme-owner'` → must NOT raise.
- `test_an_unresolved_error_severity_collision_still_refuses_the_run` — same `severity='error'`,
  `resolution=NULL` → must raise `UnresolvedFindingsError` (control: proves the sibling test isn't
  merely passing because the function stopped checking `severity` altogether).

**Mutation**: dropped `AND resolution IS NULL` from the SQL. `git diff --stat`: non-empty (1 line).
- Result: **reddened** exactly the "resolved" test (`UnresolvedFindingsError` raised where none was
  expected); the "unresolved" control stayed green.
- Restored; re-ran: 2/2 pass.

**Verdict: genuine gap, closed with two new mutation-proven tests (one positive, one control).**

## Verification: which test file(s) I ran, and why sufficient

```
tests/test_sequence_e2e.py            (whole file, 15 tests — 4 new: _sequence_graph_config's
                                        siblings live in test_cli.py; this file gained
                                        cycle_fleet_low_scc_hard_max + 3 new tests)
tests/test_cli.py                     (whole file, 214 tests — 4 new _sequence_graph_config tests
                                        + the pre-existing exit-11/exit-6 sequence sections)
tests/test_scan_e2e.py                (whole file — mutation-proof target for _phase1_exit_report
                                        and part of the _graph_nodes combined run)
tests/test_pr_e2e.py                  (mutation-proof target for _persist_cycle_findings;
                                        also contains test_collisions_wiring-adjacent CycleDetected
                                        coverage read for _sequence_impl)
tests/test_collisions_wiring.py       (read for _sequence_impl's freshly-audited collision path;
                                        included in the combined 255-test run that established the
                                        _refuse_unresolved_collisions gap)
```

Final combined run after every mutation was reverted (`git status` showing `src/fleet/cli.py`
byte-identical to `main`):
- `tests/test_sequence_e2e.py`: **15 passed**
- `tests/test_cli.py`: **214 passed** (full file, twice — once before and once after the ruff
  import-order fix touched it)
- `tests/test_scan_e2e.py` + `tests/test_sequence_e2e.py` combined (used mid-investigation for the
  `_graph_nodes` mutation): **45 passed**
- `tests/test_sequence_e2e.py tests/test_cli.py tests/test_collisions_wiring.py tests/test_pr_e2e.py`
  combined (used to establish the `_refuse_unresolved_collisions` gap): **255 passed**

**Why this set and not the whole `tests/` directory:** every one of the 8 scoped functions is
reached, directly or via its docstring's own citations, by at least one of these five files — the
brief's suggested list (`test_graph_cycles.py`, `test_sequence_e2e.py`, `test_runner.py`,
`test_cli.py`) was checked by grep for each function name plus the domain-specific greps
(`CycleDetected`, `ContractNotShared`, `fleet.*sequence`) documented above; `test_graph_cycles.py`
and `test_runner.py` were read and found to reach only `graph/cycles.py`/`graph/sequence.py`
module-level functions and the orchestrator scheduler respectively, never the 8 `cli.py` functions
in scope (confirmed by reading their imports and call sites), so they are **excluded** from the
final verification run — running them would add ~15+ minutes for zero signal about this diff.
`test_workers_contracts.py` is imported *from* `test_sequence_e2e.py` for shared fixtures
(`CYCLE_FLEET`, etc.) but is not itself a reaching test file for any of the 8 functions and was not
separately re-run. No `src/` file changed, so this is not a regression sweep over unrelated code —
it is confirming the new tests are correct standalone and every mutation-proof target used during
the investigation is still green after every mutation was reverted.

## Lint / type checks

- `.venv/bin/python -m ruff check tests/test_sequence_e2e.py tests/test_cli.py`: initially 1
  line-too-long (E501) + 1 quoted-annotation (UP037) finding; the UP037 auto-fix then surfaced an
  F821 (the quoted annotation was covering a genuine forward reference — `FleetSettings` was only
  imported locally inside the helper). Fixed by moving `from fleet.settings import FleetSettings`
  to the module-level import block (removing the now-redundant local import inside the new
  `_graph_settings` helper) and wrapping the long line. Re-checked: **All checks passed!**
- `.venv/bin/python -m mypy` (no path args — full `mypy_path`/`packages` scope per CLAUDE.md §6;
  `packages = ["fleet"]` i.e. `src/`, unaffected by a tests-only change): **Success: no issues
  found in 132 source files.**

## Files touched

- `tests/test_cli.py` — 4 new tests (`_sequence_graph_config`'s three refusal branches + control),
  1 new helper (`_graph_settings`), 3 new imports (`BreakCyclesMode`, `UsageError`,
  `_sequence_graph_config`, `FleetSettings`).
- `tests/test_sequence_e2e.py` — 4 new tests (`_sequence_impl`'s MANUAL-SCC exit path,
  `_refuse_unresolved_collisions`'s resolution-vs-severity distinction × 2), 2 new fixtures/helpers
  (`cycle_fleet_low_scc_hard_max`, `_insert_collision`), 4 new imports (`UnresolvedFindingsError`,
  `_refuse_unresolved_collisions`, `connect_ro`).
- `src/fleet/cli.py` — **unchanged** (every mutation applied during this session was reverted;
  `git status` confirms no diff against `main`).

## Per-function summary

| Function | Result |
|---|---|
| `_sequence_graph_config` | **New tests written** (zero coverage of 3 refusal branches → 4 tests), all four mutation-proven with independent discriminators, restored clean. |
| `_sequence_impl` | Mostly confirmed sound against existing tests (in-flight, force-resequence, contracts/min_confidence/min_consumers threading, freshly-audited collision block); **one new test** for the previously CLI-uncovered inline MANUAL-SCC exit-6 raise. |
| `_phase1_exit_report` | Confirmed sound via fresh mutation against an existing test; no new test. |
| `_graph_nodes` | Confirmed sound via fresh mutation against 3 existing tests; no new test. |
| `_sequence_contracts` | Confirmed sound via fresh mutation against an existing test; no new test. |
| `_persist_cycle_findings` | Confirmed sound via fresh mutation against an existing test; no new test. |
| `_persist_contract_not_shared_findings` | Confirmed sound via fresh mutation against an existing test; no new test. |
| `_refuse_unresolved_collisions` | **New tests written** (genuine gap: `resolution IS NULL` vs. `severity='error'` alone, unreachable by any current fleet but load-bearing for the disclosed CONTRACT/DEST_PATH detectors once wired), 2 tests (positive + control), mutation-proven, restored clean. |

Do not merge, do not push.
