> Round-C lane **R1** (rate-limiting decomposition) task report, produced under plan `docs/superpowers/plans/design-resume-step5.md`; promoted substantively unchanged from `.superpowers/sdd/design-resume-step5/task-rl1-report.md` (git-ignored scratch) because `docs/DECISIONS.md` ADR-0083 cites its §4 item 1 as the provenance of an Agent Recommendation. Body below is byte-identical to the scratch original.

# Lane RL1 — report

**Status: COMPLETE.** §5's R1 implemented, nothing else. Two commits.

* `a3ff0ae` — `budgets: a tier ceiling that can move while calls are in flight (R1)`
  (`src/fleet/orchestrator/budgets.py`, `tests/test_budgets.py`)
* `431b02f` — `tests/budgets: pin the wake window, where charging at resume passes anyway`
  (`tests/test_budgets.py`)

Both committed by explicit path. `git status --short` at start: `M docs/INTEGRATION_HONESTY.md`,
`M docs/superpowers/plans/task-item17-report.md`, `M src/fleet/cli.py`, `?? .superpowers/`. At
end: `M src/fleet/cli.py`, `?? .superpowers/`. Siblings landed `bf7206d` and `f84edcb`
(`CLAUDE.md`) between my two commits; neither touches anything I claim below. No sibling's
uncommitted edit is reported as a defect anywhere in this file.

---

## 1. §5's decomposition as I found it

`docs/superpowers/plans/rate-limiting-scope-research.md` §5, seven subtasks:

| # | one line | size | depends on |
|---|---|---|---|
| **R1** | The resizable limiter primitive in `budgets.py`; retype `Limits.llm` / `Limits.for_tier` to it. No wiring, no controller, no config read. | S | — |
| **R2** | `TransportError` carries `retry_after_s`, and all four backends populate it from the 429's `Retry-After`. | S | — |
| **R3** | The 429 stops producing `TierUnavailable` — branch on `exc.trigger` in `llm/client.py`, wait and re-attempt the same target, emit `on_throttle`. Closes D55's hop 1. | M | R2 |
| **R4** | Widen acquisition to one choke point in `LadderModelClient`, delete `workers/classify.py:161-162`, sweep the docstrings the change makes true. | M | R1 |
| **R5** | The AIMD controller: clock-injected, consumes `llm.rate_limit.aimd.*`, subscribed to R3's `on_throttle`, emits the `rate_limited` event. | M | R1, R3 |
| **R6** | The per-target `rpm`/`tpm` token bucket, keyed on `BackendTarget`. | M | R3 |
| **R7** | The vocabulary: narrow the three `DOWN` strings, reconcile SPEC §11.8 / §13 row 43 / §12.43, close D55. | S | R3, R5 |

Dependency order `R1 → R4`; `R2 → R3 → {R5, R6, R7}`. **R1 and R2 are the two roots.** I took
**R1**, per the brief ("§5's first subtask, or whatever §5 numbers first in dependency order").
I touched no part of R2–R7.

Note the document's own §6.3 recommends dispatching **R2+R3** first on value grounds and says "if
only one subtask ever ships, ship R3". That is a re-sizing recommendation to the orchestrator, not
a renumbering: R1 is still §5's first row and the root of the R1→R4 chain, and §6's closing
paragraph argues R1+R4 — not the AIMD — is the reason to do the LARGE at all. I did not act on
§6.3; it is the orchestrator's call.

## 2. What R1 asked for, and what landed

`ResizableLimiter` in `src/fleet/orchestrator/budgets.py`, immediately above `Limits`:
a counting limiter over an `int` capacity, an `int` borrowed count and a
`deque[asyncio.Future[None]]` of waiters, with `acquire` / `release` / `locked` / `capacity` /
`borrowed` / `resize` / `__aenter__` / `__aexit__`. `Limits.llm` is retyped
`Mapping[ModelTier, ResizableLimiter]`, `Limits.for_tier -> ResizableLimiter`, and
`Limits.create` builds `ResizableLimiter(...)` where it built `asyncio.Semaphore(...)`. The
arithmetic that computes the per-tier value (`max(1, min(configured, override))`) is untouched.

No wiring, no controller, no config read — as R1 specifies. `workers/classify.py:161-162` is
unchanged and still works: the class is **counting**, so `async with ctx.limits.for_tier(tier)`
is a drop-in, which is §4.3 reason 3 for preferring it.

### Deliberate deviations from the sketch, and one open assumption

* §4.2 Option C sketches `locked()` with a "live waiter parked" clause (CPython 3.12's
  `Semaphore.locked()` has one). **I removed it.** Because a slot is charged inside `_wake_next`
  rather than by the resumed waiter, every path that creates headroom — `release`, `resize`, the
  cancellation recovery — drains the queue *synchronously*, with no `await` between freeing a slot
  and charging it. So the invariant "headroom exists ⇒ no live waiter is parked" holds, and the
  clause is not merely redundant but actively harmful: in the one state it fires (a woken-but-not-
  yet-resumed future still sitting in `_waiters`), it parks an arrival for a slot nobody is
  queueing for. CPython needs it because it *also* carries a compensating wake at the tail of
  `acquire`; I carry neither. I could not construct a discriminating test for the clause, which
  under Rule 2 is the argument for not shipping it.
