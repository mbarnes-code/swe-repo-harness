-- fleet state schema — the v11 BASELINE for a FRESH database (SPEC §6).
--
-- This file is DATA, not a startup side effect. It is applied by `fleet migrate-db` (§10) and by
-- nothing else. §6 "Migration policy" is normative: `CREATE TABLE IF NOT EXISTS` creates a
-- *missing* schema and never executes an ALTER or a rebuild, so a startup path that "applies
-- schema.sql idempotently" silently leaves an old database at its old shape. Workers read
-- `PRAGMA user_version` at startup and REFUSE TO START if it differs from the compiled-in
-- version; they never run this file.
--
-- A brand-new database gets this file and lands directly at user_version = 11. The ordered
-- `src/fleet/migrations/vNNN_*.py` ladder (1→2 … 10→11) exists only for databases that already
-- hold data; it is never replayed against a fresh one.
--
-- ---------------------------------------------------------------------------------------------
-- PRAGMAs. Only the PERSISTENT ones live here, because this file runs once:
--   * journal_mode = WAL   — stored in the database header, survives close (§6, ADR-0004)
--   * user_version = 11    — stored in the database header (§5 SCHEMA_VERSION)
--
-- The rest are PER-CONNECTION and reset to their defaults on every new handle. `state/db.py`
-- MUST issue these on EVERY connection it opens (read and write alike); setting them here would
-- be a no-op for every connection but the one that ran the migration:
--   PRAGMA foreign_keys      = ON;      -- OFF by default in SQLite; every CASCADE below is inert
--                                       --   without it. Must be OFF for the whole of a
--                                       --   table-rebuild migration, then re-asserted (§6).
--   PRAGMA busy_timeout      = 30000;   -- backstop only, never the arbitrator (ADR-0004 exit)
--   PRAGMA synchronous       = NORMAL;  -- the correct pairing with WAL
--   PRAGMA wal_autocheckpoint = 1000;
--
-- `state/fleet.db` MUST live on a local filesystem: WAL needs shared memory, which an NFS/SMB
-- mount does not provide, and the file corrupts rather than degrades (§6).
--
-- ADR-0016/ADR-0024: SQLite is authoritative for ORCHESTRATION state only. Git is authoritative
-- for CODE state — there is deliberately no `mutations` table, and no column below may hold a
-- tree SHA, a diff, or a patch blob.
-- ---------------------------------------------------------------------------------------------

PRAGMA journal_mode = WAL;

