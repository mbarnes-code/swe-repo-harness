"""Behaviour tests for `src/fleet/graph/{sequence,collisions}.py` — §3.1 steps 7 and 8.

**The direction of layering is the single most load-bearing fact here, and it is asserted
asymmetrically on purpose.** In a chain `A → B → C` — A requires B requires C — C must be in an
*earlier* wave than B, and B earlier than A, because a wave may only start once everything it
depends on has already migrated. Layering therefore runs on `G_rev` (dependency → dependent), and
`wave_index(n) = 1 + max(wave_index(deps))`. A mirror-image implementation is still a DAG, still
layered, still covers 100% of nodes, and still passes every count-based criterion — while
migrating the fleet leaves-first and failing the build in every wave. If
`test_a_dependency_migrates_in_an_earlier_wave_than_its_dependent` fails, fix the layering; never
flip the expectation.

Two further tests exist because the criteria themselves were wrong before review:

* `test_criterion_b_passes_for_an_atomic_wave_fleet_only_after_condensation` asserts BOTH halves:
  the criterion passes with condensation *and* fails without it. An atomic wave deliberately
  breaks no edge, so the pre-review check — acyclicity of the raw ordering subgraph — was
  unsatisfiable for any fleet containing one genuine implementation cycle.
* `test_criterion_c_counts_only_ungated_repos` pins the accounting with a `SKIPPED` and a
  `REQUIRES_HUMAN_INTERVENTION` repo present. A gated repo is never assigned a wave, so a
  coverage check that counted every row would have failed every real fleet.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import pytest

from fleet.graph.build import build_graph
from fleet.graph.collisions import (
    CollisionInput,
    ContractClaim,
    CoordinateClaim,
    DestClaim,
    FileClaim,
    VersionRequirement,
    audit_collisions,
)
from fleet.graph.cycles import CycleReport, break_cycles
from fleet.graph.sequence import (
    WavePlan,
    append_synthetic_waves,
    assign_waves,
    check_criteria,
    check_criterion_a,
    check_criterion_b,
    check_criterion_c,
    check_criterion_d,
    condense_for_ordering,
    layer,
    ordering_is_acyclic,
)
from fleet.models.enums import (
    BreakStrategy,
    ContractStatus,
    Ecosystem,
    EdgeKind,
    FailureClass,
    NodeKind,
    RepoStatus,
)
from fleet.settings import GraphSection
from tests.test_graph_cycles import (
    K1,
    K2,
    edge,
    implementation_cycle,
    multi_chord_fleet,
    nodes,
)

LIB = "maven:com.acme:lib"


def chain_report() -> CycleReport:
    """`acme-a → acme-b → acme-c`: A requires B requires C. The smallest graph in which "depends
    on" and "is depended on by" have different answers."""
    graph = build_graph(
        nodes("acme-a", "acme-b", "acme-c"),
        [edge("acme-a", "acme-b"), edge("acme-b", "acme-c")],
    )
    return break_cycles(graph)


# =======================================================================================
# (1) layering direction
# =======================================================================================


def test_a_dependency_migrates_in_an_earlier_wave_than_its_dependent() -> None:
    """A requires B requires C ⇒ wave(C) < wave(B) < wave(A).

    C has no internal dependencies, so it is wave 0 and migrates first; B can only be migrated
    once C exists in the monorepo; A only once B does. §3.1 step 7 computes this as longest-path
    layering on `G_rev`, and §3.1 step 5 fixes `G_rev` as dependency → dependent. Reversing it
    would migrate the leaves first and break every build that consumes them — while every count
    in this file stays green, which is exactly why the assertion is written as an inequality on
    three distinct values rather than a membership check.
    """
    plan = assign_waves(chain_report())
    waves = plan.repo_wave_index

    assert waves["acme-c"] == 0
    assert waves["acme-c"] < waves["acme-b"] < waves["acme-a"]
    assert plan.wave(0).repo_ids == ["acme-c"]
    assert plan.wave(2).repo_ids == ["acme-a"]
    assert plan.wave(2).depends_on_waves == [1]


