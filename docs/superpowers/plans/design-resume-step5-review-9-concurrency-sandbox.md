> Promoted from `.superpowers/sdd/design-resume-step5/review-9.md` (round C scratch, lane CR-9), snapshot taken while `main` was at `1050e0a`. Examines the concurrency and sandbox code wave: `aa16846`, `d44b94f`+`05571c9`, `99862a9`+`ac7786c`, `3fe9ed7` (anchored at `ead96e6`). Verdict: 0 Critical, 1 Important, 3 Minor, 2 Nits.

# Review CR9 — the concurrency and sandbox code wave

**Read-only.** Nothing in the tree was edited or committed by this review. Every mutation below
was applied to a copy (`/tmp/.../scratchpad/cr9-reviewer/*.py`, loaded over `sys.modules`) or to a
detached worktree at `/tmp/cr9-reviewer-wt`, and every mutation was proved non-no-op by a
`diff`/`git diff --numstat` line count read **before** its test result (CLAUDE.md Rule 12).

`git status --short` at the start of this review:

```
 M tests/test_cli.py
?? .superpowers/
```

`git status --short` at the **close** of this review (the tree moved a lot underneath it):

```
 M src/fleet/sandbox/container.py
?? .superpowers/
?? tests/test_instruments_are_armed.py
```

Both of those are live lanes' in-flight work, not mine and not findings. Neither was read for
findings. Two of them intersect this review and are called out where they land: the uncommitted
`container.py` edit is a lane closing D73, which is the same defect as my **m-3**; and
`tests/test_instruments_are_armed.py` is the instrument-arming lane, which overlaps **m-1**.
Every finding below is anchored at committed `HEAD` (`ead96e6`) and every line number was checked
against `git show`-level content, not the working tree.

`HEAD` was `cf85c8b` when the review opened and `ead96e6` when it closed — a live lane landed
`0b0db5c`/`3440b12`/`ead96e6` (§11.5 step 2 in `cli.py`) mid-review. All six reviewed commits are
ancestors of both. The only in-scope file touched after `ac7786c` is `tests/test_budgets.py`, and
the change is cosmetic (`cf85c8b`, an `E501` reflow of one assertion message at `:1051`). Findings
below are anchored at `HEAD`. `src/fleet/cli.py` and `tests/test_cli.py` were not read for
findings; `cli.py:10608` is cited once, as *evidence that a backstop exists*, never as a defect.

---

## Verdict per commit group

| Commits | Subject | Verdict |
|---|---|---|
| `aa16846` | `sandbox/container.py` + `tests/test_sandbox.py` — `claims()` + namespace floor | **Accept with one Important and two Minors.** The code is right; two committed sentences about it are not. |
| `d44b94f` + `05571c9` | `orchestrator/budgets.py` + tests — cancelled waiter re-reads the ceiling | **Accept, clean.** No findings. |
| `99862a9` + `ac7786c` | `_wake_next` → `_drain`, ceiling check inside the gate | **Accept with one Minor.** Behaviour-preservation verified; the fuzz's driver-side vacuity is still unasserted. |
| `3fe9ed7` | ADR-0084 | **Accept, clean.** Every file:line citation checked at its stated anchor; every mutation-table row reproduced. |

**Counts: 0 Critical · 1 Important · 3 Minor · 2 Nits.**

---

## 1. Instrument liveness — the question this code has already failed twice

Priority 1 of the brief. For every test in the wave: what quantity does it watch, can that
quantity move under the defect it names, and is its hook bound to something a refactor renames
away?

### 1.1 `_watch_admissions` can no longer be silently disarmed — proven, not argued

The predecessor died by subclass-override rename. The replacement hooks
`loop.create_future`. That is a *different* name to rename away, so the class of failure is not
closed by the choice of hook alone — it is closed by there being a **positive** assertion that the
hook fired:

`tests/test_budgets.py:1116` — `assert admissions == [(1, 1)]` inside
`test_a_drain_from_a_full_ceiling_admits_nobody_so_no_caller_needs_a_guard`.

Mutation **A** (instrument disarm, not a behaviour change at all): rewrite `acquire`'s one line
`fut = asyncio.get_running_loop().create_future()` as
`fut = asyncio.Future(loop=asyncio.get_running_loop())` — semantically identical, and exactly the
kind of edit that silently disarmed the predecessor. 2 changed lines, proved non-no-op.

```
FAILED test_a_drain_from_a_full_ceiling_admits_nobody_so_no_caller_needs_a_guard
E   AssertionError: one waiter admitted, inside the ceiling, once
E   assert [] == [(1, 1)]
1 failed, 32 passed
```

