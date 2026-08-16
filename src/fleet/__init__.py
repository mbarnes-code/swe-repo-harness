"""fleet — polyglot monorepo migration harness (SPEC §1).

Four phases over ~250 source repositories: **scan** (preflight, manifests, symbols, edges),
**transform** (relocation + rule-driven rewrites with a 3-rung escalation ladder), **build**
(git-filter-repo ingest, BUILD generation, sandboxed bazel) and **verify** (rdeps closure,
stacked PRs).

Only the model layer is re-exported here: importing `fleet` must never drag in Typer,
Anthropic, aiosqlite or structlog, so a subprocess or test that only needs the state
contracts stays cheap and import-safe.
"""

from __future__ import annotations

from fleet.models.base import FleetModel, utcnow
from fleet.models.enums import (
    Ecosystem,
    EdgeKind,
    FailureClass,
    Phase,
    RepoStatus,
    TransformTier,
)
from fleet.models.state import MigrationState, PhaseRecord, RepoState

__version__ = "0.1.0"

__all__ = [
    "Ecosystem",
    "EdgeKind",
    "FailureClass",
    "FleetModel",
    "MigrationState",
    "Phase",
    "PhaseRecord",
    "RepoState",
    "RepoStatus",
    "TransformTier",
    "__version__",
    "utcnow",
]
