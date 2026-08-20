> Round-C research artifact, produced by lane R-2 (`design-resume-step5`), promoted unchanged from `.superpowers/` scratch by lane DOCSTALE.

# Research R2 — scoping §13 row 43 (rate limiting), next-task 6, LARGE

## 0. Evidence provenance

`git status --short` at the **start** of the reads that produced this file:

```
 M src/fleet/cli.py
 M src/fleet/state/checkpoints.py
 M src/fleet/state/repository.py
?? .superpowers/
```

`git status --short` at the **end**:

```
 M src/fleet/cli.py
?? .superpowers/
```

`HEAD` moved under this lane while it read — it was `6a5e534` at dispatch and `16879fe` at the last
read. The three commits that landed in between are `011b16d` (`orchestrator/reentry.py`), `ddc16a6`
(`docs/DECISIONS.md`, `tests/test_backend_registry_gate.py`) and `16879fe` (`state/repository.py`,
`state/checkpoints.py`, `tests/test_repository.py`). **None of them touches any file this document
makes a claim about.** Verified by a targeted status:

```
git status --short -- src/fleet/llm src/fleet/orchestrator/budgets.py \
  src/fleet/orchestrator/retry.py src/fleet/orchestrator/runner.py \
  src/fleet/models/enums.py src/fleet/workers src/fleet/settings.py \
  tests/test_config_keys_are_read.py docs/SPEC.md docs/INTEGRATION_HONESTY.md docs/PROGRESS.md
```

— **empty**. Every `src/fleet/llm/**`, `orchestrator/budgets.py`, `orchestrator/retry.py`,
`orchestrator/runner.py`, `orchestrator/findings.py`, `models/enums.py`, `workers/**`, `settings.py`
and `tests/test_config_keys_are_read.py` line number below is a working-tree read that is
**byte-identical to `main` at `16879fe`**.

`src/fleet/cli.py` was dirty throughout. **Every `cli.py` claim below was read from
`git show main:src/fleet/cli.py`** and its line numbers are `main` line numbers; the sibling lane's
uncommitted edit adds ~30 lines above 10044, so anchor on the quoted text, not the number. No
sibling lane's uncommitted edit is reported as a defect anywhere in this document.

**Ref for every claim below unless stated otherwise: `main` = `16879fe`.**

Inherited without re-derivation, cited as inherited, from `.superpowers/sdd/design-resume-step5/`
`research-1.md`: none — this lane shares no findings with R1. The `docs/PROGRESS.md` §38 and
`docs/INTEGRATION_HONESTY.md` D55 texts quoted below were read directly at `main`.

---

## 1. The two suspect numbers, re-measured

### 1.1 Worker count: **11**, not 12

```
grep -rn "@register_worker" src/fleet/workers/   →  11 hits
```

`symbolindex.py:193`, `classify.py:114`, `interrogate.py:257`, `relocate.py:129`,
`contracts.py:228`, `buildgen.py:232`, `clone.py:253`, `buildverify.py:659`, `rdepverify.py:163`,
`rewrite.py:240`, `prwriter.py:188`. `workers/base.py` is the ABC and carries no decorator.
`orchestrator/registry.py:39-57` (`discover()`) imports every non-underscore module in
`fleet.workers`, so the decorator count *is* the registry size.

**§38 next-task 6's "12 workers" is wrong; the correct figure is 11.** The tree itself is split on
this: `orchestrator/runner.py:39`, `orchestrator/runner.py:772` and `workers/base.py:635` all say
"all eleven workers", while **`cli.py:10044` (at `main`) says "All twelve workers implement …"**.
That `cli.py` string is stale on `main` and is a legitimate (tiny) sweep target for whichever
subtask touches `cli.py` — it is *not* a sibling lane's edit; it is present in
`git show main:src/fleet/cli.py`.

### 1.2 Semaphore acquirers: **1**, and the "1 of N" framing understates the gap

```
grep -rn "limits\.for_tier\|limits\.llm" src/   →  one acquisition
```

The **only** acquisition of the per-tier LLM semaphore in `src/` is:

* **`src/fleet/workers/classify.py:161-162`**
  ```
  tier = ctx.router.resolve(Role.REPO_CLASSIFY.value).tier
  async with ctx.limits.for_tier(tier):
  ```

Every other reference is a definition or a docstring: `budgets.py:993-995` (the accessor),
`budgets.py:980` (construction), `workers/base.py:323` and `orchestrator/context.py:126` (comments),
`settings.py:1264`/`settings.py:1504` (the config-side `for_tier(tier) -> int`).

**"1 of 11" is true but misleading. The sharp number is 1 of 5.** Only five of the eleven workers
reach `ctx.llm` at all:

| worker | LLM call site | acquires the tier semaphore? |
|---|---|---|
| `classify` | `classify.py:163` `ctx.llm.complete(...)` | **yes** — `classify.py:162` |
| `buildgen` | `buildgen.py:453` `author_build_file(ctx.llm, …)`, `buildgen.py:565` `resolve_version_conflict(...)` | no |
| `rewrite` | `rewrite.py:497` `escalate_repair(client, …)`, `rewrite.py:503` `propose_repair(...)` | no |
| `buildverify` | `buildverify.py:1194` `diagnose_build(ctx.llm, …)` | no |
| `prwriter` | `prwriter.py:413` `write_pr_body(...)`, `prwriter.py:421` `write_pr_title(...)` | no |

