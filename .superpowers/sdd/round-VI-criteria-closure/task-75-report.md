# Task 75 report — D122: a contract's own PR record coexists with its owning repo's PR record

**Status: DONE**

**Branch:** `agent/roundvi-task75`, from `main` @ `aa3bcae` (a preliminary docs-only commit
landing ADR-0126 itself — see "Setup" below). Commits:
- `aa3bcae` (on `main`, not this branch — see "Setup"): docs: land ADR-0126.
- `a9636b8`: feat — the code fix (fingerprint widening, `_pr_records` key widening, every
  mypy-flagged call site, the Rule-12 discriminator test, and the four test-side fingerprint
  helpers that needed updating).
- `6e8746a`: docs — D122's status heading flipped to `FIXED, LANDED` with a dated paragraph
  appended.

## Setup note (not part of the task, disclosed for completeness)

When I read the brief, `docs/DECISIONS.md` (ADR-0126) and the D122 adjudication paragraph in
`docs/INTEGRATION_HONESTY.md` already existed as **uncommitted working-tree changes** in the
primary checkout, not yet committed to `main` — the brief describes ADR-0126 as "already landed,"
but `git log` showed the last commit was `683cde4` (ADR-0125), one before. Since the content was
byte-identical to what the brief and ADR-0126 text describe, and following this project's
established pattern of a standalone `docs: land ADR-N` commit (e.g. `683cde4`), I committed it to
`main` as `aa3bcae` before branching, rather than silently picking a different base or leaving it
uncommitted. My branch is cut from `aa3bcae`.

## What I built

Exactly ADR-0126's design, no re-derivation:

1. **`PullRequestDraft.contract_id`** already existed (`src/fleet/models/tasks.py:476-480`,
   `str | None`, `default=None`, ADR-0019) — confirmed before assuming, per the brief's own
   instruction. No model change needed.

2. **`_fingerprint`'s signature vs. a dedicated helper (step 2 of the brief).** Checked every
   other call site first (11 call sites across `cli.py`): `_fingerprint`'s signature is already
   `def _fingerprint(*parts: str) -> str` (variadic, `src/fleet/cli.py:815`), so it already
   accepts any arity — **no signature change and no dedicated helper were needed**. I changed
   `_upsert_pr_record`'s call site directly:
   `_fingerprint(run_id, draft.repo_id, PR_RECORD_KIND, draft.contract_id or "")`.

3. **`_pr_records`'s key widened** from `dict[str, PullRequestDraft]` (keyed `repo_id`) to
   `dict[tuple[str, str | None], PullRequestDraft]` (keyed `(repo_id, contract_id)`), reading
   `draft.contract_id` off the already-parsed payload rather than adding a new SQL column.

4. **Every call site `mypy --strict src/fleet/` flagged** (20 errors → 0, iteratively) — all
   mechanically `(x, None)` for a repo-owned lookup, since no production call site sets
   `contract_id`:
   - `execute_hoist_rollback`'s blocking-descendant scan (two `records.get(...)` calls,
     `cli.py` ~4204/4212)
   - `_ordered_revert_shas`'s blast-set loop (`cli.py` ~4399) — plus its docstring's and its
     dedup comment's stale "`_pr_records` is keyed by `repo_id`" claims, corrected in place
     (Guardrail 7) rather than left to rot; the dedup comment also now discloses that the
     `owner_repo_id` skip is a preserved-behavior mechanical migration, not a redesign, and that
     a future contract-PR-dispatch task must revisit it if it ever adds a real second draft there
   - `_pr_sync_impl`'s `pollable` dict comprehension, its two `sorted(...)` iteration loops (fixed
     to sort on `(repo_id, contract_id or "")` rather than a bare tuple compare, since `None`
     vs. `str` at a shared `repo_id` would raise), and — **the one defect mypy could NOT catch**
     — its returned `"polled"`/`"terminal"` JSON lists (see "Defect caught only by the full test
     run" below)
   - `_pr_impl`'s eligibility/promotion lookups (`existing_urls`, `records.get(min(member_ids))`,
     the `blocking` dependency check)
   - `_emit_prs`/`_promote_prs`/`_emit_one_pr`/`_regenerate_pr_body`'s threaded
     `Mapping[str, PullRequestDraft]` parameter → `Mapping[tuple[str, str | None],
     PullRequestDraft]`, and their internal `records[dep]`/`dep in records` usages
   - `_stub_reconcile_inputs`'s provider-facts `pr_state`/`pr_created_at` lookup
   - `_apply_stub_reconcile`'s consumer-HELD `pr_records.get(consumer_repo_id)` update

   Final `mypy --strict src/fleet/`: **0 errors, 129 source files, "Success: no issues found."**

