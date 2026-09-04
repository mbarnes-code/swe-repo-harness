"""The migration ladder (SPEC §6) — what these tests exist to prove.

A migration that silently drops rows is the worst failure this repository can ship: a 250-repo
fleet is days of LLM spend, and §6 is explicit that a `user_version` gap must never be a reason to
discard an in-flight run. So the centre of this file is `test_data_survives_the_six_to_seven_step`:
a pre-v7 database is populated, migrated, and every row is read back and compared value by value.
Everything else guards a mechanism that test depends on — that the version is re-read *under* the
exclusive lock, that a failing step leaves no trace, and that the foreign-key gate runs before the
commit rather than after it.

The v6 fixture below is derived from §6's 6 → 7 note by reading its `ALTER`/rebuild list
backwards: every column that note adds is absent here, `phases` still carries `owner_pid`, an
INTEGER `scc_id`, a `'FAILED'` status and the `attempts BETWEEN 0 AND 3` CHECK, and
`rejected_approaches` still bakes the default ladder length into its own CHECK. Tables the note
does not touch are created from `schema.sql` itself, so the fixture invents no more DDL than it
must.
"""

from __future__ import annotations

import re
import shutil
import sqlite3
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Final

import pytest

from fleet.migrations import (
    EARLIEST_MIGRATABLE_VERSION,
    LATEST_VERSION,
    STEPS,
    MigrationIntegrityError,
    MigrationStep,
    MigrationStepError,
    MigrationVersionError,
    current_version,
    migrate,
    v007_logical_keys,
    v008_reservations,
)
from fleet.migrations import _support
from fleet.migrations._support import sha256_nul
from fleet.models.graph import edge_key_for
from fleet.state.db import SCHEMA_PATH

SCHEMA_SQL = Path(SCHEMA_PATH).read_text(encoding="utf-8")

SIG = "a" * 64
COMMIT_SHA = "b" * 40
PATCH_ID = "c" * 64

# --------------------------------------------------------------------------------------
# the v6 fixture — every table the 6 -> 7 note reshapes, in its pre-7 shape
# --------------------------------------------------------------------------------------

_V6_TABLES: dict[str, str] = {
    "runs": """
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
            config_sha256 TEXT NOT NULL,
            monorepo_branch TEXT NOT NULL DEFAULT 'integration',
            harness_version TEXT NOT NULL)
    """,
    "repos": """
        CREATE TABLE repos (
            repo_id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, url TEXT NOT NULL,
            default_branch TEXT NOT NULL DEFAULT 'main',
            default_branch_source TEXT NOT NULL DEFAULT 'symbolic-ref',
            head_sha TEXT, ecosystems TEXT NOT NULL DEFAULT '[]', primary_coord_key TEXT,
            dest_path TEXT, kind TEXT NOT NULL DEFAULT 'unknown', framework TEXT, owner_hint TEXT,
            size_bytes INTEGER NOT NULL DEFAULT 0, commit_count INTEGER NOT NULL DEFAULT 0,
            last_commit_at TEXT, cloned_at TEXT, is_shallow INTEGER NOT NULL DEFAULT 0,
            submodule_count INTEGER NOT NULL DEFAULT 0, has_lfs INTEGER NOT NULL DEFAULT 0,
            lfs_object_bytes INTEGER NOT NULL DEFAULT 0,
            largest_blob_bytes INTEGER NOT NULL DEFAULT 0, preflight_ok INTEGER,
            blast_radius INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL)
    """,
    "coordinates": """
        CREATE TABLE coordinates (
            coord_key TEXT PRIMARY KEY, ecosystem TEXT NOT NULL,
            grp TEXT NOT NULL DEFAULT '', name TEXT NOT NULL,
            owner_repo_id TEXT REFERENCES repos(repo_id) ON DELETE SET NULL,
            -- pre-9: no `version` column — §37 Blocker B, added at 8 -> 9
            first_seen_at TEXT NOT NULL)
    """,
    "edges": """
        CREATE TABLE edges (
            edge_id INTEGER PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
            src_kind TEXT NOT NULL DEFAULT 'REPO', src_id TEXT NOT NULL,
            dst_kind TEXT NOT NULL DEFAULT 'REPO', dst_id TEXT, dst_coord_key TEXT NOT NULL,
            dst_candidate_repo_ids TEXT NOT NULL DEFAULT '[]', retargeted_from_repo_id TEXT,
            kind TEXT NOT NULL, version_spec TEXT, base_confidence REAL NOT NULL,
            confidence REAL NOT NULL, confidence_factors TEXT NOT NULL DEFAULT '{}',
            ambiguous INTEGER NOT NULL DEFAULT 0,
            ordering_suppressed INTEGER NOT NULL DEFAULT 0, evidence_path TEXT NOT NULL,
            evidence_line INTEGER NOT NULL DEFAULT -1, detected_at TEXT NOT NULL,
            UNIQUE (run_id, src_kind, src_id, dst_coord_key, kind, evidence_path, evidence_line))
    """,
    "waves": """
        CREATE TABLE waves (
            run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
            wave_index INTEGER NOT NULL, computed_at TEXT NOT NULL,
            PRIMARY KEY (run_id, wave_index))
    """,
    "phases": """
        CREATE TABLE phases (
            run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
            repo_id TEXT NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
            phase INTEGER NOT NULL CHECK (phase BETWEEN 1 AND 4),
            -- pre-7: 'FAILED' present (not a RepoStatus member), 'BLOCKED' absent (one that is)
            status TEXT NOT NULL DEFAULT 'PENDING'
                   CHECK (status IN ('PENDING','RUNNING','SUCCEEDED','FAILED','DEGRADED',
                                     'REQUIRES_HUMAN_INTERVENTION','SKIPPED')),
            attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 3),
            transient_retries INTEGER NOT NULL DEFAULT 0, failure_class TEXT, last_error TEXT,
            blocked_by TEXT NOT NULL DEFAULT '[]', stubbed_deps TEXT NOT NULL DEFAULT '[]',
            scc_id INTEGER,                      -- pre-7: a renumbered int, no textual preimage
            pr_url TEXT, heartbeat_at TEXT,
            owner_pid INTEGER,                   -- pre-7: replaced by lease_owner at 7
            base_ref TEXT, pre_commit_sha TEXT, post_commit_sha TEXT, started_at TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (run_id, repo_id, phase))
    """,
    "tasks": """
        CREATE TABLE tasks (
            task_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
            repo_id TEXT NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
            phase INTEGER NOT NULL, kind TEXT NOT NULL, contract_id TEXT, revalidation_key TEXT,
            dest_path TEXT NOT NULL, target_paths TEXT NOT NULL DEFAULT '[]',
            rule_ids TEXT NOT NULL DEFAULT '[]', token_budget INTEGER NOT NULL DEFAULT 200000,
            ladder TEXT NOT NULL
                DEFAULT '[null,"EVIDENCE_ONLY","EVIDENCE_PLUS_REJECTED_APPROACHES"]',
            created_at TEXT NOT NULL,
            CHECK ((kind = 'HOIST') = (contract_id IS NOT NULL)),
            CHECK ((kind = 'REVALIDATE') = (revalidation_key IS NOT NULL)))
    """,
    "attempts": """
        CREATE TABLE attempts (
            attempt_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
            repo_id TEXT NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
            task_id TEXT REFERENCES tasks(task_id) ON DELETE SET NULL,
            phase INTEGER NOT NULL, attempt INTEGER NOT NULL CHECK (attempt >= 1),
            tier TEXT NOT NULL DEFAULT 'DETERMINISTIC', context_policy TEXT,
            approach_signature TEXT NOT NULL DEFAULT '', command TEXT NOT NULL DEFAULT '[]',
            exit_code INTEGER, failure_class TEXT, duration_ms INTEGER NOT NULL DEFAULT 0,
            container_id TEXT, stdout_tail TEXT NOT NULL DEFAULT '',
            stderr_tail TEXT NOT NULL DEFAULT '', input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0, cost_usd REAL NOT NULL DEFAULT 0.0,
            llm_cache_hit INTEGER NOT NULL DEFAULT 0, llm_backend TEXT,
            llm_failovers INTEGER NOT NULL DEFAULT 0, patch_id TEXT, commit_sha TEXT,
            already_applied INTEGER NOT NULL DEFAULT 0,
            started_at TEXT NOT NULL, finished_at TEXT NOT NULL,
            UNIQUE (run_id, repo_id, phase, attempt, tier, approach_signature))
    """,
    "llm_cache": """
        CREATE TABLE llm_cache (
            cache_key TEXT PRIMARY KEY, role TEXT NOT NULL,
            tier TEXT NOT NULL DEFAULT 'WORKHORSE', backend TEXT NOT NULL DEFAULT 'anthropic',
            model_id TEXT NOT NULL,
            structured_output_mode TEXT NOT NULL DEFAULT 'JSON_SCHEMA', effort TEXT NOT NULL,
            context_policy TEXT,                 -- the 2 -> 3 default is sha256(b''), not ''
            rejected_approach_digest TEXT NOT NULL
                DEFAULT 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855',
            prompt_sha256 TEXT NOT NULL, response_schema_sha256 TEXT NOT NULL,
            response_json TEXT NOT NULL, input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0, cost_usd REAL NOT NULL DEFAULT 0.0,
            hit_count INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)
    """,
    "budget_ledger": """
        CREATE TABLE budget_ledger (
            run_id TEXT PRIMARY KEY REFERENCES runs(run_id) ON DELETE CASCADE,
            spent_usd REAL NOT NULL DEFAULT 0.0, reserved_usd REAL NOT NULL DEFAULT 0.0,
            max_usd REAL NOT NULL, halted INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL)
    """,
    "repo_ledger": """
        CREATE TABLE repo_ledger (
            run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
            repo_id TEXT NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
            spent_usd REAL NOT NULL DEFAULT 0.0, max_usd REAL NOT NULL,
            revalidation_usd REAL NOT NULL DEFAULT 0.0,
            revalidation_max_usd REAL NOT NULL DEFAULT 2.0,
            revalidation_rounds INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
            PRIMARY KEY (run_id, repo_id))
    """,
    "stubs": """
        CREATE TABLE stubs (
            run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
            repo_id TEXT NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
            stub_coord_key TEXT NOT NULL,
            consumer_repo_id TEXT NOT NULL DEFAULT '',
            provider_repo_id TEXT NOT NULL, pinned_version TEXT, bazel_label TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'ACTIVE',
            stub_fidelity TEXT NOT NULL DEFAULT 'PUBLISHED_ARTIFACT',
            revalidation_round INTEGER NOT NULL DEFAULT 0, revalidation_task_id TEXT,
            resolved_at TEXT, resolved_by_run_id TEXT, abandon_reason TEXT,
            created_at TEXT NOT NULL,
            PRIMARY KEY (run_id, repo_id, stub_coord_key, revalidation_round))
    """,
    "rejected_approaches": """
        CREATE TABLE rejected_approaches (
            run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
            task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
            approach_signature TEXT NOT NULL CHECK (length(approach_signature) = 64),
            reason TEXT NOT NULL, failure_class TEXT NOT NULL,
            attempt INTEGER NOT NULL CHECK (attempt BETWEEN 1 AND 3),  -- the baked-in ladder length
            tier TEXT NOT NULL, created_at TEXT NOT NULL,
            CHECK (length(reason) BETWEEN 1 AND 280),
            PRIMARY KEY (task_id, approach_signature))
    """,
    "events": """
        CREATE TABLE events (
            event_id INTEGER PRIMARY KEY,
            run_id TEXT NOT NULL,                -- pre-7: no REFERENCES runs; events outlive runs
            seq INTEGER NOT NULL, ts TEXT NOT NULL, repo_id TEXT, phase INTEGER,
            level TEXT NOT NULL, event TEXT NOT NULL, event_uid TEXT NOT NULL,
            payload TEXT NOT NULL DEFAULT '{}',
            UNIQUE (run_id, event_uid), UNIQUE (run_id, seq))
    """,
}

