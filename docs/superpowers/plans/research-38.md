# research-38 — the Rust e2e test's live crates.io dependency, `completed_units`' two discard points, and what a real Bazel failure log costs through `util/proc.py`

Research round 38. Read-only with respect to `src/` and `tests/`; nothing in the harness was
modified. Every numeric/behavioural claim carries a label:

- **MEASURED** — I ran the command shown, on this host, and got the output shown.
- **INFERRED** — a conclusion from measured facts plus source I read; not directly observed.
- **UNKNOWN** — I could not establish it, and say so rather than guess.

`pytest` was never invoked (§19's reaper constraint). No `sudo`. `references/` untouched. No
`FLEET_*` variable was exported. `docs/DECISIONS.md`, `docs/PROGRESS.md` and
`docs/INTEGRATION_HONESTY.md` were read and not edited.

**Every `src/` line number below is against the WORKING TREE as read during this round, not against
a commit.** `git status --short` shows `src/fleet/cli.py`, `src/fleet/workers/buildverify.py`,
`src/fleet/vcs/{git,gitea,filter_repo}.py`, `src/fleet/bazel/layout.py` and the three `docs/` files
modified by concurrent sessions, so §36's warning about trusting line numbers across concurrent work
applies verbatim. The quoted code is the evidence; the numbers are a convenience.

**Scratch root for every experiment below** (nothing in the repo tree was written):
`/tmp/claude-1000/-home-redmage-swe-repo-harness/630bb920-011c-45ef-bdae-82dc401e641d/scratchpad/q1`,
abbreviated `$Q` throughout.

**The suite's own Bazel state was not disturbed.** `$Q/repos` is a **hardlink** copy
(`cp -al /tmp/fleet-bazel-160624fb8a4b/repos $Q/repos`) of the session repository cache, so the
experiments read the warm cache without being able to grow or prune the directory
`tests/conftest.py` polices (MEASURED — `df -h /` unchanged across the copy: `30G` avail before
and after; `du -sh --apparent-size` 1.7 GiB against `du -sh` 1.9 GiB).

---

## Q1 — making `test_two_rust_repos_in_one_wave_both_build` hermetic

### Q1.0 Headline

**The chosen path (a one-off networked warm run into `verify.repository_cache`) does NOT cover
`crate_universe`'s `cargo fetch`. `MODULE.bazel.lock` does — completely — and the harness already
publishes it. MEASURED, both directions.**

Two builds, same workspace, same fully-warm `--repository_cache`, same fresh output base, both with
the network broken:

| run | `MODULE.bazel.lock` present & valid for the `crate` extension | outcome |
|-----|---|---|
| run2 | **yes** | `INFO: Build completed successfully, 231 total actions` in **32.4 s**, offline |
| run4 | **no** (invalidated by one appended comment line in `//:Cargo.lock`) | `Failed to fetch crates for lockfile: exit status: 101` in **26.2 s** |

run4 is the reported flake, reproduced deterministically. The warm repository cache is identical in
both runs and changes nothing.

### Q1.1 The mechanism, read off the pinned ruleset's own source

`rules_rust` is pinned at **0.65.0** (MEASURED — `src/fleet/settings.py:522`). The extension source
below is the copy Bazel fetched into `$Q/out/881237be23d748cac2f7af4c342c7334/external/rules_rust+/`
during run1, i.e. the exact bytes this harness's pin resolves to.

**MEASURED (source read), `crate_universe/extensions.bzl:622`:**

```starlark
    # Determine whether or not to repin dependencies
    repin = not lockfile or determine_repin(...)
```

and `crate_universe/private/generate_utils.bzl:391-393`:

```starlark
    # If a deterministic lockfile was not added then always repin
    if not lockfile_path:
        return True
```

`repin = True` is what runs `splice_workspace_manifest` → `cargo-bazel splice` → `cargo`. The
harness emits **no `lockfile` attribute** on its tag (MEASURED — `src/fleet/ecosystems/rust.py:105-112`
emits exactly `name`, `cargo_lockfile`, `manifests`), so **`repin` is unconditionally `True` on
every evaluation of the extension**.

**`CARGO_HOME` is per-output-base by default.** `crate_universe/private/common_utils.bzl:150-181`:
`isolated` defaults `True` and sets `CARGO_HOME` to `repository_ctx.path(".cargo_home")` — a
directory *inside* the generated repository, i.e. inside the output base. So a fresh output base is
a cold cargo cache by construction, and nothing cargo downloads survives to the next run.
(MEASURED — after run1, `$HOME/.cargo` was **not created** under the empty `HOME=$Q/home`; the only
thing there is `.cache/bazelisk`.)

**The crate *sources* ARE covered by the repository cache; the *resolution* is not.** Both crate
tarballs are present in the cache keyed by their `Cargo.lock` checksums (MEASURED —
`$Q/repos/content_addressable/sha256/7f24254a…` = `hex 0.4.3`,
`…/2304e00983…` = `heck 0.5.0`, both present). Those are downloaded by the *generated*
`crates__hex-0.4.3` repository rules, which do go through Bazel's downloader. The splice step that
*produces* those repository specs does not: it shells out to `cargo`, which opens its own sockets —
structurally identical to `rules_jvm_external`'s coursier (§32-E).

**The exact failure, reproduced (MEASURED — `$Q/run4.err:22-42`):**

```
    Updating crates.io index
warning: spurious network error (3 tries remaining): [7] Could not connect to server …
error: failed to get `heck` as a dependency of package `acme-case-rs v0.2.0 (/tmp/.tmphZzRyQ/rust/acme-case-rs)`
Caused by:
  download of config.json failed
Caused by:
  failed to download from `https://index.crates.io/config.json`
Error: Failed to generate lockfile
Caused by:
    Failed to fetch crates for lockfile: exit status: 101
ERROR: Analysis of target '//rust/acme-codec-rs:acme-codec-rs' failed; build aborted:
  error evaluating module extension @@rules_rust+//crate_universe:extension.bzl%crate
```

Note `index.crates.io/config.json` — cargo's **sparse-registry preflight**. It is fetched before any
package data, on every `cargo fetch` that is not `--offline`.

### Q1.2 What `MODULE.bazel.lock` actually records, and why it is sufficient

**MEASURED** — parsing the 85,782-byte `MODULE.bazel.lock` that run1 wrote:

```
moduleExtensions: [ …rules_kotlin…, …rules_python…config, …rules_python…uv,
                    '@@rules_rust+//crate_universe:extension.bzl%crate' ]
crate → generatedRepoSpecs: ['crates', 'crates__heck-0.5.0', 'crates__hex-0.4.3']
crate → recordedInputs:
    ENV:CARGO_BAZEL_DEBUG \0 … ENV:CARGO_BAZEL_ISOLATED \0 … ENV:REPIN \0
    REPO_MAPPING:… (13 entries)
    FILE:@@//Cargo.lock                      9cc05845304f83660cf4dc3c70a5b7080ec26b13f9849fc54b64eb89e257f79a
    FILE:@@//Cargo.toml                      a397f7c6…
    FILE:@@//rust/acme-case-rs/Cargo.toml    8e59e3ac…
    FILE:@@//rust/acme-codec-rs/Cargo.toml   7199290f…
plus bzlTransitiveDigest and usagesDigest
```

Bazel replays `generatedRepoSpecs` — the whole result of the splice — whenever those inputs match,
and the extension body (hence `cargo`) never executes.

**The recorded `Cargo.lock` hash is the SEEDED lock, not the extended one, and that is what makes
this usable in production.** MEASURED:

```
current (seeded) //:Cargo.lock sha256: 9cc05845304f83660cf4dc3c70a5b7080ec26b13f9849fc54b64eb89e257f79a
run1-extended  //:Cargo.lock sha256: c2bf754485544c1577c885c12065c1c8d43b6d12677eb955790fd9abb928936d
recorded in MODULE.bazel.lock:        9cc05845304f83660cf4dc3c70a5b7080ec26b13f9849fc54b64eb89e257f79a
```

Bazel hashes the file as it was **read**, before `crate.from_cargo` overwrote it. `_publish`
re-asserts the *planned* (seeded) bytes over `//:Cargo.lock` before staging (`rust.py:149-157`;
`cli.py:5077` — "What is committed is the PLANNED bytes, re-asserted here, never what is on disk"),
so **the pair the harness commits — seeded `Cargo.lock` + the
`MODULE.bazel.lock` written by the same build — is self-consistent and replays offline.** That is
not an accident of my fixture; it is the direct consequence of ADR-0064 plus the lock-overwrite
re-assertion, and run2 is the proof that the pair works with the network down.

