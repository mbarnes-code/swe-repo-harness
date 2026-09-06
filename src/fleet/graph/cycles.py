"""SCC detection, deterministic feedback sets, contract hoisting, edge breaking and the
atomic-wave fallback (§3.1 step 6).

Cycles are **broken, not merely detected**: every `CycleFinding` carries a `break_strategy`, and
the model itself rejects `EDGE_BREAK` with no `broken_edge_keys`. The ladder is 6a → 6b → 6c →
6c-H → 6d → 6e, and no rung is skipped or reordered.

Four decisions in this module exist because a specific bug was found in review. Each is load
bearing; none of them is a style choice.

1. **`scc_id` is content-derived** — `"scc:" + sha256("\\x00".join(sorted(members)))[:16]`
   (`scc_id_for`). §3.1 6a's prose still says "derived from `min(sorted(member repo_ids))`", but
   an *int* derived from strings is either `hash()` — randomized per process under
   `PYTHONHASHSEED` — or undefined, and `SccId` in §5.3 fixes the recipe used here. A membership
   change therefore yields a **new** id which **supersedes** the old finding
   (`supersede_findings`), rather than renumbering an SCC in place while an `ATOMIC_WAVE` PR
   already names the old one.

2. **6c-H trials saturate before they commit.** `G_all` materializes *every* `EXTRACTABLE`
   contract owned by a member of `S` at once; only if that dissolves `S` does the greedy commit
   loop run. A greedy one-at-a-time loop that breaks on `repos_freed(best) == 0` cannot dissolve
   the modal cycle shape — one owner publishing several contracts to one consumer — because no
   single hoist frees a repo there, and the SCCs hoisting exists to fix fell through to the
   atomic fallback. `repos_freed(k) = len(S.members) - len(S_k)`, where `len(S_k) == 1` when no
   non-trivial component of `G_k` contains `k.owning_repo_id` — a definition that must not raise
   on the success path, because the success path is exactly the one that leaves the owner in no
   non-trivial component at all.

3. **`ATOMIC_WAVE` coarsening emits ONE library target per `(ecosystem, scc_id)`**
   (`coarsen_atomic_scc`). One target per member reproduces the SCC in the Bazel target graph,
   `bazel build` fails with a dependency-cycle error, and the SCC's whole dependent cone is
   stranded. A per-member target survives only for a member with no intra-SCC inbound edge.

Everything here is deterministic: no `hash()`, no set-iteration order reaches an output, and
every tie-break bottoms out in a total order on `edge_key` / `contract_id` / `repo_id`.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Final, Literal

import networkx as nx  # type: ignore[import-untyped]  # no py.typed; see HONEST_GAPS

from fleet.graph.build import FleetGraph, NodeRef, build_graph
from fleet.graph.infer import infer_contract_edges
from fleet.graph.query import descendants
from fleet.models.enums import BreakStrategy, ContractStatus, Ecosystem, EdgeKind, NodeKind
from fleet.models.graph import (
    ContractNode,
    CycleFinding,
    DependencyEdge,
    GraphNode,
    edge_key_for,
)
from fleet.settings import ContractsSection, GraphSection

__all__ = [
    "ATOMIC_DECLARED_DEP_CONFIDENCE",
    "KIND_RANK",
    "RESERVED_SCC_SEGMENT",
    "CoarsePlan",
    "CoarseTarget",
    "CycleReport",
    "EdgeClass",
    "GraphFinding",
    "MemberSources",
    "SccResolution",
    "break_cost",
    "break_cycles",
    "classify_intra_scc_edges",
    "coarsen_atomic_scc",
    "scc_dest",
    "scc_id_for",
    "scc_target_name",
    "supersede_findings",
]

_NUL: Final = "\x00"

KIND_RANK: Final[dict[EdgeKind, int]] = {
    EdgeKind.DYNAMIC_REF: 0,
    EdgeKind.API_CONTRACT: 1,
    EdgeKind.INTERNAL_IMPORT: 2,
    EdgeKind.PUBLISHED_ARTIFACT: 3,
    EdgeKind.DECLARED_DEP: 4,
    EdgeKind.CONTRACT_CONSUME: 5,
    EdgeKind.CONTRACT_IMPL: 6,
}
"""§3.1 6c, verbatim. `CONTRACT_IMPL`/`CONTRACT_CONSUME` rank *above* `DECLARED_DEP` and are
therefore never chosen: breaking one would undo the hoist that produced it. `SHARED_RESOURCE` is
absent because it never enters the ordering subgraph (ADR-0018); it falls back to `len(KIND_RANK)`
if a widened `dag_edge_kinds` admits it, which keeps it the most expensive thing to break."""

ATOMIC_DECLARED_DEP_CONFIDENCE: Final = 0.95
"""§3.1 6e: "every feedback edge is a `DECLARED_DEP` at `confidence >= 0.95`" — there is no weak
edge to yield, so the cycle is real code and pretending otherwise produces a build that cannot
link."""

RESERVED_SCC_SEGMENT: Final = "_scc"
"""§3.3: `scc_dest` puts the coarsened target under `<monorepo_dir>/_scc/`, and the step-8
collision audit reserves that namespace so a real repo can never occupy it."""

type EdgeClass = Literal["TREE", "FORWARD", "CROSS", "BACK"]

type _Nodes = dict[NodeRef, GraphNode]
type _Committed = dict[str, ContractNode]
type _SccOutcome = tuple[
    "SccResolution", _Nodes, list[DependencyEdge], FleetGraph, _Committed,
    tuple["GraphFinding", ...], tuple[ContractNode, ...],
]
type _HoistOutcome = tuple[
    tuple[str, ...], _Nodes, list[DependencyEdge], FleetGraph, _Committed,
    tuple["GraphFinding", ...], tuple[ContractNode, ...],
]
"""The state threaded through the ladder: the resolution (or the hoisted ids), the node set, the
edge rows, the graph rebuilt from them, and the contracts committed so far. Threaded rather than
held on an object because every rung must hand the *next* rung a graph that already reflects its
own decision — 6d must break edges in the graph 6c-H hoisted into.

