"""Round VIII §15.1 item 3, Wave 7.3 batch 36 (G2 — worker class + remaining scan helpers).

Unit-level, mutation-targeted tests for the group of `src/fleet/cli.py` symbols scoped to this
batch: `_collision_params`, `_collision_rows`, `_coordinate_claims`, `_owns_hints`,
`_owner_index`, `_persist_blast_radii`, `_scan_statuses`, `ScanPipelineWorker` (+ its
`_ScanState`/`_ScanWaveStore`/`_ScanEvidence`/`_ScanSink` companions), `_primary_ecosystem`,
`_scan_rows`, `_manifest_row`, `_validate_scan_flags`, `_scan_run_id`, `_fleet_entries`.

None of these are exercised at unit granularity anywhere else in the suite (verified by grep
before writing this file): they are reached only transitively through `fleet scan`/`fleet
sequence` end-to-end runs in `tests/test_scan_e2e.py`, which prove the composed pipeline lands
the right rows but cannot isolate one function's own branch. Each test below drives its target
directly and asserts the ONE branch a full e2e run cannot discriminate cheaply.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from fleet.cli import (
    GlobalOptions,
    ScanInput,
    ScanOutput,
    ScanPipelineWorker,
    UsageError,
    _collision_params,
    _collision_rows,
    _coordinate_claims,
    _fleet_entries,
    _manifest_row,
    _owner_index,
    _owns_hints,
    _persist_blast_radii,
    _primary_ecosystem,
    _scan_rows,
    _scan_run_id,
    _scan_statuses,
    _ScanEvidence,
    _ScanSink,
    _ScanState,
    _ScanWaveStore,
    _validate_scan_flags,
)
from fleet.graph.collisions import CoordinateClaim
from fleet.models.enums import Ecosystem, RepoStatus, SymbolKind
from fleet.models.graph import CollisionFinding, SymbolRef
from fleet.models.repo import Coordinate, ManifestRef
from fleet.settings import FleetSettings, RepoEntry
from fleet.state.db import StateWriter, connect_ro, initialize_database
from fleet.workers.base import WorkerResult
from fleet.workers.baseline import BaselineOutput
from fleet.workers.clone import CloneOutput
from fleet.workers.interrogate import InterrogateOutput
from fleet.workers.interrogate import ManifestDependency as InterrogatedDependency
from fleet.workers.symbolindex import SymbolIndexOutput
from tests.test_cli import RUN_ID as CLI_RUN_ID
from tests.test_cli import write_config
from tests.test_workers_scan import make_ctx


def _ekey(*parts: str) -> str:
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


# =======================================================================================
# _collision_params -- pure tuple builder
# =======================================================================================


def test_collision_params_places_repo_ids_and_blob_shas_in_their_own_columns() -> None:
    """A swap between `repo_ids`/`blob_shas` (both `list[str]`, adjacent positional params to
    `_collision_params`) would be undetectable without asserting on json-decoded VALUES, since
    both columns are JSON text and a naive "is it a string" check passes either way."""
    import json as jsonlib

    collision = CollisionFinding(
        kind="FILE_PATH",
        key="src/shared.py",
        repo_ids=["acme-a", "acme-b"],
        blob_shas=["deadbeef"],
        severity="error",
        resolution=None,
    )
    row = _collision_params("run-1", collision, "2026-09-14T00:00:00+00:00")
    assert row[0] == "run-1"
    assert row[1] == "FILE_PATH"
    assert row[2] == "src/shared.py"
    assert jsonlib.loads(cast(str, row[3])) == ["acme-a", "acme-b"], (
        "repo_ids must land in its own column"
    )
    assert jsonlib.loads(cast(str, row[4])) == ["deadbeef"], "blob_shas must land in its own column"
    assert row[5] == "error"
    assert row[6] is None
    assert row[7] == "2026-09-14T00:00:00+00:00"


# =======================================================================================
# _collision_rows -- the empty-collisions no-write branch
# =======================================================================================


class _RecordingConn:
    def __init__(self) -> None:
        self.executemany_calls: list[tuple[str, list[tuple[object, ...]]]] = []

    async def executemany(self, sql: str, params: list[tuple[object, ...]]) -> None:
        self.executemany_calls.append((sql, params))


async def test_collision_rows_writes_nothing_when_there_are_no_collisions() -> None:
    """§3.1 step 8's audit only ever writes evidence it actually has -- an empty `collisions`
    sequence must produce a unit that touches the database not at all, never an
    `executemany([])` (which would still be a real round-trip and, worse, is indistinguishable
    from 'ran and found nothing' in a query plan)."""
    conn = _RecordingConn()
    unit = _collision_rows("run-1", (), "2026-09-14T00:00:00+00:00")
    await unit(conn)  # type: ignore[arg-type]
    assert conn.executemany_calls == [], "no collisions must mean no write at all"


async def test_collision_rows_writes_one_row_per_collision_when_present() -> None:
    conn = _RecordingConn()
    collision = CollisionFinding(
        kind="COORDINATE", key="npm:@acme:widgets", repo_ids=["a", "b"], severity="warn"
    )
    unit = _collision_rows("run-1", (collision,), "2026-09-14T00:00:00+00:00")
    await unit(conn)  # type: ignore[arg-type]
    assert len(conn.executemany_calls) == 1
    _, params = conn.executemany_calls[0]
    assert len(params) == 1
    assert params[0][2] == "npm:@acme:widgets"


# =======================================================================================
# _coordinate_claims -- the unknown-ecosystem skip (except ValueError: continue)
# =======================================================================================


@pytest.fixture
async def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "state" / "fleet.db"
    await initialize_database(path)
    return path


def _seed_run_and_repos(db_path: Path, *, repos: Sequence[str]) -> None:
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO runs (run_id, started_at, config_sha256, config_digests, "
            "                  harness_version) VALUES (?, ?, ?, '{}', ?)",
            (CLI_RUN_ID, "2026-09-14T00:00:00+00:00", "a" * 64, "0.1.0"),
        )
        for repo_id in repos:
            conn.execute(
                "INSERT INTO repos (repo_id, name, url, updated_at) VALUES (?, ?, ?, ?)",
                (repo_id, repo_id, f"https://example.invalid/{repo_id}",
                 "2026-09-14T00:00:00+00:00"),
            )
    finally:
        conn.close()


async def test_coordinate_claims_skips_a_manifest_with_an_unrecognized_ecosystem(
    db_path: Path,
) -> None:
    """A manifest row whose `ecosystem` column is not a live `Ecosystem` member (a build too old
    to know a newly retired/renamed member, or corrupt data) must be dropped from the contest --
    not raise, and not manufacture a divergence against every well-formed claim on the same key.
    A well-formed sibling claim on a DIFFERENT key must still come back untouched, proving the
    `continue` skips exactly the bad row and nothing else."""
    _seed_run_and_repos(db_path, repos=["acme-a", "acme-b"])
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO manifests (repo_id, path, ecosystem, adapter, adapter_version, sha256, "
            "    publishes_key, parsed_at) VALUES (?, ?, ?, ?, 1, ?, ?, ?)",
            ("acme-a", "pyproject.toml", "not-a-real-ecosystem", "py", "0" * 64,
             "pypi:acme:widgets", "2026-09-14T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO manifests (repo_id, path, ecosystem, adapter, adapter_version, sha256, "
            "    publishes_key, parsed_at) VALUES (?, ?, ?, ?, 1, ?, ?, ?)",
            ("acme-b", "package.json", "npm", "npm", "1" * 64,
             "npm::acme-lib", "2026-09-14T00:00:00+00:00"),
        )
    finally:
        conn.close()

    read_conn = await connect_ro(db_path)
    try:
        claims = await _coordinate_claims(read_conn)
    finally:
        await read_conn.close()

    assert {c.repo_id for c in claims} == {"acme-b"}, (
        "the unrecognized-ecosystem claim must be dropped, and the well-formed sibling kept"
    )
    assert claims[0].ecosystem == Ecosystem.NPM


# =======================================================================================
# _owns_hints -- the "hint names no actual publisher" fallback
# =======================================================================================


def test_owns_hints_falls_back_to_hint_order_when_no_hinting_repo_actually_publishes() -> None:
    """`(publishing or names)[0]`: when the intersection of hinting repos and ACTUAL publishers
    is empty, the hint still resolves (to the first hinting name), rather than being dropped --
    the detector, not this function, decides what to do with a hint naming no publisher. Mutating
    the `or` to `and`, or dropping the fallback, would make this return {} instead."""
    settings_repos = (
        RepoEntry(name="acme-a", url="https://example.invalid/a", owns=("widgets",)),
        RepoEntry(name="acme-b", url="https://example.invalid/b", owns=("widgets",)),
    )

    class _FakeRepos:
        repos = settings_repos

    class _FakeSettings:
        repos = _FakeRepos()

    # Neither acme-a nor acme-b actually publishes "widgets" in the claims set.
    claims = (
        CoordinateClaim(coord_key="widgets", repo_id="acme-c", ecosystem=Ecosystem.NPM),
    )
    hints = _owns_hints(_FakeSettings(), claims)  # type: ignore[arg-type]
    assert hints == {"widgets": "acme-a"}, (
        "no hinting repo publishes 'widgets', so the fallback must pick the first hinting NAME "
        "(sorted order: acme-a before acme-b) rather than dropping the hint or picking a claimant"
    )


def test_owns_hints_prefers_the_actual_publisher_among_hinting_repos() -> None:
    """The intersection IS the point: when one hinting repo actually publishes, it wins over a
    hinting repo that merely sorts first alphabetically."""
    settings_repos = (
        RepoEntry(name="acme-a", url="https://example.invalid/a", owns=("widgets",)),
        RepoEntry(name="acme-b", url="https://example.invalid/b", owns=("widgets",)),
    )

    class _FakeRepos:
        repos = settings_repos

    class _FakeSettings:
        repos = _FakeRepos()

    claims = (CoordinateClaim(coord_key="widgets", repo_id="acme-b", ecosystem=Ecosystem.NPM),)
    hints = _owns_hints(_FakeSettings(), claims)  # type: ignore[arg-type]
    assert hints == {"widgets": "acme-b"}, "acme-b actually publishes; it must win over acme-a"


# =======================================================================================
# _owner_index -- the owner_repo_id IS NOT NULL filter
# =======================================================================================


async def test_owner_index_excludes_external_unowned_coordinates(db_path: Path) -> None:
    """`coordinates.owner_repo_id IS NULL` means external (a dependency nobody in the fleet
    publishes). `_owner_index`'s WHERE clause must exclude those rows -- an internal/external
    mixup here would make `_persist_scan_edges` treat an external dependency as ordering the
    fleet."""
    _seed_run_and_repos(db_path, repos=["acme-owner"])
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO coordinates (coord_key, ecosystem, grp, name, owner_repo_id, "
            "    first_seen_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("npm::owned-thing", "npm", "", "owned-thing", "acme-owner",
             "2026-09-14T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO coordinates (coord_key, ecosystem, grp, name, owner_repo_id, "
            "    first_seen_at) VALUES (?, ?, ?, ?, NULL, ?)",
            ("npm::external-thing", "npm", "", "external-thing", "2026-09-14T00:00:00+00:00"),
        )
    finally:
        conn.close()

    read_conn = await connect_ro(db_path)
    try:
        index = await _owner_index(read_conn)
    finally:
        await read_conn.close()

    assert set(index.owners) == {"npm::owned-thing"}, (
        "the NULL-owner (external) coordinate must not appear in the internal/external oracle"
    )
    assert index.owners["npm::owned-thing"] == ("acme-owner",)


# =======================================================================================
# _persist_blast_radii -- the no-radii early return, and real computation + persistence
# =======================================================================================


async def test_persist_blast_radii_writes_nothing_when_the_fleet_has_no_repos(
    db_path: Path, tmp_path: Path
) -> None:
    """`if not radii: return` -- an empty fleet must never reach `writer.submit`. Asserted with a
    writer that records whether it was ever called, since a call with an empty `executemany` list
    would be silently harmless but is not what the guard clause promises."""
    _seed_run_and_repos(db_path, repos=[])

    class _RecordingWriter:
        def __init__(self) -> None:
            self.submitted = False

        async def submit(self, unit: Any) -> None:
            self.submitted = True

    read_conn = await connect_ro(db_path)
    writer = _RecordingWriter()
    try:
        await _persist_blast_radii(
            _real_settings(tmp_path),
            writer=writer,  # type: ignore[arg-type]
            read_conn=read_conn,
            run_id=CLI_RUN_ID,
            now=datetime(2026, 9, 14, tzinfo=UTC),
        )
    finally:
        await read_conn.close()
    assert writer.submitted is False, "an empty fleet must never reach writer.submit"


def _real_settings(tmp_path: Path) -> FleetSettings:
    """A real, fully-validated `FleetSettings` -- `_persist_blast_radii` reads `config.graph.*`,
    which only a genuinely loaded config carries correctly."""
    config_root = tmp_path / f"settings-{id(tmp_path)}"
    write_config(config_root)
    return FleetSettings.load(config_root / "config")


async def test_persist_blast_radii_computes_and_persists_real_descendant_counts(
    db_path: Path, tmp_path: Path
) -> None:
    """The whole point of the function: `repos.blast_radius` must end up as the REAL descendant
    count from the just-written `edges`, not left at its 0 default. `acme-lib` is depended on by
    `acme-app`, so `acme-lib` (1 descendant) must out-radius `acme-app` (0 descendants) -- a
    mutation that swapped the graph orientation, or dropped the write, would leave both at 0 or
    reverse which repo gets the nonzero count."""
    _seed_run_and_repos(db_path, repos=["acme-lib", "acme-app"])
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO edges (edge_key, run_id, src_kind, src_id, dst_kind, dst_id, "
            "                   dst_coord_key, kind, base_confidence, confidence, "
            "                   evidence_path, detected_at) "
            "VALUES (?, ?, 'REPO', ?, 'REPO', ?, ?, 'DECLARED_DEP', 0.95, 0.95, "
            "        'pyproject.toml', ?)",
            (
                _ekey("dep", "acme-app", "acme-lib"),
                CLI_RUN_ID,
                "acme-app",
                "acme-lib",
                "pypi::acme-lib",
                "2026-09-14T00:00:00+00:00",
            ),
        )
    finally:
        conn.close()

    async with StateWriter(db_path, owner="test-batch36-blast-radii") as writer:
        read_conn = await connect_ro(db_path)
        try:
            await _persist_blast_radii(
                _real_settings(tmp_path),
                writer=writer,
                read_conn=read_conn,
                run_id=CLI_RUN_ID,
                now=datetime(2026, 9, 14, tzinfo=UTC),
            )
        finally:
            await read_conn.close()

    conn = sqlite3.connect(db_path)
    try:
        rows = dict(conn.execute("SELECT repo_id, blast_radius FROM repos").fetchall())
    finally:
        conn.close()
    assert rows == {"acme-lib": 1, "acme-app": 0}, (
        "acme-lib has one descendant (acme-app) and acme-app has none -- got "
        f"{rows!r}"
    )


# =======================================================================================
# _scan_statuses -- the phase = 1 (SCAN) filter
# =======================================================================================


async def test_scan_statuses_reads_only_phase_one_rows(db_path: Path) -> None:
    """A repo's Phase 3 (TRANSFORM) status must never leak into what `fleet scan` reports as the
    scan-phase status of that same repo."""
    _seed_run_and_repos(db_path, repos=["acme-a", "acme-b"])
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO phases (run_id, repo_id, phase, status, updated_at) "
            "VALUES (?, 'acme-a', 1, 'SUCCEEDED', ?)",
            (CLI_RUN_ID, "2026-09-14T00:00:00+00:00"),
        )
        # Same repo, a LATER phase, a DIFFERENT status -- must not be what comes back.
        conn.execute(
            "INSERT INTO phases (run_id, repo_id, phase, status, updated_at) "
            "VALUES (?, 'acme-a', 3, 'BLOCKED', ?)",
            (CLI_RUN_ID, "2026-09-14T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO phases (run_id, repo_id, phase, status, updated_at) "
            "VALUES (?, 'acme-b', 1, 'SKIPPED', ?)",
            (CLI_RUN_ID, "2026-09-14T00:00:00+00:00"),
        )
    finally:
        conn.close()

    read_conn = await connect_ro(db_path)
    try:
        statuses = await _scan_statuses(read_conn, CLI_RUN_ID)
    finally:
        await read_conn.close()

    assert statuses == {"acme-a": RepoStatus.SUCCEEDED, "acme-b": RepoStatus.SKIPPED}, (
        "phase 3's BLOCKED for acme-a must not shadow its phase 1 SUCCEEDED"
    )


# =======================================================================================
# _scan_run_id -- "latest run" ordering, not insertion order
# =======================================================================================


async def test_scan_run_id_picks_the_run_with_the_latest_started_at_not_last_inserted(
    db_path: Path,
) -> None:
    """`ORDER BY started_at DESC, run_id DESC LIMIT 1` must pick the run that started LATEST, even
    when it was inserted FIRST -- an `ORDER BY rowid`/no-ORDER-BY mutation would instead reflect
    insertion order and silently resume the wrong run."""
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO runs (run_id, started_at, config_sha256, config_digests, "
            "                  harness_version) VALUES (?, ?, ?, '{}', ?)",
            ("11111111-1111-4111-8111-000000000001", "2026-09-14T23:00:00+00:00", "a" * 64,
             "0.1.0"),
        )
        conn.execute(
            "INSERT INTO runs (run_id, started_at, config_sha256, config_digests, "
            "                  harness_version) VALUES (?, ?, ?, '{}', ?)",
            ("11111111-1111-4111-8111-000000000002", "2026-09-14T01:00:00+00:00", "a" * 64,
             "0.1.0"),
        )
    finally:
        conn.close()

    read_conn = await connect_ro(db_path)
    try:
        run_id = await _scan_run_id(read_conn, GlobalOptions(run=None))
    finally:
        await read_conn.close()

    assert run_id == "11111111-1111-4111-8111-000000000001", (
        "the LATER-started run (inserted first) must win, not the later-inserted row"
    )


async def test_scan_run_id_honors_an_explicit_run_flag_without_touching_the_database(
    db_path: Path,
) -> None:
    read_conn = await connect_ro(db_path)
    try:
        run_id = await _scan_run_id(read_conn, GlobalOptions(run="operator-chosen-id"))
    finally:
        await read_conn.close()
    assert run_id == "operator-chosen-id"


# =======================================================================================
# _fleet_entries -- --only glob filter, order preservation, and the no-match refusal
# =======================================================================================


def _repos_manifest(names: Sequence[str]) -> Any:
    """`_fleet_entries` only reads `.repos.repos` and `.config_dir` off its `settings` argument
    (a `FleetSettings`, a frozen dataclass with many other required fields this test does not
    need) -- a duck-typed stand-in keeps the fixture to exactly what the function under test
    touches."""

    class _Repos:
        def __init__(self) -> None:
            self.repos = tuple(
                RepoEntry(name=n, url=f"https://example.invalid/{n}") for n in names
            )

    class _Settings:
        def __init__(self) -> None:
            self.repos = _Repos()
            self.config_dir = Path("/nonexistent/does-not-matter/config")

    return _Settings()


def test_fleet_entries_filters_by_glob_and_preserves_manifest_order() -> None:
    settings = _repos_manifest(["acme-app", "zzz-lib", "acme-lib"])
    chosen = _fleet_entries(settings, "acme-*")
    assert [e.name for e in chosen] == ["acme-app", "acme-lib"], (
        "must keep manifest order (acme-app before acme-lib), not sort or reverse the match set"
    )


def test_fleet_entries_refuses_an_only_pattern_matching_nothing() -> None:
    settings = _repos_manifest(["acme-app"])
    with pytest.raises(UsageError, match="matched none"):
        _fleet_entries(settings, "no-such-*")


def test_fleet_entries_refuses_an_empty_fleet_even_with_no_only_filter() -> None:
    settings = _repos_manifest([])
    with pytest.raises(UsageError, match="declares no repos"):
        _fleet_entries(settings, None)


# =======================================================================================
# _validate_scan_flags -- the --repos identity check and the concurrency floor
# =======================================================================================


def test_validate_scan_flags_refuses_a_repos_path_outside_the_config_bundle(
    tmp_path: Path,
) -> None:
    """§9: `--repos` must resolve to EXACTLY `<config_dir>/repos.yaml`, or a later drift check
    would compare against a manifest this run never read. A `==` mutated to `!=` (or dropped
    entirely) would let a foreign manifest through silently."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "repos.yaml").write_text("version: 1\nrepos: []\n", encoding="utf-8")
    foreign = tmp_path / "elsewhere" / "repos.yaml"
    foreign.parent.mkdir()
    foreign.write_text("version: 1\nrepos: []\n", encoding="utf-8")

    opts = GlobalOptions(config_path=config_dir / "fleet.yaml")
    with pytest.raises(UsageError, match="is not"):
        _validate_scan_flags(
            opts, repos=foreign, refresh=False, preflight_only=False, concurrency=None
        )
    # The matching path must NOT raise.
    _validate_scan_flags(
        opts,
        repos=config_dir / "repos.yaml",
        refresh=False,
        preflight_only=False,
        concurrency=None,
    )


