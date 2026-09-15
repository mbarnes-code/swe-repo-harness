# Batch 43 report — cli.py build/verify plan & evidence dataclasses (G6a)

**Status:** DONE
**Branch:** `agent/roundviii-mutation-batch43` from `main` (`2d52387`)
**Commit:** (see final commit at end of this report / `git log -1`)

## Scope (group G6a, per brief)

`_RepoFacts`, `_BuildIngest`, `_ContractIngest`, `_BuildPlan`, `_VerifyPlan`, `_BuildEvidence`,
`_VerifyEvidence`, `_AttemptWriter`/`_BuildSink`/`_VerifySink` — excluding their already-proven
`llm_cache_hit`-attribution clauses (covered by `tests/test_llm_cache_hit_attribution.py` M8/M15/
M16) and excluding `_repo_facts` (the free function; out of scope per the brief).

## Per-item results

### `_RepoFacts` — NEW TEST
Frozen, slotted dataclass with no methods/validators — the only behaviour is its field
defaults. No existing test constructed `_RepoFacts` at all; production code relies on the exact
fallback `_RepoFacts(None, Ecosystem.UNKNOWN)` at six call sites in `cli.py`
(`facts.get(repo_id, _RepoFacts(None, Ecosystem.UNKNOWN))`), whose defaulted `baseline_ok: bool |
None = None` and `baseline_test_count: int = 0` are documented (docstring) as meaning "never
measured" — a distinct state from `False`/"measured and failed". Added
`test_repo_facts_fallback_construction_defaults_to_unknown_baseline`, which constructs exactly
that fallback shape and asserts on the defaults.
**Mutation:** `baseline_ok: bool | None = None` → `= False`. RED (assertion failure), confirmed,
then restored and re-verified GREEN.

### `_BuildIngest`, `_ContractIngest` — NO NEW TEST (N/A, disclosed)
Frozen, slotted dataclasses with **no default values at all** (every field required) and no
methods/validators/properties. There is no expressible branch or defaultable behaviour to
mutation-test in isolation — Python's dataclass machinery already guarantees "constructing with
args stores them" by language semantics, not by any logic this module wrote. Per Rule 12's "audit
mutations for expressibility," writing a test that only re-asserts constructor
assignment would not discriminate any real defect, so none was added. Their actual wiring
(ingest path, `already_ingested` semantics) lives in the `_build_impl`/ingest machinery, out of
this batch's explicit scope.

### `_BuildPlan`, `_VerifyPlan` — NO NEW TEST (N/A, disclosed)
`_VerifyPlan` has no defaults at all (same reasoning as above). `_BuildPlan` has exactly one
defaulted field, `gazelle_files: tuple[SupportFile, ...] = ()`. Checked whether this default is a
genuine gap: it is not — the empty-default case is exercised incidentally by essentially every
`tests/test_build_e2e.py` fixture that constructs a `_BuildPlan` and omits the field (e.g. the
`_root_file_plan` helper), and the non-default (populated) case is exercised by the real Gazelle
generator e2e tests that `replace(plan, gazelle_files=build_files)` and flow the result through
`plan.support_files`. Both branches of this one default are already covered by broad,
pre-existing e2e assertions; no isolable, currently-untested behaviour remains.

