# Per-agent git worktrees

One isolated checkout per development subagent, so `git add` / `git commit` in one lane cannot
sweep up another lane's staged work.

```sh
tools/worktree/new-worktree.sh        <task-id> [base-ref]     # create + provision + verify
tools/worktree/land-worktree.sh       <task-id>                 # rebase onto main + ff-only merge (§6)
tools/worktree/rm-worktree.sh         <task-id> [--force] [--delete-branch]
tools/worktree/provision-existing.sh  <worktree-path> [primary] # provision a worktree you did NOT create with new-worktree.sh
```

A brief can then say **"work in `/home/redmage/swe repo harness worktrees/wt-<task-id>`"** and
that directory is ready: clean `git status`, its own branch, a working `.venv`, the vendored
toolchains, and its own `BAZEL_ROOT`.

**If your worktree was created some other way** (an orchestrator's own dispatch tooling, a
manual `git worktree add`, a worktree at a scratchpad path with no space in it) — run
`tools/worktree/provision-existing.sh <path-to-your-worktree>` once, from anywhere. It provisions
in place: symlinks `tools/bin/{bazel,ast-grep,gh}`, `tools/go`, `tools/rust`, `tools/bazelisk`,
and clones+rewrites `.venv`, the same as `new-worktree.sh` does for a worktree it creates itself
(they share the provisioning code in `lib-provision.sh`). Idempotent — safe to re-run. Round VI
tasks 90, 94 and 98 each independently hit real-Bazel/real-git-filter-repo fixtures failing with
`git-filter-repo is not on PATH` / `'uv' is not installed` in exactly this situation and fixed it
by hand; this script is that fix, made reusable. Unlike `new-worktree.sh`, it does not refuse a
space-free worktree path (see §3) — it warns instead, since a space-free scratchpad path is the
common case for how tasks actually get dispatched in this project, not an edge case to reject.

---

## 1. Layout

| | |
|---|---|
| Parent | `<repo-parent>/<repo-name> worktrees/` — i.e. `/home/redmage/swe repo harness worktrees/` |
| Name | `wt-<task-id>` |
| Branch | `agent/<task-id>` |
| Granularity | **per task**, created and destroyed with the task |

**Outside the repository, not `.worktrees/`.** `.gitignore` already carries a `/.worktrees/`
line, so an in-repo layout would not show in `git status` — but it would still be inside every
Bazel glob, every `ast-grep` scan, and every `grep -r` an agent runs against the tree, and each
worktree would contain a copy of every sibling. A sibling directory costs nothing and is invisible
to all of it.

**The parent path contains a space, deliberately.** See §3 — this is what keeps multi-GiB Bazel
state out of the worktree. `new-worktree.sh` refuses to create a worktree at a space-free path
rather than silently producing one that pollutes `git status`.

**Per task, not a fixed pool.** A pool has to be reset between users, and "reset" is the step
that gets skipped; a stale pooled worktree carrying the previous task's branch and untracked
files reintroduces exactly the cross-lane confusion this removes. Creation is ~2 seconds and
~13 MiB (measured, §5), so there is no reason to reuse one.

---

## 2. What a fresh worktree lacks, and how it is provisioned

`git worktree add` carries **tracked, committed** files only. Everything below is git-ignored, so
none of it arrives on its own. `new-worktree.sh` provisions all of it.

| Missing | Provisioning | Why that way |
|---|---|---|
| `.venv/` (263 MiB) | **hardlink copy** of the primary's, with its 26 absolute-path files rewritten | see below |
| `tools/bin/{bazel,ast-grep,gh}` | symlink to primary | ignored by `tools/bin/*`; read-only binaries. Only the four wrapper *scripts* (`cargo`, `gazelle`, `go`, `rustc`) are tracked and arrive with the checkout |
| `tools/go/`, `tools/rust/` (1.7 GiB) | real dir + symlinked children | wrappers derive `GOROOT`/`GOPATH`/`GOCACHE`/`RUSTUP_HOME`/`CARGO_HOME` from their own location, so symlinks suffice; these caches are concurrency-safe |
| `tools/bazelisk/downloads` (120 MiB) | symlink to primary | pure release-archive cache, safe to share |
| `tools/bazelisk/output` (353 MiB) | **own empty directory** | it is a live Bazel `--output_user_root` (install base, MD5-keyed output bases, lock files). Sharing it across concurrent worktrees is precisely the collision this setup exists to prevent |
| `references/*/` | **not provisioned** | read-only citation corpora; read the primary's copy by absolute path |

