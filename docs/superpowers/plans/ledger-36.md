# SDD ledger — plan: docs/superpowers/plans/queue-36.md

Base: `68a41ff` (main). Orchestrator holds the sole right to run `pytest` and to commit.

## Wave 1 — dispatched (3 workers + research + review, concurrent)

- Task 1 (Worker A, sonnet) — docker invocation identity, `--name=` collision, `_DOCKER_CANNOT_RUN_EXPLAINED`
  message, `_c_toolchain_gate` clock branch. Owns `workers/buildverify.py`, `tests/test_workers_build.py`.
- Task 2 (Worker B, sonnet) — D26 then D27, in that order. Owns `cli.py`, `vcs/git.py`, `tests/test_cli.py`.
- Task 3 (Worker C, sonnet) — `ScriptedRunner` invariant + anti-drift test, then the three raw-exec sites.
  Owns `util/proc.py`, `vcs/github.py`, `vcs/gitea.py`, `vcs/filter_repo.py`.
- Task 4 (Research, opus) — Q1 docker name collision semantics, Q2 test-step 125 replay, Q3 `build_diagnosis`
  consumer + `log_path` UUID vs `--llm-cache read-only`, Q4 `_c_toolchain_gate` pull timeout.
- Task 5 (Review, opus) — audit `44d5550..68a41ff` for further overstated evidence.

Worker A is blocked on research Q1; Worker A's third sub-item is blocked on Q4. Route research
results to Worker A on return.

## Verification protocol for this wave

No agent may run `pytest` — `conftest.py::pytest_sessionfinish` reaps `BAZEL_ROOT` children, so a
finishing session destroys any concurrent one. The orchestrator's checkpoint-35 confirmation run was
in flight at dispatch time. Order: confirmation run finishes → integrate worker diffs → ONE serialized
full-suite run → commit.

## Wave 1 results

- **Task 3 (Worker C): DONE, one defect returned.** `mypy src/fleet/ --strict` clean (107 files);
  src/ changes accepted. Returned for: three Pyright type errors in `tests/test_gitea.py` (411, 424,
  443) — `RaisingRunner`/`FixedResultRunner` not assignable to `_forge`'s concrete `RecordingRunner`
  parameter. The gate is `mypy src/fleet/`, which does not cover `tests/`, so this would never fail
  CI — which is the reason to fix it, not to shrug. Told C not to use `# type: ignore` and not to
  make the doubles inherit from `RecordingRunner`; the likely fix is widening `_forge` to the protocol.
- **Task 5 (Review): 3 Critical, 5 Important, 4 Minor.** Report at `plans/review-36.md`.
  - C1/C2 — the sentence `68a41ff` retracted still SHIPS in two places: `_DOCKER_CANNOT_RUN_EXPLAINED`
    (`buildverify.py:436`) and, worse, `clock_failure`'s docstring (`base.py:161`), which that same
    commit made the canonical statement both workers defer to. Routed to Worker A; lane extended to
    `base.py` (docstring only). Ladder facts supplied: `retry.py:200-202` caps free retries at 4;
    `_diagnose` fires two LLM calls on rungs 2-3 regardless of class.
  - C3 — **ADR-0067 documents four code changes, none of which exist.** Unassigned; needs a docs worker.
  - I5 — one `GitCommandError` site still drops `started`, under a docstring implying none do.
    Routed to Worker B (owns `vcs/git.py`), sequenced after D26/D27.
  - I1, I3, I4, M1–M4 — docs/test-accuracy items, unassigned. I3 (a measured, unfixed defect living
    only in a commit body) needs a D-number in `INTEGRATION_HONESTY.md`.
  - Reviewer's root cause: there is no `PROGRESS.md` §36, so retractions had nowhere to land.
- Lint: `ruff` had 1 error (E501, `buildverify.py:363`) from Worker A's in-flight docstring. Routed to A.

## Wave 2

- **Task 3 (C): COMPLETE.** Root cause of the type errors was pre-existing: `_forge`'s `runner` param
  annotated with the concrete `RecordingRunner` instead of the `CommandRunner` Protocol that
  `GiteaForge.__init__` actually accepts. C's new doubles exposed it, did not cause it. Retyped to the
  Protocol — consistent with CLAUDE.md Guardrail 3.
