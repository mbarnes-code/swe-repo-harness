# Design — the worktree half of `fleet resume` §11.5 step 2 (D72)

**Status:** design only. Nothing in `src/` or `tests/` was edited to produce it.
**Ref for every citation below:** `main` at `d2e0090`.
**Working tree when measured** (`git status --short`):

```
 M src/fleet/sandbox/container.py     <- a sibling lane's in-progress edit, NOT read from disk
 M tests/test_sandbox.py              <- ditto
?? .superpowers/
```

`src/fleet/sandbox/container.py` was read via `git show main:…` only. Nothing a sibling lane is
mid-edit on is reported here as a defect.

**Probes run.** One value probe under `.venv/bin/python` with `src/` on the path, asking *what
value do these functions compute* (not `find_spec` = installed, not `sys.modules` = imported, not
`pyproject.toml` = declared): `slug("acme-commons") == "acme-commons"`,
`sandbox_name(run, "acme-commons", 2) == "fleet-<run>-acme-commons-2"`,
`run_prefix(run) == "fleet-<run>-"`. No pytest, no `docker`, no `FLEET_*` export.

---

## 1. The problem, evidenced

### Leg 1 — the name. **HOLDS.**

`src/fleet/orchestrator/context.py:206-208`. `OrchestratorContext.worktree(repo_id)` returns
`self.work_dir / repo_id`. There is no `fleet-` prefix, no run id and no attempt suffix in the
directory name. `context.py:232` feeds exactly that string to `WorkerContext.workdir`, so every
worker in the fleet is handed a path whose basename is the bare `repo_id`.

`src/fleet/models/repo.py:13` constrains `RepoId` to `^[a-z0-9][a-z0-9._-]{0,99}$`, and the
`_UNSAFE` class in `src/fleet/sandbox/worktree.py:46` strips exactly the characters that pattern
already excludes. Measured consequence: **`slug(repo_id) == repo_id` for every legal `RepoId`.**
So the *only* difference between what production writes (`<repo_id>`) and what the reaper looks
for (`fleet-<run_id>-<repo_id>-<attempt>`) is the run-scoped prefix and the attempt suffix — no
character-mangling is involved, and the gap is purely the missing affixes.

`WorktreeManager.reap` (`src/fleet/sandbox/worktree.py:303-338`) filters on
`path.name.startswith(run_prefix(self.run_id))` at line 330. A directory named `acme-commons`
fails that test unconditionally.

### Leg 2 — the registry. **HOLDS.**

`src/fleet/workers/clone.py:397-407` (`CloneWorker._materialize_worktree`) runs
`git worktree add --detach <worktree> <head_sha>` through a `Git` bound to `mirror`
(`clone.py:401`, `_git(mirror, ctx)`), where `mirror` is
`Path(payload.cache_dir) / f"{slug(repo_id)}.git"` (`clone.py:636-637`) and `cache_dir` is
`<root>/cache/git` (`src/fleet/cli.py:1904`, handed down at `cli.py:1185`). The registration
therefore lands in the **per-repo mirror's** git directory.

`WorktreeManager._git_run` (`worktree.py:134-141`) always passes `-C str(self.repo_dir)`, and the
one construction site sets `repo_dir` to `run.monorepo_path` (`src/fleet/cli.py:10464-10471`). So
`list_registered()` (`worktree.py:270-301`) interrogates the monorepo, which never recorded a
single Phase-1 checkout. Even a correctly *named* clone worktree would be invisible to it.

### Leg 3 — "constructed nowhere else in `src/`". **HOLDS, in the precise sense.**

`grep -rn "WorktreeManager(" src/` returns exactly one hit: `src/fleet/cli.py:10466`, inside
`_reap_worktree_manager`, which exists as a test seam for the reap itself. **No production path
creates a worktree through `WorktreeManager`.** `WorktreeManager.create` (`worktree.py:146-194`),
the one function that would produce a `fleet-<run_id>-<repo>-<attempt>` directory, is dead code in
`src/` — reachable only from `tests/test_sandbox.py`.

What *is* imported elsewhere is `sandbox_name`, by `src/fleet/workers/buildverify.py:64` and
`src/fleet/workers/rdepverify.py:56` — and in both it names a **container**
(`buildverify.py:351` `_container_prefix`, `rdepverify.py:331` the cancel-path removal), never a
directory. That asymmetry is the whole of D72: containers really are named by `sandbox_name`, so
the container half of step 2 sweeps a populated namespace; no directory anywhere in `src/` is.

