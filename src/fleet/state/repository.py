"""Typed CRUD over `state/fleet.db` — the only module in the harness that writes SQL (§6, §8).

`StateRepository` is a `typing.Protocol` and `SqliteStateRepository` is the one implementation
that exists today (CLAUDE.md Guardrail 3: orchestration logic depends on the interface, never on
a concrete store). §6's ADR-0004 exit condition names the same seam: "the moment the fleet spans
more than one process or host, the store is swapped for Postgres behind the same
`StateRepository` Protocol".

**Four normative primitives live here, and nowhere else.**

1. **The task claim is one compare-and-swap statement** (§6 "CLAIM (normative)"). Never a
   `SELECT` followed by an `UPDATE`: two workers both see `PENDING`, both write `RUNNING`, both
   run `fleet migrate` on the same repo, both commit to `migrate/<repo>`, and both burn that
   repo's `max_usd`. `claim_next_task()` returns the task **or `None`**; ownership is the fact
   that the statement matched one row.

2. **Every worker-issued write is fenced** (§6 "FENCING (normative)"). The fence granted at claim
   time is a **required, keyword-only argument** on every mutating phase method here — it cannot
   be forgotten, because there is no overload without it. A statement that matches zero rows
   means the lease was reclaimed underneath the caller, and the caller MUST abort without
   touching git or the worktree: that is why a zero-row fenced write raises `LeaseStolenError`
   rather than returning a falsy value some caller can drop on the floor (CLAUDE.md Rule 11).
   A heartbeat alone cannot do this — a bare pid is not an identity and a stale holder passes
   every check — so the *fence bump* is what invalidates the old holder.

3. **The reaper bumps the fence — and releases the expired budget holds in the SAME
   transaction** (§6, v8). `reap_expired_phase_leases()` returns how many leases it reclaimed, so
   the caller can emit one `LeaseExpired` event each. The budget half needs `reservations` to be
   possible at all: a scalar `reserved_usd` records that money is held and nothing about whose,
   so releasing "the expired reservation" could only mean releasing every live worker's hold too.

4. **Budget reserve/settle is a guarded CAS, never read-then-write** (§6 "RESERVATION
   (normative)"). Read-then-write lets 12 workers each reserve $3 against a $497/$500 ledger and
   all 12 writes succeed. A zero-row reservation is a **refusal**, raised as
   `BudgetRefusedError` — never a `bool` a caller can ignore. `repo_ledger` runs the **same**
   statement shape against the per-repo ceiling, and `reserve_repo_budget` / `settle_repo_budget`
   move BOTH ledgers in one transaction — see `reserve_repo_budget`, which is the authority on
   the run/repo nesting question §6 leaves open.

**Streaming.** §11 ("Query results"): this module exposes `iter_*` generators over `aiosqlite`
cursors with `arraysize = 1000` for every table that can exceed 10 000 rows — `symbols`,
`edges`, `events`, `attempts`. **Returning a `list` from any of those is a review-blocking
defect**, because a 250-repo run holds ~10⁵ events and ~10⁵ symbols per fleet and folding them
into memory is §13 row 8's RSS breach.

**Read/write split.** Reads go through the injected `mode=ro` connection; every write goes
through the injected `StateWriter`, which is the process's single writer (§11.5). This class
never opens a writable connection, so a repository handed to a worker is read-only by
construction rather than by convention — `ReadOnlyRepository` is exactly the query half of the
Protocol, and it is what `WorkerContext.db` is typed against (§7.1).

**Idempotency.** Every `INSERT` here carries an `ON CONFLICT` clause against the §6 idempotency
key for its table, so re-running any phase converges instead of duplicating.

**Timestamps.** Instants are passed in as tz-aware `datetime` and stored as a single fixed-width
ISO-8601 UTC rendering. That is not cosmetic: the reaper compares `lease_expires_at < :now` as
**text**, so two different renderings of the same instant would silently order wrongly. Row
fields are returned as stored (`str`).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, Protocol, runtime_checkable

import aiosqlite

from fleet.models.enums import (
    PHASE_DEMOTED_KIND,
    Phase,
    PhaseDemotion,
    RepoStatus,
    TaskKind,
    demote,
)
from fleet.obs.redact import redact_text
from fleet.state import checkpoints
from fleet.state.db import StateWriter
from fleet.util.hashing import sha256_text

__all__ = [
    "READ_ARRAYSIZE",
    "AttemptRow",
    "BudgetLedgerRow",
    "BudgetRefusedError",
    "ClaimedTask",
    "EdgeRow",
    "EventRow",
    "LeaseStolenError",
    "PhaseRow",
    "ReadOnlyRepository",
    "RepoBudgetRefusedError",
    "RepoLedgerRow",
    "RepositoryError",
    "ReservationRefusedError",
    "ReservationRow",
    "SqliteStateRepository",
    "StateRepository",
    "SymbolRow",
]

#: §11 "Query results": every `iter_*` cursor fetches in batches of this size.
READ_ARRAYSIZE: Final = 1000

#: Settlement subtracts a float it previously added; the guard must tolerate one ulp of drift
#: without letting a real over-settle through.
_USD_EPSILON: Final = 1e-9

#: `repo_ledger.revalidation_max_usd`'s schema default (`stubs.revalidation_max_cost_usd`). Named
#: here so opening a ledger row without an explicit sub-ceiling lands on the DDL's number rather
#: than on whatever the caller happened to omit.
DEFAULT_REVALIDATION_MAX_USD: Final = 2.0


# --------------------------------------------------------------------------------------
# errors — refusals are raised, never returned (CLAUDE.md Rule 11)
# --------------------------------------------------------------------------------------


class RepositoryError(RuntimeError):
    """Base class for every refusal this module raises. Never caught-and-ignored internally."""


class LeaseStolenError(RepositoryError):
    """A fenced write matched zero rows: the lease was reclaimed under its old holder (§6).

    The caller MUST abort immediately, **without touching git or the worktree**, and emit
    `LeaseStolen`. This is an exception rather than a return value precisely because the one
    thing a caller must not be able to do is continue.
    """


class BudgetRefusedError(RepositoryError):
    """A budget CAS matched zero rows (§6 "RESERVATION (normative)").

    Either the run is `halted`, or the reservation would push `spent_usd + reserved_usd` past
    `max_usd`. "Fail-closed" is a constraint, not a convention, so this is raised.
    """


class RepoBudgetRefusedError(BudgetRefusedError):
    """The **repo** half of a nested budget CAS matched zero rows (§6 `repo_ledger`, §3.5).

    A subclass, not a sibling, so `except BudgetRefusedError` still catches every refusal — but
    the type says *which* ceiling refused, which is what decides the breach behaviour: a repo
    breach sends one repo to `REQUIRES_HUMAN_INTERVENTION` and the fleet continues, where a run
    breach halts the fleet (§11.2). A caller that had to parse the message to tell them apart
    would eventually get it wrong on a repo id that contains the word "run".
    """


class ReservationRefusedError(BudgetRefusedError):
    """The `reservations` CAS matched zero rows: there is no such HELD reservation (v8).

    Raised when a `reservation_id` is re-used, and — the case that matters — when a settlement
    names a reservation the reaper has already flipped to `EXPIRED`. That is the whole point of
    giving a hold an identity: a reaped worker that wakes up and settles must be refused, not
    quietly allowed to move money it no longer holds out of `reserved_usd`.

    A `BudgetRefusedError` but deliberately NOT a `RepoBudgetRefusedError`: no ceiling refused
    here, so `CostLedger.reserve`'s backpressure interpretation must not treat it as headroom
    that waiting could free.
    """


# --------------------------------------------------------------------------------------
# row types — the public boundary is typed, never a bare tuple or `Any`
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClaimedTask:
    """The outcome of a won claim. `fence_token` is carried by every later write (§6)."""

    task_id: str
    run_id: str
    repo_id: str
    phase: Phase
    kind: TaskKind
    dest_path: str
    fence_token: int
    lease_expires_at: str
    max_attempts: int


@dataclass(frozen=True, slots=True)
class PhaseRow:
    """The subset of `phases` the orchestrator reads. Not the whole row — Rule 2."""

    run_id: str
    repo_id: str
    phase: Phase
    status: RepoStatus
    attempts: int
    max_attempts: int
    lease_owner: str | None
    lease_fence: int
    lease_expires_at: str | None
    last_error: str | None
    updated_at: str


@dataclass(frozen=True, slots=True)
class BudgetLedgerRow:
    """`budget_ledger` as read. `halted = 1` means no further LLM call may be dispatched."""

    run_id: str
    spent_usd: float
    reserved_usd: float
    max_usd: float
    halted: bool
    reservation_expires_at: str | None
    updated_at: str


@dataclass(frozen=True, slots=True)
class RepoLedgerRow:
    """`repo_ledger` as read: one repo's share of the run, durable across a restart (§3.5).

    `remaining_usd` is the number a resumed process must start from. Before this row was
    readable, a fresh `CostLedger` began every repo at zero, so a repo that had already burned
    its $6 ceiling at hour 30 was handed a fresh $6 at hour 31.
    """

    run_id: str
    repo_id: str
    spent_usd: float
    reserved_usd: float
    max_usd: float
    revalidation_usd: float
    revalidation_max_usd: float
    revalidation_rounds: int
    reservation_expires_at: str | None
    updated_at: str

    @property
    def remaining_usd(self) -> float:
        """Headroom the next reservation may take. Clamped at zero: a negative ceiling is not a
        credit, and the CAS would refuse anyway."""
        return max(0.0, self.max_usd - self.spent_usd - self.reserved_usd)


@dataclass(frozen=True, slots=True)
class ReservationRow:
    """One `reservations` row: WHOSE dollars, how many, until when, and under which fence (v8).

    This is the identity the two ledgers' scalar `reserved_usd` never carried. Without it the
    reaper can only release the aggregate — every live worker's hold together with the dead
    worker's — and the run then under-counts what is committed and overspends.
    """

    reservation_id: str
    run_id: str
    repo_id: str
    phase: Phase | None
    lease_fence: int | None
    amount_usd: float
    state: str
    expires_at: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class SymbolRow:
    """One `symbols` row. `symbol_id` is `None` on the way in and set on the way out."""

    run_id: str
    repo_id: str
    fqn: str
    kind: str
    path: str
    line: int
    language: str
    is_definition: bool
    exported: bool = False
    symbol_id: int | None = None


@dataclass(frozen=True, slots=True)
class EdgeRow:
    """One `edges` row. `edge_key` is THE logical PK (§5 `EdgeKey`); `edge_id` is a local rowid."""

    edge_key: str
    run_id: str
    src_id: str
    dst_coord_key: str
    kind: str
    base_confidence: float
    confidence: float
    evidence_path: str
    detected_at: str
    src_kind: str = "REPO"
    dst_kind: str = "REPO"
    dst_id: str | None = None
    evidence_line: int = -1
    ambiguous: bool = False
    ordering_suppressed: bool = False
    edge_id: int | None = None


@dataclass(frozen=True, slots=True)
class EventRow:
    """One `events` row. `seq` — not `ts` — is the ordering key (§6, §11.5)."""

    run_id: str
    seq: int
    ts: str
    level: str
    event: str
    event_uid: str
    payload: str = "{}"
    repo_id: str | None = None
    phase: Phase | None = None
    event_id: int | None = None


@dataclass(frozen=True, slots=True)
class AttemptRow:
    """One `attempts` row — per-rung outcome plus the two ADR-0024 pointers into git."""

    attempt_id: str
    run_id: str
    repo_id: str
    phase: Phase
    attempt: int
    started_at: str
    finished_at: str
    task_id: str | None = None
    revalidation_round: int = 0
    tier: str = "DETERMINISTIC"
    context_policy: str | None = None
    approach_signature: str = ""
    command: str = "[]"
    command_sha256: str = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    retry_ordinal: int = 0
    exit_code: int | None = None
    failure_class: str | None = None
    duration_ms: int = 0
    stdout_tail: str = ""
    stderr_tail: str = ""
    cost_usd: float = 0.0
    patch_id: str | None = None
    commit_sha: str | None = None
    already_applied: bool = False


# --------------------------------------------------------------------------------------
# the Protocol — orchestration logic depends on THIS, never on the SQLite class
# --------------------------------------------------------------------------------------


@runtime_checkable
class ReadOnlyRepository(Protocol):
    """The query half. `WorkerContext.db` is typed against this: workers read, never write.

    Every `iter_*` here is declared as returning an `AsyncIterator`, so an implementation that
    materialised a `list` would not satisfy the Protocol *and* would be the §11 review-blocking
    defect. The four tables named are exactly §11's: they are the ones that exceed 10 000 rows.
    """

    async def get_phase(self, run_id: str, repo_id: str, phase: Phase) -> PhaseRow | None: ...

    async def get_budget(self, run_id: str) -> BudgetLedgerRow | None: ...

    async def get_reservation(
        self, run_id: str, reservation_id: str
    ) -> ReservationRow | None: ...

    def iter_symbols(self, run_id: str) -> AsyncIterator[SymbolRow]: ...

    def iter_edges(self, run_id: str) -> AsyncIterator[EdgeRow]: ...

    def iter_events(self, run_id: str) -> AsyncIterator[EventRow]: ...

    def iter_attempts(self, run_id: str) -> AsyncIterator[AttemptRow]: ...


@runtime_checkable
class StateRepository(ReadOnlyRepository, Protocol):
    """Reads plus the four normative write primitives. Constructed by the runner only (§11.5)."""

    # -- plan / identity ---------------------------------------------------------------
    async def upsert_run(
        self, run_id: str, *, started_at: datetime, config_sha256: str, harness_version: str
    ) -> None: ...

    async def upsert_repo(
        self, repo_id: str, *, name: str, url: str, now: datetime, dest_path: str | None = None
    ) -> None: ...

    async def upsert_phase(
        self, run_id: str, repo_id: str, phase: Phase, *, now: datetime, max_attempts: int = 3
    ) -> None: ...

    async def upsert_task(
        self,
        task_id: str,
        *,
        run_id: str,
        repo_id: str,
        phase: Phase,
        kind: TaskKind,
        dest_path: str,
        created_at: datetime,
        max_attempts: int = 3,
    ) -> None: ...

    # -- primitive 1: the claim --------------------------------------------------------
    async def claim_next_task(
        self, run_id: str, *, worker: str, now: datetime, lease_ttl_s: int
    ) -> ClaimedTask | None: ...

    # -- primitives 2 and 3: fenced writes, renewal, the reaper ------------------------
    async def acquire_phase_lease(
        self,
        run_id: str,
        repo_id: str,
        phase: Phase,
        *,
        owner: str,
        now: datetime,
        lease_ttl_s: int,
    ) -> int | None: ...

    async def renew_phase_lease(
        self,
        run_id: str,
        repo_id: str,
        phase: Phase,
        *,
        fence: int,
        now: datetime,
        lease_ttl_s: int,
    ) -> None: ...

    async def complete_phase(
        self,
        run_id: str,
        repo_id: str,
        phase: Phase,
        *,
        fence: int,
        status: RepoStatus,
        now: datetime,
        last_error: str | None = None,
    ) -> RepoStatus: ...

    async def reap_expired_phase_leases(self, run_id: str, *, now: datetime) -> int: ...

    async def demote_to_floor(
        self,
        run_id: str,
        repo_id: str,
        *,
        floor: Phase,
        reason: str,
        now: datetime,
    ) -> tuple[PhaseDemotion, ...]: ...

    # -- primitive 4: the ledger CAS ---------------------------------------------------
    async def open_budget_ledger(self, run_id: str, *, max_usd: float, now: datetime) -> None: ...

    async def reserve_budget(
        self, run_id: str, *, amount_usd: float, now: datetime, expires_at: datetime | None = None
    ) -> None: ...

    async def settle_budget(
        self, run_id: str, *, reserved_usd: float, actual_usd: float, now: datetime
    ) -> None: ...

    async def halt_budget_ledger(self, run_id: str, *, now: datetime) -> None: ...

    # -- primitive 4, repo scope: the SAME CAS, nested inside the run's -----------------
    async def get_repo_budget(self, run_id: str, repo_id: str) -> RepoLedgerRow | None: ...

    async def open_repo_ledger(
        self,
        run_id: str,
        repo_id: str,
        *,
        max_usd: float,
        now: datetime,
        revalidation_max_usd: float = DEFAULT_REVALIDATION_MAX_USD,
    ) -> None: ...

    async def reserve_repo_budget(
        self,
        run_id: str,
        repo_id: str,
        *,
        reservation_id: str,
        amount_usd: float,
        now: datetime,
        expires_at: datetime | None = None,
        phase: Phase | None = None,
        lease_fence: int | None = None,
    ) -> None: ...

    async def settle_repo_budget(
        self,
        run_id: str,
        repo_id: str,
        *,
        reservation_id: str,
        reserved_usd: float,
        actual_usd: float,
        now: datetime,
        revalidation: bool = False,
    ) -> None: ...

    # -- evidence ----------------------------------------------------------------------
    async def record_attempt(self, row: AttemptRow) -> None: ...

    async def insert_symbols(self, rows: Sequence[SymbolRow]) -> int: ...

    async def insert_edges(self, rows: Sequence[EdgeRow]) -> int: ...

    async def append_event(self, row: EventRow) -> int: ...


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def _iso(moment: datetime) -> str:
    """One fixed-width UTC rendering, because the reaper compares these instants as TEXT."""
    if moment.tzinfo is None:
        raise RepositoryError(
            f"naive datetime {moment!r}: every persisted instant is tz-aware UTC (§5, §11.5)"
        )
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def _shift(moment: datetime, seconds: int) -> str:
    return _iso(moment + timedelta(seconds=seconds))


def _opt_str(value: object) -> str | None:
    return None if value is None else str(value)


def _opt_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    raise RepositoryError(f"expected an INTEGER column, got {type(value).__name__}: {value!r}")


# --------------------------------------------------------------------------------------
# the four ledger statements — ONE text each, shared by the run-scoped and the nested paths
# --------------------------------------------------------------------------------------
#
# There is one CAS discipline here, not two: the repo statements below are the run statements
# with `repo_id` in the key. Writing a second, subtly different guard for the sub-ledger is how a
# harness ends up with two ledgers that disagree under concurrency.

#: `reservation_expires_at` is DERIVED from `reservations` at v8, never written by hand: one
#: column cannot represent N expiries, and the last writer's value is not the one the reaper
#: needs. These two expressions are that derivation — the earliest surviving HELD expiry — and
#: they are the ONLY difference between the nested statements and the run-only primitives below.
_HELD_EXPIRY_RUN: Final = (
    "(SELECT MIN(r.expires_at) FROM reservations r "
    "  WHERE r.run_id = budget_ledger.run_id AND r.state = 'HELD')"
)
_HELD_EXPIRY_REPO: Final = (
    "(SELECT MIN(r.expires_at) FROM reservations r "
    "  WHERE r.run_id = repo_ledger.run_id AND r.repo_id = repo_ledger.repo_id "
    "    AND r.state = 'HELD')"
)


def _reserve_run_sql(expiry: str) -> str:
    """§6 RESERVATION (normative), verbatim. `rowcount == 1` grants; `rowcount == 0` refuses."""
    return (
        "UPDATE budget_ledger "  # noqa: S608
        f"   SET reserved_usd = reserved_usd + ?, reservation_expires_at = {expiry}, "
        "       updated_at = ? "
        " WHERE run_id = ? AND halted = 0 AND spent_usd + reserved_usd + ? <= max_usd"
    )


def _reserve_repo_sql(expiry: str) -> str:
    """The same statement against the per-repo ceiling. No `halted` predicate: the run row carries
    the run's halt state and is updated in the same transaction, so a halted run refuses there."""
    return (
        "UPDATE repo_ledger "  # noqa: S608
        f"   SET reserved_usd = reserved_usd + ?, reservation_expires_at = {expiry}, "
        "       updated_at = ? "
        " WHERE run_id = ? AND repo_id = ? AND spent_usd + reserved_usd + ? <= max_usd"
    )


