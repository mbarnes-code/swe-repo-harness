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
**DONE (round S, `3ef2b9a`, reviewed Approved).** `tests/test_lint_gate.py`'s
`test_uv_sync_frozen_is_exit_0_offline_on_py312` shells `uv sync --frozen --offline` against the
committed `uv.lock`, redirected to a throwaway `tempfile.mkdtemp()` target via `UV_PROJECT_ENVIRONMENT`
(shared `.venv` empirically confirmed untouched by the reviewer — `git status`/mtime identical
before and after), and asserts exit 0 plus `sys.version_info[:2] == (3, 12)`. Mutation-verified
(a corrupted `uv.lock` flips exit 0 → 2). Found, not fixed (correctly out of scope): the shared
`.venv` has `mypy 2.3.0` installed against `uv.lock`'s pinned `2.3.1` — pre-existing drift,
orthogonal to this test by construction (throwaway sync target).

## 2. `ruff check` / `ruff format --check` / `mypy --strict`
**OPEN — PARTLY (round R, `5c4204c`) — SPEC-ADJUDICATION needed for the remaining leg.** `ruff
check` is covered. `mypy --strict` is now gated: `tests/test_lint_gate.py:384` shells `mypy
--strict src/fleet/` and asserts exit 0 (`tests/test_lint_gate.py:297` is the `ruff format
--check` test discussed below). Two of the three legs SPEC §12 item 2 names are closed.

The third leg — `ruff format --check src/ tests/` exits 0 — is **not met**. The landed test
(`test_ruff_format_check_dirty_count_matches_the_pinned_baseline`,
`tests/test_lint_gate.py:297`) deliberately pins a baseline dirty count instead of requiring exit
0 (its own docstring says so); `docs/SPEC.md:7417` item 2 requires exit 0 with no baseline
carve-out. Measured in the criterion's own scope, not the whole-repo scope the pinned test
actually runs: `ruff format --check --no-cache src/ tests/` → **116 dirty / 72 clean = 188
scanned** (re-measured 2026-08-31, round R final-fix, in an isolated worktree at `54b2c80`) —
materially different from the whole-repo pin's 123/283.