### Leg 4 — not in the report, found while checking. **Two more worktree families exist.**

`src/fleet/cli.py:7321` (`_plan_build`) and `src/fleet/cli.py:7416` (`_plan_verify`) each cut a
worktree with `git worktree add --detach --force` through the `monorepo` `Git`
(`_monorepo_checkout`, `cli.py:7125-7150`, bound to `run.monorepo_path`) at
`build_root / repo_id` and `verify_root / repo_id` respectively — where
`build_root = <root>/work/integration` (`cli.py:7870`) and
`verify_root = <root>/work/verify` (`cli.py:8315`).

These are registered in **exactly the registry the reaper interrogates.** For Phase 3 and Phase 4
worktrees, leg 2 does not apply — only leg 1 does. A pure rename would make them reapable with no
change to the manager at all. This materially changes the option analysis in §4 and was not part
of the reported defect.

There is a second consequence, and it is the sharpest hazard in this whole design: **the path is
computed twice, independently, in two modules.** `cli.py:7321` builds `build_root / repo_id` for
the `git worktree add`; `cli.py:7625` hands `work_dir=build_root` to the `OrchestratorContext`,
which then builds `work_dir / repo_id` again at `context.py:208` to populate `ctx.workdir`. The
same duplication exists for verify (`cli.py:7416` vs `cli.py:7695`). Nothing enforces that the two
formulas agree. Change one and not the other and every Phase-3 worker is handed a path that does
not exist — and, because both halves are one-line joins in different files, no single listing
shows the mismatch.

---

## 2. The layout as it is, versus what `WorktreeManager` assumes

Concretely, for `settings.root = /srv/fleet`, defaults `run.work_dir = work/`,
`run.cache_dir = cache/`, `run.monorepo_path = ../acme-monorepo`, `repo_id = acme-commons`,
`run_id = 11111111-2222-3333-4444-555555555555`, second attempt:

| family | directory on disk today | registered in | cut by |
|---|---|---|---|
| Phase 1/2 repo checkout | `/srv/fleet/work/acme-commons` | `/srv/fleet/cache/git/acme-commons.git` (the mirror) | `clone.py:407` |
| Phase 3 build worktree | `/srv/fleet/work/integration/acme-commons` | `/srv/acme-monorepo` | `cli.py:7328` |
| Phase 4 verify worktree | `/srv/fleet/work/verify/acme-commons` | `/srv/acme-monorepo` | `cli.py:7423` |
| **what `reap()` sweeps** | `/srv/fleet/work/fleet-11111111-2222-3333-4444-555555555555-acme-commons-2` | `/srv/acme-monorepo` | `WorktreeManager.create` — **never called in `src/`** |

Two mismatches, three families, and they do not fail for the same reason:

* Phase 1/2 fails on **both** name and registry.
* Phase 3/4 fails on **name only** — the registry is already right.

Note also that `reap()` matches on `path.name` (`worktree.py:329-330`), not on the parent
directory, so a correctly-prefixed name nested under `work/integration/` would still be found. The
subdirectory is not itself an obstacle. `remove()`'s string form (`_resolve`, `worktree.py:263-268`)
does join `work_dir / name`, but `reap()` always passes the full `Path` from `list_registered()`,
so the nesting is safe on the removal path too.

**SPEC contradiction, which must be resolved before any code moves.** `docs/SPEC.md:1408` states
flatly that containers **and worktrees** are named `fleet-<run_id>-<repo>-<attempt>`, and
`docs/SPEC.md:6938` defines step 2 in terms of that glob. The code has never done this for
worktrees. This is the guardrail-7 shape: adjudicating the implementation is half the job; the
SPEC sentence has to move in the same change or a future reconciler will regenerate whichever side
we leave behind.

**And the SPEC's name form is structurally wrong for one of the three families.** A Phase-1/2
checkout is *reused across phases and across attempts*: `clone.py:275-289`
(`preconditions_hold`) treats an already-initialised worktree at the same path as re-entry, and
Phase 2's workers read `ctx.workdir` expecting Phase 1's clone to still be there. An
attempt-scoped name would give Phase 2 a different directory from Phase 1 and every retry a fresh
empty one. `ctx.worktree(repo_id)` (`context.py:206`) has no `attempt` parameter for exactly that
reason. The Phase-3/4 worktrees have the opposite lifecycle — `cli.py:7317-7328` removes and
re-cuts on every plan — so an attempt-scoped name is natural *there*. One naming rule cannot serve
both; §4 assumes two rules, and §5 task 1 is where that gets decided and written down.