def test_a_hoisted_contract_migrates_before_the_repos_that_consume_it() -> None:
    """§3.1 step 7: "A contract node has no inbound dependency on any repo, so longest-path
    layering on `G_rev` puts it at wave 0 … A contract migrates *first*, as its own early wave."
    """
    graph_nodes, edges, contracts = multi_chord_fleet()
    report = break_cycles(build_graph(graph_nodes, edges), contracts=contracts)
    plan = assign_waves(report)

    assert plan.wave(0).contract_ids == [K1, K2]
    assert plan.wave(0).repo_ids == [], "the first wave is contract-only, which is the point"
    assert set(plan.contract_members) == {K1, K2}
    # Once both contracts are hoisted the only surviving repo→repo edge is `acme-b → acme-a`
    # (b requires a's implementation), so a migrates before b — and both after the contracts.
    assert plan.repo_wave_index["acme-a"] < plan.repo_wave_index["acme-b"]
    assert min(plan.repo_wave_index.values()) > 0


def test_wave_zero_is_the_nodes_with_no_internal_dependencies() -> None:
    """`wave_index(n) = 0` if `n` has no internal dependencies (§3.1 step 7)."""
    condensed = condense_for_ordering(
        build_graph(nodes("acme-a", "acme-b"), [edge("acme-a", "acme-b")])
    )
    assert layer(condensed) == {("REPO", "acme-b"): 0, ("REPO", "acme-a"): 1}


# =======================================================================================
# (2) criterion (b) — acyclicity AFTER condensation
# =======================================================================================


def test_criterion_b_passes_for_an_atomic_wave_fleet_only_after_condensation() -> None:
    """(b) holds for a fleet containing an `ATOMIC_WAVE` SCC — **and** fails without condensing.

    §3.1 (b): "an atomic wave deliberately breaks no edge, so requiring the raw subgraph to be
    acyclic would fail any fleet containing one genuine implementation cycle". The second half of
    this test is what makes the first half meaningful: if `condense=False` also passed, the fixture
    would not contain a real cycle and the criterion would be testing nothing.
    """
    report = implementation_cycle()
    plan = assign_waves(report)

    passed = check_criterion_b(report, plan=plan)
    assert passed.ok, passed.detail

    old = check_criterion_b(report, plan=plan, condense=False)
    assert not old.ok, "the pre-review criterion must fail here — the raw subgraph IS cyclic"
    assert "cyclic" in old.detail


def test_atomic_wave_members_all_share_one_wave_index() -> None:
    """§3.1 6e: "every member is assigned the **same** `wave_index`, and the scheduler treats the
    SCC as one admission unit". The SCC occupies one condensation node, so it occupies one wave.
    """
    report = implementation_cycle()
    plan = assign_waves(report)
    res = report.resolutions[0]

    indices = {plan.repo_wave_index[member] for member in res.members}
    assert len(indices) == 1, f"atomic members spread across waves {sorted(indices)}"
    wave = plan.wave(indices.pop())
    assert wave.atomic_scc_ids == [res.scc_id]
    assert sorted(wave.repo_ids) == list(res.members)

    finding = next(f for f in plan.cycle_findings if f.scc_id == res.scc_id)
    assert finding.break_strategy is BreakStrategy.ATOMIC_WAVE
    assert finding.atomic_wave_index == wave.wave_index
    # acme-d depends on the SCC, so it must land strictly after it.
    assert plan.repo_wave_index["acme-d"] > wave.wave_index


def test_manual_members_are_absent_from_the_ordering_subgraph() -> None:
    """§3.1 (b): "Members of a `MANUAL` SCC are excluded from the ordering subgraph entirely,
    since they are `REQUIRES_HUMAN_INTERVENTION` and are never migrated by the fleet." """
    graph = build_graph(
        nodes("acme-a", "acme-b", "acme-c", "acme-d"),
        [
            edge("acme-a", "acme-b"),
            edge("acme-b", "acme-c"),
            edge("acme-c", "acme-a"),
            edge("acme-d", "acme-a"),
        ],
    )
    report = break_cycles(graph, config=GraphSection(scc_hard_max=2))
    assert report.resolutions[0].break_strategy is BreakStrategy.MANUAL

    condensed = condense_for_ordering(report.graph, report.resolutions)
    assert sorted(condensed.nodes) == [("REPO", "acme-d")]
    assert ordering_is_acyclic(condensed)

    plan = assign_waves(report)
    assert plan.repo_members == ("acme-d",), "a MANUAL member is never assigned a wave"
    assert plan.excluded_repo_ids == ("acme-a", "acme-b", "acme-c")

    statuses = {
        "acme-a": RepoStatus.REQUIRES_HUMAN_INTERVENTION,
        "acme-b": RepoStatus.REQUIRES_HUMAN_INTERVENTION,
        "acme-c": RepoStatus.REQUIRES_HUMAN_INTERVENTION,
        "acme-d": RepoStatus.PENDING,
    }
    assert check_criterion_b(report, plan=plan, statuses=statuses).ok