Note the symlink shape: the *parent* is a real directory and only the children are symlinks.
`.gitignore` says `tools/go/`, and a trailing-slash pattern matches only real directories — git
treats a symlink as a file, so `ln -s .../tools/go` would appear as an untracked entry and
undo the clean `git status`.

### Why the venv is copied and rewritten, not symlinked and not re-synced

**Not symlinked.** `.venv/lib/python3.12/site-packages/_editable_impl_fleet.pth` is a single
line: `/home/redmage/swe repo harness/src`. Under a symlinked venv:

* `pytest` would still be correct — `pyproject.toml` sets `pythonpath = ["src"]` (rootdir-relative)
  and `tests/conftest.py` does `sys.path.insert(0, REPO_ROOT/"src")`, both of which put the
  worktree's `src` first;
* but `.venv/bin/fleet` and any plain `python -c "import fleet"` would import the **primary's**
  `src/`. An agent would edit its own worktree and exercise someone else's code, with nothing
  visible to say so. That is a worse failure than the one being fixed.

**Not `uv sync`.** Two facts, both checked in this repo:

* **there is no `uv.lock`** (contrary to a common assumption — `ls uv.lock` → no such file), and
* every pin in `pyproject.toml` is a floor (`pydantic>=2.11`, `pytest>=8.3`, …).

So a fresh resolve is a *network* operation that can legitimately install different versions than
the primary is tested against — lanes would drift from the trunk and from each other. Note also
that `uv` is not on `PATH` at all on this host; the only copy is `.venv/bin/uv`.

The hardlink copy is offline, ~2 seconds, byte-identical to the primary, and — because `sed -i`
writes a temp file and renames — the rewritten files get fresh inodes rather than corrupting the
primary's copies. The script **verifies** the outcome: it fails the creation if
`import fleet` resolves outside the worktree.

Caveat: because the environments are hardlinked, do **not** `pip install` into a worktree venv
expecting the primary to be unaffected in reverse. Installing writes new files (safe); uninstall
and in-place patching are not. If a lane needs a new dependency, add it in the primary.

---

## 3. `BAZEL_ROOT` — the question that decides whether parallel testing is safe

`CLAUDE.md` warns: *"Never run two pytest sessions concurrently. `pytest_sessionfinish` deletes
every `BAZEL_ROOT` child except `repos/`, so parallel sessions reap each other's output bases."*

**`BAZEL_ROOT` is per-checkout.** `tests/conftest.py:216`:

```python
def _bazel_root() -> Path:
    inside = REPO_ROOT / "tools" / "bazel-test-root"
    if " " not in str(inside):
        return inside
    digest = sha256(str(REPO_ROOT).encode("utf-8")).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / f"fleet-bazel-{digest}"
```

`REPO_ROOT` is `Path(__file__).resolve().parents[1]`, and in a linked worktree `tests/conftest.py`
is a real file inside that worktree — so `REPO_ROOT` is the worktree. Verified by importing the
real module from both checkouts:

```
primary : /home/redmage/swe repo harness                            -> /tmp/fleet-bazel-160624fb8a4b
worktree: /home/redmage/swe repo harness worktrees/wt-WT1-example   -> /tmp/fleet-bazel-b3bb148a067c
```

**So the reaping collision the warning describes does not occur between worktrees.**
`pytest_sessionfinish` iterates `BAZEL_ROOT.iterdir()`, and two sessions in two worktrees are
iterating two different directories. The peak sampler, the `repos/` keep-ceiling, the
`--output_user_root`, the `XDG_CACHE_HOME` redirect and the no-C-compiler root are all children of
`BAZEL_ROOT` and are therefore isolated too.

**That is not the same as "parallel testing is now safe."** It removes one of at least three
blockers. See §4 and §6 before running two suites at once, and treat that as a change that needs
its own measured verification, not an assumption inherited from this document.

### The space-free trap

The `inside` branch of `_bazel_root()` is currently dead code *only because the primary checkout's
path happens to contain a space*. Put a worktree at a space-free path and it comes alive:
`BAZEL_ROOT` becomes `<worktree>/tools/bazel-test-root` — **multiple GiB of Bazel state inside the
worktree, matched by no `.gitignore` rule**, showing up in `git status` and one `git add -A` from
being committed. `new-worktree.sh` refuses such a path with an explanation.

*Recommended follow-up (not done here — it edits a tracked file):* add `tools/bazel-test-root/` to
`.gitignore`. It costs one line and closes the trap independently of path spelling.

---

## 4. The repository cache

`repos/` — the `--repository_cache`, the one directory `pytest_sessionfinish` deliberately keeps —
lives **under `BAZEL_ROOT`**, so it is per-checkout too. Consequences:

