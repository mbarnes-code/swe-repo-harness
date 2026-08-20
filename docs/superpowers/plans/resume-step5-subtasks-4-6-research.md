> Round-C research artifact, produced by lane R-1 (`design-resume-step5`), promoted unchanged from `.superpowers/` scratch by lane DOCSTALE.

# Research R1 — answers for resume step 5, subtasks 4, 5 and 6

## Evidence provenance

`git status --short` at the start of this investigation, and again after the last file read:

```
?? .superpowers/
```

`git status --short` immediately after this file was written:

```
 M docs/DECISIONS.md
 M src/fleet/cli.py
?? .superpowers/
?? tests/test_backend_registry_gate.py
```

Nothing under `src/`, `tests/` or `docs/` was dirty at any point during the reads that produced the
findings below — the sibling lanes' edits appeared only after the last read. `src/fleet/cli.py` was
in any case read from `git show main:src/fleet/cli.py`, never from the working tree, so those
`M` markers cannot have contaminated a single claim here. `tests/test_backend_registry_gate.py` and
the `docs/DECISIONS.md` delta are sibling-lane work in progress and are **not** reported as defects
anywhere below. The
three sibling lanes named in the dispatch brief (`orchestrator/reentry.py`, `cli.py`,
`docs/DECISIONS.md`) had **not** written to this working tree yet, so every claim below is a claim
about **`main` = `HEAD` = `6a5e5349b912d6bf68b1068a4286d99b4a735f48`**, read from the tree, and
cross-checked for `cli.py` against `git show main:src/fleet/cli.py`. Line numbers for `cli.py` are
`main` line numbers and will drift once the `cli.py` lane lands — anchor on the quoted SQL text,
not the number.

Ref for every claim below unless stated otherwise: **`main`**.

---

# Q1 — `evidence_holds`: are the four durable predicates implementable as written?

## Q1.0 — Two corrections to the design doc before the table

**(a) The design doc's Ambiguity 1 is stale. `SUCCEEDED → PENDING` is no longer forbidden.**
`src/fleet/models/enums.py:63-75` on `main` defines `RESUME_DEMOTE = {SUCCEEDED: {PENDING}}`
(ADR-0077), `transition(old, new, *, operator=False, resume=False)` at `enums.py:117-138` opens it
under `resume=True`, and `demote(old, *, repo_id, phase, reason) -> tuple[RepoStatus, PhaseDemotion]`
at `enums.py:141-163` is the intended entry point. `PHASE_DEMOTED_KIND = "PhaseDemoted"`
(`enums.py:78`) is the `findings.kind` to write. Step 3 of the design doc's algorithm **can** be
written today. Use `demote()`, not `transition(..., resume=True)` — `demote()` refuses `PENDING`,
`RUNNING` and `BLOCKED` sources by name, which is exactly the mis-mint the doc's Ambiguity 5 worries
about, and it is the only place the status and its audit record are produced together.

`PhaseDemotion.payload()` (`enums.py:101-114`) carries its own warning that matters here:
`cli._note_finding` (`cli.py:7084-7100`) fingerprints on `(run_id, repo_id, kind)` **only** and
UPSERTs, so demoting phases 2, 3 and 4 of one repo through three naive `_note_finding` calls
collapses to **one** row. Fold a repo's demotions into one finding, or fingerprint per phase.

**(b) `evidence_holds` cannot read `phases.post_commit_sha` through the repository API.**
`PhaseRow` (`repository.py:179-193`) carries `run_id, repo_id, phase, status, attempts,
max_attempts, lease_owner, lease_fence, lease_expires_at, last_error, updated_at` — and **nothing
else**. `SqliteStateRepository.get_phase` (`repository.py:808-831`) selects exactly those eleven
columns. `base_ref`, `pre_commit_sha` and `post_commit_sha` are **not reachable** through
`ReadOnlyRepository`. They are reachable only by raw SQL against a read connection (the pattern
`state/projection.py:86-95` uses, `_SQL_PHASES`, which does select all three). So `evidence_holds`
must either widen `PhaseRow` or run its own `SELECT` via `cli._rows(conn, sql, params)` against a
`connect_ro(path)` handle. Recommend the latter — subtask 5 is a `cli.py` resident and `_resume_impl`
already opens read handles that way (`cli.py:10073`).

## Q1.1 — The four rows

Common facts:

* `Git` is `src/fleet/vcs/git.py:211`, constructed as `Git(path, runner=..., deadline=..., timeout_s=...)`.
  **No `WorkerContext` is required** — `ctx` is used only to supply `deadline`; passing `None` is legal
  (`git.py:226-227`).
