"""Longest-path wave layering and the §3.1 success criteria (step 7, criteria (a)–(d)).

**Layering runs on `G_rev`, and the direction is the whole point.** `edges` rows are directed
dependent → dependency (§3.1 step 5), so `G_rev` runs dependency → dependent and
`wave_index(n) = 0` if `n` has no internal dependencies, else `1 + max(wave_index(deps))` puts
*dependencies first*. In a chain `A → B → C` — A requires B requires C — C is wave 0, B is wave 1
and A is wave 2. A mirror-image implementation is still a DAG, still layered, still covers every
node, and still passes every count-based criterion, while migrating the fleet leaves-first and
failing the build in every wave. `tests/test_graph_sequence.py` asserts the asymmetry directly.

Two rules from the review are enforced here rather than described:

* **Acyclicity is checked on the graph AFTER condensing every `ATOMIC_WAVE` and `MANUAL` SCC into
  a single node** (`condense_for_ordering`, `check_criterion_b`). An atomic wave deliberately
  breaks no edge, so requiring the raw ordering subgraph to be acyclic is unsatisfiable for any
  fleet containing one genuine implementation cycle. `MANUAL` members are excluded from the
  ordering subgraph *entirely*: they are `REQUIRES_HUMAN_INTERVENTION` and are never migrated.
* **A preflight-gated repo is never assigned a wave**, and criterion (c) counts
  `repos WHERE status NOT IN ('SKIPPED','REQUIRES_HUMAN_INTERVENTION')` against
  `wave_members WHERE node_kind = 'REPO'`, with every absent repo required to match **exactly
  one** of the five exemptions §3.1 (c) enumerates. Read the rule there — `check_criterion_c`
  and `_exemptions_for` index it, and deliberately do not restate it. Restating it here is what
  produced the drift this module was corrected for: the docstrings quoted a two-kind criterion
  that (c) had already widened, and the narrower copy is the one the code implemented.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

import networkx as nx  # type: ignore[import-untyped]  # no py.typed; see HONEST_GAPS

from fleet.graph.build import FleetGraph, NodeRef
from fleet.graph.cycles import CycleReport, SccResolution
from fleet.models.enums import (
    BreakStrategy,
    ContractStatus,
    FailureClass,
    NodeKind,
    RepoStatus,
)
from fleet.models.graph import CycleFinding, DependencyEdge, MigrationWave

__all__ = [
    "GATED_STATUSES",
    "PREFLIGHT_FINDING_KINDS",
    "CriteriaReport",
    "CriterionResult",
    "OrderNode",
    "WavePlan",
    "append_synthetic_waves",
    "assign_waves",
    "check_criteria",
    "check_criterion_a",
    "check_criterion_b",
    "check_criterion_c",
    "check_criterion_d",
    "condense_for_ordering",
    "layer",
    "ordering_is_acyclic",
]

type OrderNode = tuple[str, str]
"""A node of the condensation: `("REPO"|"CONTRACT", node_id)` or `("SCC", scc_id)`. A tuple of
two strings, so the layering's tie-breaks bottom out in a total order."""

SCC_NODE_KIND: Final = "SCC"

GATED_STATUSES: Final[frozenset[RepoStatus]] = frozenset(
    {RepoStatus.SKIPPED, RepoStatus.REQUIRES_HUMAN_INTERVENTION}
)
"""§3.1 criterion (c): the two statuses the coverage count subtracts. Both are terminal, and a
repo gated into `REQUIRES_HUMAN_INTERVENTION` by preflight is **never** assigned a wave."""

PREFLIGHT_FINDING_KINDS: Final[frozenset[str]] = frozenset(
    {"PreflightFailed", "EmptyRepo", "BaselineRed", "OperatorQuarantine"}
)
"""The `findings.kind` values named by §3.1 criterion (c)'s exemption list — a lookup table for
the rule, not the rule. A kind here is never an exemption on its own: (c) pairs each with the
status (and, for `BaselineRed`, the `repos.baseline_ok = 0`) that justifies it, and
`_exemptions_for` is where those pairs live. (c)'s other two exemptions — config-`skip` and
`MANUAL`-SCC membership — are justified by status, so no kind of theirs appears here."""


