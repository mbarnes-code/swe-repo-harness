"""Round VIII, §15.1 item 3, Wave 7.1 batch 29 — mutation-proof unit tests for cli.py's
hoist-rollback remainder group (G4): the three contract-status row writers
(`_hoisted_contract_rows`/`_rejected_contract_rows`/`_forbidden_contract_rows`), the
`_graph_edges` DB-rehydration boundary, and the `_write_hoist_rollback_*_finding` trio.

Scope note: `execute_hoist_rollback`/`_reconcile_hoist_rollbacks` are explicitly OUT of scope
(already proven elsewhere — `tests/test_hoist_rollback_git.py`, `tests/test_hoist_rollback_
wiring.py`). This file targets branches of the G4 functions that those existing suites, plus
`tests/test_unhoist_rollback.py` and `tests/test_sequence_e2e.py`, do NOT reach:

1. `_forbidden_contract_rows`'s `AND extractable = 1` guard (docstring-declared "second,
   deliberate guard") — every existing `--forbid-hoist` test (`tests/test_sequence_e2e.py::
   test_a_forbid_hoist_veto_survives_a_real_re_scan_and_re_sequence`) vetoes a contract that is
   ALREADY extractable, so the guard clause has never been exercised on the branch it exists to
   stop: an operator-typed id that was never a real hoist candidate.

2. `_write_hoist_rollback_failed_finding`'s `unhoist is None` branch. Every existing caller
   (`tests/test_hoist_rollback_wiring.py`) exercises this function only with `unhoist` populated
   (the `execute_hoist_rollback`-raised case) — asserting `unhoist_decision == "APPLIED"`. The
   narrower branch where `unhoist_contract` itself raises before returning (`unhoist=None`,
   `unhoist_decision`/`db_demoted_repo_ids` both empty strings per D44) is untested. This is
   also the concrete answer to the brief's "do not assume symmetry" prompt: `_write_hoist_
   rollback_refused_finding` and `_write_hoist_rollback_demotion_findings` take no such optional
   settled-outcome parameter at all — only `_failed` has this branch.

3. `_write_hoist_rollback_demotion_findings`'s cross-contract isolation. Its own docstring
   states it deliberately does NOT blanket-`DELETE ... WHERE kind = ?` the way `_persist_cycle_
   findings` does, specifically so that one contract's rollback does not erase an earlier
   contract's already-recorded `HoistRollbackDemotion` rows in the same run. Every existing test
   that reaches this function (`tests/test_unhoist_rollback.py`) calls `unhoist_contract` for
   exactly ONE contract per test, so this cross-contract claim has never been checked.

4. `_graph_edges`'s JSON round-trip of `dst_candidate_repo_ids`/`retargeted_from_repo_id` at the
   DB-rehydration boundary. `tests/test_graph_cycles.py` and `tests/test_repository.py` check
   these columns via hand-built `DependencyEdge`s or raw SQL, never through `_graph_edges` itself;
   `tests/test_unhoist_rollback.py`'s own `_graph_edges` proof is scoped to the
   `dst_kind='CONTRACT'` filter fix, not this REPO-edge decode path.

Each test follows CLAUDE.md Rule 12: backup the touched source region, apply a
discriminating mutation, verify the mutation actually changed the file (non-empty diff), run
this new test and confirm it fails under the mutation, then restore and re-verify the file is
byte-identical to its pre-mutation state.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fleet.cli import (
    HOIST_ROLLBACK_DEMOTION_FINDING_KIND,
    HOIST_ROLLBACK_FAILED_FINDING_KIND,
    _forbidden_contract_rows,
    _graph_edges,
    _write_hoist_rollback_demotion_findings,
    _write_hoist_rollback_failed_finding,
)
from fleet.models.enums import Ecosystem
from fleet.models.graph import NodeKind
from fleet.models.repo import Coordinate
from fleet.state.db import StateWriter, connect_ro, initialize_database

RUN_ID = "33333333-3333-4333-8333-333333333333"
NOW = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
NOW_ISO = "2026-09-14T12:00:00+00:00"


@pytest.fixture
async def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "state" / "fleet.db"
    await initialize_database(path)
    return path


def _seed_run(db_path: Path, *, repo_ids: tuple[str, ...] = ()) -> None:
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO runs (run_id, started_at, config_sha256, config_digests, "
            "                  harness_version) VALUES (?, ?, ?, '{}', ?)",
            (RUN_ID, NOW_ISO, "a" * 64, "0.1.0"),
        )
        for repo_id in repo_ids:
            conn.execute(
                "INSERT INTO repos (repo_id, name, url, updated_at) VALUES (?, ?, ?, ?)",
                (repo_id, repo_id, f"https://example.invalid/{repo_id}", NOW_ISO),
            )
    finally:
        conn.close()


def _seed_contract(
    db_path: Path,
    *,
    contract_id: str,
    extractable: int,
    status: str,
    owning_repo_id: str | None = None,
    hoist_target_path: str | None = None,
) -> None:
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO contracts (run_id, contract_id, kind, identifier, owning_repo_id, "
            "                       extractable, hoist_target_path, status, detected_at) "
            "VALUES (?, ?, 'PROTO', 'demo', ?, ?, ?, ?, ?)",
            (
                RUN_ID,
                contract_id,
                owning_repo_id,
                extractable,
                hoist_target_path,
                status,
                NOW_ISO,
            ),
        )
    finally:
        conn.close()


def _contract_status(db_path: Path, contract_id: str) -> str:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT status FROM contracts WHERE run_id = ? AND contract_id = ?",
            (RUN_ID, contract_id),
        ).fetchone()
        assert row is not None
        return str(row[0])
    finally:
        conn.close()


def _findings_of_kind(db_path: Path, kind: str) -> list[tuple[str | None, dict[str, object]]]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT repo_id, payload FROM findings WHERE run_id = ? AND kind = ?", (RUN_ID, kind)
        ).fetchall()
        return [(row[0], json.loads(row[1])) for row in rows]
    finally:
        conn.close()


# =======================================================================================
# 1. `_forbidden_contract_rows` — the `AND extractable = 1` guard
# =======================================================================================


async def test_forbidden_contract_rows_refuses_a_non_extractable_id(db_path: Path) -> None:
    """An operator-typed `--forbid-hoist` id that was never a real hoist candidate
    (`extractable=0`) must be a silent no-op — the row's `status` stays whatever it was, NOT
    'FORBIDDEN'. The docstring's own rationale: admitting a non-extractable row here would let a
    later `fleet scan` rebuild force `extractable=1` onto a row with `hoist_target_path IS NULL`
    (`workers/contracts.py::carry_over_committed` hardcodes `extractable: True` onto anything it
    carries forward) and violate the schema's own CHECK constraint on the next INSERT."""
    contract_id = "proto:never-a-candidate"
    _seed_run(db_path)
    _seed_contract(db_path, contract_id=contract_id, extractable=0, status="DETECTED")

    async with StateWriter(db_path, owner="test-forbidden") as writer:
        await writer.submit(_forbidden_contract_rows(RUN_ID, (contract_id,)))

    assert _contract_status(db_path, contract_id) == "DETECTED", (
        "a non-extractable id must never be promoted to FORBIDDEN by --forbid-hoist"
    )
    assert _findings_of_kind(db_path, "ContractHoistOverride") == []


