# Task 111 report — §12.11 Leg C: the red path (`BaselineRed`, `SKIPPED`, `baseline_ok = 0`)

Round VI, thirty-ninth wave. Branch `agent/roundvi-task111`, off `main` at `a01e004`. Worktree:
`/tmp/claude-1000/-home-redmage-swe-repo-harness/168ce0b2-2e48-46a6-837c-4cb29f1a8475/scratchpad/task111/wt`.

## What was built

**`src/fleet/cli.py`**, two additions, both surgical:

1. `_ScanEvidence.baseline_red: dict[str, BaselineOutput]` — populated in `.record()` whenever
   `output.baseline is not None and not output.baseline.baseline_ok` (a genuine native build/test
   failure — never the existing `NativeBaseline is None` green-path skip Leg B already owns, which
   leaves `output.baseline` `None` and never reaches this branch at all).
2. `_gate_baseline_red(writer, run_id, evidence, now)` — a new function placed immediately after
   `_gate_empty_repos` (its own precedent, matched structurally on purpose) and called right after
   it in `_scan_impl`'s main body. For every repo in `evidence.baseline_red`, in ONE transaction:
   `UPDATE phases SET status = 'SKIPPED' WHERE run_id=? AND repo_id=? AND phase=1 AND status <>
   'SKIPPED'` and `INSERT INTO findings (...) VALUES (?, ?, 'BaselineRed', 'error', ...)`. The scan
   summary's `"skipped"` list is extended to include these repos too (`set(gated) |
   set(baseline_gated)`).

**Why this shape, and why it satisfies the brief's atomicity requirement.** `repos.baseline_ok =
0` is already durable from `_scan_rows`'s own earlier, per-repo transaction (Leg B, unchanged) —
and `output.baseline.baseline_ok` being false is exactly what routed this repo into
`evidence.baseline_red` in the first place, so the two facts can never disagree. The phases-status
UPDATE and the `BaselineRed` finding INSERT are written together in `_gate_baseline_red`'s own
single `unit`/transaction, so by the time it commits all three facts `graph/sequence.py::
_exemptions_for` conjoins are durable together. A crash strictly between `_scan_rows`'s commit and
this one leaves only `baseline_ok = 0` durable and neither `SKIPPED` nor the finding — the
identical, already-accepted window `_gate_empty_repos`/`repos.preflight_ok` lives with for the
structurally identical reason (documented in `_gate_empty_repos`'s own docstring: "this is a
driver write, and it is the one place the driver writes a status" — there is no legal §6 edge out
of `SUCCEEDED`). A re-scan re-measures and re-gates idempotently; nothing is silently lost.

Severity is `'error'` (matching `HoistBrokeOwner`'s convention for a genuine build/test failure,
distinct from `EmptyRepo`'s `'warn'` for a repo that did nothing wrong).

**`tests/test_baseline_scan_e2e.py`** — one new test,
`test_a_genuinely_broken_native_baseline_is_skipped_with_a_baseline_red_finding`, reusing this
file's OWN existing two-repo `baseline_fleet` fixture (`acme-baseline-green`/`acme-baseline-red`,
real, offline, no-docker-needed via `container_image: null`, Leg B's own convention) — see "A
significant discovered regression" below for why this fixture was chosen over
`tests/test_scan_e2e.py::FIXTURE_REPOS`. A real `fleet scan` then asserts, through real DB
read-back:

1. `repos.baseline_ok` — `0` for the red repo, `1` for the green one (re-asserted for
   self-containedness; already proven by the sibling test above it).
2. `phases.status` — `SKIPPED` for the red repo, not `SKIPPED` for the green one.
3. `findings` — exactly one row, `('acme-baseline-red', 'BaselineRed', 'error')`, and nothing else
   for either repo (this is also the "no repo matches two exemptions" proof for this fleet).
4. The **consumer** side, through a real `fleet sequence` invocation (not a synthetic/mocked
   `_exemptions_for` call — `tests/test_graph_sequence.py` already proves that pure-function
   contract with hand-typed facts): `cli.py::_phase1_exit_report` → `graph.sequence.
   check_criteria` raises `SequenceCriterionError` (exit 6) the instant any repo absent from
   `wave_members` fails to match exactly one of §3.1(c)'s five exemptions. A clean `exit_code ==
   ExitCode.SUCCESS` here is only possible because `_exemptions_for` genuinely recognized
   `acme-baseline-red` under `baseline-red` and matched it exactly once. Also asserts the JSON
   payload directly: `acme-baseline-red` in `excluded`, absent from `wave_index_by_repo`;
   `acme-baseline-green` present in `wave_index_by_repo`.

## Rule 12 (mutation-testing) proof

Backed up `src/fleet/cli.py`. Mutated `_gate_baseline_red`'s findings-INSERT `executemany` call:
`for repo_id, out in rows` → `for repo_id, out in ()  # MUTATION`, writing only 2 of the 3 facts
(phases.status still flips to `SKIPPED`, but no `BaselineRed` finding is ever written). Confirmed
via `diff` against the backup that the mutation genuinely changed the file (Guardrail 6's
zero-change gate) before trusting any result.

