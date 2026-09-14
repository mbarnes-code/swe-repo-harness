# Worker report: §15.1 item 3, Wave 5 Batch 16 — mutation-proof llm/backends/{anthropic,bedrock,vertex}.py

## Status: DONE

Branch: `agent/roundviii-mutation-batch16` (from `main` at `c53c6f7`)
Worktree: `/home/redmage/swe repo harness worktrees/wt-roundviii-mutation-batch16` (not merged, not pushed)

## Same-class-bug check (openai_compatible.py's fixed 4xx-misclassification)

Read all three files' error-classification code and compared it against `openai_compatible.py`'s
post-fix `_call` (5xx → `SERVER_ERROR`, else 4xx → a non-`TransportError` `LlmError`/typed error,
`RateLimitError`/429 → its own `RATE_LIMIT` trigger before the generic status check):

- `anthropic.py::_from_status` — already correct: `>= 500` → `SERVER_ERROR`; `RateLimitError` caught
  separately before `APIStatusError`; everything else → `AnthropicBackendError` (not a
  `TransportError`). Matches the fixed pattern exactly, and has full parametrized test coverage
  (`test_failover_triggers_are_classified_per_spec_11_8`, `test_a_4xx_is_not_a_failover_trigger`).
- `bedrock.py::_from_client_error` — already correct: throttle codes → `RATE_LIMIT`, server codes →
  `SERVER_ERROR`, timeout code → `CONNECTION`, unmapped-but-`>=500`-status → `SERVER_ERROR`
  (fallback), everything else → bare `LlmError` (not a `TransportError`). Fully covered by
  `test_transient_faults_become_failover_triggers` (6 cases) and
  `test_a_bad_request_fails_the_task_instead_of_walking_the_tier`.
- `vertex.py::_from_status` — already correct: 429 → `RATE_LIMIT`, `>=500` → `SERVER_ERROR`, else →
  bare `LlmError`. Fully covered by `test_transient_statuses_become_failover_triggers` and
  `test_a_bad_request_fails_the_task_instead_of_walking_the_tier`.

**No production-bug finding.** None of the three carries the unclassified-error defect
`openai_compatible.py` had before this round's fix. All three were already written against the same
5xx/4xx split, and all three already have direct parametrized tests pinning it. This is a
negative finding worth stating plainly since the brief asked for it explicitly, not something
found and left unfixed.

## Coverage-gap discovery method

Ran `pytest --cov=fleet.llm.backends.{anthropic,bedrock,vertex} --cov-report=term-missing` against
the pre-existing test suites (130 passed) to find real uncovered branches rather than guessing from
reading the source. Baseline:

```
anthropic.py   94% (missing 295, 300, 322, 391)
bedrock.py     88% (missing 236->exit, 263, 274-299, 426->429, 482, 546, 582, 602, 674)
vertex.py      85% (missing 239->exit, 289-297, 337-346, 468->478, 523, 551, 569, 640)
```

Cross-referencing missing lines against source showed the same shape in all three files'
`_render`/request-builder: each has TWO guards in the turn list — "no non-system turns at all"
(`if not turns: raise ...`) and "first non-system turn is not user" (`if turns[0]["role"] != "user":
raise ...`). Grepping the existing test files found `bedrock.py` and `vertex.py` each carry
`test_a_conversation_with_no_user_turn_is_refused`, which only exercises the FIRST guard (a
turn list containing only a system message — empty after filtering). Neither file had a test
for the second guard, and `anthropic.py` had a test for **neither** guard (no `_render`
validation test existed there at all — grep for "no_user_turn"/"turns\[0\]" in
`test_llm_backend_anthropic.py` returned nothing).

