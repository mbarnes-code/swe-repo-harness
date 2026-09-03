"""Constraint tests for `src/fleet/state/schema.sql` — the v7 fresh-database baseline (SPEC §6).

These are not "does the DDL parse" tests. Each one pins a constraint that a review found to be
load-bearing, and the docstring says which failure the constraint prevents. Plain stdlib
`sqlite3` is used deliberately: the DDL is applied once by `fleet migrate-db`, synchronously,
so nothing here needs `aiosqlite`.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fleet.models.enums import Ecosystem, EdgeKind, NodeKind, Phase, RepoStatus
from fleet.models.graph import (
    EDGE_KEY_COLUMNS,
    DependencyEdge,
    edge_key_for,
    edge_key_from_row,
)
from fleet.models.repo import Coordinate
from fleet.models.state import SCHEMA_VERSION, PhaseRecord
from fleet.models.tasks import MAX_ATTEMPTS

FROZEN = datetime(2026, 8, 9, 0, 0, 0, tzinfo=UTC)
# The §6 `phases.lease_owner` format: '{host}:{container_id}:{pid}:{boot_uuid}'.
LEASE_OWNER = "worker-3:9f2c1ab4de77:42:0d9f6c1e-3b6a-4a0e-9c11-5f2b7d8e4a10"

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "src" / "fleet" / "state" / "schema.sql"
SCHEMA_SQL = SCHEMA_PATH.read_text(encoding="utf-8")

# Hard-coded so that silently DROPping a table or an index fails here rather than at 3 a.m. on a
# 250-repo run. Derived counts alone cannot catch a deletion: they move with the file.
EXPECTED_TABLE_COUNT = 23  # D114 (a): `file_blobs` (round VI task 42)
EXPECTED_INDEX_COUNT = 34

# The PRAGMAs that reset on every new handle. schema.sql runs ONCE, so setting them there would
# configure exactly one connection; `state/db.py` must issue them on every connection it opens.
PER_CONNECTION_PRAGMAS = ("foreign_keys", "busy_timeout", "synchronous", "wal_autocheckpoint")

_TABLE_RE = re.compile(r"^CREATE TABLE IF NOT EXISTS (\w+)", re.MULTILINE)
_INDEX_RE = re.compile(r"^CREATE (?:UNIQUE )?INDEX IF NOT EXISTS (\w+)", re.MULTILINE)


def _fresh_db(tmp_path: Path, *, apply_twice: bool = False) -> sqlite3.Connection:
    """A real on-disk database: WAL needs a file, and `:memory:` would not exercise it."""
    conn = sqlite3.connect(tmp_path / "fleet.db")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_SQL)
    if apply_twice:
        conn.executescript(SCHEMA_SQL)
    conn.execute("PRAGMA foreign_keys = ON")  # executescript's implicit COMMIT does not clear it,
    return conn                               # but re-asserting keeps the fixture honest


def _declared_domain(table: str, column: str) -> set[str]:
    """The literal set inside `CHECK (<column> IN (...))` for one table, read out of the DDL text.
    Scoped to the table's own CREATE statement because several tables carry a `status` CHECK."""
    block = re.search(
        rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n\);", SCHEMA_SQL, re.DOTALL
    )
    assert block is not None, f"no CREATE TABLE for {table} in schema.sql"
    clause = re.search(rf"CHECK \({column} IN \((.*?)\)\)", block.group(1), re.DOTALL)
    assert clause is not None, f"{table}.{column} has no CHECK domain"
    return set(re.findall(r"'([^']*)'", clause.group(1)))


def _objects(conn: sqlite3.Connection, kind: str) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = ? AND name NOT LIKE 'sqlite_%'", (kind,)
    )
    return {row[0] for row in rows}


def _run(conn: sqlite3.Connection, run_id: str = "run-1") -> str:
    conn.execute(
        "INSERT INTO runs (run_id, started_at, config_sha256, harness_version) "
        "VALUES (?, '2026-08-09T00:00:00Z', 'cfg', '0.1.0')",
        (run_id,),
    )
    return run_id


def _unique_tuples(conn: sqlite3.Connection, table: str) -> tuple[tuple[str, ...], ...]:
    """Every UNIQUE constraint of `table` as its column tuple, in declared order.

    Read out of the live database rather than regexed out of the file: it is the shipped DDL that
    was applied, and SQLite reports what the constraint actually is rather than what its text
    looks like.
    """
    found: list[tuple[str, ...]] = []
    for index in conn.execute(f'PRAGMA index_list("{table}")').fetchall():
        if int(index[2]):  # unique
            info = conn.execute(f'PRAGMA index_info("{index[1]}")').fetchall()
            found.append(tuple(str(column[2]) for column in info))
    return tuple(found)


