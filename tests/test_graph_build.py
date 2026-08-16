"""Behaviour tests for `src/fleet/graph/{infer,build,query}.py` — the graph foundation (§3.1).

**Read this before touching an assertion in this file.**

Every ordering decision the harness makes rests on one sentence of §3.1 step 5: an `edges` row
is directed **dependent → dependency**, `src` requires `dst`. A mirror-image implementation is
not "a different convention". It is a fleet migrated in exactly inverted order — leaves first,
the shared library that gates 187 repos last — and it passes every stated success criterion
while doing so, because a reversed DAG is still a DAG, still acyclic, still layered, still
covers all 250 repos. Nothing downstream raises. The build simply fails 187 times.

The tests below are therefore written so that a reversed graph *cannot* be green. They assert
direction directly, and they assert asymmetric facts (`blast_radius(C) == 2` **and**
`blast_radius(A) == 0`) that swap values under a reversal rather than staying true. If one of
them fails, the correct response is to fix the graph, never to flip the expectation.

The fixture is one hand-built chain — **A requires B, B requires C** — plus a contract node,
because a three-node chain is the smallest graph in which "depends on" and "is depended on by"
have different answers, and the contract is the one node kind whose orientation the SPEC calls
out separately (§3.1 5b vii: a sink in `G`, a source in `G_rev`).
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

import fleet
from fleet.graph.build import (
    FleetGraph,
    GraphError,
    build_graph,
    orders_migration,
    validate_dag_edge_kinds,
)
from fleet.graph.infer import (
    EDGE_BASE_CONFIDENCE,
    InferenceInput,
    ManifestDependency,
    OwnerIndex,
    edge_key_for,
    infer_edges,
)
from fleet.graph.query import ancestors, blast_radii, blast_radius, descendants
from fleet.models.enums import ContractKind, Ecosystem, EdgeKind, NodeKind, SymbolKind
from fleet.models.graph import DAG_EDGE_KINDS, ContractNode, DependencyEdge, GraphNode, SymbolRef
from fleet.models.repo import Coordinate, ManifestRef, RawDependency
from fleet.settings import ConfigError, GraphSection

REPO_ROOT = Path(fleet.__file__).resolve().parents[2]
CONTRACT_ID = "proto:acme.identity.v1"

A = ("REPO", "acme-a")
B = ("REPO", "acme-b")
C = ("REPO", "acme-c")
D = ("REPO", "acme-d")
K = ("CONTRACT", CONTRACT_ID)


# =======================================================================================
# fixture: the A → B → C chain, and the fuller scenario the determinism probe re-runs
# =======================================================================================


def coord(name: str, *, version: str | None = None) -> Coordinate:
    return Coordinate(
        ecosystem=Ecosystem.MAVEN, group="com.acme", name=name, version_spec=version
    )


def manifest(repo_id: str) -> ManifestRef:
    return ManifestRef(
        repo_id=repo_id,
        path="pom.xml",
        ecosystem=Ecosystem.MAVEN,
        adapter="maven",
        adapter_version=1,
        sha256=hashlib.sha256(repo_id.encode()).hexdigest(),
    )


def dependency(*, repo_id: str, on: str, version: str | None = "^1.0", line: int = 12,
               scope: str | None = None) -> ManifestDependency:
    return ManifestDependency(
        manifest=manifest(repo_id),
        raw=RawDependency(raw_id=f"com.acme:{on}", version_spec=version, scope=scope,
                          source_line=line),
        coordinate=coord(on, version=version),
    )


def owner_index() -> OwnerIndex:
    """`acme-d` publishes nothing on purpose: §3.1 step 2's unknown-ecosystem repo must still be
    addressable as an edge destination."""
    return OwnerIndex.from_published(
        [("acme-a", coord("a")), ("acme-b", coord("b")), ("acme-c", coord("c"))]
    )


def chain_input() -> InferenceInput:
    """A requires B; B requires C. Nothing else."""
    return InferenceInput(
        owners=owner_index(),
        dependencies=[
            dependency(repo_id="acme-a", on="b"),
            dependency(repo_id="acme-b", on="c", line=20),
        ],
    )


def contract_node() -> ContractNode:
    return ContractNode(
        contract_id=CONTRACT_ID,
        kind=ContractKind.PROTO,
        identifier="acme.identity.v1",
        owning_repo_id="acme-b",
        source_paths=[{"repo_id": "acme-b", "path": "idl/identity.proto", "blob_sha": "a" * 40}],
        generated_paths=[{"repo_id": "acme-a", "path": "gen/identity_pb2.py"}],
        consumer_repo_ids=["acme-a"],
    )


def full_input() -> InferenceInput:
    """The chain, a contract, and one symbol of every symbol-derived kind."""
    symbols = [
        SymbolRef(repo_id="acme-d", fqn="com.acme.c.Util", kind=SymbolKind.IMPORT,
                  path="src/main/java/D.java", line=7, language="java", is_definition=False),
        SymbolRef(repo_id="acme-c", fqn="acme.identity.v1.IdentityService",
                  kind=SymbolKind.GRPC_SERVICE, path="idl/identity.proto", line=12,
                  language="proto", is_definition=True),
        SymbolRef(repo_id="acme-d", fqn="acme.identity.v1.IdentityService",
                  kind=SymbolKind.GRPC_SERVICE, path="src/main/java/Client.java", line=30,
                  language="java", is_definition=False),
        SymbolRef(repo_id="acme-a", fqn="users", kind=SymbolKind.DB_TABLE,
                  path="db/001_init.sql", line=3, language="sql", is_definition=True),
        SymbolRef(repo_id="acme-d", fqn="users", kind=SymbolKind.DB_TABLE,
                  path="sql/read.sql", line=9, language="sql", is_definition=False),
        SymbolRef(repo_id="acme-d", fqn="com.acme.b.Bootstrap", kind=SymbolKind.DYNAMIC_REF,
                  path="src/main/java/Boot.java", line=44, language="java", is_definition=False),
    ]
    base = chain_input()
    return InferenceInput(
        owners=base.owners,
        dependencies=base.dependencies,
        symbols=symbols,
        contracts=[contract_node()],
    )


def nodes(*repo_ids: str, contracts: tuple[str, ...] = ()) -> list[GraphNode]:
    return [
        *(GraphNode(kind=NodeKind.REPO, node_id=r) for r in repo_ids),
        *(GraphNode(kind=NodeKind.CONTRACT, node_id=c) for c in contracts),
    ]


def chain_graph() -> FleetGraph:
    return build_graph(nodes("acme-a", "acme-b", "acme-c"), infer_edges(chain_input()))


def full_graph() -> FleetGraph:
    return build_graph(
        nodes("acme-a", "acme-b", "acme-c", "acme-d", contracts=(CONTRACT_ID,)),
        infer_edges(full_input()),
    )


def edge_fingerprint() -> str:
    """The inference output as one stable string: order included, timestamps excluded.

    Re-run in a subprocess under a different `PYTHONHASHSEED`; see the determinism test.
    """
    lines = [
        "|".join(
            [
                e.edge_key, e.src_kind.value, e.src_id, e.dst_kind.value, e.dst_id or "",
                e.kind.value, e.evidence_path, str(e.evidence_line), f"{e.confidence:.12f}",
            ]
        )
        for e in infer_edges(full_input())
    ]
    return "\n".join(lines)


# =======================================================================================
# (1) orientation — the single most load-bearing fact in the module
# =======================================================================================


def test_an_edge_runs_from_the_dependent_to_the_dependency() -> None:
    """A requires B ⇒ the arc is A → B in `G`, and B → A in `G_rev`. Never the other way.

    §3.1 step 5: "Every `edges` row is directed dependent → dependency: `src` requires `dst` …
    An implementation that orients `edges` the other way is wrong, not merely different."

    The two `not in` assertions are the point of the test: a reversed build would satisfy the
    `in` half by symmetry (the reversed pair exists in the reversed graph), so only asserting
    that the *wrong* arcs are absent pins the direction.
    """
    g = chain_graph()

    assert (A, B) in g.G.edges  # acme-a requires acme-b
    assert (B, C) in g.G.edges  # acme-b requires acme-c
    assert (B, A) not in g.G.edges
    assert (C, B) not in g.G.edges

    assert (B, A) in g.G_rev.edges
    assert (C, B) in g.G_rev.edges
    assert (A, B) not in g.G_rev.edges
    assert (B, C) not in g.G_rev.edges

    # A dependency is a SUCCESSOR in G and a PREDECESSOR in G_rev.
    assert sorted(g.G.successors(A)) == [B]
    assert sorted(g.G_rev.predecessors(A)) == [B]
    assert sorted(g.G.predecessors(A)) == []


def test_a_reversed_graph_cannot_produce_these_blast_radii() -> None:
    """In A → B → C, `blast_radius(C) == 2` and `blast_radius(A) == 0`. Never the reverse.

    §3.5 defines `blast_radius(r) = |descendants(G_order, r)|` over "the ordering subgraph of
    `G_rev`, not of `G`", and says why: it must count the repos that transitively *depend on* r.
    C is the leaf dependency — the `acme-commons` of this fixture — so it gates both A and B. A
    depends on everything and gates nothing.

    This is the asymmetric assertion that a reversed implementation cannot satisfy: reversing
    swaps the two numbers (C→0, A→2), it does not leave them true. Computing blast radius on `G`
    would "silently rank every repo by the count of things it consumes" (§3.5), which is why
    `blast_radius` takes a `FleetGraph` and picks `G_order` itself. Do not "fix" a failure here
    by exchanging the expected values.
    """
    g = chain_graph()

    assert blast_radius(g, C) == 2  # both acme-a and acme-b are downstream of acme-c
    assert blast_radius(g, B) == 1
    assert blast_radius(g, A) == 0  # nothing depends on the leaf consumer

    assert descendants(g.G_order, C) == (A, B)
    assert descendants(g.G, A) == (B, C)  # what A consumes — the other question entirely
    assert ancestors(g.G, C) == (A, B)  # dependents are ANCESTORS in the normative orientation
    assert blast_radii(g) == {A: 0, B: 1, C: 2}


def test_blast_radius_is_ancestors_in_g_and_descendants_in_g_rev() -> None:
    """The two phrasings must agree, or `G_rev` is not the reverse of `G`.

    §3.1 step 6c ranks break candidates by `len(descendants(G_rev, dst))` while §3.5 phrases the
    same quantity as ancestors-in-`G`. If those ever disagree, one of the two call sites is
    reading a graph built with the wrong orientation.
    """
    g = chain_graph()
    for ref in g.node_refs():
        assert len(descendants(g.G_rev, ref)) == len(ancestors(g.G, ref))


# =======================================================================================
# (2) contract nodes — sink in G, source in G_rev
# =======================================================================================


def test_a_contract_node_is_a_sink_in_g_and_a_source_in_g_rev() -> None:
    """§3.1 5b (vii): "no `edges` row has `src_kind='CONTRACT'` and `dst_kind='REPO'` … a
    contract is therefore a sink in `G` and a source in `G_rev`."

    That is the entire hoisting mechanism: a node with no outbound dependency can always be
    scheduled first, so extracting a shared interface severs any cycle that ran through it. A
    contract with an outbound arc into a repo would re-create the cycle the hoist was meant to
    dissolve — silently, since the graph stays acyclic in most shapes.
    """
    g = full_graph()

    assert g.G.out_degree(K) == 0, "a contract depends on no repo — it is a sink in G"
    assert g.G.in_degree(K) > 0, "its owner and its consumers depend on it"
    assert g.G_rev.in_degree(K) == 0, "and therefore a source in G_rev"
    assert g.G_rev.out_degree(K) == g.G.in_degree(K)

    # The owner (CONTRACT_IMPL) and the consumer (CONTRACT_CONSUME) both point AT the contract.
    assert (B, K) in g.G.edges
    assert (A, K) in g.G.edges
    kinds = {kind for _u, _v, kind in _kinds_of(g, K)}
    assert kinds == {EdgeKind.CONTRACT_IMPL.value, EdgeKind.CONTRACT_CONSUME.value}

    # A source in G_rev is reachable from nothing, so nothing can order it later than wave 0.
    assert ancestors(g.G_rev, K) == ()


def _kinds_of(g: FleetGraph, node: tuple[str, str]) -> list[tuple[object, object, str]]:
    return [(u, v, k) for u, v in g.G.in_edges(node) for k in g.G[u][v]["kinds"]]


def test_a_contract_edge_carries_its_base_confidence_unpenalized() -> None:
    """Generated code is *evidence of consumption* (§3.1 5b ii) and byte-identical vendored
    copies collapse "with no penalty" (5b iii) — so the `generated`/`vendored` modifiers must not
    be charged against CONTRACT_CONSUME. `gen/identity_pb2.py` matches `scan.generated_globs`;
    at 0.85 × 0.6 = 0.51 the edge survives `min_confidence` today and would silently stop
    ordering the moment any second modifier applied.
    """
    consume = [e for e in infer_edges(full_input()) if e.kind is EdgeKind.CONTRACT_CONSUME]
    assert [e.evidence_path for e in consume] == ["gen/identity_pb2.py"]
    assert consume[0].confidence == EDGE_BASE_CONFIDENCE[EdgeKind.CONTRACT_CONSUME] == 0.85
    assert consume[0].confidence_factors == {}


# =======================================================================================
# (3) G_rev is built once, and agrees with G
# =======================================================================================


def test_g_rev_is_built_once_and_stays_consistent_with_g() -> None:
    """§3.1 step 5: `G_rev = G.reverse()` is built "once per run".

    Two failure modes this rules out. (a) Recomputing it per call — every `descendants(G_rev, …)`
    in step 6c would then walk a graph rebuilt from an `edges` table that 6d is concurrently
    suppressing rows in, so two calls in one cycle-break disagree. (b) Handing back a live
    networkx *view* (`reverse(copy=False)`), which drifts with `G` mid-algorithm. Identity plus
    a full edge-by-edge mirror check pins both.
    """
    g = full_graph()

    assert g.G_rev is g.G_rev
    assert g.G_order is g.G_order
    assert {(v, u) for u, v in g.G.edges} == set(g.G_rev.edges)
    assert {(v, u) for u, v in g.G_dag.edges} == set(g.G_order.edges)
    assert set(g.G.nodes) == set(g.G_rev.nodes) == set(g.G_dag.nodes) == set(g.G_order.nodes)

    # A snapshot, not a view: mutating G after the fact must not silently edit G_rev.
    g.G.add_edge(C, A)
    assert (A, C) not in g.G_rev.edges


# =======================================================================================
# (4) dag_edge_kinds filtering
# =======================================================================================


def test_an_edge_kind_outside_dag_edge_kinds_does_not_constrain_ordering() -> None:
    """§3.1 step 5: SHARED_RESOURCE and DYNAMIC_REF are advisory and "excluded from the DAG by
    default … because shared infrastructure is not a build ordering constraint".

    Excluded from *ordering*, not from the record: the rows stay in `G` (and in `edges`) so
    `fleet status --format dot` can render them dashed and a reviewer can see them. The test
    asserts both halves, because dropping them entirely would lose the report and keeping them in
    `G_order` would make two repos that merely share a Kafka topic order against each other.
    """
    g = full_graph()
    advisory = [
        e for e in infer_edges(full_input())
        if e.kind in {EdgeKind.SHARED_RESOURCE, EdgeKind.DYNAMIC_REF}
    ]
    assert advisory, "the fixture must actually produce advisory edges"

    for edge in advisory:
        assert edge.edge_key in g.edges_by_key  # recorded
        assert edge.edge_key not in g.ordering_edge_keys  # but not ordering
        assert not orders_migration(edge)

    # acme-d shares the `users` table with acme-a and dynamically references acme-b, yet it
    # orders against neither: its only ordering arc is the INTERNAL_IMPORT/API_CONTRACT pair.
    assert (D, A) in g.G.edges
    assert (D, A) not in g.G_dag.edges
    assert (D, B) in g.G.edges
    assert (D, B) not in g.G_dag.edges
    assert (D, C) in g.G_dag.edges


def test_a_sub_min_confidence_edge_is_recorded_but_never_orders() -> None:
    """§3.1 step 5: below `graph.min_confidence` an edge is "persisted, counted and rendered …
    but it does not order migration". Ordering on weak evidence is how a false edge in a
    250-repo fleet turns into an unbreakable cycle."""
    weak = _repo_edge(kind=EdgeKind.INTERNAL_IMPORT, confidence=0.49)
    strong = _repo_edge(kind=EdgeKind.INTERNAL_IMPORT, confidence=0.5, path="src/Other.java")
    g = build_graph(nodes("acme-a", "acme-b"), [weak, strong])

    assert weak.edge_key in g.edges_by_key
    assert weak.edge_key not in g.ordering_edge_keys
    assert strong.edge_key in g.ordering_edge_keys


def test_a_suppressed_edge_keeps_its_evidence_but_loses_its_ordering() -> None:
    """§3.1 6d breaks a cycle by setting `ordering_suppressed`, never by deleting the row: the
    evidence has to survive into the PR body that tells a human what was severed."""
    broken = _repo_edge(kind=EdgeKind.DECLARED_DEP, confidence=1.0, suppressed=True)
    g = build_graph(nodes("acme-a", "acme-b"), [broken])

    assert g.G.edges  # still in the evidence graph
    assert not g.G_dag.edges  # gone from the ordering graph
    assert blast_radius(g, B) == 0


def test_the_two_contract_kinds_are_mandatory_members_of_dag_edge_kinds() -> None:
    """§3.1 step 5: "CONTRACT_IMPL and CONTRACT_CONSUME are **mandatory** members of that list:
    startup fails with a config error if `graph.hoist_contracts` is true and either is absent,
    because dropping them deletes every ordering constraint hoisting created."

    A hoist that creates no ordering constraint is worse than no hoist: the contract still moves
    out of its owner, but nothing then orders the owner or the consumers behind it.
    """
    assert EdgeKind.CONTRACT_IMPL in DAG_EDGE_KINDS
    assert EdgeKind.CONTRACT_CONSUME in DAG_EDGE_KINDS
    # The §3.1 default membership, and the shipped `fleet.yaml` default, are the same six kinds.
    assert set(GraphSection().dag_edge_kinds) == set(DAG_EDGE_KINDS) == {
        EdgeKind.DECLARED_DEP, EdgeKind.PUBLISHED_ARTIFACT, EdgeKind.INTERNAL_IMPORT,
        EdgeKind.API_CONTRACT, EdgeKind.CONTRACT_IMPL, EdgeKind.CONTRACT_CONSUME,
    }

    for missing in (EdgeKind.CONTRACT_IMPL, EdgeKind.CONTRACT_CONSUME):
        with pytest.raises(ConfigError, match=missing.value):
            validate_dag_edge_kinds(DAG_EDGE_KINDS - {missing}, hoist_contracts=True)
    # With hoisting off, the pre-ADR-0019 ladder is recovered exactly and the set may narrow.
    assert validate_dag_edge_kinds(
        DAG_EDGE_KINDS - CONTRACT_KINDS, hoist_contracts=False
    ) == DAG_EDGE_KINDS - CONTRACT_KINDS


CONTRACT_KINDS = frozenset({EdgeKind.CONTRACT_IMPL, EdgeKind.CONTRACT_CONSUME})


def test_a_widened_dag_edge_kinds_actually_widens_the_ordering_graph() -> None:
    """`graph.dag_edge_kinds` is config (ADR-0018), so it must be able to *add* a kind, not only
    intersect with the module default — otherwise the key is decorative."""
    g = build_graph(
        nodes("acme-a", "acme-b", "acme-c", "acme-d", contracts=(CONTRACT_ID,)),
        infer_edges(full_input()),
        dag_edge_kinds=(*DAG_EDGE_KINDS, EdgeKind.SHARED_RESOURCE),
    )
    assert (D, A) in g.G_dag.edges


# =======================================================================================
# (5) determinism
# =======================================================================================


def test_inference_is_deterministic_within_one_process() -> None:
    """Same inputs → same edges, same order, same keys. §11.6 makes run equivalence a hash over
    the ordering edge set, so any set-iteration order leaking into the output turns two
    byte-identical runs into a spurious drift report."""
    first = infer_edges(full_input())
    second = infer_edges(full_input())

    assert [e.edge_key for e in first] == [e.edge_key for e in second]
    assert [(e.src_id, e.dst_id, e.kind) for e in first] == [
        (e.src_id, e.dst_id, e.kind) for e in second
    ]
    assert len({e.edge_key for e in first}) == len(first), "edge_key must be unique per row"


@pytest.mark.parametrize("seed", ["0", "1", "12345"])
def test_inference_is_deterministic_across_pythonhashseed(seed: str) -> None:
    """A real subprocess, because `PYTHONHASHSEED` is fixed at interpreter start.

    `hash()` of a str is randomized per process, and so is the iteration order of any set built
    from strings. An inference that lets either reach its output emits the same edges in a
    different order — which changes `run_digest`, changes the deterministic DFS of §3.1 6b, and
    therefore changes which edge is chosen to break a cycle. The failure is invisible in-process.
    """
    env = {
        **os.environ,
        "PYTHONHASHSEED": seed,
        "PYTHONPATH": os.pathsep.join([str(REPO_ROOT / "src"), str(REPO_ROOT)]),
    }
    probe = "from tests.test_graph_build import edge_fingerprint; print(edge_fingerprint())"
    out = subprocess.run(  # noqa: S603
        [sys.executable, "-c", probe], env=env, capture_output=True, text=True, check=True,
        cwd=str(REPO_ROOT),
    )
    assert out.stdout.strip() == edge_fingerprint().strip()


def test_edge_key_is_the_documented_recipe_and_nothing_else() -> None:
    """`edge_key` is derived exactly as `DependencyEdge.edge_key` documents it — sha256 over
    (src_kind, src_id, dst_kind, dst_coordinate.key or dst_id, kind, evidence_path,
    evidence_line), NUL-joined. A second, subtly different recipe anywhere in the codebase means
    a cycle-break decision recorded under one key can never be found under the other.
    """
    expected = hashlib.sha256(
        "\x00".join(["REPO", "acme-a", "REPO", "maven:com.acme:b", "DECLARED_DEP", "pom.xml", "12"])
        .encode()
    ).hexdigest()
    edge = next(
        e for e in infer_edges(chain_input()) if e.src_id == "acme-a" and e.dst_id == "acme-b"
    )
    assert edge.edge_key == expected
    assert edge_key_for(
        src_kind=NodeKind.REPO, src_id="acme-a", dst_kind=NodeKind.REPO,
        dst_ref="maven:com.acme:b", kind=EdgeKind.DECLARED_DEP, evidence_path="pom.xml",
        evidence_line=12,
    ) == expected
    # A missing line hashes as -1, matching `edges.evidence_line INTEGER NOT NULL DEFAULT -1`.
    assert edge_key_for(
        src_kind=NodeKind.REPO, src_id="a", dst_kind=NodeKind.REPO, dst_ref="k",
        kind=EdgeKind.DECLARED_DEP, evidence_path="p", evidence_line=None,
    ) == edge_key_for(
        src_kind=NodeKind.REPO, src_id="a", dst_kind=NodeKind.REPO, dst_ref="k",
        kind=EdgeKind.DECLARED_DEP, evidence_path="p", evidence_line=-1,
    )


# =======================================================================================
# (6) evidence and confidence
# =======================================================================================


def test_every_inferred_edge_carries_the_evidence_it_was_derived_from() -> None:
    """An edge with no evidence is a defect, not a weak edge.

    `evidence_path` is what a reviewer opens when a `WeakEdge` finding lands on a PR body, what
    §3.1 5b (viii) matches to decide whether a row retargets to a contract, and what makes
    `edge_key` a *semantic* key. An inference rule that forgot to thread it through would produce
    edges that all collide onto one key per (src, dst, kind) triple.
    """
    edges = infer_edges(full_input())
    assert edges

    for edge in edges:
        assert edge.evidence_path, f"{edge.kind} edge with no evidence path"
        assert not edge.evidence_path.startswith("/"), "evidence is repo-relative"

    by_kind = {e.kind: e for e in edges}
    declared = next(e for e in edges if e.kind is EdgeKind.DECLARED_DEP and e.src_id == "acme-a")
    assert declared.evidence_path == "pom.xml"
    assert declared.evidence_line == 12
    # Symbol-derived edges point at the exact line the parser saw.
    assert by_kind[EdgeKind.INTERNAL_IMPORT].evidence_line == 7
    assert by_kind[EdgeKind.API_CONTRACT].evidence_line == 30
    assert by_kind[EdgeKind.DYNAMIC_REF].evidence_line == 44
    assert by_kind[EdgeKind.SHARED_RESOURCE].evidence_line in {3, 9}


def test_confidence_is_reconstructible_from_confidence_factors_alone() -> None:
    """§3.1 step 5: "every applied modifier stored verbatim in `edges.confidence_factors` … so
    any score is reconstructible from the row". A score that cannot be recomputed cannot be
    audited, and the whole point of scoring is that a human can see what the harness declined to
    order on and why."""
    inp = InferenceInput(
        owners=owner_index(),
        dependencies=[dependency(repo_id="acme-a", on="b", version="^1.2", scope="test")],
    )
    edge = infer_edges(inp)[0]

    assert edge.kind is EdgeKind.DECLARED_DEP  # an open range is not a published artifact
    assert edge.confidence_factors == {"open_range": 0.9, "test_scope": 0.7}
    product = edge.base_confidence
    for name in sorted(edge.confidence_factors):
        product *= edge.confidence_factors[name]
    assert edge.confidence == product

    pinned = infer_edges(
        InferenceInput(owners=owner_index(),
                       dependencies=[dependency(repo_id="acme-a", on="b", version="1.4.0")])
    )[0]
    assert pinned.kind is EdgeKind.PUBLISHED_ARTIFACT
    assert pinned.confidence == EDGE_BASE_CONFIDENCE[EdgeKind.PUBLISHED_ARTIFACT]


def test_a_truncated_symbol_index_caps_confidence_and_says_so() -> None:
    """§3.1 step 4: a repo past `scan.max_symbols_per_repo` "continues with a partial index whose
    edges are capped at confidence 0.6 — a truncated index may miss edges, so it may not claim
    high confidence". The cap is stored as the factor that produces it, so the row still
    reconstructs by multiplication."""
    base = full_input()
    inp = InferenceInput(
        owners=base.owners, dependencies=base.dependencies, symbols=base.symbols,
        truncated_repo_ids=frozenset({"acme-d"}),
    )
    internal = next(e for e in infer_edges(inp) if e.kind is EdgeKind.INTERNAL_IMPORT)

    assert internal.confidence == pytest.approx(0.6)
    assert "truncated_index" in internal.confidence_factors
    # A manifest join is not read out of the symbol index, so it is untouched by the cap.
    declared = next(e for e in infer_edges(inp) if e.kind is EdgeKind.DECLARED_DEP)
    assert declared.src_id == "acme-a"
    assert "truncated_index" not in declared.confidence_factors


def test_an_undeclared_import_is_an_edge_but_a_declared_one_is_not_doubled() -> None:
    """INTERNAL_IMPORT is "exactly the class of edge a single-repo scan cannot see"
    (§3.1 step 5, Constraint 4). It is defined as an import *with no corresponding manifest
    entry*, so a declared dependency must not also surface as an undeclared one — that would
    double-count the same fact at two different confidences."""
    edges = infer_edges(full_input())
    internal = [e for e in edges if e.kind is EdgeKind.INTERNAL_IMPORT]

    assert [(e.src_id, e.dst_id) for e in internal] == [("acme-d", "acme-c")]

    with_manifest = InferenceInput(
        owners=owner_index(),
        dependencies=[dependency(repo_id="acme-d", on="c")],
        symbols=[s for s in full_input().symbols if s.kind is SymbolKind.IMPORT],
    )
    assert not [e for e in infer_edges(with_manifest) if e.kind is EdgeKind.INTERNAL_IMPORT]


# =======================================================================================
# (7) graph assembly hygiene
# =======================================================================================


def test_an_edge_naming_an_unknown_node_fails_loud() -> None:
    """Rule 11 / §12.29: a dangling node id is a failure, never a silently invented node. An
    invented node would migrate in a wave of its own with no source behind it."""
    edge = _repo_edge(kind=EdgeKind.DECLARED_DEP, confidence=1.0)
    with pytest.raises(GraphError, match="acme-b"):
        build_graph(nodes("acme-a"), [edge])


def test_an_external_dependency_is_not_a_node() -> None:
    """`dst_id is None` with `dst_kind='REPO'` means the dependency is outside the fleet: it is
    persisted for the record and orders nothing (§5 `DependencyEdge.dst_id`)."""
    external = DependencyEdge(
        edge_key=edge_key_for(
            src_kind=NodeKind.REPO, src_id="acme-a", dst_kind=NodeKind.REPO,
            dst_ref="maven:org.slf4j:slf4j-api", kind=EdgeKind.DECLARED_DEP,
            evidence_path="pom.xml", evidence_line=3,
        ),
        src_id="acme-a",
        dst_coordinate=Coordinate(ecosystem=Ecosystem.MAVEN, group="org.slf4j",
                                  name="slf4j-api", version_spec="2.0.0"),
        kind=EdgeKind.DECLARED_DEP, base_confidence=1.0, confidence=1.0,
        evidence_path="pom.xml", evidence_line=3,
    )
    g = build_graph(nodes("acme-a"), [external])

    assert g.node_refs() == (A,)
    assert not g.G.edges
    assert external.edge_key not in g.edges_by_key


def test_parallel_evidence_collapses_to_one_arc_but_keeps_every_row() -> None:
    """`nx.DiGraph` holds one arc per node pair — ADR-0004 needs `nx.condensation` — so the
    several evidence rows behind one dependency are recovered through `edge_records`, not lost.
    §3.1 6c ranks *individual* edges by confidence, so it must still see all of them."""
    first = _repo_edge(kind=EdgeKind.DECLARED_DEP, confidence=1.0, path="pom.xml")
    second = _repo_edge(kind=EdgeKind.INTERNAL_IMPORT, confidence=0.8, path="src/Main.java")
    g = build_graph(nodes("acme-a", "acme-b"), [first, second])

    assert len(g.G.edges) == 1
    assert {e.edge_key for e in g.edge_records(A, B)} == {first.edge_key, second.edge_key}
    assert g.G[A][B]["kinds"] == ("DECLARED_DEP", "INTERNAL_IMPORT")


def _repo_edge(
    *, kind: EdgeKind, confidence: float, path: str = "pom.xml", suppressed: bool = False
) -> DependencyEdge:
    """A hand-built acme-a → acme-b row, for the cases inference cannot reach on its own."""
    return DependencyEdge(
        edge_key=edge_key_for(
            src_kind=NodeKind.REPO, src_id="acme-a", dst_kind=NodeKind.REPO,
            dst_ref=coord("b").key, kind=kind, evidence_path=path, evidence_line=1,
        ),
        src_id="acme-a", dst_coordinate=coord("b"), dst_id="acme-b", kind=kind,
        base_confidence=EDGE_BASE_CONFIDENCE[kind], confidence=confidence,
        ordering_suppressed=suppressed, evidence_path=path, evidence_line=1,
    )
