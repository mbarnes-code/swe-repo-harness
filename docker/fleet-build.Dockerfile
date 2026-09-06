# The sandbox image `settings.verify.container_image` names — the image every containerised
# `bazel build`/`bazel test` in PASS 5 runs inside.
#
#   docker build -f docker/fleet-build.Dockerfile -t fleet-build:9.2.0-bookworm docker/
#
# It is built with a network and then used WITHOUT one: `settings.verify.network` is `none`, and
# `buildverify` emits `docker run` with no `--env` at all, so anything a build needs must either
# be baked in here or arrive through a bind-mounted cache. Every line below is load-bearing;
# the comments say why, because several of them look removable and are not.
#
# What this image deliberately does NOT contain: `git`, `patch`, `unzip`/`xz-utils`, and any
# Go/Node/Rust toolchain. Those are supposed to arrive through the mounted Bazel repository
# cache, and baking them in would mask an empty cache instead of surfacing it.
# Named triggers to revisit: a build whose stderr names the missing binary (e.g. `git` for a
# `git_repository`, `patch` for a `bazel_dep` with `patches = [...]`), or a cgo repo whose
# linker error names a system library (`-lsqlite3` ⇒ add `libsqlite3-dev`).
#
# `python3` is the ONE exception to the paragraph above, added by round VI task 57 (§12.11 Task
# B, the first time this codebase ever ran a real, non-vacuous `py_test` target inside this
# sandbox). `rules_python`'s hermetic interpreter is real and IS what actually runs the test
# body, but its `py_test`/`py_binary` bootstrap STUB SCRIPT is itself a `.py` file with a
# `#!/usr/bin/env python3` shebang — a `PATH` lookup is needed just to START that stub, which
# then locates and re-execs the real hermetic interpreter. With no `python3` anywhere on `PATH`,
# `/usr/bin/env` fails before the hermetic toolchain is ever consulted: `bazel test` exits 0 at
# the BUILD step (nothing about resolving the hermetic interpreter needs a fetch) and every real
# Python test target fails at the TEST step with `Exit 127` / `/usr/bin/env: 'python3': No such
# file or directory` — measured directly against this exact image, `docker run --network=none`,
# before this line existed. This is the identical shape as the `gcc`/`libc6-dev` layer above:
# a binary Bazel needs to BOOTSTRAP a toolchain, not the toolchain a build actually links
# against, so baking it in does not mask an empty repository cache the way a full non-hermetic
# `git`/`patch`/language-toolchain substitute would.
#
# **`python3-minimal` was tried first and measured insufficient**, not assumed so: the bootstrap
# stub itself (not the test body) does `import uuid`, which `python3-minimal`'s pared-down
# stdlib does not ship — `ModuleNotFoundError: No module named 'uuid'` at the stub's own line 23,
# measured against this exact image before this comment existed. The full `python3` metapackage
# is what the bootstrap stub's own imports need, so it is what is installed below.

# ======================================================================================
# Stage 1 — fetch. Isolated so that `curl` and its CA bundle never reach the final image:
# a sandbox that runs with `--network=none` has no use for an HTTP client.
# ======================================================================================
FROM debian:bookworm-slim AS fetch

# Pinned to the version this project pins everywhere else (`MONOREPO_BAZEL_VERSION`, the
# `.bazelversion` the generated monorepo carries, the verified exit-code table). Bumping this
# ARG alone is not enough — those pins move together or the image builds a different Bazel than
# the harness claims to have verified.
ARG BAZEL_VERSION=9.2.0
# SHA256 of `bazel-9.2.0-linux-x86_64`, identical at releases.bazel.build, the GitHub release
# asset, and this repository's own bazelisk download cache
# (`tools/bazelisk/downloads/sha256/<this hash>/bin/bazel`, where bazelisk names the directory
# after the digest it verified). Three independent sources, one digest.
ARG BAZEL_SHA256=7668a95db1250f12c40407251e4e203b4ec8bf39bc495d2f485b2d8c99048694

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

# The OFFICIAL release binary, not `apt` (Debian ships no Bazel) and not bazelisk (bazelisk
# resolves `.bazelversion` by DOWNLOADING it, and `--network=none` cannot download anything —
# a bazelisk-based image would fail at the first build, not at build time where it is cheap to
# see). `sha256sum -c` is what makes this a pin rather than a fetch: a compromised or truncated
# download fails the image build instead of the fleet.
RUN curl --fail --silent --show-error --location \
      --output /bazel \
      "https://releases.bazel.build/${BAZEL_VERSION}/release/bazel-${BAZEL_VERSION}-linux-x86_64" \
 && printf '%s  /bazel\n' "${BAZEL_SHA256}" | sha256sum -c - \
 && chmod 0755 /bazel

# ======================================================================================
# Stage 2 — the image the fleet actually runs.
# ======================================================================================
# `debian:bookworm-slim`, NOT Alpine. The official Bazel release binary is glibc-dynamic
# (`ELF … dynamically linked, interpreter /lib64/ld-linux-x86-64.so.2`), as are the rules_go /
# rules_rust / rules_js prebuilt toolchains a migrated repo will pull through the repository
# cache. musl would need a `-installer.sh` build from source or a patched loader for each. (The
# C-compiler probe itself runs fine under busybox `ash`; the shell was never the problem.)
FROM debian:bookworm-slim

