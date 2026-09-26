# shellcheck shell=bash
# Shared provisioning logic for a git worktree of this repo: everything a fresh
# `git worktree add` cannot carry because it is git-ignored (tools/bin binaries, the vendored
# toolchains, .venv). Sourced by both:
#
#   new-worktree.sh        -- creates a NEW worktree at this project's own sibling-with-a-space
#                              convention, then provisions it.
#   provision-existing.sh  -- provisions a worktree that ALREADY EXISTS at an arbitrary path
#                              (e.g. one created by an orchestrator/dispatch tool other than
#                              new-worktree.sh, such as a scratchpad worktree with no space in
#                              its path). See README.md "Provisioning a worktree you did not
#                              create with new-worktree.sh" for why this second entry point
#                              exists: round VI tasks 90, 94 and 98 each hit the missing-tools/
#                              missing-.venv problem by hand in worktrees new-worktree.sh's own
#                              space-guard would have refused to create.
#
# Both entry points call provision_worktree "$primary" "$wt". Idempotent: safe to re-run on a
# worktree that already has some or all of this (symlinks use -f; the .venv clone is skipped,
# not re-copied, if $wt/.venv already exists).

provision_worktree() {
  primary=$1
  wt=$2

  # tools/bin: the four wrapper SCRIPTS (cargo, gazelle, go, rustc) are tracked and arrive with
  # the checkout already. The real binaries are ignored by `tools/bin/*` in .gitignore and
  # symlinked here -- they are large (ast-grep 53MB, gh 41MB, bazel 7MB, gitleaks 22MB),
  # read-only, and safe to share read-only across every worktree. `tools/bin/*` (no trailing
  # slash) ignores the symlinks too, so `git status` stays clean. gitleaks (ADR-0141) is built
  # once into the primary via `tools/bin/go install github.com/zricethezav/gitleaks/v8@v8.30.1`
  # (pinned, not @latest) and shared read-only from here like the other three.
  mkdir -p "$wt/tools/bin"
  for b in bazel ast-grep gh gitleaks; do
    if [ -e "$primary/tools/bin/$b" ]; then
      ln -sf "$primary/tools/bin/$b" "$wt/tools/bin/$b"
      say "link tools/bin/$b"
    fi
  done

  # The vendored toolchains (~2.2 GiB total) are shared, NOT copied. The wrapper scripts derive
  # GOROOT/GOPATH/GOCACHE/RUSTUP_HOME/CARGO_HOME from their own location, so symlinking just the
  # children is enough -- and it must be just the children: .gitignore says `tools/go/` and
  # `tools/rust/` (trailing slash, matches only real directories), so `ln -s .../tools/go` itself
  # would appear as an untracked file. The PARENT stays a real directory.
  _link_children() {
    src=$1; dst=$2; shift 2
    [ -d "$src" ] || return 0
    mkdir -p "$dst"
    for child in "$@"; do
      [ -e "$src/$child" ] && ln -sf "$src/$child" "$dst/$child"
    done
  }
  _link_children "$primary/tools/go"   "$wt/tools/go"   cache config env gopath sdk
  _link_children "$primary/tools/rust" "$wt/tools/rust" cargo rustup
  say "link tools/go, tools/rust (shared GOCACHE/GOMODCACHE/CARGO_HOME -- concurrency-safe)"

  # bazelisk: downloads/ is a pure release-archive cache, safe to share read-only. output/ is a
  # LIVE Bazel --output_user_root (install base, MD5-keyed output bases, lock files) -- sharing
  # that across concurrent worktrees is exactly the collision this whole setup exists to avoid,
  # so each worktree gets its own empty one, created if not already present.
  _link_children "$primary/tools/bazelisk" "$wt/tools/bazelisk" downloads
  mkdir -p "$wt/tools/bazelisk/output"
  say "link tools/bazelisk/downloads (own tools/bazelisk/output)"

  # references/*/ (third-party citation corpora) are deliberately NOT linked here: read-only
  # material, cited by absolute path into the primary checkout when needed.

  # tools/harmony-vocab: `openai-harmony`'s o200k_base.tiktoken, which `tests/conftest.py` points
  # TIKTOKEN_ENCODINGS_BASE at. Git-ignored, so `git worktree add` never carries it, and without it
  # 29 of tests/test_llm_backend_harmony_gpt_oss.py's 48 items skip (ADR-0150, D147). Hardlinked
  # from the primary rather than re-fetched: a linked worktree shares a filesystem with the primary
  # by construction, and the primary's copy is already sha256-verified. If the primary has none,
  # this is not an error -- run tools/bin/fetch-harmony-vocab (which needs network) in either tree.
  if [ -f "$primary/tools/harmony-vocab/o200k_base.tiktoken" ] &&
     [ ! -f "$wt/tools/harmony-vocab/o200k_base.tiktoken" ]; then
    mkdir -p "$wt/tools/harmony-vocab"
    cp -l "$primary/tools/harmony-vocab/o200k_base.tiktoken" \
          "$wt/tools/harmony-vocab/o200k_base.tiktoken"
    say "link tools/harmony-vocab/o200k_base.tiktoken"
  elif [ ! -f "$primary/tools/harmony-vocab/o200k_base.tiktoken" ]; then
    say "no tools/harmony-vocab in primary -- Harmony vocab tests will SKIP (tools/bin/fetch-harmony-vocab)"
  fi

  # .venv: hardlink copy of the primary's, then rewrite the absolute paths it bakes in.
  # NOT symlinked -- .venv/lib/python3.12/site-packages/_editable_impl_fleet.pth is a bare
  # absolute path to the PRIMARY's src/. A symlinked venv would still pass pytest (pyproject.toml
  # sets pythonpath=["src"] and tests/conftest.py inserts REPO_ROOT/src, both worktree-relative)
  # but `.venv/bin/fleet` and any plain `python -c "import fleet"` would silently import the
  # primary's source, not this worktree's.
  # NOT `uv sync` -- `uv` is not on PATH outside `.venv/bin/uv` on this host, and a fresh resolve
  # is a network operation that can drift from what the primary is tested against. (Corrected
  # 2026-09-25: the original reason given here opened with "there is no uv.lock in this repo",
  # which `uv.lock` being tracked at HEAD has falsified. The decision is unchanged -- a hardlink
  # copy of the primary's `.venv` is the only offline path that guarantees byte-identical
  # versions -- and the falsified clause is removed rather than the whole reason rewritten.)
  if [ -d "$wt/.venv" ]; then
    say ".venv already present, not re-cloned (rm -rf \"$wt/.venv\" first to force a re-clone)"
  else
    [ -d "$primary/.venv" ] || { echo "provision_worktree: no $primary/.venv to clone" >&2; return 1; }
    say "cloning .venv (hardlink copy, then absolute-path rewrite)"
    cp -al "$primary/.venv" "$wt/.venv"
    rewritten=0
    while IFS= read -r f; do
      sed -i "s|$primary|$wt|g" -- "$f"
      rewritten=$((rewritten + 1))
    done < <(grep -rlIF -- "$primary" "$wt/.venv" 2>/dev/null || true)
    say "rewrote $rewritten files"
  fi

  # Verify: the one check that matters is that `import fleet` resolves to THIS worktree, not the
  # primary. Deliberately not a full pytest run (~9 min, and two concurrent pytest sessions reap
  # each other's BAZEL_ROOT children per CLAUDE.md).
  imported=$(cd "$wt" && ./.venv/bin/python -c 'import fleet, sys; sys.stdout.write(fleet.__file__)')
  case "$imported" in
    "$wt"/*) say "import fleet -> $imported" ;;
    *)
      echo "provision_worktree: import fleet resolved to '$imported', OUTSIDE $wt -- the" >&2
      echo "editable-install path rewrite failed; this worktree would test the wrong source tree" >&2
      return 1
      ;;
  esac
  (cd "$wt" && ./.venv/bin/fleet --help >/dev/null) || { echo "provision_worktree: fleet --help failed in $wt" >&2; return 1; }
  say "fleet --help ok"
}