---

## 3. What depends on the current layout

This list decides feasibility, so it is enumerated rather than summarised.

**Producers of the path (all must move together, or not at all):**

1. `src/fleet/orchestrator/context.py:206-208` — `worktree()`, the formula.
2. `src/fleet/orchestrator/context.py:232` — `worker_context()` stamps it into `WorkerContext.workdir`.
3. `src/fleet/cli.py:7321` — `_plan_build`'s independent copy of the same join.
4. `src/fleet/cli.py:7416` — `_plan_verify`'s independent copy.
5. `src/fleet/cli.py:9378` — `fleet pr` deliberately **overwrites** `worker_ctx.workdir` with the
   monorepo path, because the PR's base is the monorepo, not the repo checkout. A layout change
   must not disturb this override; it is the one site that is correct precisely by ignoring the
   layout.

**Consumers of `ctx.workdir` as "the repo's tree"** — every one of these breaks if the producer
moves and they are not moved with it. `clone.py:369,640`; `buildgen.py:266,351,357,532`;
`rdepverify.py:197,211`; `prwriter.py:247`; `interrogate.py:146-147`; `rewrite.py:278,287,293`;
`buildverify.py:560,700,899`; `relocate.py:149,158`; `cli.py:1128,3345,5078,5172,5219,5278,5287,5622`.
They read it, they do not compute it — so they move for free **provided** the producers stay
consistent. That is the whole feasibility argument for a rename: 25+ consumers, 4 producers.

**Payload fields that carry a worktree path as data:**

* `CloneInput.worktree_path` / `CloneOutput.worktree_path` (`clone.py:107-108`, `136`, `344`).
* `BuildverifyInput.worktree` (`buildverify.py:559-560`), `RdepverifyInput.worktree`
  (`rdepverify.py:197`).
* `RepoFacts.worktree_path`, built at `cli.py:2207` from `ctx.worktree(entry.name)` and consumed at
  `workers/contracts.py:290`.

**Durable state holding an absolute worktree path.** `checkpoints.payload` is a Pydantic
`dump_json` BLOB (`src/fleet/state/schema.sql:814-822`) and the payload models above are what go
into it. `buildverify.preconditions_hold` (`buildverify.py:688-702`) refuses re-entry when the
worktree in the checkpoint is gone. **A layout change therefore invalidates the checkpoints of any
run that is mid-flight when the change lands** — every affected repo re-runs its phase rather than
re-entering. That is the migration question, and it is real, not theoretical.

**Tests that depend on the layout.**

* `tests/test_cli.py:1632-1656` — `_reap_workspace`, `_cut`, `_sandbox`. `_cut` performs a real
  `git worktree add` into `workspace/work/<name>` from a repo the test itself initialised, with
  `<name>` supplied by `_sandbox(...)`. **The test constructs both the registry and the name the
  manager assumes.** Five worktree reap tests build on it (`test_cli.py:1659`, `1697`, `1722`,
  `1745`, `1763`); the container tests at `1801`, `1837` and `1881` use `_reap_workspace` without
  cutting worktrees. None of them touches `clone.py`, `context.py`, or `_plan_build`. See §6 —
  this is the trap.
* `tests/test_sandbox.py` — the only place `WorktreeManager(` is constructed in tests, and the only
  exercise `create()` gets anywhere. **Currently dirty in another lane's worktree**; treat its
  contents as unread and re-check before editing.
* `tests/test_cli.py:1784` patches `fleet.cli._reap_worktree_manager` — the seam a registry-plural
  reap (§4 option A) would change the *signature* of. Any option that alters that function's
  return type breaks this test by construction.

**Not to be touched.** `worktrees/wt-WT1-example` in this repository is a round-A artifact quoted
as live evidence in a committed ADR and used as a fixture. It is **not** evidence of the fleet
layout and must survive every change here. `test_cli.py:1732` already asserts a non-`fleet-`
neighbour survives the sweep; keep that assertion.

---

## 4. Options

### Option A — run-scope the directory names at the four producers; make the reap registry-plural

