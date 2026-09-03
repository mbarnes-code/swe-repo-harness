"""`ContractAdapter` registry mechanism (SPEC §7.6, ADR-0065, round VI task 19).

Mirrors `tests/test_ecosystems.py`'s registry-mechanism tests (`test_duplicate_*`,
`test_for_ecosystem_before_discovery_fails_loud`), adapted for `ContractKind` instead of
`Ecosystem`.

**Why fake, test-local adapters rather than the real registry:** the real
`fleet.ecosystems.contracts` package ships only `proto.py` today (ADR-0065) — four of the five
`ContractKind` members have no adapter yet, so the real `discover()` always raises its own
missing-members check, correctly. Exercising `register()`/`for_kind()` therefore needs adapters
this test controls, not the real (deliberately incomplete) package walk.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import ClassVar

import pytest

from fleet.ecosystems.contracts.base import (
    ContractAdapter,
    ContractRegistryNotDiscoveredError,
    for_kind,
    register,
    reset_adapters,
)
from fleet.models.build import BuildTarget
from fleet.models.enums import ContractKind, Ecosystem
from fleet.models.graph import ContractNode


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    """`_BY_KIND` module state must not leak between tests: several tests below deliberately
    register fake adapters or provoke a duplicate-registration error."""
    reset_adapters()
    yield
    reset_adapters()


class _FakeProtoAdapter(ContractAdapter):
    name: ClassVar[str] = "fake-proto"
    kind: ClassVar[ContractKind] = ContractKind.PROTO
    root: ClassVar[str] = "fake-proto-root"

    def layout(self, contract: ContractNode) -> Path:
        return Path(self.root) / contract.identifier

    def neutral_targets(self, contract: ContractNode) -> list[BuildTarget]:
        return []

    def binding_target(
        self, contract: ContractNode, ecosystem: Ecosystem, rule: str
    ) -> BuildTarget:
        return BuildTarget(package=str(self.layout(contract)), name="x", rule=rule)


class _FakeOpenapiAdapter(ContractAdapter):
    name: ClassVar[str] = "fake-openapi"
    kind: ClassVar[ContractKind] = ContractKind.OPENAPI
    root: ClassVar[str] = "fake-openapi-root"

    def layout(self, contract: ContractNode) -> Path:
        return Path(self.root) / contract.identifier

    def neutral_targets(self, contract: ContractNode) -> list[BuildTarget]:
        return []

    def binding_target(
        self, contract: ContractNode, ecosystem: Ecosystem, rule: str
    ) -> BuildTarget:
        return BuildTarget(package=str(self.layout(contract)), name="x", rule=rule)


def test_for_kind_resolves_each_registered_adapter_to_the_class_that_claimed_its_kind() -> None:
    register(_FakeProtoAdapter)
    register(_FakeOpenapiAdapter)

    assert isinstance(for_kind(ContractKind.PROTO), _FakeProtoAdapter)
    assert isinstance(for_kind(ContractKind.OPENAPI), _FakeOpenapiAdapter)


def test_duplicate_kind_registration_raises_naming_both_claimants() -> None:
    """**Why:** a silent overwrite makes the surviving adapter a function of import order, so
    the same fleet would emit a different rule for the same `ContractKind` on different hosts
    with nothing in the logs saying so — the message must name both classes."""
    register(_FakeProtoAdapter)

    class Intruder(ContractAdapter):
        name: ClassVar[str] = "intruder"
        kind: ClassVar[ContractKind] = ContractKind.PROTO
        root: ClassVar[str] = "intruder-root"

        def layout(self, contract: ContractNode) -> Path:
            return Path(self.root)

        def neutral_targets(self, contract: ContractNode) -> list[BuildTarget]:
            return []

        def binding_target(
            self, contract: ContractNode, ecosystem: Ecosystem, rule: str
        ) -> BuildTarget:
            return BuildTarget(package=self.root, name="x", rule=rule)

    with pytest.raises(RuntimeError) as excinfo:
        register(Intruder)
    message = str(excinfo.value)
    assert "PROTO" in message
    assert "Intruder" in message
    assert "_FakeProtoAdapter" in message


def test_register_without_a_declared_kind_raises() -> None:
    class NoKind(ContractAdapter):
        name: ClassVar[str] = "no-kind"
        root: ClassVar[str] = "no-kind-root"

        def layout(self, contract: ContractNode) -> Path:
            return Path(self.root)

        def neutral_targets(self, contract: ContractNode) -> list[BuildTarget]:
            return []

        def binding_target(
            self, contract: ContractNode, ecosystem: Ecosystem, rule: str
        ) -> BuildTarget:
            return BuildTarget(package=self.root, name="x", rule=rule)

    with pytest.raises(RuntimeError, match=r"must declare a ClassVar `kind`"):
        register(NoKind)


def test_for_kind_before_any_registration_fails_loud_not_silently_none() -> None:
    """**Why (Rule 11):** a caller silently receiving `None` for an unregistered kind would emit
    a BUILD file missing a rule for a real contract, rather than failing the run that needed it.
    """
    with pytest.raises(ContractRegistryNotDiscoveredError, match=r"PROTO"):
        for_kind(ContractKind.PROTO)
