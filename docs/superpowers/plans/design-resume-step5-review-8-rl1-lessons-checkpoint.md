> Promoted from `.superpowers/sdd/design-resume-step5/review-8.md` (round C scratch, lane Review-8), snapshot taken while `main` was at `1050e0a`. Examines RL1 (`a3ff0ae`, `431b02f`), LESSONS (`f84edcb`), CHECKPOINT (`22ac0cf`). Verdict: 0 Critical, 2 Important, 5 Minor. NOTE: this header shifts original line numbers by +2 (one header line + one blank line before original content resumes at line 3) — a citation of scratch `review-8.md:N` is line `N+2` here; e.g. `design-resume-step5-orchestrator-ledger.md:851` cites `review-8.md:74-86`, which is `:76-88` in this file.

# Review 8 — RL1, LESSONS, CHECKPOINT

Read-only audit of `a3ff0ae`, `431b02f` (RL1), `f84edcb` (LESSONS), `22ac0cf` (CHECKPOINT).
Rubric: `CLAUDE.md` at `HEAD` (123 lines, itself amended by `f84edcb`), Rule 12 and Guardrails 6–7.

**Tree state at review.** `git rev-parse --short HEAD` → `27cb03b`; two commits have landed past the
reviewed set (`e4c1004`, `27cb03b`). `git status --short` at entry: `M src/fleet/cli.py`,
`M tests/test_floor_rule_statements.py`, `?? .superpowers/`; at exit: `M docs/DECISIONS.md`,
`M src/fleet/cli.py`, `?? .superpowers/` — sibling lanes are live and **no uncommitted edit of theirs
is reported below as a defect**. All committed state read at `git show <ref>:<path>`.

**How the code was exercised.** Detached worktree at `22ac0cf` under
`/tmp/claude-1000/-home-redmage-swe-repo-harness/aa676f65-.../review8-wt` — outside the shared
scratchpad, its own `BAZEL_ROOT` (`conftest._bazel_root()` resolves inside the worktree, whose path
has no space), every `cd` guarded `|| exit 1`. Scoped pytest only, one session at a time, no `FLEET_*`
exported, `.venv/bin/python` throughout. Worktree removed at the end; `worktrees/wt-WT1-example`
untouched.

---

## Verdicts

| Lane | Verdict | Critical | Important | Minor | Nit |
|---|---|---|---|---|---|
| **RL1** (`a3ff0ae`, `431b02f`) | **APPROVE WITH ONE IMPORTANT** | 0 | 1 | 1 | 0 |
| **LESSONS** (`f84edcb`) | **APPROVE** | 0 | 0 | 2 | 0 |
| **CHECKPOINT** (`22ac0cf`) | **APPROVE WITH ONE IMPORTANT** | 0 | 1 | 2 | 0 |

Totals: **0 Critical, 2 Important, 5 Minor.**

---

## 1. RL1 — `ResizableLimiter`

### 1.1 What I reproduced

* `tests/test_budgets.py` — **29 passed in 1.01 s** in the detached worktree (lane claimed 29).
* `mypy` (project config, strict) — **`Success: no issues found in 115 source files`**, exactly the
  lane's figure. The `Mapping[ModelTier, asyncio.Semaphore]` → `ResizableLimiter` retype breaks no
  typed caller.
* `git grep` for the acquisition surface: the only `async with ctx.limits.for_tier(tier)` in `src/` is
  `workers/classify.py:162`. The drop-in claim holds.

### 1.2 The seventh mutation, spot-checked (the one that nearly shipped a false claim)

Applied M7 (charge at resume) to the committed tree by exact string replacement, asserting the anchor
matched **exactly once**, and reading `git diff --numstat` **before** any test result:

```
1  1  src/fleet/orchestrator/budgets.py      # non-no-op proven first
```

Mutation: delete `self._borrowed += 1` from `_wake_next` (`budgets.py:1063`) and add it to `acquire`'s
`finally` (`:1033`).

