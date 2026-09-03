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
> - **§12.27** — COORDINATE detector now wired into real `fleet sequence` (`cli.py:3085`,
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
0 (its own docstring says so); `docs/SPEC.md:7432` item 2 requires exit 0 with no baseline
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
**DONE (round GG task 3).** *(This entry itself was stale before this round — see the addendum at
the top of this file — the "no `pytest-cov`/`coverage` dependency" sentence below was already
false by `bcd08eb`; corrected in place, not rewritten, per ground rule 1.)*
`pytest-cov`/`coverage` are declared in `pyproject.toml` (line 48, `[tool.coverage.run]`/
`[report]`), and `[tool.coverage.report] fail_under = 85` is now armed. **Investigated, not
assumed:** no ADR ever adjudicated the `pyproject.toml` comment's forkserver/multiprocessing
caveat — it was an inherited engineering note (a commit body + a `docs/PROGRESS.md` checkpoint),
not a decision record — so this round measured it directly rather than treating it as settled.
Three independent whole-suite `.venv/bin/python -m pytest --cov=fleet --cov-report=term-missing`
runs (631s, 604s, 733s) completed cleanly — no hang, no crash — and agreed on total coverage to
within rounding (17946 stmts, 1211/1211/1195 miss, branch 4510/679~673, **91%** every time,
comfortably above the 85% floor). The failing tests each run showed (24, 24, then 7 after fixing
part of the gap below) were investigated and are unrelated to `--cov`/forkserver entirely: they
reproduced identically with `--cov` removed, and root-caused entirely to worktree provisioning
gaps — a fresh `git worktree add` does not copy untracked, gitignored local toolchain state:
`tools/bin/ast-grep`, `tools/bin/bazel`, `tools/go/` (real vendored binaries/SDKs), and `.venv`
itself (so `tests/conftest.py`'s own `VENV_BIN` PATH-prepend silently no-ops, hiding
`.venv/bin/uv`). Symlinking/copying each from the primary checkout took the last batch (7 tests,
all `'uv' is not installed`) to `7 passed`. None of this is a coverage/forkserver interaction —
`--cov` is still deliberately not added to `addopts` (general principle: it would slow every
targeted single-test invocation with no benefit), not because a forkserver failure was found — see
`pyproject.toml`'s own comment and `docs/DECISIONS.md` ADR-0111 for the full account (renumbered
from this task's own worktree draft of ADR-0110, a Central Number Allocation collision with §35's
closure below caught at merge). **Note for
the next fresh worktree on this criterion or any other:** copy/symlink `tools/bin/{ast-grep,bazel,
gh}`, `tools/go/`, and `.venv` before trusting a "failure" as a real defect — this round burned
real time on that exact confusion.
`tests/unit/` now holds real tests (`tests/unit/test_retry.py`, moved from `tests/test_retry.py`
— pure `RetryPolicy` logic, no I/O; `tests/unit/conftest.py` + `tests/unit/test_no_provider_credentials.py`,
new — an autouse fixture that clears every `api_key_env` named by every profile in
`config/models.yaml`, which is SPEC's own literal §12.3 mechanism, mutation-verified to actually
clear a planted credential rather than passing vacuously). `pytest tests/unit -q`: 17 passed in
under 1s wall (measured 0.16-0.71s across several runs), no network, no Docker, no credential.
`tests/contract/` is unchanged (still `__init__.py` only) — SPEC's §12.3 text names `tests/unit`
specifically, not `tests/contract`; the latter is a different criterion's (§12.4's) concern per
SPEC's own item numbering, so it is out of this entry's scope, not a residual of it.
**Out of scope:** does not require reorganizing the rest of `tests/` into the `unit/` layout.

## 4. Model round-trip + per-backend golden response
**DONE (round V task 5, 2026-09-02, `16277f8`/`71fc59b`, reviewed Approved with elevated scrutiny)
— all 12 of 12 LLM roles now covered, closing the Done bar SPEC's literal sentence actually names.**
This criterion was previously reverted from a same-day DONE marking (round II final review,
2026-09-02): SPEC's own literal sentence names a scope this criterion's own "Done bar" paraphrase
had silently narrowed, and that round only built the paraphrase's scope. History below kept, not
deleted, per this file's ground rule 1 — see the corrections and round-by-round progress inline for
the full account of how the count moved from 1 to 12 of 12.

