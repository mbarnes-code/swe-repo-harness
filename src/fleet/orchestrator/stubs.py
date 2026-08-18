"""The §3.5.1 stub state machine: four states, four transitions, no model (ADR-0022).

A stub is a lie with a known shape and a known expiry. This module owns the *decision* that
retires one — and nothing else. It opens no connection, writes no SQL, and calls no backend:
every function takes the rows it needs as arguments and returns a `StubDecision` the single
writer (§11.5) persists. That split is CLAUDE.md guardrail 4 applied to the one table where a
shadow copy is most tempting: `RepoState.stub_states`, `PullRequestDraft.unresolved_stub_states`
and `VerificationReport.stub_fidelity` are all projections of `StubRecord`, and three
denormalised copies written independently disagree after a crash mid-supersede.

**No model participates** — SPEC §3.5.1: "driven entirely by deterministic code
(`orchestrator/stubs.py`); no model participates". Every trigger below is an exit code, a PR
state, or an integer counter.

**The four transitions, transcribed from the §3.5.1 table.**

- **T1** `ACTIVE → SUPERSEDED` — the provider reaches `RepoStatus.SUCCEEDED` **and** its
  `PullRequestDraft.state = 'MERGED'`; under `stubs.revalidation: manual` the trigger is instead
  the operator's `fleet stubs resolve`. → `supersede`
- **T2** `SUPERSEDED → RESOLVED` — the consumer's revalidation `VerificationReport` has
  `verdict='PASS'` **and** `verified_against_stubs == []`. → `settle_revalidation`
- **T3** `SUPERSEDED → ABANDONED` — revalidation failed with `FailureClass.STUB_DIVERGED`, or
  `revalidation_rounds` reached `stubs.max_revalidation_rounds`, or the revalidation budget was
  exhausted. → `settle_revalidation`, `reconcile`
- **T4** `ACTIVE → ABANDONED` — end-of-run reconciliation with the provider still unresolved, or
  the operator's `fleet stubs abandon`. → `reconcile`, `abandon_by_operator`

`RESOLVED` and `ABANDONED` are terminal: there is no path out of either and no path back to
`ACTIVE`. A re-run that would re-emit an `ABANDONED` stub inserts a **new row at the next
`revalidation_round`** (`next_round_record`) rather than resurrecting the old one, which is what
makes the audit trail append-only — and is why `apply` never widens a terminal state.

**Three invariants this module refuses to let a caller break.**

1. *Both halves of T1.* `RepoStatus.SUCCEEDED` alone does not supersede a stub. The real label
   must exist on the integration branch before a consumer may point at it (the ADR-0011 stacking
   rule), so the provider's PR must also be `MERGED`. A provider that succeeded with an open
   draft PR is exactly the state §13 row 45 exists to protect, and `supersede` returns `None`
   for it.
2. *Exhaustion never promotes.* `RepoStatus.SUCCEEDED` is reachable from a T2 decision and from
   nowhere else in this module, and only once **every** sibling stub of the consumer is
   `RESOLVED` (`RepoState._stub_invariants` makes the alternative unrepresentable). §13 row 35:
   consumers stay `DEGRADED`, and there is no config value that promotes them —
   `stubs.on_budget_exhausted` has the single value `"hold"` for precisely this reason.
3. *Reconciliation does not abandon a stub whose provider has an open PR* (§13 row 45). Under
   the shipped `pr.draft: true` a wave-0 PR is never `MERGED` until a human merges it, so an
   unconditional sweep would turn "the fleet is waiting on you" into "the fleet gave up", with
   exit 7 libelling a working run. The carve-out is scoped to `reconcile` only: a T3 raised by an
   exhausted round or by `STUB_DIVERGED` fires whatever the provider's PR is doing, because that
   round already ran against the merged real label.

**The revalidation budget is not re-implemented here.** `RevalidationBudgetExhausted` is raised
by `orchestrator/budgets.py`'s ledger CAS, and `settle_revalidation` *accepts the instance* as
evidence rather than re-deriving a breach from dollars — one ledger, one breach type, one place
the ceiling is enforced. The finding this module emits carries that class's own `__name__`, so a
rename cannot leave the report naming a type that no longer exists.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final
from uuid import uuid4

from fleet.models.enums import FailureClass, PrState, RepoStatus, StubState
from fleet.models.repo import RepoId
from fleet.models.tasks import StubRecord, VerificationReport
from fleet.orchestrator.budgets import RevalidationBudgetExhausted

__all__ = [
    "ALLOWED_TRANSITIONS",
    "OPEN_STATES",
    "TERMINAL_STATES",
    "AbandonReason",
    "InvalidStubTransition",
    "ProviderFacts",
    "ReconcileOutcome",
    "RevalidationPlan",
    "RevalidationPolicy",
    "StubDecision",
    "StubFinding",
    "StubTransition",
    "abandon_by_operator",
    "apply",
    "next_round_record",
    "plan_revalidation",
    "reconcile",
    "revalidation_key",
    "settle_revalidation",
    "supersede",
]


class StubTransition(StrEnum):
    """The four allowed transitions of §3.5.1, named so a decision is self-describing."""

    T1 = "T1"  # ACTIVE     -> SUPERSEDED
    T2 = "T2"  # SUPERSEDED -> RESOLVED
    T3 = "T3"  # SUPERSEDED -> ABANDONED
    T4 = "T4"  # ACTIVE     -> ABANDONED


#: `transition -> (from, to)`. The state machine's whole edge set, and the thing `apply` checks
#: against: a transition that is not in this mapping does not exist (§3.5.1 "exactly four").
ALLOWED_TRANSITIONS: Final[Mapping[StubTransition, tuple[StubState, StubState]]] = {
    StubTransition.T1: (StubState.ACTIVE, StubState.SUPERSEDED),
    StubTransition.T2: (StubState.SUPERSEDED, StubState.RESOLVED),
    StubTransition.T3: (StubState.SUPERSEDED, StubState.ABANDONED),
    StubTransition.T4: (StubState.ACTIVE, StubState.ABANDONED),
}

#: Terminal: no path out, no path back to ACTIVE. A re-emission opens a NEW row (§3.5.1).
TERMINAL_STATES: Final[frozenset[StubState]] = frozenset(
    {StubState.RESOLVED, StubState.ABANDONED}
)

#: The `ix_stubs_open` set — what `stub_reconcile` sweeps and what `fleet pr --ready` refuses on.
OPEN_STATES: Final[frozenset[StubState]] = frozenset({StubState.ACTIVE, StubState.SUPERSEDED})


class AbandonReason(StrEnum):
    """`stubs.abandon_reason`, verbatim from the schema's CHECK-adjacent comment (§6).

    The column is `NULL` iff the row is not `ABANDONED`, so a T3/T4 decision must always carry
    one of these — a reason is what turns a terminal row into an audit record.
    """

    STUB_DIVERGED = "STUB_DIVERGED"      # §13 row 33: the real target's surface moved
    ROUNDS_EXHAUSTED = "ROUNDS_EXHAUSTED"  # stubs.max_revalidation_rounds
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"  # stubs.revalidation_max_cost_usd sub-ceiling
    END_OF_RUN = "END_OF_RUN"            # §12.38: stub_reconcile swept it
    OPERATOR = "OPERATOR"                # `fleet stubs abandon`


class StubFinding(StrEnum):
    """The `findings` row a decision demands. Named exactly as §3.5.1/§13 name them, because
    `migration_state.json` and `fleet stubs list` render these strings to an operator."""

    STUB_ROT = "StubRot"
    REVALIDATION_BUDGET_EXHAUSTED = RevalidationBudgetExhausted.__name__
    UNRESOLVED_STUB = "UnresolvedStub"


class RevalidationPolicy(StrEnum):
    """`stubs.revalidation` (§9). Mirrors the settings `Literal`; a StrEnum here so the storm
    policy is a value this module can switch on without importing the settings tree."""

    EAGER = "eager"      # one revalidation per provider resolution
    BATCHED = "batched"  # default: one per consumer per wave, coalesced by revalidation_key
    MANUAL = "manual"    # nothing is enqueued; `fleet stubs resolve` is the only trigger


class InvalidStubTransition(Exception):
    """A caller asked for a transition the machine does not have (Rule 11: a typed error, never
    a silently-skipped write). Raised for a terminal row, a wrong `from` state, or a missing
    `abandon_reason` — every one of which would otherwise become a DB CHECK violation whose
    message names a column instead of the rule."""


@dataclass(frozen=True, slots=True)
class ProviderFacts:
    """The two facts T1 reads about the provider, and nothing else.

    Both halves are required (ADR-0011 stacking): a `SUCCEEDED` provider whose PR is still a
    draft has no real label on the integration branch for the consumer to point at.
    """

    repo_id: RepoId
    status: RepoStatus
    pr_state: PrState | None = None
    """`None` = no PR has been opened yet, which is not `MERGED` and therefore not a trigger."""

    @property
    def merged(self) -> bool:
        return self.status is RepoStatus.SUCCEEDED and self.pr_state is PrState.MERGED

    @property
    def pr_open(self) -> bool:
        """§13 row 45: an un-merged, un-closed PR means a human is mid-review, not that the
        provider failed. `reconcile` holds these rows instead of abandoning them."""
        return self.pr_state in (PrState.DRAFTED, PrState.OPEN, PrState.HELD)


@dataclass(frozen=True, slots=True)
class StubDecision:
    """What the single writer must do to one `stubs` row, and to its consumer.

    `consumer_status` is the status the *consumer* holds after the transition. It is
    `RepoStatus.SUCCEEDED` only on a T2 that resolved the consumer's last open stub; every other
    decision in this module leaves the consumer `DEGRADED` or sends it to a human.
    """

    coord_key: str
    consumer_repo_id: RepoId
    provider_repo_id: RepoId
    transition: StubTransition
    from_state: StubState
    to_state: StubState
    consumer_status: RepoStatus
    detail: str
    abandon_reason: AbandonReason | None = None
    finding: StubFinding | None = None
    round_index: int = 0
    """The `stubs.revalidation_round` this decision applies to — the round that just ran for
    T2/T3, the round about to be enqueued for T1, the row's own round for T4."""

    @property
    def promotes_consumer(self) -> bool:
        return self.consumer_status is RepoStatus.SUCCEEDED


