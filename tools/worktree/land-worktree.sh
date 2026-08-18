#!/usr/bin/env bash
# Land a finished agent worktree onto main: rebase in the lane, then fast-forward-only merge.
#
# See tools/worktree/README.md §6 for the decision and the alternatives it costs out. The short
# version: `git rebase main` in the lane keeps this project's linear `checkpoint NN:` history, and
# `git merge --ff-only` on main REFUSES rather than silently creating a merge commit if the rebase
# did not actually leave the branch fast-forwardable. That refusal is the whole safety property
# this script leans on — it does not try to be clever if either step fails.
#
# Usage:  tools/worktree/land-worktree.sh <task-id>
#
# What "current trunk" means here: this repo has an `origin` remote configured for `main`
# (`branch.main.remote=origin`), but `origin/main` is a stale mirror far behind local main (this
# project's real history is unpushed commits made directly here) and the remote requires
# credentials this host does not have — `git fetch origin` hangs on a username prompt in a
# non-interactive shell. So this script does NOT fetch. It treats the PRIMARY checkout's local
# `main` ref as trunk, which is correct here for a reason specific to git worktrees: a linked
# worktree shares the same git-dir (only HEAD is per-worktree), so refs — including `main` — are
# not copies, they are the same objects the worktree already sees. There is nothing to fetch
# between two checkouts of one repository.
#
# NB: this script deliberately exports no FLEET_* variable (see new-worktree.sh).
set -euo pipefail

die() { printf 'land-worktree: %s\n' "$*" >&2; exit 1; }
say() { printf '  %s\n' "$*"; }

[ $# -ge 1 ] || die "usage: $0 <task-id>"
task=$1

case "$task" in
  *[!A-Za-z0-9._-]*|"") die "task id must match [A-Za-z0-9._-]+ (got '$task')" ;;
esac

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
primary=$(CDPATH= cd -- "$script_dir/../.." && pwd -P)

# Must be run from the PRIMARY checkout: the ff-only merge below has to happen there (git refuses
# to check out the same branch — main — in two worktrees at once), so the merge step assumes it.
git_dir=$(git -C "$primary" rev-parse --absolute-git-dir)
common_dir=$(CDPATH= cd -- "$(git -C "$primary" rev-parse --git-common-dir)" && pwd -P)
[ "$git_dir" = "$common_dir" ] || die "run this from the primary checkout, not a linked worktree"

wt_home=${HARNESS_WORKTREE_HOME:-"$(dirname -- "$primary")/$(basename -- "$primary") worktrees"}
wt="$wt_home/wt-$task"
branch="agent/$task"

[ -d "$wt" ] || die "$wt does not exist (nothing to land)"
git -C "$primary" show-ref --verify --quiet "refs/heads/$branch" \
  || die "branch $branch does not exist"

# The primary must actually be sitting on main: the ff-only merge runs there, and if the primary
# were on some other branch (or detached) the merge would either fail confusingly or move the
# wrong ref.
primary_head=$(git -C "$primary" symbolic-ref --quiet --short HEAD || true)
[ "$primary_head" = "main" ] \
  || die "primary checkout is on '${primary_head:-<detached HEAD>}', not main — switch it to main
  before landing (this script performs the ff-only merge in the primary checkout)"

# Refuse a primary with uncommitted changes to TRACKED files: a fast-forward merge moves HEAD and
# rewrites working-tree files, and doing that on top of uncommitted edits to tracked files is
# exactly the kind of silent damage rm-worktree.sh and new-worktree.sh both already refuse
# elsewhere in this toolset. Untracked files are deliberately NOT part of this check — the SDD
# ledger convention (README §7.3) is to keep task briefs/reports untracked in the PRIMARY
# checkout, so the primary routinely carries untracked files with no bearing on the merge; `git
# merge --ff-only` itself will refuse, per file, if an untracked file would actually collide with
# an incoming path, which is the real hazard worth gating on.
primary_dirty=$(git -C "$primary" status --porcelain | grep -v '^??' || true)
[ -z "$primary_dirty" ] || die "primary checkout ($primary) has uncommitted changes to tracked
files — commit or stash them before landing:
$primary_dirty"