def test_criterion_b_rejects_a_manual_scc_whose_members_were_not_abandoned() -> None:
    """(b) requires `MANUAL` with all members `REQUIRES_HUMAN_INTERVENTION` — the strategy is a
    claim about the repos' status, not a label."""
    report = break_cycles(
        build_graph(
            nodes("acme-a", "acme-b", "acme-c"),
            [edge("acme-a", "acme-b"), edge("acme-b", "acme-c"), edge("acme-c", "acme-a")],
        ),
        config=GraphSection(scc_hard_max=2),
    )
    result = check_criterion_b(
        report, statuses=dict.fromkeys(report.manual_repo_ids, RepoStatus.PENDING)
    )
    assert not result.ok
    assert "REQUIRES_HUMAN_INTERVENTION" in result.detail


# =======================================================================================
# (3) criterion (c) — coverage accounting
# =======================================================================================


def test_criterion_c_counts_only_ungated_repos() -> None:
    """(c) `COUNT(repos WHERE status NOT IN ('SKIPPED','REQUIRES_HUMAN_INTERVENTION'))` equals
    `COUNT(wave_members WHERE node_kind='REPO')`, every absent repo matches one of §3.1 (c)'s
    exemptions (here the preflight-gated one, in both of its halves), and the `HOISTED`/`MIGRATED`
    contract count equals the `CONTRACT` member count.

    Both gated statuses are present here on purpose: a preflight-gated repo is never assigned a
    wave, so a check that counted every `repos` row could never pass on a real fleet, and a check
    that silently dropped absentees would let a repo vanish from the migration unrecorded.
    """
    graph_nodes, edges, contracts = multi_chord_fleet()
    report = break_cycles(build_graph(graph_nodes, edges), contracts=contracts)
    plan = assign_waves(report, gated_repo_ids=["acme-skip", "acme-gated"])

    statuses = {
        "acme-a": RepoStatus.PENDING,
        "acme-b": RepoStatus.PENDING,
        "acme-skip": RepoStatus.SKIPPED,
        "acme-gated": RepoStatus.REQUIRES_HUMAN_INTERVENTION,
    }
    finding_kinds = {"acme-skip": ["EmptyRepo"], "acme-gated": ["PreflightFailed"]}
    contract_statuses = {K1: ContractStatus.HOISTED, K2: ContractStatus.HOISTED}

    result = check_criterion_c(
        plan,
        statuses=statuses,
        finding_kinds=finding_kinds,
        contract_statuses=contract_statuses,
    )
    assert result.ok, result.detail
    assert plan.repo_members == ("acme-a", "acme-b")

    # Strip the finding and the same fleet fails: an absent repo must always be explained.
    unexplained = check_criterion_c(
        plan,
        statuses=statuses,
        finding_kinds={"acme-gated": ["PreflightFailed"]},
        contract_statuses=contract_statuses,
    )
    assert not unexplained.ok
    assert "acme-skip" in unexplained.detail


def gated_fleet() -> WavePlan:
    """`acme-a → acme-b` plus `acme-gated`: a fleet member that reaches no wave.

    One absent repo and a balanced count, so the *only* thing that can fail the criterion is the
    exemption clause — which is what these cases are about.
    """
    graph = build_graph(nodes("acme-a", "acme-b", "acme-gated"), [edge("acme-a", "acme-b")])
    return assign_waves(break_cycles(graph), gated_repo_ids=["acme-gated"])


