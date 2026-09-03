"""`ProtoContractAdapter` — the one shipped `ContractAdapter` (SPEC §7.6, ADR-0065, round VI
task 19). Confirmed the right first pick (not a guess, per the task 19 brief): `PROTO` is the
only `ContractKind` with a real, existing `contract_bindings` entry in every one of the five
`EcosystemAdapter`s today (`py.py`, `js.py`, `jvm.py`, `go.py`, `rust.py` each map
`ContractKind.PROTO` to a real rule name, e.g. `py_proto_library`).

`layout()` ports `workers/contracts.py`'s `hoist_target_path()` / `TARGET_PATH[ContractKind.PROTO]
== "proto/{dotted}"` template (`workers/contracts.py:112-119`, `:435-445`). One behavioral
adaptation from that port, disclosed per the task brief: `hoist_target_path()` returns `None` on
a reserved dest (`_scc`) because its return type is `str | None`; `layout()`'s return type is
`Path` with no `None` member, so the same "never emit a broken path" behavior is expressed as a
raised `ValueError` instead — the caller cannot silently receive a dest under Bazel's own
reserved namespace (§3.1 6e).
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
class ProtoContractAdapter(ContractAdapter):
    name: ClassVar[str] = "proto"
    kind: ClassVar[ContractKind] = ContractKind.PROTO
    root: ClassVar[str] = "proto"

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
                rule="proto_library",
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
            rule=rule,  # from contract_bindings[PROTO] -- never looked up here
            deps=[neutral.label],
            visibility=["//visibility:public"],
        )
