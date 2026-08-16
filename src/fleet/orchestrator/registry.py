"""Worker self-registration and discovery (SPEC §7.2).

One registry pattern, two users: this module for `BaseWorker`, `fleet.manifests.base` for
`ManifestAdapter`. Adding a worker is a file drop into `fleet/workers/` — never an
orchestrator edit. A duplicate `name` is a startup error, never a silent overwrite, because
two workers answering to one name means the run's provenance is a lie.
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import TYPE_CHECKING

from fleet.workers.base import BaseWorker

if TYPE_CHECKING:
    from types import ModuleType

_WORKERS: dict[str, type[BaseWorker]] = {}  # type: ignore[type-arg]
_DISCOVERED = False


def register_worker[W: type[BaseWorker]](cls: W) -> W:  # type: ignore[type-arg]
    """Self-registration decorator. Duplicate `name` is a startup error, not a silent overwrite."""
    name = getattr(cls, "name", None)
    if not name:
        raise RuntimeError(f"{cls.__qualname__} must declare a ClassVar `name` to register")
    existing = _WORKERS.get(name)
    if existing is not None and existing is not cls:
        raise RuntimeError(
            f"duplicate worker name: {name} "
            f"({existing.__module__}.{existing.__qualname__} vs "
            f"{cls.__module__}.{cls.__qualname__})"
        )
    _WORKERS[name] = cls
    return cls


def discover(*, force: bool = False) -> dict[str, type[BaseWorker]]:  # type: ignore[type-arg]
    """Import every module in `fleet.workers`, then return the registry.

    Import-time side effects are limited to registration. An import error is *not* swallowed
    (Rule 11): a worker module that cannot be imported is a broken run, not a missing feature.
    """
    global _DISCOVERED
    if _DISCOVERED and not force:
        return dict(_WORKERS)

    package: ModuleType = importlib.import_module("fleet.workers")
    for module in pkgutil.iter_modules(package.__path__, prefix=f"{package.__name__}."):
        if module.name.rsplit(".", 1)[-1].startswith("_"):
            continue
        importlib.import_module(module.name)
    _DISCOVERED = True
    return dict(_WORKERS)


def get_worker(name: str) -> type[BaseWorker]:  # type: ignore[type-arg]
    """Look up a registered worker, triggering discovery on first use."""
    workers = discover()
    try:
        return workers[name]
    except KeyError as exc:
        known = ", ".join(sorted(workers)) or "<none>"
        raise KeyError(f"unknown worker {name!r}; registered: {known}") from exc


def registry() -> dict[str, type[BaseWorker]]:  # type: ignore[type-arg]
    """The registry as it stands, without forcing discovery. For diagnostics only."""
    return dict(_WORKERS)


def reset_registry() -> None:
    """Test hook: drop every registration so a module can be re-imported cleanly."""
    global _DISCOVERED
    _WORKERS.clear()
    _DISCOVERED = False