Re-ran `tests/test_baseline_scan_e2e.py`: **the new test reddened specifically** — assertion 3
(the finding-row check) failed with `red_findings == []` instead of the expected row, and
assertion 4 would have failed too (not reached: `pytest` stops at the first failed `assert`) since
`_exemptions_for`'s `finding("BaselineRed")` sub-condition would now be false, making
`check_criterion_c` report a problem and `fleet sequence` raise `SequenceCriterionError`. The
other 3 tests in the same file stayed green (**1 failed, 3 passed** — not a module-wide outage,
confirming the mutation discriminates the write path specifically rather than breaking the module
wholesale). Reverted from the backup; `diff -q` against it reported identical files before
re-committing. Final `git diff --stat src/` after revert-and-redo: only the intended two lines in
`_ScanEvidence`/`_gate_baseline_red` plus the call-site wiring, matching what's described above.

## Verification

`ruff check`/`ruff format --check` on both changed files: clean. `mypy` (whole-manifest, no path
args): clean, 131 source files. Covering set, whole files, no `-k`: `tests/
test_baseline_scan_e2e.py` (4/4), `tests/test_workers_baseline.py` (18/18), `tests/
test_graph_sequence.py` (unaffected — pure-function unit tests of `_exemptions_for`/
`check_criteria`, already covering the `BaselineRed` exemption category with synthetic facts;
39/39). Combined: **96/96** unaffected + **4/4** new/updated file = all green.

Pre-existing, unrelated `ruff format --check` drift in `src/fleet/cli.py` (three unrelated blocks
around lines 19292/19524/19791 on `main`) reproduces identically on bare `main`@`a01e004` before
this task's own edit — confirmed by running `ruff format --check` against the primary checkout
directly. Not touched (Rule 3).

## A significant discovered regression — measured, disclosed, NOT fixed here

**Finding.** `tests/test_scan_e2e.py::FIXTURE_REPOS` and `tests/test_transform_e2e.py` (which
reuses it) do not override `preflight.baseline_build` in their shared `FLEET_YAML`, so they run
under the shipped pydantic default: `enabled=True`, `container_image="fleet-baseline:py3.11-node18"`
— the REAL image round VI task 108 (Leg D) built and landed as the default (after this task's own
brief was written; Leg D's own fix round changed the default from an unbuilt placeholder to a
real, built image). Measured directly, before writing any fixture: running the existing
`FIXTURE_REPOS` fleet through a real `fleet scan` produces genuine native-baseline **failures**
for 3 of its 4 non-empty repos —

- `acme-app-py`: `pip install -e .` fails (`build_exit_code=1`) — its `pyproject.toml` declares
  `dependencies = ["acme-lib-py>=2.0", "requests>=2.31"]`, and `acme-lib-py` is a fictional,
  never-published name (deliberately internal-only, so `test_transform_e2e.py`'s cross-repo edge
  inference has something to detect) that cannot resolve against the real PyPI.
- `acme-app-ts`: `npm install` fails (`build_exit_code=1`) for the identical reason
  (`@acme/lib`, an unpublished internal-only npm name).
- `acme-lib-ts`: build succeeds, `npm test` fails (`test_exit_code=1`) — its `package.json`
  declares no `"test"` script at all, and `workers/baseline.py`'s own module docstring already
  names this exact gap as a DISCLOSED, out-of-scope limit: an absent npm test script is
  misclassified as a real failure rather than "no native tests" (pytest's analogous exit-5 case
  has no npm equivalent wired), explicitly left to "Leg C/E... to refine further" — which this
  task does not attempt, since fixing it would only remove 1 of these 3 failures, not the other 2
  (which are a genuinely unresolvable dependency name, an unrelated cause).
