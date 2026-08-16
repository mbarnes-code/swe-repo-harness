"""Behaviour tests for `src/fleet/state/digest.py` — the run-equivalence proof (SPEC §11.6).

"Two runs are equivalent iff their digests match" is a claim about a *function*, and the ways it
fails are all silent. A digest built on `hash()` differs between processes because
`PYTHONHASHSEED` is randomized, so two byte-identical runs disagree and the proof means nothing.
A digest that follows dict or row order differs because SQLite returned rows in a different
order. A digest that folds in a timestamp differs every time. None of those raise; they just
quietly turn the equivalence proof into noise. Hence: a real subprocess with a different
`PYTHONHASHSEED`, and explicit relevant/irrelevant mutation pairs.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from uuid import UUID

import pytest

import fleet
from fleet.models.enums import BreakStrategy
from fleet.models.graph import CycleFinding
from fleet.state.db import connect_ro, initialize_database
from fleet.state.digest import DIGEST_SECTIONS, RunDigest, canonical_json, run_digest

RUN_ID = UUID("44444444-4444-4444-8444-444444444444")
RUN = str(RUN_ID)
TS = "2026-08-09T12:00:00+00:00"
SRC = Path(fleet.__file__).resolve().parents[1]
TRAILERS = {"acme-commons": ["p" * 40, "q" * 40], "acme-billing": ["r" * 40]}

_PROBE = """
import asyncio, sys
from pathlib import Path
from uuid import UUID

sys.path.insert(0, sys.argv[1])
from fleet.state.db import connect_ro
from fleet.state.digest import run_digest

async def main() -> None:
    conn = await connect_ro(Path(sys.argv[2]))
    try:
        result = await run_digest(conn, UUID(sys.argv[3]), patch_trailers=json.loads(sys.argv[4]))
    finally:
        await conn.close()
    print(result.digest)

