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
from uuid import UUID, uuid4

from fleet.models.enums import FailureClass, PrState, RepoStatus, StubFidelity, StubState
from fleet.models.repo import RepoId
from fleet.models.tasks import StubRecord, VerificationReport
from fleet.orchestrator.budgets import RevalidationBudgetExhausted
from fleet.orchestrator.reentry import BlockerState

__all__ = [
    "ALLOWED_TRANSITIONS",
    "OPEN_STATES",
    "TERMINAL_STATES",
    "AbandonReason",
    "HeldStub",
    "InvalidStubTransition",
    "ProviderFacts",
    "ReconcileOutcome",
    "RevalidationPlan",
    "RevalidationPolicy",
    "StubDecision",
    "StubFinding",
    "StubTransition",
    "StubTrigger",
    "abandon_by_operator",
    "apply",
    "build_stub_record",
    "detect_stub_triggers",
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
    STUB_ABANDONED = "StubAbandoned"
    """A human dispositioned this stub via `fleet stubs abandon`. Named for the kind the
    committed CLI path already writes (`cli.py`'s `INSERT INTO findings ... 'StubAbandoned'`), so
    the two encodings of that event agree. Neither string appears in SPEC.md; `UnresolvedStub`
    does, and it is assigned there to the reconciliation sweep alone."""
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
    pr_created_at: datetime | None = None
    """`PullRequestDraft.created_at`, so `reconcile` can bound the §13 row 45 carve-out by
    `pr.open_pr_max_age_s`. `None` = unknown, and an unknown age is never treated as fresh."""

    @property
    def merged(self) -> bool:
        return self.status is RepoStatus.SUCCEEDED and self.pr_state is PrState.MERGED

    @property
    def pr_open(self) -> bool:
        """§13 row 45: an un-merged, un-closed PR means a human is mid-review, not that the
        provider failed. `reconcile` holds these rows instead of abandoning them.

        `PrState.HELD` is deliberately NOT in this set. `enums.py` defines `HELD` as entered
        **only by `stub_reconcile`**, meaning "this run has finished without resolving its stubs
        and will never promote it". §13 row 35 runs reconciliation again on `fleet resume`, so
        counting `HELD` as "a human is mid-review" would feed the carve-out its own output: the
        row would be held on every resume, forever, and its `UnresolvedStub` finding would never
        be written. A `HELD` PR is the fleet's own verdict, not a pending human action.
        """
        return self.pr_state in (PrState.DRAFTED, PrState.OPEN)


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
class HeldStub:
    """One `stubs` row `reconcile` left open because its provider still has an open PR."""

    coord_key: str
    consumer_repo_id: RepoId
    provider_repo_id: RepoId


@dataclass(frozen=True, slots=True)
class ReconcileOutcome:
    """The result of the end-of-run `stub_reconcile` step (§3.5.1, §13 row 35).

    `held_for_merge` is the §13 row 45 carve-out made visible: those rows were NOT abandoned
    because their provider still has an open PR, and the operator needs to see which. It is
    per-**row**, not per-stub, for the same reason `decisions` is (see `_consumers_of`).
    """

    decisions: tuple[StubDecision, ...]
    held_for_merge: tuple[HeldStub, ...]
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
) -> tuple[StubDecision, ...]:
    """T1 for **every consumer of this stub**, or `()` when the trigger is not met.

    One `StubRecord` is the aggregate of one `stubs` row per consumer sharing a `stub_id`
    (`schema.sql`: "one row per (stub, consumer, round)"), and §3.5.1's T1 `UPDATE` matches every
    row of the fixed provider. So a stub bound to three degraded dependents yields three
    decisions, not one: taking only the first would supersede one row and leave the other two
    `ACTIVE` against a provider that no longer has a stub target.

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
        return ()  # replayed trigger; the UPDATE already happened (§3.5.1 step 4)
    if stub.provider_repo_id != provider.repo_id:
        raise InvalidStubTransition(
            f"{stub.coord_key}: provider facts are for {provider.repo_id!r}, but this stub's "
            f"provider is {stub.provider_repo_id!r}"
        )
    if not provider.merged:
        return ()  # SUCCEEDED without a MERGED PR is not a trigger (ADR-0011 stacking)
    if policy is RevalidationPolicy.MANUAL and not operator_triggered:
        return ()  # `manual`: `fleet stubs resolve` is the only trigger

    detail = f"provider {provider.repo_id} SUCCEEDED with a MERGED PR" + (
        " (operator `fleet stubs resolve`)" if operator_triggered else ""
    )
    return tuple(
        StubDecision(
            coord_key=stub.coord_key,
            consumer_repo_id=consumer,
            provider_repo_id=stub.provider_repo_id,
            transition=StubTransition.T1,
            from_state=StubState.ACTIVE,
            to_state=StubState.SUPERSEDED,
            # The consumer is NOT promoted here: SUPERSEDED means "not yet proven" (§3.5.1). It
            # stays DEGRADED with a held draft PR until a round passes against the real dep.
            consumer_status=RepoStatus.DEGRADED,
            detail=detail,
            # The round this decision's revalidation will be filed under. It is NOT written to
            # `stubs.revalidation_round` (see `apply`): at T1 time no round has run yet.
            round_index=stub.rounds_spent + 1,
        )
        for consumer in _consumers_of(stub)
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
    consumers = _consumers_of(stub)
    if report.repo_id not in consumers:
        raise InvalidStubTransition(
            f"{stub.coord_key}: report is for repo {report.repo_id!r}, which is not one of this "
            f"stub's consumers {list(consumers)}"
        )
    # A revalidation round is per consumer — one `stubs` row — so the report names which row
    # settles. Selecting `consumers[0]` here would reject C2's legitimate report as a caller bug.
    consumer = report.repo_id
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
    *,
    now: datetime | None = None,
    open_pr_max_age_s: float | None = None,
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

    The carve-out is **bounded**, per §12.38, which scopes it to a PR "still open and unmerged
    inside" the `pr` section's merge-wait timeout. Pass `now` and `open_pr_max_age_s` — a plain
    duration, deliberately NOT named after the config key, because this module reads no config
    and the key stays genuinely unread until a caller passes it — and a PR older than the bound
    is swept rather than held — otherwise a draft untouched for three weeks would be held for
    ever, which is the same silent under-report the carve-out exists to avoid, arriving by the
    opposite route. Omitting either argument leaves the carve-out unbounded, and a provider whose
    `pr_created_at` is unknown is never treated as fresh.

    Every decision is per **`stubs` row**, i.e. per consumer: one `StubRecord` is the aggregate of
    one row per consumer (`schema.sql`), so a stub bound to three degraded dependents yields three
    decisions and three `UnresolvedStub` findings. Emitting one would leave the other two rows
    `ACTIVE` past the final checkpoint and absent from `migration_state.json#unresolved_stubs` —
    the fleet reporting done over work verified against nothing, which is precisely §13 row 35.

    A provider absent from `providers` has no PR to be open, so its row is swept: a missing fact
    is not treated as a reason to keep a stub alive.
    """
    decisions: list[StubDecision] = []
    held: list[HeldStub] = []
    degraded: list[RepoId] = []

    for stub in rows:
        if stub.state not in OPEN_STATES:
            continue  # terminal rows are audit records; reconciliation never rewrites one
        provider = providers.get(stub.provider_repo_id)
        consumers = _consumers_of(stub)
        if provider is not None and _awaiting_merge(provider, now, open_pr_max_age_s):
            held.extend(
                HeldStub(stub.coord_key, consumer, stub.provider_repo_id)
                for consumer in consumers
            )
            continue
        transition = (
            StubTransition.T4 if stub.state is StubState.ACTIVE else StubTransition.T3
        )
        # One decision per consumer — one `stubs` row each. See the docstring: [0] under-reports.
        for consumer in consumers:
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