RUN = "11111111-1111-4111-8111-111111111111"
TASK = "22222222-2222-4222-8222-222222222222"
NOW = "2026-08-09T00:00:00Z"
#: A pre-v8 `reservation_expires_at`: the one instant the two ledgers could record between
#: them, whoever reserved last.
EXPIRY = "2026-08-09T00:05:00Z"

_ROWS: tuple[tuple[str, tuple[object, ...]], ...] = (
    (
        "INSERT INTO runs (run_id, started_at, config_sha256, harness_version) VALUES (?,?,?,?)",
        (RUN, NOW, "cfg" * 10, "1.4.2"),
    ),
    (
        "INSERT INTO repos (repo_id, name, url, blast_radius, updated_at) VALUES (?,?,?,?,?)",
        ("repo-a", "alpha", "git@x/alpha.git", 7, NOW),
    ),
    (
        "INSERT INTO repos (repo_id, name, url, blast_radius, updated_at) VALUES (?,?,?,?,?)",
        ("repo-b", "beta", "git@x/beta.git", 2, NOW),
    ),
    (
        "INSERT INTO edges (run_id, src_id, dst_id, dst_coord_key, kind, base_confidence, "
        "confidence, evidence_path, evidence_line, detected_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (RUN, "repo-a", "repo-b", "npm::beta", "RUNTIME_DEP", 0.9, 0.95, "package.json", 12, NOW),
    ),
    ("INSERT INTO waves (run_id, wave_index, computed_at) VALUES (?,?,?)", (RUN, 0, NOW)),
    (
        "INSERT INTO wave_members (run_id, wave_index, node_kind, node_id) VALUES (?,?,?,?)",
        (RUN, 0, "REPO", "repo-a"),
    ),
    (
        "INSERT INTO wave_members (run_id, wave_index, node_kind, node_id) VALUES (?,?,?,?)",
        (RUN, 0, "REPO", "repo-b"),
    ),
    (
        "INSERT INTO phases (run_id, repo_id, phase, status, attempts, scc_id, owner_pid, "
        "pre_commit_sha, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (RUN, "repo-a", 2, "FAILED", 3, 42, 31337, COMMIT_SHA, NOW),
    ),
    (
        "INSERT INTO phases (run_id, repo_id, phase, status, attempts, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (RUN, "repo-b", 1, "SUCCEEDED", 1, NOW),
    ),
    (
        "INSERT INTO tasks (task_id, run_id, repo_id, phase, kind, dest_path, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (TASK, RUN, "repo-a", 2, "MIGRATE", "libs/alpha", NOW),
    ),
    (
        "INSERT INTO attempts (attempt_id, run_id, repo_id, task_id, phase, attempt, command, "
        "exit_code, cost_usd, patch_id, commit_sha, started_at, finished_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "att-1", RUN, "repo-a", TASK, 2, 3, '["bazel", "build", "//..."]',
            1, 0.75, PATCH_ID, COMMIT_SHA, NOW, NOW,
        ),
    ),
    (
        "INSERT INTO llm_cache (cache_key, role, model_id, effort, prompt_sha256, "
        "response_schema_sha256, response_json, hit_count, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        ("key-1", "PLANNER", "claude-x", "high", "p" * 64, "s" * 64, "{}", 4, NOW),
    ),
    (
        "INSERT INTO budget_ledger (run_id, spent_usd, max_usd, updated_at) VALUES (?,?,?,?)",
        (RUN, 12.5, 500.0, NOW),
    ),
    (
        "INSERT INTO repo_ledger (run_id, repo_id, spent_usd, max_usd, updated_at) "
        "VALUES (?,?,?,?,?)",
        (RUN, "repo-a", 3.25, 50.0, NOW),
    ),
    (
        # consumer_repo_id == repo_id is the v004 back-fill, already applied at v6
        "INSERT INTO stubs (run_id, repo_id, stub_coord_key, consumer_repo_id, provider_repo_id, "
        "pinned_version, bazel_label, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (RUN, "repo-a", "npm::beta", "repo-a", "repo-b", "1.2.3", "//third_party/stubs:beta", NOW),
    ),
    (
        "INSERT INTO rejected_approaches (run_id, task_id, approach_signature, reason, "
        "failure_class, attempt, tier, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (RUN, TASK, SIG, "renamed the target instead of the package", "BUILD", 3, "WORKHORSE", NOW),
    ),
    (
        "INSERT INTO events (run_id, seq, ts, level, event, event_uid) VALUES (?,?,?,?,?,?)",
        (RUN, 1, NOW, "info", "RunStarted", "uid-1"),
    ),
)