`contracts.py:10` states it never touches `ctx.llm` by design. The remaining five
(`clone`, `interrogate`, `relocate`, `rdepverify`, `symbolindex`) make no LLM call.

Neither `llm/calls.py` nor `llm/client.py` acquires anything: `grep -n "limits\|semaphore\|for_tier"`
over both files returns **zero** hits. `Limits` is not reachable from a `ModelClient` at all — the
client is assembled in `RunContext.__post_init__` (`orchestrator/context.py:159-180`) from
`self.llm` (the router), `self.backends` and `self.llm_policy`, and never sees `self.limits`.

**Consequence for the design: the tier ceiling is unenforced for four of five LLM-calling workers,
including both `HEAVY` consumers (`rewrite`'s `ESCALATION` / `API_INCOMPAT_REWRITE` and `buildgen`'s
`BUILD_AUTHORING` / `CONFLICT_RESOLUTION`, per `llm/roles.py:65-77`).** An AIMD controller that
shrinks a semaphore nobody holds shrinks nothing. **This is the single most important structural
fact in this scoping** and it dictates the ordering in §4.

---

## 2. Q1 — where a 429 enters, and what happens to it

### 2.1 Entry: four backends, one typed trigger, `FailoverTrigger = "RATE_LIMIT"`

`llm/client.py:36` declares the trigger set exhaustively:
```
FailoverTrigger = Literal["CONNECTION", "SERVER_ERROR", "RATE_LIMIT", "SCHEMA_UNSATISFIED"]
```
`TransportError` (`llm/client.py:136-142`) carries it: `__init__(message, *, trigger="CONNECTION")`.

All four shipped backends already translate a 429 into `trigger="RATE_LIMIT"`, correctly and
distinctly from `SERVER_ERROR`:

| backend | site | mechanism |
|---|---|---|
| `anthropic` | `backends/anthropic.py:224-225` | `except anthropic.RateLimitError` |
| `openai_compatible` | `backends/openai_compatible.py:218-219` | `except RateLimitError` |
| `vertex` | `backends/vertex.py:104`, `:358-359` | `_RATE_LIMIT_STATUS = 429`, checked before the `>= 500` arm |
| `bedrock` | `backends/bedrock.py:144-146`, `:314-316` | `_RATE_LIMIT_CODES = {"ThrottlingException","ThrottledException","TooManyRequestsException", …}` |

**The classification layer is already correct and needs no work.** The gap is entirely downstream.

Note the SDKs retry first: `backends/anthropic.py:100` `_MAX_RETRIES = 4` (passed at `:218`),
`backends/openai_compatible.py:93` `_SDK_TRANSIENT_RETRIES = 2` (passed at `:205`). A
`RATE_LIMIT` `TransportError` therefore means *"429 survived 4 (or 2) SDK-internal retries"*, which
is already a meaningful "sustained" signal and matters for tuning any AIMD step.

### 2.2 What the code does with it today: **nothing that branches**

`llm/client.py:531-542`, the whole of the failover arm:

```
except (SchemaUnsatisfied, TransportError) as exc:
    last = exc
    trigger: FailoverTrigger = (
        "SCHEMA_UNSATISFIED" if isinstance(exc, SchemaUnsatisfied) else exc.trigger
    )
    if index + 1 < len(targets):
        self._emit_failover(role, route.tier, target, targets[index + 1], trigger)
raise TierUnavailable(route.tier, tried) from last
```

`exc.trigger` is read **only to label the emitted event** (`_emit_failover`, `client.py:655-675`).
Nothing branches on it. A `TransportError("…", trigger="RATE_LIMIT")` and one carrying a refused
connection take byte-identical paths. When `targets[:max_targets_per_call]` is exhausted,
`TierUnavailable(route.tier, tried)` is raised (`client.py:542`).

Then, **in exactly one worker**:
* `workers/classify.py:247-248` — `elif isinstance(exc, TierUnavailable): failure_class = FailureClass.BACKEND_UNAVAILABLE`
* `workers/classify.py:254-258` — `retryable = failure_class not in (BUDGET_EXHAUSTED, BACKEND_UNAVAILABLE)` ⇒ **non-retryable**
* `orchestrator/retry.py:187-198` — `BACKEND_UNAVAILABLE` ⇒ `RetryAction.TERMINATE`, `terminal_status=PENDING`, `reason="every target for the tier is DOWN; …"`
* `orchestrator/runner.py:638-641` — `raise RunHalted(HaltReason.TIER_UNAVAILABLE, f"every backend target for {repo_id}'s tier is DOWN: {decision.reason}")` ⇒ **exit 8**

So: **a sustained 429 halts the run and tells the operator the provider is down.** That is §13 row
43's named disaster (`docs/SPEC.md:7195`), live in shipped code — D55 in
`docs/INTEGRATION_HONESTY.md`, status **OPEN**.

### 2.3 Blast radius today is narrower than D55's text implies — and that is load-bearing

`classify` is the **only** worker that maps `TierUnavailable` to `BACKEND_UNAVAILABLE`.
`grep -rn "TierUnavailable" src/fleet/workers/` returns hits in `classify.py` only (`:43`, `:247`).
The other four LLM-calling workers do something different with `LlmError` (which
`TierUnavailable` subclasses, `client.py:145`):

