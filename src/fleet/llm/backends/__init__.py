"""Backend adapters for the §7.7 `ModelBackend` registry (ADR-0023).

`client.discover()` does a `pkgutil.iter_modules` walk of THIS package and imports every module
it finds, so a new transport is one file plus one `@register_backend` line and nothing else
(SPEC §7.7, §12.42).

Deliberately empty of imports. The walk needs `__path__` and nothing more, and a vendor SDK
imported here would be imported by every run — including runs whose active profile names no
target this package can serve. Each adapter module imports its own SDK at ITS module scope, which
is what makes `discover()`'s contract true: "a backend whose SDK is not installed fails its import
here and is simply not registered."
"""

from __future__ import annotations
