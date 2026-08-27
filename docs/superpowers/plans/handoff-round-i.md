# Handoff — round I

**Written at the close of round H. Base for round I: `main` at `3b98ec8`.**

---

## 0. How to read this document

| tag | meaning |
|---|---|
| **[suite @ SHA]** | certified by the whole-tree suite at that anchor |
| **[probe @ SHA]** | re-derived from the code or tree by a named probe at that anchor |
| **[ledger]** | reported by the lane that measured it; not independently re-derived here |
| **[decision]** | a ruling or deliberate deferral, not a measurement |

**Round H's reading rule.** Round G's was *"ten numbers corrected, every one relayed without
attribution"*. Round H's is narrower, and it is about **which sentence goes unmeasured**:

> **Five orchestrator claims were corrected in round H, every one by a lane. Not one was invention.
> Every one was a REAL thing stated with less precision than the measurement that produced it —
> and in three of five, the unmeasured sentence was not the conclusion but THE REASON OFFERED FOR IT.**

Conclusions get measured because they are what a mutation targets. **Reasons ride along.** A lane wrote
*"closing this requires a quote-aware SQL lexer"*; a reviewer built one in 18 lines. A lane wrote *"a
non-Python line in that fence is a module-wide failure"*; it is 20 passed. A lane wrote *"round G
measured this twice"*; the first instance is round F's. **All three sentences were supporting rationale
inside work that was otherwise correct and well-measured.**

---

## 1. Read these first, in order

1. **This document.**
2. **`CLAUDE.md`** — amended twice in round H (`3a08302`, `238a0d9`). **§6 below proposes the third and
   fourth amendments; §6.1 is the one I would take first.**
3. **`docs/DECISIONS.md`: ADR-0094** (LLM cache attribution is ALL-hit; `AND` considered and
   unimplementable).
4. **`docs/INTEGRATION_HONESTY.md`: D62 and D81**, both `PARTLY ADDRESSED`, both annotated in round H
   with the reason the field did not move.
5. **`docs/superpowers/plans/waveclock-d82-d84-build-plan.md`** — promoted from round H's research lane.
   **8 dispatchable subtasks with per-subtask success criteria.** This is round I's largest ready item.

---

## 2. Verified state at `3b98ec8`

| claim | how verified | tag |
|---|---|---|
| **`1936 passed`, 0 failed, **0 skipped**, xfail 0**, clean `bazel disk` (peak 3.72 GiB, residual 0 bytes, repo cache 1130 MiB), 806.24s | `.venv/bin/python -m pytest -q` — **no `-k`, no node IDs, no path arguments** — primary checkout, **`git status --porcelain` EMPTY at stamp**, anchor `HEAD` + tracked-tree **byte-identical before and after** | **[suite @ 3b98ec8]** |
| Round H = **11 commits** off `5f14ca0` | `git log --oneline` | **[probe @ 3b98ec8]** |
| Test total moved **1912 → 1936** | both endpoints measured; see the warning below | **[probe @ 3b98ec8]** |

**The `+24` is NOT decomposed per commit, deliberately.** Round G's handoff shipped an inferred `+2`
term and said so; round H does not repeat the exercise, because *test functions added* ≠ *tests
collected* once `parametrize` is involved — which is exactly where round G's term went soft. **Both
endpoints are measured; the decomposition is not, and is not claimed.**

**`ruff check .` and `python -m mypy` (no path arguments) were run per-lane and clean, but were NOT
re-run at `3b98ec8` after the last two doc commits.** Those two commits touch only
`docs/INTEGRATION_HONESTY.md`, which neither tool reads under its manifest scope — so the gap is
argued, not measured. **Re-run both at round I's open** if you want them anchored.

---

## 3. What round H landed

| commit | what |
|---|---|
| `94a2653` | subtask **10c** — the resume continuation ran real phase work with no disk-headroom gate |
| `3a08302` | `CLAUDE.md`: the "fifth check" **promoted** — it had been in the file since `0214840` |
| `fb2ed9c` | a rejected checkpoint was discarded **in silence** (Rule 11) |
| `041ee60` | the citation instrument **contradicted itself** — docstring 38, its own census 46 |
| `319a9f3` | **C1** — the continuation planned the SET of floors, skipping a phase and exiting 0 |
| `f9ff651` | **D62 / ADR-0094** — a cache hit is ALL-hit; any-hit publishes a row the schema forbids |
| `df3af5b` | substitute D62's fix sha |
| `238a0d9` | `CLAUDE.md`: two cited **code fragments** no longer occur in the files they name |
| `e91bcad` | **D81's** read-transaction leg bound — it was prose nothing checked |
| `f087876` | D81 annotated; the heading stays `PARTLY ADDRESSED` **and now says why** |
| `3b98ec8` | substitute D81's marker sha |

**Resume step 5 subtask 10 is now 5 of 10 (10a, 10b, 10c, 10d, 10e).**

