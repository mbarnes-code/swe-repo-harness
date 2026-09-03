"""`SharedLibContractAdapter` -- the third shipped `ContractAdapter` (SPEC 7.6, ADR-0065).

SHARED_LIB is the one kind whose layout() cannot recompute from contract.identifier: its
TARGET_PATH template is "{declared}" (workers/contracts.py:117), resolved from
scan.contracts.shared_libs[].dest -- a config value that lives in ContractsInput.config, not on
ContractNode. The only place the resolved dest survives onto the node is
ContractNode.hoist_target_path itself, so layout() reads that field rather than deriving it --
this is the one kind for which ContractAdapter.layout()'s abstract-method docstring ("Computed
once... persisted, never recomputed") is load-bearing rather than incidental.

Disclosed, inherited (not introduced) asymmetry: workers/contracts.py's hoist_target_path()
never calls is_reserved_dest on the {declared} branch (only normalize_dest), unlike every other
kind's template branch. layout() below mirrors that rather than silently tightening it --
changing that behavior is out of this adapter's scope.

neutral_targets() returns [] (SPEC 7.6, docs/SPEC.md:1339: "none -- it is an ordinary buildable
module"). binding_target()'s concrete shape (a single library-rule BuildTarget built directly
from source_paths, with no neutral target to depend on) is an Agent Recommendation: SPEC line
1339 describes the eventual BUILD-generation DRIVER delegating to
EcosystemAdapter.generate_targets(unit: BuildUnit) -- a method this ABC cannot call (it receives
a ContractNode, not a BuildUnit; constructing one is undesigned driver-layer plumbing, out of
scope here). Validate against a real fixture before trusting this body.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from fleet.ecosystems.contracts.base import ContractAdapter, register
from fleet.models.build import BuildTarget
from fleet.models.enums import ContractKind, Ecosystem
from fleet.models.graph import ContractNode


@register
class SharedLibContractAdapter(ContractAdapter):
    name: ClassVar[str] = "shared_lib"
    kind: ClassVar[ContractKind] = ContractKind.SHARED_LIB
    root: ClassVar[str] = "(declared)"  # documentary only -- no fixed root exists for "{declared}"

    def layout(self, contract: ContractNode) -> Path:
        if not contract.hoist_target_path:
            raise ValueError(
                f"{contract.contract_id}: SHARED_LIB has no persisted hoist_target_path -- a "
                "SHARED_LIB with no declared `dest:` is REJECTED at discovery and should never "
                "reach an adapter with a null path"
            )
        return Path(contract.hoist_target_path)

    def neutral_targets(self, contract: ContractNode) -> list[BuildTarget]:
        return []

    def binding_target(
        self, contract: ContractNode, ecosystem: Ecosystem, rule: str
    ) -> BuildTarget:
        package = str(self.layout(contract))
        base_name = contract.identifier.rsplit(".", 1)[-1] or "lib"
        srcs = [Path(p["path"]).name for p in contract.source_paths]
        return BuildTarget(
            package=package,
            name=f"{base_name}_{ecosystem.value.lower()}",
            rule=rule,  # e.g. "py_library" from contract_bindings[SHARED_LIB]
            srcs=srcs,
            visibility=["//visibility:public"],
        )
