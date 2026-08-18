"""Behaviour tests for `src/fleet/orchestrator/stubs.py` — the §3.5.1 stub state machine.

Four regressions are pinned here, and every one of them ships a lie rather than an error.

**Superseding on `SUCCEEDED` alone.** T1 needs *both* halves — provider `RepoStatus.SUCCEEDED`
**and** its `PullRequestDraft.state == 'MERGED'` (ADR-0011 stacking). A machine that fires on the
status alone rewrites the consumer's dependency to a `//` label that is not on the integration
branch yet, so `test_t1_does_not_fire_on_succeeded_without_merged_pr` is the negative case that
matters more than the positive one.

**Promoting on exhaustion.** §13 row 35: consumers stay `DEGRADED`, and there is no config value
that promotes them. Both exhaustion paths (rounds, budget) and the whole reconciliation sweep are
asserted to leave `consumer_status` at `DEGRADED`, because the failure mode is a green report
over work verified against nothing.

**Abandoning a stub the fleet is merely waiting on.** §13 row 45: under the shipped
`pr.draft: true` a provider's PR sits open until a human merges it. A `stub_reconcile` that
sweeps those rows turns exit 7 from "a human is needed" into "the fleet gave up waiting".

**Resurrecting a terminal row.** `RESOLVED`/`ABANDONED` are terminal and the audit trail is
append-only: a re-emission is a NEW row at the next `revalidation_round`, never a revived one.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from fleet.models.enums import (
    Equivalence,
    FailureClass,
    PrState,
    RepoStatus,
    StubFidelity,
    StubState,
)
from fleet.models.tasks import StubRecord, VerificationReport
from fleet.orchestrator.budgets import RevalidationBudgetExhausted
from fleet.orchestrator.stubs import (
    AbandonReason,
    InvalidStubTransition,
    ProviderFacts,
    RevalidationPolicy,
    StubFinding,
    StubTransition,
    abandon_by_operator,
    apply,
    next_round_record,
    plan_revalidation,
    reconcile,
    revalidation_key,
    settle_revalidation,
    supersede,
)

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
RUN_ID = uuid4()


def _stub(
    *,
    coord: str = "com.acme:widget",
    provider: str = "acme-widget",
    consumer: str = "acme-app",
    state: StubState = StubState.ACTIVE,
    rounds_spent: int = 0,
    max_rounds: int = 2,
) -> StubRecord:
    return StubRecord(
        run_id=RUN_ID,
        coord_key=coord,
        provider_repo_id=provider,
        consumer_repo_ids=[consumer],
        fidelity=StubFidelity.PUBLISHED_ARTIFACT,
        pinned_version="1.4.2",
        state=state,
        max_revalidation_rounds=max_rounds,
        rounds_spent=rounds_spent,
    )


def _report(
    *,
    consumer: str = "acme-app",
    verdict: str = "PASS",
    stubs: list[str] | None = None,
    round_index: int = 1,
) -> VerificationReport:
    named = stubs or []
    return VerificationReport(
        run_id=RUN_ID,
        repo_id=consumer,
        build_ok=verdict == "PASS",
        test_ok=verdict == "PASS",
        rdeps_ok=verdict == "PASS",
        verdict=verdict,  # type: ignore[arg-type]
        verified_against_stubs=named,
        stub_fidelity=dict.fromkeys(named, StubFidelity.PUBLISHED_ARTIFACT),
        revalidation_round=round_index,
    )


# --------------------------------------------------------------------------------------
# T1 — ACTIVE -> SUPERSEDED
# --------------------------------------------------------------------------------------


def test_t1_fires_on_succeeded_and_merged() -> None:
    stub = _stub()
    decision = supersede(
        stub, ProviderFacts("acme-widget", RepoStatus.SUCCEEDED, PrState.MERGED)
    )
    assert decision is not None
    assert decision.transition is StubTransition.T1
    assert (decision.from_state, decision.to_state) == (StubState.ACTIVE, StubState.SUPERSEDED)
    # SUPERSEDED is "not yet proven": the consumer is NOT promoted by the label swap.
    assert decision.consumer_status is RepoStatus.DEGRADED
    assert decision.round_index == 1
    assert apply(stub, decision, now=NOW).state is StubState.SUPERSEDED


@pytest.mark.parametrize(
    "pr_state",
    [None, PrState.DRAFTED, PrState.OPEN, PrState.HELD, PrState.CLOSED],
)
def test_t1_does_not_fire_on_succeeded_without_merged_pr(pr_state: PrState | None) -> None:
    """The negative case (ADR-0011): the real label must exist on the integration branch first.

    A machine that supersedes on `RepoStatus.SUCCEEDED` alone points the consumer at a label
    nothing has merged, and the failure surfaces as an unresolved-label build error attributed to
    the consumer rather than to the scheduler that jumped the gun.
    """
    decision = supersede(
        _stub(), ProviderFacts("acme-widget", RepoStatus.SUCCEEDED, pr_state)
    )
    assert decision is None


def test_t1_does_not_fire_on_merged_pr_without_succeeded() -> None:
    decision = supersede(
        _stub(), ProviderFacts("acme-widget", RepoStatus.DEGRADED, PrState.MERGED)
    )
    assert decision is None


def test_t1_under_manual_policy_needs_the_operator() -> None:
    """`stubs.revalidation: manual` — `fleet stubs resolve` is the ONLY trigger (§3.5.1)."""
    provider = ProviderFacts("acme-widget", RepoStatus.SUCCEEDED, PrState.MERGED)
    assert supersede(_stub(), provider, policy=RevalidationPolicy.MANUAL) is None
    operator = supersede(
        _stub(), provider, policy=RevalidationPolicy.MANUAL, operator_triggered=True
    )
    assert operator is not None and operator.transition is StubTransition.T1


def test_t1_replay_is_a_no_op_and_a_terminal_row_raises() -> None:
    """§3.5.1 step 4: a replayed trigger dispatches nothing new; a terminal row is a caller bug."""
    provider = ProviderFacts("acme-widget", RepoStatus.SUCCEEDED, PrState.MERGED)
    assert supersede(_stub(state=StubState.SUPERSEDED), provider) is None
    for terminal in (StubState.RESOLVED, StubState.ABANDONED):
        with pytest.raises(InvalidStubTransition, match="terminal"):
            supersede(_stub(state=terminal), provider)


def test_batched_coalesces_one_consumer_into_one_keyed_round() -> None:
    """§13 row 34, the storm bound: three late fixes of one consumer are ONE round under
    `batched` and three under `eager` — and the batched key hashes all three providers, which is
    what makes the coalescing identity explicit (and the replay idempotent)."""
    provider_ids = ["p-one", "p-two", "p-three"]
    decisions = []
    for pid in provider_ids:
        d = supersede(
            _stub(coord=f"com.acme:{pid}", provider=pid),
            ProviderFacts(pid, RepoStatus.SUCCEEDED, PrState.MERGED),
        )
        assert d is not None
        decisions.append(d)

    batched = plan_revalidation("acme-app", decisions)
    assert len(batched) == 1
    assert batched[0].provider_repo_ids == tuple(sorted(provider_ids))
    assert batched[0].key == revalidation_key(1, provider_ids)
    # Order and multiplicity of the provider set do not change the key.
    assert batched[0].key == revalidation_key(1, [*reversed(provider_ids), "p-one"])

    eager = plan_revalidation("acme-app", decisions, policy=RevalidationPolicy.EAGER)
    assert len(eager) == 3
    assert len({p.key for p in eager}) == 3

    assert plan_revalidation("acme-app", decisions, policy=RevalidationPolicy.MANUAL) == ()


# --------------------------------------------------------------------------------------
# T2 — SUPERSEDED -> RESOLVED
# --------------------------------------------------------------------------------------


def test_t2_fires_on_pass_with_no_stub_in_the_report() -> None:
    stub = _stub(state=StubState.SUPERSEDED, rounds_spent=1)
    decision = settle_revalidation(stub, _report())
    assert decision is not None
    assert decision.transition is StubTransition.T2
    assert decision.to_state is StubState.RESOLVED
    assert decision.consumer_status is RepoStatus.SUCCEEDED  # last stub of this consumer
    assert decision.abandon_reason is None
    assert apply(stub, decision, now=NOW).state is StubState.RESOLVED


def test_t2_does_not_fire_on_a_pass_that_still_names_a_stub() -> None:
    """Both halves are required: a PASS naming a stub is `STUB_LIMITED`, which proves only that a
    *different* lie still holds. The report's own validator makes that visible."""
    report = _report(stubs=["com.acme:other"])
    assert report.equivalence is Equivalence.STUB_LIMITED
    stub = _stub(state=StubState.SUPERSEDED, rounds_spent=1)
    assert settle_revalidation(stub, report) is None  # round 1 of 2: another round remains


