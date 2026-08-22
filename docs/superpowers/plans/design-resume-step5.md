# design — `fleet resume` §11.5 step 5: the per-phase re-entry driver

**Refs read.** Everything below is anchored to a committed ref, named per claim:

| Ref | What |
|---|---|
| `7a8bfbb` (`main`, primary checkout) | `src/`, `docs/SPEC.md` — `git status --short` showed only `CLAUDE.md` modified and two untracked plan files; no `src/` or `docs/SPEC.md` edit was uncommitted at read time |
| `4a519b3` (`agent/RS1`, `wt-RS1`) | `--repoll-prs`, §11.5 steps 3 and 7, `_refuse_unbuilt_resume_flags`, the marked `stub_reconcile` insertion point. `git status --short` in `wt-RS1` was clean |
| `dabfd50` (`agent/FD1`) | +40 lines in `orchestrator/runner.py` — **collision surface, see §4** |
| `92648f5` (`agent/ST1`) | `orchestrator/stubs.py` (+817) — the future `stub_reconcile` |
| `c8060b8`/`c23a08a` (`agent/BK1`/`BK2`) | `llm/backends/*` only — no overlap |

**Three factual corrections to the brief and to `research-B1.md` §Q3**, each verified at `7a8bfbb`:

1. **There are 11 registry workers, not 12** — `src/fleet/workers/` holds `buildgen, buildverify,
   classify, clone, contracts, interrogate, prwriter, rdepverify, relocate, rewrite, symbolindex`
   (`base.py` is the ABC). `runner.py:717` itself says "all eleven workers". There are **15**
   `preconditions_hold` implementations in total: those 11 plus the four pipeline composites
   defined in `cli.py` (`:1070`, `:3287`, `:5020`, `:5570`). The count matters because the four
   composites are the ones a phase walker would actually call, and they are the least informative
   of the fifteen (§2).
2. **`prwriter.preconditions_hold` is at `:209`, not `:272`, and it does NOT check that dependency
   PRs are MERGED.** It checks three things only: `report.verdict == "PASS"`, `report.repo_id ==
   ctx.repo_id`, and `repo_status not in (REQUIRES_HUMAN_INTERVENTION, SKIPPED)`. The MERGED check
   lives in `run()` (`prwriter.py:~265-285`), where an unmerged dependency returns
   `status="partial"` with `output.held = True` and `completed_units=[SYNC_UNIT]` — a **worker
   outcome**, not a precondition. Any resume logic that wants "are the dependency PRs merged?"
   must go through `_sync` / `_pr_sync_impl`, never through `preconditions_hold`.
3. **`fleet gc` is not the place to factor step 2 from.** `_gc_impl` (`cli.py:10033`) evicts
   `llm_cache` / `events` / `attempts` rows and optionally LRU-evicts the Bazel disk cache. It
   reaps **no** worktree and **no** container. The step-2 primitives already exist elsewhere and
   are already written against §11.5 by name: `sandbox/worktree.py:303 reap(*, live_names, …)` and
   `sandbox/container.py:323 reap(…)`.

`relocate.py:140` is confirmed exactly as the brief states: `_plan_matches_tree(Path(ctx.workdir),
payload.dest_path, units_owed(payload.sources, payload.completed_units))` — the `java/java/com/x`
guard.

---

## 1. The four existing `PhaseRunner` call sites, compared

All four are inside a `_run_*_wave` helper whose docstring calls itself "the composition root, and
nothing else". Read at `7a8bfbb`.

| | `cli.py:1831` | `cli.py:4127` | `cli.py:7591` | `cli.py:7661` |
|---|---|---|---|---|
| Enclosing fn | `_run_scan_wave` (`:1775`) | `_run_transform_wave` (`:4079`) | `_run_build_wave` (`:7539`) | `_run_verify_wave` (`:7608`) |
| Phase | 1 SCAN | 2 TRANSFORM | 3 BUILD | 4 VERIFY |
| Worker | `ScanPipelineWorker()` | `TransformPipelineWorker()` | `BuildPipelineWorker(bazel_runner=BAZEL_RUNNER)` | `VerifyPipelineWorker(bazel_runner=BAZEL_RUNNER)` |
| Chained units | `SCAN_UNITS` = `("clone","interrogate","classify","symbolindex")` (`:977`) | `TRANSFORM_STEPS` = `("relocate","rewrite")` (`:3175`) | `BUILD_UNITS` = generate/verify/publish (`:4581`) | `VERIFY_UNITS` = verify/rdeps (`:4582`) |
| Payload factory | `_scan_payloads(settings, fleet, steps=, batch_rows=)` — needs the **config-derived `RepoEntry` list**, not a DB read | `_transform_payloads(settings, plans, rules)` — needs `_TransformPlan` (`:3443`) + `RewriteRule`s | `_build_payloads(settings, plans, monorepo=, lock_dir=, sandboxed=)` — needs `_BuildPlan` (`:5766`) | `_verify_payloads(settings, plans, sandboxed=, rdeps_limit=, rdeps_sample_n=, affected_only=)` — needs `_VerifyPlan` (`:5841`) |
| Result sink | `_ScanSink(writer, repository, run_id, evidence=_ScanEvidence)` | `_TransformSink(writer, repository, read_conn, run_id, evidence)` | `_BuildSink(attempts=_AttemptWriter(...), writer, run_id, evidence)` | `_VerifySink(attempts=_AttemptWriter(...), evidence, writer, run_id)` |
| Scheduler store | `_ScanWaveStore(SqliteSchedulerStore, members)` — synthetic single wave | `_ScopedWaveStore(SqliteSchedulerStore, only)` | `_ScopedWaveStore(SqliteSchedulerStore, members)` | `_ScopedWaveStore(SqliteSchedulerStore, members)` |
| `descendants=` | **absent** | `ordering_descendants(await _ordering_pairs(...))` | same | same |
| `RunContext.config` | `settings.config` | `settings.config` | `_phase_config(settings, Phase.BUILD, timeout_s)` | `config` (= `settings.config`, passed in) |
| `RunContext.work_dir` | `settings.root / run.work_dir` | `settings.root / run.work_dir` | `build_root` = `…/work_dir/"integration"` | `verify_root` = `…/work_dir/"verify"` |
| Concurrency | `config.concurrency` **clamped to `lanes`** for `git_net`/`subprocess` | `config.concurrency` | `config.concurrency` | `config.concurrency` |
| Logger | `"fleet.scan"` | `"fleet.transform"` | `"fleet.build"` | `"fleet.verify"` |
| Waves driven | exactly one, `SCAN_WAVE_INDEX` | `_open_transform_waves`, ascending | `_open_phase_waves(…, Phase.BUILD, wave)` | `_open_phase_waves(…, Phase.VERIFY, wave)` |
| Wave gate | none (whole fleet) | `_gated_members(predecessor=Phase.SCAN)` | `_gated_members(predecessor=Phase.TRANSFORM)` | `_gated_members(predecessor=Phase.BUILD)` (`cli.py:8281`) |
| Returns `RunContext`? | **yes** — §3.1 step 5b runs after the wave with the same client/ledger | no | no | no |

