# DECISIONS.md — Architecture Decision Log

Fleet Engine Migration Harness. Numbered ADR-style log; each entry is a **firm** choice.
Status of every entry below: **Accepted**. Superseding an entry means appending a new
numbered entry that names the one it replaces — entries are never edited in place.

Scope reminder (from `CLAUDE.md`): ONE unified harness, ZERO per-language pipeline tuning.
Per-file syntax transformation is a commodity offloaded to deterministic AST tools + LLMs.
The differentiator is cross-repo dependency interrogation, the global DAG, and topological
sequencing of ~250 repos into a single polyglot monorepo.

---

## ADR-0001 — Python 3.12 as the floor; `uv` for packaging and dependency management

**Decision.** Target `requires-python = ">=3.12"`. Use `uv` as the sole dependency
manager and virtualenv driver, with a PEP 621 `pyproject.toml`, `hatchling` as the build
backend, and a committed `uv.lock`. No Poetry, no `requirements.txt`, no Conda.

**Rationale.** 3.12 is the lowest version that gives us `TaskGroup`/`asyncio.timeout`
(3.11), PEP 695 type params and the substantially faster/clearer error tracebacks (3.12)
without paying the ecosystem-lag tax that 3.13's free-threading builds still carry for
`tree-sitter` and native wheels. `uv` resolves and installs an order of magnitude faster
than pip/Poetry, produces a universal cross-platform lockfile, and — decisive for a harness
that spawns hundreds of throwaway sandboxes — can materialize a locked environment offline
from a warm cache in seconds.

**Alternatives rejected.** Poetry (slow resolver, non-standard lock, weak offline story);
pip + `requirements.txt` (no real locking, no dependency groups); Conda (needless for a
pure-Python harness); Python 3.11 floor (loses 3.12 typing/traceback ergonomics for no gain,
since we control the runtime).

---

## ADR-0002 — Pydantic v2 (`>=2.11`) is the single data-contract and state-modeling layer

**Decision.** Every object that crosses a process, subagent, LLM, or disk boundary is a
`pydantic.BaseModel` (v2, floor pinned at `>=2.11,<3`), plus `pydantic-settings>=2.7` for
configuration. `TypeAdapter.dump_json()` / `validate_json()` is the *only* sanctioned
serialization path for durable state — no `pickle`, anywhere, ever.

**Rationale.** Pydantic v2 is mandated by the project `CLAUDE.md`, and the mandate is
correct for this workload: LLM output is untrusted text, so a schema that both generates
the JSON Schema we hand the model *and* validates the response on the way back collapses
two problems into one declaration, and its Rust core makes validating ~250 repos × N
manifests × M findings free at our scale. The `>=2.11` floor is where `TypeAdapter`
serialization of discriminated unions, `model_config` inheritance, and the
`@model_validator` ordering semantics we rely on are all stable — the reference harness
ships an even more conservative `pydantic>=2.13.4`, and we adopt its validate-on-load rule
verbatim because a tampered or truncated checkpoint must produce a `ValidationError` that
re-runs the step, never a smuggled value.

**Alternatives rejected.** `dataclasses` + manual validation (no JSON Schema generation for
LLM structured output, no coercion); `attrs` (same gap); Pydantic v1 (dead-end API, ~20×
slower validation); raw `dict` state (the failure mode we are explicitly engineering against).

**Amended by ADR-0023** (provider-agnostic LLM layer). The decision is unchanged and is now
*load-bearing in one more place*: because backends differ in how — and whether — they honour a
response schema, **Pydantic validation on our side is the invariant that makes them
interchangeable**. A backend's native JSON-schema mode, its tool-calling coercion, its constrained
decoder, and a prompted-JSON floor are four ways to *raise the odds* of a conforming response;
none of them is trusted. `model_validate_json()` on the declared response model is the single
acceptance test for every backend, and a response that fails it is a failure of that call
regardless of which backend produced it. ADR-0023 also notes that this ADR — not ADR-0009 — is
where the "one declaration generates the schema and validates the response" argument belongs, since
it is a property of Pydantic and of JSON Schema, not of any vendor's API.

---

## ADR-0003 — `asyncio` orchestration; CPU-bound AST work exiled to a process pool

**Decision.** The orchestrator, all I/O fan-out (LLM calls, subprocesses, git, HTTP), and
all queueing is `asyncio`, structured with `asyncio.TaskGroup` and bounded by
`asyncio.Semaphore` per resource class (LLM concurrency, git concurrency, container
concurrency). CPU-bound work — tree-sitter/`ast-grep` parsing, whole-repo symbol indexing,
DAG transitive-closure computation — is dispatched through
`loop.run_in_executor(ProcessPoolExecutor(...))`, and every external tool invocation
(`ast-grep`, `git`, build commands) goes through `asyncio.create_subprocess_exec` — never
`subprocess.run`, never a bare thread. **The boundary rule: if it blocks on a socket or a
pipe it stays on the event loop; if it burns CPU inside this interpreter it goes to a
process.** No `ThreadPoolExecutor` in first-party harness code.

**Rationale.** `asyncio` is mandated by the project `CLAUDE.md`, and the mandate is the
right call because ~95% of harness wall-clock is waiting — on model tokens, on `git clone`,
on container builds — which threads would serve no better while costing us non-deterministic
interleaving in the checkpoint writer. Drawing the CPU boundary at *processes* rather than
threads is what makes the mandate survive contact with tree-sitter: parsing 250 repos is
genuinely GIL-bound, and a single blocking parse on the event loop stalls every in-flight
LLM stream and heartbeat simultaneously.

**Alternatives rejected.** `ThreadPoolExecutor` for LLM fan-out (the reference harness's
choice — see *Conflicts Surfaced*, Conflict 1); Celery/RQ/Dramatiq (a broker to operate for a
single-host batch job); `multiprocessing` as the top-level model (loses cheap I/O fan-out);
Trio/AnyIO (excellent, but the Anthropic SDK's first-class async client is asyncio-native).

---

## ADR-0004 — SQLite (WAL) is the primary state and graph store; `networkx` is a derived, in-memory view

**Decision.** One SQLite database file per fleet run (`state/fleet.db`, `journal_mode=WAL`,
`foreign_keys=ON`, `busy_timeout=5000`), accessed through a thin repository module — no ORM.
It stores: the `repos` inventory (URL, default branch, HEAD SHA, detected ecosystems), the
`manifests` table (every parsed manifest, one row per manifest file), `edges` (the
normalized cross-repo dependency graph: `src_repo`, `dst_coordinate`, `kind`, `version_spec`,
`confidence`, `evidence_path`), `symbols` (the cross-repo symbol index), `phases` (per-repo ×
per-phase status, attempt counter, terminal state), and `checkpoints` (Pydantic
`dump_json()` BLOBs keyed by `(run_id, repo_id, phase)`). The graph algorithms
(cycle detection, SCC condensation, topological sort, blast-radius queries) run in
`networkx`, **built from the `edges` table on demand and never persisted** — it is a
projection, not a source of truth.

**Rationale.** SQLite gives us crash-safe, transactional, single-file, zero-daemon
persistence with real indexes and ad-hoc SQL for the "which repos import X" questions that
dominate Phase 1, and both reference harnesses independently converged on exactly this
choice at exactly this scale. Keeping `networkx` derived rather than stored means the graph
can never drift from the evidence that produced it — a re-scan of one repo rewrites its
edges and the next topological sort is automatically correct, with no migration or graph
GC to get wrong.

**Alternatives rejected.** Neo4j (a JVM daemon, auth, and a second query language for a
graph of ~250 nodes and a few thousand edges that fits in RAM); DuckDB (superb analytics,
but weaker single-writer concurrent-durability story than WAL SQLite and we need durability
far more than OLAP); pure in-memory `networkx` (loses everything on crash — see
*Constraints Inherited From `references/`*, Constraint 2); Postgres (an operational dependency
for a single-host batch tool).

> **Editorial correction (2026-08-08).** This entry originally named the per-manifest table
> `artifacts`. It is `manifests` — `artifacts/` is the run-output directory (SPEC §8) and the
> collision was confusing. `SPEC.md` §6 is normative. No decision changed.

**Amended by ADR-0024 — "primary state store" is scoped to *orchestration* state.** This ADR's
list of what SQLite holds is unchanged and remains correct for every table it names. What ADR-0024
adds is a boundary the original entry did not draw: SQLite is authoritative for **orchestration**
state (tasks, attempts, phases, cost, events) and **Git is authoritative for code state**. The
`mutations` write-ahead journal — added later and never contemplated here — crossed that line by
storing tree SHAs, patch blobs, and a rollback log, and is deleted at `PRAGMA user_version` 5 → 6.
The reasoning is this ADR's own, applied one level up: `networkx` is kept derived "so the graph can
never drift from the evidence that produced it", and for exactly that reason the harness now keeps
no second copy of what Git already stores atomically. Commit SHAs and `phases.base_ref` remain in
SQLite as **pointers**, which is the same relationship `edges` has to the manifests it projects.

---

## ADR-0005 — Manifest interrogation via a `ManifestAdapter` plugin registry with a single normalized `Dependency` contract

**Decision.** One code path, `interrogate(repo_path) -> list[DependencyEdge]`, walks the
repo once and dispatches each candidate file to the first registered `ManifestAdapter`
whose `matches(path)` returns true. `ManifestAdapter` is an ABC with exactly three methods
— `matches(path) -> bool`, `parse(path) -> list[RawDependency]`,
`coordinate(raw) -> Coordinate` — and adapters self-register into a module-level registry
via an `@register` decorator, discovered by importing `src/fleet/manifests/`. Shipped
adapters: Maven (`pom.xml`), Gradle (`build.gradle`, `build.gradle.kts`), npm/pnpm/yarn
(`package.json` + lockfiles), Go (`go.mod`), Cargo (`Cargo.toml`), Python
(`pyproject.toml`, `requirements*.txt`, `setup.cfg`). **Every adapter returns the same
`Coordinate` model** — `(ecosystem, group, name, version_spec)` — and everything downstream
(graph construction, sequencing, ownership resolution, PR generation) sees only
`Coordinate` and never learns which language produced it.

**Rationale.** This is the concrete mechanism by which "ZERO per-language pipeline tuning"
becomes true rather than aspirational: the *only* place language knowledge is permitted to
exist is inside an adapter's three methods, so adding Ruby or PHP later is one new file
and zero edits to the orchestrator, the DAG builder, or the sequencer. Normalizing to a
single `Coordinate` at the parse boundary is what lets a Java `groupId:artifactId` and an npm
`@scope/name` be joined against the same internal-repo lookup table with one query instead
of six.

**Alternatives rejected.** A per-language `if/elif` ladder in the scanner (the exact
branching the mandate forbids); one subagent per language (per-language pipeline tuning by
another name, and unaffordable in tokens); a single universal grammar/regex (fails on
Gradle's Groovy/Kotlin DSLs immediately); shelling out to each ecosystem's native resolver
(`mvn dependency:tree`, `npm ls`) as the primary path — kept only as an opt-in enrichment
step for repos where static parsing yields low-confidence edges, because it requires
installing six toolchains and network access.

**Amended by ADR-0020 — the scope of "one new file" is corrected.** This ADR's rationale claims
that adding a language is "one new file and zero edits to the orchestrator, the DAG builder, or
the sequencer". The clause after the comma is and remains true; the phrase "one new file" was
not, because `ManifestAdapter` deliberately covers only the *input* side — parsing a manifest into
`Coordinate`s. The *output* side (monorepo destination directory, `MODULE.bazel` external-dep
dialect, `BUILD.bazel` target emission) was never in this ABC's scope and had silently accumulated
in `bazel/generators.py`. ADR-0020 gives it a symmetric home, `EcosystemAdapter` in
`src/fleet/ecosystems/`, using **this ADR's registry mechanism unchanged**. The honest count is
now stated in SPEC §1 and bounded by §12.34: one `manifests/` file, one `ecosystems/` file, one
`Ecosystem` enum member, optional `config/rules/*.yml`. Nothing about `ManifestAdapter`'s three
methods, its `@register` decorator, or the `Coordinate` contract changes.

---

## ADR-0006 — `ast-grep` is the primary structural search-and-rewrite engine

**Decision.** `ast-grep` (via the `ast-grep-py` binding, with the CLI as the subprocess
fallback) is the default engine for all cross-repo structural search and for the
deterministic tier of rewrites: import-path rewriting, package/module renames, build-file
target updates, and codemod pattern matching. Secondary engines are permitted **only** in
these named slots: **`tree-sitter`** (via `tree-sitter-language-pack`) wherever we need the
parse tree itself rather than a pattern match — the cross-repo symbol index, call/import
extraction, and chunking source for LLM context; **`libcst`** for Python-only rewrites that
must preserve comments and formatting byte-for-byte; **`ts-morph`** for TypeScript rewrites
that require the *type checker* (interface-aware refactors, `tsconfig` path remapping) — run
as a Node subprocess, never linked in. `comby` is not used.

**Rationale.** `ast-grep` is the only tool in the set that offers one YAML rule syntax and
one CLI across all six of our languages, which makes it the only candidate that satisfies
the zero-per-language-tuning mandate for the rewrite tier. The secondaries are each admitted
for a capability `ast-grep` genuinely lacks — a persistent typed AST, lossless Python
concrete syntax, and TypeScript type resolution respectively — and each is fenced to that
capability so it cannot metastasize into a parallel per-language pipeline.

**Alternatives rejected.** `comby` (language-agnostic but lexical rather than syntactic;
weaker structural guarantees and a smaller rule ecosystem); `tree-sitter` as *primary*
(a parser library, not a rewrite tool — we'd be hand-rolling `ast-grep`); `libcst`/`ts-morph`
as primary (single-language by construction — instant mandate violation); OpenRewrite
(genuinely excellent for Java/Maven, but JVM-only and would force a Java-shaped pipeline).

---

## ADR-0007 — Bazel with `bzlmod` is the target monorepo build system

**Decision.** The emitted monorepo is a Bazel workspace using `bzlmod` (`MODULE.bazel`, no
`WORKSPACE`), with `rules_java`/`rules_jvm_external`, `rules_js`/`rules_ts` (aspect),
`rules_python` + `pip_parse`, `rules_go` + Gazelle, and `rules_rust` + `crate_universe`.
`BUILD.bazel` files are generated, never hand-written, by per-ecosystem generators
(Gazelle for Go, and our own generators driven by the ADR-0005 `Coordinate` data for the
rest). Remote caching is assumed; remote execution is not required.

**Rationale.** Bazel is the only build system in the candidate set that is genuinely
polyglot *and* content-addressed — it can express a Java service depending on a Rust
library depending on a generated protobuf as first-class typed targets, and its
hermetic action graph is what makes "verify only the transitive reverse-dependencies of
this migrated repo" a `bazel query` rather than a heuristic. That reverse-dependency query
is the exact primitive Phase 4 needs, so choosing anything else would mean re-implementing
Bazel's dependency graph inside our harness.

**Alternatives rejected.** Nx and Turborepo (JS/TS-first; Java, Go, and Rust are second-class
"run this shell command" escape hatches — disqualifying for a fleet that is majority JVM);
Moon (promising and far friendlier, but task-runner semantics rather than a true artifact
graph, and a much thinner ecosystem for JVM/Rust); Pants v2 (the closest rival — genuinely
polyglot with excellent inference — rejected on ecosystem depth, remote-cache maturity, and
the far larger pool of prior art the LLM tier can draw on for Bazel).

**Amended by ADR-0020 — "per-ecosystem generators" is given a dispatch mechanism.** This ADR named
the rulesets and said `BUILD.bazel` files are generated "by per-ecosystem generators (Gazelle for
Go, and our own generators driven by the ADR-0005 `Coordinate` data for the rest)" without saying
how one is selected, which in practice meant an `if ecosystem ==` ladder inside
`bazel/generators.py`. ADR-0020 replaces that module with `bazel/emit.py`, a branch-free driver
over the `EcosystemAdapter` registry. The **ruleset choices in this ADR are unchanged** — they
move from prose here into the `ruleset` class variable of the six shipped adapters and the
`build.ruleset_versions` pins in `config/fleet.yaml`, i.e. from a decision record into executable
data. `bzlmod`-over-`WORKSPACE`, generated-never-hand-written BUILD files, remote caching
assumed / remote execution not required, and the `bazel query rdeps` rationale all stand.

---

## ADR-0008 — The LLM invocation boundary: models judge, code executes

**Decision.** A model may be invoked **only** for these five classes:
(1) **classification/labeling** — service vs. library, framework family, ownership guess,
is-this-edge-internal disambiguation; (2) **ambiguous extraction** — pulling structure out of
prose or non-declarative config (`README`s, Groovy `build.gradle` logic, hand-rolled shell
build scripts) that no adapter can parse; (3) **semantic rewrite** — changes requiring
intent, i.e. resolving a genuine API incompatibility, reconciling two divergent copies of a
shared utility, authoring a non-trivial `BUILD.bazel` target; (4) **build-failure diagnosis**
— reading a compiler/Bazel error and proposing the next edit; (5) **prose generation** — PR
titles, bodies, migration notes, `docs/PROGRESS.md` entries.
Everything else is deterministic code, and specifically these are **forbidden** to the model:
file moves and copies, git operations, dependency-graph construction, cycle detection,
topological ordering, retry/backoff decisions, state transitions in `migration_state.json`,
mechanical import-path rewriting (that is ADR-0006's job), version arithmetic, and any
verification verdict. **A model may propose a patch; only code applies, builds, and judges
it** — the model never grades its own homework.

**Rationale.** This is `CLAUDE.md` Rule 5 made enforceable at the call site, and the split
follows a single test — if the same inputs must always produce the same output, it is code —
which keeps the expensive, nondeterministic, unauditable tier confined to the ~5% of the
work that actually needs judgment. Forbidding the model from rendering verdicts is the
specific defense against the failure mode both reference harnesses report most loudly: an
agent that edits the world until its own claim becomes true and then reports success.

**Alternatives rejected.** "Agent with a shell, let it figure it out" (unbounded cost,
unreproducible, and the documented path to fabricated success); fully deterministic, zero
LLM (cannot resolve genuine semantic conflicts across 250 heterogeneous repos); per-file
LLM review of every file (cost scales with LOC instead of with ambiguity).

---

## ADR-0009 — Anthropic Claude via the official `anthropic` Python SDK, with a three-tier role→model map

**Decision.** Provider: Anthropic. Client: the official `anthropic` package
(`anthropic>=0.69`), `AsyncAnthropic` only, one module-level lazily-constructed singleton,
SDK-native retries left on (`max_retries=4`) and never re-implemented. Adaptive thinking
(`thinking={"type": "adaptive"}`) with an explicit `output_config.effort` per role;
streaming for any call with large `max_tokens`. **No other provider, no OpenAI-compatible
shim, no LangChain.**

> **NOT IMPLEMENTED, and deliberately so — do not reconcile code to the *paragraph immediately
> above*.** The marker covers **every claim in that paragraph, without exception** — the thinking
> mode, the streaming, the "`output_config.effort` per role", and "no OpenAI-compatible shim".
> Not one of those four describes what ships, and none of them is outside this marker's scope.
> **Historical means those four claims, not the whole paragraph** — the rest of it is true as
> shipped and must be kept: `anthropic>=0.69` (`pyproject.toml:33`), `AsyncAnthropic`
> (`llm/backends/anthropic.py:215`), and SDK-native retries at `max_retries=4` (`:100`, applied at
> `:218`). Do not delete those in the name of this marker.
>
> The **tier table below is a different matter: authoritative and current.** Its `effort: high`
> values are live config (`config/models.yaml`), must not be deleted, and deleting them would
> re-key the CHEAP and non-CHEAP caches exactly as ADR-0075 warns.
>
> As shipped: `llm/backends/anthropic.py` constructs **no `thinking` key at all** (nor any
> `temperature`, `seed` or `top_p` on the LLM call path). **Effort is per TARGET and conditional,
> not per role and unconditional** — it is sent **only** when the target declares one, gating on
> `target.effort is not None` and consulting no capabilities; `ModelCapabilities` has no effort
> field and must not grow one (ADR-0075; a `supports_effort` gate would suppress an explicitly
> declared `effort: high` and re-key the cache). **An OpenAI-compatible backend does ship** —
> `llm/backends/openai_compatible.py`, ADR-0023 — so "no OpenAI-compatible shim" is false as
> written. On streaming, be precise: the *protocol* exists and is implemented
> (`ModelClient.stream` at `llm/client.py:274`, `LadderModelClient.stream` at `:544`,
> `CachingModelClient.stream` at `llm/cache.py:504`, and `ModelCapabilities.supports_streaming` at
> `models/tasks.py:66`); what is false is that the anthropic backend streams — it does not — and
> **no phase consumes `ModelClient.stream`**. Determinism comes from the `llm_cache`, not from a
> sampler setting. This paragraph is retained as the original decision record; §7.7's
> shipped-backends table describes actual behaviour.

Role→model assignment:

| Tier | Model ID | Harness roles | Effort |
|---|---|---|---|
| **Heavy / semantic** | `claude-opus-5` | Cross-repo conflict resolution; genuine API-incompatibility rewrites; `BUILD.bazel` authoring for non-trivial targets; migration-plan authorship for a repo marked `REQUIRES_HUMAN_INTERVENTION`; cycle-breaking proposals | `high` |
| **Workhorse / bulk** | `claude-sonnet-5` | Per-file transformation review and repair; build-failure diagnosis loop; ambiguous manifest/README extraction; PR body and migration-note generation | `high` |
| **Cheap / volume** | `claude-haiku-4-5-20251001` | Repo classification (service/library/monolith); framework and ecosystem detection; internal-vs-external dependency labeling; commit-message and PR-title drafting; log-line triage | ~~`low`~~ **unset — superseded by ADR-0075** |

All roles are declared in one `config/models.yaml` mapping `role -> {id, effort?}` (`effort` optional per ADR-0075 — the shape, not just the word, is what a reconciliation reads as required), so
re-tiering a role is a config edit, never a code edit.

**Rationale.** Concentrating on one provider with one SDK removes an entire abstraction
layer (and its bug surface) from a harness whose real complexity lives in the dependency
graph, and Claude's structured-output + tool-use path lets ADR-0002's Pydantic models serve
double duty as the request schema and the response validator. The three-tier split is
driven purely by cost-per-judgment: classification runs ~250 × N times and must be cheap,
while semantic conflict resolution runs a few dozen times and must be right, so paying
Opus rates for the former or Haiku rates for the latter would be the same mistake in
opposite directions.

**Alternatives rejected.** LangChain/LiteLLM/DeepAgents abstraction layers (the reference
harness carries all three and pays for it in indirection we do not need); a single model
for every role (either overpays 100× on classification or underperforms on semantic
rewrite); local/self-hosted models (insufficient for tier-1 semantic work); any
non-Anthropic provider (out of scope by directive).

**Superseded by ADR-0023** (provider-agnostic `ModelClient` + backend registry). The original text
above is preserved as the record of what was decided and why it was wrong. It is preserved, not
untouched: **three** inline annotations were added later — the NOT-IMPLEMENTED blockquote
covering every claim in the Decision paragraph, the struck CHEAP `effort` cell, and the
`role -> {id, effort?}` shape qualification below the table, the last two both per ADR-0075 —
because a record that reads as current is a record that gets reconciled INTO the code. **Seven**
of its claims do not survive. Three were identified when ADR-0023 superseded it: the "out of scope
by directive" rejection cited a directive that **does not exist**
in `CLAUDE.md` or anywhere in `references/`; the claim that the reference harness "carries all
three" of LangChain/LiteLLM/DeepAgents is **factually false** for LiteLLM, which appears nowhere in
`references/`; and the structured-output rationale describes a capability that is not
provider-specific. Four more were found later and are marked inline above. (4) **Adaptive
thinking** — no `thinking` key is constructed anywhere in `src/`. (5) **Streaming for large
`max_tokens`** — stated carefully, because the loose version of this is itself false: the
`ModelClient.stream` protocol *is* implemented (`llm/client.py:274`/`:544`, `llm/cache.py:504`,
`ModelCapabilities.supports_streaming`), but the anthropic backend does not stream and **no phase
consumes it**. (6) **"No other provider, no OpenAI-compatible shim"** — `openai_compatible` ships
as a backend (ADR-0023), so this is false as written. (7) The **`effort` claims, both of them** —
the unconditional "`output_config.effort` per role" phrasing and the CHEAP tier's `effort: low` —
superseded by ADR-0075, under which effort is per target, optional, and omitted entirely when
undeclared. What survives verbatim: the **three-tier cost split**,
the rule that role→model assignment is config and never code, and the rejection of heavyweight
orchestration frameworks.
What changes: `anthropic` becomes one backend among several behind a `ModelClient` protocol, the
tiers are named `HEAVY`/`WORKHORSE`/`CHEAP` and carry no vendor string, and the model IDs above
become the *default profile* in `config/models.yaml` rather than a structural commitment.

---

## ADR-0010 — Verification runs in a Docker container over a per-repo `git worktree`

**Decision.** Two composed isolation layers, not one. **Filesystem isolation** is a
`git worktree` per in-flight repo, cut from the monorepo's integration branch — cheap,
instant, no re-clone, and it gives every parallel worker its own consistent checkout of a
shared object store. **Execution isolation** is a Docker container per verification attempt,
`--network=none` by default (build inputs come from a pre-populated, mounted toolchain and
dependency cache), `--user $(id -u):$(id -g)` so no artifact is ever written back root-owned,
memory- and CPU-capped, with a hard wall-clock timeout, and torn down after every attempt.
The worktree is bind-mounted read-write; the cache is mounted read-only.

**Rationale.** Neither layer is sufficient alone — a worktree gives no protection from a
malicious or merely careless build script and cannot pin toolchain versions across six
ecosystems, while a container without worktrees forces a full clone per attempt and
serializes on a shared checkout. Composing them makes verification both *parallel* and
*hermetic*, which is precisely what topological sequencing needs: many independent repos
verifying at once, each with a reproducible answer.

**Alternatives rejected.** Python `venv` alone (Python-only — a non-starter for Java/Go/Rust);
container-only (loses cheap parallel checkouts, re-clones constantly); worktree-only
(no toolchain pinning, no blast-radius containment, no resource caps); a full VM per attempt
(minutes of startup per verification × 250 repos × 3 retries); running builds directly on
the host (violates the workspace-containment rule in `CLAUDE.md` §2).

---

## ADR-0011 — Ingest with `git subtree` semantics implemented via `git-filter-repo`; emit stacked PRs

**Decision.** **Ingest:** each source repo is cloned bare, rewritten with `git-filter-repo`
to relocate its entire tree under its final monorepo path (`--path-rename ':<dest>/'`) and to
scrub secrets and >10 MB blobs, then merged into the monorepo with
`git merge --allow-unrelated-histories` (the `git subtree add` shape, executed via
`filter-repo` so the path rewrite is applied to *history*, not just the tip). **History is
preserved, rewritten, and attributed** — full commit history is retained for every repo,
with paths rewritten so `git log --follow` works across the merge boundary, and each merge
commit records the source repo URL and origin SHA in its trailer.
**Emit:** one PR per repo, opened against the monorepo integration branch, **stacked in
topological order** — a repo's PR is only opened once every PR it depends on is merged, so
the DAG order from Phase 1 is the PR order. PR creation is `gh` CLI via
`asyncio.create_subprocess_exec`.

**Rationale.** Losing history across 250 repos would destroy `git blame`, bisect, and every
ownership signal the organization has, and those signals are exactly what reviewers of a
migration PR need most — so history preservation is a requirement, not a nicety.
`git-filter-repo` is the only tool that rewrites paths across full history fast enough for
this scale (it is also what upstream Git now recommends over `filter-branch`), and stacking
PRs in DAG order means every PR's CI runs against a monorepo where its dependencies already
exist, which is the difference between a green build and 250 simultaneously-red ones.

**Alternatives rejected.** Plain file copy (discards all history — unacceptable);
`git subtree add` alone (prefixes new commits but leaves historical paths un-rewritten, so
`--follow` breaks); `git submodule` (does not consolidate — it is the opposite of the goal);
`filter-branch` (orders of magnitude slower, officially discouraged); one mega-PR
(unreviewable, and a single failure blocks all 250).

**Amended by ADR-0019 — ingesting a hoisted contract node.** The per-repo ingest above is
unchanged. A *contract* node has no repo of its own, so its history is taken from its **owning
repo's** mirror by a second, path-filtered `git-filter-repo` pass over a throwaway clone —
`--path <contract source> … --path-rename '<prefix>:<hoist_target_path>/'` — keeping only the
commits that touched the contract's sources, then merged with the same
`--allow-unrelated-histories`. The merge commit carries the owner's `Source-Repo:` and
`Source-Sha:` plus a third trailer `Hoisted-Contract: <contract_id>`, so the provenance of a file
that no longer lives in the repo that wrote it is recorded in git and not only in SQLite; history
preservation and `git log --follow` therefore hold for the hoisted subtree exactly as for a whole
repo. To keep those commits from appearing twice, the owning repo's own (later) relocation plan
**excludes** every path already claimed by a `HOISTED` contract, resolved as the `FILE_PATH`
collision `resolution = 'hoisted:<contract_id>'`; every vendorer's copy is dropped by the same
rule. PR emission is unchanged, except that a contract node's PR is stacked ahead of its owner's
and its consumers', which is simply the DAG order the amendment produces.

---

## ADR-0012 — `structlog` JSONL event log + `migration_state.json` as the durable checkpoint

**Decision.** Three durable artifacts, with clearly separated jobs.
(1) **`migration_state.json`** — mandated by `CLAUDE.md`; the human- and
resume-readable snapshot of the whole fleet: one entry per repo with `phase`, `status`,
`attempts`, `last_error`, `blocked_by`, `depends_on`, `pr_url`, `updated_at`. It is
written **atomically** (temp file + `os.replace`) after every state transition, serialized
from a Pydantic `MigrationState` model, and it is a *projection of the SQLite tables from
ADR-0004*, not an independent source of truth — SQLite is authoritative on conflict.
(2) **`logs/events-<run_id>.jsonl`** — an append-only JSONL event stream via `structlog`
with a JSON renderer, one object per line, every line carrying `ts`, `run_id`, `repo`,
`phase`, `event`, `level`, plus event-specific fields; token usage, cost, and latency are
logged as `llm_call` events so per-repo spend is a `jq` away. Console output is
`structlog`'s human renderer over the same events — **one event pipeline, two renderers**.
(3) **`logs/errors-<run_id>.jsonl`** — recoverable errors only, written *only if one occurs*,
so its existence is itself the signal that a run was not clean.

**Rationale.** A single mutable JSON blob cannot answer "what happened and in what order",
and an append-only event log cannot answer "where do I resume" in O(1) — so we keep both and
give each the job it is actually good at, with SQLite underneath as the transactional
arbiter. Making `migration_state.json` a derived projection is what prevents the classic
harness bug where the resume file and the real state silently diverge after a crash mid-write.

**Alternatives rejected.** Plain `logging` with format strings (unqueryable at 250-repo
scale); OpenTelemetry + a collector (a whole observability stack for a batch job — revisit
only if this becomes a service); `migration_state.json` as the sole store (no ordering, no
concurrent-write safety, no ad-hoc queries); pickled state (CWE-502 — see
*Constraints Inherited From `references/`*, Constraint 2).

> **Editorial correction (2026-08-08).** This entry originally named the projection model
> `FleetState`. The model is **`MigrationState`** (`src/fleet/models/state.py`), matching the
> `migration_state.json` filename that `CLAUDE.md` Rule 6/11 mandates. There is **no
> `FleetState` alias** — one name, one class. `SPEC.md` §5.5 is normative. No decision changed.

**Amended by ADR-0024 — the checkpoint's arbiter for *code* state is Git.** All three artifacts
keep their jobs, and "SQLite is authoritative on conflict" still holds for every orchestration
fact. But this entry's own rationale — that making `migration_state.json` a derived projection is
"what prevents the classic harness bug where the resume file and the real state silently diverge
after a crash mid-write" — applies one layer down to the `mutations` journal, which was a second
durable record of changes Git had already committed. ADR-0024 deletes it and states the invariant
plainly: **on any disagreement about whether a change landed, Git is authoritative and the SQLite
row is corrected, never the reverse.** `migration_state.json` remains a projection and is still
never an input; the only change here is that one of the things SQLite projects — "which commit did
this task produce" — is now a pointer that resume re-derives from the `Fleet-Task-Id` commit
trailer rather than a fact SQLite owns.

---

## ADR-0013 — `pytest` with a layered suite; "verified" is defined per phase and is machine-checkable

**Decision.** `pytest` + `pytest-asyncio` (`asyncio_mode=auto`) + `pytest-cov`, with
`hypothesis` for the manifest adapters and the graph algorithms. Three layers:
**unit** (pure functions — adapters, `Coordinate` normalization, DAG algorithms, retry
policy — fully offline, no network, no Docker, must run in <30s total);
**integration** (real SQLite, real `git` against fixture repos committed under
`tests/fixtures/`, real `ast-grep` on real files; LLM calls replaced by a recorded-response
fixture); **contract** (every Pydantic model round-trips `dump_json` → `validate_json`, and
every LLM prompt's declared response schema validates against a stored golden sample).
Tests assert *intent*, per `CLAUDE.md` Rule 9 — e.g. "a failing build increments
`attempts` in `migration_state.json`", "a dependency cycle produces a
`CycleDetected` finding rather than a hang", "a repo is never sequenced before its
dependencies". **CI gate:** `pytest` green + `ruff check` + `ruff format --check` +
`mypy --strict` on `src/fleet/` + ≥85% line coverage on `src/fleet/` — all five, or the
change does not land.

**"Verified" is phase-specific and never a model's opinion** (ADR-0008):
- **Phase 1** — every repo has ≥1 parsed manifest or an explicit `no-manifest` finding; the
  **ordering** edge set induces a DAG (SPEC §3.1 step 6 breaks every non-trivial SCC by edge
  suppression, atomic-wave grouping, or an explicit `MANUAL` refusal — detection alone is not
  sufficient), each recorded as a `CycleDetected` finding carrying its `break_strategy`; the
  topological order covers 100% of nodes.
- **Phase 2** — the transformed tree parses (`ast-grep` exits 0 on a parse probe for every
  touched file) and `git diff` is non-empty and confined to the repo's own subtree.
- **Phase 3** — `bazel build //<repo>/...` and `bazel test //<repo>/...` both exit 0 inside
  the ADR-0010 sandbox.
- **Phase 4** — Phase 3 green *plus* `bazel test` green for the transitive reverse-dependency
  closure (`bazel query "rdeps(//..., //<repo>/...)"`), and a PR exists with a resolvable URL.

**Rationale.** Defining "verified" as an exit code per phase — never as prose and never as a
model's self-assessment — is what makes Rule 4's autonomous loop terminate on evidence
instead of on vibes. Layering the suite so the unit tier is offline and sub-30s means the
loop the harness runs hundreds of times stays fast, while the expensive Docker/Bazel tiers
run only where they earn their cost.

**Alternatives rejected.** `unittest` (no fixtures, no parametrize, no plugin ecosystem);
live LLM calls in CI (nondeterministic, slow, expensive); coverage as the sole gate (rewards
test volume over intent); "the model says it looks right" (explicitly forbidden by ADR-0008).

---

## ADR-0014 — Retry policy: 3 attempts with distinct strategies, then `REQUIRES_HUMAN_INTERVENTION`

**Decision.** Per `(repo, phase)`, at most **3** attempts. The attempts are deliberately
**not** identical — retrying the same thing is how the reference material's "infinite loop"
failure mode begins:
- **Attempt 1** — deterministic path only (adapters + `ast-grep` rules + generators).
- **Attempt 2** — deterministic result plus the captured build/parse error handed to the
  `claude-sonnet-5` repair role; the model proposes a patch, code applies and re-verifies it.
- **Attempt 3** — escalate to `claude-opus-5` with the full failure history from both prior
  attempts plus the repo's dependency context.

After attempt 3 fails, the harness writes `status: "REQUIRES_HUMAN_INTERVENTION"` for that
`(repo, phase)` and **moves to the next item** — it never blocks the fleet. **That state
lives in three places, all written in one transaction:** the authoritative row in the SQLite
`phases` table (`status`, `attempts`, `last_error`, `failure_class`); the projected entry in
`migration_state.json` under `repos.<name>.status` (the mandated location per `CLAUDE.md`
Rule 11); and a terminal `repo_abandoned` event in the JSONL log carrying all three attempt
transcripts. Any repo that depends on an abandoned repo is marked `blocked_by: [<repo>]` and
skipped rather than retried, so one human-intervention repo costs us its subtree and nothing
more.

Orthogonal to this: **transient** infrastructure failures (HTTP 429/5xx, network resets,
Docker daemon hiccups) are *not* attempts — they are retried with exponential backoff +
jitter inside the call, by the SDK where possible, and are capped separately. **Errors are
classified by inspecting the response payload, not merely by exception type**, and a failure
is never silently swallowed (`CLAUDE.md` Rule 11 — Fail Loud).

**Rationale.** Three *escalating* attempts is the mandated budget spent the only way that
gains information — identical retries just multiply the same failure and burn tokens, which
is precisely the "infinite loop" and "silent failure" pair the reference material names as
the top harness failure modes. Recording the terminal state in the transactional store
first and projecting it outward guarantees a resumed run never re-attempts an abandoned
repo, which is what keeps a 250-repo run's cost bounded.

**Alternatives rejected.** Unlimited retries (unbounded cost, no termination proof); a
single attempt (throws away trivially recoverable build errors); halting the whole fleet on
first failure (one bad repo blocks 249 good ones); storing the terminal state only in
`migration_state.json` (non-transactional — a crash between the write and the log leaves the
run un-resumable).

**Amended by ADR-0021** (anti-anchoring context policy). The count (3), the escalating-strategy
principle, the transient/substantive split, and the `REQUIRES_HUMAN_INTERVENTION` terminal
contract all stand unchanged. What changes is the *context composition* of the two LLM rungs:
attempt 2 now runs on a **fresh slate** (`EVIDENCE_ONLY`) rather than inheriting attempt 1's
output beyond its failure evidence, and attempt 3 receives evidence plus structured
*rejected-approach summaries* instead of "the full failure history from both prior attempts" —
the raw prior diffs and prior rationale are no longer rendered into any prompt by default,
because re-showing a model its own rejected patch anchors it to an approach that was wrong at
the approach level. A new pre-probe rejection path is added: a proposal that re-fingerprints an
already-refuted `approach_signature` is refused before `git apply --check`, costing no attempt
for the in-rung re-ask. The terminal `repo_abandoned` JSONL event still carries all three attempt
transcripts — that record is for **human triage** and is explicitly never an input to a prompt or
to a resumed run (§11.5).

**Amended by ADR-0022** (stub lifecycle). The 3-attempt budget, the escalating-strategy principle,
the transient/substantive split, and the `REQUIRES_HUMAN_INTERVENTION` terminal contract all stand
unchanged. What changes is the sentence above about a dependent being "marked `blocked_by` and
skipped": under `--stub-blocked` a **direct** dependent may instead migrate against a stub and be
marked `DEGRADED`, and ADR-0022 gives that state a real exit. Two consequences bear on this ADR
specifically. First, a stub-driven **revalidation round does not consume `phases.attempts`** — the
harness's own escape hatch may not spend a repo's three chances, the same principle ADR-0019's
rollback already relies on; it is capped separately by `stubs.max_revalidation_rounds` and
`stubs.revalidation_max_cost_usd`. Second, one new failure class routes **around** the ladder
entirely: `FailureClass.STUB_DIVERGED` (the provider was fixed but its API moved) goes straight to
`REQUIRES_HUMAN_INTERVENTION` rather than spending rungs 2 and 3, because a version skew that the
build event protocol has already named is not a repair the ladder can propose.

**Amended by ADR-0023** (provider-agnostic LLM layer). The 3-attempt budget, the escalating-strategy
principle, the transient/substantive split, and the terminal contract are unchanged. Two wording and
one behavioural change. Wording: the rungs are named by **role** (`transform_repair`, `escalation`)
resolving through `config/models.yaml` to a **tier** (`WORKHORSE`, `HEAVY`), never by a model string —
"attempt 2 runs `claude-sonnet-5`" becomes "attempt 2 runs the `transform_repair` role, which the
active profile routes to the `WORKHORSE` tier". Behavioural: **backend failover does not consume
`phases.attempts`**, for the same reason a transient 429 does not — an endpoint that refused to
answer produced no evidence about the repo. A failover exhausts the tier's backend list *inside* one
attempt; only when every backend for that tier is unavailable does the attempt end, and it ends as
`FailureClass.BACKEND_UNAVAILABLE` with the run halted (SPEC §11.8, exit code 8) rather than as a
spent rung, so a two-hour outage cannot silently burn 250 repos' ladders.

---

## ADR-0015 — `Typer` is the CLI framework

**Decision.** The `fleet` entry point (`python -m fleet`, `src/fleet/cli.py`) is a
`typer.Typer` app with one command per phase verb (`scan`, `sequence`, `plan`, `transform`,
`build`, `verify`, `pr`, `status`, `resume`). Command parameters are declared as annotated
Python function signatures; `rich` output is enabled for human rendering and every command
also supports `--json` for machine-readable stdout. No `click.Group` hand-wiring, no
`argparse`, and no bespoke dispatch table.

**Rationale.** Typer derives the parser from type-annotated signatures, which means the CLI
surface is validated by the same `mypy --strict` gate as the rest of `src/fleet/` and cannot
drift from the functions it calls — for a harness whose §12 acceptance criteria are literally
"this command exits 0", the parser being type-checked is worth more than its ergonomics. It
is a thin layer over `click`, so we inherit `click`'s maturity, shell completion, and testing
harness (`CliRunner`) without inheriting its decorator boilerplate.

**Alternatives rejected.** `argparse` (no type derivation, hand-maintained help, unpleasant
subcommand nesting); raw `click` (the same result with substantially more decorator noise and
a second source of truth for parameter types); `fire` (magic dispatch, no typed contract, poor
help); a hand-rolled dispatcher (re-implements completion, help, and error handling badly).

---

## ADR-0016 — `aiosqlite` is the async SQLite driver, with a strict single-writer rule

**Decision.** SQLite access (ADR-0004) goes through `aiosqlite>=0.20` — `sqlite3` executed on a
per-connection worker thread owned by the driver, awaited from the event loop. **Exactly one
writable connection exists per run**, owned by the `PhaseRunner` in the orchestrator process;
workers return a `WorkerResult` and the runner persists it, and `ProcessPoolExecutor` children
receive no database handle at all. Readers may open additional `mode=ro` connections, which WAL
makes concurrent with the writer. All write transactions use `BEGIN IMMEDIATE`, with
`busy_timeout=5000` as a backstop; `ATTACH` and cross-database transactions are forbidden.
`state/db.py` raises if a second writable connection is requested in a process.

**Rationale.** `sqlite3` is blocking, and a blocking commit on the event loop stalls every
in-flight LLM stream and heartbeat simultaneously — the precise failure ADR-0003 draws its
offload boundary to avoid — so the driver has to be async, and `aiosqlite` is the one that is a
faithful `sqlite3` wrapper rather than a new API to learn. The single-writer rule is the other
half of the decision and matters more: SQLite in WAL mode supports one writer regardless of how
many connections exist, so *designing* for one writer converts a class of `SQLITE_BUSY` races,
lost updates, and torn multi-table transactions into a startup error instead of an intermittent
production failure.

**Alternatives rejected.** Blocking `sqlite3` on the event loop (stalls everything at every
commit); `sqlite3` behind `run_in_executor` with a `ThreadPoolExecutor` (forbidden by ADR-0003,
and re-implements `aiosqlite`); SQLAlchemy async (an ORM, explicitly rejected in ADR-0004, plus
a second dialect layer over the SQL we want to read literally); one writable connection per
worker (the lost-update and `SQLITE_BUSY` failure mode this ADR exists to prevent);
`sqlite3` with `check_same_thread=False` shared across tasks (undefined interleaving of
transaction boundaries).

---

## ADR-0017 — `Coordinate.key` is the one join-key format: `"{ecosystem}:{group}:{name}"`, case-folded

**Decision.** Every normalized dependency address carries a derived, non-optional
`key = f"{ecosystem.value}:{group.lower()}:{name.lower()}"`, with `group = ""` when the
ecosystem has no group concept. **It is the only join key used anywhere downstream of a
`ManifestAdapter`**: `coordinates.coord_key` is the primary key, `edges.dst_coord_key` and
`manifests.publishes_key` are FK-by-value against it, and internal-vs-external resolution is a
single indexed lookup on it. The key deliberately **excludes the version** — versions live in
`version_spec` on the edge, because ownership is a property of a coordinate and not of a
release. Colons inside a component are rejected at validation; a duplicate key across two
publishing repos is a `collisions` row, never a silent overwrite (SPEC §3.1 step 3).

**Rationale.** Six ecosystems name things six ways — `com.acme:commons`, `@acme/ui`,
`github.com/acme/svc`, `acme-commons` — and the entire cross-repo graph is one question asked
~10⁵ times ("does this dependency resolve to a repo we own?"), so collapsing all six into one
lexically-comparable string turns that question into a B-tree lookup instead of six per-ecosystem
matchers. Case-folding is not cosmetic: npm scopes, Maven groupIds in the wild, and Go module
paths differ in case across manifests of the same fleet, and an un-folded key silently produces
two "different" coordinates for one artifact — which shows up as a *missing* edge, the most
expensive kind of bug this harness can have.

**Alternatives rejected.** A tuple key `(ecosystem, group, name)` (not a SQLite primary key
without composite indexes everywhere, and unusable as a JSON-safe identifier in artifacts or
events); including the version in the key (splits one artifact's ownership across releases and
makes range specs unjoinable); Package-URL/`purl` (a good standard, but its type taxonomy and
qualifier syntax carry semantics we do not use, and it would still need normalizing before
joining); per-ecosystem key formats (the `if ecosystem == …` branch the whole architecture
forbids); preserving case (produces phantom duplicate coordinates and missing edges).

---

## ADR-0018 — `SHARED_RESOURCE` and `DYNAMIC_REF` edges are recorded and reported, but excluded from DAG ordering

**Decision.** Two of the six `EdgeKind`s are **advisory**: `SHARED_RESOURCE` (the same DB table,
Kafka topic, or queue name appearing in two repos) and `DYNAMIC_REF` (reflection, dynamic
`import`, string-built class names, DI string keys). Both are inferred, persisted to `edges`
with evidence, surfaced in `fleet status --format dot` and in the dependent's PR body — and both
are **excluded from `DAG_EDGE_KINDS`**, so neither orders a migration wave. The default set is
`graph.dag_edge_kinds: [DECLARED_DEP, PUBLISHED_ARTIFACT, INTERNAL_IMPORT, API_CONTRACT]`, and
an operator may add either kind for a specific fleet via config without a code change.

**Rationale.** The DAG answers exactly one question — *in what order can these repos be built
and merged without a red CI* — and neither signal constrains that: two services sharing a
`users` table have no build-order relationship at all, and a reflective lookup is by
construction invisible to the compiler that would enforce ordering. Including them would inflate
the graph with edges that manufacture false cycles (shared infrastructure is densely connected
by nature, so a naive `SHARED_RESOURCE` graph is nearly one giant SCC) and would push real repos
into `ATOMIC_WAVE` handling for a coupling no build system will ever see.

**Alternatives rejected.** Ordering on them (manufactures giant SCCs from shared infrastructure
and deadlocks the sequencer for zero build-correctness gain); discarding them entirely (throws
away the two signals most likely to explain a *runtime* failure after a green migration — a
`NoClassDefFoundError` or a schema-migration conflict — which is precisely the class of problem
a reviewer needs told); promoting them to full edges when confidence is high (confidence
measures *evidence quality*, not *whether the coupling is a build-order constraint*; conflating
the two would be a category error).

**Amended by ADR-0019.** `EdgeKind` gains `CONTRACT_IMPL` and `CONTRACT_CONSUME`, and both are
**in** `DAG_EDGE_KINDS` — which is consistent with, not an exception to, the rule above: they are
ordering edges precisely because they exist only as the product of a hoist, and a hoist is a
statement about migration order. The advisory status of `SHARED_RESOURCE` and `DYNAMIC_REF` is
unchanged. The original text stands.

---

## ADR-0019 — DAG nodes are `(kind, id)`; shared contracts are first-class nodes, hoisted to break cycles

**Decision.** A DAG node is `(kind, id)` with `kind ∈ {REPO, CONTRACT}`, not always a repo.
A **contract node** is one declared unit of shared interface — a protobuf `package`, an OpenAPI
document, an Avro/Thrift `namespace`, or an explicitly-configured shared library module — created
only by the bounded extraction pass of §3.1 step 5b, never per symbol. Byte-identical vendored
copies of one contract collapse to a single node keyed `contract_id = "{kind}:{identifier}"`;
ownership is decided by the *existing* `owns:`-hint → non-vendored → shallowest-path →
`commit_count` → `repo_id` ladder and recorded as a `collisions` row of the new kind `CONTRACT`,
reusing the coordinate-collision mechanism rather than inventing a parallel one. In cycle
breaking, a new rung **6c-H runs strictly before** whole-repo edge-breaking (6d) and before the
atomic-wave fallback (6e): for each SCC it hoists extractable contracts greedily, ranked by
`(repos_freed desc, extraction_confidence desc, blast_radius asc, contract_id)`, recomputing SCCs
after each hoist, until the SCC dissolves or no contract helps — at which point control falls
through to 6d and 6e **unchanged**. A hoisted contract migrates as its own early wave (it has no
outbound edge into any repo, so longest-path layering places it first), its history is preserved
by a path-filtered `git-filter-repo` pass over its owning repo's mirror, and its bindings are
**regenerated** in-monorepo via `proto_library`/`*_proto_library` rules rather than copied.
`graph.scc_atomic_threshold` and `graph.scc_hard_max` remain the final fallback, unmodified.

**Rationale.** In a real 250-repo fleet the overwhelming majority of cross-repo cycles are not
implementation cycles at all but shared-contract cycles — a proto package or a "commons" module
that its owner and its consumers all depend on — and against those the pre-existing remedies were
both bad: bundle up to 40 repos into one unreviewable atomic PR, or refuse and hand a human the
problem. Cutting the cycle at the contract is strictly cheaper and structurally correct, because
the shared interface genuinely *is* an independent artifact that should be built once and depended
on by everyone, so hoisting it is the migration the organization wanted anyway.

**Alternatives rejected.** Per-symbol graph nodes (unbounded — ~10⁵ symbols per repo — and the
resulting node set is neither reviewable nor migratable, whereas declared contract artifacts are
both); automatic repo *splitting* by heuristic (a general "carve this repo into modules" pass is a
refactoring product, not a sequencing one, and its failure mode is silently shipping a broken
package boundary); leaving the layout's `proto/<proto-package>/` directory as a naming convention
with no algorithm (a directory nobody fills is a comment, not a mechanism); raising
`scc_atomic_threshold` so large SCCs simply pass (moves the pain to reviewers and makes a single
member's failure fail 40 repos); ordering on the existing `API_CONTRACT` edge kind alone (it
records that a contract coupling *exists* but leaves the contract inside its owning repo, so the
cycle survives); and hoisting *after* edge-breaking (6d would already have suppressed real
`DECLARED_DEP` edges to achieve what a hoist achieves losslessly, so the cheaper remedy must run
first).

**Amends ADR-0011** (ingest) and **ADR-0018** (advisory edge kinds); see the amendment notes on
each. Supersedes nothing.

**Amended by ADR-0020 — contract BUILD emission gets an owner.** This ADR states that a hoisted
contract's bindings are "**regenerated** in-monorepo via `proto_library`/`*_proto_library` rules
rather than copied" but leaves the emitting component unnamed. ADR-0020 assigns it to a
`ContractAdapter` keyed by `ContractKind` (`src/fleet/ecosystems/contracts/`), so each of the five
`ContractKind`s has exactly one owner for its language-neutral rule, and the per-language binding
*rule name* comes from `EcosystemAdapter.contract_bindings` — a declarative table, not a branch.
`hoist_target_path` is now `ContractAdapter.layout(contract)`; the values in SPEC §3.3's
`ContractKind` table are unchanged, and 6c-H, the ownership ladder, the `retargeted_from_repo_id`
rollback, and the `contracts.status` lifecycle are untouched.

**Amended by ADR-0024 — the un-hoist revert is its own record.** Nothing about node identity,
contract hoisting, 6c-H, or the `retargeted_from_repo_id` restoration changes; that rollback was
always a SQL operation on orchestration state and stays exactly as specified. One sentence in this
ADR's rollback path does change: the `git revert -m 1` of a merged hoist was "journalled as a
`mutations` row like any other mutation", and the `mutations` table no longer exists. The revert is
now an ordinary trailered commit (`Fleet-Run-Id` / `Fleet-Repo-Id` / `Fleet-Phase` / `Fleet-Task-Id`
/ `Fleet-Attempt` / `Fleet-Patch-Id`) on the integration branch, found with `git log --grep`, and
therefore its own durable record — which strictly improves this ADR's claim that "rollback is cheap
because 5b never mutated anything destructively", since after a crash mid-revert there is nothing
to unwind beyond asking Git whether the revert commit landed.

---

## ADR-0020 — Bazel layout and target generation are adapter-driven: the `EcosystemAdapter` registry

**Decision.** All *output-side* language knowledge — monorepo destination directory, `BUILD.bazel`
target emission, `MODULE.bazel` external-dependency declarations, toolchain registration — moves
out of `src/fleet/bazel/` and into a second adapter package, `src/fleet/ecosystems/`, using
ADR-0005's registry mechanism verbatim (`@register` decorator, `pkgutil` discovery, duplicate
claim is a startup error). `EcosystemAdapter` declares `ecosystems: frozenset[Ecosystem]`,
`monorepo_dir`, `ruleset`, `uses_gazelle`, and `contract_bindings`, and implements `path_tail`,
`workspace_deps`, `generate_targets`, `test_targets`, `gazelle_config`, and
`toolchain_requirements` (SPEC §7.5). Six ship: `jvm` (`MAVEN` + `GRADLE`), `js`, `py`, `go`,
`rust`, `unknown`. Contract-node emission is a sibling `ContractAdapter` keyed by `ContractKind`
(SPEC §7.6), five of which ship. `src/fleet/bazel/generators.py` is **retired**, replaced by
`bazel/emit.py` (driver), `bazel/module.py` (MODULE.bazel/MVS), and `bazel/render.py` (Starlark
text) — none of which may contain an `Ecosystem` comparison. `layout()` becomes
`adapter.monorepo_dir / adapter.path_tail(coordinate)`, total because
`ecosystems.discover()` asserts a bijection between `Ecosystem` and the registry at startup. The
`Ecosystem` enum stays hand-maintained. New models in `src/fleet/models/build.py`: `BuildUnit`,
`BuildTarget`, `WorkspaceDep`, `GazelleConfig`, `ToolchainRequirement`, `BuildPlan` (SPEC §5.6).

**Rationale.** The §1 invariant was self-contradictory: it promised "one new file and zero edits
anywhere else" in one sentence and exempted `bazel/generators.py` from the no-branching rule in
the next. Both could not be true, and the reason was that `generators.py` had no dispatch
mechanism at all — it was specified only as "per-ecosystem BUILD generation driven by
`Coordinate`", i.e. an `if/elif` ladder waiting to be written, which is the exact construct
ADR-0005 was created to forbid on the input side. Symmetry is the fix: input-side language
knowledge already had a registry, so output-side language knowledge gets the same one rather than
a second mechanism of a different shape. The abstraction is justified by count — six ecosystem
implementations and five contract implementations, all shipping, none speculative — and the ABC
is deliberately sized to those eleven: `uses_gazelle` exists only because `go.py` genuinely
delegates to Gazelle while the other five generate, and an ABC that assumed uniform generation
would have forced `go.py` to lie. `contract_bindings` is a `dict`, not a method, because it is
data (a rule name per IDL kind) and a method would have invited a branch inside it. What this
does **not** buy is a shorter touchpoint list by fiat: adding a language is four edits, and SPEC
§1, §12.34 and §14.3 now say four rather than one.

**Alternatives rejected.** *Extending `ManifestAdapter` with the build methods* — the cardinalities
differ (`maven.py` and `gradle.py` are two manifest formats with one JVM build story), so this
forces a byte-identical `ecosystems/gradle.py` and couples a Phase 1 concern to a Phase 3 one.
*Keeping `generators.py` as the adapter host* — preserves the §1 exemption clause that caused the
contradiction, and leaves the pipeline package importing language knowledge. *Deriving the
`Ecosystem` enum from adapter registration* — genuinely tempting, and rejected on evidence: the
values appear in SQLite `CHECK` constraints (SPEC §6) and in `Coordinate.key` (ADR-0017), and
`Ecosystem.UNKNOWN` is referenced statically in several modules, so a dynamic enum breaks
`mypy --strict` and makes the DDL unverifiable. The startup bijection check gets the actual
benefit — no member can exist without an adapter — without the cost. *Contract emission as a
method on `EcosystemAdapter`* — the target that matters is the language-neutral one (one
`proto_library` per contract regardless of consumer count), and six adapters would race to emit
it with no single owner. *A YAML/plugin-entry-point mechanism instead of a Python ABC* — target
generation is code, not data (`ts_project` attribute derivation is not expressible as a table),
and an entry-point mechanism would be the third registry shape in one codebase.

**Amends ADR-0005** (adapter scope), **ADR-0007** (per-ecosystem generator dispatch), and
**ADR-0019** (contract BUILD emission ownership); see the amendment notes on each. Supersedes
nothing.

**Amended by ADR-0065 — the file layout is retired; the principle stands and is what is
enforced.** `src/fleet/bazel/generators.py` is **not** retired and is not renamed:
`bazel/emit.py`, `bazel/module.py` and `bazel/render.py` were never created, and the driver this
ADR called `emit.py` is `workers/buildgen.py` (plus `cli._run_gazelle`, ADR-0056). What this ADR
actually decided — that no module in `src/fleet/bazel/` may contain an `Ecosystem` comparison, and
that output-side language knowledge lives only in `src/fleet/ecosystems/` — **shipped and is
mechanically guarded** by five invariant tests in `tests/test_ecosystems.py` that grant
`src/fleet/bazel/` no exemption. Everything else here is unchanged: the ABC, the registry
mechanism, the six shipped adapters, the §5.6 models, the four-edit touchpoint count. Separately,
ADR-0065 records that this ADR's **second half never shipped**:
`src/fleet/ecosystems/contracts/{base,proto,openapi,avro,thrift,shared_lib}.py` and the
`ContractAdapter` registry keyed by `ContractKind` do not exist, so `contract_bindings` is declared
and read by nothing and SPEC §12.32's `contracts.discover()` criterion is unsatisfiable as written.

**Amended by ADR-0066 — SPEC §7.5 no longer embeds this ABC's source.** The `EcosystemAdapter`
listing this ADR introduced is replaced by prose plus a pointer; `src/fleet/ecosystems/base.py` is
the normative artifact. The embedded copy had fallen two ADRs behind the shipped ABC
(`workspace_deps(unit)` per ADR-0046, and `import_specifier` made abstract by the same ADR), and
nothing checked it.

---

## ADR-0021 — The escalation ladder carries *failure evidence*, not *prior patches*; approaches are fingerprinted and repeats are refused

**Decision.** Each rung of the ADR-0014 ladder declares its prompt composition explicitly as a
`ContextPolicy`, over a hard split between **evidence** — the `FailureClass`, which probe failed,
verbatim compiler/linter/test stderr, unresolved symbols and imports, the target file's current
content, the relocation map, and the repo's dependency context, all of which are carried on
*every* rung — and **priors**, meaning the previously proposed diffs and the previous model's
rationale, which are **never rendered into a prompt by default**. Three policies exist:
`EVIDENCE_ONLY`, `EVIDENCE_PLUS_REJECTED_APPROACHES` (evidence plus `RejectedApproach` records —
approach signature, a one-line approach-level reason, and the failure class, with no field capable
of holding diff text), and `EVIDENCE_PLUS_PRIORS` (the pre-amendment behaviour, opt-in only). The
default ladder becomes: attempt 1 deterministic; attempt 2 `claude-sonnet-5` at `EVIDENCE_ONLY` —
a **fresh slate**; attempt 3 `claude-opus-5` at `EVIDENCE_PLUS_REJECTED_APPROACHES`, so the
strongest model gets the search-space pruning without the dead diffs. Every proposal is
fingerprinted in deterministic code (`rewrite/approach.py`, never by a model) as
`approach_signature = sha256` over the sorted `(path, change_kind, target_symbol)` tuples of its
non-whitespace hunks, excluding line offsets, context lines, hunk order, and formatting; a
proposal whose signature is already in the task's `rejected_approaches` set is an **anchoring
loop** and is refused *before* any probe or worktree mutation, re-asked at most
`transform.anchoring.max_reasks_per_rung` times (not an attempt, since no verification was spent),
and then advances the rung. `MAX_ATTEMPTS` stays 3, the `REQUIRES_HUMAN_INTERVENTION` terminal
contract is untouched, and the composition is config (`transform.ladder`) plus a CLI override
(`fleet transform --context-policy N=POLICY`) rather than code. Because the composition changes
what was asked, `context_policy` and a `rejected_approach_digest` over the signatures actually
rendered become components of the `llm_cache` key (SPEC §11.6).

**Rationale.** When a patch fails a build or parse probe the defect is usually in the *approach*
— wrong target module, wrong layer, wrong direction of fix — and re-showing a model its own
rejected diff reliably produces a re-indented restatement of it, so the ladder's second and third
rungs converge on the same dead idea instead of searching elsewhere. Discarding the failure
evidence along with the patch would be the opposite error and would make each rung a blind retry,
so the amendment separates the two and keeps refutations only in the abstract, fingerprinted form
that prunes the search space without supplying anything to copy.

**Alternatives rejected.** *Keep feeding both transcripts* (the pre-amendment behaviour) — this is
the anchoring defect itself, and it is retained only as an opt-in `EVIDENCE_PLUS_PRIORS` so the
improvement can be A/B measured rather than merely asserted. *Drop all prior-attempt information
and make every rung fully blind* — throws away the compiler's own diagnosis, which is the single
most informative artifact the loop produces, and lets attempt 3 rediscover attempt 2's failure at
full Opus cost. *Have a model summarize the prior attempt into the "rejected approach" note* —
adds an LLM call on the failure path, makes the pruning set non-deterministic, and violates
ADR-0008 (code decides, models propose); the one-line reason is emitted by the proposing rung's
own structured response and the signature is pure code. *Detect anchoring by diff similarity
(edit distance / hashing the patch text)* — whitespace, hunk offsets, and statement reordering
defeat it, which is exactly the disguise a re-prompted model applies; fingerprinting
`(path, change_kind, target_symbol)` is invariant to all three. *Let an anchored repeat run and
fail its probe naturally* — burns a container, a worktree mutation, and a build attempt to learn
something already known before `git apply --check`. *Raise `MAX_ATTEMPTS` to give the fresh-slate
rung more room* — the mandate fixes the budget at 3, and the point of the amendment is to spend
those three on three *different* ideas.

**Amends ADR-0014** (retry/escalation policy: attempt 2 and attempt 3 context composition, and the
new pre-probe rejection path); see the amendment note there. **ADR-0009 is unchanged** — the
role→model map, the tiers, and `config/models.yaml` are exactly as they were; this ADR changes
what a rung is *shown*, never which model answers. Supersedes nothing.

**Amended by ADR-0023** (provider-agnostic LLM layer). Everything about context composition is
untouched: `ContextPolicy`, the fresh-slate default, `approach_signature`, the pre-probe rejection
path, and the rule that no raw prior diff is rendered all stand. One thing changes, and it changes
in this ADR's own favour: the `llm_cache` key gains **`backend`** and the **resolved `model_id`**
alongside `context_policy` and `rejected_approach_digest`, which are **preserved as key components,
not replaced**. The reasoning is identical to this ADR's own — a cache key must carry every
component that changes the *identity* of the call, and "which model answered" changes it at least
as much as "what was it shown". Without `backend`, a run that fails over from a frontier model to a
local 8B would poison the shared, run-unscoped cache with answers a later frontier-routed call would
silently accept; the fresh-slate guarantee this ADR bought would be intact while the *quality*
guarantee behind it quietly was not. `approach_signature` is still computed from the response and
still never enters the key.

**Amended by ADR-0024 — "the same patch is never applied twice" is re-founded on Git.** The
anti-anchoring machinery is untouched: `approach_signature` is still a sha256 over sorted
`(path, change_kind, target_symbol)` tuples computed from the *response* before `git apply --check`,
`rejected_approaches` is still the ladder's memory, and a detected repeat still costs no probe, no
container, and no `phases.attempts`. What changes is the layer beneath it. This ADR leaned on the
`mutations` unique index over `(run_id, repo_id, patch_sha256)` to make re-application impossible;
that index is deleted with the table, and the guarantee now rests on the **`Fleet-Patch-Id` commit
trailer** — sha256 over the sorted `(path, sha256(diff))` pairs of the patch set, content-only and
therefore stable across attempts and across a crash — queried with `git log
--format='%(trailers:key=Fleet-Patch-Id,valueonly)'` and backed by `git apply --check --reverse`.
The guarantee is *stronger* here than it was in SQLite: the trailer and the change it names are the
same commit object, so a crash cannot leave one without the other, which a separate index could
never promise. Note that `approach_signature` and `Fleet-Patch-Id` remain deliberately different
functions — the signature abstracts away formatting so a retyped idea collides, while the patch id
is byte-exact so only a genuinely identical change is skipped.

---

## ADR-0022 — A stub is a lie with an expiry: `DEGRADED` leaves the machine only through a budgeted, incremental revalidation round

**Decision.** Every `stubs` row carries an explicit four-state lifecycle — `ACTIVE`,
`SUPERSEDED`, `RESOLVED`, `ABANDONED` — with exactly four transitions, each with one deterministic
trigger, implemented in `orchestrator/stubs.py` with no model in the loop (SPEC §3.5.1). The
moment a provider repo reaches `SUCCEEDED` **with its PR `MERGED`**, the single writer supersedes
its `ACTIVE` stub rows in the same `IMMEDIATE` transaction, rewrites each consumer's dependency
from `//third_party/stubs/<coord>` to the provider's real `//` label, and enqueues one
`TaskKind.REVALIDATE` task keyed
`revalidation_key = 'r{round}:{sha256(sorted provider_repo_ids)}'`, which is the idempotency key
that makes a replay, a `fleet resume`, and a manual `fleet stubs resolve` all converge on one
task. The round is **incremental by construction**: Phase 2 is skipped when
`git rev-parse migrate/<consumer>^{tree}` equals the consumer's last `APPLIED` phase-2
`mutations.post_tree_sha` — a content hash, not an assumption — and Phases 3–4 reuse the existing
`verify.affected_only` rdeps machinery and the shared read-write `--disk_cache`, so only actions
downstream of the one swapped label re-execute. The consumer's draft PR is **updated in place**
(branch rebased, `git push --force-with-lease`, body regenerated via `gh pr edit`), keeping its
number, URL, and review history, and `fleet pr --ready` — the only path from draft to
ready-for-review — refuses with exit 2 while any of that repo's stub rows is `ACTIVE` or
`SUPERSEDED`. Rework is a **priced cost class**, not free: `repo_ledger.revalidation_usd` /
`revalidation_rounds` accumulate against `stubs.revalidation_max_cost_usd` (2.0, a sub-ceiling
inside `repo_max_cost_usd`) and `stubs.max_revalidation_rounds` (2), and
`stubs.revalidation: eager | batched | manual` (default `batched`) coalesces every newly-superseded
stub of one consumer into a single round per wave. `stub_fidelity` records which lie was told —
`PUBLISHED_ARTIFACT` (behaviour-honest for `pinned_version` and for nothing else) or
`EMPTY_FAILING` — and `VerificationReport.equivalence` is derived by a validator, so a green
against a stub is reported as `STUB_LIMITED` and never as `FULL`. Two end conditions are
fail-closed: stub **rot** (revalidation fails while the preceding stub-limited verification passed
and the round's only mutation is the label swap) is `FailureClass.STUB_DIVERGED` →
`REQUIRES_HUMAN_INTERVENTION`; and any row still open at end of run is `ABANDONED` by
`stub_reconcile`, leaving the consumer `DEGRADED` with `PrState.HELD` and the run exiting 7.
Contract nodes are never stubbed — they have no `phases` row, so a failed hoist takes the ADR-0019
rollback, which restores the true graph instead of publishing a placeholder.

**Rationale.** The previous spec asserted that a `DEGRADED` repo "is re-verified for free once `r`
is fixed" while providing no state, no trigger, no query, and no budget behind that sentence, so
the escape hatch could emit stubs it had no mechanism to retire — the exact shape of the silent
failure the harness exists to prevent. Naming the lifecycle, the trigger, and the price converts
an unfalsifiable claim into three testable ones (SPEC §12.37–39) and bounds the rework a late
upstream fix can force, without touching the two constraints that already bound the blast radius:
stubs remain direct-dependents-only and their PRs remain draft-only, so no merged work is ever
invalidated by a resolution.

**Alternatives rejected.** *Leave `DEGRADED` non-terminal and rely on `fleet resume` to notice* —
this is the defect being fixed; "not terminal" is a statement about the enum, not a mechanism, and
resume had no path from a fixed provider to a re-verified consumer. *Close the consumer's draft PR
and open a fresh one on resolution* — discards review history and PR number for a branch whose
content changed by one label, and turns every late fix into reviewer churn. *Delete the `stubs`
rows on resolution* — destroys exactly the audit trail that answers "was this repo ever verified
against something real?", which is the question a stub creates; the rows are retired to a terminal
state instead. *Re-run the full pipeline for the consumer* — Phase 2 output is provably unchanged
in the common case, so re-running it spends LLM tokens to reproduce a byte-identical tree. *Let
stub rot enter the ADR-0014 ladder* — burns Opus on a version skew the BEP has already diagnosed,
and the fix is a human's judgement about API compatibility, not a patch proposal. *Promote a
`DEGRADED` repo to `SUCCEEDED` when the revalidation budget is exhausted* — the single most
dangerous option available, since it reports as verified precisely the work that was never
verified; exhaustion is therefore modelled as unfinished work, and `stubs.on_budget_exhausted` has
no value other than `hold`. *A third, richer fidelity tier that synthesizes signatures* — a
generated façade compiles against code the real artifact would reject, which is the one failure
mode a stub must not have; `EMPTY_FAILING` fails loudly instead.

**Amends ADR-0014** (retry/failure: revalidation rounds do not consume `phases.attempts`, and
`STUB_DIVERGED` routes around the ladder); see the amendment note there. **ADR-0011 is unchanged**
— the stacking rule is in fact what gates transition T1, since a consumer may not point at a real
label before the provider's PR is `MERGED`. **ADR-0019 is unchanged** and is cited as the reason
contract nodes are outside this ADR's scope. Supersedes nothing.

**Amended by ADR-0024 — the "Phase 2 unchanged" check reads Git on both sides.** The lifecycle,
the four transitions, `revalidation_key`, the budget sub-ceilings, `stub_fidelity`, the
`STUB_LIMITED` equivalence rule, and the fail-closed end conditions are all untouched. Two
mechanical details are re-founded, because both named the deleted `mutations` table. (1) The
incremental-round proof compared `git rev-parse migrate/<consumer>^{tree}` against the last
`APPLIED` phase-2 `mutations.post_tree_sha` — half Git, half SQLite, and therefore exactly the
comparison that could disagree after a crash. Both sides now come from Git: resolve the newest
commit on the branch whose `Fleet-Phase` trailer is `2`, then compare its tree to the branch's
current tree. `phases.post_commit_sha` may short-circuit the walk as a cached pointer, but on
disagreement Git wins and the column is corrected. The property this ADR relied on is preserved
exactly — it is still a content hash, still deterministic, still gives the same answer however many
times it is asked — and it is now immune to a lost or stale database. (2) Stub-rot detection's
second conjunct, "the round's `mutations` set contains only the dependency-label rewrite", becomes
"the round's **commit set** (`git rev-list <round base ref>..migrate/<consumer>`) contains only the
dependency-label rewrite". The differential, and its distinction from `BUILD_ERROR`, are unchanged.

---

## ADR-0023 — The LLM layer is a `ModelClient` protocol over a backend registry; models are configuration, not architecture

**Supersedes ADR-0009.**

**Decision.** All model access goes through one ~70-line, framework-free `ModelClient` protocol
(`src/fleet/llm/client.py`): `async def complete[T: BaseModel](role, payload, response_model: type[T],
*, tier_override=None) -> ModelResponse[T]`, returning a **validated instance of the caller's Pydantic
model** plus a `TokenUsage` carrying `backend`, `tier`, and the resolved `model_id`. Concrete
backends self-register with the **same decorator registry** used by `ManifestAdapter` (§7.3) and
`EcosystemAdapter` (§7.5) — one registry pattern, now five users — and four ship:
**`anthropic`** (native Messages API), **`openai_compatible`** (any `base_url` + key: local vLLM,
Ollama, LM Studio, llama.cpp `llama-server`, TGI, and hosted OpenAI-compatible endpoints),
**`bedrock`**, and **`vertex`**. A new backend is one file under `src/fleet/llm/backends/` plus one
`@register_backend` line; nothing else in the harness may import a vendor SDK. `config/models.yaml`
becomes a set of named **profiles**, each mapping `role -> tier` and `tier -> [ordered backend
targets]`, where a target is `{backend, model_id, base_url?, effort?, capabilities_override?}` (`effort?` optional per ADR-0075 — the `?` is load-bearing here, since this line is the shape a reconciliation reads).
The three tiers are renamed to capability names carrying no vendor string — **`HEAVY`**,
**`WORKHORSE`**, **`CHEAP`** — and the ADR-0009 model IDs survive unchanged as the shipped
`default` profile. **No model string may appear in Python** (§12.40), so swapping the entire fleet
to local models is a config edit and nothing else (§12.41). Each backend declares
`ModelCapabilities`, and structured output is negotiated down a fixed ladder — native JSON-schema
mode → tool-calling coercion → constrained decoding → prompted JSON with one parse-and-repair
re-ask — with **Pydantic validation on our side as the invariant at every rung** (ADR-0002).
Failover is layered **above** §11.8's transient retry, never merged into it, and never consumes
`phases.attempts`. **The rejection of heavyweight frameworks survives ADR-0009 intact and is now
stated separately from vendor choice**: no LangChain, no LangGraph, no DeepAgents, no LiteLLM —
including the operator's local LiteLLM proxy, which is currently stopped, and a design that assumed
a proxy would be down with it.

**Rationale.** ADR-0009's binding constraint — "any non-Anthropic provider (out of scope by
directive)" — cited a directive that does not exist: `CLAUDE.md` names no vendor anywhere (its only
LLM content is Rule 5, "use the model for judgment"), `references/` mandates none, and the sentence
converted an unargued preference into an unfalsifiable constraint that then shielded two further
errors — a factual one (LiteLLM is nowhere in `references/`, so "the reference harness carries all
three" is false; `references/visa-vulnerability-agentic-harness/pyproject.toml` carries `langchain`,
`langgraph`, `deepagents`, `openai`, and `anthropic`) and a category one (JSON-Schema-driven
structured output is a property of Pydantic and of the schema, which OpenAI, Google, Mistral,
vLLM's guided decoding, and `instructor`/`outlines` all provide, so it argues for ADR-0002 and for
nothing about a vendor). Independent of that correction, a 250-repo run that can reach exactly one
endpoint has a single point of failure with no degraded mode, and this harness is for **local
development on this server**, where the workhorse connection is an OpenAI-compatible `base_url` —
so the abstraction is not speculative generality but the primary use case.

**Alternatives rejected.** *Keep ADR-0009 and add a second provider ad hoc* — the coupling is
structural (`AsyncAnthropic` singleton, model strings in prose and enum comments, no `base_url`),
so "add a provider" means rewriting the same call sites anyway, without gaining the registry that
makes the fifth backend free. *LangChain / LangGraph / DeepAgents* — ADR-0009's own rejection, and
it was right: we need one method, not an agent framework, and the reference harness's DeepAgents
path is the part of its model layer we deliberately do not copy. *LiteLLM as the universal shim* —
it would collapse the four backends into one dependency, but it buys a large surface and a proxy
process to keep alive for a normalization we perform in ~4 small files, and the operator's own
LiteLLM proxy is stopped, which is exactly the operational fragility a harness should not inherit;
the `openai_compatible` backend reaches every endpoint LiteLLM would have, directly. *One universal
`openai_compatible` backend and nothing else* — plausible, and rejected because native backends
expose per-vendor structured-output and thinking controls that an emulation layer flattens, and
because Bedrock/Vertex exist precisely to give the *same* model a *different* transport, which is
the cheapest real failover available. *Vendor strings as tier names* (`opus`/`sonnet`/`haiku` tiers)
— re-encodes the coupling in the config schema, where a local profile would have to name a tier
after a model it does not use. *A capability-probing handshake at startup that auto-detects each
backend's features* — attractive, but it makes every run's behaviour depend on a network probe;
capabilities are declared in code per backend, overridable per target in config, and `fleet models
check` probes them **on demand** and reports drift rather than silently re-planning.

**Amends ADR-0002** (Pydantic validation becomes the cross-backend invariant), **ADR-0014**
(rungs named by role/tier; failover does not consume attempts), and **ADR-0021** (`llm_cache` key
gains `backend` + resolved `model_id`, preserving `context_policy` and `rejected_approach_digest`);
see the amendment notes on each. **ADR-0008 is unchanged and is the reason this ADR is safe**: a
weaker local model may propose more badly, but it still cannot apply, build, or judge anything —
code does, and the verdict is an exit code. **ADR-0012, ADR-0013, ADR-0015 unchanged.**

**Prior art.** `references/visa-vulnerability-agentic-harness/docs/models.md` is the closest thing
in the reference material to this design, and three things are taken from it directly: (1) a
per-role `{id, via}` shape where `via` selects the *backend* and `id` the model, which is exactly
this ADR's `{backend, model_id}` target; (2) named **profile files** (`default.yaml`, `sdk.yaml`,
`full.yaml`, `taint.yaml`) selected as a unit, rather than per-role edits scattered through one
config — adopted as `config/models.yaml`'s `profiles:` map plus `--profile`; and (3) its
observation that one OpenAI-compatible backend plus a `base_url` already covers vLLM, Ollama,
Together, Azure, and Bedrock-in-compat-mode, which is why `openai_compatible` is the workhorse here
rather than a fallback. Two things are deliberately **not** taken: its `deepagents` backend (a
framework this harness rejects), and its rule that a mixed-vendor panel is refused at startup —
that constraint exists because its personas share one endpoint, whereas our tiers are independently
routed by construction, and mixed-vendor profiles are a supported configuration (§9).

---

## ADR-0024 — SQLite manages orchestration state; Git manages code state. The `mutations` write-ahead journal is deleted

**Decision.** The governing principle is a boundary, and it is absolute: **SQLite is
authoritative for orchestration state — task identity, `PENDING`/`RUNNING`/`DONE`/`FAILED`,
attempt counts, worker/owner, timestamps, sequence, failure class, token and cost accounting —
and Git is authoritative for code state.** Using SQLite as an orchestration event log is good
engineering; using it as a shadow version-control system is not, and the `mutations` table had
become the latter, storing `pre_tree_sha`, `post_tree_sha`, `patch_sha256`, patch files, and a
manual rollback log so that a resume could reconstruct what Git already knew. The table, its two
indexes (`ux_mutations_patch`, `ix_mutations_open`), its `PLANNED`/`APPLIED`/`ROLLED_BACK` state
machine, and the three-branch tree-SHA reconciliation in the old §3.2 step 6 are all **deleted**
(`PRAGMA user_version` 5 → 6) rather than shrunk to a vestigial table — everything it legitimately
held was either Git's (tree SHAs, diffs) or already carried by `tasks`/`attempts`/`phases`/`events`
(identity, ordering, timestamps), so a two-column survivor would only be a second place to look
for the truth. In its place: **apply the patch and commit immediately** on `migrate/<repo>` with
machine-readable trailers — `Fleet-Run-Id`, `Fleet-Repo-Id`, `Fleet-Phase`, `Fleet-Task-Id`,
`Fleet-Attempt`, `Fleet-Patch-Id` — so **the commit is the durable record**. Idempotency moves
from a SQLite unique index to the `Fleet-Patch-Id` trailer, a sha256 over the sorted
`(path, sha256(diff))` pairs of the patch set and nothing else, checked before applying with
`git log --format='%H %(trailers:key=Fleet-Patch-Id,valueonly)'` and backed by
`git apply --check --reverse`. Crash recovery becomes a Git **query** rather than a SQLite↔Git
comparison: `git rev-list --format='%H %(trailers:key=Fleet-Task-Id,valueonly)' <base>..migrate/<repo>`
answers "did this task's work land?", and the answer is copied down into the task row. Rollback is
Git-native — every phase anchors on a real ref, `refs/fleet/<run_id>/<repo_id>/phase-<n>/base`,
and discarding work is `git reset --hard` + `git clean -fdx`, or `git update-ref` on the branch,
or `git worktree remove --force` — never a diff replayed backwards. SQLite keeps commit SHAs and
`phases.base_ref` as **pointers**, which is explicitly fine; what it may no longer keep is a tree
SHA, a diff, or anything from which it could reverse a change itself. The resume invariant is one
line: **on any disagreement, Git is authoritative and the SQLite row is corrected, never the
reverse**, and the harness never writes to Git to make it agree with a row.

**Rationale.** The `SIGKILL`-between-apply-and-record hazard the journal existed for is real and
is now handled *better* by Git than by the journal, because `git commit` writes one tree covering
every changed file and moves the branch ref by `flock` + `rename(2)` — so a partially-applied
multi-file patch is not a state the branch can reach, only the disposable worktree can be dirty,
and recovery collapses from three cases (tree matches `pre` → re-run, matches `post` → promote,
matches neither → unwind) to two (commit present, commit absent) with no third case to get wrong.
Two durable records of the same fact can disagree and eventually will, since the journal's write
and Git's write cannot be made one transaction across a crash; keeping exactly one record of code
state, in the system that already stores code atomically and immutably, removes that drift class
by construction rather than by a reconciliation algorithm that has to be right.

**Alternatives rejected.** *Keep the journal and harden the reconciliation* — it can be made
correct for the cases enumerated, but every future mutation kind must remember to enumerate its
own, and the failure is silent when one does not; the redesign removes the enumeration entirely.
*Keep `mutations` as a thin pointer table (`task_id`, `commit_sha`)* — a vestigial table with no
column Git does not already answer, which invites re-growth of exactly the fields just removed;
those two facts belong on the `attempts` row that already exists for the same rung. *Use `git
notes` instead of commit trailers* — notes live in a separate ref that can be lost by a fetch,
push, or clone that does not know to carry it, so the identity would again be separable from the
change; a trailer is inside the commit object and travels with it into any clone. *Use `git
stash`/`git worktree` snapshots as the rollback anchor* — a stash is unnamed local state with its
own reflog semantics, whereas a ref under `refs/fleet/` is addressable, greppable, survives a
worktree being deleted, and is what `reset --hard` wants anyway. *Store `git patch-id` output as
the idempotency key* — attractive because it is offset- and whitespace-insensitive, but it is
defined only over a diff and would have re-introduced "keep the diff somewhere to recompute it";
the content sha256 is computed from the patch set already in hand and is carried in the commit.
*Leave `artifacts/diffs/` as durable state* — it is now an **export**, regenerable with
`git format-patch --stdout <base_ref>..migrate/<repo>`, and §12.45 asserts a run resumes correctly
after it is deleted outright.

**Amends ADR-0004** (SQLite's scope is narrowed to orchestration state), **ADR-0012** (the
resume contract's arbiter for code state is Git), **ADR-0019** and **ADR-0021** (their
"never applied twice" guarantee is re-founded on the `Fleet-Patch-Id` trailer), and **ADR-0022**
(its Phase-2 skip check now resolves both sides from Git); see the amendment notes on each.
**ADR-0011 is unchanged and is the reason this is cheap**: the harness already committed every
mutation to a per-repo `migrate/<repo>` branch, so this ADR deletes a parallel record rather than
building a new one. **ADR-0014, ADR-0016, ADR-0023 unchanged** — attempts, transient retries, and
failover accounting are orchestration state and stay exactly where they are.

**Prior art.** Machine-readable commit trailers as the join key between an automation system and
a repository are standard practice — Gerrit's `Change-Id`, `Signed-off-by` in the kernel workflow,
and `Co-Authored-By` in GitHub's tooling all identify an out-of-band unit of work from inside the
commit object precisely so the association cannot be separated from the change it describes. The
inverse lesson comes from this project's own `references/`: Constraint 2's warning about a
checkpoint that disagrees with reality is the same failure the `mutations` journal would have
reproduced, one layer down.

---

## ADR-0025 — Edge orientation is normative: `edges` rows are dependent → dependency, and `G_rev` is the graph that orders waves

**Decision.** Every row in `edges` is directed **dependent → dependency**: `(src_kind, src_id)` is
the node that *needs* something, `(dst_kind, dst_id)` is the node that *provides* it. `graph/build.py`
loads rows in exactly that orientation into `G`. **Every ordering question is then answered on
`G_rev = G.reverse()`, never on `G`** — wave layering is `nx.topological_generations(G_rev)`, "what
must land before this node" is `nx.ancestors(G_rev, n)`, and "what does stranding this node strand"
is `nx.descendants(G_rev, n)`. Wave 0 is therefore `{n : G.out_degree(n) == 0}` — the nodes that
depend on nothing. The orientation is stated once, normatively, in §3.1, and the identical sentence
is repeated at §5.3 (`DependencyEdge`), in the §6 `edges` DDL comment, and in the §7 graph worker
contract; every passage that implied the opposite is deleted rather than softened. `G` (unreversed)
is used for exactly two things and they are named in §3.1: rendering "what does X depend on" in
`fleet graph --explain`, and computing the blast radius of removing a provider.

**Rationale.** *Agent Recommendation* (`CLAUDE.md` Guardrail 1) — this came out of a six-way
adversarial self-review of `SPEC.md` on 2026-08-09, not from `references/` and not from any external
requirement. The spec as written contradicted itself: §3.1 read one way and the §6 DDL comment plus
the §13 failure rows read the other, so two implementers reading the same document would have
produced **exactly inverted wave orders**, and each would have passed its own unit tests. The defect
is invisible on a symmetric two-node fixture and catastrophic on a 250-repo fleet, where it means
building every leaf last. An orientation that is only implied by prose is not a contract; naming the
reversal explicitly, and naming the one graph object each question is asked of, is what makes it one.

**Alternatives rejected.** *Store both directions, or an explicit `direction` column* — doubles the
row count, and a disagreement between the two copies is a state no reconciliation can arbitrate.
*Materialize the reversed graph as a second SQLite table* — a derived view of a derived view; ADR-0004
already fixes `networkx` as the in-memory projection and `G.reverse()` is O(E) on a graph we hold
anyway. *Adopt dependency → dependent as the canonical orientation* — equally valid in the abstract,
but manifests are read dependent-first (a manifest declares what *it* needs), so the recording
orientation now matches the interrogation orientation and no `ManifestAdapter` has to invert.

**Amends ADR-0004** (the `networkx` view is specified as two named objects, `G` and `G_rev`) and
**ADR-0019** (node identity is unchanged; only the direction convention is pinned).

---

## ADR-0026 — Logical keys replace SQLite rowids in every cross-model reference: `edge_key` and `scc_id`

**Decision.** No cross-table or cross-model reference may be a SQLite `rowid` / autoincrement
`INTEGER PRIMARY KEY`, and no reference may be a positional integer over a recomputed collection.
Two logical keys are introduced and are the only admissible references. **`edge_key`** is
`sha256` over the edge's semantic tuple — `(src_kind, src_id, dst_kind, dst_id, kind,
coordinate_key)`, `\x1f`-joined, lowercase hex — and is `NOT NULL UNIQUE` on `edges`. **`scc_id`**
is `"scc:" + sha256(sorted member node keys, "\x1f"-joined)[:16]`, so a cycle's identity is derived
from its membership and nothing else. `cycles.broken_edge_keys`, `cycles.scc_id`,
`rejected_approaches`, collision findings, `wave_members`, the `attempts` failure evidence, and the
`migration_state.json` projection all carry these strings. Graph rebuild becomes
`INSERT … ON CONFLICT(edge_key) DO UPDATE`, never `DELETE FROM edges; INSERT …`.

**Rationale.** *Agent Recommendation* from the same 2026-08-09 adversarial self-review. Rowids are
allocated by insertion order and are reassigned when the graph is rebuilt — which happens on every
re-interrogation, on every resume that re-runs the graph phase, and after any `VACUUM`. Every
recorded cycle-break decision (`broken_edge_ids`) therefore **silently repoints at a different
edge**, and the failure is silent by construction: the FK still resolves, the row still reads, the
run still completes, and it breaks the wrong dependency. An unstable integer SCC index had the same
disease one level up — adding a single repo renumbers every cycle, so a `MANUAL` resolution recorded
by a human in run *n* attaches to an unrelated cycle in run *n+1*. Content-derived keys make the
reference survive a rebuild by construction rather than by remembering not to rebuild.

**Alternatives rejected.** *UUID4 per edge* — stable within one run, meaningless across runs; the
same edge re-interrogated is a different UUID, which defeats resume, the `llm_cache`, and any
cross-run comparison. *Keep rowids and simply never rebuild the graph* — an unenforceable discipline,
and rebuild is a legitimate, frequent operation. *Reference the composite natural key directly as a
six-column foreign key* — correct, but six columns wide in five referring tables and unusable as a
single token in JSONL, a commit trailer, or a CLI argument; the sha256 *is* that key, addressable in
one column. *Truncate `edge_key` to 16 hex chars like `scc_id`* — edges are ~10⁵–10⁶ per fleet, where
a 64-bit space is close enough to birthday range to be uncomfortable; SCC counts are ~10², so the
short form is safe there and is the one that a human reads in a `MANUAL` resolution.

**Amends ADR-0004** (identity of graph rows) and **ADR-0019** (`broken_edge_ids` is renamed and
retyped to `broken_edge_keys`).

**Amendment, 2026-08-09 — one recipe, in one place; `run_id` out, `dst_kind` in.** The sketch
above (`\x1f`-joined, `dst_id` + `coordinate_key`, no evidence terms) was a third spelling of the
key alongside §5's model docstring and §6's DDL, and §5 and §6 had in fact drifted apart: §6's
UNIQUE tuple carried `run_id` and omitted `dst_kind`, and the v007 back-fill followed §6. One edge,
two keys — the exact defect this ADR exists to prevent, one level up. Resolved by making the
derivation a **function, not prose**: `fleet.models.graph.edge_key_for()` (with `EDGE_KEY_COLUMNS`)
is the sole definition; inference, the v007 back-fill (as the `fleet_edge_key` SQL function), and
the tests call it, and §6's DDL cites it. The normative preimage is `(src_kind, src_id, dst_kind,
dst_ref, kind, evidence_path, evidence_line)`, NUL-joined, where `dst_ref` is `Coordinate.key` for
a REPO dst and the `contract_id` for a CONTRACT dst.

*`run_id` is NOT hashed.* It partitions rows — `edges` is per-run and CASCADEs — but it does not
identify an edge, and hashing it gives one edge two keys in two runs. That breaks §11.6 by
construction, not merely in spirit: `CycleFinding.broken_edge_keys` is an input to `run_digest`
(`state/digest.py::_cycle_section`), and §12.21 requires a fixture run re-run **from a clean
database** — a new `run_id` — to produce a byte-identical digest. A run-scoped key would report two
identical runs as inequivalent, i.e. exactly the run-scoped-identity failure for which this ADR
rejected UUID4. Row identity is therefore the pair: `edges` declares `UNIQUE (run_id, edge_key)`
(not a global `UNIQUE (edge_key)`, which would let one run's row be silently shared by — and
CASCADE-deleted out from under — another), and the rebuild is
`INSERT … ON CONFLICT (run_id, edge_key) DO UPDATE`.

*`dst_kind` IS hashed.* `dst_coord_key` holds a `Coordinate.key` for a REPO dst and a
`contract_id` for a CONTRACT dst; both are `:`-separated lowercase tokens from two independently
extensible enums. Today the two are separated only by the `edges` CHECK that ties `kind` to
`dst_kind`, so omitting `dst_kind` makes the key collision-free *only while a constraint in
another layer holds* — a hidden premise, for five bytes of preimage.

*The same correction applies to `scc_id`, which this ADR sketched as `"\x1f"`-joined.* It is
**NUL**-joined, and the sole definition is §5.3's `SccId` (implemented by
`fleet.graph.cycles.scc_id_for`) — the sketch above is not a second spelling to be reconciled but
prose that §5.3 supersedes. §3.1 6a, which restated the pre-ADR-0026 `min(sorted(member repo_ids))`
form and survived this ADR unamended, now cites §5.3 and states no recipe of its own. One further
consequence is spelled out where it belongs rather than here: `SccId` contains a `:`, which is
Bazel's label separator, so the coarsened `ATOMIC_WAVE` target's label is not the `scc_id`
verbatim — §3.3's `scc_dest`/`scc_target_name` is the one place that transformation is defined.

The divergence is guarded by `test_the_edge_key_recipe_and_the_edges_unique_tuple_are_one_key`,
which derives the key from the model and again from the columns the shipped DDL's UNIQUE clause
actually names, and requires one string. `SCHEMA_VERSION` stays **7**: this corrects an unreleased
baseline and folds into the existing v007 step.

---

## ADR-0027 — One in-process `StateWriter` actor owns every SQLite write; leases, fences, and CAS are retained anyway

**Decision.** Every write to `state/fleet.db` funnels through a single `StateWriter` actor — one
`asyncio.Task` draining an `asyncio.Queue` of typed write commands, owning the process's only
read-write `aiosqlite` connection and the exclusive right to `BEGIN IMMEDIATE`. Every other coroutine
holds a read-only connection (`file:state/fleet.db?mode=ro`). Callers `await` a future the writer
resolves, so a write still reads as a normal `async` call. `state/fleet.db` **must** be on a local
filesystem: startup probes the mount type and exits 2 on NFS/CIFS/9p/fuse rather than trusting POSIX
advisory locks that those transports do not honour. **Lease, fence, and CAS machinery is retained**
in full — `lease_owner`, `lease_expires_at`, a monotonic `fence` per leased row, and conditional
updates of the form `UPDATE … WHERE … AND fence = :fence` with the caller asserting `rowcount == 1`.

**Rationale.** *Agent Recommendation* from the 2026-08-09 adversarial self-review. Reviewers split on
this one and the split is the interesting part: single-writer removes writer-vs-writer contention, so
one camp read the fencing as now-dead weight. It is not, because it defends a **different** race —
the reaper reclaiming a lease from a worker that is merely slow rather than dead, after which the
slow worker's write arrives at a writer that is perfectly serialized and perfectly happy to apply it.
Serialization orders writes; it does not make a stale write wrong-looking. The fence token is what
makes it wrong-looking. The second reason to keep it is portability: the machinery is exactly what a
Postgres implementation needs, so the swap stays a substitution rather than a redesign.

**Records the ADR-0004 exit condition.** ADR-0004 chose SQLite "until it hurts" without saying what
hurting looks like. It is now written down: **swap the `StateRepository` implementation for Postgres
when either sustained write throughput exceeds roughly one commit per 50 ms for a full wave, or the
fleet must run workers on more than one host.** Both sides sit behind the same `StateRepository`
`Protocol` (`CLAUDE.md` Guardrail 3), the actor becomes a connection pool, `BEGIN IMMEDIATE` becomes
`SELECT … FOR UPDATE`, and the retained fence/CAS predicates port unchanged.

**Alternatives rejected.** *A `threading.Lock` or an `asyncio.Lock` around every write call site* —
correctness depends on every present and future call site remembering to take it; an actor makes the
connection unreachable without it. *A separate writer process* — adds IPC, a supervision story, and a
second crash mode, for a serialization guarantee we already have in-process. *Drop leases/fences now
that writes are serialized* — see above; this is the reaper race, and it is the one that corrupts.
*Allow a network filesystem with a documented warning* — a warning is not a mechanism, and the
resulting corruption is silent and unrecoverable.

**Amends ADR-0016** (which stated a single-writer *rule*; this ADR gives the rule an owning object)
and **ADR-0004** (SQLite's scope, plus a named exit condition).

---

## ADR-0028 — Cost ceilings are enforced by the schema, not by convention: `CHECK`, conditional CAS, expiring reservations, mandatory pricing

**Decision.** Spend control moves out of Python conditionals and into the `budgets` table. The row
carries `max_usd`, `spent_usd`, `reserved_usd` under
`CHECK (spent_usd >= 0 AND reserved_usd >= 0 AND spent_usd + reserved_usd <= max_usd)`. Reserving is
a **conditional CAS**, never a read-then-write:

```sql
UPDATE budgets SET reserved_usd = reserved_usd + :amt
 WHERE scope_key = :k AND spent_usd + reserved_usd + :amt <= max_usd;
```

and the caller asserts `rowcount == 1`. `rowcount == 0` is not an exception to log and swallow — it
is **the refusal path** and the only way a call is admitted. Every reservation carries a
`reservation_id` and a `lease_expires_at`; the reaper releases expired reservations so a `SIGKILL`ed
worker cannot sequester budget for the rest of the run. Settlement converts reserved → spent from the
backend's reported usage. Pricing is **mandatory per `(backend, model)` target** in config: an
unpriced target is a startup failure, **exit 2**, never a call priced at $0.

**Rationale.** *Agent Recommendation* from the 2026-08-09 adversarial self-review. The previous design
computed cost *after* the response returned and reserved nothing beforehand, so `max_usd` was an
observation, not a ceiling — a fleet could and would overshoot it by an unbounded amount and only
notice afterwards. Worse, any model target missing from the price map was billed at $0, which made
the *newest and most expensive* model the one the ceiling could not see. Putting the invariant in a
`CHECK` means even a future code path that forgets to reserve cannot drive the row past its ceiling:
the transaction aborts. That is the difference between a budget and a budget report.

**Alternatives rejected.** *Enforce in Python before the call* — a read-then-check-then-write with a
window between the check and the write, and N concurrent workers each individually under the ceiling
can be collectively over it. *Cap the number of calls rather than dollars* — per-call cost varies by
roughly three orders of magnitude across the ADR-0014 ladder, so a call cap is not a cost cap.
*Post-hoc alerting on overspend* — tells you after the money is gone. *Default unpriced targets to a
conservative high price* — silently mis-accounts, and the mis-accounting is invisible; failing at
startup costs one config line and is loud.

**Amends ADR-0008** (the invocation boundary now has a schema-enforced admission gate) and
**ADR-0023** (every backend registry entry must carry pricing to be selectable).

---

## ADR-0029 — Reserve at observed p95, not at `max_tokens`; a refused reservation is backpressure, not a failure

**Decision.** A reservation is `price(model) × (measured prompt tokens + p95 completion tokens for
this `(role, model)`)`, where the p95 is computed from the run's own observed completion lengths and
falls back to a config-seeded value while the sample is cold. Settlement re-prices to actual usage. A
response that overruns its reservation is allowed to complete and is charged in full — the
reservation is an admission estimate, not a hard stop. `max_tokens` stays what it always was, the
API's stop condition, and is no longer the accounting basis. A refused reservation parks the task as
`BLOCKED` with `reason=BUDGET` and re-offers it when budget frees; it never marks the task failed and
never consumes an ADR-0014 escalation rung.

**Rationale.** *Agent Recommendation* from the 2026-08-09 adversarial self-review, and the finding
here is arithmetic rather than stylistic. Reserving `max_tokens` against a per-repo ceiling made the
**top rung of the escalation ladder mathematically undispatchable**: the rung with the largest
`max_tokens` is by construction the one attempted last, when the least budget remains, so its
reservation could never fit and attempt 3 could never run. The ladder had a rung no fleet could ever
reach. Observed p95 sizes the reservation to what completions actually cost — typically a small
fraction of `max_tokens` — and the overrun path means the rare long completion is charged rather than
prevented. Treating refusal as backpressure keeps the distinction the escalation ladder depends on:
"we could not afford to try" is not "we tried and it did not work".

**Alternatives rejected.** *Reserve the mean* — under-reserves often enough that concurrent workers
routinely blow the ceiling between reservation and settlement. *Reserve nothing and settle only* —
that is the ADR-0028 defect this pairs with. *Raise the per-repo ceiling until `max_tokens` fits* —
hides the arithmetic bug and multiplies the fleet's true ceiling by the ladder depth. *Shrink the top
rung's `max_tokens` until it fits* — the top rung is large precisely because it carries the most
context; shrinking it to satisfy an accounting artifact would degrade the rung that exists to salvage
the hardest repos.

**Amends ADR-0014** (a budget refusal is not an attempt) and **ADR-0028** (it supplies the amount).

---

## ADR-0030 — Truncate evidence, never reject it: `TruncatedStr` plus full output in `artifacts/logs/`

**Decision.** Every evidence-bearing string field — `stderr_tail`, `stdout_tail`, `WorkerError.message`,
prompt and response echoes, probe output — is typed `TruncatedStr`, an annotated `str` whose
`BeforeValidator` **truncates to its documented budget and never raises**. Truncation keeps the head
and the tail and elides the middle, marked
`…[truncated N of M bytes; full: artifacts/logs/<run_id>/<task_id>/<attempt>.<stream>.log]`. The full
stream is written to that path **before** the Pydantic model is constructed, so the path in the marker
is always already valid, and only the path is persisted to SQLite. Length is consequently never a
validation error anywhere in §5.

**Rationale.** *Agent Recommendation* from the 2026-08-09 adversarial self-review, and it is the
finding with the ugliest failure mode. The previous `max_length` constraint raised `ValidationError`
when a build emitted an oversized stderr — which meant the **attempt row was never persisted at all**:
no failure class, no attempt increment, no escalation. The repair loop then re-read a task that
looked untouched and re-ran the identical failing build, forever, burning the container and the clock
on a loop no retry cap could break because no retry was ever recorded. The pathology is perfectly
inverted: it triggers precisely when a build fails most catastrophically, because that is what
produces the most stderr. Evidence must degrade, never disappear — a truncated log still classifies
the failure, and a missing row classifies nothing.

**Alternatives rejected.** *Raise the cap* — moves the cliff, does not remove it. *Persist the full
text in SQLite* — multi-megabyte BLOBs per attempt across 250 repos × 3 attempts, and ADR-0004's
store is for orchestration state, not for log archival. *Catch `ValidationError` at each call site* —
requires every present and future call site to remember, which is the class of discipline this project
keeps replacing with mechanisms. *Keep only the tail* — a compiler or Bazel failure puts the useful
signal at both ends (the first error and the summary), so head+tail with an elided middle is what a
classifier actually needs.

**Amends ADR-0002** (a validator may normalize evidence but may not reject it) and **ADR-0021**
(the ladder's failure evidence is guaranteed to exist).

---

## ADR-0031 — `WorkerResult` is five-valued with unit accounting; `WorkerContext` carries a deadline and a cancellation signal

**Decision.** `WorkerResult.status` is `Literal["ok", "partial", "failed", "timeout", "cancelled"]`,
accompanied by `completed_units: int`, `remaining_units: int`, `units_kind: str`, and an optional
structured `WorkerError`. `WorkerContext` gains `deadline: datetime` and `cancel: asyncio.Event`, and
every worker in §7 is contractually required to check both at unit boundaries and to return `partial`
rather than to raise. Orchestrator semantics are fixed per status: `partial` re-enqueues **only the
remaining units** and does **not** consume an escalation rung; `timeout` is the worker exceeding its
own deadline and does consume a rung; `cancelled` is external (wave abort, ADR-0029 budget refusal,
`SIGINT`) and consumes nothing, because nothing about the work was tried and found wanting.

**Rationale.** *Agent Recommendation* from the 2026-08-09 adversarial self-review. A boolean result
made partial progress unrepresentable, so a worker that migrated 40 of 60 files and hit its deadline
reported failure, the orchestrator replayed the entire task, and the 40 already-migrated files were
migrated again — duplicated work, and under ADR-0024's `Fleet-Patch-Id` idempotency a confusing empty
commit. It also conflated three different things a caller must treat differently: work that failed,
work that ran out of time, and work that was never allowed to start. Collapsing all three into
`ok=False` meant an operator-initiated abort burned an escalation rung, so cancelling a wave to fix a
config typo actively consumed the repo's remaining repair budget.

**Alternatives rejected.** *Boolean plus a free-text reason* — unswitchable, and every consumer
re-parses prose. *Raise typed exceptions instead of returning a status* — a raise cannot carry
`completed_units` past a process boundary intact, and §7 workers run behind a process pool (ADR-0003).
*Model partial work as a new task kind* — creates a second identity for the same task and breaks the
`(repo, phase)` attempt accounting of ADR-0014.

**Amends ADR-0014** (rung consumption is now status-dependent) and **ADR-0003** (the worker contract
crossing the process-pool boundary).

---

## ADR-0032 — Truncation is a backend outcome, not a schema violation: `finish_reason` → `OutputTruncated`, excluded from failover

**Decision.** The `ModelClient` protocol surfaces `finish_reason`, normalized across backends to
`stop | length | tool_use | content_filter | error`. `finish_reason == "length"` raises
**`OutputTruncated`** — a distinct exception, never a `ValidationError` — and the handler retries **the
same target** with a bounded larger `max_tokens` (×2, capped at the model's ceiling) or a segmented
request. `OutputTruncated` and `content_filter` are **explicitly excluded from the failover trigger
set**; only transport errors, auth failures, rate limits, 5xx, and backend-unavailable rotate to the
next backend in the ADR-0023 registry.

**Rationale.** *Agent Recommendation* from the 2026-08-09 adversarial self-review. Previously a
truncated JSON response failed Pydantic parsing, was classified as a backend defect, and the failover
chain retried the identical prompt on backend 2 and backend 3 — which truncated at their own
`max_tokens` in exactly the same place. Three times the cost, the same failure, and a final run report
claiming "all backends exhausted", which reads as a provider outage and would send an operator to
debug an infrastructure problem that does not exist. Truncation is a property of the *request*, not
of the *backend*, so the correct response is to change the request and hold the target fixed.

**Alternatives rejected.** *Always request the model's maximum `max_tokens`* — pays for headroom on
every call, and under ADR-0029 inflates every reservation. *Treat truncation as a hard failure and
escalate a rung* — spends an ADR-0014 rung on a mechanical, deterministically fixable condition.
*Detect truncation by attempting to parse the JSON* — a truncated response can still parse when the
cut lands after a closing brace, so the parse result is not a reliable signal; `finish_reason` is the
backend telling us directly.

**Amends ADR-0023** (the failover trigger set is enumerated, and `finish_reason` joins the protocol
surface).

---

## ADR-0033 — Stubbing unblocks transitively: a `DEGRADED` provider satisfies the admission gate and its subtree ships as one `STUB_LIMITED` draft stack

**Decision.** A provider in `StubState.DEGRADED` — stub published, not yet revalidated —
**satisfies the dependent-admission gate**. Its dependents are admitted to their wave and build
against the stub, and so are *their* dependents, transitively, all the way down. The entire subtree
downstream of a stub ships as a **single draft PR stack** labelled `STUB_LIMITED`, opened as draft
and held (`PrState.HELD`) until the ADR-0022 revalidation round clears the root stub, at which point
the whole stack is un-drafted together. The one exception is `StubFidelity.EMPTY_FAILING`: a stub
that raises on every call carries no behavioural signal, so it does **not** satisfy the gate and its
dependents remain `BLOCKED` with `reason=STUB_UNUSABLE`.

**Rationale.** *Agent Recommendation* from the 2026-08-09 adversarial self-review. The pre-existing
rule unblocked only the *immediate* dependents of a stub, which is non-transitive and therefore
useless on a real fleet: a chain `provider → A → B → C` admitted A and then stalled at B, because B's
own provider (A) was itself blocked-behind-a-stub rather than `DEGRADED`. The fleet spent real budget
generating stubs, admitted exactly one layer, and shipped nothing that used them — the worst of both
outcomes, since it paid the stub's cost and collected none of its value. Transitive admission is the
entire reason a stub is worth generating. Shipping the subtree as one held draft stack is what keeps
that safe: nothing stub-backed merges until the stub is discharged, and the blast radius is one
reviewable unit rather than N scattered PRs whose relationship to a stub is invisible.

**Alternatives rejected.** *Unblock everything, including `EMPTY_FAILING`* — a stub that raises on
every call produces dependents whose builds are meaningless, so it manufactures green PRs that encode
nothing. *Allow stub-backed PRs to merge* — puts a known lie on the trunk with no forcing function to
remove it, which is exactly the failure ADR-0022 exists to prevent. *Require a human acknowledgement
per admitted repo* — 250 repos, and the acknowledgement carries no information the `STUB_LIMITED`
label does not.

**Amends ADR-0022** (the admission gate becomes transitive; `EMPTY_FAILING` is carved out) and
**ADR-0011** (a stub subtree is one stacked-PR unit).

---

## ADR-0034 — PR merge state is ingested from the forge, never assumed: `fleet pr --sync`

**Decision.** `fleet pr --sync` polls the forge for every PR the run has open
(`gh pr view --json state,mergedAt,mergeStateStatus,…`) and writes the **observed** state into the
`prs` table. `prs.state` is only ever written from an observation — no code path may set `MERGED`
speculatively. `fleet run` invokes the sync at every wave boundary and immediately before any
admission decision that reads `PrState.MERGED`. It stays a command, not a service (see ADR-0038).

**Rationale.** *Agent Recommendation* from the 2026-08-09 adversarial self-review, and the single
worst liveness defect it surfaced. Three separate gates consumed `PrState.MERGED` — next-wave
admission, ADR-0022 stub revalidation, and the ADR-0019 un-hoist check — and **nothing anywhere in the
spec ever produced that value**. The fleet would complete wave 0, open its PRs, and then deadlock
permanently, waiting on a transition no code path could make. Every one of the three gates was
individually correct, which is why no unit test would ever have caught it: the missing piece was a
producer, and producers are invisible to consumer tests. The general lesson is recorded with the
decision — a state consumed by a gate must have exactly one named producer, and the spec now
identifies it for each terminal state.

**Alternatives rejected.** *Mark a PR merged when it is created* — ships unreviewed code by
definition. *Run a webhook receiver* — an inbound port, a public endpoint, a secret, and a
long-running process, all of which ADR-0004's zero-daemon posture rules out. *Infer merge from CI
going green* — green CI is not a merge, and in a stacked-PR workflow the merge order is a deliberate
human decision. *Poll continuously in a background task* — the wave boundary is the only moment the
answer changes anything, so polling anywhere else is cost without information.

**Amends ADR-0011** (the stacked-PR emit path gains its state-ingest counterpart) and **ADR-0022**
(the revalidation gate's `MERGED` precondition now has a producer).

---

## ADR-0035 — Acyclicity is asserted on the condensed graph, after `ATOMIC_WAVE` and `MANUAL` SCCs are contracted

**Decision.** The acyclicity invariant is stated over the **condensed** graph, not the raw one.
Order of operations: contract hoisting (ADR-0019) runs first, then edge-breaking; every SCC that
survives is resolved to `ATOMIC_WAVE` or `MANUAL` and is then **contracted to a single super-node**
carrying its `scc_id` (ADR-0026); the invariant asserted, in §12 and in the §13 failure row, is
`nx.is_directed_acyclic_graph(nx.condensation(G_rev))`. Wave layering runs over the condensed graph,
so an `ATOMIC_WAVE` SCC occupies exactly one wave slot as a unit.

**Rationale.** *Agent Recommendation* from the 2026-08-09 adversarial self-review. The prior criterion
demanded that the *raw* graph be acyclic, which is unsatisfiable for any fleet containing even one
genuine mutual-implementation cycle — that is, the run failed its own success criterion precisely
when the cycle machinery had worked exactly as designed and resolved the cycle to `ATOMIC_WAVE`. A
criterion that fires on correct behaviour is worse than no criterion, because the first response to it
is to disable it. An `ATOMIC_WAVE` SCC is not an unresolved cycle; it is a resolution that says "these
ship together", and contraction to a super-node is precisely how such a resolution enters a
topological order.

**Alternatives rejected.** *Drop the acyclicity assertion* — it is the one check that catches a
genuinely unordered graph, which is a real and silent defect. *Force every SCC to break* —
manufactures artificial edge removals for cycles that are legitimately atomic, corrupting the emitted
build order to satisfy a check. *Assert acyclicity on the raw graph but downgrade failure to a
warning* — a warning at 250 repos is noise, and the check stops distinguishing the correct case from
the broken one.

**Amends ADR-0019** (the cycle pipeline's terminal state is a condensed DAG) and **ADR-0018**
(advisory edges are excluded before condensation, unchanged).

---

## ADR-0036 — Schema migrations run only under `fleet migrate-db`; workers refuse a version mismatch; resume compares MAJOR only

**Decision.** Migrations execute in exactly one place: the `fleet migrate-db` command. **Never
implicitly at orchestrator or worker startup.** The command takes `BEGIN EXCLUSIVE`, **re-reads
`PRAGMA user_version` inside that transaction**, applies only the ordered steps above the value it
reads, sets the new version, and commits — so a second process that raced to the same conclusion
outside the transaction finds nothing left to do. Every other entry point reads `user_version` at
startup and **refuses to start** on mismatch: exit 2, with the remediation (`run fleet migrate-db`)
in the message. Resume compatibility compares only the **MAJOR** component of `harness_version`; a
patch or minor bump no longer invalidates an in-flight run. This ADR carries `SCHEMA_VERSION` 6 → 7
as a single migration step, `v007_logical_keys.py`, folding every DDL change from ADR-0025 …
ADR-0035 into one version bump rather than eleven.

**Rationale.** *Agent Recommendation* from the 2026-08-09 adversarial self-review. Implicit migration
at startup means N workers racing to migrate the same file, and the check-then-migrate window between
reading `user_version` outside a transaction and applying DDL inside one is real and hit — the second
worker re-applies a step the first already committed, and the failure surfaces as a corrupt schema
rather than as a lock error. Re-reading the version *inside* `BEGIN EXCLUSIVE` closes the TOCTOU
outright. Refusing to start on mismatch is the loud-failure counterpart (`CLAUDE.md` Rule 11): a
worker that quietly runs against an older schema writes rows that the newer code cannot read.
Comparing the full `harness_version` on resume was over-strict to the point of hostility — every patch
release invalidated every in-flight run, which is the strongest possible incentive never to patch.

**Alternatives rejected.** *Migrate at startup under a lock* — the lock is the same `BEGIN EXCLUSIVE`,
but attaching it to startup means every worker carries migration code and any of them can run it.
*One version bump per ADR (6 → 17)* — eleven migration files that must be applied in order for a
schema no deployed run has ever used; the single `v007` step is auditable in one read.
*Auto-migrate with a `--no-migrate` opt-out* — makes the dangerous behaviour the default.
*Compare the full `harness_version` on resume* — see above.

**Amends ADR-0004** (`user_version` handling) and **ADR-0012** (the resume contract's version
comparison is MAJOR-only).

---

## ADR-0037 — Bazel version resolution is real Minimal Version Selection: the minimum version satisfying **all** specs

**Decision.** `bazel/module.py` resolves a `WorkspaceDep` version by **Minimal Version Selection**:
collect every declared requirement on that module across all ingested repos, and select the
**lowest version that satisfies every one of them**. If no version satisfies all specs, that is a
`severity='error'` collision finding naming each conflicting spec and its declaring repo — never a
silent pick. The resolved set is what `MODULE.bazel` records.

**Rationale.** *Agent Recommendation* from the 2026-08-09 adversarial self-review. The spec previously
specified "the highest version satisfying the most specs", which is a **plurality vote**, and a
plurality vote can — by construction, not by accident — select a version that **violates a declared
upper bound** held by a minority of repos. That is a silent correctness break: the build succeeds, the
constraint that existed to prevent a known incompatibility is discarded, and nothing reports it.
Choosing real MVS also aligns us with `bzlmod`, which implements MVS itself, so our precomputed
`MODULE.bazel` and Bazel's own resolution agree rather than disagreeing in a way that surfaces as a
confusing post-ingest version drift.

**Alternatives rejected.** *Highest-wins / always-latest* — maximally likely to violate an upper bound
and to import behaviour changes nobody asked for during a migration, which is the worst possible time.
*Plurality vote* — the defect being fixed. *Defer resolution entirely to `bzlmod` at build time* —
attractive, but the pre-ingest collision report exists to surface conflicts *before* 250 repos are in
one tree, and a build-time failure at that point is far more expensive to diagnose. *Auto-relax the
lowest upper bound to force a solution* — silently discards a declared constraint; a `severity='error'`
finding puts the choice in front of a human, which is where an unsatisfiable constraint set belongs.

**Amends ADR-0007** (bzlmod version resolution semantics) and **ADR-0020** (the `EcosystemAdapter`
supplies specs; `module.py` resolves them).

---

## ADR-0038 — Observability carve-out: `fleet status --metrics` is an output, not a service

**Decision.** The "no daemon, no service, no long-running process, no port" non-goal **stands
unchanged**, and one explicit carve-out is written next to it so the boundary is not re-litigated per
feature. `fleet status --metrics` computes a **metrics projection** from SQLite and emits it twice: as
JSON on stdout, and as a Prometheus **text-format file** at `artifacts/metrics/fleet.prom`, suitable
for a node_exporter textfile collector or any scraper the operator already runs. The command binds no
socket, opens no port, forks nothing, and exits. It is an output artifact in the same category as
`migration_state.json` and `artifacts/diffs/`.

**Rationale.** *Agent Recommendation* from the 2026-08-09 adversarial self-review. The non-goal was
being read as "the harness must not be observable", which conflates *operational surface* with
*visibility*. What ADR-0004's zero-daemon posture actually buys is: nothing to supervise, nothing to
restart, no port to secure, no lifecycle to get wrong. A file written by a command that exits costs
none of that and still lets an operator watch a multi-hour 250-repo run on infrastructure they already
have. Naming the carve-out explicitly is the point — an unstated boundary gets eroded one reasonable
exception at a time, and this one is now the only exception, with the reason it qualifies stated
alongside it.

**Alternatives rejected.** *An embedded HTTP `/metrics` endpoint* — a port, a bind address, a
lifecycle, a shutdown path, and an auth question; the textfile collector already solves this for
batch jobs. *A Prometheus push gateway* — outbound network from the harness plus a credential to
manage and redact. *No metrics at all* — a run can occupy hours across 250 repos, and "is it making
progress" answered only by tailing JSONL is a real operational gap. *A `--watch` mode that loops* —
that is a long-running process wearing a flag.

**Amends ADR-0004** (the zero-daemon non-goal gains one named, bounded carve-out) and **ADR-0012**
(the observability artifact set gains the metrics projection).

---

## ADR-0039 — A reservation is a row, not a number: per-holder identity behind `reserved_usd`, and legacy aggregates are adopted rather than freed

**Decision.** `SCHEMA_VERSION` 7 → 8 adds a `reservations` table — `reservation_id` (PK, minted by
the reserver), `run_id`, `repo_id`, `phase`, `lease_fence`, `amount_usd`,
`state IN ('HELD','SETTLED','EXPIRED')`, `expires_at`, `created_at`, a composite FK to `phases`, and
`ix_reservations_expiry (run_id, state, expires_at)` for the reaper's sweep. `reservation_id` is a
**required** argument on the nested reserve and settle primitives, so no hold is unattributable.
`reserved_usd` on both ledgers **remains the enforced aggregate** — the fail-closed guard has to be
one statement — and the per-row table is what *explains* it; `reservation_expires_at` on both ledgers
becomes **derived** (`MIN(expires_at)` over surviving HELD rows) and is no longer authoritative.
Settlement is guarded on the amount (`ABS(amount_usd - :amt) <= :epsilon`) so per-row and aggregate
cannot drift, and a settle against a reaped hold is a typed `ReservationRefusedError`.
`phase`/`lease_fence` are **nullable as a pair**, CHECK-enforced. The 7 → 8 step
(`v008_reservations.py`) is additive and adopts each `repo_ledger` row with `reserved_usd > 0` as
exactly **one** legacy HELD row; where a v7 run aggregate exceeds its repos' sum the residue stays
held. The normative rules live in **SPEC §6 RESERVATION ACCOUNTING** and are not restated here.

**Rationale.** *Agent Recommendation*, originating in our own v8 implementation work — no reference
or external requirement asks for this. §6's own normative rule said the reaper "releases any
reservation past `reservation_expires_at` in the same transaction that bumps the owning
`phases.lease_fence`", and against v7 that sentence was **not implementable**. Both ledgers carried
a scalar `reserved_usd` and a single `reservation_expires_at` that every reserver overwrote: the
pair records *that* money is held and nothing about *whose*. So "release the expired reservation"
could only mean "release the whole aggregate", which zeroes every live worker's hold alongside the
dead one's, under-counts what the run has committed, and lets it overspend the ceiling ADR-0028
exists to enforce. The mirror-image failure was already live in the other direction: an orphaned
hold was never released at all, so `reserved_usd` ratcheted upward until a healthy run halted on a
ceiling it never spent. One row per holder turns the release into `SUM(amount_usd)` over exactly the
expired rows — a fix that is arithmetic rather than heuristic. Keeping the aggregate as the *enforced*
number rather than recomputing it per reservation preserves ADR-0028's single-statement CAS, which is
the property that makes the ceiling a constraint instead of a report. The nullable owner pair is what
makes an unowned hold **recordable**: a pre-v8 aggregate and a scan-time dispatch have no phase lease,
and a hold that cannot be written down is a hold the reaper cannot see. It is still reapable by
expiry; it simply has no fence to bump. Half-set is the one shape forbidden, because a phase with no
fence is an owner the reaper cannot invalidate.

**Alternatives rejected.** *Zero the aggregate at migration* — frees money live workers are still
spending, then lets them settle on top of the freed balance; that under-counts commitments and
overspends, which is the exact defect this ADR closes. *Leave the pre-v8 aggregate unattributed* —
the ratchet survives on precisely the databases that already have it, since the reaper can never
touch dollars no row claims. *Adopt one row per in-flight worker* — invents an owner v7 never
recorded, and makes the reaper bump the fence of a phase that never held the money. *Give the run
ledger its own adopted rows* — double-counts, because a run dollar is the nesting of a repo dollar;
leaving the excess held is fail-closed instead, since `MAX(reserved_usd - …, 0.0)` releases at most
what is attributed. *Derive `reserved_usd` from the rows on every read* — a `SUM` inside the
admission CAS, which is the read-then-write ADR-0028 refuses. *Keep `reservation_expires_at`
authoritative and add the rows alongside* — two spellings of one fact, drifting apart under
concurrency; a single column cannot represent N expiries and the last writer's value is not the one
the reaper needs.

**Amends ADR-0028** (its "every reservation carries a `reservation_id` and a `lease_expires_at`" is
now a schema fact rather than a convention, and expiry is per-row) and **ADR-0036** (the ladder
gains its eighth rung, `v008_reservations.py`).

---

## ADR-0040 — The forge is a `Forge` protocol with a real Gitea driver; its token is passed by `curl -K` file, never by argv

**Decision.** `vcs/forge.py` defines `Forge`, a `runtime_checkable` `Protocol` of exactly the five
methods the harness calls — `available`, `create_pr`, `mark_ready`, `view`, `sync` — plus the
shared `PrStatus` / `PrSyncItem` types and a `ForgeError(GitError)` root so `prwriter`'s existing
failure mapping catches one type for both drivers. `edit_body` is deliberately **not** on the
Protocol: no caller invokes it. `vcs/gitea.py` `GiteaForge` is a second implementation against
Gitea's `/api/v1`, selected by the config key `pr.forge` through the single factory
`vcs.build_forge`; `github` remains the default and `gitea` requires `pr.forge_url`,
`pr.forge_owner` and `pr.forge_token_config` or the run refuses to start.

The driver shells `curl` through the existing `CommandRunner` seam rather than importing an HTTP
client, and **the token is passed in a `curl -K` config file** — `argv()` is
`(curl, "-sS", "-K", <config>, *args)` and holds no credential. Request bodies go to a `mkstemp`
file (0600 at creation, deleted in a `finally`) for the same reason `github.py` uses `--body-file`.

Two Gitea behaviours were established **by experiment against the live instance**, not from docs,
and both are load-bearing:

* **`draft` is a title, not a field.** `POST …/pulls` with `{"draft": true}` and a plain title
  returns `"draft": false` — the flag is ignored — while a title beginning `WIP:` returns
  `"draft": true`. So `create_pr(draft=True)` prepends `WIP: ` (idempotently) and never sends a
  `draft` key; `mark_ready` reads the live title and PATCHes the prefix off.
* **A merged PR reports `state: "closed"` with `merged: true`.** `parse_pr_json` therefore reads
  `merged` **first** and only then falls back to `state`. Read in the other order, every landed
  dependency classifies as `CLOSED`, which releases none of the three gates that consume
  `PrState.MERGED` and reinstates exactly the wave-0 deadlock ADR-0034 and SPEC §3.4 step 5 exist
  to prevent.

**Rationale.** *Agent Recommendation*, originating in our own implementation work — no reference
document and no external requirement asks for a Gitea driver; this arose because the operator's
forge is a self-hosted Gitea and the harness had no PR path against it at all. SPEC §3.4 is written
against "the forge", but the code had `GitHubCli` wired directly into `workers/prwriter.py` and
`cli._pr_impl`, which is the concrete-vendor coupling CLAUDE.md guardrail 3 forbids.

*Why an adapter and not a base-URL swap.* `gh` is a GitHub API client, not an HTTP client: it
speaks `/repos/{o}/{r}/pulls` with GitHub's field names, GitHub's draft semantics and GitHub's
merge representation, none of which Gitea reproduces. Pointing it at `/api/v1` would not have
produced wrong URLs so much as wrong *readings* — and the two divergences above are the proof:
a base-URL swap would have created every draft as a non-draft and reported every merged PR as
`CLOSED`. A defect that stalls the fleet at the first wave boundary is not one to discover by
configuration.

*Why `curl` through `CommandRunner`.* The seam already carries what a fresh HTTP path would have to
re-earn: the deadline, the process-group kill, `attempts` recording, and §11.4 redaction. Adding
`httpx` would mean a second, parallel plumbing with its own timeout and cancellation story, for a
driver that issues fewer than a dozen requests per run — and it would have solved none of the
token problem. The dependency set stays at what `pyproject.toml` already declares.

*Why the token is a file.* `attempts.command` persists argv verbatim (`state/schema.sql`,
`repository.record_attempt`) and `WorkerError.stderr_tail` can quote a command line, so
`-H "Authorization: token …"` would write the credential into the state database in cleartext and
into any log that echoes a failing command. `curl` reads `-K` from disk after `execve`: the token
is in no argv, no environment variable and no exception message. **Known limit, recorded rather
than papered over:** the harness does not create or `stat` that file, so "mode-600" is an operator
convention the code never enforces — see the Gitea row in `docs/INTEGRATION_HONESTY.md`.

**Alternatives rejected.** *Point `gh` at Gitea's API* — see above; wrong readings, not wrong URLs.
*Add an HTTP client dependency* — a second subprocess-less seam with its own timeout, cancellation
and redaction, duplicating the one that works. *Put the token in an environment variable* — better
than argv and still wrong: it is inherited by every child of the runner and appears in `/proc`.
*A `--forge` CLI flag* — forge choice is a property of the deployment, and a flag makes it a
per-invocation accident that half a fleet can disagree about. *Send `{"draft": true}` and trust it*
— measured false. *Read `state` and treat `merged` as a detail* — the wave-0 deadlock. *Make Gitea
the default* — it would silently change the forge for any existing operator; opt-in with a
startup-time validation is the fail-loud shape.

**Amends ADR-0034** (its "polls the forge" is now a `Forge` protocol call with two implementations,
not a `gh` invocation) and **ADR-0023's** precedent for protocol-over-registry is followed rather
than re-argued.

---

## ADR-0041 — `build.ruleset_versions` is a true pin: `single_version_override` beside every `bazel_dep`

**Decision.** `bazel/generators.render_module_bazel` emits, for every ruleset the adapters name, a
`bazel_dep(name, version)` **and** a `single_version_override(module_name, version)` carrying the
same version from `build.ruleset_versions`. A validated conflict-resolution override for the same
module wins over the configured pin, because Bazel rejects two `single_version_override`s for one
module. The config key keeps its name and its documented meaning.

**Rationale.** *Agent Recommendation*, originating in our own implementation work — this was found
by running real Bazel for the first time, not read anywhere. `bazel_dep(version = X)` is an MVS
*lower bound*, not a pin. Real Bazel selected `rules_python@1.7.0` for a configured `1.0.0`,
because a transitive module in BCR declares a higher floor. §9's reproducibility guarantee is that
two runs of the same fleet build the same bytes; a floor that any unrelated BCR publication can
raise does not provide it, and the monorepo's Python rules would change under the fleet with no
diff anywhere in this repository. Note what the alternative would have cost: `settings.py` called
these "PINS" and `generators.py` said "an unpinned `bazel_dep` is a build that resolves differently
on the next run" — both were *documentation of an intent the code did not implement*, and the
offline tests compared our own strings to our own strings, so nothing could see it.

ADR-0037 is not contradicted. MVS remains how the *artifacts* — the third-party coordinates each
repo requires — are selected, and there is still no "pick a winner" branch in the generator. This
ADR pins only the **rulesets**, which are harness-chosen infrastructure rather than fleet-derived
requirements, and for which "whatever BCR floats to" is not an answer.

**Alternatives rejected.** *Rename the key to `ruleset_version_floors` and document the MVS
behaviour* — the cheaper edit, and the wrong one: it would make the documentation true by making
§9's reproducibility guarantee false, and §9 is a requirement rather than a description.
*Emit `single_version_override` only where a conflict was validated* — that is what the code did,
and it is why the defect existed. *Pin with `bazel_dep(version)` plus `--check_direct_dependencies`*
— a warning flag is not a constraint, and it says nothing about transitive floor-raising.
*Accept the floor and record the selected version in the run digest* — makes the drift auditable
after the fact instead of preventing it; the fleet still builds different bytes on Tuesday.

**Consequence, recorded because it is the point of a real pin:** with the version no longer free to
float, the configured version is the version that actually runs — and **five of the eight configured
rulesets turned out not to load** under Bazel 9.2, which is defect **D8** in
`docs/INTEGRATION_HONESTY.md`, where the cause and the resolution are stated. The pin did not
create those incompatibilities; it stopped hiding them. **Since 2026-08-11 the pins are corrected
and a standing test loads every one of them under the real binary**, so this ADR's cost — a pin
that can be wrong — is now paid by a test rather than by a run.

**Amends ADR-0037** (its MVS scope is narrowed to artifact coordinates; rulesets are pinned).

---

## ADR-0042 — Relocation is applied as an idempotent mapping: Phase 2 owns the tree, Phase 3 owns the history

**Decision.** `cli._prepare_build` applies §3.3 step 1's relocation as a **mapping that is idempotent
on a path already sitting at its image**, by passing git-filter-repo two ordered `--path-rename`
rules: `':<dest>/'`, which roots every historical path at the destination, then
`'<dest>/<dest>/:<dest>/'`, which collapses the one prefix that was already rooted there. The
phase split is unchanged and is the reason this shape is necessary: **§3.2 step 1 owns the
worktree move** (`workers/relocate.py` commits the renames) and **§3.3 step 1 owns the history
rewrite**. Phase 2's rename commit maps to `<dest>/x → <dest>/x`, becomes empty and is pruned; the
result is one uniformly relocated history whose tip is exactly the tree Phase 2 produced.

**Rationale.** *Agent Recommendation*, originating in our own implementation work. The clone handed
to git-filter-repo has **mixed** history: every commit behind Phase 2's relocation commit still
carries repo-root paths, while the tip already carries `<dest>/…`. A single catch-all
`--path-rename ':<dest>/'` re-roots both, and the tip lands at `<dest>/<dest>/…` — observed as
`py/acme_lib_py/py/acme_lib_py/pyproject.toml` with every state row green, the merge real and the
provenance trailers correct. Rule 2 cannot misfire on a path that merely *looks* relocated, because
Phase 2 refuses to run at all when a tracked source already sits under `<dest>/`
(`relocate._plan_matches_tree`), so inside this clone `<dest>/` is Phase 2's work and nothing else.

Doing it as a mapping rather than as a conditional is the substance of the decision: a rule set
that is a no-op on its own output can be re-applied by a retry, a resume, or a second ingest of
the same repo without a caller having to know which of those it is — which is the same
re-runnability property ADR-0014 asks of every step.

**Alternatives rejected.** *Drop the `--path-rename` entirely* — the tip would be correct and every
commit behind it would keep its pre-migration path, losing the `git log --follow` continuity that
is the entire reason §3.3 rewrites history rather than copying a tree. *Move the relocation wholly
into Phase 3 and stop committing renames in Phase 2* — Phase 2's tree move is what the rewrite
workers and the build worktree read; deleting it to fix a Phase 3 argv trades a two-rule mapping
for a re-architecture of two phases. *Detect the already-relocated tip and branch on it* — a
conditional that is correct only for the states someone enumerated, versus a mapping that is
correct by construction. *Add an idempotency guard inside `relocate()`* — attractive, and still
**not done**: `relocate()` remains non-idempotent by itself, so this ADR's correctness rests on a
caller convention. That is a real weakness and it is carried as such in the `git-filter-repo
idempotency` row of `docs/INTEGRATION_HONESTY.md`, not hidden here.

**Amends ADR-0014** (re-runnability of the ingest step is now a property of the rename mapping, not
only of the rmtree-and-re-clone).

---

## ADR-0043 — Lock files are *resolved* by a real resolver, behind a strict carry-over precedence

**Decision.** Every file a generated `MODULE.bazel` names but no source repo supplies is a declared
`SupportFile` owned by the adapter that names it, and it is produced by exactly one of three
mechanisms, tried in this order and never blended:

1. **carry** — if the source repo ships the file, it is taken **byte-for-byte** and **no resolver
   runs**;
2. **resolve** — otherwise `EcosystemAdapter.resolution()` names a real resolver
   (`uv pip compile` for Python, `pnpm install --lockfile-only` for JS), which the driver runs as
   **argv, never a shell string**, in a scratch directory whose inputs the adapter also declares,
   through the `cli.RESOLVER_RUNNER` seam (guardrail 3: the driver depends on a `CommandRunner`,
   not on `uv`);
3. **floor** — a synthesized, deliberately poor, *parseable* placeholder, used only where no
   resolver is declared.

A resolver that fails is a loud `DependencyResolutionError` and a `DependencyResolutionFailed`
finding naming the command (Rule 11). **There is no silent fall-through from 2 to 3** — that
fallback is the defect this ADR exists to prevent, not a robustness feature.

**Rationale.** *Agent Recommendation*, originating in our own implementation work. The harness had
**never run a resolver**: it synthesized a lock from Phase 1's declared specs, and a spec list is
not a resolution — it has **no transitive closure**. Bazel's hubs were therefore empty of everything
a direct requirement pulls in, which is what `no such package '@@rules_python++pip+pypi//certifi' …
referenced by '@@rules_python++pip+pypi_312_requests//:pkg'` was saying. A synthesized lock is a
file shaped like an answer, and every offline test that read one was checking our own arithmetic.

Carry outranking resolve is the load-bearing half. **A repo that ships a lockfile has already made
a decision, and re-resolving it silently overrides that decision with today's index state** —
which is a migration changing what the code depends on while claiming only to move it. Byte-for-byte
carry also means the common case costs no network and is trivially reproducible.

Determinism is asserted, not assumed: resolution is byte-identical on re-run, which required
`--no-header` — uv's banner embeds the scratch path, so two identical resolutions differed in bytes
for a reason that had nothing to do with dependencies. That is the same class as ADR-0041's
floating pin: a reproducibility claim that only an actual second run can falsify.

**Alternatives rejected.** *Keep synthesizing and widen the specs* — no amount of widening produces
a transitive closure; only a resolver knows it. *Require every repo to ship a lock* — the fleet is
250 repositories we do not control, and a migration that refuses the ones without a lock migrates
nothing. *Run the resolver always, ignoring a shipped lock* — deterministic in the wrong direction:
it makes the migration's output depend on the day it ran. *Fall back to the floor when the resolver
fails* — indistinguishable from success at every downstream layer, which is precisely the
"empty hub" failure this ADR is undoing. *Call `uv` in-process* — a resolver is a subprocess with a
deadline and evidence, and the seam is what makes the state machine testable without a network.

**Consequence, recorded because it narrows the sandbox story:** resolution is a **network**
dependency at build time, so ADR-0010's `--network=none` container cannot wrap a build whose lock
was neither carried nor pre-resolved. Verdicts and the untested edges are in
`docs/INTEGRATION_HONESTY.md`; they are not restated here.

---

## ADR-0044 — Bazel's exit code is the verdict; the taxonomy is reproduced against the real binary

**Decision.** `workers/buildverify.py` classifies a Bazel step by **exit code**, from a table whose
every row was reproduced against the vendored `tools/bin/bazel` rather than quoted from memory
(0, 1, 2, 3, 4, 8, 9, 36 — the table itself lives beside the constants in that module and is not
duplicated here). Three classes come out of it: exit 4 (`NO_TESTS_FOUND`) is **success** — a green
build with nothing to run; exits 8/9/36 are **environmental** and spend no ADR-0014 attempt; and
exits 2 and 127 are **unrepeatable** and terminate without charging the ladder. Everything else
follows the existing failure path.

**Rationale.** *Agent Recommendation*, originating in our own implementation work — found by running
real Bazel, not read anywhere. A library with no tests of its own is ordinary at fleet scale, and
§3.3's criterion (`bazel build` exit 0 **and** `bazel test` exit 0) was never about an empty test
set. Before the taxonomy existed, exit 4 fell through to a retryable `TEST_FAILURE`: a repo that
built perfectly burned all three ADR-0014 rungs — each of them an LLM-bearing repair attempt on a
tree with nothing wrong with it — and escalated to `REQUIRES_HUMAN_INTERVENTION`, taking its
dependents down with it.

**The reason it must be the code and not the message.** `bazel test` prints *"No test targets were
found, yet testing was requested"* in **both** the empty-test case and the failed-build case, and in
the latter it exits **1**. **Exit 1 dominates exit 4**: exit 4 is emitted only when the build that
preceded it was green. So the message is ambiguous in exactly the situation that matters and the
code is not — "the tree is fine, there was simply nothing to run" is a mechanical fact read off an
integer, not an inference from prose. This is the same discipline as `references/`' constraint 3
(classify the payload, not the exception type), arriving at the opposite-looking conclusion for the
same reason: **use whichever channel is unambiguous**, and for a build tool that is the exit code.

Reproducing the table rather than citing it is the other half of the decision, and it paid
immediately: it exposed exit 2 (`COMMAND_LINE_ERROR`) being retried three times with **byte-identical
argv**. A repair prompt cannot fix the harness's own command line; three identical attempts are
three chances spent learning the same thing.

**Alternatives rejected.** *Match on stderr text* — the string is ambiguous (above), locale- and
version-sensitive, and every match would be a new guess. *Treat exit 4 as a failure and require
every repo to have a test* — imposes a policy on 250 repositories we did not write, in order to
avoid one integer comparison. *Treat any non-zero exit as retryable* — the status quo ante, and it
is what burned the ladder on environmental faults and malformed argv alike. *Ask the model to
classify the failure* — Rule 5: an exit code is deterministic routing, and no judgment call exists
here to spend a model on.

**Amends ADR-0014** (the attempt ladder is now charged only for failures a different attempt could
plausibly repair; environmental and unrepeatable exits terminate without consuming a rung).

---

## ADR-0045 — A module extension tag creates *repositories*, plural: `repo_names` is a collection

**Decision.** `ToolchainRequirement.repo_names` is a **`list[str]`**, and `render_module_bazel`
unions it into the `use_repo(...)` set for the extension's proxy. There is no singular `repo_name`
field, and the plural is not a convenience — it is the schema stating a fact about Bazel.

**Rationale.** *Agent Recommendation*, originating in our own implementation work, and established
by `bazel mod show_repo` rather than assumed. **One tag call routinely creates several importable
repositories.** A single `python.toolchain()` yields `@python_3_12`, `@python_3_12_host` **and**
`@pythons_hub`; rules_ts's `ext.deps` creates `npm_typescript`. A repository the root module never
imports is not a warning — it is `no such package '@@[unknown repo 'npm_typescript' requested from
@@]//'` at load time, for every repo in the fleet.

A singular field would have been the obvious modelling choice, would have fixed the observed
symptom, and would have been **the same defect with a smaller blast radius**: correct for
`rules_ts`, wrong for `rules_python`, and wrong in a way that only surfaces the next time somebody
adds a toolchain. Modelling the multiplicity in the type means the failure mode cannot recur by
omission — a new adapter either lists its repos or lists none, and neither is a half-populated
scalar.

The same "the name is not the thing" trap sits one field over and is resolved the same way: a
`load()` label carries an **apparent repo** name, not a module name (`rules_go` publishes
`@io_bazel_rules_go`), so the ruleset set derived from `load_from` labels is intersected with the
pinned `build.ruleset_versions` table instead of being emitted as a `bazel_dep` verbatim. Both are
one lesson: **Bazel's naming layers are not interchangeable, and a generator that assumes they are
produces a module that looks right and does not load.**

**Alternatives rejected.** *`repo_name: str` plus a second `extra_repos` list* — two fields with one
meaning, and the first call site to set only the scalar reintroduces the defect. *Derive the repo
names from the ruleset by convention* — there is no convention; which names a ruleset exports is
per-ruleset knowledge, which is why it belongs on the adapter's declaration. *Emit `use_repo` for
every repository the extension creates, discovered at render time* — the generator does not run
Bazel and must not start; discovery would make rendering depend on a network fetch.

---

## ADR-0046 — A cross-repo import becomes a *language name*, never a Bazel label; and first-party linking is a `workspace_deps(unit)` concern

**Decision.** Two changes, one lesson.

1. `EcosystemAdapter` declares **`import_specifier(coordinate, dest) -> str`**, `@abstractmethod`,
   answering *"what does source code in another unit write to import this one after migration"*.
   It is rendered into Phase 2's rewrite params as `{{import_specifier}}` beside `{{dest_path}}`
   and `{{repo_id}}`. It may never return a Bazel label, and
   `test_a_cross_repo_import_becomes_a_language_name_and_never_a_bazel_label` asserts that over
   every `Ecosystem` member.
2. **`workspace_deps` takes the `BuildUnit`**, not a `Sequence[Coordinate]`, and
   `BuildUnit.internal_deps` carries `InternalDep(label, dest, published)` rather than a bare
   label string.

**Rationale.** *Agent Recommendation*, forced by a real `tsc`:
`ts/acme/app/src/main.ts(1,29): error TS2307: Cannot find module '//ts/acme/lib:lib' or its
corresponding type declarations.` A Bazel label had been written into a TypeScript source by a
rewrite rule. §3.2 step 2's rewrite targets are *import paths, package declarations, `tsconfig`
path aliases, Go module paths, Python module paths* — language names, all of them; Bazel labels
describe the same dependency edge one phase later, in §3.3's `BuildTarget.deps`, in a notation no
compiler reads. The rule is config and the label was the operator's, but the harness had made
nothing else expressible: `{{dest_path}}` and `{{repo_id}}` are a directory and an id, and a
directory plus a target name **is** a label. A defect a rule author is steered into is a harness
defect.

The second half is what made the first half insufficient. Correcting the specifier to `'@acme/lib'`
alone does not build: rules_js resolves a first-party package through a `node_modules` link created
by `npm_link_all_packages()` from a `link:`/`workspace:` entry in the lockfile, and an adapter
handed only `external_coordinates` cannot name a sibling at all — the internal/external split, by
construction, hands it the half that excludes them. Nor can the missing half be recovered from
`internal_deps`: `//ts/acme/lib:lib` does not spell `@acme/lib`, and guessing is how `@acme/lib`
and `@corp/lib` become one package. So the unit is handed over whole, and `JsAdapter` declares each
sibling as `link:<dest>` in the resolver's root manifest, emits `npm_package(name = "pkg")` as the
target the generated link resolves against, and gets `//:node_modules/@acme/lib` out of the same
`workspace_deps` → `external_labels` path that already produced `//:node_modules/left-pad`.

**Alternatives rejected.** *Map the specifier with `tsconfig` `paths` into the sibling's `.d.ts`* —
smaller, and it works, but it is a per-consumer file rewrite that reimplements module resolution
beside the one rules_js already performs, and it says nothing for any other language. *Add first
party linking as a `JsAdapter`-only side channel (a `first_party_deps()` hook)* — the gap is in the
shared method's signature, and a parallel channel leaves the next ruleset that links first-party
packages (rules_go's `go_deps`, `crate.from_cargo`'s path dependencies) to discover it again.
*Make the siblings pnpm `workspace:` members* — that makes each a lockfile *importer*, which
rules_js answers with a required `.bazelignore` line and an `npm_link_all_packages()` call per
package; `link:` keeps the monorepo root the single importer this dialect has always resolved.
*Leave `import_specifier` concrete with a sensible default* — the whole failure was a language
inheriting an answer that was not its own, so the method is abstract and a new `Ecosystem` must
speak for itself.

---

## ADR-0047 — The parse probe scans for `ERROR` nodes; an ast-grep exit code answers "did anything match", never "did it parse"

**Decision.** `AstGrepRewriter`'s parse probe runs `ast-grep scan --inline-rules <doc>`, where the
document is a single rule of **`kind: ERROR`** carrying **`severity: error`**, and reads the verdict
off the **exit status**: **1** = the file contains `ERROR` nodes, so it does not parse and the probe
returns `False`; **0** = none found, so it parses. Three parts of the same decision:

1. **`probe_text(path, text)` is what the pipeline injects.** The `TextProbe` seam is
   `(path, text) -> bool`; `parse_probe(path)` cannot satisfy it, so in production
   `FilePatch.parse_probe_ok` was **always `False`**. `probe_text` stages the buffer in a scratch
   directory and probes that copy — the same staging discipline the driver's `apply` already uses.
2. **`apply_patch` probes `str(repo.path / patch.path)`**, not the repo-relative `patch.path`. It
   resolves against the git handle that actually wrote the file, so an injected `git=` is honored.
3. **A target that is not on disk returns `False` and does not raise.**

**Rationale.** *Agent Recommendation*, originating in our own implementation work — established by
running the vendored `tools/bin/ast-grep` **0.45.1**, not read anywhere.

The previous probe ran `ast-grep run --pattern '$A' --lang <l> <path>` and returned its exit code.
Measured: `const = = ;` → **0**; `function f( {` → **0**; `class {{{ ???` → **0**; and a **valid but
empty** `.ts` module → **1**. Python behaves identically (`def f(:` → 0, empty → 1). tree-sitter
error-recovers and wraps the garbage in `ERROR` nodes, which `$A` matches, while a valid empty file
gives `$A` nothing to match. **The exit code was encoding "did anything match", so broken output
passed the gate and valid output failed it** — the probe was not weak, it was inverted, and the
module docstring asserted the opposite of what the binary does. It had been in the tree for five
checkpoints, and nothing found it until the real binary was run against it.

`kind: ERROR` asks the parse question directly, and **`severity: error`** is what puts the answer in
the exit code: a bare scan reports its findings and still exits 0.

The second part is the same class of failure one layer up. `apply_patch` called `probe(patch.path)`
while holding a `worktree`, so the repo-relative path was resolved against the process cwd;
`ast-grep scan … missing.ts` prints `ERROR: No such file or directory` and exits **0**, which the
gate read as "no `ERROR` nodes" and returned `True`. **A safety gate that passes silently when
pointed at nothing is worse than no gate**, because the pipeline records `parse_probe_ok=True`.

**Alternatives rejected.** *Read `--json` and check the match array is empty* — the obvious version,
and it loses on the failure that matters: `ProcResult` exposes only `stdout_tail`, truncated to
**32 KiB**, so a badly mangled file's match array arrives as unparseable JSON exactly when the file
is at its most broken. An exit code cannot be truncated, and ast-grep's codes are **disjoint** — 1
for a matched `severity: error` rule, 8 for an unusable rule document — so a broken tool cannot
masquerade as a broken file. *Keep `--pattern '$A'` and invert its sense* — a valid empty module
exits 1, so inversion trades every false pass for a false failure and blocks correct rewrites.
*Raise `EngineUnavailableError` when the probe target is missing* — `cli._transform_criterion`
(`cli.py:4149-4153`) buckets that exception into `parse_probe_unavailable`, which renders a
non-blocking warning and adds **no** violation; raising would have routed a vanished file to a pass
just as silently as the defect being fixed. Fail closed is the only option that changes the outcome.

**A measured limit, recorded rather than papered over.** `kind: MISSING` is **rejected outright by
0.45.1** — exit 8, `Cannot parse rule` — so a file that error-recovers into a `MISSING` token with
**zero** `ERROR` nodes (an unclosed brace can do this) still reads as parsing. It is a false pass,
not a false failure: it defers to the Phase 3 build gate rather than blocking a correct rewrite.
*Agent judgement:* the tradeoff was accepted rather than worked around, because the only way to ask
0.45.1 the `MISSING` question is a rule document the binary refuses to load. It is named in
`docs/INTEGRATION_HONESTY.md`'s `ast-grep` row so it is a known gap and not an assumption.

**Amends ADR-0013.** Its Phase 2 clause — *"the transformed tree parses (`ast-grep` exits 0 on a
parse probe for every touched file)"* — is now true of the implementation and not only of the
sentence. The wording needs no change; for five checkpoints the code beneath it exited 0 on
`const = = ;` and 1 on a valid empty module.

> **Measured correction (2026-08-16) — the full exit-code table is recorded as evidence; one
> line-number citation has rotted; and a proposed correction to this entry was tested and
> REFUTED.**
>
> **Mechanism, and why this one.** A dated in-place `Measured correction` blockquote, **not** an
> `**Amended by ADR-00NN**` pointer. This file uses both and they are not interchangeable: the
> note under ADR-0053's consequence 2 states the rule outright — "Amended by" is for *a decision
> later work replaced*, and a measured blockquote is for *a statement about a mechanism*, on
> ADR-0050's precedent. Nothing below replaces a decision. Items 1 and 4 are **evidence**; item 3
> is a **citation that drifted**; item 2 is a **refutation of a claim that was never true**, which
> is precisely ADR-0053's case. The one thing ADR-0067 genuinely *amends* here is flagged
> separately, underneath this blockquote, in the file's usual form.
>
> **1. The full table, measured.** Every row is a real invocation of the vendored
> `tools/bin/ast-grep` (`--version` → **`ast-grep 0.45.1`**) as
> `ast-grep scan --inline-rules <doc> <target>`, with the document `_probe_document` actually
> emits — `yaml.safe_dump({id, language, rule: {kind}, severity}, sort_keys=True)` — not a
> hand-typed approximation of it. Recorded because it is cheap to keep and expensive to re-derive,
> and because every future change to `_scan_for_error_nodes` is a change to how these eight
> numbers are read:
>
> | case | exit | note |
> | --- | --- | --- |
> | valid `.ts`, `kind: ERROR` + `severity: error` | **0** | "it parses" — the pass verdict |
> | file containing `ERROR` nodes (`const = = ;`) | **1** | "it does not parse" — the fail verdict |
> | valid but **empty** module | **0** | the row the old `--pattern '$A'` probe got backwards (it exited 1) |
> | **target file missing from disk** | **0** | plus `ERROR: nope.ts: No such file or directory (os error 2)` on **stderr**. The exit code does not carry it, which is why the driver guards this with its own `exists()` check and returns `False` — the fail-closed case this entry's third decision item exists for |
> | rule **without** `severity: error` | **0** | findings still print, as `help[p]:` rather than `error[p]:`. The verdict is simply **not in the exit code** — this row is the whole argument for `severity: error` |
> | malformed YAML rule document | **8** | `Error: Cannot parse rule INLINE_RULES` |
> | `kind: MISSING` | **8** | *same code, same message*; cause chain ends `Kind `MISSING` is invalid` |
> | unknown CLI flag | **2** | clap usage error, `unexpected argument … found` |
>
> **2. "A semantically invalid rule exits 101, and 8 is reserved for unparseable YAML" is FALSE.**
> This entry and the `_ERROR_NODES_FOUND` docstring both say **8**, and **both are correct as
> written** — the claim is recorded here only so it is not re-raised. Measured: `kind: MISSING`
> exits **8**, byte-identically to malformed YAML, and so does `kind: NOSUCHKIND`, an unknown
> `language:`, and a document missing its `rule:` key. **8 is ast-grep 0.45.1's code for "this
> rule document is unusable", full stop** — it does not distinguish a YAML parse failure from a
> schema failure, and **no invocation attempted here produced 101 at all**. What is true is the
> conclusion this entry draws from the number, and it is unaffected either way: the tool-failure
> codes (**2**, **8**) are **disjoint from 1**, so a broken tool cannot masquerade as a broken
> file.
>
> **3. The citation `cli.py:4149-4153` in "Alternatives rejected" has rotted.** The
> `except EngineUnavailableError` that buckets into `parse_probe_unavailable` is now at
> **`cli.py:4162-4164`**; lines 4149–4153 today are the `unresolved_files` clause. The *claim* is
> still true — grep `except EngineUnavailableError as exc:` in `_transform_criterion` — only the
> coordinates moved. Cite the symbol, not the line.
>
> **4. A probe killed at its deadline does not exit 124, and any future fix keyed on 124 will
> catch only half the cases.** `util/proc.py` returns the child's **real negative signal code**;
> `TIMEOUT_EXIT_CODE = 124` is synthesised **only** on the never-started path. Measured directly
> against `fleet.util.proc.run`:
>
> | condition | `exit_code` | `timed_out` | `started` |
> | --- | --- | --- | --- |
> | killed at the deadline (`SIGTERM` honoured) | **-15** | `True` | `True` |
> | killed at the deadline (`SIGTERM` ignored, then `SIGKILL`) | **-9** | `True` | `True` |
> | deadline had already passed at call time | **124** | `True` | **`False`** |
>
> So `exit_code == 124` selects the **never-started** row and misses both kill rows — and note
> that **-9 is reachable as well as -15**, so a fix keyed on `-15` is wrong in the same way. The
> only sound predicate is the pair of flags, which is what `_scan_for_error_nodes` already reads
> and what ADR-0067 lifts into `util/proc.no_verdict`.

**Amended by ADR-0067 — "raising routes a vanished file to a pass" will stop being true of every
raise, once ADR-0067 ships.** Nothing about the probe document, the `kind: ERROR` reading, the
`severity: error` argument, the `probe_text` seam, or the `MISSING` limit changes; the measured
`MISSING` gap (item 2 above) explicitly **stands**. What ADR-0067 narrows is this entry's
"Alternatives rejected" premise that `EngineUnavailableError` *always* lands in a non-blocking
`parse_probe_unavailable` warning. Once ADR-0067 ships, that will be true only of a **genuinely
absent binary**; a probe that ran and produced no verdict will raise `ProbeIndeterminateError` and
become a **violation**. **As of `68a41ff` this is undelivered** — see ADR-0067's status line —
so today `EngineUnavailableError` still covers all three causes (missing binary, never-started
probe, killed-at-deadline probe) and all three still land in the non-blocking warning this
sentence describes as narrowed. The third decision item — a missing target returns `False` rather
than raising — is **unchanged and still right**, for the reason given here: exit **0** on a
missing file (table row 4) means fail-closed is the only safe direction.

---

## ADR-0048 — The root lockfile is a pnpm *workspace* with one importer per JS repo; a flat union keys on package name and silently drops the loser

**Decision.** The monorepo's root `pnpm-lock.yaml` is resolved **once, as a pnpm workspace**, from a
`pnpm-workspace.yaml` that lists **one package directory per JS repo**, each with its own
`package.json`. The flat per-repo union is replaced. Three parts:

1. **One importer per JS repo.** `_root_package_json` stops emitting a single flattened dependency
   map for the whole fleet and emits one manifest per unit, under that unit's `dest`. The root
   resolve produces ONE `pnpm-lock.yaml` carrying a separate `importers:` entry per repo.
2. **The root `pnpm-lock.yaml` drops `carry_from`.** With ≥1 importer it is a resolution of the
   *workspace*, which no single repo's lock is.
3. **Python is decided the other way, on its own evidence** — one `requirements.lock`, one `@pypi`
   hub, unchanged (see *Python* below). This is not the JS result generalized.

**The defect.** `_module_inputs` (`cli.py:5868`) unions the fleet's root files with
`support.setdefault(file.path, file)` (`cli.py:5934`) over `sorted(plans)` — **first `repo_id`
wins**. Two JS repos each declaring `//:pnpm-lock.yaml` means one repo's external dependency is
silently discarded; real Bazel then fails analysis with `no such target '//:node_modules/ms'`, while
`fleet build` **exits 0**, because the losing repo's wave had already settled and nothing re-admits a
settled wave. There is a **second, independent half**: the `_carried` short-circuit
(`cli.py:5857`) skips the resolver entirely when a repo ships its own lock, and `js.py:521-525`
declares `carry_from` for the root path — so two repos each shipping a *real* lock collide the same
way, before any resolver runs.

Flattening loses just as quietly one layer down, measured on the real function: `js.py:239-249`
builds `dependencies` as a **dict keyed on package name**, so unioning two units overwrites in
place. `_root_package_json([app ms@^2.0.0, report ms@^2.1.3])` emits `{"ms": "^2.1.3"}` — `^2.0.0`
is gone, with no error. `_all_external` (`js.py:175`) sorts by `(key, version_spec)`, so the
survivor is the **lexicographically largest specifier**, which has no semantic meaning.

**Rationale.** *Agent Recommendation*, established by running real **pnpm 10.16.1**, not read
anywhere. `pnpm install --lockfile-only --ignore-scripts` over a `pnpm-workspace.yaml` listing one
package dir per repo was measured to preserve what the flat union destroys:

- Three importers declaring `ms@^2.0.0`, `ms@^2.1.3` and an exact `ms@2.0.0` each kept **their own
  `specifier:` verbatim**.
- `ms@2.0.0` and `ms@2.1.3` **coexisted** in `packages:`/`snapshots:` with distinct integrity
  hashes. Nothing was discarded.
- Two independent resolves produced **byte-identical** output, so §11.6 byte-determinism holds.
- A genuinely unsatisfiable range fails **loudly** and writes no lock:
  `ERR_PNPM_NO_MATCHING_VERSION No matching version found for ms@^99.0.0`, exit 1, naming the
  offending **importer path** — i.e. attributable to a repo, which is what Rule 11 needs.

**An honest limit, stated rather than implied.** pnpm still dedupes *within* a satisfiable range: an
importer declaring `^2.0.0` alongside one declaring `^2.1.3` resolves to `2.1.3`. **Only exact pins
survive verbatim.** What multi-importer buys is that each repo's declared **specifier** is preserved
and that **no repo's declaration is silently overwritten by another's** — not that every repo gets
its own resolved version.

**This does not violate the `carry_from` invariant.** Dropping `carry_from` on the root
`pnpm-lock.yaml` does not contradict `models/build.py:148` — *"`carry_from` outranks `content`,
always"*. That sentence ranks a carried lock against a **synthesized** one. With ≥1 importer the
root lock is neither: it is a resolution of the workspace, which no single repo's lock is. This is
`models/build.py:190`'s documented **`carry_from` → `Resolution` → `content`** precedence selecting
the **middle** term, which exists for exactly this case.

**Alternatives rejected.** *Write a second lockfile resolver that textually merges per-repo locks* —
rejected, because the merge would have to recompute **peer resolution**: `snapshots:` keys are
peer-suffixed (rules_js's own lock carries **43**, e.g. `'@babel/cli@7.28.3(@babel/core@7.28.5)'`),
so a merger must know which suffixed snapshot each importer receives — that is re-implementing
pnpm's solver. Worth recording precisely: integrity hashes alone *would* be mechanically
preservable, since `packages:` entries are keyed `name@version` and carry `resolution: {integrity:
…}` verbatim, so a union over disjoint keys needs no refetch. **That is not the hard part.**
`js.py:503`'s *"nothing in this harness can reconstruct one"* therefore stands — no contradicting
evidence was found. *Keep the flat union and make the collision loud instead* — turns a silent wrong
build into a hard failure, which is better, but it fails every fleet where two repos legitimately
want different versions, and pnpm already resolves that case correctly. *One `@pypi`-style hub per
JS repo* — rejected on the same ground as ADR-0046's parallel-channel alternative: it multiplies the
resolve when the ruleset already models exactly this shape.

**Make-or-break compatibility — verified against the ruleset source, NOT against a green build.**
`aspect_rules_js@3.4.0` — the version `src/fleet/settings.py:517` pins — consumes multi-importer
locks natively: `npm/private/npm_translate_lock_generate.bzl:207-225` gates
`npm_link_all_packages()` on the package being the pnpm root **or a workspace importer**, and
`package_to_importer` (`:83-86`) maps every importer path to a Bazel package. The ruleset's own repo
is this shape — **14 importers**, with `chalk` at `5.1.1` in `.` and `5.0.1` in
`npm/private/test/npm_package`. **The limitation is explicit: this is a reading of the ruleset
source, not a passing Bazel build.** Implementation must produce the green build before this ADR's
compatibility claim is more than a source reading.

**Consequences.**

1. Dependency labels become **`//<dest>:node_modules/<pkg>`**, not `//:node_modules/<pkg>`.
2. `.bazelignore` needs **one line per importer**: `npm_translate_lock_helpers.bzl:612-621` requires
   `<importer>/node_modules`, and a bare `node_modules` line does **not** cover it.
3. The root `npm_link_all_packages()` **stays**, because stores are emitted under `if is_root:`
   (`npm_translate_lock_generate.bzl:418`).
4. Three passages of prose in `src/fleet/ecosystems/js.py` are **falsified and must be corrected
   during implementation** (not edited here):
   - **`js.py:222-227`** — the docstring's rationale for `link:` over `workspace:`. Its stated
     reason (`workspace:` "makes it a second lockfile importer", which is presented as the cost)
     **inverts**: one importer per repo is now the decision, and the extra `.bazelignore` line and
     `npm_link_all_packages()` call are the price of correctness, not an argument against it.
     `link:` **itself still needs no change**, because `workspace:*` records `version:
     link:../lib` anyway.
   - **`js.py:514-515`** — *"this dialect still resolves exactly one importer"*. It resolves N.
   - **`js.py:534-537`** — the justification for the single `node_modules` line in `.bazelignore`.

**Python, decided separately and on its own evidence.** No multi-importer equivalent exists:
`pip.parse(requirements_lock = "//:requirements.lock")` (`py.py:174`) builds **one** `@pypi` hub, and
a requirements file is a flat set with one `==` per distribution. Python therefore **inherently
forces one version per package fleet-wide** — that is a property of `pip.parse` plus the requirements
format, *not* an inference carried over from the JS result. But Python's resolver input does **not**
silently drop: `_requirements_text` (`py.py:326-349`) builds a set of full spec strings, so
`urllib3<2` and `urllib3>=2.2` both survive into `requirements.in` and real `uv pip compile` fails
loudly — `No solution found when resolving dependencies … unsatisfiable`, exit 1. The **carry** half
of the defect *does* apply identically to `//:requirements.lock` (`py.py:180-210`), and a carried
lock never reaches `uv`, so that collision is silent and must be fixed with the JS one.

**Amends ADR-0046.** Its rejected alternative *"Make the siblings pnpm `workspace:` members"* was
declined on the premise that *"`link:` keeps the monorepo root the single importer this dialect has
always resolved"*. That premise no longer holds — the root is now a workspace with N importers — but
ADR-0046's **decision** is untouched: `import_specifier` stays abstract, `workspace_deps` still takes
the `BuildUnit`, and first-party siblings are still declared `link:<dest>`.

---

## ADR-0049 — `//:requirements.lock` is carried at **exactly one** contributing repo and **resolved** at two or more; Python is not pin-preserving and must not pretend to be

**Decision.** `PyAdapter.workspace_files(units)` declares the root `//:requirements.lock` with:

1. **`carry_from` set only when exactly one unit contributes** — the candidates
   `<dest>/requirements.lock`, `<dest>/requirements.txt` of that one repo (`py.py:236-245`).
2. **`carry_from` empty at two or more contributors**, which forces the `Resolution` — one real
   `uv pip compile` over the union of every contributing unit's `external_coordinates`
   (`py.py:252`).
3. **`content` unions too** — the synthesized floor is `_requirements_text` over the concatenated
   coordinates of every contributor, not of one.

**The defect this closes.** `workspace_files` returned a **single** root `SupportFile` whose
`carry_from` named only the **lexicographically-first** `dest`. `cli._carried` (`cli.py:5781`)
short-circuits the resolver whenever a `carry_from` candidate exists in the merged tree, so when
that one repo shipped a lock, **its single-package lock became the whole fleet's**: the other
repo's distributions were absent from the `@pypi` hub, and `fleet build` **exited SUCCESS**.
Measured on `[acme-app-py, acme-metrics-py]`: `py/acme-metrics-py` sorts before `py/acme_app_py`
(`-` < `_`), so `requests` vanished. **Which repo won was an artifact of string ordering** — not of
any property of the repos.

`RootFileConflictError` (`cli.py:4488`) does not catch this and structurally cannot:
`_fleet_support_files` (`cli.py:5926`) hands **every plan of one ecosystem the identical tuple**,
so there is no byte divergence left for `_module_inputs` to detect. The union guard defends the
root against *two adapters disagreeing*; it says nothing about *one adapter computing the wrong
single answer*. This is the same defect as ADR-0048's, in its **carry** half rather than its
content half, and it is worse in one specific way: the JS half produced a Bazel analysis error
(`no such target '//:node_modules/ms'`) that a real build reported, while this one produced a
**green build against a lock that was silently the wrong repo's**.

**Rationale for the one/two split.** *Agent Recommendation.* A carried lock is honoured because
**it is a resolution of exactly the set it must cover**. With one contributor that is true by
construction: the fleet's Python coordinates *are* that repo's coordinates, so the repo's own lock
is a resolution of the union, and re-resolving it would move versions the repo pinned and tested
against — the precise harm `carry_from`'s precedence exists to prevent (ADR-0043). With two or
more it is **false by construction**: one repo's lock is a resolution of a strict subset, and
promoting it to the root silently deletes the other repos' distributions. The predicate is
therefore "does this file resolve the whole union", and `len(contributors) == 1` is that predicate
computed exactly, not a heuristic.

**Dropping the carry is the `Resolution` middle term, not a violation of `carry_from` precedence.**
`models/build.py:148` — *"`carry_from` outranks `content`, always"* — ranks a **carried** lock
against a **synthesized** one, and that ranking is untouched here: at two or more contributors the
root lock is neither. It is a resolution of the *union*, which no single repo's lock is. That is
`models/build.py:190`'s documented **`carry_from` → `Resolution` → `content`** precedence selecting
its **middle** term, which exists for exactly this case. Identical reasoning to ADR-0048's §2, and
deliberately so: the two ecosystems reached the same rule from the same argument about what a root
file *is*, which is the part that generalizes — not the pnpm-specific mechanism, which does not.

**Python cannot be made pin-preserving, and this ADR does not attempt it.** ADR-0048 bought JS a
per-repo `importers:` entry with each repo's `specifier:` preserved. **No Python equivalent
exists**, and the reason is structural rather than a gap in this harness:
`pip.parse(requirements_lock = "//:requirements.lock")` (`py.py:174`) builds **one** `@pypi` hub,
and a requirements file is a **flat set with one `==` per distribution**. One version per package,
fleet-wide, is a property of `pip.parse` plus the requirements format. Stating it plainly is the
point: a reader who has just read ADR-0048 would otherwise expect the JS answer to carry over, and
it does not.

**A genuine conflict must fail loudly, and does.** `_requirements_text` (`py.py:367`) builds a
**set of full spec strings**, not a name-keyed map — which is exactly what `js.py`'s root
`dependencies` dict was and why *that* one overwrote in silence. So `urllib3<2` and `urllib3>=2.2`
both survive into `requirements.in`, both reach real `uv pip compile`, and it fails
`No solution found when resolving dependencies … unsatisfiable`, exit 1. *Agent Recommendation:*
this is the **correct** outcome and no reconciliation should be added. Two repos that pinned
genuinely incompatible ranges have a real disagreement that a single `@pypi` hub cannot represent;
any automatic winner would be the ADR-0048 defect re-introduced deliberately, and the failure is
per-repo, classified and attributable, which is what Rule 11 requires.

**Evidence.** The union resolve was run, not reasoned about. The generated root lock, verbatim:
`certifi`, `charset-normalizer`, `idna` (`# via requests`), `jinja2` (`# via -r requirements.in`),
`markupsafe` (`# via jinja2`), `requests`, `urllib3`. **`markupsafe` and `certifi` are named by no
manifest in the fleet** — they are the transitive closure, which is the one thing a promoted
single-repo lock structurally cannot contain, and therefore the assertion that distinguishes a real
resolve from the defect. Held by
`test_a_second_python_repo_revokes_the_carry_and_forces_a_union_resolve` and, terminating in Bazel
rather than in a file, `test_two_python_repos_with_different_pypi_dependencies_both_build`
(`bazel build //...` over the `integration` branch, exit 0).

**Alternatives rejected.** *Keep `carry_from` at N contributors and merge the carried locks* —
rejected: merging two `==`-pinned locks requires re-resolving every transitive edge, which is
`uv`'s job, and the harness would be writing a second resolver (ADR-0048 rejected the pnpm
equivalent on the same ground). *Keep the carry and raise when two repos both ship locks* — turns
a silent wrong build into a loud failure, which is better, but it fails the common and legitimate
case where two repos' pins are perfectly compatible and `uv` would resolve them in one pass.
*Emit one `requirements.lock` per repo and one `pip.parse` hub each* — rejected on ADR-0046's and
ADR-0048's shared ground: it multiplies hubs to work around a resolve the tool already performs,
and `@pypi//<pkg>` labels would stop being fleet-unique.

**Consequences.**

1. A single-Python-repo fleet is **unchanged** — same carried lock, same bytes, no resolver run.
   The mechanical widening of `workspace_files` to `Sequence[BuildUnit]` was verified byte-identical
   for the single-unit path across all six ecosystems before this rule was added on top.
2. Adding a second Python repo to a fleet **revokes the first repo's carried lock**. That is a
   visible behaviour change for an operator and it is intended: the alternative is the defect.
3. `base.union_workspace_files` (`base.py:182`) is the shared union for the four adapters that
   declare root files; the one/two carry rule is `py.py`'s own, because it is a statement about
   what a *requirements* lock covers.
4. **Unproven elsewhere.** `rust.py` (`Cargo.lock`) and `go.py` (`go.mod`) declare root files with
   the same shape and have **never** been run with two repos of their ecosystem. This ADR decides
   Python only; whether the same rule is right for a Cargo workspace is an open question, not an
   answered one.

---

## ADR-0050 — The Go root `go.mod` union is **deferred** until a root `go.sum` exists; the collision is real, and fixing it first would produce a claim no test in this project could check

**Decision.** `go.py`'s root-file handling is **left unchanged this round**. The Go root-file
collision is confirmed real, recorded here in full, and **not fixed**. Three parts:

1. **The collision is real and is the same defect as ADR-0048's and ADR-0049's** — one Go repo's
   `go.mod` silently wins the root, the other's requirements vanish from the module graph.
2. **The prerequisite is a root `//:go.sum`, and it does not exist anywhere in this project.**
   `go.sum` has **zero** occurrences across `src/`, `tests/` and `docs/` — grepped, not recalled.
   Without it `go_deps.from_file` cannot load a synthesized `go.mod` that carries any `require`.
3. **Deferring is the decision, not the oversight.** Writing the union first yields a root file
   that is more correct on paper and **equally unloadable** in Bazel, and — see below — **no
   offline test in this suite could tell the two apart**, because nothing here runs Gazelle.

**The collision, read off the code.** `GoAdapter.workspace_files` (`go.py:120`) delegates to
`base.union_workspace_files` (`go.py:135`), whose merge is first-writer-wins —
`merged.setdefault(file.path, file)` over units sorted by `(dest, unit_id)` (`base.py:200-202`).
The per-unit contribution (`go.py:137-148`) is a single root `//:go.mod` whose `content` is
`_go_mod_text(unit)` (`go.py:192`), rendering `module <that one unit's path>` (`go.py:209`),
`go 1.23.4` (`go.py:211`, from `_GO_VERSION`, `go.py:34`) and a `require (…)` block built from
**that one unit's** `external_coordinates` (`go.py:203`). With two Go repos the second unit's
`go.mod` is discarded in silence: the root declares one repo's module path and one repo's
requirements, and repo B's dependencies **do not exist in the module graph at all**.

`RootFileConflictError` (`cli.py:4488`) structurally cannot fire on this, for exactly the reason
ADR-0049 records: `_fleet_support_files` (`cli.py:5926`) hands **every plan of one ecosystem the
identical tuple**, so `_module_inputs` (`cli.py:5977`) sees no byte divergence. The drop happens
**inside the adapter, upstream of the guard**. This is the third instance of one shape, and the
guard has now failed to catch all three.

**What the correct shape would be — stated so the deferral is not mistaken for indecision.** One
root module whose `module` path is the **monorepo's own**, with both repos as **packages inside
it**, and the `require` blocks **unioned**. The supporting facts, read from bazel-gazelle's own
`go_deps` extension source: `deps_from_go_mod` returns `(module, deps, replace_map, tools)` and
the `module` value is consumed **only as the main module path**, while the `require` list is what
becomes the `@com_github_…` repos. So unioning the requires is **mandatory** — it is the only term
that produces repo B's dependencies — and the `module` line being one repo's path is the lesser
half of the defect. Two further constraints:

- **Only one `go_deps.from_file` tag is permitted per module**; a second makes gazelle fail with
  `Multiple "go_deps.from_file" tags defined in module`. The fleet survives this today only
  because `render_module_bazel` dedupes identical tag text —
  `block += sorted(set(grouped[(ruleset, var)]))` (`bazel/generators.py:826`). That is a
  coincidence of the renderer, not a property anyone designed for, and it is load-bearing.
- **Import paths are not hostage to the `module` line.** `# gazelle:prefix <published path>` is
  written per package (`go.py:169`, rendered by `render_gazelle_build`,
  `bazel/generators.py:202-213`), so each repo keeps compiling from its own directory under a
  monorepo-rooted module. Gazelle also requires `go >= 1.17` in the `go.mod`; the pinned `1.23.4`
  passes.

**The limitation on the paragraph above, stated rather than implied.** Those are readings of
bazel-gazelle's source, in the manner ADR-0048 read `aspect_rules_js` — **not** a passing Bazel
run. And unlike the rules_js case, the source is not even in this host's Bazel repository cache:
`sums_from_go_mod`/`deps_from_go_mod` return **zero** hits across the whole cache, and no
bazel-gazelle archive is in it, because **nothing in this project has ever fetched gazelle**.

**The blocker.** `go_deps.bzl` calls `sums_from_go_mod` whenever the `go.mod` carries any
`require`, and that function reads a **`go.sum` sitting beside the `go.mod`**. A **synthesized**
union `go.mod` can never have a matching `go.sum` — the sums are content hashes of module zips
that only a resolver can produce. So `go_deps.from_file` cannot load it. **This gap already exists
for today's single-repo carried case**; the union does not create it. What the union does is make
it **unavoidable**, because at one contributor a carried repo's own `go.sum` could in principle
be carried alongside its `go.mod`, and at two it cannot.

**Why nothing in this project can currently observe a Go build — the reason "just write the fix"
is the wrong call.** `uses_gazelle = True` (`go.py:68`) makes `generate_targets()` (`go.py:150`)
and `test_targets()` (`go.py:155`) return `[]` unconditionally. The generated Go `BUILD.bazel` is
`render_gazelle_build` output: a header plus `# gazelle:` **directive comments and zero targets**
(`bazel/generators.py:208-213`, whose docstring says so). `settings.py:563`'s
`gazelle_binary = "//:gazelle"` is **referenced by nothing else in `src/`** — there is no
`bazel run //:gazelle` call site anywhere in this codebase. Therefore a test mirroring
`test_two_js_repos_…` / `test_two_python_repos_…` for Go would assert a `BUILD.bazel` exists and
then run `bazel build //...` over a tree containing **no Go targets**, and **pass vacuously**.
`docs/INTEGRATION_HONESTY.md` already carries this as UNPROVEN and states that nothing here
compiles a line of go; this ADR is the first place that says what the consequence is for
*fixing* the Go path: **a green Go two-repo test would be evidence of nothing.**

**Host reality, since it bounds what a resolver step could do.** `go` **1.22.2** is installed at
`/usr/bin/go`, but `go.py` pins SDK **1.23.4** for `go_sdk.download` — Bazel fetches that SDK, it
does not adopt the host's. `GOPROXY` is `https://proxy.golang.org,direct`: a **live fetch**, in
the same class as the live-registry fetches that produced this round's read timeouts (§19). The
one genuinely favourable fact: unlike Rust, whose `cargo` is **absent from this host entirely**, a
Go resolver **is** runnable here.

**The correct ordering.** *Agent Recommendation.* Three steps, in this order, and the first is not
skippable:

1. **A root `//:go.sum`** — carried at **exactly one** contributing repo (ADR-0049's rule, for
   ADR-0049's reason: that repo's sums are a resolution of exactly the set they must cover), and
   **unsatisfiable at two or more without a `Resolution`** running **`go mod download all`**
   (this entry originally named ~~`go mod tidy`~~ / ~~`go mod download`~~; **both are wrong** —
   see the measured correction below) **pinned to the declared SDK version** so the sums match
   the SDK Bazel will fetch.
2. **Then the `go.mod` union**, in the shape above.
3. **Then, separately, actually running Gazelle**, which is the only thing that can turn any of
   this from a source reading into a result.

> **Measured correction (2026-08-12) — step 1's resolver command was wrong, and is corrected in
> place above.** The original text named "`go mod tidy` / `go mod download`". **Both named
> commands are wrong.** This was measured, not reasoned: real `go 1.22.2` on this host, in a
> scratch directory holding a synthesized `go.mod` and **no `.go` source files** — which is the
> only shape `_run_resolution` ever produces, because it recreates the scratch directory empty
> and writes **only `plan.inputs`** into it (`cli.py:5819-5830`), and inputs are support files,
> never sources. The deferral itself is unaffected and still stands; what was wrong was the
> command the eventual `Resolution` was told to run.
>
> - **`go mod tidy` is catastrophic here.** It printed `go: warning: "all" matched no packages`,
>   **deleted the entire `require` block from the `go.mod`**, and wrote **no `go.sum` at all**.
>   That is `tidy` behaving correctly: with no `.go` files nothing is imported, so every
>   requirement is unused and is pruned. Run against the very root file this ADR is about, it
>   would leave a `module` + `go` line and nothing else. `_run_resolution`'s "exited 0 but wrote
>   no usable lock" guard (`cli.py:5852`) would then fire — loud, but only after the command had
>   already destroyed its own input.
> - **`go mod download` (bare) is incomplete, and fails *quietly*, which is worse.** It wrote a
>   `go.sum` containing only the `/go.mod` hash lines and **no `h1:` module-zip hashes**. That
>   file is worse than a missing one: it is non-empty, so the "exited 0 but wrote no usable lock"
>   guard **passes it**, and `sums_from_go_mod` — which is reading for the `h1:` zip hash — still
>   cannot use it. A silent half-answer is precisely the defect class the `Resolution` seam
>   exists to close.
> - **`go mod download all` is the correct command.** It wrote both the `h1:` and the `/go.mod`
>   line for every module, and the **full transitive closure**: a `go.mod` requiring only
>   `github.com/stretchr/testify v1.9.0` produced sums for **five** modules (`davecgh/go-spew`,
>   `pmezard/go-difflib`, `stretchr/objx`, `testify`, `gopkg.in/yaml.v3`), four of which the
>   `go.mod` never names. It left the `go.mod` **byte-identical** (`diff`, for bare `download`
>   and for `download all` alike): `-mod=readonly` has been the default since Go 1.16, so the
>   resolver **cannot** rewrite its input, and an unresolvable pin errors instead of being
>   silently relaxed.

**Supporting facts, all measured this round (2026-08-12), same host, same scratch-dir method.**
These bear on the `Resolution` step 1 calls for; `go.py` declares **no `resolution()` today**, so
none of them describe current behaviour.

1. **`go.sum` is byte-deterministic, which is what §11.6 requires of any file the fleet writes.**
   Two clean runs of `go mod download all` over the same `go.mod` produced **byte-identical**
   `go.sum` (`diff`); a run against a **cold, private `GOMODCACHE`** produced a file
   **byte-identical** to the warm-cache run; and rerunning in place over an existing `go.sum` left
   it unchanged (idempotent). Ordering is lexicographic by module path, then version, with the
   `h1:` line before the `/go.mod` line. So a resolved `go.sum` does not make the same plan render
   different bytes in two processes.
2. **The host SDK mismatch is survivable but unpinned — a named gap, not a solved problem.** Host
   `go` is **1.22.2** and `go.py` pins **1.23.4** (`_GO_VERSION`, `go.py:34`), which
   `_go_mod_text` writes into the `go` line. With `GOTOOLCHAIN=auto` (this host's `go env` value)
   `go mod download all` **silently downloads go1.23.4** and succeeds; with `GOTOOLCHAIN=local` it
   fails hard — `go: go.mod requires go >= 1.23.4 (running go 1.22.2; GOTOOLCHAIN=local)`, exit 1;
   with `GOTOOLCHAIN=go1.23.4` it is pinned exactly and succeeds. **`Resolution` has no `env`
   field** (`models/build.py:173` — `lock_path`, `argv`, `inputs`, `timeout_s`) and
   **`_run_resolution` never passes `env=`** (`cli.py:5834`), *despite* `CommandRunner` accepting
   one (`util/proc.py:115`). The consequence is exact: which SDK computes the sums depends on a
   host environment variable **no test in this project controls**. *Agent Recommendation:* the
   minimum honest fix is an `env` field on `Resolution` threaded through to the runner. The
   in-contract workaround `argv = ["env", "GOTOOLCHAIN=go1.23.4", "go", "mod", "download",
   "all"]` does work, at the cost of making the driver's not-installed error
   (`cli.py:5839`, which quotes `plan.argv[0]`) name **`env`** instead of `go`.
3. **`_go_mod_text` renders an unresolvable floor, so the `go.mod` input must be carry-only.**
   Absent versions default to `v0.0.0` (`go.py:202`), and `go mod download all` rejects that
   outright: `go: github.com/google/uuid@v0.0.0: invalid version: unknown revision v0.0.0`,
   exit 1. Worse, for this suite's own shared parameterized unit — `Coordinate(name="left-pad",
   version_spec="^1.3.0")`, `tests/test_ecosystems.py:903` — the renderer emits `left-pad
   ^1.3.0`, which is neither a module path nor a Go version, and `go` refuses to parse the file:
   `go.mod:6:2: malformed module path "left-pad": missing dot in first path element`, exit 1.
   *Agent Recommendation:* the `Resolution`'s `go.mod` input should therefore be **carry-only**
   (`carry_from` populated, `content` left empty), so a repo with no real `go.mod` trips the
   driver's own loud "neither a carried file nor a synthesized floor produced any content"
   failure (`cli.py:5824`) instead of a confusing `go` parse error about a file the harness
   invented.

   > **Amended by ADR-0051 — carry-only is retired; the `Resolution`'s `go.mod` input is now the
   > union itself.** *Amended rather than superseded, and the distinction is the point:* ADR-0050
   > decided **defer, in this order**, and its own **step 2 was then carried out** — this fact
   > expired by being *acted on*, not by being *reversed*. What changed underneath it is that the
   > two defects it measured were properties of the **renderer**, not of synthesis:
   > `_require_line` (`go.py:353`) now validates the module path and the version and raises
   > `GoModuleCoordinateError` (`go.py:92`) naming the coordinate, with an explicit `v0.0.0`
   > rejection; and `_go_requires` (`go.py:387`) **excludes** non-Go coordinates by type, which is
   > what removed the `left-pad ^1.3.0` case this fact cites. Carry-only and a unioned root file
   > were irreconcilable in principle (ADR-0051, *The crux*), so the input is `carry_from=[]`,
   > `content=_go_mod_text(requires)` — the same call `workspace_files` makes. The guard this fact
   > bought is genuinely given up; it is recorded as ADR-0051 consequence 1.
4. **`go.sum` must never be carried — `carry_from=[]`, always. This narrows step 1's carry half,
   and the conflict is stated rather than averaged:** step 1 above reads "carried at exactly one
   contributing repo … and a `Resolution` at two or more", by analogy to ADR-0049; on the
   evidence below the `Resolution` is the right answer at **every** count, including one, and the
   carry clause of step 1 should not be implemented. *Agent Recommendation:* follow
   `js.py`'s workspace-lock rule (`js.py:618-631` — the lock drops its `carry_from` because the
   file describes the whole workspace, not one repo) rather than `py.py`'s one-contributor rule
   (`py.py:196-211`). A `go.sum` is a list of content hashes valid **only** against the `go.mod`
   sitting beside it; carrying one repo's sums next to another repo's `go.mod` is wrong in both
   directions — missing hashes for what the `go.mod` requires, stale hashes for what it does not
   — and would **manufacture a checksum mismatch**, the one class of failure this ADR has already
   said the harness must never invent. Adopting the Python rule at one contributor would work
   today and become a silent regression the moment the `go.mod` union of step 2 lands, because
   the carried sums would then cover a strict subset of the unioned requires.

**Alternatives rejected.** *Write the union now and land the `go.sum` later* — rejected: it lands
a change whose only available verification is vacuous, and this project's own record (§18's D13
and D14, and `INTEGRATION_HONESTY`'s "third instance, same shape") is that a green result standing
in for a check nobody ran is the failure mode that has cost the most here. *Emit `go.sum` as a
synthesized floor the way `_go_mod_text` synthesizes `go.mod`* — rejected outright: a `go.sum`
line is a hash of a module zip, so a synthesized one is either absent or **wrong**, and a wrong
sum fails as a security check, which is the one class of failure that must never be invented by
this harness. *Drop the `require` block from the root `go.mod` so `sums_from_go_mod` is never
reached* — rejected: it loads, and it produces a module graph with **no external dependencies for
any repo**, which is a strictly worse silent drop than the one this ADR declines to fix.

**Consequences.**

1. **The Go root-file collision remains open and is now documented**, not fixed. Two Go repos in
   one fleet produce a root `go.mod` naming one repo's module path and one repo's requirements.
   Anyone reading `go.py` should read this ADR before "fixing" it.

   > **Amended by ADR-0051 — the collision is closed, by this ADR's own step 2.** *Amended, not
   > superseded, for the same one-line reason as fact 3: the deferral was honoured and then lifted
   > on schedule, so this entry is the record of a decision that was carried out.*
   > `GoAdapter.workspace_files` (`go.py:188`) no longer routes through
   > `base.union_workspace_files`; it renders **one** root file from two pure functions,
   > `_go_requires(units)` → `_go_mod_text(requires)` (`go.py:387`, `go.py:416`), with
   > `carry_from=[]`. The root now declares the monorepo's own module path
   > (`_MONOREPO_MODULE = "fleet.internal/monorepo"`, `go.py:47`) over **every** Go unit's
   > requirements, and no repo's requirements are dropped. The sentence "anyone reading `go.py`
   > should read this ADR before fixing it" still holds — they should read ADR-0051 next.
2. **ADR-0049 consequence 4 is narrowed for Go and stands for Rust.** Whether ADR-0049's
   one/two carry rule is right for a Cargo workspace is still open; for Go the rule is *probably*
   right and **provably unverifiable today**, which is a different status and is why this ADR
   exists rather than a Go clone of ADR-0049.
3. **`docs/INTEGRATION_HONESTY.md`'s count is corrected**: the fixture is **one** JVM repo
   (`acme-commons-java`) and **zero** Go and **zero** Rust repos. The earlier "exactly one repo
   for go, jvm and rust" overstated coverage for two ecosystems out of three.
4. **The blocking item is a `go.sum` resolver step, not a `go.mod` edit.** Any future task that
   opens with "union the Go root file" has the order backwards.
5. **The resolver command is `go mod download all`, and the two commands this entry originally
   named are disqualified for opposite reasons** — `go mod tidy` destroys the `go.mod` and emits
   no lock; bare `go mod download` emits a lock that passes the harness's emptiness guard while
   being unusable by `sums_from_go_mod`. Anyone implementing step 1 should treat the second as
   the more dangerous of the two.
6. **A resolved `go.sum` does not threaten §11.6.** Byte-determinism was measured across repeat
   runs, across a cold vs. warm module cache, and on rerun in place, so the eventual step 1 does
   not reintroduce the "same plan, different bytes" problem.
7. **`Resolution` cannot currently pin the Go toolchain, and that is now a named gap.** Until an
   `env` field exists (or the `env`-prefixed `argv` workaround is adopted), the SDK that computes
   the sums is decided by the host's `GOTOOLCHAIN`, which no test controls — an implementation of
   step 1 that ignores this is nondeterministic in a way this project's suite cannot see.
8. **None of this is a Go build.** These measurements are of `go` itself in a scratch directory.
   Gazelle still never runs, no Go fixture repo exists, and nothing here compiles a line of Go —
   the standing position of `docs/INTEGRATION_HONESTY.md` and of this ADR's own §"Why nothing in
   this project can currently observe a Go build" is unchanged.

---

## ADR-0051 — The Go root `go.mod` is the monorepo's own module over the **union** of every Go unit's requirements, and the `go.sum` resolver's input is that same union: one renderer, called twice

**Decision.** ADR-0050 **step 2** is implemented. `GoAdapter.workspace_files` (`go.py:188`) stops
delegating to `base.union_workspace_files` and renders **one** root `//:go.mod` from two pure
functions — `_go_requires(units)` (`go.py:387`) → `_go_mod_text(requires)` (`go.py:416`). Five
parts:

1. **The `module` line is the monorepo's own**, `_MONOREPO_MODULE = "fleet.internal/monorepo"`
   (`go.py:47`), never one contributing repo's path. ADR-0050 read this off bazel-gazelle's
   `deps_from_go_mod`, which consumes the value **only** as the main module path; a main module is
   never fetched, so the name has one job and it is to name a module that is not any repo's.
2. **The `require` block is the union**, sorted and deduped **on the rendered line, not on the
   module path**. Two repos pinning one module at different versions contribute **two** lines and
   **Go's own MVS** takes the higher. This harness reconciles nothing — the same
   "let the Go toolchain decide" rule `workspace_deps` already applied to a single file.
3. **`carry_from=[]` on the root `go.mod`**, for `js.py`'s `pnpm-workspace.yaml` reason in Go's
   dialect: the root file is a statement about the *fleet*, which no single repo's file is, and
   promoting one would additionally short-circuit `cli._carried` so **no resolver ran at all**.
   The repo's own `go.mod` is not lost; Phase 2 left it at `<dest>/go.mod`.
4. **`resolution()`'s input is the union itself** (`go.py:244`) — `carry_from=[]`,
   `content=_go_mod_text(requires)`, the *same call* `workspace_files` makes — replacing the
   carry-only input the seam shipped with in §21.
5. **Validate or fail loud, with a type filter in front of it.** `_require_line` (`go.py:353`)
   renders only what `go` accepts and raises `GoModuleCoordinateError` (`go.py:92`) **naming the
   coordinate** otherwise; coordinates whose ecosystem is not Go are **excluded**, not raised on.

`_sum_contributor` — §21's "pick the first contributing unit's `go.mod`" helper — is **deleted**.
The coupling ADR-0050 step 2 was told to revisit is not re-pointed; it no longer exists.

**What the union renders.** For a two-unit fleet:

```
// GENERATED BY fleet — the monorepo's own module over every Go repo's requirements.
module fleet.internal/monorepo

go 1.23.4

require (
	github.com/google/uuid v1.6.0
	github.com/stretchr/testify v1.9.0
)
```

**The crux, and why it forced the input to change.** §21's `Resolution` used a **carry-only**
`go.mod` input, on ADR-0050 fact 3's reasoning that the synthesized floor was invalid. A union is
**synthesized by construction**, so carry-only and the union are irreconcilable: sums resolved
from repo A's carried file, sitting beside a *unioned* root file, are missing a hash for every
other repo's module. Every hash in that file would be **individually correct** and the file **as a
whole wrong** — the checksum mismatch `workspace_files` explicitly refuses to manufacture,
arriving by a back door. Passing the union as the input makes "the sums hash the `go.mod` that
lands" true **by construction** rather than by a selection rule that has to be kept in step by
hand. This is what supersedes ADR-0050 fact 3 and closes ADR-0050 consequence 1; both are marked
in place there as **amended**, because ADR-0050's decision was *defer, in this order*, and the
order was followed rather than reversed.

**Why synthesis is now valid, which is what made that possible.** Real Go coordinates come from
the Go manifest parser, which already rejects any requirement whose version does not start with
`v` and takes module paths **verbatim** from a `go.mod` that `go` itself accepted. On top of that,
`_require_line` validates the module path (regex, plus the dot-in-first-element rule `go` names in
its own error) and the version (regex, plus an **explicit `v0.0.0` rejection** — the old default
for a coordinate whose manifest named no version, and the one `go` answers `unknown revision
v0.0.0` to). Both failures raise `GoModuleCoordinateError` naming the coordinate, so the error
arrives **at the harness, naming the offending dependency**, not inside `go` naming a file the
harness invented (Rule 11). Held by
`test_a_go_coordinate_that_cannot_be_a_require_line_fails_loudly` (`tests/test_ecosystems.py`).

**Why a non-Go coordinate is excluded rather than raised on — the one place this ADR does not
fail loud, stated rather than implied.** `cli._external_coordinates` re-reads **every manifest a
repo ships**, dispatching on the file and not on the unit's ecosystem, so a Go-primary repo that
also has a `package.json` carries **npm** coordinates in its `external_coordinates`. An npm
package is not a Go module in **any** rendering, and nothing about the Go module graph is lost by
excluding it — the JS adapter's own root files are where that dependency is expressed. That type
filter, not the validator, is what stops the `left-pad ^1.3.0` case ADR-0050 fact 3 cites; the
loud path is reserved for coordinates that **claim** Go and still cannot render. *Agent
Recommendation:* the split is deliberate — raising on a foreign-ecosystem coordinate would make
every polyglot Go repo a hard run failure for a dependency the fleet handles correctly elsewhere.

**Measured against real `go` — manually, in a scratch directory, and NOT in the suite.** The
emitted union file **loads**: `go list -m all` reaches `missing go.sum entry`, i.e. **past
parsing**, which is where every one of ADR-0050 fact 3's failures stopped. `go mod download all`
over it exits **0**, writes sums for the **full transitive closure of both** repos' modules, and
leaves the `go.mod` **byte-identical**. **Duplicate module paths at different versions are not an
error**: a file requiring `testify` at both `v1.9.0` and `v1.8.0` loads silently and MVS selects
`v1.9.0` — which is why part 2 emits both pins instead of picking one.

> **Caveat, recorded because it bounds the measurement.** These runs were made under
> `GOTOOLCHAIN=local` with the `go` directive **lowered to 1.21**, because the host SDK is
> **1.22.2** while the harness pins **1.23.4**. The file that was measured is therefore the
> emitted union with one line changed. Nothing was measured under the pinned SDK.

**The `Resolution.env` round, which landed immediately before this and is its prerequisite.**
ADR-0050 consequence 7 named the SDK pin as an open gap; it is now closed. `Resolution` gained an
`env: dict[str, str]` field (`models/build.py:214`) with `default_factory=dict`, so **one
resolver's environment cannot leak into another's**. The driver merges it as
`{**os.environ, **plan.env}` **at the call site** (`cli.py:5844`) — an **overlay, never a
replacement** — because `util/proc.run` **replaces** the child environment wholesale; forwarding
`plan.env` alone would launch `go` with no `PATH` and surface as "`go` is not installed on this
host". `go.py` declares `env={"GOTOOLCHAIN": f"go{_GO_VERSION}"}` (`go.py:308`) off the **existing
SDK constant** rather than a second copy of the version, so the sums are computed by the same SDK
Bazel will fetch. `py.py` and `js.py` resolve with an **empty** env, guarded by
`test_a_resolution_declares_no_environment_unless_it_needs_one` and
`test_the_python_and_js_resolvers_declare_no_environment` (`tests/test_ecosystems.py`).

**Alternatives rejected.** *Keep the carry-only input and union only the workspace file* —
rejected as the crux above shows: it produces individually-correct hashes of the wrong file, which
is strictly worse than no sums, because a checksum mismatch inside Bazel reads as a supply-chain
compromise. *Re-point `_sum_contributor` at whichever unit the union "mostly" came from* —
rejected: there is no such unit, and a selection rule kept in step by hand is precisely the class
of coupling ADR-0050 step 2 was told to revisit. *Deduplicate the union on module path and pick
the higher version in the harness* — rejected: MVS is Go's job, it was measured doing that job,
and a harness-side pick is a reconciliation `workspace_deps` forbids. *Raise on non-Go coordinates
for symmetry with `_require_line`* — rejected, see the type-filter section: the driver's
manifest-blind re-read makes foreign coordinates **normal**, not exceptional.

**Consequences.**

1. **The driver's "neither a carried file nor a synthesized floor produced any content" guard
   (`cli.py:5824`) is no longer reachable for `go.mod`.** A synthesized input always has content,
   so a Go repo shipping no `go.mod` at all no longer trips it — it trips nothing, and the union
   simply carries no line for a repo that declared no requirement. This is a real capability given
   up, in exchange for the crux above, and is recorded plainly rather than softened.
2. **`GoModuleCoordinateError` is NOT caught by the driver's per-ecosystem containment.**
   `_resolved_support_files` catches only `DependencyResolutionError` (`cli.py:5980`), so an
   unrenderable Go coordinate **propagates as a hard run failure** for the whole fleet instead of
   being attributed to that ecosystem's repos the way a resolver failure is. This is a **known
   containment gap, deliberately left** — the alternative was widening a catch clause in the same
   round that changed what it protects — and it is the top follow-up item in `docs/PROGRESS.md`
   §22.
3. **`go.sum` and the `Resolution` are gated on the union being non-empty, not on any unit having
   coordinates.** A fleet whose Go units declare **only non-Go** coordinates now gets a valid,
   require-less `go.mod` and **no `go.sum`** — `resolution()` returns `None` (`go.py:287`). Before
   the gate, `go mod download all` over a require-less `go.mod` writes no `go.sum` and the
   resolver would trip the driver's "exited 0 but wrote no usable lock" guard, reporting a
   resolver failure for a fleet whose Go repos genuinely have no Go dependencies.
4. **ADR-0050's fact 3 and consequence 1 are amended in place, not deleted.** The record of the
   deferral, its evidence and its ordering stands unedited; only its expiry is marked.
5. **None of this is a Go build, and the standing position does not move.** `uses_gazelle = True`
   (`go.py:136`) still makes `generate_targets()` (`go.py:311`) return `[]`, **gazelle still never
   runs**, there is still **no `bazel run //:gazelle` call site** anywhere in `src/`, there are
   still **zero Go fixture repos**, and **no real `go` executes inside the suite** — the
   measurements above are of `go` in a scratch directory, by hand. A Go two-repo Bazel test would
   still pass **vacuously**. What this ADR changes is what the harness can *express*, not what it
   has *proven*.

---

## ADR-0052 — An adapter that cannot render a coordinate raises an **ecosystem-neutral** error; the driver catches that neutral type and re-raises its own sibling of `DependencyResolutionError` under a **new** finding kind

**Decision.** ADR-0051 consequence 2 — the containment gap left deliberately open — is closed, in
the only shape §12.6 permits. Four parts:

1. **A neutral adapter exception.** `AdapterCoordinateError(ValueError)` is new in
   `src/fleet/ecosystems/base.py` (`base.py:77`) and re-exported from the package
   (`ecosystems/__init__.py`). `GoModuleCoordinateError` (`go.py:93`) **subclasses** it. Nothing
   in Go's message, its raise sites or its existing test
   (`test_a_go_coordinate_that_cannot_be_a_require_line_fails_loudly`, `tests/test_ecosystems.py`)
   changes — the class gained a base and nothing else. There was no neutral type to reuse:
   `base.py` owned exactly one exception, `RegistryNotDiscoveredError`.
2. **A driver-side sibling, not a widened catch.** `CoordinateRenderError(BuildStepUnavailableError)`
   (`cli.py:4489`) sits beside `DependencyResolutionError` (`cli.py:4470`) under the **same
   containment base**, so the containment is identical — the ecosystem's repos are abandoned, the
   fleet continues. `_fleet_support_files` catches the **neutral** type and re-raises this one
   (`cli.py:6027`), setting `__cause__` to the adapter's exception so the coordinate the adapter
   named survives into the report.
3. **A new classification bucket**, `"CoordinateRenderFailed"` (`cli.py:6908`), read off the
   **driver's own** exception type, following the established `<Thing>Failed` convention. The
   message names **both** the offending coordinate (the adapter's contribution) and the repos the
   render was being performed for (the driver's).
4. **A structural invariant that can see this class of defect**, which the existing §12.6 greps
   could not: `test_no_adapter_package_exception_is_named_outside_the_adapter_packages`
   (`tests/test_ecosystems.py`).

Affected repos land in `REQUIRES_HUMAN_INTERVENTION` with a finding, and the wave continues.

**The distinction this ADR exists to draw.** `go.py` raising on a coordinate that claims the Go
ecosystem and cannot become a valid `require` line is **correct** and is not what changed. What
changed is where that loudness landed. Because the per-ecosystem containment caught only
`DependencyResolutionError`, the Go error propagated out of `_fleet_support_files` and out of
`build`, so **one malformed coordinate in one repo cost every other ecosystem's repos their
build**. CLAUDE.md Rule 11 asks for the other shape — *mark the target repo as
`REQUIRES_HUMAN_INTERVENTION` and move to the next item*. **Failing loudly and failing globally
are different things**, and only the first was ever the requirement; the second was an accident of
which types the `except` clause listed.

**Why the driver catches the NEUTRAL type — the constraint that forced two classes where one
would do.** The obvious one-line fix is `except GoModuleCoordinateError` in `cli.py`. That is
**language knowledge in a driver**, which §12.6 (and §13 row 33) forbid: the CLI must not know
that an ecosystem named Go exists. It is also a defect that scales badly rather than an
inelegance — the next adapter with a grammar for a root file writes
`class FooCoordinateError(ValueError)`, reintroduces the original run-ending behaviour **silently**,
and every existing test still passes, because the second `except` clause is one nobody thinks to
add. Routing through a base that `base.py` owns puts the obligation on the adapter author (raise
the neutral type, or subclass it) where it is discoverable, instead of on a driver author who has
no reason to look. `ValueError` is retained in the lineage deliberately: `GoModuleCoordinateError`
shipped as one, and dropping it would break any caller that catches it, including tests written
before this round.

**Why a *sibling* driver type rather than passing the adapter's exception through.** The adapter's
exception is the wrong object for the driver's job in two ways. It knows the coordinate but not
the repos — the driver adds `members`, because the root file is **one file for the whole
ecosystem** and a render that fails fails for all of that ecosystem's repos at once, exactly as a
resolver failure does. And a `findings.kind` read off an adapter's class name would put an
ecosystem's vocabulary into the operator-facing taxonomy. Re-raising as
`CoordinateRenderError` with `__cause__` set keeps the adapter's message verbatim while leaving
every name the driver speaks ecosystem-agnostic.

**Why a new finding kind instead of reusing `DependencyResolutionFailed`.** The two say different
things to an operator, and the cheaper reuse would actively mislead. A resolution that failed is a
fact about **something out there**: a package index did not answer, or two specs cannot both hold.
A coordinate that cannot be rendered is a fact about **this fleet's own Phase 1 inventory** — it
names a module path or a version that cannot be written into the ecosystem's root file at all, so
**no resolver ever ran and no index was ever contacted**. Filing it as `DependencyResolutionFailed`
would send a human to a registry to debug a string that is sitting in one of their own manifests.
The `<Thing>Failed` naming convention is the existing one and is followed rather than invented.

**The invariant, and what it caught on its way in.** The scan AST-collects every class name ending
in `Error` declared anywhere in `manifests/` and `ecosystems/` **outside** their own `base.py`,
then greps **all** of `src/` outside those two packages for any mention of those names. This is
the one form of language knowledge the pre-existing §12.6 greps structurally **cannot** see: they
match `if …ecosystem ==` and `Ecosystem.<MEMBER>`, and `except GoModuleCoordinateError` matches
neither while being language knowledge in a driver just as surely. The scan needs no allowlist and
cannot be widened without **moving the class**, which is the point of asserting it structurally
rather than by file and line. It immediately did work: it forced **two `cli.py` docstring lines
written by this round's own implementer** to be reworded, because they named `Ecosystem.GO` and
the Go class by name — the invariant catching the change that introduced it.

**Verified by mutation, not by assertion alone.** Replacing the new `except` clause with a
non-matching type made **both** new e2e tests fail with the raw `AdapterCoordinateError` escaping
the driver; the clause was restored and the tests re-verified. Held by
`test_an_unrenderable_coordinate_is_contained_to_its_own_ecosystems_repos` and
`test_a_coordinate_render_failure_marks_its_repos_and_the_rest_of_the_fleet_builds`
(`tests/test_build_e2e.py`), plus
`test_an_adapters_unrenderable_coordinate_error_is_the_neutral_one_the_driver_can_catch` and the
invariant above (`tests/test_ecosystems.py`).

**Alternatives rejected.** *`except GoModuleCoordinateError` in the driver* — rejected, see above:
it is the §12.6 violation, and it is silently non-transferable to the next adapter.
*Make `GoModuleCoordinateError` a subclass of `DependencyResolutionError` and change nothing else*
— rejected: it buys the containment for free but asserts something false, that a package index was
consulted, and it inverts the dependency by putting a driver type in an adapter's lineage.
*Reuse the `DependencyResolutionFailed` bucket with the neutral class* — rejected for the
operator-routing reason above. *Have adapters swallow an unrenderable coordinate and record a
finding themselves* — rejected: adapters do not own the run's findings, and it re-opens the
"drop a dependency with nothing recorded" hole `AdapterCoordinateError`'s own docstring refuses.
*Agent Recommendation:* the neutral-base-plus-driver-sibling split is a judgement call about where
the obligation should sit, not a requirement stated anywhere; the binding constraints were only
Rule 11's containment shape and §12.6's ecosystem-agnostic driver, and those admit other shapes.

**Consequences.**

1. **`AdapterCoordinateError` is now the contract for adapter authors**, and the invariant enforces
   the half of it that can be enforced mechanically (the class does not leak by name). That an
   adapter *raises the neutral type at all* is still a convention a new adapter can ignore by
   raising a bare `ValueError`; nothing detects that.
2. **`AdapterCoordinateError` is NOT reachable from the build-preparation path today, and that
   `except` clause was deliberately left alone.** An adapter raising it from `workspace_deps` or
   `package_files` would still escape as a hard run failure. Unreachable today is not guarded
   tomorrow; this is the standing follow-up, recorded in `docs/PROGRESS.md` §23.
3. **Rule 11's "after 3 retries" rung is not involved.** This is a **pre-lease preparation
   failure**, terminal on first occurrence — there is no retry ladder here to exhaust, and none
   was added.
4. **What the tests prove is attribution and containment, not Go.** There is still **no `go`, no
   gazelle, no `go mod download all` and no Go repo in the fixture fleet**; the Go half is
   exercised at the fleet-support-file layer with a **fake resolver**. Nothing here says any
   `go.mod` or `go.sum` is correct, or that Bazel accepts one.
5. **The full-chain "the rest of the fleet builds" test drives a PATCHED PYTHON adapter** raising
   the neutral error, so the survival behaviour is proven for a fleet whose **surviving ecosystem
   is JS** — **not** for a fleet that actually contains Go repos alongside others. Bazel is faked
   in those tests, so the survivors' `SUCCEEDED` is the state machine's verdict over a faked
   build.
6. **ADR-0051 consequence 2 is closed by this ADR**; ADR-0051 consequence 1 (the unreachable
   empty-content guard for `go.mod`) is untouched and still stands.

---

## ADR-0053 — One snapshot per **wave**, not per repo: a snapshot's *domain* is the plan set its root files were computed over, and a pre-dispatch guard refuses to dispatch a worktree that cannot contain that domain

**Decision.** Phase 3 stops cutting a build worktree per repo from that repo's own merge. Four
parts:

1. **`_prepare_build` is split into three passes.** An **ingest** pass
   (`_ingest_build_source`, `cli.py`) merges each repo's relocated history into the integration
   branch under the writer mutex; **one** `_wave_snapshot` runs **after the wave's last ingest**;
   a **plan** pass (`_plan_build`) cuts **every** member's worktree from that **single ref**.
2. **A snapshot's domain is stated, not implied.** The domain is the set of plans the fleet-wide
   root files were computed over. A worktree cut from a snapshot whose domain it cannot contain is
   an invalid build input, and the root files are the thing that makes it invalid.
3. **A pre-dispatch guard.** `RootFileDomainError` (`cli.py`) plus a check, before any lease is
   taken, that every `dest` in the domain the fleet-wide root files were computed over **exists as
   a directory** in every worktree about to be dispatched. It is **ecosystem-neutral**, **offline**,
   and runs **before** any build system.
4. **Snapshot immutability is untouched.** That is the property SPEC §3.3 fixes and it is
   preserved verbatim; the per-repo *cut point* was never the property.

**The defect this ADR exists to fix (D15).** Fleet-wide root files are computed over **every plan
prepared so far**, while each build worktree was cut at **that repo's own merge**. So the root
`Cargo.toml`'s `members` list could name a directory that repo's snapshot did not yet contain.
Cargo does not warn and does not skip — it **hard-fails the whole workspace** on an unreadable
member:

```
error: failed to load manifest for workspace member ...
No such file or directory (os error 2)
Error: Failed to generate lockfile
```

`fleet build` **exited 7** with both Rust repos `REQUIRES_HUMAN_INTERVENTION`. The **last** Rust
repo in the wave built and tested **green**, which is the diagnostic fact: this was an **ordering
property**, not a Rust property, and it is invisible to any fixture whose ecosystem has one repo.

**Why the other two ecosystems escaped, which is the part that generalises.** Neither was correct;
both were **tolerant**, for unrelated reasons. `npm_translate_lock` tolerates an **absent pnpm
importer directory**. A Python requirements file names **distributions, not paths**, so there is no
directory to be missing. Rust is the first ecosystem whose root file names **filesystem paths that
must resolve at build time**, so it is the first to convert the latent ordering bug into an error.
**Glob members fail identically** — including a glob that matches **nothing** — so "declare
`members = ["*"]` instead" is not an escape.

**Why the per-repo cut point was an artifact rather than an invariant.** It is recorded **nowhere**:
not in SPEC §3.3, not in any ADR, not in a docstring. The mechanical check is stronger than the
search: **no existing test encoded it.** The snapshot-immutability test, the wave-ordering test and
the Phase-3/Phase-4 ref-disjointness test **all pass untouched** after the change. A property that
three tests aimed at this exact area cannot tell you changed was not the property any of them was
defending. *Agent Recommendation:* treating "cut at the repo's own merge" as an implementation
detail rather than a contract is a judgement call — it is defensible precisely because it left no
trace in spec, ADR or test, and a future round that wants it back must say so explicitly and pin
it.

**Why the guard is separate from the fix, and why it checks existence only.** The per-wave snapshot
fixes the case that was observed. The guard is for the case that has not been observed yet: any
future root file that names a path. Three constraints shaped it. It is **ecosystem-neutral** —
it reads the domain the driver already computed and the worktrees the driver is about to dispatch,
and it names no ecosystem, per §12.6. It is **offline** and needs no build system, so it holds in
the fake-Bazel tests as well as the real ones. And it fires **before any lease**, so a run that
cannot possibly build does not spend a worker slot discovering that. Its deliberate limit: it
checks a dest directory **exists**, **not** that its contents are what a root file expects. That is
the honest boundary of a check written without ecosystem knowledge, and it is recorded rather than
narrowed by hand-waving. Verified by re-running it against the **old** behaviour, where it fires.

**Alternatives rejected.** *Compute fleet-wide root files over only the plans in the current
repo's snapshot* — rejected: it makes the root file a function of build order, which is the same
class of bug as D13/D14's first-`repo_id`-wins, and it silently shrinks the `@crates` hub.
*Make the members list a glob* — rejected on measurement: a glob matching nothing fails the same
way, so this trades a legible error for an illegible one. *Tolerate the missing member by filtering
`members` to what exists on disk* — rejected: it converts a hard, loud, correct cargo error into a
silent drop of a crate from the workspace, which is exactly the failure shape D13 and D14 cost this
project five checkpoints. *Re-snapshot per repo but re-render root files at cut time* — rejected:
it multiplies snapshots by repos for no property gain and reintroduces order-dependent root-file
content.

**Consequences.**

1. **Snapshot count drops from one-per-repo to one-per-wave.** Every repo in a wave builds from
   **byte-identical** integration state, which is a strictly stronger statement than the one the
   previous arrangement could make.
2. **The cross-wave residue is NOT fixed, and it is the real architectural question.**
   ~~Two Rust repos in **different waves** reproduce D15 **verbatim**~~ (**this clause is WRONG —
   see the correction below; the surviving cross-wave shape is D13's, not D15's**): the earlier
   wave settles with root files computed over a **smaller domain**, is **never re-admitted**, and
   its published root files stay **stale**. The guard deliberately checks only the repos a wave
   dispatches, and **both new tests assert only the intra-wave property**. Nothing here should be
   read as covering it.
3. **Fixing consequence 2 is a spec amendment with two defensible answers, and it awaits a human
   decision.** **(i)** Scope build-time root-file content separately from published content — then
   every repo **builds against something it does not publish**, adding a **fourth instance** of the
   ledger's *"the harness's exit code is not a verdict on the monorepo"* failure shape.
   **(ii)** Re-admit a settled wave when a root file changes — this touches the **scheduler** and
   **overturns the "a settled wave is never re-admitted" property recorded in three places**.
   *Agent Recommendation:* neither option is a requirement stated anywhere, both costs above are
   measured or textual rather than speculative, and **no agent should one-shot this**. Recorded in
   `docs/PROGRESS.md` §24 as the top follow-up.
4. **The guard proves less than a build.** It runs no build system, so a domain that passes it can
   still fail Bazel for any reason the guard does not model — starting with contents.

> **Correction (2026-08-14, ADR-0055) — consequence 2's headline claim was WRONG, and the reason
> it was wrong is the more useful fact.** Marked with a strikethrough above rather than an
> "Amended by" note, because this is not a decision later work replaced: it is a **statement about
> a mechanism that was never true**, and this file's precedent for that is ADR-0050's measured
> correction, not ADR-0019's amendment.
>
> **Within one `fleet build` process the domain is monotone**, which forbids the failure this
> consequence predicted. A plan exists only after its repo's merge, and every later wave's snapshot
> descends from every earlier merge — so a later wave's worktrees contain **every earlier dest**,
> and the fatal cargo shape (a `members` entry naming an absent directory) **is not reachable
> forward**. D15 cross-wave, as written, cannot happen.
>
> **What actually survived cross-wave is the milder D13 shape**, which the rest of the consequence
> describes correctly: a wave settles **green** against the root files that were at the root
> *then*, a later wave replaces them, and the settled wave is **never re-checked**. That is a
> stale-verdict defect, not a fatal-load defect — nobody's cargo hard-fails, and the harness still
> exits 0. Read consequence 2 as that claim.
>
> **Consequence 3's framing is overtaken too.** ADR-0055 took **neither** of its two options: root
> files are now computed **once per run over the whole DB-derived domain** before any dispatch, so
> nothing builds against something it does not publish (option i) and no settled wave is
> re-admitted (option ii). The residue that remains after ADR-0055 is the *narrow* one — a repo
> that succeeded in an **earlier invocation** still does not rebuild — and it is recorded there.
> The same correction is applied in `docs/PROGRESS.md` §24→§25 and `docs/INTEGRATION_HONESTY.md`
> D15. **Two test docstrings in `tests/test_build_e2e.py` still carry the wrong claim** and need a
> code change; see §25.

---

## ADR-0054 — On publish, the **planned bytes win**: `_publish` re-materializes every declared support file rather than committing whatever the build system left in the worktree

**Decision.** `_publish` re-writes **every declared support file's planned bytes** through
`buildgen.materialize` — the same loop GENERATE uses — immediately before the publish commit. The
tree as the build system left it is **not** the source of published content; `SupportFile.content`
is. This is a **general rule for every ecosystem**, not a Rust patch.

**The defect this ADR exists to fix (D16).** `crate.from_cargo` **rewrote `//:Cargo.lock` in the
worktree** as a normal part of building, and `_publish` committed the mutation. One repo therefore
published a lock naming **both** members while its siblings published the **planned** bytes, and
`git merge-tree` answered:

```
CONFLICT (add/add): Merge conflict in Cargo.lock
```

The interesting part is *which* invariant broke. §11.6 byte-determinism was violated **not by a
nondeterministic generator** — the generator is deterministic and was innocent — but by **the build
system editing the tree underneath the harness**. Every layer that reasons about determinism was
reasoning about the wrong producer.

**Why the fix is general rather than Rust-specific.** Two reasons, one principled and one
empirical.

The principled one is that `buildgen.materialize`'s **own docstring already made this argument**:
bytes must come from `SupportFile.content` and never from a re-read of the tree. GENERATE obeyed
it; publish did not. The fix is not new policy, it is applying existing policy at the second site
that needed it — which is why it is one call to an existing loop rather than a new code path.

The empirical one is that the alternative requires knowing which ecosystems mutate the worktree,
and that was **checked rather than assumed**: `npm_translate_lock`'s `update_pnpm_lock` defaults
**False** given the attrs `js.py` actually sets; `pip.parse` has **no writeback**; and the
harness's own resolvers run in a **scratch dir**, not in the worktree. So Rust is the only current
offender — and that is exactly the argument for the general rule, because the property being
defended ("published bytes are the planned bytes") is not a fact about cargo, and the next ruleset
that writes back would otherwise reintroduce D16 with every existing test green.

**Why this cannot mint spurious commits — the `is_dirty()` reasoning, stated rather than hoped.**
Re-asserting planned bytes can only move a path from **differing** to **identical**; it can never
make a path differ that did not differ before. Therefore the dirty check **can never mint a commit
it would not have minted before**. What changes is the *interpretation*: a build-system writeback
that previously read as **real work to publish** now correctly reads as **already-published**.

**Disclosed cost, because it is a real one and pretending otherwise is the failure this document
exists to prevent.** Cargo's lock **extension is discarded every build**, so **each Phase 3/4 build
re-extends from the seeded lock**. That is accepted deliberately: the extension is derivable work
that cargo redoes offline-cheaply from a warm registry, whereas the alternative — letting a build
system's writeback become published content — is a correctness property that cannot be recovered
once lost. *Agent Recommendation:* that trade is a judgement call, not a stated requirement; the
binding constraint was only §11.6 byte-determinism, which several arrangements could satisfy.

**Alternatives rejected.** *Set `skip_cargo_lockfile_overwrite` on the Rust `crate.from_cargo` tag*
— rejected **as the fix**, and kept only as **optional belt-and-braces**: it is **Rust-only**, it
needs a **`bool` in `WorkspaceDep.attrs`** (which today carries strings), and it is **unverified at
the pinned `rules_rust` version**. Making a per-ruleset opt-out the primary defence puts the
obligation on whoever adds the next ecosystem, which is precisely the shape ADR-0052 argued against.
*Re-read the worktree at publish and diff against the plan, failing loud on a mismatch* — rejected:
it converts a routine, expected, harmless writeback into a run-ending error, and Rule 11's loudness
is for things nobody can fix by re-writing a known byte string. *Publish from the tree but exclude
`Cargo.lock` by name* — rejected: language knowledge in the driver (§12.6), and it defends exactly
one file name. *Make the build worktree read-only* — rejected: build systems legitimately write to
their own outputs, and this would break far more than it fixes.

**Consequences.**

1. **Published support-file bytes are now a function of the plan alone**, for every ecosystem, and
   are independent of anything a build system did to the tree.
2. **Cargo re-extends the lock on every Phase 3/4 build** (the disclosed cost above). It is offline
   once the registry index is warm in the workspace-local `CARGO_HOME`.
3. **`is_dirty()` semantics are narrowed, never widened** — see the reasoning above; no commit can
   be minted that would not have been minted before.
4. **The audit of "who else mutates the worktree" is a point-in-time measurement**, taken at the
   currently pinned ruleset versions. A ruleset bump can invalidate it, and nothing detects that
   automatically. The general rule holds regardless, which is the reason it is the general rule.

---

## ADR-0055 — A run's build domain is derived from **SQLite**, not accumulated in the process: every eligible repo is ingested and planned before the first dispatch, and the root-file guard gains a **coverage** half

**Decision.** `_build_impl` runs **three run-level passes before any dispatch**, and the wave loop
afterwards does **dispatch only**. Five parts:

1. **One ingest pass for the whole run.** Every eligible repo is merged into the integration
   branch in `(wave_index, repo_id)` order, each merge still **alone under the writer mutex**.
2. **One snapshot, after the run's last ingest**, and **every** ingested unit is planned from it.
   ADR-0053 moved the snapshot from per-repo to per-wave; this moves it from per-wave to per-run,
   for the same reason and one level up.
3. **Support files are resolved once per run**, over that whole unit set, rather than once per
   wave over the waves seen so far.
4. **The domain is a query, not an accumulator.** `_eligible_build_units` (`cli.py`) joins wave
   members against `TRANSFORM`-SUCCEEDED phases across **all** waves. The process-local `plans`
   dict is no longer what the root files are computed over.
5. **`_check_root_file_domain` gains a second half.** Alongside the existing **containment** check
   (every `dest` in the domain exists as a directory in every worktree about to be dispatched) it
   now asserts **coverage**: the set the root files were actually rendered from **equals** the
   DB-derived domain. New `RootFileDomainDriftError`. Both passes are **skipped when there are no
   open waves**, so a settled fleet still does nothing.

**The defect this ADR exists to fix (D18) — the published root files could SHRINK.** Reproduced
with `fleet build --wave 0` followed by a plain `fleet build`. **Both invocations exited 0**, and
the published `MODULE.bazel` had lost `rules_jvm_external` and `rules_rust` **entirely** — their
`bazel_dep`, `single_version_override`, the whole `maven.install` block and the whole
`crate.from_cargo` / `rust.toolchain` block; `pnpm-workspace.yaml` and `.bazelignore` swapped
importers; the root `BUILD.bazel` stopped exporting `Cargo.toml` / `Cargo.lock`.

The mechanism is three facts that are individually reasonable: `plans` is **process-local and never
rehydrated** from SQLite; `_open_phase_waves` **excludes settled waves**; and
`_check_root_file_domain` **structurally could not fire**, because it read its domain off the same
shrunken `plans` and **a shrunken domain is trivially contained**. The guard ADR-0053 added was
therefore tautological against exactly this failure.

**The necessary condition, stated because it bounds the blast radius.** At least one wave still
**unsettled** while an earlier one is **settled**. `--repo` and `--wave` on a **fully complete**
run do **not** reproduce it — nothing is dispatched. And **`fleet resume` is `_unavailable`**, so
re-invoking `fleet build` is the **only** way an operator continues a partial run: the defect sat on
the sole recovery flow.

**One prediction was refuted by the measurement, and it is worth keeping.** `//:Cargo.toml` was
expected to shrink; it did **not**. With no Rust plan in the second invocation the file is **not
regenerated at all** — it is **orphaned**, and the root `BUILD.bazel` simply stops exporting it.
A file that stops being written looks nothing like a file that is rewritten smaller, and only the
second was being watched for.

**Why the domain comes from SQLite rather than from a process-local accumulator.** Because the
accumulator's contents are a function of **which invocation you are in**, and the root files must
not be. SQLite already holds the phase rows that decide eligibility, ADR-0004 already makes it the
authority on execution state, and guardrail 4 forbids the alternative shape (a driver-side shadow
of what git and the DB already know). A query answers the same question identically on the first
invocation and the fifth, which is the property the root files need and the only property that
would have prevented D18. *Agent Recommendation:* deriving the domain from the DB rather than
rehydrating `plans` from disk is a judgement call — both would fix the observed shrink; the query
is preferred because it has no second copy to keep correct.

**Why the guard needed a second half, and which half is non-tautological.** **Containment** asks
"does every dest in the domain exist in this worktree" — it is a check on the **tree**.
**Coverage** asks "is the set the root files were rendered from the same set the DB says is
eligible" — it is a check on the **derivation**, and it is the one that **fires against the old
behaviour**, which is how it is known not to be tautological. Its own honest limit is stated in the
consequences: it compares two **driver-side** derivations and never consults the git tree.

**Why `--repo` / `--wave` stay dispatch filters.** They select **what is built**, never **what the
monorepo is**. This is pinned by a test asserting that a narrowed run publishes **byte-identical**
root files to a full-fleet run. *Agent Recommendation:* this is a judgement call about operator
ergonomics — nothing in SPEC or `CLAUDE.md` says a filter may not narrow the fleet — and it is
chosen because the alternative reintroduces D13/D14's shape, where a root file's content depends on
which subset of the fleet an operator happened to name.

**Two failure classes, because the hoisted ingest creates a hazard of its own.** The domain is
fixed before anything builds, so a repo that later fails needs an answer:

- **Pre-domain failure** (ingest or plan fails **before** the domain is fixed): the `dest` is
  **popped from the domain**. Safe, because nothing has been dispatched and nothing published, so
  the domain is still maximal for every wave that follows.
- **Post-domain failure** (a re-plan fails **later**): the `dest` is **kept**, because earlier
  waves have **already published root files naming it**, and the repo is marked
  `REQUIRES_HUMAN_INTERVENTION`. Dropping it here would republish a smaller root file, which is
  D18 again.

**Alternatives rejected.** *Rehydrate `plans` from SQLite at startup* — rejected: it keeps a second
representation of the domain alive and fixes only the case somebody remembered to rehydrate.
*Re-admit settled waves so their root files are refreshed* — rejected here as it was in ADR-0053
consequence 3: it overturns a property recorded in three places, and it is not needed, because
computing the root files **once, maximally, before the first build** makes the refresh unnecessary.
*Make `--repo`/`--wave` narrow the fleet as well as the dispatch* — rejected: it makes a root file
a function of an operator's argv. *Keep the containment guard and add an assertion that the domain
never shrinks between invocations* — rejected: it requires the driver to remember prior
invocations, which is the shadow-state shape guardrail 4 forbids, and it detects the symptom one
invocation after the damage.

**Consequences.**

1. **The root files a run publishes are a function of the DB-derived eligible set alone**, and are
   identical whether the run dispatches one repo, one wave, or the fleet.
2. **A settled wave is still never re-admitted.** What is fixed is that root files no longer
   **shrink**; a repo that succeeded in an **earlier invocation** still does not rebuild against
   the newer root files. This is the narrowed residue of ADR-0053 consequence 2/3.
3. **The containment half still checks only that a dest directory *exists***, not that its contents
   match — unchanged from ADR-0053.
4. **The coverage half compares two driver-side derivations**, never a derivation against the git
   tree. A defect that corrupts both identically is invisible to it.
5. **A partially built branch is now legitimately un-`//...`-able.** After `--wave 0` or `--repo X`
   the root files **correctly** name units whose packages were never generated, so
   `bazel build //...` over that branch fails **by design**. This is a real change in what a
   partial run leaves behind, and it is the price of (1).
6. **The post-domain re-plan-failure path has no test.** It could not be induced deterministically
   without a new seam, and inventing one was out of scope for this round. Recorded as a gap.
7. **A whole-ecosystem resolve failure drops that ecosystem from the domain**, and the survivors
   publish a `MODULE.bazel` without it. This is **pre-existing in shape** and is disclosed here for
   the first time; it is **untested**.
8. **Every invocation now re-clones and re-filters every eligible repo.** That cost is measured
   only on **2–9-repo fixtures** and nothing here says what it is at 250.
9. **No concurrency testing** of two overlapping `fleet build` processes under the new structure.

---

## ADR-0056 — Gazelle's output is **captured from a scratch tree as planned bytes**, produced by **one invocation over every root** of the ecosystem with `-external=static -index=all`, from a **vendored v0.51.3** binary that deliberately diverges from the BCR module pin

**Decision.** `cli._build_impl` gains a **PASS 4**, build-file generation, between PASS 3 (support
file resolution) and the wave loop. Six parts:

1. **One Gazelle invocation per Gazelle-using ecosystem, over *all* of that ecosystem's repo
   roots** — not one per repo. The multi-root argv is what resolves cross-repo imports.
2. **The invocation runs over a scratch tree, never over a worktree.** `_assemble_gazelle_scratch`
   builds it: each unit's `<dest>` subtree copied out of its build worktree, the directives-only
   `BUILD.bazel` rendered from **the same `GazelleConfig` the worker uses**, the adapter's resolved
   root files written **verbatim**, and a **comment-only `MODULE.bazel`** as the repo-root marker.
3. **Every created or modified `BUILD.bazel` is captured** into `_BuildPlan.gazelle_files` as
   planned `SupportFile`s, and reaches the branch through the existing
   `materialize` → `_publish` path.
4. **PASS 4 runs after PASS 3 and before the wave loop**, for two independent reasons:
   `-external=static` resolves against the union `go.mod` **that PASS 3 produces**, and **one**
   invocation must cover **every** root, which is only possible before any dispatch.
5. **A new seam, `cli.GAZELLE_RUNNER`, mirroring `RESOLVER_RUNNER`.** Its absence is a loud
   `BuildFileGenerationError`, **never a silent skip**. New finding kind
   `BuildFileGenerationFailed`; a PASS-4 failure **drops that ecosystem from both `plans` and
   `domain`**, safe for exactly the reason ADR-0055 gives for the PASS 1–3 failure path.
6. **The generator is a vendored binary, `tools/bin/gazelle`, wrapping bazel-gazelle
   v0.51.3**, and `GazelleConfig.args` — written long ago, populated, and never dispatched —
   is now **the dispatched thing**, carrying `-external=static -index=all` with the
   why-comments beside them. The driver never spells either flag (§12.6).

**The user's two hard constraints, and how each is satisfied.** These were stated as hard, so they
are answered here explicitly rather than left to be inferred.

- **Constraint 1 — do not break the `Cargo.lock` determinism fix (ADR-0054's publish contract).
  Satisfied with no carve-out.** Because the bytes are produced in a **scratch** tree and folded
  into the plan, they are **planned** bytes; the existing `materialize` → `_publish` path commits
  them under exactly the rule ADR-0054 established — the planned bytes win. **Nothing in ADR-0054
  was reverted, excepted, or special-cased.** The corollary is that staging is done by **naming
  materialized paths**, never `git add -- <dest>`: the latter would sweep in build-system writeback
  and re-open the defect ADR-0054 closed.
- **Constraint 2 — do not produce a Go test that drops sub-packages. Satisfied structurally, not
  by an assertion.** Captured files are `SupportFile`s, so **arbitrary depth falls out for free**,
  and **nothing is staged that the plan did not declare**. A sub-package at **depth 3** is captured
  by the real binary in the integration test.

**Why one invocation over every root — and the measurement that forces it.** A per-repo invocation
**silently drops real cross-repo edges**. Measured in the integration test, with the negative
control in the same test: with a sibling import added to `acme-clitool-go`, the multi-root run
resolves the dep to the in-repo label **`//go/digest`**; the **identical source** run through a
**one-root** invocation yields `deps = ["@com_github_spf13_cobra//:go_default_library"]` — the
sibling edge is **dropped at exit 0, with no label and no warning**. So the multi-root argv is what
is under test, not the absence of a spelling. No `require` was added to any `go.mod`; `-index=all`
resolves the edge from the package indexed at the **other** root.

**Why `-external=static` and `-index=all`, and the research claim each one refutes.** Research said
the **default** `-external` mode was the safe one and that `-external=static` *"silently skips"*
unknown imports. **The opposite is true, measured with logging shims.** The **default** mode shells
out to `go get` / `go list` / `git ls-remote`, resolves in a **throwaway temp module that ignores
the union `go.mod` pins entirely** (it fetched `x/crypto` **v0.55.0**, not the pinned version),
tried to **reach GitHub** for an internal import, and **silently dropped a real dependency at exit
0**. `-external=static` used **zero subprocesses and zero network** and resolved a **strict
superset**. `-index=all` is what makes the multi-root run see the other root's packages; without it
the cross-repo edge is not resolvable from the index at all. Zero network is not an argument, it is
a measurement: `strace -e trace=socket,connect,sendto,sendmsg` recorded **zero** matching syscalls,
and `-e trace=execve` shows the generator spawns nothing beyond the wrapper's own `dirname`/`exec`.

**Why the vendored binary is v0.51.3 while the BCR module pin stays 0.52.2 — a decision, not
drift.** The module pin **cannot move down**: the version table was chosen by **load-probing under
Bazel 9.2.0** (the D8 round), and 0.52.2 is what loads. The **binary** is a separate artifact that
never participates in module resolution, and v0.51.3 is what installs. **The divergence is
deliberate and is recorded here precisely because an unrecorded version divergence is the D8
shape.** Two research claims about the install were also wrong and are corrected on measurement:

- **Research said `go install` was broken only at 0.52.x (upstream issue #2396).** In fact **every
  published version from v0.48.0 declares `go 1.24.12`**, so **no downgrade reaches an installable
  gazelle** under the pinned 1.23.4 toolchain. The install needed a **one-shot
  `GOTOOLCHAIN=go1.24.12` override**, which the wrapper's `${VAR:-default}` idiom permits and which
  kept the toolchain **inside the workspace** (~500 MB added, mostly the `go1.24.12` toolchain
  module pulled in to build it).
- **Research said `gazelle -version` identifies the binary.** It reports **`unknown`** — the
  version is **linker-stamped only by Bazel builds**. Any version assertion must parse
  **`go version -m`**, which is how v0.51.3 is confirmed. (`--version` exits 1; `-version` is a
  subcommand flag.)

**Why the wrapper is required rather than cosmetic.** Unwrapped, Gazelle's `findGoTool()` picks the
**host's `/usr/bin/go`** and writes into **`$HOME/go`** — the same containment failure §24's rustup
finding and §25's Go-telemetry finding describe, a third time. A related fact worth recording
because it looks like a discrepancy and is not: a `go install`-built gazelle **keeps the go, proto
and visibility languages**, because plain `go build` compiles `cmd/gazelle/langs.go`, which Bazel's
`gazelle_binary` rule deliberately **excludes**.

**A SPEC divergence, recorded as deliberate.** `settings.build.gazelle_binary` changes from
`"//:gazelle"` to `"gazelle"`. The old value is a Bazel **label**, implying `bazel run //:gazelle`,
which is **unrunnable over a scratch tree that is not a workspace**; it was referenced by **nothing
in `src/`**. This **contradicts SPEC's example config** and is a divergence, not a typo fix.
*Agent Recommendation:* keeping the setting (as a binary name resolved on PATH / in `tools/bin`)
rather than deleting it is a judgement call — nothing requires the setting to exist at all; it is
kept because it is the one place an operator can repoint the generator.

**Two ordering facts that make this safe rather than lucky.**

1. **The directives/re-entry conflict resolves by existing ordering.** `buildgen` writes the
   directives render and **then** calls `materialize`, which overwrites from `SupportFile.content`
   — so GENERATE's unconditional re-render on checkpoint rejection is **harmless**.
2. **A latent bug fixed in passing.** `materialize` had been **gated on
   `output.module_bazel_path`**, so a **publish-only re-entry staged a pathspec it had never
   written**. It is now **unconditional**.

**Alternatives rejected.** *Run Gazelle in the build worktree and commit what it leaves* — rejected:
it is a **writeback**, precisely the shape ADR-0054 closed, and it would make the published bytes a
function of whatever else touched the tree. *Run Gazelle once per repo* — rejected on the
measurement above: it drops cross-repo edges at exit 0. *`git add -- <dest>` after generation* —
rejected: it stages bytes the plan never declared, re-opening ADR-0054's defect. *Use the default
`-external` mode* — rejected on measurement: network, subprocesses, pins ignored, a real dependency
silently dropped. *Move the BCR module pin down to 0.51.3 so binary and module agree* — rejected:
the pin was chosen by load-probing and 0.51.3 is not what loads; agreement bought by breaking the
load is not agreement. *Assert the version from `gazelle -version`* — rejected: it prints `unknown`.
*Skip generation when the seam is absent* — rejected under Rule 11; absence is loud.

**Consequences.**

1. **Gazelle's output is planned bytes.** Every generated `BUILD.bazel` that reaches the branch is
   declared in the plan first, and ADR-0054's contract is honoured **unchanged**.
2. **Sub-packages at arbitrary depth are included**, structurally — not because a test enumerates
   them.
3. **A PASS-4 failure removes a whole ecosystem from the run**, `plans` and `domain` together. This
   is the ADR-0055 pre-domain shape and inherits its safety argument; like ADR-0055 consequence 7,
   the survivors then publish a `MODULE.bazel` without that ecosystem.
4. **The binary and the module pin are two versions on purpose**, and the *only* thing keeping them
   honest is this ADR plus a test that parses `go version -m`. If the module pin moves, nothing
   automatically re-probes the binary.
5. **No Bazel has ever loaded a file this generator produced.** Nothing here shows that
   `@com_github_spf13_cobra` or `@org_golang_x_crypto` exist under those names, that `go_deps`
   creates them, that `//go/digest` is a loadable target, or that **any** label resolves at analysis
   time. **Labels are asserted as text.**
6. **No Go is compiled by anything.** Gazelle parses `import` statements; it does not typecheck. The
   sibling import is proven to **resolve**, not to **build**.
7. **The scratch tree is not a Bazel workspace** — its `MODULE.bazel` is a one-line comment marker —
   so nothing about it constitutes evidence that the real tree loads.
8. **The real-binary tests exercise `_run_gazelle` directly.** The full
   `fleet build` → `materialize` → `_publish` path over **real** generator bytes is still covered
   only by the fake, and **no Go repo appears in any real-Bazel test**.
9. **The fake and the real binary diverge in shape, in two disclosed ways, neither affecting a
   label.** The real binary **rewrites** a pre-existing directives file, putting its `load()` at the
   top with the directives below, where the fake **appends**; and the fake prefixes created files
   with a newline while real ones start at `load(`. *Agent Recommendation:* the fake was **not**
   restructured this round — the divergence is disclosed rather than removed, which is a judgement
   call and a standing invitation for a future round to close it.

---

## ADR-0057 — A ruleset's **apparent repo name** is adapter-level knowledge: `EcosystemAdapter.ruleset_repo_names`, unioned across adapters with disagreement **raised**, and `repo_name` emitted **only** where it diverges from the module name

**Decision.** `render_module_bazel` emits `bazel_dep(name = …, version = …, repo_name = …)` where a
ruleset publishes itself under a name that is not its module name, and the mapping lives on the
adapter that also writes the `load()` labels. Four parts:

1. **New `EcosystemAdapter.ruleset_repo_names: ClassVar[Mapping[str, str]]`** — module name →
   apparent repo name. `GoAdapter` declares `{"rules_go": "io_bazel_rules_go"}`
   (`ecosystems/go.py`); every other adapter declares the empty default (`ecosystems/base.py`).
2. **A registry function, `ecosystems.ruleset_repo_names()`, unions it over every adapter and
   RAISES if two adapters give one module different names.** This mirrors `extension_bzls()`
   deliberately, with its argument sharpened: **a `bazel_dep` has exactly one apparent name**, and
   a `bazel_dep` is emitted **once** for a ruleset several adapters may share, so a
   last-writer-wins would silently break whichever adapter lost.
3. **`bazel/generators.py` reads the union through `_registry_ruleset_repo_names()`** and takes an
   injectable `ruleset_repo_names` parameter, the same shape `extension_bzl` already uses. The
   renderer therefore does not know that `rules_go` asks to be spelled `@io_bazel_rules_go`; the
   adapter that writes `@io_bazel_rules_go//go:def.bzl` into the loads is the file that knows.
4. **`repo_name` is emitted only where it differs from the module name** (`_bazel_dep`). The
   default case renders exactly the bytes it rendered before, so no other ecosystem's asserted
   output moved.

**The failure this fixes, and it was a LOADING failure, not an analysis one.** Gazelle writes
`load("@io_bazel_rules_go//go:def.bzl", …)` into every Go package it creates, while the root module
said `bazel_dep(name = "rules_go")` with no `repo_name` — so the apparent repo was `@rules_go` and
real Bazel answered **`No repository visible as '@io_bazel_rules_go' from main repository`**. The Go
packages never became targets at all, and it hit **every** Go repo, not a particular one. Repairing
it forced a second disagreement immediately: `GoAdapter.extension_bzl["go_sdk"]` spelled the module
`@rules_go` while `library_bzl` / `binary_bzl` / `test_bzl` spelled it `@io_bazel_rules_go`, and
because a `bazel_dep` has exactly one apparent name, **fixing either spelling broke the other**. The
decision above is what makes one spelling true rather than merely tolerated.

**Why route (a) — agree with the generator — and not `# gazelle:map_kind`.** Both routes make the
two names agree; the choice is *which side moves*, and it was made on evidence rather than taste.

- **Upstream `bazel-gazelle`'s own `MODULE.bazel` declares
  `bazel_dep(name = "rules_go", version = "0.59.0", repo_name = "io_bazel_rules_go")`.** The
  generator's own project spells the consumer contract this way, so agreeing with the generator is
  the canonical shape rather than a local convention.
- **This codebase already believed it.** `tests/test_bazel.py`'s `_RULESET_LOAD_PROBES` maps
  `"rules_go" → "io_bazel_rules_go"` and probes `@io_bazel_rules_go//go:def.bzl`, and that module
  file **already loads under real Bazel** — the D8 guard has been asserting the correct apparent
  name for the ruleset the renderer was spelling wrongly.
- **`map_kind` was rejected on three counts, all structural.** It pays for the fix by **rewriting
  generator output that other tests attest** (ADR-0056's set-for-set label comparison); it needs
  **one directive per kind**, with **no coverage for kinds not enumerated** (`go_test`,
  `go_proto_library`, anything a future language extension emits); and it leaves this harness's
  label spelling **permanently divergent from the wider Go ecosystem**, so every future
  copy-pasted upstream snippet is wrong here.

**The audit of the other four ecosystems found no equivalent mismatch — Go is the only one.** Each
adapter's `library_bzl`/`binary_bzl`/`test_bzl`/`extension_bzl` labels were read against its
`ruleset_versions` module names: `aspect_rules_js`, `aspect_rules_ts`, `rules_python`,
`rules_jvm_external`, `rules_rust` and `rules_proto` all publish themselves under their module
name. So `ruleset_repo_names` is a one-entry table today, and that is a measurement, not an
oversight. *Agent Recommendation:* keeping it as a **table** rather than hard-coding the single Go
case in the renderer is a judgement call — nothing requires the general shape; it is chosen because
the alternative puts ecosystem knowledge in `bazel/generators.py`, which is exactly what D1 moved
out of there.

**Alternatives rejected.** *`# gazelle:map_kind` to rewrite the generated `load()`s* — rejected on
the three structural counts above. *Spell `bazel_dep(name = "io_bazel_rules_go")`* — rejected: that
names **nothing in any registry**; the apparent name is set by the dependent's `repo_name`, never
by the module's own name. *Change the adapter's `library_bzl` to `@rules_go//go:def.bzl` and let
the generator's output be the odd one out* — rejected: the generator writes the loads for **every
package it creates**, so this is the `map_kind` route wearing a different hat, and it loses to the
same evidence. *Emit `repo_name` unconditionally, equal to the module name where they match* —
rejected: it rewrites four ecosystems' attested bytes to fix one, for no behavioural gain. *Add an
`use_repo`-style alias in the root module* — rejected: `bazel_dep(repo_name = …)` is the mechanism
Bzlmod provides for exactly this, and inventing a second one is ADR-0045's shape in reverse.

**Consequences.**

1. **The generated Go tree LOADS under real Bazel for the first time.** `bazel query //go/...`
   exits **0** over unmodified harness output, and `bazel build` reaches **108 packages loaded /
   8796 targets configured** before hitting the *separate* version conflict ADR-0058 closes. The
   loading failure and the version failure are two defects, and this ADR closes only the first.
2. **Two adapters disagreeing about one module is a startup error, not a silent last-writer-wins.**
   That refusal is the only thing standing between a shared ruleset and a `load()` that can never
   resolve for one of its two claimants.
3. **Only Go's rendered `MODULE.bazel` changed.** Every other ecosystem's module bytes are
   byte-identical to before, which is why the fix cost no other test.
4. **The table has exactly one entry and is expected to stay small.** Nothing re-audits it: if a
   future ruleset publishes itself under a different name and nobody adds a row, the failure is the
   same `No repository visible as …` this ADR fixed — loud, but only at real-Bazel time.

---

## ADR-0058 — The Go SDK pin is **1.24.12**, the minimum *both* pinned rulesets accept; the root `go.sum` did **not** move, because a lock line is a content hash of a published zip and not a toolchain artifact

**Decision.** `ecosystems/go.py`'s `_GO_VERSION` is raised **1.23.4 → 1.24.12**, and every site that
reads it moves with it. The pin is the **minimum** that satisfies both floors, not the newest
available: a version pin that drifts upward for no measured reason is D2's shape.

**The floor evidence, and the point of the table is that lowering gazelle does NOT work.** The
obvious cheaper route — move gazelle down until it accepts 1.23.4 — was checked and rejected on
measurement, not assumed away.

| Claim | Measured |
| --- | --- |
| gazelle **0.52.2** (the pinned module) needs a newer SDK | its `go.work`/`go.mod` declares **`go 1.24.12`**; a 1.23.4 SDK fails `failed to build tools: go: go.work requires go >= 1.24.12` |
| the floor is **not gazelle's alone** | **rules_go 0.61.1's own `go.mod` declares `go 1.24.0`** — above 1.23.4 by itself |
| lowering gazelle does not shed gazelle | **rules_go 0.61.1 depends on gazelle 0.51.3**, which also declares **`go 1.24.12`** |
| how far down the floor holds | **every gazelle v0.48.0 – v0.52.2 declares `go 1.24.12`** |
| the first version below the floor | **v0.47.0**, which **predates the Bazel 9 fixes the D8 settings table was chosen for** |

So the two pins were **individually justified and jointly impossible**, and the resolution is to
move the SDK rather than either ruleset. This is the same failure class §26 recorded for the
*binary* install ("no downgrade reaches an installable gazelle"), arriving a second time at the
*module* layer.

**`_GO_VERSION` is load-bearing in SIX places, and all six moved together.** Naming them is the
decision, because a partial move is a pin that lies:

1. the `go_sdk.download` version in `toolchain_requirements()` (`ecosystems/go.py`);
2. the resolver's `GOTOOLCHAIN` env on the Go `Resolution` (`ecosystems/go.py`, ADR-0051's `env`);
3. the union `go.mod`'s `go` directive (`_go_mod_text`);
4. the vendored `tools/bin/go` wrapper's `GOTOOLCHAIN` default **and the SDK it points `GOROOT` at**;
5. the `tools/bin/gazelle` wrapper's own `GOTOOLCHAIN` pin, which governs the resolver subprocess
   and must keep matching the registered `go_sdk`;
6. the root `go.sum`, which is **regenerated by the real declared argv**, never hand-edited.

**The new SDK was installed exactly as §25's was.** Official `go.dev/dl` tarball, published SHA256
**verified before extraction** (`bddf8e653c82429aea7aec2520774e79925d4bb929fe20e67ecc00dd5af44c50`,
matched), extracted under `tools/go/`, **+18 MB (269 M → 287 M)**, **no sudo**, **nothing in
`$HOME`** — the containment rule §24 and §25 each had to rediscover.

**Why the `go.sum` came back BYTE-IDENTICAL, and why that is the correct result rather than a
suspicious one.** `GO_ROOT_GO_SUM` was regenerated by the declared resolver argv and did not change
one byte. That is what should happen: **a `go.sum` line is a content hash of a published module
zip**, and the build list MVS computes from an **unchanged `require` set** does not depend on the
toolchain that computes it. Only the `go` **directive** moved. Recording it here as a sanity check
matters because the opposite outcome — a lock that churns when only the toolchain moves — would
have meant the lock was a function of the environment, which is precisely what ADR-0043's
carry/resolve split exists to prevent.

**The MVS gap, and the two halves of it that are NOT inherent.** With both pins aligned, the build
is green — and it is green over module versions the harness did not choose. Verbatim from the
build:

    DEBUG: …/gazelle+/internal/bzlmod/go_deps.bzl:753:36: The following Go modules were required
    by the root module at the given versions, but were implicitly updated to higher versions due
    to transitive dependencies:
      golang.org/x/crypto: v0.31.0 -> v0.39.0
      golang.org/x/sys: v0.28.0 -> v0.33.0

- **The raise itself is inherent to Bzlmod.** `go_deps` is **one** module extension over the
  **whole** Bazel module graph; rules_go and gazelle each call `go_deps.from_file` on their own
  `go.mod`, so Go MVS runs across all three, and **a Go module path can have exactly one repository
  in a build**. There is no "resolve only from my `go.mod`" mode, and the per-module levers
  (`go_deps.module_override`, `archive_override`) do not change it in general. **The harness cannot
  fix this.**
- **The SILENCE is not inherent.** `go_deps.config(check_direct_dependencies = "error")` is a
  **root-module-only** tag that turns that DEBUG line into a hard `fail()`. The harness renders the
  root `MODULE.bazel`, so it **could** emit it. **It does not today.**
- **The unverified-fetch path is not inherent either.** `go_deps.bzl`'s `_get_sum_from_module`
  answers a raised version **with no sum** by printing `No sum for …@… found, run … mod tidy` and
  returning `None` — i.e. **fetching that module with no checksum at all**, rather than failing.
- **In this build no `No sum for …` line appeared**, so the raised zips *were* verified — **by
  gazelle's own `go.sum`, not by the sums this harness resolved.** The honest statement of what a
  green build here means: **the harness's sums are *consistent with* what Bazel selected; they are
  not what Bazel *verified*.**

*Agent Recommendation:* emitting `go_deps.config(check_direct_dependencies = "error")` is the right
next move and is **not** taken in this ADR, because it would turn a currently-green build red on a
condition the harness cannot resolve (see the inherent half above), and choosing to do that is a
decision about the harness's failure posture rather than a bug fix. It is recorded as the top item
of `docs/PROGRESS.md` §27's Next list rather than performed here.

**Alternatives rejected.** *Lower gazelle to a version that accepts 1.23.4* — rejected on the
evidence table: no such version exists above v0.47.0, and v0.47.0 predates the Bazel 9 fixes the
settings table was load-probed for. *Lower rules_go instead* — rejected: 0.61.1's own `go.mod`
declares `go 1.24.0` and it **depends on gazelle 0.51.3**, so the floor follows it down. *Pin the
newest available Go SDK rather than the minimum* — rejected as D2's shape: a pin chosen for
"newest" is a date, not an input. *Pin to ≥1.25.7 now, as §25's corpus survey says real corpus Go
repos will need* — rejected as **out of scope and unmeasured here**: the survey's requirement stands
and is carried forward, but nothing in this round exercised a `go 1.25` module, and raising past the
measured minimum would put an unverified pin behind a green build. *Move only the `go_sdk`
download and leave the wrappers* — rejected: the wrappers govern the **resolver** subprocess, so a
split pin means the sums are computed by one toolchain and consumed by another.

**Consequences.**

1. **Real Bazel analyses and COMPILES the generated Go tree from unmodified harness output.**
   `INFO: Analyzed 4 targets (116 packages loaded, 9178 targets configured)`, `GoStdlib`,
   `GoCompilePkg`, `INFO: Build completed successfully, 21 total actions`, and real `.a` archives
   for `digest`, `command` and `clitool_lib`. ADR-0056 consequence 6 ("no Go is compiled by
   anything") is **retired**.
2. **The cross-repo edge resolves at ANALYSIS time, read out of Bazel rather than out of text.**
   `cquery deps(//go/clitool/internal/command:command, 1)` returns **`//go/digest:digest`** beside
   `@com_github_spf13_cobra//:go_default_library`. ADR-0056 consequence 5's "labels are asserted as
   text" is **answered**.
3. **The harness's sums are consistent with, not authoritative over, what Bazel fetched.** This is
   a **standing correctness gap**, recorded as a named defect in `docs/INTEGRATION_HONESTY.md`
   rather than as a missing test, because no test would close it — a code change would.
4. **The SDK pin is now a three-way constraint** (harness `go.mod` ⟂ gazelle ⟂ rules_go) and
   **nothing re-derives it**. Moving any of the three re-opens the arithmetic, and only this ADR
   records that it is arithmetic at all.
5. **`fleet build` end to end for Go is still not proven.** The tree Bazel consumed was assembled
   from the harness's own generated bytes; `materialize`/`_publish` over **real** generator output
   remains covered only by the fake, which is the surviving half of ADR-0056 consequence 8.
6. **Only linux/amd64 SDKs were fetched and hashed**, nothing exercises `go_test` (neither fixture
   ships a `*_test.go`), and the fixture poses **no conflicting Go module version across repos** —
   five direct requirements, one cross-repo edge, two repos.

**Correction appended 2026-08-15 (ADR-0059) — two statements in the "MVS gap" bullets above are
wrong, and the *Agent Recommendation* under them is superseded.** Nothing above is edited; this
paragraph is the correction, per this log's never-edit-in-place rule.

- **FALSE:** *"The unverified-fetch path is not inherent either … fetching that module with no
  checksum at all, rather than failing."* The reading of `_get_sum_from_module` was correct but
  stopped one layer short: the `None` lands in `go_repository`, where `sum` is a **mandatory**
  attribute for every repo the extension creates (`is_module_extension_repo`, set from
  `internal_only_do_not_use_apparent_name`, which `go_deps.bzl` passes for all of them), and it
  **`fail()`s at fetch time**. **Measured** against the pinned gazelle 0.52.2 by deleting the single
  `h1:` line for `github.com/spf13/cobra v1.8.1` — first established by grep to be the only source
  of that hash in the whole module graph — after which Bazel emitted `Error in fail: No sum for
  github.com/spf13/cobra@v1.8.1 found, update go.sum with: …` and `Build did NOT complete
  successfully`. **Nothing is fetched without a checksum; the build fails closed.**
- **WRONG ATTRIBUTION:** *"the raised zips were verified — by gazelle's own `go.sum`"*. **Measured:
  it was rules_go 0.61.1's.** rules_go's `go.sum` carries `golang.org/x/crypto v0.39.0 h1:` and
  `golang.org/x/sys v0.33.0 h1:`; gazelle 0.52.2's carries **no** `x/crypto` `h1:` line at all, and
  `x/sys` only at v0.28.0/v0.30.0. The bullet's conclusion — the harness's sums are *consistent
  with* what Bazel selected, not what Bazel *verified* — is unchanged and still correct.
- **SUPERSEDED:** the *Agent Recommendation* that emitting
  `go_deps.config(check_direct_dependencies = "error")` "is the right next move". It was measured
  and **rejected** in **ADR-0059**: it aborts the whole monorepo at extension-evaluation time and is
  **neither necessary nor sufficient** for the checksum question. Consequence 3's "standing
  correctness gap" is narrowed accordingly — what stands is the **inherent** MVS raise plus a
  **readability** limitation, not an unverified-fetch hole.

---

## ADR-0059 — **No `go_deps.config` is emitted.** The MVS raise stays a DEBUG line, because the checksum hole it was meant to guard **does not exist**: a Go module version with no `h1:` anywhere **fails the fetch**, measured against the pinned gazelle 0.52.2

**Decision.** The root `MODULE.bazel` renderer **does not** emit
`go_deps.config(check_direct_dependencies = "error")`, and **no code in `src/` changed**. The MVS
raise that ADR-0058 recorded (`golang.org/x/crypto: v0.31.0 -> v0.39.0`,
`golang.org/x/sys: v0.28.0 -> v0.33.0`) remains a **DEBUG** line. Two tests pin the decision, and
**D19 in `docs/INTEGRATION_HONESTY.md` moves from OPEN to NARROWED AND ACCEPTED.** This entry
supersedes ADR-0058's *Agent Recommendation* to emit the tag, and corrects two of its statements
(see the correction appended to ADR-0058).

**The finding that reversed the decision: D19's supply-chain half is FALSE, and it was refuted by
measurement rather than by re-reading.** D19 and ADR-0058 both claimed that gazelle's
`_get_sum_from_module`, answering a raised version with no sum by printing and returning `None`,
means the module is **fetched with no checksum at all**. The function was read correctly; the
reading stopped **one layer too early**. The `None` is handed to `go_repository`, where `sum` is a
**mandatory** attribute for **every** repo the extension creates — the repo carries
`is_module_extension_repo`, set from `internal_only_do_not_use_apparent_name`, which `go_deps.bzl`
passes for all of them — and `go_repository.bzl` **`fail()`s at fetch time**, with
`fetch_repo_env["GOSUMDB"] = "off"` beside it and the comment *"the sum is a mandatory attribute of
go_repository, so we don't need to look it up."*

The measurement, against the pinned gazelle **0.52.2**: exactly one `h1:` line
(`github.com/spf13/cobra v1.8.1`) was deleted from the resolved root `go.sum` — **first verified by
grepping every `go.sum` in the session's repository cache** to establish it was the **only** source
of that hash in the whole module graph, so the deletion really did leave the version uncovered — and
Bazel then refused:

    DEBUG: …/go_deps.bzl:925:18: No sum for github.com/spf13/cobra@1.8.1 found, …
    ERROR: …/gazelle+/internal/go_repository.bzl:204:21: An error occurred during the fetch of
    repository 'gazelle++go_deps+com_github_spf13_cobra':
       Error in fail: No sum for github.com/spf13/cobra@v1.8.1 found, update go.sum with: …
    ERROR: no such package '@@gazelle++go_deps+com_github_spf13_cobra//': No sum for …
    ERROR: Build did NOT complete successfully

**Nothing is ever fetched without a checksum; the build fails closed.**

**The tag itself was measured too, not theorised.** With
`go_deps.config(check_direct_dependencies = "error")` in the root `MODULE.bazel`, the build fails at
**extension-evaluation time**, naming both modules and an exact remediation argv:

    Error in fail: The following Go modules were required by the root module at the given versions,
    but were implicitly updated to higher versions due to transitive dependencies:
      golang.org/x/crypto: v0.31.0 -> v0.39.0
      golang.org/x/sys: v0.28.0 -> v0.33.0

That abort takes down the **whole monorepo** before any target builds.

**Why the tag is neither necessary nor sufficient for the question it was proposed to answer — this
is the reason it is not adopted.** It fires on a **raised direct root requirement**. The checksum
question is a **selected version with no `h1:` anywhere**, and that already fails hard at **every**
setting of the flag. The two conditions are not the same set in either direction: raises of
**indirect** requirements are **never reported at all** (`root_versions` is populated only for
non-indirect tags), and — relevant to this harness specifically — `_go_mod_text` emits **no
`// indirect` markers**, so **every** requirement in the union `go.mod` counts as direct.

**Options considered, each rejected on evidence.**

**(a) Emit `error` and raise the harness's pins to the selected versions.** Rejected: it does not
scale, and it **relocates the problem into the source repos**. The pins come from the **fixture
repos' own `go.mod` files**, so in production "remediation" means editing the **source repos'
manifests** — which a migration harness must not do silently. And because the raise is a property of
the **pinned rulesets' floors**, the pins would need re-raising on **every `ruleset_versions`
bump, for every Go repo, forever**. §25's corpus survey (**25 Go repos, 68 `go.mod` files**) makes
it near-certain at least one pins below some floor.

**(b) Emit `error` and mark the affected repo `REQUIRES_HUMAN_INTERVENTION`.** Rejected as
**unimplementable as stated**: the failure is at **extension-evaluation time**, before analysis and
before any per-repo attribution exists, and it aborts the whole monorepo — there is no repo to
mark. The DEBUG-parsing variant (leave the tag off, scrape the `DEBUG:` line) is unsound for a
**separate measured reason**: **Bazel caches module-extension results**, so the `print` appears only
on the evaluation that **actually ran**. A detector that is silent on a cache hit is **a check
nobody ran**, which is `docs/INTEGRATION_HONESTY.md`'s own standing thesis.

**(c) Resolve `go.sum` against the union plus the rulesets' known floors.** Rejected as **not
soundly knowable**: it requires an enumeration of **every Bazel module in the graph that calls
`go_deps.from_file`** — which the harness does not hold, and which **changes with BCR
transitives** — plus a mapping from **BCR module version to Go module version**. Hardcoding
rules_go + gazelle and assuming BCR-version ≡ Go-module-version is the **guessed-constant** shape
**D8** and **Rule 11** forbid; it would **diverge silently on the next pin bump**, reproducing D19
in a **harder-to-see** form.

**(d) Emit nothing, pin the measurement with tests — ADOPTED.**

**What was implemented: nothing in `src/`, two tests.**

1. A **real-Bazel** test deletes the single uncovered `h1:` line and asserts Bazel **refuses to
   fetch**. In the same run it pins the other half by asserting the raise line begins with
   `DEBUG: ` and **not** `Error in fail:` — so a future flip to `error` turns **red with an
   explanation attached** rather than turning red mysteriously.
2. A **free, offline** test asserts the rendered root `MODULE.bazel` contains no `go_deps.config`
   and no `check_direct_dependencies`.

**The reason both exist is that the safety property belongs to the pinned gazelle version, not to
any harness code.** A bump restoring older fetch-anyway behaviour would reopen a **real** hole
**with no diff anywhere in this repository** — the class of regression no code review can catch,
which is exactly why the measurement is machine-checked instead of merely written down.

**Residual risk, in the form a non-expert can act on.** The monorepo's `go.sum` is **not** a
statement about which versions the monorepo builds against — it is **one of several** checksum files
Bazel consults. For any dependency a **pinned ruleset** requires more recently than your repos do,
**the version and the hash that govern the build come from that ruleset's lock file**, which moves
when `build.ruleset_versions` moves. **Guaranteed:** an attacker cannot substitute bytes — a version
nobody covers fails the build outright. **Not guaranteed:** an operator cannot read `go.sum` and
know what shipped; that needs a **post-build attestation read out of Bazel**, not a pre-build lock.

*Agent Recommendation:* if that readability gap is ever worth closing, the shape is a
**post-build attestation** — read the resolved module versions and hashes **out of Bazel** after the
build (e.g. from the module extension's own resolved state) and publish them beside the tree — not
a richer pre-build lock, which options (a) and (c) show cannot be made sound from inputs the harness
holds. It is **not** done here, and it is a **judgement call**, not a requirement of `CLAUDE.md` or
any reference.

**Consequences.**

1. **D19 is NARROWED AND ACCEPTED, not OPEN.** Its item 2 is marked **FALSE in place** in
   `docs/INTEGRATION_HONESTY.md`; its item 1 is **WITHDRAWN** rather than pending; what stands is
   the **inherent** MVS raise plus the **readability** limitation above.
2. **The inherent third is unchanged and unfixable inside the harness.** `go_deps` runs MVS over the
   **whole Bazel module graph**, and the harness cannot resolve against a graph it does not
   enumerate. ADR-0058's first bullet stands verbatim.
3. **`go_deps.config` is now a decision with a test behind it, not an omission.** The offline test
   makes re-adding the tag a **deliberate, visible** act.
4. **The tests are pinned to gazelle 0.52.2 and say nothing about any other version.** That is the
   admitted scope, and it is why consequence 3 of ADR-0058 could not simply be deleted.
5. **This round proves nothing new about `fleet build`, `go_test`, non-linux/amd64 platforms, or a
   fleet with conflicting Go module versions across repos.** The tree is still **assembled**, not
   published.

---

## ADR-0060 — A **discoverable C compiler is a precondition of every Bazel verdict**, not a cgo concern: `buildverify` probes for one **before the first `bazel`**, in **sandboxed runs only**, and refuses **non-retryably** — with a probe that mirrors rules_cc's `_find_generic` clause for clause, because this ADR's own first implementation probed the wrong predicate **in both directions**

**Decision.** `BuildverifyWorker._c_toolchain_gate` runs when `payload.image is not None`, after
`require_free_space` and **before the first `bazel` invocation**, executing `C_TOOLCHAIN_PROBE`
inside the sandbox image via one `docker run`. On failure it returns a `WorkerError` with
`failure_class=BUILD_ERROR` and **`retryable=False`**. It is deliberately **not** part of
`preconditions_hold`. The probe is a **line-by-line mirror of rules_cc's `_find_generic(ctx,
"gcc", "CC", overridden_tools)`** — `gcc` and a stripped `$CC` only — not the three-name
`cc`/`gcc`/`clang` lookup the first implementation shipped.

**The finding that forced the gate, and the measured claim is stronger than the one that motivated
it.** The incoming claim was "cgo packages need a C toolchain in the image." Measured: **without a
discoverable C compiler, ZERO Go targets analyse — cgo or not** — because `@@rules_go+//:stdlib`,
the Go standard library itself, depends on
`@@rules_cc++cc_configure_extension+local_config_cc//:cc-compiler-k8`. Verbatim, in order:

    Auto-Configuration Error: Cannot find gcc or CC; either correct your path or set the CC
    environment variable
    ERROR: no such package '@@rules_cc++cc_configure_extension+local_config_cc//': …
      … @@rules_go+//:stdlib … failed to fetch it
      … //go/digest:digest … failed to fetch it
    INFO: Found 0 targets

That `Found 0 targets` is under **`--keep_going`**, which `BuildverifyInput.keep_going`
**defaults to** — so the harness's own default flag turns a total refusal into a line an operator
reads as "nothing matched". `--nobuild` and `cquery` fail identically; `--network=none` is not the
cause, the lookup is local.

**Where it fails, precisely — and this is one of the three statements the first implementation got
wrong.** Not "the analysis phase": the lookup runs while the `local_config_cc` **repository is
being fetched**, at loading time, and the failure is a Starlark `auto_configure_fail`. The
`Found 0 targets` / non-zero exit is what an operator **sees** downstream of that `fail()`, not
the mechanism of it. Both statements are true; only one names the thing to fix.

**Why this was invisible until now.** The word **`cgo` appeared nowhere in `src/` or in any ADR**
before this round. Neither Go fixture uses cgo, so the suite was **structurally blind** to the
question; and the host carries **`gcc` 13.3.0** (no clang), so every unsandboxed run resolved a
compiler off the same `PATH` `bazel` came from and never noticed the dependency existed. **Every
green Bazel verdict this project has ever recorded rested on an undeclared host compiler.**

**Corpus exposure, from a read-only survey of 267 bare Gitea repos (no credential read or
logged).** **31 Go repos** — the figure this project has been carrying since §25, **25**, was
low — and **10 of 31 carry cgo**, roughly **6 distinct codebases after dedup**. Two of them
(`beads`, the largest by file count, and `multi-agent-vllm`) gate on **`//go:build cgo` with zero
`import "C"`**, so the obvious `import "C"` grep misses them entirely. **But per the `//:stdlib`
finding the missing-compiler exposure is 31/31, not 10/31.** The 10/31 figure governs only the
**second-order** problem below.

**Why a gate and not a classification.** The failure arrives as a bare exit 1, which
`classify_build_failure` reads as a **retryable** `BUILD_ERROR`. Left alone it spends all three
ADR-0014 rungs — two of them LLM-bearing, prompting a model to repair a `BUILD.bazel` that is
fine — then marks the repo `REQUIRES_HUMAN_INTERVENTION`, leaving an operator to rediscover
`//:stdlib` from a stderr about a repository fetch. Refusing costs one `command -v`.

**Why NON-RETRYABLE, and it is the same mechanical argument as exit 127.** Re-running an identical
rung cannot install a compiler any more than it can install `bazel`. `retryable=False` is what
makes `RetryPolicy.decide` answer `TERMINATE` rather than spend the ladder.

**Why NOT in `preconditions_hold`, which is the structural half of this decision.** That method is
consulted only by `PhaseRunner._re_entry`, only when a checkpoint already exists, and its `False`
means *"the tree is unusable — re-run the phase from `base_ref`"*. A compiler-less image answering
`False` there would be sent **around the loop again** instead of being stopped: the gate's verdict
is "this environment cannot produce any verdict", which is not the same proposition as "this tree
needs rebuilding". So it sits where the other refusal that costs the run its attempt sits —
beside `require_free_space`, before the first invocation.

**Sandboxed runs only.** `payload.image is None` is the host path, where an unsandboxed run has
already resolved `bazel` off the same `PATH` a compiler would come from; a gate there would spend
a `docker run` to learn what the very next command learns for free, and would fail every
offline/dry-run test that has no Docker daemon at all. The image is the case where the contents
are **unknown** — see the consequence on `settings.verify.container_image` below.

**The probe mirrors `_find_generic` clause for clause — and THE FIRST IMPLEMENTATION OF THIS ADR
PROBED THE WRONG PREDICATE, IN BOTH DIRECTIONS. It is recorded here as a defect of this decision's
own first pass, not smoothed over.** The first version ran
`command -v cc || command -v gcc || command -v clang`. Bazel's actual lookup, **verified against
`cc/private/toolchain/unix_cc_configure.bzl` in this checkout's own bazelisk repository cache**, is
`_find_generic(repository_ctx, "gcc", "CC", overridden_tools)`, resolving in order:
`overridden_tools` (unreachable from an image — nothing here passes any) → the environment's `CC`,
**stripped**, and if what remains is non-empty it **replaces** the default rather than
supplementing it → the literal name `"gcc"` → `repository_ctx.which(...)`, with an **absolute**
`CC` short-circuiting `which` and returned **unvalidated**. Therefore **`cc` is never searched at
all**, and **`clang` is never a lookup candidate** — it appears upstream only inside `_is_clang`,
which *classifies* a binary the lookup already found. The old probe was wrong twice:

- **Gap A — false green.** A **clang-only** image (or one where `cc` is a symlink to clang and no
  `gcc` exists) **passed** the probe, and Bazel then refused the build anyway. A gate that passes
  the environment it exists to reject is worse than no gate: it converts "we did not check" into
  "we checked" without anybody deciding to.
- **Gap B — false non-retryable refusal.** An image carrying `ENV CC=/opt/toolchain/bin/gcc` (or
  `ENV CC=gcc-13`) **failed** the probe though Bazel would have succeeded — and failed
  **non-retryably**, which is the most expensive possible way to be wrong.

The corrected `C_TOOLCHAIN_PROBE` is one `sh -c` string whose arms are those clauses: two
parameter expansions for Starlark's `.strip()`; `""` ⇒ `command -v gcc` (the default name reaching
`which`); `/*` ⇒ the absolute short-circuit; the final arm ⇒ a relative `$CC` reaching `which`.
**One deliberate divergence:** upstream returns an absolute `CC` unvalidated where the probe tests
`[ -x "$cc" ]` — not stricter in practice, since the next thing `configure_unix_toolchain` does
with that path is `execute([cc, "-E", …])`, so the probe asks one step early the question the
fetch asks anyway. `command -v` is POSIX `sh`, so no `which(1)` need exist; the shell is the only
assumption, and an image without one fails with 127, the same verdict for the same reason.

**How the corrected probe is tested, and why the obvious test would not have caught either gap.**
The tests **execute the probe program against stub binaries on `PATH`** rather than replying to it
with a hand-chosen exit code. A fake that merely returns an exit status **cannot distinguish a
clang-only image from a gcc one** — it would have agreed with the broken probe and the corrected
one identically. **Mutation-checked:** restoring the three-name probe fails the Gap A test and all
three Gap B parameters.

**Three false statements in the gate's own operator message were corrected too**, because an
operator message that misdescribes the failure is a defect with a longer half-life than the code:

1. It said the probe looks for **gcc/cc/clang**. It looks for **`gcc` and a stripped `$CC`**, only.
2. It said the failure is in **"the analysis phase"**. It is at **repository-fetch/loading** time
   via Starlark `fail()`; the `Found 0 targets` symptom is downstream and stands as a symptom.
3. It offered **"(or set `CC`)"** as a remedy, which was **unactionable**: `buildverify` passes no
   `env` to either container, so a host `CC` never reaches the image and **only an image-level
   `ENV CC` counts**. The message now says so.

**Known limit — a compiler is the floor, not sufficiency.** Gazelle emits `cgo = True`
automatically for any package containing `import "C"`, so PASS 4 will faithfully publish cgo
targets; those additionally need the headers and system libraries their `#cgo` directives name,
and the corpus's cgo repos reference **`ole32`, `crypt32`, `IOKit`, `sqlite3`** — none of which a
minimal image carries and none of which this probe looks for. **This is where 10/31 applies.**
Those failures arrive as ordinary per-repo `BUILD_ERROR`s from the build itself. The gate removes
exactly one case: the one where **nothing** analyses.

**One thing that must NEVER be done, recorded because it is the exact failure this project exists
to catch.** `BAZEL_DO_NOT_DETECT_CPP_TOOLCHAIN=1` makes the missing compiler go away by
**silently substituting an empty toolchain** — a green Bazel run over a tree whose C toolchain
does not exist. It is not a mitigation, it is the green-with-a-broken-tree result this ledger was
written to prevent, and it is not set anywhere in this repository.

*Agent Recommendation (judgement calls, not requirements of `CLAUDE.md` or any reference).* Three
choices here are the agent's and are labelled as such: **(i)** classifying the refusal as
`BUILD_ERROR` rather than adding a new `FailureClass` — the operator-visible fact is
`retryable=False`, and a new class would have to be threaded through `RetryPolicy`, the state
schema and the reporting for no behavioural gain; **(ii)** placing the probe in its own
`--rm` container named `…-cc-probe` rather than reusing `spec_for_attempt`'s name, because
`on_cancel` force-removes that name and a shared probe would **race the build container it
precedes**; **(iii)** the `[ -x "$cc" ]` divergence above. Each is reversible and none is derived
from a directive.

**Consequences.**

1. **Every Bazel verdict in `docs/INTEGRATION_HONESTY.md` gains a named precondition it never
   declared.** A green Bazel result recorded by this project has always depended on an
   **undeclared host compiler**; the gate declares it for sandboxed runs and the ledger now says
   so for the rest.
2. **The §25 corpus figure of "25 Go repos" is corrected to 31** in `docs/INTEGRATION_HONESTY.md`.
   The derived ratios computed over the 25-repo enumeration (68 non-vendor `go.mod`s, 13 of 68 at
   `go` ≥1.25, 9 root-`go.mod` repos, 2 of 25 vendoring) were **not** re-measured over the
   corrected set and are left as historical figures.
3. **A larger blocker was found beside this one and is deliberately LEFT OPEN.**
   `verify.disk_cache` and `verify.repository_cache` are bind-mounted **RW** into the verify
   container, but `_bazel_argv` **never emits `--disk_cache=` or `--repository_cache=`** — the
   mounts are **inert**. With `--network=none`, a cold sandboxed run therefore **cannot resolve a
   single Bazel module** — `go_sdk.download` and every BCR `bazel_dep` included — **compiler or no
   compiler**. This is arguably a **larger** blocker than the C toolchain. It is recorded in
   `_bazel_argv`'s own docstring and owned by a later task; **it is not fixed here**, because
   fixing it is a separate decision about cache identity and ADR-0014 re-runnability.
4. **`settings.verify.container_image` is a placeholder and nothing in this repository builds it.**
   It names `ghcr.io/acme/fleet-build:2026-08`; **there is no Dockerfile anywhere in this
   repository**; a registry probe returned `denied`/403 without authenticating, which
   **distinguishes nothing** (GHCR answers identically for private and nonexistent), and `acme` is
   this project's canonical placeholder org. **No test exercises a real image.** The minimum
   contents, enumerated from what actually executes inside the container: a **shell**; a **real
   pinned `bazel`** — *not* bazelisk, since `--network=none` cannot download a version; a binary
   named **`gcc`** or an image-level **`ENV CC`**; and a **writable HOME for the run uid**. Not
   needed: a JDK (the Bazel release binary embeds a JRE), a system `go`, or `gazelle` — gazelle
   runs **on the host** from the vendored binary per **ADR-0056**, and the Go SDK arrives via
   `go_sdk.download`.
5. **`rdepverify` runs containerised Bazel with the same image and has NO such gate.** Named here
   so it is a known omission rather than an oversight.
6. **What this ADR does NOT prove, and it is most of the operational surface.** **No real
   container image was ever probed** — every gate test evaluates the probe against **stub files on
   the host**, no Docker daemon ran, and the argv → container behaviour is asserted as
   **constructed argv only**. **Bazel was never run against a clang-only or `CC`-carrying
   environment**: the claim that it refuses the first and accepts the second rests on **reading**
   `unix_cc_configure.bzl`, not executing it, and the one real-Bazel test in this area strips
   `gcc`/`cc`/`clang` **and** `CC` together, so it does **not** discriminate Gap A from Gap B. The
   probe's shell portability was checked on this host's `sh`/`dash`/`bash` only — **not busybox
   `ash`**, which is what an Alpine image would use. **Nothing was proven about cgo actually
   building**: no headers or libraries were probed.

**Amended by ADR-0062 — consequence 4 is retired, consequence 3 is closed by ADR-0061/ADR-0063, and
consequence 6's busybox clause is now measured.** `docker/fleet-build.Dockerfile` exists and builds,
and `settings.verify.container_image` is the **local tag** `fleet-build:9.2.0-bookworm`, not a
registry placeholder; the minimum contents this consequence enumerated were **met**. The cache
mounts are no longer inert (ADR-0061 for build/test, ADR-0063 for `query`). The probe **was**
verified working under **busybox `ash`**, so the shell was never the reason Alpine was rejected —
**glibc was** (ADR-0062). **Consequence 5 stands unchanged: `rdepverify` still has no gate.** And
**nothing here makes a sandboxed run work**: the repository cache the container is handed is created
and never populated, so a cold `--network=none` run still resolves no module — see ADR-0062 blocker
**B2**.

---

## ADR-0061 — A shared Bazel cache is **one object behind both the mount and the flag**: `CacheMount` renders the bind mount *and* the `--disk_cache=`/`--repository_cache=` that names it, on **the side bazel actually runs on** — and Phase 4 gets those same flags **unsandboxed, unconditionally**

**Decision.** `CacheMount` (`workers/buildverify.py`, a `FleetModel` with
`role: Literal["disk","repository"]` and an absolute host `path`) is the **single object** behind
both halves of a shared cache: `mount()` yields the `Mount`, and `flag(sandboxed=...)` yields
`--disk_cache=…` / `--repository_cache=…`. `cli._cache_mounts()` returns role-tagged
`CacheMount`s instead of a bare path list. `_bazel_argv` emits one flag per mount with
`sandboxed=payload.image is not None`, **before** `extra_args`. `RdepverifyInput` carries the same
`list[CacheMount]`, forwarded by `VerifyWorker._rdeps`, and renders its flags with
**`sandboxed=False` unconditionally**.

**This closed a SPEC violation, not an optimisation, and the SPEC had already named the flags.**
`settings.verify.disk_cache` and `verify.repository_cache` were bind-mounted **read-write** into
the verify container at `/cache/<name>`, but `_bazel_argv` emitted only `--keep_going`,
`--build_event_json_file`, `--jobs` and `extra_args` — **never `--disk_cache=` or
`--repository_cache=`**. The mounts were **inert** unless an operator hand-wrote the flags into
`extra_args`. **SPEC §3.4's bounds table specifies exactly those two flags**, "mounted read-write
and shared across containers and attempts", so the code **mounted them and never named them**.
With `verify.network = "none"` the consequence is not a lost cache hit: a cold sandboxed run
**cannot resolve `go_sdk.download`, any BCR `bazel_dep`, or any module at all** — compiler or no
compiler (this is the gap ADR-0060 consequence 3 recorded and deliberately left open).

**Why one object rather than two cooperating call sites.** The mount TARGET and the flag VALUE are
both derived from `path`, so renaming one **is** renaming the other and they cannot drift. `role`
exists because the two flags are **not interchangeable** and nothing in a host path says which is
which; the previous `_cache_mounts()` conveyed that **by position**, which is the kind of
convention that holds until someone appends a third directory.

**The sandboxed/unsandboxed branch, and why getting it backwards is silent rather than loud.**
`flag(sandboxed=...)` branches on `payload.image is not None`: the **container-side**
`/cache/<name>` target when sandboxed, the **host** path when not. Unsandboxed runs —
`--no-sandbox`, every offline test, every host-side dry run — have **no container at all**, and
`/cache/<name>` there would be created **at the host filesystem root**. The mirror error is as bad:
a host path handed to the container names a directory that is **not mounted there**, so bazel
creates it inside the container and `--rm` deletes it. **Neither direction fails loudly. Both throw
the cache away** — which is precisely the shape of defect this project's ledger exists to catch, so
the sandboxed tests read the expected value **back out of the emitted `--volume=` flags** rather
than from a literal: renaming one side breaks the test instead of silently diverging.

**`extra_args` still wins, and that was checked against the vendored binary rather than assumed.**
Measured with the pinned Bazel 9.2.0:

    bazel canonicalize-flags --for_command=build -- \
      --disk_cache=/a --disk_cache=/b --repository_cache=/r1 --repository_cache=/r2
    → --disk_cache=/b
      --repository_cache=/r2

Neither option is `allowMultiple`, so repeats are **last-wins**; both are accepted under
`--for_command=test`; neither is deprecated in 9.2.0. Because the cache flags are emitted **before**
`extra_args`, an operator who hand-wrote them — **previously the only way to reach the mounted
cache at all** — keeps exactly the behaviour they had. This is an ordering guarantee derived from a
measured property of the binary, not from a convention about how argv is assembled.

**Phase 4 had the same gap in a different shape, and `sandboxed=False` there is unconditional on
purpose.** `RdepverifyWorker` **never containerises** — no `image`, no `cache_mounts`, calling
`bazel_test_argv(...)` directly on the host — so the blast-radius `bazel test` ran with **no disk
cache and no repository cache at all**, re-executing from cold what Phase 3 had just cached. SPEC
§3.4 places the persistent-cache row **inside the Phase 4 section** and names `bazel/query.py`
among its enforcement points, so this was the same specified-but-unenforced bound, one phase over.
The fix is minimal: `cache_mounts: list[CacheMount]` on `RdepverifyInput`, forwarded in
`VerifyWorker._rdeps` (`VerifyInput` had carried the value all along — it was **one missing line
among twelve forwarded fields**), flags rendered `sandboxed=False`. **Unconditional, because there
is no container to be inside**: `ContainerSandbox` appears in that worker **only** as `on_cancel`
cleanup of a container it never creates, which is a reference, not evidence of containerisation.
**This is the one clause of this ADR that must change if Phase 4 is ever containerised** — at that
point the flag must take the same `payload.image is not None` branch `buildverify` takes, and a
fixed `False` would name a host path the container does not have. Non-vacuity of the forwarding was
confirmed by **reverting `_rdeps` and watching the e2e test fail**.

**One model, imported — not a second source of truth.** `CacheMount` stays in `buildverify` and is
imported by `rdepverify`, which already imports five symbols from it. One model, one flag renderer.

*Agent Recommendation (judgement calls, not requirements of `CLAUDE.md`, the SPEC, or any
reference).* Four choices here are the agent's and are labelled as such: **(i)** siting `CacheMount`
in `buildverify` and importing it into `rdepverify` rather than promoting it to a shared module —
the import edge already exists and a new module would be an abstraction for two callers;
**(ii)** emitting the cache flags **before** `extra_args` rather than after, which is what preserves
the hand-written-flag escape hatch and is only *safe* because of the `canonicalize-flags` evidence
above; **(iii)** deriving the container target as `/cache/<basename>` rather than a role-named
constant, so the flag and the `--volume=` share one string; **(iv)** leaving the rdeps `bazel query`
invocations alone (consequence 4). Each is reversible and none is derived from a directive.

**Consequences.**

1. **SPEC §3.4's persistent-cache bound moves from *specified* to *enforced*, in both Phase 3 and
   Phase 4.** `docs/INTEGRATION_HONESTY.md` records that it was specified-and-unenforced until now.
2. **An operator's hand-written `extra_args` cache flags are preserved by construction**, with the
   last-wins property measured rather than assumed.
3. **A test-infrastructure precedence trap now exists and is recorded, because it cost ~75
   minutes.** A command-line `--repository_cache` **beats** a `.bazelrc` `common
   --repository_cache=` line. `tests/conftest.py` shares one archive cache across the session via
   that `.bazelrc`; the moment the CLI began emitting the flag from a per-workspace default, every
   real-Bazel e2e test silently received an **empty** archive cache and re-fetched every ruleset
   from the live registry — the first full-suite attempt was still running at **~75 minutes** when
   it was caught. Fixed in `test_build_e2e.real_build()`, which appends
   `verify: repository_cache: <BAZEL_REPOSITORY_CACHE>` to the workspace config so the flag and the
   `.bazelrc` name **the same directory**; the suite returned to **~12m34s**. This is a **mechanism,
   not weather**: it presents exactly like the live-network flake class this project tracks and is
   **not** a member of it.
4. **A real gap remains and is deliberately unfixed: the rdeps `bazel query` invocations still
   carry no cache flags**, though `rdeps_closure` loads the module graph too. Changing
   `bazel/query.py`'s **query** argv is a separate decision; the absence is **asserted explicitly**
   in the e2e test so it is recorded rather than forgotten.
5. **The sandboxed path is proven at the argv level only.** **No cold `--network=none` run was ever
   exercised**: no Docker was involved, there is still **no in-tree Dockerfile**, and
   `settings.verify.container_image` names an image nothing here can pull (ADR-0060 consequence 4).
   What is proven is that the flags carry the mount targets and the mounts are emitted. The
   **unsandboxed** path *is* exercised against real Bazel 9.2.0 end to end, so "the flags are
   accepted and bazel uses those directories" is proven **there and only there**.
6. **No real Phase 4 run has ever been exercised against real Bazel**, warm or cold — every
   `fleet verify` test uses `FakeBazel` — so what this ADR proves for Phase 4 is **argv
   construction, ordering, and provenance from settings**, nothing about execution.
7. **There is no timed before/after for either round.** The cost claim — Phase 4 re-executing what
   Phase 3 had cached — is **inference from the flags' absence**, not a measurement.

**Amended by ADR-0063 — consequence 4's gap is closed, and consequence 5's Dockerfile clause is
now false.** The rdeps `bazel query` invocations **do** carry a cache flag now: `--repository_cache`
and **only** that one, on both queries, decided by measurement rather than by symmetry with the
build argv (ADR-0063). Judgement call (iv) in the *Agent Recommendation* above — "leaving the rdeps
`bazel query` invocations alone" — is therefore retired. Separately, consequence 5's "**there is
still no in-tree Dockerfile**" is **false as of ADR-0062**: `docker/fleet-build.Dockerfile` exists
and builds, and `settings.verify.container_image` no longer names an unpullable registry image.
**Everything else in consequence 5 stands unchanged** — the sandboxed path is still proven at the
**argv level only**, because no Bazel has ever run inside that image and the mounted repository
cache is still empty (ADR-0062, blocker B2).

---

## ADR-0062 — The verify sandbox image is **in-tree, glibc and local-tagged**: `docker/fleet-build.Dockerfile` pins Bazel **9.2.0** by a digest verified **three ways**, kills the arbitrary-uid **exit-36** death **twice over** — and closes **B1 only**, because an **empty repository cache (B2) keeps the sandboxed path red**

**Decision.** `docker/fleet-build.Dockerfile` — two stages, **332 MB**, ~10s to build — is the image
`settings.verify.container_image` names, and that setting moves from the unpullable
`ghcr.io/acme/fleet-build:2026-08` to the **local tag** `fleet-build:9.2.0-bookworm`. It carries a
shell, the **official Bazel 9.2.0 release binary**, `gcc` + `libc6-dev`, `HOME`/`USER` env for a
passwd-less uid, and a system `/etc/bazel.bazelrc` pinning `output_user_root`. It deliberately
carries **nothing else**.

**Say the limit first: this closes exactly ONE of two blockers, and the sandboxed path is still
red.** ADR-0060 consequence 4 and ADR-0061 consequence 5 each named a reason a cold
`--network=none` verify cannot work. Only the first is closed here.

- **B1 — CLOSED. An image defect that `INFRA_EXIT_CODES` turns into an infinite re-queue.** Every
  sandboxed command runs `--user <uid>:<gid>` (`current_user_spec()`, so the bind-mounted worktree
  does not fill with root-owned artifacts), and that uid has **no `/etc/passwd` entry**. For a
  passwd-less uid Docker sets **`HOME=/`** — unwritable — and leaves **`USER` unset**. Bazel's
  **client**, computing its default output user root, calls `blaze::GetUserName()`, finds neither,
  and dies with `LOCAL_ENVIRONMENTAL_ERROR` = **exit 36**. **36 is in `INFRA_EXIT_CODES`**, so
  ADR-0014 spends **no attempt** on it: the fleet would re-queue the repo **forever** against a
  defect no retry can fix. Reproduced verbatim as a **negative control** in plain
  `debian:bookworm-slim` with the vendored 9.2.0 binary: `HOME=[/] USER=[]` → `FATAL: $USER is not
  set, and unable to look up name of current user` → `bazel exit=36`.
- **B2 — UNTOUCHED, and it is why the path stays red.** `cli._cache_mounts` creates
  `<root>/cache/bazel/repo` with `mkdir(parents=True, exist_ok=True)` and **nothing ever fills it**.
  With `--network=none` and an **empty** repository cache, **no module resolves**, so a sandboxed
  build fails **regardless of the image**. Cache population is a separate task and is deliberately
  not attempted here.

**Base: `debian:bookworm-slim`, and Alpine was rejected on evidence rather than on taste.** The
official Bazel release binary is **glibc-dynamic** — `ELF … dynamically linked, interpreter
/lib64/ld-linux-x86-64.so.2` — and the rules_go / rules_rust / rules_js prebuilt toolchains a
migrated repo pulls through the repository cache are glibc too; musl would need a source build or a
patched loader for each. **The shell was never the objection**: the C-toolchain probe from ADR-0060
*was* verified working under busybox `ash`, so the probe is explicitly **not** the reason Alpine
lost, and recording that keeps a future round from re-litigating the wrong question.

**Bazel is the official release binary at a digest verified three ways — not apt, not bazelisk.**
Debian ships no Bazel, and **bazelisk resolves `.bazelversion` by downloading it**, which
`--network=none` cannot do — a bazelisk image would fail at the *first fleet build* rather than at
*image build* time, where a failure is cheap to see. The pin is `sha256sum -c` against
`7668a95d…8694`, and that digest is identical at **`releases.bazel.build`**, the **GitHub release
asset**, and **this repository's own bazelisk download cache**, whose content-addressed directory
name *is* the digest bazelisk itself verified. Three independent sources, one digest. **No JDK
layer**: the release binary is a self-extracting archive that **embeds a JRE**, so `default-jre`
would add ~180 MB nothing executes. The fetch happens in a **separate stage** so `curl` and its CA
bundle never reach an image that runs without a network.

**The C toolchain is `gcc` + `libc6-dev` and nothing more, and `ENV CC` is deliberately absent.**
`local_config_cc` resolves the literal name **`gcc`** on `PATH` (ADR-0060's probe mirrors that
lookup line for line); `libc6-dev` supplies the headers and `crt*.o` the very next step needs; `ar`,
`ld`, `nm`, `objdump`, `strip` arrive as `gcc`'s own binutils dependency, and the optional tools
Bazel cannot find degrade to `/bin/false` rather than `fail()`. **Not `build-essential`** (56
packages vs **41**) and **not `g++`** — an unused compiler is an unaudited one. **`ENV CC` is
unset on purpose**: a non-empty `CC` **replaces** the `gcc` default in that lookup rather than
supplementing it, so a wrong or stale `CC` is **strictly worse than none**.

**The uid fix is deliberately two-layer, and the second layer is structural rather than
cosmetic.** Layer one: `ENV HOME=/home/fleet`, `ENV USER=fleet`, with `/home/fleet` at mode
**0777** because **the uid is unknown at build time** (the fleet passes the *host* user's uid/gid,
which differs per machine, so no `chown` here can be correct) — and **image `ENV` survives
`--user` with no `--env`, verified**, which matters because `buildverify` emits `docker run` with
**no `--env` at all**. Layer two: `/etc/bazel.bazelrc` — Bazel's **system** rc, a compile-time
constant read before the workspace `.bazelrc` and before any command line — carries
`startup --output_user_root=…`, so the client **never needs a user name to build a default path**
and **`GetUserName()` is never reached**. That removes the exit-36 path **structurally** instead of
papering over it. **This cannot be done from the harness side**: `output_user_root` is a **startup**
option and must precede the verb, while `_bazel_argv` appends every flag **after** `build`/`test`,
where Bazel rejects it as `COMMAND_LINE_ERROR` — **exit 2**. The harness therefore *cannot* pass it
at all, which is why the rc file is in the image and not in the argv.

**The omissions are deliberate, each with a named trigger, and they exist to keep B2 visible.**
`git`, `patch`, `unzip`/`xz-utils`, `python3`, `g++`, cgo system libraries, and any Go/Node/Rust
toolchain are **absent by design**: those are supposed to arrive through the **mounted repository
cache**, and baking them in would **mask B2** — an empty cache would present as a slow-but-working
image instead of the hard failure it is. Triggers to revisit are named in the file: stderr naming
the missing binary (`git` for a `git_repository`, `patch` for a `bazel_dep` with `patches = [...]`),
a linker error naming a system library (`-lsqlite3` ⇒ `libsqlite3-dev`), or a C++ source failing to
link (⇒ `g++`).

**A local tag, because this fleet has no registry — and the failure mode is now loud.** There is no
container registry here, so a registry-shaped name could only ever fail to pull. With a local tag,
an **unbuilt** image fails at `docker run` with **125**, immediately and with a remedy that is a
`docker build` command.

**A misattribution fixed alongside it.** `_c_toolchain_gate` reported **"no C compiler in the
sandbox image"** for **any** non-zero probe exit — **including `docker run`'s 125**, which means the
container **never started** (absent or unpullable image, unreachable daemon, invalid flag). An
unbuilt `docker/fleet-build.Dockerfile` is now the *most likely* 125 there is, so reporting it as a
missing compiler would send an operator to the wrong file. The gate now **branches on 125** with a
message naming the likely causes and the `docker build` remedy. **The classification is unchanged**
— `BUILD_ERROR`, non-retryable — because both verdicts are "fix the image", only the pointed-at file
differs.

*Agent Recommendation (judgement calls, not requirements of `CLAUDE.md`, the SPEC, or any
reference).* Six choices here are the agent's and are labelled as such: **(i)** `debian:bookworm-slim`
over any other glibc base (the glibc *requirement* is measured; the *distro* is a preference);
**(ii)** the two-stage split to keep `curl` and the CA bundle out of a network-less image;
**(iii)** `gcc` + `libc6-dev` over `build-essential`, and omitting `g++` until a build names it;
**(iv)** doing the uid fix **twice** — `ENV` *and* the system rc — rather than trusting either alone;
**(v)** the tag string `fleet-build:9.2.0-bookworm`, encoding the Bazel version and base so a stale
image is nameable; **(vi)** branching `_c_toolchain_gate` on 125 rather than widening the existing
message. Each is reversible and none is derived from a directive.

**Consequences.**

1. **B1 is closed; B2 is not, and the sandboxed path remains red.** An image now exists that the
   fleet can actually run, and the arbitrary-uid exit-36 infinite re-queue is removed on two
   independent layers. **A sandboxed build still cannot succeed**, because `<root>/cache/bazel/repo`
   is created and never populated and `--network=none` cannot fetch a single module. **Populating
   that cache is the next blocker and it is not this ADR's.**
2. **No Bazel has ever run inside this image.** The integration test checks `command -v bazel`
   resolves a path — **which is not executing it** — plus that `HOME`/`USER` survive `--user`. The
   exit-36 *mechanism* was reproduced in a **separate plain Debian container**, and the *fix* is
   argued **structurally**: the `ENV` survival was verified, the rc file was written and the
   `GetUserName` symbol was verified present in the shipped binary, but **the fixed code path was
   never executed**.
3. **The image is not proven *sufficient* for any real repository.** No Go/Node/Rust toolchain, no
   `git`/`patch`/`unzip`/`python3`, no `g++`, no cgo system libraries. The first real migrated repo
   is expected to name something missing; the triggers above are how that is meant to be read.
4. **The new integration test gates on the image being present *locally* and never pulls**, so a
   **stale locally-tagged image would still pass it**. The test proves "an image with this tag
   exists and has these properties", not "the shipped Dockerfile produced it".
5. **`settings.verify.container_image` now points at something buildable**, and an unbuilt image
   fails **loudly at `docker run` with 125** with a `docker build` remedy, rather than being
   misreported as a missing compiler.
6. **Docstrings in `buildverify._c_toolchain_gate`, `tests/test_workers_build.py`,
   `tests/test_build_e2e.py` and `tests/test_sandbox.py` asserted "there is no Dockerfile in this
   repository" and were corrected.** ADR-0061 consequence 5 and `docs/INTEGRATION_HONESTY.md`
   carried the same claim and are corrected too.
7. **`rdepverify` still has no C-toolchain gate** (ADR-0060's open item), and this ADR does not
   change that.

---

## ADR-0063 — The rdeps `bazel query` names the **repository cache and only it**: both flags parse, only **one of them does work**, and **a flag on the line implies a working cache**

**Decision.** `bazel/query.py`'s `query_argv` gains `cache_flags: Sequence[str] = ()`, emitted on
**both** the closure query and the depth-1 query, fed by `RdepverifyWorker._query_cache_flags`,
which renders **`--repository_cache` only** — **no `--disk_cache`** — at **host paths**, with
`sandboxed=False` **unconditionally**. This closes the gap ADR-0061 consequence 4 recorded and
deliberately left open.

**Both flags parse, so this was never a `COMMAND_LINE_ERROR` risk in either direction.** Measured on
the vendored 9.2.0:

    bazel canonicalize-flags --for_command=query -- --disk_cache=/a --repository_cache=/r
    → --disk_cache=/a
      --repository_cache=/r

Both echoed back, **exit 0**. So neither flag is the exit-2 `COMMAND_LINE_ERROR` that
`UNREPEATABLE_EXIT_CODES` would burn an attempt on, and the decision below is **not** a safety
decision.

**Parsing is not using, so it was measured.** A probe `bazel query 'deps(//:all)'` over a workspace
with **one `bazel_dep`** wrote **2.3 MB into `--repository_cache`** and **zero files into
`--disk_cache`** (only an empty `tmp/`). **Read-back was proven separately**: a rerun from a
**fresh `--output_user_root`** against that same repository cache, with
**`--repository_disable_download`**, resolved the **whole module graph** and answered the query,
**exit 0** — the cache was read back with the network refused, which is the property that matters
under `verify.network = "none"`.

**Why the disk cache is deliberately NOT emitted.** `--disk_cache` is documented as a directory
where Bazel reads and writes **actions and action outputs**. `query` runs the loading half of a
build and **executes no actions at all**, so there is nothing for it to hold — which is exactly what
the zero-file measurement shows. The rationale recorded in the code is the one that generalises:
**a flag on the line implies a working cache**, so emitting a provably inert one is a claim the
measurement does not support. This is an **omission by evidence**, not an oversight, and it is
recorded so a later round does not "fix" the asymmetry by symmetry.

**`query_argv` takes pre-rendered flag strings, not `CacheMount` objects, and that is a layering
decision.** `CacheMount` lives in `workers/buildverify.py`, and `bazel/` sits **under** `workers/`
in this project's dependency direction. Importing the model down into `bazel/query.py` would
**invert the layering**, and re-implementing `flag()` there would **clone the one flag renderer**
ADR-0061 exists to keep singular. Passing already-rendered strings keeps both properties: one
renderer, no inverted edge.

**`sandboxed=False`, unconditionally, for the same reason ADR-0061 gives for the `bazel test`
wiring.** `RdepverifyWorker` has no `image`, builds no container and emits no `--volume=`, so
`/cache/<name>` names nothing on this host and a container-side path handed to a host process
would create it at the filesystem root. **This clause changes the day Phase 4 is containerised**,
exactly as ADR-0061's does.

**The e2e assertion that pinned the *absence* was updated, not deleted.** ADR-0061 consequence 4
recorded the missing flags by **asserting them absent** in the e2e test. That assertion now derives
its expectation from **`cli._cache_mounts()`** rather than from a literal, so it pins the new
behaviour the same way it pinned the old one — a rename on either side breaks the test instead of
silently diverging.

*Agent Recommendation (judgement calls, not requirements of `CLAUDE.md`, the SPEC, or any
reference).* Three choices are the agent's: **(i)** emitting **only** the repository cache when both
flags parse — the measurement says the disk cache is inert for `query`, and the alternative
(emitting both for symmetry with the build argv) is defensible and was rejected; **(ii)** the
`Sequence[str]` signature over an inverted import; **(iii)** carrying the flags on the **depth-1**
query as well as the closure query, on the grounds that both load the module graph.

**Consequences.**

1. **ADR-0061 consequence 4 is closed**, and its *Agent Recommendation* item (iv) is retired.
2. **The argv is proven; an end-to-end cache *hit* through the worker is not.** **No real
   `bazel query` runs through `RdepverifyWorker` anywhere in the suite** — every `fleet verify`
   test uses `FakeBazel` — so what the suite proves is **argv construction**. The hit evidence is an
   **out-of-band probe** over a workspace with **one `bazel_dep`**, on **Bazel 9.2.0 only**.
3. **The disk-cache omission is a recorded decision, not a missing feature.** Anyone re-adding it
   owes a measurement in which it holds a file.
4. **`query` is now the third invocation site** (after Phase 3's build and Phase 4's test) whose
   cache flags are derived from one `CacheMount` renderer. There is no fourth.

---

## ADR-0064 — `MODULE.bazel.lock` is a **published artifact**, read from the build worktree as planned bytes and committed **under the integration mutex** — deliberately **not** a fleet-wide root file and **never staged into a dispatch commit**, because two ecosystems' locks are an `add/add` conflict

**Decision.** `_publish` reads `MODULE.bazel.lock` **once**, at the **build worktree root** — the
directory the VERIFY unit just handed Bazel as `cwd` — and carries those bytes through
`materialize` as **planned bytes**. That is ADR-0056's shape exactly: **bytes a tool produced,
captured where it wrote them**. There is **no `content` floor and no `carry_from`** for this path:
a lock the harness invented is **not a resolution anything performed**, and the two mechanisms this
project uses for files it can author would both produce one. When the file is absent the publish
**succeeds and warns** (`module_lock_absent`); when it is published, `BuildOutput` records it in a
new `module_lock_published` flag.

**The problem this exists to solve was misdiagnosed until it was measured, and the ADR must carry
the measurement.** §31 and ADR-0062 recorded the sandboxed path as red because the **repository
cache is empty (B2)**. A controlled experiment matrix in the real `fleet-build:9.2.0-bookworm`
image, under real `docker run --network=none --user $(id -u):$(id -g)` with the cache
bind-mounted, says the missing artifact was **never the cache**:

| cache | lockfile | registry on argv | result |
| --- | --- | --- | --- |
| warm | **none** | none | **exit 32** |
| warm | matching (bcr-keyed) | none | **exit 0** |
| **empty** | matching | none | exit 32 |
| mirror-warmed | mirror-keyed | none | exit 32 |
| **mirror-warmed** | **bcr-keyed** | none | **exit 0** |
| mirror-warmed | mirror-keyed | `--registry=<mirror>` | exit 0 |

The failure is **identical in every red row and always before analysis**: `ERROR: Error computing
the main repository mapping: Error accessing registry https://bcr.bazel.build/: Failed to fetch
registry file … Unknown host: bcr.bazel.build`. **Three conclusions, each isolated by a controlled
pair.** (i) **A warm cache alone resolves nothing** without a lockfile — with no
`registryFileHashes` map Bazel must reach the **network** for registry metadata, so row 1 is red
against a cache that makes row 2 green. (ii) **A lockfile alone is not enough either**, because the
registry files themselves live in the cache (row 3). (iii) **The registry mismatch is a property of
the LOCKFILE's URL keys, not of the cache**: a **mirror-warmed** cache with a **bcr-keyed** lock is
**green**, because both BCR addresses serve **byte-identical** files and the cache is
**content-addressed**. So the mismatch §31 recorded as an unresolved blocker is **fully avoidable**
by warming and locking under the **same registry the container uses**.

**And the harness had never generated, carried or mentioned one.**
`grep -rn "MODULE.bazel.lock\|lockfile_mode" src/ tests/ docs/` returned **nothing**. This is the
design change the project had not made, and it is why the obvious next task (seed the cache) was
**refuted before it was dispatched**.

**Why it is NOT a fleet-wide root file.** The fleet-wide root-file set is computed at **plan time**
from **adapter** data. The lock **does not exist then** — it is written by a Bazel run that happens
later — and it is **no ecosystem's file**: it is **Bazel's record of the root module's
resolution**, one artifact for the whole monorepo, owned by no adapter. Both halves of the root-file
contract (ADR-0053/ADR-0055) would have to be bent to admit it.

**Why it is committed under the mutex and NOT staged into the dispatch commit — this is the
load-bearing part.** Every other fleet-wide root file is **byte-identical across a wave's
dispatches by construction**, so two repos adding the same path with the same bytes **merge
cleanly**. **A lockfile is not**: each worktree records the **module extensions its own build
evaluated**, so a JS repo's lock and a Python repo's lock **of the same wave** differ in
`moduleExtensions`. Staged into the dispatch commit, those are **two branches adding one path with
different content off a common base** — `CONFLICT (add/add)` on the **second** merge, leaving the
integration worktree conflicted and **taking the run down**. Committed on the **integration
worktree inside the `IntegrationMutex` `_publish` already holds**, it is **linear history with no
merge to conflict**. Idempotence is by **byte comparison against the file**, not by `git status`.

**The cost is stated rather than hidden: last writer wins.** `moduleExtensions` entries are
**replaced, not unioned**. What **survives every writer** is **`registryFileHashes`**, which is a
function of **`MODULE.bazel` alone** — the same root module every worktree carries — and it is the
map whose **absence produces exit 32** in row 1 of the matrix. **Whether the surviving extension
entries also suffice offline is explicitly NOT claimed** by this ADR.

**Absent lock: publish nothing, loudly.** On a **first** run there is **no lock until a Bazel that
could reach a registry has written one**, so absence is the **normal state of a first build**.
Failing the publish would cost the repo the `BUILD.bazel`/`MODULE.bazel` it **legitimately
generated**, over an artifact the **next** build produces. Publishing an **empty or synthesized**
one is the *"an empty lock is indistinguishable from no dependencies"* defect at monorepo scale:
Bazel either **overwrites it** (bought nothing) or, under `--lockfile_mode=error`, **refuses a
resolution nobody computed** — and **either way the tree LOOKS offline-ready**. So the absent case
emits a `module_lock_absent` warning **carrying the consequence**, and publishes nothing.

**A static guard for the registry half, with the registry named by the caller.** New **pure** module
`src/fleet/bazel/lockfile.py` exposes `check_lock_registry(text, *, registry)`. `registry` is a
**required parameter with no default**, because **which registry an invocation contacts is a fact
about its argv**, not a constant. The subject is the lockfile's **URL keys only**, found by
**walking the parsed JSON** so it is robust to `lockFileVersion` churn; **archive URLs appearing as
values inside `moduleExtensions` are deliberately excluded**, because those are served by the
**content-addressed cache** and treating them as registry keys would **fail every real lock**. The
failure message states the **consequence**: an offline container build dies at
`Computing main repo mapping` with **exit 32** — *"the identical failure to shipping no lockfile at
all, except that the tree LOOKS offline-ready"*. This guard would have caught the mirror row of the
matrix.

**A related measurement, recorded so a later round does not re-derive it:** `--registry` **is**
accepted as a **post-verb build option** (unlike `--output_user_root`, which ADR-0062 records as
exit 2 there), so pointing the container at a mirror would need **no `bazelrc` trick**. Not needed
today — both BCR addresses answer **200** from this host.

*Agent Recommendation (judgement calls, not requirements of `CLAUDE.md`, the SPEC, or any
reference).* Five choices are the agent's: **(i)** publishing the lock **at all** rather than
leaving it to a human-run `bazel mod deps`; **(ii)** committing it **under the mutex** rather than
teaching the merge a union driver for `moduleExtensions` — the union is defensible and was
rejected as a shadow resolver (Guardrail 4); **(iii)** **warn-and-continue** on absence rather than
failing the publish or synthesizing bytes; **(iv)** the guard's **URL-keys-only** scope; **(v)**
`registry` as a required parameter rather than defaulting to `build.registry`.

**Consequences.**

1. **§31's B2 diagnosis is superseded, not merely amended.** "The sandboxed path is red because the
   cache is empty" is **false as a complete statement**: a warm cache with no lock is **also** red,
   and for a **different reason**. Both artifacts are required; only one of them existed.
2. **The lock reaches the branch; it is NOT proven sufficient offline.** **No offline container
   build has been attempted** with a harness-published lock. What is proven is that a lock **is
   published** and that **its registry keys can be checked** against the address the container's
   Bazel contacts.
3. **Last-writer-wins is a known, accepted, unmeasured cost.** A wave of N ecosystems leaves **one**
   ecosystem's `moduleExtensions` in the committed lock. `registryFileHashes` survives; the rest is
   untested. Anyone who later unions them owes a measurement, not a merge driver.
4. **The claim that `registryFileHashes` is complete regardless of which targets were built is
   reasoning about Bazel's MVS, not a measurement.**
5. **The guard is static and deliberately not an assertion over this host's output.** On this host
   the real-Bazel tests may resolve through the BCR **mirror**, so the locks those tests publish can
   **legitimately** be mirror-keyed; a check that demanded bcr-keyed locks would be wrong here.
6. **A second design change of the same shape is now named and open: `maven_install.json`.**
   `JvmAdapter` emits `maven.install` with **no `lock_file`**, and `maven_install.json` appears
   **nowhere in `src/` or `tests/`**. Unpinned `maven.install` resolves through **coursier, which
   opens its own sockets and never passes through Bazel's downloader**, so `--repository_cache`
   **cannot cover it under any warming strategy** — **the JVM ecosystem cannot build offline
   today**. Same structural verdict at **lower confidence** (reasoned from mechanism, **not measured
   this round**): Rust's `cargo fetch` — `ecosystems/rust.py`'s own comment already records that
   `crate_universe` runs it **without `--locked`** — Python's `pip`-mode `whl_library`, and
   gazelle's `go_deps` module zips.
7. **Cache seeding is decided, and two alternatives are rejected on the record.** **Rejected:**
   pointing `verify.repository_cache` at the suite's `/tmp/fleet-bazel-*/repos` — content-correct,
   but it lives under a **temp fallback keyed to a hash of this checkout path**, holds **only what
   the fixtures fetched**, and **`tests/conftest.py` deletes it whenever it exceeds the 2 GiB
   keep-ceiling — it is at 1.8 GiB**. Pointing production at a directory the **test suite garbage
   collects** is a landmine. **Rejected:** `--vendor_dir`, the upstream offline story — it subsumes
   the lockfile problem, but **`bazel vendor` is a verb** and `_bazel_argv` hardcodes
   **`build`/`test`**, so the harness **cannot dispatch the populating step**; kept as a fallback.
   **Chosen:** a **one-off networked warm run** into the **configured** cache, with the **lockfile as
   a first-class output**. The **cache is generated and documented, never committed** (1.8 GiB of
   content-addressed blobs); **the lockfile is committed** — low single-digit MB, and it is **the
   actual pin**.

---

## ADR-0065 — ADR-0020's **principle shipped; its file layout did not**. `bazel/generators.py` keeps its name, the no-branching invariant is what is enforced, and the `ecosystems/contracts/` half is recorded as **never built**

**Decision.** Four parts, all of them corrections to *this file's own record* rather than to code.

1. **ADR-0020's `bazel/{emit,module,render}.py` split is retired. `src/fleet/bazel/` ships as
   `layout.py`, `generators.py`, `lockfile.py`, `query.py`,** and the driver ADR-0020 called
   `emit.py` is `workers/buildgen.py` (plus `cli._run_gazelle` for ADR-0056's run-level pass).
   `generators.py` holds what ADR-0020 assigned to `render.py` (`render_target`,
   `render_build_bazel`, `render_root_package`, `render_gazelle_build`) *and* what it assigned to
   `module.py` (`mvs_select`, `reconcile_versions`, `validate_override`, `render_module_bazel`).
   No file is renamed and no code moves.

2. **ADR-0020's actual decision — the one that mattered — is affirmed, and it is met.** The
   decision was never "three files"; it was *"none of which may contain an `Ecosystem`
   comparison"*, i.e. output-side language knowledge lives in `src/fleet/ecosystems/` and
   `src/fleet/bazel/` is a branch-free driver. That is true of the tree as it stands:
   `grep -n 'Ecosystem\.\|ecosystem ==\|== Ecosystem\|elif.*ecosystem' src/fleet/bazel/generators.py`
   returns **nothing** across its 868 lines, and SPEC §12.6's own gate returns 10 hits fleet-wide,
   **none of them in `src/fleet/bazel/`** — all are `Ecosystem.UNKNOWN` as an *assigned value* in
   `graph/infer.py`, `workers/` and `cli.py`, which §12.6 already exempts by name. `generators.py`
   names `Ecosystem` three times and all three are adapter-supplied data crossing a boundary:
   two `Mapping[Ecosystem, str]` *parameter annotations* on `coarse_build_targets` (the rule map is
   injected, per ADR-0057) and one comment.

3. **The invariant is mechanically guarded, so the filename is not load-bearing.** Four tests in
   `tests/test_ecosystems.py` enforce it, and **none of them exempts `src/fleet/bazel/`** —
   `ADAPTER_PACKAGES = ("src/fleet/manifests/", "src/fleet/ecosystems/")`, full stop:
   - `test_no_ecosystem_branch_exists_outside_the_adapter_packages` — no
     `if …ecosystem ==|!=|is` outside the two packages;
   - `test_no_ecosystem_member_other_than_the_unknown_sentinel_is_named_outside_the_packages` —
     any `Ecosystem.<MEMBER>` other than `UNKNOWN` outside the two packages (and `models/enums.py`)
     fails;
   - `test_the_exemption_list_is_exactly_the_two_adapter_packages` — asserts `ADAPTER_PACKAGES`
     *is* exactly that pair, so widening it to admit `bazel/` is a visible diff;
   - `test_no_language_directory_is_hardcoded_in_any_driver` — `'"(java|ts|py|go|rust|misc)/'`
     returns nothing over `bazel/`, `orchestrator/`, `graph/`.

   A fifth, `test_no_adapter_package_exception_is_named_outside_the_adapter_packages`, closes the
   form of language knowledge the greps cannot see (an adapter-private `…Error` caught by a
   driver). *Agent Recommendation:* these five are the real ADR-0020, and they would survive any
   renaming of the files they scan — which is the argument for treating the filename as cosmetic.

4. **The second half of ADR-0020 never shipped, and is recorded as unbuilt rather than
   re-pointed.** `src/fleet/ecosystems/contracts/{base,proto,openapi,avro,thrift,shared_lib}.py`
   — the `ContractAdapter` registry keyed by `ContractKind` (ADR-0019 + ADR-0020, SPEC §7.6) —
   **does not exist**. `src/fleet/ecosystems/` contains `base, jvm, js, py, go, rust, unknown` and
   no `contracts/` subpackage. `workers/contracts.py` says so in its own module docstring: *"The
   `ecosystems/contracts/` `ContractAdapter` registry §1 names does not exist in this tree yet, so
   these tables are the registry in miniature and are what should move into it."* Its three
   `Mapping[ContractKind, …]` tables honour §1's no-branch rule (there is no `if kind is …` and no
   `match`), so the *invariant* holds on the contract side too — but `for_kind`, `neutral_targets`
   and `binding_target` have no implementation, `EcosystemAdapter.contract_bindings` is declared
   and read by nothing, and **SPEC §12.32's `fleet.ecosystems.contracts.discover()` set-equality
   against `set(ContractKind)` is unsatisfiable as written**. This ADR does not fix that; it
   records it, and SPEC §12.32 and §7.6 are marked NOT YET IMPLEMENTED in the same edit so the
   criterion stops reading as a passing gate.

**Rationale.** The record drifted, the code did not. ADR-0020 was written before ADR-0005's
registry landed on the output side, and it specified *where the code would live* in the same
breath as *what the code may not do*. Only the second half was ever a decision: the SPEC §1
contradiction ADR-0020 existed to resolve ("one new file and zero edits elsewhere" versus an
explicit `generators.py` exemption from the no-branching rule) is resolved by the exemption being
gone, not by the file being gone. Every subsequent ADR that touched this area — ADR-0056,
**ADR-0057** (D3, "`bazel/generators.py` reads the union through `_registry_ruleset_repo_names()`";
D-alternatives, "puts ecosystem knowledge in `bazel/generators.py`, which is exactly what D1
moved"), and the ADR-0058 analysis at `DECISIONS.md:2397/2401/2423` — cites `generators.py` by
line number as the current, correct home. Four ADRs' worth of citation is itself evidence about
which name is real.

*Agent Recommendation:* **a rename was rejected.** It is a large diff — `generators.py` is 868
lines split three ways, plus three test modules (`test_bazel.py`, `test_ecosystems.py`,
`test_build_e2e.py`) and two importers (`workers/buildgen.py`, `cli.py`) — that changes **no
behaviour and no assertion**, invalidates the line-number citations four landed ADRs depend on,
and buys a property nothing checks: SPEC §8 is not asserted by any test. The success criterion
PROGRESS Tier-0b named for this work — *"`find src -name '*.py'` matches SPEC §8 exactly (a test
asserts the set)"* — **does not exist in `tests/`**, which is precisely why the divergence went
unnoticed. Given a mechanically-guarded principle and an unenforced layout, the honest move is to
amend the layout to what shipped rather than to spend a refactor defending a document.

**On the stale status records.** `docs/PROGRESS.md:114` still carries Tier-0b as an unstarted task
(including *"delete `bazel/generators.py`"*) and `:919` still lists the split as open defect 2.
Neither is a reversal of this ADR and neither was a decision to keep the split: they are status
lines written before `src/fleet/ecosystems/` existed and never revisited. They are **stale, not
contradictory**, and this ADR does not edit them — `PROGRESS.md` is a log, and the correction
belongs in a checkpoint entry, not in a retro-edit.

**Alternatives rejected.** *Perform the three-way split as specified* — see above: cosmetic, and
it discards citation anchors. *Silently re-point every `emit.py`/`module.py`/`render.py` mention in
SPEC to `generators.py`* — that is how the record got here; a file layout named by a landed ADR is
not corrected by an undocumented find-and-replace, so the SPEC edits below cite this ADR inline.
*Declare ADR-0020 superseded outright* — its ABC, its registry mechanism, its six shipped adapters,
its §5.6 models and its no-branching rule are all live; only the layout clause and the unbuilt
`contracts/` clause are affected, which is an amendment, not a supersession. *Re-point
`ecosystems/contracts/` to `workers/contracts.py`* — rejected as false: `workers/contracts.py` is
Phase 1 step 5b **discovery** (ownership, extractability, hoist ranking); it emits no Bazel target
and implements no `ContractAdapter`. Naming it as the registry would convert a known gap into an
invisible one.

**Amends ADR-0020** (file layout retired; principle affirmed; the `contracts/` half recorded as
unbuilt). Supersedes nothing. Consequential to **ADR-0019** (its `ContractAdapter` owner is still
unbuilt) and consistent with **ADR-0046**, **ADR-0056** and **ADR-0057**, all of which already
name `generators.py`.

---

## ADR-0066 — SPEC §7.5 stops **embedding** `EcosystemAdapter`'s source: `src/fleet/ecosystems/base.py` is the normative artifact, and the SPEC carries the contract in prose

**Decision.** SPEC §7.5's literal `# src/fleet/ecosystems/base.py` code block is **removed** and
replaced by (i) prose naming every ClassVar, abstract method and default the ABC declares, (ii) an
explicit statement that **`src/fleet/ecosystems/base.py` is normative and this section is
descriptive** — where the two disagree, the ABC wins and the SPEC is the defect — and (iii) the
registry contract (`@register`, `discover()`, `for_ecosystem()`) stated as behaviour rather than as
a body. SPEC §3.3 step 2's pseudocode is corrected from
`adapter.workspace_deps(unit.external_coordinates)` to `adapter.workspace_deps(unit)`, and §3.5's
stub call from `workspace_deps([coord])` to a `BuildUnit` carrying that coordinate.

**Rationale.** The block was declared normative by the SPEC preamble and had drifted **two landed
ADRs** deep, in the direction that matters: it declared
`workspace_deps(self, coordinates: list[Coordinate])` where the shipped ABC is
`workspace_deps(self, unit: BuildUnit)`; it omitted `import_specifier` entirely; and it declared 7
ClassVars where the ABC declares 18 (`extension`, `extension_bzl`, `ruleset_repo_names`,
`src_suffixes`, `repo_name`, `library_rule`, `binary_rule`, `test_rule`, `library_bzl`,
`binary_bzl`, `test_bzl`, `entrypoints`, `degraded` are all missing) and 5 methods where the ABC
declares 9 (`workspace_files`, `package_files`, `root_targets` missing).

**Both omissions are ADR-0046, and ADR-0046 is not silent about them.** Its decision item 2 —
*"`workspace_deps` takes the `BuildUnit`, not a `Sequence[Coordinate]`"* — states the change
outright, with the rationale that an adapter handed only `external_coordinates` *"cannot name a
sibling at all — the internal/external split, by construction, hands it the half that excludes
them"*; it rejected a `JsAdapter`-only side channel precisely because *"the gap is in the shared
method's signature."* Its decision item 1 made `import_specifier` abstract. `base.py`'s own
docstrings cite the same reasons. So this is **not** an undocumented divergence: the decision
landed, the code landed, and only the SPEC's copy of the code was left behind — which is the
entire argument against keeping a copy.

**Option (a) chosen over option (b) — generate the block from the ABC and assert equality in a
test.** *Agent Recommendation*, on four grounds:

1. **The block is not a signature dump.** Its value to a reader is its inline commentary
   (*"true ⇒ delegate emission, do not fake it"*, *"replaces the old hardcoded ecosystem_dir
   map"*). A generator either drops that prose — in which case the surviving block is strictly
   less useful than the ABC it copies, and the reader should have been sent to `base.py` — or it
   must preserve hand-written prose across a mechanical regeneration, which moves the rot into the
   generator.
2. **The block is not only the ABC.** It embeds `_BY_ECOSYSTEM`, `register()` and `discover()`
   bodies, which are module-level and not part of the ABC surface, so (b) needs either a second
   extraction source or a hand-maintained remainder — and the remainder is where the next drift
   lands.
3. **The size argument runs the wrong way.** The shipped ABC is ~340 lines of declarations and
   docstrings, against 86 lines in the SPEC. Faithfully generated, §7.5 becomes a verbatim second
   copy of a source file inside a design document.
4. **The precedent in this document favours asserting properties, not mirroring artifacts.** SPEC
   §12.6 is the section that has *not* drifted, and it is enforced by tests that scan the tree for
   a violated property — not by a copy of the tree. Rule 2 (Simplicity First) points the same way:
   a generator plus an equality test, maintained for a documentation section, is machinery in
   service of a copy that only needed to be a pointer.

The failure mode (a) has to answer is that prose drifts too. It is mitigated by what (a) chooses to
state: **names and responsibilities, not signatures.** ADR-0046 changed a signature and left every
name intact, which is the usual shape — and inverting the authority (the ABC is normative, §7.5 is
descriptive) means a future reader who finds a disagreement resolves it toward the code instead of
"correcting" the code toward a stale document, which is exactly the mistake this pair of ADRs
exists to undo.

*Agent Recommendation for a later task (not implemented here — this session may not touch
`tests/`):* add `test_spec_7_5_names_every_member_of_the_ecosystem_adapter_abc` to
`tests/test_ecosystems.py`, asserting **at name level** that (1) every identifier §7.5's prose
lists in backticks as a ClassVar or method exists on `EcosystemAdapter`, and (2) every public
ClassVar and method the ABC declares is named somewhere in §7.5's prose — parsed from `docs/SPEC.md`
between the `### 7.5` and `### 7.6` headings, resolved via
`{n for n, v in vars(EcosystemAdapter).items() if not n.startswith("_")} | EcosystemAdapter.__annotations__.keys()`.
Name-level, not signature-level, is deliberate: it catches the two failures that actually occurred
(a method omitted entirely, a ClassVar set that fell eleven behind) without re-creating the
brittleness of (b). **Until that test lands, SPEC §7.5 is not self-enforcing** and its accuracy
rests on this ADR alone.

**Alternatives rejected.** *A one-off re-sync of the block* — buys a correct document that begins
rotting at the next ADR touching the ABC, which is the state this ADR is repairing. *Keep the block
and drop the "normative" claim* — a non-normative 86-line copy of a source file is a trap with a
disclaimer on it. *Move the block to §7.5 as an appendix generated at release time* — no release
process exists to hang it on.

**Amends ADR-0020** (§7.5's presentation of the ABC it introduced) and **records ADR-0046** as the
source of both drifted members. Supersedes nothing.

---

## ADR-0067 — A parse probe that produced **no verdict** is a **violation**, not a warning: `ProbeIndeterminateError` is deliberately **not** an `EngineUnavailableError`, the `break` becomes `continue`, and `no_verdict()` lands in the module that **creates** the three-flag invariant

**Status: DECIDED, NOT YET IMPLEMENTED.** Unlike its neighbours ADR-0065 and ADR-0066 — both
retrospective records of shipped state — this ADR records a decision that had **zero** of its four
parts in the tree as of `68a41ff` (review-36 C3; re-verified here). Checked directly:

1. `grep -rn "ProbeIndeterminateError" src/ tests/` → **zero hits**. No such class exists anywhere
   in `rewrite/rules.py` or `fleet.rewrite`.
2. `src/fleet/cli.py:4163` still has exactly **one** `except EngineUnavailableError as exc:` arm
   in `_transform_criterion`, not two.
3. That single arm still ends in **`break`**, not `continue` — confirmed at `cli.py:4165`.
4. `grep -n "no_verdict" src/fleet/util/proc.py` → **zero hits**; `src/fleet/workers/clone.py:199`
   is still the full `_no_verdict` implementation, not a thin delegator to `util/proc.py`.

The gap this ADR describes is real and is tracked as **D37** in `docs/INTEGRATION_HONESTY.md`
(OPEN, and its text matches the current tree — `cli.py:4162-4163`, the `break`, and
`astgrep._scan_for_error_nodes` still raising plain `EngineUnavailableError`). No new ledger entry
is needed for it; this status line exists so this ADR itself stops reading as an accomplished
change, which the compounding error at ADR-0047's amendment below did until corrected.

**A second, narrower gap this ADR did not anticipate (review-36 I1).** Part 4's own rationale
argues `no_verdict` belongs in `util/proc.py`, not in a `workers/*` module, because "the decoder
belongs with the encoder" and `fleet.rewrite` importing from `fleet.workers` "inverts the
layering." One commit after this ADR landed, `68a41ff` added a **third** decoder of the identical
`(started, timed_out)` invariant — `clock_failure` — directly in `src/fleet/workers/base.py:155`,
the exact kind of module this ADR argues against. `clock_failure` returns a `FailureClass`, which
is legitimately a `workers` concept and not interchangeable with the reason-string `no_verdict`
this ADR specifies, so the two are not strictly the same function — but the placement tension is
real and unacknowledged in either commit. `base.py:161-164`'s docstring additionally claims
`clock_failure` is the answer "written down once," which overstates it: `clone._no_verdict`
(`clone.py:199`) and `astgrep._scan_for_error_nodes` (`astgrep.py:198`) still hand-order the same
two flags independently. Correcting `base.py`'s docstring is out of scope for this file (it is
`src/`, owned by a different worker this round); recorded here so the tension is not silently
dropped.

**Decision.** D37. §3.2's fourth success clause — *a parse probe of every rewritten file* — stops
being skippable in silence. Four parts:

1. **A new `ProbeIndeterminateError(RuntimeError)` in `rewrite/rules.py`**, exported from
   `fleet.rewrite` alongside `EngineUnavailableError`, raised by
   `AstGrepRewriter._scan_for_error_nodes` when the probe **ran but produced no verdict** — it was
   killed at its deadline, or it was never started, or it exited a code that is neither `0` nor
   `1`. It is **deliberately NOT a subclass of `EngineUnavailableError`.**
2. **`cli._transform_criterion` grows a second `except` arm**, ordered first, that appends to
   `violations`. A genuinely absent binary — still `EngineUnavailableError`, still raised by
   `ensure_available()` — keeps its non-blocking `parse_probe_unavailable` warning.
3. **The `break` becomes `continue` in *both* arms.** The indeterminate arm needs it because one
   slow file must not excuse the other thirty-nine. The unavailable arm needs it for symmetry and
   gets a **`(repo_id, engine)` dedupe** so a host with no `ast-grep` still emits one line per repo
   per engine rather than one per rewritten file.
4. **A shared `no_verdict(result: ProcResult) -> str | None` moves into `util/proc.py`**, and
   `workers/clone.py`'s `_no_verdict` becomes a thin delegator.

**Rationale.** The defect is not that the probe can fail. It is that **three different facts share
one exception type, and the type is bucketed as a warning.** `_scan_for_error_nodes` is *correct
locally* — it reads `if result.started and not result.timed_out:` **before** any exit code, which
is the right predicate in the right order, and it puts `timed_out=` in the message. One layer up
that care is undone twice: `_transform_criterion`'s single `except EngineUnavailableError` arm
appends to `unprobed`, which `cli.py:3093` renders as *"warning: the §3.2 parse probe DID NOT RUN
… no rewrite engine is installed on this host"* and which contributes **no** violation, so the run
exits **0**; and the catch site is a **`break`**, which leaves the `for unit in rewritten` loop and
abandons the clause for every remaining rewritten file in that repo. A corrupt rewrite in files
2–40 ships as a verified transform because file 1's probe was slow, and Phase 3 writes that branch
into monorepo history. The operator-facing text names an uninstalled engine for a host where
`tools/bin/ast-grep` is **vendored in-tree, resolves, and ran** — measured, `0.45.1`.

**A correction to how this defect is usually described, because the fix depends on it.** It is
tempting to say `_scan_for_error_nodes` conflates *killed*, *never started* and *binary missing*.
Verified against the tree, **it does not — it cannot.** `parse_probe` and `probe_text` both call
`self.ensure_available()` **before** reaching the helper, and that is the only thing in the driver
that reports a missing binary; if the binary were absent and `ensure_available()` somehow passed,
`create_subprocess_exec` raises `FileNotFoundError`, not a `ProcResult` (measured). So the helper
sees exactly **two** no-verdict conditions, and the conflation with the third is **at the exception
type**, one call frame up, in a single `except` clause that cannot tell its causes apart. This is
why the fix is a **new type** and not a new predicate inside the helper: the helper's predicate is
already right, and there is nothing to split there.

**`ProbeIndeterminateError` is not a subclass, and that is the entire decision.** Subclassing
`EngineUnavailableError` is the natural-looking move and it is self-defeating: the existing
`except EngineUnavailableError` arm would keep catching it, keep bucketing it into `unprobed`, and
keep exiting 0 — the same defect wearing a more precise name, which is strictly worse than the
defect, because the name would advertise a distinction the control flow does not make. Both derive
from `RuntimeError` and are siblings.

**Alternatives rejected.**

*Silent pass — the status quo.* Worst outcome available, and named here so it is on the record as a
rejected option rather than an unexamined default: one of §3.2's four success criteria is skipped
and the tree ships as verified. Rule 11 and reference lesson 6 (*"agents must not grade their own
homework"*) both forbid it, and the harness already refuses this shape elsewhere — `ProcResult.ok`
exists precisely so a timed-out process is never `ok`.

*A distinct "unmeasured" third state, neither pass nor fail.* Rejected because **it already
exists.** `parse_probe_unavailable` **is** that bucket — a list rendered as a non-blocking warning —
and it is the exact mechanism that laundered the timeout into a pass. Adding a second non-blocking
bucket reproduces the defect with better vocabulary. The problem was never that the harness lacked
a place to put "we did not find out"; it was that the place it had did not stop the run. (Rule 11.)

*Route the repo to `REQUIRES_HUMAN_INTERVENTION`.* Rejected on the CLAUDE.md §4 state boundary.
`_transform_criterion` is a **read-only verifier**: it is called after the wave loop has halted and
after `statuses = await _transform_statuses(read_conn, run_id)` has read the frozen statuses out of
SQLite, and it receives them as a `Mapping` it never writes. Marking RHI would put execution-state
writes in a checker — the harness's own version of letting the validator file its own findings. It
is also the wrong verdict: RHI (exit 7) says *this repo needs a human before it can proceed*, while
the fact here is *the run finished, the state is readable, and a claim it makes about the tree is
unproven*. Exit **6** already means exactly that — `TransformCriterionError.exit_code` is
`ExitCode.UNRESOLVED_FINDINGS`, and its own docstring says *"the run finished and the state is
readable, but a claim it makes about the tree is false, and shipping that to Phase 3 would rewrite
it into monorepo history."* The verdict this ADR needs was already defined; it was simply not
reachable from the probe.

*Widen the missing-binary path to a hard failure too.* Rejected. That gap is **declared,
host-level and uniform** — ADR-0047 records it, `docs/INTEGRATION_HONESTY.md` carries the
`ast-grep` row, and it is a property of the host rather than of any repo, so a per-repo violation
is the wrong shape for it. Widening it is a policy change nobody asked for, and it would break a
**correct** existing test of intentional behaviour (`tests/test_transform_e2e.py` asserts on a
populated `parse_probe_unavailable`). Making a truthful warning is this ADR's job; deciding whether
an uninstalled engine should stop a run is not.

**Where `no_verdict` lives, and why it is not `workers/clone.py`.** A sibling worker landed the
same classifier as `clone._no_verdict` this round, with the right body and the right ordering. It
is in the wrong module. `fleet.rewrite` importing from `fleet.workers` inverts the layering — a
rewrite driver would depend on a worker — and the classifier is not clone-specific in the first
place: it decodes an invariant that **`util/proc.py` itself creates**, synthesising
`timed_out=True` **and** `started=False` **and** `exit_code=124` together for a deadline that had
already passed. The decoder belongs with the encoder. `clone._no_verdict` becomes a delegator so
its five call sites and their tests are untouched.

**`not started` is tested BEFORE `timed_out`, and this is load-bearing rather than stylistic.** The
passed-deadline `ProcResult` carries **both** flags, so reading `timed_out` first reports a command
that never ran as one that ran too long — the same misattribution this ADR exists to remove, one
layer down. `clone._no_verdict`'s docstring already argues this; moving the function must preserve
the order, not merely the behaviour. See ADR-0047's measured correction for the three measured
rows, including the one that matters most to any future fix: **a probe killed at its deadline exits
`-15`, or `-9` if it ignored `SIGTERM`, never `124`** — so a fix keyed on `exit_code == 124` would
catch only the never-started half, and one keyed on `-15` only part of the other half.

**Consequences, stated plainly — three things this does NOT do.**

1. **It does not make the probe a parser.** `kind: MISSING` is still unqueryable in 0.45.1 (exit
   **8**, `Cannot parse rule`, re-measured 2026-08-16), so ADR-0047's **accepted false pass** — a
   file that error-recovers into a `MISSING` token with zero `ERROR` nodes reads as parsing —
   stands unchanged. This ADR makes the probe's *failures* honest, not its *successes* stronger.
2. **It does not close the missing-engine gap**, only makes the warning truthful. After this
   change, *"no rewrite engine is installed on this host"* is printed only when no rewrite engine
   is installed on this host. The clause still goes unchecked in that case, and
   `docs/INTEGRATION_HONESTY.md`'s `ast-grep` row remains the record of it.
3. **It adds no way to raise the 60 s timeout.** `AstGrepRewriter.__init__` takes
   `timeout_s: float = 60.0`, `TransformSection` has **no** corresponding key, and
   `EngineRegistry.from_modules` takes each module's ready-made `REWRITER` singleton — so the
   default is what every production probe uses and **nothing in config can change it**. An operator
   who hits the new violation on a genuinely large file can only edit code or shrink the file.
   *Agent Recommendation, deliberately not taken here:* a `transform.engine_timeout_s` key is the
   obvious follow-up, and it is out of scope because this ADR is about a verdict being lost, not
   about the deadline that loses it — adding a knob in the same change would let "raise the
   timeout" become the answer to a violation whose point is that the answer must be "look at the
   file."

**A newly-found adjacent defect, recorded for the ledger — this is NOT a decision and NOT fixed
here.** `TsMorphRewriter.parse_probe` calls `await self.ensure_available()` and then raises
**`NotImplementedError`** (*"the ts-morph bridge script is not shipped"*). On a host where Node is
absent or cannot resolve `ts-morph`, `ensure_available()` raises `EngineUnavailableError` first and
the criterion behaves as designed — which is why this has never been seen. On a host with **Node
and `ts-morph` both installed**, and any rewrite rule carrying `engine: ts-morph`,
`ensure_available()` **passes** and the `NotImplementedError` escapes: **neither** `except` arm
this ADR leaves behind catches it (`ProbeIndeterminateError` and `EngineUnavailableError` are both
`RuntimeError` subclasses; `NotImplementedError` is not), it is not a `FleetCliError`, so
`_mapped_errors()` does not funnel it and it surfaces as a **traceback at exit 1** — the outcome
`cli.py`'s own module docstring calls out as telling an operator nothing. Note the direction: this
is a *crash*, not a laundered pass, so it is strictly less dangerous than D37 and must not be
folded into it. Found while speccing D37; **not fixed by it**, and left for
`docs/INTEGRATION_HONESTY.md` to number (D45 is the current maximum) and own. `LibCstRewriter.parse_probe`
is real (`libcst.parse_module`) and is **not** affected.

**Amends ADR-0047** — narrowing its "Alternatives rejected" premise that `EngineUnavailableError`
always renders as a non-blocking warning; the pointer is recorded in that entry. **Records D37**
(`docs/INTEGRATION_HONESTY.md`) as the defect decided about, and belongs to the same four-state
family as D34–D45. Supersedes nothing.

---

## Constraints Inherited From `references/`

Concrete lessons taken from the reference material, each with its source and the ADR it
shaped.

1. **Persistence must be designed before parallelism, and the unit of persistence is
   `(run_id, repo, stage)` in SQLite.**
   *"One thing that caught us out was that persistence needs to be factored in before
   parallelism. You do not want to throw away a five-hour run because of an unforeseen
   error. Every stage writes to one SQLite database keyed by (`run_id`, `repo`, `stage`).
   Any stage can resume, retry, or get pulled into a later run without redoing work."*
   — `references/cloudflare-build-your-own-vulnerability-harness.md`
   → ADR-0004 (SQLite as primary store, keyed identically), ADR-0012, ADR-0014.

2. **Checkpoints are JSON validated on load — never pickle.** The reference harness carries
   an explicit CWE-502 note: its checkpoint module states there is *deliberately* no
   `import pickle`, because pickle's REDUCE/BUILD opcodes are an arbitrary-callable VM, and
   every payload is re-hydrated through Pydantic `model_validate` so a tampered checkpoint
   produces a `ValidationError` and the step is re-run.
   — `references/visa-vulnerability-agentic-harness/vvaharness/orchestrator/checkpoints.py`
   (and the SQLite BLOB store in `.../orchestrator/store.py`)
   → ADR-0002 (validate-on-load as the only deserialization path), ADR-0012.

3. **A transient API error can arrive as *text* inside a `200 OK` — classify the payload,
   not the exception type.**
   *"Sometimes a transient API error comes back as text in the (`200 OK`) response stream
   instead of throwing a code exception. To the orchestrator, this looks exactly like a task
   that finished cleanly. You must explicitly classify the response text, not just trust the
   exception type, or you end up logging empty runs as successes."*
   — `references/cloudflare-build-your-own-vulnerability-harness.md`
   → ADR-0014 (error classification by payload inspection; Fail Loud).

4. **Cross-repo tracing requires a unified symbol index *plus* an accurate dependency graph —
   it is the capability that finds what single-repo scans structurally cannot.**
   *"To make this work, you need a unified, cross-repo symbol index and an accurate
   dependency graph. This allows you to uncover deep, systemic flaws that a standard
   single-repo scan would miss."*
   — `references/cloudflare-build-your-own-vulnerability-harness.md`
   → ADR-0004 (`symbols` and `edges` tables as first-class, not derived afterthoughts),
   ADR-0005.

5. **The agent cannot see what is not in the repository — push context into versioned,
   repo-local artifacts.**
   *"From the agent's point of view, anything it can't access in-context while running
   effectively doesn't exist. Knowledge that lives in Google Docs, chat threads, or people's
   heads are not accessible to the system. Repository-local, versioned artifacts (e.g. code,
   markdown, schemas, executable plans) are all it can see."* The same source also makes the
   app *bootable per git worktree* so an agent can drive one isolated instance per change.
   — `references/openai-harness-engineering-codex-agent-first-world.md`
   → ADR-0010 (git worktree per in-flight repo), and this file's own existence as a
   repo-local decision record.

6. **Agents must not grade their own homework; every claim needs mechanical proof.**
   The reference describes agents editing source so their own exploit lands and then
   reporting the bug they created, and prescribes a separate validator that *cannot file its
   own findings*, with deterministic plain code mechanically verifying that cited paths
   exist and that patches and tests actually parse: *"If a **Hunter** is allowed to grade its
   own homework, it will confidently validate everything it outputs."*
   — `references/cloudflare-build-your-own-vulnerability-harness.md`, reinforced by the
   *silent failures* failure mode in
   `references/harness-engineering-ai-complete-guide-to-agent-harness.md`
   → ADR-0008 (models propose, code applies and judges), ADR-0013 (verified = exit code).

7. **Retry limits, backoff, and loop detection are mandatory; so is validating preconditions
   on checkpoint resume rather than blindly replaying.**
   *"Without retry limits, backoff policies, and loop detection, this pattern can burn
   through thousands of dollars in API calls overnight."* … *"The agent's checkpoint says
   'step 7 of 10 complete' but the preconditions for step 8 no longer hold. The fix is
   validation of preconditions on checkpoint resume, not blind replay."*
   — `references/harness-engineering-ai-complete-guide-to-agent-harness.md`
   → ADR-0014 (bounded escalating attempts), ADR-0012 (state re-validated from SQLite on
   resume, never replayed from the JSON projection).

---

## Conflicts Surfaced (not averaged)

Per `CLAUDE.md` Rule 7 — where references disagree, the cleaner/more-tested pattern is named
and the loser is recorded rather than blended.

**Conflict 1 — Threads vs. asyncio for LLM fan-out.**
The Visa harness runs its parallel LLM stages on `ThreadPoolExecutor`
(`vvaharness/pipeline/stages/s4_deepdive.py`, `s6_verify.py`, with lazily-constructed
thread-safe SDK client singletons) and reserves `asyncio.run()` for the Agent-SDK entry
points — a genuinely pragmatic hybrid. Our project `CLAUDE.md` mandates `asyncio`.
**Resolved for asyncio (ADR-0003)**, because the mandate is binding *and* the hybrid's cost
is real: mixing a thread pool with an event loop means two concurrency models, two
cancellation stories, and two ways to corrupt a checkpoint writer. We keep the *lesson*
underneath it — a single lazily-initialized client and bounded worker counts — and adopt the
harness's implicit boundary explicitly, with CPU-bound parsing pushed to processes rather
than threads.

**Conflict 2 — SQLite store vs. loose per-step JSON files as the state of record.**
The Visa harness explicitly migrated *away* from per-step `*.json` files to SQLite BLOB rows
(`orchestrator/store.py`: *"Replaces the per-step JSON files"*), while our `CLAUDE.md`
mandates `migration_state.json`. **Resolved by making SQLite authoritative and
`migration_state.json` an atomically-written projection of it (ADR-0004, ADR-0012)** — the
mandated file exists and is exactly what a human or a resuming run reads, but concurrent
writers transact against SQLite, so we get the mandate's ergonomics without the
lost-update and torn-write failure modes that drove the reference away from loose JSON.

**Conflict 3 — "Skip cross-repo tracing until you need it" vs. our stated differentiator.**
The same Cloudflare post that describes fleet-wide tracing also advises: *"You should skip
cross-repo tracing entirely until you have more than one repository that matters… only build
the next architectural stage when not having it is the specific thing slowing you down."*
**Resolved in favor of building it first**, because the advice is scoped to a harness
starting from one repo, whereas we start from 250 with an explicit mandate that cross-repo
DAG construction and topological sequencing *are* the product. We do honor the underlying
principle where it still applies: no dedup agent, no feedback/gapfill stage, and no
speculative sub-agent fan-out until a specific bottleneck demands one.

**Conflict 4 — Wire in static analysis early vs. let usage decide.**
*"The second is to not wire in static analysis early. We plumbed Semgrep all the way
through, and the **Hunters** invoked it zero times in a month of runs… It's worth paying
attention to what the agents actually reach for."* — `references/cloudflare-build-your-own-vulnerability-harness.md`.
This cuts against ADR-0006's investment in `ast-grep`. **Resolved by distinguishing
*optional agent tools* from *pipeline primitives*:** the lesson is about a tool offered to an
agent that the agent declined to use, whereas `ast-grep` is invoked by deterministic harness
code on a mandatory code path — it is not something a model may choose to ignore. The
secondary engines in ADR-0006 (`libcst`, `ts-morph`) are the ones genuinely at risk of the
Semgrep fate, so each is fenced to a named capability and will be removed outright if its
call count stays at zero.

---

## ADR-0068 — `build_diagnosis` is **kept, not deleted**, because `docs/SPEC.md` names it as an LLM slot; the unconsumed output is recorded as **D47**, not silently removed. `CacheMiss` stops being an `LlmError`, so it can no longer be swallowed by the worker degrade-and-continue pattern

**Trigger.** research-36 Q3 (MEASURED): `BuildverifyOutput.diagnosis` /
`.diagnosis_failure_class`, written at `buildverify.py:1152-1153`, have **zero readers** anywhere
in `src/` — dropped at every egress (`cli.py:4983-4989`'s and `:5480-5486`'s `_handoff` field
lists, the sole `checkpoints.save` at `orchestrator/runner.py:1005-1011`, and
`state/projection.py`/`models/state.py`, neither of which declares a `diagnosis` field). Only
`response.usage` (`buildverify.py:1050`) is consumed. **Independently re-verified**, not just
cited: `grep -rn "diagnosis" src/` was re-run for this ADR and returns the same closed set —
`schemas.py:151` (`LlmBuildDiagnosis`), `roles.py:57` (`BUILD_DIAGNOSIS = "build_diagnosis"`),
`calls.py:360`, `enums.py:222`, and the four `buildverify.py` sites above — no reader anywhere.

**Decision 1 — do not remove `build_diagnosis`.** CLAUDE.md's ruling for this situation is
conditional on `docs/SPEC.md`: if the SPEC mandates the feature, deleting the code silently would
put code and SPEC out of step, which this project treats as its signature defect. **The SPEC does
mandate it**, checked directly, not assumed:

**Correction (review-38 I4) — the `transform_repair` attribution further below is half-wrong, and
the true finding is stronger than what was recorded.** "reads the Bazel error" and "re-runs the
build" do not describe `transform_repair` either: its prompt (`calls.py:171-182`) is *"You repair
a source file whose deterministic rewrite failed"*, evidenced by a `RULE_MISS` or a `git apply
--check` stderr (`rewrite.py:325-330`, `:362-366`), and `rewrite.py` never invokes a build
(`grep -ci bazel src/fleet/workers/rewrite.py` → 0). Its ladder is **§3.2** (`SPEC.md:783`, the
attempt table at `:854-856`; `calls.py:374`'s own docstring says so), not "§9" — `SPEC.md:5878` is
`## 9. Configuration`, an unrelated section. Only the "proposes an edit, code applies it" half is
real, and it belongs to `propose_repair` (`calls.py:371-375`, returning `LlmPatchProposal`)
applied via `land_patches` (`rewrite.py:397-408`). **No role in the codebase performs the
apply-and-rerun-the-build behaviour `SPEC.md:1391-1392` describes** — the SPEC sentence conflates
`build_diagnosis`'s advisory role with `transform_repair`'s apply role, and neither one, alone or
combined, reads a Bazel error and re-runs a build. The paragraph below is left as originally
written for its own record; this note supersedes its "§9" and "re-runs the build" claims.

- `docs/SPEC.md:1390-1392` lists it in the **LLM slots** enumeration for Phase 3: "build-failure
  diagnosis on attempts 2–3 (sonnet, then opus)".
- `docs/SPEC.md:6266` — `config/models.yaml`'s worked example — declares
  `build_diagnosis: WORKHORSE` in the `roles:` table alongside `transform_repair` and
  `manifest_extract`.

So this is **Situation 2** of the two the ruling distinguishes: SPEC-mandated, code generates it,
nothing consumes it. The code is kept; the gap is recorded rather than papered over, as **D47**
in `docs/INTEGRATION_HONESTY.md`.

**A second, narrower finding while checking the SPEC citation.** `SPEC.md:1391-1392`'s own prose
— "the model reads the Bazel error and proposes an edit, code applies it and re-runs the build" —
does **not** describe what `build_diagnosis` actually does. `LlmBuildDiagnosis`
(`schemas.py:151-162`) has exactly five fields — `failure_class`, `root_cause`, `suspect_paths`,
`suggested_action`, `confidence` — **none of them a diff or patch**, and `buildverify.py`'s own
docstrings at `:625-630` and `:1129-1134` call it "advisory only" precisely because "the exit code
is the verdict." The "proposes an edit, code applies it" sentence instead describes
`transform_repair` (SPEC.md:185, :856, :6265 — a *different* role, on a *different* ladder, §9's
repair loop for relocated files, not §3.3's build-verification diagnosis). This is a documentation
defect in the SPEC prose, not a code defect — flagged here per Rule 7 (surface conflicts, don't
average) rather than corrected unilaterally, since `docs/SPEC.md` edits of this shape belong beside
the D47 entry, not folded silently into an unrelated ADR. See D47 for the full citation chain.

**Decision 2 — `CacheMiss` (`llm/cache.py:92`) no longer subclasses `LlmError`.** Independent of
(1): research-36 Q3(b) found `CacheMiss` raised on a `--llm-cache read-only` miss
(`cache.py:469-470`) is caught by the bare `except LlmError:` degrade-and-continue pattern at
every advice-call site that uses it — `buildverify.py:1148`, `buildgen.py:454`, `buildgen.py:566`,
`prwriter.py:414`, `prwriter.py:422` (all re-verified by direct read, not just cited from
research-36) — each of which exists to keep a genuine model-side hiccup (a malformed reply, a
transport failure) from turning a recorded repo failure into an unrecorded worker crash. A replay
integrity break is a different kind of event: the model was never asked, so there is nothing to
degrade gracefully from, and CLAUDE.md Rule 11 (Fail Loud) is violated when it is swallowed
identically to those.

**Why removing `LlmError` from `CacheMiss`'s bases is safe** (checked, not assumed, so as not to
break a caller that depends on the old shape):
- No `isinstance(x, LlmError)` check exists anywhere in `src/` or `tests/` (`grep` confirms zero).
- `.complete()` is called **only** from inside worker `run()` methods (`grep '\.complete('`
  confirms zero call sites outside `llm/` and `workers/`), so every exception `CacheMiss` can
  produce is already routed through `BaseWorker._run_one` (`workers/base.py:864-905`), whose
  `except Exception as exc:` at `:897` classifies and records **every** escape, `LlmError` or not
  ("every escape is classified, none is swallowed (Rule 11)" — the existing, working mechanism
  this fix now reaches). `cli.py:489`'s top-level `except LlmError as exc:` funnel is therefore
  unreachable for `CacheMiss` either before or after this change — nothing there regresses.
- `classify.py:170`'s `except (LlmError, ValidationError)` maps an uncaught exception to
  `FailureClass.UNKNOWN` via `_error_for`'s `else` arm (`classify.py:238-254`) for anything that
  isn't `BudgetExhausted`/`TierUnavailable`/`TransportError`/`SchemaUnsatisfied`/`MalformedReply`;
  `CacheMiss` hit that same `else` arm before this change. Falling through to
  `_run_one`'s `classify_exception` (`base.py:500-519`) after this change lands on the same
  `FailureClass.UNKNOWN` default. **Correction (review-38 I2): not fully identical.**
  `failure_class` (`UNKNOWN`), `retryable` (`True`) and `exception_type` match — 3 of 4 fields —
  but `_error_for` (`classify.py:238-264`) writes `stderr_tail=redact_text(str(exc))` while
  `error_from_exception` (`base.py:532`) writes `stderr_tail=str(exc)` unredacted. The message
  text bypasses the redaction pass after this change; the failure classification does not
  regress. `rewrite.py:472` (re-raises as `WorkerRepairError`, a plain `RuntimeError` with no
  catcher, landing at the same `UNKNOWN`) and `cli.py:489`/`:490` (`except LlmError as exc:
  _fail(str(exc), ExitCode.USAGE)`, unreachable in practice since `cli.py` makes no `.complete()`
  call) also name `LlmError`; neither changes outcome either.
- `rewrite.py:472`'s `except LlmError as exc:` re-wraps as `WorkerRepairError` (`RuntimeError`,
  not an `LlmError` subclass itself) before reaching `_run_one`; without the wrap `CacheMiss`
  reaches the same boundary with its own already-descriptive message and the same `UNKNOWN`
  classification (`classify_exception` has no `WorkerRepairError`-specific branch either).

No caller's depended-upon behaviour changes, so this did not need to be escalated as a blocking
report before making the edit — verified first, then made, per the task's own conditional.

**The guarantee's scope, stated precisely (review-38 I1).** `CacheMiss` makes a read-only `.complete()`
**call** fail loud; it does not and cannot make a **skipped unit** fail loud. The checkpoint layer
sits above the cache: on a re-entered run, a unit already recorded complete is returned from the
checkpoint before any model call is attempted (`runner.py:458`, `:474-475` — D48,
`docs/INTEGRATION_HONESTY.md`), so no `.complete()` call is made and no `CacheMiss` can be raised.
A resume that reuses a checkpoint written under a different prompt or model id therefore satisfies
`--llm-cache read-only` **vacuously** for that unit — the guarantee this decision installs applies
only to calls that reach the cache, not to calls the checkpoint skips before they are attempted.
This bound is D48's finding, not a new one; it is stated here because this ADR is the artifact
that claims the guarantee and D48 is where the exception to it already lives, with no
cross-reference previously connecting the two.

Separately, and outside this ADR's own two decisions: a `CacheMiss` raised from `_diagnose`
(`buildverify.py:1124-1158`, called at `:969` inside `run()`) propagates out of `run()` entirely,
discarding the `WorkerResult` already built from the measured build/test verdict at that point —
not merely the advice. The repo then records `UNKNOWN` for a unit whose build result was in fact
known. Whether that trade is correct is not re-litigated here; it is recorded so a reader does not
assume the cost is confined to the missing advice field.

**Not fixed here (`buildverify.py` is owned by another worker this round, and touching it was
not needed for either decision above):** deeper hygiene — collapsing the five now-partially-dead
`except LlmError:` sites, or teaching `classify_exception` a `CacheMiss`-specific `FailureClass`
— is left alone. Rule 2 (simplicity first): the type-hierarchy fix is the minimum change that
makes the miss fail loud everywhere at once; narrowing five separate catch clauses is
speculative work nobody asked for.

**Files changed:** `src/fleet/llm/cache.py` (`CacheMiss` base class + docstring),
`tests/test_llm_cache.py` (new `test_cache_miss_is_not_an_llm_error`, pinning both the
non-inheritance and that a bare `except LlmError:` no longer catches it). `docs/SPEC.md` was
**not** edited (the prose defect is flagged above and in D47, not silently corrected — Rule 7).

---

## ADR-0069 — `deepagents` is a **mechanism catalogue, not a runtime**: it authors **no loop** and ships **no local sandbox**, so it is consumed (if ever) **behind a Protocol** per Guardrail 3 — and the four mechanisms worth taking are all **context economics**, the one axis on which a harness with no conversation has no story

**Status: EVALUATION RECORD. The findings below are DECIDED; every adoption candidate in §4 is
NOT YET IMPLEMENTED.** No candidate has a `docs/INTEGRATION_HONESTY.md` entry, because none is a
defect in this harness — they are opportunities, and inventing D-numbers for opportunities would
corrupt the ledger's meaning. Per Guardrail 1, §4 is explicitly labelled **Agent Recommendations**
and confers no authority; nothing in this ADR is a directive until separately decided.

### 1. Provenance, and why this ADR must carry a SHA

`langchain-ai/deepagents` was cloned to `references/deepagents/` at
**`1c6d358c60306aad2af0067dcca76f85f4deeba1`** (committed 2026-08-17T16:37:13-04:00), `deepagents`
core **v0.7.6**. Every `libs/...` citation below is relative to that commit.

The SHA is load-bearing, not decoration. `.gitignore:87` (`references/*/`) excludes the clone from
this repository, matching how `Agent-Harness/` and `visa-vulnerability-agentic-harness/` are already
handled. **This tree is therefore not reproducible from our own history** — the citations are
falsifiable only against that SHA, and `deepagents` is an actively-developed monorepo whose recent
commits are CLI/TUI work. An unpinned line-number citation to a vendored, untracked, fast-moving
reference is an unmeasured claim in the sense of Guardrail 6; the pin is what makes it checkable.

Findings were produced by eight parallel read-only subagents against a fixed eight-axis schema
(control loop, context management, tool surface, state, sandboxing, subagents/parallelism,
verification, failure handling) plus evidence-quality and gaps. Claims about `src/fleet` cited here
were **re-verified directly in the main session** before entering this file (`retry.py:148`,
`container.py:135-137`, ADR-0044's title, D32/D47, `worktree.py`'s single-owner rule).

### 2. The category error this ADR exists to prevent

`deepagents` **contributes no runtime of its own** (`libs/ARCHITECTURE.md:16-28`): the loop, state
and checkpoints are LangGraph's, and `create_deep_agent` merely assembles middleware before
returning `create_agent(...)` (`libs/deepagents/deepagents/graph.py:922-944`, `recursion_limit:
9_999`). It is a middleware bundle.

`fleet` has **no LLM agent loop at all.** `PhaseRunner._drive` (`src/fleet/orchestrator/runner.py:425`)
is a claim→lease→dispatch→fenced-write loop; ADR-0044 already fixes that **Bazel's exit code is the
verdict** and the model is asked only what stderr *means*. Tools are harness-side and never
model-callable — every shell-out funnels through `src/fleet/util/proc.py:run` as argv lists.

These are different layers, not competing designs. "Adopt deepagents?" is therefore the wrong
question and would produce a wrong answer in either direction: adopting it wholesale would import a
model-driven loop this harness deliberately does not have, and dismissing it wholesale would discard
middleware that solves problems this harness has not yet had to face. **The right question is which
middleware transplants into a deterministic orchestrator**, and the answer is short.

### 3. Where `deepagents` is behind this harness

| Axis | `deepagents` @ `1c6d358c6` | `fleet` |
|---|---|---|
| Retries | **None in core.** No retry, backoff, or attempt counter; errors become `ToolMessage(status="error")` strings for the model to read | `retry.py:148 decide()`, a pure function of `(LadderState, WorkerError)` that never branches on message text |
| Sandbox | No local **shell** backend. `libs/partners/` ships five packages; the four implementing a sandbox backend are all remote SaaS (`langchain-modal`, `-daytona`, `-runloop`, `-vercel-sandbox`). The fifth, `langchain-quickjs`, **is** local but is a JavaScript-REPL middleware (`CodeInterpreterMiddleware`), not a `SandboxBackendProtocol` implementation — a JS interpreter, not a place to run `bazel build`. The local shell path is bare `subprocess.run(shell=True)` (`libs/code/.../local_shell.py:302`) | `docker run --network=none --memory --cpus` (`container.py:135-137`) + `git worktree` per `(run_id, repo, attempt)`, both with owner-aware reapers |
| Durable state | LangGraph checkpoints only — no task queue, no heartbeats, no attempt counters, no `REQUIRES_HUMAN_INTERVENTION` terminal | SQLite WAL authoritative for orchestration, git for code (ADR-0024); single-writer actor, fenced leases, stale-fence discard |
| AST | **None.** No tree-sitter, no LSP, no unified-diff parser; mutation is whole-file write or one exact-string replace | Phase-2 core; total-order fixpoint buffer (`rewrite/pipeline.py`) |
| Concurrency | Model-driven fan-out via the `task` tool; no scheduler, no concurrency control | Waves admitted by descending blast radius; five semaphore classes in `Limits` |
| Staleness | None — concurrent workers clobber silently; git must be the only guard | Single-owner worktree rule, enforced structurally by name (`worktree.py`) |

Its own `libs/deepagents/THREAT_MODEL.md` rates the shell allow-list *"an ergonomic auto-approve
heuristic, not a security boundary"* (first-token matching, defeated by `python3 -c`), and
`backends/sandbox.py:962-968` concedes `BaseSandbox` *"does not reduce or partition the trust
boundary of `execute()`."* On the axis where a migration harness is most exposed, the reference is
weaker than what is already shipped here.

### 4. Adoption candidates — **Agent Recommendations, NOT YET IMPLEMENTED**

Four, all narrow, none architectural:

1. **Capture-at-source execute offload** (`backends/sandbox.py:837-870`). A shell wrapper redirects
   combined output to a file (10 MiB cap via `head -c`), returns 5 head + 5 tail lines inline, and
   smuggles the exit code back through a `.ec` sidecar plus sentinel line, so a large build log
   never crosses the RPC boundary. This addresses a problem `CLAUDE.md` already documents — a
   32 KiB `stdout_tail` truncation that makes `--json` unreliable for `ast-grep` failure detection.
   Note it ships **off**: `enable_capture_offload` defaults `False` (`sandbox.py:974`).
2. **Hook events as a verification insertion point** (`libs/code/.../hooks/runner.py:46-163`).
   Twelve subprocess events; exit 2 blocks; JSON `permissionDecision` with most-restrictive-wins
   reduction (`hooks/reducer.py:56`). This is the clean seam for a post-edit `ast-grep`/Bazel gate,
   against a surface the ledger records as having zero coverage at five checkpoints.
3. **`PatchToolCallsMiddleware`** (`middleware/patch_tool_calls.py:14-46`) — synthesizes missing
   `ToolMessage`s for orphaned tool calls, distinguishing truncated-args from cancelled. The
   crash-recovery path after a killed worker.
4. **The sandbox-ownership idiom** (`libs/code/.../integrations/sandbox_factory.py:133`):
   `should_cleanup = sandbox_id is None` — pass an id to attach, omit to own. This is precisely the
   ownership discipline **D32** needs (`INTEGRATION_HONESTY.md:1539`: the container leaks on every
   timeout because `buildverify._argv` bypasses `ContainerSandbox.run`'s `finally`).

### 5. Convergent findings — the strongest evidence in the set

Where mutually independent references agree, the agreement outweighs any single source's authority:

- **Adversarial verification by a *different* model that cannot write its own results.**
  Cloudflare's Validator is deliberately given no findings-emission tool; Visa's S6 opens a fresh
  session per finding instructed to "assume it is WRONG" and refuses to launder an unparseable reply
  into `FALSE_POSITIVE`; `deepagents`' `middleware/rubric.py` runs a separate grader agent.
  Cloudflare states the reason plainly: *"If a Hunter is allowed to grade its own homework, it will
  confidently validate everything it outputs."* This harness satisfies it **mechanically** for
  builds (ADR-0044), which is stronger than an LLM judge — but has **no equivalent for semantic
  verification of AST transforms**, where an exit code proves the tree parses, not that it means the
  same thing.
- **Deterministic gate strictly *before* LLM judgment.** Visa's S5 prefilter applies AST backfill
  only *after* its evidence gate, specifically so backfill cannot satisfy the gate. The ordering is
  the whole mechanism and is easy to invert by accident.
- **A per-agent context ceiling.** Cloudflare holds context under 25% of the window by giving each
  agent one narrow question; Visa imposes hard byte caps in the tool layer; `deepagents` triggers
  summarization at 0.85 of `max_input_tokens`. Three unrelated mechanisms, one principle.
- **Build failure is data, not harness error.** `deepagents` keeps `status="success"` on non-zero
  exit and puts the code on `ToolMessage.artifact` (`backends/protocol.py:774-784`); ADR-0044
  reached the same conclusion independently here. Convergence on an already-decided choice is a
  reason to leave it alone (Rule 3), not to revisit it.
- **An existence proof for Guardrail 3.** Visa's `vvaharness` consumes `deepagents` in production
  behind a `Harness` ABC with vendor-neutral `ToolPolicy`/`PermissionsPolicy`/`SubagentDefinition`
  dataclasses, backend swappable by YAML `via:`. Its `read_only_middleware()` records a hard-won
  detail: `create_deep_agent(middleware=...)` applies to **the parent stack only** — subagents keep
  write tools unless given their own copy. **If any `deepagents` code is ever consumed here, this is
  the shape**, and that constraint is the first thing to test.

### 6. Evidence tiers — what may and may not be cited

Guardrail 6 forbids passing an unmeasured number into an ADR or spec. The references stratify, and
the stratification is itself a finding:

- **Citable.** Cloudflare's two documents carry a real measured funnel (20,799 raw candidates →
  12,057 surviving validation; rejection rate 40% → 11%) and explicitly **refuse** to claim a
  recall figure: *"any claimed recall number is entirely speculative."* Caveat: the "North Star"
  section reads as a modelled scenario and the document does not disambiguate it from audited runs,
  so only the funnel figures are safe.
- **Citable as self-reported only.** OpenAI's Codex essay (~1,500 PRs, 3.5 PRs/engineer/day,
  six-hour unattended runs) is first-person with no baseline, and disclaims its own generality:
  *"should not be assumed to generalize without similar investment."*
- **Honest, but offers nothing to cite.** Visa's harness is substantial working code and states
  outright it has **"No published accuracy numbers yet."** That honesty is why its *mechanisms* are
  trustworthy even though its *numbers* do not exist.
- **Not citable.** `harness-engineering-ai-complete-guide-to-agent-harness.md` is SEO content
  marketing; every figure in it ("83%→96% via verification", "30–50% token cost cut", the 36%
  compounding-failure arithmetic) appears with no citation or methodology. Its six-component
  taxonomy may be borrowed as framing; **no number from it may enter `docs/`.**
  `Agent-Harness/`'s single quantitative claim (~40% stalled-task recovery) is likewise unsourced.

### 7. Anti-patterns observed — recorded so they are not copied

- **Unenforced invariants asserted in prompt text.** `deepagents`' `edit_file` docstring claims
  *"You must read the file before editing; this tool errors otherwise"*
  (`middleware/filesystem.py:1247`). **No such enforcement exists** — two subagents independently
  grepped for read-tracking state across the middleware and every backend and found none: no
  `files_read` set, no mtime or hash comparison. Had this been taken at face value it would have
  entered an ADR as a real safety property. This is the exact failure mode Guardrail 2 exists to
  catch, found in the wild.
- **Reconstructing typed data by parsing your own prose.** Visa's S9 rebuilds SARIF by parsing its
  own Markdown report rather than serializing the typed model.
- **Masking an exit code you need.** `deepagents`' sandbox grep ends `|| true`
  (`backends/sandbox.py:705-712`), making grep's exit 2 indistinguishable from zero matches — the
  same four-state-collapse family already tracked here as D29/D34–D46.
- **Building what agents do not reach for.** Cloudflare wired Semgrep in fully and *"the Hunters
  invoked it zero times in a month of runs,"* while an informal free-text "Wishlist" tool was
  written to 25,472 times.
- **Interactive-assistant defaults in a batch harness.** `deepagents`' coding
  `system_prompt.md:134` instructs the model to stop after three attempts and **ask the user** —
  the direct opposite of Rule 11, which requires marking `REQUIRES_HUMAN_INTERVENTION` and moving
  to the next item. A borrowed prompt can import a borrowed operating model.

### 8. Decision

**`deepagents` is NOT adopted as a framework, runtime, or dependency.** It is recorded as a
mechanism catalogue and a reference implementation of context economics. `references/deepagents/`
stays untracked and READ-ONLY under §5 of `CLAUDE.md`.

**Should any `deepagents` code ever be consumed, it is consumed behind a local `Protocol`** per
Guardrail 3 — never as a vendor singleton reached from orchestration logic — following the ABC shape
Visa has already proven in production, and testing the parent-stack-only middleware constraint
first.

**The four §4 candidates are opportunities, not obligations.** Each requires its own decision, its
own tests under Rule 9, and — for anything claiming a size, a rate, or a cost — its own measurement
under Guardrail 6 before it may be written down as fact.

### 9. Alternatives rejected

- **Adopt `deepagents` as the agent layer.** Rejected: it authors no loop, ships no local sandbox,
  has no retry or attempt counter, no staleness detection, and no AST layer — the four things this
  harness most depends on. It would replace stronger machinery with weaker.
- **Dismiss it and delete the clone.** Rejected: the context-economics middleware is genuinely ahead
  of anything here, and this harness will need it the moment any worker holds a conversation.
- **Fold the seven references into a single ranking.** Rejected under Rule 7 — four are prose, one
  is a whitepaper with no code in-tree, and two are working codebases. Ranking a blog post against a
  45.7k-LOC package on "mechanism" would average away exactly the distinction that matters. They are
  kept in separate tiers, and §6 records why.
- **Open D-numbers for the §4 candidates.** Rejected: `INTEGRATION_HONESTY.md` is a defect ledger.
  An unexploited opportunity is not a defect, and diluting the ledger with wishlist items would make
  the D-numbers stop meaning "something here is wrong."

### 10. Scope correction — `examples/better-harness/`, and the one citable number in the whole reference set

**The eight-agent sweep in §1 scoped to `libs/`. It never read `examples/`.** That gap is closed here
rather than in a new ADR, because it corrects this entry's own coverage and reverses none of its
decisions. Nothing in §1–§9 changes.

**What the artifact is.** `examples/better-harness/` (3,405 LOC, `version = "0.1.0"`,
`dependencies = []`) is a **harness optimizer**: an outer Deep Agent reads eval failures and edits an
inner agent's declared *surfaces* — prompt text, tool files, skill files, middleware implementation,
and **middleware registration** — after which the evals rerun and the edit is kept only if the score
improves. It calls itself "a research artifact."

**Its lineage claims were verified against canonical sources in-session, not recalled.** All three
exist:

- **arXiv 2603.28052** — *"Meta-Harness: End-to-End Optimization of Model Harnesses"* (Lee, Nair,
  Zhang, Lee, Khattab, Finn; submitted 30 Mar 2026). Genuinely an automated harness-search paper.
- **`github.com/karpathy/autoresearch`** — exists, is karpathy's, ~94k stars. **The
  characterization is a stretch**: it optimizes *nanochat training code* against `val_bpb`, not
  another agent's harness, and its `program.md` instruction file is human-edited by design. The
  README says only "inspired by," so this is a stretch and not a falsehood. **It carries no licence
  file**, so it is all-rights-reserved and not reusable despite being cited.
- **LangChain's "Improving Deep Agents with harness engineering"** (Vivek Trivedy, 17 Feb 2026).

**The citable figure, and exactly what it does and does not support.** That blog post reports
**"13.7 points from 52.8 to 66.5 on Terminal Bench 2.0,"** with the model held fixed at
`gpt-5.2-codex`, across 89 tasks orchestrated by Harbor on Daytona sandboxes. Under §6's tiers this
is **citable as a vendor's own measurement on a public benchmark** — the second-best-evidenced claim
in the reference set after Cloudflare's funnel, and the only one anywhere in `references/` that
isolates a harness change against a fixed model. Its limits, which must travel with it: it is a
**single before/after pair** with no variance, no seed count, and no per-intervention ablation in
what was retrieved; and it is LangChain measuring LangChain's own product.

**Read the attribution before borrowing the number.** The 13.7 points came from **hand-engineered**
interventions the post names — self-verification loops, context injection, loop-detection
middleware, and a staged reasoning budget. **They were not produced by running `better-harness`.**
The optimizer is the attempt to *automate* what the post did by hand, and it is the weakest link in a
chain whose other two are strong. Citing the figure as evidence for the optimizer would be a
category error of exactly the kind Guardrail 6 exists to prevent.

**Why the artifact is a specification and not an instrument.** For a tool whose entire purpose is
measurement-driven optimization, the measurement apparatus is the weakest component:

1. **No determinism control of any kind** — `temperature|seed|random|repeat|n_trials` returns zero
   hits across the package. **One un-seeded eval run per split per candidate** is the sole basis for
   keep/discard, and the shipped example gates on 2 train + 2 holdout cases, so **a single flaky case
   flips an accept**. No repeated trials, no significance test, no re-verification of the incumbent.
2. **Infrastructure failure is scored as regression.** The pytest runner maps `<failure>` and
   `<error>` to the same `status="failed"`, and an absent case to `status="missing", score=0.0`;
   `returncode` is stored and never consulted. An import error is indistinguishable from a real
   regression, so the loop can hill-climb on noise. (A *missing* `junit.xml` does instead abort the
   whole split — loud, but a different shape.)
3. **The holdout reaches the outer agent through three channels**, not one: the acceptance gate sums
   `train.passed + holdout.passed`; train artifacts are copied **by whole file**, and the shipped
   demo puts train, holdout and scorecard cases in a single test file, so the private assertions ship
   verbatim into the proposer workspace; and each iteration's accept/reject verdict is carried
   forward in `visible_history.md`, a one-bit readout of the holdout delta. `README:118` concedes the
   split "is not a hard sandbox boundary yet."
4. **The acceptance criterion is pinned by no test.** Changing it to train-only leaves the suite
   green — both end-to-end fakes fix every surface at once, so train and holdout move together and no
   test distinguishes the criteria. Combined with a dead `combined_passed` helper and a decision
   record that persists train counts only, the holdout's presence in the gate reads as **drift rather
   than a considered tradeoff**.
5. **Nothing prevents a surface targeting the evals.** Validation checks kinds, split names, id
   uniqueness and strata parity; it never compares `surface.target` against the eval paths or grader
   module, and `module_attr` reaches any importable attribute inside the eval process.
6. **Revert is exception-safe but not crash-safe** — a proper `finally`, but in-memory backups, no
   journal and no atomic rename. SIGKILL mid-eval leaves agent-authored code in the real target
   workspace, and the next run then snapshots the mutated file as "original."

**Decision. Not adopted, and not a defect here** — no ledger entry, per §8's rule that an
unexploited opportunity is not a defect and an external artifact's flaws are not ours. Three things
are kept as ideas, all **Agent Recommendations**: the *declared-surface* pattern as a containment
primitive; the observation at its `README:46` that **middleware needs both implementation and wiring
exposed** or the outer agent cannot switch it on; and the three-tier `train`/`holdout`/`scorecard`
split, whose unbiased tier — run on baseline and final only — is the design this artifact already has
and ships as **optional**, which is the same shape as **D48**: the correct guard exists and is not on
the path that runs.

**A note on method, since it recurs.** The web summarizer used during citation checking reported
`autoresearch` as MIT-licensed; the GitHub licence API returned `Not Found`, and the primary source
won. That is the third instance in this evaluation of a confident secondary source contradicted by
the artifact it describes — after `edit_file`'s unenforced read-before-edit invariant (§7) and
`ARCHITECTURE.md`'s phantom default-stack middleware. **Prefer the primary source, every time.**

---

## ADR-0070 — The `openai_compatible` backend is **one file**, and its hard part is **error translation, not transport**: `classify_exception` branches on our own typed exceptions, so every wire condition a local vLLM can produce must be mapped inside `invoke` or it lands in `UNKNOWN` — with a **400 on `guided_json` reclassified as capability drift, not failure**, and **input-side context overflow given its own class** because `OutputTruncated` covers only `finish_reason == "length"`

**Status: DECIDED, NOT YET IMPLEMENTED.** Verified against `f12a954`. `src/fleet/llm/backends/`
**does not exist**: `discover()` (`llm/client.py:355-365`) catches the `ImportError` on
`fleet.llm.backends` and returns the empty registry, and `grep -rn register_backend src/` finds only
the decorator's own definition. `docs/INTEGRATION_HONESTY.md`'s LLM-backends row already states the
consequence exactly — **"FAKE, correctly. Every model call in the suite goes through a fake
`ModelClient` … no request has ever left the process"** — and names the closing move as "a
recorded-cassette or live-endpoint contract test per backend." This ADR decides the shape of that
backend; it writes none of it.

### 1. Why now, and what is already built

The operator runs NVIDIA DGX Spark hardware serving open-weight models through **vLLM behind an
OpenAI-compatible endpoint**. Everything needed to point a role at it exists **except the backend**:

- `BackendTarget.base_url: str | None` — `src/fleet/models/tasks.py:88`, commented *"required by
  `openai_compatible`; ignored by others"*.
- `SHIPPED_BACKENDS` already contains `"openai_compatible"` (`settings.py:107`), and
  `_REQUIRED_TARGET_FIELDS` already enforces `openai_compatible → ("base_url",)`
  (`settings.py:110-114`).
- `StructuredOutputMode.CONSTRAINED` is annotated **"server-side constrained decoding (e.g. vLLM
  guided JSON)"** (`models/enums.py:231`) and gated on
  `ModelCapabilities.supports_constrained_decoding` (`models/tasks.py:67`).
- The cache key covers `backend` and `model_id` (`llm/cache.py`), so repointing a role cannot serve
  an Anthropic-authored answer to a vLLM-routed call.
- `ModelBackend`'s own docstring already declares the target shape: **"One transport. A new provider
  is ONE file under `llm/backends/` + one `@register_backend`"** (`llm/client.py:295-296`).

So a `config/models.yaml` profile naming `backend: openai_compatible` with a Spark `base_url`
**passes settings validation today** and dies at `UnknownBackend` on first dereference. The gap is
one file, and the config surface is not what makes it hard.

### 2. Decision — the backend's contract

**One module, `src/fleet/llm/backends/openai_compatible.py`, one `@register_backend` class**, meeting
`ModelBackend` (`client.py:295-303`): `name`/`version` ClassVars, `declared_capabilities(target)`,
and `async invoke(target, messages, schema, mode, *, max_output_tokens, timeout_s) -> BackendReply`.
Per the Protocol's existing contract, **backends never validate, never retry, and never pick their
own mode** — negotiation stays in `LadderModelClient`, Pydantic validation stays at
`client.py:839-854`.

`declared_capabilities` is **read from config, not probed**. A vLLM deployment's tool-calling and
guided-decoding support depends on the server's launch flags, which the harness cannot see; guessing
from `model_id` would be the unmeasured-number failure Guardrail 6 forbids. Per-target capability
overrides already exist and are the honest mechanism.

### 3. The error-translation table — this is the actual work

`workers/classify.py:245-254` branches on **our** typed exceptions and never on an HTTP status:
`BudgetExhausted→BUDGET_EXHAUSTED`, `TierUnavailable→BACKEND_UNAVAILABLE`,
`TransportError→TRANSIENT_INFRA`, `SchemaUnsatisfied|MalformedReply|ValidationError→PARSE_ERROR`,
**else `UNKNOWN`**. Any raw SDK exception escaping `invoke` is therefore an `UNKNOWN` — the bucket
that means "we do not know what happened," charged against the retry ladder.

| Wire condition from a local vLLM | `invoke` must raise | Resulting `FailureClass` |
|---|---|---|
| Connection refused, DNS failure, read timeout | `TransportError(trigger="CONNECTION")` (`client.py:136-140`) | `TRANSIENT_INFRA` |
| `429` | `TransportError(trigger="RATE_LIMIT")` | `TRANSIENT_INFRA` |
| `5xx` | `TransportError(trigger="SERVER_ERROR")` | `TRANSIENT_INFRA` |
| Reply present but unparseable / schema-violating | `MalformedReply` / `SchemaUnsatisfied` | `PARSE_ERROR` |
| `finish_reason == "length"` | `OutputTruncated` (`client.py:92`) | ladder-handled |
| **`400`, server rejected our `guided_json` schema** | **NOT an error — see §4** | **capability drift** |
| **`400`, input context length exceeded** | **new `ContextOverflow`— see §5** | **new class** |

The two bolded rows have **no home today** and are the reason this ADR exists rather than being a
one-line task.

### 4. A `400` on `guided_json` is a **capability discovery**, not a failure

If `declared_capabilities` claims `supports_constrained_decoding` and the server rejects the schema,
the true fact learned is *this deployment cannot compile this grammar* — not *this call failed*.
Retrying is guaranteed to fail identically, so classifying it as any retryable `FailureClass` burns
a rung for nothing.

**Decision:** the backend raises a dedicated `ConstrainedDecodingUnsupported`, and
`LadderModelClient` treats it exactly as it already treats a down-rung move — emit `CapabilityDrift`
(`client.py:631`) and re-invoke at the next rung of the §7.7 ladder (`CONSTRAINED` → `TOOL_CALL` →
`PROMPTED`). The ladder and its drift signal already exist; this adds one trigger, not a mechanism.
The alternative — letting it reach `classify_exception` — spends a retry to learn a static fact
about the deployment.

### 5. Input-side context overflow needs its own class

`OutputTruncated` covers `finish_reason == "length"` — the **output** side — and its exhaustion path
becomes `BUDGET_EXHAUSTED` (`client.py:761`). A vLLM `400` for *input* context length is a different
fact with a different remedy: the prompt must shrink, which is a `ContextPolicy` concern
(ADR-0021's rung ladder), not a budget one.

**Decision:** add `ContextOverflow(LlmError)` raised by the backend, and map it to a new
`FailureClass.CONTEXT_OVERFLOW` that is **retryable only via a context-reducing rung change**, never
as a same-rung repeat. **Cost, stated plainly:** `FailureClass` is consumed widely and adding a
member touches its exhaustiveness sites. That cost is accepted because the alternative — leaving it
in `UNKNOWN` — makes the single most predictable open-weight failure mode indistinguishable from
"we do not know what happened," which is precisely the four-state-collapse family this project
already tracks as D29 and D34–D46.

### 6. What the Nemotron profile contributes, and what of it applies here

Source: `references/deepagents` at the SHA pinned in **ADR-0069 §1**
(`1c6d358c60306aad2af0067dcca76f85f4deeba1`), file
`libs/deepagents/deepagents/profiles/harness/_nvidia_nemotron_3_ultra.py` (1,826 lines) — the
densest record of driving an open-weight model that exists in the reference set. **Tailoring it
matters more than copying it**, because that profile is written for an *agent loop with
model-callable tools* and this harness has neither.

**Applies directly:**

1. **Retry classification must be string- and attribute-based, not type-based.**
   `_is_rate_limit_exception` (`:209`) matches on exception **class name** containing "ratelimit",
   `status_code == 429`, **and** the substring "rate limit" in the message — because an arbitrary
   server behind an arbitrary client library does not raise a typed `RateLimitError`. Our
   `classify_exception` is type-based **by design and stays so**; this lesson lands entirely inside
   `invoke`, which is exactly where the translation table in §3 lives. Directly informs §3's first
   three rows.
2. **Reasoning tags leak into content.** `_strip_reasoning_tags` (`:571`) removes `<think>…</think>`
   from the message body while preserving it under `additional_kwargs["reasoning_content"]`. For us
   this is a **`PROMPTED`-rung correctness requirement**: an un-stripped `<think>` block makes
   parse-and-repair fail on output the model considered non-final. Strip, but record — a discarded
   reasoning block is unattributable when the parse later fails.
3. **Empty content is a wire hazard.** `NemotronToolCallShim` substitutes `"(empty tool result)"`
   (`:110`) because some providers reject empty content blocks. Applies to any message we construct
   with an empty body.
4. **Nothing gives you context sizing for free** (`summarization.py:266-291`) — absent model
   metadata, conservative fixed defaults are assumed silently. For us this is the argument that
   `max_input_tokens` belongs in **config per target**, and it is the same fact §5 addresses from the
   error side: know the window, or discover it by 400.

**Applies only in `TOOL_CALL` mode:** `NemotronTextToolCallParser` (`:687`) parses tool calls the
model emitted as plain text in three shapes — `<function=name><parameter name=x>`, an alternate
`<function><name>`, and bare JSON `{"tool":…, "args":…}` — and **validates every parsed name against
the live tool set** (`:484`) so hallucinated tools are dropped. This harness exposes **no
model-callable tools**: `TOOL_CALL` is schema-smuggling only (`enums.py:229`). The transferable part
is therefore narrow but real — **an open-weight model may emit our smuggled schema call as text
rather than as a structured `tool_calls` field**, and a backend that reads only the structured field
will see an empty reply and raise `MalformedReply` for what is actually a recoverable format
variance. The name-validation half is unnecessary for us; there is exactly one legal tool name.

**Does not apply — recorded so nobody ports it by analogy:** the pagination continuation notice
(`:134`, `:465`), the per-turn progress budgets of 16 model calls / 48 tool results / 3 repeats
(`:990`), and roughly 700 lines of regex behavioural guards (`:1069`, `:1336`, `:1416`, `:1647`).
All three presuppose an autonomous agent loop that self-terminates badly. `PhaseRunner._drive` is not
that loop, and its bounds are the retry ladder and the five `Limits` semaphore classes.
`ChatNVIDIAMessageCompatibilityMiddleware` (`:587`) is a client-library quirk with no OpenAI-wire
analogue.

**Evidence caveat, per Guardrail 6.** `libs/evals/MODEL_GROUPS.md:149` records **`nvidia` (0
models)**, and Nemotron 3 Ultra appears in **no eval group at all** — so that 1,826-line profile is
**not covered by its own project's published eval matrix**. Its lessons are specific, plausible, and
**unvalidated**. They are adopted here as *design inputs to a translation table*, never as measured
claims, and each must be confirmed against a real Spark endpoint before any of it is written down as
fact.

### 7. Out of scope

Streaming (`ModelClient.stream` exists but no phase consumes it); prompt caching, which has no
OpenAI-compatible equivalent to Anthropic's; a second backend — `anthropic.py` is named in
`pyproject.toml:33-36` and remains unwritten, and nothing here decides it; and the
recorded-cassette contract test the honesty ledger asks for, which is a testing decision this ADR
deliberately does not pre-empt.

### 8. Alternatives rejected

- **Ship a generic `openai` backend and let errors fall to `UNKNOWN`.** Rejected: it makes the two
  most predictable open-weight failure modes — an uncompilable grammar and an over-long prompt —
  indistinguishable from an unclassified crash, and both would be charged against the retry ladder as
  if retrying could help.
- **Probe capabilities at startup instead of declaring them in config.** Rejected: a probe measures
  the endpoint at one instant and the answer is a launch-flag property; a stale probe is an unmeasured
  number wearing a measured label.
- **Reuse `OutputTruncated` for input overflow.** Rejected: it collapses two conditions with opposite
  remedies (shrink the prompt vs. raise the output cap) into one class — the same failure this project
  already tracks thirteen times over.
- **Port the Nemotron middleware stack wholesale.** Rejected: over half of it governs an agent loop
  this harness does not have, and per §6 its own project does not eval it.

### 9. Amendment — §4 was greedy, and an unmatched `400` must fall through to `UNKNOWN`

Appended rather than edited into §4, because ADR-0070 is committed (`32365cf`) and this file's header
rule keeps entries intact. §4 stands as written for the case it names; this section bounds it.

**The defect in §4 as written.** §4 says a `400` on `guided_json` is reclassified as
`ConstrainedDecodingUnsupported` — capability drift, not failure. But **§3's table carries rows for
two different `400`s** (an uncompilable grammar, and input context length exceeded), and **§6.1
concedes that an arbitrary server behind an arbitrary client library requires string- and
attribute-based matching** rather than typed exceptions. Those three statements cannot all hold: if
matching is heuristic, then assigning *any* `400` to whichever named cause is tested first is a
substring proxy standing in for a fact it did not establish. That is the four-state-collapse family
(D29, D34–D46) reintroduced prospectively, in the very ADR that cites it as the reason for §5.

Found by a read-only subagent auditing the "unknown is never laundered into a negative" pattern from
Visa's S6 against our own ledger — i.e. by the ledger's own thesis applied to a decision written two
hours earlier.

**Amendment.** The backend's `400` handling is **ordered and non-exhaustive**, and the fall-through is
mandatory:

1. Match a **positive, specific** signal for an uncompilable grammar → `ConstrainedDecodingUnsupported`
   → `CapabilityDrift` → next rung down (§4 unchanged).
2. Match a **positive, specific** signal for input context overflow → `ContextOverflow` (§5).
3. **Anything else — including a `400` whose body matches neither — raises the generic transport
   error and lands in `FailureClass.UNKNOWN`.** It is **not** assigned to (1) or (2) by elimination,
   by ordering, or by "it was probably the schema."

**Why `UNKNOWN` is the right floor and not a cop-out.** `workers/classify.py`'s `else` arm sets
`UNKNOWN` with `retryable=True`, so the ladder advances and a persistent case terminates at
`REQUIRES_HUMAN_INTERVENTION`. It is never silent and never a success. An unmatched `400` costs
retries — real, and the honest cost of not knowing. Guessing costs a **wrong verdict**, and §4's
guess would specifically suppress a real failure by re-labelling it a capability property of the
deployment, which is the expensive direction.

**Consequence for the eventual test.** A backend test suite that only feeds it the two recognised
`400` bodies proves nothing about this rule. The acceptance criterion is a **third** case: an
unrecognised `400` body must reach `UNKNOWN`, and a test must fail if a future matcher widens to
swallow it.

### 10. Amendment — §5's cost was an unmeasured number, and Guardrail 6 says that is worse than none

Appended rather than edited into §5, because ADR-0070 is committed (`32365cf`) and this file's header
rule keeps entries intact. §5's decision to add `FailureClass.CONTEXT_OVERFLOW` stands; this section
replaces its cost sentence.

**The defect in §5 as written.** §5 says: *"`FailureClass` is consumed widely and adding a member
touches its exhaustiveness sites. That cost is accepted because…"* Nobody had counted the
exhaustiveness sites, or checked whether any exist. Guardrail 6 is explicit: *"Never pass an
unmeasured number into an ADR or spec brief… a false claim carrying a 'measured' label is worse than
no claim."* "Touches its exhaustiveness sites" is exactly such a claim — it asserts a shape, and it
turns out to be **wrong in both directions**: there is less compiler-enforced cost than implied, and
more silent risk than implied.

**Measured, against `e605f0d`.** Replacement sentence: **4 silent-fallback sites in 3 files; 0
hard-break sites; 0 DB migrations; 0/134 existing tests would catch an omission.**

**No exhaustiveness to touch.** `FailureClass` (`src/fleet/models/enums.py:272-299`) has **17
members**, not fewer — recounted directly off the source rather than carried over from an earlier
estimate. There is no `Literal` mirror, and `grep -rn assert_never src/` returns **nothing**: zero
compiler-enforced exhaustiveness exists anywhere in this codebase for this type.
`src/fleet/state/schema.sql:367,637` declare `failure_class TEXT` with **no `CHECK (… IN (…))`** —
unlike `status` (`:347`, seven-way `CHECK`) and `state`/`stub_fidelity` (`:306,308`), which do
enumerate their domains. **Adding a member requires no migration.** `src/fleet/llm/schemas.py:336`
and `src/fleet/llm/client.py:498` both derive their JSON-schema enum from
`model.model_json_schema(...)` at call time, so a new member is picked up automatically, not hand-
maintained. §5's cost sentence describes a mechanism that is not there.

**The real risk is silent, not loud — four fallback sites in three files:**

1. `src/fleet/workers/base.py:130-141` — `NON_RETRYABLE: frozenset[FailureClass]`, consumed by
   `is_retryable()` (`:148-152`). Membership is a set literal; an omitted member does not error, it
   **silently defaults to retryable**.
2. `src/fleet/orchestrator/retry.py:148-244` — `RetryPolicy.decide()` branches explicitly only for
   `DISK_EXHAUSTED` (`:170`), `BACKEND_UNAVAILABLE` (`:187`), and `TRANSIENT_INFRA` (`:200`);
   everything else — `CONTEXT_OVERFLOW` included, once added — falls to the generic charge-an-
   attempt tail (`:221-244`). **This is exactly where §5's "retryable only via a context-reducing
   rung change, never a same-rung repeat" needs its own branch, and nothing in `decide()` forces one
   to exist.**
3. `src/fleet/workers/base.py:500-519` — `classify_exception()`, an if/elif chain ending
   `return FailureClass.UNKNOWN` (`:519`).
4. `src/fleet/workers/classify.py:238-258` — `_error_for()`, `else: failure_class =
   FailureClass.UNKNOWN` (`:253-254`). This exact site is already cited **inside ADR-0070 itself**
   (§3) as one of the `UNKNOWN` catch-alls this ADR exists to shrink — the author cited it without
   counting it as a cost of §5's own decision.

**Test-side.** `grep -rc FailureClass tests/` totals **134** references across **12** test files
(`test_workers_build.py` 38, `test_workers_scan.py` 22, `test_runner.py` 20, `test_workers_base.py`
15, `test_retry.py` 14, `test_state_models.py` 9, `test_graph_sequence.py` 6,
`test_workers_transform.py` 5, `test_db.py` 2, `test_llm_client.py`/`test_cli.py`/`test_bazel.py` 1
each). `grep -rnE "for .* in FailureClass|list\(FailureClass\)|len\(FailureClass\)" tests/` returns
**nothing**: zero tests iterate the member set, and none pins `NON_RETRYABLE`'s membership (the one
other hit on that name, `test_runner.py:1435`, is prose in a docstring, not an assertion). **All 134
references pass unchanged after adding a member — 0/134 would catch an omission.**

**The consequence that matters.** The cost of adding `CONTEXT_OVERFLOW` is not the edit — §2's
"one file" framing and this section's own migration/exhaustiveness count both confirm there is
almost nothing to touch. **The cost is that nothing fails if you forget to touch it.** A new member
left out of `NON_RETRYABLE`, or out of `RetryPolicy.decide()`'s explicit branches, degrades silently
to a default (retryable-by-omission, generic-attempt-charge-by-omission) rather than raising or
failing a test. §5 therefore obliges a new test that does not exist today: one that iterates
`FailureClass` and asserts every member is classified somewhere load-bearing (at minimum, that every
member has a deliberate `NON_RETRYABLE` verdict and a deliberate `RetryPolicy.decide()` outcome, not
an inherited default). Shipping the member without that test reproduces, inside this ADR's own
change, the exact failure-mode class (D29, D34–D46) §5 invokes to justify making the change at all.

**Checked and found genuinely inert, recorded without a ledger number.**
`src/fleet/workers/base.py:740-742` defaults a missing `result.error` to
`WorkerError(failure_class=FailureClass.UNKNOWN, retryable=False)` — inconsistent with the four
sibling sites that choose `retryable=True` for an unclassified or timeout-shaped failure
(`base.py:531` via `is_retryable()`, since `UNKNOWN` is not in `NON_RETRYABLE`; `base.py:852`
`_refusal`; `base.py:922` `_abandoned`; `classify.py:255-258` `_error_for`, which excludes only
`BUDGET_EXHAUSTED`/`BACKEND_UNAVAILABLE` from `retryable=True`). Reachability was checked, not
assumed: the `_status_matches_its_evidence` validator (`base.py:391-407`) raises on construction if
`status` is `"failed"` or `"timeout"` and `error is None`, and `grep -rn model_construct src/` finds
**no** bypass of that validator anywhere in this codebase. Every path that reaches `base.py:740`
with `status` in `{"failed", "timeout"}` therefore already carries a non-`None` `result.error`, so
the `or WorkerError(...)` fallback can never execute. It is a real inconsistency in the code's stated
defaults, but an unreachable one — not a defect with a live consequence, so it does not get a D-
number.

---

## ADR-0071 — `open-swe` is **`deepagents` plus an application**, and four of its parts are worth **lifting as code**: the **prepare-run fingerprint** (the wired drift gate D48 says we lack), **capture-at-source offload** (measured thresholds at last), **model-proposes/host-adjudicates** (the semantic-verification shape ADR-0069 §5 said we had no answer for), and **`shlex`-parse-don't-regex** — against a fifth finding that is a **review heuristic, not a mechanism**: safety code that is built, tested, documented, and **wired nowhere**, while operators are told to grant real permissions on its basis

**Status: DECIDED as an extraction list. Every item in §3 is NOT YET IMPLEMENTED.** No `src/`
change accompanies this entry, no ledger number is opened (per ADR-0069 §8, an unexploited
opportunity is not a defect), and each extraction still needs its own decision, its own tests under
Rule 9, and its own measurement under Guardrail 6.

### 1. Provenance and licence — read this before copying anything

`langchain-ai/open-swe` cloned to `references/open-swe/` at
**`e712a9ef950cda7200e7761400b169e09fb075be`** (2026-08-17T17:43:56-04:00). 36 MB, 398 Python files,
235 TS/TSX. Untracked by the same `.gitignore:87` (`references/*/`) rule as the other references, so
**the SHA is what makes these citations falsifiable** — see ADR-0069 §1 for why that matters.

**Licence: MIT, "Copyright (c) LangChain, Inc."** (`LICENSE:1-3`, corroborated by
`pyproject.toml` `license = { text = "MIT" }`). Lifting code is therefore permitted **provided the
copyright notice and permission text travel with it.** Any file in `src/fleet/` that carries
adapted open-swe code must name the upstream file and this SHA in a header comment. This is not
optional politeness; it is the licence condition. Note the same does **not** hold for every
reference — `karpathy/autoresearch`, cited in ADR-0069 §10, ships **no licence at all** and is not
reusable.

**Every citation below was re-derived in the main session, not taken from the subagent that found
it.** Two the subagent reported did not survive: there is no `agent/tools/file.py` (open-swe has no
file tools of its own at all), and `filter_findings_for_publish` could not be located as a symbol —
the publish cap is described in `agent/review/publish.py:1-12`'s module docstring and the
deterministic normalizers are `agent/review/findings.py:61,70`. Both corrections are recorded rather
than quietly dropped.

### 2. What open-swe is, structurally

It **authors no loop, no context management, and none of its file/shell tools.** All nine
(`read_file, write_file, edit_file, delete, ls, glob, grep, execute, task`) come verbatim from the
pinned `deepagents==0.7.6`. Five LangGraph factories call `create_deep_agent`; termination is
ceilings (`recursion_limit` 9,999; a 5,000 model-call limit), never a verdict. There is **no
in-loop verification in code** — lint/test discipline is prompt text, and nothing gates a push or a
PR on a green run.

**Consequence worth internalising: the read-before-edit lie ADR-0069 §7 recorded is present here
too, one dependency hop away**, invisible to an open-swe-only audit. We caught it because ADR-0069
scoped to deepagents directly — that was scoping luck, not method. **Audit the pins, not just the
repo.**

### 3. The four extractions

#### 3.1 Prepare-run fingerprint — the D48 precedent

**Upstream:** `agent/middleware/prepare_run.py` — `_latest_message_fingerprint` (`:25`), the latch
(`:60-67`: compute, compare `prepared_state.get("run_prepared_for") == fingerprint`, return
`{"run_prepared": True, "run_prepared_for": fingerprint}`), and `_prepare_fingerprint` (`:69-76`).
Config side: `agent/server.py:906 _prepare_config_fingerprint`.

**What it does.** Hashes the latest message **plus** `{prepare_run_id, thread_id, source, repo,
plan_mode, draft_prs, model, effort}`. Setup is skipped only on an exact match, so **changing the
model re-prepares the run.**

**Why it fits.** D48 records that our own drift gate (`settings.drifted_sections`) is wired only to
`fleet resume`, which dead-ends at `_unavailable` (`cli.py:9792`), while the six verbs that actually
re-enter a run share `_phase_preflight` (`cli.py:866-878`) and never read `runs.config_digests`.
D48's entry argues the obvious fix — calling the gate from `_phase_preflight` — is wrong, because it
would make every phase verb *refuse* on drift an operator already accepted. **This is the shape that
resolves that objection: a fingerprint mismatch invalidates the prepared work rather than refusing
the command.** Re-derive, don't refuse.

**What to change.** Their fingerprint **does not cover the prompt template**, so a resume after a
prompt deploy reuses a cached `rendered_system_prompt` — they solved the model half and have our
exact gap on the prompt half. Ours must include `prompt_template_version`, and we already compute
it: `llm/cache.py`'s key covers `prompt_template_version` and `prompt_sha256` today. The layers
disagree exactly as D48 describes — the cache re-derives while the checkpoint above it hands back
pre-change work — so the fingerprint's job is to make the **checkpoint** as discriminating as the
**cache key** already is.

#### 3.2 Capture-at-source offload — measured thresholds

**Upstream (deepagents, at ADR-0069 §1's SHA):**
`libs/deepagents/deepagents/backends/sandbox.py` — the shell wrapper at `:843-873`. The mechanism in
one line (`:851`):

```sh
{ ( eval "$__da_cmd" ); echo "$?" > "$__da_ecf"; } 2>&1 | { head -c __MAXBYTES__ > "$__da_f"; cat > /dev/null; }
```

plus the head/tail readback (`:869-873`), the flag `enable_capture_offload: bool = False` (`:974` —
**ships off**), and `execute_with_offload` (`:1005`). Generic ToolMessage eviction:
`libs/deepagents/deepagents/middleware/filesystem.py:1678` (`_large_tool_results_prefix`), `:2742`
(capture path). **open-swe deliberately preserves the path** rather than breaking it behind its own
wrapper: `agent/utils/sandbox_state.py:277` (`execute_with_offload`), `:288-298`
(`aexecute_with_offload`).

**The measured numbers, which is what this section adds over ADR-0069 §4 candidate 1:** offload
triggers above **80,000 bytes** (4 chars/token × 20,000); readback is **5 lines / 2,000 bytes from
each end**; hard cap **10 MiB**; the exit code is preserved. Against `util/proc.py`'s 32 KiB tail
that is a **2.4× larger window plus a pointer instead of a truncation.**

**What to change — and this half matters.** Their `.ec` sidecar exists because `execute()` crosses
an **RPC boundary to a remote sandbox** and the exit code cannot ride back any other way. We run
Docker locally through `util/proc.py` and **already hold the real return code**. Do **not** port the
sidecar: re-encoding an exit code we already have as a string in a file is precisely the
four-state-collapse family this project tracks as D29 and D34–D46, and `ProcResult.ok` already
conflates started / timed-out / exit-124. **Take the head/tail-plus-file-pointer; leave the exit-code
smuggling upstream.**

**Measure before adopting.** `research-38` is already investigating what a real Bazel failure log
costs through `util/proc.py`. That number, not this ADR, is what justifies the change.

#### 3.3 Model proposes, host adjudicates — the semantic-verification shape

**Upstream:** `agent/review/approval.py` — `build_approval_assessment` (`:60`), the zeroing (`:82-84`:
`if effective_score is not None and blocking_ids: effective_score = 0`), and the deterministic
blocker list (`:86-96`: `invalid_assessment`, `team_auto_approve_disabled`,
`repo_auto_approve_disabled`, `open_blocking_findings`, `score_below_threshold`). In-diff anchoring:
`agent/tools/add_finding.py:120-125`, using `agent/review/diff.py:138 compute_diff_line_set` and
`:184 is_range_in_diff`. Host-formatted output: `agent/review/publish.py:1-12` — *"Review body: a
fixed, host-formatted summary line. The agent never writes prose here."* Deterministic normalizers:
`agent/review/findings.py:61 clip_suggestion`, `:70 normalize_finding_title`.

**The pattern.** The model self-scores 0–100 and proposes findings; **host code then overrides it**
— zeroes the score when any blocking finding is open, rejects findings anchored outside the diff at
creation time, caps and filters what publishes, and writes the summary prose itself so the model
never authors the verdict text.

**Why it fits.** ADR-0069 §5 recorded that we satisfy adversarial verification **mechanically** for
builds (ADR-0044) but have **no equivalent for semantic verification of AST transforms** — an exit
code proves the tree parses, not that it means the same thing. This is the missing shape, and it
preserves Rule 5: the model supplies judgment, deterministic code supplies the verdict.

**Where it lands.** `build_diagnosis` is generated on every rung-2/3 failure and **has zero readers**
(**D47**, ADR-0068). A host adjudicator would be its first legitimate consumer — but note D47's own
argument that wiring a reader merely to justify the writer is speculative under Rule 2. **That
tension is unresolved and this ADR does not resolve it**; it records that a principled consumer now
has a known shape.

#### 3.4 `shlex`-parse, don't regex

**Upstream:** `agent/middleware/pr_creation_guard.py` — `import shlex` (`:8`), the depth-limit
sentinel (`:20`), `_split_shell_tokens` (`:55`, `shlex.split(command, posix=True)`), and
`_expand_nested_shell_tokens` (`:85-98`), which recurses into nested `sh -c` payloads to
`_MAX_SHELL_EXPANSION_DEPTH` and rejects by returning an error ToolMessage **without invoking the
handler**.

**Honest applicability — this is the weakest of the four for us, and it is included because it was
asked for and because it is cheap insurance.** We do not have the problem it solves: `util/proc.py`
takes **argv lists and never a shell**, which is strictly stronger than parsing a shell string
after the fact. The pattern becomes relevant only where a **command string originates outside the
harness** — a model-proposed repair command, or any future model-callable surface. Should that ever
exist, this is the correct technique: parse and expand, never regex, and reject before dispatch.
Recorded now so nobody reaches for a regex later.

### 4. The fifth finding is a review heuristic, not a mechanism

This evaluation has catalogued **claim-without-code** four times (ADR-0069 §7, §10). open-swe adds a
worse variant: **code that exists, is tested, is documented — and is wired into nothing, while
operators are told to grant real permissions on its basis.**

- `WorkflowPushGuardMiddleware` — genuine human approval (Slack block, SHA fingerprint, refspec
  rewrite), implemented, exported, unit-tested, **instantiated in no agent stack**, while
  `INSTALLATION.md` instructs operators to grant **`Workflows: Read & write`** because of that flow.
- `ci_monitor` / `ci_autofix` — documented in `AGENTS.md`, **absent from the tree entirely**
  (`grep` returns nothing), and `INSTALLATION.md` justifies GitHub App permissions by them.

A permission grant justified by a control that never executes is **more dangerous than an
unimplemented docstring**, because it produces a real capability increase in the world. Two more in
the family: plan mode omits `edit_file`/`write_file`/`execute` from its exclusion list while two
files assert the agent is read-only, and the "read-only" reviewer receives write tools from
`create_deep_agent` on an unwrapped backend.

**The heuristic, adopted for our own reviews:** *treat every "the agent cannot X" as prompt text
until you have located the code that blocks it — and when a permission or capability is granted on
the strength of a control, verify the control is wired, not merely present.*

### 5. What is deliberately NOT taken

The sandbox (a rented LangSmith VM; the only vendor-free option is `SANDBOX_TYPE=local`, whose own
docstring says "no isolation… runs commands directly on the host", with `inherit_env=True`); the
GitHub proxy, which is **auth header injection, not egress control** — `match_hosts` attaches an
`Authorization` header and there is no deny rule anywhere, so it is not the network boundary it
resembles; sandbox lifecycle, where nothing is torn down by design (*"Intentionally has no
delete"*), leaking a live VM for up to 2 h and a stopped one for 30 days — the inverse of D32's
treatment of the same behaviour as a defect; and the reviewer eval, which has no holdout, learns its
per-repo prompt from a crawl of the goldens' own source repos, and measures an eval-only prompt at a
severity threshold the deployment does not use.

**One credit, recorded because it is rare:** open-swe publishes **no accuracy number anywhere** — no
F1, no precision, no recall. Under ADR-0069 §6's tiers it therefore joins Cloudflare and Visa in the
honest tier: no claim outruns its evidence, because no claim is made.

### 6. Alternatives rejected

- **Adopt open-swe as a coding-agent layer.** Rejected: it authors no loop, has no in-loop
  verification, no queue, no leases, no attempt counters, and no terminal human-escalation state —
  and its `execute` takes a raw shell string with no allow-list.
- **Take the offload wholesale, sidecar included.** Rejected in §3.2: the sidecar solves an RPC
  problem we do not have and re-introduces an exit-code collapse we already track thirteen times.
- **Open ledger entries for §3's four items.** Rejected on ADR-0069 §8's rule; these are
  opportunities, and the ledger means "something here is wrong."
- **Cite open-swe's reviewer eval as evidence for anything.** Rejected under Guardrail 6 and §5.

---

## ADR-0072 — D48's checkpoint drift gate **re-derives, not refuses**: `PhaseCheckpoint` grows a `config_fingerprint`, checked in `runner.py`'s `_re_entry` through the `ReEntry.REJECTED` path that already exists for tree-state mismatches, never in `_phase_preflight` — and the fingerprint closes the exact `prompt_template_version` gap ADR-0071 §3.1 found still open in open-swe's own version of this mechanism

**Status: DECIDED, NOT YET IMPLEMENTED.** Verified against landed code at `4846fb0`. No `src/`
change accompanies this entry — CLAUDE.md's Rule 10/Rule 11 discipline and Guardrail 6 both require
the decision to be recorded before code moves, and this ADR is that record for **D48**
(`docs/INTEGRATION_HONESTY.md`), which found the fault and deliberately declined to pick a fix.

### 1. The defect this closes, restated from D48

`settings.drifted_sections(baseline)` (`settings.py:1221-1236`) compares per-section config digests
against `runs.config_digests` and is correct and well-tested. Its only call site is `_resume_impl`
(`cli.py:9814-9868`), and `resume` itself dead-ends at `_unavailable("resume", …)`
(`cli.py:9811`). The six verbs that actually re-enter an interrupted run — `plan`, `build`,
`verify`, `migrate-repos`, `transform`, `pr` — share `_phase_preflight` (`cli.py:868-880`), whose
three refusals (`_check_schema_version`, `_resolve_run`, `_refuse_concurrent_mirror_run`) never
read `config_digests`. Editing a prompt template, a model id, or a budget and re-running an
interrupted phase silently hands back a checkpoint produced under the old configuration —
`_load_checkpoint` (`runner.py:987-995`) only ever rejects on `SCHEMA_VERSION_MISMATCH`,
`MODEL_MISMATCH`, `MALFORMED_ENVELOPE`, or `INVALID_PAYLOAD` (`state/checkpoints.py:53-58`), none
of which a config edit moves. D48 declines to fix this itself because the obvious move — call
`drifted_sections` from `_phase_preflight` — would make every phase verb refuse on drift an
operator already accepted on a prior verb, and the `--accept-drift`/per-section audit trail is
keyed to a command (`resume`) that does not run.

### 2. The precedent, re-verified in the main session

`references/open-swe/agent/middleware/prepare_run.py` (pinned SHA per ADR-0071 §1) —
`BasePrepareRunMiddleware.abefore_agent` (`:54-67`) computes `_prepare_fingerprint` (`:69-76`) on
every before-agent hook and compares it to the latched `run_prepared_for` in state (`:61-64`). On a
match it returns `None` — setup is skipped. **On a mismatch it does not refuse anything: it just
calls `_prepare` again** (`:66-67`) and re-latches the new fingerprint. The concrete fingerprint
(`agent/server.py:906-914`, `_prepare_config_fingerprint`) folds in `{prepare_run_id, thread_id,
source, repo, plan_mode, draft_prs, model, effort}` alongside the latest message. Changing the
model on a resumed thread re-prepares rather than reusing stale setup.

**The mechanism is exactly the shape that resolves D48's own objection**: a fingerprint mismatch
*invalidates and redoes*, it never *refuses and waits for an operator to accept*. Re-derive, don't
refuse.

**The recorded gap, confirmed by re-reading rather than trusted from ADR-0071 §3.1's summary**:
`_prepare_config_fingerprint` has no field for the rendered system prompt or its template version —
`PrepareRunState.rendered_system_prompt` (`prepare_run.py:22`) rides through unchanged whenever the
rest of the fingerprint matches. open-swe solved the model half of this problem and left our exact
gap — the prompt half — open. This ADR does not repeat that omission (§4).

### 3. Decision — what a phase verb does on drift: re-derive, via the mechanism the checkpoint
   layer already has

On a fingerprint mismatch, the affected `(run_id, repo_id, phase)` checkpoint is **discarded and
the phase re-runs whole from `phases.base_ref`** — no refusal, no exit code, no CLI flag, no
operator gesture. Concretely: `_re_entry` (`runner.py:706-745`) already has exactly this verdict
wired end to end for a different mismatch (the worker's own `preconditions_hold` finding the tree
does not match the checkpoint) — `ReEntry.REJECTED` (`runner.py:235-239`) logs a warning
(`checkpoint_rejected`, `runner.py:736-743`), the driver sets `checkpoint = None`
(`runner.py:470-473`), and the phase dispatches whole rather than resuming
`remaining_units`. **This ADR routes a config-fingerprint mismatch into that same verdict**, checked
first — before the (filesystem-walking) call to `preconditions_hold`, so a checkpoint that is
already known stale never pays for a tree-state check it cannot use the answer to.

**Rationale.** D48's blocking objection to the obvious fix is granularity: a `_phase_preflight`
refusal is per-*command*, so one drifted section refuses an entire `fleet build` invocation across
however many repos it touches, including repos whose checkpoints do not reference the drifted
section at all, and forces the operator back through `--accept-drift`/`--force-config-drift` for a
command (`resume`) that cannot act on the acceptance anyway. The checkpoint load path is already
per-`(repo, phase)` and already re-derives silently and safely when it decides a checkpoint is
unusable — extending its existing predicate is strictly smaller than building a second gate at a
coarser altitude, and it produces no new refusal for an operator to route around. Re-deriving also
costs no more than what already happens on a `RESUME`-then-actually-different-tree case today: the
worker re-runs from its anchor with the same ladder/budget machinery that governs every other
attempt.

### 4. What goes into the fingerprint

**Decision.** `PhaseCheckpoint` (`runner.py:152-166`) gains a `config_fingerprint: str` field,
computed as `sha256(settings.config_sha256() | prompt_digest)` where:

- `settings.config_sha256()` (`settings.py:1207-1210`) is the existing whole-config digest —
  `_digest(dict(section_digests))` over all 14 `CONFIG_SECTIONS` plus the 15th `models_profile`
  section (`settings.py:1503-1521`), which folds in `models.roles` and
  `models.profiles[profile]` — i.e. model id, backend, and effort per role.
- `prompt_digest` is a new, small digest over every role's `prompt_template_version(role)`
  (`llm/calls.py:292-294`, sourced from the hand-maintained `PROMPTS` mapping,
  `llm/calls.py:100`) — `sha256` over `sorted((role.value, prompt_template_version(role)) for role
  in Role)`.

**Rationale.** `config_sha256()` is already the digest the §10 drift design stores per run
(`runs.config_digests`/`config_sha256`, written in `_open_run`, `cli.py:1885-1911`, and by
`upsert_run`, `state/repository.py:1003-1010`) — reusing it means the checkpoint fingerprint and
the run-level drift baseline can never disagree about what "the config" was, for the same reason
`config_sha256`'s own docstring gives for computing itself as a hash of the section digests rather
than a second independent hash. But `config_sha256()` cannot see `prompt_template_version`: prompt
versions are hand-maintained integers in `llm/calls.py`, not `FleetConfig` fields, so no
config-section digest moves when a template changes. **This is precisely the disagreement D48
documents** — `llm/cache.py:10-12,124-129`'s cache key already includes `prompt_template_version`
and correctly misses on a template edit, while the checkpoint above it has no configuration
component at all and hands back the stale answer. Folding the prompt digest into
`config_fingerprint` is what makes the checkpoint layer as discriminating as the cache layer
beneath it — the fix ADR-0071 §3.1 already named but did not itself build.

Hashing **all twelve roles'** versions, rather than mapping each phase to the specific role(s) its
workers call and scoping narrowly, is a deliberate `Agent Recommendation` for over-invalidation: a
phase that calls no LLM role at all pays nothing extra (its checkpoint's fingerprint still matches
unless the config half moved), and getting a narrow phase→role mapping wrong in the
under-inclusive direction reproduces exactly the silent-stale-reuse defect this ADR exists to
close. The cost of the wide version is bounded — at most one avoidable re-run of a phase whose only
changed role is unrelated to it — and that bound needs no measurement to state (Guardrail 6): it is
a worst-case property of the construction, not a claimed number.

**Explicitly not folded in**: the remaining `llm_cache.cache_key` components — `role`, `tier`,
`backend`, `effort`, `context_policy`, `rejected_approach_digest`, `prompt_sha256`,
`response_schema_sha256`, `adapter_versions` (`llm/cache.py:10-12,124-129`). `backend`/`model_id`/
`effort` already live inside `models_profile`'s digest; the rest are properties of one *rendered*
call (a specific prompt render, a specific rejected-approach set, a specific response schema
instance), not of the run's configuration, and are exactly what `llm_cache`'s own key exists to
catch. Duplicating them at the checkpoint layer would be `llm_cache` reimplemented one layer up,
against Rule 2.

Also explicitly not folded in: `harness_version`. `llm/cache.py:26-30` already rejects it from the
cache key for the identical reason that applies here — it changes on every patch release, and
including it would invalidate every in-flight checkpoint fleet-wide for a change no model output
can see.

### 5. Where enforced: the checkpoint load path, not `_phase_preflight`

**Decision.** The comparison lives in `runner.py`'s `_re_entry` (`runner.py:706-745`), not in
`_phase_preflight` (`cli.py:868-880`) and not as a new refusal in `_resume_impl`.

**Rationale.** `_phase_preflight` runs once per CLI invocation, before any repo is dispatched, and
has no per-repo checkpoint in view — it is the wrong altitude for a verdict that must be independent
per `(repo, phase)`. `_re_entry` already sits exactly where the decision has to be made: it is
called once per dispatch, after the checkpoint is loaded (`runner.py:458`) and before
`preconditions_hold` is asked, and its three-way return (`FRESH`/`RESUME`/`COMPLETE`, now joined by
the existing `REJECTED`) is already threaded through `_drive` to discard the checkpoint and
re-dispatch whole (`runner.py:469-473`). `WorkerOutput.checkpoint_is_current`
(`workers/base.py:256-259`) — itself a **D48-documented dead method, zero callers in `src/`** — is
the right *shape* of check (equality, not `>=`, on a persisted marker) but the wrong *axis*: it
compares `written_schema_version`, not configuration, so this ADR does not repurpose it. The new
comparison is a sibling check on `PhaseCheckpoint.config_fingerprint`, called from `_re_entry`
alongside, and before, the existing `preconditions_hold` call.

### 6. The accept-drift audit trail

**Decision.** `_resume_impl`'s `--accept-drift`/`--force-config-drift` flow and the `ConfigDrift`
findings it writes (`cli.py:9814-9930`, `_record_drift_findings` at `:9890-9930`) are **left
exactly as they are**. This ADR does not touch, repurpose, or deprecate them. They remain reachable
only through `fleet resume`, which still terminates at `_unavailable` (`cli.py:9811`) immediately
after they run — a separate, pre-existing inconsistency (the command writes real rows via
`_record_drift_findings`/`_raise_wave_ceiling` and then reports itself unavailable) that this ADR
does not fix and is not in scope for it.

*Agent Recommendation, not adopted as a requirement here*: when `_re_entry` returns `REJECTED` for
a `config_fingerprint` mismatch specifically (as opposed to a `preconditions_hold` tree-state
mismatch), it should write its own audited finding — e.g. a `ConfigDriftCheckpointInvalidated` row,
shaped like `_record_drift_findings`'s existing `ConfigDrift` finding (`cli.py:9890-9920`) but
fired automatically per-repo at invalidation time rather than gated behind a flag on a command
nobody can reach. This needs its own schema field, its own migration, and its own test under Rule
9; it is a recommendation for whoever implements this ADR, not a decision this entry makes.

### 7. Explicitly out of scope

- No `src/` change accompanies this ADR (CLAUDE.md constraint; Guardrail 6).
- Whether or how `fleet resume` itself gets wired up is not decided here — it stays `_unavailable`.
- The two documentation defects D48 records alongside the main finding — `workers/base.py:237-244`'s
  inaccurate `checkpoints.load` docstring, and `_open_run`'s unconditional `config_digests` rewrite
  (`cli.py:1907-1910`) racing `upsert_run`'s `ON CONFLICT DO NOTHING` on `config_sha256`
  (`state/repository.py:1003-1010`) — are separate OPEN findings with their own fixes, not addressed
  here.
- The exact `checkpoints.SCHEMA_VERSION` migration needed to add `config_fingerprint` to
  `PhaseCheckpoint` is implementation, not decision; it is not designed here beyond noting a bump is
  required (`state/checkpoints.py:17-19`'s envelope already treats a version bump as the intended
  invalidation lever).
- Whether a config-fingerprint mismatch should also be checked earlier — before the phase lease is
  acquired (`runner.py:444-457`) — as a scheduling optimization is not decided: that is a
  performance question with no measurement behind it (Guardrail 6), not a correctness one.
- Extending fingerprint checking to phases that never checkpoint (`clone`, `interrogate`,
  `symbolindex` — D48's `ReEntry.FRESH`-only workers) is out of scope: a phase with no checkpoint has
  nothing for a fingerprint to invalidate.

### Alternatives rejected

- **Call `settings.drifted_sections` from `_phase_preflight` (D48's own rejected default).**
  Rejected for the reason D48 already gives: it refuses every phase verb on drift an operator
  already accepted through a different, unreachable command, at the granularity of a whole CLI
  invocation rather than one checkpoint.
- **A whole-run refuse-with-`--force`, mirrored from `_resume_impl`, added independently to each
  phase verb.** Rejected: this only relocates the refusal, it does not remove it — CLAUDE.md Rule 2
  asks for the minimum mechanism, not a second refusal ladder standing next to the first.
- **Warn-only: log that the config drifted and serve the stale checkpoint anyway.** Rejected: this
  is, in substance, what happens today with no name attached to it — D48's own accounting is that
  the failure "costs the ladder" (rung position preserved, spend charged, rejected approaches
  carried forward under a configuration that no longer exists, `runner.py:631`). A warning nobody
  acts on is not a different outcome from no warning.
- **Port open-swe's per-message fingerprint model wholesale — fingerprint every model turn, not
  every checkpoint.** Rejected: open-swe's unit of re-preparation is a conversational turn inside
  one LangGraph thread (`_latest_message_fingerprint`, `prepare_run.py:25-38`); this harness's unit
  is a `(run_id, repo_id, phase)` checkpoint with no analogous per-turn structure. The shape
  transfers — invalidate-and-redo, not refuse — the granularity does not.
- **Reuse `WorkerOutput.checkpoint_is_current` for the config check instead of adding a sibling
  comparison.** Rejected: `checkpoint_is_current` compares `written_schema_version` against a
  `ClassVar` bumped by hand on field renames (`workers/base.py:234-259`) — a shape-versioning axis,
  not a configuration axis. Overloading one field to mean two different kinds of staleness is how
  the next reader loses the ability to tell "the model changed" from "the class changed."

---

## ADR-0073 — A status claim without the SHA it was measured against is not a claim, it is a guess with a timestamp: every ledger status, review severity, and ADR status line names the commit it was last true at, and a status *flip* is the one event that must not skip re-derivation

**Status: DECIDED, NOT YET IMPLEMENTED (documentation discipline).** No `src/` change accompanies
this entry, per the task that produced it and per Guardrail 6 — a discipline is decided here, not
wired. Verified against `983c725` (the tree at authoring time); every citation below was re-run in
this session rather than carried over from the round that reported it, because two of the nine
numbers handed to this entry were themselves wrong (§1.9, §1.10) — a fact this ADR treats as
corroborating evidence for its own thesis, not an embarrassment to smooth over.

### 1. Nine records, re-derived

A single round of documentation work in this project produced **nine stale citations against zero
missed defects** — every case below was re-measured directly against `git show`/`git diff`/`git
log -S` in this session, not transcribed from the round that reported it.

**1.1 — D34, `docs/INTEGRATION_HONESTY.md:1896`.** The entry's bold lead sentence still reads
**"D34 — OPEN."** The paragraph immediately beneath it (added by a later pass) reads **"Status —
CLOSED, FIXED in `44d5550`."**, re-verified there against `82654e8`:
`classify_build_failure` (`buildverify.py:412-471`) tests `result.exit_code ==
_DOCKER_CANNOT_RUN` before either exit-code set and returns `TRANSIENT_INFRA, True` — the missing
`125` row the entry named, closed, pinned by
`test_a_docker_run_that_exits_125_is_not_reported_as_a_broken_build_file`
(`tests/test_workers_build.py:1333`). **The header and the body of the same entry now disagree**,
and a reader who stops at the bold lead — which is the entry's entire job, per this document's own
header convention (name, then OPEN/CLOSED) — reads the wrong verdict.

**1.2, 1.3, 1.4 — D35, D36, D41, `docs/INTEGRATION_HONESTY.md:1924, 1954, 2042`.** Each names a
concrete defect in `src/fleet/workers/clone.py`. Independently re-derived in this session, not
trusted from the ledger's own correction text: `git show a1178f7:src/fleet/workers/clone.py`
already shows, at the repository's first commit — `_preflight` (`:410-421`) calling
`self._resolve_head(git, branch)`, a private method, **never** `Git.resolve` (the ambiguous-`None`
method D36 blames); `_submodule_count`, `_has_lfs`, and `_largest_blob_bytes`
(`a1178f7:568-630`) each call `_no_verdict(result)` first and `raise _indeterminate(...)` when the
probe is unsettled, falling to `0`/`False` only on a settled, genuinely-negative result — the exact
honest shape D41 itself prescribes as the fix. `git diff a1178f7 HEAD --
src/fleet/workers/clone.py` shows 136 changed lines total across the file's whole visible history,
none of which touch whether these functions raise or return a gate. **None of the three is
reproducible anywhere in visible history.** Caveat that must survive any future citation of this
entry: `a1178f7` is a squash of unrecorded earlier checkpoints (per its own commit message and
`d5a0d07`'s follow-on), so the defects may have existed pre-squash — nothing checkable in `git log`
shows that they did. "Never reproducible in visible history" is the honest claim; "never existed"
is not one this session can make.

**1.5 — D45, `docs/INTEGRATION_HONESTY.md:2137`.** Claims `tests/test_vcs.py`'s `ScriptedRunner`
has no `timed_out` parameter and hard-codes `timed_out=False`. Re-derived directly:
`git show a1178f7:tests/test_vcs.py` shows `ScriptedRunner.__init__` accepting `timed_out: bool =
False` as a real constructor argument and `__call__` forwarding it verbatim
(`timed_out=self.timed_out`) into the returned `ProcResult` — never hard-coded. The mechanism claim
does not reproduce at any point in visible history. What *was* real and separate: at `32365cf`, no
test actually constructed a `timed_out=True` `ScriptedRunner` against any of the four `vcs/` probe
methods — the fake could express the state, nothing exercised it. That coverage gap is now closed
by `d37f4ba`, which added two parametrized tests across all four probes.

**1.6, 1.7 — C1, C2, `docs/superpowers/plans/review-36.md:30, 63`.** C1 flags
`buildverify.py`'s `_DOCKER_CANNOT_RUN_EXPLAINED` string ending "no attempt charged, no repair
prompted" as false past `retry.py`'s four-retry cap. C2 flags the identical claim promoted into
`clock_failure`'s canonical docstring in `base.py`. Both re-verified absent from the current file
and confirmed via `git log -S`: `git log -S"no attempt charged, no repair prompted" --oneline --
src/fleet/workers/buildverify.py` returns exactly `44d5550` (introduced the string) and `8464dc6`
(removed it — `grep` against the working tree today returns no hit); `git show 8464dc6 --stat`
confirms `src/fleet/workers/base.py` and `src/fleet/workers/buildverify.py` both changed in that
same commit. `base.py:168` today reads *"'no attempt charged' is a bounded reprieve, not a
standing exemption"* — the corrected sentence review-36 asked for, in the exact location review-36
named. **Both fixed, in the single commit `8464dc6`.**

**1.8 — the mechanism, `44d5550`'s commit message.** The message, under "Also in this checkpoint
(earlier agents, separately verified)", claims: *"clone worker D35/D36/D41 — transient failures no
longer permanent, a rev-parse that never ran no longer means EmptyRepo reported as success, and
three probes no longer publish fabricated zeros."* `git show 44d5550 --stat` lists exactly four
changed files — `docs/DECISIONS.md`, `docs/PROGRESS.md`, `src/fleet/workers/buildverify.py`,
`tests/test_workers_build.py` — **zero** touching `src/fleet/workers/clone.py` or
`tests/test_vcs.py`. Re-verified directly in this session, not carried over: the diff is exactly
those four paths, no more, no fewer. A commit asserted work its own diff never contains, and — per
§1.2–1.4 — the work it claimed to be reporting on had never been broken to begin with, so there was
nothing for the claimed-but-absent commit to have fixed even if it had landed.

**1.9 — D48, `docs/INTEGRATION_HONESTY.md:2371-2378`.** Cited `_open_run`'s `runs.config_digests`
UPDATE at `cli.py:1902-1906`. Re-verified: the entry's own later correction (same paragraph)
states the true position at the commit the caveat pins the entry to (`8464dc6`) was in fact
`:1902-1906` — accurate *there* — and that **`32365cf` and `e605f0d`**, both landing after
`8464dc6`, each touched `cli.py` and together shifted the same code down five lines to
**`:1907-1910`**, the position confirmed against a frozen `git show 2a72f9f:src/fleet/cli.py`.
Both numbers were correct at some commit; neither commit was named at the time either was written
down, so the citation read as simply wrong rather than as "correct as of an unstated SHA, since
moved."

**1.10 — D50, `docs/INTEGRATION_HONESTY.md:2598-2599`.** Cited five `RunContext(` call sites in
`cli.py` at `:1805, :4059, :7481, :7553, :9100`. Re-verified against a frozen `git show
82654e8:src/fleet/cli.py | grep -n 'RunContext('`, which returns `1807, 4085, 7527, 7599, 9146` —
**all five off**, by amounts ranging from 2 to 46 lines. The same paragraph records a second,
independent re-measurement (`1806, 4074, 7496, 7568, 9115`) that also fails to match the frozen
snapshot. Three attempts at five numbers, no attempt agreeing with either of the other two or with
the pinned SHA — while the substantive claim underneath them (`llm_policy=` is passed at none of
the five sites; `grep -rn llm_policy= src/` returns zero hits regardless of which line numbers are
current) held at every measurement. The citation rotted; the finding did not.

**Net count: zero cases, across these nine, of a record missing a real defect.** Every stale
record overstates remaining work or misplaces true evidence; none understates it. §3 treats that
asymmetry as a finding in its own right, not a coincidence of this particular round.

### 2. Decision — the rule, and what "status claim" means

**A status claim is any sentence in `docs/` asserting a fact about the current state of code or a
prior record that a reader could falsify by looking at the tree** — an `OPEN`/`CLOSED`/`FIXED`/
`NEVER REPRODUCIBLE` tag on a ledger entry; a Critical/Important/Minor severity assignment on a
review finding; an ADR's own top-line `Status:` declaration (`DECIDED`, `IMPLEMENTED`, `NOT YET
IMPLEMENTED`); and any file:line citation offered as the evidence for one of the above. A sentence
describing what code *should* do, or what this project has decided to do, is not a status claim and
is out of scope — only a sentence claiming what the tree currently *does* is.

**Decision.** Every status claim in `docs/` names the commit SHA it was last verified true against,
inline, adjacent to the claim — not in a separate changelog, not implied by the file's own last-
edited date. A bare line-number citation with no SHA is not wrong on arrival, but it is
**unverifiable the moment the cited file next changes**, and per §1.9/§1.10 above this project's own
files move under concurrent multi-agent edits on the scale of hours, not weeks. `docs/
INTEGRATION_HONESTY.md`'s D50 correction states the working conclusion better than this ADR could
restate it: *"a bare line number into a file under active, concurrent repair is stale by the time
it is read, however carefully it was measured; only a SHA-pinned citation stays checkable."* This
ADR adopts that sentence as the rule rather than re-deriving a different one, because §1.9 and
§1.10 are this session's own independent confirmation that it is correct, not a borrowed claim
taken on faith.

**This is not a new practice invented here — it is naming one this round already used.** Six of the
nine corrections above (D34, D35/D36/D41, D45, D50) already carry exactly this shape in the ledger
today: *"Re-verified at `82654e8`"*, *"Correction — count and citations, both re-derived;
`2a72f9f`/`82654e8`"*, *"the same squash caveat that applies to D35/D36/D41 applies here."* The
pattern emerged organically, under pressure, without being written down as a rule — which is the
strongest evidence available that it is cheap enough to actually happen (§4), and the reason this
ADR's decision is to *codify* a convention already load-bearing in this document rather than to
propose an untested one.

### 3. Why overstated debt is not harmless

**The claim to argue against:** a stale-OPEN entry looks like the safe failure mode — worst case, an
agent double-checks something that turned out fine, wastes a little time, and moves on. §1's own
count (zero missed defects, nine overstated) could be read as evidence the ledger is erring on the
side of caution. **That reading is wrong, and the reasons are specific, not general nervousness
about "bad docs."**

**First — the search cost is not small and it is not bounded.** A future agent handed D35 does not
read one paragraph and stop; per this project's own Rule 8 ("read before writing... inspect
existing exports"), closing a ledger entry means reading the named function, its callers, its
tests, and usually its neighbors, before writing a fix for a bug that is not there. `44d5550`'s
commit message (§1.8) shows the mechanism by which this compounds: a false "fixed" claim about
code that was never broken becomes a false "still needs fixing" entry two commits later, each
believing the other. Left uncorrected, D35/D36/D41 were three live invitations to repeat that work
a third time.

**Second — phantom entries contaminate downstream design, not just downstream time.** `D48` is cited
in `ADR-0071`'s own headline (*"the prepare-run fingerprint (the wired drift gate D48 says we
lack)"*) and its citation drift (§1.9) is comparatively benign — the *substance* of D48 held under
re-measurement, only its line numbers moved. A stale-OPEN entry whose *substance* had already been
fixed, cited the same way by a design ADR, would not be benign: it would motivate real architecture
work — new fields, new call sites, new tests — against a gap that closed commits ago. This project's
ADRs are not read in isolation; §3.1 of ADR-0071 leans on D48 exactly as load-bearing evidence for
a lifted mechanism. A phantom entry cited as a premise does not just cost the reader who checks it —
it costs everyone who trusts the ADR built on top of it and never checks.

**Third, and the sharp one: overstated debt trains readers to discount the ledger, which is the
one thing `docs/INTEGRATION_HONESTY.md` cannot survive losing.** This document's entire reason to
exist — stated in its own header and reaffirmed at D34–D45's own section close — is that it is the
place a reader goes to learn what is actually wrong without re-deriving it from scratch. Every
entry that turns out to be phantom is evidence, accumulated one incident at a time, that the
document's `OPEN` tag does not mean what it says. A reader who has twice found `OPEN` to mean
"actually fixed two checkpoints ago" starts re-verifying `OPEN` entries before acting on them — at
which point the ledger has stopped saving anyone time, which was its only value proposition. A
missed defect degrades one incident; a pattern of overstated ones degrades the instrument itself.

**Committing to a view: overstated debt is worse than underreported debt in this specific project,
for a reason that is structural rather than a matter of taste.** This project's own established
thesis — the D34–D45 section header states it directly — is *"only running things finds defects."*
A real, underreported bug is not invisible forever: the next build, the next `pytest` run, the next
`bazel` verdict surfaces it mechanically, because CLAUDE.md §6's build-and-test discipline does not
depend on the ledger being complete to notice a broken build. A phantom `OPEN` entry has **no
equivalent mechanical corrective** — nothing runs to disprove a claim that the code is broken; only
a reader choosing to re-verify it does, and §1 shows that choice is not being made reliably. The
asymmetry `docs/INTEGRATION_HONESTY.md`'s own D34–D45 section closes with — *"a ledger that
accumulates phantom debt misleads a future reader differently than one that misses a real defect,
and costs exactly as much of this document's own credibility either way"* — is half right and this
ADR revises the other half: the *credibility* cost may be symmetric, but the *correction* cost is
not. A missed real defect is self-correcting by the mechanism this project already runs continuously.
A phantom entry is corrected only by the same manual, effortful re-reading that produced this ADR's
nine cases — and nothing about this project's existing tooling makes that correction happen on its
own.

### 4. Verification cadence — cheap enough to happen, and the ADR-0071 §4 mirror

**Decision.** Re-derivation is required at exactly two moments, both of which are points where an
agent is already reading and writing the entry — never as a standing sweep:

1. **On authoring** — any new status claim is written with the SHA it was checked against, using
   the `git show <sha>:<path> | grep -n ...` / `git diff <sha> HEAD -- <path>` pattern §1 used
   throughout, because that pattern is what stays checkable after the file moves (§2).
2. **On status flip** — `OPEN → CLOSED`, `CLOSED → OPEN`, or any severity change, is the one
   event that must not skip re-derivation. A flip is a claim that something changed; the only way
   to back that claim is to have looked at both states, which means the SHA before and the SHA
   after are both already in hand at the moment of writing the flip. This is not new overhead: §1.1
   (D34), §1.6–1.7 (C1/C2), and §1.5 (D45's coverage half) show flips already carrying exactly this
   evidence in the current document, because the workers who flipped them had no cheaper way to
   justify the flip than to show the before/after diff they had already produced to convince
   themselves.

**Explicitly not decided here: a periodic re-verification sweep of entries that have not flipped.**
Rejected in §5. Cadence is bound to the moment an entry is already being touched, not to a calendar
or a count of elapsed commits, because the former is free (the SHA is already in the terminal
scrollback) and the latter is a chore with no owner.

**The mirror worth naming plainly, per this task's own prompt: this project already has one
unwired control, and this rule risks becoming a second one if stated without the qualification
above.** ADR-0071 §4 records, of a different codebase, *"code that exists, is tested, is
documented — and is wired into nothing, while operators are told to grant real permissions on its
basis"* and closes with a heuristic this project adopted for its own reviews: *"verify the control
is wired, not merely present."* A documentation rule with no mechanical gate is not code, so the
letter of §4's finding does not transfer — but the failure shape does: a rule that exists only in
this ADR's prose, that nobody is ever forced to run, is indistinguishable from a rule that was never
decided, the first time someone is in a hurry. This ADR's answer is **not** to invent an enforcement
mechanism this project does not have (§5 declines exactly that move) — it is to bind the rule to
work that is already happening for other reasons (§1's own six organic instances), on the theory
that a rule riding on existing motion survives being ignored once in a way a rule requiring new
motion does not. Whether that theory holds is not verifiable today; it is recorded as the
**Agent Recommendation** it is, not as a proven mechanism.

### 5. The commit-message case — a review discipline, not a mechanical fix

`44d5550` (§1.8) is a different species of defect from a stale ledger entry: a commit whose message
claims work its own diff does not contain. A stale entry rots after the fact, as code moves past
it; `44d5550`'s message was false the instant it was written, against the very tree it was
committed onto.

**Decision: nothing mechanical closes this, and this ADR does not invent something to.** CLAUDE.md
names `git status` and `git diff` as the tools available in this workspace; it names no CI system,
no commit-msg hook, no pre-push gate anywhere in this project, and this task's own constraints
forbid inventing one. A commit-message claim is, structurally, prose written by whoever is
committing, checked by nobody but a reader of `git log` after the fact — there is no gate between
"the message is typed" and "the commit exists" for this ADR to install itself into without adding
infrastructure CLAUDE.md does not authorize.

**What this ADR does decide: the self-check that would have caught it costs one command and zero
new infrastructure.** `git show 44d5550 --stat` — the exact command §1.8 used to falsify the claim
— takes as long to run before a commit as after one. The discipline this ADR asks for is that the
same command that falsified `44d5550` two checkpoints later is run by the author, against their own
staged diff, before the commit message is written — comparing the files the message is about to
claim against the files `git diff --cached --stat` actually shows. This is squarely a review
discipline under CLAUDE.md's existing terms (the "Committing changes with git" protocol already
directs reviewing `git status` output before staging), not a new mechanism: it asks that the review
happen in the fifteen seconds before the commit lands rather than in the multi-checkpoint audit that
eventually found it. Whether it happens is not something this ADR can force — that is precisely
what makes it a discipline and not a gate — and this ADR states that honestly rather than dressing
a hope up as a control (the exact confusion §4's mirror warns against).

### 6. Alternatives rejected

- **A periodic full-ledger re-verification pass ("re-check everything every N checkpoints").**
  Rejected: this project's own checkpoints (`44d5550` through `983c725`, per `git log --oneline`)
  already run every few hours under multiple concurrent agents; a sweep with no natural trigger
  competes with real work for the same budget CLAUDE.md Rule 6 asks to be conserved, and — per
  §4's ADR-0071 §4 mirror — a rule with no owner and no trigger is the shape most likely to be
  documented and never run.
- **Require every status claim to be re-verified on every read, not only on flip or authoring.**
  Rejected: this makes reading the ledger as expensive as writing it, defeating the ledger's own
  purpose (§3) of letting a reader avoid re-deriving what is already known; it also has no
  plausible enforcement given this project's tooling, making it a second unwired control rather
  than zero.
- **A CI check that diffs a commit's message against its `--stat` output and fails on a mismatched
  defect ID.** Rejected under this task's explicit constraint against inventing infrastructure this
  project does not have, and under Rule 2 (simplicity first) — this project has no CI pipeline to
  attach it to, and building one to catch nine incidents in one round is the premature-abstraction
  failure Rule 2 names directly.
- **Blame-and-freeze: require sign-off from a second agent before any status claim can flip.**
  Rejected: it doubles the cost of every legitimate flip to guard against a failure mode (§1) that
  a one-line SHA citation already makes self-auditing without a second reader, and this project's
  existing multi-agent rounds (per `983c725`'s own commit message, "Ten agents, disjoint lanes, one
  serialized verification") already pay for a form of cross-checking that a sign-off gate would
  duplicate rather than add to.
- **Treat citation drift (D48, D50) as beneath ADR-level attention — it is "only" line numbers.**
  Rejected: §1.9 and §1.10 show citation drift is not cosmetic — a citation is the only thing that
  makes a claim falsifiable at all, and an un-anchored one fails silently exactly when a reader
  most needs it, which is precisely why this ADR's rule (§2) treats a citation as part of the status
  claim rather than as decoration on it.

## ADR-0074 — The agent-worktree/branch correspondence is enforced by a `pre-commit` hook, not a brief: `.githooks/pre-commit` refuses a commit whose branch and checkout side disagree, installed via `core.hooksPath` set to an **absolute** path into the primary because a relative one silently reduces to no enforcement at all

**Status: DECIDED AND IMPLEMENTED.** Verified against landed code and by direct execution in this
session (commands and output below, and in
`docs/superpowers/plans/task-HOOK1-report.md`). This closes the round CLAUDE.md's own brief
for this task opens with: five concurrent subagents sharing one primary checkout, told by prompt
alone to keep to their lanes, produced **four separate incidents of one agent's staged work landing
in another agent's commit** and one case of hand-built `git apply --cached` surgery to undo it.
Guardrail 1 forbids a subagent from citing a brief as a control; this ADR is the record that turns
"stay in your lane" from a sentence in a prompt into something git itself refuses to violate on one
specific, narrow axis — the branch/checkout correspondence, not the broader lane discipline, which
§4 states plainly this mechanism does not and cannot cover.

### 1. The rule

- A **linked worktree** is a subagent's lane. A commit made there must land on a branch matching
  `agent/*` — never `main`, never any other name, never detached.
- The **primary checkout** (`/home/redmage/swe repo harness`) is the orchestrator's. A commit made
  there must **not** land on an `agent/*` branch. `main` — or any other non-`agent/*` branch — is
  unrestricted there; that is where the orchestrator's own work, including its constant commits to
  `main`, belongs.

Git already refuses to check out the same branch in two worktrees at once, so a worktree
necessarily has *some* branch of its own — but nothing before this ADR stopped an agent creating a
differently-named branch inside its worktree, or committing directly in the primary checkout
instead. Both are exactly what happened in the round that motivated this task.

### 2. Detection: `--absolute-git-dir` vs. `--git-common-dir`, verified rather than assumed

The hook (`.githooks/pre-commit`) tells a linked worktree from the primary by comparing
`git rev-parse --absolute-git-dir` to `git rev-parse --git-common-dir` (the latter resolved to an
absolute path by `cd`-ing into it, since it can print a path relative to the working tree's top
level — e.g. plain `.git` in the primary). **In the primary these are identical; in a linked
worktree they differ**, because a linked worktree gets its own per-worktree administrative
directory (`<common-dir>/worktrees/<name>/`) while sharing the common dir — refs, config, and (via
`core.hooksPath`, §3) this very hook — with the primary. Measured directly in this session:

```
# primary:
$ git rev-parse --absolute-git-dir
/home/redmage/swe repo harness/.git
$ git rev-parse --git-common-dir
.git                                    # resolves to the same path, cd'd from the worktree top

# linked worktree (worktrees/wt-WT1-example):
$ git rev-parse --absolute-git-dir
/home/redmage/swe repo harness/.git/worktrees/wt-WT1-example
$ git rev-parse --git-common-dir
/home/redmage/swe repo harness/.git     # different from the above
```

This is not a new pattern invented for this ADR: `tools/worktree/new-worktree.sh` already uses the
identical comparison (`git_dir` vs. `common_dir`, both resolved to absolute paths) to refuse running
itself from a worktree instead of the primary. The hook reuses it for the opposite classification.

**Hooks firing in a linked worktree at all was verified, not assumed.** Before writing anything, a
throwaway `#!/bin/sh … exit 1` was dropped in the primary's (untracked) `.git/hooks/pre-commit` and
a commit attempted from the linked worktree — it fired (`HOOK FIRED in
/home/redmage/swe repo harness worktrees/wt-WT1-example`, exit 1). This confirms git's documented
behavior: hooks are read from the **common** dir by default, not the per-worktree admin dir, so a
linked worktree was never going to be silently exempt.

### 3. Installation: `core.hooksPath`, and why it is an absolute path into the primary

`.git/hooks/` is never committed — it is exactly the untracked, per-clone directory the throwaway
probe in §2 lived in, which is unusable as a distributable control. `core.hooksPath`
(`tools/worktree/install-hooks.sh`) points git at `.githooks/` instead, which **is** tracked.
`core.hooksPath` lives in the shared `.git/config`, so setting it once from the primary was verified
to apply to an *already-existing* linked worktree immediately, no per-worktree step required:

```
$ git config core.hooksPath   # (run from the primary, after install-hooks.sh)
/home/redmage/swe repo harness/.githooks
$ cd "worktrees/wt-WT1-example" && git config core.hooksPath
/home/redmage/swe repo harness/.githooks    # same value, same file, no separate config
```

**The path is absolute, anchored to the primary checkout, not the repo-relative `.githooks` that
would be the more obvious choice.** The reason is a second thing verified empirically rather than
assumed, and it is the sharpest finding in this task: **when `core.hooksPath` names a directory
that does not exist, git skips `pre-commit` entirely — no error, no message, exit 0, commit
succeeds.**

```
$ git config core.hooksPath /does/not/exist
$ echo probe > f && git add f && git commit -m x
[main bdfb15c] x
 1 file changed, 1 insertion(+)
 create mode 100644 f
$ echo $?
0
```

A repo-relative `.githooks` is resolved against whatever the **current worktree's own branch** has
checked out at that path. An agent worktree branched before this hook merged into its base ref — or
one that simply never picks up a later docs/tooling commit, which is the normal case for a
short-lived task branch — would have no `.githooks/pre-commit` file on disk at all, and by the
measurement above that is not a missing-hook error, it is **silent, total, unannounced loss of
enforcement**, indistinguishable from a checkout where this ADR was never implemented. Pointing
`core.hooksPath` at an absolute path into the primary sidesteps that: the file that runs is always
the primary's copy of `.githooks/pre-commit`, regardless of which commit any given worktree's
branch happens to have checked out. The tradeoff, stated plainly: if the primary checkout is ever
moved or renamed, `core.hooksPath` goes stale and `install-hooks.sh` must be re-run — a rarer,
louder failure (every commit everywhere silently ungated, easy to notice fast) than a per-branch gap
that only ever affects whichever worktrees happen to predate a merge.

`core.hooksPath` is **local** config — `.git/config` is never committed — so a fresh clone, or a
primary checkout moved to a new path, has **zero** enforcement until `tools/worktree/install-hooks.sh`
is run in it. `install-hooks.sh` prints this loudly on every run rather than leaving it to be
discovered the hard way; this ADR states it again here so it does not depend on the script's stdout
being read.

### 4. Fail-closed, and the escape hatch

Every git command the hook depends on (`--absolute-git-dir`, `--git-common-dir`, `git branch
--show-current`) is wrapped so that a command failure **refuses the commit**, printing the raw
condition and pointing at the override, rather than falling through to "allow" because the
classification could not be completed. A hook that goes silent on its own edge cases is worse than
no hook — it teaches an operator that the absence of a refusal means the commit is safe, which
§3 already shows is false in at least one case (a missing `hooksPath` directory) that this hook
cannot itself detect, because git never runs it.

**The orchestrator override is `HARNESS_ALLOW_BRANCH_OVERRIDE=1`, prefixed on the commit itself**
(`HARNESS_ALLOW_BRANCH_OVERRIDE=1 git commit …`), not a shell-exported variable and not `FLEET_*` —
`settings.py` pairs `env_prefix="FLEET_"` with `extra="forbid"`, so one stray `FLEET_*` variable
left in an environment makes every settings load exit 2 (CLAUDE.md; `tools/worktree/new-worktree.sh`
carries the identical constraint). Setting it skips the **entire** guard, in both directions, and
prints a visible line to stderr (`pre-commit: HARNESS_ALLOW_BRANCH_OVERRIDE=1 set -- …`) so its use
is never silent. It is documented here as an emergency valve for the human operator, not something a
subagent brief should ever instruct — handing it out routinely would recreate exactly the unwired
control §0 (Guardrail 1) warns against, this time by teaching an agent that a `git commit -m` prefix
makes the hook go away.

**Direct answer to the question this ADR exists partly to settle: committing to `main` from the
primary needs no override.** Under the rule in §1, primary + non-`agent/*` branch is the default
**allowed** path — `main` was never blocked, so there is nothing for the orchestrator to invoke for
ordinary work. `HARNESS_ALLOW_BRANCH_OVERRIDE=1` exists only for the cases the rule *does* block
(primary on an `agent/*` branch; a worktree on a non-`agent/*` branch) and an operator judges, in
that moment, to be a deliberate exception.

### 5. Verification matrix

All four combinations were exercised with throwaway files, real `git commit` invocations, and
explicit pathspecs (never `-A`) to avoid touching the other lane's concurrently staged
`tools/worktree/README.md` / `land-worktree.sh` — this session ran inside the live primary checkout
while that work was in progress, not a clean sandbox. Refused cases create no commit and need no
cleanup; the two allowed cases were undone with `git revert --no-edit`, not history rewriting, to
stay safe under concurrent commits from the sibling lane. Full transcript:
`docs/superpowers/plans/task-HOOK1-report.md`.

| # | Location | Branch | Expected | Result |
|---|----------|--------|----------|--------|
| A | linked worktree (`wt-WT1-example`) | `agent/WT1-example` | allowed | **allowed** — commit `1dc9397`, reverted via `reset --soft` |
| B | linked worktree | `not-an-agent-branch-2` (non-`agent/*`; `main` itself cannot be checked out there — git already refuses two worktrees on one branch) | refused | **refused**, teaching message, exit 1, no commit |
| C | primary | `main` | allowed | **allowed** — commit `49341fa`, undone by revert `6cf9ff7` |
| D | primary | `agent/hook-test-primary` (new branch, not checked out elsewhere) | refused | **refused**, teaching message, exit 1, no commit |
| E | linked worktree | non-`agent/*`, with `HARNESS_ALLOW_BRANCH_OVERRIDE=1` | allowed (override) | **allowed** — commit `a76a12f`, printed the bypass warning, branch deleted after |

Test B could not literally use `main` as its branch name, because git already refuses to check out a
branch that is checked out in another worktree — `main` is checked out in the primary for the
duration of this task. A differently-named non-`agent/*` branch (`not-an-agent-branch-2`) exercises
the identical code path in the hook (the `case "$branch" in agent/*)` test does not special-case
`main`), so the substitution is faithful to what the hook actually checks, not a weaker stand-in for
it.

**A near-incident during this verification is itself evidence for §3's fail-closed design being the
right call.** The throwaway `.git/hooks/pre-commit` probe from §2 (`exit 1` unconditionally) was
left in place between the detection experiment and the `core.hooksPath` work; because
`core.hooksPath` was already pointed at `.githooks/` by the time other work resumed in this checkout,
the stub was masked rather than firing — but it blocked at least one legitimate commit from the
concurrent `tools/worktree/` lane before that lane's agent noticed, and that agent **retried instead
of reaching for `--no-verify`**. The stub has been removed (`rm .git/hooks/pre-commit`, confirmed
absent — only `*.sample` files remain) and this ADR names the incident rather than omitting it,
per Guardrail 6: an unmeasured claim of "clean" is worse than a documented near-miss.

### 6. What this hook cannot catch — stated plainly, not left to be discovered

This is the section CLAUDE.md's own brief for this task warned against skipping, and ADR-0071 §4's
mirror (safety code that exists, is wired nowhere, while operators are told to rely on it) makes the
cost of skipping it concrete:

- **It cannot stop `git add -A` or any other broad-pathspec stage.** The hook inspects only the
  current branch name and which side of the primary/worktree line the commit runs on — it never
  reads `git diff --cached` and has no opinion on *which files* are staged. An agent that stages a
  sibling's edits alongside its own on a correctly-named `agent/*` branch, inside its own worktree,
  produces a commit this hook allows without comment.
- **It cannot detect a pathspec-less `git commit`.** Same reason: nothing here inspects the diff.
  The four "swept into another agent's commit" incidents this ADR's motivating round produced are
  a staging-discipline failure, not a branch/checkout failure, and this hook's rule (§1) was never
  aimed at that axis.
- **It cannot prevent an agent editing files outside its declared lane.** A brief's "touch only
  these paths" instruction remains exactly that — a sentence in a prompt — for anything this hook
  does not check. Lane discipline beyond the branch/checkout correspondence has no mechanical gate
  anywhere in this project today.
- **It cannot enforce its own installation.** §3's measurement is the sharpest form of this: an
  unset or wrongly-pointed `core.hooksPath` makes every commit succeed with **zero** indication
  enforcement is off. No script running *as* the hook can defend against the hook not being invoked
  at all — this is a property of git's hook-dispatch, not a gap in `.githooks/pre-commit` specifically.
- **It cannot survive `--no-verify`.** Anyone (human or agent) who passes `--no-verify` bypasses
  every hook `git` has, unconditionally, with no visible refusal for this hook to phrase a teaching
  message into. §5's near-incident is notable precisely because the blocked agent did not do this.

**A brief that says "stay in your lane" is still required.** This ADR closes exactly one narrow gap
in that instruction — the branch a commit lands on matches where it was made — and documents the
rest as open, the same way §3 of ADR-0071 asks every control in this project to be described:
honestly, by what it is wired to, not by what it was hoped to cover.

### 7. Alternatives rejected

- **A `commit-msg` or `post-commit` hook instead of `pre-commit`.** Rejected: both run after the
  commit object already exists, so "refuse" would mean "commit, then tell the operator to undo it"
  — strictly worse than refusing before the object is created, with no compensating benefit for this
  rule (unlike a `commit-msg` hook, which legitimately needs the composed message `pre-commit` does
  not yet have).
- **A CI check that audits branch/checkout correspondence after the fact.** Rejected under this
  task's own constraint and Rule 2 (simplicity first): this project has no CI pipeline to attach it
  to, and building one to catch a defect a three-line comparison in a hook already prevents
  before it happens is the premature-abstraction failure Rule 2 names directly.
- **Leaving `core.hooksPath` repo-relative (`.githooks`), matching the more common convention.**
  Rejected by §3's own measurement: a relative path's resolution depends on the committing
  worktree's own checked-out tree, and the failure mode (an agent branch missing `.githooks/`
  entirely) is silent and total — indistinguishable from no hook at all — which is precisely the
  category of control this task exists to prevent.
- **No override at all, on the theory that an unconditional gate is the safest gate.** Rejected:
  CLAUDE.md's Server Safety rules already forbid destructive unattended operations, but a hook with
  no human escape hatch for a genuine edge case (a corrupted worktree, a one-off repair commit) turns
  "refuse and teach" into "block and strand" — the brief for this task asked explicitly whether an
  escape hatch was needed and this ADR answers yes, scoped to a human-invoked, visibly-logged,
  non-`FLEET_*` variable rather than a standing config toggle.

## ADR-0075 — `BackendTarget.effort` is optional; `None` means "send no effort parameter"

**Status.** Accepted. Supersedes the `Effort` column of ADR-0009's tier table for the CHEAP tier,
and the `effort: low` on the CHEAP target in SPEC.md §9's `config/models.yaml` example.

**Context.** ADR-0009 pinned the CHEAP tier at `effort: low`. Anthropic's own documentation is
self-contradictory about reasoning-effort support on Haiku 4.5: one page states the *parameter*
is rejected on that model, another states only the `max` *level* is. Under the stricter reading,
every CHEAP call on the shipped `default` profile would 4xx — i.e. three of the twelve §9 roles
would be dead on arrival, and only in production, since nothing in the test suite sends a real
request.

**Decision (orchestrator, not an agent recommendation).** Do not adjudicate the upstream
contradiction. Stop sending the parameter on that target, because omitting it is strictly safer
and costs nothing.

The first attempt at this deleted the `effort: low` line from `config/models.yaml` and stopped
there. That did not work, and the way it failed is the reason this ADR exists:
`BackendTarget.effort` was non-optional with default `"medium"`, so deleting the line did not
remove the parameter — it silently substituted a value the operator had never written, and left a
backend that honours `target.effort` still sending an effort on every CHEAP call. The defect was
not fixed; it was made invisible. Verified after the fact by resolving the live config rather than
by re-reading the YAML: all three CHEAP roles reported `effort='medium'`.

So: **`effort` is now `Literal["low","medium","high"] | None`, defaulting to `None`.** There is no
honest default for "the operator did not say", so absence is representable. Config either declares
an effort or it does not.

**Consequences.**

1. **Every backend MUST omit the effort parameter entirely when `target.effort is None`**, and
   must never substitute a level of its own. This is a contract on the adapter, not a suggestion:
   re-introducing a default anywhere below the config layer restores exactly the defect above, one
   layer further from view. `openai_compatible` sends no effort at all under any value (it is a
   hosted reasoning-model knob; a local llama.cpp or TGI server answers an unknown field with a
   400, which would surface as a `CONNECTION` failover against a healthy endpoint).
2. `effort` is a cache-key component (§6, `CacheKeyParts`). `None` is keyed as `""`, deliberately
   distinct from `"medium"` — collapsing them would put the fabricated default straight back into
   the key. `LlmCallRecord.effort` and the `llm_cache.effort` column follow the same spelling:
   absence round-trips as the empty string, so the column stays `TEXT NOT NULL` and **no database
   migration is required**.
3. **One-time cache orphaning on CHEAP, and only there.** CHEAP's key contribution moved twice
   (`low` → `medium` → absent), so pre-existing CHEAP entries will not be read again and age out
   normally under `fleet gc --cache-max-age`. Correctness and per-call cost are unaffected; only
   the hit rate, and only until the cache refills. **This is the one operator-visible symptom: a
   single CHEAP hit-rate dip after upgrading, which is expected and is not a cache bug.** It is
   recorded here because a hit-rate dip with no written cause is indistinguishable from a
   regression, and someone would have spent a day on it.

**Rejected alternatives.** *Keep `"medium"` as the default and have backends skip sending it when
it equals the default* — indistinguishable from an operator who deliberately chose `medium`, and
silently changes meaning if the default ever moves. *Make the column nullable* — a migration, plus
two spellings of absence (`NULL` and `""`) for a value whose only job is to hash consistently.

**Provenance.** The doc contradiction was surfaced by the BK1 lane against the `claude-api` skill
source; the `"medium"` substitution was caught in re-review of BK2 fix round 2; this entry is the
orchestrator's decision, recorded per CLAUDE.md guardrail 1.
---

## ADR-0076 — "`fleet resume` reconciled everything it can and cannot continue" is **exit 2, not exit 1**, and it reuses §10's existing usage code rather than minting a twelfth: retrying it unchanged produces the identical refusal, which is exactly what exit 2 already tells CI — and the cost of getting this wrong is a retry loop that spends one forge call per open PR per iteration

**Status:** accepted, `agent/RS1`. **Supersedes nothing.** Anchored at `4a519b3` (the landing this
corrects) on base `7a8bfbb`.

### 1. The decision

`fleet resume`, having completed every §11.5 step that exists — the config-drift audit, any budget
raise, the `--repoll-prs` re-poll, the step-3 stale-lease sweep and the step-7 projection — and
stopping only because step 5 (re-check each phase's durable **evidence** and demote the repo to its
re-entry floor — the phase **above** the **highest** phase below the settled frontier whose durable
evidence still holds **or** which is a `DEGRADED`/`SKIPPED` hard stop
(`orchestrator/reentry._HARD_STOPS`, tested *before* evidence, so such a row ends the walk whatever
holds below it), never that phase itself, and `SCAN` only if there is no such phase — searching
*downward* from the settled frontier;
Constraint 7) has no implementation, exits **2**, via a dedicated
`ResumeIncompleteError(FleetCliError)` whose `exit_code` is `ExitCode.USAGE`.

**Wording corrected after this ADR was accepted.** §1 originally described step 5 as "re-check
preconditions and demote to the earliest phase whose **precondition** holds" — the algorithm D67
recorded as a design defect and ADR-0077 §6 replaced with two named predicates. That phrasing
promotes a never-cloned repo to Phase 4, because `rdepverify.preconditions_hold` returns `True`
when the BUILD row is missing. The decision this ADR records (exit 2, `ResumeIncompleteError`) is
unchanged; only the description of the step that is missing. `src/fleet/cli.py` still ships the
old phrasing in two operator-facing messages and is owned by another lane — reported, not edited.
A second correction followed: the replacement clause carried a `SCAN` fallback that ignored
`orchestrator/reentry._HARD_STOPS`, which `phase_floor` tests *before* evidence, so a `DEGRADED` or
`SKIPPED` row below the frontier ends the walk above itself and `SCAN` is never reached. Measured:
`BUILD` `DEGRADED` ⇒ floor `VERIFY`; `TRANSFORM` `SKIPPED` ⇒ floor `BUILD`.

Exit **1** is retained for the case where something actually failed: a `PrEmissionError` out of
`--repoll-prs` (the forge was unreachable, unauthenticated or rate-limited) still exits 1, and it
outranks the step-5 refusal when both are true.

**What CI should branch on**, and this is the whole point of the split:

| Exit | Meaning for `fleet resume` | Correct CI behaviour |
|---|---|---|
| 0 | `--dry-run` health check passed; nothing written | continue |
| 1 | the forge call failed — **the reconciliation still committed** | retry is legitimate, but fix credentials/rate limits first |
| 2 | reconciled; cannot continue until step 5 is implemented, or a flag was refused | **do not retry**; a human must change code or the command |
| 3, 10 | budget halts | `--raise-budget` / `--raise-wave-budget` |

### 2. Why exit 1 was wrong, measured rather than asserted

At `4a519b3` a single code covered three materially different outcomes: reconciled-but-incomplete,
forge-failed-and-nothing-reconciled, and a genuine crash. Exit 1 is `UNEXPECTED_ERROR`, and the
reflex wrapper around a verb that exits 1 is a retry loop.

The cost is not hypothetical and it scales with fleet size. Each `fleet resume --repoll-prs`
iteration issues one `gh pr view` per **open** PR (`vcs/github.py::sync`, sequential and bounded by
the `git_net` semaphore, but bounded by nothing across invocations). A 250-repo fleet mid-run
therefore spends up to 250 forge calls per retry on a refusal whose answer cannot change until
somebody writes code. That trips GitHub's secondary rate limit, at which point `_pr_sync_impl`
starts raising — converting a clean, idempotent reconciliation into a failing one, and sending the
operator to debug a rate limit instead of reading "step 5 is not implemented".

A verb whose refusal is **stable under retry** must not share a code with one whose refusal is
transient. That is the same argument §10 already makes for exit 11 ("deliberately not 3") and for
exit 9, and it is the argument applied here.

### 3. Why exit 2 specifically, and why not a twelfth code

`UsageError`'s contract in this codebase is "the operator must edit a file, a flag **or a stub**
before retrying", and §10's own prose already stretches exit 2 past mistyped commands: it covers a
profile with an unpriced target, `fleet pr --ready` against an unresolved stub, and a second run
started against a mirror another live run owns. None of those is a typo. The unifying property is
**the harness refused deliberately, and an identical re-invocation gets an identical refusal** —
which describes this case exactly.

Rejected alternatives, each for a specific reason:

- **A new `ExitCode` member (12).** SPEC §10 enumerates exactly `0`–`11` and calls that set the
  contract CI composes against; adding a member makes the spec's own table wrong until SPEC.md is
  edited, and SPEC.md is the main session's to change (CLAUDE.md §3), not a lane's. It would also
  oblige every existing wrapper to learn a code for a condition that disappears the day step 5
  lands. Minting a permanent code for a temporary state is the wrong trade.
- **Exit 7 (`REQUIRES_HUMAN_INTERVENTION`).** Semantically close and therefore actively dangerous:
  exit 7 means "the run *completed* and a repo needs a human". Overloading it with "the harness is
  incomplete" is the same corruption §13 row 45 warns about for `stub_reconcile` — the moment two
  unrelated conditions share exit 7, an operator can no longer read it as a statement about repos.
- **Exit 11 (`SEQUENCE_REFUSED`).** Its documented property is "changed nothing and costs nothing".
  A resume that reached this point has changed a great deal, all of it durable.
- **Exit 0 with a warning.** Rejected outright: the run did not resume. A green exit for a verb
  that did not do the thing it is named after is how a nightly pipeline reports success on a fleet
  that has not moved in a week.

### 4. Consequences, including the one that is unpleasant

- Exit 2 is now overloaded three ways for this verb (config error, refused flag, incomplete
  resume). CI cannot tell them apart from the code alone. Accepted: all three are "do not retry,
  a human must change something", which is the only decision a wrapper makes on a non-retryable
  code, and the message and the `--json` payload distinguish them for anyone reading.
- The payload is emitted on stdout **before** the refusal is raised, so `--json` consumers get the
  full reconciliation report on the exit-2 path. That ordering is load-bearing and is asserted by
  `tests/test_cli.py::test_the_reconciliation_payload_is_emitted_before_the_step_5_refusal`, which
  is deliberately the only `--json` resume test that does NOT pass `--dry-run`: every other one
  exercises the exit-0 path and would keep passing if the ordering were reversed. Moving `_emit`
  below the `raise` makes it fail on empty stdout — checked by mutation, not assumed.
- **This ADR is temporary by construction.** When §11.5 step 5 lands, `ResumeIncompleteError`
  should be deleted, not repurposed. If it is still here after step 5 exists, that is a defect.
---

## ADR-0077 — `SUCCEEDED` stays terminal for every automatic path and becomes demotable for exactly one: `RESUME_DEMOTE` gated on a keyword-only `resume=True`, mirroring `OPERATOR_REOPEN`, with a `PhaseDemoted` finding the caller cannot decline to take

**Status: DECIDED AND IMPLEMENTED**, at `agent/DEM1` branched from `7a8bfbb`. The gate has **no
caller**, deliberately: it is subtask 1 of the ten-part §11.5 step-5 decomposition and every other
subtask is downstream of the answer, so it lands alone and is verified alone. `orchestrator/reentry.py`,
`phase_floor`, `evidence_holds`, the demotion writer and the `_resume_impl` wiring are subtasks 2, 5, 6
and 7 and are **not** in this ADR's implementation.

**Provenance (CLAUDE.md Guardrail 1).** The `RESUME_DEMOTE` + `resume=True` shape is an **Agent
Recommendation** — it originated in a design pass (`docs/superpowers/plans/design-resume-step5.md`
§6, option B) and was accepted by the orchestrator. `docs/SPEC.md` requires the *demotion*; it does
not name this mechanism, and nothing below should be cited as a SPEC requirement. What the SPEC does
require is quoted verbatim in §1.

### 1. The contradiction, both sides quoted

`docs/SPEC.md:180-182`, Constraint 7, as it read at `7a8bfbb`:

> **Resume validates preconditions, never blind-replays** (Constraint 7): before re-entering a
> phase, `runner.py` re-checks the phase's declared preconditions (below) against SQLite; a
> failed precondition demotes the repo to the earliest phase whose precondition holds.

`docs/SPEC.md:6777-6778`, §11.5 step 5, same commit:

> (5) re-check each phase's declared preconditions and demote to the earliest
> phase whose precondition holds (Constraint 7)

`src/fleet/models/enums.py:45,48-49`, same commit:

> ```python
> RepoStatus.SUCCEEDED: frozenset(),
> ```
> ```
> }  # Terminal statuses map to the EMPTY set, which is what makes them terminal mechanically
> # rather than by prose: no crash sweep can resurrect an abandoned repo into RUNNING.
> ```

and `transition()` (`enums.py:59-68`), documented as "**THE single gate for every status write**
(§6, §11.5)", ends `raise ValueError(f"illegal status transition {old.value} -> {new.value}")` for
anything unlisted.

A demotion is, by definition, a write of `PENDING` over a `SUCCEEDED` `phases` row. The SPEC says in
the imperative that resume must perform it; the type system says in the imperative that it cannot.
**Demotion was literally unwritable**, and every one of the nine remaining step-5 subtasks assumes a
legal demotion write. This ADR is the reconciliation.

### 2. The decision

Two names and one parameter in `src/fleet/models/enums.py`:

```python
RESUME_DEMOTE: dict[RepoStatus, frozenset[RepoStatus]] = {
    RepoStatus.SUCCEEDED: frozenset({RepoStatus.PENDING}),
}

def transition(
    old: RepoStatus, new: RepoStatus, *, operator: bool = False, resume: bool = False
) -> RepoStatus:
    ...
    if resume and new in RESUME_DEMOTE.get(old, frozenset()):
        return new
    raise ValueError(...)
```

plus `PHASE_DEMOTED_KIND = "PhaseDemoted"`, a frozen `PhaseDemotion` record, and `demote()` — see §4.

**This is the second instance of an existing pattern, not a new mechanism.** `OPERATOR_REOPEN`
(`enums.py:52-56`) is the same construction for the same class of problem — a legitimate path that
must escape a mechanically-terminal state — and its own comment states the general principle:

> Explicit, audited to `findings`, and unreachable from any automatic path — which is precisely
> the difference between "the operator un-abandoned it" and "the reaper lost track of it".

The precedent was **verified rather than assumed** before building on it: `OPERATOR_REOPEN` exists at
`enums.py:52`, is a `dict[RepoStatus, frozenset[RepoStatus]]` keyed on `REQUIRES_HUMAN_INTERVENTION`
alone, and is consulted only behind the keyword-only `operator: bool = False` at `enums.py:59,66`.
`RESUME_DEMOTE` copies that shape exactly, so a reader who understands one understands both.

### 3. Why this over the alternatives

**A — open `SUCCEEDED → PENDING` in `ALLOWED_TRANSITIONS` unconditionally.** Rejected. It is one
line, and it destroys the exact property the comment at `enums.py:48-49` defends: every crash sweep,
every worktree/container reaper and every `_on_breach` handler would gain the ability to silently
un-finish landed, green work, and the terminality of `SUCCEEDED` would revert from mechanical to
prose. The invariant worth keeping is not "nothing ever writes over `SUCCEEDED`" — it is "**no
automatic path** can", and a default-`False` keyword-only flag preserves that precisely, because a
caller that does not name it gains no new edge at all.

**C — do not demote `SUCCEEDED`; restrict step 5 to non-settled rows.** Rejected, and this is the
more dangerous of the two. It needs no enum change and no SPEC edit, which is exactly what makes it
attractive and exactly why it is wrong: it turns step 5 into a **no-op in the scenario Constraint 7
was written for**. A Phase-3 repo whose `BUILD.bazel` was reaped is `SUCCEEDED` at Phase 2 and
`SUCCEEDED` at Phase 3, so nothing is demotable, and the repo re-enters Phase 4 to verify a package
that no longer builds. Converting a documented contradiction into a silent correctness hole is
Rule 11's named failure mode: a resume that quietly skips settled rows is quietly incomplete, which
is the failure this subsystem exists to prevent.

**D — model demotion as data: leave `phases.status` alone, add a `resume_floor` column.** Rejected
under Guardrail 4. It creates a second source of truth for "which phase is this repo at" beside
`phases.status`, and would have to be threaded through `WaveScheduler.admit`, `_gated_members` and
`state/projection.py` — a shadow scheduler state, for a question `phases.status` already answers.

### 4. The audit obligation is a convention, deliberately reinforced — and NOT a mechanism

A demotion throws away landed, green work. `orchestrator/runner.py` already argues, for the strictly
*smaller* event of a rejected checkpoint, that "a rejection means landed work was thrown away and
that must be visible to whoever reads the wave". A demotion must be at least as loud, so it writes a
`findings` row of kind `PhaseDemoted` (`findings.kind` is free text by schema design —
`state/schema.sql:253` — so this needs no migration).

`demote()` binds the two together, returning them as one value:

```python
def demote(old, *, repo_id, phase, reason) -> tuple[RepoStatus, PhaseDemotion]
```

**An earlier draft of this ADR claimed "there is no call that yields the demoted status without the
record it owes". That claim was FALSE and is retracted here.** `transition()` is public and
exported, and `transition(SUCCEEDED, PENDING, resume=True)` returns `PENDING` on its own — no
finding, no error. Python affords no way to close that door: a module-private alias, a leading
underscore or a sentinel token are all conventions wearing a mechanism's clothes, and inventing one
would restate the overstatement in code instead of retracting it.

This matters concretely rather than academically. `state/repository.py` is subtask 6's file and
calls `transition()` at zero sites today, so its author starts from the SPEC — and if the SPEC named
`transition(..., resume=True)` as the demotion path, they would call it, emit no finding, and
demotions would go silent while a test named "cannot be taken without the finding" kept passing.
That is exactly the failure this gate exists to prevent, arriving through the door the gate left
open. So the reinforcement is placed where that author will actually meet it:

1. **`docs/SPEC.md` names `demote()` in BOTH paragraphs a writer reads** — §11.5 step 5 *and* the
   §3 Constraint 7 bullet — and both say explicitly that `transition(..., resume=True)` "returns
   the status ALONE and would demote silently". Constraint 7 is named second because it was
   **missed the first time**: the round that first wrote this item scoped its own remedy to
   "§11.5 step 5", so Constraint 7 went on naming `transition(..., resume=True)` as the demotion
   write *and* asserting that path emits the finding — which `transition()` has never done.
   Corrected in `f02d124` (CR1 C-1). A remedy that names one of two sibling paragraphs, then is
   written up as complete, is the failure this ADR exists to prevent arriving through its own
   documentation (CLAUDE.md Guardrails 6 and 7).
2. **`transition()`'s own docstring says `DO NOT pass resume=True here`**, and names `demote()`.
3. **`PhaseDemotion`'s docstring carries an `HONEST LIMIT` paragraph** stating the gap in the one
   place a reader of the type is guaranteed to look.
4. **A test pins the gap** (`test_transition_demotes_without_writing_a_record_or_naming_a_new_sink`)
   so it is visible in the suite rather than contradicted by it. If anyone closes the door, that
   test fails and is deleted deliberately — which is the correct way to find out. **What it pins,
   precisely:** `transition()`'s body may reference only the names in `TRANSITION_GLOBALS`, holds
   no mutable default and no closure cell, carries no function attribute, returns a bare
   `RepoStatus` rather than a `(status, record)` pair, accumulates nothing in any module-level
   `dict`/`list`/`set`/`frozenset` (the four types the snapshot helper filters to — a sink of any
   other type is not covered), logs nothing through `logging`, and `enums.py` contains no `fleet`
   import at any indentation. **What it does not pin is stated in §4.2 — the enumeration is
   deliberately not claimed exhaustive.**

An admitted convention is strictly better than an overstated guarantee, because the next author
trusts the guarantee. What `demote()` genuinely buys is not enforcement but **strictness**: it
refuses inputs `transition()` accepts (§4.1), and it makes the audit record impossible to *forget*
for anyone who takes the path the SPEC names.

#### 4.2 Enumerating exits is the wrong shape of check — and the whitelist is still not a proof

Bullet 4's tripwire took three cuts, and the first two failed the same way this ADR's §4 failed:
**the claim outran the check.**

- **Cut 1** was named `..._can_still_demote_silently` and asserted only the return value. A reviewer
  falsified it by binding an audit side-effect into `transition()` with the return untouched. The
  test still passed while its own name became false.
- **Cut 2** added a module-state snapshot and `caplog`, and this ADR then asserted as fact that a
  side-effect "has exactly two places to go". A reviewer defeated it **six** ways: a function
  attribute (`transition.audit`), a mutable default argument (`__kwdefaults__`), a **function-local**
  `from fleet...` import, `warnings.warn`, a `ClassVar` on `PhaseDemotion`, and `print()`. The third
  is the one that mattered: a deferred, function-local import is the *only* way subtask 6 could hand
  `enums.py` a `StateWriter` without a module-scope import cycle — precisely the route the source
  scan existed to catch, and precisely the route it missed, because it matched `startswith` at line
  start and the import was indented.

Each enumeration was defeated by a route not enumerated. That is not bad luck; **enumerating exits
is the wrong shape of check**, because the enumerator has to think of every form and the defeater
has to think of one.

- **Cut 3** inverts it into a **whitelist**: `TRANSITION_GLOBALS` is the complete set of global and
  attribute names `transition()`'s body may reference, asserted against `co_names`. A side effect
  must *name* something to reach it, so every one of the six forms enlarges `co_names` and trips the
  assertion — including forms nobody predicted. All six were re-run as mutations and **all six now
  fail**. `__kwdefaults__`, `__defaults__`, `co_freevars` and `vars(transition)` are pinned
  alongside it, and the source scan now uses `.strip().startswith(...)` so an indented import is
  caught.

**And it is still not a proof, which this ADR states rather than discovers later.** A side effect
routed entirely through names *already on* the whitelist passes it. Verified, not hypothesised:
binding `RESUME_DEMOTE` to a `dict` subclass whose `get()` appends to an **instance attribute**
records every demotion while the whole test passes — `transition()`'s body is byte-identical, so
`co_names` is unchanged, and `dict.__repr__` shows only mapping contents, so the module-state
snapshot cannot see the sink either. Review found six more, all adversarial: `ALLOWED_TRANSITIONS`
as a side-effecting subclass; `RESUME_DEMOTE[SUCCEEDED]` as a **`frozenset` subclass** with a
recording `__contains__` (a value *inside* a whitelisted global, its identity untouched); a side
effect on `RepoStatus.__hash__`, an **argument type** `transition()` never names; a `sys.setprofile`
hook installed at import; an audited wrapper over the **package re-export** in `models/__init__.py`,
since the test pins `enums.transition` and not the exported name; and a sink reached through an
attribute name already in the body.

So the honest statement is narrower than "its own body": **the tripwire catches a side effect that
introduces a NEW name into `transition()`'s body, and catches nothing that works through names,
values, argument types, interpreter hooks, or re-exports already in play.**

**Freezing the identity of every whitelisted global — the obvious next patch — would not close
this.** Four of the seven escapes never touch a whitelisted global's identity: the `frozenset`
subclass hides inside a value, `RepoStatus.__hash__` is an argument type, `sys.setprofile` is
outside the module, and the re-export wrapper is outside the function. The patch would buy the
*appearance* of closure while leaving the majority of the known escapes open — which is this ADR's
own §4 failure mode, one level up. So the boundary is documented and not patched: past this point
the test would be asserting the absence of an adversary rather than a property, and every one of
these escapes requires an author deliberately building a recording sink and then hiding it. The
convention is what stops honest mistakes; nothing here is claimed to stop a determined author.

#### 4.1 `demote()` is deliberately stricter than `transition()`

`demote()` accepts a `RESUME_DEMOTE` key and nothing else. Delegating the guard to `transition()`
would have been wrong, and not only for the terminal statuses: `ALLOWED_TRANSITIONS` already routes
`RUNNING -> PENDING` (`enums.py:35`, the crash sweep) and `BLOCKED -> PENDING` (`enums.py:39`, an
unblocked dependency) to `PENDING`, and **both are matched before the resume branch is ever
consulted**. A `PhaseDemotion` minted for either would claim landed, green work was discarded when
none ran. The `BLOCKED` case is not hypothetical: step 5 runs **before** step 6's `blocked_by`
recompute, so a still-`BLOCKED` Phase-2 row is a state subtask 6 will genuinely encounter.
An already-`PENDING` row is refused for the same reason — `transition()` takes it as an idempotent
no-op (§11.7) and there is nothing to demote. A caller that wants those transitions wants
`transition()`, and wants no finding.

**One hazard handed forward to subtask 6, recorded here so it is not rediscovered as a bug.**
`cli._note_finding` fingerprints on `(run_id, repo_id, kind)` alone and `ON CONFLICT … DO UPDATE`s.
Three naive per-phase calls for one repo therefore collapse into **one row**, silently losing two
demotions. `PhaseDemotion.payload()` carries `phase` for that reason; the writer must either fold a
repo's demotions into a single finding or fingerprint per phase.

### 5. `DEGRADED`, `SKIPPED` and `REQUIRES_HUMAN_INTERVENTION` are deliberately not keys

`RESUME_DEMOTE` has exactly one key. Each omission is a decision, not an oversight:

- **`REQUIRES_HUMAN_INTERVENTION`.** §12 item 46 (ii) requires a test that drives "every automatic
  sweep — the reaper, **`fleet resume`**, `stub_reconcile`, `blocked_by` recomputation" and finds
  none of them able to move a repo out of it. `fleet resume` is named there **by name**, so the flag
  that makes step 5 writable must not also make resume an operator. Un-abandoning stays `operator=True`.
- **`SKIPPED`.** A config exclusion. Resume does not re-decide the operator's config.
- **`DEGRADED`.** This is design ambiguity 5, resolved here because §11.5 step 5 contains no
  carve-out. `DEGRADED` is deliberately non-terminal (`enums.py:25-27`) but leaves the machine
  **only** through a budgeted revalidation round (§3.5.1). Demoting it to `PENDING` would spend that
  budget by the back door with no round recorded — so `DEGRADED` is treated as **settled for
  demotion purposes**: step 5 neither demotes it nor searches past it. `transition(DEGRADED,
  PENDING, resume=True)` raises, and subtask 2's `phase_floor` must classify it accordingly.

### 6. The second contradiction, resolved by naming two predicates instead of one

Constraint 7 said "re-checks the phase's **declared preconditions**", and `runner.py`'s only
precondition mechanism is `BaseWorker.preconditions_hold` — whose own contract (`runner.py:212-214`)
says:

> `True` means "the checkpoint describes the tree in front of me, re-enter for `remaining_units`
> alone", and `False` means "it does not — run the phase whole from its anchor". **Neither verdict
> ever means "skip the work".**

A verdict that never means "skip" cannot select a phase to skip *to*, and reading it as one is
actively harmful in both directions:

- Ten of the fifteen implementations — including **all four** phase composites, the only things a
  per-phase walker can call — return `False` precisely when there is nothing to resume. For a fresh
  repo the ascending walk yields `False, False, False, False`: "the earliest phase whose precondition
  holds" is the empty set, and the SPEC defines no behaviour for it.
- `rdepverify.preconditions_hold` returns `True` when the BUILD row is **missing**, deliberately
  ("No BUILD row at all is a first admission by the runner, not evidence of a failure"). An ascending
  scan therefore answers "Phase 4 holds, Phase 1 does not" for a never-cloned repo — it **promotes to
  Phase 4** the repos that have not been cloned. That is the default state of every repo at the start
  of a run, not a corner case.

The resolution: two named predicates rather than one overloaded name. `preconditions_hold` stays at
its single documented call site (`PhaseRunner._re_entry`, "the only point where both the typed
payload and the `WorkerContext` the worker would receive exist"). Step 5 searches a separate,
resume-owned `evidence_holds` — durable state only, `phases` + Git, no payload and no
`WorkerContext` — **downward from the settled frontier**, so monotonicity is a property of the
traversal rather than an assumption about the predicates. `docs/SPEC.md` §11.5 step 5 and the
Constraint 7 bullet were rewritten in this task to say so; leaving them as written guaranteed the
next reader would re-derive the broken algorithm. `evidence_holds` itself is subtask 5.

### 7. What this ADR does not do

- **It does not close the `transition(..., resume=True)` door** — see §4, which retracts the
  earlier claim that it did.
- **It ships no caller.** `resume=True` and `demote()` are reachable from no code in `src/` today.
  That is the intended end state of subtask 1: the gate is verified in isolation, and a
  wrongly-shaped gate is cheaper to fix before four subtasks are written against it. (`operator=True`
  is in the same position — `fleet retry`'s documented escape is itself still unwired — so this is a
  known and accepted shape in this file, not a new one.)
- **It decides that `attempts` is RETAINED on demotion, but enforces nothing.** Design ambiguity 2
  is resolved — by analogy with §11.5 steps 3 and 4, which say so explicitly — and the §11.5 step 5
  text amended in this task states it. Nothing in this ADR's code can check it: `PhaseDemotion`
  carries no `attempts` field and `demote()` touches no row. Subtask 6 owns the enforcement, and
  its success criterion already requires asserting on the column.
- **It does not decide where a demoted repo re-enters the schedule** (ambiguity 3, subtask 8) or
  **`--from-phase` semantics** (ambiguity 4, subtask 9).

### 8. Verification

At `agent/DEM1`: `mypy --strict src/fleet` → `Success: no issues found in 107 source files`;
`pytest tests/test_state_models.py` → 120 passed; `pytest tests/test_schema_sql.py` → 16 passed —
the last of these because `test_schema_sql.py:342` derives the `phases.status` CHECK domain from
`{s.value for s in RepoStatus}`, and this ADR adds **no** `RepoStatus` member, so the DDL is
untouched and unmigrated. Four tests carry the reasoning:

1. A demotion without `resume=True` is refused — including with `operator=True`, which is not a
   resume key.
2. `RESUME_DEMOTE.keys() == {SUCCEEDED}`, with RHI, DEGRADED and SKIPPED each proven still refused
   *under* `resume=True`, and SUCCEEDED proven to open onto `PENDING` and nothing else.
3. `demote()` returns the `PhaseDemotion` alongside the status, payload asserted field-by-field,
   and refuses all six non-`RESUME_DEMOTE` statuses — `RUNNING`, `BLOCKED` and `PENDING` included
   (§4.1) — while `transition()` is asserted to still accept `RUNNING`/`BLOCKED -> PENDING`, so
   the strictness is demonstrably `demote()`'s own rather than inherited.
4. The open door is **pinned rather than claimed shut** (§4): `transition(SUCCEEDED, PENDING,
   resume=True)` is asserted to demote *and* to leave no record reachable from `transition()`'s
   own body under a NEW name — `co_names` whitelisted, no mutable default, no closure cell, no
   function attribute, a bare status returned, module-level `dict`/`list`/`set`/`frozenset`
   unchanged, `caplog` empty, and no `fleet` import at any indentation. Bounded, not exhaustive:
   §4.2 names the escapes that remain open, verified.

Test 2's loop over illegal targets is **derived from `RepoStatus`** rather than listed. An earlier
cut named three of the five reachable targets, so widening `RESUME_DEMOTE` to admit
`SUCCEEDED -> SKIPPED` passed both it and the `.keys()` assertion above it: the keys were airtight
and the values were not. Deriving the loop makes a new `RepoStatus` member enlarge it automatically.

**Every test here was mutation-tested rather than merely observed to pass**, and the mutations are
the rejected designs and the reviewed defects, not arbitrary edits:

| Mutation | Fails |
|---|---|
| Rejected alternative A — `ALLOWED_TRANSITIONS[SUCCEEDED] = {PENDING}` | test 1 |
| `demote()` guard reverted to `PENDING`-only (the I-1 defect) | test 3 |
| `ALLOWED_TRANSITIONS[RUNNING]` tightened instead of `demote()` | test 3's positive assertions |
| `RESUME_DEMOTE` widened to `{PENDING, SKIPPED}` (the N-2 defect) | test 2 |
| Door closed via a module-level audit registry (the N-1 falsification) | test 4 |
| Door closed via a `logging` call | test 4 |
| Door closed via a function attribute `transition.audit` | test 4 |
| Door closed via a mutable default argument `_audit=[]` | test 4 |
| Door closed via a **function-local** `from fleet.models import base` | test 4 |
| Door closed via `warnings.warn` | test 4 |
| Door closed via a `ClassVar` list on `PhaseDemotion` | test 4 |
| Door closed via `print()` | test 4 |

The last six are the forms a reviewer used to defeat cut 2 of test 4; all six kill cut 3 (§4.2).
One further form was run and **passes** — `RESUME_DEMOTE` bound to a `dict` subclass recording into
an instance attribute — and is documented in §4.2 as the boundary of the guarantee rather than
patched, because the point of §4 is that the claim matches the check.

Each mutation was reverted from a backup copy and the file re-diffed clean before commit.

---

## ADR-0078 — §9 rule 2's accepted `backend:` names are the LIVE §7.7 registry — the adapters that actually imported on this host — and never `SHIPPED_BACKENDS`, the four names we merely ship: a name we can spell is not a backend that can answer, and the difference is the whole value of a startup gate

**Status:** accepted, describing behaviour already landed on `main`. Anchored at `6a5e534` (`main`).
The change itself is `c36160e` ("BK1 fix round 1", `src/fleet/cli.py`); the four adapters it
narrows over are `21f5797` (`anthropic`), `92cfc94` (`openai_compatible`), `87b51f8` (`bedrock`)
and `fa066f4` (`vertex`). **Supersedes nothing.** Recorded late: the behaviour shipped last round
with no ADR and no test, which is item 16 of
`docs/superpowers/plans/open-items-audit-round-b.md`.

**Provenance (CLAUDE.md Guardrail 1).** The *policy* is SPEC, not an agent's invention:
`docs/SPEC.md:5679-5685` already states that a backend whose SDK is not installed "fails its import
inside `discover()` and is simply not registered", and that this becomes a startup error, with the
missing extra named, only when the active profile routes a tier through it (§13 row 36,
`docs/SPEC.md:7188`). What is an **Agent Recommendation** is the mechanism below — threading
`discover()`'s keys through the `FleetSettings.load(known_backends=...)` parameter — and the
equality (not membership) shape of the test that binds it. Nothing here should be cited as a SPEC
requirement.

### 1. The decision

`cli._load_settings` calls `llm.discover()` and passes its keys as `known_backends=`
(`src/fleet/cli.py:558`, used at `:560` and again at `:574` — **line numbers as of `6a5e534`**;
a concurrent lane had uncommitted edits to that file when this was written, which had already
shifted them by seven in the working tree, so cite the ref or cite the symbol
`_load_settings`). §9 rule 2 then validates every
`profiles.<profile>.<tier>[i].backend` against that set (`src/fleet/settings.py:1401`), so the gate
asks **"is this backend registered?"** rather than **"is this name spelled like one we ship?"**.

`SHIPPED_BACKENDS` (`src/fleet/settings.py:107`) survives as the *fallback* only:
`src/fleet/settings.py:1171` uses it when `known_backends` is `None`, which is the direct-`load()`
path used by tests and by a host with no SDKs at all. No production path reaches it.

The accepted set is therefore a **subset of the four shipped names, sized by the host's installed
SDKs**, and on any host that has not installed the `bedrock` and `vertex` extras it is a *proper*
subset. That is the narrowing.

### 2. What it narrowed to, measured under the interpreter that runs the code

Guardrail 6 — three different questions, and this is the **`find_spec` = installed** one, asked
under `.venv/bin/python` (the interpreter the harness and its tests run under), not under the
system `python3`, which does have `boto3` and would have given the wrong answer:

| Module | `find_spec` under `.venv/bin/python` | Imported by |
|---|---|---|
| `anthropic` | found | `llm/backends/anthropic.py` (core dependency) |
| `openai` | found | `llm/backends/openai_compatible.py` (core dependency) |
| `boto3` | **not found** | `llm/backends/bedrock.py:46-48` (`fleet[bedrock]`) |
| `google` | **raises `ModuleNotFoundError`** | `llm/backends/vertex.py:58-59` (`fleet[vertex]`) |
| `requests` | **not found** | `llm/backends/vertex.py:60`, travels with `fleet[vertex]` |

`fleet.llm.client.discover()`, run in that interpreter, returns exactly
`['anthropic', 'openai_compatible']`. So on this host the gate accepts **two** of the four names —
the "two-name narrowing" the audit item refers to. Two is a property of this host, not of the
harness: install both extras and `discover()` returns four and nothing is narrowed. The ADR records
the *rule*, and two is today's measurement of it.

### 3. Why the constant was the wrong name set

Before `c36160e`, `known_backends=` was never supplied from `src/`, so the gate fell back to the
literal four-name tuple. Two failures followed from that, and they compound:

1. `discover()` had **zero call sites in `src/`**, so `@register_backend` never fired in a real
   run. `RunContext.backends=None` fell back to an empty `registry()` and the first `complete()`
   raised `UnknownBackend` in wave 7 — with repos already cloned. §13 row 36's promise ("fails at
   startup, not in wave 7") was satisfied *vacuously*.
2. Even with the registry populated, validating against the constant accepts a profile naming a
   backend that **cannot answer on this host**. `bedrock` is spelled correctly and is genuinely a
   backend we ship; on a host without `boto3` it is also unreachable. Accepting it converts a
   startup error into a wave-7 error, which is the exact inversion the gate exists to prevent.

The unifying point: the constant answers a question nobody asked. An operator's `models.yaml` is
wrong in a way that matters only relative to **this** host, and the only artifact that knows this
host is the registry.

### 4. Consequences, including the two unpleasant ones

- **A profile naming an uninstalled extra now exits 2 where it previously booted.** This is the
  intended behaviour and it is a real, user-visible narrowing. The message distinguishes the three
  causes (`src/fleet/settings.py:1401-1421`) so the operator is not sent hunting a spelling mistake
  that is not there: uninstalled extra → `pip install 'fleet[<extra>]'`; correctly-spelled core
  backend → "its module failed to import on this host; check the install"; anything else → a typo.
- **`docs/SPEC.md:6365-6371`'s illustrative `models.yaml` routes the `default` profile's HEAVY tier
  through `bedrock` and its WORKHORSE tier through `vertex`.** An operator who copies that example
  onto a host without both extras now exits 2. The **shipped** `config/models.yaml` does not do
  this — it is `anthropic`-only on `default` and `openai_compatible`-only on `local`, so no shipped
  configuration is affected. Surfaced here rather than fixed: SPEC.md is the main session's to edit
  (CLAUDE.md §3), and this ADR must not be read as having adjudicated that listing.
- The `bedrock`/`vertex` adapters are still *shipped and tested* on a host without their extras:
  `tests/test_llm_backend_bedrock.py:62-108` installs a `sys.modules` SDK stub, guarded on
  `find_spec` (installed) rather than on `name in sys.modules` (imported so far), so it can never
  shadow a really-installed `boto3`. Only the routing gate narrows.
- `tests/test_cli.py:985-995`'s `skipif` — added by `c36160e` because the `local` profile routed every
  tier through an `openai_compatible` adapter that did not yet exist — is a **live `discover()`
  probe evaluated at collection**, not an xfail. `92cfc94` landed that adapter, so it re-armed by
  itself and the test runs. It is left in place: it is the correct guard for exactly this class of
  narrowing, and deleting it would have to be re-derived the next time an adapter is staged.

### 5. What binds it

`tests/test_backend_registry_gate.py`, two tests, one per half of the path.

`test_startup_hands_the_gate_exactly_the_live_registry_and_never_a_superset` spies on
`FleetSettings.load` and asserts `set(passed) == set(discover())` — an **equality**. The nearest
pre-existing assertion, `tests/test_llm_backend_anthropic.py`'s
`test_the_startup_gate_checks_the_live_registry_not_the_shipped_name_tuple`, is a disjunction
(`tuple(passed) != SHIPPED_BACKENDS or set(passed) == set(discover())`), and a **superset**
satisfies its first arm. That is the hole: widening the set one name at a time passes it silently.

`test_a_shipped_name_the_live_registry_lacks_is_refused_at_startup` drives the whole path —
`_load_settings` → `discover()` → `FleetSettings.load(known_backends=...)` → `_check_routing` —
against a copy of the shipped `config/` whose CHEAP target is rerouted to `bedrock`, with
`discover` replaced by a one-name registry. `discover` is replaced rather than leaning on this
host's missing SDKs so the assertion holds identically on a host that installs all four extras,
where the two sets coincide and the narrowing would otherwise be untestable. The rewritten target
declares `region`, so the refusal can only come from the rule 2 registry gate and never from
`_REQUIRED_TARGET_FIELDS`.

Mutation-checked (Rule 12: the discriminating mutation is one under which the **old** assertion
passes and the new one fails). Both mutations were applied in a detached `git worktree` at
`6a5e534`, proven to have changed the file with `git diff`, and reverted with the file re-diffed
clean — two sibling lanes were committing to this checkout at the time.

| Mutation | old disjunction | new test 1 | new test 2 |
|---|---|---|---|
| `cli.py:558` → `tuple(discover()) + ("bedrock",)` (adds back a third name) | **passes** | **fails** | **fails** |
| `settings.py:1401` → `... not in known_backends and ... not in SHIPPED_BACKENDS` (relaxes the gate) | **passes** | passes | **fails** |

The first mutation is the discriminating one for the CLI half: the old assertion's first arm is
true because a three-name tuple is not the four-name constant, so it reports green on the precise
defect it was written to catch. The second shows the two new tests are not redundant — test 1
inspects the argument the CLI constructs and is blind to a gate that ignores it.

Not asserted here, and deliberately: `BackendReply.usage.model_id` echoing `target.model_id`
verbatim (`c36160e`, `llm/backends/anthropic.py::_reply_from`). It is the other high-consequence
line BK1 touched, it is already bound by a round-trip test through the real `CachingModelClient`,
and no code path in this ADR reaches it.

---

## ADR-0079 — RESERVED, not yet written

Allocated to §11.5 step-5 subtask 9, "Un-refuse the step-5 flags"
(`docs/superpowers/plans/design-resume-step5.md` row 9): the `--from-phase` / `--repo` /
`--reset-attempts` semantics in `src/fleet/cli.py`'s `_refuse_unbuilt_resume_flags`. Subtask 9 has
not started — it depends on subtasks 7 and 8, neither of which has landed. This number is reserved
and not available for reuse; its absence from this file is not a deletion.

---

## ADR-0080 — RESERVED, not yet written

Allocated to §11.5 step-5 subtask 10, "Step 8 — 'continue': delegate to the three composition roots
in phase order" (`docs/superpowers/plans/design-resume-step5.md` row 10): `fleet resume` without
`--dry-run` running `_transform_impl` → `_build_impl` → `_verify_impl` for the phases the floors
demand. Subtask 10 has not started — it depends on subtask 9, not landed. This number is reserved
and not available for reuse; its absence from this file is not a deletion.

---

## ADR-0081 — RESERVED, not yet written

Allocated to the in-flight §11.5 step-2 orphan-reap lane (subtask 3, worktree/container reap wired
into `_resume_impl`), for its decision to run the reap **after** `_reset_stale_running` rather than
at the position §11.5's own step numbering puts it: reaping before the crash sweep is a no-op on
exactly the crashed runs the reap exists for, because a crashed run's worktree is still claimed by
a `RUNNING` `phases` row until the sweep resets it. That lane has not committed, and this ADR is
its own to land — a draft exists at `.superpowers/sdd/design-resume-step5/adr-0081-draft.md` and
is deliberately left unlanded here. This number is reserved and not available for reuse; its
absence from this file is not a deletion.

---

## ADR-0082 — A demotion's `checkpoints` sweep is **span-wide and conditional on a demotion having actually happened**, and it spares `DEGRADED` alone: the backward walk's hard stops and the sweep's carve-out answer two different questions, so `SKIPPED` stops the walk and still loses its checkpoint — and the residual `DEGRADED` stale-anchor hazard is a **stated boundary**, unreachable only by an induction spread across three modules that nothing binds to this method's signature

**Status:** accepted, describing behaviour already landed on `main`. Anchored at `8c00971` (`main`);
every file:line below is that ref unless another is named. The behaviour is `16879fe`, `5488157`,
`38107e9` and `9e5c093` (`src/fleet/state/repository.py`, `src/fleet/state/checkpoints.py`,
`tests/test_repository.py`); the `SKIPPED` half of the walk this reconciles against is `4a1a184`
and `8c00971` (`src/fleet/orchestrator/reentry.py`). **Supersedes nothing. Extends ADR-0077 §5**,
which decided all three non-demotable statuses for the *status* write and said nothing about the
`checkpoints` row. Recorded late, on a number allocated by the orchestrator: the implementer of
`demote_to_floor` raised the reconciliation as its first concern and deliberately took no number
(`docs/superpowers/plans/design-resume-step5-task6-demotion-writer-report.md`
§3 and §6 item 1).

**Provenance (CLAUDE.md Guardrail 1).** Nothing here is a SPEC requirement that pre-existed it.
`docs/SPEC.md:6907-6909` **as of `8c00971`, before this change** stated the *demoted-rows-only*
reading, and `docs/superpowers/plans/design-resume-step5.md:234` at `b7fc5ec` stated the
*unconditional span* reading; the two could not both be implemented, so §2 below is an **Agent
Recommendation** adjudicating them, and that SPEC sentence is corrected to match in this same commit (Guardrail 7 — the SPEC sentence
that contradicts the code regenerates the defect on the next reconciliation, and it regenerates it
as mutation M11, which the suite pins as failing). The **status** half — that `DEGRADED`, `SKIPPED`
and `REQUIRES_HUMAN_INTERVENTION` are not demotable — is ADR-0077 §5 and is *not* re-decided here.

### 1. The two readings, and why neither is safe alone

`docs/superpowers/plans/design-resume-step5.md:234` **at `b7fc5ec`** dropped the `checkpoints` row for
**every** phase in `floor..4` unconditionally (that lane has since folded this decision back into its
pseudocode at `8c00971:…:235-237`). `docs/SPEC.md` §11.5 step 5 tied the drop to **the
demoted row**. Verified against the code, each alone fails in the opposite direction:

- ***Demoted rows only.*** The span's top phase is frequently not demoted — `phase_floor` selects
  the frontier as the first phase *not* settled (`src/fleet/orchestrator/reentry.py:88-91`), so on
  the ordinary interrupted resume the frontier is `PENDING` or `RUNNING`, i.e. not `SUCCEEDED`, i.e.
  not demoted (`src/fleet/state/repository.py:1424`). Its partial payload therefore survives while
  every phase beneath it is rewritten, and `checkpoints.load()` hands that payload back reporting it
  usable — a VERIFY that resumes against a BUILD output the same transaction discarded.
- ***Unconditional.*** `phase_floor` legitimately returns the frontier itself with nothing below it
  to demote: the backward walk breaks at the first phase in `orchestrator/reentry._HARD_STOPS`
  (`src/fleet/orchestrator/reentry.py:98-99`, tested **before** `evidence` is read) or, failing
  that, at the first phase whose evidence holds (`:100-101`). The second, met immediately below
  the frontier, is what a healthy interrupted run looks like; the first returns the frontier for a
  different reason, and either way there is nothing below to demote.
  Sweeping there deletes the in-progress checkpoint on **every** `fleet resume`, with nothing
  invalidated to justify it.

### 2. The decision

The sweep is **span-wide, conditional on at least one phase actually having been demoted, and
excludes `DEGRADED` rows**. In code: `src/fleet/state/repository.py:1435-1441` — the `if demotions:`
guard is the conditionality, `span` (built at `:1410` as every `Phase >= floor`) is the width, and
the list comprehension's `is not RepoStatus.DEGRADED` filter is the carve-out. The deletion runs
through `checkpoints.delete_in_unit`, which takes a **connection** rather than a `StateWriter`
precisely so the status write and the checkpoint drop cannot become two transactions
(`src/fleet/state/checkpoints.py:138-164`).

A demotion invalidates everything above it; a no-op stays a no-op. The rest of the unit is
unchanged by this ADR and was verified against the code rather than the report:
`attempts` is absent from the demotion `UPDATE`'s SET list (`src/fleet/state/repository.py:822-825`),
the `REQUIRES_HUMAN_INTERVENTION` refusal is re-read inside the transaction (`:1413-1418`), and the
`PhaseDemoted` finding is fingerprinted per **phase**, not per repo (`:840-842`, `:832-837`).

### 3. `SKIPPED` stops the walk and still loses its checkpoint, and the asymmetry is the point

`4a1a184` made `SKIPPED` a hard stop beside `DEGRADED` in the backward walk
(`src/fleet/orchestrator/reentry.py:54`), on the two-sided reading of ADR-0077 §5. That lane's
reasoning is confirmed by reading the loop rather than inherited: an excluded phase never ran, so
`evidence.get(phase, False)` is `False` for it, so without the stop the walk falls through
`:100-102` and sets the floor one phase lower every iteration — every repo with an excluded middle
phase demoted to phase 1 on every resume.

The sweep's carve-out names `DEGRADED` only, and that is **correct, not an oversight the walk
outgrew**. The two mechanisms answer different questions:

- The walk's hard stop protects a **decision**: a budget nobody granted (`DEGRADED`) or an
  operator's config exclusion resume does not re-decide (`SKIPPED`). Both statuses need it.
- The sweep's carve-out protects a **payload a future round will legitimately resume from**. Only
  `DEGRADED` has such a round. `DEGRADED` is deliberately outside `TERMINAL_STATUSES` and keeps a
  live edge to `RUNNING` for the §3.5.1 revalidation round (`src/fleet/models/enums.py:25-29`,
  `:42-46`). `SKIPPED` is inside `TERMINAL_STATUSES` and maps to the **empty** transition set
  (`src/fleet/models/enums.py:25-26`, `:49`), which is what makes it terminal mechanically: no round
  in this run ever re-enters a `SKIPPED` phase, so no round can resume from its checkpoint.

Sparing a `SKIPPED` phase's checkpoint would therefore buy nothing and cost one durable stale
payload. Dropping it with the rest of the span is the safe direction, and **`repository.py` needs no
change on this point.** The open question `docs/superpowers/plans/design-resume-step5.md:258-260`
handed to subtask 6 is answered here: same hard stop, different checkpoint treatment, for the reason
above.

### 4. The residual `DEGRADED` stale-anchor hazard — a stated boundary, with its premises named

**The hazard.** A `DEGRADED` phase inside the span keeps a checkpoint built on output the demotion
below it regenerates, so the revalidation round that eventually re-runs it resumes from a stale
anchor. ADR-0077 §5 forecloses the obvious fix (dropping that checkpoint forces the budgeted round
to start from nothing, which is the cost the budget was sized against), so this is a real tension
that is recorded rather than resolved. It is stated in the method docstring at
`src/fleet/state/repository.py:1392-1401`.

**Ruling under CLAUDE.md Rule 12's stop rule: adversarial-only — a stated boundary, deliberately not
patched.** The state the hazard needs is a `DEGRADED` row *strictly above the frontier*, and that is
not a state the machine produces:

1. A `DEGRADED` row at phase `p` implies `p` ran, which implies every phase below `p` was
   `SUCCEEDED` at that moment (the phase ladder is ordered).
2. `ALLOWED_TRANSITIONS[SUCCEEDED]` is the **empty set** (`src/fleet/models/enums.py:47`); ADR-0077's
   `RESUME_DEMOTE` is the only door out of it, i.e. `demote_to_floor` itself.
3. So for a phase below `p` to be unsettled — which is what puts the frontier below `p` and `p` in
   the span — a prior `demote_to_floor` must already have run on this repo in a state that itself
   required a `DEGRADED` row above its own frontier. The induction has no base case.

A `DEGRADED` row *below* the frontier is common and harmless: the backward walk breaks on it without
moving the floor onto it (`src/fleet/orchestrator/reentry.py:98-99`), so it lands below the floor and
outside the span entirely. The same induction disposes of `SKIPPED`: its only entry edges are from
`PENDING` and `BLOCKED`, never from a completed phase (`src/fleet/models/enums.py:33-34` and `:41`,
and the driver's own gate records the same at `src/fleet/cli.py:2007-2010` at `8ea1881` — cite the
ref, a sibling lane has uncommitted edits to that file), so a phase marked `SKIPPED` never ran and
never wrote a checkpoint to sweep. `SKIPPED` is written to phase 1 by the manifest `skip: true` gate
and the empty-repo gate (`src/fleet/cli.py:1993-1994` and `:2022-2023` at `8ea1881`), but **not only**
to phase 1: `fleet quarantine` writes `SKIPPED` to every non-terminal phase of the target repo, not
just phase 1 — it selects all non-terminal phase rows and runs the same `transition` gate over each
(`src/fleet/cli.py:9710-9716` at `8ea1881`), then writes `SKIPPED` to all of them in one statement
(`:9754-9759`); the phase-1 `INSERT` (`:9763-9769`) is a fallback used only when the repo has no
phase rows yet.

> **Editorial correction (2026-08-20).** This paragraph originally said `SKIPPED` "is written only to
> phase 1," citing `src/fleet/cli.py:9764-9767` at `8c00971` — but that citation was only the
> phase-1 fallback branch of `fleet quarantine`'s write; it missed the `executemany` immediately
> above it (`:9754-9759` at `8c00971`, unmoved at `8ea1881`) which writes `SKIPPED` to *every*
> non-terminal phase `transition()` clears, not just phase 1. Verified at `8ea1881` (`main`) via
> `git show main:src/fleet/cli.py`, since a sibling lane has this file dirty in the working copy.
> **The paragraph's conclusion is unaffected.** The induction it draws never rested on "phase 1
> only" — it rests on `SKIPPED`'s entry edges being `PENDING`/`BLOCKED`-only, which remains true
> and unamended, and on §2's carve-out excluding only `DEGRADED` from the sweep (a `SKIPPED` row is
> swept regardless of which phase it sits at). Since a phase that transitions to `SKIPPED` was never
> `RUNNING`, it never held a checkpoint to begin with, at phase 1 or any other phase — so the
> corrected premise supports the same disposal it was cited for. §4's actual subject, the residual
> `DEGRADED` stale-anchor hazard, does not depend on this `SKIPPED` aside at all; that conclusion
> survives unchanged.

**And here is the disclosure that keeps this an honest boundary rather than a convention in a
mechanism's clothes.** That induction is spread across three modules — the ladder's ordering in
`orchestrator/runner.py`, `ALLOWED_TRANSITIONS` in `models/enums.py`, and the hard stop in
`orchestrator/reentry.py` — and **nothing binds any of it to `demote_to_floor`'s signature**, which
accepts any `Phase` as `floor` from any caller (`src/fleet/state/repository.py:453-461`). The
carve-out list is computed from the rows as read, not from a validated floor. The coupling
"`floor` is what `phase_floor` computed" is a **docstring sentence** (`:1351-1352`), not a check.
`demote_to_floor` has no production caller yet — subtask 7 is the first — and the tests that exercise
the carve-out hand in floors directly (`tests/test_repository.py:1384-1386` builds a `BUILD`-DEGRADED
repo and passes `floor=Phase.TRANSFORM`, a floor `phase_floor` would never return for those rows,
which is why the carve-out is testable at all).

So: a normal author writing subtask 7 against `phase_floor` cannot reach the hazard, which is the
Rule 12 test and why it is not patched. An author who computes a floor some other way can, and the
only thing that would tell them not to is prose. That is stated, not closed.

### 5. What this ADR does not do

- It does not re-decide ADR-0077 §5's status rules, and it adds no `RESUME_DEMOTE` key.
- It does not patch the §4 hazard, and it does **not** claim the docstring coupling in §4 is
  enforcement. If subtask 7's floor ever comes from anywhere but `phase_floor`, §4's premises must be
  re-derived, not re-read.
- It allocates no D-number. The §4 boundary is a disclosure, not a ledger defect, and central number
  allocation belongs to the orchestrator (CLAUDE.md §3).

### 6. Verification

No new behaviour is introduced by this ADR, so there is no mutation to run for it. What was checked
before it was written, against `8c00971` rather than against the implementer's report:

| Claim | Checked at |
|---|---|
| sweep is conditional on a demotion | `src/fleet/state/repository.py:1435` (`if demotions:`) |
| sweep is span-wide, not demoted-rows | `:1440` iterates `span`, not `demotions` |
| carve-out names `DEGRADED` only | `:1440` — `SKIPPED` is absent from the filter |
| `attempts` retained on every path | `:822-825`; `complete_phase` is the only other writer |
| `SKIPPED` is mechanically terminal | `src/fleet/models/enums.py:25-26`, `:49` (empty set) |
| `DEGRADED` is not terminal and reaches `RUNNING` | `src/fleet/models/enums.py:25-29`, `:42-46` |
| walk stops on both, floor never lands on either | `src/fleet/orchestrator/reentry.py:54`, `:98-99` |
| `demote_to_floor` has no production caller | `grep -rn demote_to_floor src/` → the definition and the Protocol only |

The behaviour itself is bound by mutations M10 (unconditional sweep → the no-op test fails) and M11
(narrowed to the demoted rows → the frontier-checkpoint test fails), recorded in
`docs/superpowers/plans/design-resume-step5-task6-demotion-writer-report.md` §5.

---

## ADR-0083 — The LLM tier ceiling gets a hand-rolled, counting `ResizableLimiter` in `budgets.py`, not `anyio.CapacityLimiter`: the borrowed primitive is per-borrower and undeclared, and this commit is a runtime no-op until a later subtask calls `resize()`

**Status:** accepted, describing behaviour already landed on `main`. Anchored at `27cb03b` (`main`);
every file:line below is that ref. The behaviour is `a3ff0ae` and `431b02f`
(`src/fleet/orchestrator/budgets.py`, `tests/test_budgets.py`). **Supersedes nothing.** Recorded
late, on a number allocated by the orchestrator: the implementing lane (R1 of the §5 rate-limiting
decomposition) raised the need for an ADR as its first concern and deliberately took no number
(`.superpowers/sdd/design-resume-step5/task-rl1-report.md` §4 item 1).

**Provenance (CLAUDE.md Guardrail 1).** The *requirement* is SPEC, not an agent's invention:
`docs/SPEC.md:7164-7169` already states that rate limiting is backpressure and that the owning
tier's LLM semaphore is AIMD-adjusted — halved on a 429 or a `retry-after`, one slot returned per
clean minute, bounded by `aimd.floor` and `concurrency.llm.*`. What is an **Agent Recommendation**
is everything below this line: the choice of primitive (hand-rolled vs. `anyio.CapacityLimiter`),
its shape, and the decision not to declare `anyio` as a dependency. It originates in
`docs/superpowers/plans/rate-limiting-scope-research.md` §4 (Q3, "the resize problem") and was
accepted by the orchestrator via the implementing lane's brief. Nothing here should be cited as a
SPEC requirement.

### 1. The decision

`ResizableLimiter` (`src/fleet/orchestrator/budgets.py:952-1075`) is a counting concurrency
primitive over an `int` capacity, an `int` borrowed count, and a `deque[asyncio.Future[None]]` of
waiters, with `acquire` / `release` / `resize` / `locked` / `capacity` / `borrowed` /
`__aenter__` / `__aexit__`. `Limits.llm` is retyped `Mapping[ModelTier, ResizableLimiter]` and
`Limits.for_tier` returns one (`:1089`, `:1120-1122`); `Limits.create` builds a `ResizableLimiter`
per tier where it previously built an `asyncio.Semaphore` (`:1105-1109`). The per-tier arithmetic —
`max(1, min(configured, override))` — is untouched. No wiring, no controller, no config read: the
class reads no signal and decides no policy, exactly as the research document's Q3 recommendation
scoped it.

Because the class is **counting, not per-borrower**, `workers/classify.py`'s `async with
ctx.limits.for_tier(tier)` needed no change — the drop-in property the research document names as
Option C's third reason (rate-limiting-scope-research.md §4.3 item 3).

### 2. The rejected alternative: `anyio.CapacityLimiter`

Measured under `.venv/bin/python` (the interpreter that runs the harness), `find_spec` question:
`anyio` **is installed** (4.14.2), and its `CapacityLimiter.total_tokens` is a settable property
that supports exactly the resize-while-held semantics §11.8 wants — shrinking below the current
borrowed count is legal and safe, draining to the new ceiling rather than raising.

Two measured properties disqualify it for this codebase rather than merely make it less convenient:

- **Undeclared.** `pyproject.toml`'s `project.dependencies` (`:27-40`) lists `anthropic>=0.69` and
  `openai>=1.60`, each with a per-line justification comment naming the file that needs it; `anyio`
  is not there — it arrives transitively through `anthropic`/`openai`/`httpx`. It also has zero
  existing imports anywhere in `src/` or `tests/` (verified: `grep -rn anyio src/ tests/
  pyproject.toml` → no hits). Adopting it for this one class means declaring a new runtime
  dependency and importing a second concurrency vocabulary into a tree that is otherwise uniformly
  bare-`asyncio`.
- **Per-borrower, not counting.** A task that acquires an `anyio.CapacityLimiter` it already holds
  raises `RuntimeError`; `asyncio.Semaphore` (and `ResizableLimiter`) permit it and simply consume
  two slots. §5's R4 (widening acquisition to a single choke point in `LadderModelClient`) is a
  separate, later subtask from R1; a per-borrower primitive would force R1 and R4 to land as one
  atomic change, because `workers/classify.py`'s existing acquisition would raise the moment a
  second acquisition site opened above it. A counting primitive lets the two changes be reviewed
  separately.

Under CLAUDE.md Rule 2 (simplicity first, no dependency for what a few dozen deterministic lines of
code can do) and Rule 5 (code, not judgment, for deterministic mechanics), a hand-rolled limiter in
the module that already owns `Limits` and its ceilings (`budgets.py`'s own header names it
"semaphores and the reserve-then-spend cost policy") was preferred over a new, undeclared,
semantically-mismatched dependency. Two further options the research document measured and rejected
before reaching this one: rebuilding/swapping the semaphore object on every resize (loses the
ceiling for the duration of any call already holding the old object, since `for_tier` hands out the
live reference); and declining to make the semaphore resizable at all, which would require rewriting
SPEC §11.8's AIMD sentence rather than implementing it. Neither is what landed.

### 3. Two things stated honestly rather than left implicit

**R1 is a runtime no-op today, by design and only by design.** No call site anywhere in `src/`
invokes `resize()` — the research document scoped R1 to the primitive alone, with the controller
(R5) and the widened acquisition point (R4) left to later subtasks. Behaviour today is therefore
identical to the `asyncio.Semaphore` it replaced. **The implementing lane's own recommendation,
repeated here because it is a real operational risk and not merely a caveat:** if the orchestrator
takes the shorter R2+R3-only path — closing §13 row 43's actual disaster (a throttled account
misread as `DOWN`) without ever building the AIMD controller — this commit buys nothing, and per
Option D of the research document (§4.2), the correct response is to **revert `a3ff0ae` and
`431b02f`**, not leave `ResizableLimiter` in the tree as dead code with SPEC §11.8's AIMD sentence
rewritten around it. A primitive with no caller and no plan to gain one is exactly the shape of
cruft CLAUDE.md's Rule 2 exists to prevent.

**Open assumption: the ceiling defaults to the starting capacity, and `Limits.create` passes no
explicit `ceiling=`.** `ResizableLimiter.__init__` (`:988-1006`) takes `floor: int = 1` and
`ceiling: int | None = None`, defaulting the ceiling to the constructor's `capacity` argument when
omitted (`:999`); `Limits.create`'s dict comprehension (`:1105-1109`) constructs
`ResizableLimiter(max(1, min(concurrency.llm.for_tier(tier), overrides.get(tier, 1 << 30))))` with
no `ceiling=` argument at all. The practical effect: whenever `llm.concurrency_overrides` has
**lowered** a tier below its configured `concurrency.llm.*` value, that lowered figure becomes the
ceiling, and no later `resize()` call can grow the tier back past it — a `resize()` targeting the
unoverridden configured value simply clamps at the override. This differs from what the research
document's R5 row states literally: it describes the controller clamping to
`[aimd.floor, concurrency.llm.for_tier(tier)]`, which would let a controller grow a throttled-then-
recovered tier back past an operator's override, undoing "run this slower". The assumption made
here — an override is a ceiling, not merely a starting point, because §11.8 itself names "run this
slower" as the intended response to throttling — was not imposed on R5's text; `Limits.create`
(a file this ADR's implementer owns) was left as the load-bearing choice, and `ceiling=` exists as
a constructor argument precisely so the R5 implementer can pass an explicit value if they disagree.

### 4. What this ADR does not do

- It does not decide R4's, R5's, R6's or R7's design (widened acquisition, the AIMD controller, the
  per-target token bucket, or the `DOWN`-vocabulary fix); each is a separate §5 subtask.
- It does not adjudicate the research document's §6.3 sizing recommendation (dispatch R2+R3 before
  R1+R4) — that is the orchestrator's call, not this ADR's.
- It does not sweep the stale `for_tier` docstring claim ("the limiter every `ModelClient.complete`
  on this tier must hold" — true of one of the tree's five callers) or the `workers/classify.py`
  acquisition site; both are R4's, deliberately, so a narrowing now would be un-narrowed one
  subtask later.

### 5. Verification

`tests/test_budgets.py`: 29 passed (28 before this change, 9 added, the one pre-existing
`for_tier`-keyed-by-tier test untouched and still green). Seven discriminating mutations, each
applied to the committed tree, confirmed to change the file (`git diff --numstat`), and reverted
byte-for-byte:

| Property mutated | Target test that fails under it |
|---|---|
| admission bound (`locked()` short-circuited to always-false) | `…never_admits_more_than_capacity_under_contention` |
| `resize` clamps upward only | `…shrinking_while_slots_are_held_bars_entrants_and_harms_no_holder` |
| `resize`'s wake loop dropped | `…growing_admits_parked_waiters_without_waiting_for_a_release` |
| no clamp / no zero-refusal | `…resize_clamps_to_floor_and_ceiling_and_refuses_zero` |
| waiters admitted LIFO | `…waiters_are_admitted_in_arrival_order` |
| cancellation recovery dropped | `…a_waiter_cancelled_after_being_woken_hands_its_slot_on` |
| slot charged at resume, not at wake | `…a_freed_slot_is_charged_at_wake_not_when_the_waiter_resumes` |

The last is the one worth naming: on first run it did not discriminate, because the woken tasks are
normally scheduled ahead of any later arrival and the over-admission race rarely opens; `431b02f`
adds a test that steps an arrival's `acquire()` coroutine by hand (`send(None)`) to force the two-
slots-freed-back-to-back window open without awaiting past it, and only then does the mutation fail
the target test. `mypy --strict` and `ruff check` clean on both files. Scoped run (not the full
suite): `tests/test_budgets.py tests/test_workers_scan.py tests/test_runner.py
tests/test_scan_e2e.py` — 116 passed, `0` skipped.

---

## ADR-0084 — `ResizableLimiter`'s ceiling check moves **inside** the admission gate: the charge/check split had already cost one over-admission, and the AIMD subtask adds callers who would each owe the same forgotten precondition

**Status:** accepted; the behaviour is `99862a9` (`src/fleet/orchestrator/budgets.py`,
`tests/test_budgets.py`). Every file:line below is that ref unless another is named.
**Supersedes nothing.** ADR-0083 chose the primitive and described its surface; this ADR changes
one private detail of that primitive's interior and leaves its public surface — `acquire` /
`release` / `resize` / `locked` / `capacity` / `borrowed` / `__aenter__` / `__aexit__` — exactly as
ADR-0083 records it. ADR-0083's citations are anchored at `27cb03b` and are unaffected by the line
movement here.

**Provenance (CLAUDE.md Guardrail 1).** The defect is fact: `d44b94f`. The *shape* concern is an
**Agent Recommendation**, raised as the first concern of the lane that fixed the defect
(`.superpowers/sdd/design-resume-step5/task-limfix-report.md` §7 item 1), which deliberately did
not act on it and asked for an explicit ruling before R5 lands. Nothing below is a SPEC
requirement; SPEC §11.8 requires an AIMD-adjustable ceiling and says nothing about how the
primitive is factored internally.

### 1. The shape, and why it was a defect generator rather than one defect

Before this change the class had two charge sites. `acquire`'s fast path charged inline, one line
under its own `locked()` test, so the check could not drift away from the charge. `_wake_next`
charged **unconditionally** and left the ceiling check to whoever called it — a contract carried
in prose, owed by three call sites, and enforced by nothing.

Two of the three paid it. `release` tested `_borrowed < _capacity` before calling; `resize` folded
the same test into a `while … and _wake_next()` loop. `acquire`'s cancellation recovery did not,
and a `resize` down landing between a waiter's wake and its resume left `_borrowed` above
`_capacity` while the recovery handed the slot on anyway — two calls in flight against a ceiling of
one, i.e. a cancellation silently undoing the throttle that a 429 had just imposed.
`d44b94f` fixed that caller by adding the missing test.

**The fix was correct and the shape was not.** A guard added to one caller leaves the next caller
owing the same unenforced debt, and the debt is about to be taken on: R5 (the AIMD controller) is
the subtask that will call `resize()` on a live limiter for the first time, and the research
decomposition assigns it new paths that create headroom
(`docs/superpowers/plans/rate-limiting-scope-research.md:433-436`). Under CLAUDE.md Rule 12's stop
rule the question is whether a *normal* author trips the escape, not an adversarial one. Here the
answer is that a normal author already did, on the first three tries, in code reviewed by two
lanes — which puts this on the accidentally-reachable side of the rule, not the stated-boundary
side.

### 2. The decision

`_wake_next` is replaced by `_drain` (`:1074-1095`), which performs the ceiling test itself and
charges only inside it:

- The charge at `:1091` is reachable only under the `while self._borrowed < self._capacity` test at
  `:1088`. Calling `_drain` from a state with no headroom — `_borrowed` sitting above `_capacity`
  after a shrink — admits nobody and charges nothing, rather than being undefined behaviour the
  caller was supposed to have prevented.
- All three call sites become a bare `self._drain()`: the cancellation recovery at `:1052`,
  `release` at `:1060`, `resize` at `:1072`. None of them tests the ceiling, because none of them
  can usefully have an opinion about it.

The gain is that a caller now states an **intent** — "admit whoever fits now" — where it used to
state a **precondition**. A precondition can be stated wrongly; an intent cannot. Note this closes
the hazard in both directions, which the old shape did not: the old contract could be underpaid
(the defect) *and* overpaid (a caller writing one `_wake_next()` where the headroom it created was
two slots would silently under-admit and leave a waiter parked against real headroom). The loop
lives inside the gate now, so neither is expressible.

### 3. Why this is not a behaviour change at any of the three sites

The brief for this change required all three to keep their current behaviour, so this was checked
rather than assumed.

- **`resize` (`:1072`).** Exactly equal. The old line was
  `while self._borrowed < self._capacity and self._wake_next(): pass` — the same test around the
  same charge, in the same order. `_drain`'s loop is that loop with the test moved one frame down.
- **`release` (`:1060`) and the cancellation recovery (`:1052`).** Both previously woke *at most
  one* waiter under an `if`; `_drain` loops. The loop still transfers exactly one, because both
  paths free exactly one slot: whenever live waiters exist, `_borrowed` was at or above `_capacity`
  before the decrement, so the first charge restores `_borrowed == _capacity` and the loop's test
  fails on its second pass. For the loop to wake a second waiter, the limiter would have to be
  holding parked waiters while two or more slots of headroom existed — a state no path produces,
  since every path that creates headroom drains synchronously before returning. Should such a state
  ever arise, waking into it is the correction, not a regression: it is the invariant being
  restored, not a ceiling being exceeded.

Neither of the two properties a reviewer previously established is disturbed:

- **FIFO is intact.** `_drain` still scans `_waiters` from the head and still skips `done()`
  futures (`:1089-1090`), so a waiter already chosen is never chosen twice; the removal from the
  deque still happens in the waiter's own `finally` (`:1043`), which still runs before the `except`
  (`:1044`), so a recovering waiter cannot re-select its own future.
- **No `await` was introduced between creating headroom and charging it.** `_drain` is a plain
  `def` containing no `await`, and each caller adjusts `_borrowed` or `_capacity` on the statement
  immediately above its `_drain()` call. The fast path therefore still cannot barge headroom that
  a drain is in the middle of handing out.

Rule 2 was the live alternative here — the honest option was to record "the shape cannot be
improved for less complexity than it saves" and stop. It was not taken because the change *removes*
code rather than adding it: three caller-side ceiling tests collapse into one, no new method,
no new state, no new argument, and the `bool` return value that only `resize`'s loop consumed is
gone with it.

### 4. What a future caller may now rely on

Stated as a contract, because R5's author is the next reader:

1. `_drain()` is safe to call from **any** state of the limiter, including one where `_borrowed`
   exceeds `_capacity`. It never charges a slot the current ceiling has no room for.
2. One call drains as far as the ceiling allows. A caller that creates several slots of headroom
   at once — a `resize` up by three — needs one `_drain()`, not three.
3. Withholding a slot strands nothing. If a drain declines to admit, the next `release` or
   `resize` re-drains once the headroom is real.
4. **A caller must not charge `_borrowed` itself.** This is the one obligation that survives, and
   it is the only one, so it is enforced mechanically rather than by this sentence — see §5.

### 5. Verification

Scoped run, `tests/test_budgets.py`: **33 passed** (31 before, 2 added). `mypy --strict` — the
configured gate, `packages = ["fleet"]` — **no issues found in 115 source files**, identical before
and after. `ruff check` on both files leaves one `E501` at `tests/test_budgets.py:1051`, which is
**pre-existing**: the same line is flagged at `HEAD` before this change (verified by piping
`git show HEAD:tests/test_budgets.py` through `ruff --stdin-filename`, so the repo's own config
resolves). `tests/test_budgets.py` is outside the strict gate; checked directly it carries 4 errors
against 6 on the same file at `HEAD`, the two removed being those of the deleted instrument.

**The instrument, and the near-miss that justifies its shape.** The fuzz's previous instrument
subclassed the limiter and overrode `_wake_next`. When that method was folded into `_drain`, the
override stopped being called — and the fuzz went on **passing**, recording nothing, on a run where
the whole class had just been rewritten under it. A detector that cannot fire is indistinguishable
from a clean result, which is why CLAUDE.md Guardrail 6 asks for the known-bad state before the
clean one. It is replaced by `_watch_admissions` (`tests/test_budgets.py:753-803`), which hooks the
loop's `create_future` so that each waiter's future records `(borrowed, capacity)` at the instant
its slot is charged and the future settled.

*What that quantity is, and whether it can see this defect class.* It is the **admission event**:
the ceiling in force at the moment a parked waiter is handed a slot. It is deliberately **not**
`borrowed`, which is blind here — a cancelled waiter hands on a slot that was already charged, so
the count reads 2 either side of a breach against a ceiling of 1. It is also not measured at the
waiter's *resume*, which would report a false breach whenever a legitimate wake is followed by a
shrink before the waiter runs. Settling the waiter's future is the one step any shape of this class
must take, so the instrument survives both the refactor and a mutation that reverses it — which is
what lets one instrument judge both states. A second, independent instrument covers the obligation
in §4 item 4: `test_only_the_fast_path_and_the_drain_may_charge_a_slot` (`:1121-1185`) parses the
class's AST and asserts that an increment of `_borrowed` appears in exactly `{acquire, _drain}` and
a rebinding in exactly `{__init__}`. That is the whitelist form CLAUDE.md Rule 12 asks for in place
of a blacklist of forbidden sinks: a new charge site trips it whether or not its author invented a
form nobody predicted.

Validated on four states before any clean result was trusted. Each mutation was applied by script
to the committed tree, asserted to have changed the file (byte deltas below, `assert text != before`
before the write), and restored from a byte-for-byte backup verified with `git diff --stat`:

| State | Mutation | Result |
|---|---|---|
| **M1 — the shape alone** | `_drain` charges one slot unconditionally; `release` and `resize` guard their own calls, i.e. `d44b94f`'s *behaviour* with the check back outside (+83 bytes) | **32 passed, 1 failed.** Only `test_a_drain_from_a_full_ceiling_admits_nobody_so_no_caller_needs_a_guard` fails: `the gate charged a slot it had no room for: [(3, 1)]` |
| **M2 — literally pre-`d44b94f`** | M1 with the cancellation recovery left unguarded (+27 bytes) | 3 failed: the new guarantee, `d44b94f`'s deterministic test, and the fuzz — which fires **71 events across 68 of 400 seeds** |
| **M3 — synthetic fault, fresh** | an extra unguarded charge added to `release` on the *fixed* class (+160 bytes) | fuzz fires **3,531 events, 400/400 seeds**; the AST whitelist also fires |
| **M4 — the whitelist's own escape** | `self._borrowed = self._borrowed + 1` in `release`, dodging the `AugAssign` test (+44 bytes) | the whitelist's second assertion fires: `_borrowed` is rebound outside the constructor |

**M1 is the discriminating one, and it is the Rule 12 shape.** It is behaviourally identical to the
code that shipped at `d44b94f`, so every pre-existing test passes under it — including both tests
`d44b94f` itself added, which walk the exact path the defect took. What fails is only the new
assertion, on the only thing that actually changed: whether the gate refuses on its own account or
trusts its caller to have checked. M2 is not discriminating and is not offered as such; it is there
to put the new instrument against the original known-bad state.

**The instrument's numbers reproduce the predecessor's exactly.** M2 gives 71 events over 68 of 400
seeds and M3 gives 3,531 events over 400 of 400 — the same figures the previous lane measured with
a completely different hook (`task-limfix-report.md` §4.2). Two independent instruments agreeing to
the event on the same seeded states is a stronger result than either alone, and it is the reason
the 0/400 clean reading on the shipped code is offered as evidence rather than as an absence.

One further honest note on that clean reading: with the check inside the gate, the fuzz's
`borrowed > capacity` condition cannot fire through `_drain` **by construction**. Its value is
therefore not that it re-proves the ceiling arithmetic, but that it would catch a charge reaching a
waiter by any route that bypasses the gate — which is exactly the residue §4 item 4 and the AST
whitelist exist to cover.

### 6. What this ADR does not do

- It does not change `ResizableLimiter`'s public surface, its constructor arguments, or the
  `floor` / `ceiling` clamping semantics ADR-0083 §3 records; `Limits.create` is untouched.
- It does not decide R5's design. It only removes one way for R5 to be written wrongly.
- It does not re-open ADR-0083's open assumption about the ceiling defaulting to the starting
  capacity. That is still R5's to accept or override with an explicit `ceiling=`.
- It allocates no D-number. This is a shape hardened before it produced a second defect, not a
  ledger entry; the defect it descends from was fixed at `d44b94f` and needs no new record.
- It leaves the pre-existing `E501` at `tests/test_budgets.py:1051` alone (CLAUDE.md Rule 3 — it is
  not this change's mess), and reports it here rather than editing a correct line into scope.