def test_validate_scan_flags_refuses_concurrency_below_one(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "repos.yaml").write_text("version: 1\nrepos: []\n", encoding="utf-8")
    opts = GlobalOptions(config_path=config_dir / "fleet.yaml")
    with pytest.raises(UsageError, match="must be at least 1"):
        _validate_scan_flags(
            opts, repos=config_dir / "repos.yaml", refresh=False, preflight_only=False,
            concurrency=0,
        )
    # concurrency=1 is the floor, not refused.
    _validate_scan_flags(
        opts, repos=config_dir / "repos.yaml", refresh=False, preflight_only=False, concurrency=1
    )


# =======================================================================================
# _manifest_row -- field order and bool -> int coercion
# =======================================================================================


def test_manifest_row_maps_publishes_to_its_key_and_coerces_low_confidence_to_int() -> None:
    ref = ManifestRef(
        repo_id="acme-a",
        path="pyproject.toml",
        ecosystem=Ecosystem.PYPI,
        adapter="py",
        adapter_version=1,
        sha256="0" * 64,
        publishes=Coordinate(ecosystem=Ecosystem.PYPI, name="acme-a"),
        dependency_count=3,
        low_confidence=True,
        parse_error=None,
    )
    row = _manifest_row(ref)
    assert row[0] == "acme-a"
    assert row[1] == "pyproject.toml"
    assert row[2] == "pypi"
    assert row[6] == "pypi::acme-a"
    assert row[7] == 3
    assert row[8] == 1, "low_confidence=True must serialize as SQLite INTEGER 1, not Python True"
    assert row[9] is None


