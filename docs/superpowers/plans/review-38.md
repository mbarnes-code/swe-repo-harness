# Review 38 — audit of `68a41ff..f12a954` (`8464dc6`, `f12a954`)

Rubric: this project's recurring defect is **claims that outrun their evidence** — an
operator-facing message asserting an outcome the code does not produce, an ADR documenting changes
that do not exist, a test docstring blaming history the commit itself created. This audit hunts
that disease specifically. Every `file:line` below was read at `f12a954` unless stated otherwise.

`pytest` was not run (harness constraint). The suite counts in both commit messages (1159, 1162) are
therefore **unverified, not contradicted**.

---

## Verdict

**The code in this range is sound. The prose written about it is not.**

Every functional change verifies: the `re.escape` fix is complete and its regression test is real;
the D26/D27 git work drives real git and fixes what it says; the `FileNotFoundError` replacement at
the three VCS sites removes a genuinely impossible `exit 127` guess; the `ScriptedRunner`
producibility invariant derives its ground truth from a live `run()` call rather than a docstring.
Almost every line citation in both **commit messages** resolves exactly. `ADR-0068`'s `CacheMiss`
half is fully backed by code and tests. `D48` is the most rigorous entry in the range — its core
defect is real and eleven of its twelve `src/` citations are exact.

