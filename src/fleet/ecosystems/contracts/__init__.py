"""Per-`ContractKind` hoist layout and BUILD emission (SPEC §7.6, ADR-0019, ADR-0065).

`fleet.ecosystems` (§7.5) answers *"where does a repo's own code go and what Bazel targets
describe it"*, keyed by `Ecosystem`. This package answers the sibling question for a
language-neutral contract node (a protobuf package, an OpenAPI document, ...): *"where does the
ONE hoisted target live, regardless of how many ecosystems consume it"* — keyed by `ContractKind`
instead, because putting that on `EcosystemAdapter` would leave every consuming ecosystem racing
to emit the same rule with no owner (§7.6).

Round VI task 19 (ADR-0065) shipped the ABC and the `register`/`discover`/`for_kind` registry
mechanism, with `proto.py` as a working template. Round VI task 40 (`f3c0200`) landed the
remaining four adapters (`openapi.py`/`avro.py`/`thrift.py`/`shared_lib.py`); `discover()`'s
bijection over `ContractKind` is now total and it no longer raises on an ordinary call (SPEC
§7.6's marker; §12.47 closed on this commit — see `docs/CRITERIA_PLAN.md`).
[2026-09-06, round VI research-33: corrected — the prior text describing an unconditional raise
was true 2026-09-03 through task 19, falsified by task 40.]
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