| test | under M7 |
|---|---|
| `test_limits_key_llm_semaphores_by_tier_not_by_backend` (OLD, pre-existing) | **passed** |
| `test_limiter_never_admits_more_than_capacity_under_contention` (M1's target) | **passed** |
| `test_a_freed_slot_is_charged_at_wake_not_when_the_waiter_resumes` (NEW, `431b02f`) | **FAILED** |

Old-passes / new-fails on the same input, on a mutation proven to have changed the file. **The lane's
disclosure is accurate**: the contention test does not discriminate this property, and shipping
`a3ff0ae` alone would have left the docstring's "pinned by tests" false for one of the two properties
it names. `431b02f` is load-bearing, not decorative.

**Second check — is the hand-stepped arrival decorative?** Under M7 the test fails at its bookkeeping
assertion (`tests/test_budgets.py:1024`, `borrowed == 2`) before reaching the `arrival.send(None)`
step, whose `except StopIteration` branch carries `# pragma: no cover`. I deleted line 1024 and re-ran:
the test still fails, at `tests/test_budgets.py:1031`, `"an arrival in the wake window was admitted
over the ceiling"`. **The coroutine-stepping construction discriminates on its own.** Both halves earn
their place.

### 1.3 Correctness under contention, cancellation and resize

Reasoned over `budgets.py:1022-1067` and then fuzzed: 20 tasks against capacities 1–4 with random
`resize` and random `Task.cancel`, 800 seeds, checking (a) no task inside the block while
`live > capacity`, (b) `borrowed >= live` at entry, (c) `borrowed == 0` and `_waiters` empty after
quiescence.

* **monotone-grow runs: 0/800 anomalous.** No over-admission, no leaked slot, no orphaned waiter.
* **runs including shrinks (leak checks only): 0/800 anomalous.**
* **Instrument validated** (Guardrail 6, three checks): M1 (`if not self.locked():` → `if True:`,
  numstat 1/1) fires on **800/800**; the clean tree is silent; M6 (drop the woken-then-cancelled
  recovery, numstat 2/4) **deadlocks the fuzz** — which is its documented failure mode, two leaked
  slots wedging a limiter.
* **FIFO is genuinely FIFO.** `_wake_next` (`:1060-1067`) skips `done()` futures, so a woken-but-
  unresumed waiter still sitting in the deque is never re-chosen and never blocks the one behind it;
  the `finally: self._waiters.remove(fut)` at `:1033` runs before the `except` at `:1034`, so the
  recovery path cannot re-select the future it is recovering from. No waiter is lost or double-woken.
* **The fast path cannot barge.** `acquire`'s `if not self.locked()` (`:1024`) has no queue check, but
  it does not need one: `release`, the `resize` grow loop and the recovery all charge the slot
  synchronously inside `_wake_next`, with no `await` between freeing and charging, so "headroom exists"
  and "a live waiter is parked" are never simultaneously true. The lane's decision to drop CPython's
  extra `locked()` clause is correct and is argued correctly in its report.
* **Shrink below the holder count is sane**: `borrowed` is allowed to sit above `capacity` and drain;
  no holder is cancelled or errored. `resize`'s grow loop terminates (`_wake_next` returns `False` when
  no live waiter remains). Clamping, the `floor`/`ceiling` bounds and the `ValueError` on `capacity < 1`
  all behave as documented. `release()` without `acquire()` raises (Rule 11).

### 1.4 I-1 (Important) — the cancellation recovery ignores a ceiling that shrank under it

**`src/fleet/orchestrator/budgets.py:1035-1039`** vs **`:1045-1047`**.

`release()` guards its wake with `if self._borrowed < self._capacity:`. The woken-then-cancelled
recovery does not — it decrements and calls `_wake_next()` unconditionally. When the ceiling was
shrunk *between* the wake and the cancellation, the slot the cancelled task never used is handed to the
next waiter instead of being allowed to drain, admitting a call over the shrunk ceiling.

Reproduced (`.venv/bin/python`, worktree at `22ac0cf`):

```
parked; borrowed 2 cap 2
after release+shrink: borrowed 2 cap 1        # release() charged A; resize(1) is AIMD's halving
after A cancelled:    borrowed 2 cap 1 ran ['B']
>>> OVER SHRUNK CEILING: cancellation recovery woke a waiter release() would not have
```