The disarm is **loud**. That is the strongest single thing in this wave, and it is a genuine
structural improvement over the predecessor rather than a lucky one: the fuzz's hook liveness is
now underwritten by a *positive* assertion in a neighbouring deterministic test.

### 1.2 The fuzz's own vacuity is closed on the hook side and open on the driver side — **Minor m-1**

`tests/test_budgets.py:1190-1254`. The fuzz extends `over_admitted` only from entries where
`borrowed > cap`. It never asserts that `admissions` was non-empty. It is therefore protected
against a dead hook only *transitively*, through §1.1's test — and not at all against the second
failure mode the file itself documents in that test's docstring: a **driver** that no longer
reaches the woken-but-not-resumed window (the earlier cut reported 0/800 against the unfixed
class). If a future edit changes the burst structure, the operation mix, or `ntasks`, the fuzz
goes vacuous with nothing red.

Measured today, so this is a latent risk and not a live one: at `HEAD` the fuzz's instrument
observes **4,111 admissions across the 400 seeds**. Suggested one-line close: assert a floor on
`sum(len(admissions))`, or `assert admissions, seed` per seed.

### 1.3 Could any test in this wave survive its subject being deleted?

**No.** Every test added or changed in this wave went red under a mutation of exactly its own
named subject. Verified, each mutation proved non-no-op first:

