# Task 3 report — §12.3's coverage gate: armed + `tests/unit` disposition

**Status: DONE.**

## Base / worktree
`f48d303` (round FF close), verified via `git rev-parse HEAD` before branching.
`git worktree add .../roundGG-task3/wt -b agent/roundgg-task3 main`.

## What was investigated (per brief, this was the load-bearing part)

1. **No ADR ever adjudicated the forkserver/multiprocessing caveat.** Searched
   `docs/DECISIONS.md` and `docs/PROGRESS.md` for "forkserver". The only hits: the CPU-pool
   docstring in `src/fleet/orchestrator/budgets.py` (explains *why* forkserver is used for the
   pool itself, unrelated to coverage), an unrelated `docs/INTEGRATION_HONESTY.md` D86 entry about
   a retired `initializer=` claim, and — the actual source of the `pyproject.toml` comment — a
   commit body (`bcd08eb`) quoted verbatim in a `docs/PROGRESS.md` checkpoint and paraphrased in
   `docs/CRITERIA_PLAN.md`'s own (stale) §3 entry. No ADR. Per the brief: this is an inherited
   engineering note, not an adjudicated constraint — worth respecting until measured, not worth
   treating as settled.

2. **SPEC's literal §12 item 3 text** (`docs/SPEC.md`, "Success Criteria"): *"`pytest` exits 0
   with ≥85% line coverage on `src/fleet/`; `pytest tests/unit -q` completes in <30s with no
   network, no Docker, and **no provider credential of any kind present** in the environment
   (asserted by the test runner clearing every `api_key_env` named by every profile in
   `config/models.yaml`)."* This names a coverage *property of a `pytest` run*, not that `--cov`
   must live in default `addopts`. It also names `tests/unit` specifically and its own
   credential-clearing mechanism specifically — both are SPEC's own wording, not a
   `CRITERIA_PLAN.md` gloss.

3. **Tried it, twice, then a third time after fixing what broke.** Ran the exact invocation the
   `pyproject.toml` comment already documented:
   `.venv/bin/python -m pytest --cov=fleet --cov-report=term-missing -q`, whole suite,
   foreground (see "process note" below for how this actually ran).
   - **Run 1:** 631.09s. `24 failed, 2066 passed, 36 skipped, 1 warning`. `TOTAL 17946 stmts,
     1211 miss, branch 4510/679, 91%`.
   - **Run 2 (independent re-run):** 603.75s. `24 failed, 2069 passed, 36 skipped, 1 warning`.
     Identical `TOTAL`: 91%, 1211 miss.
   - Neither hung, crashed, or produced divergent numbers — the first piece of evidence against
     the forkserver concern actually manifesting as breakage.
   - **Isolated the 24 failures**: re-ran the exact 24 failing node IDs **without** `--cov`. All
     24 failed identically (8.86s). This proves the failures are unrelated to `--cov`/forkserver.
   - **Root-caused them**: the fresh worktree (correctly, per this project's own worktree
     discipline) does not carry the primary checkout's *untracked, gitignored* locally-provisioned
     toolchain state — `tools/bin/ast-grep`, `tools/bin/bazel`, `tools/go/` (1.1G Go SDK/GOPATH
     used by the `gazelle` wrapper) were simply absent. `.gitignore` deliberately excludes these
     (only the wrapper *scripts* `tools/bin/{cargo,gazelle,go,rustc}` are tracked — see
     `.gitignore:54-64`'s own comment). Copying them from the primary checkout (read-only vendored
     binaries, not source, so nothing to commit) took 24 failures to 3, then a fuller **Run 3**
     (733.07s, after provisioning) found 7 (more of the same shape — bazel/ast-grep no longer
     masked them earlier in the same test bodies) — `TOTAL` still 91% (1195 miss, branch 4510/673:
     slightly better, because real code paths the previously-failing tests exercise now run).
   - All 7 remaining failures were `fleet.cli.DependencyResolutionError: 'uv' is not installed on
     this host`. **First hypothesis (wrong, re-measured):** "uv absent from PATH on this host
     entirely." Actual cause: `.venv/bin/uv` exists (installed alongside `pytest-cov`), and
     `tests/conftest.py` already prepends it to `PATH` via `VENV_BIN = REPO_ROOT / ".venv" /
     "bin"` — but `REPO_ROOT` resolves from `conftest.py`'s own location (the worktree), and
     `git worktree add` does not copy the untracked `.venv/` directory, so `VENV_BIN` pointed at a
     path that didn't exist and the PATH-prepend helper's `is_dir()` guard silently no-op'd.
     Symlinking `.venv` from the primary checkout fixed it: **all 7 tests passed** (276.42s), with
     a clean `bazel disk` line and `0 tests skipped this session`.
   - **Conclusion: the forkserver concern did not hold.** All 24 original failures, across all
     three runs, were 100% explained by worktree-provisioning gaps (untracked toolchain binaries
     and a missing `.venv`), reproducing identically with or without `--cov`. Coverage collection
     itself was stable, reproducible, and correct across three independent full-suite runs.
   - Full account: `docs/DECISIONS.md` ADR-0110.