def _repo(conn: sqlite3.Connection, repo_id: str = "repo-1") -> str:
    conn.execute(
        "INSERT INTO repos (repo_id, name, url, updated_at) "
        "VALUES (?, ?, 'git@host:x.git', '2026-08-09T00:00:00Z')",
        (repo_id, repo_id),
    )
    return repo_id


def _insert_phase(conn: sqlite3.Connection, **overrides: object) -> None:
    row: dict[str, object] = {
        "run_id": "run-1",
        "repo_id": "repo-1",
        "phase": 2,
        "updated_at": "2026-08-09T00:00:00Z",
    }
    row.update(overrides)
    cols = ", ".join(row)
    marks = ", ".join("?" * len(row))
    conn.execute(f"INSERT INTO phases ({cols}) VALUES ({marks})", tuple(row.values()))  # noqa: S608


def _insert_event(conn: sqlite3.Connection, run_id: str, seq: int, uid: str) -> None:
    conn.execute(
        "INSERT INTO events (run_id, seq, ts, level, event, event_uid) "
        "VALUES (?, ?, '2026-08-09T00:00:00Z', 'info', 'PhaseStarted', ?)",
        (run_id, seq, uid),
    )


def test_applies_to_a_fresh_database_and_reapplying_is_safe(tmp_path: Path) -> None:
    """Why: `fleet migrate-db` may be re-run after a crash mid-command, and an operator will run
    it twice to check. Every statement is `IF NOT EXISTS`, so the second application must be a
    no-op rather than an OperationalError that leaves the database half-built."""
    conn = _fresh_db(tmp_path, apply_twice=True)
    assert _objects(conn, "table")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_persistent_pragmas_land_in_the_file_header(tmp_path: Path) -> None:
    """Why: `user_version` and `journal_mode` are stored in the database header and survive the
    connection that set them, so they belong in a file that runs once. Workers refuse to start
    when `user_version` disagrees with the compiled-in version (§6), and WAL is what lets readers
    run concurrently with the single writer (§11.5) — a database left in `delete` mode serializes
    the whole fleet behind one lock."""
    conn = _fresh_db(tmp_path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_per_connection_pragmas_are_documented_and_not_set_in_the_file() -> None:
    """Why: `foreign_keys` is OFF by default in SQLite and resets on every handle. Setting it in
    a file that runs once would configure the migration's connection alone and leave every
    CASCADE in this schema inert for every worker — the exact failure that orphaned `events`.
    The file must therefore NAME these for `state/db.py` without pretending to set them."""
    header = SCHEMA_SQL.split("PRAGMA journal_mode", 1)[0]
    for pragma in PER_CONNECTION_PRAGMAS:
        assert pragma in header, f"{pragma} must be named in the header for state/db.py"
    executable = re.findall(r"^PRAGMA (\w+)", SCHEMA_SQL, re.MULTILINE)
    assert executable == ["journal_mode", "user_version"]


def test_every_declared_table_and_index_exists(tmp_path: Path) -> None:
    """Why: §6's index set is the difference between an indexed join and an N-squared scan at 250
    repos, and a table quietly lost to a bad merge would only surface as a runtime OperationalError
    deep inside a phase. The hard-coded counts catch a deletion that the derived sets cannot."""
    conn = _fresh_db(tmp_path)
    declared_tables = set(_TABLE_RE.findall(SCHEMA_SQL))
    declared_indexes = set(_INDEX_RE.findall(SCHEMA_SQL))
    assert _objects(conn, "table") == declared_tables
    assert _objects(conn, "index") == declared_indexes
    assert len(declared_tables) == EXPECTED_TABLE_COUNT
    assert len(declared_indexes) == EXPECTED_INDEX_COUNT
    # The lease reaper's partial index and the events ordering index are named explicitly: both
    # were added by review and both are the sole scan of a hot 30 s loop / every JSONL consumer.
    assert {"ix_phases_lease", "ix_events_seq", "ix_tasks_claimable"} <= declared_indexes


def test_phases_attempts_accepts_a_fourth_attempt(tmp_path: Path) -> None:
    """Why: the old `CHECK (attempts BETWEEN 0 AND 3)` turned the ordinary exhaustion path into an
    IntegrityError. The write that RECORDS the terminal failure is the same write that increments
    past the ladder, so it was rejected — the failure was never recorded, and the repo was neither
    retried nor gated. The ceiling is `max_attempts` in the statement, never a CHECK."""
    conn = _fresh_db(tmp_path)
    _run(conn)
    _repo(conn)
    _insert_phase(conn, attempts=4, max_attempts=3, status="REQUIRES_HUMAN_INTERVENTION")
    row = conn.execute("SELECT attempts, status FROM phases").fetchone()
    assert row == (4, "REQUIRES_HUMAN_INTERVENTION")
    conn.execute("UPDATE phases SET attempts = attempts + 1")  # still no upper bound
    assert conn.execute("SELECT attempts FROM phases").fetchone()[0] == 5


def test_budget_ledger_refuses_an_over_reserve(tmp_path: Path) -> None:
    """Why: fail-closed is a CONSTRAINT, not a convention. Twelve workers each reserving against a
    stale read of a $497/$500 ledger all succeeded and overspent the ceiling by 6%+. The CHECK
    makes the unguarded write raise; the guarded CAS makes the correct write refuse with
    `rowcount == 0` instead of raising, which is how a worker learns it did not get the money."""
    conn = _fresh_db(tmp_path)
    _run(conn)
    conn.execute(
        "INSERT INTO budget_ledger (run_id, spent_usd, max_usd, updated_at) "
        "VALUES ('run-1', 490.0, 500.0, '2026-08-09T00:00:00Z')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE budget_ledger SET reserved_usd = reserved_usd + 20.0")
    cur = conn.execute(
        "UPDATE budget_ledger SET reserved_usd = reserved_usd + 20.0 "
        "WHERE run_id = 'run-1' AND halted = 0 AND spent_usd + reserved_usd + 20.0 <= max_usd"
    )
    assert cur.rowcount == 0
    assert conn.execute("SELECT reserved_usd FROM budget_ledger").fetchone()[0] == 0.0
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE budget_ledger SET spent_usd = -1.0")


def test_repo_ledger_carries_the_same_ceiling(tmp_path: Path) -> None:
    """Why: the per-repo ceiling exists so one pathological repo cannot eat the fleet's budget.
    Enforcing it only in the fleet-wide ledger would let a single repo spend the whole run."""
    conn = _fresh_db(tmp_path)
    _run(conn)
    _repo(conn)
    conn.execute(
        "INSERT INTO repo_ledger (run_id, repo_id, spent_usd, max_usd, updated_at) "
        "VALUES ('run-1', 'repo-1', 9.0, 10.0, '2026-08-09T00:00:00Z')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE repo_ledger SET reserved_usd = 5.0")


def test_edges_edge_key_is_unique_within_a_run(tmp_path: Path) -> None:
    """Why: `edge_id` is a rowid, reassigned on every graph rebuild, so it can never be a stable
    reference. `edge_key` — sha256 over the logical tuple — is THE identity a `findings` payload
    cites. Two rows of ONE run sharing one key would make a cycle report point at two different
    edges. The two rows below differ in `evidence_line`, so only the key UNIQUE can fire.

    Scoped to the run, not global: the same edge inferred by a second run is the SAME key by
    construction (§5), and that run must be able to hold its own row for it — `edges.run_id`
    CASCADEs, so a shared row would be deleted out from under one of them.
    """
    conn = _fresh_db(tmp_path)
    _run(conn)
    _run(conn, "run-2")
    edge_key = "a" * 64
    insert = (
        "INSERT INTO edges (edge_key, run_id, src_id, dst_coord_key, kind, base_confidence, "
        "confidence, evidence_path, evidence_line, detected_at) "
        "VALUES (?, ?, 'repo-1', 'maven::lib', 'BUILD', 0.9, 0.9, 'pom.xml', ?, "
        "'2026-08-09T00:00:00Z')"
    )
    conn.execute(insert, (edge_key, "run-1", 1))
    with pytest.raises(sqlite3.IntegrityError, match=r"edges\.edge_key"):
        conn.execute(insert, (edge_key, "run-1", 2))
    conn.execute(insert, (edge_key, "run-2", 1))  # a sibling run holds the same edge, its own row


def test_the_edge_key_recipe_and_the_edges_unique_tuple_are_one_key(tmp_path: Path) -> None:
    """THE drift guard: §5's recipe and §6's UNIQUE tuple must never be two recipes again.

    They were. §5 hashed `(src_kind, src_id, dst_kind, dst_ref, kind, evidence_path,
    evidence_line)`; §6's UNIQUE tuple carried `run_id` and dropped `dst_kind`; the v007 back-fill
    followed §6. One edge, two keys — so a cycle-break decision recorded by the graph layer could
    not be found by anything reading the row back, which is the exact defect ADR-0026 introduced
    `edge_key` to remove.

    The assertion is deliberately made against the DDL TEXT and the stored ROW, never against a
    restated tuple: it derives the key from `DependencyEdge` (§5) and again from the columns the
    shipped `UNIQUE (…)` clause names (§6), and requires them to be one string. Adding `run_id`
    back to the hash, or dropping `dst_kind` from either side, fails here.

    This pins §5 and §6 to EACH OTHER. What pins the recipe itself is
    `test_graph_build.py::test_edge_key_is_the_documented_recipe_and_nothing_else`, which hashes
    the preimage literally; between them, neither the recipe nor the agreement can move unnoticed.
    """
    conn = _fresh_db(tmp_path)
    _run(conn)
    # §6's tuple is exactly `run_id` + the hashed columns, in hash order. `run_id` partitions
    # rows; hashing it would give one edge two keys in two runs and break §11.6 / §12.21.
    assert ("run_id", *EDGE_KEY_COLUMNS) in _unique_tuples(conn, "edges")
    assert "dst_kind" in EDGE_KEY_COLUMNS, "a REPO dst and a CONTRACT dst share `dst_coord_key`"

    edge = DependencyEdge(
        edge_key=edge_key_for(
            src_kind=NodeKind.REPO, src_id="repo-1", dst_kind=NodeKind.REPO,
            dst_ref="maven:com.acme:lib", kind=EdgeKind.DECLARED_DEP,
            evidence_path="pom.xml", evidence_line=12,
        ),
        src_id="repo-1",
        dst_coordinate=Coordinate(ecosystem=Ecosystem.MAVEN, group="com.acme", name="lib"),
        dst_id="repo-2", kind=EdgeKind.DECLARED_DEP, base_confidence=1.0, confidence=1.0,
        evidence_path="pom.xml", evidence_line=12,
    )
    conn.execute(
        "INSERT INTO edges (edge_key, run_id, src_kind, src_id, dst_kind, dst_id, dst_coord_key, "
        "kind, base_confidence, confidence, evidence_path, evidence_line, detected_at) "
        "VALUES (?, 'run-1', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            edge.edge_key, edge.src_kind, edge.src_id, edge.dst_kind, edge.dst_id,
            "maven:com.acme:lib", edge.kind, edge.base_confidence, edge.confidence,
            edge.evidence_path, edge.evidence_line, FROZEN.isoformat(),
        ),
    )

    # Re-derive from the persisted values of the DDL's own UNIQUE columns, minus `run_id`.
    stored = conn.execute(
        f"SELECT {', '.join(EDGE_KEY_COLUMNS)}, edge_key FROM edges"  # noqa: S608 — DDL-derived
    ).fetchone()
    assert edge_key_from_row(stored[:-1]) == edge.edge_key == stored[-1]


