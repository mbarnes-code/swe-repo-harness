# Open-items audit — §38's "What is still NOT proven / left open"

**Audited against `main` = `25af323`** (read-only; no edits to `src/`, no worktree, no pytest).

`main` has moved two commits past the `6a41840` §38 records (`5feb1e7`, `25af323`). **Both are
docs-only** — `git diff --stat 6a41840..main` touches nine files, all under `docs/`, zero under
`src/` or `tests/`. So every `src/`-grounded claim in §38 re-verifies at the same tree it was
written against, and any staleness found is staleness that was already there when it was written,
or staleness in the *world outside git* (item 18).

Working tree is clean, so file contents read below are `main`'s.

## Classification of the 20

| # | Verdict | Evidence |
|---|---------|----------|
| 1 | **VALID** | Re-measured on this host: `find_spec` → `anthropic` True, `openai` True, `boto3` **False**, `google` **raises `ModuleNotFoundError`** (not even a namespace package). The `bedrock`/`vertex` wire shapes are unexercised and the four-name `discover()` check is unsatisfiable here. Work: prove one adapter against a real endpoint in a throwaway venv. |
| 2 | **VALID** | `git grep llm_policy -- src/` returns exactly two lines: `orchestrator/context.py:141` (declaration) and `:172` (consumption). **No assignment anywhere in `src/`.** `CallPolicy()` is always all-defaults. Work: `CallPolicy.from_config` + `cli.py` wiring + 3 `KNOWN_INERT` deletions. |
| 3 | **VALID** | `record_attempt`'s INSERT (`state/repository.py:1677-1690`) names 24 columns; `llm_failovers` is **not among them**. The only `llm_failovers` mentions in `src/` are `schema.sql:683` (the column), `migrations/v005_backend_identity.py:12,28` (the DDL) and `llm/client.py:230` (a docstring). No writer. |
| 4 | **VALID** | `findings.py:337` declares `tier: ModelTier \| None = None`; the sole production caller, `runner.py:625-628`, passes `repo_id=`, `phase=`, `observed=` and **no `tier=`**. Every shipped row takes the unnarrowed path. |
| 5 | **VALID** | All three strings live on `main`: `runner.py:640` (`"…tier is DOWN: {decision.reason}"`), `retry.py:196` (`"every target for the tier is DOWN…"`), `enums.py:383` (`# every target for a tier is DOWN`). `findings.py:347` itself states `DOWN` "has no representation anywhere" in `src/`. The 429 misclassification is next-task 6. |
| 6 | **NOT-A-TASK** | A structural limit, disclosed in three places (`findings.py:380-396`, `INTEGRATION_HONESTY.md:3376,3566`). Nothing to build: the field *cannot* read "complete" because the evidence to justify it does not exist per-tier. Permanent honest caveat, not backlog. |
| 7 | **VALID** | Verified as a *call site*, not a name match: nothing under `src/` imports `orchestrator.stubs` — the only `stubs`-module hits in `src/` are `bazel/layout.py` (`third_party/stubs` paths), `models/tasks.py`/`models/state.py`/`models/enums.py` (the `stubs` **table**), and one comment in `llm/backends/bedrock.py`. The sole importer anywhere is `tests/test_stubs.py`. Meanwhile `cli.py:11006-11010` still carries the raw-SQL `UPDATE stubs SET state='ABANDONED'` T4 encoding. Two encodings of one rule. |
| 8 | **VALID** | `demote(` outside `enums.py` is empty in `src/`; `RESUME_DEMOTE` appears only in `enums.py` and as a re-export in `models/__init__.py:26,104`. A re-export is not a caller. The gate is correct but unreached — this is next-task 1's subtask 6. |
| 9 | **VALID** | `cli.py:10041` `ResumeIncompleteError` present and names steps **5, 2, 4, 6** verbatim. `_refuse_unbuilt_resume_flags` (`cli.py:10263-10267`) refuses `--from-phase`, `--repo`, `--reset-attempts` **plus** `--revalidation` and `--raise-revalidation-rounds`. §38's careful note — that "steps 2, 4, 5, 6 **and 8**" was inference and only the first four are on the record — is confirmed: step 8 is not named in the refusal text. |
| 10 | **VALID — count and module count both exactly right** | Three `_unavailable(...)` call sites: `cli.py:2408` and `:2520` (both naming `workers/relocate.py`), `cli.py:10958` (naming `workers/buildverify.py`). Two distinct modules. The false sentence is at `cli.py:905`: *"{module} still raises NotImplementedError."* **`grep -n NotImplementedError` over `workers/relocate.py` and `workers/buildverify.py` returns nothing at all** — the text is false at all three sites, not merely at some. The other `*_unavailable` hits in `cli.py` (`parse_probe_unavailable`, `adapter_unavailable`) are unrelated dict keys and correctly excluded. |
| 11 | **VALID** | `_target_for` (`cache.py:621-628`) matches `(target.backend, target.model_id)` and **returns the first match**, so a route carrying a primary and a standby that differ *only* in `effort` resolves to the primary and `_store_response` (`:593`) stores the primary's effort for an answer the standby produced. `usage` carries no effort, so the ambiguity is not recoverable at that line. Not fixed; only documented (`INTEGRATION_HONESTY.md:3602-3607`). **Severity note not in §38:** the shipped `config/models.yaml` has six targets with six distinct `model_id`s, so no current route can trigger it — it is a latent code defect, not a live one. |
| 12 | **NOT-A-TASK** | Re-verified: `config/models.yaml` ships **six** targets, three `anthropic` and three `openai_compatible`, **zero** `bedrock`/`vertex`. §38 already adjudicates this as record-accuracy and states it was deliberately not routed. A recorded judgement kept visible is not work. |
| 13 | **VALID — count re-measured and exact** | `grep -c "with _mapped_errors()" src/` → **22**. `_mapped_errors` is defined zero-arg at `cli.py:484`. The funnel carries no `run_id` and no writer. (The `fleet pr` no-flush half §38 says was fixed is out of this item's scope.) |
| 14 | **NOT-A-TASK** | Exactly the shape the audit brief names: a documented adversarial-only escape deliberately not patched, with the reason recorded (ADR-0077 §4.2) and the accidentally-reachable-vs-adversarial standard stated. Patching identity would buy the appearance of closure. |
| 15 | **VALID (all three legs)** | (a) `orchestrator/stubs.py:556-560` — `reconcile(..., now: datetime \| None = None, open_pr_max_age_s: float \| None = None)`; both default `None`, so the merge-wait bound is opt-in. (b) `stubs.py:564,586,661` emit `UnresolvedStub`; SPEC §13 row 45 (`SPEC.md:7197`) requires dependents `BLOCKED` with an `UnmergedDependency` finding past the bound — `UnmergedDependency` appears nowhere in `stubs.py`. (c) `tests/test_config_keys_are_read.py:191` still carries `"fleet.yaml:pr.merge_wait_timeout_s"`. The `open_pr_max_age_s` naming dodge is real and is the reason the ratchet is not tripped. |
| 16 | **VALID** | `grep -c '^## ADR-' docs/DECISIONS.md` → **77**; the last is `## ADR-0077`. **0078 is free.** No test binds BK1's two-name narrowing. |
| 17 | **STALE** | Both halves fixed by `c7f72c6` (landed after this row was written). `src/fleet/state/schema.sql:504` (the line moved to `:504` as the file grew; was `:474`) now carries `-- ADR-0075: '' = target declared none. NOT NULL and no CHECK, so absence needs no migration.` beside `effort TEXT NOT NULL,`, bound by `test_no_declared_effort_persists_as_empty_string_in_a_not_null_check_free_column`. And `tests/test_llm_cache.py:4`'s docstring no longer reads "adaptive thinking forbids pinning a temperature" — it now states the retraction itself: "the harness pins no sampling controls — no `temperature`, `seed`, `top_p` or `thinking` key is built anywhere under `src/fleet/llm/`". Re-verified this session via `git show main:src/fleet/state/schema.sql` and `git show main:tests/test_llm_cache.py`. |
| 18 | **STALE** | `git worktree list` now shows **two** entries: the primary checkout on `main`, and `worktrees/wt-WT1-example` on `agent/WT1-example`. The eight round-B lane worktrees are gone. **The survivor is out of scope for this item**: `agent/WT1-example` is a *round-A* leftover from the ADR-0074 pre-commit-hook verification (cited as live evidence in `DECISIONS.md:6458-6566`, `tools/worktree/README.md:117`, `task-HOOK1-report.md:53`), it is an ancestor of `main` (`git branch --merged main` lists it), and this round's own ledger records it as **"LEFT DELIBERATELY"** (`ledger-sdd-backlog-b.md:2009`). Item 18 described ten lane worktrees after eight lanes merged; that condition no longer obtains. |
| 19 | **VALID — the recorded number is right** | `.venv/bin/ruff 0.16.2 format --check --diff src/fleet/cli.py` → exit 1, **60** `@@` hunks, "1 file would be reformatted". The `7a8bfbb` baseline re-measured independently by piping `git show 7a8bfbb:src/fleet/cli.py` through `--stdin-filename src/fleet/cli.py` → exit 1, **58** hunks. §38's "60 on landed `main`, 58 at `7a8bfbb`" is exactly correct, and the +2 ratchet is real. The decision (fix vs. ratchet) is unmade. |
| 20 | **DUPLICATE** | Verbatim carry of **§37f item 6** ("Round 38 reconciliation is still pending: `research-38.md`/`review-38.md` remain tracked but uncommitted as their own checkpoint; `workers/base.py`/`workers/buildverify.py` history from that thread has not been read against this round's D34/D35/D36/D41 corrections"). §38 itself labels it as carried unchanged. Both files are still tracked (`git ls-files` → `docs/superpowers/plans/research-38.md`, `review-38.md`) and the claim is **still true** — but it is not a twenty-first distinct finding of this round. |

### Counts

| Verdict | Count | Items |
|---|---|---|
| **VALID** | **14** | 1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 13, 15, 16, 19 |
| **STALE** | **2** | 17, 18 |
| **NOT-A-TASK** | **3** | 6, 12, 14 |
| **PARTIAL** | **0** | — |
| **DUPLICATE** | **1** | 20 |

**Assessment of the list as a record.** This is a good list. Fourteen of twenty are real and
unmoved; the three NOT-A-TASKs are all *explicitly labelled as limits in their own text* ("structural",
"deliberately not patched", "recorded so the judgement stays visible") — they are not the list
padding itself, they are honest caveats parked in a backlog section where a reader may mistake
them for work. The one duplicate self-declares. The three traps the audit brief named were checked
and **§38 got all three right**: item 7's "no caller" survives a call-site-level check rather than a
name grep; item 10's *three false sites / two modules* is precise about the false ones rather than
the total `_unavailable`-ish hits (22 name matches in `cli.py`, only 3 are the call); item 13's
re-count of 22 and item 19's 60/58 both reproduce exactly. Two entries have gone stale since the
text was written, by different routes: item 18 went stale **outside git** (the worktree teardown
happened in the working world, not in a commit this list re-verifies against); item 17 went stale
**inside git** — `c7f72c6`, landed after this row, is the exact fix the row described as missing.

---

## The prioritised next-task list at the end of §38

Assessed for "is this still the right next thing" against `main` = `25af323`.

| # | Task | Verdict |
|---|------|---------|
| 1 | Build resume step 5 in the `design-resume-step5.md` order, starting at subtask 2 | **Still right.** The premise checks out: subtask 1 landed — `ebd1624` is an ancestor of `main` and `791b428..ebd1624` is the DEM1 `demote()` sequence — and `orchestrator/reentry.py`, subtask 2's target, **does not exist** (`orchestrator/` holds budgets, context, findings, registry, retry, runner, scheduler, stubs and nothing else). Closes items 8 and 9, and the `evidence_holds`-not-`preconditions_hold` constraint is still the hard part. |
| 2 | Write ADR-0078 for BK1's backend-name narrowing + the missing test | **Still right, and the number is still free** (77 ADRs, last `0077`). Cheap; do it before another lane claims 0078. |
| 3 | Give `orchestrator/stubs.py` a caller and retire `cli.py`'s raw-SQL T4 | **Still right.** Confirmed no importer in `src/`; the raw-SQL encoding is still at `cli.py:11006-11010`. |
| 4 | Wire `RunContext.llm_policy` | **Still right.** No assignment in `src/`. |
| 5 | Wire the `tier=` arm and `attempts.llm_failovers` together | **Still right,** and the "same cross-lane change" rationale holds: both need `WorkerError`/`TierUnavailable` to carry a tier, which neither does today. |
| 6 | §13 row 43 — rate limiting (LARGE) | **Still right, and its scoping premise re-measures correct:** `limits.for_tier(...)` is acquired at exactly **one** call site in all of `src/` — `workers/classify.py:162` — out of 12 workers. `asyncio.Semaphore` is still constructed fixed-size at `budgets.py:979`. |
| 7 | §13 row 38 — `ContextTruncated` (MEDIUM, greenfield) | **Still right, with one wording correction.** `ContextTruncated` has **zero occurrences in `src/`** — genuinely greenfield. But "nothing in `src/` sizes against `max_context`" is slightly too strong: `settings.py:1444-1450` *does* compare a target's declared `max_context` against `requirement.min_context` at config-validation time. What is absent is any **runtime** sizing of an actual prompt. The task is unchanged; the sentence should be. |
| 8 | Prove one backend adapter against a real endpoint, in a throwaway venv | **Still right.** `boto3` absent, `google` absent. |
| 9 | Housekeeping, batched | **Partly already done — de-scope it further.** *"Prune the ten stale `agent/*` worktrees"* is **complete**: eight were torn down, and the one survivor (`agent/WT1-example`) is the round-A ADR-0074 hook fixture that this round's own ledger records as deliberately kept. Pruning it would destroy live evidence cited in `DECISIONS.md:6458-6566` and `tools/worktree/README.md:117` — **do not prune it**. **The `schema.sql` ADR-0075 mirror and `test_llm_cache.py:4` amendment are also now done** (item 17, corrected this session): `c7f72c6` landed both. What remains of the batch: the three false `_unavailable` strings (item 10) and the `ruff format` fix-or-ratchet decision at a **confirmed 60 hunks**. The `heartbeat_ttl_seconds` writer is also still genuinely missing — `settings.py:218-222` declares `stale_after_s` as "THE authoritative liveness TTL" that a phase "captures verbatim into `phases.heartbeat_ttl_seconds` when claimed", and `test_config_keys_are_read.py:201` records the opposite: *"nothing constructs `phases.heartbeat_ttl_seconds` from the key"*. |
| 10 | Reconcile with round 38 | **Still right, but it is a §37f task wearing a §38 number** (see item 20). It has now been carried across two checkpoints without being started; if it is not going to be done it should be closed explicitly rather than re-listed a third time. |

**Two entries need changing, both inside #9: the worktree clause is done and the remaining
worktree must not be pruned, and (corrected this session) the `schema.sql`/`test_llm_cache.py:4`
clause is also done — `c7f72c6` landed both.** Everything else is still the right next thing.

---

## The carried-forward backlog (§32–§37f) — is it live or accreting?

Sampled **15 items across 9 sections** (§32, §33, §34, §35 ×2, §36 ×3, §37, §37b ×2, §37e, §37f ×3),
deliberately weighted toward items that explicitly say "unchanged from …" or "everything §32 listed
is unchanged".

**Verdict: 3 still true, 11 closed, 1 partially closed — roughly 4 in 5 of the carried-forward
backlog is dead text.** It is an accreting list, not a live one. The only survivors in the sample
were §32 #1 / §33 #1 (the offline `--network=none` sandboxed path, genuinely never attempted) and
§37f #5 (`D51` still has no second reviewer — `INTEGRATION_HONESTY.md:2970` still reads
`D51 — OPEN, narrowly` with no review block).