* `buildgen.py:454-455` — `except LlmError: return None, TokenUsage()` (degrade and continue)
* `prwriter.py:413-423` — `except LlmError: prose = None` / `proposed = None` (degrade)
* `buildverify.py:1193-1198` — `except LlmError: return TokenUsage()` (diagnosis is advice)
* `rewrite.py:505-506` — `except LlmError as exc: raise WorkerRepairError(...)` — a bare
  `RuntimeError` subclass (`rewrite.py:90`) that carries no `FailureClass`

**Two consequences an implementer must plan around:**

1. Today the exit-8-on-429 path is reachable only through **Phase 1 `classify`** (a `CHEAP`-tier
   role, `roles.py:75`). The moment a subtask adds tier-semaphore acquisition or a uniform
   `TierUnavailable → BACKEND_UNAVAILABLE` mapping to the other four, the defect becomes broadly
   live. **Fix the classification before, or in the same change as, widening the acquisition.**
2. A `HEAVY`-tier outage during `rewrite` currently surfaces as a `WorkerRepairError`, i.e. an
   unclassified exception → `FailureClass.UNKNOWN` → retryable → it **burns `phases.attempts`**,
   which §11.8 and ADR-0014 both forbid for an infrastructure failure. This is an *observation of
   `main`* made by this lane; it is adjacent to D55 but is not D55 and carries no D-number.
   Flagged, not adjudicated.

### 2.4 Q1's specific check: the three strings that misdescribe `DOWN` — **CONFIRMED, with a correction**

The brief cites `runner.py:640`, `retry.py:196`, `enums.py:383`. All three are correct at `main`:

* `orchestrator/runner.py:640` — the halt message, `f"every backend target for {repo_id}'s tier is DOWN: …"`
* `orchestrator/retry.py:196` — `reason="every target for the tier is DOWN; …"`
* `models/enums.py:383` — `BACKEND_UNAVAILABLE = "BACKEND_UNAVAILABLE"  # every target for a tier is DOWN (ADR-0023).`