* `Git.resolve(rev) -> str | None` (`git.py:311-326`) — `rev-parse --verify --quiet <rev>^{commit}`.
  Returns `None` **only** for a settled "no such rev"; a probe that never ran or was killed at its
  deadline raises `GitCommandError` (D42/D43, `_require_settled` at `git.py:274`). **This distinction
  is load-bearing for `evidence_holds`: an unsettled probe must not be read as "evidence absent".**
* `Git.ref_exists(ref) -> bool` (`git.py:335-342`) — same settled-or-raise discipline.
* `Git.is_ancestor(ancestor, descendant) -> bool` (`git.py:571-586`) — `merge-base --is-ancestor`,
  settled-or-raise. This is the primitive for "*sha* resolves **on** *branch*", which is strictly
  stronger than `resolve(sha) is not None` (a sha can exist in the object database while being
  unreachable from the branch, e.g. after a `reset --hard` discard).
* `Git.blob_at(rev, path) -> str | None` (`git.py:543-554`) — the blob SHA committed at `path` in
  `rev`, `None` when absent. Reads what a **ref** carries, never a worktree file (D27).
* Per-repo git dir, derivable from settings alone with no `WorkerContext`:
  `(settings.root / settings.config.run.work_dir).resolve() / repo_id` — the shape `cli.py:3820` uses
  in `_prepare_repo`. `run.work_dir` defaults to `"work/"` (`settings.py:218`).
* Monorepo git dir: `(settings.root / settings.config.run.monorepo_path).resolve()` — `cli.py:718`.
  `run.monorepo_path` defaults to `"../acme-monorepo"` (`settings.py:215`).
* Mirror dir: `Path(cache_dir) / f"{slug(repo_id)}.git"` — `workers/clone.py:636-637`, `slug` at
  `sandbox/worktree.py:49`. `run.cache_dir` defaults to `"cache/"` (`settings.py:217`).

---

### Row p=1 — "the mirror is a git dir and the worktree resolves `HEAD`"

**IMPLEMENTABLE without a `WorkerContext`, with one substitution.**

The three facts `CloneWorker.preconditions_hold` checks (`workers/clone.py:274-287`) are:

1. `_mirror_is_initialized(mirror)` — `workers/clone.py:749-752`, module-level sync predicate:
   `(mirror/"HEAD").is_file() and (mirror/"objects").is_dir()`. Importable and callable directly.
2. `_worktree_is_initialized(worktree)` — `workers/clone.py:755-757`: `(worktree/".git").exists()`.
   Importable and callable directly.
3. `self._git(worktree, ctx).resolve("HEAD") is not None` — replace with
   `await Git(worktree).resolve("HEAD") is not None`.

The substitution: `CloneWorker._worktree_path(ctx, payload)` (`clone.py:639-640`) is
`Path(payload.worktree_path or ctx.workdir)` — i.e. `ctx`-derived. But `ctx.workdir` is set at
`orchestrator/context.py:232` from `EngineContext.worktree(repo_id)` (`context.py:206-208`), which is
**`work_dir / repo_id`** — a pure function of settings and `repo_id`. So the resume can compute the
same path with no `ctx`. `cli._prepare_repo` (`cli.py:3820`) already does exactly this.

`_mirror_path` (`clone.py:636-637`) needs `payload.cache_dir`, which is the settings value; no
payload is required.

Note the mirror lives under `cache_dir` and the worktree under `work_dir`, and **neither** carries the
`fleet-<run_id>-` prefix that step 2's reap sweeps (`sandbox/worktree.py:56-58`). Step 2 therefore
cannot delete the tree row 1 inspects. This is what makes the doc's step-2-before-step-5 ordering safe.

**Row absent:** if there is no `phases` row for `(r, 1)` this row reads only the filesystem, so it is
independent of the row. Return `False` (see Q1.2).

---

### Row p=2 — "`migrate/<repo>` exists and `phases(r,2).post_commit_sha` resolves on it"

**IMPLEMENTABLE, but only via raw SQL for the column (Q1.0(b)) and only in the per-repo worktree.**

* The branch name is literally `f"migrate/{repo_id}"` — `cli.py:3836`. Pattern-pinned at
  `models/tasks.py:454` and `workers/prwriter.py:117` as `^migrate/[a-z0-9._-]+$`.
* The git dir holding it is `work_dir / repo_id` (`cli.py:3820`), **not** the mirror. `_prepare_repo`
  creates the branch there with `git checkout -B` (`cli.py:3847`).
* "exists": `await git.resolve(branch)` — `None` means the branch is genuinely absent; `cli.py:3846`
  uses precisely this test.
* "`post_commit_sha` resolves on it": read the column by SQL, then
  `await git.is_ancestor(post_commit_sha, branch)`. `resolve(sha) is not None` alone is **not**
  sufficient — see the common facts above.