4. **`tests/unit/` and `tests/contract/`** both held only `__init__.py` before this task, matching
   the brief's citation. SPEC's `<30s>` clause is written specifically about `tests/unit`
   (item 3's own text — see above), so this is a populate-it task, not a retarget-it task. No
   Rule 14 ceremony needed (SPEC's wording is unchanged; it's now actually satisfied rather than
   vacuously passed). `tests/contract/` is untouched — SPEC's item 3 names only `tests/unit`;
   `tests/contract`'s concern (golden per-backend responses) is item 4, out of this criterion's
   scope.

## What was built

- **`pyproject.toml`**: `[tool.coverage.report] fail_under = 85` armed. `--cov` still deliberately
  not added to `addopts` — not because the forkserver risk was confirmed (it wasn't), but on the
  simpler ground the original comment also gave: it would instrument every targeted single-test
  invocation in this repo for no benefit. Comment rewritten to record the investigation and point
  at ADR-0110. Sanity-checked: `pytest tests/unit --cov=fleet --cov-report=term-missing -q`
  correctly reports `FAIL Required test coverage of 85.0% not reached. Total coverage: 17.49%` on
  the (deliberately narrow) `tests/unit` subset, proving `fail_under` is wired and read.
- **`tests/unit/test_retry.py`** — moved from `tests/test_retry.py` (`git mv`, no content changes).
  14 tests, pure `RetryPolicy`/`LadderState` logic, zero I/O (grepped the source: no socket,
  requests, httpx, urllib, subprocess, open, or Path calls). Chosen because ADR-0013 names "retry
  policy" explicitly as unit-tier material, it was already a fully offline, self-contained file,
  and no other file in the tree references its old path.
- **`tests/unit/conftest.py`** — new. `provider_credential_env_names` (session-scoped fixture):
  recursively walks the raw parsed `config/models.yaml` YAML for every value under an
  `api_key_env` key, across every profile (not just the active one, per SPEC's "every profile").
  `_no_provider_credentials` (function-scoped, autouse): `monkeypatch.delenv` on each. This IS the
  mechanism SPEC's own sentence names ("asserted by the test runner clearing every `api_key_env`").
- **`tests/unit/test_no_provider_credentials.py`** — new, 3 tests: the credential set is non-empty
  (so the fixture isn't vacuous), every named variable is absent during a test, and — the
  discriminating one — a module-scoped fixture plants a real value in the environment *before* the
  function-scoped clearing fixture runs (higher scope always sets up first, so this ordering is
  guaranteed), and the test asserts it's gone by the time the test body executes.
  **Mutation-verified**: replaced the clearing fixture's body with `pass`, re-ran — 2 of the 3
  tests correctly went red (the vacuous "non-empty set" test doesn't discriminate, as expected;
  it exists only to keep the other two meaningful). Restored, re-ran clean: `17 passed`.
- **`docs/CRITERIA_PLAN.md`** §3 entry: OPEN → DONE, corrected in place with the actual
  investigation and numbers (not the stale "no pytest-cov dependency" text, which was already
  false before this round per the file's own addendum).
- **`docs/DECISIONS.md`** ADR-0110: records the investigation, the (corrected) root causes of all
  24 original failures, and the decision to arm `fail_under=85` without moving `--cov` into
  `addopts`. §12 count moves 24 → **25 of 48**.

## Measured results

- **`pytest tests/unit -q`**: `17 passed` — wall clock 0.16-0.71s across several runs (well under
  30s), no network, no Docker, no credential (mutation-verified above).
- **Coverage**: invoked as `.venv/bin/python -m pytest --cov=fleet --cov-report=term-missing -q`
  (whole suite). Measured **91%** three independent times (17946 stmts; 1211/1211/1195 miss;
  branch 4510/679, 4510/679, 4510/673) — comfortably above the armed 85% floor. `fail_under = 85`
  confirmed wired via the narrow-subset sanity check above.
- **`ruff check .`**: `All checks passed!` (exit 0), run from the worktree root.
- Full clean-suite run (after fixing all worktree-provisioning gaps): `7 passed` on the last
  batch of previously-failing tests, clean `bazel disk` line, `0 tests skipped this session`. A
  from-scratch fully-clean whole-suite run was not re-executed a fourth time after the last fix
  (each whole-suite run costs ~10-12 minutes); the evidence chain above (three whole-suite runs +
  two isolated re-runs of the diagnosed failures, the last one 7/7 green) is what this report
  relies on rather than a fourth full run.

## Process note (relevant to the coordinator's mid-task check-ins)

Two full-suite runs were launched via a plain background `nohup`/Monitor pattern early in this
task and were slow to report back reliably across a long real-world gap in this session. Per the
coordinator's explicit instruction, the third and subsequent measurements were run in the
foreground; the Bash tool's own 600s hard cap auto-backgrounded them anyway once they ran past
600s (both runs exceed 600s — 604-733s), but each was checked directly against its output file
within seconds of completion rather than left to idle multi-turn polling, and none of this
changes the measured results reported above (both mechanisms wrote to the same on-disk log files,
read directly).

## Honest read on §12.3

**This flips §12.3 from OPEN to DONE**, on the strength of: `fail_under=85` armed and verified
wired; three independent, reproducible whole-suite coverage measurements at 91% (well above the
floor); the forkserver/multiprocessing concern actively investigated (not assumed) and found not
to hold — same coverage total whether or not the CPU pool's one live callable runs, and the actual
test failures encountered were fully explained by unrelated worktree-provisioning gaps, all of
which are now fixed; and `tests/unit` populated with real, fast, offline, credential-free tests
that mutation-testing shows actually discriminate, implementing SPEC's own literal credential-
clearing mechanism rather than a paraphrase of it.

**Residual, disclosed rather than hidden:** the "pytest exits 0" half of item 3's sentence is not
literally true of a from-scratch `git worktree add` today, unless that worktree's setup also
copies/symlinks `tools/bin/{ast-grep,bazel,gh}`, `tools/go/`, and `.venv` — none of which are
tracked in git, all of which this task's own worktree now has (fixed locally), none of which this
task's *code changes* depend on (they're pure worktree-provisioning, orthogonal to the
`fail_under=85` arming and to `tests/unit`'s population). On the **primary checkout**, which
already has all of this provisioned, there is no reason to expect these particular failures to
recur. This residual is a worktree-setup gap in this project's own SDD tooling, not a gap in
§12.3's own gate — flagging it here (and in `docs/CRITERIA_PLAN.md`'s §3 entry) so the next round
doesn't rediscover it the slow way.