# =======================================================================================
# condensation
# =======================================================================================


def condense_for_ordering(
    graph: FleetGraph,
    resolutions: Sequence[SccResolution] = (),
    *,
    excluded_node_ids: Collection[str] = (),
    condense: bool = True,
) -> nx.DiGraph:
    """The ordering subgraph with every `ATOMIC_WAVE` (and `MANUAL`) SCC collapsed to one node.

    Orientation is preserved — dependent → dependency, exactly `G_dag` — because the caller
    reverses it once, in `layer`. `condense=False` reproduces the pre-review criterion, which was
    unsatisfiable for any fleet holding a genuine implementation cycle; it exists so a test can
    assert that the *old* check fails where the current one passes.

    `MANUAL` members are dropped outright rather than condensed: §3.1 (b) excludes them from the
    ordering subgraph entirely, since they are `REQUIRES_HUMAN_INTERVENTION` and never migrate.
    """
    manual = {
        member
        for res in resolutions
        if res.break_strategy is BreakStrategy.MANUAL
        for member in res.members
    }
    dropped = manual | set(excluded_node_ids)
    mapping: dict[NodeRef, OrderNode] = {}
    if condense:
        for res in sorted(resolutions, key=lambda r: r.scc_id):
            if res.break_strategy is BreakStrategy.ATOMIC_WAVE:
                for member in res.members:
                    mapping[(NodeKind.REPO.value, member)] = (SCC_NODE_KIND, res.scc_id)

    out: nx.DiGraph = nx.DiGraph()
    for ref in sorted(graph.G_dag.nodes):
        if ref[1] in dropped:
            continue
        out.add_node(mapping.get(ref, ref))
    for src, dst in sorted(graph.G_dag.edges):
        if src[1] in dropped or dst[1] in dropped:
            continue
        u, v = mapping.get(src, src), mapping.get(dst, dst)
        if u != v:
            out.add_edge(u, v)
    return out


def ordering_is_acyclic(condensed: nx.DiGraph) -> bool:
    acyclic: bool = nx.is_directed_acyclic_graph(condensed)
    return acyclic


def layer(condensed: nx.DiGraph) -> dict[OrderNode, int]:
    """`wave_index(n) = 0` if `n` has no internal dependencies, else `1 + max(wave_index(deps))`.

    Computed on `condensed.reverse()` — the `G_rev` orientation — so a *dependency* is a
    predecessor and every node lands strictly after everything it needs. Flipping this is the one
    change that keeps every test that counts nodes green while inverting the migration.
    """
    reverse: nx.DiGraph = condensed.reverse(copy=True)
    waves: dict[OrderNode, int] = {}
    for node in nx.lexicographical_topological_sort(reverse, key=lambda n: n):
        deps = [waves[p] for p in reverse.predecessors(node)]
        waves[node] = 1 + max(deps) if deps else 0
    return waves


# =======================================================================================
# wave assignment
# =======================================================================================


@dataclass(frozen=True, slots=True)
class WavePlan:
    """Step 7's output: `waves` + `wave_members`, plus the `CycleFinding`s that could only be
    completed once the atomic waves had an index."""

    waves: tuple[MigrationWave, ...]
    wave_index_by_node: Mapping[NodeRef, int]
    cycle_findings: tuple[CycleFinding, ...]
    excluded_repo_ids: tuple[str, ...]

    @property
    def repo_wave_index(self) -> Mapping[str, int]:
        return {
            node_id: index
            for (kind, node_id), index in sorted(self.wave_index_by_node.items())
            if kind == NodeKind.REPO.value
        }

    @property
    def repo_members(self) -> tuple[str, ...]:
        return tuple(sorted(self.repo_wave_index))

    @property
    def contract_members(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                node_id
                for kind, node_id in self.wave_index_by_node
                if kind == NodeKind.CONTRACT.value
            )
        )

    def wave(self, index: int) -> MigrationWave:
        for wave in self.waves:
            if wave.wave_index == index:
                return wave
        raise KeyError(index)


