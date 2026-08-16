"""2 → 3 — the anti-anchoring columns (ADR-0021, SPEC §6).

`attempts` and `llm_cache` learn the context policy and the approach signature, and
`rejected_approaches` — the ladder's memory — is created. §6: pre-existing `attempts` rows are
`DETERMINISTIC`-or-unpolicied and carry no signature, "which is exactly what the two defaults
express, so the CHECKs hold on back-filled rows without a rewrite". The `llm_cache` default is
`sha256(b'')`, so every entry written before ADR-0021 keys identically to a fresh-slate call and
cross-run cache reuse survives the boundary.

`rejected_approaches` is created here **with the DEFAULT ladder length baked into its CHECK**
(`attempt BETWEEN 1 AND 3`). That is deliberate and is what §6's 6 → 7 note describes as the thing
"a database migrated to 7 must lose too, or it keeps rejecting the rung-4 refutations the v7
baseline accepts" — the 6 → 7 rebuild is what removes it. Recording history accurately here is
what makes that later rebuild a real fix rather than a no-op.

The trailing "DROP and re-CREATE the `attempts` UNIQUE to add `approach_signature`" is folded into
the 6 → 7 rebuild (package docstring): the v7 baseline declares that uniqueness as a table-level
constraint, which SQLite can only install by rebuilding the table.
"""

from __future__ import annotations

import sqlite3
from typing import Final

VERSION: Final = 3

#: sha256(b'') — §6 spells the literal out, so it is spelled out here rather than computed.
_SHA256_OF_EMPTY: Final = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

_REJECTED_APPROACHES_DDL: Final = """
CREATE TABLE IF NOT EXISTS rejected_approaches (
    run_id        TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    task_id       TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    approach_signature TEXT NOT NULL CHECK (length(approach_signature) = 64),
    reason        TEXT NOT NULL,
    failure_class TEXT NOT NULL,
    -- ADR-0021 shipped the DEFAULT ladder length as a type constraint. The 6 -> 7 step removes it.
    attempt       INTEGER NOT NULL CHECK (attempt BETWEEN 1 AND 3),
    tier          TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    CHECK (length(reason) BETWEEN 1 AND 280),
    PRIMARY KEY (task_id, approach_signature)
)
"""

_STATEMENTS: Final[tuple[str, ...]] = (
    "ALTER TABLE attempts  ADD COLUMN context_policy TEXT",
    "ALTER TABLE attempts  ADD COLUMN approach_signature TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE llm_cache ADD COLUMN context_policy TEXT",
    "ALTER TABLE llm_cache ADD COLUMN rejected_approach_digest TEXT NOT NULL "
    f"DEFAULT '{_SHA256_OF_EMPTY}'",
    _REJECTED_APPROACHES_DDL,
)


def upgrade(conn: sqlite3.Connection) -> None:
    for statement in _STATEMENTS:
        conn.execute(statement)
