"""`ContractAdapter` registry mechanism (SPEC §7.6, ADR-0065, round VI task 19).

Mirrors `tests/test_ecosystems.py`'s registry-mechanism tests (`test_duplicate_*`,
`test_for_ecosystem_before_discovery_fails_loud`), adapted for `ContractKind` instead of
`Ecosystem`.

**Why fake, test-local adapters rather than the real registry:** these tests exercise the
`register()`/`for_kind()` **mechanism** in isolation — duplicate-claim detection, the
no-declared-`kind` guard, pre-discovery lookup failure — independently of whatever the real
`fleet.ecosystems.contracts` package happens to contain, the same way `_FakeProtoAdapter` et al.
let each mechanism test control exactly the registrations it needs.
[2026-09-06, round VI task 61: corrected — the prior text here read "the real
`fleet.ecosystems.contracts` package ships only `proto.py` today (ADR-0065) — four of the five
`ContractKind` members have no adapter yet, so the real `discover()` always raises its own
missing-members check, correctly." That was true through round VI task 19 and was falsified by
task 40 (`f3c0200`), which shipped the remaining four adapters; the real `discover()` bijection is
now total (see `test_the_real_contracts_registry_is_a_total_bijection_over_contractkind` below).]
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import ClassVar

import pytest

from fleet.ecosystems.contracts.base import (
    ContractAdapter,
    ContractRegistryNotDiscoveredError,
    discover,
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
    register fake adapters or provoke a duplicate-registration error.

    Teardown restores the REAL registry (`discover(force=True)`), it does not just clear it —
    mirroring `tests/test_ecosystems.py:41-51`'s landed precedent for the sibling registry. A
    bare `reset_adapters()` teardown leaves `_BY_KIND` empty afterwards, and the real
    `fleet.ecosystems.contracts` adapter modules are, by the time this file's own tests have run,
    already in `sys.modules` — so a later PLAIN `discover()` (`force=False`) cannot repopulate the
    registry (`importlib.import_module` does not re-execute an already-imported module, so the
    `@register` decorators never re-run). `src/fleet/cli.py`'s `fleet build` PASS 2b calls exactly
    that plain `discover()`, and a real pytest session that collects this file ahead of it left the
    contracts registry permanently empty for the rest of the session, raising `RuntimeError`
    downstream (round VI task 61 / research-34; the D-number is the controller's to allocate).
    `force=True` here re-runs `importlib.reload` on the five adapter modules, which re-executes
    their `@register` decorators and repopulates `_BY_KIND` (`base.py:143`,
    `reload = force and not _BY_KIND`)."""
    reset_adapters()
    yield
    reset_adapters()
    discover(force=True)


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