def test_t2_leaves_the_consumer_degraded_while_a_sibling_stub_is_open() -> None:
    """`RepoState._stub_invariants`: promotion out of DEGRADED clears the projection, and it
    clears only once EVERY StubRecord for the repo is RESOLVED."""
    stub = _stub(state=StubState.SUPERSEDED, rounds_spent=1)
    decision = settle_revalidation(
        stub, _report(), sibling_states={"com.acme:other": StubState.ACTIVE}
    )
    assert decision is not None
    assert decision.transition is StubTransition.T2
    assert decision.consumer_status is RepoStatus.DEGRADED


# --------------------------------------------------------------------------------------
# T3 — SUPERSEDED -> ABANDONED
# --------------------------------------------------------------------------------------


def test_t3_on_stub_diverged_sends_the_consumer_to_a_human() -> None:
    """§13 row 33: a genuine source incompatibility is not a retry candidate — it leaves the
    ADR-0014 ladder entirely rather than burning three more rungs on a version skew."""
    stub = _stub(state=StubState.SUPERSEDED, rounds_spent=1)
    decision = settle_revalidation(
        stub,
        _report(verdict="FAIL"),
        failure_class=FailureClass.STUB_DIVERGED,
    )
    assert decision is not None
    assert decision.transition is StubTransition.T3
    assert decision.abandon_reason is AbandonReason.STUB_DIVERGED
    assert decision.finding is StubFinding.STUB_ROT
    assert decision.consumer_status is RepoStatus.REQUIRES_HUMAN_INTERVENTION


