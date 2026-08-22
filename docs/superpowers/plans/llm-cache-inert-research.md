> Round-E research artifact, produced by lane **R3**, promoted unchanged from untracked scratch
> `.superpowers/sdd/round-e/lanes/R3/report.md` by lane **W30**. **§7's C1–C7 is the live brief for D79** (`docs/INTEGRATION_HONESTY.md`), which records the defect and routes the fix to round F. Body below is byte-identical to the source; this header prepends 3 lines, so a citation of scratch `report.md:N` is line `N+3` here.

# R3 (research, read-only) — is §11.6's LLM cache reachable from any phase run at all?

**STATUS: DONE.** No commits, no edits (read-only lane). Anchor `1d0c8f6`; **every claim
re-measured at `227e7ba`** (current `main` when this was written) and every one reproduced —
`main` moved four commits under me mid-lane, two of them touching `src/fleet/cli.py`.
Interpreter for every number: `.venv/bin/python` (the binary, not the PATH name).
**`pytest` NOT run — suite lock.** Test callables were driven directly, in a per-lane detached
worktree at `1d0c8f6` with `PYTHONPATH` pinned to that worktree's `src/`. `env | grep -c '^FLEET_'`
= 0 for every run.

---

## 0. Verdict, up front

**W22's premise holds in both halves, and the consequence is worse than it reported: SPEC §11.6's
LLM response cache is not merely unreachable from a phase run — no `LlmCacheStore` implementation
is constructed anywhere in `src/` at all.** The feature is entirely inert in shipped code.

**This is a defect, not a design.** The SPEC does not merely permit the cache, it rests three
success criteria and two §13 failure-mode rows on it, and names it as the *only* determinism
mechanism the harness has. "Inert by design, and the SPEC agrees" was on the table and the SPEC
does not agree.

---

## 1. The exercised observation (not a declaration read)

Script `exercise_cache.py`. Real `FleetSettings.load("config", …, known_backends=discover())`
against the repo's own `config/`, real temp SQLite at the current schema, real `StateWriter` +
`SqliteStateRepository`, a scripted offline backend returning schema-valid output, and a
`RunContext` built with **exactly the kwarg set `cli.py`'s five `RunContext(` sites pass**. The
call goes through `ctx.worker_context(...).llm` — the worker's only call surface — asserted
identical to `ctx.model_client`. Two **identical** `complete()` calls per arm.

**Arm B is a positive control**: the same construction plus
`llm_cache=SqliteLlmCacheStore(writer=…, read_conn=…)`. It exists so that "no cache was consulted"
in Arm A cannot be an instrument that simply cannot see a cache.