def abandon_by_operator(stub: StubRecord, consumer: RepoId, reason: str) -> StubDecision:
    """`fleet stubs abandon <consumer> <coord_key> --reason TEXT` (§10).

    The manual T4 from `ACTIVE`, and the same abandonment as T3 from `SUPERSEDED` — one code
    path because the operator's intent is identical and only the `from` state differs. The
    consumer is left `DEGRADED` with a held PR: `fleet stubs abandon` never promotes anything.
    `--reason` is required by the CLI and required here, because an audited abandonment with an
    empty reason is an unaudited one.

    The consumer is an **argument**, not `consumer_repo_ids[0]`: the CLI abandons one named
    `(consumer, coord_key)` row, and the stub's other consumers keep their own rows.

    The finding is `StubAbandoned`, matching the committed `fleet stubs abandon` path, **not**
    `UnresolvedStub`. §3.5.1 assigns `UnresolvedStub` to the end-of-run sweep, so tagging a
    deliberate human disposition with it would inflate the exit-7 "humans needed" set with work a
    human has already dispositioned.
    """
    _refuse_terminal(stub, StubTransition.T4)
    if consumer not in _consumers_of(stub):
        raise InvalidStubTransition(
            f"{stub.coord_key}: {consumer!r} is not a consumer of this stub "
            f"{list(_consumers_of(stub))}"
        )
    if not reason.strip():
        raise InvalidStubTransition(
            f"{stub.coord_key}: `fleet stubs abandon` records a reason; it may not be empty"
        )
    transition = StubTransition.T4 if stub.state is StubState.ACTIVE else StubTransition.T3
    return StubDecision(
        coord_key=stub.coord_key,
        consumer_repo_id=consumer,
        provider_repo_id=stub.provider_repo_id,
        transition=transition,
        from_state=stub.state,
        to_state=StubState.ABANDONED,
        consumer_status=RepoStatus.DEGRADED,
        detail=f"operator abandon: {reason.strip()}",
        abandon_reason=AbandonReason.OPERATOR,
        finding=StubFinding.STUB_ABANDONED,
        round_index=stub.rounds_spent,
    )