* **Every new worktree starts with a cold cache.** Its first real-Bazel test run re-fetches the
  ruleset archives (`CLAUDE.md`: ~206 MB / 9,241 files, 179 s vs 17 s warm; `conftest.py` measures
  the fully-populated cache at 1595 MiB). That is a per-worktree, one-off cost in time, network,
  and up to ~1.6 GiB of disk.
* **Each worktree carries its own 3 GiB keep-ceiling** (`BAZEL_KEEP_CACHE_CEILING_BYTES`), and a
  breach fails that session and keeps the bytes. Five worktrees = five caches to prune.

**Optional sharing — deliberately NOT enabled by default.** Symlinking `$BAZEL_ROOT/repos` at a
single shared cache would work in principle: a Bazel repository cache is content-addressed and
designed to be shared across workspaces, and `pytest_sessionfinish` compares `child != kept` by
path, so the symlink is skipped by the reaper exactly like a real directory. It is not enabled
because it changes what the keep-ceiling measures (every session would then be charged for the
shared cache) and because that claim is unverified here — verifying it means running the suite,
which this setup could not do. **The check to run, once, when the machine is idle:** create a
worktree, `ln -s <shared-repos> "$BAZEL_ROOT/repos"`, run `-m integration` in it, and confirm the
`bazel disk` line reports a plausible `repository cache kept` figure and no `DISK CEILING
BREACHED`. Until then, cold caches are the honest default.

Unrelated but worth repeating from `CLAUDE.md`: a command-line `--repository_cache` **overrides**
a `.bazelrc` `common` line. This repo has **no `.bazelrc` at all** (checked) — the cache flags come
from `src/fleet/settings.py` (`repository_cache: str = "cache/bazel/repo"`, relative, so
per-checkout) and from the conftest fixtures. Nothing needs to change per worktree.

---

## 5. Disk

Measured on this host, for the example worktree:

| | |
|---|---|
| Checkout (source, excluding `.venv`) | 9.3 MiB |
| `.venv` **incremental** (hardlinked; `du -csh` over both venvs = 267 MiB vs 263 MiB for the primary alone) | 3.9 MiB |
| Toolchains, `tools/bin` binaries, bazelisk downloads | 0 (symlinked) |
| **Total per worktree, before any test run** | **~13 MiB** |

That is cheap. What is **not** cheap is running the suite in one: `conftest.py` records a peak of
3.36 GiB per session (ceiling 6 GiB) plus up to 3 GiB kept in `repos/`.

**`/` is at 95% with ~30 GB free.** A full suite has already driven this host to 0 bytes free once
(the incident documented at `tests/conftest.py:196`). Two concurrent suites is ~8 GiB and probably
fine; five is ~20 GiB of transient state on 30 GiB free, and there is no ceiling anywhere that
sees the *sum* — each session polices only its own `BAZEL_ROOT`. **Treat "N agents each running
the full suite" as the thing to not do**, independently of the reaping question.

---

## 6. Branch model — decided: rebase, then `--ff-only` merge

A worktree **must** be on its own branch: git refuses to check out `main` in two worktrees at
once. So the branchless status quo is not available here; `new-worktree.sh` creates
`agent/<task-id>` from `HEAD`.

This project's entire history is direct commits to `main` (`checkpoint NN: …`), so how lane
branches rejoin it is a real decision with consequences for how checkpoints read. Three models
were costed out:

* **A — rebase + `--ff-only` merge (chosen).** The orchestrator does `git rebase main` in the
  lane, then `git merge --ff-only agent/<task>` on `main`. History stays linear and the checkpoint
  style survives untouched. Cost: the orchestrator resolves conflicts serially, one lane at a
  time. **That cost is accepted** — it is arguably where contention belongs anyway, since it is
  the same place `docs/INTEGRATION_HONESTY.md` contention already lands (§7).
* **B — `git merge --no-ff` per lane.** Rejected. Preserves each lane as a visible unit and
  records who did what, but `main` becomes a braid and `git log --oneline` no longer reads as the
  checkpoint ledger it currently is — every `docs/PROGRESS.md` section and every ledger SHA-pin
  assumes a linear `checkpoint NN:` history.
* **C — cherry-pick the lane's commits onto `main`.** Rejected. Keeps linearity with no rebase in
  the lane, but produces duplicate SHAs (the cherry-picked commit and the original lane commit are
  different objects with the same message), and the lane branch is then a lie about what actually
  landed on `main`.

`rm-worktree.sh` refuses to drop a worktree with uncommitted changes and warns when the branch has
commits not on `main`, so no model can silently lose work.

