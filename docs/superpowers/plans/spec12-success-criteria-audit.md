# SPEC §12 Success Criteria — coverage audit against the test suite

Promoted from the 2026-08-27 audit pass. Base commit `076076d`, tree clean at start and finish.
Measurement only: nothing was remediated in the pass that produced this report. The SPEC
corrections to §12.16 / §12.40 / §12.45 / §12.48 and ledger entry `D86` were made in the same
commit as this promotion, and are recorded as edits there rather than in this report's body.

Method: ten parallel lanes decomposed each criterion into atomic sub-clauses; roughly twenty
load-bearing claims were independently re-derived by the orchestrator before being recorded, and
every one held. Two lane-reported figures did NOT reproduce and are corrected here: the §12.45
forbidden-column regex matches **two** legitimate columns (not one), and the §12.40 model-id grep
count is predicate-dependent. Per Guardrail 6 the class results are what reproduce, not raw totals.

# docs/SPEC.md §12 Success Criteria — coverage audit vs tests/
Base `076076d`, tree clean. Measurement only; nothing remediated, no tracked file modified.

## Corrections to the dispatch brief (re-derived, per CLAUDE.md attributed-number rule)
- The >=85%-line-coverage gate is criterion **§12.3** ("Test gates"), NOT §12.2 ("Static gates" =
  ruff / ruff format --check / mypy --strict). Verified against the extracted section text.
- Next free D-number is **D86** (derivation in the ledger). No census total/range quoted per §3.

## Controller-verified facts (I re-derived these myself, not relayed)
- `tests/unit/` holds ONLY `__init__.py` (49 bytes) -> `pytest tests/unit -q` in §12.3 collects 0
  items and passes VACUOUSLY. `tests/contract/` likewise `__init__.py` only.
  `tests/fixtures/llm/` holds only `.gitkeep`.
- No `mypy` invocation anywhere in `tests/` (whitespace-normalised whole-file sweep; the 3 hits are
  a synthetic fixture dependency string and a docstring).
- No `[tool.coverage]`, `fail_under`, or `--cov` in `pyproject.toml`.
- `RunBudgetExhausted`: 8 refs in `orchestrator/budgets.py`, 5 in `tests/test_budgets.py`,
  **0 in `src/fleet/cli.py` and 0 in `tests/test_cli.py`**.
- Exactly ONE `ProcessPoolExecutor(` site in src/+tests/: `orchestrator/budgets.py:947`. It passes
  `max_workers=`/`mp_context=` only. **`initializer=` is passed nowhere in the repo.**
- DDL (`CREATE|ALTER|DROP TABLE/INDEX/VIEW/TRIGGER`) outside `migrations/`: 2 hits, both PROSE in
  docstrings (`state/db.py:45,248`). Property holds by inspection, guarded by nothing.
- Only §12.32 self-declares a deviation (scan of all 48 for UNSATISFIABLE / NOT a passing gate /
  NOT IMPLEMENTED / "not yet" / deferred).

## HEADLINE FINDING (confirmed twice, spans two criteria)
**§12.28 and §12.47 both require "every `ProcessPoolExecutor` is constructed with an initializer
that calls each `discover()`, asserted by inspecting the initializer arguments." No such
initializer exists.** Three docstrings assert the opposite as fact
(`ecosystems/base.py:109`, `:118` inside a *raised message*, `:632`), as do `SPEC.md:5386,6796`.
`grep -in 'initializer|cpu_pool' docs/INTEGRATION_HONESTY.md docs/DECISIONS.md` -> 0 hits.
This is CLAUDE.md Guardrail 7's "SPEC says X but the code cannot do X" shape: two edits, not one.
NOT registered in either ledger. Strongest candidate for D86.

---
# Verdicts

## Criteria 1-6 (lane A01, load-bearing claims re-derived by controller)
| # | paraphrase | verdict | evidence |
|---|---|---|---|
| 1 | `uv sync --frozen` offline on py3.12 | NOT COVERED | every `uv` hit in tests/ is the Python ADAPTER running `uv pip compile` on TARGET repos (test_ecosystems.py:1111,1339; test_build_e2e.py:231,2936,2976,3082,3178,3188,4030,4068,4240). No test shells `uv sync`, checks offline, or asserts interpreter==3.12. |
| 2 | ruff check / ruff format --check / mypy --strict | PARTIAL | ruff check YES: test_lint_gate.py::test_ruff_check_is_clean_across_the_whole_repository (rc==0 over `.`). ruff format --check NO but SELF-DISCLOSED in that file's module docstring ("deliberately NOT gated", 117/142 dirty at 12d3527). mypy NO test at all — but NOT undocumented: ADR-0013 (DECISIONS.md:512) declares it a CI gate, and INTEGRATION_HONESTY.md:88,106,134 record `mypy --strict` clean over 106 files as manual checkpoint measurements. Gate lives outside tests/. |
| 3 | pytest >=85% cov; tests/unit -q <30s offline no-creds | NOT COVERED | No coverage config anywhere. tests/unit/ empty -> vacuous pass. tests/conftest.py (671 lines) has no autouse fixture, no delenv, no models.yaml reference — nothing clears api_key_env-named credentials. Coverage is an ADR-0013-declared CI gate, not a tests/ gate. |
| 4 | model round-trip + golden response per backend incl. PROMPTED | PARTIAL | Round-trip YES: test_state_models.py::test_every_exported_model_round_trips_through_its_own_json:427, parametrized over every exported FleetModel. Golden-per-backend NO, ENTIRELY UNCOVERED: no golden artifact exists; test_llm_roles.py validates in-memory dict literals keyed by Role only, never by backend, no PROMPTED rung. ADR-0013 (DECISIONS.md:507) DECLARES "a stored golden sample" -> declared intent, zero artifacts. |
| 5 | no pickle / ThreadPoolExecutor / subprocess.run in src/fleet/ | PARTIAL | pickle NO test. ThreadPoolExecutor NO test (test_migrations.py:24,684,707 USE it in test code). subprocess.run PARTIAL: test_proc.py::test_no_shell_anywhere_in_the_subprocess_boundary:363 covers only 3 files (proc.py, sandbox/worktree.py, sandbox/container.py), not the src/fleet/ tree the criterion names. |
| 6 | the invariant: language knowledge confined | PARTIAL | All five named tests exist as claimed (test_ecosystems.py:1851,1872,1901,1912,1954). Exemption-list-is-exactly-two asserted by tuple equality (:1901). bazel/ correctly NOT exempted. GAPS: no pattern mentions `ContractKind` (only `Ecosystem`) though the criterion names both; no test scans `match`/`case`. Both vacuous today, but a regression of either shape passes every test. |

