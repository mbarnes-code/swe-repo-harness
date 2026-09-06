# Task 55 report — DONE

§12.31 case (i), Leg A: not-shared-after-retarget detection + in-memory rollback, landed.
ADR-0120 (supplied by the controller mid-task) records the placement decision.

## Status: DONE

Legs B–E, case (ii), the unhoist blast set, and `--forbid-hoist`/`--force-hoist` are explicitly
out of scope and untouched, per the brief.

## What landed

- `src/fleet/graph/cycles.py::_hoist_contracts` — inside the greedy commit loop, after
  `materialized = _materialize(edges, (chosen,))` and before `chosen` enters `committed`/`nodes`,
  counts distinct `src_id` repos on `materialized`'s inbound `CONTRACT_CONSUME` edges for
  `chosen.contract_id`. Below `min_consumers`: records `chosen` as a `ContractStatus.REJECTED`
  copy with `status_detail="not_shared_after_retarget"`, raises a `ContractNotShared`
  `GraphFinding` (`severity="warn"`), drops the contract from `remaining`, and — critically —
  never reassigns `edges` to `materialized`, so the pre-hoist snapshot survives with no
  reconstruction needed. `graph`/`nodes`/`committed`/`hoisted` are equally untouched, so `live` is
  still non-empty when `_resolve_scc` re-checks it: the "SCC dissolved with no hoist committed"
  guard cannot trip, and control falls through to 6d/6e unchanged.
- `min_consumers: int | None = None` threaded as a new keyword-only parameter through
  `break_cycles → _resolve_scc → _hoist_contracts`, resolved to `ContractsSection().min_consumers`
  when `None` (never a literal default, never a `GraphSection` field — one source of truth). No
  CLI flag; every production call site resolves from settings.
- `CycleReport` gained `findings: tuple[GraphFinding, ...]` (already existed, now populated) and
  a new `rejected_contracts: tuple[ContractNode, ...]` field.
- `src/fleet/cli.py`: `_rejected_contract_rows` (mirrors `_hoisted_contract_rows`) and
  `_persist_contract_not_shared_findings` (mirrors `_persist_cycle_findings`, DELETE-then-INSERT
  keyed on `(run_id, kind='ContractNotShared')`, `repo_id` non-NULL since a contract has an
  owner), wired into `_sequence_impl`'s existing `StateWriter` block alongside the hoisted-rows
  write.
- `docs/DECISIONS.md` — ADR-0120 (placement decision: inside the loop, not "at the end of 6c-H"
  per SPEC's prose, and why an "end of 6c-H" reading would require reconstructing a retargeted
  edge from `retargeted_from_repo_id` alone, which `DependencyEdge._node_shape` cannot do).
- Documentation obligations (brief's list, all done in the same commit range):
  `src/fleet/state/schema.sql` and `docs/SPEC.md`'s matching CAVEAT ("five have no Python at all"
  → four, `ContractNotShared` moved to the live-writer group); `tests/test_findings_kinds.py:736`'s
  docstring (same correction); `cli.py::_sequence_graph_config`'s docstring (the "`break_cycles`
  takes no per-invocation overrides at all" sentence amended to note `min_consumers` is
  config-derived, not CLI-driven, so the sentence's substance — no CLI flag threads to it — still
  holds); `docs/CRITERIA_PLAN.md`'s §31 entry (Leg A landed, case (i) closed, case (ii) and Legs
  B–E untouched); `docs/INTEGRATION_HONESTY.md` D111 (dated in-body marker recording Leg A;
  heading left `OPEN`, correctly — D111 is not closed).

## A genuine, disclosed finding beyond the brief's scope: the E2E fixture shape had to differ from the unit fixture's

The brief's unit-level fixture design (`not_shared_fleet` in `tests/test_graph_cycles.py`, hand-built via
the `contract()` helper) uses a contract whose `consumers` dict **includes the owner itself** as
one of two declared consumers. That shape is correct and necessary for the unit test (which
constructs `ContractNode` objects directly), but I measured that it is **not reachable through
real `fleet scan` detection** at all, for two independent, both-confirmed reasons:

1. `workers/contracts.py::_consumers` (§3.1 5b (v)) structurally excludes the owner from
   `consumer_repo_ids` at detection time (`found = {c.repo_id for c in sources if c.repo_id !=
   owner}`, and the `generated`/symbol joins repeat the same exclusion) — a real scan can never
   produce an owner-is-its-own-consumer row.
