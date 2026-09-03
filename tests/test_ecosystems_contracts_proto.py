"""`ProtoContractAdapter` — the one shipped `ContractAdapter` (SPEC §7.6, ADR-0065, round VI
task 19).

Note on `Ecosystem.PYTHON`: the task 19 brief's test sketch names `Ecosystem.PYTHON`, but the
real `fleet.models.enums.Ecosystem` (verified below) has no `PYTHON` member — the Python
ecosystem is `Ecosystem.PYPI` (`src/fleet/models/enums.py:220`). Using the real member here per
the brief's own "verify... before using it verbatim" instruction.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fleet.ecosystems.contracts.proto import ProtoContractAdapter
from fleet.models.enums import ContractKind, Ecosystem
from fleet.models.graph import ContractNode


def _proto_node(
    identifier: str = "acme.widgets.v1", contract_id: str | None = None
) -> ContractNode:
    return ContractNode(
        contract_id=contract_id or f"proto:{identifier}",
        kind=ContractKind.PROTO,
        identifier=identifier,
        source_paths=[{"repo_id": "acme-lib", "path": "widgets.proto", "blob_sha": "deadbeef"}],
    )


def test_layout_ports_the_target_path_dotted_template() -> None:
    """Ports `workers/contracts.py`'s `TARGET_PATH[ContractKind.PROTO] == "proto/{dotted}"`."""
    adapter = ProtoContractAdapter()
    node = _proto_node()

    assert adapter.layout(node) == Path("proto/acme/widgets/v1")


def test_neutral_targets_emits_one_proto_library_with_relocated_basenames() -> None:
    adapter = ProtoContractAdapter()
    node = _proto_node()

    targets = adapter.neutral_targets(node)

    assert len(targets) == 1
    target = targets[0]
    assert target.rule == "proto_library"
    assert target.package == "proto/acme/widgets/v1"
    assert target.name == "v1"
    assert target.srcs == ["widgets.proto"]  # relocated basename, not the source repo's own path


def test_binding_target_depends_on_the_neutral_targets_label_and_uses_the_supplied_rule() -> None:
    """`rule` is supplied by the driver from `contract_bindings[PROTO]` — this adapter never
    looks up a language itself (SPEC §7.6's ABC docstring)."""
    adapter = ProtoContractAdapter()
    node = _proto_node()
    neutral = adapter.neutral_targets(node)[0]

    binding = adapter.binding_target(node, Ecosystem.PYPI, "py_proto_library")

    assert binding.rule == "py_proto_library"
    assert binding.package == neutral.package
    assert neutral.label in binding.deps


def test_layout_raises_rather_than_silently_emitting_a_reserved_dest() -> None:
    """**Known-bad control (validate-the-instrument guardrail):** `is_reserved_dest` already
    exists and already flags `_scc`-rooted paths (`bazel/layout.py:155-157`); this proves the
    port actually calls it rather than silently emitting a broken path.

    `workers/contracts.py`'s `hoist_target_path()` (`:435-445`) returns `None` on this same
    condition because its return type is `str | None`; `ContractAdapter.layout()` returns `Path`
    with no `None` member, so the same "never emit a broken path" behavior is expressed as a
    raised `ValueError` instead (disclosed in `proto.py`'s module docstring).
    """
    adapter = ProtoContractAdapter()
    node = _proto_node(identifier="_scc.demo", contract_id="proto:reserved-collision-test")

    with pytest.raises(ValueError, match=r"reserved dest"):
        adapter.layout(node)