def manual_scc_fleet() -> WavePlan:
    """A 3-repo implementation cycle over `scc_hard_max` — resolved `MANUAL` — plus `acme-d`."""
    graph = build_graph(
        nodes("acme-a", "acme-b", "acme-c", "acme-d"),
        [
            edge("acme-a", "acme-b"),
            edge("acme-b", "acme-c"),
            edge("acme-c", "acme-a"),
            edge("acme-d", "acme-a"),
        ],
    )
    return assign_waves(break_cycles(graph, config=GraphSection(scc_hard_max=2)))


@dataclass(frozen=True, slots=True)
class ExemptionCase:
    """One §3.1 (c) exemption: a fleet, the repo missing from its waves, and *only* the facts
    that exemption names as the justification. Dropping them (`stripped`) must fail the
    criterion — an exemption that passes without its evidence is not an exemption."""

    exemption: str
    plan: Callable[[], WavePlan]
    absent: str
    statuses: dict[str, RepoStatus]
    finding_kinds: dict[str, list[str]] = field(default_factory=dict)
    config_skipped: list[str] = field(default_factory=list)
    baseline_ok: dict[str, int | None] = field(default_factory=dict)
    failure_classes: dict[str, FailureClass] = field(default_factory=dict)


def _gated(status: RepoStatus) -> dict[str, RepoStatus]:
    return {"acme-a": RepoStatus.PENDING, "acme-b": RepoStatus.PENDING, "acme-gated": status}


MANUAL_STATUSES: dict[str, RepoStatus] = {
    "acme-a": RepoStatus.REQUIRES_HUMAN_INTERVENTION,
    "acme-b": RepoStatus.REQUIRES_HUMAN_INTERVENTION,
    "acme-c": RepoStatus.REQUIRES_HUMAN_INTERVENTION,
    "acme-d": RepoStatus.PENDING,
}
MANUAL_CYCLE_CLASSES: dict[str, FailureClass] = dict.fromkeys(
    ("acme-a", "acme-b", "acme-c"), FailureClass.CYCLE
)

EXEMPTION_CASES: list[ExemptionCase] = [
    ExemptionCase(
        exemption="config-skipped",
        plan=gated_fleet,
        absent="acme-gated",
        statuses=_gated(RepoStatus.SKIPPED),
        config_skipped=["acme-gated"],
    ),
    ExemptionCase(
        exemption="baseline-red",
        plan=gated_fleet,
        absent="acme-gated",
        statuses=_gated(RepoStatus.SKIPPED),
        finding_kinds={"acme-gated": ["BaselineRed"]},
        baseline_ok={"acme-gated": 0},
    ),
    ExemptionCase(
        exemption="operator-quarantined",
        plan=gated_fleet,
        absent="acme-gated",
        statuses=_gated(RepoStatus.SKIPPED),
        finding_kinds={"acme-gated": ["OperatorQuarantine"]},
    ),
    ExemptionCase(
        exemption="preflight-failed",
        plan=gated_fleet,
        absent="acme-gated",
        statuses=_gated(RepoStatus.REQUIRES_HUMAN_INTERVENTION),
        finding_kinds={"acme-gated": ["PreflightFailed"]},
    ),
    ExemptionCase(
        exemption="empty-repo",
        plan=gated_fleet,
        absent="acme-gated",
        statuses=_gated(RepoStatus.SKIPPED),
        finding_kinds={"acme-gated": ["EmptyRepo"]},
    ),
    ExemptionCase(
        exemption="manual-scc-member",
        plan=manual_scc_fleet,
        absent="acme-a",
        statuses=MANUAL_STATUSES,
        failure_classes=MANUAL_CYCLE_CLASSES,
    ),
]


@pytest.mark.parametrize("case", EXEMPTION_CASES, ids=lambda c: c.exemption)
def test_each_criterion_c_exemption_excuses_an_absent_repo_on_its_own(
    case: ExemptionCase,
) -> None:
    """§3.1 (c) enumerates five exemptions (preflight-gated has two halves, `PreflightFailed` and
    `EmptyRepo`), each named with the status or finding that justifies it. Each is asserted alone,
    with no other exemption's facts supplied, so a single over-broad match cannot cover for a
    missing one.
    """
    plan = case.plan()
    assert case.absent not in plan.repo_members, "the fixture must actually gate the repo"

    result = check_criterion_c(
        plan,
        statuses=case.statuses,
        finding_kinds=case.finding_kinds,
        config_skipped_repo_ids=case.config_skipped,
        baseline_ok=case.baseline_ok,
        failure_classes=case.failure_classes,
    )
    assert result.ok, result.detail