def test_manifest_row_leaves_publishes_key_none_when_the_manifest_publishes_nothing() -> None:
    ref = ManifestRef(
        repo_id="acme-a",
        path="requirements.txt",
        ecosystem=Ecosystem.PYPI,
        adapter="py",
        adapter_version=1,
        sha256="0" * 64,
        publishes=None,
    )
    row = _manifest_row(ref)
    assert row[6] is None


# =======================================================================================
# _primary_ecosystem -- None on no publishes, and the min-by-key tie rule
# =======================================================================================


def test_primary_ecosystem_is_none_when_no_manifest_publishes_a_coordinate() -> None:
    ref = ManifestRef(
        repo_id="acme-a",
        path="README.md",
        ecosystem=Ecosystem.UNKNOWN,
        adapter="none",
        adapter_version=1,
        sha256="0" * 64,
        publishes=None,
    )
    assert _primary_ecosystem((ref,)) is None


def test_primary_ecosystem_picks_the_ecosystem_of_the_lowest_sorting_coordinate_key() -> None:
    """Matches `_scan_rows`'s own `min(coord.key for coord in published)` EXACTLY -- comparing by
    `Ecosystem` member instead (plain `Enum`, unordered) would raise or silently pick the wrong
    one on a tie in some other field."""
    npm_ref = ManifestRef(
        repo_id="acme-a", path="package.json", ecosystem=Ecosystem.NPM, adapter="npm",
        adapter_version=1, sha256="0" * 64,
        publishes=Coordinate(ecosystem=Ecosystem.NPM, name="zzz-last"),
    )
    pypi_ref = ManifestRef(
        repo_id="acme-a", path="pyproject.toml", ecosystem=Ecosystem.PYPI, adapter="py",
        adapter_version=1, sha256="1" * 64,
        publishes=Coordinate(ecosystem=Ecosystem.PYPI, name="aaa-first"),
    )
    # "npm:...:zzz-last" sorts before "pypi:...:aaa-first" ('n' < 'p'), even though "aaa-first"
    # sorts before "zzz-last" on the NAME alone -- proving the comparison is on the whole key,
    # not on some other field the two coordinates might tie-break on.
    assert _primary_ecosystem((npm_ref, pypi_ref)) == Ecosystem.NPM