This baseline-instead-of-clean relaxation predates round R (it is in this file's wording at
`b9524af` already) and has **no ADR and no "adjudication pending" flag** anywhere in this
section (`grep 'adjudication pending\|ADR-'` over this entry returns no hit) — a Rule 14 gap.
Per Rule 14, a criterion's done bar cannot relax "exit 0" to "pinned baseline" without one of
those two markers, so §2 cannot count DONE under its *current* wording until either an ADR
adjudicates the relaxation, or the `ruff format` leg is actually driven to clean. This entry now
carries the flag Rule 14 requires: **adjudication pending** for the baseline-vs-clean relaxation
on the `ruff format --check` leg.
**Done bar:** one test asserting `ruff format --check` exit code with the current dirty count
pinned as an `xfail`/known-baseline (do not silently require reformatting 142 files as a side
effect of closing this criterion), and one test that shells `mypy --strict src/fleet/` and asserts
exit 0. **Not yet sufficient to count §12.2 DONE** — see the adjudication-pending flag above;
closing §2 for real needs either the ADR or driving the `ruff format` leg to actual exit 0.
**Out of scope:** reformatting the 116-188 (criterion-scoped) dirty files is a separate, larger,
and disruptive change — track it as its own item if wanted, not folded into closing §12.2.

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
**DONE (fix landed round Q task 1, `78e5667`; AST gates landed `dbd91d6`, merged `a1f20f5`;
round-Q-final-review correction `I4` closes the one gap found in the landed gate).** *(Edited
post-round-Q-final-review: this entry previously read "OPEN — real defect found, fix ruled —
TEST-ONLY once the fix lands" and its done bar said to "land the `contracts.py` fix … then
extend" — both halves had already landed and this entry was never refreshed after the merge; see
round-Q final review finding I1.)* All five original `Ecosystem`-only line-scan tests still exist
and pass. Round Q task 1 landed both halves this entry's old done bar described as future work:
`src/fleet/workers/contracts.py:758`'s `if kind is ContractKind.OPENAPI:` was converted to the
table-derived `KIND_MODIFIERS` form (`78e5667`, `docs/DECISIONS.md` ADR-0100, `docs/SPEC.md:7421`
dated marker), and a new AST walk
(`tests/test_ecosystems.py::test_no_bare_compare_or_subscript_names_a_kind_member_outside_the_adapter_packages`
+ `::test_no_match_case_names_a_kind_member_outside_the_adapter_packages`, `dbd91d6`) now covers
both the `ContractKind`-`Compare`/`Subscript` half the five line-scans never checked and the
`match`/`case` half (confirmed vacuous in production code today, by sweep).
**One substantive gap found and closed in the same follow-up as this correction:** the landed AST
walk's `_kind_member_name` helper matched a bare `ContractKind.X`/`Ecosystem.X` operand but missed
one nested inside a `Tuple`/`List`/`Set` comparator — the natural `if kind in (ContractKind.X,
ContractKind.Y):` form the branch this gate forbids would actually take once a second
kind-specific case exists. Accidentally reachable, not adversarial-only, per Rule 12's stop rule.
Fixed by recursing `_kind_member_name` into container-literal comparators; gate re-run confirmed 0
offenders on `src/fleet/`, both before and after.
**Out of scope (disclosed, not required by SPEC.md:7421's literal wording):** an aliased import
(`from … import ContractKind as CK`) or an attribute-chain form (`enums.ContractKind.X`) still
escapes the AST walk even after the I4 fix (`tests/test_ecosystems.py` M4) — I4 closed the
tuple/list/set-literal container form, not the aliasing/attribute-chain forms. The pre-existing
`Ecosystem\.[A-Z]` line scans remain in place and are not redundant with the AST walk — they
still catch the aliased-import form for `Ecosystem` that the walk does not.

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
**DONE (round R, `320bee9`).** All three violation branches (probe-returns-False, empty-diff,
outside-`dest_path`) are now exercised by three discriminating tests —
`tests/test_cli.py:4893`, `:4983`, `:5034` — each driving `_transform_criterion` to its specific
violation and asserting the correct rejection (the probe-returns-False test plants a fake
`parse_probe` that returns `False`, closing the gap the prior wording described). SPEC §12 item
10 names exactly these three branches and nothing else, so the done bar below is fully met.
**Done bar (met):** three tests, one per branch, each driving `_transform_criterion` to the specific
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
**OPEN — mixed — TEST-ONLY, not actually blocked on D50.** Ladder-length validation,
transient-vs-attempt counting, and 3-rung tier escalation to RHI are all covered. **Corrected
2026-09-01 (round Z research): the "5-rung variant blocked on D50" framing is stale and was
re-verified false, not merely re-stated.** `LadderState` (`orchestrator/retry.py:79-93`) DOES take
a `ladder=` kwarg — the "`LadderState` lacks a `ladder=` parameter" framing was false, and made
elsewhere/orally, not sourced from `tests/test_config_keys_are_read.py:58,308`'s comment
(**correction, round Z final review + fix wave, 2026-09-01**: an earlier version of this sentence
misattributed it there). That comment makes a different, narrower, and still-TRUE claim: that
`LadderState(...)` AT THAT SPECIFIC CALL SITE (`orchestrator/runner.py`, now line 512, was ~428)
is invoked with no `ladder=` argument passed — not that the `LadderState` *class* lacks such a
parameter. Conflating "this call site doesn't pass it" with "the class can't take it" is the
error; the call-site claim needed no correction, only the class-level one. More substantively, the row-count/
stop-at-5 half of this sub-clause (5-rung config → 5 attempts rows → stop, and a
declared-length-mismatch → `ValidationError` at construction) does not depend on
`context_policy_for_attempt`'s `KNOWN_INERT` wiring gap at all: `TransformSection.
_ladder_matches_attempts` (`settings.py:476-484`) already enforces the ladder-length invariant as
a real `model_validator`, and the attempt-count ceiling is already sourced from config end to end
(`cli._validate_transform_flags` → `repository.upsert_phase(..., max_attempts=...)` →
`orchestrator/runner.py:512`'s `LadderState(...)` → `LadderState.exhausted`) with no dependency on
`context_policy`'s content — an un-wired `DEFAULT_LADDER` still produces exactly 5 attempts for a
5-rung config, since `LadderState.context_policy(attempt)` clamps and repeats past ladder length
rather than raising. Atomicity is asserted by end-state only; nothing is ever interrupted
mid-write, so it can't distinguish one atomic UPDATE from two sequential ones (audit row 13).
**Done bar:** the 5-rung sub-clause is a plain TEST-ONLY one-shot — build a fixture with a
5-rung `transform.max_attempts`/`transform.ladder`, drive `fleet transform --max-attempts 5`
against a repo whose deterministic build keeps failing, assert exactly 5 `attempts` rows and no
6th. Add the (separately trivial, likely already covered) `ValidationError`-at-construction test
against `TransformSection` if not already present. The atomicity sub-clause needs a real
interruption test (kill/inject between increment and terminal write, assert no torn state) — both
parts are independently actionable now, neither blocked on D50.
**Out of scope:** do not fake the 5-rung test against inert config; that reproduces the Rule 12
"assertion weaker than its name" failure mode this file exists to prevent. This correction does
NOT retire any of D50's `KNOWN_INERT` keys — `transform.ladder.context_policy` genuinely stays
inert; §13 simply never needed it wired to prove this sub-clause.

## 14. Blast containment + escape hatch
**OPEN — misattributed to D50 until 2026-09-01 (round X), corrected.** (a) and (b) — containment
and `fleet resume` unblocking — are fully covered through real e2e paths. (c)/(d) are actively
refused: `--stub-blocked` exits USAGE, verified directly against `src/fleet/cli.py:3582-3587`/
`5483-5489` — the refusal text is "`--stub-blocked` is not implemented: emitting a generated stub
for a blocked dependency..." — this is the missing stub-creation worker gap (no worker in
`src/fleet/workers/` writes a `stubs` row for it), the exact NEW-MECHANISM item §37's own entry
already names, **not** D50's config-key-wiring thesis. D50 only mentions `--stub-blocked` in
passing, grouping it rhetorically with two other refused flags that share the same refusal
shape, not because D50 is the fix for all three.
**Done bar:** identical to §37's `--stub-blocked` stub-creation worker (build it, wire it to
actually create a `stubs` row instead of refusing exit 2) — not D50's scope. Do not open a
separate effort for a "D50 closure" here; do not duplicate §37's own done bar.

## 15. Crash safety, Git is the arbiter
**DONE (landed round P task 1, `6efc506`, reviewed Approved).** SPEC.md item 15's three clauses:
(i) discard-onto-`tasks.pre_commit_sha` — already covered pre-round with a genuinely
discriminating fixture. (ii) adopt-the-landed-commit-without-charging — already covered
pre-round. (iii) the fabricated-reverse-disagreement case (hand-edited `attempts.commit_sha`
pointing off-branch) — closed this round. Building the fixture found the property did NOT fully
hold (D87, now fixed): the column was not being corrected when it already carried a value. Fixed
and reviewed with elevated scrutiny given it touches crash-recovery state reconciliation — row
selection confirmed identical to the pre-fix query minus the removed guard, transaction atomicity
confirmed, cross-task/cross-rung stomping hazard confirmed structurally impossible. Eighth
criterion (after §12.5, §12.7, §12.12, §12.16, §12.18, §12.26, §12.32) to reach this file's strict
DONE bar. Also surfaced D89 (pre-existing, disclosed, does not affect this fix's correctness).
**Dated annotation, 2026-08-31 (documentation-accuracy review):** "row selection confirmed
identical to the pre-fix query minus the removed guard" overclaims. Read against `41fdfa1`'s diff:
pre-fix, the `commit_sha IS NULL` filter sat *inside* the row-selection subquery, so among rows for
a given `task_id` it selected the newest-by-`ORDER BY` row **among those already holding a NULL
`commit_sha`** — the selection is not the same scan with one predicate subtracted, because that
predicate was part of what rows the `ORDER BY` ran over. Post-fix, the `ORDER BY` runs over *all*
of the task's rows, unfiltered, so it always names the true newest rung regardless of what that
rung's `commit_sha` currently holds. The two selections diverge in one concrete case: the newest
rung already holds a non-NULL (e.g. fabricated) `commit_sha` and an older rung holds NULL. There,
the pre-fix query excludes the newest rung and instead selects and overwrites the **older**, NULL
rung's `commit_sha`; the post-fix query selects and overwrites the **newest** rung's — a different
row, not the same row reached by a looser scan. Post-fix is the correct direction — the newest rung
is the one §12 item 15's fabricated-reverse-disagreement clause requires be corrected — so this is
not a defect, only an imprecise description of what the fix changed. Original sentence left in
place per this file's own annotate-in-place convention.

## 16. Checkpoint integrity on corruption
**DONE (SPEC corrected, `12be741`).** The SPEC said corruption "raises `ValidationError`"; the
code deliberately never raises (`test_a_truncated_blob_invalidates_instead_of_raising`) and that
behavior is itself the intended contract. The SPEC text was corrected to match, in the same commit
as the audit. No further code or test work — verify the corrected SPEC.md wording is what's cited
in Rule 13's checkpoint count, and mark done.

## 17. Projection fidelity — "byte-identical"
**DONE (round Y task 1, 2026-09-01, ADR-0106).** Option (a) chosen over (b) after investigating
both the §21 digest question and the "real pipeline consumer" question the entry below used to
pose: `state/digest.py::run_digest` (§21) is a wholly separate mechanism from `build_state`/
`migration_state.json` and was never affected either way, and nothing in the pipeline reads
`MigrationState.updated_at` for a real purpose (staleness/cache invalidation) — `fleet status`
only renders it, `fleet resume` never reads the projection back. The one real complication found
was a test, not a pipeline dependency: `tests/test_wave_composition_projects_mid_wave.py` had a
regression case deliberately keyed on the churn (`updated_at` differing was its discriminator for
"digest-keyed instruments are wrong"); updated in the same commit (renamed, assertion flipped,
docstring corrected) rather than treated as a reason to prefer (b). Implemented:
`state/projection.py::build_state` now derives `MigrationState.updated_at` from the latest of the
already-read `phases.updated_at`/`waves.computed_at`/`contracts.detected_at`/
`collisions.detected_at` timestamps (falling back to `runs.started_at`), a deterministic function
of already-fetched rows — no new query, no schema change, and `default_factory=utcnow` is left
untouched for every other model-construction path. **Done bar met:**
`tests/test_projection.py::test_two_projections_of_an_untouched_database_are_byte_identical`
asserts raw-byte equality across two `project_once` calls on an untouched database; mutation-
proved (reverting the fix reddens it and the renamed wave-composition case, restoring it greens
both). See `docs/SPEC.md` §12 item 17's dated correction marker and ADR-0106 for the full
investigation.

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
**OPEN — SCALE-FIXTURE, narrowed (round Q task 3 landed the scale fixtures; one leg remains).**
*(Edited post-round-Q-final-review: this entry was untouched by round Q task 3 despite the task
landing exactly the two fixtures the old done bar named — see round-Q final review finding I2.)*
Round Q task 3 (`13818c1`) landed both scale cases the old done bar called for:
`test_a_12_repo_cycle_shares_one_scc_id_across_all_members` (12-node fixture, all members share
one `scc_id`) and `test_a_41_repo_cycle_completes_without_hanging` (timeout-bounded, no-hang
property). What remains open, and the reason `OPEN` is still the correct status rather than
`DONE`: the landed 12-node test asserts shared `scc_id` via a `CycleFinding`, not via a built
`PullRequestDraft` — the round's own task report explicitly scopes `PullRequestDraft.scc_id` out
("out of this file's scope"), matching `docs/SPEC.md:7434`'s literal wording ("one
`PullRequestDraft.scc_id`") rather than the looser "assert all 12 members share one `scc_id`"
phrasing the round plan used to describe the task.
**Done bar (narrowed to the one remaining leg):** a test asserting the shared `scc_id` specifically
through a built `PullRequestDraft` for the 12-node cycle case, per `docs/SPEC.md:7434`'s literal
wording. The 41-node no-hang property is closed and does not need re-doing.

## 20. Secrets never leak
**DONE (round V, 2026-09-01) — per-column coverage listed in the 2026-08-31 round-Q-final-review
dated annotation below (the "2 of 4"/"3 of 4" running counts elsewhere in this entry do not
reconcile with each other — read the per-column list, not either count); PR-body placeholder now
closed too — see the round-V annotation at the end of this entry.** SPEC.md item 20's
literal text wants a combined fixture run (mirror-URL
token, build-script-echoed token, tracked `.env`) grepped across `logs/`/`artifacts/`/
`migration_state.json` AND four named DB columns (`events.payload`, `attempts.stderr_tail`,
`phases.last_error`, `llm_cache.response_json`) AND the PR-body `«redacted:…»` placeholder.
`events.payload` was already covered pre-round. `phases.last_error` and `llm_cache.response_json`
are now covered — but via targeted per-column tests (each planting one secret shape through its
own real write path and reading the persisted value back), not the literal "one fixture run
planting all three named secret shapes" the done-bar originally described; the underlying
property each column asserts is proven, the combined-fixture methodology is not. Investigating
`phases.last_error` also found and fixed a real, live gap (D88, security-relevant):
`complete_phase`'s terminal write wasn't redacting at all, contradicting SPEC.md:6987's explicit
claim.
**Dated annotation, 2026-08-31 (round Q whole-branch review):** the line above originally read
"3 of 4 … `events.payload`/`attempts.stderr_tail` were already covered pre-round" — that
overclaimed `attempts.stderr_tail`. Measured: no test reads the *persisted* `attempts.stderr_tail`
column and asserts redaction; the only existing assertion (`tests/test_workers_scan.py:896`) is on
the in-memory `WorkerError.stderr_tail` object, not the DB column, and `record_attempt`
(`repository.py:2124-2174`) writes `stdout_tail`/`stderr_tail` with no redaction call at all. This
is D88's own class, unclosed — tracked as **D90** (`docs/INTEGRATION_HONESTY.md`), alongside two
further unredacted `phases.last_error` write paths in `orchestrator/runner.py` that bypass
`complete_phase` (one of them terminal). Per this file's own ground rule 1 discipline, the
original sentence is left in the paragraph above and corrected here rather than rewritten in
place.
**Dated annotation, 2026-08-31 (round Q task, D90 fix landed):** D90's fix is landed —
`redact_text` now runs at `runner.py`'s `_terminate_uncharged` and `_record_diagnostics` UPDATE
sites and inside `repository.py`'s `record_attempt` for `stdout_tail`/`stderr_tail`, each proven
by a persisted-column test (credential-shaped secret planted through the real write path, read
back off a real SQLite row) plus a companion over-redaction control, and each discriminated by
mutation (revert the `redact_text` call → the new test goes genuinely RED with the live secret
visible → restore → green). That closes the `attempts.stdout_tail`/`stderr_tail` gap this
annotation's prior paragraph named and the two `runner.py` write paths D90 traced — **3 of 4**
named DB columns are now actually covered (`events.payload`, `attempts.stderr_tail`,
`phases.last_error`; `llm_cache.response_json` unchanged from D88, not re-verified by this task).
Tests: `tests/test_runner.py::test_terminate_uncharged_redacts_a_credential_in_last_error_before_the_write`,
`tests/test_runner.py::test_record_diagnostics_redacts_a_credential_in_last_error_before_the_write`,
`tests/test_repository.py::test_record_attempt_redacts_a_credential_in_stdout_and_stderr_tail_before_the_write`.
**Dated annotation, 2026-08-31 (round-Q-final-review, finding I3 — reconciling the "2 of 4" /
"3 of 4" counts).** The two running counts above (this entry's heading originally said "2 of 4";
the annotation immediately above this one says "3 of 4") do not reconcile with each other: read
literally, "2 of 4" excludes `llm_cache.response_json` from the covered set while the sentence
six lines above it says `llm_cache.response_json` "are now covered". Per Guardrail 6, retiring
both raw totals in favor of the class result — a per-column status list, which is authoritative
going forward over any "n of 4" phrasing anywhere else in this entry:
- `events.payload` — covered pre-round.
- `attempts.stdout_tail` / `attempts.stderr_tail` — covered by D90 (persisted-column test +
  over-redaction control, mutation-discriminated).
- `phases.last_error` — covered by D88 (`complete_phase`) and D90
  (`_terminate_uncharged`, `_record_diagnostics`).
- `llm_cache.response_json` — covered by D88 only; **not** re-verified by D90's task.
**Done bar (remaining, pre-round-V):** the PR-body `«redacted:…»` placeholder clause — still
entirely unverified, confirmed by two separate rounds' investigations (round M found a different
leak on the PR path; round P confirms this specific clause remains untouched). Whether the
four-column grep-sweep methodology also needs a literal combined-fixture test, or whether the
now-proven per-column properties satisfy the criterion's intent, is worth a brief adjudication
before attempting — check whether SPEC's own "either/or" language elsewhere in §12 offers a
precedent for accepting equivalent per-column coverage.

**Dated annotation, 2026-09-01 (round V controller ruling):** adjudicated the methodology
question above — per-column coverage (not a literal combined multi-secret fixture) is accepted
as satisfying this criterion's intent. Per CLAUDE.md Rule 14, this adjudication is now disclosed
at its actual site rather than only here: `docs/SPEC.md`'s §12 item 20 sentence carries a dated
2026-09-01 in-place marker recording the same ruling, and the decision itself is recorded in
**ADR-0104**, which names the actual precedent this follows — **§12.7 / ADR-0099** (a committed-
fixture-repo methodology named literally, not built, because the underlying property already held
via a different, self-contained proof mechanism; §12.20 is the same shape one level down). Round V
then closed the
PR-body placeholder clause: `tests/test_pr_body_redaction.py` plants a credential-shaped secret
in `relocation_summary`, drives the real `render_body` → `PullRequestDraft.body` →
`_write_pr_record` → `redact_text` path, and asserts the persisted `findings.payload` carries the
`«redacted:...»` placeholder rather than the live secret, plus an over-redaction control —
mutation-proven and independently reproduced by task review (real revert, real red, clean
restore). `human_intervention_notes`/`weak_edges` share the identical redaction boundary
(confirmed: `render_body` serializes all three fields into one body string before
`_write_pr_record` ever redacts it), so one representative field is sufficient. **All four DB
columns plus the PR-body placeholder are now covered — this criterion's full stated text passes.
Criterion DONE.**

## 21. Determinism — clean re-run, byte-identical digest
**DONE (all three clauses landed, round O `daf2a24` + round Q task 2).** SPEC.md item 21 has
three clauses: (1) clean re-run under `--llm-cache read-only` produces a byte-identical digest —
**covered** (`tests/test_cli.py::test_status_digest_is_byte_identical_across_two_clean_db_runs_under_a_warm_llm_cache`,
reviewed Approved through one fix round; two real from-scratch runs, cache warmed then seeded
cross-run per `schema.sql`'s own documented "NOT scoped to run_id" design, second run forced
through an exploding backend so success requires a genuine cache hit; the digest's `waves`/`edges`
sections are non-trivially exercised, the other 5 are honestly disclosed as structurally
empty-but-equal in this fixture, not silently overclaimed). (2) `--llm-cache read-only` with a
cleared cache fails loudly — **covered pre-existing** (`test_llm_cache.py:454`,
`test_run_context_llm_cache.py:279`). (3) **mutating one fixture source file changes the digest —
now covered** (round Q task 2:
`tests/test_cli.py::test_status_digest_differs_when_a_fixture_source_file_mutates_between_two_clean_db_runs`),
the direct inverse of clause 1's test, same two-workspace/warm-cache/forced-read-only harness,
with `acme-app`'s `package.json` dependency spec on `@acme/lib` rewritten from an open range
(`^1.0.0`) to a pinned exact release (`1.0.0`) — committed to the shared source git repo between
run 1 and run 2 — which `graph/infer.py`'s `_is_pinned` reads to choose the edge `kind`
(`DECLARED_DEP` vs `PUBLISHED_ARTIFACT`), a value `state/digest.py`'s `edges` section hashes. The
mutation is confirmed non-cosmetic via `git diff --numstat` inside the test (Rule 12's
zero-change gate), confirmed to land specifically in the `edges` section (per-section digests
compared, not just the whole-run digest), and confirmed discriminating by an explicit control:
with the mutation commit skipped, the same test fails at the pre-mutation-state assertion rather
than passing vacuously. `.ts` source content was deliberately NOT the mutation target — the
classify prompt (`ClassifyWorker._messages`) sends a path listing only, never file bytes, and the
npm ecosystem adapter parses only `package.json`, so a `.ts` edit would not move the graph at
all in this fixture and risked a false-negative test. `docs/PROGRESS.md`'s `<n> of 48` tally may
now count §12.21.

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
**OPEN — one sub-clause blocked on D23, otherwise DONE (round S, `4e1975d`).** Re-transform is
fully covered. Re-scan now covers all 8 of the named tables (`edges, contracts, symbols,
manifests, findings, collisions, waves, wave_members` per `docs/SPEC.md:7438`): 6 under re-scan
directly in `tests/test_scan_e2e.py` (`symbols, edges, manifests, findings` extended in-place,
`waves, wave_members` newly added this round), and 2 under re-sequence (`contracts, collisions`,
via a genuine 9-repo, real-git, real-CLI vendored-contract fixture newly added this round, plus
`tests/test_sequence_e2e.py`'s pre-existing, narrower `contracts` coverage). **Correction,
2026-08-31:** this entry's prior "6 of 8" line predates round S and had no citation; re-measured
against `tests/test_scan_e2e.py` at round S's start, the true pre-round figure was **3 of 8**
(`edges, symbols, manifests`) — round S closed the remaining 5.
**Remaining, genuinely unclosable by a test:** `edges.retargeted_from_repo_id` is never persisted
(`state/repository.py`'s `insert_edges`/`EdgeRow` carry no such column/field at all — the value
`graph/cycles.py:736` computes in memory is structurally dropped before it reaches SQL) — this is
`D23` (`docs/INTEGRATION_HONESTY.md`), OPEN, not something a TEST-ONLY task can close.
**Done bar:** fix D23 (persist `retargeted_from_repo_id` for real), then one test asserting it
survives a second scan unchanged. Everything else in this criterion is already closed.

## 24. Fail-closed budgets, ledger moves correctly
**DONE (round Y task 4, `f88e105`/`193c319`).** Wave-breach, over-reserve-refused, in-flight-wait,
the ledger-sum invariant, and the run-ceiling clause were already covered (rounds R and U — see
below). The **only** remaining open item was local-profile row completeness, tracked as D62
(`llm_backend` etc. were NULL — nothing wrote them). D62 closed in full: `f88e105` wires
`llm_backend` (ADR-0107's last-non-empty-wins ruling in `accumulate`), `llm_failovers` (new
`TokenUsage` counter, stamped in `LadderModelClient.complete()`), and `input_tokens`/
`output_tokens` (pure wiring) into `AttemptRow`/`record_attempt`/`iter_attempts`/`cli.py`'s
writers. `193c319` adds
`tests/test_runner.py::test_a_local_profile_run_writes_a_non_empty_backend_on_every_row_at_zero_cost`
— a real `PhaseRunner.run_wave` dispatch under a free-priced target, sibling to the round R
ledger-sum test and using the same harness, proving the criterion's own literal text: the run
ends with `spent_usd == 0` and every `attempts` row carries a non-empty `llm_backend` and
`llm_cache_hit == 0`. See `docs/INTEGRATION_HONESTY.md` D62 (now `FIXED, LANDED`) and ADR-0107
for the full design and rationale.
**Done bar (met in full):** one e2e test asserting the ledger-sum invariant **(met, round R)**;
one CLI-level test that organically breaches the run ceiling and asserts exit 3 **(met, round
U)**; one e2e test proving the local-profile row-completeness clause **(met, round Y task 4)**. Do
not re-dispatch any of the three sub-clauses above — all three are done.
**Historical note on the run-ceiling clause (round U, `3f089a4`):** the brief's literal scenario
(seed `run_max_cost_usd` below a known cost) is architecturally unrepresentable —
`schema.sql`'s `CHECK (spent_usd + reserved_usd <= max_usd)` makes `spent_usd > max_usd`
unseedable, and every CLI `PhaseRunner` site reserves $0 (no production `TokenEstimator` callers)
— so `tests/test_cli.py::test_run_cost_exhausted_exits_3` uses the durably-true equivalent (a
pre-seeded halted ledger), verified by task review via a full call-chain trace against source to
be a legitimate, organic exercise of the exit-3 path, not a synthetic shortcut.

## 25. The unknown repo survives the pipeline
**OPEN — SCALE-FIXTURE.** The `no-manifest` finding and `misc/<repo_id>` destination are covered.
`repos.kind == 'unknown'` conflates two different fields (`Ecosystem.UNKNOWN` vs. the classify
worker's advisory `kind`) and no test exercises the real one. The unknown filegroup is never built
under real bazel (audit row 25).
**Done bar:** add an unknown-ecosystem repo to the real-bazel e2e fixture (currently 2 TS + 2
Python only) and assert it builds; fix or split the `kind` conflation into two separately-named
assertions.

## 26. Preflight gates rather than crashes
**DONE (all five fixture categories landed, round O `1c8e0ef` + round P `659d4d5`).** SPEC.md
item 26's full text (`:7441`) names five fixture categories: empty, shallow, submodule-bearing,
LFS-bearing, and default-branched to `trunk` — this file's own earlier done-bar missed the fifth,
caught only while closing round O. Empty-repo and shallow were already covered pre-round;
submodule/LFS landed round O with real fixtures (genuine `git submodule add`, genuine LFS pointer
format); `trunk`-default-branch landed round P (genuine `git branch -m trunk`, confirmed to
exercise the primary `symbolic-ref` resolution path, not merely coincide with the fallback list).
All five reviewed Approved, each asserting `SUCCEEDED` + the documented `repos` columns, matching
the criterion's own "either proceed or produce a `PreflightFailed` finding" wording verbatim. D41
covers a different defect (never-ran vs. settled-negative), not this gap. Seventh criterion (after
§12.5, §12.7, §12.12, §12.16, §12.18, §12.32) to reach this file's strict DONE bar.
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
**DONE (round S, `0742a0f`; disclosure corrected 2026-08-31, see round S's final review, finding
I3).** `docs/SPEC.md:7443` states three clauses: (a) an integration test that starts the runner
and shows a second writable `aiosqlite` connection in the same process raises; (b) pool children
constructed with no DB handle, verified by inspecting the initializer arguments; (c) a 200-repo
simulated run produces zero `SQLITE_BUSY` errors.

* Clause (b) is met literally: `tests/test_budgets.py`'s `test_new_cpu_pool_passes_no_db_handle_bearing_kwarg`
  calls the real `new_cpu_pool` through a spy on `ProcessPoolExecutor` and asserts the only kwargs
  present are `max_workers`/`mp_context` — no `initializer`/`initargs`. Mutation-verified (a
  spurious `initializer=` kwarg reddens it).
* Clause (a) is met at the `StateWriter` level, not the runner level — a disclosed substitution.
  `tests/test_db.py`'s `test_second_writable_connection_raises_while_the_writer_is_live` starts a
  `StateWriter` (not the runner) and shows a second writable connection raises
  `SingleWriterViolationError`. This is a pre-existing test, not round S's doing, but it is
  load-bearing for this DONE marking; no runner-level integration test of this property exists.
* Clause (c) is met by 200 concurrent `writer.submit()` coroutines against the `StateWriter`
  actor (`tests/test_db.py::test_concurrent_submits_all_land_and_each_result_reaches_its_own_caller`,
  scaled from 50 this round) — an adjudicated stand-in for the SPEC's 200-*repo* simulated run,
  disclosed here rather than attributed to the SPEC's literal wording. Reason the stand-in is
  accepted: each `writer.submit()` is exactly the primitive one repo's dispatch loop calls, so 200
  of them landed concurrently (~50ms observed, gapless `seq` 1..200, zero `SQLITE_BUSY`) proves the
  single-writer actor's concurrency-safety property at real scale — the property clause (c) exists
  to protect. It is not literally a 200-repo run: `src/fleet/` has 62 `.submit(` call sites, and a
  real repo run issues far more than 200 total `.submit()` calls across many repos' phases, so this
  test proves the actor handles 200 concurrent callers, not that a 200-repo run in particular stays
  `SQLITE_BUSY`-free end to end. Mutation-verified (a real `await`→`create_task` concurrency bug
  reproducing a genuine `sqlite3.OperationalError`).

Kept DONE rather than downgraded to OPEN, per this file's own remedy-of-choice: the property each
clause exists to protect is genuinely exercised (writer-actor concurrency safety at scale, no-DB-handle
pool children, and a single-writer violation raising rather than queueing), even though (a) and (c)
are met one layer below the SPEC's literal unit (writer instead of runner; concurrent coroutines
instead of concurrent repos). If a future round wants the literal runner-level and repo-level forms,
treat that as new scope, not as evidence this entry was wrong.

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
**DONE (round U, 2026-09-01) — SCALE-FIXTURE.** The hardcoded-dir grep was already covered. Round
U closed the remaining two gaps: the ts→js monkeypatch test now drives a real
`NpmAdapter().publishes()` → `BuildUnit` → `BuildTarget.package` assertion (exercises the
scoped-package `path_tail`, not just the old bare unscoped case); the config-override e2e now
runs a full second scan→sequence→transform→build pipeline under the default layout and diffs its
output tree against the override run's (destination-string masked for the one legitimate
self-referencing difference, ADR-0048's npm hub link), proving the "identical tree" claim. Both
mutation-proven, independently reproduced by task review from scratch (matching
`AssertionError`s, clean reverts) — commit `7427626`, merge of `agent/roundu-task2`.

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
**OPEN — mixed, 20 sub-clauses — now blocked on D92/D93/D94, not TEST-ONLY.** The headline
refusal (exit 2 + PrState unchanged) is well covered by two independent tests. **Round Z task 2
re-audited the sweep sub-clauses against D80's landed `stub_reconcile`** (search
`tests/test_cli.py:2535-2701`) and closed the genuinely missing pieces: the `SUPERSEDED` arm of
the refusal guard (was untested — only `ACTIVE` ever seeded), and the positive case (a genuinely
`RESOLVED` stub → `fleet pr --ready` opens non-draft and fires `gh pr ready`) — both
mutation-proven, independently reproduced by task review. The T4 abandon path and the
held-for-merge carve-out were already covered pre-round, confirmed by the same re-audit.

**The remaining sub-clauses are now blocked on three real production gaps this same
investigation found and disclosed, not left as untested-but-buildable TEST-ONLY work:**
- **D92** — `PrState.HELD` is declared and documented as `stub_reconcile`'s exclusive write
  target but has zero production writers anywhere in `src/`.
- **D93** — no exit-code path reads `RepoStatus.DEGRADED` for the exit-7 contract SPEC §3.5.1
  point 5 (and `HumanInterventionError`'s own docstring) claims — a real SPEC/docstring-vs-code
  mismatch on an operator-facing exit code.
- **D94** — this criterion's own "resolution" sub-clause (an already-open PR getting promoted to
  ready once its stub resolves — rebase/force-push/body regeneration) has no implementation at
  all; `_pr_impl` skips any repo with an existing PR record unconditionally. This is why the
  resolution sub-clause specifically cannot be tested, not merely untested.

**Done bar:** D92/D93/D94 need their own dedicated work (D94 in particular is NEW-MECHANISM
sized, not a one-shot) before the remaining sub-clauses of this criterion become buildable. Do
not attempt to test-write around them.

## 39. Bounded, priced rework; stub rot reaches a human
**OPEN — mixed, 18 sub-clauses — TEST-ONLY, mostly blocked on §12.37's wiring.** Case (iii)
(batched=1/eager=3 cost comparison) is well covered. Cases (i)/(ii) only test what's handed in as
a parameter rather than driving it from a real stub-rot scenario (audit row 39).
**Done bar:** once §12.37 is wired, drive cases (i)/(ii) through the real stub lifecycle rather
than direct parameter injection. Sequence after §12.37, not before — closing this first would just
re-test the same disconnected parameters.

## 40. No model string outside `config/`
**DONE (SPEC + code corrected, round-K; AST clause closed round V, 2026-09-01 — now counts
toward the `<n> of 48` tally for the first time; the exclusion marker below is retired.)** The
criterion's own greps were unsatisfiable as written (named a nonexistent `llm/routing.py`, and
returned non-zero hits without the backend-file exclusion). Round-K applied the exclusion for
real and validated it three ways (clean tree / exclusion removed / synthetic fault injected).
Round V closed the last gap (audit row 40, M1): `tests/test_models_yaml_ast.py` is a real AST
walk (modeled on `tests/test_ddl_ast.py`'s precedent) asserting only `src/fleet/settings.py`
reads `config/models.yaml` — mutation-proven, independently reproduced by task review via a
standalone walk finding the identical 3 sites, all in `settings.py`. **This criterion's full
stated text now passes — both the model-id/endpoint greps and the AST clause.**
**Done bar:** met in full. Nothing remains open for §12.40.

## 41. Local-only profile runs the whole pipeline
**OPEN — structural gap, no test at all — NEW-MECHANISM (test infra); the SPEC-vs-config drift
that used to block this is resolved (round W, ADR-0105).** No test anywhere runs any pipeline
phase under a non-`default` profile (9 of 11 sub-clauses unasserted) — this is now the ONLY
remaining blocker. The drift this entry used to name (§12.41 said local CHEAP targets "declare"
both capability fields `false`; `config/models.yaml` omits them, deliberately) was traced and
adjudicated as wording-precision, not a behavior gap — `merge_capabilities`'s dict-overlay makes
an omitted key fall through to the backend's already-`false` declared floor, byte-identical
either way. `docs/SPEC.md`'s sentence carries a dated marker; see ADR-0105.
**Done bar:** run at least a Phase-1-through-Phase-3 slice of the fixture fleet under `--profile
local` (or whatever the local-only profile is named) and assert every named sub-clause. This is
genuine new test infrastructure (a runnable local-profile fixture fleet), not a one-shot task.

## 42. New backend costs one file + one registry line
**OPEN — this done bar's own scope closed (round N, `cc12a2a`), criterion overall still 5 of 9.**
`register_backend`'s duplicate-name `RuntimeError` now has a real, reviewed-Approved test
(`tests/test_llm_client.py::test_register_backend_refuses_a_duplicate_name`) — the specific gap
this file's done bar named. Four of the other eight startup refusals were already covered before
this round; four remain open (not this round's scope). Do not count §12.42 toward the `<n> of 48`
tally — the criterion as a whole is still open.

## 43. Failover layered, bounded, fail-closed
**OPEN — case (ii) is the sole remaining blocker, everything else in this entry closed.** Case
(ii) is entirely absent, blocked on D55/D58's circuit-breaker gap (explicitly out of round-Y's
size class, needs its own dedicated round). `llm_failovers` recording (D62) closed round Y task 4
(ADR-0107): `TokenUsage.llm_failovers` is stamped from `LadderModelClient.complete()`'s own
retry-loop index, wired through `AttemptRow`/`record_attempt`, with a §12.43-case-(i)-shaped test
in `tests/test_llm_backend_failover_attribution.py` proving the CONNECTION-trigger shape. The two
independently-actionable status/message tests this entry previously named closed round X task 1.
**`TierUnavailable`'s message-provenance/tier-attribution honesty fields (D78) closed round Y
task 3**: `WorkerError` gained a `tier` field, `_error_for` populates it from a real
`TierUnavailable`, `PhaseRunner._drive` forwards it into `record_backend_unavailable` —
`payload["failover_triggers_scope"]` can now genuinely read `"tier"` in production instead of
always `"run"`, independently confirmed by task review inferring the closure straight from the
diff's own data flow (no separate end-to-end test drives a real `TierUnavailable` through the
whole `_drive` path in one run; the two halves — `_error_for` populates, `_drive` forwards — are
proven separately, which is sufficient since neither has untested branching between them).
**Done bar (remaining):** case (ii) only — the D55/D58 circuit-breaker gap.

## 44. Cache not poisoned across backends
**DONE (round W, 2026-09-01) — all 6 sub-clauses of the original audit's "1 of 6 full, 4 partial,
1 absent" breakdown now closed.** Sub-clause E (tamper detection) was already real, closed round O
(`70a4398`): `tests/test_llm_cache.py` recomputes `cache_key` from a persisted row's own
SQL-read-back columns via the real `hashing.cache_key()` primitive and proves a direct-tamper
mismatch. Round W closed the remaining five without needing the stub OpenAI-compatible server this
entry previously assumed was required — genuine two-profile scenarios (two single-target
`LlmRouter`s sharing one cache store, differing only in `backend`) sufficed: **A** (two distinct
`llm_cache` rows across profiles), **B** (the second profile's identical call reports a miss),
**C** (neither response leaks into the other profile, checked by value not just call count), **D**
(re-running either profile a second time hits with `cost_usd = 0`), and **F** (a real `local`
profile call's `all_served_from_llm_cache = False`/`cost_usd = 0.0` are asserted together on the
same object, so a free call is never miscounted as a cache hit — traced against `cli.py`'s real
`attempts.llm_cache_hit` write site, the exact field pair that becomes the persisted column). Both
new mutations independently reproduced by task review in the implementer's own worktree, not
trusted from the report. TEST-ONLY, no production code touched. Commit `e526f07` (merge of `agent/roundw-task1`).

## 45. No code state persisted outside Git
**OPEN — mostly missing — TEST-ONLY, no adjudication remains.** **Corrected 2026-09-01 (round Z
research): the "needs adjudication first" framing is stale — this entry's own next sentence
already discloses the adjudication is done.** SPEC-ADJUDICATION sub-part: the forbidden-column
regex was corrected (`collisions.blob_shas` no longer false-matches); M2 records the "five vs.
one" count is predicate-dependent and the SPEC marker already carries that distinction (see
`docs/SPEC.md` §12 item 45's dated marker), so no further SPEC edit is owed there. What remains
is 5 of 7 sub-clauses of mechanical build work, each independently small: a real `PRAGMA
table_list`/`table_info` sweep, the 40-hex enumeration fix, a hunk-header scan, a
delete-artifacts-then-resume test, and coverage for 3 of 6 git trailers — none needs new
production code or a design decision, all read existing state. Substantively, 5 of 7 sub-clauses are
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
**DONE (round Z, 2026-09-01) — all 13 sub-clauses now covered.** 8 were already solidly covered
(illegal transitions raise, abandoned-reopen gating, stale-lease rejection, reap-in-flight
discard, 32 KiB truncation, zero-duplicate-commit re-entry). Round Z closed the remaining 5,
across two tasks:
- **Task 1** (TEST-ONLY, 3 files): round-trip switched from object equality to a real
  `model_fields`-based comparison, matching the criterion's literal wording — independently
  verified by task review to be mathematically equivalent to object equality *for this codebase*
  (extra="forbid" everywhere, zero `PrivateAttr`), so the value of this fix is literal-mechanism
  compliance, not a live bug closed; `Resolution` (`models/build.py`) gets real coverage via a
  genuine adapter path (`PyAdapter.resolution()`); `edges.edge_key` gets a persisted-value
  sibling test alongside the existing inference-layer one; `acquire_phase_lease` gets a
  concurrent-CAS test (8-way). All 4 independently mutation-proven by task review.
- **Task 3** (real production fix, `state/repository.py`): the reaper's RHI leg
  (`complete_phase`) — a genuinely distinct site from D77's own fix (`append_blocked_by`), which
  only moves DEGRADED→BLOCKED and leaves `complete_phase`'s separate raw-SQL bypass untouched —
  now routes its RHI-escalation write through the real `transition()` gate instead of an
  unchecked `CASE WHEN`. Closes a real, independently-verified-reachable race: D77's own fix can
  legally move a `RUNNING` phase to `BLOCKED` without bumping the fence `complete_phase` checks,
  and `BLOCKED→REQUIRES_HUMAN_INTERVENTION` is illegal per `ALLOWED_TRANSITIONS` — previously
  written anyway. Tracked as **D95, FIXED, LANDED**.

The `stub_reconcile` re-audit question this entry previously carried was resolved by **round Z
task 2**: `tests/test_cli.py:2535-2701` already covered the T4 abandon path and the held-for-merge
carve-out; task 2 added the genuinely missing piece, a new test proving `stub_reconcile` cannot
move a repo out of `REQUIRES_HUMAN_INTERVENTION` (§12.46(ii)) — independently reproduced by task
review. See §38's entry for the same investigation's other findings.

**Correction, round Z final review + fix wave (2026-09-01): two of the 13 sub-clauses this DONE
marking assumed closed were not actually driven by any test.** A whole-branch review found (a)
§12.46(ii)(c)'s reaper leg — none of the nine existing `reap_expired_phase_leases` call sites in
`tests/` ever seeded an RHI-status row, so nothing had actually exercised the reaper's own SQL
guard against it (the "reaper's RHI leg" language in Task 3 above describes `complete_phase`'s
legality check, a different sweep); and (b) §12.46(i)'s population clause — `InternalDep`
(`src/fleet/models/build.py:15`) was not re-exported through `fleet.models.__all__` at all, so the
existing parametrized round-trip test never ran against it. Both closed for real in the same fix
wave: `tests/test_repository.py::test_the_reaper_never_reclaims_a_requires_human_intervention_row`
(Rule-12 mutation-proven — dropping the reaper SQL's `status = 'RUNNING'` guard reddens it) and
`InternalDep` added to `fleet.models.__all__` + `SAMPLES["InternalDep"]` (the round-trip test now
covers it automatically). The DONE marking stands, now for real.

**Second correction, same day (2026-09-01), round Z fix-wave re-review:** the (b) fix above closed
the *reported* site but not the *class* — an independent re-derivation (a runtime walk of
`fleet.models`'s submodules plus a textual `grep '^class \w*(.*FleetModel'` sweep, both agreeing on
36 `FleetModel` subclasses total) found one more model absent from `__all__`: `Resolution`
(`src/fleet/models/build.py:189`). `Resolution` had a round-trip assertion
(`tests/test_ecosystems.py:1187`) but only via object `==`, the exact form §12.46(i)'s literal text
rules out ("compared via `model_fields`... NOT via object equality"). Closed identically to (b):
`Resolution` added to `fleet.models.__all__` + a non-degenerate `SAMPLES["Resolution"]` (populated
`inputs`/`env`, exercising the nested-model and dict/list fields) —
`pytest tests/test_state_models.py -k Resolution` passes
`test_every_exported_model_round_trips_through_its_own_json[Resolution]`; full `test_state_models.py`
+ `test_ecosystems.py` 213 passed, `ruff check` clean on both touched files. Both derivations that
found this gap are recorded above so a future sweep can reproduce them rather than re-deriving the
36-model count from scratch.
**Done bar:** met in full, independently re-derived twice for the population clause specifically.
Nothing remains open for §12.46.

## 47. Registries stateless, total, order-independent
**OPEN (reverted 2026-09-01, controller ruling C1 on this round's own final review — the `**DONE`
marking below was retracted the same round it was made; see "Why reverted" below).**
`tests/test_registries_stateless.py` (new) and `tests/test_manifests.py` are real work and 3 of
round T's 4 claimed closures stay credited: `workers` registry now has an `assert_stateless` call
site via the real `discover()`; a structural test proves `preconditions_hold` is abstract on
`BaseWorker` (walks `discover()`, fails on any class using the base default — mutation-proven:
deleting `@abstractmethod` reddens it); the 3-hand-written-orders test was replaced with a genuine
`random.Random` 20-shuffle test over a 6-item set with an anti-vacuous distinctness check. These
three are mutation-proven per Rule 12 and stay closed (commit `09b1929`, merge of
`agent/roundt-task2`). Manifests/ecosystems statelessness and the rule-engine/backend-name startup
refusals were already covered before round T and are unaffected by this reversion.

**Why reverted:** SPEC.md:7462's literal text requires "Every `discover()` asserts
`vars(inst) == {}` for every registered instance across all five registries (workers, manifests,
ecosystems, contracts, backends)." Review of round T's close-out found two problems: (a) there is
no contracts registry anywhere in `src/fleet/` (`grep 'def discover(' src/ --include=*.py` finds
exactly 4: `llm/client.py`, `orchestrator/registry.py`, `manifests/base.py`, `ecosystems/base.py`
— no contracts one); (b) `vars(inst) == {}` is measurably FALSE for the backends registry —
`openai_compatible`'s instance holds `_env`/`_transport` (`src/fleet/llm/backends/openai_compatible.py:264-265`).
**Citation-drift disclosure (2026-09-01, fix wave):** this claim was true when made (round-V's
final-review measurement, pre-task-1-fix) and is left as-is per CLAUDE.md's "annotate never
rewrite" discipline. Task 1's fix (the "(ii)" entry below) moved the code: `:264-265` is now the
`__init__` signature's `transport`/`env` parameters, and the conditional `self._transport =
transport` / `self._env = env` assignments this claim describes now sit at `:276`/`:278`.
Round T's 4th claimed closure substituted a weaker property for the backends registry (singleton
identity across repeated `discover()` calls + no new/replaced attributes across two
`declared_capabilities()` calls) in place of the criterion's literal `vars(inst) == {}`, with no
Rule-14 adjudication marker or ADR disclosing the substitution. Per CLAUDE.md Rule 14, a §12
criterion's literal wording can only be relaxed by disclosed adjudication (dated marker + ADR) —
this substitution wasn't disclosed that way, so the criterion cannot be marked DONE on it.

**(ii) Backends registry statelessness — DONE (round V, 2026-09-01).** Implemented for real, not
adjudicated away: `vars(inst) == {}` is now genuinely true for every registered backend instance.
`bedrock`/`vertex`/`openai_compatible`'s `__init__` methods conditionally-assign
(`if x is not None: self._x = x`) rather than unconditionally storing constructor-injected
collaborators, so the zero-arg registration construction (`register_backend`'s
`_BACKENDS[cls.name] = cls()`) yields a genuinely empty `__dict__`; the collaborator resolves
lazily via `getattr(self, "_x", <module constant>)` at call time. `anthropic.py` was already
stateless. No `__slots__` loophole used — task review verified `assert_stateless`'s literal
`vars(inst)` check applies for real here. `tests/test_registries_stateless.py`'s round-T weaker
singleton-identity stand-in is replaced with the real, unweakened `assert_stateless` call.
Commit `d9317e7` (merge of `agent/roundv-task1`).

**(i) Contracts registry — resolved fork, still open, tracked via ADR-0065.** Round V's research
(dispatched to resolve this fork, not to guess at it) found the "contracts registry" is a real,
deliberately designed fifth pluggable registry — `docs/SPEC.md` §7.6 fully designs
`ContractAdapter` (ABC + `@register` + `discover()` asserting totality over `ContractKind`), and
`docs/DECISIONS.md`'s ADR-0065 explicitly records it as designed-but-**NOT YET IMPLEMENTED**,
naming the exact package (`src/fleet/ecosystems/contracts/{base,proto,openapi,avro,thrift,
shared_lib}.py`) and explicitly rejecting `workers/contracts.py`'s `discover_contracts` (a pure
extraction function, not a registry — confirmed by its own docstring) as a stand-in. §12 items
29/31/32 already assume/reference this same undone registry; §12.32 already treats its own
equivalent clause as "UNSATISFIABLE AS WRITTEN... NOT a passing gate (ADR-0065)" — i.e. disclosed
and adjudicated, not silently guessed at. **This is not a "SPEC is stale" situation — the
"stale/overbroad" fork this entry's prior text offered is rejected on the evidence** (see
`.superpowers/sdd/round-V-criteria-closure/research-1-report.md`'s Part A if that workspace still
exists, or ADR-0065 directly). **Done bar:** build `src/fleet/ecosystems/contracts/` for real —
5 adapter files + base ABC + `discover()`/`for_kind()`, per SPEC §7.6's literal design. This is
genuine NEW-MECHANISM work, comparable in scope to D89 Phase 2 (multiple dependent tasks, its own
dedicated round), not a one-shot residual fix — deferred to a future round, same treatment §12.32
already gives the identical fact. Do not re-run the "is this stale" investigation; it is settled.

**Disclosed, not closed (pre-existing, unrelated to the above):** `assert_stateless` is
structurally blind to `__slots__`-stored state (by the helper's own documented design,
`src/fleet/workers/base.py:559-571` — a `__slots__` class has no `__dict__` for the check to
inspect). Pre-existing property of the shared helper, not introduced by round T's diff; flagged
for a possible future D-number if it ever needs closing, not part of this criterion's literal
text.
**Out of scope:** do not add a `ProcessPoolExecutor` initializer — that clause is retired, see
§12.28's entry.

## 48. Startup + version refusals before any cost
**DONE (round T, 2026-09-01) — 5 independent claims, all closed — TEST-ONLY.** `migrate-db`
exception, the `BEGIN EXCLUSIVE` ladder, stale-checkpoint-invalidated, and
truncation-retry-exactly-once were already covered. Round T closed the remaining five, in
`tests/test_cli.py`, `tests/test_ddl_ast.py` (new), `tests/test_resume_continue.py`: the
schema-version-refusal test is now parametrized over all 18 of the 22 `@app.command`-family
verbs that actually touch the DB (`_SCHEMA_CHECKED_COMMANDS`); the 4 excluded (`migrate-db` + 3
`models` subcommands) are excluded with a stated reason — they call only `_load_settings`, never
`_require_db` — not silently dropped; the refusal message's both-versions assertion is added;
"before a clone or an LLM call" turned out expressible (no Rule 14 flag needed) via a
call-recording spy asserting `calls == []` — the first exit-code-only design was a false
discriminator (couldn't distinguish "refusal never fired, clone ran" from "refusal never fired,
clone failed on its own"), caught and fixed by the implementer before reporting; a DDL AST test
matching the criterion's literal text now exists (`tests/test_ddl_ast.py`); the mirror-mutex
refusal now asserts exit code 2. All 5 assertions mutation-proven per Rule 12, independently
reproduced by task review against the worktree at commit `9342732` (merge `81561b1`, merge of
`agent/roundt-task3`).

---

## Rollup

| status | count | criteria |
|---|---|---|
| DONE | 21 | 1, 5, 6, 7, 10, 12, 15, 16, 17, 18, 20, 21, 24, 26, 28, 32, 33, 40, 44, 46, 48 (re-derived 2026-09-01, round Z, by scanning every `^**DONE` heading in this file and pairing each with its nearest preceding `## N.` heading — added §46 this round (all 13 sub-clauses closed: 4 TEST-ONLY + a real production fix, D95, at the reaper's RHI leg); §47 remains OPEN per round T's controller ruling C1, unaffected by this round (its `vars(inst) == {}` claim for the backends registry closed round V, its contracts-registry residual is separate, tracked in §47's own entry via ADR-0065) |
| OPEN — WIRING (cheapest, do first) | 0 | none currently — §27 and §37 were both reclassified NEW-MECHANISM by their own entries (round-K/2026-08-30 correction; each needs a new D-number and new upstream data capture or Phase-3 consumer, not a caller-wiring task) and are now counted in "everything else" below; corrected 2026-09-01, this row was stale since the reclassification landed |
| OPEN — SPEC-ADJUDICATION needed before work starts | 0 | none — §45 moved out 2026-09-01 (round Z research): its own body already disclosed the adjudication was done, the row's label was stale, see §45's own entry |
| OPEN — blocked on an existing D-number, don't duplicate | 5 | 22 (partial, D50 for one sub-clause only), 35 (partial), 36, 38 (partial — now blocked on D92/D93/D94, corrected 2026-09-01 round Z, see §38's own entry — D80 is fully landed and no longer the blocker), 43 (partial) |
| OPEN — everything else (TEST-ONLY / SCALE-FIXTURE / NEW-MECHANISM) | remainder | 13 (not actually blocked on D50 — corrected 2026-09-01, round Z research, see §13's own entry), 14 (misattributed to D50 until round X — real blocker is §37's `--stub-blocked` stub-creation worker, not a D-number, see §14's own entry), 27, 37, 39 (mis-bucketed as D-number-blocked until round Z research — its own entry names no D-number, only §37's wiring), 41 (all NEW-MECHANISM except 13/39/45; §41's own adjudication blocker cleared round W, ADR-0105 — see above), 45 (TEST-ONLY, see above), plus all others not listed in a row above — see individual entries |

Historical note on §12.40's DONE marking (superseded — kept as history only, no live instruction):
this file used to count §12.40 as DONE only for its dominant clause (no model string outside
`config/`, structurally) while the AST sub-clause (M1) was still open, and flagged it as the one
deliberate exception to this file's "DONE only when the full stated text passes" rule, instructing
readers to count §12.40 as OPEN until M1 landed. **M1 landed round V, 2026-09-01**
(`tests/test_models_yaml_ast.py` — see §12.40's own entry above, which now reads DONE in full and
retires the exclusion marker itself). §12.40 is counted normally as DONE in the Rollup table above;
there is no longer any reason to exclude it when reporting the `<n> of 48` figure.

**Recommended dispatch order, cheapest-and-highest-leverage first (updated 2026-08-30 — §12.27's
COORDINATE leg closed by round L, superseding the original §12.27+§12.37 pairing below;
§12.37's WIRING framing below corrected 2026-09-01 — see the note after this paragraph):**
§12.9, §12.37, §12.18 (originally all confirmed-open pure WIRING, zero new logic — round M's
picks; §12.37 was reclassified NEW-MECHANISM by round K's 2026-08-30 correction, see the Rollup
table's WIRING row above — leave it out of any WIRING batch, it needs its own D-number first) →
§12.7 (one-line SPEC correction, substance already passes) → the TEST-ONLY items (5, 6, 15, 20,
21, 24, 26, 32, 33, 40's AST clause, 42, 44, 48) → §47's two residuals (contracts-registry fork
and backends-statelessness implement-or-adjudicate — see §47's entry, neither is plain TEST-ONLY
any more per controller ruling C1) → SCALE-FIXTURE items → SPEC-ADJUDICATION items (17, 45's
regex half already done) → NEW-MECHANISM items (22's RSS half, 27's DEST_PATH/FILE_PATH legs,
37 — each needs its own D-number first, 31, 34) last, since they're the most expensive and least
likely to be quick wins.

**Correction, 2026-08-31 (round S, controller — this list was never refreshed as items closed
across rounds M-R and had drifted into re-dispatch risk); further corrected 2026-09-01 (round T
close-out, controller ruling C1 — §48 landed DONE this round and §47 was reverted from a
same-round DONE marking back to OPEN, so both needed their "still open" framing fixed here too):**
§5, §6, §7, §12, §15, §16, §18, §21, §26, §32 above are now **DONE** — do not re-dispatch them.
**§48 is now DONE too** (round T, 2026-09-01) — do not re-dispatch it. **§33 is now DONE too**
(round U, 2026-09-01) — do not re-dispatch it. Of the TEST-ONLY group's original membership, the
still-genuinely-open items are: **24** (one residual — the D62-blocked local-profile clause; both
the ledger-sum clause and the run-ceiling/exit-3 clause are closed, round U) and **42** (5 of 9
sub-clauses, per its own entry). **20** and **40's AST clause** closed round V and **44** closed
round W — all three are now DONE, see the Rollup table and their own entries; they are no longer
part of this "still-genuinely-open" list. **§47 is OPEN again**, but no longer as a plain TEST-ONLY item — see its
entry for the two residuals (contracts-registry fork; backends-statelessness implement-or-
adjudicate) a future round must pick up. §10 (closed round R) and §23/§28/§1 (round S) are not
part of the original list above and should be checked against their own `docs/PROGRESS.md`
checkpoints before re-dispatch, not against this stale sentence.
