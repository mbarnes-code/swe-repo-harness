"""Behaviour tests for `src/fleet/graph/cycles.py` — §3.1 step 6, the breaking ladder.

**Read this before relaxing an assertion in this file.** Every test below exists because a
specific, named bug was found in review, and each one is written so that the *previous*
implementation cannot be green:

* `test_saturating_trial_dissolves_the_multi_chord_cycle` is the headline. It is the exact shape
  a greedy one-contract-at-a-time loop with `if repos_freed(best) == 0: break` cannot dissolve —
  one owner publishing two contracts to one consumer — and it is the modal SCC at fleet scale.
  Under the old algorithm this SCC fell through to the atomic-wave fallback and 40 repos went out
  in one PR. Its companion, `test_neither_contract_alone_dissolves_the_multi_chord_cycle`, proves
  the premise: each contract on its own frees zero repos.
* `test_component_size_is_one_when_the_hoist_dissolved_the_scc` pins `len(S_k) == 1` on the
  **success** path. The old definition raised there, i.e. it blew up on precisely the trial that
  worked.
* `test_scc_id_is_stable_across_processes` runs a real subprocess under three `PYTHONHASHSEED`s.
  An int "derived from `min(member repo_ids)`" was either `hash()` — randomized per process — or
  undefined, so a prior run's `scc_id` dangled and an in-flight `ATOMIC_WAVE` PR was orphaned.
* `test_atomic_wave_emits_one_target_for_the_whole_scc` pins the coarsening. One target per
  member reproduces the cycle in the Bazel target graph; `bazel build` then fails with a
  dependency-cycle error and the SCC's whole dependent cone is stranded.
"""

from __future__ import annotations

import os
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

import pytest

import fleet
from fleet.graph.build import build_graph
from fleet.graph.cycles import (
    KIND_RANK,
    CycleReport,
    MemberSources,
    SccResolution,
    _component_size,
    _live_component,
    break_cycles,
    classify_intra_scc_edges,
    coarsen_atomic_scc,
    scc_dest,
    scc_id_for,
    supersede_findings,
)
from fleet.models.enums import (
    BreakStrategy,
    ContractKind,
    ContractStatus,
    Ecosystem,
    EdgeKind,
    NodeKind,
)
from fleet.models.graph import ContractNode, DependencyEdge, GraphNode, edge_key_for
from fleet.models.repo import Coordinate
from fleet.settings import GraphSection

REPO_ROOT = Path(fleet.__file__).resolve().parents[2]

K1 = "proto:acme.k1"
K2 = "proto:acme.k2"


# =======================================================================================
# fixtures — hand-built, because the shapes under test are exactly the pathological ones
# =======================================================================================


def coord(repo_id: str) -> Coordinate:
    return Coordinate(ecosystem=Ecosystem.MAVEN, group="com.acme", name=repo_id)


def edge(
    src: str,
    dst: str,
    *,
    kind: EdgeKind = EdgeKind.DECLARED_DEP,
    confidence: float = 1.0,
    path: str = "pom.xml",
) -> DependencyEdge:
    """One repo→repo `edges` row: `src` requires `dst` (§3.1 step 5's fixed orientation)."""
    return DependencyEdge(
        edge_key=edge_key_for(
            src_kind=NodeKind.REPO.value,
            src_id=src,
            dst_kind=NodeKind.REPO.value,
            dst_ref=coord(dst).key,
            kind=kind.value,
            evidence_path=path,
            evidence_line=None,
        ),
        src_id=src,
        dst_coordinate=coord(dst),
        dst_id=dst,
        kind=kind,
        base_confidence=confidence,
        confidence=confidence,
        evidence_path=path,
    )


def nodes(*repo_ids: str) -> list[GraphNode]:
    return [GraphNode(kind=NodeKind.REPO, node_id=repo_id) for repo_id in repo_ids]