**Failure scenario.** R5 lands the AIMD controller. A HEAVY tier at ceiling 2 takes a 429 and halves to
1. An in-flight call whose `acquire()` had just been woken is cancelled — an `asyncio.timeout`, a
`TaskGroup` unwinding a sibling failure, an operator interrupt. Instead of the halving taking effect,
the tier keeps two calls in flight against a provider that just throttled it, and the next 429 halves
again. This is the exact property `test_shrinking_while_slots_are_held_bars_entrants_and_harms_no_holder`
(`tests/test_budgets.py:770-812`) pins — but only along the `release` path; the recovery path is
uncovered.

**Reachability (Rule 12's stop rule).** Not adversarial-only: it needs no subclass, no monkeypatching
and no deliberate misuse — only a cancellation and a shrink, which is the workload R5 exists to create.
Accidentally reachable ⇒ a defect, not a boundary.

**Latency of harm.** Zero today: nothing calls `resize()`, so the shipped behaviour is
`asyncio.Semaphore`'s. It becomes live the moment R5 does.

**Fix** — one line, and one test in the shape the lane already uses:

```python
        except asyncio.CancelledError:
            if not fut.cancelled():
                self._borrowed -= 1
                if self._borrowed < self._capacity:      # <- the guard `release()` already has
                    self._wake_next()
            raise
```

The class docstring at `budgets.py:979-984` should then stop listing "the cancellation recovery" beside
`release` and `resize` without qualification, or say that it, like `release`, respects the current
ceiling. Discriminating test available directly: the probe above, asserting `ran == []` and
`borrowed == 1` after the cancel — it passes on the fixed code and fails on the code as landed.

### 1.5 m-1 (Minor) — a stale annotation inside the file the lane edited

**`tests/test_budgets.py:707`**: `async def slots(sem: asyncio.Semaphore) -> int:` is now handed a
`ResizableLimiter` by lines 715-717. Runtime-safe (duck-typed on `locked`/`acquire`) and invisible to
`mypy`, which is scoped to `packages = ["fleet"]`. The lane disclosed the identical stale annotation at
`tests/test_workers_scan.py:188` (report §4.4) and deferred it to R4 as outside its ownership — correct
— but this one is in a file `a3ff0ae` edits, so Guardrail 7's "sweep for the class" applies within it.

**Failure scenario**: the next author widening `slots()` to cover `resize` reads the annotation, writes
`sem._value`, and gets an `AttributeError` at runtime because `__slots__` has no `_value`.

### 1.6 Confirmed as disclosed, not re-raised

* **Runtime no-op.** `Limits.create` *does* construct `ResizableLimiter` on the production path
  (`budgets.py:1106`), so it is live-but-inert rather than unreachable: every `classify` LLM call
  already goes through it, and behaves identically to the semaphore it replaced because `resize()` has
  no caller anywhere in `src/`. Recorded honestly in three places — RL1 report §4.7 ("a genuine no-op
  at runtime today"), §39 item 18, and §39's next-task item 9 ("`ResizableLimiter` exists and decides
  nothing"). Nothing is half-wired.
* **"semaphore" wording surviving** at `workers/base.py:323`, `orchestrator/context.py:126`,
  `workers/classify.py:25`, `tests/test_runner.py:647` and in the name
  `test_limits_key_llm_semaphores_by_tier_not_by_backend`. Assigned to R4 together with the
  `for_tier` docstring's over-broad claim, and disclosed in report §4.5. Deferring is right: narrowing
  the docstring now would have to be un-narrowed one subtask later.
* `cli.py:10074`'s "All twelve workers" — known and blocked on the in-flight lane.

---

## 2. LESSONS — `CLAUDE.md` 118 → 123

**Verified.** `git show 6a5e534:CLAUDE.md | wc -l` → **118**; `git show f84edcb:CLAUDE.md | wc -l` →
**123**; `wc -l CLAUDE.md` → **123**. The diff touches only §3, Rule 12, §6, Guardrail 6 and
Guardrail 7 — Sections 1, 2, 5, Rules 1–11 and Guardrails 1–5 are byte-identical, as the report claims.

**Falsifiability of each amendment** (the review's own rubric question — is it a check or is it
advice?):

| site | new/amended | a lane can tell whether it complied? |
|---|---|---|
| `:25` §3 "A Ruling in a Brief Is a Fallback" | new | **Yes** — did the brief mark the ruling conditional; did the worker cite the primary source before implementing |
| `:84` §6 detached worktree | new | **Yes**, purely mechanical: outside the shared scratchpad, own `BAZEL_ROOT`, `\|\| exit 1` on every `cd` |
| `:68` Rule 12 "mechanical, read before the result" | amended | **Yes** — did the harness abort on zero changed lines, and was that read first (see m-2 on wording) |
| `:111` G6-1 "measured ≠ reproducible" | amended | **Yes** — is the predicate and normaliser stated beside the count; is it a class result |
| `:113` G6-3 the control as a fourth check | amended | **Yes** — a reflow of the same region stayed green |
| `:114` G6-4 two structural causes | amended | **Yes** — was the scope line fixed at the root; was the multi-site correction one author, one commit |
| `:115` G6 recognition-vs-resolver | new | **Yes** — does a two-direction cross-check exist; are exemptions subtracted by rule |
| `:121` G7 whole-file normalised sweep | amended | **Yes** — grep or offset→line map |
| `:122` G7 "prose drives the assertion" | new | **Yes** — is the claim parsed *out of* the prose |
| `:123` G7 "history: annotate, never rewrite" | new | **Yes** — a dated marker beside the claim vs. an edit to the evidence |

**No rule is advice-only, and none restates an existing one.** I checked specifically for the file's own
stated failure mode: the two candidates the lane *dropped* (Guardrail 6's third check as a standalone
rule; the mutation-abort as a standalone rule) were both already in the file, and dropping them was
correct. The new G6 recognition bullet is a genuine increment on G6-3 (recognition is upstream of the
resolver both bullets validate), and the new G7 prose-binding bullet is a genuine increment on G7-1
(mechanised binding vs. co-editing).

**Citations resolve** — I checked every one the amended guardrails carry:
`_recognition_gap` in `tests/test_findings_kinds.py` ✓ · `reentry._HARD_STOPS` at `reentry.py:54` ✓ ·
`phase_floor` parsed in `tests/test_floor_rule_statements.py:59,24-25` ✓ ·
`test_transition_demotes_without_writing_a_record_or_naming_a_new_sink` at `tests/test_state_models.py:711` ✓ ·
`TRANSITION_GLOBALS` at `tests/test_state_models.py:693` ✓ ·
`tests/test_settings.py:591-593` ✓ (591 `"anthropik" in message`, 593 `"pip install" not in message`) ·
`src/fleet/models/tasks.py:100-102` ✓.

### m-2 (Minor) — one outbound citation went stale during the round and was not swept

**`CLAUDE.md:120`** cites **`docs/SPEC.md:5671`** as the line that "now forbids that growth in the
listing itself". Measured:

```
6a5e534   supports_effort prohibition at: 5671    # correct at the round base
f84edcb   supports_effort prohibition at: 5733    # already stale when this commit landed
HEAD      supports_effort prohibition at: 5734
```

`docs/SPEC.md:5671` today is `        messages: Sequence[Message],`. The lane's report records the
sweep it *did* run — `grep -rn "CLAUDE\.md:[0-9]" docs/ src/ tests/` → nothing — which is the
**inbound** direction. The outbound direction (CLAUDE.md's own `file:line` citations, re-checked
against a tree that moved ~60 lines in `docs/SPEC.md` this round) was not swept, and it is the one
Guardrail 7's own "after any rename or renumber, sweep the whole tree" covers.

**Failure scenario**: a lane obeying G7's second bullet opens `docs/SPEC.md:5671`, finds a parameter
declaration, concludes the prohibition was deleted, and adds `supports_effort` to `ModelCapabilities` —
the exact regeneration the bullet exists to prevent. Bullet 2 of the same guardrail is the one that
records this defect outliving its adjudication by six rounds.

Fix: `:5734`, or cite the anchor by text (`"must not grow one to make code match this table"`) rather
than by line.

### m-3 (Minor) — "the harness" names an artifact that does not exist

**`CLAUDE.md:68`**: *"the harness itself aborts when `git diff` reports zero changed lines for the
mutation, and that check is read **before** the test result is."* There is no committed mutation
harness — `tools/bin/` holds only the toolchain wrappers (`ast-grep bazel cargo gazelle gh go rustc`),
and nothing under `src/`, `tests/` or `tools/` implements one. Each lane writes its own ad hoc.

The definite article reads as a description of an existing enforced mechanism. It is Rule 12's own
stop-rule warning turned on the file: *"never close a documentary gap with a convention wearing a
mechanism's clothes — a fake mechanism is worse than an honest disclosure because it looks enforced."*
A lane could read this and skip building the abort, believing something already enforces it.

Minor rather than Important because the sentence is still actionable as a prescription and RL1 in fact
implemented it (its harness asserts the anchor matched once and prints `--numstat` before any result).
Fix is one word: *"your mutation harness must abort when…"*.

---

## 3. CHECKPOINT — `docs/PROGRESS.md` §39

### 3.1 Re-measured, and what reproduced

| §39 claim | my measurement | verdict |
|---|---|---|
| 37 commits, `6a5e534` (excl.) → `b1de36e` | `git rev-list --count 6a5e534..b1de36e` → **37** | ✅ |
| 21 of 37 touch `src/`/`tests/`, 16 doc-only | classified each commit's `--name-only` → **21 / 16** | ✅ |
| code delta `+2,998 / −25` across 15 files | `git diff --stat 6a5e534..b1de36e -- src/ tests/` → **15 files, 2998 insertions, 25 deletions** | ✅ |
| 3 of 10 subtasks done (1, 2, 6); 3 in flight; 6 not started | `git grep phase_floor HEAD -- src/` → definition + prose only; `git grep demote_to_floor HEAD -- src/` → **`repository.py:453` (Protocol) and `:1340` (impl)**, no caller; `demote()` called at `repository.py:1438`; subtask 3 uncommitted in `cli.py` | ✅ |
| 11 registered workers vs `cli.py:10044` "All twelve" | 11; the string is present in `git show HEAD:src/fleet/cli.py` | ✅ (known/blocked) |
| `grep -rni thinking src/ tests/` → 1 | **1**, `tests/test_llm_cache.py:4` | ✅ |
| 79 ADR headings, `0079`/`0080`/`0081` absent, highest `0082` | **79**; only `ADR-0082` of the four is present | ✅ (known/adjudicated) |
| `D70` highest, `D71` free, zero allocated | ✅ | ✅ |
| `CLAUDE.md` 118 → 123 | ✅ | ✅ |
| `git worktree list` → two entries, `wt-WT1-example` intact | two, before I added mine (removed at exit) | ✅ |
| `git log -S"floor" -- schema.sql` returns no commit | **0** | ✅ |
| review 7's I-7 refuted: the quotation is at `reentry.py:80-82` | at `431b02f`, `reentry.py:80-81` states it; the module docstring at `:30-32` states it in different words | ✅ correctly refuted |
| SPEC's "stop at the first phase whose evidence holds" closed at `08ba8e2` | `git show 22ac0cf:docs/SPEC.md \| grep -c` → **0** | ✅ |

**Nothing overclaims a test run.** §39 states plainly that no full suite was run, names §38's
`1575 passed` at `6a41840` as the last whole-tree evidence, marks every per-lane number as
lane-reported, marks the mutation results as not re-run, and lists "Run a full suite" as next-task 7.
That is correct and is the section's strongest habit. Its honesty about its own staleness (the
measurement-window paragraph, "expect commits past `b1de36e`") is well-judged — two commits have
indeed landed since, and I have not penalised that drift.

### 3.2 I-2 (Important) — four figures in §39 are measured at `08ba8e2`, not at its stated anchor

§39's measurement-window paragraph (`docs/PROGRESS.md:5855`) states: **"Every figure here was measured
against `main` at `b1de36e`."** Four figures falsify that sentence. Each is the value that was correct
one commit earlier, at `08ba8e2` — the final re-measure updated the section header and left the body:

| site | §39 says | at `b1de36e` | at `08ba8e2` |
|---|---|---|---|
| `docs/PROGRESS.md:5853` | `test_findings_kinds.py` **566** | **593** | 566 |
| `docs/PROGRESS.md:5853` | four new test modules — **1,385 lines** | **1,412** | 1,385 |
| `docs/PROGRESS.md:5982` (open item 14) | "any of this round's **36** commits" | **37** | 36 |
| `docs/PROGRESS.md:5982` and `:6008` | **`+2,970`** lines of `src/`+`tests/` | **`+2,998`** | +2,970 |

Measured: `git show b1de36e:tests/test_findings_kinds.py | wc -l` → 593 (`b1de36e` itself grew the file
from 566, per §39's own item 20); `git diff --shortstat 6a5e534..08ba8e2 -- src/ tests/` → 2970
insertions, `..b1de36e` → 2998.

Open item 14's "**36** commits" also contradicts the section header's "**37**" two hundred lines above
it, inside one section.

**Failure scenario.** Round D reads open item 14 — the item §39 itself calls "the single largest
unverified claim in the round" — to size the suite run it owes, and gets a commit count and a line
delta that both understate the unverified surface. Separately, a reviewer re-measuring "1,385 lines"
at the anchor the section names gets 1,412, and the measurement-window sentence — the one thing
licensing every other number in the section — reads as false.

This is the round's own dominant class reproducing once more: the correction to draft-overclaim #4
("three new test modules … +1,256 lines" → "four … 1,385", report §4.4) fixed the module count and
re-used a stale per-file number. Remedy per `CLAUDE.md:123`: annotate in place with a dated marker
naming the anchor, do not silently rewrite the section.

### 3.3 m-4 (Minor) — one clause states two counts under two different predicates

**`docs/PROGRESS.md`, §39 item 18**: "`budgets.py` +135, `test_budgets.py` +319". `+319` is insertions
(`git show --numstat`: 279 + 40). `+135` is the `--stat` churn column; `--numstat` gives **`131 4`**.
Two predicates in one clause, and a reviewer re-measuring `+135` with the same tool used for `+319`
gets 131. This is precisely what `CLAUDE.md:111` — added this round — now forbids: *"state the
predicate and the normaliser beside the count."*

### 3.4 m-5 (Minor) — the cited command no longer produces the cited number

**`docs/PROGRESS.md:5853`**: "counted with `git rev-list --count 6a5e534..HEAD` and cross-checked with
`git log --oneline 6a5e534..HEAD | wc -l`". Run today that returns **40**, not 37. The prose does pin
the endpoint ("to `b1de36e`") and the staleness is explicitly disclosed, so this is a reproducibility
wart rather than a false claim — but a time-relative `HEAD` is not a predicate a later reader can
re-run. `6a5e534..b1de36e` is; it reproduces at 37 today and will next year.

### 3.5 Confirmed as disclosed, not re-raised

* ADR-0079/0080/0081 reserved but unwritten — §39 records it in the subtask table, in the "Not landed"
  paragraph and in the next-task list; a lane is queued.
* §38's carried items marked inherited rather than re-verified — §39 discloses this twice, including in
  next-task 10 with the round-B base rate (~80% stale).
* The ledger's incompleteness (RL1, review 7, FIX7 missing) — §39 item 17, and it names the SHAs.
* `cli.py`'s stale strings — open, blocked, correctly attributed to the committed file rather than to
  the sibling's diff.

---

## 4. Summary of actions

1. **RL1 I-1** — guard the cancellation recovery with `if self._borrowed < self._capacity:`
   (`budgets.py:1037-1038`), add the discriminating test from §1.4, and reconcile the class docstring
   at `:979-984`. Must land before R5 wires a controller to `resize()`.
2. **CHECKPOINT I-2** — annotate the four stale figures in §39 with a dated in-file marker naming
   `08ba8e2` as the anchor they were taken at, per `CLAUDE.md:123`.
3. **m-1** — retype `slots(sem: ...)` at `tests/test_budgets.py:707` (or fold into R4's sweep with
   `test_workers_scan.py:188`).
4. **m-2** — repoint `CLAUDE.md:120` from `docs/SPEC.md:5671` to `:5734`, or cite by text.
5. **m-3** — `CLAUDE.md:68`: "the harness" → "your mutation harness must abort…".
6. **m-4 / m-5** — one predicate per count in §39 item 18; pin the range in §39's header command.
