# Worker report: §15.1 item 3, Wave 6 Batch 23 — spot-check models/enums.py, models/tasks.py

## Status: DONE — both files confirmed soundly covered; no gap found, no test manufactured (Rule 2)

Branch: `agent/roundviii-mutation-batch23` (from `main` at `4f57005`)
Worktree: `/home/redmage/swe repo harness worktrees/wt-roundviii-mutation-batch23`
Commit: see bottom of this report (added after this file is written and committed).

## What I did

Read `src/fleet/models/enums.py` and `src/fleet/models/tasks.py` in full, identified each file's
most load-bearing branch/validator/state-transition logic, then swept `tests/` for genuine
Rule-12-discipline coverage of that logic — not just "the file is imported somewhere."

### `models/enums.py` — load-bearing logic identified

- `transition()` — the single gate for every `RepoStatus` write; the `ALLOWED_TRANSITIONS` guard
  plus the three opt-in doors (`OPERATOR_REOPEN`, `RESUME_DEMOTE`, `STUB_DEGRADE`) gated behind
  `operator=`/`resume=`/`stub_degrade=`.
- `demote()`, `degrade_for_stub()`, `reopen_abandoned()` — the three audited wrapper paths, each
  deliberately stricter than `transition()` (refuses `PENDING`/`RUNNING`/`BLOCKED` reached via
  `ALLOWED_TRANSITIONS` even though `transition()` itself would accept them under the flag).
- The four transition maps themselves (`ALLOWED_TRANSITIONS`, `OPERATOR_REOPEN`, `RESUME_DEMOTE`,
  `STUB_DEGRADE`) and `TERMINAL_STATUSES`.

**Coverage found in `tests/test_state_models.py`:** exhaustive and already at Rule-12 discipline.
`test_every_declared_transition_is_accepted` / `test_no_automatic_path_leaves_a_terminal_status`
parametrize directly over `ALLOWED_TRANSITIONS` and every terminal status. Each of the three doors
has a dedicated pair of tests: one proving the door is closed without its flag
(`test_a_resume_demotion_is_impossible_without_the_resume_flag`,
`test_a_stub_degradation_is_impossible_without_the_stub_degrade_flag`,
`test_abandoned_is_reachable_only_through_the_audited_operator_door`) and one proving it opens
onto exactly one target and nothing else, exhaustively enumerating every other `RepoStatus` as a
refused target (`test_the_resume_door_opens_onto_pending_from_succeeded_and_nothing_else`,
`test_the_stub_degrade_door_opens_onto_degraded_from_succeeded_and_nothing_else`). The three
audited wrappers each have a test proving they are *stricter* than `transition()`
(`test_demote_pairs_the_finding_and_is_stricter_than_transition`,
`test_degrade_for_stub_pairs_the_finding_and_is_stricter_than_transition`,
`test_reopen_abandoned_pairs_the_finding_and_is_stricter_than_transition`), explicitly re-proving
the RUNNING/BLOCKED-reach-PENDING-via-ALLOWED_TRANSITIONS edge case that would make a naive
`transition()`-delegating guard wrong. `transition()`'s own freedom from a silent side-effect sink
is covered by `test_transition_demotes_without_writing_a_record_or_naming_a_new_sink`, using a
`co_names` whitelist (`TRANSITION_GLOBALS`) rather than a blacklist — the CLAUDE.md-documented
"invert an enumeration of escapes into a whitelist" pattern.

**Re-measured per Rule 12 ("re-run a routed finding's mutation against current code"):** rather
than trust the existing docstrings unread, I ran a fresh mutation directly against current
`transition()`:

- Mutation: `if new in ALLOWED_TRANSITIONS[old]:` → `if True:` (the guard this whole suite exists
  to prove is enforced).
- `diff --numstat --no-index` against a pre-mutation backup: 1 line changed (non-empty — the
  mutation actually landed).
- Result: **26 of 127 failed, 101 passed** — discriminating, not a module-wide outage (per
  CLAUDE.md's "implausible result" check: an all-fail would indicate a broken import/module, not
  a caught defect; a partial, targeted failure set naming exactly the terminal/door/no-op tests is
  the expected shape).
- Restored from backup, `diff` against backup: identical (0 bytes difference). Re-ran suite:
  **127 passed** — confirms the restore was clean and the mutation, not incidental drift, caused
  the failures.

**Verdict: `models/enums.py` is soundly covered. No gap, no new test.**

### `models/tasks.py` — load-bearing logic identified

Seven `@model_validator` methods across `BackendTarget`, `TransformTask`, `BuildAttempt`,
`StubRecord`, `VerificationReport`:

- `BackendTarget._free_is_declared_not_derived` — a zero-rate `Price` must be the literal `free`.
- `TransformTask._ladder_matches_attempts` — ladder length == `max_attempts`; attempt 1 must be
  `None` (deterministic).
- `BuildAttempt._revalidation_is_a_verify_round` — `revalidation_round > 0` requires `Phase.VERIFY`.
- `BuildAttempt._executed_iff_command` — `command`/`exit_code` present-or-absent together, and the
  only legal "neither" is `FailureClass.ANCHORED_REPEAT`.
- `StubRecord._fidelity_matches_pin` — `PUBLISHED_ARTIFACT` needs a `pinned_version`;
  `rounds_spent` may not exceed `max_revalidation_rounds`.
- `VerificationReport._derive_equivalence` — the fixed STUB_LIMITED > CLOSURE_SAMPLED > FULL
  precedence, derived rather than caller-supplied, running in `mode="before"` specifically to
  avoid a `validate_assignment=True` re-entrant `RecursionError`.
- `VerificationReport._fidelity_covers_every_stub` — `stub_fidelity` keys must equal
  `verified_against_stubs` exactly.

**Coverage found:** every one of the seven has a discriminating, exact-match test.
`test_llm_backend_openai_compatible.py::test_the_local_profile_prices_a_free_target_as_the_literal_free`
covers `_free_is_declared_not_derived` (both the zero-rate-Price rejection and the omitted-price
rejection). `tests/test_state_models.py` covers the rest:
`test_the_configured_ladder_length_has_a_sanity_rail` /
`test_a_longer_ladder_is_representable_and_must_match_its_task` (ladder validator, both branches);
`test_a_revalidation_round_belongs_to_phase_four` and
`test_an_attempt_that_executed_nothing_must_say_why` (both `BuildAttempt` validators, each branch
of `_executed_iff_command` separately: missing-exit-code-with-a-command, and
nothing-executed-without-ANCHORED_REPEAT); `test_a_published_artifact_stub_must_name_the_version_it_impersonates`
and `test_a_stub_at_its_round_cap_cannot_buy_another_round` (both `StubRecord` branches);
`test_equivalence_is_derived_with_a_fixed_precedence` (parametrized over all three precedence
outcomes, including the stub-wins-over-sampled-closure case) and
`test_deriving_equivalence_does_not_recurse` (the `mode="before"` RecursionError regression); and
`test_a_stub_must_declare_its_fidelity` (`_fidelity_covers_every_stub`).

**Re-measured per Rule 12:** ran a fresh mutation directly against `_derive_equivalence`'s
precedence order — a plausible drift bug (reordering the `if`/`elif` chain so `rdeps_truncated`
is checked before `verified_against_stubs`):

- Mutation swapped the two branches' order (both conditions can be true simultaneously, e.g. a
  stub-limited report over a truncated closure).
- `diff --numstat --no-index` against backup: 3 lines changed (non-empty).
- Result: **1 of 127 failed** (`test_equivalence_is_derived_with_a_fixed_precedence[stub wins
  over a sampled closure-...]`, asserting `CLOSURE_SAMPLED != STUB_LIMITED`) — precisely the one
  parametrized case built to distinguish precedence order, none of the other 126. This is the
  ideal discriminator shape: narrow and targeted, not an implausible all-fail/all-pass.
- Restored from backup, `diff`: identical. Re-ran suite: **127 passed**.

**Verdict: `models/tasks.py` is soundly covered. No gap, no new test.**

## Lint/type checks

- `.venv/bin/python -m mypy` (no path args — full `mypy_path`/`packages` scope per CLAUDE.md §6):
  `Success: no issues found in 132 source files`.
- `.venv/bin/python -m ruff check src/fleet/models/enums.py src/fleet/models/tasks.py`:
  `All checks passed!`

## Files touched

None in `src/` or `tests/` — both mutations were applied, verified discriminating, then restored
byte-identical (confirmed via `diff` against pre-mutation backups and a clean 127/127 re-run
after each restore). Only this report file and the git commit recording it are new.

## Per-file spot-check result

| File | Result |
|---|---|
| `src/fleet/models/enums.py` | Confirmed sound. Re-ran a fresh mutation against `transition()`'s core guard; discriminating (26/127 failed), restored clean. |
| `src/fleet/models/tasks.py` | Confirmed sound. Re-ran a fresh mutation against `_derive_equivalence`'s precedence order; discriminating (1/127 failed, the exact parametrized case built for it), restored clean. |

Do not merge, do not push.