# =======================================================================================
# _scan_rows -- §37 Blocker B: a dependency's declared range must never reach `version`
# =======================================================================================


async def test_scan_rows_never_writes_a_dependents_version_spec_into_an_unowned_coordinate(
    db_path: Path,
) -> None:
    """The coordinate `pypi::acme-lib` is only ever seen here as a DEPENDENCY (acme-app declares
    `acme-lib>=2.0`); nobody in this scan owns/publishes it. `_scan_rows` must insert it with
    `owner_repo_id IS NULL` and `version IS NULL` -- writing the dependent's `>=2.0` into
    `version` would poison the one column §37 Blocker B reserves for the owning repo's OWN
    published version."""
    _seed_run_and_repos(db_path, repos=["acme-app"])
    manifest = ManifestRef(
        repo_id="acme-app", path="pyproject.toml", ecosystem=Ecosystem.PYPI, adapter="py",
        adapter_version=1, sha256="0" * 64, publishes=None,
    )
    # `version_spec` set on the COORDINATE itself (not just the `RawDependency` below), matching
    # how a real `ManifestDependency.coordinate` carries the declared range through -- a
    # coordinate whose own `version_spec` is unset either way could never discriminate the
    # `coord.version_spec if owned else None` branch from a mutation dropping the `if owned`.
    dependency_coord = Coordinate(ecosystem=Ecosystem.PYPI, name="acme-lib", version_spec=">=2.0")
    interrogate = InterrogateOutput(
        repo_id="acme-app",
        manifests=(manifest,),
        dependencies=(
            InterrogatedDependency(
                manifest_path=manifest.path,
                coordinate=dependency_coord,
                scope=None,
                optional=False,
            ),
        ),
        ecosystems=(Ecosystem.PYPI,),
    )
    output = ScanOutput(repo_id="acme-app", interrogate=interrogate)

    async with StateWriter(db_path, owner="test-batch36-scan-rows") as writer:
        await writer.submit(_scan_rows(CLI_RUN_ID, output, "2026-09-14T00:00:00+00:00"))

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT owner_repo_id, version FROM coordinates WHERE coord_key = ?",
            (dependency_coord.key,),
        ).fetchone()
    finally:
        conn.close()
    assert row == (None, None), (
        f"a dependency-only coordinate must have NULL owner AND NULL version, got {row!r}"
    )


