"""tests/test_ecosystems_contracts_openapi.py -- mirrors test_ecosystems_contracts_proto.py.

No reserved-dest known-bad control: OpenApiContractAdapter.layout()'s `{slug}` folding
(_DEST_ILLEGAL = [^a-z0-9]+) strips every underscore, so no `identifier` can make the slug equal
the literal `_scc` -- is_reserved_dest is unreachable through this adapter's own template, a
disclosed pre-existing property of the ported hoist_target_path() slug rule, not a defect
introduced here. A test asserting a raise via `identifier` would not discriminate a broken port
from a correct one (Guardrail 6: audit mutations for expressibility before writing them). Instead,
`test_layout_still_calls_is_reserved_dest_even_though_it_cannot_fire_today` below is a structural
control: it monkeypatches the module's `is_reserved_dest` binding to always return True, proving
the call site exists (so a future `root` change that DOES reach `_scc` is still caught) without a
misleading identifier-driven fixture.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fleet.ecosystems.contracts.openapi import OpenApiContractAdapter
from fleet.models.enums import ContractKind, Ecosystem
from fleet.models.graph import ContractNode


def _openapi_node(identifier: str = "openapi/user_api.yaml") -> ContractNode:
    return ContractNode(
        contract_id=f"openapi:{identifier}",
        kind=ContractKind.OPENAPI,
        identifier=identifier,
        source_paths=[{"repo_id": "acme-api", "path": identifier, "blob_sha": "deadbeef"}],
    )


def test_layout_folds_the_whole_identifier_into_one_slug_segment() -> None:
    adapter = OpenApiContractAdapter()
    node = _openapi_node()

    assert adapter.layout(node) == Path("contracts/openapi/openapi-user-api-yaml")


def test_neutral_targets_emits_one_filegroup_with_relocated_basenames() -> None:
    adapter = OpenApiContractAdapter()
    node = _openapi_node()

    targets = adapter.neutral_targets(node)

    assert len(targets) == 1
    target = targets[0]
    assert target.rule == "filegroup"
    assert target.package == "contracts/openapi/openapi-user-api-yaml"
    assert target.srcs == ["user_api.yaml"]


def test_binding_target_depends_on_the_neutral_targets_label_and_uses_the_supplied_rule() -> None:
    adapter = OpenApiContractAdapter()
    node = _openapi_node()
    neutral = adapter.neutral_targets(node)[0]

    binding = adapter.binding_target(node, Ecosystem.PYPI, "genrule")

    assert binding.rule == "genrule"
    assert binding.package == neutral.package
    assert neutral.label in binding.deps


def test_layout_still_calls_is_reserved_dest_even_though_it_cannot_fire_today() -> None:
    """Not a behavior test -- a structural one, so a future `root` change that DOES reach `_scc`
    is still caught. Monkeypatches is_reserved_dest via the module's own import binding rather
    than crafting an identifier, since no identifier can do it (see module docstring)."""
    import fleet.ecosystems.contracts.openapi as mod

    adapter = OpenApiContractAdapter()
    node = _openapi_node()
    original = mod.is_reserved_dest
    mod.is_reserved_dest = lambda dest: True  # type: ignore[assignment]
    try:
        with pytest.raises(ValueError, match=r"reserved dest"):
            adapter.layout(node)
    finally:
        mod.is_reserved_dest = original
