# Task WT1 — git worktree infrastructure for development subagents

**STATUS: DONE.** Worktrees are the right tool here, with one large caveat that is *not* the one
the brief expected (§1). Full design and operating notes: `tools/worktree/README.md`.

Deliverables:

| | |
|---|---|
| Create | `/home/redmage/swe repo harness/tools/worktree/new-worktree.sh` |
| Tear down | `/home/redmage/swe repo harness/tools/worktree/rm-worktree.sh` |
| Design + limits | `/home/redmage/swe repo harness/tools/worktree/README.md` |
| Working example | `/home/redmage/swe repo harness worktrees/wt-WT1-example` (branch `agent/WT1-example`) |

---

## 1. The hard question: `BAZEL_ROOT` is PER-CHECKOUT

`tests/conftest.py:216`:

```python
def _bazel_root() -> Path:
    inside = REPO_ROOT / "tools" / "bazel-test-root"
    if " " not in str(inside):
        return inside
    digest = sha256(str(REPO_ROOT).encode("utf-8")).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / f"fleet-bazel-{digest}"
```

`REPO_ROOT = Path(__file__).resolve().parents[1]` (line 37). In a linked worktree
`tests/conftest.py` is a real file inside that worktree, so `REPO_ROOT` is the worktree — under
*either* branch of the `if`. Not inferred; verified by importing the real module from both
checkouts:

```
primary : /home/redmage/swe repo harness                            -> /tmp/fleet-bazel-160624fb8a4b
worktree: /home/redmage/swe repo harness worktrees/wt-WT1-example   -> /tmp/fleet-bazel-b3bb148a067c
```

**So `pytest_sessionfinish` cannot reap another worktree's output bases** — it iterates
`BAZEL_ROOT.iterdir()`, and two sessions in two worktrees iterate two different directories. The
peak sampler, the `repos/` keep-ceiling, `--output_user_root`, the `XDG_CACHE_HOME` redirect and
`NO_C_COMPILER_OUTPUT_USER_ROOT` are all children of `BAZEL_ROOT` and isolated with it. Corroborating
evidence that this already happens in practice: `/tmp` currently holds three distinct
`fleet-bazel-*` roots (3.9 G, 948 M, 2 M).

**This does not make parallel pytest safe.** It removes one of three blockers. The other two:

* **Disk (the real headline).** `/` is at **95%, ~30 GB free**. `conftest.py` documents a measured
  per-session peak of **3.36 GiB** (ceiling 6 GiB) plus up to 3 GiB kept in `repos/`. Every ceiling
  in the suite polices only *its own* `BAZEL_ROOT`; **nothing anywhere sees the sum across
  sessions**. Two concurrent suites (~8 GiB) is probably fine; five (~20 GiB) on 30 GiB free is how
  this host reached 0 bytes free once already (`conftest.py:196`). N-agents-each-running-the-suite
  remains the thing to not do.
* **Docker.** Every lane's sandbox tests drive one daemon and one container namespace. Names are
  `run_id`-derived so collisions are unlikely, but it is shared global state that no worktree isolates.

**Verdict: keep `CLAUDE.md`'s "never two pytest sessions concurrently" rule as written.** It is now
true for a different, better reason (disk and docker, not reaping). Lifting it needs a *measured*
concurrent run, which this task could not do — a suite was running throughout.

### A trap this created and closed

The `inside` branch of `_bazel_root()` is dead code *only because the primary checkout's path
contains a space*. A worktree at a space-free path revives it: `BAZEL_ROOT` becomes
`<worktree>/tools/bazel-test-root` — **GiB of Bazel state inside the worktree, matched by no
`.gitignore` rule**, visible in `git status`, one `git add -A` from being committed. Since
cross-lane commit accidents are the entire reason for this task, the layout puts worktrees under a
path that contains a space (parity with the primary → `/tmp`), and `new-worktree.sh` **refuses** a
space-free path with an explanation rather than papering over it.

*Recommended follow-up, deliberately not done (it edits a tracked file):* add
`tools/bazel-test-root/` to `.gitignore`.

## 2. Repository cache: per-worktree, cold on first use

`repos/` lives under `BAZEL_ROOT`, so it is per-checkout too. Each new worktree's first real-Bazel
run re-fetches the ruleset archives (~206 MB / 9,241 files, 179 s vs 17 s warm; a full cache
measures 1595 MiB in `conftest.py`), and each carries its own 3 GiB keep-ceiling.

Sharing one cache via a symlink at `$BAZEL_ROOT/repos` should work — the cache is content-addressed
and `pytest_sessionfinish` skips `kept` by path comparison, so a symlink is skipped like a real
directory. **Not enabled**, because it changes what the keep-ceiling measures and the claim is
unverified without a test run. The exact one-off check to run when the machine is idle is written
down in `tools/worktree/README.md` §4.

Also fact-checked: **this repo has no `.bazelrc` at all**, so the `--repository_cache`-overrides-
`.bazelrc` hazard has nothing to override here. The flags come from
`src/fleet/settings.py` (`repository_cache: str = "cache/bazel/repo"` — relative, so per-checkout)
and the conftest fixtures. Nothing needs per-worktree configuration.

## 3. What a fresh worktree lacks — two corrections to the brief

The brief's premises were partly wrong; both corrections mattered.

* **`tools/bin/` is only *partly* tracked.** Only the four wrapper *scripts* (`cargo`, `gazelle`,
  `go`, `rustc`) are tracked; `.gitignore` has `tools/bin/*` with four `!` exceptions, so
  **`bazel`, `ast-grep` and `gh` are ignored binaries and do not arrive**. Worse, the tracked
  wrappers derive `GOROOT`/`RUSTUP_HOME`/etc. from their own location, so they are inert without
  `tools/go/` and `tools/rust/` — 1.7 GiB of ignored toolchain. `config/` is fully tracked, as stated.