**Behaviour on the three degenerate inputs, all of which occur:**

| input | correct verdict | why |
|---|---|---|
| `phases(r,2)` row absent | `False` | no evidence at all |
| `post_commit_sha IS NULL` | `False` | see below — this is the common case |
| branch missing (`resolve` → `None`) | `False` | `_prepare_repo` will recreate it from `HEAD` |
| probe unsettled (`GitCommandError`) | **propagate, do not return `False`** | D42; a demotion is destructive |

`post_commit_sha IS NULL` is **not** an edge case. On `main` there are exactly two writers of that
column, both in `cli.py`, both fenced on `lease_fence`:

* `cli.py:3670-3679` — the Phase-2 transform sink, `UPDATE phases SET post_commit_sha = ?, updated_at = ?`,
  guarded by `if commit is None: return` (`cli.py:3668`);
* `cli.py:6078-6087` — the Phase-3 sink, guarded by `if not output.published_sha: return` (`cli.py:6076`).

`state/repository.py` never writes it (the complete list of `UPDATE phases` / `INSERT INTO phases` in
`src/` is: `repository.py:651, 1037, 1165, 1207, 1252, 1309`; `orchestrator/scheduler.py:280`;
`orchestrator/runner.py:936, 1010`; `cli.py:1993, 2022, 3674, 3878, 3933, 6081, 8812, 9756, 9764, 9851`;
`migrations/v006_*.py:35`, `migrations/v007_*.py:122`). A phase that succeeded through a path that
produced no commit leaves the column NULL and will therefore be demoted. That is the conservative
direction, but the implementer must expect it to fire often and should say so in the finding `reason`.

---

### Row p=3 — "`phases(r,3).post_commit_sha` resolves ∧ `<dest>/BUILD.bazel` present on the integration ref"

**HALF IMPLEMENTABLE. The second clause is NOT what `buildverify.py` checks, and the ref it names is
not on the `phases` row.**

* First clause: same mechanics as row 2, but the sha and the ref live in the **monorepo**
  (`cli.py:718`), not the per-repo worktree, because the Phase-3 sink writes `output.published_sha`
  from the integration merge (`cli.py:6076-6087`).
* Second clause, as `buildverify` actually implements it (`workers/buildverify.py:686-702`):
  `files_present(worktree / payload.dest / "BUILD.bazel")` where `worktree = Path(payload.worktree or
  ctx.workdir)` (`buildverify.py:698-700`). That is a **filesystem** check on a **payload-supplied
  worktree**, not a ref read. It is not reproducible from `phases` alone.
* The ref-based equivalent exists: `Git.blob_at(integration_ref, f"{dest}/BUILD.bazel") is not None`
  (`git.py:543`). But **`integration_ref` is not a `phases` column.** It is
  `attempts.integration_ref` (`state/schema.sql:653-657`), written by a follow-up `UPDATE attempts SET
  integration_ref = ? WHERE attempt_id = ?` at `cli.py:5990-5996`. So the resume must join to
  `attempts` — e.g. the newest `attempts` row for `(run_id, repo_id, phase=3)` with
  `integration_ref IS NOT NULL` — which is one more durable read, and one more way to be `None`.
* `dest` is `repos.dest_path`, which `projection.py:105` reads as `r.dest_path`.

**Verdict:** implementable from durable state, but **only after rewriting the clause** from
"BUILD.bazel is a file on disk" to "BUILD.bazel is a blob reachable from `attempts.integration_ref`".
Do not claim it "mirrors `buildverify.py:688`" — it does not; it is a deliberately stronger, ref-based
restatement. Say so in the ADR. If `attempts.integration_ref` is NULL for every Phase-3 attempt of `r`,
the clause is undecidable and must return `False` (demote), not silently pass.

---

### Row p=4 — "`phases(r,3).status is SUCCEEDED` ∧ a persisted `VerificationReport` for `r`"

**FULLY IMPLEMENTABLE. This is the clean row.**

* First clause mirrors `RdepverifyWorker.preconditions_hold` (`workers/rdepverify.py:183-200`), which
  reads `await ctx.db.get_phase(str(ctx.run_id), ctx.repo_id, Phase.BUILD)`. `get_phase` is on the
  read-only protocol and `PhaseRow.status` **is** in the narrow column set, so this one needs no SQL
  widening. From a `cli.py` context, the equivalent one-line `SELECT status FROM phases WHERE …
  AND phase = 3` is simpler than instantiating a repository.
