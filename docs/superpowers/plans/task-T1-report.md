# Task T1 — D39 surviving half: `GitHubCli.available()`

## STATUS: DONE

## Commit
(recorded after commit below)

## Caller trace (done before changing anything)

`GitHubCli.available()` / the `Forge.available()` Protocol method has **no production caller**
anywhere in `src/fleet`. Traced every call site:

- `src/fleet/workers/prwriter.py` calls `create_pr`, `view`/`sync` on a `Forge`, and catches
  `ForgeError` (which `GhUnavailableError` also derives from) into
  `FailureClass.TRANSIENT_INFRA` — it never calls `.available()`.
- `src/fleet/cli.py`'s `_forge()` builds a `Forge` via `build_forge` and hands it to the pr
  workers; no call site in `cli.py` invokes `.available()` either.
- The only callers of `.available()` in the whole tree are tests: `tests/test_vcs.py` (this
  file) and `tests/test_gitea.py`'s equivalent live-forge test for the Gitea driver. Both use it
  purely as a live/skip gate ("does this test have a real, authenticated binary to talk to"), not
  as a decision that changes runtime behaviour for a migration repo.

So the "does a caller quietly skip the forge integration on `False`" concern the task asked me to
rule out does not apply — nothing in production reads this return value at all today. `available()`
is exactly what `github.py`'s module docstring already claimed: "the honest gate for the tests that
genuinely need the binary." That means turning the indeterminate case into a raise carries **zero
production behaviour change** — it only changes what a test sees, and only when the probe never
settled (a case that can't currently happen deterministically outside a scripted double).

## Fix

`GitHubCli.available()` (`src/fleet/vcs/github.py`) no longer routes through `_exec` (left
untouched, per instructions — its `FileNotFoundError` catch is the fixed D39 half and is pinned by
`test_gh_a_passed_deadline_is_not_mistaken_for_a_missing_binary`). `_exec`'s `check=True` path
collapses "ran and said no" and "never settled" into one `GhError` string, so by the time that
exception exists there's nothing left to branch on except stderr substring-matching — exactly what
the harness's §3.3 rule forbids. Instead `available()` now calls `self._runner` directly (same
argv/cwd/deadline/timeout_s as `_exec`), and:

- `FileNotFoundError` (binary genuinely absent) → `False`, no raise. Settled true negative.
- Runs to completion → `util.proc.no_verdict(result) is None` → returns `result.ok` (settled
  answer, `True`/`False` unchanged from old behaviour for this case).
- Doesn't run to completion (`no_verdict` returns a reason — never started, or killed at its
  deadline) → raises `GhError` naming the reason, instead of silently becoming `False`.

This follows the house idiom from `d37f4ba`/ADR-0067 (`vcs/git.py`'s `_require_settled`): raise on
indeterminate, return the settled answer unchanged. `GhUnavailableError` still means "binary not
here" and `available()` still answers `False` for it, per the task's explicit instruction — it's
only the OTHER `GhError` causes (clock failures) that stop collapsing into `False`.

## Tests added (`tests/test_vcs.py`)

- `test_available_reports_false_for_a_genuinely_missing_binary` — `RaisingRunner(FileNotFoundError)`
  → `available() is False`, no raise. Proves the fix didn't turn every negative into an error.
- `test_available_reports_false_for_a_settled_unauthenticated_exit` — settled non-zero exit (real
  `gh auth status` failure) → `available() is False`. The "not a blanket raise" case, mirroring
  `d37f4ba`'s equivalent test for the git probes.
- `test_available_does_not_report_false_for_an_unsettled_probe` — never-started
  (`started=False, timed_out=True, exit_code=124`) → raises `GhError`, not `GhUnavailableError`.
- `test_available_does_not_report_false_for_a_deadline_kill` — killed-at-deadline
  (`started=True, timed_out=True`) → also raises `GhError`, message names the reason.

All four use the existing `ScriptedRunner`/`RaisingRunner` doubles already in the file; no new
fixtures needed. Docstrings on both the tests and `available()` state explicitly that "not
installed" and "could not determine" are different facts.

## Verification

- `pytest tests/test_vcs.py -q` → 54 passed (single session, no `FLEET_*` exported).
- `mypy --strict src/fleet` → `Success: no issues found in 107 source files` (baseline unchanged).
- `mypy --strict tests/test_vcs.py` → 1 error, `tests/test_vcs.py:804: error: Statement is
  unreachable` — the pre-existing, unrelated error the task named. Not touched, not masked by a
  second one.
- `ruff check src/fleet/vcs/github.py tests/test_vcs.py` → `All checks passed!`

## Concerns / is D39 fully closed?

D39 had two halves. The `_exec` half was already fixed and is untouched here. The `available()`
half is now fixed and tested. I consider **D39 fully closed** with this change, with one caveat
worth recording: `available()` has no production caller, so this fix is closing a real defect in
a method that is, today, purely test/operator-facing infrastructure (docstring: "the honest gate
for the tests") rather than something that changes any migration-run decision. That's not a reason
to leave it broken — a method whose docstring promises a specific two-outcome contract should keep
that contract, and a future caller (or a human running `fleet` interactively) would inherit the bug
otherwise — but it does mean this fix's real-world blast radius today is the test suite's honesty,
not a live migration path. Flagging it rather than asserting more urgency than the evidence
supports.

No other files in this lane (`src/fleet/vcs/github.py`, `tests/test_vcs.py`) were touched beyond
this change. `src/fleet/vcs/gitea.py` and `src/fleet/vcs/git.py` were left alone as instructed.
