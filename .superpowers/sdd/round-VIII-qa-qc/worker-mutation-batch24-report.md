# Worker report: §15.1 item 3, Wave 6 Batch 24 — spot-check sandbox/worktree.py, bazel/{generators,layout,lockfile,query}.py

## Status: DONE — all 5 files confirmed soundly covered; no gap found, no test manufactured (Rule 2)

Branch: `agent/roundviii-mutation-batch24` (from `main` at `2daff33`)
Worktree: `/home/redmage/swe repo harness worktrees/wt-roundviii-mutation-batch24`
Commit: see bottom of this report (added after this file is written and committed).

## What I did

Read all 5 files in full plus their reaching test files (`tests/test_sandbox.py` for
`sandbox/worktree.py`; `tests/test_bazel.py` for the 4 `bazel/*.py` files — this is the direct
unit-test file for all four, a more precise reaching-test set than the scope report's
`test_build_e2e.py`/`test_cli.py`/`test_workers_build.py`, which reach them only transitively
through end-to-end builds). No prior documented discriminating mutation existed for any of these
5 files in `docs/` or `.superpowers/` (checked before starting, per Rule 12's "re-measure a
routed finding at the moment you act on it" — there was no routed finding to re-measure, only a
reaching-test mapping, so every mutation below is freshly chosen against current code).

For each file: identified the most load-bearing branch, ran a fresh mutation, confirmed the
mutation actually changed the file (`diff` against a pre-mutation backup, non-empty), ran the
reaching test file, read the result for plausibility (targeted failure set vs. an all-fail/all-pass
outage), then restored from backup and confirmed the restore was byte-identical before re-running
the suite clean.

### `sandbox/worktree.py` — load-bearing logic: `remove()`'s D44 ordering guard

`remove()` checks `no_verdict(result)` **before** `result.ok` — an unsettled probe (deadline
already passed, or killed mid-operation) must never be read as a settled git-level refusal that
licenses an `rmtree`. Mutated the guard to a no-op (`if False:` instead of `if reason is not
None:`), bypassing it entirely.

- `diff --no-index` against backup: 1 line changed (non-empty — the mutation landed).
- Result: **3 of 44 failed** — `test_remove_does_not_delete_a_worktree_when_the_probe_never_started`,
  `test_remove_does_not_delete_a_worktree_when_the_probe_is_killed_at_its_deadline`,
  `test_reap_continues_past_a_failed_removal_and_does_not_discard_earlier_work`. Targeted, not a
  module-wide outage (41 other tests in the same file still passed).
- Restored from backup, `diff`: identical. Re-ran `tests/test_sandbox.py`: **44 passed**.

**Verdict: confirmed sound.**

### `bazel/generators.py` — load-bearing logic: `mvs_select`/`reconcile_versions`