def assign_waves(
    report: CycleReport,
    *,
    gated_repo_ids: Collection[str] = (),
) -> WavePlan:
    """§3.1 step 7 — longest-path layering on the condensation of the ordering subgraph.

    `gated_repo_ids` are the repos preflight (or config) already removed from the fleet: they are
    dropped from the ordering subgraph before layering, because "a repo gated into
    `REQUIRES_HUMAN_INTERVENTION` by preflight is **never** assigned a wave". `MANUAL` SCC members
    are dropped on the same principle without the caller having to say so.
    """
    excluded = sorted({*gated_repo_ids, *report.manual_repo_ids})
    condensed = condense_for_ordering(
        report.graph, report.resolutions, excluded_node_ids=excluded
    )
    if not ordering_is_acyclic(condensed):  # pragma: no cover - step 6 guarantees this
        cycle = sorted(next(iter(nx.simple_cycles(condensed))))
        raise ValueError(
            f"ordering condensation is still cyclic through {cycle}; step 6 left an SCC neither "
            "broken nor condensed (§3.1 criterion (b))"
        )

    layered = layer(condensed)
    scc_members = {
        res.scc_id: res.members
        for res in report.resolutions
        if res.break_strategy is BreakStrategy.ATOMIC_WAVE
    }

    index_by_node: dict[NodeRef, int] = {}
    scc_by_wave: dict[int, set[str]] = {}
    for node, wave_index in sorted(layered.items()):
        kind, node_id = node
        if kind == SCC_NODE_KIND:
            scc_by_wave.setdefault(wave_index, set()).add(node_id)
            for member in scc_members[node_id]:
                index_by_node[(NodeKind.REPO.value, member)] = wave_index
        else:
            index_by_node[(kind, node_id)] = wave_index

    depends: dict[int, set[int]] = {}
    for node, wave_index in layered.items():
        for dependency in condensed.successors(node):
            depends.setdefault(wave_index, set()).add(layered[dependency])

    waves = _materialize_waves(index_by_node, scc_by_wave, depends)
    findings = tuple(
        res.to_finding(
            atomic_wave_index=(
                index_by_node.get((NodeKind.REPO.value, res.members[0]))
                if res.break_strategy is BreakStrategy.ATOMIC_WAVE
                else None
            )
        )
        for res in sorted(report.resolutions, key=lambda r: r.scc_id)
    )
    return WavePlan(
        waves=waves,
        wave_index_by_node=dict(sorted(index_by_node.items())),
        cycle_findings=findings,
        excluded_repo_ids=tuple(excluded),
    )


def _materialize_waves(
    index_by_node: Mapping[NodeRef, int],
    scc_by_wave: Mapping[int, Collection[str]],
    depends: Mapping[int, Collection[int]],
    *,
    synthetic: bool = False,
) -> tuple[MigrationWave, ...]:
    repos: dict[int, list[str]] = {}
    contracts: dict[int, list[str]] = {}
    for (kind, node_id), wave_index in sorted(index_by_node.items()):
        bucket = repos if kind == NodeKind.REPO.value else contracts
        bucket.setdefault(wave_index, []).append(node_id)
    return tuple(
        MigrationWave(
            wave_index=index,
            repo_ids=sorted(repos.get(index, ())),
            contract_ids=sorted(contracts.get(index, ())),
            depends_on_waves=sorted(set(depends.get(index, ())) - {index}),
            atomic_scc_ids=sorted(scc_by_wave.get(index, ())),
            synthetic=synthetic,
        )
        for index in sorted(set(repos) | set(contracts))
    )


def append_synthetic_waves(
    plan: WavePlan, graph: FleetGraph, node_refs: Sequence[NodeRef]
) -> WavePlan:
    """§3.5: repos freed by a late resolution are **appended** at `wave_index = max(waves) + 1`.

    A closed wave is never re-opened — a repo that has already migrated cannot learn about a new
    peer — so the freed set gets its own layer(s), internally ordered by the same longest-path
    rule ("across four synthetic waves if needed"). `waves.synthetic = 1` is what lets the
    projection say *why* a repo migrated outside its original layer.
    """
    fresh = [ref for ref in sorted(set(node_refs)) if ref not in plan.wave_index_by_node]
    if not fresh:
        return plan
    offset = max((wave.wave_index for wave in plan.waves), default=-1) + 1
    induced: nx.DiGraph = graph.G_dag.subgraph(fresh).copy()
    layered = {node: offset + index for node, index in layer(induced).items()}

    index_by_node = {**plan.wave_index_by_node, **layered}
    depends: dict[int, set[int]] = {}
    for node, wave_index in layered.items():
        for dependency in graph.G_dag.successors(node):
            prior = index_by_node.get(dependency)
            if prior is not None and prior != wave_index:
                depends.setdefault(wave_index, set()).add(prior)
    appended = _materialize_waves(layered, {}, depends, synthetic=True)
    return WavePlan(
        waves=(*plan.waves, *appended),
        wave_index_by_node=dict(sorted(index_by_node.items())),
        cycle_findings=plan.cycle_findings,
        excluded_repo_ids=plan.excluded_repo_ids,
    )