| Mutation | Changed lines | Result |
|---|---|---|
| **M1** `_drain` charges unconditionally, `release`/`resize` guard their own calls (ADR-0084's discriminating row) | 50 | **1 failed, 32 passed** — only `test_a_drain_from_a_full_ceiling…`, message `the gate charged a slot it had no room for: [(3, 1)]` |
| **M2** M1 with the cancellation recovery left unguarded | 3 (vs M1) | **3 failed, 30 passed** — the new guarantee, `d44b94f`'s deterministic test, and the fuzz |
| **M4** `self._borrowed = self._borrowed + 1` in `release`, dodging the `AugAssign` test | 3 | **1 failed** — the whitelist's `rebind_sites` assertion |
| **A** instrument disarm (§1.1) | 2 | **1 failed** |
| **C1** `claims(...)` → `name in live` | 2 | **4 failed, 31 passed** — and *none* of the pre-existing reap tests fail, which is the discriminating direction |
| **C2** `f"{live_name}-"` → `live_name` | 2 | **1 failed** — only the attempt-1/attempt-10 boundary assertion |
| **C3** namespace floor → `if False` | 2 | **1 failed** — only the outside-the-namespace test |
| **W1** worktree floor: drop `not name.startswith(prefix) or` | 2 | **1 failed** — only `test_worktree_reap_never_removes_a_checkout_outside_the_runs_namespace` |

M1, M2, M4, C1, C2, C3 reproduce the commit messages' and ADR-0084's mutation tables **exactly**,
including M1's literal failure text. W1 is mine: the wave's one test whose subject
(`worktree.py`'s floor) was *swept but not changed*, and which therefore had no mutation on
record. It is not inert.

One nuance on M2 worth recording, because it is a measurement discrepancy that is **not** an
error in the ADR. Loading the *literal* pre-`d44b94f` file gives **4** failures, not 3: the extra
one is `test_only_the_fast_path_and_the_drain_may_charge_a_slot`, which trips because that file
still spells the method `_wake_next` so `charge_sites == {"acquire", "_wake_next"}`. ADR-0084's M2
is defined as "M1 with the recovery unguarded", i.e. the *renamed* variant, and that variant
reproduces its stated 3 exactly. Both numbers are right for their own mutation.

`test_instrument_fires_on_a_planted_orphan` passes under C1 — but its subject is the
`reap`/`runner.removed` instrument, not `claims()`, and it is the guardrail-6 validation-1 test.
It is paired with validation 3 (`…_injected_into_the_all_live_pool`), which is what stops a
degenerate empty pool passing. That pairing is correct and is the shape CLAUDE.md asks for.

**Nit n-1.** CLAUDE.md guardrail 6 gained a *fourth* check today — "a cosmetic reflow or reindent
of the same region must stay green". Neither instrument in this wave carries that control. Both
would pass it (the AST test is explicitly reformat-immune by construction; `_watch_admissions`
reads runtime state), so this is bookkeeping, not a gap.

---

## 2. `_drain`'s behaviour-preservation claim — holds in every state checked

Priority 2. The claim is that all three call sites keep their prior behaviour: `resize` is exactly
the old loop, and `release` plus the cancel-recovery still transfer exactly one, "because with
waiters present the first charge restores `_borrowed == _capacity`".

**The reasoning holds, and the load-bearing step is stronger than the ADR states it.** The ADR
argues the premise ("whenever live waiters exist, `_borrowed` was at or above `_capacity` before
the decrement") as a property of the paths. It is in fact an *invariant with a two-line proof*:

1. A waiter is appended only inside `acquire`'s `else` branch, i.e. only when `locked()` is true,
   i.e. only in a state with `_borrowed >= _capacity`.
2. Every path that decrements `_borrowed` or moves `_capacity` — `release` (`:1058-1060`), the
   cancel recovery (`:1051-1052`), `resize` (`:1071-1072`) — calls `_drain()` synchronously
   before returning, and `_drain` exits only when `_borrowed >= _capacity` **or** no live waiter
   remains.

So on entry to any drain reached from `release` or the recovery, `_borrowed >= _capacity - 1`, and
the loop's second pass cannot fire. For it to fire, live waiters would have to coexist with two
slots of headroom, which (1) and (2) forbid.

Checked empirically as well as by argument. A **differential** over 600 seeds compares `HEAD`'s
limiter against `99862a9^` (the pre-refactor, already-correct shape) on a driving script drawn
from the RNG alone — cancel-by-fixed-index, resize-by-fixed-value — so both classes see
byte-identical input regardless of how they behave. Compared: the full admission trace, the entry
order, final `borrowed`, final `capacity`.

```
seeds compared: 600   diverged: 0
CONTROL  old-correct vs known-bad: diverged 25/600   (the differential fires)
```

The control matters: a differential that never diverges is the same trap as a fuzz that never
fires, so it was validated against a known-different pair before its clean result was used.

The three states the brief named, probed directly on `HEAD`:

| State | Result |
|---|---|
| **Zero waiters** — `_drain()` bare, then `resize` down, `_drain()`, `resize` up, `_drain()` | `borrowed 1`, `capacity 3`. The `for`/`else` returns on the empty deque; no charge, no spin. |
| **Capacity raised by more than one** — cap 1, 1 held, 4 parked, `resize(4)` | `borrowed == 4` (1 held + 3 admitted) in one call, entry order `[0, 1, 2, 3]`. Identical to the old `while … and _wake_next()` loop, and ADR-0084 §4 item 2 is the contract this establishes. |
| **Shrink below the number of holders** — cap 3, 3 held, 2 parked, `resize(1)`, then three `release()`s | `(borrowed, admitted)` = `(2,0) → (1,0) → (1,1)`. Nobody is admitted until `_borrowed` actually falls below the shrunken ceiling, and then exactly one goes. |

**FIFO and the synchronous charge (priority 3) all three survived the refactor**, checked at
`HEAD`:
* `_drain` skips `done()` futures rather than removing them — `budgets.py:1089-1090`; a woken
  waiter cannot be chosen twice, and arrival order is preserved.
* The `finally` that removes from the deque (`:1043`) is *inner* to the `except
  asyncio.CancelledError` (`:1044`), so a recovering waiter's own future is gone from `_waiters`
  before its `_drain()` runs and cannot re-select itself.
* `_drain` is a plain `def` with no `await`, and each caller mutates `_borrowed`/`_capacity` on
  the statement immediately above its `self._drain()`. No `await` sits between creating headroom
  and charging it, so `acquire`'s fast path cannot barge headroom mid-drain. The 600-seed
  differential's `entered` trace and `test_waiters_are_admitted_in_arrival_order` both hold.

**Nit n-2.** `_drain`'s inner `for` rescans `_waiters` from the head on every admission, so a grow
of *k* against *W* queued futures is O(k·W) with the already-`done()` prefix walked *k* times.
Irrelevant at tier ceilings of 2–16; noting it only so a future reader does not mistake it for an
invariant.

---

## 3. Re-measured figures — measured vs claimed

Priority 5. Two lanes reported these through different hooks. Reproduced here through a third: the
committed `_watch_admissions` context manager driven by a standalone copy of the committed fuzz
driver, run against the **actual module content at each ref** (`git show <ref>:…` piped to a file,
then loaded), not a hand-written stand-in.

| Figure | Claimed | Measured here | |
|---|---|---|---|
| Known-bad (`d44b94f^`), seeds with an over-admission | 68 / 400 | **68 / 400** | exact |
| Known-bad, over-admission events | 71 | **71** | exact |
| Swept class (`HEAD`) | 0 / 400 | **0 / 400** | exact |
| Synthetic fault (extra unguarded charge in `release`), seeds | 400 / 400 | **400 / 400** | exact |
| Synthetic fault, events | 3,531 | **3,531** | exact |
| M1 discriminating mutation | 32 passed, 1 failed, `[(3, 1)]` | **32 passed, 1 failed, `[(3, 1)]`** | exact, including the message |
| M2 (renamed split + unguarded recovery) | 3 failed | **3 failed** (the three named) | exact |
| M4 rebind escape | whitelist's second assertion fires | **1 failed**, `rebind_sites` | exact |
| ADR-0084 §5 scoped run | 33 passed | **33 passed** | exact |
| Container C1/C2/C3 | 4-fail / 1-fail / 1-fail, pre-existing reap tests unaffected under C1 | **4/31, 1/34, 1/34** | exact |

The synthetic-fault figure reproduced to the event on a fault I reconstructed from its one-line
description before seeing any lane's script, which is about as good corroboration as this kind of
number gets. `3,531` and `71`/`68` are real.

Baseline, scoped, `.venv/bin/python -m pytest tests/test_budgets.py tests/test_sandbox.py`:
**68 passed in 4.85s**, `peak 1.61 GiB (ceiling 6 GiB) · residual output bases 0 bytes`,
`0 tests skipped`. Green by the §6 definition.

`ac7786c`'s sweep is complete. A whitespace-normalised whole-tree sweep for `_wake_next` (not a
line-oriented grep — CLAUDE.md §7) finds **2** occurrences in the tracked in-scope tree, both
deliberate history: `docs/DECISIONS.md` (ADR-0084 narrating the old shape) and
`tests/test_budgets.py:767` (`_watch_admissions` recording the near-miss). Neither is a stale
reference. The remaining hits are in `.superpowers/` reports and in another lane's private
worktree, which are out of scope.

---

## 4. The container predicate's boundary

Priority 4. Judged as the brief asks — whether the boundary is where the lane claims, not whether
erring toward sparing was right.

### 4.1 Can a live container still be reaped? **No, within `container.py`.**

Three live-name shapes exist in the tree and `claims()` covers all three:
* `BuildverifyWorker` build/test step — `<sandbox_name>-t<8 hex>` (`buildverify.py:354`, via
  `_container_prefix` at `:351`). Spared by the `-`-segment branch.
* The C-toolchain probe — `<sandbox_name>-t<hex>-cc-probe`. A *further* `-` segment under the same
  live sandbox; spared.
* `spec_for_attempt`'s default name, which is bare `sandbox_name(run_id, repo, attempt)` — this is
  what `RdepverifyWorker` removes at `rdepverify.py:331`. Spared by the **equality** branch, which
  is therefore load-bearing and not merely "the degenerate case".

The attempt boundary holds in the destructive direction:
`claims("fleet-<run>-acme-commons-1", "fleet-<run>-acme-commons-10-t…")` → `False`, so attempt
10's containers are still reaped while attempt 1 is live, and C2 shows exactly that one assertion
carries it. The namespace floor is checked against `run_prefix(run_id)`, which **ends in `-`**
(`worktree.py:56-58`), so the floor has no bare-prefix hazard of its own. Any residual
"live container reaped" risk is caller-side, in how `live_names` is derived — out of scope.

### 4.2 Can a genuine orphan survive forever? Yes — three ways, one of them undocumented.

* The **stated boundary**: a crashed invocation's orphan and a live retry's orphan of the same
  rung are indistinguishable from a `phases` row, so both are spared. Adjudicated; not re-raised.
* **Finding m-2 (Minor), undocumented**: a *cross-repo* over-spare from slug collisions. Verified
  against the shipped predicate:

  ```
  live  : fleet-<run>-acme-commons-1                    # repo acme/commons, attempt 1
  orphan: fleet-<run>-acme-commons-1-2-tdeadbeef        # repo acme/commons/1, attempt 2
  claims(live, orphan) -> True
  ```

  A different repo and a different attempt, spared for as long as `acme/commons` attempt 1 is
  live. `claims()`'s docstring (`container.py:164-172`) heads this paragraph **"The separator is
  the whole boundary"** and says the `-` requirement "rules that out **by construction**, because
  attempts are digits and a digit is not `-`". That argument is sound for the *attempt* segment
  and does not reach the *repo* segment: `slug()` maps `/` to `-`, so repo ids that differ by a
  trailing path segment produce sandbox names where one is a `-`-delimited prefix of the other.
  This errs in the direction the ADR chose deliberately (sparing), so it is not a safety defect —
  but the docstring is what the next author reconciles against, and "by construction" overstates
  what the predicate buys. Fix is one sentence, not a code change.
* **Finding m-3 (Minor, pre-existing — checked, not inherited)**: `list_by_prefix` returns `[]`
  when `docker ps` is not `ok` (`container.py:355-357`), and `ContainerReapResult.complete` is
  `not self.failed` (`:222-225`). A sweep during a docker outage therefore returns
  `reaped=[] failed=[] complete=True` — "I swept and everything was fine" — while having listed
  nothing. `git show aa16846 -- src/fleet/sandbox/container.py` does not touch either line, so
  this predates the wave; raising it because it is the failure mode that makes an orphan survive
  *silently* rather than loudly (Rule 11), and because `reap()` now has a caller. **Already in
  hand:** a live lane has uncommitted work in `container.py` introducing a `ContainerListing`
  with an `error` field and a `list_with_verdict`, filed as D73. Raised here for the record only;
  it needs no separate action.

### 4.3 The three backstops — **Finding I-1 (Important): one of the three is not on this path**

The brief asks me to verify them. Claimed, in the commit message of `aa16846`, in the `claims()`
docstring at `container.py:178-180`, and at `task-container-report.md:62-63`, as "`run()`'s
`finally`, `on_cancel`'s prefix sweep, the next idempotent sweep".

**First, a correction to my own first draft of this finding, recorded because it is the exact
mistake CLAUDE.md §7 warns about.** I initially read "`run()`" as `BuildverifyWorker.run()`, found
no `finally` anywhere in `buildverify.py`, and wrote the finding up as "the backstop does not
exist". That was wrong. The `finally` is real and is `ContainerSandbox.run()`'s, at
`container.py:270-271` — `try: return await self._runner(argv, …) finally: await
self.remove(spec.name)`. The two *other* mentions of the same phrase in the tracked tree
(`container.py:284`, and one in `tests/test_sandbox.py`) are statements about which callers of
`remove()` are fire-and-forget, and in that context they are **correct**; they are reported here,
not edited, and they are not findings.

**What is actually wrong is narrower and still real: that `finally` is not on the path that
produces the containers `claims()` spares, and has no production caller at all.**

* `ContainerSandbox.run()` has **zero callers in `src/`**. Verified twice, by grep and by an AST
  walk over `src/` and `tests/` for a `.run(` whose receiver is a sandbox: every call site is in
  `tests/test_sandbox.py` (`:712`, `:743`, `:765`, `:1317`, `:1392`, `:1414`). It is the same
  live-but-inert shape ADR-0083 records for `resize()`.
* Even with a caller it would not reach the orphan in question. `BuildverifyWorker` does not go
  through `ContainerSandbox.run()`: it builds the argv itself with `docker_run_argv(spec)`
  (`buildverify.py:792` and `:1133`) and runs it through its own `CommandRunner`. The
  `ContainerSandbox` it constructs at `:1050` is used only for `list_by_prefix`/`remove`. So the
  `finally` that "already reaches" the leak is on a method the leaking code never calls.
* And the sentence names the wrong real paths. The mechanisms that *do* reach a buildverify
  container are `--rm` in `docker_run_argv` (`container.py:133`, which is why a cleanly-exiting
  invocation leaves nothing) and the two `_sweep_containers(ctx)` calls inside
  `BuildverifyWorker.run()` at `buildverify.py:829` and `:958` — both guarded by
  `result.timed_out and result.started`, i.e. a client-side deadline kill only. Neither is named.

Net effect on the argument. The residual the asymmetry accepts is a crashed invocation's orphan
spared while the rung is live. `on_cancel` (`:1038`) reaches it only on explicit cancellation —
its own docstring says so. The `:829`/`:958` sweeps reach it only on a deadline kill. The one
**general** backstop is the third: the next idempotent `reap()`, once the rung stops being live.
That backstop is now real — `reap()` gained a caller at `cli.py:10608`, landed at `e915b93`,
*after* `aa16846`; the commit message's "no committed caller" was true at its own commit and is
correctly left as history. So the honest count is one general backstop plus two conditional ones,
and the enumeration offered names a path that cannot fire.

This is the guardrail-6 shape — a reachability claim reasoned from a name rather than checked
against the environment that runs it — and it is Important rather than Minor because this
docstring is the *entire* justification offered for accepting a known leak, and because §7 makes
it an input to the next author's edit.

Suggested correction, no code change: at `container.py:178-180`, name the paths that exist —
`--rm` on the normal exit, `buildverify.run()`'s two deadline-kill sweeps, `on_cancel` on
cancellation, and the next `reap()` as the only unconditional one — and drop
`ContainerSandbox.run()`'s `finally`, which no production caller reaches. Sweep the class, not the
site: the same enumeration is at `task-container-report.md:62-63` and in `progress.md`. Leave
`container.py:284` and the `tests/test_sandbox.py` mention alone; they are correct where they
stand.

### 4.4 The rest of `aa16846` is clean

The namespace floor is the right shape and is proven rather than argued: the test drives it
through a runner that *ignores* the filter argument, which is the real failure mode
(`list_by_prefix` delegates matching to the daemon's regex), and it asserts both directions —
outsiders untouched **and** `runner.removed == [mine_orphan]`, so a floor that spared everything
cannot pass. `wt-WT1-example` is in the pool deliberately and is genuinely registered with git in
the worktree variant of the test (the premise assertion at
`test_worktree_reap_never_removes_a_checkout_outside_the_runs_namespace` checks git actually
enumerates it, so the test cannot pass by the fake declining to offer the name). "Spared" landing
in neither `reaped` nor `failed` is correct and is asserted. The `worktree.py`
swept-but-not-changed decision is verified: `path_for` (`:144`) and `create` (`:161`) both use
`sandbox_name` verbatim, so no worktree ever carries a suffix and equality is the right predicate
one layer down — and W1 shows its floor is not inert.

---

## 5. Confirmed, not re-raised

* `resize()` still has no production caller; the limiter is live-but-inert (ADR-0083). Confirmed.
* The same-rung container leak is a stated boundary. Confirmed as stated — see 4.3 for the
  backstop count, which is a documentation finding, not a re-litigation of the boundary.
* `tests/test_cli.py`'s lint findings are a live lane's uncommitted work. Not read, not reported.
* A live lane is independently building `tests/test_instruments_are_armed.py` in a private
  worktree, addressing the subclass-override-survives-rename gap. In-progress work by another
  agent; noted so it is not double-reported, and explicitly **not** a finding here.

---

## Findings

| # | Severity | Where | One line |
|---|---|---|---|
| **I-1** | Important | `src/fleet/sandbox/container.py:178-180` (also `task-container-report.md:62-63`) | The `claims()` docstring justifies accepting a known disk leak by naming three backstops, but one of them — `ContainerSandbox.run()`'s `finally` (`:270-271`) — has zero callers in `src/` and is not on the path that creates the spared containers (`BuildverifyWorker` builds its own argv via `docker_run_argv` at `buildverify.py:792`/`:1133`), so the enumeration names a path that cannot fire while omitting the two that can (`buildverify.py:829`/`:958`, deadline-kill only). |
| **m-1** | Minor | `tests/test_budgets.py:1190-1254` | The fuzz never asserts its instrument recorded anything, so a future change to the burst driver — the documented way this fuzz already went blind once, 0/800 on an unfixed class — would make it pass vacuously again; hook liveness is covered only transitively by `:1116`, and driver liveness not at all (measured live today: 4,111 admissions/400 seeds). |
| **m-2** | Minor | `src/fleet/sandbox/container.py:164-172` | "The separator is the whole boundary … rules that out by construction" holds for the attempt segment but not the repo segment: `slug()` maps `/` to `-`, so a live `acme/commons` attempt 1 spares every container of repo `acme/commons/1` forever (verified: `claims()` returns `True`). |
| **m-3** | Minor (pre-existing; a live lane is already fixing it as D73) | `src/fleet/sandbox/container.py:355-357`, `:222-225` | `list_by_prefix` swallows a failed `docker ps` into `[]`, so a sweep during a docker outage returns `complete=True` with an empty `reaped`/`failed` — a silent no-op reported as a clean sweep (Rule 11). |
| **n-1** | Nit | both instruments | Neither carries guardrail 6's newly added fourth check, a cosmetic-reflow control; both would pass it. |
| **n-2** | Nit | `src/fleet/orchestrator/budgets.py:1088-1094` | `_drain`'s inner `for` rescans the already-`done()` prefix once per admission, so a grow of *k* over *W* waiters is O(k·W); immaterial at real tier ceilings. |

No Critical findings. `d44b94f` + `05571c9` and `3fe9ed7` are clean.
