> Round-C lane **LIMFIX** (the `ResizableLimiter` cancellation/ceiling fix, `d44b94f`) task report, produced under plan `docs/superpowers/plans/design-resume-step5.md`; promoted substantively unchanged from `.superpowers/sdd/design-resume-step5/task-limfix-report.md` (git-ignored scratch) because `docs/DECISIONS.md` ADR-0084 cites its §7 item 1 as the provenance of an Agent Recommendation awaiting a ruling before R5. Body below is byte-identical to the scratch original.

# LIMFIX report — the unguarded wake in `ResizableLimiter`'s cancellation recovery

**Status: COMPLETE.** Commits `d44b94f` (fix + two tests) and `05571c9` (review-8 m-1, out of
brief scope, separate so it reverts alone). `git status --short` at start showed only
`src/fleet/cli.py` modified by a sibling lane; nothing of mine touched it, and it is still
modified and uncommitted at finish. `src/fleet/sandbox/container.py` and `tests/test_sandbox.py`
were modified by another lane mid-run and landed as `aa16846`; neither is reported as a defect.

## 1. The defect, reproduced before fixing

`src/fleet/orchestrator/budgets.py`, `acquire()`'s `except asyncio.CancelledError` block. It read:

```python
if not fut.cancelled():
    # Woken, then cancelled: we own a slot we will never use. Hand it straight on.
    self._borrowed -= 1
    self._wake_next()
```

`release()` guards the same call with `if self._borrowed < self._capacity:`. The recovery did not,
so a `resize()` down landing between the wake and the resume left `_borrowed` above `_capacity`,
and the hand-off charged the slot to the next waiter anyway.

Reproduced with a standalone script (no pytest, no sleeps) before any edit. Trace, unfixed:

```
parked:              borrowed=2 capacity=2 inflight=2
after release:       borrowed=2 capacity=2 inflight=1   # freed slot charged to `first`
after resize(1):     borrowed=2 capacity=1 inflight=1   # the 429
second ENTERED       inflight=2 capacity=1 borrowed=2   # <-- over-admission
BREACHES: [('second', 2, 1)]
```

**Two in flight against a ceiling of one.** Exactly the reviewer's numbers. Same script on the
fixed class: `BREACHES: [] STRANDED: False` — nothing over-admitted, and the withheld slot still
goes out on the next drain rather than being swallowed.

Note for R5: `borrowed` reads **2 before and 2 after** the over-admitting cancellation. The leak
never raises the counter, because the slot was already charged. Any instrument watching
`borrowed`, `capacity`, or the slack between them cannot see this defect.

## 2. Is `release()`'s guard the right model? Yes — checked, not copied

Verified rather than assumed, per the brief. The class has exactly **two charge sites**:
`acquire()`'s fast path (`_borrowed += 1` at `:1027`, guarded inline by `if not self.locked()`,
i.e. `_borrowed < _capacity`) and `_wake_next()` itself. `_wake_next` charges unconditionally by
design and pushes the ceiling check onto its callers, so every caller owes one. `release()` frees
exactly one slot and so needs a single `if`, not a loop; `resize()` may free many and correctly
uses a `while`. The cancellation recovery frees exactly one, so `release()`'s `if` is the right
shape. The invariant that makes one wake per freed slot sufficient — waiters non-empty implies
`_borrowed >= _capacity` — holds because every path that creates headroom drains synchronously.

Dropping the wake when the ceiling is full strands nothing: the next `release()` or `resize()`
re-drains the queue once headroom is real, which §3's second assertion pins.

**I did not find the reviewer's guard wrong.** `release()` at `:1046` (now `:1054`) is correct.

## 3. Sweep of every `_wake_next()` call site

Guardrail 7 — the class, not the reported site.

| Site | Context | Verdict |
|---|---|---|
| `budgets.py:1046` | `acquire()` cancellation recovery | **was UNGUARDED — the defect; now `if self._borrowed < self._capacity`** |
| `budgets.py:1055` | `release()` | guarded, `if self._borrowed < self._capacity` — correct |
| `budgets.py:1067` | `resize()` | guarded by the `while self._borrowed < self._capacity and self._wake_next()` short circuit, which re-evaluates before every wake — correct |

Three sites, one was unguarded, and it is the reported one. `_wake_next` has no other caller in
the repo. The other charge site, `acquire()`'s fast path, is guarded inline by `locked()`.

Wider shape sweep: the other four `except asyncio.CancelledError` blocks in `src/` —
`util/proc.py:417`, `workers/base.py:909`, `runner.py:721`, `runner.py:738` — kill a child
process, cancel a leaked task, or re-raise. None hands a limited resource to a queued waiter, so
the shape does not recur. The `asyncio.Semaphore` gates (`git_net`, `subprocess`, `docker`) have
no `resize`, so no shrink window can open under them.

## 4. Tests, and the discriminating mutation

Both new tests assert **admission** — who got inside against the ceiling then in force — not the
counter, which §1 shows is blind here.

### 4.1 `test_a_cancelled_waiter_hands_no_slot_over_a_ceiling_that_shrank_under_it`

Deterministic, hand-stepped in the file's existing style (`_settle()` / no real sleeps; the test
task holds the slots itself so no holder task can race the window). Asserts (a) no task is
admitted while `inflight > capacity`, (b) nobody is admitted at all during the window, and (c)
the withheld slot still goes out on the drain, so the fix does not reintroduce the "one
cancellation permanently shrinks the ceiling" bug the class docstring warns about.

**Discriminating mutation.** The guard was removed from the file programmatically, with an
`assert s != before` proving the file actually changed (56 bytes removed) and the mutated lines
printed back before any result was read — a silent no-op would have made the result worthless.
Under the mutation:

