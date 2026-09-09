# The native-baseline sandbox image `settings.preflight.baseline_build.container_image` names —
# the image every `native_baseline()` (`EcosystemAdapter.native_baseline`, ADR-0135 Leg A,
# `src/fleet/models/build.py::NativeBaseline`) build_argv/test_argv pair runs inside, for the
# PRE-migration, native (non-Bazel) probe SPEC §12.11/D116 needs and no adapter has run before.
#
#   docker build -f docker/fleet-baseline.Dockerfile -t fleet-baseline:py3.11-node18 docker/
#
# **This is a SEPARATE image from `docker/fleet-build.Dockerfile`, on purpose (ADR-0135 ruling
# 3).** That image is Bazel's own hermetic `--network=none` sandbox; this one runs WITH network
# access, because resolving an arbitrary third-party repo's OWN npm/PyPI dependencies in their
# original, pre-migration form is not something this project vendors or can make offline (the
# same ruling's own words). Never add this image's toolchains to `fleet-build.Dockerfile`, and
# never strip this image's network access to match it — the two sandboxes answer different
# questions (does OUR migrated build reproduce hermetically? vs. did THEIR original build/test
# ever pass?) and conflating them would either leak network into Bazel's hermetic guarantee or
# make a baseline probe fail on every real repo's own dependency resolution.
#
# Scope: the fixture fleet this criterion's own assertion is bounded to (ADR-0135 ruling 3,
# re-confirmed directly against `tests/test_scan_e2e.py::FIXTURE_REPOS` at task-108 dispatch:
# {acme-lib-ts, acme-app-ts} = npm, {acme-lib-py, acme-app-py} = PyPI, acme-empty = no toolchain
# needed at all). Python + Node/npm ONLY — not Go/JVM/Rust/Ruby, which nothing in the shipped
# fixture fleet exercises. Add a toolchain the day the fixture fleet actually grows one, not
# before (Rule 2: build only what the current fixture fleet needs).
FROM debian:bookworm-slim

# `python3`/`python3-pip`: the interpreter `PyAdapter.native_baseline()` names literally
# (`["python3", "-m", "pip", "install", "-e", "."]` / `["python3", "-m", "pytest"]`). Debian
# bookworm's apt python3 is 3.11, not the 3.12 `ecosystems/py.py::_PYTHON_VERSION` pins for the
# MIGRATED side's hermetic `rules_python` toolchain -- deliberately not matched here: a native
# baseline probes the repo's OWN pre-migration environment (ADR-0135 ruling 3's "in their
# original ... form"), not the harness's post-migration pin, and neither fixture Python repo
# declares a `requires-python` floor that 3.11 would violate.
#
# `nodejs`/`npm`: what `JsAdapter.native_baseline()` names literally (`["npm", "install"]` /
# `["npm", "test"]`). Debian bookworm's apt nodejs is 18.20 with npm 9.2 bundled as a separate
# package -- plain `apt` on purpose, matching `fleet-build.Dockerfile`'s own preference for pinned
# distro packages over an extra upstream repo, and nothing in the fixture fleet's package.json
# (no `engines` field) requires newer.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates \
      python3 \
      python3-pip \
      nodejs \
      npm \
 && rm -rf /var/lib/apt/lists/*

# `pytest`: the brief's own scope item 1 ("Has a real Python interpreter + pytest") -- baked in
# at BUILD time rather than left to `native_baseline()`'s `build_argv`, because neither fixture
# Python repo's `pyproject.toml` declares pytest as a dependency (there is nothing native
# `pip install -e .` would ever pull it in from), and it is the harness's own probe tool, not the
# repo's. `PIP_BREAK_SYSTEM_PACKAGES=1` is what lets this `pip install` (running as root, at
# build time) write into Debian's PEP-668 "externally managed" system site-packages at all --
# without it, `pip install` refuses outright (measured: `error: externally-managed-environment`).
ENV PIP_BREAK_SYSTEM_PACKAGES=1
RUN python3 -m pip install --no-cache-dir pytest pytest-asyncio

# The SAME `ENV PIP_BREAK_SYSTEM_PACKAGES=1` also has to be visible to the CONTAINER's own
# `pip install -e .` -- `native_baseline()`'s own `build_argv`, run at CONTAINER-run time as the
# arbitrary HOST uid `spec_for_attempt`'s `current_user_spec()` passes via `--user`, not as root
# -- and image `ENV` is what a `docker run` with no `--env` at all still sees (the same fact
# `fleet-build.Dockerfile`'s HOME/USER comment relies on; `native_baseline()`'s exact argv has no
# `--break-system-packages` flag to add one at call time). Left set for that reason: this is not
# only a build-time convenience, it is load-bearing at run time too.
#
# `PIP_USER=1` is the second half of the SAME arbitrary-uid problem, one layer down: even with
# the PEP-668 gate lifted, an arbitrary host uid has no write access to root-owned system
# site-packages (`/usr/lib/python3.11/dist-packages`) -- exactly the `--user`/system split
# `current_user_spec()`'s own docstring already names for the bind-mounted worktree, applied here
# to Python's own install target. `PIP_USER=1` makes an unmodified `pip install -e .` land in
# `$HOME/.local/lib/python3.11/site-packages` instead, which the world-writable `$HOME` below
# makes writable by any uid. Both the repo's own package (installed at container-run time, user
# site) and `pytest` (installed at image-build time, system site, as root) end up on the SAME
# interpreter's `sys.path` -- Python's `site` module includes both by default -- so
# `python3 -m pytest` sees both without any `PYTHONPATH` plumbing.
ENV PIP_USER=1

# The SAME arbitrary-uid problem `fleet-build.Dockerfile` already solved for Bazel
# (`GetUserName()`/exit 36), restated here for pip/npm rather than borrowed by reference: a
# passwd-less uid gets `HOME=/` from Docker, which is unwritable, and both `pip install --user`
# and `npm install`'s local caches need SOMEWHERE to write. World-writable because the uid is not
# known at build time -- the fleet passes the HOST user's uid/gid, which differs per machine, so
# no `chown` here can be correct, exactly as the fetch-side comment there explains for its own
# `/home/fleet`.
ENV HOME=/home/fleet
RUN mkdir -p /home/fleet \
 && chmod -R 0777 /home/fleet

# `spec_for_attempt`'s `container_workdir` default (`/work`), so a native baseline container
# bind-mounting the repo's own PRE-migration root over this directory (per `NativeBaseline`'s own
# docstring: build_argv/test_argv run "with the unit's own PRE-migration repo root as cwd") lands
# there without any extra `--workdir` plumbing on Leg B's part.
WORKDIR /work