def contract(
    contract_id: str,
    *,
    owner: str,
    owner_path: str,
    consumers: dict[str, str],
    confidence: float = 0.9,
) -> ContractNode:
    """An `EXTRACTABLE` contract carried by its owner and vendored by each consumer.

    The consumers' copies are what make the retarget of §3.1 5b (viii) possible: an edge whose
    `evidence_path` is one of them is contract evidence, and is retargeted at the contract node
    rather than the owner when — and only when — the contract is actually hoisted.
    """
    return ContractNode(
        contract_id=contract_id,
        kind=ContractKind.PROTO,
        identifier=contract_id.split(":", 1)[1],
        owning_repo_id=owner,
        source_paths=[
            {"repo_id": owner, "path": owner_path, "blob_sha": "a" * 40},
            *(
                {"repo_id": repo_id, "path": path, "blob_sha": "a" * 40}
                for repo_id, path in sorted(consumers.items())
            ),
        ],
        consumer_repo_ids=sorted(consumers),
        extractable=True,
        extraction_confidence=confidence,
        hoist_target_path=f"contracts/{contract_id.replace(':', '/')}",
        status=ContractStatus.EXTRACTABLE,
    )


def multi_chord_fleet() -> tuple[list[GraphNode], list[DependencyEdge], list[ContractNode]]:
    """The modal SCC: `a` and `b` are mutually dependent, and the `a → b` direction is carried by
    **two** separate contracts that `b` owns.

    Hoisting either contract alone retargets one of the two `a → b` rows and leaves the other, so
    the arc — and therefore the cycle — survives and `repos_freed` is 0 for each contract taken
    by itself. Hoisting both retargets both rows and the SCC is gone.
    """
    graph_nodes = nodes("acme-a", "acme-b")
    edges = [
        edge("acme-a", "acme-b", kind=EdgeKind.API_CONTRACT, confidence=0.7,
             path="vendor/k1.proto"),
        edge("acme-a", "acme-b", kind=EdgeKind.API_CONTRACT, confidence=0.7,
             path="vendor/k2.proto"),
        edge("acme-b", "acme-a", kind=EdgeKind.DECLARED_DEP, confidence=1.0, path="pom.xml"),
    ]
    contracts = [
        contract(
            K1, owner="acme-b", owner_path="proto/k1.proto",
            consumers={"acme-a": "vendor/k1.proto"},
        ),
        contract(
            K2, owner="acme-b", owner_path="proto/k2.proto",
            consumers={"acme-a": "vendor/k2.proto"},
        ),
    ]
    return graph_nodes, edges, contracts


def implementation_cycle() -> CycleReport:
    """Three repos in a `DECLARED_DEP` cycle at confidence 1.0 and no contract anywhere.

    §3.1 6e's third trigger: every feedback edge is a `DECLARED_DEP` at `confidence >= 0.95`, so
    there is no weak edge to yield — the cycle is real code, and pretending otherwise produces a
    build that cannot link.
    """
    graph = build_graph(
        nodes("acme-a", "acme-b", "acme-c", "acme-d"),
        [
            edge("acme-a", "acme-b"),
            edge("acme-b", "acme-c"),
            edge("acme-c", "acme-a"),
            edge("acme-d", "acme-a"),
        ],
    )
    return break_cycles(graph)


# =======================================================================================
# (1) 6c-H — the saturating trial
# =======================================================================================


def test_saturating_trial_dissolves_the_multi_chord_cycle() -> None:
    """THE headline. A cycle closed by two contracts from one owner to one consumer dissolves.

    §3.1 6c-H: "Materializing all of `C` at once decides *whether* hoisting can work; the greedy
    commit loop then decides *which* hoists to spend." A loop that trials one contract at a time
    and stops at `repos_freed(best) == 0` abandons exactly this shape — the very SCC hoisting
    exists to fix — and it lands in the atomic-wave fallback instead, bundling both repos into
    one PR for a cycle that was never real.
    """
    graph_nodes, edges, contracts = multi_chord_fleet()
    report = break_cycles(build_graph(graph_nodes, edges), contracts=contracts)

    assert len(report.resolutions) == 1
    res = report.resolutions[0]
    assert res.break_strategy is BreakStrategy.CONTRACT_HOIST
    assert sorted(res.hoisted_contract_ids) == [K1, K2], "both chords must be cut, not one"
    assert res.broken_edge_keys == (), "CONTRACT_HOIST means no whole-repo edge was broken"
    assert res.members == ("acme-a", "acme-b")

    assert _live_component(report.graph.G_dag, res.members) == (), "the SCC must be gone"
    assert [c.status for c in report.hoisted_contracts] == [ContractStatus.HOISTED] * 2


