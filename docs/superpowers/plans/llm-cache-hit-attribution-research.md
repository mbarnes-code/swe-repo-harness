> **Promoted from untracked scratch at round F close.** Written by lane W15 in
> `.superpowers/sdd/handoff-round-f/lanes/W15/`, which returned BLOCKED: the task it was given
> (wire `on_hit`) is not doable as posed, and this records why. Body byte-identical apart from this
> banner. **Now a tracked reference — re-measure anything you act on.** The runnable repro script is
> NOT promoted and lives at `.superpowers/sdd/handoff-round-f/lanes/W15/repro_llm_cache_hit_column.py`.

# W15 — `attempts.llm_cache_hit` on a cache hit

**STATUS: BLOCKED.** No code patch. The assigned fix ("wire `on_hit` in `context.py`") **cannot**
make the success criterion true, and the reason is not effort: it is that `on_hit` carries no
attempt identity, that the `attempts` writer never names the column, and that the only two
producers of an `AttemptRow` in `src/` live in a file another lane holds. Every leg below is
measured at the HEAD that was live when I acted on it.

Base given in the brief: `f54dac8`. **HEAD moved to `53e5d8d` mid-lane** (a sibling landed
`tests+plan: close the four citation defects the cache commit shipped`). `git diff --stat
f54dac8..53e5d8d -- src/` is **empty**, so every `src/` measurement below holds at both; the
behavioural probe was re-run at `53e5d8d` and reproduced identically. One of the sibling's new
tests changes my analysis materially — see leg 4.

---

## 1. Current truth, exercised rather than read

`.superpowers/sdd/handoff-round-f/lanes/W15/repro_llm_cache_hit_column.py` — drop into a
worktree's `tests/`, `pytest -q -s`. Real temp DB, real `StateWriter`, real
`SqliteLlmCacheStore` built by `__post_init__` itself, no `llm_cache`/`llm_cache_mode` injected
(the kwarg set every `RunContext(` site passes), scripted offline backend, no socket.

```
W15-PROBE backend_calls=['only'] llm_cache_rows=1 llm_cache_hit=[0]      # @ f54dac8 and @ 53e5d8d
W15-PROBE-SYNTHETIC llm_cache_hit=[1]
```

**Two identical `complete()` calls → 1 backend invocation, 1 `llm_cache` row, and
`attempts.llm_cache_hit == 0`.** `cac537d`'s cache works; the column SPEC §11.6 promises does not.

The second line is the check-(c) control demanded by the protocol: it injects a `1` into the same
column through the same real writer and re-reads through the same `mode=ro` handle. The read is
therefore **not** structurally blind to a `1`, so the `0` is a statement about the code and not
about the instrument. (Check (b) — silence on an already-swept file — is not applicable: there is
no swept instance of this defect anywhere in the tree.)

Note what the probe does **not** assert: no field comparison, no `isinstance`, no
`ctx.llm_cache is not None`. It asserts the backend call log, the `llm_cache` row count and the
column value, per the brief.

## 2. Gap (a) — the writer never names the column. Derived two genuinely different ways.

* **AST.** `AttemptRow`'s annotated field list (`state/repository.py`) is 24 names,
  `attempt_id … already_applied`; **`llm_cache_hit` is not among them**. `repository.py` contains
  the string `llm_cache_hit` **zero** times.
* **SQL-vs-DDL, sharing no blind spot with the above.** Parse `record_attempt`'s `sql` assignment
  out of the module with `ast.literal_eval`, extract the `INSERT INTO attempts (...)` column list,
  and diff it against `PRAGMA table_info(attempts)` on a database built from the real
  `state/schema.sql`. **31 declared, 24 named.** The class result — *columns declared in the
  schema that `record_attempt` can never write* — is exactly seven:
  `integration_ref, container_id, input_tokens, output_tokens, llm_cache_hit, llm_backend, llm_failovers`.

D62's body names **five** of those seven. The two it does not (`integration_ref`, `container_id`)
are not counter-evidence: `integration_ref` is written by a separate `UPDATE` in
`cli.py:6062`, and `container_id` I did not chase. **D62's five are correct and unchanged**; the
class is simply larger than the entry's framing, which is a `PARTLY`-shaped observation about the
entry, not a correction to it.

## 3. Gap (b) — `on_hit` is not passed, and it could not attribute a hit if it were

AST over all of `src/`: exactly **two** `CachingModelClient(` construction sites.