#: Tables the CURRENT baseline declares that v6 never had. A fixture built by rewinding the
#: baseline has to drop them, or the rung that CREATEs them finds them already there.
_POST_V6_TABLES: Final = ("reservations", "file_blobs")


def _STEPS_THROUGH(version: int) -> tuple[MigrationStep, ...]:
    """The ladder truncated at `version`. A rung's own test must run that rung and stop, or its
    assertions describe whatever the newest rung left behind instead of the one it names."""
    return tuple(step for step in STEPS if step.version <= version)


def _fresh_baseline(path: Path) -> Path:
    """A brand-new database: `schema.sql`, straight to `LATEST_VERSION` — the ladder never runs."""
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.executescript(SCHEMA_SQL)
    finally:
        conn.close()
    return path


def _v6_database(path: Path, *, populate: bool = True, up_to: int = LATEST_VERSION - 1) -> Path:
    """The v6 fixture with data, lifted to `up_to` by the REAL ladder.

    The shapes are genuinely pre-v007 — never a current file with its PRAGMA rewound, which would
    test nothing: the ladder would try to add a column that already exists and fail.

    `up_to` defaults to **exactly one rung behind this harness** because that is what an
    end-to-end `fleet migrate-db` test needs (`tests/test_cli.py` imports this helper for it, and
    asserts on the rung it reports). The 6 → 7 assertions below pass `up_to=6` for the raw fixture.
    """
    _fresh_baseline(path)
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        for table in _POST_V6_TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        for table in _V6_TABLES:
            conn.execute(f"DROP TABLE {table}")
        for ddl in _V6_TABLES.values():
            conn.execute(ddl)
        if populate:
            for sql, params in _ROWS:
                conn.execute(sql, params)
        conn.execute("PRAGMA user_version = 6")
    finally:
        conn.close()
    if up_to > 6:
        migrate(path, steps=_STEPS_THROUGH(up_to))
    return path