5. **Defect caught only by the full test run, not by mypy** (the return dict is typed loosely as
   `dict[str, object]`): `_pr_sync_impl` built its JSON-facing `"polled"`/`"terminal"` lists via
   `sorted(pollable)` and `sorted(set(records) - set(pollable))` — once the dict's key type
   changed, these silently started sorting the raw `(repo_id, contract_id)` tuples instead of
   repo_id strings, and `"polled"`/`"terminal"` leaked `[repo_id, None]` pairs into a report where
   every sibling list (`"merged"`/`"closed"`/`"unchanged"`) is a flat list of strings. This broke
   `tests/test_pr_e2e.py::test_pr_sync_sweeps_a_pre_merged_providers_stub_left_active_by_a_prior_
   crash` (`ingested["terminal"]` held `[['acme-lib-py', None]]` instead of `['acme-lib-py']`).
   Fixed by projecting both sets onto the `repo_id` component before building those two lists.
   This is exactly Guardrail 6's "state exactly what you ran" point in miniature: `mypy --strict`
   clean is necessary but was not sufficient here, because the return type annotation
   (`dict[str, object]`) doesn't propagate through to catch a value-shape regression — only the
   full-file test run surfaced it.

6. **Out of scope, respected:** no new call site constructs a `PullRequestDraft` with
   `contract_id` set. No schema/index migration (none was needed — confirmed, not assumed).

## Rule-12 old-fails/new-passes discriminator (brief step 4)

`tests/test_hoist_rollback_git.py::test_a_contracts_own_pr_record_coexists_with_its_owning_repos_
own_pr_record` (new test): constructs two `PullRequestDraft`s sharing `repo_id=OWNER_REPO` — one
`contract_id=None`, one `contract_id=CONTRACT_ID` — writes both through `_write_pr_record`
(which calls `_upsert_pr_record`), and reads back through `_pr_records`, asserting both are
present, keyed `(OWNER_REPO, None)` and `(OWNER_REPO, CONTRACT_ID)` respectively.

**Old-fails, measured directly** (not merely asserted): backed up the fixed `src/fleet/cli.py` to
a scratch file, reverted `src/fleet/cli.py` to `HEAD` (`aa3bcae`, pre-fix) with `git diff HEAD --
src/fleet/cli.py` confirmed empty and the file confirmed to genuinely differ from the backup, then
ran the new test:

```
E   AssertionError: both the owning repo's own PR record and its contract's own PR record must
    persist as TWO separate rows, not one overwriting the other -- got
    {'owner-repo': PullRequestDraft(..., contract_id='proto:demo', ...,
    body="the hoisted contract's OWN migration PR, attributed to its owning repo", ...)}
E   assert 1 == 2
1 failed, 12 deselected
```