@pytest.mark.parametrize("case", EXEMPTION_CASES, ids=lambda c: c.exemption)
def test_criterion_c_rejects_every_exemption_stripped_of_its_justification(
    case: ExemptionCase,
) -> None:
    """The same six fleets with the justifying finding/config/failure-class removed and only the
    status left. The status alone is never the exemption: `SKIPPED` and
    `REQUIRES_HUMAN_INTERVENTION` are exactly the states an unexplained drop also lands in.
    """
    result = check_criterion_c(case.plan(), statuses=case.statuses)
    assert not result.ok
    assert case.absent in result.detail


def test_an_absent_repo_with_no_justification_at_all_still_fails_criterion_c() -> None:
    """**The teeth of (c).** Widening the exemption list from two finding kinds to five closed
    exemptions must not turn the clause into a rubber stamp: a repo that is simply gone, with no
    status-or-finding justification, is still a failure.

    Asserted with the counts *balanced* — the gated repo is subtracted from the expected count by
    the `SKIPPED` status, so the arithmetic clause is silent and only the exemption clause can
    catch it. That is the whole reason (c) has a second clause at all.
    """
    plan = gated_fleet()
    result = check_criterion_c(plan, statuses=_gated(RepoStatus.SKIPPED))

    assert not result.ok
    assert "acme-gated" in result.detail
    assert "five exemptions" in result.detail
    assert "wave members" not in result.detail, "the count clause balances; only (c)'s list fails"


def test_a_manual_scc_member_is_exempt_by_its_cycle_finding_not_by_its_name() -> None:
    """(c)'s fifth exemption is `REQUIRES_HUMAN_INTERVENTION` + `FailureClass.CYCLE` + membership
    in a `CycleDetected` finding whose `break_strategy` is `MANUAL`. The finding is the authority:
    a repo carrying the identical status and failure class, but named in no `MANUAL` finding,
    is not exempt — otherwise "abandoned for a cycle" would be a self-certifying claim.
    """
    plan = manual_scc_fleet()
    members = sorted(
        member
        for finding in plan.cycle_findings
        if finding.break_strategy is BreakStrategy.MANUAL
        for member in finding.members
    )
    assert members == ["acme-a", "acme-b", "acme-c"]

    assert check_criterion_c(
        plan, statuses=MANUAL_STATUSES, failure_classes=MANUAL_CYCLE_CLASSES
    ).ok

    impostor = check_criterion_c(
        plan,
        statuses={**MANUAL_STATUSES, "acme-e": RepoStatus.REQUIRES_HUMAN_INTERVENTION},
        failure_classes={**MANUAL_CYCLE_CLASSES, "acme-e": FailureClass.CYCLE},
    )
    assert not impostor.ok
    assert "acme-e" in impostor.detail


def test_criterion_c_rejects_a_repo_matching_two_exemptions() -> None:
    """(c): every absent repo "matches **exactly one** of the five exemptions". Two justifications
    for one absence means the fleet does not actually know why the repo is gone — a config-skipped
    repo is never baseline-built — so the ambiguity is surfaced rather than averaged away.
    """
    result = check_criterion_c(
        gated_fleet(),
        statuses=_gated(RepoStatus.SKIPPED),
        finding_kinds={"acme-gated": ["BaselineRed"]},
        config_skipped_repo_ids=["acme-gated"],
        baseline_ok={"acme-gated": 0},
    )
    assert not result.ok
    assert "config-skipped, baseline-red" in result.detail
    assert "exactly one" in result.detail


def test_criterion_c_rejects_a_hoisted_contract_with_no_wave() -> None:
    """(c)'s third clause: every `HOISTED`/`MIGRATED` contract is a `wave_members` row. A hoisted
    contract that no wave builds is a node the monorepo never receives."""
    plan = assign_waves(chain_report())
    result = check_criterion_c(
        plan,
        statuses=dict.fromkeys(("acme-a", "acme-b", "acme-c"), RepoStatus.PENDING),
        contract_statuses={K1: ContractStatus.HOISTED},
    )
    assert not result.ok
    assert "CONTRACT wave members" in result.detail


