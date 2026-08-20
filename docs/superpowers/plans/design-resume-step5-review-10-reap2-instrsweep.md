> Promoted from `.superpowers/sdd/design-resume-step5/review-10.md` (round C scratch, lane Review-10), snapshot taken while `main` was at `1050e0a`. Examines REAP2 §11.5 step 2 (`e915b93`, `0b0db5c`, `3440b12`, `ead96e6`) and INSTRSWEEP (`d2e0090`). Verdict: 1 Critical, 1 Important, 3 Minor.

# Review 10 — REAP2 (§11.5 step 2) and INSTRSWEEP (the instrument gate)

**Scope.** `e915b93`, `0b0db5c`, `3440b12`, `ead96e6` (REAP2) and `d2e0090` (INSTRSWEEP).
Read-only review; nothing in the tree was changed by it.

**Anchors, and why there are two.** Analysis began at `87ed419`. `main` moved twice during the
review — `5ed4e47` (a sibling lane's D73 residual fix, which touches the very
`_reap_orphan_containers` dry-run branch two findings below concern) and `b5bbb31`. **Every
mutation result reported below was re-run and re-confirmed at `5ed4e47`, over the whole
`tests/test_cli.py` module (104 tests), unless a line says otherwise.**

`git status --short` observed at the start: ` M docs/PROGRESS.md`, `?? .superpowers/`. At the end:
` M src/fleet/cli.py`, ` M tests/test_cli.py`, `?? .superpowers/` — a sibling lane is editing
`_reap_orphan_containers` and adding a `test_resume_dry_run_reports_a_docker_ps_it_could_not_run…`
case right now. That uncommitted work is **not** reported as a defect anywhere here, and I checked
it before writing I-1 so as not to report a lane's live work back at it: its subject is D73's
listing-**error** path, not the preview's prefix/`claims()` filters, which is what I-1 is about.

**Method.** Detached worktree outside the shared scratchpad, under a private per-lane directory,
with its own `BAZEL_ROOT` (the path contains a space, so `conftest._bazel_root()` keys it by
digest to a private temp dir), every `cd` guarded `|| exit 1`, and a mutation harness that
**aborts when `git diff --numstat` reports zero changed lines and prints the numstat before the
test result**. `.venv/bin/python`, scoped pytest only, one session at a time, no `FLEET_*` exported,
no real `docker` invoked. Both worktrees removed at the end; `git worktree list` shows the primary
and `worktrees/wt-WT1-example` only.

---

## Verdict

| Lane | Verdict |
|---|---|
| REAP2 (`e915b93`, `0b0db5c`, `3440b12`, `ead96e6`) | **Land with one required correction** — the code is sound and honest; ADR-0081 §2 and the lane report carry one false claim about a test, and the property that claim is about is unpinned. |
| INSTRSWEEP (`d2e0090`) | **Land.** The gate does what it says, fails closed, and its stated boundary is real. One nit. |

**Counts:** 1 Critical · 1 Important · 3 Minor.

**Does anything in the reap wave claim or imply the worktree half works? No.** The commit message
(`e915b93`), the `_reap_orphan_worktrees` docstring (`src/fleet/cli.py:10491-10507`), ADR-0081's
own **title** and its §4 all state plainly that the worktree half is a correct sweep over an empty
namespace, name both mismatches (registry and name), and name the two modules that would have to
change. `_reap_lines` prints the namespace it searched on every line, which is the mechanism that
keeps the silence from reading as cleanliness. This is the honest shape; it is worth saying so
plainly. D72 is not re-raised.

**Could any test here survive its subject being deleted? No.** Measured, not argued: replacing the
body of `_reap_orphan_worktrees` with an empty report fails exactly the five worktree cases and no
others; the same deletion of `_reap_orphan_containers` fails exactly the three container cases.
The eight partition cleanly 5/3 with no overlap and no survivors.

---

## C-1 (Critical) — ADR-0081 §2 names a test as the guard against a deletion that test cannot see