```
test_shrinking_while_slots_are_held_bars_entrants_and_harms_no_holder   PASSED
test_a_waiter_cancelled_while_parked_consumes_no_wake                  PASSED
test_a_waiter_cancelled_after_being_woken_hands_its_slot_on            PASSED
test_a_cancelled_waiter_hands_no_slot_over_a_ceiling_that_shrank_under_it  FAILED
test_a_freed_slot_is_charged_at_wake_not_when_the_waiter_resumes        PASSED
```

`AssertionError: a cancelled waiter admitted someone over the shrunk ceiling: [('second', 2, 1)]`

This is the Rule 12 shape. `test_a_waiter_cancelled_after_being_woken_hands_its_slot_on` walks the
**same recovery line** and still passes under the mutation — the old assertions are satisfied by
the broken code, and only the new one fails. Guard restored from a byte-for-byte backup, verified
by `git diff --stat`.

### 4.2 `test_random_resize_and_cancel_interleavings_never_admit_over_the_ceiling`

No fuzz existed in the file, so one was written. 400 seeds, 20 tasks, random `resize`+`cancel`.
Instrument: a `ResizableLimiter` subclass wrapping `_wake_next` to record any wake taken while
`_borrowed >= _capacity`.

Validated on three states (Guardrail 6) before its clean result was trusted, using the **committed**
test body, not just the scratch prototype:

| State | Result |
|---|---|
| Known-bad (guard removed) | fires, **68/400 seeds, 71 events** (e.g. seed 13: a 3rd slot charged against a ceiling of 2) |
| Swept (the fix) | silent, **0/400** |
| Fresh synthetic fault injected into the swept class (an unguarded second `_wake_next` in `release`) | fires, **400/400 seeds, 3,531 events** |

**Why the reviewer's 800-seed fuzz reported clean over live code.** I reproduced the false clean.
My first driver had a *correct* instrument but awaited once per operation, and found **0/800 on
the unfixed class**. A woken-but-not-resumed waiter exists only between a charge and the next turn
of the event loop, so a driver that awaits after every operation can never cancel one and never
reaches this path at all. Issuing bursts of 1–4 operations inside a single turn is what reaches
it. Both traps — the counter-blind instrument and the await-per-operation driver — are written
into the test's docstring, since either alone reproduces the reviewer's false negative.

One instrument bug of my own, caught and fixed: the first repro's "was the slot stranded" check
read `task.done()` across two overlapping `_settle()` windows and reported a false strand on
correct code. Replaced with an explicit "did it enter" list.

## 5. mypy

`mypy --strict` (the configured gate, `packages = ["fleet"]`): **Success: no issues found in 115
source files** — clean before and after. `tests/test_budgets.py` is outside that gate; run
directly it carried 7 pre-existing errors. An apples-to-apples baseline (checking out `HEAD`'s
copy of the file, running from the repo root so the same config and plugins apply, restoring) gave
an **identical error set** to mine ignoring line numbers, so my additions introduce none. `05571c9`
then removed 3 of them. An earlier comparison that ran mypy from a scratch directory reported
5-vs-7 and was discarded: without the repo `pyproject.toml` the pydantic plugin was absent and the
reachability analysis differed. That was a config artifact, not a regression.

## 6. Scope, ADRs, docs

- **ADR-0083 is untouched and unaffected.** It records the choice of primitive, its surface, and
  the decision not to declare `anyio`; it makes no claim about the cancellation recovery. This
  repairs the implementation rather than changing the decision, so per the brief there was nothing
  to stop for. No ADR written, no number taken.
- `docs/DECISIONS.md`, `docs/PROGRESS.md`, `src/fleet/cli.py`, `src/fleet/sandbox/container.py`:
  not touched.
- The class docstring bullet at `:974` stated the hand-off unconditionally and would have been
  false after the fix. Corrected in the same commit (Guardrail 7 — the doc is what the next author
  reconciles against). Grepped for other listings of this code: `docs/SPEC.md` contains none, and
  ADR-0083's citations are line ranges and a method list, both still accurate.

## 7. Concerns

1. **`_wake_next` charges unconditionally and pushes the ceiling check onto three callers.** That
   is the shape that produced this defect, and R5 will add callers. Moving the check inside
   `_wake_next` would make it structurally impossible — but it would also change `resize()`'s
   loop into something subtler and touches ADR-0083's recorded surface, so I did not do it under
   this brief. Worth an explicit ruling before R5 lands.
2. **The reviewer's clean fuzz result is in the record somewhere.** It was a false negative for the
   reason in §4.2. If it is cited anywhere as evidence the limiter is sound under resize+cancel,
   that citation is wrong; I did not go looking outside my owned files.
3. **The shared scratchpad is genuinely shared.** A sibling lane overwrote a file I had just
   written at the same path, and I unknowingly executed *its* script and read its output as mine
   for one step. Caught only because the output was about container reaping. Every lane writing to
   `.../scratchpad/` with a plausible generic filename is one collision away from a wrong finding;
   I moved to a per-lane subdirectory.
4. **`if not fut.cancelled()` is a proxy for "the future was settled".** True today — a task
   cancelled while parked gets its future cancelled, so `not cancelled()` implies `done()` with a
   result. `if fut.done() and not fut.cancelled()` would state the intent directly. Left alone
   under Rule 3; noting it because a future refactor could make the proxy false silently.
5. `tests/test_budgets.py` still has 4 pre-existing mypy errors when checked directly (2 at the
   `Ceilings.replace` helper, 2 unreachable-statement). Outside the gate and outside this brief.