`grep -rn "\bDOWN\b" src/ --include=*.py` returns exactly seven hits: those three, plus
`orchestrator/runner.py:609` (a comment *explaining* why the word is wrong) and
`orchestrator/findings.py:86,347,349` (FD1's docstrings refusing the word). **`BackendHealth` is
defined nowhere in `src/`** — `grep -rn "BackendHealth" src/` returns only `runner.py:609` and
`findings.py:191,347`, all prose. `DOWN` is `BackendHealth`'s state name from SPEC §11.8
(`docs/SPEC.md:7056-7060`), and that state machine is not built.

**One correction to the standing framing.** `runner.py:609-614` says `client.py:532` "retires a
target without inspecting `TransportError.trigger`". D55 already corrected this once and the
correction is the accurate one: the arm **does** read `exc.trigger` (`client.py:537-538`) — but only
to *label* the event. **Nothing branches on it.** Use the D55 phrasing, not the `runner.py:609`
phrasing, in any new ADR or SPEC text; the shorthand invites a reviewer to "fix" it by adding a read
that is already there.

---

## 3. Q2 — the semaphore: what it is, what it bounds, who holds it

### 3.1 Definition

`src/fleet/orchestrator/budgets.py:950-995`:

```
@dataclass(slots=True)
class Limits:
    git_net: asyncio.Semaphore
    subprocess: asyncio.Semaphore
    docker: asyncio.Semaphore
    llm: Mapping[ModelTier, asyncio.Semaphore]      # budgets.py:962
    cpu_pool: Executor
    ledger: CostLedger

    @classmethod
    def create(cls, concurrency, *, ledger, cpu_pool=None, llm_overrides=None) -> Limits:
        overrides = llm_overrides or {}
        llm = {
            tier: asyncio.Semaphore(
                max(1, min(concurrency.llm.for_tier(tier), overrides.get(tier, 1 << 30)))
            )
            for tier in ModelTier
        }                                            # budgets.py:978-983
        ...

    def for_tier(self, tier: ModelTier) -> asyncio.Semaphore:   # budgets.py:993-995
        """The semaphore every `ModelClient.complete` on this tier must hold."""
        return self.llm[tier]
```

* **What it bounds: concurrent in-flight `complete()` calls per `ModelTier`.** Not tokens, not
  repos, not targets. Keyed by tier and never by backend or model — ADR-0023, restated at
  `budgets.py:955-957` ("a failover inherits the tier's budget instead of opening a second
  unbounded lane") and `settings.py:229`.
* **Sizes:** `settings.py:228-239` `LlmConcurrency` — `heavy=2`, `workhorse=8`, `cheap=16`.
  `ModelTier` has three members, so there are exactly **three** LLM semaphores per run.
* **Lowered-only override:** `llm.concurrency_overrides` may only *lower* a tier
  (`budgets.py:975-977`, enforced at startup by `settings.py:1501-1511`
  `_check_concurrency_overrides`). This is already the SPEC's "run this slower" direction — but see
  §5.2: the `llm_overrides=` kwarg is passed by **no** production call site.
* **`Limits` is `@dataclass(slots=True)` and not frozen**, so a field could in principle be
  rebound — but `llm` is typed `Mapping[...]` and `for_tier` returns the live object, so every
  holder of a semaphore keeps holding the *old* object across any rebind. Rebinding the mapping is
  therefore **not** a usable resize (§4.2, option B).

### 3.2 Who holds it, and who arguably should

Holders: `workers/classify.py:162`, and nothing else (§1.2).

Who should: **every `ModelClient.complete()` and `.stream()` call**, per the accessor's own
docstring at `budgets.py:994` — *"The semaphore every `ModelClient.complete` on this tier must
hold."* That sentence is currently false for four of five callers.

The one clean choke point is inside `LadderModelClient.complete` (`llm/client.py:637-657`), because
that is where the tier is known (`route = self._router.resolve(role, tier_override=...)`,
`client.py:646`) and it is below the cache wrapper. Ordering matters: `RunContext.__post_init__`
builds `LadderModelClient` first and then wraps it in `CachingModelClient`
(`orchestrator/context.py:167-180`), so a limiter installed in `LadderModelClient` is **not**
consumed by an `llm_cache` hit — which is the correct semantics. A limiter installed in
`CachingModelClient` would charge cache hits a slot and would be wrong.

`.stream()` has exactly one caller in `src/` — `llm/cache.py:514`, pure delegation — so the
streaming path costs nothing extra to cover.

---

## 4. Q3 — the resize problem

### 4.1 Why the design wants resizing

SPEC §11.8 (`docs/SPEC.md:7031-7040`), verbatim on the mechanism:

> …the owning tier's LLM semaphore is **AIMD**-adjusted — halved on a 429 or a `retry-after`, one
> slot returned per clean minute, never below `aimd.floor` (1) and never above `concurrency.llm.*`.

The knobs exist in config today and are read by nothing:
`settings.py:647-651` `AimdSection(shrink_factor=0.5, grow_every_s=60, floor=1)`,
`settings.py:654-662` `RateLimitSection(honor_retry_after=True, defaults, targets, aimd)`.

`asyncio.Semaphore` has no public API to change `_value` after construction, and mutating `_value`
directly does not wake waiters. Hence the problem.

### 4.2 The options, measured

**Probe discipline (Guardrail 2): the question asked was `find_spec` = INSTALLED**, run under
`/home/redmage/swe repo harness/.venv/bin/python` (Python 3.12.3) — the interpreter that runs the
harness — *not* `pyproject.toml` (= declared) and *not* `sys.modules` (= imported so far). Where
declaration matters I say so separately and cite `pyproject.toml`.

| module | `find_spec` under `.venv` | declared in `pyproject.toml`? |
|---|---|---|
| `anyio` | **INSTALLED** (4.14.2) | **NO** — transitive via `anthropic` / `openai` / `httpx` |
| `aiolimiter` | absent | no |
| `limits` | absent | no |
| `pyrate_limiter` | absent | no |
| `tenacity` | absent | no |
| `backoff` | absent | no |
| `asyncio_throttle` | absent | no |
| `httpx` | INSTALLED | no (transitive) |
| `anthropic` | INSTALLED (0.121.0) | yes — `anthropic>=0.69` |
| `openai` | INSTALLED (2.53.0) | yes — `openai>=1.60` |
| `boto3` / `botocore` | **absent** | `[project.optional-dependencies] bedrock` only |

Declared runtime deps at `main` (`pyproject.toml` `project.dependencies`): `pydantic>=2.11,<3`,
`pydantic-settings>=2.7`, `typer>=0.15`, `aiosqlite>=0.20`, `structlog>=24.4`, `anthropic>=0.69`,
`openai>=1.60`, `networkx>=3.4`, `pyyaml>=6.0`. Extras: `bedrock` (`boto3`), `vertex`
(`google-cloud-aiplatform`), `dev`.

---

**Option A — `anyio.CapacityLimiter`.**

Measured under `.venv/bin/python` on a plain `asyncio.run` loop (no anyio runner needed):

* `total_tokens` is a **settable property**: `4 → 2 → 7` all accepted, waiters re-evaluated.
* Shrinking **while tokens are held is legal and safe**: three holders, then `total_tokens = 1` ⇒
  `borrowed_tokens = 3`, `available_tokens = -2`. It oversubscribes transiently and drains to the
  new ceiling. This is exactly the AIMD semantics §11.8 wants.
* `total_tokens = 0` is **accepted** (deadlock hazard); `-1` raises `ValueError`; `0.5` raises
  `TypeError`; `math.inf` accepted. `settings.py:644` pins `floor: int = Field(default=1, ge=1)`,
  which forecloses the `0` hazard *if the controller clamps to `aimd.floor`*.
* `statistics()` returns `CapacityLimiterStatistics(borrowed_tokens, total_tokens, borrowers,
  tasks_waiting)` — free observability for a `rate_limited` event payload.

**The disqualifying measurement.** `CapacityLimiter` is **per-borrower, not counting**: a task that
already holds a token and acquires again raises

```
RuntimeError: this borrower is already holding one of this CapacityLimiter's tokens
```

(measured; raised from `anyio/_backends/_asyncio.py:2109`). `asyncio.Semaphore` permits it and
simply consumes two slots. **If a subtask moves acquisition into `LadderModelClient` while
`classify.py:162` still acquires, an `asyncio.Semaphore` silently double-charges (a bug that hides)
whereas a `CapacityLimiter` raises `RuntimeError` on every classify call (a bug that is loud).** For
this codebase's Rule-11 posture the loud failure is arguably *better*, but either way
`classify.py:161-162` must be deleted in the same change.

Costs: `anyio` appears **nowhere** in `src/` or `tests/` today (`grep -rn "anyio" src/ tests/
pyproject.toml docs/DECISIONS.md` ⇒ **zero hits**). Adopting it means declaring a new runtime
dependency that is currently only transitive, and importing a second concurrency vocabulary into a
tree that is uniformly bare-`asyncio`.

**Option B — rebuild the mapping / swap semaphores on change.** Free of new deps, and wrong.
`for_tier` (`budgets.py:995`) hands out the live object; every in-flight `async with` holds the old
semaphore, so a swap loses the ceiling for the duration of the longest call (`default_timeout_s =
120.0`, `client.py:456`) and double-counts across the boundary. Rejected.

**Option C — a hand-rolled `ResizableLimiter` in `budgets.py`.** ~50 lines: an `int` capacity, an
`int` borrowed count, and a `collections.deque[asyncio.Future]` of waiters; `acquire()` fast-paths
when `borrowed < capacity` else parks a future; `release()` decrements and wakes as many waiters as
the *current* capacity permits; `resize(n)` clamps to `[floor, ceiling]` and, when growing, wakes
the difference. Counting (not per-borrower), so it is a drop-in for `asyncio.Semaphore` at
`classify.py:162` and interchangeable during migration. Needs deliberate care on FIFO fairness and
on cancellation (a cancelled waiter must not consume a wake).

**Option D — an explicit "do not adapt".** Ship the token bucket and the "429 is never `DOWN`"
classification, leave the tier semaphore static, and document AIMD as declined. This closes §13 row
43's *actual disaster* (exit 8 on throttling) without any resize machinery, and is the cheapest
credible outcome. Its cost: `aimd.*` stays in `KNOWN_INERT` and SPEC §11.8's "AIMD-adjusted"
sentence must be rewritten (Guardrail 7 — *"the SPEC says X but the code cannot do X" is two edits*).

**Option E — bucket-only backpressure with no semaphore change.** A per-target token bucket
(`rpm`/`tpm`) throttles admission by *waiting* rather than by lowering a ceiling. Under a bucket
sized to the account's real quota, the semaphore's value stops being the binding constraint at all,
which makes AIMD arguably redundant. Cheapest of all; departs furthest from SPEC §11.8's letter.

### 4.3 Recommendation — **Option C**, with Option D as the honest fallback

Reasoning against the codebase's own idioms, not against general preference:

1. **CLAUDE.md Rule 5** — *"If Python code can do it deterministically, write a script."* A
   resizable counting limiter is 50 deterministic lines with no judgement in it.
2. **Zero-dependency idiom.** `anyio` is installed but **not declared**; `pyproject.toml`'s deps are
   hand-curated with a per-line justification comment (`# llm/backends/anthropic.py`). Adding
   `anyio` for one class, in a tree with zero existing `anyio` imports, buys a resize property this
   project can write itself and imports a second concurrency vocabulary alongside bare `asyncio`.
3. **Counting semantics preserve migration safety.** Option A's per-borrower rule makes
   `classify.py:162` an immediate hard error at the moment the client starts acquiring, forcing two
   changes to land atomically. Option C lets the widening and the classify deletion be separately
   reviewable.
4. **`Limits` already owns exactly this concern.** `budgets.py` is titled *"`Limits` and
   `CostLedger`: semaphores and the reserve-then-spend cost policy (§11.1, §11.2)"* and already
   hand-rolls policy objects. A limiter there is not a new abstraction; it is the module's job.

**Honest counter-argument, stated because it is real:** Option A is *measured to work today* and is
zero implementation risk on the hard parts (fairness, cancellation, wake accounting) that Option C
must get right and test. If the implementing lane cannot afford a properly adversarial test for
Option C's waiter queue, **take Option A** — a correct borrowed dependency beats a subtly wrong
hand-roll. Whichever is chosen, this is an **ADR-worthy** decision (orchestrator concurrency
primitive + possible new declared dependency). *This paragraph is an Agent Recommendation, not a
directive; nothing in `CLAUDE.md` or the SPEC mandates either option.*

---

## 5. Q4 — the decomposition

Sizes follow `design-resume-step5.md` §5: **S** ≈ one focused pass, <150 LOC + tests; **M** ≈
150–350 LOC + tests; **L** ≈ larger.

**Numbers to be allocated centrally by the orchestrator at dispatch, not by any worker: the ADR
numbers flagged below, and any D-number for §2.3's observation 2.**

| # | Title | Size | Depends on | Files touched | Falsifiable acceptance criterion |
|---|---|---|---|---|---|
| **R1** | **The resizable limiter primitive.** Land the §4.3 choice as a concrete class in `orchestrator/budgets.py`; retype `Limits.llm` to `Mapping[ModelTier, ResizableLimiter]` and `Limits.for_tier -> ResizableLimiter`. No wiring, no controller, no config read. `classify.py:162`'s `async with` must keep working unchanged. | S | — | `src/fleet/orchestrator/budgets.py`, `tests/test_budgets.py`, `docs/DECISIONS.md` (**ADR — number from orchestrator**) | (a) 6 tasks, capacity 3 ⇒ never >3 concurrent, asserted on a peak counter. (b) `resize(1)` with 3 held ⇒ no new entrant until borrowed drops to 0, and the 3 holders are **not** cancelled or errored. (c) `resize` clamps to `[floor, ceiling]`; `resize(0)` is refused. (d) FIFO: waiters admitted in arrival order. (e) A waiter cancelled while parked consumes no wake — 2 tasks queued, cancel the first, release once, the second **runs**. (f) `tests/test_budgets.py:714-716`'s existing `slots(limits.for_tier(...))` assertions still pass |
| **R2** | **`TransportError` carries `retry_after_s`, and every backend populates it.** Add `retry_after_s: float \| None = None` to `TransportError.__init__` (`llm/client.py:140-142`). Populate from `exc.response.headers.get("retry-after")` in `anthropic.py:224-225` and `openai_compatible.py:218-219` (both `RateLimitError` types subclass `APIStatusError`, which carries `.response` — **measured** under `.venv`: `anthropic.RateLimitError.__mro__` and `openai.RateLimitError.__mro__` both include `APIStatusError`, whose `__init__` takes `(self, message, response, body)`). `vertex.py:349-362` `_from_status(region, status, detail)` must grow a header argument — `vertex.py` uses `httpx` directly and currently discards the response headers. `bedrock.py:299-322` reads `exc.response["ResponseMetadata"]["HTTPHeaders"]`. | S | — | `src/fleet/llm/client.py`, `src/fleet/llm/backends/{anthropic,openai_compatible,vertex,bedrock}.py`, `tests/` for each backend | Per backend: a fake 429 carrying `Retry-After: 30` yields `TransportError(trigger="RATE_LIMIT", retry_after_s=30.0)`; a 429 with **no** header yields `retry_after_s is None` (never `0.0` — the two must be distinguishable); a 503 yields `trigger="SERVER_ERROR"` and `retry_after_s` untouched by the 429 arm. `bedrock` is `[bedrock]`-extra only — `botocore` is **absent** under `.venv` (probed via `find_spec`), so its test must run against the existing fake/stub, not the SDK |
| **R3** | **The 429 stops producing `TierUnavailable`.** Branch on `exc.trigger` at `llm/client.py:531-542`. A `RATE_LIMIT` `TransportError` must **not** retire the target: it waits (honouring `retry_after_s` when `llm.rate_limit.honor_retry_after`), re-attempts the **same** target up to a bounded count, and only after that bound is it eligible to become §11.8 trigger 3. Emits a throttle signal via a new `on_throttle: Callable[[ThrottleSignal], None] \| None` constructor parameter, **mirroring `on_drift`/`on_failover` exactly** (`client.py:466-481`, sunk in `RunContext.__post_init__` at `context.py:159-180`). **This is the single change that closes D55's hop 1.** | M | R2 | `src/fleet/llm/client.py`, `src/fleet/orchestrator/context.py`, `src/fleet/orchestrator/findings.py`, `tests/test_llm_client.py`, `docs/DECISIONS.md` (**ADR — number from orchestrator**; it redefines "sustained" in §11.8 trigger 3), `docs/SPEC.md` §11.8 | A fake backend answering 429 forever on a 2-target tier: (a) the call does **not** raise `TierUnavailable` before the throttle bound is spent, asserted against a **recorded call log** so a busy-loop fails the test (the shape `docs/SPEC.md:7136` case (ii) already demands); (b) the same fake answering `CONNECTION` fails over on the first error, unchanged; (c) `on_throttle` fires once per 429 with the target and `retry_after_s`. **Discriminating mutation (Rule 12):** revert only the `trigger == "RATE_LIMIT"` branch — the *old* assertions (failover on `CONNECTION`, event labelling) still pass while the new one fails |
| **R4** | **Widen acquisition to one choke point, and delete the worker-side one.** Give `LadderModelClient` a `limiter_for: Callable[[ModelTier], ResizableLimiter] \| None`, held across `complete()`'s target loop (`client.py:637-657`) and `stream()`. Supply it from `RunContext.__post_init__` as `self.limits.for_tier`. **Delete `workers/classify.py:161-162`** — with a counting limiter it would double-charge; under `anyio.CapacityLimiter` it raises `RuntimeError` (§4.2, measured). Sweep the now-true docstring claims at `budgets.py:994`, `workers/base.py:323`, `context.py:126`. Also fix `cli.py`'s stale "All twelve workers" (§1.1). | M | R1 | `src/fleet/llm/client.py`, `src/fleet/orchestrator/context.py`, `src/fleet/workers/classify.py`, `src/fleet/cli.py`, `tests/test_llm_client.py`, `tests/test_runner.py` | With `heavy=1`, two concurrent `rewrite`-role calls serialize (measured on a peak counter inside a fake backend) — **this fails on `main` today**, which is the point. A `classify` call holds **exactly one** slot, asserted on borrowed-count, not two. A **cache hit** (`CachingModelClient`, `cache.py`) consumes **zero** slots — assert borrowed stays 0 across a hit, which is what pins the limiter below the cache wrapper |
| **R5** | **The AIMD controller.** A clock-injected object in `budgets.py` consuming `llm.rate_limit.aimd.{shrink_factor,grow_every_s,floor}` and `honor_retry_after`; subscribed to R3's `on_throttle`; shrinks the throttled call's **tier** limiter by `shrink_factor`, returns one slot per `grow_every_s` throttle-free seconds, clamped to `[aimd.floor, concurrency.llm.for_tier(tier)]`. Emits the `rate_limited` **event** that `cli.py:9599`'s metrics query already selects for and that nothing has ever written. | M | R1, R3 | `src/fleet/orchestrator/budgets.py`, `src/fleet/orchestrator/context.py`, `src/fleet/orchestrator/findings.py`, `src/fleet/settings.py` (no schema change expected), `tests/test_budgets.py`, `tests/test_config_keys_are_read.py` | **The ratchet is the criterion.** Delete `"fleet.yaml:llm.rate_limit.aimd"`, `".aimd.shrink_factor"`, `".aimd.grow_every_s"`, `".aimd.floor"` and `"llm.rate_limit.honor_retry_after"` from `KNOWN_INERT` (`tests/test_config_keys_are_read.py:108-123`); delete `"llm.rate_limit.aimd.floor"` from `QUALIFIED_MATCH_KEYS` (`:257`) too. Then `test_known_inert_keys_are_still_inert`, `test_config_keys_are_read`'s positive half, and `test_the_allowlists_are_disjoint` (`:603`) must **all** pass — that test is the tree's own instrument for "this key is now read", and it fires in both directions. Plus: with a frozen clock, one throttle at capacity 8 ⇒ 4; six more ⇒ floor 1, never 0; `grow_every_s` elapsed ⇒ 2, never above 8. Plus: `fleet metrics` reports `fleet_events_total{event="rate_limited"}` > 0 on a throttled fixture run |
| **R6** | **The per-target token bucket.** `rpm`/`tpm` admission control per `<backend>:<model_id>`, `0` = unlimited, from `llm.rate_limit.defaults` + `.targets`. Consulted before dispatch in `llm/client.py`, keyed on `BackendTarget`, not on tier (which is what makes it orthogonal to R5). Deliberately last: it is the only piece that prevents 429s rather than reacting to them, and it is worthless without R3. | M | R3 | `src/fleet/llm/` (a new module), `src/fleet/llm/client.py`, `src/fleet/orchestrator/context.py`, `tests/`, `tests/test_config_keys_are_read.py` | Delete `"fleet.yaml:llm.rate_limit"`, `".defaults.rpm"`, `".defaults.tpm"`, `".targets.rpm"`, `".targets.tpm"` from `KNOWN_INERT` (`:108-113`); `test_known_inert_keys_are_still_inert` passes. Frozen clock: `rpm=60` ⇒ the 61st call in one minute waits and does not error; `rpm=0` ⇒ no wait ever, asserted by a **zero-sleep** count so an "unlimited" bucket cannot silently sleep 0.001s per call |
| **R7** | **The vocabulary, last.** Now that a 429 cannot reach it, rewrite `runner.py:640`, `retry.py:196` and `enums.py:383` to claim only what the code determines, and reconcile SPEC §11.8 (`docs/SPEC.md:7008-7060`) + §13 row 43 (`:7195`) + §12.43 (`:7136`) + the `docs/SPEC.md:5857` module listing in the **same** change (Guardrail 7). Close D55 in `docs/INTEGRATION_HONESTY.md` with the SHA it was measured at (ADR-0073). | S | R3, R5 | `src/fleet/orchestrator/runner.py`, `src/fleet/orchestrator/retry.py`, `src/fleet/models/enums.py`, `docs/SPEC.md`, `docs/INTEGRATION_HONESTY.md`, `docs/PROGRESS.md` | `grep -rn "\bDOWN\b" src/ --include=*.py` returns **only** the sites that genuinely mean a connection-level or 5xx verdict — today it returns 7 (three claims, four explanatory). FD1's caveat text in `findings.py:82-110` is re-derived, not left asserting a hop that no longer exists. **Sweep for the class, not the site**: `grep -rn "is DOWN" src/ tests/ docs/` must be empty of the un-narrowed claim, test docstrings included |

**Dependency order.** `R1 → R4`; `R2 → R3 → {R5, R6, R7}`; `{R1, R3} → R5`; `{R3, R5} → R7`.
**R1 and R2 are independent and can start in parallel.** R3 is the highest-value single change: it
alone closes D55's causal hop and can land without R1/R4/R5/R6 existing. **If only one subtask ever
ships, ship R3.**

**Cross-lane collision note.** R4 and R7 touch `src/fleet/cli.py` and `orchestrator/runner.py`
respectively, both of which have had active lanes this round. None of R1/R2/R3/R5/R6 touches
`cli.py`, `state/`, or `orchestrator/runner.py`.

**ADRs needed** (numbers allocated by the orchestrator at dispatch, per CLAUDE.md §3 Central Number
Allocation — **this document assigns none**):
1. **R1** — the concurrency primitive: hand-rolled `ResizableLimiter` vs `anyio.CapacityLimiter`,
   and whether `anyio` becomes a **declared** runtime dependency (it is installed-but-undeclared
   today; that distinction is the ADR's whole subject).
2. **R3** — what "sustained 429" means operationally, i.e. how many same-target throttle retries
   precede §11.8 trigger 3, and the fact that a `RATE_LIMIT`-only tier exhaustion must **not** be
   reported as `BACKEND_UNAVAILABLE`.
3. *(conditional)* If §4.3's Option D is taken instead, a **decline ADR** recording that AIMD is not
   built and the SPEC §11.8 sentence rewritten to match. That is a real, defensible outcome — but
   the decline must be written down, not left as an inert config block.

---

## 6. Q5 — the honest case that this is not worth doing now

Four arguments against, in decreasing strength.

**6.1 A large slice of row 43 is already covered, and the backlog does not say so.**
Row 43's mechanisms column (`docs/SPEC.md:7195`) names `--accept-drift` as one of its four
mechanisms. **That half is already built and generic.** `_validate_accept_drift` exists in `cli.py`
(`git show main:src/fleet/cli.py`, symbol `_validate_accept_drift`), it validates against
`FleetSettings.section_digests` (`settings.py:1218-1226`), and the digest keys come from
`CONFIG_SECTIONS` (`settings.py:82-87`), whose **second entry is `"concurrency"`**. So
`fleet resume --accept-drift concurrency` — SPEC §11.8's explicit "run this slower must not cost the
run" affordance — **works today** and needs no subtask. Likewise the 429→`RATE_LIMIT` classification
in all four backends (§2.1) is complete and correct. The genuinely missing machinery is narrower
than "LARGE" suggests.

**6.2 The blast radius is one worker and one tier.**
Per §2.3, the 429→exit-8 path is reachable only through `classify` (`CHEAP` tier, `roles.py:75`).
The four workers that consume `HEAVY` and `WORKHORSE` either degrade silently on `LlmError` or raise
`WorkerRepairError`. So today's fleet does **not** halt at exit 8 on a throttled `HEAVY` account; it
degrades or burns attempts. That is *also bad*, but it is a different bad, and it means the disaster
row 43 names is currently one code path wide.

**6.3 R3 alone buys ~80% of the value at ~20% of the cost.**
R3 is an **M** that requires only R2 (an **S**). Together they make a 429 stop producing
`TierUnavailable`, which is the entire causal chain of D55. R1+R4+R5+R6 — the AIMD and bucket
machinery, three **M**s and an **S** — buy *tuning* on top of a fault that R3 already prevents.
**Recommendation: dispatch R2+R3 as a MEDIUM ticket and re-evaluate whether R1/R4/R5/R6 are still
the bottleneck afterwards.** A LARGE that decomposes into one high-value M and four optional Ms
should be re-sized, not swallowed whole.

**6.4 Nothing here is blocked on unbuilt work — but R7 partly is.**
No subtask depends on `llm/failover.py` or `BackendHealth`, which `docs/PROGRESS.md:5563` records as
**not built** (§13 row 40, MEDIUM). R1–R6 are all independent of it. R7 is the exception: cleaning up
the `DOWN` vocabulary is *cleanest* after row 40 gives `DOWN` a real referent, but it does not
require it — the strings can honestly narrow to "every target for the tier was exhausted" with no
health state machine at all, and that is the wording R7 should take rather than waiting.

**Argument in favour, for balance.** The one thing that genuinely argues for doing the whole LARGE
now is §1.2's finding: **the tier ceiling is unenforced for four of five LLM-calling workers.** That
is not a rate-limiting defect at all — it is an unbounded-concurrency defect on the `HEAVY` tier,
where `concurrency.llm.heavy = 2` (`settings.py:231`) is currently advisory. R4 fixes it, R4 needs
R1, and a fleet that ignores its own `HEAVY` ceiling will generate the very 429s the rest of this
ticket exists to survive. **If the LARGE is dispatched whole, R1+R4 — not the AIMD — is the reason.**

---

## 7. What this lane could not answer

1. **Whether the `WorkerRepairError` path burns `phases.attempts`** (§2.3, observation 2). Tracing
   `rewrite.py:506`'s raise through `runner.py`'s exception handling to a `FailureClass` requires
   running the runner, and this lane was forbidden `pytest`. Stated as an unadjudicated observation
   with no D-number.
2. **The right value for R3's throttle-retry bound.** Setting it requires knowing how the SDKs'
   own retries (`anthropic` `max_retries=4`, `openai_compatible` `max_retries=2`) compose with a
   `Retry-After` wait, which is a measurement against a real throttled endpoint — Guardrail 6
   forbids putting an unmeasured number in a brief, so R3's ADR must measure it or make it a config
   knob with a documented-as-unmeasured default.
3. **Whether `bedrock`'s `ResponseMetadata.HTTPHeaders` actually carries `Retry-After`** for
   `ThrottlingException`. `botocore` is **absent** under `.venv` (`find_spec` — installed), so no
   probe was possible; R2 must treat this as unverified and fall back to `retry_after_s=None`.
4. **Whether `--accept-drift concurrency` has ever been exercised end-to-end.** §6.1 establishes it
   is mechanically reachable (`"concurrency"` ∈ `CONFIG_SECTIONS`); it does not establish that a
   test covers it.
