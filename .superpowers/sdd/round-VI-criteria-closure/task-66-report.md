# Task 66 report — §12.31 / D111 Leg C2: attribute a build failure to a broken hoist

**Status: DONE_WITH_CONCERNS**

(The section below dated 2026-09-06, first pass, recorded a `NEEDS_CONTEXT` block on the missing
ADR number. The controller subsequently allocated **ADR-0123**, verified free at `docs/DECISIONS.
md`'s then-current max `ADR-0122`. That block is kept below as history rather than deleted, per
this project's own record-keeping convention; the rest of this file is the completed task.)

## History — first pass, 2026-09-06, NEEDS_CONTEXT

The dispatch that launched this task did not include a controller-allocated ADR number, and the
brief was explicit that self-allocating one is forbidden (CLAUDE.md §3, Central Number
Allocation). A worktree was created and the brief re-read, but no implementation was attempted.
The controller then sent: "Controller allocation: **ADR-0123** (verified free — max in
`docs/DECISIONS.md` is currently ADR-0122, allocated at this moment, 2026-09-06)." Work resumed
in the same worktree/branch with that number.

## Status: DONE_WITH_CONCERNS

Leg C2's **detection half** is landed and proven: `ContractStatus.FAILED` is assigned for the
first time anywhere in `src/fleet/`, a `HoistBrokeOwner` finding is written, and the
`retryable=False` wiring is proven to reach the retry ladder (zero `phases.attempts` spent). Per
the brief's own instruction, **this does NOT close §12.31 case (ii) or the criterion as a whole**
— the rollback half (Leg D: `git revert -m 1` call site, unhoist blast set,
`HoistRollbackDemotion`, phase demotion, downstream-merge refusal) remains entirely unbuilt and is
explicitly out of this task's scope. "Concerns" below are the reason for `DONE_WITH_CONCERNS`
rather than plain `DONE` — none block the landed work, but each should be visible to the
controller/reviewer.

## Commits

Single commit on `agent/roundvi-task66` (this report's own commit is a second, separate commit —
see the end of this file). Files changed:

- `src/fleet/cli.py` — `HoistWatch` model; `BuildInput.hoist_watch` field; `BuildOutput.
  hoist_broke_contract_id`/`hoist_broke_target_path`/`hoist_broke_matched_line` fields;
  `_HOIST_BROKEN_LABEL_RE`/`_hoist_break_package`/`_hoist_break_match`/`_match_hoist_broke_owner`
  module functions; `BuildPipelineWorker._attribute_hoist_break` (wired into `run()`'s VERIFY_UNIT
  handling); `_BuildSink.__call__`'s new write (`contracts.status='FAILED'` + `HoistBrokeOwner`
  finding); `_hoist_watch_for_run` helper; `_build_payloads`/`_run_build_wave` threading.
- `src/fleet/workers/contracts.py` — `carry_over_committed`'s membership set widened to include
  `ContractStatus.FAILED` (sticky, like `HOISTED`/`MIGRATED`/`FORBIDDEN`), with the ADR-0123
  reasoning recorded in the docstring.
- `tests/test_workers_build.py` — unit tests for the matcher (all 5 real quoted bazel error
  strings plus constructed cases, each asserted individually) and for
  `BuildPipelineWorker._attribute_hoist_break`'s wiring (positive + four no-op negative cases).
- `tests/test_workers_contracts.py` — `test_a_failed_contract_survives_the_rebuild_it_is_not_part_of`,
  the ADR-0123 carry-over proof, mirroring the pre-existing `HOISTED` test.
- `tests/test_build_e2e.py` — one real-CLI end-to-end test
  (`test_a_real_build_failure_naming_a_hoisted_contracts_package_is_attributed_and_terminal`).
- `docs/DECISIONS.md` — ADR-0123 (the `carry_over_committed` judgment call).
- `docs/CRITERIA_PLAN.md` — criterion 31 updated: Leg C2 detection half landed; criterion still
  not DONE.
- `docs/INTEGRATION_HONESTY.md` — D111 annotated in-body (heading stays `OPEN`).

No changes to `docs/SPEC.md` (§12.31's criterion text is unchanged, per Rule 14; `HoistWatch` has
no doc-listing mirror since it lives in `cli.py`, which — unlike `models/build.py`'s §5.6 listing
— has no established SPEC.md code-listing convention for its locally-defined worker-envelope
types; `BuildInput`/`BuildOutput` themselves have none either).

## Test files run, whole, no `-k` filter, and their pass counts