def test_t3_on_rounds_exhausted_holds_the_consumer_degraded() -> None:
    stub = _stub(state=StubState.SUPERSEDED, rounds_spent=2, max_rounds=2)
    decision = settle_revalidation(stub, _report(verdict="FAIL", round_index=2))
    assert decision is not None
    assert decision.transition is StubTransition.T3
    assert decision.abandon_reason is AbandonReason.ROUNDS_EXHAUSTED
    assert decision.consumer_status is RepoStatus.DEGRADED  # never SUCCEEDED (§13 row 35)


def test_t3_on_budget_exhaustion_reuses_the_ledger_breach_and_never_promotes() -> None:
    """The breach is the instance `orchestrator/budgets.py` raised — one ledger, one type. The
    finding's name is derived from that class, so a rename cannot leave the report naming a type
    that no longer exists."""
    stub = _stub(state=StubState.SUPERSEDED, rounds_spent=1)
    breach = RevalidationBudgetExhausted("acme-app has spent $2.00 of $2.00")
    decision = settle_revalidation(stub, _report(verdict="FAIL"), budget_breach=breach)
    assert decision is not None
    assert decision.transition is StubTransition.T3
    assert decision.abandon_reason is AbandonReason.BUDGET_EXHAUSTED
    assert decision.finding.value == RevalidationBudgetExhausted.__name__
    assert decision.consumer_status is RepoStatus.DEGRADED
    assert not decision.promotes_consumer


def test_a_budget_breach_outranks_a_pass() -> None:
    """Precedence is worst-first: an exhausted sub-ceiling is fail-closed, so a round that
    happened to go green after the ledger refused is still not a promotion."""
    stub = _stub(state=StubState.SUPERSEDED, rounds_spent=1)
    decision = settle_revalidation(
        stub, _report(), budget_breach=RevalidationBudgetExhausted("out of budget")
    )
    assert decision is not None
    assert decision.transition is StubTransition.T3


def test_a_failed_round_with_rounds_left_does_not_transition() -> None:
    stub = _stub(state=StubState.SUPERSEDED, rounds_spent=1, max_rounds=2)
    assert settle_revalidation(stub, _report(verdict="FAIL", round_index=1)) is None


def test_settle_refuses_a_row_that_is_not_superseded() -> None:
    with pytest.raises(InvalidStubTransition, match="SUPERSEDED"):
        settle_revalidation(_stub(), _report())


# --------------------------------------------------------------------------------------
# T4 — ACTIVE -> ABANDONED, and the reconciliation sweep
# --------------------------------------------------------------------------------------