| | **A — shipped (cli.py's kwarg set)** | **B — control (`llm_cache` injected)** |
|---|---|---|
| `ctx.llm_cache` | `None` | `SqliteLlmCacheStore` |
| `ctx.llm_cache_mode` | `read-write` | `read-write` |
| `type(ctx.model_client)` | **`LadderModelClient`** | `CachingModelClient` |
| backend invocations after call 1 | 1 | 1 |
| backend invocations after call 2 | **2** | **1** |
| `SELECT COUNT(*) FROM llm_cache` | **0** | **1** |

The behavioural rows are the ones that matter: on the shipped assembly the **same call, issued
twice, is billed twice and writes no row**. The control shows the instrument moves when a cache is
present, so Arm A's zero is an absence, not a blind spot.

## 2. The two halves of the premise, re-measured by AST

**Instrument:** `probe_callsites.py` / `probe_cache_routes.py` / `sweep_fields.py` — `ast.Call`
nodes resolved by dotted name, so a wrapped or multi-line call counts identically to a one-liner
and a `**kwargs` forward is detected explicitly (as its own class), which is what a `grep` for a
keyword argument misses. Run separately over `src/` and `tests/`.

**Half 1 — `llm_cache` / `llm_cache_mode` are never assigned at any `RunContext(` site.**
CONFIRMED. 5 `RunContext(` sites in `src/`, all in `cli.py`, in `_run_scan_wave`,
`_run_transform_wave`, `_run_build_wave`, `_run_verify_wave`, `_emit_prs` (cited by symbol; the
line numbers moved between `1d0c8f6` and `227e7ba`). `llm_cache` at **0 of 5**, `llm_cache_mode` at
**0 of 5**. In `tests/`, 3 sites, also 0 of 3. The single `RunContext(**forward)` site
(`tests/test_run_context_llm_policy.py`, in its `_context` helper) was **read**: the forwarded dict
is built one key at a time and can only ever hold `llm_policy`. Not a hidden route.

**Half 2 — `cli.caching_client` has zero callers in `src/`.** CONFIRMED, by two genuinely
different instruments. AST: **0 `Call` nodes** named `caching_client` in `src/`; 1 in `tests/`
(`tests/test_cli.py::test_llm_cache_flag_reaches_the_caching_client`). Whitespace-normalised
whole-file text sweep for `caching_client\s*\(`: **1 occurrence in `src/`, which is its own
`def`** — the two instruments agree once the definition is subtracted, which is the check that
distinguishes "no callers" from "the probe missed the file".

**Half 3, which W22 did not measure and which is the stronger statement.** Class result:
**0 of the 2 `LlmCacheStore` implementations are ever constructed in `src/`.**
`SqliteLlmCacheStore(` and `MemoryLlmCacheStore(` have **0** call sites in `src/` by AST and **0**
by the normalised text sweep; in `tests/` they have 2 and 13 respectively. `CachingModelClient(`
has 3 call sites in `src/`: its own `for_run` classmethod inside `llm/cache.py`, the
`self.llm_cache is not None` branch of `RunContext.__post_init__` (dead, because the field is
`None` at all 5 sites), and `cli.caching_client` (dead, no callers). **Every route to the cache in
`src/` is unreachable, and there is no store to reach it with.**

> **Editorial correction (2026-08-22), lane W34 — the `for_run` classmethod name above is wrong,
> measured at `81b3b55`.** By AST (`ast.Call` nodes resolved by dotted name) over
> `src/fleet/llm/cache.py`, the `CachingModelClient(` call inside that file is in method
> **`scoped`**, not `for_run` — there is no `for_run` method anywhere in the class. `scoped` is
> exactly what the surrounding sentence already describes (a per-rung view of an
> already-constructed client), so the conclusion is unaffected. This body is a promoted scratch
> report, byte-identical apart from the provenance banner (`f36c9ad`); the wrong name was already
> there while it was still untracked (`.superpowers/sdd/round-e/lanes/R3/report.md`).
> Independently confirms lane W30's `f36c9ad`-era correction of the same site
> (`.superpowers/sdd/round-e/lanes/W30/report.md` §3 item 1).

Consequences that follow directly and were checked: `--llm-cache {read-write,read-only,off}` is
parsed, is echoed by `fleet models list --json` (the `llm_cache` field of its payload), and
installs nothing; `--llm-cache read-only`, §11.6's *replay mode where a miss is a hard error*,
cannot raise `CacheMiss` on any shipped path because nothing constructs the client that raises it;
and `fleet gc --cache-max-age` (§10) executes `DELETE FROM llm_cache WHERE last_hit_at < ?`
against a table no production path has ever written a row to.

## 3. SPEC §11.6, verbatim, and whether the shipped code does it

Heading: `### 11.6 Determinism and LLM drift`. The governing sentences:

> **The LLM tier cannot be made deterministic by sampling parameters, and the spec does not pretend
> otherwise.** […] Determinism therefore comes from **caching, not sampling**:
>
> - `llm/cache.py` keys every call by `cache_key = sha256(role | tier | backend | model_id | effort
>   | context_policy | rejected_approach_digest | prompt_sha256 | response_schema_sha256 |
>   harness_version | adapter_versions)`. […]
> - Cache mode is a CLI flag, `--llm-cache {read-write,read-only,off}` (default `read-write`).
>   `read-only` is the **replay mode**: a miss is a hard error, which is what makes "this re-run
>   used no new model output" a provable claim rather than an assertion. `off` is for deliberately
>   re-rolling a decision.
> - The cache is **not** scoped to `run_id`. Cross-run reuse is the point: re-running the fleet
>   after fixing one adapter re-pays only for the prompts that actually changed.
> - Cache hits set `attempts.llm_cache_hit = 1` and `cost_usd = 0`, so cost accounting stays honest.
>
> […] §12.20 asserts that a full fixture run, re-run with `--llm-cache read-only`, produces a
> byte-identical `run_digest` — and that mutating one fixture file changes it.

**What it requires:** that every LLM call on a run be keyed and served from `llm_cache`; that
`--llm-cache` select between serving, replaying (fatal on miss) and re-rolling; and that a re-run
under `read-only` reproduce a byte-identical digest. §12 item 21 restates it as a criterion
(*"A full fixture run, re-run from a clean database with `--llm-cache read-only`, produces a
byte-identical `fleet status --digest`; … and `--llm-cache read-only` with a cleared cache fails
loudly rather than silently re-rolling"*), §12.36 requires `--llm-cache read-only` to reproduce
every `approach_signature` byte-identically, §12.44 asserts cross-backend cache non-poisoning over
real `llm_cache` rows, and §13 rows 11 and 39 name the cache as the mitigation.

**Does the shipped code do that? No — not in any part.** No production path builds a cache, so no
call is keyed, no row is written, `read-only` cannot fail on a miss, and §12.21/§12.36/§12.44 are
unsatisfiable as written. (Note §11.6 names the criterion "§12.20"; the criterion is **item 21** in
§12's numbered list — a cross-reference off by one, reported not edited.)

**A second, separate SPEC/code contradiction found while quoting it, report-only.** §11.6's
`cache_key` list carries `harness_version` and omits `prompt_template_version`. The code does the
opposite, deliberately: `CacheKeyParts.compute` joins `… | prompt_sha256 | prompt_template_version
| response_schema_sha256 | adapter_versions` and its own docstring says *"`harness_version` is
absent"*, with `llm/cache.py`'s module docstring giving the reason (a patch release would re-spend
the whole corpus). **Class result: 4 statements of the cache key exist in the tree —
`llm/cache.py::CacheKeyParts.compute`, `state/schema.sql`'s `cache_key` column comment,
`models/tasks.py::LlmCallRecord.cache_key`'s `description`, and `docs/SPEC.md` §11.6 — and §11.6
is the only one that disagrees, in exactly two slots.** This is the "SPEC says X but the code
cannot do X" shape: a reconciler making code match §11.6 would re-key the entire corpus. It is one
SPEC sentence and it is **not** part of the wiring fix; see task **C6**.

## 4. Rule 12 on `test_llm_cache_flag_reaches_the_caching_client`

**Yes — there is a mutation under which that test passes while the cache is inert, and it is the
strongest one available.** Measured, not argued.

Driver: `driver_flagtest.py` replicates the `workspace` fixture by hand (`write_config` /
`FleetSettings.load` / `fresh_db` / `seed_run` / `chdir`) and calls the test callable. No pytest.

**Zero-change gate** on every mutation: `git diff --numstat --no-index BACKUP MUTATED` against a
**backup file**, never `HEAD`, aborting on zero changed lines, and read **before** any test result.

| mutation | lines changed (gate) | did it change behaviour? | the test |
|---|---|---|---|
| **M1** — delete the `if self.llm_cache is not None: client = CachingModelClient(…)` branch from `RunContext.__post_init__`, i.e. **§11.6's only production consumption point** | +1/−8 | **Yes, measured**: control Arm B goes `CachingModelClient` → `LadderModelClient`, backend invocations 1 → 2, `llm_cache` rows 1 → 0 | **PASS** |
| **M2** (control) — `caching_client` hardcodes `mode="read-write"` instead of `opts.cache_mode` | +1/−1 | yes | **FAIL** |
| baseline (clean `1d0c8f6`) | — | — | PASS |

M2 is there so the finding is not "the test is vacuous": the test does hold the helper's
flag→`_mode` mapping. M1 is the finding. **The test's name asserts that `--llm-cache` reaches the
caching client; its actual property is that a helper nothing calls maps the flag onto a
constructor argument.** Deleting the entire production cache path leaves it green. This is exactly
the shape CLAUDE.md names — *"a test can pass, and pass under mutation, while the property in its
name is false"* — and it is the second half of the same test that is worse: the first half asserts
`fleet models list --json` echoes `llm_cache: "off"`, which is a **report** of the flag's value
printed by the `models list` verb, not an installation of anything.

**A trap this lane fell into and had to measure its way out of, recorded because it silently
produced a clean-looking wrong answer.** The first M1 run reported "mutation applied, test PASS,
control Arm B still cached" — i.e. an apparently blind test. It was worthless: `.venv` carries an
**editable install of `fleet` pointing at the primary checkout's `src/`**, so a mutation inside a
detached worktree was never imported. `.venv/bin/python -c "import fleet; print(fleet.__file__)"`
resolved to the primary checkout. Every number in this section was re-run with
`PYTHONPATH=<lane worktree>/src`, verified by asserting the M1 marker is present in
`inspect.getsource(RunContext.__post_init__)`. **A detached worktree is not sufficient isolation
for a mutation test in this repo; the interpreter's import path must be pinned too**, and the
zero-change gate does not catch this because the file really did change.

## 5. Class sweep — all 7 `RunContext` fields at 0 of 5 sites

`sweep_fields.py` (AST field list minus `field(init=False)`, against the AST site kwargs) plus
`sweep_routes7.py`, a bucket-3 probe over `src/` **and** `tests/` for five routes a
construction-site probe cannot see: any call keyword of that name anywhere (catches
`dataclasses.replace`), `setattr`/`object.__setattr__` with the name as a string constant,
assignment to an `.<field>` attribute, a `RunContext` subclass, and `RunContext(**forward)`.

**Class result reproduced independently: 7 of 19 `init=True` fields are passed at 0 of 5 `src/`
sites** — identical to W22's number, by my own predicate, at both `1d0c8f6` and `227e7ba`.

**Bucket 3 is EMPTY. No field in the 0/5 set is assigned by any other route in `src/`.** The only
`src/` hits the route probe returned are `GlobalOptions(llm_cache=…)` — a *different* `llm_cache`,
the `LlmCacheMode` CLI flag on an unrelated class — and `WorkerContext(lease_owner=…)` /
`PhaseRow(lease_owner=…)`, which are consumers of `ctx.lease_owner`, not producers of it. Zero
`RunContext` subclasses.

| field | default | verdict |
|---|---|---|
| `lease_owner` | `field(default_factory=lease_owner_id)` | **inert-and-harmless.** The default factory *is* the production value; §6's lease identity is meant to be per-process. Passing it would be the defect. |
| `monotonic` | `_loop_time` | **inert-and-harmless.** The default is the real loop clock; the field exists so a test can drive a four-hour budget in milliseconds. |
| `backends` | `None` | **inert-and-harmless.** `LadderModelClient.__init__` resolves `registry() if backends is None`, and `cli._load_settings` calls `discover()` before any `RunContext` is built. Documented in the field's own docstring; the default is the production path. |
| `llm_policy` | `None` | **fixed in round E by W22** (`1963ca9`): `__post_init__` now derives it via `call_policy_for(self.config.llm)`. Still 0/5 by design — the fix deliberately landed at the consumption point. |
| `projector` | `None` | **inert-and-a-defect, of degree, not of kind.** `Projector(` is constructed at exactly one site in `src/`, inside `state/projection.py` itself, so `ctx.project()` — called from three sites in `orchestrator/runner.py` — is a permanent no-op on every shipped run. **But the output is not lost**: `cli.py` calls `project_once(...)` at 6 sites, so `migration_state.json` is still written, once per command instead of debounced per transition. What is lost is §6's ≤1 Hz live projection during a long wave. Smaller than W22 implied. |
| `llm_cache` | `None` | **inert-and-a-defect — the subject of this report.** See §1–§3. |
| `llm_cache_mode` | `"read-write"` | **inert-and-a-defect, consequentially.** Never assigned, and its default is never consulted because the branch that reads it is dead. It is also never derived from `config.llm.cache_mode`, so the config leaf is inert twice over. |

> **Editorial correction (2026-08-22), lane W34 — two counts in the `projector` row above are
> wrong, measured at `81b3b55`.** **(1)** `Projector(` has **0** construction call sites in `src/`
> by AST, not "exactly one" — the cited "site" inside `state/projection.py` is a **code example
> inside `class Projector`'s own docstring** (a string literal), which a text sweep matches and an
> AST probe does not; an AST probe for `Call` nodes named `Projector` over every `*.py` under
> `src/` returns zero. **(2)** `cli.py` calls `project_once(...)` at **7** sites, not 6 (confirmed
> by both AST and `grep -n "project_once(" src/fleet/cli.py`) — the 7th, inside `fleet resume`'s
> §11.5-step-7 call, was added by `c45db53` (2026-08-19), which predates this report's own anchors
> (`1d0c8f6`/`227e7ba`, both 2026-08-22), so this is a miscount, not rot from a moving `main`.
> Neither correction changes the row's verdict: `ctx.project()` is still a permanent no-op on every
> shipped run, and the projection is still written by `project_once`, just from one more call site
> than stated. This body is a promoted scratch report, byte-identical apart from the provenance
> banner (`f36c9ad`); both errors were already present while it was still untracked scratch
> (`.superpowers/sdd/round-e/lanes/R3/report.md`). The "exactly one site" half of this was already
> flagged, unannotated, in lane W30's report (`.superpowers/sdd/round-e/lanes/W30/report.md` §7
> concern 1); the "6 sites" count is a new finding, not previously flagged. Same claim recurs in
> §7's C7 row below, annotated there too.

## 6. Relation to the register — and does a record here have D58's detector shape?

**Nothing in `docs/INTEGRATION_HONESTY.md` records that the cache is never installed.** Sweeping
the ledger whitespace-normalised across the whole file for `llm_cache|llm-cache|caching_client|
CachingModelClient|cache_mode|11\.6`, the cache-adjacent entries are:

* **D61 — FIXED, LANDED** (`712fd5f`, `a9afe9d`, `6d5a4a8`): the read/write key disagreed on every
  call — *"a permanent, silent, 100% cache miss"*. **This entry describes a defect strictly inside
  `CachingModelClient`, which no shipped run ever constructs.** D61 is a bug in a component that
  cannot execute in production; that context is missing from the ledger and is worth an annotation.
* **D62 — OPEN**: `record_attempt`'s `INSERT` omits `llm_cache_hit` (among four others). D62 already
  says *"the detector for this defect is a column nothing sets"*. **This report adds the layer under
  it: the column would read 0 even if `record_attempt` wrote it, because there is no cache to hit.**
* **D56 — FIXED, LANDED** (`c36160e`): *"`llm.client.discover()` had zero call sites in `src/`"*.
  **This is the same shape as `cli.caching_client`, and it already has a precedent D-number and a
  landed remedy** — the strongest argument that this is a recognised defect class, not a design.
* **D50 — OPEN**: the `KNOWN_INERT` allowlist keys.

**The tree half-knows it, in `tests/test_config_keys_are_read.py`.** `fleet.yaml:llm.cache_mode` and
`fleet.yaml:llm.cache_path` are both in `KNOWN_INERT`, and `llm.cache_mode` is also in
`QUALIFIED_MATCH_KEYS`. Its comment says, correctly, that `context.py`'s `llm_cache_mode` field
*"is likewise never constructed from config"*.

**Does that record have D58's detector-shape problem — a detector that reads a fixed defect as
still open? No; it has the opposite and healthier shape, and one that must nevertheless be handled
in the fix commit.** D58's failure was a `git grep "llm_policy=" -- src/` returning zero both before
and after the fix. `KNOWN_INERT` is not a grep for the defect, it is an allowlist assertion: the
moment `config.llm.cache_mode` is genuinely read, `test_known_inert_keys_are_still_inert` **fails**,
loudly and correctly. Any fix must therefore delete those two allowlist entries in the same commit
— and, following W22's load-bearing choice, **keep `llm.cache_mode` in `QUALIFIED_MATCH_KEYS`**,
because the bare name `cache_mode` collides with `GlobalOptions.cache_mode`, so dropping it would
let a later revert of the wiring pass the scan silently.

**A defect in that record itself, found by checking it: every line citation in its comment has
rotted, and it names a class that no longer exists.** Measured at `1d0c8f6` (identical at
`227e7ba`): it cites `cli.py:413`'s **`Options.cache_mode`** — the class is `GlobalOptions`, and
`cli.py:413` is the comment `# flag value types`; `cli.py:742-743` for the flag (actually
`cli.py:819-821`); `cli.py:558,10529,10532` for the call sites (actually `635`, `12658`, `12661`);
`orchestrator/context.py:139` for the field (actually the `@dataclass` line above `class
RunContext`); and `settings.py:678/679` for the two leaves (actually `686/687`). **6 of 6 line
citations wrong, 1 of 1 class name wrong.** Per CLAUDE.md's cite-by-symbol rule this comment should
be re-anchored on symbols when it is edited for the fix — which task **C4** must do anyway.

---

## 7. Dispatch-ready decomposition

**Recommendation, plainly: this is a real defect and it should be fixed.** The SPEC rests three
success criteria and two failure-mode rows on the cache; a landed precedent (D56) exists for
exactly this shape; and the whole mechanism already exists and is well tested in isolation —
`tests/test_llm_cache.py` drives `CachingModelClient` and both stores hard. **What is missing is
roughly twenty lines of wiring, not a feature.** The cost is in the ratchets it releases, not the
code.

**Design fact the decomposition depends on, measured.** `RunContext` already holds `writer`,
`read_conn` and `config`, so `SqliteLlmCacheStore(writer=self.writer, read_conn=self.read_conn)`
and `config.llm.cache_mode` are both derivable **inside `__post_init__` with no `RunContext(`
call-site change at all** — the precedent `context.py` already sets twice, for `LlmFindingSink` and
(at `1963ca9`) for `call_policy_for`. And `cli._load_settings` already turns global flags into
`cli_overrides` entries by dotted key (`overrides["budgets.max_rss_mb"]`,
`overrides["budgets.run_max_cost_usd"]`), so `--llm-cache` reaches the run as
`overrides["llm.cache_mode"]` through the channel that already exists. **Together these mean the
fix touches no `RunContext(` call site and no phase-wave function.**

**The trap in that route, and it is the project's own recorded one.** `--llm-cache` is declared
`llm_cache: Annotated[LlmCacheMode, typer.Option("--llm-cache", …)] = LlmCacheMode.READ_WRITE` — a
**non-optional default**. An unconditional `overrides["llm.cache_mode"] = opts.llm_cache` therefore
substitutes `read-write` over an operator's `cache_mode: off` whenever the flag is not typed:
character-for-character the `effort: low` failure CLAUDE.md records (*"the field carried a
non-optional default, so the edit substituted a value the operator never wrote"*). The option must
become `LlmCacheMode | None = None` and the override applied only when it is not `None` — and
**C2's success criterion is a resolved-value check in the running interpreter, not a declaration
read.**

| # | Task | Files | Success criterion | Test file |
|---|---|---|---|---|
| **C1** | Derive the store and the mode in `RunContext.__post_init__`, exactly as `1963ca9` derived the policy: `store = SqliteLlmCacheStore(writer=self.writer, read_conn=self.read_conn) if self.llm_cache is None else self.llm_cache`, mode from `self.config.llm.cache_mode` when `llm_cache_mode` is not explicitly given. Correct the `llm_cache` field docstring in the **same** edit — it currently says *"absent ⇒ the client is not wrapped, which is the whole of `--llm-cache off`"*, a **second encoding of `off`** that this change retires in favour of `CachingModelClient`'s own `self._mode == "off"` branch. | `src/fleet/orchestrator/context.py` | A `RunContext` built with **`cli.py`'s exact kwarg set** yields `isinstance(ctx.model_client, CachingModelClient)`; two identical `complete()` calls give **1** backend invocation and **1** `llm_cache` row (the Arm A/Arm B table of §1 inverts). Assert on the **backend call log and the row count**, never on `ctx.llm_cache is not None` — a field comparison is the assertion that passed throughout the entire period the cache was dead. | `tests/test_run_context_llm_cache.py` (new) |
| **C2** | Route `--llm-cache` through the existing override channel: make the Typer option `LlmCacheMode \| None = None`, and in `_load_settings` set `overrides["llm.cache_mode"]` **only when it is not `None`**. Delete `cli.caching_client` in the same commit — C1 subsumes it and it has no `src/` caller. | `src/fleet/cli.py` | **Resolved-value check in the running interpreter, not a declaration read**: with `cache_mode: off` in `fleet.yaml` and **no** flag, `settings.config.llm.cache_mode == "off"` and the built `ctx.model_client._mode == "off"`; with `--llm-cache read-only` on top, `"read-only"`. **The discriminating mutation is the non-optional default** — restore `= LlmCacheMode.READ_WRITE` and the no-flag case must FAIL. | `tests/test_cli.py` |
| **C3** | Retire `test_llm_cache_flag_reaches_the_caching_client`, replacing it with a test whose name is true of a **run**: drive a phase-path call through a `RunContext` and assert the mode reached `CachingModelClient`. | `tests/test_cli.py` | **Old-passes / new-fails on the same input**: under **M1** of §4 (delete `__post_init__`'s cache branch) the old test passes and the new one fails. Report the zero-change gate output **before** the test result, diffed against a **backup**, and pin `PYTHONPATH` to the mutated tree — see §4's recorded trap. | `tests/test_cli.py` |
| **C4** | Release the ratchets: delete `"fleet.yaml:llm.cache_mode"` and `"fleet.yaml:llm.cache_path"` from `KNOWN_INERT`; **keep** `llm.cache_mode` in `QUALIFIED_MATCH_KEYS` (the bare name collides with `GlobalOptions.cache_mode`, so dropping it lets a revert pass silently — W22's measured precedent). Re-anchor the entry's comment **on symbols**: **6 of 6 line citations and 1 of 1 class name in it are wrong** (§6). | `tests/test_config_keys_are_read.py` | `test_every_config_key_is_read` and `test_qualified_match_keys_resolve_to_a_verdict` both pass with `llm.cache_mode` reported *read* by `orchestrator/context.py`; **old-passes/new-fails**: re-run the scan against pre-C1 `context.py` and `llm.cache_mode` must come back unexplained. `cache_path` is a **separate judgement** — C1 does not read it, so if it stays unread it stays in `KNOWN_INERT` with a corrected comment. | itself |
| **C5** | The ledger. Annotate **D61** (its subject is a component no shipped run constructs) and **D62** (`llm_cache_hit` would read 0 even once written, because there is no cache to hit) with dated in-file markers naming this finding's commit; add a **new D-number, allocated centrally by the orchestrator**, for the cache being uninstalled — citing **D56** as the landed precedent for the identical zero-`src`-caller shape. **Annotate, never rewrite**: both entries were true at their own commits. | `docs/INTEGRATION_HONESTY.md` | Both annotations name a commit and a date; the new entry's detector is **behavioural** (the §1 two-call/row-count probe), explicitly **not** a `grep` — D58's detector-shape failure applies here too, since after C1 a `grep "llm_cache=" -- src/` still returns zero. | n/a |
| **C6** | **Separate, SPEC-only, do not fold into C1.** Correct `docs/SPEC.md` §11.6's `cache_key` list to match the three agreeing statements in the tree (drop `harness_version`, add `prompt_template_version`) and carry §11.6's own stated reason for the exclusion. Fix §11.6's "§12.20" cross-reference — the criterion is **item 21**. | `docs/SPEC.md` | Class result re-measured **against the artefact the fix produced**: 4 statements of the cache key in the tree, **4 of 4 agreeing**, 0 dissenting. Anchor the census on text older than the correction and fail by `file:line`. | consider extending `tests/test_llm_cache.py`'s existing `test_the_effort_column_carries_its_adr_0075_annotation_in_both_copies` pattern to the key list |
| **C7** | **`projector` — separate lane, separate call, do not fold in.** Decide whether §6's ≤1 Hz debounced projection is wanted at all, given `cli.py` already calls `project_once` at 6 sites. Either construct a `Projector` in the wave commands and pass it, or **document `ctx.project()` as a stated boundary** and say in `RunContext.projector`'s docstring that production uses `project_once`. | `src/fleet/cli.py` **or** `src/fleet/orchestrator/context.py` (not both) | Whichever arm: `grep`-independent evidence that a wave's transitions do or do not refresh `migration_state.json` mid-wave. **Do not close this with a convention wearing a mechanism's clothes** — an honest disclosure beats a fake debounce. | `tests/test_projection.py` |

> **Editorial correction (2026-08-22), lane W34 — the "6 sites" count in the C7 row above is
> wrong, measured at `81b3b55`.** By AST and by `grep -n "project_once(" src/fleet/cli.py`,
> `project_once(...)` is called at **7** sites in `cli.py`, not 6 — see the identical correction
> after §5's table for the full measurement and the 7th site's location and history. C7's success
> criterion is unaffected: it asks for `grep`-independent evidence that a wave's transitions refresh
> `migration_state.json` mid-wave, not a specific site count. This body is a promoted scratch
> report, byte-identical apart from the provenance banner (`f36c9ad`); the miscount predates
> promotion.

**Ordering.** C1 → C2 → C3/C4 (C4 will fail the suite until C1 lands, so they must be one commit or
strictly sequenced) → C5. C6 and C7 are independent of all of the above and of each other.

**The exact pytest command a later lane must run**, no `-k` filter, after C1–C4:

```
.venv/bin/python -m pytest tests/test_run_context_llm_cache.py tests/test_llm_cache.py \
  tests/test_cli.py tests/test_config_keys_are_read.py tests/test_llm_findings.py \
  tests/test_run_context_llm_policy.py tests/test_runner.py tests/test_settings.py \
  tests/test_workers_build.py
```

`tests/test_workers_build.py` is in the list because its `_CacheMissModelClient` fixture is shaped
like *"a `--llm-cache read-only` cache meeting a miss"* — the one place in the suite that models
`CacheMiss` reaching a worker, and the first thing a live cache will actually exercise.
Plus `python -m mypy` with **no path arguments** (`pyproject.toml` pairs `strict` with
`packages = ["fleet"]`, so `tests/` is out of scope and C3/C4's files are unchecked by it).

---

## 8. What this report cannot catch

* **No suite run** (COMMON.md hard rule 1). Every runtime number is from standalone
  `.venv/bin/python` scripts driving library code and one test callable directly.
* **The `src/`-caller class results are static sweeps.** A cache installed through
  `importlib`/`getattr` with a computed name is invisible to both instruments. §1's exercise is the
  answer to that objection: whatever the static picture, the shipped assembly demonstrably did not
  consult or write a cache.
* **`mypy` was not run** — this lane changed no source. C1's lane must run it with no path args.
* **§3's SPEC reading is mine.** §11.6 and §12 items 21/36/44 are quoted at length precisely so the
  orchestrator can check the reading rather than inherit it.
* **The `projector` verdict in §5 is a static read** of `Projector(` sites and `project_once`
  callers. I did not build a fixture proving `migration_state.json` goes stale mid-wave; C7's lane
  should.
* **The register sweep is over `docs/INTEGRATION_HONESTY.md` only.** A record of this in
  `docs/DECISIONS.md` or `docs/PROGRESS.md` would not have been found — and `DECISIONS.md` grew 334
  lines between `1d0c8f6` and `227e7ba`, under this lane.