Derived from what actually executes the changed lines (traced by reading call graphs from
`BuildPipelineWorker.run`/`_attribute_hoist_break`, `_BuildSink.__call__`, `_build_payloads`,
`_run_build_wave`, `_hoist_watch_for_run`, and `carry_over_committed`, then confirmed by grepping
every test file that imports or calls any of them):

| File | Result |
|---|---|
| `tests/test_workers_build.py` | 91 passed |
| `tests/test_workers_contracts.py` | 26 passed |
| `tests/test_build_e2e.py` (`-m "not integration"`) | 52 passed, 22 deselected (the real-bazel `@pytest.mark.integration` tests, excluded the same way this file's own convention excludes them from an offline run — not run because they need live network/registry access unrelated to this task, not because of anything this task touched) |
| `tests/test_wave_composition_projects_mid_wave.py` (calls `cli._run_build_wave` directly) | 5 passed |
| `tests/test_event_stream_wiring.py` (mentions `_run_build_wave` in a docstring only) | 6 passed |
| `tests/test_sequence_e2e.py` (exercises `carry_over_committed` via real `fleet scan`/`fleet sequence`) | 10 passed |
| `tests/test_cli.py` (imports `BuildPipelineWorker` for unrelated `_publish`/`_publish_module_lock` tests) | 189 passed |

**Excluded, and why:** the 22 `@pytest.mark.integration` real-bazel tests in `test_build_e2e.py`
(network/registry dependent, this file's own established offline-run convention — see its module
docstring). No other file importing the changed names does anything beyond what the table above
covers; the full list of files matching `grep -rln "_build_payloads\|_run_build_wave\|
carry_over_committed\|BuildPipelineWorker\b" tests/*.py` is exactly the six files in the table.

`python -m mypy` (no path arguments, manifest-scoped via `pyproject.toml`'s `packages =
["fleet"]`): `Success: no issues found in 129 source files`.

## The mutation table

Zero-diff gate: `git diff --numstat --no-index <backup> <mutated>`, read and confirmed non-zero
BEFORE reading any test result, for every mutation below. Backups taken once, before any
mutation, at `/tmp/claude-1000/.../scratchpad/task66-mutations/backup/{cli.py,contracts.py}` (a
per-lane scratch subdirectory, not a shared path). Each mutation restored via `cp` from that
backup and verified byte-identical (`diff -q`) before the next.

| # | Mutation | Gate (lines changed) | Tests run | Result | Runtime | Verdict |
|---|---|---|---|---|---|---|
| M1 | `carry_over_committed`: remove `ContractStatus.FAILED` from the sticky membership tuple | 0 ins / 1 del | `tests/test_workers_contracts.py` (whole, 26 tests) | 1 failed (`test_a_failed_contract_survives_the_rebuild_it_is_not_part_of`), 25 passed | 7.09s (clean: 7.53s) | Discriminates exactly the targeted claim; the pre-existing `HOISTED` test stays green |
| M2 | `_hoist_break_match`: drop the `/`-boundary, use bare `package.startswith(watched)` | 1/1 | `tests/test_workers_build.py` (whole, 91 tests) | 1 failed (the matcher test, on the added "longer sibling path" negative case), 90 passed | 18.75s (clean: 18.53s) | Discriminates exactly; proves the negative case I added closes the false-positive gap the brief warned about |
| M3 | `_attribute_hoist_break`: flip `retryable` to `True` instead of `False` | 1/1 | `tests/test_workers_build.py` (whole, 91) + `tests/test_build_e2e.py::test_a_real_build_failure_...` | Unit: 1 failed, 90 passed. E2E: 1 failed — `contracts.status`/finding still correct, but `phases.attempts` went to **3** (fully exhausted) instead of staying **0** | 19.65s / 7.06s | Discriminates at BOTH levels; the e2e failure is exactly the "proves retryable=False actually reached the ladder" assertion the brief called for |
| M4 | `_BuildSink.__call__`: invert the write gate (`is not None` → `is None`) | 1/1 | `tests/test_workers_build.py` (91) + e2e test | Unit: 0 failed (this path isn't unit-tested there). E2E: 1 failed (`contracts.status` stayed `HOISTED`, no write happened) — 91 passed, 1 failed overall | 23.76s | Discriminates; only the e2e test can see this integration-layer defect, which is itself informative — no unit test alone proves the `_BuildSink` wiring |
| Control | Cosmetic reflow of `_hoist_break_package`'s body (multi-line `if/else` expression, no behavior change) | 6 ins / 1 del | `tests/test_workers_build.py` (91) + `tests/test_workers_contracts.py` (26) + e2e test | 118 passed, 0 failed | 30.21s | Stays green — the tests assert behavior, not layout |

All runtimes are within the same order of magnitude as the clean baseline (no all-fail-implausibly-fast
outage, no unimported-mutation all-pass surprise on any RED result). Final restore verified
byte-identical to backup for both files; a full re-run of the same three-file battery after
restoration reproduced the clean 118-passed baseline exactly.

## What quantity each test watches, and why the defect could not leave it unchanged

- **`_match_hoist_broke_owner`/`_hoist_break_match` (matcher unit tests).** The quantity is the
  returned `contract_id | None` for a fixed `(log_text, hoist_watch)` pair. M2's mutation
  (dropping the `/`-boundary) can only leave this quantity unchanged for inputs where exact-match
  and bare-prefix-match agree — which is every one of my first seven cases, which is exactly why
  the eighth ("a longer sibling path") had to be added: it is the only case in the whole battery
  where a bare-prefix test returns a DIFFERENT contract_id (a false positive) than the exact/
  `/`-bounded test. Without that case, M2 would have been a mutation this test set could not
  express (Rule 12's "audit mutations for expressibility").
- **`test_attribute_hoist_break_flips_retryable_and_records_the_match` (unit).** The quantity is
  `updated.error.retryable`. M3 directly flips the one line that sets it, so the defect cannot
  leave this specific boolean unchanged — it is the ONLY place in the whole codebase that computes
  this value for this code path (verified by reading `_attribute_hoist_break`'s body: there is
  exactly one `model_copy(update={"retryable": ...})` call).
- **`test_a_failed_contract_survives_the_rebuild_it_is_not_part_of` (unit).** The quantity is
  `[n.status for n in carry_over_committed([], failed)]` — whether a `FAILED` row survives an
  EMPTY fresh set. M1 removes `FAILED` from the one `frozenset`-shaped membership tuple that
  decides survivorship, and the function's only branch reading that decision is the one this test
  exercises (`survivors = {... if node.status in (...)}` then `for contract_id, prior in
  survivors.items(): out[contract_id] = prior`) — there is no second code path that could keep the
  quantity unchanged under this specific mutation.
- **The e2e test's three DB assertions, independently.** `contracts.status` and the `findings` row
  watch whether `_BuildSink`'s new `if` block ever executes and what it writes (M4 zeroes this
  block's execution by inverting its guard — the write literally never runs, so the pre-existing
  `HOISTED` status and the finding's absence are the only possible outcomes, which is exactly what
  M4 produced). `phases.attempts` watches a DIFFERENT quantity entirely — the ladder's own charge
  counter, which only M3 (not M1, M2, or M4) can move, because it is written by
  `PhaseRunner`/`RetryPolicy` code this task explicitly never touches, gated solely on
  `WorkerError.retryable`. That three of my four mutations leave `phases.attempts` unchanged and
  only M3 moves it is the intended, checked separation — a single aggregate "did the e2e test
  pass" verdict would have hidden that M4's defect and M3's defect are different bugs at different
  layers, which is why the report lists per-assertion outcomes rather than a single pass/fail.

## The `carry_over_committed` judgment call — measured answer

**Decided: `FAILED` is sticky, added alongside `HOISTED`/`MIGRATED`/`FORBIDDEN` — NOT dropped like
`REJECTED`.** Full reasoning is in `docs/DECISIONS.md` ADR-0123; summary:

- A `REJECTED` contract's hoist was rejected in memory before any commit (Leg A's own not-shared
  branch never reassigns `edges`/`nodes`/`committed`), so the ORIGINAL duplication a fresh scan
  looks for is still physically present — `discover_contracts` naturally re-derives the identical
  `EXTRACTABLE` candidate on its own. Dropping it from the membership set is correct because the
  rebuild doesn't need help remembering it.
- A `FAILED` contract's hoist was already committed and merged (Leg C2 only fires from inside
  `BuildPipelineWorker.run`, which per `_eligible_contract_units`'s own docstring only runs
  `bazel build` transitively against an ALREADY-`HOISTED`/`MIGRATED` contract's package — so the
  commit landed before any consumer's build could possibly fail against it). Leg D (the revert)
  does not exist, so nothing has undone that commit — the code is physically in the monorepo
  exactly like a `HOISTED` row's.

**What I actually observed running the mechanism, not merely reasoning about it:** I did not run a
real `fleet scan` against a tree where a hoist had genuinely, organically moved code (that organic
path does not exist in production yet — see "Concerns" below), so I cannot report "I ran a fresh
scan against a `FAILED` row and watched `discover_contracts` return nothing for it" as a directly
observed fact about real source-tree state. What I DID measure directly: `carry_over_committed`'s
pre-existing `HOISTED` proof (`test_a_hoisted_contract_survives_the_rebuild_it_is_not_part_of`)
already demonstrates the EXACT mechanical shape a post-hoist rescan produces — an EMPTY fresh
set for the contract_id in question — and already proves `HOISTED` survives it. I extended that
same measured mechanism to `FAILED` (`test_a_failed_contract_survives_the_rebuild_it_is_not_part_of`)
and confirmed via mutation (M1 above) that removing `FAILED` from the membership set makes the row
vanish under that exact empty-fresh-set shape. This is a measurement of the mechanism
`carry_over_committed` actually runs, not an assumption about it — but it does not extend to
observing `discover_contracts`'s real behavior against a genuinely-post-hoist repository tree,
because no organic mechanism yet exists in `src/fleet` to produce one (D113/ADR-0119's own
disclosed scope boundary — nothing commits hoisted contract content or rewrites a consuming
repo's own source to reference it). I flag this distinction explicitly rather than overclaiming
"measured against a fresh scan" for the full round-trip.

## Concerns

1. **The e2e proof is through a seeded `contracts` row and a `cli.BAZEL_RUNNER` seam, not a fully
   organic real-bazel failure.** No production code populates `BuildUnit.contract_deps`
   (confirmed by `grep -rn "contract_deps" src/fleet/` — the field is declared in `models/
   build.py` and read nowhere), which is D113/ADR-0119's own disclosed "narrow read-only PASS 2b"
   scoping, not a gap this task introduced. Building a genuinely organic real-bazel scenario would
   require standing up a real proto/openapi toolchain plus the contract-consumption wiring D113
   defers — out of this task's scope. The e2e test instead seeds the `contracts` row directly
   (this file's own established convention — see the pre-existing direct `INSERT INTO stubs` at
   `tests/test_build_e2e.py:3016`) and fakes only the ONE `bazel build` call that fails, through
   the same `cli.BAZEL_RUNNER` seam the majority of this file's ~50+ other tests already use.
   Whether real bazel genuinely spells its errors the way my fixture text does is answered
   independently, at the unit level, against the FIVE real quoted strings already in this
   codebase's own adapter docstrings — never invented for this task.
2. **Known interim crash risk, disclosed not fixed here.** ADR-0122 (Leg D design, Decision 1,
   already landed before this task) found that `_graph_edges` (`cli.py:3858-3902`) applies no
   `contracts.status IN (...)` filter symmetric to `_graph_nodes`'s, so ANY code setting
   `contracts.status = 'FAILED'` — this task's own new capability — can make the very next `fleet
   sequence` crash with `GraphError` on an edge naming a vanished contract node. That fix is
   `task-65-brief.md`'s own scope (Leg D slice 1, dispatched concurrently with this task, in a
   separate worktree, non-overlapping edit region) — not mine to fix, and not something my
   `carry_over_committed` widening makes better or worse (the crash risk exists the moment ANY
   code sets the status, regardless of survive-vs-drop). Recorded in ADR-0123's own "Known interim
   consequence" section too.
3. **The e2e test's assertion on `phases.status == 'REQUIRES_HUMAN_INTERVENTION'` depends on
   shared retry-ladder code this task does not own** (`RetryPolicy._terminal_status`,
   `retry.py:255-260`) — if a future round changes that default for `BUILD_ERROR`, this specific
   assertion would need updating, though the `phases.attempts == 0` assertion (the load-bearing
   one for THIS task's own claim) would not.
4. **Schema comment staleness, pre-existing, not touched.** `schema.sql`'s own comment on the
   `contracts` table (`state/schema.sql:208-211`) still only names `'HOISTED'`/`'MIGRATED'` as
   surviving a rebuild — it was already stale before this task (Leg E's `FORBIDDEN` widening,
   task 58, didn't update it either), and this task's `FAILED` widening leaves it exactly as
   stale as it already was. Not fixed here per Rule 3 (surgical changes) — flagged for the
   controller to decide whether a future round should sweep it.

## Explicitly out of scope, confirmed untouched

- `git revert -m 1` call site, unhoist blast set, `HoistRollbackDemotion`, phase demotion,
  downstream-merge refusal — all Leg D, not designed here.
- `graph/cycles.py::_hoist_contracts` (Leg A) — untouched.
- `--forbid-hoist`/`--force-hoist` (Leg E) — untouched.
- `retry.py`/`orchestrator/runner.py` — untouched; `git diff --stat` confirms neither file appears
  in this commit.
- No D-number allocated (D111 already covers this criterion, per the brief).
- `docs/SPEC.md`'s §12.31 criterion text — unchanged (Rule 14).