* **`resize(0)` is refused (`ValueError`), and so is any `capacity < 1`** — R1 criterion (c).
  Out-of-range-but-positive values *clamp* rather than raise, because AIMD's halving will
  routinely undershoot the floor and that is not an error.
* **Open assumption — the default ceiling.** R1 says `resize` clamps to `[floor, ceiling]` but
  does not say what they are; §5's R5 row says the controller clamps to
  `[aimd.floor, concurrency.llm.for_tier(tier)]`. `ResizableLimiter.__init__` takes
  `floor: int = 1` and `ceiling: int | None = None`, **defaulting the ceiling to the starting
  capacity**, and `Limits.create` passes neither. That differs from R5's literal text whenever
  `llm.concurrency_overrides` has *lowered* a tier: R5's wording would let a controller grow back
  past the operator's override, undoing "run this slower" — which §11.8 names as the intended
  response to throttling in the first place. My assumption is that the override is a ceiling, not
  a starting point. **R5's implementer must either accept that or pass an explicit `ceiling=`;
  the constructor already supports both.** I did not change R5's text (not my file).
* `release()` without a matching `acquire()` raises `RuntimeError` (Rule 11).
  `asyncio.Semaphore` allows it; a negative borrowed count silently *raises* the effective
  ceiling, which is the failure mode this whole ticket exists to prevent.

### The `anyio` ruling — followed, and I agree with it

The controller's ruling ("do not add an `anyio` dependency") matches the primary source rather
than overriding it: §4.3 recommends Option C on four grounds, of which the strongest are that
`anyio` is **installed but undeclared** and has zero uses in the tree, and that
`CapacityLimiter` is **per-borrower**, so it would raise `RuntimeError` at `classify.py:162` the
moment R4 lands and force R1/R4 to be one atomic change. I re-read §4.2/§4.3 and found no
contradiction to argue.

§4.3's honest counter-argument is conditional: *"if the implementing lane cannot afford a properly
adversarial test for Option C's waiter queue, take Option A."* I could afford it — seven
discriminating mutations below, all on the queue, the cancellation paths and the resize. The
condition therefore does not fire. **No argument against the ruling.**

I re-verified the `anyio` measurement myself only to the extent it mattered: I asked the
**declared** question (`pyproject.toml` `project.dependencies` — `anyio` is not there) and the
**imported-so-far** question is irrelevant here. I did not need `find_spec`, because the decision
was to *not* use it; whether it is installed does not change that. My change imports nothing new
beyond `collections.deque` from the stdlib.

## 3. Verification

### Discriminating mutations (Rule 12 / Guardrail 6)

Harness: apply one exact string replacement to `src/fleet/orchestrator/budgets.py`, **assert the
anchor matched exactly once**, print `git diff --numstat` as proof the file actually changed
*before* reading any test result, run the OLD assertion and the NEW one, restore byte-for-byte and
re-print `numstat` (empty). Run against the committed tree, so a non-empty `numstat` is the
mutation and nothing else. The harness's own three checks: it reports non-zero `numstat` on every
mutation (fires), reports an empty `numstat` after restore (silent on the clean file), and the
mutations *are* synthetic faults injected into a clean file (the third check).

**OLD** in every row is the pre-existing
`tests/test_budgets.py::test_limits_key_llm_semaphores_by_tier_not_by_backend` — R1's criterion
(f), the tree's existing `slots(limits.for_tier(...))` assertions. It passes under all seven.

| mutation | change | numstat | OLD | NEW (target test) |
|---|---|---|---|---|
| M1 no bound | `if not self.locked():` → `if True:` | 1/1 | **pass** | **fail** `…never_admits_more_than_capacity_under_contention` |
| M2 shrink no-op | `resize` clamps upward only | 1/1 | **pass** | **fail** `…shrinking_while_slots_are_held_bars_entrants_and_harms_no_holder` |
| M3 grow does not wake | drop `resize`'s wake loop | 1/2 | **pass** | **fail** `…growing_admits_parked_waiters_without_waiting_for_a_release` |
| M4 no clamp / no zero refusal | `self._capacity = capacity` | 1/3 | **pass** | **fail** `…resize_clamps_to_floor_and_ceiling_and_refuses_zero` |
| M5 LIFO | `for fut in reversed(self._waiters)` | 1/1 | **pass** | **fail** `…waiters_are_admitted_in_arrival_order` |
| M6 no cancel recovery | drop the woken-then-cancelled `borrowed -= 1; _wake_next()` | 1/5 | **pass** | **fail** `…a_waiter_cancelled_after_being_woken_hands_its_slot_on` |
| M7 charge at resume | move `borrowed += 1` from `_wake_next` into the waiter's `finally` | 2/2 | **pass** | **fail** `…a_freed_slot_is_charged_at_wake_not_when_the_waiter_resumes` |

