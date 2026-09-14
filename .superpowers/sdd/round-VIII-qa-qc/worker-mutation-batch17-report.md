# Worker report: round VIII, Wave 5 Batch 17 — mutation-proof tests for ecosystems/contracts/{avro,base,openapi,proto,shared_lib,thrift}.py

## Status: PARTIAL — base.py plus 2 of 5 adapters covered with new mutation-proof tests; 3 adapters (avro, proto, thrift) reviewed and found already adequately mutation-resistant by pre-existing tests, no changes needed there.

Branch: `agent/roundviii-mutation-batch17` (from `main` at `c53c6f7`)
Worktree: `/home/redmage/swe repo harness worktrees/wt-roundviii-mutation-batch17`
Commit: see bottom of this report (added after this file is written and committed).

## What I did

Read all 6 files (`src/fleet/ecosystems/contracts/{base,avro,openapi,proto,shared_lib,thrift}.py`)
and their 6 existing test files (`tests/test_ecosystems_contracts_{base,avro,openapi,proto,
shared_lib,thrift}.py`), plus `docs/CRITERIA_PLAN.md`'s §47 entry for context on the registry's
statelessness/totality property and history.

**Finding:** the 5 adapter files (avro/openapi/proto/shared_lib/thrift) already carry thorough,
mostly mutation-aware tests from round VI (layout template port, neutral_targets shape, binding
delegation, a reserved-dest "known-bad control" per file, plus disclosed guardrail-6 reasoning
about which mutations are/aren't expressible). `base.py`'s registry MECHANISM tests
(`register`/`for_kind`/duplicate-claim/no-`kind`) were also already covered, and one existing
test (`test_the_real_contracts_registry_is_a_total_bijection_over_contractkind`) proves the real
`discover()` bijection — but **`discover()`'s own core enforcement branch (§12.47's raison
d'être — "every registered instance is stateless", `base.py:161-169`) had ZERO tests anywhere in
this file or its siblings**, and the caching short-circuit (`base.py:137-138`) was also untested.
I prioritized those two `base.py` branches as the most load-bearing gap, then found and closed one
real untested branch each in `shared_lib.py` and `openapi.py`.

### Files touched (tests only — no `src/` changes; all `src/` mutations were applied, verified
discriminating, then restored byte-identical, confirmed via `diff` against a pre-mutation backup
and a passing green re-run)

1. **`tests/test_ecosystems_contracts_base.py`** — 2 new tests:
   - `test_discover_raises_on_a_stateful_registered_instance_naming_it_and_its_attributes` —
     sabotages a REAL registered instance's `__dict__` after a real `discover(force=True)`, then
     calls `discover(force=True)` again and asserts the `RuntimeError` names both the adapter
     (`"proto"`) and the leaked attribute (`"_leaked_state"`). This exercises `base.py:161-169`,
     which had no prior test.
     - Mutation: `if state:` → `if False:` in the statefulness-check loop.
     - `diff` before mutation vs after: 1 line changed (non-empty).
     - Result: **RED** — only this test failed (`DID NOT RAISE RuntimeError`); the other 6 tests
       in the file, including the new short-circuit test, stayed GREEN.
     - Restored `base.py` from backup; `diff` against backup: **empty** (byte-identical); re-ran
       file: **7/7 PASS**.
   - `test_discover_short_circuits_without_rechecking_statelessness_once_already_discovered` —
     after a real `discover(force=True)`, sabotages a live instance's `__dict__`, then calls a
     plain `discover()` (force=False) and asserts it returns the same (sabotaged) instance without
     raising — proving the `_DISCOVERED and not force` cache path (`base.py:137-138`) is real and
     is what protects a re-verification from happening on every call.
     - Mutation: `if _DISCOVERED and not force:` → `if False:` (removes the short-circuit).
     - `diff` before/after: 1 line changed (non-empty).
     - Result: **RED** — only this test failed, propagating the real `RuntimeError` from the
       now-always-executed statefulness check on the sabotaged instance; the other 6 stayed GREEN
       (including the stateful-instance test above, which does its own sabotage/cleanup
       independently and is unaffected).
     - Restored; `diff` against backup: empty; re-ran: **7/7 PASS**.

2. **`tests/test_ecosystems_contracts_shared_lib.py`** — 1 new test:
   - `test_binding_target_falls_back_to_lib_when_identifier_has_no_trailing_segment` — every
     existing fixture identifier has a non-empty trailing segment after the last `.`, so
     `shared_lib.py:58`'s `base_name = contract.identifier.rsplit(".", 1)[-1] or "lib"` fallback
     had never fired in any test. Uses `identifier="acme.common."` (valid per `ContractNode`'s
     `min_length=1`), which makes `rsplit(".", 1)[-1] == ""` (falsy), and asserts the binding
     target name is `"lib_pypi"`.
     - Mutation: removed `or "lib"` from the `base_name` assignment.
     - `diff` before/after: 1 line changed (non-empty).
     - Result: **RED** — target test failed (`'_pypi' == 'lib_pypi'` assertion error); other 4
       tests in the file stayed GREEN.
     - Restored; `diff` against backup: empty; re-ran: **5/5 PASS**.

