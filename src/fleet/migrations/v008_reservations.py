"""7 → 8 — `reservations`, the per-holder identity behind `reserved_usd` (SPEC §6, §5 = 8).

**The defect this rung closes.** `budget_ledger` and `repo_ledger` each carry a scalar
`reserved_usd` and a SINGLE `reservation_expires_at` that every reserver overwrites. That pair
records *that* money is held and nothing about *whose*, so §6's reaper — "release any reservation
past `reservation_expires_at` in the same transaction that bumps the owning `phases.lease_fence`"
— was not implementable: releasing "the expired reservation" could only mean releasing the whole
aggregate, which zeroes every live worker's hold too. The run then under-counts what is committed
and overspends the ceiling. This step adds the table that carries a reservation's identity,
amount, owner, phase and fence.

**Additive only. No table is rebuilt and no ledger number is touched.** One `CREATE TABLE`, one
`CREATE INDEX`, one `INSERT … SELECT`. `spent_usd`, `reserved_usd`, `max_usd`,
`revalidation_usd` and every other recorded value are byte-identical before and after, which is
the whole of "data must survive the step" for a rung that only adds a sidecar.

**The pre-v8 aggregate.** An existing database can hold `reserved_usd > 0` with no per-reservation
rows behind it — money held by workers that were in flight when the harness was upgraded. Three
options, and only one of them is safe:

* *Leave it unattributed* — the aggregate stays held forever and the reaper can never touch it.
  That is the ratchet this whole table exists to remove, on exactly the databases that already
  have it.
* *Zero it* — free money that live workers are still spending, then let them settle on top. That
  under-counts commitments and overspends. Refused.
* *Adopt it as ONE legacy reservation per `repo_ledger` row* — what this step does. The row's
  `amount_usd` is exactly that repo's existing aggregate and it is the only row that claims those
  dollars, so the reaper's `SUM(amount_usd)` over the repo's expired rows can never exceed what
  the ledger holds (no over-release, no under-count of what remains) and no dollar is described
  twice (no double-count). Its `expires_at` is the ledger's own `reservation_expires_at`, so the
  adopted hold expires exactly when the pre-v8 column said it would — a NULL there stays NULL and
  the hold stays immortal, which is no worse than v7 and is the conservative direction.

`phase`/`lease_fence` are NULL on an adopted row: v7 recorded no owner, and inventing one would
make the reaper bump the fence of a phase that never held the money. A NULL owner is still
reapable by expiry; it just has no fence to bump. **The run ledger gets no adopted rows of its
own** — a run dollar is the nesting of a repo dollar (`repository.reserve_repo_budget`), so the
repo rows describe it. Where a v7 `budget_ledger.reserved_usd` exceeds the sum of its repos'
(only reachable through the run-only `reserve_budget` primitive, which no orchestration path
calls), the excess simply stays held: the reaper's `MAX(reserved_usd - …, 0.0)` releases at most
what is attributed, so the residue is fail-closed, never an overspend.

The table's shape is read from `state/schema.sql` — the v8 baseline itself — so a migrated
database and a fresh one cannot drift (`_support.baseline_table_ddl`). The runner performs
`PRAGMA foreign_key_check` and sets `user_version` (§6).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Final

from fleet.migrations import _support
from fleet.state.db import SCHEMA_PATH

VERSION: Final = 8

#: The one table and the one index this rung owns. Read from the baseline, never restated.
TABLE: Final = "reservations"
INDEX: Final = "ix_reservations_expiry"

#: `reservation_id` prefix for an adopted pre-v8 aggregate. Deterministic — `(run_id, repo_id)` is
#: the `repo_ledger` primary key — so the back-fill is re-runnable and an operator grepping the
#: table can tell an adopted hold from one a worker actually minted.
LEGACY_ID_PREFIX: Final = "v008-legacy"

_ADOPT_REPO_AGGREGATES: Final = (
    # S608: `TABLE`/`LEGACY_ID_PREFIX` are module constants above, never input.
    f"INSERT INTO {TABLE} (reservation_id, run_id, repo_id, phase, lease_fence, "  # noqa: S608
    "                      amount_usd, state, expires_at, created_at) "
    f"SELECT '{LEGACY_ID_PREFIX}:' || run_id || ':' || repo_id, "
    "       run_id, repo_id, NULL, NULL, reserved_usd, 'HELD', reservation_expires_at, "
    "       updated_at "
    "  FROM repo_ledger WHERE reserved_usd > 0.0"
)


def upgrade(conn: sqlite3.Connection) -> None:
    _support.assert_foreign_keys_off(conn, context="v008_reservations")
    if _support.table_exists(conn, TABLE):  # pragma: no cover — the runner gates on user_version
        raise _support.SqlTextError(
            f"{TABLE} already exists at user_version 7; this rung creates it, and a database "
            "that already has it did not come from this ladder (§6)."
        )

    schema_sql = Path(SCHEMA_PATH).read_text(encoding="utf-8")
    conn.execute(_support.baseline_table_ddl(schema_sql)[TABLE])
    for name, ddl in _support.baseline_index_ddl(schema_sql):
        if name == INDEX:
            conn.execute(ddl)

    # The ONLY write to pre-existing data in this step, and it writes to the NEW table: every
    # ledger column keeps the value it had.
    conn.execute(_ADOPT_REPO_AGGREGATES)