# =======================================================================================
# 2. `_write_hoist_rollback_failed_finding` — the `unhoist is None` branch
# =======================================================================================


async def test_write_hoist_rollback_failed_finding_with_no_settled_outcome_writes_empty_strings(
    db_path: Path,
) -> None:
    """When `unhoist_contract` itself raises before returning (no settled `UnhoistOutcome` at
    all), `_reconcile_hoist_rollbacks` calls this writer with `unhoist=None`. D44's "no bare
    sentinel inside a settled value" precedent requires the WRITTEN payload to say so in `str`
    form: `unhoist_decision` and `db_demoted_repo_ids` must both be the empty string, not
    'None'/null/some other placeholder — distinguishing "nothing happened yet for this contract"
    from the `unhoist=APPLIED` case `tests/test_hoist_rollback_wiring.py` already covers."""
    contract_id = "proto:no-settled-outcome"
    _seed_run(db_path)

    async with StateWriter(db_path, owner="test-failed-finding") as writer:
        await _write_hoist_rollback_failed_finding(
            writer,
            RUN_ID,
            contract_id,
            RuntimeError("boom before any DB write"),
            unhoist=None,
            now=NOW,
        )

    (finding,) = _findings_of_kind(db_path, HOIST_ROLLBACK_FAILED_FINDING_KIND)
    repo_id, payload = finding
    assert repo_id is None
    assert payload["contract_id"] == contract_id
    assert payload["error_type"] == "RuntimeError"
    assert payload["unhoist_decision"] == "", (
        "unhoist=None must serialize to the empty string, not 'None' or a dropped key"
    )
    assert payload["db_demoted_repo_ids"] == ""


# =======================================================================================
# 3. `_write_hoist_rollback_demotion_findings` — cross-contract isolation
# =======================================================================================


