"""Exception classification primitives shared across worker/orchestrator error reporting.
"""

from __future__ import annotations


def exception_type_name(exc: BaseException) -> str:
    """`"<module>.<qualname>"` for `exc`'s type — the one dotted-path spelling every
    `exception_type=` field in the harness records, so it is computed in exactly one place."""
    return f"{type(exc).__module__}.{type(exc).__qualname__}"
