"""`RunContext` — the wired-together run handle (SPEC §8, §11.1, §11.5).

Every collaborator a phase needs is **injected here and handed down**: the run identity, the
single `StateWriter`, the `StateRepository`, the `CostLedger`, the `Limits` semaphores, the LLM
router, the debounced `Projector` and the bound logger. Nothing in the orchestration path
imports a module global (CLAUDE.md Guardrail 3), which is what makes a two-repo wave testable
against a temp database with fake workers and no network.

This is also where the **`ModelClient` is assembled**, once per run, from the pieces that are
injected into it — the `LlmRouter`, the `ModelBackend` mapping and (optionally) the §11.6 cache
store. A router alone resolves a role to a tier and can do nothing with it: `complete()` lives on
the client, so a `WorkerContext` handed only a router puts ADR-0014's repair and escalation rungs
behind a wall they cannot cross. The client is built HERE rather than by each worker because a
worker that constructed its own would be reaching past the context for a vendor-shaped
collaborator, and because the cache decorator is a per-run decision an individual rung must not
be able to opt out of.

The context is also where the two clocks are kept apart, because conflating them is a real bug
and not a style question:

* `clock()` is the **orchestrator host's wall clock** (`utcnow`, tz-aware UTC). It stamps lease
  expiry, heartbeats and `wave_started_at` — every value SQLite compares (§5 invariant 4,
  §11.5). A worker never stamps one.
* `monotonic()` is the **running loop's clock**, which is what `WorkerContext.deadline` is
  expressed in. A deadline compared against a wall clock breaks the moment NTP steps it.

Both are injectable so a test can drive a four-hour wave budget in milliseconds instead of
sleeping through it.
"""

from __future__ import annotations

import asyncio
import os
import socket
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

from fleet.llm.cache import CachingModelClient
from fleet.llm.client import LadderModelClient
from fleet.models.base import utcnow
from fleet.models.enums import Phase, TransformTier
from fleet.obs.log import get_logger
from fleet.orchestrator.findings import LlmFindingSink
from fleet.settings import FleetConfig
from fleet.workers.base import WorkerContext

if TYPE_CHECKING:  # types only — none of these is needed to import this module
    import aiosqlite
    from structlog.stdlib import BoundLogger

    from fleet.llm.cache import CacheMode, LlmCacheStore
    from fleet.llm.client import CallBudget, CallPolicy, ModelBackend, ModelClient
    from fleet.llm.roles import LlmRouter
    from fleet.orchestrator.budgets import CostLedger, Limits
    from fleet.state.db import StateWriter
    from fleet.state.projection import Projector
    from fleet.state.repository import StateRepository

__all__ = [
    "RunContext",
    "default_logger",
    "lease_owner_id",
]

#: §9 `budgets.task_max_wallclock_s` is declared per phase name; `Phase` is an `IntEnum`. The map
#: is explicit rather than `phase.name.lower()` so a renamed enum member fails at import, not at
#: wave 7 with a silently defaulted ceiling.
_WALLCLOCK_FIELD: Final[Mapping[Phase, str]] = {
    Phase.SCAN: "scan",
    Phase.TRANSFORM: "transform",
    Phase.BUILD: "build",
    Phase.VERIFY: "verify",
}

#: One per process. §6's lease identity needs something that changes across restarts, because a
#: fresh PID namespace reuses low pids and a bare pid is therefore not an identity.
_BOOT_UUID: Final = uuid.uuid4().hex[:12]


def lease_owner_id(container_id: str | None = None) -> str:
    """The §6 lease identity `'{host}:{container_id}:{pid}:{boot_uuid}'`.

    Recomputed per process, never persisted between runs: the whole point of the fourth component
    is that a restarted orchestrator does not inherit the identity whose lease it is about to
    reap.
    """
    container = container_id or os.environ.get("FLEET_CONTAINER_ID", "local")
    return f"{socket.gethostname()}:{container}:{os.getpid()}:{_BOOT_UUID}"


def default_logger(name: str = "fleet.orchestrator") -> BoundLogger:
    """The one place the structlog proxy is narrowed to `WorkerContext.log`'s declared type."""
    return cast("BoundLogger", get_logger(name))


def _loop_time() -> float:
    """`WorkerContext.deadline`'s clock. Requires a running loop, which every caller has."""
    return asyncio.get_running_loop().time()


