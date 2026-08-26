"""Per-`(run_id, repo_id, phase)` checkpoint payloads (SPEC §6 `checkpoints`, §8).

Saves and loads Pydantic models as JSON envelopes in the `checkpoints` table — **no pickle,
anywhere, ever** (ADR-0002). The write is a single upsert inside the `StateWriter`'s
`BEGIN IMMEDIATE`, so it is atomic by construction: a reader sees the whole previous checkpoint
or the whole new one, never a half-replaced blob, and a crash mid-write rolls back.

**A checkpoint that does not match the loading class is INVALID, not fatal.** `load()` never
raises on a bad checkpoint: it returns `LoadedCheckpoint(payload=None, rejection=...)`, which
means "no usable checkpoint — re-run the phase". Why this is the only safe behaviour:

* raising would abort resume for the *whole fleet* the moment one field is renamed, and a
  250-repo fleet is days of LLM spend;
* half-populating a model from a stale shape is worse than either — the run would resume from a
  plan built to an old contract and nothing downstream would ever notice.

So the recorded `schema_version` and the recorded model name are both compared before the
payload is parsed at all, and a mismatch, a malformed envelope or a failed validation all land
in the same place: no payload, a named `rejection`, and a re-run. The rejection is *named*, not
swallowed (CLAUDE.md Rule 11) — callers log `rejection`/`detail` and proceed.

SQL errors are NOT checkpoint invalidation and do propagate: a locked or corrupt database is an
infrastructure failure, not a stale plan.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final
from uuid import UUID

import aiosqlite
from pydantic import BaseModel, ValidationError

from fleet.models.base import utcnow
from fleet.models.enums import Phase
from fleet.models.state import SCHEMA_VERSION
from fleet.state.db import StateWriter

__all__ = [
    "CheckpointRejection",
    "LoadedCheckpoint",
    "delete_in_unit",
    "load",
    "save",
]


class CheckpointRejection(StrEnum):
    """Why there is no usable checkpoint. Every member means the same thing: re-run the phase."""

    ABSENT = "ABSENT"                                  # nothing was ever written for this key
    SCHEMA_VERSION_MISMATCH = "SCHEMA_VERSION_MISMATCH"  # written under a different model shape
    MODEL_MISMATCH = "MODEL_MISMATCH"                  # written by a different Pydantic class
    MALFORMED_ENVELOPE = "MALFORMED_ENVELOPE"          # truncated / not the envelope we write
    INVALID_PAYLOAD = "INVALID_PAYLOAD"                # envelope fine, model rejected the data


@dataclass(frozen=True, slots=True)
class LoadedCheckpoint[M: BaseModel]:
    """`payload` is the validated model, or `None` with a `rejection` naming why there is none."""

    payload: M | None = None
    rejection: CheckpointRejection | None = None
    detail: str = ""

    @property
    def usable(self) -> bool:
        return self.payload is not None

    @property
    def discarded_stored_work(self) -> bool:
        """Bytes WERE stored under this key and were refused: the phase re-runs work it had
        already landed. `ABSENT` is deliberately not this — nothing was there to discard, and a
        key with no checkpoint is the ordinary fresh dispatch rather than something to report."""
        return self.rejection is not None and self.rejection is not CheckpointRejection.ABSENT


_ENVELOPE_KEYS: Final = ("schema_version", "model", "data")

_UPSERT: Final = """
INSERT INTO checkpoints (run_id, repo_id, phase, model_name, payload, created_at)
VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT (run_id, repo_id, phase) DO UPDATE SET
    model_name = excluded.model_name,
    payload    = excluded.payload,
    created_at = excluded.created_at
"""

_SELECT: Final = """
SELECT model_name, payload FROM checkpoints
 WHERE run_id = ? AND repo_id = ? AND phase = ?