def test_deleting_a_run_cascades_to_its_events(tmp_path: Path) -> None:
    """Why: `events` used to carry no FK to `runs`, so `fleet gc` deleting a run left its event
    rows behind forever — the single unbounded table in the schema, orphaned and unreachable.
    The CASCADE must also be surgical: a sibling run's events survive."""
    conn = _fresh_db(tmp_path)
    _run(conn, "run-1")
    _run(conn, "run-2")
    _insert_event(conn, "run-1", 1, "uid-1")
    _insert_event(conn, "run-2", 1, "uid-2")
    conn.execute("DELETE FROM runs WHERE run_id = 'run-1'")
    remaining = conn.execute("SELECT run_id FROM events").fetchall()
    assert remaining == [("run-2",)]


def test_phases_status_domain_is_exactly_the_repostatus_enum(tmp_path: Path) -> None:
    """Why: the DDL's CHECK and §5 `RepoStatus` are two spellings of ONE domain, and they had
    drifted in both directions at once — the list carried 'FAILED', which is not an enum member,
    and omitted 'BLOCKED', which is. SQLite therefore rejected a status the model considers legal
    (a `blocked_by` propagation, §3.5) and accepted one the model cannot parse (any pre-7 row
    written as 'FAILED' raises on read). The enum is the source of truth, so the expected set is
    DERIVED from it here rather than retyped: adding a `RepoStatus` member without widening the
    CHECK fails this test instead of failing at 3 a.m. on a 250-repo run.

    Both halves matter. The text assertion catches drift in the DDL as written; the round trip
    catches a CHECK that parses but does not admit what the enum promises. `SKIPPED` is included
    deliberately — `fleet quarantine` (§10) sends a live repo there mid-run, so a phase row must
    be able to hold it — and `REQUIRES_HUMAN_INTERVENTION` because CLAUDE.md Rule 11 makes it the
    terminal state of an exhausted repo, so a schema rejecting it would make fail-loud unwritable.
    """
    expected = {s.value for s in RepoStatus}
    assert _declared_domain("phases", "status") == expected, (
        "schema.sql's phases.status CHECK has drifted from RepoStatus"
    )

    conn = _fresh_db(tmp_path)
    _run(conn)
    for i, status in enumerate(sorted(expected)):
        repo_id = _repo(conn, f"repo-{i}")
        _insert_phase(conn, repo_id=repo_id, phase=1, status=status)
        assert RepoStatus(status)  # what the DDL accepts, the model parses
    assert conn.execute("SELECT COUNT(DISTINCT status) FROM phases").fetchone()[0] == len(expected)

    _repo(conn, "repo-bogus")
    for bogus in ("FAILED", "DONE", "pending"):  # 'FAILED' is the exact pre-7 regression
        assert bogus not in expected
        with pytest.raises(sqlite3.IntegrityError):
            _insert_phase(conn, repo_id="repo-bogus", phase=1, status=bogus)