**M7 is the one worth reading.** On its first run it did **not** discriminate: the contention test
(M1's target) passed under it, exit 0. The woken tasks are scheduled ahead of any later arrival, so
the over-admission race usually does not open — the docstring claim "pinned by tests" was false as
written. The window only opens when two slots are freed back to back with no `await` between them:
both waiters chosen, neither resumed, `borrowed` still reading 0, and an arrival there is admitted
over the ceiling. `431b02f` adds a test that constructs exactly that state and *steps* an arrival's
`acquire()` coroutine by hand (`send(None)`) rather than awaiting it, because awaiting would hand
control to the woken waiters and close the window. Re-run: M7 now fails it, 2/2 numstat, OLD still
passes. I would have shipped a false docstring had I not run the mutation.

M6 also has a **non**-discriminating partner worth recording: the parked-waiter cancellation test
(criterion (e), "2 queued, cancel the first, release once, the second runs") **passes under M6**.
That criterion exercises the `fut.cancelled() is True` path, which needs no recovery. Both tests
ship; only the second carries the claim.

### Tests

`tests/test_budgets.py` 29 passed in 2.79s (was 28 before; 9 added, 1 pre-existing test
untouched and still green). Scoped confirmation run
`tests/test_budgets.py tests/test_workers_scan.py tests/test_runner.py tests/test_scan_e2e.py`
— **116 passed in 13.84s**, clean `bazel disk` line, `0 tests skipped`. `mypy` (strict, the
project config) — `Success: no issues found in 115 source files`. `ruff check` clean on both
files. No real sleeps anywhere: the only time primitive is `asyncio.sleep(0)` in a `_settle`
helper. Full suite not run (brief: scoped runs only); no concurrent pytest session; no `FLEET_*`
exported.

`tests/test_findings_kinds.py` is untouched and irrelevant — R1 adds no findings writer and no
`INSERT INTO findings`.

## 4. Concerns and things for the orchestrator

1. **R1 needs an ADR and I did not take a number** (brief: numbers allocated centrally). §5's R1
   row lists `docs/DECISIONS.md`, and §5's ADR list item 1 names the subject precisely: the
   concurrency primitive, hand-rolled vs `anyio.CapacityLimiter`, and whether `anyio` becomes a
   *declared* dependency. The decision as landed is: hand-rolled, `anyio` **not** adopted,
   `pyproject.toml` unchanged. Someone must write it up under an allocated number.
2. **`docs/PROGRESS.md` not updated** (Rule 10). It is not in my ownership list and other lanes
   are appending to it this round; I did not want a merge conflict on a shared tail.
3. **`src/fleet/cli.py:10074` still says "All twelve workers implement …"** — the stale string
   §1.1 predicted at `main:10044`, shifted ~30 lines by the live lane's uncommitted edit, exactly
   as §0 warned. It is present in the committed file, not the sibling's diff. **Not edited** —
   `cli.py` is on the do-not-touch list. §5 assigns it to R4.
4. **`tests/test_workers_scan.py:188` `FakeLimits.for_tier(self, tier) -> asyncio.Semaphore` is
   now a stale annotation.** It still works and its tests pass (the fake is duck-typed, and
   `classify` only ever does `async with`), and `mypy` does not check it — `pyproject.toml` sets
   `packages = ["fleet"]`, so `tests/**` is outside the type check. Not edited: outside my
   ownership. R4 touches that area and should sweep it.
5. **Docstring claims R1 deliberately leaves false.** `budgets.py`'s `for_tier` docstring still
   says "the limiter every `ModelClient.complete` on this tier must hold" — true of one of five
   callers (§3.2). I changed only the noun (`semaphore` → `limiter`, forced by the retype) and
   left the claim standing, because §5 assigns that sweep, together with `workers/base.py:323`
   and `orchestrator/context.py:126`, to **R4** — the change that makes it true. Narrowing it now
   would have to be un-narrowed one subtask later.
6. **None of §7's four open questions bears on R1.** Q1 (`WorkerRepairError` and
   `phases.attempts`) is R3/R4 territory, Q2 (the throttle-retry bound) is R3's ADR, Q3
   (`bedrock`'s `Retry-After`) is R2, Q4 (`--accept-drift concurrency` coverage) is orthogonal. I
   invented no answer to any of them.
7. **R1 is a genuine no-op at runtime today.** Nothing calls `resize`, so the shipped behaviour is
   identical to `asyncio.Semaphore`'s. That is what "no wiring, no controller, no config read"
   means and it is the right shape for a primitive — but it also means the *value* of this commit
   is entirely contingent on R4 (widen acquisition) and R5 (the controller) landing. If the
   orchestrator takes §6.3's advice and ships R2+R3 only, this commit buys nothing and §4.3's
   Option D (decline AIMD, rewrite the SPEC §11.8 sentence) would make it dead code that should
   be reverted rather than left in the tree.
