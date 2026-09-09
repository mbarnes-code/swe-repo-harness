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
from time import monotonic

import networkx as nx  # type: ignore[import-untyped]  # no py.typed; see HONEST_GAPS
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
from fleet.graph.sequence import assign_waves, condense_for_ordering, ordering_is_acyclic
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

# §12.19's "no hang" property, bounded by wall clock rather than by an iteration count: the SCC
# detection is Tarjan's algorithm (`nx.strongly_connected_components`, O(V+E)) and the MANUAL
# path taken past `scc_hard_max` returns immediately with no hoist/break loop, so a 41-member
# ring is expected to resolve in milliseconds. 5s is generous headroom for CI, not a measured
# runtime; it exists to catch a genuine hang (or an accidental exponential blowup), not to be a
# tight budget.
NO_HANG_DEADLINE_S = 5.0


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

    **`acme-c` is a second, non-SCC consumer of each contract (round VI task 55, §12.31 Leg A).**
    Without it each contract's post-retarget `CONTRACT_CONSUME` count is 1 (only `acme-a`) —
    below `scan.contracts.min_consumers=2` — and the new not-shared-after-retarget check in
    `graph/cycles.py::_hoist_contracts` would reject both, which is not what THIS fixture exists
    to prove (that shape has its own fixture, `not_shared_fleet` below). `acme-c` has one edge
    INTO `acme-b` and none back out, so it joins neither the `{acme-a, acme-b}` 2-cycle nor either
    contract's ranking differently from before — `repos_freed`/`blast_radius` are computed against
    `member_ids = ("acme-a", "acme-b")` and `len(contract.consumer_repo_ids)` respectively, and
    `acme-c` is added identically to both contracts, so the tie-break on `contract_id` this
    fixture's tests rely on is unchanged. Measured (not asserted) against real
    `break_cycles`/`assign_waves` before landing: `res.members == ("acme-a", "acme-b")` unchanged,
    `{e.retargeted_from_repo_id for e in retargeted} == {"acme-b"}` unchanged,
    `min(plan.repo_wave_index.values()) == 1` (was asserted `> 0`, still holds), and
    `plan.repo_wave_index["acme-a"] < plan.repo_wave_index["acme-b"]` unchanged (1 < 2). The one
    assertion this DID move:
    `tests/test_graph_sequence.py::test_criterion_c_counts_only_ungated_repos` asserted
    `plan.repo_members == ("acme-a", "acme-b")`, which is now genuinely 3 repos — fixed there by
    adding `acme-c` (and its `RepoStatus`) rather than by weakening the assertion.
    """
    graph_nodes = nodes("acme-a", "acme-b", "acme-c")
    edges = [
        edge("acme-a", "acme-b", kind=EdgeKind.API_CONTRACT, confidence=0.7,
             path="vendor/k1.proto"),
        edge("acme-a", "acme-b", kind=EdgeKind.API_CONTRACT, confidence=0.7,
             path="vendor/k2.proto"),
        edge("acme-b", "acme-a", kind=EdgeKind.DECLARED_DEP, confidence=1.0, path="pom.xml"),
        edge("acme-c", "acme-b", kind=EdgeKind.API_CONTRACT, confidence=0.7,
             path="vendor/k1-c.proto"),
        edge("acme-c", "acme-b", kind=EdgeKind.API_CONTRACT, confidence=0.7,
             path="vendor/k2-c.proto"),
    ]
    contracts = [
        contract(
            K1, owner="acme-b", owner_path="proto/k1.proto",
            consumers={"acme-a": "vendor/k1.proto", "acme-c": "vendor/k1-c.proto"},
        ),
        contract(
            K2, owner="acme-b", owner_path="proto/k2.proto",
            consumers={"acme-a": "vendor/k2.proto", "acme-c": "vendor/k2-c.proto"},
        ),
    ]
    return graph_nodes, edges, contracts


def six_repo_hub_cycle() -> tuple[list[GraphNode], list[DependencyEdge], list[ContractNode], str]:
    """One owner (`acme-hub`) and 5 spokes, every feedback edge running through ONE shared
    contract. Every spoke is mutually reachable with every other spoke only through the hub — a
    genuine 6-member SCC, not five disjoint 2-cycles (verified by the test itself via
    `nx.strongly_connected_components` before any `break_cycles` call).

    §12.30's flip-back clause needs the backward (feedback) edges at `kind=DECLARED_DEP,
    confidence=1.0` — deliberately **not** the realistic scanned shape (`API_CONTRACT`/
    `INTERNAL_IMPORT` below `ATOMIC_DECLARED_DEP_CONFIDENCE`) a real contract-carrier import would
    actually produce. A genuinely scanned fixture structurally cannot flip to `ATOMIC_WAVE` under
    `--no-hoist-contracts` — `_is_atomic` (`graph/cycles.py:558-573`) requires every feedback edge
    to be `DECLARED_DEP` at `confidence >= 0.95`, and it falls to `EDGE_BREAK` instead (this is
    also why `test_the_same_fleet_stays_cyclic_when_contracts_are_skipped` in
    `tests/test_sequence_e2e.py` asserts `EDGE_BREAK`, not `ATOMIC_WAVE`, for the real e2e
    `cycle_fleet` fixture). This does not weaken the hoist-path proof: `_materialize`'s retarget
    match is purely by `(src_id, evidence_path)` membership in the contract's carrier set,
    independent of `kind`/`confidence` (`cycles.py:710-720`).
    """
    K = "proto:acme.hub.v1"
    spokes = [f"acme-spoke-{i}" for i in range(5)]
    graph_nodes = nodes("acme-hub", *spokes)
    edges: list[DependencyEdge] = []
    for s in spokes:
        edges.append(
            edge("acme-hub", s, kind=EdgeKind.DECLARED_DEP, confidence=1.0, path="package.json")
        )
        # the feedback edge: kind/confidence chosen so --no-hoist-contracts genuinely falls to
        # ATOMIC_WAVE (see the docstring above) -- the retarget match in _materialize is purely by
        # (src_id, evidence_path), so this choice does not affect the hoist path at all.
        edges.append(
            edge(
                s, "acme-hub", kind=EdgeKind.DECLARED_DEP, confidence=1.0, path=f"src/gen/{s}_pb.ts"
            )
        )
    contracts = [
        contract(
            K,
            owner="acme-hub",
            owner_path="proto/hub.proto",
            consumers={s: f"src/gen/{s}_pb.ts" for s in spokes},
        ),
    ]
    return graph_nodes, edges, contracts, K


def not_shared_fleet() -> tuple[list[GraphNode], list[DependencyEdge], list[ContractNode], str]:
    """§12.31 case (i), Leg A (round VI task 55): a contract *declared* with 2 consumers — passes
    5b (vi) detection, genuinely `EXTRACTABLE` — where one of the two is the owner's own vendored
    copy of its own interface, so after retargeting only 1 distinct repo remains on the contract's
    inbound `CONTRACT_CONSUME` edges: below `scan.contracts.min_consumers=2`.

    **The literal extreme — every declared consumer IS the owner — is NOT expressible**, and this
    fixture deliberately does not reach for it: `_materialize` (`cycles.py:706`, `repo_id !=
    owner`) and `infer_contract_edges` (`graph/infer.py:562-563`, `if consumer == owner:
    continue`) both skip the owner's own vendored copy when building retarget/consume edges, so
    with ZERO non-owner consumers no consume edge would exist at all, no retarget would happen,
    the saturating trial would find the 2-cycle still live, and `_hoist_contracts` would return
    early having committed nothing — there is no hoist to roll back, and a fixture built that way
    would pass while testing nothing (measured directly against real `_materialize`/`break_cycles`
    before landing, not asserted). Keeping ONE real non-owner consumer (`acme-spoke-0`) is what
    makes the hoist genuinely dissolve the 2-cycle so the rollback is reached and observable — the
    real-world shape SPEC.md's 6c-H "When the extraction was wrong" describes: "vendored copies of
    one repo's own interface."
    """
    contract_id = "proto:acme.hub.v1"
    graph_nodes = nodes("acme-hub", "acme-spoke-0")
    edges = [
        edge("acme-hub", "acme-spoke-0", kind=EdgeKind.DECLARED_DEP, confidence=1.0,
             path="package.json"),
        edge("acme-spoke-0", "acme-hub", kind=EdgeKind.API_CONTRACT, confidence=0.7,
             path="src/gen/spoke_pb.ts"),
    ]
    contracts = [
        contract(
            contract_id, owner="acme-hub", owner_path="proto/hub.proto",
            consumers={"acme-hub": "vendor/gen/hub_pb.ts", "acme-spoke-0": "src/gen/spoke_pb.ts"},
        ),
    ]
    return graph_nodes, edges, contracts, contract_id


def edge_break_after_rollback_fleet() -> (
    tuple[list[GraphNode], list[DependencyEdge], list[ContractNode], str]
):
    """§12.31 case (ii): a 2-repo SCC that hoists cleanly on a first `break_cycles` pass, and
    whose feedback edge — carried by the contract's OWN generated-code import, `kind=API_CONTRACT`
    at `confidence=0.7`, deliberately below `ATOMIC_DECLARED_DEP_CONFIDENCE` — is genuinely
    breakable once the hoist is gone, so the re-sequence after a rollback falls to `EDGE_BREAK`
    rather than `ATOMIC_WAVE`.

    `acme-c` plays the same role it plays in `multi_chord_fleet` (§12.31 Leg A, round VI task 55):
    a second, non-SCC consumer whose own `API_CONTRACT` edge into the owner keeps the contract's
    post-retarget consumer count at 2 (>= `scan.contracts.min_consumers`), so the FIRST pass really
    hoists (this fixture is not `not_shared_fleet` — that one is built to be REJECTED, this one is
    built to succeed and then be rolled back from outside the hoist mechanism, mirroring a
    `HoistBrokeOwner` build failure discovered downstream rather than a not-shared rejection
    discovered inside 6c-H itself).
    """
    contract_id = "proto:acme.hub.v1"
    graph_nodes = nodes("acme-hub", "acme-spoke-0", "acme-c")
    edges = [
        edge("acme-hub", "acme-spoke-0", kind=EdgeKind.DECLARED_DEP, confidence=1.0,
             path="package.json"),
        edge("acme-spoke-0", "acme-hub", kind=EdgeKind.API_CONTRACT, confidence=0.7,
             path="src/gen/spoke_pb.ts"),
        edge("acme-c", "acme-hub", kind=EdgeKind.API_CONTRACT, confidence=0.7,
             path="src/gen/c_pb.ts"),
    ]
    contracts = [
        contract(
            contract_id, owner="acme-hub", owner_path="proto/hub.proto",
            consumers={"acme-spoke-0": "src/gen/spoke_pb.ts", "acme-c": "src/gen/c_pb.ts"},
        ),
    ]
    return graph_nodes, edges, contracts, contract_id


def ring_cycle(n: int) -> tuple[list[GraphNode], list[DependencyEdge], tuple[str, ...]]:
    """`n` repos in one ring — `acme-00 -> acme-01 -> ... -> acme-{n-1} -> acme-00` — all
    `DECLARED_DEP` at confidence 1.0 and no contract anywhere: a genuine SCC of `n` members, not a
    chain. Every edge is load-bearing — removing any single one turns the ring into a path and the
    SCC stops existing at all, which is what makes this shape (rather than, say, a chain plus one
    back edge) a real n-member cycle instead of a smaller cycle wearing extra nodes.
    """
    repo_ids = tuple(f"acme-{i:02d}" for i in range(n))
    edges = [edge(repo_ids[i], repo_ids[(i + 1) % n]) for i in range(n)]
    return nodes(*repo_ids), edges, repo_ids


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


def test_a_not_shared_after_retarget_contract_is_rejected_with_an_exact_rollback() -> None:
    """§12.31 case (i), Leg A (round VI task 55, ADR-0120): the not-shared-after-retarget check.

    `not_shared_fleet`'s contract is genuinely `EXTRACTABLE` with 2 *declared* consumers, but one
    is the owner's own vendored copy — both edge producers skip it, so only `acme-spoke-0` survives
    retargeting, below `min_consumers=2`. The hoist must be rejected and rolled back in memory
    BEFORE it enters `committed`/`nodes` (this brief's placement decision, ADR-0120) — never
    hoisted, never in `wave_members`, and the rolled-back edges byte-identical to their pre-hoist
    rows (the "restore is exact" property `_hoist_contracts` gets for free by never reassigning
    `edges` to the rejected candidate's materialized result).
    """
    graph_nodes, edges, contracts, contract_id = not_shared_fleet()
    pre_hoist_edges = {e.edge_key: e for e in edges}

    report = break_cycles(build_graph(graph_nodes, edges), contracts=contracts)

    # rejected, not hoisted
    assert report.hoisted_contracts == ()
    assert len(report.rejected_contracts) == 1
    rejected = report.rejected_contracts[0]
    assert rejected.contract_id == contract_id
    assert rejected.status is ContractStatus.REJECTED
    assert rejected.status_detail == "not_shared_after_retarget", (
        "must be distinguishable from the 5b (vi) detection-time rejection's "
        "status_detail='min_consumers' (tests/test_workers_contracts.py:759)"
    )

    # the finding
    assert len(report.findings) == 1
    finding = report.findings[0]
    assert finding.kind == "ContractNotShared"
    assert finding.severity == "warn"
    assert finding.repo_id == "acme-hub"
    assert finding.payload["contract_id"] == contract_id
    assert finding.payload["declared_consumers"] == "2"
    assert finding.payload["post_retarget_consumers"] == "1"
    assert finding.payload["min_consumers"] == "2"

    # never entered the ordering subgraph: no contract wave, no CONTRACT wave_members row
    res = report.resolutions[0]
    assert res.hoisted_contract_ids == ()
    assert res.break_strategy in (BreakStrategy.EDGE_BREAK, BreakStrategy.ATOMIC_WAVE), (
        "the SCC must still be live after the rejection and fall through to 6d/6e unchanged"
    )
    plan = assign_waves(report)
    assert plan.contract_members == ()
    assert all(node_kind != NodeKind.CONTRACT.value for node_kind, _ in plan.wave_index_by_node)

    # the restore is exact: every repo<->repo edge is byte-identical to its pre-hoist row, modulo
    # `ordering_suppressed` -- which 6d legitimately flips AFTER the rollback falls through to it.
    final_by_key = {e.edge_key: e for e in report.edges}
    for key, pre in pre_hoist_edges.items():
        post = final_by_key[key]
        assert post.retargeted_from_repo_id is None, f"{key}: must not have been retargeted"
        assert post.model_copy(update={"ordering_suppressed": False}) == pre.model_copy(
            update={"ordering_suppressed": False}
        ), f"{key}: edge must be byte-identical to its pre-hoist row"


def test_a_forbidden_contract_is_excluded_from_hoisting_alongside_the_extractable_filter() -> None:
    """§12.31 Leg E (round VI task 58): `GraphSection.forbidden_contract_ids` -- threaded from
    `--forbid-hoist` by `cli._sequence_graph_config` -- excludes a contract from `_hoist_contracts`'
    candidate set outright.

    Baseline first, over the SAME fixture and SAME graph: with an empty forbidden set, `K` really
    does dissolve the hub SCC (this is what makes the exclusion below attributable to
    `forbidden_contract_ids`, not to some other property of the fixture -- the same "baseline
    first" discipline `test_the_same_6_repo_cycle_flips_to_atomic_wave_under_no_hoist_contracts`
    below uses for `hoist_contracts=False`). Forbidding `K` must reproduce the identical
    `--no-hoist-contracts` outcome -- `ATOMIC_WAVE`, nothing hoisted -- via a DIFFERENT knob: the
    filter is additive alongside Leg A's not-shared rollback branch, not a replacement for it, so a
    forbidden contract is never even trialed and therefore never REJECTED either (unlike
    `test_a_not_shared_after_retarget_contract_is_rejected_with_an_exact_rollback` above, forbidding
    leaves `rejected_contracts` empty -- it was excluded from candidacy before the saturating trial
    ever ran, not tried and found wanting).
    """
    graph_nodes, edges, contracts, contract_id = six_repo_hub_cycle()
    graph = build_graph(graph_nodes, edges)

    baseline = break_cycles(graph, contracts=contracts)
    assert baseline.hoisted_contracts != (), "the fixture must really dissolve by hoisting alone"
    assert baseline.resolutions[0].break_strategy is BreakStrategy.CONTRACT_HOIST

    forbidden = break_cycles(
        graph, contracts=contracts, config=GraphSection(forbidden_contract_ids=(contract_id,))
    )
    assert forbidden.hoisted_contracts == ()
    assert forbidden.rejected_contracts == (), (
        "excluded from candidacy, never trialed -- distinct from Leg A's REJECTED outcome"
    )
    res = forbidden.resolutions[0]
    assert res.hoisted_contract_ids == ()
    assert res.break_strategy is BreakStrategy.ATOMIC_WAVE


def test_a_planted_6_repo_contract_cycle_is_dissolved_by_hoisting_alone() -> None:
    """§12.30 / SPEC.md:7462, the payoff clause: a 6-repo hub-and-spoke cycle whose every
    feedback edge runs through one shared proto package is dissolved by `CONTRACT_HOIST` alone —
    no edge broken, no `ATOMIC_WAVE`, no `MANUAL`, the contract strictly below all 6 repo waves,
    and the 6 repos spanning more than one wave (not bundled).

    Every asserted value below is a real captured output from an actual `break_cycles` run over
    this exact fixture (`.superpowers/sdd/round-V-criteria-closure/research-20-report.md` §3), not
    a design-time prediction.

    **Persistence, composed rather than re-derived** (Rule 12/Guardrail 6 discipline: a composed
    claim must be disclosed as composed): this test proves the graph-algorithm side only —
    `retargeted_from_repo_id` non-null on every retargeted edge, in memory. That the column
    actually survives the DB write path (`cli.py::_persist_contract_edges` -> `insert_edges`) is
    proven end-to-end, over the real git/CLI `cycle_fleet` fixture, by
    `tests/test_sequence_e2e.py::test_the_retargeted_contract_consume_edge_persists_its_pre_hoist_owner`
    (round VI task 31/32, D23). That test drives 2 repos / 1 retarget, not 6/5 — but
    `_persist_contract_edges` (`src/fleet/cli.py:3515-3563`) is a plain list comprehension with no
    repo- or edge-count-specific branching (`[EdgeRow(..., retargeted_from_repo_id=edge.
    retargeted_from_repo_id) for edge in edges if edge.kind in _CONTRACT_EDGE_KINDS]`, one bulk
    `insert_edges` call), so its 2-repo/1-retarget proof is legitimate evidence the same write path
    holds at this test's 6-repo/5-retarget scale too, without needing a literal 6-repo e2e re-run.
    """
    graph_nodes, edges, contracts, contract_id = six_repo_hub_cycle()
    spokes = [n.node_id for n in graph_nodes if n.node_id != "acme-hub"]
    graph = build_graph(graph_nodes, edges)

    pre_break_sccs = [c for c in nx.strongly_connected_components(graph.G_dag) if len(c) > 1]
    assert len(pre_break_sccs) == 1, "the fixture's own premise: exactly one non-trivial SCC"
    assert pre_break_sccs[0] == {("REPO", "acme-hub"), *(("REPO", s) for s in spokes)}, (
        "a genuine 6-member hub-and-spoke SCC, not five disjoint 2-cycles"
    )

    report = break_cycles(graph, contracts=contracts)
    assert len(report.resolutions) == 1
    res = report.resolutions[0]

    assert res.break_strategy is BreakStrategy.CONTRACT_HOIST
    assert res.hoisted_contract_ids == (contract_id,)
    assert res.broken_edge_keys == (), "CONTRACT_HOIST means no whole-repo edge was broken"
    assert sorted(res.members) == sorted(["acme-hub", *spokes])
    assert [c.status for c in report.hoisted_contracts] == [ContractStatus.HOISTED]

    condensed = condense_for_ordering(report.graph, report.resolutions)
    assert ordering_is_acyclic(condensed), "nx.is_directed_acyclic_graph over the final ordering"

    plan = assign_waves(report)
    contract_wave = plan.wave_index_by_node[(NodeKind.CONTRACT.value, contract_id)]
    repo_waves = {
        repo_id: plan.wave_index_by_node[(NodeKind.REPO.value, repo_id)] for repo_id in res.members
    }
    assert contract_wave == 0
    assert repo_waves == {"acme-hub": 2, **dict.fromkeys(spokes, 1)}
    assert all(contract_wave < wave for wave in repo_waves.values()), (
        "the contract node occupies a strictly lower wave_index than all 6 repos"
    )
    assert len(set(repo_waves.values())) > 1, (
        "the 6 repos land in more than one wave -- i.e. they were not bundled"
    )

    retargeted = [e for e in report.edges if e.retargeted_from_repo_id is not None]
    assert len(retargeted) == 5
    assert {e.retargeted_from_repo_id for e in retargeted} == {"acme-hub"}
    assert {e.kind for e in retargeted} == {EdgeKind.CONTRACT_CONSUME}


def test_the_same_6_repo_cycle_flips_to_atomic_wave_under_no_hoist_contracts() -> None:
    """§12.30's flip-back clause: re-running the same fixture with hoisting unavailable reproduces
    the pre-ADR-0019 outcome exactly -- `ATOMIC_WAVE` with all 6 repos sharing one `wave_index` --
    which is the proof that 6d/6e were not removed when 6c-H was added.

    Two calls, not one: `config=GraphSection(hoist_contracts=False)` (the flag path) AND
    `break_cycles(graph)` with no `contracts=` argument at all (the literal pre-ADR-0019 codepath,
    which never had a `contracts` parameter to disable). Asserting the two calls' full
    `SccResolution`s are equal to each other -- not merely each individually ATOMIC_WAVE -- is what
    proves "reproduces the pre-ADR-0019 outcome exactly" rather than a merely similar-looking one.
    """
    graph_nodes, edges, contracts, _contract_id = six_repo_hub_cycle()
    spokes = [n.node_id for n in graph_nodes if n.node_id != "acme-hub"]
    graph = build_graph(graph_nodes, edges)

    report_flag = break_cycles(
        graph, contracts=contracts, config=GraphSection(hoist_contracts=False)
    )
    report_no_contracts = break_cycles(graph)

    for report in (report_flag, report_no_contracts):
        assert len(report.resolutions) == 1
        res = report.resolutions[0]
        assert res.break_strategy is BreakStrategy.ATOMIC_WAVE
        assert res.hoisted_contract_ids == ()
        assert sorted(res.members) == sorted(["acme-hub", *spokes])
        plan = assign_waves(report)
        repo_waves = {
            plan.wave_index_by_node[(NodeKind.REPO.value, repo_id)] for repo_id in res.members
        }
        assert repo_waves == {0}, "all 6 repos share exactly one wave_index"

    assert report_flag.resolutions[0] == report_no_contracts.resolutions[0], (
        "the flag-disabled path and the literal no-contracts pre-ADR-0019 codepath must produce "
        "the identical SccResolution, not merely two individually-correct ATOMIC_WAVE outcomes"
    )
    assert report_flag.hoisted_contracts == report_no_contracts.hoisted_contracts == ()
    assert report_flag.edges == report_no_contracts.edges


def test_a_rolled_back_hoist_re_sequences_to_edge_break() -> None:
    """§12.31 case (ii)'s own text: "...every affected edge un-hoisted -- its contract-kind row
    excluded from the next graph build and the untouched pre-hoist repo->repo row standing in its
    place ... -- a re-sequenced SCC that falls through to `EDGE_BREAK` or `ATOMIC_WAVE`". Round VI
    task 96 built the rollback-TARGET mechanism (which merge gets reverted) but left this
    fall-through clause with zero test coverage (`tests/test_graph_cycles.py` had zero `FAILED`
    occurrences); this test is that coverage's `EDGE_BREAK` half.

    Two REAL `break_cycles` calls over the SAME graph, not one hand-built resolution:

    Pass 1 hoists the contract for real (`CONTRACT_HOIST`, `hoisted_contracts[0].status ==
    HOISTED`) -- exactly what a genuine `fleet sequence` run does before a downstream
    `HoistBrokeOwner` finding is even possible.

    Pass 2 re-sequences with the SAME pre-hoist graph edges (the "next graph build" per ADR-0122:
    the hoisted contract's edges are read-time excluded, never reconstructed, and the untouched
    pre-hoist repo->repo row -- built into this fixture from the start, never regenerated -- stands
    in their place) and the SAME contract now carrying `status=FAILED` -- the transition a real
    `HoistBrokeOwner` finding drives at the DB layer (`cli.py`'s `_BuildSink`), reproduced here as a
    `model_copy` because `cycles.py` itself never performs that write; it only has to behave
    correctly once handed the result. `break_cycles`'s own construction is what does the rest: the
    FAILED contract fails BOTH of `committed`'s membership test (`status in (HOISTED, MIGRATED)`,
    `cycles.py:429`) and `_hoist_contracts`'s candidacy filter (`status is EXTRACTABLE`,
    `cycles.py:672`), so nothing re-hoists it and the 2-repo SCC is live again -- and its one
    feedback edge (`API_CONTRACT` at 0.7, not a `DECLARED_DEP` at >=0.95) is genuinely breakable,
    so 6d suppresses it and the SCC resolves `EDGE_BREAK`.
    """
    graph_nodes, edges, contracts, contract_id = edge_break_after_rollback_fleet()
    graph = build_graph(graph_nodes, edges)

    first = break_cycles(graph, contracts=contracts)
    res1 = first.resolutions[0]
    assert res1.break_strategy is BreakStrategy.CONTRACT_HOIST
    assert res1.hoisted_contract_ids == (contract_id,)
    assert len(first.hoisted_contracts) == 1
    assert first.hoisted_contracts[0].status is ContractStatus.HOISTED

    failed = first.hoisted_contracts[0].model_copy(
        update={"status": ContractStatus.FAILED, "status_detail": "hoist_broke_owner"}
    )
    second = break_cycles(graph, contracts=[failed])

    assert second.hoisted_contracts == (), "a FAILED contract is never (re-)committed"
    assert len(second.resolutions) == 1
    res2 = second.resolutions[0]
    assert res2.scc_id == res1.scc_id, (
        "membership is unchanged (acme-hub, acme-spoke-0) -- this is the SAME SCC re-sequenced, "
        "not a differently-shaped one; scc_id is content-derived over membership alone"
    )
    assert res2.hoisted_contract_ids == (), "the failed contract must not be re-hoisted"
    assert res2.break_strategy is BreakStrategy.EDGE_BREAK
    assert res2.broken_edge_keys != ()
    assert _live_component(second.graph.G_dag, res2.members) == (), (
        "6d's break must have actually dissolved the SCC"
    )

    # every contract-kind node/edge is gone from the re-sequenced graph -- the FAILED contract
    # never re-enters `nodes`, so it can never reach `wave_members` either.
    assert not any(kind == NodeKind.CONTRACT.value for kind, _ in second.graph.G.nodes)
    plan = assign_waves(second)
    assert plan.contract_members == ()


def test_a_rolled_back_hoist_re_sequences_to_atomic_wave() -> None:
    """§12.31 case (ii)'s `ATOMIC_WAVE` half of the same fall-through clause. Reuses
    `six_repo_hub_cycle` -- already proven by `test_the_same_6_repo_cycle_flips_to_atomic_wave_
    under_no_hoist_contracts` to flip to `ATOMIC_WAVE` when hoisting is unavailable -- but drives
    it through the genuine two-pass rollback shape (hoist for real, then fail it, then
    re-sequence) rather than through `hoist_contracts=False`/no-`contracts=` config, which never
    exercises `committed`'s status filter or `_hoist_contracts`' candidacy filter at all.
    """
    graph_nodes, edges, contracts, _contract_id = six_repo_hub_cycle()
    spokes = [n.node_id for n in graph_nodes if n.node_id != "acme-hub"]
    graph = build_graph(graph_nodes, edges)

    first = break_cycles(graph, contracts=contracts)
    res1 = first.resolutions[0]
    assert res1.break_strategy is BreakStrategy.CONTRACT_HOIST
    assert len(first.hoisted_contracts) == 1
    assert first.hoisted_contracts[0].status is ContractStatus.HOISTED

    failed = first.hoisted_contracts[0].model_copy(
        update={"status": ContractStatus.FAILED, "status_detail": "hoist_broke_owner"}
    )
    second = break_cycles(graph, contracts=[failed])

    assert second.hoisted_contracts == ()
    assert len(second.resolutions) == 1
    res2 = second.resolutions[0]
    assert res2.scc_id == res1.scc_id, "same 6-member SCC, re-sequenced -- not a new one"
    assert sorted(res2.members) == sorted(["acme-hub", *spokes])
    assert res2.hoisted_contract_ids == ()
    assert res2.break_strategy is BreakStrategy.ATOMIC_WAVE
    assert res2.broken_edge_keys == (), "ATOMIC_WAVE means 6d could not dissolve it either"

    plan = assign_waves(second)
    assert plan.contract_members == ()
    repo_waves = {
        plan.wave_index_by_node[(NodeKind.REPO.value, repo_id)] for repo_id in res2.members
    }
    assert repo_waves == {0}, "all 6 repos land in the one atomic wave, none pinned to the failed "\
        "contract's old wave"


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


def test_a_12_repo_cycle_shares_one_scc_id_across_all_members() -> None:
    """§12.19 / SPEC.md:7434: "a planted 12-repo cycle likewise contract-free produces
    break_strategy = ATOMIC_WAVE with all 12 members sharing one wave_index and one
    PullRequestDraft.scc_id." `SccResolution.scc_id` and the `CycleFinding.scc_id` /
    `atomic_wave_index` `to_finding` derives from it are each a single scalar on the one
    resolution/finding covering every member, so "all 12 share one" is a guarantee of the type
    once the fixture really is a 12-member SCC — what this test proves is that a genuine 12-node
    ring reaches that resolution intact, with no member silently dropped along the way.
    (`PullRequestDraft.scc_id`, one layer up in `models/tasks.py`, is copied verbatim from
    `CycleFinding.scc_id` there and is out of this file's scope, which stops at the finding.)
    """
    graph_nodes, edges, repo_ids = ring_cycle(12)
    report = break_cycles(build_graph(graph_nodes, edges))

    assert len(report.resolutions) == 1, "one 12-member SCC, one resolution -- never split"
    res = report.resolutions[0]
    assert res.break_strategy is BreakStrategy.ATOMIC_WAVE
    assert len(res.members) == 12
    assert res.members == tuple(sorted(repo_ids)), "all 12 members -- none silently excluded"
    assert res.scc_id == scc_id_for(res.members), "content-derived over the full 12-member set"

    finding = res.to_finding(atomic_wave_index=0)
    assert finding.members == sorted(repo_ids)
    assert finding.scc_id == res.scc_id, "the one scc_id every member shares"
    assert finding.atomic_wave_index == 0, "the one wave_index that covers all 12 members"


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


def test_a_41_repo_cycle_completes_without_hanging() -> None:
    """§12.19 / SPEC.md:7434: "a planted 41-repo cycle produces break_strategy = MANUAL with all
    members REQUIRES_HUMAN_INTERVENTION and the rest of the fixture fleet still completing. In
    every case `nx.is_directed_acyclic_graph` holds over the final ordering subgraph, and the run
    terminates — no hang."

    41 is one past `graph.scc_hard_max`'s default of 40 (`GraphSection.scc_hard_max`,
    `src/fleet/settings.py`), so this is the literal stated scale, not a substitute: `_resolve_scc`
    refuses an SCC that size before it ever reaches the hoist saturating-trial or the 6d break
    loop (`len(member_ids) > cfg.scc_hard_max` returns `MANUAL` immediately, `src/fleet/graph/
    cycles.py`), so a real 41-node ring is also the *cheapest* of the three named scales to run —
    there is no smaller adversarial case that exercises more of the algorithm's actual complexity
    for this specific clause, since MANUAL is a short-circuit rather than an iterative rung. The
    hoist loop (bounded by `len(candidates)`, i.e. contracts, not repos) and the 6d break loop
    (bounded by `max_breaks_per_scc`) are both already-bounded loops elsewhere in the ladder, not
    reachable from this path, so a wall-clock bound here is a genuine no-hang property test rather
    than a disguised iteration-count assertion.
    """
    graph_nodes, edges, repo_ids = ring_cycle(41)
    bystander = "zzz-bystander"  # "the rest of the fixture fleet still completing" (SPEC.md:7434)
    graph_nodes = [*graph_nodes, *nodes(bystander)]
    edges = [*edges, edge(bystander, repo_ids[0])]
    graph = build_graph(graph_nodes, edges)

    started = monotonic()
    report = break_cycles(graph)
    elapsed = monotonic() - started

    assert elapsed < NO_HANG_DEADLINE_S, (
        f"break_cycles took {elapsed:.3f}s on a 41-repo cycle, exceeding the "
        f"{NO_HANG_DEADLINE_S}s no-hang deadline"
    )
    assert len(report.resolutions) == 1
    res = report.resolutions[0]
    assert res.break_strategy is BreakStrategy.MANUAL
    assert len(res.members) == 41
    assert res.members == tuple(sorted(repo_ids)), "all 41 members -- none silently dropped"
    assert report.manual_repo_ids == tuple(sorted(repo_ids))

    # `report.graph.G_dag` itself still carries the un-suppressed MANUAL cycle (§3.1 6e's MANUAL
    # branch returns before any edge is broken) -- it is `condense_for_ordering`'s output, not the
    # raw graph, that SPEC.md:7434 means by "the final ordering subgraph". `assign_waves` builds
    # exactly this condensation (`src/fleet/graph/sequence.py`), which is why this is the same
    # instrument `test_manual_members_are_absent_from_the_ordering_subgraph` in
    # `tests/test_graph_sequence.py` uses for the 3-node MANUAL case.
    condensed = condense_for_ordering(report.graph, report.resolutions)
    assert sorted(condensed.nodes) == [("REPO", bystander)], (
        "all 41 MANUAL members are dropped outright; only the bystander remains"
    )
    assert ordering_is_acyclic(condensed), "the final ordering subgraph is acyclic -- no hang"


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