The findings concentrate in three places: (1) the documentation lane of `8464dc6`, which describes
the tree **as it was before the same commit's code lane landed**; (2) two operator-facing strings in
`buildverify.py` that survived the fixes around them; (3) verification language ("confirmed by
direct read", "convergent across three references") attached to citations that were not read.

**3 Critical · 8 Important · 9 Minor.**

---

## CRITICAL

### C1 — `docs/INTEGRATION_HONESTY.md:2079-2128` (D46), `docs/PROGRESS.md §36`, `§37`: the docs lane of `8464dc6` documents the pre-fix tree, and instructs the next worker not to believe the fix

This is the rubric's disease in its purest form to date, and it is systemic rather than a slip.

**D46 is marked `OPEN`, `Severity: high`**, and closes with:

> "The working tree at the time of writing carries **uncommitted** changes to
> `src/fleet/workers/buildverify.py` ... that appear, by inspection only, to address exactly this: a
> `_invocation_name` helper builds a fresh `uuid.uuid4().hex[:8]`-suffixed name per call ...
> **This is claimed, not verified** ... Re-derive from `git diff` and the test suite once it lands,
> and close this entry only then; **do not treat this paragraph as evidence of a fix already
> shipped.**"

**Contradicting evidence, measured:**

```
git log --oneline -S"_invocation_name" -- src/fleet/workers/buildverify.py  →  8464dc6
git log --oneline -S"D46 — OPEN"       -- docs/INTEGRATION_HONESTY.md       →  8464dc6
```

D46 and the fix it calls "uncommitted" are **the same commit**. `_invocation_name` is at
`buildverify.py:354-370`, used at `:777` (probe) and `:1088` (build step). The instruction "do not
treat this paragraph as evidence of a fix already shipped" is now exactly backwards: the fix shipped
in the commit that carries the warning.

Three further D46 claims are refuted by that same commit:

1. **The central mechanism.** D46: "`buildverify.py`'s `_DOCKER_CANNOT_RUN` branch does not
   distinguish 'daemon unreachable' from 'name already in use,' both being an unreadable one-line
   docker stderr." `buildverify.py:438-442`, in the same commit, cites the *same* source
   (`68a41ff` body / review-36 I2-I3) to say there is nothing to distinguish: an unreachable daemon
   **exits 1, not 125**. The self-inflicted permanent-125 scenario D46 builds on cannot occur.
2. **"Would a test catch it? No."** `tests/test_workers_build.py:933-965`
   (`test_a_transient_retry_of_the_same_rung_never_reuses_a_container_name`) is exactly that test,
   added by the same commit.
3. **"see review-36 I2 for the adjacent fixture defect ... that would need correcting alongside
   it."** The `DAEMON_GONE` → `CONTAINER_NAME_CONFLICT` fixture correction is in the same commit
   (`tests/test_workers_build.py:1113-1117`).

**`PROGRESS.md §36` carries the same defect in its "What is still NOT proven" and "Next subagent
task" sections** — five items listed as unfixed were fixed by `8464dc6` itself:

| §36 says | Reality at `8464dc6` |
|---|---|
| "C1 and C2 are unfixed"; next task "land C1 and C2's corrected strings/docstrings" | Both landed. `base.py:161-171` already carries the `max_transient_retries` bound; `buildverify.py:474-489` already carries the corrected 125 prose. `git show 8464dc6:src/fleet/workers/base.py \| grep "no repair prompted"` → no hits |
| "`cli.py:3634`'s `GitCommandError` construction still omits `started`" | `cli.py:3640` passes `started=result.started` — added by this commit |
| I4: "`clone.py:700-706` ... remain uncorrected" | `clone.py:700-713` carries the corrected history verbatim, added by this commit. Only the `base.py` half survives, and at `base.py:180-186`, not the cited `:176-179` |
| M4: "`clone.py:723-745`'s `_is_shallow` docstring claims a 'measured across git 2.20.4 → 2.49.1' range" | Removed by this commit. `clone.py:731-758` now says "no version range is claimed here", citing git 2.43.0 |
| I1 cite `base.py:161-164` | The sentence is at `base.py:180`; `:161-164` is the *corrected* bullet |

**`PROGRESS.md §37` propagates it forward** — "Next subagent task" #2: *"Everything in §36's list
remains unstarted — C1/C2's corrected strings in `buildverify.py`/`base.py`…"* — one commit later,
re-asserting work that had already landed.

**Why this is Critical rather than an attribution nit.** These are the operative planning documents.
A future worker handed D46 (`OPEN`, `high`) or §36's next-task list will re-implement
`_invocation_name`, re-write the two docstrings, and re-add the tests — against a mechanism
(`_DOCKER_CANNOT_RUN` "cannot distinguish") the code explicitly refutes. §36's own caveat
("citations to concurrently-modified files are to their state at the moment read") explains the
cause but does not contain the damage, because nothing in the entries flags itself as possibly
stale at the point a reader acts on it.

**Honest replacement.**
- D46 → `CLOSED — FIXED in 8464dc6`: "`_invocation_name` (`buildverify.py:354-370`) gives every
  `docker run` a fresh per-call token, pinned by `tests/test_workers_build.py:933-965`. The premise
  above is also superseded: an unreachable daemon exits 1, so the 125 branch was never ambiguous;
  the residual branch covers externally-caused collisions only."
- §36 → "C1, C2, I4(clone), I5 and M4 landed in this same commit. What genuinely remains: ADR-0067's
  four parts in `cli.py`, `base.py:180-186`'s false history, and the D34/D35/D36/D41 staleness
  re-audit."
- **Process**: a commit that lands a docs lane and a code lane together must reconcile them before
  committing. A docs worker's "uncommitted, unverified" hedge is only honest while it is true, and
  it stops being true at `git commit`.

### C2 — `src/fleet/workers/buildverify.py:474-489`: the build-step 125 operator message names as "the usual cause" the cause the same commit eliminated, then contradicts itself

`_DOCKER_CANNOT_RUN_EXPLAINED` is prefixed to every build-step 125 an operator reads in
`last_error`. Two defects in one string.

**(a) The ranking (`:477-479`).**

> "Look at the host: a surviving container from a prior invocation colliding on `--name=` **is the
> usual cause**"

`8464dc6` added `_invocation_name` (`:354-370`), a fresh `uuid4().hex[:8]` per call, precisely so a
retry can never reuse a name. Its own inline comment at `:441-445` says so: the token "removes the
SELF-inflicted version of the collision; this branch still exists for a **residual or
externally-caused** one." After the fix, a `--name=` collision is the *least* likely cause from this
worker, not the usual one.

Worse, the sibling 125 message in the same file — the probe's, rewritten by `f12a954` at
`:846-853` — denies that any ranking is possible: *"Docker's exit code alone cannot tell these
apart; only its stderr text does"*, and `f12a954`'s message confirms no stderr classifier was built.
Two operator messages for one exit code, in one file, one ranking causes and one saying ranking is
impossible. `f12a954` fixed the probe's and left the build step's.

**(b) The self-contradiction (`:484-485`).**

> "a **daemon still gone** past that many retries makes the NEXT occurrence substantive"

Six lines earlier, in the same string literal: *"an unreachable daemon exits 1, not 125 —
measured"*. A daemon that is gone cannot produce the first 125, let alone a fifth; the escalation
path this describes to an operator does not exist. The code comment covering identical logic at
`:450-451` already gets it right — "a 125 that keeps recurring still escalates" — so the correct
wording was in the commit and simply was not carried into the operator string. `f12a954`'s message
claims it "corrected" the unreachable-daemon prose; it corrected the probe's message and left this
one's downstream reasoning standing on the retracted premise.

**Honest replacement.**
> "Look at the host. Several conditions all exit 125 — a leftover container holding this name, an
> image that will not resolve, a malformed `container_memory`/`container_cpus`, a nonexistent
> `--network`, a non-absolute `--workdir` — and the exit code cannot tell them apart; the docker
> stderr below is the only thing that can. (An unreachable daemon is not among them: measured, it
> exits 1.) Re-queued on the same rung as TRANSIENT_INFRA, but neither half of that is free
> forever: it costs no attempt only up to `RetryPolicy.max_transient_retries` (default 4), after
> which **a 125 that keeps recurring** is charged a rung like any other; and on rungs 2-3 this same
> retry still triggers a diagnosis call."

### C3 — `src/fleet/workers/buildverify.py:1004-1006`: the orphan-container leak is closed on paper by a mechanism that never runs

**Claim.** `on_cancel`'s new docstring, in its otherwise commendable "**Not the whole of D32**"
paragraph:

> "a container orphaned by a plain timeout (rather than a cancel or a name collision) is not swept
> here; **it waits for `ContainerSandbox.reap` at `fleet resume`.**"

**Contradicting evidence.** Two facts, both established *inside this same range*:

1. `grep -rn "\.reap(" src/` returns **zero hits**. `ContainerSandbox.reap`
   (`sandbox/container.py:240`) has no caller anywhere in `src/` — the same "implemented, zero call
   sites" state D32 recorded for `list_by_prefix`, which this docstring cites two paragraphs
   earlier.
2. `fleet resume` terminates at `_unavailable("resume", …)` — `cli.py:9792`. This range states that
   fact twice more: `tests/test_cli.py:1176` ("`fleet resume` does not exist, so PUBLISH is the only
   recovery route") and D48 at `docs/INTEGRATION_HONESTY.md:2226-2228`.

A timeout-orphaned container is therefore swept by nothing, ever. The paragraph correctly identifies
a real scope gap and then closes it with a doubly dead mechanism, which is worse than leaving it
open — a reader takes "it waits for reap" as "handled later".

**Honest replacement.** "...is not swept here, and is not swept anywhere: `ContainerSandbox.reap`
has no caller in `src/`, and `fleet resume` — where SPEC §11.5 step 2 puts the startup reap — is
`_unavailable` (`cli.py:9792`). That half of D32 is open, and a timeout leaves a live container
behind until an operator removes it by hand."

---

## IMPORTANT

### I1 — `src/fleet/llm/cache.py:91-105`: the `CacheMiss` change is correct; three claims around it are broader than the code

The change and its reasoning are good. Three specific assertions are not supported.

**(a) "a miss is fatal" is still asserted, and is still false.** `cache.py:92-94`, unchanged:
"Replay mode's whole purpose is that a miss is fatal." Trace it: `CacheMiss` escapes `run()` →
`BaseWorker._run_one`'s `except Exception` (`base.py:897`) → `error_from_exception`
(`base.py:522-534`) → `classify_exception` returns `FailureClass.UNKNOWN` (`base.py:507-519`, no arm
matches) → `is_retryable(UNKNOWN)` is `True` (`UNKNOWN ∉ NON_RETRYABLE`, `base.py:130-141`). The
result is a `WorkerResult(status="failed")` that is **retryable**, on a wave that **continues** — and
the ladder will now re-run and re-miss. Louder than being swallowed; not fatal.
*Honest wording:* "a miss fails the unit loudly and is recorded as `UNKNOWN`/retryable; it does not
halt the run."

**(b) The change discards a fully measured build result, and the docstring argues against exactly
that harm without naming it.** In `buildverify.run`, `_diagnose` is called at `:969` — *after*
`error = error_from_proc(result, unit=unit)` at `:968` and *before* the `WorkerResult` is built at
`:970-978`. A `CacheMiss` from `_diagnose` therefore destroys the whole `BuildverifyOutput`
(`build_ok`, `test_ok`, `steps`, log refs) *and* the correctly classified build error; the repo
records `UNKNOWN`. Meanwhile `cache.py:96-99` justifies the bare-except pattern because "losing that
advice must never turn a recorded build/repair failure into an unrecorded worker crash." Under
`--llm-cache read-only`, that is now precisely what happens. It may still be the right trade — but
the docstring makes the case for the change by invoking a harm the change causes.
*Honest addition:* "The cost, stated: a `CacheMiss` raised from an advice call inside `run()`
discards the `WorkerResult` that call was decorating — the measured build verdict included — and the
repo records `UNKNOWN`. Replay integrity is judged worth that."

**(c) The guarantee has a hole documented in the same commit.** D48, swept into `f12a954` at
`docs/INTEGRATION_HONESTY.md:2246-2252`: the checkpoint layer sits *above* the cache, so on a
re-entered run "the unit is marked complete, the model is never called, and **the miss never
happens**" (`runner.py:458`, `:474-475`). `f12a954`'s message says the change makes
`--llm-cache read-only` fail loud on replay drift, unqualified. It does so only for calls that reach
the cache; a resumed run still reports a clean replay for every call the checkpoint skipped. The
qualification is inside the same commit, in an entry the commit message does not mention, with no
cross-reference in either direction.
*Honest addition to ADR-0068 / D48:* "`CacheMiss` makes a read-only **call** fail loud; it cannot
make a **skipped unit** fail loud. A resume that reuses a checkpoint written under a different
prompt or model id satisfies `--llm-cache read-only` vacuously — no call is made, so no miss occurs
(D48; `runner.py:458,474`)."

### I2 — `f12a954` commit message: the `except LlmError:` enumeration is incomplete, and "classify.py's outcome is identical" is not exact

**Claim.** "The bare `except LlmError:` degrade-and-continue pattern in buildverify/buildgen/prwriter
was swallowing it ... No isinstance checks exist anywhere; **classify.py's outcome is identical
before and after.**"

**Evidence.** `grep -rn 'except.*LlmError' src/` returns eight clauses in six files: the four named
(`prwriter.py:414`, `:422`, `buildverify.py:1152`, `buildgen.py:454`, `:566`) **plus**
`rewrite.py:472`, `classify.py:170`, and `cli.py:489`.

- `rewrite.py:472` re-raised `CacheMiss` as `WorkerRepairError`. Benign — `WorkerRepairError`
  (`rewrite.py:90`, a `RuntimeError`) has no catcher either, so both routes end at `_run_one` with
  `UNKNOWN` — but not stated.
- `cli.py:489` (`except LlmError as exc: _fail(str(exc), ExitCode.USAGE)`) would have given a
  CLI-level `CacheMiss` a clean exit code. Unreachable in practice (`cli.py` makes no `.complete()`
  call), but that is a sentence, not a silence.
- **`classify.py:170` is not identical.** `_error_for` (`classify.py:238-264`) writes
  `stderr_tail=redact_text(str(exc))`; `error_from_exception` (`base.py:532`) writes
  `stderr_tail=str(exc)`. `failure_class` (`UNKNOWN`), `retryable` (`True`) and `exception_type` do
  match — 3 of 4, and the fourth is a dropped redaction pass.

*Honest replacement:* "classify.py's `failure_class`, `retryable` and `exception_type` are
unchanged; the message now bypasses `redact_text`. `rewrite.py:472` and `cli.py:489` also name
`LlmError`; neither changes outcome."

"No isinstance checks exist anywhere" **verifies** — `grep -rn 'isinstance.*LlmError\|isinstance.*CacheMiss' src/ tests/` is empty.

### I3 — `docs/INTEGRATION_HONESTY.md:2156` (D47): a verification claim attached to a citation that was never read

**Claim.** D47: "`_diagnose` fires on rungs 2 and 3 for every non-ok build/test step, a 125 included
(**`buildverify.py:873-874`, confirmed by direct read of the call site**)."

**Contradicting evidence.** `buildverify.py:873-874` is prose inside the C-toolchain probe's stderr
string — *"absolute path>`. A `CC` in the harness's own environment cannot help — buildverify passes
no env to either container…"*. It is not a call site, and it was prose at `8464dc6` too, so this is
not line drift. The actual call site is `buildverify.py:969`, guarded by `:967`
(`if not result.ok and not nothing_to_test:`).

The underlying claim is **true** — verified independently at `:967-969` and `:451-453`, no
failure-class branch. The defect is the phrase "confirmed by direct read of the call site" attached
to a line that is not one. That phrase is the exact evidence-inflation this review round exists to
catch, and it is more damaging than a wrong number, because it tells the next reader not to re-check.

Two sibling citations in D47 are wrong the same way (wrong at authoring, not drifted):
`response.usage` cited as `buildverify.py:1050` (actual `:1158`; `:1050` is inside `_bazel_argv`),
and `tests/test_workers_build.py:2340` for the `out.diagnosis == ""` assertion (actual `:2357`;
`:2340` is `out = result.output`).

*Honest replacement:* cite `buildverify.py:967-969`, and drop "confirmed by direct read" unless the
line was read.

### I4 — `docs/DECISIONS.md` ADR-0068: the `transform_repair` attribution is half-wrong, and the true finding is stronger

**Claim.** "The 'proposes an edit, code applies it' sentence instead describes `transform_repair`
(SPEC.md:185, :856, :6265 — a different role, on a different ladder, **§9's repair loop for
relocated files**)."

**Evidence.**
- "proposes an edit, code applies it" — ✅ `propose_repair` (`calls.py:371-375`) returns
  `LlmPatchProposal`; `rewrite.py:397-408` applies via `land_patches`.
- "reads the Bazel error" — ❌ the `transform_repair` prompt (`calls.py:171-182`) is *"You repair a
  source file whose deterministic rewrite failed"*; its evidence is a `RULE_MISS` or a
  `git apply --check` stderr (`rewrite.py:325-330`, `:362-366`).
- "re-runs the build" — ❌ `grep -ci bazel src/fleet/workers/rewrite.py` → **0**. The rewrite worker
  commits patches; it never invokes a build.
- "§9's repair loop" — ❌ `SPEC.md:5878` is `## 9. Configuration`. `transform_repair`'s ladder is
  §3.2 (`SPEC.md:783`, `:856`), which `calls.py:374`'s own docstring says.

*Honest replacement — materially stronger, and it matters because the current wording implies the
behaviour exists somewhere:* "The SPEC sentence conflates two roles. `transform_repair` (§3.2)
supplies the 'model proposes a diff, code applies it' half (`calls.py:371-375`,
`rewrite.py:397-408`), but it never reads a Bazel error and never re-runs a build. **No role in the
codebase performs the described apply-and-rerun over a Bazel failure.**"

### I5 — `docs/INTEGRATION_HONESTY.md:2207-2208` (D48): "the six verbs" is four, and two of the six are dead for the reason D48 faults `resume` for

**Claim.** "Config drift is gated on `fleet resume`, which is `_unavailable`; **the six verbs** that
actually re-enter an interrupted run go through `_phase_preflight`."

**Evidence.** `_phase_preflight` has **seven** call sites (`cli.py:2361, 2390, 2431, 2470, 3061,
8575, 10356`). Of D48's own six, two are `_unavailable` immediately after preflight — `plan` at
`cli.py:2363`, `migrate` at `cli.py:2475` — which is *precisely* the property D48 uses to indict
`resume`. (`stubs resolve` also preflights and is also `_unavailable`, `cli.py:10358`.) The verbs
that actually re-enter a run are **four**: `build`, `verify`, `transform`, `pr`.

The defect is unaffected — four live verbs reading no drift digest is still four too many — but an
entry whose whole argument is "the guard sits on a dead-end path" must not itself miscount which
paths are dead.

*Honest replacement:* "Seven commands share `_phase_preflight` (`cli.py:866-878`), which checks
schema, resolves the run, and refuses a concurrent mirror — and never reads `runs.config_digests`.
Three of the seven (`plan` `:2363`, `migrate` `:2475`, `stubs resolve` `:10358`) are themselves
`_unavailable`; the **four** that re-enter an interrupted run today are `build`, `verify`,
`transform` and `pr`."

### I6 — `tests/test_workers_build.py`, `test_a_probe_the_fleets_own_deadline_killed_is_not_reported_as_a_missing_compiler`: the docstring reintroduces the unbounded claim that is `8464dc6`'s headline

**Claim (test docstring).** "`clock_failure` ... reads `started=False` as `TRANSIENT_INFRA` — the
fleet's clock, not the repo's fault, **retried free of an attempt**."

**Contradicting evidence.** That is the retracted D34 sentence, unbounded, and removing it is what
`8464dc6`'s message leads with — *"Worker A found four instances… All corrected against the real
ladder: retry.py caps free retries at 4, then charges."* The four are `base.py:164-171`,
`buildverify.py:145`, `:450-453`, `:481-486` (verified: `retry.py:202` guard,
`DEFAULT_MAX_TRANSIENT_RETRIES = 4` at `retry.py:65`). The **production** message this very test
exercises — `_c_toolchain_gate`'s `never_started` string, `buildverify.py:791-798` — states the cap
correctly. The test that pins it does not. Same commit, same wave, same sentence.

*Honest replacement:* "…retried on the same rung with no attempt charged, up to
`RetryPolicy.max_transient_retries` (4); past that cap the identical clock failure is charged like
any other. This test drives one occurrence, so it pins the free half only."

### I7 — `tests/test_llm_cache.py:366-383`: the test does not exercise the regression its docstring names

**Claim (docstring).** "Pins the Rule 11 fix: `CacheMiss` must NOT be catchable by the bare
`except LlmError:` degrade-and-continue pattern **every worker's advice-call site** uses
(`buildverify.py`, `buildgen.py`, `prwriter.py`)…"

**Evidence.** Every assertion is about the class hierarchy and the cache client in isolation:
`assert not issubclass(CacheMiss, LlmError)` at `:372`, then a `try/except` at `:377-382` already
decided by that first assert. No worker is constructed; `buildverify.py:1152` and its siblings never
execute. Re-adding `except (LlmError, CacheMiss):` at `buildverify.py:1152` restores exactly the
swallow the docstring names, and this test still passes.

Not vacuous — reverting `class CacheMiss(Exception)` fails it, so it guards the line it was written
for. But it is not the regression test its docstring claims.

*Honest replacement:* narrow the docstring to "pins the class hierarchy that makes those sites
safe", and note the sites themselves are untested — or add the real test: drive
`BuildverifyWorker.run` to a rung-2 build failure under a `read-only` cache and assert the returned
`WorkerResult` records the miss.

### I8 — `CLAUDE.md` and `.claude/settings.json`: `8464dc6` ships a new project directive and three permissions under a message that mentions neither

`git log 68a41ff..f12a954 -- CLAUDE.md .claude/settings.json` → `8464dc6` only. That commit added to
`CLAUDE.md`: a whole new **§6 "Build & Test Operations"** (five operational rules, including the
pytest-serialization rule and the `FLEET_*` export prohibition), a `tools/bin/` line in §5, and a new
**Guardrail 6 "Measurement Discipline & the Multi-Agent Audit Hazard"**. To `.claude/settings.json`:
three new `Bash` permissions. `8464dc6`'s message enumerates its work down to "new D46; new
PROGRESS.md 36" and names none of it.

Same attribution failure as the swept §37, with more force: `CLAUDE.md` is the **directive
authority** Guardrail 1 makes lineage claims against. A future subagent will cite "Guardrail 6" as a
hard requirement, and the only record of its provenance is a commit message about container names
and `CacheMiss`.

Content checks out: `tools/bin/` holds exactly the seven named wrappers (`ast-grep bazel cargo
gazelle gh go rustc`); `tests/conftest.py:500` writes the "bazel disk" separator the new rule cites.

---

## MINOR

**M1 — `docs/INTEGRATION_HONESTY.md:2250-2251` (D48): a single-source finding presented as convergent
across three references.** D48 quotes ADR-0069 §5's "deterministic gate strictly before LLM
judgment" as "a convergent finding **across three references**". `DECISIONS.md:5166` cites exactly
one: *"Visa's S5 prefilter"*. (The "three unrelated mechanisms" framing belongs to a different §5
bullet — the per-agent context ceiling.) In an entry that elsewhere applies ADR-0069 §6's own
citation policy rigorously, inflating one source to three is the disease in miniature.
*Replacement:* "ADR-0069 §5 records 'deterministic gate strictly before LLM judgment' (from Visa's
S5 prefilter)…"

**M2 — `SPEC.md` citation disagrees with itself inside one commit.** `f12a954`'s message says
`SPEC.md:1389-1391`; `INTEGRATION_HONESTY.md:2176` says `1390-1392`. The paragraph is **1389-1392**
(`:1389` opens "**LLM slots.**", `:1392` closes "never asked whether the build passed"). The doc's
citation is the better of the two; the commit message's swallows the header and truncates the
verdict clause. `SPEC.md:6266` is exact in both.

**M3 — D47/ADR-0068 overstate "both name `build_diagnosis`".** `grep -n build_diagnosis
docs/SPEC.md` returns **only** `:6266`. `:1390` says "build-failure diagnosis on attempts 2–3"
— it describes the slot without using the role identifier. The SPEC does mandate it; it does not
*name* it in both places.

**M4 — ADR-0069: "five semaphore classes in `Limits`".** `budgets.py:960-965` declares three
`asyncio.Semaphore` fields (`git_net`, `subprocess`, `docker`), one `Mapping[ModelTier,
asyncio.Semaphore]` (`llm`), an `Executor` (`cpu_pool`) and a `CostLedger` (`ledger`). "Five"
matches neither the field count nor the live-object count — in an ADR whose §37 write-up claims
every `src/fleet` citation was re-derived.
*Replacement:* "four semaphore ceilings plus a CPU pool executor (`budgets.py:960-965`); the `llm`
ceiling is per-tier."

**M5 — ADR-0068 / D47 line numbers are systematically one commit stale.** They cite the file as it
was at `8464dc6`, before the edit the ADR itself documents: `CacheMiss` at `cache.py:92` (actual
`:91`), the raise at `:469-470` (actual `:479-480`), `buildverify.py:1148` (actual `:1152`), the
`diagnosis` writes at `:1152-1153` (actual `:1156-1157`), `_diagnose` at `:1120-1153` (actual
`:1124-1158`). All substance verifies; the numbers point one commit back. Distinct from I3, which is
about citations that were wrong when written.

**M6 — `.claude/settings.json:62-64`: permission patterns use a space-glob where every sibling uses
`cmd:*`.** `"Bash(mypy *)"`, `"Bash(.venv/bin/mypy *)"` against ~60 entries of the
`"Bash(git diff:*)"` form. The documented prefix syntax is `cmd:*`; a space-glob is likely matched
literally and therefore inert. Flagged as a shape mismatch, not a proven bug — not verified by
running the harness. (`"Bash(.venv/bin/ruff check src/ tests/)"` is a valid exact-match entry.)

**M7 — `tests/test_cli.py:1326-1335`: the AST invariant only matches bare-name calls.** The walk
requires `isinstance(node.func, ast.Name)`, so `G.GitCommandError(...)` would be invisible. All
three current sites (`vcs/git.py:258`, `cli.py:3634`, `workers/clone.py:241`) are bare names, so
there is no false negative today and the test is non-vacuous. Widen to `ast.Attribute` before a
module imports it qualified.

**M8 — `src/fleet/workers/buildverify.py:1008-1010`: `suppress` wraps the loop, not each removal.**
```python
with contextlib.suppress(OSError, ValueError):
    for name in await sandbox.list_by_prefix(prefix):
        await sandbox.remove(name)
```
One `OSError` on the first `docker rm` silently abandons every remaining container, while the
docstring says the sweep "removes them all". The test exercises only the happy path. Moving
`suppress` inside the loop makes the docstring true.

**M9 — `tests/test_workers_build.py`, `CONTAINER_NAME_CONFLICT`: the fixture embeds a name shape
this worker can no longer emit.** The stderr quotes `"/fleet-…-acme-widget-1"` — the
pre-`_invocation_name` deterministic name, with no `-t<token>`. Consistent with the constant's
"residual or externally-caused" framing, but a reader will take it as a name this code produces. A
`-t<8 hex>` suffix would remove the ambiguity. (Related: `base.py:166` cites `retry.py:200-202` for
"default 4", the test cites `retry.py:132`, a sibling assertion cites `:202` — all resolve to real
code, but the literal `4` is `DEFAULT_MAX_TRANSIENT_RETRIES` at `retry.py:65`, which none names.)

---

## The swept `§37` / `ADR-0069` / `D48` — assessed as in-scope

**(a) Internally consistent, and consistent with the code?** Largely yes, and D48 is the strongest
entry in the range. Independently re-verified: `_unavailable("resume")` at `cli.py:9792` ✅ ·
`_phase_preflight` at `cli.py:866-878` containing exactly the three named refusals and no drift read
✅ · `checkpoint_is_current` caller counts (1 in `src/`, 3 in `tests/`) — **exact** ✅ ·
`base.py:237-244`'s docstring defect (it says `load` compares `written_schema_version`; `load`
compares the module-level `SCHEMA_VERSION`) ✅ · `_open_run`'s unconditional digest rewrite
(`cli.py:1906`) vs `upsert_run`'s `ON CONFLICT DO NOTHING` (`repository.py:1006-1009`) ✅. ADR-0069's
`src/fleet` citations spot-check clean except M4. Guardrail 1 is respected explicitly — ADR-0069's
status block and §4 heading both label the candidates *Agent Recommendations* and state they "confer
no authority". Defects: I5, M1, M4 above.