### `_BuildEvidence`, `_VerifyEvidence` — NEW TEST
Both classes have exactly one method, `record(output) -> None`, doing
`self.by_repo[output.repo_id] = output` — a keyed **overwrite**, not append/keep-first. This is
load-bearing: `_build_criterion` (§3.3's success check) and the PR/report assembly paths read
`evidence.by_repo.get(repo_id)` *after* a wave completes, expecting the LATEST outcome for a repo
dispatched more than once (a retried BUILD, or a Phase-4 re-verify on a fresh tip). No existing
test ever called `.record()` twice for the same `repo_id`. Added
`test_build_and_verify_evidence_record_overwrites_by_repo_id_not_accumulates`, covering both
classes in one test.
**Mutation:** `self.by_repo[output.repo_id] = output` → `self.by_repo.setdefault(output.repo_id,
output)` in both `record` methods (`cli.py:8982`, `8990`). RED (assertion failure), confirmed,
then restored and re-verified GREEN.

### `_AttemptWriter` (excluding llm_cache_hit clauses) — NEW TEST
Picked `_next_ordinal`'s `COALESCE(MAX(retry_ordinal) + 1, 0)` — the value that lets a genuinely
re-executed identical command (same run/repo/phase/attempt/command_sha256) land a NEW `attempts`
row instead of colliding with the `UNIQUE (run_id, repo_id, phase, attempt, revalidation_round,
tier, command_sha256, approach_signature, retry_ordinal)` constraint (`schema.sql`). No existing
test calls `record()` twice with the identical command for the same attempt to observe the VALUE
this computes (every cache-hit/failover-attribution fixture varies the command or attempt number
across calls). Added
`test_attempt_writer_increments_retry_ordinal_for_a_repeated_identical_command`.
**Mutation:** `COALESCE(MAX(retry_ordinal) + 1, 0)` → `COALESCE(MIN(retry_ordinal), 0)`
(`cli.py:9109`). RED — the second `record()` call's row failed to persist (its `attempt_id` never
appears in the read-back, surfacing as a `KeyError` in the test rather than a clean assertion
diff, since the underlying write hit the real `UNIQUE` constraint). Confirmed genuine (real
failure, not a false green), then restored and re-verified GREEN.

