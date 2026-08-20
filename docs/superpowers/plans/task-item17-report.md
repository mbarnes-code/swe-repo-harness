> Round-C task report, promoted unchanged from `.superpowers/` scratch by lane DOCSTALE because it is cited by committed `docs/INTEGRATION_HONESTY.md` (D66's editorial correction) and would otherwise dangle if the scratch directory were deleted. DOCSTALE does not own INTEGRATION_HONESTY.md and has not repointed its citation.

# Report — lane HK17, open item 17 (both halves)

**Status: DONE_WITH_CONCERNS** (one disclosed limit of the binding, plus two observations that are
another lane's property and are reported, not edited).

Commit: **`c7f72c6`** — `src/fleet/state/schema.sql`, `tests/test_llm_cache.py`. Nothing else
touched. No ADR taken, and none needed.

## 0. Pre-flight state (five concurrent lanes)

`git status --short` at start:

```
 M docs/SPEC.md
 M src/fleet/cli.py
 M tests/test_llm_roles.py
?? .superpowers/
```

All three are sibling lanes' uncommitted work; none is reported below as a defect. `HEAD` moved
from `9e5c093` to `23a4396` mid-task (another lane landed). **The SPEC annotation this task mirrors
is NOT a sibling's uncommitted edit** — I checked, rather than assuming: `git show
HEAD:docs/SPEC.md` and the working tree carry the identical text at `:4254-4255`, and the 38/10
line diff on `docs/SPEC.md` is elsewhere in the file.

## 1. Deliverable 1 — the annotation drift

Verified, not taken on faith. `schema.sql:474` read `    effort      TEXT NOT NULL,` with no
comment; `docs/SPEC.md:4254-4255` carried the annotation as **two** lines (the brief quoted the
one-line form from the round-B audit; the second clause "NOT NULL and no CHECK, so absence needs no
migration" is present in `HEAD` and is the load-bearing half).

Checked against `## ADR-0075` in `docs/DECISIONS.md` before mirroring. Consequence 2 reads:
"`LlmCallRecord.effort` and the `llm_cache.effort` column follow the same spelling: absence
round-trips as the empty string, so the column stays `TEXT NOT NULL` and **no database migration is
required**." The SPEC's annotation states exactly that ADR's decision — it is not a paraphrase that
drifted, so it was mirrored verbatim in wording, re-indented to `schema.sql`'s column-51 comment
gutter.

`schema.sql:474-475` now reads:

```sql
    effort      TEXT NOT NULL,                    -- ADR-0075: '' = target declared none. NOT NULL
                                                  --   and no CHECK, so absence needs no migration.
```

Direction of travel was SPEC → code only. `docs/SPEC.md` untouched.

## 2. The binding — shape chosen, and why

**Chosen: (b), assert the semantic; explicitly NOT (a).** New test in `tests/test_llm_cache.py`:
`test_no_declared_effort_persists_as_empty_string_in_a_not_null_check_free_column`.

The reason (a) was rejected is stronger than "string tests are brittle", and it is a measured fact
rather than a preference. I diffed the SPEC's fenced `CREATE TABLE` listing (794 lines, from
`docs/SPEC.md:3823`) against `src/fleet/state/schema.sql`: **22 hunks**. The SPEC's rendering is a
deliberately condensed one — the same comments, rewrapped and shortened to fit the document (e.g.
the `findings.kind` enumeration is compressed, `⇒` is rendered `=>`). A binding asserting "the two
listings agree" would therefore be false the moment it was written, and narrowing it to the single
`effort` line buys a test that a reflow of either file breaks while a real defect walks past it.
Concretely: **a `CHECK (effort IN ('low','medium','high'))` added to `schema.sql` — the exact
change the annotation exists to forbid — leaves the *comment* untouched and so passes any
string-comparison binding.** It fails the semantic one (proof M2 below).

The annotation makes three claims, and each fails differently, so each gets its own assertion
against the **live** schema and the **real** write path (`initialize_database` → `StateWriter` →
`SqliteLlmCacheStore.put`), never against a declaration read:

1. `''` **is what gets written** for a target that declared no effort — raw
   `SELECT effort, typeof(effort)` returns `("", "text")`, and `store.get()` narrows it back to
   `None`. Failure mode guarded: a fabricated default is back and the cache is re-keyed.
2. **No CHECK** — the `''` row is accepted at all. Failure mode guarded: a CHECK arrived and a
   no-effort target became unstorable.
3. **NOT NULL is enforced, not merely declared** — an `UPDATE … SET effort = NULL` submitted
   through the writer raises `sqlite3.IntegrityError`. Failure mode guarded: a second spelling of
   absence arrived, which is precisely the migration ADR-0075 declined.

This is the guardrail-6 point the brief flagged (`models/tasks.py:100-102`): claim 1 is *not*
readable from the schema declaration at all — the `''` is produced by
`cache.py:302`'s `"" if record.effort is None else record.effort`, a line the schema has no
opinion about. The test exercises it.

**Disclosed limit, and it is why the status is DONE_WITH_CONCERNS.** A semantic binding does **not**
detect the literal defect this item reported. Mutation M1 below shows my test passing with the
annotation deleted again from `schema.sql`. That is the honest trade: it holds the *meaning* the
annotation asserts, in both files' name, and cannot hold the *presence of a comment*. I judged the
meaning to be what matters — a comment that agrees with a schema that means the wrong thing is the
worse outcome — but the next drift of the comment alone will still be silent, and pretending
otherwise would be the "convention wearing a mechanism's clothes" that CLAUDE.md Rule 12 forbids.

## 3. Deliverable 2 — the retracted claim, and the class sweep

**The retraction, cited:** **D66** in `docs/INTEGRATION_HONESTY.md:3798-3860` — *"FIXED, LANDED
(`87884d7`, with the marker rounds `7ecc898`, `2c6e89f`, `eabfcdb`) … verified on `main` at
`6a41840`"*. Five sites asserted the harness relies on "adaptive thinking"; **neither behaviour
exists** (no `thinking` key is constructed anywhere; `ModelCapabilities` has no effort field). D66
records that the determinism conclusion survives on its second leg, and that **"the modality weakens
from 'cannot pin' to 'does not pin'"**. D66 also records this exact miss, in its own words: *"a
sixth carrier of the same clause, `tests/test_llm_cache.py:4`, was outside every lane's file set and
is still on `main`, unamended."* Ledger context: `docs/superpowers/plans/ledger-sdd-backlog-b.md`
OO1 and "BK2 fix round 5 — commit `ddf52d9`".

**Sweep: found 6 carriers of the class · fixed 1 · left alone as correct 5.** Greps run over `src/`,
`tests/`, `config/` and `docs/` for `adaptive thinking`, `-i thinking`, and `temperature`:

| Site | Verdict |
|---|---|
| `tests/test_llm_cache.py:4` | **FIXED** — the last live carrier. `grep -ni thinking src/ tests/` now returns **zero** hits. |
| `src/fleet/llm/cache.py:3-6` | Correct, untouched — already carries the D66 replacement ("pins no sampling controls … not a `temperature=0`"). |
| `src/fleet/llm/__init__.py:6` | **Correct, untouched** — "makes a re-run reproducible without pinning a temperature" is true and is not the retracted premise: it says the cache achieves reproducibility *without* a pinned temperature, not that a model behaviour *forbids* one. Mirror-image rule applied: matched the grep, is not a defect. |
| `src/fleet/models/tasks.py:492` | Correct, untouched — carries "the harness pins no sampling controls (§11.6)". |
| `docs/SPEC.md:6927-6932` | Correct, untouched (and another lane's file regardless). |
| `docs/DECISIONS.md:309`, `:5387` | Correct, untouched — both are the negative statement, which is the true one. |

The `docs/superpowers/plans/*.md` and `docs/INTEGRATION_HONESTY.md` hits are the audit and ledger
records *of* the retraction, quoting the false clause to name it. Left alone by construction.

The replacement docstring states the negative that is code-backed (`§11.6`: no `temperature`,
`seed`, `top_p` or `thinking` key under `src/fleet/llm/`) and names the retraction so the next
reader can find D66, without re-asserting the false clause in quotable form. It does **not** quote
another module's comment verbatim (CLAUDE.md §3, "never quote another module's comment").

## 4. Verification — Rule 12 and guardrail 6

All mutation work ran in a **detached worktree** at `23a4396`
(`…/scratchpad/wt17`, removed afterwards; `git worktree list` clean), never in the shared checkout.
`PYTHONPATH=<wt>/src` was proven to win over the editable install *before* trusting any result
(`fleet.llm.cache.__file__` printed the worktree path) — otherwise every mutation would have been a
no-op against the primary `src/`. The worktree's `REPO_ROOT` has no space in it, so `conftest.py`'s
`_bazel_root()` gives it a **private** `BAZEL_ROOT` and its sessions could not reap a sibling lane's
output base.

`old` = `tests/test_llm_cache.py` at `HEAD` (18 tests). `new` = my version (19 tests). Baseline
unmutated: **37 passed**.

| # | Mutation | File actually changed? | old | new |
|---|---|---|---|---|
| M1 | Revert the annotation (`git checkout` `schema.sql`) | yes — `:474` back to `effort      TEXT NOT NULL,` | pass | **pass** — the disclosed limit, §2 |
| M2 | Add `CHECK (effort IN ('low','medium','high'))` | yes — `git diff --numstat` `2 1` | **18 passed** | **1 failed** (`IntegrityError: CHECK constraint failed`) |
| M3 | `cache.py:302` writes `"medium"` when effort is `None` | yes — `numstat` `1 1`, line shown | **18 passed** | **1 failed** (assert at `:609`) |
| M4 | Column made nullable (`effort      TEXT,`) | yes — `numstat` `2 1`, line shown | **18 passed** | **1 failed** (`pytest.raises` at `:624`) |

M2, M3 and M4 are each **discriminating**: old passes, new fails, same input, one clause each. Every
mutation was proven to have really changed the file (`git diff --numstat` plus the mutated line
printed) before its result was read — the no-op trap this project was burned by. Reverted after
each; restored-to-fixed re-run: **19 passed**.

**Guardrail 6, instrument validated three ways, not one:** (1) fires on a known-bad state — M2 and
M4 are the schema defect the annotation forbids; (2) silent on the swept/clean state — baseline
37 passed, and silent under M1 where nothing semantic broke; (3) **fires on a synthetic fault
injected into a clean file** — M3 perturbs `cache.py`, which no part of this task edited, and the
test caught it. Without (3) a schema-only detector could have been passing for the wrong reason.

Scoped runs only, one session at a time, never the full suite, no `FLEET_*` exported, `.venv/bin/python`
throughout. Final primary-checkout run: `tests/test_llm_cache.py` **19 passed in 2.09s**, clean
`bazel disk` line (peak 1.61 GiB, ceiling 6 GiB, residual 0 bytes).

`ruff check tests/test_llm_cache.py` → **All checks passed.** `ruff format --check` reports two
would-reformat sites at `:161` and `:390`, **both pre-existing and neither mine** — left alone per
Rule 3. `mypy tests/test_llm_cache.py` reports 2 errors, both pre-existing (`ecosystem="maven"` and
`effort: str = "low"` in the shared fixtures, present at `HEAD` lines 52/56) — reported, not fixed.

## 5. Concerns

1. **The binding does not catch a comment-only re-drift** (M1). Deliberate, argued in §2, and the
   reason the status is DONE_WITH_CONCERNS rather than DONE. If you want that half mechanised, the
   only honest form is a doc-listing extractor that diffs the whole SPEC fence against `schema.sql`
   — but the measurement in §2 says it would fire on 22 pre-existing intentional condensations, so
   it is a project of its own, not a line in this test.
2. **A near-miss false report, recorded because the mirror-image rule caught it.** The listing diff
   showed `schema.sql` naming `CapabilityDrift` and `BackendUnavailable` in the `findings.kind`
   comment where the SPEC listing does not. I checked before reporting it: `docs/SPEC.md` mentions
   `CapabilityDrift` **9 times** elsewhere (`:5721`, `:5727`, `:5859`, …). It is comment
   condensation inside the listing, **not** a semantic gap. **Not a defect. Do not action it.**
3. `src/fleet/llm/__init__.py:6` will keep matching any future `temperature` sweep. It is correct.
   Flagging it here so the next sweeper does not "fix" a true sentence.
4. The `docs/SPEC.md:4254` annotation is a two-line comment, not the one-liner the round-B audit
   quoted. Anyone reconciling item 17 against the audit text should use the file, not the audit.
