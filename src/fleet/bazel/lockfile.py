"""`MODULE.bazel.lock` — the resolution record without which no offline build starts (§3.3).

Bazel writes this file itself, beside `MODULE.bazel`, and it is the only place the *registry
metadata* of the module graph is recorded: `registryFileHashes` maps every registry URL the
resolution read to the hash of the bytes found there. Without it Bazel has no such map and must
re-derive the graph by fetching those URLs — an access that happens **before analysis**, while
computing the main repository mapping. That is why a `--network=none` container with a fully
warmed repository cache and no lockfile still dies with

    ERROR: Error computing the main repository mapping: Error accessing registry
    https://bcr.bazel.build/: Failed to fetch registry file … Unknown host: bcr.bazel.build

and exit 32, and why the same container with a matching lockfile exits 0. Neither artifact is
sufficient alone: an empty cache with a matching lock is exit 32 as well.

**Why the CACHE is registry-agnostic and the LOCK is not.** The repository cache is keyed by the
SHA-256 of the bytes it holds, so a cache warmed through the `raw.githubusercontent.com` mirror
answers a lookup made through `bcr.bazel.build` — both addresses serve byte-identical files.
`registryFileHashes` is keyed by the **full URL**, so a lock written against the mirror is a lock
whose keys a Bazel contacting `bcr.bazel.build` never looks up: it re-fetches, offline, and fails
exactly as if there were no lock at all — while the tree LOOKS offline-ready, which is the worse
half of the defect. `check_lock_registry` is that comparison, made as a string check over the
published bytes, which is the only form of it that does not need a container and a warm cache to
run.

The registry to check against is a **parameter and never a default here**: this module is part of
`bazel/`, a pure driver, and which registry a given invocation will contact is a fact about the
argv that invocation was built with (`workers.buildverify.BuildverifyWorker._bazel_argv` emits no
`--registry` at all, so its answer is Bazel's built-in `settings.BCR_DEFAULT_REGISTRY`). A caller
that has to name it is a caller whose assertion says which registry it was made against.
"""

from __future__ import annotations

import json
from typing import Any, Final
from urllib.parse import urlsplit

__all__ = [
    "MODULE_LOCK_PATH",
    "LockfileRegistryMismatchError",
    "check_lock_registry",
    "lock_registry_urls",
]


MODULE_LOCK_PATH: Final = "MODULE.bazel.lock"
"""Bazel's own name for the file, at the monorepo root beside `MODULE.bazel`. Not configurable:
Bazel writes and reads it at this path and nowhere else."""


class LockfileRegistryMismatchError(ValueError):
    """A lockfile is keyed by a registry the build that will read it does not contact."""


def lock_registry_urls(text: str) -> list[str]:
    """Every registry URL a lockfile is **keyed by**, sorted and de-duplicated.

    Keys, not values, and that distinction is the whole rule. A lockfile's `http(s)://` strings
    come from two unrelated places:

    * the **keys** of `registryFileHashes` are registry metadata addresses — `…/modules/rules_go/
      0.61.1/MODULE.bazel`, `…/source.json` — which Bazel looks up *by URL* when it re-checks the
      module graph, so a key it cannot match is a fetch it must perform;
    * the **values** buried in `moduleExtensions` are download URLs for archives (github.com,
      registry.npmjs.org, …) which the repository cache serves *by SHA-256*, so their host is
      irrelevant to whether the build can run offline.

    Only the first kind can make an offline build fail, so only the first kind is the subject.

    Mapping keys are collected by walking the parsed JSON rather than by naming
    `registryFileHashes`, because Bazel has already renumbered `lockFileVersion` several times and
    moved fields between versions; "a map keyed by a URL" has survived every one of those changes,
    and no other field of the format is one.

    An unreadable lockfile raises `json.JSONDecodeError` and is deliberately not tolerated: a lock
    nobody can parse is a lock nobody can say is keyed correctly.
    """
    urls: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(key, str) and key.startswith(("https://", "http://")):
                    urls.add(key)
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(json.loads(text))
    return sorted(urls)


def check_lock_registry(text: str, *, registry: str) -> None:
    """Refuse a lockfile whose registry keys name a host `registry` does not.

    `registry` is the address the Bazel that will READ this lock contacts — for the container
    build that is Bazel's built-in default, because `_bazel_argv` emits no `--registry`.

    Hosts are compared rather than whole prefixes because a host is what fails: the mirror lives
    on `raw.githubusercontent.com` under a path, and it is the DNS name, not the path, that the
    container has no route to.
    """
    expected = urlsplit(registry).netloc
    foreign = sorted({url for url in lock_registry_urls(text) if urlsplit(url).netloc != expected})
    if not foreign:
        return
    hosts = sorted({urlsplit(url).netloc for url in foreign})
    raise LockfileRegistryMismatchError(
        f"{MODULE_LOCK_PATH} is keyed by registry host(s) {hosts}, but the Bazel that reads it "
        f"contacts {registry!r} — `buildverify._bazel_argv` emits no `--registry`, so the "
        f"built-in default is the only address it will ever look a module up at. Every "
        f"registry URL it cannot match is a registry file it must RE-FETCH before analysis "
        f"begins, and under `--network=none` that fetch fails: the build dies at `Error "
        f"computing the main repository mapping: Error accessing registry {registry}/` with "
        f"exit 32 — the identical failure to shipping no lockfile at all, except that the tree "
        f"LOOKS offline-ready. Re-resolve the lock against {registry}. First offending key: "
        f"{foreign[0]}"
    )
