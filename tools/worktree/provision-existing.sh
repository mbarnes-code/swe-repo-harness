#!/usr/bin/env bash
# Provision an ALREADY-EXISTING git worktree of this repo with everything `git worktree add`
# cannot carry (it's all git-ignored): the tools/bin/{bazel,ast-grep,gh} binaries, the vendored
# tools/go and tools/rust toolchains, tools/bazelisk's download cache, and a working .venv.
#
# Use this -- not new-worktree.sh -- when the worktree was created some other way (an
# orchestrator's own worktree-dispatch tooling, a manual `git worktree add`, etc.) and just needs
# provisioning in place. new-worktree.sh both CREATES a worktree (at its own sibling-directory,
# space-in-path convention -- see README.md §3 "the space-free trap") AND provisions it; it
# refuses to run against a space-free path because BAZEL_ROOT would otherwise land inside the
# worktree. This script only provisions, so it has no opinion on where the worktree lives or how
# it got there, and works fine against a space-free path -- see the BAZEL_ROOT note below for
# what that costs you.
#
# Round VI tasks 90, 94 and 98 each independently hit this: dispatched to a worktree that was
# NOT created via new-worktree.sh (task 98's own was a scratchpad path with no space, e.g.
# `/tmp/.../scratchpad/roundVI-task98/wt`), so real-Bazel/real-git-filter-repo fixtures failed
# with `git-filter-repo is not on PATH` / `'uv' is not installed`, fixed by hand each time. This
# script is that fix, made reusable and idempotent.
#
# Usage:  tools/worktree/provision-existing.sh <worktree-path> [primary-checkout-path]
#         (run from anywhere; <worktree-path> must already exist and be a linked worktree
#         sharing this repo's git-dir. primary-checkout-path defaults to the worktree that
#         holds the actual .git directory, auto-detected via `git rev-parse --git-common-dir`.)
#
# NB: exports no FLEET_* variable, same reason as new-worktree.sh -- settings.py pairs
# env_prefix="FLEET_" with extra="forbid", so one stray FLEET_* var makes every settings load
# exit 2.
set -euo pipefail

die() { printf 'provision-existing: %s\n' "$*" >&2; exit 1; }
say() { printf '  %s\n' "$*"; }

[ $# -ge 1 ] || die "usage: $0 <worktree-path> [primary-checkout-path]"
wt=$(CDPATH= cd -- "$1" && pwd -P) || die "'$1' is not a directory"

[ -e "$wt/.git" ] || die "'$wt' has no .git -- not a git checkout at all"

common_dir=$(git -C "$wt" rev-parse --git-common-dir 2>/dev/null) || die "'$wt' is not inside a git repository"
common_dir=$(CDPATH= cd -- "$common_dir" && pwd -P)

if [ $# -ge 2 ]; then
  primary=$(CDPATH= cd -- "$2" && pwd -P) || die "'$2' is not a directory"
else
  # The primary checkout is the one whose OWN git-dir equals the common-dir every linked
  # worktree shares. For a linked worktree, `git rev-parse --git-common-dir` already points at
  # the primary's .git, so its parent is the primary checkout -- unless $wt IS the primary
  # (git-dir == common-dir there too), in which case there is nothing to provision from.
  primary=$(dirname -- "$common_dir")
fi

primary_git_dir=$(git -C "$primary" rev-parse --absolute-git-dir) || die "'$primary' is not a git checkout"
[ "$primary_git_dir" = "$common_dir" ] || die \
  "resolved primary '$primary' does not own the shared git-dir ($common_dir) --
  pass the primary checkout explicitly as the 2nd argument"
[ "$primary" != "$wt" ] || die "'$wt' IS the primary checkout (git-dir == common-dir) -- nothing to provision from"

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
# shellcheck source=./lib-provision.sh
. "$script_dir/lib-provision.sh"

case "$wt" in
  *" "*) : ;;
  *)
    cat >&2 <<EOF
provision-existing: NOTE -- '$wt' contains no space in its path.
  tests/conftest.py:_bazel_root() falls back to \$wt/tools/bazel-test-root for BAZEL_ROOT
  whenever the checkout path has no space (it uses /tmp/fleet-bazel-<digest> only when there
  IS one). That directory is matched by no .gitignore rule, so a real-Bazel test run here will
  leave multi-GiB of untracked state inside the worktree, visible to \`git status\` and one
  \`git add -A\` from being committed. See tools/worktree/README.md §3.
  Not fatal -- continuing to provision -- but keep it out of any git add in this worktree, or
  add 'tools/bazel-test-root/' to .gitignore (a separate, deliberate decision this script does
  not make for you -- see D-finding in task-98-report.md).
EOF
    ;;
esac

printf 'provisioning existing worktree %s from %s\n' "$wt" "$primary"
provision_worktree "$primary" "$wt"

cat <<EOF

worktree provisioned
  path       $wt
  primary    $primary
  python     $wt/.venv/bin/python
EOF
