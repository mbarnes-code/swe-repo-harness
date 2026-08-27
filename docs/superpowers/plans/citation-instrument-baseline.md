> **Promoted from untracked scratch at round G close.** Lane W10,
> `.superpowers/sdd/handoff-round-g/lanes/W10/`. Explains the 46-entry pinned baseline in
> `tests/test_integration_honesty_citations.py` — which citations are pinned, why the three causes are
> not separable by rule, and the per-citation sensitivity of the containment check. **Read before
> sweeping the 46.** Body byte-identical apart from this banner.

# Lane W10 — round G — an instrument that resolves `docs/INTEGRATION_HONESTY.md`'s citations

Base: `main` at `3dd500d` (re-derived: `git rev-parse HEAD` → `3dd500d735c42e14373f1c99bc99ee27bbabaad4`).
Worktree: `/tmp/claude-1000/-home-redmage-swe-repo-harness/roundg-W10` (detached at `3dd500d`).
Deliverable: **one new file**, `tests/test_integration_honesty_citations.py` (+587 lines), delivered
as `W10.patch` beside this report. **Not committed.** `git apply --check` OK against the primary at
`3dd500d`. The primary checkout was never written to; the only reads of it were `build_survey()` and
`git status`.

STATUS: **DONE_WITH_CONCERNS** — the instrument is built, green, and validated five ways, but it
publishes a **46-entry pinned baseline of citations that do not resolve today**, and that number is
a finding the orchestrator has to decide about. It is not a whitelist: it fails in both directions.

---

## 1. The three classes, measured

Predicate: a backticked `path:lines` token, matched over the **whitespace-normalised whole file**
with offsets mapped back to line numbers (a line-oriented grep misses the wrapped ones). Path
resolution: unique suffix match against an index of `src/` + `tests/` + `docs/` only.

| class | size at `3dd500d` | treatment |
|---|---|---|
| **bare `path:line`** — a claim about the *current* tree | **414** matches; **409** resolve to a unique file, **5** ambiguous (`query.py`×3, `base.py`×2), **0** unresolvable, **0** out of range | must resolve; 60 of them are checkable (below) |
| **commit-bound** — ``(`:7181` at `53e5d8d`)``, a claim about a *past* tree | **2** occurrences (the citation at ledger `:3981`, and the passage at `:6068` that quotes it) | **exempt by construction** — the form carries no path, so the citation regex cannot match it. Asserted, not assumed, in `test_a_commit_bound_citation_is_never_offered_for_resolution` |
| **looks like a citation but is not checkable** | **343** bare `:N` continuation citations whose file comes from surrounding prose; plus the 5 ambiguous basenames; plus **349** pathed citations carrying no adjacent identifier anchor | out of scope, reported not edited |

Of the 414, **60** carry an anchor — a backticked identifier immediately adjacent, as in
``` `LlmConcurrency.for_tier` (`settings.py:227-230`) ``` — that resolves in the cited file's AST.
Those 60 are the checkable population: **14 resolve, 46 do not.**

A prior lane's warning about naive-predicate false positives is confirmed and *not* eliminated. The
46 have **three different causes** and no rule in this file separates them, because the ledger marks
records in **prose**, not in syntax:
1. **genuine drift** — verified by a second, genuinely different instrument (git history line
   tracking, not the current-tree AST): at `a1178f7` `on_exhausted` stood at line **428** and
   `clone_timeout_s` at **268**, exactly as cited (`settings.py:427-428`, `:267-268`), and both moved
   **+8** by `a9afe9d`. Six `settings.py` citations share that one offset.
2. **usage-site citations** — the ledger itself says `response.usage` (`buildverify.py:1158`) names
   where the value is *consumed*, not where the name is defined.
3. **citations inside passages kept verbatim as a record** — the six `go.py` citations in the table
   cell at ledger `:787`, whose own text says the historical verdicts are "kept verbatim below as the
   record".

**Live finding, worth routing.** Two citations that lane W9 *repointed this round* at `f5a188a` have
**already drifted again** by `3dd500d`: `WaveScheduler.elapsed_s` (`scheduler.py:423-428`, now
425-430) and `WaveReport.exit_code` (`src/fleet/orchestrator/runner.py:311-320`, now 315-324). Both
are in the pinned set. `apply_and_commit` (`vcs/commits.py:232-264`) is off by −31.

---

## 2. The quantity the instrument watches (Guardrail 6 / requirement 4)

> **whether the cited line range is *contained in* the logical span of the identifier named beside
> it, in the cited file.**

