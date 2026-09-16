"""11 → 12 — `budget_ledger.calls_made` / `budget_ledger.max_calls`, the §11.2 run ceiling that a
`price: free` target cannot render inert.

**The defect this rung closes.** `run_max_cost_usd` is enforced by a CAS predicate on dollars
(`spent_usd + reserved_usd + :amt <= max_usd`), and a target declaring `price: free` prices every
call at `$0.00` — deliberately, as a positive assertion an operator makes about a locally-served
target (`docs/SPEC.md` §9 rule 5). `estimate_cost`'s own docstring states the consequence plainly:
such a target "reserves and spends `0.0` with the ledger machinery fully live". Fully live and
structurally inert: `spent_usd` stays `0.00` for the life of the run, so the only ceiling that
halts a fleet never trips, and a looping dispatch pattern has nothing short of a human to stop it.
These two columns are the second, orthogonal ceiling — a cap on the NUMBER of LLM calls a run may
dispatch, which is unaffected by what they cost.

**Two columns, and why each is shaped the way it is.** `calls_made INTEGER NOT NULL DEFAULT 0` is
the counter the reservation CAS increments; `0` is the correct value for every pre-existing row
because a run's call count before this rung was never recorded anywhere and back-filling a guess
would make the ceiling refuse work on evidence nobody measured. `max_calls INTEGER` is nullable
and NULL means **no cap** — the table's existing nullable-means-unbounded convention
(`reservation_expires_at`: "NULL => never reaped") — so every database this rung lifts keeps
exactly its pre-v12 behaviour until an operator sets the ceiling explicitly.

**No reserve/settle pair, deliberately.** The dollar ceiling needs two phases because the amount
is a p95 ESTIMATE before the call and is RECONCILED against the real token count after it. A call
count has no such gap: one dispatch is exactly 1, known atomically in advance. So the increment
and its predicate ride inside the *existing* reservation CAS (`state/repository.py`
`_reserve_run_sql`) and there is no settlement step, no expiry and nothing for the reaper to
release — `calls_made` only ever increments, because there is no "un-call".

**Guarded, not bare `ADD COLUMN` — the same two-real-paths problem 10 → 11 documents.**
`budget_ledger` is on 6 → 7's `_REBUILD` list (`v007_logical_keys._REBUILD`, entry "the two
fail-closed CHECKs"), and `rebuild_table()` always sources its target shape from the *live*
`state/schema.sql` (`_support`'s `index_target_table` docstring: "a ladder that only works from
the newest baseline is not a ladder"). Now that these columns exist in the baseline, any ladder
run replaying 6 → 7 against today's code has them present five rungs early, at v7 — `calls_made`
at its declared `DEFAULT 0` and `max_calls` NULL — and a bare `ADD COLUMN` there raises "duplicate
column name". A database that was ALREADY at v11 before this rung shipped never took that path:
its historical 6 → 7 rebuild ran against the `schema.sql` of that day, so for it the columns are
genuinely absent and the `ADD COLUMN` is exactly right. Both paths are real, so the guard checks
rather than assumes.

**What the `ADD COLUMN` path cannot carry, stated rather than hidden.** The baseline declares
`CHECK (calls_made >= 0)` as a table constraint, and SQLite's `ALTER TABLE … ADD COLUMN` cannot
add one. A database lifted by this rung therefore has the two columns and not that CHECK, while a
fresh or fully-replayed one has both. The divergence is harmless here and is not worth a table
rebuild: nothing in the harness ever decrements `calls_made` (the CAS only ever writes
`calls_made + 1`), so the CHECK guards a state no code path can reach. It is declared in the
baseline because a constraint that documents an invariant is worth having where it is free, not
because the invariant is in doubt. `PRAGMA table_info` — what the migration tests compare — is
identical either way; only `sqlite_master`'s DDL text differs.
"""

from __future__ import annotations

import sqlite3
from typing import Final

from fleet.migrations import _support

VERSION: Final = 12

_TABLE: Final = "budget_ledger"

#: Column name -> the type/constraint text `ADD COLUMN` appends. `calls_made` carries a DEFAULT
#: because SQLite refuses to add a `NOT NULL` column without one; `max_calls` deliberately has
#: none, so NULL ("no cap") is what every lifted row gets.
_COLUMNS: Final[tuple[tuple[str, str], ...]] = (
    ("calls_made", "INTEGER NOT NULL DEFAULT 0"),
    ("max_calls", "INTEGER"),
)


def upgrade(conn: sqlite3.Connection) -> None:
    present = _support.column_names(conn, _TABLE)
    for column, declaration in _COLUMNS:
        if column not in present:
            conn.execute(f"ALTER TABLE {_TABLE} ADD COLUMN {column} {declaration}")
