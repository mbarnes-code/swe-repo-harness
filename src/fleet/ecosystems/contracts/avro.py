"""`AvroContractAdapter` -- the fourth shipped `ContractAdapter` (SPEC §7.6, ADR-0065, round VI
task 40).

Mirrors `proto.py`'s shape exactly: `layout()` ports `workers/contracts.py`'s
`TARGET_PATH[ContractKind.AVRO] == "contracts/avro/{dotted}"` (`workers/contracts.py:117-123`).
The `{dotted}` identifier comes from `symbolindex.py`'s `_avro_symbols` (`:580-593`): the bare
`namespace` field (`.avsc`) or `@namespace(...)` annotation (`.avdl`), a dot-separated string
exactly like PROTO's bare `package` -- confirmed by reading the extractor directly, not assumed
from symmetry with PROTO.

Unlike PROTO, AVRO already has a real, tested `contract_bindings` entry outside just one
ecosystem: `jvm.py` maps `ContractKind.AVRO -> "java_avro_library"`
(`src/fleet/ecosystems/jvm.py:58`) -- `py.py`/`js.py`/`rust.py`/`go.py` omit it deliberately (no
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
class AvroContractAdapter(ContractAdapter):
    name: ClassVar[str] = "avro"
    kind: ClassVar[ContractKind] = ContractKind.AVRO
    root: ClassVar[str] = "contracts/avro"

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
                rule="avro_library",
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
            rule=rule,  # from contract_bindings[AVRO] -- never looked up here
            deps=[neutral.label],
            visibility=["//visibility:public"],
        )
