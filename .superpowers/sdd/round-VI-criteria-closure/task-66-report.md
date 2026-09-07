# Task 66 report — §12.31 / D111 Leg C2: attribute a build failure to a broken hoist

**Status: DONE_WITH_CONCERNS** (fix round complete — see "Fix round" section at the end for the
current, authoritative status, the Critical/Important findings closed, and freshly-measured
numbers; the disclosed concerns below are unchanged in kind, though C1/I3 specifically are now
fixed rather than merely disclosed)

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

## Fix round (2026-09-06/07) — controller review findings, all addressed

Controller review (opus-tier, task-scoped) verdict on commit `2c3e384`: Spec compliance ❌, Needs
fixes. Every finding was independently reproduced against a real seeded schema or a fresh pytest
run. This section is the authoritative, freshly-measured status after fixing all of them.

**Fix commit: `83a6644`** on `agent/roundvi-task66` (10 files changed, 366 insertions(+),
69 deletions(-); worktree/`HEAD` blob hashes verified matching for every changed path before this
report was written).

### Critical

**C1 — ADR-0123's decision was INERT in production. Fixed.** `cli._committed_contracts`
(`cli.py:2551-2558` at this commit), the ONLY production feeder of `carry_over_committed`'s
`committed` argument (`_sequence_impl`, `cli.py:2444`), still selected `WHERE status IN
('HOISTED','MIGRATED','FORBIDDEN')` — no `'FAILED'`. Widening `carry_over_committed`'s own
membership set (the first landing's whole fix) therefore did nothing observable: a real `FAILED`
row never reached it at all, was dropped by `_committed_contracts` first, and was re-derived fresh
as `EXTRACTABLE` on the next `fleet scan` — precisely the `REJECTED` treatment ADR-0123 argues
against, meaning a hoist that had just broken a build was silently re-hoisted on the very next
cycle. **Fix:** added `'FAILED'` to `_committed_contracts`'s own SQL `status IN (...)` list,
mirroring round VI task 58's `e3b1a86`, which widened BOTH halves for `FORBIDDEN` in the SAME
commit — the precedent this task's first landing should have followed and did not. **Proof:**
`tests/test_sequence_e2e.py::test_a_failed_contract_survives_a_real_re_scan` — a real fixture
(`cycle_fleet`), a real `fleet scan`/`fleet sequence` producing a genuinely `HOISTED` contract, a
`FAILED` write via the exact SQL `_BuildSink.__call__` runs in production, then
`_committed_contracts` called DIRECTLY (via a real `aiosqlite` connection) against the resulting
database — not a hand-built `ContractNode`, which is exactly the two-anchor blindness the review
named the pre-existing unit test as having. **Old-passes/new-fails verified**: reverting the SQL
fix (removing `'FAILED'` from the `IN (...)` list) reddens this new test while the pre-existing
`test_a_hoisted_contract_survives_the_rebuild_it_is_not_part_of`-family tests stay green
(reproduced live during this fix round, restored after). The pre-existing unit test
(`tests/test_workers_contracts.py::
test_a_failed_contract_survives_the_rebuild_it_is_not_part_of`) is annotated (not deleted) to
explain why it alone could not have caught this: it constructs `committed` nodes directly,
bypassing `_committed_contracts` entirely. **Why not a full second `fleet scan` instead of calling
`_committed_contracts` directly:** confirmed live that a second `fleet scan` after seeding a
`FAILED` row currently crashes with `GraphError` ("names node CONTRACT:proto:acme.identity.v1,
which is not in the fleet") — this is the disclosed `_graph_edges` gap `ADR-0122` already
documents and `task-65-brief.md` owns fixing, reproduced here as independent confirmation before
routing the test around it, exactly as the review's own "sequencing constraint" section
anticipated. `docs/DECISIONS.md` ADR-0123 and `docs/INTEGRATION_HONESTY.md` D111 both carry a
dated fix-round addendum recording this correction (the decision itself was always right; what
was missing was the second half of its wiring).

**C2 — 3 red tests on `main` from citation drift. Fixed.** This task's own ~224-line `cli.py`
insertion (first landing) drifted 13 pre-existing anchored citations in `docs/INTEGRATION_
HONESTY.md`/`docs/CRITERIA_PLAN.md`; a SECOND wave of this same fix round's own further `cli.py`
edits (the C1 SQL fix — no line shift — and the I3 docstring growth on `_hoist_watch_for_run`,
~15 lines) drifted 4 of those same citations AGAIN before this report was written (caught by
re-running the gate after finishing all code edits, not assumed clean from the first repointing
pass). Every citation was repointed by re-reading the actual current function definition at its
new location and confirming the span by content (`grep -n "^async def <name>("`/`^def <name>(`,
then reading the function body to confirm its end line) — never by applying a fixed line-number
offset. **Freshly measured: `tests/test_integration_honesty_citations.py` run whole, no `-k`: 70
passed**, matching `main`'s clean baseline exactly.

### Important

**I1 — 6 `ruff check` regressions. Fixed.** 2 unused imports (`_hoist_break_match`,
`_hoist_break_package` in `tests/test_workers_build.py`, removed — genuinely unused, confirmed by
grep before removing), 1 `ASYNC240` (a blocking `Path.mkdir` inside `async def runner` in
`tests/test_build_e2e.py`'s new fake-bazel seam — fixed by moving the `mkdir` to the plain `def`
factory function that constructs `runner`, called once at setup rather than per-invocation,
mirroring the shared `FakeBazel._result`'s own sync-helper split), 3 line-length (`E501`, wrapped).
**Disclosure the review asked for: no, I did not run `ruff check` in the first landing** — this
was the review's I1 finding, and it is correct that I omitted it. `ruff check .` (whole repo) now
passes clean, and I ran it before every code edit onward in this fix round, not only at the end.

**I2 — `HoistBrokeOwner`'s "no Python writer" declaration was stale in two files. Fixed.**
`src/fleet/state/schema.sql` and `docs/SPEC.md` both still said `HoistBrokeOwner` "remains
no-Python" alongside `HoistRollbackDemotion`. Both corrected, mirroring exactly the precedent
tasks 55/58 set in the same file for `ContractNotShared`/`ContractHoistOverride` (a dated bullet
recording which commit gave the kind its first live writer). **A parsing hazard found and worked
around while fixing this:** the CAVEAT block both files carry is parsed by `tests/
test_findings_kinds.py` with a regex (`_QUOTED = re.compile(r"'([^']+)'")`) that treats EVERY
single-quote-delimited substring in the whole block as a declared kind name — an ODD number of
possessive apostrophes (`cli.py's`, `hoist's`, `repo's`) in one new sentence broke the pairing and
produced two failing tests with multi-line garbage "kind names" as the diff. Fixed by rephrasing
to avoid unpaired possessive apostrophes entirely (`"in the cli.py _BuildSink.__call__ method"`
rather than `"in cli.py's _BuildSink.__call__"`). **Freshly measured: `tests/
test_findings_kinds.py` run whole, no `-k`: 4 passed.**

**I3 — attribution was per-run in claim but per-wave-until-first-hit in mechanism. Fixed (option
a).** `_hoist_watch_for_run` is recomputed fresh every wave (`_run_build_wave`, inside
`_build_impl`'s wave loop) and filtered to `HOISTED`/`MIGRATED` only. Once the FIRST failing
consumer's dispatch wrote `contracts.status = 'FAILED'` (via `_BuildSink`), that filter silently
stopped watching the contract for every LATER-wave consumer of the SAME broken hoist — each of
those would then hit an unattributed, still-`retryable=True` `BUILD_ERROR` and burn its own full
retry ladder, the opposite of what §12.31(ii) needs (every SCC member's `phases.attempts` should
stay unspent, not just the first repo to trip the finding). **Fix:** widened `_hoist_watch_for_
run`'s filter to include `ContractStatus.FAILED`. **Proof:** `tests/test_sequence_e2e.py::
test_a_failed_contracts_watch_survives_into_a_later_wave` — a real `HOISTED` contract from a real
`fleet scan`/`fleet sequence`, marked `FAILED` via the same SQL `_BuildSink` uses, then
`_hoist_watch_for_run` called directly against the resulting database, asserting the contract is
still in the returned watch set. **Old-passes/new-fails verified**: reverting the filter widening
reddens exactly this test (reproduced live, restored after) while the other 11 tests in the same
file stay green.

### Minor

**M1 — fixed.** ADR-0123's own `_graph_edges` citation had inherited `main`'s ADR-0122 number
(`3858-3902`) rather than being re-measured at this task's own branch `HEAD`. Corrected to
`3873-3917` (this task's first-landing state) and then re-corrected again to account for this
fix round's own further edits — see C2 above; the two `_graph_edges` citations (in
`docs/DECISIONS.md` and `docs/INTEGRATION_HONESTY.md`) are not among the 4 that drifted a second
time, since `_graph_edges` sits before both `_hoist_watch_for_run` (I3's edit site) and the C1 SQL
fix in the file.

**M2 — corrected.** The original report claimed `tests/test_workers_build.py` "91 passed."
Freshly re-measured in THIS environment, whole file, no `-k`: **91 passed, 0 skipped** — this
does NOT match the review's cited "88 passed, 3 skipped." `tools/bin/bazel` (bazelisk) is on
`PATH` in this worktree (`tests/conftest.py` wiring), so the `@pytest.mark.skipif(shutil.which(
"bazel") is None, ...)` guards in `test_workers_build.py` do not skip here; the review's "3
skipped" number may reflect a different environment where `bazel` was not resolvable. Reporting
the number I actually measured, not the review's, per this project's "measured is not the same as
reproducible" discipline — re-derive rather than trust an inherited number, in either direction.

**M3 — corrected.** The original report said the e2e test's fake-bazel seam "fakes only the ONE
`bazel build` call that fails." As the review noted, the helper (`_bazel_seam_failing_one_dest`)
actually intercepts and answers EVERY `bazel` call for the dispatch (returning exit 0 for
everything except the one matching `(sub="build", dest=fail_dest)`) — the test itself and its
docstring were always accurate about this; only this report's own prose overstated the seam's
narrowness. Corrected here.

**M5 — corrected.** `docs/CRITERIA_PLAN.md` and `docs/INTEGRATION_HONESTY.md` both said the
matcher was "proven ... against all five REAL quoted bazel error strings," worded to imply the
five real strings exercise the matcher's positive (match-found) path. In fact all five are
NEGATIVE cases (each correctly returns no match, since none of them names an in-tree package a
real `hoist_target_path` would ever equal) — the positive path is exercised only by three
constructed cases built against a real `hoist_target_path` shape. Both documents corrected in
place (dated fix-round notes, not silent rewrites) to say so explicitly.

### Sequencing constraint (informational, per the review — no action taken here)

The review noted this task's own new code is the FIRST code that can trigger the `_graph_edges`/
`build_graph` crash risk `ADR-0122` already documents, and that `task-65` (fixing that filter)
must therefore merge before `task-66`. Confirmed independently during this fix round: running a
real second `fleet scan` after seeding a `FAILED` contract row does crash with exactly that
`GraphError`, reproduced live (see C1's proof section above). No action taken on this point per
the review's own instruction that it is the controller's to enforce, not this task's to fix.

### Full, fresh verification run (whole files, no `-k`, this fix-round commit)

| Check | Result |
|---|---|
| `ruff check .` | All checks passed |
| `python -m mypy` (no path args) | Success: no issues found in 129 source files |
| `tests/test_integration_honesty_citations.py` | 70 passed |
| `tests/test_findings_kinds.py` | 4 passed |
| `tests/test_workers_build.py` | 91 passed |
| `tests/test_workers_contracts.py` | 26 passed |
| `tests/test_sequence_e2e.py` | 12 passed |
| `tests/test_wave_composition_projects_mid_wave.py` | 5 passed |
| `tests/test_event_stream_wiring.py` | 6 passed |
| `tests/test_build_e2e.py` (`-m "not integration"`) | 52 passed, 22 deselected |
| `tests/test_cli.py` | 189 passed |

Total across these 9 test files: **455 passed, 0 failed** at commit `83a6644`. The 22 deselected
in `test_build_e2e.py` are the file's own `@pytest.mark.integration` real-bazel tests, excluded
the same way this file's own module docstring says an offline run excludes them — unrelated to
anything this task touched.

### Concerns carried forward (unchanged in kind from the first landing; C1/I3 are now FIXED, not
merely disclosed)

1. The e2e proof (`tests/test_build_e2e.py::
   test_a_real_build_failure_naming_a_hoisted_contracts_package_is_attributed_and_terminal`) still
   seeds its `contracts` row directly rather than triggering the failure through an organically
   wired `BuildUnit.contract_deps` reference — D113/ADR-0119's own disclosed pre-existing scope
   boundary, not a gap this task introduced or could close within its own scope.
2. The `_graph_edges` crash risk (ADR-0122, `task-65`'s to fix) remains open until `task-65`
   merges — reproduced live this fix round as noted above, not merely theorized.
3. The e2e test's `phases.status == 'REQUIRES_HUMAN_INTERVENTION'` assertion depends on shared
   `RetryPolicy._terminal_status` code this task does not own; the `phases.attempts == 0`
   assertion (the load-bearing one for this task's own claim) does not share that dependency.
4. `schema.sql`'s own comment on the `contracts` table (`state/schema.sql:208-211`, pre-existing,
   not touched) still only names `'HOISTED'`/`'MIGRATED'` as surviving a rebuild — stale before
   this task (Leg E didn't update it either) and left exactly as stale, per Rule 3.

None of these four block the fixes above or contradict any claim this report makes; all four are
disclosed rather than silently left for a future reader to discover.
