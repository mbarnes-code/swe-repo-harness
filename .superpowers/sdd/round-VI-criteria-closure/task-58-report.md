# Task 58 report — §12.31 / D111 Leg E: wire `--forbid-hoist`

**Base:** `main` at `6844a04` (not the stale worktree branch this worktree started on — see
"Worktree note" below). **Branch:** `agent/roundvi-task58`, created from `6844a04`.

## Status: DONE

## What changed

1. **`src/fleet/settings.py`** — `GraphSection.forbidden_contract_ids: tuple[str, ...] = ()`, an
   operator override threaded from `--forbid-hoist`, never hand-authored in `config/fleet.yaml`
   (documented in the field's own description).
2. **`src/fleet/graph/cycles.py`** — `_hoist_contracts`'s candidate filter gets one additional
   clause, `and contract.contract_id not in cfg.forbidden_contract_ids`, additive alongside Leg
   A's not-shared rejection branch (diff is 6 lines, all additions, zero touches to Leg A's own
   logic — confirmed via `git diff HEAD -- src/fleet/graph/cycles.py`).
3. **`src/fleet/cli.py`**:
   - `_sequence_graph_config`: deleted the `--forbid-hoist` refusal (the two-line exit-2 stub);
     `--force-hoist` keeps its own refusal untouched (scoped out — see "Judgment calls" below).
     Threaded `forbid_hoist` into `overrides["forbidden_contract_ids"]`. Corrected the docstring's
     stale "four of the six... other three" sentence to "four of the seven... other three"
     (re-derived from the actual code, not assumed — the pre-existing text was already wrong on
     the total, independent of this task).
   - `_sequence_impl`: computes `forbidden_owner` (the full CURRENT forbidden set — survivors from
     an earlier run's `--forbid-hoist` still `FORBIDDEN` in the loaded snapshot, plus this run's
     freshly-named ids), writes `contracts.status = 'FORBIDDEN'` via the new
     `_forbidden_contract_rows` (guarded `AND extractable = 1` — see its docstring for the CHECK-
     constraint reasoning), and writes one `ContractHoistOverride` finding per currently-FORBIDDEN
     contract via the new `_persist_contract_hoist_override_findings` (DELETE-then-INSERT keyed on
     `(run_id, kind)`, so it always reflects the CURRENT set, matching `_persist_cycle_findings`'s
     established shape).
   - `_committed_contracts`: widened `WHERE status IN (...)` to include `'FORBIDDEN'`.
4. **`src/fleet/workers/contracts.py`** — `carry_over_committed`'s `survivors` filter widened to
   include `ContractStatus.FORBIDDEN`, so a `fleet scan` rebuild no longer resets a forbidden
   contract to `EXTRACTABLE`. This is the mechanism that makes the veto "sticky across
   re-sequencing" — deliberately NOT the same treatment `REJECTED` gets (that one is meant to be
   re-tried against fresh data; see the widened docstring).
5. **`docs/SPEC.md`**, **`src/fleet/state/schema.sql`** — corrected the `findings.kind` CAVEAT
   prose (both copies, same commit): `ContractHoistOverride` left the "no Python" group, narrowing
   it from 4/(WeakEdge+3) to 3/(WeakEdge+2, Legs C/D only).
6. **`tests/test_findings_kinds.py`** — docstring updated to match (five → three, via task 55 then
   task 58).
7. **Tests** (see below).

## Judgment calls (Agent Recommendations, not directives)

- **`--force-hoist` scoped OUT**, per the brief's own explicit escape hatch. It needs its own
  `_unpackaged.*` exit-2 carve-out (`docs/SPEC.md:6749-6752`) that `--forbid-hoist` does not, and
  bundling it would have grown this past one-shot size. Its refusal is untouched.
- **How `FORBIDDEN` survives a `fleet scan` rebuild.** SPEC's prose frames the survival mechanism
  as reading `findings` rows of kind `ContractHoistOverride` back out at scan time. I instead
  widened the ALREADY-EXISTING `committed`/`carry_over_committed` pass-through that already solves
  exactly this problem for `HOISTED`/`MIGRATED` — smaller, no new coupling between the contracts
  worker and the `findings` table, and produces the identical observable durability. The
  `ContractHoistOverride` finding is still written (satisfying "the finding is the authority" as
  an audit trail), it just isn't the thing that makes the *status* durable. Flagging this as a
  design choice, not a hard requirement — a future reader might prefer the literal SPEC mechanism.
- **`_forbidden_contract_rows` guards `AND extractable = 1`.** Without it, an operator-typed id for
  a never-extractable contract (`hoist_target_path IS NULL`) could become `FORBIDDEN`, and
  `carry_over_committed`'s hardcoded `extractable: True` on a survivor would later violate §6's
  CHECK constraint on the next INSERT. Every genuinely hoistable contract is already
  `extractable = 1` by construction, so this excludes only ids that were never real candidates.