def test_criteria_a_and_d_are_pure_predicates_over_injected_facts() -> None:
    """(a) a manifest row or a `no-manifest` finding for every repo; (d) no edge whose
    `evidence_path` fails to resolve at `head_sha`. Both probe facts this module does not own,
    so both take them as arguments rather than reading a worktree."""
    assert check_criterion_a(
        ["acme-a", "acme-b"], repos_with_manifests=["acme-a"], no_manifest_repo_ids=["acme-b"]
    ).ok
    assert not check_criterion_a(
        ["acme-a", "acme-b"], repos_with_manifests=["acme-a"], no_manifest_repo_ids=[]
    ).ok

    report = chain_report()
    assert check_criterion_d(report.edges, evidence_exists=lambda _r, _p: True).ok
    missing = check_criterion_d(report.edges, evidence_exists=lambda _r, _p: False)
    assert not missing.ok


def test_check_criteria_reports_all_four_in_spec_order() -> None:
    report = implementation_cycle()
    plan = assign_waves(report)
    statuses = dict.fromkeys(("acme-a", "acme-b", "acme-c", "acme-d"), RepoStatus.PENDING)
    outcome = check_criteria(
        report,
        plan,
        statuses=statuses,
        repos_with_manifests=list(statuses),
    )
    assert [r.name for r in outcome.results] == ["a", "b", "c", "d"]
    assert outcome.ok, [f.detail for f in outcome.failures]


# =======================================================================================
# (4) synthetic waves
# =======================================================================================


def test_a_freed_repo_is_appended_at_max_wave_plus_one() -> None:
    """§3.5: a repo freed by a late resolution is appended as a **synthetic wave** at
    `wave_index = max(waves) + 1`. A closed wave is never re-opened — a repo that already migrated
    cannot learn about a new peer — so the freed set gets its own layer, marked `synthetic` so the
    projection can say *why* it migrated outside its original one."""
    report = chain_report()
    plan = assign_waves(report, gated_repo_ids=["acme-a"])
    assert "acme-a" not in plan.repo_wave_index
    highest = max(w.wave_index for w in plan.waves)

    resumed = append_synthetic_waves(plan, report.graph, [("REPO", "acme-a")])
    assert resumed.repo_wave_index["acme-a"] == highest + 1
    appended = resumed.wave(highest + 1)
    assert appended.synthetic is True
    assert appended.repo_ids == ["acme-a"]
    # the earlier waves are untouched — never re-opened, never renumbered
    assert [w.wave_index for w in plan.waves] == [w.wave_index for w in resumed.waves[:-1]]


# =======================================================================================
# (5) step 8 — the collision audit
# =======================================================================================


def test_two_repos_claiming_one_destination_collide_and_the_loser_is_rewritten() -> None:
    """§3.1 step 8 `DEST_PATH`: "the repo with more inbound edges keeps it; the loser gets
    `<dest>-<repo_id>` plus a `DestPathRewritten` finding". Detected here, before any
    transformation, because a collision found in Phase 3 has already been merged into history."""
    report = audit_collisions(
        CollisionInput(
            dests=[
                DestClaim(node_id="acme-a", dest="java/com/acme/svc", inbound_edges=1),
                DestClaim(node_id="acme-b", dest="java/com/acme/svc", inbound_edges=7),
            ]
        )
    )
    collision = next(c for c in report.collisions if c.kind == "DEST_PATH")
    assert collision.key == "java/com/acme/svc"
    assert collision.repo_ids == ["acme-a", "acme-b"]
    assert collision.resolution is not None and collision.resolution.startswith("keep:acme-b")
    assert report.dest_rewrites["acme-a"] == "java/com/acme/svc-acme-a"
    assert {f.kind for f in report.findings} == {"DestPathRewritten"}
    assert report.ok, "a resolved collision never blocks the run"


def test_an_explicit_dest_override_makes_the_collision_an_error() -> None:
    """"`severity='error'` if either path came from an explicit `dest:` override — the operator
    asked for something impossible and must say what they meant." """
    report = audit_collisions(
        CollisionInput(
            dests=[
                DestClaim(node_id="acme-a", dest="java/shared", explicit=True),
                DestClaim(node_id="acme-b", dest="java/shared"),
            ]
        )
    )
    assert next(c for c in report.collisions if c.kind == "DEST_PATH").severity == "error"


