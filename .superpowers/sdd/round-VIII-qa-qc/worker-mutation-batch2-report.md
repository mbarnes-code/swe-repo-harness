# Worker report: §15.1 item 3, Wave 1 Batch 2 — mutation-proof `llm/backends/openai_compatible.py` + `llm/failover.py`

## Scope

Batch 2 of the mutation-audit sweep (`worker-mutation-scope-report.md`): both files were touched
by the `334edeb` code-review fix commit and were bucket B (reaching tests, no CONFIRMED
mutation-proof evidence).

`334edeb` made two behavior fixes in this batch:

- `src/fleet/llm/backends/openai_compatible.py::_SdkTransport.__call__`: an `APIStatusError` with
  a non-5xx, non-429 status code (400/401/404/etc — "our request is wrong") used to be raised as
  `TransportError(..., trigger="CONNECTION")`, walking the whole failover tier for a permanent
  config error. Fixed to raise a plain `LlmError` (not a `TransportError` subclass), which is
  terminal and does not trigger failover.
- `src/fleet/llm/failover.py::BackendHealth.record_failure`: the `UP -> DOWN` guard
  `if state.consecutive_failures >= self.open_after_failures:` used to fire on ANY qualifying
  failure once past threshold, including one delivered while the target was ALREADY `DOWN` (a
  late-arriving concurrent in-flight call — `may_call` only blocks NEW calls, not calls already in
  flight when the trip happened). That pushed `down_since` forward every time, so a sustained
  trickle of late failures could keep the cooldown from ever elapsing. Fixed by adding
  `and state.state != "DOWN"` to the guard, so only the `UP -> DOWN` transition (re)starts the
  cooldown clock.

## File 1: `src/fleet/llm/backends/openai_compatible.py`

**Existing test already reaches and discriminates the fix — no new test needed.**

- Test: `tests/test_llm_backend_openai_compatible.py::test_a_bad_request_fails_the_task_instead_of_walking_the_tier`
  (parametrized `status in [400, 401, 404]`). Asserts `pytest.raises(LlmError)` and
  `not isinstance(excinfo.value, TransportError)`.

**Mutation applied** (reverts the exact fix): re-added the `FailoverTrigger` import and replaced
the current `if exc.status_code >= 500: ... raise LlmError(...)` block with the pre-fix form:

```python
except APIStatusError as exc:
    trigger: FailoverTrigger = (
        "SERVER_ERROR" if exc.status_code >= 500 else "CONNECTION"
    )
    raise TransportError(
        f"{base_url}: HTTP {exc.status_code}: {exc}",
        trigger=trigger,
    ) from exc
```

**Discipline followed:** backed up to
`/tmp/claude-.../scratchpad/batch2-backups/openai_compatible.py.bak` → edited → diffed against the
backup (`git diff --numstat`, non-empty: `8 12`) → ran tests → restored from the backup → diffed
again (`diff -u` reported `IDENTICAL`, zero lines) → re-ran tests to confirm the original green
state returned.

**Result:**
- Original (pre-mutation): `tests/test_llm_backend_openai_compatible.py` — 53 passed.
- Mutant: same file — **3 failed** (`test_a_bad_request_fails_the_task_instead_of_walking_the_tier[400]`,
  `[401]`, `[404]`), **50 passed** (no other test in the file was affected — clean blast radius,
  confirming this is a targeted discriminator, not a module-wide break).
  Failure evidence (400 case):
  `assert not isinstance(excinfo.value, TransportError)` → `assert not True` (the mutant raises
  `TransportError`, the fix raises plain `LlmError`).
- Restored: `git diff` against the pre-mutation backup is empty; full file suite re-run — 53
  passed.

**Conclusion:** CONFIRMED mutation-proof. `test_a_bad_request_fails_the_task_instead_of_walking_the_tier`
is the discriminator; old (pre-fix) behavior fails it, current behavior passes it.

## File 2: `src/fleet/llm/failover.py`

**No existing test reaches this path — wrote one.**

Every existing `tests/test_llm_failover.py` test drives the scenario through
`LadderModelClient.complete()` / `FakeBackend`, and `may_call` unconditionally skips (never
dispatches) a call to an already-`DOWN` target, so `record_failure` is never invoked a second time
on a `DOWN` target through that path. The fixed line only matters for a late-arriving qualifying
failure from a call that started *before* the target tripped (a concurrent in-flight call) — the
module's own docstring names this scenario explicitly. This requires driving `BackendHealth`
directly, bypassing `may_call`.

