# CRITERIA_PLAN.md — SPEC §12 Closure Plan

> **Addendum — 2026-08-30, round M controller.** Round K (already reflected above) and an
> untracked round L landed between this file's authorship and this addendum — 21 commits,
> `12be741..fa95469`, none logged in `docs/PROGRESS.md` (a Rule 10 gap this addendum does not fix,
> flagged for a documentation task). Re-verified against current `HEAD` before dispatching round M;
> entries below are corrected in place, not rewritten, per the file's own ground rule 1.
> - **§12.1** — `uv.lock` now exists (`bcd08eb`, 83 packages, `uv lock --check` clean). `uv sync
>   --frozen` deliberately not run in that commit (avoids mutating the shared `.venv` mid-round).
>   Done bar unchanged; the precondition blocking it is gone.
> - **§12.3** — `pytest-cov` + `[tool.coverage.run]`/`[report]` now declared (`bcd08eb`),
>   *deliberately* left unarmed: no `fail_under`, `--cov` not in `addopts`, disclosed reason is a
>   real interaction risk with the suite's multiprocessing/forkserver use. Arming it is not a
>   drop-in `--cov` add — a worker on this criterion must account for that risk (e.g. a dedicated
>   coverage invocation, not blanket `addopts`), not silently override a disclosed decision.
> - **§12.18** — `EventEmitter`/`events_jsonl_path` now wired end-to-end (`01b64d3`); all six CLI
>   entry points pass `json_path=`. The commit explicitly scoped out `llm_call`/`latency_ms` and the
>   `errors-<run_id>.jsonl` split as "separate later tasks" — **now a WIRING task, not
>   NEW-MECHANISM**: the infrastructure exists, only the emission call and the error-sink split
>   remain. Reclassified below.
> - **§12.27** — COORDINATE detector now wired into real `fleet sequence` (`cli.py:2952`,
>   `3da6e79`) with 6 discriminating tests + 7 validated mutations. DEST_PATH and FILE_PATH remain
>   unwired **deliberately, not by oversight**, per that commit's own disclosure: FILE_PATH needs a
>   path/blob-SHA capture mechanism nothing upstream produces; DEST_PATH's `dest_rewrites` has no
>   consumer because Phase 3 doesn't perform the relocation it would assert. Both reclassified
>   NEW-MECHANISM (was WIRING) and deprioritized below; each needs its own D-number before work
>   starts, per that commit's own recommendation.
> - **§12.29** — the ownership-ladder rungs (ii)-(v) gap is substantially closed (`f3fb567`,
>   `73a3f8f`: `ownership_rank` now one shared function, proven rung-by-rung for CONTRACT too).
>   Re-verify the remaining sub-clauses (fixture scale, `content_sha256` reachability) before
>   counting this DONE.
>
> **Purpose.** The companion backlog to `CLAUDE.md` Rule 13 / ADR-0096. Before dispatching a round,
the orchestrator picks target criterion number(s) from this file, not from a general sense of
"what needs hardening." Built from `docs/superpowers/plans/spec12-success-criteria-audit.md` at
its final corrected state (round-K, `827fc6e`, 2026-08-28) — every gap below is cited to that
audit or re-derived from `docs/SPEC.md` directly, not invented here.

**Ground rules.**
1. `docs/SPEC.md` §12's literal wording is authoritative. This plan never redefines a criterion.
   Where the criterion's own wording is the defect ("Class 1" below), the done bar *is* the
   adjudication-and-edit, governed by `CLAUDE.md` Rule 14 — not a silent reword by whoever picks
   up the item.
2. Every "done bar" below is an **Agent Recommendation** (`CLAUDE.md` item 1, Directive Authority
   & Lineage) — a proposed bounded stopping point, not a new hard requirement laundered as one. If
   `docs/SPEC.md`'s literal text and a bound stated here ever conflict, SPEC.md wins until this
   file is revised to match.
3. "Out of scope" lines exist to stop gold-plating. Closing a criterion means making its own
   stated command/assertion pass — nothing added beyond that counts toward the count in Rule 13's
   checkpoint line, and nothing added beyond that is owed.
4. Status legend: **DONE** (no further work) · **OPEN — <class> — <effort tier>**.
   Effort tiers, cheapest first: `WIRING` (call an already-correct function from its real caller,
   no new logic) · `TEST-ONLY` (write the missing assertion, no production code changes) ·
   `SPEC-ADJUDICATION` (decide replacement wording, then usually TEST-ONLY) · `SCALE-FIXTURE`
   (existing mechanism, needs a bigger/real fixture) · `NEW-MECHANISM` (real new code).

---

## 1. `uv sync --frozen` offline on py3.12
**OPEN — unit-level standing in for the criterion — TEST-ONLY.** Every `uv` reference in `tests/`
drives the Python ecosystem *adapter* running `uv pip compile` against target repos, never `uv
sync` on this harness's own environment (spec12 audit row 1).
**Done bar:** one test that shells `uv sync --frozen` against the committed `uv.lock` with network
disabled and asserts exit 0, plus an assertion that `sys.version_info[:2] == (3, 12)`.
**Out of scope:** does not require touching the adapter's own `uv pip compile` tests.

## 2. `ruff check` / `ruff format --check` / `mypy --strict`
**OPEN — mixed — TEST-ONLY.** `ruff check` is covered. `ruff format --check` is *deliberately* not
gated (self-disclosed, 117/142 files dirty). `mypy --strict` has no test at all, though it is
independently checkpointed clean over 106 files in `INTEGRATION_HONESTY.md` (audit row 2).
**Done bar:** one test asserting `ruff format --check` exit code with the current dirty count
pinned as an `xfail`/known-baseline (do not silently require reformatting 142 files as a side
effect of closing this criterion), and one test that shells `mypy --strict src/fleet/` and asserts
exit 0.
**Out of scope:** reformatting the 117-142 dirty files is a separate, larger, and disruptive
change — track it as its own item if wanted, not folded into closing §12.2.

