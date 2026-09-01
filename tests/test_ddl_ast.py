"""SPEC §12 item 48: "`fleet migrate-db` applies the pending `vNNN_*.py` steps under `BEGIN
EXCLUSIVE` and no other code path executes DDL (asserted by an AST test finding no
`CREATE`/`ALTER`/`DROP` outside `src/fleet/migrations/` and `state/schema.sql`)."

This is that AST test. It parses every `.py` file under `src/fleet/` with `ast`, walks every
string-literal node, and checks each `;`-separated SQL statement inside for an opener of
`CREATE`/`ALTER`/`DROP`. `state/schema.sql` (`src/fleet/state/schema.sql`, the fresh-database
baseline `initialize_database()` executes verbatim via `SCHEMA_PATH.read_text()`, per
`cli.py::_check_schema_version`'s sibling `state/db.py::initialize_database`) is not a `.py`
file, so the `*.py` glob excludes it structurally rather than through an exception list --
exactly as `src/fleet/` already holds one non-Python file per `tests/test_proc.py`'s own note.

Disclosed blind spot: this walks `ast.Constant` string-literal nodes, so it cannot recognize a DDL
keyword split across a string-concatenation boundary (e.g. `"ALTER " + "TABLE t"`, two separate
`ast.Constant` nodes joined at runtime, neither of which spells `ALTER TABLE` on its own) -- an
f-string's literal parts, by contrast, ARE covered, because `ast.JoinedStr`'s literal segments are
themselves `ast.Constant` nodes that `ast.walk` visits like any other.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src" / "fleet"
_MIGRATIONS = _SRC / "migrations"

#: The SQLite DDL shapes this test hunts for, anchored at the start of a `;`-separated
#: statement. Matched against the whole statement, not just its first token: a bare
#: `"CREATE"`/`"DROP"` opener is not enough by itself -- `src/fleet/vcs/github.py`'s
#: `["pr", "create", ...]` argv for `gh pr create` is a real, non-DDL string literal whose first
#: token uppercases to `CREATE`, and a first-token-only check reported it as a false positive
#: (measured: `src/fleet/vcs/github.py:231: 'create'`, the discovery that motivated requiring the
#: DDL object keyword too). Every DDL shape actually present in `src/fleet/migrations/` and
#: `state/schema.sql` was enumerated before choosing this pattern (`CREATE [UNIQUE] TABLE/INDEX
#: [IF NOT EXISTS]`, `ALTER TABLE`, `DROP TABLE/INDEX [IF EXISTS]`) -- VIEW/TRIGGER are included
#: because they are the same SQLite DDL family even though neither appears in the tree today.
_DDL_STATEMENT = re.compile(
    r"^(CREATE|ALTER|DROP)\s+(UNIQUE\s+)?(TABLE|INDEX|VIEW|TRIGGER)\b", re.IGNORECASE
)

#: SQLite's two comment forms, stripped before splitting on `;` so a `;` hidden behind a `--`
#: comment cannot hide a DDL statement from the split, and a comment merely mentioning a DDL
#: keyword cannot be mistaken for a statement opener. Mirrors
#: `tests/test_read_transaction_statements.py::_strip_sql_comments`'s reasoning (not imported
#: from it: that module's machinery is scoped to `BEGIN`-transaction recognition, and duplicating
#: these six lines keeps this file's own SQL model self-contained and independently readable).
_SQL_COMMENT = re.compile(r"--[^\n]*|/\*[\s\S]*?\*/")


def _statements(sql: str) -> list[str]:
    """`sql`, comments stripped, split into its `;`-separated statements, stripped, empties
    dropped. `executescript` executes exactly a `;`-separated script, so this is what the call's
    own semantics mean; a single `execute()` literal with no `;` yields one statement, itself."""
    stripped = _SQL_COMMENT.sub(" ", sql)
    return [part.strip() for part in stripped.split(";") if part.strip()]


def _docstring_node_ids(tree: ast.Module) -> frozenset[int]:
    """`id()` of every literal that is a docstring: the first statement of a module, class or
    function. A position in the tree, not a shape -- so prose *about* DDL (this file's own
    module docstring above, or `state/db.py`'s "`CREATE TABLE IF NOT EXISTS` never executes an
    `ALTER`") cannot trip the scan below merely by containing the words."""
    docs: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                docs.add(id(first.value))
    return frozenset(docs)


def _py_files() -> list[Path]:
    return sorted(_SRC.rglob("*.py"))


def _ddl_sites(*, include_migrations: bool) -> list[tuple[str, int, str]]:
    """`(relpath, line, statement)` for every DDL-opening SQL statement in a non-docstring string
    literal under `src/fleet/`. `include_migrations=False` is the criterion's actual scope
    (everything except `src/fleet/migrations/`); `include_migrations=True` is the positive
    control below, which needs the whole tree to prove the extractor can find anything at all.
    """
    found: list[tuple[str, int, str]] = []
    for path in _py_files():
        if not include_migrations and (path == _MIGRATIONS or _MIGRATIONS in path.parents):
            continue
        rel = path.relative_to(_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        docs = _docstring_node_ids(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in docs:
                continue
            for statement in _statements(node.value):
                if _DDL_STATEMENT.match(statement):
                    found.append((rel, node.lineno, statement))
    return sorted(found)


def test_the_ddl_extractor_finds_the_known_ddl_in_migrations() -> None:
    """Positive control, read before any verdict below (CLAUDE.md Guardrail 6): `migrations/`
    genuinely applies DDL (each `vNNN_*.py` step is a `CREATE`/`ALTER`/`DROP` under `BEGIN
    EXCLUSIVE`), so a scan of the WHOLE tree, migrations included, must find some. If this
    reports zero, the extractor -- not the tree -- is broken, and
    `test_no_ddl_outside_migrations` below would be passing vacuously rather than because the
    tree is actually clean.
    """
    found = _ddl_sites(include_migrations=True)
    assert found, (
        "the DDL extractor found zero CREATE/ALTER/DROP statements anywhere under src/fleet/, "
        "including src/fleet/migrations/ -- this means the extractor itself is broken (e.g. the "
        "`;`-split or the opener check silently stopped matching), not that migrations/ is "
        "DDL-free"
    )
    # `src/fleet/migrations/` is where every hit above must have come from, confirming the
    # exclusion below actually removes something rather than filtering a set that was already
    # migrations-only for an unrelated reason.
    assert all(rel.startswith("src/fleet/migrations/") for rel, _line, _stmt in found)


def test_no_ddl_outside_migrations() -> None:
    """SPEC §12 item 48: `fleet migrate-db` is the only code path that executes DDL; every other
    module reads `PRAGMA user_version` (`cli.py::_check_schema_version`) and refuses on a
    mismatch rather than applying it (§6). `state/schema.sql` legitimately contains DDL -- it is
    the fresh-database baseline `migrate-db` executes verbatim -- and is out of scope here
    because it is not a `.py` file, not through a name-based exception.
    """
    found = _ddl_sites(include_migrations=False)
    assert found == [], "DDL statement(s) found outside src/fleet/migrations/: " + "; ".join(
        f"{rel}:{line}: {stmt!r}" for rel, line, stmt in found
    )