async def test_write_hoist_rollback_demotion_findings_does_not_erase_a_sibling_contracts_rows(
    db_path: Path,
) -> None:
    """Deliberately NOT a blanket `DELETE FROM findings WHERE run_id = ? AND kind = ?` (unlike
    `_persist_cycle_findings`): one `unhoist_contract` call covers exactly one contract, and a
    blanket delete-by-kind would erase a DIFFERENT contract's `HoistRollbackDemotion` rows
    recorded earlier in the same run. Every existing test that reaches this writer
    (`tests/test_unhoist_rollback.py`) drives exactly one contract per test, so this claim has
    never actually been checked against a second contract's rows."""
    from fleet.models.enums import Phase

    _seed_run(db_path, repo_ids=("repo-a", "repo-b"))

    async with StateWriter(db_path, owner="test-demotion-isolation") as writer:
        await _write_hoist_rollback_demotion_findings(
            writer, RUN_ID, "proto:first", [("repo-a", Phase.BUILD)], now=NOW
        )
        await _write_hoist_rollback_demotion_findings(
            writer, RUN_ID, "proto:second", [("repo-b", Phase.TRANSFORM)], now=NOW
        )

    rows = _findings_of_kind(db_path, HOIST_ROLLBACK_DEMOTION_FINDING_KIND)
    by_repo = dict(rows)
    assert set(by_repo) == {"repo-a", "repo-b"}, (
        "the second contract's write must not have deleted the first contract's row"
    )
    assert by_repo["repo-a"]["contract_id"] == "proto:first"
    assert by_repo["repo-a"]["pre_demotion_phase"] == "BUILD"
    assert by_repo["repo-b"]["contract_id"] == "proto:second"
    assert by_repo["repo-b"]["pre_demotion_phase"] == "TRANSFORM"


# =======================================================================================
# 4. `_graph_edges` — `dst_candidate_repo_ids`/`retargeted_from_repo_id` DB round-trip
# =======================================================================================


def _edge_key(*parts: str) -> str:
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


async def test_graph_edges_round_trips_candidate_repo_ids_and_retargeted_from(
    db_path: Path,
) -> None:
    """`tests/test_graph_cycles.py` and `tests/test_repository.py` check
    `dst_candidate_repo_ids`/`retargeted_from_repo_id` via hand-built `DependencyEdge`s or raw
    SQL, never through `_graph_edges`'s own DB-row decode (`json.loads(row[16])`,
    `str(row[17])`). This proves the rehydration path itself is wired to the right column
    indices — a swapped or dropped index here would rebuild a graph with silently wrong
    ambiguity/rollback data despite every existing edge-column test staying green."""
    repo_a, repo_b, repo_c = "acme-a", "acme-b", "acme-c"
    _seed_run(db_path, repo_ids=(repo_a, repo_b, repo_c))
    dst_coord_key = Coordinate(ecosystem=Ecosystem.MAVEN, group="com.acme", name="widget").key

    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO edges (edge_key, run_id, src_kind, src_id, dst_kind, dst_id, "
            "                   dst_coord_key, dst_candidate_repo_ids, retargeted_from_repo_id, "
            "                   kind, version_spec, base_confidence, confidence, "
            "                   evidence_path, evidence_line, detected_at) "
            "VALUES (?, ?, 'REPO', ?, 'REPO', NULL, ?, ?, ?, 'DECLARED_DEP', ?, 0.9, 0.9, "
            "        'pom.xml', ?, ?)",
            (
                _edge_key("src", repo_a, dst_coord_key),
                RUN_ID,
                repo_a,
                dst_coord_key,
                json.dumps([repo_b, repo_c]),
                repo_a,  # retargeted_from_repo_id: pre-hoist owner
                "1.2.3",
                42,
                NOW_ISO,
            ),
        )
    finally:
        conn.close()

    read_conn = await connect_ro(db_path)
    try:
        (edge,) = await _graph_edges(read_conn, RUN_ID)
    finally:
        await read_conn.close()

    assert edge.dst_kind is NodeKind.REPO
    assert edge.dst_candidate_repo_ids == [repo_b, repo_c], (
        "dst_candidate_repo_ids must decode from its own column (row[16]), not e.g. "
        "retargeted_from_repo_id's"
    )
    assert edge.retargeted_from_repo_id == repo_a
    assert edge.evidence_line == 42
    assert edge.dst_coordinate == Coordinate(
        ecosystem=Ecosystem.MAVEN, group="com.acme", name="widget", version_spec="1.2.3"
    )
