"""`edges` rows → `networkx` graphs (ADR-0004). The graph is derived, never persisted.

**Edge orientation is fixed and global, and this module is where it is fixed.** §3.1 step 5:

    Every `edges` row is directed **dependent → dependency**: `src` requires `dst`.
    `graph/build.py` constructs `G` with exactly this orientation and builds
    `G_rev = G.reverse()` once per run; wave layering (step 7) and every `descendants()` call in
    step 6 operate on `G_rev`, never on `G`. An implementation that orients `edges` the other way
    is wrong, not merely different.

That is not a stylistic preference. A mirror-image graph passes every stated success criterion
while migrating the fleet in exactly inverted order — leaves first, shared libraries last — so
the orientation is asserted directly in `tests/test_graph_build.py` rather than left to review.
Two consequences follow mechanically and are what the rest of the harness reads:

* a **dependency** is a *successor* in `G` and a *predecessor* in `G_rev`;
* a **contract node is a sink in `G` and a source in `G_rev`** (§3.1 5b vii), which is what lets
  a hoisted contract always be scheduled first.

Four graphs, each built exactly once per `build_graph()` call and each named by the SPEC:

| name | orientation | edges | used by |
|---|---|---|---|
| `G` | dependent → dependency | every internal edge, all kinds | §3.1 step 6 evidence |
| `G_rev` | dependency → dependent | mirror of `G` | §3.1 step 6c `descendants` |
| `G_dag` | dependent → dependency | ordering edges only | SCC detection, condensation |
| `G_order` | dependency → dependent | ordering edges only | §3.5 blast radius, `blocked_by` |

§3.5 states `G_order` verbatim: "the ordering subgraph of `G_rev`, not of `G`". An edge orders
migration iff `kind ∈ graph.dag_edge_kinds`, `confidence >= graph.min_confidence` and
`ordering_suppressed = 0` — the predicate of `DependencyEdge.orders_migration`, with the
*configured* kind set substituted for the module default (ADR-0018 makes the set config-driven).
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import Final

import networkx as nx  # type: ignore[import-untyped]  # no py.typed; see HONEST_GAPS

from fleet.models.enums import CONTRACT_EDGE_KINDS, EdgeKind, NodeKind
from fleet.models.graph import DAG_EDGE_KINDS, DependencyEdge, GraphNode

__all__ = [
    "DEFAULT_MIN_CONFIDENCE",
    "FleetGraph",
    "GraphError",
    "NodeRef",
    "build_graph",
    "orders_migration",
    "validate_dag_edge_kinds",
]

type NodeRef = tuple[str, str]
"""The networkx node identity: `(NodeKind.value, node_id)` — exactly `GraphNode.key`. A tuple of
two strings is totally ordered, which is what makes layering and DFS reproducible."""

DEFAULT_MIN_CONFIDENCE: Final = 0.5  # `graph.min_confidence` (§9); the settings default


class GraphError(RuntimeError):
    """An edge names a node that is not in the fleet. Fail loud (Rule 11): §12.29 makes a
    dangling node id a test failure, never a silently dropped ordering constraint."""


def validate_dag_edge_kinds(
    kinds: Collection[EdgeKind], *, hoist_contracts: bool = True
) -> frozenset[EdgeKind]:
    """§3.1 step 5: `CONTRACT_IMPL` and `CONTRACT_CONSUME` are **mandatory** members of
    `graph.dag_edge_kinds` — startup fails with a config error if `graph.hoist_contracts` is true
    and either is absent, because dropping them deletes every ordering constraint hoisting
    created."""
    selected = frozenset(kinds)
    missing = sorted(k.value for k in CONTRACT_EDGE_KINDS - selected)
    if hoist_contracts and missing:
        from fleet.settings import ConfigError

        raise ConfigError(
            f"graph.dag_edge_kinds omits {', '.join(missing)} while graph.hoist_contracts is "
            "true; hoisting a contract would then create no ordering constraint at all",
            file="config/fleet.yaml",
            key="graph.dag_edge_kinds",
        )
    return selected


def orders_migration(
    edge: DependencyEdge,
    *,
    dag_edge_kinds: Collection[EdgeKind] = DAG_EDGE_KINDS,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> bool:
    """`DependencyEdge.orders_migration` with the *configured* kind set (§3.1 step 5, ADR-0018).

    The model's own method hardcodes the `DAG_EDGE_KINDS` default, so it cannot express a
    `fleet.yaml` that widens or narrows the set; this is the same predicate, parameterized.
    """
    return (
        edge.is_internal
        and edge.kind in dag_edge_kinds
        and not edge.ordering_suppressed
        and edge.confidence >= min_confidence
    )


@dataclass(frozen=True, slots=True)
class FleetGraph:
    """The four graphs of one run, each built once. Frozen: a caller cannot swap `G_rev` for
    something that disagrees with `G`, and `G_rev is G_rev` holds for the life of the run."""

    G: nx.DiGraph
    G_rev: nx.DiGraph
    G_dag: nx.DiGraph
    G_order: nx.DiGraph
    edges_by_key: Mapping[str, DependencyEdge]
    """Every internal edge by `edge_key` — the only portable edge reference (§5 `EdgeKey`)."""
    ordering_edge_keys: frozenset[str]
    """The subset that actually orders migration; `G_dag`/`G_order` carry exactly these."""

    def node_refs(self) -> tuple[NodeRef, ...]:
        """Every node, sorted. Identical in `G`, `G_rev`, `G_dag` and `G_order`."""
        return tuple(sorted(self.G.nodes))

    def edge_records(self, src: NodeRef, dst: NodeRef) -> tuple[DependencyEdge, ...]:
        """The `DependencyEdge` rows behind one `G` arc, in `edge_key` order. A `DiGraph` keeps
        one arc per node pair (ADR-0004 needs `nx.condensation`), so the several evidence rows
        that justify it are recovered here rather than lost."""
        keys: tuple[str, ...] = tuple(self.G[src][dst]["edge_keys"])
        return tuple(self.edges_by_key[key] for key in keys)


def build_graph(
    nodes: Iterable[GraphNode],
    edges: Iterable[DependencyEdge],
    *,
    dag_edge_kinds: Collection[EdgeKind] = DAG_EDGE_KINDS,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    hoist_contracts: bool = True,
) -> FleetGraph:
    """Assemble the run's graphs from nodes + edges in the normative orientation.

    `src` requires `dst`, so the arc runs `src → dst`. External edges (`dst_id is None`) are
    dropped: they address nothing inside the fleet and order nothing. An edge naming a node that
    was not supplied raises `GraphError` rather than inventing one — an invented node would
    migrate in a wave of its own with no source behind it.
    """
    kinds = validate_dag_edge_kinds(dag_edge_kinds, hoist_contracts=hoist_contracts)
    refs = sorted({_node_ref(node.kind, node.node_id) for node in nodes})

    graph: nx.DiGraph = nx.DiGraph()
    dag: nx.DiGraph = nx.DiGraph()
    graph.add_nodes_from(refs)
    dag.add_nodes_from(refs)

    known = set(refs)
    by_key: dict[str, DependencyEdge] = {}
    ordering: set[str] = set()
    for edge in sorted(edges, key=lambda e: e.edge_key):
        if edge.dst_id is None:
            continue  # external dependency: outside the fleet, orders nothing (§5)
        src = _node_ref(edge.src_kind, edge.src_id)
        dst = _node_ref(edge.dst_kind, edge.dst_id)
        for ref in (src, dst):
            if ref not in known:
                raise GraphError(
                    f"edge {edge.edge_key} names node {ref[0]}:{ref[1]}, which is not in the "
                    "fleet; §12.29 makes a dangling node id a failure, not a silent drop"
                )
        by_key[edge.edge_key] = edge
        _link(graph, src, dst, edge)
        if orders_migration(edge, dag_edge_kinds=kinds, min_confidence=min_confidence):
            ordering.add(edge.edge_key)
            _link(dag, src, dst, edge)

    return FleetGraph(
        G=graph,
        G_rev=graph.reverse(copy=True),
        G_dag=dag,
        G_order=dag.reverse(copy=True),
        edges_by_key=dict(sorted(by_key.items())),
        ordering_edge_keys=frozenset(ordering),
    )


def _node_ref(kind: NodeKind, node_id: str) -> NodeRef:
    return (kind.value, node_id)


def _link(graph: nx.DiGraph, src: NodeRef, dst: NodeRef, edge: DependencyEdge) -> None:
    """Add or extend the `src → dst` arc. Attributes are sorted tuples, never sets, so two runs
    that found the same evidence serialize it identically (§11.6)."""
    if graph.has_edge(src, dst):
        keys: tuple[str, ...] = tuple(sorted({*graph[src][dst]["edge_keys"], edge.edge_key}))
        kinds: tuple[str, ...] = tuple(sorted({*graph[src][dst]["kinds"], edge.kind.value}))
    else:
        keys, kinds = (edge.edge_key,), (edge.kind.value,)
    graph.add_edge(src, dst, edge_keys=keys, kinds=kinds)
