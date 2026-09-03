"""`ContractAdapter` — the ABC + registry for per-`ContractKind` hoist layout and BUILD emission
(SPEC §7.6, ADR-0019, ADR-0065).

Ported from SPEC §7.6's design sketch (`docs/SPEC.md:5638-5696`). The registry mechanism mirrors
`fleet.ecosystems.base`'s `register`/`discover`/`for_ecosystem`/`reset_adapters` (§7.2's pattern),
keyed by `ContractKind` instead of by `{name, ecosystems}` — a contract node has exactly one kind,
so there is no second key to check the way `ecosystems/base.py` checks `name` in addition to
`ecosystems`.

**Round VI task 40 status:** all five adapters are shipped (`proto.py`, `openapi.py`,
`shared_lib.py`, `avro.py`, `thrift.py`) — `discover()`'s bijection over `ContractKind` is total
and every registered instance is stateless (§12.47's contracts-registry sub-clause). Not yet
wired: no caller anywhere in `src/fleet/workers/buildgen.py`, `src/fleet/bazel/generators.py`, or
`src/fleet/cli.py` invokes `for_kind()`/`neutral_targets()`/`binding_target()` to actually emit a
contract's Bazel targets — see `docs/INTEGRATION_HONESTY.md`'s D113 for that separate gap
(§12.34's Clause B), which also needs the registry populated without tripping this file's own
bijection assert for a partial `discover()` call.
"""

from __future__ import annotations

import importlib
import pkgutil
import sys
from abc import ABC, abstractmethod
from pathlib import Path
from types import ModuleType
from typing import ClassVar

from fleet.models.build import BuildTarget, ToolchainRequirement
from fleet.models.enums import ContractKind, Ecosystem
from fleet.models.graph import ContractNode

__all__ = [
    "ContractAdapter",
    "ContractRegistryNotDiscoveredError",
    "discover",
    "for_kind",
    "register",
    "reset_adapters",
]


class ContractAdapter(ABC):
    """Per-IDL-kind hoist layout and BUILD emission. Language-neutral by construction: the only
    per-language value it uses is the rule NAME, read from EcosystemAdapter.contract_bindings."""

    name: ClassVar[str]
    kind: ClassVar[ContractKind]
    root: ClassVar[str]  # 'proto', 'contracts/openapi', ... ; the layout root (§3.3)
    version: ClassVar[int] = 1

    @abstractmethod
    def layout(self, contract: ContractNode) -> Path:
        """`hoist_target_path`. Computed once in §3.1 step 5b, persisted, never recomputed."""

    @abstractmethod
    def neutral_targets(self, contract: ContractNode) -> list[BuildTarget]:
        """The language-independent rule(s): proto_library / avro_library / thrift_library /
        filegroup. Exactly one generator invocation per contract, which is the whole point of
        hoisting (§13 row 29). SHARED_LIB returns [] and delegates to its EcosystemAdapter."""

    @abstractmethod
    def binding_target(
        self, contract: ContractNode, ecosystem: Ecosystem, rule: str
    ) -> BuildTarget:
        """One per consuming ecosystem. `rule` is supplied by the driver from
        `for_ecosystem(ecosystem).contract_bindings[self.kind]` — this ABC never looks up a
        language itself, so no `if ecosystem ==` can appear here either."""

    def toolchain_requirements(self) -> list[ToolchainRequirement]:
        return []


class ContractRegistryNotDiscoveredError(RuntimeError):
    """`for_kind()` was called for a kind with no registered adapter — either `discover()` has
    not run in this process yet, or (today, ADR-0065) the kind's adapter module does not exist.

    Raised rather than returning `None`, matching `ecosystems.base.RegistryNotDiscoveredError`:
    a caller silently treating a missing adapter as "no binding" would emit a BUILD file missing
    a rule for a real contract rather than failing the run that needed it (Rule 11).
    """

    def __init__(self, kind: ContractKind) -> None:
        super().__init__(
            f"contracts.discover() has not registered an adapter for {kind.value!r} in this "
            f"process. Either discover() has not run yet, or (ADR-0065, round VI task 19) this "
            f"kind's adapter has not shipped yet."
        )


