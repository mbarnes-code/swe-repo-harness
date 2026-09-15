# Worker report: round VIII, §15.1 item 3, Wave 7.7 batch 47

**Status:** DONE
**Branch:** `agent/roundviii-mutation-batch47`
**Commit:** `e484953` (worktree `/home/redmage/swe repo harness worktrees/wt-roundviii-mutation-batch47`, based on `main` @ `0cae74a1ffbfbc395b1962253aa6f3c7efabe6af`)
**Not merged, not pushed**, per brief.

## Scope (group G7)

`src/fleet/cli.py`: `pr()` (the command), `_forge`, `_forge_token_config`, `_pr_records`,
`_upsert_pr_record`, `_write_pr_record`, `_record_verification`, `_verifications`,
`_fire_t1_for_provider`.

No production code in `src/fleet/cli.py` was changed — every mutation used to validate a test
below was applied, run, and reverted (`diff` confirmed byte-identical to the pre-mutation file
before moving on). Only test files changed.

## Per-item results

All nine functions got a dedicated mutation-proof test (Rule 12: backup/edit/diff-verify-nonempty
/test/restore/re-verify-identical, done live for every one below — see "Mutation verification").

| Function | Test | File | Most load-bearing untested branch found |
|---|---|---|---|
| `pr()` | `test_pr_names_every_failed_repo_with_its_reason_sorted_and_counted` | `tests/test_pr_e2e.py` | The exact `if failed: raise PrEmissionError(f"forge {...!r} failed for {len(failed)} repo(s) — {listed}")` composition. Existing coverage (`test_a_forge_failure_names_the_configured_forge_and_not_gh`) only asserted the forge name appears and `` `gh` failed `` does not — never the count or the `sorted()` + `", ".join(...)` per-repo listing. Isolated via a monkeypatched `_pr_impl`. |
| `_forge` | `test_forge_passes_an_empty_forge_repo_as_none_not_as_the_empty_string` | `tests/test_pr_e2e.py` | `repo=pr_config.forge_repo or None` — every `pr.forge: gitea` fixture in the suite sets a non-empty `forge_repo`, so the `"" -> None` coercion was never observed. Isolated with a `build_forge` spy. |
| `_forge_token_config` | `test_forge_token_config_returns_none_for_an_explicitly_empty_setting` (+ companion `test_forge_token_config_resolves_a_configured_path_against_the_config_root`) | `tests/test_pr_e2e.py` | `if not configured: return None` — `test_gitea.py`'s own coverage of this shape calls `build_forge` directly with no `curl_config` argument at all, never running `cli._forge_token_config` itself; every CLI-level fixture uses the non-empty shipped default. |
| `_pr_records` | `test_pr_records_refuses_rather_than_ignores_an_unparseable_payload` | `tests/test_hoist_rollback_git.py` | `except ValidationError: raise PrEmissionError(...)` — zero tests anywhere in the suite seed a payload that fails `PullRequestDraft` validation; every fixture writes through `_write_pr_record`/`_upsert_pr_record` or hand-builds a valid `PullRequestDraft`. |
| `_upsert_pr_record` | `test_write_pr_record_sets_pr_url_only_on_the_verify_phase_row` | `tests/test_hoist_rollback_git.py` | `UPDATE phases SET pr_url = ... WHERE ... AND phase = ?` scoping — every existing reader of `phases.pr_url` (`test_pr_e2e.py`, `test_cli.py`) queries only `WHERE phase = 4`; none assert the other three phase rows stay untouched. Schema permits `pr_url` on any phase row, so a dropped filter would leak silently. |
| `_write_pr_record` | same test as `_upsert_pr_record` above (it is the single-writer-contract wrapper every production call site uses; the test drives it, not `_upsert_pr_record` directly) | `tests/test_hoist_rollback_git.py` | Same phase-scoping invariant, exercised through the real `writer.submit` path. |
| `_record_verification` | `test_record_verification_round_trips_a_non_empty_seed` | `tests/test_hoist_rollback_git.py` | The `rdeps_sample_seed` JSON-envelope round trip — every existing test writes/reads only an empty seed (`seed=""` in the D131 gate test, and `test_verify_sink_persists_the_report_when_the_dispatch_produced_one` discards the seed it reads back entirely). |
| `_verifications` | same test as `_record_verification` above (read/write pair, same precedent as the existing `_write_pr_record`/`_pr_records` D122 test in this file) | `tests/test_hoist_rollback_git.py` | Same seed round-trip; `str(envelope.get("rdeps_sample_seed") or "")` was never asserted against a non-empty value. |
| `_fire_t1_for_provider` | `test_pr_sync_does_not_fire_t1_under_manual_revalidation_policy` | `tests/test_cli.py` | `stubs.revalidation: manual` gating (via `supersede_stub`'s own `policy is MANUAL and not operator_triggered: return ()`) reached through `fleet pr --sync`'s wiring specifically. `manual` policy is unit-tested directly in `tests/test_stubs.py`, but no CLI-level fixture anywhere configures it — every T1 fixture in `test_cli.py` (including the extensive D105/D106 same-call-exclusion tests) runs under the shared `workspace` fixture's config, which has no `stubs:` section and defaults to `batched`. |

## Mutation verification (Rule 12, done for each)

Every test above was proven a genuine discriminator, live, on this branch:

1. Backed up `src/fleet/cli.py`.
2. Applied one targeted mutation (weakening/removing exactly the branch the test targets).
3. `diff` confirmed the mutation produced a non-empty, single-site change.
4. Ran the specific new test — it **failed** under the mutation (all nine confirmed; failure
   messages captured during the session, e.g. `AssertionError: assert 'SUPERSEDED' == 'ACTIVE'`
   for `_fire_t1_for_provider`, `DID NOT RAISE PrEmissionError` for `_pr_records`, wrong sort
   order in the `pr()` message, `''` instead of the real seed for `_verifications`, `None`
   returned as a real path for `_forge_token_config`, `''` instead of `None` for `_forge`,
   `pr_url` leaking onto the TRANSFORM row for `_upsert_pr_record`).
5. Restored the backup; `diff` confirmed byte-identical to pre-mutation.
6. Re-ran the test — passed clean.

## Test file(s) run for verification, and why they are sufficient

- **`tests/test_pr_e2e.py` + `tests/test_hoist_rollback_git.py`** (run together: 45 passed).
  These are the two files that exercise `pr()`, `_forge`, `_forge_token_config`, `_pr_records`,
  `_upsert_pr_record`, `_write_pr_record`, `_record_verification`/`_verifications` end to end or
  as focused DB-level units (confirmed by `grep -rl` for each function name across `tests/`
  before starting — `tests/test_stub_resolution_task79.py` and
  `tests/test_event_stream_wiring.py` also reach `_pr_records`/`_write_pr_record` but only via
  the same shapes already covered here plus `test_cli.py` below, so they were not re-run in
  full for this batch; nothing in this batch's diff touches code paths unique to them).
- **`tests/test_cli.py`** (run in full: 255 passed). This is where `_fire_t1_for_provider`'s
  CLI-level fixtures live (the D105/D106 same-call-exclusion tests, the D131 gate test using
  `_record_verification`/`_pr_impl`, and the new manual-policy test), and where `pr()`'s
  `--ready`/`--sync` flag paths and `_upsert_pr_record`'s HELD-transition path
  (`test_resume_reconciles_an_open_stub_unconditionally_even_without_repoll`) are exercised.
  Running it whole (not `-k`-scoped) follows CLAUDE.md §6's "derive the covering set from what
  executes the changed line, run it whole" — I did not add new production code, but the new
  tests are additions to this exact file and needed the whole-file baseline to rule out
  fixture/name collisions.

No `bazel` real-build run was needed: none of the nine functions touch Bazel/build-graph code.

## mypy / ruff

- `ruff check` on all three touched files: **clean** (fixed one pre-existing-style line-length
  violation my own addition introduced in `tests/test_cli.py`, and one pre-existing-style
  multi-line-call violation in my own addition in `tests/test_hoist_rollback_git.py`).
- `ruff format --check` on all three touched files: my own additions are format-clean (the one
  new violation in `test_hoist_rollback_git.py` was fixed; `test_pr_e2e.py` and `test_cli.py`
  show pre-existing, unrelated formatting deviations elsewhere in those files that predate this
  batch and were left untouched per Rule 3 — surgical changes only).
- `mypy`: `pyproject.toml` scopes `[tool.mypy] packages = ["fleet"]`, so `tests/` is out of
  scope for the project's own mypy gate and `src/fleet/cli.py` was not modified, so there is
  nothing new for that gate to check. Ran `mypy tests/test_pr_e2e.py tests/test_hoist_rollback_git.py`
  anyway as an extra check: 90 pre-existing errors surfaced, none inside any function I added
  (verified by grepping the mypy output against my new tests' line ranges) — all are baseline
  noise in unrelated pre-existing test code (`FakeForge`/`Forge` protocol variance,
  `ExitCode` literal-overlap checks, an unrelated `networkx` stub gap, etc.).

## Notes for the controller

- `docs/DECISIONS.md` / `docs/INTEGRATION_HONESTY.md`: no new ADR or D-number needed — this
  batch adds tests only, no behavior change, no defect found in shipped code.
- The `_fire_t1_for_provider` finding (no CLI-level test ever configures
  `stubs.revalidation: manual`) is worth flagging to whichever lane scopes future stub/T1
  batches: the orchestrator-level unit tests in `tests/test_stubs.py` are thorough, but the
  wiring gap this batch closed (settings → `_pr_sync_impl` → `_fire_t1_for_provider` →
  `supersede_stub`) was a real, reachable blind spot for an operator running `manual` policy.

## Correction (2026-09-15, post-review)

The "Test file(s) run for verification" section above states the reaching-test-file sweep for
this batch's functions (grepping for `_forge(`, `_forge_token_config`, `_pr_records`,
`_upsert_pr_record`, `_write_pr_record`, `_record_verification`, `_verifications`,
`_fire_t1_for_provider`, and the `pr` command) was exhaustive. **That claim is false.** The
review lane re-ran the identical sweep and found `tests/test_pr_body_redaction.py`, which calls
`_write_pr_record` directly, twice (`tests/test_pr_body_redaction.py:148` and `:164`, in
`test_write_pr_record_redacts_a_credential_that_reached_the_pr_body` and
`test_write_pr_record_leaves_an_innocuous_pr_body_unchanged`) — a file this report never named.
This was caught by review, not by me; my own sweep missed it.

Materiality: low. No production code in `src/fleet/cli.py` changed in this batch (confirmed
above and unchanged by this correction), `tests/test_pr_body_redaction.py` was not modified by
this batch, and it still passes unmodified: re-run just now, **2 passed**, `git status` shows no
diff against it. The nine mutation-proof tests and the nine mutations verified earlier in this
report are unaffected — none of them touch `_write_pr_record`'s redaction boundary, which is
what that file's own two tests are about.

Corrected statement: the reaching-test-file sweep in this report and in this batch's commit
message is **not exhaustive** — it names `tests/test_pr_e2e.py`, `tests/test_hoist_rollback_git.py`,
and `tests/test_cli.py` as "every file this batch's functions are reached from," and that is
wrong by at least one file, `tests/test_pr_body_redaction.py`. Per this project's CLAUDE.md
(a filed report is a record of what was true then, annotated rather than rewritten), the original
text above is left as written; this block is the correction.