async def test_scan_rows_writes_the_owning_repos_own_published_version(db_path: Path) -> None:
    """The mirror-image case: when the SAME coordinate is later scanned as a PUBLISH (`owned`),
    its own version must land -- proving the `owned` flag, not merely absence-of-write, drives
    the branch."""
    _seed_run_and_repos(db_path, repos=["acme-lib"])
    published_coord = Coordinate(ecosystem=Ecosystem.PYPI, name="acme-lib", version_spec="3.1.0")
    manifest = ManifestRef(
        repo_id="acme-lib", path="pyproject.toml", ecosystem=Ecosystem.PYPI, adapter="py",
        adapter_version=1, sha256="0" * 64, publishes=published_coord,
    )
    interrogate = InterrogateOutput(
        repo_id="acme-lib", manifests=(manifest,), dependencies=(), ecosystems=(Ecosystem.PYPI,),
    )
    output = ScanOutput(repo_id="acme-lib", interrogate=interrogate)

    async with StateWriter(db_path, owner="test-batch36-scan-rows-owned") as writer:
        await writer.submit(_scan_rows(CLI_RUN_ID, output, "2026-09-14T00:00:00+00:00"))

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT owner_repo_id, version FROM coordinates WHERE coord_key = ?",
            (published_coord.key,),
        ).fetchone()
    finally:
        conn.close()
    assert row == ("acme-lib", "3.1.0")