2. More generally: *any* file-based consumer join (`sources`/`generated`) that adds a repo to
   `consumer_repo_ids` **also** adds it to `source_paths`/`generated_paths`, and
   `graph/infer.py::infer_contract_edges` synthesizes a `CONTRACT_CONSUME` edge from those two
   fields alone, independent of whether a real dependency edge ever existed. The *only* detection
   route that can add a declared consumer with **no** matching path entry is §3.1 5b (v)'s symbol
   join (`index.references`) — and `tests/test_workers_contracts.py`'s own "gap 2" finding (near
   its `IdentityService` reference-injection test) already records that **no shipped extractor
   emits a non-definition API reference symbol at all**, so that join is currently unreachable
   from real scan data.

Net: a real `fleet scan` cannot currently produce a contract whose declared consumer count
exceeds its post-retarget count, by any route. The E2E test
(`tests/test_sequence_e2e.py::test_a_not_shared_after_retarget_contract_is_rejected_through_the_real_cli`)
therefore: runs a real 2-repo `fleet scan` (confirmed by the test itself to genuinely produce
`extractable=0, status='REJECTED', status_detail='min_consumers'` with 1 real consumer, before
touching anything), then completes the ONE row real detection cannot yet produce with a direct SQL
`UPDATE` adding a second declared consumer (`acme-ghost-consumer`, a nominal identifier with no
`source_paths`/`generated_paths` entry and no real `repos` row — verified harmless, since nothing
downstream resolves `consumer_repo_ids` against `repos`), mirroring `test_workers_contracts.py`'s
own precedent for injecting a synthetic `SymbolRef` when a real extractor can't supply the input
yet. `fleet sequence` then runs fully real, and all assertions are against the database. This is
disclosed as a finding for the controller/future research, not something I decided to paper over —
it may be worth a note somewhere that §12.31 case (i)'s real-world trigger is currently gated on
the same "no extractor emits non-definition references" gap `test_workers_contracts.py` already
documents. I did not allocate a new D-number for it, per my constraints.

## Tests run, whole, no `-k`

- `pytest tests/test_graph_cycles.py -q` → **24 passed** (measured baseline at dispatch commit
  `d5a97e5`, working tree stashed: **23 passed**; +1 for my new test. All 6 of the brief's named
  `multi_chord_fleet` tests pass unchanged — the shared fixture's body changed, not their
  assertions — see below).
- `pytest tests/test_graph_cycles.py tests/test_graph_sequence.py tests/test_workers_contracts.py
  tests/test_findings_kinds.py -q` → **92 passed** (brief's stated baseline for the first three
  was 48; `test_graph_sequence.py` wasn't in the brief's baseline pair but IS a second consumer of
  `multi_chord_fleet` — see below — so I added it to every run from the start).
- `pytest tests/test_sequence_e2e.py -q` → **7 passed** (baseline 6 + my 1 new test).
- `pytest tests/test_integration_honesty_citations.py -q` → **70 passed** — NOT in the brief's
  suggested set; I added it after my `cli.py` edits (net +92 lines, several insertion points)
  caused real citation drift in `docs/INTEGRATION_HONESTY.md` and `docs/CRITERIA_PLAN.md` (10
  citations resolved to the wrong span; one previously-correctly-pinned-unresolved citation,
  `TransformInput`/`cli.py:4025`, began resolving through unrelated drift). All repointed; the one
  drift-into-resolution pin retired per this file's own documented policy, annotated at both the
  pin site and the ledger entry it cites (`docs/INTEGRATION_HONESTY.md:2780` area), never silently.
  The module's own stated census number (56 → 55 anchored-unresolved) corrected to match, per its
  own `test_every_census_number_this_module_states_is_the_number_it_derives`.
- `pytest tests/test_bazel.py tests/test_scan_e2e.py tests/test_contracts_criterion_scale.py -q`
  → **87 passed, 16 skipped** (`bazel is not installed on this host` — pre-existing environment
  gap in this worktree, `tools/bin/` has no `bazel`; unrelated to this change, confirmed by
  checking `tools/bin/` contents).