#: The run-only primitive mints no `reservations` row, so it has nothing to derive from and binds
#: the expiry literally. Nothing in the orchestrator calls it — `LedgerStore` deliberately omits
#: it — and a hold taken through it carries no identity, so the reaper cannot attribute it.
_RESERVE_RUN_SQL: Final = _reserve_run_sql("?")
#: The nested pair, whose expiry is derived from the row inserted in the same transaction.
_RESERVE_RUN_DERIVED_SQL: Final = _reserve_run_sql(_HELD_EXPIRY_RUN)
_RESERVE_REPO_DERIVED_SQL: Final = _reserve_repo_sql(_HELD_EXPIRY_REPO)

#: One reservation, inserted in the SAME transaction as the two `reserved_usd` growths. `DO
#: NOTHING` rather than a bare INSERT so a re-used id is a typed refusal instead of a raw
#: `IntegrityError` escaping the writer actor.
_INSERT_RESERVATION_SQL: Final = (
    "INSERT INTO reservations (reservation_id, run_id, repo_id, phase, lease_fence, "
    "                          amount_usd, state, expires_at, created_at) "
    "VALUES (?, ?, ?, ?, ?, ?, 'HELD', ?, ?) "
    "ON CONFLICT (reservation_id) DO NOTHING"
)