def test_a_fresh_phase_row_has_a_validatable_max_attempts(tmp_path: Path) -> None:
    """Why: the same bug already fixed for `heartbeat_ttl_seconds`. The column was nullable with
    no default while §5 `PhaseRecord.max_attempts` declares `default=MAX_ATTEMPTS, ge=1, le=8`,
    so any row inserted by SQL came back NULL and could not be validated into the model at all —
    and `attempts >= NULL` is never true, so an exhausted phase would never reach its Rule 11
    terminal state. The default is asserted against `MAX_ATTEMPTS` rather than the literal 3, so
    changing the ladder default without touching the DDL fails here."""
    conn = _fresh_db(tmp_path)
    _run(conn)
    _repo(conn)
    _insert_phase(conn, phase=1)
    stored = conn.execute("SELECT max_attempts FROM phases").fetchone()[0]
    assert stored == MAX_ATTEMPTS
    # The SQL default validates straight back into §5 — the round trip a NULL made impossible.
    rec = PhaseRecord(phase=Phase.SCAN, max_attempts=stored, attempts=stored, updated_at=FROZEN)
    assert rec.exhausted(), "a phase at its stored ceiling must read as exhausted (Rule 11)"
    with pytest.raises(sqlite3.IntegrityError):
        _insert_phase(conn, phase=2, max_attempts=None)
    for out_of_rail in (0, 9):  # mirrors §5's ge=1, le=8
        with pytest.raises(sqlite3.IntegrityError):
            _insert_phase(conn, phase=2, max_attempts=out_of_rail)


