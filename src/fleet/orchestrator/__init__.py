"""Run orchestration: phase runner, wave scheduler, retry ladder, budgets, worker
registry (SPEC §8).

`RunContext` is the composition root: the state layer, the ledger, the semaphores, the router
and the projector are wired together there and handed down, so nothing below this package
reaches for a module global (CLAUDE.md Guardrail 3).
"""

from __future__ import annotations

from fleet.orchestrator.context import RunContext, lease_owner_id
from fleet.orchestrator.runner import (
    HaltReason,
    PhaseCheckpoint,
    PhaseRunner,
    RepoOutcome,
    RunHalted,
    WaveReport,
)
from fleet.orchestrator.scheduler import (
    Admission,
    SchedulerStore,
    SqliteSchedulerStore,
    WaveNotReadyError,
    WaveScheduler,
    WaveState,
)

__all__ = [
    "Admission",
    "HaltReason",
    "PhaseCheckpoint",
    "PhaseRunner",
    "RepoOutcome",
    "RunContext",
    "RunHalted",
    "SchedulerStore",
    "SqliteSchedulerStore",
    "WaveNotReadyError",
    "WaveReport",
    "WaveScheduler",
    "WaveState",
    "lease_owner_id",
]