### What a generic walker would have to generalize

Six axes, none of them a parameter today:

1. **`work_dir`** — three different roots, and the Phase-3/4 roots are *not* the per-repo scan
   worktree. `RunContext.worktree()` is `work_dir / repo_id`, so getting this wrong points every
   Phase-3 dispatch at the Phase-1 checkout.
2. **`config`** — Phase 3 goes through `_phase_config(settings, Phase.BUILD, timeout_s)`; the
   others do not.
3. **The plan types.** `_TransformPlan` / `_BuildPlan` / `_VerifyPlan` are each assembled by a
   large phase-specific prologue inside `_transform_impl` (`:4254`), `_build_impl` (`:7811`),
   `_verify_impl` (`:8243`): `_dest_paths`, `_import_specifiers`, `_repo_facts`, `_unit_deps`,
   `_manifest_paths`, `_owned_coordinate_keys`, `_monorepo_checkout` + the `IntegrationMutex` lock
   dir, the gazelle scratch, and the ADR-0055 run-domain root files. `_build_impl`'s own docstring
   records that the wave's snapshot ref is **re-cut per wave** after each publish. This is the bulk
   of the work and it is not shared between phases in any form.
4. **Sink shape.** Four unrelated classes; two of the four wrap `_AttemptWriter`, two do not.
5. **Scheduler store + wave source.** `_ScanWaveStore` synthesises one wave over a fixed member
   list; the other three read `waves`/`wave_members` and need `descendants` for `blocked_by`.