#: Settlement is the mirror: the row leaves 'HELD' in the same transaction that shrinks both
#: `reserved_usd`. The amount is part of the guard, so a settlement for a different amount than
#: was reserved is refused rather than silently drifting the per-row ledger from the aggregate.
_SETTLE_RESERVATION_SQL: Final = (
    "UPDATE reservations SET state = 'SETTLED' "
    " WHERE reservation_id = ? AND run_id = ? AND repo_id = ? AND state = 'HELD' "
    "   AND ABS(amount_usd - ?) <= ?"
)

_SETTLE_RUN_SQL: Final = (
    "UPDATE budget_ledger "  # noqa: S608
    "   SET reserved_usd = MAX(reserved_usd - ?, 0.0), spent_usd = spent_usd + ?, "
    f"       reservation_expires_at = {_HELD_EXPIRY_RUN}, updated_at = ? "
    " WHERE run_id = ? AND reserved_usd >= ? "
    "   AND spent_usd + ? + MAX(reserved_usd - ?, 0.0) <= max_usd"
)

_SETTLE_REPO_SQL: Final = (
    "UPDATE repo_ledger "  # noqa: S608
    "   SET reserved_usd = MAX(reserved_usd - ?, 0.0), spent_usd = spent_usd + ?, "
    f"       reservation_expires_at = {_HELD_EXPIRY_REPO}, updated_at = ? "
    " WHERE run_id = ? AND repo_id = ? AND reserved_usd >= ? "
    "   AND spent_usd + ? + MAX(reserved_usd - ?, 0.0) <= max_usd"
)

#: §3.5.1: stub rework is a priced cost class inside the repo ceiling, so a REVALIDATION
#: settlement grows `revalidation_usd` alongside `spent_usd` and re-asserts its own sub-ceiling
#: in the same statement — "the rework was free" cannot be asserted again.
_SETTLE_REPO_REVALIDATION_SQL: Final = (
    "UPDATE repo_ledger "  # noqa: S608
    "   SET reserved_usd = MAX(reserved_usd - ?, 0.0), spent_usd = spent_usd + ?, "
    "       revalidation_usd = revalidation_usd + ?, "
    f"       reservation_expires_at = {_HELD_EXPIRY_REPO}, updated_at = ? "
    " WHERE run_id = ? AND repo_id = ? AND reserved_usd >= ? "
    "   AND spent_usd + ? + MAX(reserved_usd - ?, 0.0) <= max_usd "
    "   AND revalidation_usd + ? <= revalidation_max_usd"
)

# ---- the reaper's four statements. One transaction, in this order (§6, v8). ----------
# S608 (noqa'd per statement below): every interpolated fragment is a module constant defined
# just above — `_HELD_EXPIRY_*`, `_EXPIRED_HELD_PREDICATE` — and never user input. Interpolating
# them IS the point: the expiry derivation and the "expired AND still held" predicate have ONE
# definition each, and a restated predicate is how a reaper comes to release rows its own SUM
# did not count.
#
# Order is load-bearing: the sums and the fence bumps read the rows WHILE they are still 'HELD',
# so the state flip has to come last. Every one of them is scoped to `run_id` and to
# "expired AND still held", which is what leaves a live worker's hold untouched.

_EXPIRED_HELD_PREDICATE: Final = (
    "state = 'HELD' AND expires_at IS NOT NULL AND expires_at < ?"
)

#: The owning phase's fence, bumped in the same transaction as the release: the reaped holder's
#: next fenced write matches zero rows, so it cannot settle a reservation it no longer holds.
_REAP_RESERVATION_FENCES_SQL: Final = (
    "UPDATE phases SET lease_fence = lease_fence + 1, updated_at = ? "  # noqa: S608
    " WHERE run_id = ? AND EXISTS ("
    "   SELECT 1 FROM reservations r "
    "    WHERE r.run_id = phases.run_id AND r.repo_id = phases.repo_id "
    f"      AND r.phase = phases.phase AND r.{_EXPIRED_HELD_PREDICATE})"
)

#: Subtract EXACTLY the expired holders' dollars. `SUM` over the identified rows is the whole
#: point of the table: the pre-v8 aggregate could only be released whole, taking the live
#: workers' holds with it. `MAX(…, 0.0)` keeps float drift off the `reserved_usd >= 0.0` CHECK.
_REAP_REPO_LEDGER_SQL: Final = (
    "UPDATE repo_ledger "  # noqa: S608
    "   SET reserved_usd = MAX(reserved_usd - COALESCE(("
    "         SELECT SUM(r.amount_usd) FROM reservations r "
    "          WHERE r.run_id = repo_ledger.run_id AND r.repo_id = repo_ledger.repo_id "
    f"           AND r.{_EXPIRED_HELD_PREDICATE}), 0.0), 0.0), "
    "       reservation_expires_at = ("
    "         SELECT MIN(r.expires_at) FROM reservations r "
    "          WHERE r.run_id = repo_ledger.run_id AND r.repo_id = repo_ledger.repo_id "
    "            AND r.state = 'HELD' AND (r.expires_at IS NULL OR r.expires_at >= ?)), "
    "       updated_at = ? "
    " WHERE run_id = ? AND EXISTS ("
    "   SELECT 1 FROM reservations r "
    "    WHERE r.run_id = repo_ledger.run_id AND r.repo_id = repo_ledger.repo_id "
    f"      AND r.{_EXPIRED_HELD_PREDICATE})"
)

_REAP_RUN_LEDGER_SQL: Final = (
    "UPDATE budget_ledger "  # noqa: S608
    "   SET reserved_usd = MAX(reserved_usd - COALESCE(("
    "         SELECT SUM(r.amount_usd) FROM reservations r "
    f"          WHERE r.run_id = budget_ledger.run_id AND r.{_EXPIRED_HELD_PREDICATE}"
    "       ), 0.0), 0.0), "
    "       reservation_expires_at = ("
    "         SELECT MIN(r.expires_at) FROM reservations r "
    "          WHERE r.run_id = budget_ledger.run_id AND r.state = 'HELD' "
    "            AND (r.expires_at IS NULL OR r.expires_at >= ?)), "
    "       updated_at = ? "
    " WHERE run_id = ? AND EXISTS ("
    "   SELECT 1 FROM reservations r "
    f"    WHERE r.run_id = budget_ledger.run_id AND r.{_EXPIRED_HELD_PREDICATE})"
)

_REAP_RESERVATIONS_SQL: Final = (
    "UPDATE reservations SET state = 'EXPIRED' "  # noqa: S608
    f" WHERE run_id = ? AND {_EXPIRED_HELD_PREDICATE}"
)


async def _probe_run_ledger(conn: aiosqlite.Connection, run_id: str) -> tuple[object, ...] | None:
    """Read the run ledger's numbers **inside the refusing transaction**: the refusal says why."""
    async with conn.execute(
        "SELECT spent_usd, reserved_usd, max_usd, halted FROM budget_ledger WHERE run_id = ?",
        (run_id,),
    ) as probe:
        found = await probe.fetchone()
    return None if found is None else tuple(found)


async def _probe_repo_ledger(
    conn: aiosqlite.Connection, run_id: str, repo_id: str
) -> tuple[object, ...] | None:
    async with conn.execute(
        "SELECT spent_usd, reserved_usd, max_usd, revalidation_usd, revalidation_max_usd "
        "  FROM repo_ledger WHERE run_id = ? AND repo_id = ?",
        (run_id, repo_id),
    ) as probe:
        found = await probe.fetchone()
    return None if found is None else tuple(found)


def _settle_run_params(
    run_id: str, *, reserved_usd: float, actual_usd: float, now: datetime
) -> tuple[object, ...]:
    """Bindings for `_SETTLE_RUN_SQL`. One place, so the nested settle cannot drift from the
    run-scoped one by a reordered parameter that SQLite would accept without complaint."""
    return (
        reserved_usd,
        actual_usd,
        _iso(now),
        run_id,
        reserved_usd - _USD_EPSILON,
        actual_usd,
        reserved_usd,
    )


def _settle_refused(run_id: str, reserved_usd: float, actual_usd: float) -> str:
    return (
        f"budget settlement REFUSED for run {run_id}: cannot move ${reserved_usd:.4f} "
        f"reserved into ${actual_usd:.4f} spent — no such reservation, or the result "
        "would breach max_usd (§6)"
    )


def _reserve_refused(run_id: str, amount_usd: float, ledger: tuple[object, ...] | None) -> str:
    return f"budget reservation of ${amount_usd:.4f} REFUSED for run {run_id}: " + (
        "no budget_ledger row exists"
        if ledger is None
        else (
            f"spent={ledger[0]} reserved={ledger[1]} max={ledger[2]} halted={ledger[3]} "
            "— fail-closed is a constraint, not a convention (§6)"
        )
    )