# --------------------------------------------------------------------------------------
# applying a decision — still no SQL; a new StubRecord for the writer to persist
# --------------------------------------------------------------------------------------


def apply(stub: StubRecord, decision: StubDecision, *, now: datetime) -> StubRecord:
    """The decision as a new `StubRecord`, validated against `ALLOWED_TRANSITIONS`.

    Persistence is still the caller's: this returns the row the single writer should write, so
    the machine can be tested — and the edge set enforced — without a database.

    **`rounds_spent` advances only when a round actually ran** — T2 and T3 from
    `settle_revalidation`. It is deliberately untouched by T1 and T4: `stubs.revalidation_round`
    is documented as "0 while ACTIVE; N when the Nth round ran", is a component of
    `PRIMARY KEY (run_id, repo_id, stub_coord_key, revalidation_round)`, and §3.5.1's own T1
    statement is `UPDATE stubs SET state='SUPERSEDED', resolved_at=…, resolved_by_run_id=…` — it
    does not touch the round. Bumping it at T1 would make the writer address a row that does not
    exist (updating nothing) or insert a duplicate at a round that never ran, and would breach
    `CHECK (revalidation_round <= max_revalidation_rounds)` outright whenever
    `stubs.max_revalidation_rounds` is 0 (a legal setting, `Field(default=2, ge=0)`).
    `StubDecision.round_index` on a T1 is the round the *revalidation task* will be filed under,
    not the row's column.

    The result is re-validated rather than `model_copy`-ed blind: `model_copy` skips validators,
    which is how an out-of-cap `rounds_spent` would otherwise reach the database as a CHECK
    violation naming a column instead of the rule (Rule 11).
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
    rounds_spent = stub.rounds_spent
    if decision.transition in (StubTransition.T2, StubTransition.T3):
        rounds_spent = max(rounds_spent, decision.round_index)
    update = {"state": edge[1], "rounds_spent": rounds_spent, "state_changed_at": now}
    try:
        return StubRecord.model_validate(stub.model_dump() | update)
    except ValueError as exc:  # cap breach, or any other model invariant
        raise InvalidStubTransition(
            f"{stub.coord_key}: {decision.transition} would produce an invalid row: {exc}"
        ) from exc


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
# §37 Leg 1 (round VI task 67) — the TRANSFORM-phase stub-creation DECISION. Trigger detection and
# `StubRecord` construction, as pure functions in the same "the caller persists" shape as the four
# transitions above (`apply()`'s own docstring: "Persistence is still the caller's: this returns
# the row the single writer should write"). No connection, no SQL: the async query/INSERT glue
# this leg also needs lives in `cli.py` (`_detect_transform_stub_triggers`, `_create_stub_
# records`), the same split `orchestrator.reentry.stub_permits_removal` (a pure predicate) and
# `cli._read_blocker_states`/`cli._apply_stub_decisions` (the DB-touching callers) already use.
#
# The third piece of this leg — the `RUNNING -> DEGRADED` correction — is NOT here: ADR-0124
# (fix round 1, following a task-scoped review) moved it to `state/repository.py`'s
# `SqliteStateRepository.stub_degrade_transform`, alongside `models.enums.STUB_DEGRADE`/
# `degrade_for_stub`/`StubDegradation`, mirroring `demote_to_floor`/`RESUME_DEMOTE`/`demote`/
# `PhaseDemotion` exactly — a real `ALLOWED_TRANSITIONS` door for the one narrow, audited case a
# TRANSFORM completion needs, not a bare raw-SQL bypass. See ADR-0124 for why.
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StubTrigger:
    """One `(consumer, provider, coord_key)` triple `detect_stub_triggers` found for THIS
    TRANSFORM wave: `consumer_repo_id` is dispatched this wave, it has a live dependency edge
    naming `coord_key` (over the ordering subgraph filter) to `provider_repo_id`, and the
    provider is mechanically terminal at `REQUIRES_HUMAN_INTERVENTION` (§3.5.1's stub-creation
    trigger).

    **`coord_key` is the EDGE's own `dst_coord_key`, never the provider's `primary_coord_key`.**
    A provider owning two-plus published coordinates (e.g. a monorepo publishing several Maven
    artifacts) can have consumers naming DIFFERENT coordinates of it; `_unit_deps`'s stub-redirect
    lookup is keyed on `(consumer_repo_id, edges.dst_coord_key)` — the exact coordinate the
    consumer's own edge names — so a stub keyed on the provider's primary coordinate alone would
    silently never redirect for any edge naming a different one (a review-caught defect in this
    leg's first landing, `d548b38`). One `StubTrigger` per distinct `(consumer, provider,
    coord_key)` a real edge names is what keeps this byte-identical to `_unit_deps`'s lookup key.
    """

    consumer_repo_id: RepoId
    provider_repo_id: RepoId
    coord_key: str


@dataclass(frozen=True, slots=True)
class InheritedStubFact:
    """One ACTIVE `stubs` row a would-be DIRECT provider itself carries as a CONSUMER — i.e. that
    direct provider is itself transitively downstream of some abandoned repo `r` (§3.5 item 4's
    "whole descendant set", D131/§12.14).

    `provider_repo_id`/`coord_key`/`fidelity` are copied VERBATIM off that existing row by the
    caller, never reconstructed here — the same "copy, don't rebuild" discipline `StubTrigger`'s
    own docstring already uses for the direct-RHI case, extended one hop further so the mark stays
    on the ORIGINAL abandoned repo `r`, never the intermediate (§3.5 item 4: "provenance stays on
    `r`, never on the intermediate").
    """

    provider_repo_id: RepoId
    coord_key: str
    fidelity: StubFidelity


def detect_stub_triggers(
    dispatched_repo_ids: Iterable[str],
    edges: Iterable[tuple[str, str, str]],
    provider_states: Mapping[str, BlockerState],
    active_stub_facts_by_provider: Mapping[str, Sequence[InheritedStubFact]] | None = None,
) -> tuple[StubTrigger, ...]:
    """§37 Leg 1 step 1: which of `dispatched_repo_ids` need a stub this wave, and for which
    coordinate.

    `edges` is `(provider_id, consumer_id, coord_key)` — the same `(dst_id, src_id,
    dst_coord_key)` shape `_unit_deps`'s own edge query already selects, over the SAME filter
    `_ordering_pairs`/`_unit_deps` share (`graph.dag_edge_kinds`, `confidence >=
    graph.min_confidence`, `ordering_suppressed = 0`, `src_kind = 'REPO' AND dst_kind = 'REPO'`);
    this function does not re-derive that filter, it only consumes its output. (Earlier than
    ADR-0124's fix round, this took `_ordering_pairs`'s own `(provider_id, dependent_id)` pairs
    directly — that shape carries no `dst_coord_key` at all, which is what made the C2 defect
    possible; the caller now runs its own query over the identical filter, with `dst_coord_key`
    and the REPO-kind restriction added, matching `_unit_deps`'s query verbatim rather than
    `_ordering_pairs`'s narrower one.)

    There is no `repos.status` column (`schema.sql`'s `repos` table carries none) — a repo's
    status is a `phases`-table fact, so "the provider's status is `REQUIRES_HUMAN_INTERVENTION`"
    is read the same way `stub_permits_removal` already reads it: ANY of the provider's `phases`
    rows, not ALL (RHI is mechanically terminal — once one phase is abandoned there, the whole
    repo can never again produce landed work). `provider_states` is keyed by repo id, the exact
    shape `cli._read_blocker_states` already returns.

    A provider absent from `provider_states` (no `phases` row at all, or never resolved) is not a
    trigger — there is no RHI to find, mirroring `stub_permits_removal`'s own fail-closed default
    for an unresolvable name.

    **No `stub_blocked` policy switch here (review finding M2, fix round 1) — deliberately, not
    an oversight.** `stub_permits_removal` (`orchestrator/reentry.py`) takes one because it is
    reachable from `fleet resume` regardless of the flag and must be a hard `False` for every
    input when the operator did not opt in; this function has no such caller today. **Corrected
    2026-09-07 (round VI task 69 fix round): its own caller, `cli._detect_transform_stub_
    triggers`, is no longer permanently unreachable** — the `--stub-blocked` refusal (ADR-0113
    condition 2) was removed in round VI task 69, and `_detect_transform_stub_triggers` is now
    called for real from `_transform_impl`'s wave loop, gated exactly as this paragraph
    anticipated: `_transform_impl` does not call it at all when the flag is off, so this
    function's own gating-free contract still holds — nothing here changed, only the caller's
    reachability.

    **`active_stub_facts_by_provider` — the (M1) inheritance branch, §3.5 item 4 / D131.** Keyed
    by repo id, this names every ACTIVE `stubs` row THAT repo itself carries as a CONSUMER — i.e.
    it answers "is this edge's direct provider itself transitively downstream of an abandoned
    repo?" A second-layer-and-beyond dependent whose direct provider is not itself RHI (so the
    first branch above finds nothing) but IS itself an active stub consumer inherits a trigger
    copied from that EXISTING row — `provider_repo_id`/`coord_key`/`fidelity` come from the row,
    never from this edge and never naming the intermediate — so the mark stays on the original
    abandoned repo `r` exactly as §3.5 item 4 requires. `EMPTY_FAILING` facts are excluded: an
    empty target fails the intermediate's own BUILD (SPEC's stated exception — "no repo becomes
    `DEGRADED`"), so there is nothing live to inherit past it. Needs **no closure algorithm**: this
    function already runs once per wave, over that wave's own dispatched members, and `stubs` rows
    are durable — a trigger created here for one repo becomes, in a LATER wave, an entry
    `active_stub_facts_by_provider` reads for THAT repo's own dependents, so per-layer inheritance
    computes the full transitive descendant set by induction across wave boundaries. Defaults to
    empty (existing callers over cached fixtures need not change): a `None`/empty mapping makes
    this branch find nothing, identical to today's behaviour.
    """
    dispatched = set(dispatched_repo_ids)
    inherited_facts = active_stub_facts_by_provider or {}
    seen: set[tuple[str, str, str]] = set()
    found: list[StubTrigger] = []
    for provider_id, consumer_id, coord_key in edges:
        if consumer_id not in dispatched:
            continue
        state = provider_states.get(provider_id)
        if state is not None and RepoStatus.REQUIRES_HUMAN_INTERVENTION in state.phase_statuses:
            key = (consumer_id, provider_id, coord_key)
            if key not in seen:
                seen.add(key)
                found.append(
                    StubTrigger(
                        consumer_repo_id=consumer_id,
                        provider_repo_id=provider_id,
                        coord_key=coord_key,
                    )
                )
            continue
        for fact in inherited_facts.get(provider_id, ()):
            if fact.fidelity is StubFidelity.EMPTY_FAILING:
                continue
            key = (consumer_id, fact.provider_repo_id, fact.coord_key)
            if key in seen:
                continue
            seen.add(key)
            found.append(
                StubTrigger(
                    consumer_repo_id=consumer_id,
                    provider_repo_id=fact.provider_repo_id,
                    coord_key=fact.coord_key,
                )
            )
    return tuple(
        sorted(found, key=lambda t: (t.consumer_repo_id, t.provider_repo_id, t.coord_key))
    )


def build_stub_record(
    *,
    run_id: UUID,
    consumer_repo_id: RepoId,
    provider_repo_id: RepoId,
    coord_key: str,
    pinned_version: str | None,
    max_revalidation_rounds: int,
    now: datetime,
) -> StubRecord:
    """§37 Leg 1 step 2: the FIRST `StubRecord` of a stub's lifecycle (`state=ACTIVE`,
    `revalidation_round=0` implicitly — that column is not a `StubRecord` field; the caller's
    INSERT supplies it, per `schema.sql`'s own comment: "0 while ACTIVE").

    `max_revalidation_rounds` is REQUIRED, deliberately with no default: `StubRecord`'s own model
    default (2) and `settings.config.stubs.max_revalidation_rounds`'s default (also 2, `settings.
    py`'s `StubsSection`) happen to agree today, but a default HERE would silently substitute a
    value the operator never wrote the moment either one changes (review finding M3, task 67 fix
    round 1) — the caller must read the configured value and pass it explicitly.

    `pinned_version is None` is the ENTIRE fidelity decision (§3.5 item 2: "If no published
    artifact exists... the stub is an empty target that fails at build time") — `EMPTY_FAILING`
    exactly then, `PUBLISHED_ARTIFACT` otherwise. `StubRecord._fidelity_matches_pin` re-validates
    this at construction, so a caller cannot pass a contradictory pair and have it silently
    accepted.
    """
    fidelity = (
        StubFidelity.EMPTY_FAILING if pinned_version is None else StubFidelity.PUBLISHED_ARTIFACT
    )
    return StubRecord(
        run_id=run_id,
        coord_key=coord_key,
        provider_repo_id=provider_repo_id,
        consumer_repo_ids=[consumer_repo_id],
        fidelity=fidelity,
        pinned_version=pinned_version,
        max_revalidation_rounds=max_revalidation_rounds,
        created_at=now,
        state_changed_at=now,
    )



# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def _consumers_of(stub: StubRecord) -> tuple[RepoId, ...]:
    """Every consumer of this stub — i.e. every `stubs` **row** the record aggregates.

    `schema.sql`: "one row per (stub, consumer, round)", with `stub_id` "SHARED by every consumer
    row of one stub, so `consumer_repo_ids` is the aggregate of the rows, not a copy". The list
    is the designed shape, so returning `consumer_repo_ids[0]` is a silent truncation of the
    normal case, not a workaround for a model/schema mismatch. A record carrying no consumer is a
    row that cannot be written, so say so (Rule 11).
    """
    if not stub.consumer_repo_ids:
        raise InvalidStubTransition(
            f"{stub.coord_key}: a stub row names its consumer; consumer_repo_ids is empty"
        )
    return tuple(stub.consumer_repo_ids)


def _awaiting_merge(
    provider: ProviderFacts, now: datetime | None, open_pr_max_age_s: float | None
) -> bool:
    """§13 row 45, bounded by §12.38's merge-wait window on the provider's PR.

    An open PR holds the stub only while the wait is still legitimate. Past the bound the row is
    swept like any other, because "a human is mid-review" stops being true at some point and the
    alternative is a stub held for the life of the project.
    """
    if not provider.pr_open:
        return False
    if now is None or open_pr_max_age_s is None:
        return True  # unbounded carve-out: the caller supplied no clock and no ceiling
    if provider.pr_created_at is None:
        return True  # unknown age is not evidence of staleness; hold and report it
    return (now - provider.pr_created_at).total_seconds() <= open_pr_max_age_s


def _refuse_terminal(stub: StubRecord, transition: StubTransition) -> None:
    if stub.state in TERMINAL_STATES:
        raise InvalidStubTransition(
            f"{stub.coord_key}: {stub.state} is terminal — there is no path out of it and no "
            f"path back to ACTIVE (§3.5.1); {transition} is refused. A re-emission opens a NEW "
            f"row at the next revalidation_round (`next_round_record`)."
        )