3. **`tests/test_ecosystems_contracts_openapi.py`** — 1 new test:
   - `test_layout_collapses_runs_of_illegal_characters_into_a_single_dash_and_strips_edges` —
     `openapi.py:37`'s `_DEST_ILLEGAL = re.compile(r"[^a-z0-9]+")` has a `+` quantifier that no
     existing fixture identifier discriminates (its illegal-character runs are never longer than
     1 character at the point they matter). Built a `ContractNode` directly (identifier
     `"__Foo///Bar!!.yaml"` fails `ContractId`'s pattern, so `contract_id` is supplied explicitly
     rather than derived) and asserted `layout()` folds it to
     `contracts/openapi/foo-bar-yaml` — proving multi-character illegal runs collapse to ONE
     dash, and the leading run is stripped by `.strip("-")`.
     - Mutation: `[^a-z0-9]+` → `[^a-z0-9]` (drop the `+`).
     - `diff` before/after: 1 line changed (non-empty).
     - Result: **RED** — target test failed
       (`foo---bar---yaml` != `foo-bar-yaml`, i.e. one dash per illegal character instead of one
       per run); other 4 tests in the file stayed GREEN.
     - Restored; `diff` against backup: empty; re-ran: **5/5 PASS**.

Every mutation above used the backup/edit/diff-verify-nonempty/test/restore/re-verify-identical
recipe from CLAUDE.md Rule 12, confirmed the mutation actually changed the file (non-empty diff)
before trusting the RED result, and confirmed the specific target test was the one that failed
(not a module-wide import/collection failure) while sibling tests in the same file stayed GREEN —
satisfying the "old-passes/new-fails on the same input" discriminator and the "control" guardrail
(sibling tests function as an implicit reflow/no-op control here, since each mutation is a
single-line, narrowly-scoped edit that only the targeted branch's test should observe).

## Files needing follow-up

**avro.py, proto.py, thrift.py — reviewed, no follow-up needed this batch.** These three files
are structurally near-identical (`layout()`/`neutral_targets()`/`binding_target()` all follow the
same dotted-template pattern) and each already has 4 pre-existing tests covering: the dotted
template port, neutral-target shape (rule/package/name/srcs), binding delegation (rule supplied
by caller, deps reference the neutral label), and a reserved-dest known-bad control proving
`is_reserved_dest` is actually called. I looked for an analogous "untested branch" (e.g. an
`or` fallback, a regex quantifier, a collapsing/stripping step) in each and found none — these
three adapters have no such branch; their logic is a straight-line dotted-path join with no
conditional fallback. If a follow-up round wants additional coverage here, the only candidate I
found is a cross-cutting one: a single test in `test_ecosystems_contracts_base.py` that calls
each of the 5 REAL adapters' `layout()`/`neutral_targets()`/`binding_target()` methods multiple
times via the real `discover()` registry and re-asserts `vars(inst) == {}` afterwards, to prove
the runtime (not just construction-time) statelessness guarantee — i.e. that no method call
accidentally caches something onto `self`. I did not build this today to keep the batch to a
verified, mutation-proven subset within the per-task budget; it would be a reasonable Batch 17
follow-up if the controller wants literal per-adapter runtime-statelessness proof rather than the
construction-time proof `discover()` already gives transitively.

## Verification

- `pytest tests/test_ecosystems_contracts_{base,avro,openapi,proto,shared_lib,thrift}.py
  tests/test_new_language_touchpoints_e2e.py` → **33 passed**.
- `ruff check` on the 3 touched test files → all checks passed.
- `mypy` on the 3 touched test files → `test_ecosystems_contracts_base.py` and
  `test_ecosystems_contracts_shared_lib.py` clean; `test_ecosystems_contracts_openapi.py` reports
  one pre-existing `unused-ignore` at (post-edit) line 100 — confirmed via `git stash` that this
  same error exists unchanged on `main` (at line 76, pre-edit) in a `type: ignore[assignment]` on
  an unrelated, untouched line (`mod.is_reserved_dest = lambda dest: True`) — not introduced by
  this batch, not touched by this batch's new test.
- `git diff --stat -- src/fleet/ecosystems/contracts/` → empty (no `src/` changes ship; every
  mutation was applied, verified, then restored byte-identical to the pre-batch original).
- `git status --short` → only the 3 test files modified.

## Per-file results summary

| File | Result |
|---|---|
| `base.py` | 2 new mutation-proof tests added (statefulness-check raise, caching short-circuit) |
| `avro.py` | Reviewed — pre-existing tests already mutation-adequate, no gap found |
| `proto.py` | Reviewed — pre-existing tests already mutation-adequate, no gap found |
| `thrift.py` | Reviewed — pre-existing tests already mutation-adequate, no gap found |
| `openapi.py` | 1 new mutation-proof test added (illegal-char-run collapse + strip) |
| `shared_lib.py` | 1 new mutation-proof test added (`or "lib"` fallback branch) |

Report path: `.superpowers/sdd/round-VIII-qa-qc/worker-mutation-batch17-report.md`