# The ONLY toolchain layer. `local_config_cc` — the repository rules_cc's
# `cc_configure_extension` generates before ANY target of ANY language analyses — resolves the
# literal name `gcc` on PATH (see `C_TOOLCHAIN_PROBE`, which mirrors that lookup line for line),
# and hard-fails with `Cannot find gcc or CC` when it does not resolve. `libc6-dev` supplies the
# headers and `crt*.o` that the very next step (`gcc -E -v -` to read the builtin include
# directories, then a link) needs. The rest of Bazel's tool sweep — `ar`, `ld`, `nm`, `objdump`,
# `strip` — comes in as `gcc`'s own dependencies via binutils, and the optional tools it cannot
# find degrade to `/bin/false` rather than `fail()`.
#
# NOT `build-essential` (56 packages vs 41) and NOT `g++`: nothing in the corpus's first
# migration wave compiles C++, and an unused compiler is an unaudited one. Add `g++` the first
# time a build's stderr names a C++ source or `cc_binary` failing to link.
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc libc6-dev \
 && rm -rf /var/lib/apt/lists/*

# `python3`: see the module-level comment block above ("`python3` is the ONE exception...").
# `rules_python` needs SOME `python3` resolvable via `/usr/bin/env` on `PATH` just to start its
# `py_test`/`py_binary` bootstrap stub — the stub then locates and re-execs the real, hermetic,
# `--repository_cache`-resolved interpreter itself. The FULL metapackage, not `-minimal`: the
# bootstrap stub's own code (not the test body it hands off to) does `import uuid`, which
# `python3-minimal` does not ship — measured directly against this image before this line was
# the full package.
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 \
 && rm -rf /var/lib/apt/lists/*

# No JDK layer: the Bazel release binary is a self-extracting archive that EMBEDS a JRE
# (`embedded_tools/jdk/bin/java` inside the install base). Installing `default-jre` would add
# ~180 MB that nothing executes.
COPY --from=fetch /bazel /usr/local/bin/bazel

# ---------------------------------------------------------------------------------------
# The `--user` fix. DO NOT DELETE THESE TWO `ENV`s — they are not cosmetic.
#
# Every sandboxed command runs `--user <uid>:<gid>` (`current_user_spec()`, so the bind-mounted
# worktree does not fill with root-owned artifacts). That uid has no `/etc/passwd` entry, and
# for a passwd-less uid Docker sets `HOME=/` and leaves `USER` unset. Bazel's CLIENT then calls
# `blaze::GetUserName()` while computing its default output user root, finds neither `$USER` nor
# a passwd entry, and dies with `LOCAL_ENVIRONMENTAL_ERROR` — exit 36. 36 is in the harness's
# `INFRA_EXIT_CODES`, so ADR-0014 spends NO attempt on it and the fleet re-queues the repo
# forever against an image defect that no retry can fix. `/` being unwritable is the second half
# of the same failure: even with a name, the default output base under `/` cannot be created.
#
# Image `ENV` survives `--user` and is what the process sees when `docker run` passes no `--env`
# at all — which is exactly what `buildverify` emits.
ENV HOME=/home/fleet
ENV USER=fleet

# World-writable because the uid is not known at build time: the fleet passes the HOST user's
# uid/gid, which differs per machine, so no `chown` here can be correct. Nothing sensitive lives
# under it — it holds only Bazel's own output base.
RUN mkdir -p /home/fleet/.cache/bazel \
 && chmod -R 0777 /home/fleet

# Belt and braces for the same failure, structurally rather than by environment. `/etc/bazel.bazelrc`
# is Bazel's SYSTEM rc file — a compile-time constant, confirmed present as a literal string in
# the shipped 9.2.0 binary — read before the workspace `.bazelrc` and before any command line.
# Setting `output_user_root` explicitly means the client never needs a user NAME to build a
# default path, so `GetUserName()` is never called and the exit-36 path is removed rather than
# merely worked around.
#
# This cannot be done from the harness side instead: `output_user_root` is a STARTUP option (it
# must precede the verb), and `_bazel_argv` appends every flag AFTER `build`/`test`, where Bazel
# rejects it as `COMMAND_LINE_ERROR` — exit 2.
RUN printf 'startup --output_user_root=/home/fleet/.cache/bazel/_bazel_fleet\n' \
      > /etc/bazel.bazelrc \
 && chmod 0644 /etc/bazel.bazelrc

# `spec_for_attempt`'s `container_workdir` default, so the directory exists even for a container
# started without the worktree mount (the C-toolchain probe is one). Real builds bind-mount the
# host worktree over it, and the mount's ownership — not this line's — is what they write as.
WORKDIR /work

# NOTE, deliberately: there is no `ENV CC`. A non-empty `CC` REPLACES the `gcc` default in
# rules_cc's lookup rather than supplementing it, so a wrong or stale `CC` is strictly worse
# than none, and `gcc` on PATH is already the answer that lookup wants.