* Second clause: `cli.VERIFICATION_KIND = "VerificationReport"` (`cli.py:8631`), persisted as a
  `findings` row by `cli._record_verification(writer, run_id, repo_id, report, *, seed, now)`
  (`cli.py:8818-8848`), and read back by
  `cli._verifications(conn, run_id) -> dict[str, tuple[VerificationReport, str]]` (`cli.py:8856-8872`)
  — a **module-level async function taking a plain read connection**. No `WorkerContext`, no dispatch,
  no repository. Call it once per resume, not per repo.
* `prwriter.preconditions_hold` (`workers/prwriter.py:209-225`) additionally requires
  `report.verdict == "PASS"` and `report.repo_id == ctx.repo_id`. `_verifications` is already keyed by
  `repo_id`, so the second is free; decide explicitly whether a persisted `FAIL` counts as evidence.
  Recommendation: it does **not** — a `FAIL` report is evidence the phase must re-run.

## Q1.2 — Reachability, absent rows, and the downward search

* **Reachable without a `WorkerContext` and without a dispatch:** rows 1, 2 and 4 — yes, entirely.
  Row 3 — yes, but only under the rewrite in its section. No row needs a payload, a
  `WorkerContext`, or a worker instance. The only things needed are `FleetSettings`, a read
  connection, and `Git` handles at two derivable paths.
* **What to return when the underlying row is ABSENT (not merely failing): `False`.** Safe, and the
  reason is the traversal, not the predicate: the caller walks **downward** from `frontier-1` to 1 and
  `break`s on the first `True` (design doc §3, the block at ~line 220). A `False` on an absent row can
  therefore only push `floor` **lower**, never higher — it can over-demote, never under-demote.
  Over-demotion costs re-work; under-demotion ships a repo built on evidence that is gone. `False`
  is the conservative direction in every case, and `phases` rows are created eagerly by
  `upsert_phase` (`repository.py:1032-1047`) so a genuinely absent row means the run never planned
  that phase at all.
* **What to return on an UNSETTLED probe: nothing — let `GitCommandError` propagate.** This is the one
  place where `False` is wrong. `Git.resolve` / `ref_exists` / `is_ancestor` all raise rather than
  return a verdict they did not establish (`git.py:274 _require_settled`, D42/D43), and converting
  that raise into a `False` would demote a green repo because a `git` invocation hit its deadline.
  `discard_task` (`vcs/commits.py:357-370`) sets the precedent by raising
  `RollbackIndeterminateError` in the identical situation.

## Q1.3 — Not implementable as written

* **Row 3's second clause**, as literally worded ("mirrors `buildverify.py:688`"). `buildverify` reads
  a worktree file, not a ref. Implement the ref version and record the divergence.
* **Nothing else.** No substitute mechanism is needed or invented.

---

# Q2 — step 4 (Git-as-arbiter): what makes `attempts` byte-identical?

**First, disambiguate the name.** `attempts` is both a `phases` **column**
(`state/schema.sql:372`, `INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0)`) and a **table**
(`state/schema.sql:640`). Subtask 4's acceptance says "asserted on the column", so the assertion
target is `phases.attempts`. Assert it per `(run_id, repo_id, phase)`.

## Q2.1 — Who touches `phases.attempts`

**Exactly one writer in the whole of `src/`:** `SqliteStateRepository.complete_phase`
(`repository.py:1228-1281`). Its SQL (`repository.py:1252-1261`) opens with
`SET attempts = attempts + 1, …` unconditionally, and in the same statement escalates to
`REQUIRES_HUMAN_INTERVENTION` when `? = 1 AND attempts + 1 >= max_attempts`.

Every other `phases` writer leaves the column alone. Verified by enumerating all `UPDATE phases` /
`INSERT INTO phases` in `src/` (list in Q1 row 2) and reading each:

| writer | file:line | touches `attempts`? |
|---|---|---|
| `upsert_phase` | `repository.py:1032-1047` | No — `ON CONFLICT … DO UPDATE SET max_attempts, updated_at` only |
| `acquire_phase_lease` | `repository.py:1148-1188` | No |
| `renew_phase_lease` | `repository.py:1190-1226` | No |
| `complete_phase` | `repository.py:1228-1281` | **YES, `+ 1`** |
| `reap_expired_phase_leases` | `repository.py:1282-1329` | No |
| the reservation-fence bump | `repository.py:651` | No |
| `PhaseRunner._write_status` | `runner.py:934-942` | No |
| `PhaseRunner._record_diagnostics` | `runner.py:1008-1016` | No |
| scheduler `blocked_by` write | `scheduler.py:280` | No |
| `_RESET_RUNNING_TO_PENDING_SQL` (abort + resume step 3) | `cli.py:9851-9855` | No — and `cli.py:9836-9843` is a comment block explaining that the omission is the point |

**No timestamp side effect matters either**, but note that essentially every writer above sets
`updated_at`. If subtask 4's test asserts "byte-identical" on a `SELECT *`, it will fail on
`updated_at` alone. Assert on the `attempts` column specifically, exactly as the acceptance criterion
says, and make the test's `SELECT` name that column.

