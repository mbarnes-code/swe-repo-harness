# Task H1 report — docs/INTEGRATION_HONESTY.md lane

## Coordinator side-request: D48 citation

Verified independently against the SHA D48's own "Working-tree caveat" pins the entry to
(`8464dc6`), not the working tree or the sibling's `4846fb0` snapshot: at `8464dc6`,
`cli.py:1902-1906` was in fact the correct span (digest computation through the SQL string of
`_open_run`'s `UPDATE runs SET config_digests…`). Two commits that landed after `8464dc6`
(`32365cf`, `e605f0d`) both touched `cli.py` and shifted `_open_run`'s body down five lines, so by
`HEAD` (`2a72f9f`) the `await conn.execute(…)` call is at `cli.py:1907-1910`. This is **line
drift after correct authoring**, not a citation wrong at authoring time (the D47 pattern) — I said
so explicitly in the correction so the distinction isn't lost. Corrected item 2's citation in
place, pinned to `2a72f9f`, and added a short "Post-hoc line-drift correction" note after the
existing "Working-tree caveat" paragraph pointing at it. Nothing else in D48 was touched or
re-verified past its original `8464dc6` baseline.

## Job 1 — D49 status update

Verified every claim in the brief directly against `HEAD` (`2a72f9f`) / `c5ab3b1`:

- `workers/rewrite.py:340-344` — deterministic call site now threads
  `max_bytes=payload.max_patch_bytes`. Confirmed.
- `workers/rewrite.py:678-691` — new `_rejected_patch()` helper runs `check_diff` over every
  `repair.patches` entry; called at `:409-411` before the repair-path `land_patches` at `:430`.
  Traced both origins of `repair` (`RULE_MISS` at `:333-338`, and the caught
  `PatchApplyError`/`GitCommandError` from the deterministic branch's own `land_patches` at
  `:373-378`) and both tiers `_repair()` can answer with (`LLM_REPAIR`/`LLM_ESCALATION`,
  `:491-500`) — all four routes funnel through the same `_repair()` call and the same
  `_rejected_patch()` gate before reaching `land_patches`. No bypass found.
- `grep -n "max_patch_bytes" src/fleet/cli.py` against committed `HEAD` returns **zero hits**,
  confirmed independently (not from working tree, which has another agent's uncommitted `cli.py`
  edits). `TransformInput` (`cli.py:3200-3218`) has no such field; `_rewrite_input` never sets one.
  So `RewriteInput.max_patch_bytes` always takes its own field default (`1_048_576`,
  `workers/rewrite.py:212`) regardless of `settings.config.transform.max_patch_bytes`
  (`settings.py:452`).
- Leg 3 (`_record`, `workers/rewrite.py:567-573`) still appends `unit`, never `edit.path`.
  Untouched by `c5ab3b1`. Confirmed unchanged.
- Both new tests (`tests/test_workers_transform.py`) construct their payload with
  `max_patch_bytes=64`, an explicit override, never the real default. No test asserts acceptance
  at exactly `max_bytes` (`rewrite/apply.py:277-278` uses strict `>`). No test pins that
  `workers/rewrite.py:212`'s default literal still equals `settings.py:452`'s.

Wrote an in-place "Correction (`c5ab3b1`)" section into D49 (house style: prior "three legs" text
kept verbatim as the record; correction appended as a new block before the closing `---`), status
stays **OPEN**. Documented the sharp consequence (rejection message names a setting whose value
was never the one enforced) and the D49/D50 irony (D49's fix landed in D50's own defect shape — an
operator-settable key reaching no code) as instructed.

## Job 2 — new sibling defect

Confirmed the bypass: `workers/relocate.py:48` imports `land_patches` directly; `RelocateWorker.run`
calls it at `:184-194` on a `FilePatch` built from `rename_diff()` (`:84-95`, `:175-182`) with
`check_diff` never imported into the module. `land_patches`'s own docstring
(`workers/rewrite.py:154-156`) documents sharing with `relocate.py` as deliberate.

Traced provenance: the patches are 100% deterministic renames — `unit` comes from
`RelocateInput.sources`, which traces to `TransformInput.sources` (`cli.py:3213`) populated from a
driver-computed `_TransformPlan.sources` (`cli.py:4028`, via `_relocate_input` at `cli.py:3362`).
No LLM call, no `ProposedFileEdit`, anywhere in `relocate.py` (`grep` confirmed empty).

Reached a considered verdict rather than assuming symmetry with D49: neither of `check_diff`'s two
protections transfers correctly here. A byte cap is measuring the wrong dimension (a rename diff's
size is bounded by path length, never content, so `transform.max_patch_bytes` would never fire and
would give false confidence). The subtree-escape check is actively wrong as-is — the destination
(`new_path`) is always inside `dest_path` by construction (`relocated_path()`,
`relocate.py:79-81`), but the *source* leg of every legitimate relocation is expected to sit
outside `dest_path` before the move, so bolting `check_diff(diff, dest_path)` on unmodified would
reject every real relocation. `allow_paths_outside_dest=True` doesn't fix this either — it also
drops `_escapes`'s unconditional `..`/absolute-path check (`rewrite/apply.py:255-262, 285-286`),
so it's all-or-nothing, not scoped.

Filed as **D51 — OPEN, narrowly**: not a D49-shaped cap defect (recorded explicitly as the wrong
fix, so a future pass doesn't bolt on `check_diff` and break every relocation), but there genuinely
is no runtime assertion anywhere that a committed `new_path` lands under `dest_path` — that
guarantee lives entirely in `relocated_path()`'s one line of string concatenation, unverified at
the `land_patches` call site and untested (`tests/test_workers_transform.py`'s four relocate tests,
`:528,557,583,1050`, cover idempotency and interruption, none touch path/size safety). Recorded a
D-entry rather than NEEDS_CONTEXT because the reviewer's premise (a real, previously-undocumented
`check_diff` bypass) is factually correct and worth a permanent note explaining why it should stay
unchecked — the alternative was letting a future pass "fix" it wrongly.

## STATUS

DONE

**D-number assigned:** D51 (new). D48 and D49 corrected in place.

**Commit:** pending — committing `docs/INTEGRATION_HONESTY.md` only, this lane, after this report.

**Concerns:**
- D49's correction and D51 both cite `cli.py` line numbers against committed `HEAD` (`2a72f9f`)
  while `cli.py` itself has another agent's uncommitted edits in the working tree at the time of
  writing — every `cli.py` citation here was taken from `git show HEAD:src/fleet/cli.py`, never
  the working tree, so it should hold even if that agent's edits land differently than expected.
  If `cli.py`'s committed state changes again before this lands, the `max_patch_bytes`
  zero-hits claim and the `cli.py:3213/3362/4028` line numbers should be re-verified.
- D51's verdict ("no cap needed, a narrower invariant check is the real gap") is a judgment call,
  not a mechanical fact-check. I've tried to ground it in code (the construction guarantee in
  `relocated_path()`, the all-or-nothing shape of `allow_paths_outside_dest`) rather than
  assumption, but a second reviewer should sanity-check the reasoning, not just the citations.
