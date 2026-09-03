"""9 → 10 — `file_blobs`: the `ls-tree` path/blob-SHA listing preflight captures (D114 (a)/(b)).

**The defect this rung closes.** §3.1 step 1 ("preflight") has always cut a real worktree at
`head_sha`, but nothing in `src/` ever ran `git ls-tree` against it and persisted the result —
three independent modules' docstrings (`workers/contracts.py`, `graph/sequence.py`,
`graph/collisions.py`) describe the same not-yet-built capture mechanism, and each already carries
consumer code written against it (`ContractsInput.blob_shas`, `check_criterion_d`'s injected
`evidence_exists` callable, `collisions.CollisionInput.files`). Without the listing,
`check_criterion_d` — §12 acceptance criterion 9's `(d)` sub-check, "no edge exists whose
`evidence_path` does not resolve to a real file at `head_sha`" — was wired to an always-`True`
default and never actually checked a real run (round-VI research-28, D114).

**Additive only. No table is rebuilt.** One `CREATE TABLE`, no data migration: there is nothing to
back-fill, since no prior version ever captured this listing. The next `fleet scan` populates it
for every repo with a non-NULL `head_sha`.

The table's shape is read from `state/schema.sql` — the v10 baseline itself — so a migrated
database and a fresh one cannot drift (`_support.baseline_table_ddl`).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Final

from fleet.migrations import _support
from fleet.state.db import SCHEMA_PATH

VERSION: Final = 10

#: The one table this rung owns.
TABLE: Final = "file_blobs"


def upgrade(conn: sqlite3.Connection) -> None:
    _support.assert_foreign_keys_off(conn, context="v010_file_blobs")
    if _support.table_exists(conn, TABLE):  # pragma: no cover — the runner gates on user_version
        raise _support.SqlTextError(
            f"{TABLE} already exists at user_version 9; this rung creates it, and a database "
            "that already has it did not come from this ladder (§6)."
        )

    schema_sql = Path(SCHEMA_PATH).read_text(encoding="utf-8")
    conn.execute(_support.baseline_table_ddl(schema_sql)[TABLE])
