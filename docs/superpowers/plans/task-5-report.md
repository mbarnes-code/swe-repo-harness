# Task 5 report (Worker E)

Status: DONE, ruff and mypy --strict both clean.

**M4 (`_is_shallow` docstring).** TRUE: content-aware, and the "same signal as git" claim
self-contradicted the next sentence. FALSE/unverifiable: "measured across git 2.20.4 → 2.49.1" —
no matrix, no fixture, host git is 2.43.0 (`docs/INTEGRATION_HONESTY.md`). Rewrote to claim only
the measured fact (zero-byte `shallow` → `rev-parse --is-shallow-repository` answers `true`,
pinned by an existing test that calls the real binary) and to state plainly that the
`--unshallow`-removes-the-file claim is standard git behaviour, not version-swept in this repo.

**`clone.py:700-706` false history.** Verified via `git show 44d5550~1` and `git show 44d5550`:
before `44d5550` both classifiers read `timed_out` alone and agreed (both wrongly `TIMEOUT`).
`44d5550` reordered only `buildverify.classify_build_failure`'s branches and added the "meant to
stay in step" comment without touching `clone.py` — manufacturing real disagreement that existed
only between `44d5550` and `68a41ff`, seen by no real wave. Rewrote the comment to state this;
matches the account another worker already corrected in the test docstring.

**M2 (vacuous `retryable` pin).** Empirically verified (direct monkeypatch, no pytest) that the
original `(cloned.failure_class, cloned.retryable) == built == expected` chain is a real test
smell: `built`/`expected` both derive from the same `clock_failure` call so they always move
together, meaning the comparison can never isolate a clone-only regression even though it happens
to still fail on some hypotheticals. Per instructions, changed test logic only — split into
`cloned.failure_class == built[0] == expected[0]` plus a direct `assert cloned.retryable is True`.
`clone._error_for`'s hardcoded `True` is NOT wrong (matches `clock_failure`'s current output on
both branches), so no production change and no stop-and-report was warranted. Confirmed the new
assertion fails when `_error_for` is monkeypatched to return `retryable=False`.

Files changed: `src/fleet/workers/clone.py`, `tests/test_workers_scan.py`.
Test: `test_the_clone_and_build_classifiers_agree_on_every_clock_failure` (assertion + docstring).
