# Task 12 report — review-38 C2, C3, I6, I7

Status: all four findings addressed. `ruff check src/ tests/` and
`mypy src/fleet/ --strict` both clean. `pytest` not run (per constraint).

## C3 verdict — `on_cancel` does run, but not for the leak the docstring named

Traced `base.py:864-905` (`_run_one`) and `orchestrator/runner.py:794-818`
(`_heartbeat`). `on_cancel` IS wired at `base.py:887` and fires in production
on a real trigger: `_heartbeat`'s `LeaseStolenError` branch sets `ctx.cancel`,
and if `run()` doesn't settle within `cancel_grace_s`, `on_cancel` runs.
That is not dead code.

What genuinely never ran: the docstring's specific fallback for a *plain*
deadline timeout — "it waits for `ContainerSandbox.reap` at `fleet resume`."
Traced `util/proc.py:322-336`: a plain timeout resolves *inside*
`util.proc.run` (`asyncio.timeout_at` kills the `docker run` client), so
`run_task` completes on its own before `_run_one`'s outer watchdog ever
fires — `on_cancel` is never reached for this case. The claimed fallback was
doubly dead: `grep -rn '\.reap(' src/` → zero hits, and `fleet resume` is
`_unavailable` (`cli.py:9792`). Reviewer's C3 claim confirmed accurate.

Fix (not just docstring): extracted the prefix-sweep into
`_sweep_containers`, called from `on_cancel` as before, and now also called
directly from `run()` and `_c_toolchain_gate` at the two points a
`docker run` child can be killed at its own deadline (`result.timed_out and
result.started`). The leak is closed without touching `cli.py`/`sandbox/`.
Docstring rewritten to state this, not the broken reap/resume claim.

## C2 — `_DOCKER_CANNOT_RUN_EXPLAINED` (`buildverify.py:474-489`)

Verified against the sibling probe message (`:845-852`, `f12a954`) and the
inline comments at `:441-445`/`:450-451`. Rewrote to: several distinct
conditions all exit 125, exit code can't distinguish them (stderr can), name
collision is the one the harness can act on (residual/external only, per
`_invocation_name`), unreachable daemon is not among them (exits 1,
measured), and a recurring 125 is "charged a rung like any other failure" —
removed the self-contradicting "daemon still gone" sentence.

## I6 — `tests/test_workers_build.py`

`test_a_probe_the_fleets_own_deadline_killed_is_not_reported_as_a_missing_compiler`
docstring bounded: "retried on the same rung with no attempt charged, up to
`RetryPolicy.max_transient_retries` (4); past that cap the identical clock
failure is charged like any other. This test drives one occurrence, so it
pins the free half only."

## I7 — `tests/test_llm_cache.py:366-383`

Confirmed the reviewer's claim: no worker is constructed, `buildverify.py`'s
real `except LlmError:` never executes. Narrowed
`test_cache_miss_is_not_an_llm_error`'s docstring to its actual scope (class
hierarchy + isolated reproduction) and added the real regression to
`tests/test_workers_build.py`:
`test_a_read_only_cache_miss_during_diagnosis_is_not_swallowed_by_the_advisory_catch`
— drives `BuildverifyWorker.run()` to a rung-2 build failure with a fake
`ctx.llm` (`_CacheMissModelClient`) shaped like a read-only cache miss, and
asserts `CacheMiss` propagates out of `run()` uncaught.

Files touched: `src/fleet/workers/buildverify.py`,
`tests/test_workers_build.py`, `tests/test_llm_cache.py`.
