# Task 13 report — D46/D47/§36/ADR-0068 honesty correction

**Status: complete.** Edited only `docs/INTEGRATION_HONESTY.md` (D46, D47), `docs/PROGRESS.md`
(§36), `docs/DECISIONS.md` (ADR-0068). Did not touch `§37`, `ADR-0069`, `D48`, or any `src/`/`tests/`
file. No pytest run, no `FLEET_*` export, no commit.

## Statuses flipped, with evidence

- **D46: OPEN → CLOSED, FIXED in `8464dc6`.** `git log --oneline -S"_invocation_name" --
  src/fleet/workers/buildverify.py` and `git log --oneline -S"D46 — OPEN" --
  docs/INTEGRATION_HONESTY.md` both resolve to `8464dc6` alone — same commit, not sequential.
  `_invocation_name` at `buildverify.py:354-370`, called at `:777`/`:1088`, pinned by
  `tests/test_workers_build.py:931-965`. The "daemon unreachable exits 1, not 125" premise is at
  `buildverify.py:438-442`. `DAEMON_GONE`→`CONTAINER_NAME_CONFLICT` fixture at
  `tests/test_workers_build.py:1105-1129`, rationale at `:1275-1279`.
- **PROGRESS §36 "NOT proven"/"Next task" items 1-2, and the C1 table**: corrected to match —
  `base.py:161-171`, `buildverify.py:438-442` already fixed; `clone.py:700-713` and
  `cli.py:3635-3641` (`started=result.started`) already fixed; `clone.py:731-758` (M4) already
  fixed. Remaining, verified still open: `base.py:180-186`'s classifier-history docstring (still
  frames a manufactured divergence as pre-existing), ADR-0067's four `cli.py`/`rewrite/rules.py`
  parts, D34/D35/D36/D41 staleness re-audit.

## Structural cause (written into both D46 and §36)

Docs and code lanes were authored by different workers in the same round against different tree
states, then committed together unreconciled. A "claimed, not verified" hedge is only honest while
true and lands false the moment it commits beside the fix it doubts. Recorded remedy: a docs lane
must be written against the tree state that will actually be committed and re-verified against the
landed diff immediately before `git commit`, or explicitly flagged pre-commit for first-action
re-verification by the next round.

## I1/I2/I3/I4/I8 — handled in ADR-0068 / D47 / §36

- **I1(c)** — added to ADR-0068: `CacheMiss` fails loud on a **call**, not a **skipped unit**;
  `runner.py:458,474-475` checkpoint short-circuit means a resume can satisfy `--llm-cache
  read-only` vacuously. Also noted: a `_diagnose` `CacheMiss` discards the whole measured
  `WorkerResult`, not just advice.
- **I2** — ADR-0068's "behaviourally identical" claim narrowed: `failure_class`/`retryable`/
  `exception_type` match (3 of 4); `classify.py:238-264`'s `redact_text` pass is dropped after the
  change. `rewrite.py:472` and `cli.py:489/490` added to the enumeration.
- **I3** — D47's `buildverify.py:873-874` citation (prose, not a call site) replaced with
  `:967-969`; "confirmed by direct read" dropped. `response.usage` corrected `:1050`→`:1158`. Test
  citation corrected to `:2357` (as authored) with a note that an unrelated uncommitted edit in
  `tests/test_workers_build.py` currently shifts it to `:2348` — re-derive.
- **I4** — ADR-0068 correction added: `transform_repair`'s ladder is §3.2 (`SPEC.md:783,854-856`),
  not "§9" (`:5878` is Configuration); it never reads a Bazel error or runs a build
  (`grep -ci bazel rewrite.py` → 0). Stronger finding recorded verbatim: **no role in the codebase
  performs the described apply-and-rerun over a Bazel failure.**
- **I8** — recorded in §36 as a provenance note: `8464dc6` shipped `CLAUDE.md` §6, a Guardrail 6,
  and three `.claude/settings.json` `Bash` permissions, named in no commit-message line. Content
  re-verified (`tools/bin/` wrappers, `conftest.py:500`).

## I5 / D48 — routing only, not fixed

D48's "the six verbs" is four. `_phase_preflight` has **seven** call sites (`cli.py:2361, 2390,
2431, 2470, 3061, 8575, 10356`), not six. Three are themselves `_unavailable` immediately after
preflight — `plan` (`:2363`), `migrate` (`:2475`), `stubs resolve` (`:10358`) — the same property
D48 uses to indict `resume`. The four that actually re-enter a run: `build`, `verify`, `transform`,
`pr`. Suggested replacement text: "Seven commands share `_phase_preflight`
(`cli.py:866-878`)... three of the seven (`plan :2363`, `migrate :2475`, `stubs resolve :10358`)
are themselves `_unavailable`; the four that re-enter an interrupted run today are `build`,
`verify`, `transform` and `pr`." The defect D48 reports is unaffected — please hand to whoever owns
D48/§37.