- **`ContractHoistOverride` findings use plain DELETE-then-INSERT per `(run_id, kind)`, not a pure
  append-only audit log.** Chosen because the CURRENT forbidden set (derivable in-memory from
  `contracts` + this run's flag) is a complete, self-correcting source of truth, matching the
  existing `_persist_cycle_findings`/`_persist_contract_not_shared_findings` shape exactly, rather
  than introducing a third findings-persistence pattern into this file.

## Rule 12 proof

- **Old-passes/new-fails discriminator.** Restored `tests/test_scan_e2e.py` to its pre-task-58
  content (`git checkout HEAD -- tests/test_scan_e2e.py`) and ran
  `test_sequence_refuses_the_cycle_flags_it_cannot_thread` against the NEW `src/` code: it failed
  exactly as predicted — `AssertionError: ['--forbid-hoist', 'proto:acme'] was silently accepted;
  assert 0 == <ExitCode.USAGE: 2>`. Restored my edited test file (which removes `--forbid-hoist`
  from the refused list and asserts `SUCCESS` instead) and re-ran: passes.
- **Idempotence / stickiness, real second `fleet scan`.** New test
  `tests/test_sequence_e2e.py::test_a_forbid_hoist_veto_survives_a_real_re_scan_and_re_sequence`
  runs the real `cycle_fleet` fixture (a genuine 2-repo cycle with a real hoistable proto
  contract): `fleet scan` → `fleet sequence --forbid-hoist <id>` → asserts exit 0, `status =
  'FORBIDDEN'`, `status_detail = 'operator_forbid_hoist'`, one `ContractHoistOverride` finding row
  (`repo_id` = the owner) → a REAL second `fleet scan` (not merely a second `fleet sequence`) →
  asserts the row is STILL `FORBIDDEN` → a re-`fleet sequence` with the flag NOT repeated →
  asserts `hoisted == []` (never re-proposed). All assertions pass. Mutation-confirmed the
  `carry_over_committed` widening is load-bearing for this specific proof, not incidental: backed
  up `workers/contracts.py`, mutated `survivors`' predicate back to the pre-task-58
  `(HOISTED, MIGRATED)`-only shape, confirmed a genuine 1-line diff against the backup, re-ran this
  exact test — fails at the post-rescan assertion (`assert [('EXTRACTABLE',)] ==
  [('FORBIDDEN',)]`): without the widening the rebuild silently resets the veto exactly as
  predicted. Restored the backup byte-for-byte (empty diff) and re-ran: passes again.
- **Mutation proof.** Backed up `src/fleet/graph/cycles.py` (not diffed against `HEAD`, since the
  working tree already differs from it — diffed against the backup instead, per CLAUDE.md). Ran
  with `PYTHONPATH` pinned to this worktree's `src/` and verified `fleet.__file__` resolves inside
  the worktree before trusting any result (import-isolation guardrail). Mutated the new filter
  clause to `and True  # MUTATED`, confirmed `git diff --numstat --no-index BACKUP MUTATED` reports
  a genuine 1-line change, ran
  `tests/test_graph_cycles.py::test_a_forbidden_contract_is_excluded_from_hoisting_alongside_the_extractable_filter`:
  **FAILS** (`assert forbidden.hoisted_contracts == ()` — left contains one item, the contract WAS
  hoisted despite being on the forbidden set). Restored the backup byte-for-byte
  (`diff BACKUP RESTORED` empty) and re-ran: **PASSES**.
- **Additive-not-rewrite confirmation.** `git diff HEAD -- src/fleet/graph/cycles.py` is exactly
  one 6-line hunk, entirely new lines, zero touches to Leg A's not-shared branch below it.

## Tests run (exact files, whole, no `-k`, this worktree's `.venv`-pinned interpreter)

All via `PYTHONPATH` pinned or `cwd` inside this worktree (structural immunities per CLAUDE.md —
`pytest` with `cwd` in the worktree resolves `src/` correctly by construction):

- `tests/test_graph_cycles.py` — includes the new unit test — **passed** (part of a 29-test run
  with `test_findings_kinds.py`).
- `tests/test_findings_kinds.py` — **passed**.
- `tests/test_settings.py`, `tests/test_workers_contracts.py` — 77 passed.
- `tests/test_sequence_e2e.py` — 10 passed (includes the new e2e test).
- `tests/test_scan_e2e.py` — 33 passed (includes the displaced-refusal test).
- `tests/test_cli.py` — 189 passed.
- `tests/test_integration_honesty_citations.py` — 70 passed (see "Collateral fixes" below — this
  needed real repair, not a pass-through).
- `tests/test_config_keys_are_read.py`, `tests/test_no_state_outside_git.py`,
  `tests/test_read_transaction_statements.py`, `tests/test_lint_gate.py` — 134/135 passed on first
  run (1 failure, fixed — see below), all green after the fix.
- `python -m ruff check .` (whole tree, no path scoping) — clean.
- `python -m mypy` (no path args, manifest-scoped, `cwd` inside the worktree) — clean, 129 files.
- Full `pytest tests/` background run dispatched; result to follow in this report's addendum if it
  completes before hand-off, otherwise report the exit code to the controller directly.

## Collateral fixes this task required (disclosed, not silent)

My `cli.py` insertions (~127 net new lines across two spots: `_sequence_graph_config`/
`_sequence_impl`'s own growth, and the two new writer functions after `_rejected_contract_rows`)
shifted line numbers for everything below those insertion points. This broke **11 pre-existing,
unrelated anchored citations** in `docs/INTEGRATION_HONESTY.md` (7) and `docs/CRITERIA_PLAN.md`
(4) that pointed to other functions (`_transform_payloads`, `_AttemptWriter.record`,
`_reconcile_tasks_with_git`, `_sequence_impl` itself, `_continue_impl`, `_persist_contract_edges`,
`_TransformSink`, `validate_memory_budget`, `_repo_facts`, `_unit_deps`,
`_eligible_build_units`) by absolute line number, plus one **pinned "known-unresolved" citation**
(`_run_verify_wave` / `cli.py:9236`) that coincidentally started resolving by the same drift.
Measured this precisely, not assumed: `git stash` bisection showed **0 failures on true clean
HEAD**, **4 failures with only my `src/` changes applied** (docs/tests still reverted) — so this
was caused by my code, not inherited.

Repaired all 11 by repointing to the exact `defined at [...]` spans
`tests/test_integration_honesty_citations.py`'s own survey reported, each with a dated
"moved by round VI task 58's `cli.py` insertions" note. Retired the `_run_verify_wave` pin
following this file's own documented retirement convention (the citation names a usage site, not
a definition, and D84's standing rule is "usage sites are reported, not repointed to resolve" — so
it becomes an anchored-and-unpinned citation rather than being hand-repointed to the real call
site). Corrected the module's own self-checked census number (57 → 56, since retiring the pin
moves one citation from "pinned-unresolved" to "resolving") — `test_every_census_number_this_
module_states_is_the_number_it_derives` verifies this mechanically, not by trust.

