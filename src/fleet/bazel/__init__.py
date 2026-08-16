"""Bazel + bzlmod target layout, BUILD generation and rdeps queries (ADR-0007).

A pure **driver**, with no ecosystem exemption (§13 row 6): `layout.py`, `generators.py` and
`query.py` carry no per-ecosystem branch and name no language directory. Everything
language-shaped arrives as adapter data — `monorepo_dir`, `path_tail`, the rule name, the
`WorkspaceDep.extension` dialect — which is what makes re-pointing one adapter move every
`dest`, package and `//` label with no edit here.

Generation is pure (data in → text out) and needs no `bazel` binary; `query.py` is the only
module that shells out, and it does so through an injected `CommandRunner`.
"""

from __future__ import annotations

from fleet.bazel.generators import (
    GENERATED_HEADER,
    OverrideDecision,
    OverrideProposal,
    VersionConflict,
    VersionRange,
    VersionResolution,
    coarse_build_targets,
    mvs_select,
    parse_range,
    reconcile_versions,
    render_build_bazel,
    render_gazelle_build,
    render_module_bazel,
    render_target,
    resolve_workspace_deps,
    validate_override,
)
from fleet.bazel.layout import (
    RESERVED_SCC_SEGMENT,
    EcosystemRegistry,
    LayoutAdapter,
    LayoutNode,
    PublishedCoordinate,
    ReservedDestError,
    is_reserved_dest,
    layout,
    normalize_dest,
    scc_label,
    scc_package,
    select_primary_coordinate,
)
from fleet.bazel.lockfile import (
    MODULE_LOCK_PATH,
    LockfileRegistryMismatchError,
    check_lock_registry,
    lock_registry_urls,
)
from fleet.bazel.query import (
    DEFAULT_RDEPS_LIMIT,
    DEFAULT_SAMPLE_N,
    BazelQueryError,
    RdepsClosure,
    bazel_test_argv,
    kind_rule_query,
    parse_target_labels,
    query_argv,
    rdeps_closure,
    rdeps_query,
    registry_args,
    sample_seed_for,
    select_tested_targets,
)

__all__ = [
    "DEFAULT_RDEPS_LIMIT",
    "DEFAULT_SAMPLE_N",
    "GENERATED_HEADER",
    "MODULE_LOCK_PATH",
    "RESERVED_SCC_SEGMENT",
    "BazelQueryError",
    "EcosystemRegistry",
    "LayoutAdapter",
    "LayoutNode",
    "LockfileRegistryMismatchError",
    "OverrideDecision",
    "OverrideProposal",
    "PublishedCoordinate",
    "RdepsClosure",
    "ReservedDestError",
    "VersionConflict",
    "VersionRange",
    "VersionResolution",
    "bazel_test_argv",
    "check_lock_registry",
    "coarse_build_targets",
    "is_reserved_dest",
    "kind_rule_query",
    "layout",
    "lock_registry_urls",
    "mvs_select",
    "normalize_dest",
    "parse_range",
    "parse_target_labels",
    "query_argv",
    "rdeps_closure",
    "rdeps_query",
    "reconcile_versions",
    "registry_args",
    "render_build_bazel",
    "render_gazelle_build",
    "render_module_bazel",
    "render_target",
    "resolve_workspace_deps",
    "sample_seed_for",
    "scc_label",
    "scc_package",
    "select_primary_coordinate",
    "select_tested_targets",
    "validate_override",
]