def _repo_reserve_refused(
    run_id: str, repo_id: str, amount_usd: float, ledger: tuple[object, ...] | None
) -> str:
    return (
        f"repo budget reservation of ${amount_usd:.4f} REFUSED for {repo_id} in run {run_id}: "
        + (
            "no repo_ledger row exists — a repo with no durable ledger cannot be paced"
            if ledger is None
            else (
                f"spent={ledger[0]} reserved={ledger[1]} max={ledger[2]} — the per-repo ceiling "
                "is enforced, not asserted (§3.5, §6)"
            )
        )
    )


def _repo_settle_refused(
    run_id: str,
    repo_id: str,
    reserved_usd: float,
    actual_usd: float,
    ledger: tuple[object, ...] | None,
) -> str:
    return (
        f"repo budget settlement REFUSED for {repo_id} in run {run_id}: cannot move "
        f"${reserved_usd:.4f} reserved into ${actual_usd:.4f} spent — no such reservation, or the "
        f"result would breach the repo ceiling (§3.5). ledger={ledger}"
    )


# --------------------------------------------------------------------------------------
# §11.5 step 5 — the demotion write (ADR-0077)
# --------------------------------------------------------------------------------------

#: Every `phases` row for one repo, status only. Read *inside* the demotion's own transaction:
#: the floor was computed from a read-only handle outside it, and the `REQUIRES_HUMAN_INTERVENTION`
#: refusal below must be decided against the rows the UPDATE will actually hit.
_DEMOTE_SELECT_SQL: Final = "SELECT phase, status FROM phases WHERE run_id = ? AND repo_id = ?"

#: The demotion itself. `attempts` is ABSENT from the SET list and that absence is the feature:
#: `complete_phase` is the only writer of `phases.attempts` anywhere in `src/`, so a demotion that
#: does not call it cannot spend a rung of the ladder. `AND status = 'SUCCEEDED'` keeps the
#: statement true on its own terms — a row that changed under us matches zero rows instead of
#: silently overwriting a status `demote()` never adjudicated.
_DEMOTE_PHASE_SQL: Final = (
    "UPDATE phases SET status = ?, updated_at = ? "
    " WHERE run_id = ? AND repo_id = ? AND phase = ? AND status = 'SUCCEEDED'"
)

#: One `findings` row per demoted phase. Per PHASE, not per repo: `PhaseDemotion.payload()`'s own
#: docstring records the hazard — `cli._note_finding` fingerprints on `(run_id, repo_id, kind)`
#: alone, so three demoted phases of one repo would UPSERT into ONE row and two demotions would
#: vanish. The fingerprint below carries the phase, so §11.7 idempotency still holds (a second
#: resume demoting the same phase converges on the same row) without collapsing distinct phases.
_DEMOTE_FINDING_SQL: Final = (
    "INSERT INTO findings (run_id, repo_id, kind, severity, fingerprint, payload, created_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT (run_id, IFNULL(repo_id, ''), kind, fingerprint) "
    "DO UPDATE SET payload = excluded.payload, created_at = excluded.created_at"
)


def _demotion_fingerprint(run_id: str, repo_id: str, phase: Phase) -> str:
    """Semantic identity of one demotion: this repo, this phase, in this run."""
    return sha256_text("\x00".join((run_id, repo_id, PHASE_DEMOTED_KIND, str(int(phase)))))


# --------------------------------------------------------------------------------------
# the SQLite implementation
# --------------------------------------------------------------------------------------