**DONE (round II task 3, 2026-09-02, `251c774`, reviewed Approved in full) — as it stood before
this correction.** Round-trip was
already fully covered. `tests/test_llm_golden_responses.py` + `tests/fixtures/llm/golden_responses/
*.json` add one recorded raw response per shipped backend
(`anthropic`/`bedrock`/`vertex` at the `TOOL_CALL` rung, `openai_compatible` at the required
`PROMPTED` rung — the only shipped backend whose declared capabilities make `PROMPTED` its floor
rather than a fallback, confirmed by reading each backend's `declared_capabilities` directly), each
parsed through that backend's real response-parsing code and validated against its declared
schema. Task review independently re-verified every fixture field-for-field against real adapter
code and existing hand-built test helpers, reproduced the Anthropic SDK's own
`Message.model_validate()` succeeding on the anthropic fixture directly, confirmed the round-trip/
golden-response distinction is real (the round-trip test never calls a backend's parse function),
and independently reproduced 2 Rule-12 mutations (required-field removal → genuine
`pydantic.ValidationError`, not a crash). No `DONE_WITH_CONCERNS` on any backend — all 4 plus the
PROMPTED rung genuinely closed. **Done bar met in full, as originally stated:** one recorded golden
response per shipped backend (anthropic/bedrock/openai_compatible/vertex) validating against its
declared schema, plus one recorded at the `PROMPTED` rung, each checked into a fixture and asserted
by a real (non-network) test. **Out of scope, honored:** no live-network golden-capture pipeline
was built — a checked-in fixture satisfies the criterion as written.

**Correction, round II final review (2026-09-02): the DONE marking above overclaimed — SPEC's own
sentence names a subject this criterion's own Done bar was never checked against.**
`docs/SPEC.md:7434`, verbatim: *"every LLM role in `config/models.yaml` has a stored golden
response per shipped backend that validates against its declared schema — including one recorded
at the `PROMPTED` rung."* The subject is **every LLM role**, not "one recorded golden response per
backend" (this entry's own prior "Done bar" text, which the controller wrote when this criterion
was first opened and never re-checked against SPEC's actual sentence before flipping DONE — the
same class of error round FF's §12.8 and round EE's §12.35 both hit).

**Verified directly against source, not inferred.** `config/models.yaml` (lines 14-25) declares
**12**
roles (`conflict_resolution`, `api_incompat_rewrite`, `build_authoring`, `cycle_break_proposal`,
`escalation`, `transform_repair`, `build_diagnosis`, `manifest_extract`, `pr_body`,
`repo_classify`, `dep_disambiguate`, `pr_title`) — not interchangeable: `src/fleet/llm/schemas.py`
maps each to its own response model, and `tests/test_llm_roles.py`'s own
`test_schema_digests_are_stable_and_distinct_per_role` asserts all 12 schema digests are distinct.
Round II task 3's fixtures covered **exactly one**: `tests/test_llm_golden_responses.py:77` (at
round II's own commit) pinned `RESPONSE_SCHEMAS[Role.REPO_CLASSIFY] is RepoClassification`, and
all four fixtures carried a `RepoClassification` payload. No other stored golden response existed
anywhere in the tree at that point.

**Round III task 3 (2026-09-02, `7e8072b`, reviewed Approved) — 2 more roles landed, 3 of 12
now covered.** Refactored the test file into a `GoldenCase`-parametrized table (task review
independently confirmed every original `REPO_CLASSIFY` assertion survives unweakened in the new
structure) and added `PR_TITLE`/`PR_BODY`, each across all 4 backends, wire shapes derived from
real adapter code (confirmed: all 4 backends' response-parsing is role-agnostic — a literal
`_TOOL_NAME = "emit_response"` constant and zero payload-field branching before schema
validation — so reusing `REPO_CLASSIFY`'s proven envelopes with only the payload swapped is sound,
not merely convenient). The schema pin moved to `tests/test_llm_golden_responses.py:113-115` at
round III's own commit (3 assertions, one per landed role at the time) — the citation above
(round II's `:77`) is a record of round II's own state, not repointed. **Correction, round IV
final review (2026-09-02):** round IV's own additions moved the pin again, to
`tests/test_llm_golden_responses.py:124-130` (now 7 assertions, one per landed role) — this
`:113-115` citation is likewise kept as round III's own record, not repointed; see round IV's own
paragraph below for the current location.

**Round IV tasks 2 and 3 (2026-09-02, `bac8b91`/`f937190`, both reviewed Approved) — 4 more roles
landed, 7 of 12 now covered.** Task 2 added `BUILD_DIAGNOSIS`/`DEP_DISAMBIGUATE`; task 3 added
`CYCLE_BREAK_PROPOSAL`/`CONFLICT_RESOLUTION` — both via the same `GoldenCase` table pattern, both
independently investigated the specific complications their roles introduced (task 2: the
nullable `chosen_coordinate` field, confirmed a genuine null-branch exercise, not a mislabeled
valid case; task 3: `mechanism`'s `pattern=r"^(bazel_dep|single_version_override)$"` constraint,
confirmed the mutation test fires specifically on the pattern, not a missing-field error). Both
task reviews independently re-derived the whole-tree `ruff format --check .` claim (123 dirty
files, the pre-existing pinned baseline — not introduced by either task) rather than trusting it,
given round III's own close-out found `main` red from exactly this kind of unverified claim. The
two tasks' diffs conflicted in the same table (both add rows) — combined by the controller at
merge, additive on both sides, no logic conflict. **5 of 12 roles remain** (`api_incompat_rewrite`,
`build_authoring`, `escalation`, `transform_repair`, `manifest_extract`).

**Round V tasks 1 and 2 (2026-09-02, `271f507`/`be48e94`, both reviewed Approved) — 4 more roles
landed, 11 of 12 now covered.** Task 1 added `TRANSFORM_REPAIR`/`MANIFEST_EXTRACT`; task 2 added
`API_INCOMPAT_REWRITE`/`ESCALATION` — both via the same `GoldenCase` table pattern, both this
round's own investigation confirming the nested-object-tuple fields (`LlmPatchProposal.files`,
`ManifestExtraction.dependencies`) are genuinely exercised with multi-entry realistic payloads, not
single-entry placeholders. Both task reviews independently reproduced their Rule-12 mutation
proofs live (task 1's reviewer went further, adding a second discriminating mutation on the nested
`diff` field beyond the implementer's own proof). Task 2's worktree survived a mid-round host disk-
exhaustion interruption; 3 of its 8 fixtures were pre-existing drafts from before the interruption,
independently re-verified field-by-field against the schema rather than trusted on sight (task
review confirmed this directly). The two tasks' diffs conflicted in the same table (both add rows,
same shape as round IV's own precedent) — hand-resolved by the controller at merge by diffing each
task's commit against its own stated base to extract its exact additions and splicing them into
the other's already-merged state; verified via exact count reconstruction (36 baseline + 10 + 10 =
56 passed, matching precisely). **Only `build_authoring` remains** — per round V's own research,
confirmed the largest/most structurally complex of the 12 (a two-level nested-object tuple where
each entry itself carries three sibling array fields), but still sized as a single-task one-shot
following the same proven recipe, not needing a further split.

**Round V task 5 (2026-09-02, `16277f8`, merged `71fc59b`, reviewed Approved with elevated
scrutiny given this criterion's own history) — `BUILD_AUTHORING` landed, 12 of 12 roles now
covered. §12.4 flips DONE.** The task-scoped review independently re-derived SPEC's literal
sentence (`docs/SPEC.md:7434`) fresh rather than trusting any prior round's quotation, independently
re-derived the full 12-role set from `src/fleet/llm/roles.py::Role` / `config/models.yaml` /
`schemas.py::RESPONSE_SCHEMAS` (not from any prior round's count), and programmatically extracted
the full 12×4 role/backend-and-rung cross-product directly from `tests/test_llm_golden_responses.py`
on the landed branch — confirming every one of the 12 roles has all 4 required entries
(anthropic/bedrock/vertex at `TOOL_CALL`, openai_compatible at `PROMPTED`), no gaps. The reviewer
explicitly stated its own independent verdict that §12.4 should flip DONE, separately from
approving the diff. Rule-12 mutation reproduced live in a fresh worktree, including an extra
manual check (deleting a defaulted inner field does NOT discriminate, confirming the landed
mutation genuinely proves two-level descent rather than a shallower check). **One disclosed,
non-blocking finding**: the 4 new fixtures' scenario narrative ("no `EcosystemAdapter` can derive
targets") does not precisely match SPEC §3.3's literal trigger text ("a non-trivial `BUILD.bazel`
target that no generator template covers") or the real `buildgen.py` trigger condition (a per-unit
template miss within an already-recognized ecosystem, not "no adapter at all") — this drift
originates in a pre-existing `schemas.py` docstring, is documentation flavor text no test asserts
on, and does not touch this criterion's actual Done bar (schema validation of a recorded wire
response against a real backend's parse path). Worth a future docs-accuracy pass, not a blocker
here. **§12 count moves from 26 to 27 of 48.**

**Not closed by composition.** `tests/test_llm_roles.py`'s existing round-trip test IS
parametrized over all 12 roles, but its input is a hand-shaped Pydantic-model literal
(`_sample(model_cls)`), never a stored wire response, and not per backend — it proves a different
property than SPEC's sentence names. The union of "round-trip covers 12 roles with fake data" and
"golden fixtures cover 1 role with real data" is not the 12-roles-×-4-backends cross-product
SPEC's sentence requires.

**Not a defect in round II task 3's own work.** The fixtures built are genuine, well-derived
(task review independently verified every field against real adapter code and the real Anthropic
SDK), and they correctly satisfy this entry's own prior "Done bar" paraphrase in full — the defect
is in the controller's DONE flip reading that paraphrase as the criterion instead of re-checking
SPEC's actual sentence, not in the implementation.

**Done bar (revised, matching SPEC's literal text):** one recorded golden response per **LLM
role** (12, `config/models.yaml`) per shipped backend (4), validating against that role's declared
schema, plus one recorded at the `PROMPTED` rung — 48 combinations in the worst case, though a
single fixture per (role, backend) pair likely suffices without needing all 4 rungs per pair
(investigate before assuming the full cross-product is needed literally per-rung; SPEC's own
"including one recorded at the PROMPTED rung" reads as one additional requirement layered on the
per-role-per-backend base, not a multiplier). Round II's landed `REPO_CLASSIFY` coverage across 4
backends + PROMPTED is genuine partial progress, not reverted. §12 count reverts from 27 to
**26 of 48**. (Role count updated below by round III task 3 — see that entry, not restated here,
per round III final review's own finding that this line had gone stale by staying at "1 of 12"
after round III landed 2 more roles; read the entry's own running count, not this line, for the
current figure.)

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
table-derived `KIND_MODIFIERS` form (`78e5667`, `docs/DECISIONS.md` ADR-0100, `docs/SPEC.md:7436`
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
**Out of scope (disclosed, not required by SPEC.md:7436's literal wording):** an aliased import
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
**OPEN — PARTLY, scope-disclosed (round FF task 2 review, 2026-09-02, ADR-0109).** Confidence
reconstruction is exact and covered. The `hypothesis` property-test gap this row originally found
was already **stale** before this round — `tests/test_graph_properties.py` landed at `83e1493`
(2026-08-28, per the audit's own late correction) and covers ADR-0013's wave-ordering claim. The
gap this row's "Done bar" named — cross-repo `INTERNAL_IMPORT` proven only on a hand-built
`InferenceInput`, never the fixture fleet — is closed by
`tests/test_scan_e2e.py::test_an_undeclared_cross_repo_import_produces_a_real_internal_import_edge`
(a real `fleet scan` over a two-repo fixture fleet, asserted against the real `edges` table) plus a
Rule 12 discriminator test
(`test_declaring_the_same_import_turns_it_into_a_declared_dep_not_an_internal_import`) proving the
same import produces `DECLARED_DEP` instead once declared — confirmed genuine by a reverted
mutation of `infer.py`'s manifest-entry check that reddened only the discriminator test. The
hand-built unit test in `test_graph_build.py` is untouched and still covers the unit-level
property.

**Round FF task 2's own task review found this row's "Done bar" is narrower than §12 item 8's
literal text.** The task review traced that narrowing to `docs/superpowers/plans/spec12-success-criteria-audit.md:135`
(`12be741`, 2026-08-27), which scoped "graph correctness" to
exactly the two gaps closed above, without addressing SPEC's broader clause: "**every known
cross-repo edge is discovered** including at least one `INTERNAL_IMPORT`…". `EdgeKind` has 8
members (`src/fleet/models/enums.py:255`). A fixture-fleet, real-`fleet-scan`-driven proof against
the `edges` table now exists for 3 of them: `DECLARED_DEP`, `INTERNAL_IMPORT`, and
`PUBLISHED_ARTIFACT` (**correction, 2026-09-02, round FF's own final whole-branch review**: this
row originally said 2 of 8 and listed `PUBLISHED_ARTIFACT` among the unproven — that was wrong by
direct measurement; `PUBLISHED_ARTIFACT` already has a real fixture-fleet proof at
`tests/test_cli.py:1902-1907`, a §12.21 digest test's precondition guard over an inline-built
two-repo fleet, reproduced `1 passed`). The other 5 — `API_CONTRACT`, `CONTRACT_IMPL`,
`CONTRACT_CONSUME` (no e2e test drives a real scan through a hoisted contract to the resulting
graph edges — `vendored_contract_fleet` only asserts against `contracts`/`collisions`),
`SHARED_RESOURCE`, `DYNAMIC_REF` — remain proven only by hand-built-edge tests
(`test_graph_build.py`, `test_graph_sequence.py:863`, `test_graph_cycles.py`) or direct SQL seeding
(`test_projection.py:107-111`), never a real scan against `edges`.

**Adjudication (ADR-0109):** this is a genuine scope gap, not a SPEC-wording error — the five
unproven kinds are real, already-implemented detectors (`src/fleet/graph/infer.py`) with no
fixture-level proof, not aspirational text. §12 item 8's literal wording is unchanged; no SPEC.md
edit needed. **Done bar (revised):** one real-`fleet-scan`-driven test per remaining `EdgeKind`
(or one test covering several via a shared fixture fleet, implementer's choice) asserting against
the real `edges` table, following this round's `INTERNAL_IMPORT`/`PUBLISHED_ARTIFACT` tests as the
template. Small, additive, TEST-ONLY per kind.

**Round HH task 1 (2026-09-02, `d776c0a`, reviewed Approved in full).** `SHARED_RESOURCE` and
`DYNAMIC_REF` now closed the same way: `tests/test_scan_e2e.py` gained real fleet-scan-driven
proofs against the `edges` table, each with an independently-reproduced Rule-12 negative control
(hand-mutating the fixture's triggering shape and confirming the edge disappears — task review
reproduced both mutations itself, not just re-ran the report's). **5 of 8 `EdgeKind`s now proven**
(`DECLARED_DEP`, `INTERNAL_IMPORT`, `PUBLISHED_ARTIFACT`, `SHARED_RESOURCE`, `DYNAMIC_REF`).

**`API_CONTRACT` — genuine finding, not attempted, not a fixture gap.** Traced exhaustively (task
review independently re-traced every symbol producer, not just read the claim): no shipped
extractor in `src/fleet/workers/symbolindex.py` ever emits a non-definition `API_SYMBOL_KINDS`
symbol — `_proto_symbols` hard-codes `is_definition=True` on every `GRPC_SERVICE`/`PROTO_MESSAGE`
it produces, and `HTTP_OPERATION` is never constructed anywhere in the file. `_api_contract_edges`
(`infer.py:419-446`) requires exactly the symbol shape that never exists in real scan data — its
join is structurally starved of input, independent of any fixture. `tests/test_workers_contracts.py:727-732`'s own docstring already documented this independently; task review confirmed
the quote verbatim. **This is a production extraction-layer gap, not a fixture-design problem** —
closing it needs a `symbolindex.py` change (real API-reference extraction, not just definitions),
out of scope for a TEST-ONLY task. Not yet D-numbered; a future round should size it.

**`CONTRACT_IMPL`/`CONTRACT_CONSUME` — a second, larger finding, sharpening this entry's own
"already-implemented detectors, no fixture-level proof" framing.** Round HH task 2 traced the
persistence path and found these two kinds are computed correctly in memory during a real
`fleet sequence` run reaching a hoist (`_sequence_impl` → `break_cycles` → `_materialize` →
`infer_contract_edges`) but are **never persisted** — the only production call site of
`repository.insert_edges` anywhere in `src/` is `_persist_scan_edges`, which runs at scan time,
before any hoist, and never passes `InferenceInput.contracts`. Unlike `API_CONTRACT` (a starved
join) or the five already-closed kinds (a fixture-proof gap against working code), these two have
**no round-trip production code path at all** — a landed `xfail(strict=True)` test in
`tests/test_sequence_e2e.py` documents the target state and will fail loudly (forcing the marker's
removal) once persistence is wired. **Allocated D97** (`docs/INTEGRATION_HONESTY.md`) — task
review confirmed this is genuinely distinct from D23, not a duplicate: D23's diagnosis (a missing
`retargeted_from_repo_id` column) presumes rows are written today with the wrong shape, but
`insert_edges` has exactly one call site anywhere in `src/` (scan-time only), so no contract-kind
row is ever written at all — fixing D23's column alone would not close this gap.

**§12.8 state after round HH: 5 of 8 proven, 3 of 8 open on two distinct, now well-understood
production gaps** (`API_CONTRACT`'s starved extraction, `CONTRACT_IMPL`/`CONTRACT_CONSUME`'s
missing persistence) rather than one undifferentiated "5 remaining" bucket. Both are real,
traced-to-the-line findings — this round's own work, not fixture design failures.

**D97 fixed, round II task 2 (`a9a1d48`), reviewed Approved — 7 of 8 `EdgeKind`s now proven.**
`_persist_contract_edges` (`cli.py`) now writes `CONTRACT_IMPL`/`CONTRACT_CONSUME` from
`_sequence_impl`'s existing writer block; the `xfail` is gone, the test passes for real. Task
review found (and D23's own entry now cross-references) that this new call site inherits D23's
defect — a retargeted edge's `retargeted_from_repo_id` is silently dropped here too — genuinely
out of this fix's scope, not a regression. **`API_CONTRACT` remains the sole open `EdgeKind`**,
confirmed genuinely NEW-MECHANISM by round II's own research (two unbounded extraction gaps in
`symbolindex.py`: no consumer-reference detection for proto/gRPC symbols, and `HTTP_OPERATION`
extraction doesn't exist at all) — not D-numbered yet, held for a future dedicated round rather
than sized further here.

**Update, round V task 3 (`8f3ca97`, merged `c8c5f37`, task-scoped review Approved) — the
gRPC/proto reference side of `API_CONTRACT` is now closed; `HTTP_OPERATION` is the sole remaining
gap.** Research (round V) found a smaller slice than round II's sizing assumed: `_pattern_symbols`
(`symbolindex.py`) is an existing generic, config-driven, cross-language regex→`SymbolKind`
reference extractor already used for `SHARED_RESOURCE`/`DYNAMIC_REF`, and always emits
`is_definition=False` — exactly the reference-side shape `API_CONTRACT` needed. Round V task 3
extended it with a third pattern category (`scan.api_contract_patterns`, one language-agnostic
regex matching gRPC's wire-level `/package.Service/Method` path literal — verified real in
generated Python/TS/JS stubs, confirmed not to match Java's runtime-concatenated form) plus a real
`fleet scan`-driven fixture-fleet test against the `edges` table (`tests/test_scan_e2e.py`), Rule-12
mutation-proof reproduced independently by the task review (reverting the new call site reddens the
positive test, discriminator test stays green). `API_CONTRACT` is now genuinely reachable
end-to-end for the gRPC/proto case — **this was NOT a NEW-MECHANISM task after all**, contrary to
round II's sizing; it was a pure extension of an existing generic mechanism. `HTTP_OPERATION`
remains entirely unaddressed — no extractor exists for it at all, and (per round V task 3's own
honest read) it likely IS genuinely NEW-MECHANISM, since there is no existing generic extractor to
extend the way `_pattern_symbols` served this task. **§12.8 state: 7 of 8 `EdgeKind`s proven,
`HTTP_OPERATION` the sole remaining gap.**

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
`tests/test_cli.py:4923`, `:5013`, `:5064` — each driving `_transform_criterion` to its specific
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
**DONE (round CC task 1, 2026-09-02, `c77fc71`) — all sub-clauses closed, including atomicity.**
Ladder-length validation,
transient-vs-attempt counting, and 3-rung tier escalation to RHI are all covered. **Corrected
2026-09-01 (round Z research): the "5-rung variant blocked on D50" framing is stale and was
re-verified false, not merely re-stated.** `LadderState` (`orchestrator/retry.py:83-90`) DOES take
a `ladder=` kwarg — the "`LadderState` lacks a `ladder=` parameter" framing was false, and made
elsewhere/orally, not sourced from `tests/test_config_keys_are_read.py:58,308`'s comment
(**correction, round Z final review + fix wave, 2026-09-01**: an earlier version of this sentence
misattributed it there). That comment makes a different, narrower claim: that
`LadderState(...)` AT THAT SPECIFIC CALL SITE (`orchestrator/runner.py`, now line 512, was ~428)
is invoked with no `ladder=` argument passed — not that the `LadderState` *class* lacks such a
parameter. Conflating "this call site doesn't pass it" with "the class can't take it" is the
error; the call-site claim needed no correction THEN, only the class-level one.
**Correction, round EE final review (2026-09-02): the call-site claim is now false too, made
true by this project's own subsequent work, not by a measurement error.** Round EE's task 1
wired `runner.py:512`'s `LadderState(...)` construction to genuinely pass `ladder=
tuple(rung.context_policy for rung in self.ctx.config.transform.ladder)`, closing §12.35's
CLI-level proof — see §35's own entry. The sentence above is left in place as the historical
record of what was true through round Z; it is no longer true of the current tree. More substantively, the row-count/
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

**Closed, round AA task 1 (2026-09-01, `bca81a1`).** The 5-rung sub-clause and the
`ValidationError`-at-construction test are both landed. The `ValidationError` test was NOT
"likely already covered" as this entry previously guessed — `grep -rn "TransformSection"
tests/*.py` returned nothing before this task; `tests/test_settings.py` now covers both the
length-mismatch and the rung-0-shape refusal, plus the positive case, via real construction
(not the config loader). The row-count test could not be built as a `fleet transform` CLI e2e
test as this Done bar originally suggested: investigation found `--deterministic-only`
structurally caps a run at exactly 1 attempt regardless of configured ladder length
(`cli.py:3507-3511`,`:3612`), so a real 5-attempt run cannot be driven through the CLI without a
live LLM backend. Built instead at the `PhaseRunner`+`RetryPolicy` level
(`tests/test_runner.py`), matching the existing 3-rung escalation tests' own architecture —
Rule-12 mutation-proven (`LadderState.exhausted`'s `>=`→`>`, task review independently
reproduced the same failure). **Disclosed residual, not itself part of this sub-clause and not
fixed here:** a REAL (non-`--deterministic-only`) 5-rung run would still repeat rung 3's tier for
rungs 4-5, since `TIER_LADDER`/`DEFAULT_LADDER` are hardcoded 3-tuples reading no config —
flagged for a possible future sub-clause or D-number, orthogonal to the row-count property this
task closed (confirmed independent by both the implementer and task review).
**Closed, round CC task 1 (2026-09-02, `c77fc71`) — the atomicity sub-clause, the last one open.**
`tests/test_repository.py::
test_complete_phase_is_one_write_unit_so_a_failure_leaves_no_half_completed_phase` is a genuine
mid-transaction interruption test, not another end-state-only check: `complete_phase`'s unit has
only one write statement (unlike the landed precedent's two,
`test_the_demotion_is_one_write_unit_so_a_failure_leaves_no_half_demoted_repo`), so the
injection point was adapted — the real `UPDATE` runs unmodified inside the open `BEGIN IMMEDIATE`,
then a monkeypatched `aiosqlite.Connection.execute` raises on it, standing in for a crash after
the write lands but before the unit returns and the writer commits. Same two-assertion discipline
as the precedent: a `writer.submit` call-count proof (one transaction, not several) plus a
post-rollback read-back through a genuinely separate connection, confirming nothing landed.
Rule-12 proven three ways, independently reproduced by task review: a rollback→commit defect in
`_run_in_immediate` (`state/db.py`) reddens the read-back assertion; a split-into-two-submits
defect reddens the submit-count assertion; a cosmetic comment reflow stays green as a control.
**Correction, final review, round CC (2026-09-02): the rollback→commit mutation's claimed
uniqueness was false and unmeasured — the reason was the unmeasured sentence, CLAUDE.md's own
named failure mode.** The original text said no pre-existing test would have caught the
rollback→commit defect; measured, `tests/test_repository.py` alone goes 5 failed / 49 passed
under it — **four** pre-existing tests also catch it
(`test_the_demotion_is_one_write_unit_so_a_failure_leaves_no_half_demoted_repo` — the very
precedent this closure names — plus `test_a_run_refusal_leaves_no_phantom_repo_reservation`,
`test_a_halt_is_written_to_disk_and_refuses_every_later_reservation`,
`test_concurrent_reserve_and_settle_never_exceed_either_ceiling`). The split-into-two-submits
mutation is the genuinely unique discriminator (1 failed / 53 passed) — it is what actually earns
this test its place, not the rollback→commit one.

**Also strengthened, same review: the interrupted call's fixture now asserts SPEC's literal
state, not a structural implication of it.** SPEC §12.13 names the property as "the increment
that reaches the ceiling is the same statement that writes `status='REQUIRES_HUMAN_INTERVENTION'`".
The test as first landed drove `status=SUCCEEDED` for the interrupted call, which never takes the
escalation branch — the property held only by composition with the pre-existing
`test_the_attempt_that_reaches_max_attempts_escalates_in_the_same_state_write`, undisclosed. Fixed
directly (cheap, per the review's own suggestion): the interrupted call now seeds
`max_attempts=1` and `status=PENDING`, so the write it interrupts genuinely IS the
ceiling-escalation write SPEC names, asserted in one test rather than by composition across two.

Task review's own independent judgment (not just the mutation mechanics): this proves the SPEC
claim that "nothing is ever interrupted mid-write" for real, closing the exact gap this entry
named — not a weaker "an exception can happen somewhere" substitute.
**Done bar:** met in full. Nothing remains open for §12.13.

## 14. Blast containment + escape hatch
**OPEN — misattributed to D50 until 2026-09-01 (round X), corrected.** (a) and (b) — containment
and `fleet resume` unblocking — are fully covered through real e2e paths. (c)/(d) are actively
refused: `--stub-blocked` exits USAGE, verified directly against `src/fleet/cli.py:3606-3611`/
`5554-5559` — the refusal text is "`--stub-blocked` is not implemented: emitting a generated stub
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
("out of this file's scope"), matching `docs/SPEC.md:7449`'s literal wording ("one
`PullRequestDraft.scc_id`") rather than the looser "assert all 12 members share one `scc_id`"
phrasing the round plan used to describe the task.
**Done bar (narrowed to the one remaining leg):** a test asserting the shared `scc_id` specifically
through a built `PullRequestDraft` for the 12-node cycle case, per `docs/SPEC.md:7449`'s literal
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
**OPEN — mostly missing — NEW-MECHANISM, plus a real production gap disclosed round AA.**
`resource_guard` defaults to a no-op (`lambda: None`) — RSS/cgroup sampling is not implemented at
all, not just untested (audit row 22). `budgets.max_host_rss_mb` is `KNOWN_INERT` (D50).
**Done bar:** the RSS-sampling sub-clause is blocked on implementing `resource_guard` for real —
this is the one item in this file that's genuinely new infrastructure, not a wiring/test gap.
Track it as its own round.

**Disk-ceiling sub-clause — closed, round AA task 2 (2026-09-01, `0df7075`).** Exit-9-on-disk-
ceiling was already covered; post-exit `migration_state.json` validity is now proven too:
`tests/test_cli.py::test_a_disk_ceiling_refusal_leaves_a_prior_projection_file_untouched` runs a
real `fleet scan` to get a genuine baseline projection, lowers the floor, re-invokes `scan`, and
asserts the projection is byte-identical pre/post the exit-9 refusal — not merely "still parses."
Task review independently reproduced the Rule-12 mutation proof (skip-gate-if-projection-exists →
exit 0 instead of 9, projection silently rewritten).

**New, real production gap found during the same task's investigation — tracked as D96
(`docs/INTEGRATION_HONESTY.md`), now `FIXED, LANDED` (phase-entry half `71c3cd9` round CC task 3,
Phase-2 per-repo-worker half `ebb83d3` round DD task 2 — correction, round II, this entry still
read "disclosed not fixed" after both halves had landed).** Originally found: `_require_disk_
headroom` is called by `scan`/`build`/`verify`/`fleet resume`, but not `transform`'s command body,
and Phase 2's workers (`relocate.py`/`rewrite.py`/`buildgen.py`) carried none of the per-repo
`min_free_bytes` wiring Phase 1/3/4's workers do — see D96's own entry for what the landed fix
actually wired (both halves, phase-entry and per-repo-worker) rather than re-describing it here.
`sequence` is confirmed genuinely exempt (pure computation, no `project_once` call) — do not fold
it into D96's scope. This does not block §12.22's disk-ceiling sub-clause above, which is closed
on the property it actually claims (exit-9 + post-exit validity for the phases that DO enforce
the floor) — it is a separate, real gap in a phase this criterion's own text doesn't single out,
tracked on its own number rather than silently reopening the sub-clause just closed.

## 23. Idempotency — re-scan, re-transform
**OPEN — one sub-clause blocked on D23, otherwise DONE (round S, `4e1975d`).** Re-transform is
fully covered. Re-scan now covers all 8 of the named tables (`edges, contracts, symbols,
manifests, findings, collisions, waves, wave_members` per `docs/SPEC.md:7453`): 6 under re-scan
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
(`cli.py:3085`), persisted to the `collisions` table, gated by a real post-write exit-6 refusal —
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
I3).** `docs/SPEC.md:7458` states three clauses: (a) an integration test that starts the runner
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
**DONE (round GG task 4, 2026-09-02, `2657ec4`/`a5253ab`, ADR-0110) — see the closure paragraph
after the "Done bar" history below for the disclosed residual.** Reverted from an earlier same-day
DONE marking (round EE final review, 2026-09-02) that overclaimed by composition; the capability
that correction found missing is now built, mutation-proven both directions, and closed with an
explicit disclosed scope boundary rather than by silently narrowing what "closed" means.

**Closed, round EE task 1 (2026-09-02, `1fb6be5`), the CLI-level proof.** `--context-policy`'s
blanket refusal (`cli.py`'s old `_validate_transform_flags`) is gone — `_apply_context_policy_
overrides` now builds a per-run `FleetSettings` copy with only the overridden rungs' `context_
policy` replaced, threaded through `RunContext.config` → `PhaseRunner._drive` →
`LadderState(ladder=...)` → `BaseWorker.execute(..., ladder=...)` → `tier_for_attempt`/
`context_policy_for_attempt` (both newly parameterized, no longer reading the hardcoded
`DEFAULT_LADDER` constant on any live path). Final review independently traced every hop of that
chain and found no gap, and independently reproduced a narrower, more discriminating mutation
than the task's own proof (deleting only the `ladder=` sourcing at the runner, not reverting the
whole diff) — confirming the configured policy, not the hardcoded default, is what a worker
actually receives.

**What this closes, stated precisely — a scope question the final review raised and the
controller is settling here, not leaving implicit.** The new CLI-level test
(`tests/test_transform_e2e.py::test_context_policy_reaches_the_worker_not_the_hardcoded_default`)
proves the WIRING enabler: a configured `--context-policy` override reaches the worker (verified
via the actual role/tier the worker resolves to and the actual prompt content it builds), not the
hardcoded default. It does NOT itself re-assert diff-absence — that property was already closed,
independently, at BOTH context-policy rungs by rounds AA and CC (see above), against real
multi-line diffs via genuine per-line sweeps. Re-asserting diff-absence a third time at the
CLI level would be substantially vacuous on this test's own fixture (rung 1 is a deterministic
`RULE_MISS` that produces no prior patch to leak) and was correctly not attempted. §12.35's full
text is satisfied by composition: the worker-level tests prove no diff leaks when a policy runs;
this test proves a configured (not hardcoded) policy is what runs. Neither alone is the whole
criterion; together they are.

**Closed, round AA task 3 (2026-09-01, `7e982d6`), at the `EVIDENCE_ONLY` rung.**
`tests/test_workers_transform.py::
test_the_repair_prompt_omits_every_line_of_a_rejected_diff_and_the_prior_failure_class` closes
both gaps this rung had in one test: the prior `FailureClass` token (`str(FailureClass.
BUDGET_EXHAUSTED)`, chosen distinct from the current attempt's own legitimate `PATCH_REJECTED` to
avoid a false-positive collision) is asserted absent, and diff-absence is now a real per-line
sweep over a genuine multi-line diff built via the same `make_unified_diff` helper production
code uses (`rewrite/pipeline.py:397`) — not the single marker/header-string proxy this entry
previously described for this rung. Task review independently reproduced two separate mutations
(leaking `failure_class` in `_evidence()`; leaking rejected-patch content via `_apply_stderr`'s
`GitCommandError` branch) and confirmed each reddens only its own assertion.

**Closed, round CC task 2 (2026-09-02, `2ba4cef`), at the `EVIDENCE_PLUS_REJECTED_APPROACHES`
rung — the two proxy sites round AA's final review flagged.** `:1106` and `:1213` (both
`assert "diff --git" not in prompt`) are replaced with the same real per-line sweep the
`EVIDENCE_ONLY` rung already has. **A genuine discovery along the way, independently confirmed by
task review two ways**: the OLD `"diff --git" not in prompt` proxy was not merely weaker than a
per-line sweep — it was **structurally blind**. `make_unified_diff`
(`src/fleet/rewrite/apply.py:119`) is a thin wrapper over `difflib.unified_diff`, which emits only
`---`/`+++`/`@@` and content lines; the literal string `"diff --git a/... b/..."` is git's own
preamble, layered on by `git diff` itself, and never appears in this helper's output at all. So
the old proxy could never have caught a real leak through this path, regardless of how much
content leaked. **Correction, final review, round CC: the mutation reproducing this is a
demonstration of assertion form, not of a reachable defect** — `_evidence()`'s
`rejected_approaches` summary carries no field diff text could travel in (see the still-standing
schema-constraint paragraph below), so any mutation that "leaks" diff text there must inject the
marker literal directly; it shows what the old assertion could and couldn't catch, not a live path
a worker bug could actually take.

**Correction, final review, round CC (2026-09-02): a live disclosure from round AA's own final
review was dropped by this round's rewrite, not annotated — restored here rather than left
missing.** `RejectedApproach` (`src/fleet/models/tasks.py:213`) has no field capable of holding
diff text and `reason` is `max_length=280` — still true, re-verified unchanged by this round. So
on the `EVIDENCE_PLUS_REJECTED_APPROACHES` rung the new per-line sweep is a **tripwire against a
future schema change**, not a guard against a currently-reachable leak: no worker defect can leak
diff text through this rung while the schema stands as it is. The test file's own inline comment
already says as much ("this represents what *would* leak if that boundary were ever crossed") —
this entry should say so too, not claim more than the evidence supports.

**Done bar (as it stood before this correction — kept as history, not deleted):** met in full.
Both worker-level rungs have a real per-line sweep in place (the
`EVIDENCE_PLUS_REJECTED_APPROACHES` rung's is currently a tripwire rather than a live guard, per
the schema constraint above — a disclosed, not-blocking residual), and the CLI-level proof is
closed. Nothing remains open for §12.35.

**Correction, final review, round EE (2026-09-02): the DONE marking above overclaimed — SPEC
names a fourth sub-clause the "composition" argument never addressed, and the worker cannot
satisfy it as written.** `docs/SPEC.md`'s literal text for this item ends: *"Flipping rung 3 to
`EVIDENCE_PLUS_PRIORS` via `--context-policy 3=EVIDENCE_PLUS_PRIORS` makes the diff appear —
which is the proof the assertion tests the ladder and not the fixture."* This is a POSITIVE
control, not another diff-absence assertion: it exists specifically to prove the absence tests
above aren't vacuously passing against a policy that could never render a diff regardless of
what the worker does. The controller's DONE-flip reasoned entirely about diff-*absence* and never
checked this sentence.

**Verified directly against source, not inferred**: `workers/rewrite.py::_evidence` (~line
554-596) treats `ContextPolicy.EVIDENCE_PLUS_PRIORS` identically to
`EVIDENCE_PLUS_REJECTED_APPROACHES` — both add only `rejected_approaches` summaries
(`approach_signature`/`reason`/`failure_class`/`attempt`), never a diff. The function's own
docstring says so as a deliberate design choice: *"the previous proposal's diff is carried by no
policy this worker implements, because re-showing a model its own rejected patch biases it toward
tweaking an approach that is wrong at the approach level."* So flipping to `EVIDENCE_PLUS_PRIORS`
at ANY rung cannot make a diff appear today, regardless of `--context-policy`'s wiring — the
positive control SPEC names is not merely untested, it is **currently unimplementable**.

**Not a SPEC-wording problem — a genuinely unbuilt capability, and the enum's own comment says
so.** `ContextPolicy.EVIDENCE_PLUS_PRIORS`'s declaration (`src/fleet/models/enums.py:356`)
carries the comment `# + raw prior diffs; opt-in, never default` — the intent that this policy
specifically (unlike `EVIDENCE_PLUS_REJECTED_APPROACHES`) should render raw diff text has been
documented at the enum since before this session, and `_evidence()` has simply never implemented
that branch. Per this project's Rule 14, "building to match the criterion" is the only direction
this project treats as legitimate closure — adjudicating the SPEC sentence away to match what's
built is explicitly the wrong direction here, especially since the code's own rationale for NOT
showing diffs (bias avoidance) is sound for `EVIDENCE_PLUS_REJECTED_APPROACHES` but doesn't
actually argue against `EVIDENCE_PLUS_PRIORS` ever rendering one — that policy's whole documented
point is the opposite tradeoff (raw diffs, opt-in, never default).

**§12.35 stays OPEN. Remaining, not yet sized for dispatch**: implement a diff-rendering branch
under `EVIDENCE_PLUS_PRIORS` specifically in `workers/rewrite.py::_evidence` (distinct from
`EVIDENCE_PLUS_REJECTED_APPROACHES`'s existing summaries-only branch), then the positive-control
test SPEC's own sentence describes. **The Rule-14 adjudication itself is recorded — ADR-0108**:
building the capability, not correcting SPEC's wording, is the ruling; do not re-litigate that
question when this is picked up, only its sizing. A future round's research should confirm: where
the diff text would come from (payload's own rejected-patch data, or a git re-derivation), and
whether this is a genuine one-shot or needs further design work given the bias-avoidance rationale
already on record for the sibling policy. The CLI-level wiring closed by round EE task 1
(`--context-policy` genuinely reaching the worker) is real, correct, independently re-verified
progress and is NOT reverted by this correction — it is a necessary but not sufficient piece of
this criterion's closure.
**Done bar (as it stood before this closure — kept as history, not deleted):** the
EVIDENCE_PLUS_PRIORS diff-rendering capability above, plus its positive-control test, both still
to build.

**DONE (round GG task 4, 2026-09-02, `2657ec4`; controller fix `a5253ab`; adjudication ADR-0110).**
The capability ADR-0108 ruled must exist is built: `RewriteInput.prior_rejected_diffs: list
[FilePatch]`, never touching `RejectedApproach`/`rejected_approaches` (both confirmed byte-for-byte
unchanged from base, independently, by task review), rendered by a new `_evidence()` branch gated
on `ctx.context_policy is ContextPolicy.EVIDENCE_PLUS_PRIORS`. SPEC.md:7461's own proof shape
passes both directions, each independently proven a genuine discriminator (old-fails/new-passes,
mechanically stripped and restored with zero drift): the diff appears under `EVIDENCE_PLUS_PRIORS`
(every non-blank line, not just a marker), and — task review's own finding, closed same-round by
controller fix `a5253ab` — the identical payload stays leak-free under the DEFAULT_LADDER's own
rung-3 policy (`EVIDENCE_PLUS_REJECTED_APPROACHES`), reddening under the exact one-token-class
gate-widening mutation a plausible future refactor could make.

**Disclosed residual, per ADR-0110 — not a blocker on this criterion, read the ADR before treating
it as one.** No production caller populates `prior_rejected_diffs` today — `cli.py::_rewrite_input`
never sets it, and nothing else in `src/` does either. A real `fleet transform
--context-policy 3=EVIDENCE_PLUS_PRIORS` run therefore renders an empty `prior_diffs` list; only a
fixture-driven test (this criterion's own proof shape, per SPEC's literal framing — "a fixture...
is driven through the full ladder") exercises a populated one. This is symmetric with
`RewriteInput.rejected_approaches` (`EVIDENCE_PLUS_REJECTED_APPROACHES`'s own field) being equally
unpopulated by any production caller — round GG's own research confirmed zero production
constructors of `RejectedApproach` anywhere — and §12.36 (anchoring detection) is the criterion
that actually depends on that production data flow existing; it correctly stays OPEN, blocked on
D50. ADR-0110 rules this criterion's own header — "no raw prior diff reaches a prompt under the
default ladder" — is a claim about worker behavior given inputs, not about where production
sources those inputs, so the gap belongs to §12.36/D50, not here. **Not yet built (tracked
informally, no D-number — a disclosed scope boundary, not a defect):** wiring
`PhaseRunner._drive()`/its `_payloads` Protocol to thread a real prior rung's rejected `FilePatch`
into the next rung's payload (research's §1e, this round).

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

**Update, round VI (2026-09-02) — the creation gap is deeper than "one NEW-MECHANISM worker task";
three structural blockers found, one load-bearing, likely needing its own ADR.** A dispatched task
(`agent/roundvi-task4`, `0b31b54`, not merged — report only, zero code changed) investigated
building the minimal single-consumer stub-creation path and correctly reported BLOCKED rather than
forcing a fit. Full detail in that branch's `task-9-report.md`; summary:

- **Blocker A (load-bearing).** There is no admission path for a `BLOCKED` repo, anywhere.
  `WaveScheduler.admit()` unconditionally excludes every `RepoStatus.BLOCKED` member;
  `ALLOWED_TRANSITIONS[RepoStatus.BLOCKED]` (`models/enums.py:41`) permits only `{PENDING,
  SKIPPED}` — no edge to `RUNNING`/`DEGRADED` exists. The one existing re-admission mechanism
  (`fleet resume` step 6's synthetic-wave append) triggers only when a repo's `blocked_by`
  recomputes to *empty* — which never happens for a stub-eligible consumer, since the abandoned
  provider stays `REQUIRES_HUMAN_INTERVENTION` forever. Reusing that mechanism means teaching it a
  genuinely new second trigger condition, not wiring an existing one. `tests/test_scheduler.py::
  test_a_blocked_repo_is_not_admitted` locks the current behavior down as an invariant — any fix
  here changes tested behavior (correctly extending it is the real work, not a side effect).
- **Blocker B.** "The abandoned provider's last published version" has no durable field, not just
  no populated one — `coordinates` (schema.sql) has no version column at all, and `_repo_facts`
  (`cli.py:6969-7008`) always constructs `published: Coordinate` with `version_spec=None`. This is
  a schema-or-design decision (new column vs. re-parse-from-git-history-at-stub-time), not a
  re-derivation from an existing carrier as previously assumed. **This was Blocker B's state as
  investigated by round VI task 9; see the round VI task-12 update below for its landed fix —
  history kept as the record of what was true when this paragraph was written, not repointed to
  the post-fix state.**
- **Blocker C (newly found, not previously flagged).** No code branch reclassifies a stubbed
  `C → P` edge from internal to external — `_unit_deps` (`cli.py:7078-7171`) resolves every
  consumer→provider edge straight to the provider's own internal Bazel label with no stub-aware
  branch, so a stubbed consumer's generated `BUILD.bazel` would reference a package that was never
  materialized: a build break, not the stub SPEC promises.

**Done bar (revised):** three sequential prerequisites before the previously-scoped "bounded"
piece (trigger detection + `StubRecord` construction + `workspace_deps()` render + the `stubs`
INSERT + `EMPTY_FAILING` rendering — genuinely small, but has no real caller shape until the three
blockers below resolve) can be built without risking a narrower overclaim requiring a rewrite:
(1) admission design for a stub-eligible `BLOCKED` repo (likely its own ADR, given it changes a
tested invariant — the state-machine-adjudication shape ADR-0112 set a precedent for); (2) a
version-sourcing schema/design decision; (3) `_unit_deps`'s stub-aware reclassification branch.
Each is independently dispatchable and each is sized comparably to an ordinary round task, not a
one-shot bundle. **Still not ready for a single-task dispatch; ready for the FIRST of three
follow-on tasks (Blocker A, since it's the prerequisite for the other two being end-to-end
testable).**

**Update, round VI research-4 (2026-09-02) — Blocker A's framing corrected, adjudicated, ready to
dispatch.** A follow-up research pass found the "state-machine-adjudication shape" comparison to
ADR-0112 above was WRONG: every transition the stub scenario needs already exists in
`ALLOWED_TRANSITIONS`, and `WaveScheduler.admit()`/`test_a_blocked_repo_is_not_admitted` need zero
changes under any design considered. The real gap is narrower — `orchestrator/reentry.
still_blocking`'s removal predicate only recognizes "blocker reached `SUCCEEDED`," never true for
a terminal RHI provider. **Adjudicated via ADR-0113** (`docs/DECISIONS.md`): a new, separate
predicate combined with `still_blocking` at the `plan_unblocking` call site (`still_blocking`
itself never edited), with its CLI surface (`fleet resume --stub-blocked`) gated until the
TRANSFORM-worker stub-creation logic (item 3, `_unit_deps`'s reclassification, plus the `stubs`
INSERT and `EMPTY_FAILING` rendering — Blocker A does not include building these) also exists, to
avoid dispatching real work against a dependency that objectively cannot succeed. **Blocker A is
now ready for direct dispatch** — ADR-0113's own §7 gives a design precise enough to hand a worker
without further investigation.

**A fourth, previously-untraced item, found by the same research pass and distinct from all three
blockers above**: `_eligible_build_units` (`cli.py:8630-8665`) filters on the literal string
`phases.status = 'SUCCEEDED'`, which would silently exclude a `DEGRADED` stub-limited consumer from
the BUILD-phase domain — contradicting SPEC's "draft-only PRs" requirement for that case. Correct
for everything the codebase can reach today (nothing writes a real `DEGRADED` TRANSFORM-phase row
in production yet); does not need its own ADR (a mechanical domain-widening, not a guarded-
invariant change) but must land no later than whichever task first makes a TRANSFORM phase reach
`DEGRADED` in production, or it silently strands every such repo out of BUILD/VERIFY. Tracked here
as §37's fourth done-bar item, not yet its own D-number.

**Update, round VI task 10 (2026-09-02, `26db8a8`, merged `9a0a741`, task-scoped review Approved
with elevated scrutiny on both ADR-0113 conditions) — Blocker A lands.** `stub_permits_removal`
(`orchestrator/reentry.py`) is a new, separate predicate combined with `still_blocking` via
OR-logic at the `plan_unblocking` call site; `still_blocking` itself is confirmed byte-for-byte
unchanged (diffed in isolation, zero removed lines in its body — independently re-verified by the
review, not just the implementer's own claim). `stub_blocked` threads through
`_unblock_dependents`/`_apply_unblocking`/`clear_blocked_by` exactly as `floors` already does
(confirmed by side-by-side comparison). A new `--stub-blocked` flag exists on `fleet resume` and
is confirmed genuinely unreachable — `_validate_resume_flags` refuses it unconditionally with
`UsageError` before `_resume_impl` is even constructed, and `_resume_impl` itself has no
`stub_blocked` parameter at all, so no live path from the CLI to the new plumbing exists this
round, per ADR-0113's condition 2. Both Rule-12 mutations reproduced independently by the review
in a fresh interpreter-isolated worktree.

**§37 state after Blocker A: two of three structural blockers remain (B — version-sourcing
schema/design decision; C — `_unit_deps`'s stub-aware target-label reclassification), plus the
fourth item above (`_eligible_build_units`) and the still-unbuilt TRANSFORM-worker stub-creation
logic itself, which this task deliberately does not build (its CLI surface stays refused until
that logic exists — see ADR-0113's condition 2). §37 does not move toward DONE from this landing
alone; it removes the load-bearing prerequisite for B and C to become end-to-end testable, per
research-4's own framing.**

**Update, round VI task 12 (2026-09-02, `6b07925`, merged `13242e2`, task-scoped review Approved
with elevated scrutiny given the schema migration) — Blocker B lands.** New `coordinates.version`
column (migration `v009_coordinate_version`), captured at the existing `coordinates` write site:
research-5 found the value was already computed transiently by 4 of 5 ecosystem adapters every
scan (python/npm/maven/cargo — Go is a genuine, disclosed ecosystem limitation, module versions
are VCS tags not in-repo content) and simply discarded before reaching durable storage — Blocker B
was "the value is thrown away," not "the value cannot be derived." Only `owned=True` (the repo's
own publish) may ever supply a non-NULL version; a dependency declaration (a range, e.g. `^1.2`)
never does, guarded by an `ON CONFLICT ... DO UPDATE SET version = COALESCE(...)` clause mirroring
the existing `owner_repo_id` pattern exactly. `_repo_facts` now reads the column into
`Coordinate.version_spec`. No ADR was needed (purely additive, no state-machine/tested-invariant
change). Review independently re-verified all 5 adapters, the write-site guard, reproduced the
Rule-12 mutation proof against a real `fleet scan`, and — with elevated scrutiny given the schema
bump broke and required fixing three pre-existing tests — confirmed each fix genuinely preserves
its test's original intent rather than being mechanically patched to pass.

**§37 state after Blocker B: one structural blocker remains (C — `_unit_deps`'s stub-aware
target-label reclassification), plus the `_eligible_build_units` item and the still-unbuilt
TRANSFORM-worker stub-creation logic. §37 does not move toward DONE from this landing alone —
`coordinates.version` has no consumer yet (the still-unbuilt stub-creation logic is its only
planned reader); this makes Blocker C's own eventual work end-to-end testable, per the same
"removes a prerequisite, doesn't close the criterion" pattern Blocker A's landing established.**

**Update, round VI task 13 (2026-09-02, `7bbc0c3`+`f75493c`, merged `f98bdc9`, task-scoped review
Approved) — Blocker C lands. All three of §37's original structural blockers (A, B, C) are now
closed.** `_unit_deps` now looks up an `ACTIVE` `stubs` row keyed on `(consumer_repo_id,
dst_coord_key)` before falling back to the provider's own internal label; `SUPERSEDED`/`RESOLVED`
correctly fall through to the existing (unmodified) behavior, since T1's own invariant guarantees
the real label is live on the branch by then — a genuine finding, not a restatement: the predicate
is `state = 'ACTIVE'` only, narrower than "any OPEN state," and generalizing by analogy with
`_stub_reconcile_inputs`'s own broader filter would have been wrong. No ADR needed (pure read-only
label-resolution logic, no tested-invariant lockdown, inert until a `stubs` row exists in
production). Review independently re-derived the state-predicate reasoning from
`orchestrator/stubs.py`'s own source (not the report's summary), reproduced the Rule-12 mutation
proof in a fresh worktree (genuine discrimination — mutating the state filter reddens only the
`SUPERSEDED` case), and verified a disclosed test-setup workaround (a raw-SQL `BLOCKED → PENDING`
write between two `build()` calls, needed because `propagate_blocked` has zero `stubs` awareness
and unconditionally blocks the consumer otherwise) genuinely reaches the code under test rather
than bypassing it.

**§37 state after all three blockers: what remains is exactly what research-6 named — the
still-unbuilt TRANSFORM-worker stub-creation logic itself (trigger detection, `StubRecord`
construction, the ecosystem `workspace_deps()` render, the `stubs` INSERT, `EMPTY_FAILING` target
rendering, the `RUNNING → DEGRADED` transition — genuinely NEW-MECHANISM, not a one-shot), the
`_eligible_build_units` domain-widening item, and ADR-0113's condition-2 CLI gate (stays refused
until the stub-creation logic exists). Landing all three structural blockers does NOT close §37 or
§12.37 — nothing in production writes a `stubs` row yet, so every branch this round's three tasks
added is currently inert. §37 stays OPEN — PARTLY ADDRESSED, now blocked on exactly one remaining
piece of work (the stub-creation logic) rather than three separate structural prerequisites plus
that logic.**

**Update, round VI research-7 + research-8 (2026-09-02) — the stub-creation-logic bundle was
re-scoped now that all three structural blockers are landed, and a genuinely new, deeper open
question was found — not yet a worker-ready task.** Two research passes (full detail:
`.superpowers/sdd/round-V-criteria-closure/research-7-report.md`,
`.../research-8-report.md`) found:
- Research-3's original sizing (pre-dating the three blockers) had two real errors, both load-
  bearing: item `b` (version-sourcing) is now fully retired by Blocker B (free, via `_repo_facts`);
  item `d` (locate the `BuildInput` assembler) traced to the WRONG PHASE — the real creation
  decision site is `_transform_payloads`'s `build()` closure (TRANSFORM phase, `cli.py:4814-4860`),
  not BUILD-phase `buildgen.py` — matching ADR-0113's own repeated "TRANSFORM-worker" phrasing
  that no prior research had explained.
- Two items absent from research-3's table entirely: a `RUNNING → DEGRADED` write site (nothing
  currently writes this status the first time, in production, for a stub-limited repo), and a
  THIRD `--stub-blocked` refusal site (`_validate_resume_flags`, new since Blocker A — its own
  docstring already names removing it as this bundle's job, per ADR-0113 condition 2).
- **A genuinely deeper, newly-found open question, NOT specific to stubs**: research-8 traced
  whether TRANSFORM's existing rewrite mechanism could redirect an import toward a stub's label
  and found **no orchestrator code anywhere in `src/` dynamically constructs a `RewriteRule` for
  ANY cross-repo relocation, stub-eligible or not** — the only live rule-construction path is
  static, operator-authored YAML (`config/rules/*.yml`, currently empty in this tree). Determining
  "the OLD import text a consumer currently writes for a repo being relocated" is an unresolved
  question for the whole rewrite system, and a stub-redirect rule needs exactly that same
  unresolved piece to know what to match against. This is NOT a stub-specific gap surfaced by
  §37's investigation — it is a pre-existing gap in ordinary cross-repo relocation that §37's own
  bundle happens to depend on.
- Net aggregate size of the stub-creation-logic bundle is comparable to research-3's original
  estimate (real shrinkage offset by real growth), not ADR-shaped (no tested invariant or
  state-machine edge is touched), but **not yet dispatchable as a worker-ready task** — the
  OLD-import-text question needs its own resolution first, and that resolution is scoped wider
  than §37 alone. Deferred pending a future round's investigation of the underlying rewrite-rule
  construction gap; not attempted further this round.

## 38. No ready-for-review while a stub is unresolved
**OPEN — mixed, 20 sub-clauses — correction, round IV: D94 AND D101 block, not D94 alone.** D92
and D93 are both `FIXED, LANDED` (round GG task 1, `7cd6647`; round EE task 2, `b774c8f`); round
IV task 1's own re-audit found a second, independent blocker (D101 — the `UnmergedDependency`
finding + `--sync` clearing sub-clauses) that this "only D94" framing predates and does not
name — see the correction and D101 reference further down this entry for the full account. The
headline
refusal (exit 2 + PrState unchanged) is well covered by two independent tests. **Round Z task 2
re-audited the sweep sub-clauses against D80's landed `stub_reconcile`** (search
`tests/test_cli.py:2535-2701`) and closed the genuinely missing pieces: the `SUPERSEDED` arm of
the refusal guard (was untested — only `ACTIVE` ever seeded), and the positive case (a genuinely
`RESOLVED` stub → `fleet pr --ready` opens non-draft and fires `gh pr ready`) — both
mutation-proven, independently reproduced by task review. The T4 abandon path and the
held-for-merge carve-out were already covered pre-round, confirmed by the same re-audit.

**The remaining sub-clauses were blocked on three real production gaps this same investigation
found and disclosed; two are now closed:**
- **D92 — FIXED, LANDED.** `PrState.HELD` was declared and documented as `stub_reconcile`'s
  exclusive write target but had zero production writers; now wired (round GG task 1).
- **D93 — FIXED, LANDED.** No exit-code path read `RepoStatus.DEGRADED` for the exit-7 contract
  SPEC §3.5.1 point 5 claims; now wired (round EE task 2).
- **D94 — still OPEN, still the sole remaining blocker.** This criterion's own "resolution"
  sub-clause (an already-open PR getting promoted to ready once its stub resolves —
  rebase/force-push/body regeneration) has no implementation at all; `_pr_impl` skips any repo
  with an existing PR record unconditionally. This is why the resolution sub-clause specifically
  cannot be tested, not merely untested. Confirmed genuinely NEW-MECHANISM sized, no smaller slice
  found, twice independently (round FF's and round GG's own research).

**Done bar:** with D92/D93 landed, re-audit which of the 20 sub-clauses are now buildable against
current `HEAD` (not re-derived here — this is round II's own dispatched task) rather than assuming
all remain blocked; D94 alone blocks the resolution sub-clause specifically, not the whole
criterion.

**Round II task 1's re-audit (2026-09-02), reviewed Approved — no new tests needed, a
documentation-credit re-audit, plus one significant new finding.** Two sub-clauses D92/D93
newly unblocked turn out to already be covered by existing tests, just uncredited: the
held-for-merge `PrState.HELD` case (D92's own extended test,
`tests/test_cli.py::test_resume_stub_reconcile_holds_a_stub_whose_provider_still_has_an_open_pr`)
and the generic exit-7-on-DEGRADED sub-clause (D93's own four `_needs_human_attention` site tests,
`test_scan_e2e.py`/`test_transform_e2e.py`/`test_build_e2e.py` — task review confirmed all four
independently, including that the `test_build_e2e.py`-housed one actually drives the real
`verify` subcommand despite living in the build test file). Several other sub-clauses were already
covered pre-round, confirmed unrelated to D92/D93. The whole "resolution" sub-clause remains
D94-blocked, re-confirmed.

**Significant new finding, allocated D98 (`docs/INTEGRATION_HONESTY.md`) — independently
reproduced by task review with its own fresh probe, not the implementer's.** `fleet resume`'s own
exit code does NOT reach 7 in the "pure stub-abandon end-of-run" case — D93's fix lives only in
the four phase-command sites (`scan`/`build`/`transform`/`verify`), never in `resume()`'s own exit
path (`_continue_impl` returns early with `halted: None` when nothing is servable, and the exit
determination is a no-op on that path). A resume whose only event this cycle is a stub abandonment
silently exits 0. Task review additionally flagged, without confirming to the same rigor, a
possible SECOND gap in SPEC's own literal text — see D98's own entry for the open question.

**Correction, round III final review (2026-09-02) — D92, D98, and the held-for-merge test's own
credited behavior all changed under round III, and none of this entry's text above was updated.**
Round III fixed all three defects this entry names — D92's own write was found mistargeted and
replaced (D99/D100, `448ceae`), D98's exit-code gap was fixed (`600360d`) — and D98's own "open
question" was answered (also `448ceae`), not left open. Specifically:
- The bullet above reading *"D92 — FIXED, LANDED... now wired (round GG task 1)"* names a write
  site that no longer exists — D92's original write targeted the wrong entity and was replaced,
  not merely re-confirmed; see D92/D99/D100's own entries for the corrected account.
- `test_resume_stub_reconcile_holds_a_stub_whose_provider_still_has_an_open_pr`, credited above as
  covering *"the held-for-merge `PrState.HELD` case,"* now asserts the OPPOSITE — that no
  `PrState.HELD` write targets the provider. The underlying held-for-merge carve-out (the `stubs`
  row staying `ACTIVE`) is still covered by this test; the specific `PrState.HELD` behavior
  originally credited to it is not, because that behavior was a defect (D100) and has been removed.
- D98 is `FIXED, LANDED`, not an open "significant new finding" — see D98's own entry, which
  itself now also carries a forward reference resolving its own disclosed open question.

This criterion's own 20-sub-clause re-audit against the CORRECTED D92/D98/D99/D100 state is not
re-derived here — a future round should re-run round II task 1's re-audit against current `HEAD`
rather than trust this entry's pre-round-III account of what's covered.

**Round IV task 1's re-audit (2026-09-02), reviewed Approved — the re-run this correction asked
for.** Independently re-derived a 20-sub-clause enumeration (none previously existed in the tree)
against current `HEAD`, matched against SPEC §12 item 38's own literal text directly. Clauses
touching D92/D98/D99/D100 (refusal, end-of-run ABANDONED/HELD/exit-7, the held-for-merge
carve-out) are already covered — by tests landed inside the fix commits themselves
(`600360d`/`448ceae`/`7cd6647`), not newly buildable now; task review independently confirmed each
named test genuinely covers what's claimed by reading them directly. Clauses on "resolution
mechanics for an already-open PR" remain D94-blocked, re-confirmed against current `_pr_impl`.

**New, disclosed finding — a distinct, already-self-disclosed gap, deliberately not
D-numbered.** Two complementary-case sub-clauses (a `BLOCKED` consumer + `UnmergedDependency`
finding, and its clearing via `--sync`) are genuinely unimplemented —
`grep -rn "UnmergedDependency" src/ tests/` finds it only in comments/docstrings, never a write
site or test. Task review independently confirmed this is not a new gap this session's fixes
created: `src/fleet/models/state.py:146-148` and `src/fleet/orchestrator/reentry.py:711-714`
already name "a `pr.merge_wait_timeout_s` breach (§3.4)" as one of three `blocked_by` triggers
with "0 producers today" — the code's own comments already disclose this as unbuilt.

**Correction, round IV final review (2026-09-02) — the "no D-number" reasoning above was checked
and found false, not just unverified.** D78 (`docs/INTEGRATION_HONESTY.md`) is a real counter-
example: a code-disclosed gap that WAS given a D-number, explicitly ruled in by its own body,
naming D56/D57 as the same class. **Allocated D101** instead of leaving this un-numbered — see
that entry for the full account.

**Update, round V (2026-09-02) — D101 splits in two; Half A landed, Half B remains open.** D101's
own entry (`docs/INTEGRATION_HONESTY.md`) now records: Half A (the `BLOCKED`-consumer
`UnmergedDependency` finding write) FIXED, LANDED (task 4, `6f9b777`/`609f57f`) per an adjudication
in ADR-0112 (`docs/DECISIONS.md`) that the finding alone satisfies this sub-clause without a
`RepoStatus` transition. Half B (the `--sync`-triggered clearing of that finding) remains fully
open and is now known to be larger than originally scoped — it also requires building T1
(`orchestrator.stubs.supersede`)'s production trigger from scratch, tracked at **D102**. §38 stays
**partial**, now blocked on **D94 and D101-Half-B/D102**, not D94 and D101 as an undifferentiated
pair.

**Update, round VI (2026-09-02) — D102 FIXED, LANDED (task 6, qualified); D101 Half B splits
further, only Half B(ii) still blocks.** T1's production trigger is now real (D102's own entry) —
Half B(i), "firing T1," is landed. Half B(ii), the `--sync`-triggered *clearing* of the
`UnmergedDependency` finding, was never in task 6's scope (its brief's build instructions never
asked for it) and remains fully open — no production code reads or clears that finding kind. §38
stays **partial, now blocked on D94 and D101-Half-B(ii)** specifically, not the whole of D101/D102
— narrower than the prior update, not wider. D102's own qualifier applies here too: T1's trigger
can only fire against `stubs` rows nothing in production creates today (§12.37/D80's own stub-
creation gap), so this narrowing does not by itself move §38 or §12.37 closer to DONE.

**Update, round VI task 8 (2026-09-02) — D101 Half B(ii) lands; D101 is now FIXED, LANDED in
full (with a qualifier).** All three named pieces of D101 are genuinely implemented and tested.
§38 stays **partial, now blocked on D94 only** as far as D101/D102 are concerned — the whole
D101/D102 chain this row has tracked across three rounds is closed. **But this does not flip §38
DONE**: D94 alone remains a real blocker (still NEW-MECHANISM, 5+ confirmations), and a NEW gap
(**D105**) surfaced by this same landing means the clearing D101 Half B(ii) built is not reliable
under `fleet resume --repoll-prs`'s specific invocation shape (a same-call interaction with
`stub_reconcile` can immediately undo it) — §38's own criterion text should be re-checked against
whether it requires reliability under that specific command, since D105 is a genuinely separate,
disclosed gap from anything D94 blocks.

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
**DONE (round DD, 2026-09-02) — confirmed by the round's final whole-branch review; the "5 of 9"
framing was stale and is retired.** `register_backend`'s duplicate-name `RuntimeError` has a real test
(`tests/test_llm_client.py::test_register_backend_refuses_a_duplicate_name`, round N,
`cc12a2a`). SPEC's literal text (`docs/SPEC.md`, "A new backend costs one file and one registry
line") names, distinctly: (1) the fixture-backend proof — **closed round DD task 1
(2026-09-02)**, `tests/test_llm_backend_fixture_e2e.py` + `tests/fixtures/llm/echo_backend.py`,
reviewed Approved, mutation-proven ("serves every role" genuinely discriminates — truncating the
role loop reddens it); (2) the duplicate-name refusal — closed round N, above; (3) refusal at
`RunContext` construction when the active profile names a backend that does not resolve —
`tests/test_backend_registry_gate.py::
test_a_shipped_name_the_live_registry_lacks_is_refused_at_startup`; (4) refusal when a tier has an
empty target list — `tests/test_llm_roles.py::
test_a_role_whose_tier_has_no_target_fails_loudly_at_startup` and
`tests/test_settings.py::test_a_role_routed_to_an_empty_tier_is_a_startup_error`; (5) refusal when
an `openai_compatible` target has no `base_url` — `tests/test_settings.py::
test_openai_compatible_target_without_base_url_is_refused`; (6) refusal when a role's tier is not
a `ModelTier` member — `tests/test_settings.py::
test_a_role_routed_to_a_tier_that_is_not_a_modeltier_member_is_a_startup_error`.

**The final review independently traced each of the six to its actual raise site rather than
trusting the controller's grep, and specifically settled the "construction, not wave 7" timing
question.** None of the four condition-refusals (3-6) fire inside `RunContext.__post_init__`
itself — three fire at `FleetSettings.load()` (strictly BEFORE a `RunContext` can be built at
all) and one (empty target list) at `LlmRouter.__init__`, which every `cli.py` call site
constructs inline as a `RunContext(` argument (`llm=llm_router(settings)`, five sites) — so
`TierNotConfigured` genuinely raises during evaluation of `RunContext(`'s own argument list.
Refusing at settings-load time is strictly stronger than SPEC's "not in wave 7" bar, not a gap in
it. The old "9" denominator and "four remain open" framing predate work that has since landed and
are retired — do not cite them.

**One disclosed narrowing in the fixture-backend proof (1), not treated as a gap.** SPEC's phrase
is "serves every role in a full fixture run"; the landed test drives 12 direct
`client.complete()` calls through a directly-constructed `LlmRouter`, not a real
`scan`/`transform`/`build` pipeline (a real fixture run cannot reach all 12 roles — most fixture
repos exercise only a handful). The final review judged this a STRONGER proof of the criterion's
headline claim ("one file, zero `src/fleet/` changes"), not a weaker one, and independently
reproduced its own mutation proof (truncating the role loop reddens naming the 11 missing roles;
a synthetic `src/fleet/` dirty-tree fault reddens the zero-changes assertion; a cosmetic control
stays green).
**Done bar:** met in full. Nothing remains open for §12.42.

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
**DONE (round BB, 2026-09-01) — all 5 remaining sub-clauses covered, across two parallel tasks.**
The criterion's full stated text breaks down into 7 sub-clauses: 5 closed this round, 1 covered
pre-existing (clause (iii)'s trailers-re-derivation half, cited below), and 1 resolved via
SPEC-text adjudication (a wording fix, not a test) — 7 total, all accounted for, none silently
folded into another category.

**Correction (2026-09-02, round BB fix wave 1, response to final-review finding F3).** An earlier
version of this entry said "the 2 already-covered sub-clauses (adjudication and the
`collisions.blob_shas` regex fix) plus the 5 closed this round" — that accounting was wrong,
traced against the actual source (`docs/superpowers/plans/spec12-success-criteria-audit.md:152`,
audit row 45). The audit's 2 residual items there were partials, not "already-covered" items: an
adjudication and a SPEC regex edit aren't assertable sub-clauses. The real 2 partials were (a)
trailer coverage at 3-of-6, which this round's task 2 itself closed — already counted among the
"5 closed this round" above, so it was double-counted in the old framing, not missing — and (b)
clause (iii)'s second half, which had no citation anywhere in this entry until now (see the new
bullet below).

**Precision on what's tested against real production output vs. a construction (response to the
"non-vacuous" overclaim finding).** Not a single blanket adjective for all 7 sub-clauses: after
this fix wave's F1 (below), clauses (i) (resolvability + 40-hex format) and (ii) (content scan)
are both tested against real production output, and so is clause (iii)'s six-trailer half. Clause
(iii)'s artifacts-deletion half is tested against a construction that proves the property but is
not organically-produced real data — a manufactured stand-in file, disclosed in F2 below — because
this round's fixture only reaches Phase 2 and production writes nothing under `artifacts/` before
Phase 3.
- **Task 1** (`tests/test_migrations.py`): clause (i)'s schema sweep — `PRAGMA table_list`
  contains no `mutations` table, and `PRAGMA table_info` across every table matches no column name
  against the forbidden-pattern regex quoted verbatim from `docs/SPEC.md`'s own dated correction
  (self-validated both directions: zero false positives against the real current schema, real
  positive matches on synthetic forbidden names) — plus proving the 5 named git-SHA-shaped
  columns resolve to real commits (`git cat-file -e <sha>^{commit}`) against a hand-seeded,
  real-git-backed fixture. Clause (ii)'s content scan (no persisted TEXT/BLOB value anywhere
  contains a unified-diff hunk header) reuses the same column-enumeration helper, mutation-proven
  by planting a synthetic hunk header and confirming the scan catches it before trusting a
  zero-count result — originally against a hand-seeded, 5-row fixture (5 of 22 tables). **Fix
  wave 1 (F1, 2026-09-02, response to final-review finding F1)** added a second scan, in
  `tests/test_no_state_outside_git.py`, reusing the same column-enumeration helper and
  self-validation shape against a REAL completed `scan`→`sequence`→`transform` run (71 rows / 15
  tables / 487 non-NULL TEXT/BLOB values, per the review's own measurement) — closing the coverage
  gap the hand-seeded fixture left (it never touched `edges`/`findings`/`manifests`/`symbols`/
  `coordinates`/`waves`/`wave_members`/`reservations`/`budget_ledger`/`repo_ledger`, the
  payload-carrying tables where a smuggled diff would live). The hand-seeded scan stays — cheap,
  fast, self-contained — alongside the new real-fixture one, not replaced by it.
- **Task 2** (new file `tests/test_no_state_outside_git.py`): the 40-hex format half of clause
  (i)'s enumeration, against a REAL `scan`→`sequence`→`transform` pipeline run (distinct from
  task 1's resolvability check — a resolvable ref need not be 40 lowercase hex, and vice versa);
  clause (iii)'s sufficiency claim (delete `artifacts/` entirely, re-run `fleet resume`, same
  `SUCCEEDED` set and same `run_digest`); and clause (iii)'s six-trailer claim, the first test in
  this tree to use the SPEC-literal `git interpret-trailers --parse` mechanism (every prior
  trailer test used `git log --format=` instead) and to assert all six `Fleet-*` trailers on the
  same real commit. Both remaining sub-clause mutation proofs (digest-salting for the
  artifacts-deletion test; dropping `Fleet-Attempt` from the trailer mapping for the six-trailer
  test) independently reproduced by task review. **Disclosure (F2, 2026-09-02, response to
  final-review finding F2):** the artifacts-deletion test's fixture only drives to Phase 2, and
  production writes nothing under `artifacts/` before Phase 3 — so the test manufactures its own
  stand-in (`artifacts/logs/stand-in.log`) before deleting it, rather than deleting real
  Phase-3+ output. This was already disclosed in the test's own docstring and the module
  docstring, but not here until now. The property proved — a resume does not need `artifacts/` to
  reproduce its result — does not depend on which phase populated the directory, but the input to
  the test is a construction, not organically-produced real data.
- **Clause (iii)'s second half (F3, 2026-09-02, response to final-review finding F3) —
  pre-existing coverage, uncited in this entry until now.** SPEC clause (iii)'s second half:
  "restoring a database snapshot taken before a phase's commits while leaving the branches intact
  makes `fleet resume` re-derive that phase's outcome from the `Fleet-Task-Id` trailers and add no
  duplicate commits." Covered by `tests/test_transform_e2e.py:459`
  (`test_re_running_transform_over_a_landed_branch_duplicates_nothing`, which uses a
  `crash_the_phase` helper to roll a phase row back to `RUNNING` with branches intact and asserts
  the `Fleet-Task-Id` guard stops re-application) and `tests/test_workers_transform.py:423`
  (`test_forty_of_sixty_land_then_the_deadline_makes_it_partial_and_re_entry_replays_none`,
  credited by the same audit row cited above). Both drive `transform` re-invocation directly, not
  literally `fleet resume` — a distinction worth stating, not hiding.

**A finding surfaced during task 2's work is a pre-existing, already-tracked defect (D91), not a
new gap, and does not block this closure.** `tasks.pre_commit_sha` is never written by any
production code path — `docs/INTEGRATION_HONESTY.md`'s D91 (OPEN, allocated before this round)
already names this exact gap, more precisely than task 2's own first-draft finding, and its own
title already discloses that "§12.45(i)... cannot currently be exercised against a real
production-populated value" for this one column. §12.45(i)'s literal text requires non-NULL
values in the 5 named columns to be correct (resolvable, 40-hex) — it does not require every
column to be populated — so a column that is always NULL in production makes this sub-clause's
real-fixture coverage vacuous FOR THAT COLUMN (task 2's test says so explicitly at the assertion
site, and correctly excludes it from the non-empty requirement it applies to the other 4), without
making the criterion's stated text false. D91 stays OPEN and unrelated-to-§45 fixes remain its own
concern — do not fold its production fix into a future §45 re-visit; §45 needs no further work.
**Done bar:** met in full. Nothing remains open for §12.45.

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

**Partial progress, 2026-09-03 (round VI task 19, commit merging `f4ba7a5`) — still OPEN, this is
a disclosed sub-piece, not a closure.** The base ABC (`src/fleet/ecosystems/contracts/base.py`,
ported faithfully from SPEC §7.6) and the registry mechanism (`register`/`discover`/`for_kind`/
`reset_adapters`, mirroring `ecosystems/base.py`'s established pattern) now exist, plus the first
of five adapters (`proto.py`, confirmed the right first pick: the only `ContractKind` with both a
real identifier-extraction path and existing `contract_bindings` entries in all 5
`EcosystemAdapter`s). `discover()` correctly raises today, naming the 4 still-missing kinds
(`AVRO`, `OPENAPI`, `SHARED_LIB`, `THRIFT`) — this is expected and correct, not a defect. Still not
built: the other 4 adapters, and wiring this registry into the actual BUILD-generation pipeline
(`cli.py`/`bazel/generators.py`/`workers/contracts.py` are all untouched). §12.32/§12.47 both stay
OPEN — neither's `discover()` assertion can pass until the full 5-adapter bijection lands.

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
| DONE | 27 | 1, 3, 4, 5, 6, 7, 10, 12, 13, 15, 16, 17, 18, 20, 21, 24, 26, 28, 32, 33, 35, 40, 42, 44, 45, 46, 48 (re-derived 2026-09-02, round V task 5's own review: §4 rejoins the DONE row for the first time since round II's same-day revert — SPEC's own sentence names "every LLM role" (12), and round V task 5 landed the 12th and final role, `BUILD_AUTHORING`; the reviewer independently re-derived the full 12-role set and the 12×4 cross-product from source before confirming the flip, see §4's own entry for the full account. Prior note, kept for history: §4 was marked DONE in round II this same day and reverted the same day — SPEC's own sentence names "every LLM role" (12), this criterion's own Done bar paraphrase named only "per shipped backend" (4), and only 1 of 12 roles (`REPO_CLASSIFY`) was actually fixtured) — §47 remains OPEN per round T's controller ruling C1, unaffected (its `vars(inst) == {}` claim for the backends registry closed round V, its contracts-registry residual is separate, tracked in §47's own entry via ADR-0065) |
| OPEN — WIRING (cheapest, do first) | 0 | none currently — §27 and §37 were both reclassified NEW-MECHANISM by their own entries (round-K/2026-08-30 correction; each needs a new D-number and new upstream data capture or Phase-3 consumer, not a caller-wiring task) and are now counted in "everything else" below; corrected 2026-09-01, this row was stale since the reclassification landed |
| OPEN — SPEC-ADJUDICATION needed before work starts | 0 | none — row has been empty since round Z |
| OPEN — blocked on an existing D-number, don't duplicate | 4 | 22 (partial, D50 for one sub-clause only — its RSS-sampling piece, NEW-MECHANISM not D50-blocked per round EE research, see §22's own entry for the correction owed), 36, 38 (partial — blocked on D94 only as of round VI task 8, the D101/D102 chain this row tracked across three rounds is now fully landed; D105 is a newly-found separate gap re §38's own reliability question, see that entry), 43 (partial) |
| OPEN — everything else (TEST-ONLY / SCALE-FIXTURE / NEW-MECHANISM) | remainder | 14 (misattributed to D50 until round X — real blocker is §37's `--stub-blocked` stub-creation worker, not a D-number, see §14's own entry), 27, 37, 39 (mis-bucketed as D-number-blocked until round Z research — its own entry names no D-number, only §37's wiring), 41 (all NEW-MECHANISM except 39; §41's own adjudication blocker cleared round W, ADR-0105 — see above), plus all others not listed in a row above — see individual entries (round GG's own final review, 2026-09-02: this row previously still listed `35` after §35 moved to the DONE row above — the two rows contradicted each other; corrected here, `35` removed) |

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