**So the design doc's §4 claim is correct and now measured:** there is no existing resume code path
that bumps `phases.attempts`. Subtask 4 satisfies "without incrementing `attempts`" by *not calling*
`complete_phase`, and the property is verified by a test, not a fix. The mutation that makes such a
test discriminating is trivially available: swap the step-4 status write for a `complete_phase` call
and the assertion must go red.

## Q2.2 — What `discard_task` does today

`vcs/commits.py:344-397`. Signature: `async def discard_task(git: Git, *, task_pre_commit_sha: str,
branch: str | None = None) -> None`.

It is **pure git and touches no database at all** — no `attempts` column, no `attempts` table, no
`phases`, no `tasks`. In order:

1. refuses an empty `task_pre_commit_sha` with `RollbackAnchorError` (`commits.py:353-357`) — the
   phase anchor is explicitly not a substitute;
2. `git.resolve(task_pre_commit_sha)`, converting an unsettled probe to `RollbackIndeterminateError`
   (`commits.py:358-368`) and a settled miss to `RollbackAnchorError` (`commits.py:370-373`);
3. `git.is_ancestor(anchor, branch or "HEAD")`, again unsettled → `RollbackIndeterminateError`
   (`commits.py:375-381`), and **refuses to reset when the anchor is not an ancestor**
   (`commits.py:382-386`) — this is the guard that stops a mis-passed phase anchor from deleting
   earlier `DONE` tasks' commits;
4. `git.reset_hard(anchor)` then `git.clean(directories=True, ignored=True)`, with any failure
   converted to `RollbackIndeterminateError` (`commits.py:387-397`).

The companion primitives step 4 drives, confirmed present with these exact signatures on `main`:

* `find_task_commit(git: Git, *, branch: str, pre_commit_sha: str, task_id: UUID | str) -> str | None`
  — `commits.py:298-310`
* `commits_in_range(git: Git, *, branch: str, pre_commit_sha: str)` — `commits.py:312-332`
* `record_task_anchor(git: Git, branch: str) -> str` — `commits.py:334-342`
* `rollback_phase(git: Git, *, branch: str, phase_base_ref: str) -> str` — `commits.py:400`
* `scoped_range(pre_commit_sha, branch) -> str` — `commits.py:210-221`, raises on an empty
  `pre_commit_sha` rather than searching the branch's whole history

## Q2.3 — One `StateWriter` unit for `phases.post_commit_sha` + `attempts.commit_sha` + the status change

**Yes. One unit is one `BEGIN IMMEDIATE` transaction, and a unit may issue any number of statements.**

`StateWriter.submit(unit)` (`state/db.py:386-396`) enqueues one `WriteUnit`; `_serve`
(`db.py:413-422`) runs it through `_run_with_busy_retry` (`db.py:424-442`), which calls
`_run_in_immediate(conn, unit)` (`db.py:431`). The class docstring is explicit: *"One queue, one task,
one connection, every unit `BEGIN IMMEDIATE`"* (`db.py:295`). `reap_expired_phase_leases`
(`repository.py:1317-1327`) is the worked precedent — five `conn.execute` calls in one unit, and its
docstring states that the release and the fence bump "commit together or not at all".

So subtask 4 writes one `async def unit(conn)` containing, in whatever order it needs:

```
UPDATE phases   SET post_commit_sha = ?, status = ?, updated_at = ? WHERE run_id=? AND repo_id=? AND phase=?
UPDATE attempts SET commit_sha = ?                                   WHERE attempt_id = ?
UPDATE tasks    SET status = 'DONE'                                  WHERE task_id = ?
```

Notes on each target:

* **`phases.post_commit_sha`** — the two existing writers (`cli.py:3674`, `cli.py:6081`) both carry
  `AND lease_fence = ?`. Step 4 runs from `fleet resume` and holds **no lease**, so it cannot supply a
  fence. Decide explicitly: either omit the fence (correct, because step 3 has already reclaimed every
  stale lease and bumped its fence, so no live worker owns the row) or read the current fence and pass
  it. Recommend omitting it and stating why in the ADR — a resume-owned write fenced on a fence
  nobody granted is a fence in name only.
* **`attempts.commit_sha`** — written today only through `record_attempt`
  (`repository.py:1671-1721`), whose `INSERT … ON CONFLICT … DO UPDATE` set-list is
  `exit_code, failure_class, stdout_tail, stderr_tail, finished_at` and **does not include
  `commit_sha`** (`repository.py:1685-1689`), so an upsert cannot correct a stale sha. A direct
  `UPDATE attempts SET commit_sha = ? WHERE attempt_id = ?` is the only way, and it is precedented:
  `cli.py:5990-5996` does exactly that shape for `integration_ref`. `record_attempt` touches only the
  `attempts` table — it never touches `phases.attempts`.