-- ---------- identity ----------
CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,               -- UUID4
    started_at    TEXT NOT NULL,                  -- ISO-8601 UTC
    finished_at   TEXT,
    config_sha256 TEXT NOT NULL,                  -- hash of merged config/*.yaml; DERIVED as the
                                                  --   hash of the per-section digests below, so the
                                                  --   two can never disagree about what the config
                                                  --   was. Still the one-line resume identity check
    config_digests TEXT NOT NULL DEFAULT '{}',    -- JSON {section: sha256}, one entry per §10 drift
                                                  --   section. A resume diffs section by section,
                                                  --   so `--accept-drift budgets` accepts exactly
                                                  --   one and writes one `ConfigDrift` finding
                                                  --   naming it. A single opaque hash can only say
                                                  --   "something moved", which forces the operator
                                                  --   to accept every co-edited change (§10)
    monorepo_branch TEXT NOT NULL DEFAULT 'integration',
    harness_version TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS repos (
    repo_id        TEXT PRIMARY KEY,
    name           TEXT NOT NULL UNIQUE,
    url            TEXT NOT NULL,
    default_branch TEXT NOT NULL DEFAULT 'main',
    default_branch_source TEXT NOT NULL DEFAULT 'symbolic-ref',
    head_sha       TEXT,
    ecosystems     TEXT NOT NULL DEFAULT '[]',    -- JSON array
    primary_coord_key TEXT,                       -- FK-by-value into coordinates.coord_key
    dest_path      TEXT,
    kind           TEXT NOT NULL DEFAULT 'unknown',
    framework      TEXT,
    owner_hint     TEXT,
    size_bytes     INTEGER NOT NULL DEFAULT 0,
    commit_count   INTEGER NOT NULL DEFAULT 0,
    last_commit_at TEXT,
    cloned_at      TEXT,
    -- git preflight (§3.1 step 1)
    is_shallow          INTEGER NOT NULL DEFAULT 0,
    submodule_count     INTEGER NOT NULL DEFAULT 0,
    has_lfs             INTEGER NOT NULL DEFAULT 0,
    lfs_object_bytes    INTEGER NOT NULL DEFAULT 0,
    largest_blob_bytes  INTEGER NOT NULL DEFAULT 0,
    preflight_ok        INTEGER,                  -- NULL = not run, 0 = gated
    -- native baseline (`preflight.baseline_build`, §9): the pre-migration truth this run is
    -- measured against. `baseline_test_count` is the repo's NATIVE test-target/test-case count,
    -- and §12.11 refuses a migration that lands fewer, so a `filegroup` with no tests cannot
    -- pass as a green build.
    baseline_ok         INTEGER,                  -- NULL = not run, 0 = red => SKIPPED+BaselineRed
    baseline_test_count INTEGER NOT NULL DEFAULT 0
                        CHECK (baseline_test_count >= 0),
    -- migrated test count (§12.11, added v10 -> v11): the OTHER half of the `(repo, baseline,
    -- migrated)` report — a real `bazel query 'tests(//dest/...)'` count over the migrated
    -- package. NULL = never measured (no run's `bazel query` step has reached this repo yet);
    -- 0 is a real, meaningful "zero test targets after the move" measurement and is NOT the same
    -- as NULL, unlike `baseline_test_count` above, which repeats `baseline_ok`'s NULL/0/1
    -- tri-state one column over instead of drawing its own.
    migrated_test_count INTEGER,
    -- graph-derived (§3.5)
    blast_radius   INTEGER NOT NULL DEFAULT 0,
    updated_at     TEXT NOT NULL
);

-- ---------- Phase 1 evidence ----------
CREATE TABLE IF NOT EXISTS manifests (            -- ADR-0004 calls this the per-manifest table
    manifest_id     INTEGER PRIMARY KEY,
    repo_id         TEXT NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
    path            TEXT NOT NULL,
    ecosystem       TEXT NOT NULL,
    adapter         TEXT NOT NULL,
    adapter_version INTEGER NOT NULL DEFAULT 1,
    sha256          TEXT NOT NULL,
    publishes_key   TEXT,
    dependency_count INTEGER NOT NULL DEFAULT 0,
    low_confidence  INTEGER NOT NULL DEFAULT 0,
    parse_error     TEXT,
    parsed_at       TEXT NOT NULL,
    UNIQUE (repo_id, path)
);

CREATE TABLE IF NOT EXISTS coordinates (
    coord_key    TEXT PRIMARY KEY,                -- '{ecosystem}:{group}:{name}', case-folded
    ecosystem    TEXT NOT NULL,
    grp          TEXT NOT NULL DEFAULT '',
    name         TEXT NOT NULL,
    owner_repo_id TEXT REFERENCES repos(repo_id) ON DELETE SET NULL,  -- NULL => external
    version      TEXT,                            -- the OWNING repo's own published version
                                                   --   (§37 Blocker B). NULL until that repo is
                                                   --   scanned, or forever for Go (module
                                                   --   versions are VCS tags, not in-repo
                                                   --   content). NEVER a dependent's declared
                                                   --   range — only an owned=True write may set
                                                   --   this (`src/fleet/cli.py` `_scan_rows`).
    first_seen_at TEXT NOT NULL
);

-- Nodes are (kind, id), not always repos (ADR-0019). `src_repo_id`/`dst_repo_id` are GONE.
CREATE TABLE IF NOT EXISTS edges (
    edge_id       INTEGER PRIMARY KEY,             -- rowid: LOCAL, reassigned on rebuild. Never a
                                                   --   cross-table reference (§5 `EdgeKey`)
    edge_key      TEXT NOT NULL                    -- THE logical PK: sha256 over the UNIQUE tuple
                  CHECK (length(edge_key) = 64),   --   below MINUS `run_id`, NUL-joined. §5's
                                                   --   `edge_key_for()` IS the recipe — it is not
                                                   --   restated here, and the v007 back-fill calls
                                                   --   it rather than re-deriving it. Stable
                                                   --   across rebuilds AND across runs
    run_id        TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    src_kind      TEXT NOT NULL DEFAULT 'REPO' CHECK (src_kind IN ('REPO','CONTRACT')),
    src_id        TEXT NOT NULL,                        -- repos.repo_id | contracts.contract_id
    dst_kind      TEXT NOT NULL DEFAULT 'REPO' CHECK (dst_kind IN ('REPO','CONTRACT')),
    dst_id        TEXT,                                 -- NULL + dst_kind='REPO' => external
    dst_coord_key TEXT NOT NULL,                        -- Coordinate.key when dst_kind='REPO';
                                                        --   the contract_id when dst_kind='CONTRACT'
    dst_candidate_repo_ids TEXT NOT NULL DEFAULT '[]',  -- JSON array; >1 => ambiguous
    retargeted_from_repo_id TEXT,                       -- pre-hoist dst repo (§3.1 5b viii);
                                                        --   the rollback record for a hoist
    kind          TEXT NOT NULL,
    version_spec  TEXT,
    base_confidence REAL NOT NULL CHECK (base_confidence BETWEEN 0.0 AND 1.0),
    confidence    REAL NOT NULL CHECK (confidence BETWEEN 0.0 AND 1.0),
    confidence_factors TEXT NOT NULL DEFAULT '{}',      -- JSON {modifier_name: float}
    ambiguous     INTEGER NOT NULL DEFAULT 0,
    ordering_suppressed INTEGER NOT NULL DEFAULT 0,     -- broken cycle feedback edge (§3.1 6d)
    evidence_path TEXT NOT NULL,
    evidence_line INTEGER NOT NULL DEFAULT -1,          -- -1, not NULL: it is part of the PK below
    detected_at   TEXT NOT NULL,
    CHECK (src_kind || ':' || src_id <> dst_kind || ':' || COALESCE(dst_id, '')),
    CHECK (dst_kind = 'REPO' OR dst_id IS NOT NULL),    -- a contract edge always resolves
    CHECK (src_kind = 'REPO' OR dst_kind = 'CONTRACT'), -- contracts never point back at a repo
    CHECK ((kind IN ('CONTRACT_IMPL','CONTRACT_CONSUME')) = (dst_kind = 'CONTRACT')),
    -- IDEMPOTENCY KEY: re-running inference cannot duplicate a row (§11.7). It is `run_id` +
    -- exactly `EDGE_KEY_COLUMNS` (§5), in hash order, and `edge_key` is the sha256 of the tail:
    -- `run_id` PARTITIONS rows (this table is per-run and CASCADEs with its run) but does not
    -- IDENTIFY an edge. Hashing it would give one edge two keys in two runs, which breaks §11.6
    -- — `CycleFinding.broken_edge_keys` is a `run_digest` input and §12.21 requires two runs from
    -- a clean database to agree byte for byte. `dst_kind` is in BOTH tuples: `dst_coord_key`
    -- holds a `Coordinate.key` or a `contract_id` depending on it, and only the CHECK above ties
    -- `kind` to it — a key must not inherit another constraint's premise.
    UNIQUE (run_id, edge_key),
    UNIQUE (run_id, src_kind, src_id, dst_kind, dst_coord_key, kind, evidence_path, evidence_line)
);
-- NOTE: `src_id`/`dst_id` carry NO foreign key. SQLite has no conditional FK, and the referent
-- table depends on the sibling `*_kind` column. Referential integrity is enforced by
-- `state/repository.py` on write and re-asserted by a startup consistency check; §12.29 makes a
-- dangling node id a test failure rather than a silent orphan.
-- There is no `cycles` / `pr_drafts` table: `CycleFinding` and `PullRequestDraft` persist as
-- `findings.payload` / `checkpoints.payload` JSON. Every edge reference inside those payloads is
-- an `edge_key` string and every SCC reference an `scc_id` string ('scc:<16-hex>'); a `list[int]`
-- of rowids in a payload is a schema bug (§5, §12) and is rejected on validate-on-load.

CREATE TABLE IF NOT EXISTS contracts (            -- §3.1 step 5b; the non-repo DAG nodes
    run_id        TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    contract_id   TEXT NOT NULL,                  -- '{kind_lower}:{identifier}', case-folded
    kind          TEXT NOT NULL
                  CHECK (kind IN ('PROTO','OPENAPI','AVRO','THRIFT','SHARED_LIB')),
    identifier    TEXT NOT NULL,                  -- proto package / namespace / OpenAPI slug
    owning_repo_id TEXT REFERENCES repos(repo_id) ON DELETE SET NULL,
    source_paths  TEXT NOT NULL DEFAULT '[]',     -- JSON [{repo_id, path, blob_sha}]
    generated_paths TEXT NOT NULL DEFAULT '[]',   -- JSON [{repo_id, path}]; DELETED, not migrated
    consumer_repo_ids TEXT NOT NULL DEFAULT '[]', -- JSON array; denormalized, `edges` is the truth
    extractable   INTEGER NOT NULL DEFAULT 0,
    extraction_confidence REAL NOT NULL DEFAULT 0.0
                  CHECK (extraction_confidence BETWEEN 0.0 AND 1.0),
    confidence_factors TEXT NOT NULL DEFAULT '{}',-- JSON; the score is reconstructible from it
    content_sha256 TEXT NOT NULL DEFAULT '',      -- sha256 of the sorted DISTINCT source blob SHAs
    hoist_target_path TEXT,                       -- layout() of this node (§3.3)
    status        TEXT NOT NULL DEFAULT 'DETECTED',
    status_detail TEXT NOT NULL DEFAULT '',
    detected_at   TEXT NOT NULL,
    -- IDEMPOTENCY KEY (§11.7): nine repos vendoring one proto package => exactly one row.
    -- A graph rebuild DELETEs and re-derives this table, but rows whose `status` is 'HOISTED' or
    -- 'MIGRATED' — and their `hoist_target_path` — MUST survive that cycle: the hoist is already
    -- committed in git and named by an open PR (§3.1 6c-H).
    PRIMARY KEY (run_id, contract_id),
    CHECK (extractable = 0 OR (hoist_target_path IS NOT NULL AND owning_repo_id IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS symbols (
    symbol_id     INTEGER PRIMARY KEY,
    run_id        TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    repo_id       TEXT NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
    fqn           TEXT NOT NULL,
    kind          TEXT NOT NULL,
    path          TEXT NOT NULL,
    line          INTEGER NOT NULL,
    language      TEXT NOT NULL,
    is_definition INTEGER NOT NULL,
    exported      INTEGER NOT NULL DEFAULT 0,
    -- IDEMPOTENCY KEY: a re-indexed file cannot duplicate rows (§11.7)
    UNIQUE (run_id, repo_id, path, line, fqn, kind)
);

CREATE TABLE IF NOT EXISTS file_blobs (        -- §3.1 step 1's ls-tree listing, captured once per
                                                -- repo at preflight and never persisted before v10:
                                                -- the path/blob-SHA capture §9(d) and §12.27's
                                                -- FILE_PATH leg (D114) are both blocked on it.
    run_id    TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    repo_id   TEXT NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
    path      TEXT NOT NULL,                   -- repo-relative, POSIX, as `ls-tree` reports it
    blob_sha  TEXT NOT NULL,
    -- IDEMPOTENCY KEY (§11.7): a re-run of the same run_id's capture step DELETEs and re-inserts
    -- (mirroring `contracts`/`symbols`), so this PK is never actually raced.
    PRIMARY KEY (run_id, repo_id, path)
);

-- ---------- Phase 1 output ----------
CREATE TABLE IF NOT EXISTS waves (
    run_id      TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    wave_index  INTEGER NOT NULL,
    computed_at TEXT NOT NULL,
    wave_started_at TEXT,                         -- first admission into this wave. PERSISTED, so
                                                  --   `wave_max_wallclock_s` (§3.6) is cumulative
                                                  --   across resumes rather than restarted by one
    synthetic   INTEGER NOT NULL DEFAULT 0,       -- 1 => this wave was APPENDED to an
                                                  --   already-sequenced plan (§3.5), not produced
                                                  --   by the sequencing pass; wave_index is above
                                                  --   every wave the plan then held (first
                                                  --   appended layer = max(waves) + 1). Records
                                                  --   HOW the wave was allocated, not why: the
                                                  --   flag names no cause
    max_usd     REAL NOT NULL DEFAULT 0.0         -- §11.2: the wave ceiling is DERIVED —
                CHECK (max_usd >= 0.0),           --   `budgets.wave_max_cost_usd_per_repo` ×
                                                  --   COUNT(wave_members) — and FROZEN here at the
                                                  --   wave's first admission. Persisted because a
                                                  --   resume must enforce the same number the halt
                                                  --   was measured against. The wave's SPEND is not
                                                  --   duplicated here — it is SUM(repo_ledger
                                                  --   .spent_usd) over this wave's REPO members
    PRIMARY KEY (run_id, wave_index)
);

CREATE TABLE IF NOT EXISTS wave_members (         -- a wave holds repo AND contract nodes
    run_id     TEXT NOT NULL,
    wave_index INTEGER NOT NULL,
    node_kind  TEXT NOT NULL DEFAULT 'REPO' CHECK (node_kind IN ('REPO','CONTRACT')),
    node_id    TEXT NOT NULL,                    -- repos.repo_id | contracts.contract_id
    PRIMARY KEY (run_id, node_kind, node_id),
    FOREIGN KEY (run_id, wave_index) REFERENCES waves(run_id, wave_index) ON DELETE CASCADE
    -- No FK on node_id, for the same conditional-FK reason as `edges`; enforced in
    -- state/repository.py and re-asserted by the §3.1 exit condition (c) counts.
);

CREATE TABLE IF NOT EXISTS findings (             -- cycles, no-manifest, preflight, conflicts
    finding_id INTEGER PRIMARY KEY,
    run_id     TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    repo_id    TEXT REFERENCES repos(repo_id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,                     -- FREE TEXT, deliberately: findings are raised by
                                                  --   every layer and a Python enum here would make
                                                  --   a new one a schema migration. Shipped:
                                                  -- 'CycleDetected' | 'no-manifest'
                                                  -- | 'PreflightFailed' | 'VersionConflict'
                                                  -- | 'WeakEdge' | 'OversizeBlob'
                                                  -- | 'SymbolBudgetExceeded' | 'CoarseTarget'
                                                  -- | 'ContractHoistOverride' | 'ContractNotShared'
                                                  -- | 'HoistBrokeOwner' | 'HoistRollbackDemotion'
                                                  -- | 'HoistRollbackRefused'
                                                  -- | 'StubDegraded' -- state/repository.py,
                                                  --     stub_degrade_transform, ADR-0124: the audit
                                                  --     write paired with the SUCCEEDED->DEGRADED
                                                  --     CAS write in the same transaction
                                                  -- | 'BaselineRed' | 'RuleConflict'
                                                  -- | 'RuleOscillation' | 'UnmergedDependency'
                                                  -- | 'OperatorQuarantine' | 'ConfigDrift'
                                                  -- | 'CapabilityDrift'    -- §13 row 37, the
                                                  --     answering rung was below the promised
                                                  --     one. repo_id IS NULL, because a drift is
                                                  --     a property of a TARGET and not of any
                                                  --     one repository
                                                  -- | 'BackendUnavailable' -- §13 row 40, every
                                                  --     target for a tier spent. Written by
                                                  --     PhaseRunner immediately before the
                                                  --     exit-8 halt
                                                  -- | ...
                                                  -- CAVEAT. "Shipped" above means DECLARED, not
                                                  --   emitted. Several names in this list are
                                                  --   READ by Python that nothing writes
                                                  --   ('BaselineRed', 'PreflightFailed',
                                                  --   'RuleConflict') and two have no Python at
                                                  --   all ('WeakEdge' -- as of 2026-09-06, ALL FOUR
                                                  --   of the Contract/Hoist kinds now have live
                                                  --   writers ('ContractNotShared',
                                                  --   'ContractHoistOverride',
                                                  --   'HoistRollbackDemotion', 'HoistBrokeOwner'
                                                  --   all left this group; see below).
                                                  --   The two annotated above each have a
                                                  --   live INSERT behind them, in
                                                  --   orchestrator/findings.py. THREE more are
                                                  --   built only as in-process dataclasses that
                                                  --   no writer ever sees, so nothing emits
                                                  --   them: 'VersionConflict'
                                                  --   (bazel/generators.py:414), 'CoarseTarget'
                                                  --   (graph/cycles.py:923) and 'RuleOscillation'
                                                  --   (rewrite/pipeline.py:263) — GraphFinding
                                                  --   and RewriteFinding are imported by no
                                                  --   module that holds an INSERT INTO findings.
                                                  --   'UnmergedDependency' left that group
                                                  --   2026-09-02 (D101 Half A / ADR-0112): it now
                                                  --   has a live INSERT in cli.py's
                                                  --   _apply_stub_reconcile, one per §13 row 45
                                                  --   held-for-merge consumer, repo_id the
                                                  --   consumer's.
                                                  --   'ContractNotShared' left that group
                                                  --   2026-09-06 (§12.31 case (i) Leg A, round VI
                                                  --   task 55, ADR-0120): it now has a live
                                                  --   INSERT in cli.py's
                                                  --   _persist_contract_not_shared_findings, one
                                                  --   per not-shared-after-retarget contract
                                                  --   6c-H rejects, repo_id the owner's.
                                                  --   'ContractHoistOverride' left that group the
                                                  --   same day (§12.31 Leg E, round VI task 58):
                                                  --   it now has a live INSERT in cli.py's
                                                  --   _persist_contract_hoist_override_findings,
                                                  --   one per currently-FORBIDDEN contract,
                                                  --   repo_id the owner's. 'HoistRollbackDemotion'
                                                  --   left that group 2026-09-06 (§12.31/D111 Leg
                                                  --   D slice 1, round VI task 65, ADR-0122): it
                                                  --   now has a live upsert in cli.py, function
                                                  --   _write_hoist_rollback_demotion_findings, one
                                                  --   per blast-set repo `demote_to_floor`
                                                  --   actually demoted, repo_id that demoted repo.
                                                  --   The SAME task also introduced a NEW kind,
                                                  --   'HoistRollbackRefused' (ADR-0122 Decision 6,
                                                  --   the downstream-merge refusal; repo_id NULL,
                                                  --   since the refusal is a property of the whole
                                                  --   blast set, not one repo), emitted from
                                                  --   cli.py, function
                                                  --   _write_hoist_rollback_refused_finding -- see
                                                  --   the DECLARED list above, where it is added
                                                  --   alongside 'HoistRollbackDemotion'.
                                                  --   'HoistBrokeOwner' left the no-Python group
                                                  --   the same day (§12.31 case (ii) Leg C2, round
                                                  --   VI task 66, ADR-0123): it now has a live
                                                  --   INSERT in the cli.py _BuildSink.__call__
                                                  --   method, one per Phase 3 build failure
                                                  --   attributed to a watched hoist target path,
                                                  --   repo_id the failing repo.
                                                  --   No Contract/Hoist kind remains no-Python.
                                                  --   The REST of the DECLARED list is emitted
                                                  --   from cli.py, several through a VARIABLE
                                                  --   `kind` column ('OversizeBlob',
                                                  --   'SymbolBudgetExceeded'), so a literal grep
                                                  --   of cli.py under-reports it.
                                                  -- EMITTED BUT NEVER DECLARED — the direction the
                                                  --   CAVEAT above did not contemplate. Each of
                                                  --   these has a live writer in src/ and was
                                                  --   absent from the list above. From a sweep of
                                                  --   every `INSERT INTO findings` in src/:
                                                  -- | 'PhaseDemoted'  -- state/repository.py,
                                                  --     §11.5 step 5, one row per demoted phase
                                                  -- | 'OperatorReopened' -- state/repository.py,
                                                  --     reopen_to_pending, ADR-0125/§12.14: the
                                                  --     audit write paired with the RHI->PENDING
                                                  --     CAS write `fleet retry` performs, in the
                                                  --     same transaction
                                                  -- | 'EmptyRepo' | 'SubmodulePresent'
                                                  --     -- workers/clone.py, through the scan
                                                  --     persist and the §3.1 gate
                                                  -- | 'FileTooLarge:<path>'
                                                  -- | 'ParseFailed:<path>'
                                                  --     -- workers/symbolindex.py. PREFIXED, so a
                                                  --     `kind = ?` equality match never sees them
                                                  -- | 'TransformPreparationFailed'
                                                  -- | 'BuildPreparationFailed'
                                                  -- | 'DependencyResolutionFailed'
                                                  -- | 'CoordinateRenderFailed'
                                                  -- | 'BuildFileGenerationFailed'
                                                  -- | 'VerifyPreparationFailed'
                                                  --     -- cli.py `_abandon_repo`, which pairs
                                                  --     each with REQUIRES_HUMAN_INTERVENTION
                                                  -- | 'EcosystemAdapterUnavailable'
                                                  -- | 'ModuleLockForeignRegistry'
                                                  -- | 'ContractBindingUnavailable'
                                                  --     -- cli.py `_note_finding`; §13 row 31,
                                                  --     §12.34 Clause B (ADR-0119)
                                                  -- | 'PullRequest' | 'VerificationReport'
                                                  -- | 'OperatorAbort' | 'StubAbandoned'
                                                  -- | 'WaveBudgetRaised' | 'RunBudgetRaised'
                                                  --     -- cli.py, one dedicated writer each
                                                  -- | 'AnchoringGuardOff' -- cli.py's
                                                  --     _TransformSink.__call__, the
                                                  --     --no-anchoring-guard repeat-applied case
    severity   TEXT NOT NULL DEFAULT 'warn',
    fingerprint TEXT NOT NULL,                    -- sha256 of the semantic identity of the finding
    payload    TEXT NOT NULL,                     -- Pydantic dump_json, post-redaction
    created_at TEXT NOT NULL
);
-- IDEMPOTENCY KEY: a re-run re-raises the same finding without duplicating it (§11.7).
-- An expression index, because repo_id is nullable (fleet-level findings have no repo).
CREATE UNIQUE INDEX IF NOT EXISTS ux_findings_ident
    ON findings (run_id, IFNULL(repo_id, ''), kind, fingerprint);

CREATE TABLE IF NOT EXISTS collisions (           -- §3.1 step 8; detected BEFORE any transform
    collision_id INTEGER PRIMARY KEY,
    run_id     TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,                     -- COORDINATE | CONTRACT | DEST_PATH
                                                  --   | FILE_PATH | DEP_VERSION
    key        TEXT NOT NULL,                     -- coord_key / contract_id / dest / monorepo path
    repo_ids   TEXT NOT NULL,                     -- JSON array, sorted
    blob_shas  TEXT NOT NULL DEFAULT '[]',        -- FILE_PATH: identical => safe dedupe
    severity   TEXT NOT NULL DEFAULT 'warn',
    resolution TEXT,                              -- NULL + severity='error' fails `fleet sequence`
    detected_at TEXT NOT NULL,
    UNIQUE (run_id, kind, key)                    -- IDEMPOTENCY KEY (§11.7)
);

CREATE TABLE IF NOT EXISTS stubs (                -- the persistence of §5 `StubRecord`; one row
                                                  --   per (stub, consumer, round)
    stub_id     TEXT NOT NULL,                    -- StubRecord.stub_id; SHARED by every consumer
                                                  --   row of one stub, so `consumer_repo_ids` is
                                                  --   the aggregate of the rows, not a copy
    run_id      TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    repo_id     TEXT NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,  -- DEGRADED dependent
    stub_coord_key TEXT NOT NULL,                 -- StubRecord.coord_key: the abandoned repo's
                                                  --   published coordinate
    consumer_repo_id TEXT NOT NULL                -- explicit alias of repo_id; the two role names
        REFERENCES repos(repo_id) ON DELETE CASCADE,  --   are what make the direction readable
    provider_repo_id TEXT NOT NULL                -- the abandoned repo. FK to `repos`: a contract
        REFERENCES repos(repo_id) ON DELETE CASCADE,  --   node is NEVER a provider (§3.5, ADR-0019)
    pinned_version TEXT,                          -- NULL => build-time failure stub, never silent
    bazel_label    TEXT NOT NULL,                 -- //third_party/stubs/... while ACTIVE
    -- ---- lifecycle (§3.5.1) ----
    state       TEXT NOT NULL DEFAULT 'ACTIVE'
                CHECK (state IN ('ACTIVE','SUPERSEDED','RESOLVED','ABANDONED')),
    stub_fidelity TEXT NOT NULL                   -- StubRecord.fidelity
                CHECK (stub_fidelity IN ('PUBLISHED_ARTIFACT','EMPTY_FAILING')),
    revalidation_round  INTEGER NOT NULL DEFAULT 0,  -- 0 while ACTIVE; N when the Nth round ran.
                                                  --   StubRecord.rounds_spent is MAX() over these
    max_revalidation_rounds INTEGER NOT NULL DEFAULT 2,  -- captured per stub, so a config change
                                                  --   cannot retroactively exhaust a live stub
    state_changed_at TEXT NOT NULL,               -- StubRecord.state_changed_at; every transition
    revalidation_task_id TEXT REFERENCES tasks(task_id) ON DELETE SET NULL,  -- the REVALIDATE task
    resolved_at        TEXT,                      -- set on entry to SUPERSEDED/RESOLVED/ABANDONED
    resolved_by_run_id TEXT REFERENCES runs(run_id) ON DELETE SET NULL,  -- which run retired it
    abandon_reason     TEXT,                      -- 'STUB_DIVERGED' | 'ROUNDS_EXHAUSTED'
                                                  --   | 'BUDGET_EXHAUSTED' | 'END_OF_RUN'
                                                  --   | 'OPERATOR'; NULL unless state='ABANDONED'
    created_at  TEXT NOT NULL,
    CHECK (consumer_repo_id = repo_id),
    CHECK (pinned_version IS NOT NULL OR stub_fidelity = 'EMPTY_FAILING'),
    CHECK ((state = 'ABANDONED') = (abandon_reason IS NOT NULL)),
    CHECK (state = 'ACTIVE' OR resolved_at IS NOT NULL),
    CHECK (revalidation_round <= max_revalidation_rounds),  -- StubRecord._fidelity_matches_pin
    PRIMARY KEY (run_id, repo_id, stub_coord_key, revalidation_round)  -- IDEMPOTENCY KEY (§11.7):
);   -- a re-emitted stub inserts at the NEXT round rather than resurrecting a terminal row

-- ---------- execution ----------
CREATE TABLE IF NOT EXISTS phases (
    run_id            TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    repo_id           TEXT NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
    phase             INTEGER NOT NULL CHECK (phase BETWEEN 1 AND 4),
    -- DOMAIN = exactly the seven §5.1 `RepoStatus` members, no more and no fewer. The enum is
    -- the source of truth: pre-7 this list carried 'FAILED', which is NOT a member (an exhausted
    -- repo goes to 'REQUIRES_HUMAN_INTERVENTION', CLAUDE.md Rule 11), and omitted 'BLOCKED',
    -- which IS one (§3.5 `blocked_by` propagation) — so SQLite rejected a status the model
    -- considers legal and admitted one the model cannot parse.
    -- NOTHING is excluded. `PhaseRecord.status` is typed `RepoStatus` with no narrowing, and
    -- `transition()` is THE single gate for every status write at both levels, so every member
    -- is reachable on a phase row: 'SKIPPED' arrives via `fleet quarantine` (§10), which sends a
    -- live PENDING/BLOCKED repo there mid-run. Narrowing the column would make a legal
    -- transition unwritable — the same class of bug in the other direction.
    -- `tests/test_schema_sql.py::test_phases_status_domain_is_exactly_the_repostatus_enum`
    -- derives this list FROM the enum, so the two cannot silently drift again.
    status            TEXT NOT NULL DEFAULT 'PENDING'
                      CHECK (status IN ('PENDING','RUNNING','SUCCEEDED','BLOCKED','DEGRADED',
                                        'REQUIRES_HUMAN_INTERVENTION','SKIPPED')),
    -- NO upper bound: this counter only ever increments, and the increment that RECORDS a
    -- terminal failure must never be the one that raises. The ceiling is `max_attempts`, and
    -- the increment that reaches it transitions `status` to 'REQUIRES_HUMAN_INTERVENTION'
    -- (CLAUDE.md Rule 11) in the SAME statement — it does not violate a CHECK.
    attempts          INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts      INTEGER NOT NULL DEFAULT 3  -- 3 is §5 `MAX_ATTEMPTS`, ADR-0014's DEFAULT
                      CHECK (max_attempts BETWEEN 1 AND 8),  -- ladder length — a fallback, not the
                                                  --   governor: the value copied from the owning
                                                  --   task on admission is what actually bounds
                                                  --   this phase, so a resume reads the ladder
                                                  --   THIS phase ran under. NOT NULL WITH A
                                                  --   DEFAULT for the same reason as
                                                  --   heartbeat_ttl_seconds below: nullable makes
                                                  --   a fresh SQL row NULL, which §5
                                                  --   `PhaseRecord.max_attempts` (default
                                                  --   MAX_ATTEMPTS, ge=1, le=8) cannot validate.
                                                  --   The BETWEEN mirrors that ge/le rail.
    transient_retries INTEGER NOT NULL DEFAULT 0,
    failure_class     TEXT,
    last_error        TEXT,
    blocked_by        TEXT NOT NULL DEFAULT '[]', -- JSON array of repo_id; set-union, reversible
    stubbed_deps      TEXT NOT NULL DEFAULT '[]', -- JSON array of coord_key; non-empty => DEGRADED
    scc_id            TEXT CHECK (scc_id IS NULL OR scc_id LIKE 'scc:%'),  -- §5 SccId; set iff in
                                                  --   an ATOMIC_WAVE SCC (§3.1 6e). Content-derived
                                                  --   over the member set, never a renumbered int
    pr_url            TEXT,
    -- ---- lease (§11.5). A heartbeat alone cannot invalidate a stolen worktree. ----
    heartbeat_at      TEXT,
    heartbeat_ttl_seconds INTEGER NOT NULL DEFAULT 300  -- captured per phase from orchestrator
                      CHECK (heartbeat_ttl_seconds > 0),  -- .stale_after_s. NOT NULL WITH A DEFAULT:
                                                  --   a nullable column makes a fresh row NULL,
                                                  --   which §5 `PhaseRecord.heartbeat_ttl_seconds`
                                                  --   (default 300, gt=0) cannot validate, and
                                                  --   NULL > interval is never true, so a stale
                                                  --   worker would never be reaped
    lease_owner       TEXT,                       -- THE format, declared here and cited (never
                                                  --   restated) by §5 `PhaseRecord.lease_owner`:
                                                  --     '{host}:{container_id}:{pid}:{boot_uuid}'
                                                  --   A bare pid is NOT an identity: fresh PID
                                                  --   namespaces reuse low pids, so two containers
                                                  --   collide
    lease_fence       INTEGER NOT NULL DEFAULT 0, -- monotonic; bumped on every reclaim
    lease_expires_at  TEXT,                       -- reclaimable only past this instant AND stale
    -- ADR-0024: pointers INTO git, never a copy of git's content. A commit SHA as a reference is
    -- fine; a tree SHA or a diff kept so SQLite could reverse a change is the removed anti-pattern.
    base_ref          TEXT,                       -- rollback anchor, a REAL ref:
                                                  --   refs/fleet/<run_id>/<repo_id>/phase-<n>/base
    pre_commit_sha    TEXT,                       -- commit `base_ref` names (cached)
    post_commit_sha   TEXT,                       -- newest commit this phase produced (cached;
                                                  --   git's Fleet-Phase trailer walk is the truth)
    started_at        TEXT,
    updated_at        TEXT NOT NULL,
    PRIMARY KEY (run_id, repo_id, phase)          -- IDEMPOTENCY KEY (§11.7)
);
-- FENCING (normative). Every mutating statement a worker issues against its own phase row — and
-- every git-affecting side effect it performs — carries `AND lease_fence = ?` with the fence it
-- was granted at claim time. `rowcount == 0` means the lease was reclaimed underneath it: the
-- worker MUST abort immediately, WITHOUT touching git or the worktree, and emit `LeaseStolen`.
-- REAPER (normative). One reaper task runs every 30 s:
--     UPDATE phases SET status='PENDING', lease_owner=NULL, lease_fence=lease_fence+1,
--            heartbeat_at=NULL, lease_expires_at=NULL
--      WHERE status='RUNNING' AND lease_expires_at < :now;
-- one `LeaseExpired` event per reclaimed row. The fence bump is what makes the reclaim safe: the
-- old holder's next write matches zero rows. Without an expiry column a crashed worker leaves the
-- row RUNNING forever and every downstream wave blocks on `blocked_by` for the rest of the run.

-- There is deliberately NO `mutations` table (ADR-0024). A write-ahead journal of tree SHAs,
-- patch blobs, and rollback rows is a shadow version-control system: it duplicates what Git
-- already stores atomically and can therefore disagree with it after a crash. Code state lives
-- in Git — the commit, identified by its `Fleet-Patch-Id` / `Fleet-Task-Id` trailers (§3.2 step
-- 6), IS the record — and the orchestration state that used to sit beside it in `mutations` is
-- folded into `tasks` (identity), `attempts` (per-rung outcome + `commit_sha`/`patch_id`
-- pointers), `phases` (`base_ref`, the rollback anchor), and `events` (ordering via `events.seq`).

CREATE TABLE IF NOT EXISTS llm_cache (            -- content-addressed LLM results (§11.6)
    cache_key   TEXT PRIMARY KEY,                 -- sha256(role|tier|backend|model_id|effort|
                                                  --   context_policy|rejected_approach_digest|
                                                  --   prompt_sha256|prompt_template_version|
                                                  --   response_schema_sha256|adapter_versions).
                                                  --   `harness_version` is NOT a component: it
                                                  --   changes every patch release and would
                                                  --   re-pay for the whole fleet (§5)
    role        TEXT NOT NULL,
    tier        TEXT NOT NULL DEFAULT 'WORKHORSE',  -- ADR-0023; ModelTier
    backend     TEXT NOT NULL DEFAULT 'anthropic',  -- ADR-0023; registered backend name
    model_id    TEXT NOT NULL,                    -- The CONFIGURED id — `target.model_id` out of
                                                  --   config/models.yaml, verbatim — NOT the id
                                                  --   the transport resolved or served it as.
                                                  --   This is a cache-KEY component, and the two
                                                  --   sides are built from different objects: the
                                                  --   READ key from the config string, the WRITE
                                                  --   key from `usage.model_id`. A backend that
                                                  --   reports its own served name makes them
                                                  --   disagree on EVERY call — a permanent, silent
                                                  --   100% miss indistinguishable from a cold
                                                  --   cache, because `attempts.llm_cache_hit`
                                                  --   simply stays 0. So `usage.model_id` MUST
                                                  --   echo `target.model_id`. Surfacing the served
                                                  --   id is a legitimate want, but it needs a
                                                  --   SEPARATE field (or a log line) — never this
                                                  --   one. (tier, backend, model_id) are key
                                                  --   components, not decoration: the cache is
                                                  --   run-unscoped, so without them a failover to
                                                  --   a weaker model poisons every later run
                                                  --   (§13 row 39).
    structured_output_mode TEXT NOT NULL DEFAULT 'JSON_SCHEMA',  -- ADR-0023; §7.7 rung used
    effort      TEXT NOT NULL,                    -- ADR-0075: '' = target declared none. NOT NULL
                                                  --   and no CHECK, so absence needs no migration.
    context_policy TEXT,                          -- ADR-0021; NULL for non-ladder roles
    rejected_approach_digest TEXT NOT NULL        -- sha256 over the signatures RENDERED in prompt
        DEFAULT 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855',  -- sha256(b'')
    prompt_sha256 TEXT NOT NULL,
    prompt_template_version INTEGER NOT NULL DEFAULT 1,  -- THE invalidation knob (§5); bumped by
                                                  --   hand when a role's template changes meaning
    response_schema_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,                  -- redacted before write
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd    REAL NOT NULL DEFAULT 0.0,
    hit_count   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    last_hit_at TEXT NOT NULL                     -- LRU clock. `response_json` is unbounded and the
                                                  --   table is run-unscoped, so without an eviction
                                                  --   key "stale entries age out" names no mechanism
);                                                -- NOT scoped to run_id: cross-run reuse is the point

CREATE TABLE IF NOT EXISTS budget_ledger (        -- durable, fail-closed cost accounting (§11.2)
    run_id       TEXT PRIMARY KEY REFERENCES runs(run_id) ON DELETE CASCADE,
    spent_usd    REAL NOT NULL DEFAULT 0.0,
    reserved_usd REAL NOT NULL DEFAULT 0.0,       -- in-flight reservations, released on completion
    reservation_expires_at TEXT,                  -- DERIVED since v8, never written by hand: the
                                                  --   earliest surviving HELD expiry. WHOSE hold
                                                  --   expired is answered by `reservations`, not
                                                  --   here — see RESERVATION ACCOUNTING below
    max_usd      REAL NOT NULL,
    halted       INTEGER NOT NULL DEFAULT 0,      -- 1 => no further LLM call may be dispatched
    updated_at   TEXT NOT NULL,
    -- "Fail-closed" is a CONSTRAINT, not a convention. Read-then-write lets 12 workers each
    -- reserve $3 against a $497/$500 ledger and all 12 writes succeed.
    CHECK (spent_usd >= 0.0 AND reserved_usd >= 0.0),
    CHECK (spent_usd + reserved_usd <= max_usd)
);
-- RESERVATION (normative). Never SELECT-then-UPDATE. The reservation is one conditional CAS:
--     UPDATE budget_ledger SET reserved_usd = reserved_usd + :amt,
--            reservation_expires_at = <the derivation below>, updated_at = :now
--      WHERE run_id = :run AND halted = 0 AND spent_usd + reserved_usd + :amt <= max_usd;
-- `rowcount == 1` grants it; `rowcount == 0` is a refusal, never a warning. Settlement moves the
-- amount from `reserved_usd` to `spent_usd` in one statement. Since v8 the CAS does not stand
-- alone. The dispatch path is the NESTED pair (a repo dollar and the run dollar it sits inside,
-- §11.2): both its reserve and its settle take a REQUIRED `reservation_id` and write the
-- `reservations` row that names the holder in the SAME transaction as the two `reserved_usd`
-- moves, so no hold is unattributable. WHO holds which dollars — the thing the reaper must read
-- and a scalar column cannot carry — is defined ONCE, under RESERVATION ACCOUNTING below;
-- `reservation_expires_at` is derived there and is not the reaper's input.

CREATE TABLE IF NOT EXISTS repo_ledger (          -- per-repo cost, scaled by blast radius (§3.5)
    run_id     TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    repo_id    TEXT NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
    spent_usd  REAL NOT NULL DEFAULT 0.0,
    max_usd    REAL NOT NULL,
    -- `revalidation` cost class (§3.5.1): stub rework is priced separately so "for free" cannot
    -- be asserted again, and is a SUB-ceiling inside max_usd — it can never raise a repo's total.
    revalidation_usd     REAL NOT NULL DEFAULT 0.0,
    revalidation_max_usd REAL NOT NULL DEFAULT 2.0,   -- stubs.revalidation_max_cost_usd
    revalidation_rounds  INTEGER NOT NULL DEFAULT 0,  -- capped by stubs.max_revalidation_rounds
    reserved_usd REAL NOT NULL DEFAULT 0.0,          -- same CAS discipline as budget_ledger
    reservation_expires_at TEXT,
    updated_at TEXT NOT NULL,
    CHECK (revalidation_usd <= max_usd),
    CHECK (spent_usd >= 0.0 AND reserved_usd >= 0.0),
    CHECK (spent_usd + reserved_usd <= max_usd),     -- the per-repo ceiling, enforced not asserted
    PRIMARY KEY (run_id, repo_id)
);

CREATE TABLE IF NOT EXISTS reservations (         -- WHO holds which dollars, and until when (v8)
    -- Both ledgers above carry a scalar `reserved_usd` and a SINGLE `reservation_expires_at`
    -- that every reserver overwrites. That pair records that money is held; it records nothing
    -- about WHOSE. A reaper reading it can only release the whole aggregate, which zeroes every
    -- live worker's hold alongside the dead one's and lets the run overspend the ceiling it just
    -- under-counted. This table is the missing identity: one row per reservation, so the reaper
    -- subtracts exactly the expired holder's amount and leaves the live ones alone.
    reservation_id TEXT PRIMARY KEY,              -- minted by the reserver; carried to settlement
    run_id       TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    repo_id      TEXT NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
    -- The OWNER, when there is one. NULL/NULL is a reservation held outside a phase lease (a
    -- pre-v8 aggregate lifted by the ladder, or a scan-time dispatch): still reapable by expiry,
    -- just with no fence to bump. The CHECK keeps the pair from going half-set, because a phase
    -- with no fence is an owner the reaper cannot invalidate.
    phase        INTEGER,
    lease_fence  INTEGER,
    amount_usd   REAL NOT NULL CHECK (amount_usd >= 0.0),
    state        TEXT NOT NULL DEFAULT 'HELD'
                 CHECK (state IN ('HELD','SETTLED','EXPIRED')),
    expires_at   TEXT,                            -- NULL => never reaped; the holder is immortal
    created_at   TEXT NOT NULL,
    CHECK ((phase IS NULL) = (lease_fence IS NULL)),
    FOREIGN KEY (run_id, repo_id, phase)
        REFERENCES phases (run_id, repo_id, phase) ON DELETE CASCADE
);
-- RESERVATION ACCOUNTING (normative, v8). `reserved_usd` on both ledgers stays the enforced
-- aggregate — the CAS guard has to be one statement — and this table is the per-holder ledger
-- behind it. Invariant: for every run, SUM(amount_usd) over HELD rows equals the dollars the
-- ledgers hold on their behalf, because a row is inserted and both `reserved_usd` are grown in
-- ONE transaction, and settlement flips the row and shrinks both in ONE transaction.
-- `reservation_expires_at` on both ledger rows is now DERIVED from this table
-- (MIN(expires_at) over the surviving HELD rows), never authoritative: a single column cannot
-- represent N expiries and the last writer's value is not the one the reaper needs.
-- REAPER (normative). In the SAME transaction that bumps `phases.lease_fence`:
--     UPDATE repo_ledger/budget_ledger SET reserved_usd = MAX(reserved_usd - <SUM of the
--            expired HELD rows>, 0.0) ...;
--     UPDATE reservations SET state = 'EXPIRED' WHERE state = 'HELD' AND expires_at < :now;
-- The fence bump is what stops the reaped holder settling a reservation that is no longer held.

CREATE TABLE IF NOT EXISTS tasks (
    task_id      TEXT PRIMARY KEY,                -- UUID4
    run_id       TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    repo_id      TEXT NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
    phase        INTEGER NOT NULL,
    kind         TEXT NOT NULL,
    contract_id  TEXT,                            -- set iff kind='HOIST'; repo_id is the OWNER,
                                                  --   which keeps the repos FK and the cost ledger
    revalidation_key TEXT,                        -- set iff kind='REVALIDATE' (§3.5.1). Format
                                                  --   'r{round}:{sha256(sorted provider_repo_ids)}'
                                                  --   — the provider-set hash is what makes
                                                  --   `batched` coalescing an explicit identity
    dest_path    TEXT NOT NULL,                   -- contracts.hoist_target_path when kind='HOIST'
    target_paths TEXT NOT NULL DEFAULT '[]',
    rule_ids     TEXT NOT NULL DEFAULT '[]',
    -- ---- claim (§11.5). Without these a task has no owner and no atomic hand-off. ----
    status       TEXT NOT NULL DEFAULT 'PENDING'
                 CHECK (status IN ('PENDING','CLAIMED','RUNNING','DONE','FAILED')),
    claimed_by   TEXT,                            -- same identity shape as phases.lease_owner
    lease_expires_at TEXT,
    fence_token  INTEGER NOT NULL DEFAULT 0,      -- bumped on every (re)claim; carried by the owner
    pre_commit_sha TEXT,                          -- tip of migrate/<repo> when THIS task was
                                                  --   admitted: its per-task rollback anchor
                                                  --   (§3.2 step 6), independent of phases.base_ref
    token_budget INTEGER NOT NULL DEFAULT 200000,
    max_attempts INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts BETWEEN 1 AND 8),  -- §5: THE
                                                  --   ceiling for this task; copied onto phases
    ladder       TEXT NOT NULL                    -- ADR-0021: JSON array, one entry per attempt,
        DEFAULT '[null,"EVIDENCE_ONLY","EVIDENCE_PLUS_REJECTED_APPROACHES"]',  -- index 0 is null
    created_at   TEXT NOT NULL,
    CHECK (json_valid(ladder) AND json_array_length(ladder) = max_attempts
           AND json_extract(ladder, '$[0]') IS NULL),
    CHECK ((kind = 'HOIST') = (contract_id IS NOT NULL)),
    CHECK ((kind = 'REVALIDATE') = (revalidation_key IS NOT NULL))
);
-- IDEMPOTENCY KEY: one task per (repo, phase, kind, contract, revalidation round+provider set);
-- re-planning UPSERTs (§11.7). An expression index rather than a table UNIQUE, because one owner
-- may hoist several contracts in the same phase, one consumer may need several revalidation
-- rounds, and both discriminators are nullable for every other TaskKind.
CREATE UNIQUE INDEX IF NOT EXISTS ux_tasks_ident
    ON tasks (run_id, repo_id, phase, kind, IFNULL(contract_id, ''), IFNULL(revalidation_key, ''));
CREATE INDEX IF NOT EXISTS ix_tasks_claimable
    ON tasks (run_id, status, lease_expires_at);
-- CLAIM (normative). There is exactly ONE way to take a task, and it is a single compare-and-swap
-- statement — never SELECT-then-UPDATE, which lets two workers both see PENDING, both write
-- RUNNING, both run `fleet migrate` on the same repo, both commit to `migrate/<repo>`, and both
-- burn that repo's `max_usd`:
--     UPDATE tasks
--        SET status='CLAIMED', claimed_by=:worker, lease_expires_at=:now_plus_ttl,
--            fence_token = fence_token + 1
--      WHERE task_id = (SELECT task_id FROM tasks
--                        WHERE run_id=:run AND status='PENDING' ORDER BY created_at LIMIT 1)
--        AND status = 'PENDING'
--  RETURNING task_id, fence_token;
-- Ownership is `rowcount == 1`. The returned `fence_token` is carried on every subsequent write
-- (`AND fence_token = ?`), so a reclaimed task's old holder matches zero rows. Reclaim mirrors the
-- `phases` reaper: expired CLAIMED/RUNNING rows go back to PENDING with the token bumped.
-- ROLLBACK. A whole-phase rollback resets that phase's `tasks` rows to 'PENDING' and clears their
-- `pre_commit_sha` in the SAME transaction as the `git update-ref` of `phases.base_ref` (§3.2).

CREATE TABLE IF NOT EXISTS attempts (
    attempt_id    TEXT PRIMARY KEY,               -- UUID4
    run_id        TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    repo_id       TEXT NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
    task_id       TEXT REFERENCES tasks(task_id) ON DELETE SET NULL,
    phase         INTEGER NOT NULL,
    attempt       INTEGER NOT NULL CHECK (attempt >= 1),  -- ceiling is tasks.max_attempts, checked
                                                  --   at runtime; never a compiled-in constant (§5)
    revalidation_round INTEGER NOT NULL DEFAULT 0, -- 0 = first pass; N = the Nth round (§3.5.1). In
                                                  --   the uniqueness key below: a REVALIDATE re-run
                                                  --   reuses 1..max_attempts, so without it round 2
                                                  --   overwrites round 1's evidence
    integration_ref TEXT,                         -- the IMMUTABLE snapshot this build ran against,
                                                  --   refs/fleet/integration/<n> (§3.3). A build
                                                  --   attributed to a moving branch name is not
                                                  --   reproducible
    tier          TEXT NOT NULL DEFAULT 'DETERMINISTIC',
    context_policy TEXT,                          -- ADR-0021; NULL iff tier='DETERMINISTIC'
    approach_signature TEXT NOT NULL DEFAULT '',  -- ADR-0021; 64-hex fingerprint, '' when none.
                                                  --   NOT NULL because it joins the idempotency
                                                  --   key below and SQLite treats NULLs as distinct
    command       TEXT NOT NULL DEFAULT '[]',
    command_sha256 TEXT NOT NULL                  -- sha256 of the NORMALIZED argv. This, not the
        DEFAULT 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855',  -- sha256(b'')
                                                  --   variable-length `command` JSON, joins the
                                                  --   uniqueness key: a whitespace change in the
                                                  --   rendering must not mint a duplicate row
    retry_ordinal INTEGER NOT NULL DEFAULT 0,     -- 0 = first execution of this command; N = the
                                                  --   Nth re-execution. In the key, so a re-run
                                                  --   APPENDS its own row instead of colliding
    exit_code     INTEGER,                        -- NULL iff nothing was executed (ANCHORED_REPEAT)
    failure_class TEXT,
    duration_ms   INTEGER NOT NULL DEFAULT 0,
    container_id  TEXT,
    stdout_tail   TEXT NOT NULL DEFAULT '',
    stderr_tail   TEXT NOT NULL DEFAULT '',
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd      REAL NOT NULL DEFAULT 0.0,
    llm_cache_hit INTEGER NOT NULL DEFAULT 0,     -- 1 => served from llm_cache, cost_usd = 0.
                                                  --   The ONLY cache signal: a local backend's
                                                  --   cost_usd is also 0 (ADR-0023, §11.2)
    llm_backend   TEXT,                           -- ADR-0023; NULL for DETERMINISTIC rows
    llm_failovers INTEGER NOT NULL DEFAULT 0,     -- ADR-0023; backend hops spent inside THIS
                                                  --   attempt. Distinct from transient_retries
                                                  --   (within-target) and never increments
                                                  --   phases.attempts (§11.8)
    -- ADR-0024: what remains of the deleted `mutations` journal — two pointers and a flag.
    patch_id      TEXT,                           -- 64-hex content key; == the commit's
                                                  --   `Fleet-Patch-Id` trailer. NOT a uniqueness
                                                  --   constraint: git's trailer is the idempotency
                                                  --   authority (§11.7 rule 3), this is a cache.
    commit_sha    TEXT,                           -- the commit this attempt produced, or the one
                                                  --   the pre-apply guard found already present
    already_applied INTEGER NOT NULL DEFAULT 0,   -- 1 => guard hit; nothing applied or committed
    started_at    TEXT NOT NULL,
    finished_at   TEXT NOT NULL,
    CHECK ((tier = 'DETERMINISTIC') = (context_policy IS NULL)),
    CHECK (approach_signature = '' OR length(approach_signature) = 64),
    CHECK (patch_id IS NULL OR length(patch_id) = 64),        -- ADR-0024
    CHECK (commit_sha IS NULL OR length(commit_sha) = 40),    -- ADR-0024
    CHECK (already_applied IN (0, 1)),
    -- ADR-0021: the ONLY legal way to have executed nothing is an anchoring rejection.
    CHECK (exit_code IS NOT NULL OR failure_class = 'ANCHORED_REPEAT'),
    CHECK ((command = '[]') = (exit_code IS NULL)),   -- mirrors BuildAttempt._executed_iff_command
    -- IDEMPOTENCY KEY: attempts are append-only but a resumed attempt must not double-insert.
    -- `command_sha256` distinguishes the several commands within one ladder rung (§11.7);
    -- `approach_signature` distinguishes a rung's anti-anchoring re-asks from each other (ADR-0021);
    -- `revalidation_round` keeps round N's evidence from overwriting round N-1's (§3.5.1); and
    -- `retry_ordinal` makes a genuine RE-EXECUTION append a row rather than silently collide. That
    -- last one is the fresh-logs guarantee: under `DO NOTHING` on a key without it, a re-executed
    -- command's outcome is discarded and the repair loop is composed from the STALE first log.
    UNIQUE (run_id, repo_id, phase, attempt, revalidation_round, tier, command_sha256,
            approach_signature, retry_ordinal)
);

CREATE TABLE IF NOT EXISTS rejected_approaches (  -- ADR-0021; the ladder's memory, NOT the transcript
    run_id        TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    task_id       TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    approach_signature TEXT NOT NULL CHECK (length(approach_signature) = 64),
    reason        TEXT NOT NULL,                  -- one line, approach level; redacted before write
    failure_class TEXT NOT NULL,
    attempt       INTEGER NOT NULL CHECK (attempt >= 1),  -- NO upper bound. `BETWEEN 1 AND 3`
                                                  --   hard-coded the DEFAULT ladder length into a
                                                  --   type constraint while `tasks.max_attempts`
                                                  --   allows 1..8, so a refutation from rung 4 of a
                                                  --   configured ladder raised instead of being
                                                  --   recorded — the same policy-as-CHECK bug
                                                  --   already removed from `phases.attempts`. The
                                                  --   ceiling is the owning task's `max_attempts`,
                                                  --   checked at runtime (§5)
    tier          TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    -- There is deliberately NO diff/patch/transcript column. A later rung is composed by SELECTing
    -- this table, so a raw prior patch has no path into a prompt (§12.35).
    CHECK (length(reason) BETWEEN 1 AND 280),
    -- IDEMPOTENCY KEY: one row per (task, approach). Re-proposing it is the anchoring signal, not
    -- a second refutation, so the insert is an UPSERT that leaves the first refutation standing.
    PRIMARY KEY (task_id, approach_signature)
);

CREATE TABLE IF NOT EXISTS events (               -- mirror of the JSONL stream, for SQL queries
    event_id  INTEGER PRIMARY KEY,
    run_id    TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,  -- the CASCADE every sibling
                                                  --   table carries; without it `events` outlives
                                                  --   every run and grows without bound
    seq       INTEGER NOT NULL,                   -- monotonic per run; THE ordering key, not `ts`.
                                                  --   ALLOCATED IN-STATEMENT under BEGIN IMMEDIATE:
                                                  --     VALUES (:run, (SELECT COALESCE(MAX(seq),0)
                                                  --       + 1 FROM events WHERE run_id=:run), …)
                                                  --   A read-then-compute MAX(seq)+1 in Python makes
                                                  --   the second concurrent emitter raise
                                                  --   IntegrityError — telemetry killing a worker
    ts        TEXT NOT NULL,                      -- wall clock, for humans only (§11.5)
    repo_id   TEXT,
    phase     INTEGER,
    level     TEXT NOT NULL,
    event     TEXT NOT NULL,
    event_uid TEXT NOT NULL,                      -- uuid4; dedupes a replayed JSONL tail
    payload   TEXT NOT NULL DEFAULT '{}',         -- post-redaction (§11.4)
    UNIQUE (run_id, event_uid),                   -- IDEMPOTENCY KEY (§11.7); the ONLY conflict
                                                  --   target of the insert's ON CONFLICT clause
    UNIQUE (run_id, seq)                          -- upheld by the in-statement allocator above,
                                                  --   NOT by an application-side counter
);
-- Every documented consumer reads events ORDER BY seq, so `seq` gets the index. `ix_events_run_ts`
-- stays for human timeline queries only — `ts` is wall clock and is not an ordering key (§11.5).
CREATE INDEX IF NOT EXISTS ix_events_seq       ON events (run_id, seq);

CREATE TABLE IF NOT EXISTS checkpoints (          -- Pydantic dump_json BLOBs (Constraint 2)
    run_id     TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    repo_id    TEXT NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
    phase      INTEGER NOT NULL,
    model_name TEXT NOT NULL,                     -- Pydantic class, for validate-on-load dispatch
    payload    BLOB NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, repo_id, phase)
);

-- ---------- indexes: these are what make 250-repo edge queries instant ----------
CREATE INDEX IF NOT EXISTS ix_edges_dst_key   ON edges (dst_coord_key, kind);
CREATE INDEX IF NOT EXISTS ix_edges_src       ON edges (run_id, src_kind, src_id, kind);
CREATE INDEX IF NOT EXISTS ix_edges_dst_node  ON edges (run_id, dst_kind, dst_id) WHERE dst_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_edges_conf      ON edges (run_id, confidence);
-- "who consumes this contract" is the hot Phase-1 query once contracts exist; it is an
-- index-only scan of ix_edges_dst_node, NOT a scan of contracts.consumer_repo_ids JSON.
CREATE INDEX IF NOT EXISTS ix_contracts_owner ON contracts (run_id, owning_repo_id)
    WHERE owning_repo_id IS NOT NULL;             -- 6c-H: "which contracts does this SCC own"
CREATE INDEX IF NOT EXISTS ix_contracts_hoist ON contracts (run_id, extractable, extraction_confidence DESC)
    WHERE extractable = 1;                        -- 6c-H candidate enumeration and ranking
CREATE INDEX IF NOT EXISTS ix_contracts_status ON contracts (run_id, status);
CREATE INDEX IF NOT EXISTS ix_contracts_dest  ON contracts (run_id, hoist_target_path)
    WHERE hoist_target_path IS NOT NULL;          -- step-8 DEST_PATH/FILE_PATH audit
CREATE INDEX IF NOT EXISTS ix_coord_owner     ON coordinates (owner_repo_id) WHERE owner_repo_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_manifests_repo  ON manifests (repo_id, ecosystem);
CREATE INDEX IF NOT EXISTS ix_manifests_pub   ON manifests (publishes_key) WHERE publishes_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_symbols_fqn     ON symbols (fqn, is_definition);
CREATE INDEX IF NOT EXISTS ix_symbols_repo    ON symbols (run_id, repo_id, kind);
CREATE INDEX IF NOT EXISTS ix_phases_status   ON phases (run_id, phase, status);
-- The reaper's only scan: it must not walk 250×4 rows every 30 s to find the expired ones.
CREATE INDEX IF NOT EXISTS ix_phases_lease    ON phases (run_id, status, lease_expires_at)
    WHERE status = 'RUNNING';
-- The budget reaper's only scan: it must not walk every reservation of a 250-repo run every 30 s
-- to find the expired holds. `state` sits between `run_id` and `expires_at` because the sweep is
-- always "the HELD rows of THIS run, ordered by expiry".
CREATE INDEX IF NOT EXISTS ix_reservations_expiry ON reservations (run_id, state, expires_at);
CREATE INDEX IF NOT EXISTS ix_llm_cache_lru   ON llm_cache (last_hit_at);   -- `fleet gc` eviction
CREATE INDEX IF NOT EXISTS ix_attempts_repo   ON attempts (run_id, repo_id, phase, attempt);
CREATE INDEX IF NOT EXISTS ix_tasks_repo      ON tasks (run_id, repo_id, phase);
CREATE INDEX IF NOT EXISTS ix_attempts_anchor ON attempts (run_id, approach_signature)
    WHERE approach_signature <> '';
CREATE INDEX IF NOT EXISTS ix_rejected_task   ON rejected_approaches (task_id, attempt);
CREATE INDEX IF NOT EXISTS ix_events_run_ts   ON events (run_id, ts);
CREATE INDEX IF NOT EXISTS ix_events_repo     ON events (run_id, repo_id, event);
CREATE INDEX IF NOT EXISTS ix_wave_members    ON wave_members (run_id, wave_index, node_kind);
CREATE INDEX IF NOT EXISTS ix_edges_ordering  ON edges (run_id, kind, confidence)
    WHERE ordering_suppressed = 0 AND dst_id IS NOT NULL;        -- the sequencer's only scan
-- ADR-0024: no `ix_mutations_open`. `fleet resume` reconciles open work by asking git whether a
-- RUNNING task's commit is on the branch (§11.5 step 4), so the only index it needs is the one
-- that finds RUNNING rows — `phases`' primary key already serves that at 250 repos × 4 phases.
CREATE INDEX IF NOT EXISTS ix_collisions_open ON collisions (run_id, severity)
    WHERE resolution IS NULL;
CREATE INDEX IF NOT EXISTS ix_repos_blast     ON repos (blast_radius DESC);
CREATE INDEX IF NOT EXISTS ix_stubs_repo      ON stubs (run_id, repo_id);
-- The resolution-trigger query (§3.5.1 step 1) runs on every provider transition to SUCCEEDED,
-- so it gets its own covering index rather than scanning the table 250 times.
CREATE INDEX IF NOT EXISTS ix_stubs_provider  ON stubs (run_id, provider_repo_id, state);
CREATE INDEX IF NOT EXISTS ix_stubs_open      ON stubs (run_id, state)
    WHERE state IN ('ACTIVE','SUPERSEDED');   -- the end-of-run reconciliation sweep

-- The baseline lands directly at 10 (§5 SCHEMA_VERSION). Workers refuse to start against any
-- other value; the vNNN ladder is for databases that already hold data, never for this file.
PRAGMA user_version = 11;