Landing has closed, among others:

* **§37b #1**, "this harness has no `ModelBackend` implementations at all" — four now exist
  (`llm/backends/{anthropic,openai_compatible,bedrock,vertex}.py`), which §38 itself notes.
* **§36 #2**, "ADR-0067's four parts undelivered" — `ProbeIndeterminateError` is raised at
  `rewrite/astgrep.py:211` and exported at `rewrite/__init__.py:42`.
* **§37e #2 / §37f #2 / §37f #3** — all three landed (`9a7148c`, `f592327`, `4b22f3b`); I verified
  #2 and #3 independently: `rewrite.py:344` and `:702` both pass `declared_path=`, and
  `container.py:258` now has `_remove_with_reason` with an `except OSError` guard.
* **§35 #1 / §37 #5**, "the working tree is dirty, N files uncommitted" — the whole
  "as of this writing" class. `git status` is clean. These were stale by construction.

### The most striking finding: §35 item 4 was false when it was written

§35's list asserts the `-9`/`-15` correction has no test, citing `grep -rn '\-9' tests/` as finding
only `OOM_EXIT_CODES`. The test it says is missing is
`tests/test_vcs.py:371` —
`killed = Git(tmp_path, runner=ScriptedRunner(exit_code=-9, started=True, timed_out=True))` — and
`git show a1178f7:tests/test_vcs.py` has that exact line at **`:250`**, i.e. it was present **in
this repository's first commit**, before the claim was made. A grep whose pattern could not match
was recorded as evidence of absence, and then carried forward unchallenged. This is the same
failure §38 named as its own dominant one — *verify the thing, not a stand-in for it* — reaching
backwards.

