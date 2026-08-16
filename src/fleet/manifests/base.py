"""`ManifestAdapter` — the ONLY place per-language knowledge is permitted to live
(SPEC §7.3, ADR-0005).

Exactly three abstract methods: `matches` (cheap, no file reads), `parse` (static parse into
`RawDependency`) and `coordinate` (normalize into the one address space, ADR-0017). Adding an
ecosystem is a file drop into `fleet/manifests/` plus a `@register` decorator; nothing in the
graph, transform, build or verify layers changes.

Every parse in this package is **static, offline and deterministic**: file bytes in, values out.
No adapter may open a socket, invoke a resolver (`mvn`, `npm`, `go`, `cargo`, `pip`), or shell
out to anything. A resolver would make the fleet graph depend on a registry's mood on the day
of the run, and §12.21's run-equivalence digest exists precisely to catch that.
"""

from __future__ import annotations

import importlib
import pkgutil
import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from fleet.models.enums import Ecosystem
from fleet.models.repo import Coordinate, RawDependency

if TYPE_CHECKING:
    from types import ModuleType

    from fleet.models.graph import DependencyEdge


class ManifestParseError(Exception):
    """Raised by `parse` to mark a manifest `low_confidence`.

    A low-confidence manifest becomes eligible for the ADR-0008 class-2 LLM extraction slot.
    It is never silently dropped: the `ManifestRef` row keeps `parse_error` verbatim.

    Every message MUST begin with the offending path (Rule 11): a parse that returns `[]`
    because the file was garbage is indistinguishable, downstream, from a manifest that
    genuinely declares no dependencies — and that difference is a dropped graph edge.
    """


class ManifestAdapter(ABC):
    """The ONLY place per-language knowledge is permitted to live. Exactly three methods."""

    name: ClassVar[str]
    ecosystem: ClassVar[Ecosystem]
    version: ClassVar[int] = 1  # bump forces re-parse of cached manifests
    priority: ClassVar[int] = 100  # lower wins when two adapters match the same path

    @abstractmethod
    def matches(self, path: Path) -> bool:
        """Cheap filename/extension test. No file reads."""

    @abstractmethod
    def parse(self, path: Path) -> list[RawDependency]:
        """Static parse only. Raise ManifestParseError to mark the manifest low_confidence
        (which makes it eligible for the ADR-0008 class-2 LLM extraction slot)."""

    @abstractmethod
    def coordinate(self, raw: RawDependency) -> Coordinate:
        """Normalize into the one address space every downstream layer sees."""

    def publishes(self, path: Path) -> Coordinate | None:
        """The coordinate this manifest itself publishes, if any. Default: None."""
        return None

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={getattr(self, 'name', '?')!r} p={self.priority}>"


# --------------------------------------------------------------------------------------
# Shared, ecosystem-neutral parse helpers. They live here rather than in a sibling module so
# that every adapter fails loud in the SAME shape: "<path>: <what went wrong>".
# --------------------------------------------------------------------------------------


def read_text(path: Path) -> str:
    """Read a manifest as UTF-8, converting any I/O or decoding failure into a loud parse error.

    A manifest we cannot read is never an empty manifest.
    """
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ManifestParseError(f"{path}: unreadable manifest: {exc}") from exc


def as_table(obj: object, *, path: Path, what: str) -> dict[str, object]:
    """Narrow an untyped JSON/TOML node to a string-keyed table, or fail loud."""
    if not isinstance(obj, dict):
        raise ManifestParseError(
            f"{path}: expected a table for {what}, got {type(obj).__name__}"
        )
    return {str(k): v for k, v in obj.items()}


def as_str(obj: object, *, path: Path, what: str) -> str:
    """Narrow an untyped JSON/TOML node to a string, or fail loud."""
    if not isinstance(obj, str):
        raise ManifestParseError(
            f"{path}: expected a string for {what}, got {type(obj).__name__}"
        )
    return obj


def opt_str(obj: object) -> str | None:
    """A string if it is one and non-empty after stripping, else None. Never raises."""
    if isinstance(obj, str) and obj.strip():
        return obj.strip()
    return None


# --------------------------------------------------------------------------------------
# Registry (SPEC §7.2's mechanism, §7.3's shape: instances, not classes)
# --------------------------------------------------------------------------------------

_ADAPTERS: list[ManifestAdapter] = []
_DISCOVERED = False