- `python -m mypy` (no path arguments) → **Success: no issues found in 129 source files**.
- Combined final sweep: `pytest tests/test_graph_cycles.py tests/test_graph_sequence.py
  tests/test_workers_contracts.py tests/test_findings_kinds.py tests/test_sequence_e2e.py
  tests/test_integration_honesty_citations.py -q` → **169 passed**.

Excluded: the full `pytest tests/` suite (~15 min per `CLAUDE.md` §6) was not run; the covering
set above was derived from the brief's own list plus what I independently found touches the
changed lines (graph/cycles.py, cli.py's writer wiring, and — newly discovered — the citation
survey over the two docs I edited).

## `multi_chord_fleet` disposition (brief's measured hazard)

All 6 originally-named tests in `tests/test_graph_cycles.py` (lines 274, 297, 313, 324, 336, 760
at dispatch) go green **unchanged** — I repaired the shared fixture itself rather than any test
assertion, per the brief's Agent Recommendation: added a third repo, `acme-c`, with one edge INTO
each contract's owner (`acme-c → acme-b`, same evidence-path pattern as `acme-a`'s), giving both
`K1`/`K2` a genuine second consumer outside the 2-cycle. Measured before landing (real
`break_cycles`/`assign_waves`, not assumed): `res.members` unchanged, the retarget's
`retargeted_from_repo_id`/`kind` sets unchanged, `min(repo_wave_index.values())` unchanged
(still `> 0`), and the `acme-a`-vs-`acme-b` wave ordering unchanged.

**One assertion elsewhere DID have to change, and I found it independently of the brief** (which
only measured `test_graph_cycles.py`'s six call sites): `tests/test_graph_sequence.py` imports and
calls `multi_chord_fleet()` at two more sites.
`test_a_hoisted_contract_migrates_before_the_repos_that_consume_it` needed **no change** — its
assertions (`wave(0)` contract-only, `acme-a` before `acme-b`, `min(...) > 0`) all survive the
third repo unchanged (measured: `repo_wave_index = {'acme-a': 1, 'acme-b': 2, 'acme-c': 1}`).
`test_criterion_c_counts_only_ungated_repos` **did** need a real change: it asserted
`plan.repo_members == ("acme-a", "acme-b")`, which is now genuinely 3 repos. Fixed by adding
`"acme-c": RepoStatus.PENDING` to the test's `statuses` dict and updating the tuple to
`("acme-a", "acme-b", "acme-c")` — not a weakened assertion, a corrected one reflecting the
fixture's new, deliberately-repaired shape; `check_criterion_c` still reports `ok: True` with the
third repo's status supplied.

## Mutation table (Rule 12)

All three mutations applied to `src/fleet/graph/cycles.py` only, one at a time, via
`cp` backup/restore (not `git checkout`, so no risk of clobbering other uncommitted state);
`git diff --numstat --no-index BACKUP MUTATED` read **before** any test result, every time.

| # | Mutation | Gate (lines changed) | Test run | Result | Runtime |
|---|---|---|---|---|---|
| A | `<` → `<=` in the threshold comparison | 1 (real) | `pytest tests/test_graph_cycles.py -q` | **3 failed / 21 passed** | 1.27s (1.80s wall) |
| B | Disable the check (`if len(...) < min_consumers:` → `if False and len(...) < min_consumers:`) | 1 (real) | `pytest tests/test_graph_cycles.py -q` | **1 failed / 23 passed** | 1.25s (1.81s wall) |
| C (control) | Cosmetic reflow/reindent of the same set-comprehension region, no semantic change | 4 (real, whitespace-only) | `pytest tests/test_graph_cycles.py -q` | **24 passed / 24** (stays green) | 1.20s (1.78s wall) |

Both A and B are genuine, plausible **partial** failures (neither all-fail-implausibly-fast nor
all-pass) — read as the tell in both directions per CLAUDE.md, this rules out both an unimported
mutation and a module outage.

Per-case discrimination (not case counts):
- **Mutation A** (boundary error) is discriminated by 3 pre-existing tests that use
  `multi_chord_fleet` (`test_saturating_trial_dissolves_the_multi_chord_cycle`,
  `test_component_size_is_one_when_the_hoist_dissolved_the_scc`,
  `test_hoisted_contract_is_a_sink_and_the_retarget_records_its_origin`) — after the fixture
  repair, both `K1`/`K2` sit at **exactly** `min_consumers=2` post-retarget consumers, so `<=`
  wrongly rejects them. My own new unit test does **not** discriminate mutation A: its fixture's
  post-retarget count is 1, strictly below 2 either way, so `<` and `<=` agree on it. This is the
  correct, disclosed non-discrimination the brief's own Rule 12 discipline asks for, not an
  omission.
- **Mutation B** (check disabled) is discriminated by exactly one test, uniquely:
  `test_a_not_shared_after_retarget_contract_is_rejected_with_an_exact_rollback`. No
  `multi_chord_fleet`-based test discriminates it, because none of them are below threshold to
  begin with — disabling a check that never fires for them changes nothing observable.
- **Control C** stays green everywhere, confirming the instrument watches meaning (the threshold
  comparison and what depends on its outcome) rather than layout.

**What quantity this test watches, and why the defect could not leave it unchanged.** The unit
test watches five things together: `report.rejected_contracts` (cardinality + `status`/
`status_detail`), `report.findings` (cardinality + `kind`/`severity`/`repo_id`/`payload`),
`report.hoisted_contracts` (must be empty), the SCC's `break_strategy` (must not be
`CONTRACT_HOIST`), and byte-exact equality of the two real repo↔repo edges against their pre-hoist
snapshot. A mutation that disables or loosens the threshold check changes what gets committed
(hoisted vs. rejected) — it cannot leave `hoisted_contracts`/`rejected_contracts`/`findings`
unchanged, because those three fields are populated by exactly one of two mutually exclusive
branches in `_hoist_contracts`' loop body (commit or reject), and a boundary error moves specific,
independently-verifiable fixtures across that branch line. The E2E test watches the same five
facts as real database rows (`contracts.status`/`status_detail`, one `findings` row,
`wave_members` absence, `phases.attempts` equality) after driving the real CLI; I did not run a
separate mutation battery against it because it exercises the identical `_hoist_contracts` code
path the unit-level mutations already discriminate — its job is proving persistence and wiring,
which is proven by construction (the writer functions are trivial pass-throughs of what
`break_cycles` already decided), not by a second mutation pass.

