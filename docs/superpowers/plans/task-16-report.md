# Task 16 report — SPEC.md truncation-description correction

## Status: DONE

Read `src/fleet/util/proc.py` first (not paraphrased from the task prompt). Actual behaviour:
`_read_head_and_tail` caps capture at `LOG_TAIL_BYTES` (32_768) total, split as the first
`HEAD_BYTES` (8_192) of the stream plus the remainder of the budget from the END, with an elision
marker between the two slices when the file exceeds both combined. `HEAD_BYTES`'s own docstring
(proc.py:200-212) cites the measurement: a real 79,337-byte `--keep_going` failure log, tail-only
kept 41.3% of bytes / 37.7% of `error[...]` headers and dropped the first (root-cause) target's
diagnostics while keeping the closing summary.

## SPEC.md locations changed

1. **§11.3, line ~6638** (the one named in the task): rewrote "tail-truncated to 32 KiB... never
   re-expanded" to describe the head-and-tail split, cite the measurement, and state
   `HEAD_BYTES = 8_192` as an **unmeasured Agent Recommendation**, not derived from the
   measurement — matching `docs/superpowers/plans/task-15-report.md`'s own labelling.
2. **Directory-tree comment for `util/proc.py`** (~line 5827): "tail truncation" → "head+tail
   truncation".
3. **Risk table row 8** (~line 7047, Memory bloat row): "tail-truncated subprocess output" →
   "head-and-tail-bounded subprocess output (§11.3)".

Left unchanged (checked, not stale): `models/base.py::_truncate_tail` / `TruncatedStr` (SPEC
~line 1922 code block, and the two field descriptions at ~3031 and ~3363 that explicitly point to
`TruncatedStr`) — that validator is a separate, still-genuinely-tail-only guard, unchanged by this
work; conflating it with `proc.py`'s capture split would itself be inaccurate.

## Pre-existing false claim (separate defect, reported not folded in)

Before this change, SPEC already implied a *strict* 32 KiB cap ("tail-truncated to 32 KiB... never
re-expanded"), but per `task-15-report.md` the old `_read_tail` could itself produce output over
budget (~32,793 bytes, stacked `[truncated]` markers) that a second `TruncatedStr` validation
downstream would silently double-truncate — a defect independent of the tail-only-vs-head+tail
question, fixed in the same worker pass. SPEC's "never re-expanded / strictly 32 KiB" framing was
therefore already false pre-change for a different reason; I did not need to touch wording for it
since the underlying bug is now fixed, but flagging per instructions.

## HEAD_BYTES status as stated in SPEC

Explicitly labelled "unmeasured Agent Recommendation... not itself derived from the measurement" —
the head/tail *split* is measurement-justified; the specific byte count is not.