* **There is no `uv.lock`.** `ls uv.lock` → no such file. Combined with every pin in
  `pyproject.toml` being a floor (`>=`), a per-worktree `uv sync` is a *network* resolve that can
  install different versions than the primary is tested against — lanes drifting from trunk.
  (`uv` is also not on `PATH` on this host at all; the only copy is `.venv/bin/uv`.)

**So the venv is neither symlinked nor re-synced: it is a hardlink copy with its absolute paths
rewritten.** Symlinking is the trap worth naming — `_editable_impl_fleet.pth` is one absolute line
pointing at the primary's `src/`. Under `pytest` the worktree would still win (`pythonpath=["src"]`
is rootdir-relative and conftest does `sys.path.insert(0, ...)`), but `.venv/bin/fleet` and any
plain `python -c "import fleet"` would import the **primary's** source. An agent would edit its own
lane and exercise someone else's code, silently. `new-worktree.sh` fails creation if
`import fleet` resolves outside the worktree.

Provisioning summary: `.venv` hardlink-copied + 26 path-bearing files rewritten; `tools/bin/{bazel,
ast-grep,gh}` symlinked; `tools/go`, `tools/rust`, `tools/bazelisk/downloads` shared via
symlinked *children* under a real parent directory (a trailing-slash `.gitignore` pattern does not
match a symlink, so symlinking the directory itself would show as untracked);
`tools/bazelisk/output` given its own empty directory because it is a live Bazel output root with
lock files, not a cache; `references/*/` intentionally not provisioned.

**Measured cost per worktree: ~13 MiB** — 9.3 MiB of checkout plus 3.9 MiB of unshared venv
(`du -csh` over both venvs = 267 MiB against 263 MiB for the primary alone). Creation takes ~2 s.

## 4. `.superpowers/` — keep the ledger in the primary

Only **8** files under `.superpowers/sdd/sdd-backlog-a/` are tracked; ~32 more (all briefs, both
reviews, `progress.md`) are untracked. A fresh worktree therefore sees almost none of the ledger,
and writing reports into per-worktree `.superpowers/` would fragment the record across branches.

**Recommendation: briefs should point agents at the primary's absolute
`.superpowers/sdd/...` path for reports, not at a worktree-relative one.** This report is written
there.

## 5. Branch model — flagged, not decided

A worktree **must** have its own branch (git refuses to check out `main` twice), so the branchless
status quo is not available. `new-worktree.sh` creates `agent/<task-id>` from `HEAD`.

**Recommended: option A — rebase in the lane, then `git merge --ff-only` on `main`.** History stays
linear and the `checkpoint NN:` ledger that `git log --oneline` currently reads as survives intact.
The cost is that the orchestrator resolves conflicts serially, one lane at a time — which is the
right place for it, since that is exactly where the `INTEGRATION_HONESTY.md` contention lands.
Alternatives (`--no-ff` per lane; cherry-pick) and their costs are laid out in
`tools/worktree/README.md` §6. **This is your call — the scripts assume none of the three.**

Related: `new-worktree.sh` branches from `HEAD`, so **uncommitted primary work does not travel**.
Commit first, or pass an explicit `base-ref`.

## 6. Honest limits

1. **`INTEGRATION_HONESTY.md` contention is converted, not removed** — from *"agent B's commit
   swallowed agent A's staged hunk"* (silent, destructive) to *"lane B will not merge cleanly"*
   (loud, non-destructive, resolved once by one actor). Real improvement; not a solution. If the
   append-conflict cost is still too high, the fix is structural (one file per finding, collated at
   checkpoint), not more worktrees.
2. **Parallel pytest still not endorsed** (§1) — disk and docker, not reaping.
3. **Cold repository cache per worktree** (§2) — ~3 minutes of network on a lane's first Bazel run.
4. **Shared caches stay shared** — `GOCACHE`, `GOMODCACHE`, `CARGO_HOME`, bazelisk downloads are one
   copy for all lanes. Concurrency-safe, but a lane that corrupts one corrupts it for everyone.
5. **Nothing is sandboxed.** An absolute path into the primary works from any lane. This removes
   *accidental* cross-lane staging, not careless or deliberate writes.
6. **Don't `pip install` into a worktree venv** — it is hardlinked to the primary's.

## 7. Verification performed (no pytest — a suite was running)

* `git worktree list` shows both checkouts on their own branches.
* `git status --porcelain` in the example worktree: **empty**. Nothing provisioned leaks into it —
  this specifically confirms the symlinked-children shape defeats the trailing-slash `.gitignore`
  problem.
* `.venv/bin/python -c "import fleet"` from the worktree →
  `.../wt-WT1-example/src/fleet/__init__.py` (the worktree's own source, not the primary's).
* `.venv/bin/fleet --help` exits 0 in the worktree.
* Real `tests/conftest.py` imported from both checkouts: distinct `BAZEL_ROOT` values (§1).
* Teardown round-trip on a throwaway `wt-WT1-smoke`: refused with exit 1 while dirty, then
  `--force --delete-branch` removed the worktree, pruned, and deleted the branch cleanly.

**Left for you (needs a test session):** run `-m integration` once inside a worktree and confirm the
`bazel disk` line reports against `/tmp/fleet-bazel-<worktree-digest>` with `xfail: 0` and no
`DISK CEILING BREACHED`. That is the only claim in this report that rests on reading the code rather
than on running it.
