"""Blast radius and reachability over the ordering subgraph (§3.5).

§3.5, verbatim, is the whole of this module:

    `blast_radius(r) = |descendants(G_order, r)|` … `G_order` is **the ordering subgraph of
    `G_rev`, not of `G`** (§3.1): `edges` rows are directed dependent → dependency,
    `G_rev = G.reverse()` therefore runs dependency → dependent, and `descendants(G_order, r)` is
    consequently the set of repos that transitively *depend on* `r` — which is what "blast
    radius" has to mean. Computing it on `G` would silently rank every repo by the count of
    things it consumes.

`blast_radius` therefore takes a `FleetGraph`, never a bare `DiGraph`: the orientation is then
not something a caller can get wrong. Every result is a sorted tuple rather than the `set`
networkx returns, because set iteration order must never reach a persisted decision (§11.6).
"""

from __future__ import annotations

import networkx as nx  # type: ignore[import-untyped]  # no py.typed; see HONEST_GAPS

from fleet.graph.build import FleetGraph, NodeRef

__all__ = ["ancestors", "blast_radii", "blast_radius", "descendants"]


def descendants(graph: nx.DiGraph, node: NodeRef) -> tuple[NodeRef, ...]:
    """Everything reachable *from* `node`, sorted, excluding `node` itself.

    On `G` (dependent → dependency) these are the things `node` transitively depends on; on
    `G_rev`/`G_order` they are the things that transitively depend on `node`. The direction is a
    property of the graph handed in — which is exactly why `blast_radius` does not take one.
    """
    found: set[NodeRef] = nx.descendants(graph, node)
    return tuple(sorted(found))


def ancestors(graph: nx.DiGraph, node: NodeRef) -> tuple[NodeRef, ...]:
    """Everything that reaches `node`, sorted, excluding `node` itself."""
    found: set[NodeRef] = nx.ancestors(graph, node)
    return tuple(sorted(found))


def blast_radius(graph: FleetGraph, node: NodeRef) -> int:
    """`|descendants(G_order, r)|` — the count of nodes that transitively depend on `node`.

    Persisted to `repos.blast_radius`, and read by three things that all get worse if it is
    inverted: the `fleet status` ranking, the scheduler's within-wave admission order, and the
    `budgets.repo_max_cost_usd` scaling of `1 + log2(1 + blast_radius)`.
    """
    return len(nx.descendants(graph.G_order, node))


def blast_radii(graph: FleetGraph) -> dict[NodeRef, int]:
    """`blast_radius` for every node, which is how §3.5 computes `repos.blast_radius` after
    Phase 1. Insertion-ordered by sorted `NodeRef`, so the write order is reproducible."""
    return {ref: blast_radius(graph, ref) for ref in graph.node_refs()}
