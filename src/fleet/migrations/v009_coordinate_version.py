"""8 → 9 — `coordinates.version`, the owning repo's own published version (SPEC §37 Blocker B).

**The defect this rung closes.** `ManifestAdapter.publishes(path)` in `manifests/{python,npm,
maven,cargo}.py` already computes a real concrete version for the coordinate a repo publishes —
`pyproject.toml`'s `[project].version`, `package.json`'s `version`, a POM's (possibly
parent-inherited) `<version>`, `Cargo.toml`'s `[package].version` — every scan, for 4 of the 5
ecosystems (Go module versions are VCS tags, not in-repo content, so `gomod.py` genuinely cannot
supply one). That value reached `Coordinate.version_spec` and was then discarded before the one
production write site for `coordinates` rows, which read only `.key`/`.ecosystem`/`.group`/
`.name`. This rung adds the column; `src/fleet/cli.py`'s write and read sites (same commit) stop
discarding the value.

**Nullable, no backfill.** Every pre-existing row gets NULL — there is no way to recover a past
scan's discarded value, and NULL is the correct, honest state for a coordinate this ladder never
saw computed. The next `fleet scan` of the owning repo repopulates it.

**Additive only.** One `ALTER TABLE ... ADD COLUMN`, so the CHECK/FK-free `coordinates` table
needs no rebuild.
"""

from __future__ import annotations

import sqlite3
from typing import Final

VERSION: Final = 9

_STATEMENTS: Final[tuple[str, ...]] = ("ALTER TABLE coordinates ADD COLUMN version TEXT",)


def upgrade(conn: sqlite3.Connection) -> None:
    for statement in _STATEMENTS:
        conn.execute(statement)
