"""Structural search-and-rewrite engines. `ast-grep` is primary; libcst/ts-morph are
fenced secondaries (ADR-0006).

`pipeline.RewritePipeline` is the entry point every caller wants: it owns the buffer, the
`(priority, id)` order, the bounded fixpoint loop and conflict detection (§7.4). The engine
drivers are stateless and are reached through `rules.EngineRegistry`, never imported directly by
the orchestrator — an engine is data (`transform.engines`), not code.
"""

from __future__ import annotations

from fleet.rewrite.apply import (
    ApplyResult,
    apply_in_memory,
    apply_patch,
    check_diff,
    make_unified_diff,
    parse_unified_diff,
    validate_diff,
)
from fleet.rewrite.pipeline import (
    EngineContractError,
    RewriteFinding,
    RewriteOutcome,
    RewritePipeline,
)
from fleet.rewrite.rules import (
    EngineRegistry,
    EngineUnavailableError,
    ProbeIndeterminateError,
    Rewriter,
    RewriteRule,
    load_rules,
    rule_sort_key,
)

__all__ = [
    "ApplyResult",
    "EngineContractError",
    "EngineRegistry",
    "EngineUnavailableError",
    "ProbeIndeterminateError",
    "RewriteFinding",
    "RewriteOutcome",
    "RewritePipeline",
    "RewriteRule",
    "Rewriter",
    "apply_in_memory",
    "apply_patch",
    "check_diff",
    "load_rules",
    "make_unified_diff",
    "parse_unified_diff",
    "rule_sort_key",
    "validate_diff",
]
