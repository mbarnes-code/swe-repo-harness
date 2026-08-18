#!/usr/bin/env bash
# Wire up .githooks/ as this repo's hook directory, for every checkout (primary + every linked
# worktree) at once.
#
# `core.hooksPath` lives in the SHARED .git/config -- the same file `git worktree add` gives
# every linked worktree a view of -- so setting it once here is enough for all of them. That is
# NOT the default: git's default hooks dir is $GIT_DIR/hooks, which for a linked worktree is the
# per-worktree administrative dir (.git/worktrees/<name>/hooks), untracked and never populated by
# `git worktree add`. core.hooksPath is how this repo gets ONE tracked, committed hook directory
# instead of N untracked ones. Verified empirically (see
# .superpowers/sdd/sdd-backlog-a/task-HOOK1-report.md): a hook dropped in the PRIMARY's
# .git/hooks/ already fires when committing from a linked worktree (hooks are read from the
# common dir by default, no config needed for that much) -- but that path is untracked, so it
# cannot ship in the repo. core.hooksPath is what makes the hook directory itself something
# `git clone` delivers.
#
# core.hooksPath is set to an ABSOLUTE path into the PRIMARY checkout's .githooks/, not a
# repo-relative \".githooks\". Reasoning: a relative core.hooksPath is resolved against whatever
# the CURRENT worktree has checked out at that path, on ITS OWN branch. An agent worktree
# created before this hook was merged into its base ref -- or one whose branch never picks up a
# later docs/tooling commit -- would have no .githooks/pre-commit on disk at all, and git SKIPS a
# missing hooks dir SILENTLY (verified: commit succeeds, no message, exit 0). An absolute path
# into the primary sidesteps that: the file the hook runs is always the primary's copy,
# regardless of what any given worktree's branch contains. The tradeoff, stated honestly: if the
# primary checkout is ever moved or renamed, core.hooksPath goes stale and this script must be
# re-run. That is a smaller, rarer failure mode than "a differently-branched worktree never gets
# the hook."
#
# core.hooksPath is LOCAL config (not committed -- .git/config never is), so a fresh clone (or
# a primary checkout moved to a new path) has NO hook enforcement until this script is run in
# it. Say so loudly rather than let that be discovered the hard way.

set -euo pipefail

die() { printf 'install-hooks: %s\n' "$*" >&2; exit 1; }

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
primary=$(CDPATH= cd -- "$script_dir/../.." && pwd -P)

# Must be run from what will BE the primary checkout: core.hooksPath below is pinned to this
# tree's own .githooks/, by design (see the block comment above). Refuse from a linked worktree
# rather than silently install a pointer at the wrong tree -- same primary/worktree test the
# hook itself uses (git-dir vs. git-common-dir, resolved to absolute paths).
git_dir=$(git -C "$primary" rev-parse --absolute-git-dir) \
  || die "could not resolve --absolute-git-dir under '$primary'; is this a git checkout?"
common_dir=$(CDPATH= cd -- "$(git -C "$primary" rev-parse --git-common-dir)" && pwd -P) \
  || die "could not resolve --git-common-dir under '$primary'."
[ "$git_dir" = "$common_dir" ] || die \
  "run this from the PRIMARY checkout, not a linked worktree ($primary looks like a worktree:
git-dir '$git_dir' != common-dir '$common_dir'). Installing core.hooksPath from inside a
worktree would still write to the shared config correctly, but the point of this guard is the
same one tools/worktree/new-worktree.sh makes: don't let a worktree's own (possibly stale) view
of the repo make a decision that affects every checkout."

hooks_dir="$primary/.githooks"
[ -d "$hooks_dir" ] || die "expected $hooks_dir to exist (it should be tracked in this repo)."
[ -x "$hooks_dir/pre-commit" ] || die \
  "$hooks_dir/pre-commit is missing or not executable. 'chmod +x' does not survive a fresh
clone's checkout in all cases -- if this is a fresh clone, run: chmod +x '$hooks_dir'/*"

git -C "$primary" config core.hooksPath "$hooks_dir"

printf 'installed: core.hooksPath = %s\n' "$hooks_dir"
printf 'this applies to the primary checkout AND every existing linked worktree immediately\n'
printf '(core.hooksPath lives in the shared .git/config) -- no per-worktree step needed.\n'
printf '\n'
printf 'this is LOCAL, uncommitted config. Re-run this script after every fresh clone, and\n'
printf 'after moving or renaming this checkout (core.hooksPath is an absolute path -- see the\n'
printf 'comment at the top of this script for why).\n'