def _query(path: Path, sql: str, params: tuple[object, ...] = ()) -> list[tuple[object, ...]]:
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        return [tuple(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _table_shape(path: Path, table: str) -> list[tuple[object, ...]]:
    """`(name, type, notnull, default)` per column — order-independent structural identity."""
    rows = _query(path, f"PRAGMA table_info('{table}')")
    return sorted((r[1], r[2], r[3], r[4]) for r in rows)


# --------------------------------------------------------------------------------------
# the registry itself
# --------------------------------------------------------------------------------------


def test_registry_is_strictly_ordered_contiguous_and_ends_at_the_baseline():
    """A glob-discovered ladder can silently reorder; a gap or a duplicate corrupts data.

    §6 fixes the ladder as `1 → 2 … 10 → 11`, so the registry must be exactly that: strictly
    ascending, no duplicate VERSION, no gap, and ending on the version `schema.sql` installs.
    """
    versions = [step.version for step in STEPS]
    assert versions == sorted(versions), "steps are not in ascending order"
    assert len(set(versions)) == len(versions), "duplicate VERSION in the registry"
    assert versions == list(range(EARLIEST_MIGRATABLE_VERSION + 1, LATEST_VERSION + 1))
    assert versions == [2, 3, 4, 5, 6, 7, 8, 9, 10, 11]
    assert len({step.module for step in STEPS}) == len(STEPS), "two steps share a module"


def test_registry_target_matches_the_schema_sql_baseline(tmp_path):
    """A ladder that ends anywhere but `schema.sql`'s `user_version` migrates into a shape no
    worker will accept — workers refuse to start on any value but the compiled-in one (§6)."""
    assert current_version(_fresh_baseline(tmp_path / "fresh.db")) == LATEST_VERSION


# --------------------------------------------------------------------------------------
# the runner's invariants
# --------------------------------------------------------------------------------------


def test_migrating_an_already_current_database_is_a_no_op(tmp_path):
    """`fleet migrate-db` is run by operators who do not know the version; running it on a current
    database must cost nothing and change nothing — otherwise the safe habit is unsafe."""
    db = _fresh_baseline(tmp_path / "fleet.db")
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO runs (run_id, started_at, config_sha256, harness_version) "
            "VALUES (?,?,?,?)",
            (RUN, NOW, "cfg", "1.4.2"),
        )
    finally:
        conn.close()
    before = _query(db, "SELECT name, sql FROM sqlite_master ORDER BY name")

    assert migrate(db) == (LATEST_VERSION, LATEST_VERSION)

    assert current_version(db) == LATEST_VERSION
    assert _query(db, "SELECT name, sql FROM sqlite_master ORDER BY name") == before
    assert _query(db, "SELECT run_id, harness_version FROM runs") == [(RUN, "1.4.2")]


def test_a_step_that_raises_rolls_back_its_whole_transaction(tmp_path):
    """CLAUDE.md Rule 11 and §6's one-transaction-per-step: a half-applied DDL with the version
    already bumped is unrecoverable by any later run of the ladder, so the transaction must carry
    the version bump and the DDL together or not at all."""
    db = _v6_database(tmp_path / "fleet.db", up_to=6)

    def exploding(conn: sqlite3.Connection) -> None:
        conn.execute("ALTER TABLE runs ADD COLUMN half_applied TEXT")
        raise RuntimeError("boom")

    with pytest.raises(MigrationStepError) as excinfo:
        migrate(db, steps=(MigrationStep(7, exploding, "v007_exploding"),))

    assert "v007_exploding" in str(excinfo.value), "the error must name the step that failed"
    assert current_version(db) == 6
    columns = {row[1] for row in _query(db, "PRAGMA table_info('runs')")}
    assert "half_applied" not in columns, "partial DDL survived the rollback"


def test_a_database_ahead_of_the_ladder_is_refused_not_downgraded(tmp_path):
    """Forward-only (§6). A harness that "helpfully" rewound a newer database would delete columns
    a newer harness is actively writing."""
    db = _fresh_baseline(tmp_path / "fleet.db")
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        conn.execute("PRAGMA user_version = 99")
    finally:
        conn.close()
    with pytest.raises(MigrationVersionError, match="FORWARD-ONLY"):
        migrate(db)
    assert current_version(db) == 99


def test_an_unschemad_database_is_refused_by_name(tmp_path):
    """§6: a fresh database gets `schema.sql` and lands directly at 7; the ladder is only for
    databases that already hold data. Pretending to migrate an empty file would produce a
    half-schema no `CREATE TABLE IF NOT EXISTS` could repair."""
    db = tmp_path / "empty.db"
    sqlite3.connect(db).close()
    with pytest.raises(MigrationVersionError, match="initialize_database"):
        migrate(db)


# --------------------------------------------------------------------------------------
# THE test: data survives 6 -> 7
# --------------------------------------------------------------------------------------


def test_data_survives_the_six_to_seven_step(tmp_path):
    """A 250-repo run is days of LLM spend and §6 forbids discarding it across a version gap.

    Every row written at v6 must still be there at v7, with its values intact, the columns the
    step adds at their declared defaults, and the back-fills §6 names actually derived rather than
    guessed. A migration that silently drops rows is the worst failure in this package.
    """
    db = _v6_database(tmp_path / "fleet.db", up_to=6)
    # This rung only. Later rungs get their own data-survival test — a single "migrate to LATEST"
    # here would stop naming which step lost the row it lost.
    assert migrate(db, steps=_STEPS_THROUGH(7)) == (6, 7)
    assert current_version(db) == 7

    # --- nothing was dropped ---
    for table, expected in (
        ("runs", 1), ("repos", 2), ("edges", 1), ("waves", 1), ("wave_members", 2),
        ("phases", 2), ("tasks", 1), ("attempts", 1), ("llm_cache", 1), ("budget_ledger", 1),
        ("repo_ledger", 1), ("stubs", 1), ("rejected_approaches", 1), ("events", 1),
    ):
        # S608: `table` is a literal from the tuple above, never input
        counted = _query(db, f"SELECT COUNT(*) FROM {table}")[0][0]  # noqa: S608
        assert counted == expected, table

    # --- pre-existing values survived verbatim ---
    assert _query(db, "SELECT started_at, config_sha256, harness_version FROM runs") == [
        (NOW, "cfg" * 10, "1.4.2")
    ]
    assert _query(db, "SELECT repo_id, name, blast_radius FROM repos ORDER BY repo_id") == [
        ("repo-a", "alpha", 7),
        ("repo-b", "beta", 2),
    ]
    assert _query(db, "SELECT spent_usd, max_usd FROM budget_ledger") == [(12.5, 500.0)]
    assert _query(db, "SELECT spent_usd, max_usd FROM repo_ledger") == [(3.25, 50.0)]
    assert _query(db, "SELECT hit_count, response_json FROM llm_cache") == [(4, "{}")]
    assert _query(
        db, "SELECT attempt, exit_code, cost_usd, patch_id, commit_sha FROM attempts"
    ) == [(3, 1, 0.75, PATCH_ID, COMMIT_SHA)]
    assert _query(db, "SELECT seq, event, event_uid FROM events") == [(1, "RunStarted", "uid-1")]
    assert _query(db, "SELECT attempt, reason FROM rejected_approaches") == [
        (3, "renamed the target instead of the package")
    ]

    # --- the new columns took their declared defaults (§6: fence 0, unheld lease, ttl 300) ---
    assert _query(
        db,
        "SELECT lease_fence, lease_owner, lease_expires_at, heartbeat_ttl_seconds, max_attempts "
        "FROM phases WHERE repo_id = 'repo-b'",
    ) == [(0, None, None, 300, 3)]
    assert _query(db, "SELECT status, fence_token, claimed_by, max_attempts FROM tasks") == [
        ("PENDING", 0, None, 3)
    ]
    assert _query(db, "SELECT config_digests FROM runs") == [("{}",)]
    # baseline_ok back-fills to NULL, NOT 1: no pre-7 run measured a native baseline (§6).
    assert _query(db, "SELECT baseline_ok, baseline_test_count FROM repos ORDER BY repo_id") == [
        (None, 0),
        (None, 0),
    ]
    assert _query(db, "SELECT reserved_usd, reservation_expires_at FROM repo_ledger") == [
        (0.0, None)
    ]
    assert _query(db, "SELECT max_revalidation_rounds FROM stubs") == [(2,)]

    # --- the back-fills that RE-DERIVE (§6: "re-derive, do not guess") ---
    # Derived by CALLING §5's recipe, never by restating it: the back-fill, the model and §6's
    # UNIQUE tuple are one key, and `run_id` is not part of it (it partitions rows, §11.6).
    expected_edge_key = edge_key_for(
        src_kind="REPO", src_id="repo-a", dst_kind="REPO", dst_ref="npm::beta",
        kind="RUNTIME_DEP", evidence_path="package.json", evidence_line=12,
    )
    assert _query(db, "SELECT edge_key FROM edges") == [(expected_edge_key,)]
    assert RUN not in expected_edge_key
    # the wave ceiling SCALES with membership: 8.0 x 2 members
    assert _query(db, "SELECT max_usd, synthetic, wave_started_at FROM waves") == [
        (v007_logical_keys.WAVE_MAX_COST_USD_PER_REPO * 2, 0, None)
    ]
    assert _query(db, "SELECT last_hit_at FROM llm_cache") == [(NOW,)]
    assert _query(db, "SELECT state_changed_at FROM stubs") == [(NOW,)]
    stub_id = _query(db, "SELECT stub_id FROM stubs")[0][0]
    assert stub_id == sha256_nul(RUN, "repo-b", "npm::beta")
    command_sha = _query(db, "SELECT command_sha256 FROM attempts")[0][0]
    assert isinstance(command_sha, str) and len(command_sha) == 64

    # --- the reshapes §6 spells out ---
    # 'FAILED' is not a RepoStatus member; the pre-rebuild UPDATE is what stops the new CHECK
    # from rejecting every pre-7 exhausted row.
    assert _query(db, "SELECT status FROM phases WHERE repo_id = 'repo-a'") == [
        ("REQUIRES_HUMAN_INTERVENTION",)
    ]
    # scc_id is RE-DERIVED, not cast: an int id has no textual preimage, so it becomes NULL.
    assert _query(db, "SELECT scc_id FROM phases WHERE repo_id = 'repo-a'") == [(None,)]
    phase_columns = {row[1] for row in _query(db, "PRAGMA table_info('phases')")}
    assert "owner_pid" not in phase_columns, "the rebuild must drop owner_pid"
    assert {"lease_owner", "lease_fence", "lease_expires_at"} <= phase_columns
    # the rung-4 refutation the v7 baseline accepts and the 2 -> 3 CHECK rejected
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO rejected_approaches (run_id, task_id, approach_signature, reason, "
            "failure_class, attempt, tier, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (RUN, TASK, "d" * 64, "rung four", "BUILD", 4, "WORKHORSE", NOW),
        )
    finally:
        conn.close()

    # --- the indexes the step must install (§6) ---
    indexes = _query(db, "SELECT name FROM sqlite_master WHERE type = 'index'")
    index_names = {row[0] for row in indexes}
    assert {"ix_tasks_claimable", "ix_phases_lease", "ix_events_seq", "ix_llm_cache_lru"} <= (
        index_names
    )


def test_migrated_tables_match_a_freshly_created_v7_baseline(tmp_path):
    """§6 makes `schema.sql` the v7 baseline. If a migrated database's rebuilt tables differ from
    a fresh one's, then "user_version = 7" names two different shapes and every later migration,
    query, and CHECK is written against a schema only one of them has.

    `llm_cache` is compared in `test_the_only_divergence_from_a_fresh_baseline_is_documented`: it
    is deliberately NOT on §6's rebuild list, so it keeps one placeholder default.
    """
    migrated = _v6_database(tmp_path / "old.db", up_to=6)
    migrate(migrated)
    fresh = _fresh_baseline(tmp_path / "new.db")

    for table in _V6_TABLES:
        if table == "llm_cache":
            continue
        assert _table_shape(migrated, table) == _table_shape(fresh, table), table

    migrated_tables = {row[0] for row in _query(migrated, "SELECT name FROM sqlite_master "
                                                         "WHERE type='table'")}
    fresh_tables = {row[0] for row in _query(fresh, "SELECT name FROM sqlite_master "
                                                   "WHERE type='table'")}
    assert migrated_tables == fresh_tables, "a scratch rebuild table leaked, or one was lost"


def test_the_only_divergence_from_a_fresh_baseline_is_documented(tmp_path):
    """§6's 6 → 7 rebuild list does not include `llm_cache`, so the `NOT NULL` column the step adds
    to it keeps the placeholder `DEFAULT ''` that SQLite requires to add such a column at all —
    the v7 baseline declares `last_hit_at` with no default. This test pins that divergence to
    EXACTLY one column so it stays a known, argued deviation rather than the first of several.
    Adding a twelfth rebuild here would be the ladder overruling its own spec; if the placeholder
    is unacceptable, §6's list is what has to change.
    """
    migrated = _v6_database(tmp_path / "old.db", up_to=6)
    migrate(migrated)
    fresh = _fresh_baseline(tmp_path / "new.db")

    divergent = set(_table_shape(migrated, "llm_cache")) ^ set(_table_shape(fresh, "llm_cache"))
    assert divergent == {
        ("last_hit_at", "TEXT", 1, "''"),  # migrated: the ALTER's placeholder default
        ("last_hit_at", "TEXT", 1, None),  # fresh: schema.sql, no default
    }


def test_foreign_key_check_is_clean_after_migrating(tmp_path):
    """The rebuild path runs with `PRAGMA foreign_keys` OFF (§6), which means nothing else is
    watching: `foreign_key_check` is the only thing standing between a `RENAME` that re-pointed a
    child table and a database that looks fine until the first CASCADE deletes the wrong rows."""
    db = _v6_database(tmp_path / "fleet.db", up_to=6)
    migrate(db)
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_a_foreign_key_violation_aborts_the_step_before_it_commits(tmp_path):
    """§6: `foreign_key_check` MUST return zero rows *before* the commit. Checking after would
    report a corruption that is already durable."""
    db = _v6_database(tmp_path / "fleet.db", up_to=6)
    conn = sqlite3.connect(db, isolation_level=None)
    try:  # legal at v6 — `events` gains its REFERENCES runs(run_id) only at 7
        conn.execute(
            "INSERT INTO events (run_id, seq, ts, level, event, event_uid) VALUES (?,?,?,?,?,?)",
            ("no-such-run", 2, NOW, "info", "Orphan", "uid-2"),
        )
    finally:
        conn.close()

    with pytest.raises(MigrationIntegrityError, match="foreign-key violation"):
        migrate(db)
    assert current_version(db) == 6, "the version must not advance past a failed integrity gate"


# --------------------------------------------------------------------------------------
# the exclusive transaction
# --------------------------------------------------------------------------------------


def test_the_version_is_re_read_inside_the_exclusive_transaction(tmp_path):
    """A version read BEFORE `BEGIN EXCLUSIVE` is a race, and its loser re-applies a ladder that
    another process already applied (§6).

    The construction: the winner holds the exclusive transaction inside a deliberately slow 6 → 7
    step. The loser calls `migrate()` while that transaction is open and uncommitted — under WAL
    it *can* read `user_version` and would see 6 — then blocks on `BEGIN EXCLUSIVE`. If the check
    lived outside the transaction, the loser would act on that stale 6 and re-run v007, whose
    first `ALTER TABLE … ADD COLUMN edge_key` raises "duplicate column name". Observing `(7, 7)`
    is therefore positive evidence that the version it acted on was read after it held the lock.
    """
    db = _v6_database(tmp_path / "fleet.db", up_to=6)
    inside = threading.Event()

    def slow_upgrade(conn: sqlite3.Connection) -> None:
        inside.set()
        time.sleep(0.5)
        v007_logical_keys.upgrade(conn)

    with ThreadPoolExecutor(max_workers=2) as pool:
        slow = (MigrationStep(7, slow_upgrade, "v007_slow"),)
        winner = pool.submit(migrate, db, steps=slow)
        assert inside.wait(timeout=10.0), "the winner never entered its step"
        time.sleep(0.05)  # the loser starts while the winner's transaction is still open
        loser = pool.submit(migrate, db, steps=slow)
        assert winner.result(timeout=60.0) == (6, 7)
        assert loser.result(timeout=60.0) == (7, 7)

    assert current_version(db) == 7


def test_two_concurrent_migrations_apply_the_ladder_once(tmp_path):
    """`BEGIN EXCLUSIVE` serializes them (§6). Two operators running `fleet migrate-db` at the same
    time — or one running it twice under a flaky terminal — must not both rebuild the tables: the
    second rebuild would run against the already-migrated shape and fail loudly at best."""
    db = _v6_database(tmp_path / "fleet.db", up_to=6)
    start = threading.Barrier(2)

    def run() -> tuple[int, int]:
        start.wait(timeout=10.0)
        return migrate(db)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(f.result(timeout=60.0) for f in [pool.submit(run), pool.submit(run)])

    assert outcomes == [(6, LATEST_VERSION), (LATEST_VERSION, LATEST_VERSION)], (
        f"the ladder was not applied exactly once: {outcomes}"
    )
    assert current_version(db) == LATEST_VERSION
    assert _query(db, "SELECT COUNT(*) FROM repos")[0][0] == 2


# --------------------------------------------------------------------------------------
# THE test: data survives 7 -> 8
# --------------------------------------------------------------------------------------


def _v7_with_live_reservations(path: Path) -> Path:
    """A v7 database mid-run: money spent, money held, and a held expiry — the pre-v8 aggregate.

    This is the state the 7 → 8 step has to lift without losing a cent. The two ledgers carry a
    scalar `reserved_usd` and one `reservation_expires_at` between them and nothing about WHOSE
    dollars they are, which is exactly what v8 exists to fix.
    """
    _v6_database(path, up_to=7)
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute(
            "UPDATE budget_ledger SET spent_usd = 12.5, reserved_usd = 7.5, "
            "reservation_expires_at = ? WHERE run_id = ?",
            (EXPIRY, RUN),
        )
        conn.execute(
            "UPDATE repo_ledger SET spent_usd = 3.25, reserved_usd = 7.5, "
            "reservation_expires_at = ? WHERE run_id = ? AND repo_id = ?",
            (EXPIRY, RUN, "repo-a"),
        )
    finally:
        conn.close()
    return path


def test_every_recorded_spend_survives_the_seven_to_eight_step(tmp_path):
    """A v7 database's ledgers come through 7 → 8 with byte-identical numbers.

    Why: a 250-repo run is days of LLM spend, and the ONE thing a schema change to the ledger
    must never do is move money. This rung is additive — a new table and one `INSERT … SELECT`
    into it — so `spent_usd`, `reserved_usd` and `max_usd` are asserted value-for-value before
    and after, not merely "still present".
    """
    db = _v7_with_live_reservations(tmp_path / "fleet.db")
    before_run = _query(db, "SELECT spent_usd, reserved_usd, max_usd FROM budget_ledger")
    before_repo = _query(
        db, "SELECT repo_id, spent_usd, reserved_usd, max_usd, revalidation_usd FROM repo_ledger"
    )
    assert before_run == [(12.5, 7.5, 500.0)], "the fixture must actually hold money"

    # This rung only, same reasoning as the 6 -> 7 test above: a bare `migrate(db)` would run
    # past 8 to LATEST_VERSION once a later rung exists, and stop naming which step this test is
    # actually about.
    assert migrate(db, steps=_STEPS_THROUGH(8)) == (7, 8)
    assert current_version(db) == 8

    assert _query(db, "SELECT spent_usd, reserved_usd, max_usd FROM budget_ledger") == before_run
    assert _query(
        db, "SELECT repo_id, spent_usd, reserved_usd, max_usd, revalidation_usd FROM repo_ledger"
    ) == before_repo
    for table, expected in (("runs", 1), ("repos", 2), ("phases", 2), ("attempts", 1)):
        assert _query(
            db, f"SELECT COUNT(*) FROM {table}"  # noqa: S608
        )[0][0] == expected, table


def test_the_pre_v8_aggregate_is_adopted_as_exactly_one_reservation(tmp_path):
    """The held-but-unattributed `reserved_usd` becomes ONE row of exactly that amount.

    Why: this is the only choice that can neither under- nor double-count. Leaving it
    unattributed keeps the ratchet v8 exists to remove — the money is held forever because no
    reaper can name its owner. Zeroing it frees dollars that live workers are still spending, so
    the run under-counts what is committed and overspends. Adopting it as a single row per
    `repo_ledger` row means the reaper's `SUM(amount_usd)` over that repo can never exceed what
    the ledger holds (no over-release) and no dollar is described twice (no double-count), while
    the ledger columns themselves are not touched at all.
    """
    db = _v7_with_live_reservations(tmp_path / "fleet.db")
    migrate(db)

    adopted = _query(
        db,
        "SELECT reservation_id, repo_id, phase, lease_fence, amount_usd, state, expires_at "
        "  FROM reservations",
    )
    assert adopted == [
        (f"{v008_reservations.LEGACY_ID_PREFIX}:{RUN}:repo-a", "repo-a", None, None,
         7.5, "HELD", EXPIRY)
    ]
    # The adopted amount is the aggregate itself — not a share of it, and not a second copy.
    held = _query(db, "SELECT SUM(amount_usd) FROM reservations WHERE state = 'HELD'")[0][0]
    assert held == _query(db, "SELECT reserved_usd FROM repo_ledger")[0][0]
    # No owner is invented: v7 recorded none, and a fabricated phase would make the reaper bump
    # the fence of a lease that never held the money.
    assert _query(db, "SELECT COUNT(*) FROM reservations WHERE phase IS NOT NULL") == [(0,)]


def test_a_v7_ledger_holding_nothing_adopts_nothing(tmp_path):
    """`reserved_usd = 0` mints no row. Why: a zero-dollar HELD reservation is a hold that the
    reaper would later "release", writing an update for money that was never held and churning
    `updated_at` on a ledger nobody touched."""
    db = _v6_database(tmp_path / "fleet.db", up_to=7)
    assert _query(db, "SELECT reserved_usd FROM repo_ledger") == [(0.0,)]

    migrate(db)

    assert _query(db, "SELECT COUNT(*) FROM reservations") == [(0,)]


def test_the_reservations_table_a_migration_builds_matches_a_fresh_one(tmp_path):
    """Migrated-at-8 and fresh-at-8 must be the same shape, or "user_version = 8" names two
    schemas and every statement written against one is a runtime error on the other."""
    migrated = _v7_with_live_reservations(tmp_path / "old.db")
    migrate(migrated)
    fresh = _fresh_baseline(tmp_path / "new.db")

    assert _table_shape(migrated, "reservations") == _table_shape(fresh, "reservations")
    for db in (migrated, fresh):
        indexes = {
            row[0] for row in _query(
                db, "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='reservations'"
            )
        }
        assert v008_reservations.INDEX in indexes, f"the reaper's only scan is missing in {db}"


def _repos_ddl_before_v11() -> str:
    """`repos`'s live `CREATE TABLE`, with exactly the one column this rung adds stripped back
    out — derived from the real baseline rather than hand-copied, so the fixture cannot drift
    from the REST of `repos`'s shape the way a fully hand-typed `CREATE TABLE` could."""
    live = _support.baseline_table_ddl(SCHEMA_SQL)["repos"]
    marker = "migrated_test_count INTEGER, "
    assert marker in live, "v011's own column definition text moved; update this fixture"
    return live.replace(marker, "", 1)


def _v10_database_without_migrated_test_count(path: Path) -> Path:
    """A database GENUINELY at `user_version = 10`, `repos` missing `migrated_test_count` —
    the shape a real database already at v10 before this rung shipped actually has.

    `_v6_database(..., up_to=10)` does NOT produce this shape: `repos` is on 6 → 7's `_REBUILD`
    list, and that rebuild sources its target DDL from the *live* `state/schema.sql`
    (`v011_migrated_test_count`'s own docstring explains why), so replaying the ladder with
    TODAY's code gives `repos.migrated_test_count` four rungs early, at v7, NULL by its declared
    absence of a DEFAULT. Both shapes are real — a database migrated fully from before v7 using
    today's code, and a database that has genuinely sat at v10 since before this rung existed —
    and this fixture is the second one, built by hand because the ladder itself cannot produce it
    anymore now that this rung has landed.
    """
    _fresh_baseline(path)
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DROP TABLE repos")
        conn.execute(_repos_ddl_before_v11())
        for repo_id, name, radius in (("repo-a", "alpha", 7), ("repo-b", "beta", 2)):
            conn.execute(
                "INSERT INTO repos (repo_id, name, url, blast_radius, updated_at) "
                "VALUES (?,?,?,?,?)",
                (repo_id, name, f"git@x/{name}.git", radius, NOW),
            )
        conn.execute("PRAGMA user_version = 10")
    finally:
        conn.close()
    return path


def test_the_ten_to_eleven_step_adds_migrated_test_count_as_null_not_zero(tmp_path):
    """§12.11's `(repo, baseline, migrated)` report needs a durable place for the `migrated` half
    (`docs/CRITERIA_PLAN.md` §11 gap 2). `repo-a`/`repo-b` pre-exist this rung with no test count
    ever measured, so the back-fill MUST be NULL — 0 is a real "zero test targets" measurement and
    conflating the two is exactly the trap `baseline_test_count`'s own pre-7 back-fill note warns
    about, just for the column one over."""
    db = _v10_database_without_migrated_test_count(tmp_path / "fleet.db")
    assert current_version(db) == 10
    assert "migrated_test_count" not in {
        row[1] for row in _query(db, "PRAGMA table_info('repos')")
    }

    before, after = migrate(db, steps=_STEPS_THROUGH(11))

    assert (before, after) == (10, 11)
    assert current_version(db) == 11
    rows = _query(db, "SELECT repo_id, migrated_test_count FROM repos ORDER BY repo_id")
    assert rows == [("repo-a", None), ("repo-b", None)], "pre-existing rows must back-fill to NULL"


def test_a_full_ladder_replay_from_v6_gets_the_same_column_four_rungs_early(tmp_path):
    """The OTHER real path: a database migrated fully from before v7 using TODAY's code gets
    `repos.migrated_test_count` at v7 already (6 → 7's rebuild reads the live `schema.sql`), and
    this rung's own guard must recognise that and do nothing rather than raise "duplicate column"
    — see `v011_migrated_test_count`'s docstring for why both paths are real."""
    db = _v6_database(tmp_path / "fleet.db", up_to=7)
    assert current_version(db) == 7
    rows = _query(db, "SELECT repo_id, migrated_test_count FROM repos ORDER BY repo_id")
    assert rows == [("repo-a", None), ("repo-b", None)], "the rebuild's own DEFAULT-less NULL"

    before, after = migrate(db, steps=_STEPS_THROUGH(11))

    assert (before, after) == (7, 11)
    assert current_version(db) == 11
    assert _query(
        db, "SELECT repo_id, migrated_test_count FROM repos ORDER BY repo_id"
    ) == [("repo-a", None), ("repo-b", None)], "the guard must not touch a column already there"


def test_the_migrated_test_count_column_a_migration_builds_matches_a_fresh_one(tmp_path):
    """Migrated-at-11 and fresh-at-11 must be the same shape (name, type, notnull, default) —
    the same structural-identity guarantee `test_migrated_tables_match_a_freshly_created_v7_
    baseline` proves for the whole 6 → 7 rebuild, narrowed to this rung's one `ADD COLUMN`."""
    migrated = _v6_database(tmp_path / "old.db", up_to=6)
    migrate(migrated)
    fresh = _fresh_baseline(tmp_path / "new.db")

    assert _table_shape(migrated, "repos") == _table_shape(fresh, "repos")


def test_migrating_a_current_v11_database_is_idempotent_for_migrated_test_count(tmp_path):
    """`fleet migrate-db` run twice on an already-current database must not touch a column it
    already added — the general no-op guarantee `test_migrating_an_already_current_database_is_a_
    no_op` proves for the whole schema, re-asserted here for the specific value this rung writes."""
    db = _v6_database(tmp_path / "fleet.db", up_to=11)
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        conn.execute("UPDATE repos SET migrated_test_count = 5 WHERE repo_id = 'repo-a'")
    finally:
        conn.close()

    assert migrate(db) == (LATEST_VERSION, LATEST_VERSION)

    assert _query(
        db, "SELECT repo_id, migrated_test_count FROM repos ORDER BY repo_id"
    ) == [("repo-a", 5), ("repo-b", None)]


# --------------------------------------------------------------------------------------
# an early rung, end to end
# --------------------------------------------------------------------------------------


def test_the_first_rung_renames_columns_without_touching_rows(tmp_path):
    """§6, 1 → 2: "every pre-existing row is a repo node, so the `DEFAULT 'REPO'` back-fills the
    whole table correctly and no data is rewritten". A rename that lost rows would silently empty
    the dependency graph, and an empty graph looks exactly like a fleet with no dependencies."""
    db = tmp_path / "v1.db"
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        conn.execute("CREATE TABLE edges (edge_id INTEGER PRIMARY KEY, src_repo_id TEXT NOT NULL, "
                     "dst_repo_id TEXT, kind TEXT NOT NULL)")
        conn.execute("CREATE TABLE wave_members (run_id TEXT NOT NULL, wave_index INTEGER, "
                     "repo_id TEXT NOT NULL)")
        conn.execute("CREATE TABLE tasks (task_id TEXT PRIMARY KEY, kind TEXT NOT NULL)")
        conn.execute("INSERT INTO edges VALUES (1, 'repo-a', 'repo-b', 'RUNTIME_DEP')")
        conn.execute("INSERT INTO wave_members VALUES ('run-1', 0, 'repo-a')")
        conn.execute("INSERT INTO tasks VALUES ('task-1', 'MIGRATE')")
        conn.execute("PRAGMA user_version = 1")
    finally:
        conn.close()

    assert migrate(db, steps=STEPS[:1]) == (1, 2)

    assert _query(db, "SELECT src_id, dst_id, src_kind, dst_kind, retargeted_from_repo_id "
                      "FROM edges") == [("repo-a", "repo-b", "REPO", "REPO", None)]
    assert _query(db, "SELECT node_id, node_kind FROM wave_members") == [("repo-a", "REPO")]
    assert _query(db, "SELECT task_id, contract_id FROM tasks") == [("task-1", None)]


# --------------------------------------------------------------------------------------
# SPEC §12 item 45 — "no code state is persisted outside Git" (ADR-0024)
#
# Two of the criterion's mechanical checks: (i) schema — no `mutations` table, no column whose
# name matches the forbidden-pattern regex, and the five git-SHA-shaped columns that DO exist
# each resolve to a real commit; (ii) content — no persisted TEXT/BLOB value anywhere contains a
# unified-diff hunk header. The forbidden-name regex below is quoted VERBATIM from `docs/SPEC.md`
# §12 item 45's dated correction — this file makes that hand-validation mechanical, it does not
# re-derive the pattern.
# --------------------------------------------------------------------------------------

_FORBIDDEN_COLUMN_RE: Final = re.compile(
    r"(pre|post)_tree_sha|tree_sha|patch_sha256|patch_path|patch_blob|diff_blob|(^|_)diff(_|$)|"
    r"rollback_log"
)

#: The five columns SPEC's dated correction names as the git-SHA-shaped ones that legitimately
#: exist — pointers INTO git (ADR-0024), never a copy of its content.
_SHA_COLUMNS: Final[tuple[tuple[str, str], ...]] = (
    ("repos", "head_sha"),
    ("phases", "pre_commit_sha"),
    ("phases", "post_commit_sha"),
    ("tasks", "pre_commit_sha"),
    ("attempts", "commit_sha"),
)

_HUNK_HEADER_LIKE: Final = "%@@ -%+%@@%"


def _all_columns(path: Path) -> list[tuple[str, str, str]]:
    """Every (table, column, declared_type) SQLite tracks, across every table `PRAGMA table_list`
    reports — SQLite's own internal tables (`sqlite_schema` et al.) excluded. The one mechanical
    walk both the forbidden-name sweep and the TEXT/BLOB content scan are built from, so the
    schema is never enumerated twice."""
    triples: list[tuple[str, str, str]] = []
    for row in _query(path, "PRAGMA table_list"):
        schema, table = row[0], row[1]
        if schema != "main" or table.startswith("sqlite_"):
            continue
        for col in _query(path, f"PRAGMA table_info('{table}')"):
            triples.append((table, col[1], col[2]))
    return triples


def _all_text_blob_columns(path: Path) -> list[tuple[str, str]]:
    """(table, column) pairs whose declared type is TEXT or BLOB — what §12.45(ii)'s content scan
    walks. Derived from `_all_columns` rather than a second `PRAGMA` walk."""
    return [(table, column) for table, column, decl in _all_columns(path)
            if decl.upper() in ("TEXT", "BLOB")]


def test_schema_has_no_mutations_table_and_no_forbidden_column_name(tmp_path):
    """§12.45(i), first half: `PRAGMA table_list` contains no `mutations` table, and across every
    table `PRAGMA table_info` yields no column whose name matches the forbidden-pattern regex —
    the mechanical version of the check SPEC's 2026-08-27 correction validated by hand."""
    db = _fresh_baseline(tmp_path / "fleet.db")

    tables = {row[1] for row in _query(db, "PRAGMA table_list")
              if row[0] == "main" and not row[1].startswith("sqlite_")}
    assert "mutations" not in tables

    for table, column, _decl_type in _all_columns(db):
        assert not _FORBIDDEN_COLUMN_RE.search(column), (
            f"{table}.{column} matches the forbidden shadow-VCS column pattern"
        )


def test_the_forbidden_column_regex_is_validated_both_directions():
    """Rule 12: applying a regex without self-validating it proves nothing. This is the
    mechanical form of the double-direction check SPEC's dated correction already did by hand —
    the retired draft regex's bare `blob`/`diff` alternatives false-matched
    `repos.largest_blob_bytes` and `collisions.blob_shas`, and the replacement must not repeat
    that while still catching every one of the four synthetic names the correction names."""
    for near_miss in ("largest_blob_bytes", "blob_shas"):
        assert not _FORBIDDEN_COLUMN_RE.search(near_miss), near_miss

    for forbidden in ("pre_tree_sha", "patch_path", "rollback_log", "diff_blob"):
        assert _FORBIDDEN_COLUMN_RE.search(forbidden), forbidden


def test_the_near_miss_columns_are_real_schema_columns(tmp_path):
    """Confirms the previous test's near-misses are not straw men: `repos.largest_blob_bytes` and
    `collisions.blob_shas` are real, currently-shipped columns (SPEC names them explicitly as the
    ones that must never be renamed or dropped to satisfy the retired regex)."""
    db = _fresh_baseline(tmp_path / "fleet.db")
    real_columns = {(table, column) for table, column, _decl in _all_columns(db)}
    assert ("repos", "largest_blob_bytes") in real_columns
    assert ("collisions", "blob_shas") in real_columns


def _git(cwd: Path, *args: str) -> str:
    """A real `git`, with every ambient config source cut off, so a developer's `~/.gitconfig`
    cannot change what these assertions mean (mirrors `tests/test_reentry_evidence.py`'s `_git`;
    duplicated rather than imported — this codebase does not import fixtures across test files)."""
    result = subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={
            "GIT_AUTHOR_NAME": "Fleet Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Fleet Fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "HOME": str(cwd),
            "PATH": "/usr/bin:/bin:/usr/local/bin",
        },
    )
    return result.stdout.strip()