class SqliteStateRepository:
    """`StateRepository` over aiosqlite: reads on a `mode=ro` handle, writes via `StateWriter`.

    Both collaborators are **injected**. This class never calls `writable_connection()`, so it
    cannot become a second writer, and a caller that hands it only a read connection gets a
    read-only object by construction (§11.5).
    """

    def __init__(self, *, writer: StateWriter, read_conn: aiosqlite.Connection) -> None:
        self._writer = writer
        self._read = read_conn

    # ==================================================================================
    # reads — the injected mode=ro connection
    # ==================================================================================

    async def get_phase(self, run_id: str, repo_id: str, phase: Phase) -> PhaseRow | None:
        sql = (
            "SELECT run_id, repo_id, phase, status, attempts, max_attempts, lease_owner, "
            "       lease_fence, lease_expires_at, last_error, updated_at "
            "  FROM phases WHERE run_id = ? AND repo_id = ? AND phase = ?"
        )
        async with self._read.execute(sql, (run_id, repo_id, int(phase))) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return PhaseRow(
            run_id=str(row[0]),
            repo_id=str(row[1]),
            phase=Phase(int(row[2])),
            status=RepoStatus(str(row[3])),
            attempts=int(row[4]),
            max_attempts=int(row[5]),
            lease_owner=_opt_str(row[6]),
            lease_fence=int(row[7]),
            lease_expires_at=_opt_str(row[8]),
            last_error=_opt_str(row[9]),
            updated_at=str(row[10]),
        )

    async def get_budget(self, run_id: str) -> BudgetLedgerRow | None:
        sql = (
            "SELECT run_id, spent_usd, reserved_usd, max_usd, halted, "
            "       reservation_expires_at, updated_at "
            "  FROM budget_ledger WHERE run_id = ?"
        )
        async with self._read.execute(sql, (run_id,)) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return BudgetLedgerRow(
            run_id=str(row[0]),
            spent_usd=float(row[1]),
            reserved_usd=float(row[2]),
            max_usd=float(row[3]),
            halted=bool(row[4]),
            reservation_expires_at=_opt_str(row[5]),
            updated_at=str(row[6]),
        )

    # -- §11 "Query results": streamed, never folded into a list ------------------------

    async def get_reservation(self, run_id: str, reservation_id: str) -> ReservationRow | None:
        """One hold, by id. Bounded by construction — a reservation is read to answer "is this
        still mine", never swept — so it returns a row rather than an `iter_*` (§11)."""
        sql = (
            "SELECT reservation_id, run_id, repo_id, phase, lease_fence, amount_usd, state, "
            "       expires_at, created_at "
            "  FROM reservations WHERE run_id = ? AND reservation_id = ?"
        )
        async with self._read.execute(sql, (run_id, reservation_id)) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        phase = _opt_int(row[3])
        return ReservationRow(
            reservation_id=str(row[0]),
            run_id=str(row[1]),
            repo_id=str(row[2]),
            phase=None if phase is None else Phase(phase),
            lease_fence=_opt_int(row[4]),
            amount_usd=float(row[5]),
            state=str(row[6]),
            expires_at=_opt_str(row[7]),
            created_at=str(row[8]),
        )

    async def iter_symbols(self, run_id: str) -> AsyncIterator[SymbolRow]:
        """Stream `symbols` (~10⁵ rows/fleet). A `list` here is a §11 review-blocking defect."""
        sql = (
            "SELECT symbol_id, run_id, repo_id, fqn, kind, path, line, language, "
            "       is_definition, exported "
            "  FROM symbols WHERE run_id = ? ORDER BY symbol_id"
        )
        async with self._read.execute(sql, (run_id,)) as cursor:
            cursor.arraysize = READ_ARRAYSIZE
            while batch := await cursor.fetchmany():
                for row in batch:
                    yield SymbolRow(
                        symbol_id=int(row[0]),
                        run_id=str(row[1]),
                        repo_id=str(row[2]),
                        fqn=str(row[3]),
                        kind=str(row[4]),
                        path=str(row[5]),
                        line=int(row[6]),
                        language=str(row[7]),
                        is_definition=bool(row[8]),
                        exported=bool(row[9]),
                    )

    async def iter_edges(self, run_id: str) -> AsyncIterator[EdgeRow]:
        """Stream `edges` (unbounded in the manifest fan-out). Same rule as `iter_symbols`."""
        sql = (
            "SELECT edge_id, edge_key, run_id, src_kind, src_id, dst_kind, dst_id, "
            "       dst_coord_key, kind, base_confidence, confidence, ambiguous, "
            "       ordering_suppressed, evidence_path, evidence_line, detected_at "
            "  FROM edges WHERE run_id = ? ORDER BY edge_id"
        )
        async with self._read.execute(sql, (run_id,)) as cursor:
            cursor.arraysize = READ_ARRAYSIZE
            while batch := await cursor.fetchmany():
                for row in batch:
                    yield EdgeRow(
                        edge_id=int(row[0]),
                        edge_key=str(row[1]),
                        run_id=str(row[2]),
                        src_kind=str(row[3]),
                        src_id=str(row[4]),
                        dst_kind=str(row[5]),
                        dst_id=_opt_str(row[6]),
                        dst_coord_key=str(row[7]),
                        kind=str(row[8]),
                        base_confidence=float(row[9]),
                        confidence=float(row[10]),
                        ambiguous=bool(row[11]),
                        ordering_suppressed=bool(row[12]),
                        evidence_path=str(row[13]),
                        evidence_line=int(row[14]),
                        detected_at=str(row[15]),
                    )

    async def iter_events(self, run_id: str) -> AsyncIterator[EventRow]:
        """Stream `events` ORDER BY `seq` — the ordering key, never `ts` (§6, §11.5)."""
        sql = (
            "SELECT event_id, run_id, seq, ts, repo_id, phase, level, event, event_uid, payload "
            "  FROM events WHERE run_id = ? ORDER BY seq"
        )
        async with self._read.execute(sql, (run_id,)) as cursor:
            cursor.arraysize = READ_ARRAYSIZE
            while batch := await cursor.fetchmany():
                for row in batch:
                    raw_phase = _opt_int(row[5])
                    yield EventRow(
                        event_id=int(row[0]),
                        run_id=str(row[1]),
                        seq=int(row[2]),
                        ts=str(row[3]),
                        repo_id=_opt_str(row[4]),
                        phase=None if raw_phase is None else Phase(raw_phase),
                        level=str(row[6]),
                        event=str(row[7]),
                        event_uid=str(row[8]),
                        payload=str(row[9]),
                    )

    async def iter_attempts(self, run_id: str) -> AsyncIterator[AttemptRow]:
        """Stream `attempts` (~10⁴/run, tails capped). Same rule as `iter_symbols`."""
        sql = (
            "SELECT attempt_id, run_id, repo_id, task_id, phase, attempt, revalidation_round, "
            "       tier, context_policy, approach_signature, command, command_sha256, "
            "       retry_ordinal, exit_code, failure_class, duration_ms, stdout_tail, "
            "       stderr_tail, cost_usd, patch_id, commit_sha, already_applied, "
            "       started_at, finished_at "
            "  FROM attempts WHERE run_id = ? ORDER BY repo_id, phase, attempt, retry_ordinal"
        )
        async with self._read.execute(sql, (run_id,)) as cursor:
            cursor.arraysize = READ_ARRAYSIZE
            while batch := await cursor.fetchmany():
                for row in batch:
                    yield AttemptRow(
                        attempt_id=str(row[0]),
                        run_id=str(row[1]),
                        repo_id=str(row[2]),
                        task_id=_opt_str(row[3]),
                        phase=Phase(int(row[4])),
                        attempt=int(row[5]),
                        revalidation_round=int(row[6]),
                        tier=str(row[7]),
                        context_policy=_opt_str(row[8]),
                        approach_signature=str(row[9]),
                        command=str(row[10]),
                        command_sha256=str(row[11]),
                        retry_ordinal=int(row[12]),
                        exit_code=_opt_int(row[13]),
                        failure_class=_opt_str(row[14]),
                        duration_ms=int(row[15]),
                        stdout_tail=str(row[16]),
                        stderr_tail=str(row[17]),
                        cost_usd=float(row[18]),
                        patch_id=_opt_str(row[19]),
                        commit_sha=_opt_str(row[20]),
                        already_applied=bool(row[21]),
                        started_at=str(row[22]),
                        finished_at=str(row[23]),
                    )

    # ==================================================================================
    # writes — every one of them through the injected single writer
    # ==================================================================================

    async def upsert_run(
        self, run_id: str, *, started_at: datetime, config_sha256: str, harness_version: str
    ) -> None:
        sql = (
            "INSERT INTO runs (run_id, started_at, config_sha256, harness_version) "
            "VALUES (?, ?, ?, ?) ON CONFLICT (run_id) DO NOTHING"
        )
        params = (run_id, _iso(started_at), config_sha256, harness_version)

        async def unit(conn: aiosqlite.Connection) -> None:
            await conn.execute(sql, params)

        await self._writer.submit(unit)

    async def upsert_repo(
        self, repo_id: str, *, name: str, url: str, now: datetime, dest_path: str | None = None
    ) -> None:
        sql = (
            "INSERT INTO repos (repo_id, name, url, dest_path, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (repo_id) DO UPDATE SET name = excluded.name, url = excluded.url, "
            "    dest_path = excluded.dest_path, updated_at = excluded.updated_at"
        )
        params = (repo_id, name, url, dest_path, _iso(now))

        async def unit(conn: aiosqlite.Connection) -> None:
            await conn.execute(sql, params)

        await self._writer.submit(unit)

    async def upsert_phase(
        self, run_id: str, repo_id: str, phase: Phase, *, now: datetime, max_attempts: int = 3
    ) -> None:
        """Create the phase row at PENDING. Never touches `lease_fence` — only the CAS paths do."""
        sql = (
            "INSERT INTO phases (run_id, repo_id, phase, max_attempts, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (run_id, repo_id, phase) DO UPDATE SET "
            "    max_attempts = excluded.max_attempts, updated_at = excluded.updated_at"
        )
        params = (run_id, repo_id, int(phase), max_attempts, _iso(now))

        async def unit(conn: aiosqlite.Connection) -> None:
            await conn.execute(sql, params)

        await self._writer.submit(unit)

    async def upsert_task(
        self,
        task_id: str,
        *,
        run_id: str,
        repo_id: str,
        phase: Phase,
        kind: TaskKind,
        dest_path: str,
        created_at: datetime,
        max_attempts: int = 3,
    ) -> None:
        """Re-planning UPSERTs the **plan** columns only.

        §6: never `status`/`claimed_by`/`fence_token` — those move solely through the claim CAS.
        A re-plan that reset `status` to PENDING would hand a live task to a second worker.

        HOIST and REVALIDATE carry a `contract_id`/`revalidation_key` the schema CHECKs demand;
        planting one through this method would raise an opaque IntegrityError, so it is refused
        here with the reason (CLAUDE.md Rule 11).
        """
        if kind in (TaskKind.HOIST, TaskKind.REVALIDATE):
            raise RepositoryError(
                f"{kind} tasks carry a contract_id/revalidation_key discriminator that this "
                "method does not take (§6 tasks CHECKs); plan them through their own path"
            )
        ladder = _DEFAULT_LADDERS[max_attempts]
        sql = (
            "INSERT INTO tasks (task_id, run_id, repo_id, phase, kind, dest_path, max_attempts, "
            "                   ladder, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (run_id, repo_id, phase, kind, IFNULL(contract_id, ''), "
            "             IFNULL(revalidation_key, '')) DO UPDATE SET "
            "    dest_path = excluded.dest_path, max_attempts = excluded.max_attempts, "
            "    ladder = excluded.ladder"
        )
        params = (
            task_id,
            run_id,
            repo_id,
            int(phase),
            str(kind),
            dest_path,
            max_attempts,
            ladder,
            _iso(created_at),
        )

        async def unit(conn: aiosqlite.Connection) -> None:
            await conn.execute(sql, params)

        await self._writer.submit(unit)

    # -- primitive 1 -------------------------------------------------------------------

    async def claim_next_task(
        self, run_id: str, *, worker: str, now: datetime, lease_ttl_s: int
    ) -> ClaimedTask | None:
        """The §6 CLAIM, verbatim: ONE compare-and-swap. `None` means somebody else won.

        The inner `SELECT` picks the candidate and the outer `AND status = 'PENDING'` is the
        swap — so a racer whose candidate was taken between the two matches zero rows and gets
        `None` instead of a second copy of the same repo's work.
        """
        sql = (
            "UPDATE tasks "
            "   SET status = 'CLAIMED', claimed_by = ?, lease_expires_at = ?, "
            "       fence_token = fence_token + 1 "
            " WHERE task_id = (SELECT task_id FROM tasks "
            "                   WHERE run_id = ? AND status = 'PENDING' "
            "                   ORDER BY created_at, task_id LIMIT 1) "
            "   AND status = 'PENDING' "
            "RETURNING task_id, run_id, repo_id, phase, kind, dest_path, fence_token, "
            "          lease_expires_at, max_attempts"
        )
        params = (worker, _shift(now, lease_ttl_s), run_id)

        async def unit(conn: aiosqlite.Connection) -> ClaimedTask | None:
            async with conn.execute(sql, params) as cursor:
                rows = list(await cursor.fetchall())
            if not rows:  # rowcount == 0: another worker owns it
                return None
            row = rows[0]
            return ClaimedTask(
                task_id=str(row[0]),
                run_id=str(row[1]),
                repo_id=str(row[2]),
                phase=Phase(int(row[3])),
                kind=TaskKind(str(row[4])),
                dest_path=str(row[5]),
                fence_token=int(row[6]),
                lease_expires_at=str(row[7]),
                max_attempts=int(row[8]),
            )

        return await self._writer.submit(unit)

    # -- primitives 2 and 3 ------------------------------------------------------------

    async def acquire_phase_lease(
        self,
        run_id: str,
        repo_id: str,
        phase: Phase,
        *,
        owner: str,
        now: datetime,
        lease_ttl_s: int,
    ) -> int | None:
        """Take a PENDING phase to RUNNING and return the fence granted, or `None` if lost.

        `owner` is the §6 identity `'{host}:{container_id}:{pid}:{boot_uuid}'` — a bare pid is
        not an identity, because fresh PID namespaces reuse low pids.
        """
        stamp = _iso(now)
        sql = (
            "UPDATE phases "
            "   SET status = 'RUNNING', lease_owner = ?, lease_fence = lease_fence + 1, "
            "       lease_expires_at = ?, heartbeat_at = ?, "
            "       started_at = COALESCE(started_at, ?), updated_at = ? "
            " WHERE run_id = ? AND repo_id = ? AND phase = ? AND status = 'PENDING' "
            "RETURNING lease_fence"
        )
        params = (
            owner,
            _shift(now, lease_ttl_s),
            stamp,
            stamp,
            stamp,
            run_id,
            repo_id,
            int(phase),
        )

        async def unit(conn: aiosqlite.Connection) -> int | None:
            async with conn.execute(sql, params) as cursor:
                rows = list(await cursor.fetchall())
            return None if not rows else int(rows[0][0])

        return await self._writer.submit(unit)

    async def renew_phase_lease(
        self,
        run_id: str,
        repo_id: str,
        phase: Phase,
        *,
        fence: int,
        now: datetime,
        lease_ttl_s: int,
    ) -> None:
        """Extend a lease the caller still holds. `fence` is required — see the module docstring.

        Raises `LeaseStolenError` if the reaper has already bumped the fence: renewing a lease
        somebody else owns is precisely the write that must not silently succeed.
        """
        stamp = _iso(now)
        sql = (
            "UPDATE phases SET lease_expires_at = ?, heartbeat_at = ?, updated_at = ? "
            " WHERE run_id = ? AND repo_id = ? AND phase = ? "
            "   AND status = 'RUNNING' AND lease_fence = ?"
        )
        params = (
            _shift(now, lease_ttl_s),
            stamp,
            stamp,
            run_id,
            repo_id,
            int(phase),
            fence,
        )

        async def unit(conn: aiosqlite.Connection) -> int:
            cursor = await conn.execute(sql, params)
            return int(cursor.rowcount)

        if await self._writer.submit(unit) == 0:
            raise LeaseStolenError(_stolen(run_id, repo_id, phase, fence, "renew"))

    async def complete_phase(
        self,
        run_id: str,
        repo_id: str,
        phase: Phase,
        *,
        fence: int,
        status: RepoStatus,
        now: datetime,
        last_error: str | None = None,
    ) -> RepoStatus:
        """The fenced terminal write. Increments `attempts` and returns the status actually set.

        §6: "`attempts` only ever increments, and the increment that reaches `max_attempts` sets
        `status='REQUIRES_HUMAN_INTERVENTION'` in the same statement". That escalation applies
        to the retry hand-back (`status=PENDING`) only — a phase that SUCCEEDED on its last
        allowed attempt succeeded. Doing it in one statement is the point: a read-then-decide
        would let the ladder run one rung past its ceiling.

        Raises `LeaseStolenError` on `rowcount == 0` (CLAUDE.md Rule 11 — the caller must abort
        without touching git).
        """
        escalates = 1 if status is RepoStatus.PENDING else 0
        sql = (
            "UPDATE phases "
            "   SET attempts = attempts + 1, "
            "       status = CASE WHEN ? = 1 AND attempts + 1 >= max_attempts "
            "                     THEN 'REQUIRES_HUMAN_INTERVENTION' ELSE ? END, "
            "       last_error = ?, lease_owner = NULL, lease_expires_at = NULL, "
            "       heartbeat_at = NULL, updated_at = ? "
            " WHERE run_id = ? AND repo_id = ? AND phase = ? AND lease_fence = ? "
            "RETURNING status"
        )
        params = (
            escalates,
            str(status),
            last_error,
            _iso(now),
            run_id,
            repo_id,
            int(phase),
            fence,
        )

        async def unit(conn: aiosqlite.Connection) -> str | None:
            async with conn.execute(sql, params) as cursor:
                rows = list(await cursor.fetchall())
            return None if not rows else str(rows[0][0])

        written = await self._writer.submit(unit)
        if written is None:
            raise LeaseStolenError(_stolen(run_id, repo_id, phase, fence, "complete"))
        return RepoStatus(written)

    async def demote_to_floor(
        self,
        run_id: str,
        repo_id: str,
        *,
        floor: Phase,
        reason: str,
        now: datetime,
    ) -> tuple[PhaseDemotion, ...]:
        """Apply the §11.5 step-5 demotion for one repo, in ONE transaction (ADR-0077).

        `floor` is what `orchestrator/reentry.phase_floor` computed; this method does not
        recompute it and never consults `preconditions_hold`. Everything from `floor` up to
        `Phase.VERIFY` is re-entry territory, so for each phase in that span it does two things
        and one thing conditionally:

        * every `SUCCEEDED` row in the span goes to `PENDING` through **`demote()`** — never
          through `transition(..., resume=True)`. `transition()` returns a bare status and emits
          nothing, so a demotion made through it is invisible to whoever reads the run;
          `demote()` returns the status *and* the `PhaseDemotion` the caller owes, and this
          method discharges that obligation by writing one `PhaseDemoted` finding per demoted
          phase in the same transaction as the status change. (`docs/SPEC.md` Constraint 7 read
          the other way until `f02d124` — it named `transition(..., resume=True)` as the demotion
          path, which would have made every demotion in the fleet silent while every status
          assertion still passed. The test below asserts on the finding row for that reason.)
        * the `checkpoints` row for the phase is deleted, because a checkpoint for a phase that
          is about to be re-run is a lie about work the run no longer claims;
        * a non-`SUCCEEDED` row in the span is left as it is. `PENDING`, `RUNNING` and `BLOCKED`
          have no landed work to discard, and `demote()` refuses all three precisely so a
          `PhaseDemoted` cannot be minted for them.

        Returns the demotions applied, in phase order — empty when nothing was demotable.

        **`REQUIRES_HUMAN_INTERVENTION` short-circuits the whole repo, and the check is re-read
        here rather than trusted from the floor computation.** `phase_floor` already returns
        `None` for such a repo, but it read through the `mode=ro` handle outside this
        transaction; §12 item 46 (ii) requires that no automatic sweep — `fleet resume` named
        among them — can move a repo out of that state, and a refusal that lives only in the
        caller is a refusal the next caller can forget. One `SELECT` inside `BEGIN IMMEDIATE`
        makes it a property of the write.

        **`DEGRADED` rows in the span are left completely alone — status *and* checkpoint.**
        ADR-0077 §5: `DEGRADED` leaves the machine only through a budgeted revalidation round
        (§3.5.1), so demoting it to `PENDING` would spend that budget by the back door with no
        round recorded. Deleting its checkpoint is the same trespass one level down — it forces
        the revalidation round to start from nothing, which is the cost the budget was sized
        against — so the deletion skips it too. THE HAZARD THIS LEAVES, stated rather than
        hidden: a `DEGRADED` phase above the floor keeps a checkpoint built on output the
        demotion is about to re-generate, so the round that eventually revalidates it resumes
        from a stale anchor. ADR-0077 §5 forbids the alternative; nothing here is claimed to
        resolve the tension.

        `attempts` is retained for every phase, on every path: the SET list does not name the
        column and this method does not call `complete_phase`, which is the only writer of
        `phases.attempts` in `src/`. No lease fence is carried, deliberately — resume holds no
        lease, and step 3 has already reclaimed every stale one and bumped its fence, so a fence
        nobody granted would be a fence in name only.
        """
        stamp = _iso(now)
        span = tuple(phase for phase in Phase if phase >= floor)

        async def unit(conn: aiosqlite.Connection) -> tuple[PhaseDemotion, ...]:
            async with conn.execute(_DEMOTE_SELECT_SQL, (run_id, repo_id)) as cursor:
                rows = {
                    Phase(int(row[0])): RepoStatus(str(row[1])) for row in await cursor.fetchall()
                }
            if RepoStatus.REQUIRES_HUMAN_INTERVENTION in rows.values():
                return ()

            demotions: list[PhaseDemotion] = []
            for phase in span:
                # A missing row is the schema default, PENDING — nothing landed, nothing to
                # discard. `demote()` would refuse it, so it is filtered here rather than caught.
                if rows.get(phase) is not RepoStatus.SUCCEEDED:
                    continue
                new_status, record = demote(
                    RepoStatus.SUCCEEDED, repo_id=repo_id, phase=phase, reason=reason
                )
                await conn.execute(
                    _DEMOTE_PHASE_SQL,
                    (str(new_status), stamp, run_id, repo_id, int(phase)),
                )
                demotions.append(record)

            await checkpoints.delete_in_unit(
                conn,
                run_id=run_id,
                repo_id=repo_id,
                phases=[p for p in span if rows.get(p) is not RepoStatus.DEGRADED],
            )

            for record in demotions:
                await conn.execute(
                    _DEMOTE_FINDING_SQL,
                    (
                        run_id,
                        repo_id,
                        PHASE_DEMOTED_KIND,
                        "warn",
                        _demotion_fingerprint(run_id, repo_id, record.phase),
                        redact_text(json.dumps(record.payload(), sort_keys=True)),
                        stamp,
                    ),
                )
            return tuple(demotions)

        return await self._writer.submit(unit)

    async def reap_expired_phase_leases(self, run_id: str, *, now: datetime) -> int:
        """The §6 REAPER — leases AND budget reservations, in ONE transaction (v8).

        Returns the count of reclaimed *leases*, so the caller still emits one `LeaseExpired`
        event each. The fence bump is what makes the reclaim safe: the old holder's next write
        matches zero rows. `lease_expires_at IS NOT NULL` is redundant against SQL's NULL
        semantics and kept for the reader — a NULL expiry is never `<` anything, which is exactly
        why a nullable `heartbeat_ttl_seconds` would make a crashed worker unreapable (§6).

        **The budget half, and why it needs `reservations` to exist at all.** §6 requires the
        expired holds to be released in the same transaction that bumps the owning
        `phases.lease_fence`. Against v7's schema that was unimplementable: `reserved_usd` was a
        scalar aggregate and `reservation_expires_at` a single column each reserver overwrote, so
        "release the expired reservation" could only mean "release everything" — zeroing every
        LIVE worker's hold, under-counting what the run has committed, and letting it overspend.
        v8's per-row identity turns it into `SUM(amount_usd)` over exactly the expired holders.

        Everything below runs inside the writer's single `BEGIN IMMEDIATE` (§11.5), so the
        release and the fence bump commit together or not at all. A worker whose hold is reaped
        here cannot then settle it twice over: its `reservations` row is no longer `HELD`
        (`ReservationRefusedError`) *and* its fenced phase writes match zero rows
        (`LeaseStolenError`).

        Statement order is load-bearing: the sums and the fence bumps read the rows while they
        are still `HELD`, so the flip to `EXPIRED` is last.
        """
        lease_sql = (
            "UPDATE phases "
            "   SET status = 'PENDING', lease_owner = NULL, lease_fence = lease_fence + 1, "
            "       heartbeat_at = NULL, lease_expires_at = NULL, updated_at = ? "
            " WHERE run_id = ? AND status = 'RUNNING' "
            "   AND lease_expires_at IS NOT NULL AND lease_expires_at < ?"
        )
        stamp = _iso(now)

        async def unit(conn: aiosqlite.Connection) -> int:
            cursor = await conn.execute(lease_sql, (stamp, run_id, stamp))
            reclaimed = int(cursor.rowcount)
            await conn.execute(_REAP_RESERVATION_FENCES_SQL, (stamp, run_id, stamp))
            await conn.execute(_REAP_REPO_LEDGER_SQL, (stamp, stamp, stamp, run_id, stamp))
            await conn.execute(_REAP_RUN_LEDGER_SQL, (stamp, stamp, stamp, run_id, stamp))
            await conn.execute(_REAP_RESERVATIONS_SQL, (run_id, stamp))
            return reclaimed

        return await self._writer.submit(unit)

    # -- primitive 4 -------------------------------------------------------------------

    async def open_budget_ledger(self, run_id: str, *, max_usd: float, now: datetime) -> None:
        sql = (
            "INSERT INTO budget_ledger (run_id, max_usd, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT (run_id) DO UPDATE SET max_usd = excluded.max_usd, "
            "    updated_at = excluded.updated_at"
        )
        params = (run_id, max_usd, _iso(now))

        async def unit(conn: aiosqlite.Connection) -> None:
            await conn.execute(sql, params)

        await self._writer.submit(unit)

    async def reserve_budget(
        self, run_id: str, *, amount_usd: float, now: datetime, expires_at: datetime | None = None
    ) -> None:
        """The §6 RESERVATION, verbatim: one conditional CAS. Never SELECT-then-UPDATE.

        Read-then-write lets 12 workers each reserve $3 against a $497/$500 ledger and all 12
        writes succeed. `rowcount == 0` is a **refusal**, so it raises `BudgetRefusedError`
        carrying the ledger's actual numbers — a caller cannot ignore an exception the way it
        can ignore a `False`.
        """
        params = (
            amount_usd,
            None if expires_at is None else _iso(expires_at),
            _iso(now),
            run_id,
            amount_usd,
        )

        async def unit(conn: aiosqlite.Connection) -> tuple[int, tuple[object, ...] | None]:
            cursor = await conn.execute(_RESERVE_RUN_SQL, params)
            changed = int(cursor.rowcount)
            if changed:
                return changed, None
            # Same transaction, pure SQL: the refusal says WHY, not just "no".
            return changed, await _probe_run_ledger(conn, run_id)

        changed, ledger = await self._writer.submit(unit)
        if changed == 0:
            raise BudgetRefusedError(_reserve_refused(run_id, amount_usd, ledger))

    async def settle_budget(
        self, run_id: str, *, reserved_usd: float, actual_usd: float, now: datetime
    ) -> None:
        """Move a held reservation into spend in ONE statement (§6).

        `MAX(reserved_usd - ?, 0.0)` keeps float drift from tripping the ledger's
        `CHECK (reserved_usd >= 0.0)` on an exact settle, and the ceiling is re-asserted here so
        an over-stated `actual_usd` is refused rather than committed.
        """
        params = _settle_run_params(
            run_id, reserved_usd=reserved_usd, actual_usd=actual_usd, now=now
        )

        async def unit(conn: aiosqlite.Connection) -> int:
            cursor = await conn.execute(_SETTLE_RUN_SQL, params)
            return int(cursor.rowcount)

        if await self._writer.submit(unit) == 0:
            raise BudgetRefusedError(_settle_refused(run_id, reserved_usd, actual_usd))

    async def halt_budget_ledger(self, run_id: str, *, now: datetime) -> None:
        """Write `budget_ledger.halted = 1`. **This is the durable half of "fail-closed".**

        Before this method existed the column was only ever READ: `CostLedger.halt()` set an
        in-process flag, so the halt died with the process that raised it and a resumed run
        cheerfully kept spending past the ceiling that had already stopped it once.

        Sticky by construction: there is no argument that writes `0`, and no `AND halted = 0`
        predicate, so re-halting an already-halted run is idempotent rather than a refusal.
        §11.2 allows exactly one way out — an audited `fleet resume --raise-budget` — and it does
        not come through here. A missing ledger row raises: silently halting nothing is the one
        outcome a fail-closed write must not have.
        """
        params = (_iso(now), run_id)

        async def unit(conn: aiosqlite.Connection) -> int:
            cursor = await conn.execute(
                "UPDATE budget_ledger SET halted = 1, updated_at = ? WHERE run_id = ?", params
            )
            return int(cursor.rowcount)

        if await self._writer.submit(unit) == 0:
            raise RepositoryError(
                f"cannot halt run {run_id}: no budget_ledger row exists, so `halted = 1` was "
                "written nowhere and the halt would survive only in this process (§11.2)"
            )

    # -- primitive 4, repo scope --------------------------------------------------------

    async def get_repo_budget(self, run_id: str, repo_id: str) -> RepoLedgerRow | None:
        sql = (
            "SELECT run_id, repo_id, spent_usd, reserved_usd, max_usd, revalidation_usd, "
            "       revalidation_max_usd, revalidation_rounds, reservation_expires_at, updated_at "
            "  FROM repo_ledger WHERE run_id = ? AND repo_id = ?"
        )
        async with self._read.execute(sql, (run_id, repo_id)) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return RepoLedgerRow(
            run_id=str(row[0]),
            repo_id=str(row[1]),
            spent_usd=float(row[2]),
            reserved_usd=float(row[3]),
            max_usd=float(row[4]),
            revalidation_usd=float(row[5]),
            revalidation_max_usd=float(row[6]),
            revalidation_rounds=int(row[7]),
            reservation_expires_at=_opt_str(row[8]),
            updated_at=str(row[9]),
        )

    async def open_repo_ledger(
        self,
        run_id: str,
        repo_id: str,
        *,
        max_usd: float,
        now: datetime,
        revalidation_max_usd: float = DEFAULT_REVALIDATION_MAX_USD,
    ) -> None:
        """Create this repo's durable ledger row, or RAISE its ceiling — never lower it.

        §3.5 scales `repo_max_cost_usd` by a blast radius the graph only ever discovers *more*
        of, so the ceiling is kept as a high-water mark: `MAX(existing, incoming)`. Two things
        follow, and both are deliberate. A repo whose dependents are found late gets the larger
        ceiling §3.5 intends. And a lowered ceiling can never land underneath money already
        committed, which would trip `CHECK (spent_usd + reserved_usd <= max_usd)` and turn a
        config edit into an `IntegrityError` in the middle of a wave.
        """
        sql = (
            "INSERT INTO repo_ledger (run_id, repo_id, max_usd, revalidation_max_usd, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (run_id, repo_id) DO UPDATE SET "
            "    max_usd = MAX(repo_ledger.max_usd, excluded.max_usd), "
            "    revalidation_max_usd = MAX(repo_ledger.revalidation_max_usd, "
            "                               excluded.revalidation_max_usd), "
            "    updated_at = excluded.updated_at"
        )
        params = (run_id, repo_id, max_usd, revalidation_max_usd, _iso(now))

        async def unit(conn: aiosqlite.Connection) -> None:
            await conn.execute(sql, params)

        await self._writer.submit(unit)

    async def reserve_repo_budget(
        self,
        run_id: str,
        repo_id: str,
        *,
        reservation_id: str,
        amount_usd: float,
        now: datetime,
        expires_at: datetime | None = None,
        phase: Phase | None = None,
        lease_fence: int | None = None,
    ) -> None:
        """Hold `amount_usd` against the repo ledger **and** the run ledger, in ONE transaction.

        **`reservation_id` is required, exactly like `fence` on the phase writes.** It cannot be
        forgotten, because there is no overload without it. A hold with no identity is one the
        reaper cannot attribute, and an unattributable hold is what forced v7's reaper to release
        the whole aggregate — every live worker's money with the dead one's. `phase`/`lease_fence`
        name the OWNER when there is one, and are what the reaper bumps so a reaped holder's next
        write matches zero rows; they are set together or not at all.

        **THE NESTING DECISION (§6 says "same CAS discipline" and stops there; this is the
        answer).** A repo reservation is **NESTED inside** the run reservation: the same dollar
        is held in both ledgers, exactly once in each, and the pair moves atomically. It is not
        two independent pools, and the run ledger is not a projection of the repo ledgers.

        Why nesting is the only option that can neither double- nor under-count:

        * *Independent, repo-only* under-counts the run. 250 repos each inside their own $6
          ceiling sum to $1 500 against a $500 run ceiling, and `run_max_cost_usd` — the one
          number an operator actually set — is enforced by nothing.
        * *Independent, run-only* is today's defect: the repo ceiling exists in memory and dies
          with the process.
        * *Derived* (run spend = `SUM(repo_ledger.spent_usd)`, recomputed per reservation) is a
          read-then-write across N rows. That is precisely the aggregate-over-a-stale-read the
          §6 CAS exists to forbid, and it double-counts the instant a second writer appears.
        * *Nested* holds each dollar once per ledger. Each `UPDATE` re-asserts its own ceiling in
          its own `WHERE`, so neither can be overshot, and the sum of the repo rows can never
          exceed the run row because every repo dollar had to clear the run row too.

        **And when the run ledger refuses a reservation the repo ledger already granted?** The
        repo hold is *never observable*. Both statements run inside one `BEGIN IMMEDIATE` (§11.5
        `StateWriter`), and the refusal is raised from inside that unit, so the rollback discards
        the repo `UPDATE` with it. No compensating write, no window in which another worker can
        read a phantom hold, no reconciliation pass that has to guess whether a leftover
        `reserved_usd` belongs to a live worker or a dead one. Order is repo-then-run only
        because the narrower ceiling refuses more cheaply; the transaction makes the order
        unobservable.

        Raises `RepoBudgetRefusedError` when the repo ceiling refuses and `BudgetRefusedError`
        when the run ceiling (or `halted = 1`) does — the type, not the message, is how the
        caller knows whether one repo stops or the whole fleet does (§11.2).
        """
        expiry = None if expires_at is None else _iso(expires_at)
        stamp = _iso(now)
        if (phase is None) != (lease_fence is None):
            raise RepositoryError(
                f"reservation {reservation_id}: phase={phase!r} and lease_fence={lease_fence!r} "
                "must be set together — a phase with no fence is an owner the reaper cannot "
                "invalidate, and a fence with no phase names no row to bump (§6, v8)"
            )
        reservation_params = (
            reservation_id,
            run_id,
            repo_id,
            None if phase is None else int(phase),
            lease_fence,
            amount_usd,
            expiry,
            stamp,
        )
        repo_params = (amount_usd, stamp, run_id, repo_id, amount_usd)
        run_params = (amount_usd, stamp, run_id, amount_usd)

        async def unit(conn: aiosqlite.Connection) -> None:
            # FIRST, so both ledger statements can derive their `reservation_expires_at` from the
            # surviving HELD rows — this one included — instead of overwriting each other's.
            minted = await conn.execute(_INSERT_RESERVATION_SQL, reservation_params)
            if int(minted.rowcount) == 0:
                raise ReservationRefusedError(
                    f"reservation_id {reservation_id!r} is already recorded for run {run_id}: an "
                    "id identifies ONE hold, and re-using it would let two holders settle the "
                    "same dollars (§6, v8)"
                )
            repo_cursor = await conn.execute(_RESERVE_REPO_DERIVED_SQL, repo_params)
            if int(repo_cursor.rowcount) == 0:
                raise RepoBudgetRefusedError(
                    _repo_reserve_refused(
                        run_id, repo_id, amount_usd, await _probe_repo_ledger(conn, run_id, repo_id)
                    )
                )
            run_cursor = await conn.execute(_RESERVE_RUN_DERIVED_SQL, run_params)
            if int(run_cursor.rowcount) == 0:
                # The repo UPDATE above is rolled back with this raise: no phantom hold.
                raise BudgetRefusedError(
                    _reserve_refused(run_id, amount_usd, await _probe_run_ledger(conn, run_id))
                )

        await self._writer.submit(unit)

    async def settle_repo_budget(
        self,
        run_id: str,
        repo_id: str,
        *,
        reservation_id: str,
        reserved_usd: float,
        actual_usd: float,
        now: datetime,
        revalidation: bool = False,
    ) -> None:
        """Settle the nested pair: release both holds and grow both `spent_usd`, atomically.

        The `reservations` row leaves `HELD` in the same transaction, and its `amount_usd` is part
        of that guard — so a settlement for a different amount than was reserved is refused rather
        than drifting the per-row ledger away from the aggregate it explains. A reservation the
        reaper already expired is gone from `HELD`, so its holder's late settlement raises
        `ReservationRefusedError` instead of moving money it no longer holds.

        The mirror image of `reserve_repo_budget`, and it has to be, or the two ledgers drift:
        a repo that settled while the run did not would free repo headroom that the run still
        believes is held, and every later reservation would be paced by a number nobody wrote.

        `revalidation=True` also grows `repo_ledger.revalidation_usd` against its own sub-ceiling
        in the same statement (§3.5.1) — stub rework is priced, never free, and exhausting the
        sub-ceiling holds the repo `DEGRADED` rather than promoting it.
        """
        stamp = _iso(now)
        guard = reserved_usd - _USD_EPSILON
        repo_sql = _SETTLE_REPO_REVALIDATION_SQL if revalidation else _SETTLE_REPO_SQL
        repo_params: tuple[object, ...] = (
            (
                reserved_usd,
                actual_usd,
                actual_usd,
                stamp,
                run_id,
                repo_id,
                guard,
                actual_usd,
                reserved_usd,
                actual_usd,
            )
            if revalidation
            else (
                reserved_usd,
                actual_usd,
                stamp,
                run_id,
                repo_id,
                guard,
                actual_usd,
                reserved_usd,
            )
        )
        run_params = _settle_run_params(
            run_id, reserved_usd=reserved_usd, actual_usd=actual_usd, now=now
        )

        async def unit(conn: aiosqlite.Connection) -> None:
            # FIRST: the row leaves 'HELD' before the ledgers derive their expiry from what is
            # still held, and a reaped (or already-settled) hold is refused before any money moves.
            released = await conn.execute(
                _SETTLE_RESERVATION_SQL,
                (reservation_id, run_id, repo_id, reserved_usd, _USD_EPSILON),
            )
            if int(released.rowcount) == 0:
                raise ReservationRefusedError(
                    f"settlement REFUSED for reservation {reservation_id!r} of {repo_id} in run "
                    f"{run_id}: no HELD reservation of ${reserved_usd:.4f} by that id — it was "
                    "already settled, or the reaper expired it and bumped the owning lease fence "
                    "(§6, v8)"
                )
            repo_cursor = await conn.execute(repo_sql, repo_params)
            if int(repo_cursor.rowcount) == 0:
                raise RepoBudgetRefusedError(
                    _repo_settle_refused(
                        run_id,
                        repo_id,
                        reserved_usd,
                        actual_usd,
                        await _probe_repo_ledger(conn, run_id, repo_id),
                    )
                )
            run_cursor = await conn.execute(_SETTLE_RUN_SQL, run_params)
            if int(run_cursor.rowcount) == 0:
                raise BudgetRefusedError(_settle_refused(run_id, reserved_usd, actual_usd))

        await self._writer.submit(unit)

    # -- evidence ----------------------------------------------------------------------

    async def record_attempt(self, row: AttemptRow) -> None:
        """Append one attempt. A RE-EXECUTED command overwrites its own outcome, never a stale one.

        §6: `DO UPDATE` on the outcome columns when `already_applied = 0`, else `DO NOTHING` —
        under a plain `DO NOTHING` the repair loop would be composed from the first, stale log.
        """
        sql = (
            "INSERT INTO attempts (attempt_id, run_id, repo_id, task_id, phase, attempt, "
            "    revalidation_round, tier, context_policy, approach_signature, command, "
            "    command_sha256, retry_ordinal, exit_code, failure_class, duration_ms, "
            "    stdout_tail, stderr_tail, cost_usd, patch_id, commit_sha, already_applied, "
            "    started_at, finished_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (run_id, repo_id, phase, attempt, revalidation_round, tier, "
            "             command_sha256, approach_signature, retry_ordinal) DO UPDATE SET "
            "    exit_code = excluded.exit_code, failure_class = excluded.failure_class, "
            "    stdout_tail = excluded.stdout_tail, stderr_tail = excluded.stderr_tail, "
            "    finished_at = excluded.finished_at "
            "  WHERE attempts.already_applied = 0"
        )
        params = (
            row.attempt_id,
            row.run_id,
            row.repo_id,
            row.task_id,
            int(row.phase),
            row.attempt,
            row.revalidation_round,
            row.tier,
            row.context_policy,
            row.approach_signature,
            row.command,
            row.command_sha256,
            row.retry_ordinal,
            row.exit_code,
            row.failure_class,
            row.duration_ms,
            row.stdout_tail,
            row.stderr_tail,
            row.cost_usd,
            row.patch_id,
            row.commit_sha,
            int(row.already_applied),
            row.started_at,
            row.finished_at,
        )

        async def unit(conn: aiosqlite.Connection) -> None:
            await conn.execute(sql, params)

        await self._writer.submit(unit)

    async def insert_symbols(self, rows: Sequence[SymbolRow]) -> int:
        """Batch-insert symbols; a re-indexed file cannot duplicate rows (§6 `DO NOTHING`)."""
        sql = (
            "INSERT INTO symbols (run_id, repo_id, fqn, kind, path, line, language, "
            "                     is_definition, exported) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (run_id, repo_id, path, line, fqn, kind) DO NOTHING"
        )
        params = [
            (
                r.run_id,
                r.repo_id,
                r.fqn,
                r.kind,
                r.path,
                r.line,
                r.language,
                int(r.is_definition),
                int(r.exported),
            )
            for r in rows
        ]

        async def unit(conn: aiosqlite.Connection) -> int:
            cursor = await conn.executemany(sql, params)
            return int(cursor.rowcount)

        return await self._writer.submit(unit)

    async def insert_edges(self, rows: Sequence[EdgeRow]) -> int:
        """Batch-insert edges keyed by `(run_id, edge_key)`; re-running inference cannot
        duplicate (§6). `edge_key` alone names the EDGE — it is run-independent by construction
        (§5) — so the row this run owns is addressed by the pair."""
        sql = (
            "INSERT INTO edges (edge_key, run_id, src_kind, src_id, dst_kind, dst_id, "
            "    dst_coord_key, kind, base_confidence, confidence, ambiguous, "
            "    ordering_suppressed, evidence_path, evidence_line, detected_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (run_id, edge_key) DO UPDATE SET confidence = excluded.confidence, "
            "    ambiguous = excluded.ambiguous, "
            "    ordering_suppressed = excluded.ordering_suppressed"
        )
        params = [
            (
                r.edge_key,
                r.run_id,
                r.src_kind,
                r.src_id,
                r.dst_kind,
                r.dst_id,
                r.dst_coord_key,
                r.kind,
                r.base_confidence,
                r.confidence,
                int(r.ambiguous),
                int(r.ordering_suppressed),
                r.evidence_path,
                r.evidence_line,
                r.detected_at,
            )
            for r in rows
        ]

        async def unit(conn: aiosqlite.Connection) -> int:
            cursor = await conn.executemany(sql, params)
            return int(cursor.rowcount)

        return await self._writer.submit(unit)

    async def append_event(self, row: EventRow) -> int:
        """Append one event and return its `seq`, allocated IN-STATEMENT (§6).

        `MAX(seq) + 1` computed in Python and passed in makes the second concurrent emitter raise
        `IntegrityError` — telemetry killing a worker. `row.seq` on the way in is ignored.
        """
        sql = (
            "INSERT INTO events (run_id, seq, ts, repo_id, phase, level, event, event_uid, "
            "                    payload) "
            "VALUES (?, (SELECT COALESCE(MAX(seq), 0) + 1 FROM events WHERE run_id = ?), "
            "        ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (run_id, event_uid) DO NOTHING "
            "RETURNING seq"
        )
        params = (
            row.run_id,
            row.run_id,
            row.ts,
            row.repo_id,
            None if row.phase is None else int(row.phase),
            row.level,
            row.event,
            row.event_uid,
            row.payload,
        )

        async def unit(conn: aiosqlite.Connection) -> int:
            async with conn.execute(sql, params) as cursor:
                inserted = list(await cursor.fetchall())
            if inserted:
                return int(inserted[0][0])
            # A replayed JSONL tail: the row is already there, and its seq is the answer.
            async with conn.execute(
                "SELECT seq FROM events WHERE run_id = ? AND event_uid = ?",
                (row.run_id, row.event_uid),
            ) as probe:
                existing = await probe.fetchone()
            if existing is None:  # pragma: no cover — DO NOTHING fired, so the row exists
                raise RepositoryError(f"event {row.event_uid} neither inserted nor found")
            return int(existing[0])

        return await self._writer.submit(unit)


def _stolen(run_id: str, repo_id: str, phase: Phase, fence: int, op: str) -> str:
    return (
        f"lease STOLEN: {op} on phases({run_id}, {repo_id}, phase={int(phase)}) with fence "
        f"{fence} matched 0 rows — the lease was reclaimed and the fence bumped. Abort NOW, "
        "without touching git or the worktree, and emit LeaseStolen (§6 FENCING)."
    )


#: `tasks` CHECKs `json_array_length(ladder) = max_attempts` with a NULL index 0 (ADR-0021), so
#: the default ladder is a function of the ceiling rather than a single literal.
_DEFAULT_LADDERS: Final[dict[int, str]] = {
    n: "[" + ",".join(["null", *['"EVIDENCE_ONLY"'] * (n - 1)]) + "]" for n in range(1, 9)
}
