# SDD backlog A — close the verified defects from the reference-evaluation rounds

Round name deliberately not "38": `research-38.md`/`review-38.md` belong to another worker.

## Global Constraints
- `mypy --strict` and `ruff` clean. Match surrounding style; read neighbours first.
- Rule 9: tests verify WHY the logic matters, not shape.
- Rule 3 surgical: touch only your lane's files. Another agent owns every other file.
- **Never** export any `FLEET_*` env var (`env_prefix="FLEET_"` + `extra="forbid"` → exit 2).
- **Never** run the full suite (~9 min) and never two pytest sessions at once. Run only the
  test files covering your change.
- Commit your own lane with a message explaining WHY, not what.

## Lanes (disjoint — enforced)
- Lane A: `src/fleet/workers/rewrite.py` + `tests/test_workers_rewrite.py`
- Lane B: `src/fleet/cli.py` + `tests/test_cli*.py`
- Lane C: `docs/INTEGRATION_HONESTY.md` only

## Task A1 — D49: wire the dead patch cap and gate the model-repair path
Lane A. `check_diff` has one call site (`workers/rewrite.py:332`, deterministic branch) and
omits `max_bytes`, so `transform.max_patch_bytes` (`settings.py:452`) is enforced nowhere while
`docs/SPEC.md:6059`, `:6634` and `:7056` assert it is. The LLM branch (`rewrite.py:397-410`)
calls `land_patches` with no diff check and no parse probe at all.
Required: pass `max_bytes` from settings at the existing deterministic call site, AND apply
`check_diff` to `repair.patches` on the LLM branch before `land_patches`, rejecting the same way
the deterministic branch does. Do NOT add a `max_files_touched`-style cap: a legitimate migration
rewrite may touch every file in a repo. Tests must pin (a) an oversize patch is rejected with the
message naming `transform.max_patch_bytes`, (b) a model patch outside `dest_path` is rejected
before landing.

## Task B1 — rules_dir fail-open is a silent capability downgrade
Lane B. `cli.py:3688-3690` returns `()` when the rules directory is absent, so a deleted or
mistyped `config/rules/` silently downgrades every migration to rename-only across ~250 repos.
Required: fail loud. Raise the project's existing config error type naming the resolved path,
matching how `cli.py`'s other exit-2 refusals read. Test pins that an absent rules dir raises
rather than returning empty.

## Task C1 — ledger entry for the 26 inert config keys
Lane C. `tests/test_config_keys_are_read.py` (committed at 9644406) lists 26 config keys no code
reads, each with `settings.py:NNN` evidence, and says "ledger entry pending" 26 times. Write that
entry. Highest-consequence: `llm_policy` appears at exactly two lines in all of `src/`
(`orchestrator/context.py:140`, `:152`) and is never assigned, so `llm.failover`, `llm.rate_limit`
and `llm.max_schema_repairs` are inert — and their defaults COINCIDE with `CallPolicy()`'s, so it
misbehaves only once an operator edits them, i.e. exactly when they are being throttled.
Note the asymmetry: `cli.py` already refuses `--context-policy` for this exact defect at the flag
layer while the config key behind the same missing code is silently accepted.