**(b) Does its presence under an unrelated commit message create a concrete problem beyond
attribution?** **Yes — one, and it is the reverse of the expected shape.** D48 does not contradict
the commit's other changes; it **bounds one of them, and the bound is lost**. `f12a954`'s message
asserts the `CacheMiss` change makes `--llm-cache read-only` fail loud on replay drift. D48 at
`:2246-2252` documents the path where that guarantee cannot apply — the checkpoint short-circuits
the unit, so no call is made and no miss can occur. Two entries in one commit, one bounding the
other, no cross-reference in either direction, and a commit message naming only the unbounded half.
Recorded as **I1(c)**.

Secondary: `PROGRESS.md §37` opens "**zero `src/` changes**", true of §37's own work and false of the
commit it landed in — anyone reading `git show f12a954` top-down meets a checkpoint declaring no
source changes immediately after three source diffs. The fix is not to rewrite §37 but to say in the
commit message that it is there.

---

## Claims that hold up

Stated because the rubric asks for a verdict, not padding.

- **The `re.escape` fix at `container.py:229` is complete.** `grep -rn '\-\-filter' src/` returns
  `container.py:228` plus three `cli.py` sites parsing the harness's *own* `--filter status=/wave=`
  option (`:9276`, `:10146`, `:10285`) — not docker's. `docker_run_argv`'s `--name=` (`:134`),
  `stop` (`:198`) and `rm --force` (`:206`) pass names as literal argv to `create_subprocess_exec`
  with no shell and no regex. **No other unescaped docker filter or shell-interpolated name exists
  anywhere in `src/`.** `re.escape`'s output is also valid under Go's RE2 (every escaped character
  is non-alphanumeric ASCII), so the fix works against the real daemon and not just against Python.