# =======================================================================================
# _ScanState -- output() dedups findings while preserving first-seen order
# =======================================================================================


def test_scan_state_output_dedups_repeated_findings_preserving_order() -> None:
    state = _ScanState(repo_id="acme-a")
    state.findings.extend(["EmptyRepo", "OversizeBlob", "EmptyRepo"])
    output = state.output()
    assert output.findings == ("EmptyRepo", "OversizeBlob"), (
        "duplicates must collapse and first-seen order must survive -- `set()` would not "
        "guarantee order and a plain `tuple()` would keep the duplicate"
    )


# =======================================================================================
# _ScanWaveStore -- the wrong-wave-index empty tuple, and begin_wave's set-once clock
# =======================================================================================


async def test_scan_wave_store_returns_empty_members_for_any_wave_but_the_scan_wave() -> None:
    from fleet.cli import SCAN_WAVE_INDEX

    store = _ScanWaveStore(inner=object(), members=("acme-a", "acme-b"))  # type: ignore[arg-type]
    assert await store.wave_members("run-1", SCAN_WAVE_INDEX) == ("acme-a", "acme-b")
    assert await store.wave_members("run-1", SCAN_WAVE_INDEX + 1) == ()


async def test_scan_wave_store_begin_wave_sets_the_start_time_exactly_once() -> None:
    """A second `begin_wave` call (e.g. a re-entrant driver) must return the FIRST timestamp, not
    overwrite it -- `wave_started_at` is read later to compute wave duration, and a clock that
    moves on every call would make every wave look instantaneous."""
    store = _ScanWaveStore(inner=object(), members=())  # type: ignore[arg-type]
    from fleet.cli import SCAN_WAVE_INDEX

    first = datetime(2026, 9, 14, 10, 0, 0, tzinfo=UTC)
    second = datetime(2026, 9, 14, 11, 0, 0, tzinfo=UTC)
    started = await store.begin_wave("run-1", SCAN_WAVE_INDEX, now=first)
    started_again = await store.begin_wave("run-1", SCAN_WAVE_INDEX, now=second)
    assert started == first
    assert started_again == first, "the second call must not move the wave's recorded start"


