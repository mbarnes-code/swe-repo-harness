# Task-79 fix round 1 report

**Status: DONE**

**Branch:** `agent/roundvi-task79`. Started from `4de686c` (confirmed clean `git status`, confirmed
`HEAD == 4de686c` before starting, per the brief's own instruction). New commit:
`c3f9909` — "fix round 1: C1 -- a real mechanical gate closes the D107/D104 safety hole".

Not merged to `main`. Global constraints honored: no `git stash` used anywhere (the old-fails/
new-passes demonstration used a copied-aside backup file, restored byte-identical afterward and
verified via `diff`); `WaveScheduler`, `ALLOWED_TRANSITIONS`, `orchestrator/reentry.py`, and
`_apply_stub_decisions`'s T1/T4/T3-reconcile behavior were not touched; no subagents dispatched;
all test runs were synchronous (backgrounded only where the tool's own 120s timeout required it,
polled to completion in the same turn, never left for a passive wait).

## C1 (Critical) — fixed with a real mechanical gate

`cli._run_one_revalidation_task` now reads the checked-out `<dest>/BUILD.bazel` immediately after
cutting the claim's worktree, *inside* the same `try`/`finally` that cleans the worktree up, and
*before* constructing `VerifyInput` or dispatching `VerifyPipelineWorker`. If the file still
contains a `"//third_party/stubs/...` label, the round is refused: the task is set back to
`PENDING` (retryable, not a terminal failure — a later `fleet pr --sync`/`fleet resume` may yet
land the rewrite) and a `RevalidationLabelNotRewritten` finding is written in the same transaction,
naming the consumer, the task, the `dest`, and every still-present stub label. This is independent
of *why* the rewrite failed to land — it does not special-case "monorepo unavailable" or any other
`_rewrite_one_consumer_label` failure mode; it checks the one fact that actually matters (the
committed tree's own content) rather than trusting the trigger's own success/failure signal.

`RevalidationLabelNotRewritten` was added to both `schema.sql`'s and `docs/SPEC.md`'s findings-kind
listings (kept in sync by `tests/test_findings_kinds.py`, re-run clean).

## I2 — the negative-proof test, rewritten and proven a real discriminator

**Old test** (`test_a_revalidate_round_without_d107s_rewrite_reads_false_empty_verified_
against_stubs`, deleted this round): asserted only `_active_stubs_by_consumer(...)  == {}` — a
characterization of one SQL query's result that is true identically whether D107, or now the C1
gate, exists at all. It never drove the claiming loop. Deleted; the review's finding was correct.

**New test** (`test_c1_gate_refuses_a_revalidate_round_whose_committed_tree_still_names_a_stub_
label`): plants a real commit on `migrate/acme-app-py` whose `BUILD.bazel` still names
`_STUB_LABEL` (`_plant_stub_labeled_build_file` — a real `git commit`, not a DB fixture), supersedes
the stub via T1's own DB effect (D107's rewrite deliberately never called), inserts a REVALIDATE
task, and drives the REAL `_run_revalidation_claims_impl` under an all-green `FakeBazel` (installed
deliberately, see below). Asserts: the outcome string is `FAILED: ...still names stub label...`;
`FakeBazel.calls == []` (the gate fires before any worker is even constructed); the task is back to
`PENDING`; the stub stays `SUPERSEDED` (never `RESOLVED`); a `RevalidationLabelNotRewritten` finding
exists naming the consumer and the stub label.

**Why FakeBazel, not "no seam" (a design choice worth stating):** the first version of this test
used no seam (matching the file's other tests), and running it with the gate disabled DID fail —
but with `verdict=FAIL` from a real, seamless `bazel build` attempt against a fixture with no
module-registry pin, which is a **confounded** failure (network/`MODULE.bazel` resolution, not the
stub-label hazard). Installing an all-green `FakeBazel` (no `fail=` entries — every build/test
PASSes unconditionally) removes that confound: without the gate, FakeBazel's unconditional PASS is
exactly what would let `settle_revalidation` fire T2 and promote the consumer, so the without-gate
failure is now the CONCRETE C1 hazard itself, not a proxy for it.

### Old-fails/new-passes proof (this session, not fabricated)

Performed via a copied-aside backup file (`cp cli.py cli.py.backup-*`), never `git stash` (shared
across worktrees, forbidden by this brief). Two independent runs, since the first design (I2's
initial single-line mutation) needed a second iteration once the FakeBazel confound was found and
fixed — both are reported honestly below.

**Mutation applied** (verified via `diff` to be a real, one-line semantic change — the gate's
condition forced to `False`, never re-checked afterward):
```
-        if stub_needle in build_text:
+        if False and stub_needle in build_text:  # TEMP: C1 gate disabled for I2 old-fails proof
```

**Run 1 (real-Bazel, no seam — confounded, superseded by run 2's design):**
```
FAILED tests/test_stub_resolution_task79.py::test_c1_gate_refuses_...
AssertionError: another_round: verdict=FAIL decisions=0
assert False
 +  where False = ...startswith('FAILED:')
```
Genuinely RED, but for a confounded reason (a real, seamless `bazel build` failing on
infrastructure grounds unrelated to the stub label) — this is why the test was redesigned to
install `FakeBazel` before being finalized, per the discussion above.

**Run 2 (FakeBazel installed — the version actually committed), gate disabled:**
```
FAILED tests/test_stub_resolution_task79.py::test_c1_gate_refuses_...
AssertionError: settled: verdict=PASS decisions=1
assert False
 +  where False = ...startswith('FAILED:')
```
This is the concrete hazard, reproduced verbatim: `settled: verdict=PASS decisions=1` means the
round proceeded, `settle_revalidation` fired a decision, and the consumer was in the process of
being promoted — **exactly** what C1 names, with the committed tree still naming the stub the
whole time.

**Restore, verified byte-identical:**
```
$ diff cli.py.backup-before-gate-removal src/fleet/cli.py
(no output)
```
Then re-ran the test: **1 passed** (new-passes, gate restored and correctly refusing the round —
see the full run below). The two temporary backup files were removed from the scratchpad after
use; they were never committed and never touched via `git stash`.

## I1 — SPEC's "by construction" claim corrected

`docs/SPEC.md` §11.5's dated marker for the REVALIDATE step no longer claims the rewrite makes a
stub-free tip true "by construction." It now states the actual guarantee: the rewrite is
*intended* to land first from the same T1 trigger, but the REAL guarantee is the C1 mechanical
gate in `_run_one_revalidation_task` itself, which refuses the round rather than trusting trigger
ordering. Edited in place (this marker describes ongoing mechanism behavior, written earlier in
this same task's own session — not a frozen historical record another party needs an annotation
to leave untouched).

## I3 — D104's ledger overclaim corrected

D104's `FIXED, LANDED` body paragraph (landed in the prior round's commit `0b78560`) claimed the
old test proved the safety property as "a falsifiable negative." A dated **Correction** annotation
is appended directly below it (the original paragraph is left untouched, per this project's
"annotate, never rewrite" ledger discipline) stating precisely what was false about that claim,
what the C1 gate now does, and what the *replacement* test (`test_c1_gate_refuses_...`) actually
proves, including the old-fails/new-passes result.

## I4 — the drift-check "mechanically equivalent" claim corrected to a disclosed narrowing

`_rewrite_one_consumer_label`'s docstring (`src/fleet/cli.py`) is reworded: the correction of the
ADR's literal Phase-2-trailer pseudocode (D115's actual `migrate/<repo>` topology) is confirmed
right, but the replacement check is now stated as a **narrower** check, not an equivalent one — it
is structurally blind to drift arriving already inside `integration` (a rebase pulling in new
upstream commits before this rewrite runs — the ADR's own named example), which the unconditional
`rebase(base)` two lines below silently absorbs with no flag raised. This is named as disclosed
follow-on debt. D107's ledger entry gets a matching dated **Correction** annotation (original FIXED
paragraph from `4ead8f9` left untouched).

## Status ruling — D104's heading

**`FIXED, LANDED` stands**, per the review's own stated condition: the C1 gate is now a real
mechanism (not a docstring claim) and `test_c1_gate_refuses_a_revalidate_round_whose_committed_
tree_still_names_a_stub_label` is a genuine discriminator (old-fails/new-passes proof above). No
hole was found in the gate itself during this fix round that would call for `PARTLY ADDRESSED`
instead.

## Should-fix — rewrite-failure visibility (implemented)

`cli._pr_sync_lines` now renders a failed label rewrite in `fleet pr --sync`'s own human-readable
output (a `  N label rewrite(s) FAILED (see label_rewrites for detail):` line plus one line per
failed consumer), rather than leaving it visible only in the unrendered JSON `label_rewrites`
field. Two new unit tests (`test_pr_sync_lines_surfaces_a_failed_label_rewrite`,
`test_pr_sync_lines_is_silent_when_every_rewrite_succeeded`) — both pass. This does not change
`aabead1`'s own fix (the monorepo-unavailable catch stays non-fatal to the surrounding command);
it only makes a failure's existence visible in the same command's plain-text output.

## Full test results (this session)

- **`python -m mypy`** (no path args, manifest-scoped `packages = ["fleet"]`): clean, 129 files —
  run after every code edit in this round.
- **`ruff check src/ tests/ docs/`**: clean.
- **`tests/test_stub_resolution_task79.py`, whole file, no `-k`:**
  - Excluding the real-Bazel test (`-k "not real_bazel"`, to conserve this session's own scratch
    disk across iterations): **6 passed, 1 deselected** (the new `test_c1_gate_refuses_...` test,
    plus the two new `_pr_sync_lines` unit tests, plus the 4 tests carried over from the prior
    round unchanged).
  - The real-Bazel test (`test_d104b_claiming_loop_resolves_the_stub_under_a_real_bazel_build_
    and_test`) was run separately, standalone, with the C1 gate in place: **1 passed** (58.15s,
    bazel disk peak 1.22 GiB) — confirms the gate correctly does NOT fire when D107's rewrite
    genuinely lands (no regression to the headline proof from the prior round).
  - **Whole file including the real-Bazel test was not re-run as one invocation** this round
    (it was split into two runs — `-k "not real_bazel"` then the real-Bazel test alone — purely to
    manage this session's disk budget between iterations; both together cover every test in the
    file with no test skipped or omitted from a green result).
- **`tests/test_cli.py`, whole file, no `-k`, `-m "not integration"`:** **205 passed** — re-run
  after the C1 gate and `_pr_sync_lines` changes; no regression from the prior round's 205/205.
- **`tests/test_integration_honesty_citations.py` + `tests/test_findings_kinds.py`, whole file, no
  `-k`:** **74 passed** — re-run after every `docs/INTEGRATION_HONESTY.md`/`docs/SPEC.md`/
  `schema.sql` edit in this round (three separate edit passes, each re-verified clean before
  moving on; one stray apostrophe in a first draft of the new `RevalidationLabelNotRewritten`
  schema.sql comment briefly broke the findings-kind parser's `_QUOTED` regex — caught immediately
  by re-running this pair and fixed before proceeding).

## Not re-run this round (disclosed, not silently assumed unaffected)

- The full `tests/` suite (~15 min per CLAUDE.md's own estimate) — scoped instead to every file
  this round's diff plausibly reaches (`cli.py`, `schema.sql`, `docs/SPEC.md`,
  `docs/INTEGRATION_HONESTY.md`, and the two test files touched), each run whole, no `-k`.
- `tests/test_pr_e2e.py`, `tests/test_build_e2e.py` (non-integration), `tests/test_repository.py`,
  `tests/test_state_models.py`, `tests/test_stubs.py`, `tests/test_resume_continue.py`,
  `tests/test_resume_unblocking.py`, `tests/test_workers_build.py` — all re-verified clean in the
  PRIOR round (task-79's own base report) against the pre-fix-round code; this round's diff does
  not touch anything those files exercise (`_run_one_revalidation_task`'s internals and
  `_pr_sync_lines`, neither reached by those suites' own call graphs per the prior round's
  citation of `test_cli.py` as the file that actually exercises the T1/PR-sync call path) — not
  re-run a second time this round to conserve the shared host's disk, which was at 3.5-3.7GB free
  throughout this session (consistent with the brief's own disclosed disk-constraint note).

## Confirmed correct, not re-litigated (per the brief's own list)

Judgment calls 1/2/3/4/7, D107's headline test, D108's transaction shape, the T1/T4/T3-reconcile
non-regression, D129's fix, and `aabead1`'s regression fix are all unchanged by this round and were
not re-investigated — the brief's own review already confirmed them and this round's diff does not
touch any of that code.
