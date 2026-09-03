"""`AvroContractAdapter` -- the fourth shipped `ContractAdapter` (SPEC §7.6, ADR-0065, round VI
task 40).

Note on `Ecosystem.JVM`: there is no such member -- the real `fleet.models.enums.Ecosystem`
(verified below) has `MAVEN`/`GRADLE` for the JVM. `test_ecosystems_contracts_proto.py` carries
an analogous note about `Ecosystem.PYTHON` not existing; using the real member here per the same
"verify... before using it verbatim" discipline.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fleet.ecosystems.contracts.avro import AvroContractAdapter
from fleet.models.enums import ContractKind, Ecosystem
from fleet.models.graph import ContractNode


def _avro_node(identifier: str = "acme.events.v1", contract_id: str | None = None) -> ContractNode:
    return ContractNode(
        contract_id=contract_id or f"avro:{identifier}",
        kind=ContractKind.AVRO,
        identifier=identifier,
        source_paths=[{"repo_id": "acme-lib", "path": "events.avsc", "blob_sha": "deadbeef"}],
    )


def test_layout_ports_the_target_path_dotted_template() -> None:
    """Ports `workers/contracts.py`'s
    `TARGET_PATH[ContractKind.AVRO] == "contracts/avro/{dotted}"`."""
    adapter = AvroContractAdapter()
    node = _avro_node()

    assert adapter.layout(node) == Path("contracts/avro/acme/events/v1")


def test_neutral_targets_emits_one_avro_library_with_relocated_basenames() -> None:
    adapter = AvroContractAdapter()
    node = _avro_node()

    targets = adapter.neutral_targets(node)

    assert len(targets) == 1
    target = targets[0]
    assert target.rule == "avro_library"
    assert target.package == "contracts/avro/acme/events/v1"
    assert target.name == "v1"
    assert target.srcs == ["events.avsc"]  # relocated basename, not the source repo's own path


def test_binding_target_depends_on_the_neutral_targets_label_and_uses_the_supplied_rule() -> None:
    """`rule` is supplied by the driver from `contract_bindings[AVRO]` -- this adapter never
    looks up a language itself (SPEC §7.6's ABC docstring). `jvm.py:58` maps
    `ContractKind.AVRO -> "java_avro_library"` -- a REAL contract_bindings entry, unlike PROTO's
    test, which uses `Ecosystem.PYPI`."""
    adapter = AvroContractAdapter()
    node = _avro_node()
    neutral = adapter.neutral_targets(node)[0]

    binding = adapter.binding_target(node, Ecosystem.MAVEN, "java_avro_library")

    assert binding.rule == "java_avro_library"
    assert binding.package == neutral.package
    assert neutral.label in binding.deps


def test_layout_raises_rather_than_silently_emitting_a_reserved_dest() -> None:
    """**Known-bad control (validate-the-instrument guardrail):** `is_reserved_dest` already
    exists and already flags `_scc`-rooted paths (`bazel/layout.py:155-157`); this proves the
    port actually calls it rather than silently emitting a broken path."""
    adapter = AvroContractAdapter()
    node = _avro_node(identifier="_scc.demo", contract_id="avro:reserved-collision-test")

    with pytest.raises(ValueError, match=r"reserved dest"):
        adapter.layout(node)