Separately, the new `GraphSection.forbidden_contract_ids` field added a 182nd walked config key;
`tests/test_config_keys_are_read.py::test_the_scan_sees_a_real_config_surface` pins that count at
181 — updated to 182 (both the assertion and its docstring's "at time of writing" note).

Added `docs/INTEGRATION_HONESTY.md` D111 paragraph (Leg E landed, heading stays `OPEN` — legs B–D
remain) and a matching `docs/CRITERIA_PLAN.md` §31 update, both explicitly correcting Leg A's
now-stale "still open" list (annotated, not rewritten, per this project's history-preservation
convention). No new D-number or ADR-number allocated — the brief did not supply one, and per
CLAUDE.md's central-number-allocation rule that is the controller's call, not mine.

## Full suite

`pytest tests/ -q` (whole tree, no path/`-k` scoping, this worktree's pinned interpreter):
**32 failed, 2319 passed, 40 skipped, 1 xfailed** (the D97-pinned `strict=True` xfail already
disclosed in CLAUDE.md §6 — not suite rot). All 32 failures verified NOT caused by this task:

- 24 (`test_build_e2e.py`, `test_rewrite.py`, `test_workers_transform.py`,
  `test_new_language_touchpoints_e2e.py`) — `EngineUnavailableError`/`FileNotFoundError` for
  `ast-grep` and `uv`, missing on this host. Same class as the pre-existing `bazel`/
  `git-filter-repo`/`gh` SKIPs already in the log.
- 1 (`test_llm_backend_fixture_e2e.py::test_fixture_backend_serves_every_role_with_zero_src_fleet_changes`)
  — asserts `git status --porcelain src/fleet/ == ""` as its own precondition; this worktree is
  mid-task with real uncommitted changes, so this is expected until commit, not a regression.
- 3 (`test_ecosystems.py`) — re-ran against TRUE clean HEAD (`git stash push` of every task-58
  change, confirmed via `git status --porcelain`): **identical 3 failures reproduce on clean
  `6844a04`**, same assertions, same `cli.py` lines (pre-existing content, unchanged by this task
  — confirmed via `git show HEAD:src/fleet/cli.py | grep` for the exact matched text). Restored
  the stash afterward (`git stash pop`), confirmed `git status --porcelain` matches the pre-stash
  file list exactly.

No failure in the 32 touches any file this task modified for a reason connected to this task's
own logic.

## Sibling-task overlap check

Touched `graph/cycles.py`, `settings.py`, `workers/contracts.py`, and `cli.py`'s contract-
sequencing region. Task 59 (Leg B) is confined to `vcs/commits.py` — no overlap. Task 60 (Leg C1)
may touch `graph/collisions.py` and `cli.py`'s contract-sequencing region — my `cli.py` writes to
`contracts.status` are scoped exclusively to my own `'FORBIDDEN'` value (`_forbidden_contract_rows`),
which the brief names as the one kind of `cli.py` write in that region that is NOT a conflict
signal. I did not touch `graph/collisions.py` at all. No NEEDS_CONTEXT trigger observed from my
side; the controller should still diff against task 60's actual landed change at merge time, since
I have not seen it.

## Explicitly out of scope (unchanged)

- `--force-hoist`'s wiring (refusal untouched, by this task's own scoping judgment call).
- Leg A's own logic (`_hoist_contracts`'s not-shared branch) — untouched, confirmed by diff.
- Legs B, C, D.