### A second, live hazard: the ledger's `— OPEN` headers

§34 #2 flagged this and it is **still true and now worse**. `docs/INTEGRATION_HONESTY.md` has **42**
headers matching `**D<n> — OPEN`, and several of them are followed, in the same entry, by an
appended `**Status — CLOSED, FIXED in <sha>**` block. `D44` is the clean specimen: the header at
`:2157` reads `**D44 — OPEN.`, and `:2171` reads `**Status — CLOSED, FIXED in `f1aac12`.**`. The
same shape holds for D34/D35/D36/D41/D45 and for D39/D42/D43. **Any future count of open defects
that greps the headers will re-inflate the debt by a large factor** — which is precisely the
"stale in the closed direction" failure §37e named. Fixing the headers is cheap and is not on
either the 20-item list or the next-task list; it should be.

### What this implies for how the list should be maintained

The §38 list is markedly healthier than its predecessors — 15 of 20 valid, with the traps handled —
and the reason is visible in its own preamble: *"Each was re-verified against `main` at `6a41840`
while this section was written"*, and *"none of the seven the landing genuinely closed is carried
forward."* That discipline is what the §32–§37f lists lacked. The recommendation is to make it
explicit: **an item may only be carried into a new section with a re-verification note naming the
SHA it was re-checked at** (ADR-0073's rule, applied to `PROGRESS.md`'s open lists rather than only
to status claims). Without that, §38's clean list will accrete the same way within two rounds.