# =======================================================================================
# Registry (§7.2's mechanism, keyed by ContractKind instead of by {name, ecosystems})
# =======================================================================================

_BY_KIND: dict[ContractKind, ContractAdapter] = {}
_DISCOVERED = False


def register[A: type[ContractAdapter]](cls: A) -> A:
    """Self-registration decorator — the §7.2 mechanism, keyed by `ContractKind`.

    A duplicate claim is a startup error, not a silent overwrite, and the message names **both**
    claimants — matching `ecosystems.base.register`'s reasoning: with a silent overwrite the
    surviving adapter would depend on `pkgutil` import order.
    """
    kind = getattr(cls, "kind", None)
    if kind is None:
        raise RuntimeError(f"{cls.__qualname__} must declare a ClassVar `kind` to register")
    name = getattr(cls, "name", None)
    if not name:
        raise RuntimeError(f"{cls.__qualname__} must declare a ClassVar `name` to register")
    existing = _BY_KIND.get(kind)
    if existing is not None:
        raise RuntimeError(
            f"duplicate ContractAdapter for {kind.value}: {cls.name} "
            f"({type(existing).__module__}.{type(existing).__qualname__} vs "
            f"{cls.__module__}.{cls.__qualname__})"
        )
    _BY_KIND[kind] = cls()
    return cls


def discover(*, force: bool = False) -> dict[ContractKind, ContractAdapter]:
    """Import every module in `fleet.ecosystems.contracts`, then assert the registry is TOTAL
    over `ContractKind` and every registered singleton is stateless (§7.6, §13 row 31).

    Mirrors `ecosystems.base.discover()`'s structure exactly, keyed by `ContractKind`. **Today
    (ADR-0065, round VI task 19) this always raises**: only `proto.py` is shipped, so four of
    five kinds are unregistered. That failure is correct and expected until the remaining four
    adapters land — see this module's docstring and SPEC §7.6's marker.
    """
    global _DISCOVERED
    if _DISCOVERED and not force:
        return dict(_BY_KIND)

    # `force` after `reset_adapters()` must re-run the decorators, and a module already in
    # `sys.modules` will not re-execute on plain import. Reloading is safe only while the
    # registry is empty, otherwise the re-run decorator trips its own duplicate check.
    reload = force and not _BY_KIND
    package: ModuleType = importlib.import_module("fleet.ecosystems.contracts")
    for module in pkgutil.iter_modules(package.__path__, prefix=f"{package.__name__}."):
        leaf = module.name.rsplit(".", 1)[-1]
        if module.ispkg or leaf.startswith("_") or leaf == "base":
            continue
        loaded = sys.modules.get(module.name)
        if reload and loaded is not None:
            importlib.reload(loaded)
        else:
            importlib.import_module(module.name)

    missing = sorted(k.value for k in ContractKind if k not in _BY_KIND)
    if missing:
        raise RuntimeError(
            f"no ContractAdapter is registered for {missing}; §7.6 requires the registry to be "
            f"a total bijection over ContractKind before discover() can succeed (§12.32, §12.47)"
        )
    for inst in _BY_KIND.values():
        state = vars(inst)
        if state:
            raise RuntimeError(
                f"stateful ContractAdapter {inst.name!r} "
                f"({type(inst).__module__}.{type(inst).__qualname__}): instance attributes "
                f"{sorted(state)} — adapters are shared singletons and MUST be stateless; use a "
                f"ClassVar or a local"
            )
    _DISCOVERED = True
    return dict(_BY_KIND)


def for_kind(kind: ContractKind) -> ContractAdapter:
    """Total by construction after a successful `discover()`. Raises rather than returning
    `None` when no adapter is registered for `kind` (Rule 11) — see
    `ContractRegistryNotDiscoveredError`."""
    adapter = _BY_KIND.get(kind)
    if adapter is None:
        raise ContractRegistryNotDiscoveredError(kind)
    return adapter


def reset_adapters() -> None:
    """Test hook: drop every registration so a module can be re-imported cleanly."""
    global _DISCOVERED
    _BY_KIND.clear()
    _DISCOVERED = False