# =======================================================================================
# _ScanEvidence.record -- gated / baseline_red tracking and truncated aggregation
# =======================================================================================


def test_scan_evidence_records_gated_only_for_a_failed_preflight_not_a_passing_one() -> None:
    evidence = _ScanEvidence()
    gated_output = ScanOutput(
        repo_id="acme-empty",
        clone=CloneOutput(
            repo_id="acme-empty", url="https://x", mirror_path="/m", preflight_ok=False,
            findings=("EmptyRepo",),
        ),
        findings=("EmptyRepo",),
    )
    healthy_output = ScanOutput(
        repo_id="acme-ok",
        clone=CloneOutput(repo_id="acme-ok", url="https://x", mirror_path="/m", preflight_ok=True),
    )
    evidence.record(gated_output)
    evidence.record(healthy_output)
    assert evidence.gated == {"acme-empty": ("EmptyRepo",)}, (
        "only the gated (preflight_ok=False) repo may appear in `gated`"
    )


def test_scan_evidence_records_baseline_red_only_for_a_measured_failure() -> None:
    evidence = _ScanEvidence()
    red = ScanOutput(
        repo_id="acme-red",
        baseline=BaselineOutput(repo_id="acme-red", baseline_ok=False, baseline_test_count=0),
    )
    green = ScanOutput(
        repo_id="acme-green",
        baseline=BaselineOutput(repo_id="acme-green", baseline_ok=True, baseline_test_count=5),
    )
    skipped = ScanOutput(repo_id="acme-skip", baseline=None)
    evidence.record(red)
    evidence.record(green)
    evidence.record(skipped)
    assert set(evidence.baseline_red) == {"acme-red"}, (
        "a green baseline and a skipped (None) baseline must never appear in baseline_red"
    )


def test_scan_evidence_marks_a_repo_truncated_if_any_of_its_symbol_batches_truncated() -> None:
    evidence = _ScanEvidence()
    output = ScanOutput(
        repo_id="acme-big",
        symbol_batches=(
            SymbolIndexOutput(repo_id="acme-big", symbols=(), truncated=False),
            SymbolIndexOutput(repo_id="acme-big", symbols=(), truncated=True),
        ),
    )
    evidence.record(output)
    assert evidence.truncated == {"acme-big"}


# =======================================================================================
# _ScanSink -- skips the symbols insert when a batch produced no symbols
# =======================================================================================


class _RecordingWriter:
    def __init__(self) -> None:
        self.submitted: list[Any] = []

    async def submit(self, unit: Any) -> None:
        self.submitted.append(unit)
        await unit(_RecordingConn())  # exercise the closure, same as StateWriter would


class _RecordingRepository:
    def __init__(self) -> None:
        self.inserted: list[list[Any]] = []

    async def insert_symbols(self, rows: list[Any]) -> None:
        self.inserted.append(rows)


async def test_scan_sink_does_not_call_insert_symbols_when_there_are_no_symbols() -> None:
    writer = _RecordingWriter()
    repository = _RecordingRepository()
    evidence = _ScanEvidence()
    sink = _ScanSink(writer=writer, repository=repository, run_id="run-1", evidence=evidence)  # type: ignore[arg-type]
    output = ScanOutput(repo_id="acme-a")
    result = WorkerResult[ScanOutput](status="ok", output=output)
    await sink(repo_id="acme-a", phase=None, fence=1, result=result)  # type: ignore[arg-type]
    assert repository.inserted == [], "an empty symbol set must never reach insert_symbols"


async def test_scan_sink_inserts_every_symbol_across_every_batch(db_path: Path) -> None:
    writer = _RecordingWriter()
    repository = _RecordingRepository()
    evidence = _ScanEvidence()
    sink = _ScanSink(writer=writer, repository=repository, run_id="run-1", evidence=evidence)  # type: ignore[arg-type]
    symbol = SymbolRef(
        repo_id="acme-a", fqn="acme.a.Widget", kind=SymbolKind.CLASS, path="a.py", line=1,
        language="py",
        is_definition=True,
    )
    output = ScanOutput(
        repo_id="acme-a",
        symbol_batches=(SymbolIndexOutput(repo_id="acme-a", symbols=(symbol,)),),
    )
    result = WorkerResult[ScanOutput](status="ok", output=output)
    await sink(repo_id="acme-a", phase=None, fence=1, result=result)  # type: ignore[arg-type]
    assert len(repository.inserted) == 1
    assert len(repository.inserted[0]) == 1
    assert repository.inserted[0][0].fqn == "acme.a.Widget"