---

## 4. The one Critical carried into round I

**A leading `--` comment hides a live second read transaction entirely.** Measured at
`tests/test_read_transaction_statements.py` by round H's final reviewer: injecting a second reader whose
`BEGIN` follows a `--` comment leaves the suite **9 passed** **[ledger]**. Measured **pre-existing in
`_statements`**, not introduced by round H's fixes.

**Ruled to round I rather than folded into a lane at fix round 3 of 5** — it needs its own mutation
battery and its own review. **Re-measure it before acting: it was measured against the round-2 candidate,
and the shipped file is `e91bcad`.**

---

## 5. Open, with owners named or explicitly absent

* **D82 + D84 — READY TO DISPATCH.** `waveclock-d82-d84-build-plan.md`, 8 subtasks. **Three rulings
  already made and carried in it**: build the 4-of-4 form; the ADR lands **with** the schema bump in one
  commit; **D84 lands BEFORE D82 and does not require it.** The plan's own Rule 12 warning is the part to
  respect — **a CROSS-PHASE D84 fixture becomes a semantic no-op once D82 lands**, so fixtures must
  breach in the phase they drive. **Unowned.**
  * Its `begin_wave` site count is **8, not the 4** the round-G handoff carried — 4 declarations **plus 4
    call sites**, no `**kwargs` anywhere, so every miss is a hard `TypeError` **[ledger, round H R1]**.
* **D82-5 is LANDED** (`fb2ed9c`) and was **required** by the ruling above: the version bump silently
  voided every stored checkpoint because `_load_checkpoint` discarded the rejection. **That is now loud.**
* **`RepoOutcome.checkpoint_unreadable` has no downstream consumer, and neither does its sibling
  `checkpoint_rejected`** **[ledger]**. Ruled: **surface both in `WaveReport`, or neither** — it belongs
  to D82's wave, not to a follow-up. **Unowned.**
* **The SCAN skip is NARROWED, not closed.** `_continue_impl` passes the global `only` and
  `_transform_impl` filters via `_wave_repos`, so `{A: SCAN, B: TRANSFORM}` still puts A in the glob for
  spanned phases **[ledger]**. **Pre-existing; round H did not overclaim it.**
* **D62 is `PARTLY ADDRESSED`** — it names five columns and round H closed one. Class re-derived two ways:
  **31 declared / 25 named, unwritable 7 → 6** **[ledger]**.
* **D81 is `PARTLY ADDRESSED`** — its second leg (an external process holding a read transaction) is
  `[UNVERIFIED]` and **now unmeasured by four lanes**.
* **A stale literal class fixed in ONE file only.** `env_prefix="FLEET_"` still stands in **6** other
  files (`docs/DECISIONS.md:6538`, three `docs/superpowers/plans/*`, `.githooks/pre-commit:44`,
  `tools/worktree/new-worktree.sh:23`); `sys.path.insert(0, REPO_ROOT/"src")` in
  `tools/worktree/README.md` **[ledger]**. **A class fixed in one file is not a class fixed.**
* **`mypy>=1.13` is unpinned and ungated** — structurally identical to the ruff hazard ADR-0093 closed.
  Still deliberately deferred.
* **`ruff format --check` still fails**; deliberately NOT gated. Class result, no integer.
* **`docs/DECISIONS.md` citation coverage**: 346 citations, **31 naming a path that resolves to no file**,
  plus **52** unresolved anchors **[ledger]**. Goes red on day one. `CLAUDE.md` by contrast measured
  **0 of 109 broken** in round H — it did not need the instrument; `docs/DECISIONS.md` does.

---

## 6. Proposed `CLAUDE.md` amendments, in priority order

### 6.1 The reason is the unmeasured sentence — take this one first

**Round H measured this three times in three unrelated lanes.** A lane's *conclusion* gets a mutation
pointed at it. The *reason it offers for the conclusion* gets none, and so ships unfalsified:

- *"closing this requires a quote-aware SQL lexer"* — a reviewer wrote one in **18 lines**.
- *"a non-Python line in that fence is a module-wide failure"* — re-measured **20 passed**, module
  imported. It restated the **pre-`e93cbe3`** behaviour, in a document `CLAUDE.md` Rule 11 corrects.
- *"round G measured this twice, independently"* — the first instance is **round F's**, and the lane that
  wrote it **had the falsifying measurement in its own numbers table two paragraphs away**.

**Proposed rule: when you state WHY, mark it as a claim and measure it, or mark it explicitly as
unmeasured rationale. A supporting sentence inside correct work is where this project's false claims
now live.**

### 6.2 Derive the covering test set from what EXECUTES the changed line

**`94a2653` landed five red tests onto `main`** because the lane and its reviewer both ran
`tests/test_cli.py` — chosen because the function lives in `cli.py` — and **neither ran
`tests/test_resume_continue.py`, which is what actually drives `_continue_impl`** **[probe @ fb2ed9c]**.
CR2's review was rigorous: it re-derived the exit code through the funnel, ran the discriminating mutation
live, checked the `finally:` durability path. **It was rigorous over the wrong file.**