def test_reconcile_abandons_open_rows_via_t4_and_t3_and_never_promotes() -> None:
    """§3.5.1 step 1 / §13 row 35: ACTIVE goes via T4, SUPERSEDED via T3, both with
    `abandon_reason='END_OF_RUN'` and an `UnresolvedStub` finding, and the consumers stay
    DEGRADED — there is no argument to this function that could promote them."""
    rows = [
        _stub(coord="com.acme:a", provider="p-a"),
        _stub(coord="com.acme:b", provider="p-b", state=StubState.SUPERSEDED, rounds_spent=1),
        _stub(coord="com.acme:done", provider="p-c", state=StubState.RESOLVED),
    ]
    outcome = reconcile(
        rows,
        {
            "p-a": ProviderFacts("p-a", RepoStatus.REQUIRES_HUMAN_INTERVENTION, None),
            "p-b": ProviderFacts("p-b", RepoStatus.SUCCEEDED, PrState.CLOSED),
        },
    )
    by_coord = {d.coord_key: d for d in outcome.decisions}
    assert set(by_coord) == {"com.acme:a", "com.acme:b"}  # the RESOLVED row is an audit record
    assert by_coord["com.acme:a"].transition is StubTransition.T4
    assert by_coord["com.acme:b"].transition is StubTransition.T3
    for decision in outcome.decisions:
        assert decision.to_state is StubState.ABANDONED
        assert decision.abandon_reason is AbandonReason.END_OF_RUN
        assert decision.finding is StubFinding.UNRESOLVED_STUB
        assert decision.consumer_status is RepoStatus.DEGRADED
    assert outcome.degraded_consumers == ("acme-app",)
    assert outcome.held_for_merge == ()


@pytest.mark.parametrize("pr_state", [PrState.DRAFTED, PrState.OPEN, PrState.HELD])
def test_reconcile_does_not_abandon_a_stub_whose_provider_has_an_open_pr(
    pr_state: PrState,
) -> None:
    """§13 row 45 — the carve-out that keeps exit 7 meaning "a human is needed".

    Under the shipped `pr.draft: true`, wave-0 PRs are never `MERGED` until a human merges them.
    Sweeping those rows would report a run that was politely waiting as a run that failed.
    """
    rows = [_stub(coord="com.acme:a", provider="p-a")]
    outcome = reconcile(rows, {"p-a": ProviderFacts("p-a", RepoStatus.SUCCEEDED, pr_state)})
    assert outcome.decisions == ()
    assert outcome.held_for_merge == ("com.acme:a",)
    assert outcome.degraded_consumers == ()


def test_operator_abandon_is_t4_from_active_and_t3_from_superseded() -> None:
    active = abandon_by_operator(_stub(), "provider will not be migrated this quarter")
    assert active.transition is StubTransition.T4
    assert active.abandon_reason is AbandonReason.OPERATOR
    assert active.consumer_status is RepoStatus.DEGRADED

    superseded = abandon_by_operator(
        _stub(state=StubState.SUPERSEDED, rounds_spent=1), "give up on this one"
    )
    assert superseded.transition is StubTransition.T3

    with pytest.raises(InvalidStubTransition, match="reason"):
        abandon_by_operator(_stub(), "   ")


# --------------------------------------------------------------------------------------
# the append-only audit trail
# --------------------------------------------------------------------------------------


def test_a_re_emitted_stub_is_a_new_row_at_the_next_round() -> None:
    """§3.5.1: never a resurrected terminal row. The `stubs` primary key carries
    `revalidation_round` precisely so the new fact cannot overwrite the old one."""
    abandoned = _stub(state=StubState.ABANDONED, rounds_spent=1, max_rounds=2)
    fresh = next_round_record(abandoned, now=NOW)
    assert fresh.stub_id != abandoned.stub_id
    assert fresh.state is StubState.ACTIVE
    assert fresh.rounds_spent == 2
    assert abandoned.state is StubState.ABANDONED  # the old row is untouched

    with pytest.raises(InvalidStubTransition, match="terminal"):
        next_round_record(_stub(), now=NOW)
    with pytest.raises(InvalidStubTransition, match="exhausted"):
        next_round_record(fresh.model_copy(update={"state": StubState.ABANDONED}), now=NOW)


def test_apply_refuses_a_transition_the_machine_does_not_have() -> None:
    stub = _stub()
    decision = settle_revalidation(
        _stub(state=StubState.SUPERSEDED, rounds_spent=1), _report()
    )
    assert decision is not None
    with pytest.raises(InvalidStubTransition, match="T2 runs"):
        apply(stub, decision, now=NOW)  # T2 off an ACTIVE row
