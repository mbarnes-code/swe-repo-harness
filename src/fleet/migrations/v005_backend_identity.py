"""4 → 5 — backend identity in the cache (ADR-0023, SPEC §6).

This migration is **columns only, and it deliberately does not re-key anything**. Every row
written before ADR-0023 was produced by the sole `anthropic` backend, so the defaults are the
truth for those rows — but their `cache_key` was computed without `tier|backend|model_id`, so a
post-migration lookup will not *hit* them and they simply age out. §6 is explicit that this is the
correct outcome and not a bug to optimise away: re-deriving the old keys would require asserting
which model answered, which is exactly the assertion ADR-0023 exists to stop the harness from
making implicitly.

`attempts.llm_backend` is nullable because a `DETERMINISTIC` row had no backend at all, and
`llm_failovers` counts backend hops *inside* one attempt — §11.8: it is not an attempt, and it
never increments `phases.attempts`.
"""

from __future__ import annotations

import sqlite3
from typing import Final

VERSION: Final = 5

_STATEMENTS: Final[tuple[str, ...]] = (
    "ALTER TABLE llm_cache ADD COLUMN tier TEXT NOT NULL DEFAULT 'WORKHORSE'",
    "ALTER TABLE llm_cache ADD COLUMN backend TEXT NOT NULL DEFAULT 'anthropic'",
    "ALTER TABLE llm_cache ADD COLUMN structured_output_mode TEXT NOT NULL DEFAULT 'JSON_SCHEMA'",
    "ALTER TABLE attempts  ADD COLUMN llm_backend TEXT",  # NULL for DETERMINISTIC rows
    "ALTER TABLE attempts  ADD COLUMN llm_failovers INTEGER NOT NULL DEFAULT 0",
)


def upgrade(conn: sqlite3.Connection) -> None:
    for statement in _STATEMENTS:
        conn.execute(statement)