## Criteria 19-24 (lane A04)
| # | paraphrase | verdict | evidence |
|---|---|---|---|
| 19 | cycles broken not merely detected | PARTIAL | test_graph_cycles.py::test_the_weakest_edge_yields_first:387 is a 2-NODE cycle (criterion says 3). test_graph_sequence.py::test_atomic_wave_members_all_share_one_wave_index:161 is 3-node (criterion says 12) and never builds a PullRequestDraft -> `scc_id` sharing UNASSERTED. 41-repo MANUAL case: nothing plants 41 repos. "no hang": zero timeout assertions. |
| 20 | secrets never leak | PARTIAL | Redaction primitive well unit-tested. events.payload covered (test_obs.py::test_pat_never_reaches_an_event_payload:135); attempts.stderr_tail covered (test_workers_scan.py:839-896). `phases.last_error` and `llm_cache.response_json` have NO redaction test. PR-body `«redacted:…»` placeholder unverified. No 3-secret-shape fixture run + sweep. |
| 21 | determinism | PARTIAL | test_cli.py::test_status_digest_is_the_run_equivalence_proof:1076 only checks 64 hex chars. No clean-DB re-run + `--llm-cache read-only` + byte-identical digest test. `--llm-cache read-only` hard-fail on miss IS covered (test_llm_cache.py:454; test_run_context_llm_cache.py:279). |
| 22 | memory + disk ceilings | MOSTLY NOT COVERED | (a) RSS/cgroup sampling NOT IMPLEMENTED: `resource_guard` defaults to `lambda: None` (runner.py:358-372), only test fakes. (b) ALREADY KNOWN — D50, `budgets.max_host_rss_mb` listed KNOWN_INERT in test_config_keys_are_read.py:200. (c) exit 9 covered (test_cli.py:3365, explicitly written against "§12.22 criterion 22") but migration_state.json validity post-exit unchecked. (d) no return-type test for state/repository.py accessors. |
| 23 | idempotency | PARTIAL | Re-transform FULLY covered: test_transform_e2e.py::test_re_running_transform_over_a_landed_branch_duplicates_nothing:459-500. scan-twice covers 6 of 8 named tables (test_scan_e2e.py:423). No 9-repo vendored-contract fixture. `edges.retargeted_from_repo_id` correctness tested, stability across re-sequence not. |
| 24 | fail-closed budgets, ledger moves | PARTIAL (7 sub-clauses) | COVERED: wave breach exit 10 + `--raise-wave-budget` clears/audits (test_cli.py:389-439); over-reserve refused not clamped (test_schema_sql.py:196; test_budgets.py:286); in-flight wait-then-proceed (test_budgets.py:258). NOT: priced-run `spent_usd==sum(cost_usd)` — no e2e test queries `budget_ledger` AT ALL. local-profile row assertions = ALREADY KNOWN D62 (record_attempt omits llm_backend/llm_cache_hit/llm_failovers/input_tokens/output_tokens; `llm_backend` is NULL so "non-empty" cannot hold). run-ceiling exit 3: ledger mechanism proven (test_budgets.py:440) but **RunBudgetExhausted has 0 refs in cli.py/test_cli.py** — no CLI-level organic breach test. |

## Criteria 47-48 (lane A10)
| # | paraphrase | verdict | evidence |
|---|---|---|---|
| 47 | registries stateless, total, order-independent | PARTIAL | manifests + ecosystems statelessness enforced AND fires-on-known-bad (manifests/base.py:179 + test_manifests.py:625; ecosystems/base.py:661 + test_ecosystems.py:~170). Rule-engine and backend-name startup refusals covered (test_settings.py:653; test_backend_registry_gate.py:99). NOT: workers registry has no statelessness check (`assert_stateless` has ZERO call sites in src/); backends registry registers INSTANCES (llm/client.py:382-407) with no vars check, no test; contracts registry = ALREADY KNOWN ADR-0065 §4. "abstract on BaseWorker" unasserted — escape: delete @abstractmethod, default `return True`, test stays GREEN while clause is false. 20-shuffled-import-order/manifests-table claim is really 3 hand-written orders comparing an adapter LIST (test_manifests.py:600). **ProcessPoolExecutor initializer: does not exist (see headline).** |
| 48 | startup + version refusals before any cost | PARTIAL | NOT a summary/meta criterion — 5 independent claims. COVERED: migrate-db exception (test_cli.py:544); BEGIN EXCLUSIVE ladder (test_migrations.py:665,696); stale checkpoint invalidated-never-raised (test_checkpoints.py:109) + step re-run (test_runner.py:1173); truncation retry exactly-once + zero failover + zero drift (test_llm_client.py:229-237). NOT: "every command" asserted for exactly ONE verb (`status`, test_cli.py:339) — 12 `_check_schema_version` call sites vs 22 `@app.command` decorations, nothing enumerates the set; message-names-both-versions unasserted; "before a clone or an LLM call" unasserted (inexpressible in the `status` fixture); **the DDL AST test does not exist**; mirror-mutex refusal never asserts exit 2 (test_resume_continue.py:397). |

## Cross-lane routing notes
- §12.28 rests on the same absent ProcessPoolExecutor initializer as §12.47 (flagged by lane A10
  for the controller; §12.28 belongs to the 25-30 batch).
- Documentary inconsistency: ADR-0065 §4 marked §12.32 and §7.6 NOT YET IMPLEMENTED but left
  §12.47 reading as a passing gate over the same unbuilt ContractAdapter registry.