- **Task 4 (D): COMPLETE.** C3 CONFIRMED — **all four** ADR-0067 claims were absent at `68a41ff`:
  no `ProbeIndeterminateError` anywhere; `cli.py:4163` still one `except EngineUnavailableError` arm;
  `cli.py:4165` still `break`; `_no_verdict` still the full implementation, not a delegator. Marked
  the ADR "DECIDED, NOT YET IMPLEMENTED" rather than deleting it, cross-referenced existing D37.
  Also: new **D46** (I3's container-name collision), new **PROGRESS.md §36**, I4 false-history
  correction, M1 rename.
- **Task 2 (B): COMPLETE.** D26 fixed first (pathspec-scoped `git diff --cached` via `diff_stat`,
  replacing whole-worktree `is_dirty()`), then D27 (blob-SHA comparison against the branch via new
  `Git.hash_object()` / `Git.blob_at()`). B empirically REJECTED `git diff --quiet <rev>`: blind to
  untracked files, which is exactly the crash state D27 must detect. I5 fixed at `cli.py:3634`.
  Verified without pytest via a standalone `asyncio.run()` script plus raw `git`, and confirmed the
  pre-fix failure modes first.
- **Task 5 (E): COMPLETE.** M4 — dropped the unverifiable "git 2.20.4 → 2.49.1" range and the
  self-contradiction ("same signal as git", refuted by its own next sentence); now cites only the
  measured host git 2.43.0, pinned by an existing test (`clone.py:743`). False history at 700-706
  rewritten from `git show 44d5550~1`. M2 — verified the vacuity empirically (monkeypatch, no
  pytest): `cloned.retryable` was compared against `built`/`expected`, which always move together,
  so the assertion could not fail. Rewritten to pin `cloned.retryable is True` directly and
  **confirmed it now fails when `_error_for` is broken**. No production change needed — the
  hardcoded `True` was not wrong, only untested.
- **Task 1 (A): fix round 2** — I2 (impossible stderr/exit pairing in the D34 fixture) and M1's
  substantive half (`blips=5`, past ADR-0014's 4-retry cap). A's earlier type-error fixes accepted;
  it also caught a latent bug (`value or []` silently skipped falsy `0`/`False`).

## Serialized verification (wave 1 integrated)

`1 failed, 1159 passed in 803.12s`, gates clean (mypy 107 files, ruff clean).

- FAILED `test_a_probe_killed_mid_pull_is_a_substantive_timeout_not_a_missing_compiler`
  (`tests/test_workers_build.py:1246`). NOT a logic defect: the production message for the
  killed-mid-pull case explains itself using the phrase the test forbids ("…so `no C compiler`
  would be fabricated here too"), so the `not in` substring proxy cannot distinguish the verdict
  being *denied* from being *asserted*. Routed to A with a structural fix requested — assert on
  failure class and the field carrying the compiler verdict, not on prose. Prose is not an API.
- **Cause of the miss is my own constraint:** workers were forbidden to run pytest, so A could not
  have caught this. The serialized run is exactly the net for it. Recorded as a cost of the rule,
  not a fault of the worker.
- **The skip disappeared.** Prior run: `1141 passed, 1 skipped`. This run: `-rs` printed nothing and
  zero skips were reported. The suite's coverage therefore varies with ambient environment state
  (most likely a docker/image gate computed at import time) **without announcing it**. A suite that
  silently covers less on some runs than others while both report green is a measurement-integrity
  defect in its own right — candidate for a D-number. Do not close this by assuming which gate;
  measure it.

## Queued, NOT yet assigned

- **`GitCommandError.started` should arguably be a required keyword.** B's evidence: 1 of 3
  construction sites silently regressed under the `= True` default, and the default makes a forgotten
  flag claim the process started. Deferred because the change spans `git.py`, `clone.py`, and other
  callers; E holds `clone.py` right now. Assign as one focused cross-lane task after E reports.
- Pyright vs mypy divergence at `cli.py:6105` (`EcosystemRegistry` / `LayoutAdapter` protocol,
  `monorepo_dir` not a ClassVar). mypy passes; pyright objects. Worth one look once B's lane is free.

## Events

- Killed the first checkpoint-35 confirmation run: stalled 32 min at 6% on 38 s CPU, two idle Rust
  bazel servers; its early phase overlapped four worker suite runs, so its lone `F` was untrustworthy.
  Restarted clean. `1142 passed` in `68a41ff` remains agent-reported until that run lands.
- **Confirmation landed: `1141 passed, 1 skipped` in 660.81s, exit 0, no failures, peak 4.22 GiB,
  residual 0 bytes.** The commit message for `68a41ff` says `1142 passed`. Collected total agrees
  (1142); the implementing agent counted the skip as a pass. Correct the figure at the next docs
  touch — the suite is green, but one test did not run and the commit implies all did.
- The skipped test is NOT in `test_sandbox.py`, `test_rewrite.py`, or `test_gitea.py` (each re-run
  with `-rs`: 51, 51, 30 passed, zero skips). Identify it with `-rs` on the next serialized full run
  rather than spending another 11-minute run now.
- Note on that re-run: `test_gitea.py`'s live gate did NOT skip, so `CURL_CONFIG` exists and the
  local Gitea answered — the live tests ran against real infrastructure. Relevant to D22
  (credential-file mode is not enforced).
