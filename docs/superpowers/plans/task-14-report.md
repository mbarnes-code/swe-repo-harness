# Task 14 report — Q1 fix: hermetic `test_two_rust_repos_in_one_wave_both_build`

**Status: done.** Implemented the fix, verified it, `ruff`/`mypy` clean.

**Approach chosen: commit `MODULE.bazel.lock` as fixture data (not generate-at-setup), and why.**
Generating it at test setup would re-run the exact networked `cargo fetch` this task exists to
remove — self-defeating. A committed lock is the only shape that actually removes the network
dependency: research measured a build with it present succeeding offline in 32.4 s and reproduced
today's flake byte-for-byte without it. Captured once from a real networked `bazel build` of this
same two-crate fixture (`only_repos(["acme-codec-rs","acme-case-rs"])`, `BCR_DEFAULT_REGISTRY`),
stored at `tests/fixtures/rust/MODULE.bazel.lock`, and now written + committed onto `monorepo`'s
`integration` branch (same pattern the test already uses for `.bazelrc`) before `real_build` runs,
so the very first `crate.from_cargo` evaluation of the fresh worktree replays instead of splicing.

**`rust.py`'s missing `lockfile=` attribute: left alone, deliberately.** It is the structurally
correct production fix (the `maven_install.json` analogue) but needs a `cargo-bazel-lock.json`
producer this harness doesn't have — a new design (ADR), out of scope here. Documented this
explicitly in the fixture's docstring rather than silently doing nothing.

**Evidence the network dependency is gone (not just "it happened to pass"):** added
`_assert_no_cargo_splice`, which reads the FULL untruncated bazel stdout/stderr logs
(`<fleet>/artifacts/logs/**/*.std*.log`, not `attempts.stderr_tail`'s 32 KiB tail) and fails loudly
if either literal line `cargo-bazel splice` prints before touching a socket
(`Updating crates.io index`, `Splicing Cargo workspace`) appears anywhere. Ran the single permitted
test twice: once as a baseline (no fixture, real network, captured the lock), once with the fixture
seeded — both passed (89.0 s / 86.2 s), and in the second run the 4 real bazel logs show
`Build completed successfully, 233/2/2/233 total actions` with zero splice markers. Separately
re-ran a fully offline `bazel build //...` against the committed checkout (fresh output base, warm
`--repository_cache`) outside pytest and it built green in 34.6 s, 241 actions — confirming the
lock alone (no live `fleet build`) makes the tree buildable. (A network-namespace / proxy-blackhole
block to make `cargo` itself literally unreachable was attempted per research's own methodology but
the harness's own permission system denies `http_proxy=127.0.0.1:9`-style invocations; the
log-marker mechanism is the fallback the task explicitly permits and was used instead.) Also
confirmed `_publish_module_lock`'s post-build write was byte-identical to the seeded fixture
(idempotent, no new commit) and zero `module_lock_foreign_registry` findings — corroborating replay,
not re-splice.

**Files changed:** `tests/test_build_e2e.py` (+99 lines: `RUST_MODULE_LOCK_FIXTURE`,
`_assert_no_cargo_splice`, seed-and-commit + guard call in the test); new
`tests/fixtures/rust/MODULE.bazel.lock` (76,471 bytes, no host-specific paths). `rust.py` untouched.

**Commands run:** `.venv/bin/ruff check src/ tests/`; `.venv/bin/mypy src/fleet/ --strict`; twice
`pytest -x -q tests/test_build_e2e.py::test_two_rust_repos_in_one_wave_both_build` (explicitly
permitted); `tools/bin/bazel build //... --keep_going` directly against a copied checkout. No
`FLEET_*` exported, no `sudo`, `references/` untouched, nothing committed to git.
