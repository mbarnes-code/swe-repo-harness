"""6 → 7 — logical keys, leases, and fail-closed ledgers (SPEC §6, §5 `SCHEMA_VERSION = 7`).

**This is one migration step; every change §6 lists lands in it.** Three groups:

1. **Additive `ALTER`s with declared defaults**, applied first so that every back-fill below has a
   column to write into. The defaults are the load-bearing part: §6 says the pre-existing rows get
   "a fence of `0`, an unheld lease, and a `heartbeat_ttl_seconds` of 300", which `fleet resume`
   step 4 then reconciles against git.

2. **Back-fills that re-derive rather than guess.**
   * `edges.edge_key` is a pure function of columns the row already carries, so it is
     deterministic and re-runnable.
   * `waves.max_usd` is re-derived from the live membership (§11.2: the ceiling is
     `wave_max_cost_usd_per_repo × COUNT(wave_members)`), so a resumed wave enforces the same
     ceiling the halt was measured against.
   * `phases.status = 'FAILED'` becomes `'REQUIRES_HUMAN_INTERVENTION'` **before** the rebuild, or
     the new CHECK — exactly the seven §5.1 `RepoStatus` members — rejects every pre-7 exhausted
     row.
   * `repos.baseline_ok` back-fills to **NULL, not to 1**: no pre-7 run measured a native
     baseline, so `baseline_test_count = 0` means "unknown", not "this repo had no tests".
   * `runs.config_digests` back-fills to `'{}'` — "no per-section baseline was recorded", never
     "every section matches".
   * `llm_cache` rows keyed before `prompt_template_version` **age out** rather than being
     re-keyed, exactly as at 4 → 5.

3. **The table-rebuild path**, under `PRAGMA foreign_keys = OFF` for its whole duration, because
   SQLite cannot `ALTER` a CHECK, a UNIQUE, a PRIMARY KEY, or a foreign key. Its target shapes are
   read from `state/schema.sql` — the v7 baseline itself — so a migrated database and a fresh one
   cannot drift; see `_support.baseline_table_ddl`. `phases.scc_id` is **re-derived, not cast**: an
   int SCC id has no textual preimage, so any surviving integer becomes NULL and the run's
   `findings` are re-derived by `fleet sequence --refresh`.

The whole index set is dropped and re-created from the baseline afterwards, which is what installs
`ix_tasks_claimable`, `ix_phases_lease`, `ix_events_seq` and `ix_llm_cache_lru` and what makes the
indexes of a migrated database identical to a fresh one's rather than a union of every historical
definition. The runner performs `PRAGMA foreign_key_check` and sets `user_version` (§6).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Final

from fleet.migrations import _support
from fleet.state.db import SCHEMA_PATH

VERSION: Final = 7

#: §9 `budgets.wave_max_cost_usd_per_repo`. §6 writes this back-fill with a bind parameter but the
#: step signature it fixes (`upgrade(conn)`) carries no channel for one, so the shipped default is
#: used. Re-deriving from live membership with the default is strictly better than the `0.0`
#: placeholder — and `fleet sequence --refresh` recomputes it from the operator's own config.
WAVE_MAX_COST_USD_PER_REPO: Final = 8.0

_ALTERS: Final[tuple[str, ...]] = (
    # ---- logical keys (§5 EdgeKey / SccId) ----
    "ALTER TABLE edges  ADD COLUMN edge_key TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE waves  ADD COLUMN wave_started_at TEXT",
    "ALTER TABLE waves  ADD COLUMN synthetic INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE waves  ADD COLUMN max_usd REAL NOT NULL DEFAULT 0.0",
    # ---- native baseline (§9) and per-section config digests (§10) ----
    "ALTER TABLE repos  ADD COLUMN baseline_ok INTEGER",  # NULL = never measured
    "ALTER TABLE repos  ADD COLUMN baseline_test_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE runs   ADD COLUMN config_digests TEXT NOT NULL DEFAULT '{}'",
    # ---- claim / lease / fence ----
    "ALTER TABLE tasks  ADD COLUMN status TEXT NOT NULL DEFAULT 'PENDING'",
    "ALTER TABLE tasks  ADD COLUMN claimed_by TEXT",
    "ALTER TABLE tasks  ADD COLUMN lease_expires_at TEXT",
    "ALTER TABLE tasks  ADD COLUMN fence_token INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE tasks  ADD COLUMN pre_commit_sha TEXT",
    "ALTER TABLE tasks  ADD COLUMN max_attempts INTEGER NOT NULL DEFAULT 3",
    "ALTER TABLE phases ADD COLUMN max_attempts INTEGER NOT NULL DEFAULT 3",
    "ALTER TABLE phases ADD COLUMN heartbeat_ttl_seconds INTEGER NOT NULL DEFAULT 300",
    "ALTER TABLE phases ADD COLUMN lease_owner TEXT",  # replaces owner_pid, dropped in the rebuild
    "ALTER TABLE phases ADD COLUMN lease_fence INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE phases ADD COLUMN lease_expires_at TEXT",
    # ---- evidence ----
    "ALTER TABLE attempts ADD COLUMN revalidation_round INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE attempts ADD COLUMN integration_ref TEXT",
    "ALTER TABLE attempts ADD COLUMN command_sha256 TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE attempts ADD COLUMN retry_ordinal INTEGER NOT NULL DEFAULT 0",
    # ---- cache and ledgers ----
    "ALTER TABLE llm_cache ADD COLUMN prompt_template_version INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE llm_cache ADD COLUMN last_hit_at TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE budget_ledger ADD COLUMN reservation_expires_at TEXT",
    "ALTER TABLE repo_ledger   ADD COLUMN reserved_usd REAL NOT NULL DEFAULT 0.0",
    "ALTER TABLE repo_ledger   ADD COLUMN reservation_expires_at TEXT",
    "ALTER TABLE stubs ADD COLUMN stub_id TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE stubs ADD COLUMN max_revalidation_rounds INTEGER NOT NULL DEFAULT 2",
    "ALTER TABLE stubs ADD COLUMN state_changed_at TEXT NOT NULL DEFAULT ''",
)

#: `edges.edge_key` — the recipe is NOT restated here. `fleet_edge_key` is §5's `edge_key_for`
#: (`_support._edge_key`) and the argument list is `EDGE_KEY_COLUMNS` itself, so the back-fill,
#: the model and §6's UNIQUE tuple cannot drift into three different keys again. `run_id` is not
#: an argument: it partitions rows, it does not identify an edge (§5, §11.6).
#: S608: the interpolated names are `EDGE_KEY_COLUMNS`, a module-level tuple of literals — never
#: user input — and interpolating them is the point: the column list cannot drift from the recipe.
_BACKFILL_EDGE_KEY: Final = (
    f"UPDATE edges SET edge_key = fleet_edge_key({', '.join(_support.EDGE_KEY_COLUMNS)})"  # noqa: S608
)

#: Re-derive, do not guess (§6). The wave ceiling SCALES with membership (§11.2).
_BACKFILL_WAVE_MAX_USD: Final = """
UPDATE waves SET max_usd = ? * (SELECT COUNT(*) FROM wave_members m
                                 WHERE m.run_id = waves.run_id
                                   AND m.wave_index = waves.wave_index)