### `_BuildSink` (excluding llm_cache_hit / already-covered branches) — NEW TEST
Picked the `if not output.published_sha: return` guard ahead of the final `UPDATE phases SET
post_commit_sha = ...` write. The TRUE branch (something publishes) is already covered
end-to-end by real `fleet build`/`fleet verify` runs (e.g.
`tests/test_reentry_evidence.py`'s real post-BUILD `post_commit_sha` checks); the FALSE branch
(early return, nothing published) is exercised by
`test_a_never_measured_migrated_test_count_writes_nothing_to_repos` but that test never seeds or
re-reads `phases.post_commit_sha`, so an unguarded/inverted write would still leave it green (an
UPDATE with `params=("", ...)` on a freshly-created row with NULL `post_commit_sha` reads back
indistinguishable from "never written"). Added
`test_build_sink_leaves_post_commit_sha_untouched_when_nothing_published`, which seeds a REAL
prior `post_commit_sha` value first so an unguarded write is visible as a silent overwrite.
**Mutation:** `if not output.published_sha:` → `if False:` (`cli.py:9231`). RED (assertion
failure: preexisting sha overwritten with `""`), confirmed, then restored and re-verified GREEN.

### `_VerifySink` (excluding llm_cache_hit clauses) — NEW TEST
Picked `if output.report is not None: await _record_verification(...)`. No existing test drove
`_VerifySink` with a real `VerificationReport` attached and checked the `findings` row lands:
`test_the_build_and_verify_sinks_forward_the_flag_they_were_billed_on`
(`test_llm_cache_hit_attribution.py`) drives `_VerifySink` but its `VerifyOutput.report` is
`None` by construction (that test exists only to prove the already-excluded llm_cache_hit
clause); the D131 PR-gate test in `test_cli.py` calls `_record_verification` **directly**,
bypassing `_VerifySink` entirely. Added
`test_verify_sink_persists_the_report_when_the_dispatch_produced_one`.
**Mutation:** `if output.report is not None:` → `if output.report is None:` (`cli.py:9300`). RED
(assertion failure: no `findings` row landed) — this same mutation also crashed the pre-existing
`test_the_build_and_verify_sinks_forward_the_flag_they_were_billed_on` (calling
`_record_verification` with `report=None` raises `AttributeError` on `.model_dump`), so the
mutation is caught by two independent tests. Confirmed, then restored and re-verified GREEN.

## Mutation-proof workflow (Rule 12)

For every mutation above: backed up `src/fleet/cli.py` to `/tmp/wt43-cli-backup.py`, edited the
one target line with `sed`, ran `diff -u` against the backup to confirm a non-empty, single-line
change (never a silent no-op), ran the targeted new test and confirmed RED, restored via `cp` from
the backup, confirmed `diff -q` reports no difference (exact restoration), and re-ran the same
test to confirm GREEN again. `src/fleet/cli.py` is unmodified relative to `main` in the final
commit — this batch is test-only.

## Which test file(s) were run, and why they're a sufficient covering set

- **`tests/test_cli.py`** (full file, 245 tests, all pass) — the file all five new tests live in,
  and the file that already carries the bulk of direct unit coverage for
  `_AttemptWriter`/`_BuildSink`/`_BuildEvidence` (e.g. the migrated-test-count pair) and for
  `VerifyOutput`/`_RepoFacts`'s production consumers.
- **`tests/test_llm_cache_hit_attribution.py`** (6 tests, all pass) — the file that owns the
  excluded llm_cache_hit-attribution clauses on `_AttemptWriter`/`_BuildSink`/`_VerifySink`;
  re-run to confirm this batch's new tests and the one source-adjacent mutation trial
  (`_VerifySink`'s `report is not None` inversion) didn't regress or double-count that coverage.
- **`tests/test_llm_backend_failover_attribution.py`** (9 tests, all pass) — the sibling
  attribution file, also constructs `_VerifySink`/`_AttemptWriter` directly; run for the same
  reason.
- **`tests/test_wave_composition_projects_mid_wave.py`** (6 tests, all pass) and
  **`tests/test_d89_phase1_task_lifecycle.py`** (4 tests, all pass) — the two remaining files
  that name `_BuildSink`/`_VerifySink`/`_AttemptWriter` outside `test_cli.py`/the attribution
  files (found via `grep -rl _VerifySink tests/*.py` and `grep -rl _AttemptWriter`), driving them
  through the real wave/task-lifecycle wiring rather than directly.
- **`tests/test_build_e2e.py`** (real-Bazel e2e suite; this is the file that constructs
  `_BuildPlan` directly, e.g. `_root_file_plan`, and drives `_BuildSink`/`_RepoFacts` end to end
  through real `fleet build`/`fleet verify` runs) — run in full; result appended below.

This set covers every reaching test file the brief named
(`tests/test_build_e2e.py`, `tests/test_cli.py`, `tests/test_llm_cache_hit_attribution.py`) plus
every other file a `grep -rl` for each of the nine in-scope names actually turned up
(`test_llm_backend_failover_attribution.py`, `test_wave_composition_projects_mid_wave.py`,
`test_d89_phase1_task_lifecycle.py`), so nothing that touches G6a's classes was left unrun.

## `tests/test_build_e2e.py` result

`90 passed in 833.53s (0:13:53)`. Clean `bazel disk` line: `peak 4.18 GiB (ceiling 6 GiB) ·
residual output bases 0 bytes · repository cache kept 1124 MiB`. `0 tests skipped this session —
full collected coverage ran`.

## mypy / ruff

- `ruff check tests/test_cli.py` — all checks passed (after fixing one `F821` — a missing
  inline `import aiosqlite` in the new `_BuildSink` test — and one `I001` import-order nit in the
  new `_VerifySink` test, both introduced by this batch's own edits).
- `mypy tests/test_cli.py` — 58 errors, byte-identical in count and message to `mypy` run against
  `main`'s copy of the same file (verified by diffing a `git show main:tests/test_cli.py` copy
  against the working tree copy under `mypy`) — this batch introduces zero new mypy errors.
- `src/fleet/cli.py` is untouched (byte-identical to `main`) in the final commit; only
  `tests/test_cli.py` was edited.
