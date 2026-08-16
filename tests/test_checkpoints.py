"""Behaviour tests for `src/fleet/state/checkpoints.py` (SPEC §6 `checkpoints`, §8).

The property under test is the one the review corrected: a checkpoint that does not match the
loading class must *invalidate* — no payload, a named reason, re-run the phase — and must never
raise and never half-populate a model. Both failure modes are silent in the worst way: raising
aborts resume for a whole fleet at once because one field was renamed, and half-populating
resumes a run from a plan built to a shape that no longer exists.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import pytest

from fleet.models.enums import BreakStrategy, Phase
from fleet.models.graph import CollisionFinding, CycleFinding
from fleet.models.state import SCHEMA_VERSION
from fleet.state import db as dbmod
from fleet.state.checkpoints import CheckpointRejection, load, save
from fleet.state.db import StateWriter, connect_ro, initialize_database

RUN_ID = UUID("33333333-3333-4333-8333-333333333333")
RUN = str(RUN_ID)
REPO = "acme-commons"
TS = "2026-08-09T12:00:00+00:00"
SCC = "scc:" + "b" * 16


@pytest.fixture(autouse=True)
def _clean_write_slot() -> Iterator[None]:
    yield
    dbmod._release_write_slot()


def _payload() -> CycleFinding:
    return CycleFinding(
        scc_id=SCC,
        members=["acme-commons", "acme-billing"],
        edges=["1" * 64, "2" * 64],
        broken_edge_keys=["2" * 64],
        break_strategy=BreakStrategy.EDGE_BREAK,
        rationale="checkpointed plan",
    )


@pytest.fixture
async def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "state" / "fleet.db"
    await initialize_database(path)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO runs (run_id, started_at, config_sha256, harness_version) "
            "VALUES (?, ?, ?, '0.1.0')",
            (RUN, TS, "a" * 64),
        )
        conn.execute(
            "INSERT INTO repos (repo_id, name, url, updated_at) VALUES (?, ?, ?, ?)",
            (REPO, REPO, "ssh://git/commons.git", TS),
        )
        conn.commit()
    finally:
        conn.close()
    return path


async def test_a_checkpoint_round_trips_through_sqlite(db_path: Path) -> None:
    """The happy path: what was saved is what is loaded, validated, not merely deserialized.

    Why: the checkpoint is the reason a resumed phase does not re-pay for the LLM work that
    produced it. A round trip that loses a field silently re-runs the expensive part.
    """
    async with StateWriter(db_path, owner="test-writer") as writer:
        await save(writer, run_id=RUN_ID, repo_id=REPO, phase=Phase.SCAN, payload=_payload())

    conn = await connect_ro(db_path)
    try:
        result = await load(
            conn, run_id=RUN_ID, repo_id=REPO, phase=Phase.SCAN, model=CycleFinding
        )
    finally:
        await conn.close()

    assert result.usable
    assert result.rejection is None
    assert result.payload == _payload()


async def test_absent_checkpoint_is_a_rejection_not_an_error(db_path: Path) -> None:
    """"Nothing saved yet" is the ordinary first-run case and must look like every other
    "no usable checkpoint" answer, so a caller has exactly one branch to write."""
    conn = await connect_ro(db_path)
    try:
        result = await load(
            conn, run_id=RUN_ID, repo_id=REPO, phase=Phase.BUILD, model=CycleFinding
        )
    finally:
        await conn.close()

    assert result.payload is None
    assert result.rejection is CheckpointRejection.ABSENT


async def test_schema_version_mismatch_invalidates_and_never_raises(db_path: Path) -> None:
    """A checkpoint written under a different schema version yields NO payload — and no exception.

    Why: this is the whole point of the module. A renamed field would otherwise abort resume for
    the entire fleet at once (if load raised) or resume it from a plan half-built out of a stale
    shape (if load validated leniently). The version is compared BEFORE the data is parsed.
    """
    async with StateWriter(db_path, owner="test-writer") as writer:
        await save(
            writer,
            run_id=RUN_ID,
            repo_id=REPO,
            phase=Phase.SCAN,
            payload=_payload(),
            schema_version=SCHEMA_VERSION - 1,
        )

    conn = await connect_ro(db_path)
    try:
        result = await load(
            conn, run_id=RUN_ID, repo_id=REPO, phase=Phase.SCAN, model=CycleFinding
        )
    finally:
        await conn.close()

    assert result.payload is None, "a stale-shape checkpoint must not be half-populated"
    assert result.rejection is CheckpointRejection.SCHEMA_VERSION_MISMATCH
    assert str(SCHEMA_VERSION - 1) in result.detail


async def test_a_version_bumped_under_an_existing_checkpoint_invalidates_it(
    db_path: Path,
) -> None:
    """The upgrade shape of the same defect: the row is already on disk when the harness moves.

    Why: the realistic sequence is save-at-v7, upgrade, load-at-v8 — the recorded version is
    edited by nobody; the *loader* moved. Same outcome required: invalidate, do not raise.
    """
    async with StateWriter(db_path, owner="test-writer") as writer:
        await save(writer, run_id=RUN_ID, repo_id=REPO, phase=Phase.SCAN, payload=_payload())

    conn = await connect_ro(db_path)
    try:
        result = await load(
            conn,
            run_id=RUN_ID,
            repo_id=REPO,
            phase=Phase.SCAN,
            model=CycleFinding,
            schema_version=SCHEMA_VERSION + 1,
        )
    finally:
        await conn.close()

    assert result.payload is None
    assert result.rejection is CheckpointRejection.SCHEMA_VERSION_MISMATCH


async def test_a_checkpoint_written_by_another_model_invalidates(db_path: Path) -> None:
    """A different Pydantic class is a different checkpoint, even at the same schema version.

    Why: `checkpoints` is keyed only by (run_id, repo_id, phase), so a phase whose payload type
    changed would otherwise feed one model's JSON to another's validator and rely on luck.
    """
    async with StateWriter(db_path, owner="test-writer") as writer:
        await save(writer, run_id=RUN_ID, repo_id=REPO, phase=Phase.SCAN, payload=_payload())

    conn = await connect_ro(db_path)
    try:
        result = await load(
            conn, run_id=RUN_ID, repo_id=REPO, phase=Phase.SCAN, model=CollisionFinding
        )
    finally:
        await conn.close()

    assert result.payload is None
    assert result.rejection is CheckpointRejection.MODEL_MISMATCH


async def test_a_truncated_blob_invalidates_instead_of_raising(db_path: Path) -> None:
    """A torn or tampered payload re-runs the phase; it never escapes as a JSON error.

    Why: `load()` is called on the resume path for every repo. One corrupt row must cost one
    phase, not the run.
    """
    async with StateWriter(db_path, owner="test-writer") as writer:
        await save(writer, run_id=RUN_ID, repo_id=REPO, phase=Phase.SCAN, payload=_payload())

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE checkpoints SET payload = ? WHERE run_id = ?", (b'{"schema_ver', RUN)
        )
        conn.commit()
    finally:
        conn.close()

    ro = await connect_ro(db_path)
    try:
        result = await load(
            ro, run_id=RUN_ID, repo_id=REPO, phase=Phase.SCAN, model=CycleFinding
        )
    finally:
        await ro.close()

    assert result.payload is None
    assert result.rejection is CheckpointRejection.MALFORMED_ENVELOPE


async def test_data_that_no_longer_validates_invalidates(db_path: Path) -> None:
    """Right version, right class, but the recorded data violates the model's invariants.

    Why: the model's validators are the contract (`CycleFinding` requires ≥2 members). Accepting
    such a payload would smuggle an illegal object into a resumed run.
    """
    async with StateWriter(db_path, owner="test-writer") as writer:
        await save(writer, run_id=RUN_ID, repo_id=REPO, phase=Phase.SCAN, payload=_payload())

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT payload FROM checkpoints WHERE run_id = ?", (RUN,)).fetchone()
        envelope = json.loads(bytes(row[0]).decode("utf-8"))
        envelope["data"]["members"] = ["only-one"]
        conn.execute(
            "UPDATE checkpoints SET payload = ? WHERE run_id = ?",
            (json.dumps(envelope).encode("utf-8"), RUN),
        )
        conn.commit()
    finally:
        conn.close()

    ro = await connect_ro(db_path)
    try:
        result = await load(
            ro, run_id=RUN_ID, repo_id=REPO, phase=Phase.SCAN, model=CycleFinding
        )
    finally:
        await ro.close()

    assert result.payload is None
    assert result.rejection is CheckpointRejection.INVALID_PAYLOAD


async def test_saving_twice_replaces_rather_than_duplicates(db_path: Path) -> None:
    """One checkpoint per (run_id, repo_id, phase); the second save is the live one.

    Why: the upsert is the idempotency key (§11.7) and the reason the write is atomic — a reader
    sees the whole old checkpoint or the whole new one, never two rows to choose between.
    """
    second = _payload()
    second.rationale = "revised plan"

    async with StateWriter(db_path, owner="test-writer") as writer:
        await save(writer, run_id=RUN_ID, repo_id=REPO, phase=Phase.SCAN, payload=_payload())
        await save(writer, run_id=RUN_ID, repo_id=REPO, phase=Phase.SCAN, payload=second)

    conn = await connect_ro(db_path)
    try:
        async with conn.execute("SELECT COUNT(*) FROM checkpoints") as cursor:
            count = await cursor.fetchone()
        result = await load(
            conn, run_id=RUN_ID, repo_id=REPO, phase=Phase.SCAN, model=CycleFinding
        )
    finally:
        await conn.close()

    assert count is not None and count[0] == 1
    assert result.payload is not None
    assert result.payload.rationale == "revised plan"