### Landing procedure

```sh
tools/worktree/land-worktree.sh <task-id>
```

Run from the primary checkout once a lane's work is committed and ready. It:

1. Refuses if the worktree (or the primary checkout) has uncommitted changes.
2. Refuses if `agent/<task-id>` has no commits ahead of `main` — nothing to land.
3. Rebases `agent/<task-id>` onto `main`, **in the worktree**. If that conflicts, it stops and
   leaves the rebase in progress for the operator to resolve by hand (`git rebase --continue` or
   `--abort` in the worktree) — this script never attempts automatic conflict resolution.
4. Fast-forward-merges `agent/<task-id>` onto `main`, **from the primary checkout** (git refuses
   to check out `main` in the worktree too, since the primary already has it checked out).
   `--ff-only` is the safety property this whole model rests on: if the rebase did not actually
   leave the branch as a fast-forward of `main`, the merge refuses rather than silently creating a
   merge commit.
5. Prints the landed SHA range and the commit subjects that landed.

**On "fetch main first":** this repo has an `origin` remote configured for `main`
(`branch.main.remote=origin`, pointed at the local Gitea mirror), but `origin/main` is a stale
copy far behind local `main` — this project's real history is unpushed commits made directly in
the primary checkout — and `git fetch origin` hangs on a credential prompt in a non-interactive
shell on this host. `land-worktree.sh` does not fetch. It treats the primary checkout's local
`main` as trunk, which is exactly right for worktrees specifically: a linked worktree shares its
git-dir with the primary (only `HEAD` is per-worktree), so `main` is not a copy that can go
stale between the two checkouts — the worktree already sees the same ref the primary does, with
no sync step possible or needed.

`land-worktree.sh` deliberately does **not** run the test suite before or after landing (~9
minutes, and `CLAUDE.md` forbids two concurrent pytest sessions). Run it yourself, in the primary
checkout, after landing:

```sh
cd "/home/redmage/swe repo harness" && ./.venv/bin/pytest
```

If a lane's rebase conflicts, it is a real conflict between two lanes' work — `land-worktree.sh`
will not paper over it, and re-running it after resolving is exactly the retry path.

---

## 7. What this does NOT fix

1. **`docs/INTEGRATION_HONESTY.md` contention is converted, not removed.** Every lane still wants
   to append to the same file. The failure mode changes from *"agent B's `git commit` swallowed
   agent A's staged hunk"* (silent, destructive) to *"lane B does not merge cleanly"* (loud,
   non-destructive, resolved at integration time by one actor). That is a genuine improvement and
   it is not the same as solving it. If the append-conflict cost is still too high, the fix is
   structural — one file per finding under a directory, collated at checkpoint time — not more
   worktrees.
2. **Parallel pytest is still not endorsed.** `BAZEL_ROOT` is isolated (§3), but disk is shared and
   unpoliced in aggregate (§5), the docker daemon is a single global namespace shared by every
   lane's sandbox tests, and no one has measured a concurrent run. `CLAUDE.md`'s rule stands until
   someone does.
3. **The `.superpowers/` SDD workspace does not travel, and does not survive.** It is git-ignored
   working scratch: briefs, review packages, reviews and the round ledger. A fresh worktree sees
   none of it, so a per-worktree ledger would fragment the record. **Keep the ledger in the primary
   checkout**: briefs should tell agents to write reports to the primary's absolute path, not to
   `.superpowers/` relative to their worktree.
   **Anything another document cites must be promoted to `docs/superpowers/plans/` before the
   workspace is deleted at round close.** Round B nearly shipped this defect twice: five committed
   references (two of them in ADRs) pointed into git-ignored scratch, and `.githooks/pre-commit`
   and `install-hooks.sh` cited a report that was never tracked at all. A citation into
   `.superpowers/` is a dangling reference waiting for the next cleanup.
4. **Uncommitted work in the primary does not travel either.** `new-worktree.sh` branches from
   `HEAD`. If the primary has uncommitted changes an agent needs, commit them first or pass an
   explicit `base-ref`.
5. **Shared caches are shared.** `GOCACHE`, `GOMODCACHE`, `CARGO_HOME` and the bazelisk download
   cache are one copy across all lanes by design. They are concurrency-safe (cargo takes locks;
   the go caches are built for it), but a lane that corrupts one corrupts it for everyone, and a
   `go env -w` in one lane is visible in all of them.
6. **Agents can still reach outside their worktree.** Nothing here sandboxes anything — an
   absolute path into the primary works from any lane. This removes *accidental* cross-lane
   staging, not deliberate or careless writes.
