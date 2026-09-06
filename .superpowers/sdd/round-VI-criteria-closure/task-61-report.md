# Task 61 report — §12.32 second half: `contracts.discover()` bijection test + teardown fix

Status: **DONE**

Worktree self-repair note: this worktree's HEAD was stale (on `agent/roundl-W9`/`fa95469`, a
different round entirely) and could not reach `task-61-brief.md` or `research-34-report.md` via
`git show`/`ls` at that HEAD. Verified `.superpowers/sdd/` is gitignored wholesale
(`.superpowers/sdd/.gitignore`), so the brief and research report are untracked files that live
only in the primary checkout's working tree — not reachable via any commit. Read both directly
from the primary checkout by absolute path (`/home/redmage/swe repo harness/.superpowers/sdd/
round-VI-criteria-closure/{task-61-brief.md,research-34-report.md}`) via the `Read` tool (not
`cd`, which the harness blocks for a worktree-isolated agent). Then self-repaired by creating
`agent/roundvi-task61` from local `main`'s tip `728cb1b` (`git checkout -B agent/roundvi-task61
728cb1b`) — `main` was not directly checkoutable (already used by the primary worktree) but its
tip commit was available locally. Re-verified `728cb1b`'s own log line matches the dispatch
message's expected commit.

## What was changed

All changes confined to `tests/test_ecosystems_contracts_base.py`. **No `src/` file is in the
final diff** (verified: `git status --porcelain` shows only that one test file modified).

1. **Added `test_the_real_contracts_registry_is_a_total_bijection_over_contractkind`** — calls the
   real `fleet.ecosystems.contracts.discover(force=True)` and asserts:
   - `set(registry) == set(ContractKind)` (the criterion's own equality)
   - the five adapter instances are five **distinct** objects (`id()` set size 5)
   - the adapter `name`s equal `{"avro", "openapi", "proto", "shared_lib", "thrift"}`
   Includes a "Why" docstring per Rule 9, modeled on
   `tests/test_ecosystems.py:59-70::test_discover_is_a_total_bijection_over_ecosystem`, explaining
   both the superset (discover()'s own check) and subset (unasserted anywhere in `src/`)
   directions, and why `force=True` is required given the autouse fixture.

2. **Fixed `_clean_registry`'s teardown** — was `reset_adapters(); yield; reset_adapters()`
   (clears, never restores). Now `reset_adapters(); yield; reset_adapters();
   discover(force=True)`, mirroring `tests/test_ecosystems.py:41-51`'s landed precedent for the
   sibling registry. Docstring extended to name the mechanism (plain `discover()` can't re-import
   an already-`sys.modules`-cached adapter module) and the production caller it protects
   (`src/fleet/cli.py`'s `fleet build` PASS 2b). Added `discover` to the `base` import list.

3. **Corrected the stale module docstring** (was lines 7-11) that claimed "the real
   `fleet.ecosystems.contracts` package ships only `proto.py` today ... the real `discover()`
   always raises its own missing-members check, correctly" — false at `HEAD` since round VI task
   40. Corrected **in place with a dated marker** (`[2026-09-06, round VI task 61: corrected —
   ...]`), following the exact convention at `src/fleet/ecosystems/contracts/__init__.py:15-16`,
   and kept the file's still-true reason for fake adapters (mechanism isolation), rewritten to not
   depend on the false premise. **This is the one test-tree site of research-34 §7's five-site
   class** — the other four (`docs/SPEC.md:56-62`, `:5593-5595`, `:5647-5655`, `:6035-6039`) are
   explicitly the controller's, per the brief; not touched here.

## Reproduction — known-bad and post-fix (CLAUDE.md Guardrail 6)

Exact 7-file command from the brief, run with `cwd` inside this worktree (structurally safe per
CLAUDE.md Rule 12's third bullet — `pyproject.toml`'s `pythonpath = ["src"]` +
`tests/conftest.py`'s `sys.path` insert both resolve from the worktree), interpreter
`/home/redmage/swe repo harness/.venv/bin/python` (shared `.venv`, editable-installed against the
*primary* checkout, but pytest's own `pythonpath` ini option overrides that — confirmed by the
traceback path prefix in every run below, which reads under this worktree's own path):

- **Pre-fix (before any edit — read first, per Guardrail 6):** `1 failed, 24 passed` —
  `RuntimeError: no ContractAdapter is registered for ['AVRO', 'OPENAPI', 'PROTO', 'SHARED_LIB',
  'THRIFT']` at `base.py:157`, exactly matching the brief. Reproduced independently of research-34.
- **Post-fix:** `26 passed` (24 pre-existing + the new bijection test + the previously-failing e2e
  test, now green).

## Covering-set files run WHOLE, no `-k` (CLAUDE.md §6)

All run individually (isolated) and together, post-fix:

| File | Individual | In the 7-file/8-file combined run |
|---|---|---|
| `tests/test_ecosystems_contracts_avro.py` | 4 passed | — |
| `tests/test_ecosystems_contracts_base.py` | 5 passed | — |
| `tests/test_ecosystems_contracts_openapi.py` | 4 passed | — |
| `tests/test_ecosystems_contracts_proto.py` | 4 passed | — |
| `tests/test_ecosystems_contracts_shared_lib.py` | 4 passed | — |
| `tests/test_ecosystems_contracts_thrift.py` | 4 passed | — |
| `tests/test_new_language_touchpoints_e2e.py` | 4 passed | — |
| **Brief's exact 7-target command** (6 files + 1 nodeid) | — | 26 passed |
| **All 7 files, whole** (no nodeid narrowing) | — | 29 passed |

Not run: the full `pytest tests/` suite (budget; the brief's exact reproduction command plus the
whole-file covering set is what it asks for). **Scoping stated, not silent**, per CLAUDE.md §6.

## Mutation table (CLAUDE.md Rule 12)

Mutation harness: backup to a per-lane scratch subdir (never `/tmp` directly, never a shared
filename), zero-change gate via `git diff --numstat --no-index BACKUP MUTATED` (against the
backup, not `HEAD`), gate read and reported **before** the test result in every row below.

| # | Mutation | Gate (backup vs mutated) | Test result | Runtime | Blast radius | Verdict |
|---|---|---|---|---|---|---|
| 1 | Delete `@register` from `src/fleet/ecosystems/contracts/thrift.py` | `0→1` line changed (non-zero — genuine) | `test_ecosystems_contracts_base.py`: 1 failed (my new test, direct `RuntimeError` at `discover(force=True)`), 4 passed-at-call/5 errored-at-teardown (the fixture's own restored teardown now also raises — **every** test in the file, because teardown calls `discover(force=True)` unconditionally); 7-file run: `2 failed, 27 passed, 5 errors` — the second failure is `test_new_language_touchpoints_e2e.py`'s e2e test (the real production-caller path, same as the original defect). Sibling adapter files (avro/openapi/proto/shared_lib) unaffected. | 0.14s (single file), 5.55s (7-file) — plausible, not an instant/implausible crash | **Module-scale within `test_ecosystems_contracts_base.py`** (all 5 tests reddened via fixture teardown) **plus** the one real e2e caller test. Disclosed per the brief's explicit ask ("state explicitly which other tests it also reddens"); an all-fail-shaped result here is expected and genuine, not a red flag — every test in the file legitimately depends on the fixture's restored teardown succeeding. | **RED — genuine, discriminating.** Reverted; `git diff --stat` against `HEAD` empty afterwards. |
| 2 (control) | Cosmetic reflow of the new test's final `assert {...} == {...}` set literal (multi-line → 2-line wrap, no semantic change) in `tests/test_ecosystems_contracts_base.py` | `2→5` lines changed (non-zero — genuine edit, purely formatting) | `test_ecosystems_contracts_base.py`: `5 passed` | 0.10s | None — file-local only | **GREEN, as required.** Reverted; file restored to the real (non-cosmetic) edit content, confirmed via `git status --porcelain`. |

Both mutations targeted the actual worktree's own copy of the file (not the primary checkout);
confirmed by reading the traceback path prefix in row 1's failure, which is this worktree's
absolute path, not the primary checkout's. No standalone script was used (only `pytest` runs with
`cwd` inside the worktree), so the `env -i`/`fleet.__file__`-assert pin was not required per Rule
12's third bullet's "structural immunity" — but a direct `python -c "import fleet;
print(fleet.__file__)"` was also run once beforehand and independently confirmed
`/home/redmage/swe repo harness/src/fleet/__init__.py` (the *primary* checkout) for a **plain**
interpreter invocation with no `pythonpath` ini in effect — demonstrating the primary/worktree
ambiguity is real for a bare `python -c`, and that `pytest`'s own `pythonpath` handling is what
resolves it, exactly as CLAUDE.md's third bullet states.

