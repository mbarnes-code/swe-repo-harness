# Task L1 report — C1/C2 from review-36.md (buildverify.py / base.py)

## Headline: both findings were already stale (fixed) before this task started

**C1** (`buildverify.py`'s `_DOCKER_CANNOT_RUN_EXPLAINED` operator message asserting unconditional
"no attempt charged, no repair prompted") — **already fixed**, landed in commit `32365cf`
(checkpoint 37). `grep -n "no attempt charged, no repair prompted" src/fleet/workers/buildverify.py`
→ zero hits. The current string already states the `max_transient_retries` (4) bound, the
post-cap charge, and the rung-2/3 diagnosis exposure — confirmed by reading `buildverify.py:474-493`
directly and cross-checked against `tests/test_workers_build.py`'s
`test_the_docker_cannot_run_message_bounds_its_free_retry_claim`, which already pins the corrected
wording in past tense ("used to say ... unconditionally").

**C2** (`base.py`'s `clock_failure` docstring bullet re-asserting the same unconditional claim) —
the specific quoted bullet was **already fixed**, landed in commit `8464dc6` (checkpoint 36):
`base.py:163-171` already states the `max_transient_retries` bound and the "bounded reprieve, not a
standing exemption" correction.

However, C2's cited span (`base.py:155-186`) includes more than the quoted bullet, and one part of
it was **still live**: the docstring's closing claim **"This function exists so the answer is
written down once"** (base.py:180) plus the false-history sentence right after it, both untouched
since `8464dc6`. This is review-36's I1 and I4, not literally C1/C2, but it sits inside C2's own
"Where" span and inside the exact function the task's guidance paragraph warned about. I fixed it:

1. **"written down once" overstated.** Post-ADR-0067, `util.proc.no_verdict` is a second,
   deliberate decoder of the identical `(started, timed_out)` flags (for a reason string, not a
   `FailureClass`) — confirmed live at `util/proc.py:132-158`, delegated to by `clone._no_verdict`
   at 5 call sites. `rewrite/astgrep.py:198` still hand-orders the same two flags inline, a third
   decoder. The rewritten docstring says `clock_failure` is the single owner of the `FailureClass`
   half only, names the sibling decoders, states they must not be merged (per this task's explicit
   instruction), and is careful not to claim the whole invariant is written down once.
2. **False history.** The old text framed "clone discarded `started`... and answered `TIMEOUT` for
   both" as a pre-existing condition `clock_failure` fixed. Verified against `git show
   44d5550~1:src/fleet/workers/{buildverify,clone}.py`: both already answered `TIMEOUT` for the
   never-started shape before `44d5550` — wrong, but in step. `44d5550` reordered only
   `buildverify`'s branches and, in the same hunk, added the false "meant to stay in step" comment
   without touching `clone.py`, manufacturing the divergence the comment denied. This account
   already exists correctly in `clone.py:680-694` and in
   `tests/test_workers_scan.py`'s `test_the_clone_and_build_classifiers_agree_on_every_clock_failure`
   docstring (both landed in `8464dc6`) — the rewritten `base.py` docstring now matches them.
3. **Same false history was duplicated as an inline comment** in `buildverify.py:424-428`
   (`classify_build_failure`, the caller), with the added overclaim "drawn in exactly one place."
   Corrected identically, scoped to "the FailureClass decision," which is accurate — `clock_failure`
   genuinely is the sole decoder of that half.

No test asserts on the docstring/comment text itself (`grep -rn "written down once" tests/` → no
hits), so no test changes were needed. `tests/test_workers_build.py:2586` and
`tests/test_workers_scan.py:729` were checked and are already accurate (they describe
`clone._no_verdict`'s ordering and the corrected divergence history respectively) — left untouched.

## Files changed
- `src/fleet/workers/base.py` — `clock_failure` docstring (lines ~180-198)
- `src/fleet/workers/buildverify.py` — inline comment in `classify_build_failure` (lines ~423-431)

No test files were modified (none needed correction).

## Verification
- `ruff check src/fleet/workers/buildverify.py src/fleet/workers/base.py` → All checks passed.
- `mypy --strict src/fleet` → Success: no issues found in 107 source files (baseline held).
- `pytest tests/test_workers_build.py tests/test_workers_base.py tests/test_workers_scan.py -q` →
  **141 passed**, 0 skipped, clean "bazel disk" line.
- No `FLEET_*` env vars exported at any point.

## STATUS: DONE

Commit: (see `git log -1`)

Test summary: 141 passed, 0 failed, 0 skipped (`test_workers_build.py` + `test_workers_base.py` +
`test_workers_scan.py`).

**Both review-36 findings, as literally quoted, were already stale** — C1 fully fixed in `32365cf`,
C2's quoted bullet fixed in `8464dc6`. The only live defect remaining under C2's citation span was
the adjacent "written down once" / false-history text in the same docstring (review-36's I1/I4),
which this task fixed along with its duplicate in `buildverify.py`'s inline comment.