def _throwaway_git_repo(path: Path) -> list[str]:
    """A real repo with three commits on its default branch. Returns their SHAs, oldest first."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "--initial-branch=main", ".")
    shas = []
    for i in range(3):
        (path / f"file{i}.txt").write_text(f"content {i}\n")
        _git(path, "add", "--all")
        _git(path, "commit", "-m", f"commit {i}")
        shas.append(_git(path, "rev-parse", "HEAD"))
    return shas


def _sha_fixture(tmp_path: Path) -> tuple[Path, Path]:
    """A fresh `state/fleet.db` whose five §12.45(i) SHA columns are seeded with real commits from
    a throwaway git repo. Not a genuine pipeline run — §12.45(i)'s SHA-resolves-to-a-commit
    property doesn't need one, and sub-clauses 2-5 (the sibling task) cover that separately.
    Returns (db_path, repo_path)."""
    repo = tmp_path / "throwaway-repo"
    shas = _throwaway_git_repo(repo)
    db = _fresh_baseline(tmp_path / "fleet.db")
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO runs (run_id, started_at, config_sha256, harness_version) "
            "VALUES (?,?,?,?)",
            (RUN, NOW, "cfg", "1.4.2"),
        )
        conn.execute(
            "INSERT INTO repos (repo_id, name, url, head_sha, updated_at) VALUES (?,?,?,?,?)",
            ("repo-a", "acme/widget", "https://example.invalid/widget.git", shas[0], NOW),
        )
        conn.execute(
            "INSERT INTO phases (run_id, repo_id, phase, pre_commit_sha, post_commit_sha, "
            "updated_at) VALUES (?,?,?,?,?,?)",
            (RUN, "repo-a", 2, shas[0], shas[1], NOW),
        )
        conn.execute(
            "INSERT INTO tasks (task_id, run_id, repo_id, phase, kind, dest_path, "
            "pre_commit_sha, created_at) VALUES (?,?,?,?,?,?,?,?)",
            ("task-a", RUN, "repo-a", 2, "MIGRATE", "java/acme/widget", shas[1], NOW),
        )
        conn.execute(
            "INSERT INTO attempts (attempt_id, run_id, repo_id, task_id, phase, attempt, "
            "command, exit_code, commit_sha, started_at, finished_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("attempt-a", RUN, "repo-a", "task-a", 2, 1, '["true"]', 0, shas[2], NOW, NOW),
        )
    finally:
        conn.close()
    return db, repo


def test_the_five_named_sha_columns_all_resolve_to_real_commits(tmp_path):
    """§12.45(i), second half: `repos.head_sha`, `phases.pre_commit_sha`/`post_commit_sha`,
    `tasks.pre_commit_sha` and `attempts.commit_sha` are pointers INTO git (ADR-0024), never a
    copy of its content — so every non-NULL value must resolve to a real commit on the repo's
    branch, asserted with `git cat-file -e <sha>^{commit}`."""
    db, repo = _sha_fixture(tmp_path)

    verified: set[tuple[str, str]] = set()
    for table, column in _SHA_COLUMNS:
        values = {
            row[0]
            for row in _query(
                db,
                f'SELECT DISTINCT "{column}" FROM "{table}" '  # noqa: S608
                f'WHERE "{column}" IS NOT NULL',
            )
        }
        assert values, f"{table}.{column} has no non-NULL value in the fixture"
        for sha in values:
            result = subprocess.run(  # noqa: S603
                ["git", "cat-file", "-e", f"{sha}^{{commit}}"],  # noqa: S607
                cwd=repo,
                check=False,
                capture_output=True,
            )
            assert result.returncode == 0, f"{table}.{column} = {sha} is not a real commit"
        verified.add((table, column))

    assert verified == set(_SHA_COLUMNS), "not every named column was actually exercised"


def test_no_persisted_text_or_blob_value_contains_a_diff_hunk_header(tmp_path):
    """§12.45(ii): after a fixture run, no persisted value anywhere in `state/fleet.db` contains a
    unified-diff hunk header — a diff smuggled into a payload, checkpoint, or error field would be
    exactly the shadow-VCS anti-pattern ADR-0024 forbids, just relocated to a TEXT column instead
    of a dedicated `mutations` table."""
    db, _repo = _sha_fixture(tmp_path)
    columns = _all_text_blob_columns(db)
    assert columns, "the column walk returned nothing — that is broken, not clean"

    # Rule 12: prove the scan is a real discriminator BEFORE trusting a zero count on real data.
    poisoned = tmp_path / "poisoned.db"
    shutil.copyfile(db, poisoned)
    conn = sqlite3.connect(poisoned, isolation_level=None)
    try:
        conn.execute(
            "UPDATE phases SET last_error = ? WHERE run_id = ? AND repo_id = ? AND phase = ?",
            ("@@ -1,4 +1,4 @@\n unrelated context\n", RUN, "repo-a", 2),
        )
    finally:
        conn.close()
    poisoned_count = _query(
        poisoned,
        'SELECT COUNT(*) FROM "phases" WHERE "last_error" LIKE ?',
        (_HUNK_HEADER_LIKE,),
    )[0][0]
    assert poisoned_count > 0, "the scan failed to catch a synthetic hunk header planted in it"

    for table, column in columns:
        count = _query(
            db,
            f'SELECT COUNT(*) FROM "{table}" WHERE "{column}" LIKE ?',  # noqa: S608
            (_HUNK_HEADER_LIKE,),
        )[0][0]
        assert count == 0, f"{table}.{column} contains a diff hunk header"
