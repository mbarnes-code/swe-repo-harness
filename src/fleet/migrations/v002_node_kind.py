"""1 → 2 — the node-kind change (ADR-0019, SPEC §6).

Nodes become `(kind, id)` rather than always-a-repo: `edges.src_repo_id`/`dst_repo_id` and
`wave_members.repo_id` are renamed and given a sibling `*_kind` column, and `tasks` gains
`contract_id`. Every pre-existing row is a repo node, so `DEFAULT 'REPO'` back-fills the whole
table correctly and **no data is rewritten** (§6) — which is also what makes an in-flight run
resumable across this boundary once the step has run.

§6 adds "then DROP and re-CREATE the affected indexes and the tasks UNIQUE". `RENAME COLUMN`
already rewrites the definition of every index over the renamed column, so no index work is
needed here; the *constraint* half of that clause is folded into the 6 → 7 rebuild, which
re-creates these tables and the whole index set against the authoritative v7 baseline. See the
package docstring for why that consolidation is the honest reading of §6 rather than a shortcut.
"""

from __future__ import annotations

import sqlite3
from typing import Final

VERSION: Final = 2

_STATEMENTS: Final[tuple[str, ...]] = (
    "ALTER TABLE edges        RENAME COLUMN src_repo_id TO src_id",
    "ALTER TABLE edges        RENAME COLUMN dst_repo_id TO dst_id",
    "ALTER TABLE edges        ADD COLUMN src_kind TEXT NOT NULL DEFAULT 'REPO'",
    "ALTER TABLE edges        ADD COLUMN dst_kind TEXT NOT NULL DEFAULT 'REPO'",
    "ALTER TABLE edges        ADD COLUMN retargeted_from_repo_id TEXT",
    "ALTER TABLE wave_members RENAME COLUMN repo_id TO node_id",
    "ALTER TABLE wave_members ADD COLUMN node_kind TEXT NOT NULL DEFAULT 'REPO'",
    "ALTER TABLE tasks        ADD COLUMN contract_id TEXT",
)


def upgrade(conn: sqlite3.Connection) -> None:
    for statement in _STATEMENTS:
        conn.execute(statement)
