"""10 → 11 — `repos.migrated_test_count`, the durable half of §12.11's `(repo, baseline,
migrated)` report (round VI task 47, closing `docs/CRITERIA_PLAN.md` §11's gap 2).

**The defect this rung closes.** Round VI task 38 wired a real `bazel query 'tests(//dest/...)'`
count into `BuildverifyOutput.migrated_test_count` and `test_count_regressed`, and refuses on a
real regression — but that count lived only in the in-memory `WorkerOutput` for the life of one
worker run. `repos.baseline_test_count` (added at 6 → 7) already has a durable column; the
`migrated` half of the triple SPEC's own sentence names had none, so the count on regression
reached only a stderr message, never a queryable table. This rung adds the column; `src/fleet/
cli.py`'s `_BuildSink` (same commit) stops discarding the value.

**Nullable, no backfill — the same distinction `baseline_test_count`/`baseline_ok` already draw
(6 → 7's own note).** `NULL` means "never measured": every pre-existing row gets NULL, and so does
every repo whose `bazel query` step never ran (no `baseline_ok IS True` native baseline to compare
against). `0` is a real, meaningful "the migrated package has zero test targets" measurement and
must never be confused with "not measured" — the reason this is NOT `NOT NULL DEFAULT 0` the way
`baseline_test_count` is: that column's `0` already means "unknown" by convention (its own comment
in `state/schema.sql`), a convention this column deliberately does NOT repeat, because `repos`
already draws the correct tri-state distinction one column over on `baseline_ok`.

**Guarded, not a bare `ADD COLUMN` — the one way this rung genuinely differs from 9 → 10's
`coordinates.version` template.** `repos` is on 6 → 7's `_REBUILD` list (`v007_logical_keys.
_REBUILD`), and `rebuild_table()` always sources its target shape from the *live* `state/
schema.sql` (`_support.baseline_table_ddl`, by design — see that module's `index_target_table`
docstring: "a ladder that only works from the newest baseline is not a ladder"). Once this column
exists in `schema.sql`, any ladder run that replays 6 → 7 against today's code — every test
fixture that migrates a v6 database fresh, and any real database that ever gets migrated from
before v7 — has `repos.migrated_test_count` already present the moment 6 → 7's rebuild finishes,
NULL by its declared absence of a DEFAULT, four rungs before this one runs. A bare `ADD COLUMN`
there raises "duplicate column name". A database that was ALREADY at v10 before this rung shipped
never took that path — its historical 6 → 7 rebuild ran against the schema.sql of that day, which
had no such column — so for it the column is genuinely absent and the `ADD COLUMN` is exactly
right. Both are legitimate, so the guard checks rather than assumes.
"""

from __future__ import annotations

import sqlite3
from typing import Final

from fleet.migrations import _support

VERSION: Final = 11

_TABLE: Final = "repos"
_COLUMN: Final = "migrated_test_count"


def upgrade(conn: sqlite3.Connection) -> None:
    if _COLUMN not in _support.column_names(conn, _TABLE):
        conn.execute(f"ALTER TABLE {_TABLE} ADD COLUMN {_COLUMN} INTEGER")
