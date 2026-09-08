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
| **`util/proc.run` itself** | the base of every boundary above | **REAL** — `test_proc.py` drives real subprocesses: exit codes, timeouts and kill, tail truncation, `log_dir` streaming to disk, `started=False` for a missing binary. **Corrected 2026-08-18, verified against `f592327`, and this is the most serious kind of correction this file carries: the final clause is a claim that a test exists when it does not.** `_run_locked` (`util/proc.py:359-441`) wraps `asyncio.create_subprocess_exec` (`:396`) in a `try`/`finally` (`:394` … `:439-441`) with **no `except` clause anywhere in the function** — the ONLY `started=False` `_run_locked` can ever produce is the synthetic one at `:372-382`, returned BEFORE the subprocess is attempted, for a deadline that had already passed. A genuinely missing binary makes `create_subprocess_exec` raise `FileNotFoundError` straight out of `run()`, uncaught — no `ProcResult` is ever constructed, so `started=False` cannot describe this case at all, and no test tries to produce it: a grep for "missing", "nonexistent" or "filenotfound" (case-insensitive) across `tests/test_proc.py` is empty, and the suite's one real-subprocess `started=False` case, `test_call_past_the_deadline_never_spawns` (`:306-316`), is the deadline branch, not an absent binary. **This project's own record already said so, unnoticed:** `docs/PROGRESS.md:4240` — *"`util/proc.run` has no `FileNotFoundError` handler, so the exception propagates and no `ProcResult` is ever constructed"* — and `docs/DECISIONS.md:4744-4745` — *"if the binary were absent and `ensure_available()` somehow passed, `create_subprocess_exec` raises `FileNotFoundError`, not a `ProcResult` (measured)"* — both flatly contradicted this row, and neither correction crossed over to fix it. **Why this one outranks every debt-side correction landed today:** every other stale entry in this file overstated **debt** — a defect recorded that was not real, caught by re-checking. This overstates **coverage** — it tells a reader a boundary is tested when it is not, and a reader who trusts it skips writing exactly the guard that is missing here. That is the sharper failure mode this file's own thesis warns about. **Not a code defect — a deliberate design choice, confirmed by D38's fix:** `git grep -n "except FileNotFoundError" src/fleet/` shows `vcs/filter_repo.py:214`, `vcs/gitea.py:320`, `vcs/github.py:156,201`, `settings.py:925`, `cli.py:6447,6704` — six call sites, each independently catching `FileNotFoundError` around its OWN `runner(...)` call. D38's fix (`8464dc6`) is the clearest specimen: it replaced `filter_repo.relocate`'s three-predicate guess at a missing binary from `ProcResult` fields with a direct `except FileNotFoundError` wrapped around its own call (`vcs/filter_repo.py:212-218`, whose comment names the reason: a `ProcResult` "can never actually carry that evidence"). `proc.run` deliberately does not catch spawn errors — each call site guards its own need. The fix this row needed was to its own claim, not to `util/proc.py`. | Nothing outstanding in `src/`. **Corrected 2026-08-18: the doc gap was in this row, not the code** — see the Verdict cell. |

---

## Defects found by running the real tools

### D1–D4 — found by the first real `bazel` run, now FIXED

Each is recorded here with the assertion that now holds it, because a fixed defect with no test is
a defect waiting for its next commit.

**Header/status convention, for every `D<n>` entry below:** the status word in an entry's `**D<n> —
…**` header is that entry's *current* status, so when a `**Status — …**` block or a later correction
inside the entry changes it, the header must be rewritten to match in the same pass — the original
analysis text stays verbatim underneath, per this file's in-place correction convention.

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

**D19 — NARROWED AND ACCEPTED. The versions Bazel fetches for Go are not the versions the harness
pinned, and the harness's `go.sum` is not what verified them.** Unlike every other entry in this
section, D19 is a **correctness gap rather than a missing test** — no assertion closes it; a code
change does. Verbatim from the build that this round's headline rests on:

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
tests.** `RewriteWorker.pipeline_for` (`workers/rewrite.py:370`) constructs its `RewritePipeline`
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

**D21 — CLOSED, FIXED in `32365cf` (2026-08-17, one day after this section was written). The
`--replace-text` secret scrub is now wired end to end.** This entry read **OPEN, SECURITY** as
written on 2026-08-16 (§33 below); re-verified this session and that is no longer the state of the
tree. `git grep -n replace_text -- src/fleet/cli.py` now shows `cli.py:7252` passing
`replace_text=resolve_replace_text(settings.root, settings.config.redaction.history_scrub_file)`
into the `RelocationSpec` the render site reads — the caller this entry said did not exist.
`config/` is no longer absent either: `config/rules/secrets.txt` exists in the tree today, so
`settings.history_scrub_file`'s default now names a real path. `tests/test_build_e2e.py:1267`
asserts `"--replace-text" in call.argv`, closing the "would a test catch it" gap this entry
originally answered "No" to. Nothing here retracts the original finding — the wiring genuinely was
missing when written; it stopped being missing the next day and the record was never updated to
say so.

**D22 — CLOSED, FIXED in `32365cf` (2026-08-17, one day after this section was written). The
Gitea credential file's mode is now enforced.** This entry read **OPEN, SECURITY** as written on
2026-08-16; re-verified this session and that is no longer the state of the tree.
`src/fleet/vcs/gitea.py:106-129` now defines `_require_private_credential_file`, called from
`build_forge` at `:269`, which reads `path.stat().st_mode` and raises `GiteaError` when
`mode & 0o077` — the exact `st_mode & 0o077` refusal this entry said was missing. The "zero
occurrences in `src/`" measurement no longer holds: `grep -rn "$t" src/` (`$t` in `chmod`,
`st_mode`, `0o600`, `0o077`) returns **2, 3, 1, 1** across `src/` — `chmod` and `0o600` occur only
in `gitea.py:110`'s own docstring (which now narrates its history: *"had zero occurrences anywhere
in `src/` before this"*), `0o077` only at the enforcement line `gitea.py:124`, and `st_mode` twice
in `gitea.py` (`:110`, `:121`) plus once more in `workers/interrogate.py:172` — an unrelated
directory check (`stat.S_ISDIR(root.stat().st_mode)`) that predates this fix and was never part of
D22's claim either way. No test asserts the credential-mode refusal directly, so "would a test
catch it" is unresolved either way; that is a narrower, separate gap from "is it enforced," which
is now yes.

**D23 — FIXED, LANDED (round VI task 31, `d4cfc3e`/merge `31357cd`). Retargeted edges are never persisted; both readers always see NULL.**

> ***Forward reference (2026-09-02, round HH final review) — read before acting on this entry.***
> D97 (below) found `insert_edges` has exactly ONE production call site anywhere in `src/`
> (`_persist_scan_edges`, scan-time, before any contract can be hoisted) — so for
> `CONTRACT_IMPL`/`CONTRACT_CONSUME` specifically, **no row is ever written at all**, not merely a
> row missing this column. Fixing the fifteen-column list below would not, by itself, make a
> round-trip test for those two kinds pass — `_sequence_impl` would still need a real write path
> added first (D97's own "Not yet built"). D23's own retargeted-edge case may still be reachable
> through `_persist_scan_edges`'s call site (not re-verified here); do not assume fixing D23's
> column closes D97, or that D97's write path alone closes D23 — read both.

> ***Second forward reference (2026-09-02, round II task 2 review) — D97 is now fixed, and this
> entry's own scope now demonstrably extends to the new call site.*** `insert_edges` no longer has
> exactly one call site — round II task 2 added `_persist_contract_edges`, called from
> `_sequence_impl` after a contract hoist. Task review found this new call site hits D23's exact
> defect: a real source-scanned `INTERNAL_IMPORT` edge gets *retargeted* onto a hoisted contract
> (kind rewritten to `CONTRACT_CONSUME`, confidence/evidence_line preserved from the original) via
> pre-existing, unchanged `graph/cycles.py` logic, and the resulting row's
> `retargeted_from_repo_id` is silently dropped exactly as this entry describes — confirmed live at
> `acme-billing` in the `cycle_fleet` fixture (a second `CONTRACT_CONSUME` row at 0.8 confidence,
> alongside a fresh 0.85 one, both losing their retarget provenance). D23 is not closed by this —
> it is now reachable through TWO call sites instead of one, both losing the same column.

`state/repository.insert_edges` (`repository.py:2368`) enumerates **fifteen** columns and
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

**FIXED, 2026-09-03 (round VI task 31, commit `d4cfc3e`, merge `31357cd`).** The brief this task
was dispatched against assumed `EdgeRow` already carried `retargeted_from_repo_id` — investigation
found that was false: that field belongs to `DependencyEdge` (`models/graph.py:141`), a
structurally distinct in-memory inference class; `EdgeRow` (`state/repository.py:359-381`) had no such
field at all. The real fix touched three things, not one: (1) added the field to `EdgeRow`
itself, (2) added it to `insert_edges`'s SQL column list and params (now sixteen columns, not
fifteen), (3) updated both production call sites (`_persist_scan_edges`, `_persist_contract_edges`
in `cli.py`) to pass the already-computed `DependencyEdge.retargeted_from_repo_id` value through.
Independently re-verified by task-scoped review, including the `EdgeRow`/`DependencyEdge`
distinction itself (confirmed not a re-export or a confused correction) and column-ordering
consistency (16 columns / 16 placeholders / 16 params, matching across the SQL, the params tuple,
and both call sites). Mutation-proven twice — once by the implementer, independently reproduced by
the reviewer with import-isolation pinned: reverting the column-list fix makes the new round-trip
test read back `None` again; a companion non-retargeted-edge test stays green under the same
mutation, proving the fix doesn't write a bogus value into every edge.

The `ON CONFLICT ... DO UPDATE SET` clause deliberately still excludes this column — a disclosed
judgment call, not an oversight — citing `docs/SPEC.md` §6's normative idempotency table text
("DO UPDATE on confidence fields") verbatim. Review found an independently stronger justification
the implementer didn't cite: §12 criterion 23's own literal text requires "every
`edges.retargeted_from_repo_id` is unchanged" across an idempotent re-run, which excluding the
column from the update clause guarantees by construction. This is a real, disclosed prerequisite
for §12.23 (which named D23 as its specific blocker) and for §12.30 (whose own done bar silently
depended on this same gap) — neither criterion's closure is claimed by this fix alone; both need
their own follow-up verification against their full done bars.

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
it. All are recorded as OPEN below, in the order the §33 *Next* list takes them, because none was
fixed **when this section was written (2026-08-16)**. **Two no longer are: D26 and D27 were both
closed the next day at `8464dc6` (checkpoint 36, 2026-08-17 02:28), re-verified this session and
left corrected in place below rather than deleted** — the entries record what a reviewer found and
when; the fix is noted where it landed. Three of them (**D26**, **D27**, **D28**) are in the
publish path this round touched, which is the argument for reviewing a change and not only testing
it.

**D26 — CLOSED, FIXED in `8464dc6` (2026-08-17, the day after this entry was written). `_publish`'s
idempotence guard asked a question the tree could not answer, and a transient failure became
`REQUIRES_HUMAN_INTERVENTION`.** The guard is now pathspec-scoped rather than whole-worktree;
`tests/test_cli.py:3168`,
`test_publish_is_not_blocked_by_worktree_droppings_outside_the_pathspec` (docstring: *"D26:
`_publish`'s idempotence guard used to ask `is_dirty()` … rather than the pathspec it had just
staged"*), drives exactly the re-entry scenario below and asserts both
`result is None` and `output.already_published is True`. The "zero occurrences in `tests/`"
measurement below no longer holds — `already_published` now has one, in that assertion — which is
itself the coverage this entry originally said was missing. What follows is the original sequence,
unchanged, for the historical record:
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

**D27 — CLOSED, FIXED in `8464dc6` (2026-08-17, the day after this entry was written).
`_publish_module_lock` used to compare against the FILE, not the BRANCH, and its docstring named
that as a feature.** `src/fleet/cli.py:5407-5421`'s current docstring narrates the fix in the
function's own name for it — *"Idempotent by blob SHA against what is actually ON
`integration_branch` (D27), never against the worktree file alone"* — and the code at
`:5474-5478` computes `local_sha = await integration.hash_object(...)` against
`branch_sha = await integration.blob_at(payload.integration_branch, ...)`, returning early only
when they agree. `tests/test_cli.py:3229`,
`test_publish_module_lock_survives_a_crash_between_materialize_and_commit`, drives the exact scenario below (materialize, skip the
commit, re-enter) and asserts `git show <ref>:MODULE.bazel.lock` resolves — the test this entry
said would catch it. What follows is the original sequence, unchanged, for the historical record:
the order was registry check → read `MODULE.bazel.lock` **off the
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

**D29 — CLOSED, FIXED in `68a41ff`. `_c_toolchain_gate` misattributes timeouts and never-started
probes as "no C compiler", non-retryably — the THIRD instance of this bug in one gate.** The gate
branches exactly twice: `if result.ok: return None`, then `if result.exit_code ==
_DOCKER_CANNOT_RUN` (125), then an **unconditional fallthrough** to the refusal that begins *"no C
compiler in the sandbox image"* (`buildverify.py:700–722`). Everything else lands there: a probe
that **timed out** (`result.timed_out`, whose exit code is not 125), and a `docker` binary that
**never started at all** (`ProcResult.started is False`). Neither consulted the image. Both are
reported as a verdict about its contents, and both are **`retryable=False`**, so a **transient**
condition — a loaded daemon, a slow pull-less start — **terminates the repo**. **Severity: high**,
and the count is the point: §29 recorded this gate's first implementation probing *"the wrong
predicate in both directions"*, §31 recorded the 125 misattribution, and **this is the third**.
The 125 arm's own comment states the correct principle — *"the probe never executed inside it …
says nothing about whether the image has a C compiler"* — and the fallthrough violates it for two
more cases. **Would a test catch it? Yes:** feed the gate a timed-out `ProcResult` and a
`started=False` one, and assert neither refusal mentions a compiler.

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

> **Editorial correction (2026-08-21), lane W2 — the reaper IS invoked now; the leak above is
> unaffected. The entry stands as written; only this premise has moved.** `e915b93` landed §11.5
> step 2's orphan sweep, and `cli._reap_orphan_containers` calls `ContainerSandbox.reap()` on every
> `fleet resume`. Re-measured this session: `grep -rn '\.reap(' src/fleet/ | grep -v test` returns
> **three** lines, not zero: two of them are call sites (`cli._reap_orphan_worktrees`'s
> `manager.reap` and `cli._reap_orphan_containers`'s `sandbox.reap`) and the third is a docstring
> mention of `` `ContainerSandbox.reap()` `` in `sandbox/container.py`. Cited by symbol because
> `cli.py` is another lane's file and its line numbers moved twice while this was being written.
> (The predicate is a text grep, so it counts the mention; the claim that matters — `reap()` has
> production callers, so "zero" is false — needs only the two.) **What this does and does not change:** D32's
> defect is unchanged and still OPEN — the containerising path still bypasses `ContainerSandbox.run`
> and still leaks on every timeout. What changes is the "would a test catch it?" reasoning above,
> which rested on the reaper's *absence*: the leaked container is now swept by the next `fleet
> resume`, so the residue is bounded rather than permanent, and the sweep is reachable code a test
> can drive. D53 records the separate defect that this backstop reports a failed `docker rm` as
> reaped, so "swept" is not yet "gone".

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

**D34 — CLOSED, FIXED in `44d5550`. `classify_build_failure` has no `125` branch, and the build
steps ARE `docker run`.** `BuildverifyWorker._argv` (`buildverify.py:889–920`) returns
`tuple(docker_run_argv(spec))` whenever `payload.image is not None`, so the process the classifier
judges is **the docker client**, and a real build or test can exit **125**: unreachable daemon,
image deleted mid-run, malformed `--memory`/`--cpus`. **125 is in neither `INFRA_EXIT_CODES` (`{8,
9, 36} | OOM_EXIT_CODES`, `buildverify.py:206`) nor `UNREPEATABLE_EXIT_CODES` (`{2, 127}`,
`buildverify.py:212`)**, so it falls through every branch to the terminal line `return
(TEST_FAILURE if unit == TEST_UNIT else BUILD_ERROR), True` (`buildverify.py:388`). **A daemon
restart mid-wave is therefore reported as a broken `BUILD.bazel`**, spends all three ADR-0014
rungs — **two of them LLM-bearing, prompting a model to repair a file that is fine** — and lands
`REQUIRES_HUMAN_INTERVENTION` anyway. **Severity: high**, and the aggravating fact is that **the
same file already knows this**: `_DOCKER_CANNOT_RUN: Final = 125` is defined at
`buildverify.py:114`, **245 lines above the classifier**, and is consulted in exactly one place —
the `_c_toolchain_gate` probe (`buildverify.py:675`) — whose own comment states the principle the
classifier violates. **Would a test catch it? No.** `test_the_exit_code_table_is_the_policy`
(`tests/test_workers_build.py:1974–1993`) has rows for **1, 2, 3, 4, 8, 9, 36, 127, 137 and -9**
and **no 125 row**; the missing row is the defect. Adding `125 → TRANSIENT_INFRA, True` closes
both.

**Status — CLOSED, FIXED in `44d5550`.** Re-verified at `82654e8`: `classify_build_failure`
(`buildverify.py:412-471`) tests `result.exit_code == _DOCKER_CANNOT_RUN` at `:430` — before
either `INFRA_EXIT_CODES` or `UNREPEATABLE_EXIT_CODES` — and returns `FailureClass.TRANSIENT_INFRA,
True`: the missing row this entry named. Pinned by
`test_a_docker_run_that_exits_125_is_not_reported_as_a_broken_build_file`
(`tests/test_workers_build.py:1333`). `git show a1178f7:src/fleet/workers/buildverify.py` confirms
the entry's premise as originally written: at the initial commit `classify_build_failure` ran only
to its final `return`, with no 125 branch anywhere in it — the defect was real when filed and is
absent now.

**D35 — NEVER REPRODUCIBLE IN VISIBLE HISTORY. `clone._unshallow` turns every transient failure
into `REQUIRES_HUMAN_INTERVENTION`, on the single most transient operation the worker performs.**
`_unshallow` (`clone.py:478–489`) runs `git fetch --unshallow` **inside `ctx.limits.git_net`** —
it holds the network limiter, which is the harness's own statement that this is the call most
likely to be slow — and on any non-`ok` returns a **gate string**. `run` maps a gate to
`FailureClass.PREFLIGHT, retryable=False` (`clone.py:302–310`), which is terminal. **`PREFLIGHT`
is defined in `workers/base.py:136` as *"the repo's own shape; identical on every attempt"***, and
a network timeout is the opposite of that. **Severity: high** — one slow fetch permanently
sidelines a repo that would have cloned on the next attempt. **The class already knows how to do
this correctly:** `_error_for` reads `timed_out` off the exception and returns
`TIMEOUT`/`TRANSIENT_INFRA` with `retryable=True` (`clone.py:555–580`) — but it is only reachable
from the `except` path, and `_unshallow` calls `git.exec(..., check=False)`, which **returns
instead of raising**, so the correct handler 77 lines below is bypassed by construction. **Would a
test catch it? No** — see D45.

**Status — NEVER REPRODUCIBLE IN VISIBLE HISTORY.** `git diff a1178f7 HEAD -- src/fleet/workers/clone.py`
(re-verified at `82654e8`) shows `_unshallow` already had this entry's correct shape at the
repository's first commit. `git show a1178f7:src/fleet/workers/clone.py:537-566` — same body,
same line range, as `HEAD` — raises `_indeterminate(...)` for every non-`ok` fetch result whose
`_no_verdict` reads as unsettled, which `run()`'s handler routes through `_error_for` to retryable
`TIMEOUT`/`TRANSIENT_INFRA`; a non-retryable `PREFLIGHT` gate is returned only for the two settled
cases the function's own docstring names as legitimate (`unshallow` disabled by config, or a fetch
that succeeded and the mirror stayed shallow anyway). The only changes the diff shows to this
function across the repository's entire visible history are cosmetic — a shared `clock_failure`
helper factored out of `_error_for` later, `started` threaded alongside `timed_out` into the
raised exception — neither touches whether the function raises or returns a gate. **Caveat, not a
clean bill of health:** `a1178f7` is a squash of earlier unrecorded checkpoints, so this defect
may have existed pre-squash — nothing checkable in `git log` shows that it did. The verdict is
*never reproducible in visible history*, not *never existed*. See the note after D41 for how this
entry came to exist despite that.

**D36 — NEVER REPRODUCIBLE IN VISIBLE HISTORY. `clone._preflight` reports `EmptyRepo` for a
`rev-parse` that never ran, and returns `status="ok"` while doing it.** `head_sha = await
git.resolve(branch)` (`clone.py:409`); `Git.resolve` (`vcs/git.py:265–271`) is `rev-parse --verify
--quiet` returning `sha if result.ok and sha else None`, so it answers **`None` for a passed
deadline exactly as it does for a rev that does not exist**. `_preflight` then appends the finding
`"EmptyRepo"` and returns early (`clone.py:410–423`), `run` takes `cut_worktree = ... and
preflight.head_sha is not None` (`clone.py:313`) — **false**, so **no worktree is cut** — and
returns `WorkerResult(status="ok", ...)` with `worktree_path=None`. **Severity: high, and it is
the worst shape in this set:** the other eleven persist a wrong verdict as a **failure**, which an
operator reads; this one persists a wrong finding **as a success**. Downstream workers then fail
on a **missing worktree** (D40's gate) and blame the clone that reported green. **Would a test
catch it? No.** The catching test asserts that a timed-out `resolve` cannot produce `status="ok"`
at all.

**Status — NEVER REPRODUCIBLE IN VISIBLE HISTORY.** This entry's own premise does not match the
code even before checking history: `_preflight` never calls `Git.resolve` (`vcs/git.py:265-271`,
the ambiguous-`None` method this entry blames) at all. `git show a1178f7:src/fleet/workers/clone.py:412-421`
and `HEAD` both show `head_sha = await self._resolve_head(git, branch) if branch else None` — a
private method, not `git.resolve` — and `_resolve_head` (`a1178f7:509-533`, unchanged in shape at
`HEAD`) raises via `_indeterminate` whenever `_no_verdict` reports the `rev-parse` unsettled; only
a *settled* `rev-parse --verify --quiet` that genuinely answers "no such rev" reaches the
`EmptyRepo` finding. `_preflight` therefore cannot return `status="ok"` with a fabricated
`EmptyRepo` for a probe that never ran — the raise happens two calls up the stack before
`_preflight` gets a `head_sha` to act on. Never reproducible in visible history; same squash
caveat as D35.

**D37 — CLOSED, FIXED in `2af7dfb`. A timed-out parse probe is reported as *"no rewrite engine is
installed on this host"*, the run still exits SUCCESS, and the `break` abandons the probe for
every remaining file in the repo.** `AstGrepDriver._scan_for_error_nodes`
(`rewrite/astgrep.py:181–207`) is **correct locally**: it reads `if result.started and not
result.timed_out:` before any exit code, and raises `EngineUnavailableError` otherwise, printing
`timed_out=` in the message. **One layer up it is undone twice.** `cli._transform_criterion`
catches that exception into `unprobed` (`cli.py:4162–4163`) — a list rendered by
`cli.py:3090–3097` as a **non-blocking warning that adds no violation**, so `§3.2`'s fourth clause
silently goes unchecked and the command still exits SUCCESS — and the catch site is a **`break`**,
which exits the `for unit in rewritten` loop and therefore **skips the probe for every remaining
rewritten file in that repo**. **Severity: high.** One slow probe on file 1 of 40 turns a corrupt
rewrite in files 2–40 into a green transform. **Operator-facing text: *"no rewrite engine is
installed on this host"*** — for a host where `tools/bin/ast-grep` is vendored and ran. **The
docstring one layer down claims the tool failure *"raises rather than resolving to a parse result
nobody measured"*: true of the helper, false of the pipeline**, which is the same
doc-versus-caller split D25 records. **Would a test catch it? No** on both halves; the cheap one
is a timed-out probe asserted to produce a **violation**, not a warning, and a `continue` asserted
to reach file 2.

### Tier 2 — wrong operator target, right retry direction

**D38 — CLOSED, FIXED in `8464dc6`. `filter_repo.relocate` reports *"install git-filter-repo"* for
two conditions that are not that, one of them via a substring proxy.** `if not result.started or
result.exit_code == 127 or "No such file" in result.stderr_tail:` → `FilterRepoUnavailableError`
(`vcs/filter_repo.py:163–167`). **Three predicates, one message.** `not result.started` is the
passed-deadline case (D34's mechanism). And the third is a **substring match on stderr** — the
thing §3.3 says the harness never does — while `filter_repo_argv` (`vcs/filter_repo.py:123–143`)
emits `--path` for every `source_paths` entry and `--replace-text <file>` when set:
git-filter-repo prints `No such file or directory` for a **missing `--replace-text` file** or a
**bad `--path`**, neither of which means the binary is absent. **Severity: medium** — the verdict
is at least in the right direction (unavailable is not the repo's fault), but the operator is sent
to install a tool that is already installed. **Would a test catch it? No.** Note the interaction
with **D21**: `replace_text` is never set by any caller today, so the `--replace-text` variant is
currently latent and becomes live the moment D21 is fixed.

**D39 — CLOSED, FIXED in `8464dc6` and `854189a`. `GitHubCli._exec` and `GiteaCli._exec` report
`gh`/`curl` as uninstalled for a passed deadline; `GitHubCli.available()` additionally answers a
two-part question with a flat No.** Both open with `if not result.started or result.exit_code ==
127:` → `GhUnavailableError` (`vcs/github.py:150–154`) / `GiteaUnavailableError`
(`vcs/gitea.py:291–295`), naming the binary. **Severity: low, and the containment is real** —
`prwriter` catches `ForgeError` (which both derive from) into `FailureClass.TRANSIENT_INFRA` with
an explicit comment *"both re-queue, neither judges"* (`workers/prwriter.py:259–260`), so **the
retry verdict is right and only the message is wrong**. That makes this the family's benign form
and worth recording as the contrast case. **The second half is not benign:** `available()`
(`vcs/github.py:162–169`) wraps `gh auth status` in `except GhError: return False`, and
`GhUnavailableError` subclasses `GhError` — so a **network timeout** (started, timed out, exit 124
→ falls to the `check` branch → `GhError`) and a **passed deadline** both return `False` from a
method whose docstring asks *"Is `gh` installed AND authenticated?"*. A flat No to a two-part
question nobody measured either half of. **Would a test catch it? No** — and
`tests/test_vcs.py:804` shows the shape the fake supports: `started=False` is constructible,
`timed_out` is not (D45).

**Status — CLOSED. Both halves fixed.** The `_exec` half (the `gh`/`curl` "uninstalled" misreport
for a passed deadline) was fixed earlier at `8464dc6`. The second half — `available()`'s flat
`False` for both a genuinely missing `gh` and a probe that never settled — is fixed in `854189a`:
`available()` no longer routes through `_exec` (whose `check=True` path collapses "ran and said
no" and "never settled" into one `GhError` string with nothing left to branch on). It now calls
the runner directly and checks `util.proc.no_verdict` BEFORE reading the exit code, the same
pattern `d37f4ba`'s `Git._require_settled` uses for D42 (ADR-0067: raise on indeterminate, return
the settled answer unchanged). `FileNotFoundError` and a settled non-zero exit still return
`False` unchanged; only a probe that never started or was killed at its deadline now raises
`GhError`. Pinned by four new tests added in the same commit in `tests/test_vcs.py`: a genuinely
missing binary still returns `False`, a settled unauthenticated exit still returns `False`, and
both unsettled shapes (never-started, killed-at-deadline) raise.

**Scope caveat the implementer raised, recorded as-is because it changes the severity, not the
verdict:** `available()` has no production caller anywhere in `src/fleet` — `prwriter.py` and
`cli.py` call `create_pr`/`view`/`sync` directly and catch `ForgeError`, never `available()`. The
only callers are tests (this file, and `test_gitea.py`'s live-forge equivalent), using it purely
as a skip gate. This closes a real contract violation — the method's own docstring promises a
two-outcome answer it was not giving — with zero production blast radius today.

**D40 — CLOSED, FIXED in `f1aac12`. `symbolindex` and `interrogate` use `Path.is_dir()` as "did
the clone run", and get `PREFLIGHT, retryable=False` wrong for every `OSError`.** Both open `run`
with `if not await asyncio.to_thread(root.is_dir):` → `PREFLIGHT`, `retryable=False`, *"worktree
{root} does not exist; run the clone worker first"* (`workers/symbolindex.py:228–236`,
`workers/interrogate.py:261–269`). **`Path.is_dir()` swallows every `OSError` and returns
`False`**: EACCES on a parent, ELOOP, a stale NFS handle, ENAMETOOLONG, or a path that exists and
is a **file**. Each of those is reported as *"the clone never ran"*, terminally, under the class
`base.py:136` defines as *"identical on every attempt"* — and a permissions or mount fault is the
textbook retryable. **Severity: medium**, raised by its pairing with **D36**: the clone that
returned `status="ok"` with no worktree lands here, and the operator is told to run the worker
that already reported success. **Would a test catch it? No.** The distinguishing test is `root` as
a regular file, or a directory with mode `0o000`.

**Status — CLOSED, FIXED in `f1aac12`.** `workers/interrogate.py` gains `worktree_presence(root)`,
which stats the path directly instead of going through `Path.is_dir()`: `FileNotFoundError` and
`NotADirectoryError` are the settled negative (unchanged `PREFLIGHT`, `retryable=False` — this
bucket now also covers this entry's own "path that exists and is a file" case, since a `stat` on a
regular file succeeds and `stat.S_ISDIR` on the result is a determinate `False`, the same settled
answer as genuine absence); every other `OSError` is returned as the exception itself instead of
being swallowed. Both `interrogate.py` and `symbolindex.py`'s `run()` now branch on that: an
`OSError` reports `TRANSIENT_INFRA, retryable=True` (the class `base.clock_failure` uses for a
subprocess call that gathered no evidence, re-derived here without a `ProcResult` since there is
no subprocess) instead of terminal `PREFLIGHT`. Pinned by new cases in
`tests/test_workers_scan.py`. This also resolves the D36 pairing this entry names: a clone that
reported `status="ok"` with no worktree material now hits a worker that retries instead of
terminally blaming the wrong worker.

**D41 — NEVER REPRODUCIBLE IN VISIBLE HISTORY. `clone`'s three silent zeros: a probe that never
ran returns a measured-looking answer, and one of them disarms the gate two lines below it.** All
three read `result.ok` and substitute a default: `_submodule_count` → `return 0`
(`clone.py:491–495`), `_has_lfs` → `result.ok and "filter=lfs" in ...` i.e. **`False`**
(`clone.py:497–499`), `_largest_blob_bytes` → `return 0` (`clone.py:501–518`). **The LFS one is
load-bearing:** `has_lfs=False` makes `if gate is None and has_lfs and payload.require_lfs_binary
and not shutil.which("git-lfs")` (`clone.py:436`) — the gate **on the line after the probe** —
unconditionally false, so a repo that does declare `filter=lfs` proceeds into a history rewrite
with no `git-lfs` on PATH. And `_largest_blob_bytes` returning `0` for a `cat-file` that never ran
means `if largest_blob > payload.max_blob_bytes` (`clone.py:440`) **never fires**, so
`OversizeBlob` is structurally unreportable on the timeout path. **Severity: medium, trending
high** — all three values are persisted to `repos.*` columns (SPEC §3.1 step 1's table) as
**measurements**, and a `0` that means *"we did not look"* is indistinguishable in the DB from a
`0` that means *"we looked and there are none"*. **Would a test catch it? No.** The honest form is
`int | None` / `bool | None` with the gate refusing on `None`.

**Status — NEVER REPRODUCIBLE IN VISIBLE HISTORY.** All three probes already distinguished "never
ran" from "ran and settled" at the repository's first commit — the opposite of what this entry
describes. `git show a1178f7:src/fleet/workers/clone.py:568-630`, unchanged in shape at `HEAD`
(`82654e8`): `_submodule_count`, `_has_lfs`, and `_largest_blob_bytes` each call
`_no_verdict(result)` first and raise via `_indeterminate` when it reports the probe unsettled —
only a *settled*, genuinely-negative result (a `git show` that ran and exited non-zero because the
path is not in the tree) falls through to the `0`/`False` default. That is exactly the honest form
this entry itself prescribes ("the honest form is `int | None` / `bool | None` with the gate
refusing on `None`") — implemented as a raise instead of an `Optional` return, which produces the
identical operator-facing effect (a probe that could not run stops the wave rather than persisting
a fabricated measurement). Never reproducible in visible history; same squash caveat as D35/D36.

**How three phantom entries survived a full checkpoint.** `44d5550`'s own commit message, under
"Also in this checkpoint (earlier agents, separately verified)", states: *"clone worker
D35/D36/D41 -- transient failures no longer permanent, a rev-parse that never ran no longer means
EmptyRepo reported as success, and three probes no longer publish fabricated zeros. ScriptedRunner
gained timed_out/stderr, which is what made the family testable at all."* `git show 44d5550 --stat`
shows exactly four files touched — `docs/DECISIONS.md`, `docs/PROGRESS.md`,
`src/fleet/workers/buildverify.py`, `tests/test_workers_build.py` — **zero** touching
`src/fleet/workers/clone.py`, `tests/test_vcs.py`, or `tests/test_workers_scan.py`. The claimed
fix and the claimed test coverage both landed in **no commit that exists**: this is the same
claimed-not-landed hazard D46's own structural-cause paragraph already flags for a different pair
of lanes, except here the underlying code these three entries described was, per the diffs above,
never in the defective shape the ledger claimed to begin with — a commit message asserting
unverified work as "separately verified" is how three phantom entries acquired a ledger existence
at all. **The asymmetry is worth naming:** this pass found four entries stale in the *closed*
direction (D35, D36, D41, and D45 below) against one stale in the *open* direction (D34, already
fixed but still marked OPEN before this pass) — a ledger that accumulates phantom debt misleads a
future reader differently than one that misses a real defect, and costs exactly as much of this
document's own credibility either way.

### Tier 3 — honest verdict, dishonest message or mechanism

**D42 — CLOSED, FIXED in `d37f4ba`. `result.ok` is used as the answer to four git *questions*, and
the answers are asserted downstream as facts about history.** `Git.ref_exists` → `return
result.ok` (`vcs/git.py:280–282`); `Git.apply_check` → `return result.ok` (`vcs/git.py:344–357`);
`Git.is_ancestor` → `return result.ok` (`vcs/git.py:476–482`); `Git.resolve` → `sha if result.ok
and sha else None` (`vcs/git.py:265–271`). **Exhaustion is live on every one of them**, not
theoretical: `Git.__init__` defaults `timeout_s=DEFAULT_TIMEOUT_S = 600.0` (`vcs/git.py:53, 204`)
and `exec` passes both `self.deadline` and `self.timeout_s` into the runner
(`vcs/git.py:230–239`), so any of the four can return the *No* answer for a reason that is not an
answer. Downstream this becomes assertion: `PatchApplyError(... "the patch does not apply to the
current tree")` and `RollbackAnchorError(... "the anchor is not an ancestor of the current tip",
vcs/commits.py:335)` — **a claim about a history relationship that was never measured**.
`apply_check`'s own docstring says *"a non-zero exit is the answer No"* (`vcs/git.py:346`); **exit
124 is not the answer No.** **Severity: low, and the mitigation is real** — these surface as
`UNKNOWN`/retryable rather than terminal, so the fleet recovers; what is wrong is that the
operator reads a confident sentence about their patch or their anchor. **Would a test catch it?
No** (D45). The honest form is a tri-state or a raise, not a bool.

**D43 — CLOSED, FIXED in `d37f4ba` (inherited from D42's fix; not independently pinned). A
`resolve()` that returns `None` because it timed out FORCE-MOVES the migration branch, discarding
committed work.** `cli.py:3761–3763`: `tip = await git.resolve(branch)` → `if tip is None: await
git.exec(["checkout", "-B", branch, "HEAD"])`. `-B` is **create-or-reset-hard**: if
`migrate/<repo>` exists and carries this run's commits, a `rev-parse` that merely **failed to
answer** resets it to `HEAD` and **the migration commits are reachable only from the reflog**. The
`else` arm is the careful one — it reads the `Fleet-Run-Id` trailer and **refuses** a foreign
run's branch (`cli.py:3765–3775`) — so the code's entire ownership check is placed on the branch
of a question that can silently answer wrong. **The same shape re-cuts the phase rollback
anchor:** `anchor = await git.resolve(anchor_ref)`; `if anchor is None:` → `rev_parse(branch)` →
`update_ref` (`cli.py:3779–3782`), i.e. a timed-out lookup **re-anchors phase 2 at the current
tip**, so a later rollback rewinds to the wrong place. **Severity: low likelihood, catastrophic
outcome** — this is the only entry in this set that destroys work rather than misreporting it.
**Would a test catch it? No.** The fix is one line: `resolve` must distinguish *"absent"* from
*"unanswered"* before anything acts on `None`.

**D44 — CLOSED, FIXED in `f1aac12`. `WorktreeManager.remove`'s comment names one cause for a
condition with four, then `rmtree`s on all of them.** `sandbox/worktree.py:158–166`: the comment
asserts *"git refuses a path it does not know as a worktree. If the directory is gone (or never
existed) that is success; if it is still there, git's refusal is real"* — and the code below it is
`if not result.ok and path.exists(): shutil.rmtree(path, ignore_errors=True)`. **`git's refusal is
real` is exactly what `not result.ok` fails to establish**: the `worktree remove --force` may have
timed out mid-operation, or never started at all under a passed deadline, in which case **git was
never consulted** and the harness deletes a registered worktree out from under it, leaving the
admin record for the `worktree prune` on the next line to reap. **Severity: low** — worktrees are
disposable by design and re-entry re-cuts them, which is why this is tier 3 and not tier 1 — but
it is the clearest specimen in the set of the family's **documentation** signature: **a comment
that names one cause for a condition that has four.** Grep for that phrasing; it is where these
live. **Would a test catch it? No.**

**Status — CLOSED, FIXED in `f1aac12`.** This was the data-loss member of the family — an
unsettled probe authorising an `rmtree`. `WorktreeManager.remove` (`sandbox/worktree.py`) now
calls `util.proc.no_verdict` on the `worktree remove --force` result BEFORE reading `result.ok`
(the same ordering the rest of this family uses, and for the same reason: a call made past an
already-passed deadline reports both `started=False` and `timed_out=True` at once, and reading
`timed_out` first would misreport "never ran" as "ran too long"), and raises `WorktreeError`
instead of deleting when the call did not settle. A settled non-zero exit — a genuine git
refusal — still triggers the old `rmtree` behaviour unchanged. Pinned by three new cases added to
`tests/test_sandbox.py`: never-started and killed-at-deadline probes both raise without deleting
(with the started-before-timed_out message ordering asserted), and a settled genuine refusal still
deletes as before.

**A related classification choice, made in the same commit for the sibling D40 fix and worth
recording here because it sets the boundary this entry's own "four causes" enumeration depends
on:** `worktree_presence` (the D40 fix, `workers/interrogate.py`) treats *"path exists but is a
regular file"* as the SETTLED-NEGATIVE branch — the same bucket as genuine absence — because
`root.stat()` succeeds and `stat.S_ISDIR` on the result is a fully determinate `False`; there is no
filesystem-level ambiguity to raise on there, unlike an `OSError` that establishes nothing. D44's
own mechanism does not call `worktree_presence` (it reads `no_verdict` off a `ProcResult`, not a
`stat`), but both fixes are drawing the identical three-state line — settled-false and settled-true
are both "the filesystem answered," only a raised exception or an unsettled `ProcResult` means "it
did not." That is a judgement call about where the boundary sits, not an incidental implementation
detail, and it is the reason this family's fixes read as one shape applied twice rather than two
unrelated patches.

**D45 — NEVER REPRODUCIBLE IN VISIBLE HISTORY (mechanism); coverage gap now closed, and it is a
testability gap that explains the clustering. `tests/test_vcs.py`'s `ScriptedRunner` has no
`timed_out` parameter.** Its `__init__` accepts `stdout`, `exit_code` and `started`
(`tests/test_vcs.py:159–163`) and its `__call__` returns a `ProcResult` with **`timed_out=False`
hard-coded** (`tests/test_vcs.py:176–182`). So **the entire `vcs/` layer cannot be
regression-tested against this family with the fake it has** — D35, D38, D39, D42, D43 and D44 all
live behind it. **Severity: medium, and it is the causal entry of the twelve:** a fake that cannot
express a state is a fake that guarantees the state is never asserted on, which is why
`started=False` **does** appear in a test (`tests/test_vcs.py:804`) and `timed_out=True` appears
in none. **Would a test catch it? The question is inverted here** — this is why the others have no
tests. The fix is one keyword argument, and it makes six of the eleven above cheaply testable.

**Status — NEVER REPRODUCIBLE IN VISIBLE HISTORY (mechanism); coverage gap now closed.**
`git show a1178f7:tests/test_vcs.py` — the repository's first commit — already shows
`ScriptedRunner.__init__` accepting a `timed_out: bool = False` constructor argument (`:173`) and
`__call__` forwarding it verbatim into the returned `ProcResult` (`timed_out=self.timed_out`,
`:215`), never a hard-coded `False`. Re-confirmed unchanged in shape at `32365cf`. The entry's
mechanism claim does not reproduce at any point in visible history; the same squash caveat that
applies to D35/D36/D41 applies here. **What the entry's "would a test catch it? No" half named
was real, and separate from the mechanism claim:** at `32365cf`, no test in `tests/test_vcs.py`
constructed a `timed_out=True` `ScriptedRunner` against `resolve`, `ref_exists`, `apply_check`, or
`is_ancestor` — the fake could express the state, nothing exercised it. That gap is now closed by
`d37f4ba` ("fix(D42): Git probe methods raise on an unsettled ProcResult, not \"No\""), whose own
commit message states the correction plainly: *"tests/test_vcs.py already had
ScriptedRunner(timed_out=...) wired through to ProcResult (D45's fix had already landed), so this
only needed new coverage: parametrized tests across all four probes."* Added:
`test_d42_probe_never_started_raises_naming_it_never_started` and
`test_d42_probe_killed_at_deadline_raises_naming_the_kill` (`tests/test_vcs.py`), both
parametrized across the four probe methods this entry names.

**D46 — CLOSED, FIXED in `8464dc6`. The `TRANSIENT_INFRA` fix D34 shipped guarantees four
byte-identical, deterministic 125s before it ever charges a rung, because the retry re-issues the
same `docker run --name=`.** Recorded here per review-36 I3: at `68a41ff` this measurement exists
only in that commit's body, not in this ledger, so it was invisible to anyone reading the register
rather than `git log`.

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

**Resolved.** The "claimed, not verified" hedge above was accurate as written — the repair another
lane claimed (`44d5550`'s commit message) never landed in any commit, per the note after D41 — but
it pointed at the wrong defect. There was no repair to verify because `_unshallow`, `_preflight`,
`_resolve_head`, `_submodule_count`, `_has_lfs`, and `_largest_blob_bytes` already had the correct
raise-via-`_indeterminate` shape in the repository's first commit (`a1178f7`); see the
NEVER-REPRODUCIBLE corrections after D35, D36, and D41 above. **D45 is likewise
NEVER-REPRODUCIBLE** on its mechanism claim, with its coverage-gap half now separately closed —
see the correction after D45. That leaves **seven of the twelve untouched by this pass**: D37,
D38, D39, D40, D42, D43, D44. Whether those seven's line citations and code shapes still hold is
not re-verified here — this correction addresses only the four entries named above.

**The thesis takes a real amendment.** This document has argued since D1 that **only running things
finds defects**, and D20–D33 already qualified it. **D34–D45 qualify it further and in a specific
direction:** reading found twelve instances **because a previous defect (D29) had already exposed
the mechanism**, and the mechanism is greppable. The generalisable claim is not *"reading works"* —
D1–D19 remain the counter-evidence — but that **once a defect's mechanical cause is written down,
the same cause can be swept for at near-zero cost, and it will be there more than once.** The
count for this family is now **thirteen** (D29 plus these twelve), across `workers/`, `vcs/`,
`sandbox/`, `rewrite/` and `cli.py`, in code written by different rounds. **None of the thirteen
would have been found by running the suite harder, and all of them have passing tests today.**

**Correction — the "seven untouched" and "thirteen, all real" framing above are both superseded;
re-derived directly against source and `git log`, not transcribed from ADR-0073's own citation of
this paragraph.** ADR-0073 §1 (`docs/DECISIONS.md`) independently flagged this paragraph as
carrying a stale count and handed forward a corrected sentence: *"Of the thirteen four-state-collapse
entries (D29, D34–D45), six are fixed and tested, four were never reproducible in visible history,
one is half-fixed, and two remain open exactly as filed — the collapse pattern itself is real and
current (D40, D44), but 'thirteen, all real' overstates the surviving count by roughly half."* That
sentence was itself already out of date the moment it was handed over: D39 (the "half-fixed" one)
closed today in `854189a`, and D40/D44 (the "open exactly as filed" two) closed today in `f1aac12`
— see the Status paragraphs after each entry above. Re-measured fresh rather than propagating
either count:

- **Fixed and tested (9):** D29 (`68a41ff`, pinned by
  `test_a_probe_the_fleets_own_deadline_killed_is_not_reported_as_a_missing_compiler`), D34
  (`44d5550`), D37 (`2af7dfb`, ADR-0067 — the exact `break`→`continue` and timed-out-probe
  mechanism this entry named), D38 (`8464dc6` — `relocate` now catches `FileNotFoundError`
  directly instead of guessing from `not result.started`/exit 127/stderr substring), D39 (`8464dc6`
  for `_exec`, `854189a` for `available()`), D40 (`f1aac12`), D42 (`d37f4ba`), D44 (`f1aac12`).
  **D43 is the ninth, with a caveat the count above must not erase:** it is fixed but not
  independently pinned. `Git.resolve` (`vcs/git.py:311-326`) can no longer return `None` for a
  timed-out or never-started `rev-parse` — `_require_settled` raises first, and the method's own
  docstring names D42/D43 directly as the reason — so `cli.py`'s force-reset mechanism this entry
  describes cannot occur. The protection is inherited from `Git.resolve`'s own tests, not from a
  `cli.py`-level regression test built against this entry's own branch/anchor scenario; that test
  is being added separately.
- **Never reproducible in visible history (4):** D35, D36, D41, D45 — unchanged from the
  corrections above.
- **Open as filed (0), not two.** D40 and D44 were the two members ADR-0073's handed-forward
  sentence correctly called "open exactly as filed" and "real and current" — and they were, right
  up until `f1aac12` landed today. D39 closed alongside them in `854189a`. **Nothing in D29 or
  D34–D45 remains open as filed.**

9 + 4 + 0 = 13. This also retires the "seven of the twelve untouched by this pass" sentence above:
D37, D38, D39, D40, D42, D43, D44 were untouched *by that pass*, not permanently — six of the seven
are now independently fixed-and-tested and the seventh (D43) inherits its fix from D42's.

**The thesis was never the thing that was wrong, and stating the correct count only strengthens
it.** "Thirteen, all real" overstated how much of the family was still *open* — most of it wasn't,
by the time it was written. But the collapse pattern itself — the same "settled vs. unsettled"
confusion recurring across `workers/`, `vcs/`, `sandbox/`, `rewrite/` and `cli.py` — was not a
paper tiger: D40 and D44 demonstrated it *live*, in production code, right up until today's fix.
"The count was wrong" and "the pattern doesn't exist" are different claims; only the first one ever
held here, and even that has now fully resolved — there is nothing left in this family for a future
pass to find open as filed.

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
   (**`cli.py:1907-1910`** — corrected from `:1902-1906`. That earlier range was accurate at
   `8464dc6`, the commit the caveat below pins the whole entry to: `digests = json.dumps(...)`
   through the SQL string sat at those five lines there. This is line drift, not a citation wrong
   at authoring (contrast D47's `:873-874`) — `32365cf` and `e605f0d` both touched `cli.py` after
   `8464dc6` and before this correction, shifting `_open_run`'s body down five lines. Re-verified
   against a frozen `git show 2a72f9f:src/fleet/cli.py`, this correction's own HEAD, not the
   working tree, which had `cli.py` under a sixth agent's concurrent, uncommitted edit at the time
   of this correction) while `upsert_run` is `ON CONFLICT DO NOTHING` for `config_sha256`
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

**Post-hoc line-drift correction.** Item 2's `_open_run` citation was re-pinned to `2a72f9f`
(above) two commits after this entry's own `8464dc6` baseline moved `cli.py`'s line numbers under
it — see the inline correction for the commits responsible. Nothing else in this entry was
re-checked past `8464dc6`; a fuller re-derivation, if the tree keeps moving under `cli.py`, should
re-verify the whole entry against one fresh SHA rather than patch citations one at a time.

---

**D49 — FIXED, LANDED (`9a7148c`; the fix is split across three commits — `c5ab3b1`, `82654e8`,
`9a7148c` — closing three legs in sequence, `9a7148c` closes the last one; round W research
verified all three legs closed against `HEAD`, dated 2026-09-01, see the dated note at the end of
this entry). The model-repair rewrite path lands patches that are neither size-checked nor
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

**Correction (`c5ab3b1`) — two of the three legs above are closed in code; D49 stays OPEN because
the third leg is untouched and the first leg's fix cannot be reached from a live config.** Verified
directly against `git show c5ab3b1` and current `HEAD` (`2a72f9f`), not the working tree — `cli.py`
is under a sixth agent's concurrent, uncommitted edit as this correction is written.

1. **Leg 1 (size ceiling) — fixed in the worker, not reachable from config.** The deterministic
   call site now threads it: `check_diff(outcome.patch.diff, payload.dest_path,
   max_bytes=payload.max_patch_bytes)` at `workers/rewrite.py:340-344`. `RewriteInput` gained the
   field (`:209-217`, same `1_048_576` default as `TransformSection`). This is a real fix of what
   leg 1 described — but leg 1's own severity claim was that `transform.max_patch_bytes`, the
   *operator's* setting, never reached the check. It still doesn't. `TransformInput`
   (`cli.py:3200-3218`, committed) declares no `max_patch_bytes` field, and `_rewrite_input`
   (`cli.py`, the method building the `RewriteInput` passed to the worker) never sets one —
   confirmed directly: `grep -n "max_patch_bytes" src/fleet/cli.py` against `HEAD` returns **zero
   hits**. `RewriteInput.max_patch_bytes` therefore always takes its own field default; nothing an
   operator writes in `fleet.yaml`'s `transform:` block can ever change it.
2. **Leg 3 in the original numbered list (the LLM branch skipping `check_diff` entirely) — closed.**
   A new `_rejected_patch()` helper (`workers/rewrite.py:678-691`) runs `check_diff` over every
   entry in `repair.patches`, called at `:409-411` before the repair-path `land_patches` at `:430`.
   Traced every route that reaches that call: `repair` comes from the single `_repair()` call at
   `:385`, whose evidence originates either from `RULE_MISS` (no rule matched, `:333-338`) or from
   a caught `PatchApplyError`/`GitCommandError` on the deterministic branch's own `land_patches`
   (`:373-378`) — both funnel into the same `_repair()` call, and `_repair()` itself branches on
   `ctx.tier` between `LLM_REPAIR` (`propose_repair`) and `LLM_ESCALATION` (`escalate_repair`,
   `:491-500`) before returning. `_rejected_patch` runs unconditionally after `_repair()` returns,
   regardless of which evidence produced the call or which tier answered it — no route reaches
   `land_patches` at `:430` without passing through it first.
3. **The sharp consequence of leg 1 only being fixed in the worker.** The rejection message
   `_rejected_patch`/`check_diff` produce *names* the setting: `"patch is larger than
   transform.max_patch_bytes (… bytes)"` (`rewrite/apply.py:278`). Since no config value ever
   reaches `RewriteInput.max_patch_bytes`, that message always reports the field default
   (`1_048_576`), never whatever an operator actually put in `fleet.yaml`. An operator who raises
   `transform.max_patch_bytes` to admit large legitimate migration diffs keeps getting patches
   rejected, and the rejection cites the exact setting they already raised — worse than inert,
   because it reads as confirmation that the operator's edit took effect when it never reached the
   check at all.
4. **The irony.** D49's own fix has landed in **D50's shape**: an operator-settable key
   (`transform.max_patch_bytes`) that validates, has a real enforcement point downstream
   (`check_diff`), and still reaches no live config path — the same "written and consumed by
   nothing" defect class D50 catalogs 26 instances of, except this one is a *config* key whose
   *code*-side consumer is real and gated correctly, just never wired to it. D49 and D50 now
   describe the identical gap from opposite ends: D50 found the dead key; this correction found
   the live check it was supposed to feed.
5. **Leg 3 (the evidence-domain half) — untouched, remains open.** `_record`
   (`workers/rewrite.py:616-622`) still appends `unit` — the deterministic target name — to
   `output.rewritten`, never `edit.path` from the landed patch. Nothing in `c5ab3b1` touches
   `_record` or its call sites (`:381`, `:453`). The original entry's description of this leg is
   unchanged and still accurate.

**Second correction (`82654e8`) — point 1 and point 4 above are now stale; the config path
lands.** Re-verified against `git show 82654e8:src/fleet/cli.py`, not the working tree (`cli.py`
is modified in `git status` again as this correction is written). `TransformInput` gained
`max_patch_bytes` (`cli.py:3219-3224`); `_transform_payloads` reads
`settings.config.transform.max_patch_bytes` and sets it on the built `TransformInput`
(`cli.py:4025`, `:4057`); `TransformPipelineWorker._rewrite_input` copies it onto the
`RewriteInput` it builds. `grep -n "max_patch_bytes" src/fleet/cli.py` at `82654e8` — the same
command point 1 ran and got zero hits from — now returns five hits, including the two threading
sites above. Pinned by
`test_transform_max_patch_bytes_is_threaded_from_settings_to_rewrite_input`
(`tests/test_cli.py`), which drives the real `_transform_payloads` → `_rewrite_input` →
`check_diff` path with a configured 100-byte cap and asserts a 500-byte patch is rejected
**citing the configured value, not the 1 MiB field default** — closing exactly the "worse than
inert" failure mode point 3 above described. **Point 4's "irony" framing no longer holds**: the
config key is no longer dead, so this is not an instance of D50's defect class any more; D50's
own catalog was never built on this key in the first place (`transform.max_patch_bytes` was
never one of its 26/37 `KNOWN_INERT` entries) and needs no correction from this. **D49's status
is therefore: legs 1–3 (as originally numbered) all closed in code and pinned by tests; leg 5
above (`_record`, the evidence-domain half) is the only remaining open piece** — re-confirmed at
this same `82654e8`: `_record` (`workers/rewrite.py:616-622`) still appends `unit`, not
`edit.path`. Not itself re-titled OPEN/CLOSED here, since the entry already carries two prior
corrections layered on the original text per this file's convention; a future pass should read
all three before citing this entry's status.

*(Citation note, 2026-09-06, round VI task 55 — not a correction to the finding above.)* The
`TransformInput` (`cli.py:4025`) citation two paragraphs up is pinned in
`tests/test_integration_honesty_citations.py` as a known-unresolved usage-site citation (line
4025 sits outside `TransformInput`'s own class body, correctly, since it names a USAGE of the
class inside `_transform_payloads`, not the class definition). Task 55's own `cli.py` edits
(§12.31 Leg A, first wave) briefly moved `TransformInput`'s span to cover the cited line —
retired from the pin list once, then a second wave of edits in the SAME task (the controller-
review fixes: threading `min_consumers` through `break_cycles`'s call site, and the
`ContractNotShared` findings-writer filter) drifted it back to genuinely unresolved, and it was
re-pinned. This is normal churn for a citation into a large, actively-edited function, not a
correction to the finding it sits beside. `_transform_payloads` (round VI tasks 58, 65, 66, and
67's `cli.py` insertions having each moved it again — repointed fresh at merge time below) still
reads `settings.config.transform.max_patch_bytes` and sets it on the built `TransformInput` — the
two threading sites this paragraph's claim is actually
about are unmoved in substance, only in line number.

**Two deferred minors, both against the leg-1 fix specifically.** No accept-at-boundary test
exists: `check_diff` rejects with a strict `>` (`rewrite/apply.py:277-278`,
`len(diff.encode("utf-8")) > max_bytes`), so a patch at *exactly* `max_bytes` is accepted, and
nothing in `tests/test_workers_transform.py` pins that acceptance — both new tests only exercise
the rejection side. And the fix is verified only off-default: both tests construct their payload
via `rewrite_payload(anchor, [unit], max_patch_bytes=64)`, a value chosen to keep the fixture small,
not `RewriteInput`'s real default of `1_048_576`. Nothing in the tree asserts that default
(`workers/rewrite.py:212`) still equals `TransformSection.max_patch_bytes`'s default
(`settings.py:452`) — the two literals could drift apart with no test noticing, on top of neither
ever being reachable from a config file per point 1 above.

> **[Dated note, 2026-09-01, round W research — heading moved to FIXED, LANDED, this is not a
> new leg.]** Re-verified against `HEAD` independently of this entry's own prose: `9a7148c`
> ("fix(D49): `_record` appends landed `FilePatch.path`, not the unit name") is an ancestor of
> `HEAD`, `_record` (`workers/rewrite.py:614-634`) now appends `patch.path` at both call sites,
> and the fix is pinned by a genuinely discriminating multi-file test
> (`tests/test_workers_transform.py:918-957`,
> `test_a_multi_file_repair_records_every_landed_path_not_just_the_unit`). This closes the
> "evidence-domain half" the second correction above (`82654e8`) explicitly left as D49's one
> remaining open leg. Nothing else this entry describes as an open leg remains open — the two
> "deferred minors" immediately above (no boundary-value accept-at-exactly-`max_bytes` test; no
> assertion binding `RewriteInput.max_patch_bytes`'s default to `TransformSection.
> max_patch_bytes`'s default) are still true and still real, but the entry's own author already
> framed them as minors separate from D49's "legs," not reasons to keep the heading open — if a
> future round wants to close them, that is a small standalone TEST-ONLY task, not a reopening of
> D49. **See D52:** `9a7148c` — the very commit this note cites as closing D49's last leg — itself
> caused a Critical regression (silently reopened `check_diff`'s §3.2 subtree-escape gate for one
> commit), fixed two commits later in `2976a7e`. D52 is closed and does not reopen D49, but a
> reader following this note's citation to `9a7148c` should know the commit's own history before
> treating it as an unqualified good state.

---

**D50 — OPEN. The 26 keys `tests/test_config_keys_are_read.py`'s `KNOWN_INERT` allowlist marks
"ledger entry pending" are a config-surface instance of the same defect class as D47 — a thing
that is written (here: validated, digested, echoed back by `fleet config`) and consumed by
nothing — and twelve of the twenty-six share one root cause: `RunContext.llm_policy`
(`orchestrator/context.py:140`) is never assigned by any of the five `RunContext(` call sites in
`cli.py` (`:1805`, `:4059`, `:7481`, `:7553`, `:9100` — checked directly, none passes
`llm_policy=`), so `LadderModelClient` always falls back to `CallPolicy()`'s own defaults
(`llm/client.py:478`) no matter what `fleet.yaml`'s `llm:` block says.**

**Correction — count and citations, both re-derived; `2a72f9f`/`82654e8`.**

1. **The count was already stale when this entry was written and has grown since.**
   `tests/test_config_keys_are_read.py`'s `KNOWN_INERT` held 26 entries at `9644406` (this entry's
   own baseline commit) but **37** as of `2a72f9f` and unchanged since (`git diff 2a72f9f HEAD --
   tests/test_config_keys_are_read.py` is empty at `82654e8`) — counted by parsing the
   `KNOWN_INERT` frozenset literal directly, not by eye. `2a72f9f` ("strip prose, add
   qualified-match + UNVERIFIABLE tier") blanks every comment and docstring out of the scanned
   source before matching a key name against it, which revealed **11** additional entries the
   bare-text scan had been passing on prose alone or on a same-named-field collision:
   `budgets.max_host_rss_mb`, `llm.concurrency_overrides`, `llm.failover`,
   `llm.failover.enabled`, `llm.failover.max_targets_per_call`, `llm.max_schema_repairs`,
   `pr.merge_wait_timeout_s`, `redaction.entropy_min_bits`, `redaction.entropy_min_len`,
   `transform.ladder.context_policy`, `transform.ladder.role` (`26 + 11 = 37`). The same commit
   introduced two categories that did not exist at this entry's baseline: **`QUALIFIED_MATCH_KEYS`**
   (5 keys — a subset of `KNOWN_INERT`, not a separate tally; the immediate-parent-qualified name a
   plain bare-name scan cannot resolve, e.g. `llm.failover.enabled` vs. the unrelated
   `scan.contracts.enabled`) and **`UNVERIFIABLE`** (1 key, `transform.ladder.tier` — genuinely
   undecidable by either scan, because its qualifying parent name collides with
   `orchestrator/retry.py`'s unrelated `LadderState.tier` method; carries no `KNOWN_INERT` line and
   is not counted in the 37). `DECLARATIVE` — the true-positive category, keys genuinely read
   inside `settings.py` — is unchanged at 3. Of the 11 newly-revealed `KNOWN_INERT` entries, 4
   (`llm.failover`, `llm.failover.enabled`, `llm.failover.max_targets_per_call`,
   `llm.max_schema_repairs`) trace to the same `llm_policy` cause this entry names, bringing that
   cause's count to **16 of 37** (not 12 of 26); the remaining 7 are new instances of the same
   "written and consumed by nothing" defect class from unrelated causes the test file's own
   comments document (an unpassed `Limits.create(llm_overrides=...)` kwarg, an uncalled validator,
   two dead-rationale comments, two hardcoded module constants standing in for config, and the
   `transform.ladder` qualified-match pair, which is this entry's own Group 2).
2. **The `RunContext(` citations drifted twice over.** This entry cites `:1805, :4059, :7481,
   :7553, :9100`; a later reviewer measured `1806, 4074, 7496, 7568, 9115`. Both are stale.
   `cli.py` is under concurrent edit by other lanes in this pass (`git status` shows it modified as
   this correction is written), so re-measured against a frozen snapshot rather than the working
   tree: `git show 82654e8:src/fleet/cli.py | grep -n 'RunContext('` returns `1807, 4085, 7527,
   7599, 9146`. Neither this entry's numbers nor the reviewer's match that snapshot, and this
   correction's own numbers will not match whatever lands next — the file has moved at least three
   times since this entry was authored (`82654e8` itself is a `cli.py`-touching commit, from an
   unrelated D49 fix). **The substantive claim survives all three measurements:**
   `git show 82654e8:src/fleet/cli.py | grep -n 'llm_policy='` returns zero hits, confirming no
   call site passes `llm_policy=` regardless of which line numbers are current. A future citation
   of this entry should re-derive the five line numbers from a fresh `git show <sha>:src/fleet/cli.py`
   rather than trust any number printed here, including this one — the recurring lesson across this
   pass's corrections (see also D49's second correction and D51's citation correction) is that a
   bare line number into a file under active, concurrent repair is stale by the time it is read,
   however carefully it was measured; only a SHA-pinned citation stays checkable.

**Second correction — the four categories named explicitly; the count has NOT moved again at any
committed SHA, despite a same-day report that it had grown to 46.** Re-verified directly against
`tests/test_config_keys_are_read.py` as committed at `dee9886` (`git show
dee9886:tests/test_config_keys_are_read.py`, parsing the frozenset literals with `ast` rather than
counting by eye), not the working tree — `git status` at the time of this correction shows that
file modified, mid-edit by a lane still in progress.

1. **The four categories, named together with what each one asserts** — the opening line above
   names only `KNOWN_INERT`, and the first correction explains the other three scattered across
   several paragraphs; collecting them once, because the distinction is the point of this file
   existing at all:
   - **`KNOWN_INERT`** — verified dead: a config leaf whose name (or, for the subset below, its
     parent-qualified name) occurs nowhere in `src/fleet/` outside `settings.py`. The entry's
     headline defect class and the number in its own title.
   - **`QUALIFIED_MATCH_KEYS`** — not a fifth tally, a *subset of `KNOWN_INERT`*: the leaves whose
     bare field name collides with an unrelated same-spelled field, method, flag, or local
     elsewhere in the tree, resolved instead by matching `parent.leaf` (e.g. `failover.enabled`,
     not bare `enabled`). Every member is already counted inside `KNOWN_INERT`'s total; adding the
     two together would double-count.
   - **`UNVERIFIABLE`** — the opposite of `KNOWN_INERT`, not a second amnesty list: a leaf the scan
     cannot decide even with qualified matching, because the qualified text itself collides with
     unrelated real code (`transform.ladder.tier` vs. `LadderState.tier()`, a method on a
     different object — see the module docstring's limitation 2). Carries no `KNOWN_INERT` line
     and is not part of that count. Conflating `UNVERIFIABLE` with `KNOWN_INERT` would overstate
     exactly the way this document exists to prevent: "the scan could not decide" is not "the scan
     decided it was dead."
   - **`DECLARATIVE`** — the true-positive category: a leaf genuinely read, inside `settings.py`
     itself (invisible to the scan by the same exclusion that stops a key trivially matching its
     own declaration), by a named accessor or startup validator with a real caller elsewhere in
     the tree.
2. **At `dee9886`, parsed directly, not transcribed: `KNOWN_INERT`=37, `QUALIFIED_MATCH_KEYS`=5
   (subset), `UNVERIFIABLE`=1, `DECLARATIVE`=3, and `_config_keys()` walks 180 leaves total** — all
   five numbers unchanged from the prior correction's `82654e8` snapshot; `git diff 82654e8 dee9886
   -- tests/test_config_keys_are_read.py` is empty at the committed level.
3. **A same-day report of this entry's count (46 / 11 / 1 / 6) does not reproduce at any committed
   SHA — it is an uncommitted edit, not yet a citation.** `git status` at the time of this
   correction shows `tests/test_config_keys_are_read.py` modified in the working tree;
   `git diff HEAD -- tests/test_config_keys_are_read.py` shows `_strip_comments_and_docstrings`
   rewritten to blank every `ast.Constant` string literal, not only a docstring positioned as the
   first statement of a module/class/function body — exactly the mechanism the report described.
   Parsing that *working-tree* file directly (not `git show`) does reproduce the reported numbers
   exactly: `KNOWN_INERT`=46, `QUALIFIED_MATCH_KEYS`=11, `UNVERIFIABLE`=1, `DECLARATIVE`=6, so the
   report's arithmetic and mechanism are both accurate against what exists on disk right now. But
   per this entry's own repeated lesson two paragraphs up (and D49's/D51's citation corrections),
   an uncommitted number is not a citation: it can still change shape, be reverted, or fail review
   before it lands, and nothing here should assert as fact what `git show` cannot reproduce. **This
   correction records the committed count (37/5/1/3/180) only.** When the in-progress edit
   commits, D50 will need a third correction re-pinned to that landing SHA — flagged here so the
   next reader does not assume 37 is current indefinitely, the same trap that made this entry's
   second correction necessary in the first place.
4. **One fact from the pending edit is worth recording now, independently, because it does not
   depend on that edit landing.** Two keys the pending working-tree draft's own `DECLARATIVE`
   comments admit an "independent sweep" nearly mis-filed as `KNOWN_INERT` — `redaction.enabled`
   and `redaction.patterns` — are, right now, genuinely read, verified directly against committed
   `settings.py` (unmodified by any concurrent lane; `git status` confirms): `FleetSettings.load`
   (`settings.py:1105`) calls `_check_redaction_switch(config, environ, fleet_path)` at `:1149`,
   which refuses to load when `config.redaction.enabled` is false and `FLEET_ALLOW_RAW != "1"`
   (`:1314-1320`); the same `load` calls `_refuse_secret_material(path, raw,
   config.redaction.patterns)` at `:1147`, which refuses a source file matching a configured
   pattern (`:1299-1310`). Neither key has ever been named as inert anywhere in this ledger, at any
   committed revision (`git log --all -p -- tests/test_config_keys_are_read.py` has no hit for
   either name, and neither does `docs/INTEGRATION_HONESTY.md`) — so there is no existing ledger
   claim to correct here. But had the pending draft's near-miss landed unreviewed, this entry would
   have listed two live safety switches as dead code, the dangerous direction for this kind of
   error to be wrong in: an operator reading `KNOWN_INERT` as license to delete a "no-op" key would
   have disabled secret-redaction enforcement believing it inert. Recorded as a standing caveat for
   whichever lane lands that edit next, not as a correction to anything this entry currently
   claims.

**Third correction — the pending edit landed; committed at `6ad64e0`, re-derived independently,
not transcribed from its own commit message.** Parsed directly with `ast` against `git show
6ad64e0:tests/test_config_keys_are_read.py` (not the working tree, not counted by eye):
`KNOWN_INERT`=**46**, `QUALIFIED_MATCH_KEYS`=**11** (subset, same relationship to `KNOWN_INERT` as
before), `UNVERIFIABLE`=**1**, `DECLARATIVE`=**6**, and `_config_keys()` still walks **180** leaves
total — matching the second correction's own prediction exactly, and re-confirmed unchanged at
`HEAD` (`f1aac12`, which carries `6ad64e0`; `git diff 6ad64e0 HEAD --
tests/test_config_keys_are_read.py` is empty). `6ad64e0`'s own commit message names the mechanism:
`_strip_comments_and_docstrings` now blanks every `ast.Constant` string literal, not only a
docstring positioned as the first statement of a module/class/function body — closing the last gap
the second correction's own working-tree preview had already identified (a `Field(description=...)`
literal at `models/state.py` that happened to name `run.stale_after_s` was passing the scan on
prose alone). **The count grew because the scanner improved, not because the code decayed** — the
180-leaf total this document has cited at every count so far (26, then 37, now 46) has never moved;
only the scanner's ability to see past prose describing a key has. The revision is not
one-directional: of the newly-resolved keys, most moved INTO `KNOWN_INERT` (`run.stale_after_s`,
`scan.contracts.marker_scan_bytes`, `transform.anchoring` among them), but `concurrency.llm.cheap`
moved the other way, OUT of the suspect set and into `DECLARATIVE`, because `LlmConcurrency.for_tier`'s
`self.cheap` fallback genuinely reads it — confirming this is a re-derivation, not a ratchet that
only ever finds more dead keys.

**The two keys the prior correction warned would have been false positives did NOT land as
`KNOWN_INERT`.** `redaction.enabled` and `redaction.patterns` — the pair flagged above as a
near-miss that would have had this ledger declare two live safety switches dead — are, in the
committed `6ad64e0` frozenset (checked directly, not taken from the commit message's own claim),
members of `DECLARATIVE`: `"redaction.enabled" in KNOWN_INERT` and `"redaction.patterns" in
KNOWN_INERT` both evaluate `False`; both evaluate `True` against `DECLARATIVE`. `6ad64e0`'s own
message independently states the same finding ("Of nine keys an independent sweep reported inert,
TWO DID NOT REPRODUCE: redaction.enabled and redaction.patterns... They went to DECLARATIVE."),
which this correction treats as corroboration, having re-derived it rather than trusted it. The
near-miss is closed, not merely a standing caveat any longer. **This correction records the
committed count (46/11/1/6/180) as current as of `f1aac12`.**

**The asymmetry the test file's docstring names is real and this entry is its second half.**
`cli.py:3141-3147` refuses `--context-policy` at the flag layer and says exactly why: the value
"would be parsed and then ignored by every rung" because `workers/base.context_policy_for_attempt`
"reads no config" — the same reasoning covers `--no-anchoring-guard` (`:3148-3154`) and
`--stub-blocked` (`:3155-3160`). Nothing analogous exists for `config/fleet.yaml`. The three
missing-machinery defects those flags are refused for each have a config-key twin below (the
anchoring pair; the rate-limit/failover block traces to a different gap — `llm_policy`, not
`context_policy_for_attempt` — but lands in the identical place: a value that validates, is
echoed back, and reaches no rung). `fleet config` has no refusal path at all; it prints the
operator's number back at them regardless.

**Verified directly, not transcribed from the allowlist.** `llm_policy`: `grep -rn llm_policy
src/` returns exactly the two lines in the thesis above — the field declaration and its one use
inside `RunContext.__post_init__` — and no assignment anywhere. One timeout key:
`grep -rn 'build_timeout_s\|clone_timeout_s' src/fleet/ | grep -v settings.py` is empty; both
names (`settings.py:267-268`) exist nowhere else in the tree. One `DECLARATIVE` correction, for
contrast: `concurrency.llm.workhorse` (`settings.py:224`) *is* genuinely read, via
`LlmConcurrency.for_tier` (`settings.py:227-230`), which `orchestrator/budgets.py:980` calls to
size the per-tier semaphore — a real accessor with a real caller, which is exactly what every key
below is missing.

**Group 1 — `llm.rate_limit` (9 keys) and `llm.failover` (3 keys): the sharp one.** Because
`llm_policy` is never wired, `settings.llm.rate_limit.*` and `settings.llm.failover.*` cannot
reach `CallPolicy` by any path — there is no second assignment, no adapter, nothing translating
one into the other. `llm/client.py` never references `.failover` or `.rate_limit` as attributes
at all (`grep -n '\.failover\b\|\.rate_limit\b' src/fleet/llm/client.py` is empty); the word
`failover` appears throughout the module only in prose and event names. The confirmed behavior:
`llm/client.py:499` slices `route.targets[: self._policy.max_targets_per_call]` and the transient
retry is a fixed backoff — no token bucket, no `Retry-After` handling, no failure counter, no
cooldown, no circuit open. **This is the group that actually bites, and it bites at the worst
moment.** All twelve keys' defaults (`RateLimitEntry.rpm=0` i.e. unlimited, `honor_retry_after`
irrelevant with no limiter, `FailoverSection.open_after_failures=3`, `cooldown_s=120`,
`on_tier_exhausted="halt"`) describe behavior nothing implements, so a fresh `fleet.yaml` is not
wrong — there is no limiter to under- or over-configure. The failure surfaces exactly when an
operator, being throttled by a provider, edits `llm.rate_limit.targets.<target>.rpm` or
`llm.failover.cooldown_s` expecting backpressure or a circuit break, and gets neither: the run
keeps hammering the same target on the same fixed backoff it always used, `fleet config` confirms
the edit took, and nothing in the run's output says the knob was never connected.

**Group 2 — `transform.anchoring` (2 keys): latent unless the ladder loops.**
`transform.anchoring.max_reasks_per_rung` and `.on_exhausted` (`settings.py:427-428`) name the
same missing machinery `cli.py` cites refusing `--no-anchoring-guard`: `models/tasks.py:211`
declares `TransformTask.reasks` and nothing increments it, `rewrite/approach.py` does not exist,
so there is no counter for either leaf to act on. Latent under normal operation because the
ladder's own `transform.max_attempts` already bounds total attempts per unit; it bites once an
operator sets a *tighter* per-rung reask cap expecting the harness to stop repeating a rejected
approach early — instead a repair loop can spend the full ladder budget on one rung.

**Group 3 — `preflight.baseline_build` (1 key): not latent, always wrong.** Unlike the other
groups, this one does not wait for an operator to touch it. `grep -rn 'baseline_build\|BaselineBuild'
src/fleet/ | grep -v settings.py` returns one hit — a comment in `state/schema.sql:83` — and zero
code. §3.1 and §14.1 (per the `KNOWN_INERT` comment and `settings.py:287`'s own docstring) describe
a native build/test gate run before any transformation, gated on `enabled: bool = True` by default.
Default-true plus zero readers means the gate the SPEC says always runs, never runs, on every
config, not only a hand-edited one — the one entry in this ledger's 26 whose severity is
unconditional rather than deferred to an operator's edit.

**Group 4 — bare timeouts and ceilings (5 keys): confirmed inert, consequence not fully traced.**
`budgets.build_timeout_s`, `.clone_timeout_s` (`settings.py:267-268`), `graph.max_edges`
(`:411`), `run.reaper_interval_s` (`:215`) and `run.projection_hz` (`:217`) all have zero
occurrences outside `settings.py` — re-verified above for the first two. What is **not** claimed:
that a clone or build hangs forever without them. `buildverify.py` has its own hard-coded
`C_TOOLCHAIN_PROBE_TIMEOUT_S` for a narrower probe, and no periodic `sleep(reaper_interval_s)`-style
loop was found — `reaper` in `orchestrator/runner.py` names lease-reclaim logic triggered on
access, not a cadence loop — so whatever ceiling or cadence these operations actually have, it is
not sourced from these five keys. Bite condition, where determinable: `build_timeout_s` /
`clone_timeout_s` bite if the underlying operation's real ceiling (wherever it lives) is
materially different from the operator's tuned value; `graph.max_edges` bites only for a repo
large enough that a cap would matter; `reaper_interval_s` / `projection_hz` are cadence knobs
whose absence is likely cosmetic but was not traced to a concrete alternate constant.

> **PARTLY DISCHARGED 2026-08-25 (round G, lane W1, ADR-0080) — `budgets.build_timeout_s` is no
> longer inert, and the paragraph above is left standing as the record of what was true at its own
> commit.** §11.5 step 8 (`cli.resume`'s continuation) resolves `_build_impl`'s `timeout_s` from
> that key rather than from `fleet build --timeout`'s Typer literal, so the key has a reader in
> `src/` and its `KNOWN_INERT` line was deleted in the same change — `test_known_inert_keys_are_
> still_inert` fails while an entry there has become live, which is how this was found rather than
> reasoned about. **Group 4 is now 4 keys, not 5**, and the other four are untouched: nothing here
> gives `clone_timeout_s`, `graph.max_edges`, `reaper_interval_s` or `projection_hz` a reader.

**Group 5 — single-value policy keys nothing branches on (6 keys): latent, bite only off-default.**
`stubs.on_budget_exhausted` (`:604`), `build.fail_on_missing_adapter` (`:579`),
`build.openapi_generator` (`:580`), `scan.unknown_ecosystem_dest` (`:364`), `pr.reviewers_from`
(`:692`) each re-verified zero-hit outside `settings.py`. Each names a policy where the code path
has exactly one behavior regardless of the setting, so the default value is indistinguishable from
correct; the defect surfaces only if an operator picks the non-default option and expects a branch
that does not exist. `llm.cache_path` (`:679`) is the gentlest of the 26: `LlmCacheStore`
(`llm/cache.py`) never reads the name `cache_path` at all — its path comes from the caller — so
the operator's value is not missing-and-needed, it is redundant; the cache still gets a path,
just never this one.

**Would a test catch it? Partially, and only structurally.**
`test_every_config_key_is_read` and `test_known_inert_keys_are_still_inert` (both in this file)
are exactly the regression guard this ledger asks for: the day any of these 26 names is wired up,
the ratchet fails until the `KNOWN_INERT` line is deleted, and a *new* inert key fails on
introduction rather than accumulating silently. That is real coverage, and it is why this entry
exists rather than a 27th. What no test in the tree does is behavioral: nothing asserts that
setting `llm.rate_limit.targets.foo.rpm` throttles a call, that `llm.failover.cooldown_s` opens a
circuit, or that `preflight.baseline_build.enabled` runs a native build. The structural scan can
only prove a name is absent from `src/fleet/` outside `settings.py`; for these 26 that happens to
be equivalent to "does nothing," but the test's own docstring (limitation 2) flags where it is
not: `llm.max_schema_repairs` (`settings.py:680`) and `llm.failover.enabled` (`:660`) /
`.max_targets_per_call` (`:663`) share the exact spelling of unrelated `CallPolicy` fields
(`llm/client.py:452-453`, used at `:499`, `:732`), so the name-scan marks them "read" even though
`llm_policy` being unwired makes them exactly as inert as their twelve siblings in Group 1 — they are simply
not falsifiable by this test and so carry no `KNOWN_INERT` line and no D-number claim here. Not
encoded as part of this entry's 26; noted so the gap is not mistaken for absence of a defect.

**Not fixed here.** Wiring `llm_policy` from `settings.llm` into every `RunContext(` call site is
the one change that would close Group 1 in full and is not obviously more than a few lines per
call site, but doing it as a drive-by here would touch `cli.py`, which two other agents are
editing concurrently in this same pass, and Rule 3 confines this lane to
`docs/INTEGRATION_HONESTY.md`. `preflight.baseline_build` (Group 3) needs an actual native
build/test worker, not a wiring fix, and deciding what §14.1's gate should refuse on belongs in an
ADR, not this ledger. The other four groups are individually small, but batching six unrelated
config sections into one fix would violate Rule 2 (no speculative abstraction) for what is, in
each case, a single-purpose branch or accessor.

**Fourth correction, 2026-09-01 (round BB, controller — docs-only, no code touched, per Rule 6/
Rule 7's "annotate never rewrite" convention this entry already uses for
`budgets.build_timeout_s`'s "PARTLY DISCHARGED" marker above).** This entry's counts and its
"Group 1" narrative are stale a second time, found by round AA's research and re-verified here by
`ast`-parsing `tests/test_config_keys_are_read.py`'s frozenset literals directly (not by eye, not
by grep-count) at current `HEAD`: **`KNOWN_INERT`=38, `QUALIFIED_MATCH_KEYS`=11, `UNVERIFIABLE`=1,
`DECLARATIVE`=6** (this entry's third correction had pinned 46/11/1/6). The −8 drop is real
further wiring, not measurement error — `git log --oneline -- tests/test_config_keys_are_read.py`
shows commit `1963ca9` (2026-08-22, "context+config-keys: derive `CallPolicy` from `config.llm`,
so `llm.failover.*` reaches the client") landed after this entry's last correction and moved
`llm.max_schema_repairs`, `llm.failover.enabled`, and `llm.failover.max_targets_per_call` from
`KNOWN_INERT` to `QUALIFIED_MATCH_KEYS`-only, plus removed the `fleet.yaml:llm.failover` section
key from `KNOWN_INERT` entirely. (`budgets.build_timeout_s`'s removal was already correctly
annotated above; `pr.merge_wait_timeout_s`'s removal, round M/D80, was not previously noted here.)

**This is a factual-accuracy correction to the headline claim, not just a count refresh.** The
opening sentence above states `RunContext.llm_policy` is never assigned, so `LadderModelClient`
"always falls back to `CallPolicy()`'s own defaults no matter what `fleet.yaml`'s `llm:` block
says" — that is now **false**. `orchestrator/context.py`'s `RunContext.__post_init__` (landed in
`1963ca9`) reads `policy = call_policy_for(self.config.llm) if self.llm_policy is None else
self.llm_policy`: no call site passes `llm_policy=` (still true, re-verified `git grep
"llm_policy=" -- src/` empty at `HEAD`), but that no longer means "all defaults" — it means
"derive from config." This exact mechanism is correctly recorded in the separate entry **D58**
("PARTLY ADDRESSED (`1963ca9`)"), which this entry already cross-references once, but "Group 1 —
the sharp one" above still describes the pre-`1963ca9` world: 3 of its stated 12 keys
(`llm.rate_limit`'s 9 + `llm.failover`'s 3) are now demonstrably wired via `call_policy_for`.
**Group 1's true remaining size is 9, not 12** — `llm.rate_limit.*` only, untouched by
`call_policy_for`, which maps solely `max_schema_repairs` and `failover.max_targets_per_call`/
`.enabled`. Group 1's core complaint ("rate limiting doesn't work") stays true for those 9.

The **"16 of 37" figure** carried elsewhere in this project (e.g. round Z's own research summary)
traces to this entry's *first* correction (`KNOWN_INERT` first re-measured at 37, "bringing that
cause's count to 16 of 37"). Both halves have since moved twice (denominator 37→46→38, numerator
shrunk by the 3 keys `1963ca9` wired) — doubly stale, not merely off-by-a-little; do not cite
"16 of 37" going forward. Groups 2/3/5 are unaffected — no key named in those groups appears in
the diff between this entry's third correction and `HEAD` for `tests/test_config_keys_are_read.py`.

**One key genuinely wired, 2026-09-03 (round VI task 24, §12.22's startup-refusal sub-clause,
`c73d023`/`84800a7`) — with a caught, disclosed instrument limitation, not a silent count
change.** `budgets.max_host_rss_mb` now has a real caller: `cli.py::_load_settings` calls
`settings.validate_memory_budget(host_total_mb=_read_host_mem_total_mb())` after every
`FleetSettings.load()`, and the value genuinely affects behavior (exit 2 on breach, independently
mutation-proven by task-scoped review). Per this file's own ratchet, that should mean the key
leaves `KNOWN_INERT` — but attempting the removal and re-running
`test_every_config_key_is_read` failed: `tests/test_config_keys_are_read.py`'s own `_inert_keys()`
scan requires the bare key name to appear in `src/fleet/` outside `settings.py`, and the new call
site reads the field only indirectly (through `validate_memory_budget`'s own internal
`self.max_host_rss_mb` access) — the literal token never appears at the `cli.py` call site, so the
scan cannot see this form of wiring. The key stays in `KNOWN_INERT`, annotated in place at its own
line, not removed — no change to this entry's own counts (they describe a different measurement
taken before this key's wiring, and re-deriving them here would risk exactly the kind of
uncoordinated count drift this entry's own corrections already warn against). A future pass
extending the scan to recognize a validator-method call as a read could close this key's
`KNOWN_INERT` entry properly.

**Marker, 2026-09-03 (round VI task 36) — `budgets.max_host_rss_mb` now OUT of `KNOWN_INERT`; the
"future pass" the paragraph above deferred was not needed, because a *different* call site closed
the gap the scan can see.** A full-suite run found
`test_known_inert_keys_are_still_inert[fleet.yaml:budgets.max_host_rss_mb]` **FAILING** — not
because the scan changed, but because the code did: task 27/29's `HostMemorySampler` wiring
(`cli.py::_host_memory_sampler`, `cli.py:2010`, plus the two `_run_*_wave` composition roots at
`cli.py:8619` and `:8710`) constructs `HostMemorySampler(..., max_host_rss_mb=settings.config.
budgets.max_host_rss_mb, ...)` — a direct, literal `config.budgets.max_host_rss_mb` attribute read
outside `settings.py`, landed after the paragraph above was written and independent of task 24's
`validate_memory_budget` indirection it describes. `_readers("max_host_rss_mb")` now returns
`['cli.py', 'orchestrator/memory_guard.py']` — a real, non-empty reader list, not a scan
regression. Investigated by running both `test_every_config_key_is_read` and
`test_known_inert_keys_are_still_inert` directly (not guessed): the former was never the
contradiction (removing the `KNOWN_INERT` line changes nothing for it — `_inert_keys()` already
excludes this key once real readers exist, so it was never in the `unexplained` set either way);
the latter was correctly reporting that the `KNOWN_INERT` allowlist's claim ("this key is inert")
had gone stale. Reconciled by deleting `"fleet.yaml:budgets.max_host_rss_mb"` from `KNOWN_INERT`
in `tests/test_config_keys_are_read.py` (comment left in place, extended with this history) —
`tests/test_config_keys_are_read.py` run whole, no `-k`: 47/47 pass. This closes this one key's
piece of D50 (now genuinely, directly read, joining the other keys this entry already tracks as
wired out of the 26/37/38/46-count history above); **D50's heading stays OPEN** — the remaining
`KNOWN_INERT` members this entry catalogs (Group 1's 9 `llm.rate_limit.*` keys and the other
groups) are unaffected by this task and untouched by it.

---

**D51 — OPEN, narrowly. `workers/relocate.py` lands its patches through the exact same
`land_patches` that D49 gated, entirely outside `check_diff` — but D49's own fix is the wrong fix
here. The patches are 100% deterministic renames, not model-authored content, and neither of
`check_diff`'s two protections transfers cleanly: a byte cap measures the wrong thing for a
rename, and the subtree-escape check would reject every legitimate relocation outright. The
residual gap is narrower than "unbounded": there is no runtime assertion, anywhere, that the
destination path a relocate patch commits actually lands under `dest_path` — that guarantee is a
property of one function's string concatenation, unverified at the call site and untested.**

Found by a reviewer auditing D49's shape for siblings after `c5ab3b1` landed; D49 scoped itself
explicitly to `rewrite.py`'s two `land_patches` call sites and never named this one. Verified
against `HEAD` (`2a72f9f`); `workers/relocate.py` carries no uncommitted edits in this pass
(`git status` — absent from the modified list).

**The bypass is real.** `relocate.py:48` imports `land_patches` from `workers/rewrite.py` directly
(`from fleet.workers.rewrite import land_patches, units_owed`); `RelocateWorker.run` calls it at
`:184-194` on a single `FilePatch` built two lines above. `check_diff` is never imported into this
module — `grep -n check_diff src/fleet/workers/relocate.py` is empty. `land_patches` itself
documents the sharing as deliberate, not an oversight: its docstring (`workers/rewrite.py:154-156`)
reads *"Shared with `workers/relocate.py` — a rename and a rewrite differ in how the diff is
produced, never in how it is committed, and two copies of this sequence would be two chances to
skip the guard."* That sentence is about `apply_and_commit`'s idempotency-check-plus-`git apply
--check` guard, which both workers do get uniformly. It says nothing about `check_diff`, which
only `rewrite.py` calls — the omission from `relocate.py` reads, on this evidence, as intentional
rather than a gap nobody considered, which is exactly why it is worth confirming rather than
assuming.

**What reaches this path: purely deterministic, plan-computed renames — not model output.** The
one `FilePatch` built at `relocate.py:175-182` is `rename_diff(unit, new_path)` (`:84-95`): a fixed
four-line template — `diff --git`, `similarity index 100%`, `rename from`, `rename to` — with zero
hunks and zero file content. `unit` comes from `payload.sources`, which `RelocateInput` documents
as "supplied by the caller rather than derived from `git ls-files` here" (`:106-112`); tracing the
one production call site, `cli.py`'s `_relocate_input` passes `sources=list(payload.sources)`
(`cli.py:3362`, reading `TransformInput.sources`, declared `:3213`), which is populated as
`plan.sources` inside `_transform_payloads` (`cli.py:4028`) — a driver-computed plan object, never
an LLM response. No
rung, no `ctx.llm`, no `ProposedFileEdit` exists anywhere in this file. This is the "purely
deterministic" case the brief asked to check for, confirmed: `grep -n "ctx.llm\|propose_repair\|escalate_repair"
src/fleet/workers/relocate.py` is empty.

**Citation correction (`82654e8`).** This entry's own text predicted the hazard it now hits: it
was written against a `cli.py` under concurrent edit and flagged that. `82654e8` ('fix(D49):
thread transform.max_patch_bytes...') shifted the file after this entry was authored. Re-derived
against `git show 82654e8:src/fleet/cli.py`, not the working tree (`cli.py` is modified again per
`git status` as this correction is written): `sources=list(payload.sources)` is now at `:3369`
(was `:3362`), `TransformInput.sources` is unchanged at `:3213`, and `plan.sources` inside
`_transform_payloads` is now at `:4037` (was `:4028`). `relocate.py`'s own line numbers are
undisturbed (`82654e8` touched `cli.py`, `workers/rewrite.py`, `tests/test_cli.py` — not
`workers/relocate.py`); re-confirmed `grep -n check_diff src/fleet/workers/relocate.py` is still
empty at `82654e8`, so the substantive finding is unaffected — only the `cli.py` line numbers
moved. This is the second ledger correction landing on a `cli.py` citation invalidated by the very
commit that fixed the defect the citation was tracing (see D49's second correction, above); a
citation into a file under concurrent, active repair is stale by the time it is read regardless of
how carefully it was measured, which is the argument for pinning every such citation to the SHA it
was taken from rather than trusting a bare line number.

**Why D49's provenance rule does not imply the same gate — the byte cap.** D49's own severity
paragraph already states the general form: *"a legitimate migration rewrite may touch every file
in a repo, so a global `max_files_touched` would be actively wrong."* The relocate case is a
sharper instance of the same point applied to bytes instead of file count. `rename_diff`'s output
size is a function of two path lengths, never of file content — a repo with 100,000 files each
under a long path still produces one small diff per file, one `land_patches` call per file
(`relocate.py:164-194`, one iteration of `owed` per commit). There is no accumulation point where
`transform.max_patch_bytes` (tuned for a content diff, default 1 MiB) would ever fire short of a
single path being megabytes long. Applying it here would not guard against anything real; it would
just be a check that always passes, giving false confidence that this path is "covered" the way
D49's leg 1 is.

**Why D49's provenance rule does not imply the same gate — the subtree-escape check.** `check_diff`'s
second protection rejects any hunk path that is not `.is_relative_to(dest_subtree)`
(`rewrite/apply.py:255-262`). For `relocate.py`'s renames this check is backwards. The
*destination* path (`new_path = relocated_path(payload.dest_path, unit)`, `:174`) is always inside
`dest_path` by construction — `relocated_path` (`:79-81`) is `f"{dest_path.rstrip('/')}/{source.lstrip('/')}"`,
which cannot produce a path outside `dest_path` for any non-empty `source`. But the *source* path
(`unit`, the `rename from` side) is, by the entire purpose of this worker, expected to sit
**outside** `dest_path` before the move — that is what "relocate this repo's tree to its monorepo
path" means. Reusing `check_diff(diff, payload.dest_path)` unmodified would reject the `rename
from` hunk of every single legitimate relocation patch, which would not harden this worker, it
would break it. `check_diff` does carry an `allow_paths_outside_dest=True` escape hatch
(`:270`, checked at `:285-286`) that would dodge this — but reading its implementation shows it
returns `None` immediately when set, skipping not just the subtree check but also `_escapes`'s
unconditional absolute-path/`..`-traversal check (`:255-258`), so it is an all-or-nothing flag,
not a scoped one;
it was not designed for "outside this subtree but still traversal-safe," and bolting it on here
would need its own review, not a drive-by.

**The residual gap, once both of the obvious gates are ruled out.** `new_path`'s
containment-in-`dest_path` is real and currently guaranteed, but the guarantee lives entirely
inside `relocated_path()`'s one line of string concatenation (`relocate.py:81`) — nothing at the
`land_patches` call site (`:184-194`) or anywhere else in the worker asserts it. A future edit to
`relocated_path`, to how `new_path` is computed, or to `dest_path`'s validation (currently only
`Field(min_length=1)`, `:105`) would have no runtime check and no test catching a relocation that
committed a rename to somewhere other than the plan's destination before it landed on the branch.
That is a narrower, structural claim — not "unbounded," not "needs D49's cap" — and it is the one
part of this finding that is actually open.

**Would a test catch it? No, on any dimension.** `grep -n "def test_" tests/test_workers_transform.py`
shows four `relocate` tests (`:528`, `:557`, `:583`, `:1050`, `test_workers_transform.py`) covering
double-move idempotency and mid-plan interruption; none constructs an oversize patch, a source or
destination path that would escape a subtree, or asserts anything about `new_path`'s relationship
to `dest_path` at commit time. `check_diff` itself is unit-tested in `tests/test_rewrite.py:1020-1028`,
but nothing there or in `test_workers_transform.py`'s relocate tests exercises it against a rename
diff. The absence is consistent with the verdict above — there is no size/path defect to catch —
but it also means the one real residual claim (no runtime assertion on `new_path`'s containment)
is equally uncovered.

**Verdict: not a D49-shaped defect. Closing this without a cap is the correct fix, not an
oversight left unfixed.** A blast-radius cap belongs on a write whose shape or size a caller does
not fully control — that describes D49's LLM branch and does not describe this one. Recorded here,
rather than as NEEDS_CONTEXT, because the reviewer's premise (an unchecked `land_patches` call
D49 never named) is factually correct and worth a permanent note explaining why it stays unchecked
on purpose, so a future pass does not "fix" it by bolting on `check_diff` and breaking every
relocation in the fleet.

**Not fixed here.** The one real residual item — a cheap runtime assertion that `new_path` is
`.is_relative_to(payload.dest_path)` before `land_patches` at `relocate.py:184`, plus a test that
exercises it — is small and in `src/`, out of this docs-only lane (Rule 3). It is deliberately not
folded into D49's "Not fixed here" ADR-bound decision either: D49's open question is what *cap*
model-authored writes should carry; this worker's open question is an *invariant* check on a
deterministic write whose shape is already fully controlled, which is a different kind of fix and
does not need the same ADR.

---

**D52 — CLOSED, FIXED in `2976a7e`. A one-line fix for D49 silently reopened its own §3.2 gate one
layer up, in `cli.py`, for a single-run window.** Verified against `2976a7e` and current `HEAD`
(`f592327`, which does not touch `cli.py`'s `_TransformEvidence`).

**The mechanism.** `9a7148c` (D49 leg 3) correctly changed `RewriteWorker._record` to append every
landed `FilePatch.path` — not the deterministic unit name — to `output.rewritten`, because the
§3.2 parse probe needs the paths actually written, not the target it was aimed at. That commit did
not touch `cli.py`. `cli._TransformEvidence.record()` (`cli.py:3469-3480` before the fix) dedupes a
retry's `unresolved` list against everything already landed: `prior.unresolved = [unit for unit in
output.unresolved if unit not in set(prior.rewritten)]`. Before `9a7148c` this was an **identity**
check — `prior.rewritten` held unit names, so a unit only cleared `unresolved` by resolving under
its own name. After `9a7148c`, `prior.rewritten` holds landed **paths**, including collateral
siblings a multi-file LLM repair touched while fixing a *different* unit — so the same membership
test became a **coincidental filename match**: a unit whose own canonical path happens to equal a
path some other unit's repair collaterally wrote is read as resolved, even though it never landed
and never ran the probe. **Effect: a genuinely-unresolved unit can be silently dropped from
`unresolved`, `_transform_criterion` never sees it, and the run reports `SUCCEEDED` over a broken,
unprobed file** — reachable within a single `fleet transform` invocation via `PhaseRunner._drive`'s
in-process retry on a `partial` status, no resumption required, because `_TransformEvidence` lives
for one CLI invocation (`cli.py:4252`) and `record()` is called once per attempt into the same
instance.

**Severity: Critical, and worse than the defect the fixing commit closed.** D49 leg 3 was a
reporting gap that under-probed a file. This is the same gate accepting a **broken** file as
`SUCCEEDED` — the exact failure mode §3.2 exists to prevent — and it was live for exactly the
window between `9a7148c` and `2976a7e`, both landed in this same round.

**Found by review, not by the implementer — worth recording, since this file's purpose is
tracking how defects escape notice.** `9a7148c`'s own task report (`task-O1-report.md`) flagged
`_TransformEvidence.record()`'s dedup as an out-of-lane "follow-up observation," but misdiagnosed
its direction: it described the risk as a resolved unit being left listed as unresolved (a
false-negative annoyance) if a repair fixed a unit by editing *only* sibling files. **The real
defect runs the opposite way and is more severe** — a unit that is still genuinely broken gets
dropped from `unresolved` because a sibling's collateral path happens to name-match it, producing
a false `SUCCEEDED`, not a spurious unresolved entry. A reviewer auditing `9a7148c`'s shape for
exactly this kind of one-layer-up fallout caught the correct mechanism and dispatched the fix
directly rather than leaving it as a filed observation.

**The fix.** `2976a7e` gives `_TransformEvidence` a per-repo `_resolved: dict[str, set[str]]`
accumulating rewrite-unit **identities** — `WorkerResult.completed_units` entries namespaced
`rewrite:`, prefix stripped — populated on every call site that legitimately resolves a unit under
its own name (the deterministic land, the idempotent `find_task_commit` shortcut, and the
repair-rung land; never on a collateral edit). `record()` now takes `completed_units:
Sequence[str] = ()` and dedupes `unresolved` against `_resolved[repo_id]` instead of
`set(prior.rewritten)`; `_TransformSink.__call__` threads `result.completed_units` through at the
one production call site (`cli.py:3583`). `output.rewritten` is left holding `FilePatch.path`s,
unchanged from `9a7148c` — the §3.2 parse probe still needs that.

**Reviewed clean.** A second reviewer traced every `completed_units` write site in
`RewriteWorker.run` and confirmed the identity set is populated exactly on legitimate resolution
and never by a collateral edit, then confirmed the new tests fail with a signature-level
`TypeError` against pre-fix `record()` (not merely an assertion mismatch) — the failure a plain
revert would reproduce, which is the correct shape for a regression pin. Pinned by
`tests/test_transform_e2e.py`'s new section 6:
`test_a_units_own_failure_survives_a_siblings_collateral_rewrite` (the worked scenario verbatim —
a unit's own canonical path coincides with a sibling's collaterally-landed path, and the unit's
genuine failure still survives into `unresolved`) and
`test_a_units_own_completion_still_clears_it_from_unresolved` (a unit resolved under its own
identity is still correctly dropped, so the fix does not just widen `unresolved` back out).

**Would a test catch it before this round? No** — zero assertions existed anywhere in the suite on
`output.rewritten` before `9a7148c` added the first ones, and none of those exercised
`_TransformEvidence.record()`'s dedup at all; the gap this defect lived in was untested on both
sides of the commit that (re-)opened it.

---

**D53 — OPEN. `ContainerSandbox.reap()` reports a failed `docker rm` as reaped.** Verified against
`HEAD` (`f592327`; `src/fleet/sandbox/container.py` carries no uncommitted edits this pass —
absent from `git status`).

`ContainerSandbox.remove()` (`container.py:202-208`) runs `docker rm --force` and returns
`result.ok` — a plain `bool`, never an exception. `reap()` (`container.py:239-254`) calls
`await self.remove(name)` at `:252` **without reading the return value**, and unconditionally
appends `name` to `reaped` at `:253` on the very next line — regardless of whether the removal
actually succeeded. A `docker rm` that fails (daemon busy, container already mid-teardown, a
transient daemon error) is reported to the caller exactly like a successful reap, and the
container it names can still exist afterward.

**Reachability, honestly stated: `reap()` has zero production callers today.** `grep -rn
"\.reap(" src/fleet/ | grep -v test` is empty — §11.5 step 2's `fleet resume` sweep is
implemented and never invoked, the same absence D32 already recorded for this method. This defect
is therefore latent, not live, exactly as D32's own "would a test catch it?" line already implied
by naming `reap` as the unreached backstop.

**Distinct from D32, not an amendment to it — recorded as a new entry and cross-referenced.** D32
is a **leak**: the containerising path (`BuildverifyWorker._argv`) bypasses `ContainerSandbox.run`
entirely, so on a timeout the client is killed and the container is never asked to be removed at
all — no `remove()` call happens on that path. This entry is a **reporting collapse** inside
`remove()`'s own caller: `remove()` **is** called, docker **is** asked, and the asking can fail
without `reap()` noticing. Folding this into D32 would describe two different code paths and two
different failure shapes (no call vs. an unchecked call) under one root cause, which they do not
share — D32's fix is "route the containerising path through `ContainerSandbox.run`" or "wire up
`reap()`'s call site"; this entry's fix is internal to `reap()` regardless of who calls it or how
often. **Not the same shape as D44's fix either:** D44/the `WorktreeManager.reap()` regression
(`4a421a3`) was an **abort-mid-sweep** bug — one entry's `remove()` *raising* propagated out of the
whole loop, discarding already-accumulated progress and abandoning every worktree still to come.
`ContainerSandbox.remove()` never raises — it returns `False` — so there is no abort and no
discarded progress; the loop always completes and every name is visited. The defect here is purely
that the boolean answer is thrown away, not that an exception derails the sweep.

**Severity: low while unreached, and it inherits D32's own "medium" once `reap()` is wired up** —
a caller that trusts `reaped` as "these are gone" (exactly what a `fleet resume` sweep would do)
would leave a container running and believe otherwise, compounding D32's leak with a backstop that
lies about having caught it.

**Would a test catch it? No** — `tests/test_sandbox.py` has no case that scripts a failing
`remove()` inside a `ContainerSandbox.reap()` sweep and asserts on the returned list; the method
returns a bare `list[str]`, not the `ReapResult(reaped, failed)` shape `WorktreeManager.reap()`
was given in `4a421a3` for the identical honesty problem — the fix here is not a new invention, it
is threading that same already-landed pattern one file over. **Not fixed here**: `sandbox/*.py`
is out of this docs-only lane (Rule 3).

> **Editorial correction (2026-08-21), lane W2 — "`reap()` has zero production callers today" is
> false at `main`, so this defect is LIVE, not latent. The defect itself is unchanged and still
> OPEN.** `e915b93` landed §11.5 step 2's orphan sweep; `cli._reap_orphan_containers` calls
> `ContainerSandbox.reap()` on every `fleet resume`, and `grep -rn '\.reap(' src/fleet/ | grep -v
> test` re-measured this session returns **three** lines, not empty — two real call sites
> (`cli._reap_orphan_worktrees`, `cli._reap_orphan_containers`) and one docstring mention in
> `sandbox/container.py` that the text predicate cannot exclude. Cited by symbol: `cli.py` is
> another lane's file and had uncommitted edits in the tree while this was written. **Consequences,
> stated rather than implied:** (1) the severity line above — "low while unreached, and it inherits
> D32's own 'medium' once `reap()` is wired up" — has had its condition met, so read it as medium;
> (2) the caller that "trusts `reaped` as 'these are gone'" is no longer hypothetical, it is the
> resume sweep; (3) D32's cross-reference to this method as "the unreached backstop" is corrected in
> its own entry. The "would a test catch it?" answer above is unaffected: `tests/test_sandbox.py`
> still has no case scripting a failing `remove()` inside a sweep.

### `run.stale_after_s` leaves `KNOWN_INERT` — half of the defect it recorded, closed

`tests/test_config_keys_are_read.py`'s ratchet fired on the RS1 landing: `fleet.yaml:run.stale_after_s`
is now read, so its `KNOWN_INERT` line had to go. What actually changed is narrower than the
removal implies, and the difference is worth writing down rather than letting the deleted comment
take it with it.

**Now read:** `fleet resume` §11.5 step 3 computes its staleness cutoff from
`settings.config.run.stale_after_s` (`cli.py::_resume_impl`), so the key governs which `RUNNING`
rows a resume reclaims. That is a real read by the run's own reconciler.

**Still hardcoded, and still a defect:** nothing constructs `phases.heartbeat_ttl_seconds` from
this key. `schema.sql`'s `DEFAULT 300` and `migrations/v007_logical_keys.py`'s literal are what a
claimed phase actually carries, so `models/state.py`'s promise that the column is "Config-sourced
(`orchestrator.stale_after_s`, §9), captured per phase so a config change cannot retroactively
declare a live worker dead" remains prose. An operator who sets `run.stale_after_s: 900` moves the
resume sweep's cutoff and does NOT move the TTL the reaper compares against
(`SqliteStateRepository.reap_expired_phase_leases` keys on `lease_expires_at`, not on this
column) — two liveness horizons from one key, which is exactly the drift the per-phase capture was
supposed to prevent.

**Contained, not fixed.** The resume sweep does not get to pick a winner between the two clocks: it
reclaims a row only when the row has outlived **both**, so a lowered `run.stale_after_s` can never
reclaim a lease the per-row TTL still calls live (the two-writer collision on `migrate/<repo>`),
and a raised one only makes a resume more conservative than the reaper. That bounds the blast
radius of the disagreement wherever RS1's code reads it; it does not remove the disagreement.
**Still not fixed**: the fix belongs in `claim_phase`'s parameter list and in the `PhaseRecord`
construction path, neither of which is in RS1's lane (Rule 3). When it lands, the conjunction in
`_STALE_HEARTBEAT_PREDICATE` becomes a redundant no-op rather than a wrong answer, which is the
property that makes it safe to leave in place until then.

## D54–D70 — pre-existing defects surfaced by Round B (the unbuilt-subsystems round), none of which the suite fails on

**What makes this batch different from D26–D33 and D34–D45.** Those recorded defects in code the
round itself had just written. Every entry below was **already on `main`** before Round B started
and is verified against `7a8bfbb` — the round's lanes found them while building *beside* them.
Defects a lane introduced and then fixed inside the round are deliberately **excluded**; they are
churn, and the round's own ledger (`docs/superpowers/plans/ledger-sdd-backlog-b.md`) is where they
belong. Excluded on that rule, though the ledger records them at length: the `"strict": True` schema
violation (9 of 12 roles), the `effort` default fabrication (ADR-0075), every `LlmFindingSink`
error-path defect, `stubs.py`'s `_consumers_of` Critical, and the ADR-0075/0076 number collision.

**Numbering.** The highest number previously assigned in this document is **D53**. The entry
immediately above this block — `run.stale_after_s` leaves `KNOWN_INERT`, contributed by the RS1 lane
and landed with its branch — **claims no D-number**, so it neither collides with nor duplicates
anything here (its subject, `phases.heartbeat_ttl_seconds` having no writer in `claim_phase`, appears
in no entry below). It is left un-numbered: renumbering a landed entry is a separate edit, not a
side effect of appending. **D71 is the next free number.**

**The measurement that frames the whole batch, restated after landing.** The full suite was run on
unmodified `main` at `7a8bfbb` as the mandatory pre-land baseline — **1305 passed, 0 failed, 0
skipped**, `0 tests skipped this session`, `bazel disk: peak 4.22 GiB (ceiling 6 GiB) · residual
output bases 0 bytes`. Round B has since landed in full and the suite was re-run on `main` at
`6a41840` — **1575 passed in 1077.73s, 0 failed, 0 skipped**, same clean disk line, cache kept
1645 MiB. Green by CLAUDE.md §6 on both conditions, on both trees. It follows directly that **no
test in the suite fails on account of any entry below, before or after the fixes landed**. Read that
for exactly what it says and no more: it does not mean every entry is untested — several are
*asserted* by a passing test, or *documented* by one (D58's is documented verbatim in
`tests/test_config_keys_are_read.py`), which is worse than untested and is noted per entry. Where an
entry says "would a test catch it? No", that is a claim about the suite's content, checked
separately.

**Status vocabulary, used strictly — read it before citing any entry.**

| Status | Means |
| --- | --- |
| **OPEN** | Present on `main` at `6a41840`, after Round B landed in full. Nothing fixes it. Every OPEN entry below was re-verified against that SHA when this block was appended, per ADR-0073. |
| **FIXED, LANDED (`<sha>`)** | The fix is on `main`. `<sha>` is the landed (post-rebase) commit, not the `agent/*` branch SHA the lane and the pre-land audit cite — the branches were rebased, so those SHAs no longer resolve to these commits. |
| **PARTLY ADDRESSED** | Some legs landed, others still open; the entry says which, and which SHA carries the landed half. |

Landed ranges on `main`, for citation: ST1 `8968319..5c52ee5` · CLEAN1 `712fd5f..a9afe9d` · BK2
`92cfc94..eabfcdb` · BK1 `21f5797..6d5a4a8` · BK3 `87b51f8..36984cb` · RS1 `c45db53..74dc7bc` ·
DEM1 `791b428..ebd1624` · FD1 `d1ed2be..6a41840`.

**Three corrections to the candidate list as it was briefed**, made because the code did not support
the wording, and recorded here rather than silently dropped:

- "`client.py:532` never inspects `exc.trigger`" — it **does** read it, to label the emitted failover
  event. It never *branches* on it. D55 states the accurate form; the stronger claim is false as
  written.
- "the comment existed in **five** places" — **four** are pre-existing (D61). The fifth is two
  verbatim quotations inside code BK1 wrote this round, i.e. churn by this block's own exclusion
  rule. When a summary count and its own enumeration disagree, the enumeration is the evidence.
- "`_unavailable` … all five modules it names" — **five call sites, four distinct modules** at
  `7a8bfbb`, and **three call sites, two distinct modules** on `main` after RS1 landed (D63).

---

**D54 — FIXED, LANDED (`c45db53`). Nothing in the harness could clear the sticky budget halt, so
§10's documented exit-3 recovery was a permanent no-op and a budget-halted run was unrecoverable
without hand-editing SQLite.** Verified against `7a8bfbb`; fix verified on `main` at `6a41840`.

`git grep halted 7a8bfbb -- src/` finds exactly **one** writer of the column:
`state/repository.py:1410`, `"UPDATE budget_ledger SET halted = 1, updated_at = ? WHERE run_id = ?"`.
Its own docstring states the property as a design virtue — *"Sticky by construction: there is no
argument that writes `0`"* — and then names the way out it does not provide: *"§11.2 allows exactly
one way out — an audited `fleet resume --raise-budget` — and it does not come through here."* No
other code path wrote `halted = 0`. The reservation CAS at `:562` carries `AND halted = 0` in its
`WHERE`, so once the flag was set every reservation in every later process returned `rowcount == 0`
and refused.

**Consequence.** `docs/SPEC.md:6594` states *"`halted = 1` — clearing it requires `fleet resume
--raise-budget <usd>`, which writes an audited finding"*; `:6466` and `:7013` say the same from the
CI and fixture sides. That recovery did not exist. A run that crossed its ceiling was terminal, and
the only recovery was an operator editing `budget_ledger` by hand — outside the audited path the
SPEC requires, and outside `findings` entirely. (A briefing error worth recording: the exit-3
recovery was first cited at `SPEC.md:6445-6450`, which is the `--raise-wave-budget` paragraph and
mentions `--raise-budget` only by analogy. `:6594` is the real anchor.)

**Found by** the RS1 reviewer, checking a claim rather than accepting it: RS1 reported the gap as a
latent defect found while building `fleet resume`'s unrelated step 3, and the reviewer verified
**both halves independently** — the single-writer grep and the CAS predicate — before it was
recorded. The round ledger calls it "the round's most valuable single find".

**Would a test catch it? No**, and the shape is the reason: the sticky write is well tested, the CAS
predicate is well tested, and *the absence of a second writer* is not a thing a test asserts. The
suite was green on `main` with the defect present, at 1305 passed.

**Fix, and its exact reach.** `c45db53` puts `"UPDATE budget_ledger SET max_usd = ?, halted = 0,
updated_at = ? WHERE run_id = ? AND spent_usd + reserved_usd <= ? AND max_usd <= ?"` and the
`RunBudgetRaised` audit `INSERT` in **one** `StateWriter` unit, and `state/db.py` wraps every unit in
`BEGIN IMMEDIATE`→`COMMIT`, so the raise and its audit row are atomic. `repository.py:1410` is
**unchanged** and is still the only `halted` writer in `state/`; the clear lives in the CLI's resume
path, which is where §10 puts it. `git log -S"halted = 0" 7a8bfbb..main -- src/` returns `c45db53`
and nothing else.

---

**D55 — PARTLY ADDRESSED (`6d5c721`); see Status at the end of this entry. A pure 429 walks a
tier and reaches a halt that tells the operator every backend target is `DOWN` — a state with no
representation anywhere in `src/`.** Verified against `7a8bfbb`;
**re-verified OPEN on `main` at `6a41840`**, where the halt string is `orchestrator/runner.py:640`,
the two comment carriers are `models/enums.py` (`FailureClass.BACKEND_UNAVAILABLE`'s trailing
comment — `:383` at that anchor, `:390` at `f9cb3f9`) and `orchestrator/retry.py:196` (unmoved), and
`llm/client.py` is byte-for-byte the base file — no lane modified it.

Three hops, each verified at source:

1. `llm/client.py:532` catches `(SchemaUnsatisfied, TransportError)` as *"The ONLY two failover
   paths"* and fails the target over. It computes `trigger` at `:536-538` — and here the round's own
   shorthand needs correcting: the arm **does** read `exc.trigger`, but only to *label* the emitted
   failover event. **Nothing branches on it.** A `TransportError` carrying `RATE_LIMIT` and one
   carrying a refused connection take byte-identical paths. When the target list runs out, `:542`
   raises `TierUnavailable(route.tier, tried)`.
2. `workers/classify.py:247-248` maps `TierUnavailable` → `FailureClass.BACKEND_UNAVAILABLE`, and
   `:255-258` makes that class **non-retryable** (`retryable = failure_class not in
   (BUDGET_EXHAUSTED, BACKEND_UNAVAILABLE)`).
3. `orchestrator/runner.py:640` turns that class into `raise RunHalted(HaltReason.TIER_UNAVAILABLE,
   f"every backend target for {repo_id}'s tier is DOWN: …")`.

**`DOWN` is vocabulary the harness does not own.** It is `BackendHealth`'s state name from SPEC
§11.8, and `BackendHealth` is not built. On `main` the string `DOWN` appears in `src/` only inside
that halt message, two comments (`FailureClass.BACKEND_UNAVAILABLE` in `models/enums.py`,
`orchestrator/retry.py:196`), FD1's own
docstrings explaining why the finding refuses the word, and two unrelated `--max-cost-usd`
"DOWNWARD" strings in `cli.py`. So the message asserts a diagnosis from a subsystem that does not
exist, on evidence that does not distinguish throttling from an outage.

**Consequence — SPEC §13 row 43's named disaster, live in shipped code.** Row 43 exists to prevent
"sustained throttling mistaken for an outage": a fleet that is being rate-limited halts at exit 8
telling the operator its provider is down, when the correct action was to lower concurrency. There
is no rate limiter — R2 measured the shape of that gap: the 429 signal is in `llm/`, the semaphore is
`orchestrator/budgets.py:962`, and its **only** acquisition is `workers/classify.py:162`, i.e. 1 of
12 workers; `asyncio.Semaphore` has no resize API.
*(Adjudicated 2026-08-20 by the task-adjud lane, replacing the `CITE` lane's anchor-repair note
above, which correctly declined to adjudicate.* **D55 remains OPEN; the "no resize API" clause is
corrected, not the verdict.** The sentence is precise about what it names — `asyncio.Semaphore`
the stdlib class, unchanged, still exposes no public method to grow or shrink a live ceiling — and
that half of the claim is still true today, verified by inspection of the stdlib class this
session. What has changed is which object sits at `Limits.for_tier`: it is no longer an
`asyncio.Semaphore` at all. `src/fleet/orchestrator/budgets.py:1146` (`Limits.for_tier`) now
returns ADR-0083's `ResizableLimiter`, a hand-rolled primitive whose `resize()` (`:1062-1072`)
*is* safe to call while slots are held, and ADR-0084 hardened its interior admission gate. So the
sentence's premise — that the object guarding this acquisition point cannot be resized — is now
false; a resizable primitive sits there.

**That does not close, or partly close, the defect.** D55 rests on the halt message asserting a
`DOWN` diagnosis a throttled-but-not-outaged backend cannot support (§11.8's row 43), which is a
question of whether anything *reads the 429 signal and calls `resize()`* — not of whether `resize()`
exists. Verified this session: `grep -rn '\.resize(' src/fleet/` returns zero call sites anywhere
in `src/`; ADR-0083 §3 records this absence of a caller as deliberate — a no-op until a later
subtask builds the AIMD controller — and states that if the controller is never built, its own
preferred remedy is to revert the primitive rather than leave it as dead code, not to keep it as an
unused stand-in for a fix. `orchestrator/runner.py:640`
still raises the unqualified `is DOWN` halt on `main` at `f10a863`, `llm/client.py:532`'s failover
arm still does not branch on `exc.trigger`, and nothing in `src/` connects a 429/`retry-after`
signal to `Limits.for_tier(tier).resize(...)`. The causal chain D55 describes — pure throttling
reaches the same halt as a real outage — is unchanged end to end. **Net: premise corrected, defect
fully open, 0% closed by this work.** This matches, and does not extend, `docs/PROGRESS.md`'s own
running assessment that R2→R3 (not R1's primitive) is the slice that would close D55's causal hop.*)

*(2026-08-20, FIXA lane — **sweep only, verdict untouched.** The premise correction above had been
made here and nowhere else, leaving four scoping documents still asserting the old premise as live:
`docs/PROGRESS.md`'s §38 row-43 record and its next-work item 6, `docs/superpowers/plans/
ledger-sdd-backlog-b.md`'s R2 research summary, and `docs/superpowers/plans/open-items-audit-round-b.md`
item 6 (found by re-sweep; the review named only the first three). Each now carries a dated note
using this entry's framing — premise corrected, defect fully open, 0% closed — plus ADR-0083 §3's
revert recommendation, so that "the primitive exists" cannot be read as progress on row 43.
`docs/superpowers/plans/design-resume-step5-orchestrator-ledger.md`'s "OVERTAKEN BY ADR-0083" note
was annotated for the same reason. Re-verified this session: `grep -rn '\.resize(' src/fleet/` is
empty. **D55 stays OPEN at 0%.**)*

**Found by** R2 (research), scoping §13 rows 40 and 43 for build size and reading the halt path on
the way. Recorded in the round ledger as "the highest-risk finding of the round".

**Partly addressed, landed (`c5e28ce`, `31e6776`) — the *reporting*, not the defect.** FD1 was
explicitly told not to fix the misclassification (LARGE, out of scope) and not to codify the
misdiagnosis. Its `BackendUnavailable` finding row now reports what was *observed* (tier and every
target tried, in order) with machine-readable `asserts_outage: false`, a three-valued
`failover_triggers_recorded` that is **never** "complete", and a caveat naming throttling. But the
halt itself is untouched on `main`: `runner.py:640` still raises the `is DOWN` message,
`FailureClass.BACKEND_UNAVAILABLE`'s comment in `enums.py` still carries the word, `llm/client.py`
is unmodified across the whole round, and
`classify.py` was never in FD1's diff. **The operator-facing halt still asserts a cause nothing
determined.**

**Would a test catch it? No.** A test would have to drive a rate-limited transport through a full
tier and assert on the halt's *claim*, and the round found none. The suite is green on `main` at
1575 passed.

**Status — PARTLY ADDRESSED, 2026-09-08 (round VI task 88, ADR-0132, `6d5c721`).** Everything
above records what was true when it was written and is left standing. What changed:

* **`DOWN` now has a representation in `src/`.** `llm/failover.py::BackendHealth` is a real
  per-target three-state breaker (`UP`/`DOWN`/`HALF_OPEN`), held once per `LadderModelClient`.
  `llm/client.py::_call_target` gained a bounded same-target backoff-retry arm for
  `RATE_LIMIT` (reusing `orchestrator/retry.py`'s existing jitter/backoff primitive), and
  `complete()`'s target loop now calls `BackendHealth.record_failure` only on a QUALIFYING
  failure — a connection-level/5xx `TransportError`, or a `RATE_LIMIT` that has exhausted its
  entire backoff schedule — never on a single absorbed 429. This is §12.43 case (ii)'s literal
  text, proven by 8 tests in `tests/test_llm_failover.py` (old-fails/new-passes, backup-file
  method) plus the exhausted-backoff proof in `tests/test_llm_findings.py`.
* **What this closes, precisely.** The specific mechanism this entry names — a target that is
  merely throttled reaching the same halt as a genuine outage, on the strength of ONE 429 — is
  closed: a single 429 (or several, each absorbed) is retried on the SAME target and never even
  reaches the failover layer, so it cannot contribute to a tier exhausting. A target that
  answers 429 through its entire backoff schedule, `open_after_failures` times, IS now marked
  `DOWN` and drives the same `TierUnavailable`/exit-8 halt as before — which is §11.8's own
  design (case (ii)'s acceptance bar), not a residual instance of this defect: sustained,
  unrecovering throttling is meant to eventually read as unavailability.
* **What is NOT closed, disclosed rather than silently left.** (1) `orchestrator/runner.py:739`'s
  halt STRING is untouched in substance (only the comment above it was corrected, fix round 1) —
  it still reads `"...is DOWN"` unconditionally on any
  `TierUnavailable`, including the narrower edge case where a tier exhausts via `max_targets_
  per_call` before any individual target's `consecutive_failures` reaches `open_after_failures`
  (e.g. `open_after_failures=3` with each of 3 targets failing once) — that halt still fires
  with nothing in `BackendHealth` actually marking anyone `DOWN`. ADR-0132 judgment call 3
  explicitly left this wording change to implementer judgment, not mandated it; task 88 did not
  take it up, and Rule 14's build-to-the-literal-wording discipline is why. (2) The broader §13
  row 43 framing this entry's title carries — "no rate limiter exists at all" — remains open:
  the proactive token-bucket/AIMD half (`llm.rate_limit.rpm`/`tpm`/`aimd.*`) is unbuilt — 8
  genuine LEAF fields under that prefix are `KNOWN_INERT` (`honor_retry_after`, `defaults.rpm`,
  `defaults.tpm`, `targets.rpm`, `targets.tpm`, `aimd.shrink_factor`, `aimd.grow_every_s`,
  `aimd.floor`), plus 2 section-level entries for the same reason (`llm.rate_limit`,
  `llm.rate_limit.aimd`) — 10 `KNOWN_INERT` entries total under the prefix, re-counted directly
  against `tests/test_config_keys_are_read.py` rather than inherited from research-48's
  unattributed "8". Per ADR-0132's own explicit scoping (§12.43(ii)'s literal text is entirely
  about the reactive breaker, not proactive pacing). A sustained, uniform throttle across every
  target in a tier — one no backoff schedule ever resolves — still eventually halts the run at
  exit 8, which is correct per §11.8 but is the scenario an AIMD controller would instead have
  prevented by lowering concurrency.
* **Observability, beyond the call-log assertion §12.43(ii) requires.** A `backend_health_
  transition` event now records every `UP`→`DOWN`/`DOWN`→`HALF_OPEN`/`HALF_OPEN`→`UP`/
  `HALF_OPEN`→`DOWN` change, through the same buffer-then-flush sink (`LlmFindingSink`) D55's
  own "Partly addressed" reporting-machinery paragraph above already established — the natural
  place to consume it, per ADR-0132's own judgment call 3 question.

---

**D56 — FIXED, LANDED (`c36160e`). `llm.client.discover()` had zero call sites in `src/`, so the
backend registry could never be populated; and `settings.py` validated `backend:` against a
hard-coded tuple rather than the registry, so the "checked at construction" guarantee was vacuous
against an empty one.** Verified against `7a8bfbb`; fix verified on `main` at `6a41840`.

`llm/client.py:355` defines `discover()`. `git grep "discover()" 7a8bfbb -- src/` returned only
`ecosystems.discover()` sites — **the LLM one was never called**. `_BACKENDS` was therefore populated
by import side effect alone, via `register_backend`, and nothing in `src/` imported a backend module.
At `7a8bfbb` `src/fleet/llm/backends/` **did not exist as a directory**: `git ls-tree -r 7a8bfbb --
src/fleet/llm/` lists `__init__.py`, `cache.py`, `calls.py`, `client.py`, `roles.py`, `schemas.py`
and nothing else.

**The second half is what made it silent.** `settings.py:1163` read `backends = tuple(known_backends)
if known_backends is not None else SHIPPED_BACKENDS`, and `SHIPPED_BACKENDS` was the literal
`("anthropic", "openai_compatible", "bedrock", "vertex")`. The `known_backends=` parameter **already
existed**, is documented as the thing that "injects the live §7.7 registry (Guardrail 3)" — and
neither of `cli.py`'s two `FleetSettings.load(` sites supplied it. So the §9 gate validated a config
against a list of four names none of which corresponded to a registered backend. A profile naming
`anthropic` passed startup cleanly on a tree where no backend could answer.

**Consequence, and why it would have surfaced late.** A backend module lands, its own test imports it
directly (so `register_backend` runs, and the test is green), it ships — and the first real run
raises `UnknownBackend` at dispatch, i.e. at wave 7 of a long run, not at startup. The round routed
this to a single lane precisely because two lanes writing backends would each have shipped green.

**Found by** R1 (research) and CR1 (review) **independently converging on it** in wave 1, from
different directions — R1 from the call-graph, CR1 from the settings gate. Corroboration by two
agents that could not see each other is the reason it was trusted enough to route immediately.

**Would a test catch it? No** — the pre-fix suite had no test that asserted the registry's state in a
fresh interpreter; a backend's own test file populates the registry as a side effect of importing
itself, which is exactly the observation that hides the defect.

**Fix, and what landing proved that a branch could not.** `c36160e` calls `discover()` from
`cli._load_settings` and threads its keys into both `FleetSettings.load(` sites, which closes all
three readers of `_BACKENDS` at once: §9 rule 2's gate, `fleet models check`'s `registry()`, and
`RunContext(backends=None)`'s fallback. Guarded by a fresh-interpreter test that imports only
`fleet.cli`, asserts the registry is empty, then drives real startup; removing `discover()` fails 2
tests. **Demonstrated on `main`, not reasoned:** the post-land integration check prints `before: []`
then `after: ['anthropic', 'openai_compatible']` — the empty first line is the part that matters,
because it proves the registry is filled by `discover()`'s `pkgutil` walk and not by an accidental
eager import. A second check AST-parses `src/fleet/llm/backends/__init__.py` and finds **exactly one
non-docstring statement**, `from __future__ import annotations`, so `client.py:363-365`'s
silent-empty-registry path is unreachable. **Note the knock-on, which is not a defect but is a
behaviour change that landed with it:** the gate switched from four hard-coded names to the live
registry, so a config naming `bedrock` or `vertex` on a host without those extras now exits 2 —
correct by design per SPEC, with CLEAN1's remedy message (landed first, deliberately) naming the
missing extra. On this host `discover()` registers `anthropic` and `openai_compatible` only, because
`importlib.util.find_spec` returns `None` for `boto3` and for `google`; that is the designed
`client.py:366-369` path, **not** a regression, and `config/models.yaml` ships zero `bedrock`/`vertex`
targets.

---

**D57 — OPEN. The identical shape one parameter over, and nobody fixed this one:
`FleetSettings.load(capabilities=...)` is never supplied, so §9 rule 3 cannot consult a target's
declared capabilities at all.** Verified against `7a8bfbb`; **re-verified OPEN on `main` at
`6a41840`**.

`settings.py` declares `capabilities: Callable[[BackendTarget], ModelCapabilities] | None = None`,
documented as the thing that *"injects declared capabilities for the §9 rule 3 gate"*, and forwards
it to `_check_routing`. Rule 3's body is:

    declared = capabilities(first).max_context if capabilities is not None else None
    override = first.capabilities_override.get("max_context")

Neither `FleetSettings.load(` site in `cli.py` passes `capabilities=`. `declared` is therefore
**always `None`**, and rule 3 — the gate that is supposed to refuse a profile whose tier cannot meet
`llm.require_capabilities` — fires only when an operator has hand-written a
`capabilities_override.max_context` into `models.yaml`. On the config the harness ships, it cannot
fire.

**Why this is its own entry and not a footnote to D56.** It is the same defect *class* — a
dependency-injection parameter that exists, is documented, and is never supplied — but D56's fix does
not touch it. BK1 rewrote both `FleetSettings.load(` calls to multi-line form to add
`known_backends=`; `git grep "capabilities=" -- src/fleet/cli.py` is **empty on `main`**. The lane
that was in exactly the right function to fix this fixed the sibling parameter and left this one.
Recording it separately is what stops "BK1 wired the injection" from being read as covering both.

**Found by** the CR1 pre-flight review, then sharpened by R2, which corrected CR1's own framing: the
original claim was "§9 rule 3 never fires", and the accurate statement is narrower — it fires, but
only on an operator override, never on declared capabilities.

**Would a test catch it? No.** A test that exercises rule 3 by writing a `capabilities_override`
passes and proves the rule's arithmetic; nothing asserts that the *declared* leg is reachable. The
suite is green on `main`.

---

**D58 — PARTLY ADDRESSED (`1963ca9`); see Status at the end of this
entry. `RunContext.llm_policy` is never assigned, so no `llm.failover.*` config key reaches
the model client — and the repo's own test suite already documented this in a comment.** Verified
against `7a8bfbb`; **re-verified OPEN on `main` at `6a41840`**, where `orchestrator/context.py:141`
declares it and `:172` consumes it.

`context.py:141` declares `llm_policy: CallPolicy | None = None` and `:172` consumes it
(`LadderModelClient(self.llm, self.backends, policy=self.llm_policy)`). `client.py:478` then reads
`self._policy = policy or CallPolicy()`. `git grep "llm_policy=" -- src/` is **empty** on `7a8bfbb`
and still empty on `main`: no `RunContext(` site supplies it, so `policy` is always `None` and
`CallPolicy()` is always built with **all defaults**. `llm.failover.enabled`,
`llm.failover.max_targets_per_call` and `llm.max_schema_repairs` are read out of `fleet.yaml` into
`FleetSettings` and go nowhere.

**The suite documents the defect rather than catching it**, which is this document's whole thesis
happening again. `tests/test_config_keys_are_read.py` lists those three keys in `KNOWN_INERT` with
the mechanism spelled out in a comment: *"`CallPolicy()` is constructed with no override at its one
call site: `RunContext.llm_policy` is declared and consumed but never assigned by any of the five
`RunContext(` sites in `cli.py`. The bare names pass the plain scan on
`self._policy.max_schema_repairs` / `.max_targets_per_call` — real code, wrong object."* That comment
is accurate, it has been accurate for as long as it has existed, and the ratchet it lives in is
designed to keep the suite green while it stays true.

**Found by** R2, which corrected a too-generous earlier claim of its own lineage: research-B1 had
recorded "only `max_targets_per_call` is read", and R2's re-check established the stronger and
correct statement — **no** `llm.failover.*` field reaches the client; `client.py:499` reads a
`CallPolicy` field that merely shares a *name* with the config leaf.

**Not fixed, and the round says why.** FD1 reported it as its concern 3 and declined: closing it
needs a `CallPolicy.from_config` builder, edits to `cli.py` (off-limits to that lane), an invented
semantics for `failover.enabled` that no ADR decides, and three `KNOWN_INERT` deletions. That is a
decision for an ADR, not a drive-by. **This is a config-surface sibling of D50** — a live code
consumer correctly gated but never wired to the key that should drive it — and the same relationship
D49's second correction describes from the opposite end.

**Status — CLOSED IN PART, 2026-08-22 (round E, lane W22).** Everything above records what was
true when it was written and is left standing. What changed:

* `RunContext.__post_init__` now derives the policy — `policy = call_policy_for(self.config.llm)
  if self.llm_policy is None else self.llm_policy` — so `None`, which is what every
  `RunContext(` site in `cli.py` still passes, means *from config* instead of *all defaults*.
  (Five sites, AST-counted over the `f4eade0` blob of `cli.py` and again over the working
  tree; `llm_policy` is passed at none of them, then or now.)
  `orchestrator/context.py::call_policy_for` maps exactly two §9 leaves onto `CallPolicy`:
  `llm.max_schema_repairs`, and `llm.failover.max_targets_per_call` gated by
  `llm.failover.enabled` (§9: *"false ⇒ a tier uses only its first target; a dead target is
  fatal"* — one target walked, then `TierUnavailable`).
* **Exercised, not read.** Real `FleetSettings.load("config", cli_overrides={...})`, real
  `RunContext`, scripted offline backend, tier of three dead targets, config asking for one:
  before, the client walked `['t1','t2','t3']` and `client._policy == CallPolicy()`; after, it
  walks `['t1']`. FD1's "invented semantics for `failover.enabled`" concern is answered by §9's
  own comment, quoted above, rather than by an ADR.
* The three `KNOWN_INERT` lines this entry names are deleted, and so is a fourth it does
  not name: the section key `fleet.yaml:llm.failover`, inert only because no real (non-
  comment) code spelled the word. The three leaves keep their
  `QUALIFIED_MATCH_KEYS` membership, because the `CallPolicy` bare-name collision that made the
  plain scan useless for them is unchanged and is the only thing that would hide a revert.
* **Still open, and deliberately not faked:** `llm.failover.open_after_failures`,
  `llm.failover.cooldown_s` and `llm.failover.on_tier_exhausted` remain in `KNOWN_INERT`.
  They describe §11.8's three-state per-target `BackendHealth` in an `llm/failover.py` that
  does not exist; `CallPolicy` cannot express a circuit breaker. Measured after the fix, with
  the same scan the test file uses: those three still report inert.
* **This entry's own detector no longer detects it.** `git grep "llm_policy=" -- src/` returns
  **zero** hits *after* the fix as well as before — measured both ways — because the wiring
  landed at the consumption point rather than at a call site. A re-audit that reruns the grep
  quoted above will read a fixed defect as still open. The falsifiable check is behavioural:
  construct a `RunContext` with no `llm_policy=` and a config whose
  `llm.failover.max_targets_per_call` is 1, and count the targets the backend is asked for.
  `tests/test_run_context_llm_policy.py` is that check.

> **Editorial correction (2026-08-22), lane W26 — the heading above now reads `PARTLY ADDRESSED
> (`1963ca9`)`, not `CLOSED IN PART`.** The Status paragraph above is left exactly as lane W22
> wrote it, per this file's own convention (an entry's body records what was true when it was
> written; a dated marker beside it records what changed) — it correctly describes the fix and
> correctly warns that this entry's own detector reads a fixed defect as still open. "CLOSED IN
> PART" is not one of this file's three defined statuses (`OPEN` / `FIXED, LANDED (`<sha>`)` /
> `PARTLY ADDRESSED`); the heading is a status **field**, governed by that vocabulary and updated
> on fix, per the ruling at `39862ec` that corrected D63's heading the same way. `PARTLY
> ADDRESSED` is used rather than `FIXED, LANDED` because this entry's own title sentence — "no
> `llm.failover.*` config key reaches the model client" — is not fully resolved: three of the five
> `llm.failover.*` leaves (`open_after_failures`, `cooldown_s`, `on_tier_exhausted`) are unchanged
> and still `KNOWN_INERT` in `tests/test_config_keys_are_read.py`, re-verified by this lane. That
> is not a residual leg of `1963ca9`'s own fix — this entry's measured claim, both in its body and
> in the `KNOWN_INERT` comment it quotes, only ever named three leaves (`llm.failover.enabled`,
> `llm.failover.max_targets_per_call`, `llm.max_schema_repairs`), all now wired — but the title's
> broader wording keeps the entry's own claim partly open, and `CallPolicy` structurally cannot
> express the other three (§11.8's per-target `BackendHealth`, which `llm/failover.py` would
> implement and does not exist; the same gap D55 already tracks). Marking that inertness as false
> would be the mirror-image error this project names: it is still true, and stays recorded as
> such. The behavioural check that replaces this entry's rotted `git grep` detector is
> `tests/test_run_context_llm_policy.py`, already named in the Status paragraph above.

> **Editorial correction (2026-09-08), round VI task 88, ADR-0132 (`6d5c721`) — 2 of the 3
> "structurally cannot express" leaves above are no longer true, and D55's own entry is where
> the mechanism landed.** `llm/failover.py::BackendHealth` now exists, so `CallPolicy` genuinely
> can express part of the circuit breaker: `open_after_failures` and `cooldown_s` are read off
> it by `BackendHealth`'s own constructor args, and `call_policy_for` maps both. Re-verified with
> the same scan this file uses: those two leaves are no longer `KNOWN_INERT` in
> `tests/test_config_keys_are_read.py`. **`on_tier_exhausted` alone remains, and this paragraph
> does not close it** — it is `Literal["halt"]`, a single legal value, so there is no second
> value for a read of it to select between; it is recorded as still `KNOWN_INERT` for the same
> reason `stubs.on_budget_exhausted` is, not because the breaker is missing. This does not change
> this entry's own heading (still `PARTLY ADDRESSED (`1963ca9`)`) or its title's literal claim,
> which was always about `llm_policy`'s wiring path rather than about `llm/failover.py`'s
> existence — that is D55's claim, and D55's own entry (this file) now carries the fuller Status
> update for the circuit breaker itself.

---

**D59 — FIXED, LANDED (`d1ed2be`, with fix rounds through `6a41840`). Every `CapabilityDrift` and
every `BackendFailover` the client computed was discarded, for the whole life of the design.**
Verified against `7a8bfbb`; fix verified on `main` at `6a41840`.

`llm/client.py:472-473` accepts `on_drift` and `on_failover` callbacks; `:479-480` stores them;
`_emit_drift` and `_emit_failover` guard on `is None` and return. `orchestrator/context.py` is the
one place a client is assembled for a run, and it passed **neither**. So the drift computation (a
target's declared capabilities disagreeing with what the backend actually did) and the failover
records (which target handed off to which, and why) were computed on every call and dropped on the
floor. Nothing reached `findings`, nothing reached `events`, nothing reached an operator.

**The fixing commit states it more bluntly than this entry needs to.** The new `context.py`
docstring: *"for the whole life of that design **nobody supplied either callback** — every drift and
every failover the fleet computed was discarded at the `is None` guards in `_emit_drift` and
`_emit_failover`."*

**Found by** R1 and CR1 in wave 1 (routed as "trap T3"), from the same read that produced D56.

**Would a test catch it? No** — a callback nobody passes has no observable effect to assert on, and
the pre-fix suite contained no assertion on a persisted drift or failover row. The suite was green on
`main` with the defect present.

**Fix, and its scope stated honestly.** `orchestrator/findings.py::LlmFindingSink` buffers on the
sync hot path and drains through the run's single writer; `context.py` wires both callbacks;
`runner.py` flushes per dispatch and per wave. `llm/client.py` is **not modified** — the fix supplies
the callbacks the client already offered. 11 tests, every one asserting a **persisted SQLite row**
rather than a mock call. **One live gap the fix itself uncovered and closed:** `cli.py`'s `fleet pr`
verb builds a `RunContext` and runs `PrwriterWorker` directly with **no `PhaseRunner`**, so on the
first cut of the fix that shipped command buffered findings and never flushed them — the same
discard one layer up. Closed in fix round 3 (`337e1cf`) with the drain in a `finally` inside the
still-open writer. Two related items are **disclosed, not closed**, in code an operator reads:
`attempts.llm_failovers` is still unwritten (see D62), and `tier=` has no production caller on `main`
— `LlmFindingSink.record_backend_unavailable` (`orchestrator/findings.py`) declares
`tier: ModelTier | None = None` and the single call site, in `PhaseRunner._drive`'s
`FailureClass.BACKEND_UNAVAILABLE` branch (`orchestrator/runner.py`), passes none — so **every
shipped row is `scope: "run"`**. *(Citations re-anchored by symbol, 2026-08-21, round E lane W18,
per COMMON.md rule 5. Both line numbers this sentence carried were off by one at `da45a43`:
`findings.py:337` names the `async def` line while the `tier:` declaration is on 338, and
`runner.py:625-628` stops one line before the call's closing paren on 629. Both still resolved to
the right symbols, so this was rot, not falsity, and nothing else in this entry was altered. The
claim itself is re-measured and promoted to its own entry at D78.)*

---

**D60 — OPEN. `_emit_failover` never fires for the target that exhausts the tier, so the trigger set
behind a `TierUnavailable` is structurally unreconstructable — and a single-target tier emits nothing
at all.** Verified against `7a8bfbb`; **re-verified OPEN on `main` at `6a41840`**, at
`llm/client.py:540-542`.

    if index + 1 < len(targets):
        self._emit_failover(role, route.tier, target, targets[index + 1], trigger)
    raise TierUnavailable(route.tier, tried) from last

The guard is correct on its own terms — a failover event names a *hand-off*, and the last target
hands off to nobody — but its consequence is that the trigger for the failure that actually ends the
tier is never emitted. For a tier with N targets, N−1 triggers are recorded; for N == 1, zero.

**Consequence.** `TierUnavailable` carries the tier and `targets_tried` but no per-target
`FailoverTrigger`, so a consumer trying to answer "was this throttling or an outage?" — D55's exact
question — cannot recover the full picture from `events` either. This is the structural reason FD1's
row is right to refuse a diagnosis, and the reason its `failover_triggers_recorded` field can be
`"none"` or `"partial"` but **never** `"complete"`.

**Found by** FD1 while building D59's sink, and it is worth recording *how* the claim settled,
because the first form of it was wrong in the safe direction. FD1 originally reported that no
triggers were recoverable at all. The reviewer adjudicated: **true for the full set, false for the
partial set** — `exc.trigger` distinguishes `RATE_LIMIT` cleanly and 1..N−1 triggers *are* emitted
and now persisted, so a row claiming "none recorded" was itself inaccurate. The three-valued field is
the outcome of that correction.

**Would a test catch it? No**, and note the asymmetry: a test asserting that failovers are emitted
passes, because N−1 of them are. Only a test that counts triggers against targets on an *exhausting*
tier sees the gap.

**Not fixed anywhere.** **No lane modified `llm/client.py` this round** — verified across all eight
landed ranges.

---

**D61 — FIXED, LANDED (`712fd5f`, `a9afe9d`, `6d5a4a8`). A comment that inverted a cache invariant,
copied into four places, caused the same cache-poisoning bug in three independent lanes in a single
round.** Verified against `7a8bfbb`; fix verified on `main` at `6a41840`.

**The mechanism, at source.** `llm/cache.py` builds the **read** key from the *configured* primary
target, whose `model_id` is `target.model_id` out of `config/models.yaml`. It builds the **write** key
from the response: `"model_id": usage.model_id or parts.model_id`. And `client.py`'s `_stamp` lets a
backend-reported value win — `"model_id": usage.model_id or target.model_id`. So an adapter that
populates `TokenUsage.model_id` from what the server *reported* (an API body's `model` field, a
Bedrock inference-profile ARN, a Vertex snapshot id) makes the two keys disagree on **every** call: a
permanent, silent, 100% cache miss.

**Why it is invisible, and this is the sharpest part.** The miss is indistinguishable from a cold
cache — the rows are written, they just are never read. The one signal the codebase names is
`attempts.llm_cache_hit`, which `models/tasks.py` calls *"the only cache signal (§11.2, §11.6)"* —
and that column is `INTEGER NOT NULL DEFAULT 0` which `record_attempt` **never writes** (D62). The
detector for this defect is a column nothing sets. **Second consequence, verified and correctly
scoped:** `cache._target_for` matches `(backend, model_id)` against the route's targets to recover
the answering target's `effort`, itself a key component — so a served name also loses that match and
`effort` falls back to the primary's. The round's first framing of this called it an independent
defect; the reviewer corrected it to what it is — **second-order**, mis-attributing only on failover
to a standby whose `effort` differs. **That second-order leg is still OPEN on `main`**:
`llm/cache.py:621-628` still matches on `(backend, model_id)`, documented by CLEAN1's rewritten
docstring and not fixed.

**The documentation defect, with a measured cost.** Four pre-existing sites described the field as
the backend-reported id, i.e. the exact opposite of the invariant:

- `src/fleet/models/tasks.py:47` — `model_id: str = ""  # the RESOLVED model id, as the backend reported it`
- `src/fleet/state/schema.sql:434` — `model_id TEXT NOT NULL,  -- RESOLVED id. …`
- `docs/SPEC.md:2845` — byte-identical to `tasks.py:47`
- `docs/SPEC.md:4164` — the `llm_cache` DDL listing, carrying `schema.sql:434`'s wording

**Cost:** two of the round's three backend lanes implemented the bug (BK1 with `model_id=message.model`,
BK2 in `openai_compatible.py`); the third avoided it **only because it was warned explicitly**. The
round ledger's verdict — *"BK2 was not careless"* — is the correct reading: the comments actively
invited the choice.

**Found by** the orchestrator, by direct `grep` against a lane's commit rather than by trusting a
reviewer's report — then by each successive lane that touched the text, each finding one more copy.
BK1 found `SPEC.md:2845`; CLEAN1 found the unflagged `SPEC.md:4164`. **Record the count precisely:**
the round says "five places". Four of those are pre-existing (above); the fifth was two verbatim
quotations of the comment inside code BK1 wrote *this round* — round churn, closed at `6d5a4a8`, and
the second of the two was wrapped across lines so it escaped a single-line grep. The durable lesson
is the one the round drew: **a wrong comment propagates by copy, so fixing "the" site is almost always
fixing one of N.**

**Would a test catch it? No.** There is nothing to fail: the cache still stores, still reads, still
returns correct answers — just never a hit. The round's regression tests for it assert
`len(fake.requests) == 1` on a two-call round trip through a real `CachingModelClient`; a
`len(store) == 1` assertion, the obvious one, **would have passed with the bug present**.

**Fix.** CLEAN1 rewrote all four sites to state the invariant *and the reason* — including the one an
adapter author actually reads, `client.py`'s assignment, where the `or` had read as an invitation.
`_stamp`'s **behaviour is deliberately unchanged** (several lanes depend on it); the fix is
comment-only plus BK1's two quotations, and the pre-land audit measured CLEAN1's `client.py` diff at
+28/−1, **all docstring or comment**. A correct-as-written sibling wording elsewhere in `SPEC.md` /
`tasks.py` (where "resolved" means the id the *role routed to*) was **reported and deliberately not
edited** — editing a correct sentence because it matches a grep is the mirror-image error, and this
round saw both.

> **Editorial correction (2026-08-21), lane W19 — the second-order leg above is RE-VERIFIED OPEN,
> not closed by `9555346`; only its line anchor has rotted.** That commit grew `_target_for`'s
> docstring by seven lines, so the range `llm/cache.py:621-628` no longer bounds that function —
> it now runs to `:635`, and `:628` falls inside the docstring. Cite **`cache._target_for`**
> (`src/fleet/llm/cache.py`). The sentence *"That second-order leg is still OPEN on `main`"* is left
> exactly as its author wrote it because it is **still true**, re-measured at `704099c` with
> `.venv/bin/python`: a `usage.model_id` carrying the id the *server reported* rather than the id the
> config declared still yields `_target_for(...) is None`, and
> `CachingModelClient._store_response`'s `None` branch still keeps `parts.effort`, which
> `CachingModelClient.complete` builds from `route.targets[0]` — the primary's. What `9555346`
> closed is a **different door onto the same symptom**: a route declaring one `(backend, model_id)`
> at two `effort` levels, now refused at router construction by
> `TierRoute._one_effort_per_backend_and_model_id` (**ADR-0091**; the record `9555346` *does*
> falsify is item 11 of `docs/superpowers/plans/open-items-audit-round-b.md`, marked there). The validator cannot reach
> this leg — there is no ambiguity inside the route for it to refuse. **This marker exists because
> `9555346` was twice reported in round E as having closed this sentence — once by the lane that
> wrote that commit and once by the brief that routed this marker. It had not.**
> **The heading is unchanged, and that is the ruling, not an omission.** By the "Status vocabulary,
> used strictly" block above — and by the ruling recorded at `39862ec`, that a heading is a status
> **field** governed by that vocabulary and updated on fix — the heading states the disposition of
> **this entry's own defect**, the four copied comments, which is `FIXED, LANDED`. The second-order
> leg is a *consequence* recorded in the body; a body is annotated, never rewritten, and it does not
> govern the heading. Nothing in the vocabulary block permits a `FIXED, LANDED` heading to be
> reopened for a consequence the entry itself scopes as second-order.

*(2026-08-25, round F lane W11 — **annotation only. Heading, body and the marker above are all left
exactly as their authors wrote them; nothing here is retracted.** The layer **under** this entry
closed at `cac537d`. `CachingModelClient` — the component whose read/write key disagreement this
entry records and whose four copied comments it fixed — was constructed by **no shipped run** for
the whole life of that fix: D79 measured it and is now `FIXED, LANDED (cac537d)`. So D61's fix was
correct at its own commit and was, until `cac537d`, correct about a code path production never
reached. Re-derived at `b5f7760`: `RunContext.__post_init__` now wraps unconditionally
(`src/fleet/orchestrator/context.py:228-241`), so this entry's invariant is load-bearing on a
shipped run for the first time. **No claim in this entry changes** — the cache-key mechanism, the
`9555346` correction above and the heading ruling are all untouched and all still hold.)*

---

**D62 — FIXED, LANDED (`f88e105`). `record_attempt`'s `INSERT` omits five declared columns, so `llm_failovers`,
`llm_backend`, `input_tokens`, `output_tokens` and `llm_cache_hit` are dead in shipped code.**
Verified against `7a8bfbb`; **re-verified OPEN on `main` at `6a41840`**.

`SqliteStateRepository.record_attempt` in `state/repository.py` (`:1671` at that anchor, `:1861` at
`f9cb3f9`); its `INSERT INTO attempts` SQL (`:1677-1690` then, `:1867-1880` now) names 24 columns
(`attempt_id … finished_at`) and none of the five — re-read on `main`, unchanged. `git grep` for any
of the five names in `repository.py` returns nothing. The columns are declared and carry CHECK-free
defaults in `state/schema.sql` — `llm_cache_hit` is `INTEGER NOT NULL DEFAULT 0`, commented *"1 =>
served from llm_cache, cost_usd = 0"*; `llm_backend` and `llm_failovers` are ADR-0023's, the latter
*"backend hops spent inside THIS attempt"*. Every attempts row the harness has ever written carries
the defaults.

**Consequences, and they compound with other entries.** `llm_cache_hit` is the detector D61's defect
needed and did not have. `llm_failovers` is the per-attempt attribution D55 and D60 would want.
`input_tokens`/`output_tokens` make per-attempt token accounting unavailable even though `TokenUsage`
computes both. **SPEC §12.24's fixture assertion** requires that a `--profile local` run end with
*"every row carrying a non-empty `backend` and `llm_cache_hit = 0`"* — half of which passes for the
wrong reason (the column is 0 because nothing writes it) and half of which cannot hold at all
(`llm_backend` is `NULL`, never non-empty). Round B landed the `local` profile that assertion is
written against, so the gap is now reachable by a shipped configuration.

**Found by** R2, which also corrected the round's own earlier framing: the first report named
`llm_failovers` alone; re-reading the statement gave **five**.

**Would a test catch it? No** — and this is the "silent-zero" failure §12.24 was written to catch,
arriving through the writer rather than the pricing. The suite is green on `main` at 1575 passed.

**Not fixed, and the reason is structural rather than effort.** FD1 reported `attempts.llm_failovers`
as its concern 2 and declined: attribution is impossible from a wave-shared client without changing
`WorkerError` (which carries no tier) or `TierUnavailable`'s raise sites — the same cross-lane change
deferred for `tier=`. Writing the other four is smaller and unowned.

*(2026-08-25, round F lane W11 — **annotation only; this entry stays `OPEN` and not one word of it
is rewritten. A stated REASON is retired while the SYMPTOM is live for a NEW cause.** The retired
reason is not stated here but in **D79**, which added that `llm_cache_hit` *"would read 0 even once
written, because there is no cache to hit"*. That is **obsolete at `cac537d`**: `RunContext.__post_init__`
now derives a `SqliteLlmCacheStore` and wraps the ladder client unconditionally
(`src/fleet/orchestrator/context.py:228-241` at `b5f7760`), so a hit is reachable on a shipped run.

**The symptom is unchanged: `attempts.llm_cache_hit` still reads `0` on a hit.** The cause is now
**two** independent gaps where the body above named one:

* **(a) this entry's own defect, untouched.** `record_attempt`'s `INSERT INTO attempts` still omits
  the column. `cac537d` touched `cli.py`, `orchestrator/context.py` and four test files; it did not
  touch `state/repository.py`.
* **(b) a NEW cause, introduced by the wiring and disclosed by the wiring commit itself.**
  `CachingModelClient.__init__` takes an `on_hit: Callable[[LlmCallRecord], None] | None = None`
  (`src/fleet/llm/cache.py:412`), stores it (`:425`) and calls it on a hit (`:560-561`).
  `__post_init__` passes **no** `on_hit`, so nothing reports a hit to the writer even if the
  `INSERT` named the column. `cac537d`'s message states this: *"C1 does not pass on_hit, so
  attempts.llm_cache_hit still reads 0 on a hit."*

Both halves must land before the column is truthful; neither is done, and (b) is unowned. The other
four columns this entry names — `llm_failovers`, `llm_backend`, `input_tokens`, `output_tokens` —
are entirely unaffected by `cac537d`. `SPEC §12.24`'s fixture assertion is affected in one direction
only, and not in this entry's favour: its `llm_cache_hit = 0` half used to pass because nothing
wrote the column *and* nothing could hit the cache, and it now passes because nothing writes the
column alone.)*

*(2026-08-25, round F lane W13 — **annotation only; status stays `OPEN` and nothing above is
rewritten, W11's marker included.** Recording what lane **W15** measured when it was dispatched to
close leg (b) by passing `on_hit`, and returned **BLOCKED**. Every figure below re-derived by W13 at
`53e5d8d` before being written; W15's own report and a runnable repro are at
`.superpowers/sdd/handoff-round-f/lanes/W15/`.*

* ***Leg (a) is real, and this entry's class is larger than the heading says — its five are all
  correct, and there are two more.*** Parsing `SqliteStateRepository.record_attempt`'s
  `INSERT INTO attempts` column list out of the source and differencing it against
  `PRAGMA table_info(attempts)` over `state/schema.sql` gives **31 declared / 24 named → 7
  unwritable**: this entry's five plus **`integration_ref`** and **`container_id`**. Derived a
  second, genuinely different way — `AttemptRow`'s own annotated field set (24) differenced against
  the same 31 — the seven are **identical**. `integration_ref` is not in the same position as the
  other six: it is written afterwards by `cli._AttemptWriter._stamp_ref`'s separate
  `UPDATE attempts SET integration_ref = ?`, so it is reachable; `container_id` is not.
  Consequence for whoever fixes this: adding `llm_cache_hit` to the `INSERT` **without a producer**
  would create one more declared-and-never-assigned field — the D79 shape, not a fix.*

* ***Leg (b) is not merely unowned; `on_hit` alone cannot close it, because the attribution does not
  exist to be passed.*** `LlmCallRecord` (`src/fleet/models/tasks.py`) has **16** fields and **none**
  of `run_id` / `repo_id` / `phase` / `attempt`, and one `CachingModelClient` serves a whole wave —
  so an `on_hit` callback is handed a record that cannot name the attempt whose row it would set.
  That is this entry's own wave-shared-client argument, which it already accepts for
  `llm_failovers`, now confirmed for this column too. **A buffer-then-`UPDATE` fails for an
  independent second reason**: inside `PhaseRunner._drive`, `_drain_llm_findings` is called at
  `runner.py:489` and the `attempts` row is written by `self._sink(...)` at `:520`, so any flush
  keyed to the drain precedes the row it would update.*

* ***The one seam that could have supplied the identity was closed this round, deliberately and
  correctly, by us.*** `RunContext.worker_context()` handing a worker a per-attempt `scoped()` view
  of the client was the natural attribution route. `53e5d8d` landed
  `tests/test_run_context_llm_cache.py::test_the_client_handed_to_a_worker_is_the_one_this_file_drives`,
  asserting `worker.llm is ctx.model_client` (`:376`). **That test was the right fix for a real
  review finding** — the file's justification for driving `model_client` instead of the worker
  surface cited an assertion that did not exist anywhere in the tree — **and it is simultaneously a
  constraint on this entry's repair.** Both facts are recorded here so that whoever takes the
  eventual route relaxes that assertion as a decision rather than discovering it as a surprise.*

* ***The route W15 recommends instead, recorded as an Agent Recommendation and NOT as a
  requirement*** (CLAUDE.md Guardrail 1): carry the hit flag on `TokenUsage`, set it in
  `llm/cache.py`'s `_replay`, so the attribution travels **by data flow** rather than by ambient
  context. Two things that route must not miss: it has to be **`OR`-ed** in `workers/base.py`'s
  `accumulate()` (`:228`) or a multi-call attempt silently drops the flag; and it needs
  `state/repository.py` and `cli.py` changed **in one commit by one author**, since a multi-site
  correction split across authors ships partial wording. **Deferred to round G by ruling**, not by
  oversight: what a hit flag should mean for an attempt that made several calls, some hits and some
  misses, is unsettled in SPEC §11.6 and needs a ruling of its own before any column can be truthful.*

* ***`docs/SPEC.md` §11.6's cache-hit bullet — *"Cache hits set `attempts.llm_cache_hit = 1` and
  `cost_usd = 0`, so cost accounting stays honest"* (`:7181` at `53e5d8d`) is deliberately NOT
  being edited, by ruling, and this entry is where that is recorded.*** It is a **conjunction whose two halves have different truth values**: `cost_usd =
  0` is true and exercised (`_replay` returns the usage with `cost_usd` zeroed), while
  `attempts.llm_cache_hit = 1` is false on every path and under no condition — so there is no
  conditional wording that would make the sentence describe the code. Softening it to match code the
  project intends to fix is how the `supports_effort` class of defect is manufactured. **The sentence
  therefore stands as a true REQUIREMENT and a false DESCRIPTION, and that split is a property of
  this entry, not of the SPEC.** Two neighbours a later lane must keep consistent if it disagrees:
  §11.2 (`:6785`) lists `llm_cache_hit` among six columns written in **one** transaction, which
  `record_attempt` plus `_stamp_ref`'s separate `UPDATE` already does not satisfy; and §12.24
  (`:7351`) / §12.44 (`:7371`) still pass for the wrong reason, exactly as this entry's body says.*

*W11's citations above re-derive **unchanged** at `53e5d8d` — `src/fleet/orchestrator/context.py`
has **zero** commits between `b5f7760` and `53e5d8d`, which is why. **What this annotation does not
establish:** W13 changed no code for it and ran no suite for it; every figure is an `ast` or
normalised-text read of the working tree at `53e5d8d`, plus one `sqlite3` `PRAGMA table_info` over
`state/schema.sql`. The behavioural half — that a shipped `RunContext` leaves the column at `0` on a
real hit — is **W15's** measurement, reproducible from its repro script, and is cited here rather
than re-executed.)*

*(2026-08-25, round G lane W4 — **annotation only; this entry stays `OPEN` and nothing above is
rewritten, W11's and W13's markers included.** Recording the **orchestrator ruling on
`llm_cache_hit` semantics** that W13's marker above explicitly deferred to round G — *"what a hit
flag should mean for an attempt that made several calls, some hits and some misses, is unsettled in
SPEC §11.6 and needs a ruling of its own before any column can be truthful."* Every figure below
re-derived by this lane at `8b40498`; no code changed, no suite run.*

***RULING: `attempts.llm_cache_hit` is ALL-HIT.*** The row's flag is 1 iff **every** model call the
attempt made was served from `llm_cache`.

***The reason is decisive, and it is a shipped invariant rather than a preference.***
`state/schema.sql:729` declares the column with the comment ***"1 => served from llm_cache,
cost_usd = 0"*** — an invariant binding the flag to the cost. `llm/cache.py::_replay` returns the
cached usage with `cost_usd` zeroed (`:564`, `model_copy(update={"cost_usd": 0.0})`), while
`workers/base.py::accumulate` **sums** `cost_usd` across an attempt's calls (`:241`). So under
**any-hit** or **count** semantics a partial hit yields `cost_usd > 0` with the flag at 1 —
**breaking a stated invariant that is already shipped in three places**: `state/schema.sql:729`,
`docs/SPEC.md:4614` (the same DDL, mirrored), and `docs/SPEC.md:7188` (§11.6 prose: *"Cache hits set
`attempts.llm_cache_hit = 1` and `cost_usd = 0`, so cost accounting stays honest"*). All-hit is the
only semantics that preserves the invariant **without editing all three**. The dispatch named that
paired edit as `schema.sql` + `SPEC:4614`; re-measured here it is a **three**-site edit, and the
third site, `docs/SPEC.md:7188`, is the one W13's marker above rules must **not** be softened.

***A direct consequence for the route W13's marker recommends, and it inverts one word of it.***
That Agent Recommendation says a hit flag carried on `TokenUsage` *"has to be **`OR`-ed** in
`workers/base.py`'s `accumulate()` … or a multi-call attempt silently drops the flag"*. Under this
ruling it must be **`AND`-ed**: `OR` is precisely any-hit semantics and is exactly the edit that
would break `state/schema.sql:729`. The recommendation's *mechanism* — carry the flag by data flow
through `_replay` and `accumulate` rather than by ambient context — is untouched; only the
combinator changes. **Recorded here because an implementer reading that marker alone would `OR`
it**, and the marker is a correct record of what W13 wrote and is not being rewritten.

***The cost of the ruling, recorded rather than buried: partial hits are the NORMAL case, so an
all-hit column will read `0` almost always.*** An attempt is multi-call by construction:
`accumulate(` has **7** call sites in `src/` at `8b40498` — `workers/buildgen.py:315`, `:366` (the
per-module `MODULE.bazel` step) and `:504`, `workers/prwriter.py:418` and `:426`,
`workers/rewrite.py:389`, and `workers/base.py:734` — each folding one further call's usage into the
attempt's total. A buildgen attempt therefore reaches three accumulation points before its row is
written, and one cold prompt among them takes the flag to 0.

***Why no consumer's need overrides that — measured. The column has ZERO consumers.***
`grep -rn llm_cache_hit src/` returns **11** sites at `8b40498`: **1** DDL declaration
(`state/schema.sql:729`) and **10** comments or docstrings (`models/tasks.py:53` and `:62`,
`llm/cache.py:396`, `llm/client.py:903`, `llm/backends/anthropic.py:374`, `bedrock.py:628`,
`vertex.py:595`, `openai_compatible.py:534`, `state/schema.sql:512`,
`orchestrator/budgets.py:225`). **No `SELECT`, no projection, no branch anywhere in `src/`.** And
**every `docs/SPEC.md` §12 criterion naming the column is single-call and so discriminates nothing**:
§12.24 (`:7351`) asserts `llm_cache_hit = 0` over a `--profile local` run — all-miss, identical under
all three semantics — and §12.44 (`:7371`) issues ***"One fixture call"*** per profile, so both its
miss and its second-run hit are single-call, where all-hit, any-hit and count coincide. **That is
*why* the invariant wins: nothing in the tree needs partial-hit granularity today.** If a consumer
ever does, the three-site paired edit above is the known price, and it is a SPEC decision rather than
an implementation one.

***What this annotation does not establish.*** No column became truthful, and this marker fixes
nothing. Both legs this entry's markers name are still open — leg (a), `record_attempt`'s `INSERT`
(which W13 measured as **7** unwritable columns, not five), and leg (b), the `on_hit` that
`RunContext.__post_init__` still does not pass (`orchestrator/context.py:233-241` constructs
`CachingModelClient` with three positional and two keyword arguments, none of them `on_hit`;
re-verified at `8b40498`). This marker settles only what the flag must MEAN when someone writes it.)*

*(2026-08-26, round H lane W2 — **the status FIELD moves `OPEN` -> `PARTLY ADDRESSED`; not one
word above is rewritten, W11's, W13's, W15's and W4's markers included.** One of this entry's five
columns is now written; four are not, which is exactly what this file's own vocabulary block calls
`PARTLY ADDRESSED`. Landed in `f9ff651`.*

***Which leg landed, as a class result rather than a raw total.*** Re-deriving W15's §2
measurement against the patch, two genuinely different ways — `ast.literal_eval` of
`record_attempt`'s `sql` assignment for the `INSERT` column list, and `PRAGMA table_info(attempts)`
on a database built from the real `state/schema.sql` — gives **31 declared, 25 named**. The class
*"declared in the schema and never nameable by `record_attempt`"* goes **7 -> 6**:
`integration_ref, container_id, input_tokens, output_tokens, llm_backend, llm_failovers`. Of this
entry's five, **`llm_cache_hit` is closed and `llm_failovers`, `llm_backend`, `input_tokens`,
`output_tokens` remain open.** `AttemptRow` gained the field, `record_attempt`'s `INSERT` names the
column, and `iter_attempts` reads it back.

***Leg (b) is closed by a different door than the one this entry describes, and `on_hit` is
untouched.*** The flag is not attributed by `on_hit` at all: `TokenUsage` gained two counters
(`llm_cache_lookups`, `llm_cache_hits`) which `CachingModelClient` stamps on **both** sides of the
lookup, and they ride the usage that already flows from the call to the attempt that billed it.
`on_hit` is retained, unchanged, as the out-of-band observer hook `scoped()` propagates; only
`cache.py`'s class docstring, which described it as the column's route, is corrected. W15's §4
"door 2" is NOT used, so `53e5d8d`'s `worker.llm is ctx.model_client` was not relaxed and did not
need to be.

***CORRECTION to one word of round G lane W4's ruling above — the ALL-HIT verdict stands, its
combinator does not.*** W4 inverted W13's recommended `OR` to **`AND`** in `accumulate`. Measured
at the patch with `ast`, per enclosing function rather than by line proximity: **7** functions in
`src/fleet/` seed `usage = TokenUsage()` and **5** of those fold that seed with
`accumulate(usage, ...)` — `base.py::execute`, `buildgen.py::run`, `buildgen.py::_module_bazel`,
`prwriter.py::_compose`, `rewrite.py::run`. (`buildverify.py::run` and `prwriter.py::run` seed
without folding; the 7 and the 5 are different quantities and are not one number. Note also that
W4's own **7** above counts `accumulate(` CALL SITES, a third quantity again.) A boolean `AND`
over a fold whose seed is a hit of nothing answers `False` for **every** attempt those five
produce — so `AND` is not merely undesirable, it is unimplementable as a boolean, and `OR` is
any-hit, which W4 correctly refused. The fix keeps W4's semantics exactly and drops the boolean:
two summed counters have no identity element to poison, and the ALL-hit rule is derived once, in
`TokenUsage.all_served_from_llm_cache` = `lookups > 0 and hits == lookups`. The `lookups > 0`
half is load-bearing: `all()` over nothing is `True`, so without it every DETERMINISTIC rung and
every `--llm-cache off` run would self-report as fully cached. Recorded as ADR-0094.

***W4's "the cost of the ruling" paragraph stands unaltered and is now shipped.*** Partial hits
are still the normal case and the column will still read `0` most of the time; nothing here
softens that, and no consumer was added.

***What this annotation does not establish.*** The four remaining columns are untouched; no
suite-wide certification is claimed here (a sibling held the primary checkout's pytest session
while this was written); and `docs/SPEC.md` §11.2's "written in **one** transaction" claim about
six columns is still inaccurate for `integration_ref`, which W15 recorded and this change neither
worsens nor repairs.)*

*(2026-09-01, round Y task 4 — **the status FIELD moves `PARTLY ADDRESSED` -> `FIXED, LANDED
(f88e105)`; not one word above is rewritten, W11's, W13's, W15's, W4's and W2's markers
included.** The four columns round H lane W2's marker left open — `llm_backend`, `llm_failovers`,
`input_tokens`, `output_tokens` — are now written. Of this entry's originally-named five columns,
all five are closed: `llm_cache_hit` by ADR-0094 (round H), the remaining four by `f88e105`.*

***`llm_failovers` — new `TokenUsage` counter, stamped in `LadderModelClient.complete()`'s target
loop from the hop index at the point the call succeeded, summed by `accumulate` exactly like the
two ADR-0094 cache counters.*** No semantics ruling needed: §11.8's "backend hops spent inside
THIS attempt" is unambiguous once the field exists. This also closes the last code gap for
§12.43(i)'s literal fixture assertion (`attempts.llm_failovers = 1`) — a criterion this entry did
not previously name, found by round Y task 4's own re-verification of D62 against current `main`.

***`llm_backend` needed a ruling, not just wiring — recorded as ADR-0107.*** `accumulate` drops
`backend` by explicit, reasoned design (a ladder rung can answer from more than one tier across
several role-routed calls), so writing the column required saying which value wins when an
attempt's usage spans more than one backend. ADR-0107 rules **last-non-empty-wins**,
order-preserving over the fold's call order — the same shape round G lane W4's ALL-HIT ruling
took for `llm_cache_hit` before ADR-0094 could write it, smaller in scope (one field, no rejected
alternative needing its own investigation).

***`input_tokens`/`output_tokens` — pure wiring, exactly as W13's leg (a) measurement implied.***
`TokenUsage` already had both fields and `accumulate` already summed both; no ruling was needed,
only `AttemptRow`/`record_attempt`/`iter_attempts`/`cli.py`'s two `AttemptRow` sites and
`_AttemptWriter.record` (plus its two callers) naming them.

***`docs/SPEC.md`'s `TokenUsage` listing moved in the same commit as the code (`f88e105`)***, per
this project's "Documents Are Inputs to Future Edits" guardrail and the same discipline ADR-0094
already established for this exact listing — `llm_failovers` is now present in both places.

***What this annotation does not establish.*** `integration_ref` and `container_id` — the two
columns W13's `PRAGMA table_info` measurement found beyond this entry's own five, making the
class "declared and never nameable by `record_attempt`" seven rather than five — are **explicitly
untouched by `f88e105`** and are **not** part of this entry's own heading claim (which has always
named five columns, not seven). `integration_ref` is written by a separate `UPDATE`
(`cli._AttemptWriter._stamp_ref`) and was already reachable before this change; `container_id` has
no producer anywhere in `src/` and remains a distinct, unscoped gap — closing it is not this
entry's defect and this task deliberately did not touch either column (round Y task 4's own brief,
§7). Seven tests were added covering the four columns closed here
(`tests/test_llm_client.py`, the new `tests/test_llm_backend_failover_attribution.py`, and
`tests/test_runner.py`'s new local-profile e2e test) — no suite-wide certification beyond those
files is claimed here.)*

---

**D63 — FIXED, LANDED (`698f750`). `_unavailable`'s message text is false for every module it names: it tells the operator
each "still raises `NotImplementedError`" when none of them does.** Verified against `7a8bfbb`; **the
false string survives on `main` at `6a41840`, in `cli.py`'s `_unavailable` helper (`:905` at that
anchor; `def _unavailable` is `:910` and the string `:912` at `f9cb3f9`).**

    def _unavailable(verb: str, module: str) -> NoReturn:
        raise CommandUnavailableError(
            f"`fleet {verb}` cannot run: {module} still raises NotImplementedError. …"
        )

At `7a8bfbb` it had **five call sites** naming **four distinct modules** (`workers/relocate.py` ×2,
`workers/prwriter.py`, `workers/clone.py`, `workers/buildverify.py`). `git grep NotImplementedError
7a8bfbb -- src/` returned **four raises** — `manifests/base.py`, `rewrite/libcst_py.py`,
`rewrite/tsmorph.py` ×2 — **none in `workers/`**. All twelve workers are implemented and all of them
implement `preconditions_hold`. The same grep on `main` returns the same four raises, in the same
four non-`workers/` files.

**Consequence.** The message points a maintainer at a file to go implement, and the file is already
implemented; the real reason each verb refuses is elsewhere (missing driver assembly, not a missing
worker body). It also invites the reader to trust the string as evidence about `src/` — which the
round did, in its own briefs, until it was checked.

**Found by** R1, and it produced one of the round's durable rules: **never trust an `_unavailable`
string as evidence.** It is prose, and prose in a codebase ages.

**Would a test catch it? No.** Tests assert the exit code and the refusal, which are correct; no test
compares the message's factual claim against `src/`.

**Partly addressed, landed (`c45db53`) — two sites removed, three left, all still false.** RS1 built
`fleet resume`'s steps 3 and 7, so `resume` and `resume --repoll-prs` now refuse through a
`CommandUnavailableError` that names the genuinely missing piece (the per-phase `PhaseRunner`
assembly), not through `_unavailable`. **Re-measured on `main` at `6a41840`: three call sites remain —
`cli.py:2408`, `:2520` (both `workers/relocate.py`, in `plan` and `migrate`) and `:10958`
(`workers/buildverify.py`, in `stubs resolve`), i.e. two
distinct modules — and the false sentence in `_unavailable` is unchanged from `7a8bfbb`.**
Re-anchored at `f9cb3f9` (lane CITE, citations only — all three call sites are still there and D63
stays OPEN): `git show HEAD:src/fleet/cli.py | grep -n '_unavailable('` returns four lines — the
definition at `:910` and the three surviving calls at `:2415`, `:2527` and `:11293`. RS1 reported
this rather than rewording it — correctly, since the rewording is a `cli.py` edit three lanes were
contending for. **It needs an owner.**

**FIXED (round E, lane W5) — the heading above still reads OPEN because it records what was true
when it was written; this paragraph carries the status, as the `c45db53` paragraph above it does.**
Re-measured at `a69fba8` before the fix: three `_unavailable(` call sites (`plan` and `migrate`
naming `workers/relocate.py`, `stubs resolve` naming `workers/buildverify.py`), two distinct
modules — unchanged from the `6a41840` re-measurement, so nothing had rotted. `git grep -n
NotImplementedError -- src/` still returns the same four raises in the same three non-`workers/`
files (`manifests/base.py`, `rewrite/libcst_py.py`, `rewrite/tsmorph.py` ×2). The helper no longer
claims anything about the module it names: it says the verb has no implementation in the CLI, lists
the checks `_phase_preflight` actually ran (§9 config, §6 schema version, the run identity, §10
mirror mutex — the old text also claimed "budgets", which only `migrate` validates), and names the
module as a pointer.

**The entry's "Would a test catch it? No" was too kind, and that is the durable finding.** A test
did exist over the message — `tests/test_cli.py`, then named
`test_unimplemented_verb_names_the_stub_module` — and it asserted `"NotImplementedError" in
result.output`. It did not merely fail to catch the false claim; it *required* it, so the defect was
load-bearing for a green suite. The assertion is now inverted (`not in`) under the renamed
`test_unavailable_verb_names_a_module_without_calling_it_a_stub`.

**Residual, NOT fixed here (reported, not rewritten).** Two adjacent stale claims found by the same
sweep, both outside this entry's class: (1) `src/fleet/cli.py::_build_impl`'s docstring and
`src/fleet/workers/buildverify.py::on_cancel`'s docstring both state that `fleet resume` is
`_unavailable`. Only `on_cancel`'s docstring actually carries a line citation for that claim
(`cli.py:9792`) — `_build_impl`'s docstring makes the same claim with no citation at all, so "both
... cite `cli.py:9792`" was wrong when written, not merely rotted. The citation is also false at
the anchor this paragraph names: at `a69fba8` that line falls inside `_quarantine_impl`, nowhere
near `_unavailable`, and `resume` is by then a real command driving `_resume_impl`, not an
`_unavailable` call site at all. (2) the `stubs resolve` call site
passes `"src/fleet/workers/buildverify.py (revalidation round)"`, and `grep -c revalidat
src/fleet/workers/buildverify.py` returns **0** — the parenthetical points at a round that file does
not implement. Both need an owner and a decision this lane could not settle from a primary source.

> **Editorial correction (2026-08-21), lane W13 — the heading above now reads `FIXED, LANDED
> (`698f750`)`, not `OPEN`.** The "FIXED (round E, lane W5)" paragraph above argues the heading
> should stay `OPEN` because a heading "records what was true when it was written"; a review (CR3)
> measured that this file's own "Status vocabulary, used strictly" block defines `OPEN` as
> "nothing fixes it," found 15 precedents elsewhere in this document of a heading changing when its
> entry was fixed, and found that D54 — the very precedent W5 cited for leaving a heading alone —
> had in fact changed its own heading on fix (`FIXED, LANDED (`c45db53`)`). The heading is a status
> **field**, governed by that vocabulary, and is updated on fix; the annotate-never-rewrite rule
> protects an entry's body, not its heading. This paragraph's body is left exactly as its author
> wrote it, per this file's own convention: an entry records what was true at its own commit; a
> dated marker beside it records what changed.

---

**D64 — FIXED, LANDED (`433dd55`). The required-target-field gate tested `is None`, so `base_url: ''`,
a whitespace-only `base_url`, and `region: ''` all passed startup — violating §13 row 36's "fail at
startup naming profile/tier/index/field".** Verified against `7a8bfbb`; fix verified on `main` at
`6a41840`.

At `7a8bfbb`: `for required in _REQUIRED_TARGET_FIELDS.get(target.backend, ()): if getattr(target,
required) is None: raise ConfigValidationError(...)`. An empty string is not `None`, so it satisfied
the gate. The blank value then travelled to wave 7, where a transport builds a client against an
empty base URL — the exact "fail late instead of at startup" outcome row 36 exists to prevent.
`_REQUIRED_TARGET_FIELDS` drives **both** `base_url` and `region` from one table, so the same hole
covered `region` for the (then unbuilt) `bedrock`/`vertex` transports — which Round B then built.

**Found by** the CR1/BK2 pre-flight review as a minor, deferred because `settings.py` was under
contention, then routed to CLEAN1 — and **reproduced before being fixed**: `''`, `'   '` and a valid
URL all loaded clean against pre-fix `settings.py`. The `region` half was handed over by BK3 from the
other side, and CLEAN1 **verified rather than assumed** that round 1's shared-loop edit had already
closed it: against `7a8bfbb` both blanks load clean; at the fixed tip both refuse.

**Would a test catch it? No** — `tests/test_settings.py` had no blank-string case for either field;
the round added them, and confirmed the four behaviour-changing assertions **fail against pre-fix
`settings.py`** rather than inferring it.

**Fix.** `if value is None or (isinstance(value, str) and not value.strip())`, reusing the existing
message and `key` shape verbatim so the error's §13-row-36 form is unchanged. `git log -S"isinstance(value, str)" 7a8bfbb..main -- src/`
returns `433dd55` and nothing else.

---

**D65 — FIXED, LANDED (`a9afe9d`). A markdown blockquote inverted a normative SPEC rule, and had
since commit `a1178f7`.** Verified against `7a8bfbb` and reproduced in `a1178f7`'s own tree; fix
verified on `main` at `6a41840`.

At `7a8bfbb`, `docs/SPEC.md:474-477`, inside numbered step 6 of the plan phase:

    demand, never persisted (ADR-0004). `strongly_connected_components` → any component of size
    > 1 becomes a `CycleFinding` with a proposed break edge chosen deterministically: lowest
    `confidence`, tie-broken by fewest transitive dependents, tie-broken by `edge_id`. The
    condensation graph is what gets sorted.

The `>` at line-start of `:475` (line-initial within the list item's content block) opened a
**blockquote**, and lazy continuation absorbed `:476` and `:477` into it.

**Consequence — the rule was inverted, not merely mis-rendered.** The `> 1` threshold was consumed as
blockquote syntax, so `:474` ended bare at *"any component of size"* and the orphaned text read
**"size 1 becomes a `CycleFinding`"** — i.e. every single-node component, which is every node. The
tie-break ordering, a normative determinism requirement, rendered as a quotation, i.e. as something
non-authoritative.

**Found by** BK2 while sweeping `DECISIONS.md` for a defect of its own, and confirmed independently
by that lane's re-reviewer: an independent parser sweep found `SPEC.md` had 4 blockquotes and **2
absorptions** — which also corrected BK2's own summary sentence claiming it had swept both docs to
zero.

**Would a test catch it? No.** Nothing in the suite renders the SPEC. The defect is invisible in the
source, which is why the round's instruction was **verify by rendering, not by reading** — and why
every lane touching it was required to validate its parser against the known-bad state first.

**Fix, and the constraint on it.** CLEAN1 rewrapped the line so `>` is not line-initial — **identical
words, identical order**, proven by `old.split() == new.split()` → `True`. That property is what a
normative rule requires: a silent reword there would have changed the rule while appearing to fix
formatting. Render checked pre- and post-fix (`size 1` → `size > 1`), and all 26 `docs/*.md` sweep to
0 absorptions. **On `main`, `docs/SPEC.md:479` now carries "any component of size > 1" on a single
line.**

---

**D66 — FIXED, LANDED (`87884d7`, with the marker rounds `7ecc898`, `2c6e89f`, `eabfcdb`). A false
claim about model behaviour was the stated premise for a different subsystem's determinism argument,
in the SPEC and in two `src/` comments.** Verified against `7a8bfbb`; fix verified on `main` at
`6a41840`.

Five pre-existing sites asserted that the harness relies on "adaptive thinking", and two of them used
it as a *premise*:

- `docs/SPEC.md:3268` and its `src/` mirror `models/tasks.py:478` — *"…a re-run reproducible, since
  adaptive thinking forbids temperature pinning (§11.6)"*
- `src/fleet/llm/cache.py:4` — the same clause, at the top of the cache module
- `docs/SPEC.md:5589` — the `anthropic` backend row: *"adaptive thinking + `effort` where the
  target's declared capabilities carry it"*
- plus `SPEC.md:6810`, `:7073` and `DECISIONS.md:290` (ADR-0009, the root the SPEC row derived from)

**Neither behaviour exists.** No `thinking` key is constructed anywhere; `git grep -i thinking`
against the backend lane hits only prose — and that still holds on `main` now that all four adapters
are landed. And `ModelCapabilities` has **no `effort` field**, so "where the target's declared
capabilities carry it" described a consultation the type system makes impossible.

**Consequence, and it is the reason this is a defect rather than a stale sentence.** The claim was
**load-bearing for the LLM cache's determinism rationale** — the argument for why a cached reply is
safe to replay rested on a model behaviour that does not exist. Separately, `SPEC.md:5589`'s
capabilities clause was a live trap: a maintainer reconciling code to spec adds `supports_effort` to
`ModelCapabilities`, which then **suppresses an explicitly declared `effort: high`** and re-keys the
cache.

**Found by** BK1 in its round-1 report — it flagged that §7.7's table describes a capability lookup
the model cannot express — and then **the round's own handling is the instructive part, recorded here
because it is a ledger-worthy lesson**: the orchestrator adjudicated the *implementation* question
and never corrected the *SPEC sentence that raised it*, so the false claim stood for the whole round
until a re-reviewer found it again from the other end. **When a worker reports "the SPEC says X but
the code cannot do X", deciding the code is half the adjudication; the sentence regenerates the
defect otherwise.**

**Would a test catch it? No.** Nothing in the suite asserts a negative about a wire payload's keys.

**Fix, one thing it deliberately did not do, and one site it missed.** All five sites corrected
including both `src/` mirrors; on `main`, `docs/SPEC.md:5671` now carries the `anthropic` row with an
explicit guard — *"`ModelCapabilities` has no effort field, and must not grow one to make code match
this table"*. `ADR-0009`'s original decision paragraph is **preserved** — an ADR records history —
with a scoped, unmissable *NOT IMPLEMENTED — do not reconcile code to this paragraph* marker naming
each false claim. **The determinism conclusion survives:** the re-reviewer was asked explicitly
whether removing the premise orphaned it, and the argument's second leg (a role may be answered by a
different backend next call, and `backend`/`model_id` are key components) is code-backed and
independent. The modality weakens from "cannot pin" to "does not pin". **Removing a false premise
without checking what rested on it is how a correct conclusion gets orphaned** — that check was worth
running. **The miss, recorded rather than smoothed over:** a sixth carrier of the same clause,
`tests/test_llm_cache.py:4`, was outside every lane's file set and **is still on `main`, unamended**,
asserting that "adaptive thinking forbids pinning a temperature" in the docstring of the very module
whose determinism argument the fix corrected. The five-site enumeration was complete for `docs/` and
`src/`; it was never a claim about `tests/`.

> **Editorial correction (2026-08-20).** The sixth carrier above is no longer on `main`, unamended.
> `c7f72c6` rewrote `tests/test_llm_cache.py:4`'s docstring to state the retraction itself — "the
> harness pins no sampling controls — no `temperature`, `seed`, `top_p` or `thinking` key is built
> anywhere under `src/fleet/llm/`" — rather than the false premise. **Re-measured this session**
> with a whitespace-normalized whole-file scan (offset-to-line map, not line-oriented `grep`, per
> three prior line-break-spanning misses this round) over `src/` and `tests/`: `grep -ni thinking
> src/ tests/` returns **one** hit, not zero, and that one hit **is** this corrected line — a
> negative assertion necessarily names the word it denies. A later report
> (`docs/superpowers/plans/task-item17-report.md`, promoted out of `.superpowers/` scratch at
> `5be5064`) claimed the sweep left **zero** hits; that
> claim was itself false the moment it was written, for the same reason. One hit, correctly present,
> is the sweep's finished state, not its outstanding state.

---

**D67 — DESIGN DEFECT IN THE SPEC, CORRECTED AND LANDED (`d0b1150`); the replacement predicate is
still unbuilt. `fleet resume` step 5's documented algorithm, implemented exactly as written, would
have promoted never-built repos to Phase 4.** Verified against `7a8bfbb`; correction verified on
`main` at `6a41840`.

At `7a8bfbb`, `docs/SPEC.md:180-182` (Constraint 7) read: *"before re-entering a phase, `runner.py`
re-checks the phase's declared preconditions (below) against SQLite; a failed precondition demotes the
repo to the earliest phase whose precondition holds."* `SPEC.md:6777` said the same. Taken literally —
walk phases ascending, stop at the earliest whose precondition holds — the walk stops too early in
the wrong direction, because `BaseWorker.preconditions_hold` is not a durable-evidence predicate.
`workers/rdepverify.py:199-203`:

    row = await ctx.db.get_phase(str(ctx.run_id), ctx.repo_id, Phase.BUILD)
    if row is None:
        # No BUILD row at all is a first admission by the runner, not evidence of a failure;
        # refusing here would deadlock a fresh run on a row nothing has written yet.
        return True
    return row.status is RepoStatus.SUCCEEDED

That `return True` is **correct at its own call site** — a precondition gate must admit a fresh repo —
and **wrong as a resume predicate**: a repo that has never been built has no BUILD row, so
`preconditions_hold` returns `True` and an ascending walk marks it ready for Phase 4.
`orchestrator/runner.py:214` says so outright: *"Neither verdict ever means 'skip the work'."*

**Consequence, stated at the right strength.** No such resume was ever shipped — step 5 is
unimplemented on `main` and was on every lane tip. What was shipped is the **instruction to build it
that way**, in the document a future implementer reconciles against; the round propagated that
phrasing into its own task brief before catching it. Recorded here because the ledger's subject is
latent defects in shipped artifacts, and a normative SPEC sentence is a shipped artifact.

**Found by** R3, doing design-only scoping of step 5 — before any code was written. The round ledger
calls it "the save".

**Fix, and its limits.** `d0b1150` rewrote Constraint 7 to *"re-checks each phase's durable evidence …
and demotes the repo to the earliest phase whose **evidence** still holds"*, naming **two distinct
predicates, not one** (ADR-0077 §6): a durable, payload-free `evidence_holds` for step 5's search,
with `BaseWorker.preconditions_hold` left at its single existing call site. **`evidence_holds` does
not exist on `main`** — it is subtask 5 of a ten-subtask decomposition
(`docs/superpowers/plans/design-resume-step5.md` §5). The SPEC no longer instructs the wrong
algorithm; the right one is unbuilt.

---

**D68 — CONTRADICTION BETWEEN SPEC AND CODE; gate BUILT AND LANDED (`791b428`, `ea9ee57`) — and it
has no caller. `resume`'s documented demotion was mechanically forbidden by the status machine.**
Verified against `7a8bfbb`; gate and its no-caller status verified on `main` at `6a41840`.

`SPEC.md` mandated demoting a repo to an earlier phase on resume. `models/enums.py` had
`RepoStatus.SUCCEEDED: frozenset(),` (now `:47` on `main`), and the trailing comment states the intent
as a virtue: *"Terminal statuses map to the EMPTY set, which is what makes them terminal mechanically
rather than by prose: no crash sweep can resurrect an abandoned repo into RUNNING."* `transition()`
consults `ALLOWED_TRANSITIONS`, so writing `PENDING` over a `SUCCEEDED` phase row raises. **The SPEC
required a write the code refused.** The two had coexisted because nothing performs the write.

**Found by** R3, in the same design pass as D67 — recorded there as "demotion is literally unwritable
today".

**Would a test catch it? No.** Both halves are individually correct and individually tested; the
contradiction lives between them, and no test asserts that the SPEC's demotion is performable.

**Addressed, landed in TWO rounds — and this paragraph asserted completeness after the first.**
The gate and its limit are as described below; what was wrong here was the SPEC half, corrected in
`f02d124` and restated in the closing paragraph of this entry.
`enums.py:61` adds a `RESUME_DEMOTE` map consulted only under a keyword-only `resume=True` (`:136`),
mirroring the existing `OPERATOR_REOPEN` precedent axis-for-axis, plus `demote()` (`:141`) which is
strictly stricter than `transition()` and returns a `PhaseDemotion` audit record with the status.
**`git grep "demote(" -- src/` outside `enums.py` is empty on `main`** — the gate ships with **no
caller**, exactly like `OPERATOR_REOPEN` before it. That is correct for a gate whose consumers are six
subtasks away, and it **must not be read as shipped capability**: any statement that "resume can
demote" is false on `main` today. The change also carries an honest limit —
`transition(..., resume=True)` remains public and would demote *silently*, so `SPEC.md` names
`demote()` and says so — at first in **§11.5 step 5 only** (`e5b8b11`), and that scoping is the half
of this record that was wrong. `d0b1150` had rewritten §11.5 step 5 **and** the §3 Constraint 7
bullet together; `e5b8b11` swept only the first, so Constraint 7 (`SPEC.md:186`) went on naming
`transition(..., resume=True)` as the demotion write *and* asserting that path "emits a
`PhaseDemoted` finding", which it has never done — in the paragraph the demotion writer's author
reads first. Corrected in **`f02d124`** (CR1 C-1); ADR-0077 §4 item 1 now names both paragraphs so
the remedy cannot be re-narrowed to one. The test that claimed the door was closed was **deleted and
replaced** by one that pins the door open.

---

**D69 — OPEN (SPEC half, and the duplicate encoding). `fleet stubs abandon` writes a `findings.kind`
that exists in no enum and in no SPEC section.** Verified against `7a8bfbb`; **re-verified on `main` at
`6a41840`**.

`git grep "StubAbandoned" 7a8bfbb` returned **exactly one hit in the entire repository**:
`src/fleet/cli.py:10502` — inside `stubs_abandon()`, `:11350` at `f9cb3f9` — a bare string literal
inside a raw-SQL `INSERT INTO findings … VALUES (?, ?,
'StubAbandoned', 'warn', ?, ?, ?)`. There was no enum member, no model, and **no mention in
`docs/SPEC.md`**. The surrounding block also encoded the ABANDONED state transition in raw SQL, a
second encoding of a rule that belongs to the stub state machine.

**Consequence.** A finding kind that appears in one SQL string literal cannot be reconciled against
anything: a consumer filtering `findings` by kind has no list to filter against, and an implementer
writing the stub state machine from the SPEC will not produce this kind — which is precisely what
happened. ST1, building `orchestrator/stubs.py` from the SPEC, first emitted `UnresolvedStub` for an
operator abandon. **Two encodings of one event, disagreeing on kind**, which would have reported
deliberately-abandoned stubs as ones the fleet failed to resolve.

**Found by** the ST1 reviewer, and it is worth noting the adjudication that produced it: ST1's own
report classified a *different* mismatch as "pre-existing" and the reviewer checked and found it **was
not** — that error was the root of the lane's one Critical. **An implementer's "pre-existing defect"
claim is exactly the claim a reviewer must check**, in both directions; this entry is the case where
the claim held.

**Half addressed, landed (`5c52ee5`).** `stubs.py:145` adds `STUB_ABANDONED = "StubAbandoned"` to
`StubFinding` and the abandon path emits it, so the new state machine matches `cli.py`'s committed
kind rather than inventing a rival — verified on `main`, where the string now appears in both files.
**Still open, and re-verified on `main`:** `docs/SPEC.md` does not name the kind, and the raw-SQL
abandon block in `cli.stubs_abandon()` remains a second encoding of a rule `stubs.py` now owns —
which nothing yet calls
(`git grep -l "orchestrator.stubs" -- src/` returns only the module itself). Neither is in that lane's
scope.

*(Anchor repair, lane CITE 2026-08-20, citations only — D69 stays OPEN.* The abandon block was
cited as `cli.py:11015`; at `6a41840` that line is the finding write
(`"VALUES (?, ?, 'StubAbandoned', 'warn', ?, ?, ?) "`), **not** the state transition, which is
`"UPDATE stubs SET state = 'ABANDONED', …"` nine lines above at `:11006` — so the original number
named the wrong half of the block it meant. At `f9cb3f9` the two are `:11345` and `:11350`, both
inside `stubs_abandon()`, which is the durable anchor and is what this entry now uses.*)

> **Editorial correction (2026-09-08, research-47).** The "Still open" paragraph above says
> *"`docs/SPEC.md` does not name the kind"* — that half is now stale: `docs/SPEC.md:4403` lists
> `'StubAbandoned'` in the `findings.kind` enumeration comment. D69 stays OPEN regardless — the
> entry's other, load-bearing claim is unaffected: `cli.stubs_abandon()`'s raw-SQL state-transition
> block is still a second encoding of a rule `orchestrator/stubs.py` now owns, and nothing calls
> `orchestrator.stubs` from that path yet.

---

**D70 — OPEN, low severity, recorded because the shape is this document's subject. `ruff format
--check` has never passed on `src/fleet/cli.py`, the green suite does not run it, and landing moved
the count.** Measured directly for this entry, twice.

`ruff format --diff src/fleet/cli.py` against `main` at `7a8bfbb` reported **58 hunks**. The same
command against `main` at `6a41840` reports **60**. The full-suite baseline on the first tree is green
(1305 passed) and on the second is green (1575 passed), so formatting is not a suite gate on either.

**Found by** RS1, which hit the failure while running its own checks and reported it as pre-existing,
verified via `git stash`. **The correction its reviewer made is the part worth keeping**, and it
belongs in this ledger more than the defect does: RS1 reported the failure as "identical"
pre-existing; the reviewer **measured** it — 58 hunks at base, **59** at RS1's mid-round commit, 58
after RS1's fix. Pre-existence stood; "identical" did not. **Verifying that a failure exists before
and after is not verifying it is unchanged** — and the 58 → 60 move across the landing is that same
lesson arriving a second time, on the same file, at the whole-round scale.

**Severity: hygiene, not correctness.** Nothing behaves wrongly. It is recorded because a green suite
that does not run a check the project's own toolchain provides is exactly the "we have not tested
this" → "we have tested this" conversion this document exists to refuse, and because the next person
to add `ruff format` to CI will find 60 hunks in the most-contended file in the repo.

---

**A stale disclosure, CLOSED by `6e0a5fa`. This entry claims no D-number** — it retires a limit
another lane disclosed rather than recording a new defect, so the precedent set above (an entry
contributed with no number, left un-numbered because renumbering a landed entry is a separate edit)
applies. **D71 remains the next free number.**

`docs/superpowers/plans/task-item17-report.md` §2 and §5 item 1 disclose, honestly and at length,
that the binding `c7f72c6` added for ADR-0075's `effort` annotation "does **not** detect the literal
defect this item reported" — its mutation M1 deletes the two annotation lines from `schema.sql`
again and the file stays green — and conclude that "the next drift of the comment alone will still
be silent". §5 item 1 argues the only honest mechanism would be a whole-fence extractor, which the
same report measures as firing on **22** pre-existing intentional condensations between the SPEC
listing and the real schema, and therefore rejects as a project of its own.

**That is no longer true.** `6e0a5fa` added
`test_the_effort_column_carries_its_adr_0075_annotation_in_both_copies` to
`tests/test_llm_cache.py`. It normalises `--` markers and whitespace away and compares **only the
`effort` column's own comment region** in the two files — a region the report's own measurement puts
outside all 22 condensations — asserting that both cite ADR-0075 and that the two regions carry the
same words. The report's stated dichotomy (whole fence, or nothing) had a third term.

**Measured this session, not inherited**, with the detector-validation rule this document exists to
enforce — three checks, not one, each proven to have really changed its file (`git diff --numstat`)
before its result was read:

| probe | file changed | outcome |
|---|---|---|
| baseline, clean tree | n/a | **1 passed** |
| known-bad: the report's own M1, annotation deleted from `schema.sql` | `1 2` | **1 failed** |
| synthetic fault in an otherwise-untouched file: `docs/SPEC.md` copy reworded only (`needs` → `requires`) | `1 1` | **1 failed** |
| pure reflow of the SPEC copy across three lines, same words | `3 2` | **1 passed** |

The third row is the one that matters: it perturbs the file the fix did not target, so a detector
silently broken on the mirror copy could not have passed it. The fourth confirms the instrument is
not merely a checksum.

**What is still not caught, stated so this closure does not become the next stale disclosure.** The
marker binds only the `effort` column. Every other comment in the two listings — including the
`findings.kind` CAVEAT corrected in `60d400b`, which exists in both files and drifted for exactly
this reason — has no binding at all, and the 22-condensation measurement is why a general one is
still unbuilt. The semantic assertions `c7f72c6` added are untouched and still bind what the marker
cannot: a `CHECK` arriving without the comment changing.

---

**A fresh overclaim, landed knowingly in `60d400b`/`e0404b0` and disclosed rather than silently
narrowed. This entry claims no D-number** — it discloses a defect in prose this session applied, and
the correction is one edit across five sites that must stay identical, which is not this lane's to
make unilaterally. **D71 remains the next free number.**

`60d400b` fixed the re-entry-floor quantifier ("earliest" → "highest") across `docs/SPEC.md`
Constraint 7 and §11.5 step 5, `src/fleet/state/schema.sql`'s SPEC mirror and `docs/DECISIONS.md`
ADR-0076 §1, applying an upstream lane's wording verbatim so that five sites state one rule in one
author's words and cannot drift apart again. That wording ends with a fallback clause: *"and `SCAN`
if no phase below the frontier holds."*

**The fallback clause is false whenever a phase below the frontier is `DEGRADED` or `SKIPPED`.**
`phase_floor` (`src/fleet/orchestrator/reentry.py:96-103`) breaks on `_HARD_STOPS` —
`{DEGRADED, SKIPPED}` — *before* it consults `evidence`, so the walk stops above such a row and the
floor never descends past it, however little evidence holds. `phase_floor`'s own docstring states
that rule outright, on ADR-0077 §5 (`src/fleet/orchestrator/reentry.py:80-81` — cited, not quoted;
see editorial point 4 below); the new prose does not.

**Measured, not reasoned** — `phase_floor` called directly under `.venv/bin/python` with the test
module's own `_row` builder, `evidence = {}` (nothing holds) in every case:

| rows below a `PENDING` `VERIFY` frontier | returned floor |
|---|---|
| `SCAN`/`TRANSFORM`/`BUILD` all `SUCCEEDED` | `SCAN` — the clause is right here |
| same, but `BUILD` `DEGRADED` | **`VERIFY`**, not `SCAN` |
| same, but `TRANSFORM` `SKIPPED` | **`BUILD`**, not `SCAN` |

**Why it is recorded here instead of patched.** This is the shape CLAUDE.md guardrail 6 predicts —
the fix for an overclaim introducing a narrower successor of itself — arriving for the fifth time
this round, and caught only because the re-sweep was run against the applied text rather than
against the tree it replaced. Patching it at some sites and not others would restore precisely the
drift the single-author wording exists to prevent, and the brief that carried the wording forbade
re-authoring it. The correction is one edit, made at all five sites at once, by whoever owns the
canonical sentence: the hard-stop rows bound the walk before evidence is consulted, so `SCAN` is
the floor only when no phase below the frontier holds **and** none is `DEGRADED` or `SKIPPED`.

**Severity: the sentence is wrong in a case the code handles correctly.** No behaviour changed; the
suite is unaffected (`tests/test_reentry_floor.py` binds the real rule in both directions). The risk
is the one this whole class has: a reconciler follows the prose.

> **Editorial correction (2026-08-20), lane BIND — this disclosure is now stale, and its site
> enumeration was wrong when written. The history above stands; only what is still true changes.**
>
> **1. The overclaim is closed.** `f466287` made the edit this entry says was "not this lane's to
> make unilaterally", writing one corrected clause and applying it verbatim to every site that
> states the rule. Read back this session: `docs/SPEC.md:182`, `docs/SPEC.md:6952` and
> `docs/DECISIONS.md:6713` each carry the identical 336-character clause once whitespace is
> normalised — measured, not asserted. So the paragraph above beginning "Why it is recorded here
> instead of patched" describes a state that no longer exists.
>
> **2. It is closed by a mechanism, not by another hand-maintained agreement** — which is what this
> entry, and the five wrong-successor corrections it counts, argued was needed.
> `tests/test_floor_rule_statements.py` binds the three prose copies to each other by normalised
> text, parses the hard-stop status set, the cited symbol and the fallback phase **out of the prose
> itself** and checks each against `orchestrator/reentry`, and binds the `enums.py` paraphrase by a
> vocabulary whitelist plus that same parsed-out set. Its residual is recorded in the file's own
> `_RESIDUAL` and is not claimed closed here.
>
> **3. The site enumeration above is wrong on two counts, corrected rather than deleted.** It reads
> that `60d400b` fixed the quantifier "across `docs/SPEC.md` Constraint 7 and §11.5 step 5,
> `src/fleet/state/schema.sql`'s SPEC mirror and `docs/DECISIONS.md` ADR-0076 §1 … so that five
> sites state one rule". Measured this session: `60d400b --name-only` touched **`docs/SPEC.md` and
> `src/fleet/state/schema.sql` only** — ADR-0076 §1 was corrected later, by `e0404b0`; and
> `src/fleet/state/schema.sql` **has never contained a statement of the floor rule at all**
> (`grep -ci "re-entry floor\|settled frontier\|floor"` ⇒ 0 today, and
> `git log -S"floor" -- src/fleet/state/schema.sql` over all history returns no commit). The
> schema half of `60d400b` was the `findings.kind` CAVEAT, co-shipped in the same commit; the
> "five sites" figure was that commit's own count across **both** subjects and was carried into
> this paragraph as if it counted floor-rule sites. The true count is **four statements** — three
> prose (`docs/SPEC.md` ×2, `docs/DECISIONS.md` ×1) and one code paraphrase
> (`src/fleet/models/enums.py`), which is exactly the set the new test binds.
>
> **4. The `reentry.py` reference two paragraphs above was a verbatim quotation until 2026-08-20
> (lane FIX7); it is now a file:line citation.** The change is CLAUDE.md §3 hygiene — never quote
> another module's text, because nothing enforces the copy and one reword leaves the quotation
> pointing at a string no longer in the tree. It is **not** a correction of a false quotation, and
> is recorded because `review-7` §3.3 reported the quoted sentence as absent from `reentry.py`.
> Measured at `431b02f`, whitespace-normalised: it occurs at `reentry.py:80-81`, in the
> `phase_floor` docstring this entry named, differing from the quotation only in its leading
> capital. The review measured the **module** docstring instead, which states the same rule in
> different words — the same paraphrase axis this entry's own class keeps being missed on.
>
> **What is still open, verified here rather than inherited from ADR-0076's assertion of it.**
> `git show HEAD:src/fleet/cli.py | grep -n` finds the pre-`d0b1150` phrasing alive in two
> operator-facing messages — the `ResumeIncompleteError` raised by `resume()` (*"demote each repo to
> the earliest phase whose …"*) and the `UsageError` raised by `_refuse_unbuilt_resume_flags()`
> (*"(re-check preconditions and demote to the earliest phase whose precondition holds)"*) — so both
> carry the retracted **predicate** and the wrong **quantifier**. Both were cited by line as
> `:10043` and `:10274`; **re-verified STILL PRESENT and STILL OPEN at `f9cb3f9`** by lane CITE with
> `git show HEAD:src/fleet/cli.py | grep -n "earliest phase whose"`, which now returns `:10078` and
> `:10390`. The symbols above are the durable anchor; the numbers will move again. That file is owned by
> another lane and had uncommitted edits in the tree while this was written, so it is reported and
> not touched. It is deliberately outside the new test's `_EXPECTED_SITES`: the census anchor keys
> on the corrected wording, so a fifth carrier phrased the old way is not detected by it. That is a
> gap in coverage, stated as one. **D71 remains the next free number** (highest allocated: D70).

> **Editorial correction (2026-08-20).** The two sites above are fixed, not "STILL PRESENT and
> STILL OPEN". `da70221` rewrote both operator-facing messages; `git show HEAD:src/fleet/cli.py |
> grep -n "earliest phase whose precondition holds"` and `git show HEAD:src/fleet/cli.py | grep -n
> "twelve workers"` (committed tree, not the working tree — a sibling lane has further uncommitted
> edits to this file right now) each return no match. `e784573` binds the replacement text:
> `tests/test_cli.py:1269` and `:2149` each assert `"HIGHEST phase below the settled frontier" in
> result.output`. **Scope, stated precisely: only these two cited sites are verified fixed here.**
> The `_EXPECTED_SITES` census-gap sentence above is untouched and still true — the two
> `cli.py` sites remain outside the new test's census. And a *different* string in the same
> `ResumeIncompleteError` message — the clause naming step 2 as "absent too", now false because the
> orphan reap it describes runs before the refusal — is not addressed by `da70221` and is not
> claimed fixed here; it is a separate, currently open defect in the same message.

> **Editorial correction (2026-08-21), lane W2 — two `reentry.py` line citations in this entry no
> longer resolve; the symbols they meant are given here. The entry's history and its verdict stand
> unamended.** (a) The paragraph beginning "**The fallback clause is false…**" cites
> `src/fleet/orchestrator/reentry.py:96-103` for the `_HARD_STOPS` break. That range was correct
> when written; `1e857f3` then grew `phase_floor`'s docstring by nine lines, and at `main` it lands
> on the *frontier* loop (`_SETTLED_FOR_DEMOTION`) instead — a reconciler following it reads the
> wrong predicate. The durable anchor is **`phase_floor`'s `break` on
> `orchestrator/reentry._HARD_STOPS`**, which is still tested before `evidence` is consulted. (b)
> The same paragraph's `src/fleet/orchestrator/reentry.py:80-81` for the docstring sentence stating
> the rule was moved by the **same commit, `1e857f3`** — named rather than inherited by adjacency:
> the sentence sat at `:80` at `431b02f` and at `:89` after `1e857f3`, and a later docstring growth
> has since carried it to `:103`, so it has now rotted twice. The anchor is the sentence in
> **`phase_floor`'s docstring** that states where the backward search stops on a hard-stop row —
> named, not quoted, because nothing enforces a copy. Editorial point 4 above cites the
> same `:80-81` **at `431b02f`**, where it is correct and was measured — that citation is anchored to
> its ref and is **not** corrected here. Nothing in this entry's measured table or its verdict
> depends on either line number.

---

### D71 — UNUSED. Allocated in error and never written; the number is free for allocation

Not a defect record: no code, no test, and no failed run is filed under D71. The number is
mentioned four times above (`:3267`, `:4008`, `:4052`, `:4139`), every time as "the next free
number," never as a heading that opens an entry — this document's convention for a real record is
a line beginning `**D<n> —` or `### D<n> —`. Before this placeholder existed, `grep -n
'^\*\*D71\b\|^### D71\b'` returned nothing; **it does not return nothing now.** This entry's own
heading is written in exactly that form, so the anchored detector — correctly — matches it, and
returns exactly one hit: the heading line immediately above. That is not a mistake to fix by rewording the heading;
the point of filing this placeholder was to make D71's status discoverable by the same convention
a real record uses, and a detector that stayed silent on a filed placeholder would be the broken
one. Read the single hit as "one placeholder, zero defect entries" rather than "zero entries": the
number is still unassigned to any code, test, or failed run — only reserved by this notice. The
gap exists because D72 and D73 (the
section immediately following) were allocated centrally under the mistaken belief that D70 was not
the highest pre-existing number; those two numbers are committed and cited elsewhere, so
renumbering them down to close this gap would break live citations to fix an appearance, and is
not done. This entry exists instead — the same choice made for three unwritten ADR numbers earlier
today: a short, clearly-marked placeholder rather than a silent renumber (`docs/DECISIONS.md`
ADR-0079–0081, `f76da41`). It marks the distinction a reader cannot otherwise make from a bare gap:
**free is not deleted.** D71 remains open for the next lane that needs a number.

> **Editorial correction (2026-08-21), lane W2 — this placeholder's own four citations have rotted,
> and the high-water mark beside the fourth is stale. The entry above is left exactly as its author
> wrote it, including the four numbers; its verdict is unchanged and correct: D71 is free.** The
> body cites `:3267`, `:4008`, `:4052`, `:4139`. Re-measured this session with a
> whitespace-normalised whole-file scan and an offset-to-line map, the four sentences are at
> `:3323`, `:4119`, `:4163`, `:4254` at `b4567a5` — off by +56, +111, +111, +115. All four texts
> still exist and still say what the entry says they say; only the anchors moved, under section
> growth in this same file. **The durable anchor for them is their own sentence**, "D71 remains/is
> the next free number", because a register mention has no symbol to name and a sentence is the
> nearest thing to one; read the body's four numbers as the measurement its author took, and this
> sentence as the way to find them again.
>
> *(An earlier version of this marker edited those four numbers **out of the entry body** and put
> the correction here — a rewrite, not an annotation, and inconsistent with the very next paragraph,
> which declines to touch the neighbouring sentence on the opposite reasoning. The body is restored
> verbatim. One convention, applied one way: an entry records what was true at its own commit; a
> dated marker beside it records what changed.)*
>
> Separately, the fourth site says *"(highest allocated: D70)"*. That was true when written and is
> not now. Counted by this file's own entry syntax (`^\*\*D<n> —` / `^### D<n> —`), headings exist
> for every number D1–D75 with no gaps — but **"present" is not "allocated"**, and D71 is precisely
> the case that separates them: its heading matches the syntax while its body disclaims any
> allocation. So the highest number **allocated to a defect** is **D75**, this session's own new
> entry below, and D71 remains free. **That parenthetical is a hand-asserted high-water mark and it
> has now rotted once; re-measure it with the predicate above, and subtract placeholders by reading
> them, rather than inheriting the figure.** The `(highest allocated: D70)` sentence itself is left
> as written: it sits inside a dated editorial correction of its own and is a record of what was
> true at that commit.

---

## D72–D73 — §11.5 step 2's two halves: one sweeping an empty namespace, one that could not report its own failure

Both numbers were allocated centrally at dispatch (CLAUDE.md §3). **D71 was never allocated and
remains free**: the disclosures at `:4008`, `:4052` and `:4139` each explicitly decline a number
and say so, and this section does not quietly close that gap by taking it — the brief that
dispatched this work believed D71 was the highest pre-existing number, and it was D70. Highest allocated after this
section: **D73**.

> **Editorial correction (2026-08-21), lane W10 — this entry's three citations have rotted. The
> entry above is left exactly as its author wrote it, including the three numbers; its verdict is
> unchanged and correct: D71 was never allocated and remains free.** The body cites `:4008`,
> `:4052` and `:4139`. Re-measured with a whitespace-normalised whole-file scan and an
> offset-to-line map (predicate `D71 (remains|is) the next free number`, case-insensitive,
> leading `>` stripped per line), those three disclosures are at `:4124`, `:4168` and `:4259` at
> `31484d5`. All three texts still exist and still decline a number, exactly as the entry says;
> only the anchors moved, under growth in this same file. **The durable anchor for them is their
> own sentence**, "D71 remains/is the next free number" — the same anchor the D71 marker above
> names, because a register mention has no symbol and a sentence is the nearest thing to one. This
> is the second time the class has rotted: these three numbers are the *same measurement* the D71
> entry body took, copied into a second entry, so correcting one copy was never going to be
> enough. A sweep for that sentence finds every copy; a sweep for the numbers finds none of them.
>
> *"Highest allocated after this section: **D73**"* is **not** corrected and is not a defect: it is
> scoped to this section's own effect and was true at its commit, unlike the unscoped
> `(highest allocated: D70)` the D71 marker above had to correct. Left as written deliberately, so
> that a later sweep of the high-water-mark class does not "fix" a sentence that is right.

### D73 — CLOSED, FIXED in `cfd89c7`. `list_by_prefix` collapsed a failed `docker ps` into "no containers"

`ContainerSandbox.list_by_prefix` returned `[]` when `docker ps` did not exit 0. A daemon that is
down, a permission error on the socket, and a run that genuinely has no containers were one
answer. **This is D44's collapse on the read side, and it is the silent member of the family:**
D44's `ReapResult.failed` swallowed the failure of something that was *attempted*, so at least an
attempt had happened; here nothing is attempted at all, because the sweep is told there is nothing
to sweep.

**Why it stopped being theoretical.** §11.5 step 2 landed a caller (`e915b93`): `fleet resume`
sweeps orphan containers on every non-`--dry-run` run, unconditionally
(`cli.py:10231`). `ContainerReapResult.complete` is `not self.failed`, so a sweep during a docker
outage returned `reaped=[] failed=[] complete=True` and `cli._reap_lines` printed
`no orphan containers`. A mechanism that appears to run and cannot fire, reporting success —
CLAUDE.md Rule 11 is *fail loud*.

**The fix, shaped like D44's rather than newly invented.** `list_with_verdict` returns a
`ContainerListing` carrying either `names` or an `error` reason string — a string and not a bool,
because the operator's next action differs between a daemon that is down and a `docker ps` killed
at its deadline. `no_verdict` is asked before `result.ok` (ADR-0067 part 4), so a call that never
started is not reported as a docker-level refusal, and `stdout_tail` is not parsed when `error` is
set: a partial listing read as the inventory is exactly how a reaper concludes that a container it
never saw does not exist. `reap()` reads that method and, on an error, returns one `failed` entry
naming the **prefix** it could not enumerate — so `complete` is `False` and the operator gets
docker's own words. The widening of `ContainerReapFailure.name` from "a container" to "a container
or the prefix" is documented on that dataclass, because `name` is what an operator pastes after
`docker rm`.

**Every caller, and how each behaves now.** Three in `src/`:

| Caller | Before | Now |
|---|---|---|
| `ContainerSandbox.reap` (`sandbox/container.py`) | read `[]`, returned an empty `ContainerReapResult` that reads as a clean sweep | reads `list_with_verdict`; a failed listing becomes one `failed` entry named `fleet-<run_id>-*`, `complete` is `False` |
| `BuildverifyWorker._sweep_containers` (`workers/buildverify.py`) | iterated `[]`, swept nothing, said nothing | **closed, `5ed4e47`** — reads `list_with_verdict`; a failed listing logs a `container_sweep_listing_failed` warning naming the reason and returns without sweeping, still without raising (see adjudication below) |
| `cli._reap_orphan_containers`, `--dry-run` branch (`cli.py:10594` at `87ed419`, the `list_by_prefix` call under `if dry_run:`; the same branch's call is `:10605` at `f10a863`) | previewed an empty list as "nothing to reap" | **closed, `5ed4e47`** — reads `list_with_verdict`; a failed listing is returned as `error`, and `cli.py:10319`'s formatting prints "the container sweep did not run" instead of the clean headline |

**The residual — CLOSED, `5ed4e47`, and then the wrapper it left behind was deleted at
`f10a863`.** Originally stated rather than closed: `list_by_prefix` was kept, lenient, and the two
call sites above lived in modules this entry's original fix did not own. `5ed4e47` moves both to
`list_with_verdict` **in the modules that own them**, as this section called for. Verified directly
against the code at `main` `f10a863` (not the commit message):
`workers/buildverify.py:1163-1224`'s `_sweep_containers` awaits `sandbox.list_with_verdict(prefix)` and, on a failed
listing, logs a `container_sweep_listing_failed` warning and returns without sweeping — still
deliberately not raising, for the reason this section already gave (all three of `_sweep_containers`'s
call sites are cleanup after something else already failed); `cli.py:10605`'s `--dry-run` branch
also awaits `sandbox.list_with_verdict(prefix)` and, on `listing.error is not None`,
returns a dict carrying that `error` rather than an empty `reaped` list, which `_reap_lines`
(`cli.py:10319`) formats as "the container sweep did not run" — the clean "no orphan containers"
headline the original defect risked is no longer reachable on a failed listing.
With both callers moved, `list_by_prefix` itself had zero remaining call sites in `src/`, so a
later commit, `f10a863`, deleted the method from `sandbox/container.py` entirely rather than leave
it as a dead wrapper (CLAUDE.md Rule 2); `grep -n 'def list_by_prefix\|list_by_prefix(' src/fleet/sandbox/container.py`
against `f10a863` returns nothing. The pairing test this section previously cited,
`test_list_by_prefix_stays_the_lenient_view_and_list_with_verdict_the_honest_one`, no longer exists
for the same reason — its whole premise (a lenient/honest pair) is gone — and was replaced by
`test_list_with_verdict_carries_dockers_own_words_on_a_failed_listing`
(`tests/test_sandbox.py`), whose own docstring records this closure and cites `5ed4e47`; the two
newer tests (`b5bbb31`'s `test_workers_build.py` additions, `a65a305`'s
`_ScriptedDocker.ps_fails` case in `test_cli.py`) each independently pin one of the two call sites
above on its reported verdict, with removal counts kept as controls since a failed listing produces
zero removals either way. **This closes the residual; it does not reopen this entry's own "CLOSED,
FIXED in `cfd89c7`" heading, which was never in question.** (Adjudicated 2026-08-20 by the
task-adjud lane, replacing the `CITE` lane's anchor-repair note, which correctly declined to
adjudicate and asked the next owner to re-measure. Note for the reader: `main` advanced from
`18a1fc6` to `f10a863` — deleting `list_by_prefix` — while this adjudication was being written; the
citations above were re-verified against `f10a863`, the tip at commit time.)

**What the test measures, and the blindness it was written to avoid.** A test that measured
*removals* — `result.reaped`, or the `docker rm --force` argv the fake runner recorded — **cannot
observe this defect**: a failed listing produces zero removals both before and after the fix, so
the number it reads is `0` either way. Both are asserted in
`test_reap_reports_a_failed_listing_instead_of_a_clean_empty_sweep` as *controls*, and the
discriminating assertions are on `failed` and `complete`. Demonstrated, not argued: under a
mutation restoring the pre-fix read (`reap()` back on `list_by_prefix`), pytest fails at the
`result.complete is False` line — the two removal-counting assertions above it executed and
passed. The same mutation leaves
`test_reap_reports_a_real_empty_inventory_as_a_clean_complete_sweep` green, which is why that one
is the no-over-correction control and not the test that pins the fix.

### D72 — OPEN, recorded only. Step 2's worktree half is a correct sweep over an empty namespace

**Not a defect in the step-2 code.** `_reap_orphan_worktrees` and `WorktreeManager.reap` do
exactly what they say. The namespace they sweep is empty because nothing else in `src/` ever puts
a worktree into it. Three independent legs, each verified here at `8df4af8` rather than inherited
from the report that first raised it:

1. **The names carry no run prefix.** `OrchestratorContext.worktree` (`orchestrator/context.py:206-208`)
   returns `work_dir / repo_id`. That path is what reaches a worker
   (`context.py:232` `workdir=`, `cli.py:2207` `worktree_path=`). `WorktreeManager.reap`
   (`sandbox/worktree.py:303-310`) removes "every `fleet-<run_id>-*` worktree not in
   `live_names`". A directory named `<repo_id>` is not `fleet-<run_id>-<repo>-<attempt>`, so no
   worktree the fleet actually cuts can match the glob.
2. **The worktrees are registered in a different git dir.** `CloneWorker._materialize_worktree`
   (`workers/clone.py:397-407`) runs `git worktree add --detach` through `self._git(mirror, ctx)`
   — the **per-repo mirror**. `WorktreeManager` interrogates one fixed repo:
   `_git_run` (`sandbox/worktree.py:134-141`) is `git -C self.repo_dir …`, and its sole
   construction site `cli._reap_worktree_manager` (`cli.py:10464-10471`) sets
   `repo_dir = settings.root / run.monorepo_path`. `git worktree list` on the monorepo cannot see
   worktrees registered in a mirror.
3. **There is no third party that would bridge the two.** `WorktreeManager(` appears exactly once
   in `src/` — `cli.py:10466`. Nothing else constructs it, so no other code path supplies a
   `repo_dir` that would make legs 1 and 2 line up.

**Net effect: the container half of step 2 works; the worktree half is a correct sweep over an
empty namespace.** It reports `no orphan worktrees` truthfully about the namespace it examined,
and that namespace is not where the fleet's worktrees are.

**Deliberately not fixed here.** The fix is structural — it requires changing the worktree naming
in `orchestrator/context.py` and the git dir in `workers/clone.py`, both outside the step-2 lane's
scope and outside this one's, and a separate design lane owns it. Recorded so a future lane has
the evidence rather than the symptom; that design landed while this was being written and is
`docs/superpowers/plans/design-worktree-namespace.md` (`c6bdd26`), which re-verifies these same
three legs and adds a fourth about the Phase 3/4 worktrees a rename alone would make reapable. **Do not close this by making `WorktreeManager` scan the
mirrors**: that decides the namespace question by the reaper's convenience rather than by what
`§3.3` says a sandbox name is, and legs 1 and 2 would still disagree.

### A hazard the same work exposed: the resume tests reached the host docker daemon

Independent of both defects, and worth its own line because it is about the *suite*, not the code.
`cli._reap_container_sandbox` returns a bare `ContainerSandbox()`, whose default runner is the real
`util.proc.run`. When §11.5 step 2 landed (`e915b93`) the sweep became unconditional on the resume
path (`cli.py:10231`), and nine `test_resume_*` tests already existed in `tests/test_cli.py`. For
the two commits until `0b0db5c`, running the suite meant `docker ps --filter name=^fleet-…` against
whatever daemon the developer had running — and, off the `--dry-run` branch, `docker rm --force`
for anything that matched. `0b0db5c` closed it with an **autouse** fixture,
`_no_test_may_reach_the_host_docker_daemon` (`tests/test_cli.py:1609-1617`), which monkeypatches
that seam to a scripted docker. Autouse is the right shape and the reason is in its docstring: the
hazard belongs to the code under test, not to the tests that remember to opt out of it. Recorded
because "a test suite that reaches a real daemon" is a hazard that outlives the defect that
exposed it, and because the seam functions (`_reap_container_sandbox`, `_reap_worktree_manager`)
exist for exactly this and a future sweep added outside them re-opens it silently.

> **Annotation (2026-08-21), lane W2 on behalf of lane W6 — two of D72's seven design tasks have
> landed. D72 itself is NOT closed and this entry needs no correction; every claim in it is still
> true as written.** `eaa112f` landed tasks 1 and 2 of the seven in
> `docs/superpowers/plans/design-worktree-namespace.md`: ADR-0085 decides the two worktree name
> forms, and `sandbox/worktree.py` gains `checkout_name`. Verified here rather than inherited from
> the lane report — `grep -rn checkout_name src/fleet/` at `eaa112f` returns the definition, the
> `sandbox/__init__.py` re-export and four docstring mentions, and **no production call site**. So
> the claim this entry rests on — that step 2's worktree half sweeps a namespace nothing writes to —
> is unchanged, and **tasks 3–7 remain open**. ADR-0085 §3 adds one fact this entry did not have:
> **neither name form is injective over `RepoId`**, so once task 3a lands, `live_names` can spare
> the wrong directory. That is a new hazard for the fix, not a change to this defect.

---

### D74 — OPEN, recorded only. Phase 3/4 worktree paths are computed twice, independently, and nothing enforces the two stay equal

Found by the lane designing the worktree-namespace fix for D72
(`docs/superpowers/plans/design-worktree-namespace.md`, its "Leg 4"). Verified here directly
against `HEAD` (`87ed419`), not inherited from that document.

**Leg A — unlike D72's worktrees, these are already registered where the reaper looks.**
`_plan_build` and `_plan_verify` (`src/fleet/cli.py:7321`, `:7416`) each cut a worktree with `git
worktree add --detach --force` through the `Git` bound to the *monorepo* checkout, at `build_root
/ repo_id` and `verify_root / repo_id`. `WorktreeManager` interrogates that same monorepo checkout
(`repo_dir = settings.root / run.monorepo_path`, `cli.py:10464-10471`, feeding `_git_run` at
`sandbox/worktree.py:134-141`). D72's Phase 1/2 worktrees fail on both name *and* registry — cut
through a per-repo mirror `WorktreeManager` never looks at (`workers/clone.py:397-407`). These fail
on name only: a directory already sits in the registry the reaper reads, so renaming it to carry
`run_prefix(run_id)` would make it reapable with no change to `WorktreeManager` itself. Holds, on
direct read.

**Leg B — the path is computed twice, independently, and nothing ties the two together.**
`cli.py:7321` computes `build_root / repo_id` for the `git worktree add` call. Separately,
`cli.py:7625` passes `work_dir=build_root` into `OrchestratorContext`, whose `worktree()` method
(`src/fleet/orchestrator/context.py:206-208`) computes `self.work_dir / repo_id` again to hand
every worker its `workdir`. Verify repeats the pattern: `cli.py:7416` vs. `cli.py:7695` /
`context.py:208`. Both computations read the same two inputs and, today, agree — but they are two
separate lines in two separate modules, with no shared function, constant, or assertion binding
them. Holds, on direct read: nothing between these four sites ties them together.

**Latent, not broken.** Nothing fails today; the two computations happen to agree because both
reduce to `<root-var> / repo_id`. The hazard is silent divergence later: if one of the two lines
changes — for instance, when D72's eventual fix adds a `run_prefix`/attempt suffix to one of them —
and the other is missed, a Phase-3 or Phase-4 worker is handed a `workdir` that does not exist,
and because each half is a one-line join in a different file, no single listing would surface the
mismatch.

**Relationship to D72.** Same subsystem (worktree naming and registration across §3.3's build and
verify phases), a different cause (an unenforced duplicate computation, not a naming/registry
mismatch). D72's fix is expected to touch these same sites (`cli.py:7321`, `:7416`, `:7625`,
`:7695`, `context.py:206-208`) when it adds the missing `run_prefix`, which is exactly what would
put this defect's two legs at risk of disagreeing.

**Deliberately not fixed here.** The fix belongs to the worktree-namespace design that raised it,
which has its own plan and reserved ADR numbers. Recorded so that design's lane inherits the
evidence rather than rediscovering it.

---

## D75 — corrections to tracked review findings

### D75 — CORRECTION RECORDED, no code defect. The round-C final review's I1 "inverted ordering" failure scenario does not follow from the code

`docs/superpowers/plans/design-resume-step5-review-final.md` finding **I1** states that if a
reconciler "moves the evidence test first", then "for a repo whose BUILD row is `DEGRADED` and whose
BUILD evidence does not hold, `phase_floor` then sets `floor = BUILD` and walks below it — landing
the re-entry floor **on** a `DEGRADED` phase, the one outcome ADR-0077 §5 forbids". That consequence
does not follow. **Both branches of the backward walk in `orchestrator/reentry.phase_floor` are a
bare `break` with no other effect, so swapping them is a no-op**: whichever fires first, the loop
ends on the same iteration with the same `floor`.

**Measured, twice, independently.** Lane W3 measured it at `0e945b8`; this entry's author reproduced
it from scratch at `4cde582` rather than inheriting the number: all 7 `RepoStatus` values × 4
`Phase` positions = **2,401 row states**, × all **16** subsets of `evidence` = **38,416 inputs**,
comparing `phase_floor` against a copy with the two tests swapped — **0 differing returns**. W3
independently corroborated with `tests/test_reentry_floor.py` staying **41/41** green under the
swap. The scenario I1 describes requires the hard-stop test to be **deleted**, not reordered, and a
deletion is loud: measured under the deletion (change confirmed by `git diff --numstat` before the
result was read), **five cases fail — four of them behavioural and pre-existing** —
`test_degraded_phase_is_a_hard_stop_it_is_not_demoted_and_search_does_not_pass_it`,
`test_skipped_phase_is_never_the_floor_the_walk_stops_at_it_exactly_as_for_degraded`,
`test_search_does_not_pass_a_skipped_phase_when_evidence_below_it_holds` and
`test_search_does_not_pass_a_skipped_phase_even_when_nothing_earlier_holds`.

**The observation underneath I1 stands, and is now closed.** The ordering *word* in the canonical
clause really was unbound — the prose could say "tested *after* evidence" and no layer read it.
`tests/test_reentry_floor.py::test_the_hard_stop_test_runs_in_the_order_phase_floors_own_docstring_claims`
(`4cde582`) binds it, and `test_floor_rule_statements._observed_hard_stop_order` measures the code's
real order rather than asserting one. What is corrected here is **only the claimed consequence**.

**Why this is a ledger entry and not an edit to the review.** The review correctly records what its
author measured at `d123035`; the superset half of I1 was measured and is true, and the ordering
half was reasoned rather than measured, which the finding does not distinguish. Guardrail 7 governs:
a dated in-file marker is placed beside I1 in the review, and the finding itself is not rewritten.
**Status: correction recorded, no code defect, nothing to fix in `src/`.**


---

## D76 — OPEN, recorded only. SPEC §3.5's "current integration tip" clause has no binding

`docs/SPEC.md` §3.5, the wave re-entry paragraph:

> *"Their Phase 4 runs against the **current** integration tip — a fresh snapshot ref per §3.3
> step 1, not the tip their original wave saw — and any Phase 3 merge is rebased onto it, because
> everything that closed in between has already landed."*

**Correcting a symbol name that reached this entry's originating brief.** The brief named the
Phase 4 builder `cli._verify_plan`. That name has **zero** definitions anywhere in `src/`
(`grep -rn 'def _verify_plan' src/` empty). The sole builder of `_VerifyPlan` is
**`cli._prepare_verify`** (`src/fleet/cli.py:7427`; `_VerifyPlan` itself at `:5921`), verified
directly here before writing — a research lane's name was wrong, and the correction is due to
lane W7 (`.superpowers/sdd/round-e/lanes/W7/report.md`, its §0 "Correction to R1's symbol name").

**The behaviour the clause describes is real, at two different confidence levels — and the split
is the entry's whole content, not a detail:**

* **Function-level: CONFIRMED BY EXECUTION.** Cited from lane W7's report (§2, "Fact 2"), which
  drove both halves against real local git repos and real production methods, not a source read:
  `cli._prepare_verify`, called three times with a stale ref deliberately made available first and
  the branch tip moved between calls — **including a repeat call for a repo that already had
  one** — cut a **new** snapshot at the **live** tip every time (`VERDICT_PHASE4:
  UNCONDITIONALLY-FRESH`); `BuildgenWorker._ingest`, called twice with the tip advanced in
  between, merged with `first_parent == tip_before` both times (`VERDICT_PHASE3:
  MERGED-ONTO-CURRENT-TIP`). W7 also validated its own instruments against known-bad mutations and
  a cosmetic-reflow control (four checks per fact) — see that report for the full battery.
  **Rot check performed here, not inherited:** `git diff --numstat 698f750 34d6f82 --
  src/fleet/cli.py src/fleet/workers/buildgen.py` (`34d6f82` is `HEAD` at the time this entry was
  written) returns **empty** — zero changed lines in either file since W7's anchor commit, so the
  citation has not rotted.
* **Caller-level: SOURCE-READ, NOT EXECUTED — stated at exactly W7's own confidence, not
  upgraded.** `_verify_impl` memoises `plans: dict[str, _VerifyPlan]` **outside** the wave loop; a
  repo already in the dict is skipped (`if repo_id in plans: continue`), so within one `fleet
  verify` process a repo planned while an earlier wave ran keeps that wave's ref if a later wave
  touches it again. W7 named a mitigation and did not exercise it either: `wave_members`'s
  `PRIMARY KEY (run_id, node_kind, node_id)` gives a repo exactly one `wave_index`, so the memo
  cannot hand a later wave's member an earlier wave's ref — but nobody has driven `_verify_impl`
  over a real multi-wave fleet to confirm it. Recorded as unexercised, not as confirmed.

**The defect is not that the behaviour is missing.** It is that **nothing binds this clause to the
code that satisfies it.** Measured here: `grep -rn "current integration tip\|fresh snapshot ref
per §3.3\|SPEC §3.5\|SPEC 3.5" src/ tests/` (excluding `docs/`) returns **zero** hits anywhere near
`_prepare_verify` or `_ingest` — no docstring, no comment, no test name ties the clause to either
symbol. `docs/superpowers/plans/handoff-round-e.md` §"T — the orphaned §3.5 'current integration
tip' clause" says the same thing independently: **"Currently owned by nobody."**

**Would a test catch it? No.** Measured: `grep -n "^async def test_\|^def test_" tests/*.py |
grep -iE "verify|snapshot"` under `_prepare_verify`/`_ingest`-adjacent files, and a direct name
search for `_prepare_verify`, `"fresh snapshot"`, `"rebased onto"` across `tests/*.py`, all return
**zero** matches. If a future edit to `_prepare_verify` or `_ingest` silently reused a stale
snapshot or merged onto a stale tip, no test in the tree would fail.

**Severity: low.** The behaviour is correct today at the function level; the gap is that nothing
would notice if it stopped being, and the caller-level property is unexercised rather than known
to be safe. **Deliberately not fixed here** — this is a binding/ownership gap, not a code change;
ADR-0090 (referenced in W7's report as in progress) is the natural place to either assign the
clause a home (subtask 8, subtask 10, or a documented gap) or bind it with a test.

> **Editorial correction (2026-08-21), lane W19 — SYMBOL anchors added; both `cli.py` line numbers
> above are kept as history and no longer resolve.** Cite **`cli._prepare_verify`** (a module-level
> `async def` in `src/fleet/cli.py`) and the **`cli._VerifyPlan`** class in the same file. Both
> citations were **correct when this entry was written** — measured: at `8cf4958`, the commit that
> appended this entry, and at `34d6f82`, the anchor the entry names, `cli.py:7427` is
> `async def _prepare_verify(` and `cli.py:5921` is `class _VerifyPlan:`. They rotted at `f865eed`;
> at `704099c` `:7427` is an `adapter_name=` keyword argument and `:5921` is a docstring line inside
> **`cli._BuildPlan`** — a different class. This is rot **after** writing, so the entry is annotated rather than
> rewritten. Nothing else in the entry is disturbed: the rot check it performed
> (`698f750..34d6f82` empty over `cli.py` and `buildgen.py`) was correct at its own anchor.

---

## D77 — FIXED, LANDED (`c4a1532`). `append_blocked_by` takes a DEGRADED phase to BLOCKED via raw SQL, bypassing `ALLOWED_TRANSITIONS`

**Re-measured from the primary source, not inherited from R1's one-line summary.** There are four
textual definitions of `append_blocked_by`; two (`cli.py:1419`, `cli.py:3622`) are pass-through
wrappers (`return await self._inner.append_blocked_by(...)`), one (`scheduler.py:125`) is the
`SchedulerStore` Protocol's signature, and the **only real implementation** is
`SqliteSchedulerStore.append_blocked_by` (`src/fleet/orchestrator/scheduler.py:255`). Its own
docstring: *"Set-union `blocker` into every non-`SUCCEEDED` phase of `repo_id` and mark it
BLOCKED."* Read directly: the guard is

```python
if current is RepoStatus.SUCCEEDED or current in TERMINAL_STATUSES:
    continue
```

`TERMINAL_STATUSES` (`models/enums.py:25-28`) is `{SUCCEEDED, REQUIRES_HUMAN_INTERVENTION,
SKIPPED}` **by the module's own comment, deliberately excluding BLOCKED and DEGRADED because both
are "RESOLVABLE"**. So a `DEGRADED` phase is not skipped: the function falls through to
`UPDATE phases SET blocked_by = ?, status = 'BLOCKED', updated_at = ? WHERE ...` — raw SQL, with
**no call to `transition()` and no reference to `ALLOWED_TRANSITIONS` anywhere on this path**.

**`ALLOWED_TRANSITIONS[DEGRADED]` does not contain `BLOCKED`.** `models/enums.py:42-45`:
`DEGRADED -> {RUNNING, SUCCEEDED, REQUIRES_HUMAN_INTERVENTION}` only. Confirmed by calling the
gate function itself, not by reading the dict: `transition(RepoStatus.DEGRADED,
RepoStatus.BLOCKED)` raises `ValueError: illegal status transition DEGRADED -> BLOCKED`.

**Executed against a real temp SQLite database, through the real writer/CAS pair — not a source
read.** Probe run in an isolated detached worktree at `34d6f82` (`.venv/bin/python`, module
`__file__` printed and confirmed to resolve inside the worktree, per COMMON.md rule 9/11):

1. `acme-degraded`'s BUILD phase driven to `DEGRADED` through `acquire_phase_lease` +
   `complete_phase` — the same production CAS pair `tests/test_scheduler.py`'s `_set_status`
   helper uses, never a hand-edited row.
2. A real `stubs` row inserted for it, `state='ACTIVE'` (genuinely unresolved), provider
   `acme-blocker`.
3. `SqliteSchedulerStore.append_blocked_by(RUN, "acme-degraded", "acme-blocker", now=...)` called
   directly.

Result: `touched=1`; the phase row's `status` column reads `DEGRADED` before the call and
`BLOCKED` after, with `blocked_by=["acme-blocker"]`. The `stubs` row is untouched — this method
never writes that table.

**Real consequence, also executed — the actual `state.projection.build_state`, not a synthetic
`RepoState`.** With the ACTIVE stub row present throughout, `build_state()` after the bypass
reports: `state.repos["acme-degraded"].status == BLOCKED`, `.stubbed_deps == []`,
`state.degraded == []`, `state.unresolved_stubs == {}`. The mechanism is `_fold_repos`
(`state/projection.py:275-318`, moved from `:241-277` by ADR-0106's `_derive_updated_at`
addition), which derives `stubbed_deps`/`stub_states` **conditionally on the
phase row's own `status` being `DEGRADED`** (`degraded_stubs = dict(stub_states[repo_id]) if
status is RepoStatus.DEGRADED else {}`) — so the invariant `RepoState._stub_invariants`
(`models/state.py:188-193`, `stubbed_deps is non-empty iff status is DEGRADED`) is never violated;
the projection just silently stops describing this repo as degraded at all. `unresolved_stubs`'s
own docstring calls this **"the 'Degraded and unresolved' section of the final report (§3.5.1
reconciliation)... Non-empty at end of run means the fleet has work it must NOT ship."** A repo
with a genuinely unresolved, ACTIVE stub row disappears from both operator-facing lists the moment
one of its ordering-subgraph ancestors is abandoned, with nothing else changing about the stub.

**Honest limit — deliberately not claimed further.** Whether this currently changes the run's
*exit code* was not established and is not asserted here. `cli.py`'s own in-tree comment at the
`stub_reconcile` insertion point (§13 row 45, near `cli.py:10236`) states that the mechanism which
would independently gate exit 7 by walking `stubs` directly — *"`stub_reconcile`... walks
`ix_stubs_open` and writes one `UnresolvedStub` finding per open row, which is what makes the run
exit 7"* — **"does not exist on `main` yet."** So the measured consequence is the projection-level
one above; whether any exit-code path in the current tree reads `degraded`/`unresolved_stubs` for
this exact scenario was not traced further, and is left unknown rather than assumed either way.

**Second-order, from the gate itself.** Once the row reads `BLOCKED`, `ALLOWED_TRANSITIONS[BLOCKED]
= {PENDING, SKIPPED}` — `DEGRADED`'s own legal exits (`RUNNING` for a revalidation round,
`SUCCEEDED` on resolution, `REQUIRES_HUMAN_INTERVENTION` on stub rot, §3.5.1) are no longer
reachable through the gate for this phase; its only legal next moves are step 6's un-blocking to
`PENDING` or an operator `SKIPPED`.

**Would a test catch it? No — measured, not assumed.** Three files call `append_blocked_by`:
`tests/test_blocked_by_writer_statements.py`, `tests/test_scheduler.py`, `tests/test_runner.py`.
`grep -c DEGRADED` on each returns **0, 0, 0**. No test anywhere drives a DEGRADED phase into
`append_blocked_by`.

**Severity: medium.** A state-machine transition the gate explicitly rejects happens anyway by
construction on every abandoned-ancestor propagation that reaches a DEGRADED dependent, and a
genuinely unresolved stub becomes invisible to both operator triage lists with nothing else
changing. Not rated high only because the current exit-code consequence was not traced end-to-end
(see the honest limit above) and the writer's own docstring frames the union as deliberately broad
("mark it BLOCKED") rather than as an oversight. **Deliberately not fixed here** — R1 recommended
recording rather than fixing inside the next subtask, and the fix (excluding `DEGRADED` from the
fall-through, or routing through `transition()`) touches `src/fleet/orchestrator/scheduler.py`,
which round E has since changed at `da45a43` (`SchedulerStore.append_unblocked_wave`); a
future lane should re-measure before touching `scheduler.py` again.

> **Editorial correction (2026-08-21), lane W19 — the sentence above is CORRECTED IN PLACE, because
> it was false when it was written, and there is no "what was true then" for a writing-time error to
> preserve.** It read: *"touches the same file a sibling round-E lane just changed (`50ad1e4`,
> `WaveScheduler.propagate_blocked` / `append_blocked_by` caller-naming)"*. Found by lane CR4 and
> re-measured here: **`50ad1e4` does not touch `src/fleet/orchestrator/scheduler.py` at all** —
> `git show --stat 50ad1e4` is `docs/SPEC.md`, `src/fleet/models/state.py` and
> `tests/test_blocked_by_writer_statements.py`, three files. Nor did any other commit: `git log
> --oneline 47df73d..8cf4958 -- src/fleet/orchestrator/scheduler.py` is **empty**, so at the moment
> this entry was appended (`8cf4958`) no round-E commit had touched that file. `50ad1e4`'s *subject*
> was adjacent — it defines "non-delegating caller" and does name `propagate_blocked` — but the
> file claim was wrong, and the clause's whole force was the file. The replacement states what is
> true at `704099c`: `git log --oneline 47df73d..704099c -- src/fleet/orchestrator/scheduler.py`
> returns exactly one commit, **`da45a43`**, which added `SchedulerStore.append_unblocked_wave`
> (+88 lines in that file). The "re-measure before touching `scheduler.py`" advice is unchanged and
> is now, unlike before, actually earned.
>
> **Separately — SYMBOL anchors added; the `cli.py` line numbers above are kept as history and no
> longer resolve.** The two pass-through wrappers are **`cli._ScanWaveStore.append_blocked_by`** and
> **`cli._ScopedWaveStore.append_blocked_by`**; the §13 row 45 in-tree comment is the
> **`stub_reconcile`** comment block inside **`cli._resume_impl`** (`src/fleet/cli.py`), which is
> also the function `:10236` falls inside at `704099c` — the comment moved within it. All three were **correct when written**
> (at `8cf4958` and at the entry's own `34d6f82` anchor, `cli.py:1419` and `:3622` are both
> `async def append_blocked_by(` and `cli.py:10236` is the `UnresolvedStub`/exit-7 sentence) and
> rotted at `f865eed` — so they are annotated, not rewritten. **One of them is now actively
> misleading and is the reason this marker is not optional:** at `704099c`, `cli.py:3622` lands on
> `return await self._inner.wave_indices(run_id)` — a **different** pass-through wrapper, so the
> citation silently points at the wrong method rather than at nothing.
>
> **The non-`cli.py` anchors were re-measured too, and most of them still hold at `704099c`** —
> reported rather than edited, since editing a correct citation because it matched a sweep is the
> mirror-image error: `scheduler.py:125` is still the `SchedulerStore` Protocol's
> `async def append_blocked_by(`; `models/enums.py:25-28` is still `TERMINAL_STATUSES`;
> `models/enums.py:42-45` is still `ALLOWED_TRANSITIONS[DEGRADED]`; `state/projection.py:241-277`
> still opens on `def _fold_repos(`; `models/state.py:188-193` still covers
> `RepoState._stub_invariants`. The one that has rotted is **`scheduler.py:255`**: cite
> **`SqliteSchedulerStore.append_blocked_by`**, which `da45a43` moved further down the file.

### Fixed (2026-09-01, round Y task 2), `c4a1532` — body above left as written, per this file's own convention

**`DEGRADED`→`BLOCKED` was NOT added to `ALLOWED_TRANSITIONS`.** The state-model comments this
entry already quotes (`models/enums.py:27-29`, `:42-46`) say a `DEGRADED` phase leaves the machine
only via a budgeted revalidation round (`RUNNING`), outright success, or human intervention — never
by being blocked — so widening the allowed set would be a semantic change, not a bugfix. The fix
instead makes `append_blocked_by`'s loop consult the real gate before writing:
`transition(current, RepoStatus.BLOCKED)` inside a `try`/`except ValueError: continue`, so a
`DEGRADED` row is skipped (not counted in `touched`, `blocked_by` and `status` both left alone) and
`PENDING`/`RUNNING`/`BLOCKED` rows are unaffected (all three are already in
`ALLOWED_TRANSITIONS` for a transition to `BLOCKED`, so their behaviour is unchanged). The gate is
the real `transition()`, not a hand-rolled `is RepoStatus.DEGRADED` check — this stays correct if
`ALLOWED_TRANSITIONS[DEGRADED]` is ever revisited without a second edit to this method. The write
itself is still raw SQL: the `SchedulerStore` surface stays deliberately narrower than the full
repository (module docstring, unchanged by this fix).

**Test:** `tests/test_scheduler.py::test_append_blocked_by_does_not_move_a_degraded_phase_to_blocked`
drives a phase to `DEGRADED` through the real `acquire_phase_lease`/`complete_phase` CAS pair (the
same helper the rest of this file's suite uses, never a hand-edited row), calls
`append_blocked_by`, and asserts `touched == 0` and the row still reads `status='DEGRADED'`,
`blocked_by='[]'`. Rule 12: reverting the guard (deleting the `try`/`transition`/`except` block)
was confirmed to change the file (`git diff --numstat --no-index` against a pre-mutation backup,
non-empty) and reddened exactly this test — 1 failed / 12 passed in `tests/test_scheduler.py`, no
other test in that file affected, so this is not a module-wide outage wearing a discriminator's
clothes.

**Honest limit, unchanged from the OPEN entry above:** whether this ever changed the run's exit
code was not established there and is not re-derived here — only the projection-level consequence
(`_fold_repos` reading the phase's own `status` column) was traced, and that is what this fix
closes: a `DEGRADED` phase this method touches now keeps reporting as degraded in
`state.degraded`/`state.unresolved_stubs` rather than silently disappearing from both.

**Scope, checked before closing this out — the reaper's RHI leg is a SEPARATE site, not fixed
here.** `docs/CRITERIA_PLAN.md` §46 names "the reaper's RHI leg writes raw SQL bypassing
`transition()` entirely... the D77 bypass shape, recurring" as one of §46's gaps. `complete_phase`
(`src/fleet/state/repository.py`) sets `REQUIRES_HUMAN_INTERVENTION` via its own inline `CASE WHEN`
SQL and never calls `transition()` either — a textually distinct site, a different function, and a
different destination status (`REQUIRES_HUMAN_INTERVENTION`, not `BLOCKED`) from the one this entry
fixes. Not touched by `c4a1532`. Whether §46's mention refers to that site or another was not
resolved further here — left for the controller/a future task, per this task's brief, which scoped
fixing it out explicitly.

---

## D78 — FIXED, LANDED (`6c3c014`). `LlmFindingSink.record_backend_unavailable`'s `tier=` arm has zero producers, so every `BackendUnavailable` row this harness ships is run-scoped and both derived fields refuse to answer

**Measured in round E by lane W18 against `main` at `da45a43`.** Carried from a round-D backlog
item that lane W1 re-measured at `47df73d`; it reproduces unchanged. Class result rather than a
commit list: of the **16** commits in `47df73d..da45a43`, **none** touches any file this entry
names — `git diff --name-only 47df73d..da45a43` does not list `orchestrator/findings.py`,
`orchestrator/runner.py`, `workers/base.py`, `workers/classify.py`, `tests/test_llm_findings.py` or
`tests/test_runner.py`. Recorded rather than removed, by orchestrator ruling — see "Why this is
recorded and not deleted".

`LlmFindingSink.record_backend_unavailable` (`src/fleet/orchestrator/findings.py`) declares
`tier: ModelTier | None = None` and branches its entire honesty block on it. With `tier`:
`failover_triggers` is narrowed to that tier, `failover_triggers_scope` is `"tier"`,
`failover_triggers_recorded` is `"none"`/`"partial"` **about that tier**, `throttling_observed` is a
real boolean, and `_caveat` emits `_CAVEAT_TIER`. Without it: the whole-run map, scope `"run"`,
`failover_triggers_recorded` `"unknown"`, `throttling_observed` `null`, and `_CAVEAT_RUN`.
**Nothing in `src/` takes the first branch.**

**Enumeration — by AST, not by grep.** A grep for `tier=` miscounts a wrapped call and cannot see a
`**kwargs` forward. The instrument (`ast.Call` whose `func` terminal identifier is
`record_backend_unavailable`, over every `*.py` under a root) was validated three ways before its
clean result was trusted: it fired on a synthetic file containing exactly those two shapes (a
five-line wrapped call with `tier=` and a separate `**kw` forward), stayed silent on an
already-swept package (`src/fleet/llm`), and stayed silent on a call-free control file.

| root | calls to `record_backend_unavailable` | of those, passing `tier=` | `**kwargs` forwards |
| --- | --- | --- | --- |
| `src/` | **1** | **0** | 0 |
| `tests/` | **8**, in **7** test functions, all in `tests/test_llm_findings.py` | **4**, in 4 of those 7 | 0 |

The one `src/` call is in `PhaseRunner._drive`'s `FailureClass.BACKEND_UNAVAILABLE` branch
(`src/fleet/orchestrator/runner.py`) and passes `repo_id=`, `phase=`, `observed=` only. A
whitespace-normalised whole-repo sweep for the bare identifier (normaliser: `re.sub(r"\s+", " ",
text)` across the whole file, so a wrapped occurrence cannot escape) finds it in exactly three
tracked source files — `orchestrator/findings.py` ×4 (the `def` plus three docstring mentions),
`orchestrator/runner.py` ×2 (the call plus one comment), `tests/test_llm_findings.py` ×8 — and in no
other tracked file. So there is no `getattr`, string-dispatch or bound-method-alias site that spells
the name out, and the AST enumeration is complete against that predicate: the class "production call
sites that can reach the narrowed arm" has **zero** members. **What this predicate cannot exclude**
is a call through a name assembled at runtime from fragments; nothing in this codebase does that,
but it was not proven and is not claimed.

**Why it cannot be wired from where it is called.** `TierUnavailable` carries `tier` and
`targets_tried`, and both are lost at the exception→`WorkerError` boundary. `WorkerError`
(`src/fleet/workers/base.py`) declares exactly six fields — `failure_class`, `retryable`,
`exit_code`, `stderr_tail`, `artifact_ref`, `exception_type` — and **no `tier`** (counted off the
class's own annotated assignments, not off a docstring). `_error_for`
(`src/fleet/workers/classify.py`) maps `TierUnavailable` to `FailureClass.BACKEND_UNAVAILABLE` and
keeps only `stderr_tail=redact_text(str(exc))`; the generic escape path,
`error_from_exception` (`src/fleet/workers/base.py`), keeps `stderr_tail=str(exc)`. The only
surviving carrier is therefore `observed`, and parsing it is forbidden by the design the finding
rests on: nothing in this codebase branches on message text.

### This is NOT a SPEC gap — SPEC and code already agree, and no SPEC edit is owed in either direction

**State this before touching anything here.** Measured against `git show HEAD:docs/SPEC.md` at
`da45a43` — the committed blob, not the working tree, because a sibling lane had `docs/SPEC.md`
modified while this was being written — whitespace normalised across the whole file:

`BackendUnavailable` occurs **5** times: one `findings.kind` comment in the state-schema listing, one
artefact-column mention in §13 row 40, and **three requirement sentences** — §11.8's *"Fail closed
when a tier is exhausted"* paragraph and §13 row 40 both say the run *"writes a `BackendUnavailable`
finding **naming the tier and every target tried**"* (2 occurrences of that exact phrase), and
§12.43 acceptance case (iv) says the run exits 8 *"with a `BackendUnavailable` finding **naming the
tier and each target tried**"* (1 occurrence).

**The SPEC mandates naming the tier. It does not mandate the `tier=` parameter, and it does not
describe the arm at all.** In the same normalised sweep, `docs/SPEC.md` contains **0** occurrences
of `failover_triggers`, **0** of `failover_triggers_scope`, **0** of `failover_triggers_recorded`,
**0** of `throttling_observed`, **0** of `asserts_outage`, and **0** collocations of `scope` within
40 characters of `tier` (and 0 of `tier` within 40 of `scope`). Every one of those fields is an
implementation-level honesty design, not a SPEC requirement. **Predicate stated, because one of
these is case-sensitive and a careless re-measurement will disagree:** the payload field `caveat`
also has **0** case-sensitive occurrences, but a case-**in**sensitive search returns **2** — both
the English word `CAVEAT.` introducing the state-schema listing's own "Shipped means DECLARED, not
emitted" note, neither the field. Counted case-sensitively, `caveat` is 0; do not read the two
`CAVEAT.` hits as the field surviving.

**And the requirement is already met, on the shipped path, without the arm** — verified through the
live chain rather than inferred from a docstring: `PhaseRunner._drive` passes
`observed=self._detail(failure)`; `PhaseRunner._detail` returns `error.stderr_tail`; `_error_for`
sets that to `redact_text(str(exc))`; and `TierUnavailable.__init__` composes
`f"tier {tier} exhausted after targets: {', '.join(targets_tried) or '-'}"`. The shipped row names
the tier and every target tried, in order. The covering test is named for exactly that —
`tests/test_llm_findings.py::test_backend_unavailable_names_the_tier_and_every_target_tried` — and
it passes **no** `tier=`.

**So a future reader must not "reconcile" the SPEC against the unused parameter.** There is no
sentence in `docs/SPEC.md` promising a tier-scoped payload, and growing one to make the SPEC match
this parameter would manufacture a requirement that has never existed. That is the `supports_effort`
failure mode this project already paid six rounds for: a reconciler adding a field to make code
match a table, and thereby suppressing a value an operator had declared.

### Why this is recorded and not deleted

The obvious remedy — the parameter is dead in `src/`, so delete it under Rule 2 — was evaluated and
rejected by the orchestrator on this lane's evidence. Three reasons, in order of weight:

1. **Removal is not a one-parameter change.** It deletes `_CAVEAT_TIER` and collapses `_caveat` to a
   constant; it orphans `LlmFindingSink.observed_triggers`'s own `tier` narrowing parameter, whose
   only `src/` argument comes from this call; it turns three persisted payload fields into
   constants, so `failover_triggers_scope` becomes a field that looks like it discriminates and
   cannot; and it deletes
   `tests/test_llm_findings.py::test_a_cheap_tier_throttle_does_not_contaminate_a_heavy_tier_outage_row`,
   the regression test for a contamination defect that **was real and was on `main`** — stated at
   the grain that is checkable rather than as "it shipped to an operator", which was not
   established. At `31e6776` (an ancestor of `HEAD`) `LlmFindingSink._triggers` was a flat
   `dict[str, str]` keyed by target across the whole run, and `throttling_observed` was
   `any(t == "RATE_LIMIT" for t in triggers.values())` over that run-wide map with **no `null`
   arm at all** — so a CHEAP-tier 429 set `throttling_observed: true` on a HEAVY-tier outage row,
   telling an operator to lower concurrency while a dead HEAVY endpoint went unrepaired. It was
   caught in review as FD1 finding N1 and corrected by tier-keying the map at `337e1cf`, also on
   `main`. Rule 2 says write the minimum code that solves the problem; it does not say delete a
   guard against a defect that happened.
2. **This document has already adjudicated the class, twice, and never by deletion.** D56 and D57
   are — in D57's own words — *"the same defect class — a dependency-injection parameter that
   exists, is documented, and is never supplied."* D56 (`llm.client.discover()`, zero call sites in
   `src/`) was closed by **wiring it** (`c36160e`). D57 (`FleetSettings.load(capabilities=...)`) is
   **OPEN, recorded**. Neither was deleted.
3. **It is scheduled work, not speculation.** `docs/PROGRESS.md` carries it as a defect item and
   again as a next task — wire the `tier=` arm and `attempts.llm_failovers` together, because both
   need the same cross-lane change and doing them separately pays that cost twice.

**Why this is its own entry rather than a footnote to D59.** Until this entry, the only place in
this file that carried the claim was an aside inside **D59, whose heading reads "FIXED, LANDED"**. By
the "Status vocabulary, used strictly" block above — and by the ruling recorded at `39862ec`, that a
status heading is a **field** governing discoverability rather than history — a reader scanning this
register for OPEN items could not see it. D57 makes the same argument explicitly for itself against
D56: *"Recording it separately is what stops 'BK1 wired the injection' from being read as covering
both."* Same reasoning, same remedy. D59's aside has been left in place and re-anchored by symbol in
the same commit as this entry; it now points here.

### Would a test catch it? No — and one test actively pins the zero-producer state

Measured, not assumed, and the D63 check was run rather than skipped:

* **The arm's behaviour is well covered.** 4 of the 8 recorder calls in `tests/test_llm_findings.py`
  drive the narrowed arm, and `test_the_caveat_never_contradicts_the_scope_field_of_its_own_row`
  drives **both** arms in one test and asserts the caveat cannot contradict its own row's scope. So
  "the narrowed arm works" is proven; that is not the gap.
* **What no test asserts is that the arm is reachable from production.** This is D54's shape: the
  absence of a producer is not a thing a test asserts.
* **One test does more than fail to catch it — it pins the current state.**
  `tests/test_runner.py::test_the_shipped_halt_path_refuses_both_derived_claims_when_triggers_exist`
  drives the real `PhaseRunner` halt through `run_wave` and asserts, on the resulting row,
  `failover_triggers_scope == "run"` and `failover_triggers_recorded == "unknown"`. **Unlike D63's
  case this assertion is true of today's tree** and is the right test of the shipped arm — but it is
  load-bearing for the zero-producer state, and its own docstring says so (*"Nothing in `src/` passes
  `tier=` … so every row this harness writes today is `scope: \"run\"`"*). Whoever wires the arm
  must update that test in the same commit or the wiring lands red. Counts: `failover_triggers_scope`
  is asserted **5** times in `tests/test_llm_findings.py` and **once** in `tests/test_runner.py`, and
  the `test_runner.py` one is the only assertion of it on the production path.

### What would have to exist for the arm to become reachable

Exactly one of:

1. `WorkerError` (`src/fleet/workers/base.py`) grows a `tier` field, `_error_for`
   (`src/fleet/workers/classify.py`) populates it from `exc.tier` on the `TierUnavailable` branch,
   and `PhaseRunner._drive`'s halt branch forwards it as `tier=`; **or**
2. `TierUnavailable`'s raise sites in `src/fleet/llm/client.py` route the tier to the halt path by
   some carrier other than `observed`.

Parsing the tier back out of `observed` is **not** an option — `observed` is the worker's verbatim
`stderr_tail` by design, and nothing in this codebase branches on message text. Either route also
has to update the pinning test named above. `docs/PROGRESS.md` pairs this with
`attempts.llm_failovers` (D62) because both need the same boundary change.

**Severity: low-to-medium, and deliberately not rated higher.** Nothing false ships: the run-scoped
row is correct, its two refusals (`"unknown"` / `null`) are honest, and the caveat it carries matches
its own scope. The cost is operator-facing and bounded — §13 row 43's throttle-vs-outage question
stays unanswerable *in the row*, so an operator must match the tier-keyed map against `observed` by
eye, which the caveat text tells them to do. Not rated lower because the arm that would answer it is
built, tested, and one boundary field away from being reachable.

**Related:** D55 (throttling vs outage), D59 (the sink itself; its aside is the prior record of this
claim), D60 (why `failover_triggers_recorded` can never read `"complete"`), D62
(`attempts.llm_failovers`, the paired unwritten field), D56/D57 (the defect class and its precedent).

### Fixed (2026-09-01, round Y task 3), `6c3c014` — body above left as written, per this file's own convention

**The section "What would have to exist for the arm to become reachable" named exactly option 1,
and that is the option built.** `WorkerError` (`src/fleet/workers/base.py`) gains `tier: ModelTier
| None = None`; `_error_for` (`src/fleet/workers/classify.py`) sets it to `exc.tier` on the
`TierUnavailable` branch and `None` on every other branch; `PhaseRunner._drive`
(`src/fleet/orchestrator/runner.py`) forwards `tier=failure.tier` into
`record_backend_unavailable`. `failure` was already bound at that point in `_drive` (the synthetic
no-result `WorkerError` at the top of the same function still carries no `tier`, so that path
correctly keeps forwarding `None`). Option 2 (routing the tier some other way) was not needed.

**Both tests this entry's own "Would a test catch it?" section named as pinning the zero-producer
state were updated in the same commit** — that section undercounted by one: it named only
`test_the_shipped_halt_path_refuses_both_derived_claims_when_triggers_exist`, but
`test_a_tier_outage_writes_a_backend_unavailable_finding_before_it_halts` asserts the identical
`failover_triggers_recorded == "unknown"` claim and pins the same state. Both are **kept**, not
deleted or mutated in place — each still exercises a real, still-reachable tier-less arm (a
`WorkerError` built without going through `_error_for`, e.g. the synthetic no-result one in
`_drive` itself), with their docstrings corrected to stop calling that "the SHIPPED arm" now that
a second, tier-scoped arm also ships. Each gets a new sibling test
(`test_a_tier_outage_with_a_real_tier_writes_a_tier_scoped_finding`,
`test_a_real_tier_scoped_halt_excludes_a_different_tiers_contamination`) driving the identical
scenario with `tier=` supplied, asserting `failover_triggers_scope == "tier"` and the correctly
narrowed `failover_triggers_recorded`/`throttling_observed` — including the exact cross-tier
contamination scenario this entry's body describes (a CHEAP 429 planted, a HEAVY outage halts),
now proven CORRECTLY EXCLUDED once a real tier is known.
`tests/test_workers_scan.py::test_classify_takes_no_model_client_by_constructor_and_calls_the_one_on_the_context`
gets one new assertion, `result.error.tier is ModelTier.CHEAP`, on the real
`ClassifyWorker.run()` → `UnavailableModelClient` → `_error_for` path — the `_error_for`-population
half; the `test_runner.py` additions are the `_drive`-forwarding half, since `fails_with` bypasses
`_error_for` entirely.

**Rule 12, mutation-proved, not merely run.** Reverting `runner.py`'s `tier=failure.tier` forward
(restoring the pre-fix `record_backend_unavailable(repo_id=..., phase=..., observed=...)` call)
was confirmed to change the file (`git diff --numstat --no-index` against a pre-mutation backup,
non-empty) and reddened both new `test_runner.py` scenarios exactly (`failover_triggers_scope`
read `"run"` where each asserts `"tier"`), no other test in that file affected. Separately,
reverting `classify.py`'s `tier=exc.tier if isinstance(exc, TierUnavailable) else None` line was
confirmed to change the file and reddened the new `test_workers_scan.py` assertion exactly
(`result.error.tier` read `None` against an asserted `ModelTier.CHEAP`), no other test in that file
affected.

**`findings.py`'s own docstring corrected in the same commit (Guardrail 7).** Its "NOT REACHED IN
PRODUCTION TODAY" section asserted "nothing in `src/` passes `tier=`" and cited the pre-fix
`WorkerError` field list (`workers/base.py:359-374`, six fields, no tier) as the reason it could
not — both became false the moment this fix landed, so the section was rewritten to state the
narrowed arm is now live and to name the two callers (the synthetic no-result `WorkerError`, and
any future `BACKEND_UNAVAILABLE` `WorkerError` built without `_error_for`) that keep the run-scoped
arm real rather than dead.

**Scope, checked before closing this out.** Only D78 (this entry) is closed here. D62
(`attempts.llm_failovers`, `llm_backend`, `input_tokens`, `output_tokens`) is a separate task
(round Y task, disjoint files: `src/fleet/models/tasks.py`, `src/fleet/llm/client.py`,
`src/fleet/state/repository.py`, `src/fleet/cli.py`, `src/fleet/workers/base.py`'s `accumulate` —
corrected 2026-09-01, this list omitted `workers/base.py` even though D62 touched it) and is
untouched here, per this task's brief —
the research report this task followed (§4) established D78 and D62's `llm_failovers` leg are
independent fixes, not a dependency chain, despite sharing ADR-0094's "carry attribution on
`TokenUsage`" precedent.

**§12.43 (`docs/CRITERIA_PLAN.md` item 43) — checked, does not close any further sub-clause here.**
That entry's "audit row 43, D78" gap is about **message provenance**: *"`TierUnavailable.__init__`,
the sole producer of the 'names the tier and every target tried' message, is asserted by nothing;
every test checking that message writes the string itself rather than reading it from the
producer."* That gap is a DIFFERENT sub-clause from the `tier=` parameter this fix wires, and it
was already closed before this task started — `tests/test_runner.py` already constructed a real
`TierUnavailable` and read `str(tier_exc)` rather than a hand-written string in both tests this fix
touches, confirmed present at this task's base commit before any edit here. `docs/CRITERIA_PLAN.md`
item 43's "Done bar" prose (naming `test_runner.py:1613` and "add one test that constructs a real
`TierUnavailable`" as still-open work) is stale for that reason — a pre-existing doc-sync gap, not
introduced or corrected by this task, flagged for whoever's commit next legitimately touches that
entry. This fix's OWN new production-path coverage
(`test_classify_takes_no_model_client_by_constructor_and_calls_the_one_on_the_context`'s new
`tier` assertion) exercises the real `TierUnavailable` → `_error_for` chain but does not add a new
assertion on the message text itself, so it does not move item 43's message-provenance sub-clause
further than where round X already left it.

**Correction (2026-09-01, final-fix round) — two sentences in the "Fixed" addendum above claim the
synthetic no-result `WorkerError` in `runner.py`'s `_drive` is a live production producer of the
tier-less `BACKEND_UNAVAILABLE` arm; both are false, left in place per this file's own convention.**
The synthetic `WorkerError` at `runner.py:621-624` is constructed with `FailureClass.UNKNOWN`, not
`BACKEND_UNAVAILABLE`, and the gate on `runner.py`'s only `record_backend_unavailable` call site
(`:705`, guarded by the check at `:675`) is `failure.failure_class is
FailureClass.BACKEND_UNAVAILABLE` — so that synthetic error can never reach it. Separately,
`classify.py::_error_for` (`:248`) is the sole site in `src/` that
assigns `FailureClass.BACKEND_UNAVAILABLE` to a `WorkerError`, and it does so only via
`isinstance(exc, TierUnavailable)`, whose `tier` constructor parameter is non-optional — so every
production `BACKEND_UNAVAILABLE` `WorkerError` now carries a `tier`. Net effect: as of this fix,
the run-scoped (`tier=None`) arm has ZERO production producers — the exact mirror image of the
pre-fix state this entry closes. This does not reopen D78 (the tier-scoped arm this entry proves
is genuinely wired and live) and does not mean the tests naming the synthetic `WorkerError` as
their reachable case should be deleted — they remain legitimate regression guards for a
hypothetical future caller that constructs a `BACKEND_UNAVAILABLE` `WorkerError` without going
through `_error_for`. `src/fleet/orchestrator/findings.py`'s docstring and the two
`tests/test_runner.py` sites this addendum describes as having been "corrected" have themselves
been corrected again, in this round, to state this accurately.

---

## D79 — FIXED, LANDED (`cac537d`). SPEC §11.6's LLM response cache is entirely inert: no `LlmCacheStore` implementation is constructed anywhere in `src/`, so every production call is billed and no `llm_cache` row is ever written

**Measured by lane W30 (round E) at `1698de3`; found by lane R3, whose every figure repeated below
was re-measured here before being repeated, and whose figures that were *not* re-measured are
labelled as R3's.** Interpreter for every number: `.venv/bin/python` (the binary, not the PATH
name). **No `pytest` session was run — suite lock.** The full research, including the C1–C7
decomposition this entry deliberately does not restate, is promoted to
`docs/superpowers/plans/llm-cache-inert-research.md`.

### The defect, as exercised rather than inferred

A `RunContext` built with **exactly the kwarg set every `RunContext(` site in `cli.py` passes**
assembles a `LadderModelClient`. A second arm — the same construction plus an injected
`llm_cache=` store — assembles a `CachingModelClient`. The second arm is a **positive control**: it
exists so that "no cache was consulted" in the first arm cannot be an instrument that simply cannot
see a cache. Both arms driven by W30 in a per-lane detached worktree with `PYTHONPATH` pinned to
that worktree's `src/`:

| | shipped (`cli.py`'s kwarg set) | control (`llm_cache=` injected) |
|---|---|---|
| `type(ctx.model_client)` | **`LadderModelClient`** | `CachingModelClient` |

R3 drove the same two arms through two identical `complete()` calls against a scripted offline
backend and reports **2 backend invocations and 0 `llm_cache` rows** on the shipped arm against
**1 and 1** on the control — *R3's numbers, promoted at
`docs/superpowers/plans/llm-cache-inert-research.md` §1, not re-measured by W30.* The type result
above is W30's own and is sufficient for the entry: the cache client is the only thing that could
consult or write the table, and it is never assembled.

### The static class results behind it, W30's own predicate

Instrument: `ast.Call` nodes resolved by dotted name over every `*.py` under `src/` (115 files) and
`tests/` (62 files), so a wrapped or multi-line call counts identically to a one-liner. Class
results, at `1698de3`:

* **0 of the 2 `LlmCacheStore` implementations are constructed in `src/`.** `SqliteLlmCacheStore(`
  and `MemoryLlmCacheStore(` have **0** call sites in `src/`; in `tests/` they have 2 and 13.
* **`llm_cache` and `llm_cache_mode` are passed at 0 of the 5 `src/` `RunContext(` sites** (all five
  in `cli.py`, in `_run_scan_wave`, `_run_transform_wave`, `_run_build_wave`, `_run_verify_wave` and
  `_emit_prs`), and at 0 of the 3 sites in `tests/`.
* **`cli.caching_client` has 0 callers in `src/`** and exactly 1 in `tests/`
  (`tests/test_cli.py::test_llm_cache_flag_reaches_the_caching_client`).
* `CachingModelClient(` has 3 call sites in `src/`, and **every one is unreachable in production**:
  `CachingModelClient.scoped`, which returns a per-rung view of an already-constructed client; the
  `self.llm_cache is not None` branch of
  `RunContext.__post_init__`, dead because the field is `None` at all five sites; and
  `cli.caching_client`, dead because nothing calls it.

**`llm_cache_mode` is never assigned and never derived.** `RunContext.llm_cache_mode` carries the
default `"read-write"`, is read at exactly one place — the dead `__post_init__` branch — and is
never constructed from `config.llm.cache_mode`. `LlmSection.cache_mode` and `LlmSection.cache_path`
in `settings.py` have **zero readers in `src/`**, so the config leaf is inert on its own account as
well as through the field.

What follows, and was checked: `--llm-cache {read-write,read-only,off}` is parsed and echoed by
`fleet models list --json` and installs nothing; `--llm-cache read-only` — §11.6's replay mode, in
which a miss must be a hard error — cannot raise on any shipped path, because nothing constructs
the client that raises (the only `raise CacheMiss` in `src/` is inside
`CachingModelClient.complete`); and `cli._gc_impl` executes
`DELETE FROM llm_cache WHERE last_hit_at < ?` for `fleet gc --cache-max-age` against a table no
production path has ever written a row to. SPEC §12 item **21** (Determinism), §12.36 and §12.44, and §13 rows 11 and 39 all
rest on this cache and are unsatisfiable as written.

### Would a test catch it? MEASURED — no, and the instructive part is *why*

`tests/test_cli.py::test_llm_cache_flag_reaches_the_caching_client` is the only test in the suite
whose name claims the flag reaches the cache. It does not catch this, and W30 measured that rather
than arguing it. Every mutation below was applied in a per-lane detached worktree against a **file
backup, not `HEAD`**; the zero-change gate is `git diff --numstat --no-index BACKUP MUTATED` and was
**read before** the test result; and the interpreter's import path was pinned to the mutated tree
and verified by asserting the mutation marker's presence or absence in
`inspect.getsource(RunContext.__post_init__)` — without that pin the `.venv` editable install
resolves `fleet` to the primary checkout and the mutation is never imported at all (R3 recorded
that trap after producing one clean-looking wrong answer with it).

| check | mutation | gate (lines) | did it change behaviour? | the test |
|---|---|---|---|---|
| baseline | none | — | — | **PASS** |
| **known-bad** | **M1** — delete the `if self.llm_cache is not None:` branch from `RunContext.__post_init__`, i.e. §11.6's only production consumption point | +1 / −8 | **yes, measured**: the control arm goes `CachingModelClient` → `LadderModelClient` | **PASS** |
| discriminating control | **M2** — `caching_client` hardcodes `mode="read-write"` instead of `opts.cache_mode` | +1 / −1 | yes | **FAIL** |
| cosmetic control | reflow `caching_client`'s `CachingModelClient(` call onto fewer lines, no semantic change | +2 / −5 | no | **PASS** |

M2 and the reflow are there so the finding is not "the test is vacuous" and not "the test asserts
layout": it really does hold the helper's flag→`_mode` mapping, and it is not disturbed by
formatting. **M1 is the finding. Deleting the entire production cache path leaves it green.** The
reason is that the test never constructs a `RunContext` at all — it calls `cli.caching_client`
directly, and that helper has zero callers in `src/`. **Its name is true of a helper nothing calls
and false of any run**, which is CLAUDE.md's *"a test can pass, and pass under mutation, while the
property in its name is false"* in its purest available form. The test's first half is weaker still:
it asserts `fleet models list --json` echoes `llm_cache: "off"`, which is a **report** of the flag's
value, not an installation of anything.

**The detector for this entry must therefore be behavioural, not a `grep`** — the D58 shape applies
here. After a fix, `git grep "llm_cache=" -- src/` still returns zero, because the intended remedy
derives the store inside `__post_init__` rather than passing it at a call site. The detector is the
two-arm probe above: a `RunContext` built with `cli.py`'s kwarg set must yield a
`CachingModelClient`, and two identical calls must bill once and leave one row.

### Precedent and relations

* **D56 — FIXED, LANDED (`c36160e`) — is the landed precedent for the identical shape**:
  `llm.client.discover()` had zero call sites in `src/`, so the backend registry could never be
  populated. A dependency-injection parameter that exists, is documented, and is never supplied is a
  defect class this register has already adjudicated, and never by deletion. D57 is the same class.
* **D61 — FIXED, LANDED.** Its subject is a read/write key disagreement **inside**
  `CachingModelClient` — a component no shipped run constructs. D61 was true and correctly fixed at
  its own commit; this entry only adds the layer under it.
* **D62 — OPEN.** `record_attempt`'s `INSERT` omits `llm_cache_hit`. This entry adds that the column
  would read 0 even once written, because there is no cache to hit.
* **D50 — OPEN.** `tests/test_config_keys_are_read.py`'s `KNOWN_INERT` already holds
  `fleet.yaml:llm.cache_mode` and `fleet.yaml:llm.cache_path`, so the tree half-knows this. That
  allowlist is **not** a D58-shaped detector: the moment `config.llm.cache_mode` is genuinely read,
  `test_known_inert_keys_are_still_inert` fails loudly and correctly, and a fix must retire those
  two entries in the same commit.

### Disclosure — recorded here, deliberately NOT acted on, and it must reach round F

**Four statements of the `llm_cache` cache key exist in the tree and `docs/SPEC.md` §11.6 is the
only dissenter.** Class result, W30's own, at `1698de3`: `llm/cache.py::CacheKeyParts.compute`,
`state/schema.sql`'s `llm_cache.cache_key` column comment and
`models/tasks.py::LlmCallRecord.cache_key`'s `description` all name
`… | prompt_sha256 | prompt_template_version | response_schema_sha256 | adapter_versions` and all
three state that `harness_version` is deliberately excluded because it changes every patch release
and would re-pay for the whole fleet. **§11.6 carries `harness_version` and omits
`prompt_template_version`** — wrong in exactly two slots, 3 agree / 1 dissents.

**This is the "SPEC says X but the code cannot do X" shape and the direction of the fix matters: a
reconciler making the code match §11.6 would re-key the entire corpus and invalidate every cached
answer the harness ever writes.** §11.6 is the sentence that must move, not the three code
statements. It is one SPEC edit, it is **not** part of the wiring fix, and it is C6 of the promoted
brief. Separately, §11.6's cross-reference to "§12.20" is off by one — Determinism is §12 item
**21**; item 20 is "Secrets never leak". Both are reported, not edited, by this entry.

### The ruling

**Recorded, not fixed, in round E.** Wiring the cache is a new workstream off the resume-step-5
critical path; starting it in this round would displace subtask 10 and the round-close verification.
Round F takes it, from `docs/superpowers/plans/llm-cache-inert-research.md` §7 (C1–C7) — which also
carries the trap that `--llm-cache` is declared with a **non-optional default**, so an unconditional
override would substitute `read-write` over an operator's `cache_mode: off`, character-for-character
the `effort: low` failure CLAUDE.md records.

### What this entry does not establish

* **No suite run** (round-E suite lock). Every runtime number here is from standalone
  `.venv/bin/python` scripts driving library code and one test callable directly. The pytest command
  a later lane must run, with no `-k` filter, is in the promoted brief's §7.
* **The `src/`-caller results are static sweeps.** A cache installed through `importlib`/`getattr`
  with a computed name would be invisible to them. The two-arm exercise is the answer to that
  objection: whatever the static picture, the shipped assembly demonstrably assembles no cache.
* **The billing and row-count figures are R3's**, cited above as R3's and not re-measured by W30.
* **§11.6's reading is R3's and W30's**, not a third party's; §11.6 and §12 items 21/36/44 are
  quoted at length in the promoted brief precisely so a reader can check the reading.

*(2026-08-25, round F lane W11 — **status field updated; the body above is untouched.** The defect
this entry records is fixed on `main` at `cac537d`, whose own subject line names it: *"llm cache:
wire SPEC §11.6's response cache, which shipped entirely inert (D79 C1-C4)"*. It lands C1–C4 of the
promoted brief. `RunContext.__post_init__` now derives a `SqliteLlmCacheStore` and wraps the ladder
client **unconditionally** — `src/fleet/orchestrator/context.py`, the `store = SqliteLlmCacheStore(…)
if self.llm_cache is None else self.llm_cache` / `client = CachingModelClient(…)` pair at `:228-241`
at `b5f7760` — and `llm_cache_mode` became `CacheMode | None`, so an unset field means *"not given"*
rather than an override of an operator's `cache_mode: off`. That is the `effort: low` trap this entry
predicted, avoided. **No `RunContext(` call site changed.**

**The detector is behavioural, and it is NOT a `grep` — re-measured at `b5f7760`.** The class result
this entry rests on is unchanged by the fix and that is the point: an AST sweep resolving
`RunContext(` by dotted name finds **5 call sites in `src/` and 5 in `tests/`, and `llm_cache=` and
`llm_cache_mode=` are passed at 0 of the 10**, before the fix and after it. A caller-side probe of
any shape is blind here by construction, which is the D58 shape this entry named.

**One raw total in the body above does not reproduce, and it is the grep figure.** The body predicts
*"After a fix, `git grep "llm_cache=" -- src/` still returns zero"*. Measured at four anchors —
`1698de3` (this entry's own), `cac537d^`, `cac537d` and `b5f7760` — that command returns **1 line in
1 file at every one of them**: `src/fleet/cli.py`'s `GlobalOptions(… llm_cache=llm_cache …)`, the
`--llm-cache` flag being carried into the options object, which is not a `RunContext(` kwarg and was
never the subject. So the figure was false when written and is still false; the **class** claim it
was standing in for — no `RunContext(` site passes the field — reproduces exactly. Guardrail 6's
"prefer a class result to a raw total", demonstrated on this entry's own text. *The body is left as
written: it records what its author measured at its own commit.*

**Instrument validated against the known-bad state.** `tests/test_run_context_llm_cache.py` reads a
call log off a scripted offline backend and a row count out of a real temp database — never a type
or a field comparison alone. Run by this lane in a per-lane detached worktree at `b5f7760` with
`cwd` inside it (pytest's `pythonpath = ["src"]` plus `tests/conftest.py`'s `sys.path.insert` make
that tree the imported one), **no `-k` filter, file-scoped, not a whole-tree run**: **4 passed, 0
failed**. The shipped-kwarg arm bills `['only']` — one backend call for two identical `complete()`
calls — and leaves **1** `llm_cache` row. Restoring `src/fleet/orchestrator/context.py` from
`cac537d^` in the same worktree, zero-change gate `git diff --numstat --no-index BACKUP MUTATED` =
`12 31` read **before** the result, takes it to **3 failed, 1 passed**, with the shipped arm billing
`['only', 'only']`. Reported per case rather than as a count, because that is what discrimination
means: the revert reddens `test_two_identical_calls_bill_the_backend_once_and_write_one_row`,
`test_cache_mode_read_only_in_fleet_yaml_makes_a_miss_fatal` and
`test_an_explicit_llm_cache_mode_still_overrides_the_config`, and does **not** redden
`test_cache_mode_off_in_fleet_yaml_bills_every_call_and_writes_nothing` — which is blind to it by
construction, since `off` and "no cache at all" are indistinguishable at the backend. The `0`-row
figure for the pre-fix shipped arm remains **R3's**, labelled as such in the body: the known-bad run
aborts at the call-log assertion before reaching the row count, so this lane did not re-derive it.

**Not closed by `cac537d`, and both were disclosed by the fix commit itself.** (i) C1 does not pass
`on_hit` into the caching client — `CachingModelClient.__init__`'s `on_hit` parameter
(`src/fleet/llm/cache.py:412`, stored at `:425`, invoked at `:560-561`) receives nothing from
`__post_init__` — so `attempts.llm_cache_hit` still reads `0` on a hit. See the marker on **D62**,
which stays `OPEN`. (ii) C5–C7 are not in this commit: §11.6's cache-key sentence still carries
`harness_version` and omits `prompt_template_version` against three agreeing code statements, and its
"§12.20" cross-reference is still off by one. The disclosure block above is unacted and still stands.

**A separate D-number was allocated for this fix and is deliberately NOT used.** Round F's dispatch
allocated **D81** for "SPEC §11.6's LLM cache shipped entirely inert — FIXED, LANDED (`cac537d`)".
That is this entry's defect and not a second one; `cac537d` names D79 itself. By the "Status
vocabulary, used strictly" block above and by the ruling recorded at `39862ec`, this file records a
fix by setting the heading's status **field**, not by opening a duplicate entry — so the heading
moved and **D81 remains free**.)*

*(2026-08-25, round G lane W4 — **annotation only, on the fix annotation above. Not one word of it
is rewritten and this entry's heading is untouched: `FIXED, LANDED (`cac537d`)` still holds.** This
marker governs the paragraph beginning *"**Not closed by `cac537d`**"*, and specifically its clause
**(ii)**.*

***(ii) has been FALSIFIED by a later commit in its own round; (i) re-measures TRUE.*** (ii) states
that *"§11.6's cache-key sentence still carries `harness_version` and omits
`prompt_template_version` … and its '§12.20' cross-reference is still off by one"*. **Commit
`f54dac8` fixed both halves**, and this lane re-derived that at `8b40498`: `docs/SPEC.md:7136-7138`
now states the key as `sha256(role | tier | backend | model_id | effort | context_policy |
rejected_approach_digest | prompt_sha256 | prompt_template_version | response_schema_sha256 |
adapter_versions)` — ten components, with **`prompt_template_version` present** and
**`harness_version` absent** — and `:7200` now cross-references **§12.21**, whose item 21 is
**Determinism** (`:7355`). The cross-reference is no longer off by one.

***Why this marker earns its space rather than being cosmetic.*** `docs/SPEC.md:7140-7141` now
carries its own guard — ***"`harness_version` is deliberately not a key component, and must not be
re-added to make code match a longer list"***. A later lane "reconciling" §11.6 against (ii) **as
written** would **re-add `harness_version`**, reintroducing the exact defect `f54dac8` removed, on
the authority of a ledger entry. That is CLAUDE.md's "documents are inputs to future edits" hazard
with a live trigger, and it is the whole reason for this annotation.

***(i) is untouched and still true at `8b40498`.*** `CachingModelClient.__init__` still takes
`on_hit` (`src/fleet/llm/cache.py:412`, stored at `:425`, invoked at `:560-561`), and
`RunContext.__post_init__` still constructs the client **without** it
(`src/fleet/orchestrator/context.py:235-243`) — so `attempts.llm_cache_hit` still reads `0` on a
hit. See **D62**, which stays `OPEN` and now carries the semantics ruling that leg needs.

***Provenance and limits.*** Found by the round-F whole-branch review (lane CR1) at `635a83c`;
every figure above re-derived by this lane at `8b40498` before being written. No code changed and no
suite was run for this marker.)*

## D80 — FIXED, LANDED (`5377969`, merged `9c20eeb`). `docs/SPEC.md` §10 orders a `stub_reconcile` step inside `fleet resume` that no code performs, so `fleet resume` has TWO absent steps and not one — "step 8 is the sole remaining absence" is true of §11.5's numbered list and false of §10's row

**Found by lane R4 (round E) while costing subtask 10; recorded by lane W35, which re-measured
every figure below before repeating it and labels the two it did not.** Interpreter for every
number: `.venv/bin/python` (the binary, never the PATH name). Anchor **`9bf15bb`**, with every
`src/` figure re-measured at **`f36c9ad`** as an independent second ref. All `src/` and `tests/`
figures are read from `git show <ref>:<path>` blobs and never from the working tree. **Five commits
landed on `main` while this entry was being written** — `81b3b55` → `9bf15bb`, two of them touching
`src/fleet/cli.py` and `tests/` — so **every figure below was re-measured at `9bf15bb` after they
landed**, and every one reproduced unchanged. **No `pytest` session was run — round-E suite lock.**

### The three SPEC sites that mandate it, quoted rather than summarised

* **§10's CLI-surface table, the `fleet resume` row** — the verb's positive specification. Its
  activity list reads *"… recompute `blocked_by`, **re-run `stub_reconcile`** (§3.5.1: re-derive
  every `stubs` row's state from `repos`/`phases`/PR state and re-enqueue any revalidation round
  lost to the crash under its `revalidation_key` — idempotent, so a resume never doubles the
  rework), regenerate `migration_state.json`, continue from each repo's re-entry floor …"*. The
  step sits **between** §11.5's step 6 and step 7.
* **§13 row 35** (*"A stub is never resolved and the fleet ships it anyway"*): *"`stub_reconcile`
  runs before the final checkpoint **and again in `fleet resume`**"*.
* **§3.5.1**, the end-of-run reconciliation paragraph: *"Before the runner writes its final
  checkpoint it executes a `stub_reconcile` step (**also part of `fleet resume`'s reconciliation,
  §11.5**)"*.

**And the one site that does not: §11.5's numbered list.** Its closing paragraph runs
*"… (7) regenerate `migration_state.json` from SQLite; (8) continue. Steps 1–7 make no network
call and invoke no model …"*, with no stub step anywhere in the enumeration. **That asymmetry is
the whole entry.** Read against §11.5's list, step 8 is the only thing missing. Read against §10's
row, `stub_reconcile` is missing too, and no numbered step names it.

### The absence, measured

Instrument: `ast` over **every** `.py` blob under `src/` — **115** of them at both refs — resolving
`ast.Call` by `Name.id` or `Attribute.attr`, so a wrapped or multi-line call counts identically to
a one-liner, and walking `ast.Import` / `ast.ImportFrom` for any module or imported name containing
`stubs`.

| quantity | `9bf15bb` | `f36c9ad` |
|---|---|---|
| `.py` blobs under `src/` | 115 | 115 |
| calls named `reconcile` or `stub_reconcile` | **0** | **0** |
| imports of any module whose name contains `stubs` | **0** | **0** |

**The implementation exists and nothing calls it.** `src/fleet/orchestrator/stubs.py` defines
`reconcile` (alongside `supersede`, `plan_revalidation`, `settle_revalidation`,
`abandon_by_operator`, `apply`). The module is imported **once in the whole tree**, by
`tests/test_stubs.py`, which calls `reconcile` **9** times. So this is not a missing function; it
is a missing **wiring** — the shape D79 records for D56 and D57, *"a dependency-injection
parameter that exists, is documented, and is never supplied"*, which this register has adjudicated
before and never by deletion.

**The tree already says so, in a committed comment.** `cli._resume_impl` carries a comment block at
the insertion point: *"`stub_reconcile` (§3.5.1, §13 row 45) **BELONGS HERE** — immediately
below the re-poll and above the step-3 sweep — and nowhere earlier"*, ending *"**It does not exist
on `main` yet**; when it lands, it goes on the next line, with that decision made explicitly rather
than inherited from this ordering."* The comment also states the design question its author
declined to settle: `stub_reconcile` judges PR state, and `--repoll-prs` is opt-in, so whoever
lands it must decide what it does when `repoll != "polled"` — gate on it, or make `--repoll-prs`
implied. **That question is part of this entry's cost and is not answered here.**

### Would a test catch it? MEASURED — no, and the measurement is a two-armed one

Predicate, over the **62** `.py` blobs under `tests/` at `9bf15bb`, applied to each `test_*`
function's `ast.get_source_segment` (so a wrapped assertion is inside one segment and cannot be
split by a line boundary — this is why it is not a `grep`):

* **arm A**, "invokes the verb": the segment matches `"resume"` / `'resume'` / `cli.resume` /
  `_resume_impl`;
* **arm B**, "touches stub state": the segment matches `StubState` / `UnresolvedStub` /
  `FROM stubs` / `INTO stubs` / `stub_reconcile`.

| | count | files |
|---|---|---|
| arm A alone | **56** | 3 (`test_cli.py`, `test_resume_unblocking.py`, `test_state_models.py`) |
| arm B alone | **26** | 7 (`test_blocked_by_writer_statements.py`, `test_cli.py`, `test_migrations.py`, `test_projection.py`, `test_state_models.py`, `test_stubs.py`, `test_workers_build.py`) |
| **A ∧ B** | **0** | — |

**Both arms fire on their own and their intersection is empty**, which is what makes the zero a
measurement rather than a blind instrument: the predicate demonstrably *can* see stub state (26
hits) and *can* see the verb (56 hits), and no test in the suite does both. A looser predicate —
the bare word `stub`, case-insensitively, beside `resume` — returns **8** functions; every one was
read, and all eight are word overlaps. **2** of the 8 invoke the verb and use "stub" only as prose
(*"the §3.5.1 T1 stub trigger"*; *"edit a file, a flag or a stub before retrying"*); the other
**6** never invoke it — one uses `stub_log: Any = _StubLog()` as the name of a test double, and
five are `test_stubs.py` / `test_state_models.py` / `test_repository.py` tests in which "resume"
is an ordinary English word. **Nothing binds `stub_reconcile` to `fleet resume`, so deleting the §10 row's
clause and deleting a hypothetical implementation are indistinguishable to this suite.**

**The detector after a fix may be static here, unlike D79's.** The intended remedy inserts a call
at the marked point in `cli._resume_impl`, so the `0 → 1` call-count above is a real signal. The
*sufficient* detector is still behavioural: seed a run with an open `stubs` row, run
`fleet resume`, and assert the row moved to `ABANDONED` with an `UnresolvedStub` finding and that
`migration_state.json#unresolved_stubs` names it — that is the §13 row 35 contract, and it is also
the check that would notice a call placed above the re-poll.

### Why this matters beyond bookkeeping, and what it does **not** do to ADR-0079

ADR-0079's title and §6 rule that the un-refusal of `--from-phase` / `--repo` ships with subtask 10
*because* step 8 is the only thing left for a scoping flag to scope. **That statement is scoped, in
this round's same commit, to §11.5's numbered list, and §10's row is disclosed as this ninth
absence. The RULING is unaffected and holds *a fortiori*:** §6 turns on there being nothing to
scope, and two absences are strictly less to scope than one. ADR-0079 §6's own premise paragraph
already argues the two-absence case explicitly, for `f4eade0`. **This entry is not grounds to
reopen the un-refusal question**, and it must not be read as one.

What it *does* change is the argument available to subtask 10. `docs/DECISIONS.md` ADR-0076's
standing bullet says `ResumeIncompleteError` must be **deleted, not repurposed**, once the verb can
do what it owes, and W7's 2026-08-21 amendment conditions that on *"steps 6 and 8 both"* existing.
Step 6 exists. If §10's row is authoritative for what the verb owes, then after step 8 lands the
verb still will not do everything §10 names, and ADR-0076's bullet would be discharged by a
**second message rewrite** rather than by a deletion.

### What this entry deliberately does NOT decide

* **Which source is authoritative for "what `fleet resume` owes" — §11.5's numbered list, or §10's
  row.** R4 states the two branches and this entry repeats them without choosing: **(a)** §11.5's
  list governs and §10's row is prose, in which case an ADR must say so, because three sources
  currently read the other way and none says this; or **(b)** `stub_reconcile` is a real ninth
  obligation, in which case `ResumeIncompleteError` survives subtask 10 with its message rewritten
  to name `stub_reconcile` instead of step 8. **The branches produce different subtask-10 file sets
  and different tests**, which is why this is recorded as open rather than resolved by a worker.
* **The `repoll != "polled"` gating question** the `_resume_impl` comment states.
* **Ownership.** **Nothing owns this.** Measured at `9bf15bb` from the `git show HEAD:` blob:
  `stub_reconcile` occurs **9** times in `docs/superpowers/plans/design-resume-step5.md`, and
  **none of them makes landing it a deliverable of any row**. Exactly **one** of the ten task rows
  names it — row 7, and only to require the opposite: *"the RS1 `stub_reconcile` marker comment is
  left above, untouched"*. The other eight sit where nobody is being assigned anything: **2** in the
  landed-branch provenance table at the head of the file, **1** under *"Order inside
  `_resume_impl`"*, and **5** in the *"`--repoll-prs` → `stub_reconcile` ordering constraint"*
  section (its heading and four body lines), which reasons about **where** the step would go and
  never about **who writes it**. *(This bullet
  originally asserted that no row named `stub_reconcile` at all; that was false and was caught by
  re-running the sweep against this entry's own text before it was committed. Row 7's mention is a
  non-ownership clause, which is the weaker and true claim.)* Design row 10 was **not** given it: row 10 is step 8
  plus the two-flag un-refusal, and widening it to a step §11.5 never numbered would be exactly the
  "assign by symmetry" move CLAUDE.md records as a dispatcher failure. **It needs an owner and a
  ruling on (a)/(b) before it can be dispatched, in that order.**

> **Ruling 2026-08-30 (round M controller, ADR-0098) — both (a) and (b) decided; this entry's
> analysis stands unedited above.** **(a)** §11.5's numbered list is incomplete, not authoritative
> over §10/§13 row 35/§3.5.1: `stub_reconcile` is a real ninth obligation of `fleet resume`, three
> sites require it against one silent list omission. See `docs/SPEC.md` §11.5's own marker,
> immediately after its numbered list, for the full reasoning — deliberately not renumbered as a
> step, for the citation-stability reason given there. **(b)** `reconcile()` runs unconditionally
> at the marked `_resume_impl` insertion point regardless of `repoll`'s value — not gated on
> `repoll == "polled"`, and does not imply `--repoll-prs`. Rationale for both in ADR-0098.
> **Consequence flagged, not resolved here:** since (a) makes `stub_reconcile` a real obligation,
> `docs/DECISIONS.md` ADR-0076's "`ResumeIncompleteError` deleted once steps 6 and 8 both exist"
> bullet needs revisiting once this round's wiring lands — per this entry's own analysis, the
> verb still won't do everything §10 names until `stub_reconcile` is *also* wired (not merely steps
> 6 and 8), so deletion may need to become a message rewrite instead. Left for whoever owns
> subtask 10; round M's task 2 is scoped to the wiring only, not to ADR-0076's bullet.
> **Owner, now:** round M task 2 (implementer agent `ada3bb9a965bcc4f5`).
> **Landed `5377969`, merged `9c20eeb`, reviewed Approved (no Critical/Important findings; every
> checkable claim independently reproduced, including the exact assertion-failure text under the
> rejected-design mutation). Heading updated above.**

### What this entry does not establish

* **No suite run** (round-E suite lock). Every figure is from standalone `.venv/bin/python` scripts
  over `git show` blobs, plus one runtime import of `fleet.orchestrator.reentry` with
  `fleet.__file__` asserted against this checkout's `src/`. The pytest command a later lane must
  run, with no `-k` filter, is `.venv/bin/python -m pytest tests/test_stubs.py tests/test_cli.py
  tests/test_projection.py`.
* **The `src/` results are static sweeps.** A `stub_reconcile` reached through `importlib` or a
  computed `getattr` would be invisible to them. The committed `_resume_impl` comment saying *"It
  does not exist on `main` yet"* is the independent second source, and it is the implementer's own.
* **The reading of §10 against §11.5 is R4's and W35's**, not a third party's. All four passages are
  quoted above at length precisely so a reader can check the reading rather than inherit it.
* **Two figures are R4's and were not re-measured here**: that `cli.resume`'s final statement is the
  unconditional `raise ResumeIncompleteError`, and R4's 27-test blast-radius count for subtask 10.
  Neither is load-bearing for this entry.

---

## D81 — PARTLY ADDRESSED (SPEC half corrected by `4398358`, whose 2026-08-25 marker at the end of this entry, which also names the leg still open). `docs/SPEC.md` §5's retention paragraph assigns `PRAGMA wal_checkpoint(TRUNCATE)` to "the single projector task", and no code under `src/` issues it — a named duty with no implementation, which the C7 projector wiring does **not** create

**Found and measured by lane W13 (round F) while ruling on D79's task C7 (`RunContext.projector`),
at `01ac4ca` and re-derived unchanged at `ee1ddc8`. Static sweeps plus one runtime read of the
shipped pragma list — NOT an exercised WAL-growth reproduction, and this entry says so rather than
implying one.**

`docs/SPEC.md`'s retention-and-on-disk-ceiling paragraph (§5, at `docs/SPEC.md:5027` at `ee1ddc8`;
anchor on the sentence, not the line — it names `fleet gc` and the **< 2 GB** steady-state ceiling
in the same block) reads:

> The single projector task (below) issues `PRAGMA wal_checkpoint(TRUNCATE)` after each debounce
> window, which is what stops a long-lived reader from pinning the WAL until the disk fills mid-run.

**Measured two genuinely different ways at `ee1ddc8`, both returning zero:**

1. `grep -rniE 'wal_checkpoint' src/` → **0 matches.**
2. A whitespace-normalised whole-file sweep for `wal[ _]*checkpoint` (case-insensitive) over every
   `*.py` under `src/`, offsets mapped back to line numbers — the predicate that catches a
   line-wrapped or oddly-spaced occurrence a line-oriented `grep` would miss → **0 matches.**

`Projector._loop` (`src/fleet/state/projection.py`) issues no PRAGMA of any kind: its debounce body
is `project_once` plus a `writes` increment. The **only** WAL bound anywhere in the tree is
`PRAGMA wal_autocheckpoint = 1000` in `src/fleet/state/db.py`'s per-connection pragma list (also
recorded, commented out, in `src/fleet/state/schema.sql`'s header). The same normalised sweep over
`docs/SPEC.md` returns **1** occurrence — the sentence quoted above — so the SPEC states the duty
exactly once and nothing implements it.

**Why this is recorded now, and why it is NOT closed by the C7 landing.** C7 asked whether §6's
≤ 1 Hz debounced projection is wanted, and W13 ruled Arm A (wire it): before the fix, a wave's
transitions refreshed `migration_state.json` **zero** times, measured by driving `cli`'s own
composition root and watching the file. That fix constructs and starts a real `Projector` in each of
the four wave functions — so after it lands, "the single projector task" finally exists on a shipped
run. **It still issues no `wal_checkpoint`.** Recorded as its own number rather than as an
annotation on the C7 commit precisely because an annotation on a commit that does not fix it is how
a reader comes to believe it was fixed: with the projector now live, the SPEC sentence acquires a
subject it never had, and its verb is still unimplemented.

**Deliberately not decided here — the entry records the gap, not its remedy.** Two readings are
open and this lane took neither: (a) the SPEC sentence is the design and the projector should issue
the checkpoint, in which case `Projector._loop` gains it and this becomes a code fix; or (b)
`wal_autocheckpoint = 1000` is the harness's actual and sufficient WAL bound and the SPEC sentence
should be corrected to say so, in which case it is a SPEC fix. **The measurement that would decide
between them was not taken**: whether a long-lived reader in this harness can in fact pin the WAL
past the autocheckpoint threshold. `state/db.py` opens a `mode=ro` `read_conn` that lives for a
whole command, and SQLite's passive autocheckpoint is the one a reader can block — but passive
checkpointing is blocked by an open **read transaction**, not by an idle connection, and nobody has
measured which of those the shipped `read_conn` presents during a long wave. Whoever takes this must
measure that first; both remedies are wrong if the premise is.

**Would a test catch it? MEASURED — no.** `grep -rniE 'wal_checkpoint' tests/` returns **0**. No
test in the suite asserts on WAL size, on checkpoint behaviour, or on the projector issuing any
PRAGMA. `tests/test_projection.py` (10 checks at `53e5d8d`) drives the debounce, the coalescing, the
atomic write, the failure re-raise and the non-blocking of the single writer — and none of them can
observe a PRAGMA the loop does not issue.

**What this entry does not establish.** No `pytest` session measured it and no WAL file was grown.
Every figure above is a static sweep or a source read of the working tree at `ee1ddc8`, plus the
`db.py` pragma list read as text. A checkpoint reached through a computed string or an `executescript`
of a file this lane did not read would be invisible to both sweeps — though `wal_autocheckpoint`
being the *only* WAL-adjacent pragma found by a case-insensitive `PRAGMA[^"']*wal` sweep over `src/`
is the independent second source that makes that unlikely.

---

**2026-08-25 — status marker, lane W8 (round G), at `f6a2e4e`. Nothing above is rewritten.** Every
measurement W13 recorded was true at `ee1ddc8` and re-derives unchanged at `f6a2e4e`; the paragraph
above that says the deciding measurement "was not taken" is a correct record of what was true when
it was written. It has since been taken — by lane R3 (round G) and independently again here — and
this marker records the outcome, the fix, and the leg the fix does **not** close.

**The SPEC sentence is corrected, not deleted, in the same commit as this marker.** R3 measured that
its *causal* half is TRUE: a held read transaction defeats `wal_autocheckpoint` completely. Only the
*attribution* was false, and the severity with it. Deleting the sentence would have removed the
tree's only written statement of a real hazard, which this project treats as worse than an honest
disclosure. The replacement names `PRAGMA wal_autocheckpoint = 1000` as the actual bound, states
that a held read transaction defeats it, records that `build_state`'s `BEGIN DEFERRED` is the tree's
only read transaction and is debounce-bounded, and drops *"until the disk fills mid-run"*.

**Re-measured here at `f6a2e4e`, not inherited from R3.**

* `wal_checkpoint` under `src/` → **0**, two genuinely different instruments: a whitespace-normalised
  whole-file sweep over all 115 `*.py`, and an `ast` walk over 10 557 string literals whose positive
  control finds the one `wal_autocheckpoint` literal (`state/db.py:98`). `tests/` → **0**. All of
  W13's static figures reproduce.
* Exactly **three** executed `BEGIN` sites under `src/`: `migrations/__init__.py:228` EXCLUSIVE,
  `state/db.py:459` IMMEDIATE, `state/projection.py:227` `BEGIN DEFERRED` (moved from `:194` by
  ADR-0106's `_derive_updated_at` addition) — the only reader.
  `project_once` (`state/projection.py:375-389`, moved from `:341-353` by ADR-0106's
  `_derive_updated_at` addition) opens and closes that handle around one snapshot.
* **Runtime, own fixture** (SQLite 3.45.1, ext4, 1 000-row `phases` table, 8 000 write transactions
  per arm, a **unique** 400-byte payload per transaction): shipped pragmas with no reader plateau at
  **4 120 032 B**, flat from txn 1 000; `wal_autocheckpoint = 0` grows linearly to **33 437 952 B**
  (**8.12×**); **shipped plus one held `BEGIN DEFERRED` is byte-identical to the disabled arm at all
  eight samples**, with `wal_checkpoint(TRUNCATE)` returning `(1, 8116, 116)` and reclaiming nothing
  while the reader is open, then `(0, 0, 0)` once it closes. Four validation checks were read before
  any verdict: fires on known-bad; silent on a clean arm; fires on a synthetic held-read-transaction
  fault injected into a verified-clean fresh instance `(0,0,0)` → `(1, 300, 0)`; and the **cosmetic
  control** — a `mode=ro` handle open but **idle**, no `BEGIN` — stays **GREEN** at 4 120 032 B and
  `(0,0,0)`, which is what proves the instrument reads *"a read transaction is held"* and not
  *"a connection exists"*.
* **An instrument defect found and disclosed:** the first version of that probe rewrote each row
  with an **identical** payload. SQLite then dirties no page, so every arm read flat, the
  autocheckpoint-disabled arm did not grow, and all four validation checks passed **vacuously**. The
  WAL header's checkpoint-sequence field (`ckpt_seq`, non-perturbing, unlike `PRAGMA wal_checkpoint`)
  is what exposed it: 0 throughout, i.e. no checkpoint had occurred and the WAL simply was not
  growing. Only the unique-payload workload discriminates.
* **Severity.** The pathological arm grows **4 179 744 B per 1 000 write transactions**, so at the
  ~4 000 transitions `docs/SPEC.md` and `state/projection.py:11` both size a 250-repo run at, the
  worst case is on the order of **16 MB**, and reaching the SPEC's own **< 2 GB** ceiling would take
  ~**478 000** transactions. *"Until the disk fills mid-run"* was overstated as well as misattributed.
  This extrapolates one fixture's per-transaction page cost; treat 16 MB as an order of magnitude,
  not a measurement. R3, on a different fixture and a different driver, derived ~16.5 MB and
  ~485 000 — a class agreement, not a shared instrument.

**The open leg — why this is `PARTLY ADDRESSED` and not `FIXED, LANDED`.** The corrected sentence's
load-bearing structural claim is *"the tree's only read transaction is `build_state`'s"*, and
**nothing binds it**. The pragma half *is* bound — `tests/test_db.py:147` asserts
`wal_autocheckpoint == 1000` on the read handle and `:160-166` on the write handle — but no test
asserts how many read transactions exist, and `grep -rniE 'wal_checkpoint' tests/` still returns
**0** at `f6a2e4e`, so W13's *"Would a test catch it? MEASURED — no"* remains true for that clause.
A second `BEGIN DEFERRED` added anywhere under `src/` would falsify the SPEC silently and re-open
the hazard R3 measured. The replacement sentence's *"no code path may hold a read transaction open
across a wave"* is a **stated rule, not a mechanism**, and is recorded as one here rather than
closed with a convention wearing a mechanism's clothes. Closing this leg means an instrument that
derives the `BEGIN` set from the source and fails by `file:line` — out of W8's file scope this
round, and not written.

**Also still unmeasured, and inherited as unmeasured:** whether a process *outside* the harness — an
operator's `sqlite3` shell, or a command overlapping a wave — can hold a read transaction across it.
R3 tagged that `[UNVERIFIED]`; W8 did not measure it either.

**One number in the routing corrected.** The brief and R3 both state the SPEC class sweep as
*"46 hits, 2 belonging to the claim"*. Under R3's own predicate that reproduces exactly at
`f6a2e4e` (46, of which 2 are the claim); under a predicate that also names the severity clause
(`disk fills`) the claim has **3** members. Both readings resolve to the **same single sentence** —
a predicate-dependent raw total over an identical class result, exactly as Guardrail 6 predicts.
After the fix that class is **0 affirming sites**; one `wal_checkpoint` occurrence survives in
`docs/SPEC.md` and it is the correction's own **denial** (*"nothing under `src/` issues …"*), which
a count-based detector cannot distinguish from the claim it retired — subtract it by rule.

---

**2026-08-26 — annotation, lane W9 (round H), at `f087876`. Nothing above is rewritten: no
figure, sentence or verdict of W13's or W8's is altered, and nothing here is retracted.** W8's
marker above records the leg that kept this entry open — the corrected §5 sentence's structural
claim *"the tree's only read transaction is `build_state`'s"* was a **stated rule, not a
mechanism** — and names what would close it: an instrument deriving the `BEGIN` set from the
source and failing by `file:line`. **That instrument now exists**, written by a sibling lane in
its own commit this round: `tests/test_read_transaction_statements.py`. It parses the claims out
of §5's paragraph rather than comparing copies of it (so editing the prose changes what is
asserted), derives the executed transaction set from `src/` with `ast`, and fails by `file:line`
when a second read transaction exists; it binds the paragraph's other parsed claims in the same
way — the `wal_autocheckpoint` value and the module constant said to carry it, the denial that
anything under `src/` issues `PRAGMA wal_checkpoint`, and the debounce rate — and it separately
asserts that the `BEGIN` is terminated inside the function that opens it, so no caller can hold
the snapshot past that function's return. A second `BEGIN DEFERRED` added anywhere under `src/`
therefore no longer falsifies `docs/SPEC.md` with the suite green, which is exactly the silent
falsification W8 recorded as unguarded.

**What that instrument cannot catch, from the residual it records about itself — stated here so
no reader takes "bound" for "closed".** Two of its recorded limits bear directly on this entry:

* **Everything it asserts is static.** No WAL file is grown and no transaction is executed by it.
  It binds the *structural* claim about how many read transactions the source contains; the
  physics — the 8 000-write-transaction fixture, the held reader byte-identical to the
  autocheckpoint-disabled arm, the 8.12× figure — remain W8's runtime measurements recorded above
  and are **not** reproduced by any test. **This file must not be cited as reproducing D81's
  fixture.**
* **A quoted `;` inside a SQL script literal buys a bounded silence in the instrument's
  recognition-gap arm.** Because that arm's gate splits on `;` without parsing string quoting, a
  literal carrying a quoted semicolon is rejected whole and the arm never looks at a `BEGIN`
  inside it. Its lane measured the bound rather than asserting it: the **same** literal handed to
  an execute call is still caught by the primary census arm, which does not consult that gate, so
  what escapes is a transaction statement that both sits in such a literal **and** is executed
  through no call the file can resolve. What is lost is the net under the net, not the census.

The instrument's residual is longer than these two and is recorded in the file itself rather than
summarised here; it is explicit that it is a floor and not a ceiling.

**Why the heading still reads `PARTLY ADDRESSED`, and why that is a ruling rather than an
oversight.** By the "Status vocabulary, used strictly" block above, `PARTLY ADDRESSED` means some
legs landed and others are still open, with the entry saying which — and by the ruling recorded at
`39862ec`, the heading is a status **field** governed by that vocabulary. The read-transaction leg
is now bound and is no longer the reason. **The second unclosed item is**: whether a process
*outside* the harness — an operator's `sqlite3` shell, or a command overlapping a wave — can hold
a read transaction across it. R3 tagged that `[UNVERIFIED]`; W8 recorded that it did not measure
it either; and the new instrument does not reach it, its own residual stating that its scope is
`src/` and that a read transaction opened by a test, a script or an operator's shell is outside
every assertion it makes. So the item is now carried unmeasured by a third lane, and the entry
stays `PARTLY ADDRESSED` for that item alone rather than for the one this annotation closes.
Nothing here promotes the entry, and nothing here weakens W8's `[UNVERIFIED]` tag.


## D82 — OPEN, recorded only. `WaveScheduler` is documented "one instance per (run, phase)" but its wall clock is per-`(run, wave)` with no phase, so one exit-4 breach in TRANSFORM leaves BUILD and VERIFY of that wave permanently un-admittable

**Found and exercised by lane R2 (round F) against a real temp database; recorded by lane W11, which
re-derived every source fact below at `b5f7760` and labels the runtime figures it did not
re-execute.** No `pytest` session was run by this lane in the primary checkout; the source facts are
`ast`- and normalised-text reads of the working tree at `b5f7760`.

### The mechanism, re-derived at `b5f7760`

* **The scheduler is phase-scoped; its store is not.** `WaveScheduler`'s own class docstring
  (`src/fleet/orchestrator/scheduler.py`) reads *"Decides what may run next, and what a failure
  costs. **One instance per (run, phase)**"*, and it carries a `phase` field that `status_of` uses on
  every read (`db.get_phase(self.run_id, repo_id, self.phase)`). Class result, this lane's own
  predicate: of the **8** methods on the `SchedulerStore` protocol (`record_plan`, `wave_indices`,
  `wave_members`, `wave_started_at`, `begin_wave`, `blast_radii`, `append_blocked_by`,
  `append_unblocked_wave`), **0 take a phase argument**. The phase reaches `phases`, never `waves`.
* **`waves` has no phase column.** `CREATE TABLE IF NOT EXISTS waves` (`src/fleet/state/schema.sql:218`)
  declares `run_id`, `wave_index`, `computed_at`, `wave_started_at`, `synthetic`, `max_usd`, and
  `PRIMARY KEY (run_id, wave_index)`. One row per `(run, wave)`, shared by all four phases.
* **The clock is measured from that shared persisted stamp.** `WaveScheduler.elapsed_s`
  (`scheduler.py:423-428`) is `max(0.0, clock() - stored)`; `WaveScheduler.breached`
  (`scheduler.py:430-437`) is `False` iff the stamp is `NULL`, else
  `elapsed_s >= budgets.wave_max_wallclock_s`.
* **The stamp is written once and never cleared.** `begin_wave` issues
  `UPDATE waves SET wave_started_at = COALESCE(wave_started_at, ?)` (`SqliteSchedulerStore.begin_wave`, `scheduler.py:242-244`), and its
  docstring gives the reason: racing resumes "must not each decide the wave started now". Class
  result at `b5f7760`: `grep -rn wave_started_at src/` → **20 hits**, of which exactly **1** is an
  assignment — that `COALESCE` — and **0** assign `NULL` or a fresh value.
* **Exit 4 is not `breached` alone.** `WaveReport.exit_code` (`src/fleet/orchestrator/runner.py:311-320`)
  returns 4 iff `withheld` **and** `state is WaveState.PARTIAL`; a breached wave whose members have
  all settled is `CLOSED` and returns `None` (`WaveScheduler.wave_state`, `scheduler.py:414-421`).
* **Four schedulers, one clock.** AST sweep resolving `WaveScheduler(` by name: **4** construction
  sites in `src/`, all in `cli.py` — `phase=Phase.SCAN` (`:1929`), `Phase.TRANSFORM` (`:4242`),
  `Phase.BUILD` (`:7716`), `Phase.VERIFY` (`:7793`). All four read the same `waves` row for a given
  wave index.

### What it does — **R2's exercised figures, not re-executed by this lane**

R2 drove a real `SqliteSchedulerStore` on a temp database with a 60 s ceiling: TRANSFORM opens wave 0
and burns its clock, then a **brand-new scheduler for a phase that has never run** reads the same row.

```
TRANSFORM breached(0)=True  elapsed=61.0   wave_started_at(0)=2026-08-09 12:00:00+00:00
BUILD     breached(0)=True  admitted=() withheld=('acme-auth','acme-commons') state=PARTIAL exit=4
VERIFY    breached(0)=True  admitted=() withheld=('acme-auth','acme-commons') state=PARTIAL exit=4
BUILD.open_wave(0) returned 2026-08-09T12:00:00+00:00  (COALESCE kept the TRANSFORM stamp)
```

R2 further measured a fresh process ten days later on the same database still reading `breached=True`
(`elapsed=864000.0`), and members left `PENDING` with `attempts=0` — no attempt consumed, exactly as
admission promises. **The consequence: one exit-4 halt makes every later phase on that wave
un-admittable, in that run and in every later one**, because nothing in `src/` ever clears the stamp.
The one live escape R2 found is §11.5 step 6's **synthetic** appended wave, whose `wave_started_at` is
`NULL` and therefore un-breached — but that reaches only repos whose `blocked_by` cleared, not a repo
that merely sat in a breached wave.

### The open question — recorded, and this entry does NOT decide it

**Nothing in the tree states whether cross-phase sharing of `wave_started_at` is intended.** R2
looked and found no ADR or SPEC sentence addressing it either way; this lane re-checked at `b5f7760`
with a whole-file normalised sweep — `wave_started_at` appears **20 times in `src/`, 11 in `tests/`
across 5 files, and 8 in `docs/*.md`** — and every `docs/` hit is about **resumes and resequences**,
never about phases: §3.4's budget-table row (`docs/SPEC.md:1473`), the `MigrationWave` model listing,
the DDL listing, the migration `ALTER TABLE`, and §6's table-lifecycle row (*"a rewrite PRESERVES
`wave_started_at`, or a resequence would restart a wave's wall clock"*). So the behaviour is
**emergent from a schema choice**, and the question — should the clock be per-`(run, wave, phase)`,
should a phase transition re-stamp, or is one wall clock for a wave's whole four-phase life the
intent? — has no answer anywhere in the tree. **This entry states the behaviour and its consequence
and rules on none of them.** An owner must decide before anything changes; choosing by symmetry with
the per-phase `phases` table, or with the budget ledger, would be exactly the "assign by symmetry"
move CLAUDE.md records as a dispatcher failure.

### A second contradiction in the same §3.4 passage — recorded here, NOT fixed

`docs/SPEC.md:1482-1484` — the prose nine lines below §3.4's budget-table row — says *"`fleet resume`
re-opens the same `PARTIAL` wave and re-admits its `PENDING` members in descending blast-radius
order, exactly as a first admission would."* Measured at `b5f7760`, that cannot hold on either count:
`fleet resume`'s step 8 does not exist, so the verb reconciles and then refuses with **exit 2**
(`ResumeIncompleteError`; the verb's own docstring says so at `cli.py:10338-10341`); and even once it
does, the `COALESCE` above means the same breach recurs on every resume unless
`budgets.wave_max_wallclock_s` is raised and the drift accepted. **[2026-08-25, round G lane W9 — the `cli.py:10338-10341` pointer above CANNOT be repointed: its
target no longer exists.** `f6a2e4e` (ADR-0080) wired §11.5 step 8 and DELETED
`ResumeIncompleteError` (`grep -rn 'class ResumeIncompleteError\|ResumeIncompleteError('
src/ tests/` → 0 at `f5a188a`). `fleet resume` continues now. **The paragraph's conclusion is
unaffected and is left as written**: it rested on two independent counts, and the second — the
`COALESCE` means the same breach recurs on every resume — is untouched by step 8, which ADR-0080 §7
records as not touching `WaveScheduler` at all. Only the FIRST count, and its citation, are
falsified. Nothing above is rewritten.]**

The **table row** at
`docs/SPEC.md:1473` is the honest half — "exits with code 4", and "cumulative across resumes — a
resume continues the wave's clock, it never restarts it". **The two halves of one §3.4 passage
disagree with each other.** That is why the three `src/`- and `tests/`-side copies of the re-admit
claim (corrected in the same change as this entry — see the class sweep under D84) were rewritten to
describe **what the code does** and to point at this contradiction, rather than being "reconciled
against the SPEC" as if the SPEC were settled. **Which half of §3.4 moves is a SPEC decision this
entry does not take and this lane did not make: `docs/SPEC.md` is untouched, and this needs an
owner.**

> **[2026-08-25, round G lane W4 — DATED MARKER on the paragraph above. That paragraph is left
> exactly as its author wrote it and was TRUE at its own commit; nothing in it is retracted.] The
> `docs/SPEC.md` half of this section is now FALSE: `docs/SPEC.md` is no longer untouched, and it no
> longer says what this section quotes.** The quoted sentence — *"`fleet resume` re-opens the same
> `PARTIAL` wave and re-admits its `PENDING` members in descending blast-radius order, exactly as a
> first admission would"* — was rewritten by commit **`f54dac8`**, which replaced the three lines at
> `docs/SPEC.md:1482-1484` with the ten now at **`:1482-1491`**. §3.4's prose now reads **"Re-entry
> is therefore not re-admission"** (`:1484-1485`) and states that `waves.wave_started_at` *"is
> stamped once and inherited, so the wall clock above continues where it stopped rather than
> restarting"* — i.e. it now **agrees** with the `:1473` table row this section calls "the honest
> half". **The two halves of §3.4 no longer disagree: the "second contradiction" this section
> records is CLOSED, and the owner it asks for is discharged.**
>
> **Timing, re-measured rather than inherited.** This lane's dispatch said `f54dac8` landed
> "thirty-two minutes after D82 was written". It did not. D82 was written by **`ee1ddc8`**
> (2026-08-25 11:42:49 +0000) and `f54dac8` is its **immediate child** (11:54:14 +0000) — **11
> minutes 25 seconds**; `git log ee1ddc8~1..f54dac8` lists exactly two commits. The substance of the
> dispatch's claim reproduces; its number does not, and the correction is recorded here rather than
> propagated.
>
> **What `f54dac8` does NOT close: the cross-phase half, which is this entry's actual subject.** It
> touched `docs/SPEC.md` and nothing else — no code — and nothing in it addresses whether one
> `waves` row's clock should be shared by four phases. "The mechanism" and "The open question" above
> both stand at `8b40498`, re-derived by this lane, and this entry stays `OPEN` on that half alone.

### Would a test catch it? MEASURED — no, and it is not expressible

AST sweep for `WaveScheduler(` in `tests/`: **5** construction sites across **4** files
(`test_scheduler.py:142` and `:240`, `test_runner.py:418`, `test_cli.py:4989`,
`test_step6_wave_write.py:356`). Every site in a given file uses a **single** phase —
`PHASE = Phase.TRANSFORM` at `test_scheduler.py:47` and at `test_runner.py:97`, literal
`Phase.BUILD` and `Phase.TRANSFORM` in the other two. **No test file constructs two schedulers for
different phases over the same wave**, so no fixture in the suite can express this defect at all;
R2's probe is the only artefact that has. That is a statement about expressibility, not about case
count: adding cases to any of those files cannot reach it.

*(2026-08-25, round G lane W4 — **annotation only. This entry stays `OPEN`, and not one word above
is rewritten. The only in-place edits are `file:line` citations — pointers, not records — and every
one is itemised below.** Recording the orchestrator's ruling on the cross-phase half and the two
costs of the shipped remedy that nobody had disclosed. Every number below was re-derived by this
lane at `8b40498` with its predicate stated beside it; nothing is inherited.*

***Cross-phase sharing of `wave_started_at` is a DESIGN CONSEQUENCE, not a SPEC violation — because
the SPEC never scopes the wave clock to a phase at all.*** Whole-file whitespace-normalised sweep of
`docs/SPEC.md` with offsets mapped back to line numbers, predicate
`wave_max_wallclock_s|wave_started_at|wall.?clock` (case-insensitive): **31 hits**. **8** carry a
`phase` token within ±220 normalised characters, and **all 8 were read individually**: not one
scopes the *wave* clock to a phase. Five are the **different**, genuinely per-phase budget
`budgets.task_max_wallclock_s` (`:6165`, `:6775`; a `TaskWallclock` mapping at
`src/fleet/settings.py:270`), two are a neighbouring `phase` column or `phases.blocked_by` in an
unrelated table or risk row (`:4688`, `:7401`), and one is the `waves`/`wave_members` lifecycle row
(`:5054`). **Class result: 0 of 31 SPEC hits scope the wave clock to a phase**, while its per-*task*
sibling is per-phase by declaration — so the shape is chosen, not overlooked. This lane's dispatch
offered "19 normalised hits, `phase` in none": the **raw total does not reproduce** under this lane's
predicate (31, not 19) while the **class result does, in a stronger form**. Exactly the Guardrail-6
asymmetry, so the raw total is not propagated.

***Reset-on-resume is forbidden in THREE places, which is what makes the shipped remedy the
sanctioned one rather than a workaround.*** `docs/SPEC.md:1473` (the §3.4 budget-table row:
*"cumulative across resumes — a resume continues the wave's clock, it never restarts it"*),
`:1483-1484` (the `f54dac8` prose), and `:4101-4103` (the `waves` DDL listing: *"PERSISTED, so
`wave_max_wallclock_s` (§3.6) is cumulative across resumes rather than restarted by one"*). A fourth
adjacent site, `:5054`, forbids a **resequence** from restarting the clock — a different subject,
disclosed rather than counted. Raising `budgets.wave_max_wallclock_s` is therefore normative, and
stated as such at **`:1487-1491`**: *"Raising that ceiling is what clears an exit 4, and it is an
audited config change, not a flag … the edit drifts exactly the `budgets` section, which
`fleet resume --accept-drift budgets` accepts and records as a `ConfigDrift` finding."* (The dispatch
cited `:1486-1491`; the sentence begins mid-`:1487`, and `:1486` belongs to the re-entry clause
above it.)

***The two costs of that remedy, disclosed here because nothing else in the tree states them.***
**(1) The required ceiling grows with REAL elapsed time, not with work done.** `elapsed_s` is
`max(0.0, clock() - stored)` (`WaveScheduler.elapsed_s`, `scheduler.py:423-428`), so a database sitting idle spends the ceiling
just as fast as one running. Against the default `wave_max_wallclock_s = 14_400`
(`src/fleet/settings.py:269`), R2's ten-day figure in the body (`elapsed=864000.0`) needs a ceiling
**60× the default** merely to admit one member; the multiplier is `elapsed / 14400` and it is
unbounded. **(2) It is a FLEET-WIDE SCALAR, so un-breaching one wave raises the ceiling for every
wave.** `wave_max_wallclock_s: int` on `BudgetsSection` carries no `run_id`, `wave_index` or phase
dimension, and `breached` reads `self.budgets.wave_max_wallclock_s` for every wave
(`WaveScheduler.breached`, `scheduler.py:437`). Nor is there a per-run escape hatch: `fleet resume --raise-wave-budget` is a
**USD** ceiling that clears exit **10** (`cli.py:10299`, applied at `:10543-10544`), not exit 4 —
which is precisely why §3.4 calls the wall-clock remedy *"an audited config change, not a flag"*.

***Disposition B — re-stamp `wave_started_at` on a phase transition — is DELIBERATELY UNRULED and
must not be written as settled in either direction.*** It needs a row-level signal distinguishing
"phase transition" from "resume", and the research lane looked for one, **found none, and explicitly
declined to assert that none exists**. A targeted lane is investigating. Writing "there is no such
signal" here would be the unattributed-certainty failure this file exists to prevent.

***Citation corrections made in place — pointers, not records; no claim changed.*** Re-derived by
predicate at `8b40498`: `scheduler.py` `:234-235`→`:239-241` (the `COALESCE`), `:415-420`→`:420-425`
(`elapsed_s`), `:422-429`→`:427-434` (`breached` — and it is **public**, which matters to D84),
`:406-413`→`:411-418` (`wave_state`); `runner.py:293-302`→`:308-317` (`WaveReport.exit_code`);
`cli.py` `:1916`/`:4222`/`:7688`/`:7758`→`:1948`/`:4261`/`:7735`/`:7812` (the four `WaveScheduler(`
sites — still **4**, still all in `cli.py`, phases unchanged); `cli.py:10099-10101`→`:10338-10341`
(`fleet resume`'s exit-2 statement); `tests/test_runner.py:416`→`:418` and `:96`→`:97`. **Unchanged
and re-verified rather than assumed:** `state/schema.sql:218`, `docs/SPEC.md:1473`,
`tests/test_scheduler.py:142`/`:240`/`:47`, `tests/test_cli.py:4989`,
`tests/test_step6_wave_write.py:356`. **Thirteen citations had drifted where the dispatch named
four.** Its four all re-derived correctly; its proposed `runner.py` replacement `:301-309` did not —
that span is `WaveReport`'s field block, and the property is at `:308-317`.

> **[2026-08-25, round G lane W9 — every REPLACEMENT value in the record above had itself drifted by
> the time it landed, and the live prose citations elsewhere in this entry are repointed to
> `f5a188a`. The record above is left verbatim: it states what `3dc3a98` wrote at its own commit.]**
> `3dc3a98` disclosed its anchor (`8b40498`) and landed after `12d3527` had inserted 3 lines into
> each of `runner.py`'s and `scheduler.py`'s header blocks, above every cited line — so **9 of 9**
> of its `runner.py`/`scheduler.py` replacements were wrong on arrival, uniformly **+3**, while its
> `cli.py` replacements were right. `f6a2e4e` then moved `cli.py` by **−19** in this region and
> `tests/test_cli.py` by **+150**, so that half drifted too. Re-derived at **`f5a188a`** by an
> old→new line map built from `git diff -U0 8b40498 f5a188a`, cross-checked against an `ast`
> resolution of each named symbol (both instruments agree on all nine):
> `scheduler.py` `:239-241`→**`:242-244`**, `:411-418`→**`:414-421`**, `:420-425`→**`:423-428`**,
> `:427-434`→**`:430-437`**, `:434`→**`:437`**; `runner.py` `:308-317`→**`:311-320`**,
> `:313-314`→**`:316-317`**, `:406-424`→**`:409-427`**, `:425`→**`:428`**;
> `cli.py` `:1948`/`:4261`/`:7735`/`:7812`→**`:1929`/`:4242`/`:7716`/`:7793`**,
> `:10312`→**`:10299`**, `:10472-10473`→**`:10543-10544`**;
> `tests/test_cli.py:4839`→**`:4989`**. `cli.py:10099-10101`→`:10338-10341` is the one that cannot
> be repointed — see the marker in the "second contradiction" section above.
>
> **The count in the sentence above does not reproduce; the class result does.** "Thirteen
> citations had drifted" re-derives to **12** under the predicate *"distinct citations enumerated
> in this paragraph"* (4 `scheduler.py` + 1 `runner.py` + 4 `cli.py` `WaveScheduler(` + 1
> `cli.py:10099-10101` + 2 `tests/test_runner.py`) and to **7** under *"citations appearing in a
> removed line of `3dc3a98`'s own diff and absent from its added lines"*. No predicate tried by
> lane CR2 or by this lane yields 13. **The class result is the part that reproduces and the part
> that matters: the dispatch named four drifted citations, a sweep for the class found
> substantially more, and the dispatch's own proposed `runner.py` replacement (`:301-309`) was
> itself wrong.** Per CLAUDE.md Guardrail 6, the class is stated and the raw total is not
> propagated. The sentence above is a record and is not rewritten.
>
> **A drift-resistant citation form exists in this file already and is worth copying.** `:3981`
> writes ``(`:7181` at `53e5d8d`)`` — a line number bound to the commit it was measured at, which
> stays TRUE forever instead of silently going wrong. For a pointer meant to resolve at `main`,
> the cheap half is a **symbol name beside the line** so a reader who lands in the wrong place can
> recover; the repointed citations above now carry one wherever they stood bare. Neither form is a
> mechanism: **no file under `tests/` constructs a path to `docs/INTEGRATION_HONESTY.md`** (0 path
> constructions at `f5a188a`, re-derived by this lane), so nothing in the suite can resolve a
> citation in this file, and this class will recur until something does.

***One CONTENT change hiding behind one of those citations, stated rather than silently repointed.***
`WaveReport.exit_code` no longer reaches 4 only through the branch this entry describes: `8b40498`
(D83's fix, below) added a first branch `if self.halt is not None: return self.halt.exit_code`
(`runner.py:313-314`), and `run_wave` now constructs a `RunHalted(HaltReason.WAVE_WALLCLOCK, …)` on
exactly this entry's predicate. **The exit code is still 4 and this entry's mechanism is unchanged**
— but the sentence *"returns 4 iff `withheld` **and** `state is WaveState.PARTIAL`"* now describes
the *fallback* branch. That sentence is left as written and this marker is its correction.

***What this annotation does not establish.*** This lane ran no `pytest` session, executed no
scheduler and changed no code. Every figure above is a `grep`/`awk`/normalised-text read of the
working tree at `8b40498`, plus `git log`. R2's runtime figures in the body are **cited, not
re-executed**.)*

---

## D83 — FIXED, LANDED (`8b40498`). A wall-clock breach exits 4 carrying the literal message `None`, through all four phase verbs

**Found by lane R2 (round F); the site located and the value re-exercised in the running interpreter
by lane W11 at `b5f7760`.**

`WaveReport.halt` is populated **only** from a `RunHalted` escaping the wave's `TaskGroup` —
`except* RunHalted as raised: halt = _flatten(raised)` (`src/fleet/orchestrator/runner.py:379-380`),
the sole assignment to it in the module. A wall-clock breach raises nothing: `admit` returns
`admitted=()` and the runner's `may_admit` poll withholds the rest, so `halt` stays `None` while
`WaveReport.exit_code` returns `WAVE_WALLCLOCK_EXIT_CODE = 4` from the *other* branch
(`runner.py:293-302`; the constant at `runner.py:109`).

Each phase impl then folds the reports into a payload with
`"halt": next((str(r.halt) for r in reports if r.halt is not None), None)` — `None` for a breach —
and the CLI maps a non-zero code with `raise FleetCliError(str(result["halt"]), exit_code=code)`.
`str(None)` is `'None'`.

**Class result, not a single site.** That exact `raise` line occurs **3 times** in
`src/fleet/cli.py` — in `scan` (`:1047`), in `transform` (`:3208`), and in `_raise_for_phase`
(`:8587`), which `build` calls at `:2509` and `verify` at `:2550`. **All four phase verbs reach it.**
The payload half has the same shape at four sites: `:1864` (`scan`'s
`None if report.halt is None else str(...)`) and the three `next((str(r.halt) …), None)` folds at
`:4505`, `:8316` and `:8502`.

**Exercised, not read.** Driver in this lane's own scratch subdirectory, run as
`env -i PATH=/usr/bin:/bin HOME=… PYTHONPATH=<worktree>/src .venv/bin/python <driver>`, printing
`sys.executable` and `fleet.__file__` and **asserting** the latter resolves inside the worktree
before any result:

```
fleet.__file__ = <worktree>/src/fleet/__init__.py      PIN OK
cli._raise_for_phase({"exit_code": 4, "halt": None, "failed": 0, "attention": []})
  MESSAGE  = 'None'
  EXITCODE = 4
```

An operator whose wave breaches its four-hour ceiling is told `None` on stderr, with exit 4. The exit
code is correct; the message carries no information at all — not the wave index, not the ceiling, not
the elapsed seconds, none of which is unavailable at the fold site. Rule 11 ("fail loud") is met by
the code and defeated by the text.

**Would a test catch it? MEASURED — no.** The two places in `tests/` that name this exit assert the
**code**: `tests/test_runner.py:1801` (`assert report.exit_code == 4`) and `tests/test_cli.py:3844`
(`ExitCode.WAVE_WALL_CLOCK_EXHAUSTED == runner.WAVE_WALLCLOCK_EXIT_CODE == 4`). A text sweep of
`tests/` finds **no** assertion on the message of an exit-4 `FleetCliError`. The fix is a message
construction at the fold sites, not a control-flow change.

*(2026-08-25, round G lane W4 — **the status FIELD in the heading is set to
`FIXED, LANDED (`8b40498`)`; the body above is left exactly as lane W11 wrote it and nothing in it
is retracted.** A heading is a field governed by the "Status vocabulary, used strictly" block above
and by the ruling recorded at `39862ec`; a body is a record. Every source fact below re-derived by
this lane at `8b40498`, by predicate rather than by line.*

***The fix landed at the SOLE `WaveReport` producer, NOT at the four fold sites this entry's body
proposes — a narrower fix than the entry called for, and recorded as such.*** The body closes *"The
fix is a message construction at the fold sites"*. Those sites are all in `src/fleet/cli.py`, and
lane W2 could not touch that file while lane W1 was rewriting it. `grep -rn 'WaveReport(' src/`
returns **exactly 1** construction site — `PhaseRunner.run_wave`'s `return WaveReport(` at
`src/fleet/orchestrator/runner.py:428` —
so populating `halt` there closes the class for **every** consumer with no consumer changing.
**Nothing at the four fold sites changed**: all four still read `r.halt` / `report.halt` exactly as
before (`cli.py:1851`, `:4530`, `:8362`, `:8549`).

***The mechanism, re-derived here.*** `run_wave` constructs
`RunHalted(HaltReason.WAVE_WALLCLOCK, …)` under `if halt is None and withheld and state is
WaveState.PARTIAL` (`runner.py:409-427`) — the **same predicate** as `WaveReport.exit_code`'s
withheld/PARTIAL branch, so the two cannot disagree — and it is guarded on `halt is None`, so a real
`RunHalted` (DISK, TIER_UNAVAILABLE) is never overwritten.
`_EXIT_CODES[HaltReason.WAVE_WALLCLOCK]` is `WAVE_WALLCLOCK_EXIT_CODE` = 4 (`:148`, `:119`) — the
same integer; **no new exit code exists**. `RunHalted.__init__` renders `f"{reason}: {detail}"`
(`:164`), which is what `str(r.halt)` folds into the payload. The message names the wave index, the
phase, the ceiling, the elapsed seconds and the withheld members — every fact the body says was
"not unavailable at the fold site" — and it deliberately names **no remedy**, because at this commit
nothing re-admits a breached wave (**D82** above, still `OPEN` on that half).

> **[2026-08-25, round G lane W9 — `8b40498`'s message named the WRONG elapsed on the mid-wave
> path, and its test could not fail under that. Fixed in this commit; nothing above is rewritten.]**
> `8b40498` built the message from `admission.elapsed_s`, which `WaveScheduler.admit` snapshots
> **before** the wave runs. On the **mid-wave** path — `may_admit` re-polled between admissions,
> which is the path `run_wave`'s own docstring describes — `admit` did **not** breach, so that
> snapshot is **by construction below the ceiling**: lane CR2 measured the real runner, scheduler
> and DB emitting *"spent its 60s wall-clock ceiling **after 0.0s**"* with `admission.breached =
> False` and 1–3 repos genuinely dispatched. The rationale sentence *"the elapsed the breach was
> decided on"* was therefore false wherever it appeared. `run_wave` now re-reads
> `await self.scheduler.elapsed_s(wave_index)` at the point of construction — the same quantity
> `breached` compares against — and the comment states why.
>
> **The test was the more important half.** `test_a_wall_clock_breach_says_what_happened_rather_
> than_the_string_none` drives only the **pre-wave** shape (`open_wave(0)` → `advance(61)` →
> `run_wave(0)`, `breached is True`, nothing dispatched) and compared the parsed elapsed against
> **`report.admission.elapsed_s` — the same stale snapshot the message was built from**. That is
> internal consistency, not correspondence: its two anchors coincide, so the defect was not
> expressible in it (CLAUDE.md Rule 12's two-anchor shape). `tests/test_runner.py`'s new
> `test_a_mid_wave_wall_clock_breach_names_the_elapsed_the_withholding_saw` is the discriminator,
> and it is proved by **old-passes/new-fails on the SAME input**: under the pre-fix source, on the
> new mid-wave fixture, the OLD assertion form passes **38/38** while the new one FAILS. Full
> battery in `.superpowers/sdd/handoff-round-g/lanes/W9/report.md`.

***A second-order effect on D82, recorded there as well.*** `WaveReport.exit_code` grew a first
branch, `if self.halt is not None: return self.halt.exit_code` (`WaveReport.exit_code`,
`runner.py:316-317`), so a breach now
returns 4 **through the halt** rather than through the withheld/PARTIAL branch. Both branches yield
4 and no caller can observe a difference in the code; what changed is the message.

***Citation drift in the body, reported rather than edited*** (this lane's brief scoped in-place
citation repair to D82, and this entry's body is now a record of a fixed defect). Re-derived at
`8b40498`: the three `raise FleetCliError(str(result["halt"]), exit_code=code)` sites are
`cli.py:1052`, `:3244` and `:8647` (the body's `:1047`/`:3208`/`:8587`), `_raise_for_phase` is
defined at `:8638` and called by `build` at `:2545` and `verify` at `:2586` (`:2509`/`:2550`); the
four payload folds are `:1870`, `:4549`, `:8375`, `:8562` (`:1864`/`:4505`/`:8316`/`:8502`); in
`runner.py`, `except* RunHalted as raised` is at `:394-395` (`:379-380`), the constant at `:116`
(`:109`), and `WaveReport.exit_code` at `:308-317` (`:293-302`). **Content unchanged at every one
except `exit_code`**, whose change is the fix itself and is stated above.

> **[2026-08-25, round G lane W9 — the re-derived values in the record above have themselves
> drifted; the record is left verbatim as what was true at `8b40498`.]** At **`f5a188a`**:
> the three `raise FleetCliError(str(result["halt"]), exit_code=code)` sites are **`cli.py:1033`,
> `:3225`, `:8634`**; `_raise_for_phase` is defined at **`:8625`** and called by `build` at
> **`:2526`** and `verify` at **`:2567`**; the four payload folds are **`:1851`** (`fleet scan`'s
> `"halt": None if report.halt is None else str(report.halt)`) and **`:4530`, `:8362`, `:8549`**
> (the three `"halt": next(...)` folds). In `runner.py`, `except* RunHalted as raised` is at
> **`:397-398`**, the constant `WAVE_WALLCLOCK_EXIT_CODE` at **`:119`**, and
> `WaveReport.exit_code` at **`:311-320`**. `cli.py` moved by `f6a2e4e` (ADR-0080), `runner.py` by
> `12d3527` and by this lane's own fix below.
>
> **A predicate note, because it caught this lane's own instrument.** A `grep` for
> `"halt": next(` returns **3**, not the 4 folds the record names — `fleet scan`'s fold has a
> different shape. The count that reproduces is the class *"a payload key `halt` folded from a
> `WaveReport`"*, = **4**, derived from the old→new line map rather than from a text predicate.

***What this annotation does not establish.*** This lane ran no `pytest` session and re-executed
nothing. The post-fix message strings, the two independent exercise routes and the six-mutation
battery are **lane W2's** measurements, cited rather than re-derived; its report and patch are at
`.superpowers/sdd/handoff-round-g/lanes/W2/`. What this lane re-derived at `8b40498` is every source
fact above.)*

---

## D84 — OPEN. `_prepare_repo` mutates every member's git state BEFORE admission discovers it can admit none

**Found by lane R2 (round F) from source order; re-derived and widened to a second phase by lane W11
at `b5f7760`. Source order, `ast`- and text-derived — NOT exercised end to end by either lane, and
this entry says so rather than implying a run.**

In `_transform_impl`'s wave loop (`src/fleet/cli.py`), for each wave index the order is:

1. `upsert_phase` for **every** member (`cli.py:4419-4426`);
2. `_prepare_repo` for **every** member not already planned (`cli.py:4427-4443`);
3. `_run_transform_wave` (`cli.py:4445`) — **which is the first place `WaveScheduler.admit` is
   reached at all**.

> **[Marker 2026-08-28, round-K lane W5 — measured at `506cadb`. The three citations above are
> left exactly as their author wrote them; this entry already rules its citation drift *reported,
> not edited*, and that ruling stands.]** Step 3's `:4445` names the **call** to
> `_run_transform_wave` inside `_transform_impl`, not that function's definition — steps 1 and 2
> name consecutive ranges in the same loop body, and this entry's own drift marker below maps all
> three together. At `506cadb` a sibling lane's additions to `cli.py` moved the **definition** of
> `_run_transform_wave` to `cli.py:4393-4461`, so the cited `:4445` now falls **inside** it —
> `cli.py:4445` is `        ctx,`, an argument to a `PhaseRunner(` call in that function's own
> body. The call this step means is at `cli.py:4674`, **229 lines away**; `_transform_impl` now
> spans `cli.py:4576-4736`, with its `upsert_phase` call at `cli.py:4633` and its `_prepare_repo`
> call at `cli.py:4659`. **Nothing was repaired and nothing here is repointed.** The citation is as
> wrong as it was; only the arithmetic of containment changed. Recorded because it had a
> mechanical consequence: `tests/test_integration_honesty_citations.py` pinned this citation as
> *unresolved*, the drift flipped it to *resolving*, and that pin is retired in this same commit
> under the one stated exception now written at the pin site. **Exactly one direction of cover is
> lost, not both**, re-measured at `12f2cf3` rather than inferred: the citation is still an anchored
> citation of that module's survey (60 anchored, 45 unresolved, 45 pins), still sited at this entry's
> step 3, now *resolving* and *unpinned*. Because it is unpinned it is still covered by
> `test_no_unpinned_anchored_citation_fails_to_resolve`, which fails by `file:line` if drift ever
> moves the definition back off `:4445`. What is no longer covered is the other direction: while it
> was pinned, `test_each_pinned_citation_is_still_unresolved` also failed if the citation's text
> changed at all, so a silent **repoint** of this known-wrong citation would now go undetected. This
> marker is what records that it is wrong.

> **[Marker 2026-08-30, round M/N controller — measured fresh, not inherited.]** The prediction in
> the marker above held: `cli.py` grew a further ~417 lines across round M's three merges (now
> 13830 lines), `_run_transform_wave`'s definition moved to `cli.py:4555-4626`, and the cited
> `:4445` — previously interior to the definition — now falls 110 lines **before** it starts,
> outside any span. The citation is unresolved again, exactly as the retirement comment said it
> would be if drift moved the definition back off `:4445`. Per this entry's own standing ruling,
> the citation names a USAGE site — the call inside `_transform_impl`, now at `cli.py:4839`, 394
> lines from the cited line — and sits in a passage this entry rules is reported, not repointed.
> **Nothing is repointed here either.** `tests/test_integration_honesty_citations.py`'s
> `_PINNED_UNRESOLVED` re-pins it (it had been retired from that list at the prior marker) with a
> matching dated comment, and that test module's own "anchored citations are nonetheless
> unresolved" census sentence is corrected from 45 to 46 to reflect the flip back.

`_prepare_repo` (`cli.py:3858`) is not a read. Its own docstring enumerates what it does before the
first mutation, and two of its four steps are writes: step 2 **discards a crashed predecessor's
dirty worktree** ("§3.2 step 6.4's 'No' branch"), and step 4 creates the phase anchor
`refs/fleet/<run>/<repo>/phase-2/base` as a **real ref**, mirrored into `phases.base_ref` /
`pre_commit_sha`. It also checks out `migrate/<repo>`.

So on a wave whose clock is already spent (D82), the delegate discards worktrees and creates anchor
refs for **every member of the wave**, and only then learns from `admit` that it may admit **none** —
`admitted=()`, `withheld=` every member, exit 4. The git writes are not rolled back. This is not data
loss on its own terms (step 2 can only discard an uncommitted worktree, and the anchor is reused
verbatim on re-entry), but it is work done and state changed on behalf of an admission that was
already impossible before the loop began.

**Class result — the same ordering exists in VERIFY, which R2 marked `[UNVERIFIED]`.** `_verify_impl`
calls `_prepare_verify` for every member (`cli.py:9216-9223` — **corrected 2026-09-02, round EE
final review: the citations here had rotted to the point of naming the wrong function entirely**,
`8430-8449` now falls inside `_run_verify_wave`'s own definition, a different function) before
`_run_verify_wave` (repointed fresh below), and `_prepare_verify` is the heavier git mutator of the two: under the integration
mutex it takes a fresh snapshot ref, then per member runs `worktree remove --force`, a `shutil.rmtree`
fallback, `worktree prune`, and `worktree add --detach --force`. So **2 of the 3 wave-driving phase
impls** put per-member git mutation ahead of admission. `_build_impl` is **not** in this class as
measured: its pre-admission work at `cli.py:8235-8251` is re-planning plus `_check_root_file_domain`,
and this lane did not trace whether either writes git — **`[UNVERIFIED]` for BUILD**, stated rather
than assumed in either direction.

**The cheap fix, and why this entry does not prescribe it.** Polling `scheduler.breached(index)`
before the preparation loop would skip the mutation on a breached wave — but a breach is not the only
way admission ends up empty, and the preparation loop is also what produces the `plans` a later
resume reuses. Whether preparation belongs per-admitted-repo rather than per-member touches the same
`admit` contract D82 records as undecided, and should be ruled on **with** D82, not before it.

**Would a test catch it? MEASURED — no.** No test in the suite asserts on work performed before an
empty admission; the transform and verify e2e suites drive waves that admit.

### The comment class this change also corrected

The same change as these three entries corrected **3 `src/`- and `tests/`-side statements** of one
false claim — *"a wall-clock-breached `PARTIAL` wave is re-admitted by `fleet resume`"* — which is
false at `b5f7760` on the two counts D82 records. R2 reported **two** sites
(`src/fleet/orchestrator/runner.py:107-108` and a line-16 citation in `tests/test_scheduler.py`); a
whitespace-normalised whole-file sweep with offsets mapped back to line numbers, over `src/`,
`tests/` and `docs/` (predicate: a `re-?admit` match within 220 normalised characters of both a
`resume` token and one of `wall.?clock|breach|PARTIAL|wave`), returned **6 matches in 6 files**, of
which **4 are in the class**: the two R2 named, plus
**`src/fleet/orchestrator/scheduler.py:17`** — the module docstring, which no line-oriented grep for
R2's phrasing reaches — plus `docs/SPEC.md:1482-1484`. The other 2 matches are about operator
quarantine and about a `PENDING` row, and are out of class. R2's second citation had also **rotted**:
the claim is at `tests/test_scheduler.py:306`, inside
`test_a_wall_clock_breach_withholds_members_as_pending_and_leaves_the_wave_partial`'s docstring, not
at `:15-16` — the module docstring's line-15 bullet says the *opposite* (that restarting the clock on
resume would be the defect) and is **true**; editing it because it matched a grep would have been the
mirror-image error. **The class of ASSERTING sites went 4 → 1**: the three code and test
statements now describe what the code does; the survivor is `docs/SPEC.md`, a SPEC decision this lane
did not take and left untouched, recorded under D82.

**The raw total moved the other way, and that is the honest number.** Re-running the identical sweep
after the corrections returns **10 matches in 7 files**, up from 6 in 6 — because a correction of
this shape is a **retraction that quotes its own quarry** so a reader can see what was withdrawn
("this rule used to claim `fleet resume` does"), and a match-counting detector cannot tell a
retraction from the claim it retires. That is CLAUDE.md's recorded trap, reproduced here on this
lane's own edit. Subtracting quotations-inside-retractions **by rule** and reading the residue: 1
asserting site (`docs/SPEC.md:1483`), 4 matches over the 3 corrected sites now stating the negation
(`runner.py:109`, `scheduler.py:17`, `tests/test_scheduler.py:308` and `:309`), 3 matches inside
these ledger entries describing the class, and the same 2 out-of-class matches as before (a
quarantine-dependents passage in a promoted plan, and `cli.py:3978`'s `PENDING`-row comment). **Do
not sweep this class by count.**

---

*(2026-08-25, round G lane W4 — **annotation only. This entry stays `OPEN`; nothing above is
rewritten and no citation in it is edited — the drift is reported at the end instead.** Recording
lane W2's first end-to-end exercise of this defect, the resolution of this entry's BUILD
`[UNVERIFIED]`, and where the fix goes when its turn comes. W2's report and repro are at
`.superpowers/sdd/handoff-round-g/lanes/W2/report.md`.*

***EXERCISED for the first time — this entry states plainly that neither R2 nor W11 ran it, and lane
W2 did.*** Real `fleet transform`, real git, real database, over `tests/test_transform_e2e.py`'s
fixture, with wave 0's `waves.wave_started_at` stamped in the past so admission was impossible
**before the loop began**: exit **4**, **0 admitted**, both members left `PENDING` — and **2 of 2**
member worktrees carrying the phase anchor `refs/fleet/<run>/<repo>/phase-2/base` **and** the branch
`migrate/<repo>`, with `phases.base_ref` non-null for both. All three quantities read **0 before the
run**. The watched quantity is deliberately a **git** ref counted per worktree rather than a database
digest, and `_prepare_repo`'s step 4 is its sole creator, so it moves 2→0 exactly with the ordering
and cannot move for any other reason. **These are W2's measurements, cited and not re-executed
here**; this lane re-derived only the source facts below.

***This entry's `[UNVERIFIED]` for BUILD resolves INTO the class: 3 of 3, not 2 of 3 — and narrower.***
Source-derived by W2 and **re-derived independently by this lane at `8b40498`**, where every line
number reproduces exactly because `src/fleet/cli.py` has **zero** commits between W2's anchor
`635a83c` and `8b40498`. `_build_impl`'s pre-admission block cuts an integration snapshot via
`_wave_snapshot` (`cli.py:8237-8239`) and calls `_plan_build`, which runs `worktree remove --force`,
a `shutil.rmtree` fallback, `worktree prune` and `worktree add --detach --force`
(`cli.py:7406-7412`) — **real git mutation** — before `_run_build_wave` at `:8298`. **But narrower
than TRANSFORM's and VERIFY's, and the qualification is load-bearing:** the whole block is guarded on
`published and snapshot is not None` (`cli.py:8231`), so it fires only on a wave that **follows a
publishing wave in the same invocation**, never on the first driven wave — where `_transform_impl`'s
and `_verify_impl`'s per-member loops are **unconditional**. Labelled **source-derived, not
exercised**, in both directions.

***The fix sites, named so the next lane does not re-derive them.*** `_transform_impl`'s loop calls
`_prepare_repo` per member at `cli.py:4451-4468`, before `_run_transform_wave` at `:4470` reaches
`admit` at all; `_verify_impl` calls `_prepare_verify` per member at `cli.py:8477-8485`, before
`_run_verify_wave` at `:8497`. **Both are in `src/fleet/cli.py`**, which is why lane W2 — forbidden
that file this round — measured the defect and did not fix it. **The primitive the cheap fix needs
already exists and is already public: `WaveScheduler.breached` (`scheduler.py:430-437`).** Nothing is
missing from `scheduler.py` or `runner.py`; the fix is a guard in `cli.py`.

***Still NOT fixed, and correctly so.*** This entry's own closing paragraph rules that the ordering
be decided **with** D82's `admit` contract and not before it, and D82 above remains `OPEN` on its
cross-phase half, with Disposition B deliberately unruled. **By ruling this entry waits for D82 and
then gets its own lane.** Implementing ahead of that would be the "ruling in a brief versus primary
source" failure CLAUDE.md records; W2 reported the correction instead of implementing it, and this
marker is where that decision is recorded.

***Citation drift in this entry, reported rather than edited*** (this lane's brief scoped in-place
citation repair to D82). Re-derived at `8b40498`, content unchanged at every one: `_prepare_repo`
`:3858`→`:3894` (def); the `upsert_phase` loop `:4419-4426`→`:4463-4469`; the `_prepare_repo` loop
`:4427-4443`→`:4470-4487`; `_run_transform_wave` `:4445`→`:4489`; `_prepare_verify`'s call
`:8430-8449`→`:8490-8498` (its def is at `:7499`); `_run_verify_wave` `:8451`→`:8510`;
`_build_impl`'s pre-admission block `:8235-8251`→`:8240-8252`, with `_check_root_file_domain` now
defined at `:7140`.)*


---

## D85 — FIXED, LANDED (`b76c041`). The lint gate failed 3 of 4 checks in every detached worktree and passed in the primary, because `_ruff()` asked where this checkout's virtualenv *would be* instead of whether `ruff` runs — the certification environment and the working environment diverged, and the instrument reported the divergence as the defect

> **Editorial correction (2026-09-08), audit task D85 — the heading above now reads `FIXED, LANDED
> (`b76c041`)`, not `OPEN`.** The heading previously read *"OPEN (the fix is in flight, not landed:
> lane W7's round-G patch to `tests/test_lint_gate.py` was uncommitted when this entry was written;
> `main` was `f6a2e4e` and the newest commit touching that file was still `352c514`)"* — quoted here
> verbatim per this file's own convention, so a sweep for it finds this correction and not a
> survival. That was accurate of `f6a2e4e`'s certification environment and is left as an accurate
> record below; it is stale as a live status. `git log --oneline -- tests/test_lint_gate.py` shows
> W7's patch landed as `b76c041`, dated 2026-08-25 17:22:39 +0000 — fourteen minutes after
> `f6a2e4e` (2026-08-25 17:08:19 +0000), and an ancestor of current `HEAD`. Re-read: `_ruff()`
> (`tests/test_lint_gate.py`) now tries `sysconfig.get_path("scripts")` and
> `Path(sys.executable).parent` in addition to PATH, before failing — genuinely probing whether
> `ruff` runs in the environment executing the suite, not merely where this checkout's `.venv`
> would sit. Reproduced fresh in a genuinely detached `git worktree add --detach` (own scratch
> subdirectory, not the primary checkout): `pytest tests/test_lint_gate.py`, no `-k`, no node IDs,
> nothing applied → **6 passed, 1 failed** (was **3 failed, 1 passed** at `f6a2e4e`). The one
> failure is `test_ruff_format_check_dirty_count_matches_the_pinned_baseline`, a *different*,
> already-pinned baseline check (module docstring: "deliberately a BASELINE, not a clean-format
> gate") — 125 dirty files in this fresh worktree vs. the 123 pinned from the primary checkout,
> traced to `.superpowers/sdd/.gitignore`, an **untracked** local convenience file
> (`git status --ignored` shows `!!`) present only in the primary working tree, which excludes
> `.superpowers/sdd/round-VI-criteria-closure/` from `ruff format`'s whole-repo scan there and is
> naturally absent from any fresh worktree. That is a distinct, disclosed environment-dependent
> baseline-reproducibility gap in a different check, not a recurrence of D85's `_ruff()` mechanism —
> not diagnosed or fixed here; the D85 heading is updated on D85's own evidence only. Per this
> file's "Status vocabulary, used strictly" block: the heading is a status **field**, updated on
> fix; the body immediately below is the historical **record** and is left exactly as W7/W8 wrote
> it.

**Found, fixed and measured by lane W7 (round G) at `f6a2e4e`; recorded here by lane W8, which
re-derived every figure it uses in its own separate detached worktree at `f6a2e4e` — a second
environment, not W7's. Figures W8 did not re-derive are labelled inline as W7's.** The number was
allocated centrally by the orchestrator and verified free by a form-agnostic `\bD85\b` sweep over
this whole file (0 occurrences) before writing, because this document heads entries three ways and
`D12`/`D18`/`D19`/`D75` each carry two forms.

### The instance, reproduced independently

`pytest tests/test_lint_gate.py` in a clean detached worktree at `f6a2e4e`, **no `-k`, no node IDs,
nothing applied** → **3 failed, 1 passed in 0.12s**. W7's "3 of 4" reproduces exactly, and so does
its sharper claim: **all three fail at one site**, the single `pytest.fail` in `_ruff()`
(`tests/test_lint_gate.py:63`). The survivor,
`test_every_declared_ruff_requirement_pins_one_exact_version`, is the **only** check that never
shells out to `ruff` — it reads `pyproject.toml`. One defect with three faces, not three failures.

The mechanism: `_ruff()` is `shutil.which("ruff")` and nothing else. `tests/conftest.py` prepends
`REPO_ROOT/.venv/bin` to PATH at import time, `REPO_ROOT` is derived from `conftest.__file__`, and
`_prepend_path` inserts an entry only `if entry.is_dir()` — so in a worktree that entry is
`<WT>/.venv/bin`, which **does not exist**, is silently skipped, and `ruff` is never found. The
emitted failure names that path verbatim.

### Why this is not the missing-toolchain class, which is the finding

ADR-0093's consequence (ii) reads, at `f6a2e4e` (anchor on the sentence, not the line): *"A worktree
lacking `.venv` will fail this gate, the same class as the existing missing-toolchain failures."*
**That classification is wrong**, and W8 measured why in its own worktree:

| | the 24 missing-`tools/` failures | this gate in a worktree |
| --- | --- | --- |
| Is the tool present in the running environment? | **No** — gitignored binaries, genuinely absent | **Yes** — `ruff 0.16.2`, and `ruff check --no-cache --output-format=concise .` from the worktree root exits **0** |
| What the predicate asks | "is the toolchain here?" | "would this checkout's virtualenv be here?" |
| Verdict | a genuine environmental **boundary** | a **predicate defect** |

The linter is installed, pinned (`ruff==0.16.2` in both dependency tables of `pyproject.toml`),
runnable and clean; the gate looked in a checkout-relative place instead of asking the environment.
That is Guardrail 6's *"a declaration read is not a value exercised"* — committed inside a gate
written to enforce exactly that discipline. **Every lane in this project works in a detached
worktree**, so the gate could not be self-certified by the workers it exists to protect.

The ADR sentence is **true of `352c514`'s certification environment** and is a correct record of it;
W7 annotated ADR-0093 in `docs/DECISIONS.md` rather than rewriting it, and this entry does the same.

### The class — why this earns a number rather than a comment

1. **An instrument's verdict can depend on which checkout it runs in even when the thing it measures
   is identical in both.** `ruff` is the same binary, at the same version, resolving the same 182
   files, in both environments; only the verdict differs.
2. **"INEXPRESSIBLE" is a property of the environment, not of the mutation.** `352c514` reported the
   un-ignoring of `references/*/` as an inexpressible mutation and correctly refused to count its
   green. In W7's populated control the same mutation is expressible and fires — **2 failed / 2
   passed** *(W7's figure; W8 built no populated control and did not re-derive it)*. A lane that
   writes off a mutation as inexpressible has measured its checkout, not its instrument.

### A separate finding, disclosed as a boundary and deliberately NOT patched

The gate's scope check has **zero discriminating power in a worktree**. Re-derived by W8 in its own
worktree at `f6a2e4e`: `ruff check --no-cache --show-files .` resolves **182** files and
`--no-respect-gitignore` resolves the **same 182**, of which **0** are under `references/`, so
`.gitignore` keeps **0** files out. Cross-derived a second way: `git ls-tree -r --name-only f6a2e4e
| grep -c '\.py$'` = **181** tracked `.py`, plus `pyproject.toml` = 182; and `find references -name
'*.py'` in a worktree returns **0**, because `references/` holds only tracked `.md` files there —
the sibling git repositories that make it large are untracked and absent from any worktree.

So that check **cannot fail in a worktree for the reason it exists**. W7 did not patch it: patching
it would be a convention wearing a mechanism's clothes. It is **disclosed where a lane meets it** —
the check emits a `UserWarning` naming the environment and the measured zero whenever `.gitignore`
keeps nothing out, silent where the check does discriminate. This entry states that plainly rather
than implying the instrument is equally trustworthy in both environments: **after the fix in flight
lands, checks 1, 2 and 4 are self-certifiable in a worktree and check 3 is green by construction
there.** A disclosure is not a mechanism, and this leg stays open by design.

### What this entry does not establish

* **No populated-checkout control was built by W8.** W7's `CTL` figures — 1,673 `references/` files,
  the 6,970 bounded-above cross-derivation, and the (f) mutation's 2 failed / 2 passed — are W7's
  and are **not** re-derived here.
* **The primary checkout was not touched** by W8: no edit, no `pytest`, no `ruff`. A whole-tree
  suite was running there. Only read-only `git` was run against it.
* **A second instance of the same class is inherited unswept**: W7 reports that
  `.claude/settings.json` allowlists the file-scoped `ruff check src/ tests/`, narrower than
  `ruff check .` by exactly `pyproject.toml`, so a lane reaching for the allowlisted form gets a run
  that cannot see a `pyproject.toml` finding. Neither W7 nor W8 swept that independently.
* **Status.** `OPEN`, not `FIXED, LANDED`, because the fix was uncommitted when this was written —
  checked against `git log`, not assumed. When it lands, the status field takes the landed SHA and
  the scope-check boundary above stays open as a stated boundary.

---

## D86 — PARTLY ADDRESSED. §12.47 required every `ProcessPoolExecutor` to be built with an initializer that calls each registry's `discover()`, "asserted by inspecting the initializer arguments". No such initializer exists, no test mentions one, and six other places in the tree assert that it does — three in `docs/SPEC.md` prose (§7.2, §7.5, §11.1) and three in `src/fleet/ecosystems/base.py`. §12.47's criterion sentence is RETIRED and all three SPEC prose sites carry `D86` markers (`827fc6e`); the three `src/fleet/ecosystems/base.py` sites still assert it unqualified

> **[2026-08-28, round-K lane W4 — this heading was rewritten because it was wrong twice, and the retired wording is quoted here so nothing is laundered. The BODY below is untouched; every correction to it is a dated marker beside the sentence it corrects, never an edit to it.]**
>
> The heading read: *"**D86 — OPEN.** §12.28 and §12.47 **both** require every `ProcessPoolExecutor` … and **five** other places in the tree assert that it does"*. Two errors and a status change:
>
> * **§12.28 does not carry this claim.** `docs/SPEC.md` §12.28 requires that pool children carry **no database handle**, "asserted by inspecting the initializer arguments" — the same *evidence* phrase, a different and weaker *requirement*, and one that is **true in the tree today**: the sole `ProcessPoolExecutor(` (`src/fleet/orchestrator/budgets.py:947-949`) passes `max_workers=` and `mp_context=` only, so no handle can cross. Retiring §12.28 alongside §12.47 would have discarded a true, cheap requirement. Only §12.47 carried the `discover()` claim; only §12.47's copy is retired. §12.28 now carries a marker saying so.
> * **Six sites, not five, and the SPEC list named the wrong sections.** The body below says the SPEC prose sites are "§5 and §9". A fence-aware nearest-preceding-heading walk over `docs/SPEC.md` at `12be741` puts them at **§7.2** (`:5386-5387`), **§7.5** (`:5587-5588`) and **§11.1** (`:6796-6797`) — **three** SPEC prose sites, not two, so the total is **six**, not five. Class result, which is what reproduces: a normalised sweep of `src/` and `docs/SPEC.md` for the cpu-pool-initializer claim returns exactly two families — the three `src/fleet/ecosystems/base.py` docstrings (`:109`, `:118` **inside a raised message**, `:632`) and the three `docs/SPEC.md` prose paragraphs above — plus §12.47's criterion, which is the sentence being retired rather than a site asserting it.
> * **Status is now `PARTLY ADDRESSED`**, per this file's "Status vocabulary, used strictly" block: *some legs landed, others still open; the entry says which, and which SHA carries the landed half.* **Landed (`827fc6e`):** §12.47's sentence is retired in `docs/SPEC.md`; §12.28 is marked as not carrying the claim; the three SPEC prose sites carry `D86` markers; ~~§13 row 36 is marked~~ **[clause struck 2026-08-28, round-K lane W5 — see the next bullet]**. **Still open:** `src/fleet/ecosystems/base.py:109`, `:118`, `:632` still assert the mechanism as settled fact — `src/` was outside this lane's ownership, and `:118` is printed to an operator at the moment the registry check fails, which is the sharpest of the three.
>
> * **[Correction 2026-08-28, round-K lane W5 — measured at `506cadb`; the clause struck above is quoted here verbatim so a sweep for it finds this correction and not a survival.]** The struck clause is *“§13 row 36 is marked”*. It is **struck, not repointed**, and it is not a fabrication about the tree: `827fc6e` did add a marker to §13 row 36 (`docs/SPEC.md:7483`) and that marker is still there. It is a **category error**. That marker belongs to a *different* repair carried in the same commit — it reads “`llm/routing.py` does not exist and never has” — and it contains **zero** occurrences of `D86`, of `initializer` and of `cpu_pool`; so does every other row of §13 (`docs/SPEC.md:7440-7496`), swept for all three tokens. The clause entered `D86`'s leg list because that list was derived from what the commit *touched* rather than from what carried the *claim* — the failure `CLAUDE.md` §3 names as “a commit groups by file ownership, not by claim”. **There is no unnamed `D86` site to repoint it at.** A form-agnostic `\bD86\b` sweep of `docs/SPEC.md` returns exactly six lines — `docs/SPEC.md:5389` (§7.2), `docs/SPEC.md:5591` (§7.5), `docs/SPEC.md:6816` (§11.1), `docs/SPEC.md:7416` (§12.28), `docs/SPEC.md:7435` (§12.47) and `docs/SPEC.md:7428` (§12.40). The first five are exactly the three clauses that survive above; the sixth is a *see-also* pointer at the tail of §12.40's marker for the same unrelated `llm/routing.py` repair, not a site that asserts or retires the `cpu_pool` claim — so repointing the clause at §12.40 would repeat the error it is being struck for. The three surviving clauses were each re-grepped at `506cadb` against the sites they name, and each site says what is claimed of it.
>
> **The remedy is adjudicated: RETIRE the claim, do not build the initializer.** `mp_context` is unconditionally `forkserver` (`budgets.py:948`), so a pool child genuinely does not inherit the parent's **in-process** registry state. That alone does not settle the body's "import side effects at module load" hypothesis — `forkserver` children still execute module-level code for whatever modules unpickling the submitted callable imports — so it was measured separately and **is refuted**: an `ast` pass over `src/fleet` that counts `discover()` calls reachable at **import time only** (every `FunctionDef`, `AsyncFunctionDef`, `ClassDef` and `Lambda` body excluded, since none of those runs on import) finds **zero**, and `set_forkserver_preload` is called nowhere in `src/` or `tests/`, so nothing preloads a registry either. The first pass at this measurement was wrong in the opposite direction — it reported nine call sites because `ast.walk` descends into function bodies — and the nine resolve to `ecosystems/base.py`, `manifests/base.py`, `orchestrator/registry.py` and `cli.py` call sites that are all **inside** functions. Recorded because a walk that silently includes unexecuted code is the same shape of error this entry exists to correct. But the property is unreached rather than violated: exactly one callable is ever submitted to that pool — `scan_file`, through the tree's only `run_in_executor` (`src/fleet/workers/symbolindex.py:266-268`) — and an `ast` walk of `scan_file`'s body names no `discover()`, no registry and no adapter. Adding the initializer would import `anthropic`, `boto3`, `openai` and `google.auth` in every pool child and convert a startup `RuntimeError` naming the missing adapter into a `BrokenProcessPool`, **inverting** CLAUDE.md Rule 11 to buy a property nothing needs. This closes the body's third "What this entry does NOT establish" bullet ("No fix is proposed here") and answers its second ("the blast radius was not measured"): the blast radius is one callable, and it is registry-free.

**Found by:** the 2026-08-27 audit of `docs/SPEC.md` §12 against the test suite, at base `076076d`
(tree clean). Allocated centrally by the orchestrator after a form-agnostic sweep; see "Allocation"
below.

### The measurement

`ProcessPoolExecutor(` has exactly **one** occurrence in `src/` and `tests/` combined —
`src/fleet/orchestrator/budgets.py`, inside `new_cpu_pool` — and it passes `max_workers=` and
`mp_context=` only. The token `initializer=` appears **nowhere** in `src/` or in `tests/`. So the
clause is not weakly covered or covered at the wrong scale: the thing it asserts about does not
exist, and the assertion it names ("inspecting the initializer arguments") could not be written
today against anything.

Re-derived three times before landing: two audit lanes reached it independently without shared
context (one auditing §12.28, one auditing §12.47), and the orchestrator re-measured it by hand.

### Why this is worse than an ordinary missing test

Five places state the initializer as settled fact, so the tree reads as if the property holds:

* `src/fleet/ecosystems/base.py` — three separate statements that `discover()` runs "in every
  process, including each `cpu_pool` initializer". **One of the three is inside a raised error
  message**, so the claim is printed to an operator at the moment the registry check fails.
* `docs/SPEC.md` §5 and §9 carry the same claim in prose.
  *(Corrected 2026-08-28, round-K lane W4 — measured at `12be741`: the sites are **§7.2** `:5386-5387`, **§7.5** `:5587-5588` and **§11.1** `:6796-6797`. Three, not two, and neither is in §5 or §9. The retired "§5 and §9" is quoted above on purpose so a sweep for it finds this correction and not a survival. All three now carry `D86` markers in `docs/SPEC.md`.)*

This is the CLAUDE.md Guardrail 7 shape — "the SPEC says X but the code cannot do X" is **two
edits, not one**. A reconciler who trusts the docstrings will make the *code* match a claim that
nothing enforces, and will reasonably believe they are fixing a bug rather than implementing an
unbuilt feature.

### What this entry does NOT establish

* **Not shown to cause a live defect.** `new_cpu_pool`'s children may already resolve their
  registries by another route — import side effects at module load are the obvious candidate. This
  entry records that the *asserted mechanism* is absent and that five sites claim otherwise; it does
  **not** claim a pool child fails to resolve an adapter. Establishing that needs a run, not a grep,
  and no such run was made.
* **The blast radius was not measured.** Whether any pool child actually needs a registry it would
  not otherwise have is unexamined.
* **No fix is proposed here.** The audit that found this was scoped to measurement. Whether the
  remedy is to add the initializer, or to retire the claim from all five sites, is undecided — and
  the two answers have different costs.

### Status

`OPEN`. Nothing fixes it, checked against `git log` rather than assumed: `initializer=` has never
appeared in this tree. The SPEC sentences in §12.28 and §12.47 were **left standing** by the audit
that found this, deliberately — correcting them is the second half of the two-edit pair above and
belongs with the adjudication, not with a measurement pass.

> **[2026-08-28, round-K lane W4 — SUPERSEDED AS A STATUS FIELD; kept as a record. The heading is this
> entry's only status field and now reads `PARTLY ADDRESSED`; read it, not this block.]** The paragraph
> above was accurate at `12be741` and is left exactly as its author wrote it. Two things in it have since
> changed: §12.47's sentence is no longer standing (retired at `827fc6e`), and §12.28 was never the
> same sentence — see the heading's marker. `initializer=` still occurs **zero** times in `src/` and
> `tests/`, so the mechanism half is unchanged. Stating `OPEN` in both a heading and a body block is the
> divergence D63 had to be repaired for; this annotation collapses the authority to the heading without
> rewriting the record.

### Allocation

`D86` was verified free before writing, form-agnostically: `\bD86\b` over `docs/` returned **0**
occurrences. Its only occurrence anywhere in the working tree was in a git-ignored round-G lane
report reading "D86+ free" — a negative mention, not an allocation. That is the D71 trap named in
`CLAUDE.md` §3, avoided by reading the body rather than trusting the match. `D85` was confirmed a
real entry with a `## D85 — OPEN` heading. Per `CLAUDE.md` §3 no census total or range is quoted.

---

## D87 — FIXED, LANDED (`41fdfa1`). A fabricated (or merely stale, non-NULL) `attempts.commit_sha` is never corrected by `fleet resume`'s git arbitration, only `phases.post_commit_sha` is — the two pointers §11.5's authority table pairs can disagree with each other after a resume the harness reports as a clean, applied reconciliation

**Found by round P task 1 (2026-08-31), while building the fabricated-reverse-disagreement
positive fixture SPEC.md §12 item 15 names.** `D86`'s allocation was verified free the same way
this one was: `\bD87\b` over `docs/` returned **0** occurrences before this entry was written.

**The claim, and what it is not.** §12 item 15 requires: "Fabricating the reverse disagreement —
hand-editing `attempts.commit_sha` to a SHA that is not on the branch — makes resume correct the
column, never `git reset` the branch to match it." The second half holds — measured directly, not
assumed: the landed/discarded verdict is driven by Git alone (a `_reconcile_tasks_with_git` read
never consults `attempts.commit_sha`), and the branch is never reset to agree with a fabricated
pointer. **The first half does not hold** for the specific case where `attempts.commit_sha`
already carries *some* value — right or wrong — rather than `NULL`.

**Mechanism, in `src/fleet/cli.py::_persist_arbitration`:**

```sql
UPDATE attempts SET commit_sha = ? WHERE attempt_id = (
    SELECT attempt_id FROM attempts
     WHERE run_id = ? AND task_id = ? AND commit_sha IS NULL
     ORDER BY attempt DESC, revalidation_round DESC, retry_ordinal DESC
     LIMIT 1)
```

The `commit_sha IS NULL` predicate is deliberate and correctly guards a real, different hazard
(picking the wrong `attempts` row among several for the same `task_id` when one already recorded
its own commit — `docs/DECISIONS.md` §5b). Its zero-match case is already handled for the
*absent-row* scenario: `cursor.rowcount == 0` is reported by name as `provenance_missing`, and
`test_resume_step4_reports_a_landed_commit_no_attempts_row_could_record` covers exactly that
(`with_attempt_row=False` — no row exists at all). **The same predicate produces the identical
zero-match outcome for a second, unhandled scenario: a row exists, and its `commit_sha` is
already non-NULL** (fabricated, or merely stale from an earlier, unrelated write) — here there
*is* a row that needs correcting, and the SPEC's criterion says correcting it is exactly what
must happen, but the guard silently declines the same way it does for "nothing to correct."
`phases.post_commit_sha` carries no equivalent guard and is corrected unconditionally in the same
transaction, so the two pointers diverge from each other, and **no `provenance_missing` entry is
emitted for this case either** — that finding was built to disclose the absent-row scenario, not
this one, so a resumed run reports `applied: true` with nothing surfacing the disagreement.

> **[Dated annotation, 2026-08-31, documentation-accuracy review — the sentence above overclaims
> the severity; body left untouched, this is the correction.]** "No `provenance_missing` entry is
> emitted for this case either" is false against the pre-fix code. The pre-fix branch, read from
> `41fdfa1`'s diff, was `if not cursor.rowcount: unwritten.append((task_id, repo_id, phase, sha))`,
> and every `unwritten` entry the caller receives is appended to `report["provenance_missing"]`
> unconditionally (`src/fleet/cli.py`, the loop after `_persist_arbitration` returns). The
> `UPDATE ... WHERE attempt_id = (SELECT ... AND commit_sha IS NULL ...)` subquery matches zero
> rows in the fabricated-non-NULL-`commit_sha` case just as it does in the absent-row case — same
> `cursor.rowcount == 0`, same code path, same append. So this case's disagreement **was**
> surfaced pre-fix, via a `provenance_missing` entry — just one carrying a misleading label,
> reporting "no `attempts` row could record this commit" for a row that demonstrably existed and
> already held a (wrong) commit. That is real and worth having fixed, but it is materially milder
> than "nothing surfacing the disagreement": an operator reading `provenance_missing` pre-fix would
> have seen an entry naming this `task_id`/`repo_id`/`phase`, even if its label pointed at the
> wrong cause.

**Reproduced by a genuinely discriminating fixture, not merely asserted.** A real, divergent
commit (`git checkout -b rogue`, one commit, off the `migrate/<repo>` branch entirely — not
"not yet landed", which is the existing discard test's scenario) is hand-written via raw SQL into
`attempts.commit_sha` — never through `_land_task_commit` or any path `fleet resume` itself uses.
Driving `fleet resume --no-continue --json` against this fixture: the verdict is correctly
unaffected by the corruption (`landed` reports the real `sha`, not the fabricated `rogue_sha`),
the branch is correctly never reset, but `attempts.commit_sha` measures `rogue_sha` (uncorrected)
while `phases.post_commit_sha` measures `sha` (corrected) — the exact divergence this entry
describes. Test: `tests/test_cli.py::test_resume_step4_corrects_a_fabricated_attempts_commit_sha_pointing_off_branch`,
committed as `@pytest.mark.xfail(strict=True, ...)` (this codebase's established convention for a
known, open, executable-proof defect — `tests/test_build_e2e.py` history uses the same pattern).

**Shape of the fix, not yet built:** key the guard on `attempt_id` — "is this the row the
arbitration scan's own candidate selection chose" — rather than "is `commit_sha` currently
`NULL`", so an already-wrong value in the *targeted* row is still corrected while a *different*
row's already-recorded commit is still left alone (the hazard the `NULL` guard exists to prevent).
After the fix: remove the `xfail` marker so the test becomes the green proof, and re-run
`tests/test_cli.py -k step4` to confirm both precedent tests and the `provenance_missing` test
(`with_attempt_row=False`) still pass unchanged — that one's zero-match case must remain reported,
not start silently "succeeding" against a row that was never there.

> **[Fixed 2026-08-31, `41fdfa1`, round P task 1. Heading updated above; this is the annotation,
> not a rewrite of what precedes it.]** `_persist_arbitration`'s `attempts` write is now a
> select-then-update: the target `attempt_id` is fetched via the unchanged `task_id` scope +
> `ORDER BY attempt DESC, revalidation_round DESC, retry_ordinal DESC LIMIT 1` (no `commit_sha`
> filter), then that specific row is updated unconditionally — exactly the shape described above.
> Reviewed Approved: the row-selection SQL confirmed identical to the pre-fix query minus the
> dropped `NULL` predicate, the two-statement form confirmed atomic within `StateWriter`'s single
> `BEGIN IMMEDIATE` transaction, the `provenance_missing` absent-row test re-verified unchanged,
> and a reviewer-reproduced (not just implementer-reported) revert-based mutation check failed at
> the identical original assertion. `xfail` marker removed; the test is now the green proof
> (`tests/test_cli.py::test_resume_step4_corrects_a_fabricated_attempts_commit_sha_pointing_off_branch`).
> **A pre-existing, disclosed limit found during review, not introduced by this fix:** the
> `task_id` scope this fix (and the original query) depends on is populated by zero production
> write sites — every `AttemptRow` construction in `src/fleet/` leaves `task_id` at its `None`
> default, and the per-unit task queue (`upsert_task`/`claim_next_task`) that would populate it is
> fully built and unit-tested but has no production caller. So step 4's "correct `attempts.commit_sha`"
> mechanism, as written and as now fixed, may not currently fire against any row a real `fleet
> resume` produces — every test exercising it, including this fix's own, seeds rows directly via
> raw SQL. Tracked separately as D89; this entry's own fix is correct and complete for the
> mechanism as specified, independent of whether that mechanism is reachable today.

---

## D88 — FIXED, LANDED (`c410999`), SECURITY-RELEVANT. `phases.last_error` is written unredacted by the only real `complete_phase` implementation, contradicting `docs/SPEC.md`:6987's explicit claim that `state/repository.py` redacts it on write — a secret embedded in an exception's `str()` persists verbatim to a durable, queryable SQLite column

**Found by round P task 2 (2026-08-31), while building §12.20's redaction-coverage tests for
`phases.last_error`.** Verified free before writing: `\bD88\b` over `docs/` returned **0**
occurrences.

**The contradiction, quoted.** `docs/SPEC.md:6987` (§11.4, "Redaction — mandatory, at the write
boundary, not at review time"): *"`state/repository.py` redacts `last_error`, `findings.payload`,
and `attempts.*_tail` on write."* The only concrete `complete_phase` implementation
(`src/fleet/state/repository.py:1358`, on `SqliteStateRepository` — the sibling declaration at
`:498` is an abstract Protocol method, `...` body, not an implementation) takes `last_error` as a
plain parameter and writes it directly into the `UPDATE phases SET ... last_error = ? ...`
statement's params tuple, with **no `redact_text`/`redact()` call anywhere in the function**.

**The unredacted path, traced end to end.** `workers/base.py::error_from_exception` (the
generic catch-all for any exception escaping any worker's `run()`) builds
`WorkerError.stderr_tail=str(exc)` — its own docstring cites §11.4 by name and correctly avoids a
full traceback ("a traceback carries local variables, and locals carry credentials"), but `str(exc)`
itself is not redacted either, and an exception message can just as easily echo a credential
(a failed HTTP call embedding an auth header in its message, a subprocess error echoing a URL with
embedded token, etc.). `orchestrator/runner.py::_complete` (`:937`) passes `last_error` straight
through to `repository.complete_phase` with no redaction call in between.

**Reproduced against a real, persisted row, not asserted.** A `github_pat_…`-shaped credential
embedded in a raised exception's message was driven through this path end-to-end and read back
from a real SQLite `phases` row: the credential persisted verbatim. Two mutation-proven regression
tests were added this round for the *other* two redaction gaps §12.20 named (`_abandon_repo`'s
`phases.last_error` write in `cli.py`, and `CachingModelClient`'s `llm_cache.response_json` write)
— both of those write paths DO redact correctly and are now covered. This third path
(`complete_phase`'s own terminal write, the one every phase transition goes through) is the one
that doesn't, and per the task's TEST-ONLY scope no test asserting it as safe was written (it
isn't), and no production code was touched.

**Not yet built:** a fix adding a `redact_text`/`redact()` call to `complete_phase`'s `last_error`
parameter (or to `_complete`'s call site, or to `error_from_exception` itself — three viable
insertion points, not yet adjudicated) before it reaches the `UPDATE` statement, plus a
discriminating test proving a planted secret does not survive this specific path (the pattern the
two sibling tests already established this round). The PR-body `«redacted:…»` placeholder clause
of §12.20 remains separately unverified (out of this entry's scope).

> **[Fixed 2026-08-31, `c410999`, round P task 2. Heading updated above; this is the annotation,
> not a rewrite of what precedes it.]** Controller ruled on the insertion point: inside
> `complete_phase` itself, in `state/repository.py`, immediately before `last_error` enters the
> `UPDATE` params tuple — `None if last_error is None else redact_text(last_error)` — matching
> `docs/SPEC.md:6987`'s literal "`state/repository.py` redacts... on write" and giving
> defense-in-depth over every current and future caller of `complete_phase`, not only
> `runner.py`'s. Reviewed Approved across two rounds (one fix round, for a docstring claim that
> overstated `redact_text`'s general idempotency — narrowed to what was actually verified, with
> the `authorization`-kind self-collision counterexample named rather than hidden). Both reviews
> independently reproduced the fix location, the `None`-handling, the mutation check (revert →
> genuine RED with the raw PAT visible → restore → green), and confirmed no other `complete_phase`
> caller exists that would need separate treatment. Test:
> `tests/test_repository.py::test_complete_phase_redacts_a_credential_in_last_error_before_the_write`.
> The PR-body `«redacted:…»` placeholder clause remains separately unverified, as before.

---

## D89 — FIXED, LANDED (`1739453`, merge of `agent/roundu-task1-fix2` — the landed, post-merge commit on `main`; component work: Phase 1 at `1b0d3c1`, Phase 2 Task A at `09bc0f8`, Task B at `91daa25`, fix waves at `615b5ba`/`7d2d0da` — see the dated addenda below for what each landed). `attempts.task_id` is never populated by any production write site — the per-unit task queue (`upsert_task`/`claim_next_task`) it depends on is fully built and unit-tested but has zero production callers, so any mechanism scoped by `task_id` (D87's git-arbitration fix among them) may not currently fire against a row a real `fleet resume` produces

**Found by round P task 1's reviewer (2026-08-31), disclosed while verifying D87's fix rather than
searched for independently — recorded here rather than left inside D87's own entry, since it is a
different, pre-existing defect D87 did not introduce and does not depend on.** Verified free
before writing: `\bD89\b` over `docs/` returned **0** occurrences.

**The gap, as measured.** Every `AttemptRow(...)` construction in `src/fleet/` was swept
(`grep -rn "AttemptRow(" src/fleet/`) — exactly two production write sites exist,
`cli.py`'s `_TransformSink.__call__` and `record()` — and **neither ever sets `task_id`**; both
leave it at the dataclass default (`None`, per `state/repository.py`'s `AttemptRow` definition).
The per-unit task queue that would populate a real `task_id` — `upsert_task`/`claim_next_task`
(`state/repository.py`) — is itself fully implemented and covered by its own unit tests, but
`claim_next_task(` occurs nowhere in `src/fleet/` outside its own definition; its only callers in
the whole tree are in `tests/test_repository.py`. Every test that exercises a `task_id`-scoped
mechanism (D87's `_persist_arbitration`/`_reconcile_tasks_with_git` among them) seeds `tasks`/
`attempts` rows directly via raw SQL rather than through any code path a real run would take.

**What this does and does not mean.** It does not make D87's fix wrong — the fix is a correct,
complete implementation of the mechanism exactly as `§11.5` specifies it, and the reviewer
confirmed this independently. What's open is whether that mechanism is *reachable* at all in a
real run: if nothing ever populates `task_id`, the `task_id = ?` scope every version of this query
(before and after D87's fix) relies on may never match a real `attempts` row, meaning git
arbitration's correction step could be permanently inert in production despite being correct and
tested in isolation — the same "declared and unit-tested but never wired to a real caller" shape
this ledger has recorded before (D56, D57, D80 pre-fix).

**Not yet built:** trace whether `task_id` is genuinely meant to be populated by a currently-missing
wiring step (in which case this is a wiring gap, closable the way D80 was), or whether the design
intent has shifted since `task_id`-scoped queries were written and the scoping should instead key
on something a real write path does populate — that adjudication is not made here.

> **[Traced 2026-08-31, round R research dispatch — findings, not a fix; this is still OPEN.]**
> The gap is wider than this entry's original text: the `tasks` table itself has **zero
> production `INSERT`s anywhere** in `src/fleet/` — `upsert_task` (`state/repository.py`) is the
> only `INSERT INTO tasks` in the tree, and its only callers are in `tests/test_repository.py`.
> Consequently D87's driving query (`_ARBITRATED_TASKS_SQL`, `cli.py:11838`,
> `FROM tasks WHERE status='RUNNING'`) always returns zero candidates against a real `fleet
> resume`, so `_reconcile_tasks_with_git` exits at `candidates: 0` before `_persist_arbitration`
> is ever invoked. D87's fix is not merely "may not currently fire" — it is **provably always
> inert against real `fleet resume` traffic today** (the mechanism is correct and reachable only
> from `tests/test_repository.py`'s direct calls).
>
> This is a wiring gap in spirit (D80's shape), but bigger: a real per-unit task identity DOES
> exist in production — `workers/rewrite.py:107`'s `task_id_for` computes a deterministic UUID5
> already embedded in real `Fleet-Task-Id` git trailers on every rewrite/relocate commit — it is
> simply never persisted to SQL. Closing this is not a single missing call, though: there is a
> genuine cardinality mismatch (`land_patches` runs once per unit but one phase-dispatch
> `attempts`/`AttemptRow` write can correspond to N per-unit commits/task ids), and HOIST/
> REVALIDATE task kinds have no insertion path at all — `upsert_task` explicitly refuses those
> kinds, deferring to an "own path" that does not exist.
> **Recommend:** scope as its own ADR/task in a future round rather than a quick D87 follow-up
> patch. Full trace: `.superpowers/sdd/round-R-criteria-closure/research-1-d89-report.md` (session
> workspace — may be deleted by the time this is read; the paragraph above is the durable record).

> **[Fix design scoped 2026-08-31, round S research dispatch — a design, not a fix; still OPEN.
> This is the most consequential thing found about D89 to date: a naive fix would make it worse.]**
> The right value for `attempts.task_id` and the right value for D87's `find_task_commit`
> git-trailer lookup are **different quantities**. A coarse UUID4 minted once per phase-dispatch
> (right after `acquire_phase_lease` succeeds, `runner.py:511-518`) is the correct grain for
> `attempts.task_id` — but REWRITE/RELOCATE commits carry `task_id_for`'s **per-unit** UUID5 in
> their real `Fleet-Task-Id` git trailers, not the coarse dispatch-level id. Swept: only
> `rewrite.py`/`relocate.py` ever write that trailer at all. So **D87's arbitration mechanism is
> reachable-and-correct for zero task kinds today** — not merely inert. Populating `tasks` at the
> coarse per-dispatch grain (the natural-looking fix) would make the mechanism *reachable* while
> feeding it the *wrong* identity for REWRITE/RELOCATE — `find_task_commit` would fail to match a
> real landed commit's trailer, and D87's own logic ("Git is authoritative and the SQLite row is
> corrected") would then correct a genuinely-landed row back toward "not landed," discarding real
> work on crash recovery. **A naive close of D89 would be a regression, not a fix.**
> Confirmed out of scope, not merely unwired: HOIST/REVALIDATE task kinds have no commit-producing
> path anywhere in the tree that could carry a trailer, so no fix needs to cover them.
> **Recommended shape (two gated phases, genuinely too large for one task):** Phase 1 — populate
> `attempts.task_id`/the `tasks` lifecycle for real, in the real dispatch loop, at the coarse
> per-dispatch grain (new repository method, a `Phase`→`TaskKind` mapping that does not exist
> yet). Phase 2 — rebuild `_reconcile_tasks_with_git`'s REWRITE/RELOCATE branch to loop **per-unit**
> over `tasks.target_paths` using `task_id_for`'s per-unit identity, including a new "partially
> landed" third verdict state (today's arbitration is binary: landed or not). Full design:
> `.superpowers/sdd/round-S-criteria-closure/research-1-d89-fix-design.md` (session workspace —
> may be deleted; this paragraph is the durable summary). Recommend its own dedicated round, not
> folded into a general TEST-ONLY round's task list.

> **[Phase 1 LANDED 2026-09-01, round T task 1 — ADR-0101. D89 remains OPEN; this is a partial
> fix, not a close.]** `attempts.task_id`/the `tasks` lifecycle is now populated at the coarse
> per-dispatch grain the round-S design above scoped as Phase 1: `src/fleet/cli.py`'s
> `_coarse_task_id` (called from `_TransformSink.__call__` and `_AttemptWriter.record`, shared by
> `_BuildSink`/`_VerifySink`) mints/reuses one `tasks` row per `(run_id, repo_id, phase)` via
> `upsert_task` — now `RETURNING task_id`, `state/repository.py` — and stamps it onto every
> `attempts` row that dispatch writes. Re-derivation for this task found the round-S design's own
> regression-risk framing too narrow: `_ARBITRATED_TASKS_SQL` has no `kind` filter, so the
> "naive fix would make it worse" hazard is structural to **any** coarse `tasks` row that reaches
> `status = 'RUNNING'` (TRANSFORM/BUILD/VERIFY alike), not just REWRITE/RELOCATE. Phase 1 closes
> this by construction rather than by a kind-guard: `_coarse_task_id` calls `upsert_task` and
> nothing else, `upsert_task` never writes `status` (schema default `PENDING` survives every
> UPSERT), and `claim_next_task` — the only other write to `tasks.status` in the tree, still zero
> production callers — is never invoked. Proof, not argument: `tests/
> test_d89_phase1_task_lifecycle.py` scenario E executes the PRODUCTION `_ARBITRATED_TASKS_SQL`
> query against a real Phase-1 row and asserts zero candidates, a sibling test proves that same
> query is not vacuously empty (forces `RUNNING` via raw SQL, asserts the row then IS selected),
> and both were proven under a real code mutation of `upsert_task` (Rule 12) that reddened
> scenario E plus 3 other tests, 49/53 unaffected — not a module-wide outage. Full argument:
> `docs/DECISIONS.md` ADR-0101.
>
> **What is still OPEN and unbuilt (Phase 2, per the round-S design above, still a future round):**
> no per-unit REWRITE/RELOCATE `tasks` rows exist, so D87's arbitration mechanism remains inert for
> the per-unit reconciliation it was built for — nothing puts a matching row at `status =
> 'RUNNING'` for `_reconcile_tasks_with_git`'s per-unit REWRITE/RELOCATE branch to act on, and that
> branch is unchanged (zero lines). `task_id_for`'s per-unit UUID5 git-trailer identity is also
> unchanged and still not persisted to SQL anywhere. Closing D89 fully still needs Phase 2's
> per-unit rebuild, including the new "partially landed" third verdict state the round-S design
> names.

> **[Phase 2 Task A landed 2026-09-01, round U task 1A — ADR-0102. D89 remains OPEN/PARTLY
> ADDRESSED; this is one of two Phase-2 sub-tasks, not a close.]** The TRANSFORM coarse `tasks`
> row (Phase 1's `kind = 'REWRITE'` row) now gets a real `PENDING -> RUNNING -> {DONE, PENDING}`
> claim lifecycle, scoped to one dispatch window: `orchestrator/runner.py`'s `PhaseRunner` gains an
> optional `pre_dispatch: PreDispatchHook[I]` collaborator (mirrors the existing `sink=` shape),
> called once in `_dispatch` on the final payload, before the worker executes; `cli.py`'s TRANSFORM
> `PhaseRunner(...)` site (only) wires in `_TransformClaimHook`, which populates
> `tasks.target_paths` and claims the row `RUNNING` via two new `state/repository.py` primitives
> (`set_task_target_paths`, `claim_task_by_id`); `_TransformSink.__call__` resolves the row to
> `DONE` on `status == "ok"` or back to re-claimable `PENDING` (fence bumped) otherwise. Identity
> design: reuses Phase 1's single coarse row (populating the schema's pre-existing
> `target_paths` JSON column) rather than minting N per-unit rows — full rationale in
> `docs/DECISIONS.md` ADR-0102 §"Identity-key decision". BUILD/VERIFY/SCAN are unaffected by
> construction (their `PhaseRunner(...)` sites pass no `pre_dispatch`) — verified both structurally
> (an AST census of all four `PhaseRunner(...)` call sites) and functionally (a hookless coarse row
> stays invisible to `_ARBITRATED_TASKS_SQL`, re-running Phase 1's own scenario-E pattern).
>
> **What is still OPEN — Task B, future, unbuilt.** `_reconcile_tasks_with_git`'s REWRITE/RELOCATE
> branch is UNCHANGED (zero lines): it still treats a candidate row's own `task_id` as the git
> trailer identity, which was never true for REWRITE/RELOCATE (`task_id_for`'s per-unit UUID5 is
> the real trailer identity — see the round-S design note above). **This makes Task A alone a NEW
> way to reach the pre-existing hazard**, not a fix for it: a TRANSFORM dispatch that crashes
> mid-flight now leaves a genuinely `RUNNING` row that `_ARBITRATED_TASKS_SQL` WILL select on the
> next `fleet resume`, `find_task_commit` will almost always find nothing (wrong identity), and
> `discard_task` would `reset --hard`/`clean -fdx` real landed unit commits away. Task B (the
> per-unit `task_id_for`-keyed reconciliation loop and the "partially landed" verdict) must land
> before this path is safe for real crash recovery — see ADR-0102's "Cost if wrong".

> **[Correction 2026-09-01, fix wave: the mechanism named just above is overstated —
> `discard_task` could not actually have fired.]** A source sweep of `src/fleet/` for every writer
> of `tasks.pre_commit_sha` (the sole `INSERT INTO tasks`, `state/repository.py:1219`, omits the
> column; every `UPDATE tasks` site was checked and none names it either) plus a runtime probe
> both found **no production writer of `tasks.pre_commit_sha` anywhere in `src/fleet/`** — see
> `D91` below. `_reconcile_tasks_with_git` reads that column to build `task_anchor` and guards
> `if task_anchor is None: _unresolved(...); continue` **before** any branch that could call
> `discard_task`, for both the REWRITE and non-REWRITE paths. So a crashed TRANSFORM dispatch
> under Task-A-alone would not have had its landed unit commits deleted — `task_anchor` is always
> `None` in production, so the row would instead have gone permanently `unresolved` and stuck
> `RUNNING` forever, never re-examined. The underlying point this addendum makes — that Task A
> alone is a new way to reach a real hazard, and Task B must land before this path is trustworthy
> — still holds; only the specific mechanism (commit destruction vs. permanent hang) was wrong.
> Same correction applied to ADR-0102's "Cost if wrong" and ADR-0103's opening paragraph
> (`docs/DECISIONS.md`).

> **[Phase 2 Task B landed 2026-09-01, round U task 1B — commit `91daa25` on branch
> `agent/roundu-task1b`, based on Task A's `agent/roundu-task1a` at `09bc0f8` (not yet merged to
> `main` as of this addendum — the heading above cites this branch-tip sha per this task's brief;
> if the eventual `main` merge sha differs, the controller should update the heading to match).
> D89's two-phase fix is now COMPLETE.** `_reconcile_tasks_with_git`'s REWRITE-kind branch is
> rebuilt to loop per unit over `tasks.target_paths`, asking git about each unit's own
> `task_id_for_ids`-derived synthetic trailer identity instead of the row's own (never-written-as-
> a-trailer) coarse `task_id` — closing exactly the hazard the Task A addendum above named. Three
> outcomes: every unit landed → the existing `DONE` path (last landed unit's sha onto
> `attempts.commit_sha`/`phases.post_commit_sha`); zero units landed → the existing discard path,
> unchanged; some-but-not-all landed → the **new** third verdict — `discard_task` is NEVER called,
> the row resets to re-claimable `PENDING` with the fence bumped, and the report names the
> landed/missing unit split (`report["partially_landed"]`). Non-REWRITE kinds
> (HOIST/BUILDGEN/RDEP_VERIFY/PR_EMIT/REVALIDATE) are unaffected — verified by a dedicated
> regression test, not merely asserted. Full design rationale, the mutation proof, and what was
> re-verified against Task A's actual landed diff (the round-U research plan's own line numbers
> had shifted): `docs/DECISIONS.md` ADR-0103. Tests: `tests/test_d89_phase2_reconciliation.py`,
> 8 cases against a real git repository with real `Fleet-Task-Id` trailers (no raw SQL
> fabrication of the git side, per CLAUDE.md Rule 9).

## D90 — FIXED, LANDED (8e16653, merged 3c4d165). D88's redaction fix does not cover every `phases.last_error` write path — two raw `UPDATE phases` sites in `orchestrator/runner.py` bypass `complete_phase` entirely, one of them terminal; and the same SPEC sentence's `attempts.stdout_tail`/`stderr_tail` columns are still written unredacted by `repository.py` itself

**Found by round Q's whole-branch review catch-up of round P (2026-08-31), verifying D88's fix
rather than searching for a new defect independently — recorded separately from D88 since it is a
distinct, pre-existing gap D88's fix did not introduce and does not depend on.** Verified free
before writing: `\bD90\b` over `docs/` returned 0 occurrences.

**The gap, as measured.** `docs/SPEC.md`:6987 (quoted verbatim inside D88's own entry): "`state/
repository.py` redacts `last_error`, `findings.payload`, and `attempts.*_tail` on write." D88
fixed exactly one of these three, at exactly one of `phases.last_error`'s call sites:

1. `src/fleet/orchestrator/runner.py:964` (`_terminate_uncharged`) and `runner.py:1055`
   (`_record_diagnostics`) both issue raw `UPDATE phases SET … last_error = ?, …` statements that
   never call `complete_phase` and never call `redact_text`. `_detail()` (`runner.py:1284-1286`)
   returns `error.stderr_tail`, which for a generic `except Exception` is `str(exc)` — the exact
   unredacted-source shape D88's own docstring names. `_terminate_uncharged` is reached from
   `RetryPolicy.decide` returning a non-retryable TERMINATE (`runner.py:704-711`) and is
   **terminal** — the row settles at `REQUIRES_HUMAN_INTERVENTION` with the unredacted value as
   its final persisted state, and `state/projection.py:301` (moved from `:265` by ADR-0106's
   `_derive_updated_at` addition) copies `last_error` straight into the
   projected state with no redaction call anywhere in that module (confirmed by grep).
   `_record_diagnostics` is reached on `RetryAction.RETRY_TRANSIENT` and leaves the unredacted
   value in the column for the retry window, permanently if the process dies there.
2. `record_attempt` (`state/repository.py:2755-2823`, repointed +20 by round VI task 83's D91
   fix growing `claim_task_by_id` above it — pure insertion, confirmed by exact-line-content
   match against the current tree) passes `row.stdout_tail`/`row.stderr_tail`
   into its INSERT params with no redaction call — D88's own pattern, in the same file, ~750
   lines below the fix, not applied to the sibling columns SPEC:6987 names in the same sentence.
   Production caller `_AttemptWriter.record` (repointed fresh below, moved repeatedly by round VI
   tasks 65, 66, and 67's `cli.py` insertions) sets
   `stderr_tail="" if step.ok or error is None else error.stderr_tail`, the same
   `WorkerError.stderr_tail` value D88 traced for `phases.last_error`.

**Concrete failure scenario.** A worker raises an exception whose message quotes a
credential-bearing URL; the retry policy returns a non-retryable TERMINATE; `_terminate_uncharged`
writes the raw message to `phases.last_error`; the row is terminal at
`REQUIRES_HUMAN_INTERVENTION`; `fleet status`'s projection surfaces it verbatim. Independently, any
attempt whose `stderr_tail` quotes a credential persists it verbatim via `record_attempt` regardless
of how the phase itself resolves.

**Not yet built:** the fix — apply `redact_text` at both `runner.py` UPDATE sites and inside
`record_attempt` for `stdout_tail`/`stderr_tail`, matching D88's placement pattern — plus tests
that read the persisted column (not the in-memory `WorkerError` object, which
`tests/test_workers_scan.py:896` already covers and which does not exercise either gap).
`docs/CRITERIA_PLAN.md` §12.20 needs a dated annotation correcting its "3 of 4 (already covered
pre-round)" claim to 2 of 4 pending this fix — per this project's own discipline, annotate in
place, do not rewrite what round P wrote.

> **[Fixed 2026-08-31, round Q, lane W4. Heading updated above; this is the annotation, not a
> rewrite of what precedes it.]** Applied `redact_text` at all three sites the "not yet built"
> paragraph named: `runner.py::_terminate_uncharged`'s and `::_record_diagnostics`'s own raw
> `UPDATE phases … last_error = ?` params (`None if last_error is None else
> redact_text(last_error)`, mirroring D88's exact `complete_phase` pattern), and
> `repository.py::record_attempt`'s `stdout_tail`/`stderr_tail` params
> (`redact_text(row.stdout_tail)` / `redact_text(row.stderr_tail)` — unconditional, no `None`
> guard needed, since `AttemptRow.stdout_tail`/`stderr_tail` default to `""` and `redact_text`
> returns a falsy string unchanged). Three new tests read the *persisted* column off a real
> SQLite row through the real orchestrator/repository write paths (not the in-memory
> `WorkerError`), each with a companion over-redaction control and each proven to discriminate by
> mutation (revert the `redact_text` call → genuine RED with the live `github_pat_…` value
> visible in the assertion diff → restore → green):
> `tests/test_runner.py::test_terminate_uncharged_redacts_a_credential_in_last_error_before_the_write`
> (a non-retryable `DEP_CONFLICT` driven through a real `run_wave`, terminal at
> `REQUIRES_HUMAN_INTERVENTION`),
> `tests/test_runner.py::test_record_diagnostics_redacts_a_credential_in_last_error_before_the_write`
> (a retryable `TRANSIENT_INFRA` failure caught mid-retry via the existing
> `_one_dispatch_then_crash` resource-guard harness, so the row read is exactly what
> `_record_diagnostics` wrote and not masked by a later `complete_phase`/`_terminate_uncharged`
> overwrite), and
> `tests/test_repository.py::test_record_attempt_redacts_a_credential_in_stdout_and_stderr_tail_before_the_write`.
> `mypy --strict` clean on both touched source files; `tests/test_runner.py` (45 tests) and
> `tests/test_repository.py` (47 tests) both pass whole-file, no `-k`. `docs/CRITERIA_PLAN.md`
> §12.20 updated in the same round: 2 of 4 → 3 of 4 named DB columns covered
> (`llm_cache.response_json`, D88's own column, was not re-verified by this task and is left as
> D88 left it). The PR-body `«redacted:…»` placeholder clause remains separately unverified, as
> before.
>
> ***Citation corrections made in place — pointers, not records; no claim changed.*** This fix's
> own `+14` lines (the `redact_text` import plus the two sites' docstring paragraphs) shifted
> every `runner.py` line citation below it in this entry's own body, caught by
> `tests/test_integration_honesty_citations.py` going 52/54 on this branch (54/54 on unmodified
> `main`). Re-derived against this branch's `runner.py`: `:963`→`:964` (`_terminate_uncharged`),
> `:1048`→`:1055` (`_record_diagnostics`), `:1204-1209`→`:1216-1223` (`_detail()`),
> `:703-710`→`:704-711` (the non-retryable-TERMINATE dispatch block). `tests/test_integration_honesty_citations.py`
> back to 54/54 whole-file, no `-k`, after the correction.

## D91 — FIXED, LANDED (`adc029e`, round VI task 83). `tasks.pre_commit_sha` has no production writer anywhere in `src/fleet/`, so §12.15(i) and §12.45(i) cannot currently be exercised against a real production-populated value

**Found reviewing D89 Phase 2 Task B (2026-09-01, round U fix wave), while checking the hazard
mechanism ADR-0102's "Cost if wrong", ADR-0103's opening paragraph, and the D89 Task-A ledger
addendum above all describe.** Verified free before writing: a form-agnostic sweep of this file's
`D<n>` headings (`**D<n> — `, `### D<n> — `, `## D<n> — `) found no `D91` heading; the highest
allocated number is `D90`.

**The gap, as measured — two independent ways.** A source sweep of `src/fleet/` for every writer
of the `tasks` table's `pre_commit_sha` column: the sole `INSERT INTO tasks`
(`state/repository.py:1219`, `upsert_task`) omits the column entirely (its column list is
`task_id, run_id, repo_id, phase, kind, dest_path, max_attempts, ladder, created_at`); every
`UPDATE tasks` site in the tree was checked (`state/repository.py:1259` `set_task_target_paths`,
`:1284` `claim_task_by_id`, `:1311` `claim_next_task`, `cli.py:4262`/`:4269`/`:12412`/`:12447` the
two `_TransformSink`/`_persist_arbitration` DONE/PENDING resolutions) and none names
`pre_commit_sha`. A runtime probe against a real database corroborates: after a real
`_TransformClaimHook` claim and a real `_TransformSink` dispatch, `tasks.pre_commit_sha` reads
`NULL`. The one place a real per-unit anchor value is computed at all —
`workers/rewrite.py:190`'s `record_task_anchor(git, branch)` call inside `land_patches` — is
used only in-process, passed directly to a same-call `discard_task` on a caught `PatchApplyError`;
it is never written to SQL.

**Consequence.** `_reconcile_tasks_with_git` (`cli.py`) reads `t.pre_commit_sha` off
`_ARBITRATED_TASKS_SQL` to build `task_anchor`, and guards `if task_anchor is None:
_unresolved(...); continue` before any branch that could call `discard_task`, for both the REWRITE
per-unit path and the non-REWRITE single-`task_id` path. Since the column is always `NULL` in
production, that guard always fires: a real crashed `RUNNING` task row is reported `unresolved`
and left exactly as it was, forever — it can never reach the `DONE`/discard/partially-landed
verdicts this mechanism exists to compute. `docs/SPEC.md` §12.15(i) and §12.45(i) both depend on
`fleet resume` discarding a worktree onto `tasks.pre_commit_sha` for a crash-mid-mutation scenario
with nothing landed; neither criterion can currently be exercised against a value a real
production write path populates, because no such path exists. This also means three existing
documents overstated the pre-Task-B hazard's mechanism (they described `discard_task` firing and
destroying landed commits; the real pre-Task-B mechanism was `_unresolved` firing and the row
hanging `RUNNING` forever) — corrected in place, dated 2026-09-01, at ADR-0102's "Cost if wrong",
ADR-0103's opening paragraph (`docs/DECISIONS.md`), and the D89 Task-A ledger addendum above; this
entry is the underlying defect those corrections point back to.

**Correction (2026-09-01, round U fix wave) — the "Consequence" paragraph above overstates what
the guard blocks; re-measured against the merged post-Task-B `_reconcile_tasks_with_git`
(repointed fresh below, at the merge of tasks 65, 66, AND 67 together), not the pre-Task-B code
the paragraph above was describing.** *(Repointed repeatedly across round VI as this round's own
successive `cli.py` additions keep shifting it — this repointing follows, in order: round VI task
53's merge (added `_partition_test_srcs`/`_is_python_test_src`), task 56's merge (added the §12.34
Clause B PASS 2b), task 55's fix waves (threaded `min_consumers` through `break_cycles`'s call
site, filtered the `ContractNotShared` findings writer, and added the Leg A hoist-rejection
writers themselves), task 58 in two waves (§12.31 Leg E's own commit adding
`_forbidden_contract_rows` and `_persist_contract_hoist_override_findings`, then this same task's
controller-review fix wave growing `_sequence_graph_config`'s corrected docstring by two more
lines), task 65 in two waves of its own (its first landing, adding the `_graph_edges` crash-fix
predicate, `unhoist_contract` and its helpers; then this same task's OWN fix round, correcting the
demotion gate and growing `unhoist_contract`'s docstring and body further), task 66 in two waves
of its own (§12.31 Leg C2's own ~224-line insertion, then its own fix-round docstring growth on
`_hoist_watch_for_run`), and task 67 in two waves of its own (`d548b38`'s stub-creation-decision
landing and `6d8961e`'s fix round) — all merged in sequence onto `main`, each shifting this
citation in turn. Correction to this
paragraph's own prior self: the "at `<sha>`" suffix a previous repointing added here was NOT a
functional exemption — `test_no_unpinned_anchored_citation_
fails_to_resolve` checks a hardcoded `pins` tuple in the test module, not an "at sha" prose
convention (that convention marks a paragraph's CENSUS NUMBER claims as historical via
`_RECORD_MARKER`, a different check entirely) — the citation gate still required a live-resolving
line range regardless of the suffix, and did in fact catch it re-drifted a fourth time. Reverted
to a plain line citation; a real pin would need editing the test file itself, not done here. The
bare sub-citations inside this same paragraph — `:12347`/`:12389`/`:12335-12343`/`:12360-12376`
below — are still NOT re-verified, only the one anchored citation the automated gate checks.)* The
`if task_anchor is None:` guard is consulted at exactly two sites, `cli.py:12347` (REWRITE's
`elif not landed_units:` nothing-landed branch) and `:12389` (the non-REWRITE discard branch) —
both, and only, `discard_task` call sites, so the claim that a real crashed `RUNNING` row "can
never reach the `DONE`/discard/partially-landed verdicts" is false for two of those three verdicts.
Task B's `if units and not missing_units:` DONE branch (`:12335-12343`) and its `else:`
partially-landed branch (`:12360-12376`) never read `task_anchor` at all — neither is gated by
this guard, and both are production-reachable: `phases.pre_commit_sha` (a *different* column,
read into `anchor`/`phase_anchor` above, not `task_anchor`) DOES have a real production writer
(moved repeatedly by round VI tasks 65, 66, and 67's `cli.py` insertions, repointed fresh below:
`_TransformSink`'s
`UPDATE phases SET base_ref = ?, pre_commit_sha = ?, ...`), so
`find_task_commit` can genuinely locate a landed unit's commit and route into DONE or
partially-landed with `task_anchor` still `NULL` throughout. The heading's claim stays true as far
as it goes — the discard path (what §12.15(i)/§12.45(i) actually need guarded, since discarding
without a real anchor is the destructive case) is correctly blocked at both its sites — but the
generalization to "any of the DONE/discard/partially-landed verdicts" was wrong the moment Task B
landed the DONE and partially-landed branches, because it was never re-measured against them in
the same commit that made them reachable.

> ***Citation corrections made in place (2026-09-02, round GG task 2) — pointers, not records; no
> claim changed.*** Unrelated growth in `src/fleet/cli.py` above `_reconcile_tasks_with_git`
> between `cd0b76b` (when this correction's citations were written) and the current tree shifted
> every line citation in the paragraph above by exactly `+97` (the function's own body is
> byte-for-byte identical between the two trees — `diff` of the full definition returns nothing —
> so the shift is pure unrelated insertion, not a rewritten function), and the `_TransformSink`
> citation two paragraphs below by `+63`. Re-derived from content, each confirmed against the
> current tree by exact-line-content match, not carried forward from the gate: `:12183-12262` ->
> `:12280-12359` (`_reconcile_tasks_with_git`), `:12217` -> `:12314` and `:12259` -> `:12356` (the
> two `discard_task` guard sites), `:12205-12213` -> `:12302-12310` (the DONE branch), `:12230-12246`
> -> `:12327-12343` (the partially-landed branch), `:4474` -> `:4537` (`_TransformSink`'s `UPDATE
> phases` statement).

> ***Second citation correction (2026-09-02, round GG final review) — same class, one round
> later.*** Round GG task 1's own `cli.py` edits (D92, `_apply_stub_reconcile`) inserted `+33`
> lines above this function — again confirmed pure insertion, `_reconcile_tasks_with_git`'s body
> byte-for-byte identical between the previous correction's tree and current `HEAD`. The five
> citations the correction above just repointed had already drifted again by the time this round's
> own final review checked: `:12280-12359` -> `:12313-12392`, `:12314` -> `:12347` and `:12356` ->
> `:12389` (the two `discard_task` guard sites), `:12302-12310` -> `:12335-12343` (the DONE
> branch), `:12327-12343` -> `:12360-12376` (the partially-landed branch). The `_TransformSink`
> citation below is unaffected (`cli.py:4537`, unchanged — the D92 edits land well after it).
> Flagged by the same recurring pattern this file's own citation-gate work has now hit twice on
> this exact paragraph: any edit to `cli.py` above this function rots these five citations, and
> nothing currently watches for it between rounds.

**Not yet built:** a production write path for `tasks.pre_commit_sha` — most naturally, having
`_TransformClaimHook` (or an equivalent `pre_dispatch` hook for whichever kinds need it) persist
the git tip it reads at claim time, mirroring `_TransformSink`'s own `pre_dispatch`/`sink` pairing.
That design choice is not made here.

**Adjudication note (2026-09-02, round BB's §45 closure — appended, not a fix to this defect).**
Round BB's `docs/CRITERIA_PLAN.md` §45 entry read this entry's heading sentence — that
`tasks.pre_commit_sha` cannot currently be exercised against a real production-populated value —
and did not treat it as blocking §12.45's closure. Reasoning: §12.45(i)'s literal text requires
non-NULL values in the 5 named columns to be correct (resolvable, 40-hex), not every column to be
populated; a column that is always NULL in production makes the real-fixture coverage vacuous for
that one column without making the criterion's stated text false. This defect's status stays
`OPEN` — nothing above is changed by this note, which records only that another entry read and
adjudicated this sentence, so a reader arriving here directly sees the same context a reader
arriving via `docs/CRITERIA_PLAN.md` sees.

**Fixed, 2026-09-08 (round VI task 83, `adc029e`).** Fresh re-derivation (mandatory per this
task's brief, given this entry's own staleness disclosure above) confirmed the gap as this entry
describes it, with two shape changes since it was last written: a second `INSERT INTO tasks` site
now exists (`insert_revalidation_task_row`, for `REVALIDATE` tasks) and `claim_task_by_id` now has
a second caller (`cli._run_one_revalidation_task`, round VI task 79's REVALIDATE claiming loop,
ADR-0128) — both out of scope for this fix, since neither has an existing computed anchor value to
wire through (`record_task_anchor` is REWRITE/RELOCATE-specific, called only from
`workers/rewrite.py::land_patches`); §12.15(i)/§12.45(i) do not depend on REVALIDATE (§12.15(i)'s
scenario is specifically the `git apply`/`git commit` REWRITE mutation flow), so this scope
boundary does not block the criteria this defect names.

`_TransformClaimHook` (`cli.py:5673`) — the `pre_dispatch` hook that claims a TRANSFORM coarse
`tasks` row RUNNING, the exact "moves the task row to RUNNING" moment `vcs/commits.py`'s
`record_task_anchor` docstring already names as `tasks.pre_commit_sha`'s intended write site
(§3.2 step 6.5) — now reads the real `migrate/<repo>` tip via `record_task_anchor` (read from git
at claim time, not copied from `payload.phase_pre_commit_sha`, matching that function's own "the
two distinct anchors exist to prevent" contract: on a retried dispatch, earlier units of the same
coarse row may already have landed, so the live tip and the phase anchor can differ) and threads
it through `claim_task_by_id`'s new optional `pre_commit_sha` parameter
(`state/repository.py:1492`) into the same claim CAS. The parameter defaults to `None`, so the
pre-existing REVALIDATE caller above is unaffected — it still leaves the column `NULL`, exactly as
every claim did before this parameter existed. No `INSERT INTO tasks` change was needed: the
column already defaults `NULL` by omission (`state/schema.sql`), matching every other optional
column's (`status`/`claimed_by`/`fence_token`) existing handling — the D91-era instruction to "add
`pre_commit_sha` to the `INSERT INTO tasks` column list" would have been inconsistent with that
established convention.

**Proof (CLAUDE.md Rule 12, backup-file method, never `git stash`).** A standalone probe script
(`probe_d91.py`, this task's own scratch dir) sets up a real SQLite state DB and a real
`migrate/<repo>` git worktree, drives a real `_TransformClaimHook` claim exactly as
`_run_transform_wave` wires it, and reads `tasks.pre_commit_sha` back. Run against the pre-fix
code (confirmed via `git diff --no-index` to genuinely differ from the fix, not a no-op revert):
`has_work_dir parameter: False` / `tasks.pre_commit_sha: None` — a real claim leaves the column
NULL, reproducing this defect's own "Consequence" paragraph directly. Restored to the fix: the
same probe reads `tasks.pre_commit_sha` equal to the real `migrate/<repo>` branch tip a fresh
`git rev-parse` reports.
`tests/test_d89_phase2_claim_lifecycle.py::test_claim_hook_populates_target_paths_and_claims_running_before_dispatch`
carries the same assertion as a permanent regression test. The downstream consequence — the actual
hang this defect describes — is proven end to end, real hook then real reconciliation, in
`tests/test_d89_phase2_reconciliation.py::test_a_real_claim_hook_anchor_lets_the_discard_path_fire_instead_of_hanging_unresolved`:
a real `_TransformClaimHook` claim followed by a real `_reconcile_tasks_with_git` call with zero
units landed now reaches the `discard_task` branch (`report["unresolved"] == []`, one `discarded`
entry, fence bumped 1→2, worktree tip reset to the anchor) instead of the `task_anchor is None`
`_unresolved` branch this defect's own "Consequence" paragraph describes as the permanent hang.
Both proofs' old-fails/new-passes discriminators were confirmed against a genuine mutation
(`git diff --no-index` against the pre-fix backup showed real, non-trivial diffs on both touched
production files, never a zero-change no-op).

Full covering set (derived by grepping for callers of `_reconcile_tasks_with_git`,
`claim_task_by_id`, `_TransformClaimHook`, `upsert_task`, `record_task_anchor`, and
`_run_transform_wave` — CLAUDE.md §6's covering-set discipline, run whole, no `-k` filter): 331
passed, 2 skipped (`gh` unavailable), 0 failed. `ruff check src/ tests/` clean. `mypy` (no path
arguments) clean, 129 source files. `tests/test_integration_honesty_citations.py`: 70/70 — two
unrelated citations this change's own line-shift rotted (`record_attempt`,
`cli._committed_contracts`, both well outside this entry) were repointed in place above, confirmed
by exact-line-content match against the current tree, per this file's own established convention
for incidental drift. **Not re-verified in this fix**: this entry's OWN historical correction
paragraphs above carry bare (non-gate-checked) sub-citations already disclosed as stale by an
earlier round (`:12347`/`:12389`/`:12335-12343`/`:12360-12376`) — those predate this task by
several thousand lines of intervening `cli.py` growth and are out of this fix's scope; the
automated citation gate does not check them (disclosed in place, above), and neither the fix nor
this addendum changes that disclosure.

## D92 — FIXED, LANDED (`7cd6647`, round GG task 1). `PrState.HELD` is declared and documented but never written anywhere in `src/fleet/`

**Found by round Z task 2 (2026-09-01), while re-auditing §12.38/§12.46's `stub_reconcile` test
coverage against D80's landed fix — a disclosed-but-out-of-scope finding, not this task's own
job to fix.** Verified free before writing: a form-agnostic sweep of this file's `D<n>` headings
found no `D92` heading; the highest allocated number was `D91`.

**The gap, as measured, independently confirmed by task review.** `enums.py` documents
`PrState.HELD` as "entered ONLY by `stub_reconcile`" — the state a PR sits in while its stub is
still unresolved and readiness must stay withheld. A sweep of `src/` for every write/assignment
of `PrState.HELD` finds exactly two hits, neither a write: `forge.py:59` is a set-membership
check reading the value, and `orchestrator/stubs.py:195` is a comment explicitly stating the
state is deliberately excluded from that function's own write paths. `_apply_stub_reconcile`
(read in full by task review) touches only the `stubs` and `findings` tables — it never writes
`PrState` at all. Every other `'HELD'` string hit in the tree is the unrelated budget-ledger
`reservations.state` column, not `PrState`.

**Consequence.** The state machine `enums.py` documents has a declared, named state with zero
production writer — the same "declared but never wired" shape this ledger has recorded before
(D56, D57, D80 pre-fix, D89 pre-Phase-1). Whatever downstream logic is meant to read `PrState ==
HELD` (readiness gating, a PR-status render, an operator-facing report) can never observe it
under real traffic.

**Not yet built:** the write site. Most naturally this belongs inside `_apply_stub_reconcile` or
a sibling function in `orchestrator/stubs.py`, transitioning a PR's state to `HELD` when its
stub_reconcile pass finds the stub still unresolved (mirroring how `ACTIVE`/`SUPERSEDED` are
presumably set — not traced here). That design choice is not made here.

**Fixed, 2026-09-02 (round GG task 1, `7cd6647`), reviewed Approved (spec/brief compliance) with
one small controller fix (`d9cc964`) for two stale test comments the task review found.**
`_apply_stub_reconcile` now takes `reconcile()`'s `held_for_merge`, dedupes by
`provider_repo_id` (a provider can legitimately repeat across consumers or stub rows), loads each
held provider's existing `PullRequestDraft` via `_pr_records`, and re-persists it with
`state=PrState.HELD` in the same write transaction — an `_upsert_pr_record` helper extracted from
`_write_pr_record` so both share the SQL without a second `BEGIN IMMEDIATE`. Proven a genuine
discriminator (old-fails/new-passes): reverting only the `cli.py` portion leaves the pre-existing
`test_resume_stub_reconcile_holds_a_stub_whose_provider_still_has_an_open_pr` assertions passing
while a new read of the held provider's PR record fails `DRAFTED != HELD`; restored, it passes —
reproduced independently by task review in a fresh worktree. **Consequence of this bullet's own
paragraph above being wrong in one respect, corrected here rather than rewritten:** the claim
"`_apply_stub_reconcile` touches only the `stubs` and `findings` tables" no longer holds — the
held-provider path now also updates the provider's own `phases.pr_url`/`updated_at`
(`Phase.VERIFY`), a pre-existing side effect of the reused `_write_pr_record` SQL the task review
surfaced; it never touches the CONSUMER's `phases.status`, so §12.38's "consumers stay DEGRADED"
guarantee is unaffected. Does not flip §12.38 alone — D94 (no PR-promotion mechanism) remains
OPEN and still blocks it, confirmed genuinely NEW-MECHANISM sized with no smaller slice
(round GG's own research, re-checked directly against current `HEAD`).

**Correction, round II final review (2026-09-02) — this fix is FIXED, LANDED for what it actually
built (a real write, genuinely mutation-proven), but that write targets the wrong entity relative
to SPEC's own text; not reverting this status, since the write itself is real and does something,
but see D99/D100 for the scope this entry's own original investigation missed.** SPEC names the
CONSUMER's PR as `PrState.HELD`'s target at the T4/ABANDONED event (`docs/SPEC.md:1862`,
`:7520`), not the provider's PR at the `held_for_merge` carve-out this fix actually writes to —
and the provider-side write this fix landed has a further, self-defeating interaction with the
same carve-out on a subsequent resume (D100). Read D99 and D100 in full before treating D92 as
having closed the `PrState.HELD` gap its own original investigation described.

**Second correction, round III task 2 (2026-09-02, `448ceae`) — D99 and D100 are now FIXED,
LANDED, so the instruction in the paragraph above ("read D99/D100 before treating D92 as closed")
now resolves to: they ARE closed.** The mistargeted provider-side write this entry describes is
removed; a correctly-targeted consumer-side write replaces it. Read D99/D100's own entries for the
full account. D92's own status stays `FIXED, LANDED` for the historical record of what it built
and why that was wrong (both correction paragraphs above are kept, not deleted, per this file's
own convention).

## D93 — FIXED, LANDED (`b774c8f`, round EE task 2; component commit `f0936c2`). No exit-code path in `cli.py` reads `RepoStatus.DEGRADED`; SPEC §3.5.1 point 5 and `HumanInterventionError`'s own docstring both claim a DEGRADED-driven exit 7 that does not exist in code

**Found by round Z task 2 (2026-09-01), same investigation as D92 — disclosed, not fixed, out of
a test-writing task's scope.** Verified free before writing: highest allocated number was `D92`
(allocated in this same commit, immediately above).

**The gap, as measured, independently confirmed by task review.** All four exit-code
determination sites in `cli.py` (approximately lines 1863, 5060, 8985, 9201 — re-verify against
current `HEAD` before citing further, these were read once during this investigation and not
independently re-anchored since) compute `attention`/`exit_code` from
`RepoStatus.REQUIRES_HUMAN_INTERVENTION` only — none checks `RepoStatus.DEGRADED`.
`_resume_impl`'s own docstring confirms `--no-continue` always exits 0 regardless of DEGRADED/
unresolved-stub state ("the reconciliation is WRITTEN... at exit 0"). `HumanInterventionError`'s
own docstring claims coverage of both `REQUIRES_HUMAN_INTERVENTION` and `DEGRADED`, but the code
checks only the former — a real doc/code mismatch, not merely a missing feature.

**Consequence.** `docs/SPEC.md` §3.5.1 point 5's claim that a run with any DEGRADED repo exits 7
does not hold today — a run can complete with degraded repos present and still exit 0, silently.
This is a real, observable divergence between what the SPEC and a docstring both claim and what
the code does — worth prioritizing over D92 if only one gets picked up next, since it's an
operator-facing exit-code contract, not an internal state-machine gap.

**Not yet built:** whichever exit-code site (or a shared helper all four could route through) is
meant to fold `RepoStatus.DEGRADED` into the "human attention needed" determination alongside
`REQUIRES_HUMAN_INTERVENTION`. That design choice — and confirming which of the four sites is the
authoritative one vs. which are derived/duplicated — is not made here.

**Re-verified, round Z final review + fix wave (2026-09-01): the hedge above resolves.** `grep -n
"RepoStatus.REQUIRES_HUMAN_INTERVENTION" src/fleet/cli.py` at current `HEAD` confirms all four
cited lines are exact and unchanged since the original measurement — `1863`, `5060`, `8985`,
`9201` — each an `attention = sorted(... if status is RepoStatus.REQUIRES_HUMAN_INTERVENTION)`
exit-code determination site, each reading `RepoStatus.REQUIRES_HUMAN_INTERVENTION` only, no
`RepoStatus.DEGRADED` check at any of the four.

**Fixed, 2026-09-02 (round EE task 2, component commit `f0936c2`, merged `b774c8f`).** All four
sites (scan/transform/build/verify) now gate `exit_code` on a new shared
`_needs_human_attention(statuses) -> bool` helper (RHI OR DEGRADED), rather than widening
`attention` itself — `attention`'s own RHI-only meaning is read again downstream at each site for
operator-facing messages, and DEGRADED is resolvable per §3.5.1 (not a terminal failure), so
folding it into `attention` would have mislabeled it there. The design also sidestepped a real
naming collision: the build site already has an unrelated local `degraded` set
(`EcosystemAdapter`-unavailable repos, feeding an `"adapter_unavailable"` finding) that a naive
per-site `degraded = sorted(...)` block would have shadowed. Task review independently
mutation-proved 2 of the 4 sites (scan, build) and independently confirmed the verify site is
genuinely fixed AND tested — not by a dedicated verify-e2e file, but by
`tests/test_build_e2e.py::test_a_degraded_repo_at_phase_four_with_no_rhi_repo_exits_7`, which
drives the real `verify` CLI subcommand end to end. Regression-checked via the same before/after
separate-worktree comparison this project has now used three times for this exact claim shape
(rounds CC/DD's own D96 fixes, this round's D93 fix): identical failing-test sets in both trees,
confirming the 11 (implementer's run) / 7 (task review's own, slightly flaky, re-run)
`test_build_e2e.py` failures are pre-existing environment gaps, not caused by this fix.

## D94 — OPEN. No mechanism exists to promote an already-open PR to ready (rebase, force-push, body regeneration) — §12.38's "resolution" sub-clause has no code to test

**Found by round Z task 2 (2026-09-01), same investigation as D92/D93 — disclosed, not fixed, out
of a test-writing task's scope.** Verified free before writing: highest allocated number was
`D93` (allocated in this same commit, immediately above).

**The gap, as measured, independently confirmed by task review.** `_pr_candidates`/`_pr_impl` (or
wherever PR emission's candidate-selection lives — re-verify the exact function name/location
against current `HEAD`) builds its candidate set fresh each run and, for any repo that already
has an open PR record, appends it to an `already`-tracking list and `continue`s — skipping it
entirely rather than checking whether the underlying stub has since resolved and the PR should be
promoted (rebased onto the latest target branch, force-pushed if the draft content changed,
body regenerated to drop the stub-pending notice). §12.38's own criterion text describes this
resolution path as part of what "no ready-for-review while a stub is unresolved" requires — the
mechanism to ever LEAVE the held state for an already-open PR does not exist in code today.

**Consequence.** A stub that resolves after its PR was already opened (in the draft/held state)
has no code path that ever promotes that PR to ready — only a fresh, not-yet-opened PR can reach
the `RESOLVED`-triggers-ready-non-draft path round Z task 2's new positive-case test proves.
§12.38's resolution sub-clause is untestable as a consequence, not merely untested — there is no
resolution mechanism to write a test against.

**Not yet built:** the rebase/force-push/body-regeneration mechanism itself, and the trigger
logic deciding when an existing open PR should be re-examined for promotion. This is
NEW-MECHANISM sized, not a caller-wiring gap — genuinely new logic, not a one-shot. That design
choice is not made here.

**Partial progress, 2026-09-03 (round VI task 15, commit `741e9a2`, merge of
`agent/roundvi-task10`) — status stays OPEN, this is a disclosed sub-piece, not a fix.** A
dedicated research pass (`research-9`) designed and an implementer built the git-level primitive
this mechanism will need: `Git.rebase(onto)`, `Git.abort_rebase()`, and
`Git.push_force_with_lease(remote, branch, *, expected_sha)` in `src/fleet/vcs/git.py`. The
force-with-lease safety property (stale `expected_sha` refuses rather than clobbers the remote)
and a silent-branch-creation guard were both proven by discriminating mutation, independently
reproduced by two separate task-scoped reviews. **Still not built:** the trigger logic deciding
when an already-open PR should be re-examined for promotion, the body-regeneration step, and the
`Forge.mark_ready` wiring — this primitive has no caller anywhere in `src/` yet. §12.38's
resolution sub-clause remains untestable for the same reason stated above; this piece alone does
not move it.

**Update, round VI task 45 (2026-09-04, `a70c985`, merged `9ed6e74`, task-scoped review Approved
with nits) — the remaining three pieces all land: status now `PARTLY ADDRESSED`, not `OPEN`.**
`_pr_impl`'s already-open-PR branch now distinguishes a genuine promotion candidate (`PrState.
HELD`, written only by `_apply_stub_reconcile`'s end-of-run stub-abandonment path — confirmed the
single production writer, never conflated with the separate "dependency not merged" `held` dict
`_pr_impl` already tracks) from an ordinary already-open PR, and drives it through `_promote_one_pr`
(`Git.rebase` → `_regenerate_pr_body` → `Git.push_force_with_lease` → `Forge.mark_ready`), reusing
every existing primitive with no reimplementation. Body regeneration uses the full `render_body`
path (not a surgical delete) after task review confirmed a surgical delete would leave a stale
`Equivalence:` line — verbatim-preserves any `### Migration notes` section via
`_extract_migration_notes`. Two genuinely discriminating Rule-12 mutations (always-promotes /
never-promotes), independently reproduced by review. **Real, disclosed, not-fixed gap found in the
same task, allocated `D115` below**: this monorepo's ingest never creates a per-repo
`migrate/<repo>` branch, so a real `fleet pr` promotion attempt fails at its first check today —
proven correct via a real git bare-remote/clone (6 unit tests), reported through `failed`, never
silently skipped or crashed. **§12.38's resolution sub-clause is now testable end-to-end at the
mechanism level and blocked at the branch-topology level** — see `D115`.

## D115 — FIXED, LANDED (round VI task 50, `cbd3883`, merged `73d480f`, task-scoped review Approved zero findings). `fleet pr`'s PR-promotion mechanism (D94) has no live path to run against: this
monorepo's ingest never creates a per-repo `migrate/<repo>` branch, so promotion's first
precondition check always fails in production

**Found by round VI task 45 (2026-09-04), disclosed rather than forced into scope — the task's
own brief explicitly put `vcs/git.py` and `orchestrator/stubs.py` off-limits, and this gap sits
one layer up, in PR/ingest topology.** `_ingest_build_source`/`vcs.filter_repo.ingest`
(`src/fleet/vcs/filter_repo.py:429`) merges each repo's rewritten history directly onto
`integration`, never creating a `migrate/<repo>` branch inside the monorepo checkout. This is the
same family as the pre-existing, already-disclosed `_emit_prs --push` refusal ("not implemented...
no code path in `src/fleet/vcs/`", `cli.py:10096-10113`) — not something this task introduced.

**Consequence.** D94's promotion mechanics (`_promote_one_pr`) are correct and proven against a
real git bare-remote + clone (6 unit tests: clean rebase, conflict-abort, missing branch, refused
push, failed body edit), but a promotion attempted through the real `fleet pr` CLI path fails at
its very first check ("`migrate/<repo>` does not exist") because that branch was never created.
`tests/test_pr_e2e.py::test_pr_attempts_promotion_of_an_already_open_held_pr_once_its_stub_resolves`
asserts this real, honest outcome (Rule 11: reported via `failed`, never faked or swallowed) —
it proves the TRIGGER fires correctly, discriminated from a genuinely-still-open control case.

**Not yet built:** the decision of how/whether to create a `migrate/<repo>` branch in the
monorepo, at ingest time or at PR-creation time — a design question, not sized further here.
Closing D115 is what makes D94's mechanism reachable from a real `fleet pr` invocation; D94's own
mechanism does not need to change when D115 lands.

**Fixed, round VI task 50 (2026-09-05), per the decided design in `docs/DECISIONS.md`'s ADR-0118
(research-30).** `filter_repo.py::ingest()` now creates `migrate/<repo_id>` as a plain alias of
the merge commit it already produces (`await git.create_branch(f"migrate/{source.repo_id}",
<merge_sha or existing>, force=True)`), on both the fresh-merge and idempotent paths, inside the
existing `IntegrationMutex`. Purely additive — `IngestResult`'s shape unchanged, no other call
site modified. Three new tests prove: a fresh ingest's branch points at the real `merge_sha`; the
idempotent re-run path also gets it; a stale pre-existing branch pointing elsewhere is correctly
re-pointed (`force=True`). Task review independently reproduced both Rule-12 mutations and traced
the actual old-passes/new-fails discriminator on a pre-existing task-45 test
(`tests/test_pr_e2e.py::test_pr_attempts_promotion_of_an_already_open_held_pr_once_its_stub_resolves`),
confirming D115's fix genuinely moves `_promote_one_pr`'s failure point from its first
precondition check ("`migrate/<repo>` does not exist") to its second (no `origin` ref — still
correctly refused, since `--push` remains separately unimplemented), not merely re-labeling the
same failure. §12.38 is no longer blocked on D115.

## D95 — FIXED, LANDED (`00b9e68`, merge of `agent/roundz-task3`; component commit `f9df242`). `state/repository.py::complete_phase`'s RHI-escalation leg wrote raw SQL bypassing `transition()`, letting a stale-but-not-reclaimed fence corrupt the state model's own `ALLOWED_TRANSITIONS` invariant

**Found and fixed in the same task, round Z task 3 (2026-09-01), closing the specific
`state/repository.py::complete_phase` sub-clause D77's own "Scope" note names as a separate site
without pre-deciding a number for it.** Verified free before writing: highest allocated number
was `D94`. **Controller allocation, not self-assigned by the implementer** — the implementer's
own report argued this was "the same bypass shape D77 documents" and needed no new number; task
review independently traced the claim against `ALLOWED_TRANSITIONS` and D77's actual landed code,
found it a distinct call site with a distinct destination-status pair and a genuinely new
reachable race, and recommended a number; the controller sided with the reviewer.

**The gap, as measured — verified two ways, independently by the implementer and re-verified by
task review, not accepted on either party's word alone.** `ALLOWED_TRANSITIONS[RepoStatus.BLOCKED]
= frozenset({PENDING, SKIPPED})` (`src/fleet/models/enums.py:41`) — `BLOCKED →
REQUIRES_HUMAN_INTERVENTION` is not a legal transition. D77's own landed fix
(`orchestrator/scheduler.py::append_blocked_by`) legally moves a `RUNNING` phase to `BLOCKED`
(legal per `ALLOWED_TRANSITIONS[RUNNING]`) via an `UPDATE` touching only `blocked_by`/`status`/
`updated_at` — confirmed by grep that it never touches `lease_fence`. The pre-fix
`complete_phase` raw SQL guarded its RHI-escalation write with `WHERE ... AND lease_fence = ?`
only — no check of current status — so a worker holding a stale-but-not-reclaimed fence on a row
that had since flipped to `BLOCKED` (via `append_blocked_by`, racing concurrently) would land its
`REQUIRES_HUMAN_INTERVENTION` write unconditionally, silently corrupting the state model's own
`ALLOWED_TRANSITIONS` invariant. This is `§12.46`'s "the reaper's RHI leg writes raw SQL bypassing
`transition()` entirely" sub-clause — the "D77 bypass shape, recurring," but at a genuinely
distinct site.

**The fix.** `complete_phase` now reads current status inside its fenced write transaction and
validates the escalation target through the real `transition()` gate. On an illegal transition it
raises `PhaseTransitionRefusedError(LeaseStolenError)` — a subclass, so all three existing
`except LeaseStolenError:` sites in `orchestrator/runner.py` catch it transparently with no caller
changes; task review confirmed only one of the three (`_complete`) can actually receive it (the
other two call `renew_phase_lease`, not `complete_phase`, and are unaffected), and that the raised
message ("the fence was NOT bumped and no lease was reclaimed") is genuinely distinguishable from
`_stolen()`'s true-stale-fence message, not a misleading reuse of it. Rule 11 (fail loud): the
illegal-transition case raises a typed, caught exception in the reaper completion path rather than
crashing or silently corrupting state.

**Verification.** `tests/test_repository.py::test_complete_phase_refuses_an_illegal_transition_instead_of_writing_it`
proves the fix directly. Mutation-proved, independently reproduced by task review in a fresh
worktree: reverting to the raw-SQL bypass reddens exactly this one test (1 failed / 117 passed
across `test_repository.py`+`test_runner.py`+`test_scheduler.py`), restoring the fix greens all
118.

**Disclosed residual, out of this fix's scope (not a regression — unchanged from the pre-fix
behavior).** The fix only refuses when the *target* status is illegal from the raced current
status. A worker racing `append_blocked_by`'s `RUNNING→BLOCKED` but completing with a legal
`BLOCKED→PENDING` target (e.g. before hitting `max_attempts`) still silently overwrites the
propagated block — `ALLOWED_TRANSITIONS` permits it, so `transition()` correctly allows it; no
current-status check existed in the old raw SQL for this case either, so this is not a new gap,
just an edge this fix does not close. Flagged by task review for whoever next touches this area,
not itself a defect worth its own D-number.

**Correction, round Z final review + fix wave (2026-09-01): the docstring's uniqueness claim was
false.** `complete_phase`'s docstring (as landed above) said D77's `append_blocked_by` race was
"the one theoretical window where this leg fires." A whole-branch review found a second, more
concretely reachable route to the same `PhaseTransitionRefusedError` outcome:
`cli.py::_quarantine_impl` writes `status = 'SKIPPED'` directly via raw SQL with no status guard
at all and no `lease_fence` bump, and `ALLOWED_TRANSITIONS[RepoStatus.SKIPPED]` is the empty set
(`src/fleet/models/enums.py`) — so `fleet quarantine`, an explicit operator command against a live
fleet, not a hypothetical race, causes a worker completing under a still-valid fence on a
just-quarantined phase to be refused for ANY completion target, not only the RHI-escalation case
D77's route is framed around. This does not change the fix's logic or its landed status — the
refuse-loudly behavior above is still correct and still real — it corrects only what the
docstring claimed about how many routes reach it. The docstring itself has been corrected in the
same fix wave; other `cli.py` writers noted as sharing the same "no fence bump" shape
(approximately lines 2094, 2123, 4533) were not individually confirmed as concretely reachable as
the quarantine path and are not claimed here.

## D96 — FIXED, LANDED (phase-entry half `71c3cd9`, round CC task 3; Phase-2 per-repo-worker half `ebb83d3`, round DD task 2). `fleet transform` has zero disk-headroom enforcement anywhere in its path — the phase most likely to consume disk at scale is the one phase left unguarded

**Found by round AA task 2 (2026-09-01), during a TEST-ONLY task closing a different §12.22
sub-clause — disclosed, not fixed, out of that task's scope.** Verified free before writing:
highest allocated number was `D95`. Independently re-confirmed by the controller (not accepted on
task review's word alone), against current `HEAD` — separately from task review's own trace,
which itself went further than the implementer's original grep-only flag.

**The gap, as measured, independently confirmed twice.** `_require_disk_headroom` (`cli.py:13782`)
was called at exactly 4 sites as of when this defect was found (a 5th, `transform` itself, exists
now — see the fix note below): `scan` (`:1016`), `build` (`:2656`), `verify` (`:2697`),
`_continue_impl`/`fleet resume` (`:9468`). `transform`'s command body (`cli.py:3465-3538` as of
when this defect was found, now `3465-3542` post-fix) calls
`_phase_preflight(ctx)` and then goes straight to `_transform_impl` — no `_require_disk_headroom`
call anywhere in the function, confirmed by isolating the function body between `def transform(`
and the next `@app.command` and grepping it directly. This breaks a pattern applied consistently
everywhere else: `build` and `verify` each call `_phase_preflight` immediately followed by
`_require_disk_headroom` under the identical `§11.3/§12.22` comment — `transform` alone omits the
follow-up call, reading as a copy-paste gap rather than a deliberate exemption.

**`sequence` is NOT part of this gap** — confirmed genuinely exempt: it is pure computation, no
`project_once` call anywhere in `_sequence_impl`, no clone, no container. Its own docstring's
"never touches the network" claim extends to disk at scale. Do not fold it into this defect's
scope.

**The per-repo fallback this project's own code claims should cover it does not exist for Phase
2.** `_require_disk_headroom`'s own docstring (`cli.py:13713-13724`) states explicitly: "Per-repo
enforcement is NOT here and must not be: the floor is re-checked before every clone and every
container start by the workers themselves (§11.3)." Phase 1/3/4's workers back this claim up —
`clone.py:113-116` and `buildverify.py:602-605` each carry a `min_free_bytes`-checked-before-
operation field, wired via `min_free_bytes=` payload construction at `cli.py:1234`/`5827`/`6309`
(`ScanPipelineWorker`/`BuildPipelineWorker`/`VerifyPipelineWorker`). **Phase 2's workers
(`workers/relocate.py`, `workers/rewrite.py`, `workers/buildgen.py`) carry no such wiring at
all** — confirmed by grep for `min_free_bytes`/`require_free`/`disk` across all three, returning
nothing but one unrelated `scoped_tempdir` import in `buildgen.py`. Transform also spawns no
`ContainerSandbox` (only `rdepverify.py`/`buildverify.py` — Phase 3/4 — do), so there is no
per-container check standing in for the missing phase-level one either.

**Net: `fleet transform` has zero disk-headroom enforcement anywhere in its path** — not at
command entry, not per-repo, not per-container. This directly contradicts
`_require_disk_headroom`'s own docstring and `docs/SPEC.md:7527` (§9 audit row 42: "re-checked
before EVERY clone and EVERY container start — not once at startup"). Transform is plausibly the
single most disk-hungry phase in the fleet (worktree checkouts, repo-scale rewrites, commits
across every repo in a wave), and it is the one phase this project's own design intent says should
be checked and isn't.

**Not yet built:** the missing `_require_disk_headroom(settings)` call in `transform`'s command
body (small, mechanical, identical to `build`/`verify`'s own pattern) closes the phase-entry half.
The Phase 2 per-repo/per-worker wiring `_require_disk_headroom`'s docstring claims exists is a
separate, larger piece — sizing it (a one-shot call-site addition, or something needing new
plumbing through `relocate.py`/`rewrite.py`/`buildgen.py`'s worker payloads) is not done here.

**Correction, 2026-09-01 (round AA final review): the grep residue for Phase 2's disk-check sweep
was misattributed above — the material claim reproduces exactly, the supporting detail did
not.** Re-running this entry's own stated predicate (`min_free_bytes|require_free|disk`) over the
three Phase 2 workers returns exactly one hit, `buildgen.py:625` — a docstring reading "Put every
file the generated text NAMES on disk," unrelated to a disk-floor check. `scoped_tempdir` is
imported at `relocate.py:36` and `rewrite.py:61` (two sites, not one), and is NOT imported by
`buildgen.py` at all — the reverse of what this entry originally said. Neither correction changes
the finding: no Phase 2 worker carries any disk-floor check, confirmed independently by the
round's own final review.

**Phase-entry half FIXED, LANDED (2026-09-02, round CC task 3, merge `71c3cd9`, component commit
`ce4cbef`).** `transform`'s command body now calls `_require_disk_headroom(settings)`
immediately after `_phase_preflight(ctx)`, identical in placement and comment wording to
`build`/`verify`'s own sites — closing exactly the copy-paste gap this entry's "Net" paragraph
named. New regression test: `tests/test_cli.py::
test_transform_refuses_to_start_below_the_disk_floor_with_exit_9`. Rule-12 mutation-proven (revert
the fix, confirm exit 0 instead of 9; restore, confirm exit 9), independently reproduced by task
review in a separate worktree with byte-identical results. Task review additionally ran
`tests/test_build_e2e.py` in matched before/after worktrees (base `96243a4` vs. fixed `ce4cbef`)
and found the failure SET byte-identical between them (same test names, same counts) — direct
symmetric evidence this fix introduces zero regressions in that suite; the failures themselves
are unrelated pre-existing environment gaps (`uv` not installed on this host; a gitignored
`tools/go/` toolchain directory not carrying into a fresh `git worktree add`), independently
confirmed by reading the actual failure tracebacks rather than trusting the implementer's
categorization.

**Still OPEN: the Phase 2 per-repo/per-worker wiring half.** `_require_disk_headroom`'s own
docstring claim that per-repo enforcement happens "before every clone and every container start
by the workers themselves" remains false for Phase 2 — `relocate.py`/`rewrite.py`/`buildgen.py`
still carry no such wiring, unchanged by this fix (which only closes the phase-ENTRY gate, not
the per-repo one). This is explicitly out of scope for the phase-entry fix and is the entry's one
remaining open surface.

**Closed, 2026-09-02 (round DD task 2, component commit `6711229`, merged `ebb83d3`) — the
paragraph immediately above is superseded, not deleted, per this file's annotate-never-rewrite
convention.** `RelocateInput`/`RewriteInput`/`BuildgenInput` each now carry a `min_free_bytes`
field matching `CloneInput`'s own shape (`default=0, ge=0`), and each worker's `run()` calls
`require_free_space(...)` immediately before its real disk-consuming write — `relocate.py`'s
before `scoped_tempdir`/`land_patches`, `rewrite.py`'s in the identical position, `buildgen.py`'s
immediately before writing the generated `BUILD.bazel`. `cli.py`'s Phase-2 payload construction
wires `min_free_bytes=` from `preflight.min_free_bytes` the same way `ScanPipelineWorker`'s
already did. Both halves of this defect are now closed — see the heading's status field, updated
in the same commit as this note. **One disclosed residual, not closed by this fix and not itself
a new defect**: `buildgen.py`'s `_ingest` step (a real `git fetch`+`merge`) runs before its new
disk check, unprotected — currently a dead path in production (`cli.py`'s only `BuildgenInput`
construction site always passes `ingest=None`), noted in a one-line code comment rather than left
silent. **A second disclosed residual, from this round's own final review, not yet closed**:
three of the four new per-worker checks (`RewriteWorker`'s, `BuildgenWorker`'s, and the
`_buildgen_input` wiring) ship with zero dedicated test coverage — only `RelocateWorker`'s got a
regression test this round. Deleting any of the other three today would leave the suite green;
flagged for a future round, not fixed here.

## D97 — FIXED, LANDED (`a9a1d48`, round II task 2). `CONTRACT_IMPL`/`CONTRACT_CONSUME` edges are computed in memory but never persisted — no production call site writes them, at any point

**Found by round HH task 2 (2026-09-02), while attempting SPEC §12.8's residual fixture-fleet
proof for these two `EdgeKind` members — a disclosed BLOCKED finding, not this task's own job to
fix (TEST-ONLY scope). Verified free before writing**: form-agnostic sweep of this file's `D<n>`
headings found no `D97`; highest allocated number was `D96`. Independently re-verified by task
review, not accepted on the implementer's word alone — including a fresh, independently-written
standalone reproduction script (not a reuse of the implementer's own test code) driving a real
`fleet scan` → `fleet sequence` through a genuine hoist and querying the persisted `edges` table
directly by SQL.

**The gap, as measured, twice.** `repository.insert_edges` (repointed fresh below)
has exactly ONE production call site anywhere in `src/`: inside
`_persist_scan_edges` (repointed fresh below), itself called only from `_scan_impl` (the scan
path). `_persist_scan_edges` builds its `InferenceInput` with no `contracts=` argument, so
`infer_contract_edges` never fires there — there is nothing to persist at scan time because no
contract has been hoisted yet. Separately, `_sequence_impl` (moved repeatedly by round VI tasks
65, 66, and 67's `cli.py` insertions, repointed fresh below) does reach a real hoist via
`break_cycles` → `graph/cycles.py::_materialize`, which genuinely
computes `CONTRACT_IMPL`/`CONTRACT_CONSUME` `DependencyEdge` objects in memory for wave
assignment — but the whole of `_sequence_impl`'s body contains zero calls to `insert_edges` or any
other write path (grepped in full by task review). A real run through `cycle_fleet`'s fixture
(contract genuinely reaches `contracts.status = 'HOISTED'`, confirmed) leaves the `edges` table
holding only the pre-existing `DECLARED_DEP`/`INTERNAL_IMPORT` rows from scan time — zero
contract-kind rows, always, independent of fixture shape.

**Not a duplicate of D23 — distinct and broader, cross-referenced here.** D23 (search `**D23 —`
above) diagnoses a 15-column `insert_edges` schema missing a `retargeted_from_repo_id` column,
implying retargeted-edge rows ARE written today, just with the wrong shape. That premise does not
hold for the mechanism this entry describes: since `insert_edges` has exactly one call site
(scan-time only, per above), no row `_materialize` produces — retargeted or freshly-inferred,
contract-kind or otherwise — is ever written, missing column or not. D23 also never mentions
`CONTRACT_IMPL` (a source-side edge, not a retarget) at all. **Fixing D23's missing column alone
would not close this gap** — `_sequence_impl` would still need a real write path added. Read both
entries together; this one is not covered by D23's proposed remedy.

**Consequence.** SPEC §12 item 8's "every known cross-repo edge is discovered" clause
(`docs/CRITERIA_PLAN.md` §8, ADR-0109) cannot close for these two `EdgeKind` members via a
fixture-proof alone, unlike the five already-proven kinds (`DECLARED_DEP`, `INTERNAL_IMPORT`,
`PUBLISHED_ARTIFACT`, `SHARED_RESOURCE`, `DYNAMIC_REF`) — there is no round-trip production code
path to prove against. A landed `xfail(strict=True)` test
(`tests/test_sequence_e2e.py::test_the_hoisted_contract_produces_real_contract_impl_and_consume_edges_in_the_table`)
pins the target state and will hard-fail the suite once persistence is wired, forcing the marker's
removal rather than silently staying green forever.

**Not yet built:** a real write path — most naturally, `_sequence_impl` (or wherever
`_materialize`'s output is available post-hoist) calling `insert_edges` with the contract-kind
edges it already computes in memory, mirroring how `_persist_scan_edges` does it for scan-time
edges. That design choice is not made here.

**Fixed, 2026-09-02 (round II task 2, `a9a1d48`), reviewed Approved.** A new
`_persist_contract_edges` helper, called inside `_sequence_impl`'s existing `StateWriter` block
(already used for 4 other writes), filters `report.edges` to `CONTRACT_IMPL`/`CONTRACT_CONSUME`
and calls `insert_edges`, mirroring `_persist_scan_edges`'s exact `EdgeRow` construction — task
review confirmed field-for-field parity. The landed `xfail(strict=True)` test now passes for real
(genuine old-fails/new-passes: commenting out the new call reproduces the exact targeted `KeyError`
the xfail used to pin, restored with zero drift). Task review additionally ran a fresh standalone
script (not reusing the implementer's test) driving a real scan→sequence and querying `edges`
directly — found **4** contract-kind rows, not 3: `acme-billing` gets two `CONTRACT_CONSUME` rows
(0.85 from fresh manifest-based inference, 0.8 from a pre-existing, unchanged `_materialize`
mechanism that *retargets* a real source-scanned `INTERNAL_IMPORT` edge onto the contract,
preserving its original confidence/evidence_line). This is D23's own scope
(`retargeted_from_repo_id` silently dropped — `EdgeRow` has no such column) extending to this new
call site, not a defect in this fix — genuinely out of this task's scope, cross-referenced here for
whoever picks up D23. Also noted, not fixed here: the existing fixture test's dict-comprehension
assertion silently collapses the two `acme-billing` rows, so despite its docstring's wording it
does not actually prove "exactly one `CONTRACT_CONSUME` row per consumer" — pre-existing test
structure, untouched by this diff, flagged for a future pass.

## D98 — FIXED, LANDED (`600360d`, round III task 1). `fleet resume` never exits 7 in the pure stub-abandon end-of-run case — D93's exit-7 wiring lives only in the four phase-command sites, never in `resume()`'s own exit path

**Found by round II task 1 (2026-09-02), while re-auditing SPEC §12 item 38's sub-clauses against
D92/D93's now-landed fixes — a disclosed finding, not this TEST-ONLY task's own job to fix.**
Verified free before writing: form-agnostic sweep of this file's `D<n>` headings found no `D98`;
highest allocated number was `D97`. Independently reproduced by task review, not accepted on the
implementer's word alone — including a fresh, independently-written throwaway probe test (reverted
after use, confirmed clean via `git status`) rather than a reuse of the implementer's own probe.

**The gap, as measured, twice.** A resume cycle where the ONLY event is `stub_reconcile` abandoning
a stub (nothing else servable that cycle) exits **0**, not 7 — confirmed via a real
`fleet --json resume` invocation over a minimal fixture (a `DEGRADED` consumer at the frontier
phase plus one `ACTIVE` stub row, nothing else). The JSON payload shows
`"continuation": {"plan": [], "driven": [], "halted": null, "halted_phase": null}`.
`_continue_impl` (moved repeatedly by round VI tasks 65, 66, and 67's `cli.py`
insertions, repointed fresh below) returns early at `if not servable: return result` (repointed
fresh below) with `halted: None`, and `_raise_for_continuation` (repointed fresh below) is a no-op
when
`halted is None` — `resume`'s own exit path never calls `_needs_human_attention` or reads the run's
overall phase statuses at all when nothing gets re-driven. D93's fix (four call sites at
`cli.py:1889`, `:5183`, `:9113`, `:9333`) lives exclusively inside `_scan_impl`/`_transform_impl`/
`_build_impl`/`_verify_impl` — genuinely distinct code from `_resume_impl`/`_continue_impl`/
`resume()`'s own body, confirmed by task review reading both paths directly. `phase_floor`'s
hard-stop logic (`orchestrator/reentry.py:48,54,116-117`) confirms `DEGRADED` at the frontier
phase yields floor `None`, so nothing gets planned and no phase impl — and thus no
`_needs_human_attention` call — ever runs on this path.

**Consequence.** A `fleet resume` invocation whose only outcome this cycle is a stub abandonment —
"a human is needed" per SPEC §3.5.1/§13's own intent — silently exits 0 instead of 7. An operator
or CI pipeline gating on exit code sees success where the fleet's own state says otherwise.

**Not yet built:** a real read of the run's overall phase statuses (or equivalent
`_needs_human_attention`-style check) inside `resume()`'s/`_continue_impl`'s own exit-code
determination, for the case where nothing was re-driven this cycle. That design choice is not made
here.

**Fixed, 2026-09-02 (round III task 1, `600360d`), reviewed Approved.** A new async helper
`_resume_needs_human_attention(path, run_id)` — `connect_ro` → `build_state` → `_needs_human_
attention` over the repos' statuses, closed in a `finally` — is called from `resume()`'s own
command body via the module's existing `_run(...)` sync/async bridge, immediately after
`_raise_for_continuation(continuation)` inside the `if continuation is not None:` block. Design
deviates from this round's own research sketch only in mechanics (`resume()` is a sync `typer`
command, so the sketch's inline `await` couldn't compile) — task review independently confirmed
the helper is semantically identical to the design and reaches the exact call-site location
specified. Confirmed `_stub_reconcile_impl`'s `degraded_consumers` return value is genuinely
insufficient (only covers this-pass abandonments, missing an already-DEGRADED repo from an earlier
resume) by direct read of `orchestrator/stubs.py::reconcile()`. Proven old-fails/new-passes,
reproduced independently by task review in a fresh worktree; `--no-continue`/`--dry-run`
confirmed still exempt, unchanged.

**Disclosed, not confirmed, open question for a future pass (task review's own residual finding,
explicitly flagged as ambiguous, not asserted as a second defect here) — as it stood before round
III, kept as history.** SPEC.md:7464's literal text — "`C` stays `DEGRADED` with `PrState.HELD`"
for the end-of-run ABANDONED case — may describe a second, currently-unimplemented behavior
(`_apply_stub_reconcile`'s ABANDONED decisions loop, `cli.py:12043-12096`, never writes any
`PrState`; only the separate `held_for_merge` branch does, and only to the provider's own record,
not the consumer `C`'s). Read SPEC's own compound sentence directly before treating this as a
confirmed gap — it was not independently re-derived to the same confidence as the exit-code
finding above.

**Forward reference (2026-09-02, round III final review) — the question above is now answered,
by D99/D100, not by this task.** The two claims in the paragraph above are now false at HEAD: the
decisions loop DOES write `PrState` (D99's fix, `448ceae`), and the `held_for_merge` branch no
longer exists (D100's fix, same commit) — both landed by round III task 2, a sibling task to this
entry's own D98 fix, not this task itself. Read D99 and D100's own entries for the confirmed,
fixed account; this paragraph is kept as the record of what was genuinely unconfirmed at the time
D98 was found and fixed.

## D99 — FIXED, LANDED (`448ceae`, round III task 2). `PrState.HELD` is written to the wrong PR: SPEC names the consumer's, D92's fix writes the provider's

**Found by round II final review (2026-09-02), re-verifying task review's own disclosed residual
finding for D98 — this one confirmed to the reviewer's own higher confidence, not just flagged.**
Verified free before writing: form-agnostic sweep of this file's `D<n>` headings found no `D99`;
highest allocated number was `D98`.

**The gap, as measured against SPEC directly.** Three independent citations name the CONSUMER's PR
as `PrState.HELD`'s real target, at the point a stub is finally `ABANDONED` (end-of-run, T4):
`docs/SPEC.md:1862` (§3.5.1 point 3): *"Their PRs are held"* — "their" = the consumers named in
point 2 immediately above. `docs/SPEC.md:7520` (§13 row 35): *"consumers **stay** `DEGRADED`...;
their PRs **go** `PrState.HELD`"* — a transition verb, tied to the same abandon event.
`src/fleet/models/enums.py:409-411`'s own `PrState.HELD` docstring: *"still a draft on the forge,
and the harness has finished without resolving **its** stubs... Entered ONLY from `DRAFTED`, by
`stub_reconcile`"* — "its stubs" names the PR's own repo's unresolved dependencies, i.e. the
consumer, not a provider it depends on.

D92's landed fix (`7cd6647`, round GG task 1) wrote `PrState.HELD` to a different entity
entirely: `_apply_stub_reconcile`'s `held_providers` loop — at `cli.py:12097-12106` as this
finding was written; that loop no longer exists, removed by this same D99's own fix (`448ceae`,
round III task 2) — marked the **provider's** own PR record `HELD` when `reconcile()`'s
`held_for_merge` carve-out (§13 row 45 —
a DIFFERENT SPEC location, about NOT abandoning a stub row prematurely while its provider's PR is
still under human review) fires. No code path anywhere in `src/` marks a CONSUMER's PR `HELD` at
the T4/ABANDONED event SPEC actually describes — the sole `PrState.HELD` write site is this one,
targeting the provider.

**Root cause, as best understood:** D92's original finding (round Z, this file, search
`## D92 —`) quoted `enums.py`'s docstring ("the state a PR sits in while its stub is still
unresolved") without checking which SPEC location defines the target entity, and the round GG
implementation inferred the target from `held_for_merge`'s own carve-out framing (which is about a
STUB row's state, not a PR's) rather than from §3.5.1 point 3 / §13 row 35's own explicit "their
PRs"/"consumers... their PRs" language.

**Consequence.** No consumer's PR is ever marked `HELD` when its stub is genuinely abandoned at
end-of-run — the exact scenario SPEC's sentence describes ("the run ends with the provider still
abandoned... their PRs go `PrState.HELD` and `fleet pr --ready` refuses them, exit 2") is
unimplemented. `fleet pr --ready` may refuse a stub-limited consumer's PR for other reasons (a
`stubbed_deps` check, not traced here), but not via the `PrState.HELD` mechanism SPEC names
specifically. See D100 for a second, compounding defect in the provider-side write this gap left
in place.

**Not yet built:** a write, inside the T4/ABANDONED branch of `_apply_stub_reconcile`'s decisions
loop (`cli.py`, the branch handling `decision.transition` for an abandon, not the
`held_providers` loop), marking the CONSUMER's own PR record `HELD` — mirroring the shape D92's
fix already built for the provider case, applied to the correct entity. Whether the provider-side
write should be removed, kept for a different purpose, or renamed is not decided here — see D100.

**Fixed, 2026-09-02 (round III task 2, `448ceae`), reviewed Approved.** The provider-side
`held_providers` write is deleted entirely (closes D100 too — see that entry). A consumer-side
write added to `_apply_stub_reconcile`: unique `consumer_repo_id`s collected while iterating
`decisions` (accumulation inside the loop, `cli.py:12133-12134`), the write itself after the loop
(`if consumer_ids:`, `cli.py:12136-12143`) — `pr_records` fetched once, each present consumer's
draft upserted with
`state=PrState.HELD`, mirroring D92's own `model_copy`/`_upsert_pr_record` shape verbatim, just
keyed correctly. Task review independently re-read SPEC and found further corroboration beyond
this entry's own citations — §12.38 and §12.39(ii) (`docs/SPEC.md`) and the `stubs:` config block
comment both also name the consumer, and confirmed via `git grep` that no sentence anywhere in
`docs/SPEC.md` describes a provider's PR being marked `HELD`. Proven old-fails/new-passes,
reproduced independently by task review in a fresh worktree, zero drift confirmed both ways.

## D100 — FIXED, LANDED (`448ceae`, round III task 2). The provider-side `PrState.HELD` write (D92's landed fix) is self-defeating: it can cause the exact premature abandonment SPEC's carve-out exists to prevent

**Found by round II final review (2026-09-02), via a runtime probe against the shipped
`reconcile()` function — reproduced directly, not inferred.** Verified free before writing:
highest allocated number was `D99` (immediately above).

**The gap, as measured.** `pr_open` (`src/fleet/orchestrator/stubs.py:202`), the property §13 row
45's carve-out reads to decide whether a stub row should stay `ACTIVE` instead of being abandoned,
returns `True` only for `PrState.DRAFTED`/`PrState.OPEN` — `PrState.HELD` is deliberately excluded
(`stubs.py:191-199`'s own comment explains why: a `HELD` PR is "the fleet's own verdict", not a
pending human action). But D92's fix (D99, above) writes `PrState.HELD` to a provider's PR record
the FIRST time `held_for_merge` fires for it — and that write happens inside the very
`_apply_stub_reconcile` pass that computed `held_for_merge` from `pr_open` being `True` a moment
earlier. On the NEXT `stub_reconcile` pass (a subsequent `fleet resume`), that provider's PR now
reads `HELD`, `pr_open` returns `False`, and the row that was legitimately protected by the
carve-out is abandoned — even though the provider's real forge PR may still be genuinely open and
under human review, since resume's forge re-poll is opt-in (`--repoll-prs`,
`cli.py:11460-11476`, the `if not repoll_prs: repoll = "not-requested"` opt-in gate) and the
locally-cached `PrState` is what `pr_open` actually reads.

Reproduced directly against the shipped `reconcile()` (one `ACTIVE` stub, provider PR 10s old,
`pr.merge_wait_timeout_s` 3600s — well inside the carve-out's own window):
```
resume #1: pr_state=DRAFTED  -> held=['consumer'], decisions=[]            (correctly held)
resume #2: pr_state=OPEN     -> held=['consumer'], decisions=[]            (correctly held)
resume #3: pr_state=HELD     -> held=[],            decisions=[('T4', 'END_OF_RUN')]  (abandoned)
```
Resume #3 abandons a stub row whose provider PR is still open and inside the timeout window —
exactly what `docs/SPEC.md`'s carve-out text (§13 row 45, cited in D92's own investigation) says
must NOT happen: *"with `P`'s PR still open and unmerged inside `pr.merge_wait_timeout_s`,
reconciliation does not abandon the row."* `tests/test_cli.py::test_resume_stub_reconcile_holds_a_stub_whose_provider_still_has_an_open_pr`
(D92's own test) invokes `resume` exactly once, so this second-resume interaction is untested.

**Consequence.** A stub genuinely being held for a human's in-progress review can be abandoned by
the SECOND `fleet resume` a run happens to make, purely because the first resume's own
carve-out-preserving write flips a state flag (`pr_open`) that the SAME mechanism reads on its
next pass — a self-defeating interaction, not a race with external state.

**Relationship to D99.** These are two sides of one root cause: D92 wrote `PrState.HELD` to the
wrong entity (D99) using a mechanism (`held_for_merge`) whose own state feeds back into the
carve-out that produced it (D100). Fixing D99 (writing the CONSUMER's PR instead) may resolve D100
as a side effect if the provider-side write is removed entirely — or D100 may need its own fix
(e.g., `pr_open` reading the forge's live state rather than the cached `PrState`, or the
`held_for_merge` write simply not happening at all) if the provider-side write turns out to serve
a real purpose SPEC names elsewhere. Not designed here — a future round should read both entries
together before dispatching either.

**Fixed, 2026-09-02 (round III task 2, `448ceae`), reviewed Approved — resolved exactly as D99's
own entry predicted, by removing the provider-side write entirely.** No SPEC text anywhere
names a legitimate purpose for it — task review re-read §13 row 45 and §3.5.1 in full and
confirmed no sentence describes a provider's PR being marked `HELD`. Discriminator: the exact
second-resume repro this entry documents (a provider PR still `DRAFTED`/`OPEN` inside the timeout
window) was extended into a genuine test — after the fix, a second `resume` call leaves the stub
row `ACTIVE`, not abandoned, and the provider's PR record stays unchanged (`DRAFTED`) rather than
ever reading `HELD`. Task review reproduced this old-fails/new-passes independently, including
running the exact repro shape against unfixed code in a scratch copy to directly observe the raw
abandonment before the fix's own test assertion would have short-circuited the observation.

## D101 — FIXED, LANDED, WITH A QUALIFIER — see below. `BLOCKED` consumers never get an `UnmergedDependency` finding, and `--sync` has nothing to clear — self-disclosed in the code's own comments before this ledger entry existed

**Found by round IV task 1 (2026-09-02), while re-auditing §12 item 38's sub-clauses against the
now-fully-corrected D92/D98/D99/D100 state — a disclosed finding, not this TEST-ONLY task's own
job to fix.** Verified free before writing: form-agnostic sweep of this file's `D<n>` headings
found no `D101`; highest allocated number was `D100`.

**Correction, round IV final review (2026-09-02) — this was originally left un-numbered on a
reasoning that does not hold; numbered here instead.** The task's own report and this ledger's
first version of this finding declined to allocate a number, reasoning that a D-number here has
"so far" marked a defect discovered by investigation rather than one the code already names as its
own known gap. That premise is false: **D78** (`search `## D78 —` above) is exactly a code-
disclosed gap — the commit that filed it postdates, by two days, a commit that already wrote *"NOT
REACHED IN PRODUCTION TODAY... every row this harness ships today is `scope: "run"`..."` into
`src/fleet/orchestrator/findings.py` itself — and D78's own body explicitly rules it into the
ledger anyway, naming D56/D57 as the same already-adjudicated class. The decision not to number
this finding was right to reconsider, wrong in its stated reason; allocating D101 now, following
D78's own precedent rather than inventing a new rule.

**The gap, as measured.** `grep -rn "UnmergedDependency" src/ tests/` finds it only in comments and
docstrings — never a write site, never a test. `src/fleet/models/state.py:146-148` and
`src/fleet/orchestrator/reentry.py:711-714` both already name "a `pr.merge_wait_timeout_s` breach
(§3.4)" as one of three `blocked_by` triggers with "0 producers today" — the code's own comments
disclose this as unbuilt, which is why round IV task 1's own investigation found it rather than
inventing it. SPEC §12 item 38's own text describes two complementary sub-clauses this gap leaves
untested: a `BLOCKED` consumer getting an `UnmergedDependency` finding, and that finding clearing
once `--sync` resolves the dependency.

**Consequence.** `docs/CRITERIA_PLAN.md` §38's own status line and Rollup row both currently read
"only D94 still blocks" — false as of this entry; D101 is a second, independent blocker on two of
§38's 20 sub-clauses, unrelated to D94's PR-promotion gap.

**Not yet built:** the write site for `UnmergedDependency` (most naturally wherever `blocked_by`
propagation already runs, per `reentry.py:711-714`'s own comment) and the `--sync`-triggered
clearing path. That design choice is not made here.

**Update, round V research + controller (2026-09-02) — this gap does not have a single write site;
it splits into two halves of different size, and a fork blocks Half A's design.** Full sizing in
that round's research report is not reproduced here (see `docs/PROGRESS.md`'s round V checkpoint);
in summary: **Half A** (the `UnmergedDependency` finding write, `_apply_stub_reconcile`'s
`held_for_merge` branch, `cli.py:12149-12178`, currently reads `held_for_merge` and writes nothing
for it) is a small, TEST-ONLY-adjacent one-shot *once* a fork over what "`C` is `BLOCKED`" means is
resolved — resolved by **ADR-0112** (`docs/DECISIONS.md`): no `RepoStatus` transition, finding only.
**Half B** (the `--sync`-triggered clearing of that finding, plus firing T1) is NOT a one-shot — see
**D102** below, which the same sizing pass split out as its own independent gap: T1
(`orchestrator.stubs.supersede`) has zero production call sites anywhere in `src/`, so "firing T1"
in Half B means building the trigger, not calling an existing one.

**Half A FIXED, LANDED (round V task 4, 2026-09-02, `6f9b777`, merged `609f57f`, task-scoped review
Approved).** `_apply_stub_reconcile` now inserts one `UnmergedDependency` finding per held-for-merge
stub consumer (`consumer_repo_id`/`provider_repo_id`/`coord_key` payload), in the same
`StateWriter` transaction as the D99/D100 abandoned-decision branch — no `phases.status` write, no
`transition()` call, per ADR-0112. Review independently confirmed zero calls to
`transition()`/`append_blocked_by`/`UPDATE phases` anywhere in the touched function, and
independently reproduced the Rule-12 mutation proof in a fresh worktree. **D101 as a whole stays
PARTLY ADDRESSED — Half B (the `--sync`-triggered clearing and firing T1) remains fully open,
tracked jointly here and at D102.**

**Update, round VI task 6 (2026-09-02) — Half B splits further; the "firing T1" sub-part is FIXED,
the "clearing" sub-part is NOT.** Post-hoc task-scoped review (this task self-merged before
review — a controller briefing gap, not implementer error, see the process note in its own review
report) confirmed: Half B(i), firing T1, is genuinely landed — see D102's own updated entry.
**Half B(ii), the `--sync`-triggered clearing of the `UnmergedDependency` finding D101 Half A
writes, was never in this task's "What to build" scope** (its brief's Context section named it,
but the concrete build instructions and Rule-12 requirement did not ask for it — the implementer
correctly flagged this as a discrepancy rather than silently building or skipping it). No
production code reads or clears an `UnmergedDependency` finding anywhere. **D101 stays PARTLY
ADDRESSED — Half B(ii) is the sole remaining open piece, now precisely scoped rather than bundled
with "firing T1."**

**Update, round VI task 8 (2026-09-02, `9077795`, merged `3b861f9`, task-scoped review Approved) —
Half B(ii) lands. All three named pieces of D101 (Half A, Half B(i), Half B(ii)) are now genuinely
implemented and each proven by a real fixture-fleet test.** `_apply_stub_decisions` now deletes
the `UnmergedDependency` finding for a `(consumer, coord_key)` pair whenever its decision is T1,
targeted by the exact same fingerprint Half A's write site uses, in the same transaction as the
`stubs` UPDATE. Review independently reproduced both Rule-12 mutations (disabling the DELETE;
broadening it to prove targeting precision) and confirmed no scope overlap with sibling D103.

**Qualifier, found by the same review — D101 is FIXED, LANDED but not unconditionally reliable
under every invocation shape.** `fleet resume --repoll-prs` chains `_pr_sync_impl` (fires T1,
clears the finding) and `stub_reconcile` in one call, and `reconcile()`'s own pre-existing logic
immediately re-abandons the just-superseded row in that same call (its provider's PR is now
`MERGED`, no longer "open" per `_awaiting_merge`) — overwriting T1's outcome and the just-cleared
finding within one command. This is a distinct, newly-found gap, **allocated D105**, not a defect
in D101's own three landed pieces — each is correct on its own, proven by its own test using
`fleet resume` and `fleet pr --sync` as two SEPARATE commands, which does not reach D105's path.

---

## D102 — FIXED, LANDED (round VI task 6, `3cc679d`/`ae0d961`), WITH A QUALIFIER — see below.
`orchestrator.stubs.supersede` (T1) and `plan_revalidation` have zero production
call sites anywhere in `src/` — the automatic re-entry trigger §12.37's own criterion text depends
on is unwired

**Found by round V's research task (2026-09-02), while sizing D101 Half B (above) — disclosed as a
distinct, independently-trackable gap from D101's own stated scope, per that research's own
recommendation.** Verified free before allocating: form-agnostic sweep of `docs/
INTEGRATION_HONESTY.md`/`docs/DECISIONS.md`/`docs/CRITERIA_PLAN.md`/`docs/SPEC.md` for `\bD[0-9]+\b`
found `D101` as the highest allocated number.

**The gap, re-measured directly by the controller (2026-09-02), not carried forward from research's
own hand-traced claim** (research flagged this exact claim as needing a real `grep` re-check, since
its own trace was done by hand during a Bash-tool outage): `grep -rn "supersede(" src/` and `grep
-rn "plan_revalidation(" src/` each return exactly one hit — the function's own definition in
`src/fleet/orchestrator/stubs.py` (lines 300 and 365 respectively) — zero call sites anywhere else
in `src/`. `grep -rln` for importers of `orchestrator.stubs` across `src/` returns exactly one file,
`src/fleet/cli.py`, whose own import block (`cli.py:184-186`) imports only `ProviderFacts`,
`StubDecision`, `apply` (aliased `apply_stub_decision`), and `reconcile` (aliased `stub_reconcile`)
— `supersede` and `plan_revalidation` are not among them. This confirms research's hand-traced
finding exactly; no correction needed.

**Consequence.** T1 (`orchestrator.stubs.supersede`) is unit-tested as a pure function
(`tests/test_stubs.py` exercises it directly and extensively) but has no live invocation path. SPEC
§12 item 37's own criterion text ("re-running P to SUCCEEDED with its PR MERGED moves that row to
SUPERSEDED") describes an automatic trigger that does not fire in production today — `cli.py`'s
`_pr_sync_impl` (`:10021-10152`), the function SPEC's own docstring names as where T1 should fire
(`:10115`), already discloses in its own inline comment (`:10117-10127`) that the `pr_merged` event
it emits has no reader anywhere. This matters specifically for D101 Half B: `fleet pr --sync`
"firing T1" is not wiring a call into an existing trigger — the trigger itself does not exist in any
live code path and must be built.

**Not yet built:** the call site inside `_pr_sync_impl`'s `pr_merged` branch that constructs
`ProviderFacts` for the merged provider and invokes `supersede()`/`plan_revalidation()`. That design
choice, and how it composes with D101 Half B's own clearing logic, is not made here — this entry
only establishes the gap exists and is now tracked independently of D101.

**FIXED, LANDED (round VI task 6, 2026-09-02, `3cc679d`, merged `ae0d961`), with a qualifier this
task's own report omitted, found by the post-hoc task-scoped review that verified this landing.**
`_pr_sync_impl`'s `pr_merged` branch now builds `StubRecord`s for the merged provider's ACTIVE
stubs (`_stub_supersede_inputs`), calls `supersede()`/`plan_revalidation()` (both pure, unit-tested,
unchanged), and writes the `stubs` UPDATE plus a new `tasks` row (`kind='REVALIDATE'`, via the new
`insert_revalidation_task_row`) in one transaction with the stub decision — proven end-to-end by a
real `fleet pr --sync` fixture-fleet test (`tests/test_pr_e2e.py`), Rule-12 mutation-verified.
T1's literal complaint (zero production call sites) is genuinely closed.

**The qualifier: `grep -rn "INSERT INTO stubs" src/` returns nothing — no production code path
creates a `stubs` row at all.** That is §12.37/D80's own stub-*creation* gap (`--stub-blocked`'s
worker, `workers/buildgen.py`, was never built), unrelated to and unmoved by this fix. T1's newly-
wired trigger can only fire against rows nothing in production creates today, so §12.37's own
criterion is still not satisfiable end-to-end. This does not diminish what D102 closed (the trigger
genuinely fires against any `stubs` row that DOES exist, e.g. one seeded by a test fixture or
future stub-creation code) — it is a pre-existing, separately-tracked blocker one layer up, not a
defect in this fix. See D103 below for the residual gaps this same landing surfaced.

---

## D103 — FIXED, LANDED (round VI task 7, `6007423`/`2a5c2ac`). Two residual gaps surfaced by
D102's landing: a narrow crash-window ordering gap in `_pr_sync_impl`, and
`stubs.revalidation_task_id` (SPEC §3.5.1 step 4) is never written

**Found by round VI task 6's post-hoc task-scoped review (2026-09-02)** — both self-disclosed by
the review as fix-forward items, not by the implementer's own report. Verified free before
allocating: form-agnostic sweep of `docs/INTEGRATION_HONESTY.md`/`docs/DECISIONS.md`/`docs/
CRITERIA_PLAN.md`/`docs/SPEC.md` for `\bD[0-9]+\b` found `D102` as the highest allocated number.

**Gap 1 — the crash window.** Inside `_pr_sync_impl`'s per-repo loop, a merged PR causes THREE
separate `writer.submit(...)` transactions: (1) `_write_pr_record` marks the PR record durably
`MERGED`; (2) `emitter.emit("pr_merged", ...)`; (3) T1's own effect (the `stubs` `ACTIVE`→
`SUPERSEDED` UPDATE and the `REVALIDATE` task INSERT, correctly atomic with EACH OTHER, per the
brief). A crash between (1) and (3) leaves a durably-`MERGED` PR record whose stub was never
superseded. **Reviewed and found NOT a correctness bug**: no double-supersede is possible (the
UPDATE is guarded on `revalidation_round`/`state`; `supersede()` itself no-ops a `SUPERSEDED` row),
no duplicate `REVALIDATE` task is possible (measured directly against `ux_tasks_ident`), and the
crash outcome — `stub_reconcile`'s end-of-run sweep abandons the stub instead of superseding it —
is exactly what happened to 100% of cases before this task existed. No new failure mode; a
narrowing of an existing one.

**Not yet built:** on every `fleet pr --sync` invocation, sweep PR records already durably `MERGED`
(not just newly-observed-this-run) whose named stub is still `ACTIVE`, and re-fire T1 for them —
closing the crash window by making the trigger retry-safe rather than single-shot. Estimated ~10
lines, reusing `_stub_supersede_inputs`/`supersede`/`plan_revalidation` as-is (all three are already
idempotent against a replay).

**Gap 2 — `stubs.revalidation_task_id` is a real schema column
(`src/fleet/state/schema.sql:386`, `revalidation_task_id TEXT REFERENCES tasks(task_id) ON DELETE
SET NULL`) that SPEC §3.5.1 step 4 (`docs/SPEC.md:1774`) requires be set to the minted `REVALIDATE`
task's id — verified by direct grep (`grep -rn "revalidation_task_id" src/fleet/`) to be written
NOWHERE in production code**, only declared in `schema.sql` and the `v004_stub_lifecycle` migration
that added the column. `RevalidationPlan.coord_keys` already carries the data a write site would
need; `insert_revalidation_task_row` already returns the winning `task_id`. Not yet built: an
UPDATE of the relevant `stubs` rows to the returned `task_id`, in the same transaction T1's own
effect already runs in.

**Relationship to D101 Half B(ii).** Writing `stubs.revalidation_task_id` is plumbing, not the
`--sync`-triggered *clearing* of the `UnmergedDependency` finding D101 Half A writes — those remain
two distinct gaps. Whoever picks up D101 Half B(ii) should read `revalidation_task_id` (once Gap 2
here is closed) to find which `REVALIDATE` task a held consumer is waiting on, rather than
re-deriving it.

**Not yet built, either gap:** neither is designed in detail here — this entry establishes both
gaps exist and are now tracked, following D102's own precedent of disclosing a real gap without
prescribing its exact implementation.

**FIXED, LANDED (round VI task 7, 2026-09-02, `6007423`, merged `2a5c2ac`) — heading only updated
2026-09-08 (round VI task 82); this paragraph corrects a stale field, not the body above, per
`docs/INTEGRATION_HONESTY.md`'s own "status heading is a FIELD; the entry body is a RECORD" rule.**
Both gaps were closed the SAME day this entry was filed, by a sibling lane (`agent/roundvi-task2`,
task-scoped review APPROVED at `2a5c2ac`) — but the heading above was never updated to reflect it,
so this entry read OPEN for six days while the fix sat on `main`. Task-82 was dispatched against
that stale OPEN heading to build "gap 1" and "gap 2"; its first read-first step (CLAUDE.md: "a
finding is a hypothesis... and it perishes between filing and fix") re-derived both call sites
fresh against current `HEAD` and found `_fire_t1_for_provider` in `src/fleet/cli.py`
(`:13219` at `96555e2`) already implements exactly what "Not yet built" above describes for both
gaps: gap 1's post-loop sweep over
durably-`MERGED` PR records with a still-`ACTIVE` stub (`_pr_sync_impl`, D103 gap-1 sweep comment)
and gap 2's `UPDATE stubs SET revalidation_task_id = ?` inside T1's own transaction, keyed on the
pre-UPDATE `revalidation_round` exactly as this entry's own "Not yet built" text for gap 2
specifies. Task-82 wrote no source changes — re-implementing either gap would have been a
duplicate, disconnected transaction (violating the "same transaction T1's own effect already runs
in" requirement this entry itself states). Instead it independently re-derived the original
implementer's own Rule-12 mutation claims rather than trusting them inherited: disabling the gap-1
sweep loop (`continue` before `_fire_t1_for_provider`) reddens
`test_pr_sync_sweeps_a_pre_merged_providers_stub_left_active_by_a_prior_crash` while
`test_pr_sync_fires_t1_and_enqueues_a_revalidate_task_for_a_merged_providers_stub` (the
newly-observed-merge control) stays green; disabling the `revalidation_task_id` UPDATE (`continue`
before the `conn.execute`) reddens BOTH tests' `revalidation_task_id` assertions. Both mutations
passed the zero-change gate (`git diff --numstat --no-index` against a backup file, never `git
stash`) and were run with `fleet.__file__`/marker-in-source pinned to the mutating worktree before
either result was trusted (CLAUDE.md Rule 12's interpreter-isolation guardrail) — reproduced in a
detached worktree at `agent/roundvi-task82`, tree restored clean after each mutation. Whole-file
`tests/test_pr_e2e.py` + `tests/test_stubs.py`: 65/65 passed, unmutated. Only this heading and this
paragraph changed; no source edit was needed or made.

---

## D104 — FIXED, LANDED (round VI task 79, `4ead8f9`). `TaskKind.REVALIDATE` has no execution/dispatch path anywhere, and `settle_revalidation`
(T2/T3) has zero production call sites — the same "defined, unit-tested, zero callers" shape D102
found and fixed for T1, one layer downstream

**Found by research-3 (2026-09-02), while sizing §37's stub-creation worker and confirming, per
that research's own brief, whether closing creation alone would make §12.37 satisfiable end-to-end
— it does not, and this is why.** Verified free before allocating: form-agnostic sweep of `docs/
INTEGRATION_HONESTY.md`/`docs/DECISIONS.md`/`docs/CRITERIA_PLAN.md`/`docs/SPEC.md` for `\bD[0-9]+\b`
found `D103` as the highest allocated number.

**The gap, as measured by research-3, re-verified by the controller before allocating:**
`grep -rn "TaskKind.REVALIDATE" src/fleet/orchestrator/*.py src/fleet/workers/*.py` returns nothing
outside `orchestrator/stubs.py`'s own docstrings, which only describe the *plan* a `RevalidationPlan`
represents, never consume it — `cli.py`'s `_COARSE_TASK_KIND` dispatch-grain table has no mapping
for `REVALIDATE` (confirmed at D102's own landing, unchanged since). `grep -rn
"settle_revalidation(" src/fleet/` returns only `orchestrator/stubs.py`'s own definition — the
function that turns a `VerificationReport` into a T2 (`RESOLVED`)/T3 (another failed round)
`StubDecision` has zero production callers; `tests/test_stubs.py` is its only caller anywhere.

**Consequence.** D102 (T1's production trigger) now correctly mints `REVALIDATE` `tasks` rows —
but nothing ever claims, executes, or resolves one. Every `REVALIDATE` row sits `PENDING` forever
by construction, exactly as D102's own entry disclosed as expected scope at the time. SPEC §12
item 37's own criterion text requires the row eventually reach `RESOLVED` via T2 and the consumer
`SUCCEEDED` — none of that is reachable today, independent of whether a stub was ever created
(§37's own stub-creation gap is upstream of this one; both must close for §12.37 to be satisfiable
end-to-end, and they are independent gaps, not the same one restated).

**Not yet built:** a dispatch-grain mapping for `REVALIDATE` (`_COARSE_TASK_KIND` or an equivalent
routing decision), a worker/handler that turns a claimed `REVALIDATE` task into a real
`VerificationReport` (likely reusing `BuildverifyWorker`'s existing build/test-check engine, per
this project's own established pattern of reusing an existing worker rather than building a new
one — not confirmed here), and the call site that feeds that report to `settle_revalidation` and
applies the resulting `StubDecision`. That design choice is not made here — this entry only
establishes the gap exists and is now tracked, following D102's own precedent.

**Design choice made, 2026-09-03 (round VI, research-14) — sizing only, not a fix.** The
`_COARSE_TASK_KIND` dict this entry points at above is the wrong mechanism: that's D89/ADR-0101's
git-arbitration bookkeeping, and its own comment explicitly excludes `REVALIDATE` by design. The
real dispatch mechanism is the hand-composed `_run_*_wave` functions (`_run_verify_wave` traced in
full) — no `_run_revalidate_wave` exists, and `REVALIDATE` tasks sit outside `wave_members`
entirely, so they need their own driving loop (natural home: a new `fleet resume` step). SPEC's
own text confirms revalidation reuses `VerifyPipelineWorker`/`BuildverifyWorker`, not a new worker.
Splits into 3 pieces: (a) small `VerifyInput` field-threading, (b) the claiming loop +
`settle_revalidation` caller (MEDIUM, comparable to a §37 sub-task), (c) a `consumer_status` →
`phases` write (see D108 below — currently a silent no-op this piece must not reuse unmodified).
**Not dispatched this round** — see D107/D108, found by the same research pass, which make even
this full 3-piece build land as tested-but-inert infrastructure on a real tree.

**Correction, 2026-09-08 (controller, ADR-0128, research-44) — "tested-but-inert" understates the
risk; a REVALIDATE claiming loop built without D107 first is actively UNSAFE, not merely inert,
and this is now a live production risk, not a hypothetical.** `VerificationReport.
verified_against_stubs` is a pure pass-through of `RdepverifyInput.verified_against_stubs`
(`workers/rdepverify.py:123-128`), itself populated from a `stubs.state = 'ACTIVE'` query
(`cli._active_stubs_by_consumer`) — not derived from inspecting what Bazel actually built. The
instant a stub row flips to `SUPERSEDED` (T1), that query reads empty regardless of whether the
consumer's committed `BUILD.bazel` was ever rewritten off the stub label. A REVALIDATE task built
per this entry's own (b)/(c) sizing, without D107's rewrite landing first, would legitimately fire
T2 (`RESOLVED`) on a build that never touched the real dependency — precisely the "ships work
verified against nothing" failure SPEC §3.5.1 exists to prevent. This is no longer hypothetical:
round VI task 69 removed all three `--stub-blocked` CLI refusals, so stub creation (and therefore
a stub reaching `SUPERSEDED` via T1) is live in production today, not test-fixture-only as it was
when this entry was first written. **Ruling: D104(b)/(c) must land bundled with D107 in one task
(task-79), never merged separately** — see ADR-0128 judgment call 5 for the full reasoning and the
rejected split alternative.

**FIXED, 2026-09-08 (round VI task 79, `4ead8f9`, ADR-0128).** Landed bundled with D107/D108 per
the ruling directly above. D104(a): `VerifyInput.revalidation_round` (previously always 0) threads
into `RdepverifyInput` of the same name. D104(b): a new claiming loop over `tasks WHERE
kind = 'REVALIDATE' AND status = 'PENDING'` (`state/repository.py::claim_task_by_id`, not a
`WaveScheduler` wave), driven from a new `fleet resume` step between step 6 (unblocking) and step
7 (projection regen), re-runs `VerifyPipelineWorker` unmodified against the consumer's
now-rewritten `migrate/<consumer>` tip (D107, below) and calls `settle_revalidation`
(`cli._run_revalidation_claims_impl`/`_run_one_revalidation_task`). D104(c) is D108's own fix, see
that entry. The safety property this entry's own correction demanded — a REVALIDATE loop must
never observe a stub still pointing at the stub label while reading `verified_against_stubs` as
empty — is proven both positively (the real-Bazel headline test) and as a falsifiable negative
(`tests/test_stub_resolution_task79.py::
test_a_revalidate_round_without_d107s_rewrite_reads_false_empty_verified_against_stubs`, which
reproduces the exact hazard when D107's rewrite is skipped, per this task's own Rule-12 brief).

**Correction, 2026-09-08 (fix round 1, opus-tier review, C1/I2/I3) — the paragraph above
overclaimed both the safety property and the negative-proof test's coverage; both are now fixed
for real, and this correction records what changed.** (C1, Critical) The claim above was false as
landed: the "safety property" was a docstring/SPEC sentence, not an enforced mechanism —
`_rewrite_superseded_consumer_labels` runs best-effort from `_pr_sync_impl` and every failure mode
(a transiently unavailable monorepo checkout, drift, a rebase conflict, a render failure, a
refused push) became a `"FAILED: ..."` string nothing downstream checked, so a REVALIDATE task
claimed after such a failure would build against a tree still naming a stub with nothing refusing
it. **Fixed**: `cli._run_one_revalidation_task` now reads the checked-out `BUILD.bazel` before
dispatching `VerifyPipelineWorker` and refuses the round (task -> `PENDING`, a
`RevalidationLabelNotRewritten` finding naming the consumer and the still-present stub label) if a
`//third_party/stubs/...` label is still present — a real, mechanical gate, independent of
whether the upstream rewrite trigger succeeded. (I3) The test named above,
`test_a_revalidate_round_without_d107s_rewrite_reads_false_empty_verified_against_stubs`, did NOT
prove what this paragraph claimed: it asserted only `_active_stubs_by_consumer`'s DB-derived
`fidelity == {}`, a characterization of one query that passes identically whether or not D107 (or
now the C1 gate) exists — it never drove the claiming loop at all. **Replaced** by
`test_c1_gate_refuses_a_revalidate_round_whose_committed_tree_still_names_a_stub_label`, which
plants a real stub-labeled `BUILD.bazel` commit on `migrate/<consumer>`, drives the REAL
`_run_revalidation_claims_impl`, and asserts the round is refused (task `PENDING`, the finding
written, the stub still `SUPERSEDED` not `RESOLVED`) — proven a genuine discriminator by an
old-fails/new-passes run (with the C1 gate disabled via a copied-aside file: `settled:
verdict=PASS decisions=1`, i.e. the consumer is silently promoted despite the stub label still
present — the concrete C1 hazard, reproduced; with the gate: refused, as asserted). Also added:
`cli._pr_sync_lines` now surfaces a failed rewrite in `fleet pr --sync`'s own human-readable
output (previously silent — exit 0 with the failure visible only in the JSON `label_rewrites`
payload nothing rendered), proven by `test_pr_sync_lines_surfaces_a_failed_label_rewrite`/
`test_pr_sync_lines_is_silent_when_every_rewrite_succeeded`. **`FIXED, LANDED` stands** — the
review's own words once this gate and test exist for real.

## D107 — FIXED, LANDED (round VI task 79, `4ead8f9`). Nothing rewrites a consumer's `BUILD.bazel` dependency label from a stub target to
the real one once the stub resolves — SPEC §3.5.1 item 1 has zero production implementation

**Found by research-14 (2026-09-03), while sizing D104.** Verified free before allocating:
form-agnostic sweep found `D106` as the highest allocated number.

**The gap, as measured.** SPEC §3.5.1 item 1 (`docs/SPEC.md:1725-1868`) describes a consumer
repo's generated Bazel target depending on a stub's placeholder label at hoist time, and that
label being rewritten to the real target once the awaited coordinate resolves. `grep -rn` across
`src/fleet/` for any code that rewrites a generated `BUILD.bazel` dependency label post-hoist finds
nothing — the mechanism SPEC describes for closing this loop does not exist anywhere.

**Consequence.** `verified_against_stubs` (the field D104's T2 trigger needs to go empty for a
consumer) can never actually clear on a real tree, because nothing ever performs the rewrite that
would make the awaited coordinate genuinely resolved from the consumer's own build graph's point
of view — D104's T2 can only be exercised via a hand-seeded fixture, never end-to-end. This sits
upstream of D104 the same way §37's stub-creation gap sits upstream of D102/D104 — a third,
independent layer in the same chain.

**Not yet built:** the rewrite mechanism itself — where in the pipeline it would run (a new Phase-3
sub-step, most likely, since it operates on already-generated `BUILD.bazel` content), what
triggers it (presumably the same "stub resolved" signal D104/T2 would need), and how it locates
every consumer-side label referencing a given stub. Genuinely unsized here — that design choice is
not made in this entry.

**FIXED, 2026-09-08 (round VI task 79, `4ead8f9`, ADR-0128).** `cli._rewrite_superseded_consumer_
labels`/`_rewrite_one_consumer_label`, fired from `_pr_sync_impl`'s existing T1 trigger (D102) for
every distinct `consumer_repo_id` `supersede()` returns: cuts a fresh worktree from the consumer's
`migrate/<repo>`, rebases onto the current `integration` tip, re-derives the `BuildUnit`
(`_unit_deps` reused unmodified — the redirect logic itself was already correct per task 13's
Blocker C), re-renders via `BuildgenWorker` unmodified, and commits+pushes the diff via
`vcs/commits.py::guard`/`apply_and_commit` with the standard `Fleet-*` trailers. The ADR-0128
precondition check (comparing the branch's tree to its "last Phase-2 commit") does not correspond
to this codebase's actual `migrate/<repo>` topology — `vcs/filter_repo.py::ingest()` (D115)
force-moves the branch to Phase 3's own merge commit on every ingest, not a Phase-2 commit — and
is replaced with a check that every commit unique to `migrate/<consumer>` relative to
`integration` is either none or one of this function's own prior rewrites, disclosed in the
function's docstring and the task-79 report. Proven end to end under REAL Bazel (no seam) by
`tests/test_stub_resolution_task79.py::
test_d104b_claiming_loop_resolves_the_stub_under_a_real_bazel_build_and_test`, which reads the
label off the actual committed `migrate/<consumer>` file and confirms via a real `bazel query`
that the real provider's label — not the stub's — is what the analysed graph names.
`test_d107_rewrites_the_committed_migrate_branch_off_the_stub_label` and `test_d107_is_idempotent_
on_replay` prove the rewrite and its idempotency directly.

**Correction, 2026-09-08 (fix round 1, opus-tier review, I4) — "is replaced with a check that..."
above overclaimed an EQUIVALENCE the replacement check does not have; corrected to a disclosed
narrowing.** The literal ADR-0128/SPEC pseudocode comparison this paragraph replaces was correctly
identified as wrong (D115's topology, as stated above) — that correction stands. But the
replacement (every commit unique to `migrate/<consumer>` relative to `integration` must be none or
one of this function's own prior rewrites) is NARROWER than what judgment call 6 asked for, not a
like-for-like substitute: it can only see commits unique to `migrate/<consumer>`, so it is
STRUCTURALLY BLIND to drift that arrives already inside `integration` itself — e.g. a rebase
pulling in new upstream commits before this rewrite runs, which is the ADR's own named example of
what this check should catch. `_rewrite_one_consumer_label`'s unconditional `rebase(base)` two
lines below the check silently absorbs exactly that case, with no flag raised. This is disclosed
follow-on debt, not solved — `_rewrite_one_consumer_label`'s own docstring now states this
precisely (fix round 1) rather than claiming mechanical equivalence.

**Correction, 2026-09-08 (round VI task 81) — the C1 gate's two remaining disclosed nits (the fix
round 1 docstring's own "Disclosed, not fixed here" paragraph in `_run_one_revalidation_task`) are
now fixed, not merely disclosed.** Nit 1: a missing, unreadable, or unparseable committed
`BUILD.bazel` used to read as `""` (no stub label found) and fail OPEN, letting the round proceed
un-inspected; it now refuses via the SAME `_refuse_gate` path as a still-present stub label, with a
distinct `reason: "build_file_missing"` in the finding payload so the two repair actions are never
conflated. Nit 2: a permanently-refusing round used to loop PENDING -> claim -> refuse forever with
no operator-visible signal; the `RevalidationLabelNotRewritten` finding now carries a
`refused_count`/`first_refused_at` that survives across separate `fleet resume` invocations
(task_id-keyed, read from the finding's own prior payload — no new schema column), and once
`refused_count` reaches the task's own `tasks.max_attempts` (reused, not a bespoke parallel
counter), a `revalidation_round_stuck_refusing` WARNING is logged. Proven by
`tests/test_stub_resolution_task79.py::
test_c1_gate_refuses_a_revalidate_round_whose_build_bazel_is_missing` and `::
test_c1_gate_logs_a_distinct_warning_once_a_round_has_refused_max_attempts_times` (old-fails/new-
passes against a backed-up pre-fix `cli.py`, task-81 report). I4's own narrowing above is
UNTOUCHED by this round — a different, still-open disclosed gap.

## D108 — FIXED, LANDED (round VI task 79, `4ead8f9`). `StubDecision.consumer_status` has zero production readers — `_apply_stub_decisions`
writes only `stubs`/`findings`, never `phases`

**Found by research-14 (2026-09-03), while sizing D104.** Verified free before allocating:
form-agnostic sweep found `D107` (allocated immediately above, same commit) as the highest number.

**The gap, as measured.** `StubDecision.consumer_status` is the field meant to promote a consumer
repo to `SUCCEEDED` or escalate it to `REQUIRES_HUMAN_INTERVENTION` once its stub is resolved or
permanently diverged. `_apply_stub_decisions` (the landed D105/D106 call site) writes only to the
`stubs` and `findings` tables — `grep -rn "consumer_status"` across `src/fleet/` finds no site that
reads this field and writes `phases`.

**Consequence, currently silent and currently harmless.** Every `StubDecision` kind actually
produced in production today (T1, T4, T3-reconcile) leaves `consumer_status` at a value that keeps
the consumer `DEGRADED` regardless, so this gap has caused no observed defect yet. It becomes
live the moment D104's T2 (`RESOLVED`) or T3-`STUB_DIVERGED` transitions are wired in — those are
exactly the decision kinds `consumer_status` exists to act on, and reusing `_apply_stub_decisions`
unmodified would silently swallow them.

**Not yet built:** the `phases`-write call site for `consumer_status`. Trivial in isolation, but
correctly scoped as part of whichever future task wires D104's T2/T3 transitions in (D104-c above),
not dispatched standalone — there is nothing for it to act on until D104 lands.

**FIXED, 2026-09-08 (round VI task 79, `4ead8f9`, ADR-0128).** `SqliteStateRepository.
apply_stub_consumer_status` writes the `phases` CAS (mirroring `stub_degrade_transform`'s ADR-0124
transaction shape in the opposite direction, `DEGRADED -> {SUCCEEDED, REQUIRES_HUMAN_
INTERVENTION}`), called from the new D104(b) REVALIDATE claiming loop for exactly the two
`StubDecision` kinds this entry names (T2 all_clear, T3 `STUB_DIVERGED`) whenever `decision.
consumer_status is not RepoStatus.DEGRADED`. A `StubConsumerStatusApplied` finding records the
correction, mirroring `StubDegraded`'s own audit discipline. Proven end to end (FakeBazel) by
`tests/test_stub_resolution_task79.py::
test_d108_promotes_the_consumer_once_a_revalidation_round_genuinely_passes`. Landed bundled with
D104/D107 per ADR-0128 judgment call 5 — see D104's own entry above for why splitting was rejected.

---

## D105 — FIXED, LANDED (round VI task 11, `d8c1cd7`/`0243792`). `fleet resume --repoll-prs` can immediately abandon a stub T1 just superseded, in
the same call, overwriting T1's outcome and D101 Half B(ii)'s newly-cleared finding

**Found by round VI task 8's task-scoped review (2026-09-02), while independently investigating a
concern the implementer disclosed but did not diagnose or fix.** Verified free before allocating:
form-agnostic sweep of `docs/INTEGRATION_HONESTY.md`/`docs/DECISIONS.md`/`docs/CRITERIA_PLAN.md`/
`docs/SPEC.md` for `\bD[0-9]+\b` found `D104` as the highest allocated number.

**The gap, as measured by the review, re-verify before acting on it — this account is the
review's, not independently re-derived by the controller.** `fleet resume --repoll-prs` runs
`_pr_sync_impl` (fires T1 on a newly-merged provider: `stubs` `ACTIVE`→`SUPERSEDED`, the
`UnmergedDependency` finding cleared per D101 Half B(ii)) and then unconditionally runs
`_stub_reconcile_impl` in the **same call**. `SUPERSEDED` is in `reconcile()`'s own `OPEN_STATES`,
and the provider's PR is now `MERGED` (no longer "open"), so `_awaiting_merge` returns `False` —
`reconcile()` immediately re-sweeps the just-superseded row to `ABANDONED` via T3, writing a fresh
`UnresolvedStub` finding and overwriting T1's outcome (and this round's D101 Half B(ii) fix)
within one command invocation. No crash, no race, no unusual timing — ordinary `--repoll-prs`
success reaches this.

**Consequence.** D101 Half B(ii) (the finding-clearing logic) is correctly implemented and proven
by a real fixture-fleet test in isolation (`fleet resume` then `fleet pr --sync` as two separate
commands) — this is a DIFFERENT code path (`--repoll-prs` chaining both in one call) that
undoes it. D101 as a whole should be recorded as landed WITH this qualifier, not left implying
the clearing is reliable under every invocation shape.

**Not yet built:** either (a) `_stub_reconcile_impl`'s sweep needs to skip a row T1 just
superseded in the SAME call (an ordering/exclusion fix), or (b) `_awaiting_merge`'s definition of
"open" needs to account for a PR merged so recently that T1 already consumed the event this same
invocation (a narrower fix, more surgical but more fragile). Neither is designed here — this entry
only establishes the gap exists and is now tracked, following D102's own precedent of disclosing a
real gap without prescribing its exact implementation.

**FIXED, LANDED (round VI task 11, 2026-09-02, `d8c1cd7`, merged `0243792`, task-scoped review
Approved).** Option (a) chosen. `_fire_t1_for_provider` now returns the `(consumer_repo_id,
coord_key)` pairs it superseded; `_pr_sync_impl` accumulates these across both its normal
merge-observation loop and D103's own crash-window sweep into `t1_superseded_this_call`;
`_resume_impl` threads that set into `_stub_reconcile_impl`'s new `exclude_this_call` parameter,
filtering exactly those rows out of `reconcile()`'s sweep input before `reconcile()` runs.
`reconcile()` itself is byte-for-byte unchanged (confirmed by the review: its own file's diff
against base is empty). Review independently reproduced the Rule-12 mutation proof and confirmed
the exclusion key matches `_stub_reconcile_inputs`' own dict key exactly — not a broader
per-consumer or per-provider exclusion.

**Disclosed follow-up, found by the review, not this fix's own scope: a plausible CROSS-call
(not same-call) analogue.** `_stub_reconcile_impl` runs unconditionally on every `fleet resume`
call — so a `SUPERSEDED` row that survives until a LATER, separate `fleet resume` invocation (not
the same one T1 fired in) is exposed to the identical abandon mechanism this fix's exclusion set
cannot see, if a REVALIDATE task for it hasn't been processed by then (see D104: `REVALIDATE`
execution has no dispatch path yet, so this window can be arbitrarily long in practice today).
Not confirmed as a live bug — a plausible gap, not independently investigated further. See D106.

---

## D106 — FIXED, LANDED (round VI task 14, `d503eb2`/`877155f`). Possible cross-call analogue of D105: a `SUPERSEDED` stub surviving to a LATER
`fleet resume` invocation may still be abandoned by `reconcile()`, unprotected by D105's fix

**Found by round VI task 11's task-scoped review (2026-09-02), flagged as plausible but not
independently investigated as a live bug.** Verified free before allocating: form-agnostic sweep
of `docs/INTEGRATION_HONESTY.md`/`docs/DECISIONS.md`/`docs/CRITERIA_PLAN.md`/`docs/SPEC.md` for
`\bD[0-9]+\b` found `D105` as the highest allocated number.

**The gap, as reasoned by the review — NOT independently re-derived or measured by the
controller; re-verify before acting on it.** D105's fix excludes, from `reconcile()`'s sweep, only
the `(consumer_repo_id, coord_key)` pairs T1 superseded in the SAME `fleet resume` call
(`t1_superseded_this_call`, scoped to one invocation's own accumulator, never persisted). Since
`_stub_reconcile_impl` runs unconditionally on every `fleet resume` invocation, a stub that T1
superseded in one call, then survives — un-revalidated — into a SUBSEQUENT, separate `fleet
resume` call has no exclusion protecting it: that later call's own `t1_superseded_this_call` is
empty (T1 didn't fire again this call), so the row is fully exposed to `reconcile()`'s ordinary
sweep, which per its own existing logic (unchanged by D105's fix) would abandon a `SUPERSEDED` row
whose provider's PR is `MERGED` and therefore not "open." Because D104 (`REVALIDATE` task
execution has no dispatch path) means nothing currently advances a `SUPERSEDED` row toward
`RESOLVED`, this window — a superseded-but-not-yet-revalidated stub sitting between resume calls —
is not a narrow timing accident; it may be the ORDINARY case today, for as long as D104 stays
open.

**Not yet built or confirmed:** whether this is genuinely reachable (a real test needs to drive
two SEPARATE `fleet resume` invocations, superseding a stub in the first and observing whether the
second abandons it) and, if so, what the fix should be — likely something that persists
"recently superseded, awaiting revalidation" as real state (not a call-scoped accumulator), or a
`reconcile()`-side change to its own "open" definition (option (b) from D105's own entry, deferred
there as "more fragile"). Neither is designed here — this entry only establishes the gap as
plausible and disclosed, following D102/D104/D105's own precedent, not as a confirmed defect.

**FIXED, LANDED (round VI task 14, 2026-09-02, `d503eb2`, merged `877155f`, task-scoped review
Approved).** **Confirmed real, not refuted**, via a genuine two-separate-invocation test (not two
calls in one process) before any fix was written — a stub superseded in one `fleet resume` call,
surviving un-revalidated into a later separate call, was incorrectly re-abandoned by
`reconcile()`'s ordinary sweep, since D105's own exclusion set is call-scoped and never persisted.
Option (a) chosen: a new `_stub_awaiting_revalidation` query protects a `SUPERSEDED` row whose
`revalidation_task_id` (D103) names a `tasks` row not yet `DONE`/`FAILED`, filtered into
`_stub_reconcile_impl`'s exclusion set alongside D105's own `exclude_this_call` (a plain
`frozenset` union, additive/independent — no override risk). `reconcile()` itself remains
untouched (empty diff, matching D105's own precedent). A `SUPERSEDED` row with no revalidation-task
tracking is deliberately left unprotected (proven by a control) — the fix's scope is not
overbroad, targeted precisely by SQL semantics (an `INNER JOIN` on `revalidation_task_id`
structurally excludes any NULL-tracked row).

**Correctness given D104 stays open, independently confirmed by the review**: since nothing
currently moves a `REVALIDATE` task off `PENDING` (D104), this exclusion's protection is
effectively indefinite for as long as D104 stays open — the review grepped every `REVALIDATE`
reference and confirmed no code path anywhere (dispatch loop, git-arbitration sweep) ever executes
one. This is disclosed, not a silent accident: the CLI reports an `excluded_awaiting_revalidation`
payload key on every `--repoll-prs` call naming exactly which rows are held back this way.

## D109 — FIXED, LANDED (round VI task 23, `380e3f0`/merge). `tests/fixtures/llm/stub_openai_server.py`'s `assert_loopback_only()` guard has a
false positive on `ProcessPoolExecutor`'s forkserver control channel (a Unix-domain socket)

**Found by round VI task 22 (2026-09-03), while driving the full pipeline under `--profile local`
through this fixture (task 21).** Verified free before allocating: form-agnostic sweep found `D108`
as the highest allocated number.

**The gap, as measured.** `assert_loopback_only()`'s `socket.socket.connect` monkeypatch checks the
connection target address as a `(host, port)` tuple. `ProcessPoolExecutor`'s forkserver control
channel connects over a Unix-domain socket, whose address is a filesystem path string, not a
`(host, port)` tuple — the guard's tuple-only check misidentifies this as an off-loopback attempt,
producing a false positive (`REQUIRES_HUMAN_INTERVENTION`) on any code path that spins up a real
`ProcessPoolExecutor` (e.g. `symbolindex.py`'s per-file CPU-bound parsing) while the guard is
active. Task 22 reproduced this directly: disabling its own workaround reproduced the exact
false-positive failure.

**Consequence.** Not a safety hole — the guard fails CLOSED (over-cautious, blocking legitimate
local IPC) rather than open (missing a real off-loopback call), so no defect class this fixture
exists to catch is weakened. But it is a real ergonomics trap for any future caller of this shared
fixture that needs a real `ProcessPoolExecutor` active while the guard is engaged: task 22 worked
around it locally (substituting a `ThreadPoolExecutor` for the guarded run's `cpu_pool` only,
independently confirmed by review to not weaken the safety proof — the LLM call path is orthogonal
to `cpu_pool`), but did not fix the shared fixture, correctly out of that task's own scope.

**Not yet built:** teaching `assert_loopback_only()` to recognize a Unix-domain socket address
(a `str`, not a `(host, port)` tuple) as never off-loopback by construction — a small, well-scoped
fix to the shared fixture, deferred to whoever next needs this guard active alongside a real
process pool. That design choice is not made here.

**FIXED, 2026-09-03 (round VI task 23, commit `380e3f0`).** Reproduced the original bug first
(using `multiprocessing.get_context("forkserver")` specifically — the platform-default `fork`
context does not open the `AF_UNIX` control channel and so does not reproduce it, independently
confirmed by task-scoped review with its own probe script), then fixed the guard additively: a
new `isinstance(address, str)` branch recognizing a Unix-domain socket address runs before the
original, byte-identical tuple-based `(host, port)` off-loopback check — the existing detection is
untouched. Both discriminator directions independently reproduced by review: reverting the fix
turns the false-positive regression test RED (with the exact pre-fix failure) while the
genuine-off-loopback-attempt test stays green, proving the two checks are independent; restoring
returns both to green. Disclosed scope limit (Linux's abstract-namespace `bytes` address variant
left unhandled) independently confirmed sound, not a corner cut: CPython's own
`multiprocessing.connection.arbitrary_address('AF_UNIX')` unconditionally returns a filesystem-path
`str` on every platform, and this repo's only production `forkserver` caller
(`orchestrator/budgets.py::new_cpu_pool`) builds its pool the standard way — no code anywhere
produces the unhandled shape.

## D110 — OPEN. §11.3's narrative "halve cpu_pool/subprocess semaphores on a breach" backoff step
has no resizable primitive to call — deliberately deferred, not part of §12.22's literal text

**Found by research-17 (2026-09-03), while sizing §12.22's runtime RSS-sampling sub-clause.**
Verified free before allocating: form-agnostic sweep found `D109` as the highest allocated number.

**The gap, as measured.** `docs/SPEC.md`'s §11.3 narrative states that a resource-guard breach
"halves the effective `cpu_pool` and `subprocess` semaphores" before a third-consecutive-breach
hard halt. `src/fleet/orchestrator/budgets.py`'s `Limits` dataclass has no live-resizable primitive
for either: `Limits.subprocess` is a plain `asyncio.Semaphore` (no resize API); `Limits.cpu_pool`
is a `ProcessPoolExecutor` (worker count fixed at construction, no supported way to shrink it live
without carefully handling in-flight work). The only resizable primitive anywhere in this module,
`Limits.llm`'s `ResizableLimiter`, has zero production callers of its own `.resize()` — its own
docstring says "what calls `resize()`, and with what, is a later change."

**Consequence.** §11.3's narrative behavior (graduated backoff before a hard halt) is not
implementable today without first building new resize-capable replacements for `ProcessPoolExecutor`
and `asyncio.Semaphore` — a materially bigger, riskier change with its own correctness surface (an
in-flight-work-during-shrink hazard neither primitive is designed to handle).

**Not required by §12.22's literal acceptance text**, which only requires (i) RSS stays under the
ceiling throughout, sampled correctly, and (ii) a variant that inflates only pool children/
containers fails the criterion correctly (i.e. halts/exits 5) — not that a halving step happens
first. Deliberately deferred: round VI's RSS-sampling implementation (tasks B1/B2/B3) is
explicitly scoped to the sample→compare→halt(exit 5) path only, per this D-ticket, rather than
either silently building the resize infrastructure or silently dropping the narrative's promise
without disclosure.

**Not yet built:** resize-capable `cpu_pool`/`subprocess` primitives, the breach-count-based
halving trigger, and the in-flight-work-safety design a live pool/semaphore shrink needs. That
design choice is not made here.

## D111 — OPEN. §12.31 "wrong contract hoist detected and rolled back" has no mechanism anywhere
in `src/` — confirmed multi-leg, not one-shot

**Found by research-22 (2026-09-03), sizing §12.31 for round VI dispatch.** Verified free before
allocating: form-agnostic sweep found `D110` as the highest allocated number.

**The gap, as measured.** `ContractStatus.FAILED` is declared but never assigned anywhere in
`src/fleet/`. Zero `git revert` call sites exist in the entire codebase. `_hoist_contracts`
(`src/fleet/graph/cycles.py:622`) is a pure in-memory trial simulation with no rollback branch.
`--forbid-hoist` (`src/fleet/cli.py:2875`) is parsed only to be explicitly refused with exit 2 —
stubbed, not wired. *(This paragraph describes the state before round VI task 55; Leg A below is
now built — see the dated marker after "Not yet built" for what that changes and what it does
not.)*

**Why this is not a one-shot task.** SPEC's own §3.1 6c-H design (`docs/SPEC.md:589-629`) requires
at minimum four to five independently dispatchable, interacting legs: (A) not-shared detection +
state-surgery rollback; (B) a new `git revert -m 1`/`Fleet-*`-trailer primitive (no precedent
exists anywhere in `src/fleet/vcs/`); (C) broke-owner detection via a `FILE_PATH`-collision join
and/or Phase 3 build-failure attribution; (D) the unhoist blast-set/phase-demotion/downstream-
merge-refusal logic; (E) finishing the already-stubbed `--forbid-hoist` wiring. This is
structurally the same shape as the D94/D104 stub-lifecycle chain — a coordinated multi-piece
mechanism, not a single missing wiring call — and is deliberately NOT dispatched as a single round
VI task for the same reason that chain was repeatedly, correctly deferred rather than forced.
Full findings: `.superpowers/sdd/round-V-criteria-closure/research-22-report.md`.

**Not yet built:** any of legs A–E above. No design choice among them is made here — a future
round should build a dedicated multi-task closure plan (mirroring how D94/D104 and §12.37/§12.38
are tracked) before dispatching the first leg.

**Leg A landed 2026-09-06 (round VI task 55, ADR-0120) — case (i) only; the heading stays OPEN.**
`graph/cycles.py::_hoist_contracts` now counts distinct `src_id` repos on the greedy commit loop's
own `materialized = _materialize(edges, (chosen,))` result before committing `chosen`; below
`scan.contracts.min_consumers` it records `chosen` as `ContractStatus.REJECTED` with
`status_detail="not_shared_after_retarget"`, raises a `ContractNotShared` `GraphFinding`
(`severity="warn"`), and simply never reassigns `edges` to `materialized` — the pre-hoist snapshot
the loop already held, needing no reconstruction. `CycleReport` gained `findings`/
`rejected_contracts`; `cli._sequence_impl` persists both (`_rejected_contract_rows`,
`_persist_contract_not_shared_findings`). Proof: `tests/test_graph_cycles.py::
test_a_not_shared_after_retarget_contract_is_rejected_with_an_exact_rollback` (unit level,
byte-exact edge restore asserted row-by-row) plus an end-to-end CLI test asserting the database
directly. **Still open, unchanged by this leg:** legs B–E in full (case (ii) `HoistBrokeOwner`/
`FAILED`/`git revert -m 1`, the unhoist blast set, `HoistRollbackDemotion`, phase demotion, the
downstream-merge refusal, and `--forbid-hoist`/`--force-hoist`'s wiring — the refusal at
`cli.py:2875` above is untouched). `ContractStatus.FAILED` is still never assigned anywhere in
`src/fleet/`, and zero `git revert` call sites still exist — the two sentences in the "gap, as
measured" paragraph above about `FAILED`/`git revert` remain true of everything except case (i).
`docs/CRITERIA_PLAN.md`'s §31 entry records the same split. Full details:
`.superpowers/sdd/round-VI-criteria-closure/task-55-report.md`.

**Leg B landed 2026-09-06 (round VI task 59) — the `git revert -m 1` + `Fleet-*`-trailer primitive
built and proven standalone; still not wired into anything.** `Git.revert(sha, *, mainline=1)`
(`src/fleet/vcs/git.py`) runs `git revert --no-commit -m <mainline> <sha>`: `True` on a clean
stage, `False` on a settled conflict (`REVERT_HEAD` resolves — caller must `abort_revert()`),
`GitCommandError` for anything else (verified empirically that git 2.43 accepts `-m 1` on an
ordinary single-parent commit, and only refuses a mainline the commit lacks — this method does
not itself validate merge-vs-non-merge). `commits.revert_and_commit` stages then stamps the
standard six `Fleet-*` trailers via the existing `commit()` call, returning `RevertOutcome`
(`commit_sha`, `conflicted`) — mirrors `apply_and_commit`'s stage/commit split, deliberately
without an `already_applied` idempotency guard of its own (a revert has no `Fleet-Patch-Id` to
check against; that question belongs to whichever future leg wires this up and knows what
trailer to look for). Proof: 7 new tests in `tests/test_vcs.py` against a real git repo,
including a genuine two-parent merge commit and a genuine `-m 1` revert conflict; mutation-tested
(dropping `--no-commit`, and hardcoding `mainline`) — both mutations independently reddened the
tests built to catch them, confirmed via a backup-diff zero-change gate before and after. **This
is Leg B only.** `ContractStatus.FAILED` is still never assigned anywhere in `src/fleet/`, and
this primitive has **zero call sites** outside its own tests — legs A (landed) and B (this leg)
are the only two of the five built; C, D, and E remain exactly as sized in research-32. Full
details: `.superpowers/sdd/round-VI-criteria-closure/task-59-report.md`.

**Leg E landed 2026-09-06 (round VI task 58) — `--forbid-hoist` wiring; the heading stays OPEN.**
`GraphSection.forbidden_contract_ids` (`settings.py`) is threaded from `--forbid-hoist CONTRACT_ID`
(repeatable) by `cli._sequence_graph_config`, which no longer refuses the flag — the exit-2 stub
at `cli.py:3059-3060` (pre-task-58 numbering) is deleted; `--force-hoist` keeps its own refusal
untouched (out of scope, per the brief's own scoping judgment call). `_hoist_contracts`'s
candidate filter (`graph/cycles.py`) excludes any contract on that set, additive alongside Leg A's
own not-shared branch, never touching it. `cli._sequence_impl` writes `contracts.status =
'FORBIDDEN'` (`_forbidden_contract_rows`, guarded `AND extractable = 1` — see that function's
docstring for why) and a `ContractHoistOverride` finding per currently-FORBIDDEN contract
(`_persist_contract_hoist_override_findings`, DELETE-then-INSERT keyed on `(run_id, kind)`, so it
always reflects the CURRENT set rather than only what one invocation typed) — SPEC's "the finding
is the authority" mechanism (`docs/SPEC.md:6762-6768`). `workers/contracts.py::
carry_over_committed` is widened to carry `FORBIDDEN` across a `fleet scan` rebuild the same way it
already carried `HOISTED`/`MIGRATED` — this is the part that makes the veto "sticky across
re-sequencing" (`docs/SPEC.md:6746-6753`) rather than reset on the next scan, unlike a `REJECTED`
row (Leg A), which is deliberately NOT carried because it is meant to be re-tried against fresh
data. Proof: `tests/test_graph_cycles.py::
test_a_forbidden_contract_is_excluded_from_hoisting_alongside_the_extractable_filter` (unit,
mutation-proven — flipping the new filter clause to a no-op reddens it) and
`tests/test_sequence_e2e.py::test_a_forbid_hoist_veto_survives_a_real_re_scan_and_re_sequence`
(end-to-end: exit 0, `FORBIDDEN` status, the finding row, and survival across a REAL second `fleet
scan`, not merely a second `fleet sequence`). **This corrects Leg A's paragraph above**, whose
"still open" list named `--forbid-hoist`'s wiring and the untouched `cli.py:2875` refusal — both
now stale; annotated here rather than edited there, per this file's own history-preservation
convention. **Still open, unchanged by this leg:** legs B–D in full and case (ii) generally
(`HoistBrokeOwner`/`FAILED`/`git revert -m 1`, the unhoist blast set, `HoistRollbackDemotion`,
phase demotion, the downstream-merge refusal), and `--force-hoist`'s own wiring.
`ContractStatus.FAILED` is still never assigned anywhere in `src/fleet/`, and zero `git revert`
call sites still exist. Full details: `.superpowers/sdd/round-VI-criteria-closure/task-58-report.md`.

**Update, 2026-09-06 (round VI task 65) — Leg D slice 1 landed: persisted un-hoist, blast-set
demotion, and the downstream-merge refusal check (ADR-0122 Decisions 1/2/3/6); the heading stays
`OPEN`.** `cli._graph_edges` now carries the `contracts.status IN ('HOISTED','MIGRATED')` filter
ADR-0122 Decision 1 designed — the crash it closes (`GraphError` on any edge naming a vanished
contract node) is reproduced-then-closed by
`tests/test_unhoist_rollback.py::test_a_failed_contracts_edges_crash_the_pre_fix_query_and_the_fix_closes_it`.
A new `cli.unhoist_contract(read_conn, settings, *, writer, run_id, contract_id, now)` builds the
graph, runs the transitive downstream-merge-refusal check (`graph/query.py::descendants` over
`G_order`, seeded at every blast-set member with a `MERGED` PR — proven to catch a two-hop merged
descendant a single-hop check would miss), and on the non-refused path writes
`contracts.status = 'FAILED'`, demotes each qualifying blast-set member via
`SqliteStateRepository.demote_to_floor(floor=Phase.TRANSFORM)` directly (never
`phase_floor`/`evidence_holds`/`resume_floor`/`cli._demote_to_floors`), and writes one new
`HoistRollbackDemotion` finding per demoted repo plus a new `HoistRollbackRefused` finding on the
refused path. **Disclosed deviation from the brief's own "one `StateWriter` unit" instruction**:
`demote_to_floor` manages its own `writer.submit()` transaction internally, so nesting it inside
another already-running unit is a real single-pump-queue deadlock, not a style question — the
function instead runs three write phases (status flip, N per-repo `demote_to_floor` calls, one
findings batch), documented in `unhoist_contract`'s own docstring. **Disclosed SPEC-vs-ADR wording
divergence, not resolved here**: `docs/SPEC.md`'s own §3.1 6c-H prose (as corrected by this same
ADR-0122) reads "SUCCEEDED beyond `PHASE_SCAN`" for the demotion-eligibility gate, while ADR-0122
Decision 3's own text (landed in `docs/DECISIONS.md`) reads "SUCCEEDED beyond `Phase.TRANSFORM`" —
a strictly narrower predicate. This task followed Decision 3's text verbatim, per the brief's own
instruction not to re-litigate Decision 3; the two documents disagree with each other and neither
is corrected by this task. **This is the DB/graph half only** — legs C1/C2 (the trigger), the
`git revert -m 1` revert-series execution (ADR-0122 Decisions 4/5), and the production wiring
that would ever call `unhoist_contract` are all still unbuilt; nothing outside its own tests calls
it. Mutation-proven: the `_graph_edges` filter (`IN` → `NOT IN`) reddens the crash-reproduction
test while an unrelated, DB-free §12.31 Leg E test
(`tests/test_graph_cycles.py::test_a_forbidden_contract_is_excluded_from_hoisting_alongside_the_extractable_filter`)
stays green; the `descendants` traversal (narrowed to direct successors only) reddens the two-hop
refusal test while the direct-hop sanity test stays correctly refused. Full account:
`.superpowers/sdd/round-VI-criteria-closure/task-65-report.md`.

**Correction, 2026-09-07 (task-65 fix round, controller review) — the paragraph above's "Disclosed
SPEC-vs-ADR wording divergence, not resolved here" is superseded; the divergence IS now resolved,
and in SPEC's favor.** The review traced it to a transcription slip in ADR-0122 Decision 3 itself,
not a considered narrowing: `demote_to_floor`'s span is INCLUSIVE of `floor`
(`orchestrator/reentry.py`; `state/repository.py:1674`'s own docstring), so
`floor=Phase.TRANSFORM` already means "`SUCCEEDED` at TRANSFORM or above" — which IS SPEC's own
UNMODIFIED "beyond `PHASE_SCAN`" text restated exactly (the paragraph above's parenthetical "(as
corrected by this same ADR-0122)" was itself wrong — that SPEC clause was never touched by
ADR-0122's landing). `cli.unhoist_contract`'s pre-filter gate is deleted; `demote_to_floor` is now
called for every blast-set member unconditionally, letting its own `()` no-op return do the
filtering. `docs/DECISIONS.md` ADR-0122 Decision 3 carries a matching dated correction. Fixed in
the same task-65 branch's fix-round commit; kept here as the record of what this entry said before
the fix, not rewritten.

**Leg C2 (detection half) landed 2026-09-06 (round VI task 66, ADR-0123) — case (ii)'s
`HoistBrokeOwner`/`contracts.status='FAILED'` now real; the heading stays OPEN.**
`ContractStatus.FAILED` is now assigned for real, for the first time in this codebase's history:
`BuildInput.hoist_watch` (a new `tuple[HoistWatch, ...]`, precomputed once per run in
`_run_build_wave`/`_hoist_watch_for_run` from the same `HOISTED`/`MIGRATED` contract set
`_eligible_contract_units`/`_sequence_contracts` already rehydrate, unscoped to any one repo's
wave — attribution is a plain string match, never an edge/wave-membership join, per
research-38-report.md Question 1) is checked inside `BuildPipelineWorker.run`'s VERIFY_UNIT
handling by a new `_attribute_hoist_break`/`_hoist_break_match` pair against the FULL build log
(`WorkerError.artifact_ref`, never the bounded `stderr_tail`) whenever a step's error is a
`FailureClass.BUILD_ERROR`. A match flips that error's `retryable` to `False` — the entire
integration with the shared retry ladder (`RetryPolicy.decide`'s first branch, `retry.py:
166-176`, already TERMINATEs with no attempt charged; zero changes to `retry.py`/
`orchestrator/runner.py`, per the brief's own explicit prohibition) — and records the match on
`BuildOutput`, which `_BuildSink.__call__` reads to write `UPDATE contracts SET status = 'FAILED'`
and a `HoistBrokeOwner` `findings` row (`severity='error'`, payload carrying `contract_id`,
`hoist_target_path`, `repo_id`, and the matched bazel label line). `workers/contracts.py::
carry_over_committed` is widened a THIRD time (after `FORBIDDEN`, Leg E above) to carry `FAILED`
across a `fleet scan` rebuild — sticky like `HOISTED`/`MIGRATED`, not dropped like `REJECTED` — a
measured judgment call recorded as ADR-0123 (the reasoning: unlike `REJECTED`, whose hoist was
never committed, a `FAILED` contract's hoist commit is NOT reverted by anything today, since Leg D
does not exist, so the code is physically in the monorepo exactly like a `HOISTED` row's).
Proof: `tests/test_workers_build.py::test_hoist_broke_owner_matcher_reads_real_quoted_bazel_error_forms`
(unit, the five REAL quoted bazel error strings already in this codebase's own adapter docstrings
plus three constructed cases, each asserted individually — correction, round VI task 66 fix round:
all five real strings are negative cases, correctly returning no match; the POSITIVE match path is
exercised only by the three constructed cases, never by any of the five real strings) and two more
unit tests proving the
`BuildPipelineWorker._attribute_hoist_break` wiring itself (retryable flip + no-op cases);
`tests/test_workers_contracts.py::test_a_failed_contract_survives_the_rebuild_it_is_not_part_of`
(the ADR-0123 carry-over proof, mirroring the pre-existing `HOISTED` test); and
`tests/test_build_e2e.py::
test_a_real_build_failure_naming_a_hoisted_contracts_package_is_attributed_and_terminal`
(end-to-end through the real CLI: a real `scan → sequence → transform → build`, a directly-seeded
`HOISTED` contract row — the organic path is a separate, larger, disclosed pre-existing gap, see
below — a crafted-but-realistically-shaped `bazel build` failure through the `cli.BAZEL_RUNNER`
seam, and a real database left with `contracts.status='FAILED'`, one `HoistBrokeOwner` finding,
and `phases.attempts` unchanged at 0 for the failing repo — the assertion that actually proves the
`retryable=False` wiring worked, not merely that the string match fired). **This closes only
Leg C2's DETECTION half.** The `git revert -m 1` primitive's call site (Leg B exists standalone,
still unwired), the unhoist blast set, `HoistRollbackDemotion`, phase demotion, and the
downstream-merge refusal are ALL still Leg D's job, not designed here — a `FAILED` contract's
failing repo is left at whatever `RetryPolicy._terminal_status` already does today
(`REQUIRES_HUMAN_INTERVENTION`), a disclosed, deliberately incomplete placeholder until Leg D
exists to re-route it, exactly as research-38-report.md's own "what Leg D's design should know"
section anticipated. **Disclosed, pre-existing scope boundary the e2e proof works around rather
than closes:** no production code populates `BuildUnit.contract_deps` (D113/ADR-0119's own
narrow-read-only-PASS-2b scoping — "nothing in `src/` commits hoisted contract content"), so no
real manifest-driven dependency on a hoisted contract's package exists for a REAL, organically-
triggered `bazel build` to fail against; the e2e proof's `contracts` row is seeded directly
(this file's own established convention for state an earlier phase does not itself organically
produce), and the "does real bazel actually spell its errors this way" question is answered
independently at the unit level against five real quoted strings, never invented for this task.
`ContractStatus.FAILED` is no longer "declared but never assigned" — that sentence in this entry's
opening "gap, as measured" paragraph is now stale for case (ii) specifically and is left
unedited per this file's own annotate-don't-rewrite convention; it remains literally true only of
whatever Leg C1 would additionally need. Full details:
`.superpowers/sdd/round-VI-criteria-closure/task-66-report.md`.

**Fix round, round VI task 66 (2026-09-06) — controller review (opus-tier) independently
reproduced every finding against a real seeded schema or a fresh pytest run; all fixed.**
(C1, critical) The ADR-0123 decision above was INERT in production: `cli._committed_contracts`
(`cli.py:2566-2603`, repointed +1 by round VI task 83's D91 fix adding one import above it — pure
insertion, confirmed by exact-line-content match against the current tree), the ONLY production
feeder of `carry_over_committed`'s `committed` argument,
still selected `WHERE status IN ('HOISTED','MIGRATED','FORBIDDEN')` — no `'FAILED'` — so a real
`FAILED` row was silently dropped and RE-DERIVED AS `EXTRACTABLE` on the next `fleet scan`,
re-hoisting a contract that had just broken a build (precisely the `REJECTED` treatment ADR-0123
argues against). The pre-existing unit test (`test_a_failed_contract_survives_the_rebuild_it_is_
not_part_of`) could not have caught this — it constructs `committed` nodes directly, bypassing
`_committed_contracts` entirely (now annotated to say so in its own docstring). Fixed by adding
`'FAILED'` to `_committed_contracts`'s SQL list (mirroring round VI task 58's `e3b1a86`, which
widened both halves for `FORBIDDEN` in one commit); proven end to end against a real database
driven through a real `fleet scan`/`fleet sequence` (`tests/test_sequence_e2e.py::
test_a_failed_contract_survives_a_real_re_scan`, calling `_committed_contracts` directly against
the resulting database rather than a second full `fleet scan` — a second real scan currently
crashes on the disclosed `_graph_edges` gap ADR-0122/task-65 owns, reproduced and confirmed before
the test was written to avoid it). (I3, important) `_hoist_watch_for_run` is recomputed fresh per
wave; its `HOISTED`/`MIGRATED`-only filter silently stopped watching a contract the moment it went
`FAILED`, so every LATER-wave consumer of the SAME broken hoist would burn its own full retry
ladder unattributed. Fixed by widening the filter to include `FAILED`; proven
(`tests/test_sequence_e2e.py::test_a_failed_contracts_watch_survives_into_a_later_wave`). Also
fixed: 13 anchored citations across this file and `docs/CRITERIA_PLAN.md` that this task's own
`cli.py` insertion drifted (re-measured and repointed by content, not by offset —
`tests/test_integration_honesty_citations.py` now 70/70, matching `main`); `HoistBrokeOwner`'s
"remains no-Python" declaration in `schema.sql`/`docs/SPEC.md` corrected now that it has a live
writer; 6 `ruff check` regressions (2 unused imports, 1 ASYNC240 blocking-call, 3 line-length);
and the overstated claim above that the five real quoted bazel strings prove the matcher's
positive path (corrected: all five are negative cases; the positive path is exercised only by
constructed text). Full fix-round account:
`.superpowers/sdd/round-VI-criteria-closure/task-66-report.md`'s fix-round section.

## D112 — PARTLY ADDRESSED. `BuildUnit.test_srcs` is never populated by any production ecosystem
adapter — no adapter can ever emit a real nonzero test target

**Found by round VI task 38 (2026-09-03), while sizing §12.11 Task A's discriminator test.**
Verified free before allocating: form-agnostic sweep found `D111` as the highest allocated number.
Independently confirmed by task-scoped review before this entry was written: `grep -rn
"test_srcs=" src/` returns zero hits; the sole production `BuildUnit(...)` construction site
(`cli.py:8328` at review time — verify current line) never passes `test_srcs`; every ecosystem
adapter's `test_targets()` reads it via `test_sources()`, which is always empty.

**Consequence.** No path through `test_build_against_a_real_bazel`/`real_build()` (the codebase's
one existing unsandboxed real-bazel e2e test) can ever exercise a real nonzero `bazel test`
target — every real-bazel proof in this repo runs zero tests, not because the fixture repos have
none, but because nothing wires a `BuildUnit`'s discovered test sources into its constructor call.
Task 38's own §12.11 discriminator had to be proven directly against `BuildverifyWorker` with a
hand-built real-bazel workspace instead, sidestepping this gap rather than fixing it (a
disclosed, deliberate scope narrowing per Guardrail 6's "verify the resolved value" discipline —
not a silent workaround).

**Not required by §12.11's literal text** — `tests(//dest/...)`'s COUNT is what SPEC's own
sentence checks, and a real-bazel fixture's test target count can be proven nonzero another way
(as task 38's own discriminator did). This gap only blocks proving the discriminator through the
*existing* `real_build()` e2e path specifically, not the count-comparison mechanism itself.

**Not yet built:** whatever wiring would populate `test_sources()`/`test_srcs` from a real
ecosystem adapter's manifest scan — no design decision (which adapters, what discovery heuristic)
is made here.

**PARTLY ADDRESSED (round VI task 53, 2026-09-06, `018259b`) — Python only, JS/Rust/JVM still
untouched.** `_plan_build` (`cli.py`) now calls `_partition_test_srcs(facts.ecosystem, srcs)`
before constructing `BuildUnit`, which — for `Ecosystem.PYPI` only — splits `_dest_sources`'s walk
by pytest's own `test_*.py`/`*_test.py` discovery convention (`_is_python_test_src`) into
`(srcs, test_srcs)`; every other ecosystem gets `test_srcs=()` unchanged, exactly as this entry
originally described. Proven end to end and disclosed as narrowed, not closed: the fast covering
test `tests/test_build_e2e.py::test_a_python_repo_with_a_real_test_file_gets_a_real_py_test_target`
(revert-and-rerun RED/GREEN plus a mutation on `_is_python_test_src`, both in-worktree per Rule
12) and the real-Bazel `tests/test_build_e2e.py::
test_a_python_test_target_runs_and_passes_under_a_real_bazel` (`@pytest.mark.integration`) —
`bazel test //py/acme-widgets-py:acme-widgets-py_test` PASSES, the first real-Bazel nonzero
test-target result this codebase has ever produced through `real_build()`'s own path. **This does
NOT close D112** — JS/Rust/JVM adapters still never populate `test_srcs`, and no design decision
for their own test-file conventions has been made (see the still-current "Not yet built" paragraph
above, which remains true for those three). Do not round the `<n> of 48` §12 count up on this
account; §11's Task B (`docs/CRITERIA_PLAN.md`) is the criterion-closing work this unblocks, and
it is dispatched separately.

**PARTLY ADDRESSED, widened (round VI task 70, 2026-09-07) — JVM now covered; JS and Rust are NOT,
for a measured reason each, not a scoping choice; `Ecosystem.GO` needs no coverage at all — it is
structurally out of scope, not merely unaddressed.** `go.py`'s `generate_targets()`/
`test_targets()` return `[]` unconditionally: Gazelle generates `go_test` targets directly from
`*_test.go` files (the `uses_gazelle` adapter invariant), bypassing `BuildUnit.test_srcs` entirely
— there is no `test_srcs`-driven path for Go to populate. This closes D112's full membership
question: PyPI (done), Maven/Gradle (this task), Go (N/A by design) — JS and Rust remain the only
two genuinely open gaps, both for the measured reasons below.
`TEST_SRC_PARTITIONED_ECOSYSTEMS`
(`ecosystems/base.py`) now also carries `MAVEN`/`GRADLE` (one adapter, `jvm.py`), and
`cli._partition_test_srcs` dispatches to a new `cli._is_jvm_test_src` (the Maven Standard
Directory Layout's `src/test/{java,kotlin,scala}/` convention) via a small
`Mapping[str, Callable[[str], bool]]` table (`cli._TEST_SRC_PREDICATES`, keyed by
`Ecosystem.value` rather than by the member itself — see that table's own docstring for why:
§12.6/ADR-0100's textual sweep bans the literal member spelling anywhere in `cli.py`, not only in
a `Compare`/`Subscript`). Proven at the FakeBazel tier exactly as task 53 proved Python: the fast
covering test
`tests/test_build_e2e.py::test_a_jvm_repo_with_a_real_test_file_gets_a_real_java_test_target`
(revert-and-rerun RED/GREEN plus a mutation on `_is_jvm_test_src`'s path-prefix table, both
in-worktree per Rule 12) — `java_test(` renders with the right `srcs`/`deps`, and the test file is
correctly absent from `java_library`'s own `srcs`.

**The real-Bazel half of this proof was ATTEMPTED and could NOT be completed — not because of
this task's predicate logic, but because of a separate, pre-existing, measured defect that blocks
EVERY JVM repo's real-Bazel analysis, with or without a test file.** A
`test_a_jvm_test_target_runs_and_passes_under_a_real_bazel` test mirroring the Python one was
written, run against the real toolchain, and removed again after it failed with:

```
ERROR: error loading package 'java/com/acme/widgets': Unable to find package for
@@[unknown repo 'rules_java' requested from @@]//java:defs.bzl: The repository
'@@[unknown repo 'rules_java' requested from @@]' could not be resolved: No repository visible
as '@rules_java' from main repository.
```

i.e. `load("@rules_java//java:defs.bzl", "java_library")` — the load statement EVERY JVM
`BUILD.bazel` this adapter has ever rendered contains — cannot resolve, because the generated
`MODULE.bazel` never declares `bazel_dep(name = "rules_java", ...)` directly; `jvm.py`'s own
`toolchain_requirements()` docstring already names the underlying fact ("`rules_java`... is a
transitive `bazel_dep` of `rules_jvm_external` and is **not** a key in `build.ruleset_versions`"),
but under bzlmod a transitive dependency's repo is not visible to the root module by that route
alone. This reproduces on the PRE-EXISTING `acme-commons-java` fixture too (confirmed by the error
being about the `load()` itself, before any target-level analysis, and independent of `srcs`
content) — it is §24/§27's own audit row's "jvm is still zero" status (`docs/
INTEGRATION_HONESTY.md`'s shared D4/D6/D7 audit table, not `## D7`'s own dedicated entry, which
is exclusively about `js_binary`'s missing `deps` attribute), unresolved: **no generated JVM
package has ever been analysed by real Bazel in this codebase's history, this task's attempt
included.**
Fixing it means adding `rules_java` (some version) as an explicit `bazel_dep` in
`build.ruleset_versions`/`render_module_bazel` — a design decision (which version, whether it
also finally closes the JDK-toolchain gap `toolchain_requirements()` already discloses) outside
this task's scope, per its own "no new ADR/D-number" instruction. **Controller allocation:
`D121`** (verified free — max was `D120`; see `## D121`'s own entry below for the full account).

**JS (`NPM`) and Rust (`CARGO`) are measured, real Bazel-analysis-time blockers in each adapter's
OWN `test_targets()` — not merely an undecided file-naming convention** — see
`ecosystems.base.TEST_SRC_PARTITIONED_ECOSYSTEMS`'s docstring for the full citation of each:

* **Rust**: `rust.py::test_targets()` emits `rust_test(crate = ":<lib>", srcs = test_srcs, ...)`.
  `rules_rust`'s `_rust_test_impl` (`rust/private/rust.bzl`, read directly off the pinned
  `rules_rust@0.65.0` tag) hard-fails analysis the instant BOTH `crate` and a non-empty `srcs` are
  set: `"rust_test.crate and rust_test.srcs are mutually exclusive. Update <target> to use only
  one of these attributes"`. Since `test_srcs` has always been `()` for Rust, this line has never
  fired — widening `TEST_SRC_PARTITIONED_ECOSYSTEMS` to `CARGO` with any nonempty-producing
  predicate turns every matching Rust repo's build into a hard failure, in real Bazel, today.
* **JS**: `js.py::test_targets()` emits `js_test(srcs = test_srcs, deps = [f":{name}", ...], ...)`.
  This repeats, unfixed, the exact defect class `## D7` above already found and fixed in the
  SIBLING `js_binary` target one function up (`generate_targets()`): `js_binary`/`js_test` are
  rules_js *runtime* rules with **no `deps` attribute** — the fix there was to use `data` instead,
  and the fix's own comment says so. `test_targets()` was written with the pre-D7 `deps=` shape
  and has never been exercised (`test_srcs` has always been `()` for JS too), so it still carries
  the bug D7 fixed everywhere else.

Fixing either requires a design decision about how to reshape that adapter's `test_targets()` (a
separate `rust_test(srcs=[file], deps=[":lib"])` per Cargo integration-test file, rather than one
combined with `crate=`, for Rust; swapping `deps=` for `data=` — and verifying `js_test` needs
nothing else beyond that single-attribute fix, which was NOT verified here — for JS), which is
explicitly out of this task's scope (brief: no new ADR/D-number, land what can be proven). Neither
was attempted; both are left `test_srcs=()`-unchanged, exactly as before this task, so this is not
a regression. **Remaining under D112: JS and Rust's `test_targets()` methods need their own fix
before either can be added to `TEST_SRC_PARTITIONED_ECOSYSTEMS`; JVM's predicate/table wiring is
done and FakeBazel-proven, but its own real-Bazel proof needs the separate `rules_java`
`bazel_dep` fix flagged above first.** Do not round the `<n> of 48` §12 count up on this account
(same as task 53's note above).

**PARTLY ADDRESSED, widened further (round VI task 86, 2026-09-08) — Rust now covered; JS remains
the only genuinely open gap.** *(Superseded by round VI task 87's paragraph below, landed
concurrently on a sibling branch and merged after this one — JS closed too as of that paragraph;
left standing verbatim as the accurate record of what task 86 alone established.)* Re-verified the
constraint above fresh against this repo's actual
pin rather than trusting the citation: `settings.py:566` pins `rules_rust` at exactly `0.65.0`, and
`rust/private/rust.bzl` fetched directly from the `0.65.0` tag
(`https://raw.githubusercontent.com/bazelbuild/rules_rust/0.65.0/rust/private/rust.bzl`) confirms
`_rust_test_impl`'s `if ctx.attr.crate and ctx.attr.srcs: fail(...)` at line 352-353 verbatim as
task 70's paragraph above cites it. `rust.py::test_targets()` no longer combines `crate=` with a
non-empty `srcs=` on one target: the unconditional `crate = ":<lib>"` unit-test target is
unchanged (still `srcs=[]`, still covers `#[cfg(test)]` tests compiled inside the library), and
each file `cli._is_rust_test_src` matches (Cargo's own `tests/*.rs` convention — a `.rs` file
DIRECTLY under `tests/`, excluding nested helper modules like `tests/common/mod.rs` a top-level
test file `mod`-includes) now gets its OWN separate `rust_test(srcs=[file], deps=[":<lib>", ...])`
target. `Ecosystem.CARGO` is now a `TEST_SRC_PARTITIONED_ECOSYSTEMS` member.

Proven at both tiers this task's brief required, unlike JVM's still-incomplete real-Bazel half
above. FakeBazel tier: `tests/test_build_e2e.py::
test_a_rust_repo_with_a_real_test_file_gets_a_real_rust_test_target` — two separate `rust_test(`
blocks render (`body.count("rust_test(") == 2`), the `crate=` block carries no `srcs=`, the
integration-test block carries no `crate=`, and the nested `tests/common/mod.rs` helper gets no
target of its own and stays in `rust_library`'s `srcs` instead. Real-Bazel tier (the proof D112's
own text says a green Python-only assertion cannot give): a new fixture, `acme-widgets-rust`
(zero external crates, so no `crate.from_cargo`/`MODULE.bazel.lock` machinery is even invoked —
`workspace_deps()` returns `[]` when `external_coordinates` is empty), with a REAL `#[test] fn`
asserting a real value (not a bare script) in `tests/widgets.rs`. `bazel test
//rust/acme-widgets-rust:acme-widgets-rust_widgets_test` PASSES against the real toolchain —
`Traceback`-style checks aside, the test log shows the assertion actually ran, and a
`--test_output=errors` empty-stderr-on-a-failure shape would have caught a vacuous target. Both
tiers pass old-fails/new-passes: reverting `rust.py`/`base.py`/`cli.py` to their pre-fix state
(backup-file method, not `git stash`, per CLAUDE.md Rule 12) reproduces the pre-fix defect exactly
as the JVM/Python precedents' reports record it — the FakeBazel test's discriminating assertions
(`body.count("rust_test(") == 2`, `srcs = [` absent from the `crate=` block) fail RED against the
unmodified pre-fix adapter (which emits one combined `rust_test(crate=..., srcs=test_srcs, ...)`
block once `CARGO` is a `TEST_SRC_PARTITIONED_ECOSYSTEMS` member — which it was not, pre-fix,
making `test_srcs` empty and the assertions fail differently but still RED) and GREEN after.

**Remaining under D112, narrowed: JS only.** `js.py::test_targets()`'s `deps=` defect (the D7
regression cited above) is untouched by this task — out of scope, no attempt made, `test_srcs=()`
unchanged for `NPM`, not a regression. Do not round the `<n> of 48` §12 count up on this account
(same as task 53's/70's notes above) — this closes D112's Rust membership question but D112 itself
stays PARTLY ADDRESSED until JS lands too.

**JS (`NPM`) FIXED (round VI task 87).** The precedent above — "swapping `deps=` for `data=`...
verifying `js_test` needs nothing else beyond that single-attribute fix, which was NOT verified
here" — turned out NOT to transfer cleanly, exactly as flagged: fresh verification against the
pinned `aspect_rules_js@3.4.0` tag (`js/private/js_binary.bzl`, `js_test = rule(attrs = dict(
js_binary_lib.attrs, **{...}))`) found `js_test` has neither `deps` NOR `srcs` — the `srcs=`
defect is new, D7's `js_binary` fix never had to face it, because a `js_binary`'s entry point is
one of the UNIT's own `srcs` (already compiled by the unit's own `ts_project`), while a TEST file
is compiled by nothing (`unit.test_srcs` is disjoint from `unit.srcs`). `js.py::test_targets()`
now emits a second `ts_project` (`{name}_test_lib`, `testonly=True`) compiling the test source,
and a `js_test` naming its compiled output via `data=[f":{name}_test_lib"]` — no `deps=`/`srcs=`
at all. `declaration: True` is set on the test `ts_project` (matching the library one) not only
for consistency: `aspect_rules_ts@3.10.0`'s own `ts_project` macro auto-emits a hidden
`<name>_typecheck_test` `build_test` target whenever declaration emission is off, which pulled in
a C++ toolchain resolution (`@apple_support`) this fix's own analysis proof never asked for —
found live when a real `bazel build --nobuild` first failed on exactly that hidden target.
`TEST_SRC_PARTITIONED_ECOSYSTEMS` (`ecosystems/base.py`) now also carries `NPM`, dispatching to a
new `cli._is_js_test_src` (Jest's own default `testMatch` convention — `*.test.ts(x)`/
`*.spec.ts(x)` by basename, or anything under `__tests__/`) via `cli._TEST_SRC_PREDICATES`.

Proven at both tiers this entry's own precedent (task 53/70) established. **FakeBazel tier:**
`tests/test_build_e2e.py::test_a_js_repo_with_a_real_test_file_gets_a_real_js_test_target` —
old-fails/new-passes via the backup-file method (never `git stash`): pre-fix reproduces this
entry's own pre-fix shape exactly (no `js_test(` in the body, the test file swallowed into the
library `ts_project`'s own `srcs=[...]`); post-fix asserts a real `js_test(` plus a real
`{name}_test_lib` `ts_project`, neither `deps` nor `srcs` naming the `js_test` block, and the
library `ts_project`'s `srcs` no longer carrying the test file. **Real-Bazel tier (D7's own proof
shape, widened):** `tests/test_bazel.py::test_real_bazel_analyses_the_generated_js_test` — TWO
negative controls, not one (`deps=` re-spliced onto `js_test` fails real analysis with "no such
attribute 'deps' in 'js_test' rule"; `srcs=` re-spliced fails with "no such attribute 'srcs' in
'js_test' rule"), then the generated pair passes real `bazel build --nobuild` analysis and
`bazel query 'labels(data, //<dest>:widgets_test)'` resolves to the compiled test's `ts_project`
label, as Bazel itself resolved it — not as the adapter spelled it. `tests/test_bazel.py::
test_real_bazel_analyses_the_generated_js_binary` (D7's own test) re-run alongside it, unchanged,
confirming no regression to the sibling fix. `tests/test_ecosystems.py`'s JS unit test
(`test_js_maps_a_scoped_npm_package_to_ts_targets`) updated for the new two-target
`test_targets()` return shape (a `(ts_project, js_test)` tuple, not a one-element `js_test` alone)
and re-verified 90/90 in that file. Full fast-tier regression across every file this task touched
(`tests/test_build_e2e.py`, `tests/test_bazel.py`, `tests/test_ecosystems.py`, whole files, no
`-k`, `-m "not integration"`): **201 passed, 0 failed.** `ruff format`/`ruff check`/`mypy` (no
path arguments) all clean.

**Merge note (round VI, controller, 2026-09-08): this paragraph was drafted on a branch cut before
task 86's Rust fix landed, so its "Rust remains the ONLY open ecosystem slice" framing (accurate
of that branch alone) is corrected here rather than left to stand false.** Both `CARGO` and `NPM`
are now `TEST_SRC_PARTITIONED_ECOSYSTEMS` members — **every named ecosystem slice of D112 is now
closed.** JVM's own real-Bazel proof remains separately blocked on D121's `rules_java` `bazel_dep`
gap, as this entry already recorded above — that is the one genuinely open piece of D112 remaining.
Do not round the `<n> of 48` §12 count up on this account — §12.11's Task B (`docs/
CRITERIA_PLAN.md`) is the criterion-closing work this unblocks, tracked there.

**Correction, 2026-09-08 (round VI task 87 review, fix round 1).** The paragraph above describes
`cli._is_js_test_src` as matching "`*.test.ts(x)`/`*.spec.ts(x)` by basename, or anything under
`__tests__/`". That is what landed at `41369ad` and it was wrong twice; the sentence is left
standing as the record of what that commit did, and both defects are corrected in the fix commit
this paragraph heads. Neither was caught by that commit's own real-Bazel proof, and the reason is
worth keeping: `bazel build --nobuild` is **analysis only**, so a `js_test` naming a wrong-but-
existing label analyses green — the instrument could not move under either defect.

* **`__tests__/` matched any suffix, and a matched non-source file was then LOST.** It is the one
  predicate in `_TEST_SRC_PREDICATES` keyed on a directory rather than a basename, so unlike
  `_is_python_test_src` (always `.py`) it could match a file `JsAdapter` does not compile. Such a
  path left `unit.srcs` for `unit.test_srcs`, `test_sources()`'s `accepts_src` filter then dropped
  it, and `non_source_files()` — which reads `unit.srcs` alone — could no longer see it either.
  Measured both sides at `f45e75f` and `41369ad` over the same input: a Jest-default
  `__tests__/__snapshots__/x.snap` was carried in the library `ts_project`'s `data=` before and
  appeared **nowhere in the generated `BUILD.bazel`** after, against `base.py::non_source_files`'s
  own stated contract ("Refused files are carried, not dropped"). The clause now requires
  `.ts`/`.tsx`, which puts every refused file back in `srcs` where `non_source_files()` carries it.
* **`entry_point` took `test_srcs[0]`, which is a SORT, not a choice.** `package_relative` sorts
  and `_` (0x5F) precedes `s` (0x73), so in any repo with a `__tests__/` directory the entry point
  was deterministically the alphabetically-first file there — a helper, or a fixture. The observed
  artefact was `js_test(entry_point = "__tests__/fixture.json")`: green `bazel build`, a test
  target Node cannot start, which is the exact failure the pre-task-87 `test_targets()` docstring
  warned about and which that task's rewrite had deleted. `js.py::_test_entry_point` now requires
  a compiled `.ts(x)` and prefers a `*.test.*`/`*.spec.*` basename, and the warning is restored.

Proven by two new discriminators, each the unique discriminator of one of the above:
`tests/test_ecosystems.py::test_js_picks_the_test_file_as_the_entry_point_not_the_first_path`
(four cases, one per mutation, including the `.json`-first case `--nobuild` cannot see), and the
`__tests__/helper.ts` + `__tests__/__snapshots__/index.test.ts.snap` pair added to
`test_build_e2e.py`'s `acme-widgets-ts` fixture, which makes
`test_a_js_repo_with_a_real_test_file_gets_a_real_js_test_target` fail on both defects. **This
correction does not change the `<n> of 48` §12 count either way.**

## D113 — OPEN. §12.34 Clause B (`ContractBindingUnavailable`/`unbound_contract_kinds`) needs a
design leg before any dispatch — bigger than first estimated, one live blocker found

**Found by research-23 (2026-09-03), sized further by research-25 (2026-09-03).** Verified free
before allocating: form-agnostic sweep found `D112` as the highest allocated number.

**The gap, as measured.** A hoisted `ContractNode` has zero Phase 3 existence today beyond a
`wave_members` row: `cli.py:_eligible_build_units`'s `node_kind = 'REPO'` filter excludes CONTRACT
rows by construction, so no `phases` row, ingest, `BuildPlan`, or dispatch is ever created for a
contract — `_build_impl` never queries the `contracts` table. Making
`BuildPlan.unbound_contract_kinds` real needs standing up a real slice of Phase 3 pipeline for
contract nodes, not one lookup bolted onto `buildgen.py`'s existing per-repo loop.

**A live, currently-blocking defect, not just missing wiring.** SPEC §13 row 31's own emission
pseudocode opens with `contracts.for_kind(contract.kind)`, which requires `contracts.discover()`
to have already run — but `contracts.base.discover()` unconditionally raises `RuntimeError` today
because `avro.py`/`thrift.py` are unshipped (§12.32/§12.47 territory) and it asserts total
bijection over the whole `ContractKind` enum regardless of which kind triggered the call. Wiring
Clause B "the obvious way" (eager `discover()` at Phase 3 startup, mirroring
`ecosystems.base.discover()`'s pattern) would make every run containing so much as one hoisted
contract of ANY kind — including a fully-bound one — fail loudly, directly contradicting the
criterion's own "and the run still completes" clause.

**Judgment calls a design leg must resolve before dispatch (none picked here):**
1. How to populate the contract registry without tripping the bijection assert — block Clause B on
   §12.32/§12.47 landing first, or bypass `discover()` for this call site with a scoped direct
   import of only the kinds a run's actual contracts need (the latter is research-25's own Agent
   Recommendation, not adjudicated).
2. How Phase 3's domain grows to admit CONTRACT-kind wave members — full contract ingest/worktree/
   publish machinery, or a narrower post-pass reading already-committed contracts and persisting
   their `BuildPlan`s without solving "does a hoisted contract's own `BUILD.bazel` get published."
   Research-25 flags the narrower reading as possibly one-shot once explicitly chosen.
3. Which failures are allowed to become a `ContractBindingUnavailable` finding (a missing binding
   — the criterion's whole point) versus which must still fail loud (a missing adapter for a real,
   in-use kind — the guardrail against silently softening judgment call 1's blocker into a no-op).

Full findings: `.superpowers/sdd/round-V-criteria-closure/research-23-report.md` (Q5),
`.superpowers/sdd/round-V-criteria-closure/research-25-report.md` (full sizing).

**Not yet built:** any resolution to the three judgment calls above, the Phase 3 domain expansion,
or the emission wiring itself. No design choice among them is made here.

**Update, round VI research-33 (2026-09-06) — all three judgment calls decided (`ADR-0119`); this
entry's "live, currently-blocking defect" paragraph above is STALE, not rewritten (annotated in
place per this project's "annotate, never rewrite" discipline).** `contracts.base.discover()` does
**not** unconditionally raise at `HEAD` (`41929a2`) — round VI task 40 (`f3c0200`), landed after
this entry was written, shipped `avro.py`/`thrift.py`, and `discover()`'s bijection over
`ContractKind` is measured total (all five kinds, `set(_BY_KIND) == set(ContractKind)`). Judgment
call 1 has no live premise: call `discover()` the documented way, lazily, only when the run has a
contract build unit. Judgment call 2 is decided as a narrow read-only PASS 2b in
`cli.py::_build_impl` (not full ingest — nothing in `src/` commits hoisted contract content, so a
published `BUILD.bazel` would name sources that don't exist in any worktree). Judgment call 3 is
decided as: a missing binding is a finding (`severity='warn'`, run continues); a missing adapter,
an unresolvable node, or a `HOISTED` row with no `hoist_target_path` raises unhandled, with no
`try`/`except` softening. Full account and the decided design: `ADR-0119`
(`docs/DECISIONS.md`). The build is sized one-shot; task brief at
`.superpowers/sdd/round-VI-criteria-closure/task-56-brief.md`. **D113 stays OPEN — decided is not
built** — this update records the design decision only; §12.34 does not move toward DONE until
the pass in `ADR-0119` actually lands and is reviewed.

## D117 — FIXED, LANDED (round VI task 65, `6f74ea9`, task-scoped review). SPEC's and the
`DependencyEdge` model's own claim that `edges.retargeted_from_repo_id` makes a contract un-hoist
"exact" is false as written — the column cannot reconstruct a REPO-dst edge

**Found by round VI research-32 (2026-09-06), while decomposing §12.31/D111 into worker-ready
legs.** Verified free before allocating: form-agnostic sweep found `D115` as the highest allocated
number on `main` at dispatch time; `D116` was independently and concurrently allocated by round VI
task 54's own worktree for an unrelated §12.11 finding — both numbers stand, this is `D117`, next
free after reconciling both lanes' claims at merge time (CLAUDE.md's Central Number Allocation
guardrail: the orchestrator reconciles a collision at the moment it's discovered, not by
re-deriving from a stale prose count).

**The claim, as written.** `docs/SPEC.md:598-600`: "restore every affected edge from
`edges.retargeted_from_repo_id` — that column exists for exactly this and makes the un-hoist
**exact** rather than a re-inference." `src/fleet/models/graph.py:172-176` repeats it: "The
rollback record: restoring it un-hoists the edge exactly."

**Measured against `_materialize`** (`src/fleet/graph/cycles.py:684-742`): the retarget's
`model_copy(update=…)` (`:729-739`) overwrites six fields on the pre-hoist edge and the
`retargeted_from_repo_id` column recovers only one of them (`dst_id`, via the owner repo_id it
carries). `dst_coordinate` becomes `None` (unrecoverable) and `kind` becomes `CONTRACT_CONSUME`
(unrecoverable — the original `DECLARED_DEP`/`INTERNAL_IMPORT`/`API_CONTRACT`/etc. value is not
stored anywhere). The loss is structurally fatal, not merely lossy:
`DependencyEdge._node_shape` (`models/graph.py:198-210`) raises `"a REPO dst edge must carry
dst_coordinate"` on any attempt to reconstruct a REPO-dst edge from a persisted retargeted row
alone — a `ValidationError`, not a wrong-but-valid edge.

**Why nothing is broken today.** `grep -rn "DELETE FROM edges" src/fleet/` returns zero hits in the
whole codebase; `_persist_contract_edges` (moved repeatedly by round VI tasks 65, 66, and 67's
`cli.py` insertions) only INSERTs. The pre-hoist repo→repo
row is therefore still present in the `edges` table, untouched, alongside the new contract row. A
durable, exact un-hoist IS achievable today — by deleting the contract-kind rows and letting the
pre-existing repo→repo row stand — just not by the mechanism SPEC's own prose describes.
`retargeted_from_repo_id` is a record of what was retargeted, not a recipe for undoing it.

**Consequence, scoped.** Does not block §12.31/D111 Leg A (in-memory 6c-H rollback, never persists
the edge in the first place, so this gap is moot for it — see `task-55-brief.md`). It DOES matter
for Legs C/D (which un-hoist across process boundaries, from a DB, per D111's own leg breakdown)
and for SPEC's own narrative accuracy — a future Leg C/D worker who trusts `SPEC.md:598-600`
literally will build a reconstruction path that raises `ValidationError` on first real use.

**Not yet built/decided:** whether Leg C/D should un-hoist via a preserved repo→repo row (delete
contract rows, no reconstruction — matches what already works) or via some other mechanism; whether
`SPEC.md:598-600`/`models/graph.py:172-176`'s "exact" language should be corrected now or left for
the Leg C/D design pass to correct alongside its own build. No design choice is made here.

**Update, 2026-09-06 (round VI, D111 Leg D design pass, ADR-0122): the remedy choice above is now
decided.** Neither of D117's own two named remedy options is what got chosen: the mechanism is
**exclusion at graph-build read time**, not a preserved-row-plus-`DELETE` and not reconstruction.
`_graph_edges` (moved repeatedly by round VI tasks 65, 66, and 67's `cli.py` insertions — task 65
is the SAME task that landed this widening in code, see D111's own dated update above) is widened
with the identical `contracts.status IN ('HOISTED','MIGRATED')` predicate `_graph_nodes` already
applies, so a `FAILED`/`REJECTED` contract's edge rows are simply never read into the next graph
build — the untouched pre-hoist repo→repo row stands in their place, and nothing is ever deleted
or reconstructed. This also surfaced a previously-unknown correctness gap this entry's own
investigation did not find:
`_graph_edges` had **no** filter symmetric to `_graph_nodes`'s, so setting `contracts.status =
'FAILED'` and leaving edges alone (D117's own remedy option (a), taken literally, minus the
`DELETE`) would crash the very next `fleet sequence` with a `GraphError` on the vanished contract
node. Full account: `docs/DECISIONS.md` ADR-0122. **This heading stays `OPEN`** — an ADR is a
design decision, not the fix; it moves once Leg D's code (task-65-brief.md and its
not-yet-written successors) actually lands the mechanism.

**Correction, 2026-09-07 (task-65 fix round, controller review, I2): the sentence immediately
above is now stale — task-65's own commit (`6f74ea9`) IS the code landing this paragraph said to
wait for.** D117's own narrow scope (the `_graph_edges` reconstruction claim) is closed by that
commit; the heading above is flipped to `FIXED, LANDED`. What remains genuinely open belongs to
`D111` (Leg D as a whole — Legs C1/C2, the git revert-series execution, and the production
wiring), not to D117 specifically; see `D111`'s own dated update for that residue. Kept here as
the record of what was true when this paragraph was written, not rewritten, per this file's own
history-preservation convention.

## D114 — FIXED, LANDED (round VI task 42, `79024e6`) for §9(d). No path/blob-SHA `ls-tree`
listing is ever captured or persisted at scan time — blocked §9(d); did NOT block §12.27 as
originally framed below (correction dated 2026-09-03, see end of entry)

**Found by round VI research-28 (2026-09-03), sizing the shared blocker named — but never
D-numbered — by both §9's and §12.27's own `docs/CRITERIA_PLAN.md` entries.** Verified free
before allocating: form-agnostic sweep found `D113` as the highest allocated number.

**The gap, as measured.** Three independent modules' docstrings (`workers/contracts.py`,
`graph/sequence.py`, `graph/collisions.py`) all describe the identical missing mechanism in
near-identical language, and all three already have consumer code written against it
(`ContractsInput.blob_shas`, `check_criterion_d`'s injected `evidence_exists` callback,
`collisions.py`'s `FileClaim`/`_file_collisions`) — but nothing in `src/` ever runs `git ls-tree`
against a repo's full tree at `head_sha` and persists the result. `workers/clone.py` computes
`head_sha` and cuts the worktree every later step reads from, but never lists it. No existing
table is a superset of "every tracked file at `head_sha`" (`manifests` is manifest-files-only,
`symbols` is symbol-occurrences-only).

**Unusually well-prepared ground, not a speculative design.** The consuming interface is already
frozen and agreed in three places: `check_criterion_d` is fully implemented and takes the capture
as an injected callable (`graph/sequence.py:566-586`), and `graph/collisions.py`'s FILE_PATH
detector (`FileClaim`, `_file_collisions`) is fully implemented and simply never invoked with real
data. In every case the gap is real production data never being computed, not missing algorithm
code.

**Sizing, three separable pieces (full design in
`.superpowers/sdd/round-V-criteria-closure/research-28-report.md`):**
- (a) the capture mechanism itself — a new `file_blobs` table + one `git ls-tree -r -z <head_sha>`
  call per already-cloned repo, reusing `_git_output`/`_nul_fields` verbatim (already used
  identically for `_tracked_at`'s Phase-3 `ls-tree`).
- (b) wiring §9(d)'s `check_criterion_d` to consume it — zero changes to `graph/sequence.py`
  itself; a single query + lambda in `_phase1_exit_report`.
- (c) wiring §12.27's FILE_PATH collision leg to consume it — a real 3-table join (`repos.dest_path`
  + the new table + `contracts.source_paths`) plus a `wave_plan` lookup, reusing
  `relocate.py::relocated_path` and the already-complete `_file_collisions`/`FileClaim` machinery.

**(a)+(b) together close §9(d) independently of (c)** — mirroring the §12.34 Clause A/Clause B
split. (c) is a separate, larger follow-on task, dispatched only after (a) lands.

**Not yet built:** any of (a)/(b)/(c). No design choice among them beyond what research-28's report
already specifies is made here.

**Correction, 2026-09-03 (round VI task 43's own investigation) — piece (c)'s premise was wrong;
this entry's framing above is left as-is per this project's "annotate, never rewrite" discipline,
corrected here rather than edited in place.** §12.27's SPEC.md text (`docs/SPEC.md:7459`) was
already corrected on 2026-08-28 — before this D-number was even allocated — to retire FILE_PATH
from the criterion's literal text entirely, not merely defer it pending a capture mechanism.
`docs/CRITERIA_PLAN.md`'s own §12.27 entry had gone stale in the same way (describing FILE_PATH as
a legitimately blocked leg since 2026-08-30), and this D-number's own piece-(c) framing inherited
that stale premise without an independent check against SPEC.md's primary text. Beyond the
data-availability question piece (a) answers, SPEC's own correction gives a SECOND, structural
reason FILE_PATH can never satisfy §12.27's exit-6 clause: `_file_collisions` sets a `resolution`
on every branch, so a FILE_PATH row can never be `blocking` regardless of whether real data
reaches it — task 43 confirmed this empirically (a genuine `severity='error'` FILE_PATH collision
still exits 0 with real data wired through). **Piece (a)+(b) closing §9(d) is unaffected and
remains correctly landed. Piece (c) is retired, not merely deferred — do not dispatch it.** Full
account in `docs/CRITERIA_PLAN.md`'s §27 entry.

## D116 — OPEN. Nothing in `src/fleet/` ever writes `repos.baseline_ok` or
`repos.baseline_test_count` — under the shipped default config, §12.11's last sentence's
"exclusion set" is the WHOLE fleet, not the empty set it requires

**Found by round VI task 54 (2026-09-06), while proving §12.11's third and last disclosed gap
(`docs/CRITERIA_PLAN.md`, criterion 11, "gap 3", itself first named by round VI task 38's own
audit).** Verified free before allocating: form-agnostic sweep (`grep -oE '\bD[0-9]+\b'
docs/*.md`) found `D115` as the highest allocated number.

**The gap, as measured.** SPEC §12.11's literal last sentence (`docs/SPEC.md:7447`): "Repos with
`baseline_ok IS NULL` (baseline never measured, e.g. `preflight.baseline_build.enabled: false` or
a pre-schema-7 run) are excluded from the count assertion, not silently passed: the fixture run
asserts the exclusion set is empty under the shipped config." `tests/test_baseline_ok_exclusion.
py::test_the_baseline_ok_exclusion_set_is_empty_under_the_shipped_config` drives the real CLI
(`scan -> sequence -> transform -> build`, `tests/test_transform_e2e.py`'s five-repo fixture, no
`preflight.baseline_build.enabled: false` override anywhere in the fixture's `config/fleet.yaml`
— i.e. genuinely "the shipped config", since `BaselineBuild.enabled`'s pydantic default is `True`,
`settings.py:282`) and reads `repos.baseline_ok` back from the real database `fleet build` wrote
to. Measured result: **every** fixture repo's `baseline_ok` is NULL after a real build — the
exclusion set is the entire fleet, not empty. Root cause, independently confirmed by two
form-agnostic sweeps: zero sites anywhere in `src/fleet/` ever `UPDATE`/`INSERT` `repos.
baseline_ok` or `repos.baseline_test_count` (both columns are read in several places —
`_RepoFacts.baseline_ok`/`baseline_test_count` in `cli.py`, the `SELECT` at `cli.py:7368`,
`graph/sequence.py::_exemptions_for`'s `baseline_ok.get(repo_id) == 0` — and written nowhere), and
`BaselineBuild.enabled` (`settings.py:282`) itself is read nowhere outside `settings.py` (`grep
-rn "baseline_build" src/fleet/` returns only its own declaration at `settings.py:295`). There is
no production code path, gated on the config flag or otherwise, that ever runs the native
baseline build §3.1/§9's own comments (`state/schema.sql:87`'s `baseline_ok` column comment,
`settings.py:280`'s `BaselineBuild` docstring: "the native build/test gate §14.1 claims to have")
describe.

**Consequence.** §12.11's own count-comparison half (`bazel query 'tests(//<dest>/...)' | wc -l`
`>= repos.baseline_test_count` for every `baseline_ok = 1` repo) is structurally unreachable for
any repo in any real run — `baseline_ok` never becomes `1` (or `0`) for anyone, so the guarded
comparison this criterion's main clause exists to run never executes, and the sentence guarding
its blind spot ("the exclusion set is empty") is currently false rather than vacuously satisfied.
This is a strictly larger gap than Task A's gap 1 (`D112`, `BuildUnit.test_srcs` never populated —
that blocks a NONZERO test count from ever reaching the comparison) and gap 2 (`migrated_test_
count` persistence, closed round VI task 47): those two assume `baseline_ok`/`baseline_test_count`
themselves get written, which this entry shows they never do.

**Not required by this task to fix** (round VI task 54 was TEST-ONLY, no production code
change): `tests/test_baseline_ok_exclusion.py`'s first test pins the target state as a
`strict=True` xfail so a future fix's own correctness self-proves by making the xfail an
unexpected pass (CI failure) until the marker is deleted; its second test proves the assertion
helper is a genuine, non-tautological `repos.baseline_ok IS NULL` read against real DB state
(three-step direct-DB-write discriminator, since the pipeline's own natural state is already the
"red" case with no seed needed — see that test's docstring for why the task brief's literal
"seed a NULL, confirm red, remove it, confirm green" recipe had to be inverted).

**Not yet built:** the native baseline build measurement itself — what repo kinds it can run
against, how it maps to `baseline_ok`/`baseline_test_count`, and where in Phase 1/Phase 3 it
belongs. No design choice is made here.

## D118 — FIXED, LANDED (round VI task 57, `3348935`+`faec3f3`, merged; task-scoped review
Approved both waves). `BuildverifyWorker._test_query_argv` passed the SANDBOXED
`--repository_cache`/`--disk_cache` flag values to a query that always runs on the HOST, breaking
§12.11's real test-count comparison for every sandboxed build

**Found by round VI task 57 (2026-09-06), while building §12.11 Task B's sandboxed combination
fixture** (`docs/CRITERIA_PLAN.md` §11, `research-24-report.md`) — the first time this codebase
ever drove a real `--network=none` `bazel test` of a genuinely non-vacuous Python target (D112,
round VI task 53) with `repos.baseline_ok = 1` seeded (D116 is why that seeding is direct rather
than through the shipped pipeline; disclosed in this task's own report, not this entry's concern).
Verified free before allocating: form-agnostic sweep (`grep -oE '\bD[0-9]+\b' docs/*.md`) found
`D117` as the highest allocated number.

**The bug, as measured.** `_test_query_argv`'s own docstring states the query is "Host-only,
deliberately" — it is never wrapped in `docker_run_argv`, always `payload.bazel_bin` invoked
directly by the worker's `runner()`. But its cache-flag construction read
`cache.flag(sandboxed=payload.image is not None)` — the SAME expression `_bazel_argv` correctly
uses for the build/test steps, which genuinely do run inside the container when `payload.image`
is set. Copied without adjusting for the one caller that must always answer `sandboxed=False`.
Under a sandboxed `payload` (`image` set, exactly this criterion's own scenario), this emitted
`--repository_cache=/cache/repos`/`--disk_cache=/cache/disk` — the CONTAINER-side mount targets —
for a `bazel query` process running on the HOST filesystem, where `/cache` does not exist.
Measured directly, twice, against the real fixture before the fix (`docker` daemon on host,
`fleet-build:9.2.0-bookworm` image, `--network=none` build+test steps both real and both exit 0):
`bazel query` failed with `ERROR: [unix_jni.cc:471] /cache (Permission denied)` /
`ERROR: could not acquire lock on repo contents cache`, taking the whole `buildverify` step down
as a `BUILD_ERROR` before `migrated_test_count` was ever measured — silently, since neither the
build nor the test step (the ones an operator's first instinct is to inspect) show any failure.
`CacheMount.flag`'s own docstring (`src/fleet/workers/buildverify.py`) names this exact class of
bug ahead of time ("Sandboxed and unsandboxed are different paths and getting it backwards is
silent... Neither fails loudly"); this was that bug, on the one caller whose answer is always
`False`.

**Consequence.** Every sandboxed `fleet build` of a repo with `repos.baseline_ok = 1` hit this —
not a narrow fixture-only case. §12.11's test-count comparison (the sentence D116 already showed
was structurally unreachable under the shipped default config for a DIFFERENT reason) was ALSO
broken on the one path — sandboxed, `baseline_ok` seeded or otherwise made `True` — where D116's
gap does not apply. The two gaps are independent and this task found the second one only because
fixing/seeding around D116 for one fixture repo was enough to reach it for the first time.

**The fix, on this branch.** `_test_query_argv` now calls `cache.flag(sandboxed=False)`
unconditionally, matching its own docstring's stated intent — the query always runs host-side
regardless of whether the payload's build/test steps are sandboxed. Verified end to end, real
Docker/Bazel, `tests/test_build_e2e.py::
test_a_real_bazel_lock_publish_and_a_real_sandboxed_build_happen_in_the_same_run`: PRE-fix, the
exact `/cache (Permission denied)` error above, reproduced twice; POST-fix, the same fixture's
sandboxed build of `task57-happy-py` (`baseline_test_count = 1`, real migrated count 1) succeeds
end to end, and a second, deliberately-shrunk fixture (`task57-shrink-py`, `baseline_test_count =
99`, real migrated count 1) correctly reaches `TEST_FAILURE`/`REQUIRES_HUMAN_INTERVENTION` via the
real `test_count_regressed` comparison — not via `no_test_targets`/`tests_lost`, which stays green
throughout (both real `docker run --network=none` build/test steps exit 0), exactly the
discriminator this criterion's own `tests_query` mechanism (Task A, round VI task 38/53) exists to
prove. Test passed 3/3 consecutive real runs. `mypy`/`ruff` clean; `tests/test_workers_build.py`
(87/87) and `tests/test_build_e2e.py -m "not integration"` (51/51) show no regression from the
`sandboxed=False` change.

**Also touched, same task:** `docker/fleet-build.Dockerfile` now installs the full `python3`
package (not `-minimal`, which measurably lacked the stdlib `uuid` module `rules_python`'s own
`py_test`/`py_binary` bootstrap stub imports) — a separate, pre-existing infrastructure gap this
task's fixture surfaced first (the image never had ANY `python3`, so this codebase had never
before run a real `py_test` under this sandbox at all), fixed and disclosed in the Dockerfile's
own comments rather than in this entry, since it is not a `src/fleet/` defect.

**Fix-round addendum (2026-09-06, controller review) — offline regression coverage added.** The
real-Docker e2e test above is the only proof reachable on a host with Docker+Bazel; a host
without either has zero coverage of this fix. Added
`tests/test_workers_build.py::test_the_test_query_uses_host_side_cache_paths_even_under_a_sandboxed_payload`
— a unit-level test (`RecordingRunner`, no subprocess, no Docker, no real Bazel binary) that
constructs a sandboxed (`image` set) payload and asserts `_test_query_argv` emits host-side, not
container-side, cache paths. Mutation-proven: reverting to `sandboxed=payload.image is not None`
flips both cache flags to the container-side paths this bug used to emit, reddening the new test;
task-scoped re-review independently traced this mutation rather than trusting the report.

## D119 — FIXED, LANDED (round VI task 61, `2d19310`, task-scoped review Approved with one
report-arithmetic correction). `tests/test_ecosystems_contracts_base.py`'s autouse teardown leaves the contracts
registry unrecoverably empty for the rest of a realistic pytest session, and `fleet build`'s PASS
2b now depends on it being populated

**Found by round VI research-34 (2026-09-06), re-auditing §12.32's DONE marking.** Verified free
before allocating: form-agnostic sweep found `D118` as the highest allocated number.

**The gap, as measured.** `src/fleet/ecosystems/contracts/base.py:136-153`: `discover(force=False)`
calls `importlib.import_module(...)` per adapter module, which for a module already in
`sys.modules` returns the cached module without re-running its `@register` decorators — so
`_BY_KIND` stays empty and `discover()` raises. `tests/test_ecosystems_contracts_base.py:33-39`'s
autouse `_clean_registry` fixture tears down with a bare `reset_adapters()` and never restores
(unlike the landed precedent for the sibling registry, `tests/test_ecosystems.py:41-51`, whose
teardown follows `reset_adapters()` with `discover(force=True)` specifically to prevent this class
of cross-test contamination). `src/fleet/cli.py:9527` — `fleet build`'s PASS 2b, landed this same
session by round VI task 56 (`57d7171`) — calls a plain `discover()` with no `force=`, and is the
first in-tree caller; before task 56 this fixture's omission had no observable consequence.

**Reproduced two ways.** In isolation: `pytest tests/test_ecosystems_contracts_base.py
"tests/test_new_language_touchpoints_e2e.py::test_contract_binding_unavailable_finding_and_unbound_contract_kinds_end_to_end"`
is GREEN alone (adapter modules never previously imported, so `discover()`'s own `import_module`
call really executes them); adding the five `tests/test_ecosystems_contracts_{avro,base,openapi,
proto,shared_lib,thrift}.py` files ahead of it (which import all five adapter modules at
collection) turns it RED with `RuntimeError: no ContractAdapter is registered for [...]`. **A full,
unfiltered `pytest tests/` run at `6844a04` confirms this is not a narrow-selection artefact: 6
failed, 2384 passed, 1 xfailed, 2 errors in 1141.77s, including this exact failure** — `main` was
red on this test at that commit.

**Fix recommended (Agent Recommendation, TEST-ONLY):** restore the fixture teardown to match
`tests/test_ecosystems.py`'s own landed precedent — `reset_adapters(); yield; reset_adapters();
discover(force=True)`. Rejected alternative: making `discover()` reload whenever `_BY_KIND` is
empty even without `force=True` — this would change production semantics of a registry function to
paper over a test hook's own misuse (Rule 2/3), not recommended.

**Not yet built:** the fix. Dispatched as
`.superpowers/sdd/round-VI-criteria-closure/task-61-brief.md`, bundled with the still-missing
§12.32 bijection test and one stale docstring in the same file.

## D120 — FIXED, LANDED (round VI task 62, `1996e44`, task-scoped review Approved zero blocking
findings). §12.6(a)'s "no branch outside the adapter packages" invariant is violated by two
already-landed, deliberate changes this same session — `tests/test_ecosystems.py` has 3 live,
deterministic reds on `main`

**Found by round VI research-34 (2026-09-06), as a side effect of the same full-suite run that
found `D119`.** Verified free before allocating: form-agnostic sweep found `D119` as the highest
allocated number (allocated immediately above, same investigation).

**The gap, as measured.** `tests/test_ecosystems.py`'s §12.6(a)/(b) enforcement tests (a regex
line-scan plus an AST walk over `ast.Compare`/`ast.Subscript` nodes, `_no_ecosystem_branch...`/
`_no_bare_compare_or_subscript_names_a_kind_member...`) assert, with — per the AST test's own
docstring — "no allowance whatsoever" (the one documented exception, ADR-0100, is a `Subscript`
whose base is a module-scope dict-literal table in the same file), that no `Ecosystem` or
`ContractKind` member is named in a branch or bare compare/subscript anywhere outside
`src/fleet/manifests/`/`src/fleet/ecosystems/`. Two commits landed this same session violate this
directly:
- `src/fleet/cli.py:8834` (`if ecosystem is not Ecosystem.PYPI:`, inside `_partition_test_srcs`,
  round VI task 53, D112's Python-only scoping) — the comment at `:8827` names the site itself.
- `src/fleet/cli.py:9529` (`cnode.kind is ContractKind.SHARED_LIB`, round VI task 56's `fleet
  build` PASS 2b, ADR-0119's deliberate `SHARED_LIB` carve-out).

Reproduced in isolation, deterministically, in 1.8s:
`pytest tests/test_ecosystems.py -k "no_ecosystem_branch or no_ecosystem_member_other or
no_bare_compare"` → `3 failed, 87 deselected`. Not an ordering artefact — unlike `D119`, this
reproduces standalone.

**Why this is a real defect, not a false positive.** §12.6's own stated purpose (per the AST
test's docstring) is that adding a language must be a file-drop, and precedent exists for treating
exactly this shape as a genuine bug rather than an acceptable exception: ADR-0100 itself was
adjudicated after `workers/contracts.py:758`'s `if kind is ContractKind.OPENAPI:` was found
violating the identical rule and was fixed, not exempted. Both D112's and ADR-0119's own design
work were done without checking this pre-existing invariant test, and neither commit's own review
caught it (the covering test files each task ran did not include `tests/test_ecosystems.py`).

**Not yet built:** the fix. ADR-0100's own precedent (a module-scope dict/set-literal table,
looked up rather than branched on) is the natural pattern for both sites — e.g. a
`_PYTHON_ONLY_TEST_PARTITION: Final[frozenset[Ecosystem]]`-style table for D112's site and an
equivalent named table for the `SHARED_LIB` skip — but no design choice is made here, and neither
site should be patched without also re-confirming the fix doesn't reintroduce a second source of
truth (the same class of defect task 55's fix round caught in a different location this session).
No task briefed yet; this is the controller's next dispatch.

**Fixed (2026-09-06, round VI task 62, `1996e44`).** Both sites now match ADR-0100's own precedent
exactly: `_partition_test_srcs`'s `Ecosystem.PYPI` branch became `ecosystem not in
TEST_SRC_PARTITIONED_ECOSYSTEMS` (a new `Final[frozenset[Ecosystem]]` table in
`src/fleet/ecosystems/base.py` — an adapter package, required because a separate textual regex
test bans any `Ecosystem.<member>` mention anywhere outside the adapter packages, even inside a
table/docstring, with no equivalent regex for `ContractKind`); PASS 2b's `ContractKind.SHARED_LIB`
skip became `cnode.kind in _BUILD_PASS_2B_SKIPPED_KINDS` (a new `Final[frozenset[ContractKind]]`
table defined in `cli.py` itself, permissible since no such regex constrains `ContractKind`).
Behavior preservation proven by an exhaustive equivalence check over both enums' full membership
(7 `Ecosystem` + 5 `ContractKind` members), independently re-derived by task-scoped review plus a
genuine mutation test (emptying each table at runtime and confirming membership behavior actually
changes). `tests/test_ecosystems.py` — 90/90 passing (was 87 pass / 3 fail).

## D121 — FIXED, LANDED (round VI task 73, `d9dd533`, controller review pending). No generated
JVM package has ever been analysed by real Bazel: the generated `MODULE.bazel` never declares
`rules_java` as an explicit `bazel_dep`

**Found by round VI task 70 (2026-09-07), while attempting the real-Bazel half of D112's JVM
test-source proof.** Verified free before allocating: form-agnostic sweep found `D120` as the
highest allocated number.

**The gap, as measured.** Every JVM `BUILD.bazel` this adapter has ever rendered contains
`load("@rules_java//java:defs.bzl", "java_library")` (or `java_test`). A real-Bazel run against a
generated JVM package — `acme-commons-java`, a pre-existing fixture, not one this task added —
fails before any target-level analysis even starts:
```
ERROR: error loading package 'java/com/acme/widgets': Unable to find package for
@@[unknown repo 'rules_java' requested from @@]//java:defs.bzl: The repository
'@@[unknown repo 'rules_java' requested from @@]' could not be resolved: No repository visible
as '@rules_java' from main repository.
```
`jvm.py`'s own `toolchain_requirements()` docstring already names the underlying fact:
`rules_java` is a transitive `bazel_dep` of `rules_jvm_external`, not a key in
`build.ruleset_versions`. Under bzlmod, a transitive dependency's repo is not visible to the root
module by that route alone — the generated `MODULE.bazel` needs its own explicit
`bazel_dep(name = "rules_java", ...)` line, which nothing in `build.ruleset_versions`/
`render_module_bazel` ever writes.

**Consequence.** This is not narrow to test-source population — it blocks ANY real-Bazel
analysis of ANY generated JVM package, with or without a test file, independent of this task's
own `_is_jvm_test_src` predicate (confirmed: the error is at package-load time, before `srcs`
content is ever read). It is the concrete, previously-unmeasured root cause behind this file's
own long-standing "jvm is still zero" real-Bazel-analysis status note (§24/§27's own audit row,
`grep -n "jvm is still zero"`).

**Not yet built:** the fix — add `rules_java` (some pinned version) as an explicit `bazel_dep` in
`build.ruleset_versions`/`render_module_bazel`. Sizing/design note: whether the same fix should
also close the JDK-toolchain gap `jvm.py::toolchain_requirements()`'s own docstring already
discloses is a design choice, not a re-derivation from this task's own measurement — a future
task should check both possibilities before deciding scope. No task briefed yet; this is the
controller's next dispatch candidate.

**Fixed (2026-09-07, round VI task 73, `d9dd533`, controller review pending).** Pinned
`"rules_java": "9.1.0"` in `BuildSection.ruleset_versions` (`src/fleet/settings.py`) — measured,
not guessed, against this repo's real, pinned Bazel (9.2.0): `rules_jvm_external@6.7`'s own floor
(`7.12.2`) and `8.6.1` both fail with `name 'JavaInfo' is not defined` / `JavaPluginInfo`
(`java/private/native.bzl`), the same class of Bazel-9 native-symbol-removal breakage already
documented for `rules_rust`/`rules_go`/`gazelle`/`aspect_rules_ts`; `9.1.0` loads and builds
cleanly combined with all 8 pre-existing pins. No change to `generators.py` or
`jvm.py::toolchain_requirements()` (still `return []`, unchanged docstring) — `render_module_bazel`'s
pre-existing `loaded & set(ruleset_versions)` admission mechanism does the rest automatically the
moment the key exists, and `rules_java`'s apparent repo name equals its module name, so no
`ruleset_repo_names()` entry is needed either.

Proven under real Bazel, not merely offline: `tests/test_bazel.py::_RULESET_LOAD_PROBES` gained a
`"rules_java"` entry (required — `test_every_pinned_ruleset_has_a_load_probe` asserts set equality
against `ruleset_versions` and fails immediately without it), which also auto-parametrizes
`test_every_pinned_ruleset_version_loads_under_real_bazel` with a `rules_java` case. A new test,
`test_real_bazel_builds_the_generated_jvm_package`, mirrors D6's
`test_real_bazel_resolves_a_load_whose_ruleset_only_a_target_names` shape exactly for a synthetic
`java_library` (no Maven/`rules_jvm_external` involvement): proves the pre-fix failure
(`unknown repo 'rules_java'`) against the real loader, then proves a real `bazel build` completes
successfully post-fix. `--java_runtime_version=remotejdk_21` in that second half is
test-invocation plumbing only — it is not emitted by `render_module_bazel` or
`toolchain_requirements()`, and does not resolve the JDK-toolchain gap named above, which stays
explicitly out of scope pending its own future D-number/task.

Verified: `pytest tests/test_bazel.py -k "ruleset_has_a_load_probe or
every_pinned_ruleset_version_loads or jvm_package" -m integration` — 10/10 pass under real Bazel
9.2.0. Full `tests/test_bazel.py` + `tests/test_settings.py` (including all integration tests) —
123/123 pass, no regressions. This does NOT move any `<n> of 48` §12 criterion count — no §12
criterion names JVM/Bazel real-analysis directly (checked `docs/SPEC.md` and
`docs/CRITERIA_PLAN.md`); this is an infrastructure/blocker fix (unblocks JVM real-Bazel analysis
generally), consistent with research-41's own read.

## D122 — FIXED, LANDED (round VI task 75, `a9636b8`, controller review pending). A contract's own
PR record and its owning repo's own PR record cannot coexist — one silently overwrites the other
via `findings`' own `ux_findings_ident` unique index.

**Found by round VI task 71's controller-review fix round (2026-09-07), while disclosing why
`execute_hoist_rollback` (`cli.py`, ADR-0122 Decisions 4/5) is currently unreachable via
production data.** `PullRequestDraft.repo_id`'s own docstring: "For a contract PR this is the
OWNING repo" — so a contract's migration PR and its owning repo's own migration PR would, if both
were ever written, be persisted under the IDENTICAL `repo_id`. `_upsert_pr_record` (`cli.py`)
computes its fingerprint as `_fingerprint(run_id, draft.repo_id, PR_RECORD_KIND)` — a pure function
of `repo_id` and the finding kind, with no `contract_id` component — and writes via `INSERT ...
ON CONFLICT (run_id, IFNULL(repo_id, ''), kind, fingerprint) DO UPDATE SET payload = excluded...`,
where the conflict target is exactly `findings`' own unique index, `ux_findings_ident` (`state/
schema.sql:422`, `ON findings (run_id, IFNULL(repo_id, ''), kind, fingerprint)`). Two
`PullRequestDraft` rows sharing one `repo_id` therefore share one fingerprint and one `findings`
row: writing the second **overwrites** the first rather than coexisting beside it. `_pr_records`
(`cli.py`) reads this same table keyed by `repo_id` into a `dict[str, PullRequestDraft]`, so even
if both writes somehow landed in some order, only the LAST one written would ever be visible again.

**Why this blocks task-72 (or any future task wiring contract PRs into `fleet pr`), not merely
narrows it.** Confirmed, this same round: no production code path in `src/fleet/` today
constructs a `PullRequestDraft` with `contract_id` set (`_emit_one_pr`'s `PrwriterInput` call never
passes it, and `_PrCandidate` has no `contract_id` field at all) — so
`cli._ordered_revert_shas`'s scan for `draft.contract_id == contract_id` always returns `None`
today, and any call to `execute_hoist_rollback` with a non-empty ordered list raises
`RollbackAnchorError` by design (there is no anchor to resume from). This is not merely "a future
wiring task hasn't been written yet" — the FIRST attempt to wire contract-PR dispatch would hit
this collision immediately, because the natural way to add it (give the contract's PR task
`ctx.repo_id = <owning repo>`, matching the model's own documented field meaning, and let it
`_write_pr_record` normally) silently clobbers whichever of the two PRs (the owner's own, or the
contract's) is written second — a defect that would not surface as a crash, only as one PR
record silently vanishing from `_pr_records`'s output the next time either PR is polled or
referenced. Wiring contract PRs therefore needs a real persistence-schema decision first: a
different key shape (e.g. keying a contract's PR record on `contract_id` instead of `repo_id`,
or a compound key), or a separate table/column — not a pass-through of the existing
`_upsert_pr_record`/`_pr_records` shape as-is.

**Not yet built:** the fix, or even the design decision. Scoped here as a disclosed blocking gap
for whoever briefs the contract-PR-dispatch task (named but not itself designed by ADR-0122's own
"Consequences" paragraph) — allocating D122 rather than silently leaving this implicit, per
CLAUDE.md's Central Number Allocation rule and Guardrail 6 ("state exactly what you ran, including
what you excluded").

**Adjudicated 2026-09-08 (controller, ADR-0126) — design decision made, not yet built.** The
fingerprint feeding `contracts`' PR record widens to include `contract_id` (empty-string sentinel
when absent, matching this project's existing `IFNULL(repo_id, '')` convention), and
`_pr_records`'s key widens from `repo_id` to `(repo_id, contract_id)` — no schema/index migration.
Full rationale in `docs/DECISIONS.md` ADR-0126. Task-75 dispatched to build it. Status remains
OPEN until task-75 lands and is reviewed.

**FIXED, LANDED 2026-09-08 (round VI task 75, `a9636b8`).** Built exactly as ADR-0126 specified:
`_upsert_pr_record`'s fingerprint call site gained `draft.contract_id or ""` as a 4th argument
(`_fingerprint`'s signature is already `*parts: str`, so no signature change was needed — checked
every other call site first, per this task's own brief); `_pr_records`'s return type widened from
`dict[str, PullRequestDraft]` to `dict[tuple[str, str | None], PullRequestDraft]`, keyed
`(repo_id, contract_id)`. `PullRequestDraft.contract_id` already existed on `models/tasks.py`
(ADR-0019) — no model change was needed, matching one of ADR-0126's own disclosed possibilities.
Every call site `mypy --strict` flagged was updated (`execute_hoist_rollback`'s and
`_ordered_revert_shas`'s repo-owned lookups, `_pr_sync_impl`'s poll set and D103 gap-1 sweep,
`_pr_impl`'s eligibility/promotion lookups, `_emit_prs`/`_promote_prs`/`_emit_one_pr`/
`_regenerate_pr_body`'s threaded `Mapping` parameter, `_stub_reconcile_inputs`'s provider-facts
lookup, `_apply_stub_reconcile`'s consumer-HELD update) — all as `(x, None)`, since no production
call site sets `contract_id` yet, exactly as ADR-0126 anticipated. One defect the type checker
could not see (the return dict is typed `dict[str, object]`) surfaced only via the full-file test
run: `_pr_sync_impl`'s JSON-facing `"polled"`/`"terminal"` lists were built from `sorted(pollable)`
/ `set(records) - set(pollable)`, which silently started sorting the raw `(repo_id, contract_id)`
tuple keys once the type changed, breaking `test_pr_sync_sweeps_a_pre_merged_providers_stub_left_
active_by_a_prior_crash` (`tests/test_pr_e2e.py`) — fixed by projecting onto the `repo_id`
component before building those two lists. Rule-12 discriminator:
`tests/test_hoist_rollback_git.py::test_a_contracts_own_pr_record_coexists_with_its_owning_repos_
own_pr_record` writes two `PullRequestDraft` rows sharing one `repo_id` (one `contract_id=None`,
one set) through `_write_pr_record` and reads both back through `_pr_records` — reverting
`src/fleet/cli.py` to its pre-fix state makes this exact test fail (`_pr_records` returns exactly
one entry, the second draft silently overwrote the first); restoring the fix makes it pass, both
present, addressable by `(repo_id, contract_id)`. No production call site was added that
constructs a `PullRequestDraft` with `contract_id` set (still zero, as ADR-0126 scoped) — wiring
one remains a future task's job. Full test files run whole, no `-k`: `tests/test_cli.py` (194),
`tests/test_hoist_rollback_git.py` (13, +1 new), `tests/test_pr_e2e.py` (25),
`tests/test_event_stream_wiring.py` (6) — 238 total, identical to the 237-passing baseline plus
exactly the one new discriminator test. `mypy --strict src/fleet/` and `ruff check`/
`ruff format --check` (on every line this task touched) both clean.

## D123 — FIXED, LANDED (round VI task 76 `981abed` + round VI task 84 `3044479`/`6ab0277`/`62b2049`,
ADR-0130) — both the single-invocation case and the `--wave`-scoped multi-invocation residual
(formerly tracked separately as D126) are now closed. A direct dependent of an RHI repo does not
become `BLOCKED` at the TRANSFORM phase — cross-wave `blocked_by` propagation
silently never reaches a not-yet-dispatched dependent

**Found by round VI task 69's own task-scoped review (2026-09-07), independently reproduced on
unmodified `main` (`c01fe46`) with zero task-69 code involved.** Verified free before allocating:
form-agnostic sweep found `D122` as the highest allocated number.

**The gap, as measured.** A real `fleet scan` + `fleet transform` fixture (one provider forced to
fail and reach `REQUIRES_HUMAN_INTERVENTION`, one direct dependent with an ordinary edge to it)
was run against real `main`. Expected per `docs/SPEC.md:7578`'s literal §12 item 14 text: the
dependent marks `BLOCKED`. Measured: `EXIT=7 | ('acme-lib-py','REQUIRES_HUMAN_INTERVENTION','[]')
| ('acme-app-py','SUCCEEDED','[]')` — the dependent never became `BLOCKED`, and its own
`blocked_by` column stayed empty throughout.

**Root cause, as traced.** `_transform_impl`'s wave loop writes each wave's `phases` rows via a
lazy per-wave `upsert_phase` call. A dependent scheduled in a LATER wave than its now-RHI provider
never has its own `blocked_by` column populated at the moment the provider's status is known,
because the write that would set it only happens when that dependent's own wave is reached — by
which point the propagation step that should have caught it has already run and moved on. This is
a structural gap in the propagation timing, not a one-off bug in a single call site.

**Consequence.** §12.14's own blast-containment clause — "a repo in `REQUIRES_HUMAN_INTERVENTION`
marks exactly its transitive dependents ... `BLOCKED` — no more, no less" — is provably FALSE
against real production code today, independent of anything §37/§14/§39's stub-creation bundle
built or didn't build. This also falsifies `docs/CRITERIA_PLAN.md`'s own prior §14 entry, which
claimed "(a) and (b) — containment and `fleet resume` unblocking — are fully covered through real
e2e paths" — see that entry's own dated correction, landed in the same commit as this one.

**Not yet built:** the fix — most likely restructuring the wave loop's `blocked_by` propagation to
run before or independent of each wave's own lazy `upsert_phase`, so a provider's RHI status is
visible to every dependent's `blocked_by` column regardless of which wave the dependent is
scheduled in, not only dependents in the same or an earlier wave. No task briefed yet; this is the
controller's next dispatch candidate for §12.14.

**Fixed for the SAME-invocation case, 2026-09-08 (round VI task 76, ADR-0127, `981abed`) — see the dated paragraph below for the narrower residual this does NOT close.** `_transform_impl`'s wave loop now
pre-seeds every wave's TRANSFORM `phases` row for the whole invocation domain upfront, before any
wave dispatches — mirroring `_build_impl`'s already-correct PASS 1 (ADR-0127 judgment call 1); no
change was needed to `WaveScheduler.admit()`, `SqliteSchedulerStore.append_blocked_by`,
`propagate_blocked`, `ALLOWED_TRANSITIONS`, or `orchestrator/reentry.py` (judgment call 2,
re-confirmed by reading, not assumed). `tests/test_transform_e2e.py::
test_a_provider_failing_in_an_earlier_wave_blocks_its_later_wave_dependent_in_one_run` proves the
same-invocation shape this entry measured now holds, with an old-fails/new-passes check against
the pre-fix code reproducing this entry's own measured numbers exactly
(`('acme-app-py','SUCCEEDED','[]')`). **Disclosed, not closed by this fix:** the OTHER shape this
entry's own "Measured directly, twice" discovery recorded (`tests/test_pr_e2e.py::_seed_blocked`'s
docstring) — two SEPARATE `fleet transform --wave N` invocations — still exhibits the defect,
because `propagate_blocked` has exactly one call site (`orchestrator/runner.py`'s
`PhaseRunner._contain`, fired once at the RHI transition) and a provider that already went RHI in
an earlier, separate invocation never re-fires containment in a later invocation's own pre-seed
pass. This residual is now tracked separately as **D126** (below), per the controller's ruling in
round VI task-76 fix round 1 — see that entry for the fix-round's own independent reproduction.
Also out of this task's scope: `_verify_impl` has the byte-for-byte identical structural pattern
(ADR-0127 judgment call 3) — already allocated **D125** (above) by the controller while landing
ADR-0127, not fixed here.

**Controller ruling, 2026-09-08 (round VI task-76 fix round 1) — status corrected from `FIXED,
LANDED` to `PARTLY ADDRESSED`.** An independent opus-tier review of task-76's branch confirmed the
fix above is real and correctly matches ADR-0127's design, but found the prior `FIXED, LANDED`
heading overclaimed by not distinguishing two genuinely different cases. §12 item 14's own literal
wording ("a repo in `REQUIRES_HUMAN_INTERVENTION` marks exactly its transitive dependents ...
`BLOCKED`") is written as describing ONE continuous run — ADR-0127's actual target, and the case
`981abed`'s fix and its regression test cover. **The single-`fleet transform`-invocation case is
fixed and proven:** driving both `acme-lib-py`'s and `acme-app-py`'s waves through one `fleet
transform` call now correctly leaves the dependent `BLOCKED`. **A `--wave`-scoped sequence of
separate invocations still reproduces the identical original symptom** — `acme-app-py` ends
`SUCCEEDED` / `blocked_by == '[]']` rather than `BLOCKED` — independently re-measured by the fix
round (`fleet transform --wave 0` then `fleet transform --wave 1`, real dispatch, no hand-seeding:
`FIRST_EXIT=7, SECOND_EXIT=7, ROWS=[('acme-lib-py','REQUIRES_HUMAN_INTERVENTION','[]'),
('acme-lib-ts','SUCCEEDED','[]'), ('acme-app-py','SUCCEEDED','[]'),
('acme-app-ts','SUCCEEDED','[]')]`). Tracked separately as **D126** (below); the heading and this
paragraph are the correction, the fix description above is left as written since it is accurate
for what it covers.

**FIXED, 2026-09-08 (round VI task 84, ADR-0130, `3044479`/`6ab0277`/`62b2049`) — the `--wave`-scoped multi-invocation
residual tracked as D126 is now closed; see D126's own entry below for the full fix description
and regression proof.** `_transform_impl` now calls a new shared helper,
`_repropagate_terminal_providers`, immediately after its existing pre-seed pass and before wave
dispatch begins, which re-broadcasts `blocked_by` against any provider already durably
`REQUIRES_HUMAN_INTERVENTION` on record — closing the gap this entry's own "Controller ruling"
paragraph above disclosed as NOT fixed by `981abed`. This entry's heading is corrected from
`PARTLY ADDRESSED` to `FIXED, LANDED` accordingly; every paragraph above is left as written, since
each was accurate for the state of the code at the time it was written (CLAUDE.md's "annotate, do
not rewrite" convention).

## D124 — FIXED, LANDED (round VI task 74, `489cc0d`, controller review pending). No CLI surface
exists to re-run an abandoned (`REQUIRES_HUMAN_INTERVENTION`) repo to `SUCCEEDED` — `fleet retry`
does not exist, and `ALLOWED_TRANSITIONS` has no edge out of that status

**Found by round VI task 69's own task-scoped review (2026-09-07).** Verified free before
allocating (immediately after D123, same investigation): form-agnostic sweep found `D123` as the
highest allocated number at the moment of this entry.

**The gap, as measured.** `docs/SPEC.md:7578`'s literal §12 item 14 text requires: "Re-running the
abandoned repo to `SUCCEEDED` removes it from every `blocked_by` and returns any repo with an
empty `blocked_by` to `PENDING`." `grep -n "def retry" src/fleet/cli.py` returns zero hits — no
`fleet retry` (or equivalent) CLI command exists to drive this. Independently, `RepoStatus.
REQUIRES_HUMAN_INTERVENTION` has no outbound edge in `ALLOWED_TRANSITIONS` (`models/enums.py`),
and no `OPERATOR_REOPEN`-style flag exists to open one — so even a caller that wanted to write
`SUCCEEDED` over an RHI row has no legal transition to do it with. This is also, independently, one
of the blockers `docs/SPEC.md`'s §12.37 item 1 needs for its own "re-running `P` to `SUCCEEDED`"
clause (see `docs/CRITERIA_PLAN.md`'s `## 37.` entry) — the same missing mechanism blocks both
criteria's identical-shaped clause.

**Correction, 2026-09-07 (round VI research-42, ADR-0125) — the "no `OPERATOR_REOPEN`-style flag
exists" clause above is false, measured against current `HEAD`.** `src/fleet/models/enums.py`
already defines `OPERATOR_REOPEN: dict[RepoStatus, frozenset[RepoStatus]] = {REQUIRES_HUMAN_
INTERVENTION: frozenset({PENDING})}`, and `transition()` already accepts the `operator: bool`
keyword that opens it — both present since the project's initial commit (`a1178f7`), predating
this entry, and already unit-tested (`tests/test_state_models.py::
test_abandoned_is_reachable_only_through_the_audited_operator_door`). The design decision this
entry called for was already made; what is genuinely missing, per ADR-0125, is narrower: a
dedicated writer function pairing the CAS-guarded status write with an audit finding (mirroring
`degrade_for_stub()`), and the `fleet retry` CLI command itself — zero production call sites of
`operator=True` exist anywhere in `src/`. The rest of this entry's verdict stands unchanged: OPEN,
no CLI surface, no writer function, no production call site. See ADR-0125 for the full design.

**Consequence.** Neither §12.14's clause (2) nor §12.37's "re-running P" clause can ever be driven
for real today, regardless of anything else either criterion's own bundle builds. `D104` (already
`OPEN`) covers the `TaskKind.REVALIDATE` dispatch path specifically; this entry is the narrower,
structurally distinct gap of there being no CLI entry point or legal state-machine edge at all for
re-running an RHI repo, which D104's own scope does not cover.

**Not yet built:** the fix — a new `fleet retry <repo>` (or similarly-named) CLI command, a new
`ALLOWED_TRANSITIONS` edge (likely gated behind an explicit `operator: bool` flag on `transition()`,
mirroring the `resume`/`stub_degrade` precedent already established for other operator-gated
terminal-state exceptions), and the `blocked_by`-clearing/`PENDING`-return logic clause (2)
describes. This is real design work (a new gated transition edge is exactly the class of change
this project's own precedent treats as needing an ADR — see `RESUME_DEMOTE`/`STUB_DEGRADE`'s own
history), not a mechanical wiring task. No task briefed yet; this is the controller's next dispatch
candidate for both §12.14 and §12.37, likely as a shared fix since both criteria need the identical
mechanism.

**Fixed (2026-09-07, round VI task 74, `489cc0d`).** `models.enums.OperatorReopen`/
`reopen_abandoned()` (mirrors `PhaseDemotion`/`demote()` and `StubDegradation`/`degrade_for_stub()`
exactly) and `state.repository.SqliteStateRepository.reopen_to_pending` (mirrors
`stub_degrade_transform`'s transaction shape — CAS-guarded, audit finding in the same `BEGIN
IMMEDIATE`, raises `RepositoryError` on zero or multiple RHI rows rather than silently picking one)
back a new `fleet retry <repo> --reason <text> [--dry-run]` CLI command (ADR-0125).

**Correction (2026-09-08, fix round 1, task-74, opus-tier review).** This paragraph originally
claimed one end-to-end fixture proved "ADR-0125 judgment call 5's central empirical claim" as a
single whole. That claim's central empirical question has TWO independent halves — the
`still_blocking`/step-6 `blocked_by`-clearing half, and the `phase_floor`/step-5 freshly-reopened-
row half — and the original fixture only exercised the first: it wrote the reopened phase straight
to `SUCCEEDED` by direct SQL *before* the first `fleet resume` call, so at resume time the repo was
already all-`SUCCEEDED` and `phase_floor` hit its `frontier is None` early return without ever
running a real backward walk on a genuinely-`PENDING`, freshly-reopened row. Two separate fixtures
now prove the two halves separately, exactly as they must be: `tests/test_cli.py::
test_fleet_retry_reopens_p_then_a_later_resume_clears_c_once_p_relands_succeeded` (unchanged, still
correct for what it actually proves) proves the `still_blocking`/step-6 half — via the real
`SqliteSchedulerStore.append_blocked_by` writer, a repo reopened via `fleet retry`, driven to a
genuine `SUCCEEDED` (disclosed shortcut in the test's own docstring: a direct SQL write standing in
for the real VERIFY worker, the same shortcut this suite's own step-5/6 fixtures already use
elsewhere), then a SECOND `fleet resume` clears a dependent's `blocked_by` and returns it to
`PENDING` in a freshly appended synthetic wave. `tests/test_cli.py::
test_fleet_retry_reopens_a_genuinely_pending_rhi_phase_and_resume_recomputes_the_real_floor` (new,
fix round 1) proves the other half: the reopened VERIFY phase is left genuinely `PENDING` (never
pre-set to `SUCCEEDED`), so `fleet resume`'s step 5 meets a live, unsettled frontier and actually
runs the backward walk — measured result: the floor lands at `BUILD`, `BUILD` is demoted
`SUCCEEDED -> PENDING` with a `PhaseDemoted` finding, and VERIFY (already `PENDING` from the retry,
never `SUCCEEDED`) is correctly left untouched by the demotion. Both fixtures together confirm the
reviewer's own independent measurement: ADR-0125 judgment call 5's claim is TRUE — zero changes to
`orchestrator/reentry.py` were needed for either half.

This closes this entry's own scope in full: CLI surface, writer function, production call site all
now exist and are tested. It does **not** by itself close §12.14 (D123's cross-wave `blocked_by`
propagation gap and the undesigned transitive-stub-stacking mechanism are untouched) or §12.37
(D104's separate, still-open `REVALIDATE`-dispatch gap is untouched) — see
`docs/CRITERIA_PLAN.md`'s §14/§37 entries.

## D125 — FIXED, LANDED (round VI task 80 `74aedc4` + round VI task 84 `3044479`/`6ab0277`/`62b2049`,
ADR-0130) — both the single-invocation case and the `--wave`-scoped multi-invocation residual
(formerly tracked separately as D126) are now closed. `_verify_impl`'s wave loop has the same
cross-wave `blocked_by` propagation gap D123 found in `_transform_impl`

**Found by round VI research-43 (2026-09-08), while designing D123's fix (ADR-0127), as a
byproduct of reading `_transform_impl` alongside its siblings — not independently investigated
further, per that research task's own scope boundary.** Verified free before allocating:
form-agnostic sweep found `D124` as the highest allocated number.

**The suspicion, as read (not yet measured against a real fixture).** `_verify_impl`'s wave loop
(`src/fleet/cli.py:11687-11702`, cited by research-43 against `HEAD` at the time of that
research) has the byte-for-byte same structural shape TRANSFORM had before ADR-0127's fix: a
lazy, per-wave `upsert_phase` call inside the dispatch loop, rather than `_build_impl`'s upfront
PASS 1 pre-seed. If this reading is correct, a repo reaching `REQUIRES_HUMAN_INTERVENTION` during
an early VERIFY wave would fail to propagate `blocked_by` to a direct dependent scheduled into a
later VERIFY wave within the same `fleet verify` invocation — the identical defect D123 measured
for TRANSFORM, by the same mechanism.

**Why this is disclosed as OPEN rather than assumed fixed by ADR-0127/task-76.** ADR-0127's fix is
scoped to `_transform_impl` only (its judgment call 2 explicitly declines to touch `_verify_impl`,
deferring it as this entry). Re-verify this reading fresh against current `HEAD` before acting on
it — code may have moved since research-43 read it — and confirm with a real fixture (mirroring
D123's own discovery fixture, adapted to `fleet verify`) before treating this as more than a
structural suspicion.

**Not yet built:** the fixture that would confirm or refute this, and (if confirmed) a fix
mirroring ADR-0127's shape adapted to `_verify_impl`'s own PASS structure. **Not dispatched this
round** — the controller is deferring this to a future round to avoid over-extending the current
wave; this entry exists so the finding is not lost between rounds.

**Confirmed, 2026-09-08 (round VI task 78) — measurement only, no fix built, per this task's own
scope boundary.** `_verify_impl`'s wave loop, re-read fresh against current `HEAD`
(`src/fleet/cli.py:11968-11983`, line range shifted from research-43's `11687-11702` citation but
the shape is unchanged): `for index in waves: members, blocked = await _gated_members(...); ...;
for repo_id in members: await repository.upsert_phase(run_id, repo_id, Phase.VERIFY, now=_now(),
max_attempts=MAX_ATTEMPTS)` — still lazy, still per-wave, still inside the dispatch loop, exactly
the shape TRANSFORM had before ADR-0127. It has NOT been changed since research-43 read it.

**Root cause, as measured — identical mechanism to D123's, restated for VERIFY.** `PhaseRunner.
_contain` -> `WaveScheduler.propagate_blocked` -> `SqliteSchedulerStore.append_blocked_by` fires
synchronously the moment a VERIFY-phase member reaches `REQUIRES_HUMAN_INTERVENTION`, with the
full, wave-independent descendant set already correctly computed — but `append_blocked_by`'s
write is UPDATE-only, and a dependent scheduled into a later VERIFY wave has no `phases` row for
`Phase.VERIFY` yet at that moment, because that wave's own lazy `upsert_phase` loop iteration has
not run. The write silently no-ops; the dependent is later admitted into its own wave as an
ordinary unblocked repo.

**Fixture built and run:** `tests/test_build_e2e.py::
test_a_verify_provider_reaching_rhi_in_an_earlier_wave_blocks_its_later_wave_dependent`,
mirroring D123's own discovery fixture as closely as `fleet verify`'s own PASS structure allows.
`acme-lib-py` (wave 0) is driven to a real `REQUIRES_HUMAN_INTERVENTION` through a genuine VERIFY
dispatch — no hand-seeding — via the same `FakeBazel` seam `tests/test_build_e2e.py`'s Phase-3
failure test already uses (`bazel.fail[("build", "py/acme_lib_py")] = 34`, injected only AFTER
Phase 3's own `fleet build` completed clean, so the predecessor gate admits every repo into
VERIFY). `acme-app-py`, `acme-lib-py`'s real direct dependent, is scheduled into wave 1 in the
same `fleet verify` invocation. Measured result (real rows, one `fleet verify` call driving both
waves): `{'acme-lib-py': ('REQUIRES_HUMAN_INTERVENTION', []), 'acme-lib-ts': ('SUCCEEDED', []),
'acme-app-py': ('SUCCEEDED', []), 'acme-app-ts': ('SUCCEEDED', [])}` — `acme-app-py` reads
`SUCCEEDED`/`blocked_by == []` where §12.14's blast-containment clause requires
`BLOCKED`/`['acme-lib-py']`. This is byte-for-byte the same wrong shape ADR-0127's own discovery
fixture measured for TRANSFORM before its fix (`('acme-app-py','SUCCEEDED','[]')`). **CONFIRMED**,
not merely a structural suspicion.

**Consequence.** Identical to D123's own: §12.14's blast-containment clause is provably FALSE
against real production VERIFY dispatch today, independently of D123/ADR-0127's TRANSFORM-only
fix — a repo abandoned during an early VERIFY wave does not correctly block a later-wave
dependent within the same `fleet verify` invocation.

**Regression-proof landed as a known-failing test**, not a fix:
`tests/test_build_e2e.py::test_a_verify_provider_reaching_rhi_in_an_earlier_wave_blocks_its_later_wave_dependent`
is `@pytest.mark.xfail(strict=True, reason="D125: ...")` — it will hard-fail the instant someone
fixes `_verify_impl` without deleting this marker, exactly as `tests/test_baseline_ok_exclusion.py`
(D116) and D123's own TRANSFORM fixture (before ADR-0127) already do.

**Not yet built:** the fix itself — most likely restructuring `_verify_impl` to pre-seed every
wave's VERIFY `phases` row upfront, before any wave dispatches, mirroring ADR-0127's own shape
(and `_build_impl`'s PASS 1) exactly. Whether the same TRANSFORM residual D126 tracks (a
`--wave`-scoped sequence of SEPARATE `fleet verify` invocations) also applies to VERIFY was not
investigated here — out of this task's own scope, which was measurement only. No task dispatched
yet for the fix; this entry's confirmation is what makes it dispatch-ready.

**FIXED, 2026-09-08 (round VI task 80, ADR-0129).** `_verify_impl` (`src/fleet/cli.py`) now
pre-seeds every open wave's VERIFY `phases` row upfront, before any wave dispatches, adapting
ADR-0127's `_transform_impl` shape to VERIFY's own predecessor-phase gate: a
`gated_by_wave: dict[int, tuple[tuple[str, ...], tuple[str, ...]]]` is computed by calling
`_gated_members(..., predecessor=Phase.BUILD)` once per open wave (memoized, hoisted above the
dispatch loop), then `upsert_phase` runs once over every wave's `members` half ONLY (never the
`blocked`/withheld half — `_gated_members`'s own documented invariant that a repo not yet
BUILD-ready must get no VERIFY phase row at all is preserved). The dispatch loop's own
`_gated_members` call and its redundant `upsert_phase` loop were deleted; `withheld.update(blocked)`
unpacks the precomputed tuple, unchanged in behavior. Full design in `docs/DECISIONS.md`'s
ADR-0129. Regression-proof: `tests/test_build_e2e.py::
test_a_verify_provider_reaching_rhi_in_an_earlier_wave_blocks_its_later_wave_dependent`'s
`xfail(strict=True)` marker is removed; the test now genuinely PASSES —
`acme-app-py` reads `('BLOCKED', ['acme-lib-py'])`, `acme-lib-py` reads
`REQUIRES_HUMAN_INTERVENTION`, and the two non-dependent survivors (`acme-lib-ts`, `acme-app-ts`)
read `('SUCCEEDED', [])`, confirming the fix blocks exactly the true descendant. Old-fails/
new-passes discriminator run (pre-fix code restored from `HEAD`, same un-xfail'd test): FAILS,
reproducing D125's own measured symptom byte-for-byte (`acme-app-py` reads `('SUCCEEDED', [])`).
`tests/test_prepare_before_admit.py::test_a_breached_verify_wave_cuts_no_worktree` was run before
and after the fix and stays green both times, confirming ADR-0129's prediction that its
subset-of-values assertion is unaffected.

**Disclosed, same date — a VERIFY-side D126 residual is CONFIRMED real by direct measurement, not
merely predicted.** ADR-0129's judgment call 3 predicted that a `--wave`-scoped sequence of
SEPARATE `fleet verify` invocations (mirroring D126's own TRANSFORM-side shape) would still exhibit
this defect, because the pre-seed pass above is scoped to each invocation's own `waves` domain
(`_open_phase_waves` returns exactly `(wave,)` when `--wave` is given). Measured directly (round VI
task 80, same fixture, driven across two separate CLI invocations: `fleet verify --wave 0` then a
second, separate `fleet verify --wave 1`): `FIRST_EXIT=7, SECOND_EXIT=7`, final `phases` rows
`[('acme-app-py','SUCCEEDED','[]'), ('acme-app-ts','SUCCEEDED','[]'),
('acme-lib-py','REQUIRES_HUMAN_INTERVENTION','[]'), ('acme-lib-ts','SUCCEEDED','[]')]` —
`acme-app-py` again reads `SUCCEEDED`/`blocked_by == []`, never `BLOCKED`, reproducing the defect
across the invocation boundary exactly as predicted. This is genuinely OUT OF SCOPE for this fix
(same disposition as D126 itself for TRANSFORM) and is **not being allocated a new D-number here**
— it is named as an open question for the controller's next dispatch round, exactly as ADR-0127
did for D125 itself: fold it into D126's existing scope (retitle to cover both phases) or allocate
a sibling D-number is a controller prioritization call, not a technical fact this task can settle.

**Controller ruling, 2026-09-08 (round VI task-80 review) — status corrected from `FIXED, LANDED`
to `PARTLY ADDRESSED`, and the VERIFY-side residual folded into D126 rather than given a sibling
number.** An independent opus-tier review of task-80's branch confirmed the fix above is real and
correctly matches ADR-0129's design, but found the `FIXED, LANDED` heading overclaimed by the same
measure D123's own heading was corrected against (round VI task-76 fix round 1): the
`--wave`-scoped multi-invocation residual is CONFIRMED, not merely predicted, by this same entry's
own measurement above. Mirroring D123's precedent exactly, this heading now reads `PARTLY
ADDRESSED` — the single-`fleet verify`-invocation case is fixed and proven; the `--wave`-scoped
residual is tracked below as part of **D126**, retitled to cover both TRANSFORM and VERIFY since
both share the identical root cause (`_open_phase_waves` returning `(wave,)` when wave-scoped,
`propagate_blocked`'s single call site) rather than being two independent defects. No new
D-number allocated.

**FIXED, 2026-09-08 (round VI task 84, ADR-0130, `3044479`/`6ab0277`/`62b2049`) — the `--wave`-scoped multi-invocation
residual tracked as D126 is now closed for VERIFY as well as TRANSFORM; see D126's own entry below
for the full fix description and regression proof.** `_verify_impl` now calls the same shared
helper `_transform_impl` calls, `_repropagate_terminal_providers`, immediately after its own
existing gated pre-seed pass and before wave dispatch begins. This entry's heading is corrected
from `PARTLY ADDRESSED` to `FIXED, LANDED` accordingly; every paragraph above is left as written,
since each was accurate for the state of the code at the time it was written (CLAUDE.md's
"annotate, do not rewrite" convention).

## D126 — FIXED, LANDED (round VI task 84, `3044479`/`6ab0277`/`62b2049`, ADR-0130). A `--wave N`-scoped
sequence of multiple separate `fleet transform` or `fleet verify` invocations does not propagate
`blocked_by` across invocation boundaries — the narrower residual of D123/D125 that
ADR-0127/ADR-0129's same-invocation fixes do not close

**Found by an independent opus-tier review of round VI task-76's branch (2026-09-08), ruled on by
the controller in that task's fix round 1 (this entry).** Verified free before allocating:
form-agnostic sweep found `D125` as the highest allocated number.

**The gap, as independently re-measured (round VI task-76 fix round 1, not merely copied from the
review).** Same fixture D123 used (`acme-lib-py` claims its own module with a rule that matches
nothing, `acme-app-py` a direct dependent scheduled into a later wave), driven across TWO SEPARATE
CLI invocations rather than one: `fleet transform --wave 0` (real dispatch, `acme-lib-py` reaches
`REQUIRES_HUMAN_INTERVENTION` for real, no hand-seeding), then a second, separate `fleet transform
--wave 1`. Measured: `FIRST_EXIT=7, SECOND_EXIT=7, ROWS=
[('acme-lib-py','REQUIRES_HUMAN_INTERVENTION','[]'), ('acme-lib-ts','SUCCEEDED','[]'),
('acme-app-py','SUCCEEDED','[]'), ('acme-app-ts','SUCCEEDED','[]')]` — `acme-app-py` ends
`SUCCEEDED` with `blocked_by == '[]'`, never `BLOCKED`, reproducing D123's exact original symptom
even though D123's own fix (`981abed`) is present on the branch/commit this was measured against.

**Root cause, as traced (task-76's own reasoning, independently confirmed by the review).**
`_open_transform_waves` (`src/fleet/cli.py`) delegates to `_open_phase_waves`, which returns
exactly `(wave,)` — and nothing else — when `wave is not None`. ADR-0127's pre-seed pass computes
`members_by_wave` by iterating only `waves`, so a `--wave 0`-scoped invocation's pre-seed pass
touches ONLY wave 0's members; wave 1's members (`acme-app-py`) get no `phases` row at all during
that invocation. `acme-lib-py`'s RHI transition, and its one and only call to
`WaveScheduler.propagate_blocked` (`orchestrator/runner.py`'s `PhaseRunner._contain` is the sole
call site in `src/fleet/`), happens entirely inside the FIRST invocation, before `acme-app-py`'s
row exists in either invocation. The second, separate `--wave 1` invocation's own pre-seed pass
then creates `acme-app-py`'s row fresh at `PENDING` — but nothing re-fires containment for the
already-terminal, already-exited `acme-lib-py`, because there is no second call site to do so.
`acme-app-py` is admitted and dispatched as an ordinary unblocked repo.

**Why this is a narrower, genuinely different case from D123's now-fixed one, not a sign the fix
is wrong.** §12 item 14's own literal wording ("a repo in `REQUIRES_HUMAN_INTERVENTION` marks
exactly its transitive dependents ... `BLOCKED`") is phrased as describing ONE continuous run —
ADR-0127's actual target, closed by `981abed`, and the case
`tests/test_transform_e2e.py::test_a_provider_failing_in_an_earlier_wave_blocks_its_later_wave_dependent_in_one_run`
proves. A `--wave`-scoped sequence of separate invocations is a materially different shape: each
invocation is its own `_transform_impl` call with its own pre-seed domain, and nothing in
ADR-0127's design (or SPEC's wording) claims to unify state across separate invocation boundaries
— that would need either a resume-time/wave-open-time re-derivation of `blocked_by` against
already-terminal providers, or widening the pre-seed pass to cover the whole fleet's plan
regardless of `--wave` scoping (which would itself change `--wave`'s documented semantics and is a
design decision, not a bugfix).

**Consequence.** An operator driving TRANSFORM one wave at a time via repeated `--wave N`
invocations (rather than one unscoped `fleet transform` call) does not get real-time
blast-containment across those invocations — a later-wave dependent of an earlier invocation's
abandoned provider is admitted and dispatched as if nothing happened, exactly as D123 originally
measured. `tests/test_pr_e2e.py::_seed_blocked` hand-seeds around exactly this gap today (its
docstring, corrected in round VI task-76 fix round 1, explains why).

**Not yet built:** the fix — most likely a `fleet resume`-time or wave-open-time re-derivation of
`blocked_by` against every already-terminal (`REQUIRES_HUMAN_INTERVENTION`) provider still on
record, run once at the start of any invocation regardless of `--wave` scoping; or an explicit
decision to change what `--wave` means (widen its own pre-seed domain fleet-wide). Either is real
design work, not a mechanical fix. **Not dispatched this round** — this entry exists so the
finding is not lost between rounds.

**Retitled, 2026-09-08 (round VI task-80 review, controller ruling) — this entry now also covers
`_verify_impl`.** Round VI task-80 confirmed, by direct measurement, that the identical residual
reproduces for VERIFY: driving the same fixture across two separate CLI invocations (`fleet
verify --wave 0` then `fleet verify --wave 1`) leaves `acme-app-py` reading `SUCCEEDED`/
`blocked_by == '[]'` rather than `BLOCKED`, byte-for-byte the same shape measured above for
TRANSFORM (`FIRST_EXIT=7, SECOND_EXIT=7`, final rows
`[('acme-app-py','SUCCEEDED','[]'), ('acme-app-ts','SUCCEEDED','[]'),
('acme-lib-py','REQUIRES_HUMAN_INTERVENTION','[]'), ('acme-lib-ts','SUCCEEDED','[]')]`). Root
cause is the identical shared mechanism this entry already traces (`_open_phase_waves` returning
`(wave,)` when wave-scoped; `propagate_blocked`'s single call site) — not a second, independent
defect — so this is folded into D126's existing scope rather than given a sibling D-number, per
this entry's own heading correction. `docs/INTEGRATION_HONESTY.md`'s D125 entry now reads
`PARTLY ADDRESSED` accordingly, mirroring D123's own correction in round VI task-76 fix round 1.
Fix remains **not yet built** for either phase.

**FIXED, 2026-09-08 (round VI task 84, `3044479`, ADR-0130).** A single shared helper,
`_repropagate_terminal_providers(read_conn, writer, run_id, phase, settings)` (`src/fleet/cli.py`),
implements ADR-0130's judgment call 1 exactly: `SELECT DISTINCT repo_id FROM phases WHERE run_id
= ? AND phase = ? AND status = 'REQUIRES_HUMAN_INTERVENTION'`, then `await
scheduler.propagate_blocked(repo_id)` for each match — the identical, unchanged
`WaveScheduler.propagate_blocked` the live `PhaseRunner._contain` call site already uses,
constructed the same way `_run_transform_wave`/`_run_verify_wave` already construct it. Called
once from `_transform_impl` and once from `_verify_impl`, immediately after each function's
existing ADR-0127/ADR-0129 pre-seed pass and before its `for index in waves:` dispatch loop
begins. Per the controller's ruling on ADR-0130 judgment call 3, the identical call was also added
to `_build_impl` for defensive uniformity, even though BUILD was never exposed to this residual
(`_eligible_build_units`'s whole-fleet, `--wave`-independent PASS 1 already gives every invocation
full row visibility) — **corrected, round VI task-84 fix round 2 (opus-tier review, F2): the
SELECT is NOT a no-op** (a second invocation over a still-abandoned provider genuinely returns a
row — measured, below); **the WRITE is**, because the dependent was already correctly `BLOCKED` by
the first invocation's own live containment, and `append_blocked_by`'s illegal `BLOCKED ->
BLOCKED` self-edge silently skips the redundant write. See below for the corrected measurement.

Regression proof, old-fails/new-passes via the backup-file method (never `git stash`, per
CLAUDE.md's disclosed guardrail on `refs/stash` being repo-wide across concurrent worktrees):
`tests/test_transform_e2e.py::
test_a_provider_rhi_in_an_earlier_invocation_blocks_a_dependent_in_a_later_invocation`
and `tests/test_build_e2e.py::
test_a_verify_provider_rhi_in_an_earlier_invocation_blocks_a_dependent_in_a_later_invocation`
each drive the same fixture this entry's own measurements used, across two SEPARATE `--wave`-scoped
CLI invocations. Against the pre-fix code (`cli.py` restored from a pre-fix backup file, diffed
against the post-fix backup to confirm the mutation genuinely changed the file before trusting the
result), both reproduce this entry's own cited numbers byte-for-byte
(`('acme-app-py','SUCCEEDED','[]')`); against the fix, both correctly assert
`('acme-app-py', 'BLOCKED', ['acme-lib-py'])`, with the two non-dependent survivors
(`acme-lib-ts`/`acme-app-ts`) unaffected in every run. `tests/test_build_e2e.py::
test_a_later_invocations_build_sweep_reads_a_real_row_but_writes_nothing_new` (**corrected, fix round 2** — the
original version drove only one `build()` call over a leaf failure with no dependent, a fixture in
which the SELECT is trivially, structurally unreachable-as-non-zero; that proved nothing, per
CLAUDE.md's own "validate what the instrument watches" guardrail, and an independent review
measured the reachable case directly) now drives TWO real `build()` invocations over a genuine
provider/dependent pair (`acme-lib-py`/`acme-app-py`, no stub row), instruments `cli._rows` to
intercept the BUILD-side sweep's own distinctive SELECT starting after the first invocation, and
asserts two things: the SELECT's row count is non-zero at least once on the second invocation
(`[1]`, matching the review's own `SWEEP_ROWCOUNTS_INVOCATION2=[1]` measurement byte-for-byte —
reachability is real, not assumed), and `acme-app-py`'s `(status, blocked_by)` is byte-identical
before and after that second invocation (the WRITE, not the SELECT, is what is actually inert).
`tests/test_transform_e2e.py::
test_a_provider_failing_in_an_earlier_wave_blocks_its_later_wave_dependent_in_one_run`,
`tests/test_build_e2e.py::
test_a_verify_provider_reaching_rhi_in_an_earlier_wave_blocks_its_later_wave_dependent`, and
`tests/test_prepare_before_admit.py`'s three breach fixtures were all re-run and stay green,
confirming this fix does not regress the already-fixed single-invocation cases (D123/D125) or the
D84 breach-handling behavior ADR-0127/ADR-0129 disclosed.

**Fix round 1, same date (`6ab0277`) — two real conflicts with §37's stub-based escape hatches,
found by this task's own required covering-set run (CLAUDE.md §6), neither anticipated by
ADR-0130.** Running the full covering set rather than only the new fixtures surfaced two
pre-existing tests this sweep broke, both because the sweep re-derives `blocked_by` purely from
`phases.status = REQUIRES_HUMAN_INTERVENTION` — a fact §37's own, already-reviewed machinery
deliberately overrides in two different ways this sweep did not know about:

1. **TRANSFORM / §37 Blocker A (ADR-0113).** `fleet resume --stub-blocked` runs step 6
   (`orchestrator.reentry.stub_permits_removal`: once `stub_blocked` is set, EVERY blocker with an
   RHI phase row is stub-eligible for removal from EVERY dependent's `blocked_by` — a fact purely
   about the blocker and the flag, not the dependent) and then step 8's real
   `_transform_impl(stub_blocked=True)` continuation, in the SAME invocation. The sweep, blind to
   this policy, fired immediately after step 6 and silently re-blocked the exact repo step 6 had
   just correctly freed, before it could ever reach the dispatch that lets it discover its own
   stub trigger — broke `tests/test_pr_e2e.py::
   test_stub_blocked_creation_reaches_degraded_through_the_real_cli_and_feeds_t1_for_real`.
   Fixed: `_repropagate_terminal_providers` now takes `stub_blocked: bool = False` and is a hard
   no-op when set, mirroring `stub_permits_removal`'s own predicate exactly.
2. **BUILD / §37 Blocker C.** `_unit_deps`'s stub redirect (`_active_stub_facts`) lets a consumer
   build from an `ACTIVE` stub's pinned coordinate while its real provider stays permanently RHI —
   no CLI flag involved; the stub's mere `ACTIVE` existence is the whole of the policy. The sweep's
   unconditional re-block defeated this the moment a consumer already had an `ACTIVE` stub for its
   abandoned provider — broke `tests/test_build_e2e.py::
   test_an_active_stub_redirects_the_consumers_generated_dependency_to_the_stubs_label`. Fixed: the
   sweep now excludes any `(dependent, provider)` pair already covered by an `ACTIVE` `stubs` row
   before writing `blocked_by`, which required reimplementing `propagate_blocked`'s own body
   (`descendants` + `append_blocked_by`, sorted, self-edge skipped) with that one added exclusion
   rather than calling `propagate_blocked` unchanged — the one place this fix genuinely departs
   from ADR-0130's judgment call 1 as written, disclosed here rather than silently done. Applied
   uniformly across all three phases (the exemption is a fact about what an `ACTIVE` stub means,
   not a BUILD-specific one); TRANSFORM/VERIFY are unaffected by it in the existing suite, since no
   such row exists yet at the moment either of their sweeps runs in any fixture this project has.

Both fixes verified old-fails/new-passes via the same backup-file method: reverting to the
fix-round-0 code (`3044479`) reproduces both failures byte-for-byte, and this task's own two new
D126 fixtures were re-run against the fix-round-1 code and still pass. The full covering set
(`tests/test_transform_e2e.py`, `tests/test_build_e2e.py`, `tests/test_prepare_before_admit.py`,
`tests/test_resume_continue.py`, `tests/test_resume_unblocking.py`, `tests/test_pr_e2e.py`) was
re-run whole, no `-k`, after this round; the only other failures observed are 11 pre-existing
`tests/test_build_e2e.py` real-toolchain tests requiring a locally-registered `cargo`/`bazel`
network-mirror setup this sandbox does not have — confirmed unrelated by reproducing one
(`test_two_rust_repos_in_one_wave_both_build`) identically against the pre-D126 code with the
identical `tools/bin` PATH.

**Fix round 2, 2026-09-08 (`62b2049`) — opus-tier review, blocking findings F1-F4 and minor
findings F5-F8, all addressed.** An independent review of fix round 1's branch found one more
real, undisclosed bug (F3) and three disclosure/documentation defects (F1, F2, F6-F8 minor) this
entry and the code itself had not caught:

- **F3 (real bug, the same class as F3's own TRANSFORM/BUILD pair above).** `_verify_impl`/
  `_build_impl` had NO `stub_blocked` parameter of their own — only `_transform_impl` did — so
  `_continue_impl` could not thread `resume()`'s flag to their sweeps at all. `fleet resume
  --stub-blocked` can drive BUILD or VERIFY, not only TRANSFORM, in the SAME invocation as step
  6's real `stub_permits_removal` unblock, and an unguarded sweep silently re-blocked what step 6
  had just correctly freed — measured directly by the review. Fixed: both now take `stub_blocked:
  bool = False` (still not operator-facing — `fleet build`/`fleet verify` have no such CLI flag,
  so every direct call site is unaffected) and `_continue_impl` threads its own flag to all three
  delegates. Proven two ways: `tests/test_resume_continue.py::
  test_stub_blocked_reaches_every_delegate_not_only_transform` (the wiring itself — old-fails/
  new-passes confirmed, `KeyError` against the fix-round-1 code) and `tests/test_build_e2e.py::
  test_the_build_side_sweep_respects_stub_blocked_from_a_resume_continuation` (the guard, both
  branches, against real state).
- **F2 (false claim + vacuous test).** The original test (then named
  `test_the_build_side_defensive_sweep_is_a_provable_no_op` — renamed in fix round 3, per its own
  finding below, to `test_a_later_invocations_build_sweep_reads_a_real_row_but_writes_nothing_new`)
  drove only ONE `build()` call over a leaf failure with no dependent — a fixture in which the
  sweep's SELECT is structurally guaranteed to read zero (nothing has failed yet at the point a
  FIRST invocation's sweep runs), so the test proved nothing about reachability. Corrected: the
  test now drives TWO real invocations over a genuine provider/dependent pair and asserts the
  SELECT reads non-zero on the second (`[1]`, matching the review's own independent
  `SWEEP_ROWCOUNTS_INVOCATION2=[1]` measurement byte-for-byte) while the dependent's `(status,
  blocked_by)` stays unchanged — the WRITE, not the SELECT, is what is actually inert. The two
  sentences above and `_build_impl`'s own code comment are corrected to match.
- **F1.** `docs/DECISIONS.md`'s ADR-0130 judgment call 1 now carries a dated in-place correction:
  its own prose still said "reuse `propagate_blocked` completely unchanged," which the BUILD-side
  stub exclusion (fix round 1) already contradicted in the landed code — a future reconciler
  reading only the ADR could have "fixed" the code back to a bare call and silently reintroduced
  the Blocker C break.
- **F6-F8 (minor, all in the sweep's own docstring/comments).** F6: a wrong citation (`ADR-0102`,
  D89 Phase 2 Task A, unrelated) corrected to `ADR-0113`. F7: a false reason for needing no `only`
  parameter ("a repo excluded by `--repo` never has a row") replaced with the true one (rows
  persist from earlier, unscoped invocations; `--repo`/`--wave` scope dispatch, never which rows a
  durable write path may touch — `PhaseRunner._contain` already writes cross-repo). F8: disclosed
  that the `ACTIVE`-stub exclusion is keyed on a DIRECT `(consumer, provider)` pair, not the
  transitive closure `scheduler.descendants()` actually walks — a two-hop dependent of an
  indirectly stub-covered provider is not yet handled, folded into the already-named undesigned
  transitive-stub-stacking mechanism rather than claimed as closed. F5 (also minor): the
  reimplemented sweep now logs `blocked_by_propagated` (matching `PhaseRunner._contain`'s own
  event) whenever it actually blocks a dependent, so a cross-invocation re-propagation is no
  longer the one blast-containment event with no operator-visible trace.
- **Also corrected: the once-only `tests/test_resume_unblocking.py::
  test_the_source_order_matches_the_behaviour_above` failure this task's own report first
  disclosed as an unexplained flake.** The review identified the actual mechanism: that test's
  `inspect.getsource(fleet_cli._resume_impl)` reads `cli.py` from disk via `linecache`, and this
  task's own backup-file mutation-testing methodology (the sanctioned alternative to `git stash`,
  per CLAUDE.md) swaps that exact file on disk — a benign methodology artifact when a swap
  overlaps a live pytest session reading the same file, not a code defect. No further
  investigation needed; the task-84 report's own disclosure is corrected to name this mechanism.

Full covering set (`tests/test_transform_e2e.py`, `tests/test_build_e2e.py`,
`tests/test_prepare_before_admit.py`, `tests/test_resume_continue.py`,
`tests/test_resume_unblocking.py`, `tests/test_pr_e2e.py`) re-run whole, no `-k`, after this round;
`ruff format`/`ruff check`/`mypy` (no path arguments) all clean — see task-84's own report for the
fresh numbers.

## D129 — FIXED, LANDED (round VI task 79, `4ead8f9`). `bazel/query.py::rdeps_query`'s
`affected_only=True` form was invalid Bazel query syntax, never exercised under a real `bazel
query` anywhere in this tree before this task

**Found by round VI task 79, while building D104(b)'s own required real-Bazel proof (this
project's own repeated lesson: FakeBazel-only testing has masked real gaps here specifically, most
recently D121).** Verified free before allocating: form-agnostic sweep of `docs/
INTEGRATION_HONESTY.md`/`docs/DECISIONS.md`/`docs/CRITERIA_PLAN.md`/`docs/SPEC.md` for `\bD[0-9]+\b`
found `D126` as the highest allocated number.

**The gap, as measured.** `rdeps_query(dest, affected_only=True)` rendered
`f"rdeps(//..., set({kind_rule_query(dest)}))"` = `rdeps(//..., set(kind(rule, //<dest>/...)))`.
`set()` is Bazel query's LITERAL-LABEL-LIST constructor (`set(//a //b //c)`); it does not accept a
nested query expression as its argument. A real `bazel query` on this string fails:
`ERROR: ... syntax error at '( rule ,'` — reproduced in a throwaway single-package Bazel workspace
with zero harness code involved, isolating the defect to the query STRING itself rather than to
anything downstream. `rdeps()`'s own second argument already accepts an arbitrary query expression
directly, so `kind(rule, //<dest>/...)` needed no `set()` wrapper at all. `grep -rn` across
`tests/` found no test anywhere that ran this string through a real `bazel query` before this
task — every prior test (`tests/test_bazel.py::
test_the_affected_only_query_is_intersected_with_this_repos_rules` and others) asserted the STRING
`rdeps_query` returns, never that Bazel would accept it, and no real-Bazel Phase-4 `verify` test
existed in this tree at all (confirmed: `grep -rn "def test_.*real_bazel" tests/test_build_e2e.py
tests/test_pr_e2e.py` matches no `verify`-phase test).

**Consequence.** Every real (non-`FakeBazel`) invocation of `verify.affected_only: true` (the
CONFIGURED DEFAULT) against the real `bazel` binary would fail with a Bazel query syntax error,
not merely narrow its rdeps universe — an ordinary Phase 4 `fleet verify` dispatch, not specific to
stub revalidation. This had never been observed because no test in this tree drove Phase 4's own
`RdepverifyWorker` against a real `bazel` binary before round VI task 79's own D104(b) proof needed
one.

**FIXED in the same commit.** `rdeps_query`'s `affected_only=True` branch now renders
`f"rdeps(//..., {kind_rule_query(dest)})"` — no `set()` wrapper. `tests/test_bazel.py`'s two string
assertions updated to match; `tests/test_stub_resolution_task79.py::
test_d104b_claiming_loop_resolves_the_stub_under_a_real_bazel_build_and_test` is the real-Bazel
regression proof (a real `bazel build` + `bazel test` + `bazel query` all succeed against the
rewritten tree under this fix). Unrelated to D107/D104/D108 otherwise — fixed here only because it
directly blocked this task's own required real-Bazel proof (CLAUDE.md's Rule 11 disclosure, not a
scope expansion of the task brief).

---

## D130 — FIXED, LANDED (round VI task 89, `dee859c`). `fleet stubs resolve` has zero CLI
implementation — `cli.stubs_resolve` validates preconditions then unconditionally raises
`CommandUnavailableError`

**Found by round VI task 85 (2026-09-08), while building the §12.37/§12.39 combined real-CLI
stub-resolution fixture.** Verified free before allocating: form-agnostic sweep of
`docs/INTEGRATION_HONESTY.md`/`docs/DECISIONS.md`/`docs/CRITERIA_PLAN.md` for `\bD[0-9]+\b` found
`D129` as the highest allocated number.

**The gap, as measured.** `cli.stubs_resolve` validates its preconditions and then unconditionally
calls `cli._unavailable("stubs resolve", ...)`, raising `CommandUnavailableError` before touching
any state-mutating code — confirmed by task-85's own fixture, which drives the real CLI command and
asserts exactly this (exit 1, `"cannot run"` in output, zero new rows written). This is not a new
finding in isolation: D63's own "Residual, NOT fixed here" paragraph already discloses the same
underlying non-implementation as an aside while fixing a different, narrower thing (the verb's
error-message truthfulness) — D63 never owned wiring the verb itself, and no other D-number does
either.

**Consequence.** `docs/SPEC.md` §12.37's literal text requires the stub-resolution idempotency
clause's third trigger to be `fleet stubs resolve` — "triggering the resolution... three more times
... adds no further rows" presumes the trigger actually runs its resolution logic and finds nothing
left to do. Because `stubs_resolve` never reaches that logic, task-85's proof for this one
sub-clause is real (the command genuinely adds zero rows) but weaker than the literal text asks for
— it proves the command is a no-op by being unimplemented, not by being idempotent. This is the
single, precisely-scoped residual keeping §12.37 at PARTLY ADDRESSED rather than DONE after task-85
otherwise closed all four other named gaps (real `fleet retry`, a real merge-driven `fleet pr --sync`
T1 trigger, the REVALIDATE claiming loop via real `fleet resume`, and reaching
`RESOLVED`/`SUCCEEDED`/`equivalence == 'FULL'` under `FakeBazel`, per the task's own accepted scope
boundary).

**Not yet built.** Wire `fleet stubs resolve` to the existing T1 machinery
(`_stub_supersede_inputs`/`supersede`/`plan_revalidation`, or whatever the current manual-trigger
call path is — re-derive fresh, do not trust this sentence's naming without checking) — the
manual-trigger resolution logic the verb's own docstring promises already exists and is already
exercised via `_pr_sync_impl`'s `--sync` path; this is CLI-driver wiring, not new-mechanism, per
task-85's own assessment. Estimated small/cheap, comparable to the D107/D104/D108 bundle's own
smallest piece (D108).

**Correction (2026-09-08, round VI task 85 fix round 1, opus-tier review) — "the single,
precisely-scoped residual" above is wrong; annotated here, not rewritten.** The review re-read
`docs/SPEC.md:7671`'s full literal clause list against task-85's own fixture and found at least
THREE more sub-clauses the fixture does not yet assert, independent of this D-number's own gap:
(a) the `stubs` row itself arrives via `_insert_stub_row` (a raw `INSERT INTO stubs`), not
through a real `--stub-blocked` CLI dispatch — no test in `tests/test_stub_resolution_task79.py`
drives stub CREATION through the real CLI, only its real-CLI *consequences*; (b) the
same-transaction atomicity clause ("asserted by killing the process immediately after and
confirming on resume that state and task agree") has no kill/resume anywhere in the fixture;
(c) "enqueues exactly **one** `tasks` row of `kind='REVALIDATE'`" is asserted only as
`revalidation_task_id IS NOT NULL`, never as an exact count; (d) "zero new phase-2 commits...
zero LLM calls, and an `already_applied` event... the `revalidation_key` is the proof" is not
asserted at all (the fixture's own `already_applied` string is D107's rewrite-replay event, a
different mechanism). Wiring `fleet stubs resolve` (this D-number's own scope) remains necessary
but is no longer sufficient on its own for §12.37 DONE — `docs/CRITERIA_PLAN.md` §37's done bar
carries the corrected, fuller enumeration; this body is not rewritten, per this file's own
"annotate, never rewrite" convention.

**Fixed (round VI task 89, `dee859c`) — this D-number's own scope (the CLI verb has zero
implementation) is closed; three of the four remaining `docs/CRITERIA_PLAN.md` §37 done-bar
items are now also independently closed by the same task, though §12.37 itself is NOT flipped
DONE (see that file's own updated §37 entry for the residual).** `cli.stubs_resolve` now reuses
`_fire_t1_for_provider` unchanged, scoped to one operator-named provider, plus the D107 label
rewrite — real supersede-and-enqueue, not a stub. A real defect was caught and fixed during
implementation, not shipped: `_fire_t1_for_provider` never threaded `operator_triggered` to
`orchestrator.stubs.supersede`/`plan_revalidation`, so under `stubs.revalidation: manual`
neither `--sync` nor this verb could ever fire T1 (both functions require
`operator_triggered=True` under that policy specifically to let this verb be the one live
trigger, per their own docstrings) — fixed by adding the parameter (default `False`, so
`--sync`'s two call sites are behaviourally unchanged) and threading `True` only from this verb.

Of the four §37 done-bar items this file's own correction above named: **(1) D130's own wiring —
closed** (this fix). **(3) atomicity — closed**:
`tests/test_stub_resolution_task79.py::test_stubs_resolve_fires_t1_for_real_and_is_idempotent_
and_atomic` simulates a crash mid-`t1_unit`-transaction (forcing `insert_revalidation_task_row`
to raise after the stub `UPDATE` has run in the SAME `BEGIN IMMEDIATE` transaction) and asserts
NEITHER write is durable afterward, then resumes with a real, uninterrupted call. **(4a) the
exact-count clause — closed**: the same test asserts `COUNT(*) = 1` on the minted `REVALIDATE`
task, not merely non-null. **(4b) the zero-new-work idempotent-repeat clause — closed for the
repeat-trigger reading of it**: the same test's final section re-invokes `fleet stubs resolve`
on the now-`SUPERSEDED` stub and asserts zero new `tasks`/`stubs`/`attempts` rows and zero new
commits on `migrate/<consumer>`. **The literal "already_applied event... keyed on
revalidation_key" sub-phrase of (4b) was investigated, not merely left unasserted**:
`_run_one_revalidation_task` (`cli.py:13733`) re-runs `VerifyPipelineWorker` directly against the
already-rewritten tree — it never dispatches a phase-2/`apply_and_commit`-shaped step at all, so
there is no separate "already applied" EVENT for a REVALIDATE round's own phase-2 work to emit;
SPEC's "zero new phase-2 commits" reading holds vacuously by construction (REVALIDATE
structurally cannot produce a phase-2 commit), and "the revalidation_key is the proof" is what
the exact-count-plus-idempotent-repeat assertions above already establish. This reading is
disclosed as this task's own investigation, not a re-derivation confirmed against SPEC's
drafting intent.

**(2) stub creation via the real CLI, combined into this same resolution chain — still open**,
and out of this task's own scope (its brief named exactly three residual pieces — atomicity,
the exact count, and the idempotent-repeat assertion — matching (3)/(4a)/(4b) above, and did not
name (2)). `docs/CRITERIA_PLAN.md`'s own §37 entry is the more complete, independently-corrected
source for this item; §12.37 stays PARTLY ADDRESSED, not DONE, on that account. See this task's
own report (`.superpowers/sdd/round-VI-criteria-closure/task-89-report.md`) for the full
disclosure of the brief-vs-CRITERIA_PLAN discrepancy this task found and did not paper over.
