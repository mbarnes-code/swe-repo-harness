# Integration honesty: what this suite actually proves

**Purpose.** Every external tool the harness shells out to is listed below exactly once, with an
unflattering verdict. The value of this document is entirely in its honesty: a green suite that
overstates its reach is worse than a red one, because it converts "we have not tested this" into
"we have tested this" without anybody deciding to.

**Verdict vocabulary — used strictly.**

| Verdict | Means |
| --- | --- |
| **REAL** | The genuine binary/service executed and the assertion is on its *effect* (a rewritten tree, an analysed target graph, a resolved module graph), not on the fact that it exited 0. |
| **REAL, TRIVIAL** | The genuine binary executed, but on an input so small the test cannot distinguish a working integration from a broken one under load. |
| **FAKE** | Proven only against an injected `CommandRunner`/`ModelClient`. What is real is our argv, our exit-code handling, our parsing and everything downstream. What is not real is that any tool looked at anything. |
| **UNPROVEN** | No test exercises this path at all, in either mode. |

Environment these verdicts were measured in (2026-08-11): `bazel` = `tools/bin/bazel` (bazelisk →
**Bazel 9.2.0** — and now the version the end-to-end fixture pins too, see D8), `gh` =
`tools/bin/gh` **2.97.0**, `git-filter-repo` = `.venv/bin/git-filter-repo`
(**a40bce54**), `git` 2.43.0, `docker` server 29.6.2, `curl` on PATH, `uv` and `pnpm` as the two
dependency resolvers, a live **Gitea 1.25.4** at `http://localhost:3001` answering
`/api/v1/version` **200**,
its API token in the gitignored `.secrets/gitea-curl.conf` (observed `0600`; see the Gitea row —
the harness does not check that), and **no host JDK** (`java`
is not installed — Bazel does not need one, its release embeds a JVM). `tests/conftest.py` puts
`tools/bin` and `.venv/bin` on `PATH` and points `BAZELISK_HOME` inside the workspace, all derived
from the repo root.

**Correction to the previous revision's environment line, and it is the most important sentence in
this header.** It recorded "network to `bcr.bazel.build` reachable". It is **not** reachable from
this host: the socket is accepted and then stalls, so Bazel reports `Error accessing registry …:
Connect timed out` rather than a refusal. The real-Bazel tests had a guard that turned that into
`pytest.skip` — so **the only real check on generated Bazel output stopped running while the suite
stayed green**, which is this document's own thesis happening to this document. Fixed on two
levels: `build.registry` (`settings.py`) is a configured input that reaches every `bazel` command
line as `--registry=`, and `tests/conftest.py`'s `bazel_registry` fixture probes the pinned
registry, then `build.registry`, then `BCR_DEFAULT_REGISTRY` and `BCR_MIRROR_REGISTRY`, and
**fails — naming every URL probed and the exact error each returned — rather than skipping** when
none answers. Degrading to a skip now requires somebody to say so out loud with
`FLEET_TEST_ALLOW_OFFLINE_BAZEL=1`. **A skip reads as a pass at a glance; that is the whole
mechanism, and no verdict below may rest on one.**

**This revision.** Defects **D1–D4** were fixed two passes ago and **D5–D11** in the last one
(**D5 withdrawn** — it was never a defect of its own). This pass closed **D12** and the
end-to-end real-Bazel test now puts **all 4 fixture repos** through: `fleet build` exits 0, and
the test additionally runs `bazel build //...` over the `integration` tree itself and asserts
Bazel's own exit status and "Build completed successfully". **The `xfail(strict=True)` is
deleted**, so that test is a guard rather than a pinned failure. The honest headline is *not*
"the build layer is proven": it is that four three-file repos in two languages build, that the
`bazel query`/`rdeps` boundary is still fake, that nothing here compiles go, jvm or rust, and
that the largest boundary in the harness (`ast-grep`) is still at zero coverage for the fifth
checkpoint running.

**Corrected 2026-08-13 (§24) — the clause above reading *"nothing here compiles go, jvm or rust"*
is now FALSE for rust.** Two Rust fixture repos (`acme-codec-rs`, `acme-case-rs`) are migrated by
`fleet build` and a real `bazel build //...` over the integration checkout exits **0** with
`Build completed successfully`, producing **real `.rlib` artifacts for both crates**. **go and jvm
are unchanged and still compile nothing.** See the new `rust` ledger row, the Rust-build addendum
below, and `docs/PROGRESS.md` §24. The environment line above also gains a **workspace-local Rust
toolchain**: `rustup-init` installed **1.97.1, pinned explicitly** (not the floating `stable`
channel, so the toolchain is a build input rather than a date) with `CARGO_HOME`/`RUSTUP_HOME`
under `tools/rust/` — minimal profile, host target only, **616 MB**, no sudo, and `~/.cargo` and
`~/.rustup` **confirmed absent before and after**. `tools/bin/cargo` and `tools/bin/rustc` are
self-locating POSIX `sh` wrappers, so **no `conftest.py` change was needed**.

**Corrected 2026-08-15 (§27) — the same clause is now FALSE for go as well, and the sentence
directly above reading *"go and jvm are unchanged and still compile nothing"* is itself now half
wrong.** Real **Bazel 9.2.0** **analyses and compiles** a generated Go tree built from **the
harness's own unmodified output** — `INFO: Analyzed 4 targets (116 packages loaded, 9178 targets
configured)`, `GoStdlib`, `GoCompilePkg`, `INFO: Build completed successfully, 21 total actions`,
and real **`.a` archives** for `digest`, `command` and `clitool_lib` — with the **cross-repo edge
resolved at analysis time** (`cquery deps(//go/clitool/internal/command:command, 1)` returns
**`//go/digest:digest`**). **jvm is unchanged and still compiles nothing.** Read the scope with
care and it is deliberately narrow: the tree is **assembled from generated bytes**, not published by
`fleet build`; **two** repos; **linux/amd64** only. See the Bazel-Go addendum below, the rewritten
`gazelle` ledger row, and **D19**.

**Counts are cited, not re-measured.** The suite's pass/xfail/skip figures for this round are
recorded once, in `docs/PROGRESS.md` §16, and are not repeated here. What matters for *this*
document is the caveat attached to them: that run predates an in-flight edit to `src/` and
`tests/`, and **this revision did not re-run the suite**. Every verdict below is read off the code
and off that run, so any row whose test the in-flight edit touches is stale until the next full
run.

**Addendum — the `ast-grep` round (same date).** `tools/bin/ast-grep` **0.45.1** joins the
environment line above, installed from the release asset `app-x86_64-unknown-linux-gnu.zip` with no
sudo and nothing on the host. **The counts were re-measured this time** —
`.venv/bin/python -m pytest -q` → **1032 passed, 0 failed, 0 skipped**, `mypy src/fleet/ --strict`
clean over 106 files, `ruff check src/ tests/` clean — so the "did not re-run the suite" caveat
above belongs to the revision that wrote it, not to the `ast-grep` row; the figures and the
discrepancy in the incoming baseline are recorded once, in `docs/PROGRESS.md` §18. **Three sentences
elsewhere in this file are superseded and are left standing rather than rewritten**, because what
they say about the cost of the gap is the argument that closed it: *"the largest boundary in the
harness (`ast-grep`) is still at zero coverage for the fifth checkpoint running"* (this header) and
*"is at **zero** for the fifth checkpoint in a row"* (the closing summary) are answered by the row
below, which now reads **REAL, TRIVIAL**; and **D12(a)**'s *"the only rewrite rule this project has
ever run end to end is wrong"* is no longer *only* — §7c's rules run through `RewritePipeline` into
a real worktree, though none of them has yet been through a fixture build. **The registry
paragraph's rule was applied verbatim on the new boundary:** no `pytest.skip`/`skipif` was added for
a missing `ast-grep`. `tests/conftest.py:47,66` already prepends `tools/bin` to `PATH`,
`shutil.which("ast-grep")` resolves under the test session, and an absent binary turns the suite
red. The rule that ended the registry skip is now the rule on the newest boundary too.

**Addendum — the root-lockfile round (same date), and it falsified a prediction this file carried
for five checkpoints.** Counts re-measured: `.venv/bin/python -m pytest -q` → **1047 passed, 0
failed, 0 skipped, 0 xfailed, 0 xpassed**, `mypy src/fleet/ --strict` clean over **106 files**,
`ruff check src/ tests/` clean; **zero `xfail` markers remain anywhere in `tests/`**. Three things
this file said are now wrong and are corrected in place rather than left standing, because unlike
the `ast-grep` sentences above they are not superseded-but-instructive — they are **inaccurate about
a mechanism**: (i) the JS row's and D12's *"two JS repos with different external deps emit
conflicting `npm_translate_lock` tags"* — **they do not**; the tags are byte-identical constants
that `render_module_bazel` collapses, and the real conflict was the root lock's *content* (**D13**);
(ii) the `bazel build` row's implicit reading that `fleet build` exiting 0 means the monorepo
builds — **it does not**, and that is how both of this round's defects stayed invisible; (iii)
Python was not exempt (**D14**). **The `pytest.skip` rule was applied again and again held:** the
new two-repo tests carry `pytest.mark.integration` and a `skipif` on *bazel not installed on this
host*, which is the same shape the existing real-Bazel tests use — no new skip was added for a
defect, only for an absent toolchain. **One new operational fact belongs in this header because it
can silently corrupt a verdict:** `tests/conftest.py`'s session-end reaper deletes everything under
the shared `BAZEL_ROOT` except `repos/` and fails the session on residual bytes, so **two concurrent
pytest sessions destroy each other** — one agent's `--collect-only` reaped another's in-flight suite
(`DISK CEILING BREACHED: 221577094 bytes ... survived the session`). Full-suite verification must be
**serialized across agents**; a figure from a run that overlapped another session is not evidence.
Relatedly, the intermittent single-test failures earlier revisions treated as flakes are **closed
and were never defects**: the captured traceback is `java.net.SocketTimeoutException: Read timed
out` fetching `https://nodejs.org/dist/v22.22.0/node-v22.22.0-linux-x64.tar.xz` — a transient live
fetch in tests that deliberately **fail rather than skip**, per the registry paragraph above. Run
time fell from ~39 min to ~6 min once the Bazel repository cache warmed.

**Addendum — the Rust + Go root-file round (2026-08-12), and the honest verdict for both ecosystems
is the same three words: *plumbing-verified, resolution- and build-UNPROVEN*.** Counts are
**reported, not orchestrator-confirmed**: the implementing agent measured
`.venv/bin/python -m pytest -q` → **1057 passed, 0 failed, 0 skipped, 0 xfailed, 0 xpassed**,
`mypy src/fleet/ --strict` clean over **106 files**, `ruff` clean, zero `xfail` markers in `tests/`
— and the orchestrator's independent confirmation run **was still in flight when this revision was
written**, so per this document's own rule (§"Counts are cited, not re-measured") the figure carries
that caveat until `docs/PROGRESS.md` §21's successor removes it.

- **Rust.** The root `Cargo.toml` is now a real workspace computed over the whole unit set
  (`members` = every contributing dest) with its `carry_from` **dropped unconditionally** — a
  repo's own `[package]` manifest has no `[workspace]` table, so rules_rust's splicer took the
  `Package` branch and only one crate's dependencies reached the `@crates` hub — and every member
  now has a declared `<dest>/Cargo.toml`. `Cargo.lock` **keeps** its carry, deliberately:
  `crate_universe`'s `LockGenerator::generate` runs `cargo fetch` without `--locked` when a lock
  exists, so cargo repairs and extends a partial lock, and ADR-0049's "drop the carry at ≥2" would
  throw pins away here. **All of that is asserted against our own rendered strings in
  `tests/test_ecosystems.py`.** No `resolution()` exists for Rust — **`cargo` and `rustc` are
  absent from this host** — so there is no `cargo` row in the ledger below and should not be: the
  harness shells out to nothing for Rust. **Zero Rust fixture repos; nothing here compiles Rust.**
  **Corrected 2026-08-13 (§24) — the last sentence is now false in both halves.** There are **two**
  Rust fixture repos and this project **compiles Rust for real**. Read the rest of this bullet with
  care, because two of its clauses survive and one does not: **"No `resolution()` exists for Rust"
  is still true** (`rust.py` declares none, and the harness still shells out to no resolver for
  Rust — `crate_universe` runs cargo *inside* Bazel), and **"`cargo` and `rustc` are absent from
  this host" is still literally true of the host** — they are now present **workspace-locally**
  under `tools/rust/` with nothing in `~`. What is false is the conclusion drawn from them. There
  **is** a Rust row in the ledger now, and it is about `bazel build`, not about a resolver.
- **Go.** See the `gazelle` row, which now carries the landed `//:go.sum` + `Resolution` and their
  boundary. A standalone `go` resolver row is **deliberately not opened yet**: the only evidence is
  against an injected runner, and a row implying the ledger's `uv` / `pnpm` standard would overstate
  it. That omission is itself a gap in this document and is named here so it is not silent.
- **One real hole in the suite was found and closed, and it is the round's most transferable
  finding.** The per-ecosystem "every file the generated files name exists" test built its payload
  through a helper that ran **only the carry step**, while the buildgen materializer writes every
  `SupportFile.content` unconditionally — so a `carry_from=[]`, `content=""` `go.sum` landed as a
  **0-byte file** with every assertion still green. A file that exists and says nothing passed an
  existence check. Fixed by applying the resolve step in the helper for a declared `Resolution`
  whose lock has no floor, and by a class guard in
  `test_every_file_the_generated_files_name_exists_after_the_phase_that_writes_them`
  (`tests/test_workers_build.py:1695`) that no materialized support file is empty — the general fix,
  since `go.sum` is named by **no label** and the `//:`-reference walk cannot see it.
- **The live-network flake class is ongoing, not closed.**
  `test_two_python_repos_with_different_pypi_dependencies_both_build` failed once on
  `Download from https://pypi.org/simple/keyring/ failed: Connect timed out` and passed on retry in
  96s; the same class explains the earlier unexplained Bazel failure with an empty `FAILED:` line.
  A single red run on a network-touching test is not yet evidence of a defect — and a green one
  over a fetch that stalled is exactly what the registry paragraph above exists to forbid hiding.

**Addendum — the Rust build round (2026-08-13), and the headline is a correction rather than a
verdict: this document's long-standing *"nothing here compiles go, jvm or rust"* is now false for
rust.** Counts are **reported, not orchestrator-confirmed**: the implementing agent measured
**1079 passed** (1077 baseline + 2 new), 0 failed, 0 skipped, 0 xfailed, 0 xpassed, in **504s**;
`mypy src/fleet/ --strict` clean over **106 files**; `ruff` clean; zero `xfail` markers in `tests/`
— and **the orchestrator's independent confirmation run was still in flight when this revision was
written**, so per this document's own rule the figure carries that caveat until `docs/PROGRESS.md`
§24's successor removes it. For context the **three prior rounds WERE** orchestrator-confirmed, at
**1069**, **1076** and **1077**.

- **What is newly REAL.** Two Rust fixture repos, migrated by `fleet build`, **built by real
  Bazel**: exit 0, `Build completed successfully`, `rust_library` targets, **`.rlib` artifacts for
  both crates**, first attempt, no retry, no flake. See the new `rust` ledger row for exactly what
  that does and does not cover.
- **Three defects, found on the fixtures' first run — D15, D16, D17 below.** Two are ordering bugs
  that four checkpoints of Rust plumbing could not see, because every assertion in those rounds
  compared our rendered strings to our own expectations. This is the **third** time in this document
  that *adding a second repo to an ecosystem* found a defect that reading did not.
- **One defect is only half-fixed and it is deliberate.** D15's **cross-wave residue is unfixed**
  and awaits a **human decision** (ADR-0053 consequence 3). Both new tests assert only the
  **intra-wave** property, and the new guard deliberately checks only the repos a wave dispatches.
  Recorded loudly here so no future reader mistakes "D15 FIXED" for "ordering is solved".
- **A containment finding worth transferring beyond Rust.** The raw rustup shim, invoked with a bare
  `PATH`, does not merely fail — it **creates `~/.rustup` first**. Symlinking it the way `bazel`,
  `gh` and `ast-grep` are symlinked would therefore have **written outside the project on every
  call**, violating CLAUDE.md §2 workspace containment with **no test able to notice**. The
  `tools/bin/cargo` and `tools/bin/rustc` wrappers exist for that reason, not for ergonomics.
- **The disk ceiling held and was not raised.** The new Rust test first drove session peak to
  **6.59 GiB**, breaching conftest's **6 GiB** `BAZEL_PEAK_CEILING_BYTES` (which sets a non-zero
  exit status). Rather than moving the ceiling, the test **scopes its `fleet build` to the two Rust
  repos** and reaps dead output bases; **full-suite peak went 4.45 GiB → 3.26 GiB**, below where it
  started. A ceiling that gets raised whenever it fires is not a ceiling.
- **Go is unchanged, and is explicitly still UNPROVEN.** No gazelle run, `generate_targets()`
  returns `[]` for Go, **zero Go fixture repos**, no real `go` in the suite. A two-Go-repo build
  test would still **pass vacuously**. **Gazelle remains the prerequisite** — this is the fourth
  checkpoint carrying that sentence forward unchanged. **Corrected 2026-08-15 (§27): every clause
  in this bullet is now false.** Gazelle runs (§26), `generate_targets()` is no longer the question
  because Gazelle writes the targets, there are **two** Go fixture repos, a **real `go` and a real
  `gazelle`** are in the suite, and the two-Go-repo tree is **compiled by real Bazel** — the
  opposite of vacuous. **Go is no longer UNPROVEN.**
- **Also measured, and it bounds what an offline claim could mean here:** `cargo generate-lockfile`
  is **0.47s cold** for one crate and **0.59s** for a 27-crate tree over the sparse registry, and is
  **fully offline once the index is warm** in the workspace-local `CARGO_HOME`. That is the
  *fixture-generation* path; the **build** path still reaches crates.io.

**Addendum — the whole-fleet-ingest round (2026-08-14), and its headline is a correction this
document made four times over: *"two repos in different waves reproduce D15 verbatim"* was
WRONG.** Counts are **reported, not orchestrator-confirmed**: the implementing agent measured
**1085 passed** (1082 baseline + 1 un-xfailed + 2 new), 0 failed, 0 skipped, 0 xfailed, 0 xpassed,
in **19:31**; `mypy --strict` clean over **106 files**; `ruff` clean; zero `xfail` markers in
`tests/`; peak Bazel disk **3.26 GiB** against an **untouched 6 GiB** ceiling — and **the
orchestrator's independent confirmation run was still in flight when this revision was written**,
so per this document's own rule the figure carries that caveat until `docs/PROGRESS.md` §25's
successor removes it. The first full run hit the **known live-network flake** (`pypi.org` connect
timeouts inside `rules_python`'s `pip.bzl`, **445s** of retries) and was clean on re-run — the same
class the Rust/Go root-file addendum above records as ongoing.

- **The correction, and the mechanism is the finding.** Within one `fleet build` process the domain
  is **monotone**: a plan exists only after its repo's merge, and every later wave's snapshot
  descends from every earlier merge, so a later wave's worktrees contain **every earlier dest** and
  D15's fatal cargo shape (a `members` entry naming an absent directory) **is not reachable
  forward**. What actually survived cross-wave is the **milder D13 shape** — a wave settles
  **green** against root files a later wave replaces, and is **never re-checked**. **Corrected in
  place** in the D15 entry below, in ADR-0053 consequence 2, and in `docs/PROGRESS.md` §25. **Two
  test docstrings in `tests/test_build_e2e.py` still carry the wrong claim** and need a code
  change; §25 names them by grep string.
- **A new defect, D18, and it is the one this round exists for:** the published root files could
  **shrink** on a second `fleet build`, with **both invocations exiting 0**. Full entry below.
  Fixed by **ADR-0055**.
- **The environment line gains a workspace-local Go toolchain.** Go **1.23.4** — matching
  `go.py`'s `_GO_VERSION` SDK pin — under `tools/go/`, **SHA256-verified against go.dev's published
  digest before extraction**, with `tools/bin/go` a self-locating wrapper mirroring the
  `cargo`/`rustc` ones: `GOROOT`, `GOPATH`, `GOMODCACHE`, `GOCACHE` all inside the workspace,
  **293 MB, no sudo, nothing on the host, no `conftest.py` change needed**. `GOTOOLCHAIN` is
  pinned, so a `go.mod` demanding newer **fails loudly** — measured:
  `go: go.mod requires go >= 1.25 (running go 1.23.4; GOTOOLCHAIN=go1.23.4)`, exit **1** — versus
  system `go`, which silently fetches another toolchain.
- **A containment finding, and it is §24's rustup finding happening a second time.** Go 1.23 added
  telemetry whose directory resolves through `os.UserConfigDir()` with **no dedicated env var**, so
  a bare `go version` writes counters to `$HOME/.config/go/telemetry/`; system go 1.22.2 does not.
  Counters for **both `go1.23.4` and `go1.24.2` were already present, dated that day**, from this
  harness's own prior runs — the pre-existing `GOTOOLCHAIN=auto` path was **live-leaking into
  `$HOME`** with **no test able to notice**. The wrapper redirects `XDG_CONFIG_HOME` into the
  workspace and sets telemetry off. **Documented caveat rather than hidden:** `XDG_CONFIG_HOME` is
  **inherited by children**, so a `git` spawned for a VCS-fetched module will not see
  `~/.config/git/config`; the resolver's own path is **proxy fetches** and is unaffected.
- **Two Go fixture repos now exist, and they are DELIBERATELY VACUOUS — this is a statement of a
  gap, not of coverage.** `acme-clitool-go` (`github.com/acme/clitool`, `cmd/` + `internal/`,
  `spf13/cobra`) and `acme-digest-go` (`github.com/acme/digest`, flat, `golang.org/x/crypto`) sit
  in `POLYGLOT_REPOS` **opt-in by name**, so **no real-Bazel test touches Go**. Both **import**
  what they declare — Go makes an unused import a **compile error** and Gazelle derives `deps`
  purely from `import` statements, which **inverts** the JS/Python declared-not-imported convention
  and differs from Rust's reasoning (an unused `--extern` is legal). The pair exercises the two
  different `go_deps` label schemes (`@com_github_spf13_cobra` vs `@org_golang_x_crypto`), and the
  `go.sum`s are real (`tools/bin/go mod download all`). **No Go code is compiled by anything, no
  `go_library` has ever existed in a tree this harness produced, and the branch `go.sum` is a
  fake-resolver fixture. Go's verdict does not move: still UNPROVEN.**
  **Corrected 2026-08-15 (§27) — all three clauses of that last sentence are now false.** Go **is**
  compiled (real `.a` archives), `go_library` targets **do** exist in a tree this harness produced
  and real Bazel **analysed** them, and **Go's verdict moves to REAL**. What survives from this
  bullet is the honest boundary: the compiled tree is **assembled from the harness's generated
  bytes**, not published through `fleet build`, so the branch-publish path is still fake-covered.
- **A tripwire guards the vacuity, and it is designed to fail the day Gazelle lands.** It asserts
  the generated Go `BUILD.bazel` contains **no `go_library(`, `go_binary(`, `go_test(`,
  `go_proto_library(` or `load(`** and **no non-comment payload lines**, guarded by **positive**
  assertions that the `# gazelle:` directives *are* present — so it cannot pass by the file being
  empty or absent. Its docstring **names the replacement assertion**, so wiring up Gazelle turns
  this test red and forces someone to make it real. That is the intended mechanism, and it is the
  opposite of the vacuous green this document exists to catch.
- **A read-only, user-authorized Gitea corpus survey bounds what the Go plumbing means.** **25 Go
  repos, 68 non-vendor `go.mod` files** across ~267 bare repos; dominant layout `cmd/` +
  `internal/`; vendoring rare (2 of 25); `go.sum` almost always present. **No Gazelle configuration
  exists anywhere in the corpus** — a corpus-wide grep found only false positives inside a vendored
  Rust crate's Starlark parser test fixtures — so `go.py`'s Gazelle contract has **no real-world
  precedent here to validate against**. And a hazard: corpus `go` directives span **1.15–1.25.7**
  with **13 of 68 modules declaring ≥1.25** (including **5 of the 9 root-`go.mod` repos**), while
  measurement under `GOPROXY=off` shows `1.23.4` and `1.24.2` resolve and **`1.25.0` fails**
  `toolchain not available`. Real corpus Go repos therefore require the SDK pin at **≥1.25.7** or
  they become `REQUIRES_HUMAN_INTERVENTION`. **No credential was read or logged; no repo `config`
  file was touched.**
  **Corrected 2026-08-15 (§29) — the "25 Go repos" figure is WRONG and it was low: the corpus
  carries 31.** A second read-only survey of the same **267** bare repos (again: **no credential
  read or logged, no repo `config` touched**) counts **31 Go repos**, of which **10 carry cgo**
  (~**6 distinct codebases** after dedup). **The other figures in this bullet — 68 non-vendor
  `go.mod` files, 13 of 68 declaring `go` ≥1.25, 9 root-`go.mod` repos, 2 of 25 vendoring — were
  computed over the 25-repo enumeration and were NOT re-measured over the corrected set**; they
  are left standing as historical figures, and the `≥1.25.7` SDK conclusion is unaffected in
  direction. **The 25 → 31 correction is repeated wherever this document states the figure; the
  §25 checkpoint text and ADR-0059's quotation of it are left verbatim as history.**

**Addendum — the Bazel-Go round (2026-08-15, three rounds in one session), and the headline is the
correction this document has been carrying for six checkpoints: *"nothing in this project compiles a
line of go"* is FALSE, and Go's verdict moves from UNPROVEN to REAL.** Real **Bazel 9.2.0** loads,
analyses and **compiles** a Go tree made of **the harness's own unmodified output** — Gazelle's
`BUILD.bazel` files, the root `MODULE.bazel`, the union `go.mod`, the resolved `go.sum` — producing
real **`.a` archives**, with the **cross-repo edge resolved at analysis time**. Recorded as
**ADR-0057** and **ADR-0058**, and in `docs/PROGRESS.md` §27.

- **Round 1 refused the tree, and the refusal was the finding.** Two `@pytest.mark.integration`
  tests were written over a tree **assembled from the harness's own generated bytes** — deliberately
  **not** driven through `fleet build`, because the failure happens inside **Phase 3's own per-repo
  `bazel build`**, so an end-to-end run would have asserted *the harness's exit code for the
  failure*, three retries deep, at one Go-sized output base per repo. **Three pin disagreements,
  none of them a generator bug:** (1) Gazelle writes `load("@io_bazel_rules_go//go:def.bzl", …)`
  while `render_module_bazel` emitted `bazel_dep(name = "rules_go")` with **no `repo_name`**, so the
  apparent repo was `@rules_go` — **`No repository visible as '@io_bazel_rules_go' from main
  repository`**, a **loading** failure hitting **every** Go repo, so the Go packages never became
  targets; (2) repairing it forced the next, since `GoAdapter.extension_bzl["go_sdk"]` spelled the
  module `@rules_go` while `library_bzl`/`binary_bzl`/`test_bzl` spelled it `@io_bazel_rules_go`,
  and **a `bazel_dep` has exactly one apparent name**, so fixing one broke the other; (3)
  `_GO_VERSION = "1.23.4"` against gazelle 0.52.2's `go_repository` tools requiring **≥1.24.12** —
  two individually-justified pins, **jointly impossible**, firing for any Go repo with any external
  dependency. **The agent stopped rather than forcing any of them green**, applying each as a text
  edit **inside the test**, one rung at a time, each asserted to clear the **specific verbatim
  error** before it — and with all three applied Bazel **already** analysed and compiled.
- **Round 2 (ADR-0057) fixed the apparent name on evidence, not taste.** Upstream `bazel-gazelle`'s
  own `MODULE.bazel` declares `bazel_dep(name = "rules_go", version = "0.59.0", repo_name =
  "io_bazel_rules_go")`, so agreeing with the generator is the canonical consumer shape — and **this
  codebase already believed it**: `tests/test_bazel.py`'s `_RULESET_LOAD_PROBES` maps
  `"rules_go" → "io_bazel_rules_go"` and **that** module file already loads under real Bazel. New
  `EcosystemAdapter.ruleset_repo_names` plus a registry union that **raises if two adapters
  disagree** (the `extension_bzls()` shape); `repo_name` is emitted **only** where it diverges from
  the module name, so no other ecosystem's attested bytes moved. **`# gazelle:map_kind` was
  rejected** — it would pay for the fix by rewriting generator output other tests attest, needs one
  directive per kind with **no coverage for kinds not enumerated**, and would leave this harness's
  spelling permanently divergent from the wider Go ecosystem. **An audit of js/py/rust/jvm found no
  equivalent mismatch — Go is the only one.** Result: `bazel query //go/...` exits **0** over
  unmodified harness bytes, and `bazel build` reaches **108 packages loaded / 8796 targets
  configured** before the remaining version conflict.
- **Round 3 (ADR-0058) raised `_GO_VERSION` 1.23.4 → 1.24.12, the minimum both floors accept.**
  Lowering gazelle was **checked and rejected on evidence**: gazelle 0.52.2 declares `go 1.24.12`;
  **rules_go 0.61.1's own `go.mod` declares `go 1.24.0`**, so the floor is **not gazelle's alone**;
  rules_go 0.61.1 **depends on** gazelle 0.51.3, which also declares `go 1.24.12`; every gazelle
  **v0.48.0–v0.52.2** declares `go 1.24.12`; and the newest below that floor is **v0.47.0**, which
  **predates the Bazel 9 fixes the D8 settings table already records**. The pin is load-bearing in
  **six** places and all six moved together: the `go_sdk.download` version, the resolver's
  `GOTOOLCHAIN` env, the union `go.mod`'s `go` directive, the vendored `tools/bin/go` wrapper **and
  its SDK**, the `tools/bin/gazelle` wrapper's pin, and the root `go.sum`. **The environment line
  gains Go 1.24.12**, installed exactly as §25's 1.23.4 was: official tarball, published **SHA256
  verified before extraction** (`bddf8e653c82429aea7aec2520774e79925d4bb929fe20e67ecc00dd5af44c50`,
  matched), extracted under `tools/go/`, **+18 MB (269 M → 287 M)**, no sudo, nothing in `$HOME`.
- **The root `go.sum` came back BYTE-IDENTICAL, and that is the correct result** — recorded because
  the opposite would have been the alarming one. A `go.sum` line is a **content hash of a published
  module zip**, and the build list MVS computes from an **unchanged `require` set** does not depend
  on the toolchain version; **only the `go` directive moved**. The lock bytes were **regenerated by
  the real declared argv**, not hand-edited.
- **A new defect, D19, and it is a correctness gap rather than a missing test:** `go_deps` **raises
  the harness's pins during MVS** and reports it only in a DEBUG line. Full entry below.
- **An adjacent gap found during the audit and deliberately left untouched:** `ecosystems/jvm.py`
  loads `@rules_java//java:defs.bzl` while `rules_java` is **deliberately absent** from
  `ruleset_versions` and from `toolchain_requirements()`, relying on Bazel's injection — and **no
  real-Bazel test ever loads a generated Java package**, so that visibility is **untested**. It is a
  **missing-declaration** question, not a naming one.
- **Counts, with the flake named rather than smoothed over.** Implementing agents reported **1096
  passed** after each round. The orchestrator's **independent confirmation run of the final tree hit
  the known live-network flake** —
  `tests/test_bazel.py::test_real_bazel_analyses_the_generated_js_binary`, whose guard
  **deliberately fails rather than skips** on a mid-run registry outage, which is the rule the
  registry paragraph in this header exists to enforce — and it **passed alone in 16s**, so the
  effective state is **1096 passed, 0 skipped, 0 xfailed, zero `xfail` markers**;
  `mypy src/fleet/ --strict` clean over **106 files**; `ruff` clean. **That flake has now recurred
  three times across this session's rounds and always passes in isolation** — the ongoing
  live-network class this document has tracked since the Rust/Go root-file round, and still not
  evidence of a defect.
- **Disk held without a ceiling being moved:** peak Bazel **3.94 GiB** against an **untouched 6
  GiB** ceiling; repository cache **1359 MiB** against its own **2 GiB** keep-ceiling.
- **What this does NOT prove, and it is deliberately most of the Go story.** **(1)** **Not the
  `fleet build` path end to end**: the tree is **assembled from the harness's generated bytes**, so
  `materialize`/`_publish` over **real** generator output is still covered **only by the fake**.
  **(2)** Nothing exercises **`go_test`** — neither fixture ships a `*_test.go`. **(3)** Only
  **linux/amd64** SDKs were fetched and hashed. **(4)** The fixture is **two repos, one cross-repo
  edge, five direct requirements** — nothing here says a fleet with **conflicting** Go module
  versions across repos resolves cleanly. **(5)** The path is **not reproducible offline**: BCR,
  `go.dev/dl` and `proxy.golang.org` were all reachable, and the resolve hit a **warm workspace
  module cache**.

**Addendum — the C-toolchain round (2026-08-15, §29), and it names a precondition EVERY Bazel
verdict in this document has silently depended on.** Every row below whose verdict rests on a real
`bazel` invocation — the four `bazel` rows, the `bazel build`/`bazel test` row, the `rust` row and
the `gazelle` row — was measured on a host carrying **`gcc` 13.3.0**, and **none of them declared
it**. Measured this round: **without a discoverable C compiler, ZERO Go targets analyse — cgo or
not**, because `@@rules_go+//:stdlib` itself depends on
`@@rules_cc++cc_configure_extension+local_config_cc//:cc-compiler-k8`. Verbatim:
`Auto-Configuration Error: Cannot find gcc or CC; either correct your path or set the CC
environment variable` → `ERROR: no such package
'@@rules_cc++cc_configure_extension+local_config_cc//'` → **both** `@@rules_go+//:stdlib` **and**
`//go/digest:digest` failing to fetch it → under **`--keep_going`**, which the verify worker
**defaults to**, `INFO: Found 0 targets`. Recorded as **ADR-0060** and in `docs/PROGRESS.md` §29.

- **The dependency was invisible because nothing here could see it.** The word **`cgo` appeared
  nowhere in `src/` or in any ADR** before this round; **neither Go fixture uses cgo**, so the
  suite was **structurally blind**; and the host's `gcc` meant every unsandboxed run resolved a
  compiler off the same `PATH` `bazel` came from. **A green Bazel result recorded anywhere in this
  document has always been conditional on an undeclared host compiler.** That is not a defect in
  any row's assertion — it is a **precondition none of them states**, and it is stated here now.
- **The failure is at repository-fetch/loading time**, via a Starlark `auto_configure_fail`, **not
  in the analysis phase**. `Found 0 targets` is the downstream *symptom*, and it is the shape this
  document exists to distrust: **a total refusal that reads like "nothing matched"**.
- **Corpus exposure, and the important number is the bigger one.** From a read-only survey of
  **267** bare Gitea repos (**no credential read or logged**): **31 Go repos** (correcting **25**,
  see the §25 bullet above), **10 of 31 carrying cgo**, ~**6 distinct codebases** after dedup —
  and two of those (**`beads`**, the largest by file count, and **`multi-agent-vllm`**) gate on
  **`//go:build cgo` with zero `import "C"`**, so **an `import "C"` grep misses them entirely**.
  **But the missing-compiler exposure is 31/31, not 10/31.** The 10/31 figure governs only the
  **second-order** problem: cgo packages additionally need headers and system libraries a minimal
  image will not carry (`ole32`, `crypt32`, `IOKit`, `sqlite3`), which the gate does not look for
  and which arrive as ordinary per-repo `BUILD_ERROR`s.
- **A gate landed in `buildverify`, and its own first implementation was wrong in both
  directions — recorded because it is this document's thesis happening to this round's fix.**
  `_c_toolchain_gate` fires **only when sandboxed**, **before the first `bazel`**, and refuses
  **non-retryably**. Its first probe was `command -v cc || command -v gcc || command -v clang`;
  Bazel's actual lookup is `_find_generic(ctx, "gcc", "CC", overridden_tools)`, so **`cc` is never
  searched** and **`clang` is never a lookup candidate**. **Gap A — false green:** a clang-only
  image **passed** a gate that exists to reject it. **Gap B — false non-retryable refusal:** an
  image with `ENV CC=/opt/toolchain/bin/gcc` was refused though Bazel would have succeeded. The
  corrected probe mirrors `_find_generic` clause for clause, and **its tests execute the probe
  against stub binaries rather than replying with a hand-chosen exit code** — a fake returning only
  an exit status **cannot tell a clang-only image from a gcc one**, which is the same
  fake-agrees-with-the-code failure D1–D4 were. Mutation-checked. **Three false statements in the
  gate's own operator message were corrected too** (it probes `gcc`/`$CC` only; it fails at
  loading time, not analysis; and the "(or set `CC`)" hint was **unactionable**, since
  `buildverify` passes **no `env`** to either container, so only an image-level `ENV CC` counts).