The last two elements — a `GraphFinding` tuple and a rejected-`ContractNode` tuple — are 6c-H's
not-shared-after-retarget outcome (§12.31 case (i), Leg A): threaded the same way as everything
else here so a rejection inside one SCC's hoist loop reaches `CycleReport` without a side channel."""


def scc_id_for(members: Iterable[str]) -> str:
    """`"scc:" + sha256("\\x00".join(sorted(members))).hexdigest()[:16]` (§5.3 `SccId`).

    Content-derived over the member set, so it is the same string in every process, under every
    `PYTHONHASHSEED`, and in every run that finds the same SCC — and so that adding a member
    yields a *different* id rather than silently redefining one an in-flight PR already names.
    """
    joined = _NUL.join(sorted(members))
    return "scc:" + sha256(joined.encode("utf-8")).hexdigest()[:16]


# =======================================================================================
# findings
# =======================================================================================


@dataclass(frozen=True, slots=True)
class GraphFinding:
    """One `findings` row raised by the sequencer, before the `findings` table exists in memory.

    §6 makes `findings.kind` free text on purpose, so this carries a `str` rather than an enum;
    `fingerprint` is the sha256 of the semantic identity, which is what makes a re-run re-raise
    the same finding without duplicating it (§11.7).
    """

    kind: str
    severity: Literal["warn", "error"] = "warn"
    repo_id: str | None = None
    payload: Mapping[str, str] = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        body = json.dumps(
            {
                "kind": self.kind,
                "repo_id": self.repo_id,
                "payload": dict(sorted(self.payload.items())),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return sha256(body.encode("utf-8")).hexdigest()


# =======================================================================================
# 6b — deterministic DFS edge classification
# =======================================================================================


def classify_intra_scc_edges(
    dag: nx.DiGraph, members: Iterable[NodeRef]
) -> dict[tuple[NodeRef, NodeRef], EdgeClass]:
    """§3.1 6b: DFS from the lexicographically smallest member, children visited in sorted order.

    Only `BACK` arcs close cycles, so the feedback edge set is the `BACK` set. The *ordering* is
    the whole point: an unordered DFS yields a different (still valid) feedback set every run,
    which would make wave assignment non-reproducible across two runs of the same fleet.
    """
    inside = frozenset(members)
    classes: dict[tuple[NodeRef, NodeRef], EdgeClass] = {}
    disc: dict[NodeRef, int] = {}
    gray: set[NodeRef] = set()
    clock = 0

    def children(node: NodeRef) -> Iterator[NodeRef]:
        return iter(sorted(v for v in dag.successors(node) if v in inside))

    for root in sorted(inside):
        if root in disc:
            continue
        clock += 1
        disc[root] = clock
        gray.add(root)
        stack: list[tuple[NodeRef, Iterator[NodeRef]]] = [(root, children(root))]
        while stack:
            node, it = stack[-1]
            child = next(it, None)
            if child is None:
                gray.discard(node)
                stack.pop()
                continue
            if child not in disc:
                classes[(node, child)] = "TREE"
                clock += 1
                disc[child] = clock
                gray.add(child)
                stack.append((child, children(child)))
            elif child in gray:
                classes[(node, child)] = "BACK"
            elif disc[child] > disc[node]:
                classes[(node, child)] = "FORWARD"
            else:
                classes[(node, child)] = "CROSS"
    return classes


# =======================================================================================
# 6c — break_cost
# =======================================================================================


def break_cost(edge: DependencyEdge, graph: FleetGraph) -> tuple[float, int, int, int, str]:
    """§3.1 6c, verbatim. Ascending; the cheapest feedback edge becomes the proposed break.

    `descendants` runs on `G_rev` — "fewest transitive dependents disturbed" is a question about
    what depends on the *destination*, and `G` would answer the mirror-image question.
    """
    dst_ref: NodeRef = (edge.dst_kind.value, str(edge.dst_id))
    dependents = len(descendants(graph.G_rev, dst_ref)) if graph.G_rev.has_node(dst_ref) else 0
    return (
        edge.confidence,
        KIND_RANK.get(edge.kind, len(KIND_RANK)),
        dependents,
        0 if edge.ambiguous else 1,
        edge.edge_key,
    )


# =======================================================================================
# results
# =======================================================================================


@dataclass(frozen=True, slots=True)
class SccResolution:
    """One non-trivial SCC of the raw ordering subgraph and how step 6 dealt with it.

    Not a `CycleFinding` yet: an `ATOMIC_WAVE` finding is invalid without `atomic_wave_index`,
    and that index is step 7's answer, not step 6's. `to_finding` is where the two meet.
    """

    scc_id: str
    members: tuple[str, ...]
    edge_keys: tuple[str, ...]
    feedback_edge_keys: tuple[str, ...]
    proposed_break_edge_key: str | None
    hoisted_contract_ids: tuple[str, ...] = ()
    broken_edge_keys: tuple[str, ...] = ()
    break_strategy: BreakStrategy = BreakStrategy.EDGE_BREAK
    rationale: str = ""

    @property
    def is_condensable(self) -> bool:
        """`ATOMIC_WAVE` and `MANUAL` SCCs are the two that survive step 6 still cyclic, and are
        therefore the two the success criterion (b) check condenses (§3.1 (b))."""
        return self.break_strategy in (BreakStrategy.ATOMIC_WAVE, BreakStrategy.MANUAL)

    def to_finding(
        self, *, atomic_wave_index: int | None = None, superseded_by: str | None = None
    ) -> CycleFinding:
        return CycleFinding(
            scc_id=self.scc_id,
            members=list(self.members),
            edges=list(self.edge_keys),
            feedback_edge_keys=list(self.feedback_edge_keys),
            proposed_break_edge_key=self.proposed_break_edge_key,
            superseded_by=superseded_by,
            hoisted_contract_ids=list(self.hoisted_contract_ids),
            broken_edge_keys=list(self.broken_edge_keys),
            break_strategy=self.break_strategy,
            atomic_wave_index=atomic_wave_index,
            rationale=self.rationale,
        )


@dataclass(frozen=True, slots=True)
class CycleReport:
    """Everything step 6 decided, plus the graph step 7 must sequence.

    `graph` is rebuilt from `edges`, so it already carries the hoists' retargets and the 6d
    suppressions: step 7 never has to re-apply a decision made here.
    """

    resolutions: tuple[SccResolution, ...]
    nodes: tuple[GraphNode, ...]
    edges: tuple[DependencyEdge, ...]
    graph: FleetGraph
    hoisted_contracts: tuple[ContractNode, ...]
    findings: tuple[GraphFinding, ...] = ()
    rejected_contracts: tuple[ContractNode, ...] = ()
    """§12.31 case (i), Leg A: contracts 6c-H tried to hoist and rolled back in-memory because the
    materialized edges did not corroborate `scan.contracts.min_consumers` distinct consumers. Each
    carries `status=ContractStatus.REJECTED` and `status_detail="not_shared_after_retarget"` —
    distinct from the 5b (vi) detection-time rejection's `status_detail="min_consumers"`
    (`workers/contracts.py`) — because this is a different question asked at a different time:
    "do the retargeted edges corroborate the count 5b already checked?", not the count itself."""

    @property
    def manual_repo_ids(self) -> tuple[str, ...]:
        """Members of a `MANUAL` SCC — `REQUIRES_HUMAN_INTERVENTION`, excluded from the ordering
        subgraph entirely (§3.1 criterion (b)), never assigned a wave."""
        return tuple(
            sorted(
                {
                    member
                    for res in self.resolutions
                    if res.break_strategy is BreakStrategy.MANUAL
                    for member in res.members
                }
            )
        )

    @property
    def scc_id_by_member(self) -> Mapping[str, str]:
        """repo_id → `scc_id` for every member of a condensable (`ATOMIC_WAVE`/`MANUAL`) SCC."""
        out: dict[str, str] = {}
        for res in sorted(self.resolutions, key=lambda r: r.scc_id):
            if res.is_condensable:
                for member in res.members:
                    out[member] = res.scc_id
        return dict(sorted(out.items()))

    @property
    def atomic_resolutions(self) -> tuple[SccResolution, ...]:
        return tuple(r for r in self.resolutions if r.break_strategy is BreakStrategy.ATOMIC_WAVE)

    def resolution(self, scc_id: str) -> SccResolution:
        for res in self.resolutions:
            if res.scc_id == scc_id:
                return res
        raise KeyError(scc_id)


def supersede_findings(
    prior: Sequence[CycleFinding], current: Sequence[SccResolution]
) -> tuple[CycleFinding, ...]:
    """Mark prior findings whose SCC changed membership as `superseded_by` the new `scc_id`.

    A membership change yields a *new* content-derived id (`scc_id_for`), so the prior finding is
    never mutated in place: the decision an already-open `ATOMIC_WAVE` PR was opened under stays
    readable, and nothing dangles at the old id. The successor is the current SCC sharing the most
    members, tie-broken by `scc_id` so the choice is total.
    """
    live = {res.scc_id for res in current}
    ordered = sorted(current, key=lambda r: r.scc_id)
    out: list[CycleFinding] = []
    for finding in prior:
        if finding.scc_id in live or finding.superseded_by is not None:
            out.append(finding)
            continue
        members = set(finding.members)
        best = max(
            ordered,
            key=lambda r: (len(members & set(r.members)), tuple(-ord(c) for c in r.scc_id)),
            default=None,
        )
        if best is None or not (members & set(best.members)):
            out.append(finding)
            continue
        out.append(finding.model_copy(update={"superseded_by": best.scc_id}))
    return tuple(out)


# =======================================================================================
# 6a–6e — the ladder
# =======================================================================================


def break_cycles(
    graph: FleetGraph,
    *,
    contracts: Sequence[ContractNode] = (),
    config: GraphSection | None = None,
    min_consumers: int | None = None,
) -> CycleReport:
    """Run §3.1 step 6 end to end and return the sequenced-ready graph.

    The ladder, in order and without a skipped rung: 6a condense, 6b classify, 6c rank, **6c-H
    hoist**, 6d break, 6e atomic/manual. Hoisting runs strictly before any whole-repo edge is
    broken and before any atomic wave is considered, exactly as §3.1 6c-H requires.

    `min_consumers` is §12.31 case (i)'s not-shared-after-retarget threshold — the same question
    `scan.contracts.min_consumers` already answered once at 5b (vi) detection time, re-asked here
    of the retargeted edges. The production call site (`cli._sequence_impl`) passes
    `settings.config.scan.contracts.min_consumers` explicitly — the same field
    `workers/contracts.py:797` reads — so the two checks read one live, operator-configured value
    rather than two independent sources that happen to share a name. `None` is a **fallback for
    every other caller** (tests, and any future call site that has no `FleetSettings` in hand):
    it resolves to `ContractsSection().min_consumers`, the field's own declared default — correct
    only in the absence of a real settings object, never a substitute for reading one when it
    exists. It is a config value, not an operator-facing override: nothing in `cli.py` threads a
    flag to it.
    """
    cfg = config or GraphSection()
    resolved_min_consumers = (
        ContractsSection().min_consumers if min_consumers is None else min_consumers
    )
    nodes: _Nodes = {
        (kind, node_id): GraphNode(kind=NodeKind(kind), node_id=node_id)
        for kind, node_id in graph.node_refs()
    }
    edges: list[DependencyEdge] = list(graph.edges_by_key.values())

    by_id = {c.contract_id: c for c in contracts}
    # Already-HOISTED contracts survive a `contracts` rebuild and are PRE-COMMITTED (§3.1
    # "Contract extraction is re-runnable by construction"): materialized here, never re-ranked.
    committed: dict[str, ContractNode] = {
        c.contract_id: c
        for c in sorted(contracts, key=lambda c: c.contract_id)
        if c.status in (ContractStatus.HOISTED, ContractStatus.MIGRATED)
    }
    if committed:
        edges = _materialize(edges, tuple(committed.values()))
        for contract in committed.values():
            nodes[contract.node.key] = contract.node

    current = _rebuild(nodes.values(), edges, cfg)
    resolutions: list[SccResolution] = []
    findings: list[GraphFinding] = []
    rejected_contracts: list[ContractNode] = []

    for members in _nontrivial_sccs(current.G_dag):
        member_ids = tuple(node_id for kind, node_id in members if kind == NodeKind.REPO.value)
        if len(member_ids) < 2:  # pragma: no cover - a contract is a sink, so this cannot happen
            continue
        resolution, nodes, edges, current, committed, scc_findings, scc_rejected = _resolve_scc(
            members=members,
            member_ids=member_ids,
            nodes=nodes,
            edges=edges,
            graph=current,
            contracts=by_id,
            committed=committed,
            cfg=cfg,
            min_consumers=resolved_min_consumers,
        )
        resolutions.append(resolution)
        findings.extend(scc_findings)
        rejected_contracts.extend(scc_rejected)

    hoisted = tuple(
        contract
        for _, contract in sorted(committed.items())
        if contract.status is not ContractStatus.MIGRATED
    )
    return CycleReport(
        resolutions=tuple(sorted(resolutions, key=lambda r: r.scc_id)),
        nodes=tuple(nodes[ref] for ref in sorted(nodes)),
        edges=tuple(sorted(edges, key=lambda e: e.edge_key)),
        graph=current,
        hoisted_contracts=hoisted,
        findings=tuple(sorted(findings, key=lambda f: f.fingerprint)),
        rejected_contracts=tuple(sorted(rejected_contracts, key=lambda c: c.contract_id)),
    )


def _resolve_scc(
    *,
    members: tuple[NodeRef, ...],
    member_ids: tuple[str, ...],
    nodes: _Nodes,
    edges: list[DependencyEdge],
    graph: FleetGraph,
    contracts: Mapping[str, ContractNode],
    committed: _Committed,
    cfg: GraphSection,
    min_consumers: int,
) -> _SccOutcome:
    scc_id = scc_id_for(member_ids)
    edge_keys = _intra_edge_keys(graph.G_dag, members)
    feedback = _feedback_edge_keys(graph.G_dag, members)
    proposed = _cheapest(feedback, graph)

    if len(member_ids) > cfg.scc_hard_max:
        return (
            SccResolution(
                scc_id=scc_id,
                members=member_ids,
                edge_keys=edge_keys,
                feedback_edge_keys=feedback,
                proposed_break_edge_key=proposed,
                break_strategy=BreakStrategy.MANUAL,
                rationale=(
                    f"{len(member_ids)} members exceeds graph.scc_hard_max={cfg.scc_hard_max}; "
                    "refusing beats emitting an unmergeable artifact (§3.1 6e)"
                ),
            ),
            nodes,
            edges,
            graph,
            committed,
            (),
            (),
        )

    hoisted_ids: tuple[str, ...] = ()
    findings: tuple[GraphFinding, ...] = ()
    rejected: tuple[ContractNode, ...] = ()
    if cfg.hoist_contracts:
        hoisted_ids, nodes, edges, graph, committed, findings, rejected = _hoist_contracts(
            member_ids=member_ids,
            nodes=nodes,
            edges=edges,
            graph=graph,
            contracts=contracts,
            committed=committed,
            cfg=cfg,
            min_consumers=min_consumers,
        )

    live = _live_component(graph.G_dag, member_ids)
    if not live and not hoisted_ids:  # pragma: no cover - the SCC came from this same graph
        raise ValueError(
            f"{scc_id}: the SCC dissolved with no hoist committed; the graph changed underneath "
            "step 6, which would make the recorded break strategy a lie"
        )
    if not live:
        return (
            SccResolution(
                scc_id=scc_id,
                members=member_ids,
                edge_keys=edge_keys,
                feedback_edge_keys=feedback,
                proposed_break_edge_key=proposed,
                hoisted_contract_ids=hoisted_ids,
                break_strategy=BreakStrategy.CONTRACT_HOIST,
                rationale=(
                    f"dissolved by hoisting {len(hoisted_ids)} contract(s) "
                    f"({', '.join(hoisted_ids)}); no edge was broken (§3.1 6c-H)"
                ),
            ),
            nodes,
            edges,
            graph,
            committed,
            findings,
            rejected,
        )

    broken: list[str] = []
    while live and len(broken) < cfg.max_breaks_per_scc:
        if _is_atomic(live, graph, cfg):
            break
        candidates = _feedback_edge_keys(graph.G_dag, live)
        cheapest = _cheapest(candidates, graph)
        if cheapest is None:
            break
        edges = _suppress(edges, cheapest)
        broken.append(cheapest)
        graph = _rebuild(nodes.values(), edges, cfg)
        live = _live_component(graph.G_dag, member_ids)

    if not live:
        strategy = BreakStrategy.EDGE_BREAK
        rationale = (
            f"suppressed {len(broken)} feedback edge(s) after "
            f"{len(hoisted_ids)} hoist(s) (§3.1 6d)"
        )
    else:
        strategy = BreakStrategy.ATOMIC_WAVE
        rationale = (
            f"{len(member_ids)} members still cyclic after {len(hoisted_ids)} hoist(s) and "
            f"{len(broken)} break(s); migrating as one unit (§3.1 6e)"
        )
    return (
        SccResolution(
            scc_id=scc_id,
            members=member_ids,
            edge_keys=edge_keys,
            feedback_edge_keys=feedback,
            proposed_break_edge_key=proposed,
            hoisted_contract_ids=hoisted_ids,
            broken_edge_keys=tuple(broken),
            break_strategy=strategy,
            rationale=rationale,
        ),
        nodes,
        edges,
        graph,
        committed,
        findings,
        rejected,
    )


def _is_atomic(live: tuple[NodeRef, ...], graph: FleetGraph, cfg: GraphSection) -> bool:
    """§3.1 6e's two *pre-emptive* triggers: too big to break, or nothing weak enough to yield.

    The third trigger — `max_breaks_per_scc` exhausted and still cyclic — is the 6d loop's exit
    condition and is decided by the caller.
    """
    if len(live) > cfg.scc_atomic_threshold:
        return True
    feedback = _feedback_edge_keys(graph.G_dag, live)
    if not feedback:
        return False
    records = [graph.edges_by_key[key] for key in feedback]
    return all(
        e.kind is EdgeKind.DECLARED_DEP and e.confidence >= ATOMIC_DECLARED_DEP_CONFIDENCE
        for e in records
    )


# =======================================================================================
# 6c-H — contract hoisting, saturating trial first
# =======================================================================================


def _hoist_contracts(
    *,
    member_ids: tuple[str, ...],
    nodes: _Nodes,
    edges: list[DependencyEdge],
    graph: FleetGraph,
    contracts: Mapping[str, ContractNode],
    committed: _Committed,
    cfg: GraphSection,
    min_consumers: int,
) -> _HoistOutcome:
    """§3.1 6c-H. **Saturate, then commit greedily** — never a one-at-a-time loop that stops at
    the first zero-gain trial.

    `C` is every `EXTRACTABLE` contract owned by a member of `S`. `G_all` materializes all of `C`
    at once and answers the only question a single-contract trial cannot: *can hoisting work at
    all?* An SCC closed by several contracts published from one owner to one consumer frees no
    repo under any single hoist — `repos_freed(k) == 0` for every `k` — so a greedy loop with an
    immediate zero-gain break abandons exactly the shape 6c-H exists to fix. Once `G_all` says
    yes, the greedy commit loop decides *which* hoists to spend, and it terminates by
    construction: every iteration commits a distinct contract, so it is bounded at `len(C)`.
    `max_hoists_per_scc` is a safety valve, not the terminating condition.

    **§12.31 case (i), Leg A — not-shared-after-retarget, checked before commit.** SPEC (§3.1
    6c-H "When the extraction was wrong") describes counting `edges` "at the end of 6c-H"; this
    checks the SAME observable — distinct repos on the contract's inbound `CONTRACT_CONSUME` edges
    once retargeted — but INSIDE the loop, before `chosen` enters `committed`/`nodes` (ADR-0120).
    That placement is what makes the rollback exact: `edges` is reassigned to the materialized
    result only on the accept path, so on the reject path the pre-`_materialize` `edges` (this
    function's own local variable) is never overwritten and needs no reconstruction — a
    reconstruction the retargeted rows themselves cannot support (`retargeted_from_repo_id`
    survives the retarget but `_materialize` also drops `dst_coordinate`, and
    `DependencyEdge._node_shape` requires it for a REPO-kind dst; research-32 flagged the two
    sentences claiming otherwise for controller adjudication, out of scope here). It also keeps
    the rejected contract out of `nodes`/`committed` by construction, so it can never reach
    `wave_members` without a compensating delete — there is no `DELETE FROM wave_members` in this
    codebase and this task adds none.
    """
    members = set(member_ids)
    candidates = tuple(
        contract
        for _, contract in sorted(contracts.items())
        if contract.owning_repo_id in members
        and contract.extractable
        and contract.status is ContractStatus.EXTRACTABLE
        and contract.contract_id not in committed
        and contract.extraction_confidence >= cfg.min_extraction_confidence
    )
    if not candidates:
        return (), nodes, edges, graph, committed, (), ()

    saturated = _trial_graph(nodes, edges, candidates, cfg)
    if _live_component(saturated.G_dag, member_ids):
        # No subset of C dissolves S: hoisting cannot finish the job, so 6d owns this SCC and
        # nothing is committed. Falling through with partial hoists would spend blast radius for
        # an ordering change that still needs an edge broken.
        return (), nodes, edges, graph, committed, (), ()

    hoisted: list[str] = []
    findings: list[GraphFinding] = []
    rejected: list[ContractNode] = []
    remaining = list(candidates)
    live = _live_component(graph.G_dag, member_ids)
    while live and remaining and len(hoisted) < cfg.max_hoists_per_scc:
        scored: list[tuple[int, float, int, str, ContractNode]] = []
        for contract in remaining:
            trial = _trial_graph(nodes, edges, (contract,), cfg)
            owner = str(contract.owning_repo_id)
            freed = len(live) - _component_size(trial.G_dag, ("REPO", owner))
            owner_ref: NodeRef = ("REPO", owner)
            dependents = (
                descendants(graph.G_rev, owner_ref) if graph.G_rev.has_node(owner_ref) else ()
            )
            radius = len(contract.consumer_repo_ids) + len(dependents)
            scored.append(
                (-freed, -contract.extraction_confidence, radius, contract.contract_id, contract)
            )
        scored.sort(key=lambda row: row[:4])
        chosen = scored[0][4]

        materialized = _materialize(edges, (chosen,))
        post_retarget_consumers = {
            str(e.src_id)
            for e in materialized
            if e.dst_kind is NodeKind.CONTRACT
            and e.dst_id == chosen.contract_id
            and e.kind is EdgeKind.CONTRACT_CONSUME
        }
        if len(post_retarget_consumers) < min_consumers:
            # Reject, in memory, exactly: `edges` is never reassigned to `materialized`, so it
            # stays the pre-`_materialize` snapshot — no restore step is needed because nothing
            # was committed. `nodes`/`committed`/`hoisted`/`graph`/`live` are equally untouched,
            # so the SCC the caller sees is still cyclic (the guard at `_resolve_scc`'s
            # "the SCC dissolved with no hoist committed" cannot trip: `graph` here is identical
            # to what the caller already rebuilt `live` from).
            rejected.append(
                chosen.model_copy(
                    update={
                        "status": ContractStatus.REJECTED,
                        "status_detail": "not_shared_after_retarget",
                    }
                )
            )
            findings.append(
                GraphFinding(
                    kind="ContractNotShared",
                    severity="warn",
                    repo_id=chosen.owning_repo_id,
                    payload={
                        "contract_id": chosen.contract_id,
                        "declared_consumers": str(len(chosen.consumer_repo_ids)),
                        "post_retarget_consumers": str(len(post_retarget_consumers)),
                        "min_consumers": str(min_consumers),
                    },
                )
            )
            remaining = [c for c in remaining if c.contract_id != chosen.contract_id]
            continue

        edges = materialized
        nodes[chosen.node.key] = chosen.node
        committed[chosen.contract_id] = chosen.model_copy(
            update={"status": ContractStatus.HOISTED}
        )
        hoisted.append(chosen.contract_id)
        remaining = [c for c in remaining if c.contract_id != chosen.contract_id]
        graph = _rebuild(nodes.values(), edges, cfg)
        live = _live_component(graph.G_dag, member_ids)

    return tuple(hoisted), nodes, edges, graph, committed, tuple(findings), tuple(rejected)


# =======================================================================================
# graph plumbing
# =======================================================================================


def _rebuild(
    nodes: Iterable[GraphNode], edges: Sequence[DependencyEdge], cfg: GraphSection
) -> FleetGraph:
    return build_graph(
        nodes,
        edges,
        dag_edge_kinds=cfg.dag_edge_kinds,
        min_confidence=cfg.min_confidence,
        hoist_contracts=cfg.hoist_contracts,
    )


def _trial_graph(
    nodes: Mapping[NodeRef, GraphNode],
    edges: Sequence[DependencyEdge],
    contracts: Sequence[ContractNode],
    cfg: GraphSection,
) -> FleetGraph:
    """One in-memory trial: `G` with `contracts` materialized. Never mutates the committed state."""
    trial_nodes = [*nodes.values(), *(c.node for c in contracts)]
    return _rebuild(trial_nodes, _materialize(edges, contracts), cfg)


def _materialize(
    edges: Sequence[DependencyEdge], contracts: Sequence[ContractNode]
) -> list[DependencyEdge]:
    """Apply §3.1 5b (viii)'s candidate retargets and add the 5b (vii) contract edges.

    A repo→repo row whose `evidence_path` belongs to the contract's source-or-generated set is
    retargeted to the contract node, preserving the original owner in `retargeted_from_repo_id` —
    the column that makes an un-hoist exact rather than a re-inference. A consumer whose
    dependency on the owner is *entirely* contract evidence is genuinely freed; one that also
    calls the owner's implementation keeps those rows and stays ordered behind it.
    """
    if not contracts:
        return list(edges)
    owned: dict[str, str] = {}
    paths: dict[tuple[str, str], str] = {}
    for contract in sorted(contracts, key=lambda c: c.contract_id):
        owner = contract.owning_repo_id
        if owner is None:
            continue
        owned[contract.contract_id] = owner
        for entry in [*contract.source_paths, *contract.generated_paths]:
            repo_id, path = entry.get("repo_id"), entry.get("path")
            if repo_id and path and repo_id != owner:
                paths.setdefault((repo_id, path), contract.contract_id)

    out: dict[str, DependencyEdge] = {}
    for edge in edges:
        target = None
        if (
            edge.src_kind is NodeKind.REPO
            and edge.dst_kind is NodeKind.REPO
            and edge.dst_id is not None
        ):
            target = paths.get((str(edge.src_id), edge.evidence_path))
        if target is None:
            out[edge.edge_key] = edge
            continue
        out_edge = edge.model_copy(
            update={
                "edge_key": edge_key_for(
                    src_kind=NodeKind.REPO.value,
                    src_id=str(edge.src_id),
                    dst_kind=NodeKind.CONTRACT.value,
                    dst_ref=target,
                    kind=EdgeKind.CONTRACT_CONSUME.value,
                    evidence_path=edge.evidence_path,
                    evidence_line=edge.evidence_line,
                ),
                "dst_kind": NodeKind.CONTRACT,
                "dst_id": target,
                "dst_coordinate": None,
                "kind": EdgeKind.CONTRACT_CONSUME,
                "retargeted_from_repo_id": edge.dst_id,
            }
        )
        out[out_edge.edge_key] = out_edge
    for contract_edge in infer_contract_edges(contracts):
        out.setdefault(contract_edge.edge_key, contract_edge)
    return [out[key] for key in sorted(out)]


def _suppress(edges: Sequence[DependencyEdge], edge_key: str) -> list[DependencyEdge]:
    """§3.1 6d: broken edges are **never deleted**, only marked `ordering_suppressed`, so the
    evidence survives into `fleet status --format dot` and the PR body's "dependencies this
    migration deliberately did not order on"."""
    return [
        edge.model_copy(update={"ordering_suppressed": True}) if edge.edge_key == edge_key else edge
        for edge in edges
    ]


def _nontrivial_sccs(dag: nx.DiGraph) -> tuple[tuple[NodeRef, ...], ...]:
    components: list[tuple[NodeRef, ...]] = [
        tuple(sorted(component))
        for component in nx.strongly_connected_components(dag)
        if len(component) > 1
    ]
    return tuple(sorted(components))


def _live_component(dag: nx.DiGraph, member_ids: Sequence[str]) -> tuple[NodeRef, ...]:
    """The largest non-trivial component still holding ≥2 of `member_ids`, or `()` when the SCC
    is dissolved. `()` — not a raise — is what "the SCC is trivial now" has to look like: the
    success path of a trial hoist is exactly the one where the owner sits in no non-trivial
    component at all."""
    members = set(member_ids)
    best: tuple[NodeRef, ...] = ()
    best_score: tuple[int, tuple[NodeRef, ...]] | None = None
    for component in _nontrivial_sccs(dag):
        shared = sum(1 for kind, node_id in component if kind == "REPO" and node_id in members)
        if shared < 2:
            continue
        score = (-shared, component)
        if best_score is None or score < best_score:
            best_score, best = score, component
    return best


def _component_size(dag: nx.DiGraph, node: NodeRef) -> int:
    """`len(S_k)`: the size of the non-trivial component containing `node`, **1 when there is
    none**. This is the definition that must not raise — a `KeyError`/`max()` on an empty
    sequence here would blow up on precisely the trial that dissolved the SCC."""
    if not dag.has_node(node):
        return 1
    for component in _nontrivial_sccs(dag):
        if node in component:
            return len(component)
    return 1


def _intra_edge_keys(dag: nx.DiGraph, members: Iterable[NodeRef]) -> tuple[str, ...]:
    inside = frozenset(members)
    keys: set[str] = set()
    for src in sorted(inside):
        for dst in sorted(v for v in dag.successors(src) if v in inside):
            keys.update(dag[src][dst]["edge_keys"])
    return tuple(sorted(keys))


def _feedback_edge_keys(dag: nx.DiGraph, members: Iterable[NodeRef]) -> tuple[str, ...]:
    classes = classify_intra_scc_edges(dag, members)
    keys: set[str] = set()
    for (src, dst), kind in classes.items():
        if kind == "BACK":
            keys.update(dag[src][dst]["edge_keys"])
    return tuple(sorted(keys))


def _cheapest(edge_keys: Sequence[str], graph: FleetGraph) -> str | None:
    if not edge_keys:
        return None
    return min(edge_keys, key=lambda key: break_cost(graph.edges_by_key[key], graph))


# =======================================================================================
# 6e — ATOMIC_WAVE target coarsening
# =======================================================================================


def scc_target_name(scc_id: str) -> str:
    """A Bazel target name for an `scc_id`. `:` is a label separator and cannot appear in a
    target name, so the `scc:` prefix is folded to `scc_` — §3.3's `str(scc_id)` predates the
    `SccId` type becoming `'scc:<16hex>'` (see SPEC_GAPS)."""
    return scc_id.replace(":", "_")


def scc_dest(ecosystem: Ecosystem, scc_id: str, *, monorepo_dir: str) -> str:
    """§3.3: `Path(monorepo_dir) / "_scc" / <scc_id>`, keyed by the **pair** — the label must be
    unique per `(ecosystem, scc_id)`, not per SCC, e.g. `java/_scc/…` and `ts/_scc/…`."""
    return f"{monorepo_dir.rstrip('/')}/{RESERVED_SCC_SEGMENT}/{scc_target_name(scc_id)}"


@dataclass(frozen=True, slots=True)
class MemberSources:
    """One `ATOMIC_WAVE` member's already-relocated sources, per ecosystem.

    A dataclass rather than a `BuildUnit` because step 6 must not depend on the Phase 3 adapter
    registry: the *rule* an ecosystem emits is `EcosystemAdapter`'s business (§7.5), the *shape*
    of the coarsening is §3.1 6e's.
    """

    repo_id: str
    ecosystem: Ecosystem
    dest: str
    srcs: tuple[str, ...] = ()

    def monorepo_srcs(self) -> tuple[str, ...]:
        base = self.dest.rstrip("/")
        return tuple(sorted(f"{base}/{src}" for src in self.srcs))


@dataclass(frozen=True, slots=True)
class CoarseTarget:
    """The ONE library target an `(ecosystem, scc_id)` pair emits. `srcs` reach into the members'
    own `layout(repo)` destinations, which is where their history actually lands."""

    scc_id: str
    ecosystem: Ecosystem
    package: str
    name: str
    srcs: tuple[str, ...]
    member_repo_ids: tuple[str, ...]

    @property
    def label(self) -> str:
        return f"//{self.package}:{self.name}"


@dataclass(frozen=True, slots=True)
class CoarsePlan:
    targets: tuple[CoarseTarget, ...]
    standalone_repo_ids: tuple[str, ...]
    merged_repo_ids: tuple[str, ...]
    findings: tuple[GraphFinding, ...]


def coarsen_atomic_scc(
    resolution: SccResolution,
    members: Sequence[MemberSources],
    graph: FleetGraph,
    *,
    monorepo_dirs: Mapping[Ecosystem, str],
) -> CoarsePlan:
    """§3.1 6e: **one** library target per `(ecosystem, scc_id)`, at `//<scc_dest>:<scc_id>`.

    6e is reached precisely because the members are mutually dependent, so per-member targets
    *cannot* be acyclic: emitting one target per member reproduces the SCC in the Bazel target
    graph, `bazel build` fails with a dependency-cycle error, and every repo in the SCC's
    dependent cone is stranded behind a build that can never go green. A per-member target
    survives only for a member with **no intra-SCC inbound edge** — nothing inside the SCC depends
    on it, so it cannot participate in the cycle. Every other member is merged and records one
    `CoarseTarget` finding.
    """
    member_ids = frozenset(resolution.members)
    refs = {("REPO", repo_id) for repo_id in member_ids}
    standalone: set[str] = set()
    for repo_id in sorted(member_ids):
        ref: NodeRef = ("REPO", repo_id)
        if not graph.G_dag.has_node(ref):
            standalone.add(repo_id)
            continue
        inbound = [p for p in graph.G_dag.predecessors(ref) if p in refs]
        if not inbound:
            standalone.add(repo_id)

    merged = sorted(member_ids - standalone)
    by_ecosystem: dict[Ecosystem, list[MemberSources]] = {}
    for member in sorted(members, key=lambda m: (m.ecosystem.value, m.repo_id)):
        if member.repo_id in merged:
            by_ecosystem.setdefault(member.ecosystem, []).append(member)

    targets: list[CoarseTarget] = []
    for ecosystem in sorted(by_ecosystem, key=lambda e: e.value):
        group = by_ecosystem[ecosystem]
        monorepo_dir = monorepo_dirs.get(ecosystem, ecosystem.value)
        srcs: set[str] = set()
        for member in group:
            srcs.update(member.monorepo_srcs())
        targets.append(
            CoarseTarget(
                scc_id=resolution.scc_id,
                ecosystem=ecosystem,
                package=scc_dest(ecosystem, resolution.scc_id, monorepo_dir=monorepo_dir),
                name=scc_target_name(resolution.scc_id),
                srcs=tuple(sorted(srcs)),
                member_repo_ids=tuple(sorted(m.repo_id for m in group)),
            )
        )

    findings = tuple(
        GraphFinding(
            kind="CoarseTarget",
            severity="warn",
            repo_id=repo_id,
            payload={
                "scc_id": resolution.scc_id,
                "reason": "merged into the SCC's single library target; an intra-SCC inbound "
                "edge makes a per-member target cyclic in Bazel (§3.1 6e)",
            },
        )
        for repo_id in merged
    )
    return CoarsePlan(
        targets=tuple(targets),
        standalone_repo_ids=tuple(sorted(standalone)),
        merged_repo_ids=tuple(merged),
        findings=findings,
    )