def test_a_phase_row_holds_the_fence_the_model_carries_and_invalidates_a_stolen_lease(
    tmp_path: Path,
) -> None:
    """Why: fencing is normative in §6 but was missing from §5, so `PhaseRecord` could not
    round-trip its own row. This drives the whole loop end to end: a model instance carrying a
    fence is written to the column, read back into the model unchanged, and then a write from the
    OLD fence matches zero rows once the reaper has bumped it — which is exactly the signal that
    tells that worker to abort without touching git."""
    conn = _fresh_db(tmp_path)
    _run(conn)
    _repo(conn)
    rec = PhaseRecord(
        phase=Phase.TRANSFORM, status=RepoStatus.RUNNING, lease_owner=LEASE_OWNER, lease_fence=4,
        heartbeat_at=FROZEN, heartbeat_ttl_seconds=300, lease_expires_at=FROZEN,
        started_at=FROZEN, updated_at=FROZEN,
    )
    _insert_phase(
        conn, phase=int(rec.phase), status=rec.status.value, lease_owner=rec.lease_owner,
        lease_fence=rec.lease_fence, heartbeat_ttl_seconds=rec.heartbeat_ttl_seconds,
    )
    stored = conn.execute(
        "SELECT phase, status, lease_owner, lease_fence, heartbeat_ttl_seconds FROM phases"
    ).fetchone()
    assert stored == (2, "RUNNING", LEASE_OWNER, 4, 300)
    # The row validates straight back into the model — the round trip §5 could not previously make.
    assert PhaseRecord(
        phase=Phase(stored[0]), status=RepoStatus(stored[1]), lease_owner=stored[2],
        lease_fence=stored[3], heartbeat_ttl_seconds=stored[4], updated_at=FROZEN,
    ).lease_fence == rec.lease_fence

    conn.execute("UPDATE phases SET status='PENDING', lease_owner=NULL, "
                 "lease_fence = lease_fence + 1 WHERE status='RUNNING'")   # the reaper
    stale = conn.execute(
        "UPDATE phases SET attempts = attempts + 1 WHERE lease_fence = ?", (rec.lease_fence,)
    )
    assert stale.rowcount == 0, "a write under a stolen lease must match zero rows, not merge"
    assert conn.execute("SELECT lease_fence, attempts FROM phases").fetchone() == (5, 0)