* **`tasks.status = 'DONE'`** — there is **no** existing writer. The only `UPDATE tasks` anywhere in
  `src/` is `repository.py:1114`, inside `claim_next_task`. The `tasks.status` domain is
  `('PENDING','CLAIMED','RUNNING','DONE','FAILED')` (`schema.sql:596-597`). Subtask 4 writes this
  statement itself; nothing to reuse and nothing to collide with.

## Q2.4 — What recreates a missing `phases.base_ref`

**The path exists, but only for Phase 2 and only inside `fleet migrate`.**

`cli._prepare_repo` (`cli.py:3792-3894`) is the whole of it. The relevant block is `cli.py:3861-3884`:

1. `anchor_ref = f"refs/fleet/{run_id}/{repo_id}/phase-{int(Phase.TRANSFORM)}/base"` (`cli.py:3861`) —
   the format `schema.sql:413-414` declares;
2. `anchor = await git.resolve(anchor_ref)`; if `None`, `anchor = await git.rev_parse(branch)` then
   `await git.update_ref(anchor_ref, anchor, message="fleet phase-2 anchor")` (`cli.py:3862-3865`);
3. one `StateWriter` unit: `UPDATE phases SET base_ref = ?, pre_commit_sha = ?, started_at =
   COALESCE(started_at, ?), updated_at = ? WHERE run_id = ? AND repo_id = ? AND phase = ?`
   (`cli.py:3876-3884`). Note: **no `lease_fence` predicate** — precedent for the recommendation in
   Q2.3.

Limits the implementer must know:

* **`Phase.TRANSFORM` is hardcoded** at `cli.py:3861`. There is no equivalent for phases 1, 3 or 4, and
  no `phases.base_ref` is ever written for them by any code in `src/`.
* Its only caller is `cli.py:4365`, inside the `fleet migrate` planner. It is **not** reachable from
  `_resume_impl`.
* It refuses rather than recreates in two cases: no Phase-1 worktree at `work_dir/<repo_id>`
  (`cli.py:3821-3826`), and a `migrate/<repo>` branch whose tip carries a foreign `Fleet-Run-Id`
  trailer (`cli.py:3850-3859`) — the latter raises `TransformStepUnavailableError` and says in its own
  message that the reconciliation §3.2 step 6 describes "is not implemented here".
* The one other `base_ref` writer in the tree is the v006 migration back-fill
  (`migrations/v006_mutations_deleted.py:34-36`), which only touches rows that already have a
  non-NULL `pre_commit_sha`. It is not a runtime path.

**So: if subtask 4 needs a `base_ref` for a phase other than 2, it must write that path itself.**
For Phase 2 it can either call `_prepare_repo` (heavy — it also does dirty-worktree discard, branch
checkout and a source scan) or lift the six-line anchor block. Lifting is Rule 2; calling drags in
`TransformStepUnavailableError` semantics the resume has no story for.

---

# Q3 — `live_names` for step 2's reap

## Q3.1 — The consumers

Both take `live_names` as a **required keyword**, and both do an **exact set-membership** test:

* `WorktreeManager.reap(*, live_names: Iterable[str], deadline=None, timeout_s=120.0) -> ReapResult`
  — `sandbox/worktree.py:303-337`. Filter: `if not name.startswith(prefix) or name in live: continue`
  (`worktree.py:329-330`), where `prefix = run_prefix(self.run_id)` and `name = path.name` from
  `list_registered()` (`worktree.py:277-301`, `git worktree list --porcelain`).
* `ContainerSandbox.reap(*, run_id: UUID | str, live_names: Iterable[str], timeout_s=30.0) -> ContainerReapResult`
  — `sandbox/container.py:322-357`. Filter: `if name in live: continue` (`container.py:350-351`),
  over `list_by_prefix(run_prefix(run_id))` (`container.py:299-321`).