- Only `acme-lib-py` (no external deps, no test files) genuinely passes (`baseline_ok=1`, via
  pytest's exit-5 "no tests collected" case).

**Consequence, once this task's gate lands.** Wiring `BaselineRed`/`SKIPPED` onto this shared,
5-repo fixture fleet (rather than a fixture built specifically for this proof) flips 3 of those 5
repos out of the wave plan. Measured directly by running the full covering files with this task's
own production change applied:

- `tests/test_scan_e2e.py`: **4 of 33 fail** —
  `test_an_empty_repo_is_skipped_with_a_finding_and_the_fleet_continues`,
  `test_a_library_is_sequenced_before_the_app_that_depends_on_it`,
  `test_an_interrupted_scan_resumes_without_losing_completed_work`,
  `test_the_run_projects_a_migration_state_that_round_trips` — all asserting a fixture repo's
  `phases.status`/`MigrationState.repos[...].status` is `SUCCEEDED` where it is now (correctly, per
  this task's own new logic) `SKIPPED`.
- `tests/test_transform_e2e.py`: **14 of 20 fail** — every test that asserts all four
  non-empty `DESTINATIONS` repos participate in the transform plan/wave, or asserts a
  cross-repo edge/dependent relationship between `acme-app-*`/`acme-lib-*`, now sees only
  `acme-lib-py` (the sole repo that genuinely passes its native baseline).
- `tests/test_baseline_ok_exclusion.py`: **unaffected** (1 passed, 1 xfailed — identical to
  before this task's change) — its own two assertions only read the `baseline_ok IS NULL` set,
  which is insensitive to `phases.status`, so it happens not to collide.
- `tests/test_workers_baseline.py`, `tests/test_graph_sequence.py`,
  `tests/test_config_keys_are_read.py`: unaffected (96/96), as expected — none of them touch this
  shared fixture fleet or `cli.py`'s scan-time gating.

**Why this is not fixed in this task, and why it is reported rather than silently patched
around.** The correct fix touches shared fixture infrastructure this task's brief did not scope
(`tests/test_scan_e2e.py`/`tests/test_transform_e2e.py`'s `FIXTURE_REPOS`/`FLEET_YAML`, both reused
by several other suites including some outside the two named above), and the "right" shape is a
genuine judgment call with more than one defensible answer — e.g. (a) give `test_scan_e2e.py` its
own `FLEET_YAML` override disabling `baseline_build` (it never asserts on baseline behavior at
all), while leaving `test_transform_e2e.py`'s alone if narrower analysis shows its own 14 failures
have a different root fix; or (b) something the controller prefers. Weakening this task's own
production logic to avoid tripping on `FIXTURE_REPOS` would be gaming the mechanism rather than
fixing the actual conflict (CLAUDE.md Guardrail 6: "a fake mechanism is worse than an honest
disclosure"), and is not what was done. This task's own new test avoids the collision entirely by
reusing `test_baseline_scan_e2e.py`'s own isolated two-repo `baseline_fleet` (documented in the new
test's own docstring), which is not reused by any other suite and cannot collide with any other
§3.1(c) exemption category by construction (only two repos, neither empty/config-skipped/
quarantined/cyclic).

## Scope discipline

Did not touch: Leg E (D116's `strict=True` xfail, `test_baseline_ok_exclusion.py`), §12.39/B2, or
the Leg D container image (`docker/fleet-baseline.Dockerfile`, `settings.py`'s `BaselineBuild`) —
no genuine defect was found in the image itself; the regression above is entirely about the
SHARED FIXTURE FLEET's manifest content interacting with the (correct, already-merged) real image
default. `git diff --stat` (against staged files): `src/fleet/cli.py` (+89/-1),
`tests/test_baseline_scan_e2e.py` (+89) — nothing else.

## Status

**DONE_WITH_CONCERNS.**

Commit range: `agent/roundvi-task111` = one commit on `main`@`a01e004`.

Test summary: `tests/test_baseline_scan_e2e.py` 4/4 (including the new red-path test);
`tests/test_workers_baseline.py` 18/18; `tests/test_graph_sequence.py` unaffected; combined
unaffected covering set 96/96; ruff clean; mypy clean (131 files); Rule 12 mutation proof
confirmed (1 test reddened specifically, 3 stayed green, mutation verified as a genuine code
change via diff-against-backup).

**Concerns:**

1. **The significant, measured regression above** (18 total test failures across
   `tests/test_scan_e2e.py`/`tests/test_transform_e2e.py`) is the main thing the controller needs
   to decide on before this lands — it is a genuine, disclosed side effect of correctly closing
   §12.11 Leg C against a shared fixture fleet that was never validated end-to-end against a real
   native-baseline container (Leg D landed after this fixture fleet, and after Leg B's own report
   was written against the placeholder-image interim state). Recommend: give
   `tests/test_scan_e2e.py`'s own `FLEET_YAML` an explicit `baseline_build: enabled: false`
   override (it never asserts on baseline behavior), and separately assess
   `tests/test_transform_e2e.py`'s 14 failures the same way — but this is the controller's call,
   not decided unilaterally here, since it touches infrastructure several other tasks/tests share.
2. `workers/baseline.py`'s own disclosed npm "missing test script" misclassification (named as
   Leg C/E territory) is NOT fixed here — it would only close 1 of the 3 root causes behind
   concern 1, not the other 2 (genuinely-unresolvable dependency names), so fixing it alone would
   not resolve the regression above.
3. Pre-existing, unrelated `ruff format` drift in `src/fleet/cli.py` (three blocks, confirmed
   identical on bare `main` before this task) — not touched, per Rule 3.
