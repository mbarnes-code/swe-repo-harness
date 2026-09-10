# Task 113 report — §12.11 Leg E: strengthen the fixture, close D116, delete the xfail

Round VI, thirty-ninth wave. Branch `agent/roundvi-task113`, off `main` at `947983e`. Worktree:
`/tmp/claude-1000/-home-redmage-swe-repo-harness/168ce0b2-2e48-46a6-837c-4cb29f1a8475/scratchpad/task113/wt`.

## Step 0/1 — what was actually true, measured directly

Ran `tests/test_baseline_ok_exclusion.py` as-is on current `main` first: **1 passed, 1 xfailed**
(the discriminator test's own comment already recorded, and this run confirmed, that only
`acme-empty` is `baseline_ok IS NULL`; the other four non-empty fixture repos already get a real
0-or-1 value from Legs B/C). A throwaway probe test (deleted before finalizing) confirmed the
exact per-repo values via a real `fleet scan`:

| repo | `baseline_ok` | `baseline_test_count` | finding |
|---|---|---|---|
| `acme-app-py` | 0 | 0 | `BaselineRed` |
| `acme-app-ts` | 0 | 0 | `BaselineRed` |
| `acme-lib-ts` | 0 | 0 | `BaselineRed` |
| `acme-lib-py` | 1 | 0 | (none) |
| `acme-empty` | NULL | 0 | `EmptyRepo` |

and, via a real `fleet sequence`: `excluded = [acme-app-py, acme-app-ts, acme-empty, acme-lib-ts]`,
`wave_index_by_repo = {acme-lib-py: 0}` — exit 0.