**INFERRED (not measured):** the same replay covers `pip.parse`'s `whl_library` and gazelle's
`go_deps` for the same structural reason (they are module extensions with recorded results), and
`maven.install` too — which would make §32-E's "coursier cannot be covered under ANY warming
strategy" **too strong**. I did not test JVM, Python or Go. Flagging it because it is a claim
currently in `docs/PROGRESS.md` that this round's mechanism casts doubt on.

### Q1.3 Why the test flakes today

The e2e test builds a **fresh monorepo per test** and `MODULE.bazel.lock` is an artifact the *first*
successful build produces (MEASURED — `cli.py:4904-4905` reads it out of the worktree, `cli.py:5170`
logs `module_lock_absent` when there is none, and `test_build_e2e.py:1662` says in as many words
"On the very first run there is no `MODULE.bazel.lock` anywhere"). So the Rust e2e test's build is
always the *first* build: the extension always re-splices, `cargo` always runs, and crates.io is a
live dependency of the assertion. `--repository_cache` cannot help, warmed or not.

### Q1.4 The three options the brief named, against the harness's existing pins

| option | permitted by the current pins? | verdict |
|---|---|---|
| **`crate.from_cargo(lockfile = …)`** — the cargo-bazel lockfile | **YES.** `lockfile` is a real attribute of the `from_cargo` tag class at rules_rust 0.65.0 (MEASURED — `crate_universe/extensions.bzl:1073-1077`, in `_FROM_COMMON_ATTRS`, which `_from_cargo` includes at `:1093-1103`) | The exact `maven_install.json` analogue and the right *production* fix — but it needs a `cargo-bazel-lock.json` that nothing in this harness produces (`rust.py` declares no `resolution()`, and `cargo`/`rustc` are absent from the host per §23). A second design change of the same shape as ADR-0064. |
| **`CARGO_HOME` placement** — `isolated = False` / `CARGO_BAZEL_ISOLATED=false` + a persistent `CARGO_HOME` | **YES**, both routes: `isolated` is an attribute (`extensions.bzl:1063-1071`) and `CARGO_BAZEL_ISOLATED` is a recorded env input of the extension (MEASURED — it appears in `recordedInputs`) | **Warms, does not make hermetic** — see Q1.5. |
| **`cargo vendor` / `crates_vendor`** | **YES**, the rule exists (MEASURED — `crate_universe/private/crates_vendor.bzl:605 crates_vendor = rule(…)`, with `is_executable = True` at `:478`) | Rejected for the same reason §32-F rejected `--vendor_dir`: it is driven by **`bazel run //:crates_vendor`**, and `_bazel_argv` hardcodes `build`/`test` (MEASURED — `buildverify.py:1045`), so the harness cannot dispatch the populating step. `cargo_config` (`extensions.bzl:1055`) would be the seam for a `[source]` replacement, but the vendor tree still has to be produced by a verb the harness cannot run. |