def test_a_hoisted_contract_always_keeps_the_destination_path() -> None:
    """§3.1 step 8: "a **hoisted contract always keeps the path** (it is the shared artifact and
    was migrated first)" — regardless of how many inbound edges the repo has."""
    report = audit_collisions(
        CollisionInput(
            dests=[
                DestClaim(node_id="acme-a", dest="contracts/acme", inbound_edges=99),
                DestClaim(
                    node_id=K1,
                    dest="contracts/acme",
                    node_kind=NodeKind.CONTRACT,
                    hoisted_contract=True,
                ),
            ]
        )
    )
    assert report.dest_rewrites == {"acme-a": "contracts/acme-acme-a"}


def test_the_scc_namespace_is_reserved_against_every_repo() -> None:
    """§3.3: "`_scc` is reserved: the Phase 1 step-8 collision audit treats a repo `dest` under
    any `<monorepo_dir>/_scc/` as a `FILE_PATH` collision, so a real repo can never occupy the
    coarsening namespace." The `ATOMIC_WAVE` union target lives there; a repo landing on it would
    silently overwrite the one artifact holding a 40-repo cycle together.
    """
    report = audit_collisions(
        CollisionInput(dests=[DestClaim(node_id="acme-a", dest="java/_scc/tricky")])
    )
    reserved = next(f for f in report.findings if f.kind == "ReservedDestPath")
    assert reserved.severity == "error"
    assert reserved.repo_id == "acme-a"
    assert report.dest_rewrites["acme-a"] == "java/_scc/tricky-acme-a"


def test_identical_blob_shas_make_a_file_collision_a_safe_dedupe() -> None:
    """§3.1 step 8 `FILE_PATH`: byte-identical duplicates are the common case at fleet scale, and
    the blob-SHA join from step 5 makes detecting them free — the file is written once and both
    repos' BUILD targets reference it. Divergent copies under `src/` are an error."""
    same = audit_collisions(
        CollisionInput(
            files=[
                FileClaim(path="java/x/Util.java", repo_id="acme-a", blob_sha="a" * 40),
                FileClaim(path="java/x/Util.java", repo_id="acme-b", blob_sha="a" * 40),
            ]
        )
    )
    dedupe = next(c for c in same.collisions if c.kind == "FILE_PATH")
    assert dedupe.severity == "warn"
    assert dedupe.resolution is not None and dedupe.resolution.startswith("dedupe:")

    divergent = audit_collisions(
        CollisionInput(
            files=[
                FileClaim(path="src/x/Util.java", repo_id="acme-a", blob_sha="a" * 40),
                FileClaim(
                    path="src/x/Util.java", repo_id="acme-b", blob_sha="b" * 40, wave_index=2
                ),
            ]
        )
    )
    conflict = next(c for c in divergent.collisions if c.kind == "FILE_PATH")
    assert conflict.severity == "error"
    assert {f.kind for f in divergent.findings} == {"DuplicateDivergent"}


def test_a_hoisted_contract_claims_the_path_and_the_carriers_drop_it() -> None:
    """§3.1 step 8: "A path already claimed by a `HOISTED` contract is resolved in the contract's
    favour … and the carrier's relocation plan drops that path — this is how the owner's and every
    vendorer's copy of the contract stop being copied at all." """
    report = audit_collisions(
        CollisionInput(
            files=[
                FileClaim(
                    path="contracts/acme/k1.proto", repo_id="acme-b", blob_sha="a" * 40,
                    contract_id=K1, hoisted_contract=True,
                ),
                FileClaim(path="contracts/acme/k1.proto", repo_id="acme-a", blob_sha="c" * 40),
            ]
        )
    )
    collision = next(c for c in report.collisions if c.kind == "FILE_PATH")
    assert collision.resolution == f"hoisted:{K1}"
    assert report.dropped_paths["acme-a"] == ("contracts/acme/k1.proto",)


