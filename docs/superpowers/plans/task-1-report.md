# Task 1 (Worker A) report — docker invocation identity and the 125 branch

Base commit: `68a41ff`. Scope per `docs/superpowers/plans/queue-36.md` Task 1, extended mid-task
by the orchestrator to `src/fleet/workers/base.py` (docstring only).

Status: **DONE**

## Files changed

- `src/fleet/workers/buildverify.py`
- `src/fleet/sandbox/container.py` (additive change to `spec_for_attempt` — the "docker sandbox
  argv construction" the plan names as part of this task; no default behaviour changed)
- `src/fleet/workers/base.py` (docstring only, `clock_failure` — logic untouched, per the
  orchestrator's instruction; this function is shared with `clone._error_for`, which I did not
  touch)
- `tests/test_workers_build.py`

Did not touch `src/fleet/cli.py` or `src/fleet/vcs/*` (Worker B's files) — no change of mine
required anything there.

## 1. `--name=` collision (the item flagged as mattering most)

**The defect.** `RetryPolicy.decide` (`orchestrator/retry.py`) re-runs a `TRANSIENT_INFRA`
failure — 125 among them — on the *same rung*: identical `(run_id, repo, attempt)`,
`phases.attempts` untouched. `spec_for_attempt` defaulted the container's `--name=` to bare
`sandbox_name(run_id, repo, attempt)`, which is byte-identical across every one of those retries.
If the docker daemon that produced the first 125 had managed to *register* a container under that
name before dying — exactly what a mid-wave daemon restart can leave behind — the retry's own
`docker run --name=<same>` meets docker's "the container name ... is already in use" and exits 125
again, this time *permanently*: no amount of retrying removes a name collision. That is the one
case where `classify_build_failure`'s `retryable=True` for 125 is actively wrong.

**The fix.**
- `sandbox/container.py`: `spec_for_attempt` gained an optional `name: str | None = None`
  parameter. When omitted, behaviour is byte-for-byte unchanged (`sandbox_name(...)`), so
  `tests/test_sandbox.py` (not owned by me) and `rdepverify.py`'s convention are untouched. When
  supplied, it overrides the default — purely additive.
- `buildverify.py`: two new module-level helpers.
  - `_container_prefix(ctx)` — `sandbox_name(...) + "-t"`. The `-t` separator is load-bearing:
    `list_by_prefix`'s `docker ps --filter name=^<prefix>` is a regex anchored only at the start,
    so a bare `...-1` prefix would also match `...-10-t...` (attempt 10's containers). Attempt
    numbers are always digits and `-t` is not, so this cannot happen.
  - `_invocation_name(ctx, *, suffix="")` — `_container_prefix(ctx) + uuid4().hex[:8] + suffix`,
    a fresh token per call.
  - `_argv` now passes `name=_invocation_name(ctx)` to `spec_for_attempt` for the build/test step.
  - `_c_toolchain_gate`'s probe container now uses `_invocation_name(ctx, suffix="-cc-probe")`
    instead of the old deterministic `f"{sandbox_name(...)}-cc-probe"` — the probe had the
    identical latent collision (it runs first, on every retry of the same rung too).
  - `on_cancel` can no longer remove one exact name (there isn't one anymore), so it now sweeps
    by prefix: `ContainerSandbox.list_by_prefix(_container_prefix(ctx))` then `remove()` each
    match. **This gives `list_by_prefix` its first caller in `src/`** (D32: recorded implemented
    with zero call sites). I documented in `on_cancel`'s docstring that this closes only the
    *cancellation* half of D32 — a plain deadline timeout that is not a cancel still does not
    reach `on_cancel` at all (that dispatch lives outside this file), so a container orphaned by a
    pure timeout still waits for `ContainerSandbox.reap` at `fleet resume`. I did not overclaim
    this as closing D32 fully.

## 2. `_DOCKER_CANNOT_RUN_EXPLAINED`'s false "no attempt charged" claim

Measured against `orchestrator/retry.py`: `TRANSIENT_INFRA` retries free of a charged attempt only
while `transient_retries < RetryPolicy.max_transient_retries` (default 4). Past that cap, the
*same* 125 falls through to the substantive branch and charges a rung like any other failure — so
"no attempt charged" was true only conditionally, not always. Separately (raised by the
orchestrator's code-review pass, not something I'd caught myself before the interrupt): `_diagnose`
fires whenever `ctx.context_policy` is set (rungs 2–3), *regardless of failure class* — so "no
repair prompted" was equally false once the ladder has advanced past rung 1. Both message
constant halves were corrected, and I found and fixed the *same* two-part overclaim in three
places total (all in `buildverify.py`, my owned file):

1. `_DOCKER_CANNOT_RUN_EXPLAINED` (the plan's explicit target).
2. The inline comment inside `classify_build_failure`'s 125 branch (`"it just does not buy a
   repair prompt first"`) — same false unconditional claim, same file, same root cause.
3. The clock-branch messages I added in `_c_toolchain_gate` (item 3 below) — my first draft of
   `never_started` repeated the identical unconditional "no attempt charged" phrasing; caught and
   corrected during my own review before this was flagged externally.

I also corrected `clock_failure`'s docstring in `base.py` (orchestrator-granted, docstring only):
its `started=False` bullet claimed unconditional free retries too. I deliberately did **not**
claim there that "the retry also always triggers a diagnosis call" — that's true for
`buildverify.run()` specifically (verified: `_diagnose` runs unconditionally after
`error_from_proc` whenever `not result.ok`), but `clock_failure` is shared with
`clone._error_for`, which has no diagnosis step at all. Stating a buildverify-specific fact as a
property of the shared function would have been the same kind of overclaim I was fixing, so the
base.py docstring only states what's true for every caller (the retry-cap fact) and explicitly
defers the diagnosis question to each caller.

## 3. `_c_toolchain_gate` clock branch

**The defect.** The probe's `docker run` shares `ctx.deadline` and a 120s budget
(`C_TOOLCHAIN_PROBE_TIMEOUT_S`, which the existing docstring already says "also pays the image
pull"). Neither a deadline-already-passed call (`started=False`) nor a killed-at-timeout call
(`started=True, timed_out=True`) — e.g. a >120s image pull — was read before falling to the
generic "no C compiler in the sandbox image" `WorkerError`: an immediate, permanent, fabricated
verdict about an image the probe never got to open.

**The fix.** Reused `workers.base.clock_failure` (already imported, already the single source of
truth `classify_build_failure` uses for the main build/test steps — the file's own stated
principle: "the line is now drawn in exactly one place"). `_c_toolchain_gate` now calls it right
after the probe's `runner(...)` and, if it returns non-`None`, returns a `WorkerError` carrying
the real `(failure_class, retryable)` — `TRANSIENT_INFRA, True` for never-started,
`TIMEOUT, True` for killed-at-deadline — with a message that says the probe never reached a
verdict rather than claiming a missing compiler, and (per item 2 above) does not overclaim being
free of cost.

## Tests added (all in `tests/test_workers_build.py`, none run)

- `test_a_transient_retry_of_the_same_rung_never_reuses_a_container_name` — two `run()` calls at
  the identical `(run_id, repo, attempt)` produce different `--name=` values but the same prefix.
  Direct regression test for item 1's core bug.
- `test_on_cancel_sweeps_every_container_a_dead_run_could_have_left_by_prefix` — seeds two fake
  leftover names (probe-shaped, build-shaped) under one prefix, calls `on_cancel`, asserts both
  are removed and the `docker ps --filter` argv is scoped to the exact prefix.
- `test_the_docker_cannot_run_message_bounds_its_free_retry_claim` — asserts the corrected message
  no longer contains the bare unconditional phrases and does mention the retry cap, without losing
  the original evidence ("never started"/"never opened").
- `test_a_probe_the_fleets_own_deadline_killed_is_not_reported_as_a_missing_compiler` —
  `started=False` shape → `TRANSIENT_INFRA`, retryable, no "no C compiler" claim, bazel never
  invoked.
- `test_a_probe_killed_mid_pull_is_a_substantive_timeout_not_a_missing_compiler` —
  `started=True, timed_out=True` shape → `TIMEOUT`, retryable, message names the likely cause
  (image pull) without fabricating a compiler verdict.
- Updated two pre-existing tests whose assertions depended on the now-removed deterministic exact
  name: `test_the_sandboxed_command_is_network_none_and_named_after_the_attempt` and
  `test_the_c_compiler_probe_asks_the_one_question_bazel_asks` (also updated its docstring, which
  asserted the old "probe deliberately not named after the attempt" framing that no longer holds
  — the probe now shares the prefix, just not the full invocation name).

## Verification

`git status --porcelain` shows other workers' files under concurrent edit (`cli.py`, `vcs/*`,
`util/proc.py`, `test_cli.py`, etc.) — not touched by me.

```
.venv/bin/ruff check src/fleet/workers/buildverify.py src/fleet/workers/base.py \
  src/fleet/sandbox/container.py tests/test_workers_build.py
# All checks passed!

.venv/bin/mypy src/fleet/workers/buildverify.py src/fleet/workers/base.py \
  src/fleet/sandbox/container.py --strict
# Success: no issues found in 3 source files

.venv/bin/python -m py_compile src/fleet/workers/buildverify.py src/fleet/workers/base.py \
  src/fleet/sandbox/container.py tests/test_workers_build.py
# COMPILE_OK
```

A repo-wide `ruff check src/ tests/` at report time shows 12 `F401` errors, all in
`tests/test_cli.py` (another worker's in-flight file) — none in files I own or touched; confirmed
by grepping the `-->` locations in ruff's output.

**pytest was never run**, per the hard constraint. Tests are written and self-reviewed
(field names against `ProcResult`'s dataclass, predicate/rule ordering against
`RecordingRunner`'s matching semantics, and argv shapes against the real
`ContainerSandbox.list_by_prefix`/`remove` argv) but not executed.

## Known, deliberately out-of-scope items (not silently done, not silently skipped)

- `_c_toolchain_gate`'s clock-branch failure still returns `WorkerResult(status="failed", ...)`
  rather than `status="timeout"` for the `TIMEOUT` shape, unlike the main build/test loop (which
  sets `"timeout" if result.timed_out else "failed"`). Only one caller (`runner.py`) inspects
  `WorkerResult.status`, and only for `"partial"`, so this has no behavioural effect I could find,
  but it is an inconsistency a future pass could clean up. Left alone as scope creep beyond the
  three named sub-items.
- D32's timeout-path gap (a plain deadline timeout never reaches `on_cancel`, so a build-step
  container orphaned that way is not swept by my change) is explicitly called out as unaddressed
  in `on_cancel`'s new docstring, not fixed — it lives outside `buildverify.py`.
- `rdepverify.py` calls `sandbox.remove(sandbox_name(ctx.run_id, ctx.repo_id, ctx.attempt))` in
  its own `on_cancel`, the same deterministic-name pattern I removed from `buildverify.py`. It is
  not in my ownership (not listed as mine, and it doesn't call `spec_for_attempt` so my additive
  `name=` parameter doesn't reach it) and I left it untouched. Worth a follow-up task.

## Questions for the research agent

None — the ladder facts needed (retry cap location and value, `_diagnose`'s unconditional-firing
behavior) were either already verifiable directly in `orchestrator/retry.py` and
`buildverify.run()`, or supplied by the orchestrator's code-review pass with exact line numbers.

## Addendum — two `mypy --strict tests/test_workers_build.py` type errors (orchestrator follow-up)

Both were in tests I wrote this round; `mypy src/fleet/ --strict` (the real gate) never sees
`tests/`, so neither would have failed CI.

1. **`result.error.stderr_tail` on `WorkerError | None`** (my new
   `test_the_docker_cannot_run_message_bounds_its_free_retry_claim`). Diagnosed: `None` is not
   possible for *this* call — the runner is built to return a 125 on the first build step, so
   `run()`'s `not result.ok` branch always sets `error` — but the general type is honestly
   `WorkerError | None` (success leaves it `None`), so the file's own established idiom
   (`assert result.error is not None` before every `.error.x` access, used throughout this file's
   other failure-path tests) is the right narrowing, not a cast. Added `assert result.status ==
   "failed"` and `assert result.error is not None` with a docstring explaining why `None` would
   mean the test's premise itself had failed.

2. **`list(value or [])` against `BuildTarget.attrs: dict[str, str | int | bool | list[str]]`**
   (pre-existing test, `test_every_file_the_generated_files_name_exists_after_the_phase_that_writes_them`,
   now line 3052). Diagnosed: the production type is correct as-is — other attribute names
   legitimately hold an int or bool — so this was the test's own over-broad handling, not a
   production bug. Beyond the type error, `value or []` was a latent logic bug independent of
   typing: a falsy `0`/`False` under `src`/`srcs` would have silently become `[]` (check skipped)
   rather than being flagged as the wrong shape. Replaced with explicit `isinstance` narrowing
   (`str` → `[value]`, `list` → `value`, anything else → `pytest.fail(...)`, satisfying ruff's
   TRY004 in a test file where `pytest.fail` is the idiomatic "this shouldn't happen" over a raw
   `raise`) — now fails loud instead of silently passing on the wrong shape.

Found and left alone: a third, genuinely pre-existing `union-attr` at line 2823
(`result.error.exception_type.lower()` against `str | None`) — not in my diff (verified against
`git diff`; last real commit touching this file before this task was `44d5550`), not one of the
two the orchestrator named. Not fixed, to stay inside the scope actually asked for.

Re-verified: `ruff check` on all four owned/touched files — clean. `mypy src/fleet/ --strict` —
clean, 107 files. `python -m py_compile tests/test_workers_build.py` — OK. No pytest run, no
commit.

## Addendum 2 — I2, M1, and the Pyright confirmation (review-36)

**I2 — the D34 fixture's stderr/exit-code pairing was impossible.** `DAEMON_GONE` paired
"Cannot connect to the Docker daemon..." with `exit_code=125`; measured (`68a41ff` commit body,
Docker 29.7.2): an unreachable daemon exits **1**, not 125. What genuinely exits 125 is
`docker run --name=X` colliding with a surviving container — the exact case `_invocation_name`
(my earlier fix this round) now removes the self-inflicted version of. Renamed the constant to
`CONTAINER_NAME_CONFLICT` with real "Conflict... already in use" stderr, renamed the test to
`test_a_docker_run_that_exits_125_is_not_reported_as_a_broken_build_file`, and updated the one
assertion that checked the old (now-absent) stderr text — a required consequence of the fixture
correction, not a weakening: classification (`TRANSIENT_INFRA`/`retryable=True`) is driven by
`result.exit_code` alone, never by stderr content, so it is unaffected and still passes.

**The finding, not papered over.** Correcting the fixture does not fail the renamed test, but it
does expose that D34's *original* motivating scenario — a daemon restart mid-wave — measures to
exit 1, and `classify_build_failure` has no exit-1 branch (by design: never reads stderr text,
and exit 1 is also bazel's own generic failure code, so the two are not mechanically
distinguishable there). That scenario is **still** a retryable `BUILD_ERROR` today, still buys
the repair prompts D34 was meant to remove, and has no test anywhere in this file. I named this
explicitly in the renamed test's docstring rather than leave the corrected test implying the gap
is closed. I did not attempt a fix — distinguishing "docker client failure" from "genuine build
failure" both reporting exit 1 needs a design decision (a stderr-based special case, which
conflicts with the file's stated "never classify by message" principle) that belongs in an ADR,
not a same-round patch.

**Same false premise, found in production text I authored this session.** The "daemon
restarted mid-wave" mischaracterization was not only in the test fixture — it was in three
places in `buildverify.py` I had written or left standing this round: `_DOCKER_CANNOT_RUN`'s
module docstring, the inline comment in `classify_build_failure`'s 125 branch, and
`_DOCKER_CANNOT_RUN_EXPLAINED`'s operator-facing "usual cause" sentence. All three now name the
real cause (a surviving container colliding on `--name=`) and note the daemon-unreachable case
exits 1. Left as a pure wording correction — no logic changed, `classify_build_failure`'s
exit-code branches are untouched.

**M1 — the substantive half.** Added
`test_a_docker_run_that_keeps_failing_past_the_retry_cap_charges_a_rung` (`blips=5`), driven
through the real `RetryPolicy` via the existing `drive_the_ladder` helper. Traced by hand against
`retry.py`: four 125s re-run free (`transient_retries` 1→4, each meeting `<
max_transient_retries`); the fifth meets `transient_retries == 4`, fails the `< 4` guard, and
charges rung 1 (`ADVANCE_LADDER`); the sixth call succeeds on the new rung. Asserts
`status is RepoStatus.SUCCEEDED` and `attempts == 1` — the exact boundary `retry.py:200-202`
encodes and the sibling `blips=1` test explicitly disclaimed. Did not touch the other worker's
rename or narrowed docstring at `test_a_single_daemon_blip_costs_the_repo_no_attempt`.

**Pyright "named possibly unbound" (line 3075 at the time reported).** Confirmed resolved: ran
`pyright tests/test_workers_build.py` directly (not pytest) — 7 errors remain, all pre-existing
and unrelated (lines ~205, 207, 725, ~2894, ~3055×3; none about `named`, none in code I touched
this round; verified against `git diff`). My `elif isinstance(value, list): named = value` /
`else: pytest.fail(...)` narrowing is what pyright uses to prove `named` bound on every reachable
path (`pytest.fail` is typed `NoReturn`).

Re-verified after this addendum: `ruff check` on `buildverify.py` and `test_workers_build.py` —
clean. `mypy src/fleet/ --strict` — clean, 107 files. `pyright` on the test file — 7 pre-existing
errors, zero new. `python -m py_compile` on both files — OK. No pytest run, no commit.

## Addendum 3 — the serialized-suite failure: a prose substring proxy, not a logic bug

`test_a_probe_killed_mid_pull_is_a_substantive_timeout_not_a_missing_compiler` failed: it asserted
`"no C compiler" not in detail`, but the production message denied a fabricated verdict by
literally saying `"so 'no C compiler' would be fabricated here too"` — the denial contains the
forbidden phrase, and a substring check cannot distinguish asserting a claim from denying it.
Confirmed via re-derivation, not by re-running pytest.

**Fix, structural as requested.** Both clock-branch tests (`_fleets_own_deadline_killed` and
`_killed_mid_pull`) no longer check message prose for the compiler-verdict question. This worker
has no dedicated "is this a compiler verdict" field, so `failure_class`/`retryable` — already
asserted in both tests — **are** the field that carries it: a genuinely fabricated "no C compiler"
verdict is categorically `FailureClass.BUILD_ERROR, retryable=False` (the two branches directly
below the clock check in `_c_toolchain_gate`), mutually exclusive with the `TRANSIENT_INFRA`/
`TIMEOUT` + `retryable=True` these tests already assert. Documented this reasoning inline so a
reader sees why the removed check was redundant-at-best and unsound-at-worst. Also rewrote the
production `killed_at_deadline` message to stop quoting `'no C compiler'` inside its own denial
(the coordinator's secondary, optional ask) — meaning unchanged, phrase not repeated.

**Re-verified the other new tests in that region for the same class.** Every `"X" not in detail`
assertion added this round was checked for whether the message could contain `X` inside a denial:
`"BUILD.bazel" not in detail` and `"no attempt charged"/"no repair prompted" not in detail` — read
the current production strings verbatim; neither phrase appears anywhere in them (confirmed by
grep against the literal constant), and both are checks about *message correctness itself* (no
structured field to substitute — the bug they guard was a documentation bug, not a classification
bug), so left as prose checks. The pre-existing (not-new-this-round) `test_a_probe_that_never_
started_is_not_reported_as_a_missing_compiler` has the identical `"no C compiler" not in detail`
shape but its message never contains the phrase either; left untouched as out of this round's
scope.

Re-verified: `ruff check` — clean. `mypy src/fleet/ --strict` — clean, 107 files. `pyright` on the
test file — same 7 pre-existing unrelated errors, zero new. `python -m py_compile` — OK. No
pytest run (per instruction — the orchestrator re-ran the serialized suite), no commit.