### Q1.5 `CARGO_HOME` measured, not assumed

**run5a** — `CARGO_BAZEL_ISOLATED=false`, `CARGO_HOME=$Q/cargohome` (empty), network up, fresh
output base: **`Build completed successfully, 231 total actions`, 36.6 s**, and the cache is
genuinely shared out of the output base (MEASURED):

```
$Q/cargohome/registry/index/index.crates.io-1949cf8c6b5b557f
$Q/cargohome/registry/cache/index.crates.io-1949cf8c6b5b557f/{heck-0.5.0.crate,hex-0.4.3.crate}
$Q/cargohome/registry/src/index.crates.io-1949cf8c6b5b557f
```

So the option works mechanically. Then two offline runs, and **they disagree — which is the whole
finding**:

- **run5b** — warm `CARGO_HOME`, network broken, extension forced to re-evaluate, and the
  `//:Cargo.lock` in the tree was the **extended** one run5a had written (both crates pinned with
  checksums). **`Build completed successfully, 231 total actions`, 33.9 s, offline.** The extension
  really did re-run (MEASURED — the `MODULE.bazel.lock` it wrote records
  `ENV:CARGO_BAZEL_ISOLATED false` and `FILE:@@//Cargo.lock c2bf7544…`, both different from the
  previous entry), and cargo needed nothing from the network (MEASURED — zero files under
  `$Q/cargohome` are newer than run5a's log).
- **run5c** — identical, except `//:Cargo.lock` restored to the **seeded** bytes the harness
  actually publishes (`heck` only, no `hex`). **FAILS**, 24.9 s:

  ```
  error: failed to get `heck` as a dependency of package `acme-case-rs v0.2.0 (/tmp/.tmplW5vGu/rust/acme-case-rs)`
  Caused by: download of config.json failed
  Caused by: failed to download from `https://index.crates.io/config.json`
  Error: Failed to generate lockfile
  Caused by: Failed to fetch crates for lockfile: exit status: 101
  ```

**Verdict: a persistent `CARGO_HOME` is NOT sufficient for this harness.** It covers the `.crate`
downloads, not the *resolution*: whenever the root `Cargo.lock` is incomplete — which is always,
because `union_workspace_files` seeds it from ONE contributing repo and `_publish` re-asserts those
seeded bytes (`rust.py:149-157`, `cli.py:5077`) — cargo must contact `index.crates.io` before it can add the other
repo's crate, and a warm cache does not answer that. The very property `rust.py`'s "the lock KEEPS
its `carry_from`" docstring relies on (cargo *extends* a partial lock) is what forces the network
call. `CARGO_NET_OFFLINE=true` would refuse rather than resolve; **UNKNOWN** whether it can be made
to work, and it would break the warming run by construction.

### Q1.6 Recommendation — narrowest change first

1. **For the test (smallest change, highest confidence): give the test's monorepo a
   `MODULE.bazel.lock` before `fleet build` runs.** The e2e helper `real_build`
   (`tests/test_build_e2e.py:4377-4431`) already commits a `.bazelrc` into the monorepo for exactly
   this class of reason ("stop a network symptom from impersonating a defect in `src/`"). Committing
   a checked-in `MODULE.bazel.lock` beside it — generated once by a networked run of this same test
   and stored as a fixture — makes the `crate` extension replay instead of splice, which is the only
   step that touches crates.io. It is *falsifiable and self-correcting*: if the lock ever stops
   matching (a `rules_rust` bump changes `bzlTransitiveDigest`, a fixture edit changes a
   `FILE:` hash), Bazel's default `--lockfile_mode=update` silently falls back to today's behaviour
   rather than failing — so the change can only remove flakiness, never add a new failure mode.
   **Caveat, stated because it is the weak point:** the lock is only valid while `MODULE.bazel`,
   `//:Cargo.toml`, `//:Cargo.lock` and both member manifests are byte-identical, and `MODULE.bazel`
   is *generated per fleet composition* — so this fixture is coupled to `only_repos(fleet,
   ["acme-codec-rs", "acme-case-rs"])`. A guard asserting the lock was actually used (e.g. that the
   build emitted no `Splicing Cargo workspace` progress line) is what keeps it honest.
2. **For production, in a later round: `crate.from_cargo(lockfile = "//:cargo-bazel-lock.json")`.**
   Same shape as `maven_install.json` for JVM. Needs a producer for that file, so it is an ADR, not
   an edit.
3. **Do not** point `verify.repository_cache` anywhere new for this. It is measurably not the
   mechanism.

### Q1.7 What Q1 does NOT prove

- No claim is made about the **JVM/Python/Go** extensions; Q1.2's generalisation is INFERRED.
- The measurements were made on a **hand-built reproduction** of what the harness emits, not by
  running the e2e test. The reproduction's `MODULE.bazel` was generated by the harness's own
  `render_module_bazel` over two `BuildUnit`s (`heck`/`hex`), and its `BUILD.bazel` files by the
  harness's own `render_build_bazel`, so the Bazel-facing inputs are the harness's; the *pipeline*
  around them was not exercised.
- Network blocking was done with `http_proxy`/`https_proxy` pointed at a closed port
  (`127.0.0.1:9`), not a network namespace: **unprivileged `unshare -rn` is denied on this host**
  (MEASURED — `unshare: write failed /proc/self/uid_map: Operation not permitted`), and there is no
  passwordless `sudo`. A dependency that ignores proxy env would therefore look "offline-clean"
  here. cargo honours it (the run4 error names the proxy), Bazel's downloader honours it.
- **UNKNOWN:** whether the `MODULE.bazel.lock` a *containerised* (`--network=none`) verify run needs
  differs from the host-run one. Not tested; §32's "no offline container build has been attempted"
  still stands.

---

## Q2 — `completed_units=["build"]` and its two discard points

### Q2.1 Where it is produced

**MEASURED** — `src/fleet/workers/buildverify.py:969-977`: on a non-`ok` step, `BuildverifyWorker.run`
returns

```python
return WorkerResult[BuildverifyOutput](
    status="timeout" if result.timed_out else "failed",
    output=output,
    completed_units=completed,        # ["build"] when the TEST step is the one that failed
    remaining_units=units[index:],    # ["test"]
    …
)
```

`completed` is appended per unit at `:978`, and `units = [BUILD_UNIT] + ([TEST_UNIT] if
payload.run_tests else [])` (`:894`). So on a test-step failure the result really does carry
`completed_units=["build"], remaining_units=["test"]`.

Note the status is **`failed`**, not `partial` — permitted, because
`WorkerResult._status_matches_its_evidence` (`workers/base.py:389-405`) only forbids `partial`
*without* `completed_units`, never `failed` *with* them.

### Q2.2 Discard #1 — `BuildWorker._handoff` never reads it

**MEASURED** — `src/fleet/cli.py:5361-5394`. The sub-result arrives as `step` and the only thing
taken from it is `status`, `error` and `usage`:

```python
done = list(dict.fromkeys(landed))                       # cli.py:5375 — `landed` is the OUTER
remaining = [unit for unit in units if unit not in set(done)]
status = "failed" if step.status == "partial" else step.status   # cli.py:5377
```

`landed` is built from `BUILD_UNITS = (GENERATE_UNIT, VERIFY_UNIT, PUBLISH_UNIT)` (`cli.py:4497`,
consumed at `:4948`). `step.completed_units` is **never referenced**. Two independent losses here:

- the inner `["build"]` is dropped, and
- `partial` is **downgraded to `failed`** at `:5377` — which matters for discard #2. (The
  `TransformWorker._handoff` at `cli.py:3384-3421` does the opposite: it returns `status="partial"`
  when `done` is non-empty. The Build pipeline's copy deliberately does not.)

### Q2.3 Discard #2 — the runner only checkpoints `partial`

**MEASURED** — `src/fleet/orchestrator/runner.py:509`:

```python
if result is not None and result.status == "partial":
    await self._save_checkpoint(repo_id, result, checkpoint, ladder.attempts + 1)
```

`_save_checkpoint` (`runner.py:997-1016`) is the only writer of `PhaseCheckpoint`. Because §Q2.2
turned the result into `failed`, this branch is not taken, so **even the outer
`completed_units=["generate"]` is discarded** — not just the inner `["build"]`. The build phase
therefore never writes a checkpoint on a failure of any kind; it writes one only on the
cancel/deadline path, where `_interrupted` returns `partial` (`buildverify.py:946`, `_interrupted` at `:1132`).

### Q2.4 What would consume it if it survived — and the answer is "nothing, yet"

**MEASURED.** The consumption path is `_save_checkpoint` → `checkpoints.save` →
`_load_checkpoint` (`runner.py:985-995`) → `_re_entry` (`runner.py:735-747`) → the phase payload's
`remaining_units`. `BuildInput` has that field (`cli.py:4829`) and `BuildPipelineWorker.run` honours
it (`cli.py:4948-4949`: `owed = set(units) if payload.remaining_units is None else
set(payload.remaining_units)`).

**But `BuildverifyInput` has no `remaining_units` and no `completed_units` field at all** (MEASURED —
`buildverify.py:543` onward; `WorkerInput` is a bare marker base, `workers/base.py`, with no unit
fields). `_buildverify_input` (`cli.py:5039`) therefore has nothing to put a surviving `["build"]`
into.

**So: preserving `completed_units` alone would NOT let a test-step failure skip the successful
build step.** It would take three changes, not one:

1. `BuildverifyInput` gains a `remaining_units`/`completed_units` field, and
   `BuildverifyWorker.run` computes `units` from it instead of unconditionally from `run_tests`
   (`buildverify.py:894`);
2. `BuildWorker._handoff` propagates the sub-result's units into that field — which also means
   deciding what a **two-level unit vocabulary** means, because `PhaseCheckpoint.completed_units`
   is currently a flat list of *phase* units (`generate`/`verify`/`publish`) and `"build"` is a
   *step* unit. Putting `"build"` into the same list would be read by
   `_handoff`'s `remaining = [u for u in units if u not in set(done)]` as an unknown name — harmless
   there, but it makes `cli.py:1048`'s stated invariant ("`completed_units` is a list of *steps*")
   ambiguous at the Build layer;
3. `runner.py:509` must checkpoint a `failed`-with-`completed_units` result, or `_handoff` must stop
   downgrading `partial`.

### Q2.5 On the predecessor's conclusion

The predecessor said replaying a build after a test-step exit 125 is *sound but wasteful*, costing
one warm build since `--rm` kills the output base anyway. **I agree with the verdict and would
sharpen the cost estimate**: the replay is not one warm build, it is one **cold** build wherever the
output base did not survive, and for a Rust repo that is the whole `rust_toolchain` again
(`test_build_e2e.py:4597` records a Rust output base as ~1.5 GiB). On this host a full cold Rust
build of the two-crate fixture is **36.7 s** with a warm repository cache (MEASURED, run1) and
would be minutes without one. That is a real cost, but it is a *performance* cost, not a
correctness one — and against it stands a three-part change that crosses a vocabulary boundary.
**Agent Recommendation: leave it, and record the reason, rather than half-preserve it.** A
`completed_units` that survives discard #1 but dies at discard #2 buys nothing and makes the
checkpoint contract harder to read.

---

## Q3 — what a real Bazel failure log costs through `util/proc.py`'s tail

### Q3.0 Method

Both measurements below drive `fleet.util.proc.run` — the real function, not a re-implementation —
over a real `bazel build //rust/... --keep_going` against the Rust fixture crates with real `rustc`
errors introduced into their sources. `LOG_TAIL_BYTES = 32_768` (`models/base.py:10`). Token figures
use **the harness's own estimator**, `estimate_input_tokens`'s `len(text) // 4`
(`src/fleet/llm/client.py:431`) — deliberately not a vendor tokenizer, matching what the budget gate
itself uses. Script: `$S/q3_measure.py`.

### Q3.1 The small case — a realistic single-repo failure fits entirely

**MEASURED.** Two crates, 8 `rustc` errors (an unresolved import, a wrong-type return, a missing
method, etc.):

| field | value |
|---|---|
| `exit_code` | 1 |
| `stdout_bytes` (full) | **0** |
| `stderr_bytes` (full) | **7,078** |
| `stderr_tail` bytes kept | **7,078** |
| `stderr_truncated` | **False** |
| fraction surviving | **100 %** |
| est. tokens (chars // 4) | **1,769** |

**Bazel writes essentially everything to stderr; stdout was empty.** The `stdout_tail` half of the
`ProcResult`/`attempts` row cost nothing at all here.

### Q3.2 The large case — 79 KiB, and 59 % is dropped

**MEASURED.** Same build with 60 additional generated source modules in `acme-codec-rs`, 3 real
errors each:

| field | value |
|---|---|
| `stderr_bytes` (full) | **79,337** |
| `stderr_tail` bytes kept | **32,792** (32,768 window + a 24-byte `[truncated 46569 bytes]` marker) |
| fraction of the log surviving | **41.3 %** (46,569 bytes dropped) |
| est. tokens, kept tail | **8,198** |
| est. tokens, whole log | **19,834** |
| `error[…]` diagnostic headers in the full log | **244** |
| `error[…]` headers inside the kept 32 KiB window | **92** (**37.7 %**) |

**The dropped 59 % is the causally-first half.** The tail keeps the end of the log; under
`--keep_going` with two failing targets, the *first* target's diagnostics are at the head. The
first two errors in the full log are

```
11:error[E0432]: unresolved import `heck::ToSnakeCaseMissing`
17:error[E0599]: no method named `to_snake_case_missing` found for reference `&str` …
```

— i.e. the whole of `acme-case-rs`'s failure — and neither survives. What *does* survive is
`error: aborting due to 240 previous errors`, the `rustc --explain` hint, `INFO: Elapsed time` and
`ERROR: Build did NOT complete successfully`: the summary, not the cause.

### Q3.3 A defect found while measuring: the tail is truncated twice, and the marker nests

**MEASURED.** `_read_tail` returns 32,768 bytes **plus** its `[truncated N bytes]` marker = 32,792
bytes, which is over `LOG_TAIL_BYTES`. `WorkerError.stderr_tail` is a `TruncatedStr`
(`workers/base.py:352`), so persisting it re-truncates:

```
full size 79337  tail chars 32792  tail bytes 32792
tail ends with: 'Build did NOT complete successfully\n\n[truncated 46569 bytes]'
persisted chars 32779  bytes 32779
persisted ends with: '…ERROR: Build did NOT complete successfully\n\n[truncated 46569 bytes]\n[truncated 24 bytes]'
```

The second pass eats 24 bytes off the **front** of the already-truncated window and stacks a second
marker. Cosmetic in size (24 bytes), but the stored evidence now says `[truncated 46569 bytes]`
followed by `[truncated 24 bytes]`, which reads as two separate losses. **Not filed as a D-number —
that is the record-owner's call, and another session holds `docs/INTEGRATION_HONESTY.md`.**

### Q3.4 What this means for ADR-0069 §4 candidate 1

The number §37 said nobody had: **for a failure the size this harness's own fixtures produce, the
truncation costs nothing (7 KiB, 100 % kept, ~1.8k tokens). It starts costing at ~33 KiB of stderr,
which this fixture reached at roughly 244 rustc diagnostics; at 79 KiB it drops 59 % of the bytes
and 62 % of the diagnostics, and the half it drops is the half naming the first broken target.**

Two honest consequences, both **INFERRED** from the above:

- The *token* case for capture-at-source offload is weak: 8,198 tokens is not the problem. The
  *diagnostic* case is strong — losing the first failing target's errors is a repair loop reading
  the wrong end of the log.
- A **head+tail** split (keep the first N KiB and the last N KiB) would have retained both first
  errors *and* the summary in the 79 KiB case, at the same 32 KiB budget, and is a strictly smaller
  change than an offload mechanism. Labelled *Agent Recommendation* per Guardrail 1; unmeasured, and
  it would need `_truncate_tail`'s contract renegotiated, which is an ADR.

### Q3.5 What Q3 does NOT prove

- Both logs came from **Rust** builds. A Gradle/JVM or a `go build` failure has a different shape and
  a different size distribution; **UNKNOWN** here.
- The 79 KiB case used generated sources. The *errors* are real `rustc` output from a real build, but
  no repo in this fleet is that broken today; treat 79 KiB as an upper-ish bound the fixtures can
  reach, not as an observed production log.
- I did not measure a **timeout** or a **container** failure, where `stderr_tail` is set by the
  harness rather than by Bazel and is tiny by construction.

---

## Appendix — the runs, in order

| run | what changed | result |
|---|---|---|
| run1 | fresh output base, warm hardlinked `--repository_cache`, empty `HOME`, network up | success, **36.7 s**; `//:Cargo.lock` extended with `hex`; `$HOME/.cargo` never created |
| run2 | fresh output base, same warm cache, **`MODULE.bazel.lock` from run1 present**, `//:Cargo.lock` restored to seeded bytes, **network broken** | **success, 32.4 s** — `cargo` never ran (`//:Cargo.lock` not re-extended) |
| run3 | `MODULE.bazel.lock` removed, network broken | stalled at `Computing main repo mapping` (registry unreachable); killed — not a useful discriminator |
| run4 | `MODULE.bazel.lock` present, `//:Cargo.lock` invalidated by one comment line, network broken | **`Failed to fetch crates for lockfile: exit status: 101`**, 26.2 s |
| run5a | `CARGO_BAZEL_ISOLATED=false`, persistent `CARGO_HOME`, network up | success, 36.6 s; `CARGO_HOME` populated with the index and both `.crate` files |
| run5b | same, network broken, extension re-evaluated, tree holding the **extended** `Cargo.lock` | **success, 33.9 s** offline |
| run5c | same, network broken, tree holding the **seeded** (incomplete) `Cargo.lock` — the shape the harness publishes | **`Failed to fetch crates for lockfile: exit status: 101`**, 24.9 s |
| q3a | real failing `bazel build --keep_going`, 8 rustc errors | 7,078 B stderr, 100 % kept |
| q3b | same with 60 generated broken modules, 244 rustc errors | 79,337 B stderr, 41.3 % kept |

Commands are reproduced verbatim in the transcript. The workspaces (`$Q/ws`, `$Q/wsfail`), every
`run*.err`, the captured failure logs under `$Q/q3logs/` and `$Q/MODULE.bazel.lock.saved` remain
under `$Q` for anyone re-deriving these numbers; the Bazel **output bases** (`$Q/out*`) were reaped
afterwards, because this host was at 96 % full and they are re-derivable from the hardlinked
repository cache in ~35 s each.
