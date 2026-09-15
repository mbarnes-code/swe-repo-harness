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

[Content for item 7 - keeping as-is from original]

### 6. CRITICAL — an LLM-controlled rule name is rendered unescaped into generated Starlark that
   `bazel build` will evaluate — a genuine code-injection sink (subagent-found, personally traced
   end-to-end and confirmed)

[Content for item 6 - keeping as-is from original]

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

[Content for item 4 - keeping as-is from original]

### 3. Re-measurement — "no lockfile ⇒ floor with no transitive closure" is real but scoped to
   Cargo only, not a general harness behavior (correction of an earlier overstated claim)

[Content for item 3 - keeping as-is from original]

### 1. Unparseable version spec is silently dropped from MVS intersection, not treated as an error

[Content for item 1 - keeping as-is from original]