@dataclass(frozen=True, slots=True)
class RevalidationPlan:
    """One `TaskKind.REVALIDATE` task to enqueue — the *plan*, not the row.

    `key` is `tasks.revalidation_key`, the idempotency key of §3.5.1 step 4: replaying the
    trigger (a crash between the `UPDATE` and the enqueue, a `fleet resume`, a second
    `fleet stubs resolve`) upserts the same row and dispatches nothing new.
    """

    consumer_repo_id: RepoId
    provider_repo_ids: tuple[RepoId, ...]
    round_index: int
    key: str
    coord_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReconcileOutcome:
    """The result of the end-of-run `stub_reconcile` step (§3.5.1, §13 row 35).

    `held_for_merge` is the §13 row 45 carve-out made visible: those rows were NOT abandoned
    because their provider still has an open PR, and the operator needs to see which.
    """

    decisions: tuple[StubDecision, ...]
    held_for_merge: tuple[str, ...]
    degraded_consumers: tuple[RepoId, ...]


# --------------------------------------------------------------------------------------
# the idempotency key
# --------------------------------------------------------------------------------------


def revalidation_key(round_index: int, provider_repo_ids: Iterable[RepoId]) -> str:
    """`'r' || <round> || ':' || sha256(<sorted provider_repo_ids>)` — §3.5.1 step 4, verbatim.

    The provider set is sorted and de-duplicated before hashing, because the coalescing identity
    is the *set* of providers whose fix this round covers: `batched` folds three late fixes of
    one consumer into one round, and it can only do so if the same three ids in any order and
    any multiplicity produce the same key. Ids are newline-joined so that `{"a-b", "c"}` and
    `{"a", "b-c"}` cannot collide on a concatenation.
    """
    if round_index < 0:
        raise ValueError(f"revalidation round must be non-negative, got {round_index}")
    ids = sorted(set(provider_repo_ids))
    if not ids:
        raise ValueError("a revalidation round covers at least one provider")
    digest = hashlib.sha256("\n".join(ids).encode()).hexdigest()
    return f"r{round_index}:{digest}"