This is a real, load-bearing, unguarded branch in all three files: a conversation that opens on
`assistant`/`tool` (not empty, so the first check doesn't fire) reaches the vendor transport
unvalidated unless this second check catches it — the difference between a clear, named,
loud config-time-style rejection and either a confusing vendor-side 400 or (worse) a silently
accepted turn order nothing upstream intended.

## Tests added (one per file, same branch, mutation-proofed)

1. `tests/test_llm_backend_anthropic.py::test_a_conversation_whose_first_non_system_turn_is_not_user_is_refused`
   — drives `AnthropicBackend().invoke(...)` via the existing `_invoke` helper with
   `[Message(role="assistant", ...), Message(role="user", ...)]`, asserts `AnthropicBackendError`
   naming `"assistant"`. Placed in the "Request rendering" section, after the CONSTRAINED-rung
   test.
2. `tests/test_llm_backend_bedrock.py::test_a_conversation_whose_first_non_system_turn_is_not_user_is_refused`
   — drives `build_request(...)` directly with the same turn shape, asserts `BedrockTargetMisconfigured`
   naming `"assistant"`. Placed immediately after `test_a_conversation_with_no_user_turn_is_refused`.
3. `tests/test_llm_backend_vertex.py::test_a_conversation_whose_first_non_system_turn_is_not_user_is_refused`
   — drives `build_body(...)` directly with the same turn shape, asserts `VertexTargetMisconfigured`
   naming `"assistant"`. Placed immediately after `test_a_conversation_with_no_user_turn_is_refused`.

## Mutation-proof discipline (Rule 12), per file

For each file: backed up the untouched source to
`/tmp/.../scratchpad/batch16/<file>.py.bak`, mutated the target guard from
`if turns[0]["role"] != "user":` to `if False and turns[0]["role"] != "user":` (a discriminating,
behavior-changing mutation — it disables exactly the guard under test without touching anything
else), verified the diff against the backup was non-empty (`git diff --no-index --numstat`, 1
line changed in each case), ran the new test and confirmed it failed (`DID NOT RAISE
<ErrorType>` in all three), restored the file from the backup, verified the restored file is
byte-identical to the backup (`diff` exit 0, "IDENTICAL after restore"), and re-ran the full
per-file test suite to confirm all-green:

| File | Mutation result | Restore verified | Full suite after restore |
|---|---|---|---|
| `anthropic.py` | RED (`DID NOT RAISE AnthropicBackendError`) | identical | 40 passed |
| `bedrock.py` | RED (`DID NOT RAISE BedrockTargetMisconfigured`) | identical | 46 passed |
| `vertex.py` | RED (`DID NOT RAISE VertexTargetMisconfigured`) | identical | 47 passed |

Old-passes/new-fails shape: the OLD test suite (without the new test) passes on both the
original and the mutated code for all three files — the mutation only reddens the NEW test,
which is what makes it a discriminating mutation for that specific test rather than a module-wide
outage. Confirmed by construction: the mutation touches only the guard the new test targets, and
the "run test (expect fail)" step above ran only the new test (`-k first_non_system`), which
failed cleanly with an assertion message (not a collection error or an unrelated crash) —
ruling out the "all-fail is a module outage" and "unimported mutation" blind spots Rule 12 calls
out.

## Coverage after

```
anthropic.py   96% (missing 295, 322, 391 — unrelated: no-turns-at-all guard, no-schema-at-rung
                     guard, and the tool-arguments-not-a-dict MalformedReply guard)
bedrock.py     89% (missing 236->exit, 263, 274-299 [live boto3 call, needs a socket],
                     426->429, 482, 582, 602, 674 [compile-time-only _protocol_conformance])
vertex.py      86% (missing 239->exit, 289-297/337-346 [live google.auth/requests call, needs a
                     socket], 468->478, 551, 569, 640 [compile-time-only _protocol_conformance])
```

133 of 133 tests pass across the three suites combined (up from 130). `ruff check` on all three
touched test files: clean. `mypy` on all three touched test files: 7 pre-existing
`unused-ignore` errors in `test_llm_backend_bedrock.py`/`test_llm_backend_vertex.py`, confirmed
identical (same count, same kind, only line numbers shifted) on `main` via `git stash` — not
introduced by this change.

## Files changed

- `tests/test_llm_backend_anthropic.py` (+22 lines, 1 new test)
- `tests/test_llm_backend_bedrock.py` (+18 lines, 1 new test)
- `tests/test_llm_backend_vertex.py` (+18 lines, 1 new test)

No production code touched — this was a coverage task per the brief, not a fix task, and no fix
was needed (no bug found).
