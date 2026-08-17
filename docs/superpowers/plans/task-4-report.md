# Worker D — Task 4 report

**Status: complete.** All assigned findings (C3, I1, I3, I4, M1, M2, M3, M4) verified directly
against the tree at `68a41ff` (grep/sed, `git show 44d5550~1`, `git show 44d5550 --stat`) before
any doc was edited, and PROGRESS §36 added.

**C3 — verified myself; all four claims were absent at `68a41ff`, none implemented:**
1. `ProbeIndeterminateError(RuntimeError)` in `rewrite/rules.py` — zero hits, doesn't exist.
2. Second `except` arm in `cli._transform_criterion` — still one arm (`cli.py:4163`).
3. `break`→`continue` + dedupe — still `break` (`cli.py:4165`).
4. `no_verdict()` in `util/proc.py` + thin `clone._no_verdict` delegator — zero hits in `proc.py`; `clone.py:199` still the full implementation.

Fixed ADR-0067 in `docs/DECISIONS.md`: added a "Status: DECIDED, NOT YET IMPLEMENTED" block with
citations, cross-referenced to D37 (already tracks this gap accurately, OPEN — no duplicate
ledger entry needed), and converted the ADR-0047 amendment's indicative claims to future tense.

**Files changed:**
- `docs/DECISIONS.md` — ADR-0067 status block + I1 tension note; ADR-0047 amendment tense fix.
- `docs/INTEGRATION_HONESTY.md` — new `D46` entry (I3: D34's fix guarantees deterministic
  container-name-collision 125s across all 4 free retries), marked OPEN, noting the apparent
  (uncommitted, unverified) fix visible in the working tree.
- `docs/PROGRESS.md` — new §36 covering this wave and its root-cause diagnosis.
- `tests/test_workers_build.py` — M1: renamed
  `test_a_daemon_blip_costs_the_repo_no_attempt_and_reaches_no_human` →
  `test_a_single_daemon_blip_costs_the_repo_no_attempt`, narrowed docstring/assertion message to
  its actual `blips=1` scope. No assertion logic changed.
- `tests/test_workers_scan.py` — M2: added a docstring note that `retryable` is vacuously pinned
  on clone's side (assertion unchanged). I4: corrected the same test's docstring, which repeated
  the false history (verified via `44d5550~1`: the two classifiers agreed pre-`44d5550`; `44d5550`
  reordered only buildverify and added the "meant to stay in step" comment without touching
  `clone.py`, so the comment was false the instant it was committed, not inherited).

**Left for a code-owning worker (outside my file grant):**
- C1/C2 (`buildverify.py` operator string, `base.py` docstring) — still assert the retracted
  unconditional "no attempt charged."
- I1/I4's remaining occurrences in `base.py:161-175` and `clone.py:700-706`.
- I2, I5 (test fixture, `cli.py:3634`).
- M4 (`clone.py:723-745` `_is_shallow` docstring — self-contradicts and overstates its version range).
- M1's missing `blips=5` sibling test; M2's actual `clone._error_for` fix or assertion narrowing —
  both require test-logic changes I was told to report, not make.