# =======================================================================================
# §3.1 success criteria (a)–(d)
# =======================================================================================


@dataclass(frozen=True, slots=True)
class CriterionResult:
    name: Literal["a", "b", "c", "d"]
    ok: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class CriteriaReport:
    results: tuple[CriterionResult, ...]

    @property
    def ok(self) -> bool:
        return all(result.ok for result in self.results)

    @property
    def failures(self) -> tuple[CriterionResult, ...]:
        return tuple(result for result in self.results if not result.ok)

    def result(self, name: str) -> CriterionResult:
        for result in self.results:
            if result.name == name:
                return result
        raise KeyError(name)


def check_criterion_a(
    repo_ids: Collection[str],
    *,
    repos_with_manifests: Collection[str],
    no_manifest_repo_ids: Collection[str],
) -> CriterionResult:
    """(a) every repo has ≥1 `manifests` row **or** a `no-manifest` row in `findings`."""
    covered = set(repos_with_manifests) | set(no_manifest_repo_ids)
    missing = sorted(set(repo_ids) - covered)
    return CriterionResult(
        name="a",
        ok=not missing,
        detail="" if not missing else f"no manifest and no 'no-manifest' finding: {missing}",
    )


def check_criterion_b(
    report: CycleReport,
    *,
    plan: WavePlan | None = None,
    statuses: Mapping[str, RepoStatus] | None = None,
    condense: bool = True,
) -> CriterionResult:
    """(b) the ordering subgraph, **after condensing every `ATOMIC_WAVE` and `MANUAL` SCC into a
    single node**, is acyclic — *and* every non-trivial SCC of the raw graph carries a
    substantiated `CycleFinding`.

    `condense=False` is the pre-review check, kept callable so a test can prove the difference:
    an atomic wave breaks no edge, so the raw subgraph of a fleet holding one genuine
    implementation cycle is cyclic by construction and the old criterion could never be met.
    """
    condensed = condense_for_ordering(report.graph, report.resolutions, condense=condense)
    problems: list[str] = []
    if not ordering_is_acyclic(condensed):
        cycles = sorted(sorted(c) for c in nx.simple_cycles(condensed))[:3]
        shape = "" if condense else " (uncondensed)"
        problems.append(f"ordering subgraph is cyclic{shape}: {cycles}")

    manual_members = {
        member
        for res in report.resolutions
        if res.break_strategy is BreakStrategy.MANUAL
        for member in res.members
    }
    resolved: dict[frozenset[str], SccResolution] = {
        frozenset(res.members): res for res in report.resolutions
    }
    for component in nx.strongly_connected_components(report.graph.G_dag):
        members = frozenset(
            node_id
            for kind, node_id in component
            if kind == NodeKind.REPO.value and node_id not in manual_members
        )
        if len(members) < 2:
            continue
        if members not in resolved:
            problems.append(f"non-trivial SCC {sorted(members)} carries no CycleFinding")

    for res in report.resolutions:
        problems.extend(_substantiation_problems(res, plan=plan, statuses=statuses))

    return CriterionResult(name="b", ok=not problems, detail="; ".join(problems))