- **`BAZEL_DO_NOT_DETECT_CPP_TOOLCHAIN=1` must never be set**, and is set nowhere here: it
  silently substitutes an **empty toolchain**, manufacturing exactly the
  green-run-over-a-broken-tree result this document exists to catch.
- **OPEN GAP, and arguably larger than the compiler: the verify container's Bazel caches are
  MOUNTED BUT NEVER USED.** `verify.disk_cache` and `verify.repository_cache` are bind-mounted
  **RW** into the container, but `_bazel_argv` **never emits `--disk_cache=` or
  `--repository_cache=`** — **the mounts are inert**. With `--network=none`, a cold sandboxed run
  therefore **cannot resolve a single Bazel module** (`go_sdk.download` and every BCR `bazel_dep`
  included) — **compiler or no compiler**. Deliberately **not fixed** this round; recorded in
  `_bazel_argv`'s docstring and owned by a later task.
- **OPEN GAP: the sandbox image is a placeholder and nothing in this repository builds it.**
  `settings.verify.container_image` is `ghcr.io/acme/fleet-build:2026-08`; **there is no Dockerfile
  anywhere in this repository**; `acme` is this project's canonical placeholder org; a registry
  probe returned `denied`/403 without authenticating, which **distinguishes nothing**, since GHCR
  answers identically for private and nonexistent. **No test exercises a real image.** Enumerated
  from what actually executes inside the container, the minimum is: a **shell**, a **real pinned
  `bazel`** (**not** bazelisk — `--network=none` cannot download a version), a binary named
  **`gcc`** or an image-level **`ENV CC`**, and a **writable HOME for the run uid**. **No JDK**
  (Bazel's release binary embeds a JRE), **no system `go`**, **no gazelle** (it runs on the host
  from the vendored binary, ADR-0056; the Go SDK arrives via `go_sdk.download`).
  **Corrected 2026-08-16 (§31) — "there is no Dockerfile anywhere in this repository" is now FALSE,
  and so is the placeholder image name.** `docker/fleet-build.Dockerfile` exists and builds (**332
  MB**, two stages, ~10s), and `settings.verify.container_image` is now the **local tag**
  `fleet-build:9.2.0-bookworm` rather than the unpullable `ghcr.io/acme/fleet-build:2026-08` — this
  fleet has no container registry, so a registry-shaped name could only ever fail to pull. **The
  enumerated minimum above was met** — shell, pinned Bazel **9.2.0** release binary (digest verified
  three ways), `gcc` + `libc6-dev`, writable HOME for an arbitrary uid, no JDK, no `go`, no gazelle.
  **What is NOT corrected: "no test exercises a real image" is only half-retired.** A test now runs
  the real image, but **no Bazel has ever executed inside it** — `command -v bazel` resolves a path,
  which is not running it — and the test **gates on the image being present locally and never
  pulls**, so a **stale locally-tagged image would still pass**. See the §31 addendum below.
- **OPEN GAP: `rdepverify` runs containerised Bazel with the same image and has NO such gate.**
  **Corrected 2026-08-16 (§33) — the premise is FALSE, so the gap is not a gap; it is an
  unactionable open item and it has been carried in three documents at once.** `rdepverify` does
  **not run containerised Bazel**, with this image or any other: `RdepverifyInput` has **no `image`
  field**, `docker_run_argv` has **zero occurrences** in `workers/rdepverify.py`, and the worker's
  own comment beside `cache.flag(sandboxed=False)` says so in as many words — *"this worker has no
  `image`, builds no container and emits no `--volume=`"*. The only `ContainerSandbox` it
  constructs is in `on_cancel`, to force-remove a name that on this path never exists. **So "no
  C-toolchain gate" is MOOT rather than missing**: a gate probes an image's contents, and there is
  no image; Phase 4 inherits whatever compiler the host has, which is the same undeclared
  dependency §29 named for every host run and not a second one. **What IS true, and it is the
  actionable residue:** containerising Phase 4 is an unmade design change — it would have to add
  the image, the mounts and the `sandboxed=` argument together — and the gate would be needed
  **then**, not now. The same false premise appears in **ADR-0060 consequence 5**, **ADR-0062
  consequence 7**, and the two *"`rdepverify` still has no gate"* sentences in the §29/§31
  correction blocks near the end of this file. All of them are wrong in the same way and are
  corrected by this note rather than by editing each site.
- **What this round does NOT prove, and it is most of the operational surface.** **No real
  container image was ever probed** — every gate test evaluates the probe against **stub files on
  the host**, **no Docker daemon ran**, and the argv → container behaviour is asserted as
  **constructed argv only**. **Bazel was never run against a clang-only or `CC`-carrying
  environment**: that it refuses the first and accepts the second rests on **reading**
  `unix_cc_configure.bzl`, not executing it, and the one real-Bazel test in this area strips
  `gcc`/`cc`/`clang` **and** `CC` **together**, so it does **not** discriminate Gap A from Gap B.
  The probe's shell portability was checked on this host's `sh`/`dash`/`bash` only — **not busybox
  `ash`**, which is what an Alpine image would use. **Nothing about cgo actually building** was
  probed.
- **Counts, and one of them is explicitly NOT orchestrator-confirmed.** Implementing agents
  reported **1103 passed** after the gate and **1108 passed** after the correction (**+5 tests**),
  **0 failed, 0 skipped, 0 xfailed**, **zero `xfail` markers**; `mypy src/fleet/ --strict` clean
  over **106 files**; `ruff` clean. **The 1103 figure was orchestrator-confirmed; the 1108 figure
  was not** — the independent confirmation run was **still in flight** when this was written, and
  per this document's own rule a figure nobody re-ran is a report, not evidence. **Disk:** peak
  Bazel **4.14 GiB** against an **untouched 6 GiB** ceiling; repository cache **1561 MiB** against
  its own **untouched 2048 MiB** keep-ceiling — **~490 MiB headroom, worth watching**.

**Addendum — the Bazel-cache rounds (2026-08-15, §30), and the headline is that a SPEC bound was
*specified and unenforced* in BOTH phases.** The previous addendum's largest open gap is closed for
Phase 3 and, once measured, was found to exist in Phase 4 too. Recorded as **ADR-0061** and in
`docs/PROGRESS.md` §30.

- **The bound was written down and never enforced — that is the finding.** SPEC §3.4's bounds table
  specifies `--disk_cache=` and `--repository_cache=`, "mounted read-write and shared across
  containers and attempts". The code **mounted them and never named them**: `verify.disk_cache` and
  `verify.repository_cache` were bind-mounted RW at `/cache/<name>`, but `_bazel_argv` emitted only
  `--keep_going`, `--build_event_json_file`, `--jobs` and `extra_args`, so the mounts were **inert**
  unless an operator hand-wrote the flags into `extra_args`. Under `verify.network = "none"` a cold
  sandboxed run could therefore **resolve no module at all** — `go_sdk.download` and every BCR
  `bazel_dep` included, **compiler or not**. This is a different failure class from the ones this
  document usually catches: not a fake agreeing with the code, but **a specification the tests never
  asked the code to satisfy**.
- **Phase 4 had the same gap in a different shape.** `RdepverifyWorker` **never containerises** — no
  `image`, no `cache_mounts`, calling `bazel_test_argv(...)` directly on the host — so the
  blast-radius `bazel test` ran with **no disk cache and no repository cache at all**, while SPEC
  §3.4 places the persistent-cache row **inside the Phase 4 section** and names `bazel/query.py`
  among its enforcement points. Now fixed: `cache_mounts` on `RdepverifyInput`, forwarded in
  `VerifyWorker._rdeps` — `VerifyInput` had carried the value all along, **one missing line among
  twelve forwarded fields** — with the flags rendered `sandboxed=False` **unconditionally**, because
  that worker builds no container. Non-vacuity was confirmed by **reverting the forwarding and
  watching the e2e test fail**.
- **A test-infrastructure hazard, and it belongs beside the concurrent-pytest reaper in this
  header — NOT in the live-network flake class, though it presents identically.** **A command-line
  `--repository_cache` beats a `.bazelrc` `common --repository_cache=` line.** `tests/conftest.py`
  shares one archive cache across the session via that `.bazelrc`; the moment the CLI began emitting
  the flag from a per-workspace default, **every real-Bazel e2e test silently received an empty
  archive cache** and re-fetched every ruleset from the live registry. The symptom — long, slow,
  network-bound test runs — is **exactly** what the live-network flake class looks like, and it is
  **not weather: it is a precedence rule**. The first full-suite attempt was still running at **~75
  minutes** when it was diagnosed. Fixed in `test_build_e2e.real_build()`, which appends
  `verify: repository_cache: <BAZEL_REPOSITORY_CACHE>` to the workspace config so the flag and the
  `.bazelrc` name **the same directory**; the suite returned to **~12m34s**. A future round seeing
  this symptom should check flag precedence **before** blaming the network.
- **OPEN GAP, recorded rather than fixed: the rdeps `bazel query` invocations still carry no cache
  flags**, though `rdeps_closure` loads the module graph too. Changing `bazel/query.py`'s **query**
  argv is a separate decision; the absence is **asserted explicitly in the e2e test** so it is
  recorded rather than forgotten.
  **Corrected 2026-08-16 (§31) — CLOSED, with ONE flag and by measurement (ADR-0063).** The rdeps
  `bazel query` invocations now carry **`--repository_cache` and only that**, on **both** the closure
  query and the depth-1 query. **Both flags parse** — `canonicalize-flags --for_command=query` echoes
  `--disk_cache` and `--repository_cache` back, exit 0 — so this was never a `COMMAND_LINE_ERROR`
  risk; but **parsing is not using**, and a probe query over a workspace with one `bazel_dep` wrote
  **2.3 MB into the repository cache and ZERO files into the disk cache**. `query` **executes no
  actions**, so a disk cache has nothing to hold, and **the disk cache is deliberately NOT emitted**
  — a flag on the line implies a working cache. The e2e assertion that pinned the **absence** was
  **updated, not deleted**, and now derives its expectation from `cli._cache_mounts()`.
- **What this does NOT prove, and the honest reading is narrow.** **No cold `--network=none` run was
  ever exercised** — **no Docker was involved**, there is still **no in-tree Dockerfile**, and
  `settings.verify.container_image` names an image **nothing here can pull** (unchanged from the
  previous addendum). The **sandboxed path is proven at the argv level ONLY**: the flags carry the
  mount targets and the mounts are emitted, nothing further. The **unsandboxed** path *is* exercised
  against real Bazel 9.2.0 end to end, so "the flags are accepted and bazel uses those directories"
  is proven **there and only there**. **No real Phase 4 run has ever been exercised against real
  Bazel**, warm or cold — every `fleet verify` test uses `FakeBazel` — so for Phase 4 what is proven
  is **argv construction, ordering, and provenance from settings**. And there is **no timed
  before/after for either round**: the cost claim (Phase 4 re-executing what Phase 3 cached) is
  **inference from the flags' absence**, not a measurement.
  **Corrected 2026-08-16 (§31) — two clauses of the first sentence are now false, and the one that
  matters is NOT.** There **is** an in-tree Dockerfile (`docker/fleet-build.Dockerfile`), and
  `settings.verify.container_image` now names a **local tag this repository builds** rather than an
  image nothing here can pull. **"No cold `--network=none` run was ever exercised" still stands, and
  so does every sentence after it**: the sandboxed path is **still argv-proven only**, because the
  mounted repository cache is **created and never populated**, so under `--network=none` **no module
  resolves — image or no image**. See the §31 addendum below.
- **`extra_args` precedence was measured, not assumed**, because it is the escape hatch operators
  were already using: `bazel canonicalize-flags --for_command=build -- --disk_cache=/a
  --disk_cache=/b --repository_cache=/r1 --repository_cache=/r2` collapses to `--disk_cache=/b` /
  `--repository_cache=/r2` — neither option is `allowMultiple`, so repeats are **last-wins**, both
  are accepted under `--for_command=test`, and neither is deprecated in 9.2.0. Cache flags are
  emitted **before** `extra_args`, so a hand-written flag still wins.
- **Counts, and one of them is explicitly NOT orchestrator-confirmed.** Cache-flag round: **1113
  passed**, **orchestrator-confirmed**; peak Bazel **4.20 GiB**, repository cache **1619 MiB**.
  Phase 4 round: the implementing agent reported **1118 passed** (1113 + 5 new), peak **4.18 GiB**,
  repository cache **1595 MiB** — ***down* from 1619, so no growth**, ~**453 MiB** headroom against
  the **untouched 2048 MiB** keep-ceiling. **Both ceilings untouched** (6 GiB disk, 2048 MiB cache).
  `mypy src/fleet/ --strict` clean over **106 files**; `ruff` clean; **zero `xfail` markers**. **The
  1118 figure was NOT orchestrator-confirmed** — the independent confirmation run was **still in
  flight** when this was written, and per this document's own rule a figure nobody re-ran is a
  report, not evidence. The Phase 4 round's full-suite wall clock was **37m33s**; **no baseline
  runtime is orchestrator-recorded for comparison**, and the mechanical argument is that this change
  cannot have moved it (**no real-Bazel test constructs an `RdepverifyInput`**), so the number is
  recorded **without** claiming a regression or a saving.

**Addendum — the query-cache and verify-image rounds (2026-08-16, §31), and the headline has to be
the limit rather than the achievement: THE SANDBOXED PATH IS STILL RED.** A verify container image
now exists in this repository, which retires a claim this document has carried since §29. It closes
**one** of the two blockers standing between the fleet and a green sandboxed build. The other is
untouched, and it is the one that decides the verdict. Recorded as **ADR-0062** (image), **ADR-0063**
(query cache), and in `docs/PROGRESS.md` §31.

- **The rdeps `bazel query` cache gap is CLOSED — with ONE flag, chosen by measurement rather than
  by symmetry.** `canonicalize-flags --for_command=query` **accepts both** `--disk_cache` and
  `--repository_cache` (both echoed, exit 0), so there was **no `COMMAND_LINE_ERROR` risk in either
  direction** and this was never a safety decision. **Parsing is not using**: a probe
  `bazel query 'deps(//:all)'` over a workspace with **one `bazel_dep`** wrote **2.3 MB into
  `--repository_cache`** and **zero files into `--disk_cache`** (only an empty `tmp/`). **Read-back
  was proven** — a rerun from a **fresh `--output_user_root`** against that repository cache, with
  **`--repository_disable_download`**, resolved the whole module graph and answered the query, exit
  0. `query` **executes no actions**, so a disk cache has nothing to hold, and **the disk-cache
  omission is deliberate and recorded**: a flag on the line implies a working cache. Emitted on
  **both** the closure query and the depth-1 query, at **host paths**, `sandboxed=False`
  unconditionally. The e2e assertion that pinned the **absence** was **updated, not deleted**, and
  now derives its expectation from `cli._cache_mounts()`.
- **What the query round does NOT prove.** **No real `bazel query` runs through `RdepverifyWorker`
  anywhere in the suite** — every `fleet verify` test uses `FakeBazel` — so **the argv is proven and
  an end-to-end cache *hit* is not**. The hit evidence is an **out-of-band probe** over a workspace
  with **one `bazel_dep`**, on **Bazel 9.2.0 only**.
- **The image exists — and it closes exactly ONE of two blockers.** **B1, CLOSED:** with
  `--user <uid>:<gid>` and **no passwd entry**, Docker sets **`HOME=/`** (unwritable) and leaves
  **`USER` unset**; Bazel's **client** then dies in `GetUserName()` with `LOCAL_ENVIRONMENTAL_ERROR`
  = **exit 36**, and **36 is in `INFRA_EXIT_CODES`**, so ADR-0014 spends **no attempt** and **the
  fleet re-queues forever against an image defect**. Reproduced verbatim as a **negative control** in
  plain `debian:bookworm-slim` with the vendored 9.2.0 binary: `HOME=[/] USER=[]` → `FATAL: $USER is
  not set, and unable to look up name of current user` → `bazel exit=36`. **B2, UNTOUCHED:**
  `cli._cache_mounts` creates `<root>/cache/bazel/repo` with `mkdir(parents=True, exist_ok=True)`
  and **nothing ever fills it**, so with `--network=none` and an empty repository cache **no module
  resolves and a sandboxed build fails regardless of the image**. Cache population is a separate
  task. **B2 is why this document's sandboxed rows do not move.**