`CLAUDE.md` already says *"key recognition on what the code DOES, not on where its text sits"* — about
detectors. **It applies to choosing which tests to run**, and nobody had noticed.

> **FALSIFIED IN PART (2026-08-27), lane W3 — the conclusion holds and the REASON does not.
> `94a2653`'s five reds reproduce exactly; the clause *"which is what actually drives
> `_continue_impl`"* does not. Nothing above is changed, per Guardrail 7 — this is the record of
> what round H's close believed.**
> Re-measured at `94a2653` in a detached worktree, whole files, **no `-k` and no node-ID
> selection**: `tests/test_resume_continue.py` **5 failed / 8 passed**, `tests/test_cli.py`
> **133 passed**. That is the conclusion this section draws, and it reproduces.
>
> * **Both files drive `_continue_impl`.** A pytest-plugin counter wrapping `cli._continue_impl`
>   (`functools.wraps`; `fleet.__file__` pinned to the worktree and asserted before any test
>   result) reads **6 executions in each file** at `94a2653`.
> * **Both files execute the changed line.** `94a2653` adds a single
>   `_require_disk_headroom(settings)` call inside `_continue_impl`. A caller-attributed probe
>   wrapping `cli._require_disk_headroom` and keying each hit on `sys._getframe(1)` reads, at
>   `94a2653`, `_continue_impl:8759` → **4 hits inside the GREEN `tests/test_cli.py` run** against
>   **5** inside the red one. The same run separates those from `scan:991` and from two direct
>   in-test calls, which is the probe's discrimination check.
> * **So the rule as stated in this heading would not have prevented the incident it was written
>   from.** "Run what executes the changed line" selects `tests/test_cli.py` too, and that file
>   executed the changed line four times while passing 133 of 133. Executing the line is the
>   **floor, not a sufficient condition**.
> * **The corrected rule — take EVERY executor, not the nearest — is what landed in `CLAUDE.md`**,
>   as an extension of §6's "State what you ran, including what you excluded" bullet rather than as
>   a free-standing rule.
>
> **This section is itself an instance of §6.1 above.** The unmeasured sentence was the *reason*
> offered for the amendment, attached to a conclusion that reproduces exactly — which is what §6.1
> says happens. Kept on the record rather than tidied away.

### 6.3 Two smaller ones, both measured

- **A green can mean "known cost", not "correct".** Round H shipped a mutation deliberately green **by
  disclosed design** rather than as a control implying correctness. The distinction has no vocabulary in
  `CLAUDE.md` today.
- **`git diff -- <paths>` cannot see a patch's NEW file.** A landing guard aborted on a 9-of-10 staged
  set. **CLAUDE.md §6's five documented faces are all about MODIFIED tracked files**; `git add
  --intent-to-add` before deriving the patch is the fix.

---

## 7. Traps still live

1. **The landing guard fired THREE times in round H, on three different lanes** (a tracked-file edit
   mid-commit, an untracked candidate, and a sibling editing the ledger during another lane's landing).
   Every lane had been told twice not to create the race. **The one-invocation recipe with the staged-set
   assertion is what held each time** — and the *worktree* post-check is what catches the fifth face.
2. **A qualified suite run is not merely a weaker certification — it can be SILENT about the very commit
   that crossed it.** Round H's only qualified run reported 1 failure and missed 5, because the landing
   happened mid-run. **Quiesce, stamp, verify unchanged after.**
3. **An instrument's output is not read merely because it was produced.** A lane's own sweep listed the
   exact SPEC line that became a Critical; the lane read the file list, not the line list.
4. **Two agreeing arms can share one blind spot** — and a control written to defeat that can be *inside*
   it. Round H: a 40-char cap missed a 43-char escape.
5. **A withdrawn number does not discharge the account of where it came from.** One lane withdrew a total
   **and then withdrew its own explanation of the total**, because the explanation reproduced under no
   predicate either.
6. **`.superpowers/` is IGNORED, not merely untracked.** `git clean -fdx` deletes it. It now holds rounds
   D–H, including round H's 20 lane directories.

---

## 8. What round H could not verify

- **The `+24` test delta is not decomposed per commit** (§2), deliberately.
- **`ruff` and `mypy` were not re-run at `3b98ec8`** after the final two doc commits (§2).
- **The `--`-comment Critical (§4) was measured against the round-2 candidate**, not the shipped file.
- **Seven of one lane's mutations were never independently re-run** by its reviewer and remain `[ledger]`.
- **Nothing binds `docs/INTEGRATION_HONESTY.md`'s prose.** Round H bound one *paragraph* of SPEC and
  corrected the citation instrument's self-description; **a false SENTENCE in the ledger is still
  uncatchable.**