6. **The wave gate.** Phases 2–4 withhold on the predecessor's verdict *in the driver*, before any
   phase row is written (`_gated_members`, `cli.py:7686` docstring: "a repo that has not met it
   must get **no phase row at all**").

**Verdict — the fifth path cannot reuse these four and must not re-derive them.** Reusing them
means calling `_run_*_wave`, which means reconstructing their prologues; re-deriving them means a
second copy of ~450 lines of `_build_impl` that will drift (Rule 2, Rule 3, Guardrail 6). The
correct shape is that **resume owns no `PhaseRunner` at all**: step 5 is a durable state
correction over `phases`, and §11.5 step 8 "continue" is delegation to the three existing
composition roots in phase order. See §6.

---

## 2. The re-entry contract as it actually exists

### The method

`BaseWorker.preconditions_hold(self, ctx: WorkerContext, payload: I) -> bool`, declared abstract at
`workers/base.py:620`. Abstractness is *enforced* rather than declared: `base.py:966-970` rejects a
concrete subclass for which `cls.preconditions_hold is BaseWorker.preconditions_hold`. That is why
no worker is a stub — the class would not be constructible.

Note the shape of the arguments. It takes a **typed payload and a live `WorkerContext`**. Neither
exists outside a dispatch. `runner.py:624` states this explicitly: it is consulted between the
payload and `execute`, "the only point where both the typed payload and the `WorkerContext` the
worker would receive exist". A resume driver that wanted to poll `preconditions_hold` across four
phases would have to manufacture four payloads and four worker contexts per repo — i.e. it would
have to have already solved §1's problem.

### `ReEntry` (`runner.py:208-265`), verbatim on the polarity

> The polarity is the whole point and it is not symmetric … `True` means "the checkpoint describes
> the tree in front of me, re-enter for `remaining_units` alone", and `False` means "it does not —
> run the phase whole from its anchor". **Neither verdict ever means "skip the work".** A driver
> that read `False` as a skip would skip every fresh repo in the fleet (`interrogate`,
> `symbolindex`, `rewrite` and `clone` all return `False` when there is nothing to resume) and
> report success for work that never ran.

| Outcome | Condition (`_re_entry`, `runner.py:706-745`) | Effect |
|---|---|---|
| `FRESH` | `checkpoint is None` — **`preconditions_hold` is not called** | phase runs whole |
| `RESUME` | holds, `checkpoint.remaining_units` non-empty | dispatch `remaining_units` only |
| `COMPLETE` | holds, nothing owed | **no dispatch**; `_already_complete` writes SUCCEEDED with `attempts=0` |
| `REJECTED` | does not hold | checkpoint dropped, phase re-runs from `phases.base_ref`, `RepoOutcome.checkpoint_rejected=True`, `checkpoint_rejected` warning |

### What the fifteen implementations actually validate — two families

I read all eleven registry implementations and all four composites at `7a8bfbb` rather than trusting
`research-B1.md`'s index. They split cleanly, and the split is the crux of §3.

**Family A — checkpoint-validity.** Answers "does my checkpoint still describe this tree?".
**Returns `False` when there is no checkpoint.** Ten of fifteen:

| Impl | Site | Predicate |
|---|---|---|
| `ScanPipelineWorker` | `cli.py:1070` | `remaining_units is None → False`; else `Path(ctx.workdir).is_dir()` |
| `TransformPipelineWorker` | `cli.py:3287` | identical |
| `BuildPipelineWorker` | `cli.py:5020` | identical |
| `VerifyPipelineWorker` | `cli.py:5570` | identical |
| `clone` | `clone.py:274` | mirror is a git dir ∧ worktree is a git worktree ∧ `HEAD` resolves |
| `interrogate` | `interrogate.py:268` | `remaining_units is None → False`; worktree is a dir; every checkpointed manifest path still `paths_intact` |
| `symbolindex` | `symbolindex.py:204` | same shape, over indexed source files |
| `classify` | `classify.py:125` | `remaining_units is None or UNIT in remaining_units → False`; else worktree is a dir |
| `relocate` | `relocate.py:140` | `_plan_matches_tree(workdir, dest_path, units_owed(sources, completed_units))` — **the `java/java/com/x` guard** |
| `rewrite` | `rewrite.py:269` | `_targets_are_present(workdir, units_owed(targets, completed_units), dest_path)` |

**Family B — phase-entry gates.** Answers a question about durable state that is meaningful with or
without a checkpoint. Five of fifteen:

| Impl | Site | Predicate |
|---|---|---|
| `buildgen` | `buildgen.py:253` | `dest` not under the reserved `_scc/` namespace ∧ `dirs_present(workdir)` ∧ (`ingest` dirs present) ∧ (`targets != []` ∨ `gazelle is not None` ∨ `ctx.context_policy is not None`) |
| `buildverify` | `buildverify.py:688` | `integration_ref.startswith("refs/")` ∧ `dirs_present(worktree)` ∧ `files_present(worktree/dest/"BUILD.bazel")` |
| `rdepverify` | `rdepverify.py:183` | `integration_ref.startswith("refs/")` ∧ `dirs_present(...)` ∧ **`ctx.db.get_phase(run, repo, Phase.BUILD).status is SUCCEEDED`** (a missing row returns `True`, deliberately: "No BUILD row at all is a first admission by the runner, not evidence of a failure") |
| `contracts` | `contracts.py:245` | hard `return True` — "a claim rather than a default"; 5b is a rebuild, not an accumulation |
| `prwriter` | `prwriter.py:209` | `report.verdict == "PASS"` ∧ `report.repo_id == ctx.repo_id` ∧ `repo_status not in {RHI, SKIPPED}` |

### The fifth usage pattern, which is not `PhaseRunner` at all

`prwriter` is never driven by `PhaseRunner`. `fleet pr` drives it through a bespoke `_emit_one_pr`
(`cli.py:9228`) which calls `preconditions_hold` at `cli.py:9293` as an **admission gate** — if it
returns `False`, the repo is reported as failed with a rendered reason, not re-run whole. It also
overrides `worker_ctx.workdir` to the monorepo path (`cli.py:9291`). So the harness already
contains two mutually incompatible readings of the same method: `runner.py`'s ("`False` is never a
skip") and `_emit_one_pr`'s ("`False` means do not do this"). Any step-5 design that adds a third
reading makes it three.

---

## 3. The demotion algorithm

### The SPEC sentence, twice

`SPEC.md:180-182` (Constraint 7):

> **Resume validates preconditions, never blind-replays** (Constraint 7): before re-entering a
> phase, `runner.py` re-checks the phase's declared preconditions (below) against SQLite; a failed
> precondition demotes the repo to the earliest phase whose precondition holds.

`SPEC.md:6777-6779` (§11.5 step 5):

> (5) re-check each phase's declared preconditions and demote to the earliest phase whose
> precondition holds (Constraint 7)

> **SUPERSEDED (2026-08-21), lane W2 — both quotes above are RETRACTED SPEC wording, kept as the
> record of what this design was reacting to. They were verbatim and correct at this document's own
> declared anchor `7a8bfbb` (see "Refs read" at the top); they are NOT what `docs/SPEC.md` says
> today, and no line in `docs/SPEC.md` carries "earliest phase whose precondition holds" any more.**
> At `main`, Constraint 7 and §11.5 step 5 both read: demote the repo to its **re-entry floor** —
> the phase *above* the *highest* phase below the settled frontier whose durable evidence still
> holds **or** which is a `DEGRADED`/`SKIPPED` hard stop
> (`orchestrator/reentry._HARD_STOPS`, tested before evidence), never that phase itself, and `SCAN`
> only if there is no such phase. The retraction is `60d400b`/`f466287`; the rule is implemented by
> `orchestrator/reentry.phase_floor` and bound by `tests/test_floor_rule_statements.py`, whose
> census does **not** reach this directory. Anyone building subtasks 4, 5 or 7 from this document
> must take the wording from `docs/SPEC.md`, not from these quotes.

### Why the literal reading is unimplementable against the code

**(a) It is monotonically false for the case it is written for.** Ten of the fifteen implementations
— including *all four* phase composites, which are the only things a per-phase walker can call —
return `False` precisely when there is nothing to resume. For a repo with no checkpoints, the walk
`p ∈ {1,2,3,4}` produces `False, False, False, False`. "The earliest phase whose precondition holds"
is the empty set and the SPEC defines no behaviour for it.

> **SUPERSEDED (2026-08-21), lane W2 — this section analyses the RETRACTED wording quoted above and
> is preserved as the argument that produced the correction, not as a statement about `docs/SPEC.md`
> today.** "The earliest phase whose precondition holds" is no longer in the SPEC; the argument
> below is what removed it. Its conclusion stands and is implemented — see
> `orchestrator/reentry.phase_floor` and ADR-0076.


**(b) It inverts for a fresh repo.** `rdepverify.preconditions_hold` returns `True` when the BUILD
row is missing (`rdepverify.py:199-202`, quoted above), which is exactly a repo that has never
built. A naive ascending walk over the *unit* workers therefore answers "Phase 4 holds, Phase 1
does not" for a fresh repo — the algorithm **promotes to Phase 4** the very repos that have not been
cloned. This is not a corner case; it is the default state of every repo at the start of a run.

**(c) Non-monotone by construction in the healthy steady state.** For a repo mid-Phase 3, the tree
is already relocated, so `relocate.preconditions_hold` is `False` (its whole job), while
`buildverify.preconditions_hold` is `True` (there is a `BUILD.bazel` under `dest`). Phase 3 holds
and Phase 2 does not, for a completely healthy repo. So the answer to the brief's question "is that
reachable?" is **yes, and it is the normal case, not an anomaly** — which means an ascending scan
cannot distinguish it from genuine corruption.

### The algorithm, restated precisely

The correction is structural: **demotion is a search *downward* from the settled frontier, never an
upward scan, and it does not call `preconditions_hold` at all.**

```
settled(row)   :=  row.status in {SUCCEEDED, SKIPPED, DEGRADED}   # ADR-0077 §5
terminal(row)  :=  row.status is REQUIRES_HUMAN_INTERVENTION
hard_stop(row) :=  row.status in {DEGRADED, SKIPPED}              # ADR-0077 §5 (ambiguity 5)

for each repo r in the run:
    rows := {p: phases(r, p) for p in 1..4}       # a MISSING row is PENDING (schema default)

    # 1. the frontier
    if any p with terminal(rows[p]):  skip r      # RHI is mechanically terminal (enums.py:46)
    frontier := min{ p in 1..4 : not settled(rows[p]) }   ( = 5 if all four settled )

    # 2. demote by looking BACKWARDS from the frontier only
    floor := frontier
    for p from frontier-1 down to 1:
        if hard_stop(rows[p]):        break       # never demoted, and never searched PAST
        if not evidence_holds(r, p):  floor := p
        else:                         break       # earlier phases are covered by this one

    # 3. write (one transaction)
    demoted := [ q in floor .. 4 : rows[q].status is SUCCEEDED ]
    for q in demoted:  demote q to PENDING, attempts RETAINED, one PhaseDemoted finding each
    if demoted:                                   # a no-op resume must not sweep checkpoints
        for q in floor .. 4 where rows[q].status is not DEGRADED:
            drop checkpoints row (r, q)           # a checkpoint above a demoted phase is a lie

    # 4. preconditions_hold is NOT consulted here.
    #    It is consulted by PhaseRunner._re_entry when `floor` is next dispatched — the only
    #    place a payload and a WorkerContext exist (runner.py:624).
```

Because phase `p` is only *asked* when `p+1 … 4` were `SUCCEEDED`, case (c) never arises:
monotonicity is a property of the traversal, not an assumption about the predicates.

`hard_stop` is design ambiguity 5, resolved in **ADR-0077 §5** (`docs/DECISIONS.md`) and not in
this document. `DEGRADED` leaves the machine only through a budgeted revalidation round and
`SKIPPED` is a config exclusion resume does not re-decide, so for both the rule is two-sided: the
floor may not land *on* such a phase, and the walk may not continue *below* one. The second half is
what an earlier draft of this block dropped — and dropping it demotes every repo with an excluded
or degraded middle phase all the way to phase 1 on every resume, because such a phase can never
produce holding evidence. `src/fleet/orchestrator/reentry.py::phase_floor` implements both stops
and `tests/test_reentry_floor.py` binds each of them separately.

Step 3 is stated as `demote_to_floor` landed it (`src/fleet/state/repository.py`): the sweep is
span-wide but conditional on something actually having been demoted, and it leaves `DEGRADED` rows'
checkpoints alone. **Decided — no longer open, and not subtask 6's to re-adjudicate:** that
exclusion names `DEGRADED` only, so a `SKIPPED` row above the floor keeps its status and still
loses its `checkpoints` row. The asymmetry is the answer, not an oversight: the walk's hard stop
protects a *decision* (a budget nobody granted, or an operator's config exclusion), which both
statuses need, while the sweep's carve-out protects a *payload a future round will legitimately
resume from* — and only `DEGRADED` has such a round, since `SKIPPED` is in `TERMINAL_STATUSES` with
an empty transition set. **ADR-0082 §3** (`docs/DECISIONS.md`) settled it and states that
`repository.py` needs no change on this point; commit `704e52f` pinned it with
`test_a_skipped_phase_keeps_its_status_but_still_loses_its_checkpoint`
(`tests/test_repository.py`). ADR-0077 §5 speaks only to the status, which is why it did not.

### `evidence_holds` — a new, resume-owned predicate

This is what "the phase's **declared preconditions** … against SQLite" (Constraint 7's own wording,
note "against SQLite") actually means, and it is deliberately *not* `preconditions_hold`. Proposed
per-phase content, each clause chosen because it duplicates a fact some Family-B implementation
already checks, so the two cannot disagree:

| p | `evidence_holds(r, p)` | Mirrors |
|---|---|---|
| 1 | the mirror is a git dir and the worktree resolves `HEAD` | `clone.py:274`'s three facts |
| 2 | `migrate/<repo>` exists and `phases(r,2).post_commit_sha` resolves on it | §3.2 step 6; fed by step 4 |
| 3 | `phases(r,3).post_commit_sha` resolves ∧ `<dest>/BUILD.bazel` present on the integration ref | `buildverify.py:688` |
| 4 | `phases(r,3).status is SUCCEEDED` ∧ a persisted `VerificationReport` for `r` | `rdepverify.py:183`, `prwriter.py:209` |

> **SETTLED (2026-08-21), lane W4 — the table's preamble is FALSE for row 4, and row 3's clause is
> wrong. Both are implemented deliberately otherwise; do not reconcile the code to this table.**
> True as a proposal at this document's declared anchor `7a8bfbb`; falsified by reading the
> implementations it cites, and landed at `abd009b` with **ADR-0088 §2–§3** as the record.
>
> * **The preamble — "so the two cannot disagree" — does not hold for row 4.**
>   `workers/rdepverify.preconditions_hold` returns `True` when the BUILD row is *missing*, with
>   its own comment giving the reason ("No BUILD row at all is a first admission by the runner,
>   not evidence of a failure"). That is right for an admission gate and catastrophic for
>   evidence: a missing BUILD row is the state of every repo that has never built, so copying it
>   would let the backward walk stop at Phase 4 — §3(b)'s promotion inversion, arriving by the one
>   route this section believed it had closed. `evidence_holds(r, 4)` reads a missing row as
>   `PENDING` and answers `False`. `docs/SPEC.md` §11.5 step 5 is affirmatively on the
>   implementation's side here, naming the same inversion and citing ADR-0077 §6.
> * **Row 3's "present on the integration ref" is wrong.** The method it cites checks
>   `files_present(worktree / payload.dest / "BUILD.bazel")` — the worktree. §6 option C below is
>   written about a Phase-3 repo *whose `BUILD.bazel` was reaped*, and a ref read cannot observe a
>   deletion from disk: the blob stays reachable from `post_commit_sha`. Reading the ref would
>   answer `True` for exactly the repo step 5 exists to demote.
> * **Row 3 also transcribes only the third of `buildverify.preconditions_hold`'s three
>   refusals.** The second, `dirs_present(worktree)`, is load-bearing, not defensive:
>   `util.proc._run_locked` has no `except OSError`, so a `Git` call bound to a directory that is
>   not there raises `FileNotFoundError` instead of answering. Both Git-reading predicates check
>   the worktree first.
> * **Rows 2 and 3 differ on ancestry, and that is inherited from this table rather than argued.**
>   Row 2 says "resolves *on it*" (implemented as `Git.is_ancestor`); row 3 says only "resolves".
>   The implementation follows the table and discloses the consequence in `_build_evidence`'s
>   docstring; a subtask that wants them symmetric must decide it, not assume it.

It reads `phases` + git and needs no payload, no `WorkerContext`, and no worker instance — which is
what makes step 5 implementable at all without solving §1.

### Ambiguities the SPEC leaves open — named, not resolved

1. **`SUCCEEDED → PENDING` is mechanically forbidden.** `enums.py:45` is
   `RepoStatus.SUCCEEDED: frozenset()`, with the comment "Terminal statuses map to the EMPTY set,
   which is what makes them terminal mechanically rather than by prose: no crash sweep can
   resurrect an abandoned repo into RUNNING." `transition()` (`enums.py:59-68`) raises
   `ValueError` on any unlisted pair. **Step 3 of the algorithm above cannot be written today.**
   This is a direct contradiction with §11.5 step 5 and is the subject of §6.
   (`OPERATOR_REOPEN` covers only `RHI → PENDING`, and `operator=True` has **no caller anywhere in
   `src/`** — `fleet retry`'s documented escape is itself unwired.)

   > **SETTLED (2026-08-21), lane W2 — no longer an ambiguity, and "step 3 cannot be written today"
   > is false at `main`. Do not re-adjudicate this.** True at this document's declared anchor
   > `7a8bfbb`, where `git show 7a8bfbb:src/fleet/models/enums.py` contains no `RESUME_DEMOTE`
   > (verified, zero matches). §5 task 1 landed it: `models/enums.RESUME_DEMOTE` plus the
   > `resume=True` gate in `transition()`, with `demote()` as the only sanctioned door — call
   > `demote()`, **not** `transition(..., resume=True)`, which returns the status alone and emits no
   > `PhaseDemoted` finding. ADR-0077 records the decision; `state/repository.demote_to_floor` is the
   > writer; `orchestrator/reentry.phase_floor` computes the floor it writes to. The `OPERATOR_REOPEN`
   > parenthetical is untouched and not re-verified here.
2. **`attempts` on demotion.** Steps 3 and 4 both say "retaining `attempts`" / "do not increment
   `attempts`" explicitly. Step 5 says nothing at all. If a repo is demoted from Phase 4 to Phase 2,
   do Phases 3 and 4 keep their own `attempts` counters, or reset? By analogy with step 3 they
   should be retained, but retaining them means a repo demoted twice arrives at Phase 3 with a
   ladder already two rungs spent through no fault of its own.
3. **Where a demoted repo re-enters the schedule.** `SPEC.md:1558-1564` is emphatic that "**closed
   waves are never re-opened**" and prescribes a synthetic wave at `max(waves)+1` — but it says so
   for `blocked_by` *reversal* (step 6), not for step-5 demotion. Step 5 never says where a demoted
   repo goes. `graph/sequence.py:291 append_synthetic_waves` exists and is the obvious answer, but
   the SPEC does not authorise it here.
4. **`--from-phase` semantics.** The flag is specified but its interaction with the computed floor
   is not: override (`floor := from_phase`), clamp (`floor := min(floor, from_phase)`), or filter.
   RS1 currently refuses it outright (`_refuse_unbuilt_resume_flags`, `4a519b3`).
5. **`DEGRADED`.** It is deliberately non-terminal (`enums.py:25-27`) and leaves only via a budgeted
   revalidation round (§3.5.1). Under `settled()` above it is *not* settled, so a DEGRADED Phase 3
   becomes the frontier and gets demoted — which would bypass the revalidation budget entirely.
   §11.5 step 5 contains no carve-out. Recommend treating DEGRADED as settled-for-demotion-purposes
   and recording it as an ADR; the SPEC does not say.

   > **SETTLED (2026-08-21), lane W2 — resolved in ADR-0077 §5, and this item is contradicted by
   > this document's own §3 body. Do not re-adjudicate it and do not file the recommended ADR; it
   > exists.** The recommendation was carried out: ADR-0077 §5 makes `DEGRADED` and `SKIPPED`
   > non-demotable, and ADR-0082 §3 settles the separate question of the `checkpoints` sweep. Two
   > sentences of the item are now false against the algorithm block in "The algorithm, restated
   > precisely" above, which was updated when the ADR landed while this item was not: that block
   > defines `settled(row)` as `{SUCCEEDED, SKIPPED, DEGRADED}` and `hard_stop(row)` as
   > `{DEGRADED, SKIPPED}`, both annotated `# ADR-0077 §5`, and says outright that "`hard_stop` is
   > design ambiguity 5, resolved in **ADR-0077 §5**". So "Under `settled()` above it is *not*
   > settled" is false — it **is** settled there — and "a DEGRADED Phase 3 becomes the frontier and
   > gets demoted" cannot happen: `orchestrator/reentry._HARD_STOPS` stops the walk on it without
   > moving the floor onto it.

---

## 4. Interaction with what already exists

### Order inside `_resume_impl`

`_resume_impl` at `agent/RS1` `4a519b3` executes, in source order:

1. drift/profile/wave-ceiling refusals — **step 1, built**
2. `_record_drift_findings`, `_raise_wave_ceiling`, `_raise_run_ceiling`
3. `if repoll_prs and not dry_run: await _pr_sync_impl(...)` — **built (RS1)**
4. **the marked insertion point** — a 9-line comment block reserving the slot for
   `stub_reconcile`, "immediately below the re-poll and above the step-3 sweep — and nowhere
   earlier"
5. `cutoff = _iso(_now() - timedelta(seconds=settings.config.run.stale_after_s))`; then
   `_count_stale_running` (dry-run) or `_reset_stale_running` — **step 3, built (RS1)**
6. `project_once(path, run_id=…, path=DEFAULT_PROJECTION_PATH)` — **step 7, built (RS1)**
7. back in `resume()`: `raise CommandUnavailableError(...)` naming step 5

**Step 5 goes between 5 and 6**, i.e. immediately after `_reset_stale_running` and immediately
before `project_once`. Both boundaries are load-bearing:

- **After the stale sweep**, because demotion reads `phases.status` and a `RUNNING` row is neither
  settled nor demotable — `ALLOWED_TRANSITIONS[RUNNING]` does include `PENDING` but only "crash
  sweep only" (`enums.py:35`), and running two writers over the same row in one command is exactly
  the collision the fence exists to prevent. Let step 3 normalise `RUNNING → PENDING` first.
- **Before `project_once`**, because §11.5 step 7 regenerates `migration_state.json` *from SQLite*
  and RS1's own comment insists the projection is "an OUTPUT regenerated from SQLite, never an
  input". A projection taken before the demotions would ship a `migration_state.json` that
  disagrees with `phases` — the exact drift class §11.5's preamble says it removes.

### The `--repoll-prs` → `stub_reconcile` ordering constraint

§13 row 45 (`SPEC.md:7093`), verbatim on the consequence:

> `stub_reconcile` does **not** abandon a stub whose provider has an open PR, so exit 7 keeps
> meaning "a human is needed" rather than "the fleet gave up waiting"

RS1 already honours this — the re-poll is step 3 in source order and the marker is step 4. **Step 5
must land below the marker**, not above it, so the invariant chain is:

```
--repoll-prs  →  stub_reconcile (ST1)  →  step 3 sweep  →  step 5 demotion  →  step 7 projection
```

If step 5 were placed above `stub_reconcile`, a demotion could return a repo to Phase 2 and drop its
Phase-4 row before `stub_reconcile` reads `ix_stubs_open`, changing which stubs are open at
reconcile time — inverting exit 7 by a different route than the one row 45 warns about.

### How step 4 (Git-as-arbiter) feeds step 5

Step 4 is a hard **prerequisite** of step 5, not a sibling: `evidence_holds(r, 2)` and
`evidence_holds(r, 3)` both read `phases.post_commit_sha`, and §11.5's own table says that column is
"A **pointer** into Git … Never trusted over Git". Running step 5 before step 4 means demoting on a
pointer nobody has validated.

**The primitives already exist** — `research-B1.md` says step 4 "belongs in `vcs/commits.py`"; it is
already *there*:

- `vcs/commits.py:298 find_task_commit(git, *, branch, pre_commit_sha, task_id) -> str | None`,
  whose docstring reads "Resume's ONLY question (§3.2 step 6.4): did this task's commit land on
  `branch`? … Yes → the work landed; reconcile the task row to `DONE` with that SHA, re-run
  nothing, **consume no attempt**. No → nothing landed; discard the worktree and re-run the rung.
  There is no third answer."
- `vcs/commits.py:312 commits_in_range` — "the reconciliation read".
- `vcs/commits.py:344 discard_task(git, *, task_pre_commit_sha, branch)` — the no-branch, which
  refuses the *phase* anchor by name.
- `vcs/commits.py:400 rollback_phase(git, *, branch, phase_base_ref)` — whole-phase rollback.

So step 4 is a **driver**, not new machinery, and the "without incrementing `attempts`" constraint
is satisfied by *not calling* the attempt-incrementing path — there is no existing resume code path
that bumps `attempts`, so this is a constraint on new code, verified by a test rather than a fix.

### Collision surface with in-flight lanes

- **`agent/FD1` (`dabfd50`) edits `orchestrator/runner.py`** — inserts `await
  self.ctx.llm_findings.flush()` into `run_wave` (after the `except*`) and into `_drive` (right
  after `_dispatch`), plus a `record_backend_unavailable` call in the `TierUnavailable` arm. The
  recommended design **does not touch `runner.py` at all**, so there is no collision. Any subtask
  that decides to modify `PhaseRunner` will conflict in both hunks.
- **`agent/ST1` (`92648f5`)** adds `orchestrator/stubs.py` only — no `cli.py` edit yet, so its
  eventual `_resume_impl` insertion is one line at RS1's marker and does not overlap step 5's
  insertion point five statements later.
- **`agent/BK1`/`BK2`** touch `llm/` only. `agent/BK1` also edits `cli.py` (+24) — in
  `fleet models check` / `SHIPPED_BACKENDS` territory (`cli.py:~10600`), far from `_resume_impl`.

---

## 5. Task breakdown

Sizes: **S** ≈ one focused pass, <150 LOC + tests. **M** ≈ 150–350 LOC + tests. **L** ≈ larger; only
one task is L and it is deliberately last.

| # | Title | Size | Depends on | Files touched | Success criterion |
|---|---|---|---|---|---|
| 1 | **Decide and land the demotion transition** — extend `ALLOWED_TRANSITIONS` / `transition()` for `SUCCEEDED → PENDING` under a resume-scoped gate, per §6 | S | — | `src/fleet/models/enums.py`, `docs/DECISIONS.md` (new ADR), `docs/SPEC.md` (§11.5 step 5 wording), `tests/test_state_models.py` + `tests/test_schema_sql.py` (**not** `tests/test_enums.py` — that module has never existed; the transition tests live in `test_state_models.py` and the CHECK-domain test in `test_schema_sql.py`) | `transition(SUCCEEDED, PENDING, resume=True)` returns PENDING; `transition(SUCCEEDED, PENDING)` still raises; `transition(RHI, PENDING, resume=True)` still raises; the schema-domain test still derives its CHECK list from the enum |
| 2 | **`phase_floor` — the pure frontier + demotion computation** | S | 1 | new `src/fleet/orchestrator/reentry.py`, `tests/test_reentry_floor.py` | A pure function `phase_floor(rows: Mapping[Phase, PhaseRow \| None], evidence: Mapping[Phase, bool]) -> Phase \| None`, no I/O. Table-driven test over all 7 `RepoStatus` × 4 phases; RHI ⇒ `None`; all-settled ⇒ `None`; missing row treated as PENDING; DEGRADED per the ADR from task 1 |
| 3 | **Step 2 — reap orphan worktrees and containers** | S | — | `src/fleet/cli.py` (`_resume_impl`), reads `src/fleet/sandbox/worktree.py:303`, `container.py:323`, `tests/test_cli.py` | `live_names` derived from `phases` rows still holding a lease; a `fleet-<run_id>-*` worktree no live row claims is removed; `ReapResult.failed` is surfaced in the emitted lines, never swallowed (D44); `--dry-run` reaps nothing |
| 4 | **Step 4 — Git-as-arbiter task reconciliation** | M | 3 | `src/fleet/cli.py`, reads `src/fleet/vcs/commits.py:298,312,344`, `tests/test_cli.py` | For a task whose trailered commit is on `migrate/<repo>`: row → `DONE`, SHA written to `attempts.commit_sha` and `phases.post_commit_sha`, **the `phases.attempts` COLUMN byte-identical before and after** (assert on the column). For one that is not: `discard_task`, row → `PENDING`, `phases.attempts` again unchanged. *(Corrected 2026-08-21, W1: this row originally said "`attempts` byte-identical" unqualified. Read as the `attempts` TABLE that is impossible on the landed branch and contradicts the binding SPEC, which orders a write to `attempts.commit_sha` in the same breath; the parenthetical "assert on the column" was the only thing disambiguating it. The SPEC governs and the counter column is what must be invariant.)* Missing `phases.base_ref` is recreated. No third branch |
| 5 | **`evidence_holds` — the four per-phase durable predicates** | M | 4 | `src/fleet/orchestrator/reentry.py`, `tests/test_reentry_evidence.py` | Each of the four table rows in §3 implemented against `phases` + `Git`; a fixture whose `<dest>/BUILD.bazel` is deleted yields `evidence_holds(r,3) == False`; a fresh repo yields `False` at every phase without any promotion effect (the caller never asks upward) |
| 6 | **The demotion writer** | S | 1, 2 | `src/fleet/state/repository.py` (one new method), `src/fleet/state/checkpoints.py`, `tests/test_repository.py` | Demoting a repo from frontier 4 to floor 2 sets phases 2–4 `PENDING`, retains every `attempts` value, deletes the `checkpoints` rows for 2–4, and writes it in **one** `StateWriter` unit; a repo with an RHI row is untouched |
| 7 | **Wire step 5 into `_resume_impl`** | S | 2, 5, 6 | `src/fleet/cli.py` (`_resume_impl`, `_resume_lines`), `tests/test_cli.py` | Placed after `_reset_stale_running` and before `project_once`; `--dry-run` computes and prints the demotion plan and writes nothing; `migration_state.json` reflects the demotions; the RS1 `stub_reconcile` marker comment is left above, untouched |
| 8 | **Step 6 — recompute `blocked_by` and append the un-blocked wave** | M | 7 | `src/fleet/orchestrator/scheduler.py` (`SchedulerStore` Protocol + `SqliteSchedulerStore`), `src/fleet/cli.py` (`_resume_impl`, `_ScanWaveStore`, `_ScopedWaveStore`), `src/fleet/orchestrator/reentry.py`, `docs/SPEC.md` (§12 item 46(ii), which per ADR-0090 §6 ships in the **same commit** as the predicate it constrains); reads `scheduler.SqliteSchedulerStore.append_blocked_by`, `scheduler.WaveScheduler.propagate_blocked`, `scheduler.WaveScheduler.open_wave`, `graph.sequence.append_synthetic_waves` — **by symbol**: this column's former line citations `scheduler.py:255,411` and `graph/sequence.py:291` are replaced per CLAUDE.md, and at `34d6f82` `:411` already resolved to a docstring line | An entry leaves `blocked_by` when the repo it names is no longer in a blocking population — **not** "is now `SUCCEEDED`", which SPEC §3.4's `pr_merged` reversal refutes as an exclusive condition (ADR-0090 §2; the removal **polarity** — erase-what-is-not-re-derived vs retain-what-cannot-be-resolved — is **ruled R2-CLOSED** in ADR-0090 §2.4: the recompute retains any `blocked_by` entry it cannot positively show is no longer blocking, and removes only what it can). A repo whose list empties returns to `PENDING` at its floor and is moved into an appended wave at `MAX(wave_index) + 1` with `waves.synthetic = 1`, written through the new `SchedulerStore.append_unblocked_wave` (ADR-0090 §1) and **never** through `record_plan`, whose upsert re-budgets `waves.max_usd` on every wave it touches. **No existing `waves` row is modified** — that is the achievable form of this row's former absence claim, which does **not** hold for the step-5 demoted population because `WaveScheduler.wave_state` is computed rather than stored, and which is disclosed and deliberately unfixed in **ADR-0089 §4**. *(Corrected 2026-08-21, W11 at `34d6f82`, in the same commit as ADR-0090. This row previously read "A repo whose blocking ancestor is now SUCCEEDED loses that entry from `blocked_by`; one whose list empties returns to PENDING at its floor and lands in a synthetic wave at `max(waves)+1` with `waves.synthetic = 1`; no closed wave is re-opened", with "Files touched: `src/fleet/cli.py`". Two of those criterion clauses and the file list were false; ADR-0090 §8 measures each. The retired text is quoted here on purpose, so a count-based sweep for it finds this quotation — that hit is the retraction, not a survival.)* *(Updated 2026-08-21, W15: the polarity question this row pointed at as open in ADR-0090 §2.4 has been ruled by the round-E orchestrator — R2-CLOSED, recorded in the same ADR section, same commit as this row edit.)* |
| 9 | **Settle and record the step-5 flag semantics** — the un-refusal itself ships with row 10 | S | 7, 8 | `docs/DECISIONS.md` (ADR-0079), this file | ADR-0079 rules `--from-phase` as **option C(i)**, a repo filter: the floor `orchestrator.reentry.phase_floor` computed is never overridden, clamped or capped, and a repo whose floor sits below the flag is **skipped** rather than re-aimed. It rules `--repo` a **consistency ruling** against the six sibling verbs that already declare it (`plan`, `build`, `verify`, `migrate_repos`, `transform`, `pr` — measured by AST at `f4eade0`), scoping §11.5 steps 5, 6 and (later) 8 and **never** the run-wide steps 2, 3, 4 and 7; the same scope governs `--from-phase`. It records as a recital that `--revalidation` / `--raise-revalidation-rounds` **stay refused** (ST1's §13 row 34 and §3.5.1's stub lifecycle, not the re-entry floor). **`--reset-attempts`' semantics are NOT ruled and stay open** (ADR-0079 §8). **Nothing is removed from `_refuse_unbuilt_resume_flags`' refusal set by this row**: with step 8 the sole remaining absence, no ruled flag has a continuation to scope, so the un-refusal ships with row 10 (ADR-0079 §6). *(Corrected 2026-08-22, W24 at ADR-0079's own commit. This row previously read "**Un-refuse the step-5 flags**", S, deps 7, 8, files "`src/fleet/cli.py` (`_refuse_unbuilt_resume_flags`), `tests/test_cli.py`", criterion "`--from-phase` / `--repo` / `--reset-attempts` removed from the refusal set with their semantics fixed by an ADR (ambiguity 4); `--revalidation` / `--raise-revalidation-rounds` stay refused (they are ST1's row 34, not step 5)". The second clause survives as a ruling; the **removal** is what ADR-0079 §6 falsifies, which is why the title and the file list move with it. The retired text is quoted here on purpose, so a count-based sweep for it finds this quotation — that hit is the retraction, not a survival. Row 10 still does not name the un-refusal in its own criterion; that is reported to the dispatcher rather than edited here.)* |
| 10 | **Step 8 — "continue": delegate to the three composition roots in phase order, and un-refuse the two flags ADR-0079 ruled** | L | 9 | `src/fleet/cli.py` (`_refuse_unbuilt_resume_flags`), `tests/test_cli.py`, `docs/DECISIONS.md` | `fleet resume` without `--dry-run` runs `_transform_impl` → `_build_impl` → `_verify_impl` for the phases the floors demand, stops at the first halt, and exits 0; `CommandUnavailableError` is deleted from `resume()`; **no new `PhaseRunner` instantiation is added anywhere**. **The un-refusal ships here**: `cli._refuse_unbuilt_resume_flags`' `unbuilt` dict loses **`--from-phase`** and **`--repo`**, whose semantics are **ADR-0079 §2** (option C(i), a repo filter) and **ADR-0079 §4** (a consistency ruling scoping §11.5 steps 5, 6 and 8) — cited here, deliberately not restated, because a second copy is a second site to keep in step. **The other three entries do not move with them**: `--revalidation` and `--raise-revalidation-rounds` stay refused (ADR-0079 §5 — a §3.5.1 stub-lifecycle question, not a re-entry-floor one), and `--reset-attempts` stays refused because ADR-0079 §8 expressly does **not** rule its semantics, so no row owns removing it until an ADR does. *(Extended 2026-08-22, W27. Nothing above is retracted; this is an addition. Row 9's rewrite at `bd35a54` moved the un-refusal out of row 9 per ADR-0079 §6 without any row taking it on, so the plan had no row naming who edits the refusal set — reported by W24 rather than widened into its own path set, and this criterion now owns it. Measured at `bac5069` under `.venv/bin/python`, by parsing the `unbuilt` dict out of the `git show bac5069:src/fleet/cli.py` blob with `ast`: it has **five** keys and this row removes **two** of them. The dispatch routing this fix said the row must own removing all five; **ADR-0079 §5 and §8 falsify that**, and this criterion is written to the ADR rather than to the dispatch. `tests/test_cli.py` joins the file list because `test_resume_refuses_the_flags_whose_behaviour_does_not_exist` invokes `resume` with `--from-phase 2` and asserts exit 2 — measured at the same ref, not assumed.)* |

**Dependency order.** `1 → 2`; `3 → 4 → 5`; `{1,2} → 6`; `{2,5,6} → 7 → 8 → 9 → 10`.
Tasks 1 and 3 are independent and can start in parallel. Tasks 2 and 3/4/5 are independent after
task 1 lands. **Task 1 is the gate for everything and should go first and alone**, because it
touches `enums.py` and `docs/SPEC.md` and every other task builds on its answer.

None of the ten touches `orchestrator/runner.py`, so none collides with `agent/FD1`.

---

## 6. The single hardest design decision

### The decision: how does a demotion *write*, given that `SUCCEEDED` is mechanically terminal?

This is the hardest because it is the one place where the SPEC and the code state opposite things
in the imperative, and every other task in §5 is downstream of the answer.

**The contradiction, both sides quoted.**

`docs/SPEC.md:180-182` **at this document's declared anchor `7a8bfbb`**:
> a failed precondition **demotes the repo to the earliest phase whose precondition holds**.

> **SUPERSEDED (2026-08-21), lane W2 — the quote above is RETRACTED SPEC wording, preserved because
> the contradiction it half-states is what this section decides.** `60d400b`/`f466287` removed it;
> `docs/SPEC.md` Constraint 7 now states the re-entry floor (the phase *above* the *highest* phase
> below the settled frontier whose evidence holds or which is a `DEGRADED`/`SKIPPED` hard stop). The
> contradiction itself was real and **is resolved**: §5 task 1 landed `models/enums.RESUME_DEMOTE`
> and the `resume=True` gate, so `SUCCEEDED → PENDING` is now legal through `demote()` alone. See
> ADR-0077, and the SETTLED marker on ambiguity 1 in §3.

`src/fleet/models/enums.py:45,48-49`:
> ```python
> RepoStatus.SUCCEEDED: frozenset(),
> ```
> `# Terminal statuses map to the EMPTY set, which is what makes them terminal mechanically`
> `# rather than by prose: no crash sweep can resurrect an abandoned repo into RUNNING.`

with `transition()` (`enums.py:59-68`) documented as "**THE single gate for every status write**
(§6, §11.5)" and raising `ValueError` on anything unlisted. A demotion is by definition a write of
`PENDING` over a `SUCCEEDED` phase row, and there is no legal way to make it.

### Options

**A — extend `ALLOWED_TRANSITIONS` with `SUCCEEDED → PENDING` unconditionally.**
One line. Also destroys the property the comment names: every crash sweep, every reaper, every
`_on_breach` path becomes able to resurrect a completed phase, and the terminality of SUCCEEDED
stops being mechanical.

**B — add a second gated map beside `OPERATOR_REOPEN`, e.g. `RESUME_DEMOTE = {SUCCEEDED:
frozenset({PENDING})}`, reachable only via `transition(old, new, resume=True)`.**
Mirrors an existing, already-blessed precedent: `OPERATOR_REOPEN` exists for exactly this shape of
problem (`RHI → PENDING` for `fleet retry`), is documented as "unreachable from any automatic path
— which is precisely the difference between 'the operator un-abandoned it' and 'the reaper lost
track of it'", and is audited to `findings`. The keyword-only flag makes every demotion site
grep-able, and the default path is unchanged, so no existing caller can demote by accident.

**C — do not demote `SUCCEEDED` at all; restrict step 5 to non-settled rows.**
Requires no enum change and no SPEC edit. But it makes step 5 a no-op in exactly the scenario
Constraint 7 was written for — a Phase-3 repo whose `BUILD.bazel` was reaped is `SUCCEEDED` at
Phase 2 and `SUCCEEDED` at Phase 3, so nothing is demotable and the repo re-enters Phase 4 to test
a package that no longer builds. It converts a spec contradiction into a silent correctness hole,
which is Rule 11's failure mode.

**D — model demotion as data rather than status: leave `phases.status` alone and record a
`resume_floor` column that the schedulers consult.**
Avoids the enum question entirely, but creates a second source of truth for "which phase is this
repo at" beside `phases.status` — Guardrail 4's shadow-state prohibition, and it would have to be
threaded through `WaveScheduler.admit`, `_gated_members` and `projection.py`.

### Recommendation — **B**, with the audit obligation

Take option B: a `RESUME_DEMOTE` map gated on a new keyword-only `resume: bool = False` parameter to
`transition()`, plus a `PhaseDemoted` finding written in the same `StateWriter` unit as the status
change.

Reasoning, in order of weight:

1. **It preserves the property the comment defends while satisfying the SPEC.** A: the automatic
   paths can demote. B: only a caller that says `resume=True` can, and there is exactly one such
   caller (task 6's repository method). The mechanical terminality of SUCCEEDED against the crash
   sweep, the reaper and `_on_breach` is untouched — which is the actual invariant, not "SUCCEEDED
   is never written over by anything".
2. **The precedent is already in the file and already justified.** `OPERATOR_REOPEN` is the same
   construction for the same reason and its comment states the general principle ("unreachable
   from any automatic path"). B is the second instance of a pattern, not a new mechanism —
   Guardrail 1: this is an *Agent Recommendation*, but it is an extension of a decision the
   codebase already took, not a new architecture.
3. **It makes the demotion auditable, which C and D cannot.** A demotion throws away landed,
   green work. `RepoOutcome.checkpoint_rejected` exists precisely because "a rejection means landed
   work was thrown away and that must be visible to whoever reads the wave" (`runner.py:255-261`).
   A demotion is strictly larger than a checkpoint rejection and must be at least as loud. The
   finding is the mechanism; C has nothing to record and D records it in a column nothing surfaces.
4. **It is the smallest change that lets §5's ten tasks be written at all.** Every one of tasks
   2, 6, 7, 8 assumes a legal demotion write.

The cost, stated plainly: `transition()` grows a third parameter and a third map, and the SPEC's
§11.5 step 5 sentence must be amended to say that demotion is a downward search from the settled
frontier over durable evidence, not an ascending scan over `preconditions_hold`. That SPEC edit is
part of task 1 and is not optional — leaving the sentence as written guarantees the next reader
re-derives the broken algorithm.

### The second contradiction, recorded but not the subject of the decision

§11.5 step 5 says "re-check each phase's **declared preconditions**", and Constraint 7 names
`runner.py` as the thing that re-checks them. But `runner.py`'s only precondition mechanism is
`preconditions_hold`, whose own contract (`runner.py:213-214`) says:

> `False` means "it does not — run the phase whole from its anchor" … **Neither verdict ever means
> "skip the work".**

A verdict that never means "skip" cannot select a phase to skip *to*. The two documents describe
different predicates under one name. The design above resolves this by introducing `evidence_holds`
as a distinct, named, durable-state predicate and leaving `preconditions_hold` at its single
documented call site (`_re_entry`) — so there remains exactly one enforcement point for "never blind
replays", rather than two that can disagree.

### The subordinate decision, for the record

**Where the fifth path lives.** Recommend: **nowhere new.** Resume owns a pure state correction over
`phases` (tasks 2–8) and step 8 "continue" delegates to `_transform_impl` / `_build_impl` /
`_verify_impl` in phase order (task 10). A generic `PhaseWalker` that instantiates `PhaseRunner` per
phase would have to re-derive the six axes in §1 — including `_build_impl`'s monorepo checkout,
`IntegrationMutex`, gazelle scratch, ADR-0055 root-file domain and per-wave snapshot re-cut — and
the second copy will drift from the first. Extracting a `PhaseComposer` protocol over the four
composition roots is the right end state but is a refactor of three shipped verbs; record it as a
follow-up ticket, not as part of step 5.
