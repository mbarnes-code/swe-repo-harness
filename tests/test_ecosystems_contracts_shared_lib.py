"""tests/test_ecosystems_contracts_shared_lib.py.

Known-bad control here is not a reserved-dest case (layout() doesn't compute a dest; see the
module docstring) -- it is proving layout() reads `hoist_target_path` rather than recomputing from
`identifier` the way proto/openapi do. `test_layout_reads_the_persisted_field_not_the_identifier`
is that control: identifier and hoist_target_path are set to deliberately DIFFERENT values, so a
wrong port that recomputed from `identifier` (as if this were another template kind) would return
the wrong path and fail the assertion.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fleet.ecosystems.contracts.shared_lib import SharedLibContractAdapter
from fleet.models.enums import ContractKind, Ecosystem
from fleet.models.graph import ContractNode


def _shared_lib_node(
    identifier: str = "acme.common.utils",
    hoist_target_path: str | None = "libs/acme/common-utils",
    extractable: bool = True,
) -> ContractNode:
    return ContractNode(
        contract_id=f"shared_lib:{identifier}",
        kind=ContractKind.SHARED_LIB,
        identifier=identifier,
        source_paths=[{"repo_id": "acme-lib", "path": "src/utils.py", "blob_sha": "deadbeef"}],
        hoist_target_path=hoist_target_path,
        extractable=extractable,
        owning_repo_id="acme-lib" if extractable else None,
    )


def test_layout_reads_the_persisted_field_not_the_identifier() -> None:
    """`identifier` folds to nothing like the returned path -- proves layout() is not recomputing
    a template from it the way ProtoContractAdapter/OpenApiContractAdapter do."""
    adapter = SharedLibContractAdapter()
    node = _shared_lib_node(
        identifier="acme.common.utils", hoist_target_path="totally/different/declared-dest"
    )

    assert adapter.layout(node) == Path("totally/different/declared-dest")


def test_layout_raises_on_a_rejected_shared_lib_with_no_declared_dest() -> None:
    """A SHARED_LIB with no `dest:` is REJECTED before extraction (hoist_target_path stays None);
    layout() must raise rather than crash on Path(None) if ever called on such a node."""
    adapter = SharedLibContractAdapter()
    node = _shared_lib_node(hoist_target_path=None, extractable=False)

    with pytest.raises(ValueError, match=r"no persisted hoist_target_path"):
        adapter.layout(node)


def test_neutral_targets_returns_empty_list() -> None:
    """SPEC 7.6 / docs/SPEC.md:1339: SHARED_LIB has no neutral rule, delegated wholesale."""
    adapter = SharedLibContractAdapter()
    node = _shared_lib_node()

    assert adapter.neutral_targets(node) == []


def test_binding_target_carries_its_own_srcs_with_no_neutral_target_to_depend_on() -> None:
    adapter = SharedLibContractAdapter()
    node = _shared_lib_node()

    binding = adapter.binding_target(node, Ecosystem.PYPI, "py_library")

    assert binding.rule == "py_library"
    assert binding.package == "libs/acme/common-utils"
    assert binding.name == "utils_pypi"
    assert binding.srcs == ["utils.py"]
    assert binding.deps == []  # no neutral target exists for this kind