def test_the_real_contracts_registry_is_a_total_bijection_over_contractkind() -> None:
    """§12.32's second half: `fleet.ecosystems.contracts.discover()` satisfies the same key-set
    equality against `set(ContractKind)` that `tests/test_ecosystems.py:59-70`
    `test_discover_is_a_total_bijection_over_ecosystem` proves for the sibling `ecosystems`
    registry. Unlike that fixture-scoped mechanism-only file, THIS test drives the real,
    production `fleet.ecosystems.contracts` package.

    **Why:** `for_kind()` is total by construction only *if* the registry is. A `ContractKind`
    member with no adapter would not surface as a missing feature at discovery time — it would
    surface as `fleet build`'s PASS 2b (`src/fleet/cli.py`) raising
    `ContractRegistryNotDiscoveredError` the first time a repo actually used that kind, potentially
    days into a run, rather than failing loudly where the defect actually is. `discover()`'s own
    internal check (`base.py:155-160`) only proves the SUPERSET direction (every `ContractKind`
    member has a registered adapter) and only fires when `discover()` is actually called; nothing
    in `src/` catches a key registered OUTSIDE `ContractKind` (`register()`, `base.py:107`, does
    not check `isinstance(kind, ContractKind)`), so this test also asserts the SUBSET direction the
    implementation itself does not cover.

    `force=True`: the autouse `_clean_registry` fixture clears the registry before every test in
    this file, and by the time this test runs the five real adapter modules are typically already
    in `sys.modules` (imported at collection by the sibling
    `tests/test_ecosystems_contracts_avro.py` etc. files, or by this file's own module-level
    `import register` machinery pulling in `base`) — a plain `discover()` cannot re-populate an
    already-emptied registry from an already-imported module (`importlib.import_module` does not
    re-execute a cached module, so `@register` never re-runs; see the fixture's own docstring for
    the full mechanism). Forcing here mirrors the fixture's own teardown."""
    registry = discover(force=True)

    assert set(registry) == set(ContractKind)

    instances = list(registry.values())
    assert len({id(inst) for inst in instances}) == len(instances), (
        "every ContractKind gets its OWN adapter instance (unlike ecosystems' MAVEN/GRADLE, which "
        "deliberately share one) -- a shared instance here would mean two kinds silently reuse "
        "one adapter's layout()/binding_target() logic, which is not what any adapter module "
        "declares"
    )

    assert {adapter.name for adapter in instances} == {
        "avro",
        "openapi",
        "proto",
        "shared_lib",
        "thrift",
    }


def test_discover_raises_on_a_stateful_registered_instance_naming_it_and_its_attributes() -> None:
    """§12.47's core enforcement mechanism (`base.py:161-169`) has no prior test anywhere in this
    file or its siblings -- every adapter test file proves ITS OWN adapter happens to be stateless,
    never that `discover()` would actually catch one that wasn't. `register()` itself does not
    check statelessness (it only guards duplicate `kind`/missing `kind` -- see
    `test_duplicate_kind_registration_raises_naming_both_claimants` and
    `test_register_without_a_declared_kind_raises` above); the check lives only in `discover()`'s
    per-instance loop.

    Sabotages a REAL registered instance (rather than a fake test-local adapter class) so the
    `discover(force=True)` re-scan hits the exact `for inst in _BY_KIND.values(): if vars(inst):
    raise ...` loop with a real singleton already in place -- `force=True` re-enters that loop
    even though `_BY_KIND` is non-empty (so no module reload actually happens; the check runs
    regardless, which is also what the short-circuit sibling test below relies on)."""
    discover(force=True)
    adapter = for_kind(ContractKind.PROTO)
    adapter.__dict__["_leaked_state"] = "should never survive a real re-scan"
    try:
        with pytest.raises(RuntimeError, match=r"stateful ContractAdapter") as excinfo:
            discover(force=True)
        message = str(excinfo.value)
        assert "proto" in message
        assert "_leaked_state" in message
    finally:
        del adapter.__dict__["_leaked_state"]


def test_discover_short_circuits_without_rechecking_statelessness_once_already_discovered() -> (
    None
):
    """`discover()`'s `if _DISCOVERED and not force: return dict(_BY_KIND)` (`base.py:137-138`)
    is the caching fast path every ordinary caller (e.g. `fleet build`'s repeated PASS 2b calls)
    takes after the first real scan. No existing test distinguishes it from simply calling the
    full scan-and-verify path twice with nothing having changed -- both would pass. This sabotages
    a live registered instance's state AFTER a real `discover(force=True)`, then calls a plain
    `discover()`: if the short circuit is real, the sabotage is never re-checked and no error is
    raised; if the short circuit were removed (mutation), the per-instance statelessness loop
    would re-run and raise on the sabotaged instance."""
    discover(force=True)
    adapter = for_kind(ContractKind.PROTO)
    adapter.__dict__["_leaked"] = "state a real re-scan would reject"
    try:
        result = discover()  # force=False -- must short-circuit, not re-verify
        assert result[ContractKind.PROTO] is adapter
    finally:
        del adapter.__dict__["_leaked"]