def test_neither_contract_alone_dissolves_the_multi_chord_cycle() -> None:
    """The premise of the headline test, asserted rather than assumed: `repos_freed(k) == 0`.

    Run the same fleet with only ONE contract available. The saturating trial then says hoisting
    cannot finish the job, nothing is committed, and control falls through to 6d/6e exactly as
    §3.1 6c-H's "When hoisting is not enough" requires. If this test ever goes green with
    `CONTRACT_HOIST`, the fixture stopped being a multi-chord cycle and the headline test above
    stopped testing anything.
    """
    graph_nodes, edges, contracts = multi_chord_fleet()
    for solo in contracts:
        report = break_cycles(build_graph(graph_nodes, edges), contracts=[solo])
        res = report.resolutions[0]
        assert res.break_strategy is not BreakStrategy.CONTRACT_HOIST
        assert res.hoisted_contract_ids == (), f"{solo.contract_id} frees no repo on its own"


def test_component_size_is_one_when_the_hoist_dissolved_the_scc() -> None:
    """`len(S_k) == 1` when no non-trivial component of `G_k` contains `k.owning_repo_id`.

    §3.1 6c-H defines it that way for one reason: the *success* path of a trial hoist is exactly
    the path on which the owner sits in no non-trivial component at all. An implementation that
    reaches for "the component containing the owner" and raises when there is none crashes on the
    only outcome hoisting is trying to produce.
    """
    graph_nodes, edges, contracts = multi_chord_fleet()
    report = break_cycles(build_graph(graph_nodes, edges), contracts=contracts)

    assert _component_size(report.graph.G_dag, ("REPO", "acme-b")) == 1
    assert _component_size(report.graph.G_dag, ("REPO", "nonexistent")) == 1


def test_hoisted_contract_is_a_sink_and_the_retarget_records_its_origin() -> None:
    """A hoisted contract is a sink in `G` (§3.1 5b vii) and every retargeted row keeps the
    pre-hoist owner in `retargeted_from_repo_id` — the column that makes an un-hoist exact
    rather than a re-inference (§3.1 6c-H rollback)."""
    graph_nodes, edges, contracts = multi_chord_fleet()
    report = break_cycles(build_graph(graph_nodes, edges), contracts=contracts)

    assert list(report.graph.G.successors(("CONTRACT", K1))) == []
    retargeted = [e for e in report.edges if e.retargeted_from_repo_id is not None]
    assert {e.retargeted_from_repo_id for e in retargeted} == {"acme-b"}
    assert {e.kind for e in retargeted} == {EdgeKind.CONTRACT_CONSUME}


def test_hoisting_is_skipped_when_disabled() -> None:
    """`--no-hoist-contracts` / `graph.hoist_contracts: false` "recovers the pre-ADR-0019 ladder
    exactly" (§3.1 6c-H): the same fleet then falls through to 6d/6e with no hoist."""
    graph_nodes, edges, contracts = multi_chord_fleet()
    report = break_cycles(
        build_graph(graph_nodes, edges),
        contracts=contracts,
        config=GraphSection(hoist_contracts=False),
    )
    assert report.resolutions[0].hoisted_contract_ids == ()
    assert report.hoisted_contracts == ()


# =======================================================================================
# (2) scc_id — content-derived, superseded, never renumbered
# =======================================================================================