- **The regression test is not vacuous.** `RegexFilterRunner` (`tests/test_sandbox.py:361-397`)
  interprets the filter as a regex, as the daemon does. Without `re.escape`, prefix
  `fleet-<uuid>-my.repo.js-1-t` really does match `fleet-<uuid>-myXrepo.js-1-tbbbbbbbb`, so the
  assertion fails on revert. The two re-pinned tests (`test_sandbox.py:351`,
  `test_workers_build.py:1580-1596`) assert the escaped form **and**, in the second, re-interpret the
  argv through `re.compile` to check it still matches the rung's own containers — the property that
  actually matters.
- **Line citations in the commit messages.** `cache.py:91` ✅ · `container.py:229` ✅ ·
  `base.py:897` ✅ (verbatim, including the inline Rule 11 comment) · `buildverify.py:421` ✅
  (inside `classify_build_failure`, which opens at `:412`) · `worktree.py:36` = `def slug` ✅ with
  `_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")` at `:33` quoted correctly ✅ · `cli.py:3634` ✅ ·
  `retry.py:202` ✅ · `SPEC.md:6266` ✅.
- **"`_c_toolchain_gate`'s clock branch is NOT redundant."** ✅ `classify_build_failure`'s clock check
  is `buildverify.py:421`, reachable only via `error_from_proc` on a build/test step result; the
  probe returns its own `WorkerError` and never routes through it.
