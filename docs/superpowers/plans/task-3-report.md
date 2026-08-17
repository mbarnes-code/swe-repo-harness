# Task 3 (Worker C) report

Status: COMPLETE. Tests written, not executed (hard constraint honored — no pytest run).

## Files changed
- `src/fleet/util/proc.py` — added `is_producible_shape(*, started, timed_out, exit_code)`,
  the invariant read off `_run_locked`: `started=False` is producible only as
  `(timed_out=True, exit_code=TIMEOUT_EXIT_CODE)`; `started=True` is unconstrained.
- `src/fleet/vcs/github.py`, `src/fleet/vcs/gitea.py`, `src/fleet/vcs/filter_repo.py` — at each
  call site: removed dead `exit_code == 127` / stderr-text checks (unreachable, no shell exists
  to produce them), removed the `not result.started` → "*Unavailable*" mislabel (that state is
  only the clock's passed-deadline synthesis and now falls through to the normal `not result.ok`
  path, correctly reported as a clock failure), and added `except FileNotFoundError` around the
  runner call to classify a genuinely missing binary as `GhUnavailableError` /
  `GiteaUnavailableError` / `FilterRepoUnavailableError` instead of letting it escape raw.
- `tests/test_vcs.py` — `ScriptedRunner.__init__` now validates via `is_producible_shape` (raises
  `ValueError` on an impossible state); added `RaisingRunner` double; fixed two tests that
  simulated "missing binary" via the now-impossible `exit_code=127` shape; added 4 new tests.
- `tests/test_proc.py` — added the anti-drift tests (derive ground truth from a live `run()`
  call, not from the docstring).
- `tests/test_gitea.py` — added `RaisingRunner`/`FixedResultRunner` doubles + 3 new tests
  mirroring the github/filter_repo coverage.

## Test names (written, unexecuted)
`test_is_producible_shape_matches_the_real_never_started_branch`,
`test_scripted_runner_cannot_construct_states_the_real_runner_cannot_produce`,
`test_relocate_exit_127_is_an_ordinary_failure_not_a_missing_binary`,
`test_relocate_a_passed_deadline_is_not_mistaken_for_a_missing_binary`,
`test_gh_exit_127_is_an_ordinary_failure_not_a_missing_binary`,
`test_gh_a_passed_deadline_is_not_mistaken_for_a_missing_binary`,
`test_a_missing_curl_is_named_not_retried_forever`,
`test_curl_exit_127_is_an_ordinary_failure_not_a_missing_binary`,
`test_a_passed_deadline_is_not_mistaken_for_a_missing_curl`;
plus corrected `test_relocate_names_a_missing_git_filter_repo_instead_of_falling_back` and
`test_a_missing_gh_is_named_not_retried_forever` (now use `RaisingRunner` instead of the
impossible `exit_code=127` state).

Verified via `ruff check` (clean) and `py_compile` on all touched files — not pytest.

## Questions to route to research
None. The `.claude/settings.json`/`CLAUDE.md` diff visible in `git status` predates this session
and was not touched.

## Follow-up: pyright errors on tests/test_gitea.py (addressed)

Root cause confirmed as diagnosed: `_forge`'s `runner` parameter was annotated with the
*concrete* `RecordingRunner` class, not the `CommandRunner` Protocol `GiteaForge.__init__` itself
accepts. `RaisingRunner`/`FixedResultRunner` satisfy `CommandRunner` structurally (same `__call__`
shape) but aren't `RecordingRunner` instances, so the concrete annotation was simply wrong — an
existing latent defect my new doubles exposed rather than caused.

Fix: `tests/test_gitea.py` now imports `CommandRunner` from `fleet.util.proc` and `_forge`'s
`runner` parameter is typed `CommandRunner`. No doubles changed. `pyright --pythonpath
.venv/bin/python tests/test_gitea.py` → 0 errors (was 3). No `# type: ignore` used.