**Step 0's own question, answered directly:** the xfail's literal `_baseline_ok_exclusion_set(fleet)
== []` assertion was indeed stale relative to ADR-0135's already-landed narrowing (`docs/SPEC.md`'s
dated marker at §12.11's last sentence): the narrowed text permits `acme-empty`'s NULL (it matches
§3.1(c)'s `EmptyRepo`/preflight-gated exemption), so `== []` could never pass on this fixture by
construction, exactly ADR-0135's own point. The assertion needed replacing with the narrowed claim
— every `baseline_ok IS NULL` repo must be exempt, not that the set is literally empty — **before**
asking whether it passes, per the brief's own instruction.

## Step 2 — the fixture, strengthened (the load-bearing requirement)

Before this task, every one of the four non-empty fixture repos had zero native test files, so
`baseline_test_count` was 0 even for the one genuinely green repo (`acme-lib-py`) — a mechanism
that always answers "green, 0 tests" would have passed any assertion here vacuously, the exact
"green-and-empty" failure §12.11 exists to catch, one level up (research-53's own finding,
reproduced directly above).

**Real native tests (green case).** Added one real, pytest-discoverable test file
(`test_normalize.py`, `from acme_lib_py import normalize; def test_normalize_lowercases...`)
directly to THIS test's own git clone of `acme-lib-py`'s source repo — via a new
`_add_real_native_test` helper writing to `tmp_path / "sources" / "acme-lib-py"` (the exact path
`tests.test_transform_e2e.fleet`'s own `_make_repo` call already built it at) and committing a
second, real git commit there, **never** to the shared `tests.test_scan_e2e.FIXTURE_REPOS` dict.
Chose this over editing the shared dict because `tmp_path` is unique per test function: editing
the shared dict would reach `acme-lib-py`'s content in **every** test file that also builds its own
repos from it — measured directly, 251 references across 11 other files (`test_pr_e2e.py`,
`test_hoist_rollback_wiring.py`, `test_stub_resolution_task79.py`, `test_cli.py`,
`test_workers_build.py`, `test_baseline_container.py`, `test_heavy_tier_outage_e2e.py`, among
others), several of which derive exact per-repo file/commit counts from that dict
(`test_transform_e2e.py::test_transform_lands_one_trailered_commit_per_task`'s
`len(FIXTURE_REPOS[repo_id])`). The per-test-tmp_path commit reaches only this one test's own
clone, verified empirically: the full covering set (below) is unaffected.

Verified end to end through the REAL pipeline (real `scan → sequence → transform → build`,
`--no-sandbox`, real Docker against the real `fleet-baseline:py3.11-node18` image this machine
already has built): `acme-lib-py` now gets `baseline_ok=1`, `baseline_test_count=1` (pytest exits 0,
`workers/baseline.py::_classify`'s own "exit 0 → count 1" branch), and the real Bazel build+test of
the migrated `py/acme_lib_py` package (now including the added test file, partitioned into
`test_srcs` by `_is_python_test_src`'s `test_*.py` predicate, compiled into a real `py_test`
target) passes.

**Broken build (red case).** Relied on the EXISTING, structural native-build failures round VI
task 111's own report measured and disclosed (chose this over constructing a new deliberately-
broken fixture repo, per the brief's own explicit "either is acceptable" choice) — cheaper, and
these are structural/reproducible properties of the fixture's own cross-repo dependency
declarations, not flakes: `acme-app-py`/`acme-app-ts` each declare a dependency on a fictional,
never-published cross-repo package name that cannot resolve against the real PyPI/npm registries;
`acme-lib-ts` declares no `"test"` script at all (`workers/baseline.py`'s own disclosed
misclassification of that case). The test asserts on these **three named repos specifically**
(`acme-app-py == 0`, `acme-app-ts == 0`, `acme-lib-ts == 0`), not "at least one fails", so a future
accidental fix to the cross-repo package-name issue would fail this assertion loudly rather than
silently making the proof vacuous again.

## Step 3 — the corrected assertion, and D116 closed

Replaced the `strict=True` xfail's body with an assertion checked against REAL production code
rather than a local re-implementation of §3.1(c)'s exemption rules: drove `scan` → captured a real
`fleet sequence --json` payload → `transform` → `build`, then asserted every `baseline_ok IS NULL`
repo_id is absent from `wave_index_by_repo` and present in `excluded` — i.e. genuinely exempted by
`graph.sequence.check_criteria`, the same mechanism `tests/test_baseline_scan_e2e.py`'s own Leg C
test already trusts for this exact purpose. (Note: `fleet sequence` must be captured **before**
`transform` runs, not after — a real re-sequence after Phase 2 has landed is correctly refused,
`SEQUENCE_REFUSED`, "N repo(s) in flight"; the test now drives `scan`/`sequence`/`transform`
directly rather than via the `transformed()` helper, for this reason.)

Deleted the `@pytest.mark.xfail(strict=True, ...)` decorator entirely (not loosened, not
retargeted) — its own passing state is now the proof, per the D97/CLAUDE.md §6 precedent the brief
names.

**`docs/INTEGRATION_HONESTY.md`'s D116**: heading changed `OPEN` → `FIXED, LANDED (round VI task
113, `3ab7c6e`...)`; a dated marker appended to the body (never rewriting the original prose), per
this file's "update the heading, annotate the body, never rewrite it" convention. Diff is exactly
the heading line plus one new paragraph — verified via `git diff` before committing.

## Rule 12 (mutation-testing) proofs

Three mutations, each backed up before mutating, diffed against the backup to confirm a genuine
change, and reverted after (`git diff --stat src/` is empty at the end — confirmed).

1. **Real-tests expressibility.** `workers/baseline.py::_classify`: `if result.exit_code == 0:
   return True, 1` → `return True, 0`. Result: **1 failed, 1 passed** — the closure test's own
   `baseline_test_count > 0` assertion for `acme-lib-py` failed specifically
   (`baseline_test_count=0`); the sibling discriminator test stayed green. Reddens exactly the
   real-tests case, not the module.
2. **Broken-build expressibility.** `_measure`'s build-failure short-circuit (`if not
   build_result.ok: return False, 0, build_exit, None`) disabled (`if False:`). Result: **1 failed,
   1 passed** — `acme-app-py`'s `baseline_ok` flipped to `1` (pytest then ran against
   acme-app-py's own directory, found no test files, exited 5 — "no tests collected" — the wrong
   verdict for a repo whose install genuinely failed), failing the `acme-app-py == 0` assertion
   specifically. `acme-app-ts`/`acme-lib-ts` stayed `0` for their own, separate reasons (missing
   `"test"` script exits non-zero regardless of the build step), confirming the mutation
   discriminates the specific repo it targets rather than the whole mechanism.
3. **Xfail-deletion meaningfulness (the brief's own explicit ask).**
   `BaselineWorker.run()`'s `if not payload.enabled or payload.ecosystem is None:` gate forced to
   `if True:` — i.e. every repo takes the "nothing to measure" skip unconditionally, reverting to
   the pre-Leg-B state D116 originally described (nothing ever writes `baseline_ok`). Result:
   **both tests in the file failed** — the closure test's exemption-membership assertion fired
   exactly as designed: `baseline_ok IS NULL for ['acme-app-py', 'acme-app-ts', 'acme-lib-py',
   'acme-lib-ts']` and all four **still participate in the wave plan** (Leg C's gate never fires
   without a measured `baseline_ok=0`, so nothing exempts them) — proving the now-passing state is
   genuinely sensitive to the Leg B/C mechanism existing, not a coincidence of the current fixture.

All three mutations confirmed via `diff` to be genuine, non-no-op changes to the file before their
results were trusted (CLAUDE.md's zero-change gate). All three reverted; final `diff -q` against
the pre-mutation backup confirmed byte-identical; `git diff --stat src/` is empty.

## Verification

- Covering set, whole files, no `-k`: `tests/test_baseline_ok_exclusion.py` **2/2**, `tests/
  test_scan_e2e.py` **33/33** (32.46s), `tests/test_transform_e2e.py` **20/20** (41.74s), `tests/
  test_baseline_scan_e2e.py` **4/4**, `tests/test_workers_baseline.py` **18/18** — all green, no
  regression from adding real test content to `acme-lib-py`'s per-test clone.
- `ruff check`: clean (whole repo). `ruff format --check` on the changed file: clean (whole-repo
  drift is pre-existing on bare `main` — 126 files there vs. 129 in this worktree, confirmed by
  running the same check against the primary checkout directly; not touched, per Rule 3).
- `mypy` (whole-manifest, no path args): clean, 131 source files.
- `git diff --stat src/`: empty (no production code change survives; all mutations reverted).
- `git diff --stat main..HEAD`: `docs/INTEGRATION_HONESTY.md` (+20/-1 heading edit + appended
  paragraph), `tests/test_baseline_ok_exclusion.py` (+226/-74, the corrected assertion + fixture
  strengthening + updated docstrings).

## Scope discipline

Did not touch `tests/conftest.py`'s `baseline_build_yaml` fixture or its docstring (no defect
found). Did not touch Legs A-D's own landed production code. Did not edit
`docs/CRITERIA_PLAN.md`'s §11 entry (left to the controller per the brief). Did not edit the
shared `tests.test_scan_e2e.FIXTURE_REPOS` dict (see Step 2's reasoning above for why that would
have been a far larger, riskier blast radius than needed).

## Does this close §12.11 as a whole?

**Yes, based on direct verification against `docs/CRITERIA_PLAN.md` §11's own current text.** Its
most recent entry (round VI task 111's fix round, already on `main` before this task started)
states verbatim: *"Only Leg E (delete D116's `strict=True` xfail, strengthen the fixture with real
native tests) remains open for §12.11 as a whole."* This task did exactly that: deleted the xfail
marker, strengthened the fixture (real native tests for the green case, three named real broken
builds for the red case), and closed D116 (heading `FIXED, LANDED`). Reading the whole §11 entry
top to bottom: Task A (round VI task 38) and Task B (round VI task 57, re-verified live round VI
task 90) are both closed; gap 2 (`migrated_test_count` persistence, round VI task 47) is closed;
gap 1 (`test_srcs` population) is closed for Python/JVM/Rust/JS, with JVM's own real-Bazel proof
separately blocked on `D121` (a different, already-disclosed defect outside Leg E's scope) and
JS's real-Bazel exercise noted as "not yet through `real_build()` itself" — both of these are
pre-existing, disclosed residuals the round's own controller had already determined do not block
"§12.11 as a whole" (the criterion's own text, per ADR-0135 ruling 3/research-53's narrowing, scopes
the fixture-run assertion to what the actual fixture fleet exercises — {PyPI, NPM} plus one empty
repo — and neither JVM nor a from-scratch JS `real_build()` proof is part of that fixture); gap 3
(`D116`) is now closed by this task. I did not re-derive the JVM/JS residuals myself (they predate
Leg E and were already disclosed as non-blocking by the round's own prior text) — flagging this
explicitly rather than silently inheriting it, per CLAUDE.md's own "the reason is the unmeasured
sentence" guardrail. If the controller's own reading of what "as a whole" requires differs from
what CRITERIA_PLAN.md §11 itself currently says, that is a controller-level call, not one I made
unilaterally.

## Status

**DONE.**

Commit range: `agent/roundvi-task113` = `947983e..8343649` (two commits: `3ab7c6e` test change,
`8343649` docs/D116 closure).

Test summary: covering set (`test_baseline_ok_exclusion.py`, `test_scan_e2e.py`,
`test_transform_e2e.py`, `test_baseline_scan_e2e.py`, `test_workers_baseline.py`) — **77/77 pass**;
ruff clean; mypy clean (131 files); three Rule 12 mutations each confirmed genuine and each
reddened specifically (real-tests case, broken-build case, xfail-deletion-meaningfulness case);
`git diff --stat src/` empty after revert.

**Concerns:** none load-bearing. The one thing worth the controller's own eyes: whether the
JVM-blocked-on-`D121`/JS-not-yet-through-`real_build()` residuals (both pre-existing, both
disclosed in `docs/CRITERIA_PLAN.md` §11 well before this task) should be read as blocking "§12.11
as a whole" despite the round's own most recent text saying only Leg E remained — I read that text
at face value and did not second-guess it, since re-adjudicating it was not this task's brief and
would be exactly the kind of unilateral Rule-14-flavored ruling the brief tells me to flag rather
than make.

---

## Fix round (coordinator-dispatched, same session) — two Minor findings from review, addressed

**Per this project's "annotate it, never rewrite it" convention: everything above is left exactly
as originally written. This section records what the fix round found and changed.** Task 113 was
reviewed and merged to `main` (`52752f4`) with 0 Critical findings and §12.11 confirmed closed as
a whole; two Minor findings were raised for a quick follow-up fix round on this same branch
(already-merged, so this lands as a small follow-up merge).

### Finding 1 — missing Docker-availability skipif guard

The closure test's own new assertions (`baseline_ok["acme-lib-py"] == 1`,
`test_count["acme-lib-py"] > 0`, and the three named `== 0` assertions) require the real native-
baseline container to genuinely SUCCEED, not merely run — a portability risk on a machine without
`docker`/the built `fleet-baseline:py3.11-node18` image, where the test would FAIL rather than
SKIP.

**Fix:** imported `_IMAGE_OK`/`_IMAGE_WHY` directly from `tests.test_baseline_container` (the
exact same helper that file's own tests use — chose importing over re-implementing the shape
locally, since it is literally the SAME image being checked, not a different one with its own skip
message the way `test_sandbox.py`'s and `test_baseline_container.py`'s own gates deliberately stay
un-shared) and added `@pytest.mark.skipif(not _IMAGE_OK, reason=f"fleet baseline image unusable:
{_IMAGE_WHY}")` to the closure test only.

**Deliberately NOT added to the discriminator test**, with the reasoning now recorded in that
test's own docstring: it only asserts NULL-set membership, and `workers/baseline.py::_measure`'s
own control flow makes that claim independent of whether the container succeeds — a build/test
step that can't even start (`docker` missing, an `OSError` `_invoke` catches) still returns
`(False, 0, ...)` (never NULL), and so does a container that starts but exits non-zero (daemon
unreachable, image unbuilt). The only way a non-empty repo's `baseline_ok` stays NULL is
`payload.ecosystem is None`, which has nothing to do with Docker. Verified this reasoning two ways
rather than asserting it:

1. Monkeypatched `shutil.which` to hide `docker` and called `test_baseline_container.
   _baseline_image_usable()` directly: returned `(False, 'docker CLI not on PATH')` — confirms the
   underlying helper's own gating logic.
2. Force-set `_IMAGE_OK = False` on both the `test_baseline_container` and
   `test_baseline_ok_exclusion` module objects (via a throwaway probe test, deleted after use) and
   re-ran the file: the closure test correctly **SKIPPED** (`fleet baseline image unusable:
   SIMULATED: docker CLI not on PATH`), and the discriminator test **PASSED**, unaffected —
   confirming both the skip mechanism fires correctly and the un-guarded test's own claim held
   independent of the (simulated) image-unusable state.

### Finding 2 — stale docstring sentence

`baseline_build_yaml`'s local-override docstring (originally around line 126) still read, present
tense, "both tests still pass (1 passed, 1 xfailed)" — stale the moment this task's own earlier
work deleted the `strict=True` xfail (the file is now 2 passed, 0 xfailed). **Fix:** reworded to
state the file is "2 passed, 0 xfailed" now and to avoid re-pinning an exact count that would go
stale again the next time a test is added or removed to this file — the substantive point (neither
test reads `phases.status`/wave membership) is unchanged and kept.

### Verification

- `tests/test_baseline_ok_exclusion.py`: **2 passed** (re-run after both fixes).
- `ruff check`/`ruff format --check` on the changed file: clean (one import-order fix needed —
  `test_baseline_container` import had to sort before `test_build_e2e`'s multi-line import; fixed,
  re-verified clean).
- `mypy` (whole-manifest, no path args): clean, 131 source files.
- Broader check beyond the brief's own ask, to confirm the new cross-file import didn't regress
  anything: `tests/test_baseline_ok_exclusion.py` + `tests/test_baseline_container.py` + `tests/
  test_baseline_scan_e2e.py` + `tests/test_workers_baseline.py` combined — **27/27 pass**.
- `git diff --stat src/`: empty (still no production code change).

### Status

**DONE.**

Commit range: `agent/roundvi-task113` = `947983e..<fix-round-sha>` (four commits total on top of
`main`@`947983e`: `3ab7c6e` test change, `8343649` D116 closure, `cb7c078` original report, plus
this fix round's own commit — see `git log agent/roundvi-task113` for the exact fix-round SHA).
This branch was already merged to `main` at `52752f4` before this fix round; the new commit is a
small follow-up the controller will merge separately, per the controller's own note.

Confirmation both fixes are in place: (1) the skipif guard is on the closure test, imports the
same `_IMAGE_OK`/`_IMAGE_WHY` helper `test_baseline_container.py` already uses (not
reinvented), and was verified to actually skip under a simulated image-unusable state; (2) the
stale "(1 passed, 1 xfailed)" sentence is corrected and no longer pins a count that will go stale
again.