## Criteria 31-36 (lane A06)
| # | paraphrase | verdict | evidence |
|---|---|---|---|
| 31 | wrong contract hoist detected + rolled back | NOT COVERED — mechanism absent | CONTROLLER-VERIFIED: `ContractStatus.FAILED` is DECLARED at models/enums.py:252 ("hoist attempted and rolled back (§3.1 6c-H failure path)") and **assigned nowhere in src/fleet/**. No `git revert` call anywhere (3 `revert` hits are all docstring prose: vcs/commits.py:23,251; vcs/gitea.py:418). `ContractNotShared`/`HoistBrokeOwner` appear only in SPEC prose. graph/cycles.py `_hoist_contracts`:581-651 is a pure in-memory saturating-trial simulation — no bazel build, no rollback branch. Not in any D-number or ADR. |
| 32 | adapter registries total, delegation honest | PARTIAL (1 sub-clause = known ADR-0065) | COVERED: discover()==set(Ecosystem) (test_ecosystems.py:58-68); decoy double-claim RuntimeError (:71-103); generate_targets XOR gazelle_config biconditional (:631-643, cites "§12.32" in its own docstring, parametrized over every Ecosystem). NOT COVERED: decoy Ecosystem member -> import-time raise — branch exists at ecosystems/base.py:648-654, no test ever triggers it. contracts.discover() = ALREADY KNOWN, ADR-0065 (DECISIONS.md:4532-4640); `src/fleet/ecosystems/contracts/` confirmed absent. |
| 33 | layout adapter-derived not hardcoded | PARTIAL | Hardcoded-dir grep COVERED (test_ecosystems.py:1912-1930). Monkeypatch ts->js: PARTIAL — test_ecosystems.py:870-892 checks one synthetic node's layout() and one SCC label, not a full fixture run, no BuildTarget.package check. Config-override path is a genuine e2e (test_build_e2e.py:2820-2853) but nothing compares the two trees for the "identical tree" claim. |
| 34 | new language costs exactly the documented touchpoints | NOT COVERED | `tests/fixtures/adapters/` does not exist. No fixture ecosystem added end-to-end anywhere. `ContractBindingUnavailable` only in source comments, never emitted. `unbound_contract_kinds` only in a static schema fixture (test_state_models.py:408), never exercised. Consistent with ADR-0065 ("contract_bindings is declared and read by nothing"). No D-number. |
| 35 | no raw prior diff reaches a prompt | PARTIAL | Strong worker-level coverage: test_workers_transform.py:848-916, :964-1016 (verbatim current stderr present, prior attempt's marker absent, RejectedApproach.reason present at rung 3, "diff --git" absent). GAPS: prior `FailureClass` token never asserted; diff-absence is a marker/header-string proxy, not the per-non-blank-line->12-char sweep the criterion specifies; no schema-negative test for RejectedApproach/rejected_approaches. CLI-level proof impossible — cli.py:3253-3258 refuses `--context-policy` for ANY value (test_transform_e2e.py:733-750). Downstream of D50, not fresh. |
| 36 | anchoring detected mechanically | ALREADY KNOWN GAP — D50 | INTEGRATION_HONESTY.md D50 header line 2761, status OPEN. cli.py:3262-3266 self-declares in its `--no-anchoring-guard` refusal that "approach_signature fingerprinting has no implementation (rewrite/approach.py is absent) and no rejected_approaches row is ever written". Verified: no rewrite/approach.py, no `def approach_signature`, no `INSERT INTO rejected_approaches`. NB the deviation is disclosed in the CLI error string + D50, NOT in the §12.36 SPEC text. |

## Criteria 41-44 (lane A08 — full detail at lanes/A08/report.md)
| # | paraphrase | verdict | evidence |
|---|---|---|---|
| 41 | local-only profile runs whole pipeline | NOT COVERED (9 of 11 sub-clauses unasserted) | Structural cause: **no test anywhere runs a pipeline phase under any non-`default` profile** — `--profile` appears in tests only in settings/router/`models list` contexts. ALSO SPEC-vs-CONFIG DRIFT (not a test gap): §12.41 says local CHEAP targets "declare supports_json_schema: false and supports_tools: false"; config/models.yaml:105-108 declares NEITHER, deliberately, per its own comment. Guardrail-7 shape: two edits, not one. |
| 42 | new backend = one file + one registry line | PARTIAL (4 of 9) | Four startup refusals covered. CONTROLLER-VERIFIED: `register_backend`'s duplicate-name RuntimeError (llm/client.py:379-380) has **zero assertions anywhere** — all five `register_backend` hits in tests/ are docstring mentions or source-string parsing, none constructs a duplicate. |
| 43 | failover layered, bounded, fail-closed | PARTIAL (8 of 16) | Case (ii) entirely absent = KNOWN D55/D58. `llm_failovers` = KNOWN D62. CONTROLLER-VERIFIED: tests/test_runner.py:1613 destructures `_, attempts, _, _, _ = await harness.phase_row("repo-a")` — **the status column is discarded**, only `attempts == 0` is asserted, so the test never checks "zero repos in REQUIRES_HUMAN_INTERVENTION" and would pass with the repo marked RHI. Also: every test asserting the BackendUnavailable row "names the tier and every target tried" WRITES that string itself; TierUnavailable.__init__ (client.py:152), the sole producer, is asserted by nothing (D78 verified that chain by source reading only). |
| 44 | cache not poisoned across backends | PARTIAL (1 of 6 full, 4 partial, 1 absent) | Cache-key TAMPER detection (recompute cache_key from a stored row's own columns) does not exist, though asserted in both §12.44 and §13 row 39. `test_every_key_component_changes_the_key` is a different assertion — it compares two COMPUTED keys, never a persisted row. No stub OpenAI-compatible server exists; that module asserts the opposite property (`test_no_test_in_this_module_can_reach_a_socket`). |

## Criteria 25-30 (lane A05)
| # | paraphrase | verdict | evidence |
|---|---|---|---|
| 25 | the unknown repo survives | PARTIAL | COVERED: `no-manifest` finding (test_workers_scan.py::test_interrogate_synthesizes_one_manifest_for_an_unknown_ecosystem ~1012; test_manifests.py:635); `dest='misc/<repo_id>'` (test_ecosystems.py:597-624). NOT: `kind='unknown'` — `repos.kind` (cli.py:1568) is the CLASSIFY worker's advisory field (service|library|unknown), a DIFFERENT thing from `Ecosystem.UNKNOWN`; the one `kind=='unknown'` hit (test_scan_e2e.py:347) is a normal TS repo under --skip-classify. `wave_index==0` only structural. **The UNKNOWN filegroup is never built under REAL bazel** — the suite's one real-bazel e2e (test_build_e2e.py:4451) drives only 2 TS + 2 Python repos. No migration_state.json entry (FIXTURE_REPOS:74-141 has no no-manifest repo). |
| 26 | preflight gates rather than crashes | PARTIAL | Empty repo YES, real e2e (test_scan_e2e.py:302). Shallow PARTIAL (unit only, test_workers_scan.py:348-434). **Submodule-bearing, LFS-bearing, and `trunk`-default-branch: NO positive fixture anywhere** — `grep 'submodule "'` and `grep 'filter=lfs'` over all of tests/ return zero. D41 (INTEGRATION_HONESTY.md:2117-2151) covers a DIFFERENT defect (never-ran vs settled-negative) and does not cover the missing-positive-fixture gap. |
| 27 | collisions caught before transformation | **NOT COVERED end-to-end — detectors are UNREACHABLE** | CONTROLLER-VERIFIED: `audit_collisions` has exactly ONE production caller, workers/contracts.py:367, which passes only `contracts=`/`owns_hints=`/`vendor_repo_ids=` — never `dests=`/`files=`/`coordinates=`. `grep -c '"COORDINATE"\|"DEST_PATH"\|"FILE_PATH"' src/fleet/cli.py` -> **0**. graph/sequence.py (which drives `fleet sequence`) references collisions nowhere. So COORDINATE/DEST_PATH/FILE_PATH detection is implemented and unit-tested (test_graph_sequence.py:574,622,728) but structurally unreachable from any CLI command. Exit 6 IS covered e2e (test_cli.py:493-512) but the row is planted by hand via raw SQL, not produced by the audit. Neither ledger documents this. NEW. |
| 28 | single writer | PARTIAL | Second writable connection raises: YES (test_db.py:233-243). **Process-pool-children-have-no-db-handle "asserted via initializer args": NO — `new_cpu_pool` builds ProcessPoolExecutor with no `initializer=` at all** (independent re-derivation of the headline finding, from a lane without A10's context). `new_cpu_pool`/`forkserver` have zero hits in tests/. 200-repo zero-SQLITE_BUSY: NO — largest is 50 coroutines through the single writer (test_db.py:262), serialized by construction. |
| 29 | contracts extracted once, deterministically | PARTIAL (9 sub-clauses) | COVERED: >=2 consumers (:410); one CONTRACT collision severity=warn incl. real CLI e2e (:835); zero-repos-owner on generated-code strength (:457-476); `extraction_confidence == 0.9 x prod(factors)` exact reconstruction (:451-454). PARTIAL: fixture SHARED_FLEET is 3 repos, not the literal "3 vendoring + 2 generated"; owner ladder rungs (iii)(iv)(v) never exercised. NOT: **(h) the `divergent` x0.5 modifier is structurally unreachable from real scan data** — workers/contracts.py's own docstring (L23-35) says no blob SHA is available from real scan data so `content_sha256` is empty and the modifier "can never fire from real scan data". (i) the node-integrity/orphan-edge zero-row query does not exist anywhere. |
| 30 | contract cycle broken by hoisting not bundling | PARTIAL | Real scan->sequence e2e exists but at 2-3 repo scale only (test_sequence_e2e.py:145-186), never the literal 6-repo cycle. CONTRACT_HOIST / hoisted_contract_ids / empty broken_edge_keys / lower contract wave_index: YES at that scale. NOT: acyclicity never asserted for a CONTRACT_HOIST-resolved graph (`ordering_is_acyclic` has zero hits in the contract-cycle test files); no fixture shows the same repo set flipping between CONTRACT_HOIST and pre-ADR-0019 ATOMIC_WAVE under `--no-hoist-contracts` (test_sequence_e2e.py:196-231 falls through to 6d edge-breaking instead, and its --no-hoist-contracts half never checks break_strategy or shared wave_index). |

### Cross-criterion note raised by A05, routed to §12.9 (lane A02)
CONTROLLER-VERIFIED: **no production code path ever WRITES a `"PreflightFailed"` finding.**
In src/fleet/ it appears only as an exemption-set literal (graph/sequence.py:78), a docstring saying
it is "the caller's to enforce upstream" (:468), a read (:484), and schema.sql comments (:262,283).
`workers/clone.py` writes `EmptyRepo` / `OversizeBlob` / `SubmodulePresent` instead (:148, :423).
This bears directly on §12.9's claim that the fixture fleet "plants one of each" of the five
enumerated exemptions — one of the five cannot be planted by the pipeline.

## Criteria 7-12 (lane A02 — full detail at lanes/A02/report.md)
| # | paraphrase | verdict | evidence |
|---|---|---|---|
| 7 | six ManifestAdapters parse fixtures, Coordinate.key matches | PARTIAL | Substance COVERED: all six adapters parse and assert an exact `.key` literal (test_manifests.py:124,174,305,372,458,536). BUT the criterion's named location is false: CONTROLLER-VERIFIED `tests/fixtures/repos/` contains ONLY `.gitkeep` and has **ZERO references** in tests/ or src/. Real fixtures are synthesized inline via `write(tmp_path,...)` (test_manifests.py:44). Location claim factually false as written; property holds. Not in either ledger. |
| 8 | graph correctness | PARTIAL | Confidence reconstruction COVERED and exact (test_graph_build.py:535-557; `==` is stronger than the 1e-9 bound, though the 1e-9 slack itself is unexercised). Cross-repo INTERNAL_IMPORT proven only on a hand-built InferenceInput (test_graph_build.py:581-596), NOT on the fixture fleet — `grep INTERNAL_IMPORT tests/test_scan_e2e.py` is empty. **CONTROLLER-VERIFIED: `hypothesis>=6.115` is declared TWICE in pyproject.toml (lines 48, 63) and ZERO files import it or use `@given`.** The "property-checked with hypothesis over random DAGs" clause is unimplemented and contradicts ADR-0013's own stated decision (DECISIONS.md:501, "hypothesis for the manifest adapters and the graph algorithms"). Wave-order direction IS tested deterministically (test_graph_sequence.py:92-108). |
| 9 | Phase 1 exit condition | PARTIAL | Strongest single assertion in the batch: condensation both-directions (test_graph_sequence.py:142-159, passes condensed / fails raw with "cyclic" in detail). Criteria a/b/c well covered AS PURE FUNCTIONS. **CONTROLLER-VERIFIED cross-cutting: `check_criteria(` has exactly ONE call site in the whole tree — tests/test_graph_sequence.py:513.** The only non-test references to check_criterion_c/_d are inside graph/sequence.py's own body (:611,:620). No CLI/runtime path invokes any of them, so the Phase 1 exit condition exists as a library property, NOT as a runtime gate. Also: the five exemptions are each proven individually, but **each ExemptionCase builds its OWN synthetic WavePlan with ONE exemption** (test_graph_sequence.py:280-306,326-380) — no fixture plants all five simultaneously and asserts closure "there", contrary to the criterion's literal wording. `evidence_path` resolvability NOT covered: check_criterion_d's default is `lambda _repo,_path: True` (sequence.py:621), vacuously true, and tests supply only hand-written True/False lambdas. |
| 10 | Phase 2 exit condition | PARTIAL | All three violation branches of `_transform_criterion` are unexercised: probe-returns-False (cli.py:4423-4425), empty-diff (cli.py:4366-4370, `grep "is EMPTY" tests/` = nothing), outside-dest_path (cli.py:4377-4384, `grep "written or deleted outside" tests/` = nothing). Every fake `parse_probe` in tests returns True or raises. D20 documents a DIFFERENT, more severe gap; D37/ADR-0067 and D52 cover related fixed history, none touching (b)/(c). |
| 11 | Phase 3 exit condition | PARTIAL | Real bazel and networkless container are proven SEPARATELY, never together (real: test_build_e2e.py:4451 with --no-sandbox; sandboxed argv: FakeBazel-only at :2214) — ALREADY KNOWN, INTEGRATION_HONESTY.md addenda §29/§31-33 (~L557-728, L1800-1875, "THE SANDBOXED PATH IS STILL RED" at L595, unretracted). attempts-row exit codes COVERED (:5321-5349). Green-and-empty regression genuinely planted and caught (test_workers_build.py:2603, baseline_test_count=14 + NO_TESTS_FOUND -> RHI). **CONTROLLER-VERIFIED: `tests(//` has ZERO hits in src/ or tests/** — the magnitude comparison and the (repo, baseline, migrated) table do not exist; only the boolean went-to-exactly-zero case is implemented (self-documented cli.py:5317), so baseline 14 -> migrated 5 passes undetected. `baseline_ok IS NULL` exclusion-set-is-empty assertion: absent. NEW. |
| 12 | Phase 4 exit condition | **FULLY COVERED** | rdeps closure incl. disclosed sampling (test_build_e2e.py:2398-2422); PullRequestDraft url resolvable (test_pr_e2e.py:372-373); PR never opened while a dependency PR is unmerged, gh stubbed (test_pr_e2e.py:518-522), with a companion proving the gate reads ingested state rather than being trivially true (:382-441). The only FULLY COVERED criterion found so far. |

## Criteria 37-40 (lane A07 — full detail at lanes/A07/report.md)
| # | paraphrase | verdict | evidence |
|---|---|---|---|
| 37 | stub lifecycle, only way out of DEGRADED | PARTIAL (16 sub-clauses: 4 yes, 4 partial, 8 no) | **The fixture premise is CONTRADICTED: `--stub-blocked` is unimplemented and refused exit 2 (cli.py:5001), and that refusal is itself a passing test.** `orchestrator/stubs.py` has NO importer in src/ — the whole §3.5.1 state machine is unwired; tests/test_stubs.py is a pure-function suite writing no row, enqueuing no task, emitting no finding. Already recorded as a D69 amendment. |
| 38 | no ready-for-review while a stub is unresolved | PARTIAL (20 sub-clauses: 5 yes, 4 partial, 11 no) | The headline refusal IS well covered — exit 2 + PrState unchanged + a recording forge asserting `gh pr ready` never ran, two independent tests. Everything downstream (resolution, sweep, exit 7) unasserted. `stub_reconcile` has no implementation (cli.py:10771 is a comment) = KNOWN D80, which already names §12.38's missing behavioural check nearly verbatim. Sharpest: the `--ready` guard's SUPERSEDED arm is unexercised — **deleting `'SUPERSEDED'` from cli.py:9902 passes the whole suite**; and the gate's POSITIVE half is asserted nowhere, so an unconditionally-refusing guard would also pass. |
| 39 | bounded, priced rework; stub rot reaches a human | PARTIAL (18 sub-clauses: 4 yes, 6 partial, 8 no) | Case (iii) (batched=1 / eager=3, key hashes all three providers) is the batch's strongest assertion. Cases (i)/(ii) only test what is handed in as a parameter. |
| 40 | no model string outside config | PARTIAL (1 of 5) | **CONTROLLER-VERIFIED: the criterion's own greps do NOT return nothing at 076076d.** `src/fleet/llm/backends/openai_compatible.py` carries llama.cpp prose (non-zero under any variant of the pattern; A07 counted 7 under its predicate, I counted 10 lines under mine — the CLASS result is what holds: non-empty), and `llm/backends/vertex.py:262` is a hardcoded `f"https://{host}/v1/projects/{project}/locations/{region}"`. The AST clause names `src/fleet/llm/routing.py`, which **does not exist** (the reader is settings.py:1138; PROGRESS.md:777 records "none exists, none should be created"). Implementing §12.40 verbatim would RED the suite — a SPEC-text edit is owed alongside any coverage work. **[Marker 2026-08-28, round-K lane W5 — beside the claim, because the correction at `:453` is ~319 lines below this row and a reader of this row never reaches it. This row's original wording is left untouched.]** The attribution *"PROGRESS.md:777 records 'none exists, none should be created'"* is **wrong, and inverted**: `docs/PROGRESS.md:5542` carries those words; `docs/PROGRESS.md:777` resolves the same clause the **other** way — *"`llm/roles.py` holds the router that §8 and §12.40's AST test expect at `llm/routing.py` — a rename plus import update"*. Re-measured at `506cadb`. So the two contradicting records were **conflated**, not merely one overlooked. The rest of the row stands; see the fuller correction at `:453`. |

## Criteria 45-46 (lane A09)
| # | paraphrase | verdict | evidence |
|---|---|---|---|
| 45 | no code state persisted outside Git | **NOT COVERED** (5 of 7 sub-clauses unasserted, 2 partial) | The criterion calls its three checks "all mechanical"; at this tree all three are PROSE (schema.sql:483 "There is deliberately NO `mutations` table"; :867). No `PRAGMA table_list`/`table_info` sweep exists. **CONTROLLER-VERIFIED: the forbidden-column regex is unsatisfiable as written — `collisions.blob_shas` (schema.sql:350) matches its own `blob` alternative.** The 40-hex enumeration also omits `repos.head_sha` (:65) and `tasks.pre_commit_sha` (:651). No hunk-header (`'%@@ -%+%@@%'`) scan of the DB. No delete-artifacts/-then-resume test, and `artifacts/diffs/` has zero src/fleet/ references. `Fleet-Repo-Id`/`Fleet-Phase`/`Fleet-Attempt` are read by NO test (3 of 6 trailers read, test_vcs.py:337-361); `git interpret-trailers` appears nowhere. Note DECISIONS.md:1198 ASSERTS "§12.45 asserts a run resumes correctly" — unbacked at this tree. |
| 46 | model-layer invariants | PARTIAL (13 sub-clauses: 8 yes, 4 partial, 1 effectively no) | COVERED and strong: illegal RepoStatus transition raises (:517-537); abandoned re-openable only via OPERATOR_REOPEN (:550-567); stale lease_fence rejected (test_repository.py:186-222 + SQL rowcount==0); late WorkerResult under a reclaimed lease discarded via reap-in-flight (test_runner.py:821-847); 32 KiB truncation stored (LOG_TAIL_BYTES=32_768, five fields, into the real column at test_build_e2e.py:2307-2325); partial-checkpoint re-entry adds zero duplicate commits (test_workers_transform.py:423-466, first-40 SHAs byte-identical). GAPS: round-trip asserted by OBJECT EQUALITY (:441) — the exact form the criterion says NOT to use ("via model_fields, not object comparison"); population is `__all__` (34) not `src/fleet/models/` (36) — `Resolution` (models/build.py:189) is covered by NOTHING though built by all three ecosystem adapters. **The reaper leg of "every automatic sweep": the reaper is never driven with an RHI row and writes RAW SQL (repository.py:1748-1754) with no transition() call, so ALLOWED_TRANSITIONS tests do not bind it** — the D77 bypass shape, one function over. `stub_reconcile` = KNOWN D80. `edges.edge_key` byte-stability across a rebuild: key-by-key exists at the INFERENCE layer only, never on persisted values. `acquire_phase_lease` never driven concurrently — the criterion's `(repo, phase)` claim is tested on `tasks` instead (test_repository.py:144-170); runner.py:517-521's `admitted=False` branch has no test. |

### Protocol note
Lane A02 spawned its own sub-forks, which the SDD contract forbids (implementers never dispatch
subagents). It was net-positive here — one fork returned §12.10 as FULLY COVERED and the lane
refuted it by reading the file (lines 621-650 are a fixture module STRING, not a test) — but the
duplicate seats are a cost, and the fork's wrong verdict is exactly what an unchecked extra seat
produces. Its final verdict (PARTIAL) is the one recorded above.

---
# §12.3 coverage gate — MEASURED (this is the first measurement; nothing measured it before)

**Exact command** (no `-k`, no path arguments, no deselection, nothing excluded):
```
.venv/bin/python -m pytest --cov=src/fleet --cov-report=term-missing \
    --cov-report=json:<scratch>/coverage.json
```
`testpaths = ["tests"]` and `addopts = "-ra --strict-markers"` come from `pyproject.toml`; that is
the entire scope. Tooling: `.venv/bin/python -m pip install pytest-cov` (pytest_cov 7.1.0,
coverage 7.15.4) — `uv` is not on PATH in this environment. Installed into `.venv` ONLY;
`pyproject.toml` and `uv.lock` were NOT modified and `git status --short` is empty.

## Result: **93%** — the >=85% gate PASSES
```
TOTAL   17506 stmts   1217 miss   93%
1942 passed in 1335.78s (0:22:15)      PYTEST_EXIT=0
```
Also green by CLAUDE.md §6's own definition:
- `xfail: 0` — no `xfailed`/`xpassed` in the summary line
- clean `bazel disk` line: `peak 3.72 GiB (ceiling 6 GiB) · residual output bases 0 bytes ·
  repository cache kept 1127 MiB`
- `0 tests skipped this session — full collected coverage ran`

## Was 85% ever actually measured? No.
Neither `pytest-cov` nor `coverage` was installed in `.venv` or declared in `pyproject.toml`, and
there is no `[tool.coverage]` section, no `fail_under`, and no `--cov` in `addopts`. The number was
aspirational from the start — declared as a CI gate by ADR-0013 (`docs/DECISIONS.md:512`) and never
mechanically checked in-tree. It happens to be comfortably met.

## Modules materially below 85% (9 of 115 measured)
| module | stmts | miss | cover |
|---|---|---|---|
| `src/fleet/__main__.py` | 6 | 6 | **0%** |
| `src/fleet/rewrite/libcst_py.py` | 38 | 10 | 74% |
| `src/fleet/migrations/v004_stub_lifecycle.py` | 8 | 2 | 75% |
| `src/fleet/migrations/v005_backend_identity.py` | 8 | 2 | 75% |
| `src/fleet/migrations/v006_mutations_deleted.py` | 8 | 2 | 75% |
| `src/fleet/orchestrator/registry.py` | 38 | 9 | 76% |
| `src/fleet/migrations/v003_anti_anchoring.py` | 10 | 2 | 80% |
| `src/fleet/rewrite/tsmorph.py` | 37 | 6 | 84% |
| `src/fleet/workers/buildgen.py` | 215 | 34 | 84% |

Nothing here is alarming in isolation — the largest shortfall is 34 missed statements in
`workers/buildgen.py`. **But read this beside the audit above, not on its own:** line coverage of
93% coexists with detectors that no CLI path can reach (§12.27), a state machine with no importer
(§12.37), and criteria whose own commands do not pass (§12.40, §12.45). Line coverage measures
whether a line ran, not whether anything asserted the line's contract — which is precisely the
distinction §12's preamble ("a command whose exit code is the verdict") is trying to draw.
Note `v006_mutations_deleted.py`'s 75%: §12.45's `DROP TABLE IF EXISTS mutations` is among the
lines never exercised against a table that exists.

## Criteria 13-18 (lane A03)
| # | paraphrase | verdict | evidence |
|---|---|---|---|
| 13 | retry semantics: ladder, ceiling, atomicity, transient budget | PARTIAL | COVERED: ladder-length/max_attempts mismatch IS a ValidationError at construction (test_state_models.py:1120-1142, plus a [1,8] sanity rail); transient failure increments `transient_retries` leaving `attempts` unchanged (test_retry.py). Tier escalation to `["DETERMINISTIC","LLM_REPAIR","LLM_ESCALATION"]` + `("REQUIRES_HUMAN_INTERVENTION", 3)` proven at PhaseRunner+RetryPolicy level against a real SqliteStateRepository. NOT: the 5-rung variant — `max_attempts=5` occurs ZERO times in tests/, and D50 documents `transform.ladder.*` as KNOWN_INERT (`LadderState(...)` at runner.py:428 takes no `ladder=` kwarg), so a configured ladder never reaches a rung. The "same statement" atomicity claim is asserted by END STATE only (test_repository.py:259-301) — **nothing is ever interrupted between the increment and the terminal write**, so it cannot distinguish one atomic UPDATE from two that both happened to finish. |
| 14 | blast containment + escape hatch | PARTIAL | (a) FULLY COVERED — `blocked_by` is exactly the union over transitive dependents, unrelated repo stays PENDING (test_scheduler.py:232-266), plus e2e containment. (b) FULLY COVERED through a real `fleet resume` (test_resume_unblocking.py:323-410), with retained cases on the same fixture proving it clears only what it can show. (c)/(d) NOT COVERED — **the feature is actively refused**: `fleet build --stub-blocked` exits USAGE, and the test proving that refusal says why in its own docstring ("no worker writes a `stubs` row"). KNOWN D50. |
| 15 | crash safety, Git is the arbiter | PARTIAL | Step-4 git arbitration is the strongest part of the batch: discard-onto-`tasks.pre_commit_sha`-not-`phases.base_ref` has a genuinely DISCRIMINATING fixture (test_cli.py:3079-3135 commits an earlier DONE task's work between the two anchors, so the defect is expressible), attempts unchanged 2->2; adopt-the-landed-commit-without-charging (test_cli.py:3038-3076). NOT: the fabricated reverse disagreement (hand-edited `attempts.commit_sha` pointing off-branch) is constructed nowhere; the nearby test covers an ABSENT row, a different divergence. |
| 16 | checkpoint integrity | **NOT COVERED — the suite CONTRADICTS the SPEC** | CONTROLLER-VERIFIED. §12.16 says a byte-corrupted payload "raises `ValidationError` on load". `tests/test_checkpoints.py:188` is named **`test_a_truncated_blob_invalidates_instead_of_raising`** and asserts `payload is None`, `rejection is MALFORMED_ENVELOPE` — no exception. `:218` does the same for INVALID_PAYLOAD. The module docstring says this is deliberate ("must never raise and never half-populate a model"), corroborated at DECISIONS.md:5947. The "never partially populated" half IS fully covered. SPEC is also internally inconsistent: §12.48 contrasts version-skew ("never raised... §12.16 covers corruption") against §12.16, implying corruption DOES raise — the code treats both identically. Undocumented as a SPEC/code mismatch. |
| 17 | projection fidelity | PARTIAL — "byte-identical" is FALSE BY CONSTRUCTION | CONTROLLER-VERIFIED precisely: `MigrationState` (models/state.py:223) declares `updated_at: datetime = Field(default_factory=utcnow)` at :236, and `build_state` (projection.py:185) validates a dict containing run_id/started_at/monorepo_branch/config_sha256/harness_version/repos/waves/contracts/cycles/collisions — **and no `updated_at`**. (The `updated_at` at projection.py:277 belongs to the per-repo entry, a DIFFERENT model.) So every rebuild mints a fresh timestamp. No test asserts byte-equality across two regenerations — test_projection.py calls `project_once` twice and only checks each output PARSES. Valid-JSON and interrupted-mid-write (monkeypatched `Path.replace`, old file survives, no stray temp) are covered. **NB: CLAUDE.md's own Guardrail 6 "Round F" already records this exact fact about this exact field — but as a detector's wrong quantity, never connected to §12.17.** |
| 18 | observability | PARTIAL — 2 of 3 artifacts do not exist | CONTROLLER-VERIFIED: `llm_call` -> **0 hits in src/fleet/**; `latency_ms` -> **0 hits**; `errors-` -> **0 hits**. §12.18's `llm_call` event and `logs/errors-<run_id>.jsonl` are both ADR-0012 decisions (DECISIONS.md:462,464) and SPEC text (SPEC.md:6819,6893,7389) that were never built; `obs/` implements only a single combined `logs/events-<run_id>.jsonl`. The named fields exist as columns on `llm_cache` (a cache-KEY record), not as an emitted per-call event. COVERED: `backend_failover` names both targets and the trigger (test_llm_findings.py:297-329) with the negative case pinned. |

---
# Rollup

## Verdict tally (predicate: the criterion's verdict as recorded above; 48 rows, all accounted for)
| verdict | count | criteria |
|---|---|---|
| FULLY COVERED | 1 | 12 |
| PARTIALLY COVERED | 37 | 2,4,5,6,7,8,9,10,11,13,14,15,17,18,19,20,21,23,24,25,26,28,29,30,32,33,35,37,38,39,40,42,43,44,46,47,48 |
| NOT COVERED (incl. contradicted / unimplemented / unreachable) | 9 | 1,3,16,22,27,31,34,41,45 |
| ALREADY KNOWN GAP as the whole-criterion verdict | 1 | 36 (D50) |

Read that top row carefully: **exactly one of 48 criteria is fully covered.** The modal verdict is
PARTIAL, and "partial" here usually means the pure-function core is well tested while the
end-to-end claim the criterion actually makes is not.

## The five classes that explain most of the misses
1. **The criterion is unsatisfiable as written** (SPEC is wrong, not the tests): §12.40 (its own
   greps return hits; names a nonexistent `llm/routing.py`), §12.45 (its forbidden-column regex
   matches the real `collisions.blob_shas`), §12.16 (says "raises", code deliberately never
   raises, and §12.48 cross-references it as if it did), §12.17 ("byte-identical" is false by
   construction), §12.7 (names an empty, unreferenced fixture directory), §12.32 (already
   adjudicated, ADR-0065). **These need a SPEC edit, not a test.**
2. **The mechanism does not exist**: §12.31 (`ContractStatus.FAILED` never assigned, no git revert),
   §12.34 (no adapter fixture at all), §12.18 (`llm_call`, `errors-<run_id>.jsonl`), §12.37
   (`orchestrator/stubs.py` has no importer), §12.22(a) (`resource_guard` defaults to `lambda: None`),
   the `ProcessPoolExecutor` initializer (§12.28 + §12.47).
3. **The mechanism exists but nothing reaches it**: §12.27 (`audit_collisions`'s DEST_PATH /
   FILE_PATH / COORDINATE detectors have one production caller that passes none of them),
   §12.9 (`check_criteria` called only from a unit test — the Phase 1 exit condition is not a
   runtime gate), §12.13.4 (D50's inert ladder config).
4. **Unit-level coverage standing in for the criterion's stated scale or scope**: §12.19 (2-4 node
   graphs vs the stated 3/12/41), §12.29-30 (3 repos vs "3 vendoring + 2 generated" / 6-repo cycle),
   §12.11 (real bazel and networkless container proven separately, never together — known),
   §12.24(c), §12.46(vi).
5. **The assertion is weaker than its own name** — the Rule 12 shape: `test_runner.py:1613`
   discards the status column; §12.47's "abstract on BaseWorker" survives deleting
   `@abstractmethod`; §12.38's `--ready` guard passes with `'SUPERSEDED'` deleted from cli.py:9902
   and its positive half asserted nowhere; §12.13.6's atomicity checked only by end state.

## D-number allocation
Next free is **D86**, derived two ways (see ledger). Per CLAUDE.md §3 no census total or range is
quoted, and I did NOT write to `docs/INTEGRATION_HONESTY.md` — that is out of scope for a
measurement pass, per the dispatch's own boundary.

**Recommended D86** (the single strongest, and the only one that is one defect spanning two
criteria plus three code docstrings plus two SPEC sites):
> Every `ProcessPoolExecutor` is required by §12.28 and §12.47 to be constructed with an
> initializer calling each registry's `discover()`, "asserted by inspecting the initializer
> arguments". The repo's only `ProcessPoolExecutor(` site — `orchestrator/budgets.py:947` — passes
> no `initializer=`, and `initializer=` appears nowhere in `src/` or `tests/`. Three docstrings
> assert the opposite as settled fact (`ecosystems/base.py:109`, `:118` inside a RAISED message,
> `:632`), as do `SPEC.md:5386` and `:6796`. Neither ledger mentions it.
Guardrail 7 shape: fixing this is two edits (code + the SPEC/docstring sentences), and a
reconciler who trusts the docstrings would "fix" the code to match a claim nothing enforces.

> **[Marker 2026-08-28, round-K lane W5 — read this before the paragraph below it. That paragraph
> is left exactly as its author wrote it at `12be741`; this is the annotation, not a rewrite.]**
>
> **The numbers in the next paragraph are PROPOSALS. The ledger has issued none of them.** Of the
> numbers this section names, only `D86` — recommended above — was ever allocated, and it is a real
> entry in `docs/INTEGRATION_HONESTY.md`. Class result, measured at `506cadb`: for **every** number
> proposed in the paragraph below, a form-agnostic word-boundary sweep over `docs/` returns
> **exactly one hit, and that hit is the paragraph below itself**; a `git grep` of the same pattern
> over every tracked file in the worktree returns that one file and nothing else. So an allocator
> who sweeps and stops at the match reads every one of them as taken, and taken for these subjects.
> **This marker deliberately does not repeat those numbers**, so that the sweep it describes still
> lands on that paragraph and on nothing else — quoting them here would make this marker a member
> of the class it measures and falsify its own count.
>
> This is exactly the trap `CLAUDE.md` §3 records: **a match is not an allocation — read the body.**
> The body is this marker, and it says these numbers are free. Do not treat the paragraph below as
> an allocation, and do **not** renumber it. **Re-derive the next free number from
> `docs/INTEGRATION_HONESTY.md` at the moment you allocate**, form-agnostically, as one past the
> *measured* maximum — the highest number carrying an entry there at `506cadb` is `D86`. Anything
> actually allocated off this list is recorded in the ledger; this file never records an allocation.
>
> **One candidate below is also stale.** The last one — *"no `hypothesis` test exists despite
> ADR-0013 deciding on it"* — was true at `12be741` and is **false at `506cadb`**:
> `tests/test_graph_properties.py` landed at `83e1493` (2026-08-28) as a `hypothesis` property test
> for ADR-0013's wave-ordering claim, and it is the only file under `tests/` or `src/` importing
> `hypothesis`. Its words stand below as the record of what was proposed; they are no longer a live
> finding.

Further candidates, if more numbers are allocated, in my order of value:
D87 §12.27 detectors unreachable from any CLI path · D88 §12.16 SPEC-vs-code "raises"
contradiction (+ §12.48's cross-reference) · D89 §12.9 `check_criteria` is not a runtime gate ·
D90 §12.17 "byte-identical" false by construction · D91 §12.40 + §12.45 criteria whose own
commands do not pass · D92 no `hypothesis` test exists despite ADR-0013 deciding on it.

## What this audit did NOT do
Remediation (explicitly out of scope). No tracked file was modified — `git status --short` is empty.
No D-number was written into the ledger. Lanes did not run pytest (one controller session only), so
per-criterion liveness comes from the single full-suite run, which was green: every test cited above
is in a suite that passed 1942/1942.

## Disclosed limits of this audit
- Verdicts are per-lane judgements over sub-clause decomposition; I independently re-derived the
  load-bearing claims (every one I checked held, ~20 checks) but did NOT re-derive all ~90
  sub-clause findings.
- "NOT COVERED" means no test asserts the clause, NOT that the behaviour is broken. Several
  unasserted properties are true in the tree today by inspection (e.g. §12.48's DDL containment).
- Lane A02 spawned sub-forks against the SDD contract; one fork's FULLY COVERED verdict for §12.10
  was wrong and the lane refuted it. Recorded verdict is the lane's.
- Raw sub-clause counts are predicate-dependent and are NOT quoted as totals (Guardrail 6); the
  per-criterion CLASS verdicts are what reproduce.

---
# Residue this pass deliberately did NOT fix (class swept, sites disclosed)

Correcting §12.40's `llm/routing.py` reference regenerates its class unless the class is swept, so
it was. A whitespace-normalised sweep over `docs/` and `src/` (excluding `references/`, `tools/`
and the git-ignored `.superpowers/`) finds `routing.py` still named at:

* **`docs/SPEC.md` §8's module listing** (the architecture file tree) — asserts `routing.py` as a
  module that exists.
* **`docs/SPEC.md` §13 row 36.**
* **`src/fleet/llm/client.py` ×3** — design prose declaring the Protocol "so `routing.py` plugs in
  without this file changing", i.e. forward-looking rather than a claim it exists today.
* `docs/PROGRESS.md` ×2 — these are the *record that it does not exist* ("none exists, none should
  be created"). **Correct as written; not to be edited.**
* This report ×2, and §12.40's own correction marker — a correction that quotes the citation it
  retired. A naive count-based sweep reads these as survivals; they are not.

**Why they were left standing.** Whether `routing.py` gets built or the §8 listing gets retired is
an *adjudication*, not a correction — the two answers cost different things, and `docs/PROGRESS.md`
already records the finding. A measurement pass that quietly rewrote §8's architecture listing would
be deciding that question by side effect. Flagged here so the next author decides it on purpose.

**Also left standing, deliberately:** the §12.28 and §12.47 sentences asserting the
`ProcessPoolExecutor` initializer, and the three `ecosystems/base.py` docstrings that agree with
them. That is the second half of `D86`'s two-edit pair and belongs with its adjudication — retiring
the claim and building the mechanism are different remedies, and this pass did not choose between
them.

---

# 2026-08-28 — round-K repair of `12be741` (lane W4). Annotation only; nothing above is rewritten.

An independent review found three Critical and five Important defects in `12be741`, the commit that
promoted this report and filed `D86`. The controller re-verified every Critical and all reproduce.
Each correction below was **re-measured in this lane's own worktree at `12be741`** before it was
written; none is inherited. The retired wording is quoted inside each correction on purpose, so a
count-based sweep for a withdrawn claim finds this section rather than a survival — which also means
**no raw occurrence total quoted above or below is safe to re-derive from this file**, because this
section is itself a member of every class it names.

## The residue swept a string, not the class — §12.17 and §12.7

"The five classes that explain most of the misses", class 1, names **six** unsatisfiable criteria:
§12.40, §12.45, §12.16, §12.17, §12.7 and §12.32. `12be741` corrected three (§12.16, §12.40,
§12.45), §12.32 was pre-adjudicated by ADR-0065 and self-declares in the SPEC — and **§12.17 and
§12.7 got neither a correction nor a disclosure**, in either the SPEC or this report's residue
section, whose stated boundary therefore did not account for every member of its own class. Both
now carry in-place dated markers in `docs/SPEC.md`:

* **§12.17** — "byte-identical" is false by construction, re-derived here rather than inherited:
  `MigrationState.updated_at` is `Field(default_factory=utcnow)` (`src/fleet/models/state.py:236`)
  and `state/projection.py::build_state` (`:213-238`) does not supply that key, so two projections
  of an untouched database differ in it. The JSON-validity half is unaffected.
* **§12.7** — the named location is false: `tests/fixtures/repos/` holds exactly one file,
  `.gitkeep`, and a sweep of `src/` and `tests/` for that path returns **zero** references. The
  fixtures are synthesized inline into `tmp_path` by `tests/test_manifests.py`'s `write()` helper
  (`:47-50` — this report's row 7 cites `:44`, which is the `SRC` constant, not the helper). The
  property the criterion asserts still holds; only its location claim does not.

Both markers state that choosing the replacement wording is an **adjudication**, and neither makes
it.

## §12.40's exclusion was applied, and validated as a gate before it was called one

`12be741`'s marker asserted that "both greps take the same `| grep -v '^src/fleet/llm/backends/'`
exclusion". They did not — at `12be741` that exclusion appears on the §12.40 line only inside the
SDK-import command and inside the sentence describing the edit, so the two greps it describes still
returned **7** and **1** and the criterion remained unsatisfiable, while the same marker forbade the
repairs a reader would otherwise reach for. This lane made the edit rather than restating the gap,
because the alternative leaves a criterion asserting "returns nothing" of a command that returns
seven, with no legal repair available.

Validated three ways before being called a gate, all re-run in this lane's worktree:

| state | model-id grep | `/v1` endpoint grep |
| --- | --- | --- |
| clean tree, exclusion applied | 0 | 0 |
| clean tree, exclusion removed | 7 | 1 |
| synthetic fault injected into `src/fleet/llm/client.py` (outside `backends/`), exclusion applied | 1 | 1 |

The fault was reverted and `git status src/` is clean; no file under `src/` is modified by this
repair. The quantity these greps watch is *a model id or endpoint literal appearing in Python
outside `llm/backends/`*, and the disclosed blind spot is exact: a hard-coded model id **inside** a
backend file is invisible to them. That is why the structural SDK-import form stays beside them, and
it is stated in the marker rather than left to be discovered.

`12be741`'s other §12.40 number was inverted: "The structural form … returns **0** unscoped". The
SDK-import form **without** the exclusion returns **5** at `12be741` — `llm/backends/anthropic.py:24`,
`bedrock.py:46`, `openai_compatible.py:44`, `vertex.py:58`, `:59` — and **0** only with it. The class
result, which is what reproduces: *no vendor SDK is imported outside `src/fleet/llm/backends/`*, true
at `12be741` and true now.

## The §12.16 marker carried a fabricated verbatim quote

It attributed *"must never raise and never half-populate a model"* to `state/checkpoints.py`'s
docstring. **The phrase is not in that file.** A whitespace-normalised sweep of `src/` and `tests/`
finds it exactly **once** — in the module docstring of `tests/test_checkpoints.py`, wrapped across
`:4-5`, which is why a line-oriented `grep` reports zero and why the review that found this recorded
zero occurrences. So the review's substance was right (the quote is misattributed and unenforced)
and its measurement was not: one occurrence, in the test module rather than the module under test.
The quote is replaced in `docs/SPEC.md` §12.16 by a citation to
`src/fleet/state/checkpoints.py:8-15`, per CLAUDE.md §3's rule against quoting another module's
comment verbatim.

`docs/DECISIONS.md` ADR-0002 (`:49-51`) is the **origin** of the retired "must produce a
`ValidationError`" sentence and was the last site still asserting it after §12.16 and §12.48 were
corrected. It now carries a dated marker: annotation only, the decision it records is unchanged and
still correct, and the reconciler is told the SPEC is the corrected side.

## The `routing.py` sweep was incomplete within its own stated scope

The sweep above declares its scope as "`docs/` and `src/`, excluding `references/`, `tools/` and
the git-ignored `.superpowers/`". Re-run at `12be741` under that exact scope, whitespace-normalised
over the whole file, it finds one file the list omits:

* **`docs/superpowers/plans/ledger-sdd-backlog-b.md:47`** — "SPEC §8 names llm/routing.py,
  negotiate.py, capabilities.py — none exists, none should be created." Correct as written and not
  to be edited; missing from the residue list, which is the defect.

Two further measurements the residue did not make:

* **`docs/SPEC.md` §8's `llm/` listing names FOUR modules that do not exist**, not one:
  `routing.py`, `negotiate.py`, `failover.py` and `capabilities.py` (`:6036-6042` at `12be741`).
  Exactly **one** module that exists is missing from the listing: `roles.py`. `__init__.py` is
  omitted throughout the listing by convention — `rewrite/` has one and it is not listed either —
  so it is not counted as an omission. The listing now carries an in-place marker.
* **`docs/PROGRESS.md` records the absence in THREE places, and two of them disagree.** `:5542`
  ("none exists, none should be created") names routing/negotiate/capabilities; `:780` names
  `failover.py`; `:777` resolves the SAME clause the OTHER way — "`llm/roles.py` holds the router
  that §8 and §12.40's AST test expect at `llm/routing.py` — a rename plus import update".
  `12be741` cited `:5542` and not `:777`, resolving a contradicting record silently, which
  CLAUDE.md Rule 7 forbids. §12.40's marker now surfaces the conflict and says why `settings.py`
  wins: the clause's predicate is *reads `config/models.yaml`*, and `settings.py` opens the file
  (`:1138`, `:1149`) while `roles.py` wraps an already-parsed tree (`:112`, `:161`). Class result
  re-derived here: of the modules under `src/fleet/` that name `models.yaml`, exactly **one** opens
  it.
* **This report's row 40 (`:134`) attributes `:5542`'s words to `:777`.** It reads, verbatim: *(the reader is
  settings.py:1138; PROGRESS.md:777 records "none exists, none should be created")*. It does not — `:5542` does; `:777` says the
  opposite. So the two contradicting records were **conflated**, not merely one overlooked, which is
  how the conflict survived into `12be741`'s commit message. The row is left as its author wrote it;
  this is the dated marker beside it.
* **This report's own self-count was wrong at its own commit.** The bullet above reads "This report
  ×2"; a whole-file normalised count of `routing.py` in this file at `12be741` is **7**. It is
  replaced by a class statement rather than a corrected number, because a self-referential count in
  a document that quotes what it retires cannot be kept true: **every mention of `routing.py` in
  this file is either a finding about the SPEC or a quotation of one, and none is a claim that the
  module exists.**

## §12.28 was never the §12.47 claim — `D86` conflated two criteria

`D86`'s heading said §12.28 and §12.47 **both** require the `discover()` initializer. §12.28
requires only that pool children carry **no database handle**, "asserted by inspecting the
initializer arguments" — the same evidence phrase, a weaker requirement, and one that is **true
today**: `orchestrator/budgets.py:947-949` passes `max_workers=` and `mp_context=` only. Retiring
both together would have discarded a true requirement. §12.47's sentence is retired; §12.28's is
kept and marked. `D86`'s heading and status are corrected in `docs/INTEGRATION_HONESTY.md`, with
the retired wording quoted there.

## Two minors, recorded rather than fixed

* **M1 — §12.40's AST clause is intent, not coverage.** It asserts "an AST test asserts that the
  only module in `src/fleet/` reading `config/models.yaml` is `settings.py`". A whitespace-
  normalised sweep of `tests/` for any such assertion returns **zero** at `12be741`; the two
  §12.40-and-`models.yaml` hits in `tests/` (`test_llm_backend_vertex.py:755`,
  `test_llm_backend_bedrock.py:783`) assert the model-id half. Recorded in §12.40's marker; writing
  the test is out of this repair's scope.
* **M2 — §12.45's "five" is predicate-dependent, and the landed marker already says so.** Measured
  here two ways: under *column holding a single commit SHA* (the predicate §12.45's own
  `git cat-file -e <sha>^{commit}` assertion fixes) the count is **5** — `repos.head_sha`,
  `phases.pre_commit_sha`, `phases.post_commit_sha`, `tasks.pre_commit_sha`,
  `attempts.commit_sha`. Under a *structural* predicate — a `length(...) = 40` CHECK in
  `state/schema.sql` — it is **1**, `attempts.commit_sha` alone. The marker in `docs/SPEC.md`
  already carries that distinction ("only `attempts.commit_sha` carries a `length(...) = 40`
  CHECK; the other four are conventionally 40-hex but unconstrained"), so no SPEC edit was made.
  `collisions.blob_shas` is excluded under both predicates: it holds a JSON list of blob SHAs, not
  a commit.

## What this repair deliberately did NOT do

* It did not touch `src/`. The three `ecosystems/base.py` docstrings (`:109`, `:118` inside a
  **raised** message, `:632`) still assert the initializer as settled fact. That is the reason
  `D86` is `PARTLY ADDRESSED` and not fixed.
* It did not adjudicate whether `routing.py`, `negotiate.py`, `failover.py` and `capabilities.py`
  get built or the §8 listing gets retired, and it did not repoint the listing. It made the
  disagreement between `docs/PROGRESS.md:777` and `:5542` visible at the sites a reconciler reads,
  which is what `12be741` failed to do.
* It did not choose replacement wording for §12.7 or §12.17.
* It could not verify `docs/DECISIONS.md`'s reference-harness passage: the file it cites,
  `references/visa-vulnerability-agentic-harness/vvaharness/orchestrator/checkpoints.py`, is not in
  `references/` and appears nowhere in this repository's history. It is named as a deliberate
  non-edit beside ADR-0002's marker so the next sweep does not "fix" it.

---
# 2026-08-28 — round-K final-review repairs (lane W5). Annotation only; nothing above is rewritten.

Three documentary corrections landed in this pass — the `D86` "Landed" clause in
`docs/INTEGRATION_HONESTY.md`, the pre-allocation marker above the candidate list, and the marker
beside row 40. Each is a dated marker beside the claim it corrects; no earlier author's wording was
edited. One further item is **recorded here rather than fixed**, because `src/` is outside this
lane's ownership.

## Residue — recorded, not fixed

* **`_owns_hints` is now defined twice, with different signatures.** Measured at `506cadb` with an
  `ast` walk of each file, not by grep:
  * `src/fleet/cli.py:2488-2518` — `_owns_hints(settings: FleetSettings, claims: Sequence[CoordinateClaim]) -> dict[str, str]`
  * `src/fleet/workers/contracts.py:889-896` — `_owns_hints(payload: ContractsInput, known: set[str]) -> Mapping[str, str]`

  Harmless at runtime: they are module-private, neither imports the other, and nothing resolves the
  name across module boundaries. It is recorded because of what `CLAUDE.md` Guardrail 6 measured
  about detectors that key on **name plus file membership** rather than on what a function's body
  does: a sibling-exclusion filter written as *"ignore any function of this name"* now silently
  excludes **two** functions, and a detector deriving a set by name would fold two unrelated
  functions into one. No detector in `tests/` is known to be fooled by this today — that was not
  swept, and this note claims only the duplication, not a live defect. Neither function was touched.