"""

_BACKFILLS: Final[tuple[str, ...]] = (
    "UPDATE attempts SET command_sha256 = fleet_command_sha256(command)",
    "UPDATE llm_cache SET last_hit_at = created_at WHERE last_hit_at = ''",
    # One stub is shared by every consumer row of it, so the id is keyed on the stub's own
    # identity — (run, provider, coordinate) — never on the consumer that happens to hold the row.
    "UPDATE stubs SET stub_id = fleet_sha256_nul(run_id, provider_repo_id, stub_coord_key) "
    "WHERE stub_id = ''",
    "UPDATE stubs SET state_changed_at = COALESCE(resolved_at, created_at) "
    "WHERE state_changed_at = ''",
    # BEFORE the phases rebuild: 'FAILED' is not a §5.1 RepoStatus member and the new CHECK
    # would reject every pre-7 exhausted row (CLAUDE.md Rule 11 — the terminal state is RHI).
    "UPDATE phases SET status = 'REQUIRES_HUMAN_INTERVENTION' WHERE status = 'FAILED'",
)

#: §6's rebuild list, in its order. Every entry is a table SQLite cannot ALTER into shape.
_REBUILD: Final[tuple[str, ...]] = (
    "edges",  # the (run_id, edge_key) UNIQUE, the dst_kind-bearing natural UNIQUE, the default
    "repos",  # CHECK (baseline_test_count >= 0)
    "waves",  # CHECK (max_usd >= 0.0)
    "phases",  # DROP owner_pid; status domain; attempts >= 0; scc_id -> TEXT; ttl/max CHECKs
    "attempts",  # the new UNIQUE tuple and the placeholder default on command_sha256
    "tasks",  # the status CHECK and the ladder-length CHECK against max_attempts
    "rejected_approaches",  # attempt CHECK 1..3 -> >= 1 (the 2 -> 3 step baked the ladder in)
    "stubs",  # CHECK (revalidation_round <= max_revalidation_rounds)
    "budget_ledger",  # the two fail-closed CHECKs
    "repo_ledger",  # the two fail-closed CHECKs
    "events",  # add REFERENCES runs(run_id) ON DELETE CASCADE
)

#: An int SCC id has no textual preimage, so it is re-derived — not cast — and NULL until
#: `fleet sequence --refresh` recomputes it over the member set (§6).
_COLUMN_EXPRS: Final[dict[str, dict[str, str]]] = {
    "phases": {"scc_id": "CASE WHEN scc_id LIKE 'scc:%' THEN scc_id ELSE NULL END"},
}


def upgrade(conn: sqlite3.Connection) -> None:
    _support.assert_foreign_keys_off(conn, context="v007_logical_keys")
    _support.register_sql_functions(conn)

    for statement in _ALTERS:
        conn.execute(statement)

    conn.execute(_BACKFILL_EDGE_KEY)
    conn.execute(_BACKFILL_WAVE_MAX_USD, (WAVE_MAX_COST_USD_PER_REPO,))
    for statement in _BACKFILLS:
        conn.execute(statement)

    schema_sql = Path(SCHEMA_PATH).read_text(encoding="utf-8")
    table_ddl = _support.baseline_table_ddl(schema_sql)
    for table in _REBUILD:
        _support.rebuild_table(
            conn,
            table,
            target_ddl=table_ddl[table],
            column_exprs=_COLUMN_EXPRS.get(table),
        )

    # The rebuilds took their tables' indexes down with them; re-create the WHOLE baseline set so
    # the result is the v7 index set exactly, not a union with every historical definition.
    #
    # `schema.sql` is the CURRENT baseline, so it also declares indexes on tables a LATER rung
    # introduces (`reservations` at v8). Those are skipped here rather than pinned to a frozen
    # list: a frozen list is the second source of truth this step exists to avoid, and the rung
    # that adds a table is the rung that owns its index.
    for name, ddl in _support.baseline_index_ddl(schema_sql):
        if not _support.table_exists(conn, _support.index_target_table(ddl)):
            continue
        conn.execute(f'DROP INDEX IF EXISTS "{name}"')
        conn.execute(ddl)