# --------------------------------------------------------------------------------------
# T1 — ACTIVE -> SUPERSEDED
# --------------------------------------------------------------------------------------


def supersede(
    stub: StubRecord,
    provider: ProviderFacts,
    *,
    policy: RevalidationPolicy = RevalidationPolicy.BATCHED,
    operator_triggered: bool = False,
) -> StubDecision | None:
    """T1, or `None` when the trigger is not met.

    Fires only on `RepoStatus.SUCCEEDED` **and** `PullRequestDraft.state == 'MERGED'`. A provider
    that succeeded with an unmerged PR returns `None`: the real `//` label is not on the
    integration branch yet, so rewriting the consumer's dependency to it would point at nothing.

    Under `stubs.revalidation: manual` the automatic trigger is disabled entirely and the
    operator's `fleet stubs resolve` (`operator_triggered=True`) is the only path — SPEC §3.5.1:
    "`manual`: nothing is enqueued; `fleet stubs resolve` is the only trigger". The operator
    trigger still requires the merged provider: `resolve` is a scheduling override, not a licence
    to point a consumer at a label that does not exist.

    A row already `SUPERSEDED` returns `None` — that is the replay case of §3.5.1 step 4, and it
    must be a no-op rather than a second transition. A terminal row raises: the resolution query
    selects `state = 'ACTIVE'`, so a terminal row reaching here is a caller bug.
    """
    _refuse_terminal(stub, StubTransition.T1)
    if stub.state is StubState.SUPERSEDED:
        return None  # replayed trigger; the UPDATE already happened (§3.5.1 step 4)
    if stub.provider_repo_id != provider.repo_id:
        raise InvalidStubTransition(
            f"{stub.coord_key}: provider facts are for {provider.repo_id!r}, but this stub's "
            f"provider is {stub.provider_repo_id!r}"
        )
    if not provider.merged:
        return None  # SUCCEEDED without a MERGED PR is not a trigger (ADR-0011 stacking)
    if policy is RevalidationPolicy.MANUAL and not operator_triggered:
        return None  # `manual`: `fleet stubs resolve` is the only trigger

    return StubDecision(
        coord_key=stub.coord_key,
        consumer_repo_id=_consumer_of(stub),
        provider_repo_id=stub.provider_repo_id,
        transition=StubTransition.T1,
        from_state=StubState.ACTIVE,
        to_state=StubState.SUPERSEDED,
        # The consumer is NOT promoted here: SUPERSEDED means "not yet proven" (§3.5.1). It stays
        # DEGRADED with a held draft PR until a round passes against the real dependency.
        consumer_status=RepoStatus.DEGRADED,
        detail=(
            f"provider {provider.repo_id} SUCCEEDED with a MERGED PR"
            + (" (operator `fleet stubs resolve`)" if operator_triggered else "")
        ),
        round_index=stub.rounds_spent + 1,
    )


