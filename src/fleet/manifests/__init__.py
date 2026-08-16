"""`ManifestAdapter` plugins — the only place per-language knowledge lives (ADR-0005).

Every adapter here is a stateless singleton registered by importing this package's modules
(`fleet.manifests.base.discover`). Parsing is static, offline and deterministic: no network, no
resolver, no subprocess — same bytes in, same values out, in a stable order.
"""

from __future__ import annotations

from fleet.manifests.base import (
    ManifestAdapter,
    ManifestParseError,
    adapter_for,
    adapters,
    discover,
    register,
    reset_adapters,
)

__all__ = [
    "ManifestAdapter",
    "ManifestParseError",
    "adapter_for",
    "adapters",
    "discover",
    "register",
    "reset_adapters",
]
