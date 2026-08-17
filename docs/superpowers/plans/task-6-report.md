# Task 6 (Worker F) report — docker filter regex escaping, 125 prose correction, clock-branch redundancy check

Status: **DONE**

## Task 1 — `.`-as-regex over-match (CRITICAL)

Escaped in `src/fleet/sandbox/container.py::ContainerSandbox.list_by_prefix` (the ONLY
`docker ... --filter name=^...` site in `src/`; `cli.py`'s `--filter` options are unrelated
fleet-status filters). Changed `f"name=^{prefix}"` → `f"name=^{re.escape(prefix)}"` (added
`import re`). Checked every other name/prefix interpolation site (`docker_run_argv`'s `--name=`,
`remove`'s `docker rm`, `stop`'s `docker stop`): all pass the name as a literal argv element to a
non-regex flag, so no other escaping was needed. `slug()` in `worktree.py` was left untouched
(unescaped-at-source was rejected in favor of escaping-at-the-regex-boundary, per the research's
recommendation).

Tests (`tests/test_sandbox.py`): added `RegexFilterRunner`, a fake that matches Docker's real
`--filter name=^<pattern>` regex semantics (not a literal-prefix stub) against a name pool, and
`test_list_by_prefix_escapes_dots_so_a_sibling_repo_is_not_cross_matched` — repos `my.repo.js` /
`myXrepo.js`, dotted so they'd cross-match unescaped. Verified by hand (one-off `.venv/bin/python3`,
no pytest) that the old unescaped pattern matches both names and the fixed code matches only the
target. Also fixed `test_reap_lists_by_run_prefix_and_spares_live_containers`'s assertion, which
depended on the now-escaped argv string.

## Task 2 — 125 prose at `buildverify.py:845`

Corrected: removed "an unreachable daemon" from the 125 causes list (measured: exit 1, not 125),
replaced with the real causes (name collision, invalid memory/cpus, bad `--network`/`--workdir`),
and added a sentence stating only stderr text distinguishes them. **No stderr-sniffing classifier
built** — the six causes all reach the harness as the same non-retryable `BUILD_ERROR` today, and
building a classifier to split them wasn't asked for and isn't justified by anything currently
consuming the distinction. If precision here is later wanted, my recommendation is a narrow
substring match on `Conflict.`/`is already in use by container` to special-case the one
actionable, retryable cause (name collision — everything else is a config problem needing a
human), not a general-purpose classifier.

## Task 3 — `_c_toolchain_gate` clock branch: NOT redundant, kept as-is

Verified via `git diff 68a41ff 8464dc6 -- src/fleet/workers/buildverify.py`: before this shipped
commit, `_c_toolchain_gate` had zero clock handling — the diff is a pure addition, not a
duplicate of anything already in that function. It is also not redundant with
`classify_build_failure`'s own `clock_failure` call (line 421): that function is invoked only at
`error_from_proc` (line 509) on the build/test step's `ProcResult`, never on the probe's — grepped,
one call site. The two branches guard two different `docker run` invocations that can each
independently time out or never start; neither's absence is covered by the other. Two existing
tests (`test_a_probe_the_fleets_own_deadline_killed_is_not_reported_as_a_missing_compiler`,
`test_a_probe_killed_mid_pull_is_a_substantive_timeout_not_a_missing_compiler`) pin this real
behavior — left untouched.

## Files changed

- `src/fleet/sandbox/container.py`
- `src/fleet/workers/buildverify.py`
- `tests/test_sandbox.py`

## Tests added/changed

- `tests/test_sandbox.py::test_list_by_prefix_escapes_dots_so_a_sibling_repo_is_not_cross_matched` (new)
- `tests/test_sandbox.py::test_reap_lists_by_run_prefix_and_spares_live_containers` (assertion fixed for escaped argv)

## Addendum — second stale argv assertion (orchestrator follow-up)

`tests/test_workers_build.py::test_on_cancel_sweeps_every_container_a_dead_run_could_have_left_by_prefix:1580`
also pinned the raw prefix (`re.escape` escapes `-` too, so it differs even without a dot).
Searched both owned test files — no third occurrence. Replaced with: an explicit, commented
`filter_arg == f"name=^{re.escape(prefix)}"` assertion (says why escaping is required, so
unescaping it back doesn't silently pass), plus a regex-interpreted check that the resulting
pattern actually matches both leaked names — the dotted-sibling non-match property itself stays
in its dedicated regression test in `test_sandbox.py` rather than duplicated here. `removed ==
{leaked_probe, leaked_build}` (the real load-bearing assertion) is untouched.

## Verification

`.venv/bin/ruff check src/ tests/` — all checks passed. `.venv/bin/mypy src/fleet/ --strict` —
success, 107 files. `py_compile` on all touched files — OK. Fix logic hand-verified with one-off
`.venv/bin/python3` (not pytest, per hard constraint). No pytest run, no commit.