def plan_revalidation(
    consumer_repo_id: RepoId,
    superseded: Sequence[StubDecision],
    *,
    policy: RevalidationPolicy = RevalidationPolicy.BATCHED,
    operator_triggered: bool = False,
) -> tuple[RevalidationPlan, ...]:
    """Turn one consumer's T1 decisions into the `TaskKind.REVALIDATE` task(s) to enqueue.

    This is the §3.5.1 storm policy and nothing else:

    - `batched` (default) → **one** plan per consumer, whose `revalidation_key` hashes the whole
      provider set. Three providers of one consumer fixed in the same wave are one round, which
      is the mechanical form of the storm bound (§13 row 34).
    - `eager` → one plan per superseded row, each with its own single-provider key.
    - `manual` → nothing, unless the operator ran `fleet stubs resolve`, in which case it batches.

    Every plan is keyed, so replaying this on `fleet resume` upserts the same task rows.
    """
    rows = [d for d in superseded if d.transition is StubTransition.T1]
    if not rows:
        return ()
    if any(d.consumer_repo_id != consumer_repo_id for d in rows):
        raise InvalidStubTransition(
            f"plan_revalidation batches ONE consumer; got decisions for "
            f"{sorted({d.consumer_repo_id for d in rows})}"
        )
    if policy is RevalidationPolicy.MANUAL and not operator_triggered:
        return ()

    if policy is RevalidationPolicy.EAGER:
        return tuple(
            RevalidationPlan(
                consumer_repo_id=consumer_repo_id,
                provider_repo_ids=(d.provider_repo_id,),
                round_index=d.round_index,
                key=revalidation_key(d.round_index, (d.provider_repo_id,)),
                coord_keys=(d.coord_key,),
            )
            for d in rows
        )

    providers = tuple(sorted({d.provider_repo_id for d in rows}))
    # One round covers them all, so the round index is the highest any member asked for: a stub
    # already through one failed round may not have its second round filed as its first.
    round_index = max(d.round_index for d in rows)
    return (
        RevalidationPlan(
            consumer_repo_id=consumer_repo_id,
            provider_repo_ids=providers,
            round_index=round_index,
            key=revalidation_key(round_index, providers),
            coord_keys=tuple(sorted(d.coord_key for d in rows)),
        ),
    )


