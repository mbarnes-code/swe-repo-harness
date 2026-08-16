"""3 → 4 — the stub lifecycle (ADR-0022, SPEC §6).

Every pre-existing stub row is an unresolved one, so `DEFAULT 'ACTIVE'` is the correct back-fill
and `resolved_at` stays NULL, satisfying `CHECK (state = 'ACTIVE' OR resolved_at IS NOT NULL)`.

The `consumer_repo_id` **two-step** is required, not stylistic: SQLite cannot add a `NOT NULL`
column whose default is another column, so the column is added with a placeholder default and
then `UPDATE`d from `repo_id` (§6). The rebuild that eventually drops the placeholder default and
installs `CHECK (consumer_repo_id = repo_id)` is the 6 → 7 one (package docstring) — the
`UPDATE` here is what makes that CHECK hold when it arrives, and running it in the same
transaction as the `ALTER` is what keeps the intermediate state invisible.

`stub_repo_id → provider_repo_id` is the ADR-0022 disambiguation: the old name did not say which
end of the stub it meant.
"""

from __future__ import annotations

import sqlite3
from typing import Final

VERSION: Final = 4

_STATEMENTS: Final[tuple[str, ...]] = (
    "ALTER TABLE stubs ADD COLUMN state TEXT NOT NULL DEFAULT 'ACTIVE'",
    "ALTER TABLE stubs ADD COLUMN stub_fidelity TEXT NOT NULL DEFAULT 'PUBLISHED_ARTIFACT'",
    "ALTER TABLE stubs ADD COLUMN revalidation_round INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE stubs ADD COLUMN revalidation_task_id TEXT",
    "ALTER TABLE stubs ADD COLUMN resolved_at TEXT",
    "ALTER TABLE stubs ADD COLUMN resolved_by_run_id TEXT",
    "ALTER TABLE stubs ADD COLUMN abandon_reason TEXT",
    # placeholder default, then the UPDATE below: SQLite cannot default a column to another column
    "ALTER TABLE stubs ADD COLUMN consumer_repo_id TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE stubs RENAME COLUMN stub_repo_id TO provider_repo_id",
    "ALTER TABLE tasks ADD COLUMN revalidation_key TEXT",
    "UPDATE stubs SET consumer_repo_id = repo_id",
)


def upgrade(conn: sqlite3.Connection) -> None:
    for statement in _STATEMENTS:
        conn.execute(statement)
