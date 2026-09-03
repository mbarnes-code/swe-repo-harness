"""`OpenApiContractAdapter` -- the second shipped `ContractAdapter` (SPEC 7.6, ADR-0065).

Ports workers/contracts.py's TARGET_PATH[ContractKind.OPENAPI] == "contracts/openapi/{slug}"
and hoist_target_path()'s slug rule (workers/contracts.py:440-445): every non-alphanumeric run
in the casefolded identifier folds to a single "-". That rule (_DEST_ILLEGAL) is private to
workers/contracts.py and not exported; this module carries its own copy of the same pattern
rather than reaching into that module's internals, matching proto.py's precedent of importing
only from fleet.bazel.layout.

The {slug} folding has a disclosed consequence: it strips ALL non-alphanumeric characters
(including "_") rather than preserving path segments the way PROTO's {dotted} template does, so
is_reserved_dest's _scc-segment check can never trip through the slug itself (the folded
alphabet is [a-z0-9-], which cannot contain a literal "_") -- the call is kept for structural
parity with hoist_target_path()'s own template branch and as defense against a future root
change, not because today's {slug} template can reach it. See the test file for why this means
no reserved-dest control test is included here.

The neutral target is a filegroup, not a compiled-schema rule -- each consuming ecosystem's
genrule (from contract_bindings[OPENAPI], uniform across all 5 EcosystemAdapters today) depends
on it to run its own codegen step. The neutral/binding target name ("spec") is an Agent
Recommendation, not derived from SPEC or any existing generator: no OpenAPI-target generator
exists anywhere in src/ to port from -- validate against a real fixture before trusting it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import ClassVar

from fleet.bazel.layout import is_reserved_dest, normalize_dest
from fleet.ecosystems.contracts.base import ContractAdapter, register
from fleet.models.build import BuildTarget
from fleet.models.enums import ContractKind, Ecosystem
from fleet.models.graph import ContractNode

_DEST_ILLEGAL = re.compile(r"[^a-z0-9]+")  # local copy of workers/contracts.py's private pattern


@register
class OpenApiContractAdapter(ContractAdapter):
    name: ClassVar[str] = "openapi"
    kind: ClassVar[ContractKind] = ContractKind.OPENAPI
    root: ClassVar[str] = "contracts/openapi"

    def layout(self, contract: ContractNode) -> Path:
        slug = _DEST_ILLEGAL.sub("-", contract.identifier.casefold()).strip("-")
        dest = f"{self.root}/{slug}"
        normalized = normalize_dest(dest)
        if is_reserved_dest(normalized):
            raise ValueError(f"{contract.contract_id}: reserved dest {normalized!r}")
        return Path(normalized)

    def neutral_targets(self, contract: ContractNode) -> list[BuildTarget]:
        package = str(self.layout(contract))
        srcs = [Path(p["path"]).name for p in contract.source_paths]
        return [
            BuildTarget(
                package=package,
                name="spec",
                rule="filegroup",
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
            rule=rule,  # "genrule" from contract_bindings[OPENAPI], never looked up here
            deps=[neutral.label],
            visibility=["//visibility:public"],
        )
