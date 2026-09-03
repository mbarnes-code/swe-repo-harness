"""`ThriftContractAdapter` -- the fifth shipped `ContractAdapter` (SPEC §7.6, ADR-0065, round VI
task 40).

Mirrors `proto.py`'s shape exactly: `layout()` ports `workers/contracts.py`'s
`TARGET_PATH[ContractKind.THRIFT] == "contracts/thrift/{dotted}"` (`workers/contracts.py:117-123`).
The `{dotted}` identifier comes from `symbolindex.py`'s `_thrift_symbols` (`:596-609`): the
namespace directive's value -- the `*` (all-languages) slot if present, else the
lexicographically-first per-language slot -- a dot-separated string exactly like PROTO's bare
`package`. That tie-break is already resolved upstream in the extractor; this adapter just reads
`contract.identifier` and does no tie-break logic of its own.

Unlike PROTO, THRIFT already has a real, tested `contract_bindings` entry outside just one
ecosystem: `jvm.py` maps `ContractKind.THRIFT -> "java_thrift_library"`
(`src/fleet/ecosystems/jvm.py:59`) -- `py.py`/`js.py`/`rust.py`/`go.py` omit it deliberately (no
maintained Bazel rule outside the JVM), each with its own comment (§13 row 31's
`ContractBindingUnavailable` path is the driver-layer consequence, out of scope here).
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from fleet.bazel.layout import is_reserved_dest, normalize_dest
from fleet.ecosystems.contracts.base import ContractAdapter, register
from fleet.models.build import BuildTarget
from fleet.models.enums import ContractKind, Ecosystem
from fleet.models.graph import ContractNode


@register
class ThriftContractAdapter(ContractAdapter):
    name: ClassVar[str] = "thrift"
    kind: ClassVar[ContractKind] = ContractKind.THRIFT
    root: ClassVar[str] = "contracts/thrift"

    def layout(self, contract: ContractNode) -> Path:
        dest = f"{self.root}/{contract.identifier.replace('.', '/')}"
        normalized = normalize_dest(dest)
        if is_reserved_dest(normalized):
            raise ValueError(f"{contract.contract_id}: reserved dest {normalized!r}")
        return Path(normalized)

    def neutral_targets(self, contract: ContractNode) -> list[BuildTarget]:
        package = str(self.layout(contract))
        srcs = [Path(p["path"]).name for p in contract.source_paths]  # relocated basenames
        return [
            BuildTarget(
                package=package,
                name=contract.identifier.rsplit(".", 1)[-1],
                rule="thrift_library",
                srcs=srcs,
                visibility=["//visibility:public"],
            )
        ]

    def binding_target(
        self, contract: ContractNode, ecosystem: Ecosystem, rule: str
    ) -> BuildTarget:
        package = str(self.layout(contract))
        neutral = self.neutral_targets(contract)[0]
        return BuildTarget(
            package=package,
            name=f"{neutral.name}_{ecosystem.value.lower()}",
            rule=rule,  # from contract_bindings[THRIFT] -- never looked up here
            deps=[neutral.label],
            visibility=["//visibility:public"],
        )