## 3. Coverage gate + `tests/unit` isolation
**OPEN — SPEC was aspirational — NEW-MECHANISM (small).** No `pytest-cov`/`coverage` dependency,
no `[tool.coverage]` config anywhere; `tests/unit/` and `tests/contract/` hold only `__init__.py`
so they pass vacuously (audit row 3, and the §12.3 gate-measurement section — 93% measured
out-of-band, never wired in-tree).
**Done bar:** declare `pytest-cov`/`coverage` in `pyproject.toml`, add `[tool.coverage] fail_under
= 85`, wire `--cov` into the real `pytest` invocation this criterion names, and either populate
`tests/unit/` with real fast/no-network/no-Docker/no-credential tests or retarget the criterion's
`<30s>` clause at whatever subset genuinely is that (adjudication, not silent narrowing — flag via
Rule 14 if the latter).
**Out of scope:** does not require reorganizing the rest of `tests/` into the `unit/` layout.

## 4. Model round-trip + per-backend golden response
**OPEN — mechanism partly exists — NEW-MECHANISM (small, per backend).** Round-trip is fully
covered. No golden-response artifact exists for any backend at any rung, including the required
`PROMPTED` rung (audit row 4); ADR-0013 declares the intent, nothing ships it.
**Done bar:** one recorded golden response per shipped backend (anthropic/bedrock/openai_compatible
/vertex) validating against its declared schema, plus one recorded at the `PROMPTED` rung, each
checked into a fixture and asserted by a real (non-network) test.
**Out of scope:** does not require a live-network golden-capture pipeline — a checked-in fixture
satisfies the criterion as written.