def register[A: type[ManifestAdapter]](cls: A) -> A:
    """Self-registration decorator; keeps `_ADAPTERS` sorted so dispatch is total.

    Same duplicate-is-a-startup-error rule as §7.2 — this registry is keyed by no dict, so the
    check is explicit rather than free. The sort key is `(priority, name)`, not `priority`:
    `list.sort` is stable, so a bare priority sort leaves equal-priority adapters in `pkgutil`
    import order, and the same repo would then yield different `Coordinate`s across runs —
    which silently breaks the §12.21 run-equivalence digest rather than failing anything.
    """
    name = getattr(cls, "name", None)
    if not name:
        raise RuntimeError(f"{cls.__qualname__} must declare a ClassVar `name` to register")
    existing = next((a for a in _ADAPTERS if a.name == name), None)
    if existing is not None:
        raise RuntimeError(
            f"duplicate manifest adapter name: {name} "
            f"({type(existing).__module__}.{type(existing).__qualname__} vs "
            f"{cls.__module__}.{cls.__qualname__})"
        )
    _ADAPTERS.append(cls())
    _ADAPTERS.sort(key=lambda a: (a.priority, a.name))
    return cls


def discover(*, force: bool = False) -> list[ManifestAdapter]:
    """Import every module in `fleet.manifests`, then return the registry in dispatch order.

    Import-time side effects are limited to registration. An import error is *not* swallowed
    (Rule 11): an adapter module that cannot be imported is a broken run, not a missing
    ecosystem.

    Asserts `vars(inst) == {}` for every registered instance (§7.2). The registry holds
    singletons shared across the runner's whole TaskGroup fan-out, so an instance attribute
    other than a `ClassVar` is a defect, not a style preference: an adapter caching a parsed
    lockfile on `self` leaks repo A's dependencies into repo B.
    """
    global _DISCOVERED
    if _DISCOVERED and not force:
        return list(_ADAPTERS)

    # `force` after `reset_adapters()` must re-run the decorators, and a module already in
    # `sys.modules` will not re-execute on plain import. Reloading is safe only while the
    # registry is empty, otherwise the re-run decorator trips its own duplicate check.
    reload = force and not _ADAPTERS
    package: ModuleType = importlib.import_module("fleet.manifests")
    for module in pkgutil.iter_modules(package.__path__, prefix=f"{package.__name__}."):
        leaf = module.name.rsplit(".", 1)[-1]
        if leaf.startswith("_") or leaf == "base":
            continue
        loaded = sys.modules.get(module.name)
        if reload and loaded is not None:
            importlib.reload(loaded)
        else:
            importlib.import_module(module.name)

    for inst in _ADAPTERS:
        state = vars(inst)
        if state:
            raise RuntimeError(
                f"stateful ManifestAdapter {inst.name!r} "
                f"({type(inst).__module__}.{type(inst).__qualname__}): "
                f"instance attributes {sorted(state)} — adapters are shared singletons and "
                f"MUST be stateless; use a ClassVar or a local"
            )
    _DISCOVERED = True
    return list(_ADAPTERS)


def adapters() -> list[ManifestAdapter]:
    """The registry as it stands, in `(priority, name)` order. Does not force discovery."""
    return list(_ADAPTERS)


def adapter_for(path: Path) -> ManifestAdapter | None:
    """First adapter (by priority) that claims `path`; `unknown.py` is the last-resort catch.

    After `discover()` this is total — `UnknownAdapter.matches` returns True for anything and
    sits at priority 10 000 — so an unrecognized repo gets the unknown adapter rather than an
    exception (§3.1 step 2: "a repo is never silently dropped"). `None` is therefore reachable
    only from a deliberately emptied registry in a test.
    """
    discover()
    return next((a for a in _ADAPTERS if a.matches(path)), None)


def reset_adapters() -> None:
    """Test hook: drop every registration so a module can be re-imported cleanly."""
    global _DISCOVERED
    _ADAPTERS.clear()
    _DISCOVERED = False


def interrogate(repo_path: Path, repo_id: str) -> list[DependencyEdge]:
    """One walk, one dispatch table, one output contract (ADR-0005).

    Not implemented in this scaffold: Phase 1 step 2/3 (`fleet.workers.interrogate`) owns the
    walk, the `ManifestRef` persistence and the coordinate resolution that turns a
    `RawDependency` into a `DependencyEdge`.
    """
    raise NotImplementedError("manifest interrogation is implemented by fleet.workers.interrogate")
