"""Round VI task 65 — §12.31/D111 Leg D slice 1 (ADR-0122 Decisions 1/2/3/6).

Three things, DB + in-memory graph only, no git:

1. The `_graph_edges` crash fix (D117/ADR-0122 Decision 1) — a `FAILED` contract's
   `CONTRACT_IMPL`/`CONTRACT_CONSUME` edge rows must stop being rehydrated into the next graph
   build, or `build_graph` raises `GraphError` the instant it iterates one.
2. `unhoist_contract`'s applied path — blast-set demotion via `demote_to_floor` (Decision 3).
3. `unhoist_contract`'s refused path — the transitive downstream-merge check (Decision 6).

Fixtures are hand-built directly against the schema (no `fleet scan`/`fleet sequence` involved):
this is deliberately a unit-level proof of the DB/graph primitives, mirroring how Leg A
(`test_graph_cycles.py`) and Leg B (`test_vcs.py`) were each proven standalone before any
production wiring existed.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
import pytest

from fleet.cli import (
    HOIST_ROLLBACK_DEMOTION_FINDING_KIND,
    HOIST_ROLLBACK_REFUSED_FINDING_KIND,
    PR_RECORD_KIND,
    UnhoistOutcome,
    _graph_edges,
    _graph_nodes,
    unhoist_contract,
)
from fleet.graph.build import GraphError, build_graph
from fleet.llm.roles import SPEC_ROLE_TIERS
from fleet.models.enums import FailureClass, PrState, RepoStatus
from fleet.models.graph import DependencyEdge, NodeKind
from fleet.models.tasks import PullRequestDraft
from fleet.settings import FleetSettings
from fleet.state.db import StateWriter, connect_ro, initialize_database

RUN_ID = "22222222-2222-4222-8222-222222222222"
NOW = datetime(2026, 9, 6, 12, 0, 0, tzinfo=UTC)
NOW_ISO = "2026-09-06T12:00:00+00:00"
CONTRACT_ID = "proto:demo"

# -- minimal config, copied from tests/test_cli.py's write_config/MODELS_YAML/REPOS_YAML/
# FLEET_YAML shape: `unhoist_contract` takes a `FleetSettings` (mirrors `_persist_blast_radii`'s
# own signature), and this is the smallest known-good recipe for constructing one. -------------

_ROLES_BLOCK = "".join(
    f"  {role.value}: {tier.value}\n" for role, tier in sorted(SPEC_ROLE_TIERS.items())
)
_MODELS_YAML = (
    "version: 2\nroles:\n" + _ROLES_BLOCK + "default_profile: default\nprofiles:\n  default:\n"
    "    HEAVY:\n"
    "      - { backend: anthropic, model_id: claude-opus-5, effort: high,\n"
    "          api_key_env: ANTHROPIC_API_KEY,\n"
    "          price: { in_per_mtok: 5.0, out_per_mtok: 25.0 } }\n"
    "    WORKHORSE:\n"
    "      - { backend: anthropic, model_id: claude-sonnet-5, effort: high,\n"
    "          api_key_env: ANTHROPIC_API_KEY,\n"
    "          price: { in_per_mtok: 3.0, out_per_mtok: 15.0 } }\n"
    "    CHEAP:\n"
    "      - { backend: anthropic, model_id: claude-haiku-4-5,\n"
    "          api_key_env: ANTHROPIC_API_KEY,\n"
    "          price: { in_per_mtok: 1.0, out_per_mtok: 5.0 } }\n"
)
_REPOS_YAML = (
    "version: 1\ndefaults:\n  ref: main\nrepos:\n"
    "  - name: acme-commons\n    url: https://github.com/acme/acme-commons\n"
)
_FLEET_YAML = (
    "run:\n  monorepo_path: ../acme-monorepo\n"
    "preflight:\n  min_free_bytes: 1048576\n"
    "concurrency:\n  docker: 1\n"
    "verify:\n  container_memory: 64m\n"
    "budgets:\n  max_rss_mb: 512\n"
)


def _settings(tmp_path: Path) -> FleetSettings:
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "fleet.yaml").write_text(_FLEET_YAML, encoding="utf-8")
    (config_dir / "models.yaml").write_text(_MODELS_YAML, encoding="utf-8")
    (config_dir / "repos.yaml").write_text(_REPOS_YAML, encoding="utf-8")
    (config_dir / "rules").mkdir(parents=True, exist_ok=True)
    return FleetSettings.load(config_dir)


@pytest.fixture
async def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "state" / "fleet.db"
    await initialize_database(path)
    return path


def _ekey(*parts: str) -> str:
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


def _seed(
    db_path: Path,
    *,
    repos: Sequence[str],
    blast_set: Sequence[str] = (),
    owner_repo: str = "owner",
    contract_status: str = "HOISTED",
    dep_edges: Sequence[tuple[str, str]] = (),  # (dependent, dependency) edges
    phases: Mapping[str, Mapping[int, tuple[str, int]]] | None = None,  # repo -> {phase: status}
    pr_states: Mapping[str, str] | None = None,  # repo -> PrState value
) -> None:
    """Hand-build one run's worth of `runs`/`repos`/`contracts`/`edges`/`phases`/`findings` rows,
    exactly as if `fleet scan && fleet sequence` had really hoisted `CONTRACT_ID` and some repos
    had really progressed through Phase 3/4 against it."""
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO runs (run_id, started_at, config_sha256, config_digests, "
            "                  harness_version) VALUES (?, ?, ?, '{}', ?)",
            (RUN_ID, NOW_ISO, "a" * 64, "0.1.0"),
        )
        for repo_id in {*repos, owner_repo}:
            conn.execute(
                "INSERT INTO repos (repo_id, name, url, updated_at) VALUES (?, ?, ?, ?)",
                (repo_id, repo_id, f"https://example.invalid/{repo_id}", NOW_ISO),
            )
        conn.execute(
            "INSERT INTO contracts (run_id, contract_id, kind, identifier, owning_repo_id, "
            "                       extractable, hoist_target_path, status, detected_at) "
            "VALUES (?, ?, 'PROTO', 'demo', ?, 1, 'libs/proto/demo', ?, ?)",
            (RUN_ID, CONTRACT_ID, owner_repo, contract_status, NOW_ISO),
        )
        for repo_id in blast_set:
            conn.execute(
                "INSERT INTO edges (edge_key, run_id, src_kind, src_id, dst_kind, dst_id, "
                "                   dst_coord_key, kind, base_confidence, confidence, "
                "                   evidence_path, detected_at) "
                "VALUES (?, ?, 'REPO', ?, 'CONTRACT', ?, ?, 'CONTRACT_CONSUME', 0.9, 0.9, "
                "        'x.proto', ?)",
                (
                    _ekey("consume", repo_id, CONTRACT_ID),
                    RUN_ID,
                    repo_id,
                    CONTRACT_ID,
                    CONTRACT_ID,
                    NOW_ISO,
                ),
            )
        for dependent, dependency in dep_edges:
            conn.execute(
                "INSERT INTO edges (edge_key, run_id, src_kind, src_id, dst_kind, dst_id, "
                "                   dst_coord_key, kind, base_confidence, confidence, "
                "                   evidence_path, detected_at) "
                "VALUES (?, ?, 'REPO', ?, 'REPO', ?, ?, 'DECLARED_DEP', 0.95, 0.95, "
                "        'pom.xml', ?)",
                (
                    _ekey("dep", dependent, dependency),
                    RUN_ID,
                    dependent,
                    dependency,
                    f"maven:test:{dependency}",
                    NOW_ISO,
                ),
            )
        for repo_id, by_phase in (phases or {}).items():
            for phase_int, (status, attempts) in by_phase.items():
                conn.execute(
                    "INSERT INTO phases (run_id, repo_id, phase, status, attempts, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (RUN_ID, repo_id, phase_int, status, attempts, NOW_ISO),
                )
        for repo_id, state in (pr_states or {}).items():
            draft = PullRequestDraft(
                run_id=uuid.UUID(RUN_ID),
                repo_id=repo_id,
                wave_index=0,
                branch=f"migrate/{repo_id}",
                title=f"[fleet] migrate {repo_id}",
                body="body",
                source_url=f"https://example.invalid/{repo_id}",
                source_sha="a" * 40,
                state=PrState(state),
                url=f"https://forge.invalid/{repo_id}/pull/1",
            )
            conn.execute(
                "INSERT INTO findings (run_id, repo_id, kind, severity, fingerprint, payload, "
                "                      created_at) VALUES (?, ?, ?, 'info', ?, ?, ?)",
                (
                    RUN_ID,
                    repo_id,
                    PR_RECORD_KIND,
                    f"pr:{repo_id}",
                    draft.model_dump_json(),
                    NOW_ISO,
                ),
            )
    finally:
        conn.close()


def _contract_status(db_path: Path) -> str:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT status FROM contracts WHERE run_id = ? AND contract_id = ?",
            (RUN_ID, CONTRACT_ID),
        ).fetchone()
        assert row is not None
        return str(row[0])
    finally:
        conn.close()


def _phase_row(db_path: Path, repo_id: str, phase: int) -> tuple[str, int]:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT status, attempts FROM phases WHERE run_id = ? AND repo_id = ? AND phase = ?",
            (RUN_ID, repo_id, phase),
        ).fetchone()
        assert row is not None
        return str(row[0]), int(row[1])
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


async def _rows(
    conn: aiosqlite.Connection, sql: str, params: tuple[object, ...] = ()
) -> list[tuple]:
    async with conn.execute(sql, params) as cursor:
        return [tuple(row) for row in await cursor.fetchall()]


async def _unconditional_graph_edges(
    conn: aiosqlite.Connection, run_id: str
) -> list[DependencyEdge]:
    """Reproduces `_graph_edges` EXACTLY as it read before this task's fix — the unconditional
    `SELECT ... FROM edges WHERE run_id = ?` with no `contracts.status` filter. This is test-only
    scaffolding for Proof 1 (the crash must be real before the fix, and gone after); it is a
    deliberate historical snapshot, not a second implementation to keep in sync."""
    from fleet.models.enums import EdgeKind

    rows = await _rows(
        conn,
        "SELECT edge_key, src_kind, src_id, dst_kind, dst_id, dst_coord_key, kind, version_spec,"
        "       base_confidence, confidence, confidence_factors, ambiguous, ordering_suppressed,"
        "       evidence_path, evidence_line, detected_at, dst_candidate_repo_ids,"
        "       retargeted_from_repo_id"
        "  FROM edges WHERE run_id = ? ORDER BY edge_key",
        (run_id,),
    )
    edges: list[DependencyEdge] = []
    for row in rows:
        dst_kind = NodeKind(str(row[3]))
        line = int(row[14])
        dst_coordinate = None
        if dst_kind is NodeKind.REPO:
            from fleet.cli import _coordinate

            dst_coordinate = _coordinate(str(row[5]), row[7])
        edges.append(
            DependencyEdge(
                edge_key=str(row[0]),
                src_kind=NodeKind(str(row[1])),
                src_id=str(row[2]),
                dst_kind=dst_kind,
                dst_id=None if row[4] is None else str(row[4]),
                dst_coordinate=dst_coordinate,
                dst_candidate_repo_ids=list(json.loads(str(row[16]) or "[]")),
                retargeted_from_repo_id=None if row[17] is None else str(row[17]),
                kind=EdgeKind(str(row[6])),
                version_spec=None if row[7] is None else str(row[7]),
                base_confidence=float(row[8]),
                confidence=float(row[9]),
                confidence_factors=dict(json.loads(str(row[10]) or "{}")),
                ambiguous=bool(row[11]),
                ordering_suppressed=bool(row[12]),
                evidence_path=str(row[13]),
                evidence_line=None if line < 1 else line,
                detected_at=datetime.fromisoformat(str(row[15])),
            )
        )
    return edges


# =======================================================================================
# Proof 1 — the crash, reproduced and closed (D117/ADR-0122 Decision 1)
# =======================================================================================


async def test_a_failed_contracts_edges_crash_the_pre_fix_query_and_the_fix_closes_it(
    db_path: Path,
) -> None:
    """A `HOISTED` contract with a `CONTRACT_CONSUME` edge, flipped to `FAILED` by hand (bypassing
    `unhoist_contract` entirely — this proof is about the READ path, not the write). The
    UNCONDITIONAL pre-fix query crashes `build_graph`; the real, patched `_graph_edges` does not,
    and the untouched pre-hoist `consumer -> owner` row is what the DAG contains instead."""
    _seed(
        db_path,
        repos=("consumer",),
        blast_set=("consumer",),
        owner_repo="owner",
        contract_status="HOISTED",
        dep_edges=[("consumer", "owner")],  # the never-deleted pre-hoist repo->repo row
    )
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute(
            "UPDATE contracts SET status = 'FAILED' WHERE run_id = ? AND contract_id = ?",
            (RUN_ID, CONTRACT_ID),
        )
    finally:
        conn.close()

    read_conn = await connect_ro(db_path)
    try:
        nodes = await _graph_nodes(read_conn, RUN_ID)
        assert all(node.kind is not NodeKind.CONTRACT for node in nodes), (
            "sanity: `_graph_nodes` already excludes a FAILED contract's node — this is the "
            "half of the symmetry that was already correct before this task"
        )

        pre_fix_edges = await _unconditional_graph_edges(read_conn, RUN_ID)
        with pytest.raises(GraphError, match="not in the fleet"):
            build_graph(nodes, pre_fix_edges)

        fixed_edges = await _graph_edges(read_conn, RUN_ID)
        graph = build_graph(nodes, fixed_edges)
        assert graph.G.has_edge(("REPO", "consumer"), ("REPO", "owner")), (
            "the untouched pre-hoist repo->repo row must be exactly what the DAG contains"
        )
        assert not any(kind == "CONTRACT" for kind, _ in graph.G.nodes), (
            "no contract-kind node should have survived into this rebuild"
        )
    finally:
        await read_conn.close()


# =======================================================================================
# Proof 2 — `unhoist_contract`, applied path (ADR-0122 Decisions 2/3)
# =======================================================================================


async def test_unhoist_contract_applies_demotes_only_members_past_transform_and_retains_attempts(
    db_path: Path, tmp_path: Path
) -> None:
    settings = _settings(tmp_path)
    _seed(
        db_path,
        repos=("deep", "shallow"),
        blast_set=("deep", "shallow"),
        phases={
            # `deep`: BUILD SUCCEEDED -> past PHASE_TRANSFORM -> demoted
            "deep": {
                1: ("SUCCEEDED", 1),
                2: ("SUCCEEDED", 2),
                3: ("SUCCEEDED", 3),
                4: ("PENDING", 0),
            },
            # `shallow`: only TRANSFORM SUCCEEDED -> "at or below Phase.TRANSFORM has nothing to
            # demote" (ADR-0122 Decision 3) -> untouched
            "shallow": {
                1: ("SUCCEEDED", 1),
                2: ("SUCCEEDED", 5),
                3: ("PENDING", 0),
                4: ("PENDING", 0),
            },
        },
    )

    async with StateWriter(db_path, owner="test-unhoist") as writer:
        read_conn = await connect_ro(db_path)
        try:
            outcome = await unhoist_contract(
                read_conn,
                settings,
                writer=writer,
                run_id=RUN_ID,
                contract_id=CONTRACT_ID,
                now=NOW,
            )
        finally:
            await read_conn.close()

    assert isinstance(outcome, UnhoistOutcome)
    assert outcome.decision == "APPLIED"
    assert outcome.blast_set == ("deep", "shallow")
    assert outcome.demoted_repo_ids == ("deep",)
    assert outcome.blocking_repo_ids == ()

    assert _contract_status(db_path) == "FAILED"

    # `deep`: TRANSFORM and BUILD both go PENDING (both were SUCCEEDED, span >= TRANSFORM);
    # `attempts` is retained on every phase, not reset (ADR-0122 Decision 3 / ADR-0014 precedent).
    assert _phase_row(db_path, "deep", 2) == ("PENDING", 2)
    assert _phase_row(db_path, "deep", 3) == ("PENDING", 3)
    assert _phase_row(db_path, "deep", 1) == ("SUCCEEDED", 1), "below the floor: untouched"

    # `shallow` is untouched entirely: not in `demoted_repo_ids`, and its TRANSFORM row (with its
    # distinctive attempts=5 marker) is still SUCCEEDED.
    assert _phase_row(db_path, "shallow", 2) == ("SUCCEEDED", 5)

    phase_demoted = _findings_of_kind(db_path, "PhaseDemoted")
    assert {repo_id for repo_id, _ in phase_demoted} == {"deep"}
    assert len(phase_demoted) == 2, "one PhaseDemoted per demoted phase (TRANSFORM, BUILD)"

    rollback_findings = _findings_of_kind(db_path, HOIST_ROLLBACK_DEMOTION_FINDING_KIND)
    assert [repo_id for repo_id, _ in rollback_findings] == ["deep"]
    payload = rollback_findings[0][1]
    assert payload["contract_id"] == CONTRACT_ID
    assert payload["pre_demotion_phase"] == "BUILD"

    assert _findings_of_kind(db_path, HOIST_ROLLBACK_REFUSED_FINDING_KIND) == []


# =======================================================================================
# Proof 3 — `unhoist_contract`, refused path (ADR-0122 Decision 6): transitive, not one-hop
# =======================================================================================


def _seed_refusal_case(db_path: Path, *, hops: int, mid_merged: bool) -> None:
    """`blast` (PR MERGED) is the only blast-set member. `hops=1`: `direct` depends on `blast`
    directly. `hops=2`: `mid` depends on `blast`, `far` depends on `mid` — `far` is TWO hops from
    `blast`, and `mid_merged` controls whether the direct hop is ALSO merged (kept `False` for the
    discriminating case, so refusal can only come from the transitive hop)."""
    if hops == 1:
        _seed(
            db_path,
            repos=("blast", "direct"),
            blast_set=("blast",),
            dep_edges=[("direct", "blast")],
            pr_states={"blast": "MERGED", "direct": "MERGED"},
        )
    elif hops == 2:
        _seed(
            db_path,
            repos=("blast", "mid", "far"),
            blast_set=("blast",),
            dep_edges=[("mid", "blast"), ("far", "mid")],
            pr_states={
                "blast": "MERGED",
                "mid": "MERGED" if mid_merged else "OPEN",
                "far": "MERGED",
            },
        )
    else:
        raise ValueError(hops)


async def test_unhoist_contract_refuses_on_a_direct_merged_dependent_sanity(
    db_path: Path, tmp_path: Path
) -> None:
    """Sanity: the ordinary single-hop case must still refuse (a transitive-closure
    implementation is a superset of single-hop, not a replacement that drops it)."""
    settings = _settings(tmp_path)
    _seed_refusal_case(db_path, hops=1, mid_merged=False)

    async with StateWriter(db_path, owner="test-unhoist") as writer:
        read_conn = await connect_ro(db_path)
        try:
            outcome = await unhoist_contract(
                read_conn, settings, writer=writer, run_id=RUN_ID, contract_id=CONTRACT_ID, now=NOW
            )
        finally:
            await read_conn.close()

    assert outcome.decision == "REFUSED"
    assert outcome.blocking_repo_ids == ("direct",)
    assert _contract_status(db_path) == "HOISTED"


async def test_unhoist_contract_refuses_on_a_two_hop_merged_descendant_not_just_direct(
    db_path: Path, tmp_path: Path
) -> None:
    """THE discriminating proof (ADR-0122 Decision 6): `mid` (direct dependent of `blast`) is NOT
    merged, so a single-hop check would find nothing blocking and let the rollback through. `far`,
    two hops from `blast` via `mid`, IS merged — only a transitive traversal catches this, which
    is exactly what distinguishes this refusal from `fleet pr`'s one-hop check."""
    settings = _settings(tmp_path)
    _seed_refusal_case(db_path, hops=2, mid_merged=False)

    async with StateWriter(db_path, owner="test-unhoist") as writer:
        read_conn = await connect_ro(db_path)
        try:
            outcome = await unhoist_contract(
                read_conn, settings, writer=writer, run_id=RUN_ID, contract_id=CONTRACT_ID, now=NOW
            )
        finally:
            await read_conn.close()

    assert outcome.decision == "REFUSED"
    assert outcome.blocking_repo_ids == ("far",), (
        "`mid` must NOT be reported as blocking — it is not itself MERGED, only a conduit"
    )
    assert _contract_status(db_path) == "HOISTED", (
        "refused: contracts.status is left exactly as it was"
    )

    # Nothing else was written: no demotion, no HoistRollbackDemotion finding — the refusal
    # finding is the ONLY write on this path.
    assert _findings_of_kind(db_path, "PhaseDemoted") == []
    assert _findings_of_kind(db_path, HOIST_ROLLBACK_DEMOTION_FINDING_KIND) == []
    refused = _findings_of_kind(db_path, HOIST_ROLLBACK_REFUSED_FINDING_KIND)
    assert len(refused) == 1
    payload = refused[0][1]
    assert payload["contract_id"] == CONTRACT_ID
    assert payload["blocking_repo_ids"] == "far"
    assert payload["failure_class"] == str(FailureClass.CYCLE)
    assert payload["repo_status"] == str(RepoStatus.REQUIRES_HUMAN_INTERVENTION)