"""

_DELETE: Final = "DELETE FROM checkpoints WHERE run_id = ? AND repo_id = ? AND phase = ?"


def _model_name(model: type[BaseModel]) -> str:
    """Fully qualified, so two same-named models in different modules cannot be confused."""
    return f"{model.__module__}.{model.__qualname__}"


async def save(
    writer: StateWriter,
    *,
    run_id: UUID,
    repo_id: str,
    phase: Phase,
    payload: BaseModel,
    schema_version: int = SCHEMA_VERSION,
) -> None:
    """Persist `payload` for `(run_id, repo_id, phase)`, replacing any previous checkpoint.

    The envelope records the schema version and the model's qualified name *beside* the data, so
    `load()` can reject a stale shape without parsing it. Writes go through the single writer
    (§11.5); the whole unit is one statement, which is what makes it atomic.
    """
    envelope = json.dumps(
        {
            "schema_version": schema_version,
            "model": _model_name(type(payload)),
            "data": json.loads(payload.model_dump_json()),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    row = (
        str(run_id),
        repo_id,
        int(phase),
        _model_name(type(payload)),
        envelope,
        utcnow().isoformat(),
    )

    async def unit(conn: aiosqlite.Connection) -> None:
        await conn.execute(_UPSERT, row)

    await writer.submit(unit)


async def delete_in_unit(
    conn: aiosqlite.Connection,
    *,
    run_id: str | UUID,
    repo_id: str,
    phases: Iterable[Phase],
) -> int:
    """Delete the checkpoints for `phases`, INSIDE a write unit the caller already owns.

    This is the one function here that takes a **connection** rather than a `StateWriter`, and
    that asymmetry is the whole point. §11.5 step 5 demotes a repo's `phases` rows to `PENDING`
    and drops the checkpoints above the re-entry floor together: a checkpoint for a phase that is
    about to be re-run is a lie about work the run no longer claims. If the two writes were two
    units they would be two `BEGIN IMMEDIATE` transactions, and a crash between them would make
    that lie *durable* — a `PENDING` phase carrying the previous run's finished payload, which
    `load()` would hand back as usable on the next resume. So the caller submits one unit and
    calls this inside it; there is no `StateWriter` overload, because offering one would offer
    the split.

    Returns the number of rows actually deleted (phases with no checkpoint contribute 0), so a
    caller can report what it discarded rather than what it attempted.
    """
    deleted = 0
    for phase in phases:
        cursor = await conn.execute(_DELETE, (str(run_id), repo_id, int(phase)))
        deleted += int(cursor.rowcount)
    return deleted


async def load[M: BaseModel](
    conn: aiosqlite.Connection,
    *,
    run_id: UUID,
    repo_id: str,
    phase: Phase,
    model: type[M],
    schema_version: int = SCHEMA_VERSION,
) -> LoadedCheckpoint[M]:
    """Return the checkpoint for `(run_id, repo_id, phase)`, or a named rejection.

    NEVER raises on a checkpoint problem. The version and model-name checks run *before* the
    data is handed to Pydantic, so a stale shape can never half-populate `model`.
    """
    async with conn.execute(_SELECT, (str(run_id), repo_id, int(phase))) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return LoadedCheckpoint(rejection=CheckpointRejection.ABSENT)

    expected = _model_name(model)
    stored_name = str(row[0])
    if stored_name != expected:
        return LoadedCheckpoint(
            rejection=CheckpointRejection.MODEL_MISMATCH,
            detail=f"checkpoint holds {stored_name!r}, loader expects {expected!r}",
        )

    try:
        envelope = json.loads(bytes(row[1]).decode("utf-8"))
    except (ValueError, TypeError) as exc:
        return LoadedCheckpoint(
            rejection=CheckpointRejection.MALFORMED_ENVELOPE, detail=str(exc)
        )
    if not isinstance(envelope, dict) or any(key not in envelope for key in _ENVELOPE_KEYS):
        return LoadedCheckpoint(
            rejection=CheckpointRejection.MALFORMED_ENVELOPE,
            detail=f"envelope must carry {_ENVELOPE_KEYS}",
        )

    stored_version = envelope["schema_version"]
    if stored_version != schema_version:
        return LoadedCheckpoint(
            rejection=CheckpointRejection.SCHEMA_VERSION_MISMATCH,
            detail=f"checkpoint at schema_version {stored_version}, loader at {schema_version}",
        )
    if envelope["model"] != expected:
        return LoadedCheckpoint(
            rejection=CheckpointRejection.MODEL_MISMATCH,
            detail=f"envelope holds {envelope['model']!r}, loader expects {expected!r}",
        )

    try:
        return LoadedCheckpoint(payload=model.model_validate(envelope["data"]))
    except ValidationError as exc:
        return LoadedCheckpoint(rejection=CheckpointRejection.INVALID_PAYLOAD, detail=str(exc))