def test_divergent_contract_copies_outside_vendor_globs_are_an_error() -> None:
    """§3.1 5b (iv): `warn` when the copies are byte-identical, `error` when they are divergent
    *and* ≥2 carriers sit outside `scan.vendor_globs` — two repos independently editing the same
    published interface is a fleet fact an operator must see before anything merges."""
    identical = audit_collisions(
        CollisionInput(
            contracts=[
                ContractClaim(contract_id=K1, repo_id="acme-a", blob_sha="a" * 40),
                ContractClaim(contract_id=K1, repo_id="acme-b", blob_sha="a" * 40, owner=True),
            ]
        )
    )
    assert identical.collisions[0].severity == "warn"

    diverged = audit_collisions(
        CollisionInput(
            contracts=[
                ContractClaim(contract_id=K1, repo_id="acme-a", blob_sha="a" * 40),
                ContractClaim(contract_id=K1, repo_id="acme-b", blob_sha="b" * 40, owner=True),
            ]
        )
    )
    assert diverged.collisions[0].severity == "error"


def test_an_unsatisfiable_version_set_blocks_the_run() -> None:
    """§3.1 step 8 `DEP_VERSION`: MVS selects the minimum version satisfying **all** specs; "a
    plurality vote is never taken, because it ships a build violating a declared upper bound". A
    spec set whose intersection is empty is unsatisfiable — `severity='error'` with no resolution,
    which is exactly what makes `fleet sequence` exit non-zero."""
    satisfiable = audit_collisions(
        CollisionInput(
            versions=[
                VersionRequirement(coord_key=LIB, repo_id="acme-a", version_spec=">=1.2"),
                VersionRequirement(coord_key=LIB, repo_id="acme-b", version_spec=">=2.0"),
            ]
        )
    )
    assert satisfiable.ok
    assert satisfiable.collisions[0].severity == "warn"

    empty = audit_collisions(
        CollisionInput(
            versions=[
                VersionRequirement(coord_key=LIB, repo_id="acme-a", version_spec="<1.0"),
                VersionRequirement(coord_key=LIB, repo_id="acme-b", version_spec=">=2.0"),
            ]
        )
    )
    assert not empty.ok
    blocking = empty.blocking[0]
    assert blocking.kind == "DEP_VERSION"
    assert blocking.resolution is None


def test_a_coordinate_published_by_two_repos_is_recorded_with_its_owner() -> None:
    """§3.1 step 8 `COORDINATE`: two repos publishing one `coord_key`, resolved by step 3's
    ownership rules — the same `owns:` hint mechanism the `CONTRACT` detector uses."""
    report = audit_collisions(
        CollisionInput(
            coordinates=[
                CoordinateClaim(coord_key="maven:com.acme:lib", repo_id="acme-a",
                                ecosystem=Ecosystem.MAVEN),
                CoordinateClaim(coord_key="maven:com.acme:lib", repo_id="acme-b",
                                ecosystem=Ecosystem.MAVEN),
            ],
            owns_hints={"maven:com.acme:lib": "acme-b"},
        )
    )
    collision = report.collisions[0]
    assert collision.kind == "COORDINATE"
    assert collision.resolution == "owns-hint:acme-b"
    assert report.ok


def test_the_audit_is_empty_when_nothing_collides() -> None:
    report = audit_collisions(
        CollisionInput(
            dests=[
                DestClaim(node_id="acme-a", dest="java/a"),
                DestClaim(node_id="acme-b", dest="java/b"),
            ],
            files=[FileClaim(path="java/a/Main.java", repo_id="acme-a", blob_sha="a" * 40)],
        )
    )
    assert report.collisions == ()
    assert report.findings == ()
    assert report.ok


def test_an_advisory_edge_kind_never_orders_migration() -> None:
    """A `SHARED_RESOURCE` cycle is not a cycle for sequencing purposes: it is excluded from
    `dag_edge_kinds` (ADR-0018), so it reaches neither SCC detection nor the layering."""
    graph = build_graph(
        nodes("acme-a", "acme-b"),
        [
            edge("acme-a", "acme-b", kind=EdgeKind.SHARED_RESOURCE, confidence=0.5, path="db.sql"),
            edge("acme-b", "acme-a", kind=EdgeKind.SHARED_RESOURCE, confidence=0.5, path="db.sql"),
        ],
    )
    report = break_cycles(graph)
    assert report.resolutions == ()
    plan = assign_waves(report)
    assert set(plan.repo_wave_index.values()) == {0}, "mutually independent ⇒ one wave"
