# Security Review Notes — Fleet Engine Migration Harness

Ad hoc investigation log, kept outside `docs/` on purpose: this is not a SPEC/ADR/D-number
artifact under CLAUDE.md's SDD protocol — it's a running record of security questions being
asked about the harness and what the source actually shows, so each item can be revisited and
closed out (gap vs. intentional) as the review continues.

Each entry: what was found, where, why it might matter, and its current status.

---

## Open Items

### 7. CRITICAL — a source repo can commit a symlink to an arbitrary host path; the rewrite worker
   dereferences it and ships the target file's content to a third-party LLM (subagent-found,
   personally re-verified)

**Where:**
- `src/fleet/workers/rewrite.py:452`: `source = (root / unit).read_text(encoding="utf-8")`.
- `src/fleet/workers/rewrite.py:804`: `"current_content": source` in `_evidence()` (already
  established under item #4).
- `src/fleet/cli.py:6233` (`_tracked_at()`, `git ls-tree -r -z --name-only` — confirmed under item
  #2 to have zero file-type filtering).
- `src/fleet/rewrite/rules.py` (`rule_matches_path()`, ~lines 213-218) and `rewrite.py`'s
  `_targets_are_present()` (~line 1010): neither checks file type before treating a path as an
  ordinary file.
- `src/fleet/workers/rewrite.py` itself: no `container`/`sandbox`/`docker` reference anywhere in
  the file (confirmed by grep) — Phase 2 (transform/rewrite) is not run inside the `--network=none`
  Docker isolation that SPEC documents for Phase 4 (`buildverify.py`) only.

**What was found, and personally re-verified.** This connects three previously-separate threads
from this review into one live exploit chain:

1. **Item #2** already established that the relocation domain (`_tracked_at()`) applies no
   file-type filter — a tracked symlink is indistinguishable from an ordinary file in that listing.
2. Git's own well-documented behavior (not specific to this codebase): a tracked symlink's blob
   content **is** its target path string, verbatim; `git checkout`/`git worktree` recreates it as a
   real OS-level symlink with **no restriction on what it may point to** — an absolute host path,
   a path outside the repo, anywhere the checking-out process has filesystem permission to reach.
   This is a well-known class of git-symlink hazard in tools that process untrusted repositories.
3. **Confirmed personally:** `rewrite.py:452`'s `source = (root / unit).read_text(encoding=
   "utf-8")` — the read that produces the `"current_content"` sent to an LLM whenever a
   deterministic `ast-grep` rule fails to transform a file — performs an ordinary `Path.read_text()`
   with **no** `is_symlink()` check beforehand. Python's `read_text()` follows a symlink exactly
   like the OS does. The nearby presence-guard `_targets_are_present()` also uses `.is_file()`,
   which likewise **follows** symlinks and returns `True` for one pointing at a real file — so it
   provides no protection either.
4. **Confirmed personally:** `rewrite.py` has zero container/sandbox references — Phase 2 runs
   directly against a git worktree, not inside the `--network=none` Docker isolation SPEC documents
   only for Phase 4's build verification (`sandbox/container.py`, `buildverify.py`). Whatever
   filesystem access the harness process itself has on its host is, in principle, reachable this
   way.

**The resulting chain:** a malicious (or compromised) source repo commits a tracked symlink — at a
path a configured `RewriteRule` targets — pointing at an absolute host path the harness process can
read (a credentials file, an SSH key, an environment file, a config secret — whatever the
process's own file permissions allow). When that rule's deterministic transform doesn't apply
cleanly (a normal, expected occurrence this ladder is specifically designed to handle by escalating
to an LLM), the worker reads the **symlink target's** content instead of repo content, and — per
item #4's already-confirmed gap — ships it **unredacted** to whichever third-party LLM backend is
configured. **This requires no compromised or steered LLM at all** — unlike item #6, the exploit is
entirely on the input side; the LLM is just the exfiltration channel.

**Status:** OPEN. This is the most severe confirmed finding in this document: it does not depend on
a hypothetical prompt-injection-steered model response (item #6) or a downstream disclosure gap
(item #4/PR-body) alone — it's a complete, self-contained arbitrary-host-file-read-and-egress
primitive triggerable purely by the content of a repo being migrated.

**What was also confirmed SAFE, for calibration:** the relocation mechanism itself (`relocate.py`'s
rename-patch construction, a pure `git apply` rename) is inert with respect to symlink targets — it
moves the symlink object, never dereferences it. And Phase 3's BUILD-generation path
(`buildgen.py`/`ecosystems/*.py` consuming `BuildUnit.srcs`) only ever manipulates path *strings*
into generated `BUILD.bazel` text — zero `read_text`/`open` calls found there. The dereference is
specific to Phase 2's rewrite/repair worker, not a property of the whole pipeline.

**Questions to chase:**
- What filesystem permissions does the harness process actually run with in a real deployment —
  is this a realistic host-file-read primitive (process runs with broad read access) or a narrow
  one (process is itself already sandboxed at the OS/container level one layer up from what this
  review has inspected, e.g. `RewriteWorker` tasks dispatched inside their own per-task container)?
  This review has only inspected the code, not a real deployment's process/container topology.
- Does `require_free_space`/other pre-read checks in `rewrite.py` incidentally already reject a
  symlink for an unrelated reason (e.g. `os.stat` following the link into a filesystem the free-
  space check can't traverse)? Not traced.

**On remediation (not applied, offered by the investigating subagent):** add an `is_symlink()`
check (which does not follow the link) immediately before the read in `rewrite.py`, refusing with a
named, loud failure class rather than silently dereferencing — capability-preserving, since no
legitimate rewrite target should ever need to be a symlink to begin with.

> **Status update (2026-09-15, round VII task 1).** FIXED (`e5be438`). `RewriteWorker.run()` now
> checks `.is_symlink()` immediately before the read (which does not itself follow the link) and
> refuses with a named, non-retryable `FailureClass.PREFLIGHT` failure via the worker's existing
> `_failed()` convention — no silent skip, no fallback content source. `_targets_are_present()` was
> also fixed so a symlinked unit no longer counts as present under `.is_file()`. Two new tests in
> `tests/test_workers_transform.py` drive the real `RewriteWorker` against a real OS-level symlink
> committed into a real git repo, pointing outside the fixture's own tree; RED/GREEN verified by
> reverting the fix. **Status: FIXED.**

---

### 6. CRITICAL — an LLM-controlled rule name is rendered unescaped into generated Starlark that
   `bazel build` will evaluate — a genuine code-injection sink (subagent-found, personally traced
   end-to-end and confirmed)

**Where:**
- `src/fleet/bazel/generators.py::render_target()`, line ~103: `lines = [f"{target.rule}("]`.
- `src/fleet/models/build.py:73`: `BuildTarget.rule: str = Field(min_length=1, description="e.g.
  java_library, ts_project, go_test, filegroup")` — a free string, no `Literal`, no pattern, no
  validator anywhere in the class.
- `src/fleet/llm/schemas.py:244`: the LLM-facing proposal schema field — also `rule: str =
  Field(min_length=1, max_length=100)` — free text, length-capped only.
- `src/fleet/workers/buildgen.py::_author()`, lines ~504-515 (read and confirmed personally in an
  earlier part of this same review session): `rule=proposed.rule` — the LLM's raw string copied
  directly into `BuildTarget.rule` with no transformation.

**What was found, and personally re-traced end-to-end.** Every other place in this codebase where
an LLM's output selects a fixed construct is deliberately constrained to a closed set —
`VersionConflictResolution.mechanism` (`llm/schemas.py:281-285`) uses `pattern=r"^(bazel_dep|
single_version_override)$"`, with its own description stating outright: *"A closed pattern, not
free text: these are the only two `bazel/module.py` knows how to emit."* `BuildTarget.rule` — the
literal Bazel rule keyword written as the head of a generated function call in `BUILD.bazel` — has
no equivalent constraint anywhere in its chain, despite being exactly the same *shape* of problem:
an LLM-controlled string that becomes a Bazel-evaluated code construct.

**The full, live chain** (§3.3 step 2's `build_authoring` escape hatch — the rung `buildgen.py`
reaches when no deterministic ecosystem-adapter template covers a target, SPEC §3.3 step 2):

1. `_author()` calls `author_build_file(ctx.llm, evidence, budget=ctx.budget)` — a real, wired LLM
   call, not a dead/unwired path (unlike `manifest_extract` in item #4).
2. Its response's `targets` (typed `ProposedBuildTarget`, `llm/schemas.py:244`'s `rule: str`) are
   copied directly: `BuildTarget(..., rule=proposed.rule, ...)`.
3. `render_build_bazel()` → `render_target()` renders `f"{target.rule}("` — the LLM's string,
   **verbatim, with zero escaping** — as the opening of a Starlark function call.
4. `buildgen.py::_write()` writes this text straight to a real `<dest>/BUILD.bazel` in the
   monorepo working tree.
5. The sandboxed `bazel build`/`bazel test` step (§3.3 step 4) then **evaluates this file as
   executable Starlark**.

If an LLM response ever contained a `rule` value shaped like
`"filegroup(name = \"x\", srcs = [])\nload(\"@evil//:x.bzl\", \"y\")\n#"` (or any string breaking
out of the intended `<rule>(` call head), that text is written into a real `BUILD.bazel` and
evaluated by Bazel with no code-side check ever having looked at it. This is a genuine
Starlark-injection primitive, not a hypothetical — Bazel evaluates `BUILD.bazel`/`MODULE.bazel` as
real Starlark at build/analysis time, so an unescaped string reaching that file is a code-execution
surface in the same family as SQL injection or template injection, scoped to whatever Starlark can
do (which includes `load()`ing arbitrary `.bzl` files and running arbitrary build actions).

**Contrast confirmed safe, for calibration — not every LLM-to-generated-file path has this gap:**
`bazel_dep`/`single_version_override` values, and every `BuildTarget` **attribute value** (`srcs`,
`deps`, `attrs`, `visibility`, etc.), go through `_render_scalar()` → `json.dumps`, which correctly
and comprehensively escapes them — no bypass found there. `layout.py::normalize_dest()` also
correctly blocks path traversal, and the LLM-controlled `package_path` field the same rung produces
is never used to build a filesystem path. So this is a narrow, specific miss — the rule *keyword*
position is unescaped while everything around it is handled correctly — not a systemic "the whole
renderer is unsafe" situation.

**Severity qualifier:** the sink is CONFIRMED (traced end-to-end through real, live, wired code —
this is not a dead path like `manifest_extract`). Live exploitability depends on whether a real
model response can actually be steered (via prompt injection from a malicious source repo's file
content — see item #4, which already establishes that raw repo content reaches LLM evidence
unredacted) into emitting such a `rule` string; that was not executed against a real model in this
investigation, so it is PLAUSIBLE rather than empirically demonstrated. The sink itself needs no
such caveat — it is real regardless of how hard the input is to steer.

**Minor, related finding:** `_render_attr()`'s attribute *names* and `_split_extension()`'s parsed
var/tag are similarly unescaped, but every current call site across all five ecosystem adapters
uses hardcoded literal attribute names — not LLM- or repo-controlled — so this is a latent pattern
weakness, not a live sink today.

**Status:** OPEN — confirmed oversight (not deliberate), and confirmed to be the SOLE live instance
of this bug class after a full sweep. Investigated by a dispatched subagent, citations personally
re-verified.

**Questions answered:**
- **No ADR discusses this.** `docs/DECISIONS.md` around lines 840-850 does mention `BuildTarget`,
  but only in the context of a *different*, since-superseded proposal (a `bazel/generators.py` →
  `emit.py`/`module.py`/`render.py` module split that — per this project's own CLAUDE.md §1
  corollary — "was never created", ADR-0065 kept `generators.py` as-is instead). That entry never
  addresses `rule`'s lack of a pattern/`Literal` constraint. Confirmed: oversight, not a documented
  trade-off.
- **The prompt template discourages but does not enforce.** `PROMPTS[Role.BUILD_AUTHORING]`
  (`src/fleet/llm/calls.py:214-217`), quoted exactly: *"Propose the minimum set of targets that
  builds this package's sources and tests. Emit target structure only — rule name, srcs, deps,
  visibility. Do not write Starlark text; the harness renders it."* This is a real instruction
  against emitting raw Starlark, but it's prose the model can ignore or be steered around (e.g. via
  prompt injection from a malicious source repo, per item #4) — not a security control. The
  schema/rendering layer is still the only place this can actually be closed.

**Sweep result — `BuildTarget.rule` is confirmed the ONLY live instance of this bug class.** Every
other free-text field in `src/fleet/llm/schemas.py` was checked individually:
- `ProposedFileEdit.path`/`.diff` (looked like the scariest candidate — an LLM choosing a file path
  and content to write) is actually **SAFE, confirmed live**: `src/fleet/rewrite/apply.py::
  check_diff()`/`_escapes()` (lines ~257-309) is a real, wired guard that rejects an absolute path,
  a `..` path segment, or any hunk path escaping the repo's own destination subtree, before a patch
  is ever applied. This is exactly the kind of check `BuildTarget.rule` is missing.
- `BuildFileProposal.package_path` only ever becomes a Bazel *label* component, never the actual
  filesystem write path (`buildgen.py`'s `_write()` uses the driver's own deterministic `dest`,
  confirmed by direct reading) — SAFE.
- `ManifestExtraction`/`extract_manifest()` is dead code (zero callers) — already independently
  established under item #4; this sweep reached the same conclusion independently, which is a
  useful cross-confirmation.
- Every other free-text field (rationale strings, diagnosis text, PR prose) stays inert data or
  passes through `_render_scalar()`'s `json.dumps` escaping correctly.

**Two latent siblings found — same unescaped-identifier shape, but not LLM-reachable today (no
current schema field feeds them), so PLAUSIBLE/future-risk rather than a live finding:**
1. `render_target()`'s attribute *names* from `target.attrs` (`generators.py:109-110`) — bare,
   unescaped identifiers in the rendered output (distinct from attribute *values*, which are
   correctly escaped). No current `BuildTargetProposal` schema field populates `attrs`.
2. `render_module_bazel()`'s `var`/`tag` derived from `WorkspaceDep.extension`/
   `ToolchainRequirement.extension` via `_split_extension()` (`generators.py:700-707, 903, 933`) —
   currently sourced only from deterministic ecosystem-adapter code, never from an LLM.

Both are worth fixing at the same time as `rule` on general defensive-coding grounds (an adapter
bug or a future schema change could feed either), but neither is a currently-exploitable path.

**On remediation (not verified/adjudicated, offered by the investigating subagent):** constrain
`BuildTarget.rule` (and/or `ProposedBuildTarget.rule` at the LLM-schema boundary, which is the
earlier and cheaper place to reject it) to a conservative identifier pattern
(`^[a-zA-Z_][a-zA-Z0-9_]*$`) — this closes the injection primitive without restricting which real
Bazel rules the harness can emit, since every legitimate rule name already satisfies that pattern.

> **Status update (2026-09-15, round VII tasks 2/3).** FIXED (`72e4959`). A shared
> `BAZEL_IDENTIFIER_PATTERN` (`^[a-zA-Z_][a-zA-Z0-9_]*$`) is now applied to both `BuildTarget.rule`
> (`models/build.py`) and the LLM-facing `BuildTargetProposal.rule` (`llm/schemas.py` — this
> document's own earlier prose calls the same class `ProposedBuildTarget`, a naming slip in this
> file, not a second field), rejecting at the cheaper LLM-schema boundary as well as the internal
> model. RED/GREEN verified by temporarily reverting both source files. The two latent siblings
> named above (`render_target()`'s attribute names, `_split_extension()`'s var/tag) were also
> hardened defensively (`9790a90`, lint-fixed in `2e32967`) — neither was LLM-reachable, so this is
> hardening, not closure of a second live sink. **Status: FIXED** (live sink); latent siblings
> defensively hardened.

---

### 5. The sandbox's "impossible by construction" `--network=none` guarantee rests on an
   unconstrained, env-overridable string field — plus a plausible Cargo-specific network need
   (subagent-dispatched investigation, personally verified before logging)

**Where:**
- `src/fleet/sandbox/container.py`, module docstring (lines 1-5) and `docker_run_argv` (line ~135).
- `src/fleet/settings.py:739` (`VerifySection.network: str = "none"`) and lines 868-879
  (`FleetConfig`'s `env_prefix="FLEET_"`, `env_nested_delimiter="__"`).
- `src/fleet/workers/buildverify.py:603` (`container_network: str = DEFAULT_NETWORK`) and
  240-250 (`CACHE_MOUNT_ROOT`, `_CACHE_FLAGS` — only `disk`/`repository`, both Bazel's own).
- `src/fleet/ecosystems/rust.py:141-158` (`crate_universe`'s `cargo fetch` behavior, quoted below).

**What was found (Finding A — CONFIRMED, personally verified).** `sandbox/container.py`'s own
docstring states, in absolute terms: *"Verification never touches the network, so a build that
'passes' by fetching a dependency from the internet is impossible by construction — `--network=
none` is not a hardening nicety, it is what makes a green build evidence that the vendored/
declared deps are complete."* That claim rests entirely on one field:

```python
# settings.py:739, inside VerifySection
network: str = "none"
```

This is a plain `str`, not a `Literal["none"]` or an enum — nothing in the `Section`/`FleetConfig`
model constrains its value. `FleetConfig` is a `pydantic-settings` `BaseSettings` with
`env_prefix="FLEET_"` and `env_nested_delimiter="__"` (settings.py:873-878), which is the standard
mechanism that makes a nested field overridable via `FLEET_<SECTION>__<FIELD>` — so
`FLEET_VERIFY__NETWORK=bridge` (or `host`) overrides `verify.network` with **zero validation
error**. `docker_run_argv` (`container.py:135`) then emits `f"--network={spec.network}"` verbatim.
So the "impossible by construction" guarantee is actually "true by default, silently voidable by
one env var or one `config/fleet.yaml` line" — a misconfiguration (not even a deliberate attack)
would produce a `docker run --network=bridge ...` sandbox that Bazel's own analysis has no way to
distinguish from a legitimately hermetic one, and a build that "passed" by fetching a dependency
live would look like ordinary evidence of a complete, vendored dependency set.

Note: this repo also has a **separate, deliberately-networked** `container_network: str = "bridge"`
field for the "native baseline" comparison run (settings.py:339, Leg B — a different code path that
intentionally needs network to probe a repo's pre-migration build). That field is out of scope; the
finding is specific to `verify.network`, the field the sandboxed hermetic-build guarantee depends
on.

**What was found (Finding B — PLAUSIBLE, not fully verified — would need an actual sandboxed run to
confirm empirically).** `rust.py`'s own docstring states that `crate_universe`'s
`LockGenerator::generate` runs `cargo fetch` **without `--locked`** — and this happens on **every**
Phase 3/Phase 4 Cargo build, not only repos missing a `Cargo.lock`:

> "The extension is never committed... `crate.from_cargo` rewrites `//:Cargo.lock` in the worktree
> as it runs... every Phase 3 and Phase 4 build re-extends from the seeded lock instead of from the
> previous build's output. That is the same `cargo fetch` without `--locked` described above doing
> the same work again."

`cargo fetch` is a native Cargo subprocess with its own HTTP client, separate from Bazel's own
downloader — the only caches mounted into the sandbox are Bazel's `--disk_cache`/
`--repository_cache` (`buildverify.py:240-250`); no `CARGO_HOME`, no Cargo-specific registry cache,
and no `CARGO_NET_OFFLINE` were found wired anywhere. It's plausible this needs network under
`--network=none` for any Cargo repo, confirming and *widening* item #3's original scope (which
focused only on repos with no `Cargo.lock` — this suggests the exposure is every Cargo build,
lock or no lock). No `FailureClass` and no test fixture anticipates this specific failure shape.

**Confirmed NOT an issue for Go/NPM/JVM:** Go and NPM ecosystems always run real resolvers
producing genuine lockfiles pre-build (never a synthesized floor, per item #3); Gazelle itself is
pinned `-external=static` (`ecosystems/go.py:386-392` — zero network, zero subprocesses by
explicit design); JVM has no Gradle-daemon bridge — Gradle manifests route through the same
`maven.install` path as Maven, which has no lockfile-floor concept at all.

**Status:** OPEN (Finding A); PLAUSIBLE, unresolved (Finding B) — answers and extends item #3's
open question about Cargo/sandbox network interaction, but needs an actual sandboxed run against a
real Cargo repo to confirm empirically rather than from static reading alone.

**Both findings RESOLVED by a dispatched subagent — Finding B empirically, not just statically —
personally re-verified before being logged here.**

**Finding A — CONFIRMED, pure oversight.** No ADR, docstring, test, or config anywhere sets
`verify.network` to anything but `"none"`. The only test touching it, `tests/test_settings.py:199`
(personally re-verified: `assert cfg.verify.network == "none"`), asserts the *default* — it does
not, and nothing else does, assert that a non-`"none"` value is rejected. `docs/DECISIONS.md`'s
ADR-0060/61/62/63 all discuss `--network=none` as a fixed property of the sandbox's design, never
as an operator-configurable knob. The sibling `container_network` field on the *baseline* (non-
hermetic, deliberately networked) path is the one documented exception to "always none" — `verify.
network` has no such rationale anywhere. Conclusion stands: `"none"` was always meant to be the
only legal value.

**Finding B — CONFIRMED EMPIRICALLY**, via an actual Docker run, not static reasoning alone — the
one item in this whole review that needed this. The subagent built a throwaway Rust crate (one
real crates.io dependency, `rand`) in a scratch directory and ran three separate, fresh,
uncached-container scenarios in `rust:1-bookworm` under `--network=none`:

| Scenario | Command | Result |
|---|---|---|
| No `Cargo.lock` | `cargo fetch` | exit 101, `Could not resolve host: index.crates.io` |
| Real, committed `Cargo.lock` | `cargo fetch --locked` | exit 101, identical error |
| Real, committed `Cargo.lock` | plain `cargo fetch` (matches `rust.py:144`'s description of what `crate.from_cargo` actually invokes) | exit 101, identical error |

**A committed lockfile does not help at all** — it pins *versions*, not *bytes*; `cargo fetch`
still needs to reach the registry to download the actual crate archives it names, lockfile or not.
This directly answers item #5's original open question: the exposure isn't "Cargo repos missing a
lockfile," it's **every Cargo repo, lockfile or not**. Confidence: CONFIRMED for the `cargo`
primitive itself, tested directly; PLAUSIBLE-not-fully-closed for whether this project's actual
`rules_rust`/`crate_universe` integration has some other mitigation this test's minimal repro
wouldn't surface (`crate_universe`'s own source wasn't found vendored anywhere in this repo to
trace directly).

**This connects to something this project's own history already flagged and left open.**
`docs/DECISIONS.md` (~lines 4183-4191, an ADR-0061-era entry) states, about the sandboxed
verification path generally: *"The sandboxed path is proven at the argv level only. **No cold
`--network=none` run was ever exercised**... What is proven is that the flags carry the mount
targets and the mounts are emitted"* and *"No real Phase 4 run has ever been exercised against
real Bazel, warm or cold — every `fleet verify` test uses `FakeBazel`."* That entry predates the
in-tree Dockerfile this review found (`docker/fleet-build.Dockerfile`, confirmed to exist, and
confirmed to deliberately ship no Rust/Go/Node toolchain — comment: *"Those are supposed to arrive
through the mounted Bazel repository cache, and baking them in would mask an empty cache instead
of surfacing it"*), so the image gap that ADR entry names is closed — but its core warning, that a
genuinely cold `--network=none` run had never been tested end-to-end, held true until this review's
empirical test. **This is, as far as this review can determine, the first actual cold
`--network=none` run performed against this codebase's real design — and it failed, for Cargo.**

**Reconciliation with item #3's Gitea scan:** since lockfile presence doesn't change the outcome,
this affects **all ~33 Cargo-touching repos** found there, not a lock/no-lock subset — a wider
blast radius than item #5 originally scoped.

**Status:** OPEN, Finding A and B both confirmed (B empirically). This is a real, reproducible
build-time failure mode for a meaningful fraction (~12%) of the local Gitea corpus, currently
invisible to the harness's own error handling (no dedicated `FailureClass`, per the earlier
investigation).

**On remediation (not applied, offered by the investigating subagent):** type `verify.network` as
`Literal["none"]`. For Finding B: provision a Cargo-specific offline vendor cache analogous to
`ecosystems/go.py`'s zero-network Gazelle approach (the one ecosystem already confirmed immune to
this class of issue), and/or add an explicit network-shaped `FailureClass` so this failure at least
surfaces legibly instead of consuming retry-ladder budget on an unfixable BUILD.bazel edit. A
`cargo vendor`-based full-source vendoring approach (this project's own documented pattern for
other sovereign-build efforts, per its global CLAUDE.md) was noted as the one approach that would
structurally close this, untested here.

> **Status update (2026-09-15, round VII task 5).** Finding A resolved by Task 4: `verify.network`
> is now constrained to `Literal["none"]`, enforcing the design intent at the type level. Finding B
> disclosed not fixed: Cargo's `cargo fetch` network requirement during `crate_universe` builds is
> a documented architectural limitation, disclosed via ADR-0137 as a known constraint of the
> current integration, pending a structural vendoring/cache fix (out of scope this round).

---

### 4. Redaction is wired into every EGRESS boundary except the one that talks to the LLM —
   raw source file content reaches the model backend unredacted

**Where:**
- `src/fleet/obs/redact.py` — the redaction module itself (fully read; see its own docstring
  claims below).
- `src/fleet/llm/client.py` — zero references to `redact`/`redact_text`/`redact_mapping`
  anywhere in the file (confirmed by grep, exit 0 / no matches).
- `src/fleet/llm/calls.py` — the shared `_call()` (line ~302) and `render_prompt()` (line ~257)
  functions that build every LLM request. Neither calls redaction; `render_prompt` JSON-serializes
  the caller's `evidence` mapping directly into the outbound `Message`.
- `src/fleet/workers/rewrite.py`, `_evidence()` (line ~768-807) — the concrete, confirmed instance:
  line 804, `"current_content": source` — the **raw, full source text of the file being
  transformed** goes straight into the `Evidence` dict with no redaction call anywhere between the
  file read and the outbound API call.

**What was found.** `obs/redact.py`'s own docstring states its design goal in absolute terms:
*"the single `redact()` applied at EVERY egress boundary... The threat is a forgotten call, not a
missing feature. So `redact()` is wired into the boundaries themselves."* It backs this up for
every boundary it names — logs (`obs/log.py`'s `redaction_processor` in the structlog pipeline),
events (`obs/events.py`), the state DB (`state/repository.py`), subprocess output (`util/proc.py`
redacts `stdout_tail`/`stderr_tail` **at capture time**, before any caller ever sees them), and VCS
output (`vcs/git.py`, `vcs/gitea.py`, `vcs/github.py` — which is presumably where "every PR body"
gets covered). Grepping every file that imports a `redact*` function confirms exactly this set:
`cli.py`, `llm/cache.py`, `obs/events.py`, `obs/log.py`, `orchestrator/findings.py`,
`orchestrator/runner.py`, `state/repository.py`, `util/proc.py`, `vcs/{gitea,github,git}.py`,
`workers/{classify,clone}.py`.

**`src/fleet/llm/client.py` and `src/fleet/llm/calls.py` are absent from that list.** These are the
two modules that actually construct and send every LLM request (`_call()` → `render_prompt()` →
`client.complete()` → the configured `ModelBackend` — `anthropic`/`openai_compatible`/`bedrock`/
`vertex`, per §7.7/§12.40). Nothing in that path calls `redact()`. Protection at this boundary is
therefore **entirely incidental** — it only happens when the specific value handed into `Evidence`
already passed through a boundary that redacts (e.g. a `ProcResult.stderr_tail`, redacted at
`util/proc.py` capture time), never because the LLM egress boundary itself enforces it.

**Confirmed concrete exposure:** `workers/rewrite.py::_evidence()` (the evidence-builder for the
`propose_repair`/`escalate_repair` LLM roles — Phase 2's transform-repair ladder, SPEC §3.2 step 5)
puts `source` — the literal current content of the repo file being transformed — into the
evidence dict verbatim as `"current_content"`. If that file happens to contain a hardcoded
credential (a checked-in `.env`-like config, a hardcoded API key, a committed secret), it goes to
whichever third-party model backend the run is configured against with **zero** redaction pass
applied anywhere in the chain from file read to outbound HTTP call.

**Mitigating factors found in the same sweep** (so this isn't a blanket "the LLM path leaks
everything" claim):
- `workers/buildverify.py`'s `diagnose_build` evidence uses `result.stderr_tail` directly from a
  `ProcResult` — already redacted at capture by `util/proc.py`, so this call site is protected by
  inheritance, not by its own discipline.
- `workers/buildgen.py`'s `author_build_file` evidence (`_author()`) is limited to file **paths**,
  ecosystem name, and dependency **labels** — no file content — so the exposure surface there is
  much narrower (a secret would have to be embedded in a *path*, which is atypical though not
  impossible).
- `rewrite.py`'s own `_apply_stderr()` helper reads from `GitCommandError.stderr` or
  `git.exec(...).stderr_tail` — both plausibly redacted upstream via `util/proc.py`/`vcs/git.py`,
  though this wasn't independently traced end-to-end.
- The SPEC §7.3 `manifest_extract` LLM fallback (`ManifestExtraction` schema, designed to hand raw
  unparseable-manifest text to the model) is defined (`llm/schemas.py`, `llm/calls.py`,
  `llm/roles.py`) but **has zero call sites** anywhere in `src/fleet/workers/` or `cli.py` —
  currently unwired. Not a live exposure today, but the same gap shape is latent in it if/when it
  is wired up, since it's specifically designed around raw file content.

**External comparison (via a fork surveying `references/`):** `references/
visa-vulnerability-agentic-harness/` — a comparably-shaped Python harness that also forwards repo
source to an LLM — documents this *exact* gap shape as a disclosed, named limitation in its own
`SECURITY.md`: *"redaction is not a universal pre-egress guarantee for every prompt or backend...
the default... route... is masked only at the report boundary, not before it reaches the model."*
That harness treats this as a known, disclosed risk category (with mitigations: recommends
zero-retention model endpoints, presence-only credential logging, scoped short-lived clone
tokens) rather than an oversight — which is a useful frame for adjudicating this item: the
question isn't "is any gap here surprising," it's whether *our* harness discloses it anywhere the
way that one does. It currently does not appear to (no `SECURITY.md` or equivalent found in this
repo).

**Status:** OPEN, with the core claim now upgraded from "presumed" to fully traced — and a second,
arguably more severe instance of the same gap found and confirmed, in the **public PR body** rather
than only the private LLM egress. Investigated by a dispatched subagent, citations personally
re-verified.

**Question 1 — CONFIRMED, fully traced.** `rewrite.py:452`:
`source = (root / unit).read_text(encoding="utf-8")` — a plain worktree file read, immediately
followed by `outcome = await pipeline.rewrite_file(unit, source)`, which flows to `_evidence()`'s
`"current_content": source` (line 804) with no redaction anywhere in between. This was "presumed"
in the original write-up above; it is now personally re-verified against the exact line.

**NEW finding — Important/Critical (public-facing), CONFIRMED, corrects a prior mitigating claim in
this very item.** The original write-up above listed VCS output (`vcs/git.py`, `vcs/gitea.py`,
`vcs/github.py`) among the boundaries redaction covers, on the assumption that this is
*"presumably... where 'every PR body' gets covered."* **That assumption was wrong, and independently
verified wrong here:** every `redact_text`/`redact` call in `vcs/gitea.py` and `vcs/github.py`
(re-grepped personally) is used exclusively to sanitize **inbound** error/response text for
exception messages (e.g. `gitea.py:347`: `f"...{redact_text(payload)[:400]}"` inside a raised
`GiteaError` about a failed API call) — never the **outbound** PR body/title content actually being
posted. `src/fleet/workers/prwriter.py` has **zero** `from fleet.obs.redact import` anywhere in the
file (re-verified by grep). The actual PR body text is assembled at `prwriter.py:588-590`:
`notes = prose.value.body` (the LLM's own generated prose from `write_pr_body`) is concatenated
directly — `lines += ["", "### Migration notes", "", notes]` — into the final returned body string,
with no redaction call anywhere in that function or file. So a secret that reached the LLM via
item #4's original gap (or one the LLM's own prose happens to echo for any reason) can propagate
one step further: into a **PR body publicly posted to GitHub/Gitea** — a broader-audience exposure
than a single API call to a configured model backend, since a PR is visible to every repo
collaborator and, if the repo or PR is public, the internet. `prwriter.py:120`'s field comment on
`source_url` — `"Credential-free; redacted at §11.4"` — is a comment *asserting* a property, not a
call *enforcing* it, which is exactly the "forgotten call" failure mode `obs/redact.py`'s own
docstring warns against.

**Question 3 — CONFIRMED, matches suspicion.** No data-retention policy for the configured LLM
backend exists anywhere in `config/models.yaml` or `FleetSettings` — confirmed absent by the
investigating subagent's sweep, independently plausible given neither file surfaced in any of this
review's greps of settings/config for retention-related terms.

**Question 4a (`ClassifyOutput` advisory-only) — CONFIRMED.** `repos.kind` (classify.py's output)
is write-only in the codebase — set, never read back to drive any branch — confirming ADR-0008's
"advisory only" claim actually holds in practice, not just in the docstring.

**Question 4b (symlink guard scope) — PARTIALLY CONFIRMED, narrower than feared.** `walk_files()`
(repo-content scanning) and `clone.py` (the clone step) both independently reject symlinks. Two
**lower-trust, harness-config-authored** walks — `src/fleet/rewrite/rules.py:251` (loading
`RewriteRule` YAML files) and `src/fleet/settings.py:1597` — do not check for symlinks, but these
walk the harness's *own* config directories (author-controlled, not arbitrary repo content), which
is a materially different trust boundary than the item #2 finding about the *relocation* path
(`cli.py::_tracked_at()`) not inheriting the guard over **repo-controlled** content. Lower severity
than item #2's open symlink question, not the same finding.

**Question 5 (raw secret echoed in diagnostics) — one real, narrower finding, not a leak by
itself.** `SecretRegistry` (`settings.py:963`) exists and correctly wraps API keys in `SecretStr`
specifically so a settings object caught in a traceback/log/`model_dump()` cannot carry a live key
(confirmed by reading its docstring and `__repr__`). But `src/fleet/llm/backends/anthropic.py`'s
`_api_key()` (line ~258-266) reads the key directly via `os.environ.get(name, "")` into a plain
`str`, bypassing `SecretRegistry` entirely — so this one backend's key exists, briefly, as an
un-wrapped plain string that relies solely on `obs/redact.py`'s boundary-scanning (which does
independently re-scan `os.environ` for secret-shaped var names — see `redact_text()`'s
`_iter_env_secrets()`) rather than also getting the type-level protection `SecretRegistry` was
built to provide. This is a defense-in-depth inconsistency, not a demonstrated leak on its own —
no path was found where this specific `value` reaches a log/error string unredacted.

**Question 6 (secret-flagging mechanism) — CONFIRMED present.** `SecretRegistry` (above) is exactly
this mechanism, resolving keys "by the *name* each target's `api_key_env` declares" — comparable to
what the fork's `deepagents` survey found, just not applied uniformly (see Question 5).

> **Status update (2026-09-15, round VII task 9).** Verification confirmed the controller's findings:
> `SecretRegistry` is defined and constructed only in `settings.py` (lines 963–1636) but has **zero
> call sites in any of the five `src/fleet/llm/backends/*.py` implementations** — confirmed by grep.
> `anthropic.py::_api_key()` (line 258) and `openai_compatible.py::_api_key()` (line 339) both resolve
> their keys via plain `os.environ.get()`, bypassing `SecretRegistry` identically. `bedrock.py` and
> `vertex.py` ship no `_api_key()` method at all — they have no `api_key_env` field by design.
> **Ruling (not re-litigated, merely recorded here for closure):** wrapping only `anthropic.py` in
> `SecretRegistry` would make it the *sole inconsistent backend*, the opposite of the goal. This is a
> **defense-in-depth gap, not a demonstrated leak** (no path was found where the bare-string `value`
> reaches a log/error unredacted at call time). A proper fix requires threading
> `FleetConfig.secrets: SecretRegistry` through the backend registry at construction time — a
> cross-cutting change across all five backend files, out of scope for this round's remediation plan
> and properly routed through an architectural follow-up ADR instead.

**Question 2 (LLM cache as a second at-rest leak) — CONFIRMED SAFE, ruling out a hypothesis.**
`src/fleet/llm/cache.py` never persists raw prompt/evidence content — only `prompt_sha256` (a hash)
— and the model's *response* is redacted before being written to the cache (`cache.py:654`). So a
secret in `"current_content"` does not ALSO end up sitting in plaintext in the local LLM response
cache; the network egress (and now the PR-body egress) are the confirmed exposure paths, not the
cache.

**ESCALATION — CRITICAL, CONFIRMED: this exact gap is affirmatively claimed CLOSED in the
project's own SPEC and criteria-tracking docs, and it is not.** A follow-up subagent (instructed to
also chase whether commit messages share this gap, and to build a full artifact-vs-redaction
table) found and I personally re-verified:

- `docs/SPEC.md:7175` (§11.4's own redaction-boundary list) states outright: *"`workers/
  prwriter.py` redacts the assembled PR body **and** re-scans it after the LLM prose..."* —
  **false**. `grep -n redact src/fleet/workers/prwriter.py` returns exactly **one** hit in the
  entire file, and it's a field-description comment (`source_url: ... "Credential-free; redacted
  at §11.4"`), not a function call. `render_body()` — the function whose output becomes the literal
  `gh pr create`/Gitea-API body — contains zero redaction calls.
- `docs/CRITERIA_PLAN.md:1468` marks §12.20 **"Criterion DONE"**, citing
  `tests/test_pr_body_redaction.py`. That test's own docstring is remarkably candid about the
  actual scope, once read closely: it states the redaction boundary is `cli.py::_write_pr_record`'s
  `redact_text(draft.model_dump_json())` call **before the SQLite INSERT** — i.e. it proves the
  **local DB mirror** of the PR body is redacted, and its own comment explicitly notes `render_body`
  itself "never redacts: its output is also the `gh`/Gitea [body]" (the same string that gets
  POSTED). The test is real, passes, and proves a true thing — it just proves something narrower
  than what SPEC.md and CRITERIA_PLAN.md both claim it proves. This is exactly the "criterion text
  vs. what was actually built" divergence this project's own CLAUDE.md Rules 13/14 and Guardrail 6
  exist to catch — and it recurred here undetected.
- **Two more unredacted call sites found, previously unlogged:** `write_pr_title`'s LLM output
  (`prwriter.py:438-444`: `title = proposed.value.title`, returned as `title[:120]` with no
  redaction) has the identical gap. And `render_body()` is called a **second** time, independently,
  on every PR-promotion/revalidation round via `cli.py::_regenerate_pr_body()`
  (lines 15877-15933) — so the gap isn't confined to initial PR creation.
- **Commit messages are confirmed CLEAN** — the one place this sweep found no gap. Every commit
  message in `rewrite.py`, `relocate.py`, and `cli.py` (6 sites checked) is built entirely from
  code-authored f-strings; no LLM-generated text ever reaches `git commit`.
- **Full artifact table** (from the subagent, spot-checked): PR body posted → **NOT redacted**; PR
  title posted → **NOT redacted** (new); PR body DB mirror → redacted (DB copy only, not what's
  posted); PR body on promotion → **NOT redacted** (new, second call site); build-diagnosis LLM
  prose → write-only, never persisted/posted anywhere (no exposure); structured logs → redacted
  correctly at the sink; commit messages → no LLM text present at all, not applicable.

**Status:** OPEN, now the highest-confidence and most consequential finding in this document: a
real secret reaching LLM evidence (this item's original finding) has **two** confirmed, live,
publicly-visible ways to surface further downstream — the PR body and the PR title, on every
create and every promotion — while the project's own tracking documents assert this is fixed.

**Questions to chase next:**
- Is there a deliberate design reason redaction stops at "wrote to our own storage" rather than
  "left the process or became a public artifact"? Given the SPEC.md text directly claims the
  opposite (that `prwriter.py` DOES redact the posted body), "oversight" is now confirmed as the
  more likely answer, not just suspected — the documentation and the code have simply drifted apart
  since whichever revision made the SPEC.md claim true (if it ever was).
- Whether `docs/INTEGRATION_HONESTY.md` should carry a D-number for this (per this project's own
  CLAUDE.md rules on documentation-vs-code integrity failures) — that's a question for the
  project's own maintenance process, out of scope for this security-review log to decide, but worth
  raising given how squarely it matches that document's stated purpose.

> **Status update (2026-09-15, round VII tasks 6/7/8).** FIXED. The core gap (evidence reaching the
> outbound LLM prompt unredacted) is fixed in `llm/calls.py::render_prompt()` (`ccc0016`):
> `redact_mapping()` is applied to the evidence mapping before `json.dumps`, preserving the
> byte-identical-for-identical-inputs contract `prompt_sha256` depends on. The PR-egress gap named
> above (title and body, on every create and every promotion) is fixed at both call sites —
> `workers/prwriter.py::_compose()`'s title (redacted before the `[:120]` truncation, so a secret
> spanning that boundary can't be sliced in half unredacted) and `cli.py::_regenerate_pr_body()`'s
> return (`199f2dd`) — `render_body()` itself is deliberately left unredacted, since its direct-call
> contract (`tests/test_pr_body_redaction.py`) proves the separate DB-mirror redaction and would be
> silently narrowed by redacting inside it. The documentation drift this item flagged (§11.4's
> wrong-module attribution, the PR-title omission, and `CRITERIA_PLAN.md`'s §12.20 DONE verdict
> citing only the DB-mirror test) is corrected in `docs/SPEC.md`/`docs/CRITERIA_PLAN.md`, and
> `docs/INTEGRATION_HONESTY.md` D138 (`08d2aa9`) records the divergence itself, status FIXED,
> LANDED against `ccc0016`/`199f2dd`. Question 5 (SecretRegistry bypass) remains a disclosed
> defense-in-depth gap, not fixed as code — see the round VII task 9 note on Question 5 above.
> **Status: FIXED** (core gap and both PR-egress sites); Question 5's defense-in-depth gap remains
> open, disclosed.

---

### 3. Re-measurement — "no lockfile ⇒ floor with no transitive closure" is real but scoped to
   Cargo only, not a general harness behavior (correction of an earlier overstated claim)

**Where:** `src/fleet/cli.py::_resolved_support_files()` / `_run_resolution()`; `src/fleet/models/
build.py::Resolution`; `src/fleet/ecosystems/{go,js,py,rust,jvm}.py`.

**What was found — re-checking an earlier claim made in this review session:** the original
statement was *"if a source repo ships no lockfile, the harness can only synthesize a 'floor' from
the manifest's declared specs, which carries no transitive closure — Bazel then fails analysis."*
That is true for exactly **one** of the six ecosystems, not as a general rule.

`_resolved_support_files()`'s own docstring states the precedence explicitly — **carry → resolve →
floor** — and says outright: *"Step 2 never falls back to step 3. That fallback is the defect."*

- **Go, NPM/JS, PyPI** (`go.py`, `js.py`, `py.py` each implement `resolution()`): a missing
  lockfile triggers `_run_resolution()`, which shells out to a real resolver (`go`/`pnpm`/pip-
  equivalent) via `RESOLVER_RUNNER or proc_run` (the `CommandRunner` seam) over the **whole
  ecosystem's** unit set, computing a genuine transitive closure. Any resolver failure — missing
  tool, non-zero exit, timeout, empty output — raises a loud `DependencyResolutionError`. It never
  silently substitutes the floor.
- **JVM** (`jvm.py`): implements neither `resolution()` nor `workspace_files()`. `maven.install`
  pins per artifact and has no lockfile concept at all, so there's no floor to fall through to —
  the risk this item describes doesn't apply to JVM by construction, not by mitigation.
- **Cargo** (`rust.py`): the one real case. No `resolution()` override, so a repo shipping no
  `Cargo.lock` gets a synthesized placeholder (`"# GENERATED BY fleet — no Cargo.lock was found in
  the source repo.\nversion = 3\n"` — an empty lock). **But** `rust.py`'s own docs note this is
  less severe than it looks: `crate_universe`'s `LockGenerator::generate` runs `cargo fetch`
  **without** `--locked` when a lock is present, so Cargo's own resolver extends that empty floor
  into a real lock **during the Bazel build itself** — outside this harness's `_run_resolution`
  path, and (implicitly) requiring registry/network access at Bazel-build time rather than at
  harness-plan time. So Cargo's floor is real, but Bazel's own tooling papers over it rather than
  the harness shipping a definitively broken lock.

**Corrected takeaway:** the "floor with no transitive closure" failure mode is real, but scoped to
Cargo repos with no `Cargo.lock` (and mitigated somewhat by `crate_universe`'s own extension
behavior at build time) — not a general property of "no lockfile" across the harness.

**Status:** CLOSED as originally worded (the blanket claim was overstated); reopened narrowly —
see the Cargo-specific question below.

**Questions to chase:**
- Does `crate_universe`'s build-time `cargo fetch` extension require network access under the
  `--network=none` sandboxed verification step (§3.3 step 4)? If so, a Cargo repo with no
  `Cargo.lock` may still hard-fail in the sandbox even though the harness's own resolution step
  didn't reject it — worth checking whether this is a distinct, undisclosed failure mode from the
  one `lockfile.py`/`MODULE.bazel.lock` already documents.

**Impact — scan of the local Gitea corpus (2026-09-15), by ecosystem exposure.** Same 269 bare
repos as item #2's scan, same method (`git ls-tree -r --name-only <default-branch>` per repo, no
clone, no `.git/config` read). This time classifying each repo by whether it contains a manifest
filename any of the six `ManifestAdapter`s would match, **anywhere in its tree** — `go.mod`
(Go), `package.json` (NPM), `pyproject.toml`/`setup.cfg`/`requirements*.txt` (PyPI), `pom.xml`/
`build.gradle`/`build.gradle.kts`/`libs.versions.toml` (JVM), `Cargo.toml` (Cargo). A repo can
match more than one bucket (polyglot repos are common in this corpus), so per-bucket counts are
**not** mutually exclusive except for the "all other" bucket, which is the exhaustive residual
(matches none of the five patterns) and is disjoint from the other three by construction.

**Caveat before the numbers:** this is filename-presence detection, not the harness's real Phase 1
walk — it doesn't run `ManifestAdapter.parse()`, doesn't resolve a repo's *primary* published
coordinate (the harness picks one ecosystem per repo for `layout()`; a polyglot repo here may
still land under a single ecosystem in a real run), and doesn't check whether a matching manifest
sits inside an ignored path (`node_modules/`, `vendor/`, etc. — `DEFAULT_IGNORE_GLOBS`) or a
vendored third-party subtree the repo owner didn't author. Treat these as upper-bound exposure
counts, not a prediction of exact per-repo harness behavior.

| Section | Ecosystems | Repos matched | Notes |
|---|---|---|---|
| 1. Go / NPM / PyPI (real resolver protection) | `GO`, `NPM`, `PYPI` | **216 / 269 (~80%)** | Individually: `go.mod` 25, `package.json` 123, `pyproject.toml`/`setup.cfg`/`requirements*.txt` 152 — heavy overlap (many repos match 2+ of the three). This is the bucket where item #3's correction applies: a missing lockfile here gets a real resolver run, not a silent floor. |
| 2. JVM (no lockfile concept) | `MAVEN`, `GRADLE` | **14 / 269 (~5%)** | `pom.xml`/`build.gradle`/`build.gradle.kts`/`libs.versions.toml`. No floor risk applies — there's no lockfile mechanism to fall through from. |
| 3. Cargo (real floor risk, build-time mitigated) | `CARGO` | **33 / 269 (~12%)** | `Cargo.toml`. This is the bucket where the original "floor, no transitive closure" claim is actually accurate, moderated by `crate_universe`'s build-time `cargo fetch` extension. |
| 4. All other (no matching manifest anywhere) | none of the above — `UNKNOWN` fallback | **45 / 269 (~17%)** | No `ManifestAdapter` would claim anything in these repos at all, so no lockfile/resolution question arises — they'd land in the `UNKNOWN` ecosystem fallback (bare `filegroup`, no external deps declared) per item #2's discussion. Sample: `claude-code`, `jellyfin`, `pokeemerald`, `searxng-docker`, `sigma`, `superpowers`, `vllm-dgx-spark`, and 38 others — a mix of non-supported languages, docs/config-only repos, and tooling repos. |

Sections 1-3 sum to 263 bucket-memberships across 224 distinct repos (269 − 45 all-other) — the
39-repo gap between the sum and the distinct count is polyglot overlap (e.g. a repo with both
`package.json` and `pyproject.toml` counts in section 1 once but contributes to both the `npm` and
`pypi` sub-tallies).

---

### 1. Unparseable version spec is silently dropped from MVS intersection, not treated as an error

**Where:** `src/fleet/bazel/generators.py`, `mvs_select()`, lines ~517-523.

**What was found:** When reconciling a third-party dependency's version across every repo that
declares it (SPEC §3.3 step 3), each repo's `version_spec` string is run through `parse_range()`.
Specs that parse successfully are collected into `parsed` and intersected together to compute the
MVS-selected version. A spec that **fails to parse** is silently excluded from `parsed` — it
contributes no constraint at all to the intersection. Only if **every** spec in the group fails to
parse does `mvs_select()` raise (`VersionConflict(..., "no spec could be parsed")`).

```python
parsed: list[tuple[str, VersionRange]] = []
for spec in specs:
    rng = parse_range(spec)
    if rng is not None:
        parsed.append((spec, rng))
if not parsed:
    raise VersionConflict(coord_key, specs, repo_ids, "no spec could be parsed")
```

**Why it might matter:** A single malformed, unusual, or maliciously crafted version spec string
in one repo's manifest (e.g. from a `pom.xml`/`package.json`/`Cargo.toml` a `ManifestAdapter`
parsed, or an LLM `manifest_extract` fallback result) does not block the build and does not raise
a finding — its constraint is simply not enforced. The MVS reconciliation proceeds as if that repo
had declared nothing, and the selected version is computed from the remaining, parseable specs
only. Contrast with `VersionConflict`'s stated design goal (module docstring): "A version that
satisfies *most* specs is never selected, however large that majority" — the empty-intersection
protection is real MVS, but that protection only covers specs that made it into `parsed` in the
first place. An unparseable spec is invisible to it.

**Status:** CLOSED — deliberate and tested. Investigated by a dispatched subagent, re-verified
personally.

**Questions answered:**
- Both `graph/collisions.py::_intersects()` (L482-499, Phase 1's satisfiability check) and
  `bazel/generators.py::mvs_select()` (L503-549, Phase 3's selection) drop an unparseable spec from
  the intersection identically — the same policy at both layers, not an inconsistency between them.
- `tests/test_bazel.py:438-443` pins this exact behavior:
  `test_an_unparseable_spec_never_produces_a_conflict_it_cannot_prove` — so this is a tested,
  intentional design choice, not an oversight. No `findings` row surfaces an unparseable spec
  elsewhere in the pipeline (unverified whether that absence itself is worth a separate finding —
  low priority given the behavior is deliberate).
- **A related, more concerning defect was found in the same regex** (see below) — the grammar
  question ("does `parse_range()`'s grammar cover everything") had a real answer, and it's not
  "yes."

**New finding — Important, CONFIRMED, untested: a real semver pre-release is silently
MISparsed, not excluded.** `_ATOM` (`generators.py:331`, `re.compile(r"^(==|>=|<=|=|>|<|\^|~>|~)?
\s*v?(\d+(?:\.\d+)*)")`) is used with `.match()`, which anchors only at the **start** of the
string and does not require consuming it to the end. A spec like `1.2.3-beta.1` — a real,
legitimate semver pre-release tag, not a malicious or malformed input — matches the numeric prefix
`1.2.3` and silently ignores `-beta.1` entirely. This is a **confidently wrong answer**, which is
worse than this item's original "silently dropped" behavior: a dropped spec contributes no
constraint (conservative), but a misparsed one contributes a **wrong** constraint that MVS then
trusts as if the repo had declared plain `1.2.3`, potentially selecting a version the repo never
actually asked for or one that violates what its pre-release pin was guarding against. Not covered
by any test found.

**New finding — see item #6 below.** The broader sweep of this same rendering path (whether an
unescaped value could inject Starlark into generated `MODULE.bazel`/`BUILD.bazel`) found a
separate, more severe, Critical-severity issue, logged as its own item since it's a materially
different vulnerability class (code injection, not a parsing gap) with its own distinct sink and
exploit chain.

**Blast-radius follow-up (dispatched subagent, all key claims independently re-run by me in a
live Python shell against this repo's actual `.venv` before being logged):**

- **The bug is wider than one file.** `graph/collisions.py:51` has its **own** regex,
  `_VERSION_ATOM = re.compile(r"^(==|>=|<=|~=|=|>|<|\^|~>|~)?\s*v?(\d+(?:\.\d+)*)")` — a separate
  literal from `generators.py`'s `_ATOM`, not a shared import. `generators.py`'s own docstring
  comment calling it *"the same spec grammar `graph/collisions.py` audits with"* is imprecise: the
  two are textually different (`collisions.py`'s adds `~=`, PEP 440's compatible-release operator),
  but both share the identical `.match()`-not-`.fullmatch()` anchoring defect. So this bug lives in
  **both** Phase 1 (`collisions.py::_intersects`, the satisfiability check) and Phase 3
  (`generators.py::mvs_select`, the actual selection) — independently, not through shared code.
- **Personally re-verified, live, in this repo's own `.venv`:**
  ```
  >>> from fleet.graph.collisions import _intersects
  >>> _intersects(['==1.2.3-beta.1', '==1.2.3'])
  True
  >>> from fleet.bazel.generators import mvs_select
  >>> mvs_select('pkg', [VersionRequirement(coord_key='pkg', repo_id='A',
  ...     version_spec='>=1.2.3-beta.1'),
  ...     VersionRequirement(coord_key='pkg', repo_id='B', version_spec='>=1.2.0')])
  '1.2.3'
  ```
  Both outputs match the subagent's report exactly. `_intersects` reports two genuinely different,
  non-interchangeable published artifacts (`1.2.3-beta.1` and the later stable `1.2.3`) as
  satisfiable, which routes the `DEP_VERSION` finding to `severity="warn"` (`collisions.py:456-476`)
  instead of surfacing a real conflict. And `mvs_select` silently drops `-beta.1` from the text
  that reaches `bazel_dep(version = ...)`, pinning a version the declaring repo never actually
  asked for.
- **Real-world prevalence — small, honestly-scoped sample, not extrapolated.** Tracing confirms
  `manifests/npm.py` and `manifests/python.py` pass version-spec strings through verbatim, unmodified,
  into `RawDependency.version_spec`. A spot-check of 12 real manifests from this session's earlier
  Gitea corpus scan (6 PyPI, 6 npm) found **zero** hits in the 6 PyPI manifests and **2 of 6** npm
  `package.json` files with a real trigger: `acheron:Gryffindor/package.json`
  (`"react-autocomplete": "^1.0.0-rc2"`) and `activepieces:package.json` (three hits:
  `json-server@1.0.0-beta.0`, `react-data-grid@7.0.0-beta.47`, `redlock@5.0.0-beta.2`). This is a
  small sample (12 files), not a corpus-wide count — reported honestly as such, not rounded up.
- **Confirmed no test covers this shape** in `test_bazel.py`, `test_graph_sequence.py`, or
  `test_collisions_wiring.py`.

**Status:** OPEN (was CLOSED for the original "deliberate, tested, dropped" behavior; the misparse
sub-finding is a live, untested, confirmed-real defect, independently reproduced).

**On remediation (not applied):** switching both regexes from `.match()` to `.fullmatch()` was
verified by the subagent to preserve every currently-supported spec while converting a misparse
into the existing, already-tested "unparseable → dropped/reported" path — no expressiveness lost.

> **Status update (2026-09-15, round VII task 11).** FIXED (`74dfbd3`). Both `_ATOM`
> (`bazel/generators.py`) and `_VERSION_ATOM` (`graph/collisions.py`) now use `.fullmatch()` instead
> of `.match()`, so a spec carrying a trailing pre-release tag (e.g. `1.2.3-beta.1`) fails to match
> entirely and is routed through the existing, deliberately-tested "unparseable → dropped" path
> instead of being silently misparsed to `1.2.3`. The original "deliberate, tested, dropped"
> behavior this item was originally CLOSED for is unchanged — only what counts as "unparseable"
> changed. Every spec literal used across `tests/test_bazel.py` and `tests/test_graph_sequence.py`
> was verified to still parse identically under `fullmatch`. **Status: FIXED.**

---

### 2. A repo's own pre-existing `BUILD.bazel`/`WORKSPACE`/`MODULE.bazel` is relocated into
   history, then silently overwritten in the worktree by the generated file — no diff, no finding

**Where:**
- `src/fleet/workers/interrogate.py` (`walk_files`, `DEFAULT_IGNORE_GLOBS`) — the Phase 1 file
  walk that decides what's visible to the fleet.
- `src/fleet/manifests/base.py` (`adapter_for`) — ecosystem classification, via `ManifestAdapter`.
- `src/fleet/vcs/filter_repo.py` / `src/fleet/workers/relocate.py` — the relocation/ingest path
  (no filename-based exclusion found for `BUILD.bazel`/`WORKSPACE`/`MODULE.bazel`).
- `src/fleet/workers/buildgen.py`, lines ~361-390 and `_write()` at line ~676-679 — the generated
  `BUILD.bazel` write.

**What was found, starting from the original claim under investigation** ("if an ingested repo
already has hand-written `BUILD`/`MODULE.bazel` files, the harness ignores them entirely — it
never parses them for dependency truth"):

1. **No repo is skipped or excluded for having Bazel files.** `interrogate.py`'s own docstring:
   *"A repo is never silently dropped."* Ecosystem classification is driven entirely by whichever
   native-language manifest (`go.mod`, `pom.xml`, `package.json`, `Cargo.toml`,
   `pyproject.toml`/`requirements*`) a `ManifestAdapter.matches()` claims — none of the six shipped
   adapters match on `BUILD.bazel`/`WORKSPACE`/`MODULE.bazel`, because there is no Bazel adapter
   (confirmed earlier, item under the "does it understand Bazel deps" discussion). A repo with
   *only* Bazel files and no recognized manifest still isn't dropped — it falls to the `UNKNOWN`
   ecosystem fallback (`ecosystems/unknown.py`): a degraded `filegroup`, zero declared external
   deps, plus a `no-manifest` finding.
2. **The pre-existing Bazel files are not filtered out of the walk or the relocation.**
   `DEFAULT_IGNORE_GLOBS` in `interrogate.py` (`node_modules`, `target`, `build`, `dist`, `vendor`,
   `.venv`, `testdata`, `*.min.js`) does not cover `BUILD.bazel`, `WORKSPACE`, or `MODULE.bazel`
   filenames, and neither `relocate.py` nor `vcs/filter_repo.py` name them. So they walk through
   like any other source file and get carried into the monorepo's history at `<dest>/BUILD.bazel`
   etc. by the `git-filter-repo` ingest step (SPEC §3.3 step 1).
3. **The same worker then overwrites that exact path unconditionally.** `buildgen.py` runs ingest
   → BUILD.bazel generation → MODULE.bazel reconciliation as three units of *one* worker
   (docstring: "Ingest → `BUILD.bazel` → `MODULE.bazel`, as three separately-resumable units").
   Step 2 renders the adapter's targets and writes them via:
   ```python
   def _write(path: Path, text: str) -> None:
       """Write a generated file, creating its package directory. Generated, never hand-edited."""
       path.parent.mkdir(parents=True, exist_ok=True)
       path.write_text(text, encoding="utf-8")
   ```
   No read of the existing file, no diff, no comparison, no `findings` row recording that a
   hand-written file at that path was replaced.

**Net effect:** a source repo's own `BUILD.bazel`/`WORKSPACE`/`MODULE.bazel` is preserved in git
history (`git log` on that path in the monorepo still shows the original commits, per the
`Source-Repo:`/`Source-Sha:` trailers ADR-0011 requires), but the **working tree** content at that
path is silently replaced by the harness's generated version, with nothing in the pipeline
recording that a hand-authored file existed there and was discarded.

**Why it might matter:** this is a *different* risk shape than item #1 — it's not about a bad
version string slipping through, it's about **loss of operator/author intent with no signal**. If
a source repo's hand-written `BUILD.bazel` encoded something the generic adapter-driven generation
doesn't know to reproduce (a custom visibility restriction, a `select()` for a platform-specific
build, an extra `data` dependency the manifest doesn't declare), that intent is discarded silently
— discoverable only by manually diffing git history against the generated file, not by any
`findings` row or migration report.

**Status:** RESOLVED as a genuine, undisclosed gap — investigated by a dispatched subagent
(superpowers:subagent-driven-development, adapted for research rather than code-change tasks),
citations personally re-verified against current source before being logged here.

**Questions answered (subagent findings, independently re-verified):**
- **No `collisions`/`findings` kind covers this case, structurally.** `CollisionFinding.kind`
  (`src/fleet/models/graph.py:361`) is a closed `Literal["COORDINATE", "CONTRACT", "DEST_PATH",
  "FILE_PATH", "DEP_VERSION"]`, and every kind requires `repo_ids: list[RepoId] = Field(min_length=
  2)` (line 365) — verified directly. `FILE_PATH`'s own detector needs two *different* repos
  landing on one path; a repo's own pre-existing file versus its own later-generated replacement is
  one repo, so this is structurally unrepresentable in the current model, not merely undetected.
- **SPEC.md never discloses this.** A grep for "overwrit"/"hand-written" across the whole 7,891-line
  file (independently re-run) turns up only unrelated hits — DB upsert semantics, reservation
  arithmetic, the harness's own generated-file self-description — never this behavior. §3.3's Ingest
  section (SPEC.md:1211-1260) documents the `git-filter-repo` → merge → snapshot-ref sequence in
  detail and never mentions what happens to a source repo's own Bazel files.
- **The PR body cannot disclose it — and the field built for exactly this purpose is dead code.**
  `PrwriterInput.relocation_summary` (`src/fleet/workers/prwriter.py:140`) is rendered into a
  "### Relocation map" section (lines 573-574) — but a fleet-wide grep for `relocation_summary=`
  (excluding tests) finds **zero** call sites that ever populate it with real data anywhere in
  `src/fleet/`. It always defaults to `[]`, so that section can never render in an actual run. This
  was independently re-confirmed by grep before being logged: the mechanism exists in the schema
  and the renderer, and nothing in the pipeline ever feeds it.

**New finding from the broader sweep, independently re-verified — symlink discipline doesn't extend
past Phase 1's own scan.** `interrogate.py::walk_files()`'s documented "symlinks are not followed"
(line 136) governs only Phase 1's *scanning* walk. The actual relocation domain — what Phase 2/3
moves — comes from a **separate** code path: `cli.py::_tracked_at()` (lines 6233-6236), which lists
every path via `git ls-tree -r -z --name-only <rev>`. Verified directly: `--name-only` strips the
mode column from `ls-tree`'s output entirely, so a tracked symlink (git mode `120000`) is
indistinguishable in this listing from an ordinary tracked file — there is no type filter anywhere
in `_tracked_at()` or its caller. This is a previously-undocumented second traversal path with no
inherited symlink guard. **Not yet determined:** whether anything downstream actually *dereferences*
such a relocated symlink in a way that reads/writes outside the intended worktree (the subagent
ran out of budget tracing this; flagged as the next thing to chase, not asserted as exploitable).

**Supporting context, independently re-verified — `_write()`'s no-diff/no-check pattern is used
uniformly, though not always as a defect.** `buildgen.py::_write()` is used identically for
`BUILD.bazel` (line ~390), `MODULE.bazel` (line ~581), and every declared support file via
`materialize()` (line ~701, e.g. resolved lockfiles). For `BUILD.bazel`/`MODULE.bazel` at a
per-repo destination, this is the same silent-overwrite gap described above. For fleet-wide
`materialize()`d support files (a resolved `pnpm-lock.yaml`/`requirements.lock`), overwriting is
**by design**, not a new instance of the gap — item #3 already establishes the carry→resolve→floor
precedence that intentionally supersedes whatever a single repo's own lock carried. Noted here only
to establish that `_write()`'s pattern is systemic, not to claim a third defect site.

**Both follow-up questions RESOLVED by a dispatched subagent, personally re-verified:**
- **The symlink-dereference question is resolved, and the answer is worse than "escapes the
  worktree" — see new item #7 (CRITICAL).** Phase 3 BUILD-generation itself is confirmed clean (only
  path strings, never file content, per item #7's write-up). The actual dereference happens one
  phase earlier, in Phase 2's rewrite/repair worker, and it doesn't just escape the worktree — it
  reads an arbitrary host path and ships the content to a third-party LLM. Full chain in item #7.
- **`relocation_summary` is confirmed an intended, unwired feature — not dead code to remove.**
  `git log -S relocation_summary` shows it was added once and never populated at either real
  `PrwriterInput` construction site (`cli.py:15780`, `:15902`). But `docs/SPEC.md` (lines 876, 1539,
  2424) explicitly requires a "relocation map" section in the PR body, `prwriter.py` already
  contains the rendering logic for it, and a test already exercises that rendering path. This
  corrects the framing above (which floated CLAUDE.md Rule 2/YAGNI removal as an option) — the
  right fix is wiring the field, not deleting it; the SPEC already commits to this feature existing.

**Impact — scan of the local Gitea corpus (2026-09-15).** Swept all 269 bare repos under
`/mnt/storage/gitea/data/git/repositories/redmage/` for filenames `BUILD.bazel`, `BUILD`,
`WORKSPACE`, `WORKSPACE.bazel`, `MODULE.bazel` at any path, via `git ls-tree -r --name-only
<default-branch>` on each bare repo directly (no clone, no `.git/config` read — avoids the
github_pat-in-origin-URL exposure CLAUDE.md flags for the 251 GitHub mirrors). All 269 repos had a
resolvable default branch (0 empty repos).

**6 of 269 repos (~2.2%) contain at least one matching filename:**

| Repo | Branch | Matching files | Shape |
|---|---|---|---|
| `codex` | main | 70 × `BUILD.bazel` + 1 × `MODULE.bazel` | Near-total: one `BUILD.bazel` per crate across almost the entire `codex-rs/` Rust workspace, plus a root `MODULE.bazel` (bzlmod). A real, substantial, hand-maintained Bazel build graph, not incidental config. |
| `codex-sovereign` | main | 71 × `BUILD.bazel` + 1 × `MODULE.bazel` | Same shape as `codex`, one crate larger (`codex-rs/vendored-crates/runfiles-0.1.0/BUILD.bazel`) — evidently a fork/vendor pass of the same Bazel-built Rust workspace. |
| `expert-dollop` | main | `infrastructure/kong/{BUILD.bazel,MODULE.bazel,WORKSPACE}` | 3 files, one subtree — looks like a vendored Kong API-gateway Bazel build, not fleet-wide config. |
| `modular-monolith-2` | main | `features/kong/{BUILD.bazel,MODULE.bazel,WORKSPACE}` | Identical 3-file footprint to `expert-dollop`, different mount point — likely the same vendored Kong subtree reused across two repos. |
| `organicmaps` | master | `3party/open-location-code/BUILD` | 1 file, classic (non-`.bazel`) name, under a `3party/` vendor directory — a vendored dependency's own build file, not this repo owner's authorship. |
| `yara` | master | `sandbox/BUILD.bazel` | 1 file, single subdirectory. |

**Reading on impact:** if this harness's fleet ever included `codex`/`codex-sovereign`, the defect
in this item stops being a theoretical edge case — it means **~70-71 real, hand-authored,
per-crate `BUILD.bazel` files plus a root `MODULE.bazel`** would each be silently discarded and
replaced with adapter-generated equivalents, with no diff and no finding. Whatever crate-specific
`deps`, `visibility`, `data`, or feature-gated `select()` logic those files encode is not
recoverable from the generated output — only from `git log` on the pre-migration commits, which a
reviewer would have to know to go looking for. The other four repos are lower-impact: three of the
four hits are vendored third-party subtrees (Kong, open-location-code) rather than the repo
owner's own Bazel authorship, so the "loss of author intent" framing applies most sharply to
`codex`/`codex-sovereign` and more weakly to the rest.

**Caveats on this scan:** filename match only — it does not parse content, so it cannot
distinguish a fully-featured hand-written target graph from a stub, nor confirm the Kong/
open-location-code files are actually vendored rather than authored in-repo (inference from path
naming only). It also does not indicate whether any of these 6 repos are actually slated for
migration by this harness's own `config/repos.yaml` — that's a separate, harness-specific question
this scan doesn't answer.

> **Status update (2026-09-15, round VII tasks 12/13).** Detection added, then wired to disclosure
> (not a fix to the underlying overwrite behavior, which `_write()` still performs unchanged — this
> closes the "no diff, no disclosure" gap, not the overwrite itself). Task 12 (`f0773ae`) added
> detection at both `buildgen.py::_write()` call sites: before each write, the destination's
> pre-existing content (if any, and if different from what's about to be rendered) is collected into
> a new `BuildgenOutput.overwritten_bazel_files` list; byte-identical pre-existing content is not
> reported, per this item's own "silent loss" framing. Task 13 (`a2425e4`) wired that list through
> to `PrwriterInput.relocation_summary` — the same previously-dead field this item's investigation
> found above — so an overwrite is now disclosed in the PR body. The real propagation path differed
> from the originally-traced one: `fleet pr` is a later, separate process that reads only the
> `findings` table, never the Phase 3 checkpoint record, so the durable hop is a new
> `BAZEL_OVERWRITE_FINDING_KIND` finding written by `_BuildSink` and read back into `_PrCandidate`,
> following the same pattern `VERIFICATION_KIND`/`PR_RECORD_KIND` already use. For an ATOMIC_WAVE
> SCC's shared PR, the summary is the union of every member's notes. The symlink-dereference
> question this item raised and left unresolved is answered under item #7 (fixed, round VII task 1).
> **Status: overwrite now disclosed (detection + PR-body wiring); the underlying silent-overwrite
> behavior itself is unchanged by design** — a repo's own hand-written Bazel file is still replaced,
> now with a visible record rather than none.

---