# Refuse a dirty worktree: uncommitted changes would be silently left behind by the rebase (they
# travel with the working tree, not the branch) and never make it onto main.
wt_dirty=$(git -C "$wt" status --porcelain)
[ -z "$wt_dirty" ] || die "worktree $wt has uncommitted changes — commit them first:
$wt_dirty"

# Refuse to resume into an unfinished rebase left by a previous failed landing attempt.
rebase_merge=$(git -C "$wt" rev-parse --git-path rebase-merge)
rebase_apply=$(git -C "$wt" rev-parse --git-path rebase-apply)
if [ -d "$rebase_merge" ] || [ -d "$rebase_apply" ]; then
  die "$wt has a rebase in progress already — resolve it (git -C \"$wt\" rebase --continue or
  --abort) before re-running this script"
fi

# Nothing to land?
main_before=$(git -C "$primary" rev-parse main)
to_land=$(git -C "$primary" rev-list --count "main..$branch")
[ "$to_land" -gt 0 ] || die "branch $branch has no commits ahead of main — nothing to land"

printf 'landing %s (%s commit(s) ahead of main) onto main\n' "$branch" "$to_land"

# ---------------------------------------------------------------------------------------
# Step 1: rebase the lane onto current trunk, IN THE WORKTREE.
#
# "Current trunk" is the primary's local main ref — see the header comment on why this script
# does not fetch. Because the worktree shares the primary's git-dir, `main` here is already that
# exact ref; no update step is needed or possible.
# ---------------------------------------------------------------------------------------
say "rebasing $branch onto main ($main_before)"
if ! git -C "$wt" rebase main; then
  cat >&2 <<EOF
land-worktree: rebase of $branch onto main CONFLICTED and was left in progress.

This script does not attempt automatic conflict resolution. Resolve it by hand:
  cd "$wt"
  git status                 # see the conflicting files
  <edit, then> git add <file>
  git rebase --continue
  (or: git rebase --abort    to give up and try again later)

Once the rebase is clean, re-run: $0 $task
EOF
  exit 1
fi
say "rebase clean"

# ---------------------------------------------------------------------------------------
# Step 2: verify trunk did not move out from under us between the rebase and the merge below
# (e.g. a concurrent landing in another lane). If it did, the branch may no longer be a fast-
# forward of the CURRENT main even though the rebase above succeeded against the OLD main.
# ---------------------------------------------------------------------------------------
main_now=$(git -C "$primary" rev-parse main)
[ "$main_now" = "$main_before" ] \
  || die "main advanced from $main_before to $main_now while landing $branch — re-run
  $0 $task to rebase onto the new tip (this indicates something else landed concurrently;
  this toolset assumes landings happen one at a time)"

# ---------------------------------------------------------------------------------------
# Step 3: the ff-only merge, from the PRIMARY checkout, not the worktree — git refuses to check
# out main in two places at once, and this merge moves main's HEAD, so it must run where main is
# actually checked out. --ff-only is load-bearing: if the rebase above did not truly leave
# $branch as a descendant of main (a logic error in this script, or a race), this REFUSES instead
# of silently fabricating a merge commit.
# ---------------------------------------------------------------------------------------
say "fast-forwarding main to $branch"
if ! git -C "$primary" merge --ff-only "$branch"; then
  die "git merge --ff-only $branch failed in the primary checkout even though the rebase in
  $wt reported success. This should not happen — it means $branch is not actually a
  fast-forward of main. Inspect by hand:
    git -C \"$primary\" log --oneline --graph main $branch -10
  Nothing has been changed by this failed merge attempt."
fi

landed_sha=$(git -C "$primary" rev-parse main)
printf '\nlanded %s..%s onto main\n' "${main_before:0:12}" "${landed_sha:0:12}"
git -C "$primary" log --oneline "$main_before..$landed_sha"

cat <<EOF

main is now at $landed_sha
worktree       $wt (unchanged; branch $branch now merged)
teardown       "$primary/tools/worktree/rm-worktree.sh" $task --delete-branch

This script did NOT run the test suite (CLAUDE.md: ~9 minutes, never two concurrent pytest
sessions). Run it yourself before trusting this landing:
  cd "$primary" && ./.venv/bin/pytest
EOF