Give each family a name beginning with `run_prefix(run_id)`: Phase 3/4 keep `sandbox_name`
(attempt-scoped, matching their cut-and-recut lifecycle); Phase 1/2 gets a new attempt-free form
(`fleet-<run_id>-<repo_id>`, still inside `run_prefix`'s glob so `reap()` finds it unchanged).
Then teach `_reap_orphan_worktrees` to build one `WorktreeManager` per registry — the monorepo
*plus* every `cache/git/*.git` mirror — and merge the `ReapResult`s.

* **Fixes:** all three families.
* **Breaks / costs:** four producer sites must move in one commit or Phase 3 is handed a
  nonexistent path (§1 leg 4). Mid-flight checkpoints are invalidated (§3). `_reap_worktree_manager`'s
  signature changes, breaking `test_cli.py:1784`. Needs an ADR for the attempt-free name form,
  because `docs/SPEC.md:1408` currently forbids it. `clone` is on the hot path for every repo, but
  the change there is a *name*, not a new operation — no extra git invocation, no re-clone.
* **Honest weak point:** the mirror sweep is N+1 `git worktree list` invocations for N repos. At
  fleet scale (~250) that is 250 short local git calls once per `fleet resume`. Acceptable, but it
  should be measured, not assumed.

### Option B — change the manager to match the layout

Drop the prefix filter; sweep `work_dir`'s children against the live `repo_id` set.

* **Fixes:** nothing safely. The prefix is what confines the sweep to *this run's* namespace, and
  `test_cli.py:1732` exists because widening the predicate to "anything registered that no row
  claims" would delete a neighbouring run's checkout and non-fleet worktrees alike. It also cannot
  fix leg 2 — a filesystem or monorepo-registry scan still cannot see mirror-registered worktrees
  unless it drops git entirely. **Rejected.**

### Option C — route worktree creation through `WorktreeManager.create()`

Make `clone.py` and the two planners call `create()` instead of raw `git worktree add`.

* **Fixes:** all three, and kills the dead-code asymmetry of leg 3 at the root.
* **Breaks / costs:** `WorktreeManager` binds one `repo_dir` at construction, so `clone` needs a
  per-repo manager built from the mirror — a new object on the hot path for every repo. `create()`
  *raises* on an existing path (`worktree.py:163-167`), which is precisely the re-entry case
  `clone.py:275-289` is built to treat as success, so `clone` would need either a probe-then-create
  race or a new `create_or_reuse`. Strictly more surface than option A for the same outcome, and it
  puts a raise-on-exists constructor on the re-entry path. Defer: adopt A first; C becomes cheap
  afterwards and can be its own later change.

### Option D — conclude the worktree half should not exist

Delete it; reap worktrees by filesystem scan of `work_dir` for `run_prefix`-matching directories,
followed by `git worktree prune` in whichever registry owns them.

* **Fixes:** the registry leg, by not having one. Still needs the rename — a filesystem scan cannot
  find `acme-commons` either without re-introducing option B's unsafe predicate.
* **Breaks / costs:** `rmtree` without git's verdict is exactly the over-deletion D44 established
  the `no_verdict` discipline against (`worktree.py:229-251`). Losing the "git said this is a
  registered worktree" check to save an N+1 listing is a bad trade. **Rejected**, but worth stating
  because it makes the point that **the rename is the necessary condition under every option** —
  the registry question only decides *how removal happens*.

### Recommendation

**Option A.** Its main cost: the run-scoped name must land at all four producer sites in a single
change (two of them independent copies of the same one-line join in `cli.py`), and it needs an ADR
to bless an attempt-free name for the cross-phase checkout that `docs/SPEC.md:1408` presently
rules out.

A defensible **reduced-scope fallback** if the fleet-wide rename is judged too expensive: do the
Phase-3/4 rename only (task 3b below). Those two families are already in the right registry, so a
rename alone makes them reapable with no manager change, no mirror sweep, and no touch to
`clone.py`'s hot path — and it converts D72 from "finds nothing" to "finds two of three families".
Phase 1/2 would then need the reap's stated scope narrowed in SPEC and in the docstring, honestly,
rather than left looking complete.

---

## 5. Task breakdown

Sizes: **S** ≈ one focused pass, <150 LOC + tests. **M** ≈ 150–350 LOC + tests. **L** ≈ larger.

| # | Title | Size | Depends on | Files touched | Falsifiable acceptance criterion |
|---|---|---|---|---|---|
| 1 | **Decide and record the two name forms** — attempt-scoped `sandbox_name` for cut-and-recut Phase 3/4 worktrees, attempt-free `fleet-<run_id>-<repo_id>` for the cross-phase checkout — and correct `docs/SPEC.md:1408` and the §11.5 step-2 sentence at `docs/SPEC.md:6938` in the **same** change | S | — | `docs/DECISIONS.md` (new ADR — number assigned centrally), `docs/SPEC.md` | `grep -n "fleet-<run_id>-<repo>-<attempt>" docs/SPEC.md` no longer returns a line that asserts it of *worktrees* unqualified; the ADR states, in one sentence each, why the checkout cannot carry an attempt (Phase 2 reuses Phase 1's tree) and why Phase 3/4 can (recut per plan, `cli.py:7317-7328`) |
| 2 | **Add the name helper** — one function in `sandbox/worktree.py` producing the attempt-free run-scoped form, beside `sandbox_name` | S | 1 | `src/fleet/sandbox/worktree.py`, `tests/test_sandbox.py` | New name starts with `run_prefix(run_id)` for every legal `RepoId`; `reap()`'s prefix filter accepts it and rejects a bare `repo_id`, asserted against a **literal** `"fleet-"`-bearing string, not against a second call to the helper |
| 3a | **Move the checkout producer** — `OrchestratorContext.worktree` emits the task-2 name | M | 2 | `src/fleet/orchestrator/context.py`, `tests/test_orchestrator*.py` | `ctx.worktree("acme-commons").name.startswith(f"fleet-{run_id}-")`; `ctx.worker_context(...).workdir` ends in the same basename; `fleet pr`'s override at `cli.py:9378` still yields the monorepo path (assert it, it is the one site that must NOT change) |
| 3b | **Move the two planner producers in the same change** — `_plan_build` (`cli.py:7321`) and `_plan_verify` (`cli.py:7416`) | M | 3a | `src/fleet/cli.py`, `tests/test_cli.py` | A test that reads the worktree path out of `git worktree list --porcelain` in the monorepo after `_plan_build` runs, and asserts it **equals** the `workdir` the `OrchestratorContext` built for the same repo — the drift between the two independent joins is the quantity, and it must be read from git, not recomputed by the test |
| 4 | **Registry-plural reap** — `_reap_orphan_worktrees` sweeps the monorepo *and* each `cache/git/*.git`, merging `ReapResult`s and keeping `failed` per-registry with its registry named | M | 2 | `src/fleet/cli.py`, `tests/test_cli.py` | A mirror-registered orphan and a monorepo-registered orphan are both in `reaped_worktrees.reaped` from one `fleet resume`; a `WorktreeError` from one registry leaves the other registry's removals intact and names which registry failed; `test_cli.py:1784`'s seam patch is updated, not deleted |
| 5 | **The end-to-end instrument** — one test where the worktree is created by *production* code (a driven clone and a driven `_plan_build`), never by `_cut` | M | 3b, 4 | `tests/test_cli.py` | Before the sweep, `list_registered()` for the relevant registry is asserted **non-empty** (the denominator, see §6); after it, the production-created worktree is gone and `wt-WT1-example`-shaped non-fleet neighbours survive. Must fail on `main` — demonstrate that, do not assert it |
| 6 | **Disposition of worktrees already on disk** — adopt, or leak-and-report | S | 1 | `src/fleet/cli.py` (resume reporting), `docs/DECISIONS.md` or the ADR from task 1 | `fleet resume` against a workspace holding old-form `work/<repo_id>` directories reports them by name in a distinct key — neither silently reaped (the sweep has no license: an old-form directory may be a *live* run's checkout) nor invisible; mid-flight checkpoint invalidation (§3) is stated in the operator-facing output or the ADR |
| 7 | **Retire the KNOWN-LIMITATION record** — the long docstring on `_reap_orphan_worktrees` (`cli.py:10478-10508`) exists to make D72 visible and becomes false the moment tasks 3–5 land | S | 5 | `src/fleet/cli.py`, `docs/INTEGRATION_HONESTY.md` | After the edit, `grep -rn "never registered them\|no live run\|finds nothing" src/fleet/cli.py` returns nothing that still asserts the sweep is inert; the D-number's ledger entry is closed with the commit that closed it, and **the sweep-scope sentence in the replacement docstring is re-checked against task 5's test** rather than written from the plan |

**Dependency order.** `1 → 2 → 3a → 3b`; `2 → 4`; `{3b, 4} → 5 → 7`; `1 → 6`.
Task 1 is the gate and should go first and alone — it touches `docs/SPEC.md` and every other task
inherits its answer. Tasks 3b and 4 are the only pair that can run in parallel, and only after 2.

**3a and 3b are listed separately but must land in one commit.** They are split only so a
subagent gets one logical task each; if 3a merges without 3b, Phase 3 workers receive a path
nothing created. If the executing lane cannot guarantee a single commit, merge them into one M/L
task rather than shipping 3a alone.

**ADR needed:** task 1 (the two name forms plus the SPEC correction) and task 6 (the disposition
of pre-existing directories, if the answer is anything other than "report them"). **Do not assign
numbers** — 0078 and 0081-0084 are taken, 0079/0080 reserved, D72/D73 allocated; the orchestrator
allocates at dispatch.

---

## 6. The instrument question, answered in advance

**The quantity to measure.** The number of entries in `fleet resume --json`'s
`reaped_worktrees.reaped` **when the worktree under test was created by production code** — a
driven `CloneWorker` run, or a driven `_plan_build` — and never by the test. Under the defect that
number is 0 for every one of the three families. After the fix it is ≥1 for each. That is the only
form of the quantity that moves under D72.

**Trap 1 — the test does the defect's job for it.** `tests/test_cli.py:1647-1652`'s `_cut` helper
performs a real `git worktree add` **into the monorepo** with a name from `_sandbox(...)`. Both of
D72's legs are translations: mirror→monorepo and `repo_id`→`fleet-<run>-<repo>-<attempt>`. `_cut`
performs both translations itself, for free, before the sweep ever runs. Every existing reap test
therefore passes identically on `main` and on a fixed tree — the quantity cannot move under the
defect, because the fixture already stands where the defect would have prevented the code from
standing. This is the validated-but-blind shape: those tests really do validate `reap()`'s
predicate, prefix confinement, `ReapResult.failed` propagation and the fresh-instance case; they
say nothing whatever about whether anything in the fleet is ever named or registered so as to be
found. **Task 5 may not use `_cut`.** If a future author reaches for it "just to set up the
fixture", the instrument is dead again and will look green.

**Trap 2 — the rename disarms the assertion.** If task 5 computes its expected directory name by
calling the same helper task 2 adds, both sides of the assertion move together under any rename or
reword and the test passes vacuously — the shape that produced 33 green tests over a rewritten
class today. At least one assertion must be against a literal string containing `fleet-` and the
run id, spelled out in the test.

**Trap 3 — the fault lives *between* two listings, so a single-file test cannot see it.** The
Phase-3 path is computed at `cli.py:7321` and, independently, at `context.py:208` via
`work_dir=build_root` (`cli.py:7625`). A test that patches `ctx.worktree`, or that builds the
expected path itself, observes exactly one of the two and passes while the other is wrong — this
is the same fault-between-two-listings shape that defeated an instrument on this code path today.
Task 3b's criterion is written to avoid it: read the cut path out of `git worktree list --porcelain`
in the monorepo, read the handed-down path off the `WorkerContext`, and assert the two are equal.
Neither side is computed by the test.

**Trap 4 — the empty denominator.** `reaped == []` is returned by a sweep that sees nothing, by a
sweep whose registry holds nothing, and by a sweep pointed at a registry that does not exist
(`cli.py:10509-10522` returns a `skipped` key for that last one, which is the only one of the three
currently distinguishable). A test asserting `reaped == [orphan]` after a *successful* fix is
therefore not enough on its own to prove the sweep is looking anywhere real. **Assert the
denominator:** `list_registered()` for the registry under test is non-empty before the filter runs.
Without that, task 5 can regress into trap 1 silently the first time someone changes a fixture path.

**The one thing a test cannot establish here.** No test proves that the *production* dispatch path
— `fleet scan` at fleet scale, with real `cache_dir` and real config — produces names matching
`run_prefix`. Task 5 drives the workers directly. Closing that last gap needs either an assertion
inside `_reap_orphan_worktrees` that the registries it swept were non-empty, or an operator-visible
count in the resume output. Task 4's criterion asks for the per-registry reporting that makes it
observable; it does not make it *asserted*, and this design does not claim otherwise.

---

## 7. Open items this design did not settle

* Whether the N+1 mirror listings in option A are cheap enough at 250 repos. Not measured here —
  and per guardrail 6, no number goes into the ADR until someone measures it.
* Whether any *other* consumer persists a worktree path outside `checkpoints`. The three payload
  models in §3 were found by grep; a fourth would change task 6's answer.
