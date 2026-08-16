"""The OUTPUT-side language boundary (SPEC §7.5, ADR-0020, §8).

`manifests/` answers *"what does this repo depend on"*; this package answers *"where does it go
and what Bazel targets describe it"*. Six adapters ship — `jvm` (MAVEN + GRADLE), `js` (NPM),
`py` (PYPI), `go` (GO, delegating to Gazelle), `rust` (CARGO) and `unknown` (the §3.1 step 2
floor) — and together they are a **total bijection** over `Ecosystem`, which is what makes
`layout()` (§3.3) and the §3.3 step 2 driver fallback-free rather than defensively branched.

The module itself is the registry: `fleet.ecosystems` satisfies `bazel.layout.EcosystemRegistry`
structurally, so `layout(node, ecosystems)` needs no wrapper object between the driver and the
adapters.

Adding an ecosystem is a file drop plus `@register`. Nothing in `graph/`, `bazel/`,
`orchestrator/` or `workers/` changes — those modules take injected maps and Protocols precisely
so that per-language knowledge cannot leak into them (§12.6, §13 row 33).
"""

from __future__ import annotations

from fleet.ecosystems.base import (
    AdapterCoordinateError,
    EcosystemAdapter,
    RegistryNotDiscoveredError,
    adapters,
    discover,
    extension_bzls,
    for_ecosystem,
    library_loads,
    library_rules,
    monorepo_dirs,
    register,
    reset_adapters,
    ruleset_repo_names,
)

__all__ = [
    "AdapterCoordinateError",
    "EcosystemAdapter",
    "RegistryNotDiscoveredError",
    "adapters",
    "discover",
    "extension_bzls",
    "for_ecosystem",
    "library_loads",
    "library_rules",
    "monorepo_dirs",
    "register",
    "reset_adapters",
    "ruleset_repo_names",
]
