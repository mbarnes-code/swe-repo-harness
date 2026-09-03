"""Per-`ContractKind` hoist layout and BUILD emission (SPEC §7.6, ADR-0019, ADR-0065).

`fleet.ecosystems` (§7.5) answers *"where does a repo's own code go and what Bazel targets
describe it"*, keyed by `Ecosystem`. This package answers the sibling question for a
language-neutral contract node (a protobuf package, an OpenAPI document, ...): *"where does the
ONE hoisted target live, regardless of how many ecosystems consume it"* — keyed by `ContractKind`
instead, because putting that on `EcosystemAdapter` would leave every consuming ecosystem racing
to emit the same rule with no owner (§7.6).

Round VI task 19 (ADR-0065) ships the ABC, the `register`/`discover`/`for_kind` registry
mechanism, and ONE adapter (`proto.py`) as a working template. `openapi.py`/`avro.py`/`thrift.py`/
`shared_lib.py` are follow-on work — `discover()` therefore still raises today, correctly, naming
the four missing kinds (SPEC §7.6's marker; §12.32/§12.47 stay OPEN until all five land).
"""

from __future__ import annotations

from fleet.ecosystems.contracts.base import (
    ContractAdapter,
    ContractRegistryNotDiscoveredError,
    discover,
    for_kind,
    register,
    reset_adapters,
)

__all__ = [
    "ContractAdapter",
    "ContractRegistryNotDiscoveredError",
    "discover",
    "for_kind",
    "register",
    "reset_adapters",
]
