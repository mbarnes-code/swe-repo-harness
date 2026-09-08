#!/usr/bin/env bash
# Create an isolated git worktree for one development subagent.
#
# See tools/worktree/README.md for the design and the honest limits. The short version:
#
#  * The worktree lives OUTSIDE the repository, in a sibling directory whose path CONTAINS A
#    SPACE. That is load-bearing, not cosmetic — see the `no space in path` guard below.
#  * Everything a fresh worktree lacks (the venv, the vendored toolchains, the non-wrapper
#    binaries in tools/bin) is provisioned here, because all of it is git-ignored and a
#    `git worktree add` therefore does not carry it.
#  * The provisioned venv is a HARDLINK COPY of the primary's with its absolute paths rewritten,
#    NOT a symlink and NOT a fresh resolve. A symlink would make `.venv/bin/fleet` and any
#    `python -c "import fleet"` import the PRIMARY checkout's src/ (the editable-install .pth is
#    a static absolute path), i.e. an agent would test code it did not edit. A fresh resolve is
#    not possible offline and would drift: there is no uv.lock in this repo and every dependency
#    pin in pyproject.toml is a floor (`>=`), so a re-resolve can install different versions than
#    the primary is tested against.
#
# Usage:  tools/worktree/new-worktree.sh <task-id> [base-ref]
# Env:    HARNESS_WORKTREE_HOME  override the parent directory for worktrees
#
# NB: this script deliberately exports no FLEET_* variable. settings.py pairs
# env_prefix="FLEET_" with extra="forbid", so one stray FLEET_* var makes every settings load
# exit 2.
set -euo pipefail

die() { printf 'new-worktree: %s\n' "$*" >&2; exit 1; }
say() { printf '  %s\n' "$*"; }

[ $# -ge 1 ] || die "usage: $0 <task-id> [base-ref]"
task=$1
base=${2:-HEAD}

case "$task" in
  *[!A-Za-z0-9._-]*|"") die "task id must match [A-Za-z0-9._-]+ (got '$task')" ;;
esac

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
primary=$(CDPATH= cd -- "$script_dir/../.." && pwd -P)

# Must be run from the PRIMARY checkout: the provisioning below copies from it by path.
git_dir=$(git -C "$primary" rev-parse --absolute-git-dir)
common_dir=$(CDPATH= cd -- "$(git -C "$primary" rev-parse --git-common-dir)" && pwd -P)
[ "$git_dir" = "$common_dir" ] || die "run this from the primary checkout, not a linked worktree"

wt_home=${HARNESS_WORKTREE_HOME:-"$(dirname -- "$primary")/$(basename -- "$primary") worktrees"}
wt="$wt_home/wt-$task"
branch="agent/$task"

# ---------------------------------------------------------------------------------------
# The space guard. tests/conftest.py:_bazel_root() picks BAZEL_ROOT like this:
#
#     inside = REPO_ROOT / "tools" / "bazel-test-root"
#     if " " not in str(inside): return inside
#     return Path(tempfile.gettempdir()) / f"fleet-bazel-{sha256(REPO_ROOT)[:12]}"
#
# REPO_ROOT is Path(__file__).resolve().parents[1] — the WORKTREE, so BAZEL_ROOT is per-checkout
# either way. But the two branches land in very different places:
#
#   path has a space (the primary's does) -> /tmp/fleet-bazel-<digest>, outside the tree.
#   path has no space                     -> <worktree>/tools/bazel-test-root, INSIDE the tree
#                                            and matched by NO .gitignore rule — multiple GiB of
#                                            untracked Bazel state showing up in `git status`,
#                                            one `git add -A` away from being committed.
#
# Cross-lane commit accidents are the entire reason this infrastructure exists, so a space-free
# worktree path is refused rather than papered over. (The alternative fix is a
# `tools/bazel-test-root/` line in .gitignore; that is a change to a tracked file and is left as
# a recommendation, not made here.)
# ---------------------------------------------------------------------------------------
case "$wt" in
  *" "*) : ;;
  *) die "worktree path '$wt' contains no space; BAZEL_ROOT would land at
    $wt/tools/bazel-test-root
  which is inside the worktree and ignored by nothing. Choose a HARNESS_WORKTREE_HOME whose
  path contains a space (the primary checkout's does), or add tools/bazel-test-root/ to
  .gitignore first." ;;
esac

[ -e "$wt" ] && die "$wt already exists"
git -C "$primary" show-ref --verify --quiet "refs/heads/$branch" \
  && die "branch $branch already exists (tear the old worktree down first)"

mkdir -p "$wt_home"

printf 'creating worktree %s on branch %s from %s\n' "$wt" "$branch" "$base"
git -C "$primary" worktree add "$wt" -b "$branch" "$base"

# ---------------------------------------------------------------------------------------
# Provision what git-ignored state `git worktree add` cannot carry. Shared with
# provision-existing.sh (the entry point for a worktree created some other way, e.g. by an
# orchestrator's own dispatch tooling at a space-free scratch path) — see lib-provision.sh.
# ---------------------------------------------------------------------------------------
printf 'provisioning ignored assets\n'
# shellcheck source=./lib-provision.sh
. "$script_dir/lib-provision.sh"
provision_worktree "$primary" "$wt"

# ---------------------------------------------------------------------------------------
# Verify git status is clean. provision_worktree already checks `import fleet` resolves inside
# the worktree and `fleet --help` runs; this check is specific to a FRESHLY created worktree
# (an already-existing one provisioned via provision-existing.sh may legitimately be mid-work
# and dirty, so that script does not run this check).
# ---------------------------------------------------------------------------------------
status=$(git -C "$wt" status --porcelain)
[ -z "$status" ] || die "worktree is not clean after provisioning:
$status"
say "git status clean"

digest=$(printf '%s' "$wt" | sha256sum | cut -c1-12)
cat <<EOF

worktree ready
  path       $wt
  branch     $branch (from $base)
  BAZEL_ROOT ${TMPDIR:-/tmp}/fleet-bazel-$digest   (per-checkout; NOT shared with the primary)
  python     $wt/.venv/bin/python
  teardown   "$primary/tools/worktree/rm-worktree.sh" $task

Before running the suite here, read tools/worktree/README.md — BAZEL_ROOT is isolated but disk
is not, and this host has limited free space.
EOF