import json
asyncio.run(main())
"""


def _seed(path: Path, *, reverse: bool = False) -> None:
    """Populate the tables the digest reads. `reverse` inserts every multi-row set backwards, so
    a digest that depended on row/insertion order would differ between the two databases."""
    cycle = CycleFinding(
        scc_id="scc:" + "c" * 16,
        members=["acme-billing", "acme-commons"],
        edges=["1" * 64, "2" * 64],
        broken_edge_keys=["2" * 64],
        break_strategy=BreakStrategy.EDGE_BREAK,
    )
    repos = [("acme-commons", 0), ("acme-billing", 1)]
    members = [(0, "REPO", "acme-commons"), (1, "REPO", "acme-billing")]
    edges = [
        ("1" * 64, "acme-billing", "acme-commons", "DECLARED_DEP", 0),
        ("2" * 64, "acme-commons", "acme-billing", "DECLARED_DEP", 1),  # suppressed feedback edge
    ]
    attempts = [
        ("att-1", 1, "REASONING", "EVIDENCE_ONLY", "a" * 64),
        ("att-2", 2, "REASONING", "EVIDENCE_PLUS_REJECTED_APPROACHES", "b" * 64),
    ]
    if reverse:
        repos, members, edges, attempts = (
            list(reversed(repos)),
            list(reversed(members)),
            list(reversed(edges)),
            list(reversed(attempts)),
        )

    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO runs (run_id, started_at, config_sha256, harness_version) "
            "VALUES (?, ?, ?, '0.1.0')",
            (RUN, TS, "a" * 64),
        )
        for repo_id, _ in repos:
            conn.execute(
                "INSERT INTO repos (repo_id, name, url, updated_at) VALUES (?, ?, ?, ?)",
                (repo_id, repo_id, f"ssh://git/{repo_id}.git", TS),
            )
        for index in (0, 1):
            conn.execute(
                "INSERT INTO waves (run_id, wave_index, computed_at) VALUES (?, ?, ?)",
                (RUN, index, TS),
            )
        for index, kind, node in members:
            conn.execute(
                "INSERT INTO wave_members (run_id, wave_index, node_kind, node_id) "
                "VALUES (?, ?, ?, ?)",
                (RUN, index, kind, node),
            )
        for key, src, dst, kind, suppressed in edges:
            conn.execute(
                "INSERT INTO edges (edge_key, run_id, src_id, dst_id, dst_coord_key, kind, "
                "ordering_suppressed, base_confidence, confidence, evidence_path, detected_at) "
                "VALUES (?, ?, ?, ?, 'maven:com.acme:x', ?, ?, 0.9, 0.9, 'pom.xml', ?)",
                (key, RUN, src, dst, kind, suppressed, TS),
            )
        conn.execute(
            "INSERT INTO contracts (run_id, contract_id, kind, identifier, owning_repo_id, "
            "extractable, extraction_confidence, hoist_target_path, status, detected_at) "
            "VALUES (?, 'proto:acme.v1', 'PROTO', 'acme.v1', 'acme-commons', 1, 0.9, "
            "'contracts/acme/v1', 'HOISTED', ?)",
            (RUN, TS),
        )
        conn.execute(
            "INSERT INTO findings (run_id, kind, severity, fingerprint, payload, created_at) "
            "VALUES (?, 'CycleDetected', 'warn', ?, ?, ?)",
            (RUN, "f" * 64, cycle.model_dump_json(), TS),
        )
        conn.execute(
            "INSERT INTO collisions (run_id, kind, key, repo_ids, severity, resolution, "
            "detected_at) VALUES (?, 'DEST_PATH', 'apps/portal', ?, 'warn', 'suffixed', ?)",
            (RUN, json.dumps(["acme-billing", "acme-commons"]), TS),
        )
        conn.execute(
            "INSERT INTO tasks (task_id, run_id, repo_id, phase, kind, dest_path, created_at) "
            "VALUES ('task-1', ?, 'acme-commons', 2, 'TRANSFORM', 'libs/commons', ?)",
            (RUN, TS),
        )
        for attempt_id, attempt, tier, policy, signature in attempts:
            conn.execute(
                "INSERT INTO attempts (attempt_id, run_id, repo_id, task_id, phase, attempt, "
                "tier, context_policy, approach_signature, command_sha256, started_at, "
                "finished_at) "
                "VALUES (?, ?, 'acme-commons', 'task-1', 2, ?, ?, ?, ?, ?, ?, ?)",
                (attempt_id, RUN, attempt, tier, policy, signature, "0" * 64, TS, TS),
            )
        conn.commit()
    finally:
        conn.close()


async def _digest(path: Path) -> RunDigest:
    conn = await connect_ro(path)
    try:
        return await run_digest(conn, RUN_ID, patch_trailers=TRAILERS)
    finally:
        await conn.close()


@pytest.fixture
async def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "state" / "fleet.db"
    await initialize_database(path)
    _seed(path)
    return path


def _subprocess_digest(tmp_path: Path, db_path: Path, seed: str) -> str:
    probe = tmp_path / "digest_probe.py"
    probe.write_text(_PROBE, encoding="utf-8")
    env = {**os.environ, "PYTHONHASHSEED": seed}
    completed = subprocess.run(  # noqa: S603 — our own script, our own interpreter
        [sys.executable, str(probe), str(SRC), str(db_path), RUN, json.dumps(TRAILERS)],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=True,
    )
    return completed.stdout.strip()


async def test_digest_is_identical_in_another_process_under_a_different_hash_seed(
    tmp_path: Path, db_path: Path
) -> None:
    """The load-bearing property: same inputs, different process, different PYTHONHASHSEED,
    same digest.

    Why: `hash()` is randomized per process and `set`/`dict` iteration inherits that randomness.
    A digest built on either differs run to run while every input is identical — and the
    "two runs are equivalent iff their digests match" proof it backs silently becomes noise,
    reporting drift that did not happen and hiding drift that did.
    """
    in_process = (await _digest(db_path)).digest

    for seed in ("0", "1", "524287"):
        assert _subprocess_digest(tmp_path, db_path, seed) == in_process, f"seed {seed} differed"


async def test_digest_ignores_row_and_insertion_order(tmp_path: Path, db_path: Path) -> None:
    """The same decisions recorded in a different order are the same run.

    Why: SQLite makes no promise about the order of an unordered SELECT, and a second run's
    inserts arrive in whatever order its workers finished. A digest that follows that order
    reports two identical runs as different — the false positive that trains operators to ignore
    the check.
    """
    other = tmp_path / "state" / "reversed.db"
    await initialize_database(other)
    _seed(other, reverse=True)

    assert (await _digest(other)).digest == (await _digest(db_path)).digest


async def test_digest_changes_when_the_wave_assignment_changes(db_path: Path) -> None:
    """A semantically relevant input: the same repos migrating in a different order.

    Why: the wave assignment IS the migration plan. If it can change without moving the digest,
    the digest cannot prove two runs equivalent.
    """
    before = await _digest(db_path)

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE wave_members SET wave_index = 0 WHERE run_id = ? AND node_id = 'acme-billing'",
            (RUN,),
        )
        conn.commit()
    finally:
        conn.close()

    after = await _digest(db_path)
    assert after.digest != before.digest
    assert after.sections["waves"] != before.sections["waves"]
    # ... and the section digests NAME the difference rather than merely reporting one (§11.6)
    unchanged = [s for s in DIGEST_SECTIONS if s != "waves"]
    assert all(after.sections[s] == before.sections[s] for s in unchanged)


async def test_digest_changes_when_the_anchoring_ladder_changes(db_path: Path) -> None:
    """`approach_signature` is in the digest so a run that anchored differently says so (§11.6).

    Why: two runs can reach the same waves and the same patches while one of them re-proposed a
    rejected approach. That is a real difference in how the run was produced, and the spec
    requires it to be visible rather than invisible.
    """
    before = await _digest(db_path)

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE attempts SET approach_signature = ? WHERE attempt_id = 'att-2'", ("d" * 64,)
        )
        conn.commit()
    finally:
        conn.close()

    after = await _digest(db_path)
    assert after.digest != before.digest
    assert after.sections["attempts"] != before.sections["attempts"]


async def test_digest_is_unchanged_by_semantically_irrelevant_inputs(db_path: Path) -> None:
    """Timestamps, confidence bookkeeping and signature-less attempts are not part of the run's
    identity, and must not move the digest.

    Why: a digest that changes on wall-clock time or on an unrelated row is never equal between
    two runs, so it can only ever report "different" — an equivalence proof that always fails
    proves nothing.
    """
    before = await _digest(db_path)

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("UPDATE edges SET detected_at = '2027-01-01T00:00:00+00:00', "
                     "confidence = 0.5 WHERE run_id = ?", (RUN,))
        conn.execute("UPDATE collisions SET detected_at = '2027-01-01T00:00:00+00:00' "
                     "WHERE run_id = ?", (RUN,))
        conn.execute("UPDATE runs SET finished_at = '2027-01-01T00:00:00+00:00' WHERE run_id = ?",
                     (RUN,))
        conn.execute(
            "INSERT INTO attempts (attempt_id, run_id, repo_id, task_id, phase, attempt, tier, "
            "approach_signature, command_sha256, started_at, finished_at) VALUES ('att-3', ?, "
            "'acme-commons', 'task-1', 2, 3, 'DETERMINISTIC', '', ?, ?, ?)",
            (RUN, "0" * 64, TS, TS),
        )
        conn.commit()
    finally:
        conn.close()

    assert (await _digest(db_path)).digest == before.digest


async def test_patch_trailer_commit_order_is_load_bearing(db_path: Path) -> None:
    """Repos are sorted; the trailers WITHIN a repo are not — their order is the commit order.

    Why: ADR-0024 makes the trailers read off `migrate/<repo>` the attestation of what the branch
    actually contains. Two patches applied in the opposite order is a different branch, so
    sorting them away would erase exactly the difference the digest exists to catch.
    """
    conn = await connect_ro(db_path)
    try:
        baseline = await run_digest(conn, RUN_ID, patch_trailers=TRAILERS)
        swapped_repos = await run_digest(
            conn, RUN_ID, patch_trailers=dict(reversed(list(TRAILERS.items())))
        )
        swapped_commits = await run_digest(
            conn,
            RUN_ID,
            patch_trailers={**TRAILERS, "acme-commons": list(reversed(TRAILERS["acme-commons"]))},
        )
    finally:
        await conn.close()

    assert swapped_repos.digest == baseline.digest
    assert swapped_commits.digest != baseline.digest


def test_canonical_json_is_key_order_independent_and_refuses_nan() -> None:
    """The serialization, not the caller, is what removes dict order — and NaN cannot round-trip.

    Why: every section is hashed through this one function, so a `json.dumps` default that
    preserved insertion order would reintroduce order-dependence everywhere at once.
    """
    assert canonical_json({"b": 1, "a": [2, 3]}) == canonical_json({"a": [2, 3], "b": 1})
    assert canonical_json({"a": 1}) == '{"a":1}'
    with pytest.raises(ValueError, match="Out of range"):
        canonical_json({"a": float("nan")})