## Concerns / disclosures

1. The E2E fixture shape had to differ materially from what the brief's unit-fixture design would
   suggest, for reasons measured above (owner-as-consumer and the symbol-join gap are both
   currently unreachable from real `fleet scan`). This is disclosed prominently, not hidden.
2. While sweeping for citation drift I found and fixed one **pre-existing, unrelated** stale
   citation in `tests/test_graph_cycles.py` (a `_persist_contract_edges` line-range citation, wrong
   even before my edit — `cli.py:3363-3410` pointed at unrelated code at dispatch commit `d5a97e5`)
   since it sat in a file I was already editing. I explicitly did **not** chase three similar
   pre-existing stale citations I found in `tests/test_config_keys_are_read.py`,
   `tests/test_cli.py`, and `tests/test_runner.py` (verified each was already wrong at the
   dispatch commit, unrelated to my change, and out of scope for §12.31 Leg A) — noted here rather
   than silently left for a future sweep to rediscover.
3. My assigned worktree was initially provisioned stale (`fa95469`, round L); resolved by
   branching directly off `3277b1c` (the dispatch commit) before starting, then rebasing onto
   `d5a97e5` after the controller's mid-task note that main had advanced (tasks 53/54 merged,
   confirmed no file overlap with this task's scope by diffing `3277b1c..d5a97e5`).
4. Full `pytest tests/` was not run (see "Tests run" above for the exact exclusion and why).

## Documentation obligations checklist (brief's list)

- [x] `src/fleet/state/schema.sql:307-313`-area CAVEAT corrected.
- [x] `docs/SPEC.md`'s mirror corrected identically (exact-match content, not reworded
  independently).
- [x] `tests/test_findings_kinds.py:736` docstring corrected.
- [x] `src/fleet/cli.py::_sequence_graph_config`'s docstring re-read and amended (judged: adding
  `min_consumers` as a config-derived, non-CLI-driven parameter does not falsify the sentence's
  substance, but the literal signature claim needed a footnote).
- [x] `docs/DECISIONS.md` — ADR-0120.
- [x] `docs/CRITERIA_PLAN.md` criterion 31 — Leg A recorded done, case (ii) open. §12.31's
  `docs/SPEC.md` criterion text itself untouched (Rule 14).
- [x] `docs/INTEGRATION_HONESTY.md` D111 — dated in-body marker; heading left `OPEN`.