First attempted mutation (reported here per Rule 12's "audit mutations for expressibility, not
only pass/fail"): reversed the order `mvs_select`'s `candidates` loop is walked
(`reversed(candidates)`), hypothesising this could pick a different lower-bound candidate. This
mutation was confirmed to have landed (`diff`: 1 line changed) but produced **72/72 passing** — an
all-pass result. Rather than accept that as "sound", I reasoned about why: `combined` is the
intersection of every parsed range, so `combined.lower` already equals the *maximum* of the
candidate lower bounds; among the candidates, only the one whose value equals that maximum can
ever satisfy `combined.contains(version)`. With at most one candidate ever able to satisfy the
loop's condition, iterating forward vs. reversed reaches the same answer by construction — the
mutation is **not expressible** against this algorithm's invariant, not a coverage gap. Restored
and discarded.

Second mutation, on the property the module's own docstring calls out as load-bearing
("Collected, not raised, because one unsatisfiable coordinate must not hide the other four"):
removed `reconcile_versions`'s `try`/`except VersionConflict` collection, letting the first
conflict raise and abort the whole reconciliation instead of being collected into
`VersionResolution.conflicts`.

- `diff --no-index` against backup: 4 lines changed (non-empty).
- Result: **1 of 72 failed** — `test_reconcile_collects_every_conflict_and_still_resolves_the_rest`
  (the exact test built for this property; the traceback shows the mutated code raising
  `VersionConflict` straight out of `reconcile_versions` instead of collecting it). 71 other tests
  passed — targeted, not an outage.
- Restored from backup, `diff`: identical. Re-ran `tests/test_bazel.py`: **72 passed**.

**Verdict: confirmed sound** (with one non-expressible mutation disclosed rather than silently
discarded).

### `bazel/layout.py` — load-bearing logic: `is_reserved_dest`

Mutated the `_scc` reserved-segment check from an exact-segment membership test
(`RESERVED_SCC_SEGMENT in dest.split("/")`) to a substring test (`RESERVED_SCC_SEGMENT in dest`) —
a plausible drift bug that would over-match names merely containing `_scc` as a substring.

- `diff --no-index` against backup: 1 line changed (non-empty).
- Result: **1 of 72 failed** —
  `test_scc_is_a_reserved_path_segment_under_every_monorepo_dir`, specifically on
  `assert not is_reserved_dest("java/com/acme/_scc_helper")` (the exact negative case the test
  carries for this distinction). 71 other tests passed.
- Restored from backup, `diff`: identical. Re-ran `tests/test_bazel.py`: **72 passed**.

**Verdict: confirmed sound.**

### `bazel/query.py` — load-bearing logic: `RdepsClosure.equivalence`

Mutated the `equivalence` property to invert its condition
(`CLOSURE_SAMPLED if not self.truncated else FULL`) — the disclosure property §3.4/§3.5.1 exists
for (a truncated/sampled closure must never report as `FULL`).

- `diff --no-index` against backup: 1 line changed (non-empty).
- Result: **5 of 72 failed** — `test_a_closure_under_the_bound_is_tested_whole`,
  `test_a_capped_closure_is_disclosed_not_silent`,
  `test_a_truncated_closure_forces_closure_sampled_on_the_report`,
  `test_rdeps_closure_builds_the_argv_and_skips_the_depth_query_when_it_can`,
  `test_closure_defaults_match_the_bounds_table` — every failure directly asserts on `.equivalence`
  or `.truncated`. 67 other tests passed — targeted, not an outage.
- Restored from backup, `diff`: identical. Re-ran `tests/test_bazel.py`: **72 passed**.

**Verdict: confirmed sound.**

### `bazel/lockfile.py` — load-bearing logic: `check_lock_registry`'s host comparison

Mutated the foreign-host filter from `!= expected` to `== expected` — inverting which URLs are
flagged as foreign, so a lock keyed by the *matching* registry would be refused and a
mirror-keyed lock would be silently accepted (exactly the failure mode the module's docstring
describes at length: a lock that looks offline-ready but silently isn't).

- `diff --no-index` against backup: 1 line changed (non-empty).
- Result: **3 of 72 failed** —
  `test_a_lockfile_keyed_by_the_registry_the_container_contacts_is_accepted`,
  `test_a_mirror_keyed_lockfile_is_refused_and_the_message_states_the_offline_consequence`,
  `test_the_archive_urls_a_lockfile_holds_as_VALUES_are_not_the_subject` — all three failures are
  directly on `check_lock_registry`'s pass/refuse behavior. 69 other tests passed.
- Restored from backup, `diff`: identical. Re-ran `tests/test_bazel.py`: **72 passed**.

**Verdict: confirmed sound.**

## Final verification

- All 5 files re-diffed against their pre-mutation backups after every restore: byte-identical in
  every case. `git status --short` in the worktree shows no modified files in `src/`.
- Full re-run of both reaching test files together: `tests/test_bazel.py tests/test_sandbox.py`
  → **116 passed** (72 + 44).
- `.venv/bin/python -m mypy` (no path args, full `mypy_path`/`packages` scope):
  `Success: no issues found in 132 source files`.
- `.venv/bin/python -m ruff check` on all 5 target files: `All checks passed!`

## Files touched

None in `src/` or `tests/` — every mutation was applied, verified discriminating (or, in the one
non-expressible case, verified non-discriminating for a structural reason and discarded), then
restored byte-identical. Only this report file and the git commit recording it are new.

## Per-file spot-check result

| File | Result |
|---|---|
| `src/fleet/sandbox/worktree.py` | Confirmed sound. Fresh mutation on `remove()`'s D44 no-verdict-before-ok guard; discriminating (3/44 failed), restored clean. |
| `src/fleet/bazel/generators.py` | Confirmed sound. First mutation (reversed MVS candidate order) proved non-expressible by the algorithm's own structure — disclosed rather than discarded silently. Second mutation on `reconcile_versions`'s collect-not-raise property was discriminating (1/72 failed, the exact test built for it), restored clean. |
| `src/fleet/bazel/layout.py` | Confirmed sound. Fresh mutation on `is_reserved_dest`'s exact-segment check; discriminating (1/72 failed), restored clean. |
| `src/fleet/bazel/query.py` | Confirmed sound. Fresh mutation inverting `RdepsClosure.equivalence`; discriminating (5/72 failed), restored clean. |
| `src/fleet/bazel/lockfile.py` | Confirmed sound. Fresh mutation inverting `check_lock_registry`'s host filter; discriminating (3/72 failed), restored clean. |

Do not merge, do not push.

## Correction (2026-09-14, post-review, appended per CLAUDE.md's "annotate, never rewrite")

**This does not overturn the report's "confirmed sound" verdict for `bazel/generators.py`, nor
any other conclusion in this report — only one stated *reasoning* sentence below was found false
by review, not by me.**

The original text above (§ `bazel/generators.py`) claims:

> `combined.lower` already equals the maximum of the candidate lower bounds; among the
> candidates, only the one whose value equals that maximum can ever satisfy
> `combined.contains(version)`. With at most one candidate ever able to satisfy the loop's
> condition, iterating forward vs. reversed reaches the same answer by construction — the
> mutation is **not expressible** against this algorithm's invariant, not a coverage gap.

The reviewer (`.superpowers/sdd/round-VIII-qa-qc/review-mutation-batch24-report.md`) constructed
a real counter-example, verified by direct execution against `parse_range`/`VersionRange`: two
requirements `>=31` and `>=31.0` on the same coordinate. `candidates` is a `set` of `(rng.lower,
rng.lower_text)` pairs, deduplicated on the *pair*, not on numeric value — so `"31"` (parsing to
`(31,)`) and `"31.0"` (parsing to `(31, 0)`) are two distinct candidate entries that are **both**
numerically maximal under the padded `_cmp` comparison `contains()` uses. Both pass
`combined.contains(version) and all(rng.contains(version) for ...)`, so forward order picks `"31"`
and reversed order picks `"31.0"` — a real, reproducible divergence. My "at most one candidate can
ever satisfy the loop's condition" claim is therefore false as a general statement about
`mvs_select`; the correct scope, per the reviewer, is "at most one **value** can satisfy it, but
ties on that value between differently-precision-spelled requirements are possible and undecided
by any documented tie-break rule."

**Why this doesn't change the file's verdict:** the reviewer judged the consequence cosmetic (both
spellings denote the same version to Bazel/BCR; no existing `tests/test_bazel.py` fixture
constructs the tie) and recommended noting the gap rather than requiring a fix in this batch. My
overall handling — not accepting the resulting 72/72 all-pass as proof of soundness, and instead
running a second, genuinely discriminating mutation (`reconcile_versions`'s collect-not-raise
property, 1/72 failed) — was assessed as the correct response to an inconclusive result, regardless
of the flawed reasoning I gave for why it was inconclusive.

**Caught by review, not by me.** I did not verify the "at most one candidate" claim by
constructing a tie case before writing it; I reasoned about the single-lower-bound case and did
not consider that `candidates` dedupes on the `(lower, lower_text)` pair rather than on `lower`
alone. Flagging per CLAUDE.md's measurement discipline: an unmeasured reasoning claim, even one
that only supports a conclusion later found correct on other grounds, is not itself validated by
that conclusion, and is corrected here rather than left standing.