Both return a result object with `.reaped` and `.failed`; `worktree.py:315-321` and
`container.py:330-344` both explain that swallowing `.failed` is the D44 four-state collapse. Surface
it (this is already in the subtask's acceptance criteria).

## Q3.2 — The name to put in the set

`sandbox_name(run_id, repo, attempt) -> f"{run_prefix(run_id)}{slug(repo)}-{attempt}"` —
`sandbox/worktree.py:61-66`. `run_prefix(run_id) = f"fleet-{slug(str(run_id))}-"` —
`worktree.py:56-58`. `slug` is `worktree.py:49-53`.

**`attempt` is `phases.attempts + 1`, not `phases.attempts`.** Stated three times in
`orchestrator/runner.py`: the module docstring (`runner.py:28-29`, "the dispatched
`WorkerContext.attempt` is `phases.attempts + 1` read off disk"), `_dispatch`'s docstring
(`runner.py:673`), and the computation `attempt = ladder.attempts + 1` (`runner.py:686`), where
`ladder.attempts` is seeded from `row.attempts` at `runner.py:436`. Getting this off by one reaps the
live sandbox and spares a dead one.

So: `live_names = {sandbox_name(run_id, row.repo_id, row.attempts + 1) for row in live_rows}`.

## Q3.3 — Which `phases` rows are "still holding a lease"

**There is no single column and no accessor.** The lease is four columns on `phases`
(`schema.sql:394-410`): `heartbeat_at`, `heartbeat_ttl_seconds`, `lease_owner`, `lease_fence`,
`lease_expires_at`, gated by `status`. The load-bearing ones:

* `status = 'RUNNING'` — the only status under which a lease is held; `ix_phases_lease`
  (`schema.sql:800-801`) is a partial index `WHERE status = 'RUNNING'`, which is the reaper's only scan.
* `lease_owner` — `'{host}:{container_id}:{pid}:{boot_uuid}'` (`schema.sql:403-408`), NULLed by every
  release path.
* `lease_expires_at` — NULLed by every release path.

`PhaseRow` (`repository.py:179-193`) exposes `status`, `attempts`, `lease_owner`, `lease_fence` and
`lease_expires_at`, so `ReadOnlyRepository.get_phase` **can** answer this per repo — but there is
**no** "iterate the phases of a run" accessor anywhere in `src/fleet/state/`. `ReadOnlyRepository`
(`repository.py:345-368`) offers `iter_symbols`, `iter_edges`, `iter_events`, `iter_attempts` and a
single-row `get_phase`, and nothing else. So step 2 must issue its own `SELECT repo_id, attempts FROM
phases WHERE run_id = ? AND status = 'RUNNING'` through `cli._rows` against a `connect_ro` handle,
matching what `_count_stale_running` (`cli.py:10282-10292`) already does.

**Recommended predicate:** `status = 'RUNNING'` alone, **provided step 2 runs after step 3** (below).
Adding `AND lease_owner IS NOT NULL` costs nothing and documents the intent.

## Q3.4 — What a crashed run leaves behind, and the ordering

A crashed worker leaves its `phases` row at `status = 'RUNNING'` with `lease_owner`,
`heartbeat_at` and `lease_expires_at` all still populated and now stale, plus whatever sandbox it had
created. Nothing clears them at process death — that is precisely why the reaper and step 3 exist
(`schema.sql:427-436`).

**Order in `_resume_impl` on `main`** (`cli.py:10058-10195`), in source order:

1. `cli.py:10072-10099` — drift / profile / wave-ceiling refusals, in one read handle
2. `cli.py:10104-10113` — `_refuse_bad_raise_budget`, `_record_drift_findings`,
   `_raise_wave_ceiling`, `_raise_run_ceiling`
3. `cli.py:10128-10140` — `if repoll_prs and not dry_run: await _pr_sync_impl(...)`
4. `cli.py:10143-10157` — the 9-line reserved-slot comment for `stub_reconcile`
5. `cli.py:10161-10169` — `now = _now()`;
   `horizons = (_iso(now - timedelta(seconds=settings.config.run.stale_after_s)), _iso(now))`;
   then `_count_stale_running` under `--dry-run` (`cli.py:10166`) **or** `_reset_stale_running`
   (`cli.py:10169`) — **this is step 3**
6. `cli.py:10171-10173` — `project_once(path, run_id=UUID(run_id), path=DEFAULT_PROJECTION_PATH)`,
   non-dry-run only — step 7
7. back in `resume()` — `CommandUnavailableError` naming step 5

**There is no step-2 slot on `main`.** `_resume_impl` never mentions a worktree or a container.

**`_reset_stale_running` has NOT cleared anything by the time §11.5's numbering would run step 2**,
because §11.5 numbers the reap as step 2 and the stale sweep as step 3. That ordering is wrong for the
reap, and this is the single most important thing to tell the lane:

* `_RESET_RUNNING_TO_PENDING_SQL` (`cli.py:9851-9855`) sets
  `status='PENDING', lease_owner=NULL, heartbeat_at=NULL, lease_expires_at=NULL,
  lease_fence=lease_fence+1`, gated by `_STALE_HEARTBEAT_PREDICATE` (`cli.py:9891-9896`), which
  requires `heartbeat_at IS NOT NULL AND heartbeat_at < <config cutoff> AND (julianday(now) -
  julianday(heartbeat_at)) * 86400.0 > heartbeat_ttl_seconds` — stale by **both** clocks.
* **If step 2 runs before that**, every crashed row is still `RUNNING` with a live-looking
  `lease_owner`, so `live_names` spares its sandbox. The reap then does nothing for exactly the
  worktrees and containers it exists to collect, on exactly the run — a crashed one — where it matters.
  Not a safety bug; a total no-op, which is worse because it looks like it worked.
* **If step 2 runs after**, only genuinely-live rows are still `RUNNING`, and `status = 'RUNNING'` is
  a correct and sufficient liveness test.

**Recommendation: place step 2 immediately after `_reset_stale_running` (`cli.py:10169`) and before
`project_once` (`cli.py:10171`), i.e. in the same slot subtask 5 wants, sequenced ahead of the
demotion.** This inverts §11.5's own step numbering; that is a real SPEC conflict and needs its own
ADR line rather than being done quietly. Under `--dry-run` the sweep does not run (`cli.py:10166`
counts instead), so a dry-run step 2 sees stale rows as live — harmless, since `--dry-run` reaps
nothing, but the emitted preview count will understate what a real resume would collect. Say so in the
emitted line.

## Q3.5 — Three defects in the reap surface, all on `main`

**(a) `WorktreeManager` is never instantiated anywhere in `src/`.** `grep -rn "WorktreeManager("
src/fleet --include=*.py` returns nothing but the class definition. Nothing in the shipped harness ever
calls `WorktreeManager.create` (`worktree.py:146`) or `path_for` (`worktree.py:144`), so **no
`fleet-<run_id>-*` worktree directory is ever created**. Meanwhile the real per-phase worktree is
`EngineContext.worktree(repo_id) = work_dir / repo_id` (`context.py:206-208`), which carries no
`fleet-` prefix and is not a registered git worktree of the mirror in the way `list_registered`
expects. **A worktree reap wired today sweeps an empty namespace.** That is not a reason to skip it —
it is a reason the subtask's test must build the `fleet-<run_id>-*` worktrees it then reaps, and a
reason the emitted line must not be read as evidence that anything was leaking.

**(b) `.reap(` has no caller anywhere in `src/`.** Both reap methods are unwired today. So is
`docs/`-level expectation to the contrary: check before claiming a regression.

**(c) `live_names` built from `sandbox_name` alone will kill a live buildverify container.**
`ContainerSandbox.reap` compares names by **equality** (`container.py:350`), but `BuildverifyWorker`
does not name its containers `sandbox_name(...)`. It uses
`_invocation_name(ctx, suffix="") = f"{_container_prefix(ctx)}{uuid4().hex[:8]}{suffix}"`
(`buildverify.py:354-370`) where `_container_prefix(ctx) = f"{sandbox_name(ctx.run_id, ctx.repo_id,
ctx.attempt)}-t"` (`buildverify.py:341-351`). Those names *start with* `sandbox_name(...)` but never
equal it, so they land in neither branch of the `in live` test and are force-removed while the build
is running. `RdepverifyWorker` is the opposite case — `on_cancel` removes exactly
`sandbox_name(ctx.run_id, ctx.repo_id, ctx.attempt)` (`rdepverify.py:331`), matching
`ContainerSpec`'s default name (`container.py:104, 115`), so equality works there.

**Fix for (c): either** make `ContainerSandbox.reap`'s spare-test a prefix test rather than an
equality test, **or** — Rule 3, since the reap is not this subtask's to redesign — have step 2 pass
both forms for every live row:

```
{sandbox_name(run_id, repo_id, attempts + 1),
 sandbox_name(run_id, repo_id, attempts + 1) + "-t"}
```

The second still will not match, because the real name has a random token *after* the `-t`. So the
prefix test is the only correct fix, and step 2 cannot be landed safely without it. **Flag this to the
orchestrator before implementing:** it is a change to `sandbox/container.py`, outside the file list
the subtask names, and it is the difference between a reap and a build-killer. `buildverify.py:341-350`
already documents why `-t` exists and that `list_by_prefix` is a regex anchored at the start — the
prefix semantics are the ones the naming scheme was designed for; `reap`'s equality test is the part
that did not keep up.

---

## Summary of what could not be established

* **Nothing was unanswerable from durable state.** Every question has an answer above.
* Two answers are "yes, but the design doc's wording is wrong": Q1 row 3's second clause (worktree
  file vs. ref blob) and Q1's Ambiguity 1 (`RESUME_DEMOTE` has landed since the doc was written).
* One answer is a genuine SPEC conflict rather than a code fact: Q3's ordering. §11.5 numbers the reap
  as step 2 and the stale sweep as step 3; the reap is a no-op in that order. Resolving it is an ADR
  decision, not a research finding.