def test_scc_id_is_content_derived_over_the_member_set() -> None:
    """`"scc:" + sha256("\\x00".join(sorted(members))).hexdigest()[:16]` (§5.3 `SccId`)."""
    expected = "scc:" + sha256(b"acme-a\x00acme-b").hexdigest()[:16]
    assert scc_id_for(["acme-b", "acme-a"]) == expected
    assert scc_id_for(["acme-a", "acme-b"]) == expected, "input order must not matter"
    assert scc_id_for(["acme-a", "acme-b", "acme-c"]) != expected, "a new member is a new SCC"


def scc_id_fingerprint() -> str:
    """Printed by the subprocess probe below."""
    return "|".join(
        scc_id_for(members)
        for members in (["acme-a", "acme-b"], ["z", "a", "m"], ["one"])
    )


@pytest.mark.parametrize("seed", ["0", "1", "12345"])
def test_scc_id_is_stable_across_processes(seed: str) -> None:
    """A real subprocess, because `PYTHONHASHSEED` is fixed at interpreter start.

    §3.1 6a's prose still says the id is "derived from `min(sorted(member repo_ids))`" as an int.
    An int derived from strings is either `hash()` — randomized per process — or undefined, and
    either way the id changes between runs. A changed id means the prior run's `CycleFinding`
    dangles and an in-flight `ATOMIC_WAVE` PR names an SCC that no longer exists.
    """
    env = {
        **os.environ,
        "PYTHONHASHSEED": seed,
        "PYTHONPATH": os.pathsep.join([str(REPO_ROOT / "src"), str(REPO_ROOT)]),
    }
    probe = "from tests.test_graph_cycles import scc_id_fingerprint; print(scc_id_fingerprint())"
    out = subprocess.run(  # noqa: S603
        [sys.executable, "-c", probe], env=env, capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == scc_id_fingerprint()


def test_membership_change_supersedes_rather_than_renumbers() -> None:
    """A new member yields a NEW `scc_id`, and the old finding is marked `superseded_by` it.

    §5.3: the finding is superseded "rather than mutated in place, so the decision an already-open
    `ATOMIC_WAVE` PR was opened under stays readable". Renumbering in place is what orphaned the
    in-flight PR.
    """
    before = SccResolution(
        scc_id=scc_id_for(["acme-a", "acme-b"]),
        members=("acme-a", "acme-b"),
        edge_keys=(),
        feedback_edge_keys=(),
        proposed_break_edge_key=None,
        hoisted_contract_ids=(K1,),
        break_strategy=BreakStrategy.CONTRACT_HOIST,
    )
    after = SccResolution(
        scc_id=scc_id_for(["acme-a", "acme-b", "acme-c"]),
        members=("acme-a", "acme-b", "acme-c"),
        edge_keys=(),
        feedback_edge_keys=(),
        proposed_break_edge_key=None,
        hoisted_contract_ids=(K1,),
        break_strategy=BreakStrategy.CONTRACT_HOIST,
    )
    assert before.scc_id != after.scc_id

    updated = supersede_findings([before.to_finding()], [after])
    assert updated[0].scc_id == before.scc_id, "the old finding is never renumbered"
    assert updated[0].superseded_by == after.scc_id

    unchanged = supersede_findings([after.to_finding()], [after])
    assert unchanged[0].superseded_by is None, "a live SCC is not superseded by itself"


# =======================================================================================
# (3) 6b / 6c — deterministic feedback set and break_cost
# =======================================================================================


def test_back_edges_are_the_feedback_set_and_the_dfs_is_ordered() -> None:
    """§3.1 6b: DFS from the lexicographically smallest member, children in sorted order.

    An unordered DFS yields a different (still valid) feedback set every run, which makes wave
    assignment non-reproducible — so the classification is asserted node-by-node, not by count.
    """
    graph = build_graph(
        nodes("acme-a", "acme-b", "acme-c"),
        [edge("acme-a", "acme-b"), edge("acme-b", "acme-c"), edge("acme-c", "acme-a")],
    )
    members = [("REPO", "acme-a"), ("REPO", "acme-b"), ("REPO", "acme-c")]
    classes = classify_intra_scc_edges(graph.G_dag, members)

    assert classes[(("REPO", "acme-a"), ("REPO", "acme-b"))] == "TREE"
    assert classes[(("REPO", "acme-b"), ("REPO", "acme-c"))] == "TREE"
    assert classes[(("REPO", "acme-c"), ("REPO", "acme-a"))] == "BACK"
    assert classes == classify_intra_scc_edges(graph.G_dag, reversed(members))


def test_contract_edges_are_the_most_expensive_thing_to_break() -> None:
    """§3.1 6c: `CONTRACT_IMPL` (6) and `CONTRACT_CONSUME` (5) rank above `DECLARED_DEP` (4) and
    are therefore never chosen — breaking one would undo the hoist that produced it."""
    assert KIND_RANK[EdgeKind.DYNAMIC_REF] < KIND_RANK[EdgeKind.API_CONTRACT]
    assert KIND_RANK[EdgeKind.DECLARED_DEP] < KIND_RANK[EdgeKind.CONTRACT_CONSUME]
    assert KIND_RANK[EdgeKind.CONTRACT_CONSUME] < KIND_RANK[EdgeKind.CONTRACT_IMPL]


def test_the_weakest_edge_yields_first() -> None:
    """A low-confidence edge is the preferred break candidate: weak evidence is precisely what
    should yield when the graph must be made acyclic (§3.1 step 5, 6c)."""
    weak = edge("acme-b", "acme-a", kind=EdgeKind.INTERNAL_IMPORT, confidence=0.6, path="B.java")
    graph = build_graph(nodes("acme-a", "acme-b"), [edge("acme-a", "acme-b"), weak])
    report = break_cycles(graph)

    res = report.resolutions[0]
    assert res.break_strategy is BreakStrategy.EDGE_BREAK
    assert res.broken_edge_keys == (weak.edge_key,)
    suppressed = {e.edge_key for e in report.edges if e.ordering_suppressed}
    assert suppressed == {weak.edge_key}, "broken edges are suppressed, never deleted (§3.1 6d)"


# =======================================================================================
# (4) 6e — atomic wave and its coarsening
# =======================================================================================


def test_a_genuine_implementation_cycle_reaches_atomic_wave() -> None:
    """§3.1 6e: every feedback edge is a `DECLARED_DEP` at `confidence >= 0.95`, so there is no
    weak edge to yield and edge-breaking is abandoned."""
    report = implementation_cycle()
    res = report.resolutions[0]
    assert res.break_strategy is BreakStrategy.ATOMIC_WAVE
    assert res.members == ("acme-a", "acme-b", "acme-c")
    assert res.broken_edge_keys == (), "6e is reached without spending a break here"


def test_atomic_wave_emits_one_target_for_the_whole_scc() -> None:
    """§3.1 6e: **one** library target per `(ecosystem, scc_id)` at `//<scc_dest>:<scc_id>`.

    "A cyclic Bazel target graph is itself an error, and 6e is reached precisely because the
    members are mutually dependent, so per-member targets cannot be acyclic." One target per
    member reproduces the cycle in Bazel, the build fails with a dependency-cycle error, and every
    repo downstream of the SCC is stranded behind a build that can never go green.
    """
    report = implementation_cycle()
    res = report.resolutions[0]
    members = [
        MemberSources(repo_id=repo_id, ecosystem=Ecosystem.MAVEN, dest=f"java/{repo_id}",
                      srcs=("Main.java",))
        for repo_id in res.members
    ]
    plan = coarsen_atomic_scc(
        res, members, report.graph, monorepo_dirs={Ecosystem.MAVEN: "java"}
    )

    assert len(plan.targets) == 1, "one target per (ecosystem, scc_id), never one per member"
    target = plan.targets[0]
    assert target.member_repo_ids == res.members
    assert target.srcs == ("java/acme-a/Main.java", "java/acme-b/Main.java",
                           "java/acme-c/Main.java")
    assert target.label == f"//{scc_dest(Ecosystem.MAVEN, res.scc_id, monorepo_dir='java')}:" \
                           f"{target.name}"
    assert ":" not in target.name, "':' is a Bazel label separator, not a target-name character"
    assert plan.standalone_repo_ids == (), "every member here has an intra-SCC inbound edge"
    assert {f.kind for f in plan.findings} == {"CoarseTarget"}
    assert len(plan.findings) == 3, "one CoarseTarget finding per merged member"


def test_a_member_with_no_intra_scc_inbound_edge_keeps_its_own_target() -> None:
    """§3.1 6e: "A per-member target is emitted **only** for a member with no intra-SCC inbound
    edge" — nothing inside the SCC depends on it, so it cannot close the Bazel cycle."""
    graph = build_graph(
        nodes("acme-a", "acme-b", "acme-c"),
        [
            edge("acme-a", "acme-b"),
            edge("acme-b", "acme-a"),
            edge("acme-c", "acme-a"),  # c depends INTO the cycle, nothing depends on c
        ],
    )
    report = break_cycles(graph, config=GraphSection(scc_atomic_threshold=1))
    res = report.resolutions[0]
    assert res.break_strategy is BreakStrategy.ATOMIC_WAVE
    members = [
        MemberSources(repo_id=r, ecosystem=Ecosystem.MAVEN, dest=f"java/{r}", srcs=("M.java",))
        for r in ("acme-a", "acme-b")
    ]
    plan = coarsen_atomic_scc(
        SccResolution(
            scc_id=res.scc_id,
            members=("acme-a", "acme-b", "acme-c"),
            edge_keys=(),
            feedback_edge_keys=(),
            proposed_break_edge_key=None,
            break_strategy=BreakStrategy.ATOMIC_WAVE,
        ),
        members,
        report.graph,
        monorepo_dirs={Ecosystem.MAVEN: "java"},
    )
    assert plan.standalone_repo_ids == ("acme-c",)
    assert plan.merged_repo_ids == ("acme-a", "acme-b")


def test_beyond_scc_hard_max_the_harness_refuses() -> None:
    """§3.1 6e: past `graph.scc_hard_max` the strategy is `MANUAL` and the run continues on the
    rest of the fleet. "A 40-repo atomic wave is at the edge of reviewability; past it, refusing
    beats emitting an unmergeable artifact." """
    report = break_cycles(
        build_graph(
            nodes("acme-a", "acme-b", "acme-c"),
            [edge("acme-a", "acme-b"), edge("acme-b", "acme-c"), edge("acme-c", "acme-a")],
        ),
        config=GraphSection(scc_hard_max=2),
    )
    res = report.resolutions[0]
    assert res.break_strategy is BreakStrategy.MANUAL
    assert report.manual_repo_ids == ("acme-a", "acme-b", "acme-c")


# =======================================================================================
# (5) determinism
# =======================================================================================


def test_break_cycles_is_reproducible() -> None:
    """§11.6: two runs over the same edge set must produce byte-identical decisions, because
    `CycleFinding.broken_edge_keys` is a `run_digest` input."""
    graph_nodes, edges, contracts = multi_chord_fleet()
    first = break_cycles(build_graph(graph_nodes, edges), contracts=contracts)
    second = break_cycles(build_graph(graph_nodes, list(reversed(edges))), contracts=contracts)

    assert [r.scc_id for r in first.resolutions] == [r.scc_id for r in second.resolutions]
    assert first.resolutions[0].hoisted_contract_ids == second.resolutions[0].hoisted_contract_ids
    assert [e.edge_key for e in first.edges] == [e.edge_key for e in second.edges]


def test_an_acyclic_fleet_produces_no_findings() -> None:
    """The common case: no SCC, no `CycleFinding`, and the graph is handed on untouched."""
    report = break_cycles(
        build_graph(nodes("acme-a", "acme-b"), [edge("acme-a", "acme-b")])
    )
    assert report.resolutions == ()
    assert report.manual_repo_ids == ()
    assert isinstance(report, CycleReport)