# --------------------------------------------------------------------------------------
# T2 / T3 — the outcome of one revalidation round
# --------------------------------------------------------------------------------------


def settle_revalidation(
    stub: StubRecord,
    report: VerificationReport,
    *,
    sibling_states: Mapping[str, StubState] | None = None,
    failure_class: FailureClass | None = None,
    budget_breach: RevalidationBudgetExhausted | None = None,
) -> StubDecision | None:
    """T2 or T3 from one round's outcome, or `None` when the stub gets another round.

    Precedence, worst first — a round can satisfy more than one clause, and the consequences
    differ in how far they escalate:

    1. `budget_breach` → T3 `BUDGET_EXHAUSTED`. The consumer **stays `DEGRADED`** with a held
       draft PR (§3.5.1 "fail-closed on exhaustion"); `stubs.on_budget_exhausted` has one legal
       value, `"hold"`, so there is no branch here that promotes.
    2. `FailureClass.STUB_DIVERGED` → T3 `STUB_DIVERGED`, and the consumer goes
       `REQUIRES_HUMAN_INTERVENTION` (§13 row 33). This is the one outcome that escalates past
       `DEGRADED`: a genuine source incompatibility is not a retry candidate, and it does **not**
       enter the ADR-0014 ladder.
    3. `verdict == 'PASS'` **and** `verified_against_stubs == []` → T2. Both halves are required:
       a PASS that still names a stub proves only that a *different* lie still holds.
    4. Otherwise, rounds exhausted → T3 `ROUNDS_EXHAUSTED`; else `None` — the round failed but
       the stub has another one, so nothing transitions.

    The budget breach is the instance `orchestrator/budgets.py` raised, not a dollar comparison
    re-done here: the ceiling is enforced by one CAS, in one place (§11.2).

    `sibling_states` maps the consumer's *other* coord_keys to their states. A T2 promotes the
    consumer to `SUCCEEDED` only when every one of them is already `RESOLVED`
    (`RepoState._stub_invariants`); otherwise the consumer stays `DEGRADED` with one fewer lie.
    """
    if stub.state is not StubState.SUPERSEDED:
        raise InvalidStubTransition(
            f"{stub.coord_key}: a revalidation round settles a SUPERSEDED row; this row is "
            f"{stub.state}"
        )
    if report.repo_id != _consumer_of(stub):
        raise InvalidStubTransition(
            f"{stub.coord_key}: report is for repo {report.repo_id!r}, but this stub's consumer "
            f"is {_consumer_of(stub)!r}"
        )
    consumer = _consumer_of(stub)
    round_index = max(stub.rounds_spent, report.revalidation_round)

    if budget_breach is not None:
        return StubDecision(
            coord_key=stub.coord_key,
            consumer_repo_id=consumer,
            provider_repo_id=stub.provider_repo_id,
            transition=StubTransition.T3,
            from_state=StubState.SUPERSEDED,
            to_state=StubState.ABANDONED,
            # never SUCCEEDED: an exhausted budget means unfinished work, not finished work
            consumer_status=RepoStatus.DEGRADED,
            detail=f"revalidation budget exhausted: {budget_breach}",
            abandon_reason=AbandonReason.BUDGET_EXHAUSTED,
            finding=StubFinding.REVALIDATION_BUDGET_EXHAUSTED,
            round_index=round_index,
        )

    if failure_class is FailureClass.STUB_DIVERGED:
        return StubDecision(
            coord_key=stub.coord_key,
            consumer_repo_id=consumer,
            provider_repo_id=stub.provider_repo_id,
            transition=StubTransition.T3,
            from_state=StubState.SUPERSEDED,
            to_state=StubState.ABANDONED,
            consumer_status=RepoStatus.REQUIRES_HUMAN_INTERVENTION,
            detail=(
                f"stub rot: the migrated {stub.provider_repo_id} is incompatible with "
                f"pinned_version {stub.pinned_version!r}"
            ),
            abandon_reason=AbandonReason.STUB_DIVERGED,
            finding=StubFinding.STUB_ROT,
            round_index=round_index,
        )

    if report.verdict == "PASS" and not report.verified_against_stubs:
        others = dict(sibling_states or {})
        others.pop(stub.coord_key, None)
        all_clear = all(state is StubState.RESOLVED for state in others.values())
        return StubDecision(
            coord_key=stub.coord_key,
            consumer_repo_id=consumer,
            provider_repo_id=stub.provider_repo_id,
            transition=StubTransition.T2,
            from_state=StubState.SUPERSEDED,
            to_state=StubState.RESOLVED,
            consumer_status=RepoStatus.SUCCEEDED if all_clear else RepoStatus.DEGRADED,
            detail=(
                f"round {round_index} PASSed against the real dependency with no stub in the "
                f"report" + ("" if all_clear else "; other stubs of this consumer are still open")
            ),
            round_index=round_index,
        )

    if round_index >= stub.max_revalidation_rounds:
        return StubDecision(
            coord_key=stub.coord_key,
            consumer_repo_id=consumer,
            provider_repo_id=stub.provider_repo_id,
            transition=StubTransition.T3,
            from_state=StubState.SUPERSEDED,
            to_state=StubState.ABANDONED,
            consumer_status=RepoStatus.DEGRADED,
            detail=(
                f"{round_index} of {stub.max_revalidation_rounds} revalidation rounds spent "
                f"without a green against the real dependency"
            ),
            abandon_reason=AbandonReason.ROUNDS_EXHAUSTED,
            finding=StubFinding.REVALIDATION_BUDGET_EXHAUSTED,
            round_index=round_index,
        )

    return None  # the round failed, but this stub has another one; it stays SUPERSEDED


