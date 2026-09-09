# Task 106 report — D132: owner-side subtraction, so a hoisted contract's sources land once

Round VI, thirty-seventh wave. Branch `agent/roundvi-task106`, worktree at
`/home/redmage/swe repo harness worktrees/wt-roundvi-task106`. Commit range: `6ae84c7..fb20f52`
(one commit, `fb20f52`, on top of `main`'s `6ae84c7`).

## Scope recap

`docs/INTEGRATION_HONESTY.md`'s `D132` entry and `docs/DECISIONS.md`'s JC-4 discussion (round VI
task 95's ADR draft) both describe the same disclosed, bounded gap: SPEC §3.3 step 1
(`docs/SPEC.md:1270-1274`) requires a `HOISTED`/`MIGRATED` contract's owner to be ingested "with
one subtraction: its relocation plan excludes every path already claimed by a `HOISTED`
contract... The contract's commits therefore appear once, on the contract merge, and are not
duplicated onto the owner's merge." Task 95 built the contract-side half
(`cli._ingest_contract_source`) but left the owner-side subtraction unbuilt: `_ingest_build_source`
still relocated the owner's WHOLE tree, so a hoisted contract's carrier paths landed on
`integration` twice — once at `hoist_target_path`, once again under the owner's own `dest`. This
task builds that missing subtraction.

## What was built

`src/fleet/cli.py` only (no other `src/` file touched):

1. **New `_contract_owner_paths(cnode: ContractNode) -> tuple[str, ...]`** — factored out of
   `_ingest_contract_source`'s existing inline expression (`[entry["path"] for entry in
   cnode.source_paths if entry.get("repo_id") == cnode.owning_repo_id]`, normalized through
   `default_source_paths`). Now the ONE shared computation both `_ingest_contract_source`'s
   INCLUDE filter (`--path`, unchanged) and `_ingest_build_source`'s new EXCLUDE filter read, so
   the include-set and the exclude-set cannot drift apart the way two independently-written
   filters could.

2. **`_ingest_build_source` gains `excluded_contract_paths: Sequence[str] = ()`** (default keeps
   every other call site behavior-identical — there is only one production call site, in
   `_build_impl`, updated below). For each excluded path, the function now adds BOTH the raw form
   and the `<dest>/`-prefixed form to a `--path` list, plus one trailing `--invert-paths`,
   appended to `RelocationSpec.extra_args`. Both forms are necessary and this is not
   over-engineering: the clone's history is mixed between commits behind Phase 2's relocation
   (raw repo-root paths) and the tip (already `<dest>/`-prefixed, since Phase 2 already moved the
   worktree by committing renames) — `--path`/`--invert-paths` filters on each commit's OWN path
   as it existed in that commit, never on the later `--path-rename` result — so this mirrors the
   *existing* two-rule idempotency trick immediately above it in the same function (the
   `--path-rename` pair that roots every historical path at `<dest>/` and then collapses the one
   prefix already rooted there). I verified this empirically, not just by argument — see
   "What I got wrong first" below.

3. **`_build_impl`'s PASS 0 loop** (the one that calls `_ingest_contract_source` for every
   eligible `HOISTED`/`MIGRATED` contract) now also records, in a new `else:` branch that only
   runs on a **successful** ingest, `cnode.owning_repo_id -> _contract_owner_paths(cnode)` into a
   new `hoisted_owner_paths: dict[str, list[str]]`. Deliberately gated to the success path only,
   never the `except:` arm: a contract whose ingest FAILED has no merge on `integration` yet, so
   excluding its paths from the owner's own relocation would delete the only copy of that content
   from the monorepo entirely — a strictly worse outcome than today's disclosed, bounded
   duplication. PASS 1's `_ingest_build_source` call is then threaded
   `excluded_contract_paths=hoisted_owner_paths.get(repo_id, ())`.

**JC-4's two candidate shapes, and why I built a hybrid rather than picking one.** JC-4 named (1)
`--invert-paths`/`extra_args` at the git-filter-repo level, and (2) a direct read of the
`contracts` table to learn which paths are already claimed. Reading the real code before choosing
(per the brief's instruction not to assume): shape 2 alone cannot work standing alone, because
`RelocationSpec.source_paths` is a KEEP-list (`--path`, "Paths to KEEP... Non-empty is the
hoisted-contract case"; `filter_repo.py`'s own docstring) — expressing "the owner's whole tree
MINUS these paths" as a keep-list would require enumerating every other path in the owner's
entire tree, which nothing in this codebase does or could do without a full tree walk at ingest
time. Shape 1 alone (an ad-hoc `--invert-paths` with no design for WHERE the excluded paths come
from) isn't a complete answer either. What actually closes the gap is shape 2's mechanism (a
DB-derived read — here, reusing PASS 0's own already-materialized `ContractNode`s rather than a
fresh SQL query, since the join `_ingest_contract_source` needs is already resolved by
`_eligible_contract_units` before PASS 0 runs) supplying the path set, and shape 1's mechanism
(`--invert-paths`/`extra_args` at the filter-repo invocation) being the only way to apply it. This
is smaller than either shape read in isolation: no new SQL, no new join, only a dict threaded
between two loops that already run adjacently in `_build_impl`.

`graph/collisions.py`'s dead `resolution = f"hoisted:{contract_id}"` route was NOT touched or
revived — confirmed still unreached before starting (`audit_collisions` is still called with no
`files=` argument anywhere in `src/fleet/`, matching every prior task's measurement this round).

## What I got wrong first, and how I caught it (Rule 12 / Guardrail 6 discipline)

My first version of the proof test asserted directly against `git ls-tree -r
migrate/acme-identity` (the OWNER's own branch name) for absence of the contract's carrier path.
That assertion is **wrong even under a correct fix** — I ran it and it failed on the FIRST attempt
of the fix I had already written and hand-verified via a separate debug trace, which is what
caught it. The reason: `filter_repo.ingest()` force-moves `migrate/<repo_id>` to the **merge
commit itself** (`git.create_branch(branch, merge_sha, force=True)`, `src/fleet/vcs/
filter_repo.py:493`; this is JC-2's own documented behavior), not to the pre-merge fetched clone
HEAD. A merge commit's tree is `integration`'s whole CUMULATIVE state at that point — including
the contract's own PASS-0 merge, inherited via the merge's first parent — so `migrate/
acme-identity`'s `ls-tree` shows the contract's carrier path regardless of whether the owner-side
subtraction is built or not. I re-derived the correct check by diffing the owner's merge against
its own first parent (`git diff --name-only <merge>^1 <merge>`) — this isolates exactly what THAT
merge introduced onto `integration`, which is the actual site the fix touches. I verified this
distinction is real, not just reasoned about, by running the wrong assertion, seeing it fail
against known-good code, tracing why, and only then writing the corrected version — the finished
test file never contains the wrong assertion; a docstring comment inside the landed test explains
why the naive check is a false positive, so a future reader does not reintroduce it.

## Proof

`tests/test_build_e2e.py::test_a_hoisted_contracts_carrier_path_lands_on_integration_exactly_once`
— placed immediately after the existing (task 95) section-7b test
`test_a_hoisted_contracts_content_is_really_merged_with_the_trailer`, reusing the identical real
fixture (`CYCLE_FLEET`/`PROTO_ID`/`_make_hoist_ingest_workspace`, the same real `git-filter-repo`
binary, no seeded `contracts` row — the same organic hoist that fixture already proves). It:

1. Runs `scan`/`sequence`/`transform`/`build` for real (fake `RESOLVER_RUNNER`/`BAZEL_RUNNER`
   only — matching every other test in this file's section 7b/7c convention; `FILTER_REPO_RUNNER`
   stays `None`, i.e. the real binary).
2. Anti-vacuity: confirms no `ContractIngestFailed` finding, confirms exactly one
   `Hoisted-Contract:` trailer merge exists.
3. **The headline assertion**: `git ls-tree -r --name-only` of the final `integration` tip,
   filtered to paths ending `identity.proto`, equals exactly
   `["proto/acme/identity/v1/identity.proto"]` — the contract's carrier path exists exactly once
   on the whole branch, not duplicated anywhere else. This is real `git` state read after a real
   CLI run, never an in-memory `RelocationSpec`.
4. **Direct site evidence**: diffs the owner's own merge (`migrate/acme-identity`) against its own
   first parent and asserts no path ending `identity.proto` appears in that diff — proving the
   OWNER's merge itself does not (re-)introduce the contract's path, which is the actual
   mechanism the fix touches (see "what I got wrong first" above for why this is diffed against
   the first parent rather than read as a plain `ls-tree`).
5. **Anti-over-exclusion check**: the same owner-merge diff DOES contain the owner's other,
   non-hoisted files (`package.json`, `src/index.ts`, under a `owner_dest` derived from the diff
   itself — never hardcoded — by finding the one `package.json` path this merge adds), proving
   the exclusion filter did not disable relocation altogether.
6. Confirms the contract's own merge is unaffected (`git cat-file -e` against the hoist merge sha
   for the contract's file).

## Rule 12 mutation

Applied: changed `for claimed_path in default_source_paths(excluded_contract_paths):` to iterate
`default_source_paths(())` instead (a no-op — the exclusion set is always empty, reproducing
pre-fix behavior). Confirmed the mutation actually changed the file (`git diff --numstat --no-index
<backup> <mutated>` reported exactly 1 changed line before running). Result: the new test's
headline assertion (step 3 above) reddens specifically —

```
AssertionError: D132: a hoisted contract's carrier path must appear exactly once on `integration`,
not duplicated onto its owner's own merge: ['proto/acme/identity/v1/identity.proto',
'ts/acme/identity/proto/acme/identity/v1/identity.proto']
```

— showing both the contract's own copy and the exact duplicate this fix eliminates
(`ts/acme/identity/proto/acme/identity/v1/identity.proto`, the owner's `dest`-prefixed relocation
of the same file). Reverted immediately after (`cp` from a pre-mutation backup, then re-verified
`ruff`/`mypy`/the test green again); `git diff --stat src/` is empty except this task's actual
fix at the final commit.

## Covering set run

All run inside the provisioned worktree (`.venv` correctly repointed at this worktree's own
`src/` — verified via the `_editable_impl_fleet.pth` content before trusting any result, per
CLAUDE.md's worktree-import-isolation guardrail):

- `tests/test_build_e2e.py::test_a_hoisted_contracts_carrier_path_lands_on_integration_exactly_once`
  — 1 passed (new test).
- `tests/test_hoist_rollback_git.py` + `tests/test_hoist_rollback_wiring.py` +
  `tests/test_transform_e2e.py` — 41 passed.
- Whole `tests/test_build_e2e.py` (includes the two existing hoist e2e tests this task's fix
  sits beside, plus everything else in that ~8800-line file) — **87 passed in 831.89s (~13m52s)**,
  `bazel disk` line clean (peak 4.19 GiB / ceiling 6 GiB, residual output bases 0 bytes).
- `ruff check src/fleet/cli.py tests/test_build_e2e.py` — all checks passed.
- `python -m mypy` (whole package, no path arguments — the scoping this project's own CLAUDE.md
  Guardrail 6 requires be stated) — Success: no issues found in 130 source files.

I did not re-run the FULL suite (`docs/PROGRESS.md`'s ~15-minute full run) — the brief's own
covering-set instruction names `test_hoist_rollback_wiring.py` and "any other tests touching
`_ingest_build_source`/`_ingest_contract_source`", which I found by grep
(`tests/test_hoist_rollback_git.py`, `tests/test_hoist_rollback_wiring.py`,
`tests/test_build_e2e.py`, `tests/test_transform_e2e.py` — all four run above) and confirmed
`_ingest_build_source` has exactly one production call site (`_build_impl`, the one I changed).

## Concerns

- None load-bearing. The change is additive (`excluded_contract_paths` defaults to `()`, so every
  existing non-hoist repo's relocation is byte-identical to before — confirmed by the 87/87 green
  full-file run, which includes many non-hoist e2e builds).
- I did not update `docs/INTEGRATION_HONESTY.md`'s `D132` entry or `docs/CRITERIA_PLAN.md`'s §31
  entry to mark them closed. This matches this project's own established convention (verified
  against `d1ecfdf`, the round VI task 100 commit that fixed `D133`: that commit's own message
  states "D133 allocated in docs/INTEGRATION_HONESTY.md in a follow-up commit, referencing this
  sha" — i.e. the ledger update is a separate, controller-driven commit, not part of the fixing
  worker's own commit) and the brief's own workflow section, which does not list a docs-update
  step. Leaving that update for the controller.

## Does this close D132, and thereby §12.31 as a whole?

**Yes, I believe so, verified against both primary sources rather than trusted from the brief's
own framing:**

- `docs/INTEGRATION_HONESTY.md`'s `D132` entry (searched fresh, not assumed): "Not yet built: the
  owner-side subtraction itself. Two candidate shapes, neither chosen here: excluding the
  contract's own carrier paths from the owner's `RelocationSpec` via `--invert-paths`/
  `extra_args`, or a direct per-owner read of the `contracts` table at ingest time." This task
  builds exactly that subtraction (a hybrid of both named shapes, per the reasoning above) and
  proves it end-to-end against real `git` state on `integration`, with a mutation that reddens
  the new test specifically.
- `docs/CRITERIA_PLAN.md`'s §31 entry (re-read at this branch's `HEAD`, not carried forward from
  the brief): "**What remains open for §12.31 as a whole: `D132`**... **Do not read §12.31 as
  DONE until D132 closes; it is the sole remaining sub-question.**" This is dated round VI task
  101/104 (2026-09-09), i.e. current as of the branch point this task started from — I re-read it
  myself rather than trusting the brief's paraphrase of it, and it says the same thing the brief
  claims.

I have not audited every one of §12.31's five sub-legs (A-E, the un-hoist/`--forbid-hoist`
machinery `docs/CRITERIA_PLAN.md` also tracks) from scratch — I am relying on the ledger's own
current, dated statement that D132 is the sole remaining gap for §12.31 as a *criterion* (case
(i) and case (ii)'s reachable-trigger clauses are separately recorded DONE by tasks 55/96/101/104,
per the same entry). If that ledger statement is accurate — and it reads as a carefully
cross-checked claim, not an offhand one, complete with its own superseded-framing history — then
this task's fix, once merged and the ledger updated, closes both D132 and §12.31 as a whole.