def _substantiation_problems(
    res: SccResolution,
    *,
    plan: WavePlan | None,
    statuses: Mapping[str, RepoStatus] | None,
) -> list[str]:
    problems: list[str] = []
    if res.break_strategy is BreakStrategy.CONTRACT_HOIST and not res.hoisted_contract_ids:
        problems.append(f"{res.scc_id}: CONTRACT_HOIST with no hoisted contracts")
    if res.break_strategy is BreakStrategy.EDGE_BREAK and not res.broken_edge_keys:
        problems.append(f"{res.scc_id}: EDGE_BREAK with no broken edges")
    if res.break_strategy is BreakStrategy.ATOMIC_WAVE and plan is not None:
        indices = {plan.repo_wave_index.get(member) for member in res.members}
        if len(indices) != 1 or None in indices:
            spread = sorted(str(i) for i in indices)
            problems.append(f"{res.scc_id}: ATOMIC_WAVE members span wave indices {spread}")
    if res.break_strategy is BreakStrategy.MANUAL and statuses is not None:
        wrong = sorted(
            member
            for member in res.members
            if statuses.get(member) is not RepoStatus.REQUIRES_HUMAN_INTERVENTION
        )
        if wrong:
            problems.append(
                f"{res.scc_id}: MANUAL members not REQUIRES_HUMAN_INTERVENTION: {wrong}"
            )
    return problems


def _exemptions_for(
    repo_id: str,
    *,
    status: RepoStatus,
    kinds: Collection[str],
    exempt_kinds: Collection[str],
    config_skipped: Collection[str],
    baseline_ok: Mapping[str, int | None],
    failure_class: FailureClass | None,
    manual_members: Collection[str],
) -> tuple[str, ...]:
    """Every §3.1 (c) exemption `repo_id` matches, in (c)'s order — usually one, and (c) requires
    exactly one.

    Each entry pairs a status with the finding (or config fact) (c) names beside it, because a
    bare finding kind is evidence of the wrong thing: an `EmptyRepo` row on a `PENDING` repo does
    not explain why that repo has no wave. Two sub-conditions (c) states are not checkable from
    this module's inputs and are therefore the caller's to enforce upstream: `PreflightFailed` at
    `severity='error'`, and the reason `fleet quarantine` requires on `OperatorQuarantine`.
    """

    def finding(kind: str) -> bool:
        return kind in exempt_kinds and kind in kinds

    skipped = status is RepoStatus.SKIPPED
    abandoned = status is RepoStatus.REQUIRES_HUMAN_INTERVENTION
    matched: list[str] = []
    if skipped and repo_id in config_skipped:
        matched.append("config-skipped")
    if skipped and finding("BaselineRed") and baseline_ok.get(repo_id) == 0:
        matched.append("baseline-red")
    if skipped and finding("OperatorQuarantine"):
        matched.append("operator-quarantined")
    if (abandoned and finding("PreflightFailed")) or (skipped and finding("EmptyRepo")):
        matched.append("preflight-gated")
    if abandoned and failure_class is FailureClass.CYCLE and repo_id in manual_members:
        matched.append("manual-scc-member")
    return tuple(matched)