def test_a_fresh_phase_row_has_a_usable_heartbeat_ttl(tmp_path: Path) -> None:
    """Why: the column was nullable with no default while §5 declared `default=300, gt=0`, so a
    row inserted by SQL was NULL and the model could not validate it. Worse, `NULL > interval` is
    never true, so a worker holding a NULL ttl was never reapable at all."""
    conn = _fresh_db(tmp_path)
    _run(conn)
    _repo(conn)
    _insert_phase(conn, phase=1)
    assert conn.execute("SELECT heartbeat_ttl_seconds FROM phases").fetchone()[0] == 300
    with pytest.raises(sqlite3.IntegrityError):
        _insert_phase(conn, phase=2, heartbeat_ttl_seconds=None)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_phase(conn, phase=3, heartbeat_ttl_seconds=0)


def test_a_refutation_from_a_longer_ladder_is_recordable(tmp_path: Path) -> None:
    """Why: `CHECK (attempt BETWEEN 1 AND 3)` on `rejected_approaches` hard-coded the DEFAULT
    ladder length into a type constraint while `tasks.max_attempts` allows 1..8. On a task
    configured with a longer ladder, rung 4's refutation RAISED instead of being recorded — so the
    anti-anchoring memory (ADR-0021) silently lost exactly the approaches it exists to remember.
    The ceiling is the owning task's `max_attempts`, checked at runtime."""
    conn = _fresh_db(tmp_path)
    _run(conn)
    _repo(conn)
    conn.execute(
        "INSERT INTO tasks (task_id, run_id, repo_id, phase, kind, dest_path, max_attempts, "
        "ladder, created_at) VALUES ('t-1', 'run-1', 'repo-1', 2, 'TRANSFORM', 'apps/x', 4, "
        "'[null,\"EVIDENCE_ONLY\",\"EVIDENCE_PLUS_REJECTED_APPROACHES\",\"EVIDENCE_ONLY\"]', "
        "'2026-08-09T00:00:00Z')"
    )
    insert = (
        "INSERT INTO rejected_approaches (run_id, task_id, approach_signature, reason, "
        "failure_class, attempt, tier, created_at) VALUES ('run-1', 't-1', ?, 'rewrote the pom', "
        "'BUILD_ERROR', ?, 'WORKHORSE', '2026-08-09T00:00:00Z')"
    )
    conn.execute(insert, ("a" * 64, 4))          # rung 4 of a 4-rung ladder: recordable
    conn.execute(insert, ("b" * 64, 8))          # and the 1..8 sanity rail is the task's, not this
    assert conn.execute("SELECT COUNT(*) FROM rejected_approaches").fetchone()[0] == 2
    with pytest.raises(sqlite3.IntegrityError):  # a 0th attempt is still meaningless
        conn.execute(insert, ("c" * 64, 0))


def test_scc_id_must_be_a_content_derived_string(tmp_path: Path) -> None:
    """Why: `scc_id` was an INTEGER that renumbered on every graph rebuild, so a stored SCC
    reference pointed at a different cycle after a re-derive. §5 `SccId` is 'scc:<16-hex>',
    derived from the member set; the LIKE check is what keeps a bare renumbered int out."""
    conn = _fresh_db(tmp_path)
    _run(conn)
    _repo(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_phase(conn, phase=1, scc_id="0123456789abcdef")
    _insert_phase(conn, phase=1, scc_id="scc:0123456789abcdef")
    _insert_phase(conn, phase=2, scc_id=None)  # NULL = not in an ATOMIC_WAVE SCC, still legal
    assert conn.execute("SELECT COUNT(*) FROM phases").fetchone()[0] == 2
