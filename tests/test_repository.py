"""Behaviour tests for `src/fleet/state/repository.py` — the §6 normative primitives.

Every property pinned here is one whose *absence is silent*. A `SELECT`-then-`UPDATE` claim does
not raise: it hands the same repo to two workers, which both commit to `migrate/<repo>` and both
burn that repo's `max_usd`. A lease without a fence does not raise: the reclaimed holder keeps
writing and every check still passes, because a bare pid was never an identity. A read-then-write
reservation does not raise: twelve workers each reserve $3 against a $497/$500 ledger and all
twelve writes succeed — a 6% overspend that no assertion in the harness would have caught.
An `iter_*` that returned a `list` does not raise either; it just holds 10⁵ rows resident and
breaches the RSS ceiling at 250 repos (§13 row 8).

So the assertions are behavioural — a real temp database, real concurrency, real locks — and
never a string match against the source.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import sqlite3
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest
from pydantic import BaseModel

from fleet.models.enums import PHASE_DEMOTED_KIND, Phase, RepoStatus, TaskKind
from fleet.obs.redact import redact_text
from fleet.state import checkpoints
from fleet.state import db as dbmod
from fleet.state.db import StateWriter, connect_ro, initialize_database
from fleet.state.repository import (
    AttemptRow,
    BudgetRefusedError,
    EdgeRow,
    EventRow,
    FloorSnapshotStaleError,
    LeaseStolenError,
    ReadOnlyRepository,
    RepoBudgetRefusedError,
    RepositoryError,
    ReservationRefusedError,
    SqliteStateRepository,
    StateRepository,
    SymbolRow,
)

NOW = datetime(2026, 8, 9, 12, 0, 0, tzinfo=UTC)
RUN = "11111111-1111-4111-8111-111111111111"
REPO = "acme-commons"
OTHER = "acme-billing"
WORKER = "host-a:cid-1:4242:boot-uuid-1"


@pytest.fixture(autouse=True)
def _clean_write_slot() -> Iterator[None]:
    """A leaked single-writer slot would fail every later test for the wrong reason."""
    yield
    dbmod._release_write_slot()


@pytest.fixture
async def db_path(tmp_path: Path) -> Path:
    """A fresh v7 database on disk — `:memory:` would not exercise WAL, which needs a file."""
    path = tmp_path / "state" / "fleet.db"
    await initialize_database(path)
    return path


@pytest.fixture
async def repo(db_path: Path) -> AsyncIterator[SqliteStateRepository]:
    """A repository wired exactly as the runner wires it: one writer, one `mode=ro` handle."""
    async with StateWriter(db_path, owner="test-writer") as writer:
        read_conn = await connect_ro(db_path)
        try:
            store = SqliteStateRepository(writer=writer, read_conn=read_conn)
            await store.upsert_run(
                RUN, started_at=NOW, config_sha256="a" * 64, harness_version="0.1.0"
            )
            for repo_id in (REPO, OTHER):
                await store.upsert_repo(
                    repo_id, name=repo_id, url=f"https://example.invalid/{repo_id}.git", now=NOW
                )
            yield store
        finally:
            await read_conn.close()


@pytest.fixture
async def read_conn(db_path: Path) -> AsyncIterator[aiosqlite.Connection]:
    conn = await connect_ro(db_path)
    try:
        yield conn
    finally:
        await conn.close()


def _rid(label: str = "") -> str:
    """A fresh `reservation_id`. Required on every reserve/settle since v8: a hold with no
    identity is one the reaper cannot attribute, which is the whole defect `reservations` fixes."""
    return f"res-{label}-{uuid.uuid4()}" if label else f"res-{uuid.uuid4()}"


async def _pending_task(store: SqliteStateRepository, task_id: str) -> None:
    await store.upsert_task(
        task_id,
        run_id=RUN,
        repo_id=REPO,
        phase=Phase.TRANSFORM,
        kind=TaskKind.REWRITE,
        dest_path="libs/com/acme/commons",
        created_at=NOW,
    )


# ======================================================================================
# the Protocol seam (CLAUDE.md Guardrail 3)
# ======================================================================================


async def test_the_sqlite_store_satisfies_the_protocol_the_orchestrator_depends_on(
    repo: SqliteStateRepository,
) -> None:
    """The orchestrator types against `StateRepository`, never against the SQLite class.

    Why: §6's ADR-0004 exit condition is "swap the store for Postgres behind the same Protocol".
    That swap is only real if the concrete class is substitutable, and only checkable if the
    Protocol is the declared dependency rather than a comment.
    """
    assert isinstance(repo, StateRepository)
    assert isinstance(repo, ReadOnlyRepository)


# ======================================================================================
# `upsert_task` — D89 Phase 1 (ADR-0101): idempotent identity, RETURNING, never a claim
# ======================================================================================


async def test_upsert_task_returns_the_canonical_id_not_the_freshly_passed_one(
    repo: SqliteStateRepository,
) -> None:
    """Two UPSERTs of the same `(run_id, repo_id, phase, kind)` tuple, two different UUID4s: the
    row's ORIGINAL `task_id` wins both times, and `RETURNING task_id` is what lets a caller learn
    that rather than silently keeping the discarded id it passed on the second call.

    Why this matters for D89 Phase 1: `_coarse_task_id` mints a fresh UUID4 on every dispatch and
    relies on this idempotent-identity behavior to get back the SAME `task_id` on every retry rung
    of one phase — without it, `attempts.task_id` would point at a different `tasks` row per rung.
    """
    first_id = str(uuid.uuid4())
    second_id = str(uuid.uuid4())
    assert first_id != second_id

    won_first = await repo.upsert_task(
        first_id,
        run_id=RUN,
        repo_id=REPO,
        phase=Phase.TRANSFORM,
        kind=TaskKind.REWRITE,
        dest_path="libs/com/acme/commons",
        created_at=NOW,
    )
    won_second = await repo.upsert_task(
        second_id,
        run_id=RUN,
        repo_id=REPO,
        phase=Phase.TRANSFORM,
        kind=TaskKind.REWRITE,
        dest_path="libs/com/acme/commons",
        created_at=NOW,
    )

    assert won_first == first_id
    assert won_second == first_id, "a re-plan must not silently switch the row's identity"


async def test_upsert_task_never_writes_a_status_other_than_the_schema_default(
    repo: SqliteStateRepository, db_path: Path
) -> None:
    """§6: `upsert_task` UPSERTs the plan columns only. `status` is never in its SET list, so a
    row it creates OR re-plans sits at the schema default `'PENDING'` forever — this is the
    property D89 Phase 1 (ADR-0101) leans on to stay invisible to `_ARBITRATED_TASKS_SQL`'s
    `status = 'RUNNING'` scan, closing a misfire risk broader than REWRITE/RELOCATE (see the ADR).
    """
    task_id = str(uuid.uuid4())
    await repo.upsert_task(
        task_id,
        run_id=RUN,
        repo_id=REPO,
        phase=Phase.TRANSFORM,
        kind=TaskKind.REWRITE,
        dest_path="libs/com/acme/commons",
        created_at=NOW,
    )
    # a second UPSERT of the same identity is a re-plan, not a fresh insert
    await repo.upsert_task(
        str(uuid.uuid4()),
        run_id=RUN,
        repo_id=REPO,
        phase=Phase.TRANSFORM,
        kind=TaskKind.REWRITE,
        dest_path="libs/com/acme/commons",
        created_at=NOW,
    )

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT status FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row == ("PENDING",)


# ======================================================================================
# primitive 1 — the atomic claim
# ======================================================================================


async def test_exactly_one_of_many_concurrent_claimers_wins_the_task(
    repo: SqliteStateRepository,
) -> None:
    """Fire eight claimers at one PENDING task: one wins, seven get `None`.

    Why: without a single compare-and-swap, two workers both see PENDING, both write RUNNING,
    both run `fleet migrate` on the same repo, both commit to `migrate/<repo>`, and both burn
    that repo's `max_usd` (§6 CLAIM). A second winner is not a slow path — it is a double spend
    and a branch with two authors.
    """
    await _pending_task(repo, "task-solo")

    results = await asyncio.gather(
        *(
            repo.claim_next_task(RUN, worker=f"{WORKER}-{i}", now=NOW, lease_ttl_s=300)
            for i in range(8)
        )
    )

    winners = [claim for claim in results if claim is not None]
    assert len(winners) == 1, f"{len(winners)} workers claimed the same task"
    assert results.count(None) == 7
    won = winners[0]
    assert won.task_id == "task-solo"
    assert won.fence_token == 1, "the claim must bump the fence exactly once"
    assert won.phase is Phase.TRANSFORM
    assert won.kind is TaskKind.REWRITE


async def test_claiming_an_empty_queue_returns_none_rather_than_inventing_work(
    repo: SqliteStateRepository,
) -> None:
    """No PENDING row means `None`. Why: the claim is also the scheduler's "is there work" probe,
    and an exception there would turn an idle fleet into a crash loop."""
    assert await repo.claim_next_task(RUN, worker=WORKER, now=NOW, lease_ttl_s=300) is None


# ======================================================================================
# primitives 2 and 3 — fencing, renewal, the reaper
# ======================================================================================


async def test_a_stolen_lease_cannot_write(repo: SqliteStateRepository) -> None:
    """Worker A holds fence 1; the reaper bumps to 2; A's fenced writes match zero rows and raise.

    Why: a heartbeat and a pid are not a fencing token. Fresh PID namespaces reuse low pids, so
    a reclaimed lease could not be invalidated and BOTH writers passed every check they made.
    The fence bump is the *only* thing that makes the old holder's next write match zero rows
    (§6 FENCING), and the refusal must be an exception — a caller that could ignore a `False`
    would go on to touch git and the worktree it no longer owns.
    """
    await repo.upsert_phase(RUN, REPO, Phase.TRANSFORM, now=NOW)
    fence = await repo.acquire_phase_lease(
        RUN, REPO, Phase.TRANSFORM, owner=WORKER, now=NOW, lease_ttl_s=60
    )
    assert fence == 1

    later = NOW + timedelta(seconds=120)
    assert await repo.reap_expired_phase_leases(RUN, now=later) == 1

    with pytest.raises(LeaseStolenError):
        await repo.renew_phase_lease(
            RUN, REPO, Phase.TRANSFORM, fence=1, now=later, lease_ttl_s=60
        )
    with pytest.raises(LeaseStolenError):
        await repo.complete_phase(
            RUN,
            REPO,
            Phase.TRANSFORM,
            fence=1,
            status=RepoStatus.SUCCEEDED,
            now=later,
            last_error=None,
        )

    row = await repo.get_phase(RUN, REPO, Phase.TRANSFORM)
    assert row is not None
    assert row.status is RepoStatus.PENDING, "the reaper hands the phase back, not to its thief"
    assert row.lease_fence == 2
    assert row.lease_owner is None
    assert row.attempts == 0, "a stolen-lease write must not have landed its attempts increment"


async def test_the_reaper_reaps_only_the_expired_lease(repo: SqliteStateRepository) -> None:
    """One expired lease, one live one: exactly the expired row is reclaimed.

    Why: a reaper that swept by status alone would yank the worktree out from under a perfectly
    healthy worker every 30 s, and the fence bump would make that worker's next write fail with
    no cause anyone could see. Expiry is the discriminator, and it is compared as ISO-8601 TEXT,
    which only orders correctly because every instant is written in one fixed rendering.
    """
    for repo_id in (REPO, OTHER):
        await repo.upsert_phase(RUN, repo_id, Phase.BUILD, now=NOW)
    expired_fence = await repo.acquire_phase_lease(
        RUN, REPO, Phase.BUILD, owner=WORKER, now=NOW, lease_ttl_s=30
    )
    live_fence = await repo.acquire_phase_lease(
        RUN, OTHER, Phase.BUILD, owner=f"{WORKER}-live", now=NOW, lease_ttl_s=3600
    )
    assert (expired_fence, live_fence) == (1, 1)

    reaped = await repo.reap_expired_phase_leases(RUN, now=NOW + timedelta(seconds=60))
    assert reaped == 1, "the live lease must not be counted"

    live = await repo.get_phase(RUN, OTHER, Phase.BUILD)
    assert live is not None
    assert live.status is RepoStatus.RUNNING
    assert live.lease_fence == 1, "an untouched lease keeps its fence"

    # The live holder is still able to renew — the proof that it was left genuinely intact.
    await repo.renew_phase_lease(
        RUN, OTHER, Phase.BUILD, fence=1, now=NOW + timedelta(seconds=60), lease_ttl_s=3600
    )


async def test_the_attempt_that_reaches_max_attempts_escalates_in_the_same_statement(
    repo: SqliteStateRepository,
) -> None:
    """A retry hand-back that exhausts the ladder lands REQUIRES_HUMAN_INTERVENTION, not FAILED.

    Why: §6 — "the increment that RECORDS a terminal failure must never be the one that raises",
    and the escalation must happen in the same statement as the increment. A read-then-decide
    would let the ladder run one rung past its ceiling, which is exactly the spend CLAUDE.md
    Rule 11 exists to bound.
    """
    await repo.upsert_phase(RUN, REPO, Phase.VERIFY, now=NOW, max_attempts=2)

    first = await repo.acquire_phase_lease(
        RUN, REPO, Phase.VERIFY, owner=WORKER, now=NOW, lease_ttl_s=60
    )
    assert first is not None
    assert (
        await repo.complete_phase(
            RUN, REPO, Phase.VERIFY, fence=first, status=RepoStatus.PENDING, now=NOW
        )
        is RepoStatus.PENDING
    )

    second = await repo.acquire_phase_lease(
        RUN, REPO, Phase.VERIFY, owner=WORKER, now=NOW, lease_ttl_s=60
    )
    assert second is not None
    final = await repo.complete_phase(
        RUN,
        REPO,
        Phase.VERIFY,
        fence=second,
        status=RepoStatus.PENDING,
        now=NOW,
        last_error="bazel test //...: 3 failures",
    )
    assert final is RepoStatus.REQUIRES_HUMAN_INTERVENTION

    row = await repo.get_phase(RUN, REPO, Phase.VERIFY)
    assert row is not None
    assert row.attempts == 2
    assert row.status is RepoStatus.REQUIRES_HUMAN_INTERVENTION
    assert row.last_error == "bazel test //...: 3 failures"


async def test_complete_phase_redacts_a_credential_in_last_error_before_the_write(
    repo: SqliteStateRepository,
) -> None:
    """D88 (§12.20, SECURITY-RELEVANT): `complete_phase` is the write boundary SPEC.md:6987
    names — "`state/repository.py` redacts `last_error`... on write" — and until this test's
    companion fix it did not. The gap is real, not hypothetical: `workers/base.py`'s generic
    `except Exception` catch-all (`error_from_exception`) builds `WorkerError.stderr_tail =
    str(exc)` with no redaction, and that string reaches this exact `last_error` parameter on
    every retry/terminal path through `orchestrator/runner.py`. A raised exception whose message
    happens to quote a credential-bearing URL — plausible for any network or git failure, not an
    adversarial input — is the shape planted here.

    The control is `test_the_attempt_that_reaches_max_attempts_escalates_in_the_same_statement`
    just above: it asserts an innocuous `last_error` ("bazel test //...: 3 failures") survives
    UNCHANGED, so this test is not free to over-redact its way to green.
    """
    pat = "github_pat_11ABCDEFG0abcdefghijklmnopqrstuvwxyz0123456789ABCDEF"
    tainted = f"clone failed for remote https://oauth2:{pat}@gitea.local:3001/x.git"

    await repo.upsert_phase(RUN, REPO, Phase.TRANSFORM, now=NOW)
    fence = await repo.acquire_phase_lease(
        RUN, REPO, Phase.TRANSFORM, owner=WORKER, now=NOW, lease_ttl_s=60
    )
    assert fence is not None

    await repo.complete_phase(
        RUN, REPO, Phase.TRANSFORM, fence=fence, status=RepoStatus.PENDING, now=NOW,
        last_error=tainted,
    )

    row = await repo.get_phase(RUN, REPO, Phase.TRANSFORM)
    assert row is not None
    assert row.last_error is not None
    assert pat not in row.last_error, f"a live PAT reached phases.last_error: {row.last_error!r}"
    assert "github_pat_" not in row.last_error
    assert "«redacted:" in row.last_error, "the placeholder must survive, or debugging is blind"
    assert "gitea.local" in row.last_error, "over-redaction destroys the debuggable part too"


def test_redacting_a_github_pat_twice_is_idempotent() -> None:
    """The narrower claim `complete_phase`'s docstring now makes: for the `github_pat_…` shape
    this fix's own test plants, a second `redact_text` pass is a no-op — NOT a general property
    of every `PATTERNS` entry (the `authorization` kind's own placeholder text re-triggers its
    own pattern on a second pass, per code review; deliberately not tested here — out of this
    fix's scope). Proven directly against `redact_text`, with no database involved, because the
    property under test is about the function, not about `complete_phase`'s wiring of it."""
    pat = "github_pat_11ABCDEFG0abcdefghijklmnopqrstuvwxyz0123456789ABCDEF"
    tainted = f"clone failed for remote https://oauth2:{pat}@gitea.local:3001/x.git"

    once = redact_text(tainted)
    twice = redact_text(once)

    assert once == twice, "a second pass over an already-redacted PAT must change nothing"
    assert pat not in once


# ======================================================================================
# primitive 4 — the fail-closed ledger
# ======================================================================================


async def test_an_over_reservation_is_refused_and_leaves_the_ledger_untouched(
    repo: SqliteStateRepository,
) -> None:
    """A reservation past `max_usd` raises, and `reserved_usd` is exactly what it was.

    Why: "fail-closed" is a CONSTRAINT, not a convention (§6). A refusal returned as `False`
    is a refusal a caller can drop, and the very next line dispatches the LLM call anyway.
    """
    await repo.open_budget_ledger(RUN, max_usd=10.0, now=NOW)
    await repo.reserve_budget(RUN, amount_usd=9.0, now=NOW)

    with pytest.raises(BudgetRefusedError, match="REFUSED"):
        await repo.reserve_budget(RUN, amount_usd=2.0, now=NOW)

    ledger = await repo.get_budget(RUN)
    assert ledger is not None
    assert ledger.reserved_usd == pytest.approx(9.0)
    assert ledger.spent_usd == pytest.approx(0.0)


async def test_concurrent_reservations_never_collectively_exceed_the_ceiling(
    repo: SqliteStateRepository,
) -> None:
    """Twenty-four racers reserve $1 each against a $10 ledger: ten succeed, fourteen are refused.

    Why: this is the overspend verbatim. Twelve workers each reserving against a *stale read* of
    a $497/$500 ledger all wrote successfully and the run overspent by 6%+. The guard has to be
    inside the UPDATE's WHERE clause — `spent + reserved + :amt <= max_usd` — so the losers match
    zero rows instead of racing on a value they read a moment ago.
    """
    await repo.open_budget_ledger(RUN, max_usd=10.0, now=NOW)

    async def attempt() -> bool:
        try:
            await repo.reserve_budget(RUN, amount_usd=1.0, now=NOW)
        except BudgetRefusedError:
            return False
        return True

    outcomes = await asyncio.gather(*(attempt() for _ in range(24)))

    assert sum(outcomes) == 10, "more reservations were granted than the ceiling allows"
    ledger = await repo.get_budget(RUN)
    assert ledger is not None
    assert ledger.reserved_usd == pytest.approx(10.0)
    assert ledger.spent_usd + ledger.reserved_usd <= ledger.max_usd


async def test_settlement_moves_a_reservation_into_spend_and_refuses_a_phantom_one(
    repo: SqliteStateRepository,
) -> None:
    """Settling converts held money into spent money; settling money nobody reserved raises.

    Why: a settlement that silently no-ops leaves `reserved_usd` ratcheted up forever, and the
    run halts on a budget it never actually spent (§6 `reservation_expires_at`).
    """
    await repo.open_budget_ledger(RUN, max_usd=10.0, now=NOW)
    await repo.reserve_budget(RUN, amount_usd=4.0, now=NOW)
    await repo.settle_budget(RUN, reserved_usd=4.0, actual_usd=3.25, now=NOW)

    ledger = await repo.get_budget(RUN)
    assert ledger is not None
    assert ledger.reserved_usd == pytest.approx(0.0)
    assert ledger.spent_usd == pytest.approx(3.25)

    with pytest.raises(BudgetRefusedError):
        await repo.settle_budget(RUN, reserved_usd=5.0, actual_usd=5.0, now=NOW)


# ======================================================================================
# primitive 4, repo scope — the sub-ledger, and the crash it has to survive
# ======================================================================================


@asynccontextmanager
async def _process(db_path: Path, owner: str) -> AsyncIterator[SqliteStateRepository]:
    """ONE process's view of the database: its own single writer, its own `mode=ro` handle.

    Leaving this block is the crash: the writer is closed, the write slot released, the read
    handle closed and the repository object dropped. Anything a later block can still see was, by
    construction, on disk rather than in the object that wrote it.
    """
    async with StateWriter(db_path, owner=owner) as writer:
        read_conn = await connect_ro(db_path)
        try:
            yield SqliteStateRepository(writer=writer, read_conn=read_conn)
        finally:
            await read_conn.close()


async def _open_ledgers(store: SqliteStateRepository, *, run_max: float, repo_max: float) -> None:
    await store.upsert_run(RUN, started_at=NOW, config_sha256="a" * 64, harness_version="0.1.0")
    await store.upsert_repo(REPO, name=REPO, url=f"https://example.invalid/{REPO}.git", now=NOW)
    await store.open_budget_ledger(RUN, max_usd=run_max, now=NOW)
    await store.open_repo_ledger(RUN, REPO, max_usd=repo_max, now=NOW)


async def test_a_repos_spend_survives_the_process_that_spent_it(db_path: Path) -> None:
    """A repo burns $4.50 of its $6.00 ceiling, the process dies, and the NEXT process sees it.

    Why: this is the defect. `repo_ledger` had no repository method at all, so every per-repo
    ceiling lived in a `dict` on one `CostLedger`. A 250-repo run that crashed at hour 30 and
    resumed at hour 31 handed a repo that had already spent its $6.00 a fresh $6.00 — the ceiling
    that exists to bound a runaway was, in exactly the crash it exists to bound, a no-op.

    The second block shares nothing with the first but the file: new writer, new read handle, new
    object. The headroom it computes and the reservation it refuses come off disk or from nowhere.
    """
    async with _process(db_path, "process-1") as first:
        await _open_ledgers(first, run_max=500.0, repo_max=6.0)
        held = _rid("spend")
        await first.reserve_repo_budget(RUN, REPO, reservation_id=held, amount_usd=4.5, now=NOW)
        await first.settle_repo_budget(
            RUN, REPO, reservation_id=held, reserved_usd=4.5, actual_usd=4.5, now=NOW
        )

    async with _process(db_path, "process-2") as second:
        resumed = await second.get_repo_budget(RUN, REPO)
        assert resumed is not None
        assert resumed.spent_usd == pytest.approx(4.5), "the second process started from zero"
        assert resumed.remaining_usd == pytest.approx(1.5)

        with pytest.raises(RepoBudgetRefusedError):
            await second.reserve_repo_budget(
                RUN, REPO, reservation_id=_rid(), amount_usd=2.0, now=NOW
            )

        # ...and exactly the surviving headroom is still spendable, so the ceiling paces the
        # resumed run rather than ending it.
        await second.reserve_repo_budget(
            RUN, REPO, reservation_id=_rid(), amount_usd=1.5, now=NOW
        )
        after = await second.get_repo_budget(RUN, REPO)
        assert after is not None
        assert after.remaining_usd == pytest.approx(0.0)


async def test_concurrent_repo_reservations_never_collectively_exceed_the_repo_ceiling(
    repo: SqliteStateRepository,
) -> None:
    """24 racers reserve $1 against a repo with $10 left and a run with $1 000: ten win.

    Why: the run ledger's CAS never protected the repo ceiling. A per-repo `dict` under 24
    concurrent dispatches is a read-then-write by another name — every racer checks the same
    stale number and every one of them proceeds. The guard has to be inside the repo `UPDATE`'s
    own `WHERE`, or "per-repo ceiling" means "per-repo suggestion".

    The run row is asserted too, at the same total: nesting holds each dollar once in EACH
    ledger, so a run reservation of $20 here would be the double-count, and $0 the under-count.
    """
    await repo.open_budget_ledger(RUN, max_usd=1_000.0, now=NOW)
    await repo.open_repo_ledger(RUN, REPO, max_usd=10.0, now=NOW)

    async def attempt() -> bool:
        try:
            await repo.reserve_repo_budget(
                RUN, REPO, reservation_id=_rid(), amount_usd=1.0, now=NOW
            )
        except RepoBudgetRefusedError:
            return False
        return True

    outcomes = await asyncio.gather(*(attempt() for _ in range(24)))

    assert sum(outcomes) == 10, "more repo reservations were granted than the repo ceiling allows"
    repo_row = await repo.get_repo_budget(RUN, REPO)
    run_row = await repo.get_budget(RUN)
    assert repo_row is not None and run_row is not None
    assert repo_row.reserved_usd == pytest.approx(10.0)
    assert run_row.reserved_usd == pytest.approx(10.0), "each dollar is held once in EACH ledger"


async def test_a_run_refusal_leaves_no_phantom_repo_reservation(
    repo: SqliteStateRepository,
) -> None:
    """The repo ceiling fits, the RUN ceiling does not, and the repo row is untouched afterwards.

    Why: this is the question §6 leaves open — what happens to the repo hold when the outer
    ledger refuses. Anything other than "it never happened" leaks: a compensating second write
    can be lost to the crash between the two, and a leftover `reserved_usd` is indistinguishable
    from a live worker's hold, so it ratchets the repo closed until a human reads the table.
    Both statements share one `BEGIN IMMEDIATE`, so the refusal rolls the repo hold back with it.
    """
    await repo.open_budget_ledger(RUN, max_usd=5.0, now=NOW)
    await repo.open_repo_ledger(RUN, REPO, max_usd=30.0, now=NOW)
    await repo.reserve_repo_budget(RUN, REPO, reservation_id=_rid(), amount_usd=4.0, now=NOW)

    refused_id = _rid("refused")
    with pytest.raises(BudgetRefusedError) as refused:
        await repo.reserve_repo_budget(
            RUN, REPO, reservation_id=refused_id, amount_usd=2.0, now=NOW
        )
    assert not isinstance(refused.value, RepoBudgetRefusedError), (
        "the RUN ceiling refused, and the type has to say so: a repo breach sends one repo to a "
        "human, a run breach stops the fleet (§11.2)"
    )

    repo_row = await repo.get_repo_budget(RUN, REPO)
    assert repo_row is not None
    assert repo_row.reserved_usd == pytest.approx(4.0), "a phantom repo hold survived the refusal"


async def test_a_repo_refusal_is_a_typed_error_and_never_a_falsy_return(
    repo: SqliteStateRepository,
) -> None:
    """`rowcount == 0` raises a type; a grant returns `None`, so there is nothing to `if` on.

    Why: a `bool` return is a refusal a caller can drop on the floor, and the very next line
    dispatches the LLM call anyway. The type is also the routing decision — `RepoBudgetRefusedError`
    is a `BudgetRefusedError`, so `except BudgetRefusedError` still catches everything, but a
    caller that must tell a repo breach from a run breach never has to parse a message to do it.
    """
    await repo.open_budget_ledger(RUN, max_usd=1_000.0, now=NOW)
    await repo.open_repo_ledger(RUN, REPO, max_usd=1.0, now=NOW)

    granted = await repo.reserve_repo_budget(
        RUN, REPO, reservation_id=_rid(), amount_usd=1.0, now=NOW
    )
    assert granted is None, "a grant returns nothing: there is no truthy/falsy protocol here"

    with pytest.raises(RepoBudgetRefusedError) as refused:
        await repo.reserve_repo_budget(
            RUN, REPO, reservation_id=_rid(), amount_usd=0.01, now=NOW
        )
    assert type(refused.value) is RepoBudgetRefusedError
    assert isinstance(refused.value, BudgetRefusedError)
    assert isinstance(refused.value, RepositoryError)


async def test_settling_moves_both_ledgers_and_prices_revalidation_separately(
    repo: SqliteStateRepository,
) -> None:
    """One settlement releases both holds, grows both spends, and bills rework to its own class.

    Why: two ledgers that settle independently drift. A repo that settled while the run did not
    would free repo headroom the run still believes is held, and every later reservation would be
    paced by a number nobody wrote. And §3.5.1 prices stub rework separately precisely so "the
    rework was free" cannot be asserted again — `revalidation_usd` is a sub-ceiling *inside*
    `max_usd`, so exhausting it can never raise the repo's total.
    """
    await repo.open_budget_ledger(RUN, max_usd=100.0, now=NOW)
    await repo.open_repo_ledger(RUN, REPO, max_usd=10.0, now=NOW, revalidation_max_usd=2.0)

    normal, rework = _rid("normal"), _rid("rework")
    await repo.reserve_repo_budget(RUN, REPO, reservation_id=normal, amount_usd=3.0, now=NOW)
    await repo.settle_repo_budget(
        RUN, REPO, reservation_id=normal, reserved_usd=3.0, actual_usd=1.25, now=NOW
    )

    await repo.reserve_repo_budget(RUN, REPO, reservation_id=rework, amount_usd=1.0, now=NOW)
    await repo.settle_repo_budget(
        RUN, REPO, reservation_id=rework, reserved_usd=1.0, actual_usd=0.75, now=NOW,
        revalidation=True,
    )

    repo_row = await repo.get_repo_budget(RUN, REPO)
    run_row = await repo.get_budget(RUN)
    assert repo_row is not None and run_row is not None
    assert repo_row.reserved_usd == pytest.approx(0.0)
    assert repo_row.spent_usd == pytest.approx(2.0)
    assert repo_row.revalidation_usd == pytest.approx(0.75), "rework was billed as ordinary spend"
    assert run_row.spent_usd == pytest.approx(2.0), "the two ledgers drifted"

    # The rework sub-ceiling is enforced by the settle statement, not asserted afterwards.
    over = _rid("over-rework")
    await repo.reserve_repo_budget(RUN, REPO, reservation_id=over, amount_usd=2.0, now=NOW)
    with pytest.raises(RepoBudgetRefusedError):
        await repo.settle_repo_budget(
            RUN, REPO, reservation_id=over, reserved_usd=2.0, actual_usd=2.0, now=NOW,
            revalidation=True,
        )


async def test_a_halt_is_written_to_disk_and_refuses_every_later_reservation(
    repo: SqliteStateRepository,
) -> None:
    """`halted = 1` is WRITTEN, sticky, idempotent — and the CAS itself is what enforces it.

    Why: nothing in the harness ever wrote this column. `CostLedger.halt()` set an in-process
    flag, so the durable halt was only ever read and never written, and the "fail-closed and
    sticky" state §11.2 promises evaporated with the process that entered it. A halted run that
    resumes and keeps spending is the exact runaway the ceiling exists to stop.
    """
    await repo.open_budget_ledger(RUN, max_usd=100.0, now=NOW)
    await repo.open_repo_ledger(RUN, REPO, max_usd=10.0, now=NOW)

    await repo.halt_budget_ledger(RUN, now=NOW)
    await repo.halt_budget_ledger(RUN, now=NOW)  # sticky: re-halting is not a refusal

    ledger = await repo.get_budget(RUN)
    assert ledger is not None and ledger.halted is True

    with pytest.raises(BudgetRefusedError):
        await repo.reserve_repo_budget(
            RUN, REPO, reservation_id=_rid(), amount_usd=0.01, now=NOW
        )
    repo_row = await repo.get_repo_budget(RUN, REPO)
    assert repo_row is not None
    assert repo_row.reserved_usd == pytest.approx(0.0), "a halted run still held repo money"

    with pytest.raises(RepositoryError, match="no budget_ledger row"):
        await repo.halt_budget_ledger("99999999-9999-4999-8999-999999999999", now=NOW)


# ======================================================================================
# primitive 4, v8 — WHOSE dollars: the reaper, per reservation
# ======================================================================================


async def _two_leased_phases(store: SqliteStateRepository) -> tuple[int, int]:
    """One doomed worker on REPO and one healthy worker on OTHER, both holding a phase lease."""
    await store.open_budget_ledger(RUN, max_usd=10.0, now=NOW)
    for repo_id in (REPO, OTHER):
        await store.open_repo_ledger(RUN, repo_id, max_usd=6.0, now=NOW)
        await store.upsert_phase(RUN, repo_id, Phase.TRANSFORM, now=NOW)
    doomed = await store.acquire_phase_lease(
        RUN, REPO, Phase.TRANSFORM, owner=WORKER, now=NOW, lease_ttl_s=30
    )
    healthy = await store.acquire_phase_lease(
        RUN, OTHER, Phase.TRANSFORM, owner=f"{WORKER}-live", now=NOW, lease_ttl_s=7200
    )
    return doomed, healthy


async def test_the_reaper_releases_the_dead_holders_money_and_not_the_live_holders(
    repo: SqliteStateRepository,
) -> None:
    """One expired hold and one live hold: exactly the expired dollars come back.

    Why: THIS is why the table exists. Before v8 both ledgers carried a scalar `reserved_usd` and
    a single `reservation_expires_at` that every reserver overwrote, so a reaper had no way to
    know how much of the aggregate belonged to the expired holder. The only implementable
    behaviour was to release the aggregate — which zeroes the live worker's hold too, so the run
    under-counts what it has committed and overspends the ceiling it just cleared. The naive
    implementation passes every other assertion in this file and fails this one.

    The other half is that the headroom genuinely returns: a killed worker's hold must not ratchet
    `reserved_usd` upward until a healthy 250-repo run halts on money it never spent.
    """
    doomed_fence, live_fence = await _two_leased_phases(repo)
    dead, live = _rid("dead"), _rid("live")
    await repo.reserve_repo_budget(
        RUN, REPO, reservation_id=dead, amount_usd=4.0, now=NOW,
        expires_at=NOW + timedelta(seconds=30), phase=Phase.TRANSFORM, lease_fence=doomed_fence,
    )
    await repo.reserve_repo_budget(
        RUN, OTHER, reservation_id=live, amount_usd=4.0, now=NOW,
        expires_at=NOW + timedelta(hours=2), phase=Phase.TRANSFORM, lease_fence=live_fence,
    )
    run_before = await repo.get_budget(RUN)
    assert run_before is not None and run_before.reserved_usd == pytest.approx(8.0)

    await repo.reap_expired_phase_leases(RUN, now=NOW + timedelta(seconds=60))

    run_after = await repo.get_budget(RUN)
    dead_repo = await repo.get_repo_budget(RUN, REPO)
    live_repo = await repo.get_repo_budget(RUN, OTHER)
    assert run_after is not None and dead_repo is not None and live_repo is not None
    assert run_after.reserved_usd == pytest.approx(4.0), (
        "the run ledger released the aggregate, not the expired holder's share — the live "
        "worker's $4 was freed underneath it"
    )
    assert dead_repo.reserved_usd == pytest.approx(0.0), "the expired hold was not released"
    assert live_repo.reserved_usd == pytest.approx(4.0), "a LIVE worker's hold was reaped"

    dead_row = await repo.get_reservation(RUN, dead)
    live_row = await repo.get_reservation(RUN, live)
    assert dead_row is not None and dead_row.state == "EXPIRED"
    assert live_row is not None and live_row.state == "HELD"

    # The headroom is real, not just a smaller number: it is spendable again.
    await repo.reserve_repo_budget(RUN, REPO, reservation_id=_rid(), amount_usd=4.0, now=NOW)
    # ...and the live worker still owns its money and can settle it.
    await repo.settle_repo_budget(
        RUN, OTHER, reservation_id=live, reserved_usd=4.0, actual_usd=2.5, now=NOW
    )
    settled = await repo.get_repo_budget(RUN, OTHER)
    assert settled is not None and settled.spent_usd == pytest.approx(2.5)


async def test_the_release_and_the_fence_bump_are_one_transaction(
    repo: SqliteStateRepository,
) -> None:
    """A reaped worker cannot settle the hold it lost, and its fenced writes match zero rows.

    Why: §6 requires the release and the owning `phases.lease_fence` bump in ONE transaction. If
    they were two, the window between them is a worker settling a reservation the reaper has
    already released — moving money out of a `reserved_usd` that no longer holds it, which grows
    `spent_usd` against a hold that was refunded. Both refusals below are the same commit: the
    reservation is no longer HELD *and* the fence it was granted under is stale.
    """
    doomed_fence, _ = await _two_leased_phases(repo)
    dead = _rid("dead")
    await repo.reserve_repo_budget(
        RUN, REPO, reservation_id=dead, amount_usd=4.0, now=NOW,
        expires_at=NOW + timedelta(seconds=30), phase=Phase.TRANSFORM, lease_fence=doomed_fence,
    )

    later = NOW + timedelta(seconds=60)
    await repo.reap_expired_phase_leases(RUN, now=later)

    with pytest.raises(ReservationRefusedError) as refused:
        await repo.settle_repo_budget(
            RUN, REPO, reservation_id=dead, reserved_usd=4.0, actual_usd=3.9, now=later
        )
    assert isinstance(refused.value, BudgetRefusedError)
    assert not isinstance(refused.value, RepoBudgetRefusedError), (
        "no ceiling refused here; treating it as one would send a healthy repo to a human"
    )
    with pytest.raises(LeaseStolenError):
        await repo.complete_phase(
            RUN, REPO, Phase.TRANSFORM, fence=doomed_fence,
            status=RepoStatus.SUCCEEDED, now=later, last_error=None,
        )

    phase = await repo.get_phase(RUN, REPO, Phase.TRANSFORM)
    ledger = await repo.get_repo_budget(RUN, REPO)
    assert phase is not None and ledger is not None
    assert phase.lease_fence > doomed_fence, "the fence was not bumped with the release"
    assert ledger.spent_usd == pytest.approx(0.0), "the refused settlement moved money anyway"
    assert ledger.reserved_usd == pytest.approx(0.0)


async def test_concurrent_reserve_and_settle_never_exceed_either_ceiling(
    repo: SqliteStateRepository,
) -> None:
    """Twenty-four racers reserve $1 and settle it against a $10 repo inside a $1 000 run.

    Why: the per-row accounting added at v8 must not weaken the guard that made the aggregate
    safe. Each reservation is now an INSERT plus two conditional `UPDATE`s in one transaction, and
    if the ceiling check were read outside that `WHERE` — or if the row and the aggregate could
    disagree — twelve workers would again each reserve $1 against a stale read and all twelve
    would write. The invariant asserted is the one that matters at both scopes: neither
    `spent + reserved` exceeds its `max_usd`, and the rows explain the aggregate exactly.
    """
    await repo.open_budget_ledger(RUN, max_usd=1_000.0, now=NOW)
    await repo.open_repo_ledger(RUN, REPO, max_usd=10.0, now=NOW)

    async def attempt() -> bool:
        reservation_id = _rid()
        try:
            await repo.reserve_repo_budget(
                RUN, REPO, reservation_id=reservation_id, amount_usd=1.0, now=NOW
            )
        except BudgetRefusedError:
            return False
        await repo.settle_repo_budget(
            RUN, REPO, reservation_id=reservation_id, reserved_usd=1.0, actual_usd=1.0, now=NOW
        )
        return True

    outcomes = await asyncio.gather(*(attempt() for _ in range(24)))
    assert sum(outcomes) == 10, "more money was granted than the repo ceiling allows"

    repo_row = await repo.get_repo_budget(RUN, REPO)
    run_row = await repo.get_budget(RUN)
    assert repo_row is not None and run_row is not None
    assert repo_row.spent_usd + repo_row.reserved_usd <= repo_row.max_usd
    assert run_row.spent_usd + run_row.reserved_usd <= run_row.max_usd
    assert repo_row.spent_usd == pytest.approx(10.0)
    assert run_row.spent_usd == pytest.approx(10.0), "each dollar is spent once in EACH ledger"

    states = dict(
        await _reservation_states(repo)
    )
    assert states == {"SETTLED": 10}, f"the rows must explain the aggregate exactly: {states}"


async def _reservation_states(store: SqliteStateRepository) -> list[tuple[str, int]]:
    """The durable table itself, grouped by state — the aggregate's own explanation."""
    async with store._read.execute(
        "SELECT state, COUNT(*) FROM reservations WHERE run_id = ? GROUP BY state", (RUN,)
    ) as cursor:
        return [(str(row[0]), int(row[1])) for row in await cursor.fetchall()]


async def test_a_reservation_refusal_is_a_typed_error_and_never_a_falsy_return(
    repo: SqliteStateRepository,
) -> None:
    """Re-using an id, settling twice, and settling a different amount all RAISE.

    Why: every one of these is a way for two claims to be made on the same dollars. A `bool`
    return would let the caller carry on and settle anyway, and the aggregate `reserved_usd`
    would silently stop matching the rows that are supposed to explain it — which is exactly the
    state v7 was permanently in.
    """
    await repo.open_budget_ledger(RUN, max_usd=100.0, now=NOW)
    await repo.open_repo_ledger(RUN, REPO, max_usd=10.0, now=NOW)
    held = _rid("held")
    assert await repo.reserve_repo_budget(
        RUN, REPO, reservation_id=held, amount_usd=3.0, now=NOW
    ) is None, "a grant returns nothing: there is no truthy/falsy protocol here"

    with pytest.raises(ReservationRefusedError, match="already recorded"):
        await repo.reserve_repo_budget(RUN, REPO, reservation_id=held, amount_usd=1.0, now=NOW)
    with pytest.raises(ReservationRefusedError, match="REFUSED"):
        await repo.settle_repo_budget(
            RUN, REPO, reservation_id=held, reserved_usd=2.0, actual_usd=2.0, now=NOW
        )

    await repo.settle_repo_budget(
        RUN, REPO, reservation_id=held, reserved_usd=3.0, actual_usd=1.0, now=NOW
    )
    with pytest.raises(ReservationRefusedError):
        await repo.settle_repo_budget(
            RUN, REPO, reservation_id=held, reserved_usd=3.0, actual_usd=1.0, now=NOW
        )

    ledger = await repo.get_repo_budget(RUN, REPO)
    assert ledger is not None
    assert ledger.spent_usd == pytest.approx(1.0), "a refused settlement moved money"
    assert ledger.reserved_usd == pytest.approx(0.0)


async def test_an_owner_must_be_a_phase_and_a_fence_together(
    repo: SqliteStateRepository,
) -> None:
    """Half an owner is refused. Why: a phase with no fence is an owner the reaper cannot
    invalidate — it would release the money and leave the dead holder able to settle it."""
    await repo.open_budget_ledger(RUN, max_usd=100.0, now=NOW)
    await repo.open_repo_ledger(RUN, REPO, max_usd=10.0, now=NOW)
    await repo.upsert_phase(RUN, REPO, Phase.TRANSFORM, now=NOW)

    with pytest.raises(RepositoryError, match="must be set together"):
        await repo.reserve_repo_budget(
            RUN, REPO, reservation_id=_rid(), amount_usd=1.0, now=NOW, phase=Phase.TRANSFORM
        )

    ledger = await repo.get_repo_budget(RUN, REPO)
    assert ledger is not None and ledger.reserved_usd == pytest.approx(0.0)


# ======================================================================================
# §11 "Query results" — the iter_* generators
# ======================================================================================


@pytest.mark.parametrize("name", ["iter_symbols", "iter_edges", "iter_events", "iter_attempts"])
async def test_every_unbounded_table_is_read_as_an_async_generator(
    repo: SqliteStateRepository, name: str
) -> None:
    """Each of §11's four unbounded tables is streamed, not folded.

    Why: `symbols`, `edges`, `events` and `attempts` are the only tables that exceed 10 000 rows,
    and §11 makes returning a `list` from any of them a review-blocking defect — a 250-repo run
    holds ~10⁵ events, and materialising them is the §13 row 8 RSS breach that sheds concurrency
    and exits 5.
    """
    method = getattr(repo, name)
    assert inspect.isasyncgenfunction(method), f"{name} must stream, never return a list"


async def test_iter_symbols_streams_every_row_of_a_large_batch(
    repo: SqliteStateRepository,
) -> None:
    """2 500 symbols go in; 2 500 come back out, one at a time.

    Why: batched fetching is only correct if the batch boundary is invisible to the caller. An
    off-by-one in the `fetchmany` loop silently truncates the symbol index, and a missing symbol
    is a rewrite that never happens rather than an error anybody sees.
    """
    total = 2_500
    written = await repo.insert_symbols(
        [
            SymbolRow(
                run_id=RUN,
                repo_id=REPO,
                fqn=f"com.acme.Commons{i}",
                kind="class",
                path="src/main/java/com/acme/Commons.java",
                line=i,
                language="java",
                is_definition=True,
            )
            for i in range(total)
        ]
    )
    assert written == total

    seen = 0
    last_id = 0
    async for row in repo.iter_symbols(RUN):
        seen += 1
        assert row.symbol_id is not None and row.symbol_id > last_id
        last_id = row.symbol_id
    assert seen == total


async def test_iter_edges_streams_every_row_of_a_large_batch(
    repo: SqliteStateRepository,
) -> None:
    """2 100 edges go in; 2 100 stream back. Why: same boundary bug, and a dropped edge silently
    reorders the wave plan — a repo builds before the dependency it needs."""
    total = 2_100
    await repo.insert_edges(
        [
            EdgeRow(
                edge_key=f"{i:064x}",
                run_id=RUN,
                src_id=REPO,
                dst_coord_key=f"maven:com.acme:lib{i}",
                kind="COMPILE",
                base_confidence=0.9,
                confidence=0.9,
                evidence_path="pom.xml",
                evidence_line=i,
                detected_at=NOW.isoformat(),
            )
            for i in range(total)
        ]
    )

    keys = set()
    async for edge in repo.iter_edges(RUN):
        keys.add(edge.edge_key)
    assert len(keys) == total


async def test_iter_events_streams_in_seq_order_with_seq_allocated_in_statement(
    repo: SqliteStateRepository,
) -> None:
    """Events come back ordered by `seq`, and `seq` was allocated by the INSERT itself.

    Why: `seq`, not `ts`, is the ordering key (§6) — wall clock is for humans. And a
    `MAX(seq) + 1` computed in Python makes the second concurrent emitter raise `IntegrityError`:
    telemetry killing a worker.
    """
    count = 30
    seqs = await asyncio.gather(
        *(
            repo.append_event(
                EventRow(
                    run_id=RUN,
                    seq=0,  # ignored: the INSERT allocates it
                    ts=NOW.isoformat(),
                    level="info",
                    event="phase.started",
                    event_uid=f"uid-{i}",
                    repo_id=REPO,
                    phase=Phase.SCAN,
                )
            )
            for i in range(count)
        )
    )
    assert sorted(seqs) == list(range(1, count + 1))

    streamed = [row.seq async for row in repo.iter_events(RUN)]
    assert streamed == sorted(streamed) == list(range(1, count + 1))

    # A replayed JSONL tail must not mint a second row (§6 `DO NOTHING` on (run_id, event_uid)).
    replayed = await repo.append_event(
        EventRow(
            run_id=RUN,
            seq=0,
            ts=NOW.isoformat(),
            level="info",
            event="phase.started",
            event_uid="uid-0",
            repo_id=REPO,
            phase=Phase.SCAN,
        )
    )
    assert replayed == 1
    assert len([row async for row in repo.iter_events(RUN)]) == count


async def test_a_re_executed_command_overwrites_its_own_stale_outcome(
    repo: SqliteStateRepository,
) -> None:
    """Re-recording the same attempt key replaces the outcome columns, and streams back once.

    Why: §6 — under a plain `DO NOTHING` a re-executed command's outcome is discarded and the
    repair loop is composed from the STALE first log, so the LLM is handed an error that no
    longer describes the tree.
    """
    base = AttemptRow(
        attempt_id="attempt-1",
        run_id=RUN,
        repo_id=REPO,
        phase=Phase.BUILD,
        attempt=1,
        started_at=NOW.isoformat(),
        finished_at=NOW.isoformat(),
        command='["bazel","build","//..."]',
        exit_code=1,
        stderr_tail="stale: missing dep //libs/acme:commons",
    )
    await repo.record_attempt(base)

    await repo.record_attempt(
        replace(base, exit_code=0, stderr_tail="", stdout_tail="INFO: Build completed")
    )

    rows = [row async for row in repo.iter_attempts(RUN)]
    assert len(rows) == 1, "the re-execution must overwrite, never duplicate"
    assert rows[0].exit_code == 0
    assert rows[0].stderr_tail == ""


async def test_record_attempt_redacts_a_credential_in_stdout_and_stderr_tail_before_the_write(
    repo: SqliteStateRepository,
) -> None:
    """D90 (§12.20, SECURITY-RELEVANT): `record_attempt` writes `stdout_tail`/`stderr_tail` with
    no redaction call — D88's own pattern, in the same file, not applied to the sibling columns
    `docs/SPEC.md:6987` names in the same sentence ("`state/repository.py` redacts `last_error`,
    `findings.payload`, and `attempts.*_tail` on write"). Production caller `cli.py:6441` sets
    `stderr_tail=error.stderr_tail`, the same `WorkerError.stderr_tail` value D88 traced for
    `phases.last_error` — a credential embedded in a worker's own captured output persists
    verbatim to this durable, queryable column exactly the way D88's `last_error` gap did.

    The control is `test_a_re_executed_command_overwrites_its_own_stale_outcome` just above: its
    `stderr_tail`/`stdout_tail` values are innocuous and untouched by this fix.
    """
    pat = "github_pat_11ABCDEFG0abcdefghijklmnopqrstuvwxyz0123456789ABCDEF"
    tainted_stdout = f"cloning https://oauth2:{pat}@gitea.local:3001/x.git into /tmp/work"
    tainted_stderr = f"fatal: remote https://oauth2:{pat}@gitea.local:3001/x.git unreachable"

    await repo.record_attempt(
        AttemptRow(
            attempt_id="attempt-1",
            run_id=RUN,
            repo_id=REPO,
            phase=Phase.BUILD,
            attempt=1,
            started_at=NOW.isoformat(),
            finished_at=NOW.isoformat(),
            command='["git","clone"]',
            exit_code=1,
            stdout_tail=tainted_stdout,
            stderr_tail=tainted_stderr,
        )
    )

    rows = [row async for row in repo.iter_attempts(RUN)]
    assert len(rows) == 1
    stdout_tail, stderr_tail = rows[0].stdout_tail, rows[0].stderr_tail
    assert pat not in stdout_tail, f"a live PAT reached attempts.stdout_tail: {stdout_tail!r}"
    assert pat not in stderr_tail, f"a live PAT reached attempts.stderr_tail: {stderr_tail!r}"
    assert "github_pat_" not in stdout_tail and "github_pat_" not in stderr_tail
    assert "«redacted:" in stdout_tail and "«redacted:" in stderr_tail, (
        "the placeholder must survive, or debugging is blind"
    )
    assert "gitea.local" in stdout_tail and "gitea.local" in stderr_tail, (
        "over-redaction destroys the debuggable part too"
    )


async def test_record_attempt_leaves_innocuous_stdout_and_stderr_tail_unchanged(
    repo: SqliteStateRepository,
) -> None:
    """The control for the test above: this fix is not free to over-redact its way to green."""
    await repo.record_attempt(
        AttemptRow(
            attempt_id="attempt-1",
            run_id=RUN,
            repo_id=REPO,
            phase=Phase.BUILD,
            attempt=1,
            started_at=NOW.isoformat(),
            finished_at=NOW.isoformat(),
            command='["bazel","build","//..."]',
            exit_code=0,
            stdout_tail="INFO: Build completed successfully",
            stderr_tail="",
        )
    )

    rows = [row async for row in repo.iter_attempts(RUN)]
    assert len(rows) == 1
    assert rows[0].stdout_tail == "INFO: Build completed successfully"
    assert rows[0].stderr_tail == ""


# ======================================================================================
# the read/write split (§11.5)
# ======================================================================================


async def test_a_write_on_the_read_connection_raises(
    read_conn: aiosqlite.Connection, repo: SqliteStateRepository
) -> None:
    """The handle the repository reads through physically cannot write.

    Why: read-only is not advisory here. It is the mechanism that stops a worker from becoming a
    second writer, which would make the single-writer resume contract (§11.5) unfalsifiable — a
    convention nobody can test is not a constraint.
    """
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        await read_conn.execute(
            "INSERT INTO repos (repo_id, name, url, updated_at) VALUES (?, ?, ?, ?)",
            ("sneaky", "sneaky", "https://example.invalid/x.git", NOW.isoformat()),
        )

    # ...while the same write through the injected StateWriter succeeds.
    await repo.upsert_repo(
        "acme-portal", name="acme-portal", url="https://example.invalid/portal.git", now=NOW
    )
    async with read_conn.execute(
        "SELECT COUNT(*) FROM repos WHERE repo_id = ?", ("acme-portal",)
    ) as cursor:
        found = await cursor.fetchone()
    assert found is not None and found[0] == 1


async def test_a_naive_datetime_is_refused_rather_than_stored(
    repo: SqliteStateRepository,
) -> None:
    """A tz-naive instant raises instead of being written.

    Why: the reaper compares `lease_expires_at < :now` as TEXT. One naive rendering mixed in
    among tz-aware ones sorts wrongly, and a crashed worker's row is then either never reaped
    (its wave blocks forever on `blocked_by`) or reaped while live.
    """
    with pytest.raises(RepositoryError, match="naive datetime"):
        await repo.upsert_phase(RUN, REPO, Phase.SCAN, now=datetime(2026, 8, 9, 12, 0, 0))


# ======================================================================================
# §11.5 step 5 — the demotion write (ADR-0077)
# ======================================================================================


class _Note(BaseModel):
    """A checkpoint payload. Its shape is irrelevant here — only whether the row survives is."""

    note: str


@pytest.fixture
async def demotion_bed(db_path: Path) -> AsyncIterator[tuple[SqliteStateRepository, StateWriter]]:
    """`repo`, plus the `StateWriter` behind it.

    Planting a checkpoint goes through `checkpoints.save(writer, ...)`, and reaching for
    `store._writer` would tie every test below to a private attribute of the class under test.
    """
    async with StateWriter(db_path, owner="test-writer") as writer:
        read_conn = await connect_ro(db_path)
        try:
            store = SqliteStateRepository(writer=writer, read_conn=read_conn)
            await store.upsert_run(
                RUN, started_at=NOW, config_sha256="a" * 64, harness_version="0.1.0"
            )
            for repo_id in (REPO, OTHER):
                await store.upsert_repo(
                    repo_id, name=repo_id, url=f"https://example.invalid/{repo_id}.git", now=NOW
                )
            yield store, writer
        finally:
            await read_conn.close()


async def _settle_phase(
    store: SqliteStateRepository,
    repo_id: str,
    phase: Phase,
    *,
    status: RepoStatus,
    attempts: int,
) -> None:
    """Drive one phase to `status` with exactly `attempts` recorded, through the public API only.

    Every increment is earned by a real `complete_phase`, so the `attempts` values the demotion
    tests assert on are values the harness itself wrote — not numbers a fixture invented with raw
    SQL that no production path could produce.
    """
    assert attempts >= 1, "a status is only reachable through a completion, so 0 would be a no-op"
    await store.upsert_phase(RUN, repo_id, phase, now=NOW, max_attempts=8)
    for index in range(attempts):
        fence = await store.acquire_phase_lease(
            RUN, repo_id, phase, owner=WORKER, now=NOW, lease_ttl_s=300
        )
        assert fence is not None
        await store.complete_phase(
            RUN,
            repo_id,
            phase,
            fence=fence,
            status=status if index == attempts - 1 else RepoStatus.PENDING,
            now=NOW,
        )


async def _plant_checkpoint(writer: StateWriter, repo_id: str, phase: Phase) -> None:
    await checkpoints.save(
        writer,
        run_id=uuid.UUID(RUN),
        repo_id=repo_id,
        phase=phase,
        payload=_Note(note=f"{repo_id}:{int(phase)}"),
    )


async def _quarantine_phase(writer: StateWriter, repo_id: str, phase: Phase) -> None:
    """Move one already-`PENDING` phase row to `SKIPPED`, the statement `fleet quarantine` uses.

    Raw SQL deliberately, and not a shortcut around the state machine: no `SqliteStateRepository`
    method writes `SKIPPED` at all, and `complete_phase` cannot reach it either
    (`ALLOWED_TRANSITIONS[RUNNING]` carries no `SKIPPED` edge). The only production writers are
    `cli.py`'s own `UPDATE phases SET status = 'SKIPPED'` statements, of which this is one — so
    the row this plants is a row the harness really produces, via `PENDING -> SKIPPED`.
    """

    async def unit(conn: aiosqlite.Connection) -> None:
        await conn.execute(
            "UPDATE phases SET status = 'SKIPPED', updated_at = ? "
            " WHERE run_id = ? AND repo_id = ? AND phase = ?",
            (NOW.isoformat(), RUN, repo_id, int(phase)),
        )

    await writer.submit(unit)


async def _phase_state(
    conn: aiosqlite.Connection, repo_id: str
) -> dict[int, tuple[str, int]]:
    """`{phase: (status, attempts)}`. Named columns, never `SELECT *`: `updated_at` moves on
    every write, so a row-identity assertion would flap for a reason unrelated to the property."""
    async with conn.execute(
        "SELECT phase, status, attempts FROM phases WHERE run_id = ? AND repo_id = ?",
        (RUN, repo_id),
    ) as cursor:
        return {int(row[0]): (str(row[1]), int(row[2])) for row in await cursor.fetchall()}


async def _checkpoint_phases(conn: aiosqlite.Connection, repo_id: str) -> set[int]:
    async with conn.execute(
        "SELECT phase FROM checkpoints WHERE run_id = ? AND repo_id = ?", (RUN, repo_id)
    ) as cursor:
        return {int(row[0]) for row in await cursor.fetchall()}


async def _demotion_findings(
    conn: aiosqlite.Connection, repo_id: str
) -> list[tuple[str, dict[str, object]]]:
    async with conn.execute(
        "SELECT fingerprint, payload FROM findings "
        " WHERE run_id = ? AND repo_id = ? AND kind = ?",
        (RUN, repo_id, PHASE_DEMOTED_KIND),
    ) as cursor:
        return [(str(row[0]), json.loads(str(row[1]))) for row in await cursor.fetchall()]


async def _four_succeeded(
    store: SqliteStateRepository, writer: StateWriter, repo_id: str
) -> None:
    """All four phases SUCCEEDED with attempts 1, 2, 3, 4 and a checkpoint on each.

    The four attempts values are deliberately distinct: a demotion that reset the column would
    still pass an "all equal" assertion, and a demotion that incremented it would still pass an
    "attempts > 0" one.
    """
    for phase in Phase:
        await _settle_phase(
            store, repo_id, phase, status=RepoStatus.SUCCEEDED, attempts=int(phase)
        )
        await _plant_checkpoint(writer, repo_id, phase)


async def test_demoting_to_a_floor_pends_the_span_keeps_every_attempt_and_drops_its_checkpoints(
    demotion_bed: tuple[SqliteStateRepository, StateWriter], read_conn: aiosqlite.Connection
) -> None:
    """Frontier 4, floor 2: phases 2, 3 and 4 go PENDING, phase 1 does not, every `attempts`
    value survives, and the checkpoints for 2-4 are gone.

    Why each half matters. A demotion that missed a phase leaves the repo re-entering at 2 while
    3 and 4 still claim to be finished, so the scheduler admits a repo whose later phases will
    never run again. A demotion that *reset* `attempts` hands a repo that has already burned its
    ladder a fresh one, and §6's escalation to `REQUIRES_HUMAN_INTERVENTION` — the only thing
    that stops an infinite retry loop — never fires. A demotion that left the checkpoints behind
    is worse than either: `checkpoints.load()` would hand the re-run phase the *previous* run's
    finished payload and report it `usable`, so the phase would "succeed" without doing anything.
    """
    store, writer = demotion_bed
    await _four_succeeded(store, writer, REPO)
    await _four_succeeded(store, writer, OTHER)

    demotions = await store.demote_to_floor(
        RUN, REPO, floor=Phase.TRANSFORM, reason="phase-2 evidence gone", now=NOW
    )

    assert [record.phase for record in demotions] == [Phase.TRANSFORM, Phase.BUILD, Phase.VERIFY]
    assert all(record.from_status is RepoStatus.SUCCEEDED for record in demotions)
    assert all(record.to_status is RepoStatus.PENDING for record in demotions)

    # The status claim and the attempts claim are asserted SEPARATELY, so that a mutation which
    # breaks one cannot be masked by the other failing first — each assertion below is the
    # discriminating one for its own property.
    state = await _phase_state(read_conn, REPO)
    assert {phase: state[phase][0] for phase in (1, 2, 3, 4)} == {
        1: "SUCCEEDED",  # below the floor: not re-entry territory
        2: "PENDING",
        3: "PENDING",
        4: "PENDING",
    }
    assert {phase: state[phase][1] for phase in (1, 2, 3, 4)} == {1: 1, 2: 2, 3: 3, 4: 4}, (
        "every attempts value is retained — asserted on the column, not inferred from the status"
    )

    assert await _checkpoint_phases(read_conn, REPO) == {1}

    assert await _phase_state(read_conn, OTHER) == {
        1: ("SUCCEEDED", 1),
        2: ("SUCCEEDED", 2),
        3: ("SUCCEEDED", 3),
        4: ("SUCCEEDED", 4),
    }, "the demotion is scoped to one repo_id"
    assert await _checkpoint_phases(read_conn, OTHER) == {1, 2, 3, 4}


async def test_a_demotion_writes_one_phasedemoted_finding_per_phase_and_never_a_silent_one(
    demotion_bed: tuple[SqliteStateRepository, StateWriter], read_conn: aiosqlite.Connection
) -> None:
    """Three demoted phases produce three `PhaseDemoted` rows, one per phase.

    Why: a demotion throws away landed, green work, and ADR-0077 makes the finding an obligation
    the caller cannot decline. This is not hypothetical: until `f02d124`, `docs/SPEC.md`
    Constraint 7 told this method's author to write the demotion through
    `transition(..., resume=True)` — which returns a bare status and emits nothing at all — so a
    writer who followed the SPEC would have passed every status assertion in the test above while
    every demotion in the fleet went unrecorded. Only an assertion on the finding row itself can
    see that, which is measured rather than asserted: removing the `findings` INSERT leaves the
    status/attempts/checkpoint test GREEN and fails only this one.

    Why *per phase*: `PhaseDemotion.payload()`'s docstring records the hazard —
    `cli._note_finding` fingerprints on `(run_id, repo_id, kind)` alone and UPSERTs, so three
    per-phase calls through it collapse into ONE row and two demotions disappear silently.
    """
    store, writer = demotion_bed
    await _four_succeeded(store, writer, REPO)

    await store.demote_to_floor(
        RUN, REPO, floor=Phase.TRANSFORM, reason="phase-2 evidence gone", now=NOW
    )

    rows = await _demotion_findings(read_conn, REPO)
    assert len(rows) == 3, "one row per demoted phase — not one row per repo"
    assert len({fingerprint for fingerprint, _ in rows}) == 3, (
        "the fingerprints must differ per phase or the rows UPSERT over each other"
    )
    assert sorted(int(payload["phase"]) for _, payload in rows) == [2, 3, 4]
    for _, payload in rows:
        assert payload["repo_id"] == REPO
        assert payload["from_status"] == "SUCCEEDED"
        assert payload["to_status"] == "PENDING"
        assert payload["reason"] == "phase-2 evidence gone"

    # A second call over the SAME rows demotes nothing: phases 2-4 are PENDING now, so the
    # `is not RepoStatus.SUCCEEDED` filter skips every one and no INSERT runs. Asserting the row
    # count here would say nothing about §11.7 — the count is held by the absence of a write, not
    # by the UPSERT. What it does bind is that the no-op really is one: the return value is empty
    # and no fourth row appears. The §11.7 claim is bound by the test below, on a demotion that
    # actually reaches the INSERT a second time.
    assert (
        await store.demote_to_floor(
            RUN, REPO, floor=Phase.TRANSFORM, reason="phase-2 evidence gone", now=NOW
        )
        == ()
    ), "already-PENDING rows are not demotable, so the second call writes nothing at all"
    assert len(await _demotion_findings(read_conn, REPO)) == 3


async def test_re_demoting_a_re_succeeded_phase_upserts_its_finding_instead_of_colliding(
    demotion_bed: tuple[SqliteStateRepository, StateWriter], read_conn: aiosqlite.Connection
) -> None:
    """§11.7 on the path that actually reaches the INSERT twice: demote, let phases 2-4 succeed
    again, demote again. Three rows still, carrying the SECOND reason.

    Why this test exists at all, and why the obvious one does not do its job. Re-calling
    `demote_to_floor` on the rows it just demoted is a **no-op** — they are `PENDING`, the
    `is not RepoStatus.SUCCEEDED` filter skips them, `demotions` is empty and the findings INSERT
    never executes. A row count asserted after that call is satisfied by the absence of any write,
    so it holds identically with `_DEMOTE_FINDING_SQL`'s entire `ON CONFLICT ... DO UPDATE` tail
    deleted. Measured, not argued: with the clause removed the whole file stayed green, which is
    the old-passes case Rule 12 asks for. This test re-earns the SUCCEEDED status first, so the
    second demotion re-executes the INSERT against `ux_findings_ident`
    (`src/fleet/state/schema.sql`) with the same per-phase fingerprint — without the clause it
    raises `IntegrityError: UNIQUE constraint failed`, aborting the write unit and rolling back a
    demotion that has done nothing wrong.

    The `reason` assertion is the second half: `DO UPDATE SET payload = excluded.payload` must
    actually *replace* the row, not merely tolerate the collision. `DO NOTHING` would keep the
    count at three while leaving the stale reason behind, and a reader of `findings` would be
    told the repo was demoted for a cause that no longer applies.
    """
    store, writer = demotion_bed
    await _four_succeeded(store, writer, REPO)

    await store.demote_to_floor(RUN, REPO, floor=Phase.TRANSFORM, reason="r1", now=NOW)
    first = await _demotion_findings(read_conn, REPO)
    assert len(first) == 3
    assert {payload["reason"] for _, payload in first} == {"r1"}

    for phase in (Phase.TRANSFORM, Phase.BUILD, Phase.VERIFY):
        await _settle_phase(store, REPO, phase, status=RepoStatus.SUCCEEDED, attempts=1)

    demotions = await store.demote_to_floor(
        RUN, REPO, floor=Phase.TRANSFORM, reason="r2", now=NOW
    )

    assert [record.phase for record in demotions] == [Phase.TRANSFORM, Phase.BUILD, Phase.VERIFY]
    second = await _demotion_findings(read_conn, REPO)
    assert len(second) == 3, "the same three phases converge on the same three rows"
    assert {fingerprint for fingerprint, _ in second} == {
        fingerprint for fingerprint, _ in first
    }, "the identity is the semantic one, so the rows are updated in place rather than added to"
    assert {payload["reason"] for _, payload in second} == {"r2"}, (
        "DO UPDATE replaces the payload — a DO NOTHING would leave the first run's reason"
    )


async def test_the_demotion_is_one_write_unit_so_a_failure_leaves_no_half_demoted_repo(
    demotion_bed: tuple[SqliteStateRepository, StateWriter],
    read_conn: aiosqlite.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One `submit`, and a raise partway through rolls the whole thing back.

    Two assertions, because either alone is weak. The `submit` count alone is a proxy — it counts
    units, not transactions. The rollback alone would pass if the status write happened to be in
    the *last* of three units. Together they pin the property the acceptance criterion states:
    one unit, therefore one `BEGIN IMMEDIATE`, therefore all-or-nothing.

    Why it must be all-or-nothing: the failure mode is not a lost demotion, it is a durable lie.
    A crash between a committed `phases` UPDATE and an uncommitted checkpoint delete leaves a
    PENDING phase carrying the previous run's finished payload, which `checkpoints.load()` reports
    as `usable` forever after. Split into two units, that state is reachable by a power cut.
    """
    store, writer = demotion_bed
    await _four_succeeded(store, writer, REPO)

    submits = 0
    original = writer.submit

    async def counting_submit(unit: object) -> object:
        nonlocal submits
        submits += 1
        return await original(unit)  # type: ignore[arg-type]

    monkeypatch.setattr(writer, "submit", counting_submit)

    await store.demote_to_floor(
        RUN, REPO, floor=Phase.TRANSFORM, reason="one unit", now=NOW
    )
    assert submits == 1, "the status change, the checkpoint deletion and the findings are one unit"

    # Now make the unit fail *after* the phase UPDATE has already executed inside it.
    await _four_succeeded(store, writer, OTHER)
    boom = RuntimeError("injected mid-unit failure")

    async def exploding_delete(*args: object, **kwargs: object) -> int:
        raise boom

    monkeypatch.setattr(checkpoints, "delete_in_unit", exploding_delete)

    with pytest.raises(RuntimeError, match="injected mid-unit failure"):
        await store.demote_to_floor(
            RUN, OTHER, floor=Phase.TRANSFORM, reason="one unit", now=NOW
        )

    assert await _phase_state(read_conn, OTHER) == {
        1: ("SUCCEEDED", 1),
        2: ("SUCCEEDED", 2),
        3: ("SUCCEEDED", 3),
        4: ("SUCCEEDED", 4),
    }, "the phase UPDATE ran before the raise; it must not have survived it"
    assert await _checkpoint_phases(read_conn, OTHER) == {1, 2, 3, 4}
    assert await _demotion_findings(read_conn, OTHER) == []


async def test_a_repo_with_a_requires_human_intervention_row_is_left_entirely_untouched(
    demotion_bed: tuple[SqliteStateRepository, StateWriter], read_conn: aiosqlite.Connection
) -> None:
    """One RHI row anywhere in the repo and the whole demotion is a no-op.

    Why the writer re-reads it instead of trusting `phase_floor`: §12 item 46 (ii) requires that
    no automatic sweep — and it names `fleet resume` — can move a repo out of
    `REQUIRES_HUMAN_INTERVENTION`. `phase_floor` does return `None` for such a repo, but it reads
    through the read-only handle *outside* this transaction. A refusal that lives only in the
    caller is a refusal the next caller forgets, and the demotion here is the write that would
    make the forgetting durable.
    """
    store, writer = demotion_bed
    for phase in Phase:
        status = (
            RepoStatus.REQUIRES_HUMAN_INTERVENTION
            if phase is Phase.BUILD
            else RepoStatus.SUCCEEDED
        )
        await _settle_phase(store, REPO, phase, status=status, attempts=int(phase))
        await _plant_checkpoint(writer, REPO, phase)
    async with read_conn.execute(
        "SELECT status FROM phases WHERE run_id = ? AND repo_id = ? AND phase = 3", (RUN, REPO)
    ) as cursor:
        planted = await cursor.fetchone()
    assert planted is not None and planted[0] == "REQUIRES_HUMAN_INTERVENTION"

    before = await _phase_state(read_conn, REPO)
    demotions = await store.demote_to_floor(
        RUN, REPO, floor=Phase.TRANSFORM, reason="should not happen", now=NOW
    )

    assert demotions == ()
    assert await _phase_state(read_conn, REPO) == before
    assert await _checkpoint_phases(read_conn, REPO) == {1, 2, 3, 4}
    assert await _demotion_findings(read_conn, REPO) == []


async def test_a_degraded_phase_in_the_span_keeps_both_its_status_and_its_checkpoint(
    demotion_bed: tuple[SqliteStateRepository, StateWriter], read_conn: aiosqlite.Connection
) -> None:
    """ADR-0077 §5: step 5 neither demotes a DEGRADED phase nor searches past it.

    Why the checkpoint is spared too, which the design doc's loop does not say: DEGRADED leaves
    the machine only through a budgeted stub-revalidation round (§3.5.1). Routing it to PENDING
    would spend that budget by the back door with no round recorded — and deleting the payload
    the round resumes from forces the same unbudgeted re-run one level down. The neighbours on
    either side are demoted in the same call, so this is a carve-out for DEGRADED specifically
    and not a demotion that quietly stopped early.
    """
    store, writer = demotion_bed
    for phase in Phase:
        status = RepoStatus.DEGRADED if phase is Phase.BUILD else RepoStatus.SUCCEEDED
        await _settle_phase(store, REPO, phase, status=status, attempts=int(phase))
        await _plant_checkpoint(writer, REPO, phase)

    demotions = await store.demote_to_floor(
        RUN, REPO, floor=Phase.TRANSFORM, reason="degraded neighbour", now=NOW
    )

    assert [record.phase for record in demotions] == [Phase.TRANSFORM, Phase.VERIFY]
    state = await _phase_state(read_conn, REPO)
    assert state[3] == ("DEGRADED", 3), "a DEGRADED row is never demoted"
    assert state[2] == ("PENDING", 2)
    assert state[4] == ("PENDING", 4)
    assert await _checkpoint_phases(read_conn, REPO) == {1, 3}
    assert sorted(int(payload["phase"]) for _, payload in await _demotion_findings(
        read_conn, REPO
    )) == [2, 4]


async def test_a_skipped_phase_keeps_its_status_but_still_loses_its_checkpoint(
    demotion_bed: tuple[SqliteStateRepository, StateWriter], read_conn: aiosqlite.Connection
) -> None:
    """ADR-0082 §3: the sweep's carve-out names `DEGRADED` alone, and `SKIPPED` is not in it.

    The asymmetry against the backward walk is the point, so it is pinned rather than left
    incidental. `phase_floor` hard-stops on `SKIPPED` exactly as it does on `DEGRADED`
    (ADR-0077 §5) because the walk protects a **decision** — the operator's config, which resume
    does not re-take. This carve-out protects a different thing: a payload a future round will
    legitimately resume from. `DEGRADED` has such a round and keeps a live `RUNNING` edge for it;
    `SKIPPED` is in `TERMINAL_STATUSES` and maps to the EMPTY transition set, so no round in this
    run re-enters it. Sparing its checkpoint would preserve a payload nothing can read.

    The status half still holds — a `SKIPPED` row is never demoted, because `demote()` refuses
    everything but `SUCCEEDED` — so this test discriminates the two carve-outs from each other
    rather than testing the demotion filter twice.

    The row is planted the way production makes one: a phase that ran, was reset to `PENDING` by
    the crash sweep with its partial checkpoint intact, and was then quarantined. That is the only
    shape in which a `SKIPPED` phase has a checkpoint to lose.
    """
    store, writer = demotion_bed
    for phase in Phase:
        status = RepoStatus.PENDING if phase is Phase.BUILD else RepoStatus.SUCCEEDED
        await _settle_phase(store, REPO, phase, status=status, attempts=int(phase))
        await _plant_checkpoint(writer, REPO, phase)
    await _quarantine_phase(writer, REPO, Phase.BUILD)

    demotions = await store.demote_to_floor(
        RUN, REPO, floor=Phase.TRANSFORM, reason="quarantined neighbour", now=NOW
    )

    assert [record.phase for record in demotions] == [Phase.TRANSFORM, Phase.VERIFY]
    state = await _phase_state(read_conn, REPO)
    assert state[3] == ("SKIPPED", 3), "a SKIPPED row is never demoted — ADR-0077 §5 still holds"
    assert state[2] == ("PENDING", 2)
    assert state[4] == ("PENDING", 4)
    assert await _checkpoint_phases(read_conn, REPO) == {1}, (
        "the SKIPPED phase's checkpoint goes with the rest of the span: only DEGRADED is spared"
    )


async def test_the_checkpoint_assertion_is_silent_on_a_clean_repo_and_fires_on_an_injected_row(
    demotion_bed: tuple[SqliteStateRepository, StateWriter], read_conn: aiosqlite.Connection
) -> None:
    """Instrument validation for the deletion assertion (CLAUDE.md Guardrail 6).

    `_checkpoint_phases(...) == {1}` is the detector every test above leans on, and a detector
    never observed firing is not evidence of absence. Two of its three checks live here: it stays
    SILENT on a repo that never had checkpoints above the floor (no false positive), and it FIRES
    on a synthetic checkpoint injected into that same clean repo *after* the sweep (so it is
    reading live rows rather than a cached or empty result). The third check — that it fires on
    the known-bad state, a demotion with the deletion removed — is a mutation, recorded in the
    task report rather than committed as a test that must fail.
    """
    store, writer = demotion_bed
    for phase in Phase:
        await _settle_phase(
            store, REPO, phase, status=RepoStatus.SUCCEEDED, attempts=int(phase)
        )
    await _plant_checkpoint(writer, REPO, Phase.SCAN)  # below the floor; must survive

    await store.demote_to_floor(
        RUN, REPO, floor=Phase.TRANSFORM, reason="clean fixture", now=NOW
    )
    assert await _checkpoint_phases(read_conn, REPO) == {1}, "silent on the already-swept repo"

    await _plant_checkpoint(writer, REPO, Phase.BUILD)
    assert await _checkpoint_phases(read_conn, REPO) != {1}, (
        "fires on a synthetic checkpoint injected into the clean fixture"
    )
    assert await _checkpoint_phases(read_conn, REPO) == {1, 3}


async def _frontier_at_verify(store: SqliteStateRepository, writer: StateWriter) -> None:
    """The realistic step-5 shape: phases 1-3 SUCCEEDED, phase 4 the unsettled frontier.

    A checkpoint sits on phase 1 (below any floor these tests use) and on phase 4 (the partial
    payload an interrupted VERIFY left behind). Those two are what separate the span-wide sweep
    from the conditional one.
    """
    for phase in (Phase.SCAN, Phase.TRANSFORM, Phase.BUILD):
        await _settle_phase(
            store, REPO, phase, status=RepoStatus.SUCCEEDED, attempts=int(phase)
        )
    await store.upsert_phase(RUN, REPO, Phase.VERIFY, now=NOW, max_attempts=8)
    await _plant_checkpoint(writer, REPO, Phase.SCAN)
    await _plant_checkpoint(writer, REPO, Phase.VERIFY)


async def test_a_demotion_below_the_frontier_also_drops_the_frontiers_own_checkpoint(
    demotion_bed: tuple[SqliteStateRepository, StateWriter], read_conn: aiosqlite.Connection
) -> None:
    """Floor 2 against a frontier at 4: phases 2 and 3 are demoted, and phase 4's *partial*
    checkpoint is deleted even though phase 4 was never `SUCCEEDED` and never demoted.

    Why the sweep is wider than the set of demoted rows: phase 4's payload was written against
    the phase-3 output this call has just discarded. Left in place, `checkpoints.load()` reports
    it `usable` on the next resume and VERIFY re-enters against a tree that no longer exists —
    a green verdict for a build nobody performed. A demotion invalidates everything above it, not
    only the rows whose status it happened to rewrite.
    """
    store, writer = demotion_bed
    await _frontier_at_verify(store, writer)

    demotions = await store.demote_to_floor(
        RUN, REPO, floor=Phase.TRANSFORM, reason="phase-2 evidence gone", now=NOW
    )

    assert [record.phase for record in demotions] == [Phase.TRANSFORM, Phase.BUILD]
    state = await _phase_state(read_conn, REPO)
    assert {phase: state[phase][0] for phase in (1, 2, 3, 4)} == {
        1: "SUCCEEDED",
        2: "PENDING",
        3: "PENDING",
        4: "PENDING",
    }
    assert {phase: state[phase][1] for phase in (1, 2, 3, 4)} == {1: 1, 2: 2, 3: 3, 4: 0}, (
        "every attempts value is retained, including the frontier's untouched 0"
    )
    assert await _checkpoint_phases(read_conn, REPO) == {1}, (
        "the frontier's partial checkpoint goes with the demotion below it; phase 1's stays"
    )


async def test_a_resume_that_demotes_nothing_leaves_the_frontiers_checkpoint_alone(
    demotion_bed: tuple[SqliteStateRepository, StateWriter], read_conn: aiosqlite.Connection
) -> None:
    """Floor == frontier, nothing demotable: the call is a true no-op, checkpoint included.

    Why this is not a missing sweep. `phase_floor` returns the frontier itself whenever the
    backward walk finds the evidence below it intact — the ordinary case of resuming a run that
    was simply interrupted. Nothing below phase 4 was rewritten, so nothing above it was
    invalidated, and deleting the payload would cost the run a whole VERIFY phase on every
    `fleet resume` for no correctness gain. §8's checkpoints exist precisely to survive this.

    The identical fixture one test above, with a floor that *does* demote, loses that same
    checkpoint — which is what makes this a rule and not an omission.
    """
    store, writer = demotion_bed
    await _frontier_at_verify(store, writer)
    before = await _phase_state(read_conn, REPO)

    demotions = await store.demote_to_floor(
        RUN, REPO, floor=Phase.VERIFY, reason="evidence holds all the way up", now=NOW
    )

    assert demotions == ()
    assert await _phase_state(read_conn, REPO) == before
    assert await _checkpoint_phases(read_conn, REPO) == {1, 4}
    assert await _demotion_findings(read_conn, REPO) == []


# --------------------------------------------------------------------------------------
# `observed=` — the floor as a property of the write (ADR-0089 §3)
# --------------------------------------------------------------------------------------


async def test_a_floor_snapshot_that_still_holds_demotes_exactly_as_an_unguarded_call_would(
    demotion_bed: tuple[SqliteStateRepository, StateWriter], read_conn: aiosqlite.Connection
) -> None:
    """The control for the refusal below: a matching `observed` changes nothing.

    Every one of the fourteen demotion tests above this section predates `observed` and passes
    `None`, so the whole guard is exercised only on its default. A guard that refused every call
    would leave all fourteen green while `fleet resume` demoted nothing in production, which is
    why the passing case has to be asserted on the same fixture as the failing one rather than
    inferred from the fourteen.

    `observed` is stated for all four phases including the one *below* the floor, which is the
    shape `cli._demote_to_floors` builds it in.
    """
    store, writer = demotion_bed
    await _four_succeeded(store, writer, REPO)
    observed = dict.fromkeys(Phase, RepoStatus.SUCCEEDED)

    demotions = await store.demote_to_floor(
        RUN, REPO, floor=Phase.TRANSFORM, reason="evidence gone", now=NOW, observed=observed
    )

    assert [record.phase for record in demotions] == [Phase.TRANSFORM, Phase.BUILD, Phase.VERIFY]
    assert {phase: state[0] for phase, state in (await _phase_state(read_conn, REPO)).items()} == {
        1: "SUCCEEDED",
        2: "PENDING",
        3: "PENDING",
        4: "PENDING",
    }


async def test_a_row_below_the_floor_moving_refuses_the_write_and_leaves_the_repo_untouched(
    demotion_bed: tuple[SqliteStateRepository, StateWriter], read_conn: aiosqlite.Connection
) -> None:
    """The staleness check spans EVERY phase, not the demotion's span.

    This is the half of the guard that a span-scoped comparison would silently drop, and the
    reason `demote_to_floor` iterates `Phase` rather than `span`: the floor is the output of a
    backward walk that starts at the frontier and reads *downward*, so a row below the floor
    moving means the walk would have stopped somewhere else and the floor handed to this call is
    an answer to a question about a database that no longer exists. Writing it anyway demotes a
    span nobody computed.

    Phase 1 sits below floor `TRANSFORM` and is not itself demotable, so a check that only looked
    at the span would see four identical statuses and proceed — demoting 2-4 and deleting their
    checkpoints on a stale floor, with no refusal and no operator-visible trace.

    The refusal must also be *total*: no status moved, no checkpoint deleted, no `PhaseDemoted`
    finding minted. A partial refusal would be worse than none, because the operator is told the
    repo was skipped while some of it was rewritten.
    """
    store, writer = demotion_bed
    await _four_succeeded(store, writer, REPO)
    before_state = await _phase_state(read_conn, REPO)
    before_checkpoints = await _checkpoint_phases(read_conn, REPO)

    # The caller looked when phase 1 was still PENDING; it has since landed. Phases 2-4 — the
    # entire span the write would touch — are identical in both readings.
    stale_observation = {
        Phase.SCAN: RepoStatus.PENDING,
        Phase.TRANSFORM: RepoStatus.SUCCEEDED,
        Phase.BUILD: RepoStatus.SUCCEEDED,
        Phase.VERIFY: RepoStatus.SUCCEEDED,
    }

    with pytest.raises(FloorSnapshotStaleError) as caught:
        await store.demote_to_floor(
            RUN,
            REPO,
            floor=Phase.TRANSFORM,
            reason="evidence gone",
            now=NOW,
            observed=stale_observation,
        )

    assert "SCAN was" in str(caught.value), (
        "the message must name the phase that moved — an operator who cannot tell which row "
        "changed cannot tell a race from a bug"
    )
    assert await _phase_state(read_conn, REPO) == before_state
    assert await _checkpoint_phases(read_conn, REPO) == before_checkpoints
    assert await _demotion_findings(read_conn, REPO) == []


async def test_a_phase_with_no_row_reads_as_pending_on_both_sides_of_the_snapshot(
    demotion_bed: tuple[SqliteStateRepository, StateWriter], read_conn: aiosqlite.Connection
) -> None:
    """An absent `phases` row is the schema default, so omitting it from `observed` is not a move.

    `_frontier_at_verify` leaves phase 4 upserted but unsettled; a caller that built `observed`
    from a `SELECT` returning only settled rows would omit it. Both sides default to `PENDING`,
    so this must proceed — otherwise the guard refuses the single most ordinary resume shape
    there is, and does it *after* the fleet-wide Git reads have already been paid for.
    """
    store, writer = demotion_bed
    await _frontier_at_verify(store, writer)

    demotions = await store.demote_to_floor(
        RUN,
        REPO,
        floor=Phase.TRANSFORM,
        reason="evidence gone",
        now=NOW,
        observed={Phase.SCAN: RepoStatus.SUCCEEDED, Phase.TRANSFORM: RepoStatus.SUCCEEDED,
                  Phase.BUILD: RepoStatus.SUCCEEDED},
    )

    assert [record.phase for record in demotions] == [Phase.TRANSFORM, Phase.BUILD]
    assert (await _phase_state(read_conn, REPO))[4][0] == "PENDING"