# --------------------------------------------------------------------------------------
# T4 / T3 — end-of-run reconciliation and the operator's abandon
# --------------------------------------------------------------------------------------


def reconcile(
    rows: Iterable[StubRecord],
    providers: Mapping[RepoId, ProviderFacts],
) -> ReconcileOutcome:
    """`stub_reconcile` (§3.5.1, §13 row 35): the sweep that stops the fleet shipping a lie.

    Every row still `ACTIVE` or `SUPERSEDED` goes `ABANDONED` with `abandon_reason='END_OF_RUN'`
    (§12.38) and an `UnresolvedStub` finding — `ACTIVE` via T4, `SUPERSEDED` via T3, exactly as
    §3.5.1 step 1 writes it. Their consumers **stay `DEGRADED`**: never promoted, never quietly
    re-labelled `SUCCEEDED`, and there is no parameter on this function that could do so.

    **The one carve-out (§13 row 45): a row whose provider still has an open PR is not
    abandoned.** Under the shipped `pr.draft: true`, wave-0 PRs sit unmerged until a human merges
    them; sweeping those rows would make exit 7 mean "the fleet gave up waiting" instead of "a
    human is needed". Those rows come back in `held_for_merge` and stay open for
    `fleet pr --sync` to resolve. The carve-out lives here and only here — a T3 from an exhausted
    round or from `STUB_DIVERGED` already ran against the merged real label and is unaffected.

    A provider absent from `providers` has no PR to be open, so its row is swept: a missing fact
    is not treated as a reason to keep a stub alive.
    """
    decisions: list[StubDecision] = []
    held: list[str] = []
    degraded: list[RepoId] = []

    for stub in rows:
        if stub.state not in OPEN_STATES:
            continue  # terminal rows are audit records; reconciliation never rewrites one
        provider = providers.get(stub.provider_repo_id)
        if provider is not None and provider.pr_open:
            held.append(stub.coord_key)
            continue
        transition = (
            StubTransition.T4 if stub.state is StubState.ACTIVE else StubTransition.T3
        )
        consumer = _consumer_of(stub)
        decisions.append(
            StubDecision(
                coord_key=stub.coord_key,
                consumer_repo_id=consumer,
                provider_repo_id=stub.provider_repo_id,
                transition=transition,
                from_state=stub.state,
                to_state=StubState.ABANDONED,
                consumer_status=RepoStatus.DEGRADED,
                detail=(
                    f"end of run: provider {stub.provider_repo_id} never reached a MERGED PR"
                ),
                abandon_reason=AbandonReason.END_OF_RUN,
                finding=StubFinding.UNRESOLVED_STUB,
                round_index=stub.rounds_spent,
            )
        )
        if consumer not in degraded:
            degraded.append(consumer)

    # §13 row 35 enforced rather than commented, and as a raise rather than an `assert` — `-O`
    # strips asserts, and "consumers are never promoted here" must not be a debug-build promise.
    promoted = [d.coord_key for d in decisions if d.promotes_consumer]
    if promoted:
        raise InvalidStubTransition(
            f"stub_reconcile may never promote a consumer out of DEGRADED (§13 row 35); "
            f"{sorted(promoted)} tried to"
        )
    return ReconcileOutcome(
        decisions=tuple(decisions),
        held_for_merge=tuple(held),
        degraded_consumers=tuple(degraded),
    )