| site | kwargs | `on_hit` passed |
|---|---|---|
| `llm/cache.py:443` (`scoped()`, propagating) | `mode … on_hit … redact` | **yes** |
| `orchestrator/context.py:233` (`__post_init__`) | `mode`, `harness_version` | **no** |

So W11's D62 annotation leg (b) re-derives exactly. Its citations also re-derive at `53e5d8d`
unchanged from `b5f7760`: `cache.py:412` (the parameter), `:425` (stored), `:560-561` (called from
`_replay`). **One citation has rotted, harmlessly:** W12 located the gap at
`context.py:233-238`; at `53e5d8d` the call spans **233-241** (`mode=(` opens at 237, closes 239,
`harness_version=` 240, `)` 241). It resolves to the right symbol — rot, not falsity, and the same
shape D62's own re-anchoring note records for `runner.py:625-628`.

**The deeper half.** `LlmCallRecord` (`models/tasks.py:491`) carries `cache_key`,
`prompt_template_version`, `role`, `tier`, `backend`, `model_id`, `structured_output_mode`,
`effort` … and **no `run_id`, `repo_id`, `phase` or `attempt`**. One `CachingModelClient` serves a
whole wave concurrently (`worker_context()` hands `self.model_client` to every repo), so an
`on_hit` that attributed a hit to "the current repo" would be the guess
`orchestrator/findings.py`'s own module docstring rejects for drift and failover — and it is the
same structural reason D62 records for `llm_failovers` ("attribution is impossible from a
wave-shared client").

## 4. The three doors out of that, and why each is shut

**Door 1 — `record_attempt` writes the column from an `AttemptRow` field.** Needs
`repository.py` (unowned, available to me) **and a producer**. AST over `src/`: `AttemptRow(` is
constructed at exactly **`cli.py:3715`** (`_TransformSink.__call__`) and **`cli.py:6053`**
(`_AttemptWriter.record`) — `repository.py:1097` is the `iter_attempts` *read*. `cli.py` is
*[EDITORIAL, 2026-08-26, round H lane W2. **Two of the three citations in the sentence above were
repointed; the third was correct and is untouched.** All numbers below re-measured at `5f14ca0`,
the ref this lane's change is authored against. As written at `53e5d8d` the sentence read
`cli.py:3698`, `cli.py:6028` (`_BuildAttemptSink.record`) and `repository.py:1097`. (i) `3698` had
rotted to **3715** and still resolved to the right symbol. (ii) `6028` had rotted to **6053**, and
more seriously **the name `_BuildAttemptSink` does not resolve at all** — no such symbol exists in
the tree; the class is `_AttemptWriter` (`cli.py:6008` at `5f14ca0`). This lane found it only
because a test import of the old name raised, which is the shape of rot a reader cannot catch by
eye. (iii) `repository.py:1097` **re-measures correct at `5f14ca0` and was NOT edited** — it is
recorded here so a later sweep does not "fix" a right number. Forward notice, because the commit
carrying this marker moves two of them itself: after it lands, the `cli.py` construction sits at
**6059** and the `repository.py` read at **1101**, both shifted by lines this same commit inserts
above them. Nothing else in this document is edited.]*
W13's this round. Landing the field and the column without a producer would create a **fifth**
declared-and-never-assigned field beside `llm_cache`, `llm_cache_mode`, `llm_policy` and the two
`LlmFindingSink` callbacks — the exact anti-pattern D79 and this entry exist to record. I did not
do it.

**Door 2 — a per-attempt scoped client, so `on_hit` closes over `(repo_id, phase, attempt)`.**
`worker_context()` is the one place in `context.py` that knows all three, and `scoped()` already
propagates `on_hit`. **This door closed while my lane was running.** `53e5d8d` landed
`test_the_client_handed_to_a_worker_is_the_one_this_file_drives`
(`tests/test_run_context_llm_cache.py:376`), whose assertion is `worker.llm is ctx.model_client`.
Handing a worker a per-attempt *view* reddens it by construction. Worth recording that the same
commit **retracted** the claim my earlier reading rested on: the pre-`53e5d8d` `_ask` docstring
said `tests/test_runner.py` asserted that identity, and it did not — the sibling found and fixed
that itself. Either way the identity is now pinned, on purpose.

**Door 3 — buffer the hits and `UPDATE` them in at flush.** Defeated by ordering, independent of
attribution. In `PhaseRunner._drive`: `await self._drain_llm_findings(repo_id)` is
`runner.py:489`; the sink that writes the `attempts` row is `runner.py:520`. **The drain runs
before the row exists**, so the `UPDATE` matches zero rows. (`runner.py:394`'s end-of-wave
`_drain_llm_findings(None)` does run after every sink, and `flush()` documents re-buffering what
did not land — so a *retry-until-the-row-appears* mechanism is conceivable — but making that the
contract means editing `_drive`, the phase-wave driver, which the brief forbids, and proving a
"lands eventually" property is a far larger verification than this lane's remit.)

## 5. The brief's premise, checked against its own cited source

The brief says: *"Wire `on_hit` so a hit increments the column, following the pattern
`RunContext.__post_init__` already uses."* That presumes gap (b) is the only gap. **D62 — which
the brief itself cites — says otherwise, and D62 is right**: leg (a), "`record_attempt`'s `INSERT
INTO attempts` still omits the column", re-measures true at `53e5d8d` two independent ways
(§2 above). Per the primary-source rule I acted on D62 rather than on the brief, and this is the
correction. Wiring `on_hit` alone would produce a callback whose every effect is discarded — a
sixth dead wire, and one that *looks* like the fix.

## 6. Agent Recommendation (a recommendation, not a directive, and not implemented)

The attribution problem has a solution that needs neither a scoped client nor a `ContextVar`
(which would be a module global with extra steps, against Guardrail 3): **carry the flag on the
data that already flows from the call to the row.** `_replay` already rewrites the usage
(`usage=record.usage.model_copy(update={"cost_usd": 0.0})`); a `TokenUsage.cache_hit: bool = False`
set to `True` there would ride `ModelResponse.usage` → the worker's accumulated
`WorkerResult.usage` → `_TransformSink` → `AttemptRow`, attributed exactly, with no ambient
context and no ordering hazard. Three things a lane taking this must not miss:

1. `workers/base.py:228 accumulate()` **sums** fields. A boolean must be OR-ed there explicitly or
   it is silently dropped at the first multi-call rung — declared-and-never-assigned again.
2. It needs `cli.py` (both producers) **and** `repository.py` (field + `INSERT` column). It is a
   one-commit, one-author change across three files by the "a multi-site correction split across
   authors ships partial wording" rule.
3. Semantics for a multi-call attempt (2 misses + 1 hit) are **not** settled by SPEC §11.6's
   sentence and need a ruling before code. `models/tasks.py:62` constrains the neighbourhood:
   whatever is added must not make `cost_usd == 0` a cache signal.

It also does not use `on_hit` at all, which then wants an explicit decision: keep `on_hit` as the
out-of-band signal `cache.py:396` advertises, or retire it. I did not decide that.

## 7. Documentation — reported, not edited

`docs/INTEGRATION_HONESTY.md` is **W13's file** this round (D81), so per the brief I report the
text rather than editing the ledger. `docs/SPEC.md` I did not touch either, for the reason in (b).

**(a) D62's heading status: `OPEN`, unchanged.** Nothing closed. My lane adds no fix, and the two
legs W11 recorded both re-measure true.

**(b) Proposed extension to W11's dated marker — an annotation, appended after W11's parenthetical,
not a rewrite of one word of it:**

> *(2026-08-25, round F lane W15 — **annotation only; status stays `OPEN`.** Both legs of W11's
> annotation re-measure true at `53e5d8d`, and the symptom is now measured behaviourally rather
> than inferred: a `RunContext` assembled exactly as `cli.py` assembles one, given two identical
> `complete()` calls, invokes the backend **once**, writes **one** `llm_cache` row, and leaves
> `attempts.llm_cache_hit` at **0** (repro:
> `.superpowers/sdd/handoff-round-f/lanes/W15/repro_llm_cache_hit_column.py`; the same file
> injects a `1` through the real writer and re-reads it, so the `0` is the code and not the read).
> Leg (a)'s class is **larger than this entry states and the entry's five are all correct**:
> diffing `record_attempt`'s parsed `INSERT` column list against `PRAGMA table_info(attempts)`
> gives **31 declared, 24 named**, i.e. seven unwritable columns — this entry's five plus
> `integration_ref` (written by a separate `UPDATE` at `cli.py:6062`) and `container_id`.
> **What is new is that leg (b) is now harder than "unowned", and one door shut this round.**
> `LlmCallRecord` carries no `run_id`/`repo_id`/`phase`/`attempt`, and one `CachingModelClient`
> serves a whole wave, so `on_hit` cannot attribute a hit to an attempt — the same wave-shared-client
> argument this entry already accepts for `llm_failovers`. The one seam that could have supplied
> the identity was `worker_context()` handing a per-attempt `scoped()` view; `53e5d8d` landed
> `tests/test_run_context_llm_cache.py::test_the_client_handed_to_a_worker_is_the_one_this_file_drives`,
> which asserts `worker.llm is ctx.model_client`, so that view is now a deliberate regression.
> And a buffer-then-`UPDATE` cannot help either, for a reason independent of attribution:
> `PhaseRunner._drive` drains at `runner.py:489` and writes the `attempts` row at `:520`, so the
> flush precedes the row. **Consequence for the entry's framing: `llm_cache_hit` is no longer
> "smaller and unowned" than `llm_failovers` — as of `cac537d` it is a live falsity rather than an
> unwritten default, and it needs the same kind of cross-lane change.** W15 changed no code; the
> route it recommends instead is a `TokenUsage`-borne flag set in `cache._replay`, which attributes
> by data flow.)*

**(c) `docs/SPEC.md` §11.6, line 7181 at `53e5d8d`:**

> `- Cache hits set `attempts.llm_cache_hit = 1` and `cost_usd = 0`, so cost accounting stays honest.`

**W12's measurement is confirmed and I add the missing half.** The sentence is a conjunction and
the two halves have *different* truth values: `cost_usd = 0` **is** true and exercised —
`_replay` returns `record.usage.model_copy(update={"cost_usd": 0.0})` — while
`attempts.llm_cache_hit = 1` is **false on every path**, measured above. It is not true under any
condition, so there is no conditional wording to add. **My recommendation is that it not be
edited**: it states the intended invariant, D62 records that the code does not meet it, and
softening a SPEC sentence to match code the project intends to fix is how the `supports_effort`
class of defect is manufactured. Two neighbours to keep consistent if a later lane disagrees:
§11.2 (`:6785`) lists `llm_cache_hit` among six columns "written in **one** transaction", which
`record_attempt` plus `cli.py:6062`'s separate `UPDATE` already does not satisfy; and §12.24
(`:7351`) / §12.44 (`:7371`) assert `llm_cache_hit = 0` for a `local` profile — which still passes
for the wrong reason, exactly as D62's body says.

## 8. Scoping disclosed

* **Probe:** `pytest tests/repro_llm_cache_hit_column.py -q -s` inside my own detached worktree
  (`/tmp/claude-1000/-home-redmage-swe-repo-harness/roundf-W15`), cwd inside the tree — the
  structural import isolation the protocol names (pyproject `pythonpath=["src"]` + conftest's
  `sys.path.insert`), so no `env -i` pin was required. **2 passed**, no `-k` filter.
* **Regression check:** `pytest tests/test_run_context_llm_cache.py tests/test_llm_cache.py -q`,
  **no `-k` filter**, in the same worktree at `53e5d8d` → **28 passed**. I did **not** run the
  whole suite; nothing I did could move it, because I changed no file.
* **mypy: not run, and it would be uninformative.** `pyproject.toml` pairs `strict` with
  `packages = ["fleet"]`; I changed nothing under `src/fleet/`, so there is no changed line for it
  to check. (Had I shipped the `context.py` change, mypy with no path arguments *would* have
  covered it — the brief is right about that; it is moot only because there is no change.)
* **Mutation testing: not applicable and deliberately not faked.** Rule 12's old-passes/new-fails
  obligation attaches to a test I add or rewrite. I added none to the tree. Fabricating a mutation
  battery for a patch that does not exist would be the "instrument asserting layout rather than
  meaning" failure in its purest form.
* **Files changed in the repo: none.** `git status --porcelain` in the worktree is empty; there is
  no patch, so no `git apply --check` was needed. The only artefacts are this report and the repro
  script, both under `.superpowers/sdd/handoff-round-f/lanes/W15/`.
* **Coordination:** I edited neither `src/fleet/cli.py` nor `docs/INTEGRATION_HONESTY.md` (W13),
  nor `tests/test_runner.py` / `tests/test_run_context_llm_cache.py` (W14). §7(b) is text for the
  orchestrator to sequence into D62, not a hunk I staged.

## 9. Questions for the orchestrator / research lane

1. **Does the `TokenUsage.cache_hit` route (§6) get a ruling before a lane implements it?** It
   needs `cli.py` + `repository.py` + `workers/base.py::accumulate` in **one** commit by one
   author, and it needs a decision on multi-call-attempt semantics that SPEC §11.6 does not settle.
2. **Is `on_hit` retained or retired** if the flag travels on `TokenUsage` instead?
   `cache.py:396` currently advertises `on_hit` as the reason §6 has the column at all; if the
   answer is "retired", that docstring is a doc edit that must land in the same change.
