"""ADR-0023 backend adapters: one file per transport, each ending in `@register_backend`.

Deliberately empty of imports. `fleet.llm.client.discover()` walks this package with `pkgutil`
and imports each module individually, tolerating an `ImportError` from a backend whose SDK is not
installed. An eager re-export here would turn one missing optional SDK into a failure to import
the package at all, and every backend would vanish together.
"""

from __future__ import annotations
