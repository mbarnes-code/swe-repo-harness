#!/usr/bin/env bash
# Tear down a worktree created by new-worktree.sh.
#
# Usage:  tools/worktree/rm-worktree.sh <task-id> [--force] [--delete-branch]
#
#   --force          remove even if the worktree has uncommitted changes (they are LOST)
#   --delete-branch  also delete agent/<task-id> (refused unless it is merged, or --force)
#
# The per-worktree Bazel root under $TMPDIR is NOT deleted. It is multiple GiB and deleting a
# repository cache is a deliberate act in this project (CLAUDE.md: a keep-ceiling breach fails
# the session and KEEPS the bytes; you prune with the rm -rf the failure names). The exact
# command is printed instead.
set -euo pipefail

die() { printf 'rm-worktree: %s\n' "$*" >&2; exit 1; }

[ $# -ge 1 ] || die "usage: $0 <task-id> [--force] [--delete-branch]"
task=$1; shift
force=0; delete_branch=0
for arg in "$@"; do
  case "$arg" in
    --force) force=1 ;;
    --delete-branch) delete_branch=1 ;;
    *) die "unknown option $arg" ;;
  esac
done

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
primary=$(CDPATH= cd -- "$script_dir/../.." && pwd -P)
wt_home=${HARNESS_WORKTREE_HOME:-"$(dirname -- "$primary")/$(basename -- "$primary") worktrees"}
wt="$wt_home/wt-$task"
branch="agent/$task"

[ -d "$wt" ] || die "$wt does not exist"

# Refuse to discard work silently. This is the whole point of the lane isolation.
dirty=$(git -C "$wt" status --porcelain)
if [ -n "$dirty" ] && [ "$force" -eq 0 ]; then
  die "worktree has uncommitted changes — commit them, or re-run with --force to LOSE them:
$dirty"
fi

unmerged=$(git -C "$primary" rev-list --count "main..$branch" 2>/dev/null || echo 0)
if [ "$unmerged" -gt 0 ] && [ "$force" -eq 0 ]; then
  printf 'rm-worktree: NOTE %s has %s commit(s) not on main; the branch is kept.\n' \
    "$branch" "$unmerged" >&2
fi

digest=$(printf '%s' "$wt" | sha256sum | cut -c1-12)
bazel_root="${TMPDIR:-/tmp}/fleet-bazel-$digest"

if [ "$force" -eq 1 ]; then
  git -C "$primary" worktree remove --force "$wt"
else
  git -C "$primary" worktree remove "$wt"
fi
git -C "$primary" worktree prune
printf 'removed %s\n' "$wt"

if [ "$delete_branch" -eq 1 ]; then
  if [ "$force" -eq 1 ]; then
    git -C "$primary" branch -D "$branch"
  else
    git -C "$primary" branch -d "$branch" \
      || die "branch $branch is not merged; merge it or re-run with --force --delete-branch"
  fi
  printf 'deleted branch %s\n' "$branch"
else
  printf 'branch %s kept (add --delete-branch to remove it)\n' "$branch"
fi

if [ -d "$bazel_root" ]; then
  printf '\nThis worktree left Bazel state behind (%s). Nothing is running against it now;\nprune it deliberately when you want the space back:\n  rm -rf %s\n' \
    "$(du -sh -- "$bazel_root" | cut -f1)" "$bazel_root"
fi