def check_criterion_c(
    plan: WavePlan,
    *,
    statuses: Mapping[str, RepoStatus],
    finding_kinds: Mapping[str, Collection[str]] = {},
    contract_statuses: Mapping[str, ContractStatus] = {},
    exempt_finding_kinds: Collection[str] = PREFLIGHT_FINDING_KINDS,
    config_skipped_repo_ids: Collection[str] = (),
    baseline_ok: Mapping[str, int | None] = {},
    failure_classes: Mapping[str, FailureClass] = {},
) -> CriterionResult:
    """(c) the topological order covers 100% of nodes — the criterion as §3.1 (c) states it.

    Three clauses: the ungated-repo count equals the `REPO` `wave_members` count; every repo
    absent from `wave_members` matches **exactly one** of (c)'s five closed exemptions
    (`_exemptions_for`); and the `HOISTED`/`MIGRATED` contract count equals the `CONTRACT` member
    count. The exemption set is closed, so an absent repo explained by nothing is a failure —
    that is the whole point of the clause, and widening the set is not the same as removing it.

    Facts this module cannot read are injected rather than assumed: `config_skipped_repo_ids` is
    (c)'s config-`skip` exemption, whose record is the `config/repos.yaml` entry and *not* a
    `findings` row; `baseline_ok` and `failure_classes` are the `repos` columns (c) names.
    `MANUAL`-SCC membership is read off `plan.cycle_findings` — the `CycleDetected` payloads —
    so that exemption is substantiated by the finding, never by a repo's name.
    """
    expected = sorted(
        repo_id for repo_id, status in statuses.items() if status not in GATED_STATUSES
    )
    members = set(plan.repo_members)
    problems: list[str] = []
    if len(expected) != len(members):
        problems.append(
            f"{len(expected)} ungated repos vs {len(members)} REPO wave members; "
            f"missing={sorted(set(expected) - members)} extra={sorted(members - set(expected))}"
        )
    manual_members = {
        member
        for finding in plan.cycle_findings
        if finding.break_strategy is BreakStrategy.MANUAL
        for member in finding.members
    }
    for repo_id in sorted(set(statuses) - members):
        matched = _exemptions_for(
            repo_id,
            status=statuses[repo_id],
            kinds=finding_kinds.get(repo_id, ()),
            exempt_kinds=exempt_finding_kinds,
            config_skipped=config_skipped_repo_ids,
            baseline_ok=baseline_ok,
            failure_class=failure_classes.get(repo_id),
            manual_members=manual_members,
        )
        if not matched:
            problems.append(
                f"{repo_id} (status={statuses[repo_id].value}) is absent from wave_members and "
                "matches none of §3.1 (c)'s five exemptions"
            )
        elif len(matched) > 1:
            problems.append(
                f"{repo_id} is absent from wave_members matching {len(matched)} §3.1 (c) "
                f"exemptions ({', '.join(matched)}); (c) requires exactly one"
            )
    hoisted = sorted(
        contract_id
        for contract_id, status in contract_statuses.items()
        if status in (ContractStatus.HOISTED, ContractStatus.MIGRATED)
    )
    if len(hoisted) != len(plan.contract_members):
        problems.append(
            f"{len(hoisted)} HOISTED/MIGRATED contracts vs "
            f"{len(plan.contract_members)} CONTRACT wave members"
        )
    return CriterionResult(name="c", ok=not problems, detail="; ".join(problems))


def check_criterion_d(
    edges: Iterable[DependencyEdge], *, evidence_exists: Callable[[str, str], bool]
) -> CriterionResult:
    """(d) no edge exists whose `evidence_path` does not resolve to a real file at `head_sha`.

    The probe is injected — this module owns no worktree and reads no file — so the caller joins
    against the `ls-tree` listing preflight already captured (§3.1 step 1) rather than stat()ing
    250 checkouts a second time.
    """
    dangling = sorted(
        {
            (str(edge.src_id), edge.evidence_path)
            for edge in edges
            if not evidence_exists(str(edge.src_id), edge.evidence_path)
        }
    )
    return CriterionResult(
        name="d",
        ok=not dangling,
        detail="" if not dangling else f"evidence_path does not resolve: {dangling[:5]}",
    )


def check_criteria(
    report: CycleReport,
    plan: WavePlan,
    *,
    statuses: Mapping[str, RepoStatus],
    repos_with_manifests: Collection[str] = (),
    no_manifest_repo_ids: Collection[str] = (),
    finding_kinds: Mapping[str, Collection[str]] = {},
    contract_statuses: Mapping[str, ContractStatus] = {},
    config_skipped_repo_ids: Collection[str] = (),
    baseline_ok: Mapping[str, int | None] = {},
    failure_classes: Mapping[str, FailureClass] = {},
    evidence_exists: Callable[[str, str], bool] | None = None,
) -> CriteriaReport:
    """All four machine-checkable criteria of §3.1 in one pass, in spec order."""
    results = [
        check_criterion_a(
            sorted(statuses),
            repos_with_manifests=repos_with_manifests,
            no_manifest_repo_ids=no_manifest_repo_ids,
        ),
        check_criterion_b(report, plan=plan, statuses=statuses),
        check_criterion_c(
            plan,
            statuses=statuses,
            finding_kinds=finding_kinds,
            contract_statuses=contract_statuses,
            config_skipped_repo_ids=config_skipped_repo_ids,
            baseline_ok=baseline_ok,
            failure_classes=failure_classes,
        ),
        check_criterion_d(
            report.edges, evidence_exists=evidence_exists or (lambda _repo, _path: True)
        ),
    ]
    return CriteriaReport(results=tuple(results))