@dataclass(frozen=True, slots=True)
class RunContext:
    """Process-wide run handle. Constructed once by the CLI, passed into the `PhaseRunner`.

    Frozen because a run's identity, its writer and its ledger are not things a phase may swap
    out mid-wave: a runner that could rebind `repository` would defeat the single-writer rule by
    accident rather than by design (§11.5).
    """

    run_id: uuid.UUID
    config: FleetConfig
    writer: StateWriter
    """The ONLY write path (§11.5). Workers never see it; they return `WorkerResult`s."""
    repository: StateRepository
    read_conn: aiosqlite.Connection
    """`mode=ro`, WAL-concurrent with the writer — what checkpoints are loaded through."""
    ledger: CostLedger
    limits: Limits
    llm: LlmRouter
    """ADR-0023's role → tier → targets routing. What the client is BUILT from, and what a worker
    gets as `WorkerContext.router` for the `limits.for_tier` semaphore — never as a call surface."""
    log: BoundLogger
    work_dir: Path
    lease_owner: str = field(default_factory=lease_owner_id)
    projector: Projector | None = None
    clock: Callable[[], datetime] = utcnow
    monotonic: Callable[[], float] = _loop_time
    backends: Mapping[str, ModelBackend] | None = None
    """The registered `ModelBackend`s, injected. `None` defers to the process-wide
    `@register_backend` registry, which is the harness's own plugin table and not a vendor
    singleton — a test hands in `{"fake": FakeBackend()}` and reaches no network."""
    llm_cache: LlmCacheStore | None = None
    """§11.6's store. Present ⇒ the client is wrapped in `CachingModelClient`; absent ⇒ it is not,
    which is the whole of `--llm-cache off`."""
    llm_cache_mode: CacheMode = "read-write"
    llm_policy: CallPolicy | None = None
    harness_version: str = ""

    model_client: ModelClient = field(init=False, repr=False, compare=False)
    """The ONE call surface every worker gets. Assembled in `__post_init__` from the three fields
    above so that the assembly happens once per run and in one place, and so that no rung can
    quietly build a differently-configured client of its own."""

    llm_findings: LlmFindingSink = field(init=False, repr=False, compare=False)
    """Where the client's `CapabilityDrift` and `BackendFailover` records are PERSISTED.

    Derived rather than injected because it is the client's other half: the client accepts
    `on_drift`/`on_failover` precisely so that it does not have to hold a database handle, and
    for the whole life of that design **nobody supplied either callback** — every drift and
    every failover the fleet computed was discarded at the `is None` guards in `_emit_drift` and
    `_emit_failover`. Building the sink here, beside the client it feeds, is what makes that
    impossible to forget again. `PhaseRunner` drains it; see `orchestrator/findings.py`."""

    def __post_init__(self) -> None:
        """Assemble the client. `object.__setattr__` because the dataclass is frozen and this is
        a derived field, not a mutation of run identity."""
        sink = LlmFindingSink(
            run_id=str(self.run_id),
            writer=self.writer,
            repository=self.repository,
            clock=self.clock,
        )
        object.__setattr__(self, "llm_findings", sink)
        client: ModelClient = LadderModelClient(
            self.llm,
            self.backends,
            policy=self.llm_policy,
            on_drift=sink.on_drift,
            on_failover=sink.on_failover,
        )
        if self.llm_cache is not None:
            client = CachingModelClient(
                client,
                self.llm,
                self.llm_cache,
                mode=self.llm_cache_mode,
                harness_version=self.harness_version,
            )
        object.__setattr__(self, "model_client", client)

    # ---------------------------------------------------------------- derived ceilings

    @property
    def lease_ttl_s(self) -> int:
        return self.config.run.lease_ttl_s

    @property
    def heartbeat_interval_s(self) -> float:
        """Renew at a third of the TTL: two consecutive missed renewals must not be a reclaim,
        or a GC pause becomes a stolen worktree."""
        return max(self.config.run.lease_ttl_s / 3.0, 0.001)

    def wallclock_s(self, phase: Phase) -> int:
        """§11.1: `asyncio.timeout(budgets.task_max_wallclock_s[phase])` wraps every worker."""
        return int(getattr(self.config.budgets.task_max_wallclock_s, _WALLCLOCK_FIELD[phase]))

    def deadline_for(self, phase: Phase) -> float:
        """An ABSOLUTE loop-clock instant, which is what `WorkerContext.deadline` means."""
        return self.monotonic() + self.wallclock_s(phase)

    def worktree(self, repo_id: str) -> Path:
        """`run.work_dir/<repo_id>` — the worktree a phase's lease actually protects."""
        return self.work_dir / repo_id

    # ---------------------------------------------------------------- hand-down

    def worker_context(
        self,
        *,
        repo_id: str,
        phase: Phase,
        attempt: int,
        lease_fence: int,
        cancel: asyncio.Event,
        budget: CallBudget,
        tier: TransformTier = TransformTier.DETERMINISTIC,
        deadline: float | None = None,
    ) -> WorkerContext:
        """Build the worker's whole world. `db` is handed in as the read side by construction:
        `StateRepository` is a `ReadOnlyRepository`, and `WorkerContext.db` is typed as the
        narrow one, so a worker cannot reach a write method it was never meant to see (§11.5).
        """
        return WorkerContext(
            run_id=self.run_id,
            repo_id=repo_id,
            attempt=attempt,
            workdir=str(self.worktree(repo_id)),
            lease_owner=self.lease_owner,
            lease_fence=lease_fence,
            deadline=self.deadline_for(phase) if deadline is None else deadline,
            cancel=cancel,
            budget=budget,
            db=self.repository,
            llm=self.model_client,
            router=self.llm,
            limits=self.limits,
            log=self.log.bind(run_id=str(self.run_id), repo_id=repo_id, phase=int(phase)),
            tier=tier,
        )

    def project(self) -> None:
        """Ask for a `migration_state.json` rebuild after a transition. Never awaits, never
        raises: the projection is an OUTPUT (§11.5), so a stalled projector must not be able to
        stall the wave that feeds it."""
        if self.projector is not None:
            self.projector.request()