def abandon_by_operator(stub: StubRecord, reason: str) -> StubDecision:
    """`fleet stubs abandon <consumer> <coord_key> --reason TEXT` (§10).

    The manual T4 from `ACTIVE`, and the same abandonment as T3 from `SUPERSEDED` — one code
    path because the operator's intent is identical and only the `from` state differs. The
    consumer is left `DEGRADED` with a held PR: `fleet stubs abandon` never promotes anything.
    `--reason` is required by the CLI and required here, because an audited abandonment with an
    empty reason is an unaudited one.
    """
    _refuse_terminal(stub, StubTransition.T4)
    if not reason.strip():
        raise InvalidStubTransition(
            f"{stub.coord_key}: `fleet stubs abandon` records a reason; it may not be empty"
        )
    transition = StubTransition.T4 if stub.state is StubState.ACTIVE else StubTransition.T3
    return StubDecision(
        coord_key=stub.coord_key,
        consumer_repo_id=_consumer_of(stub),
        provider_repo_id=stub.provider_repo_id,
        transition=transition,
        from_state=stub.state,
        to_state=StubState.ABANDONED,
        consumer_status=RepoStatus.DEGRADED,
        detail=f"operator abandon: {reason.strip()}",
        abandon_reason=AbandonReason.OPERATOR,
        finding=StubFinding.UNRESOLVED_STUB,
        round_index=stub.rounds_spent,
    )