**Sites.** `docs/DECISIONS.md`, ADR-0081 §2 ("…and
`test_resume_dry_run_names_the_orphans_it_would_reap_and_removes_none` (`tests/test_cli.py:1745`)
fails if it goes"), repeated in the lane report
`.superpowers/sdd/design-resume-step5/task-reap2-report.md` ("`tests/test_cli.py:1745` fails if it
is removed"). Subject: `_LIVE_SANDBOX_PREDICATE`, `src/fleet/cli.py:9928-9930`.

**Measured.** Two mutations, both with a non-zero numstat read before the test result:

| Mutation | Change | `tests/test_cli.py` |
|---|---|---|
| neuter the negation — `NOT (1 …)` → `NOT (0 …)`, i.e. liveness collapses to `status='RUNNING'` with the bind parameters still consumed | `1 1` | **104 passed** |
| the realistic refactor — `_LIVE_SANDBOX_PREDICATE = " AND status = 'RUNNING'"` **and** drop the now-unused `horizons` from the query's parameters | `2 2` | **104 passed** |

The eight step-2 cases pass under both. The only mutation that *does* fail is deleting the
concatenation while leaving two extra `?` parameters bound — which fails all eight with a SQLite
parameter-count error, i.e. it is caught as a crash, not as a behaviour, and would be caught the
same way by tests that have nothing to do with liveness.

**Why the named test cannot see it.** `test_resume_dry_run_names_the_orphans_it_would_reap_and_removes_none`
(`tests/test_cli.py:1745-1762`) builds a workspace with **no `phases` rows at all** — no
`_put_leased` call — and cuts one orphan. The live set is empty either way, so the quantity it
watches (the `would reap …` line for a single worktree) cannot move under any change to the
liveness predicate. This is CLAUDE.md Guardrail 6's fourth question, unasked at exactly the site
the ADR uses to answer it.

**The property is real and a test can catch it.** I wrote a throwaway probe (in the worktree,
never landed): a stale `RUNNING` row (`heartbeat_at` three days old, `attempts = 2`) plus a
worktree cut at rung 3, then `resume --dry-run --json`. On the clean tree it reports
`reaped_worktrees.reaped == ["fleet-…-acme-commons-3"]`; under the neutered negation it reports
`[]`. So the gap is missing coverage, not an unreachable property.

**Failure scenario.** ADR-0081 §2 exists to stop a future reader from deleting a negation that is
provably redundant on the non-dry path. That reader will delete it, run `tests/test_cli.py`, see
104 green, and cite the ADR's own sentence as evidence the guard held. From then on
`fleet resume --dry-run` reports every crashed run's own orphans as live and previews reaping
nothing — the one answer ADR-0081 §2 says a health check must never get wrong.

**Remedy (two edits, per CLAUDE.md §7).** Land the probe above as a ninth case, *and* correct the
sentence in ADR-0081 §2 and in the lane report — an ADR that names a test which does not do the
job is worse than one that names none, because it stops the next author looking.

---

## I-1 (Important) — the `--dry-run` preview's own filters are unpinned, while the docstring asserts them

**Sites.** `src/fleet/cli.py:10528-10531` (worktree preview: `p.name.startswith(prefix) and p.name
not in live`) and `src/fleet/cli.py:10614-10619` at `5ed4e47` (container preview:
`name.startswith(prefix) and not any(claims(live_name, name) …)`). The claim they back:
`_reap_orphan_worktrees`'s docstring — "Nothing outside `fleet-<run_id>-*` is reachable from here
in **EITHER** branch, because the prefix filter is applied before any `remove()` and **the preview
applies the identical one**."

**Measured at `5ed4e47`, whole module each time:**

| Mutation | Change | Result |
|---|---|---|
| worktree preview: drop `p.name.startswith(prefix)` | `1 1` | **104 passed** |
| container preview: drop `and not any(claims(...))` | `0 1` | **104 passed** |

**Why.** `test_resume_reap_cannot_reach_a_worktree_outside_the_run_namespace`
(`tests/test_cli.py:1722`) — the case whose name asserts the containment property — runs the
**non-dry** path only, so it exercises `WorktreeManager.reap`'s filter, never the preview's copy.
The single `--dry-run` case registers exactly one worktree (so a widened filter finds the same
one) and runs under the autouse docker fake with an **empty** inventory (so the container
preview's `claims()` filter never has an input). Between them, no test puts a neighbour worktree
or a live container in front of a preview.

**Failure scenario.** A future edit widens either preview filter — the likeliest being a
"simplification" that trusts `list_registered()`/`list_with_verdict` to be already scoped. The
suite stays green. `fleet resume --dry-run` then prints
`step 2: would reap 3 orphan worktree(s): wt-WT1-example, fleet-<other-run>-…` and the operator,
who ran the health check precisely to be told what to clean, runs `git worktree remove` on another
lane's evidence — or `docker rm` on a live build for the container half. The preview removes
nothing itself; the operator is the removal path, which is exactly why a preview must not lie.

**Remedy.** Two cheap cases: give the existing `--dry-run` worktree test the same two neighbours
test 1722 already builds (another run's sandbox, a non-`fleet-` name), and give the container
preview a live-rung container plus a `_put_leased` row.

---

## Minors

**m-1 — `src/fleet/cli.py:9877-9879` attributes a verbatim quote to the wrong module.** The comment
says "`settings.py` says exists «captured per phase so a config change cannot retroactively declare
a live worker dead»". Normalised whole-file search over `src/`: the sentence does not occur in
`settings.py`; it is the `Field(description=…)` of `heartbeat_ttl_seconds` in
`src/fleet/models/state.py:64-65`. **Pre-existing, not this wave** — `git log -S` puts it at
`6f78e62`, and `git show e915b93 -- src/fleet/cli.py` does not contain the string, so REAP2 neither
wrote nor moved it. Reported rather than inherited as "pre-existing" on someone's say-so, because
it is a live instance of CLAUDE.md §3's "never quote another module's comment verbatim" sitting in
the block ADR-0081 cites: reword `models/state.py`'s description and this comment points at a
sentence no longer in the tree, with the wrong file named for finding it.

**m-2 — the `raising=False` gate reads keywords only.** `tests/test_instruments_are_armed.py:214-224`
inspects `node.keywords`, so `monkeypatch.setattr(obj, "_name", spy, False)` — the positional
fourth argument, which is `raising` — evades it. Verified: adding such a call to `tests/test_cli.py`
in the worktree left all three gate tests green. There are **0** such calls in the suite today
(AST sweep for `setattr`/`patch.object` with ≥4 positional args), so this is a latent hole in the
checker, not a live defect. One `len(node.args) >= 4` clause closes it.

**m-3 — the five worktree cases pin a namespace production never populates, and only the ADR's
title says so.** The cases are not vacuous — they cut real worktrees with `git worktree add` and
all five fail when the sweep is deleted — but a reader arriving at `tests/test_cli.py:1659-1800`
sees five green tests of a sweep that finds nothing in production, and the disclosure lives one
hop away (the section comment names ADR-0081, whose title carries it). One sentence in the
`_reap_workspace` docstring — "production does not register worktrees under these names; see D72"
— would put it where the next author of these fixtures reads.

---

## Confirmations (checked, clean, no action)

**The eight cases — spot-checked well past two.** Baseline 8/8 green.

| Mutation | numstat | Killed by |
|---|---|---|
| drop `+ 1` from `_live_sandbox_names` | `1 1` | cases 1, 6, 7 |
| **pre-expansion** — caller lists, computes `spared`, passes it to `reap()` | `3 1` | **case 7 only** |
| `_reap_lines` headline computed independently of `failed` | `1 3` | cases 5, 8 |
| worktree half returns an empty report (subject deleted) | `2 0` | cases 1-5, and only those |
| container half returns an empty report (subject deleted) | `2 0` | cases 6-8, and only those |

**The eighth case does open the window it claims to.** This is the round's most-repeated failure
shape, and the lane caught it in itself. Confirmed mechanically: under the pre-expansion mutation
`test_resume_reap_hands_the_container_sweep_sandbox_names_not_expanded_ones` **passes** — the
quantity it watches (which of a fixed inventory ends up removed) does not move — and
`test_resume_container_sweep_lists_once_so_a_build_starting_mid_sweep_survives` is the sole case
that fails. The `starts_after_first_listing` fixture is a genuine second listing, not a restatement
of the first.

**ADR-0081 §5's discriminating-mutation claim holds.** Under the independent-headline mutation the
first assertion (`FAILED to reap worktree <name>` present) still passes and the failing assertion is
`assert "no orphan worktrees" not in result.output` — the old-passes/new-fails shape Rule 12 asks
for, at the assertion the ADR names.

**ADR-0081's citations.** All 13 `file:line` references spot-checked against `git show 3440b12:<f>`:
`cli.py:9857` (`_RESET_RUNNING_TO_PENDING_SQL`), `:9928` (`_LIVE_SANDBOX_PREDICATE`), `:10398`
(`_count_stale_running`), `:10460` (the two-rung comprehension), `runner.py:686`
(`attempt = ladder.attempts + 1`), `worktree.py:61` (`sandbox_name`), `:330` (the prefix/live
filter), `clone.py:397`, `context.py:206`, `container.py:184` (`claims`), `buildverify.py:64`,
`rdepverify.py:56`, `:331` — every one lands on what it says. The ADR **quotes no other module's
text verbatim**, labels the ordering departure as an Agent Recommendation (Guardrail 1), and
records that the placeholder's pointer to `adr-0081-draft.md` names a file that does not exist —
which is true (`ls` confirms). Its ordering reasoning matches the code: step 2 is below
`_reset_stale_running` **and** below `project_once` at `cli.py:10211-10231`, and the "immaterial
because the reap writes no `phases` row" justification is correct — the reap's writes are the
filesystem and the docker daemon only.

**The instrument gate — I could not defeat it within its stated scope.** Four-way validation, each
run against `tests/test_instruments_are_armed.py`:

1. *known-bad*: rename `BaseWorker.preconditions_hold` → `preconditions_admit` (`1 1`) → **fires**;
2. *clean*: unmodified tree → **silent** (3 passed);
3. *synthetic fault into a clean file*: append `class _Review10Disarmed(ContainerSandbox)` with a
   method the base does not have → **fires**;
4. *control*: cosmetic reflow of an existing subclass's `on_cancel` signature across four lines
   (`4 1`) → **stays green**, so it asserts meaning and not layout.

`checked >= 30` behaves as claimed and is tight, not decorative: the real count is **34** (18
classes), and breaking `_test_sources` to skip all but one file produced
`only 8 subclass methods were examined` rather than a green pass. The self-expiring exemption
works: adding a `reset()` classmethod to `BaseWorker` makes `test_the_allowlist_has_not_gone_stale`
fail with `…is now a real override…; delete its NOT_OVERRIDES line`. Unresolvable bases fail the
run rather than being skipped, so the checker fails closed.

**The stated boundary is real.** Deleting the single `await self.on_cancel(ctx)` call from
`workers/base.py` while keeping `on_cancel` defined (`1 1`) leaves all three gate tests green —
exactly the "no rename at all" disarm the file's *What this CANNOT see* section discloses. (In this
particular instance the owning test failed for its own reasons, but that is that test's assertion,
not the gate's.) The `fixtures/` exclusion costs nothing today: there are **0** `.py` files under
any `tests/**/fixtures/` path. The docstring's claim that `monkeypatch.setattr` is self-defending is
sound; `delitem`/`delenv` with `raising=False` do appear in the suite but bind `sys.modules` keys
and environment variables, not production attributes, so they are outside the class and correctly
not swept.

**Container-half correctness.** `live` reaches `ContainerSandbox.reap` unexpanded
(`cli.py:10608` at `3440b12`), the preview imports `claims` rather than restating it, and
`test_resume_reap_cannot_reach_a_worktree_outside_the_run_namespace` correctly cites ADR-0074 as
quoting `worktrees/wt-WT1-example` (verified: the string appears under the ADR-0074 heading in
`docs/DECISIONS.md`). The reap tests point `run.monorepo_path` inside `tmp_path`, so no test can
reach the real `worktrees/` directory; that fixture is untouched and `git worktree list` confirms it
intact.

**Not re-raised (recorded, adjudicated elsewhere):** D72 (the worktree half sweeps an empty
namespace), D73 (`list_by_prefix` collapsing a non-zero exit to `[]`, whose residuals a sibling
landed at `5ed4e47` mid-review), `resize()` having no production caller, and the real-daemon test
hazard the autouse fixture in `0b0db5c` closes.
