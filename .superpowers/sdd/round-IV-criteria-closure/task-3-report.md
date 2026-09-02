# Round IV task 3 report — §12.4 `CYCLE_BREAK_PROPOSAL` + `VERSION_CONFLICT_RESOLUTION` golden fixtures

**Status: DONE**

BASE verified: `git rev-parse HEAD` = `4de887cadf570bae4817c0621bb1d67f9a35f3ec` (round III's close),
matching the brief. Worked in
`/tmp/claude-1000/-home-redmage-swe-repo-harness/70748df2-2eda-45cd-8e9a-3fd636467943/scratchpad/roundIV-task3/wt`
on branch `agent/roundiv-task3`, own session's scratchpad path, not shared. Toolchain copied per
ADR-0111 before running anything: `.venv` symlinked from the primary checkout, `tools/bin/{ast-grep,
bazel,gh}` and `tools/go/` copied.

## What was investigated

1. **Schemas re-verified against source** (`src/fleet/llm/schemas.py:258-292`, matching the
   brief's line estimate):
   - `CycleBreakProposal`: `strategy: BreakStrategy` (a `StrEnum`, `src/fleet/models/enums.py:283`,
     4 members `CONTRACT_HOIST`/`EDGE_BREAK`/`ATOMIC_WAVE`/`MANUAL`), `broken_edge_ids:
     tuple[str, ...]` (`default=()`, `max_length=256`), `hoisted_contract_ids: tuple[str, ...]`
     (same), `rationale: Rationale` (the shared `Annotated[str, Field(min_length=1, max_length=600)]`
     alias, no default — the field a missing-required-field mutation would target).
   - `VersionConflictResolution`: `coord_key: str` (`min_length=1, max_length=512`),
     `proposed_version: str` (`min_length=1, max_length=128`), `mechanism: str` with
     `pattern=r"^(bazel_dep|single_version_override)$"` (a closed two-value enumeration expressed
     as a pattern-constrained string, not an actual enum type), `violated_specs: tuple[str, ...]`
     (`default=(), max_length=64`), `rationale: Rationale`.
   - Confirmed directly in the running interpreter (not just read) that both schemas validate the
     fixture-shaped payloads and that an invalid `mechanism` value genuinely raises
     `pydantic.ValidationError` citing `mechanism` and `string_pattern_mismatch` — not merely
     schema-shape-valid. See "Rule 12" below for the full discriminator.

2. **Backend envelope shapes confirmed role-agnostic**, including enum serialization. Grepped
   `_TOOL_NAME`/`parse_reply`/`build_request`/`build_body` across all four backend modules
   (`src/fleet/llm/backends/{anthropic,bedrock,vertex,openai_compatible}.py`): every one treats
   `_TOOL_NAME = "emit_response"` as a role-agnostic constant and never introspects the `input`/
   tool-call-arguments payload's internal shape before handing it to `client_module._validate`.
   `strategy` (a `BreakStrategy` `StrEnum`) is written into every fixture as a bare JSON string
   (e.g. `"CONTRACT_HOIST"`) exactly the way `RepoClassification.ecosystem` (an `Ecosystem`
   `StrEnum`) already is in the existing `REPO_CLASSIFY` fixtures — confirmed in the running
   interpreter that `CycleBreakProposal.model_validate({"strategy": "CONTRACT_HOIST", ...})`
   coerces the bare string into `BreakStrategy.CONTRACT_HOIST` with no special-casing needed on
   any backend's parse path, TOOL_CALL or PROMPTED.

3. **Already-adjudicated valid payloads** found at `tests/test_llm_roles.py::_sample()` lines
   217-223 and used as the semantic starting point for each fixture's `input`/JSON-text payload
   (values then varied per backend the way `REPO_CLASSIFY`/`PR_TITLE`/`PR_BODY` already do, so no
   two of the 4 fixtures per role are byte-identical).

## What was built

8 new fixtures in `tests/fixtures/llm/golden_responses/`, each carrying a `_provenance` key
documenting the adapter code / vendor doc it matches:

- `cycle_break_proposal_{anthropic,bedrock,vertex}_tool_call.json`,
  `cycle_break_proposal_openai_compatible_prompted.json`
- `conflict_resolution_{anthropic,bedrock,vertex}_tool_call.json`,
  `conflict_resolution_openai_compatible_prompted.json`

8 new `GoldenCase` rows added to `tests/test_llm_golden_responses.py`'s `GOLDEN_CASES` table
(anthropic/bedrock/vertex at `TOOL_CALL`, openai_compatible at `PROMPTED`, matching the
established per-role rung split), plus the two `RESPONSE_SCHEMAS[...] is ...` pin assertions and
the `CycleBreakProposal`/`VersionConflictResolution`/`BreakStrategy` imports. Every fixture is
parsed through the real backend `parse_reply`/`_reply_from` and validated through the real
`client_module._validate` — no mock of parsing or validation.

## Rule 12 — genuine discriminator

Added a mutation pair targeting `VersionConflictResolution.mechanism`'s `pattern=` constraint
specifically (per the brief's steer, since this is a different failure mode than a missing-field
mutation):

- `test_a_conflict_resolution_reply_with_an_invalid_mechanism_fails_schema_validation` — mutates
  the bedrock `conflict_resolution` fixture's `mechanism` from `"single_version_override"` to
  `"override_all"`, a value that is schema-shape-valid on every OTHER constraint (non-empty `str`,
  present) and would pass a shape-only check, but fails the closed pattern. Confirmed live in the
  interpreter before writing the test: `VersionConflictResolution.model_validate({...,
  "mechanism": "override_all", ...})` raises `ValidationError` naming `mechanism` and
  `string_pattern_mismatch` specifically — not `missing` or any other error type.
- `test_the_unmutated_conflict_resolution_fixture_still_validates_after_the_mutation` — same
  fixture unmutated, must still validate (old-passes/new-fails discriminator; the control half).

Both run in the actual test suite (see pass count below), not just the standalone interpreter
check.

## Self-gate results

- `.venv/bin/ruff check .` — **all checks passed** (whole tree; two `E501` violations found and
  fixed during this task — a too-long docstring line and a too-long parenthesized-return def
  signature on the new mutation-control test — both in `tests/test_llm_golden_responses.py`).
- `.venv/bin/ruff format --check .` on the **whole tree** — reports "123 files would be
  reformatted, 177 files already formatted." **This is the expected, pinned result, not a
  regression**: `tests/test_lint_gate.py`'s `_RUFF_FORMAT_DIRTY_BASELINE = 123` pins this exact
  count as a deliberate baseline (not a clean-format gate — reformatting is out of scope per
  `docs/CRITERIA_PLAN.md` §2's done bar), and I confirmed both derivations agree at 123 and that
  `tests/test_llm_golden_responses.py` and all 8 new fixture files do **not** appear in the dirty
  set — i.e. this task added zero new drift. Ran the actual pinned test to confirm mechanically
  rather than trusting my own read of the raw count:
  `tests/test_lint_gate.py::test_ruff_format_check_dirty_count_matches_the_pinned_baseline` —
  **1 passed**. (This is exactly the check the brief's own warning about round III's regression —
  "main went red from a targeted-only self-gate missing exactly this check" — points at: round
  III's regression was a NEW file going dirty, taking the pin from 123 to 124; this task's whole-
  tree run confirms the count is still 123, unchanged.)
- `tests/test_llm_golden_responses.py`, whole file, no `-k`: **26 passed** — 16 pre-existing items
  (12 `GoldenCase` rows: 4 `REPO_CLASSIFY` + 4 `PR_TITLE` + 4 `PR_BODY`, plus 4 mutation-pair
  tests: `REPO_CLASSIFY`'s and `PR_BODY`'s missing-required-field pairs) + 10 new items (8
  `GoldenCase` rows: 4 `CYCLE_BREAK_PROPOSAL` + 4 `CONFLICT_RESOLUTION`, plus the 2 new
  `CONFLICT_RESOLUTION` mutation-pair tests) = 26, matching `pytest`'s own collected-and-run count
  exactly.
- `.venv/bin/python -m mypy` with no path arguments: **Success: no issues found in 115 source
  files.**

## Roles closed

Both roles assigned to this task are closed with 4 fixtures each (anthropic/bedrock/vertex at
TOOL_CALL, openai_compatible at PROMPTED), parsed through real backend code and validated against
their declared schema:

- `CYCLE_BREAK_PROPOSAL` (`Role.CYCLE_BREAK_PROPOSAL` → `CycleBreakProposal`)
- `CONFLICT_RESOLUTION` (`Role.CONFLICT_RESOLUTION` → `VersionConflictResolution`)

No shape had to be reported `DONE_WITH_CONCERNS` — the backend envelope investigation in step 2
above found nothing role-specific to work around for either role.

## What was NOT done (by design, per brief)

- Did not touch any role besides these 2.
- Did not touch `docs/CRITERIA_PLAN.md`.
- Did not flip §12.4 DONE — this task's own progress alone does not meet the revised Done bar (12
  roles required); see running count below.

## Concerns

- None outstanding. The one thing that looked like a concern at first (`ruff format --check .`
  reporting 123 dirty files) turned out to be the expected, already-adjudicated pinned baseline,
  confirmed both by reading `tests/test_lint_gate.py`'s own docstring/pin and by running its test.
- Task 2 (sibling, `LLM_BUILD_DIAGNOSIS`/`DEP_DISAMBIGUATE`) was not assumed to have landed and
  was not checked for; this task's `git diff --stat` against BASE touches only
  `tests/test_llm_golden_responses.py` (modified) and the 8 new fixture files (added) — no table
  rows or other content belonging to task 2 are present, so a controller merge should see a clean
  table-append, not a conflict, per the brief's own prediction.

## Running §12.4 count (this task's own half only)

**5 of 12 roles** covered with golden-response fixtures: `REPO_CLASSIFY` (round II), `PR_TITLE` +
`PR_BODY` (round III task 3), `CYCLE_BREAK_PROPOSAL` + `CONFLICT_RESOLUTION` (this task, round IV
task 3). 7 remain uncovered from this task's own vantage point — if the sibling round IV task 2
also lands (`LLM_BUILD_DIAGNOSIS`/`DEP_DISAMBIGUATE`), that would bring the combined count to 7 of
12, but that is not assumed or claimed here. §12.4 stays OPEN either way (Done bar requires all
12); not flipped by this task, as directed.

## Commits

1 commit on `agent/roundiv-task3`, not yet merged to `main` (that is the controller's job per this
project's SDD protocol) — see the branch for the exact SHA at merge time.