# --------------------------------------------------------------------------------------
# applying a decision — still no SQL; a new StubRecord for the writer to persist
# --------------------------------------------------------------------------------------


def apply(stub: StubRecord, decision: StubDecision, *, now: datetime) -> StubRecord:
    """The decision as a new `StubRecord`, validated against `ALLOWED_TRANSITIONS`.

    Persistence is still the caller's: this returns the row the single writer should write, so
    the machine can be tested — and the edge set enforced — without a database. `rounds_spent`
    advances to the decision's round, which is what keeps `stubs.revalidation_round` and the
    model's counter from drifting.
    """
    edge = ALLOWED_TRANSITIONS[decision.transition]
    if stub.state is not edge[0]:
        raise InvalidStubTransition(
            f"{stub.coord_key}: {decision.transition} runs {edge[0]} -> {edge[1]}, but the row "
            f"is {stub.state}"
        )
    if (decision.to_state is StubState.ABANDONED) != (decision.abandon_reason is not None):
        raise InvalidStubTransition(
            f"{stub.coord_key}: abandon_reason is set iff the row is ABANDONED (§6 stubs CHECK)"
        )
    return stub.model_copy(
        update={
            "state": edge[1],
            "rounds_spent": max(stub.rounds_spent, decision.round_index),
            "state_changed_at": now,
        }
    )


def next_round_record(stub: StubRecord, *, now: datetime) -> StubRecord:
    """A re-emitted stub as a **new row at the next round** — never a resurrected terminal one.

    §3.5.1: "A re-run that would re-emit an `ABANDONED` stub inserts a new row at the next
    `revalidation_round` rather than resurrecting the old one, so the audit trail is
    append-only." The `stubs` primary key is
    `(run_id, repo_id, stub_coord_key, revalidation_round)`, so this is also the only insert that
    does not collide. A fresh `stub_id` is minted deliberately: the new row is a new fact.
    """
    if stub.state not in TERMINAL_STATES:
        raise InvalidStubTransition(
            f"{stub.coord_key}: a new round opens only after the previous row is terminal; this "
            f"row is {stub.state}"
        )
    nxt = stub.rounds_spent + 1
    if nxt > stub.max_revalidation_rounds:
        raise InvalidStubTransition(
            f"{stub.coord_key}: round {nxt} exceeds max_revalidation_rounds "
            f"{stub.max_revalidation_rounds}; the stub is exhausted, not re-emittable"
        )
    return stub.model_copy(
        update={
            "stub_id": uuid4(),
            "state": StubState.ACTIVE,
            "rounds_spent": nxt,
            "created_at": now,
            "state_changed_at": now,
        },
        deep=True,
    )


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def _consumer_of(stub: StubRecord) -> RepoId:
    """`stubs.consumer_repo_id` is an explicit alias of `repo_id`, one row per consumer (§6). A
    `StubRecord` carrying no consumer is a row that cannot be written, so say so."""
    if not stub.consumer_repo_ids:
        raise InvalidStubTransition(
            f"{stub.coord_key}: a stub row names its consumer; consumer_repo_ids is empty"
        )
    return stub.consumer_repo_ids[0]


def _refuse_terminal(stub: StubRecord, transition: StubTransition) -> None:
    if stub.state in TERMINAL_STATES:
        raise InvalidStubTransition(
            f"{stub.coord_key}: {stub.state} is terminal — there is no path out of it and no "
            f"path back to ACTIVE (§3.5.1); {transition} is refused. A re-emission opens a NEW "
            f"row at the next revalidation_round (`next_round_record`)."
        )
