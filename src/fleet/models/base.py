"""Shared Pydantic v2 base for every durable model (SPEC §5, ADR-0002)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, field_validator, model_validator

LOG_TAIL_BYTES = 32_768  # what a durable row keeps; see TruncatedStr


def utcnow() -> datetime:
    """Single source of 'now'. Always tz-aware UTC; never naive. Every timestamp in the system
    is stamped by this function ON THE ORCHESTRATOR HOST — never by a worker container, never by
    SQLite's `CURRENT_TIMESTAMP` — so heartbeat and lease arithmetic compares one clock (§11.5)."""
    return datetime.now(UTC)


def _truncate_tail(v: object) -> object:
    """Keep the LAST 32 KiB and say so. Deliberately truncating rather than rejecting: a 400 KB
    Gradle stderr that fails validation is an attempt that is never persisted, an `attempts`
    counter that never increments, and a repair loop that re-runs the identical failing build
    forever — Rule 11 inverted into a silent infinite loop. The FULL stream is written to
    `artifacts/logs/<run_id>/<attempt_id>.log`; only that path is persisted beside the tail."""
    if not isinstance(v, str):
        return v
    raw = v.encode("utf-8")
    if len(raw) <= LOG_TAIL_BYTES:
        return v
    kept = raw[-LOG_TAIL_BYTES:].decode("utf-8", errors="replace")
    return f"{kept}\n[truncated {len(raw) - LOG_TAIL_BYTES} bytes]"


TruncatedStr = Annotated[str, BeforeValidator(_truncate_tail)]
"""The ONLY type any captured tool/probe/build output may be persisted under. `max_length` is
forbidden on such a field: a length bound on evidence rejects the very failures worth recording."""


class FleetModel(BaseModel):
    """Base for every durable model (ADR-0002). JSON is the only serialization path."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=False,
        validate_assignment=True,
        validate_default=True,
        str_strip_whitespace=True,
        use_enum_values=False,
        ser_json_timedelta="float",
    )

    @model_validator(mode="before")
    @classmethod
    def _drop_computed_fields(cls, data: Any) -> Any:
        """`model_dump_json()` emits every `@computed_field`, and `extra="forbid"` would then
        reject the model's own output on the way back in — which is exactly what resume does
        (§11.5) for `migration_state.json` and for every `BuildPlan` checkpoint. Computed keys
        are DERIVED, never inputs: they are dropped here rather than stored."""
        if isinstance(data, dict) and cls.model_computed_fields:
            return {k: v for k, v in data.items() if k not in cls.model_computed_fields}
        return data

    @field_validator("*", mode="after")
    @classmethod
    def _require_aware_datetimes(cls, v: object) -> object:
        if isinstance(v, datetime) and v.tzinfo is None:
            raise ValueError("naive datetime rejected: all timestamps must be tz-aware UTC")
        return v

    def touch(self) -> None:
        """Re-stamp `updated_at`. `default_factory=utcnow` fires ONCE, at construction, so an
        un-touched `updated_at` is really `created_at`. Every durable mutation goes through
        `state/repository.py`, and every such write calls this before it persists."""
        if "updated_at" in type(self).model_fields:
            self.updated_at = utcnow()