## Pre-existing reds observed (NOT mine — recorded per the brief's instruction)

Not independently re-run at full-suite scale (out of this task's budget), but per the brief and
research-34, these are **not** attributed to this task and were not touched:
- A live §12.6 no-branch regression (`src/fleet/cli.py:8827`/`:8834`/`:9529`) — three
  `tests/test_ecosystems.py` reds. Did not touch `cli.py`; did not touch `tests/test_ecosystems.py`.
- Two `tests/test_build_e2e.py` Go reds + 2 errors needing real Bazel/`gcc`. Not touched, not
  diagnosed.

## Explicitly out of scope, confirmed not done

- No `src/` change in the final diff (mutation-tested `src/` temporarily, then fully reverted).
- No decoy-`ContractKind`-member or duplicate-kind `discover()`-level test added (the existing
  `test_duplicate_kind_registration_raises_naming_both_claimants` already covers the `@register`
  duplicate path per the brief; §12.32's second half obliges only the equality).
- No edit to `docs/SPEC.md`, `docs/CRITERIA_PLAN.md`, `docs/INTEGRATION_HONESTY.md`,
  `docs/PROGRESS.md`, `docs/DECISIONS.md`. No D-number or ADR number allocated.
- No edit to `tests/test_ecosystems.py` (read only, as precedent).
- §12.47 not touched.

## Concerns

None blocking. One note for the controller: the D-number for the teardown defect (research-34's
Gap 1) is still unallocated, as instructed — this report names the defect and its fix but does not
mint a number.

Commits on branch `agent/roundvi-task61`, based on `728cb1b`. Not merged to main.
