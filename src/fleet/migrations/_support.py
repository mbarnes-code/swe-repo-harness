"""Deterministic helpers shared by the `vNNN_*.py` ladder steps (SPEC §6).

Nothing in here decides *what* a migration does — the steps do, from §6's per-version notes. This
module owns the three mechanical things every step would otherwise re-implement badly:

1. **`sha256_nul()`** — the one hash shape §6 names ("sha256 over the UNIQUE tuple, NUL-joined").
   Registered onto the migration connection as a deterministic SQL function so a back-fill is one
   `UPDATE`, not a Python read-modify-write loop over 10⁵ rows inside the exclusive transaction.
   `edges.edge_key` is **not** one of its callers: that key has exactly one definition, §5's
   `edge_key_for()`, and it is registered here as `fleet_edge_key` so the back-fill *calls* the
   recipe instead of restating it in SQL. A restated recipe is how §5 and §6 came to disagree.

2. **`rebuild_table()`** — SQLite's 12-step table rebuild (`CREATE … _new`, `INSERT … SELECT`,
   `DROP`, `RENAME`), because SQLite cannot `ALTER` a CHECK, a UNIQUE, a PRIMARY KEY, or a foreign
   key. It copies the **intersection** of the old and new column lists, so a column the new shape
   adds takes its declared DEFAULT and every pre-existing value is carried across verbatim. The
   caller may override any single column's source expression (`column_exprs`) — that is how §6's
   "`scc_id` is re-derived, not cast" is expressed without a second pass over the table.
   `PRAGMA foreign_keys` MUST already be OFF (§6): on `ALTER TABLE … RENAME` SQLite silently
   rewrites *other* tables' `REFERENCES` clauses to point at the new name, which would leave every
   child pointing at the scratch table. `assert_foreign_keys_off()` refuses to proceed otherwise.

3. **`baseline_table_ddl()` / `baseline_index_ddl()`** — the v7 target shapes, read from
   `state/schema.sql` itself. The 6 → 7 step rebuilds eleven tables; hand-copying their DDL into a
   migration module would create a second source of truth that silently drifts from the baseline
   the fresh-database path installs. §6 makes `schema.sql` the v7 baseline, so the ladder's
   destination is read from it rather than restated. A test asserts migrated-at-7 and fresh-at-7
   agree column for column.

`sqlite3` is used directly and synchronously here and in every step — see the package docstring.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Iterator, Mapping
from typing import Final

from fleet.models.graph import EDGE_KEY_COLUMNS, edge_key_from_row

__all__ = [
    "EDGE_KEY_COLUMNS",
    "NUL",
    "assert_foreign_keys_off",
    "baseline_index_ddl",
    "baseline_table_ddl",
    "column_names",
    "index_target_table",
    "rebuild_table",
    "register_sql_functions",
    "sha256_nul",
    "split_statements",
    "strip_sql_comments",
    "table_exists",
]

#: §6: the logical keys are "sha256 over the UNIQUE tuple, NUL-joined".
NUL: Final = "\x00"

_CREATE_TABLE_RE: Final = re.compile(
    r"^CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE
)
_CREATE_INDEX_RE: Final = re.compile(
    r"^CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)
_INDEX_TARGET_RE: Final = re.compile(
    r"\sON\s+\"?([A-Za-z_][A-Za-z0-9_]*)\"?\s*\(", re.IGNORECASE
)


class SqlTextError(RuntimeError):
    """`schema.sql` did not contain a statement a step asked for. Never silently skipped."""


# --------------------------------------------------------------------------------------
# hashing — the back-fill primitives §6 names
# --------------------------------------------------------------------------------------


def sha256_nul(*parts: object) -> str:
    """sha256 of the NUL-joined `parts`, hex. `None` joins as the empty string.

    This is the shape §6 specifies for `edges.edge_key` and it is a pure function of columns the
    row already carries, which is what makes the back-fill deterministic and re-runnable.
    """
    joined = NUL.join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _normalized_command_sha256(command: object) -> str:
    """sha256 of a normalized argv (`attempts.command_sha256`, §6 6 → 7).

    §6 says "sha256 of normalized command" without defining *normalized*. Normalization here is
    "the argv JSON with insignificant whitespace removed", which is the weakest assumption that
    still satisfies the column's stated purpose — "a whitespace change in the rendering must not
    mint a duplicate row" (`schema.sql`). It is applied uniformly to every pre-7 row, so the
    column is internally consistent whatever the runtime writer later chooses.
    """
    text = "" if command is None else str(command)
    return hashlib.sha256(re.sub(r"\s+", "", text).encode("utf-8")).hexdigest()


def _edge_key(*values: object) -> str:
    """`fleet_edge_key(<EDGE_KEY_COLUMNS>)` — §5's `edge_key_for`, reached from SQL.

    Deliberately a one-line delegation: the migration must produce the SAME key the model and the
    §6 UNIQUE tuple produce, and the only way to guarantee that is to call the one function that
    defines it. Note the absence of `run_id` — see `EDGE_KEY_COLUMNS`.
    """
    return edge_key_from_row(values)


def register_sql_functions(conn: sqlite3.Connection) -> None:
    """Attach the back-fill helpers as deterministic SQL functions for this connection."""
    conn.create_function("fleet_sha256_nul", -1, sha256_nul, deterministic=True)
    conn.create_function("fleet_command_sha256", 1, _normalized_command_sha256, deterministic=True)
    conn.create_function(
        "fleet_edge_key", len(EDGE_KEY_COLUMNS), _edge_key, deterministic=True
    )


# --------------------------------------------------------------------------------------
# introspection
# --------------------------------------------------------------------------------------


def column_names(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
    """Declared column names of `table`, in declaration order."""
    rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    return tuple(str(row[1]) for row in rows)


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def assert_foreign_keys_off(conn: sqlite3.Connection, *, context: str) -> None:
    """§6: `PRAGMA foreign_keys` MUST be OFF for the whole of every table-rebuild path.

    Fails loud rather than rebuilding under enforcement, because the damage is silent: SQLite
    rewrites child `REFERENCES` clauses on `RENAME` and the database ends up structurally valid
    and semantically wrong. The PRAGMA is a no-op inside a transaction, so it can only be set by
    the runner before `BEGIN EXCLUSIVE` — this assertion is how a step proves that happened.
    """
    row = conn.execute("PRAGMA foreign_keys").fetchone()
    if row is not None and int(row[0]) != 0:
        raise SqlTextError(
            f"{context}: PRAGMA foreign_keys is ON; the table-rebuild path requires it OFF for "
            "its whole duration (SPEC §6). It cannot be changed inside a transaction — the "
            "migration runner must set it before BEGIN EXCLUSIVE."
        )


# --------------------------------------------------------------------------------------
# schema.sql — the v7 baseline, parsed rather than restated
# --------------------------------------------------------------------------------------


def strip_sql_comments(sql: str) -> str:
    """Remove `--` comments, respecting single-quoted string literals.

    `schema.sql` documents itself with SQL *inside* comments — the reaper `UPDATE`, the claim CAS
    — and those comments contain semicolons, so statements cannot be split before comments are
    stripped.
    """
    out: list[str] = []
    in_string = False
    i = 0
    while i < len(sql):
        char = sql[i]
        if in_string:
            out.append(char)
            if char == "'":
                in_string = False
            i += 1
            continue
        if char == "'":
            in_string = True
            out.append(char)
            i += 1
            continue
        if char == "-" and sql.startswith("--", i):
            newline = sql.find("\n", i)
            if newline == -1:
                break
            i = newline
            continue
        out.append(char)
        i += 1
    return "".join(out)


def split_statements(sql: str) -> Iterator[str]:
    """Yield comment-free, semicolon-separated statements with their whitespace collapsed."""
    current: list[str] = []
    in_string = False
    for char in strip_sql_comments(sql):
        if char == "'":
            in_string = not in_string
        if char == ";" and not in_string:
            statement = " ".join("".join(current).split())
            if statement:
                yield statement
            current = []
            continue
        current.append(char)
    tail = " ".join("".join(current).split())
    if tail:
        yield tail


def baseline_table_ddl(schema_sql: str) -> dict[str, str]:
    """`{table_name: CREATE TABLE …}` for every table in the v7 baseline."""
    ddl: dict[str, str] = {}
    for statement in split_statements(schema_sql):
        match = _CREATE_TABLE_RE.match(statement)
        if match is not None:
            ddl[match.group(1)] = statement
    return ddl


def index_target_table(ddl: str) -> str:
    """The table a `CREATE INDEX` statement indexes.

    A step that re-creates "the whole baseline index set" is re-creating the set the *current*
    `schema.sql` declares, which includes indexes on tables a LATER rung introduces. Without this,
    adding a table at v8 breaks the v6 → 7 rung — it would try to index a table that does not
    exist yet — and a ladder that only works from the newest baseline is not a ladder.
    """
    match = _INDEX_TARGET_RE.search(ddl)
    if match is None:  # pragma: no cover — guarded by baseline_index_ddl's own regex
        raise SqlTextError(f"cannot find the indexed table in: {ddl[:80]!r}")
    return match.group(1)


def baseline_index_ddl(schema_sql: str) -> tuple[tuple[str, str], ...]:
    """`((index_name, CREATE INDEX …), …)` for every index in the baseline, in file order."""
    found: list[tuple[str, str]] = []
    for statement in split_statements(schema_sql):
        match = _CREATE_INDEX_RE.match(statement)
        if match is not None:
            found.append((match.group(1), statement))
    return tuple(found)


# --------------------------------------------------------------------------------------
# the 12-step rebuild
# --------------------------------------------------------------------------------------


def rebuild_table(
    conn: sqlite3.Connection,
    table: str,
    *,
    target_ddl: str,
    column_exprs: Mapping[str, str] | None = None,
) -> None:
    """Rebuild `table` into the shape `target_ddl` declares, carrying every row across.

    The copied column list is `new_columns ∩ old_columns`, plus any key of `column_exprs`: a
    column the new shape introduces is left to its declared DEFAULT, and a column the new shape
    drops (`phases.owner_pid` at 6 → 7) is simply not selected. `column_exprs` maps a *new* column
    name to the SQL expression that produces it from the *old* row.

    Every CHECK in `target_ddl` is enforced by the `INSERT … SELECT`, so a row the new shape
    considers illegal aborts the migration (and, with it, the whole transaction) instead of
    landing in a database whose constraints no longer describe its contents.
    """
    assert_foreign_keys_off(conn, context=f"rebuild of {table!r}")
    exprs = dict(column_exprs or {})
    scratch = f"{table}__fleet_rebuild"
    renamed = _CREATE_TABLE_RE.sub(f'CREATE TABLE "{scratch}"', target_ddl, count=1)
    if renamed == target_ddl:  # pragma: no cover — guarded by baseline_table_ddl's own regex
        raise SqlTextError(
            f"DDL for {table!r} is not a CREATE TABLE statement: {target_ddl[:80]!r}"
        )
    conn.execute(renamed)

    old_columns = set(column_names(conn, table))
    new_columns = column_names(conn, scratch)
    copied = [c for c in new_columns if c in old_columns or c in exprs]
    targets = ", ".join(f'"{c}"' for c in copied)
    sources = ", ".join(exprs.get(c, f'"{c}"') for c in copied)
    # S608: every identifier here comes from PRAGMA table_info or the baseline DDL, never from
    # user input, and the whole statement runs inside the migration's exclusive transaction.
    copy_sql = f'INSERT INTO "{scratch}" ({targets}) SELECT {sources} FROM "{table}"'  # noqa: S608
    conn.execute(copy_sql)
    conn.execute(f'DROP TABLE "{table}"')
    conn.execute(f'ALTER TABLE "{scratch}" RENAME TO "{table}"')