Drift *is* motion of that span while the cited range stands still, so a rotted citation takes a
contained range to an uncontained one. It cannot leave the quantity unchanged. Three weaker
quantities were measured and rejected, and each would have been a round-F-style vacuous green:

- **"the file exists"** and **"the file still has that many lines"** are *invariant under drift*.
  All 409 resolvable citations are in range at `3dd500d`, and 46 of them are nonetheless wrong. Both
  checks are still in the module — they catch the **deleted-target** case that round G hit once —
  but they are a different quantity, not a weaker version of this one, and the module says so.
- **"the name occurs as a token inside the cited range"** certified
  `ContainerSandbox.remove()` (`container.py:202-208`) GREEN against a definition at 345-358,
  because `remove` appears there by coincidence. **3 of 22** greens were green only that way.
- **"the cited range *intersects* the span"** tolerates drift up to a whole definition's extent, and
  certified the two already-re-drifted W9 citations above as GREEN. Containment fails both.

`_logical_span` widens the raw AST span by **decorators**, **leading blank/comment lines** and
**trailing comment lines**, each edge stopping at the first line that is neither blank nor a comment.
There is no tolerance constant anywhere (CLAUDE.md Rule 11: a magic bound must derive or fail loudly).
Without the widening, three *correct* citations read as drifted (`docker_run_argv`
`container.py:127-133` cites the blank line before `def` at 128; `TERMINAL_STATUSES`
`models/enums.py:25-28` cites one line into that statement's own trailing comment).

**Sensitivity, measured not asserted.** Of the 14 green citations, the smallest drift that reddens
each is **1 line for six, 2 for five, 3 for two, and 25 for `phase_floor`**
(`src/fleet/orchestrator/reentry.py:96-103`) — the one citation naming the *middle* of a long
definition rather than the definition. That last number is the instrument's blind spot, stated in
the module docstring rather than implied away.

---

## 3. Validation — five checks, zero-change gate read before every result

Harness: `scratchpad/W10/battery.sh` and `battery_e.sh`. Each mutation is gated on
`git diff --numstat --no-index BACKUP MUTATED` (**backup-relative, not HEAD-relative**) and the gate
is printed **before** the pytest result. The gate fired for real once — a mutation string went stale
after `ruff format` changed the pin's quote style, and the battery aborted with `GATE: ZERO CHANGE`
instead of reporting a meaningless pass.

| check | mutation | gate | result |
|---|---|---|---|
| (b) silent on the swept file | none | — | **50 passed** |
| (a) fires on known-bad | unpin `.on_exhausted`/`settings.py:427-428` (drift confirmed against history) | `0 1` | **1 failed / 48 passed**, message names `docs/INTEGRATION_HONESTY.md:2958` and the real span |
| (c) fires on a synthetic fault in a clean citation | round G's own defect: −3 on the green `util/proc.py:359-441` | `1 1` | **1 failed / 49 passed**, names `docs/INTEGRATION_HONESTY.md:791` |
| (d) cosmetic control | reflow the ledger paragraph holding that citation to width 72 (48 lines rewritten, content identical) | `48 1` | **50 passed** — the citation's doc line number moves, the verdict does not |
| (e) fixture vs `parametrize` containment | append an unparseable line to `tests/test_workers_build.py`, cited by exactly 2 pins | `2 0` | **fixture module: 2 failed / 48 passed, module imported, both failures named by anchor and citation.** Identical-logic variant with the locator in the `parametrize` decorator: **collection ERROR, 0 of 60 executed.** Restored: 50 and 60 pass respectively |

(e) is requirement 1 reproduced: the same fault costs **2 of 50** checks in the fixture design and
**60 of 60** in the decorator design. `build_survey` *captures* per-citation `OSError`/`SyntaxError`
into the record instead of raising, which is what makes the containment real rather than incidental.

**First attempt at (e) was confounded and is reported rather than buried:** injecting the syntax
error into `src/fleet/models/state.py` broke `tests/conftest.py`'s `import fleet`, so *both* designs
died at conftest and the comparison measured nothing. A non-`fleet` target was needed.

---

## 4. Environment independence (requirement 5 / D85)

The index is built from `src/` + `tests/` + `docs/` **only** — never the repo root, recursively or
otherwise. An earlier draft also scanned repo-root files and indexed 259 in each tree *by
coincidence* (the primary carries an untracked `migration_state.json`; the worktree carries this
module). **Zero** of the 414 citations resolve to a repo-root file, so the scan was dropped.

Run in both trees from one interpreter invocation:

| tree | indexed | citations | unique | ambig | commit-bound | anchored | unresolved | **unpinned failures** | **stale pins** |
|---|---|---|---|---|---|---|---|---|---|
| worktree (holds this module) | 256 | 414 | 409 | 5 | 2 | 60 | 46 | **0** | **0** |
| primary `3dd500d` (does not) | 255 | 414 | 409 | 5 | 2 | 60 | 46 | **0** | **0** |

The whole index difference is this file itself. The verdict is identical.

---

## 5. The pinned set is a ratchet, not an exemption list

`_PINNED_UNRESOLVED` holds the 46 as `(anchor, citation)` pairs — keyed on the citation's **text**,
not its doc line, so it survives reflow and moves exactly when someone repoints a citation.
- a **new** unresolved citation fails `test_no_unpinned_anchored_citation_fails_to_resolve`, by
  `docs/INTEGRATION_HONESTY.md:<line>`, naming the anchor, the citation and the real span;
- a pinned citation that starts resolving, or whose text changes at all, fails **its own**
  parametrised case telling the author to delete the entry.

CLAUDE.md's warning that "a hand-maintained exemption list is the part that rots" is the reason it
fails in **both** directions: it cannot rot silently. It is still 46 entries of debt and the module
says so in its docstring, with the three causes named. **I did not repoint any of them** — per the
brief, that is the orchestrator's call.

## 6. Scoping disclosed

- `pytest tests/test_integration_honesty_citations.py -q` — **no `-k` filter**, full file: **50
  passed** (4 aggregate + 46 pinned cases).
- Meta-instruments that enumerate `tests/`, run to check the new file does not disarm one:
  `test_instruments_are_armed.py test_findings_kinds.py test_config_keys_are_read.py
  test_blocked_by_writer_statements.py test_manifests.py test_lint_gate.py` → **115 passed**.
- `python -m mypy` with **no path arguments** → `Success: no issues found in 115 source files`.
  **Disclosure:** `pyproject.toml` sets `packages = ["fleet"]`, so that run does **not** cover
  `tests/`. Run explicitly on the file (a scoping, stated): `mypy
  tests/test_integration_honesty_citations.py` → `Success: no issues found in 1 source file`.
- `ruff check` and `ruff format --check` on the file → clean (line-length 100; the generated pin
  block was reformatted to fit rather than `noqa`'d).
- **No whole-tree suite run.** A detached worktree cannot run it green (round F result 7), and the
  primary's session was reserved. This is a module-level certification, not a suite certification.

## 7. Extending to the other docs — cost, not done (Rule 2)

Measured by pointing the same survey at each file; the module is already root- and file-parameterised,
so the code change is one constant becoming a tuple. The cost is the pin and the immediate red:

> **Partly superseded, 2026-08-27 — round I, lane W4, against `924b159`.** "one constant becoming a
> tuple" no longer describes the change. `tests/test_integration_honesty_citations.py` now carries a
> frozen `DocProfile`, and a second profile must additionally **declare a disposition** — `assert`, or
> `not_applicable` with a reason — for each category `_CATEGORIES` names, because a document lacking a
> category would otherwise turn every check over it into a vacuous green. Nothing in the table below is
> retracted. Two of its figures were re-derived at `924b159` and are unchanged (`docs/DECISIONS.md`:
> 346 citations, 66 anchored; `docs/SPEC.md`: 0); the remaining columns were not re-measured.

| file | citations | unique | ambiguous | **path resolves to nothing** | anchored | would-be pins | green |
|---|---|---|---|---|---|---|---|
| `docs/DECISIONS.md` | 346 | 303 | 12 | **31** | 66 | 52 | 14 |
| `docs/PROGRESS.md` | 248 | 229 | 12 | **7** | 34 | 28 | 6 |
| `docs/SPEC.md` | **0** | 0 | 0 | 0 | 0 | 0 | 0 |

`docs/SPEC.md` is **free** — it contains zero citations in this form (it cites by `§`, which this
instrument does not read). `docs/DECISIONS.md` is **not** cheap: 31 citations there name a path that
resolves to no file under `src/`/`tests/`/`docs/`, so extension turns the suite red on day one and
needs a sweep first. That sweep is a separate lane's work.

## 8. What this instrument still cannot catch

- A citation into the **interior** of a long definition (tolerance 25 lines for `phase_floor`).
- Any of the **343** bare `:N` continuation citations.
- The **5** ambiguous basenames (`query.py`, `base.py`).
- **349** pathed citations that carry no anchor at all — the form the brief correctly calls wrong.
  This instrument does not fix that form; it makes the anchored form checkable, so adding an anchor
  now buys enforcement, and it publishes exactly how many citations still lack one.
- A consistent rewrite of a citation *and* its anchor to a matching but false pair.
