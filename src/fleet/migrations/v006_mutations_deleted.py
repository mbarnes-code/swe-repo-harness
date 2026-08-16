"""5 → 6 — the mutations journal is deleted (ADR-0024, SPEC §6).

This migration **deletes state rather than reshaping it**, which is the point: every column it
drops was either Git's own (`pre_tree_sha`, `post_tree_sha`, `patch_path`, `patch_sha256`) or a
duplicate of orchestration state already carried by `tasks`/`attempts`/`phases`/`events`. Nothing
is lost that Git cannot answer — a pre-migration run's applied patches are exactly the commits on
its `migrate/<repo>` branches, and **the branches are untouched by this migration**.

What survives of the journal is three columns on `attempts` (two pointers and a flag) and
`phases.base_ref`, the rollback anchor. The `base_ref` back-fill only touches rows that already
recorded a pre-mutation tip, so an upgraded database has a Git-resolvable anchor rather than a
NULL one; the ref itself is (re)created by `fleet resume` step 4 from this value.

Pre-ADR-0024 commits carry no `Fleet-*` trailers, so their `Fleet-Patch-Id` guard always misses —
a **cost, not a correctness break**: the `git apply --check --reverse` backstop still refuses an
already-applied patch, so the worst case is one re-checked patch, not a duplicated commit.

§6's trailing "table-rebuild path for `attempts` (the three new CHECKs cannot be added by ALTER)"
is folded into the 6 → 7 rebuild, which installs those same three CHECKs from the v7 baseline
(package docstring).
"""

from __future__ import annotations

import sqlite3
from typing import Final

VERSION: Final = 6

_STATEMENTS: Final[tuple[str, ...]] = (
    "ALTER TABLE attempts ADD COLUMN patch_id        TEXT",
    "ALTER TABLE attempts ADD COLUMN commit_sha      TEXT",
    "ALTER TABLE attempts ADD COLUMN already_applied INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE phases   ADD COLUMN base_ref        TEXT",
    "UPDATE phases SET base_ref = 'refs/fleet/' || run_id || '/' || repo_id "
    "|| '/phase-' || phase || '/base' WHERE pre_commit_sha IS NOT NULL",
    "DROP INDEX IF EXISTS ix_mutations_open",
    "DROP INDEX IF EXISTS ux_mutations_patch",
    "DROP TABLE IF EXISTS mutations",
)


def upgrade(conn: sqlite3.Connection) -> None:
    for statement in _STATEMENTS:
        conn.execute(statement)