# =======================================================================================
# ScanPipelineWorker -- the clone-gate break, and cancelled-vs-partial interruption
# =======================================================================================


class _FakeStepWorker:
    """Duck-types `BaseWorker.run`; records whether it was ever invoked."""

    def __init__(self, result: WorkerResult[Any] | None = None, *, on_call: Any = None) -> None:
        self._result = result
        self._on_call = on_call
        self.calls = 0

    async def run(self, ctx: Any, payload: Any) -> WorkerResult[Any]:
        self.calls += 1
        if self._on_call is not None:
            self._on_call(ctx)
        assert self._result is not None
        return self._result


def _scan_input(**overrides: Any) -> ScanInput:
    base: dict[str, Any] = {
        "repo_id": "acme-a",
        "url": "https://example.invalid/acme-a",
        "cache_dir": "/nonexistent/cache",
    }
    base.update(overrides)
    return ScanInput(**base)


async def test_scan_pipeline_worker_stops_after_a_gated_clone_without_running_later_steps(
    tmp_path: Path,
) -> None:
    """§3.1 step 1's "an empty repo is SKIPPED, not an error": once `clone` reports
    `preflight_ok=False`, the dispatch must break BEFORE `interrogate`/`classify`/`baseline`/
    `symbolindex` run at all -- proven here by a call counter a real e2e run cannot expose
    (it can only see the end state, not whether a later worker's `.run()` was ever entered)."""
    worker = ScanPipelineWorker()
    clone_output = CloneOutput(
        repo_id="acme-a", url="https://x", mirror_path="/m", preflight_ok=False,
        findings=("EmptyRepo",),
    )
    clone_worker = _FakeStepWorker(WorkerResult[CloneOutput](status="ok", output=clone_output))
    later_workers = {
        unit: _FakeStepWorker(WorkerResult[Any](status="ok", output=object()))
        for unit in ("interrogate", "classify", "baseline", "symbolindex")
    }
    worker._workers = cast(Any, {"clone": clone_worker, **later_workers})

    ctx = make_ctx(tmp_path)
    payload = _scan_input()
    result = await worker.run(ctx, payload)

    assert clone_worker.calls == 1
    for name, fake in later_workers.items():
        assert fake.calls == 0, f"{name} must never run after a gated clone"
    assert result.status == "ok"
    assert result.completed_units == ["clone"]


async def test_scan_pipeline_worker_reports_cancelled_when_nothing_has_landed(
    tmp_path: Path,
) -> None:
    """`_interrupted([], owed, state)`: a cancellation before the FIRST step lands must report
    `cancelled`, never `partial` -- a `partial` with empty `completed_units` is rejected by
    `WorkerResult`'s own validator, but a hand-rolled bypass of that distinction is exactly what
    this guards against at the call site."""
    worker = ScanPipelineWorker()
    never_called = _FakeStepWorker(WorkerResult[Any](status="ok", output=object()))
    units = ("clone", "interrogate", "classify", "baseline", "symbolindex")
    worker._workers = cast(Any, dict.fromkeys(units, never_called))
    ctx = make_ctx(tmp_path)
    ctx.cancel.set()
    payload = _scan_input()
    result = await worker.run(ctx, payload)
    assert result.status == "cancelled"
    assert never_called.calls == 0


async def test_scan_pipeline_worker_reports_partial_with_correct_remaining_units_mid_fleet(
    tmp_path: Path,
) -> None:
    """A cancellation that arrives AFTER `clone` landed but before `interrogate` runs must report
    `partial`, with `completed_units == ["clone"]` and `remaining_units` holding exactly the
    steps that never ran -- the checkpoint a resumed scan re-enters at."""
    worker = ScanPipelineWorker()
    clone_output = CloneOutput(repo_id="acme-a", url="https://x", mirror_path="/m",
                                preflight_ok=True)

    ctx = make_ctx(tmp_path)

    def _cancel_after_clone(_ctx: Any) -> None:
        ctx.cancel.set()

    clone_worker = _FakeStepWorker(
        WorkerResult[CloneOutput](status="ok", output=clone_output), on_call=_cancel_after_clone
    )
    never_called = _FakeStepWorker(WorkerResult[Any](status="ok", output=object()))
    worker._workers = cast(Any, {
        "clone": clone_worker,
        "interrogate": never_called,
        "classify": never_called,
        "baseline": never_called,
        "symbolindex": never_called,
    })
    payload = _scan_input()
    result = await worker.run(ctx, payload)
    assert result.status == "partial"
    assert result.completed_units == ["clone"]
    assert set(result.remaining_units) == {"interrogate", "classify", "baseline", "symbolindex"}
    assert never_called.calls == 0