**New test:**
`tests/test_llm_failover.py::test_a_late_arriving_failure_on_an_already_down_target_does_not_push_down_since_forward`

Constructs `BackendHealth(open_after_failures=1, cooldown_s=10.0, clock=<MutableClock>,
on_transition=transitions.append)` directly, then:
1. `record_failure` at t=0 → `UP -> DOWN` (one transition recorded).
2. Advance clock to t=5, `record_failure` again (simulating the late-arriving concurrent failure)
   → asserts still exactly one transition recorded (no second `DOWN` transition emitted).
3. Advance clock to t=11 (11s since t=0 ≥ cooldown 10.0s, but only 6s since t=5 < cooldown) →
   asserts `may_call` returns `True` — a purely behavioral proof that `down_since` was not reset to
   5.0, since a reset would keep the cooldown unexpired at t=11.

This is the same behavioral-proof style already used in this file
(`test_schema_unsatisfied_during_a_half_open_probe_abandons_it_to_down_not_wedged`'s "the actual
proof it is not wedged" comment) rather than reaching into `BackendHealth`'s private `_targets`
dict.

**Mutation applied** (reverts the exact fix): changed

```python
if state.consecutive_failures >= self.open_after_failures and state.state != "DOWN":
```

back to

```python
if state.consecutive_failures >= self.open_after_failures:
```

**Discipline followed:** backed up to `.../batch2-backups/failover.py.bak` → edited → diffed
against the backup (non-empty: `1 1`) → ran tests → restored → diffed again (`IDENTICAL`) → re-ran
tests to confirm the original green state returned.

**Result:**
- Original (pre-mutation): `tests/test_llm_failover.py` — 14 passed (13 pre-existing + 1 new).
- Mutant: same file — **1 failed** (the new test), **13 passed** (every pre-existing test in the
  file stayed green — confirms the mutation only affects the late-arriving-failure-while-DOWN path,
  which no pre-existing test reaches, and rules out a module-wide/import-level break).
  Failure evidence: `assert ['DOWN', 'DOWN'] == ['DOWN']` at the second-transition assertion — the
  mutant emits a second `DOWN -> DOWN` transition and (per the reasoning that a second
  `_transition` call updates `down_since`) resets the cooldown clock, matching the defect
  `334edeb` fixed.
- Restored: `git diff` against the pre-mutation backup is empty; full file suite re-run — 14
  passed.

**Conclusion:** CONFIRMED mutation-proof.
`test_a_late_arriving_failure_on_an_already_down_target_does_not_push_down_since_forward` is the
discriminator; old (pre-fix) behavior fails it, current behavior passes it.

## Verification

- `.venv/bin/python -m pytest tests/test_llm_failover.py tests/test_llm_backend_openai_compatible.py -q`
  → **67 passed** on the final (post-restore, plus new test) tree.
- `.venv/bin/python -m ruff check tests/test_llm_failover.py` → All checks passed.
- `.venv/bin/python -m ruff format --check tests/test_llm_failover.py` → already formatted.
- `.venv/bin/python -m mypy tests/test_llm_failover.py` → Success: no issues found in 1 source
  file. (Disclosed scoping: `pyproject.toml`'s `[tool.mypy]` strict block scopes to
  `packages = ["fleet"]`, i.e. `src/`, not `tests/`; this run passed the test file explicitly as an
  extra check beyond that configured scope. No `src/` files were changed in this batch — both
  source files are byte-identical to `main` after the mutation-and-restore cycles, confirmed by
  `git status --short` showing only `tests/test_llm_failover.py` modified.)

## Reproduction

From the worktree at `agent/roundviii-mutation-batch2`:

```bash
cd "/home/redmage/swe repo harness worktrees/wt-roundviii-mutation-batch2"
.venv/bin/python -m pytest tests/test_llm_failover.py tests/test_llm_backend_openai_compatible.py -q
```

To re-run either mutation: apply the diff shown above to the relevant file, run the named test(s),
observe the failure, then `git checkout -- <file>` to restore.

## Files changed

- `tests/test_llm_failover.py` — added one new test (see above) plus a one-name import addition
  (`BackendHealth`).
- No `src/` changes (both fixes already correct on `main`; this batch only adds proof).