- **"`_diagnose` fires regardless of failure class."** ✅ `buildverify.py:967-969`, no failure-class
  branch.
- **D47's "zero readers" is true, and understated.** `grep -rn diagnosis src/` yields only the two
  field declarations (`buildverify.py:625-631`), the two writes (`:1156-1157`), and LLM plumbing.
  All three egress paths are genuinely closed, and a fourth is too: `BuildverifyWorker` is never
  dispatched top-level (only nested from `cli.py:4984`, `:5505`), the pipeline field-copies drop
  `diagnosis` (`cli.py:4986-4990`, `:5507-5510`), and `_AttemptWriter.record` persists `steps`, not
  the output object. The field is not merely un-branched-on — it never reaches disk, a log line, or
  an operator's terminal.
- **ADR-0068's `CacheMiss` half is fully backed** — the catch-site enumeration, the `.complete()`
  call-site inventory, `cli.py:489`'s unreachability, `classify.py:170`→`_error_for`'s else arm,
  `base.py:500-519`, `rewrite.py:472`/`:90`, and "SPEC.md was not edited" all verify.
- **`hash_object` / `blob_at` (D27).** Both return through `Git.text`/`Git.exec` with `.strip()`
  (`git.py:280`, `:501`), so the SHA comparison at `cli.py:5338-5340` cannot fail on a trailing
  newline. The test drives real git through a real crash-shaped state.
- **`ScriptedRunner`'s producibility invariant** (`util/proc.py:62-88`, `tests/test_vcs.py:184-191`)
  derives ground truth from a **live** `run()` call, which is the anti-drift property it claims.
  Non-vacuous in both directions.
- **The `FileNotFoundError` work** at `filter_repo.py:162-179`, `gitea.py:284-297`,
  `github.py:147-164` correctly replaces an `exit_code == 127` guess with the real failure mode, and
  each site keeps a comment on why `not result.started` must not be relabelled a missing binary.
  `RaisingRunner` exercises the real path in both test modules.
- **`clone.py:741-749`** retracts a prior overclaim ("measured across git 2.20.4 → 2.49.1") down to
  the one version actually measured. That is the rubric's disease being cured, unprompted — and it
  is the model for every fix recommended above.