Exactly the predicted old-code symptom: `_pr_records` returns **one** entry, keyed by the bare
`repo_id`, holding the **second** write's draft (the contract's) — the first (owner's own) was
silently overwritten via `findings`' own `ux_findings_ident` unique index, precisely as D122
described.

**New-passes**, fix restored: `1 passed, 12 deselected`.

**Gate-discipline note on how "old" was reverted.** I initially reverted via `git stash push --
src/fleet/cli.py` / `git stash pop`, and the `pop` returned a **different, concurrent lane's**
stash entry (`agent/roundvi-task76`'s own "old-fails-check" push, confirmed via
`git cat-file -p` on the popped commit SHA `978d3d2445a60bc9cd52d43198c45cbae7a2fc93`, message "On
agent/roundvi-task76: task76-old-fails-check") instead of my own — `refs/stash` is repo-wide, not
worktree-wide, so two concurrent worktrees' stash pushes landed on one shared stack and my
argument-less `pop` grabbed whichever was on top. I caught this via `diff -q` against a
pre-stash backup I'd taken defensively (not by checking before applying, which is the safer
practice) and restored my correct fix from that backup before anything was committed — no damage
landed in this branch. `git stash list` is now empty and my own original stash entry appears to
have been consumed in the collision; it is not needed since the backup was byte-identical to what
I restored and both the old-fails and new-passes runs above were independently valid (the
old-fails run's revert was verified empty-diff-against-HEAD at the time it ran, before the pop
confusion). Task-76's popped entry (`978d3d2...`) is still present as an unreachable git object
and recoverable via `git stash apply 978d3d2445a60bc9cd52d43198c45cbae7a2fc93` from any worktree
of this repository, should that lane need it — I did not apply it myself, since it is not mine to
resolve. **This exact hazard is already disclosed in `CLAUDE.md`** (commit `108898d`, landed by
task-76's own lane before I finished this task) — my incident independently confirms it from the
other side of the same collision. I did not duplicate the disclosure.

## Full test results (whole files, no `-k`)

| File | Before (baseline, `main` @ `108898d`) | After (this branch) |
|---|---|---|
| `tests/test_cli.py` | 194 passed | 194 passed |
| `tests/test_hoist_rollback_git.py` | 12 passed | 13 passed (+1 new discriminator test) |
| `tests/test_pr_e2e.py` | 25 passed | 25 passed |
| `tests/test_event_stream_wiring.py` | 6 passed | 6 passed |
| **Total** | **237 passed** | **238 passed** |

All four files identified by grepping for `_pr_records`/`_upsert_pr_record` coverage
(`tests/test_event_stream_wiring.py` only references `_pr_records` in a comment, no direct call —
included anyway for completeness). Baseline run on the primary checkout at `main`'s current HEAD
(`108898d`, which includes ADR-0126's docs and the CLAUDE.md stash-hazard disclosure, but not this
task's code fix). No regressions; the only count change is the one new test I added.

`tests/test_integration_honesty_citations.py` re-run after the D122 doc edit: **70 passed**
(citation line numbers below my edit point shifted; all still resolve).

## mypy / ruff

- `mypy --strict src/fleet/` (also bare `python -m mypy`, which resolves the same scope via
  `pyproject.toml`'s `packages = ["fleet"]`): **Success: no issues found in 129 source files.**
- `ruff check src/fleet/` and the three touched test files: **All checks passed.**
- `ruff format --check` on `src/fleet/cli.py` and the touched test files: flags the same
  pre-existing reformat opportunities that exist on unmodified `main` (verified by running the
  identical command against the primary checkout — same files, same line content, just shifted
  line numbers) — nothing introduced by this task.

## Concerns

- None blocking. The `execute_hoist_rollback`/`_ordered_revert_shas` dedup-by-`owner_repo_id`
  logic is now annotated (not redesigned, per this task's explicit scope) to disclose that it
  will need revisiting once a future task actually wires a contract-PR-dispatch call site that
  could construct a second, genuinely distinct repo-owned draft alongside a contract's own draft
  for the same `repo_id`.
- The git-stash cross-lane hazard is already disclosed in `CLAUDE.md` by task-76; I did not
  re-add it, only note my own corroborating incident here for the record.
