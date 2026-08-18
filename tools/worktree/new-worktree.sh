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
# Provision what git-ignored state `git worktree add` cannot carry.
# ---------------------------------------------------------------------------------------
printf 'provisioning ignored assets\n'

# tools/bin: the four wrapper SCRIPTS are tracked and arrive with the checkout; the three real
# binaries are ignored by `tools/bin/*`. Symlinked — they are read-only executables.
# `tools/bin/*` (no trailing slash) ignores the symlinks, so `git status` stays clean.
for b in bazel ast-grep gh; do
  if [ -e "$primary/tools/bin/$b" ]; then
    ln -s "$primary/tools/bin/$b" "$wt/tools/bin/$b"
    say "link tools/bin/$b"
  fi
done

# The vendored toolchains (~2.2 GiB) are shared, NOT copied. The wrappers derive GOROOT/GOPATH/
# GOCACHE/RUSTUP_HOME/CARGO_HOME from their own location, so the symlinks are all that is needed.
#
# The PARENT is a real directory and only the children are symlinks. This matters: .gitignore
# says `tools/go/`, and a trailing-slash pattern matches only real directories — git treats a
# symlink as a file, so `ln -s .../tools/go` would show up as an untracked entry.
link_children() {
  src=$1; dst=$2; shift 2
  [ -d "$src" ] || return 0
  mkdir -p "$dst"
  for child in "$@"; do
    [ -e "$src/$child" ] && ln -s "$src/$child" "$dst/$child"
  done
}
link_children "$primary/tools/go"   "$wt/tools/go"   cache config env gopath sdk
link_children "$primary/tools/rust" "$wt/tools/rust" cargo rustup
say "link tools/go, tools/rust (shared GOCACHE/GOMODCACHE/CARGO_HOME — concurrency-safe)"

# bazelisk: `downloads/` is a pure release-archive cache and is safe to share. `output/` is a
# live Bazel --output_user_root (install base + MD5-keyed output bases + lock files); sharing one
# across concurrent worktrees is exactly the reaping/locking collision this is meant to avoid, so
# each worktree gets its own empty one.
link_children "$primary/tools/bazelisk" "$wt/tools/bazelisk" downloads
mkdir -p "$wt/tools/bazelisk/output"
say "link tools/bazelisk/downloads (own tools/bazelisk/output)"

# references/*/ (the third-party corpora) are NOT linked: `references/*/` is a trailing-slash
# pattern for the same reason as above, and they are read-only citation material. Read the
# primary's copy by absolute path if you need it.

# ---------------------------------------------------------------------------------------
# The venv.
# ---------------------------------------------------------------------------------------
printf 'cloning .venv (hardlink copy, then absolute-path rewrite)\n'
[ -d "$primary/.venv" ] || die "no $primary/.venv to clone"
cp -al "$primary/.venv" "$wt/.venv"

# Every file that names the primary by absolute path: console-script shebangs, the activate
# scripts, pyvenv.cfg, and — the important one — _editable_impl_fleet.pth, which is a bare
# absolute path to the primary's src/. `sed -i` writes a temp and renames, so each rewritten file
# gets a fresh inode and the hardlink to the primary's copy is broken rather than followed.
rewritten=0
while IFS= read -r f; do
  sed -i "s|$primary|$wt|g" -- "$f"
  rewritten=$((rewritten + 1))
done < <(grep -rlIF -- "$primary" "$wt/.venv" 2>/dev/null || true)
say "rewrote $rewritten files"

# ---------------------------------------------------------------------------------------
# Verify. Deliberately NOT by running pytest: a full suite is ~9 minutes, and two concurrent
# pytest sessions are unsafe on this host for disk reasons (see README.md).
# ---------------------------------------------------------------------------------------
printf 'verifying\n'

status=$(git -C "$wt" status --porcelain)
[ -z "$status" ] || die "worktree is not clean after provisioning:
$status"
say "git status clean"

imported=$(cd "$wt" && ./.venv/bin/python -c 'import fleet, sys; sys.stdout.write(fleet.__file__)')
case "$imported" in
  "$wt"/*) say "import fleet -> $imported" ;;
  *) die "import fleet resolved to '$imported', OUTSIDE the worktree — the editable-install
  rewrite failed and this worktree would test the wrong source tree" ;;
esac

(cd "$wt" && ./.venv/bin/fleet --help >/dev/null) || die "fleet --help failed in $wt"
say "fleet --help ok"

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