- **What the image is, and why each choice — because "we added a Dockerfile" is not evidence.**
  `docker/fleet-build.Dockerfile`, **two stages**, **332 MB**, builds in **10s**. Base
  `debian:bookworm-slim`, with **Alpine rejected on evidence**: the official Bazel release binary is
  **glibc-dynamic** (`interpreter /lib64/ld-linux-x86-64.so.2`) and the rules_go/rules_rust/rules_js
  prebuilts are glibc too — and note that **the C-probe itself *was* verified working under busybox
  `ash`**, so the probe was **not** the reason (this document's §29 addendum had flagged the unproven
  busybox case; it is now measured, and it exonerates the shell). Bazel **9.2.0**, **official release
  binary** — not apt, not bazelisk (**`--network=none` cannot download a version**) — SHA256
  `7668a95d…8694` verified against **`releases.bazel.build`**, the **GitHub asset**, **and this
  repo's own bazelisk download cache**, whose content-addressed directory name *is* that digest.
  **No JDK layer** (the release binary embeds a JRE). C toolchain is **`gcc` + `libc6-dev` only**
  (**41** packages vs `build-essential`'s **56**), with **`ENV CC` deliberately unset** because a
  non-empty `CC` **replaces** the `gcc` default rather than supplementing it, so a wrong one is
  **strictly worse than none**. The uid fix is **two-layer**: `ENV HOME=/home/fleet` + `ENV
  USER=fleet` with `/home/fleet` at **0777** (the uid is unknown at build time), **image `ENV`
  survives `--user` with no `--env`, verified**; and **belt and braces**, `/etc/bazel.bazelrc`
  carries `startup --output_user_root=…` so **`GetUserName()` is never reached** and the exit-36 path
  is removed **structurally**. That second layer is not redundancy for its own sake: **the harness
  cannot pass `--output_user_root` at all**, because `_bazel_argv` appends every flag **after the
  verb** and a startup option there is **exit 2**. **Deliberate omissions with named triggers:**
  `git`, `patch`, `unzip`/`xz-utils`, `python3`, `g++`, cgo system libraries and any Go/Node/Rust
  toolchain — those are meant to arrive through the mounted repository cache, and **baking them in
  would mask B2**.
- **`settings.verify.container_image` moved to a local tag, and the failure is now loud.**
  `fleet-build:9.2.0-bookworm` replaces the unpullable `ghcr.io/acme/fleet-build:2026-08`, since this
  fleet has **no container registry**. An **unbuilt** image now fails at `docker run` with **125**.
- **A misattribution FIXED, and it is the same shape this document keeps counting: a check reporting
  a verdict it had not established.** `_c_toolchain_gate` reported **"no C compiler in the sandbox
  image"** for **any** non-zero probe exit — **including `docker run`'s 125**, which means the
  container **never started** (absent or unpullable image, unreachable daemon, invalid flag). With a
  local tag, an unbuilt Dockerfile is now *the most likely 125 there is*, so the old message would
  have sent an operator to the wrong file. It now **branches on 125** with a message naming the
  likely cause and the `docker build` remedy; **classification unchanged** (`BUILD_ERROR`,
  non-retryable).
- **The "no Dockerfile in this repository" claim is corrected everywhere it appears.** It was
  asserted in `buildverify._c_toolchain_gate`, `tests/test_workers_build.py`,
  `tests/test_build_e2e.py`, `tests/test_sandbox.py`, **ADR-0061 consequence 5**, and **three places
  in this document** (the §29 addendum's placeholder bullet, the §30 addendum's "what this does not
  prove" bullet, and the §29 correction paragraph in *Honest summary* below). All corrected.
- **What the image round does NOT prove, and this is the part that must not be softened.** **No
  Bazel has ever run inside `fleet-build:9.2.0-bookworm`** — the integration test checks
  `command -v bazel` **resolves a path, which is not executing it**, plus that `HOME`/`USER` survive
  `--user`. The **exit-36 mechanism was reproduced in a *separate* plain Debian container**, and the
  **fix is argued structurally**: the `ENV` survival was verified, and the rc file and the
  `GetUserName` symbol were verified present in the shipped binary, but **the fixed code path was
  never executed**. **B2 keeps the sandboxed path red.** The image is **not proven *sufficient* for
  any real repository** — no Go/Node/Rust toolchain, no `git`/`patch`/`unzip`/`python3`, no `g++`, no
  cgo system libraries. And the new integration test **gates on the image being present *locally* and
  never pulls**, so a **stale locally-tagged image would still pass it** — the test proves "an image
  with this tag exists and has these properties", **not** "the shipped Dockerfile produced it".
- **Counts, and one of them is explicitly NOT orchestrator-confirmed.** Query round: **1122 passed**,
  **orchestrator-confirmed**. Image round: the implementing agent reported **1124 passed** (**+2**),
  peak Bazel disk **4.18 GiB** and repository cache **1595 MiB** — **both identical to baseline,
  neither ceiling raised** (6 GiB disk, 2048 MiB cache); wall clock **~9m16s**; `mypy --strict` clean
  over **106 files**; `ruff` clean; **0 `xfail` markers**. **The 1124 figure was NOT
  orchestrator-confirmed** — the independent confirmation run of that tree was **still in flight**
  when this was written, and per this document's own rule a figure nobody re-ran is a **report, not
  evidence**.

**Addendum — the lockfile round (2026-08-16, §32), and the headline is the harshest verdict this
document has had to write about the product rather than about the tests: THE HARNESS SHIPPED A
MONOREPO THAT COULD NEVER BUILD OFFLINE, BECAUSE IT CARRIED NO LOCKFILE — AND NOTHING IN THIS SUITE
COULD HAVE CAUGHT IT.** `grep -rn "MODULE.bazel.lock\|lockfile_mode" src/ tests/ docs/` returned
**nothing**: the harness had never generated, carried or mentioned a `MODULE.bazel.lock`, in code,
in tests, or in any ADR. **No test could have noticed**, and the reason is structural rather than an
oversight — **no offline build has ever run in this project**, so the one condition under which the
absence is fatal has never been created. This is the same shape this document keeps counting, in its
purest form yet: not a check that was believed to be running and was not checking the thing, but a
**property nobody ever asked a machine about**. Recorded as **ADR-0064** and `docs/PROGRESS.md`
§32.

- **The diagnosis this document carried since §31 was WRONG, and a controlled matrix is what
  refuted it.** §31 recorded the sandboxed path as red because the repository cache is *"created and
  never populated"* (B2). Measured in the real `fleet-build:9.2.0-bookworm` image under real
  `docker run --network=none --user $(id -u):$(id -g)` with the cache bind-mounted: a **warm cache
  with no lockfile is exit 32**, and the **same warm cache with a matching lockfile is exit 0**. An
  **empty** cache with a matching lock is **exit 32**; a **mirror-warmed** cache with a
  **mirror-keyed** lock is **exit 32**; the **same mirror-warmed cache with a bcr-keyed lock is
  exit 0**; and a mirror-keyed lock with `--registry=<mirror>` on the argv is **exit 0**. Every red
  row fails **identically and before analysis**: `ERROR: Error computing the main repository mapping:
  Error accessing registry https://bcr.bazel.build/: … Unknown host: bcr.bazel.build`. **Both
  artifacts are required and only one of them existed**; and the `--registry` mismatch §31 called an
  unresolved blocker across both seeding options is a property of **the lockfile's URL keys, not of
  the cache** — the two BCR addresses serve **byte-identical** files into a **content-addressed**
  cache. **The obvious next task was refuted before it was dispatched**, which is the outcome this
  document exists to make possible.
- **What is newly true, stated at exactly its strength: the lock now reaches the branch, and that is
  all.** `_publish` reads `MODULE.bazel.lock` **once** at the **build worktree root** and carries it
  as **planned bytes** (ADR-0056's shape); it is committed on the integration worktree **inside the
  `IntegrationMutex`** and **deliberately not staged into the dispatch commit**, because each
  worktree records the module extensions **its own** build evaluated — two ecosystems in one wave
  would be **two branches adding one path with different content**, i.e. **`CONFLICT (add/add)` on
  the second merge**, which takes the run down. **Absence is warned, never synthesized**
  (`module_lock_absent` + `BuildOutput.module_lock_published`), because an empty lock is
  **indistinguishable from no dependencies** and makes the tree **LOOK offline-ready**. A new pure
  `src/fleet/bazel/lockfile.py` `check_lock_registry(text, *, registry)` checks the lock's **URL keys
  only** (values inside `moduleExtensions` are **deliberately excluded** — content-addressed archive
  URLs, not registry keys), with `registry` a **required parameter** because which registry an
  invocation contacts is **a fact about its argv**. **None of that is an offline build.** **No
  `--network=none` build has been attempted, no cache has been warmed, and the sandboxed path is
  still RED.** It is **not** proven the published lock is *sufficient* offline; the
  **last-writer-wins `moduleExtensions`** cost is **accepted and untested**, and the claim that the
  surviving `registryFileHashes` is complete regardless of which targets were built is **reasoning
  about Bazel's MVS, not a measurement**.
- **NAMED STRUCTURAL LIMITATION — L1: the JVM ecosystem CANNOT build offline, and no warming strategy
  can fix it.** `JvmAdapter` emits `maven.install` with **no `lock_file`**, and **`maven_install.json`
  appears nowhere in `src/` or `tests/`**. An unpinned `maven.install` resolves through **coursier,
  which opens its own sockets and never passes through Bazel's downloader**, so `--repository_cache`
  **cannot cover it under ANY warming strategy** — this is not a gap that seeding closes, it is a
  boundary the cache does not span. Closing it needs a **pinned `maven_install.json` published as a
  root file**: a **second design change of the same shape as the lockfile**. This sits beside the
  jvm ledger row's existing "one repo, nothing compiled, `@rules_java` visibility untested" and is
  **strictly worse news** than any of those, because it does not go away by adding fixtures.
  **Same structural verdict at LOWER confidence — reasoned from mechanism, NOT measured this
  round:** Rust's `cargo fetch` (the harness's own comment in `ecosystems/rust.py` records that
  `crate_universe` runs it **without `--locked`**), Python's `pip`-mode `whl_library`, and gazelle's
  `go_deps` module zips. **Those three are unmeasured and are recorded as suspicion, not verdict.**
- **The live-network flake has now recurred FOUR times this session, and it still always passes
  alone.** `tests/test_bazel.py::test_real_bazel_analyses_the_generated_js_binary` — whose guard
  **deliberately fails rather than skips** on a mid-run registry outage, which is the rule the
  registry paragraph in this header exists to enforce — failed in the orchestrator's independent
  confirmation run (**1 failed, 1128 passed**) and **passed alone in 142s**. Its history across this
  session is **four failures, four isolated passes, zero reproductions in isolation**. Per this
  document's standing rule that is **not yet evidence of a defect** — but four is no longer "once",
  and the honest statement is that **nobody has diagnosed it**; it has only ever been re-run.
- **Counts.** Implementing agent reported **1129 passed** (**+5**); with the flake accounted for as
  above the effective state is **1129 passed, orchestrator-confirmed**, **0 skipped, 0 xfailed, 0
  `xfail` markers**. `mypy src/fleet/ --strict` clean over **107** files (**up from 106** — the new
  `lockfile.py`); `ruff` clean. Peak Bazel disk **4.18 GiB** and repository cache **1595 MiB**,
  **both unchanged from the previous round, neither ceiling raised**.

---

## The ledger

| Boundary | Seam | Verdict | What would close the gap |
| --- | --- | --- | --- |
| **`git`** — clone, worktree, branch, commit, merge, `update-ref`, `apply --check --reverse`, trailers, `log --follow` | `vcs/git.py` `Git(runner=…)`, default `util/proc.run` | **REAL** — `test_vcs.py`, `test_workers_transform.py`, `test_transform_e2e.py`, `test_build_e2e.py` all drive real temp repositories. The `BAZEL_RUNNER`/`FILTER_REPO_RUNNER` seams deliberately never reach `Git`, so a merge or a snapshot those files assert on really happened. | Nothing outstanding. Scale is untested: no test drives 250 repos or a repo with a large history. |
| **`git-filter-repo`** — §3.3 step 1 history rewrite | `cli.FILTER_REPO_RUNNER`; `vcs/filter_repo.relocate(runner=…)` | **REAL** — `test_relocate_rewrites_history_into_the_monorepo_path` rewrites a 3-commit history and asserts on the **root commit's tree** and on `git log --follow`, neither of which a copy-the-tree implementation can satisfy. `test_the_retry_of_a_relocation_reproduces_it_exactly` proves ADR-0014 re-runnability the way `cli._prepare_build` really does it (rmtree → re-clone → relocate) and proves the rewrite is deterministic. `test_ingest_rewrites_history_not_only_the_tip` walks `<merge>^2` through the real CLI. | `--strip-blobs-bigger-than` is rendered into argv and asserted there, but **never executed against a real oversized blob**. `--replace-text` (the §11.4 secret scrub) is **worse than untested — it is unwired**: `RelocationSpec.replace_text` defaults to `None`, **no caller in `src/` ever sets it**, the setting that would feed it (`settings.history_scrub_file`) is **read by nothing**, and `--replace-text` has **zero** test hits of any kind. The three real git-filter-repo integration tests exercise `--path-rename` only. This is the largest remaining hole on this boundary and it is security-relevant: the harness does not scrub secrets from rewritten history, and no test would notice. |
| **`git-filter-repo` idempotency** | same | **REAL — and harsher than it was.** `relocate()` is *still not* idempotent: `--force` turns git-filter-repo's refusal of an already-filtered repo into a silent second `--path-rename`, nesting the destination. Pinned by `test_relocating_an_ALREADY_relocated_clone_nests_the_destination`, which still stands **un-inverted** because no guard was added to the primitive. **D3** was fixed one level up, in `cli._prepare_build` (ADR-0042), so the correctness of every future ingest rests on a *caller convention* — a second `--path-rename` that a new call site can simply omit — not on the primitive refusing to do the wrong thing. | An idempotency guard inside `relocate()` itself, after which that test must be inverted and the caller's collapse rule can stop being load-bearing. |
| **`bazel` — MODULE.bazel/BUILD.bazel syntax + analysis** | `bazel/generators.py` + `ecosystems/py.py` → real subprocess in `test_bazel.py` | **REAL, TRIVIAL** — `test_real_bazel_builds_the_generated_python_package` runs `bazel build //py/acme-svc:all` over a `BUILD.bazel` rendered by the shipped `PyAdapter` + renderer, then reads back `labels(srcs, …)` and `labels(data, …)` so the assertion is on **what Bazel resolved**, not on our strings. `test_real_bazel_accepts_the_generated_module` does the same for a bare `filegroup`. **Trivial on purpose, and it must be said: the input is one package, three files (`__init__.py`, `client.py`, `pyproject.toml`), zero internal deps and zero third-party deps.** It proves path re-rooting and file-kind routing (**D4**) and nothing about a package with dependencies. **Widened this round, but only by one ecosystem:** `test_real_bazel_analyses_the_generated_js_binary` puts the JS adapter's `js_binary` through real analysis (that is what **D7** was), and `test_real_bazel_resolves_a_load_whose_ruleset_only_a_target_names` does the same for a `load()`-only ruleset (**D6**). | A generated package for **go / jvm / rust** analysed for real — still zero. The dependent-on-dependency case is now covered end to end for Python only (see the `bazel build` row). **Corrected 2026-08-13 (§24): rust is no longer zero** — two generated `rust_library` packages are analysed *and built* for real; see the dedicated Rust row. **go and jvm are still zero**, and the dependent-on-dependency sentence is unchanged (the Rust fixtures depend on external crates, not on each other). **Corrected 2026-08-15 (§27): go is no longer zero either, and it is the FIRST ecosystem here to cover the dependent-on-dependency case in a compiled language** — real Bazel analysed a generated Go tree and `cquery` resolved `//go/clitool/internal/command:command` → `//go/digest:digest`, a first-party cross-repo edge, not an external one. **jvm is still zero**, and see D19 for what the Go build's greenness does and does not say about the versions it fetched. |
| **`bazel` — `bazel_dep` resolution against a live registry** | same | **REAL** — `test_real_bazel_resolves_the_generated_bazel_dep_lines` runs `bazel mod graph`, which fetches from a real module registry; the assertion is that our rendered module is the root and the ruleset we emitted really resolves with its own transitive `bazel_dep`s. **Read the registry paragraph in the header before trusting this row's history:** for an unknown number of runs it was *skipping*, not passing, because BCR is unreachable here. It now resolves against whichever registry answered and **fails loudly if none does**. | Nothing on the rendering side. The registry that answers here is a mirror, so "resolves against BCR proper" remains unmeasured. |
| **`bazel` — the ruleset version is the version Bazel *selects*, for *every* pin** | same | **REAL — and this is the round's one structural improvement to the suite.** `test_the_configured_ruleset_version_is_the_version_bazel_selects` asserts on the version read out of `bazel mod graph` rather than on text we emitted, which is why it caught **D2**. It covered **one** ruleset, and **D8** is what one uncovered pin costs: five of eight configured versions could not load at all. So the assertion is now a standing guard — `test_every_pinned_ruleset_version_loads_under_real_bazel` loads **every** entry of `build.ruleset_versions` under the real binary, with `test_every_pinned_ruleset_has_a_load_probe` refusing a pin that has no probe so the guard cannot silently stop covering a new one. **A recurring defect class became a test**, which is the only durable form of a fix. | Nothing on this boundary. The guard proves a version *loads*; it does not prove the rules it exports do the right thing. |
| **`bazel` — `use_extension` / tag-class evaluation** | same | **REAL** — `test_real_bazel_evaluates_the_generated_module_extensions` asserts the module graph resolves with **no `extensions failed`** and no `no such file`, over the `PyAdapter`'s two real extension labels; `pip.parse` genuinely evaluates against an **empty** `requirements.lock` — a caveat that **D10** has now retired: the lock the pipeline builds is resolved, not empty (see the resolver rows). Input covers **one adapter's** extensions; the go/jvm/rust `extension_bzl` tables are asserted only against our own expectations. | Run it per adapter for go / jvm / rust. |
| **`bazel build` / `bazel test` of a migrated repo** | `cli.BAZEL_RUNNER`; `workers/buildverify.py` `BuildverifyWorker(runner=…)`, default `LoggedRunner` | **FAKE for the whole state machine; REAL and now 4 of 4 end-to-end.** Tests 1–6 of `test_build_e2e.py` and all of `test_workers_build.py` still use `FakeBazel` — real argv, real exit codes, real stderr truncation, real `attempts` rows, real failure classification, real closure logic, but no build system ever read the tree. `test_build_against_a_real_bazel` runs the real thing with **no seam installed**, carries **no `xfail`**, and asserts twice: `fleet build` exits 0, and a direct `bazel build //...` over the `integration` worktree exits 0 with "Build completed successfully". All four repos reach Phase 3 `SUCCEEDED`; `acme-app-py` builds against a dependency the harness migrated, and `acme-app-ts` **type-checks** an import of one (`//ts/acme/app:app` produces `src/main.js`, and `ts_project` fails the action on a type error). **Extended this round to two-repos-per-ecosystem, and that is where the "exits 0" half stopped being trustworthy on its own:** `test_two_js_repos_with_different_npm_dependencies_both_build` and `test_two_python_repos_with_different_pypi_dependencies_both_build` each add a fifth repo via `add_repos` and assert **`bazel build //...` over the integration checkout**, not `fleet build`'s exit code — because in both defects `fleet build` **exited 0 over a monorepo that does not build** (D13, D14). Both were `xfail(strict=True)` and are now guards. | Note what this row still does not say: nothing here compiles go, jvm or rust, no test is run (only `build`), and the fixture repos are three files apiece. **And one claim this row can no longer make: `fleet build` exiting 0 is not evidence the monorepo builds.** A wave that settled green against an earlier root file is never re-admitted when a later wave replaces it, so the harness's own verdict was green in both of this round's defects; only a direct `bazel build //...` over the merged tree caught either. Every future end-to-end assertion has to terminate in Bazel's answer, not the harness's. **Corrected 2026-08-13 (§24): this cell's *"nothing here compiles go, jvm or rust"* is now false for rust** — see the Rust row below. What this row claims is NOT widened: the Rust proof **scopes `fleet build` to the two Rust repos**, so "4 of 4 in one polyglot fixture" is still the polyglot claim and Rust is **not** inside it. **And the *"a settled wave is never re-admitted"* sentence above is now load-bearing in a second place:** it is the mechanism behind the **unfixed cross-wave residue** of D15, recorded below and in ADR-0053 consequence 2. **Corrected 2026-08-15 (§27) — *"nothing here compiles go, jvm or rust"* is now false for go as well, but read the correction narrowly, because it does NOT widen what THIS row claims.** The Go proof does not run `fleet build` at all: the tree real Bazel compiled was **assembled from the harness's own generated bytes** and published by a test helper, deliberately, because the failure it was chasing lives **inside Phase 3's own per-repo `bazel build`** and an end-to-end run would have asserted the harness's exit code for it. So Go is **not** inside "4 of 4 in one polyglot fixture", and the `fleet build` → `materialize` → `_publish` path over **real** generator bytes remains **fake-covered** for Go. **jvm still compiles nothing.** |
| **`rust` — real `bazel build` of two migrated Rust repos (`rules_rust` + `crate_universe`)** | `cli.BAZEL_RUNNER` with **no seam installed**; `ecosystems/rust.py`; workspace-local `tools/bin/cargo` (**1.97.1**) for fixture lockfiles only | **REAL — and it is the first Rust this project has ever compiled.** `fleet build` migrates `acme-codec-rs` (`hex`) and `acme-case-rs` (`heck`), then the test runs `bazel build //...` over the **integration checkout** and asserts Bazel's own answer: exit **0**, `Build completed successfully`, a `rust_library` target in each generated `BUILD.bazel`, and **real `.rlib` artifacts for both crates**. First attempt, **no retry, no flake**. Two assertions carry more than the exit code: the root `Cargo.toml` names **both** members, and **`@crates//:hex` resolves from a carried lock that never named `hex`** — cargo really extended it. The fixtures **import** their dependency rather than merely declaring it, deliberately: `rules_rust` passes `--extern` to the same `rustc` that compiles the crate and an **unused `--extern` is not an error**, so a declared-not-imported Rust fixture would assert nothing. Each crate has **zero transitive deps and no build script**, verified against the **unpacked registry sources**. | Four things this row does **not** say, each measured rather than hedged. **(1)** The `fleet build` here is **scoped to the two Rust repos**, so **Rust root files are never proven to coexist with JS and Python ones under a real build**; only the offline `RootFileDomainError` guard covers the polyglot domain, and **that guard runs no build system**. **(2)** `bazel build //...` **reaches crates.io at fetch time** — this is **not** an offline proof, and it inherits the live-network flake class the header describes. **(3)** Both Rust repos are in **one wave**; two Rust repos in **different** waves reproduce **D15 verbatim** and that residue is **unfixed** (ADR-0053 consequence 2). **(4)** `bazel test` is not run and the fixtures are three-file crates. Closing these means: a polyglot real build that includes Rust, a vendored/offline crate fetch, and the cross-wave decision in ADR-0053 consequence 3. |
| **`bazel query` / `rdeps` closure** | `bazel/query.py` (pure argv + parsing) + `LoggedRunner` | **FAKE** — argv construction, the 2000/500 bounds, `CLOSURE_SAMPLED` disclosure, and refusing to read a large closure out of a 32 KiB tail are all proven offline against `FakeBazel`. **No real `rdeps` query has ever run**, and this row did not move this round even though its stated blocker (D5–D7, D9–D11) is gone — it is now unblocked and simply not done. | A real `bazel query 'rdeps(//..., //x:y)'` over the generated monorepo. There is now a monorepo to run it on. |
| **`uv pip compile` — Python dependency resolution** | `ecosystems/py.py` `resolution()` → `cli._resolved_support_files` over `cli.RESOLVER_RUNNER` | **REAL, and new this round** — `test_the_lockfiles_the_module_tags_name_are_resolved_not_synthesized` and `test_the_resolved_lock_carries_the_transitive_closure` run the real `uv` against the real PyPI index and assert on the **transitive closure**, which is the one thing a synthesized lock structurally cannot contain: `requests` in, and `certifi` / `charset-normalizer` / `idna` / `urllib3` out. That closure is then fed to real `rules_python`, so the assertion terminates in a Bazel hub rather than in a file. `test_a_repo_that_ships_its_own_lock_keeps_it_and_no_resolver_runs` pins the carry-over precedence (ADR-0043) and `test_a_resolver_failure_is_loud_and_classified_and_never_an_empty_lock` pins Rule 11. Re-running is byte-identical (`--no-header`; uv's banner embeds the scratch path). **Widened this round to a union over two repos:** `test_two_python_repos_with_different_pypi_dependencies_both_build` resolves `requests` + `jinja2` from two repos into one root lock and asserts real `bazel build //...` over the integration branch, and the lock contains `markupsafe` and `certifi` — **distributions no manifest in the fleet names** — which is the closure a promoted single-repo lock structurally cannot contain. `test_a_second_python_repo_revokes_the_carry_and_forces_a_union_resolve` pins ADR-0049's one/two carry rule. | **Still trivial in one dimension and it must be said: one direct requirement per repo, no version conflict, no yanked release, no private index, no `--find-links`.** A conflict the resolver must actually *solve* has never been posed — the union is two disjoint requirement sets, not two that disagree. The `unsatisfiable` path (ADR-0049) is split evidence and should be read as such: that both `urllib3<2` and `urllib3>=2.2` survive into `requirements.in` is pinned **offline against our own output** (`test_ecosystems.py`), and that real `uv` then exits 1 is a **scratch-directory measurement recorded in ADR-0048, not a suite assertion** — `test_build_e2e.py`'s conflict test feeds a *fabricated* stderr tail. **No test in this suite has handed a genuine version conflict to the real resolver.** |
| **`pnpm install --lockfile-only` — JS dependency resolution** | `ecosystems/js.py` `resolution()`, same seam | **REAL, TRIVIAL — and no longer single-importer.** The real `pnpm` reaches the real npm registry and produces a lock with a genuine `packages:` section, which `npm_translate_lock` then turns into an `@npm` hub resolving under real Bazel. **The input is still packages with one dependency each that itself has none**, so the transitive-closure property that makes the `uv` row meaningful is *not* demonstrated here — the closure is trivially the direct set. **What changed this round:** the root lock is resolved once as a pnpm **workspace** with one `importers:` entry per JS repo (ADR-0048), and `test_two_js_repos_with_different_npm_dependencies_both_build` puts two repos declaring **different** packages (`left-pad`, `ms`) through real pnpm and then asserts real `bazel build //...` over the integration branch — the assertion is Bazel's verdict on the union, not on either repo's own package, and it carried an `xfail(strict=True)` until the fix landed. | A JS dependency with real transitive depth; and two importers whose **ranges overlap** (`^2.0.0` vs `^2.1.3`), which pnpm dedupes — the fixture's two repos declare disjoint packages, so the dedupe behaviour ADR-0048 records as its honest limit is measured in a scratch dir, not in the fixture. **The gap this column used to name is closed and was also wrong**: the conflicting-`npm_translate_lock`-tags prediction was falsified (the tags are byte-identical constants); the real conflict was the lock's *content* — see D13. |
| **`LoggedRunner` → real subprocess plumbing** | `workers/buildverify.LoggedRunner` | **REAL, TRIVIAL** — `test_the_default_runner_leaves_the_full_stream_where_artifact_ref_points` spawns real `bazel --version` and asserts the **bytes on disk**, not merely that a file exists, and that the tail and the file agree. Trivial input on purpose: the subject is the plumbing. | A real multi-megabyte stream, to prove the 32 KiB tail/full-file split under the size that motivated it. |
| **`gh` — binary identity** | `tools/bin/gh` via `util/proc.run` | **REAL, TRIVIAL** — `test_the_real_gh_binary_is_the_one_this_harness_would_invoke` runs `gh --version` and asserts a 2.x major, which is the version whose `pr view --json state,mergedAt,mergeCommit` surface the fixtures encode. | Nothing; this is the ceiling for a version check. |
| **`gh` — auth probe** | `vcs/github.GitHubCli.available()` | **REAL, TRIVIAL** — `test_real_gh_reports_its_auth_state` really runs `gh auth status`, and asserts that an unauthenticated exit becomes `False` plus a named `GhError` from `view()`, never an exception the 48-hour poll loop would retry forever. | Nothing locally. |
| **`gh` — `pr create`, `pr view`, `pr ready`** | `cli.GH_RUNNER`; `GitHubCli(runner=…)` | **FAKE — and this row got worse, not better.** Proven: argv (including `--body-file` rather than `--body`, so a PR body never enters the process table), `MERGED`/`mergedAt`/`mergeCommit` parsing from **recorded** JSON, refusal to default an unreadable state to `OPEN`, and polling only non-terminal PRs. Every one of those JSON shapes is **our own fixture**, checked against no live GitHub. The Gitea row below is not evidence for this row: the two forges disagreed on the two behaviours that mattered most (see ADR-0040), so a driver validated against one says nothing about the other. `github` is still the **default** `pr.forge`, so the untested driver is the one operators get. | A GitHub credential and a throwaway repository, or recorded cassettes captured from a real GitHub response rather than hand-written. Neither exists here, and nothing on github.com is being created from this host. |
| **Gitea forge — `create_pr`, `view`, `sync`, `mark_ready`, `available`** | `vcs/gitea.GiteaForge` via `CommandRunner`; `vcs/forge.Forge` | **REAL** — and **this is the only live-forge evidence in the project**. `tests/test_gitea.py`'s `live` block runs against the Gitea 1.25.4 named above: it creates a throwaway repository with two branches, **opens a real PR**, merges it **through the Gitea API directly rather than through the driver** (`_api`, deliberately not `GiteaForge`, so setup and assertion cannot be wrong in the same way), and then observes the harness's own `sync()` report **`MERGED`** — the state three gates consume and nothing else in this project has ever produced from a real forge. A real draft is opened, read back as `DRAFTED`, and promoted by `mark_ready`. **What is trivial about it:** one repository, one commit per branch, one PR at a time, no concurrency, no rate limiting, and a forge on `localhost`. The skips are specific — curl missing / no credential file / unreachable / credential rejected — and none of them fired in this environment. **One thing this row must not be read as proving:** the token stays out of argv (`test_the_token_never_appears_in_the_argv_of_a_create_pr_call`, ADR-0040), but **nothing in `src/` creates, chmods or checks the mode of the credential file** — every `chmod(0o600)` in this project is in `tests/`. The operator's file happens to be `0600` and `.secrets/` is gitignored; that is hygiene, not an invariant. A world-readable `curl -K` file would work silently. | Concurrency and a non-local instance, and a `st_mode & 0o077` refusal in `build_forge` so the "mode-600" the docstrings promise is enforced rather than assumed. |
| **`docker` sandbox (ADR-0010)** | `sandbox/container.ContainerSandbox(runner=…)` | **REAL** — `test_real_container_runs_without_network_and_is_torn_down` uses the live daemon and asserts the mount is visible, the network is genuinely absent, and the container does not survive the call. argv shape is separately proven offline. | The sandbox has never wrapped a real `bazel build`, so `--network=none` is proven for `sh`, not for the workload it exists to contain. **Its stated blocker is gone and a new one is visible:** resolution is now a real network dependency (`uv` → PyPI, `pnpm` → npm, Bazel → the registry), so a `--network=none` sandbox around a build whose lock was **not** carried in is a design question this row cannot answer by testing harder. |
| **`ast-grep`** | `rewrite/astgrep.AstGrepRewriter(binary=…)` | **REAL, TRIVIAL — and closing it found two defects in the probe, both of which had shipped for five checkpoints.** `tools/bin/ast-grep` **0.45.1** is vendored beside `bazel` and `gh` (release asset `app-x86_64-unknown-linux-gnu.zip`, 53 MB; no sudo, nothing on the host; the zip's `sg` shim is deliberately **not** installed, because `/usr/bin/sg` here is shadow-utils'). §7c of `tests/test_rewrite.py` drives the real binary with **no `skip`/`skipif` on this boundary** — an absent binary fails the suite: `test_astgrep_rewrites_real_typescript_to_exact_bytes` reconstructs the post-image through `apply_in_memory` and asserts the **whole file byte for byte**, with the untouched lines verbatim and only line index 3 differing; a mutated `fix` is asserted to diverge and a non-matching pattern to return `None`; all 3 matches in a multi-match file are rewritten in one pass; the same driver rewrites a real `.py` file **and the TypeScript rule is asserted not to fire on it**, so `language_for_path` routing is real rather than a TypeScript special case; `test_astgrep_probe_gate_blocks_the_landing_and_no_commit_is_created` asserts the **whole** `ApplyResult(ok=False, path="web/app.ts", reason="parse probe failed after apply", parse_probe_ok=False, already_applied=False)`, the wreckage on disk, and `git rev-list --count HEAD == 1` with `git log --format=%s == ["initial"]`; and `test_the_whole_path_pipeline_to_worktree_lands_exact_bytes` runs `RewritePipeline` + a real `AstGrepRewriter` in an `EngineRegistry` with `probe=rewriter.probe_text` and lands exact bytes in a real git worktree with `parse_probe_ok is True`. **Mutation-checked, not assumed:** a no-op `astgrep.apply` fails **7** tests, the old `parse_probe` body fails the 3 probe tests, and reverting the `apply.py` path join fails the cwd-independence test (`src/` restored and md5-verified after each). **What makes it trivial, named as §16's criterion requires:** one rule per pass, one pattern shape (`console.log($A)` → `logger.info($A)`, `print($A)` → `logger.info($A)`), source files of **7 lines / 147 bytes**, at most 3 matches, and two languages — `typescript` and `python`. No rule set from `config/rules/` has been run by the real binary, and nothing here rewrites an import specifier. **The two defects the install exposed, both fixed and pinned (ADR-0047):** the parse probe was **inverted** — `run --pattern '$A'` exits **0** on `const = = ;`, on `function f( {` and on `class {{{ ???` (tree-sitter error-recovers and `$A` matches the `ERROR` nodes) and **1** on a valid empty module, so it passed wreckage and failed valid files, and the module docstring asserted the opposite; and `apply_patch` probed a **repo-relative** path while holding a `worktree`, where `ast-grep scan … missing.ts` prints `ERROR: No such file or directory` and exits **0** — the gate read that as "no `ERROR` nodes" and returned `True`. | A real `config/rules/` rule set applied to a fixture repo end to end; more than one rule and more than one language in a single pass; the rewrite languages beyond `typescript`/`python`. And one **measured** hole: `kind: MISSING` is rejected outright by 0.45.1 (exit 8, `Cannot parse rule`), so a file that error-recovers into a `MISSING` token with **no** `ERROR` node still reads as parsing — a false pass that defers to the Phase 3 build gate, but a real gap. |
| **`libcst` (Python rewrites)** | `rewrite/libcst_py.py`, in-process import | **UNPROVEN (absence path only).** `libcst` is not installed in `.venv`; the test that would skip *if it were present* is the one that runs. | `pip install libcst`, then a real CST rewrite. |
| **`ts-morph` / `node` (TS rewrites)** | `rewrite/tsmorph.py` `(node_binary="node", runner=…)` | **UNPROVEN (absence path only).** `node` is on PATH but `ts-morph` is not resolvable, so only the unavailability path runs. | Install `ts-morph`, then a real TS rewrite. |
| **`gazelle`** | `BuildPlan.generated_by == "gazelle"`, `build.gazelle_binary = "gazelle"` (**changed from `"//:gazelle"` in §26** — a Bazel *label* implying `bazel run //:gazelle`, unrunnable over a scratch tree; a deliberate SPEC divergence, ADR-0056); `cli.GAZELLE_RUNNER`; vendored `tools/bin/gazelle` **v0.51.3** | **REAL — corrected 2026-08-15 (§27).** *The generated Go tree is **loaded, analysed and COMPILED** by real **Bazel 9.2.0** from **unmodified harness output** — Gazelle's `BUILD.bazel` files, the root `MODULE.bazel`, the union `go.mod` and the resolved `go.sum`, none of them edited between the harness and Bazel (the test asserts the `MODULE.bazel` bytes are unchanged across the build) — producing real **`.a` archives**, with the **cross-repo edge resolved at ANALYSIS time**. Read the scope precisely, because it is narrow: a **scoped, assembled tree** (published by a test helper, **not by `fleet build`**), **two** repos, **linux/amd64** only. **What is NOT covered: the `fleet build` → `materialize` → `_publish` path over real generator bytes, `go_test`, and conflicting Go module versions across repos.** See the §27 update at the end of this cell, ADR-0057, ADR-0058 and **D19**.* *This row's two previous verdicts are kept verbatim below as the record, per this file's in-place correction convention: **"THE GENERATOR IS REAL; THE GO BUILD IS UNPROVEN"** (§26), and before it **UNPROVEN** for six checkpoints. Both were true when written; both are now FALSE.* *This row opened with **"UNPROVEN … nothing runs gazelle"** for six checkpoints, and every later sentence in this cell reading "gazelle still never runs" / "there is still no `bazel run //:gazelle` call site" said the same thing. **All of it was true when written and all of it is now FALSE**: gazelle runs, for real, and its output reaches the branch. The historical text is kept verbatim below as the record, per this file's in-place correction convention; read it as history, not as status.* **Do not read this as more than it is:** it is the **first Go progress in this project that is not plumbing**, and it is **still not a build** — no Bazel has loaded a generated Go file and no Go has been compiled by anything. The original verdict, as written: **UNPROVEN.** The Go path emits a `GazelleConfig` and directives; nothing runs gazelle. Its stated blocker was "no working Bazel monorepo"; **there is one now**, and the pinned `gazelle` version was one of the five D8 raised precisely so it *could* load. So this row is unblocked, untouched and has no test in either mode. **A new blocker is now named, and it is concrete (ADR-0050):** `go_deps.bzl` calls `sums_from_go_mod` whenever the `go.mod` carries any `require`, and that reads a **`go.sum` beside the `go.mod`** — and `go.sum` has **zero** occurrences in `src/`, `tests/` or `docs/`, so `go_deps.from_file` cannot load the `go.mod` this harness synthesizes. Two further facts bound what a test here could mean: `uses_gazelle = True` makes `generate_targets()`/`test_targets()` return `[]`, so the generated Go `BUILD.bazel` is directive comments with **zero targets**, and `settings.py:563`'s `gazelle_binary` is referenced by nothing in `src/` — there is **no `bazel run //:gazelle` call site anywhere**. A Go two-repo test built like the JS/Python ones would therefore `bazel build //...` over a tree with no Go targets and **pass vacuously**. **Updated 2026-08-12 (§21) — the named blocker's first half is closed in *plumbing only*, and this row does not move.** `go.py` now declares a root `//:go.sum` (`go.py:188`, `carry_from=[]` — **never carried at any count**, because a sums list is valid only against the `go.mod` beside it and carrying one repo's would manufacture a checksum mismatch) and a `resolution()` (`go.py:191`) running `["go", "mod", "download", "all"]` with a **carry-only** `go.mod` input. The honest status is **plumbing-verified, resolution- and build-UNPROVEN**: `test_the_go_root_sum_is_resolved_from_the_union_go_mod_and_never_carried_itself` (`tests/test_build_e2e.py`; **renamed in §22** from `…_from_the_carried_go_mod_…` when the resolver's input became the union) drives `cli._resolved_support_files` against an **injected** resolver runner and asserts the argv, the staged input, the carry that must not happen, and where the bytes land — **no real `go` has ever run in this suite**, nothing asserts the sums are correct or that `go_deps.from_file` accepts them, there are still **zero Go fixture repos**, and gazelle still never runs. **Updated 2026-08-12 (§22) — the `go.mod` union lands (ADR-0050 step 2 → ADR-0051), and this row STILL does not move.** The status is unchanged and deliberate: **plumbing-verified, resolution- and build-UNPROVEN.** `go.py`'s `workspace_files` (`go.py:188`) no longer routes through `base.union_workspace_files` — it renders ONE root `//:go.mod` from `_go_requires(units)` → `_go_mod_text(requires)` (`go.py:387`, `go.py:416`) with `carry_from=[]`: the **monorepo's own** module line (`fleet.internal/monorepo`, `go.py:47`) over the **union** of every Go unit's requirements, duplicates emitted as two lines and left to Go's MVS, `GoModuleCoordinateError` (`go.py:92`) raised on a coordinate that claims Go and cannot render, and non-Go coordinates **excluded by type** (which is what removed `left-pad ^1.3.0`). **The carry-only `go.mod` input this row described above is gone:** `resolution()` (`go.py:244`) now stages the **union itself** — the same `_go_mod_text(requires)` call — so the sums hash the file that lands by construction rather than by a selection rule; `_sum_contributor` is deleted. Held by `test_the_go_sum_is_resolved_against_the_union_go_mod_that_actually_lands` (`tests/test_workers_build.py`) and `test_the_go_root_module_is_the_monorepos_own_and_unions_both_repos_requires` (`tests/test_ecosystems.py`). **What did NOT change is the entire reason this row exists:** `uses_gazelle = True` (`go.py:136`) still makes `generate_targets()` (`go.py:311`) return `[]`, there is still **no `bazel run //:gazelle` call site** in `src/`, there are still **zero Go fixture repos**, and **no real `go` runs inside this suite** — the union was measured against real `go` **by hand in a scratch directory** (it parses; `go mod download all` exits 0 over it and leaves it byte-identical), under `GOTOOLCHAIN=local` with the `go` directive **lowered to 1.21** because the host SDK is 1.22.2 and the harness pins 1.23.4. A more correct root file is not a build. **Updated 2026-08-14 (§25) — a real `go` and two Go fixture repos now exist, and this row STILL does not move; the verdict stays UNPROVEN.** `tools/bin/go` is a workspace-local, SHA256-verified **Go 1.23.4** (the same `_GO_VERSION` the SDK pin declares) with `GOROOT`/`GOPATH`/`GOMODCACHE`/`GOCACHE` inside the workspace and `GOTOOLCHAIN` pinned so a newer `go.mod` **fails loudly** (measured: exit 1, `go: go.mod requires go >= 1.25 (running go 1.23.4; GOTOOLCHAIN=go1.23.4)`), which retires the "which toolchain would compute the sums" worry for **fixture generation** only. `acme-clitool-go` and `acme-digest-go` are in `POLYGLOT_REPOS` **opt-in by name**, so **no real-Bazel test touches Go**; they **import** what they declare (an unused import is a Go compile error and Gazelle derives `deps` from `import` statements — the inverse of the JS/Python fixtures), and their `go.sum`s are real (`tools/bin/go mod download all`). **What has not changed is everything this row is about:** `uses_gazelle = True` still makes `generate_targets()` return `[]`, there is still **no `bazel run //:gazelle` call site in `src/`**, **no Go is compiled by anything**, **no `go_library` has ever existed in a tree this harness produced**, and the branch `go.sum` is a **fake-resolver fixture**. The fixtures are therefore **deliberately vacuous**, and a **tripwire** says so mechanically: it asserts the generated Go `BUILD.bazel` has **no `go_library(`/`go_binary(`/`go_test(`/`go_proto_library(`/`load(`** and **no non-comment payload lines**, guarded by positive assertions that the `# gazelle:` directives *are* present, and its docstring **names the replacement assertion** so wiring Gazelle up turns it **red**. **Updated 2026-08-15 (§26) — GAZELLE RUNS, and this row moves for the first time: the generator half becomes REAL, the build half stays UNPROVEN.** `tools/bin/gazelle` is a wrapper over bazel-gazelle **v0.51.3** (confirmed by parsing `tools/bin/go version -m` — `gazelle -version` prints **`unknown`**, because the version is linker-stamped only by Bazel builds), **deliberately diverging from the BCR module pin 0.52.2**, which cannot move down because that table was chosen by load-probing under Bazel 9.2.0 (ADR-0056; an unrecorded divergence is the **D8** shape). `cli._build_impl` gained **PASS 4**: **one** invocation per Gazelle-using ecosystem over **all** its repo roots, in a scratch tree (`_assemble_gazelle_scratch`), with every created/modified `BUILD.bazel` captured into the plan as `SupportFile`s — so ADR-0054's publish contract carries them **unchanged** and sub-packages at arbitrary depth fall out structurally. Two `@pytest.mark.integration` tests drive the **real** binary through that same PASS-4 code (`GAZELLE_RUNNER = None`), needing **no Bazel** and adding **zero Bazel disk**: a **depth-3** `go_library` (`command`, `importpath = "github.com/acme/clitool/internal/command"`, `visibility = ["//go/clitool:__subpackages__"]`, `deps = ["@com_github_spf13_cobra//:go_default_library"]`) is created and captured verbatim; the **cross-repo edge is proven for the first time** — a sibling import resolves to the in-repo label **`//go/digest`** with `com_github_acme_digest` in no captured file, and the **negative control sits in the same test**, where the identical source through a **one-root** invocation drops that edge **silently at exit 0**; real labels matched the fake's **set-for-set per file**, `go/clitool/BUILD.bazel` is left byte-identical and correctly not captured, and output is byte-identical across two clean runs. **Zero network is measured, not assumed:** `strace -e trace=socket,connect,sendto,sendmsg` recorded **zero** matching syscalls, and `-e trace=execve` shows nothing spawned beyond the wrapper's own `dirname`/`exec` — which is the reverse of what research claimed, since the **default** `-external` mode shells out to `go get`/`go list`/`git ls-remote`, resolves in a throwaway temp module that **ignores the union `go.mod` pins** (fetched `x/crypto` **v0.55.0**), reaches for GitHub, and **silently drops a real dependency at exit 0**; `-external=static -index=all` (now dispatched from `GazelleConfig.args`) used zero subprocesses and resolved a **strict superset**. **What the row still does NOT say, and it is most of what matters: no Bazel has ever loaded any of these files.** Nothing shows `@com_github_spf13_cobra` or `@org_golang_x_crypto` exist under those names, that `go_deps` creates them, that `//go/digest` is a loadable target, or that **any** label resolves at analysis time — **the labels are asserted as text**. **No Go is compiled by anything**: gazelle parses `import` statements and does not typecheck, so the sibling import is proven to **resolve**, not to **build**. The scratch tree is **not a Bazel workspace** (its `MODULE.bazel` is a one-line comment marker), the real-binary tests exercise **`_run_gazelle` directly** so the full `fleet build` → `materialize` → `_publish` path over real generator bytes is still covered **only by the fake**, and **no Go repo appears in any real-Bazel test**. Two disclosed fake/real shape divergences remain, neither affecting a label: the real binary **rewrites** a pre-existing directives file with its `load()` on top, where the fake **appends**; and the fake prefixes created files with a newline while real ones start at `load(`. The fake was **not** restructured — a deliberate, disclosed limit. **Updated 2026-08-15 (§27) — REAL BAZEL LOADS, ANALYSES AND COMPILES THE TREE, and the sentence directly above beginning *"What the row still does NOT say, and it is most of what matters: no Bazel has ever loaded any of these files"* is now FALSE in every clause.** `@com_github_spf13_cobra` and `@org_golang_x_crypto` exist under those names, `go_deps` creates them, `//go/digest` is a loadable target, and the labels are **no longer text**. Verbatim from **unmodified** output: `INFO: Analyzed 4 targets (116 packages loaded, 9178 targets configured)`, `GoStdlib`, `GoCompilePkg`, `INFO: Build completed successfully, 21 total actions`, with real **`.a` archives** for `digest`, `command` and `clitool_lib`; and the cross-repo edge read out of **Bazel's configured-target graph** rather than out of the generated text — `cquery deps(//go/clitool/internal/command:command, 1)` returns **`//go/digest:digest`** alongside `@com_github_spf13_cobra//:go_default_library`, which is ADR-0056 consequence 5 answered by the build system. **Getting there took three pin defects, none of them a generator bug**, each applied as a text edit inside the test and asserted to clear the **specific verbatim error** before it: the missing `bazel_dep(repo_name = "io_bazel_rules_go")` (`No repository visible as '@io_bazel_rules_go' from main repository` — a **loading** failure, every Go repo, no targets at all); the `extension_bzl["go_sdk"]` / `library_bzl` spelling split, unfixable in isolation because **a `bazel_dep` has exactly one apparent name**; and `_GO_VERSION = "1.23.4"` against gazelle 0.52.2's `≥1.24.12` tools floor. **ADR-0057** closed the first two (`ruleset_repo_names` on the adapter, unioned with a raise on disagreement, `repo_name` emitted only where it diverges; `map_kind` rejected; js/py/rust/jvm audited clean), **ADR-0058** the third (`_GO_VERSION` → **1.24.12**, the minimum both floors accept, propagated through **six** sites; the root `go.sum` came back **byte-identical**, which is correct — a sum line is a content hash of a published zip and MVS over an unchanged `require` set does not depend on the toolchain). **The tree was NOT produced by `fleet build`** — it is assembled from the harness's own generated bytes, deliberately, because the failure lives inside Phase 3's own per-repo `bazel build`, so an end-to-end run would have asserted the harness's exit code for it, three retries deep, at one Go-sized output base per repo. **And a green build here is not a statement about the versions fetched: see D19.** | A real `bazel run //:gazelle` over a generated Go package, and a real `go mod download all` behind the `Resolution` this row now describes. **The SDK is still unpinned:** `Resolution` has no `env` field (`models/build.py:173`) and `_run_resolution` never passes `env=`, so which toolchain would compute the sums is decided by the host's `GOTOOLCHAIN` — which no test controls (host `go` resolves to **1.23.4** under `GOTOOLCHAIN=auto`, correcting §20's "1.22.2"). Then ADR-0050 step 2, the `go.mod` union, which must revisit the "first contributor" coupling this resolution introduced. **Corrected 2026-08-12 (§22) — both of those are now done, and neither closed this row.** The SDK **is** pinned: `Resolution` gained an `env` field (`models/build.py:214`, `default_factory=dict`) which the driver merges as `{**os.environ, **plan.env}` at the call site (`cli.py:5844`, an overlay — `util/proc.run` replaces the child environment wholesale), and `go.py` declares `env={"GOTOOLCHAIN": f"go{_GO_VERSION}"}` (`go.py:308`) off the same constant the `go_sdk` toolchain registers. The union landed as ADR-0051 and the "first contributor" coupling was **deleted**, not re-pointed. What is left to close this row is therefore only what it always was: **running gazelle**, over a **real Go fixture repo**, with a **real `go`** behind the `Resolution` — and a **new** known gap now sits beside it, that `GoModuleCoordinateError` escapes the driver's per-ecosystem containment (which catches only `DependencyResolutionError`, `cli.py:5980`) and fails the whole run. **Corrected 2026-08-12:** this cell previously said "carried at exactly one contributing repo, and produced by a `Resolution` (`go mod tidy`/`go mod download`) at two or more" — both named commands are wrong (measured: `tidy` deletes the `require` block and writes no `go.sum`; bare `download` writes no `h1:` sums), and on the same evidence the `go.sum` should never be carried at any count. See ADR-0050's measured correction. **Updated 2026-08-14 (§25):** two of the three things named above now exist — a real `go` (`tools/bin/go`, 1.23.4) and **real Go fixture repos** — and **the row is closed by neither**, because what is missing is the middle term: **gazelle running**, and a **real `go` behind the `Resolution`** (the suite still drives it through an **injected** runner). Two corpus facts bound what closing it would mean, from a read-only survey of ~267 bare Gitea repos: **no Gazelle configuration exists anywhere in the corpus** (25 Go repos — **corrected 2026-08-15 (§29) to 31**, of which **10 carry cgo**; the `68 non-vendor go.mod`s figure beside it was computed over the 25-repo enumeration and was not re-measured — 68 non-vendor `go.mod`s; the only grep hits were false positives inside a vendored Rust crate's Starlark parser test fixtures), so this contract has **no real-world precedent to validate against**; and **13 of 68 modules declare `go` ≥1.25** (5 of the 9 root-`go.mod` repos), while measurement under `GOPROXY=off` shows `1.23.4` and `1.24.2` resolve and **`1.25.0` fails `toolchain not available`** — so real corpus Go repos need the SDK pin at **≥1.25.7** or they land as `REQUIRES_HUMAN_INTERVENTION`. **Updated 2026-08-15 (§26) — the middle term is now supplied, and what is left is the *other* two.** Gazelle runs (real binary, real output, captured into the plan), so what would close the **remaining** half of this row is, in priority order: **(1) a real Bazel load/analysis of a generated Go tree** — the single largest gap, because every Go label above is still only **text**; **(2)** the full `fleet build` → `materialize` → `_publish` path exercised over **real** generator bytes rather than the fake; **(3)** the fake/real shape divergence closed or documented permanently; **(4)** a **real `go` behind the `Resolution`** — the suite still drives it through an **injected** runner — and the SDK pin raised to **≥1.25.7** before real corpus Go repos are in scope. Note the vendored binary costs **~500 MB**, mostly a `go1.24.12` toolchain module pulled in to build it: **every published gazelle from v0.48.0 declares `go 1.24.12`**, so **no downgrade** reaches an installable gazelle under the 1.23.4 pin (correcting the §25 claim that `go install` was broken only at **0.52.x**, upstream #2396), and the install required a **one-shot `GOTOOLCHAIN=go1.24.12`** override kept inside the workspace. The wrapper is **required, not cosmetic** — unwrapped, Gazelle's `findGoTool()` picks the host `/usr/bin/go` and writes into `$HOME/go`. **Updated 2026-08-15 (§27) — item (1), the single largest gap, is CLOSED**, and what remains to close this row is, in priority order: **(1)** emit **`go_deps.config(check_direct_dependencies = "error")`** in the root `MODULE.bazel`, so an MVS raise **fails loudly** instead of printing a DEBUG line (**D19**'s non-inherent half — the harness renders that file and could emit the tag today); **(2)** the full `fleet build` → `materialize` → `_publish` path over **real** generator bytes, since the compiled tree here is **assembled**, not published; **(3)** a **`go_test`** anywhere — neither fixture ships a `*_test.go`; **(4)** a fleet with **conflicting Go module versions across repos**, which the two-repo/five-requirement fixture cannot pose; **(5)** the fake/real shape divergence closed or documented permanently; **(6)** a **real `go` behind the `Resolution`** — the suite still drives it through an **injected** runner; **(7)** SDK coverage beyond **linux/amd64**, and the pin raised to **≥1.25.7** before real corpus Go repos are in scope (the §25 corpus survey's requirement is unchanged by the move to 1.24.12). Also unclosed and not a Go question: the build is **not reproducible offline** — BCR, `go.dev/dl` and `proxy.golang.org` were all reachable and the resolve hit a **warm workspace module cache**. **Corrected 2026-08-15 (§28): item (1) of the priority list in this cell — emit `go_deps.config(check_direct_dependencies = "error")` — is WITHDRAWN, not pending.** It was measured and **decided against** (**ADR-0059**): it aborts the whole monorepo at extension-evaluation time and is **neither necessary nor sufficient** for the checksum question, whose supply-chain half turned out to be **false** — a version with no `h1:` anywhere **`fail()`s at fetch time** at every setting. Items (2)–(7) stand unchanged. |
| **`openapi-generator-cli`** | `build.openapi_generator` setting | **UNPROVEN.** Configured, never invoked by any test. | A contract-hoisting test that actually generates a client. |
| **LLM backends (Anthropic / OpenAI-compatible)** | `llm/client.ModelClient` Protocol (ADR-0023, guardrail 3) | **FAKE, correctly.** Every model call in the suite goes through a fake `ModelClient`. Budgets, caching, role routing, retry ladders and schema validation are proven; no request has ever left the process. | A recorded-cassette or live-endpoint contract test per backend, asserting the real wire shape matches the Protocol. |
| **SQLite / `aiosqlite`** | `state/db.py` | **REAL** — real database files, real migrations v002–v008, real WAL/pragma configuration, real concurrent-writer behaviour. | Nothing outstanding. |
| **`util/proc.run` itself** | the base of every boundary above | **REAL** — `test_proc.py` drives real subprocesses: exit codes, timeouts and kill, tail truncation, `log_dir` streaming to disk, `started=False` for a missing binary. | Nothing outstanding. |

---

## Defects found by running the real tools

### D1–D4 — found by the first real `bazel` run, now FIXED

Each is recorded here with the assertion that now holds it, because a fixed defect with no test is
a defect waiting for its next commit.

**D1 — `use_extension` named a `.bzl` no ruleset ships.** `render_module_bazel` defaulted
`extension_bzl` to `@<ruleset>//:extensions.bzl`, and nothing in `src/` ever passed the parameter.
Real Bazel: `cannot load '@@rules_python+//:extensions.bzl': no such file` … `2 extensions failed`,
which resolved `@pypi//:requests` and every other third-party label to nothing, for every repo.
**Fixed** by moving the mapping onto the adapters — `EcosystemAdapter.extension_bzl` is now a
`Mapping[str, str]` of proxy name → real label (`ecosystems/py.py`, `js.py`, `go.py`, `jvm.py`,
`rust.py`), read by `generators._registry_extension_bzls()`; a proxy neither the caller nor the
registry can place **raises** rather than emitting a plausible-looking label (Rule 11).
Held by `test_real_bazel_evaluates_the_generated_module_extensions` — no longer an xfail.

**D2 — `build.ruleset_versions` was a floor, not a pin.** `bazel_dep(version = X)` is a *minimum*
under MVS, so real Bazel selected **`rules_python@1.7.0`** for a configured `1.0.0` and §9's
reproducibility guarantee did not hold. **Fixed** by emitting a `single_version_override` beside
every ruleset's `bazel_dep` (`generators.render_module_bazel`); a validated conflict-resolution
override wins over the configured pin, because two overrides for one module is an error Bazel
rejects outright. Rationale and the rejected alternative are ADR-0041.
Held by `test_the_configured_ruleset_version_is_the_version_bazel_selects`.

**D3 — Phase 3 relocated a tree Phase 2 had already relocated.** Observed on the integration
branch as `py/acme_lib_py/py/acme_lib_py/pyproject.toml`, with every state row green, the merge
real and the provenance trailers correct. **Fixed** in `cli._prepare_build` by applying the relocation
as an idempotent *mapping*: `--path-rename ':<dest>/'` followed by
`--path-rename '<dest>/<dest>/:<dest>/'`, which collapses exactly what Phase 2 already moved
(ADR-0042). Held by `test_ingest_does_not_relocate_an_already_relocated_tree`.

> **Why D3 is the important one in this document.** It survived every end-to-end test because
> `FakeFilterRepo` moves no paths, *and* because the assertion that was supposed to guard it —
> `path.startswith(f"{dest}/")` in `test_ingest_rewrites_history_not_only_the_tip` — is
> **vacuously satisfied by the bug**. That is the transferable lesson of the round; it is worked
> through in `docs/PROGRESS.md` §15 and is not repeated here.

**D4 — generated `BUILD.bazel` files were rejected by real Bazel analysis.** Two causes, both in
target emission: `srcs` were emitted repo-root-relative inside a package already named `<dest>`,
doubling the prefix; and non-source files (`pyproject.toml`) were placed in `py_library.srcs`,
which rules_python rejects with *"is misplaced here (expected .py or .py3)"*. **Fixed** in
`ecosystems/base.py`: `package_relative(dest, paths)` re-roots every path (idempotently), and
`accepts_src()` splits `sources()` from `non_source_files()` so a refused file moves to `data`
rather than being dropped — the package keeps a file it reads at runtime. Every adapter states its
own `src_suffixes`. Held by `test_real_bazel_builds_the_generated_python_package`, which asserts on
`labels(srcs, …)` and `labels(data, …)` as Bazel resolved them.

**The `xfail` reason string is gone, because the defect it described is.** It was maintained as
evidence across three revisions — it once named D4's fixed causes as outstanding, then the six a
real run reported, then **D12** alone with the verbatim `tsc` error — and the rule it was
maintained under is the reason it is now deleted rather than left passing: a strict xfail that
passes is a defect report about code that works. What survives it is the scope note, which moved
into `real_build()`'s docstring: without `--repository_cache=` and `--http_timeout_scaling=` both
TS repos die on a download timeout, so a red run here still has to be read against the weather.

### D5–D11 — the round's verdicts

The previous revision recorded these as seven defects found by running the real tools against the
D1–D4 fixes. Working through them produced **six fixes and one withdrawal**. Each entry keeps its
original mechanism (so the record of what was actually wrong survives) and adds the verdict and the
assertion that now holds it.

**The `.bazelversion` caveat is CLOSED.** `make_monorepo` wrote `.bazelversion` = `"7.4.1"` while
the toolchain and the generator tests were on 9.2.0, so the end-to-end path had never run on the
Bazel this document's header names — and that gap was **masking D8 in its entirety**. The fixture
now pins the version `tools/bin/bazel` actually launches, and
`test_build_e2e.py` asserts that the two agree, so the fixture cannot drift off the toolchain again
without a test saying so.

**D5 — WITHDRAWN. Not a defect; a symptom of a symptom.** It was written up as "a dependent cannot
resolve `//py/acme_lib_py` because its dependency never published a `BUILD.bazel`", with the
proposed cause "publication is gated on that repo's own Phase 3 succeeding". **Both write-ups were
wrong, and this is recorded rather than quietly deleted because the correction is the most useful
thing in this section.** The first revision said the labels pointed outside the worktree; they did
not. The second said the dependency's BUILD file was withheld; **it was not** — dumping the actual
snapshot trees shows wave 0 = libraries, wave 1 = applications, and the dependent's snapshot
**did contain the dependency's `BUILD.bazel`**. The entire failure was **D9** knocking `acme-lib-py`
out before its dependent ran. Fixing D9 fixed D5 with no D5-specific change of any kind. The
ordering the second write-up guessed at is now pinned for real by
`test_a_dependencys_generated_package_is_on_the_branch_before_its_dependents_snapshot`.
**The lesson: a defect diagnosed by reading was wrong twice; it took one dump of the real tree to
end it.** A "known cause" that nobody reproduced is a hypothesis wearing a defect's clothes.

**D6 — FIXED. A ruleset used only by a `load()` got no `bazel_dep`.** `render_module_bazel` derived
its ruleset set as `{d.ruleset for d in workspace_deps} | {t.ruleset for t in toolchains}`, so a
ruleset named only in a target's `load_from` label was invisible to `MODULE.bazel`. The JS adapter
was the clear case: it loads `ts_project` from `@aspect_rules_ts//ts:defs.bzl` and `js_binary` from
`@aspect_rules_js//js:defs.bzl`, and a JS unit with no external npm coordinates contributes no
`WorkspaceDep` at all — so the BUILD file loaded from repositories the module never declared.
**Fixed** by deriving the set from `load_from` labels as well, with one subtlety that is the whole
reason this needed thought: **a load label names an *apparent repo*, not a module** — `rules_go`
publishes `@io_bazel_rules_go`, so the derived names are intersected with the pinned
`build.ruleset_versions` table rather than emitted verbatim (ADR-0045's sibling reasoning; the
mechanism is in `generators._load_repo`). Held by
`test_real_bazel_resolves_a_load_whose_ruleset_only_a_target_names`, plus
`test_a_load_from_an_apparent_repo_that_is_not_a_pinned_ruleset_is_left_alone` and
`test_an_unpinned_ruleset_is_refused` on the two edges.

**D7 — FIXED, by deletion.** `ecosystems/js.py` emitted
`BuildTarget(rule="js_binary", deps=[f":{name}"], …)`; rules_js's `js_binary` has **no `deps`
attribute**, and the edge was already carried correctly by `data` on the line below. `bazel query`
would not catch this; only analysis does, which is why it survived until a real `build`. Held by
`test_real_bazel_analyses_the_generated_js_binary`.

**D8 — FIXED, and turned into a standing guard.** The pinned `rules_rust: "0.54.1"` could not load
under Bazel 9.2.0 (`CcInfo` removed). It was never an isolated case: **five of the eight configured
pins were on the wrong side of a Bazel 9 removal** — `rules_rust` 0.54.1→**0.65.0**, `rules_go`
0.50.1→**0.61.1** and `gazelle` 0.39.1→**0.52.2** (all three on `CcInfo`), `aspect_rules_ts`
3.5.0→**3.10.0** and `aspect_rules_js` 2.1.3→**3.4.0** (`@local_config_platform`, removed). The
authoritative list of versions is `settings.py`'s `build.ruleset_versions` and is not restated
anywhere else. This was the direct consequence of D2's fix: while the version was an MVS floor a
transitive floor-raise could quietly supply a loadable ruleset; a true pin means the configured
version is the version that runs. **The fix that matters is not the five numbers** — it is
`test_every_pinned_ruleset_version_loads_under_real_bazel` (see its ledger row), which converts a
defect class that would otherwise recur on every Bazel upgrade into a test.

**D9 — FIXED, and it was the highest-value defect in the list, exactly as predicted.**
`acme-lib-py` built green and then failed `bazel test` with `ERROR: No test targets were found, yet
testing was requested` — Bazel **exit 4** — which the harness classified as a retryable
`TEST_FAILURE`, burned all three ADR-0014 rungs on a library that built perfectly, and escalated it
to `REQUIRES_HUMAN_INTERVENTION`, starving its dependents (that was D5). **Fixed** with an exit-code
taxonomy reproduced against the real binary rather than quoted from memory (ADR-0044; the table
itself lives in `workers/buildverify.py` and is not duplicated here). It caught a second live bug
in passing: **exit 2 (`COMMAND_LINE_ERROR`) was being retried three times with byte-identical
argv** — so 2 and 127 now terminate without charging the ladder. The subtlety that makes this a
*code* check and not a message check — **exit 1 dominates exit 4**, so the stderr string is
ambiguous in exactly the case that matters — is the substance of ADR-0044 and is stated there.

**D10 — FIXED, and it is the reason the resolver rows above exist.** The generated `MODULE.bazel`
named lock and config files no phase created — `pip.parse` pointed at `//:requirements.lock`
(`Error in read: Unable to load package for //:requirements.lock`), with the same shape for
`//:pnpm-lock.yaml`, `//:.bazelignore` and a `ts_project(tsconfig = ":tsconfig")` naming a target no
adapter emitted. **Fixed** by making every such file a declared `SupportFile` the adapters own,
resolved by an explicit precedence — **carried from the source repo where one exists → resolved by
a real resolver → synthesized floor** (ADR-0043) — so the knowledge of *which* files a module needs
sits in the adapter that names them. Held by the resolver tests in the ledger.

**D11 — FIXED, and the fix is a plural, which is the whole point.** No `use_repo` was emitted for a
toolchain's repository: `render_module_bazel` populated `repos` only in the `workspace_deps` loop,
and `ToolchainRequirement` had no repo field at all, so rules_ts's `ext.deps` created
`npm_typescript` and nothing imported it (`no such package '@@[unknown repo 'npm_typescript'
requested from @@]//'`). **Fixed** by adding `ToolchainRequirement.repo_names` — a **collection**,
not a single name, because one toolchain tag creates several importable repositories; the count,
the names and how they were established (`bazel mod show_repo`, not assumption) are ADR-0045's, and
so is the argument that a singular field would have been the same defect with a smaller blast
radius.

### D12 — FIXED: the monorepo had no way for one TypeScript package to import another

**D12 — this monorepo had no way for one TypeScript package to import another.** `acme-app-ts` was
the single repo that did not pass `test_build_against_a_real_bazel`. Everything Bazel is asked to
do now works — the module graph loads, `@npm` is a hub with `left-pad` in it, `//ts/acme/lib:lib` is
on the branch and is a real dependency of the `ts_project`, and the action **runs**. What fails is
`tsc` resolving the import specifier: `error TS2307: Cannot find module '//ts/acme/lib:lib'`. Two
things compound, and the second is the real one:

**(a)** the fixture's rewrite rule rewrites `'@acme/lib'` to `'//ts/acme/lib:lib'` — **a Bazel label
is not a module specifier**, and no TypeScript resolver has ever resolved one. That rule stands in
for an operator's rule set (§3.2 rules come from config, not from `src/`), so it is a *fixture*
defect — and it is also a preview of what the `ast-grep` row costs: the only rewrite rule this
project has ever run end to end is wrong, and nothing but a real build noticed.

**(b)** it cannot simply be corrected, because **there is no specifier that would work**. rules_js
resolves first-party packages the way pnpm does, through a `node_modules` link created by
`npm_link_all_packages()` in the importer's own package, from a `workspace:`/`link:` entry in the
lockfile. This harness resolves **one** importer — the monorepo root — over
`BuildUnit.external_coordinates`, which by construction excludes internal siblings; and a relative
`../../lib/src/index` is refused by the `--rootDir ts/acme/app` that rules_ts passes. So the missing
piece is **first-party workspace linking in `ecosystems/js.py`**, and it is blocked on
`EcosystemAdapter.workspace_deps(coordinates)` not being handed the unit: an importer path cannot be
derived from a coordinate list.

**The fix, and which half of it mattered.** Both halves were real and they are not the same kind
of thing:

*(a) was a fixture defect with a harness cause.* The rule is config, so the label it wrote is the
operator's — but the only monorepo facts a `RewriteRule` could render were `{{dest_path}}` and
`{{repo_id}}`, a directory and an id. A rule author asked "what does this dependency become" had
nothing language-legal to answer with, and a path plus a target name is a label. `EcosystemAdapter`
now declares **`import_specifier(coordinate, dest)`** — abstract, so a new language must state its
own answer rather than inherit one that would be a label — and `cli._import_specifiers` renders it
into the run params as `{{import_specifier}}`. What a cross-repo import becomes is pinned per
ecosystem by `test_a_cross_repo_import_becomes_a_language_name_and_never_a_bazel_label`, over the
whole `Ecosystem` enum, asserting both the exact string and that it contains no `//` and no `:`.

*(b) was the API gap.* `workspace_deps` now takes the `BuildUnit`, and `BuildUnit.internal_deps`
carries `InternalDep(label, dest, published)` instead of a bare label — a label cannot be turned
back into `@acme/lib`, which is why "the adapter cannot know which siblings to link" was true as
written. `JsAdapter` declares each sibling as a pnpm **`link:<dest>`** entry in the resolver's root
manifest (with the sibling's own `package.json` as a resolver input, because pnpm reads the
manifest at the linked directory), which puts a first-party entry in the lock; `npm_link_all_packages()`
materializes it at `//:node_modules/@acme/lib`; and the adapter emits `npm_package(name = "pkg")`
in each JS package, which is the target `npm_translate_lock` generates the link against
(`npm_package_target_name` defaults to `"pkg"`). That last part is the one only a sandbox shows:
linking the `ts_project` directly stages its declarations and **not** its `package.json`, so `tsc`
finds the directory, finds no manifest in it, and reports the consumer's import as unresolvable.
`link:` was chosen over `workspace:` because `workspace:` makes every sibling a second lockfile
*importer*, and rules_js then requires a `.bazelignore` line and an `npm_link_all_packages()` call
per importer — the root stays the one importer this dialect has always resolved.

**A structural defect visible from the same place, and still not fixed:** there is **one** `pnpm-lock.yaml`
at the monorepo root, because `npm_translate_lock` builds a single hub. Two JS repos with different
external dependencies therefore emit **conflicting** `npm_translate_lock` tags. The fixture has one
JS repo with external deps, so nothing fails today; a second one is all it takes.

> **Correction (root-lockfile round).** The last two sentences are right and the middle one is
> **wrong**, and the correction is worth more than the prediction was. A second JS repo *was* all it
> took — but the `npm_translate_lock` tags do **not** conflict. Their attributes are constants, so
> every JS repo renders a **byte-identical** tag and `render_module_bazel` collapses them;
> `MODULE.bazel` was never at risk. The conflict was in the root lock's **content**, one layer down,
> and it was resolved silently by a `setdefault`. See **D13**. Left in place rather than rewritten
> because a defect argued from reading and wrong about its own mechanism is exactly what §15's D5
> lesson is about, and this is the second instance.

### D13–D14 — found by adding a *second* repo to an ecosystem, not by reading

Both were silent drops of another repo's dependency from a monorepo-root file, both left
`fleet build` **exiting 0**, and neither was visible from inside either repo's own package. They
are recorded together because the second was found only because fixing the first prompted looking.

**D13 — FIXED. One JS repo's external dependency was deleted from the root lock, and the migration
reported success.** `cli._module_inputs` unioned the fleet's root files with
`support.setdefault(file.path, file)` over `sorted(plans)` — **first `repo_id` wins**. With
`acme-app-ts` declaring `left-pad` and `acme-report-ts` declaring `ms`, one lock survived and real
Bazel failed analysis with `no such target '//:node_modules/ms'`. **`fleet build` exited 0,
reporting 5/5 SUCCEEDED**, because the losing repo had already built green in wave 0 against the
lock that was at the root *then*, wave 1 replaced it, and a settled wave is never re-admitted.
Flattening lost just as quietly one layer further down: `js.py`'s root `dependencies` was a **dict
keyed on package name**, so `[app ms@^2.0.0, report ms@^2.1.3]` emitted `{"ms": "^2.1.3"}` with no
error — the lexicographically-largest specifier winning by a rule with no semantic meaning.
**Fixed** by resolving the root lock once per ecosystem as a pnpm **workspace** with one importer
per JS repo (ADR-0048), moving labels to `//<dest>:node_modules/<pkg>`, and replacing the
`setdefault` with `RootFileConflictError` — identical bytes dedupe quietly, divergent bytes raise
naming the path and every contributing repo. Held by
`test_two_js_repos_with_different_npm_dependencies_both_build`, which asserts real
`bazel build //...` over the integration branch rather than the presence of `ms`: pinning the
symptom would go green on a fix that only changed *which* repo loses.

**D14 — FIXED, and it is the worse of the two.** `PyAdapter.workspace_files([app, metrics])`
returned a single root `requirements.lock` whose `carry_from` named only the
**lexicographically-first** `dest` (`-` < `_`). When that repo shipped a lock, `cli._carried`
short-circuited the resolver, **its single-package lock became the whole fleet's**, `requests` was
absent from the `@pypi` hub — **and `fleet build` exited SUCCESS**. Which repo won was an artifact
of string ordering. Worse than D13 in one specific way: D13 at least produced a Bazel analysis
error for anyone who ran Bazel directly; D14's monorepo was green in the harness and wrong in the
hub. **`RootFileConflictError` did not fire and structurally cannot** — `_fleet_support_files`
hands every plan of one ecosystem the identical tuple, so no divergence remains to detect; the
union guard defends against two adapters disagreeing, not against one adapter computing the wrong
single answer. **Fixed** by keeping `carry_from` at **exactly one** contributing unit and dropping
it at **two or more**, forcing the `Resolution` middle term (ADR-0049). Held by
`test_a_second_python_repo_revokes_the_carry_and_forces_a_union_resolve` and
`test_two_python_repos_with_different_pypi_dependencies_both_build`.

> **The transferable lesson, and it is a statement about this fixture rather than about locks.**
> Both defects had shipped for five checkpoints behind a green suite, and both became visible the
> moment an ecosystem had **two** repos instead of one. The single-repo-per-ecosystem fixture did
> not fail to test the union — it made the union's contract **vacuously true**, which is the same
> shape as D3's `path.startswith(f"{dest}/")` assertion being vacuously satisfied by the bug. The
> same blind spot is still open for **go** and **rust**, which declare root files (`go.mod`,
> `Cargo.lock`) with the same shape and have never been run with two repos of their ecosystem.
>
> **Corrected 2026-08-13 (§24): closed for rust, still open for go.** Two Rust repos with different
> crates now build for real — and running them **found D15 and D16 on arrival**. That is the
> *"found by adding a second repo, not by reading"* shape for the **third** time, and this
> prediction was right about the mechanism twice over. **Go still has zero fixture repos**, so for
> Go the blind spot is not merely open, it is **untested in principle** until gazelle runs.
>
> **Corrected 2026-08-14 (§25): Go now has TWO fixture repos, and the blind spot is unchanged.**
> `acme-clitool-go` and `acme-digest-go` exist, are **opt-in by name**, and are **deliberately
> vacuous** — no Gazelle, no `go_library`, nothing compiled — so the second-repo test that found
> D13/D14 for JS and Python and D15/D16 for Rust **has still never been run for Go**. The sentence
> to update is *"zero fixture repos"*; the sentence that still stands is *"untested in principle
> until gazelle runs"*, and a **tripwire** now asserts that vacuity mechanically so no future
> reader can mistake the fixtures' presence for coverage.

---

### D15–D17 — found by adding the *first two Rust repos*, on their first run

All three were invisible to four checkpoints of Rust plumbing work that asserted our rendered
strings against our own expectations. None needed a subtle input: the fixtures are three-file
crates with one external dependency each.

**D15 — FIXED intra-wave, UNFIXED cross-wave. The fleet-wide `members` list named a directory the
per-repo snapshot did not contain.** Phase 3 cut each build worktree from the integration snapshot
**at that repo's own merge**, while fleet-wide root files are computed over **every plan prepared
so far**. Cargo **hard-fails the whole workspace** on an unreadable member —
`error: failed to load manifest for workspace member ... No such file or directory (os error 2)` →
`Error: Failed to generate lockfile`. **`fleet build` exited 7** with both Rust repos
`REQUIRES_HUMAN_INTERVENTION`, while the **last** Rust repo built and tested green: a pure
**ordering** property. **JS and Python escaped by tolerance, not by correctness** —
`npm_translate_lock` tolerates an absent pnpm importer directory, and a requirements file names
**distributions, not paths**. **Glob members fail identically, including a glob matching nothing.**
**Fixed** by cutting **one snapshot per wave** instead of per repo (`_prepare_build` split into
`_ingest_build_source` → one `_wave_snapshot` → `_plan_build`), plus a general, **ecosystem-neutral,
offline** `RootFileDomainError` pre-dispatch guard that every `dest` in the root files' domain
exists as a directory in every worktree about to be dispatched — verified to fire against the old
behaviour. **Snapshot immutability (SPEC §3.3) is preserved**; the per-repo cut point was an
artifact recorded nowhere, and **no existing test encoded it** (the immutability, wave-ordering and
Phase-3/Phase-4 ref-disjointness tests all pass untouched). ADR-0053. **What is NOT fixed, and it
is the most important line in this entry:** two Rust repos in **different waves** reproduce this
**verbatim** — the earlier wave settles against a smaller domain and is **never re-admitted**, so
its published root files stay **stale**. The guard checks only the repos a wave dispatches and
**both new tests assert only the intra-wave property**. Fixing it is a **spec amendment awaiting a
human decision** (ADR-0053 consequence 3).

> **Corrected 2026-08-14 (§25) — the sentence above beginning *"two Rust repos in different waves
> reproduce this verbatim"* is WRONG, and it was repeated in four places before anyone checked
> it.** Corrected in place rather than left standing, because it is **inaccurate about a
> mechanism** — the same class as the root-lockfile round's three corrections, not the
> superseded-but-instructive class. **Within one `fleet build` process the domain is monotone:** a
> plan exists only after its repo's merge, and every later wave's snapshot descends from every
> earlier merge, so a later wave's worktrees contain **every earlier dest** and the fatal cargo
> shape **is not reachable forward**. What actually survived cross-wave is the **milder D13 shape**
> — a wave settles **green** against root files a later wave replaces and is **never re-checked**:
> a stale verdict, not a failed load, and `fleet build` still exits 0. **The rest of the entry
> stands.** The residue that is real is now narrowed further by **ADR-0055** (see **D18**): root
> files are computed **once per run over the whole DB-derived domain**, so they can no longer
> shrink — but **a settled wave is still never re-admitted**, so a repo that succeeded in an
> **earlier invocation** still does not rebuild. **Two test docstrings in `tests/test_build_e2e.py`
> still carry the wrong claim** and need a code change (`docs/PROGRESS.md` §25 names them).

**D16 — FIXED, generally rather than for Rust. `crate.from_cargo` rewrote `//:Cargo.lock` in the
worktree and `_publish` committed the mutation.** One repo published a lock naming **both** members
while its siblings published the planned bytes; `git merge-tree` answered
`CONFLICT (add/add): Merge conflict in Cargo.lock`. **§11.6 byte-determinism was broken not by a
nondeterministic generator but by the build system editing the tree underneath the harness** — the
generator was innocent, and every layer reasoning about determinism was reasoning about the wrong
producer. **Fixed** by having `_publish` re-write **every declared support file's planned bytes**
via `buildgen.materialize`, the same loop GENERATE uses, whose docstring **already argued** bytes
must come from `SupportFile.content` and never from a re-read of the tree. **Checked, not assumed,
that no other ecosystem mutates the worktree:** `update_pnpm_lock` defaults **False** for the attrs
`js.py` sets, `pip.parse` has **no writeback**, the harness's own resolvers run in a **scratch
dir** — which is the argument for the general rule, not against it. **Disclosed cost:** cargo's
lock extension is **discarded every build**, so each Phase 3/4 build re-extends from the seeded
lock. **`is_dirty()` reasoning:** re-asserting planned bytes can only move a path from *differing*
to *identical*, so the guard **can never mint a commit it would not have minted before**; what
changes is that a build-system writeback now reads as *already-published* rather than as real work.
ADR-0054. **Not done, and optional:** `skip_cargo_lockfile_overwrite` as belt-and-braces — Rust-only,
needs a `bool` in `WorkspaceDep.attrs`, and **unverified at the pinned `rules_rust` version**.

**D17 — FIXED. A declared toolchain pin never reached `MODULE.bazel`, and the build used whatever
`rules_rust` defaults to.** The tag-attrs helper returns the declared `attrs` when non-empty and a
`{name, version}` fallback otherwise; `rust.py` declared a **non-empty** `attrs`, so its
`_RUST_VERSION` reached **nothing**. **The bug was `rust.py`'s declaration, not the helper**: the
`rust.toolchain` tag class has **no `name`/`version` attrs at all** — it takes `versions` as a
**string list** — so the fallback firing would have emitted an **invalid** tag. `py.py`, `go.py`
and `js.py` all put their version **inside `attrs`**; Rust was the odd one out. **Fixed** by
widening `ToolchainRequirement.attrs` to `dict[str, str | list[str]]` and declaring
`versions = [_RUST_VERSION]`. Held by a new **table-driven** test that asserts, for **every
registered adapter**, that a declared toolchain version appears in the rendered tag call — and it
asserts in **both directions**, so an adapter regressing to *no* toolchain **fails** instead of
passing vacuously (**two** adapters declare none by design: **jvm** and **unknown**). **Noted, not
fixed, per Rule 3:** `js.py` hardcodes its TypeScript version **twice** rather than using a
constant; the new test now catches divergence between the two literals. **Do not conflate two
versions:** the `rules_rust` toolchain pin in `rust.py` (`_RUST_VERSION`, currently `1.81.0`) is a
different thing from the workspace-local rustup toolchain (**1.97.1**) that generates fixture
lockfiles; they are not required to match and nothing asserts they do.

---

### D18 — found by re-invoking `fleet build` on a partially complete run, which is the only recovery flow there is

**D18 — FIXED. The published root files could SHRINK, and both invocations exited 0.** Reproduced
with `fleet build --wave 0` followed by a plain `fleet build`. The published `MODULE.bazel` lost
`rules_jvm_external` and `rules_rust` **entirely** — `bazel_dep`, `single_version_override`, the
whole `maven.install` block and the whole `crate.from_cargo` / `rust.toolchain` block;
`pnpm-workspace.yaml` and `.bazelignore` swapped importers; the root `BUILD.bazel` stopped
exporting `Cargo.toml` / `Cargo.lock`. **Mechanism — three individually reasonable facts:** `plans`
is **process-local and never rehydrated** from SQLite, `_open_phase_waves` **excludes settled
waves**, and `_check_root_file_domain` **structurally cannot fire**, because it read its domain off
the same shrunken `plans` and **a shrunken domain is trivially contained**. ADR-0053's guard was
**tautological against exactly this**. **Necessary condition:** at least one wave still
**unsettled** while an earlier one is **settled** — `--repo` and `--wave` on a **fully complete**
run do **not** reproduce it, because nothing is dispatched. **And it sat on the sole recovery
flow:** `fleet resume` is `_unavailable`, so re-invoking `fleet build` is the only way to continue
a partial run. **One prediction was refuted, and the refutation is the transferable part:**
`//:Cargo.toml` did **not** shrink — it was **orphaned**. With no Rust plan the file is **not
regenerated at all** and the root `BUILD.bazel` simply stops exporting it; a file that stops being
written looks nothing like a file rewritten smaller, and only the second was being watched for.
**Fixed** by hoisting three **run-level** passes ahead of all dispatch — ingest every eligible repo
in `(wave_index, repo_id)` order (each merge still alone under the writer mutex), **one** snapshot
after the run's last ingest with **every** ingested unit planned from it, and fleet-wide support
files resolved **once per run** — and by deriving the domain from **SQLite** (`_eligible_build_units`
joins wave members against `TRANSFORM`-SUCCEEDED phases across **all** waves) instead of from
`plans`. `_check_root_file_domain` gained a **coverage** half — the set the root files were rendered
from must **equal** the DB-derived domain — beside the existing **containment** half, with a new
`RootFileDomainDriftError`; **coverage is the non-tautological one and it fires against the old
behaviour**. `--repo`/`--wave` are now **dispatch filters only**, pinned by a test asserting a
narrowed run publishes **byte-identical** root files to a full-fleet run. **ADR-0055.**

**What D18's fix does NOT cover, recorded here rather than in a summary line.** **(1)** A settled
wave is **still never re-admitted**: what is fixed is that root files no longer **shrink**, not that
a repo which succeeded in an earlier invocation rebuilds. **(2)** The containment half still checks
only that a dest directory **exists**, not its contents. **(3)** The coverage half compares two
**driver-side derivations** and never consults the git tree. **(4)** A partially built branch is now
**legitimately un-`//...`-able** — after `--wave 0` or `--repo X` the root files **correctly** name
units whose packages were never generated, so `bazel build //...` over that branch fails **by
design**. **(5)** The **post-domain re-plan-failure** path has **no test**: it could not be induced
deterministically without a new seam. **(6)** A **whole-ecosystem resolve failure** drops that
ecosystem from the domain and the survivors publish a `MODULE.bazel` without it — **pre-existing in
shape, disclosed here for the first time, untested**. **(7)** Every invocation now **re-clones and
re-filters every eligible repo**, measured only on **2–9-repo fixtures**. **(8)** **No concurrency
testing** of two overlapping `fleet build` processes under the new structure.

**One deterministic regression was found and fixed honestly, and it is a fixture correction rather
than a harness one.** The real-Bazel two-Rust-repo test had used `--repo` purely for **disk
scoping**; with `--repo` now dispatch-only, the root files correctly describe repos nothing built
and `bazel build //...` fails on an ungenerated package. **The harness was right and the fixture was
wrong**, so the **fleet was bounded** instead of the dispatch. Separately, the `xfail(strict=True)`
that pinned this defect **XPASSed and was deleted** per the established precedent — its invariant
assertions untouched, its fixture guards replaced with a true measure of branch growth — and the
tree is back to **zero `xfail` markers**. Invariants **proved preserved, each with a named green
test:** snapshot immutability, the single-writer mutex, wave ordering, Phase-3/Phase-4 ref
disjointness, and **ADR-0054's publish contract**. Also verified **by grep** that **nothing in
`src/` ever un-merges a repo whose build later fails** — "history lands before the build is judged"
is **pre-existing behaviour**, not something this round introduced.

### D19 — found by reading the DEBUG output of the first green Go build, which is the only place it is reported

**D19 — OPEN. The versions Bazel fetches for Go are not the versions the harness pinned, and the
harness's `go.sum` is not what verified them.** Unlike every other entry in this section, D19 is a
**correctness gap rather than a missing test** — no assertion closes it; a code change does.
Verbatim from the build that this round's headline rests on:

    DEBUG: …/gazelle+/internal/bzlmod/go_deps.bzl:753:36: The following Go modules were required
    by the root module at the given versions, but were implicitly updated to higher versions due
    to transitive dependencies:
      golang.org/x/crypto: v0.31.0 -> v0.39.0
      golang.org/x/sys: v0.28.0 -> v0.33.0

**Mechanism, and one third of it is inherent while two thirds are not.** `go_deps` is **ONE** module
extension over the **whole** Bazel module graph: rules_go and gazelle each call `go_deps.from_file`
on their own `go.mod`, so Go **MVS runs across all three** — theirs and the harness's union — and
**a Go module path can have exactly one repository in a build**. A transitive floor above a harness
pin therefore **raises** it. **That part is inherent to Bzlmod**: there is no "resolve only from my
`go.mod`" mode, and the per-module levers (`go_deps.module_override`, `archive_override`) do not
change it in general. **The harness cannot fix it.** What the harness *can* fix is the other two:

1. **The silence.** `go_deps.config(check_direct_dependencies = "error")` is a **root-module-only**
   tag that turns that DEBUG line into a hard `fail()`. **The harness renders the root
   `MODULE.bazel` and could emit it. It does not.** A raise that only appears in DEBUG output is,
   by this document's own standing thesis, a check nobody ran.
2. **The unverified-fetch path.** gazelle's `_get_sum_from_module` answers a raised version **with
   no sum** by **printing** `No sum for …@… found, run … mod tidy` and returning `None` — i.e.
   **fetching that module with no checksum at all**, rather than failing.

**What this build actually establishes, stated exactly.** **No `No sum for …` line appeared anywhere
in the output**, so the raised zips **were** verified — **by gazelle's own `go.sum`, not by the sums
this harness resolved.** So: **the harness's sums are *consistent with* what Bazel selected; they
are not what Bazel *verified*.** A green Go build is evidence about `sums_from_go_mod` accepting the
harness's bytes for the modules whose versions survived MVS, and about nothing else. **Fixing item 1
is `docs/PROGRESS.md` §27's top Next item**, and it is deliberately not done in the round that found
it: turning the DEBUG into a `fail()` makes a currently-green build red on a condition the harness
**cannot resolve**, which is a decision about failure posture rather than a bug fix (ADR-0058,
*Agent Recommendation*).

**Corrected 2026-08-15 (§28) — D19 is NO LONGER OPEN, item 2 above is FALSE, and the attribution in
the paragraph above names the wrong ruleset. New status: NARROWED AND ACCEPTED** — the three thirds
were taken apart one at a time, **one was refuted by measurement**, **one was decided against on
evidence** (ADR-0059), and **the inherent one is accepted with its residual risk written out
below**. **Everything above is kept verbatim**, per this document's in-place correction convention:
a defect that was **overstated and then refuted by its own measurement** is exactly the kind of
record this ledger exists to keep, and deleting it would delete the evidence that the ledger
corrects itself.

**Item 2 — "fetching that module with no checksum at all" — is FALSE, and it is the half that
mattered, because it was the supply-chain half.** The reading of `_get_sum_from_module` was
**correct**; it stopped **one layer too early**. The `None` it returns lands in `go_repository`,
where `sum` is a **mandatory** attribute for **every** repo the extension creates — the repo is
flagged `is_module_extension_repo` from `internal_only_do_not_use_apparent_name`, which `go_deps.bzl`
passes for all of them — and `go_repository.bzl` **`fail()`s at fetch time**, with
`fetch_repo_env["GOSUMDB"] = "off"` set beside it and the comment *"the sum is a mandatory attribute
of go_repository, so we don't need to look it up."* Measured against the pinned gazelle **0.52.2**,
not reasoned: exactly one `h1:` line (`github.com/spf13/cobra v1.8.1`) was deleted from the resolved
root `go.sum` — after grepping **every** `go.sum` in the session's repository cache to establish it
was the **only** source of that hash anywhere in the module graph — and Bazel refused:

    DEBUG: …/go_deps.bzl:925:18: No sum for github.com/spf13/cobra@1.8.1 found, …
    ERROR: …/gazelle+/internal/go_repository.bzl:204:21: An error occurred during the fetch of
    repository 'gazelle++go_deps+com_github_spf13_cobra':
       Error in fail: No sum for github.com/spf13/cobra@v1.8.1 found, update go.sum with: …
    ERROR: no such package '@@gazelle++go_deps+com_github_spf13_cobra//': No sum for …
    ERROR: Build did NOT complete successfully

**Nothing is ever fetched without a checksum; the build fails closed.** A version nobody covers is a
**hard build failure**, at **every** setting of `check_direct_dependencies`.

**Corrected attribution.** The paragraph above says the raised zips were verified by **gazelle's
own `go.sum`**. **Measured: it was rules_go 0.61.1's.** rules_go's `go.sum` carries
`golang.org/x/crypto v0.39.0 h1:` and `golang.org/x/sys v0.33.0 h1:`; **gazelle 0.52.2's carries no
`x/crypto` `h1:` line at all**, and `x/sys` only at v0.28.0/v0.30.0. The conclusion the sentence
draws is unchanged and still correct — the harness's sums are *consistent with* what Bazel selected
and are not what Bazel *verified* — but the file that did the verifying belongs to the **other**
pinned ruleset, which is the difference between an operator grepping the right lock and the wrong
one.

**Item 1 — the silence — was investigated and deliberately NOT closed, on evidence (ADR-0059).**
`go_deps.config(check_direct_dependencies = "error")` was **measured**, not theorised: verbatim, it
fails the build at extension-evaluation time naming both modules and an exact remediation argv
(`Error in fail: The following Go modules were required by the root module at the given versions,
but were implicitly updated to higher versions due to transitive dependencies:
golang.org/x/crypto: v0.31.0 -> v0.39.0 / golang.org/x/sys: v0.28.0 -> v0.33.0`), aborting the
**whole monorepo** before any target builds. It is **neither necessary nor sufficient for the
checksum question**: it fires on a raised **direct root requirement**, while the checksum question
is a **selected version with no `h1:` anywhere** — and that already fails hard at every setting.
Raises of **indirect** requirements are never reported at all (`root_versions` is populated only for
non-indirect tags), and since `_go_mod_text` emits **no `// indirect` markers**, every requirement
in the harness's union `go.mod` counts as direct. The four options and their rejections are in
**ADR-0059**; **`src/` gained nothing**.

**What was implemented is two tests, and the reason they exist is the important sentence.** One
real-Bazel test deletes the single uncovered `h1:` line and asserts Bazel **refuses to fetch**; in
the same run it pins the other half by asserting the raise line begins with `DEBUG: ` and **not**
`Error in fail:`, so a future flip to `error` turns red **with an explanation attached**. One free
offline test asserts the rendered root `MODULE.bazel` contains no `go_deps.config` /
`check_direct_dependencies`. **The safety property belongs to the pinned gazelle version, not to any
harness code** — a bump restoring older fetch-anyway behaviour would reopen a real hole **with no
diff anywhere in this repository**, which is precisely why the measurement is now machine-checked
rather than written down here.

**What remains, and it is two things, neither of them a fetch without a checksum.** (i) **The
inherent MVS raise**, unchanged and unfixable inside the harness: `go_deps` runs MVS over the whole
Bazel module graph, and the harness cannot resolve against a graph it does not enumerate. (ii) **A
readability limitation, stated so a non-expert can act on it:** the monorepo's `go.sum` is **not** a
statement about which versions the monorepo builds against — it is **one of several** checksum files
Bazel consults. For any dependency a **pinned ruleset** requires more recently than your repos do,
the version **and** the hash that govern the build come from **that ruleset's** lock file, which
moves when `build.ruleset_versions` moves. **Guaranteed:** an attacker cannot substitute bytes,
because a version nobody covers fails the build outright. **Not guaranteed:** an operator cannot
read `go.sum` and know what shipped — that needs a **post-build attestation read out of Bazel**, not
a pre-build lock.

**Still not proven by any of this:** that the raise is **harmless** — only that an **uncovered**
version fails closed; anything about gazelle versions **other than 0.52.2** (which is the whole
reason the test exists); the `fleet build` end-to-end path (the tree is **assembled**, not
published); `go_test`; anything beyond **linux/amd64**; and a fleet with **conflicting** Go module
versions across repos.

### D20–D25 — found by reading `src/` for wiring rather than by running anything

Every entry in D1–D19 was found by **running a real tool**. These six were found by asking a
different question — *does the production call path reach this code at all?* — and each answer was
**no**. That makes them a different class, and the class matters: a boundary can be **REAL** in the
ledger above, with real binaries and real assertions, while the **production caller that would
invoke it does not exist**. Every one of these has tests that pass. None of those tests is
evidence, because the thing they exercise is not the thing that ships.

**D20 — OPEN. Commits are not probe-gated in production; the ast-grep parse probe is wired only in
tests.** `RewriteWorker.pipeline_for` (`workers/rewrite.py:252`) constructs its `RewritePipeline`
with `rules`, an `EngineRegistry`, `max_passes`, `params`, `tier` and `repo_id` — and **no
`probe=`**. The only `probe=` in `src/` is `workers/rewrite.py:456`, an unrelated local in the
repair-evidence renderer. Separately, `rewrite/apply.apply_patch` — the function whose docstring
calls `git apply` *"the ONLY writer of source files"* and which owns the post-apply probe gate —
has **no caller anywhere in `src/`**; the only occurrences are its own definition, two re-exports
and a docstring reference in `pipeline.py`. So the gate that
`test_astgrep_probe_gate_blocks_the_landing_and_no_commit_is_created` proves works, and that
`test_the_whole_path_pipeline_to_worktree_lands_exact_bytes` drives with an explicit
`probe=rewriter.probe_text`, is **assembled by the tests and by nothing else**. **Severity: high.**
A rewrite that produces unparseable output lands and commits in production exactly as it did before
`ast-grep` was vendored. **Would a test catch it? No, and this is the sharp part** — the existing
probe tests pass **because they wire the probe themselves**, which is precisely how the defect
stayed invisible. The test that would catch it asserts on `pipeline_for`'s **output**, not on a
pipeline the test built.

**D20 corrects a stale entry, and the correction is about the CAUSE.** `docs/PROGRESS.md` has
carried *"`FilePatch.parse_probe_ok` is always `False` **because no rewrite engine ships**"* at
§§ 923, 1058, 1210, 1386 and 1549. **The reason is now false** — `tools/bin/ast-grep` 0.45.1 is
vendored and the ledger's ast-grep row is **REAL** — while **the symptom is unchanged**:
`parse_probe_ok` is still `False` on every production commit. An engine shipped and the entry was
never revisited, because the entry named a blocker that had been removed rather than the mechanism
that was doing the blocking.

**D21 — OPEN, SECURITY. The `--replace-text` secret scrub is unwired end to end.**
`RelocationSpec.replace_text` (`vcs/filter_repo.py:117`) defaults to `None`; `filter_repo.py:139`
is the **only** other occurrence in `src/`, and it is the render site that never fires because
**no caller in `src/` ever sets the field**. The setting that would feed it,
`settings.history_scrub_file` (`settings.py:310`), defaults to `"config/rules/secrets.txt"` and is
**read by nothing** — one occurrence in `src/`, zero in `tests/`. And **`config/` does not exist in
this repository at all**, so the default names a path that has never been present. **Severity:
high, and it is the one entry in this document that is a security property rather than a
correctness one.** §11.4 promises secrets are scrubbed from rewritten history; the harness rewrites
history for every ingested repo and scrubs nothing. **Would a test catch it? No.** The ledger's
`git-filter-repo` row already says `--replace-text` has *"zero test hits of any kind"* — that row
was right about the coverage and is now joined by the wiring: there is no code path to cover.

**D22 — OPEN, SECURITY. The Gitea credential file's mode is never enforced.** `chmod`, `st_mode`,
`0o600` and `0o077` have **zero occurrences anywhere in `src/`**; all **ten** hits are in `tests/`.
The docstrings promise a mode-600 `curl -K` file and `build_forge` refuses nothing. **Severity:
medium** — the token stays out of argv (ADR-0040, tested), and this host's file happens to be
`0600` with `.secrets/` gitignored, so the exposure is a **world-readable file working silently**
rather than a leak that has happened. **Would a test catch it? No**, and the ledger's Gitea row has
named the missing `st_mode & 0o077` refusal in its "what would close the gap" column since it was
written. What is new here is only the count — *every* mode operation in this project is test-side —
which is what turns "not enforced" from an impression into a fact.

**D23 — OPEN. Retargeted edges are never persisted; both readers always see NULL.**
`state/repository.insert_edges` (`repository.py:1752`) enumerates **fifteen** columns and
`retargeted_from_repo_id` is not among them, though the column exists (`state/schema.sql:139`,
added by `migrations/v002_node_kind.py`), the model carries it (`models/graph.py:172`) and
`graph/cycles.py:736` computes it — *"preserving the original owner in
`retargeted_from_repo_id`"*, per its own docstring at `cycles.py:690`. **Two readers select the
column** (`cli.py:2941` and `cli.py:9430`) and both therefore read `NULL` for every row ever
written. **Severity: medium.** Nothing crashes; the §3.1 5b viii provenance of a contract-hoisted
edge is simply gone, so an operator asking *"which repo did this edge originally point at"* gets
`None` for every edge in every run. **Would a test catch it? Only a round-trip one** — insert an
edge carrying the field, read it back, assert it survives. No such test exists, and a test that
asserts on `EdgeRow` in memory passes.

**D24 — OPEN. Every `test_targets()` is vacuous fleet-wide, and it is the producer, not the
adapters.** `BuildUnit.test_srcs` and `BuildUnit.resources` (`models/build.py:53–54`) both default
to `[]`, and the **single construction site in `src/`** — `cli.py:7173` — sets `unit_id`,
`ecosystem`, `dest`, `srcs`, `published`, `internal_deps` and `external_coordinates`, and **neither
of the other two**. Every adapter's `test_targets()` opens with `test_srcs = self.test_sources(unit)`
followed by `if not test_srcs: return []` (`py.py:334`, `js.py:773`, `jvm.py:149`, `rust.py:265`),
so `[*adapter.generate_targets(unit), *adapter.test_targets(unit)]` on the next line of `cli.py` is
**`generate_targets` and nothing else, for every repo of every ecosystem**. **Severity: high.** No
generated `BUILD.bazel` in this project has ever carried a `py_test`/`js_test`/`java_test`/`rust_test`
target, which means §12.11's `tests_lost` baseline and Phase 4's `no_test_targets` handling are
reasoning about targets the harness structurally cannot emit. **Would a test catch it? No, and this
one has a named trap:** `test_targets()` is unit-tested per adapter with a `BuildUnit` the test
constructs **with `test_srcs` populated**, so all of those pass. The catching assertion is over the
unit `_plan_build` produces.

**D24 corrects a stale entry, and again the correction is about the CAUSE.** The empty
`test_srcs`/`resources` had been attributed to a **missing adapter hook**. The hooks exist and are
correct in all four ecosystems; what is missing is the **producer**. A reader acting on the old
cause would have gone to `ecosystems/`, found working code, and concluded the entry was stale
rather than mis-attributed.

**D25 — OPEN. `build.registry` is dead config, and its docstring claims the opposite in
load-bearing language.** `bazel/query.registry_args` is called from exactly two places —
`query_argv` (`query.py:151`) and `bazel_test_argv` (`query.py:180`). `query_argv` has **no caller
in `src/` at all**; `bazel_test_argv` has exactly one (`rdepverify.py:262`) and **`registry=` is
not among the arguments passed**. `buildverify._bazel_argv` (`buildverify.py:853`) never mentions
a registry — `bazel/lockfile.py` states this correctly in three places, including the refusal text
it emits. So **setting `build.registry` changes no argv the harness ever executes.** Meanwhile
`settings.py:500`'s docstring says *"Set it and `query_argv` / `bazel_test_argv` emit
`--registry=<url>` on every command"* and *"This is **load-bearing for verification**, not a
convenience … on a host that cannot reach BCR the choice is this setting or no verification at
all."* **Severity: medium, and the severity is entirely in the docstring.** An operator on a
BCR-blackholed network follows that text, sets the key, gets no change in behaviour and no warning,
and the failure they were trying to fix recurs identically. Note what this does **not** touch:
`payload.module_registry`, which the §33 lock guard reads, is a **different consumer of the same
setting** and does work — which is exactly why the dead half is easy to miss. **Would a test catch
it? No.** `registry_args` is well tested as a pure function; the untested proposition is that
anything calls it with a registry.

### D26–D33 — found by code review of the round's own landing, not by running it

These eight were found by reviewing the diff that landed §33's two items, plus the code adjacent to
it. **None is fixed**; all are recorded as OPEN, in the order the §33 *Next* list takes them.
Three of them (**D26**, **D27**, **D28**) are in the publish path this round touched, which is the
argument for reviewing a change and not only testing it.

**D26 — OPEN. `_publish`'s idempotence guard asks a question the tree cannot answer, and a
transient failure becomes `REQUIRES_HUMAN_INTERVENTION`.** The sequence is
`git.exec(["add", "--", *paths])` → `published = await git.is_dirty()` →
`output.already_published = not published` → commit if `published` (`cli.py:5177–5180`). The `add`
stages an **explicit pathspec**; `is_dirty()` is `git status --porcelain` over the **whole
worktree** (`vcs/git.py:331`). Those are different questions, and real Bazel guarantees they
disagree: `buildverify._bazel_argv` emits `--build_event_json_file=bazel-{unit}-events.json`, a
**relative** path, so the events file lands **in the worktree**; convenience symlinks land there
too; and `MODULE.bazel.lock` is deliberately excluded from `paths`. So on a PUBLISH-only re-entry
whose generated files are already committed, `add` stages **nothing**, `is_dirty()` still answers
**True** on the droppings, and `git.commit` runs against an **empty index** — `commit` commits the
index and `allow_empty` defaults to `False`, so it exits non-zero and the dispatch fails. **Severity:
high**, because the failure is **non-idempotent in the worst direction**: re-entry is the recovery
flow, and each re-entry fails the same way until the ladder is spent. **Corollary, and it is the
tell:** `already_published` can be `True` only when the worktree is byte-clean **after** a Bazel
run, which on this path it never is — so the field can never be `True` in production. It has
**zero occurrences in `tests/`**, so nothing has ever asserted the state it names. **Would a test
catch it? Yes, and cheaply:** re-run `_publish` over a worktree carrying one untracked file, and
assert the second call is a no-op rather than a failure.

**D27 — OPEN. `_publish_module_lock` compares against the FILE, not the BRANCH, and its docstring
names that as a feature.** The order is: registry check → read `MODULE.bazel.lock` **off the
integration worktree's filesystem** → `output.module_lock_published = True` → `if current ==
lock.content: return` → `materialize` → `add` → `commit` (`cli.py:5313–5330`). A dispatch that dies
**between `materialize` and `commit`** leaves the correct bytes **on disk and uncommitted**. Every
later dispatch — and, because the worktree outlives the process, **every later run** — then reads
that file, finds it equal, sets `module_lock_published = True` and **returns without committing**.
**The branch ships with no lockfile**, the offline container build exits **32** at `Error computing
the main repository mapping`, and the harness's own output says the lock was published. **Severity:
high**, and it is the exact defect ADR-0064 exists to prevent, reintroduced by the comparison's
subject. The docstring argues the choice explicitly — *"the comparison is against the file rather
than the index so it is unaffected by whatever else a merge just staged"* — which is a real
property and the wrong trade: unaffected by a staged merge, and equally unaffected by **whether the
bytes are on the branch**. **Would a test catch it? Yes:** materialize the lock, skip the commit,
re-enter, and assert `git show <ref>:MODULE.bazel.lock` resolves.

**D28 — OPEN. `module_lock_published = True` is set BEFORE the write.** `cli.py:5317` assigns the
flag; `materialize`, `add` and `commit` all run after it, and none of them is wrapped. Any failure
in the three persists a checkpoint claiming the lock was published. **Severity: medium** — lower
than D27 only because the field has **no consumer in `src/`** and **no assertion in `tests/`**
(two occurrences total, the declaration at `cli.py:4854` and the assignment), so today nothing
reads the lie. That is not a defence: a field written by production code, read by nothing and
asserted by nothing is a checkpoint waiting to be trusted. **Would a test catch it? No**, for the
same reason — there is nothing to assert against until the field acquires a reader.

**D29 — OPEN. `_c_toolchain_gate` misattributes timeouts and never-started probes as "no C
compiler", non-retryably — the THIRD instance of this bug in one gate.** The gate branches exactly
twice: `if result.ok: return None`, then `if result.exit_code == _DOCKER_CANNOT_RUN` (125), then an
**unconditional fallthrough** to the refusal that begins *"no C compiler in the sandbox image"*
(`buildverify.py:700–722`). Everything else lands there: a probe that **timed out**
(`result.timed_out`, whose exit code is not 125), and a `docker` binary that **never started at
all** (`ProcResult.started is False`). Neither consulted the image. Both are reported as a verdict
about its contents, and both are **`retryable=False`**, so a **transient** condition —
a loaded daemon, a slow pull-less start — **terminates the repo**. **Severity: high**, and the
count is the point: §29 recorded this gate's first implementation probing *"the wrong predicate in
both directions"*, §31 recorded the 125 misattribution, and **this is the third**. The 125 arm's
own comment states the correct principle — *"the probe never executed inside it … says nothing
about whether the image has a C compiler"* — and the fallthrough violates it for two more cases.
**Would a test catch it? Yes:** feed the gate a timed-out `ProcResult` and a `started=False` one,
and assert neither refusal mentions a compiler.

**D30 — OPEN. Gazelle capture globs only `BUILD.bazel`, so a repo carrying a legacy `BUILD` file
ships a package with no targets at exit 0.** `GENERATED_BUILD_FILE = "BUILD.bazel"`
(`cli.py:6449`) is used for all three of the scratch scan (`_scratch_build_files`'s
`root.rglob(GENERATED_BUILD_FILE)`), the directives plant, and the captured `SupportFile` path —
its own docstring says those *"have to be the same string or the capture silently finds
nothing"*, which is correct and does not cover the case where **Gazelle writes somewhere else**.
Gazelle writes into an **existing** build file when it finds one, and the corpus name it would find
is `BUILD`. The scan then reports the tree unchanged, `<dest>/BUILD.bazel` stays the
directives-only file the plan already had, and the publish is **green**. **Severity: high**, and
the mechanism is the one this document keeps counting: a check that was believed to be running and
was not checking the thing. **Would a test catch it? Yes:** a fixture repo carrying a legacy
`BUILD`, asserted to produce a captured file with a target in it.

**D31 — OPEN. `_assemble_gazelle_scratch`'s `copytree` follows symlinks; a cycle takes the RUN
down.** `shutil.copytree(plan.worktree / plan.dest, scratch / plan.dest, dirs_exist_ok=True)`
(`cli.py:6517`) is called with the default `symlinks=False`, i.e. **copy the target, not the
link**, over a tree cloned from an arbitrary corpus repository. Two failures follow. A **dangling**
symlink raises `shutil.Error`, which **is** an `OSError`, so `_gazelle_support_files`'s
`except (BuildFileGenerationError, OSError)` catches it — and that handler attributes the failure
to **every repo of the ecosystem**, because one invocation serves all of them. One bad link in one
repo therefore costs the **whole ecosystem's** BUILD generation. A symlink **cycle** recurses until
Python raises **`RecursionError`**, which is **not** an `OSError`, is caught by nothing on this
path, and **takes the run down**. **Severity: medium for the dangling link, high for the cycle** —
the first is a loud, contained, wrong-blast-radius failure; the second is an uncaught exception in
a fleet-wide pass. **Would a test catch it? Yes, and it costs two fixture files:** one dangling
link, one two-directory cycle.

**D32 — OPEN. The container leaks on every timeout, because the containerising path does not go
through the code that removes it.** `ContainerSandbox.run` is the method with the
`finally: await self.remove(spec.name)` and the pre-start `require_free_space` gate over **every
mount** (`sandbox/container.py:154–182`). `BuildverifyWorker._argv` does not use it: it builds a
`ContainerSpec` and returns `tuple(docker_run_argv(spec))` (`buildverify.py:903–920`), which the
worker then hands to its own runner. So on the deadline path the `docker run` client is killed and
**the container keeps running** — the very thing `on_cancel`'s docstring says killing the client
does not stop. `on_cancel` covers cancellation and removes the **attempt** name; the probe
container carries a different name (`…-cc-probe`, relied on `--rm`), and a timeout that is not a
cancellation reaches neither. The backstop that would catch the residue is
`ContainerSandbox.reap` — which, with `list_by_prefix`, has **zero call sites in `src/`**; §11.5
step 2's reaper is implemented and never invoked. And the spec's **`min_free_bytes` is inert on
this path**: it is passed into `spec_for_attempt` and only ever consulted inside `run`, so the
**cache mounts** are never checked (the worker does separately check the worktree at
`buildverify.py:746`). **Severity: medium** — a leaked `--network=none` container on a build host
is disk and memory, not a correctness or security failure, but it accumulates once per timeout with
no reaper behind it. **Would a test catch it? Only an integration one:** a real container, a
deadline shorter than the command, and `docker ps --all` afterwards.

**D33 — OPEN, and it is a defect in a CLAIM rather than in behaviour.
`_check_root_file_domain`'s coverage half cannot currently fail.** The half reads
`covered = {repo_id: plans[repo_id].dest for repo_id in plans}` and raises unless
`covered == dict(domain)` (`cli.py:6913–6915`). ADR-0055's argument for it is that `domain` is
derived from **SQLite** while `plans` is **process-local**, so the comparison is between
independent derivations. That was true of the shape D18 produced. It is **not** true of the code as
it stands: in `_build_impl`, `domain.pop` at `cli.py:7780` and `cli.py:7828` both fire on
preparation failures that occur **before** `plans[repo_id]` is ever assigned (`cli.py:7812`), and
the only other removals are the **paired** `plans.pop` / `domain.pop` at `cli.py:7891–7892` and
`cli.py:7933`. Every mutation is therefore in **lockstep**, and `covered == domain` holds **by
construction**. **Severity: low, and deliberately so** — the guard is a genuinely useful **lint
against a future edit** that removes from one and not the other, which is exactly the edit D18 was.
What is wrong is the **description**: ADR-0055 presents coverage as *"the non-tautological one"*
and the round that landed it called it *"a checked fact rather than a hope"*. It is a hope with a
tripwire attached, and a reader who believes the stronger claim will not write the test that would
make it true. **Would a test catch it? No test can** — a property that holds by construction has no
failing case to assert; what closes this is a **mutation check** run against the guard, which is
how it was established.

### Nine open items that were already CLOSED, and why they are listed rather than deleted

A full backlog audit this round verified **nine** items recorded as open against the tree and found
them **already done**. They are listed because a stale open item is not free: it is re-dispatched,
re-investigated, and — as **B** of `docs/PROGRESS.md` §33 records — it can be handed to an agent as
work. **Three are named here because they are the ones re-verifiable by name from this document's
own text**; the audit's remaining six were counted in the round's report and are not reproduced
here rather than restated on trust, which is the failure mode this section exists to stop.

1. **SPEC §9's `ruleset_versions` block — CLOSED.** The gazelle ledger row and older checkpoints
   treat §9 as restating a stale version table. `docs/SPEC.md:6059` now carries
   `# ruleset_versions:  # OMITTED ON PURPOSE`, and `SPEC.md:6166` explains that the table lives in
   `src/fleet/settings.py`, is the authority, is deliberately **not** reproduced, and is verified by
   `test_every_pinned_ruleset_version_loads_under_real_bazel` — *"a version list restated here
   cannot be verified, and restating it is how this section went stale."* The document fixed the
   problem and the backlog kept the ticket.
2. **`settings.gazelle_binary` "referenced by nothing in `src/`" — CLOSED.** That sentence is still
   in the gazelle row above and has been false since §26: `cli.py:6680` passes
   `binary=settings.config.build.gazelle_binary` into `_run_gazelle`, and `cli.py:4428` documents
   the seam. The clause it is welded to — *"there is no `bazel run //:gazelle` call site anywhere"* —
   is still **true** and is no longer the same claim, which is why the pair survived: half of a
   sentence went stale.
3. **`go.sum` "zero occurrences in `src/`, `tests/` or `docs/`" — CLOSED**, and this one is
   **ADR-0050's named blocker**. `go.sum` occurs in `src/fleet/cli.py`, `src/fleet/ecosystems/go.py`,
   `tests/test_workers_build.py`, `tests/test_ecosystems.py` and `tests/test_build_e2e.py`. It has
   been false since §21, and the gazelle row's own later update paragraphs say so — the **blocker
   sentence was never retracted where it was first written**, only contradicted further down the
   same cell.

### The "right symptom, wrong cause" pattern — a distinct failure mode from a stale entry

The audit's more useful finding is that several open items were **not stale at all**: the symptom
they described was still exactly true, and the **cause** attached to it had become false. **D20**
and **D24** are the two worked examples, and both are recorded above with their corrections
attached to the defect rather than to a list.

**Why this is worse than a stale entry, and it is worth stating plainly.** A stale entry fails
**safe**: a reader checks it, finds the symptom gone, and closes it. An entry with the right
symptom and the wrong cause fails **dangerously**: a reader checks the **symptom**, finds it
present, concludes the entry is current, and then acts on the **cause** — going to
`ecosystems/` for D24's missing adapter hook and finding four working ones, or waiting for a
rewrite engine to ship for D20 when one shipped five checkpoints ago. The entry is self-confirming
in the half that is cheap to check and wrong in the half that determines what to do. **The
convention this file adopts, going forward:** an entry that carries a cause must be re-verified
against the mechanism, not against the symptom, and a corrected cause is recorded **in place beside
the original** — as D19's correction block does — because "the symptom persisted while the reason
changed" is itself the evidence.

### Eight contradictions between documents, and what they have in common

The same audit found **eight** claims where two documents disagree about the tree. The
`rdepverify`-containerisation trio — this file's own OPEN GAP bullet, **ADR-0060 consequence 5**
and **ADR-0062 consequence 7**, all asserting a containerised Phase 4 that does not exist — is
corrected in place at the bullet itself, and it is the instructive one because **all three cite
each other rather than the code**. That is the mechanism behind every contradiction in the set: a
claim enters one document from a measurement, is restated in a second as a reference, and the
restatement outlives the measurement. **Guardrail 2 already forbids it** — verify against the
codebase, never against another document's summary — and the eight are what non-compliance looks
like after ten checkpoints.

---

## Honest summary of the shape of the coverage

The harness's **git** layer is genuinely well tested, its **state machine** is genuinely well
tested, and it now has exactly **one** live-forge integration (Gitea) with an end-to-end
create → merge → observe-`MERGED` path behind it.

Its **build** layer had been tested against a mirror of itself: the fakes were written from the
same understanding as the code, so they agreed with it, and four independent defects sat behind
that agreement. Those four were fixed, seven more took their place, six of those seven were closed
and the last withdrawn, and this round closed **D12**. The end-to-end run went **0 of 4 → 3 of 4 →
4 of 4**, with the `xfail(strict=True)` deleted rather than left passing: two repos now build
against a dependency this harness migrated, one of them **type-checking** the import, and the
fleet runs a **real dependency resolver** instead of a synthesized lock with no transitive
closure.

**What that progress does not license.** Every count above is one fixture monorepo of three-file
repos — four in the base fixture, five in the two-repos-per-ecosystem cases the root-lockfile round
added, and **exactly one repo for jvm** (`acme-commons-java`) and **zero for go and zero for
rust** — corrected: the earlier claim of "exactly one repo for go, jvm and rust" was true of jvm
only, and overstated two ecosystems out of three. **Nothing in this project compiles a line of
go, jvm or rust.** The resolver rows are REAL on inputs that pose no resolution problem. And one
sentence in this paragraph has to be read against D13/D14 now: *"four three-file repos"* was never
only a statement about scale — **one repo per ecosystem made the root-file contract vacuously
true**, and it hid two silent-drop defects behind a green suite for five checkpoints. For go and
rust the condition is **worse than one repo, it is none** — the root-file contract there is not
merely vacuously true, it has never been exercised at all. And the largest single boundary — `ast-grep`,
Phase 2's primary rewrite engine — is at **zero** for the fifth checkpoint in a row, with D12(a)
still the best argument for closing it: the only rewrite rule this project has ever run end to end
emitted a Bazel label as a TypeScript import specifier, no offline test could tell (every layer
between the rule and `tsc` compared our strings to our strings), and only a real build noticed.

Two verdicts about *this document* are worth keeping. First, **a skipped test is not a passing
test**, and the skip that hid the only real check on generated Bazel output was invisible in a
green summary line for an unknown number of runs — the registry paragraph in the header is the
fix and the confession. Second, **D5 was diagnosed twice by reading and was wrong both times**;
one dump of the real snapshot trees ended it. Both failures share a shape with the fakes: a claim
that nobody made the machine check.

A third belongs beside them now. **The harness's own exit code is not a verdict on the monorepo.**
D13 and D14 both ended with `fleet build` reporting every repo `SUCCEEDED` over a tree real Bazel
refuses, because a wave that settled green against an earlier root file is never re-admitted when a
later wave replaces it. That is the same failure mode as the skip and the same as D5 — a green
summary line standing in for a check nobody ran — and it is the reason both fixes are pinned by a
direct `bazel build //...` over the merged tree rather than by `fleet build`'s status. **Third
instance, same shape.**

**Corrected 2026-08-13 (§24) — three sentences in the two paragraphs above are now wrong about
rust, and one of them is wrong in a way that matters.** (i) *"zero for go and **zero for rust**"* —
there are now **two** Rust fixture repos. (ii) *"Nothing in this project compiles a line of go, jvm
or rust"* — **it compiles rust**: a real `bazel build //...` over the integration checkout exits 0
and produces **`.rlib` artifacts for both crates**, which is the first line of Rust this project has
ever compiled. (iii) *"For go and rust the condition is worse than one repo, it is none"* — true of
**go only**; for rust the condition is now **two**, and the very first run of those two produced
**D15 and D16**, which is this paragraph's own thesis confirming itself a third time. **Everything
this paragraph says about go and jvm stands unchanged**, and go's is the harsher case: gazelle still
never runs, `generate_targets()` returns `[]` for Go, there is **no Go fixture repo**, and a
two-Go-repo build test would still pass **vacuously** — so **go remains UNPROVEN** and adding
fixtures before gazelle would manufacture exactly the green-but-empty result this document exists to
catch. **And the "third instance, same shape" count is not retired by this round — it is what the
unfixed cross-wave residue of D15 would become.** Option (i) in ADR-0053 consequence 3 (scope
build-time root-file content separately from published content) makes every repo **build against
something it does not publish**, which is a **fourth** instance of the same shape; that is a reason
the decision is a human's and not an agent's.

**Corrected 2026-08-14 (§25) — two claims in the paragraphs above have moved, and one of the
corrections is against this document's own repeated prediction.** (i) *"there is **no Go fixture
repo**"* — there are now **two**, and everything else in that sentence stands: gazelle still never
runs, `generate_targets()` still returns `[]` for Go, **no Go is compiled by anything**, and the
fixtures are **deliberately vacuous with a tripwire asserting it**, so **go remains UNPROVEN** and
the "adding fixtures before gazelle manufactures a green-but-empty result" warning was answered by
making the emptiness **an assertion** rather than by ignoring it. (ii) *"the 'third instance, same
shape' count is not retired by this round — it is what the unfixed cross-wave residue of D15 would
become"* — the residue was **misdescribed**: D15 cross-wave is **not reachable** (see the correction
under D15), the surviving shape was **D13's stale verdict**, and **D18** then showed the same shape
doing real damage — root files that **shrink** while `fleet build` exits 0, **twice**. So the count
is not retired: **D18 is the fourth instance of "the harness's own exit code is not a verdict on the
monorepo"**, it arrived by measurement rather than by the ADR-0053 option (i) route this paragraph
predicted, and **ADR-0055** closes the shrink while leaving "a settled wave is never re-admitted"
standing.

**Corrected 2026-08-15 (§27) — the sentence *"Nothing in this project compiles a line of go, jvm or
rust"* above, already corrected once for rust, is now FALSE for go as well, and every "go remains
UNPROVEN" in the two §24/§25 correction paragraphs goes with it.** Real **Bazel 9.2.0** loads,
analyses and **compiles** a generated Go tree built from **the harness's own unmodified output**,
producing real **`.a` archives**, with the **cross-repo edge resolved at analysis time** — so **go's
verdict is REAL**, and what stands unchanged in those paragraphs is now **jvm alone**: still
**exactly one repo** (`acme-commons-java`), still nothing compiled, and — new this round — its
`load("@rules_java//java:defs.bzl", …)` names a ruleset that is **deliberately absent** from
`ruleset_versions`, relying on Bazel's injection, with **no real-Bazel test ever loading a generated
Java package**. **Three things this correction must not be read as licensing.** (i) The Go proof
**does not run `fleet build`**: the tree was **assembled from generated bytes**, deliberately,
because the failure being chased lives inside Phase 3's own per-repo `bazel build` — so
`materialize`/`_publish` over real generator output is still **fake-covered for go**, and the
warning this section has repeated since D13 (*"the harness's own exit code is not a verdict on the
monorepo"*) is **neither confirmed nor retired** by this round, because the harness's exit code was
never what was asserted. (ii) The fixture is **two repos, one cross-repo edge, five direct
requirements, linux/amd64, no `go_test`** — the *"one repo per ecosystem made the root-file contract
vacuously true"* lesson is answered for go at **two**, which is the count that produced D13/D14/D15
elsewhere, not a large number. (iii) **A green Go build is not a statement about the versions Bazel
fetched** — `go_deps` raised two of the harness's pins during MVS and the raised zips were verified
by **gazelle's** sums rather than by this harness's, which is **D19**, and it is the first entry in
this document that is a **correctness gap rather than a missing test**.

**Corrected 2026-08-15 (§28) — the clause immediately above reading *"the raised zips were verified
by **gazelle's** sums"* is wrong about which ruleset, and the "correctness gap" framing is now too
strong.** **Measured: the sums that verified them are rules_go 0.61.1's**, not gazelle's — gazelle
0.52.2's `go.sum` carries **no** `x/crypto` `h1:` line at all. And the supply-chain half of D19 was
**refuted by measurement**: deleting the one uncovered `h1:` line makes Bazel **`fail()` at fetch
time** rather than fetch unverified, so **nothing is ever fetched without a checksum and the build
fails closed**. **D19 is now NARROWED AND ACCEPTED** — see the correction block under D19 itself and
**ADR-0059**. What survives is the **inherent** MVS raise plus a **readability** limitation (the
monorepo's `go.sum` does not tell an operator what shipped), and the true first-of-its-kind claim in
that clause is narrower than written: D19 was the first entry here to be **opened as a correctness
gap**, and it is also the first to be **closed by measurement refuting its own defect text**.

**Corrected 2026-08-15 (§29) — every "real Bazel" verdict in this document carries an undeclared
precondition, and it is not a Go question.** **Bazel cannot analyse a target of any language
without a discoverable C compiler**: `@@rules_go+//:stdlib` — the Go standard library, **cgo or
not** — depends on `@@rules_cc++cc_configure_extension+local_config_cc//:cc-compiler-k8`, and with
no compiler the fetch of that repository fails at **loading** time (`Cannot find gcc or CC`),
which under the verify worker's default `--keep_going` surfaces as **`INFO: Found 0 targets`**.
Every green Bazel result recorded above was measured on a host carrying **`gcc` 13.3.0** that
**nothing in this document, `src/` or any ADR ever named** — the word **`cgo` appeared nowhere in
`src/` or the ADRs** before this round, and **neither Go fixture uses cgo**, so the suite was
**structurally blind** to a dependency it was relying on in every row. A gate now declares it for
**sandboxed** runs (**ADR-0060**), and **that gate's own first implementation probed the wrong
predicate in both directions** — passing a clang-only image and refusing a working
`ENV CC=…` one — which is **a fifth instance of the shape this section keeps counting**: a check
that was believed to be running and was not checking the thing. **Two open gaps sit beside it and
are recorded rather than fixed**: the verify container's `disk_cache`/`repository_cache` mounts
are **inert** (`_bazel_argv` emits neither flag), so a cold `--network=none` run **cannot resolve
a single Bazel module at all**, compiler or not; and `settings.verify.container_image` is a
**placeholder with no Dockerfile in this repository and no test that exercises a real image**.
**`rdepverify` has no gate.** The corpus figure this document has carried since §25 — **25 Go
repos** — is also corrected to **31** (of which **10 carry cgo**), with the exposure being
**31/31** for the missing compiler and **10/31** only for the headers-and-libraries problem
downstream of it.

**Corrected 2026-08-16 (§31) — both "open gaps" in the paragraph above have moved, and NEITHER
correction makes a sandboxed run work.** (i) *"the verify container's `disk_cache`/`repository_cache`
mounts are inert (`_bazel_argv` emits neither flag)"* — **false since §30**: both flags are emitted
in Phase 3 and Phase 4 (**ADR-0061**), and as of this round the rdeps **`bazel query`** carries
`--repository_cache` too (**ADR-0063**, disk cache **deliberately** omitted because a query executes
no actions and a measured probe wrote **zero files** there). (ii) *"`settings.verify.container_image`
is a placeholder with no Dockerfile in this repository and no test that exercises a real image"* —
**`docker/fleet-build.Dockerfile` now exists and builds**, the setting names the **local tag**
`fleet-build:9.2.0-bookworm`, and a test does run the real image (**ADR-0062**). **What is emphatically
NOT corrected is the conclusion those gaps supported.** A cold `--network=none` run **still cannot
resolve a single Bazel module**, because the repository cache the container is handed is **created by
`cli._cache_mounts` and never populated by anything** — the flag now names a directory that is
**empty**, which is a different defect with the identical outcome. **No Bazel has ever executed
inside the new image** (`command -v bazel` resolves a path; it does not run it), the exit-36 fix is
**argued structurally rather than exercised**, and the image test **never pulls**, so a stale
locally-tagged image would pass it. **The sandboxed path remains argv-proven only**, and *"a check
that was believed to be running and was not checking the thing"* keeps its count: this round found
the same shape once more, in `_c_toolchain_gate` reporting **"no C compiler in the sandbox image"**
for `docker run`'s **125** — an exit that means the container **never started**. **`rdepverify` still
has no gate.**

**Corrected 2026-08-16 (§32) — the paragraph immediately above names the wrong missing artifact, and
the correction is larger than the claim it fixes.** *"A cold `--network=none` run still cannot
resolve a single Bazel module, because the repository cache the container is handed is created by
`cli._cache_mounts` and never populated by anything"* — the **conclusion stands and the cause is
incomplete**. **Measured** in the real `fleet-build:9.2.0-bookworm` image under real
`docker run --network=none --user $(id -u):$(id -g)`: a **warm** repository cache with **no
lockfile** is **exit 32**, and the **same warm cache with a matching lockfile is exit 0**. So an
empty cache was **never the whole reason**, and seeding one — §31's top-priority next task — would
**not** have produced a green sandboxed build. **The harness carried no `MODULE.bazel.lock` at all**
(`grep -rn "MODULE.bazel.lock\|lockfile_mode" src/ tests/ docs/` → **nothing**), which means **the
monorepo this harness ships could never have built offline**, and **nothing in this suite could have
caught that**, because **no offline build has ever run here**. That is a **new entry in this
document's running count and the purest instance of it**: not a check that was believed to be
running and was not checking the thing, but a **property nobody ever asked a machine about**. Also
corrected: the `--registry` mismatch §31 recorded as *"an unresolved mismatch sitting across both"*
seeding options is a property of **the lockfile's URL keys, not of the cache** — a **mirror-warmed
cache with a bcr-keyed lock is exit 0**, because both BCR addresses serve **byte-identical** files
into a **content-addressed** cache. **What is NOT corrected is the verdict.** A lock is now
published (**ADR-0064**) and its registry keys can be checked, and that is **all**: **no offline
container build has been attempted**, **no cache has been warmed**, the lock is **not proven
sufficient** offline, and **the sandboxed path is still RED**. And a **new named structural
limitation, L1**, joins it: **the JVM ecosystem cannot build offline under any warming strategy**,
because unpinned `maven.install` resolves through **coursier**, which **never passes through Bazel's
downloader** — so everything this section says about jvm gets **worse**, not better, and unlike the
other jvm gaps it **does not close by adding fixtures**.

**Corrected 2026-08-16 (§33) — nothing above is refuted, and that is the finding.** A round that
ran **five agents in parallel** produced **fourteen** new defects (**D20–D33**) and **zero**
corrections to any measurement in this document. Every one of the fourteen was found by **reading
`src/` for wiring or by reviewing a diff** — not by running a tool — and the class they form is new
here: **D1–D19 were all found by running a real tool against real output**, whereas **D20–D25** are
cases where the ledger's verdict on a boundary is **REAL and the production caller does not
exist**. Three shipped code paths are gated **only in tests** (**D20** the parse probe, **D21** the
secret scrub, **D22** the credential mode), and one — **D25** — is a setting whose docstring calls
it *"load-bearing for verification"* while nothing on any argv reads it. **What this costs the
document is a qualifier on every REAL verdict above:** those rows are honest about **what the
tests drive**, and this round establishes that "the tests drive it" and "production drives it" are
**independent questions this file had never separated**. Not one of the fourteen would have been
found by running the suite harder; all fourteen have passing tests today.

**The record decayed faster than the code, and it is measured rather than felt.** The same audit
found **nine** open items **already closed**, **eight** cross-document contradictions, and — the
worse class — several entries carrying the **right symptom and the wrong cause**, which fail
dangerously where a stale entry fails safe. Both are recorded in their own sections above. **And a
third multi-agent hazard joins the concurrent-pytest reaper (§19) and the `.bazelrc`-precedence
trap (§30):** with concurrent agents on one tree, *"verified against the tree"* means *"verified
against whatever was there at that instant"*, and **an audit cannot distinguish landed code from
another agent's uncommitted edits** — which this round demonstrated by producing a false
"you are duplicating work" instruction from a correct read of a mid-write worktree. That hazard is
specific to this document's own method: **every verdict here is a claim about the tree at a
moment**, and the moment is now something the reader has to be told about.

---

## D34–D45 — THE FOUR-STATE COLLAPSE: twelve more instances of one bug, all found by reading

**None of these twelve was found by running anything.** They came from a read-only sweep that took
**one** defect already in the ledger — **D29**, `_c_toolchain_gate` reporting *"no C compiler in
the sandbox image"* for a probe that never ran — and asked the only question that matters after the
same shape appears five times: *how many more of these are there?* Twelve, and **none of them is
among D20–D33**. In a document whose recurring thesis is *"only running things finds defects"*,
that is worth stating plainly: **the highest-yield hour in this round was spent reading, and the
reason it worked is that the family has a mechanical cause that can be grepped for.**

### The family, and the cause a future reviewer can grep for

**Name: the four-state collapse.** *Code that reports a verdict it did not establish.* A check
receives a result that could mean several different things, collapses them into one boolean, and
then reports **one** of the causes as an established fact — usually **non-retryably**, and usually
pointing an operator at a file that is fine.

**The mechanism is two definitions and it is why the family is pervasive rather than incidental.**
`util/proc.py:261–271` synthesises the passed-deadline result with **`timed_out=True` AND
`started=False` AND `exit_code=124`** (`TIMEOUT_EXIT_CODE`, `proc.py:57`) — **all three at once** —
and `ProcResult.ok` (`proc.py:84–87`) is `started and not timed_out and exit_code == 0`. So every
`if not result.ok` in this codebase is true for **four distinct causes**: (a) the command ran and
its answer was *No*; (b) the command ran and was killed at the wall clock; (c) the binary never
started; (d) the deadline had already passed and **nothing was attempted** — and (d) presents as
(b) and (c) simultaneously. `ProcResult` is honest: it carries `started`, `timed_out` and
`exit_code` as separate fields. **`ok` is the lossy projection, and almost every caller reads only
the projection.**

**Branch order is load-bearing, and it already costs a branch in `src/`.**
`classify_build_failure` tests `if result.timed_out:` (`buildverify.py:368`) **before**
`if not result.started:` (`buildverify.py:370`). Because the only site in `src/` that ever
constructs `started=False` is `proc.py:269`, which also sets `timed_out=True`, **the `not started`
branch is dead code via `util.proc.run`** — its `TRANSIENT_INFRA` verdict is unreachable in
production and reachable only from `tests/test_workers_build.py:2019`, which builds the
`ProcResult` by hand. A reader auditing this family must read the branches **in order**, not as a
set.

**How to find the rest.** `grep -rn "result\.ok\|\.ok\b" src/` and, at each hit, ask the two
questions this family is made of: *how many causes reach this branch*, and *does the message name
one of them as a fact?* The honest form is `if result.started and not result.timed_out:` with an
explicit `exit_code` reading, or a message that prints `timed_out=` rather than a diagnosis.

### Tier 1 — burns the repo

**D34 — OPEN. `classify_build_failure` has no `125` branch, and the build steps ARE `docker run`.**
`BuildverifyWorker._argv` (`buildverify.py:889–920`) returns `tuple(docker_run_argv(spec))` whenever
`payload.image is not None`, so the process the classifier judges is **the docker client**, and a
real build or test can exit **125**: unreachable daemon, image deleted mid-run, malformed
`--memory`/`--cpus`. **125 is in neither `INFRA_EXIT_CODES` (`{8, 9, 36} | OOM_EXIT_CODES`,
`buildverify.py:206`) nor `UNREPEATABLE_EXIT_CODES` (`{2, 127}`, `buildverify.py:212`)**, so it
falls through every branch to the terminal line `return (TEST_FAILURE if unit == TEST_UNIT else
BUILD_ERROR), True` (`buildverify.py:388`). **A daemon restart mid-wave is therefore reported as a
broken `BUILD.bazel`**, spends all three ADR-0014 rungs — **two of them LLM-bearing, prompting a
model to repair a file that is fine** — and lands `REQUIRES_HUMAN_INTERVENTION` anyway. **Severity:
high**, and the aggravating fact is that **the same file already knows this**:
`_DOCKER_CANNOT_RUN: Final = 125` is defined at `buildverify.py:114`, **245 lines above the
classifier**, and is consulted in exactly one place — the `_c_toolchain_gate` probe
(`buildverify.py:675`) — whose own comment states the principle the classifier violates. **Would a
test catch it? No.** `test_the_exit_code_table_is_the_policy` (`tests/test_workers_build.py:1974–1993`)
has rows for **1, 2, 3, 4, 8, 9, 36, 127, 137 and -9** and **no 125 row**; the missing row is the
defect. Adding `125 → TRANSIENT_INFRA, True` closes both.

**D35 — OPEN. `clone._unshallow` turns every transient failure into `REQUIRES_HUMAN_INTERVENTION`,
on the single most transient operation the worker performs.** `_unshallow` (`clone.py:478–489`)
runs `git fetch --unshallow` **inside `ctx.limits.git_net`** — it holds the network limiter, which
is the harness's own statement that this is the call most likely to be slow — and on any non-`ok`
returns a **gate string**. `run` maps a gate to `FailureClass.PREFLIGHT, retryable=False`
(`clone.py:302–310`), which is terminal. **`PREFLIGHT` is defined in `workers/base.py:136` as
*"the repo's own shape; identical on every attempt"***, and a network timeout is the opposite of
that. **Severity: high** — one slow fetch permanently sidelines a repo that would have cloned on
the next attempt. **The class already knows how to do this correctly:** `_error_for`
reads `timed_out` off the exception and returns `TIMEOUT`/`TRANSIENT_INFRA`
with `retryable=True` (`clone.py:555–580`) — but it is only reachable from the `except` path, and `_unshallow` calls
`git.exec(..., check=False)`, which **returns instead of raising**, so the correct handler 77 lines
below is bypassed by construction. **Would a test catch it? No** — see D45.

**D36 — OPEN. `clone._preflight` reports `EmptyRepo` for a `rev-parse` that never ran, and returns
`status="ok"` while doing it.** `head_sha = await git.resolve(branch)` (`clone.py:409`);
`Git.resolve` (`vcs/git.py:265–271`) is `rev-parse --verify --quiet` returning
`sha if result.ok and sha else None`, so it answers **`None` for a passed deadline exactly as it
does for a rev that does not exist**. `_preflight` then appends the finding `"EmptyRepo"` and
returns early (`clone.py:410–423`), `run` takes `cut_worktree = ... and preflight.head_sha is not
None` (`clone.py:313`) — **false**, so **no worktree is cut** — and returns
`WorkerResult(status="ok", ...)` with `worktree_path=None`. **Severity: high, and it is the worst
shape in this set:** the other eleven persist a wrong verdict as a **failure**, which an operator
reads; this one persists a wrong finding **as a success**. Downstream workers then fail on a
**missing worktree** (D40's gate) and blame the clone that reported green. **Would a test catch it?
No.** The catching test asserts that a timed-out `resolve` cannot produce `status="ok"` at all.

**D37 — OPEN. A timed-out parse probe is reported as *"no rewrite engine is installed on this
host"*, the run still exits SUCCESS, and the `break` abandons the probe for every remaining file in
the repo.** `AstGrepDriver._scan_for_error_nodes` (`rewrite/astgrep.py:181–207`) is **correct
locally**: it reads `if result.started and not result.timed_out:` before any exit code, and raises
`EngineUnavailableError` otherwise, printing `timed_out=` in the message. **One layer up it is
undone twice.** `cli._transform_criterion` catches that exception into `unprobed`
(`cli.py:4162–4163`) — a list rendered by `cli.py:3090–3097` as a **non-blocking warning that adds
no violation**, so `§3.2`'s fourth clause silently goes unchecked and the command still exits
SUCCESS — and the catch site is a **`break`**, which exits the `for unit in rewritten` loop and
therefore **skips the probe for every remaining rewritten file in that repo**. **Severity: high.**
One slow probe on file 1 of 40 turns a corrupt rewrite in files 2–40 into a green transform.
**Operator-facing text: *"no rewrite engine is installed on this host"*** — for a host where
`tools/bin/ast-grep` is vendored and ran. **The docstring one layer down claims the tool failure
*"raises rather than resolving to a parse result nobody measured"*: true of the helper, false of
the pipeline**, which is the same doc-versus-caller split D25 records. **Would a test catch it? No**
on both halves; the cheap one is a timed-out probe asserted to produce a **violation**, not a
warning, and a `continue` asserted to reach file 2.

### Tier 2 — wrong operator target, right retry direction

**D38 — OPEN. `filter_repo.relocate` reports *"install git-filter-repo"* for two conditions that
are not that, one of them via a substring proxy.** `if not result.started or result.exit_code == 127
or "No such file" in result.stderr_tail:` → `FilterRepoUnavailableError` (`vcs/filter_repo.py:163–167`).
**Three predicates, one message.** `not result.started` is the passed-deadline case (D34's
mechanism). And the third is a **substring match on stderr** — the thing §3.3 says the harness
never does — while `filter_repo_argv` (`vcs/filter_repo.py:123–143`) emits `--path` for every
`source_paths` entry and `--replace-text <file>` when set: git-filter-repo prints
`No such file or directory` for a **missing `--replace-text` file** or a **bad `--path`**, neither
of which means the binary is absent. **Severity: medium** — the verdict is at least in the right
direction (unavailable is not the repo's fault), but the operator is sent to install a tool that is
already installed. **Would a test catch it? No.** Note the interaction with **D21**: `replace_text`
is never set by any caller today, so the `--replace-text` variant is currently latent and becomes
live the moment D21 is fixed.

**D39 — OPEN. `GitHubCli._exec` and `GiteaCli._exec` report `gh`/`curl` as uninstalled for a passed
deadline; `GitHubCli.available()` additionally answers a two-part question with a flat No.** Both
open with `if not result.started or result.exit_code == 127:` → `GhUnavailableError`
(`vcs/github.py:150–154`) / `GiteaUnavailableError` (`vcs/gitea.py:291–295`), naming the binary.
**Severity: low, and the containment is real** — `prwriter` catches `ForgeError` (which both derive
from) into `FailureClass.TRANSIENT_INFRA` with an explicit comment *"both re-queue, neither judges"*
(`workers/prwriter.py:259–260`), so **the retry verdict is right and only the message is wrong**.
That makes this the family's benign form and worth recording as the contrast case. **The second
half is not benign:** `available()` (`vcs/github.py:162–169`) wraps `gh auth status` in
`except GhError: return False`, and `GhUnavailableError` subclasses `GhError` — so a **network
timeout** (started, timed out, exit 124 → falls to the `check` branch → `GhError`) and a **passed
deadline** both return `False` from a method whose docstring asks *"Is `gh` installed AND
authenticated?"*. A flat No to a two-part question nobody measured either half of. **Would a test
catch it? No** — and `tests/test_vcs.py:804` shows the shape the fake supports: `started=False` is
constructible, `timed_out` is not (D45).

**D40 — OPEN. `symbolindex` and `interrogate` use `Path.is_dir()` as "did the clone run", and get
`PREFLIGHT, retryable=False` wrong for every `OSError`.** Both open `run` with
`if not await asyncio.to_thread(root.is_dir):` → `PREFLIGHT`, `retryable=False`,
*"worktree {root} does not exist; run the clone worker first"* (`workers/symbolindex.py:228–236`,
`workers/interrogate.py:261–269`). **`Path.is_dir()` swallows every `OSError` and returns `False`**:
EACCES on a parent, ELOOP, a stale NFS handle, ENAMETOOLONG, or a path that exists and is a
**file**. Each of those is reported as *"the clone never ran"*, terminally, under the class
`base.py:136` defines as *"identical on every attempt"* — and a permissions or mount fault is the
textbook retryable. **Severity: medium**, raised by its pairing with **D36**: the clone that
returned `status="ok"` with no worktree lands here, and the operator is told to run the worker that
already reported success. **Would a test catch it? No.** The distinguishing test is `root` as a
regular file, or a directory with mode `0o000`.

**D41 — OPEN. `clone`'s three silent zeros: a probe that never ran returns a measured-looking
answer, and one of them disarms the gate two lines below it.** All three read `result.ok` and
substitute a default: `_submodule_count` → `return 0` (`clone.py:491–495`), `_has_lfs` →
`result.ok and "filter=lfs" in ...` i.e. **`False`** (`clone.py:497–499`), `_largest_blob_bytes` →
`return 0` (`clone.py:501–518`). **The LFS one is load-bearing:** `has_lfs=False` makes
`if gate is None and has_lfs and payload.require_lfs_binary and not shutil.which("git-lfs")`
(`clone.py:436`) — the gate **on the line after the probe** — unconditionally false, so a repo that
does declare `filter=lfs` proceeds into a history rewrite with no `git-lfs` on PATH. And
`_largest_blob_bytes` returning `0` for a `cat-file` that never ran means
`if largest_blob > payload.max_blob_bytes` (`clone.py:440`) **never fires**, so `OversizeBlob` is
structurally unreportable on the timeout path. **Severity: medium, trending high** — all three
values are persisted to `repos.*` columns (SPEC §3.1 step 1's table) as **measurements**, and a
`0` that means *"we did not look"* is indistinguishable in the DB from a `0` that means
*"we looked and there are none"*. **Would a test catch it? No.** The honest form is `int | None` /
`bool | None` with the gate refusing on `None`.

### Tier 3 — honest verdict, dishonest message or mechanism

**D42 — OPEN. `result.ok` is used as the answer to four git *questions*, and the answers are
asserted downstream as facts about history.** `Git.ref_exists` → `return result.ok`
(`vcs/git.py:280–282`); `Git.apply_check` → `return result.ok` (`vcs/git.py:344–357`);
`Git.is_ancestor` → `return result.ok` (`vcs/git.py:476–482`); `Git.resolve` → `sha if result.ok
and sha else None` (`vcs/git.py:265–271`). **Exhaustion is live on every one of them**, not
theoretical: `Git.__init__` defaults `timeout_s=DEFAULT_TIMEOUT_S = 600.0` (`vcs/git.py:53, 204`)
and `exec` passes both `self.deadline` and `self.timeout_s` into the runner (`vcs/git.py:230–239`),
so any of the four can return the *No* answer for a reason that is not an answer. Downstream this
becomes assertion: `PatchApplyError(... "the patch does not apply to the current tree")` and
`RollbackAnchorError(... "the anchor is not an ancestor of the current tip", vcs/commits.py:335)` —
**a claim about a history relationship that was never measured**. `apply_check`'s own docstring
says *"a non-zero exit is the answer No"* (`vcs/git.py:346`); **exit 124 is not the answer No.**
**Severity: low, and the mitigation is real** — these surface as `UNKNOWN`/retryable rather than
terminal, so the fleet recovers; what is wrong is that the operator reads a confident sentence
about their patch or their anchor. **Would a test catch it? No** (D45). The honest form is a
tri-state or a raise, not a bool.

**D43 — OPEN. A `resolve()` that returns `None` because it timed out FORCE-MOVES the migration
branch, discarding committed work.** `cli.py:3761–3763`: `tip = await git.resolve(branch)` →
`if tip is None: await git.exec(["checkout", "-B", branch, "HEAD"])`. `-B` is
**create-or-reset-hard**: if `migrate/<repo>` exists and carries this run's commits, a `rev-parse`
that merely **failed to answer** resets it to `HEAD` and **the migration commits are reachable only
from the reflog**. The `else` arm is the careful one — it reads the `Fleet-Run-Id` trailer and
**refuses** a foreign run's branch (`cli.py:3765–3775`) — so the code's entire ownership check is
placed on the branch of a question that can silently answer wrong. **The same shape re-cuts the
phase rollback anchor:** `anchor = await git.resolve(anchor_ref)`; `if anchor is None:` →
`rev_parse(branch)` → `update_ref` (`cli.py:3779–3782`), i.e. a timed-out lookup **re-anchors
phase 2 at the current tip**, so a later rollback rewinds to the wrong place. **Severity: low
likelihood, catastrophic outcome** — this is the only entry in this set that destroys work rather
than misreporting it. **Would a test catch it? No.** The fix is one line: `resolve` must
distinguish *"absent"* from *"unanswered"* before anything acts on `None`.

**D44 — OPEN. `WorktreeManager.remove`'s comment names one cause for a condition with four, then
`rmtree`s on all of them.** `sandbox/worktree.py:158–166`: the comment asserts *"git refuses a path
it does not know as a worktree. If the directory is gone (or never existed) that is success; if it
is still there, git's refusal is real"* — and the code below it is
`if not result.ok and path.exists(): shutil.rmtree(path, ignore_errors=True)`. **`git's refusal is
real` is exactly what `not result.ok` fails to establish**: the `worktree remove --force` may have
timed out mid-operation, or never started at all under a passed deadline, in which case **git was
never consulted** and the harness deletes a registered worktree out from under it, leaving the
admin record for the `worktree prune` on the next line to reap. **Severity: low** — worktrees are
disposable by design and re-entry re-cuts them, which is why this is tier 3 and not tier 1 — but it
is the clearest specimen in the set of the family's **documentation** signature: **a comment that
names one cause for a condition that has four.** Grep for that phrasing; it is where these live.
**Would a test catch it? No.**

**D45 — OPEN, and it is a testability gap that explains the clustering. `tests/test_vcs.py`'s
`ScriptedRunner` has no `timed_out` parameter.** Its `__init__` accepts `stdout`, `exit_code` and
`started` (`tests/test_vcs.py:159–163`) and its `__call__` returns a `ProcResult` with
**`timed_out=False` hard-coded** (`tests/test_vcs.py:176–182`). So **the entire `vcs/` layer cannot
be regression-tested against this family with the fake it has** — D35, D38, D39, D42, D43 and D44
all live behind it. **Severity: medium, and it is the causal entry of the twelve:** a fake that
cannot express a state is a fake that guarantees the state is never asserted on, which is why
`started=False` **does** appear in a test (`tests/test_vcs.py:804`) and `timed_out=True` appears in
none. **Would a test catch it? The question is inverted here** — this is why the others have no
tests. The fix is one keyword argument, and it makes six of the eleven above cheaply testable.

**D46 — OPEN. The `TRANSIENT_INFRA` fix D34 shipped guarantees four byte-identical, deterministic
125s before it ever charges a rung, because the retry re-issues the same `docker run --name=`.**
Recorded here per review-36 I3: at `68a41ff` this measurement exists only in that commit's body,
not in this ledger, so it was invisible to anyone reading the register rather than `git log`.

`sandbox/container.py`'s `sandbox_config` (`container.py:103-109`, docstring) already names the
mechanism: `name` defaults to `sandbox_name(run_id, repo, attempt)`, which is **identical** across
every `RetryPolicy.decide` re-run of the same `TRANSIENT_INFRA` rung — `retry.py:203-217`'s
`RETRY_TRANSIENT` branch replays `(run_id, repo, attempt)` unchanged, only incrementing
`transient_retries`, and `docker_run_argv` (`container.py:127-133`) emits both `--rm` and
`--name={spec.name}` from that same spec. `--rm` is enforced by the daemon at container exit; a
daemon that died mid-build never runs it, so a container the dead daemon registered under that
name survives it. **MEASURED (68a41ff commit body): a surviving container makes a subsequent
`docker run --name=X` exit 125, and that 125 is now `TRANSIENT_INFRA` per D34's own fix** —
`buildverify.py`'s `_DOCKER_CANNOT_RUN` branch does not distinguish "daemon unreachable" from
"name already in use," both being an unreadable one-line docker stderr. So the harness's own
crash-recovery debris — the previous attempt's own container — manufactures a **permanent** 125
that `RETRY_TRANSIENT` cannot resolve by retrying, because every retry reissues the identical
name. Four free retries (`retry.py:132`, `DEFAULT_MAX_TRANSIENT_RETRIES = 4`) are consumed against
a condition retrying cannot fix, and only then does the ladder charge a rung for a name collision
it has never once told the operator about.

**Severity: high, and worse than D34's own motivating case.** D34 wasted rungs on a *transient*
condition that eventually clears on its own. This is *self-inflicted and deterministic*: once a
daemon dies mid-container, every subsequent retry of that rung is guaranteed to fail identically,
by construction, until the four free retries are exhausted and a real rung is charged — for a
condition the harness caused and could have named. **Would a test catch it? No** — no test in
`tests/test_workers_build.py` constructs a name-collision 125 distinct from a daemon-unreachable
125; see review-36 I2 for the adjacent fixture defect (a `DAEMON_GONE` stderr paired with an
exit code Docker 29.7.2 does not produce for that case) that would need correcting alongside it.

**Status — CLOSED, FIXED in `8464dc6`.** Corrected by review-38 C1: this entry and the fix it
disbelieved are **the same commit**. `git log --oneline -S"_invocation_name" -- src/fleet/workers/buildverify.py`
and `git log --oneline -S"D46 — OPEN" -- docs/INTEGRATION_HONESTY.md` both resolve to `8464dc6`
alone — there was never an "uncommitted, unverified" state to hedge; the paragraph above described
the tree as it stood before its own commit's code lane landed, not a separate later event.

`_invocation_name` (`buildverify.py:354-370`) gives every `docker run` this worker issues a fresh
`uuid.uuid4().hex[:8]`-suffixed name per call, replacing the bare `sandbox_name(run_id, repo,
attempt)` this entry's defect depended on. It is called at the probe (`:777`, `-cc-probe` suffix)
and the build step (`:1088`). Pinned by
`tests/test_workers_build.py:931-965`
(`test_a_transient_retry_of_the_same_rung_never_reuses_a_container_name`), which drives two `run()`
calls at the identical `(run_id, repo, attempt)` — exactly what `RetryPolicy`'s free
`RETRY_TRANSIENT` re-issues — and asserts the emitted `--name=` differs between them while still
sharing the deterministic prefix `on_cancel` sweeps by.

Two further premises this entry rested on are also superseded, in the same commit:

1. **The central mechanism no longer holds.** `buildverify.py:438-442`'s inline comment (citing
   the same `68a41ff` commit-body measurement this entry cites) states an unreachable docker
   daemon exits **1, not 125** — so the self-inflicted, permanent-125 scenario this entry built on
   cannot occur. The residual `_DOCKER_CANNOT_RUN` branch covers a residual or externally-caused
   collision only, not the daemon-restart case.
2. **"Would a test catch it? No"** no longer holds — see the test cited above, added by this same
   commit.
3. The `DAEMON_GONE` → `CONTAINER_NAME_CONFLICT` fixture correction this entry called an adjacent,
   still-needed fix is also in this commit: `tests/test_workers_build.py:1105-1129`, with the
   rename and rationale recorded at `:1275-1279`.

**Structural cause, recorded so the next round does not repeat it.** This entry and its own fix
were written by different workers in the same round against different tree states — the docs lane
against the pre-fix tree, the code lane against the post-fix one — then both landed in the same
commit without being reconciled against each other first. `PROGRESS.md §36`'s "What is still NOT
proven" list and "Next subagent task" carried the identical mismatch (see §36, corrected
2026-08-17). The fix is not only to flip this status: a docs lane must either be written against
the tree state that will actually be committed, or be re-verified immediately before `git commit`
— a "claimed, not verified" hedge is only honest while it remains true, and lands false the moment
it is committed alongside the fix it doubts.

- **`cli.py`'s two timeout printers are correct.** `cli.py:6369` and `cli.py:6626` both render
  `f"{' (timed out)' if result.timed_out else ''}"` into the failure text, so the operator is told
  which of the four causes occurred. **This is the honest form of D38/D39's message.**
- **`bazel/query.py` is correct.** `query.py:73` reports `f"exited {result.exit_code}
  (timed_out={result.timed_out})"` — it prints the evidence and diagnoses nothing.
- **`INFRA_EXIT_CODES` and `UNREPEATABLE_EXIT_CODES` are individually justified** against the
  vendored binary, and their docstrings (`buildverify.py:206–214`) give the reasoning per row.
  **D34 is the absence of a 125 row, not a wrong row** — nothing in either set should be removed,
  and an agent handed D34 must not "rebalance" the tables.

### Status, and what this section costs the document's thesis

**In flight:** the **clone trio (D35, D36, D41)** is reported as already being repaired by another
lane this round. That is recorded here as **claimed, not verified**, and the verification itself
ran into §33's third multi-agent hazard live: **`workers/clone.py` shifted by ~41 lines between two
reads inside this one sweep**, so every line number above was re-taken at the end and the four
cited bodies (`_unshallow`, `_preflight`'s `EmptyRepo` return, and the three silent zeros) were
**unchanged in substance at that instant**. Another agent is editing the file; the defects were
still present in it when last read. **The remaining nine are untouched.** Anyone closing D35/D36/D41
should re-read rather than trust these offsets.

**The thesis takes a real amendment.** This document has argued since D1 that **only running things
finds defects**, and D20–D33 already qualified it. **D34–D45 qualify it further and in a specific
direction:** reading found twelve instances **because a previous defect (D29) had already exposed
the mechanism**, and the mechanism is greppable. The generalisable claim is not *"reading works"* —
D1–D19 remain the counter-evidence — but that **once a defect's mechanical cause is written down,
the same cause can be swept for at near-zero cost, and it will be there more than once.** The
count for this family is now **thirteen** (D29 plus these twelve), across `workers/`, `vcs/`,
`sandbox/`, `rewrite/` and `cli.py`, in code written by different rounds. **None of the thirteen
would have been found by running the suite harder, and all of them have passing tests today.**

**D47 — OPEN. `build_diagnosis` is generated on every rung-2/3 build or test failure and consumed
by nothing.** `BuildverifyOutput.diagnosis` and `.diagnosis_failure_class`
(`buildverify.py:625-630`) are written exactly once, at `buildverify.py:1156-1157`, from an LLM
call (`_diagnose`, `buildverify.py:1124-1158`) gated only on `ctx.context_policy is not None` —
i.e. it fires on rungs 2 and 3 for **every** non-ok build/test step, a 125 included. **Correction
(review-38 I3):** the call site is `buildverify.py:967-969` (`if not result.ok and not
nothing_to_test:` guarding `usage = await self._diagnose(...)`), not `:873-874` as an earlier
version of this entry cited — that line is prose inside the C-toolchain probe's stderr string, not
a call site, and was already prose at the commit this entry was first written against, so this is
a correction to a citation wrong at authoring, not line drift. The "confirmed by direct read of
the call site" phrase attached to the wrong line is dropped; the underlying claim holds and is
re-verified at `:967-969`, with no failure-class branch in the guard. **No code anywhere reads
either field.** Re-verified independently of research-36 for this entry: `grep -rn "diagnosis"
src/` returns only the two `Field()` declarations, the two writes above, the LLM-plumbing trio
(`roles.py:57`, `calls.py:360`, `schemas.py:151-162`), and prose comments — zero reads, zero
`getattr`, zero string-key lookups. Traced through all three egress paths and all three are
closed:

1. **Parent handoff drops it.** `BuildWorker._handoff` (`cli.py:4983-4989`) and
   `VerifyWorker`'s equivalent (`cli.py:5480-5486`) each copy a fixed field list — `steps`,
   `build_ok`, `test_ok`, `tests_ran` — that does not include `diagnosis`.
2. **Checkpoints don't carry it.** The only `checkpoints.save` call
   (`orchestrator/runner.py:1005-1011`) persists a `PhaseCheckpoint(completed_units,
   remaining_units, attempt)`, not the worker output object.
3. **`migration_state.json` doesn't carry it.** No `diagnosis` field exists in
   `state/projection.py` or `models/state.py` (checked directly — zero hits).

The only thing consumed is the token cost: `response.usage` (`buildverify.py:1158`, corrected from
an earlier `:1050` citation — that line is inside `_bazel_argv`, unrelated) flows into
`WorkerResult.usage` and is billed by the budget machinery, so the harness pays WORKHORSE-tier
tokens on every rung-2/3 build failure across the fleet and keeps only a token counter for it.

**Why this is D47 and not a silent deletion (ADR-0068).** `docs/SPEC.md:1390-1392` and
`docs/SPEC.md:6266` both name `build_diagnosis` as a mandated LLM slot — it is not an
orphaned experiment nobody asked for, it is a shipped fraction of a spec'd feature whose
consuming half was never built. Removing the code would make the SPEC describe a slot that does
not exist; keeping it without recording the gap would let a reader believe the diagnosis is used
somewhere because the SPEC says the slot exists. Neither silence is acceptable, so: **kept, and
recorded here.**

**A secondary defect found while writing this entry.** `SPEC.md:1391-1392`'s own description of
the slot — "the model reads the Bazel error and proposes an edit, code applies it and re-runs the
build" — does not match `LlmBuildDiagnosis` (`schemas.py:151-162`: `failure_class`, `root_cause`,
`suspect_paths`, `suggested_action`, `confidence` — no diff/edit field of any kind) or
`buildverify.py`'s own docstrings (`:625-630`, `:1129-1134`), which call the output "advisory
only" because "the exit code is the verdict." The SPEC sentence instead describes
`transform_repair` (a different role, a different ladder — SPEC.md:185, :856, :6265). The SPEC
prose overclaims what this slot does; not corrected here (Rule 7 — surfaced, not silently
averaged into an unrelated edit; `docs/SPEC.md` is this worker's file, but the correction is
recorded as a known follow-up rather than bundled into this ledger entry). **Would a test catch
it? No** — `tests/test_workers_build.py` asserts only the negative case (`out.diagnosis == ""`
when nothing failed, at line 2357 in the tree this entry was authored against; `git status` shows
that file with uncommitted, unrelated edits above this point as of this correction, so re-derive
the exact line rather than trust either number); nothing asserts the field is ever populated, let
alone consumed.

**Severity: waste, not correctness.** Nothing downstream is wrong because of D47 — the exit code
remains the verdict, per the harness's own design (Rule 5: models judge nothing). The cost is
pure: WORKHORSE tokens spent fleet-wide, on every rung-2/3 failure, for advice that reaches no
reader. **Not fixed here**: wiring a consumer would be speculative (CLAUDE.md Rule 2 forbids
building a reader nobody asked for just to justify the writer), and deleting it would contradict
the SPEC without a corresponding SPEC edit, which is out of scope for this pass.

---

**D48 — OPEN. Config drift is gated on `fleet resume`, which is `_unavailable`; the six verbs that
actually re-enter an interrupted run go through `_phase_preflight`, which never reads
`runs.config_digests`. Edit a prompt template or a model id, re-run the phase verb, and prior work
is handed back — the checkpoint key `(run_id, repo_id, phase)` carries no configuration component,
so the correctly content-addressed LLM cache below it is never consulted.**

Found by a targeted audit (checkpoint 37) after a comparative read of `references/` surfaced the
same defect class in Visa's `vvaharness`, which keys resume checkpoints on `sha256(repo path)` only.
`fleet` does **not** have that literal bug — `run_id` is a UUID4 (`cli.py:1630`), not a path hash —
but arrives at the same outcome by a different route, which is why it is filed as its own entry
rather than as a note on someone else's.

**The guard exists and is good.** `settings.drifted_sections(baseline)` compares per-section
digests, not one opaque hash (`settings.py:1222`, `:1504`), and the refusal it raises is unusually
well-written: `--accept-drift <section>` takes them one at a time, each writing its own audited
`ConfigDrift` finding, and `--force-config-drift` warns in its own message that it "also accepts
every OTHER co-edited change, and is how a run is lost (§10)" (`cli.py:9819-9824`). Nothing about
the design is wrong.

**It is on a dead-end path.** Its sole call site is `_resume_impl`, and `resume` terminates at
`_unavailable("resume", "src/fleet/workers/clone.py (the phase drivers it re-enters)")`
(`cli.py:9792`) — the drift check runs, then the command refuses to do anything. The verbs that do
re-enter a run (`plan`, `build`, `verify`, `migrate-repos`, `transform`, `pr`) share
`_phase_preflight` (`cli.py:866-878`), whose three refusals are `_check_schema_version`,
`_resolve_run`, and `_refuse_concurrent_mirror_run`. **No drift read.** `grep -rn config_digests
src/` returns schema, migration, settings and two `cli.py` comment lines — no reader on a live path.

**The failure is silent and it costs the ladder.** `_resolve_run` picks the latest `run_id`;
`_load_checkpoint` finds the row under `(run_id, repo_id, phase)` (`state/checkpoints.py:74-88`);
`checkpoints.load` rejects on exactly three things — stored `model_name` ≠ loader class name,
envelope `schema_version` ≠ the module-level `SCHEMA_VERSION`, and Pydantic validation
(`checkpoints.py:141-172`). None of the three moves when a prompt, a model id, or a `budgets` limit
changes. Worse, ladder position is preserved rather than reset (`runner.py:631`,
`WorkerContext.attempt = phases.attempts + 1`), so a repo three rungs into an escalation under the
old model resumes at **rung 4 under the new one**, with the old rungs' spend charged and their
rejected approaches fed forward as `EVIDENCE_PLUS_REJECTED_APPROACHES` — evidence gathered against
a configuration that no longer exists.

**The two layers disagree, and the wrong one wins.** `llm/cache.py:10-12,138-152` keys on
`role | tier | backend | model_id | effort | context_policy | rejected_approach_digest |
prompt_sha256 | prompt_template_version | response_schema_sha256 | adapter_versions`, with
`prompt_sha256` over the fully rendered prompt (`llm/calls.py:281-289`). A prompt or model change is
correctly a cache **miss**. But the checkpoint sits above the cache: the unit is marked complete, the
model is never called, and the miss never happens. ADR-0069 §5 records "deterministic gate strictly
before LLM judgment" as a convergent finding across three references; this is the same ordering
error with the sign flipped — a stale deterministic gate suppressing a correct re-derivation.

**Two documentation defects found alongside it, both OPEN:**

1. `workers/base.py:237-244` claims `checkpoints.load` "compares the persisted
   `written_schema_version` against the loading class's `schema_version`". It does not — `load`
   compares the module-level `SCHEMA_VERSION` (the DB `PRAGMA user_version`, `models/state.py:16`);
   `written_schema_version` rides inside the payload as a plain `int` and validates against any
   value. `WorkerOutput.checkpoint_is_current` (`base.py:257-259`) has **no caller in `src/`** —
   `grep -rn checkpoint_is_current src/` returns 1 hit (its own definition), `tests/` returns 3.
   Bumping a `WorkerOutput.schema_version` therefore does not invalidate that output's checkpoints,
   contrary to what the docstring tells the next reader.
2. `_open_run` rewrites `runs.config_digests` unconditionally on every re-scan
   (`cli.py:1902-1906`) while `upsert_run` is `ON CONFLICT DO NOTHING` for `config_sha256`
   (`state/repository.py:1006-1009`). A re-scan under edited config silently **resets the drift
   baseline** and leaves `config_sha256` disagreeing with the digest map that `settings.py:1207-1210`
   states "can never disagree".

**Would a test catch it? No.** The drift machinery is tested against `_resume_impl`; nothing asserts
that a phase verb refuses, warns, or invalidates on a changed digest, because no phase verb reads
one. `fleet resume --reset-attempts` exists as a flag and is discarded unused (`cli.py:9786`,
`_ = (from_phase, repo, reset_attempts, …)`).

**Not fixed here.** This entry is a finding, not a change: `cli.py` is `src/` and outside the
documentation pass that produced checkpoint 37. The fix is not obviously "call the gate from
`_phase_preflight`" either — that would make every phase verb refuse on any drift, including drift
the operator already accepted on a prior verb, and the accept-once-per-section audit trail is
currently keyed to a command that does not run. **Whoever takes this must decide what a phase verb
should do on drift before writing any code**, and that decision belongs in an ADR.

**Working-tree caveat.** Verified against landed code at `8464dc6`. The uncommitted edits present
during this audit (`llm/cache.py`, `sandbox/container.py`, `workers/buildverify.py`, three test
files) touch no checkpoint, resume, or drift code — checked by diff before the claims above were
written, per §33's misattribution lesson.

---

**D49 — OPEN. The model-repair rewrite path lands patches that are neither size-checked nor
path-checked, and the one advertised size ceiling is unreachable code. `check_diff` has exactly one
call site — the *deterministic* branch (`workers/rewrite.py:332`) — and it omits the `max_bytes`
argument, so `transform.max_patch_bytes` is enforced nowhere while `docs/SPEC.md` asserts in three
places that it is. The LLM branch (`workers/rewrite.py:397-410`) calls `land_patches` with no diff
check and no parse probe at all. Compounding it, `_record` appends the deterministic **unit name**,
never the landed `edit.path`, so `_transform_criterion` probes a name the model may not have
touched.**

Found twice, independently, by two read-only subagents that never saw each other's output — one
auditing "deterministic gate strictly before LLM judgment" (Visa S5), one auditing "blast-radius caps
on writes" (Visa S10). Same line, two lenses. Every citation below was re-derived in the main session
at `32365cf` after that commit moved the tree.

**What is genuinely strong here, so the entry is not read as broader than it is.** The *schema* half
of this is exemplary and is not in question. `llm/schemas.py:16-18` states the rule — *"a model that
could assert it could assert its own patch parses. That is why `ProposedFileEdit` exists instead of
reusing `FilePatch`"* — and `workers/rewrite.py:528-531` enforces it by constructing every lifted
patch with `parse_probe_ok=False` hard-coded. The model **cannot** certify its own patch, and under
`extra="forbid"` it cannot acquire the field to try. That is stronger than the reference pattern this
was measured against, where the same property is one moved call from inverting. The defect is not
that the model can certify; it is that **nothing else does either, on that path.**

**The three legs, each verified.**

1. **The size ceiling is dead code.** `rewrite/apply.py:269` takes `max_bytes: int | None = None` and
   enforces it at `:277-278`, returning a message that *names the setting*: `"patch is larger than
   transform.max_patch_bytes (… bytes)"`. `settings.py:452` declares
   `max_patch_bytes: int = Field(default=1_048_576, gt=0)`. The only production call site
   (`workers/rewrite.py:332`) passes no `max_bytes`; the other two occurrences are internal to
   `apply.py` (`:295`, `:336`). **Setting, enforcement, and error message all exist and are not
   connected.**
2. **The SPEC asserts the ceiling works, in three places.** `SPEC.md:6059` (*"larger →
   PATCH_REJECTED, never held in RAM"*), `:6634` (*"Patches over `transform.max_patch_bytes` (1 MiB)
   are rejected as `PATCH_REJECTED` rather than held in memory or sent to a model"*), and `:7056`,
   where it is listed as a live mitigation for **"Memory bloat at 250 repos × 125k files"**. That
   third one is the reason severity is not merely cosmetic: a documented memory-bound mitigation is
   not running.
3. **The LLM branch skips the check entirely.** `workers/rewrite.py:397-410` calls `land_patches` on
   `repair.patches`; that path reaches `apply_and_commit` (`vcs/commits.py:232-264`), which performs
   an idempotency check and `git apply --check` and **neither `check_diff` nor a parse probe** — it
   is not `rewrite/apply.py:apply_patch`. Every commit is `--no-verify` (`vcs/git.py`), so hooks
   cannot backstop it either.

**The evidence-domain half, which is the subtler defect.** `_record`
(`workers/rewrite.py:535-541`) appends **`unit`** — the deterministic target name — to
`output.rewritten`, and never `edit.path`. `cli._transform_criterion` probes exactly
`output.rewritten`. A model returning a `ProposedFileEdit` for a *different* path inside the same
dest subtree therefore gets that file written and never probed, while the file that **is** probed may
be one it did not touch. The gate's domain is deterministic — which is correct — but it is **not
derived from the diff that actually landed**. Only `cli.py`'s changed-path clause catches
out-of-subtree escapes, and it runs after the wave loop has already committed: an audit, not a gate.

**Severity: correctness, and it is the model path specifically.** Nothing here affects the
deterministic bulk rewrite, which is bounded by rule coverage and is reviewable — a legitimate
migration rewrite may touch every file in a repo, so a global `max_files_touched` would be actively
wrong and is **not** what this entry asks for. The asymmetry is inverted from what it should be: the
reviewable path carries the containment check, the model-authored path carries none.

**Would a test catch it? No.** No test asserts that a model-supplied patch is size-checked, path-checked,
or probed; `transform.max_patch_bytes` has no test that exercises a patch exceeding it, because no
call site can produce that rejection. `models/tasks.py` declares a `files_changed` field that is never
written, and `cli.py` computes the staged set from a real `DiffStat` and collapses it to a bool — so
the data a cap would need is already produced and discarded.

**Not fixed here.** The mechanical part is roughly one line —
`check_diff(..., max_bytes=settings…max_patch_bytes)` on `repair.patches` at
`workers/rewrite.py:397` — and that single change closes legs 1 and 3 together. But the *rule* is a
decision this ledger should not make: **deterministic writes bounded by rule coverage, model-authored
writes bounded by size and path.** That belongs in an ADR, along with whether `_record` should track
landed paths (changing what `output.rewritten` means, which the §3.2 criterion reads) and whether
`SPEC.md`'s three claims are corrected or the code is brought up to them. Rule 7: surfaced, not
averaged into an unrelated edit.