## 5. No `pickle` / `ThreadPoolExecutor` / `subprocess.run` in `src/fleet/`
**DONE (landed round N task 1, `51f7e31`, reviewed Approved).**
`test_no_forbidden_construct_appears_anywhere_in_src_fleet` (`tests/test_proc.py`) reproduces the
criterion's exact grep semantics as a tree-wide walk over all of `src/fleet/`, with substring
containment (stricter than the grep's BRE alternation). Mutation-tested individually against all
three forbidden constructs plus a cosmetic control. Sixth criterion to reach this file's strict
DONE bar.
**Out of scope:** none — this is a pure grep-widening, no production risk.

## 6. The ecosystem/contract-kind confinement invariant
**OPEN — TEST-ONLY.** All five named tests exist and pass, but only scan for `Ecosystem` patterns,
never `ContractKind`, and never scan for `match`/`case` statements (audit row 6) — both vacuous
gaps today, but a regression of either shape would pass every test.
**Done bar:** extend the existing pattern set to include `ContractKind` comparisons and `match`/
`case` forms, re-run against the same exemption list.
**Out of scope:** none.

## 7. Six `ManifestAdapter`s parse fixtures at `tests/fixtures/repos/`
**DONE (ADR-0099, 2026-08-30).** SPEC.md item 7 reworded to name the actual mechanism
(`tests/test_manifests.py`'s inline `write(tmp_path, ...)` helper) rather than a location that was
never populated. The property already held and needed no code/test change — this closure was the
SPEC sentence alone.

## 8. Graph correctness incl. `hypothesis` property tests
**OPEN — mixed — TEST-ONLY, mostly landed.** Confidence reconstruction is exact and covered.
Cross-repo `INTERNAL_IMPORT` is proven only on a hand-built input, never the fixture fleet (audit
row 8). The `hypothesis` property-test gap this row originally found is **stale** —
`tests/test_graph_properties.py` landed at `83e1493` (2026-08-28, per the audit's own late
correction) and now covers ADR-0013's wave-ordering claim.
**Done bar:** one test asserting `INTERNAL_IMPORT` edges appear from a real `test_scan_e2e.py`-style
fixture run, not only the hand-built `InferenceInput`.

## 9. Phase 1 exit condition is a runtime gate
**PARTLY ADDRESSED (landed round M, `42e760f`/`agent/roundm-task1`, reviewed Approved).**
`check_criteria()` is now wired into `_sequence_impl` (`fleet sequence`) via a new
`_phase1_exit_report()` helper, refusing with `SequenceCriterionError` (exit 6, naming the failed
sub-criterion) when the gate fails. One e2e fixture plants all six §3.1(c) exemption shapes
simultaneously (five SPEC bullets, six test shapes — see the file addendum) and asserts closure.
Criterion `(a)`'s scoping bug (unsatisfiable over an unscoped repo set for config-skipped/
baseline-red/quarantined/preflight-failed/empty-repo categories) was found and fixed at the call
site during this task — `docs/SPEC.md`'s criterion `(a)` text corrected in place (dated marker,
2026-08-30) to match, rather than left silently inconsistent.
**Still open — criterion `(d)`** ("no edge exists whose `evidence_path` does not resolve to a real
file at `head_sha`") is a **disclosed no-op**: nothing in `src/` persists the `ls-tree` listing
`evidence_path` resolution needs, so the sub-check defaults to vacuously `True`. This is the same
underlying gap §12.27's FILE_PATH leg is blocked on (a missing path/blob-SHA capture mechanism) —
do not build a second, divergent mechanism for this criterion; wire both from the same capture
point once it exists.
**Also open:** the `MANUAL`-SCC exemption shape is only exercised at the `check_criteria()` unit
level — an earlier guard in `_sequence_impl` refuses `MANUAL` SCCs before the new gate runs, so
live CLI traffic never reaches that one shape through the wired path (disclosed, not a regression).
**Done bar (remaining):** wire criterion `(d)` once a real path/blob-SHA capture mechanism exists
(shared with §12.27's FILE_PATH leg — track as one piece of work, not two). Until then this
criterion stays PARTLY ADDRESSED, not DONE — do not round up the `<n> of 48` count for it.
**Out of scope:** `check_criteria()`'s other sub-checks' internal logic was not and should not be
touched further — already correct.

## 10. Phase 2 exit condition — `_transform_criterion`
**OPEN — TEST-ONLY.** All three violation branches (probe-returns-False, empty-diff,
outside-`dest_path`) are unexercised; every fake `parse_probe` in tests returns True or raises
(audit row 10).
**Done bar:** three tests, one per branch, each driving `_transform_criterion` to the specific
violation and asserting the correct rejection.

## 11. Phase 3 exit condition — real bazel + sandboxed, together
**OPEN — SCALE-FIXTURE.** Real bazel and the networkless sandbox are each proven, but never in the
same run (audit row 11; already known, `INTEGRATION_HONESTY.md` addenda §29/§31-33, "the sandboxed
path is still red", unretracted). Also: no test compares baseline vs. migrated test *counts*
(`tests(//` has zero hits) — only the boolean went-to-zero case is caught, so 14→5 tests passes
undetected.
**Done bar:** one e2e run combining real bazel + the networkless sandbox, and one test asserting
`migrated_test_count >= baseline_test_count` (not just non-zero).
**Out of scope:** does not require re-litigating the existing "sandboxed path is still red" defect
— that's tracked separately; this criterion needs the *combination* proven, which is a fixture
composition, not a new bugfix.

## 12. Phase 4 exit condition
**DONE.** The only criterion the audit found fully covered — rdeps closure with disclosed
sampling, resolvable PR URLs, and the cross-repo unmerged-dependency gate proven non-trivially.
No further work; do not touch.

## 13. Retry semantics — ladder, ceiling, atomicity, transient budget
**OPEN — mixed — TEST-ONLY + known D50.** Ladder-length validation, transient-vs-attempt counting,
and 3-rung tier escalation to RHI are all covered. The 5-rung ladder variant is untestable today
because `transform.ladder.*` is `KNOWN_INERT` (D50) — `LadderState` takes no `ladder=` kwarg.
Atomicity is asserted by end-state only; nothing is ever interrupted mid-write, so it can't
distinguish one atomic UPDATE from two sequential ones (audit row 13).
**Done bar:** the 5-rung sub-clause is blocked on D50 and should not be worked independently —
closing D50 closes it. The atomicity sub-clause needs a real interruption test (kill/inject
between increment and terminal write, assert no torn state) — that part is independently
actionable now.
**Out of scope:** do not fake the 5-rung test against inert config; that reproduces the Rule 12
"assertion weaker than its name" failure mode this file exists to prevent.

## 14. Blast containment + escape hatch
**OPEN — known D50, partial.** (a) and (b) — containment and `fleet resume` unblocking — are
fully covered through real e2e paths. (c)/(d) are actively refused: `--stub-blocked` exits USAGE
by design, tracked as D50 (audit row 14).
**Done bar:** identical to D50's own closure — do not open a separate effort here.

## 15. Crash safety, Git is the arbiter
**OPEN — TEST-ONLY.** Step-4 git arbitration (discard vs. adopt) is strongly covered with a
genuinely discriminating fixture. Missing: the fabricated reverse-disagreement case (hand-edited
`attempts.commit_sha` pointing off-branch) is never constructed (audit row 15).
**Done bar:** one fixture constructing that specific divergence and asserting the arbitration
resolves it correctly.

## 16. Checkpoint integrity on corruption
**DONE (SPEC corrected, `12be741`).** The SPEC said corruption "raises `ValidationError`"; the
code deliberately never raises (`test_a_truncated_blob_invalidates_instead_of_raising`) and that
behavior is itself the intended contract. The SPEC text was corrected to match, in the same commit
as the audit. No further code or test work — verify the corrected SPEC.md wording is what's cited
in Rule 13's checkpoint count, and mark done.

## 17. Projection fidelity — "byte-identical"
**OPEN — false as written — SPEC-ADJUDICATION (real decision, not trivial).** `MigrationState`'s
`updated_at` uses `Field(default_factory=utcnow)`, so an untouched rebuild always differs. This is
the same class of bug CLAUDE.md's own Guardrail 6 "Round F" entry independently documents for a
different detector — never previously connected to this criterion (audit row 17). Round-K added a
marker but explicitly left the replacement wording unchosen.
**Two real options, cost stated for both (this is the adjudication, not a decision made here):**
(a) drop `updated_at` from the value compared for equality (code change to `projection.py`,
restores literal byte-identity, cheap); (b) weaken the SPEC clause to "structurally identical
modulo `updated_at`" and add a field-scoped equality test (SPEC edit only, no code change).
**Done bar:** whichever option is chosen, via Rule 14 (dated marker + ADR), then one test proving
the chosen invariant across two regenerations of an untouched database.

## 18. Observability — `llm_call` event, `latency_ms`, `logs/errors-<run_id>.jsonl`
**DONE (landed round M task 3, `5f31fd3`/`agent/roundm-task3`, reviewed Approved, fix round 1
addressed both findings).** Verified against SPEC.md item 18's exact text (all three clauses):
(1) every `llm_call` event carries `role`, `tier`, `backend`, `model_id`, `structured_output_mode`,
token counts, `cost_usd`, `latency_ms` — field set confirmed to match verbatim, emitted once per
raw `backend.invoke()` call (`llm/client.py`'s `_call_target`, the corrected call site — the
brief's original `.invoke`/`complete()` citation was wrong and was corrected mid-task). (2)
`backend_failover` events name both targets and trigger — already covered pre-existing, unaffected
by this task. (3) `logs/errors-<run_id>.jsonl` exists iff a recoverable error occurred — implemented
as a filter (not route-away) so the main stream's "every line" property (`obs/events.py`'s own
docstring) stays true, written lazily so the file's mere existence is the signal. Redaction
verified: both sinks share the same already-redacted line, no second unredacted egress path. A
regression-guard sweep test (AST-based, not a hand-maintained list) now asserts all 5 production
`RunContext(` call sites in `cli.py` pass `root=`, closing the one gap the task review found.
**This is the third criterion (after §12.12 and §12.16) to reach the file's genuinely strict DONE
bar — full literal text, not a partial reading.**

## 19. Cycles broken at the stated scale
**OPEN — SCALE-FIXTURE.** Existing tests use 2-3 node cycles; the criterion states 3/12/41-node
cases specifically, plus a "no hang" property with zero timeout assertions anywhere (audit row 19).
**Done bar:** a 12-node fixture asserting shared `scc_id` via a built `PullRequestDraft`, and one
timeout-bounded test asserting the 41-repo case completes and doesn't hang (does not need to
literally plant 41 repos if the algorithm's complexity class makes a smaller adversarial case
equally discriminating — state which was used).

## 20. Secrets never leak
**OPEN — TEST-ONLY.** Redaction primitive and two of the named leak sites (`events.payload`,
`attempts.stderr_tail`) are covered. `phases.last_error` and `llm_cache.response_json` have no
redaction test; the PR-body `«redacted:…»` placeholder is unverified (audit row 20).
**Done bar:** one fixture run planting all three named secret shapes, sweeping the two uncovered
columns plus the PR-body placeholder.

## 21. Determinism — clean re-run, byte-identical digest
**OPEN — 2 of 3 clauses closed (round O, `daf2a24`), one remains.** SPEC.md item 21 has three
clauses: (1) clean re-run under `--llm-cache read-only` produces a byte-identical digest — **now
covered** (`tests/test_cli.py`, reviewed Approved through one fix round; two real from-scratch
runs, cache warmed then seeded cross-run per `schema.sql`'s own documented "NOT scoped to
run_id" design, second run forced through an exploding backend so success requires a genuine
cache hit; the digest's `waves`/`edges` sections are non-trivially exercised, the other 5 are
honestly disclosed as structurally empty-but-equal in this fixture, not silently overclaimed).
(2) `--llm-cache read-only` with a cleared cache fails loudly — **already covered pre-existing**
(`test_llm_cache.py:454`, `test_run_context_llm_cache.py:279`). (3) **mutating one fixture source
file changes the digest — still untested**, confirmed by grep, not attempted this round. Do not
count §12.21 toward the `<n> of 48` tally until clause 3 lands.
**Done bar (remaining):** one test that mutates a single fixture source file (any real content
change) between two otherwise-identical runs and asserts the digest *differs* — the inverse of
this round's test, proving the digest is actually sensitive to source content, not just stable
when nothing changes.

## 22. Memory + disk ceilings
**OPEN — mostly missing — NEW-MECHANISM.** `resource_guard` defaults to a no-op
(`lambda: None`) — RSS/cgroup sampling is not implemented at all, not just untested (audit row
22). `budgets.max_host_rss_mb` is `KNOWN_INERT` (D50). Exit-9-on-disk-ceiling is covered; post-exit
`migration_state.json` validity is not.
**Done bar:** the RSS-sampling sub-clause is blocked on implementing `resource_guard` for real —
this is the one item in this file that's genuinely new infrastructure, not a wiring/test gap.
Track it as its own round; do not fold it into a general "criterion 22" sweep that also claims the
disk-ceiling sub-clause, which is smaller and already mostly done (needs only the post-exit state
validity assertion).

## 23. Idempotency — re-scan, re-transform
**OPEN — SCALE-FIXTURE.** Re-transform is fully covered. Re-scan covers 6 of 8 named tables. No
9-repo vendored-contract fixture exists (audit row 23).
**Done bar:** extend the re-scan test to the remaining 2 tables, and add the 9-repo fixture the
criterion names for the vendored-contract case.

## 24. Fail-closed budgets, ledger moves correctly
**OPEN — mixed, 7 sub-clauses — TEST-ONLY + known D62.** Wave-breach, over-reserve-refused, and
in-flight-wait are covered. `spent_usd == sum(cost_usd)` is entirely untested (no e2e queries
`budget_ledger`). Local-profile row completeness is D62 (`llm_backend` etc. are NULL — do not
re-open, it's tracked). Run-ceiling ledger mechanism is proven but never through an organic CLI
breach (`RunBudgetExhausted` has zero refs in `cli.py`/`test_cli.py`) (audit row 24).
**Done bar:** one e2e test asserting the ledger-sum invariant, and one CLI-level test that
organically breaches the run ceiling (not a direct call into the budgets module) and asserts exit
3.

## 25. The unknown repo survives the pipeline
**OPEN — SCALE-FIXTURE.** The `no-manifest` finding and `misc/<repo_id>` destination are covered.
`repos.kind == 'unknown'` conflates two different fields (`Ecosystem.UNKNOWN` vs. the classify
worker's advisory `kind`) and no test exercises the real one. The unknown filegroup is never built
under real bazel (audit row 25).
**Done bar:** add an unknown-ecosystem repo to the real-bazel e2e fixture (currently 2 TS + 2
Python only) and assert it builds; fix or split the `kind` conflation into two separately-named
assertions.

## 26. Preflight gates rather than crashes
**OPEN — submodule/LFS closed (round O, `1c8e0ef`), one sub-case this file's original done-bar
missed remains.** SPEC.md item 26's full text (`:7441`) names **five** fixture categories: empty,
shallow, submodule-bearing, LFS-bearing, and **default-branched to `trunk`**. This file's earlier
done-bar named only submodule/LFS — an incomplete reading, caught only now. Empty-repo and shallow
were already covered pre-round; submodule/LFS are now covered with real fixtures
(`tests/test_scan_e2e.py`, reviewed Approved — genuine `git submodule add`, genuine LFS pointer
format, driven through real preflight, `SUCCEEDED` + documented columns + `SubmodulePresent`
finding, matching the criterion's own "either proceed or produce a `PreflightFailed` finding"
wording verbatim). **`trunk`-default-branch has zero fixture coverage anywhere** — confirmed by
grep, not yet attempted. D41 covers a different defect (never-ran vs. settled-negative), not this
gap. Do not count §12.26 toward the `<n> of 48` tally until the fifth case lands.
**Done bar (remaining):** one fixture repo whose default branch is named `trunk` (not `main`),
driven through preflight, asserted to proceed or gate per the same criterion text — read
`workers/clone.py`'s actual handling of a non-`main` default branch before writing the assertion,
don't assume.
**Also flagged, separate future item (not this criterion's scope):** `require_lfs_binary: true` +
no `git-lfs` on `PATH` → `PREFLIGHT` failure has zero test coverage anywhere (found by round O
task 3, correctly left out of scope).

## 27. Collisions caught before transformation
**PARTIALLY CLOSED (2026-08-30, see file addendum) — COORDINATE done, DEST_PATH/FILE_PATH
reclassified NEW-MECHANISM.** `3da6e79` wired COORDINATE through real `fleet sequence`
(`cli.py:2952`), persisted to the `collisions` table, gated by a real post-write exit-6 refusal —
6 discriminating tests + 7 validated mutations, `tests/test_collisions_wiring.py`. DEST_PATH and
FILE_PATH remain unwired **by disclosed decision, not oversight**: FILE_PATH needs a path/blob-SHA
listing nothing upstream captures; DEST_PATH's `dest_rewrites` has no consumer because Phase 3
doesn't perform the relocation it would assert. Neither is a caller-wiring task anymore — each
needs new upstream data capture (FILE_PATH) or a new Phase-3 consumer (DEST_PATH) first.
**Done bar (COORDINATE leg):** none — already closed, do not re-touch.
**Done bar (DEST_PATH/FILE_PATH legs):** file a D-number for each (per `3da6e79`'s own
recommendation) before starting; FILE_PATH's done bar is "capture path+blob-SHA per claim
somewhere in the scan pipeline, then wire the detector"; DEST_PATH's is "give Phase 3 a real
relocation-tracking consumer, then wire the detector" — both are multi-step, do not attempt as a
single one-shot task.
**Out of scope:** do not re-implement or duplicate a second COORDINATE call site.

## 28. Single writer, pool children have no DB handle
**OPEN — TEST-ONLY, narrower than the original wording (round-K correction).** The single-writer
half is covered. The pool-children-no-handle half is **true in the tree today** by inspection
(`orchestrator/budgets.py:947-949` passes only `max_workers=`/`mp_context=`) but not asserted by
any test that inspects the initializer arguments, as the criterion's own evidence clause requires.
200-repo zero-`SQLITE_BUSY` is untested at scale (largest today is 50 coroutines) (audit row 28,
corrected `docs/SPEC.md:34` per round-K — this is NOT the retired `D86` clause).
**Done bar:** one test inspecting `budgets.py:947-949`'s actual `ProcessPoolExecutor(...)` call
arguments and asserting no DB-handle-bearing kwarg is present, plus a coroutine-count test at closer
to the stated 200-repo scale (or a documented, adjudicated smaller number if 200 real coroutines is
impractical — state the number chosen and why).
**Out of scope:** do **not** add an `initializer=` — that would satisfy the retired §12.47
sentence, not this one, and SPEC.md's own round-K correction explicitly forbids it as "inverting
Rule 11 to buy a property nothing needs."

## 29. Contracts extracted once, deterministically
**OPEN — mixed, 9 sub-clauses — SCALE-FIXTURE + one structurally-unreachable clause.** Most
numeric/severity sub-clauses are covered exactly. The fixture is 3 repos, not the literal "3
vendoring + 2 generated." Sub-clause (h), the `divergent` ×0.5 modifier, is **structurally
unreachable from real scan data** by the module's own docstring (no blob SHA available from real
scans) — this is a SPEC-vs-implementation gap, not a missing test. Sub-clause (i), the
node-integrity/orphan-edge query, doesn't exist (audit row 29).
**Done bar:** build the "3 vendoring + 2 generated" fixture for the scale-testable sub-clauses;
flag sub-clause (h) via Rule 14 as needing adjudication (either make `content_sha256` reachable
from real scans, or retire the ×0.5 modifier clause); write the missing orphan-edge query for (i).

## 30. Contract cycle broken by hoisting, not bundling
**OPEN — SCALE-FIXTURE.** Real scan→sequence e2e exists but only at 2-3 repo scale, never the
literal 6-repo cycle. Acyclicity of a `CONTRACT_HOIST`-resolved graph is never asserted. No fixture
shows the same repo set flipping between `CONTRACT_HOIST` and pre-ADR-0019 `ATOMIC_WAVE` under
`--no-hoist-contracts` (audit row 30).
**Done bar:** the 6-repo cycle fixture, an acyclicity assertion on the resolved graph, and the
flip-under-flag comparison test.

## 31. Wrong contract hoist detected and rolled back
**OPEN — mechanism doesn't exist — NEW-MECHANISM.** `ContractStatus.FAILED` is declared but never
assigned anywhere in `src/fleet/`. No `git revert` call exists in the hoist path.
`_hoist_contracts` is a pure in-memory trial simulation with no rollback branch (audit row 31). Not
in any ledger.
**Done bar:** implement the rollback path that assigns `ContractStatus.FAILED` and issues the
`git revert`, then a fixture that plants a hoist failure and asserts recovery. This is real new
code — do not attempt to close it via test-only scaffolding.
**Recommend filing a new `D`-number for this before starting**, since it isn't in either ledger yet.

## 32. Adapter registries total, delegation honest
**DONE (landed round N task 3, `7cf3147`, reviewed Approved).** SPEC.md item 32's text has three
checkable parts plus one explicit carve-out: (1) `discover()` key set equals `set(Ecosystem)`
exactly, decoy-member-missing raises at import time — closed this round
(`test_discover_raises_naming_a_decoy_ecosystem_member_with_no_adapter`). (2) decoy double-claim
raises `RuntimeError` from `@register` — already covered pre-existing
(`test_ecosystems.py:71-103`). (3) `generate_targets`/`gazelle_config` biconditional — already
covered pre-existing, parametrized over every `Ecosystem`. The fourth part, `contracts.discover()`
equality against `set(ContractKind)`, is **self-declared "UNSATISFIABLE AS WRITTEN and NOT a
passing gate" in the SPEC's own text**, pre-adjudicated by ADR-0065 — the criterion does not
require it, so its absence doesn't block DONE. Fifth criterion (after §12.7, §12.12, §12.16,
§12.18) to reach this file's strict DONE bar.

## 33. Layout is adapter-derived, not hardcoded
**OPEN — SCALE-FIXTURE.** The hardcoded-dir grep is covered. The ts→js monkeypatch test checks one
synthetic node, not a full fixture run or `BuildTarget.package`. The config-override e2e exists but
nothing compares the two resulting trees for the "identical tree" claim (audit row 33).
**Done bar:** widen the monkeypatch test to a full fixture run with a `BuildTarget.package`
assertion, and add the tree-comparison assertion to the config-override e2e.

## 34. New-language cost is exactly the documented touchpoints
**OPEN — mechanism doesn't exist — NEW-MECHANISM.** `tests/fixtures/adapters/` doesn't exist; no
fixture ecosystem has ever been added end-to-end. `ContractBindingUnavailable` and
`unbound_contract_kinds` are declared and never exercised (audit row 34; consistent with ADR-0065).
**Done bar:** add one genuinely new (fixture-only, not a real language) `EcosystemAdapter` +
`ManifestAdapter` pair end to end, and assert the touchpoint count matches SPEC §1's documented
four.

## 35. No raw prior diff reaches a prompt
**OPEN — mostly covered, one structurally-blocked sub-clause.** Worker-level coverage is strong.
Missing: the prior `FailureClass` token is never asserted; diff-absence is checked via a
marker/header-string proxy, not the full per-line sweep the criterion specifies. CLI-level proof is
currently impossible — `cli.py` refuses `--context-policy` for any value at all (audit row 35).
Downstream of D50.
**Done bar:** add the `FailureClass` assertion and the real per-line sweep at the worker level now
(both are TEST-ONLY, independent of D50). The CLI-level proof is blocked on D50's `--context-policy`
work — don't force it before that lands.

## 36. Anchoring detected mechanically
**OPEN — already tracked, D50.** `rewrite/approach.py` doesn't exist; `--no-anchoring-guard`
self-declares the gap in its own refusal message (audit row 36).
**Done bar:** identical to D50's closure. Do not open a separate effort here.

## 37. Stub lifecycle — only way out of DEGRADED
**OPEN — PARTLY ADDRESSED (2026-08-30). D80 landed (resume-time reconciliation), but the
criterion's own literal text requires more than D80 covers.** SPEC.md item 37's full scenario
starts with `--stub-blocked` actually creating a `stubs` row (`state='ACTIVE'`,
`stub_fidelity='PUBLISHED_ARTIFACT'`, a `VerificationReport` with `equivalence='STUB_LIMITED'`) —
that half still doesn't exist: `--stub-blocked` needs a stub-*creation* worker in
`workers/buildgen.py` that was never built (confirmed 2026-08-30 by a research agent investigating
round M task 2 — see `docs/INTEGRATION_HONESTY.md` D80's entry and ADR-0098). What round M's task
2 landed is the *other* half the criterion also requires: once a stub row exists, re-running its
blocker to `SUCCEEDED` now correctly reconciles it via `fleet resume` (`orchestrator/stubs.py`'s
`reconcile()`, wired at last), moving `ACTIVE`→`SUPERSEDED`→`RESOLVED`, and the idempotency
half (`revalidation_key`, no duplicate rows across repeat triggers) is real per D80's landed
tests. This closes real ground but not the whole criterion — do not mark §12.37 DONE.
**Done bar (remaining):** build the `--stub-blocked` stub-creation worker (NEW-MECHANISM, not
wiring — this is genuinely new logic, not a caller-wiring task; do not attempt as a one-shot). Once
it exists, wire it to actually create the row instead of refusing exit 2, then the full end-to-end
scenario in SPEC.md item 37 becomes testable for the first time.
**Out of scope for the remaining work:** D80's landed reconciliation logic does not need to
change — it's correct and tested; the remaining gap is purely on the creation side.

## 38. No ready-for-review while a stub is unresolved
**OPEN — mixed, 20 sub-clauses — mostly D80 + TEST-ONLY.** The headline refusal (exit 2 + PrState
unchanged) is well covered by two independent tests. `stub_reconcile` has no implementation
(`cli.py:10771` is a comment) — that's D80, which already names this gap nearly verbatim. Sharpest
independent finding: deleting `'SUPERSEDED'` from `cli.py:9902` passes the whole suite, and the
guard's positive half (readiness correctly granted) is asserted nowhere (audit row 38).
**Done bar:** the resolution/sweep/exit-7 sub-clauses are D80's scope — closing D80 closes them.
Independently and immediately actionable: one test exercising the `SUPERSEDED` arm specifically
(so deleting it reddens), and one test asserting the positive case (stub genuinely resolved →
ready-for-review succeeds).

## 39. Bounded, priced rework; stub rot reaches a human
**OPEN — mixed, 18 sub-clauses — TEST-ONLY, mostly blocked on §12.37's wiring.** Case (iii)
(batched=1/eager=3 cost comparison) is well covered. Cases (i)/(ii) only test what's handed in as
a parameter rather than driving it from a real stub-rot scenario (audit row 39).
**Done bar:** once §12.37 is wired, drive cases (i)/(ii) through the real stub lifecycle rather
than direct parameter injection. Sequence after §12.37, not before — closing this first would just
re-test the same disconnected parameters.

## 40. No model string outside `config/`
**DONE (SPEC + code corrected, round-K).** The criterion's own greps were unsatisfiable as written
(named a nonexistent `llm/routing.py`, and returned non-zero hits without the backend-file
exclusion). Round-K applied the exclusion for real and validated it three ways (clean tree /
exclusion removed / synthetic fault injected). **Remaining, independently small (audit row
40, M1):** the AST clause — "an AST test asserts only `settings.py` reads `config/models.yaml`" —
is asserted by no test at all; only the model-id/endpoint greps are covered.
**Done bar:** write the one AST test the M1 note names. Everything else in this criterion is
already closed.

## 41. Local-only profile runs the whole pipeline
**OPEN — structural gap, no test at all — NEW-MECHANISM (test infra).** No test anywhere runs any
pipeline phase under a non-`default` profile (9 of 11 sub-clauses unasserted). Separately, a
genuine SPEC-vs-config drift: §12.41 says local CHEAP targets declare `supports_json_schema: false`
and `supports_tools: false`; `config/models.yaml` declares neither, deliberately, per its own
comment (audit row 41).
**Done bar:** the drift is Rule-14 territory first — adjudicate whether `config/models.yaml` should
gain those declarations or the SPEC sentence should drop them, with a dated marker either way.
Then: run at least a Phase-1-through-Phase-3 slice of the fixture fleet under `--profile local`
(or whatever the local-only profile is named) and assert every named sub-clause.

## 42. New backend costs one file + one registry line
**OPEN — this done bar's own scope closed (round N, `cc12a2a`), criterion overall still 5 of 9.**
`register_backend`'s duplicate-name `RuntimeError` now has a real, reviewed-Approved test
(`tests/test_llm_client.py::test_register_backend_refuses_a_duplicate_name`) — the specific gap
this file's done bar named. Four of the other eight startup refusals were already covered before
this round; four remain open (not this round's scope). Do not count §12.42 toward the `<n> of 48`
tally — the criterion as a whole is still open.

## 43. Failover layered, bounded, fail-closed
**OPEN — mixed, 8 of 16 sub-clauses — TEST-ONLY + known D55/D58/D62/D78.** Case (ii) is entirely
absent (D55/D58). `llm_failovers` recording is D62. Independently and newly found: an existing test
(`test_runner.py:1613`) discards the status column entirely, so "zero repos land in RHI" is never
actually checked — it would pass with the repo marked RHI. `TierUnavailable.__init__`, the sole
producer of the "names the tier and every target tried" message, is asserted by nothing; every test
checking that message *writes the string itself* rather than reading it from the producer (audit
row 43, D78).
**Done bar (independently actionable now, not blocked on D55/D58):** fix `test_runner.py:1613` to
destructure and assert the status column instead of discarding it, and add one test that
constructs a real `TierUnavailable` and asserts the message it actually produces rather than a
hand-written string. Case (ii) itself stays blocked on D55/D58.

## 44. Cache not poisoned across backends
**OPEN — this done bar's own scope closed (round O, `70a4398`), criterion overall still mixed.**
Cache-key tamper detection is now real: `tests/test_llm_cache.py` recomputes `cache_key` from a
persisted row's own SQL-read-back columns via the real `hashing.cache_key()` primitive and
proves a direct-tamper mismatch — reviewed Approved, verified non-tautological. The rest of the
original audit's "1 of 6 full, 4 partial, 1 absent" breakdown is untouched by this round (no stub
OpenAI-compatible server, per this criterion's remaining sub-clauses). Do not count §12.44 toward
the `<n> of 48` tally — the criterion as a whole is still open.

## 45. No code state persisted outside Git
**OPEN — mostly missing — mixed, needs adjudication first.** SPEC-ADJUDICATION sub-part: the
forbidden-column regex was corrected (`collisions.blob_shas` no longer false-matches); M2 records
the "five vs. one" count is predicate-dependent and the SPEC marker already carries that
distinction, so no further SPEC edit is owed there. Substantively, 5 of 7 sub-clauses are
uncovered: no `PRAGMA table_list`/`table_info` sweep exists; the 40-hex enumeration omits
`repos.head_sha` and `tasks.pre_commit_sha`; no hunk-header (`@@`) scan of the DB exists; no
delete-artifacts-then-resume test exists; only 3 of 6 git trailers are read by any test (audit row
45).
**Done bar:** build the `PRAGMA table_list`/`table_info` sweep as a real mechanical check (this is
what the criterion calls "all mechanical" and today is prose-only), fix the 40-hex enumeration to
include the two omitted columns, add the hunk-header scan, the delete-artifacts-then-resume test,
and coverage for the remaining 3 trailers.
**Out of scope:** the `collisions.blob_shas` regex fix is already done — do not re-touch it.

## 46. Model-layer invariants
**OPEN — mixed, 13 sub-clauses, mostly strong — TEST-ONLY + known D77/D80.** 8 of 13 are solidly
covered (illegal transitions raise, abandoned-reopen gating, stale-lease rejection, reap-in-flight
discard, 32 KiB truncation, zero-duplicate-commit re-entry). Gaps: round-trip is asserted by object
equality, which is the exact form the criterion says *not* to use ("via `model_fields`, not object
comparison"); `Resolution` (in `models/build.py`) is covered by nothing though built by all three
ecosystem adapters; the reaper's RHI leg writes raw SQL bypassing `transition()` entirely so
`ALLOWED_TRANSITIONS` tests never bind it (the D77 bypass shape, recurring); `stub_reconcile` is
D80; `edges.edge_key` stability is tested only at the inference layer, never on persisted values;
`acquire_phase_lease` is never driven concurrently (audit row 46).
**Done bar:** switch the round-trip test to compare via `model_fields` as the criterion literally
requires; add a test for `Resolution`; drive the reaper against a real RHI row and assert it goes
through `transition()` (this is the same shape as D77 — check whether closing this closes D77 or
vice versa before doing both); add a persisted-value `edge_key` stability test; add a concurrent
`acquire_phase_lease` test. `stub_reconcile` stays D80's scope.

## 47. Registries stateless, total, order-independent
**OPEN — TEST-ONLY, narrower than original wording (round-K correction retired the `discover()`
initializer clause entirely — see §12.28's entry above for why it stays retired).** Manifests/
ecosystems statelessness and the rule-engine/backend-name startup refusals are covered. Missing:
`workers` registry has zero `assert_stateless` call sites; `backends` registry registers
*instances*, not classes, with no statelessness check at all; "abstract on `BaseWorker`" is
unasserted — deleting `@abstractmethod` and defaulting to `return True` passes the whole suite; the
"20 shuffled import orders" claim is really 3 hand-written orders comparing an adapter list, not a
genuine shuffle (audit row 47).
**Done bar:** add `assert_stateless` calls (or an equivalent check) for the `workers` and `backends`
registries; add a test asserting `preconditions_hold` is abstract on `BaseWorker` by walking
`workers.discover()` and failing on any class inheriting the base implementation (SPEC.md's own
`:53` text names this exact mechanism — build it); replace the 3-hand-written-orders test with a
genuine randomized-order test across ~20 real shuffles.
**Out of scope:** do not add a `ProcessPoolExecutor` initializer — that clause is retired, see
§12.28's entry.

## 48. Startup + version refusals before any cost
**OPEN — 5 independent claims, mixed — TEST-ONLY.** `migrate-db` exception, the `BEGIN EXCLUSIVE`
ladder, stale-checkpoint-invalidated, and truncation-retry-exactly-once are all covered. Missing:
"every command" is asserted for exactly one verb (`status`) against 22 `@app.command`-decorated
verbs and 12 `_check_schema_version` call sites — nothing enumerates the full set; the
message-names-both-versions clause is unasserted; "before a clone or an LLM call" is unasserted and
inexpressible in the current `status`-only fixture; the DDL AST test named by the criterion doesn't
exist; the mirror-mutex refusal never asserts exit 2 (audit row 48).
**Done bar:** parametrize the schema-version-refusal test over all 22 commands (or the subset that
actually touches the DB, if some genuinely can't — state which and why); add the
both-versions-in-message assertion; build the DDL AST test; add the exit-2 assertion to the
mirror-mutex refusal test. "Before a clone or an LLM call" may need a different, non-`status`
fixture — flag via Rule 14 if it turns out inexpressible as worded, rather than silently dropping
it.

---

## Rollup

| status | count | criteria |
|---|---|---|
| DONE | 3 | 12, 16, 40 (40's AST sub-clause still open — see its entry; counted DONE for its main clause per round-K) |
| OPEN — WIRING (cheapest, do first) | 2 | 27, 37 |
| OPEN — SPEC-ADJUDICATION needed before work starts | 3 | 17, 41 (partial), 45 (partial) |
| OPEN — blocked on an existing D-number, don't duplicate | 7 | 13 (partial), 14, 22 (partial, D50 for one sub-clause only), 35 (partial), 36, 38 (partial), 39, 43 (partial), 46 (partial, D77/D80) |
| OPEN — everything else (TEST-ONLY / SCALE-FIXTURE / NEW-MECHANISM) | remainder | see individual entries |

Note on §12.40's DONE marking: its dominant clause (no model string outside `config/`, structurally)
is closed; the AST sub-clause (M1) is still open and small. This file counts criteria as DONE only
when their full stated text passes — §12.40 is the one deliberate exception, flagged here rather
than silently overstating the Rule 13 checkpoint count. If in doubt when reporting the `<n> of 48`
figure, count §12.40 as OPEN until M1 lands, and prefer under-counting to over-counting.

**Recommended dispatch order, cheapest-and-highest-leverage first (updated 2026-08-30 — §12.27's
COORDINATE leg closed by round L, superseding the original §12.27+§12.37 pairing below):**
§12.9, §12.37, §12.18 (all now confirmed-open pure WIRING, zero new logic — round M's picks) →
§12.7 (one-line SPEC correction, substance already passes) → the TEST-ONLY items (5, 6, 10, 15, 20,
21, 24, 26, 32, 33, 40's AST clause, 42, 44, 47's sub-clauses, 48) → SCALE-FIXTURE items →
SPEC-ADJUDICATION items (17, 41, 45's regex half already done) → NEW-MECHANISM items (22's RSS
half, 27's DEST_PATH/FILE_PATH legs — each needs its own D-number first, 31, 34) last, since
they're the most expensive and least likely to be quick wins.
