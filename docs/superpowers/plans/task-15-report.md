# Task 15 report — `util/proc.py` truncation fix (research-38 Q3)

## Status: DONE

## Double-truncation defect: CONFIRMED, and fixed

Reproduced independently before touching code (not taken on faith): `_read_tail`'s old
implementation returned `window(<=32768B) + "\n[truncated N bytes]"`, i.e. up to ~32,793 bytes —
over `LOG_TAIL_BYTES`. Any consumer that puts that string into a `TruncatedStr` field re-validates
it — confirmed the real path, `buildverify.error_from_proc` → `WorkerError.stderr_tail`
(`TruncatedStr`) — which silently re-truncated and stacked a second `[truncated N bytes]` marker,
eating bytes off the front of the first marker. Reproduced with a 135,200-byte synthetic stderr:
proc tail was 32,793 B; wrapping it in `WorkerError` produced two `[truncated` markers.

Fix: `_read_head_and_tail` (renamed from `_read_tail`) now computes the elision marker's
worst-case width from `size` up front and reserves that room inside the `LOG_TAIL_BYTES` budget
in one pass, so its return value (content + every marker) never exceeds `LOG_TAIL_BYTES`. Verified:
same 135 KB repro now produces exactly one marker end-to-end, through `WorkerError`.

## Head/tail decision: implemented head+tail split

Argued for by the research's own measurement, not kept out of inertia: under `bazel --keep_going`,
the first failing target is ordinarily root cause and later ones are cascade; a tail-only window
was measured discarding the first target's diagnostics while keeping the closing summary — the
consequence, not the cause. `_read_head_and_tail` now keeps `HEAD_BYTES=8192` (Agent
Recommendation — 25% of the 32 KiB budget, unmeasured tuning) from the start plus the remainder
from the end, elision marker in between, still capped at `LOG_TAIL_BYTES` total — the token
ceiling is unchanged, only the mix. Change is self-contained to `util/proc.py`; no other owned
file needed edits.

**Flag for docs/ owner:** SPEC §11.3 (`docs/SPEC.md:6638`) says output is "tail-truncated to 32
KiB" — now only half true. Left a note in `proc.py`'s module docstring; SPEC wording needs an
owner's edit.

## Tests (in `tests/test_proc.py`)

- `test_first_targets_diagnostics_survive_a_large_multitarget_failure_log` — realistic 2-target
  Bazel-shaped log (real rustc error text from the research doc, 150-module cascade, closing
  summary); asserts first-target error text and summary both survive, a marker buried in the
  cascade does not.
- `test_head_and_tail_split_never_double_truncates_through_workererror` — regression test for the
  defect, run through the actual `WorkerError(stderr_tail=...)` consumer path on the same
  realistic log.
- `test_head_bytes_is_smaller_than_the_total_budget` — sanity bound on the new constant.
- Updated `test_huge_output_is_tail_bounded_but_fully_on_disk`: bound tightened from `< LOG_TAIL_BYTES + 128` to `<= LOG_TAIL_BYTES`; marker check updated to `"bytes elided"`.

All `test_proc.py` tests pass (run individually via direct `asyncio` invocation, not `pytest`).
`ruff check src/ tests/` and `mypy src/fleet/ --strict` both clean. `is_producible_shape` and its
anti-drift test untouched.
