"""The `fleet` Typer application: the operator-facing command surface of SPEC §10 (ADR-0015).

Every verb in §10's table lives here and **nothing else does**. A command in this module is a
thin composition of four things: parse and validate flags, load `FleetSettings` (§9), call into
the real implementation in `state/`, `migrations/`, `graph/`, `llm/` or `orchestrator/`, and map
whatever came back onto a documented exit code. No migration SQL, no DAG arithmetic and no
routing policy is re-implemented here — a CLI that owns logic is a second, untested copy of it.

**Exit codes are the contract** (§10). `0` success; `1` unexpected; `2` usage — which includes a
profile with an unpriced target, `fleet pr --ready` against an unresolved stub, and a second run
against a mirror a live run already owns; `3` run cost; `4` wave wall-clock; `5` memory; `6`
unresolved `severity='error'` collisions or `MANUAL` cycles; `7` `REQUIRES_HUMAN_INTERVENTION` or
`DEGRADED` with an unresolved stub; `8` every backend for a required tier unavailable; `9` disk
exhausted; `10` wave cost, cleared only by `fleet resume --raise-wave-budget`; `11`
`fleet sequence` refused mid-flight. The numbers are **imported** from
`orchestrator/budgets.py` and `orchestrator/runner.py` rather than restated: two sources for
`WAVE_BUDGET_EXIT_CODE` is how CI ends up reading a budget stop as a crash.

`ExitCode` is the single mapping, `_mapped_errors()` the single funnel. A typed error that escapes
as a traceback exits `1`, which tells an operator nothing, so every error this harness raises on
purpose is listed there with the code §10 gives it (Rule 11).

**Nothing here prints an unredacted string.** `_echo`/`_echo_json` run every byte through
`obs/redact.py` before it reaches stdout or stderr, including error messages — a clone URL in a
`ConfigError` is exactly the shape of leak §11.4 exists to stop.

*Agent recommendations* (CLAUDE.md guardrail 1 — these are not in §10):
`--db PATH` is a global flag, because §9 declares no key for the SQLite path and every read-only
verb needs one that is not `state/fleet.db`; and `fleet quarantine` inserts a `SKIPPED` phase-1
row for a repo that has no `phases` rows yet, because "the repo is out of the fleet" has to be
durable somewhere and §10 names `RepoStatus.SKIPPED` as where.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import time
from collections.abc import Callable, Coroutine, Iterator, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import IntEnum, StrEnum
from fnmatch import fnmatch
from pathlib import Path
from typing import Annotated, Any, ClassVar, Final, Literal, NoReturn, TypeVar, cast
from uuid import UUID, uuid4

import aiosqlite
import typer
from pydantic import Field, ValidationError
from typer._click.core import Command as ClickCommand
from typer._click.exceptions import Abort, ClickException
from typer._click.exceptions import Exit as ClickExit
from typer.core import TyperGroup

from fleet import ecosystems
from fleet.bazel.generators import render_gazelle_build, render_root_package
from fleet.bazel.layout import LayoutNode, ReservedDestError, layout, normalize_dest
from fleet.bazel.lockfile import MODULE_LOCK_PATH, check_lock_registry
from fleet.bazel.query import DEFAULT_RDEPS_LIMIT, DEFAULT_SAMPLE_N
from fleet.ecosystems.base import EcosystemAdapter, path_segment
from fleet.graph.build import GraphError, build_graph
from fleet.graph.cycles import break_cycles
from fleet.graph.infer import InferenceInput, OwnerIndex, infer_edges
from fleet.graph.infer import ManifestDependency as InferredDependency
from fleet.graph.query import blast_radii as graph_blast_radii
from fleet.graph.sequence import WavePlan, assign_waves
from fleet.llm.cache import CacheMode
from fleet.llm.client import (
    CallBudget,
    LlmError,
    TierUnavailable,
    UnknownRole,
    discover,
    registry,
)
from fleet.llm.roles import LlmRouter, TierNotConfigured, UnknownProfile
from fleet.manifests.base import ManifestParseError
from fleet.manifests.base import adapter_for as manifest_adapter_for
from fleet.migrations import (
    LATEST_VERSION,
    MigrationError,
    MigrationVersionError,
    current_version,
    migrate,
)
from fleet.models.build import (
    BuildTarget,
    BuildUnit,
    GazelleConfig,
    InternalDep,
    Resolution,
    SupportFile,
    ToolchainRequirement,
    WorkspaceDep,
)
from fleet.models.enums import (
    TERMINAL_STATUSES,
    BreakStrategy,
    ContextPolicy,
    ContractKind,
    ContractStatus,
    Ecosystem,
    EdgeKind,
    Equivalence,
    FailureClass,
    ModelTier,
    NodeKind,
    Phase,
    PrState,
    RepoStatus,
    StubFidelity,
    StubState,
    SymbolKind,
    TransformTier,
    transition,
)
from fleet.models.graph import (
    ContractNode,
    CycleFinding,
    DependencyEdge,
    GraphNode,
    SymbolRef,
)
from fleet.models.repo import Coordinate, ManifestRef, RawDependency, RepoId
from fleet.models.state import SCHEMA_VERSION
from fleet.models.tasks import MAX_ATTEMPTS, PullRequestDraft, VerificationReport
from fleet.obs.log import configure as log_configure
from fleet.obs.redact import redact_text
from fleet.orchestrator.budgets import (
    RUN_BUDGET_EXIT_CODE,
    WAVE_BUDGET_EXIT_CODE,
    Ceilings,
    CostLedger,
    LedgerBreach,
    Limits,
    WaveBudgetExhausted,
    new_cpu_pool,
)
from fleet.orchestrator.context import RunContext, default_logger
from fleet.orchestrator.reentry import (
    BlockerState,
    EvidenceRow,
    RepoEvidence,
    Unblocking,
    demotable_phases,
    evidence_holds,
    plan_unblocking,
    resume_floor,
)
from fleet.orchestrator.registry import get_worker
from fleet.orchestrator.runner import (
    DISK_EXIT_CODE,
    HOST_MEMORY_EXIT_CODE,
    TIER_UNAVAILABLE_EXIT_CODE,
    WAVE_WALLCLOCK_EXIT_CODE,
    PayloadFactory,
    PhaseRunner,
    RunHalted,
    WaveReport,
)
from fleet.orchestrator.scheduler import (
    SqliteSchedulerStore,
    WaveNotReadyError,
    WaveScheduler,
    ordering_descendants,
)
from fleet.rewrite.rules import (
    EngineRegistry,
    EngineUnavailableError,
    ProbeIndeterminateError,
    RewriteRule,
    load_rules,
    rule_matches_path,
)
from fleet.sandbox.container import ContainerSandbox, claims
from fleet.sandbox.worktree import (
    WorktreeError,
    WorktreeManager,
    run_prefix,
    sandbox_name,
)
from fleet.settings import (
    BCR_DEFAULT_REGISTRY,
    ConfigError,
    ConfigFileError,
    FleetConfig,
    FleetSettings,
    GraphSection,
    RepoEntry,
    parse_duration_s,
)
from fleet.state.db import (
    DEFAULT_DB_PATH,
    SchemaVersionError,
    StateDbError,
    StateWriter,
    connect_ro,
    initialize_database,
)
from fleet.state.digest import run_digest
from fleet.state.projection import (
    DEFAULT_PROJECTION_PATH,
    Projector,
    build_state,
    project_once,
)
from fleet.state.repository import (
    AttemptRow,
    BlockedBySnapshotStaleError,
    BlockerResolver,
    EdgeRow,
    EventRow,
    FloorSnapshotStaleError,
    SqliteStateRepository,
    SymbolRow,
)
from fleet.util.fs import atomic_write, scoped_tempdir
from fleet.util.fs import free_bytes as disk_free_bytes
from fleet.util.hashing import sha256_text
from fleet.util.proc import CommandRunner
from fleet.util.proc import run as proc_run
from fleet.vcs import build_forge
from fleet.vcs.commits import discard_task, find_task_commit
from fleet.vcs.filter_repo import (
    FilterRepoUnavailableError,
    IngestError,
    IntegrationMutex,
    RelocationSpec,
    SnapshotRef,
    SourceProvenance,
    ingest,
    integration_snapshot,
    relocate,
    resolve_replace_text,
)
from fleet.vcs.forge import (
    NON_TERMINAL_STATES,
    Forge,
    ForgeError,
    PrStatus,
    PrSyncItem,
)
from fleet.vcs.git import Git, GitCommandError, GitError
from fleet.workers.base import (
    BaseWorker,
    WorkerContext,
    WorkerError,
    WorkerInput,
    WorkerOutput,
    WorkerResult,
    loop_now,
)
from fleet.workers.buildgen import (
    BuildgenInput,
    BuildgenOutput,
    ExternalRequirement,
    materialize,
)
from fleet.workers.buildverify import (
    BuildverifyInput,
    BuildverifyOutput,
    CacheMount,
    StepRecord,
)
from fleet.workers.classify import ClassifyInput, ClassifyOutput
from fleet.workers.clone import CloneInput, CloneOutput, credential_free
from fleet.workers.contracts import (
    CONTRACT_SYMBOL_KINDS,
    ContractsInput,
    ContractsOutput,
    RepoFacts,
)
from fleet.workers.interrogate import InterrogateInput, InterrogateOutput
from fleet.workers.prwriter import (
    DependencyPr,
    PrwriterInput,
    PrwriterOutput,
    PrwriterWorker,
)
from fleet.workers.rdepverify import RdepverifyInput, RdepverifyOutput
from fleet.workers.relocate import RelocateInput, RelocateOutput, relocated_path
from fleet.workers.rewrite import RewriteInput, RewriteOutput
from fleet.workers.symbolindex import SymbolIndexInput, SymbolIndexOutput

__all__ = [
    "ExitCode",
    "FleetCliError",
    "app",
    "build_app",
    "command_paths",
]

T = TypeVar("T")

DEFAULT_CONFIG_PATH: Final = Path("config/fleet.yaml")
HARNESS_VERSION: Final = "0.1.0"


# --------------------------------------------------------------------------------------
# exit codes — imported, never redefined
# --------------------------------------------------------------------------------------


class ExitCode(IntEnum):
    """SPEC §10's table. The four halt codes are aliases of the constants the halt paths raise,
    so a change in `budgets.py`/`runner.py` cannot leave CI reading a stale number here."""

    SUCCESS = 0
    UNEXPECTED_ERROR = 1
    USAGE = 2
    RUN_COST_EXHAUSTED = RUN_BUDGET_EXIT_CODE                 # 3
    WAVE_WALL_CLOCK_EXHAUSTED = WAVE_WALLCLOCK_EXIT_CODE      # 4
    MEMORY_EXHAUSTED = HOST_MEMORY_EXIT_CODE                  # 5
    UNRESOLVED_FINDINGS = 6
    REQUIRES_HUMAN_INTERVENTION = 7
    TIER_UNAVAILABLE = TIER_UNAVAILABLE_EXIT_CODE             # 8
    DISK_EXHAUSTED = DISK_EXIT_CODE                           # 9
    WAVE_COST_EXHAUSTED = WAVE_BUDGET_EXIT_CODE               # 10
    SEQUENCE_REFUSED = 11


class FleetCliError(RuntimeError):
    """A deliberate refusal with a documented exit code. Never a traceback (Rule 11)."""

    exit_code: int = ExitCode.UNEXPECTED_ERROR

    def __init__(self, message: str, *, exit_code: int | None = None) -> None:
        super().__init__(message)
        if exit_code is not None:
            self.exit_code = exit_code


class UsageError(FleetCliError):
    """§10 exit 2: the operator must edit a file, a flag or a stub before retrying."""

    exit_code = ExitCode.USAGE


class UnresolvedFindingsError(FleetCliError):
    """§10 exit 6: `severity='error'` collisions or `MANUAL` cycles still stand."""

    exit_code = ExitCode.UNRESOLVED_FINDINGS


class HumanInterventionError(FleetCliError):
    """§10 exit 7: the run finished, but a repo is `REQUIRES_HUMAN_INTERVENTION`/`DEGRADED`."""

    exit_code = ExitCode.REQUIRES_HUMAN_INTERVENTION


class DiskExhaustedError(FleetCliError):
    """§10 exit 9: `budgets.max_disk_gb` / `preflight.min_free_bytes` breached after eviction."""

    exit_code = ExitCode.DISK_EXHAUSTED


class SequenceRefusedError(FleetCliError):
    """§10 exit 11: the fleet is in flight. Deliberately NOT 3 — nothing changed, nothing was
    spent, and no `halted = 1` ledger is waiting for `--raise-budget`."""

    exit_code = ExitCode.SEQUENCE_REFUSED


class CommandUnavailableError(FleetCliError):
    """The verb has no implementation in this CLI: it validates its preconditions and stops.

    Exit 1 with a module named, rather than a silent no-op: exit 1 IS "an unexpected error", and
    an operator who is told where the gap is can act on it. The name is a POINTER, never a verdict
    on that module. `_unavailable`'s text used to assert the named module "still raises
    `NotImplementedError`" — false for every module it named, and D63 in
    `docs/INTEGRATION_HONESTY.md` is the ledger entry for it.

    The first sentence holds because `_unavailable` is this class's ONLY raise site: an `ast`
    sweep of `src/` for an `ast.Raise` of this name returns exactly one, `_unavailable` itself.
    The predicate is the AST one and not a `grep -c`, which also counts prose. A second raiser,
    for a verb that IS implemented but stops part-way, would falsify the first sentence; that
    case is carried by separate classes rather than by a second raise here, and by more than one
    of them — `ScanStepUnavailableError`, `TransformStepUnavailableError` and
    `BuildStepUnavailableError`, each for a step of a verb that ran.

    **The enumeration above named a fourth, `ResumeIncompleteError`, until ADR-0080 (round G).**
    That class existed because `fleet resume` stopped on a step of its own; §11.5 step 8 is
    wired now, so the class was deleted rather than repurposed — ADR-0076's own closing bullet
    required exactly that. The sentence about "a definite article over four" went with it: there
    are three, and the exit-code caveat it carried was about the deleted member.
    """

    exit_code = ExitCode.UNEXPECTED_ERROR


# --------------------------------------------------------------------------------------
# flag value types
# --------------------------------------------------------------------------------------


class LlmCacheMode(StrEnum):
    """§10 `--llm-cache {read-write,read-only,off}` — §11.6's three modes, as a Typer choice."""

    READ_WRITE = "read-write"
    READ_ONLY = "read-only"
    OFF = "off"


class OutputFormat(StrEnum):
    TABLE = "table"
    JSON = "json"
    DOT = "dot"


class BreakCyclesMode(StrEnum):
    AUTO = "auto"
    MANUAL = "manual"


class Revalidation(StrEnum):
    EAGER = "eager"
    BATCHED = "batched"
    MANUAL = "manual"


# --------------------------------------------------------------------------------------
# global options
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GlobalOptions:
    """§10's "global flags on every command", parsed once by the group callback.

    A value object, deliberately: the callback must stay side-effect-free because click runs it
    before `<subcommand> --help` is rendered, and a `--help` that opens a database is a `--help`
    that fails on a host with no database.
    """

    config_path: Path = DEFAULT_CONFIG_PATH
    db_path: Path = DEFAULT_DB_PATH
    run: str | None = None
    log_level: str = "INFO"
    json_output: bool = False
    llm_cache: LlmCacheMode | None = None
    profile: str | None = None
    max_cost_usd: float | None = None
    max_rss_mb: int | None = None

    @property
    def config_dir(self) -> Path:
        return self.config_path.parent

    @property
    def cache_mode(self) -> CacheMode | None:
        """`--llm-cache` as the `Literal` §11.6's `CachingModelClient` takes — or `None`.

        `None` is "the flag was not typed", and it is NOT a synonym for `read-write`: the
        resolved mode then comes from `fleet.yaml`'s `llm.cache_mode`, which an operator may
        have set to `off`. Collapsing the two here is how a flag default silently overwrites a
        config value the operator did write; `_load_settings` therefore overrides on `is not
        None`, never unconditionally. The RESOLVED mode is `settings.config.llm.cache_mode`,
        and that is what a command must report or act on.
        """
        match self.llm_cache:
            case LlmCacheMode.READ_ONLY:
                return "read-only"
            case LlmCacheMode.OFF:
                return "off"
            case LlmCacheMode.READ_WRITE:
                return "read-write"
            case _:
                return None


def _options(ctx: typer.Context) -> GlobalOptions:
    obj = ctx.find_root().obj
    return obj if isinstance(obj, GlobalOptions) else GlobalOptions()


# --------------------------------------------------------------------------------------
# output — every byte through §11.4
# --------------------------------------------------------------------------------------


def _echo(text: str, *, err: bool = False) -> None:
    """The ONE stdout/stderr door. `redact_text` first, always: a repo URL carrying a
    `github_pat_…` reaches this function from a config error, a git message and a PR body alike,
    and a redactor applied at three of those four sites is not applied at all (§11.4)."""
    typer.echo(redact_text(text), err=err)


def _echo_json(payload: object) -> None:
    """JSON output, redacted after serialization so nested values are covered too."""
    _echo(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _emit(opts: GlobalOptions, payload: Mapping[str, object], lines: Sequence[str]) -> None:
    """`--json` machine output or the human rendering — never both, never neither."""
    if opts.json_output:
        _echo_json(dict(payload))
    else:
        for line in lines:
            _echo(line)


def _fail(message: str, code: int) -> NoReturn:
    _echo(f"error: {message}", err=True)
    raise typer.Exit(code)


@contextmanager
def _mapped_errors() -> Iterator[None]:
    """THE error funnel: every typed failure to its §10 code, nothing to a traceback.

    Ordered most-specific first. `click`'s own exceptions pass through untouched so `--help`,
    `--version` and Typer's own usage errors keep their behaviour.
    """
    try:
        yield
    except (ClickExit, ClickException, Abort):
        raise
    except FleetCliError as exc:
        _fail(str(exc), exc.exit_code)
    except ConfigError as exc:
        _fail(str(exc), ConfigError.exit_code)
    except (UnknownProfile, UnknownRole, TierNotConfigured) as exc:
        _fail(f"config/models.yaml: {exc}", ExitCode.USAGE)
    except TierUnavailable as exc:
        _fail(str(exc), ExitCode.TIER_UNAVAILABLE)
    except RunHalted as exc:
        _fail(str(exc), exc.exit_code)
    except LedgerBreach as exc:
        code = exc.exit_code
        _fail(str(exc), ExitCode.UNEXPECTED_ERROR if code is None else code)
    except MigrationVersionError as exc:
        _fail(str(exc), ExitCode.USAGE)
    except SchemaVersionError as exc:
        _fail(str(exc), ExitCode.USAGE)
    except MigrationError as exc:
        _fail(str(exc), ExitCode.UNEXPECTED_ERROR)
    except GraphError as exc:
        _fail(str(exc), ExitCode.UNRESOLVED_FINDINGS)
    except LlmError as exc:
        _fail(str(exc), ExitCode.USAGE)
    except StateDbError as exc:
        _fail(str(exc), ExitCode.UNEXPECTED_ERROR)


def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    """Drive one async unit of work. The CLI owns the event loop; nothing below it does."""
    return asyncio.run(coro)


# --------------------------------------------------------------------------------------
# settings, database, run identity
# --------------------------------------------------------------------------------------


def _load_settings(opts: GlobalOptions) -> FleetSettings:
    """§9's loader, with `--profile` and `--max-cost-usd` folded in as CLI overrides.

    `--profile` is routed here and nowhere else, so it reaches `LlmRouter` and the `llm_cache`
    key through `FleetSettings.profile` exactly as §10 describes — the CLI never carries a second
    notion of "the active profile" beside the settings object's.

    **§7.7's startup step lives here, not in the `main` callback.** `llm.discover()` is the fifth
    registry's walk, and unlike `ecosystems.discover()` it is NOT total over an enum — backends are
    open-ended. What it IS total over is the active profile (§13 row 36), and the only place that
    can be checked is where the profile is resolved: `known_backends` below is the §9 rule 2 gate's
    name set, and passing the LIVE registry instead of settings' `SHIPPED_BACKENDS` fallback is
    what makes "every `backend` resolves" mean *registered* rather than *spelled like a name we
    ship*. Without this call the registry is empty at run time, the gate passes on a hard-coded
    tuple, and `UnknownBackend` surfaces in wave 7 with repos already cloned — vacuously satisfying
    row 36's "checked at RunContext construction". It also feeds `registry()` for
    `fleet models check`, and `RunContext(backends=None)`, both of which read the same table.

    Idempotent: `import_module` is `sys.modules`-cached, so a second call re-registers nothing and
    cannot trip `register_backend`'s duplicate check.
    """
    overrides: dict[str, Any] = {}
    if opts.profile is not None:
        overrides["llm.profile"] = opts.profile
    if opts.max_rss_mb is not None:
        overrides["budgets.max_rss_mb"] = opts.max_rss_mb
    # §11.6's `--llm-cache` reaches the run as `llm.cache_mode` and nowhere else, so
    # `RunContext.__post_init__` reads ONE resolved value and the CLI carries no second notion
    # of "the active cache mode" — the same rule `overrides["llm.profile"]` above is held to.
    # `is not None` is load-bearing: the flag's absence must leave `fleet.yaml` alone.
    if (cache_mode := opts.cache_mode) is not None:
        overrides["llm.cache_mode"] = cache_mode

    backends = tuple(discover())
    settings = FleetSettings.load(
        opts.config_dir, cli_overrides=overrides, known_backends=backends
    )
    if opts.max_cost_usd is None:
        return settings

    declared = settings.config.budgets.run_max_cost_usd
    if opts.max_cost_usd > declared:
        raise UsageError(
            f"--max-cost-usd {opts.max_cost_usd} raises budgets.run_max_cost_usd above the "
            f"{declared} declared in {opts.config_path}. §10: the flag overrides DOWNWARD only — "
            "raising a run ceiling is `fleet resume --raise-budget`, which is audited."
        )
    overrides["budgets.run_max_cost_usd"] = opts.max_cost_usd
    return FleetSettings.load(
        opts.config_dir, cli_overrides=overrides, known_backends=backends
    )


def llm_router(settings: FleetSettings) -> LlmRouter:
    """The ADR-0023 router for the ACTIVE profile — the one `--profile` selected."""
    return LlmRouter.from_models_config(settings.models, profile=settings.profile)


def _require_db(opts: GlobalOptions) -> Path:
    path = opts.db_path
    if not path.exists():
        raise UsageError(
            f"{path} does not exist. A fresh database is created by `fleet migrate-db` "
            "(§6, §10: the only path in the harness that executes DDL)."
        )
    return path


def _check_schema_version(path: Path) -> int:
    """Every command except `migrate-db` READS `user_version` and refuses on a mismatch (§6)."""
    version = current_version(path)
    if version != SCHEMA_VERSION:
        raise UsageError(
            f"{path} is at user_version={version}, this harness compiles {SCHEMA_VERSION}. "
            "Run `fleet migrate-db` — no other command applies DDL underneath a live run (§6)."
        )
    return version


async def _resolve_run(conn: aiosqlite.Connection, opts: GlobalOptions) -> str:
    """`--run ID`, or §10's "default: latest run"."""
    if opts.run is not None:
        async with conn.execute(
            "SELECT run_id FROM runs WHERE run_id = ?", (opts.run,)
        ) as cursor:
            if await cursor.fetchone() is None:
                raise UsageError(f"no run {opts.run!r} in {opts.db_path}")
        return opts.run
    async with conn.execute(
        "SELECT run_id FROM runs ORDER BY started_at DESC, run_id DESC LIMIT 1"
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise UsageError(
            f"{opts.db_path} holds no runs; `fleet scan` creates one (§10). "
            "Pass --run explicitly if you meant a different database."
        )
    return str(row[0])


async def _rows(
    conn: aiosqlite.Connection, sql: str, params: tuple[object, ...] = ()
) -> list[tuple[Any, ...]]:
    """One SELECT, fully materialized. `Any` cells because SQLite's dynamic typing is the source:
    every call site immediately narrows with `int()`/`float()`/`str()`, which is where the real
    contract is asserted."""
    async with conn.execute(sql, params) as cursor:
        return [tuple(row) for row in await cursor.fetchall()]


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def _fingerprint(*parts: str) -> str:
    from fleet.util.hashing import sha256_text

    return sha256_text("\x00".join(parts))


# --------------------------------------------------------------------------------------
# the in-flight predicate (§3.1, exit 11) and the mirror mutex (§10, exit 2)
# --------------------------------------------------------------------------------------


async def _in_flight_repos(conn: aiosqlite.Connection, run_id: str) -> tuple[str, ...]:
    """Repos this run has already started moving: anything past `PENDING` and not terminal.

    §3.1's resequence refusal is about *the fleet being in flight*, not about the process being
    alive: a `RUNNING` row left by a crash still means waves were computed, work was admitted
    against them, and renumbering them now would move a repo that is mid-transform into another
    wave. `fleet resume` reconciles that; `--force-resequence` overrides it deliberately.

    A **completed Phase 1** is the one non-PENDING state that is not "in flight", and excluding
    it is not a loosening: `fleet sequence` IS Phase 1 steps 6–8, so its whole input is a fleet
    whose scan has finished. Counting a `SUCCEEDED` SCAN row as in-flight made the documented
    order — `fleet scan` then `fleet sequence` — refuse itself with exit 11 on every run that
    scanned even one repo. Nothing has been transformed at that point, so nothing can be moved
    out from under.
    """
    rows = await _rows(
        conn,
        "SELECT DISTINCT repo_id, phase, status FROM phases "
        " WHERE run_id = ? AND status NOT IN ('PENDING', 'SKIPPED') "
        "   AND NOT (phase = ? AND status = 'SUCCEEDED') ORDER BY repo_id",
        (run_id, int(Phase.SCAN)),
    )
    return tuple(f"{row[0]}({row[2]})" for row in rows)


async def _gated_repos(conn: aiosqlite.Connection, run_id: str) -> tuple[str, ...]:
    """Repos Phase 1 removed from the fleet: `SKIPPED` by the step-1 gate or by config, and
    `REQUIRES_HUMAN_INTERVENTION` by preflight. §3.1 step 7 never assigns any of them a wave."""
    rows = await _rows(
        conn,
        "SELECT DISTINCT repo_id FROM phases "
        " WHERE run_id = ? AND phase = ? "
        "   AND status IN ('SKIPPED', 'REQUIRES_HUMAN_INTERVENTION') ORDER BY repo_id",
        (run_id, int(Phase.SCAN)),
    )
    return tuple(str(row[0]) for row in rows)


def _mirror_lock_path(settings: FleetSettings, run_id: str) -> Path:
    """`vcs/filter_repo.py`'s `integration:<run_id>` mutex file, without taking it."""
    from fleet.sandbox.worktree import slug

    monorepo = (settings.root / settings.config.run.monorepo_path).resolve()
    return monorepo / ".git" / f"fleet-integration-{slug(run_id)}.lock"


def _refuse_concurrent_mirror_run(settings: FleetSettings, run_id: str) -> None:
    """§10 exit 2: "a second run started against a mirror another live run already owns".

    The lock file is advisory and per-file, so a non-blocking `flock` that fails is exactly the
    "another live run owns this mirror" signal — and two runs over one mirror corrupt both, which
    is why this is a refusal and not a wait.
    """
    import fcntl
    import os

    lock = _mirror_lock_path(settings, run_id)
    if not lock.exists():
        return
    fd = os.open(lock, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise UsageError(
            f"the integration mirror mutex {lock} is held by another live run: two runs over one "
            "mirror corrupt both (§10). Wait for it, or `fleet abort` the run that owns it."
        ) from None
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# --------------------------------------------------------------------------------------
# the app
# --------------------------------------------------------------------------------------

app = typer.Typer(
    name="fleet",
    help="Polyglot monorepo migration harness (SPEC §10).",
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
contracts_app = typer.Typer(help="Read-only view of the contract nodes (§3.1 step 5b).")
stubs_app = typer.Typer(help="The stub lifecycle (§3.5.1). Retires stubs; never creates one.")
models_app = typer.Typer(help="The LLM routing surface (ADR-0023).")
app.add_typer(contracts_app, name="contracts")
app.add_typer(stubs_app, name="stubs")
app.add_typer(models_app, name="models")


@app.callback()
def main_callback(
    ctx: typer.Context,
    config: Annotated[
        Path, typer.Option("--config", help="Path to config/fleet.yaml (§9).")
    ] = DEFAULT_CONFIG_PATH,
    db: Annotated[
        Path, typer.Option("--db", help="SQLite state database (agent recommendation).")
    ] = DEFAULT_DB_PATH,
    run: Annotated[
        str | None, typer.Option("--run", help="Run id. Default: the latest run.")
    ] = None,
    log_level: Annotated[str, typer.Option("--log-level")] = "INFO",
    json_output: Annotated[
        bool, typer.Option("--json", help="Machine-readable stdout.")
    ] = False,
    llm_cache: Annotated[
        LlmCacheMode | None,
        typer.Option("--llm-cache", help="§11.6 cache mode. Default: fleet.yaml llm.cache_mode."),
    ] = None,
    profile: Annotated[
        str | None, typer.Option("--profile", help="config/models.yaml profile (ADR-0023).")
    ] = None,
    max_cost_usd: Annotated[
        float | None,
        typer.Option("--max-cost-usd", help="Lower budgets.run_max_cost_usd. DOWNWARD only."),
    ] = None,
    max_rss_mb: Annotated[int | None, typer.Option("--max-rss-mb")] = None,
) -> None:
    """Global flags (§10), plus §7.5's one startup step.

    `ecosystems.discover()` belongs here and nowhere later: it imports every adapter module and
    asserts the registry is a **total bijection** over `Ecosystem`, so an `Ecosystem` member with
    no adapter is a startup error rather than a crash on whichever repo in wave 14 happens to be
    the first of that language. It reads no file, no clock and no database — the callback stays
    side-effect free in the sense that matters, which is that click runs it before
    `<verb> --help` renders.
    """
    ecosystems.discover()
    ctx.obj = GlobalOptions(
        config_path=config,
        db_path=db,
        run=run,
        log_level=log_level,
        json_output=json_output,
        llm_cache=llm_cache,
        profile=profile,
        max_cost_usd=max_cost_usd,
        max_rss_mb=max_rss_mb,
    )


# --------------------------------------------------------------------------------------
# fleet migrate-db — THE only DDL path (§6, §10)
# --------------------------------------------------------------------------------------


@app.command("migrate-db")
def migrate_db(
    ctx: typer.Context,
    to: Annotated[int | None, typer.Option("--to", help="Stop at this user_version.")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    backup: Annotated[Path | None, typer.Option("--backup", help="Copy the db here first.")] = None,
) -> None:
    """Bring the state database to this harness's schema version. The ONLY DDL path (§6).

    Two branches, and conflating them is the defect this wiring closes: a database at
    `user_version = 0` has **no schema**, so the ladder cannot lift it and `migrate()` refuses it
    by name — it is created from `schema.sql` by `state/db.py::initialize_database()`, landing
    directly at the baseline. A database that already holds data goes through the ordered
    `vNNN_*.py` steps instead, one `BEGIN EXCLUSIVE` transaction each.
    """
    opts = _options(ctx)
    with _mapped_errors():
        path = opts.db_path
        fresh = not path.exists() or _safe_user_version(path) == 0

        if to is not None and to != LATEST_VERSION:
            raise UsageError(
                f"--to {to}: this harness's ladder ends at {LATEST_VERSION} and migrations are "
                "FORWARD-ONLY with no partial target (§6). Upgrade the harness for a later one."
            )
        if dry_run:
            plan = (
                f"initialize {path} from schema.sql → user_version {SCHEMA_VERSION}"
                if fresh
                else f"apply the ladder {_safe_user_version(path)} → {LATEST_VERSION}"
            )
            _emit(
                opts,
                {"dry_run": True, "fresh": fresh, "plan": plan, "path": str(path)},
                [f"dry-run: would {plan}"],
            )
            return

        if backup is not None and path.exists():
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backup)
            _echo(f"backed up {path} → {backup}")

        if fresh:
            version = _run(initialize_database(path))
            _emit(
                opts,
                {"path": str(path), "fresh": True, "from": 0, "to": version},
                [f"initialized {path} from schema.sql at user_version={version}"],
            )
            return

        before, after = migrate(path)
        _emit(
            opts,
            {"path": str(path), "fresh": False, "from": before, "to": after},
            [
                f"{path}: user_version {before} → {after}"
                if before != after
                else f"{path}: already at user_version {after}; nothing to do"
            ],
        )


def _safe_user_version(path: Path) -> int:
    """`PRAGMA user_version` of a file that may not be a database yet. 0 means "no schema"."""
    if not path.exists():
        return 0
    try:
        return current_version(path)
    except (MigrationError, sqlite3.DatabaseError):
        return 0


# --------------------------------------------------------------------------------------
# fleet scan / plan / build / verify / migrate — Phase drivers
# --------------------------------------------------------------------------------------


def _unavailable(verb: str, module: str) -> NoReturn:
    """The verb has no code path: `_phase_preflight` ran, and there is nothing after it.

    **The message's own precondition: every caller runs `_phase_preflight` first**, which is what
    licenses the list of validated checks. All three call sites (`plan`, `migrate`, `stubs
    resolve`) do; a future caller that does not must not use this helper.

    It deliberately asserts NOTHING about the named module. The two modules the call sites name,
    `workers/relocate.py` and `workers/buildverify.py`, are implemented — `RelocateWorker` is
    dispatched by `fleet transform` and `BuildverifyWorker` by `fleet build` — so the gap is a
    missing driver in this file, not a missing worker body (D63).
    """
    raise CommandUnavailableError(
        f"`fleet {verb}` cannot run: this verb has no implementation in the CLI. Its "
        "preconditions validated (§9 config, §6 schema version, the run identity, §10 mirror "
        "mutex) and it then stopped without dispatching any work. Nothing was written. "
        f"Related module: {module}."
    )


def _phase_preflight(ctx: typer.Context) -> tuple[GlobalOptions, FleetSettings, str]:
    """Everything a phase verb can honestly do today: §9 config, §6 schema, §10 mirror mutex.

    Shared rather than copied, because every one of these is a documented exit-2 refusal and a
    verb that skips one is a verb that starts a run it should have refused.
    """
    opts = _options(ctx)
    settings = _load_settings(opts)
    path = _require_db(opts)
    _check_schema_version(path)
    run_id = _run(_with_ro(path, lambda conn: _resolve_run(conn, opts)))
    _refuse_concurrent_mirror_run(settings, run_id)
    return opts, settings, run_id


async def _with_ro[T](
    path: Path, work: Callable[[aiosqlite.Connection], Coroutine[Any, Any, T]]
) -> T:
    conn = await connect_ro(path)
    try:
        return await work(conn)
    finally:
        await conn.close()


@app.command()
def scan(
    ctx: typer.Context,
    repos: Annotated[Path, typer.Option("--repos")] = Path("config/repos.yaml"),
    only: Annotated[str | None, typer.Option("--only", help="Glob over repo names.")] = None,
    refresh: Annotated[bool, typer.Option("--refresh")] = False,
    concurrency: Annotated[int | None, typer.Option("--concurrency")] = None,
    skip_symbols: Annotated[bool, typer.Option("--skip-symbols")] = False,
    skip_classify: Annotated[
        bool,
        typer.Option("--skip-classify", help="Skip the ONE model-bearing step (ADR-0008)."),
    ] = False,
    skip_contracts: Annotated[bool, typer.Option("--skip-contracts")] = False,
    preflight_only: Annotated[bool, typer.Option("--preflight-only")] = False,
    symbol_batch_rows: Annotated[int | None, typer.Option("--symbol-batch-rows")] = None,
) -> None:
    """Phase 1 steps 1–5b: preflight, mirrors, manifests, coordinates, symbols, edges, contracts.

    Step 5b runs unless `--skip-contracts` (or `scan.contracts.enabled: false` /
    `graph.hoist_contracts: false`) says otherwise, in which case every node in the graph is a
    repo and §3.1 behaves exactly as it did before ADR-0019.
    """
    opts = _options(ctx)
    with _mapped_errors():
        settings = _load_settings(opts)
        # §11.3/§12.22: evict the Bazel disk cache to `budgets.max_disk_gb`, then refuse to
        # start at all if the volume is still under `preflight.min_free_bytes` (exit 9). A phase
        # that consumes gigabytes does not get to discover ENOSPC inside a write transaction.
        _require_disk_headroom(settings)
        path = _require_db(opts)
        _check_schema_version(path)
        _validate_scan_flags(
            opts,
            repos=repos,
            refresh=refresh,
            preflight_only=preflight_only,
            concurrency=concurrency,
        )
        run_id = _run(_with_ro(path, lambda conn: _scan_run_id(conn, opts)))
        _refuse_concurrent_mirror_run(settings, run_id)
        result = _run(
            _scan_impl(
                opts,
                settings,
                path,
                run_id=run_id,
                only=only,
                concurrency=concurrency,
                skip_symbols=skip_symbols,
                skip_classify=skip_classify,
                skip_contracts=skip_contracts,
                symbol_batch_rows=symbol_batch_rows,
            )
        )
        _emit(
            opts,
            result,
            [
                f"run {result['run_id']}: {result['succeeded']} scanned, "
                f"{result['skipped']} skipped, {result['failed']} needing a human "
                f"({result['edges']} edges over {result['repos']} repos)"
            ],
        )
        code = int(str(result["exit_code"]))
        if code == ExitCode.REQUIRES_HUMAN_INTERVENTION:
            raise HumanInterventionError(
                f"{result['failed']} repo(s) ended REQUIRES_HUMAN_INTERVENTION: "
                f"{', '.join(str(name) for name in cast('list[str]', result['attention']))}"
            )
        if code != ExitCode.SUCCESS:
            raise FleetCliError(str(result["halt"]), exit_code=code)


# --------------------------------------------------------------------------------------
# fleet scan — the Phase 1 pipeline, composed from the REGISTERED workers (§3.1 steps 1–5)
# --------------------------------------------------------------------------------------

SCAN_WAVE_INDEX: Final = 0
"""Scan has no dependency order to respect — the DAG is its OUTPUT, not its input — so the whole
fleet is one synthetic wave. It is deliberately NOT written to `waves`: those rows are
`fleet sequence`'s, and planting a pseudo-plan there would leave a stale member behind for every
repo the real plan later excludes."""

SCAN_UNITS: Final[tuple[str, ...]] = ("clone", "interrogate", "classify", "symbolindex")
"""§3.1 steps 1–4, in order, and each name is the REGISTRY key of the worker that performs it —
`get_worker(unit)`, never a constructor call on a concrete class (§7.2)."""

CONTRACTS_SCOPE: Final = "contracts"
"""The `repo_id` §3.1 step 5b's dispatch runs under. 5b is FLEET-wide — it is re-derived for the
whole run in one transaction — so there is no repo to name; this value only reaches
`WorkerContext.workdir` and the log binding, and the worker touches neither. It is not a `repos`
row and never becomes one."""


class ScanStepUnavailableError(FleetCliError):
    """A step §3.1 names has no implementation, and the run would silently omit its output.

    Distinct from `CommandUnavailableError`: the verb runs, and what is missing is one step of it.
    Silence here is the expensive failure — a graph with no contract nodes looks exactly like a
    fleet that shares no contracts, and nothing downstream can tell those apart (Rule 11).
    """

    exit_code = ExitCode.UNEXPECTED_ERROR


class ScanInput(WorkerInput):
    """One repo's whole Phase 1 dispatch. Flat scalars, because each step's own payload model is
    built from them here rather than nested — a nested payload would let a step's defaults
    disagree with the run's `config/fleet.yaml`."""

    repo_id: RepoId
    url: str = Field(min_length=1)
    cache_dir: str = Field(min_length=1)
    steps: tuple[str, ...] = SCAN_UNITS
    ignore_globs: tuple[str, ...] = ()
    branch_fallbacks: tuple[str, ...] = ("main", "master", "trunk", "develop")
    max_repo_bytes: int = Field(default=5_368_709_120, gt=0)
    max_blob_bytes: int = Field(default=104_857_600, gt=0)
    min_free_bytes: int = Field(
        default=0,
        ge=0,
        description="`preflight.min_free_bytes`, re-checked before EVERY clone rather than once "
        "at startup (§11.3) — the fleet fills the volume as it runs",
    )
    unshallow: bool = True
    require_lfs_binary: bool = True
    max_file_bytes: int = Field(default=2_097_152, gt=0)
    symbol_batch_rows: int = Field(default=5000, gt=0)
    max_symbols_per_repo: int = Field(default=500_000, gt=0)
    resource_patterns: dict[str, str] = Field(default_factory=dict)
    dynamic_patterns: dict[str, str] = Field(default_factory=dict)
    remaining_units: tuple[str, ...] | None = None


class ScanOutput(WorkerOutput):
    """Everything one repo's scan landed, typed per step.

    A `dict[str, WorkerOutput]` was the obvious shape and is wrong: pydantic would validate every
    value back into the *base* class and drop the fields that make it evidence. Four declared
    fields cost four lines and keep `manifests`, `coordinates` and `symbols` recoverable.
    """

    repo_id: RepoId
    clone: CloneOutput | None = None
    interrogate: InterrogateOutput | None = None
    classify: ClassifyOutput | None = None
    symbol_batches: tuple[SymbolIndexOutput, ...] = ()
    findings: tuple[str, ...] = ()


class ScanPipelineWorker(BaseWorker[ScanInput, ScanOutput]):
    """§3.1 steps 1–4 as ONE dispatch of ONE phase, chaining the four registered workers.

    Why one dispatch and not four: `phases` holds a single row per `(run, repo, phase)` and
    `acquire_phase_lease` only moves a **PENDING** one. A second `PhaseRunner` pass over
    `Phase.SCAN` therefore finds every repo already terminal and admits nothing — the four steps
    would look like they ran while three of them never did. Chaining them under one lease also
    makes the §11.5 checkpoint mean what it says: `completed_units` is a list of *steps*, so a
    resumed scan re-enters at the step that did not land instead of re-walking the tree.

    Each step is fetched from the registry by name (§7.2). Nothing here constructs a concrete
    worker class, so a worker swapped behind its registry key is swapped here too.
    """

    __slots__ = ("_workers",)

    name: ClassVar[str] = "scan"
    phase: ClassVar[Phase] = Phase.SCAN
    input_model: ClassVar[type[WorkerInput]] = ScanInput
    output_model: ClassVar[type[WorkerOutput]] = ScanOutput

    def __init__(self) -> None:
        self._workers: Mapping[str, BaseWorker[Any, Any]] = {
            unit: get_worker(unit)() for unit in SCAN_UNITS
        }

    async def preconditions_hold(self, ctx: WorkerContext, payload: ScanInput) -> bool:
        """Re-entry is admitted only for a checkpoint whose worktree is still on disk.

        The steps this pipeline chains each own a finer precondition over their own units; what
        this one adds is the fact they all share — a worktree that has been reaped makes every
        landed step's evidence a claim about a tree nobody can read, so the phase re-runs whole.
        """
        if payload.remaining_units is None:
            return False
        return await asyncio.to_thread(Path(ctx.workdir).is_dir)

    async def run(self, ctx: WorkerContext, payload: ScanInput) -> WorkerResult[ScanOutput]:
        owed = [
            unit
            for unit in payload.steps
            if payload.remaining_units is None or unit in payload.remaining_units
        ]
        landed = [unit for unit in payload.steps if unit not in owed]
        state = _ScanState(repo_id=payload.repo_id)

        for index, unit in enumerate(owed):
            if ctx.cancelled() or ctx.expired(loop_now()):
                return self._interrupted(landed, owed[index:], state)
            failure = await self._step(unit, ctx, payload, state)
            if failure is not None:
                return failure
            landed.append(unit)
            if unit == "clone" and state.gated:
                # §3.1 step 1: an empty repo is "SKIPPED, not an error". There is no commit, so
                # no worktree was cut and steps 2-4 have nothing to read. Stopping here is the
                # whole of not crashing the fleet on one bad repo; the gate itself is applied by
                # the driver, which is the only thing allowed to write a status.
                break

        return WorkerResult[ScanOutput](
            status="ok",
            output=state.output(),
            completed_units=landed,
            evidence=[ctx.workdir],
        )

    # -- steps ---------------------------------------------------------------------------

    async def _step(
        self, unit: str, ctx: WorkerContext, payload: ScanInput, state: _ScanState
    ) -> WorkerResult[ScanOutput] | None:
        """Run one step. `None` means it landed; a result means the whole dispatch stops."""
        match unit:
            case "clone":
                return await self._clone(ctx, payload, state)
            case "interrogate":
                return await self._interrogate(ctx, payload, state)
            case "classify":
                return await self._classify(ctx, payload, state)
            case _:
                return await self._symbolindex(ctx, payload, state)

    async def _clone(
        self, ctx: WorkerContext, payload: ScanInput, state: _ScanState
    ) -> WorkerResult[ScanOutput] | None:
        worker = cast("BaseWorker[CloneInput, CloneOutput]", self._workers["clone"])
        result = await worker.run(
            ctx,
            CloneInput(
                repo_id=payload.repo_id,
                url=payload.url,
                cache_dir=payload.cache_dir,
                branch_fallbacks=payload.branch_fallbacks,
                max_repo_bytes=payload.max_repo_bytes,
                max_blob_bytes=payload.max_blob_bytes,
                min_free_bytes=payload.min_free_bytes,
                unshallow=payload.unshallow,
                require_lfs_binary=payload.require_lfs_binary,
            ),
        )
        if result.output is not None:
            state.clone = result.output
            state.findings.extend(result.output.findings)
            state.gated = not result.output.preflight_ok
        return self._halt_on(result, state)

    async def _interrogate(
        self, ctx: WorkerContext, payload: ScanInput, state: _ScanState
    ) -> WorkerResult[ScanOutput] | None:
        worker = cast(
            "BaseWorker[InterrogateInput, InterrogateOutput]", self._workers["interrogate"]
        )
        result = await worker.run(
            ctx,
            InterrogateInput(repo_id=payload.repo_id, ignore_globs=payload.ignore_globs),
        )
        if result.output is not None:
            state.interrogate = result.output
            state.findings.extend(result.output.findings)
        return self._halt_on(result, state)

    async def _classify(
        self, ctx: WorkerContext, payload: ScanInput, state: _ScanState
    ) -> WorkerResult[ScanOutput] | None:
        worker = cast("BaseWorker[ClassifyInput, ClassifyOutput]", self._workers["classify"])
        manifests = () if state.interrogate is None else state.interrogate.manifests
        result = await worker.run(
            ctx,
            ClassifyInput(
                repo_id=payload.repo_id,
                manifest_paths=tuple(ref.path for ref in manifests),
                ecosystems=() if state.interrogate is None else state.interrogate.ecosystems,
            ),
        )
        if result.output is not None:
            state.classify = result.output
        return self._halt_on(result, state)

    async def _symbolindex(
        self, ctx: WorkerContext, payload: ScanInput, state: _ScanState
    ) -> WorkerResult[ScanOutput] | None:
        """§3.1 step 4, batch by batch, until the tree is indexed.

        The batch loop is HERE rather than left to the phase ladder because a batch boundary is
        not an attempt: `symbol_batch_rows` exists to bound resident memory (§11.3), and letting
        each batch consume a rung would exhaust `max_attempts` on a large repo that is doing
        exactly what it was told to do.
        """
        worker = cast(
            "BaseWorker[SymbolIndexInput, SymbolIndexOutput]", self._workers["symbolindex"]
        )
        remaining: tuple[str, ...] | None = None
        completed: tuple[str, ...] = ()
        indexed = 0
        while True:
            result = await worker.run(
                ctx,
                SymbolIndexInput(
                    repo_id=payload.repo_id,
                    ignore_globs=payload.ignore_globs,
                    max_file_bytes=payload.max_file_bytes,
                    symbol_batch_rows=payload.symbol_batch_rows,
                    max_symbols_per_repo=payload.max_symbols_per_repo,
                    symbols_already_indexed=indexed,
                    resource_patterns=dict(payload.resource_patterns),
                    dynamic_patterns=dict(payload.dynamic_patterns),
                    remaining_units=remaining,
                    completed_units=completed,
                ),
            )
            if result.output is not None:
                state.symbol_batches.append(result.output)
                state.findings.extend(result.output.findings)
                indexed += len(result.output.symbols)
            halt = self._halt_on(result, state)
            if halt is not None:
                return halt
            if result.status != "partial":
                return None
            owed = tuple(result.remaining_units)
            if remaining is not None and len(owed) >= len(remaining):
                # A batch that owes no less than it did last time will owe the same forever.
                raise ScanStepUnavailableError(
                    f"symbolindex made no progress on {payload.repo_id}: {len(owed)} file(s) "
                    "still owed after a batch that landed none"
                )
            remaining = owed
            completed = tuple(dict.fromkeys([*completed, *result.completed_units]))

    def _halt_on(
        self, result: WorkerResult[Any], state: _ScanState
    ) -> WorkerResult[ScanOutput] | None:
        """A step's failure is the dispatch's failure, carrying whatever already landed.

        The output travels WITH the failure on purpose: the manifests a repo parsed before its
        symbol index blew up are real evidence, and discarding them would make the next rung
        re-derive facts that were never in doubt.
        """
        if result.status in ("ok", "partial"):
            return None
        return WorkerResult[ScanOutput](
            status=result.status,
            output=state.output(),
            error=result.error,
        )

    def _interrupted(
        self, landed: Sequence[str], owed: Sequence[str], state: _ScanState
    ) -> WorkerResult[ScanOutput]:
        """Out of deadline or cancelled between steps (§11.5). Nothing landed is `cancelled`,
        because a `partial` with an empty `completed_units` is a `failed` with better manners."""
        if not landed:
            return WorkerResult[ScanOutput](
                status="cancelled",
                error=WorkerError(
                    failure_class=FailureClass.TIMEOUT,
                    retryable=True,
                    stderr_tail="scan cancelled before any step landed",
                ),
            )
        return WorkerResult[ScanOutput](
            status="partial",
            output=state.output(),
            completed_units=list(landed),
            remaining_units=[unit for unit in owed if unit not in set(landed)],
        )


@dataclass(slots=True)
class _ScanState:
    """One repo's steps as they land. A mutable local of `run()`, never worker state (§7.2)."""

    repo_id: str
    clone: CloneOutput | None = None
    interrogate: InterrogateOutput | None = None
    classify: ClassifyOutput | None = None
    symbol_batches: list[SymbolIndexOutput] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)
    gated: bool = False

    def output(self) -> ScanOutput:
        return ScanOutput(
            repo_id=self.repo_id,
            clone=self.clone,
            interrogate=self.interrogate,
            classify=self.classify,
            symbol_batches=tuple(self.symbol_batches),
            findings=tuple(dict.fromkeys(self.findings)),
        )


# --------------------------------------------------------------------------------------
# the scan wave, the evidence buffer and the fenced sink
# --------------------------------------------------------------------------------------


class _ScanWaveStore:
    """`SchedulerStore` for the ONE synthetic scan wave.

    Everything durable is delegated to the SQLite store — `blast_radii` reads `repos`, and
    `append_blocked_by` is a real §3.5 write the runner makes when a repo is abandoned. Only the
    wave membership is local, because `waves`/`wave_members` describe the *migration* plan and
    scan is what computes it: writing a scan pseudo-wave there would leave a stale
    `wave_members` row for every repo `fleet sequence` later excludes.
    """

    def __init__(self, inner: SqliteSchedulerStore, members: Sequence[str]) -> None:
        self._inner = inner
        self._members = tuple(members)
        self._started: datetime | None = None

    async def record_plan(
        self, run_id: str, plan: WavePlan, *, now: datetime, max_usd_per_repo: float
    ) -> None:
        await self._inner.record_plan(
            run_id, plan, now=now, max_usd_per_repo=max_usd_per_repo
        )

    async def wave_indices(self, run_id: str) -> tuple[int, ...]:
        return (SCAN_WAVE_INDEX,)

    async def wave_members(self, run_id: str, wave_index: int) -> tuple[str, ...]:
        return self._members if wave_index == SCAN_WAVE_INDEX else ()

    async def wave_started_at(self, run_id: str, wave_index: int) -> datetime | None:
        return self._started

    async def begin_wave(self, run_id: str, wave_index: int, *, now: datetime) -> datetime:
        if self._started is None:
            self._started = now
        return self._started

    async def blast_radii(self, run_id: str, repo_ids: Sequence[str]) -> Mapping[str, int]:
        return await self._inner.blast_radii(run_id, repo_ids)

    async def append_blocked_by(
        self, run_id: str, repo_id: str, blocker: str, *, now: datetime
    ) -> int:
        return await self._inner.append_blocked_by(run_id, repo_id, blocker, now=now)

    async def append_unblocked_wave(
        self,
        run_id: str,
        repo_ids: Sequence[str],
        *,
        now: datetime,
        max_usd_per_repo: float,
    ) -> int | None:
        return await self._inner.append_unblocked_wave(
            run_id, repo_ids, now=now, max_usd_per_repo=max_usd_per_repo
        )


@dataclass(slots=True)
class _ScanEvidence:
    """What step 5 needs and no table holds.

    `manifests` records a dependency COUNT, not the dependencies: §6 has no per-dependency table,
    so a declared edge can only be inferred in the same invocation that parsed the manifest. A
    repo whose scan already SUCCEEDED contributes its published `coordinates` row (durable) but
    not its dependencies (not durable) — its edges survive in `edges`, which is why re-inference
    is an idempotent upsert rather than a rebuild.
    """

    dependencies: list[InferredDependency] = field(default_factory=list)
    symbols: list[SymbolRef] = field(default_factory=list)
    truncated: set[str] = field(default_factory=set)
    gated: dict[str, tuple[str, ...]] = field(default_factory=dict)
    """repo_id → finding kinds, for every repo §3.1 step 1 gated out of the fleet."""

    def record(self, output: ScanOutput) -> None:
        if output.clone is not None and not output.clone.preflight_ok:
            self.gated[output.repo_id] = output.findings or ("EmptyRepo",)
        if output.interrogate is not None:
            by_path = {ref.path: ref for ref in output.interrogate.manifests}
            for dep in output.interrogate.dependencies:
                manifest = by_path.get(dep.manifest_path)
                if manifest is None:  # pragma: no cover - a dep names its own manifest
                    continue
                self.dependencies.append(
                    InferredDependency(
                        manifest=manifest,
                        # §3.1 step 5 wants the RAW spec beside the coordinate; the interrogate
                        # worker's `ManifestDependency` keeps scope and optionality but not the
                        # adapter's `raw_id`, so the coordinate key stands in for it. The only
                        # thing that reads it is the sort key, which stays total either way.
                        raw=RawDependency(
                            raw_id=dep.coordinate.key,
                            version_spec=dep.coordinate.version_spec,
                            scope=dep.scope,
                            optional=dep.optional,
                        ),
                        coordinate=dep.coordinate,
                    )
                )
        for batch in output.symbol_batches:
            self.symbols.extend(batch.symbols)
            if batch.truncated:
                self.truncated.add(output.repo_id)


class _ScanSink:
    """Persists one repo's scan evidence under the fence that produced it (§11.5).

    Every statement goes through the run's single `StateWriter`. The rows are written BEFORE the
    phase is marked `SUCCEEDED`, which is the ordering that matters: a `SUCCEEDED` phase is never
    re-admitted, so evidence written after the status would be lost forever by any crash in
    between.
    """

    def __init__(
        self,
        *,
        writer: StateWriter,
        repository: SqliteStateRepository,
        run_id: str,
        evidence: _ScanEvidence,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self._writer = writer
        self._repository = repository
        self._run_id = run_id
        self._evidence = evidence
        self._clock = clock

    async def __call__(
        self, *, repo_id: str, phase: Phase, fence: int, result: WorkerResult[ScanOutput]
    ) -> None:
        output = result.output
        if output is None:  # pragma: no cover - the runner only calls a sink with an output
            return
        _ = (phase, fence)
        self._evidence.record(output)
        stamp = _iso(self._clock())
        await self._writer.submit(_scan_rows(self._run_id, output, stamp))
        rows = [
            SymbolRow(
                run_id=self._run_id,
                repo_id=symbol.repo_id,
                fqn=symbol.fqn,
                kind=str(symbol.kind),
                path=symbol.path,
                line=symbol.line,
                language=symbol.language,
                is_definition=symbol.is_definition,
                exported=symbol.exported,
            )
            for batch in output.symbol_batches
            for symbol in batch.symbols
        ]
        if rows:
            await self._repository.insert_symbols(rows)


def _scan_rows(
    run_id: str, output: ScanOutput, stamp: str
) -> Callable[[aiosqlite.Connection], Coroutine[Any, Any, None]]:
    """One transaction for one repo's `repos` / `manifests` / `coordinates` / `findings` rows.

    Every statement is an idempotent upsert keyed the way §6 keys it, so a re-scan of the same
    tree writes the same rows rather than a second copy of them (§11.7).
    """
    manifests = () if output.interrogate is None else output.interrogate.manifests
    ecosystems = () if output.interrogate is None else output.interrogate.ecosystems
    published = [ref.publishes for ref in manifests if ref.publishes is not None]
    dependencies = () if output.interrogate is None else output.interrogate.dependencies

    async def unit(conn: aiosqlite.Connection) -> None:
        if output.clone is not None:
            await conn.execute(
                "UPDATE repos SET url = ?, default_branch = ?, default_branch_source = ?, "
                "    head_sha = ?, size_bytes = ?, commit_count = ?, cloned_at = ?, "
                "    is_shallow = ?, submodule_count = ?, has_lfs = ?, "
                "    largest_blob_bytes = ?, preflight_ok = ?, updated_at = ? "
                " WHERE repo_id = ?",
                (
                    output.clone.url,
                    output.clone.default_branch,
                    output.clone.default_branch_source,
                    output.clone.head_sha,
                    output.clone.size_bytes,
                    output.clone.commit_count,
                    stamp,
                    int(output.clone.is_shallow),
                    output.clone.submodule_count,
                    int(output.clone.has_lfs),
                    output.clone.largest_blob_bytes,
                    int(output.clone.preflight_ok),
                    stamp,
                    output.repo_id,
                ),
            )
        if output.interrogate is not None or output.classify is not None:
            await conn.execute(
                "UPDATE repos SET ecosystems = ?, primary_coord_key = ?, kind = ?, "
                "    updated_at = ? WHERE repo_id = ?",
                (
                    json.dumps([eco.value for eco in ecosystems]),
                    min((coord.key for coord in published), default=None),
                    "unknown" if output.classify is None else output.classify.kind,
                    stamp,
                    output.repo_id,
                ),
            )
        if manifests:
            await conn.executemany(
                "INSERT INTO manifests (repo_id, path, ecosystem, adapter, adapter_version, "
                "    sha256, publishes_key, dependency_count, low_confidence, parse_error, "
                "    parsed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (repo_id, path) DO UPDATE SET ecosystem = excluded.ecosystem, "
                "    adapter = excluded.adapter, adapter_version = excluded.adapter_version, "
                "    sha256 = excluded.sha256, publishes_key = excluded.publishes_key, "
                "    dependency_count = excluded.dependency_count, "
                "    low_confidence = excluded.low_confidence, "
                "    parse_error = excluded.parse_error, parsed_at = excluded.parsed_at",
                [_manifest_row(ref) for ref in manifests],
            )
        coordinates = [
            (coord, output.repo_id if owned else None)
            for coord, owned in [
                *((coord, True) for coord in published),
                *((dep.coordinate, False) for dep in dependencies),
            ]
        ]
        if coordinates:
            await conn.executemany(
                "INSERT INTO coordinates (coord_key, ecosystem, grp, name, owner_repo_id, "
                "    first_seen_at) VALUES (?, ?, ?, ?, ?, ?) "
                # COALESCE, never assignment: a dependency row is written owner-less because the
                # repo that publishes it may not be scanned yet, and it must never DEMOTE an
                # internal coordinate to external — that deletes an ordering edge (§3.1 step 3).
                "ON CONFLICT (coord_key) DO UPDATE SET owner_repo_id = "
                "    COALESCE(excluded.owner_repo_id, coordinates.owner_repo_id)",
                [
                    (
                        coord.key,
                        coord.ecosystem.value,
                        coord.group,
                        coord.name,
                        owner,
                        stamp,
                    )
                    for coord, owner in coordinates
                ],
            )
        if output.findings:
            await conn.executemany(
                "INSERT INTO findings (run_id, repo_id, kind, severity, fingerprint, payload, "
                "                      created_at) VALUES (?, ?, ?, 'warn', ?, ?, ?) "
                "ON CONFLICT (run_id, IFNULL(repo_id, ''), kind, fingerprint) "
                "DO UPDATE SET payload = excluded.payload, created_at = excluded.created_at",
                [
                    (
                        run_id,
                        output.repo_id,
                        kind,
                        _fingerprint(run_id, output.repo_id, kind),
                        redact_text(json.dumps({"repo_id": output.repo_id, "kind": kind})),
                        stamp,
                    )
                    for kind in output.findings
                ],
            )

    return unit


def _manifest_row(ref: ManifestRef) -> tuple[object, ...]:
    return (
        ref.repo_id,
        ref.path,
        ref.ecosystem.value,
        ref.adapter,
        ref.adapter_version,
        ref.sha256,
        None if ref.publishes is None else ref.publishes.key,
        ref.dependency_count,
        int(ref.low_confidence),
        ref.parse_error,
        _iso(ref.parsed_at),
    )


# --------------------------------------------------------------------------------------
# the scan driver
# --------------------------------------------------------------------------------------


def _validate_scan_flags(
    opts: GlobalOptions,
    *,
    repos: Path,
    refresh: bool,
    preflight_only: bool,
    concurrency: int | None,
) -> None:
    """Every scan flag either does what it says or is refused here (§10).

    A flag that parses and then does nothing is worse than an absent flag: the operator reads the
    run as having honoured it. `--refresh` and a foreign `--repos` are refused for reasons that
    are properties of the design, not of the calendar — see each message.
    """
    manifest = (opts.config_dir / "repos.yaml").resolve()
    if repos.resolve() != manifest:
        raise UsageError(
            f"--repos {repos} is not {manifest}. §9 loads the fleet manifest as part of the "
            "config bundle and hashes it into `runs.config_digests`, so a manifest read from "
            "somewhere else would make every later drift check compare against a file this run "
            "never used. Point --config at the directory that holds the manifest you mean."
        )
    if refresh:
        raise UsageError(
            "--refresh is not implemented: re-scanning a repo means moving its SCAN phase from "
            "SUCCEEDED back to PENDING, and `ALLOWED_TRANSITIONS[SUCCEEDED]` is empty by "
            "design (§6) — a terminal phase is terminal. Start a new run (`--run <new-id>`) to "
            "re-scan the fleet against fresh mirrors."
        )
    if concurrency is not None and concurrency < 1:
        raise UsageError(f"--concurrency {concurrency} must be at least 1")
    if preflight_only:
        raise UsageError(
            "--preflight-only is not implemented, and the shape of the phase model is why: "
            "`phases` holds ONE row per (run, repo, SCAN) and `PhaseRunner` drives that row to a "
            "terminal status before the wave closes, re-dispatching its own `partial` hand-backs "
            "until the ladder is spent. A run that stopped after step 1 would therefore either "
            "leave the phase SUCCEEDED with no manifests and no symbols — and a SUCCEEDED phase "
            "is never re-admitted, so the fleet could never be asked for them again — or burn "
            "the repo's whole attempt ladder handing itself back. Scan the fleet, or scan a "
            "subset with --only."
        )


async def _scan_run_id(conn: aiosqlite.Connection, opts: GlobalOptions) -> str:
    """`--run ID`, else the latest run, else a fresh one — `fleet scan` is what CREATES a run."""
    if opts.run is not None:
        return opts.run
    async with conn.execute(
        "SELECT run_id FROM runs ORDER BY started_at DESC, run_id DESC LIMIT 1"
    ) as cursor:
        row = await cursor.fetchone()
    return str(uuid4()) if row is None else str(row[0])


def _fleet_entries(settings: FleetSettings, only: str | None) -> tuple[RepoEntry, ...]:
    """The manifest entries this invocation covers, in manifest order."""
    chosen = tuple(
        entry
        for entry in settings.repos.repos
        if only is None or fnmatch(entry.name, only)
    )
    manifest = settings.config_dir / "repos.yaml"
    if not settings.repos.repos:
        raise UsageError(f"{manifest} declares no repos: there is no fleet to scan (§9)")
    if not chosen:
        raise UsageError(
            f"--only {only!r} matched none of the {len(settings.repos.repos)} repo(s) in "
            f"{manifest}"
        )
    return chosen


async def _scan_impl(
    opts: GlobalOptions,
    settings: FleetSettings,
    path: Path,
    *,
    run_id: str,
    only: str | None,
    concurrency: int | None,
    skip_symbols: bool,
    skip_classify: bool,
    skip_contracts: bool,
    symbol_batch_rows: int | None,
) -> dict[str, object]:
    """Phase 1 steps 1–5b over one fleet, through the real composition.

    `RunContext` assembles the client, the ledger and the semaphores exactly once; `PhaseRunner`
    drives; the workers come from the registry. Nothing here re-implements a worker, and nothing
    here writes a status the runner is responsible for.
    """
    # §14: install the log pipeline for THIS run before anything can log. `--log-level` had no
    # effect at all until here — it was parsed into `GlobalOptions` and read by nothing — and the
    # lazy `get_logger()` fallback binds whatever `sys.stderr` was on first use, which in a
    # long-lived process is a handle that may since have been closed. A worker whose `log.info`
    # raises is a worker that FAILS: the first symptom of this was five healthy repos reaching
    # REQUIRES_HUMAN_INTERVENTION with `I/O operation on closed file`.
    log_configure(level=opts.log_level)
    fleet = _fleet_entries(settings, only)
    steps = tuple(
        unit
        for unit in SCAN_UNITS
        if not (unit == "classify" and skip_classify)
        and not (unit == "symbolindex" and skip_symbols)
    )
    lanes = max(1, concurrency or settings.config.concurrency.cpu_pool_workers)
    pool = new_cpu_pool(lanes)
    now = _now()
    try:
        async with StateWriter(path, owner="fleet-scan") as writer:
            read_conn = await connect_ro(path)
            try:
                repository = SqliteStateRepository(writer=writer, read_conn=read_conn)
                await _open_run(repository, writer, run_id=run_id, settings=settings, now=now)
                members = await _seed_fleet(repository, writer, run_id, fleet, now=now)
                # §6's reaper, before anything is admitted: a phase left RUNNING by a killed
                # process holds a lease nobody renews, and until it is reclaimed the repo is
                # invisible to admission — a resume that quietly scans 249 of 250 repos.
                await repository.reap_expired_phase_leases(run_id, now=_now())
                await repository.open_budget_ledger(
                    run_id, max_usd=settings.config.budgets.run_max_cost_usd, now=now
                )
                evidence = _ScanEvidence()
                report, run_ctx = await _run_scan_wave(
                    settings,
                    db_path=path,
                    repository=repository,
                    writer=writer,
                    read_conn=read_conn,
                    run_id=run_id,
                    fleet=fleet,
                    members=members,
                    steps=steps,
                    pool=pool,
                    lanes=lanes,
                    symbol_batch_rows=symbol_batch_rows,
                    evidence=evidence,
                )
                gated = await _gate_empty_repos(writer, run_id, evidence, now=_now())
                edges = await _persist_scan_edges(
                    settings,
                    repository=repository,
                    writer=writer,
                    read_conn=read_conn,
                    run_id=run_id,
                    evidence=evidence,
                    now=_now(),
                )
                contracts = await _extract_contracts(
                    run_ctx,
                    settings,
                    writer=writer,
                    read_conn=read_conn,
                    run_id=run_id,
                    fleet=fleet,
                    skip=skip_contracts,
                    now=_now(),
                )
                statuses = await _scan_statuses(read_conn, run_id)
            finally:
                await read_conn.close()
    finally:
        pool.shutdown(wait=True, cancel_futures=True)

    with suppress(Exception):  # a projection is an OUTPUT (§11.5); it never fails a scan
        await project_once(path, run_id=UUID(run_id), path=DEFAULT_PROJECTION_PATH)

    attention = sorted(
        repo
        for repo, status in statuses.items()
        if status is RepoStatus.REQUIRES_HUMAN_INTERVENTION
    )
    halt = report.exit_code
    exit_code = (
        halt
        if halt is not None
        else (ExitCode.REQUIRES_HUMAN_INTERVENTION if attention else ExitCode.SUCCESS)
    )
    return {
        "run_id": run_id,
        "repos": len(fleet),
        "succeeded": sum(1 for s in statuses.values() if s is RepoStatus.SUCCEEDED),
        "skipped": sorted(gated),
        "failed": len(attention),
        "attention": attention,
        "edges": edges,
        "contracts": contracts,
        "steps": list(steps),
        "halt": None if report.halt is None else str(report.halt),
        "exit_code": int(exit_code),
    }


async def _close_wave_projector(projector: Projector, ctx: RunContext) -> None:
    """Stop a wave's live projector without letting a failed rebuild fail the wave.

    `Projector.aclose()` re-raises whatever the last rebuild raised, deliberately, so that a
    projection which stopped updating is not silent (Rule 11). But the projection is an OUTPUT
    (§11.5) and `RunContext.project()` is documented as never raising, so that same exception
    must not stall the wave that feeds it. Logged rather than suppressed — `suppress(Exception)`
    would satisfy §11.5 by hiding an error, and a bare `async with Projector(...)` would satisfy
    Rule 11 by failing the command; this is the shape `_drain_llm_findings` already uses for a
    diagnostics write that must not rewrite a run's outcome. The command's own trailing
    `project_once` still writes the durable file, so nothing an operator reads is lost.
    """
    try:
        await projector.aclose()
    except Exception as exc:
        ctx.log.error(  # noqa: TRY400 - §11.4: no formatted traceback in a durable record
            "wave_projection_failed",
            run_id=str(ctx.run_id),
            exception_type=f"{type(exc).__module__}.{type(exc).__qualname__}",
            error=str(exc),
        )


async def _run_scan_wave(
    settings: FleetSettings,
    *,
    repository: SqliteStateRepository,
    writer: StateWriter,
    read_conn: aiosqlite.Connection,
    db_path: Path,
    run_id: str,
    fleet: Sequence[RepoEntry],
    members: Sequence[str],
    steps: Sequence[str],
    pool: ProcessPoolExecutor,
    lanes: int,
    symbol_batch_rows: int | None,
    evidence: _ScanEvidence,
) -> tuple[WaveReport, RunContext]:
    """Compose the run and drive the one scan wave. The composition root, and nothing else.

    The `RunContext` comes back out because §3.1 step 5b runs AFTER this wave and after step 5's
    edges have landed, and it is a worker like any other: it must be handed the run's own client,
    ledger and semaphores rather than a second set assembled beside them (Guardrail 3).
    """
    ledger = CostLedger(
        repository,
        run_id=run_id,
        ceilings=Ceilings.from_settings(settings.config.budgets, settings.config.stubs),
        clock=_now,
    )
    concurrency = settings.config.concurrency.model_copy(
        update={
            "git_net": min(settings.config.concurrency.git_net, lanes),
            "subprocess": min(settings.config.concurrency.subprocess, lanes),
        }
    )
    projector = Projector(db_path, run_id=UUID(run_id), path=DEFAULT_PROJECTION_PATH)
    ctx = RunContext(
        run_id=UUID(run_id),
        config=settings.config,
        writer=writer,
        repository=repository,
        read_conn=read_conn,
        ledger=ledger,
        limits=Limits.create(concurrency, ledger=ledger, cpu_pool=pool),
        llm=llm_router(settings),
        log=default_logger("fleet.scan"),
        work_dir=(settings.root / settings.config.run.work_dir).resolve(),
        projector=projector,
        clock=_now,
        harness_version=HARNESS_VERSION,
    )
    scheduler = WaveScheduler(
        run_id=run_id,
        phase=Phase.SCAN,
        store=_ScanWaveStore(
            SqliteSchedulerStore(writer=writer, read_conn=read_conn), members
        ),
        db=repository,
        budgets=settings.config.budgets,
        clock=_now,
    )
    runner = PhaseRunner(
        ctx,
        ScanPipelineWorker(),
        scheduler,
        payloads=_scan_payloads(settings, fleet, steps=steps, batch_rows=symbol_batch_rows),
        sink=_ScanSink(
            writer=writer, repository=repository, run_id=run_id, evidence=evidence
        ),
    )
    try:
        await projector.start()
        return await runner.run_wave(SCAN_WAVE_INDEX), ctx
    finally:
        await _close_wave_projector(projector, ctx)


def _scan_payloads(
    settings: FleetSettings,
    fleet: Sequence[RepoEntry],
    *,
    steps: Sequence[str],
    batch_rows: int | None,
) -> PayloadFactory[ScanInput]:
    """The §9 config, resolved into one repo's payload. Injected, because what a payload holds is
    the phase's business and not the driver's (Guardrail 3)."""
    scan = settings.config.scan
    preflight = settings.config.preflight
    by_id = {entry.name: entry for entry in fleet}
    cache_dir = str((settings.root / settings.config.run.cache_dir / "git").resolve())

    async def build(
        *, repo_id: str, phase: Phase, attempt: int, remaining_units: Sequence[str] | None
    ) -> ScanInput:
        _ = (phase, attempt)
        entry = by_id[repo_id]
        return ScanInput(
            repo_id=repo_id,
            url=entry.url,
            cache_dir=cache_dir,
            steps=tuple(steps),
            ignore_globs=tuple(scan.ignore_globs),
            branch_fallbacks=tuple(preflight.branch_fallbacks),
            max_repo_bytes=preflight.max_repo_bytes,
            max_blob_bytes=preflight.max_blob_bytes,
            min_free_bytes=preflight.min_free_bytes,
            unshallow=preflight.unshallow,
            require_lfs_binary=preflight.require_lfs_binary,
            max_file_bytes=scan.max_file_bytes,
            symbol_batch_rows=batch_rows or scan.symbol_batch_rows,
            max_symbols_per_repo=scan.max_symbols_per_repo,
            resource_patterns=dict(scan.resource_patterns),
            dynamic_patterns=dict(scan.dynamic_patterns),
            remaining_units=None if remaining_units is None else tuple(remaining_units),
        )

    return build


async def _open_run(
    repository: SqliteStateRepository,
    writer: StateWriter,
    *,
    run_id: str,
    settings: FleetSettings,
    now: datetime,
) -> None:
    """Create the `runs` row, digests and all. Idempotent: a re-scan reuses the run it resumes.

    `config_digests` is written here rather than left to default `'{}'` because §10's drift check
    is per section: an empty map makes every section look changed, and the only escape from that
    is `--force-config-drift`, which accepts every co-edited change too.
    """
    await repository.upsert_run(
        run_id,
        started_at=now,
        config_sha256=settings.config_sha256(),
        harness_version=HARNESS_VERSION,
    )
    digests = json.dumps(dict(settings.section_digests), sort_keys=True)

    async def unit(conn: aiosqlite.Connection) -> None:
        await conn.execute(
            "UPDATE runs SET config_digests = ?, monorepo_branch = ? WHERE run_id = ?",
            (digests, settings.config.run.monorepo_branch, run_id),
        )

    await writer.submit(unit)


async def _seed_fleet(
    repository: SqliteStateRepository,
    writer: StateWriter,
    run_id: str,
    fleet: Sequence[RepoEntry],
    *,
    now: datetime,
) -> tuple[str, ...]:
    """Every manifest entry becomes a `repos` row and a PENDING `phases` row before anything runs.

    A repo the manifest marks `skip: true` is written `SKIPPED` here, while its phase is still
    `PENDING` — the one transition into `SKIPPED` the §6 state machine allows. Nothing downstream
    has to special-case it: a `SKIPPED` phase is settled, so admission passes over it.
    """
    members: list[str] = []
    skipped: list[str] = []
    for entry in fleet:
        await repository.upsert_repo(
            entry.name,
            name=entry.name,
            url=credential_free(entry.url),
            now=now,
            dest_path=entry.dest,
        )
        await repository.upsert_phase(run_id, entry.name, Phase.SCAN, now=now)
        if entry.skip:
            skipped.append(entry.name)
        else:
            members.append(entry.name)

    if skipped:
        stamp = _iso(now)

        async def unit(conn: aiosqlite.Connection) -> None:
            await conn.executemany(
                "UPDATE phases SET status = 'SKIPPED', updated_at = ? "
                " WHERE run_id = ? AND repo_id = ? AND phase = 1 AND status = 'PENDING'",
                [(stamp, run_id, name) for name in skipped],
            )

        await writer.submit(unit)
    return tuple(members)


async def _gate_empty_repos(
    writer: StateWriter, run_id: str, evidence: _ScanEvidence, *, now: datetime
) -> tuple[str, ...]:
    """§3.1 step 1's gate: a repo with no commit is `SKIPPED`, with its finding, not an error.

    **This is a driver write, and it is the one place the driver writes a status.** The §6 state
    machine has no edge out of `SUCCEEDED`, and the clone worker's contract (correctly) reports
    an empty repo as `ok` rather than as a failure — so between the two there is no legal route
    into `SKIPPED` for a phase that has already completed. The gate is therefore applied here,
    audited by the `EmptyRepo` finding written beside it, exactly as `fleet quarantine` applies
    an operator's removal. The alternative — reporting an empty repo as a failure so the ladder
    can terminate it — would exit 7 on a repo that did nothing wrong.
    """
    if not evidence.gated:
        return ()
    stamp = _iso(now)
    rows = sorted(evidence.gated.items())

    async def unit(conn: aiosqlite.Connection) -> None:
        await conn.executemany(
            "UPDATE phases SET status = 'SKIPPED', updated_at = ? "
            " WHERE run_id = ? AND repo_id = ? AND phase = 1 AND status <> 'SKIPPED'",
            [(stamp, run_id, repo_id) for repo_id, _ in rows],
        )
        await conn.executemany(
            "INSERT INTO findings (run_id, repo_id, kind, severity, fingerprint, payload, "
            "                      created_at) VALUES (?, ?, ?, 'warn', ?, ?, ?) "
            "ON CONFLICT (run_id, IFNULL(repo_id, ''), kind, fingerprint) "
            "DO UPDATE SET payload = excluded.payload, created_at = excluded.created_at",
            [
                (
                    run_id,
                    repo_id,
                    kind,
                    _fingerprint(run_id, repo_id, kind),
                    redact_text(
                        json.dumps(
                            {"repo_id": repo_id, "kind": kind, "gated": True}, sort_keys=True
                        )
                    ),
                    stamp,
                )
                for repo_id, kinds in rows
                for kind in kinds
            ],
        )

    await writer.submit(unit)
    return tuple(repo_id for repo_id, _ in rows)


async def _persist_scan_edges(
    settings: FleetSettings,
    *,
    repository: SqliteStateRepository,
    writer: StateWriter,
    read_conn: aiosqlite.Connection,
    run_id: str,
    evidence: _ScanEvidence,
    now: datetime,
) -> int:
    """§3.1 step 5: infer the edges this scan's evidence supports, then persist blast radius.

    The owner index is read back out of `coordinates` rather than taken from the evidence buffer:
    a repo scanned by an earlier invocation still owns its coordinate, and an ownership oracle
    that only knew about this invocation would call it external and silently drop the ordering
    constraint that depends on it.
    """
    owners = await _owner_index(read_conn)
    edges = infer_edges(
        InferenceInput(
            owners=owners,
            dependencies=tuple(evidence.dependencies),
            symbols=tuple(evidence.symbols),
            truncated_repo_ids=frozenset(evidence.truncated),
            vendor_globs=tuple(settings.config.scan.vendor_globs),
            generated_globs=tuple(settings.config.scan.generated_globs),
        )
    )
    if edges:
        await repository.insert_edges(
            [
                EdgeRow(
                    edge_key=edge.edge_key,
                    run_id=run_id,
                    src_kind=edge.src_kind.value,
                    src_id=edge.src_id,
                    dst_kind=edge.dst_kind.value,
                    dst_id=edge.dst_id,
                    dst_coord_key=(
                        # §6: a `Coordinate.key` when the dst is a repo, the `contract_id` when
                        # it is a contract. The column carries both and only `dst_kind` says
                        # which — see the `edges` DDL.
                        str(edge.dst_id)
                        if edge.dst_coordinate is None
                        else edge.dst_coordinate.key
                    ),
                    kind=edge.kind.value,
                    base_confidence=edge.base_confidence,
                    confidence=edge.confidence,
                    evidence_path=edge.evidence_path,
                    evidence_line=-1 if edge.evidence_line is None else edge.evidence_line,
                    detected_at=_iso(edge.detected_at),
                    ambiguous=edge.ambiguous,
                    ordering_suppressed=edge.ordering_suppressed,
                )
                for edge in edges
            ]
        )
    await _persist_blast_radii(
        settings, writer=writer, read_conn=read_conn, run_id=run_id, now=now
    )
    return len(edges)


async def _extract_contracts(
    ctx: RunContext,
    settings: FleetSettings,
    *,
    writer: StateWriter,
    read_conn: aiosqlite.Connection,
    run_id: str,
    fleet: Sequence[RepoEntry],
    skip: bool,
    now: datetime,
) -> int:
    """§3.1 step 5b: dispatch the `contracts` worker over the WHOLE run and persist its rows.

    Fleet-wide rather than per-repo, because a contract spans repos: §3.1 keys `contracts` on
    `(run_id, contract_id)` and re-derives the table "for the whole run in one transaction", so
    nine vendored copies of one proto can never become nine rows. The three flags below each
    genuinely skip the step rather than degrade it — a contract-free graph is a legitimate
    configuration (§3.1 5b), an invented one is not.
    """
    if skip or not settings.config.scan.contracts.enabled:
        return 0
    if not settings.config.graph.hoist_contracts:
        return 0

    worker = get_worker("contracts")()
    payload = ContractsInput(
        repos=await _contract_repo_facts(ctx, read_conn, fleet),
        symbols=await _contract_symbols(read_conn, run_id),
        committed=await _committed_contracts(read_conn, run_id),
        config=settings.config.scan.contracts,
        ignore_globs=tuple(settings.config.scan.ignore_globs),
        vendor_globs=tuple(settings.config.scan.vendor_globs),
        generated_globs=tuple(settings.config.scan.generated_globs),
        min_extraction_confidence=settings.config.graph.min_extraction_confidence,
    )
    result = await worker.run(
        ctx.worker_context(
            repo_id=CONTRACTS_SCOPE,
            phase=Phase.SCAN,
            attempt=1,
            lease_fence=0,
            cancel=asyncio.Event(),
            # 5b invokes no model (§3.1: extraction and ownership are FORBIDDEN to the model), so
            # a zero token/usd budget is the honest ceiling rather than an arbitrary one.
            budget=CallBudget(remaining_tokens=0, remaining_usd=0.0, deadline=loop_now() + 600.0),
        ),
        payload,
    )
    output = result.output
    if output is None or not result.ok:  # pragma: no cover - 5b is total over its input
        raise ScanStepUnavailableError(
            f"§3.1 step 5b returned {result.status!r} with no contract rows; refusing to leave a "
            "run whose graph claims the fleet shares no contracts (Rule 11)"
        )
    if output.unidentifiable_idl_paths:
        ctx.log.warning(
            "contracts.unidentifiable_idl",
            count=len(output.unidentifiable_idl_paths),
            sample=list(output.unidentifiable_idl_paths[:5]),
        )
    await writer.submit(_contract_rows(run_id, output, _iso(now)))
    return len(output.contracts)


async def _contract_repo_facts(
    ctx: RunContext, conn: aiosqlite.Connection, fleet: Sequence[RepoEntry]
) -> tuple[RepoFacts, ...]:
    """§3.1 5b (iv)'s ladder inputs: the `owns:` hint, `commit_count`, and whether the repo
    publishes a coordinate at all. `worktree_path` is the run's own, so the path universe is the
    tree this scan actually cloned."""
    counts = {
        str(row[0]): int(row[1] or 0)
        for row in await _rows(conn, "SELECT repo_id, commit_count FROM repos")
    }
    publishers = {
        str(row[0])
        for row in await _rows(
            conn, "SELECT DISTINCT owner_repo_id FROM coordinates WHERE owner_repo_id IS NOT NULL"
        )
    }
    return tuple(
        RepoFacts(
            repo_id=entry.name,
            worktree_path=str(ctx.worktree(entry.name)),
            commit_count=counts.get(entry.name, 0),
            owns=tuple(entry.owns),
            publishes_coordinate=entry.name in publishers,
        )
        for entry in sorted(fleet, key=lambda e: e.name)
    )


async def _contract_symbols(
    conn: aiosqlite.Connection, run_id: str
) -> tuple[SymbolRef, ...]:
    """The `symbols` rows 5b joins on, and only those — the kind list is the worker's, not this
    query's, so there is one definition of which rows the step is made of."""
    kinds = ",".join("?" for _ in CONTRACT_SYMBOL_KINDS)
    rows = await _rows(
        conn,
        "SELECT repo_id, fqn, kind, path, line, language, is_definition, exported "  # noqa: S608
        f"  FROM symbols WHERE run_id = ? AND kind IN ({kinds}) "
        " ORDER BY repo_id, path, line, fqn, kind",
        (run_id, *(kind.value for kind in CONTRACT_SYMBOL_KINDS)),
    )
    return tuple(
        SymbolRef(
            repo_id=str(row[0]),
            fqn=str(row[1]),
            kind=SymbolKind(str(row[2])),
            path=str(row[3]),
            line=int(row[4]),
            language=str(row[5]),
            is_definition=bool(row[6]),
            exported=bool(row[7]),
        )
        for row in rows
    )


async def _committed_contracts(
    conn: aiosqlite.Connection, run_id: str
) -> tuple[ContractNode, ...]:
    """The `HOISTED`/`MIGRATED` rows the rebuild must not lose (§3.1 "re-runnable by
    construction"). Only the three fields that survive are read back: everything else is
    re-derived, which is the point of a rebuild."""
    rows = await _rows(
        conn,
        "SELECT contract_id, kind, identifier, owning_repo_id, status, status_detail, "
        "       hoist_target_path FROM contracts "
        " WHERE run_id = ? AND status IN ('HOISTED','MIGRATED') ORDER BY contract_id",
        (run_id,),
    )
    return tuple(
        ContractNode(
            contract_id=str(row[0]),
            kind=ContractKind(str(row[1])),
            identifier=str(row[2]),
            owning_repo_id=None if row[3] is None else str(row[3]),
            status=ContractStatus(str(row[4])),
            status_detail=str(row[5] or ""),
            hoist_target_path=None if row[6] is None else str(row[6]),
            extractable=True,  # §6's CHECK: a HOISTED row already satisfied it
        )
        for row in rows
    )


def _contract_rows(
    run_id: str, output: ContractsOutput, stamp: str
) -> Callable[[aiosqlite.Connection], Coroutine[Any, Any, None]]:
    """One transaction for the whole run's `contracts` and `CONTRACT` `collisions` rows.

    `DELETE` then re-insert, exactly as §3.1 prescribes for 5b — the table is a projection of
    `symbols` plus config, and a contract that no longer exists must not survive as a node the
    sequencer would schedule. What must survive the rebuild arrived on `ContractsInput.committed`
    and is already folded into `output.contracts`.
    """

    async def unit(conn: aiosqlite.Connection) -> None:
        await conn.execute("DELETE FROM contracts WHERE run_id = ?", (run_id,))
        if output.contracts:
            await conn.executemany(
                "INSERT INTO contracts (run_id, contract_id, kind, identifier, owning_repo_id, "
                "    source_paths, generated_paths, consumer_repo_ids, extractable, "
                "    extraction_confidence, confidence_factors, content_sha256, "
                "    hoist_target_path, status, status_detail, detected_at) "
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        run_id,
                        node.contract_id,
                        node.kind.value,
                        node.identifier,
                        node.owning_repo_id,
                        json.dumps(node.source_paths, sort_keys=True),
                        json.dumps(node.generated_paths, sort_keys=True),
                        json.dumps(node.consumer_repo_ids),
                        int(node.extractable),
                        node.extraction_confidence,
                        json.dumps(node.confidence_factors, sort_keys=True),
                        node.content_sha256,
                        node.hoist_target_path,
                        node.status.value,
                        node.status_detail,
                        stamp,
                    )
                    for node in output.contracts
                ],
            )
        if output.collisions:
            await conn.executemany(
                "INSERT INTO collisions (run_id, kind, key, repo_ids, blob_shas, severity, "
                "    resolution, detected_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                # §11.7: a re-scan re-detects the same contest and must not duplicate its row.
                "ON CONFLICT (run_id, kind, key) DO UPDATE SET repo_ids = excluded.repo_ids, "
                "    blob_shas = excluded.blob_shas, severity = excluded.severity, "
                "    resolution = excluded.resolution, detected_at = excluded.detected_at",
                [
                    (
                        run_id,
                        collision.kind,
                        collision.key,
                        json.dumps(collision.repo_ids),
                        json.dumps(collision.blob_shas),
                        collision.severity,
                        collision.resolution,
                        stamp,
                    )
                    for collision in output.collisions
                ],
            )

    return unit


async def _owner_index(conn: aiosqlite.Connection) -> OwnerIndex:
    """The `coordinates` table as §3.1 step 3's internal-vs-external oracle."""
    rows = await _rows(
        conn,
        "SELECT owner_repo_id, coord_key, ecosystem, grp, name FROM coordinates "
        " WHERE owner_repo_id IS NOT NULL ORDER BY coord_key",
    )
    return OwnerIndex.from_published(
        (
            str(row[0]),
            Coordinate(ecosystem=Ecosystem(str(row[2])), group=str(row[3]), name=str(row[4])),
        )
        for row in rows
    )


async def _persist_blast_radii(
    settings: FleetSettings,
    *,
    writer: StateWriter,
    read_conn: aiosqlite.Connection,
    run_id: str,
    now: datetime,
) -> None:
    """§3.5: `repos.blast_radius = |descendants(G_order, r)|`, recomputed from the rows just
    written. It is what the scheduler admits by and what `repo_max_cost_usd` scales with, so a
    scan that leaves it at 0 hands every later wave a flat fleet."""
    nodes = await _graph_nodes(read_conn, run_id)
    edges = await _graph_edges(read_conn, run_id)
    graph = build_graph(
        nodes,
        edges,
        dag_edge_kinds=tuple(settings.config.graph.dag_edge_kinds),
        min_confidence=settings.config.graph.min_confidence,
        hoist_contracts=settings.config.graph.hoist_contracts,
    )
    radii = [
        (count, _iso(now), node_id)
        for (kind, node_id), count in graph_blast_radii(graph).items()
        if kind == NodeKind.REPO.value
    ]
    if not radii:
        return

    async def unit(conn: aiosqlite.Connection) -> None:
        await conn.executemany(
            "UPDATE repos SET blast_radius = ?, updated_at = ? WHERE repo_id = ?", radii
        )

    await writer.submit(unit)


async def _scan_statuses(
    conn: aiosqlite.Connection, run_id: str
) -> dict[str, RepoStatus]:
    rows = await _rows(
        conn,
        "SELECT repo_id, status FROM phases WHERE run_id = ? AND phase = 1 ORDER BY repo_id",
        (run_id,),
    )
    return {str(row[0]): RepoStatus(str(row[1])) for row in rows}


@app.command()
def plan(
    ctx: typer.Context,
    repo: Annotated[str | None, typer.Option("--repo")] = None,
    wave: Annotated[int | None, typer.Option("--wave")] = None,
    all_repos: Annotated[bool, typer.Option("--all")] = False,
    diff: Annotated[bool, typer.Option("--diff")] = False,
) -> None:
    """Dry-run Phase 2: compute the `RelocationPlan` and the rewrite target set. Changes nothing."""
    with _mapped_errors():
        _phase_preflight(ctx)
        _ = (repo, wave, all_repos, diff)
        _unavailable("plan", "src/fleet/workers/relocate.py")


@app.command()
def build(
    ctx: typer.Context,
    wave: Annotated[int | None, typer.Option("--wave")] = None,
    repo: Annotated[str | None, typer.Option("--repo")] = None,
    timeout: Annotated[int, typer.Option("--timeout")] = 1800,
    no_sandbox: Annotated[bool, typer.Option("--no-sandbox", help="CI only.")] = False,
    regen_build_files: Annotated[bool, typer.Option("--regen-build-files")] = False,
    stub_blocked: Annotated[bool, typer.Option("--stub-blocked")] = False,
) -> None:
    """Phase 3: `git-filter-repo` ingest + merge, BUILD generation, `bazel build`/`test`.

    The composition is Phase 2's: the driver does the git preparation §3.3 step 1 requires to be
    true *before* the lease (the filtered throwaway clone, the mutex-serialized merge, the
    immutable snapshot ref and the worktree cut from it), `PhaseRunner` drives one wave at a
    time, `BuildPipelineWorker` chains the two REGISTERED workers, and the evidence reaches
    SQLite through a fenced sink before the status that vouches for it (§11.5).

    `--regen-build-files` is accepted and is the default behaviour rather than a switch: §3.3 is
    explicit that "BUILD.bazel files are always generated, never hand-edited", so every
    invocation re-renders them from the plan. It is documented here rather than refused because
    passing it gets exactly what it says.
    """
    with _mapped_errors():
        opts, settings, run_id = _phase_preflight(ctx)
        # §11.3/§12.22: evict the Bazel disk cache to `budgets.max_disk_gb`, then refuse to
        # start at all if the volume is still under `preflight.min_free_bytes` (exit 9). A phase
        # that consumes gigabytes does not get to discover ENOSPC inside a write transaction.
        _require_disk_headroom(settings)
        _validate_build_flags(stub_blocked=stub_blocked)
        _ = regen_build_files
        _check_wave_budget(opts, settings, run_id, wave)
        result = _run(
            _build_impl(
                opts,
                settings,
                opts.db_path,
                run_id=run_id,
                wave=wave,
                only=repo,
                timeout_s=timeout,
                sandboxed=not no_sandbox,
            )
        )
        _emit(opts, result, _build_lines(result))
        _raise_for_phase(result)


@app.command()
def verify(
    ctx: typer.Context,
    repo: Annotated[str | None, typer.Option("--repo")] = None,
    wave: Annotated[int | None, typer.Option("--wave")] = None,
    rdeps: Annotated[bool, typer.Option("--rdeps/--no-rdeps")] = True,
    rdeps_limit: Annotated[int, typer.Option("--rdeps-limit")] = 2000,
    affected_only: Annotated[bool, typer.Option("--affected-only/--full")] = True,
    rdeps_sample_n: Annotated[int, typer.Option("--rdeps-sample-n")] = 500,
) -> None:
    """Phase 4 steps 1–3: re-test on tip, `bazel query rdeps`, test the closure, write a report.

    Step 4 (`gh pr create`) is deliberately NOT here: it is `fleet pr`, and it is still
    unwired — so this verb satisfies §3.4's success criterion only up to "a `PullRequestDraft`
    with a resolvable non-null `url`", which it says and does not claim.
    """
    with _mapped_errors():
        opts, settings, run_id = _phase_preflight(ctx)
        # §11.3/§12.22: evict the Bazel disk cache to `budgets.max_disk_gb`, then refuse to
        # start at all if the volume is still under `preflight.min_free_bytes` (exit 9). A phase
        # that consumes gigabytes does not get to discover ENOSPC inside a write transaction.
        _require_disk_headroom(settings)
        _validate_verify_flags(rdeps=rdeps, rdeps_limit=rdeps_limit, rdeps_sample_n=rdeps_sample_n)
        _check_wave_budget(opts, settings, run_id, wave)
        result = _run(
            _verify_impl(
                opts,
                settings,
                opts.db_path,
                run_id=run_id,
                wave=wave,
                only=repo,
                rdeps_limit=rdeps_limit,
                rdeps_sample_n=rdeps_sample_n,
                affected_only=affected_only,
            )
        )
        _emit(opts, result, _verify_lines(result))
        _raise_for_phase(result)


@app.command("migrate")
def migrate_repos(
    ctx: typer.Context,
    wave: Annotated[int | None, typer.Option("--wave")] = None,
    repo: Annotated[str | None, typer.Option("--repo")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """Run the four phases end to end over the fleet.

    **Distinct from `fleet migrate-db`, which is DDL.** §10 is explicit that this verb — like
    every other command except `migrate-db` — *reads* `PRAGMA user_version` and refuses on a
    mismatch rather than upgrading underneath a live run. Merging the two verbs would put a
    table rebuild on the startup path of the busiest command in the harness.
    """
    with _mapped_errors():
        opts, settings, run_id = _phase_preflight(ctx)
        _check_wave_budget(opts, settings, run_id, wave)
        _ = (repo, dry_run)
        # Every phase has a verb of its own today (`scan`/`sequence`, `transform`, `build`,
        # `verify`); what `migrate` lacks is the driver that chains them into one run. The module
        # below is Phase 2's worker — a pointer, not a claim that it is unwritten.
        _unavailable("migrate", "src/fleet/workers/relocate.py (Phase 2 of the end-to-end run)")


# --------------------------------------------------------------------------------------
# fleet sequence — pure computation; refuses mid-flight with exit 11
# --------------------------------------------------------------------------------------


@app.command()
def sequence(
    ctx: typer.Context,
    break_cycles_mode: Annotated[
        BreakCyclesMode, typer.Option("--break-cycles")
    ] = BreakCyclesMode.AUTO,
    accept_breaks: Annotated[list[str] | None, typer.Option("--accept-breaks")] = None,
    hoist_contracts: Annotated[
        bool | None, typer.Option("--hoist-contracts/--no-hoist-contracts")
    ] = None,
    max_hoists_per_scc: Annotated[int | None, typer.Option("--max-hoists-per-scc")] = None,
    force_hoist: Annotated[list[str] | None, typer.Option("--force-hoist")] = None,
    forbid_hoist: Annotated[list[str] | None, typer.Option("--forbid-hoist")] = None,
    min_confidence: Annotated[float | None, typer.Option("--min-confidence")] = None,
    edge_kinds: Annotated[str | None, typer.Option("--edge-kinds")] = None,
    scc_atomic_threshold: Annotated[int | None, typer.Option("--scc-atomic-threshold")] = None,
    force_resequence: Annotated[
        bool, typer.Option("--force-resequence", help="Resequence even with work in flight.")
    ] = False,
    emit: Annotated[
        Path | None, typer.Option("--emit", help="Write the plan here as JSON.")
    ] = None,
) -> None:
    """Phase 1 steps 6–8: build the DAG, break cycles, assign waves, audit collisions.

    Pure computation — it never touches the network. It **refuses with exit 11 while the fleet is
    in flight** (§3.1): renumbering the waves under a repo that is mid-transform moves it into a
    wave whose dependencies have not landed. Exit 11 rather than 3 on purpose: the refusal changed
    nothing and cost nothing, whereas exit 3 is a sticky `halted = 1` ledger.
    """
    opts = _options(ctx)
    with _mapped_errors():
        settings = _load_settings(opts)
        path = _require_db(opts)
        _check_schema_version(path)
        # Tri-state on purpose: the flag defaults to `None`, not to `True`. A `True` default
        # OVERRIDES `graph.hoist_contracts: false` on every invocation, which — now that the
        # contracts really are threaded into `break_cycles` — would hoist under a config that
        # forbade it, the exact silent lie `_sequence_graph_config` refuses flags to avoid.
        hoisting = (
            settings.config.graph.hoist_contracts if hoist_contracts is None else hoist_contracts
        )
        graph_config = _sequence_graph_config(
            settings,
            break_cycles_mode=break_cycles_mode,
            accept_breaks=accept_breaks,
            force_hoist=force_hoist,
            forbid_hoist=forbid_hoist,
            hoist_contracts=hoisting,
            max_hoists_per_scc=max_hoists_per_scc,
            min_confidence=min_confidence,
            scc_atomic_threshold=scc_atomic_threshold,
        )
        result = _run(
            _sequence_impl(
                opts,
                settings,
                path,
                graph_config=graph_config,
                force_resequence=force_resequence,
                hoist_contracts=hoisting,
                min_confidence=min_confidence,
                edge_kinds=edge_kinds,
                emit=emit,
            )
        )
        _emit(
            opts,
            result,
            [
                f"run {result['run_id']}: {result['waves']} waves over "
                f"{result['repos']} repos ({result['edges']} ordering edges)"
            ],
        )


def _sequence_graph_config(
    settings: FleetSettings,
    *,
    break_cycles_mode: BreakCyclesMode,
    accept_breaks: Sequence[str] | None,
    force_hoist: Sequence[str] | None,
    forbid_hoist: Sequence[str] | None,
    hoist_contracts: bool,
    max_hoists_per_scc: int | None,
    min_confidence: float | None,
    scc_atomic_threshold: int | None,
) -> GraphSection:
    """§10's cycle flags: each one is either an override on the `GraphSection` `break_cycles()`
    reads, or a refusal naming the parameter it would need.

    `break_cycles(graph, *, contracts, config)` takes no per-invocation overrides at all, so the
    only honest way to thread a flag is through the config object it is handed. Four of the six
    have a key there and are threaded. The other three do not, and they are REFUSED with exit 2
    rather than accepted and dropped: an operator who typed `--forbid-hoist acme/billing` and got
    a run that hoisted it anyway has been told a lie by the exit code.

    `--break-cycles manual` is refused for a sharper reason: `GraphSection.break_cycles` exists as
    a key but `cycles.py` never reads it, so threading it would produce exactly the silent no-op
    this function exists to eliminate.
    """
    refused: list[str] = []
    if break_cycles_mode is not BreakCyclesMode.AUTO:
        refused.append(
            "--break-cycles manual (graph/cycles.py never reads GraphSection.break_cycles; "
            "every SCC is ranked and broken by the 6c ladder, and MANUAL is an OUTCOME of that "
            "ladder rather than an input to it)"
        )
    if accept_breaks:
        refused.append(
            "--accept-breaks (break_cycles() has no parameter for a pre-approved feedback edge; "
            "the 6d break set is derived, and pre-accepting one would need an override argument "
            "that does not exist)"
        )
    if force_hoist:
        refused.append("--force-hoist (break_cycles() takes no forced-contract set)")
    if forbid_hoist:
        refused.append("--forbid-hoist (break_cycles() takes no forbidden-contract set)")
    if refused:
        raise UsageError(
            "these flags parse but nothing downstream can honour them, so this run is refused "
            "rather than run with them ignored: " + "; ".join(refused)
        )

    overrides: dict[str, object] = {"hoist_contracts": hoist_contracts}
    if max_hoists_per_scc is not None:
        overrides["max_hoists_per_scc"] = max_hoists_per_scc
    if scc_atomic_threshold is not None:
        overrides["scc_atomic_threshold"] = scc_atomic_threshold
    if min_confidence is not None:
        overrides["min_confidence"] = min_confidence
    # `model_copy` would skip validation; a re-validated construction keeps `ge=`/`le=` bounds
    # on a CLI-supplied number, so `--scc-atomic-threshold 0` is a usage error and not a graph
    # that silently condenses everything.
    try:
        return GraphSection.model_validate(
            settings.config.graph.model_dump() | overrides
        )
    except ValidationError as exc:
        raise UsageError(f"invalid --break-cycles override: {exc}") from exc


async def _sequence_impl(
    opts: GlobalOptions,
    settings: FleetSettings,
    path: Path,
    *,
    graph_config: GraphSection,
    force_resequence: bool,
    hoist_contracts: bool,
    min_confidence: float | None,
    edge_kinds: str | None,
    emit: Path | None,
) -> dict[str, object]:
    conn = await connect_ro(path)
    try:
        run_id = await _resolve_run(conn, opts)

        in_flight = await _in_flight_repos(conn, run_id)
        if in_flight and not force_resequence:
            raise SequenceRefusedError(
                f"run {run_id} has {len(in_flight)} repo(s) in flight "
                f"({', '.join(in_flight[:5])}{'…' if len(in_flight) > 5 else ''}); "
                "re-sequencing now would move a live repo into a wave whose dependencies have "
                "not landed (§3.1). Pass --force-resequence to override, or `fleet abort` first."
            )

        await _refuse_unresolved_collisions(conn, run_id)

        # §3.1 6c-H's input. Loaded only when hoisting is on, so `--no-hoist-contracts` /
        # `graph.hoist_contracts: false` recovers the pre-ADR-0019 ladder by handing step 6 an
        # empty candidate set rather than by hoping a downstream guard notices.
        contracts = await _sequence_contracts(conn, run_id) if hoist_contracts else ()
        nodes = await _graph_nodes(conn, run_id)
        edges = await _graph_edges(conn, run_id)
        graph = build_graph(
            nodes,
            edges,
            dag_edge_kinds=_edge_kinds(edge_kinds, settings),
            min_confidence=(
                settings.config.graph.min_confidence if min_confidence is None else min_confidence
            ),
            hoist_contracts=hoist_contracts,
        )
        report = break_cycles(graph, contracts=contracts, config=graph_config)
        manual = tuple(
            res.scc_id for res in report.resolutions if res.break_strategy is BreakStrategy.MANUAL
        )
        if manual:
            raise UnresolvedFindingsError(
                f"{len(manual)} SCC(s) resolved MANUAL and can never be sequenced: "
                f"{', '.join(manual)}. §10 exit 6 — resolve them, or exclude their members."
            )
        # §3.1 step 7: "a repo gated into REQUIRES_HUMAN_INTERVENTION by preflight is NEVER
        # assigned a wave". An empty repo SKIPPED by step 1's gate is out of the fleet on the
        # same principle — giving it a wave index would make it a member the wave waits on.
        wave_plan = assign_waves(report, gated_repo_ids=await _gated_repos(conn, run_id))
    finally:
        await conn.close()

    # The hoists 6c-H committed this run, as opposed to the ones it inherited pre-committed.
    pre_committed = {
        contract.contract_id
        for contract in contracts
        if contract.status in (ContractStatus.HOISTED, ContractStatus.MIGRATED)
    }
    newly_hoisted = tuple(
        contract.contract_id
        for contract in report.hoisted_contracts
        if contract.contract_id not in pre_committed
    )

    async with StateWriter(path, owner="fleet-sequence") as writer:
        conn_ro = await connect_ro(path)
        try:
            if newly_hoisted:
                await writer.submit(_hoisted_contract_rows(run_id, newly_hoisted))
            from fleet.orchestrator.scheduler import SqliteSchedulerStore

            store = SqliteSchedulerStore(writer=writer, read_conn=conn_ro)
            await store.record_plan(
                run_id,
                wave_plan,
                now=_now(),
                max_usd_per_repo=settings.config.budgets.wave_max_cost_usd_per_repo,
            )
            await _persist_cycle_findings(writer, run_id, wave_plan.cycle_findings, now=_now())
        finally:
            await conn_ro.close()

    payload: dict[str, object] = {
        "run_id": run_id,
        "waves": len(wave_plan.waves),
        "repos": len(wave_plan.repo_members),
        "contracts": len(wave_plan.contract_members),
        "hoisted": list(newly_hoisted),
        "edges": len(graph.ordering_edge_keys),
        "excluded": list(wave_plan.excluded_repo_ids),
        "wave_index_by_repo": dict(wave_plan.repo_wave_index),
        # How each SCC was broken. Reported rather than only computed because `CONTRACT_HOIST` vs
        # `ATOMIC_WAVE` is the difference between three independent PRs and one 3-repo PR, and
        # `waves`/`repos` counts cannot tell an operator which of the two they got.
        "cycles": [
            {
                "scc_id": finding.scc_id,
                "members": list(finding.members),
                "break_strategy": finding.break_strategy.value,
                "hoisted_contract_ids": list(finding.hoisted_contract_ids),
                "broken_edge_keys": list(finding.broken_edge_keys),
            }
            for finding in wave_plan.cycle_findings
        ],
        "forced": force_resequence,
    }
    if emit is not None:
        emit.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(emit, json.dumps(payload, indent=2, sort_keys=True, default=str))
    return payload


CYCLE_FINDING_KIND: Final = "CycleDetected"
"""The `findings.kind` `state/projection.py` and `state/digest.py` BOTH read (§6, §11.6).

Until `fleet sequence` wrote these rows, step 6 computed a `CycleFinding` per non-trivial SCC —
members, feedback edges, the 6c ladder's `break_strategy`, the edges 6d actually suppressed — and
then dropped every one of them on the floor. Two readers were already looking for them:
`MigrationState.cycles` (`SELECT payload FROM findings WHERE kind = 'CycleDetected'`), which was
therefore empty in production and could never say why a repo migrated in an atomic wave; and the
§11.6 `cycles` digest section, which hashed an EMPTY list while its module docstring claimed
"each `CycleFinding`'s `break_strategy` + `hoisted_contract_ids` + `broken_edge_keys`" as an
input. That second one is the dangerous half: §12.21 proves two runs equivalent by digest, and a
digest blind to how the cycles were broken calls an `EDGE_BREAK` run and an `ATOMIC_WAVE` run over
the same fleet byte-identical.
"""


async def _persist_cycle_findings(
    writer: StateWriter,
    run_id: str,
    findings: Sequence[CycleFinding],
    *,
    now: datetime,
) -> None:
    """Write step 6's `CycleFinding`s as `CycleDetected` rows, in the shape both readers expect.

    The payload is the model's own `model_dump_json()` and nothing else, because the two readers
    parse it two different ways and only the full model satisfies both: `projection.py` calls
    `CycleFinding.model_validate_json(payload)` — so a hand-rolled summary dict would fail
    validation and empty `MigrationState.cycles` exactly as before — and `digest.py` reads
    `scc_id` / `break_strategy` / `hoisted_contract_ids` / `broken_edge_keys` straight out of the
    JSON *without* the model, deliberately, so the digest cannot move because a model default did.

    `repo_id` is NULL: an SCC spans repos by definition and the column is a single-repo FK, so
    attributing the finding to one member would be a claim about that member. `fingerprint` is
    the `scc_id`, which is content-derived over the member set — a re-sequence that decides the
    same thing rewrites the same row (§11.7), and one that decides differently rewrites its
    payload rather than accumulating a second opinion.

    DELETE-then-insert, in one unit under the writer's `BEGIN IMMEDIATE`: an SCC that a re-scan
    dissolved must not leave a stale `CycleDetected` row behind, or the digest would keep hashing
    a cycle the graph no longer has and `MigrationState.cycles` would report it to an operator.
    """
    rows = [
        (
            run_id,
            CYCLE_FINDING_KIND,
            "warn",
            finding.scc_id,
            redact_text(finding.model_dump_json()),
            _iso(now),
        )
        for finding in findings
    ]

    async def unit(conn: aiosqlite.Connection) -> None:
        await conn.execute(
            "DELETE FROM findings WHERE run_id = ? AND kind = ?", (run_id, CYCLE_FINDING_KIND)
        )
        if rows:
            await conn.executemany(
                "INSERT INTO findings (run_id, repo_id, kind, severity, fingerprint, payload, "
                "                      created_at) VALUES (?, NULL, ?, ?, ?, ?, ?)",
                rows,
            )

    await writer.submit(unit)


def _edge_kinds(spec: str | None, settings: FleetSettings) -> tuple[EdgeKind, ...]:
    if spec is None:
        return tuple(settings.config.graph.dag_edge_kinds)
    try:
        return tuple(EdgeKind(name.strip()) for name in spec.split(",") if name.strip())
    except ValueError as exc:
        raise UsageError(
            f"--edge-kinds {spec!r}: {exc}. Valid: {', '.join(k.value for k in EdgeKind)}"
        ) from exc


async def _refuse_unresolved_collisions(conn: aiosqlite.Connection, run_id: str) -> None:
    """§10 exit 6: an unresolved `severity='error'` collision fails `fleet sequence`."""
    rows = await _rows(
        conn,
        "SELECT kind, key FROM collisions "
        " WHERE run_id = ? AND severity = 'error' AND resolution IS NULL ORDER BY kind, key",
        (run_id,),
    )
    if rows:
        listed = ", ".join(f"{row[0]}:{row[1]}" for row in rows[:5])
        raise UnresolvedFindingsError(
            f"{len(rows)} unresolved severity='error' collision(s) ({listed}"
            f"{'…' if len(rows) > 5 else ''}); §3.1 step 8 detects them BEFORE any transform so "
            "they are resolved in config, not in a half-migrated monorepo."
        )


async def _graph_nodes(conn: aiosqlite.Connection, run_id: str) -> list[GraphNode]:
    """Repos, plus the contracts that are actually *committed* — `HOISTED` or `MIGRATED`.

    §3.1 5b (viii): candidate retargets are applied "**only** when the contract is actually
    hoisted in 6c-H, so a detected-but-unhoisted contract changes ordering by exactly nothing".
    An `EXTRACTABLE` row admitted here would be an edgeless node, and an edgeless node is a wave
    member: `fleet sequence` would report a contract in wave 0 that no step ever hoisted, and
    `check_criterion_c` counts `HOISTED`/`MIGRATED` contracts against exactly that member list.
    6c-H adds the nodes it commits itself, so nothing is lost by leaving them out here.
    """
    repos = await _rows(conn, "SELECT repo_id FROM repos ORDER BY repo_id")
    contracts = await _rows(
        conn,
        "SELECT contract_id FROM contracts WHERE run_id = ? AND status IN ('HOISTED','MIGRATED')"
        " ORDER BY contract_id",
        (run_id,),
    )
    return [GraphNode(kind=NodeKind.REPO, node_id=str(row[0])) for row in repos] + [
        GraphNode(kind=NodeKind.CONTRACT, node_id=str(row[0])) for row in contracts
    ]


async def _sequence_contracts(
    conn: aiosqlite.Connection, run_id: str
) -> tuple[ContractNode, ...]:
    """Rehydrate the whole `contracts` table as the `ContractNode`s §3.1 6c-H ranks.

    Not `_committed_contracts`, which reads back the seven fields a *rebuild* must not lose. 6c-H
    needs strictly more, and every one of them is load-bearing rather than decorative:
    `owning_repo_id` + `extractable` + `status` + `extraction_confidence` select the candidate set;
    `source_paths`/`generated_paths` are what `cycles._materialize` matches `edges.evidence_path`
    against, so an omitted one silently retargets nothing and the SCC never dissolves; and
    `consumer_repo_ids` is half of `blast_radius(k)`, the third key of the hoist ranking.

    Every status is returned, not just `EXTRACTABLE`: `break_cycles` itself is what separates the
    pre-committed `HOISTED`/`MIGRATED` rows (materialized, never re-ranked) from the candidates,
    and a filter here would take that decision away from the step that owns it.
    """
    rows = await _rows(
        conn,
        "SELECT contract_id, kind, identifier, owning_repo_id, source_paths, generated_paths,"
        "       consumer_repo_ids, extractable, extraction_confidence, confidence_factors,"
        "       content_sha256, hoist_target_path, status, status_detail, detected_at"
        "  FROM contracts WHERE run_id = ? ORDER BY contract_id",
        (run_id,),
    )
    return tuple(
        ContractNode(
            contract_id=str(row[0]),
            kind=ContractKind(str(row[1])),
            identifier=str(row[2]),
            owning_repo_id=None if row[3] is None else str(row[3]),
            source_paths=list(json.loads(str(row[4]) or "[]")),
            generated_paths=list(json.loads(str(row[5]) or "[]")),
            consumer_repo_ids=list(json.loads(str(row[6]) or "[]")),
            extractable=bool(row[7]),
            extraction_confidence=float(row[8]),
            confidence_factors=dict(json.loads(str(row[9]) or "{}")),
            content_sha256=str(row[10] or ""),
            hoist_target_path=None if row[11] is None else str(row[11]),
            status=ContractStatus(str(row[12])),
            status_detail=str(row[13] or ""),
            detected_at=datetime.fromisoformat(str(row[14])),
        )
        for row in rows
    )


def _hoisted_contract_rows(
    run_id: str, hoisted: Sequence[str]
) -> Callable[[aiosqlite.Connection], Coroutine[Any, Any, None]]:
    """Promote the contracts 6c-H just committed to `status='HOISTED'`.

    Without this write the hoist exists only inside one process: §3.1 requires an already-`HOISTED`
    contract to be treated as **pre-committed** and "never re-ranked or re-proposed", and the only
    way the *next* `fleet sequence` can know that is the row. It is also what makes
    `fleet scan && fleet sequence && fleet sequence` leave `contracts` unchanged. Only `status`
    moves: `hoist_target_path` was decided by 5b and is already on the row (§6's CHECK makes it
    non-null for anything `extractable`), and `detected_at` records the detection, not this run.
    """

    async def unit(conn: aiosqlite.Connection) -> None:
        await conn.executemany(
            "UPDATE contracts SET status = 'HOISTED' WHERE run_id = ? AND contract_id = ?",
            [(run_id, contract_id) for contract_id in hoisted],
        )

    return unit


async def _graph_edges(conn: aiosqlite.Connection, run_id: str) -> list[DependencyEdge]:
    """Rehydrate `DependencyEdge` from the `edges` table.

    `dst_coord_key` is a `Coordinate.key` (`eco:group:name`) when `dst_kind='REPO'`, which is all
    the ordering needs; `version_spec` and `confidence_factors` come back verbatim so a
    re-sequenced run's `run_digest` is the same one the first sequencing produced (§11.6).
    """
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
        edges.append(
            DependencyEdge(
                edge_key=str(row[0]),
                src_kind=NodeKind(str(row[1])),
                src_id=str(row[2]),
                dst_kind=dst_kind,
                dst_id=None if row[4] is None else str(row[4]),
                dst_coordinate=(
                    _coordinate(str(row[5]), row[7]) if dst_kind is NodeKind.REPO else None
                ),
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


def _coordinate(coord_key: str, version_spec: object) -> Coordinate:
    eco, _, rest = coord_key.partition(":")
    group, _, name = rest.partition(":")
    return Coordinate(
        ecosystem=Ecosystem(eco),
        group=group,
        name=name or coord_key,
        version_spec=None if version_spec is None else str(version_spec),
    )


# --------------------------------------------------------------------------------------
# fleet transform
# --------------------------------------------------------------------------------------


def _parse_context_policies(
    raw: Sequence[str], max_attempts: int
) -> dict[int, ContextPolicy]:
    """`--context-policy 2=EVIDENCE_PLUS_PRIORS`, repeatable (§10, ADR-0021).

    Three refusals, all exit 2 and all deterministic (no prompt is ever composed to find out):
    rung `1` is the deterministic rung and has no prompt to give a policy to; a rung outside
    `1..max_attempts` names a ladder step that does not exist; and a policy name outside
    `ContextPolicy` would be silently dropped into the `llm_cache` key.
    """
    policies: dict[int, ContextPolicy] = {}
    for item in raw:
        rung_text, sep, policy_text = item.partition("=")
        if not sep:
            raise UsageError(
                f"--context-policy {item!r} is not `N=POLICY` (e.g. `2=EVIDENCE_PLUS_PRIORS`)"
            )
        try:
            rung = int(rung_text)
        except ValueError as exc:
            raise UsageError(
                f"--context-policy {item!r}: rung {rung_text!r} is not an int"
            ) from exc
        if rung == 1:
            raise UsageError(
                "--context-policy 1=...: rung 1 is deterministic and composes no prompt, so a "
                "context policy there has nothing to govern (§10). Override rungs 2..N."
            )
        if not 1 <= rung <= max_attempts:
            raise UsageError(
                f"--context-policy {rung}=...: transform.ladder has rungs 1..{max_attempts}; "
                f"rung {rung} does not exist (§10)."
            )
        try:
            policies[rung] = ContextPolicy(policy_text)
        except ValueError as exc:
            valid = ", ".join(p.value for p in ContextPolicy)
            raise UsageError(
                f"--context-policy {rung}={policy_text!r} is not a ContextPolicy; valid: {valid}"
            ) from exc
    return policies


@app.command()
def transform(
    ctx: typer.Context,
    wave: Annotated[int | None, typer.Option("--wave")] = None,
    repo: Annotated[str | None, typer.Option("--repo")] = None,
    max_attempts: Annotated[int, typer.Option("--max-attempts")] = 3,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    deterministic_only: Annotated[bool, typer.Option("--deterministic-only")] = False,
    stub_blocked: Annotated[bool, typer.Option("--stub-blocked")] = False,
    context_policy: Annotated[
        list[str] | None,
        typer.Option("--context-policy", help="N=POLICY, repeatable (ADR-0021)."),
    ] = None,
    no_anchoring_guard: Annotated[bool, typer.Option("--no-anchoring-guard")] = False,
) -> None:
    """Phase 2: apply the relocation plan and the rewrite ladder; the commit is the record.

    The composition is the scan path's: `RunContext` builds the client, the ledger and the
    limits, `PhaseRunner` drives one wave at a time, the workers come from the registry, and the
    evidence reaches SQLite through a fenced sink BEFORE the status that vouches for it (§11.5).

    `--deterministic-only` is Phase 2's `--skip-classify`: it caps the ladder at rung 1, which is
    the rung that composes no prompt (`workers/base.context_policy_for_attempt`), so the run
    provably reaches no model. It is a property of the command line, not of a mock.
    """
    with _mapped_errors():
        opts, settings, run_id = _phase_preflight(ctx)
        ladder = _validate_transform_flags(
            settings,
            max_attempts=max_attempts,
            deterministic_only=deterministic_only,
            stub_blocked=stub_blocked,
            context_policy=context_policy or (),
            no_anchoring_guard=no_anchoring_guard,
        )
        _check_wave_budget(opts, settings, run_id, wave)
        result = _run(
            _transform_impl(
                opts,
                settings,
                opts.db_path,
                run_id=run_id,
                wave=wave,
                only=repo,
                ladder=ladder,
                dry_run=dry_run,
            )
        )
        unprobed = cast("list[str]", result["parse_probe_unavailable"])
        _emit(
            opts,
            result,
            [
                f"run {result['run_id']}: {result['succeeded']} transformed, "
                f"{result['failed']} needing a human ({result['commits']} commits over "
                f"{result['repos']} repo(s) in wave(s) {result['waves']})",
                *(
                    [
                        "warning: the §3.2 parse probe DID NOT RUN for "
                        f"{len(unprobed)} item(s) — no rewrite engine is installed, or no "
                        f"transform rule claims the file: {unprobed[0]}"
                    ]
                    if unprobed
                    else []
                ),
            ],
        )
        code = int(str(result["exit_code"]))
        if code == ExitCode.REQUIRES_HUMAN_INTERVENTION:
            raise HumanInterventionError(
                f"{result['failed']} repo(s) ended REQUIRES_HUMAN_INTERVENTION: "
                f"{', '.join(str(name) for name in cast('list[str]', result['attention']))}"
            )
        if code != ExitCode.SUCCESS:
            raise FleetCliError(str(result["halt"]), exit_code=code)


def _validate_transform_flags(
    settings: FleetSettings,
    *,
    max_attempts: int,
    deterministic_only: bool,
    stub_blocked: bool,
    context_policy: Sequence[str],
    no_anchoring_guard: bool,
) -> int:
    """Every transform flag either does what it says or is refused here (§10).

    Same rule as `_validate_scan_flags`, for the same reason: a flag that parses and is then
    dropped is read by the operator as honoured. The three refusals below each name the module
    that would have to exist for the flag to mean anything.
    """
    declared = settings.config.transform.max_attempts
    if max_attempts < 1:
        raise UsageError(f"--max-attempts {max_attempts} must be at least 1")
    if max_attempts > declared:
        raise UsageError(
            f"--max-attempts {max_attempts}: transform.ladder declares {declared} rung(s) (§9) "
            f"and a rung that does not exist has no tier and no context policy. Lower the flag, "
            f"or add rungs to transform.ladder."
        )
    # Parsed BEFORE it is refused: `--context-policy 1=…` and an unknown policy name are their
    # own documented exit-2 refusals, and they must keep their messages.
    _parse_context_policies(context_policy, max_attempts)
    if context_policy:
        raise UsageError(
            "--context-policy is not implemented: the ADR-0021 rung policies are derived from "
            "`models.tasks.DEFAULT_LADDER` by `workers/base.context_policy_for_attempt`, which "
            "reads no config, so a per-run override would be parsed and then ignored by every "
            "rung. Edit `transform.ladder` (§9) once that value reaches the worker."
        )
    if no_anchoring_guard:
        raise UsageError(
            "--no-anchoring-guard names a guard that does not exist: §3.2 step 5's "
            "`approach_signature` fingerprinting has no implementation (`rewrite/approach.py` is "
            "absent) and no `rejected_approaches` row is ever written, so there is nothing to "
            "disable. Accepting the flag would report a guard as bypassed that never ran."
        )
    if stub_blocked:
        raise UsageError(
            "--stub-blocked is not implemented: emitting a generated stub for a blocked "
            "dependency (§3.5.1) has no worker in src/fleet/workers/ and would write no `stubs` "
            "row, so the flag would silently transform the repo WITHOUT the stub it promised."
        )
    return 1 if deterministic_only else max_attempts


# --------------------------------------------------------------------------------------
# fleet transform — the Phase 2 pipeline, composed from the REGISTERED workers (§3.2)
# --------------------------------------------------------------------------------------

RELOCATE_UNIT: Final = "relocate:"
REWRITE_UNIT: Final = "rewrite:"
"""Unit-id namespaces. One namespace per step, because §11.5's checkpoint is a flat list of unit
ids and a bare path would make "moved `src/x.ts`" and "rewrote `src/x.ts`" the same unit — a
resume would then skip the rewrite because the move landed."""

TRANSFORM_STEPS: Final[tuple[str, ...]] = ("relocate", "rewrite")
"""§3.2 steps 1 and 3–6, in order, and each name is the REGISTRY key of the worker that performs
it — `get_worker(step)`, never a constructor call on a concrete class (§7.2)."""


class TransformStepUnavailableError(FleetCliError):
    """A repo cannot be transformed because a step §3.2 names has no implementation here.

    Distinct from `CommandUnavailableError`: the verb runs, and the gap is per repo — so it is
    recorded against that repo (`REQUIRES_HUMAN_INTERVENTION` plus a finding) and the fleet
    continues, which is §11.1's isolation rule rather than a run-wide stop.
    """

    exit_code = ExitCode.UNEXPECTED_ERROR


class TransformCriterionError(FleetCliError):
    """§3.2's success criterion does not hold for a repo whose phase says `SUCCEEDED`.

    Exit 6 rather than 1: the run finished and the state is readable, but a claim it makes about
    the tree is false, and shipping that to Phase 3 would rewrite it into monorepo history.
    """

    exit_code = ExitCode.UNRESOLVED_FINDINGS


class TransformInput(WorkerInput):
    """One repo's whole Phase 2 dispatch: the relocation plan and the rewrite target set.

    Both lists are computed by the driver from the tree **at the phase anchor**, never from the
    worktree as it stands: after step 1 has run once, `git ls-files` returns the relocated paths,
    and a plan rebuilt from those would move `java/com/x` to `java/com/java/com/x` (§7.1).
    """

    repo_id: RepoId
    branch: str = Field(min_length=1, description="`migrate/<repo>`; run-reconciled, not scoped")
    phase_pre_commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    dest_path: str = Field(min_length=1)
    sources: tuple[str, ...] = ()
    targets: tuple[str, ...] = ()
    rules: tuple[RewriteRule, ...] = ()
    engines: dict[str, str] = Field(default_factory=dict)
    params: dict[str, str] = Field(default_factory=dict)
    max_passes: int = Field(default=3, ge=1)
    max_patch_bytes: int = Field(
        default=1_048_576,
        gt=0,
        description="`transform.max_patch_bytes` (`settings.py:452`); `_transform_payloads` "
        "threads the configured value here so `_rewrite_input` can copy it onto "
        "`RewriteInput.max_patch_bytes` — see that field's docstring in `workers/rewrite.py`.",
    )
    remaining_units: tuple[str, ...] | None = None


class TransformOutput(WorkerOutput):
    """What one repo's transform landed, per step, plus the rung that landed it.

    `attempt`/`tier` travel on the output because the `ResultSink` is handed a `WorkerResult` and
    not a `WorkerContext`, and an `attempts` row without its rung number is not evidence.
    """

    repo_id: RepoId
    attempt: int = 1
    tier: str = str(TransformTier.DETERMINISTIC)
    context_policy: str | None = None
    """§6 CHECKs `(tier = 'DETERMINISTIC') = (context_policy IS NULL)`, so the rung's policy has
    to travel beside its tier or an escalated rung's `attempts` row is unwritable."""

    relocate: RelocateOutput | None = None
    rewrite: RewriteOutput | None = None
    commits: list[str] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)
    rewritten: list[str] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)


def _transform_units(payload: TransformInput) -> list[str]:
    """Every unit this dispatch owns, in execution order: the moves, then the rewrites."""
    return [
        *(f"{RELOCATE_UNIT}{source}" for source in payload.sources),
        *(f"{REWRITE_UNIT}{target}" for target in payload.targets),
    ]


class TransformPipelineWorker(BaseWorker[TransformInput, TransformOutput]):
    """§3.2 steps 1 and 3–6 as ONE dispatch of ONE phase, chaining the two registered workers.

    Why one dispatch and not two: `phases` holds a single row per `(run, repo, phase)` and
    `acquire_phase_lease` only moves a **PENDING** one, so a second `PhaseRunner` pass over
    `Phase.TRANSFORM` would find the row terminal and admit nothing — the rewrite step would look
    like it ran while it never did. Chaining them under one lease also makes the §11.5 checkpoint
    mean what it says: `completed_units` is a namespaced list of *files*, so a resumed transform
    re-enters at the file that did not land instead of replaying the tree.

    Each step is fetched from the registry by name (§7.2), and each step's own
    `preconditions_hold` is consulted before it is re-entered — nothing else in the harness calls
    that method, and it is the whole of "never blind replay" (`relocate` re-run over a relocated
    tree is the `java/java/com/x` defect §7.1 exists to forbid).
    """

    __slots__ = ("_workers",)

    name: ClassVar[str] = "transform"
    phase: ClassVar[Phase] = Phase.TRANSFORM
    input_model: ClassVar[type[WorkerInput]] = TransformInput
    output_model: ClassVar[type[WorkerOutput]] = TransformOutput

    def __init__(self) -> None:
        self._workers: Mapping[str, BaseWorker[Any, Any]] = {
            step: get_worker(step)() for step in TRANSFORM_STEPS
        }

    async def preconditions_hold(self, ctx: WorkerContext, payload: TransformInput) -> bool:
        """Re-entry is admitted only for a checkpoint whose worktree is still on disk.

        The per-step preconditions are finer and are checked inside `run()`, immediately before
        the step they govern: `rewrite`'s asks whether its targets exist under `dest_path`, which
        is only answerable **after** the relocation step of this same dispatch has run.
        """
        if payload.remaining_units is None:
            return False
        return await asyncio.to_thread(Path(ctx.workdir).is_dir)

    async def run(
        self, ctx: WorkerContext, payload: TransformInput
    ) -> WorkerResult[TransformOutput]:
        units = _transform_units(payload)
        owed = set(units) if payload.remaining_units is None else set(payload.remaining_units)
        output = TransformOutput(
            repo_id=payload.repo_id,
            attempt=ctx.attempt,
            tier=str(ctx.tier),
            context_policy=None if ctx.context_policy is None else str(ctx.context_policy),
        )

        relocate = self._workers["relocate"]
        relocate_input = self._relocate_input(payload, owed)
        if payload.remaining_units is not None and not await relocate.preconditions_hold(
            ctx, relocate_input
        ):
            # §7.1: the checkpoint does not describe this tree, so the phase re-runs whole from
            # its anchor rather than moving a tree it does not describe. Re-running is cheap and
            # safe, not a replay: every unit is re-guarded by §3.2 step 6.1 before it is applied.
            ctx.log.info("transform_checkpoint_rejected", step="relocate", owed=len(units))
            owed = set(units)
            relocate_input = self._relocate_input(payload, owed)

        landed = [unit for unit in units if unit not in owed]
        result = await relocate.run(ctx, relocate_input)
        moved: RelocateOutput | None = result.output
        if moved is not None:
            output.relocate = moved
            output.commits.extend(moved.commits)
            output.skipped.extend(f"{RELOCATE_UNIT}{unit}" for unit in moved.skipped)
        landed.extend(f"{RELOCATE_UNIT}{unit}" for unit in result.completed_units)
        landed = list(dict.fromkeys(landed))
        if result.status != "ok":
            return self._handoff(result, units, landed, output)

        rewrite = self._workers["rewrite"]
        rewrite_input = self._rewrite_input(payload, owed)
        if payload.remaining_units is not None and not await rewrite.preconditions_hold(
            ctx, rewrite_input
        ):
            ctx.log.info("transform_checkpoint_rejected", step="rewrite", owed=len(units))
            rewrite_input = self._rewrite_input(payload, set(units))
        result = await rewrite.run(ctx, rewrite_input)
        rewritten: RewriteOutput | None = result.output
        if rewritten is not None:
            output.rewrite = rewritten
            output.commits.extend(rewritten.commits)
            output.skipped.extend(f"{REWRITE_UNIT}{unit}" for unit in rewritten.skipped)
            output.rewritten.extend(rewritten.rewritten)
            output.unresolved.extend(rewritten.unresolved)
        landed.extend(f"{REWRITE_UNIT}{unit}" for unit in result.completed_units)
        landed = list(dict.fromkeys(landed))
        if result.status != "ok":
            return self._handoff(result, units, landed, output)

        return WorkerResult[TransformOutput](
            status="ok",
            output=output,
            completed_units=landed,
            usage=result.usage,
            evidence=output.commits,
        )

    # -- payload plumbing ----------------------------------------------------------------

    def _relocate_input(self, payload: TransformInput, owed: set[str]) -> RelocateInput:
        return RelocateInput(
            branch=payload.branch,
            phase_pre_commit_sha=payload.phase_pre_commit_sha,
            dest_path=payload.dest_path,
            sources=list(payload.sources),
            completed_units=[
                source
                for source in payload.sources
                if f"{RELOCATE_UNIT}{source}" not in owed
            ],
        )

    def _rewrite_input(self, payload: TransformInput, owed: set[str]) -> RewriteInput:
        return RewriteInput(
            branch=payload.branch,
            phase_pre_commit_sha=payload.phase_pre_commit_sha,
            dest_path=payload.dest_path,
            targets=list(payload.targets),
            rules=list(payload.rules),
            engines=dict(payload.engines),
            params=dict(payload.params),
            max_passes=payload.max_passes,
            max_patch_bytes=payload.max_patch_bytes,
            completed_units=[
                target
                for target in payload.targets
                if f"{REWRITE_UNIT}{target}" not in owed
            ],
        )

    def _handoff(
        self,
        step: WorkerResult[Any],
        units: Sequence[str],
        landed: Sequence[str],
        output: TransformOutput,
    ) -> WorkerResult[TransformOutput]:
        """A step's non-`ok` result is the dispatch's, carrying every commit that landed first.

        The commits travel WITH the failure on purpose: they are on the branch whatever this row
        says, and a result that hid them would have the next rung replay them (§7.1).
        """
        done = list(dict.fromkeys(landed))
        remaining = [unit for unit in units if unit not in set(done)]
        if step.status == "partial" and done:
            return WorkerResult[TransformOutput](
                status="partial",
                output=output,
                completed_units=done,
                remaining_units=remaining,
                usage=step.usage,
                evidence=output.commits,
            )
        status = "failed" if step.status == "partial" else step.status
        error = step.error
        if error is None and status in ("failed", "timeout"):
            error = WorkerError(
                failure_class=FailureClass.UNKNOWN,
                retryable=True,
                stderr_tail=f"{self.name}: a step returned {step.status!r} with no error",
            )
        return WorkerResult[TransformOutput](
            status=cast("Any", status),
            output=output,
            completed_units=done,
            remaining_units=remaining,
            usage=step.usage,
            evidence=output.commits,
            error=error,
        )


# --------------------------------------------------------------------------------------
# the transform plan, the fenced sink and the driver
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _TransformPlan:
    """One repo's Phase 2 plan: where it goes, from which anchor, and what moves and is rewritten.

    Computed once per repo per invocation, BEFORE the lease is taken, because every field is a
    fact about git that the worker must be handed rather than re-derive (§7.1) — and because
    `sources` re-derived after step 1 has run describes an already-relocated tree.
    """

    repo_id: str
    worktree: Path
    branch: str
    dest_path: str
    import_specifier: str
    pre_commit_sha: str
    base_ref: str
    sources: tuple[str, ...]
    targets: tuple[str, ...]


@dataclass(slots=True)
class _TransformEvidence:
    """What the sink saw, keyed by repo — the input to §3.2's success criterion.

    A buffer rather than a table read, because `TransformOutput.unresolved` is the one component
    of the criterion that no column holds: §6 records commits (in git) and attempts, not the
    per-file resolution state of a rung.
    """

    by_repo: dict[str, TransformOutput] = field(default_factory=dict)
    _resolved: dict[str, set[str]] = field(default_factory=dict)
    """Per-repo, the raw rewrite-unit names actually landed under their OWN identity — i.e.
    `WorkerResult.completed_units` entries namespaced `rewrite:`, prefix stripped.

    Deliberately not sourced from `output.rewritten`: since D49 that field holds every landed
    `FilePatch.path`, which a multi-file LLM repair can populate with a SIBLING file collaterally
    touched while resolving a different unit. `completed_units` has no such collateral entries —
    `RewriteWorker.run` only ever appends the loop's own `unit` to it, on every path that
    legitimately resolves that unit (the deterministic land, the idempotent `find_task_commit`
    shortcut, and the repair-rung land) — so it is the identity check `record()` needs."""

    def record(self, output: TransformOutput, completed_units: Sequence[str] = ()) -> None:
        resolved = self._resolved.setdefault(output.repo_id, set())
        resolved.update(
            unit[len(REWRITE_UNIT) :]
            for unit in completed_units
            if unit.startswith(REWRITE_UNIT)
        )
        prior = self.by_repo.get(output.repo_id)
        if prior is None:
            self.by_repo[output.repo_id] = output
            prior = output
        else:
            prior.commits.extend(output.commits)
            prior.skipped.extend(output.skipped)
            prior.rewritten.extend(output.rewritten)
        prior.unresolved = [unit for unit in output.unresolved if unit not in resolved]


class _ScopedWaveStore:
    """`SchedulerStore` narrowed to the repos this invocation actually drives.

    Only the membership is overridden; every durable read and write is delegated. Narrowing it
    here rather than in the runner is what keeps `--repo` honest in both directions: the excluded
    members are not admitted AND they are not counted when the wave is asked whether it closed,
    so an operator transforming one repo of forty does not get told the wave is `OPEN` forever.

    Phases 3 and 4 need the same narrowing for a second reason, which is why this is not named
    after Phase 2: their gate is a *previous phase's verdict*. A repo whose Phase 2 never
    succeeded has no Phase 3 row at all, and counting it as an unsettled member would hold the
    wave `OPEN` forever over work that is not this phase's to do (§3.3 / §3.4 preconditions).
    """

    def __init__(self, inner: SqliteSchedulerStore, only: Sequence[str] | None) -> None:
        self._inner = inner
        self._only = None if only is None else frozenset(only)

    async def record_plan(
        self, run_id: str, plan: WavePlan, *, now: datetime, max_usd_per_repo: float
    ) -> None:
        await self._inner.record_plan(run_id, plan, now=now, max_usd_per_repo=max_usd_per_repo)

    async def wave_indices(self, run_id: str) -> tuple[int, ...]:
        return await self._inner.wave_indices(run_id)

    async def wave_members(self, run_id: str, wave_index: int) -> tuple[str, ...]:
        members = await self._inner.wave_members(run_id, wave_index)
        if self._only is None:
            return members
        return tuple(member for member in members if member in self._only)

    async def wave_started_at(self, run_id: str, wave_index: int) -> datetime | None:
        return await self._inner.wave_started_at(run_id, wave_index)

    async def begin_wave(self, run_id: str, wave_index: int, *, now: datetime) -> datetime:
        return await self._inner.begin_wave(run_id, wave_index, now=now)

    async def blast_radii(self, run_id: str, repo_ids: Sequence[str]) -> Mapping[str, int]:
        return await self._inner.blast_radii(run_id, repo_ids)

    async def append_blocked_by(
        self, run_id: str, repo_id: str, blocker: str, *, now: datetime
    ) -> int:
        return await self._inner.append_blocked_by(run_id, repo_id, blocker, now=now)

    async def append_unblocked_wave(
        self,
        run_id: str,
        repo_ids: Sequence[str],
        *,
        now: datetime,
        max_usd_per_repo: float,
    ) -> int | None:
        return await self._inner.append_unblocked_wave(
            run_id, repo_ids, now=now, max_usd_per_repo=max_usd_per_repo
        )


async def _wave_is_breached(
    settings: FleetSettings,
    *,
    writer: StateWriter,
    read_conn: aiosqlite.Connection,
    repository: SqliteStateRepository,
    run_id: str,
    wave_index: int,
    phase: Phase,
    only: Sequence[str] | None,
) -> bool:
    """Has this wave already spent `budgets.wave_max_wallclock_s`? (D84)

    The `_impl` drivers prepare every member's git BEFORE `_run_*_wave` composes the scheduler
    that decides whether anything can be admitted at all, so on a breached wave they cut branches,
    anchors and worktrees for work that will never run. The predicate they need is
    `WaveScheduler.breached`, but the scheduler is constructed inside `_run_*_wave` — after the
    loops that would have to consult it. So it is constructed here instead, from the same inputs,
    and the formula is not copied: `breached` is the one definition of it.

    `descendants` is deliberately NOT supplied. It is admission's ordering input and `breached`
    reads only `store.wave_started_at`, `clock` and `budgets`; supplying it would run
    `_ordering_pairs` and build a graph once per wave for a predicate that cannot consult it.
    This helper is therefore the `_run_*_wave` scheduler MINUS that one field, which is a
    disclosure rather than a claim that the two objects are identical.
    """
    scheduler = WaveScheduler(
        run_id=run_id,
        phase=phase,
        store=_ScopedWaveStore(SqliteSchedulerStore(writer=writer, read_conn=read_conn), only),
        db=repository,
        budgets=settings.config.budgets,
        clock=_now,
    )
    return await scheduler.breached(wave_index)


class _TransformSink:
    """Persists one dispatch's ADR-0024 pointers under the fence that produced it (§11.5).

    Two writes and no third: an `attempts` row carrying the rung, its cost and the commit it
    produced, and `phases.post_commit_sha`. **No diff, no tree SHA and no patch blob** — git holds
    the code state and SQLite holds a pointer to it, and the moment SQLite holds a second copy of
    the change it can drift from the branch across a crash (ADR-0024).

    Called before the terminal status for the same reason the scan sink is: a `SUCCEEDED` phase
    is never re-admitted, so evidence written after the status is evidence a crash can lose.
    """

    def __init__(
        self,
        *,
        writer: StateWriter,
        repository: SqliteStateRepository,
        read_conn: aiosqlite.Connection,
        run_id: str,
        evidence: _TransformEvidence,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self._writer = writer
        self._repository = repository
        self._read = read_conn
        self._run_id = run_id
        self._evidence = evidence
        self._clock = clock

    async def __call__(
        self, *, repo_id: str, phase: Phase, fence: int, result: WorkerResult[TransformOutput]
    ) -> None:
        output = result.output
        if output is None:  # pragma: no cover - the runner only calls a sink with an output
            return
        self._evidence.record(output, result.completed_units)
        stamp = _iso(self._clock())
        commit = output.commits[-1] if output.commits else None
        error = result.error
        attempt = max(output.attempt, 1)
        command_json = json.dumps(["fleet", "transform", "--repo", repo_id])
        await self._repository.record_attempt(
            AttemptRow(
                attempt_id=str(uuid4()),
                run_id=self._run_id,
                repo_id=repo_id,
                phase=phase,
                attempt=attempt,
                # §6: `retry_ordinal` is in the row's uniqueness key precisely so a RE-EXECUTION
                # of the same rung appends its own row rather than overwriting the first one's
                # outcome. A resumed dispatch is exactly that — same rung, same (empty) command —
                # and without this the `already_applied` a re-run reports would be written onto a
                # row whose `DO UPDATE` clause cannot carry it.
                retry_ordinal=await self._next_ordinal(repo_id, phase, attempt),
                started_at=stamp,
                finished_at=stamp,
                tier=output.tier,
                context_policy=output.context_policy,
                # §6 ties three columns together: `(command = '[]') = (exit_code IS NULL)` and
                # "the ONLY legal way to have executed nothing is an anchoring rejection". A
                # transform rung DID execute — it is a composition of guarded git commits rather
                # than one process — so the row records the unit that was executed (this repo's
                # rung of this verb) and whether it landed. Inventing a `git` argv here would be
                # worse: it would name one of the dozens of commands the rung actually ran.
                command=command_json,
                command_sha256=sha256_text(command_json),
                exit_code=0 if result.status in ("ok", "partial") else 1,
                failure_class=None if error is None else str(error.failure_class),
                duration_ms=result.duration_ms,
                stderr_tail="" if error is None else error.stderr_tail,
                cost_usd=result.usage.cost_usd,
                # §11.6, ALL-hit. Derived from the SAME accumulated `TokenUsage` the line above
                # bills from, which is the whole point of carrying the signal on the usage: the
                # flag and the cost it constrains (`schema.sql`: `1 => cost_usd = 0`) cannot drift
                # apart, because neither is read from anywhere the other is not.
                llm_cache_hit=result.usage.all_served_from_llm_cache,
                commit_sha=commit,
                # §3.2 step 6.1's `already_applied` event: at least one unit of this dispatch was
                # skipped because the guard found its effect already in the tree. It is what makes
                # a re-run visibly free rather than merely quiet.
                already_applied=bool(output.skipped),
            )
        )
        if commit is None:
            return
        params = (commit, stamp, self._run_id, repo_id, int(phase), fence)

        async def unit(conn: aiosqlite.Connection) -> None:
            await conn.execute(
                "UPDATE phases SET post_commit_sha = ?, updated_at = ? "
                " WHERE run_id = ? AND repo_id = ? AND phase = ? AND lease_fence = ?",
                params,
            )

        await self._writer.submit(unit)

    async def _next_ordinal(self, repo_id: str, phase: Phase, attempt: int) -> int:
        rows = await _rows(
            self._read,
            "SELECT COALESCE(MAX(retry_ordinal) + 1, 0) FROM attempts "
            " WHERE run_id = ? AND repo_id = ? AND phase = ? AND attempt = ?",
            (self._run_id, repo_id, int(phase), attempt),
        )
        return int(rows[0][0])


async def _git_output(worktree: Path, args: Sequence[str]) -> str:
    """One git command's FULL stdout, read back off disk.

    `Git.text` returns a bounded tail (§11.3), which is right for a diagnostic and wrong for a
    file list: a truncated `ls-tree` is a relocation plan that moves most of a tree and silently
    drops the rest. `util.proc` already streams stdout to a file, so this reads that file.
    """
    argv = ["git", "-C", str(worktree), *args]
    with scoped_tempdir(prefix="fleet-git-") as sink:
        result = await proc_run(argv, cwd=worktree, log_dir=sink, log_stem="git")
        if not result.ok or result.stdout_path is None:
            raise GitCommandError(
                argv,
                result.exit_code,
                result.stderr_tail,
                cwd=worktree,
                timed_out=result.timed_out,
                started=result.started,
            )
        return await asyncio.to_thread(result.stdout_path.read_text, encoding="utf-8")


def _nul_fields(text: str) -> list[str]:
    return [field for field in text.split("\0") if field]


async def _tracked_at(worktree: Path, rev: str) -> tuple[str, ...]:
    """Every tracked file at `rev`, repo-relative — the §3.2 step 1 relocation plan's domain."""
    listing = await _git_output(worktree, ["ls-tree", "-r", "-z", "--name-only", rev])
    return tuple(sorted(_nul_fields(listing)))


async def _changed_entries(
    worktree: Path, rev_range: str
) -> tuple[tuple[str, str, str], ...]:
    """`(status, pre_image, post_image)` per entry changed across `rev_range`.

    The status is carried rather than collapsed to a path list because §3.2's "every changed path
    is under `<dest>/`" is a statement about what the phase **wrote**, and a relocation
    necessarily deletes its own pre-image outside `<dest>/`. Git cannot settle that for us: a
    rename is only reported as one when the two blobs are similar enough, and a four-line file
    whose one import line changed scores 47% — below `-M`'s 50% default — so the very rewrite
    this phase exists to perform breaks rename pairing. The caller therefore checks deletions
    against the relocation plan, which is exact.
    """
    raw = _nul_fields(await _git_output(worktree, ["diff", "--name-status", "-z", rev_range]))
    entries: list[tuple[str, str, str]] = []
    index = 0
    while index + 1 < len(raw):
        status = raw[index]
        if status[:1] in ("R", "C") and index + 2 < len(raw):
            entries.append((status, raw[index + 1], raw[index + 2]))
            index += 3
            continue
        entries.append((status, raw[index + 1], raw[index + 1]))
        index += 2
    return tuple(sorted(set(entries)))


def _transform_rules(settings: FleetSettings) -> tuple[RewriteRule, ...]:
    """§9 `transform.rules_dir`, loaded and validated.

    A MISSING directory is refused, not treated as zero rules. `transform.rules_dir` defaults
    to `config/rules` and §9 has no "rules disabled" value, so an absent directory and a fleet
    that genuinely runs no rewrite rules were indistinguishable — this used to `return ()` for
    both, and a deleted or mistyped `config/rules` silently downgraded every repo's transform
    phase to a pure relocation, with no refusal to say why. A fleet whose migration really is
    pure relocation is still a legal fleet, but it says so by leaving the directory PRESENT and
    empty: `load_rules` already returns `()` for that case, no special-casing needed here."""
    rules_dir = (settings.root / settings.config.transform.rules_dir).resolve()
    if not rules_dir.is_dir():
        raise ConfigFileError(
            "does not exist. `transform.rules_dir` (§9) defaults to `config/rules` and has no "
            "\"disabled\" setting, so a missing directory used to load silently as zero rules — "
            "downgrading every repo in the fleet to a pure relocation with no rule applied and "
            "no refusal to explain why. If this fleet genuinely has no rewrite rules, create the "
            "directory empty to say so explicitly; otherwise fix `transform.rules_dir` or "
            "restore the directory.",
            file=rules_dir,
        )
    return load_rules(rules_dir)


def _rewrite_targets(
    dest_path: str, sources: Sequence[str], rules: Sequence[RewriteRule]
) -> tuple[str, ...]:
    """§3.2 step 2, deterministically: the relocated files at least one rule claims.

    Claimed by `rules.rule_matches_path` — language ∩ `applies_to` — against the file's path
    **after** relocation, which is the path the rule will actually see. A file no rule claims is
    not a target: `RewriteWorker` reports "no rule produced a change" as `RULE_MISS`, so handing
    it every file in the tree would fail every repo on its first unmatched README.
    """
    targets = [relocated_path(dest_path, source) for source in sources]
    return tuple(
        target
        for target in targets
        if any(rule_matches_path(rule, target) for rule in rules)
    )


async def _prepare_repo(
    settings: FleetSettings,
    *,
    writer: StateWriter,
    run_id: str,
    repo_id: str,
    dest_path: str | None,
    import_specifier: str,
    rules: Sequence[RewriteRule],
    now: datetime,
) -> _TransformPlan:
    """Everything §3.2 requires to be true of git before the first mutation, for one repo.

    In order, and each step is a documented requirement rather than a convenience:

    1. the phase runs in the worktree Phase 1 cut, which is the only tree whose contents the
       `symbols` and `manifests` rows describe;
    2. a crashed predecessor's *dirty worktree* is discarded (§3.2 step 6.4's "No" branch). Only
       the worktree can be dirty — a commit is atomic — so this can never discard landed work;
    3. `migrate/<repo>` exists and is CHECKED OUT, because `git commit` moves `HEAD`, and in a
       detached worktree that is not the branch the §3.2 step 6.1 guard searches;
    4. the phase anchor `refs/fleet/<run>/<repo>/phase-2/base` is a REAL ref, created before the
       first mutation and reused verbatim on re-entry (§3.2 step 6.5), and mirrored into
       `phases.base_ref`/`pre_commit_sha` as a pointer.

    Raises `TransformStepUnavailableError` for a repo this harness cannot transform; the caller
    records that against the repo and the fleet continues (§11.1).
    """
    worktree = (settings.root / settings.config.run.work_dir).resolve() / repo_id
    if not await asyncio.to_thread((worktree / ".git").exists):
        raise TransformStepUnavailableError(
            f"{repo_id}: no Phase 1 worktree at {worktree}. §3.2's input IS the scanned tree; "
            "run `fleet scan` for this repo before transforming it."
        )
    if dest_path is None:
        raise TransformStepUnavailableError(
            f"{repo_id}: §3.3's `layout(repo)` REFUSED a destination for this node — it lies "
            "under the reserved `_scc/` coarsening namespace (§3.1 6e), or its `dest:` escapes "
            "the monorepo root. `layout()` is total otherwise, so this is a fact about this "
            "repo: fix `dest:` in config/repos.yaml, or let §3.1 step 8's collision audit rewrite "
            "it."
        )
    dest = dest_path.rstrip("/")
    git = Git(worktree)
    branch = f"migrate/{repo_id}"

    if await git.is_dirty():
        # A killed `git apply` leaves the worktree dirty and nothing on the branch (§3.2 step
        # 6.4). The worktree is disposable; the branch is the record.
        await git.reset_hard("HEAD")
        await git.clean(directories=True, ignored=True)

    tip = await git.resolve(branch)
    if tip is None:
        await git.exec(["checkout", "-B", branch, "HEAD"])
    else:
        owner = await git.text(
            ["log", "-1", "--format=%(trailers:key=Fleet-Run-Id,valueonly)", branch]
        )
        if owner and owner.strip() != run_id:
            raise TransformStepUnavailableError(
                f"{repo_id}: {branch} was last written by run {owner.strip()}, not {run_id}. "
                "§3.2 step 6 resets a foreign run's branch to the mirror's default branch with a "
                "`branch_reset` finding before cutting the anchor; that reconciliation is not "
                "implemented here, and cutting this run's anchor at run "
                f"{owner.strip()}'s tip would rewrite its rejected edits into monorepo history."
            )
        await git.exec(["checkout", branch])

    anchor_ref = f"refs/fleet/{run_id}/{repo_id}/phase-{int(Phase.TRANSFORM)}/base"
    anchor = await git.resolve(anchor_ref)
    if anchor is None:
        anchor = await git.rev_parse(branch)
        await git.update_ref(anchor_ref, anchor, message="fleet phase-2 anchor")

    sources = await _tracked_at(worktree, anchor)
    if not sources:
        raise TransformStepUnavailableError(
            f"{repo_id}: the tree at {anchor[:12]} has no tracked file, so there is nothing to "
            "relocate and §3.2's non-empty-diff criterion could never hold."
        )
    stamp = _iso(now)
    params = (anchor_ref, anchor, stamp, stamp, run_id, repo_id, int(Phase.TRANSFORM))

    async def unit(conn: aiosqlite.Connection) -> None:
        await conn.execute(
            "UPDATE phases SET base_ref = ?, pre_commit_sha = ?, "
            "    started_at = COALESCE(started_at, ?), updated_at = ? "
            " WHERE run_id = ? AND repo_id = ? AND phase = ?",
            params,
        )

    await writer.submit(unit)
    return _TransformPlan(
        repo_id=repo_id,
        worktree=worktree,
        branch=branch,
        dest_path=dest,
        import_specifier=import_specifier,
        pre_commit_sha=anchor,
        base_ref=anchor_ref,
        sources=sources,
        targets=_rewrite_targets(dest, sources, rules),
    )


async def _abandon_repo(
    writer: StateWriter,
    run_id: str,
    repo_id: str,
    *,
    detail: str,
    now: datetime,
    phase: Phase = Phase.TRANSFORM,
    kind: str = "TransformPreparationFailed",
) -> None:
    """A repo whose git preparation failed: terminal for it, invisible to the rest of the wave.

    **A driver write, and deliberately so** — the same exception `_gate_empty_repos` is. The
    failure happened before any lease existed, so there is no fenced path to it and no worker to
    return a `WorkerResult`; leaving the row `PENDING` instead would re-admit the repo on every
    resume forever, and leaving it out of the wave silently would report a fleet as transformed
    that is missing a member (Rule 11).

    `phase`/`kind` are parameters because Phase 3's git preparation (§3.3 step 1's filtered
    clone, merge and snapshot) fails in exactly the same *place* — before any lease — and a
    second copy of this function would be a second place the containment rule could drift.
    """
    stamp = _iso(now)
    status = (str(RepoStatus.REQUIRES_HUMAN_INTERVENTION), redact_text(detail), stamp)
    finding = (
        run_id,
        repo_id,
        kind,
        _fingerprint(run_id, repo_id, kind),
        redact_text(json.dumps({"repo_id": repo_id, "detail": detail}, sort_keys=True)),
        stamp,
    )

    async def unit(conn: aiosqlite.Connection) -> None:
        await conn.execute(
            "UPDATE phases SET status = ?, last_error = ?, failure_class = 'PREFLIGHT', "
            "    updated_at = ? WHERE run_id = ? AND repo_id = ? AND phase = ? "
            "  AND status = 'PENDING'",
            (*status, run_id, repo_id, int(phase)),
        )
        await conn.execute(
            "INSERT INTO findings (run_id, repo_id, kind, severity, fingerprint, payload, "
            "                      created_at) VALUES (?, ?, ?, 'error', ?, ?, ?) "
            "ON CONFLICT (run_id, IFNULL(repo_id, ''), kind, fingerprint) "
            "DO UPDATE SET payload = excluded.payload, created_at = excluded.created_at",
            finding,
        )

    await writer.submit(unit)


async def _open_transform_waves(
    conn: aiosqlite.Connection, run_id: str, wave: int | None
) -> tuple[int, ...]:
    """The waves this invocation drives, in ascending order — never a wave already settled.

    Scoped to `phase = 2` unlike `_earliest_open_wave`, which joins `phases` on the repo alone:
    after Phase 1 every member has a SUCCEEDED scan row, and an unscoped join therefore reports
    a fleet that has never been transformed as having no open wave at all.
    """
    return await _open_phase_waves(conn, run_id, Phase.TRANSFORM, wave)


async def _open_phase_waves(
    conn: aiosqlite.Connection, run_id: str, phase: Phase, wave: int | None
) -> tuple[int, ...]:
    """`_open_transform_waves` with the phase as a parameter — Phases 3 and 4 need the same join
    for the same reason, and re-deriving it per verb is how one of them ends up unscoped."""
    if wave is not None:
        return (wave,)
    rows = await _rows(
        conn,
        "SELECT DISTINCT m.wave_index FROM wave_members m "
        "  LEFT JOIN phases p ON p.run_id = m.run_id AND p.repo_id = m.node_id "
        "                    AND p.phase = ? "
        " WHERE m.run_id = ? AND m.node_kind = 'REPO' "
        "   AND (p.status IS NULL OR p.status NOT IN ('SUCCEEDED','SKIPPED',"
        "        'REQUIRES_HUMAN_INTERVENTION')) "
        " ORDER BY m.wave_index",
        (int(phase), run_id),
    )
    return tuple(int(row[0]) for row in rows)


async def _wave_repos(
    conn: aiosqlite.Connection, run_id: str, wave_index: int, only: str | None
) -> tuple[str, ...]:
    rows = await _rows(
        conn,
        "SELECT node_id FROM wave_members "
        " WHERE run_id = ? AND wave_index = ? AND node_kind = 'REPO' ORDER BY node_id",
        (run_id, wave_index),
    )
    return tuple(
        str(row[0]) for row in rows if only is None or fnmatch(str(row[0]), only)
    )


async def _import_specifiers(
    conn: aiosqlite.Connection, overrides: Mapping[Ecosystem, str]
) -> dict[str, str]:
    """Every repo's post-migration IMPORT SPECIFIER — what other code writes to import it.

    §3.2 step 2 says a rewrite target is an import path, a package declaration, a `tsconfig` path
    alias, a Go module path, a Python module path. Every one of those is a name in a *language*,
    and until this existed the only monorepo facts a `RewriteRule` could render were
    `{{dest_path}}` and `{{repo_id}}` — a directory and an id. A rule author with a path and a
    dependency to point at composes a Bazel label out of them, and the label reaches a compiler:
    `error TS2307: Cannot find module '//ts/acme/lib:lib'`. That is not a rule-set typo; it is
    the only destination the harness had made expressible.

    Resolved off the registry (`import_specifier`) for the same reason `_dest_paths` resolves
    `layout()` off it: the answer is per-language, so the driver asks the adapter and names no
    ecosystem of its own (§13 row 6). A repo whose destination `layout()` refuses contributes
    nothing rather than a specifier computed from a destination that does not exist.
    """
    facts = await _repo_facts(conn)
    out: dict[str, str] = {}
    for repo_id, fact in facts.items():
        with suppress(ReservedDestError, ValueError):
            dest = _dest_for(repo_id, fact, overrides)
            coordinate = fact.published or Coordinate(ecosystem=fact.ecosystem, name=repo_id)
            out[repo_id] = ecosystems.for_ecosystem(fact.ecosystem).import_specifier(
                coordinate, dest
            )
    return out


async def _dest_paths(
    conn: aiosqlite.Connection, overrides: Mapping[Ecosystem, str]
) -> dict[str, str | None]:
    """§3.3's `layout(node)` for every repo — resolved HERE so Phase 2 relocates a tree into the
    same directory Phase 3 merges its history into.

    Phase 2 used to read `repos.dest_path` raw and abandon a repo whose column was NULL. It is
    resolved through the same `_dest_for` Phase 3 uses instead, because the two phases writing
    the same repo to two different destinations is not a layout disagreement a later stage could
    notice — Phase 2 commits the renames and Phase 3 rewrites history to match them, and the
    first stage to see the mismatch is a `bazel build` over a package that does not exist.

    `None` only where `layout()` REFUSES a destination (the reserved `_scc/` namespace, or a
    `dest:` that escapes the monorepo root); the caller reports that per repo.
    """
    facts = await _repo_facts(conn)
    out: dict[str, str | None] = {}
    for repo_id, fact in facts.items():
        try:
            out[repo_id] = _dest_for(repo_id, fact, overrides)
        except (ReservedDestError, ValueError):
            out[repo_id] = None
    return out


async def _ordering_pairs(
    conn: aiosqlite.Connection, settings: FleetSettings, run_id: str
) -> tuple[tuple[str, str], ...]:
    """`(dependency, dependent)` over the ORDERING subgraph — §3.5's `blocked_by` closure.

    `edges` is stored dependent → dependency (§3.1 step 5), so the pair is the row reversed. Only
    the configured `graph.dag_edge_kinds` above `graph.min_confidence` order anything, which is
    the same filter `build_graph` applies — a repo abandoned here must block exactly the repos
    the sequencer put after it, no more.
    """
    kinds = tuple(str(kind) for kind in settings.config.graph.dag_edge_kinds)
    placeholders = ",".join("?" for _ in kinds)
    sql = (
        # `placeholders` is a run of `?`, one per configured edge kind — no value is
        # interpolated, so this is parameterised in the only sense that matters.
        "SELECT dst_id, src_id FROM edges "  # noqa: S608
        " WHERE run_id = ? AND dst_id IS NOT NULL AND ordering_suppressed = 0 "
        f"   AND confidence >= ? AND kind IN ({placeholders})"
    )
    rows = await _rows(conn, sql, (run_id, settings.config.graph.min_confidence, *kinds))
    return tuple((str(row[0]), str(row[1])) for row in rows)


def _transform_payloads(
    settings: FleetSettings,
    plans: Mapping[str, _TransformPlan],
    rules: Sequence[RewriteRule],
) -> PayloadFactory[TransformInput]:
    """One repo's dispatch payload, built from its plan. Injected (Guardrail 3)."""
    engines = dict(settings.config.transform.engines)
    max_passes = settings.config.transform.max_passes
    max_patch_bytes = settings.config.transform.max_patch_bytes

    async def build(
        *, repo_id: str, phase: Phase, attempt: int, remaining_units: Sequence[str] | None
    ) -> TransformInput:
        _ = (phase, attempt)
        plan = plans[repo_id]
        return TransformInput(
            repo_id=repo_id,
            branch=plan.branch,
            phase_pre_commit_sha=plan.pre_commit_sha,
            dest_path=plan.dest_path,
            sources=plan.sources,
            targets=plan.targets,
            rules=tuple(rules),
            engines=engines,
            # §3.2 step 5's relocation-map substitutions. `{{dest_path}}`/`{{repo_id}}` are
            # where this repo's tree LANDS and what it is called; `{{import_specifier}}` is what
            # other code WRITES to import it, which is the fact §3.2 step 2 is actually about and
            # the one that was missing. A rule set that has only the first two can express a
            # destination as a path — and a path plus a target name is a Bazel label, which is
            # how `import … from '//ts/acme/lib:lib'` came to be written into a source file that
            # no TypeScript resolver could then read. The specifier comes from the §3.3 adapter,
            # so what a cross-repo import may become stays per-language (§13 row 6). An unknown
            # placeholder is still a `KeyError` at render time, never a literal `{{new_pkg}}`
            # compiled into a source file.
            params={
                "dest_path": plan.dest_path,
                "repo_id": repo_id,
                "import_specifier": plan.import_specifier,
            },
            max_passes=max_passes,
            max_patch_bytes=max_patch_bytes,
            remaining_units=None if remaining_units is None else tuple(remaining_units),
        )

    return build


async def _run_transform_wave(
    settings: FleetSettings,
    *,
    repository: SqliteStateRepository,
    writer: StateWriter,
    read_conn: aiosqlite.Connection,
    db_path: Path,
    run_id: str,
    wave_index: int,
    plans: Mapping[str, _TransformPlan],
    rules: Sequence[RewriteRule],
    only: Sequence[str] | None,
    pool: ProcessPoolExecutor,
    evidence: _TransformEvidence,
) -> WaveReport:
    """Compose the run and drive ONE transform wave. The composition root, and nothing else."""
    ledger = CostLedger(
        repository,
        run_id=run_id,
        ceilings=Ceilings.from_settings(settings.config.budgets, settings.config.stubs),
        clock=_now,
    )
    projector = Projector(db_path, run_id=UUID(run_id), path=DEFAULT_PROJECTION_PATH)
    ctx = RunContext(
        run_id=UUID(run_id),
        config=settings.config,
        writer=writer,
        repository=repository,
        read_conn=read_conn,
        ledger=ledger,
        limits=Limits.create(settings.config.concurrency, ledger=ledger, cpu_pool=pool),
        llm=llm_router(settings),
        log=default_logger("fleet.transform"),
        work_dir=(settings.root / settings.config.run.work_dir).resolve(),
        projector=projector,
        clock=_now,
        harness_version=HARNESS_VERSION,
    )
    scheduler = WaveScheduler(
        run_id=run_id,
        phase=Phase.TRANSFORM,
        store=_ScopedWaveStore(
            SqliteSchedulerStore(writer=writer, read_conn=read_conn), only
        ),
        db=repository,
        budgets=settings.config.budgets,
        clock=_now,
        descendants=ordering_descendants(
            await _ordering_pairs(read_conn, settings, run_id)
        ),
    )
    runner = PhaseRunner(
        ctx,
        TransformPipelineWorker(),
        scheduler,
        payloads=_transform_payloads(settings, plans, rules),
        sink=_TransformSink(
            writer=writer,
            repository=repository,
            read_conn=read_conn,
            run_id=run_id,
            evidence=evidence,
        ),
    )
    try:
        await projector.start()
        return await runner.run_wave(wave_index)
    finally:
        await _close_wave_projector(projector, ctx)


async def _transform_criterion(
    settings: FleetSettings,
    *,
    plans: Mapping[str, _TransformPlan],
    evidence: _TransformEvidence,
    statuses: Mapping[str, RepoStatus],
    rules: Sequence[RewriteRule],
) -> tuple[list[str], list[str]]:
    """§3.2's **success criterion**, checked against git for every repo the run calls SUCCEEDED.

    Returns `(violations, unprobed)`. The criterion has four clauses and this evaluates all four:
    a non-empty `git diff` against the pre-transform tree, every changed path under `<dest>/`, an
    empty `TransformResult.unresolved_files`, and a parse probe of every rewritten file.

    **The parse probe's fourth clause has two distinct failure shapes, and ADR-0067 (D37) keeps
    them apart.** A genuinely absent engine — no rewrite engine ADR-0006 names is installed on
    this host — raises `EngineUnavailableError` from `ensure_available()` and is reported as its
    own non-blocking list rather than counted as a pass: "the probe did not run" and "the probe
    returned 0" are different facts, and quietly collapsing them would let a corrupt rewrite ship
    as a verified one. A probe that RAN but produced no verdict — killed at its deadline, never
    started, or an exit code that is neither 0 nor 1 — raises `ProbeIndeterminateError` instead,
    which is a **violation**: an indeterminate probe on file 1 must not excuse files 2–40, so
    every rewritten file is still probed (`continue`, never `break`) and a host with no engine at
    all is deduped to one `parse_probe_unavailable` line per `(repo_id, engine)`.

    **A third shape, since D49 (`9a7148c`): a rewritten path that no rule claims at all.**
    `output.rewritten` no longer holds only rule-matched unit names — it holds every landed
    `FilePatch.path`, including a repair's legitimate collateral edits to sibling files
    (`llm/schemas.py` permits up to 64 paths per repair). `rule_matches_path` returns `False` for
    such a path (unknown suffix, a recognised language no rule targets, or a matching rule whose
    `applies_to` glob misses), so there is no `rule.engine` to route the probe through. That is
    rule-absence, not engine-absence: `EngineRegistry` is keyed by `rule.engine` strings from
    config, not by language, so a loaded engine that could technically parse this file is never
    tried. It is non-blocking for the same reason as the engine-unavailable arm — no configured
    way to check this file — but it must not silently vanish either: it is reported into
    `unprobed`, one line per unmatched path (unlike the engine-unavailable dedupe, each such path
    is its own distinct fact, not a repeat of the same host-wide cause).
    """
    violations: list[str] = []
    unprobed: list[str] = []
    unavailable_seen: set[tuple[str, str]] = set()
    registry = EngineRegistry.from_modules(dict(settings.config.transform.engines))
    for repo_id in sorted(plans):
        if statuses.get(repo_id) is not RepoStatus.SUCCEEDED:
            continue
        plan = plans[repo_id]
        changed = await _changed_entries(
            plan.worktree, f"{plan.pre_commit_sha}..{plan.branch}"
        )
        if not changed:
            violations.append(
                f"{repo_id}: `git diff {plan.pre_commit_sha[:12]}..{plan.branch}` is EMPTY — the "
                "phase reports SUCCEEDED having changed nothing"
            )
        moved = set(plan.sources)
        outside: list[str] = []
        for status, pre, post in changed:
            if status.startswith("D"):
                # The relocation's own pre-image. Any OTHER deletion is a write outside the
                # subtree by another name, and §3.2 step 6.6 forbids it.
                if pre not in moved:
                    outside.append(pre)
            elif not post.startswith(f"{plan.dest_path}/"):
                outside.append(post)
        if outside:
            violations.append(
                f"{repo_id}: {len(outside)} path(s) written or deleted outside "
                f"{plan.dest_path}/: {sorted(outside)[:3]}"
            )
        output = evidence.by_repo.get(repo_id)
        if output is not None and output.unresolved:
            violations.append(f"{repo_id}: unresolved files remain: {output.unresolved[:3]}")
        rewritten = () if output is None else tuple(dict.fromkeys(output.rewritten))
        for unit in rewritten:
            rule = next((r for r in rules if rule_matches_path(r, unit)), None)
            if rule is None:
                # No configured rule claims this landed path (D49 collateral, an unknown suffix,
                # or a recognised language/glob no rule covers) — rule-absence, not engine
                # absence. Non-blocking for the same reason as a genuinely unavailable engine (no
                # configured way to check this file), but the file must still reach the operator
                # rather than disappear: it goes into `unprobed`.
                unprobed.append(
                    f"{repo_id}: {unit} landed but no transform rule claims it — the §3.2 "
                    "parse probe did not run"
                )
                continue
            try:
                probed = await registry.require(rule.engine).parse_probe(
                    str(plan.worktree / unit)
                )
            except ProbeIndeterminateError as exc:
                # Ran, no verdict: blocking. One slow or corrupt file must not excuse the other
                # thirty-nine, so this is `continue`, never `break` (ADR-0067).
                violations.append(
                    f"{repo_id}: the §3.2 parse probe for {unit} produced no verdict: {exc}"
                )
                continue
            except EngineUnavailableError as exc:
                # Genuinely absent engine: non-blocking, but still `continue` (for symmetry with
                # the arm above, and so the rest of this repo's files are still checked against
                # any OTHER clause), deduped to one line per (repo_id, engine) rather than one
                # per rewritten file.
                key = (repo_id, rule.engine)
                if key not in unavailable_seen:
                    unavailable_seen.add(key)
                    unprobed.append(f"{repo_id}: {exc}")
                continue
            if not probed:
                violations.append(f"{repo_id}: the parse probe failed for {unit} (§3.2 step 4)")
    return violations, unprobed


async def _transform_impl(
    opts: GlobalOptions,
    settings: FleetSettings,
    path: Path,
    *,
    run_id: str,
    wave: int | None,
    only: str | None,
    ladder: int,
    dry_run: bool,
) -> dict[str, object]:
    """Phase 2 over one fleet, wave by wave, through the real composition.

    Waves are driven in ascending order and the loop stops at the first halt: `WaveScheduler`
    refuses to open wave N+1 while wave N is not `CLOSED`, which is the whole ordering guarantee
    the topological sequencer bought (§3.1 step 7).
    """
    log_configure(level=opts.log_level)
    rules = _transform_rules(settings)
    evidence = _TransformEvidence()
    plans: dict[str, _TransformPlan] = {}
    reports: list[WaveReport] = []
    driven: list[int] = []
    pool = new_cpu_pool(settings.config.concurrency.cpu_pool_workers)
    try:
        async with StateWriter(path, owner="fleet-transform") as writer:
            read_conn = await connect_ro(path)
            try:
                repository = SqliteStateRepository(writer=writer, read_conn=read_conn)
                _validate_monorepo_dir_overrides(settings.config.build.monorepo_dir_overrides)
                dest_paths = await _dest_paths(
                    read_conn, settings.config.build.monorepo_dir_overrides
                )
                specifiers = await _import_specifiers(
                    read_conn, settings.config.build.monorepo_dir_overrides
                )
                waves = await _open_transform_waves(read_conn, run_id, wave)
                if dry_run:
                    return await _transform_dry_run(
                        settings,
                        read_conn,
                        run_id=run_id,
                        waves=waves,
                        only=only,
                        rules=rules,
                        dest_paths=dest_paths,
                    )
                await repository.reap_expired_phase_leases(run_id, now=_now())
                await repository.open_budget_ledger(
                    run_id, max_usd=settings.config.budgets.run_max_cost_usd, now=_now()
                )
                for index in waves:
                    members = await _wave_repos(read_conn, run_id, index, only)
                    if not members:
                        continue
                    driven.append(index)
                    for repo_id in members:
                        await repository.upsert_phase(
                            run_id,
                            repo_id,
                            Phase.TRANSFORM,
                            now=_now(),
                            max_attempts=ladder,
                        )
                    # D84: a wave whose wall clock is already spent admits nothing, so every
                    # branch, anchor and worktree `_prepare_repo` cuts below is git mutation for
                    # work that cannot run. Skipping it changes no verdict: control falls through
                    # to `_run_transform_wave`, which produces the same exit-4 report — `admit`
                    # returns `admitted=()`, the members stay PENDING and consume no attempt.
                    if not await _wave_is_breached(
                        settings,
                        writer=writer,
                        read_conn=read_conn,
                        repository=repository,
                        run_id=run_id,
                        wave_index=index,
                        phase=Phase.TRANSFORM,
                        only=members if only is not None else None,
                    ):
                        for repo_id in members:
                            if repo_id in plans:
                                continue
                            try:
                                plans[repo_id] = await _prepare_repo(
                                    settings,
                                    writer=writer,
                                    run_id=run_id,
                                    repo_id=repo_id,
                                    dest_path=dest_paths.get(repo_id),
                                    import_specifier=specifiers.get(repo_id, repo_id),
                                    rules=rules,
                                    now=_now(),
                                )
                            except (TransformStepUnavailableError, GitError, OSError) as exc:
                                await _abandon_repo(
                                    writer, run_id, repo_id, detail=str(exc), now=_now()
                                )
                    try:
                        report = await _run_transform_wave(
                            settings,
                            db_path=path,
                            repository=repository,
                            writer=writer,
                            read_conn=read_conn,
                            run_id=run_id,
                            wave_index=index,
                            plans=plans,
                            rules=rules,
                            only=members if only is not None else None,
                            pool=pool,
                            evidence=evidence,
                        )
                    except WaveNotReadyError as exc:
                        raise UsageError(str(exc)) from exc
                    reports.append(report)
                    if report.exit_code is not None:
                        break
                statuses = await _transform_statuses(read_conn, run_id)
                violations, unprobed = await _transform_criterion(
                    settings,
                    plans=plans,
                    evidence=evidence,
                    statuses=statuses,
                    rules=rules,
                )
            finally:
                await read_conn.close()
    finally:
        pool.shutdown(wait=True, cancel_futures=True)

    with suppress(Exception):  # a projection is an OUTPUT (§11.5); it never fails a transform
        await project_once(path, run_id=UUID(run_id), path=DEFAULT_PROJECTION_PATH)

    if violations:
        raise TransformCriterionError(
            "§3.2's success criterion does not hold for "
            f"{len(violations)} repo(s) whose phase says SUCCEEDED: {'; '.join(violations)}"
        )
    attention = sorted(
        repo
        for repo, status in statuses.items()
        if status is RepoStatus.REQUIRES_HUMAN_INTERVENTION
    )
    halt = next((r.exit_code for r in reports if r.exit_code is not None), None)
    exit_code = (
        halt
        if halt is not None
        else (ExitCode.REQUIRES_HUMAN_INTERVENTION if attention else ExitCode.SUCCESS)
    )
    return {
        "run_id": run_id,
        "waves": driven,
        "repos": len(statuses),
        "succeeded": sum(1 for s in statuses.values() if s is RepoStatus.SUCCEEDED),
        "failed": len(attention),
        "attention": attention,
        "commits": sum(len(output.commits) for output in evidence.by_repo.values()),
        "parse_probe_unavailable": unprobed,
        "halt": next((str(r.halt) for r in reports if r.halt is not None), None),
        "exit_code": int(exit_code),
    }


async def _transform_dry_run(
    settings: FleetSettings,
    conn: aiosqlite.Connection,
    *,
    run_id: str,
    waves: Sequence[int],
    only: str | None,
    rules: Sequence[RewriteRule],
    dest_paths: Mapping[str, str | None],
) -> dict[str, object]:
    """§3.2 step 1: "The plan is written and diffed **before** it is executed; `--dry-run` stops
    here." Nothing is written — not a ref, not a branch, not a row — so the plan is computed
    against `HEAD` rather than against an anchor this invocation is not allowed to create.
    """
    work_dir = (settings.root / settings.config.run.work_dir).resolve()
    plan: dict[str, object] = {}
    for index in waves:
        for repo_id in await _wave_repos(conn, run_id, index, only):
            worktree = work_dir / repo_id
            dest = dest_paths.get(repo_id)
            if dest is None or not await asyncio.to_thread((worktree / ".git").exists):
                plan[repo_id] = {"wave": index, "plan": None, "reason": "not preparable"}
                continue
            sources = await _tracked_at(worktree, "HEAD")
            targets = _rewrite_targets(dest.rstrip("/"), sources, rules)
            plan[repo_id] = {
                "wave": index,
                "dest_path": dest.rstrip("/"),
                "moves": {source: relocated_path(dest, source) for source in sources},
                "rewrites": list(targets),
            }
    return {
        "run_id": run_id,
        "dry_run": True,
        "waves": list(waves),
        "repos": len(plan),
        "succeeded": 0,
        "failed": 0,
        "attention": [],
        "commits": 0,
        "plan": plan,
        "parse_probe_unavailable": [],
        "halt": None,
        "exit_code": int(ExitCode.SUCCESS),
    }


async def _transform_statuses(
    conn: aiosqlite.Connection, run_id: str
) -> dict[str, RepoStatus]:
    return await _phase_statuses(conn, run_id, Phase.TRANSFORM)


async def _phase_statuses(
    conn: aiosqlite.Connection, run_id: str, phase: Phase
) -> dict[str, RepoStatus]:
    rows = await _rows(
        conn,
        "SELECT repo_id, status FROM phases WHERE run_id = ? AND phase = ? ORDER BY repo_id",
        (run_id, int(phase)),
    )
    return {str(row[0]): RepoStatus(str(row[1])) for row in rows}


# --------------------------------------------------------------------------------------
# fleet build / fleet verify — Phases 3 and 4, composed from the REGISTERED workers
# --------------------------------------------------------------------------------------
#
# THE HOST GAP, STATED ONCE AND NEVER AGAIN IMPLIED AWAY. `bazel` and `git-filter-repo` are not
# installed here, and this harness does not install them. Everything up to the moment one of them
# is executed is real and runs for real: the throwaway clone, the argv, the mutex-serialized
# merge with its ADR-0011 trailers, the immutable snapshot ref, the worktree cut from it, the
# rendered `BUILD.bazel`/`MODULE.bazel`, the exit-code handling, the `attempts` rows and every
# state transition. The two binaries themselves reach the host through the two seams below, and
# with neither seam set they are invoked for real and fail LOUDLY — `FilterRepoUnavailableError`
# and exit 127 classified `BUILD_ERROR`, non-retryable — rather than being skipped.

BAZEL_RUNNER: CommandRunner | None = None
"""The `bazel` subprocess seam for Phases 3 and 4 (CLAUDE.md guardrail 3).

`None` — the default, and the only value a real run uses — lets each worker build its own
`LoggedRunner`, which really executes `bazel` and really keeps the full stream on disk. A test
sets this to a recorder and thereby exercises argv construction, output parsing, failure
classification and every state transition on a host with no Bazel. It is deliberately NOT wired
into `Git`: the git half of Phase 3 is executed for real, and a seam that faked it too would make
the merge, the trailers and the snapshot ref unprovable.
"""

FILTER_REPO_RUNNER: CommandRunner | None = None
"""The `git-filter-repo` seam for §3.3 step 1's history rewrite. Same contract as `BAZEL_RUNNER`:
`None` executes the real binary, and its absence is `FilterRepoUnavailableError` — a named, loud
refusal, never a silent fallback to an un-rewritten history."""

RESOLVER_RUNNER: CommandRunner | None = None
"""The dependency-resolver seam for §3.3 step 2's lockfiles — `uv pip compile`, `pnpm install`.

Same contract as the two above, and it is the seam that keeps the OFFLINE suite offline. A
resolution is a network operation by nature: `uv` talks to PyPI and `pnpm` to the npm registry to
learn what `requests>=2.31` and `^1.3.0` actually resolve to. With this seam set, a test proves
the argv, the scratch inputs, the carry-beats-resolve precedence and the failure classification
with no network at all; with it `None` — production, and the `integration`-marked tests — the real
resolver runs and its output is the lock that is committed onto the integration branch.

Which resolver, which argv and which file it writes are NOT here: they are `EcosystemAdapter.
resolution()`, because they are per-language facts (§7.5, §13 row 6). This module only executes
what an adapter declared, in a scratch directory, and turns a non-zero exit into a loud
`DependencyResolutionError` rather than an empty lock."""

GAZELLE_RUNNER: CommandRunner | None = None
"""The BUILD-file-generator seam for §3.3 step 2's *delegating* adapters (`uses_gazelle`).

Same contract as the three above: `None` — production — really executes `build.gazelle_binary`,
and its absence is a loud `BuildFileGenerationError` rather than a fleet whose Go packages
quietly contain no targets. With this seam set a test proves the whole path — the scratch tree
the driver assembles, the one fleet-wide argv it builds from the adapter's flags and every Go
repo root, the files it captures back, and the planned bytes those become — on a host with no
Gazelle, no Go and no Bazel.

Which flags, and therefore what the generator is allowed to reach for, are NOT here: they are
`GazelleConfig.args`, because they are per-language facts (§7.5, §13 row 6). This module only
executes what an adapter declared, in a scratch directory that is never a build worktree, and
folds what came back into the plan as `SupportFile` content so that the ADR-0054 publish
contract — published bytes are the PLANNED bytes — holds over generator output unchanged."""

GH_RUNNER: CommandRunner | None = None
"""The `gh` seam for §3.4 steps 4–5 — PR emission and PR STATE INGESTION. Same contract again.

It exists for a sharper reason than the other two. `MERGED` is a fact about GitHub, and three
gates consume it: the §3.4 Phase 4 stacking precondition, the §3.5 `blocked_by` release, and the
§3.5.1 T1 stub trigger. Nothing else in the harness can produce it, so a `gh` that could only be
exercised on a host with the binary and a credential would leave the *deadlock* — wave 0's PRs
open, nothing polling, every later wave refused forever — permanently unproven. With this seam
set, `fleet pr --sync` really parses recorded `gh` JSON through `vcs/github.parse_pr_view`, really
writes `PrState` through the single writer, really emits `pr_merged`, and the gate really opens.

`None` in production: `build_forge` then hands the driver its own default runner and the real
binary executes — `gh` for GitHub, `curl` for Gitea — and its absence is a
`ForgeUnavailableError`, loud, never a silent "assume merged"."""

PLAN_BUILD_HOOK: Callable[[str], None] | None = None
"""The §3.3 step 1 PLANNING seam: called with `repo_id` at the top of every `_plan_build`.

`None` — the default, and the only value a real run uses — makes it a no-op, so production never
calls it at all. It is the fifth seam of this block and the only one that is not a
`CommandRunner`, for a stated reason: the thing a test needs to fail here is not a subprocess.
`_plan_build`'s work is `git worktree` plus reads off the resulting tree, and `BAZEL_RUNNER`'s
docstring above says why a git seam is refused — "the git half of Phase 3 is executed for real,
and a seam that faked it too would make the merge, the trailers and the snapshot ref unprovable".
So this is a hook rather than a runner: it fakes NOTHING, it only lets a caller inject a refusal.

**What it exists to make assertable.** `_build_impl` handles a preparation failure in two
deliberately different ways (ADR-0055), and the difference is the whole design: a PASS 1/2/3
failure pops the repo's `dest` out of the run's `domain`, while a **re-plan failure at a later
wave keeps it**, because earlier waves have already published root files naming that `dest` and
its history is on the branch. The second class is only reachable after a wave has published, so
no fixture — no missing worktree, no unreadable tree, no absent binary — can induce it without
also breaking the first pass, where the same repo would be dropped instead and the interesting
branch never taken.

It is deliberately ignorant of which pass is calling it: it receives a `repo_id` and nothing
else. A hook that knew "this is the re-plan" would encode the very distinction the tests exist to
check, and would pass by construction. A test that wants the later wave counts the calls.
"""

GENERATE_UNIT: Final = "generate"
"""§3.3 steps 2–3: `BUILD.bazel` and `MODULE.bazel`, rendered by `buildgen`."""
VERIFY_UNIT: Final = "verify"
"""§3.3 step 4 / §3.4 step 1: `bazel build` then `bazel test`, run by `buildverify`."""
PUBLISH_UNIT: Final = "publish"
"""The generated files, committed and merged onto `integration` under the mutex.

Not decorative: §3.4 step 1 re-tests on a FRESH snapshot of the branch tip, and a tip that never
received Phase 3's generated `BUILD.bazel` is a tip no `bazel build //<dest>/...` can resolve. The
alternative — leaving them uncommitted in a per-attempt worktree — makes Phase 4 verify a tree
that could not build, which no amount of green output would make true.
"""
RDEPS_UNIT: Final = "rdeps"
"""§3.4 steps 2–3: the closure, the verified target set and the report."""

BUILD_UNITS: Final[tuple[str, ...]] = (GENERATE_UNIT, VERIFY_UNIT, PUBLISH_UNIT)
VERIFY_UNITS: Final[tuple[str, ...]] = (VERIFY_UNIT, RDEPS_UNIT)

BUILDGEN_STEP: Final = "buildgen"
BUILDVERIFY_STEP: Final = "buildverify"
RDEPVERIFY_STEP: Final = "rdepverify"

ROOT_PACKAGE_PATH: Final = "BUILD.bazel"
"""The monorepo root's own `BUILD.bazel` — what turns `//:requirements.lock` from a path into a
label. Written by `_module_inputs` from the union of every adapter's root files, never by an
adapter: there is one root package and two adapters declaring it would drop one's exports."""


class BuildStepUnavailableError(FleetCliError):
    """A repo cannot reach Phase 3/4 because a step §3.3 names cannot run for *it*.

    The Phase 2 sibling of `TransformStepUnavailableError`, and the same containment: the verb
    runs, the gap is per repo, so it is recorded against that repo and the fleet continues
    (§11.1). `git-filter-repo` being absent from the host arrives here.
    """

    exit_code = ExitCode.UNEXPECTED_ERROR


class DependencyResolutionError(BuildStepUnavailableError):
    """An adapter's declared resolver did not produce the lockfile it names (Rule 11).

    A subclass of `BuildStepUnavailableError` so the containment is identical — one repo is
    abandoned, the fleet continues — but a distinct type, and a distinct `findings.kind`, because
    the two say different things to an operator: `git-filter-repo` is missing from the *host*,
    while this is "PyPI did not answer" or "`requests>=2.31` and `urllib3<2` cannot both hold".

    It exists at all because the alternative is the defect this whole path was written to close.
    A resolver that fails and falls back to the synthesized floor produces a lockfile that is
    syntactically fine, names no transitive dependency, and is **indistinguishable from a repo
    with no dependencies** — so the build fails much later, inside a ruleset, with a message about
    a package nobody in the fleet declared. Failing here names the resolver, its exit code and its
    stderr, at the repo that needed it.
    """

    exit_code = ExitCode.UNEXPECTED_ERROR


class BuildFileGenerationError(BuildStepUnavailableError):
    """A delegating adapter's BUILD-file generator did not produce the files it owns (Rule 11).

    The `uses_gazelle` sibling of `DependencyResolutionError`, with the identical containment —
    the delegating ecosystem's repos are abandoned, the fleet continues — and a distinct type
    because it says something different to an operator: not "PyPI did not answer" but "the
    generator that writes every Go target in this monorepo is missing, or refused".

    It exists because the silent form is worse than any build failure. `generate_targets()`
    returns `[]` for a delegating adapter *by design*, so a generator that did not run leaves a
    package whose `BUILD.bazel` is a header and some `# gazelle:` comments — a file that parses,
    a package that loads, and a `bazel build //go/...` that is **green over nothing**. Failing
    here names the generator, its exit code and its stderr, at the repos that needed it.
    """

    exit_code = ExitCode.UNEXPECTED_ERROR


class CoordinateRenderError(BuildStepUnavailableError):
    """An adapter refused to render a declared coordinate into its root file (Rule 11).

    The driver's form of `ecosystems.AdapterCoordinateError`: the adapter names the coordinate,
    this names the repos the render was being done for, and the containment is
    `BuildStepUnavailableError`'s — the ecosystem's repos are abandoned, the fleet continues.

    **Why it is caught at all, which is the whole point of this type.** An adapter that refuses a
    coordinate its language's root file cannot express is right to refuse — writing it out
    produces a root file the language's own tooling rejects, often before it reads a single
    dependency. But an adapter exception the §3.3 step 2 driver did not catch propagated out of
    `_fleet_support_files` and out of `build` itself, so ONE malformed coordinate ended the whole
    fleet's run — every other ecosystem's repos included. Rule 11 asks for the opposite shape:
    mark the affected repos `REQUIRES_HUMAN_INTERVENTION` and move to the next item. Loud, and
    local.

    **A distinct type from `DependencyResolutionError`, and a distinct `findings.kind`,** for the
    same reason that one is distinct from `BuildStepUnavailableError`. A resolution failure says a
    package index did not answer or two specs cannot both hold; this says a coordinate in this
    fleet's own inventory cannot be written down at all, and no resolver ever ran. An operator
    reading `DependencyResolutionFailed` would go and check a registry that was never contacted.

    The driver builds the message from the neutral exception's text, never by inspecting the
    adapter's own subclass — naming one would be naming an ecosystem, which §12.6 keeps out of
    every driver in this project.
    """

    exit_code = ExitCode.UNEXPECTED_ERROR


class RootFileConflictError(FleetCliError):
    """Two repos contributed DIFFERENT bytes for one monorepo-root path (Rule 11, ADR-0048).

    The monorepo root holds one file per path, so two candidates for `//:pnpm-lock.yaml` is not a
    tie to break — it is a statement that some root file was computed per repo when it describes
    the fleet. The union used to resolve it with `setdefault` over `sorted(plans)`, which is
    exactly how one JS repo's `left-pad` and another's `ms` became one lock with one of them
    missing while `fleet build` exited 0 and no wave was ever re-admitted.

    Raised rather than reported per repo because it is not a fact about one repo: it names the
    path and every repo that contributed to it, so the answer is visible in the message instead
    of in a diff between two runs. Identical bytes are not a conflict and never reach here.
    """

    exit_code = ExitCode.UNEXPECTED_ERROR

    def __init__(self, path: str, repo_ids: Sequence[str]) -> None:
        self.path = path
        self.repo_ids = tuple(repo_ids)
        super().__init__(
            f"{path!r}: {len(self.repo_ids)} repos contributed different content for one "
            f"monorepo-root file ({', '.join(self.repo_ids)}); the root holds ONE file per path, "
            f"so this is a root file computed per repo that describes the whole fleet — "
            f"ADR-0048's `workspace_files(units)` must compute it once per ecosystem, and "
            f"picking a winner here would silently drop the other repos' dependencies"
        )


class RootFileDomainError(FleetCliError):
    """A dispatched worktree is missing a `dest` the fleet-wide root files were computed over.

    The general form of the defect the per-wave snapshot closes, and the reason it is checked
    rather than assumed. `_fleet_support_files` computes the monorepo-ROOT files over the RUN's
    whole domain — `//:Cargo.toml`'s `members`, `//:pnpm-workspace.yaml`'s importers,
    `//:.bazelignore`'s lines — and `_module_inputs` writes that same union into every dispatch's
    tree. So a root file's domain is a set of destinations, and a worktree that does not contain
    all of them is a tree whose root files describe units it does not have.

    Ecosystem-neutral on purpose. Cargo is the only ecosystem that fails LOUDLY on it today —
    `cargo metadata` refuses the whole workspace on a `members` entry it cannot read, which is
    what made two Rust repos in one wave unbuildable — while `npm_translate_lock` tolerates an
    absent pnpm importer and a requirements file names distributions rather than paths. Those are
    tolerances of two package managers, not properties of this harness, so the invariant is
    stated over `dest`s and checked before dispatch instead of being left to whichever ruleset
    happens to complain.

    Raised for the wave rather than recorded per repo: every repo in it was planned from ONE
    snapshot, so a member missing from that tree is a statement about the cut, not about a repo.
    """

    exit_code = ExitCode.UNEXPECTED_ERROR

    def __init__(self, missing: Mapping[str, Sequence[str]]) -> None:
        self.missing = {repo: tuple(dests) for repo, dests in missing.items()}
        detail = "; ".join(
            f"{repo} is missing {', '.join(dests)}" for repo, dests in sorted(self.missing.items())
        )
        super().__init__(
            f"{len(self.missing)} dispatched worktree(s) do not contain every destination the "
            f"fleet-wide monorepo-root files were computed over: {detail}. The root files name "
            f"units this tree does not have — a `members`/importer entry pointing at a directory "
            f"that is not there — so the build reads a workspace description that is false about "
            f"its own tree. The snapshot every member of a wave is cut from must be taken after "
            f"the wave's LAST ingest."
        )


class RootFileDomainDriftError(FleetCliError):
    """The fleet-wide root files were computed over a set of units that is not the RUN's domain.

    The other half of `RootFileDomainError`, and the half that catches the defect a containment
    check structurally cannot. Containment asks whether a tree holds every `dest` the root files
    name; it is satisfied trivially by root files that name too FEW. This asks the opposite
    question — do the root files describe every unit the run is responsible for? — and it can
    only be asked because the domain is derived from SQLite (`wave_members` ⋈ `phases` ⋈
    `layout()`) rather than from the same `plans` dict the root files were rendered from.

    Raised for the run rather than recorded per repo. A mismatch means the driver's own bookkeeping
    disagrees with the database about which units exist, and every root file rendered from it is
    wrong for every repo at once — there is no per-repo containment to fall back to (Rule 11).
    """

    exit_code = ExitCode.UNEXPECTED_ERROR

    def __init__(self, *, expected: Mapping[str, str], covered: Mapping[str, str]) -> None:
        self.expected = dict(expected)
        self.covered = dict(covered)
        absent = sorted(set(self.expected) - set(self.covered))
        extra = sorted(set(self.covered) - set(self.expected))
        moved = sorted(
            repo
            for repo in set(self.expected) & set(self.covered)
            if self.expected[repo] != self.covered[repo]
        )
        parts = [
            f"{label}: {', '.join(repos)}"
            for label, repos in (
                ("missing from the root files", absent),
                ("not in the run's domain", extra),
                ("planned at a different destination", moved),
            )
            if repos
        ]
        super().__init__(
            "the fleet-wide monorepo-root files were computed over a unit set that is not this "
            f"run's domain ({'; '.join(parts)}). §3.3 step 2's root files are ONE file each for "
            "the whole monorepo, so a set that is short of the domain publishes a "
            "`MODULE.bazel`/`Cargo.toml`/`pnpm-workspace.yaml` that describes a fraction of the "
            "branch, and the branch only ever grows. The domain is fixed once per run, before "
            "the first build, and `--wave`/`--repo` narrow DISPATCH only."
        )


class BuildCriterionError(FleetCliError):
    """§3.3's success criterion does not hold for a repo whose phase says `SUCCEEDED`.

    Exit 6 rather than 1, for the reason `TransformCriterionError` is: the run finished and the
    state is readable, but a claim it makes about a tree is false, and shipping that to Phase 4
    would verify a blast radius around a package that never built.
    """

    exit_code = ExitCode.UNRESOLVED_FINDINGS


class MonorepoUnavailableError(FleetCliError):
    """§3.3's precondition "the monorepo integration branch exists" does not hold.

    Exit 2, not 1: nothing is wrong with the harness — the operator has to create the monorepo at
    `run.monorepo_path` and check out `run.monorepo_branch` before a merge has anywhere to land.
    Refused for the whole verb rather than per repo, because it is one fact about one repository.
    """

    exit_code = ExitCode.USAGE


def _validate_build_flags(*, stub_blocked: bool) -> None:
    """Every `fleet build` flag either does what it says or is refused here (§10)."""
    if stub_blocked:
        raise UsageError(
            "--stub-blocked is not implemented: emitting a generated stub for a blocked "
            "dependency (§3.5.1) has no worker in src/fleet/workers/ and would write no `stubs` "
            "row, so the flag would silently build the repo WITHOUT the stub it promised."
        )


def _validate_verify_flags(*, rdeps: bool, rdeps_limit: int, rdeps_sample_n: int) -> None:
    """§3.4's bounds are numbers, so the flags that set them are checked as numbers."""
    if not rdeps:
        raise UsageError(
            "--no-rdeps names a verification that is not one: §3.4's success criterion IS "
            "`bazel test` over the verified target set, and a run that skipped the rdeps closure "
            "would report a blast radius of zero as proven. Raise --rdeps-limit instead."
        )
    if rdeps_limit < 1:
        raise UsageError(f"--rdeps-limit {rdeps_limit} must be at least 1")
    if rdeps_sample_n < 0:
        raise UsageError(f"--rdeps-sample-n {rdeps_sample_n} must not be negative")


# --------------------------------------------------------------------------------------
# the Phase 3 pipeline worker
# --------------------------------------------------------------------------------------


class BuildInput(WorkerInput):
    """One repo's whole Phase 3 dispatch, downstream of the ingest the driver already did.

    Everything git-shaped is a *fact handed in* rather than something the worker re-derives
    (§7.1): `integration_ref` is the immutable snapshot the merge produced, and `ctx.workdir` is
    a worktree cut from exactly that ref, so the tree this dispatch renders into and the tree the
    build reads are the same named tree by construction.
    """

    repo_id: RepoId
    dest: str = Field(min_length=1)
    unit: BuildUnit
    targets: list[BuildTarget] = Field(default_factory=list)
    gazelle: GazelleConfig | None = Field(
        default=None,
        description="Set iff the unit's adapter delegates emission (`uses_gazelle`); the driver "
        "reads it off the adapter and never decides it here.",
    )
    module_targets: list[BuildTarget] = Field(
        default_factory=list,
        description="The FLEET's targets, deduplicated to one per distinct `load_from`. Feeds "
        "MODULE.bazel's ruleset set only (D6) — a ruleset named solely by a target's `load()` "
        "gets no `bazel_dep` from `workspace_deps` and Bazel answers `unknown repo`.",
    )
    workspace_deps: list[WorkspaceDep] = Field(
        default_factory=list,
        description="The FLEET's external deps, not this repo's: MODULE.bazel is one root file "
        "every dispatch writes, so every dispatch must write the same superset (§3.3 step 3).",
    )
    toolchains: list[ToolchainRequirement] = Field(default_factory=list)
    support_files: list[SupportFile] = Field(
        default_factory=list,
        description="The FLEET's root files plus THIS repo's package files — everything the two "
        "generated files name and no phase created (D10). Already resolved against the source "
        "trees, so the worker writes bytes rather than re-reading a tree it only partly sees.",
    )
    baseline_test_count: int = Field(
        default=0,
        ge=0,
        description="`repos.baseline_test_count`, passed to `buildverify` so §12.11's "
        "green-and-empty failure is distinguishable from a library that never had tests.",
    )
    requirements: list[ExternalRequirement] = Field(default_factory=list)
    ruleset_versions: dict[str, str] = Field(default_factory=dict)
    integration_ref: str = Field(min_length=1, pattern=r"^refs/")
    integration_branch: str = "integration"
    integration_worktree: str = Field(min_length=1)
    lock_dir: str = Field(min_length=1)
    log_dir: str = "artifacts/logs"
    bazel_bin: str = "bazel"
    module_registry: str = BCR_DEFAULT_REGISTRY
    """The module registry the Bazel that will READ this repo's `MODULE.bazel.lock` contacts.

    `build.registry` when the operator set one and `settings.BCR_DEFAULT_REGISTRY` when they did
    not — the same "unset means Bazel's built-in address" reading `query.registry_args`
    implements by emitting no `--registry` at all, spelled out here because a *comparison* cannot
    be made against "nothing". It travels on the payload rather than being read out of settings
    inside the worker for Guardrail 3's reason: a worker takes its facts from its input.
    """
    image: str | None = None
    container_memory: str = "8g"
    container_cpus: str = "4.0"
    container_network: str = "none"
    cache_mounts: list[CacheMount] = Field(
        default_factory=list,
        description="Role-tagged Bazel caches: each is bind-mounted read-write into the "
        "verify container AND named by the matching `--disk_cache=`/`--repository_cache=` "
        "flag, from the one object, so the mount and the flag cannot drift apart.",
    )
    min_free_bytes: int = Field(
        default=0,
        ge=0,
        description="`preflight.min_free_bytes`, carried down to the bazel invocation and the "
        "container start so §11.3's floor is re-checked per dispatch rather than once at startup",
    )
    run_tests: bool = True
    jobs: int | None = None
    remaining_units: tuple[str, ...] | None = None


class BuildOutput(WorkerOutput):
    """What one repo's Phase 3 landed: the files, the bazel steps, and the tree it all names.

    `attempt`/`tier`/`context_policy` travel here for the reason `TransformOutput` documents: the
    `ResultSink` is handed a `WorkerResult` and never a `WorkerContext`, and §6 CHECKs
    `(tier = 'DETERMINISTIC') = (context_policy IS NULL)`, so an escalated rung's `attempts` row
    is literally unwritable without them.
    """

    repo_id: RepoId
    attempt: int = 1
    tier: str = str(TransformTier.DETERMINISTIC)
    context_policy: str | None = None
    dest: str = ""
    integration_ref: str = ""
    build_bazel_path: str = ""
    module_bazel_path: str = ""
    steps: list[StepRecord] = Field(default_factory=list)
    build_ok: bool = False
    test_ok: bool = False
    tests_ran: bool = False
    published_sha: str = ""
    already_published: bool = False
    module_lock_published: bool = False
    """Whether `MODULE.bazel.lock` is on the integration branch carrying THIS build's resolution.

    False is the honest bootstrap answer and not an error: on the first run there is no lockfile
    until a Bazel that could reach a registry has written one, and this dispatch's Bazel may not
    have (a re-entry that owes only the publish unit never ran one at all). It is recorded rather
    than assumed because a monorepo without it cannot resolve a single module in a
    `--network=none` container — see `BuildPipelineWorker._publish`."""

    module_lock_foreign_registry: str = ""
    """Why the published `MODULE.bazel.lock` cannot be shown to work offline — `""` when it can.

    Set from `bazel.lockfile.check_lock_registry`'s own refusal text when the lock this dispatch
    published is keyed by a registry host `module_registry` does not name, or when it does not
    parse at all. It is a RECORD and never a verdict: the publish still happens, for the reasons
    argued at the call site in `BuildPipelineWorker._publish_module_lock`. Carried on the output
    because the sink is handed a `WorkerResult` and nothing else, and a warning that reached only
    the log would leave the run's own state with no memory of it."""

    @property
    def green(self) -> bool:
        """§3.3's success criterion for this node: `bazel build` 0 and `bazel test` 0, and for a
        node with no tests of its own only the build (the CONTRACT case)."""
        return self.build_ok and (self.test_ok or not self.tests_ran)


def _read_text_or_none(path: Path) -> str | None:
    """The file's text, or `None` when it is not there. Never a partial read and never a raise —
    "is this file already what I am about to write?" has *No* as an answer, not an exception."""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _captured_module_lock(worktree: Path) -> SupportFile | None:
    """`MODULE.bazel.lock` as Bazel left it in this build worktree, as PLANNED BYTES — or `None`.

    The same capture shape as ADR-0056's Gazelle output and for the same reason: these are bytes
    a tool produced, so the only honest source for them is the place the tool wrote them, and the
    only honest way to carry them onward is a `SupportFile` that the existing `materialize` writer
    puts back on disk. Nothing is synthesized here — there is no `content` floor and no
    `carry_from` candidate list, because a lockfile the harness invented is not a resolution
    anything performed.

    `None` means Bazel wrote no lock in this worktree, which is the ordinary state of a first run
    and of any dispatch whose build never ran. The decision about what to do with that answer is
    the caller's and is documented at the call site in `BuildPipelineWorker._publish`.
    """
    text = _read_text_or_none(worktree / MODULE_LOCK_PATH)
    return None if text is None else SupportFile(path=MODULE_LOCK_PATH, content=text)


class BuildPipelineWorker(BaseWorker[BuildInput, BuildOutput]):
    """§3.3 steps 2–4 as ONE dispatch of ONE phase, chaining the two registered workers.

    One dispatch and not two, for the reason `TransformPipelineWorker` documents: `phases` holds
    a single row per `(run, repo, phase)` and `acquire_phase_lease` only moves a **PENDING** one,
    so a second `PhaseRunner` pass over `Phase.BUILD` would find the row terminal and admit
    nothing — the build would look as though it had run while it never did.

    Step 1 (ingest) is NOT here. It is the driver's, in `_ingest_build_source`, because its output —
    the immutable `refs/fleet/<run_id>/integration/<seq>` — is what the dispatch's *worktree* is
    cut from, and a worktree cannot be cut from a ref a later step of the same dispatch creates.
    That is the same placement `_prepare_repo` has in Phase 2: everything git requires to be true
    before the first mutation happens before the lease.
    """

    __slots__ = ("_workers",)

    name: ClassVar[str] = "build"
    phase: ClassVar[Phase] = Phase.BUILD
    input_model: ClassVar[type[WorkerInput]] = BuildInput
    output_model: ClassVar[type[WorkerOutput]] = BuildOutput

    def __init__(self, *, bazel_runner: CommandRunner | None = None) -> None:
        self._workers: Mapping[str, BaseWorker[Any, Any]] = {
            BUILDGEN_STEP: _worker(BUILDGEN_STEP),
            BUILDVERIFY_STEP: _worker(BUILDVERIFY_STEP, runner=bazel_runner),
        }

    async def preconditions_hold(self, ctx: WorkerContext, payload: BuildInput) -> bool:
        """Re-entry is admitted only for a checkpoint whose worktree is still on disk.

        The per-step preconditions are finer and are consulted by the steps themselves on the
        re-entry path: `buildverify`'s asks whether `<dest>/BUILD.bazel` exists, which is only
        answerable **after** the generation step of this same dispatch has run.
        """
        if payload.remaining_units is None:
            return False
        return await asyncio.to_thread(Path(ctx.workdir).is_dir)

    async def run(self, ctx: WorkerContext, payload: BuildInput) -> WorkerResult[BuildOutput]:
        units = list(BUILD_UNITS)
        owed = set(units) if payload.remaining_units is None else set(payload.remaining_units)
        output = BuildOutput(
            repo_id=payload.repo_id,
            dest=payload.dest,
            integration_ref=payload.integration_ref,
            attempt=ctx.attempt,
            tier=str(ctx.tier),
            context_policy=None if ctx.context_policy is None else str(ctx.context_policy),
        )
        landed = [unit for unit in units if unit not in owed]

        if GENERATE_UNIT in owed:
            worker = self._workers[BUILDGEN_STEP]
            result = await worker.run(ctx, self._buildgen_input(payload))
            generated: BuildgenOutput | None = result.output
            if generated is not None:
                output.build_bazel_path = generated.build_bazel_path
                output.module_bazel_path = generated.module_bazel_path
            if result.status != "ok":
                return self._handoff(result, units, landed, output)
            landed.append(GENERATE_UNIT)

        if VERIFY_UNIT in owed:
            worker = self._workers[BUILDVERIFY_STEP]
            verify_input = self._buildverify_input(payload)
            if payload.remaining_units is not None and not await worker.preconditions_hold(
                ctx, verify_input
            ):
                # §7.1: the checkpoint does not describe this tree — the generated file the build
                # was to read is gone — so the phase re-runs whole rather than recording a build
                # failure for a `BUILD.bazel` that was never written.
                ctx.log.info("build_checkpoint_rejected", step=BUILDVERIFY_STEP, dest=payload.dest)
                result = await self._workers[BUILDGEN_STEP].run(ctx, self._buildgen_input(payload))
                if result.status != "ok":
                    return self._handoff(result, units, landed, output)
            result = await worker.run(ctx, verify_input)
            verified: BuildverifyOutput | None = result.output
            if verified is not None:
                output.steps.extend(verified.steps)
                output.build_ok = verified.build_ok
                output.test_ok = verified.test_ok
                output.tests_ran = verified.tests_ran
            if result.status != "ok":
                return self._handoff(result, units, landed, output)
            landed.append(VERIFY_UNIT)

        if PUBLISH_UNIT in owed:
            failure = await self._publish(ctx, payload, output)
            if failure is not None:
                return WorkerResult[BuildOutput](
                    status="partial" if landed else "failed",
                    output=output,
                    completed_units=landed,
                    remaining_units=[PUBLISH_UNIT],
                    error=failure,
                    evidence=self._evidence(payload, output),
                )
            landed.append(PUBLISH_UNIT)

        return WorkerResult[BuildOutput](
            status="ok",
            output=output,
            completed_units=list(dict.fromkeys(landed)),
            evidence=self._evidence(payload, output),
        )

    # -- payload plumbing ----------------------------------------------------------------

    def _buildgen_input(self, payload: BuildInput) -> BuildgenInput:
        """§3.3 steps 2–3, with `ingest=None`: the history is already on the branch and the
        snapshot is already cut, which is exactly the case `IngestSpec` documents as absent."""
        return BuildgenInput(
            unit=payload.unit,
            targets=list(payload.targets),
            # D6, actually reached: `render_module_bazel` derives a `bazel_dep` from every ruleset
            # a target's `load_from` reads out of, and this argument is the only path by which any
            # target reaches it in production. The mechanism was written and never wired.
            module_targets=list(payload.module_targets),
            gazelle=payload.gazelle,
            workspace_deps=list(payload.workspace_deps),
            toolchains=list(payload.toolchains),
            support_files=list(payload.support_files),
            requirements=list(payload.requirements),
            ruleset_versions=dict(payload.ruleset_versions),
            write_module_bazel=True,
            ingest=None,
            log_dir=payload.log_dir,
        )

    def _buildverify_input(self, payload: BuildInput) -> BuildverifyInput:
        """`worktree=None` on purpose: it defaults to `ctx.workdir`, which the driver cut from
        `integration_ref`. Naming the ref twice — once as the tree and once as the path — is how
        the two come to disagree.

        `baseline_test_count` is passed and that is the whole of §12.11's green-and-empty check
        being reachable at all: `buildverify` distinguishes "this library never had tests" (exit
        4, fine) from "this migration DELETED the repo's tests" (exit 4 with a positive baseline,
        a real defect) by comparing against this number, and every repo arrived carrying the
        default 0 — so the second case was indistinguishable from the first for the whole fleet.
        """
        return BuildverifyInput(
            dest=payload.dest,
            integration_ref=payload.integration_ref,
            worktree=None,
            baseline_test_count=payload.baseline_test_count,
            log_dir=payload.log_dir,
            bazel_bin=payload.bazel_bin,
            run_tests=payload.run_tests,
            jobs=payload.jobs,
            image=payload.image,
            container_memory=payload.container_memory,
            container_cpus=payload.container_cpus,
            container_network=payload.container_network,
            cache_mounts=list(payload.cache_mounts),
            min_free_bytes=payload.min_free_bytes,
        )

    async def _publish(
        self, ctx: WorkerContext, payload: BuildInput, output: BuildOutput
    ) -> WorkerError | None:
        """Commit the generated files in this attempt's worktree, then merge them onto
        `integration` **under the same mutex the ingest used** (§3.3 step 1).

        Idempotent by git rather than by a flag: on a re-run the worktree is cut from a snapshot
        that already contains the identical generated files, `git add` stages nothing, and
        `already_published` records that the guard hit instead of minting an empty commit.

        **What is committed is the PLANNED bytes, re-asserted here, never what is on disk.**
        Between GENERATE and here a build system has run with `cwd=` this worktree, and some of
        them write to it: `crate_universe`'s `crate.from_cargo` rewrites `//:Cargo.lock` in place
        (`skip_cargo_lockfile_overwrite` defaults False). Staging the tree as found published one
        repo's cargo-extended lock while every sibling published the materialized plan, and the
        next merge was an add/add conflict on `Cargo.lock` — §11.6 byte-determinism broken not by
        a nondeterministic generator but by the build editing the tree underneath the harness. So
        this is a publish-contract rule and not a Rust workaround: no ecosystem's build output is
        allowed to become a commit, whether or not that ecosystem writes back today.
        """
        git = Git(ctx.workdir, deadline=ctx.deadline)
        paths = [f"{payload.dest}/BUILD.bazel"]
        # **The one literal above is the only hard-coded path, and it is deliberately no longer
        # the only staged one.** A package's generated content used to be exactly one file at
        # `<dest>/BUILD.bazel`; a delegating adapter's generator writes packages of its own —
        # `<dest>/cmd/<name>/BUILD.bazel`, `<dest>/internal/<pkg>/BUILD.bazel` — at arbitrary
        # depth. Every one of those arrives as a declared `SupportFile` (`_run_gazelle` captures
        # it into the plan), so the `materialize` call below both writes it and RETURNS its path,
        # and the pathspec widens to exactly the set of planned paths without a second mechanism.
        # Nothing is committed here that the plan did not declare.
        #
        # Widening by naming those paths rather than by staging `<dest>` wholesale is the point:
        # `git add -- <dest>` would sweep in whatever the build system left under the package,
        # which is precisely what ADR-0054 exists to keep out of a commit. The set staged is
        # therefore still "the planned files", just no longer assumed to be one file at one depth.
        #
        # The idempotence check below is scoped to `paths` (D26): it asks `git diff --cached`
        # restricted to exactly what `add` just staged, never `git status --porcelain` over the
        # whole worktree. A generated sub-package at any depth is still IN `paths`, so it is still
        # seen when `add` actually stages it. What changes is everything OUTSIDE `paths` — a
        # `bazel-*-events.json` real Bazel writes relative to `cwd`, a convenience symlink,
        # `MODULE.bazel.lock` (deliberately excluded from `paths` and published separately below)
        # — none of which can any longer make a re-entry that staged nothing look like one that
        # staged something.
        #
        # D10: the lockfiles and configs MODULE.bazel names travel in the SAME commit. A
        # generated module that reaches `integration` without them is the identical defect one
        # merge later — the next repo's worktree is cut from a tip whose MODULE.bazel points
        # at `//:requirements.lock` and whose tree has no such file.
        #
        # `materialize` is `buildgen`'s own loop — the one whose docstring explains why the
        # bytes come from `SupportFile.content` and never from a re-read of the tree — so the
        # committed bytes equal the planned bytes BY CONSTRUCTION rather than by the tree
        # having been left alone. It replaces the old `_existing_paths` filter outright: every
        # declared path now exists because this call just wrote it, which also keeps `git add`
        # off a pathspec that matches nothing on a re-entry that skipped the generation unit.
        # It is called unconditionally, and no longer only when this dispatch rendered a
        # MODULE.bazel: a re-entry that owes only the publish unit owes every planned byte just
        # the same, and the old gate left exactly that path staging a pathspec it had not written.
        #
        # Idempotence is unchanged, and re-asserting before the check is what makes it exact.
        # The pathspec-scoped `git diff --cached -- paths` answers "does the INDEX, restricted to
        # exactly these paths, differ from the snapshot it was cut from?". Re-writing planned
        # bytes can only move a path from differing to identical — GENERATE wrote these same bytes
        # into this same worktree minutes ago — so the guard can never mint a commit it would not
        # have minted before. What changes from the whole-worktree `is_dirty()` this replaced
        # (D26) is that unrelated dirt outside `paths` can no longer make a re-entry that staged
        # nothing look like one that staged something, which is what used to send `git commit` at
        # an empty index and fail the whole dispatch on every subsequent re-entry. A tip that
        # genuinely disagrees with the plan (D10's fleet-wide superset grew since this snapshot)
        # still stages a real diff for `paths` and still publishes — which is the case the guard
        # exists for.
        #
        # The cost, stated: cargo's extension of the lock is discarded on every build, so each
        # Phase 3 and Phase 4 build re-extends from the seeded lock rather than from the
        # previous build's output. That is exactly the arrangement `rust.py` already relies on
        # — `crate_universe` runs `cargo fetch` WITHOUT `--locked`, repairing and extending a
        # partial lock every time — and this harness declares no Cargo resolution of its own,
        # so there is no resolved artifact being thrown away here, only work being redone.
        paths.extend(await asyncio.to_thread(materialize, Path(ctx.workdir), payload.support_files))
        if output.module_bazel_path:
            paths.append("MODULE.bazel")
        # `MODULE.bazel.lock` is captured HERE and from THIS worktree, for the same reason
        # ADR-0056 captures Gazelle's BUILD files from the scratch tree: it is bytes a *tool*
        # produced, and the only way to have them is to look where the tool wrote them. Bazel
        # writes it beside `MODULE.bazel` during the VERIFY unit that just ran with `cwd=` this
        # worktree, so the capture is one read and the rest of the path is the existing one —
        # a `SupportFile`, `materialize`, `git add`.
        lock = await asyncio.to_thread(_captured_module_lock, Path(ctx.workdir))
        if lock is None:
            # **The bootstrap, stated where it is decided: publish NOTHING, loudly.** On a first
            # run there is no lockfile until a Bazel that could reach a registry has written one,
            # and that Bazel is the one this dispatch just ran — so "absent" is the normal state
            # of the very first build and not a defect to fail on. The two alternatives are both
            # worse. Failing the publish would cost this repo the `BUILD.bazel`/`MODULE.bazel` it
            # legitimately generated, over an artifact the *next* build produces. Publishing an
            # empty or synthesized lock is worse still: it is the `Resolution` docstring's "an
            # empty lock is indistinguishable from no dependencies" defect with a bigger blast
            # radius — Bazel would either overwrite it (so it bought nothing) or, under
            # `--lockfile_mode=error`, refuse a resolution nobody computed, and either way the
            # tree LOOKS offline-ready while a `--network=none` container still exits 32 at
            # `Error computing the main repository mapping`. So the absence is recorded on the
            # output and logged at WARNING, and the tree ships without it — visibly.
            ctx.log.warning(
                "module_lock_absent",
                dest=payload.dest,
                path=MODULE_LOCK_PATH,
                detail=(
                    "no lockfile in the build worktree, so the published tree carries none; a "
                    "--network=none container build of it fails at `Error computing the main "
                    "repository mapping` with exit 32 until a build that can reach the registry "
                    "writes one"
                ),
            )
        try:
            await git.exec(["add", "--", *paths])
            # D26: scoped to `paths` — exactly what was just staged — never `is_dirty()`'s
            # whole-worktree `git status --porcelain`, which also sees droppings `add` was never
            # asked to stage (a relative `bazel-*-events.json`, a convenience symlink,
            # `MODULE.bazel.lock`) and would read a re-entry that staged nothing as "dirty",
            # sending `git commit` at an empty index and failing the whole dispatch.
            staged = await git.diff_stat(staged=True, paths=paths)
            published = bool(staged.files)
            output.already_published = not published
            sha = ""
            if published:
                sha = await git.commit(
                    f"Generate Bazel targets for {payload.dest}",
                    # Deliberately NOT `Source-Repo:`/`Source-Sha:`. Those two are ADR-0011's
                    # ingest provenance and `already_ingested()` reads them with git's own trailer
                    # parser: a generated-file commit wearing them would answer "this history is
                    # already merged" for a merge that never happened.
                    trailers={
                        "Fleet-Repo-Id": str(ctx.repo_id),
                        "Fleet-Run-Id": str(ctx.run_id),
                        "Fleet-Integration-Ref": payload.integration_ref,
                    },
                )
                output.published_sha = sha
            if not published and lock is None:
                return None
            mutex = IntegrationMutex(
                payload.lock_dir, ctx.run_id, timeout_s=max(ctx.time_left(loop_now()), 1.0)
            )
            async with mutex:
                integration = Git(payload.integration_worktree, deadline=ctx.deadline)
                if published:
                    await integration.exec(
                        [
                            "merge",
                            "--no-ff",
                            "--no-verify",
                            "-m",
                            f"Merge generated Bazel targets for {payload.dest}",
                            sha,
                        ],
                        with_identity=True,
                    )
                if lock is not None:
                    await self._publish_module_lock(ctx, payload, output, integration, lock)
        except (GitError, OSError) as exc:
            return WorkerError(
                failure_class=FailureClass.TRANSIENT_INFRA,
                retryable=True,
                stderr_tail=str(exc),
                exception_type=f"{type(exc).__module__}.{type(exc).__qualname__}",
            )
        return None

    async def _publish_module_lock(
        self,
        ctx: WorkerContext,
        payload: BuildInput,
        output: BuildOutput,
        integration: Git,
        lock: SupportFile,
    ) -> None:
        """Put this build's `MODULE.bazel.lock` on `integration`, under the mutex already held.

        **Why it is committed HERE and not staged into the dispatch commit above.** Every other
        fleet-wide root file is byte-identical across the dispatches of a wave by construction —
        `_fleet_support_files` computes one tuple per ecosystem and hands it to every plan, and
        `MODULE.bazel` is rendered from one fleet-wide input set — so two repos of one wave add
        the same path with the same bytes and git merges them without a conflict. A lockfile is
        NOT that: every dispatch's worktree is cut from the run's single snapshot, and Bazel
        records in the lock the module extensions its own build actually evaluated, so the JS
        repo's lock and the Python repo's lock of one wave differ in `moduleExtensions` while
        agreeing everywhere else. Staged into the dispatch commit, those are two branches adding
        one path with different content off a common base — `CONFLICT (add/add)` on the second
        merge, which leaves the integration worktree conflicted and takes the rest of the run
        down with it. Committed here, inside the `IntegrationMutex` that already serializes every
        writer to this branch, the same bytes reach the same branch with linear history and no
        merge to conflict.

        The bytes are still the PLANNED bytes and still land through `materialize` — the one
        writer that turns a `SupportFile` into a file — so this is ADR-0054's rule (what is
        committed is what was planned, never what a build system left lying around) applied to an
        artifact whose plan could only be made after the build ran.

        **The cost, stated.** Last writer wins: within a wave the lock on the branch is the one
        the repo that merged last wrote, so another repo's `moduleExtensions` entries are
        replaced rather than unioned. What survives every writer is `registryFileHashes`, which
        is a function of `MODULE.bazel` alone — MVS resolves the whole module graph whatever
        targets were asked for — and that is the map whose absence produces exit 32 before
        analysis. Whether the surviving extension entries are also sufficient for an offline
        build is NOT established here and is not claimed: no container build has been run against
        a published tree.

        Idempotent by blob SHA against what is actually ON `integration_branch` (D27), never
        against the worktree file alone: a re-run whose Bazel produced the identical lock writes
        nothing and mints no commit, but "identical" is answered by `Git.blob_at` reading the
        BRANCH, because a dispatch that dies between `materialize` and `commit` used to leave
        correct bytes on disk, untracked or staged but never committed, and every later read of
        that same file compared equal to itself and returned without ever committing — so the
        branch shipped with no lock while `module_lock_published` said otherwise. Comparing
        `Git.hash_object` of the just-materialized file against `Git.blob_at(integration_branch,
        …)` means an uncommitted materialize is never mistaken for a publish: the two SHAs can
        only agree once a commit has actually landed. Neither call reads the file's content back
        into Python — both are small, fixed-size answers, which also keeps this comparison correct
        for a lock file whose bytes exceed the 32 KiB tail-truncation `util.proc.run` applies to
        every capture (SPEC §11.3). The comparison is still against the branch's committed tree
        rather than the index, so — same as before — it is unaffected by whatever else a merge
        just staged.

        **The registry check, and why it WARNS instead of refusing.** `check_lock_registry` is the
        comparison this module's docstring exists for: a lock keyed by a mirror is, to a Bazel
        contacting `bcr.bazel.build`, worth exactly as much as no lock at all — the same exit 32
        at `Error computing the main repository mapping` — except that the tree LOOKS
        offline-ready. Until now it was called from tests only, so nothing in production ever
        asked the question about bytes that actually reached the branch. It is asked here, before
        the idempotence return, because the bytes on the branch are foreign whether this dispatch
        wrote them or a sibling of the same wave did.

        Three outcomes were available and the middle one is taken:

        * *Refuse the publish.* Rejected, and the decisive argument is that **this check can be
          wrong**. `module_registry` is derived from `build.registry`, but that is not the only
          way the reading Bazel's address is set: a `common --registry=` line in the monorepo's
          own committed `.bazelrc` overrides it, and the harness does not parse `.bazelrc` (doing
          so means implementing Bazel's `import`/`try-import`/`config` precedence, which is a
          second mechanism, not a check). On a host whose BCR address is blackholed — the case
          `BCR_MIRROR_REGISTRY` exists for, and the case this repository's own test fixtures hit
          — a mirror-keyed lock is the CORRECT lock, and refusing it would withhold the artifact
          from the very builds it was written to make offline-capable.
        * *Publish silently.* Rejected outright: that is the defect, not a policy.
        * **Publish, and record it so it cannot pass unnoticed** — a `WARNING` log line beside the
          `module_lock_absent` one it is the sibling of, and `module_lock_foreign_registry` on the
          output, from which `_BuildSink` writes a `findings` row. The lock is still worth having
          even when the warning is right: it is a real resolution, it is what a networked build
          and any re-resolution read, and it is strictly better than the nothing the alternative
          leaves behind. What it is not is *evidence of offline readiness*, and the record is what
          stops it from being read as such.

        `ValueError` is caught rather than `LockfileRegistryMismatchError` alone, and that is not
        breadth for its own sake: `lock_registry_urls` raises `json.JSONDecodeError` — itself a
        `ValueError` — on a lock it cannot parse, and a lock nobody can parse is a lock nobody can
        say is keyed correctly. Both answers are "this cannot be shown to work offline", which is
        the one thing this field means.
        """
        try:
            check_lock_registry(lock.content, registry=payload.module_registry)
        except ValueError as exc:
            output.module_lock_foreign_registry = str(exc)
            ctx.log.warning(
                "module_lock_foreign_registry",
                dest=payload.dest,
                path=MODULE_LOCK_PATH,
                registry=payload.module_registry,
                detail=str(exc),
            )
        root = Path(payload.integration_worktree)
        # D27: `materialize` runs FIRST and unconditionally (it is idempotent, same as the
        # dispatch commit's re-assertion above), so the comparison below reads the same bytes
        # `add`/`commit` would stage rather than a state before this run touched the file.
        await asyncio.to_thread(materialize, root, [lock])
        local_sha = await integration.hash_object(MODULE_LOCK_PATH)
        branch_sha = await integration.blob_at(payload.integration_branch, MODULE_LOCK_PATH)
        output.module_lock_published = True
        if local_sha == branch_sha:
            return
        await integration.exec(["add", "--", MODULE_LOCK_PATH])
        await integration.commit(
            f"Record {MODULE_LOCK_PATH} from {payload.dest}'s build",
            trailers={
                "Fleet-Repo-Id": str(ctx.repo_id),
                "Fleet-Run-Id": str(ctx.run_id),
                "Fleet-Integration-Ref": payload.integration_ref,
            },
        )

    @staticmethod
    def _evidence(payload: BuildInput, output: BuildOutput) -> list[str]:
        paths = [step.log_path for step in output.steps if step.log_path is not None]
        return [payload.integration_ref, *paths]

    def _handoff(
        self,
        step: WorkerResult[Any],
        units: Sequence[str],
        landed: Sequence[str],
        output: BuildOutput,
    ) -> WorkerResult[BuildOutput]:
        """A step's non-`ok` result is the dispatch's, carrying the evidence that landed first.

        The `bazel` steps travel WITH the failure on purpose: the `attempts` rows they become are
        the whole record of what was executed, and a result that hid them would leave a build
        failure with no command, no exit code and no log path for the repair rung to read.
        """
        done = list(dict.fromkeys(landed))
        remaining = [unit for unit in units if unit not in set(done)]
        status = "failed" if step.status == "partial" else step.status
        error = step.error
        if error is None and status in ("failed", "timeout"):
            error = WorkerError(
                failure_class=FailureClass.UNKNOWN,
                retryable=True,
                stderr_tail=f"{self.name}: a step returned {step.status!r} with no error",
            )
        if status == "ok":  # pragma: no cover - `_handoff` is only reached for a non-ok step
            status = "failed"
        return WorkerResult[BuildOutput](
            status=cast("Any", status),
            output=output,
            completed_units=done,
            remaining_units=remaining,
            usage=step.usage,
            error=error,
            evidence=[output.integration_ref, *(s.log_path or "" for s in output.steps)][:1],
        )


# --------------------------------------------------------------------------------------
# the Phase 4 pipeline worker
# --------------------------------------------------------------------------------------


class VerifyInput(WorkerInput):
    """§3.4 steps 1–3 for one repo, against a snapshot cut at THIS task's start."""

    repo_id: RepoId
    dest: str = Field(min_length=1)
    integration_ref: str = Field(min_length=1, pattern=r"^refs/")
    log_dir: str = "artifacts/logs"
    bazel_bin: str = "bazel"
    image: str | None = None
    container_memory: str = "8g"
    container_cpus: str = "4.0"
    container_network: str = "none"
    cache_mounts: list[CacheMount] = Field(
        default_factory=list,
        description="Role-tagged Bazel caches: each is bind-mounted read-write into the "
        "verify container AND named by the matching `--disk_cache=`/`--repository_cache=` "
        "flag, from the one object, so the mount and the flag cannot drift apart.",
    )
    min_free_bytes: int = Field(
        default=0,
        ge=0,
        description="`preflight.min_free_bytes`, carried down to the bazel invocation and the "
        "container start so §11.3's floor is re-checked per dispatch rather than once at startup",
    )
    jobs: int | None = None
    affected_only: bool = True
    rdeps_limit: int = Field(default=DEFAULT_RDEPS_LIMIT, ge=1)
    rdeps_sample_n: int = Field(default=DEFAULT_SAMPLE_N, ge=0)
    rdeps_sample_seed: str | None = None
    remaining_units: tuple[str, ...] | None = None


class VerifyOutput(WorkerOutput):
    """The report, plus the sampling evidence a reviewer needs to audit the reduction."""

    repo_id: RepoId
    attempt: int = 1
    tier: str = str(TransformTier.DETERMINISTIC)
    context_policy: str | None = None
    dest: str = ""
    integration_ref: str = ""
    steps: list[StepRecord] = Field(default_factory=list)
    build_ok: bool = False
    test_ok: bool = False
    report: VerificationReport | None = None
    rdeps_query: str = ""
    rdeps_target_count: int = Field(default=0, ge=0)
    rdeps_tested: int = Field(default=0, ge=0)
    rdeps_truncated: bool = False
    rdeps_sample_seed: str = ""
    rdeps_sample_n: int = Field(default=0, ge=0)
    direct_rdeps: int = Field(default=0, ge=0)
    target_pattern_file: str = ""

    @property
    def equivalence(self) -> Equivalence:
        """Always the report's, so the PR and the report cannot disagree. A dispatch that never
        reached the report at all is not `FULL`: nothing was verified, so nothing is equivalent.
        """
        return Equivalence.FULL if self.report is None else self.report.equivalence


class VerifyPipelineWorker(BaseWorker[VerifyInput, VerifyOutput]):
    """§3.4 step 1 (`buildverify` on the fresh tip) chained to steps 2–3 (`rdepverify`).

    Step 1 is a re-run rather than a re-read of Phase 3's verdict, and §3.4 says why:
    dependencies merged since the Phase 3 snapshot may have moved. The verdict it produces is
    what `RdepverifyInput.build_ok`/`test_ok` carry into the report, so the report's own-tests
    columns describe the tip and not a tree from an earlier wave.
    """

    __slots__ = ("_workers",)

    name: ClassVar[str] = "verify"
    phase: ClassVar[Phase] = Phase.VERIFY
    input_model: ClassVar[type[WorkerInput]] = VerifyInput
    output_model: ClassVar[type[WorkerOutput]] = VerifyOutput

    def __init__(self, *, bazel_runner: CommandRunner | None = None) -> None:
        self._workers: Mapping[str, BaseWorker[Any, Any]] = {
            BUILDVERIFY_STEP: _worker(BUILDVERIFY_STEP, runner=bazel_runner),
            RDEPVERIFY_STEP: _worker(RDEPVERIFY_STEP, runner=bazel_runner),
        }

    async def preconditions_hold(self, ctx: WorkerContext, payload: VerifyInput) -> bool:
        if payload.remaining_units is None:
            return False
        return await asyncio.to_thread(Path(ctx.workdir).is_dir)

    async def run(self, ctx: WorkerContext, payload: VerifyInput) -> WorkerResult[VerifyOutput]:
        units = list(VERIFY_UNITS)
        owed = set(units) if payload.remaining_units is None else set(payload.remaining_units)
        output = VerifyOutput(
            repo_id=payload.repo_id,
            dest=payload.dest,
            integration_ref=payload.integration_ref,
            attempt=ctx.attempt,
            tier=str(ctx.tier),
            context_policy=None if ctx.context_policy is None else str(ctx.context_policy),
        )
        landed = [unit for unit in units if unit not in owed]

        if VERIFY_UNIT in owed:
            result = await self._workers[BUILDVERIFY_STEP].run(ctx, self._own_tests(payload))
            own: BuildverifyOutput | None = result.output
            if own is not None:
                output.steps.extend(own.steps)
                output.build_ok = own.build_ok
                output.test_ok = own.test_ok
            if result.status != "ok":
                return self._handoff(result, units, landed, output)
            landed.append(VERIFY_UNIT)

        result = await self._workers[RDEPVERIFY_STEP].run(ctx, self._rdeps(payload, output))
        closure: RdepverifyOutput | None = result.output
        if closure is not None:
            self._absorb(output, closure)
        if result.status != "ok":
            return self._handoff(result, units, landed, output)
        landed.append(RDEPS_UNIT)
        return WorkerResult[VerifyOutput](
            status="ok",
            output=output,
            completed_units=list(dict.fromkeys(landed)),
            evidence=[payload.integration_ref, output.target_pattern_file],
        )

    # -- payload plumbing ----------------------------------------------------------------

    def _own_tests(self, payload: VerifyInput) -> BuildverifyInput:
        return BuildverifyInput(
            dest=payload.dest,
            integration_ref=payload.integration_ref,
            worktree=None,
            log_dir=payload.log_dir,
            bazel_bin=payload.bazel_bin,
            run_tests=True,
            jobs=payload.jobs,
            image=payload.image,
            container_memory=payload.container_memory,
            container_cpus=payload.container_cpus,
            container_network=payload.container_network,
            cache_mounts=list(payload.cache_mounts),
            min_free_bytes=payload.min_free_bytes,
        )

    def _rdeps(self, payload: VerifyInput, output: VerifyOutput) -> RdepverifyInput:
        return RdepverifyInput(
            dest=payload.dest,
            integration_ref=payload.integration_ref,
            build_ok=output.build_ok,
            test_ok=output.test_ok,
            worktree=None,
            log_dir=payload.log_dir,
            affected_only=payload.affected_only,
            rdeps_limit=payload.rdeps_limit,
            rdeps_sample_n=payload.rdeps_sample_n,
            rdeps_sample_seed=payload.rdeps_sample_seed,
            jobs=payload.jobs,
            # The same caches `_own_tests` hands Phase 3, and for a stronger reason: the rdeps
            # `bazel test` covers the whole blast radius, so it is the run with the most to reuse
            # from the disk cache Phase 3 just filled. `VerifyInput` has carried them all along —
            # only this line was missing, and without it Phase 4 ran with no cache at all.
            cache_mounts=list(payload.cache_mounts),
        )

    @staticmethod
    def _absorb(output: VerifyOutput, closure: RdepverifyOutput) -> None:
        """Copy the rdeps evidence across VERBATIM — `equivalence` is never recomputed here.

        `VerificationReport` derives it (`rdeps_truncated` ⇒ `CLOSURE_SAMPLED`, §3.4), and a
        second derivation in the driver is a second opinion the PR body could disagree with.
        """
        output.report = closure.report
        output.rdeps_query = closure.rdeps_query
        output.rdeps_target_count = closure.rdeps_target_count
        output.rdeps_tested = closure.rdeps_tested
        output.rdeps_truncated = closure.rdeps_truncated
        output.rdeps_sample_seed = closure.rdeps_sample_seed
        output.rdeps_sample_n = closure.rdeps_sample_n
        output.direct_rdeps = closure.direct_rdeps
        output.target_pattern_file = closure.target_pattern_file
        if closure.test_command:
            output.steps.append(
                StepRecord(
                    unit=RDEPS_UNIT,
                    command=list(closure.test_command),
                    exit_code=-1 if closure.test_exit_code is None else closure.test_exit_code,
                    ok=closure.test_exit_code == 0,
                    log_path=closure.test_log_path,
                )
            )

    def _handoff(
        self,
        step: WorkerResult[Any],
        units: Sequence[str],
        landed: Sequence[str],
        output: VerifyOutput,
    ) -> WorkerResult[VerifyOutput]:
        done = list(dict.fromkeys(landed))
        remaining = [unit for unit in units if unit not in set(done)]
        status = "failed" if step.status == "partial" else step.status
        error = step.error
        if error is None and status in ("failed", "timeout"):
            error = WorkerError(
                failure_class=FailureClass.UNKNOWN,
                retryable=True,
                stderr_tail=f"{self.name}: a step returned {step.status!r} with no error",
            )
        return WorkerResult[VerifyOutput](
            status=cast("Any", status),
            output=output,
            completed_units=done,
            remaining_units=remaining,
            usage=step.usage,
            error=error,
            evidence=[output.integration_ref],
        )


def _worker(step: str, *, runner: CommandRunner | None = None) -> BaseWorker[Any, Any]:
    """One step, fetched from the REGISTRY by name (§7.2), with its subprocess seam.

    `get_worker` is typed `type[BaseWorker]` and `BaseWorker` declares no `__init__`, so the
    keyword the concrete workers all take (`runner`) is invisible to the type checker at this
    call site. The cast is to the factory, not to the instance: what comes back is still checked
    as a `BaseWorker`, and the alternative — importing `BuildverifyWorker` directly — is the
    constructor call §7.2 exists to forbid.
    """
    factory = cast("Callable[..., BaseWorker[Any, Any]]", get_worker(step))
    return factory() if runner is None else factory(runner=runner)


# --------------------------------------------------------------------------------------
# the Phase 3 / Phase 4 plans, the fenced sinks and the drivers
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _RepoFacts:
    """The `repos` columns Phase 3 reads: whose adapter owns the node, and what the adapter needs
    to place it.

    `dest_path` is the operator's `dest:` from `config/repos.yaml` and is a genuine **override**,
    not the destination: §3.3's `layout(node)` computes one from the adapter whenever it is NULL,
    which is why it may stay NULL for a whole fleet without abandoning a single repo.
    `published` is the primary published coordinate that `path_tail()` addresses; a node without
    one is not a special case (`layout()` synthesizes one from the node id).
    """

    dest_path: str | None
    ecosystem: Ecosystem
    published: Coordinate | None = None
    baseline_test_count: int = 0
    """`repos.baseline_test_count` — the repo's NATIVE, pre-migration test count, read here
    because Phase 3 is where §12.11's green-and-empty check happens and the worker cannot query.
    Defaulting to 0 is `baseline_ok IS NULL`'s meaning: "unknown", which §12.11 treats as "this
    library never had tests" rather than as a lost suite."""


@dataclass(frozen=True, slots=True)
class _BuildIngest:
    """One repo's §3.3 step 1 result: what its merge landed on `integration`, and nothing else.

    Deliberately carries **no ref and no worktree**. The snapshot a build is cut from is a
    property of the RUN and then of the WAVE, never of one repo (`_build_impl` cuts one after the
    run's last ingest and one after each wave publishes), so a per-repo record of one would be a
    second answer to "which tree did this build read" — and the per-repo answer is the one that
    made a fleet-wide root file name a sibling directory the build worktree did not have yet.
    """

    repo_id: str
    dest: str
    merge_sha: str
    source_sha: str
    already_ingested: bool


@dataclass(frozen=True, slots=True)
class _BuildPlan:
    """One repo's Phase 3 plan: the tree that was merged, the ref that names it, and the unit.

    Computed once per repo per invocation, BEFORE the lease, because every field is a fact about
    git the worker must be handed rather than re-derive (§7.1) — and because the worktree the
    dispatch renders into can only be cut once the snapshot ref it is cut from exists.
    """

    repo_id: str
    dest: str
    worktree: Path
    integration_ref: str
    integration_sha: str
    merge_sha: str
    source_sha: str
    already_ingested: bool
    unit: BuildUnit
    targets: tuple[BuildTarget, ...]
    workspace_deps: tuple[WorkspaceDep, ...]
    toolchains: tuple[ToolchainRequirement, ...]
    workspace_files: tuple[SupportFile, ...]
    """The monorepo-ROOT files this repo's adapter says its MODULE.bazel tags name (D10), with
    every `carry_from` candidate already RESOLVED and every lockfile already computed by the
    adapter's declared resolver. Resolved in the driver and not in the worker: the union of these
    is fleet-wide and every dispatch of the wave writes it, so content read from one dispatch's
    worktree would make the same file differ per repo.

    Empty when `_plan_build` returns and filled in by `_fleet_support_files`, which computes
    it ONCE PER ECOSYSTEM over the RUN's whole domain (ADR-0048, ADR-0055): `pnpm-workspace.yaml`
    must
    name every importer and `.bazelignore` owes a line per importer, so this is a fact about the
    fleet that no single repo's plan can hold. Every plan of one ecosystem therefore carries the
    identical tuple, and `_module_inputs` has nothing left to arbitrate."""
    package_files: tuple[SupportFile, ...]
    """The same, for files inside this repo's own package (the `ts_config` source)."""
    root_targets: tuple[BuildTarget, ...]
    """Rules this repo's adapter needs in the monorepo ROOT package — rules_js's
    `npm_link_all_packages()`, which is what makes the pnpm virtual store every
    `//<dest>:node_modules/<pkg>` link resolves through exist at all (the links themselves are
    per importer and live in the importer's own `targets`).
    Unioned fleet-wide by `_module_inputs` for the same reason the root files are: there is one
    root `BUILD.bazel` and every dispatch of the wave renders the identical superset of it."""
    requirements: tuple[ExternalRequirement, ...]
    gazelle: GazelleConfig | None
    baseline_test_count: int
    """§12.11's other half, carried from `repos` so `buildverify` can tell "never had tests" from
    "the migration deleted them". Zero for every repo until it was threaded through here."""
    adapter_name: str
    adapter_degraded: bool
    """True iff the adapter that emitted these targets says so — `EcosystemAdapter.degraded`,
    which only `ecosystems/unknown.py` sets. The §3.1 step 2 floor stays reachable and stays
    DISCLOSED (`EcosystemAdapterUnavailable`); what it no longer is, is universal."""
    gazelle_files: tuple[SupportFile, ...] = ()
    """What a delegating adapter's generator WROTE for this repo, captured out of a scratch tree
    and turned into planned bytes (`_run_gazelle`). One `SupportFile` per `BUILD.bazel` the
    generator created or modified, at **any depth** under `dest` — a Go repo shaped like the
    corpus's `cmd/<name>/main.go` + `internal/<pkg>` gets two files, neither of which is
    `<dest>/BUILD.bazel`.

    **Package files by nature, and a field of their own by necessity.** They live inside this
    repo's own `dest`, exactly like the `ts_config` source `package_files` carries, and they reach
    the worker through the same `support_files` slot — but `_plan_build` recomputes
    `package_files` from the adapter on every re-plan, and a generator pass that ran once for the
    whole RUN, over every root at once, cannot be reproduced from one repo's worktree. They are
    therefore carried across a wave's re-plan verbatim, for the same reason and by the same
    mechanism as `workspace_files`: they are a function of the run's domain, and the domain does
    not move.

    **Defaulted, and last, because the empty tuple is the honest value for almost every plan.**
    It is empty for every non-delegating ecosystem — which is all of them but Go — and empty for a
    delegating one whose generator changed nothing, in which case `<dest>/BUILD.bazel` is the
    directives-only render and that is the right answer rather than a missing one."""


@dataclass(frozen=True, slots=True)
class _VerifyPlan:
    """One repo's Phase 4 plan. `integration_ref` is a FRESH snapshot cut at this task's start
    (§3.4 step 1) — current, and still immutable for the build's duration."""

    repo_id: str
    dest: str
    worktree: Path
    integration_ref: str
    integration_sha: str


@dataclass(slots=True)
class _BuildEvidence:
    """What the sink saw, keyed by repo — the input to §3.3's success criterion.

    A buffer rather than a table read for the reason Phase 2's is: `BuildOutput.green` is a
    conjunction over the *steps of one dispatch*, and `attempts` holds one row per command with
    no column that says "and this repo's set of commands was complete".
    """

    by_repo: dict[str, BuildOutput] = field(default_factory=dict)

    def record(self, output: BuildOutput) -> None:
        self.by_repo[output.repo_id] = output


@dataclass(slots=True)
class _VerifyEvidence:
    by_repo: dict[str, VerifyOutput] = field(default_factory=dict)

    def record(self, output: VerifyOutput) -> None:
        self.by_repo[output.repo_id] = output


class _AttemptWriter:
    """Turns one dispatch's executed commands into `attempts` rows (§3.3 "Idempotency/resume").

    One row per `bazel` invocation, never one per dispatch: §6 CHECKs
    `(command = '[]') = (exit_code IS NULL)` and "the ONLY legal way to have executed nothing is
    an anchoring rejection", so a single row summarising two commands would have to pick one
    argv and discard the other's exit code. `integration_ref` is written by a follow-up UPDATE
    because `AttemptRow` carries no such field — the column exists (v007), the dataclass does
    not, and a build attributed only to a moving branch name is unreproducible (§6).
    """

    def __init__(
        self,
        *,
        writer: StateWriter,
        repository: SqliteStateRepository,
        read_conn: aiosqlite.Connection,
        run_id: str,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self._writer = writer
        self._repository = repository
        self._read = read_conn
        self._run_id = run_id
        self._clock = clock

    async def record(
        self,
        *,
        repo_id: str,
        phase: Phase,
        attempt: int,
        tier: str,
        context_policy: str | None,
        integration_ref: str,
        steps: Sequence[StepRecord],
        error: WorkerError | None,
        cost_usd: float = 0.0,
        llm_cache_hit: bool = False,
    ) -> list[str]:
        stamp = _iso(self._clock())
        written: list[str] = []
        for step in steps:
            command_json = json.dumps(list(step.command))
            attempt_id = str(uuid4())
            await self._repository.record_attempt(
                AttemptRow(
                    attempt_id=attempt_id,
                    run_id=self._run_id,
                    repo_id=repo_id,
                    phase=phase,
                    attempt=max(attempt, 1),
                    retry_ordinal=await self._next_ordinal(repo_id, phase, attempt, command_json),
                    started_at=stamp,
                    finished_at=stamp,
                    tier=tier,
                    context_policy=context_policy,
                    command=command_json,
                    command_sha256=sha256_text(command_json),
                    exit_code=step.exit_code,
                    failure_class=(
                        None if step.ok or error is None else str(error.failure_class)
                    ),
                    duration_ms=step.duration_ms,
                    stderr_tail="" if step.ok or error is None else error.stderr_tail,
                    cost_usd=cost_usd if not written else 0.0,
                    # Attributed exactly as `cost_usd` on the line above is: to the FIRST step row
                    # of the rung. The two must ride the same row or the pair contradicts
                    # `schema.sql`'s `llm_cache_hit = 1 => cost_usd = 0` — a later step row would
                    # carry the flag beside a `cost_usd` that is 0 only because the first row took
                    # it, which reads as a cache hit on a step that made no LLM call.
                    llm_cache_hit=llm_cache_hit if not written else False,
                )
            )
            written.append(attempt_id)
            if integration_ref:
                await self._writer.submit(self._stamp_ref(attempt_id, integration_ref))
        return written

    def _stamp_ref(
        self, attempt_id: str, integration_ref: str
    ) -> Callable[[aiosqlite.Connection], Coroutine[Any, Any, None]]:
        params = (integration_ref, attempt_id)

        async def unit(conn: aiosqlite.Connection) -> None:
            await conn.execute(
                "UPDATE attempts SET integration_ref = ? WHERE attempt_id = ?", params
            )

        return unit

    async def _next_ordinal(
        self, repo_id: str, phase: Phase, attempt: int, command_json: str
    ) -> int:
        rows = await _rows(
            self._read,
            "SELECT COALESCE(MAX(retry_ordinal) + 1, 0) FROM attempts "
            " WHERE run_id = ? AND repo_id = ? AND phase = ? AND attempt = ? "
            "   AND command_sha256 = ?",
            (self._run_id, repo_id, int(phase), max(attempt, 1), sha256_text(command_json)),
        )
        return int(rows[0][0])


class _BuildSink:
    """Persists one Phase 3 dispatch's evidence under the fence that produced it (§11.5).

    Called before the terminal status for the reason every other sink is: a `SUCCEEDED` phase is
    never re-admitted, so evidence written after the status is evidence a crash can lose.
    """

    def __init__(
        self,
        *,
        attempts: _AttemptWriter,
        writer: StateWriter,
        run_id: str,
        evidence: _BuildEvidence,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self._attempts = attempts
        self._writer = writer
        self._run_id = run_id
        self._evidence = evidence
        self._clock = clock

    async def __call__(
        self, *, repo_id: str, phase: Phase, fence: int, result: WorkerResult[BuildOutput]
    ) -> None:
        output = result.output
        if output is None:  # pragma: no cover - the runner only calls a sink with an output
            return
        self._evidence.record(output)
        await self._attempts.record(
            repo_id=repo_id,
            phase=phase,
            attempt=output.attempt,
            tier=output.tier,
            context_policy=output.context_policy,
            integration_ref=output.integration_ref,
            steps=output.steps,
            error=result.error,
            cost_usd=result.usage.cost_usd,
            llm_cache_hit=result.usage.all_served_from_llm_cache,
        )
        if output.module_lock_foreign_registry:
            # The durable half of `_publish_module_lock`'s decision. The publish already happened
            # and is not being reversed; what this row buys is that "the branch carries a lock a
            # `--network=none` build cannot use" survives the process that noticed it. `warn` and
            # not `error` on purpose: the check cannot see a `common --registry=` line in the
            # monorepo's own `.bazelrc`, so on a host that pins a mirror there this row is a false
            # positive, and a severity that overstated it would train an operator to ignore it.
            await _note_finding(
                self._writer,
                self._run_id,
                repo_id,
                kind="ModuleLockForeignRegistry",
                payload={
                    "repo_id": repo_id,
                    "path": MODULE_LOCK_PATH,
                    "detail": output.module_lock_foreign_registry,
                },
                severity="warn",
                now=self._clock(),
            )
        if not output.published_sha:
            return
        params = (output.published_sha, _iso(self._clock()), self._run_id, repo_id, int(phase),
                  fence)

        async def unit(conn: aiosqlite.Connection) -> None:
            await conn.execute(
                "UPDATE phases SET post_commit_sha = ?, updated_at = ? "
                " WHERE run_id = ? AND repo_id = ? AND phase = ? AND lease_fence = ?",
                params,
            )

        await self._writer.submit(unit)


class _VerifySink:
    """The Phase 4 half of the same contract: the `attempts` rows §3.4 step 3 says the report is
    assembled *from*, and — because `fleet pr` is a separate process — the assembled report too.

    The `VerificationReport` has no table in §6, and until this sink persisted it the verdict
    reached only the verb's JSON payload, in memory, for the life of one `fleet verify`. That is
    not enough for §3.4 step 4: `fleet pr` runs later, and `equivalence` is what forces a
    `STUB_LIMITED`/`CLOSURE_SAMPLED` PR to a draft. A `fleet pr` that had to re-derive the verdict
    from `attempts` argv could not recover `rdeps_target_count`/`rdeps_truncated` at all, so a
    sampled closure would have been presented to a reviewer as an unqualified pass — the one
    reading §3.4 spends a whole paragraph forbidding. So the report is written as a `findings` row
    (free-text `kind` by design, §6) alongside the `rdeps_sample_seed` the banner names, which
    `VerificationReport` has no column for.
    """

    def __init__(
        self,
        *,
        attempts: _AttemptWriter,
        evidence: _VerifyEvidence,
        writer: StateWriter,
        run_id: str,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self._attempts = attempts
        self._evidence = evidence
        self._writer = writer
        self._run_id = run_id
        self._clock = clock

    async def __call__(
        self, *, repo_id: str, phase: Phase, fence: int, result: WorkerResult[VerifyOutput]
    ) -> None:
        _ = fence
        output = result.output
        if output is None:  # pragma: no cover - the runner only calls a sink with an output
            return
        self._evidence.record(output)
        await self._attempts.record(
            repo_id=repo_id,
            phase=phase,
            attempt=output.attempt,
            tier=output.tier,
            context_policy=output.context_policy,
            integration_ref=output.integration_ref,
            steps=output.steps,
            error=result.error,
            cost_usd=result.usage.cost_usd,
            llm_cache_hit=result.usage.all_served_from_llm_cache,
        )
        if output.report is not None:
            await _record_verification(
                self._writer,
                self._run_id,
                repo_id,
                output.report,
                seed=output.rdeps_sample_seed,
                now=self._clock(),
            )


async def _repo_facts(conn: aiosqlite.Connection) -> dict[str, _RepoFacts]:
    """`repos.dest_path`, the node's primary ecosystem and its published coordinate, fleet-wide.

    The coordinate is joined in rather than left behind because it is what `path_tail()`
    addresses: without it every adapter-computed destination would fall back to the synthesized
    `Coordinate(name=repo_id)` and a Maven module would land in `java/acme-commons` instead of
    `java/com/acme/commons` — a layout that is not wrong so much as not the one §3.3 specifies.
    """
    rows = await _rows(
        conn,
        "SELECT r.repo_id, r.dest_path, r.ecosystems, c.ecosystem, c.grp, c.name, "
        "       r.baseline_test_count "
        "  FROM repos AS r LEFT JOIN coordinates AS c ON c.coord_key = r.primary_coord_key",
    )
    facts: dict[str, _RepoFacts] = {}
    for row in rows:
        declared = json.loads(str(row[2] or "[]"))
        ecosystem = Ecosystem.UNKNOWN
        for name in declared:
            with suppress(ValueError):
                ecosystem = Ecosystem(str(name))
                break
        published: Coordinate | None = None
        if row[3] is not None:
            with suppress(ValueError):
                published = Coordinate(
                    ecosystem=Ecosystem(str(row[3])), group=str(row[4] or ""), name=str(row[5])
                )
        facts[str(row[0])] = _RepoFacts(
            dest_path=None if row[1] is None else str(row[1]),
            ecosystem=ecosystem,
            published=published,
            baseline_test_count=int(row[6] or 0),
        )
    return facts


def _validate_monorepo_dir_overrides(overrides: Mapping[Ecosystem, str]) -> None:
    """§9's `build.monorepo_dir_overrides`, checked against the REGISTRY and not only against
    itself (Rule 11).

    `BuildSection` already refuses two overrides pointing at one directory. What it cannot see is
    the other half of the same collision: an override aimed at a directory some *un-overridden*
    adapter still owns merges two languages' trees into one, and no later stage can un-merge
    them. The check is a set comparison over `monorepo_dirs()`, so it names no directory and no
    ecosystem member of its own.
    """
    defaults = ecosystems.monorepo_dirs()
    taken = {
        directory: eco for eco, directory in defaults.items() if eco not in overrides
    }
    for eco, directory in sorted(overrides.items(), key=lambda item: item[0].value):
        owner = taken.get(directory)
        if owner is not None:
            raise UsageError(
                f"build.monorepo_dir_overrides sends {eco.value} to {directory!r}, which is "
                f"already {owner.value}'s `monorepo_dir`; two ecosystems under one directory "
                "merge two languages' trees and nothing downstream can separate them again (§9)."
            )


def _dest_for(repo_id: str, facts: _RepoFacts, overrides: Mapping[Ecosystem, str]) -> str:
    """§3.3's `layout(node)` for one repo — the driver's ONLY answer to "where does this go".

    `repos.dest_path` is passed as `dest_override` because that is what it is: the `dest:` an
    operator wrote in `config/repos.yaml`, which §3.3 gives precedence to. When it is NULL the
    destination comes from the adapter's `monorepo_dir` and `path_tail()`, which is why this
    function names no directory and compares no `Ecosystem` member (§13 row 33).

    `build.monorepo_dir_overrides` (§9) is applied HERE and only to the adapter-computed branch.
    `monorepo_dir` is a `ClassVar` on a singleton shared by every process and every `cpu_pool`
    child, so an override written onto the adapter would be a global mutation with no owner; the
    driver rewrites the one leading segment `layout()` took from the adapter instead, and leaves
    `path_tail()` — the part that is genuinely adapter knowledge — untouched. An explicit `dest:`
    is the operator's own answer to the same question and is never second-guessed by it.
    """
    node = LayoutNode(
        node_id=repo_id,
        ecosystem=facts.ecosystem,
        published=facts.published,
        dest_override=facts.dest_path,
    )
    dest = layout(node, ecosystems)
    if facts.dest_path is not None:
        return dest
    replacement = overrides.get(facts.ecosystem)
    if replacement is None:
        return dest
    head, separator, tail = dest.partition("/")
    if not separator or head != ecosystems.monorepo_dirs()[facts.ecosystem]:
        return dest
    return normalize_dest(f"{replacement}/{tail}")


def _internal_label(dest: str) -> str:
    """`//<dest>:<leaf>` — the label the dest's own adapter will name its primary target.

    `path_segment` is imported from the adapter package rather than reimplemented, because this
    label has to be byte-identical to the one `ecosystems.base.target_name()` produces on the
    *other* side of the edge; a second sanitizer here is how `//ts/acme/lib:lib` and
    `//ts/acme/lib:Lib` come to be two spellings of one target and Bazel reports the consumer's
    build as broken.
    """
    return f"//{dest}:{path_segment(dest.rstrip('/').rsplit('/', maxsplit=1)[-1])}"


async def _unit_deps(
    conn: aiosqlite.Connection,
    settings: FleetSettings,
    run_id: str,
    *,
    facts: Mapping[str, _RepoFacts],
    overrides: Mapping[Ecosystem, str],
) -> dict[str, tuple[InternalDep, ...]]:
    """Every repo's INTERNAL dependencies, over the SAME subgraph the sequencer ordered.

    `graph.dag_edge_kinds` above `graph.min_confidence`, `ordering_suppressed = 0` — the filter
    `_ordering_pairs` uses. Sharing it is the point: an internal `deps = ["//..."]` the sequencer
    did not order is a Bazel edge pointing at a package that has not been merged yet, and a
    suppressed feedback edge (§3.1 6d) re-emitted here would rebuild in `BUILD.bazel` exactly the
    cycle Phase 1 broke.

    Only the internal half: `graph/infer.py` writes no `edges` row at all for a dependency that
    resolves to no internal repo ("an edge that orders nothing is not written"), so the external
    half is recovered from the manifests instead — see `_external_coordinates`.

    Destinations are resolved through `_dest_for`, so a dependency's label follows its adapter's
    `monorepo_dir` — including an override — with no second copy of the layout rule. A repo whose
    own destination `layout()` refuses contributes no label rather than a broken one; its own
    `_ingest_build_source` is where that refusal is reported.
    """
    resolved: dict[str, InternalDep] = {}
    for repo_id, fact in facts.items():
        with suppress(ReservedDestError, ValueError):
            dest = _dest_for(repo_id, fact, overrides)
            # The label AND the two facts an adapter needs to link the sibling in its own
            # dialect (D12): `dest`, and the coordinate the sibling publishes. Both are read from
            # the same `_RepoFacts` row that produced the label, so a first-party link and the
            # Bazel edge beside it can never describe two different repos.
            resolved[repo_id] = InternalDep(
                label=_internal_label(dest), dest=dest, published=fact.published
            )

    kinds = tuple(str(kind) for kind in settings.config.graph.dag_edge_kinds)
    placeholders = ",".join("?" for _ in kinds)
    rows = await _rows(
        conn,
        # `placeholders` is a run of `?`, one per configured edge kind — no value is
        # interpolated, so this is parameterised in the only sense that matters.
        "SELECT src_id, dst_id FROM edges "  # noqa: S608
        " WHERE run_id = ? AND src_kind = 'REPO' AND dst_kind = 'REPO' AND dst_id IS NOT NULL "
        "   AND ordering_suppressed = 0 AND confidence >= ? "
        f"   AND kind IN ({placeholders}) ORDER BY edge_key",
        (run_id, settings.config.graph.min_confidence, *kinds),
    )
    internal: dict[str, dict[str, InternalDep]] = {}
    for row in rows:
        dep = resolved.get(str(row[1]))
        if dep is not None:
            internal.setdefault(str(row[0]), {})[dep.label] = dep
    return {
        repo_id: tuple(deps[label] for label in sorted(deps))
        for repo_id, deps in ((key, internal.get(key, {})) for key in facts)
    }


async def _owned_coordinate_keys(conn: aiosqlite.Connection) -> frozenset[str]:
    """Every `Coordinate.key` an internal repo publishes — §3.1 step 3's internal/external oracle.

    Read from `coordinates.owner_repo_id` rather than from this invocation's evidence, for the
    reason `_persist_scan_edges` documents: a repo scanned by an earlier invocation still owns
    its coordinate, and an oracle that only knew about this one would call it external and put a
    sibling repo into `MODULE.bazel` as a third-party artifact.
    """
    rows = await _rows(
        conn, "SELECT coord_key FROM coordinates WHERE owner_repo_id IS NOT NULL"
    )
    return frozenset(str(row[0]) for row in rows)


async def _manifest_paths(conn: aiosqlite.Connection) -> dict[str, tuple[str, ...]]:
    """repo_id → the manifest paths Phase 1 parsed, excluding the §3.1 step 2 catch-all.

    `adapter = 'unknown'` is the synthetic row a repo with no recognizable manifest gets; it
    names no real file and parsing it would re-run the catch-all that returns `[]` by definition.
    """
    rows = await _rows(
        conn,
        "SELECT repo_id, path FROM manifests "
        " WHERE adapter <> 'unknown' AND parse_error IS NULL ORDER BY repo_id, path",
    )
    out: dict[str, list[str]] = {}
    for row in rows:
        out.setdefault(str(row[0]), []).append(str(row[1]))
    return {repo_id: tuple(paths) for repo_id, paths in out.items()}


def _external_coordinates(
    root: Path, paths: Sequence[str], owned: frozenset[str]
) -> list[Coordinate]:
    """The repo's EXTERNAL dependencies, re-read from its own manifests in the merged tree.

    Re-read rather than looked up, because §6 has no per-dependency table: `manifests` records a
    dependency *count*, `edges` is written only where a dependency resolves to an internal repo
    (`graph/infer.py` skips the external case explicitly), and `coordinates` holds external keys
    with no version spec and no owner. So the one durable statement of "this repo depends on
    guava 33.2.1-jre" is the manifest file itself — which Phase 3 has already merged into the
    worktree and can read at `<dest>/<path>` for nothing.

    Dispatch is `manifests.adapter_for` (§7.3), so no ecosystem is named here and a new manifest
    format is picked up by dropping a file into `src/fleet/manifests/`. A file that no longer
    parses is skipped rather than raised on: Phase 1 already recorded that as a `parse_error` and
    a build is not the place to re-report it.
    """
    found: dict[tuple[str, str], Coordinate] = {}
    for rel in paths:
        path = root / rel
        adapter = manifest_adapter_for(path)
        if adapter is None or adapter.name == "unknown":
            continue
        try:
            raws = adapter.parse(path)
        except (ManifestParseError, OSError):
            continue
        for raw in raws:
            if raw.optional:
                continue
            coordinate = adapter.coordinate(raw)
            if coordinate.key in owned:
                continue  # an internal sibling: it is a `//` label, not a `bazel_dep`
            found.setdefault((coordinate.key, coordinate.version_spec or ""), coordinate)
    return [found[key] for key in sorted(found)]


def _existing_paths(root: Path, paths: Sequence[str]) -> list[str]:
    """The subset of `paths` that is on disk under `root`, order preserved.

    A named sync helper called from a coroutine, the same shape (and for the same reason) as
    `workers/buildverify.dirs_present`: a handful of `stat(2)` calls do not need a thread, and
    writing them inline in an `async def` trips `ASYNC240`.
    """
    return [path for path in paths if (root / path).exists()]


def _resolve_support_files(
    worktree: Path | Sequence[Path], declared: Sequence[SupportFile]
) -> tuple[SupportFile, ...]:
    """Turn each adapter-declared `SupportFile` into one whose `content` is the file that will
    actually be written — the REAL file whenever the tree has one (D10).

    **`worktree` may be several**, because these files are resolved once per ECOSYSTEM over every
    one of its units and the units' worktrees are snapshots of the same integration branch taken
    at different times: since ADR-0055 every unit's history is merged before the first of those
    snapshots is cut, but a wave that has published moves the branch on, so the trees still differ
    in what they carry at the ROOT and in the packages published so far. The trees are searched in
    the order given — the driver hands them in `repo_id` order, a total order — so the resolved
    bytes are a function of the fleet and not of which snapshot the driver happened to look at
    first. A path present in two of them carries the same content by construction: every one of
    these trees is an ancestor-or-descendant of the others on one append-only branch.

    Only the declared `carry_from` candidates are consulted, in order, and never the destination
    itself. That asymmetry is deliberate and is the difference between the two kinds of file: a
    package file names its own path as its candidate (relocation already put the repo's
    `tsconfig.json` exactly there, so carrying it and leaving it alone are one operation), while a
    root file is fleet-owned and must be re-derived every run. Treating an existing destination as
    a carry would freeze the monorepo's `requirements.lock` at whatever the first wave wrote, and
    a repo joining in wave 12 would never appear in it.

    Resolution happens HERE — in the driver, against the repo's own merged tree — rather than in
    the worker, whose worktree is one snapshot among the wave's several. A worker that read the
    tree would emit the carried lock for the repo that owns it and the synthesized floor for
    every other dispatch, and those two disagree on the bytes of a file all of them merge (§11.6).

    A candidate that is not valid UTF-8 is skipped rather than mangled: `SupportFile.content` is
    text, and a lockfile that did not survive decoding is not the repo's lockfile.
    """
    roots = [worktree] if isinstance(worktree, Path) else list(worktree)
    out: list[SupportFile] = []
    for support in declared:
        content = support.content
        for candidate in support.carry_from:
            found = next(
                (root / candidate for root in roots if (root / candidate).is_file()), None
            )
            if found is None:
                continue
            try:
                content = found.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            break
        out.append(support.model_copy(update={"content": content}))
    return tuple(out)


def _carried(worktrees: Sequence[Path], support: SupportFile) -> bool:
    """Whether this `SupportFile` has a REAL file behind it in the ecosystem's merged trees.

    The predicate the carry-beats-resolve rule turns on, and it is deliberately the same question
    `_resolve_support_files` answers by reading: a repo that ships a lockfile has a resolution it
    tested against, and re-resolving it would silently move versions it pinned.

    **An adapter that drops `carry_from` on its lock therefore always reaches the resolver**, and
    that is the point of the JS half of ADR-0048: with ≥1 pnpm importer the root lock is a
    resolution of the workspace, so this short-circuit — which skips the resolver entirely — must
    not be reachable for it.
    """
    return any(_existing_paths(worktree, support.carry_from) for worktree in worktrees)


async def _run_resolution(
    plan: Resolution,
    *,
    repo_id: str,
    worktrees: Sequence[Path],
    scratch: Path,
    deadline: float | None = None,
) -> str:
    """Execute an adapter's declared resolver in `scratch` and return the lockfile it wrote.

    The scratch directory is not the build worktree, and that separation is the point: the
    resolver's inputs are a *synthesized* view of what stays external (`requirements.in`, a
    `package.json` holding only registry dependencies), and writing them into the tree Phase 3 is
    about to build would put files in the monorepo that no adapter declared and no MODULE.bazel
    names. It is recreated per call, so a stale lock from a previous run can never be mistaken for
    this run's output — the read below would otherwise succeed against the wrong file.

    Every failure mode is loud and names the resolver (Rule 11): a missing tool, a non-zero exit,
    a timeout, an input with nothing behind it, and — the one that matters most — a resolver that
    exits 0 and writes nothing or writes an empty file. That last case is exactly the shape of the
    defect this path closes: an empty lock parses, produces a hub with no packages, and is
    indistinguishable from a repo that has no dependencies at all.
    """
    await asyncio.to_thread(shutil.rmtree, scratch, ignore_errors=True)
    await asyncio.to_thread(scratch.mkdir, parents=True, exist_ok=True)
    for source in _resolve_support_files(list(worktrees), plan.inputs):
        if not source.content:
            raise DependencyResolutionError(
                f"{repo_id}: {plan.argv[0]} needs {source.path!r} and neither a carried file nor "
                f"a synthesized floor produced any content for it; resolving from an empty input "
                f"would write an empty lock"
            )
        target = scratch / source.path
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(target.write_text, source.content, encoding="utf-8")

    runner = RESOLVER_RUNNER or proc_run
    # **`Resolution.env` OVERLAYS the inherited environment; it never replaces it.** The merge is
    # done HERE, at the call site, and not left to the runner, because `util/proc.run` passes its
    # `env=` straight to `create_subprocess_exec`, which REPLACES the child's environment
    # wholesale: handing it `{"GOTOOLCHAIN": "go1.24.12"}` alone would launch a `go` with no `PATH`
    # to be found on and no `HOME` to find its module cache under, turning a pin into
    # `FileNotFoundError` — which this function then reports as "`go` is not installed on this
    # host". Merging before the seam also means every runner behind it, real or fake, sees one
    # fully-formed environment and cannot disagree about the semantics. The declared vars win over
    # the inherited ones, which is the entire point: `GOTOOLCHAIN=auto` in the operator's shell
    # must not decide which SDK computes the sums this run commits.
    env = {**os.environ, **plan.env}
    try:
        result = await runner(
            plan.argv, cwd=scratch, env=env, deadline=deadline, timeout_s=plan.timeout_s
        )
    except FileNotFoundError as exc:
        raise DependencyResolutionError(
            f"{repo_id}: {plan.argv[0]!r} is not installed on this host, so the lockfile "
            f"{plan.lock_path!r} cannot be resolved; a lockfile is a resolution and this harness "
            f"will not substitute the declared specs for one"
        ) from exc
    if not result.ok:
        raise DependencyResolutionError(
            f"{repo_id}: {' '.join(plan.argv)} exited {result.exit_code}"
            f"{' (timed out)' if result.timed_out else ''} while resolving "
            f"{plan.lock_path!r}: {result.stderr_tail.strip() or result.stdout_tail.strip()}"
        )

    produced = scratch / plan.lock_path
    text = await asyncio.to_thread(_read_if_file, produced)
    if not (text or "").strip():
        raise DependencyResolutionError(
            f"{repo_id}: {' '.join(plan.argv)} exited 0 but wrote no usable {plan.lock_path!r} "
            f"in {scratch}; an empty lock is indistinguishable from 'this repo has no "
            f"dependencies' and is exactly how a missing transitive closure stays invisible"
        )
    return text or ""


def _read_if_file(path: Path) -> str | None:
    """`path`'s text, or `None` if it is not a readable UTF-8 file. A named sync helper so the
    caller can hand it to `asyncio.to_thread` instead of doing file I/O on the event loop."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


async def _resolved_support_files(
    adapter: EcosystemAdapter,
    units: Sequence[BuildUnit],
    *,
    repo_id: str,
    worktrees: Sequence[Path],
    scratch: Path,
) -> tuple[SupportFile, ...]:
    """The adapter's root files with the lockfile among them **actually resolved** (§3.3 step 2).

    The precedence, in one place, because it is the whole contract:

    1. **carry** — the repo shipped the file, so its own resolution wins and no resolver runs.
       Re-resolving a lock a repo pinned would move versions it tested against, which is a change
       the migration has no business making;
    2. **resolve** — nothing to carry and the adapter declares a resolver, so the transitive
       closure is computed. This is the step whose absence made every synthesized lock a list of
       declared specs, which `pip.parse` and `npm_translate_lock` read as a *resolution* and then
       failed on, naming a package no manifest in the fleet ever mentioned;
    3. **floor** — no carry and no resolver: `SupportFile.content`, which says in its own first
       line that it is generated.

    Step 2 never falls back to step 3. That fallback is the defect: an empty lock is a well-formed
    file that produces an empty dependency hub and no error until Bazel is deep inside a ruleset.

    **Once per ECOSYSTEM over every one of its units, never once per repo** (ADR-0048). These are
    files at the monorepo ROOT, one per path, and some of them state a fact about the whole fleet
    — `pnpm-workspace.yaml`'s importer list, `.bazelignore`'s line per importer — so a per-repo
    call renders different bytes for one path in every repo and `_module_inputs` keeps exactly
    one of them. Widening `workspace_files()`/`resolution()` to take a `Sequence[BuildUnit]` was
    inert until this call site passed the ecosystem's full unit set; this is that call site.
    """
    declared = adapter.workspace_files(units)
    files = await asyncio.to_thread(_resolve_support_files, list(worktrees), declared)
    plan = adapter.resolution(units)
    if plan is None:
        return files
    lock = next((f for f in declared if f.path == plan.lock_path), None)
    if lock is None:
        raise DependencyResolutionError(
            f"{repo_id}: adapter {adapter.name!r} declares a resolver for {plan.lock_path!r}, "
            f"which is not one of the files it says its MODULE.bazel tags name "
            f"({sorted(f.path for f in declared)}); a resolved file nothing references is a file "
            f"nothing reads"
        )
    if await asyncio.to_thread(_carried, list(worktrees), lock):
        return files
    content = await _run_resolution(
        plan, repo_id=repo_id, worktrees=worktrees, scratch=scratch
    )
    return tuple(
        f.model_copy(update={"content": content}) if f.path == plan.lock_path else f
        for f in files
    )


GENERATED_BUILD_FILE: Final = "BUILD.bazel"
"""The file name a delegating generator writes, and the only one `_run_gazelle` captures back.

One constant rather than a literal in three places, for `_GO_MOD_LABEL`'s reason: the name the
scratch is scanned for, the name the directives are planted under and the name the captured
`SupportFile` carries have to be the same string or the capture silently finds nothing."""

GAZELLE_TIMEOUT_S: Final = 600.0
"""Ceiling for the one fleet-wide generator invocation (§11.1). It is one process over every Go
root in the fleet, not one per repo, so it is bounded like a resolver rather than like a build."""


def _scratch_build_files(root: Path) -> dict[str, str]:
    """Every `BUILD.bazel` under `root`, keyed by root-relative POSIX path.

    Walked recursively and keyed by PATH rather than by directory, because what the generator
    produces is a tree of packages and not one file: a repo shaped like the corpus's
    `cmd/<name>/main.go` + `internal/<pkg>` gets files at depth 2 and 3 under its `dest` and none
    at `<dest>` itself. A capture that looked only at `<dest>/BUILD.bazel` would find the
    directives file byte-identical, conclude nothing happened, and publish a Go package with no
    targets in it while every assertion about it stayed green.

    A file that is not readable UTF-8 is omitted rather than mangled — the captured bytes become
    `SupportFile.content`, which is text.
    """
    out: dict[str, str] = {}
    for path in sorted(root.rglob(GENERATED_BUILD_FILE)):
        if not path.is_file():
            continue
        text = _read_if_file(path)
        if text is not None:
            out[path.relative_to(root).as_posix()] = text
    return out


def _assemble_gazelle_scratch(scratch: Path, plans: Sequence[_BuildPlan]) -> None:
    """Build the tree the generator runs over — **never a build worktree** (§3.3 step 2).

    A scratch tree for `_run_resolution`'s reason and one sharper one. The generator *writes*:
    it creates and rewrites `BUILD.bazel` files wherever it finds sources, and pointing it at a
    build worktree would make its output a mutation of the tree Phase 3 is about to build and
    publish — which is precisely the class of thing ADR-0054 forbids from becoming a commit. Run
    in a scratch and captured, the same bytes arrive as *planned* bytes, and the publish contract
    holds over them unchanged rather than needing an exception carved into it.

    What goes in, and why each part is needed:

    1. **The relocated sources**, copied from each unit's build worktree at `<dest>` — the tree
       the plan describes, so the packages the generator finds are the packages the build will
       have. Copied rather than symlinked: the generator writes, and a symlink farm would put
       those writes back in the worktree this function exists to keep out of.
    2. **The directives-only `<dest>/BUILD.bazel`**, rendered from the same `GazelleConfig` the
       worker will render — `# gazelle:prefix` is what resolves an intra-repo import to a label
       at all, so a scratch without it produces a package whose every internal dependency is an
       unresolvable external one.
    3. **The monorepo-root files the adapter resolved** (`go.mod`, `go.sum` for Go), verbatim
       from `workspace_files`. The union `go.mod` is the module graph `-external=static` resolves
       every out-of-tree import against; without it the generator has no pins to resolve from.
    4. **A root `MODULE.bazel`**, only if the adapter did not declare one, as a repo-root marker.
       Its content is a comment: nothing reads it, and a scratch file is never published, so
       synthesizing a real module declaration here would be inventing bytes to no purpose.

    Recreated per call, so output from a previous run can never be mistaken for this one's — the
    before/after comparison in `_run_gazelle` would otherwise read a stale file as "unchanged".
    """
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True, exist_ok=True)
    for plan in sorted(plans, key=lambda p: p.dest):
        shutil.copytree(plan.worktree / plan.dest, scratch / plan.dest, dirs_exist_ok=True)
        if plan.gazelle is not None:
            (scratch / plan.dest / GENERATED_BUILD_FILE).write_text(
                render_gazelle_build(plan.gazelle), encoding="utf-8"
            )
    roots = {file.path: file.content for plan in plans for file in plan.workspace_files}
    for path in sorted(roots):
        target = scratch / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(roots[path], encoding="utf-8")
    marker = scratch / "MODULE.bazel"
    if not marker.exists():
        marker.write_text(
            "# Scratch repo-root marker for the BUILD-file generator. Never published.\n",
            encoding="utf-8",
        )


def _gazelle_argv(binary: str, scratch: Path, plans: Sequence[_BuildPlan]) -> list[str]:
    """The ONE invocation: the binary, the repo root, the adapter's flags, every unit's root.

    **One invocation over every root of the ecosystem, and that is the whole design.** With both
    Go roots on the command line an import of a sibling resolves to a real in-repo label
    (`//go/digest`); with only one root passed, the same import is **silently dropped and the
    generator exits 0**. Cross-repo edges are the entire point of a monorepo migration, so a
    per-repo invocation would produce a fleet of packages that build individually and express
    none of the dependencies the migration exists to express.

    `-repo_root=` is required and is not the same as `cwd`: run from a foreign working directory
    without it the generator refuses with `not a subdirectory of repo root`, and the directories
    are therefore passed **absolute and under that root**.

    The flags come from `GazelleConfig.args` and are never spelled here (§12.6): which resolution
    mode is safe is a fact about the generator's language plugin, and the driver naming it would
    be the driver knowing what Go is. Every unit of the group must declare the identical flags —
    they configure one process that serves all of them, so a disagreement has no correct
    resolution and is refused loudly rather than averaged (Rule 7).
    """
    declared = {tuple(plan.gazelle.args) for plan in plans if plan.gazelle is not None}
    if len(declared) > 1:
        raise BuildFileGenerationError(
            f"{', '.join(sorted(plan.repo_id for plan in plans))}: the units of one delegating "
            f"ecosystem declare different generator flags {sorted(declared)}, but they are "
            f"served by ONE invocation over all of their roots — there is no argv that is both"
        )
    flags = list(next(iter(declared), ()))
    return [
        binary,
        f"-repo_root={scratch}",
        *flags,
        *[str(scratch / dest) for dest in sorted(plan.dest for plan in plans)],
    ]


async def _run_gazelle(
    plans: Sequence[_BuildPlan],
    *,
    binary: str,
    scratch: Path,
    deadline: float | None = None,
) -> dict[str, tuple[SupportFile, ...]]:
    """Run a delegating adapter's generator once over the whole ecosystem and CAPTURE its output.

    Returns `repo_id → the BUILD files the generator created or modified inside that repo's
    `dest``, as `SupportFile`s whose `content` is the generated text — i.e. as **planned bytes**.
    From there the existing path does the rest with no new machinery: `buildgen.materialize`
    writes them in GENERATE, and `_publish` re-materializes and stages them (ADR-0054).

    **Created or modified, computed by comparison rather than assumed.** The scratch's
    `BUILD.bazel` set is read before and after, and a file is captured when it is new or its
    bytes changed. That is the only way to get the real answer: for a fixture-shaped fleet the
    generator *creates* `go/clitool/cmd/clitool/BUILD.bazel` and
    `go/clitool/internal/command/BUILD.bazel`, *modifies* `go/digest/BUILD.bazel`, and leaves
    `go/clitool/BUILD.bazel` **byte-identical** — so a capture of `<dest>/BUILD.bazel` alone
    captures nothing for one repo and an unchanged file for the other, and looks like it worked.

    **A file the generator wrote outside every unit's `dest` is a loud refusal**, not a dropped
    file: there is nothing to attribute it to, and a `SupportFile` no repo owns would either be
    lost or published by whichever dispatch happened to carry it.

    **Determinism.** The generator's output was measured byte-identical across repeated clean
    runs and idempotent on re-run; everything this function adds to that is sorted — the roots on
    the command line, the walk of the scratch, and the captured files per repo — so one plan set
    yields one argv and one captured set regardless of the order the driver holds its plans in
    (§11.6).

    **What is deliberately NOT checked here: that anything was captured at all.** A unit with no
    sources the generator recognises legitimately yields nothing, and the driver cannot tell that
    case from a broken one without knowing what a Go file is (§12.6). The vacuity guard is a test
    that asserts real targets in the published file, not a driver-side heuristic.
    """
    await asyncio.to_thread(_assemble_gazelle_scratch, scratch, plans)
    before = await asyncio.to_thread(_scratch_build_files, scratch)
    argv = _gazelle_argv(binary, scratch, plans)
    runner = GAZELLE_RUNNER or proc_run
    members = ", ".join(sorted(plan.repo_id for plan in plans))
    try:
        result = await runner(
            argv, cwd=scratch, deadline=deadline, timeout_s=GAZELLE_TIMEOUT_S
        )
    except FileNotFoundError as exc:
        raise BuildFileGenerationError(
            f"{members}: {binary!r} is not installed on this host, so the BUILD files these "
            f"repos delegate to it cannot be generated; a delegating adapter emits no targets of "
            f"its own, so continuing would publish Go packages containing nothing at all"
        ) from exc
    if not result.ok:
        raise BuildFileGenerationError(
            f"{members}: {' '.join(argv)} exited {result.exit_code}"
            f"{' (timed out)' if result.timed_out else ''}: "
            f"{result.stderr_tail.strip() or result.stdout_tail.strip()}"
        )
    after = await asyncio.to_thread(_scratch_build_files, scratch)
    captured: dict[str, list[SupportFile]] = {plan.repo_id: [] for plan in plans}
    owners = sorted(((plan.dest, plan.repo_id) for plan in plans), key=lambda pair: -len(pair[0]))
    for path in sorted(after):
        if before.get(path) == after[path]:
            continue
        owner = next((repo for dest, repo in owners if path.startswith(f"{dest}/")), None)
        if owner is None:
            raise BuildFileGenerationError(
                f"{members}: {argv[0]} wrote {path!r}, which is inside none of the destinations "
                f"it was pointed at ({sorted(plan.dest for plan in plans)}); a generated file no "
                f"unit owns is one no dispatch would publish"
            )
        captured[owner].append(SupportFile(path=path, carry_from=[], content=after[path]))
    return {repo_id: tuple(files) for repo_id, files in captured.items()}


async def _gazelle_files(
    plans: Mapping[str, _BuildPlan],
    *,
    settings: FleetSettings,
    run_id: str,
) -> tuple[dict[str, tuple[SupportFile, ...]], dict[str, BuildStepUnavailableError]]:
    """Every delegating repo's generated BUILD files, computed ONCE PER ECOSYSTEM per run.

    Returns `(repo_id → captured files, repo_id → the failure that cost it them)`, the same shape
    and the same containment as `_fleet_support_files` — and grouped the same way, by adapter,
    because the fact being computed is the ecosystem's and not the repo's: the generator resolves
    an import of a sibling to an in-repo label only when every one of that ecosystem's roots is
    on one command line.

    A failure is attributed to **every repo of that ecosystem** for the resolver's reason: one
    invocation serves all of them, so there is no per-repo generation left to succeed, and
    letting the survivors continue would publish packages whose cross-repo edges silently vanished.

    Non-delegating ecosystems are absent from the result entirely — `uses_gazelle` is the whole
    predicate, read off the adapter, so the driver never asks which language it got (§12.6).
    """
    groups: dict[str, list[_BuildPlan]] = {}
    for repo_id in sorted(plans):
        plan = plans[repo_id]
        adapter = ecosystems.for_ecosystem(plan.unit.ecosystem)
        if adapter.uses_gazelle:
            groups.setdefault(adapter.name, []).append(plan)
    cache = (settings.root / settings.config.run.cache_dir).resolve() / "gazelle" / run_id
    files: dict[str, tuple[SupportFile, ...]] = {}
    failures: dict[str, BuildStepUnavailableError] = {}
    for name in sorted(groups):
        group = groups[name]
        try:
            captured = await _run_gazelle(
                group, binary=settings.config.build.gazelle_binary, scratch=cache / name
            )
        except (BuildFileGenerationError, OSError) as exc:
            failure = (
                exc
                if isinstance(exc, BuildFileGenerationError)
                else BuildFileGenerationError(
                    f"{', '.join(sorted(plan.repo_id for plan in group))}: the scratch tree the "
                    f"BUILD-file generator runs over could not be assembled: {exc}"
                )
            )
            for plan in group:
                failures[plan.repo_id] = failure
            continue
        files.update(captured)
    return files, failures


async def _fleet_support_files(
    plans: Mapping[str, _BuildPlan],
    *,
    settings: FleetSettings,
    run_id: str,
) -> tuple[dict[str, tuple[SupportFile, ...]], dict[str, BuildStepUnavailableError]]:
    """Every prepared repo's monorepo-ROOT files, computed ONCE PER ECOSYSTEM (ADR-0048).

    Returns `(repo_id → files, repo_id → the failure that cost it its files)`. The grouping key
    is the adapter, because that is the thing whose `resolution()` and `workspace_files()` are
    fleet-wide: `npm_translate_lock` builds one `@npm` hub from one `//:pnpm-lock.yaml` for every
    JS repo in the monorepo, and `pip.parse` one `@pypi` from one `//:requirements.lock`.

    Computed over the RUN's whole domain — every eligible unit, ingested and planned before the
    first build (ADR-0055) — and computed exactly ONCE per run. That is what makes the result a
    fleet-wide fact rather than a per-wave one: a JS repo in wave 3 adds an importer, so wave 0's
    repos owe the *same* root files, and computing them per wave meant wave 0 built against files
    wave 3 then replaced while nothing re-admits a settled wave. Every plan of one ecosystem is
    handed the identical tuple, so `_module_inputs` has nothing left to decide.

    A resolver failure is attributed to **every repo of that ecosystem**, because the resolve is
    theirs jointly: with one lock for the fleet there is no per-repo resolution left to succeed,
    and reporting it against one repo would leave the others building against a lock that was
    never produced (Rule 11).

    **An adapter that refuses to RENDER a coordinate is contained here too, by the same rule and
    into the same per-ecosystem bucket.** `ecosystems.AdapterCoordinateError` is raised out of
    `workspace_files()`/`resolution()` by an adapter whose root file is a grammar rather than a
    list — and until it was caught here it propagated out of `build` and ended the run:
    one repo's malformed coordinate cost every OTHER ecosystem's repos their build, which is the
    opposite of Rule 11's "mark the target repo and move to the next item". It is attributed to
    the whole group for the resolver's own reason: the root file is one file for the ecosystem,
    so a render that fails fails for all of them at once. It is caught by its NEUTRAL base and
    re-raised as `CoordinateRenderError` rather than passed through, so that the driver names no
    ecosystem's exception (§12.6) and so the repos it was rendering for appear in the message
    beside the coordinate the adapter named.
    """
    groups: dict[str, list[_BuildPlan]] = {}
    for repo_id in sorted(plans):
        groups.setdefault(plans[repo_id].adapter_name, []).append(plans[repo_id])
    cache = (settings.root / settings.config.run.cache_dir).resolve() / "resolve" / run_id
    files: dict[str, tuple[SupportFile, ...]] = {}
    failures: dict[str, BuildStepUnavailableError] = {}
    for name in sorted(groups):
        group = groups[name]
        adapter = ecosystems.for_ecosystem(group[0].unit.ecosystem)
        members = ", ".join(plan.repo_id for plan in group)
        try:
            resolved = await _resolved_support_files(
                adapter,
                [plan.unit for plan in group],
                repo_id=members,
                worktrees=[plan.worktree for plan in group],
                scratch=cache / name,
            )
        except DependencyResolutionError as exc:
            for plan in group:
                failures[plan.repo_id] = exc
            continue
        except ecosystems.AdapterCoordinateError as exc:
            render = CoordinateRenderError(
                f"{members}: adapter {adapter.name!r} cannot write a coordinate these repos "
                f"declare into the monorepo-root files it names, so none of them can build "
                f"against that ecosystem's root file: {exc}"
            )
            render.__cause__ = exc
            for plan in group:
                failures[plan.repo_id] = render
            continue
        for plan in group:
            files[plan.repo_id] = resolved
    return files, failures


def _module_inputs(
    plans: Mapping[str, _BuildPlan],
) -> tuple[
    list[WorkspaceDep],
    list[ToolchainRequirement],
    list[ExternalRequirement],
    list[BuildTarget],
    list[SupportFile],
]:
    """The FLEET's `MODULE.bazel` inputs, unioned over the RUN's whole domain (ADR-0055).

    Fleet-wide and not per repo, for two reasons that are the same reason. §3.3 step 3's MVS is a
    reconciliation *across* repos — a per-repo render would select a version from one repo's
    specs and call it the monorepo's. And `MODULE.bazel` is ONE file at the monorepo root that
    every Phase 3 dispatch writes and merges: two repos in a wave rendering two different
    versions of it is a merge conflict on the integration branch, where two repos rendering the
    identical superset is a no-op. Deduplicated on identity and sorted, so the bytes do not
    depend on which repo happened to finish first (§11.6).

    Two of the five outputs are here for that same "one root file" reason and no other:

    * **`module_targets`** — a target's `load_from` is a claim on a ruleset that no `WorkspaceDep`
      and no `ToolchainRequirement` declares (D6: a `js_binary` in a package with no npm
      coordinates), so `render_module_bazel` needs the targets to derive the `bazel_dep` set. It
      needs only their `load_from`, so one representative target per distinct label is kept and
      the payload stays O(rulesets) rather than O(fleet).
    * **`workspace_files`** — the root files those tags NAME (D10), **plus the root
      `BUILD.bazel` that makes those names labels at all**. A `pip.parse` pointed at
      `//:requirements.lock` is in every dispatch's MODULE.bazel, so the lock has to be in every
      dispatch's tree, not only in the Python repo's; and `//:requirements.lock` does not resolve
      to a file that merely exists — real Bazel reports `Unable to load package for
      //:requirements.lock: BUILD file not found` until the monorepo root is a package. The root
      package is synthesized HERE, from the union, rather than by an adapter, because there is
      exactly one root `BUILD.bazel` and two adapters each declaring their own would collide on
      the `support` key below and silently drop one ecosystem's exports.
    """
    deps: dict[tuple[str, str, str, str], WorkspaceDep] = {}
    toolchains: dict[tuple[str, str, str, str], ToolchainRequirement] = {}
    requirements: dict[tuple[str, str, str], ExternalRequirement] = {}
    module_targets: dict[str, BuildTarget] = {}
    support: dict[str, SupportFile] = {}
    contributors: dict[str, list[str]] = {}
    root_targets: dict[tuple[str, str], BuildTarget] = {}
    for repo_id in sorted(plans):
        plan = plans[repo_id]
        for dep in plan.workspace_deps:
            deps.setdefault(
                (dep.ruleset, dep.extension, dep.repo_name, dep.coordinate.key), dep
            )
        for toolchain in plan.toolchains:
            toolchains.setdefault(
                (toolchain.ruleset, toolchain.extension, toolchain.name, toolchain.version),
                toolchain,
            )
        for requirement in plan.requirements:
            requirements.setdefault(
                (requirement.coord_key, requirement.repo_id, requirement.version_spec),
                requirement,
            )
        for target in plan.targets:
            if target.load_from is not None:
                module_targets.setdefault(target.load_from, target)
        for file in plan.workspace_files:
            # NEVER a silent winner (Rule 11). This was `support.setdefault(file.path, file)` —
            # first repo_id wins — and that one line is the ADR-0048 defect: two repos of one
            # ecosystem each offering a root lock is a real ambiguity, and picking the lower
            # repo_id dropped the other repo's external dependency from the monorepo while
            # `fleet build` exited 0. Identical bytes still dedupe quietly, which is the normal
            # case now that `_fleet_support_files` computes these once per ecosystem and hands
            # every plan of it the identical tuple; divergence means the fleet-wide computation
            # did not happen or two adapters claim one root path, and both are defects that must
            # be seen rather than resolved by iteration order.
            existing = support.get(file.path)
            if existing is None:
                support[file.path] = file
                contributors[file.path] = [repo_id]
                continue
            contributors[file.path].append(repo_id)
            if existing != file:
                raise RootFileConflictError(file.path, contributors[file.path])
        for target in plan.root_targets:
            # Keyed by (name, rule) and NOT by repo: every JS repo in the fleet asks for the same
            # `npm_link_all_packages(name = "node_modules")`, and the root package must declare it
            # exactly once. Two adapters wanting the same target NAME with different rules is a
            # collision Bazel would report at the root, so it is kept visible in the key rather
            # than resolved by iteration order.
            root_targets.setdefault((target.name, target.rule), target)
    if support:
        support.setdefault(
            ROOT_PACKAGE_PATH,
            SupportFile(
                path=ROOT_PACKAGE_PATH,
                content=render_root_package(
                    sorted(support),
                    targets=[root_targets[key] for key in sorted(root_targets)],
                ),
            ),
        )
    return (
        [deps[key] for key in sorted(deps)],
        [toolchains[key] for key in sorted(toolchains)],
        [requirements[key] for key in sorted(requirements)],
        [module_targets[key] for key in sorted(module_targets)],
        [support[key] for key in sorted(support)],
    )


async def _check_root_file_domain(
    plans: Mapping[str, _BuildPlan],
    dispatched: Sequence[str],
    *,
    domain: Mapping[str, str],
) -> None:
    """The RUN's domain, asserted twice: the root files COVER it, and every worktree CONTAINS it.

    `domain` is `repo_id → dest` for the whole eligible fleet, derived in `_build_impl` from
    SQLite (`wave_members` ⋈ `phases`, then `layout()`) and **never** from `plans`. That
    independence is the point of the first half below, and it is what stops this check from being
    the tautology it used to be.

    **First half — coverage (ADR-0055).** `_fleet_support_files` and `_module_inputs` compute the
    monorepo-root files over `plans`, so `{repo_id: plan.dest}` IS the set those files describe.
    Comparing it to a domain SQLite derived independently makes "the root files describe the
    whole eligible fleet" a checked fact rather than a hope. It is what the previous, per-wave
    form could not state: `plans` is process-local and used to accumulate only across the waves
    ONE invocation drove, so a second `fleet build` over a partially complete run recomputed the
    root files over the unsettled waves ALONE and republished files describing a fraction of the
    branch — while this guard passed, because a domain read off `plans` shrank with it and a
    shrunken domain is trivially contained.

    **Second half — containment (ADR-0053).** Each dispatched worktree must hold every `dest` in
    that domain, because the root files in that same tree name them: `//:Cargo.toml`'s `members`,
    `//:pnpm-workspace.yaml`'s importers, `//:.bazelignore`'s lines. Ecosystem-neutral, offline,
    and before any lease, exactly as ADR-0053 fixed it.

    `dispatched` is the wave's members minus anything already abandoned — a repo whose worktree
    could not be re-cut for this wave is REQUIRES_HUMAN_INTERVENTION and will not be leased, so
    checking its (removed) worktree would fail the whole run over a repo nothing is going to
    build. It stays in `domain` and in `plans` regardless: its history is on the branch and
    earlier waves already published root files that name it.
    """
    covered = {repo_id: plans[repo_id].dest for repo_id in plans}
    if covered != dict(domain):
        raise RootFileDomainDriftError(expected=domain, covered=covered)
    dests = sorted(set(domain.values()))
    missing: dict[str, list[str]] = {}
    for repo_id in dispatched:
        plan = plans.get(repo_id)
        if plan is None:
            continue
        absent = [
            dest
            for dest in dests
            if not await asyncio.to_thread((plan.worktree / dest).is_dir)
        ]
        if absent:
            missing[repo_id] = absent
    if missing:
        raise RootFileDomainError(missing)


async def _note_finding(
    writer: StateWriter,
    run_id: str,
    repo_id: str | None,
    *,
    kind: str,
    payload: Mapping[str, object],
    severity: str,
    now: datetime,
) -> None:
    """One `findings` row, idempotent on its semantic identity (§11.7)."""
    row = (
        run_id,
        repo_id,
        kind,
        severity,
        _fingerprint(run_id, repo_id or "", kind),
        redact_text(json.dumps(dict(payload), sort_keys=True)),
        _iso(now),
    )

    async def unit(conn: aiosqlite.Connection) -> None:
        await conn.execute(
            "INSERT INTO findings (run_id, repo_id, kind, severity, fingerprint, payload, "
            "                      created_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (run_id, IFNULL(repo_id, ''), kind, fingerprint) "
            "DO UPDATE SET payload = excluded.payload, created_at = excluded.created_at",
            row,
        )

    await writer.submit(unit)


async def _monorepo_checkout(settings: FleetSettings) -> tuple[Git, Path, Path]:
    """`(git, path, lock_dir)` for the monorepo — §3.3's "the integration branch exists".

    The lock directory is the MAIN checkout's `--git-common-dir`, so the `flock` excludes writers
    arriving through any worktree of the same repository *and* a second `fleet` process, which is
    the case that matters when twenty Phase 3 tasks and a `fleet resume` overlap.
    """
    path = (settings.root / settings.config.run.monorepo_path).resolve()
    branch = settings.config.run.monorepo_branch
    if not await asyncio.to_thread((path / ".git").exists):
        raise MonorepoUnavailableError(
            f"no git repository at {path} (`run.monorepo_path`). §3.3's precondition is that the "
            f"monorepo exists with {branch!r} checked out; a merge has nowhere to land without "
            "it. Create it, or point `run.monorepo_path` at it."
        )
    git = Git(path)
    current = await git.current_branch()
    if current != branch:
        raise MonorepoUnavailableError(
            f"{path} has {current or 'a detached HEAD'} checked out, not {branch!r} "
            "(`run.monorepo_branch`). §3.3 step 1 merges into the integration branch and "
            "`ingest()` refuses to merge anywhere else: a merge made on another branch strands "
            "the merge commit off `integration` and no later phase can detect it."
        )
    common = await git.text(["rev-parse", "--path-format=absolute", "--git-common-dir"])
    return git, path, Path(common.strip())


async def _ingest_build_source(
    settings: FleetSettings,
    *,
    run_id: str,
    repo_id: str,
    facts: _RepoFacts,
    overrides: Mapping[Ecosystem, str],
    monorepo: Git,
    lock_dir: Path,
) -> _BuildIngest:
    """§3.3 step 1's INGEST for ONE repo, in the order the spec fixes it and before any lease.

    1. a **throwaway** clone of `migrate/<repo>` — `git-filter-repo` rewrites every commit and
       drops the origin remote, so pointing it at the Phase 2 worktree would destroy it;
    2. the relocation plan applied to *history* by `git-filter-repo`;
    3. `git merge --allow-unrelated-histories` into the integration branch with the ADR-0011
       `Source-Repo:`/`Source-Sha:` trailers, **under `IntegrationMutex`** — the single-writer
       merge queue, unchanged: this repo's merge is still serialized against every other.

    What is NOT here any more is the snapshot the build reads. `ingest()` still cuts one inside
    the lock (its own contract, and the ref it names is still immutable), but the driver no
    longer builds from it: `_build_impl` cuts ONE snapshot after the RUN's last ingest and every
    unit of the fleet is planned from that. Snapshotting per repo made a build worktree hold the
    merges that landed before *its own*, while the fleet-wide root files
    (`_fleet_support_files`) are computed over every plan prepared so far — so the first Rust
    repo of a wave got a root `Cargo.toml` whose `members` named a sibling directory that did not
    exist yet, and cargo fails the WHOLE workspace on a member it cannot read. The per-repo cut
    point was never a recorded decision; the immutability of what is cut, and the mutex, are.

    Raises `BuildStepUnavailableError` for a repo this host cannot ingest — which is where
    `git-filter-repo`'s absence lands, as a named per-repo failure rather than a run-wide stop.
    """
    source_worktree = (settings.root / settings.config.run.work_dir).resolve() / repo_id
    branch = f"migrate/{repo_id}"
    if not await asyncio.to_thread((source_worktree / ".git").exists):
        raise BuildStepUnavailableError(
            f"{repo_id}: no Phase 2 worktree at {source_worktree}. §3.3's input IS the "
            "transformed tree; run `fleet transform` for this repo before building it."
        )
    try:
        dest = _dest_for(repo_id, facts, overrides)
    except (ReservedDestError, ValueError) as exc:
        # `layout()` is TOTAL over nodes, so the only way it declines is a destination it must
        # not return: `_scc/` (§3.1 6e's coarsening namespace, which the step-8 audit owns) or a
        # `dest:` that escapes the monorepo root. Both are per-repo facts, so both land here.
        raise BuildStepUnavailableError(f"{repo_id}: {exc}") from exc
    source = Git(source_worktree)
    tip = await source.resolve(branch)
    if tip is None:
        raise BuildStepUnavailableError(
            f"{repo_id}: {branch} does not exist in {source_worktree}; §3.3 ingests the branch "
            "Phase 2 wrote, and there is nothing to merge without it."
        )

    clone_dir = (
        (settings.root / settings.config.run.cache_dir).resolve() / "ingest" / run_id / repo_id
    )
    if await asyncio.to_thread(clone_dir.exists):
        await asyncio.to_thread(shutil.rmtree, clone_dir)
    await asyncio.to_thread(clone_dir.parent.mkdir, parents=True, exist_ok=True)
    await Git(clone_dir.parent).exec(
        [
            "clone",
            "--no-local",
            "--single-branch",
            "--branch",
            branch,
            str(source_worktree),
            str(clone_dir),
        ],
        timeout_s=1800.0,
    )

    # §3.3 step 1: the relocation plan applied to HISTORY — and applied as a *mapping*, which
    # means it has to be idempotent on a path that already sits at its image.
    #
    # The clone above is of `migrate/<repo>`, and §3.2 step 1 already moved the WORKTREE into
    # `<dest>/` by committing renames (`workers/relocate.py`). So this history is mixed: every
    # commit behind Phase 2's relocation commit still carries repo-root paths, and the tip
    # already carries `<dest>/…`. `--path-rename ':<dest>/'` alone re-roots BOTH, and the tip
    # lands at `<dest>/<dest>/…` — defect D3, observed as
    # `py/acme_lib_py/py/acme_lib_py/pyproject.toml`. The phase split is not the bug and is not
    # being changed here: §3.2 owns the tree, §3.3 step 1 owns the history, which is why
    # dropping the rename entirely is wrong too — it would leave every commit behind the tip at
    # its pre-migration path and lose the `git log --follow` continuity §3.3 rewrites history to
    # get (`tests/test_vcs.py::test_relocate_rewrites_history_into_the_monorepo_path`).
    #
    # `git-filter-repo` applies `--path-rename` rules to each path in the order given, so the
    # pair below is the mapping's idempotent form: rule 1 roots every historical path at
    # `<dest>/`, rule 2 collapses the one prefix that was already rooted there. Rule 2 cannot
    # fire on a path that merely *looks* relocated, because Phase 2 refuses to run at all when a
    # tracked source already sits under `<dest>/` (`relocate._plan_matches_tree`) — so inside
    # this clone, `<dest>/` is Phase 2's work and nothing else. Phase 2's rename commit maps to
    # `<dest>/x → <dest>/x`, becomes empty, and is pruned; the result is one uniformly relocated
    # history whose tip is exactly the tree Phase 2 produced.
    await relocate(
        clone_dir,
        RelocationSpec(
            dest_path=dest,
            extra_args=("--path-rename", f"{dest}/{dest}/:{dest}/"),
            replace_text=resolve_replace_text(
                settings.root, settings.config.redaction.history_scrub_file
            ),
        ),
        runner=FILTER_REPO_RUNNER or proc_run,
    )

    result = await ingest(
        monorepo,
        source_dir=clone_dir,
        source=SourceProvenance(repo_id=repo_id, sha=tip),
        run_id=run_id,
        integration_branch=settings.config.run.monorepo_branch,
        mutex=IntegrationMutex(lock_dir, run_id),
    )
    return _BuildIngest(
        repo_id=repo_id,
        dest=dest,
        merge_sha=result.merge_sha,
        source_sha=tip,
        already_ingested=result.already_present,
    )


async def _wave_snapshot(
    settings: FleetSettings, *, run_id: str, monorepo: Git, lock_dir: Path
) -> SnapshotRef:
    """The ONE immutable `refs/fleet/<run_id>/integration/<seq>` a whole Phase 3 wave builds from.

    Cut after the RUN's last ingest — and again after any wave that published, so the next wave's
    worktrees carry its packages — and always **under the same writer mutex** every merge is taken
    under, which is what makes it immutable in the sense §3.3 step 1 requires: a merge landing
    mid-build cannot be in it, because it cannot land while this lock is held and the ref never
    moves afterwards. Written exactly the way `_prepare_verify` cuts Phase 4's — same helper,
    same lock, same branch tip — because "one snapshot for the units this phase is about to
    dispatch" is the same operation in both phases.

    A distinct ref from every snapshot `ingest()` cut on the way here (the sequence number is
    derived from the refs git holds), so Phase 3's and Phase 4's refs stay disjoint and an
    `attempts.integration_ref` still names exactly one tree.
    """
    async with IntegrationMutex(lock_dir, run_id):
        return await integration_snapshot(
            monorepo, run_id, tip=settings.config.run.monorepo_branch
        )


async def _plan_build(
    *,
    repo_id: str,
    ingested: _BuildIngest,
    snapshot: SnapshotRef,
    facts: _RepoFacts,
    internal_deps: Sequence[InternalDep],
    manifest_paths: Sequence[str],
    owned_keys: frozenset[str],
    monorepo: Git,
    build_root: Path,
) -> _BuildPlan:
    """§3.3 step 1's step 4 and step 2 for ONE repo, over a snapshot the DRIVER chose.

    The worktree is cut from `snapshot.ref` — one ref shared by every unit planned together — and
    not from the branch and not from this repo's own merge. Everything below it (`_dest_sources`,
    the external coordinates, the adapter's targets and package files) is read off that worktree, so
    the tree the targets describe is the tree the build will actually see, exactly as before.
    """
    if PLAN_BUILD_HOOK is not None:
        PLAN_BUILD_HOOK(repo_id)
    dest = ingested.dest
    worktree = build_root / repo_id
    if await asyncio.to_thread(worktree.exists):
        await monorepo.exec(["worktree", "remove", "--force", str(worktree)], check=False)
        if await asyncio.to_thread(worktree.exists):
            await asyncio.to_thread(shutil.rmtree, worktree)
    await monorepo.exec(["worktree", "prune"])
    await asyncio.to_thread(build_root.mkdir, parents=True, exist_ok=True)
    await monorepo.exec(["worktree", "add", "--detach", "--force", str(worktree), snapshot.ref])

    srcs = await _dest_sources(worktree, dest)
    external = await asyncio.to_thread(
        _external_coordinates, worktree / dest, manifest_paths, owned_keys
    )
    unit = BuildUnit(
        unit_id=repo_id,
        ecosystem=facts.ecosystem,
        dest=dest,
        srcs=list(srcs),
        published=facts.published,
        internal_deps=list(internal_deps),
        # Deduplicated to one row per coordinate: `workspace_deps()` renders one tag call each,
        # and two rows for one artifact would emit it twice. Every distinct SPEC still reaches
        # MVS below, because §3.3 step 3 reconciles over the specs and dropping the losing bound
        # here is exactly how a conflict becomes a silent downgrade.
        external_coordinates=list({c.key: c for c in external}.values()),
    )
    # §3.3 step 2, as the spec writes it and with no branch of its own: the registry is total, so
    # `for_ecosystem` always answers and the driver never asks which language it got. Everything
    # per-language — the rule names, the `load()` labels, the `bazel_dep`/`use_extension` dialect,
    # the toolchain tags, whether emission is delegated to Gazelle — is read off the adapter.
    adapter = ecosystems.for_ecosystem(unit.ecosystem)
    targets = [*adapter.generate_targets(unit), *adapter.test_targets(unit)]
    # §3.3 step 2's monorepo-ROOT files are deliberately NOT computed here: they are fleet-wide,
    # so `_fleet_support_files` fills them in once per ecosystem after the wave's plans exist
    # (ADR-0048). A per-repo resolve would answer "what is at `//:pnpm-lock.yaml`" from inside
    # one repo, which is a question only the whole ecosystem can answer.
    package_files = await asyncio.to_thread(
        _resolve_support_files, worktree, adapter.package_files(unit)
    )
    return _BuildPlan(
        repo_id=repo_id,
        dest=dest,
        worktree=worktree,
        integration_ref=snapshot.ref,
        integration_sha=snapshot.sha,
        merge_sha=ingested.merge_sha,
        source_sha=ingested.source_sha,
        already_ingested=ingested.already_ingested,
        unit=unit,
        targets=tuple(targets),
        workspace_deps=tuple(adapter.workspace_deps(unit)),
        toolchains=tuple(adapter.toolchain_requirements()),
        workspace_files=(),
        package_files=package_files,
        # `gazelle_files` is left at its default here, for the reason `workspace_files` is left
        # empty: `_run_gazelle` fills it in in PASS 4, because the generator runs ONCE for the
        # whole fleet (a cross-repo import resolves to a real label only when every Go root is on
        # one command line) and a per-repo plan cannot hold a fleet-wide fact.
        root_targets=tuple(adapter.root_targets(unit)),
        baseline_test_count=facts.baseline_test_count,
        requirements=tuple(
            ExternalRequirement(
                coord_key=coordinate.key,
                repo_id=repo_id,
                version_spec=coordinate.version_spec,
            )
            for coordinate in external
            if coordinate.version_spec
        ),
        gazelle=adapter.gazelle_config(unit),
        adapter_name=adapter.name,
        adapter_degraded=adapter.degraded,
    )


async def _prepare_verify(
    settings: FleetSettings,
    *,
    run_id: str,
    repo_id: str,
    dest: str,
    monorepo: Git,
    lock_dir: Path,
    verify_root: Path,
) -> _VerifyPlan:
    """§3.4 step 1's "current integration branch tip", read the way §3.3 step 1 says to read it.

    A **fresh** snapshot ref taken at this task's start, under the same mutex — current, and
    still immutable for the build's duration. The branch name itself never reaches a worktree:
    dependencies merged mid-build are exactly what the snapshot exists to exclude.
    """
    async with IntegrationMutex(lock_dir, run_id):
        snapshot = await integration_snapshot(
            monorepo, run_id, tip=settings.config.run.monorepo_branch
        )
    worktree = verify_root / repo_id
    if await asyncio.to_thread(worktree.exists):
        await monorepo.exec(["worktree", "remove", "--force", str(worktree)], check=False)
        if await asyncio.to_thread(worktree.exists):
            await asyncio.to_thread(shutil.rmtree, worktree)
    await monorepo.exec(["worktree", "prune"])
    await asyncio.to_thread(verify_root.mkdir, parents=True, exist_ok=True)
    await monorepo.exec(["worktree", "add", "--detach", "--force", str(worktree), snapshot.ref])
    return _VerifyPlan(
        repo_id=repo_id,
        dest=dest,
        worktree=worktree,
        integration_ref=snapshot.ref,
        integration_sha=snapshot.sha,
    )


async def _dest_sources(worktree: Path, dest: str) -> tuple[str, ...]:
    """The unit's sources, dest-relative and sorted, read off the merged tree.

    Read from the worktree cut from the snapshot rather than from the Phase 2 branch, because the
    tree the targets describe has to be the tree the build will actually see.
    """
    out = await _git_output(worktree, ["ls-files", "-z", "--", dest])
    prefix = f"{dest}/"
    return tuple(
        sorted(path[len(prefix) :] for path in _nul_fields(out) if path.startswith(prefix))
    )


def _phase_config(settings: FleetSettings, phase: Phase, timeout_s: int) -> FleetConfig:
    """`--timeout` as the value it claims to be: the phase's `asyncio.timeout` ceiling.

    A config copy rather than a second deadline parameter, because `RunContext.deadline_for` is
    the ONE place a phase's wall clock is derived (§11.1) and a per-call override beside it is a
    second answer to the same question — the shape of defect that leaves a subprocess outliving
    the wave that would reap it.
    """
    wall = settings.config.budgets.task_max_wallclock_s
    key = "build" if phase is Phase.BUILD else "verify"
    if int(getattr(wall, key)) == timeout_s:
        return settings.config
    budgets = settings.config.budgets.model_copy(
        update={"task_max_wallclock_s": wall.model_copy(update={key: timeout_s})}
    )
    return settings.config.model_copy(update={"budgets": budgets})


def _cache_mounts(settings: FleetSettings) -> list[CacheMount]:
    """`verify.disk_cache`/`verify.repository_cache`, created and absolute (§3.4 bounds table).

    Mounted READ-WRITE and shared across containers and attempts: the ADR-0010 read-only mount
    applies to the *toolchain* cache, and a re-run after a crash re-executing everything is the
    cost explosion the bound exists to prevent.

    Each is returned ROLE-TAGGED rather than as a bare path, because the consumer does two things
    with it — bind-mount it and emit its `--disk_cache=`/`--repository_cache=` flag — and the two
    flags are not interchangeable. Conveying "the first one is the disk cache" by list order was
    what made emitting the flags impossible to do safely.
    """
    mounts: list[CacheMount] = []
    roles: tuple[tuple[Literal["disk", "repository"], str], ...] = (
        ("disk", settings.config.verify.disk_cache),
        ("repository", settings.config.verify.repository_cache),
    )
    for role, relative in roles:
        path = (settings.root / relative).resolve()
        path.mkdir(parents=True, exist_ok=True)
        mounts.append(CacheMount(role=role, path=str(path)))
    return mounts


def _build_payloads(
    settings: FleetSettings,
    plans: Mapping[str, _BuildPlan],
    *,
    monorepo: Path,
    lock_dir: Path,
    sandboxed: bool,
) -> PayloadFactory[BuildInput]:
    """One repo's Phase 3 dispatch payload, built from its plan. Injected (Guardrail 3)."""
    log_dir = str((settings.root / "artifacts/logs").resolve())
    mounts = _cache_mounts(settings)

    async def build(
        *, repo_id: str, phase: Phase, attempt: int, remaining_units: Sequence[str] | None
    ) -> BuildInput:
        _ = (phase, attempt)
        plan = plans[repo_id]
        # Computed at DISPATCH time and not at prepare time: by now every unit of the run's
        # domain has been prepared, so every dispatch renders the identical MODULE.bazel and the
        # second one to publish merges instead of conflicting.
        deps, toolchains, requirements, module_targets, root_files = _module_inputs(plans)
        return BuildInput(
            repo_id=repo_id,
            dest=plan.dest,
            unit=plan.unit,
            targets=list(plan.targets),
            module_targets=module_targets,
            gazelle=plan.gazelle,
            workspace_deps=deps,
            toolchains=toolchains,
            # Root files are the fleet's (every dispatch's MODULE.bazel names all of them);
            # package files are this repo's, because they live inside its own `dest`. The
            # generated BUILD files are package files too — same slot, same worker loop — and
            # they come LAST so that `<dest>/BUILD.bazel`, if the generator rewrote it, is the
            # captured text rather than the directives-only render: `buildgen` writes the render
            # first and `materialize` overwrites from this list immediately afterwards, which is
            # what makes GENERATE's unconditional re-render harmless on every re-entry.
            support_files=[*root_files, *plan.package_files, *plan.gazelle_files],
            baseline_test_count=plan.baseline_test_count,
            requirements=requirements,
            ruleset_versions=dict(settings.config.build.ruleset_versions),
            integration_ref=plan.integration_ref,
            integration_branch=settings.config.run.monorepo_branch,
            integration_worktree=str(monorepo),
            lock_dir=str(lock_dir),
            log_dir=log_dir,
            # The one place the run's registry becomes a value rather than an absence. `None`
            # means Bazel's built-in address, which is `BCR_DEFAULT_REGISTRY` and is named THERE
            # and only there — `bazel/lockfile.py` takes the registry as a parameter precisely so
            # that the default does not get a second spelling inside a pure driver.
            module_registry=settings.config.build.registry or BCR_DEFAULT_REGISTRY,
            image=settings.config.verify.container_image if sandboxed else None,
            container_memory=settings.config.verify.container_memory,
            container_cpus=settings.config.verify.container_cpus,
            container_network=settings.config.verify.network,
            cache_mounts=mounts,
            min_free_bytes=settings.config.preflight.min_free_bytes,
            remaining_units=None if remaining_units is None else tuple(remaining_units),
        )

    return build


def _verify_payloads(
    settings: FleetSettings,
    plans: Mapping[str, _VerifyPlan],
    *,
    sandboxed: bool,
    rdeps_limit: int,
    rdeps_sample_n: int,
    affected_only: bool,
) -> PayloadFactory[VerifyInput]:
    log_dir = str((settings.root / "artifacts/logs").resolve())
    mounts = _cache_mounts(settings)

    async def build(
        *, repo_id: str, phase: Phase, attempt: int, remaining_units: Sequence[str] | None
    ) -> VerifyInput:
        _ = (phase, attempt)
        plan = plans[repo_id]
        return VerifyInput(
            repo_id=repo_id,
            dest=plan.dest,
            integration_ref=plan.integration_ref,
            log_dir=log_dir,
            image=settings.config.verify.container_image if sandboxed else None,
            container_memory=settings.config.verify.container_memory,
            container_cpus=settings.config.verify.container_cpus,
            container_network=settings.config.verify.network,
            cache_mounts=mounts,
            min_free_bytes=settings.config.preflight.min_free_bytes,
            affected_only=affected_only,
            rdeps_limit=rdeps_limit,
            rdeps_sample_n=rdeps_sample_n,
            remaining_units=None if remaining_units is None else tuple(remaining_units),
        )

    return build


async def _run_build_wave(
    settings: FleetSettings,
    *,
    config: FleetConfig,
    repository: SqliteStateRepository,
    writer: StateWriter,
    read_conn: aiosqlite.Connection,
    db_path: Path,
    run_id: str,
    wave_index: int,
    plans: Mapping[str, _BuildPlan],
    members: Sequence[str],
    monorepo: Path,
    lock_dir: Path,
    build_root: Path,
    sandboxed: bool,
    pool: ProcessPoolExecutor,
    evidence: _BuildEvidence,
) -> WaveReport:
    """Compose the run and drive ONE Phase 3 wave. The composition root, and nothing else."""
    ledger = CostLedger(
        repository,
        run_id=run_id,
        ceilings=Ceilings.from_settings(config.budgets, config.stubs),
        clock=_now,
    )
    projector = Projector(db_path, run_id=UUID(run_id), path=DEFAULT_PROJECTION_PATH)
    ctx = RunContext(
        run_id=UUID(run_id),
        config=config,
        writer=writer,
        repository=repository,
        read_conn=read_conn,
        ledger=ledger,
        limits=Limits.create(config.concurrency, ledger=ledger, cpu_pool=pool),
        llm=llm_router(settings),
        log=default_logger("fleet.build"),
        # `ctx.workdir` is `work_dir / repo_id`, and for Phase 3 that must be the worktree cut
        # from this repo's snapshot ref — not the Phase 1/2 checkout of the source repo.
        work_dir=build_root,
        projector=projector,
        clock=_now,
        harness_version=HARNESS_VERSION,
    )
    scheduler = WaveScheduler(
        run_id=run_id,
        phase=Phase.BUILD,
        store=_ScopedWaveStore(
            SqliteSchedulerStore(writer=writer, read_conn=read_conn), members
        ),
        db=repository,
        budgets=config.budgets,
        clock=_now,
        descendants=ordering_descendants(await _ordering_pairs(read_conn, settings, run_id)),
    )
    runner = PhaseRunner(
        ctx,
        BuildPipelineWorker(bazel_runner=BAZEL_RUNNER),
        scheduler,
        payloads=_build_payloads(
            settings, plans, monorepo=monorepo, lock_dir=lock_dir, sandboxed=sandboxed
        ),
        sink=_BuildSink(
            attempts=_AttemptWriter(
                writer=writer, repository=repository, read_conn=read_conn, run_id=run_id
            ),
            writer=writer,
            run_id=run_id,
            evidence=evidence,
        ),
    )
    try:
        await projector.start()
        return await runner.run_wave(wave_index)
    finally:
        await _close_wave_projector(projector, ctx)


async def _run_verify_wave(
    settings: FleetSettings,
    *,
    config: FleetConfig,
    repository: SqliteStateRepository,
    writer: StateWriter,
    read_conn: aiosqlite.Connection,
    db_path: Path,
    run_id: str,
    wave_index: int,
    plans: Mapping[str, _VerifyPlan],
    members: Sequence[str],
    verify_root: Path,
    sandboxed: bool,
    rdeps_limit: int,
    rdeps_sample_n: int,
    affected_only: bool,
    pool: ProcessPoolExecutor,
    evidence: _VerifyEvidence,
) -> WaveReport:
    """Compose the run and drive ONE Phase 4 wave."""
    ledger = CostLedger(
        repository,
        run_id=run_id,
        ceilings=Ceilings.from_settings(config.budgets, config.stubs),
        clock=_now,
    )
    projector = Projector(db_path, run_id=UUID(run_id), path=DEFAULT_PROJECTION_PATH)
    ctx = RunContext(
        run_id=UUID(run_id),
        config=config,
        writer=writer,
        repository=repository,
        read_conn=read_conn,
        ledger=ledger,
        limits=Limits.create(config.concurrency, ledger=ledger, cpu_pool=pool),
        llm=llm_router(settings),
        log=default_logger("fleet.verify"),
        work_dir=verify_root,
        projector=projector,
        clock=_now,
        harness_version=HARNESS_VERSION,
    )
    scheduler = WaveScheduler(
        run_id=run_id,
        phase=Phase.VERIFY,
        store=_ScopedWaveStore(
            SqliteSchedulerStore(writer=writer, read_conn=read_conn), members
        ),
        db=repository,
        budgets=config.budgets,
        clock=_now,
        descendants=ordering_descendants(await _ordering_pairs(read_conn, settings, run_id)),
    )
    runner = PhaseRunner(
        ctx,
        VerifyPipelineWorker(bazel_runner=BAZEL_RUNNER),
        scheduler,
        payloads=_verify_payloads(
            settings,
            plans,
            sandboxed=sandboxed,
            rdeps_limit=rdeps_limit,
            rdeps_sample_n=rdeps_sample_n,
            affected_only=affected_only,
        ),
        sink=_VerifySink(
            attempts=_AttemptWriter(
                writer=writer, repository=repository, read_conn=read_conn, run_id=run_id
            ),
            evidence=evidence,
            writer=writer,
            run_id=run_id,
        ),
    )
    try:
        await projector.start()
        return await runner.run_wave(wave_index)
    finally:
        await _close_wave_projector(projector, ctx)


async def _gated_members(
    conn: aiosqlite.Connection,
    run_id: str,
    wave_index: int,
    only: str | None,
    *,
    predecessor: Phase,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """`(admitted, withheld)` — the wave's members split by the PREVIOUS phase's verdict.

    THE wave gate, and it is a driver decision on purpose. §3.3 and §3.4 both open with
    "<previous phase> SUCCEEDED", and a repo that has not met it must get **no phase row at
    all**: `WaveScheduler.admit` reads `phases.status`, a missing row is `PENDING`, and a
    `PENDING` member holds the wave `OPEN` forever over work this phase is not allowed to do.
    Withholding it here — rather than letting the worker refuse later — also means no attempt is
    consumed and no lease is taken for a repo that was never eligible.
    """
    members = await _wave_repos(conn, run_id, wave_index, only)
    rows = await _rows(
        conn,
        "SELECT repo_id FROM phases WHERE run_id = ? AND phase = ? AND status = 'SUCCEEDED'",
        (run_id, int(predecessor)),
    )
    ready = {str(row[0]) for row in rows}
    return (
        tuple(repo for repo in members if repo in ready),
        tuple(repo for repo in members if repo not in ready),
    )


async def _eligible_build_units(
    conn: aiosqlite.Connection, run_id: str
) -> tuple[str, ...]:
    """The RUN's Phase 3 domain: every repo this run may ingest, in wave order (ADR-0055).

    `_gated_members` unioned over EVERY wave with no `only` glob and no `--wave` — deliberately
    so. This is the set the fleet-wide monorepo-root files are computed over, and §3.3 step 2
    makes that a property of the whole fleet: one `MODULE.bazel`, one `pnpm-workspace.yaml`, one
    `Cargo.toml` at the root, each describing every unit in the monorepo. A domain that moved
    with the invocation's flags would republish root files describing a fraction of the branch —
    which is exactly what a second `fleet build` over a partially complete run used to do, since
    `_open_phase_waves` drops every wave whose members are all settled.

    So `--wave`/`--repo` are **dispatch filters and nothing else**: they narrow which repos are
    leased and built, never which units the root files describe.

    Settled repos are IN. Their merges are already on the branch and their destinations are
    already named by the published root files; leaving them out is the shrink. Re-ingesting one
    is a no-op by ADR-0011's trailer pair (`already_present`), and it is what gives this run a
    tree to read their manifests from — `external_coordinates` is not in §6, it is re-parsed from
    the merged worktree by `_external_coordinates`.

    Gated on TRANSFORM `SUCCEEDED` for the reason `_gated_members` is: §3.3 opens with it, and a
    repo that never transformed has no `migrate/<repo>` branch to ingest.
    """
    rows = await _rows(
        conn,
        "SELECT m.node_id FROM wave_members m "
        "  JOIN phases p ON p.run_id = m.run_id AND p.repo_id = m.node_id AND p.phase = ? "
        " WHERE m.run_id = ? AND m.node_kind = 'REPO' AND p.status = 'SUCCEEDED' "
        " ORDER BY m.wave_index, m.node_id",
        (int(Phase.TRANSFORM), run_id),
    )
    return tuple(str(row[0]) for row in rows)


async def _build_impl(
    opts: GlobalOptions,
    settings: FleetSettings,
    path: Path,
    *,
    run_id: str,
    wave: int | None,
    only: str | None,
    timeout_s: int,
    sandboxed: bool,
) -> dict[str, object]:
    """Phase 3 over one fleet: **ingest the whole eligible fleet, then build wave by wave**.

    Waves are driven in ascending order and the loop stops at the first halt: `WaveScheduler`
    refuses to open wave N+1 while wave N is not `CLOSED`, which is the ordering guarantee the
    topological sequencer bought and the reason a dependency's merge is on the branch before its
    dependent is ingested (§3.1 step 7). The ingest pass below walks the eligible fleet in that
    same `(wave_index, repo_id)` order, so that guarantee is unchanged.

    **The shape, and why it is this shape (ADR-0055).** Three run-level passes precede the wave
    loop, and the wave loop then does dispatch and nothing else:

    1. **INGEST** every eligible repo — `_eligible_build_units`, which is DB-derived and ignores
       `--wave`/`--repo` entirely.
    2. **PLAN** every one of them from **ONE** snapshot cut after the last of those ingests. Every
       worktree therefore contains every `dest` in the fleet, which is ADR-0053's intra-wave
       property widened to the whole run.
    3. **RESOLVE** §3.3 step 2's fleet-wide monorepo-root files **once**, over that whole set.

    `plans` used to be filled inside the wave loop and the root files recomputed per wave over
    "every plan prepared so far". Two things followed, and both are closed here. *Cross-wave
    staleness*: wave 0 built against root files that wave 1 replaced, and a settled wave is never
    re-admitted (ADR-0053 consequence 2). *And the worse one*: `plans` is process-local and was
    never rehydrated from SQLite, while `_open_phase_waves` drops every wave whose members are
    all settled — so a SECOND `fleet build` over a partially complete run computed the root files
    over the unsettled waves alone and republished a `MODULE.bazel` missing whole ecosystems,
    exiting 0 both times. Re-invoking `fleet build` was the only way to continue a partial run
    when this was written, so that was the normal operator path.

    **CORRECTED 2026-08-25 (round G, ADR-0080).** The premise this paragraph gave for that —
    *"`fleet resume` reconciles the ledger and then refuses to continue with exit 2 (§11.5 step 8
    has no implementation)"* — is false now: step 8 is wired, `fleet resume` continues, and it
    delegates to `_build_impl` rather than to this command. **The defect above is unaffected and
    the fix below still holds**; a resume-driven continuation enters `_build_impl` on a partial
    run exactly as a second `fleet build` did, so the DB-derived domain is what makes the root
    files right either way. Only the sentence naming why an operator ended up here changed.

    Hoisting the ingest is what makes the domain expressible: the units the root files must cover
    are exactly the eligible fleet, that set is known from SQLite before anything builds, and it
    does not move for the rest of the run. `_check_root_file_domain` then checks the root files
    against that DB-derived domain rather than against the dict they were rendered from.

    **The one thing still cut per wave is the snapshot the wave's members BUILD from.** After a
    wave publishes, the next wave's members are re-planned from a fresh snapshot, because §3.4
    and the internal-dependency labels need a dependency's generated `BUILD.bazel` to be on the
    branch before its dependents' worktrees are cut. Re-planning changes a plan's worktree and
    ref; it cannot change its `unit`, because a publish only ever adds files under the publishing
    repo's own `dest` and at the root. The resolved root files are carried across the re-plan
    verbatim, so the bytes every dispatch writes stay a function of the run's domain (§11.6).

    **What this deliberately does not change.** Each merge is still taken alone under
    `IntegrationMutex`; every snapshot is still cut inside that lock and never moves (SPEC §3.3);
    Phase 3's refs are still disjoint from Phase 4's; the wave gate, the wave order and "a settled
    wave is never re-admitted" are untouched. And a repo's history reaching `integration` before
    its build is judged is pre-existing: nothing in `src/` un-merges a repo whose build later
    fails, so what widened is *when* the merge happens, not *whether* an unbuilt repo can be on
    the branch.
    """
    log_configure(level=opts.log_level)
    config = _phase_config(settings, Phase.BUILD, timeout_s)
    evidence = _BuildEvidence()
    plans: dict[str, _BuildPlan] = {}
    reports: list[WaveReport] = []
    driven: list[int] = []
    withheld: set[str] = set()
    degraded: set[str] = set()
    overrides = dict(settings.config.build.monorepo_dir_overrides)
    _validate_monorepo_dir_overrides(overrides)
    build_root = (settings.root / settings.config.run.work_dir).resolve() / "integration"
    monorepo, monorepo_path, lock_dir = await _monorepo_checkout(settings)
    pool = new_cpu_pool(settings.config.concurrency.cpu_pool_workers)
    try:
        async with StateWriter(path, owner="fleet-build") as writer:
            read_conn = await connect_ro(path)
            try:
                repository = SqliteStateRepository(writer=writer, read_conn=read_conn)
                facts = await _repo_facts(read_conn)
                internal_deps = await _unit_deps(
                    read_conn, settings, run_id, facts=facts, overrides=overrides
                )
                manifest_paths = await _manifest_paths(read_conn)
                owned_keys = await _owned_coordinate_keys(read_conn)
                waves = await _open_phase_waves(read_conn, run_id, Phase.BUILD, wave)
                await repository.reap_expired_phase_leases(run_id, now=_now())
                await repository.open_budget_ledger(
                    run_id, max_usd=settings.config.budgets.run_max_cost_usd, now=_now()
                )
                # ---- the RUN's domain, fixed here and not moved again (ADR-0055) ----------
                #
                # Skipped entirely when this invocation drives no wave: a fleet whose every wave
                # is settled must do NOTHING, which is the idempotency `fleet build` already had
                # and which a fleet-wide ingest would otherwise turn into a clone per repo.
                eligible = await _eligible_build_units(read_conn, run_id) if waves else ()
                # `repo_id -> dest`, derived from SQLite (`wave_members` ⋈ `phases`) and
                # `layout()` — INDEPENDENTLY of `plans`, which is the whole point: it is what
                # `_check_root_file_domain` compares the root files against. A repo `layout()`
                # refuses a destination for is left out here and fails its ingest below for the
                # same reason, so the two halves cannot disagree about it.
                domain: dict[str, str] = {}
                for repo_id in eligible:
                    await repository.upsert_phase(
                        run_id, repo_id, Phase.BUILD, now=_now(), max_attempts=MAX_ATTEMPTS
                    )
                    with suppress(ReservedDestError, ValueError):
                        domain[repo_id] = _dest_for(
                            repo_id,
                            facts.get(repo_id, _RepoFacts(None, Ecosystem.UNKNOWN)),
                            overrides,
                        )
                # ---- PASS 1: INGEST the whole eligible fleet, before the first build ------
                #
                # In `(wave_index, repo_id)` order, so §3.1 step 7's "a dependency's merge is on
                # the branch before its dependent is ingested" is exactly as it was. Each merge is
                # still taken alone under `IntegrationMutex`. A repo already merged is
                # `already_present` by its ADR-0011 trailer pair and merges nothing.
                ingests: dict[str, _BuildIngest] = {}
                for repo_id in eligible:
                    try:
                        ingests[repo_id] = await _ingest_build_source(
                            settings,
                            run_id=run_id,
                            repo_id=repo_id,
                            facts=facts.get(repo_id, _RepoFacts(None, Ecosystem.UNKNOWN)),
                            overrides=overrides,
                            monorepo=monorepo,
                            lock_dir=lock_dir,
                        )
                    except (
                        BuildStepUnavailableError,
                        FilterRepoUnavailableError,
                        IngestError,
                        GitError,
                        OSError,
                    ) as exc:
                        # OUT of the domain, not merely out of `plans`. A `dest` left in the
                        # domain by a repo that never merged is a root file naming a directory no
                        # worktree will ever have — the very defect ADR-0053 closed — and it is
                        # safe to drop precisely because nothing has been built or published yet:
                        # the domain is still fixed and still maximal for the rest of the run.
                        domain.pop(repo_id, None)
                        await _abandon_repo(
                            writer,
                            run_id,
                            repo_id,
                            detail=str(exc),
                            now=_now(),
                            phase=Phase.BUILD,
                            # Classified apart from every other preparation failure: a lock
                            # that could not be resolved is a fact about a package index or a
                            # spec conflict, not about this host's tooling, and it is the one
                            # failure whose silent form (an empty lock) builds a dependency
                            # hub with nothing in it and fails much later inside a ruleset.
                            kind=(
                                "DependencyResolutionFailed"
                                if isinstance(exc, DependencyResolutionError)
                                else "BuildPreparationFailed"
                            ),
                        )
                        continue
                # ---- PASS 2: ONE snapshot over the whole fleet, and a plan for every unit ----
                #
                # Cut after the LAST ingest of the run and under the same writer mutex, so every
                # worktree below holds every `dest` in the domain. Not cut at all when nothing was
                # ingested: a ref named after a run that merged nothing would name a stale tree.
                snapshot: SnapshotRef | None = None
                if ingests:
                    snapshot = await _wave_snapshot(
                        settings, run_id=run_id, monorepo=monorepo, lock_dir=lock_dir
                    )
                    for repo_id, ingested in ingests.items():
                        try:
                            plans[repo_id] = await _plan_build(
                                repo_id=repo_id,
                                ingested=ingested,
                                snapshot=snapshot,
                                facts=facts.get(repo_id, _RepoFacts(None, Ecosystem.UNKNOWN)),
                                internal_deps=internal_deps.get(repo_id, ()),
                                manifest_paths=manifest_paths.get(repo_id, ()),
                                owned_keys=owned_keys,
                                monorepo=monorepo,
                                build_root=build_root,
                            )
                        except (
                            BuildStepUnavailableError,
                            GitError,
                            OSError,
                        ) as exc:
                            domain.pop(repo_id, None)
                            await _abandon_repo(
                                writer,
                                run_id,
                                repo_id,
                                detail=str(exc),
                                now=_now(),
                                phase=Phase.BUILD,
                                kind=(
                                    "DependencyResolutionFailed"
                                    if isinstance(exc, DependencyResolutionError)
                                    else "BuildPreparationFailed"
                                ),
                            )
                            continue
                        plan = plans[repo_id]
                        if plan.adapter_degraded:
                            # The §3.1 step 2 floor, still reachable and still DISCLOSED. It is
                            # now a statement about ONE repo whose language nothing recognized,
                            # not about the harness: `adapter.degraded` is set by exactly one
                            # adapter, so this finding can no longer be produced by a repo whose
                            # ecosystem a real adapter claims.
                            degraded.add(repo_id)
                            await _note_finding(
                                writer,
                                run_id,
                                repo_id,
                                kind="EcosystemAdapterUnavailable",
                                payload={
                                    "repo_id": repo_id,
                                    "dest": plan.dest,
                                    "ecosystem": str(plan.unit.ecosystem),
                                    "adapter": plan.adapter_name,
                                    "emitted": "filegroup",
                                    "detail": (
                                        "no ecosystem could be determined for this repo (§3.1 "
                                        "step 2), so the UNKNOWN adapter emitted a single "
                                        "filegroup: the sources are merged and reachable, but "
                                        "nothing compiles them and no external dependency of "
                                        "theirs reached MODULE.bazel (§3.3 step 2)"
                                    ),
                                },
                                severity="warn",
                                now=_now(),
                            )
                # ---- PASS 3: §3.3 step 2's fleet-wide root files, resolved ONCE -------------
                #
                # Once per ecosystem (ADR-0048) over the WHOLE domain, and once per RUN rather
                # than once per wave. Recomputing them per wave is what made wave 0 build against
                # files wave 1 then replaced, and computing them over "the plans this invocation
                # happens to hold" is what made a second `fleet build` shrink them.
                resolved, root_file_failures = await _fleet_support_files(
                    plans, settings=settings, run_id=run_id
                )
                for repo_id, failure in sorted(root_file_failures.items()):
                    # Every repo of the failing ecosystem, with no `members` exemption left to
                    # make: the resolve happens once, before any dispatch, so there is no earlier
                    # wave holding files a later failure would be rewriting a verdict on. A repo
                    # that settled in an EARLIER INVOCATION keeps its `SUCCEEDED` status — the
                    # `_abandon_repo` UPDATE is `WHERE status = 'PENDING'` — and gets the finding,
                    # which is the honest record: this run could not resolve its ecosystem's root
                    # file. It leaves the domain with the rest of its group, because the whole
                    # group is unbuildable and no root file names any of them.
                    plans.pop(repo_id, None)
                    domain.pop(repo_id, None)
                    await _abandon_repo(
                        writer,
                        run_id,
                        repo_id,
                        detail=str(failure),
                        now=_now(),
                        phase=Phase.BUILD,
                        # Two buckets, because they send an operator to two different
                        # places: a resolution that failed is a package index or a spec
                        # conflict, while a coordinate that cannot be rendered is in this
                        # fleet's own inventory and no resolver ever ran. Read off the
                        # exception type, which is a driver type either way — the adapter's
                        # own subclass is never named here (§12.6).
                        kind=(
                            "CoordinateRenderFailed"
                            if isinstance(failure, CoordinateRenderError)
                            else "DependencyResolutionFailed"
                        ),
                    )
                for repo_id, root_files in resolved.items():
                    plans[repo_id] = replace(plans[repo_id], workspace_files=root_files)
                # ---- PASS 4: the delegating adapters' BUILD files, generated ONCE ------------
                #
                # After PASS 3 because the generator reads the fleet's ROOT files — the union
                # `go.mod` is the module graph `-external=static` resolves every out-of-tree
                # import against — and those exist only once the resolve has run. Before the wave
                # loop because one invocation must cover EVERY root of the ecosystem: an import of
                # a sibling resolves to a real in-repo label (`//go/digest`) only when both roots
                # are on one command line, and with one root it is silently dropped at exit 0.
                # ADR-0055 already ingests and plans the whole fleet before the first dispatch,
                # so the set this needs is exactly the set that is already sitting there.
                #
                # A failure here drops its ecosystem from the domain, which is safe for the same
                # reason a PASS 1/2/3 failure's drop is: nothing has been dispatched and nothing
                # published, so the domain is still maximal for every wave that follows.
                generated, generation_failures = await _gazelle_files(
                    plans, settings=settings, run_id=run_id
                )
                for repo_id, failure in sorted(generation_failures.items()):
                    plans.pop(repo_id, None)
                    domain.pop(repo_id, None)
                    await _abandon_repo(
                        writer,
                        run_id,
                        repo_id,
                        detail=str(failure),
                        now=_now(),
                        phase=Phase.BUILD,
                        # Its own bucket, beside the resolver's and the renderer's, because it
                        # sends an operator somewhere else again: not a package index and not
                        # this fleet's inventory, but the generator that writes every target a
                        # delegating ecosystem has.
                        kind="BuildFileGenerationFailed",
                    )
                for repo_id, build_files in generated.items():
                    if repo_id in plans:
                        plans[repo_id] = replace(plans[repo_id], gazelle_files=build_files)
                # ---- the wave loop: DISPATCH, and nothing else ------------------------------
                published = False
                undispatchable: set[str] = set()
                for index in waves:
                    # `only` and `wave` reach the run HERE and only here — they choose which
                    # repos are leased and built. Neither touches `eligible`, `domain`, `plans`
                    # or the root files above, which is what keeps them dispatch filters.
                    members, blocked = await _gated_members(
                        read_conn, run_id, index, only, predecessor=Phase.TRANSFORM
                    )
                    withheld.update(blocked)
                    if not members:
                        continue
                    driven.append(index)
                    # D84: the re-cut below is one `_wave_snapshot` plus one `_plan_build` per
                    # member, and `_plan_build` runs `worktree remove --force`, a `shutil.rmtree`
                    # fallback, `worktree prune` and `worktree add --detach --force`. On a wave
                    # whose wall clock is already spent, all of that is git mutation for members
                    # `_run_build_wave`'s `admit` will not admit: it returns `admitted=()`, the
                    # members stay PENDING at 0 attempts and the verb exits 4. Skipping it moves
                    # none of that. Control falls through to `_check_root_file_domain`, whose
                    # coverage half reads the KEYS of `plans` — which a re-cut never changes, it
                    # only replaces a value — and whose containment half reads the PASS-2
                    # worktree, cut from a snapshot taken after the run's last ingest and so
                    # already holding every `dest` in the domain.
                    #
                    # `published` is deliberately NOT cleared when the re-cut is skipped: the
                    # re-cut is still owed. It is in fact never read again, because a breached
                    # wave's report carries an exit code and the loop `break`s on it below.
                    #
                    # PASS 2 above is the other pre-admission git mutation in this function. It
                    # is NOT guarded here, and that is a DEFERRED DEFECT — not a stated boundary.
                    # The stop rule's "documented boundary" means adversarial-only, and this is
                    # reachable on an ordinary path (below), so it stays an open defect that this
                    # change did not fix. Two measured reasons not to have patched it HERE:
                    #
                    #   1. It is not in this guard's class. PASS 2 is the ONLY populator of
                    #      `plans`, and PASS 3, PASS 4 and `_check_root_file_domain` all read
                    #      `plans` and the worktrees it cuts, regardless of what `admit` does. So
                    #      it is not "git mutation solely for work that cannot run" — though its
                    #      consumers do sit on the same doomed path.
                    #   2. The ONE candidate fix that was tried — "every wave is breached", the
                    #      only predicate that fits a site with no wave index — empties `plans`,
                    #      and `_check_root_file_domain`'s coverage half then raises
                    #      `RootFileDomainDriftError` naming every repo, so the run exits 1 where
                    #      the breached-wave contract fixes it at 4. That is a result about ONE
                    #      candidate, NOT about the class of fixes: whether any fix exists was
                    #      never measured, and nothing here should be read as saying it does not.
                    #
                    # The residue is reachable, not adversarial: `waves` has no phase column, so
                    # TRANSFORM stamps `wave_started_at` for every wave — measured non-NULL after
                    # `fleet transform`, NULL only before any phase has run — and the clock is
                    # cumulative across phases. A `fleet build` started more than
                    # `budgets.wave_max_wallclock_s` (default 14 400 s) after its transform
                    # reaches PASS 2 with every wave ALREADY breached, on a fresh run with no
                    # resume: transform before lunch, build after. Filed OPEN; D84 is PARTLY
                    # ADDRESSED, not fixed.
                    if (
                        published
                        and snapshot is not None
                        and not await _wave_is_breached(
                            settings,
                            writer=writer,
                            read_conn=read_conn,
                            repository=repository,
                            run_id=run_id,
                            wave_index=index,
                            phase=Phase.BUILD,
                            # The same narrowing `_run_build_wave` gives the scheduler it builds
                            # for this wave, so the predicate is read off the same store.
                            only=members,
                        )
                    ):
                        # A previous wave published, so this wave's members are re-cut from a
                        # fresh snapshot: their internal dependencies' generated `BUILD.bazel`
                        # files are on the branch now and their worktrees must contain them.
                        # ONE snapshot for the wave, still under the writer mutex, still
                        # immutable — only the cut POINT moves, exactly as ADR-0053 has it.
                        snapshot = await _wave_snapshot(
                            settings, run_id=run_id, monorepo=monorepo, lock_dir=lock_dir
                        )
                        published = False
                        for repo_id in members:
                            current = plans.get(repo_id)
                            if current is None:
                                continue
                            try:
                                replanned = await _plan_build(
                                    repo_id=repo_id,
                                    ingested=ingests[repo_id],
                                    snapshot=snapshot,
                                    facts=facts.get(
                                        repo_id, _RepoFacts(None, Ecosystem.UNKNOWN)
                                    ),
                                    internal_deps=internal_deps.get(repo_id, ()),
                                    manifest_paths=manifest_paths.get(repo_id, ()),
                                    owned_keys=owned_keys,
                                    monorepo=monorepo,
                                    build_root=build_root,
                                )
                            except (BuildStepUnavailableError, GitError, OSError) as exc:
                                # Kept in `domain` and in `plans`, unlike a PASS 1/2 failure.
                                # Earlier waves have already published root files that name this
                                # unit and its history is on the branch; dropping it now would
                                # shrink the domain mid-run, which is the defect. It is
                                # REQUIRES_HUMAN_INTERVENTION so the scheduler will not admit it,
                                # and it is excluded from the containment check below because its
                                # worktree is the thing that could not be made.
                                undispatchable.add(repo_id)
                                await _abandon_repo(
                                    writer,
                                    run_id,
                                    repo_id,
                                    detail=str(exc),
                                    now=_now(),
                                    phase=Phase.BUILD,
                                    kind="BuildPreparationFailed",
                                )
                                continue
                            # The run's root files ride across the re-cut verbatim: they are a
                            # function of the domain, and the domain did not move (§11.6). So do
                            # the generated BUILD files, for the identical reason and with one
                            # more of its own: the generator ran ONCE, over every root at once,
                            # and `_plan_build` cannot reproduce that from one repo's worktree —
                            # a re-plan that dropped them would republish a Go package whose
                            # targets and cross-repo edges had silently disappeared.
                            plans[repo_id] = replace(
                                replanned,
                                workspace_files=current.workspace_files,
                                gazelle_files=current.gazelle_files,
                            )
                    # The root files describe the RUN's domain, and every worktree about to be
                    # dispatched must contain it (Rule 11 — loud, before a lease).
                    await _check_root_file_domain(
                        plans,
                        [repo for repo in members if repo not in undispatchable],
                        domain=domain,
                    )
                    try:
                        report = await _run_build_wave(
                            settings,
                            config=config,
                            db_path=path,
                            repository=repository,
                            writer=writer,
                            read_conn=read_conn,
                            run_id=run_id,
                            wave_index=index,
                            plans=plans,
                            members=members,
                            monorepo=monorepo_path,
                            lock_dir=lock_dir,
                            build_root=build_root,
                            sandboxed=sandboxed,
                            pool=pool,
                            evidence=evidence,
                        )
                    except WaveNotReadyError as exc:
                        raise UsageError(str(exc)) from exc
                    reports.append(report)
                    # Whatever this wave built has published to `integration`, so the NEXT wave's
                    # members must be re-cut from a snapshot taken after it.
                    published = True
                    if report.exit_code is not None:
                        break
                statuses = await _phase_statuses(read_conn, run_id, Phase.BUILD)
                violations = _build_criterion(plans, evidence, statuses)
            finally:
                await read_conn.close()
    finally:
        pool.shutdown(wait=True, cancel_futures=True)

    with suppress(Exception):  # a projection is an OUTPUT (§11.5); it never fails a build
        await project_once(path, run_id=UUID(run_id), path=DEFAULT_PROJECTION_PATH)

    if violations:
        raise BuildCriterionError(
            "§3.3's success criterion does not hold for "
            f"{len(violations)} repo(s) whose phase says SUCCEEDED: {'; '.join(violations)}"
        )
    attention = sorted(
        repo for repo, status in statuses.items()
        if status is RepoStatus.REQUIRES_HUMAN_INTERVENTION
    )
    halt = next((r.exit_code for r in reports if r.exit_code is not None), None)
    exit_code = (
        halt
        if halt is not None
        else (ExitCode.REQUIRES_HUMAN_INTERVENTION if attention else ExitCode.SUCCESS)
    )
    return {
        "run_id": run_id,
        "waves": driven,
        "repos": len(statuses),
        "succeeded": sum(1 for s in statuses.values() if s is RepoStatus.SUCCEEDED),
        "failed": len(attention),
        "attention": attention,
        "withheld": sorted(withheld),
        "sandboxed": sandboxed,
        "adapter_unavailable": sorted(degraded),
        "integration_refs": {
            repo: plan.integration_ref for repo, plan in sorted(plans.items())
        },
        "halt": next((str(r.halt) for r in reports if r.halt is not None), None),
        "exit_code": int(exit_code),
    }


def _build_criterion(
    plans: Mapping[str, _BuildPlan],
    evidence: _BuildEvidence,
    statuses: Mapping[str, RepoStatus],
) -> list[str]:
    """§3.3's **success criterion**, checked against the evidence for every repo called SUCCEEDED.

    `bazel build` exit 0 **and** `bazel test` exit 0, both recorded as `attempts` rows, plus the
    two generated files actually on disk. Checked here rather than trusted from the phase status
    because a status is a claim and these are the facts it claims.
    """
    violations: list[str] = []
    for repo_id in sorted(plans):
        if statuses.get(repo_id) is not RepoStatus.SUCCEEDED:
            continue
        plan = plans[repo_id]
        output = evidence.by_repo.get(repo_id)
        # `None` is not a violation: a repo that succeeded in an EARLIER invocation is settled
        # and this one dispatched nothing for it, which is idempotency working. What is checked
        # either way is the tree — the criterion is about the files, not about the dispatch.
        if output is not None and not output.green:
            violations.append(
                f"{repo_id}: SUCCEEDED with build_ok={output.build_ok} test_ok={output.test_ok}"
            )
        for label, path in (
            ("BUILD.bazel", plan.worktree / plan.dest / "BUILD.bazel"),
            ("MODULE.bazel", plan.worktree / "MODULE.bazel"),
        ):
            if not path.is_file():
                violations.append(f"{repo_id}: SUCCEEDED with no {label} at {path}")
    return violations


async def _verify_impl(
    opts: GlobalOptions,
    settings: FleetSettings,
    path: Path,
    *,
    run_id: str,
    wave: int | None,
    only: str | None,
    rdeps_limit: int,
    rdeps_sample_n: int,
    affected_only: bool,
) -> dict[str, object]:
    """Phase 4 steps 1–3 over one fleet, wave by wave, through the real composition."""
    log_configure(level=opts.log_level)
    config = settings.config
    evidence = _VerifyEvidence()
    plans: dict[str, _VerifyPlan] = {}
    reports: list[WaveReport] = []
    driven: list[int] = []
    withheld: set[str] = set()
    overrides = dict(settings.config.build.monorepo_dir_overrides)
    _validate_monorepo_dir_overrides(overrides)
    verify_root = (settings.root / settings.config.run.work_dir).resolve() / "verify"
    monorepo, _monorepo_path, lock_dir = await _monorepo_checkout(settings)
    pool = new_cpu_pool(settings.config.concurrency.cpu_pool_workers)
    try:
        async with StateWriter(path, owner="fleet-verify") as writer:
            read_conn = await connect_ro(path)
            try:
                repository = SqliteStateRepository(writer=writer, read_conn=read_conn)
                facts = await _repo_facts(read_conn)
                waves = await _open_phase_waves(read_conn, run_id, Phase.VERIFY, wave)
                await repository.reap_expired_phase_leases(run_id, now=_now())
                await repository.open_budget_ledger(
                    run_id, max_usd=settings.config.budgets.run_max_cost_usd, now=_now()
                )
                for index in waves:
                    members, blocked = await _gated_members(
                        read_conn, run_id, index, only, predecessor=Phase.BUILD
                    )
                    withheld.update(blocked)
                    if not members:
                        continue
                    driven.append(index)
                    for repo_id in members:
                        await repository.upsert_phase(
                            run_id,
                            repo_id,
                            Phase.VERIFY,
                            now=_now(),
                            max_attempts=MAX_ATTEMPTS,
                        )
                    # D84: same class, same remedy as `_transform_impl`'s prepare loop, but the
                    # guard is read once per wave and consulted below rather than wrapping the
                    # loop — and that is REQUIRED, not a preference. A breached wave's observable
                    # behaviour must not move, and a member's STATUS is part of what must not
                    # move. `_dest_for`'s §3.4 refusal calls `_abandon_repo`, which writes
                    # REQUIRES_HUMAN_INTERVENTION; wrapping the loop would leave that member
                    # PENDING instead, changing the very thing the guard must preserve. So the
                    # refusal keeps running: `_dest_for` is pure, and the guard sits after it and
                    # before `_prepare_verify`, which is the only git mutation here.
                    breached = await _wave_is_breached(
                        settings,
                        writer=writer,
                        read_conn=read_conn,
                        repository=repository,
                        run_id=run_id,
                        wave_index=index,
                        phase=Phase.VERIFY,
                        only=members,
                    )
                    for repo_id in members:
                        if repo_id in plans:
                            continue
                        # The SAME `layout(node)` Phases 2 and 3 used. §3.4 tests
                        # `//<dest>/...`, so a fourth opinion about where the repo went would
                        # test a package that does not exist and report it as the repo's failure.
                        try:
                            dest = _dest_for(
                                repo_id,
                                facts.get(repo_id) or _RepoFacts(None, Ecosystem.UNKNOWN),
                                overrides,
                            )
                        except (ReservedDestError, ValueError) as exc:
                            await _abandon_repo(
                                writer,
                                run_id,
                                repo_id,
                                detail=f"{repo_id}: §3.4 tests `//<dest>/...` and §3.3's "
                                f"layout() names no package for this node: {exc}",
                                now=_now(),
                                phase=Phase.VERIFY,
                                kind="VerifyPreparationFailed",
                            )
                            continue
                        if breached:
                            # D84: the snapshot ref, the `worktree remove --force`/`rmtree`, the
                            # `worktree prune` and the `worktree add` below are all git mutation
                            # for a member `admit` will not admit. Fall through to
                            # `_run_verify_wave` and its unchanged exit-4 report.
                            continue
                        try:
                            plans[repo_id] = await _prepare_verify(
                                settings,
                                run_id=run_id,
                                repo_id=repo_id,
                                dest=dest,
                                monorepo=monorepo,
                                lock_dir=lock_dir,
                                verify_root=verify_root,
                            )
                        except (GitError, OSError) as exc:
                            await _abandon_repo(
                                writer,
                                run_id,
                                repo_id,
                                detail=str(exc),
                                now=_now(),
                                phase=Phase.VERIFY,
                                kind="VerifyPreparationFailed",
                            )
                    try:
                        report = await _run_verify_wave(
                            settings,
                            config=config,
                            db_path=path,
                            repository=repository,
                            writer=writer,
                            read_conn=read_conn,
                            run_id=run_id,
                            wave_index=index,
                            plans=plans,
                            members=members,
                            verify_root=verify_root,
                            sandboxed=True,
                            rdeps_limit=rdeps_limit,
                            rdeps_sample_n=rdeps_sample_n,
                            affected_only=affected_only,
                            pool=pool,
                            evidence=evidence,
                        )
                    except WaveNotReadyError as exc:
                        raise UsageError(str(exc)) from exc
                    reports.append(report)
                    if report.exit_code is not None:
                        break
                statuses = await _phase_statuses(read_conn, run_id, Phase.VERIFY)
            finally:
                await read_conn.close()
    finally:
        pool.shutdown(wait=True, cancel_futures=True)

    with suppress(Exception):  # a projection is an OUTPUT (§11.5); it never fails a verification
        await project_once(path, run_id=UUID(run_id), path=DEFAULT_PROJECTION_PATH)

    attention = sorted(
        repo for repo, status in statuses.items()
        if status is RepoStatus.REQUIRES_HUMAN_INTERVENTION
    )
    halt = next((r.exit_code for r in reports if r.exit_code is not None), None)
    exit_code = (
        halt
        if halt is not None
        else (ExitCode.REQUIRES_HUMAN_INTERVENTION if attention else ExitCode.SUCCESS)
    )
    return {
        "run_id": run_id,
        "waves": driven,
        "repos": len(statuses),
        "succeeded": sum(1 for s in statuses.values() if s is RepoStatus.SUCCEEDED),
        "failed": len(attention),
        "attention": attention,
        "withheld": sorted(withheld),
        "reports": {repo: _report_payload(out) for repo, out in sorted(evidence.by_repo.items())},
        "halt": next((str(r.halt) for r in reports if r.halt is not None), None),
        "exit_code": int(exit_code),
    }


def _report_payload(output: VerifyOutput) -> dict[str, object]:
    """The `VerificationReport`'s disclosure surface, rendered from the report and nothing else.

    §3.4: a sampled verification is a **disclosed** reduction that a human clears. So the sample
    size, the true closure size and the seed that chose it are all here whenever
    `rdeps_truncated` is true, and `equivalence` is read off the report rather than recomputed.
    """
    report = output.report
    return {
        "verdict": "FAIL" if report is None else report.verdict,
        "equivalence": str(output.equivalence),
        "build_ok": output.build_ok,
        "test_ok": output.test_ok,
        "rdeps_ok": False if report is None else report.rdeps_ok,
        "rdeps_query": output.rdeps_query,
        "rdeps_target_count": output.rdeps_target_count,
        "rdeps_tested": output.rdeps_tested,
        "rdeps_truncated": output.rdeps_truncated,
        "rdeps_sample_n": output.rdeps_sample_n,
        "rdeps_sample_seed": output.rdeps_sample_seed,
        "direct_rdeps": output.direct_rdeps,
        "integration_ref": output.integration_ref,
        "target_pattern_file": output.target_pattern_file,
    }


def _build_lines(result: Mapping[str, object]) -> list[str]:
    degraded = cast("list[str]", result["adapter_unavailable"])
    blocked = cast("list[str]", result["withheld"])
    lines = [
        f"run {result['run_id']}: {result['succeeded']} built, {result['failed']} needing a "
        f"human over {result['repos']} repo(s) in wave(s) {result['waves']}"
    ]
    if blocked:
        lines.append(
            f"withheld: {len(blocked)} repo(s) whose Phase 2 has not SUCCEEDED were not "
            f"admitted to Phase 3: {', '.join(blocked[:3])}"
        )
    if degraded:
        lines.append(
            f"warning: no ecosystem was recognized for {len(degraded)} repo(s), so each was "
            "emitted as the UNKNOWN adapter's single filegroup with no external dependency in "
            f"MODULE.bazel (§3.3 step 2): {', '.join(degraded[:3])} — recorded as "
            "EcosystemAdapterUnavailable"
        )
    return lines


def _verify_lines(result: Mapping[str, object]) -> list[str]:
    reports = cast("dict[str, dict[str, object]]", result["reports"])
    sampled = sorted(repo for repo, body in reports.items() if body["rdeps_truncated"])
    blocked = cast("list[str]", result["withheld"])
    lines = [
        f"run {result['run_id']}: {result['succeeded']} verified, {result['failed']} needing a "
        f"human over {result['repos']} repo(s) in wave(s) {result['waves']}"
    ]
    if blocked:
        lines.append(
            f"withheld: {len(blocked)} repo(s) whose Phase 3 has not SUCCEEDED were not "
            f"admitted to Phase 4: {', '.join(blocked[:3])}"
        )
    for repo in sampled:
        body = reports[repo]
        lines.append(
            f"{repo}: CLOSURE_SAMPLED — {body['rdeps_tested']} of {body['rdeps_target_count']} "
            f"rdeps tested (seed {body['rdeps_sample_seed']}); the reduction is disclosed and a "
            "human clears it (§3.4)"
        )
    return lines


def _raise_for_phase(result: Mapping[str, object]) -> None:
    """One exit-code mapping for both phase verbs — §10's table, read off the payload."""
    code = int(str(result["exit_code"]))
    if code == ExitCode.REQUIRES_HUMAN_INTERVENTION:
        raise HumanInterventionError(
            f"{result['failed']} repo(s) ended REQUIRES_HUMAN_INTERVENTION: "
            f"{', '.join(str(name) for name in cast('list[str]', result['attention']))}"
        )
    if code != ExitCode.SUCCESS:
        raise FleetCliError(str(result["halt"]), exit_code=code)


# --------------------------------------------------------------------------------------
# §11.5 step 8 — continue from the re-entry floors
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Continuation:
    """One phase §11.5 step 8 must re-drive, and the repos whose floor put it there.

    `repos` is **empty** for a phase the plan carries because it lies inside the span rather than
    because a repo is floored at it. That is the honest reading of the field's own definition —
    "the repos whose floor put it there" — and an empty tuple is not "nothing to do": the repos
    floored BELOW that phase pass through it, and the delegate re-derives its own admissions from
    `only`, never from this tuple.
    """

    phase: Phase
    repos: tuple[str, ...]


#: The phases step 8 can actually serve. `Phase.SCAN` is deliberately absent: `_scan_impl` takes
#: five flags no resume carries and no `wave` at all, so serving a `SCAN` floor means step 8
#: inventing five values the operator never wrote — character for character the recorded
#: `effort: low` defect (`src/fleet/models/tasks.py`). ADR-0080 rules it reported and skipped.
_SERVABLE_PHASES: Final = (Phase.TRANSFORM, Phase.BUILD, Phase.VERIFY)

#: The loud payload key ruling B makes load-bearing. Named for what is TRUE of both routes into
#: a `SCAN` floor — a frontier `SCAN` ("never started") and a walk that fell through ("the
#: durable evidence is gone") — because v1 does not distinguish them and a name like
#: `never_scanned` would be false of the second.
_SCAN_SKIPPED_KEY: Final = "scan_floor_not_continued"


def _continue_from_floors(
    floors: Mapping[str, Phase], only: str | None
) -> tuple[_Continuation, ...]:
    """§11.5 step 8's PLAN: the servable SPAN from the lowest floor upward, ascending. **No I/O.**

    **The span, not the set of floors present.** Each delegate drives exactly ONE phase and
    nothing walks the ladder, so a phase the plan omits is not run for anybody. Planning the
    distinct floors instead of the span from the lowest of them is how `{A: TRANSFORM,
    B: VERIFY}` plans `TRANSFORM, VERIFY` with **BUILD absent**: `A` is transformed, never built,
    `_verify_impl` cannot admit it, nothing halts, and the verb exits 0 having stranded it
    mid-ladder — "a success it has not earned", which is what ADR-0076's closing bullet forbade.
    So every phase of `_SERVABLE_PHASES` at or above the lowest **servable** floor is planned,
    carrying its own floored repos or none (see `_Continuation`). The upper end is
    `_SERVABLE_PHASES`'s own last entry rather than a literal, and the lower end is a floor that
    was actually observed — neither is a hard-coded list.

    **The span is fleet-wide; ADMISSION stays per repo,** so a repo floored above a spanned phase
    is not re-run at it: `orchestrator.runner.PhaseRunner` admits on a `PENDING` CAS and answers
    an already-terminal phase with `_already_complete`, which is why §10's *"continue from each
    repo's re-entry floor"* still holds of every repo while the phases driven are the union.

    **Lowest SERVABLE floor, not lowest floor.** A `SCAN`-floored repo is skipped, not served
    (below), so it is not a repo the span exists to carry. Spanning up from `SCAN` would plan
    `TRANSFORM` for a fleet whose only sub-`VERIFY` repo was never scanned — spend for a
    beneficiary that was skipped, and an invitation to transform un-scanned work. The skip stays
    the driver's, and its input is unchanged.

    `floors` is step 5's `computed_floors`, PASSED IN and never recomputed: the caller has
    already paid for the walk, and a second derivation is free to disagree with the one the
    demotion was written from — a plan that continues from a floor nothing was demoted to is a
    plan for a fleet that does not exist.

    `only` is applied with the same predicate the phase drivers use — `fnmatch` over the repo
    id, as `_wave_repos` does — so the span is measured over the floors of the repos the
    delegates would admit, and a repo the glob excludes cannot pull the span down. Wave
    membership is NOT consulted: it is a SQLite read, this
    function performs none, and each delegate re-derives it from the `only` it is handed.

    A `Phase.SCAN` group is returned like any other. `phase_floor` really can return
    `Phase.SCAN` — `phase_floor({}, {})` does — and `computed_floors` is written before
    `demotable_phases` runs, so it retains every repo step 5 did NOT demote, which on a fleet
    with un-started repos is `SCAN`-heavy. The plan stays TOTAL and the driver owns the skip:
    a repo dropped here would be a repo an operator cannot see, and `_SCAN_SKIPPED_KEY` is the
    whole point of the ruling.
    """
    grouped: dict[Phase, list[str]] = {}
    for repo_id, floor in floors.items():
        if only is not None and not fnmatch(repo_id, only):
            continue
        grouped.setdefault(floor, []).append(repo_id)
    servable_floors = [floor for floor in grouped if floor in _SERVABLE_PHASES]
    if servable_floors:
        lowest = min(servable_floors)
        for phase in _SERVABLE_PHASES:
            if phase >= lowest:
                grouped.setdefault(phase, [])
    return tuple(
        _Continuation(phase=phase, repos=tuple(sorted(grouped[phase])))
        for phase in sorted(grouped)
    )


async def _continue_impl(
    opts: GlobalOptions,
    settings: FleetSettings,
    path: Path,
    *,
    run_id: str,
    floors: Mapping[str, Phase],
    only: str | None,
    ladder: int,
    dry_run: bool,
    timeout_s: int,
    sandboxed: bool,
    rdeps_limit: int,
    rdeps_sample_n: int,
    affected_only: bool,
) -> dict[str, object]:
    """Drive `_continue_from_floors`'s plan through the three phase composition roots.

    Every per-phase knob is a REQUIRED keyword with no default. The three roots do not share a
    signature and this function does not invent one: `ladder`/`dry_run` are `_transform_impl`'s,
    `timeout_s`/`sandboxed` are `_build_impl`'s, the three `rdeps*`/`affected_only` are
    `_verify_impl`'s. Defaulting any of them here would be step 8 choosing a value the operator
    never wrote, which is the same defect the `SCAN` skip exists to avoid; the caller that owns
    the command line chooses them.

    **No fifth `PhaseRunner`.** The four instantiations in this module are the phase impls' own;
    step 8 re-enters them rather than composing a runner of its own, so a continuation and a
    bare `fleet transform` run the identical composition.

    **The mirror mutex, once, before the first delegate (ADR-0080 ruling C).** §10's "a second
    run started against a mirror another live run already owns" is checked by
    `_phase_preflight` — which the `transform`/`build`/`verify` COMMANDS call and the `_impl`s do
    not — so delegating straight to the impls bypasses it. Taken once rather than per delegate:
    a per-delegate check leaves a window between phases for exactly the racing run it excludes.
    Taken only when something is servable, so a plan that is entirely `SCAN` still reports
    `_SCAN_SKIPPED_KEY` instead of refusing with the diagnosis withheld.

    Stops at the FIRST halt. The halting delegate's whole payload is returned under `"halted"`;
    `_raise_for_continuation` reads §10's exit code off it. This function raises nothing of its
    own, so the caller can emit the report before the exit code ends the process.
    """
    plan = _continue_from_floors(floors, only)
    servable = tuple(entry for entry in plan if entry.phase in _SERVABLE_PHASES)
    skipped = tuple(
        repo for entry in plan if entry.phase is Phase.SCAN for repo in entry.repos
    )
    result: dict[str, object] = {
        "run_id": run_id,
        "plan": [{"phase": e.phase.name, "repos": list(e.repos)} for e in plan],
        _SCAN_SKIPPED_KEY: list(skipped),
        "driven": [],
        "phase_results": {},
        "halted": None,
        "halted_phase": None,
    }
    if not servable:
        return result
    # §11.3/§12.22, once and before the first delegate — the same shape as the mirror mutex
    # immediately below and for the same reason: `fleet build`/`fleet verify` take this gate at
    # the top of their own command bodies (cli.py's `_require_disk_headroom(settings)` calls),
    # `_continue_impl` re-enters `_build_impl`/`_verify_impl` directly and bypasses it, and a
    # continuation is not exempt from starting real phase work on a volume already under
    # `preflight.min_free_bytes` (subtask 10c, ADR-0080 §7).
    _require_disk_headroom(settings)
    _refuse_concurrent_mirror_run(settings, run_id)
    for entry in servable:
        if entry.phase is Phase.TRANSFORM:
            phase_result = await _transform_impl(
                opts,
                settings,
                path,
                run_id=run_id,
                wave=None,
                only=only,
                ladder=ladder,
                dry_run=dry_run,
            )
        elif entry.phase is Phase.BUILD:
            phase_result = await _build_impl(
                opts,
                settings,
                path,
                run_id=run_id,
                wave=None,
                only=only,
                timeout_s=timeout_s,
                sandboxed=sandboxed,
            )
        elif entry.phase is Phase.VERIFY:
            phase_result = await _verify_impl(
                opts,
                settings,
                path,
                run_id=run_id,
                wave=None,
                only=only,
                rdeps_limit=rdeps_limit,
                rdeps_sample_n=rdeps_sample_n,
                affected_only=affected_only,
            )
        else:
            raise FleetCliError(
                f"§11.5 step 8 has no composition root for {entry.phase.name}; "
                f"the servable phases are {[p.name for p in _SERVABLE_PHASES]}."
            )
        cast("list[str]", result["driven"]).append(entry.phase.name)
        cast("dict[str, object]", result["phase_results"])[entry.phase.name] = phase_result
        if int(str(phase_result["exit_code"])) != ExitCode.SUCCESS:
            result["halted"] = phase_result
            result["halted_phase"] = entry.phase.name
            break
    return result


def _raise_for_continuation(result: Mapping[str, object]) -> None:
    """§11.5 step 8's exit code — the halting delegate's, read off ITS payload.

    Delegates to `_raise_for_phase` rather than re-deriving §10's table. Exactly three functions
    in this module pair `HumanInterventionError` with `FleetCliError(exit_code=...)`; step 8 is
    deliberately not a fourth, because a fourth copy is a fourth place §10's table can rot.
    """
    halted = result["halted"]
    if halted is None:
        return
    _raise_for_phase(cast("Mapping[str, object]", halted))


# --------------------------------------------------------------------------------------
# the durable wave ledger (§11.2) — exit 10
# --------------------------------------------------------------------------------------


async def _wave_spend(
    conn: aiosqlite.Connection, run_id: str, wave_index: int
) -> tuple[float, float, int]:
    """`(max_usd, spent_usd, members)` for one wave.

    §6: the wave's ceiling is FROZEN in `waves.max_usd` at first admission, and its spend is
    **not duplicated** — it is `SUM(repo_ledger.spent_usd)` over the wave's REPO members. Reading
    it here rather than recomputing the ceiling is what makes a resume enforce the same number
    the halt was measured against.
    """
    rows = await _rows(
        conn,
        "SELECT w.max_usd, "
        "       COALESCE((SELECT SUM(rl.spent_usd) FROM repo_ledger rl "
        "                  JOIN wave_members m ON m.run_id = rl.run_id AND m.node_id = rl.repo_id "
        "                 WHERE rl.run_id = w.run_id AND m.wave_index = w.wave_index "
        "                   AND m.node_kind = 'REPO'), 0.0), "
        "       (SELECT COUNT(*) FROM wave_members m2 WHERE m2.run_id = w.run_id "
        "         AND m2.wave_index = w.wave_index AND m2.node_kind = 'REPO') "
        "  FROM waves w WHERE w.run_id = ? AND w.wave_index = ?",
        (run_id, wave_index),
    )
    if not rows:
        raise UsageError(f"run {run_id} has no wave {wave_index}; `fleet sequence` plans waves.")
    return float(rows[0][0]), float(rows[0][1]), int(rows[0][2])


async def _earliest_open_wave(conn: aiosqlite.Connection, run_id: str) -> int | None:
    rows = await _rows(
        conn,
        "SELECT DISTINCT m.wave_index FROM wave_members m "
        "  LEFT JOIN phases p ON p.run_id = m.run_id AND p.repo_id = m.node_id "
        " WHERE m.run_id = ? AND m.node_kind = 'REPO' "
        "   AND (p.status IS NULL OR p.status NOT IN ('SUCCEEDED','SKIPPED',"
        "        'REQUIRES_HUMAN_INTERVENTION')) "
        " ORDER BY m.wave_index LIMIT 1",
        (run_id,),
    )
    return None if not rows else int(rows[0][0])


async def _refuse_exhausted_wave(
    conn: aiosqlite.Connection,
    settings: FleetSettings,
    run_id: str,
    wave: int | None,
    *,
    raise_to_usd: float | None = None,
) -> None:
    """§10 exit 10, enforced BEFORE a wave is re-entered.

    The wave ledger is durable, so a resume that walks back into an exhausted wave carrying the
    same spend halts again — forever — unless the ceiling is raised. Checking here rather than at
    the first `reserve()` means the operator learns it before any work is admitted, and gets the
    exact `--raise-wave-budget` figure to pass.
    """
    index = wave if wave is not None else await _earliest_open_wave(conn, run_id)
    if index is None:
        return
    max_usd, spent, members = await _wave_spend(conn, run_id, index)
    ceiling = raise_to_usd if raise_to_usd is not None else max_usd
    if members == 0 or spent < ceiling:
        return
    derived = Ceilings.from_settings(settings.config.budgets, settings.config.stubs).wave_max_usd(
        members
    )
    raise WaveBudgetExhausted(
        f"wave {index} of run {run_id} has spent ${spent:.2f} of its ${ceiling:.2f} ceiling "
        f"({members} members × ${settings.config.budgets.wave_max_cost_usd_per_repo:.2f} = "
        f"${derived:.2f} at today's config). §11.2: the wave ledger is durable, so re-entering "
        "it halts again. Clear it with "
        f"`fleet resume --raise-wave-budget <usd greater than {spent:.2f}>` (audited)."
    )


def _check_wave_budget(
    opts: GlobalOptions, settings: FleetSettings, run_id: str, wave: int | None
) -> None:
    """The synchronous entry point for a command that is not already inside the loop."""
    _run(
        _with_ro(
            opts.db_path, lambda conn: _refuse_exhausted_wave(conn, settings, run_id, wave)
        )
    )


# --------------------------------------------------------------------------------------
# fleet pr
# --------------------------------------------------------------------------------------


PR_RECORD_KIND: Final = "PullRequest"
"""Where a `PullRequestDraft` lives between `fleet pr` and `fleet pr --sync`.

§6 says outright that there is no `pr_drafts` table. The two stores it names are
`checkpoints.payload` and `findings.payload`, and `checkpoints` is not available for this:
`PhaseRunner` already owns `(run_id, repo_id, 4)` for its `PhaseCheckpoint`, so a PR record
written there would be clobbered by the next `fleet verify` — which is the deadlock this whole
verb exists to remove, re-introduced through the back door. So it is a `findings` row, whose
`kind` §6 makes free text precisely so a new one is not a schema migration, keyed by the same
idempotency index every other finding uses: one row per repo, upserted, so ingesting `MERGED`
REPLACES the believed `DRAFTED` instead of appending a second opinion.
"""

VERIFICATION_KIND: Final = "VerificationReport"
"""The report `_VerifySink` persists and `fleet pr` renders the PR body from. Same store, same
reason: `fleet pr` is a later process and §3.4's draft rules are functions of `equivalence`."""


class PrEmissionError(FleetCliError):
    """The forge failed for at least one repo. Exit 1, and the repos are named.

    Not exit 7: nothing here is a verdict about a repo's migration. The configured forge's client
    being absent, unauthenticated or rate-limited is an infrastructure fact the operator fixes and
    re-runs, and `fleet pr` is idempotent, so re-running costs nothing but the calls that failed.

    The message names `pr.forge`, not `gh`: an operator running `pr.forge: gitea` has no `gh` on
    the box at all, and a failure that blames a binary they never configured sends them to debug
    the wrong thing.
    """

    exit_code = ExitCode.UNEXPECTED_ERROR


def _validate_pr_flags(*, draft: bool, push: bool) -> None:
    """§10's `fleet pr` flags: each one does what it says, or is refused here."""
    if not draft:
        raise UsageError(
            "--no-draft is refused: draft is a VERDICT, not a style. "
            "`workers/prwriter.py::_must_be_draft` derives it from the `VerificationReport` — a "
            "`STUB_LIMITED` or `CLOSURE_SAMPLED` verification and a `DEGRADED` repo each force a "
            "draft (§3.4, §3.5.1) — and a flag that could override that would put §3.4's "
            "disclosure after the reviewer instead of before them. `--ready` is the only "
            "promotion path, and it refuses while any stub row is ACTIVE or SUPERSEDED."
        )
    if push:
        raise UsageError(
            "--push is not implemented: pushing `migrate/<repo>` to a forge remote has no code "
            "path in src/fleet/vcs/ (`git.py` pushes nothing and `github.py` wraps `gh` only), "
            "so the flag would report a push that never happened and then open a PR against a "
            "head ref the forge has never seen."
        )


@app.command()
def pr(
    ctx: typer.Context,
    wave: Annotated[int | None, typer.Option("--wave")] = None,
    repo: Annotated[str | None, typer.Option("--repo")] = None,
    draft: Annotated[bool, typer.Option("--draft/--no-draft")] = True,
    ready: Annotated[
        bool, typer.Option("--ready", help="The ONLY path from draft to ready-for-review.")
    ] = False,
    sync: Annotated[
        bool, typer.Option("--sync", help="Re-read every non-terminal PR's merge state.")
    ] = False,
    push: Annotated[bool, typer.Option("--push")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """Phase 4 steps 4–5: ingest PR merge state, then open stacked PRs in topological order.

    **Step 5 is why this verb is not optional.** `MERGED` is a fact about GitHub; three gates
    consume it (the §3.4 stacking precondition, the §3.5 `blocked_by` release, the §3.5.1 T1 stub
    trigger) and nothing else in the harness can produce it. `--sync` is that step invoked once:
    it re-reads every non-terminal PR through `gh`, writes `MERGED`/`CLOSED` through the single
    writer, and emits `pr_merged`. It re-checks merge state **without re-running any phase**,
    which is what makes "the wave is waiting on a merge" a pollable fact rather than a stall.

    Without `--sync` the verb opens PRs, and it will not open one for a repo whose dependency PR
    is not `MERGED` **as ingested** — that repo is HELD, not shipped and not failed. The rule is
    then applied a second time by the worker against what the forge says right now, because §3.4
    says the precondition is observed and never assumed.

    `--ready` refuses with exit 2 while any `stubs` row for the repo is `ACTIVE` or `SUPERSEDED`
    (§3.5.1, §12.37) — marking a stubbed repo ready-for-review is how a stub reaches `main`.
    """
    with _mapped_errors():
        opts, settings, run_id = _phase_preflight(ctx)
        _validate_pr_flags(draft=draft, push=push)
        path = _require_db(opts)
        if ready:
            _run(_with_ro(path, lambda c: _refuse_unresolved_stubs(c, run_id, repo)))
        if sync:
            result = _run(_pr_sync_impl(opts, settings, path, run_id=run_id))
            _emit(opts, result, _pr_sync_lines(result))
            return
        result = _run(
            _pr_impl(
                opts, settings, path, run_id=run_id, wave=wave, only=repo,
                ready=ready, dry_run=dry_run,
            )
        )
        _emit(opts, result, _pr_lines(result))
        failed = cast(Mapping[str, str], result["failed"])
        if failed:
            listed = ", ".join(f"{name}: {reason}" for name, reason in sorted(failed.items()))
            raise PrEmissionError(
                f"forge {settings.config.pr.forge!r} failed for {len(failed)} repo(s) — {listed}"
            )


# --------------------------------------------------------------------------------------
# step 5 — PR state ingestion. The only writer of `PrState.MERGED` in the harness.
# --------------------------------------------------------------------------------------


def _forge(
    settings: FleetSettings, *, cwd: Path | None = None, deadline: float | None = None
) -> Forge:
    """The driver `pr.forge` names, over the `GH_RUNNER` seam or over the real binary.

    Config selects a DRIVER, not a base URL: `gh` speaks the GitHub API and cannot talk to a
    self-hosted Gitea, so `pr.forge: gitea` is a different protocol and not a flag. `pr.forge` is
    validated at settings load (`PrSection._forge_resolves`), which is why an unknown name is an
    operator error caught before wave 0 rather than a `fleet pr` that fails after 250 repos have
    been transformed.

    `curl_config` is the PATH of the mode-600 `curl -K` file, resolved against the config root —
    never the token. `attempts.command` persists argv verbatim, so a token passed as a header
    argument would be written into the state DB in cleartext (§11.4); curl reads it from disk
    after `execve` and it therefore appears in no argv this harness records.
    """
    pr_config = settings.config.pr
    return build_forge(
        pr_config.forge,
        runner=GH_RUNNER,
        base_url=pr_config.forge_url,
        owner=pr_config.forge_owner,
        repo=pr_config.forge_repo or None,
        curl_config=_forge_token_config(settings),
        cwd=cwd,
        deadline=deadline,
    )


def _forge_token_config(settings: FleetSettings) -> Path | None:
    """`pr.forge_token_config` resolved against the config root, or None when unset."""
    configured = settings.config.pr.forge_token_config.strip()
    if not configured:
        return None
    return (settings.root / configured).resolve()


async def _pr_records(conn: aiosqlite.Connection, run_id: str) -> dict[str, PullRequestDraft]:
    """Every PR this run has opened, as the harness currently believes it (§3.4 step 5's input)."""
    rows = await _rows(
        conn,
        "SELECT repo_id, payload FROM findings WHERE run_id = ? AND kind = ? ORDER BY repo_id",
        (run_id, PR_RECORD_KIND),
    )
    records: dict[str, PullRequestDraft] = {}
    for row in rows:
        try:
            records[str(row[0])] = PullRequestDraft.model_validate_json(str(row[1]))
        except ValidationError as exc:  # Rule 11: a PR we cannot parse is not a PR we may ignore
            raise PrEmissionError(
                f"the persisted PR record for {row[0]!r} does not validate: {exc}. Refusing to "
                "continue — treating it as absent would open a SECOND PR for the same repo."
            ) from exc
    return records


async def _write_pr_record(
    writer: StateWriter, run_id: str, draft: PullRequestDraft, *, now: datetime
) -> None:
    """One upserted `PullRequest` row. The single writer (§11.5) is the only path to `MERGED`."""
    row = (
        run_id,
        draft.repo_id,
        PR_RECORD_KIND,
        "info",
        _fingerprint(run_id, draft.repo_id, PR_RECORD_KIND),
        redact_text(draft.model_dump_json()),
        _iso(now),
    )

    async def unit(conn: aiosqlite.Connection) -> None:
        await conn.execute(
            "INSERT INTO findings (run_id, repo_id, kind, severity, fingerprint, payload, "
            "                      created_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (run_id, IFNULL(repo_id, ''), kind, fingerprint) "
            "DO UPDATE SET payload = excluded.payload, created_at = excluded.created_at",
            row,
        )
        await conn.execute(
            "UPDATE phases SET pr_url = ?, updated_at = ? "
            " WHERE run_id = ? AND repo_id = ? AND phase = ?",
            (draft.url, _iso(now), run_id, draft.repo_id, int(Phase.VERIFY)),
        )

    await writer.submit(unit)


async def _record_verification(
    writer: StateWriter,
    run_id: str,
    repo_id: str,
    report: VerificationReport,
    *,
    seed: str,
    now: datetime,
) -> None:
    """Persist one `VerificationReport` plus the `rdeps_sample_seed` the model has no column for."""
    payload = json.dumps(
        {"report": report.model_dump(mode="json"), "rdeps_sample_seed": seed}, sort_keys=True
    )
    row = (
        run_id,
        repo_id,
        VERIFICATION_KIND,
        "info",
        _fingerprint(run_id, repo_id, VERIFICATION_KIND),
        redact_text(payload),
        _iso(now),
    )

    async def unit(conn: aiosqlite.Connection) -> None:
        await conn.execute(
            "INSERT INTO findings (run_id, repo_id, kind, severity, fingerprint, payload, "
            "                      created_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (run_id, IFNULL(repo_id, ''), kind, fingerprint) "
            "DO UPDATE SET payload = excluded.payload, created_at = excluded.created_at",
            row,
        )

    await writer.submit(unit)


async def _verifications(
    conn: aiosqlite.Connection, run_id: str
) -> dict[str, tuple[VerificationReport, str]]:
    rows = await _rows(
        conn,
        "SELECT repo_id, payload FROM findings WHERE run_id = ? AND kind = ? ORDER BY repo_id",
        (run_id, VERIFICATION_KIND),
    )
    out: dict[str, tuple[VerificationReport, str]] = {}
    for row in rows:
        envelope = json.loads(str(row[1]))
        out[str(row[0])] = (
            VerificationReport.model_validate(envelope["report"]),
            str(envelope.get("rdeps_sample_seed") or ""),
        )
    return out


async def _pr_sync_impl(
    opts: GlobalOptions, settings: FleetSettings, path: Path, *, run_id: str
) -> dict[str, object]:
    """§3.4 step 5, invoked once. Poll every non-terminal PR, write what the forge says, emit.

    This function is the whole of the fix for the wave-boundary deadlock. Before it, `MERGED` was
    a state three gates read and nothing wrote: wave 0's PRs opened, nothing ever polled GitHub,
    and every later wave's Phase 4 precondition failed forever — 242 of 250 repos stranded behind
    8 that had actually landed. The driver `pr.forge` names reads each PR back (`gh pr view
    --json state,mergedAt,mergeCommit`, or Gitea's `GET …/pulls/{index}`) and its pure parser —
    `vcs/github.parse_pr_view`, `vcs/gitea.parse_pr_json` — decides the state; an unknown state
    RAISES rather than defaulting to OPEN, since a wrong OPEN blocks every dependent forever. The
    result is persisted through the single writer, and every
    transition INTO `MERGED` emits a `pr_merged` event — the event §3.4 says unblocks the
    dependent and fires §3.5.1's T1, rather than a worker's guess that it might have.

    Terminal PRs are not re-polled: `NON_TERMINAL_STATES` is the filter, and spending a `git_net`
    slot on an answer that cannot change is what makes a 250-PR fleet cost one call per PR per
    `pr.poll_interval_s`.
    """
    log_configure(level=opts.log_level)
    conn = await connect_ro(path)
    try:
        records = await _pr_records(conn, run_id)
    finally:
        await conn.close()

    pollable = {
        repo_id: draft
        for repo_id, draft in records.items()
        if draft.url and draft.state in NON_TERMINAL_STATES
    }
    gh = _forge(settings)
    try:
        statuses: tuple[PrStatus, ...] = await gh.sync(
            PrSyncItem(url=draft.url or "", state=draft.state) for draft in pollable.values()
        )
    except ForgeError as exc:
        raise PrEmissionError(
            f"PR state ingestion failed: {exc}. Nothing was written; §3.4 step 5 reports what the "
            "forge said or it reports nothing — it never falls back to 'unchanged'."
        ) from exc

    observed = {status.url: status for status in statuses}
    merged: list[str] = []
    closed: list[str] = []
    unchanged: list[str] = []
    stamp = _now()
    async with StateWriter(path, owner="fleet-pr-sync") as writer:
        read_conn = await connect_ro(path)
        try:
            repository = SqliteStateRepository(writer=writer, read_conn=read_conn)
            for repo_id, draft in sorted(pollable.items()):
                status = observed.get(draft.url or "")
                if status is None or status.state is draft.state:
                    unchanged.append(repo_id)
                    continue
                # Re-validated rather than `model_copy`d: the state write that unblocks a wave is
                # the last one that should skip its model's own rails.
                updated = PullRequestDraft.model_validate(
                    draft.model_dump() | {"state": status.state}
                )
                await _write_pr_record(writer, run_id, updated, now=stamp)
                if status.state is PrState.MERGED:
                    merged.append(repo_id)
                    await repository.append_event(
                        EventRow(
                            run_id=run_id,
                            seq=0,  # allocated IN-STATEMENT by append_event (§6)
                            ts=_iso(stamp),
                            level="info",
                            event="pr_merged",
                            event_uid=str(uuid4()),
                            repo_id=repo_id,
                            phase=Phase.VERIFY,
                            payload=json.dumps(
                                {
                                    "url": status.url,
                                    "merge_commit_sha": status.merge_commit_sha,
                                    "merged_at": (
                                        None
                                        if status.merged_at is None
                                        else _iso(status.merged_at)
                                    ),
                                },
                                sort_keys=True,
                            ),
                        )
                    )
                elif status.state is PrState.CLOSED:
                    closed.append(repo_id)
        finally:
            await read_conn.close()

    return {
        "run_id": run_id,
        "polled": sorted(pollable),
        "merged": sorted(merged),
        "closed": sorted(closed),
        "unchanged": sorted(unchanged),
        "terminal": sorted(set(records) - set(pollable)),
        "exit_code": int(ExitCode.SUCCESS),
    }


def _pr_sync_lines(result: Mapping[str, object]) -> list[str]:
    polled = cast(Sequence[str], result["polled"])
    merged = cast(Sequence[str], result["merged"])
    closed = cast(Sequence[str], result["closed"])
    return [
        f"pr --sync: polled {len(polled)} open PR(s); {len(merged)} newly MERGED, "
        f"{len(closed)} CLOSED",
        *(f"  merged {repo}" for repo in merged),
    ]


# --------------------------------------------------------------------------------------
# step 4 — PR emission, stacked, and never over an unmerged dependency
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _PrCandidate:
    """One repo `fleet pr` could ship, with everything its body is rendered from."""

    repo_id: str
    wave_index: int
    status: RepoStatus
    source_url: str
    source_sha: str
    report: VerificationReport
    seed: str
    stub_states: dict[str, StubState]
    stub_fidelity: dict[str, StubFidelity]
    dependencies: tuple[str, ...]


async def _pr_candidates(
    conn: aiosqlite.Connection,
    settings: FleetSettings,
    run_id: str,
    wave: int | None,
    only: str | None,
) -> tuple[tuple[_PrCandidate, ...], tuple[str, ...]]:
    """`(candidates, unverified)` — the repos §3.4 step 4 may ship, and the ones it may not.

    A repo reaches step 4 only from a Phase 4 row that is `SUCCEEDED` or `DEGRADED`. `DEGRADED` is
    deliberately included and is the §3.5 escape hatch: it migrated against a stub, so its PR is
    opened as a draft carrying the stub banner rather than not opened at all.

    A repo whose Phase 4 succeeded but whose `VerificationReport` was never persisted is returned
    as `unverified` and shipped by nothing: every verdict field in the body comes from that
    report, and inventing a PASS to fill the gap is the one error a reviewer cannot catch by
    reading the PR (Rule 11).
    """
    statuses = {
        str(row[0]): RepoStatus(str(row[1]))
        for row in await _rows(
            conn,
            "SELECT repo_id, status FROM phases WHERE run_id = ? AND phase = ? "
            "   AND status IN ('SUCCEEDED','DEGRADED')",
            (run_id, int(Phase.VERIFY)),
        )
    }
    waves = {
        str(row[0]): int(row[1])
        for row in await _rows(
            conn,
            "SELECT node_id, wave_index FROM wave_members "
            " WHERE run_id = ? AND node_kind = 'REPO'",
            (run_id,),
        )
    }
    facts = {
        str(row[0]): (str(row[1]), None if row[2] is None else str(row[2]))
        for row in await _rows(conn, "SELECT repo_id, url, head_sha FROM repos")
    }
    stub_state: dict[str, dict[str, StubState]] = {}
    stub_tier: dict[str, dict[str, StubFidelity]] = {}
    for row in await _rows(
        conn,
        "SELECT repo_id, stub_coord_key, state, stub_fidelity FROM stubs "
        " WHERE run_id = ? AND state IN ('ACTIVE','SUPERSEDED') ORDER BY repo_id, stub_coord_key",
        (run_id,),
    ):
        stub_state.setdefault(str(row[0]), {})[str(row[1])] = StubState(str(row[2]))
        stub_tier.setdefault(str(row[0]), {})[str(row[1])] = StubFidelity(str(row[3]))

    dependencies: dict[str, set[str]] = {}
    for dependency, dependent in await _ordering_pairs(conn, settings, run_id):
        dependencies.setdefault(dependent, set()).add(dependency)

    reports = await _verifications(conn, run_id)
    candidates: list[_PrCandidate] = []
    unverified: list[str] = []
    for repo_id, status in sorted(statuses.items()):
        if only is not None and not fnmatch(repo_id, only):
            continue
        if wave is not None and waves.get(repo_id) != wave:
            continue
        found = reports.get(repo_id)
        url, sha = facts.get(repo_id, ("", None))
        if found is None or not url or not sha:
            unverified.append(repo_id)
            continue
        report, seed = found
        stubs = stub_state.get(repo_id, {})
        candidates.append(
            _PrCandidate(
                repo_id=repo_id,
                wave_index=waves.get(repo_id, 0),
                status=status,
                source_url=redact_text(url),
                source_sha=sha,
                report=_report_with_stubs(report, stubs, stub_tier.get(repo_id, {})),
                seed=seed,
                stub_states=stubs,
                stub_fidelity=stub_tier.get(repo_id, {}),
                dependencies=tuple(sorted(dependencies.get(repo_id, set()))),
            )
        )
    return tuple(candidates), tuple(unverified)


def _report_with_stubs(
    report: VerificationReport,
    states: Mapping[str, StubState],
    fidelity: Mapping[str, StubFidelity],
) -> VerificationReport:
    """Re-derive `equivalence` against the stub rows as they stand NOW (§3.5.1).

    The report was assembled when Phase 4 ran; a stub row's lifecycle keeps moving afterwards, and
    `equivalence` is what forces the draft. Re-validating rather than mutating is what re-runs
    `_derive_equivalence`, so `STUB_LIMITED` is derived from the data exactly as §5.1 requires and
    never assigned by this caller.
    """
    if not states:
        return report
    return VerificationReport.model_validate(
        report.model_dump()
        | {
            "verified_against_stubs": sorted(states),
            "stub_fidelity": {key: fidelity[key] for key in sorted(states) if key in fidelity},
        }
    )


async def _pr_impl(
    opts: GlobalOptions,
    settings: FleetSettings,
    path: Path,
    *,
    run_id: str,
    wave: int | None,
    only: str | None,
    ready: bool,
    dry_run: bool,
) -> dict[str, object]:
    """§3.4 step 4 over one fleet: one PR per eligible repo, stacked, through `PrwriterWorker`.

    Eligibility is the ADR-0011 stacking rule read off INGESTED state: a repo ships only when
    every ordering dependency has a `PullRequestDraft` whose state is `MERGED`. Anything else is
    HELD — reported, not failed, and not shipped — because the dependency may merge in five
    minutes and burning a repo's attempt on someone else's review latency is what
    `pr.merge_wait_timeout_s` exists to bound. The worker then applies the same rule a second time
    against what the forge says right now, which is the difference between "observed" and
    "assumed" that §3.4 step 5 is written to enforce.
    """
    log_configure(level=opts.log_level)
    conn = await connect_ro(path)
    try:
        candidates, unverified = await _pr_candidates(conn, settings, run_id, wave, only)
        records = await _pr_records(conn, run_id)
    finally:
        await conn.close()

    eligible: list[_PrCandidate] = []
    already: list[str] = []
    held: dict[str, list[str]] = {}
    for candidate in candidates:
        existing = records.get(candidate.repo_id)
        if existing is not None and existing.url:
            already.append(candidate.repo_id)
            continue
        blocking = sorted(
            dep
            for dep in candidate.dependencies
            if dep not in records or records[dep].state is not PrState.MERGED
        )
        if blocking:
            held[candidate.repo_id] = blocking
            continue
        eligible.append(candidate)
    eligible.sort(key=lambda item: (item.wave_index, item.repo_id))

    opened: dict[str, str] = {}
    drafted: list[str] = []
    failed: dict[str, str] = {}
    if eligible and not dry_run:
        opened, drafted, held_late, failed = await _emit_prs(
            settings, path, run_id=run_id, candidates=eligible, records=records, ready=ready
        )
        held.update(held_late)

    return {
        "run_id": run_id,
        "opened": dict(sorted(opened.items())),
        "draft": sorted(drafted),
        "eligible": [candidate.repo_id for candidate in eligible],
        "held": dict(sorted(held.items())),
        "already_open": sorted(already),
        "unverified": list(unverified),
        "failed": dict(sorted(failed.items())),
        "dry_run": dry_run,
        "exit_code": int(ExitCode.SUCCESS if not failed else ExitCode.UNEXPECTED_ERROR),
    }


async def _drain_llm_findings(ctx: RunContext) -> None:
    """Persist what `ctx`'s LLM client buffered, before the `StateWriter` closes.

    **Every `RunContext` gets an `LlmFindingSink` wired into its client** (`context.py`), because
    `on_drift` / `on_failover` are synchronous callbacks that can only buffer. `PhaseRunner`
    drains its own after each dispatch — but a command that drives a worker WITHOUT a
    `PhaseRunner` has no drain at all, and `fleet pr` is exactly that shape: it builds a full
    `RunContext` and calls `PrwriterWorker.run` directly. A local endpoint serving `pr_body` at
    PROMPTED while `models.yaml` promises JSON_SCHEMA would emit a `CapabilityDrift` for every PR
    in the fleet, and all of them would be discarded when the writer closed — "computed, then
    discarded", the defect this sink exists to close, in a shipped command.

    Swallowed and surfaced, for the same reason and in the same shape as
    `PhaseRunner._drain_llm_findings` and `obs/events.py`: this is telemetry, and a failed
    diagnostics write must not turn a fleet of successfully-opened PRs into a failed command.
    `flush()` re-buffers what did not land, so nothing is lost that a later drain could save.
    """
    try:
        await ctx.llm_findings.flush()
    except Exception as exc:
        ctx.log.error(  # noqa: TRY400 - §11.4: no formatted traceback in a durable record
            "llm_findings_flush_failed",
            run_id=str(ctx.run_id),
            pending=ctx.llm_findings.pending,
            exception_type=f"{type(exc).__module__}.{type(exc).__qualname__}",
            error=str(exc),
        )


async def _emit_prs(
    settings: FleetSettings,
    path: Path,
    *,
    run_id: str,
    candidates: Sequence[_PrCandidate],
    records: Mapping[str, PullRequestDraft],
    ready: bool,
) -> tuple[dict[str, str], list[str], dict[str, list[str]], dict[str, str]]:
    """Compose the run and dispatch `PrwriterWorker` once per eligible repo, in stack order.

    `gh` runs with the MONOREPO working copy as its cwd, not a per-repo worktree: the PR is
    against the monorepo's integration branch, so that is the repository whose remote `gh` must
    resolve. `_monorepo_checkout` is therefore the same exit-2 precondition Phase 3 has.
    """
    config = settings.config
    pr_root = (settings.root / config.run.work_dir).resolve() / "pr"
    _monorepo, monorepo_path, _lock_dir = await _monorepo_checkout(settings)
    pool = new_cpu_pool(config.concurrency.cpu_pool_workers)
    opened: dict[str, str] = {}
    drafted: list[str] = []
    held: dict[str, list[str]] = {}
    failed: dict[str, str] = {}
    try:
        async with StateWriter(path, owner="fleet-pr") as writer:
            read_conn = await connect_ro(path)
            try:
                repository = SqliteStateRepository(writer=writer, read_conn=read_conn)
                ledger = CostLedger(
                    repository,
                    run_id=run_id,
                    ceilings=Ceilings.from_settings(config.budgets, config.stubs),
                    clock=_now,
                )
                ctx = RunContext(
                    run_id=UUID(run_id),
                    config=config,
                    writer=writer,
                    repository=repository,
                    read_conn=read_conn,
                    ledger=ledger,
                    limits=Limits.create(config.concurrency, ledger=ledger, cpu_pool=pool),
                    llm=llm_router(settings),
                    log=default_logger("fleet.pr"),
                    work_dir=pr_root,
                    clock=_now,
                    harness_version=HARNESS_VERSION,
                )
                worker = PrwriterWorker(runner=GH_RUNNER)
                try:
                    for candidate in candidates:
                        outcome = await _emit_one_pr(
                            ctx,
                            worker,
                            settings,
                            candidate,
                            records=records,
                            monorepo_path=monorepo_path,
                            pr_root=pr_root,
                            ready=ready,
                        )
                        if isinstance(outcome, str):
                            failed[candidate.repo_id] = outcome
                            continue
                        if outcome.held:
                            held[candidate.repo_id] = list(outcome.unmerged_dependencies)
                            continue
                        if outcome.pr is None:  # pragma: no cover - `ok` implies a draft record
                            failed[candidate.repo_id] = "the worker returned ok with no PR record"
                            continue
                        await _write_pr_record(writer, run_id, outcome.pr, now=_now())
                        opened[candidate.repo_id] = outcome.pr.url or ""
                        if outcome.draft:
                            drafted.append(candidate.repo_id)
                finally:
                    # `finally`, not "after the loop": `_write_pr_record` and `_emit_one_pr` can
                    # both raise, and a drain placed after the loop would discard the buffer on
                    # exactly the runs that failed partway — a narrower copy of the defect this
                    # drain was added to fix. The writer is still open here (it closes with the
                    # `async with` two frames out), so this is the last point at which the
                    # buffered findings can still be persisted.
                    await _drain_llm_findings(ctx)
            finally:
                await read_conn.close()
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    return opened, drafted, held, failed


async def _emit_one_pr(
    ctx: RunContext,
    worker: PrwriterWorker,
    settings: FleetSettings,
    candidate: _PrCandidate,
    *,
    records: Mapping[str, PullRequestDraft],
    monorepo_path: Path,
    pr_root: Path,
    ready: bool,
) -> PrwriterOutput | str:
    """One repo's PR, or a string naming why it could not be opened."""
    config = settings.config
    try:
        payload = PrwriterInput(
            report=candidate.report,
            wave_index=candidate.wave_index,
            branch=f"migrate/{candidate.repo_id}",
            base=config.pr.base,
            source_url=candidate.source_url,
            source_sha=candidate.source_sha,
            dependencies=[
                DependencyPr(
                    repo_id=dep,
                    url=records[dep].url if dep in records else None,
                    state=records[dep].state if dep in records else PrState.DRAFTED,
                )
                for dep in candidate.dependencies
            ],
            repo_status=candidate.status,
            stub_states=candidate.stub_states,
            stub_fidelity=candidate.stub_fidelity,
            rdeps_sample_seed=candidate.seed,
            ready=ready,
            log_dir=str(pr_root / "bodies"),
            # §3.4 through whichever forge `pr.forge` names. The token is NOT here and cannot be:
            # `forge_token_config` is the PATH of the mode-600 `curl -K` file, and a payload is a
            # persisted object (§6) exactly as `attempts.command` is.
            gh_repo=config.pr.forge_repo or None,
            forge=config.pr.forge,
            forge_url=config.pr.forge_url,
            forge_owner=config.pr.forge_owner,
            forge_token_config=str(_forge_token_config(settings) or ""),
        )
    except ValidationError as exc:
        return f"the PR payload does not validate: {exc}"

    worker_ctx = ctx.worker_context(
        repo_id=candidate.repo_id,
        phase=Phase.VERIFY,
        attempt=1,
        lease_fence=0,
        cancel=asyncio.Event(),
        budget=CallBudget(
            remaining_tokens=config.budgets.task_max_tokens,
            remaining_usd=config.budgets.repo_max_cost_usd,
            deadline=loop_now() + float(config.budgets.task_max_wallclock_s.verify),
        ),
    )
    # `gh` must run inside the monorepo: the PR's base is the monorepo's integration branch, and
    # `ctx.worktree()` points at this run's per-repo scratch directory, which is not a checkout of
    # the repository the PR belongs to.
    worker_ctx.workdir = str(monorepo_path)

    if not await worker.preconditions_hold(worker_ctx, payload):
        return (
            f"§3.4's preconditions do not hold: verdict={candidate.report.verdict!r}, "
            f"status={candidate.status.value}. A PR opened on a FAIL report would present a red "
            "verification as reviewable work."
        )
    result = await worker.run(worker_ctx, payload)
    output = result.output
    if output is None:  # pragma: no cover - the worker always returns its output
        return f"the prwriter worker returned {result.status!r} with no output"
    if result.ok or output.held:
        return output
    error = result.error
    if error is None:  # pragma: no cover - a non-ok result always carries its error
        return f"forge {payload.forge!r} failed with no error attached"
    return f"{error.exception_type}: {error.stderr_tail}"


def _pr_lines(result: Mapping[str, object]) -> list[str]:
    opened = cast(Mapping[str, str], result["opened"])
    held = cast(Mapping[str, Sequence[str]], result["held"])
    drafts = cast(Sequence[str], result["draft"])
    lines = [
        f"pr: opened {len(opened)} PR(s) ({len(drafts)} draft), held {len(held)}, "
        f"{len(cast(Sequence[str], result['already_open']))} already open"
    ]
    lines += [f"  {repo} -> {url}" for repo, url in sorted(opened.items())]
    lines += [
        f"  HELD {repo}: dependency PR not MERGED — {', '.join(deps)}"
        for repo, deps in sorted(held.items())
    ]
    lines += [
        f"  SKIPPED {repo}: no persisted VerificationReport"
        for repo in cast(Sequence[str], result["unverified"])
    ]
    return lines


async def _refuse_unresolved_stubs(
    conn: aiosqlite.Connection, run_id: str, repo: str | None
) -> None:
    sql = (
        "SELECT consumer_repo_id, stub_coord_key, state FROM stubs "
        " WHERE run_id = ? AND state IN ('ACTIVE','SUPERSEDED')"
    )
    params: tuple[object, ...] = (run_id,)
    if repo is not None:
        sql += " AND consumer_repo_id = ?"
        params = (run_id, repo)
    rows = await _rows(conn, sql + " ORDER BY consumer_repo_id, stub_coord_key", params)
    if rows:
        listed = ", ".join(f"{row[0]}→{row[1]}({row[2]})" for row in rows[:5])
        raise UsageError(
            f"`fleet pr --ready` refused: {len(rows)} stub(s) still ACTIVE or SUPERSEDED "
            f"({listed}). §3.5.1 — only a green revalidation round retires a stub; there is no "
            "CLI path from 'the operator is tired of waiting' to 'verified'."
        )


# --------------------------------------------------------------------------------------
# fleet status
# --------------------------------------------------------------------------------------


@app.command()
def status(
    ctx: typer.Context,
    output_format: Annotated[
        OutputFormat, typer.Option("--format")
    ] = OutputFormat.TABLE,
    filters: Annotated[list[str] | None, typer.Option("--filter", help="status=X | wave=N")] = None,
    sort: Annotated[str | None, typer.Option("--sort", help="blast-radius")] = None,
    digest: Annotated[bool, typer.Option("--digest", help="Emit the §11.6 run_digest.")] = False,
    watch: Annotated[bool, typer.Option("--watch", help="Re-render until interrupted.")] = False,
    interval: Annotated[float, typer.Option("--watch-interval")] = 5.0,
    metrics: Annotated[bool, typer.Option("--metrics", help="Liveness projection (§10).")] = False,
    metrics_out: Annotated[
        Path | None, typer.Option("--metrics-out", help="Prometheus text format file.")
    ] = None,
) -> None:
    """Read-only projection of SQLite. An OUTPUT, not a service — nothing listens on a port."""
    opts = _options(ctx)
    with _mapped_errors():
        path = _require_db(opts)
        _check_schema_version(path)
        selectors = _parse_filters(filters or ())
        while True:
            _run(
                _status_once(
                    opts,
                    path,
                    output_format=output_format,
                    selectors=selectors,
                    sort=sort,
                    digest=digest,
                    metrics=metrics,
                    metrics_out=metrics_out,
                )
            )
            if not watch:
                return
            try:
                time.sleep(max(interval, 0.1))
            except KeyboardInterrupt:  # pragma: no cover - operator-driven
                return


def _parse_filters(raw: Sequence[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in raw:
        key, sep, value = item.partition("=")
        if not sep or key not in {"status", "wave"}:
            raise UsageError(f"--filter {item!r}: expected `status=X` or `wave=N` (§10)")
        out[key] = value
    return out


async def _status_once(
    opts: GlobalOptions,
    path: Path,
    *,
    output_format: OutputFormat,
    selectors: Mapping[str, str],
    sort: str | None,
    digest: bool,
    metrics: bool,
    metrics_out: Path | None,
) -> None:
    conn = await connect_ro(path)
    try:
        run_id = await _resolve_run(conn, opts)
        if digest:
            computed = await run_digest(conn, UUID(run_id))
            _emit(
                opts,
                {"run_id": run_id, "digest": computed.digest, "sections": computed.sections},
                [f"run_digest {computed.digest}"],
            )
            return

        state = await build_state(conn, UUID(run_id))
        rows = [
            {
                "repo": repo_id,
                "phase": int(repo.phase),
                "status": repo.status.value,
                "attempts": repo.attempts,
                "wave": repo.wave_index,
                "blast_radius": repo.blast_radius,
                "blocked_by": list(repo.blocked_by),
                "stubbed_deps": list(repo.stubbed_deps),
            }
            for repo_id, repo in sorted(state.repos.items())
        ]
        if "status" in selectors:
            rows = [r for r in rows if r["status"] == selectors["status"]]
        if "wave" in selectors:
            rows = [r for r in rows if str(r["wave"]) == selectors["wave"]]
        if sort == "blast-radius":
            rows.sort(key=lambda r: (-int(str(r["blast_radius"] or 0)), str(r["repo"])))

        measurements = await _metrics(conn, run_id) if (metrics or metrics_out) else {}
        if metrics_out is not None:
            metrics_out.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(metrics_out, _prometheus(measurements))
            _echo(f"wrote {len(measurements)} metrics to {metrics_out}")

        if output_format is OutputFormat.DOT:
            _echo(await _dot(conn, run_id))
            return
        payload: dict[str, object] = {
            "run_id": run_id,
            "budget_remaining_usd": state.budget_remaining_usd,
            "repos": rows,
        }
        if measurements:
            payload["metrics"] = measurements
        lines = [f"run {run_id}  ({len(rows)} repos)"]
        lines += [
            f"  {r['repo']:<32} phase={r['phase']} {r['status']:<28} "
            f"wave={r['wave']} attempts={r['attempts']} blast={r['blast_radius']}"
            for r in rows
        ]
        if metrics:
            lines += [f"  metric {name} = {value}" for name, value in sorted(measurements.items())]
        _emit(opts, payload, lines)
    finally:
        await conn.close()


async def _metrics(conn: aiosqlite.Connection, run_id: str) -> dict[str, float]:
    """§10's liveness projection: what a multi-day run needs to know it is still moving."""
    out: dict[str, float] = {}
    ledger = await _rows(
        conn,
        "SELECT spent_usd, reserved_usd, max_usd FROM budget_ledger WHERE run_id = ?",
        (run_id,),
    )
    if ledger:
        out["fleet_budget_spent_usd"] = float(ledger[0][0])
        out["fleet_budget_reserved_usd"] = float(ledger[0][1])
        out["fleet_budget_max_usd"] = float(ledger[0][2])

    statuses = await _rows(
        conn, "SELECT status, COUNT(*) FROM phases WHERE run_id = ? GROUP BY status", (run_id,)
    )
    for name, count in statuses:
        out[f"fleet_phase_status_total{{status=\"{name}\"}}"] = float(count)

    started = await _rows(
        conn, "SELECT started_at FROM runs WHERE run_id = ?", (run_id,)
    )
    done = await _rows(
        conn,
        "SELECT COUNT(*) FROM phases WHERE run_id = ? AND status = 'SUCCEEDED'",
        (run_id,),
    )
    if started and done:
        elapsed_h = max(
            (_now() - datetime.fromisoformat(str(started[0][0]))).total_seconds() / 3600.0, 1e-9
        )
        out["fleet_repos_per_hour"] = float(done[0][0]) / elapsed_h

    events = await _rows(
        conn,
        "SELECT event, COUNT(*) FROM events WHERE run_id = ? "
        "   AND event IN ('rate_limited','backend_failover') GROUP BY event",
        (run_id,),
    )
    for name, count in events:
        out[f"fleet_events_total{{event=\"{name}\"}}"] = float(count)
    return out


def _prometheus(measurements: Mapping[str, float]) -> str:
    """Prometheus text format. An output file — §14.5 stands, nothing listens on a port."""
    lines = ["# TYPE fleet_metrics gauge"]
    lines += [f"{name} {value!r}" for name, value in sorted(measurements.items())]
    return "\n".join(lines) + "\n"


async def _dot(conn: aiosqlite.Connection, run_id: str) -> str:
    """§10: repos as ellipses, contracts as boxes, suppressed cycle-break edges in red."""
    repos = await _rows(conn, "SELECT repo_id FROM repos ORDER BY repo_id")
    contracts = await _rows(
        conn, "SELECT contract_id FROM contracts WHERE run_id = ? ORDER BY contract_id", (run_id,)
    )
    edges = await _rows(
        conn,
        "SELECT src_id, dst_id, ordering_suppressed, retargeted_from_repo_id, confidence "
        "  FROM edges WHERE run_id = ? AND dst_id IS NOT NULL ORDER BY edge_key",
        (run_id,),
    )
    out = ["digraph fleet {"]
    out += [f'  "{row[0]}" [shape=ellipse];' for row in repos]
    out += [f'  "{row[0]}" [shape=box];' for row in contracts]
    for src, dst, suppressed, retargeted, confidence in edges:
        attrs = []
        if bool(suppressed):
            attrs.append("color=red")
        elif retargeted is not None:
            attrs.append("color=blue")
        if float(confidence) < 0.5:
            attrs.append("style=dashed")
        suffix = f" [{', '.join(attrs)}]" if attrs else ""
        out.append(f'  "{src}" -> "{dst}"{suffix};')
    out.append("}")
    return "\n".join(out)


# --------------------------------------------------------------------------------------
# fleet quarantine — the config-edit-free removal (§10)
# --------------------------------------------------------------------------------------


@app.command()
def quarantine(
    ctx: typer.Context,
    repo: Annotated[str, typer.Argument(help="repo_id to remove from the fleet.")],
    reason: Annotated[str, typer.Option("--reason", help="Required; recorded verbatim.")],
    stub_blocked: Annotated[bool, typer.Option("--stub-blocked")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """Remove one pathological repo from the fleet mid-run **without editing config**.

    Editing `config/repos.yaml` instead would trip config-drift detection, whose only escape
    (`--force-config-drift`) silently accepts every other co-edited change too — which is how an
    operator loses a run. So the removal is recorded as state: an audited `OperatorQuarantine`
    finding carrying the reason, `RepoStatus.SKIPPED`, and `blocked_by` propagated to the
    dependents exactly as an abandonment does (§3.5).
    """
    opts = _options(ctx)
    with _mapped_errors():
        path = _require_db(opts)
        _check_schema_version(path)
        result = _run(
            _quarantine_impl(opts, path, repo=repo, reason=reason, dry_run=dry_run)
        )
        _ = stub_blocked
        _emit(
            opts,
            result,
            [
                f"{'would quarantine' if dry_run else 'quarantined'} {repo}: "
                f"{result['phases_skipped']} phase row(s) SKIPPED, "
                f"{result['dependents_blocked']} dependent(s) blocked"
            ],
        )


async def _quarantine_impl(
    opts: GlobalOptions, path: Path, *, repo: str, reason: str, dry_run: bool
) -> dict[str, object]:
    if not reason.strip():
        raise UsageError("--reason is required and must not be empty (§10): it is the audit record")

    conn = await connect_ro(path)
    try:
        run_id = await _resolve_run(conn, opts)
        known = await _rows(conn, "SELECT repo_id FROM repos WHERE repo_id = ?", (repo,))
        if not known:
            raise UsageError(f"no repo {repo!r} in {path}: `fleet status` lists the fleet")
        phase_rows = await _rows(
            conn,
            "SELECT phase, status FROM phases WHERE run_id = ? AND repo_id = ? ORDER BY phase",
            (run_id, repo),
        )
        edge_rows = await _rows(
            conn,
            "SELECT src_id, dst_id FROM edges "
            " WHERE run_id = ? AND dst_id IS NOT NULL AND ordering_suppressed = 0 "
            "   AND src_kind = 'REPO' AND dst_kind = 'REPO'",
            (run_id,),
        )
    finally:
        await conn.close()

    movable = [
        (int(phase), RepoStatus(str(state)))
        for phase, state in phase_rows
        if RepoStatus(str(state)) not in TERMINAL_STATUSES
    ]
    for _, state in movable:
        transition(state, RepoStatus.SKIPPED)  # the single gate; raises before anything is written

    # `ordering_descendants` wants (dependency, dependent). An `edges` row is "src depends on
    # dst", so the pair is reversed here — the closure must answer "who breaks if `repo` is
    # abandoned", which is its dependents, not its dependencies (§3.5).
    descendants = ordering_descendants(
        [(str(dst), str(src)) for src, dst in edge_rows]
    )
    dependents = sorted(set(descendants(repo)) - {repo})

    if dry_run:
        return {
            "dry_run": True,
            "run_id": run_id,
            "repo": repo,
            "reason": reason,
            "phases_skipped": len(movable) or 1,
            "dependents_blocked": len(dependents),
            "dependents": dependents,
        }

    now = _now()
    stamp = _iso(now)
    payload = json.dumps(
        {"repo_id": repo, "reason": reason, "operator": True, "at": stamp}, sort_keys=True
    )

    async with StateWriter(path, owner="fleet-quarantine") as writer:

        async def unit(db: aiosqlite.Connection) -> int:
            await db.execute(
                "INSERT INTO findings (run_id, repo_id, kind, severity, fingerprint, payload,"
                "                      created_at) "
                "VALUES (?, ?, 'OperatorQuarantine', 'warn', ?, ?, ?) "
                "ON CONFLICT (run_id, IFNULL(repo_id, ''), kind, fingerprint) "
                "DO UPDATE SET payload = excluded.payload, created_at = excluded.created_at",
                (run_id, repo, _fingerprint(run_id, repo, reason), redact_text(payload), stamp),
            )
            if movable:
                await db.executemany(
                    "UPDATE phases SET status = 'SKIPPED', updated_at = ? "
                    " WHERE run_id = ? AND repo_id = ? AND phase = ?",
                    [(stamp, run_id, repo, phase) for phase, _ in movable],
                )
                return len(movable)
            # No phase row yet: "out of the fleet" still has to be durable somewhere, and §10
            # names RepoStatus.SKIPPED as where. Phase 1 is where a repo enters the run.
            await db.execute(
                "INSERT INTO phases (run_id, repo_id, phase, status, updated_at) "
                "VALUES (?, ?, 1, 'SKIPPED', ?) "
                "ON CONFLICT (run_id, repo_id, phase) DO UPDATE SET status = 'SKIPPED', "
                "    updated_at = excluded.updated_at",
                (run_id, repo, stamp),
            )
            return 1

        skipped = await writer.submit(unit)

        read_conn = await connect_ro(path)
        try:
            from fleet.orchestrator.scheduler import SqliteSchedulerStore

            store = SqliteSchedulerStore(writer=writer, read_conn=read_conn)
            blocked = 0
            for dependent in dependents:
                if await store.append_blocked_by(run_id, dependent, repo, now=now):
                    blocked += 1
        finally:
            await read_conn.close()

    with suppress(Exception):  # a projection is an OUTPUT; it must not fail the quarantine
        await project_once(path, run_id=UUID(run_id), path=DEFAULT_PROJECTION_PATH)

    return {
        "dry_run": False,
        "run_id": run_id,
        "repo": repo,
        "reason": reason,
        "phases_skipped": skipped,
        "dependents_blocked": blocked,
        "dependents": dependents,
    }


# --------------------------------------------------------------------------------------
# fleet abort
# --------------------------------------------------------------------------------------


@app.command()
def abort(
    ctx: typer.Context,
    drain: Annotated[bool, typer.Option("--drain/--now")] = True,
    reason: Annotated[str | None, typer.Option("--reason")] = None,
) -> None:
    """Stop the run deliberately, checkpoint, regenerate `migration_state.json`, exit 0.

    `--drain` (the default) lets in-flight work finish inside `budgets.wave_drain_timeout_s`;
    `--now` cancels immediately. Either way the run stops ADMITTING first — an abort that left
    admission open would race the drain it just started.
    """
    opts = _options(ctx)
    with _mapped_errors():
        settings = _load_settings(opts)
        path = _require_db(opts)
        _check_schema_version(path)
        result = _run(_abort_impl(opts, settings, path, drain=drain, reason=reason))
        _emit(
            opts,
            result,
            [
                f"run {result['run_id']} aborted "
                f"({'drained' if drain else 'cancelled'}); "
                f"{result['running_reset']} RUNNING row(s) reset, "
                f"projection at {result['projection']}"
            ],
        )


# The §11.5 crash sweep, shared by `fleet abort` (every RUNNING row, the run is stopping) and
# `fleet resume` step 3 (only the stale ones, below). Factored rather than copied because the two
# must agree on exactly which columns are cleared: a resume that reset `status` without clearing
# the lease would re-admit a repo whose original container still holds `migrate/<repo>`.
#
# `attempts` is deliberately ABSENT from the SET list, and that omission is the point of the
# statement. A crash — or a deliberate abort — is not an attempt the repo made, so charging one
# would spend a rung of ADR-0014's ladder on something that never ran; three resumes would then
# exhaust a three-rung ladder and send a healthy repo to REQUIRES_HUMAN_INTERVENTION having
# failed nothing. §11.5 says "retaining `attempts`" for exactly this reason.
#
# The `lease_fence` bump is what makes the reclaim safe rather than merely optimistic: every
# mutating statement the previous holder issues carries `AND lease_fence = ?` with the fence it
# was granted at claim time, so after the bump its next write matches zero rows and it aborts
# (§6, §11.5) instead of writing into a worktree that has just been handed back.
_RESET_RUNNING_TO_PENDING_SQL: Final = (
    "UPDATE phases SET status = 'PENDING', lease_owner = NULL, heartbeat_at = NULL, "
    "       lease_expires_at = NULL, lease_fence = lease_fence + 1, updated_at = ? "
    " WHERE run_id = ? AND status = 'RUNNING'"
)

#: Appended to the statement above by `fleet resume`, and to the COUNT that previews it under
#: `--dry-run`. Abort stops the whole run so it reclaims unconditionally; a resume may be racing a
#: worker that is still alive and heart-beating, and reclaiming that one produces the two-writer
#: collision the lease exists to prevent.
#:
#: **Two horizons, and the gate is their CONJUNCTION — never either one alone.** The harness
#: carries two answers to "is this worker dead":
#:
#: * `run.stale_after_s`, live config, named by §11.5 step 3 itself; and
#: * `phases.heartbeat_ttl_seconds`, per row, which `PhaseRecord.is_stale` (`models/state.py`)
#:   compares against and which `settings.py` says exists "captured per phase so a config change
#:   cannot retroactively declare a live worker dead".
#:
#: They can disagree, because that second promise is **not kept today**: `claim_phase`
#: (`state/repository.py`) writes `lease_owner`, `lease_fence`, `lease_expires_at`, `heartbeat_at`
#: and `started_at`, and never `heartbeat_ttl_seconds` — so every row carries the schema's
#: hardcoded `DEFAULT 300` no matter what the operator configured (recorded in
#: docs/INTEGRATION_HONESTY.md). Reading live config alone is therefore an active hazard: an
#: operator who sets `run.stale_after_s: 30` to make the reaper responsive would have a resume
#: reclaim rows that `PhaseRecord.is_stale` and `reap_expired_phase_leases` both still call ALIVE,
#: which is the two-writer collision on `migrate/<repo>` the lease exists to prevent.
#:
#: So a row is reclaimed only when it is stale by BOTH clocks. Lowering `stale_after_s` can then
#: never reclaim a live lease (the per-row TTL still guards it), and raising it only makes a
#: resume MORE conservative than the reaper — the safe direction for a step whose failure mode is
#: two writers on one worktree. When `claim_phase` is fixed to stamp the column, the two collapse
#: back into one number and this conjunction becomes a no-op rather than a wrong answer.
#:
#: `heartbeat_at IS NOT NULL` mirrors `PhaseRecord.is_stale`, which reports a NULL heartbeat as
#: NOT stale: a RUNNING row with no heartbeat is a row `claim_phase` is still mid-write on, not a
#: corpse. The config cutoff is computed in Python and compared as TEXT, which is sound because
#: every persisted instant is one fixed-width UTC rendering (`_iso`); the per-row half needs
#: arithmetic against a column, so it uses `julianday`, which parses that same rendering.
#: Both are evaluated inside the writer's statement, not read-then-written, so a worker that
#: heartbeats mid-resume cannot be reclaimed on a stale read.
_STALE_HEARTBEAT_PREDICATE: Final = (
    " AND heartbeat_at IS NOT NULL "
    "   AND heartbeat_at < ? "
    "   AND (julianday(?) - julianday(heartbeat_at)) * 86400.0 > heartbeat_ttl_seconds"
)

#: Which `phases` rows §11.5 step 2 treats as *claiming* a sandbox, and therefore which
#: `fleet-<run_id>-*` worktrees and containers it must NOT reap. Same two horizon parameters as
#: the predicate above, in the same order.
#:
#: **"Live" is defined as "a row step 3 would not reclaim", and it is built FROM step 3's
#: predicate rather than restated beside it.** A step-2 reader that drifted LOOSER than step 3's
#: would delete the checkout of a worker step 3 is about to declare alive — the two-writer
#: collision inverted, with the worktree gone instead of doubly written. Restating the two-clock
#: conjunction in a second string literal is exactly how that drift happens, so this composes the
#: one string that already exists. `NOT (1 <predicate>)` is the composition: the constant opens
#: with ` AND `, so it needs a left operand, and SQLite has no boolean literal — `1` supplies one
#: without changing the truth value. The NULL heartbeat case survives the negation intact: SQL
#: `AND` with a false operand is false regardless of NULLs, so a row `_STALE_HEARTBEAT_PREDICATE`
#: calls not-stale is a row this calls live.
#:
#: **The negation is load-bearing ONLY under `--dry-run`, and must not be deleted as dead code.**
#: Step 2 runs AFTER `_reset_stale_running` (ADR-0081), so in a real resume every stale row has
#: already become PENDING and `status = 'RUNNING'` alone would give the same answer. `--dry-run`
#: does not run the sweep — it only counts what the sweep would reclaim — so without the negation
#: a preview would report every crashed run's own orphans as live and preview reaping nothing,
#: which is precisely the reading an operator runs the health check to get right.
#:
#: `lease_owner IS NOT NULL` is deliberately NOT part of this. It would narrow "live", and
#: narrowing "live" is the dangerous direction here: every row it excluded would become reapable.
_LIVE_SANDBOX_PREDICATE: Final = (
    " AND status = 'RUNNING' AND NOT (1" + _STALE_HEARTBEAT_PREDICATE + ")"
)


async def _abort_impl(
    opts: GlobalOptions,
    settings: FleetSettings,
    path: Path,
    *,
    drain: bool,
    reason: str | None,
) -> dict[str, object]:
    conn = await connect_ro(path)
    try:
        run_id = await _resolve_run(conn, opts)
    finally:
        await conn.close()

    stamp = _iso(_now())
    detail = reason or "operator abort"
    async with StateWriter(path, owner="fleet-abort") as writer:

        async def unit(db: aiosqlite.Connection) -> int:
            await db.execute(
                "INSERT INTO findings (run_id, repo_id, kind, severity, fingerprint, payload,"
                "                      created_at) "
                "VALUES (?, NULL, 'OperatorAbort', 'warn', ?, ?, ?) "
                "ON CONFLICT (run_id, IFNULL(repo_id, ''), kind, fingerprint) "
                "DO UPDATE SET payload = excluded.payload, created_at = excluded.created_at",
                (
                    run_id,
                    _fingerprint(run_id, detail, stamp),
                    redact_text(
                        json.dumps(
                            {
                                "reason": detail,
                                "drain": drain,
                                "drain_timeout_s": settings.config.budgets.wave_drain_timeout_s,
                            },
                            sort_keys=True,
                        )
                    ),
                    stamp,
                ),
            )
            # Roll uncommitted tasks back onto `phases.base_ref`: the row returns to PENDING with
            # its `attempts` retained (§11.5 crash sweep), and the fence bump invalidates the old
            # holder's next write.
            cursor = await db.execute(_RESET_RUNNING_TO_PENDING_SQL, (stamp, run_id))
            await db.execute(
                "UPDATE runs SET finished_at = ? WHERE run_id = ?", (stamp, run_id)
            )
            return cursor.rowcount

        reset = await writer.submit(unit)

    projection = await project_once(path, run_id=UUID(run_id), path=DEFAULT_PROJECTION_PATH)
    return {
        "run_id": run_id,
        "drain": drain,
        "reason": detail,
        "running_reset": reset,
        "projection": str(projection),
    }


# --------------------------------------------------------------------------------------
# fleet resume
# --------------------------------------------------------------------------------------


@app.command()
def resume(
    ctx: typer.Context,
    from_phase: Annotated[int | None, typer.Option("--from-phase", min=1, max=4)] = None,
    repo: Annotated[str | None, typer.Option("--repo")] = None,
    reset_attempts: Annotated[bool, typer.Option("--reset-attempts")] = False,
    accept_drift: Annotated[
        list[str] | None,
        typer.Option("--accept-drift", help="Accept exactly one config section. Repeatable."),
    ] = None,
    force_config_drift: Annotated[bool, typer.Option("--force-config-drift")] = False,
    raise_budget: Annotated[float | None, typer.Option("--raise-budget")] = None,
    raise_wave_budget: Annotated[float | None, typer.Option("--raise-wave-budget")] = None,
    repoll_prs: Annotated[bool, typer.Option("--repoll-prs")] = False,
    revalidation: Annotated[Revalidation | None, typer.Option("--revalidation")] = None,
    raise_revalidation_rounds: Annotated[
        int | None, typer.Option("--raise-revalidation-rounds")
    ] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    no_continue: Annotated[
        bool,
        typer.Option(
            "--no-continue",
            help="Reconcile and stop at exit 0; do not run §11.5 step 8 (ADR-0080).",
        ),
    ] = False,
) -> None:
    """Reconcile after a crash and continue from each repo's re-entry floor (§11.5 step 5),
    never the earliest incomplete phase.

    The three refusals that make a resume safe, in order: **config drift** per section, so
    `--accept-drift budgets` accepts exactly `budgets` and still refuses everything else (a single
    opaque hash forces an operator to accept every co-edited change in order to accept one); the
    **wave cost ceiling**, because the ledger is durable and re-entering an exhausted wave halts
    again forever without `--raise-wave-budget`; and the **schema version**, which a resume reads
    and never upgrades. Reads nothing from `migration_state.json` (§11.5).

    **What is built today.** §11.5 steps 1 (its config-digest half only — nothing here reads
    `runs.harness_version`, the other half step 1 names), 2 (reap the orphan containers and
    worktrees no live `phases` row claims), 3 (stale `RUNNING` → `PENDING`, retaining
    `attempts`), 4 (ask Git whether each `RUNNING` task's commit landed and correct the row to
    match, charging no attempt either way), 5 (re-check each phase's durable evidence and demote
    every repo to its re-entry floor), 6 (recompute `blocked_by`, and move the repos that frees
    into an appended `waves.synthetic = 1` row), 7 (regenerate `migration_state.json`) and **8
    (continue)** run, plus `--repoll-prs`, `--raise-budget` and `--raise-wave-budget`. §10's
    `stub_reconcile` step is still absent, and D80 in `docs/INTEGRATION_HONESTY.md` is its
    record: this verb continues, and it does not reconcile stubs. (§10's, not §11.5's: D80's
    heading rules the attribution, and §11.5 contains the string zero times.)

    **Step 8, and what it will and will not serve (ADR-0080).** The continuation re-enters
    `fleet transform`/`build`/`verify`'s own composition roots, every servable phase from the
    lowest servable floor UPWARD in ascending phase order — not the distinct floors present,
    which would leave a repo transformed and never built at exit 0 — and it hands the FIRST
    halting delegate's exit code back unchanged (§10). A repo
    whose floor is `Phase.SCAN` is **reported and skipped, never served** — `fleet scan` takes
    five flags no resume carries and no `wave` at all, so serving that floor means step 8 choosing
    five values the operator never wrote — and the skipped repos are named under
    `scan_floor_not_continued`. Every per-phase knob the delegates need is resolved from the
    DECLARED config (`transform.max_attempts`, `budgets.build_timeout_s`, `verify.rdeps_limit`,
    `verify.rdeps_sample_n`, `verify.affected_only`), never from a literal chosen here.

    `--dry-run` is the free health check §11.5 promises: it makes no network call, writes
    nothing, previews the step-3 sweep, the step-4 arbitration, the step-5 demotion plan (both of
    them every git READ, no git or SQL write), the step-6 un-blocking plan — the same
    `plan_unblocking` call the write path makes, over the same inputs — and the step-2 reap, and
    it never continues. `--no-continue` is the other half of that pair and is not a preview: the
    reconciliation is WRITTEN, and only step 8 is withheld, at exit 0. It is what an operator
    wants after a crash — "reconcile my run, do not spend money yet" — and it is what the
    §11.5-step tests in `tests/test_cli.py` pass so that asserting on a reconciliation does not
    also drive four phases.

    **One consequence of step 5 that this verb does not repair, stated because nothing else
    reports it (ADR-0089 §4).** A wave is `CLOSED` iff every one of its `wave_members` rows is
    settled — `orchestrator.scheduler.wave_state` computes it from `phases`, and no column
    stores it. A demotion writes `SUCCEEDED → PENDING`, so a repo demoted out of a closed wave
    re-opens that wave. `docs/SPEC.md` §3.5's "closed waves are never re-opened" governs
    un-blocking, and §11.5 step 5 does not mention waves at all.
    """
    opts = _options(ctx)
    with _mapped_errors():
        settings = _load_settings(opts)
        path = _require_db(opts)
        _check_schema_version(path)
        _refuse_unbuilt_resume_flags(
            from_phase=from_phase,
            repo=repo,
            reset_attempts=reset_attempts,
            revalidation=revalidation,
            raise_revalidation_rounds=raise_revalidation_rounds,
        )
        result = _run(
            _resume_impl(
                opts,
                settings,
                path,
                accept_drift=tuple(accept_drift or ()),
                force_config_drift=force_config_drift,
                raise_budget=raise_budget,
                raise_wave_budget=raise_wave_budget,
                repoll_prs=repoll_prs,
                dry_run=dry_run,
            )
        )
        # A forge failure is a REAL failure (exit 1) and outranks the continuation: it means the
        # PR re-poll the operator asked for did not happen, and step 8 must not spend money on a
        # fleet whose merge state the harness could not read.
        failure = result["pr_sync_error"]
        withheld = dry_run or no_continue or isinstance(failure, str)

        # ONE payload and ONE `_emit`, in a `finally`, and both halves of that are load-bearing.
        # One payload, because `fleet resume --json` is parsed with a single `json.loads` and a
        # second document on the same stdout is not JSON. In a `finally`, because the durability
        # promise §11.5 rests on is that the reconciliation is REPORTED even when what comes
        # after it does not survive: `_continue_impl` raises nothing of its own, but the phase
        # impls it delegates to raise plenty, and an operator who loses the step-4 arbitration
        # because a Phase-3 build threw has lost the only record of it.
        continuation: dict[str, object] | None = None
        try:
            if not withheld:
                continuation = _run(
                    _continue_impl(
                        opts,
                        settings,
                        path,
                        run_id=str(result["run_id"]),
                        floors=_floors_from(result),
                        only=None,
                        # Every knob is the operator's DECLARED value, never a literal chosen
                        # here: a default invented at this call site is the recorded `effort:
                        # low` defect (`src/fleet/models/tasks.py`), where deleting a value from
                        # config substituted one the operator never wrote. `dry_run=False` is not
                        # such a choice — `--dry-run` returns above without continuing at all.
                        ladder=settings.config.transform.max_attempts,
                        dry_run=False,
                        timeout_s=settings.config.budgets.build_timeout_s,
                        # `sandboxed` has no config form. This is not a chosen value but the
                        # ABSENCE of `fleet build --no-sandbox`, whose own help says "CI only";
                        # a continuation that dropped the sandbox silently would be strictly more
                        # dangerous than the command it re-enters.
                        sandboxed=True,
                        rdeps_limit=settings.config.verify.rdeps_limit,
                        rdeps_sample_n=settings.config.verify.rdeps_sample_n,
                        affected_only=settings.config.verify.affected_only,
                    )
                )
        finally:
            _emit(
                opts,
                {**result, "continuation": continuation},
                [*_resume_lines(result), *_continue_lines(continuation, withheld=withheld)],
            )

        if isinstance(failure, str):
            raise PrEmissionError(
                f"{failure}\n"
                "§11.5 steps 3 and 7 ran anyway and are committed: the stale-lease sweep and "
                "`migration_state.json` are purely local, and a flag added to do MORE must never "
                "subtract the steps a plain `fleet resume` would have done. Fix the forge access "
                "and re-run; the re-poll is the only part that did not happen. Step 8 did NOT "
                "run: continuing over a fleet whose merge state the harness could not read is "
                "how a dependent is transformed against a provider that merged an hour ago."
            )
        if continuation is not None:
            _raise_for_continuation(continuation)


def _floors_from(result: Mapping[str, object]) -> dict[str, Phase]:
    """Step 5's computed floors, re-typed off the payload step 8 is handed.

    Read from `computed_floors` rather than from `reentry_floors`: the report's `unchanged` rows
    carry a *reason* and no floor, so a repo step 5 confirmed in place would silently drop out of
    the continuation plan — the fleet would resume minus every repo that needed no demotion,
    which is most of it. `_demote_to_floors` returns the mapping separately for exactly this
    reason and `_resume_impl` carries it through by phase NAME, so the payload stays JSON.
    """
    raw = cast("Mapping[str, str]", result["computed_floors"])
    return {repo_id: Phase[name] for repo_id, name in raw.items()}


def _continue_lines(
    continuation: Mapping[str, object] | None, *, withheld: bool
) -> list[str]:
    """§11.5 step 8's human rendering, including the case where it did not run.

    A silent absence is the failure mode this exists to prevent: "no continuation happened" and
    "the continuation drove nothing" are the same empty output and opposite facts, and the
    `scan_floor_not_continued` key is only load-bearing if an operator reading the table sees it.
    """
    if continuation is None:
        return ["step 8: withheld" if withheld else "step 8: did not complete"]
    driven = cast("list[str]", continuation["driven"])
    skipped = cast("list[str]", continuation[_SCAN_SKIPPED_KEY])
    halted = continuation["halted_phase"]
    lines = [
        f"step 8: continued {len(driven)} phase(s)"
        + (f" ({', '.join(driven)})" if driven else "")
        + (f", halted in {halted}" if halted is not None else "")
    ]
    if skipped:
        lines.append(
            f"step 8: {len(skipped)} repo(s) NOT continued — their re-entry floor is SCAN and "
            f"`fleet scan` takes flags no resume carries: {', '.join(sorted(skipped))}"
        )
    return lines


async def _resume_impl(
    opts: GlobalOptions,
    settings: FleetSettings,
    path: Path,
    *,
    accept_drift: Sequence[str],
    force_config_drift: bool,
    raise_budget: float | None,
    raise_wave_budget: float | None,
    repoll_prs: bool,
    dry_run: bool,
) -> dict[str, object]:
    # The three refusals run in §11.5's order — config, then profile, then the durable wave
    # ledger — inside ONE read handle. A resume that reported the budget before the drift would
    # send an operator to raise a ceiling for a run it is about to refuse anyway.
    conn = await connect_ro(path)
    try:
        run_id = await _resolve_run(conn, opts)
        rows = await _rows(conn, "SELECT config_digests FROM runs WHERE run_id = ?", (run_id,))
        baseline: Mapping[str, str] = json.loads(str(rows[0][0]) or "{}") if rows else {}
        drifted = settings.drifted_sections(baseline)
        wave = await _earliest_open_wave(conn, run_id)

        accepted = _validate_accept_drift(settings, accept_drift)
        unaccepted = tuple(name for name in drifted if name not in accepted)
        if unaccepted and not force_config_drift:
            raise UsageError(
                f"config drift in section(s) {', '.join(unaccepted)} since run {run_id} started. "
                "Accept them one at a time with `--accept-drift <section>` (each writes its own "
                "audited ConfigDrift finding), or `--force-config-drift` to accept everything — "
                "which also accepts every OTHER co-edited change, and is how a run is lost (§10)."
            )
        if opts.profile is not None and not force_config_drift:
            raise UsageError(
                f"--profile {opts.profile!r} on a resume is a config drift the resume must be "
                "told about (§10): it is refused without --force-config-drift, and writes a "
                "ProfileChanged finding when accepted."
            )
        await _refuse_exhausted_wave(
            conn, settings, run_id, wave, raise_to_usd=raise_wave_budget
        )
    finally:
        await conn.close()

    # `--raise-budget` is validated read-only FIRST, in both modes. A `--dry-run` that returned
    # 0 for a figure the real run would refuse is the failure this whole preview exists to catch.
    ledger: _LedgerRow | None = None
    if raise_budget is not None:
        ledger = await _with_ro(path, lambda c: _refuse_bad_raise_budget(c, run_id, raise_budget))

    if not dry_run and accepted:
        await _record_drift_findings(path, run_id, accepted, settings)
    if not dry_run and raise_wave_budget is not None and wave is not None:
        await _raise_wave_ceiling(path, run_id, wave, raise_wave_budget)
    if not dry_run and raise_budget is not None:
        await _raise_run_ceiling(path, run_id, raise_budget)

    # §3.4: "`fleet resume` runs one synchronous sync before re-validating preconditions." This is
    # `fleet pr --sync`'s code path invoked once — NOT a second sync — so `MERGED` still has
    # exactly one writer. It is opt-in because §11.5 promises steps 1–7 make no network call, and
    # that promise is what makes `--dry-run` a free health check; `--dry-run --repoll-prs`
    # therefore skips it rather than quietly reaching the forge, and SAYS it skipped it — a
    # `"pr_sync": null` an operator cannot tell apart from "the flag was never passed" is the same
    # silent-discard defect `_refuse_unbuilt_resume_flags` exists to kill.
    #
    # A forge failure is caught rather than propagated, and this is the reason: steps 3 and 7 are
    # purely local, and the natural command after a crash — `fleet resume --repoll-prs`, on a host
    # whose `gh` credentials expired in the meantime — would otherwise reconcile NOTHING, leaving
    # every dead worker's row RUNNING. A flag that adds a step must never subtract the steps that
    # would have run without it. Nothing is swallowed (Rule 11): the message is carried out in the
    # payload and re-raised as exit 1 by the caller, after the reconciliation has committed.
    pr_sync: dict[str, object] | None = None
    pr_sync_error: str | None = None
    if not repoll_prs:
        repoll = "not-requested"
    elif dry_run:
        repoll = "skipped-dry-run"
    else:
        try:
            pr_sync = await _pr_sync_impl(opts, settings, path, run_id=run_id)
            repoll = "polled"
        except PrEmissionError as exc:
            pr_sync_error = str(exc)
            repoll = "failed"

    # ---------------------------------------------------------------------------------
    # `stub_reconcile` (§3.5.1, §13 row 45) BELONGS HERE — immediately below the re-poll and
    # above the step-3 sweep — and nowhere earlier. It walks `ix_stubs_open` and writes one
    # `UnresolvedStub` finding per open row, which is what makes the run exit 7. Run before the
    # re-poll, it would report as "a human is needed" every stub whose resolving PR a human had
    # ALREADY merged and the harness simply had not observed yet, and exit 7 would come to mean
    # "the fleet gave up waiting" instead of "a human is needed".
    #
    # RELATIVE ORDER IS NOT ENOUGH, because the line above is CONDITIONAL. `repoll` is
    # `"not-requested"` on a plain `fleet resume` (the flag is opt-in), `"skipped-dry-run"` under
    # `--dry-run`, and `"failed"` when the forge refused — in all three the PR state
    # `stub_reconcile` would judge is whatever was last observed, possibly hours stale, and row 45
    # fires exactly as if the re-poll had been ordered after it. So whoever lands it must ALSO
    # decide what it does when `repoll != "polled"`: either gate it on that, or make
    # `--repoll-prs` implied by it. It does not exist on `main` yet; when it lands, it goes on the
    # next line, with that decision made explicitly rather than inherited from this ordering.
    # ---------------------------------------------------------------------------------

    # §11.5 step 3. `cutoff` is the config horizon `run.stale_after_s` names; `now` drives the
    # per-row `heartbeat_ttl_seconds` half. Both are required — see `_STALE_HEARTBEAT_PREDICATE`.
    now = _now()
    horizons = (_iso(now - timedelta(seconds=settings.config.run.stale_after_s)), _iso(now))

    if dry_run:
        stale = await _with_ro(path, lambda conn: _count_stale_running(conn, run_id, horizons))
    else:
        stale = await _reset_stale_running(path, run_id, horizons)

    # §11.5 step 4 — ask Git, per ambiguous task, whether its commit landed, and correct the row
    # to match.
    #
    # It runs BELOW the step-3 sweep so that the rows it reads are already normalised: a crashed
    # phase is `PENDING` by the time step 4 asks about its tasks, rather than a `RUNNING` row two
    # writers in one command are both touching. **That ordering is not what stops step 4 racing a
    # LIVE worker, and an earlier draft of this comment claimed it was.** Step 3 resets only STALE
    # `phases` rows and no `tasks` row at all, so the tasks of a worker alive by both clocks
    # survive it untouched; the gate is `_ARBITRATED_TASKS_SQL`'s own `lease_live` column, which
    # is composed from `_LIVE_SANDBOX_PREDICATE` and therefore agrees with step 2 and step 3 by
    # construction rather than by inspection. `horizons` is shared with them for the same reason.
    #
    # It runs ABOVE step 7 because the projection is regenerated from SQLite, and a projection
    # taken before the corrections would ship a `migration_state.json` that disagrees with
    # `tasks`/`phases` — the drift class §11.5's preamble says a resume removes.
    arbitration = await _reconcile_tasks_with_git(
        settings, path, run_id, horizons=horizons, dry_run=dry_run
    )

    # §11.5 step 5 — the demotion to each repo's re-entry floor — sits HERE, between step 4 and
    # the projection: it reads the `phases.post_commit_sha` pointers step 4 has just reconciled
    # against Git, and step 7 must publish the result of the demotion, not the state before it.
    # It also runs BELOW the step-3 sweep. **That ordering is not what makes the FLOOR right, and
    # an earlier draft of this comment claimed it was** — the same retraction the step-4 comment
    # eleven statements above already carries, for the same claim shape. `RUNNING` and `PENDING`
    # are BOTH outside `reentry._SETTLED_FOR_DEMOTION` (`{SUCCEEDED, SKIPPED, DEGRADED}`), so a
    # stale row is the backward search's frontier before step 3 as well as after; step 3 moves it
    # from one non-settled status to another. Measured over 17,680 inputs — every assignment of
    # `RepoStatus` to the four phases containing at least one `RUNNING` (7^4 - 6^4 = 1,105 row
    # states) x all 16 evidence subsets — the `RUNNING -> PENDING` rewrite changes `phase_floor`
    # in 0 and `demotable_phases` in 0. Both columns were validated against synthetic faults on
    # the same inputs: counting `RUNNING` as settled moves the floor in 5,312, and a membership
    # rule admitting `RUNNING` moves the plan in 10,736. The ordering STAYS — the two reasons
    # above it are the load-bearing ones — but nothing about the floor may be re-derived from it.
    floors, computed_floors = await _demote_to_floors(
        settings, path, run_id, dry_run=dry_run, now=now
    )

    # §11.5 step 6 — recompute `blocked_by` from `phases` + `edges` — sits HERE, between step 5
    # and the projection, and the ordering is load-bearing in BOTH directions.
    #
    # BELOW step 5, because `plan_unblocking` takes step 5's floors as an input it never
    # re-derives: `computed_floors` is the mapping the call above returned, and running step 6
    # first would have nothing to pass it. It also reads the `phases` rows step 5 has just
    # rewritten, so a repo demoted out of `SUCCEEDED` is judged on the status it now carries.
    #
    # ABOVE step 7, because the projection is regenerated FROM SQLite. Run after it, step 6's
    # `blocked_by` edits, its `BLOCKED -> PENDING` writes and its appended `waves` row would all
    # be invisible in the `migration_state.json` this same command publishes — the operator would
    # read a fleet still blocked by a dependency the run had already cleared, which is precisely
    # the drift class §11.5's preamble says a resume REMOVES.
    # `test_step_6_runs_between_step_5_and_the_projection_it_must_precede`, in
    # `tests/test_resume_unblocking.py`, asserts it against the published file rather than
    # against this comment.
    unblocked = await _unblock_dependents(
        settings, path, run_id, floors=computed_floors, dry_run=dry_run, now=now
    )

    projection: str | None = None
    if not dry_run:
        # §11.5 step 7 — the projection is an OUTPUT regenerated from SQLite, never an input.
        projection = str(
            await project_once(path, run_id=UUID(run_id), path=DEFAULT_PROJECTION_PATH)
        )

    # §11.5 step 2 — "reap containers and worktrees named `fleet-<run_id>-*` that no live
    # `phases` row claims" — deliberately placed AFTER the step-3 sweep above, departing from
    # §11.5's own step numbering. ADR-0081 records the decision: a crashed run's sandbox is still
    # claimed by a row that says RUNNING until the sweep resets it, so a reap ordered before the
    # sweep is a no-op on exactly the crashed runs the reap exists for.
    #
    # **Ordering alone is not the whole fix, and the ADR says so.** `--dry-run` skips the sweep
    # entirely, so under a preview this code runs against unswept rows however it is ordered.
    # That is why `_LIVE_SANDBOX_PREDICATE` negates step 3's staleness test rather than reading
    # `status = 'RUNNING'`, and it shares this function's single `now`: step 2 and step 3 must
    # agree about which rows are alive, and two `_now()` calls cannot be made to agree by
    # inspection.
    #
    # Sitting below `project_once` as well is immaterial and deliberate rather than overlooked:
    # this sweep touches the filesystem and the docker daemon and writes no `phases` row, so the
    # projection §11.5 step 7 regenerates from SQLite cannot differ either side of it. Splitting
    # the branch above to interleave them would buy a numbering that matches §11.5 and nothing
    # else.
    live_names = await _with_ro(path, lambda conn: _live_sandbox_names(conn, run_id, horizons))
    reaped_worktrees = await _reap_orphan_worktrees(settings, run_id, live_names, dry_run=dry_run)
    reaped_containers = await _reap_orphan_containers(run_id, live_names, dry_run=dry_run)

    return {
        "run_id": run_id,
        "drifted_sections": list(drifted),
        "accepted_sections": list(accepted),
        "earliest_open_wave": wave,
        "raise_budget": raise_budget,
        # What was ASKED is `raise_budget`; whether it HAPPENED is this. A payload that reported
        # only the request let `--dry-run --raise-budget 50 --json` assert a cleared halt beside
        # `"dry_run": true` while `budget_ledger.halted` was still 1.
        "raise_budget_applied": raise_budget is not None and not dry_run,
        "budget_ledger_before": None if ledger is None else ledger.payload(),
        "raise_wave_budget": raise_wave_budget,
        "raise_wave_budget_applied": raise_wave_budget is not None and not dry_run,
        "stale_running_reset": stale,
        # §11.5 step 4. The whole report, not a count: "2 tasks reconciled" cannot tell an
        # operator whether landed work was adopted or a worktree was discarded, and those are
        # opposite facts about the same run.
        "git_arbitration": arbitration,
        # §11.5 step 5, the whole report for the same reason step 4's is: "3 repos demoted"
        # cannot tell an operator whether landed work was discarded or a frontier was merely
        # confirmed, and `applied` is what separates the preview from the write (the defect
        # `raise_budget_applied` exists for, one key down).
        "reentry_floors": floors,
        # §11.5 step 5's floors as a MAPPING, which the report above is not: `unchanged` rows
        # carry a reason and no floor, so step 8 fed from the report would silently drop every
        # repo that needed no demotion. By phase NAME so the payload survives `--json`.
        "computed_floors": {repo_id: floor.name for repo_id, floor in computed_floors.items()},
        # §11.5 step 6, the whole report for step 5's reason: "2 repos un-blocked" cannot tell an
        # operator whether an audited quarantine was re-admitted or a subtree is still held, and
        # `applied` is what separates the preview from the write.
        "unblocked_dependents": unblocked,
        # §11.5 step 2. `live_sandbox_names` is in the payload because it is the input an
        # operator has to see to trust the other two keys: "0 orphans reaped" and "every orphan
        # was spared as live" are the same output with opposite meanings.
        "live_sandbox_names": sorted(live_names),
        "reaped_worktrees": reaped_worktrees,
        "reaped_containers": reaped_containers,
        "repoll_prs": repoll,
        "pr_sync": pr_sync,
        "pr_sync_error": pr_sync_error,
        "projection": projection,
        "dry_run": dry_run,
    }


def _resume_lines(result: Mapping[str, object]) -> list[str]:
    """Every flag the operator passed accounted for, including the ones that did nothing."""
    run_id = result["run_id"]
    stale = result["stale_running_reset"]
    dry = bool(result["dry_run"])
    lines = [
        f"dry-run: run {run_id} is resumable"
        if dry
        else f"run {run_id}: {stale} stale RUNNING row(s) reset to PENDING (attempts retained)"
    ]
    if dry:
        lines.append(
            f"  step 3: {stale} stale RUNNING row(s) would reset to PENDING (attempts retained)"
        )
    lines.extend(_arbitration_lines(result, dry=dry))
    lines.extend(_floor_lines(result, dry=dry))
    lines.extend(_unblock_lines(result, dry=dry))
    lines.extend(_reap_lines(result, dry=dry))
    lines.extend(_budget_lines(result))
    lines.extend(_repoll_lines(result))
    if not dry:
        lines.append(f"  projection at {result['projection']}")
    return lines


def _arbitration_lines(result: Mapping[str, object], *, dry: bool) -> list[str]:
    """§11.5 step 4, reported so the two verdicts can never read as each other.

    "reconciled 2 tasks" is the line this function refuses to print. Adopting landed work and
    discarding an unlanded worktree are opposite corrections, and an operator reading the second
    as the first goes looking for commits that were deliberately thrown away. Every `unresolved`
    entry is printed with its reason for the same reason `_reap_lines` prints every `failed`
    entry (docs/INTEGRATION_HONESTY.md D44): a candidate Git could not be asked about is still
    open work, and a silent count would let a partial reconciliation read as a complete one.
    """
    report = result["git_arbitration"]
    if not isinstance(report, Mapping):  # pragma: no cover - the payload always carries it
        return []
    candidates = int(cast(int, report["candidates"]))
    spared = cast(Sequence[Mapping[str, object]], report["spared_live"])
    missing = cast(Sequence[Mapping[str, object]], report["provenance_missing"])
    if not candidates and not spared:
        return []
    landed = cast(Sequence[Mapping[str, object]], report["landed"])
    discarded = cast(Sequence[Mapping[str, object]], report["discarded"])
    unresolved = cast(Sequence[Mapping[str, object]], report["unresolved"])
    recreated = cast(Sequence[Mapping[str, object]], report["anchors_recreated"])
    verb = "would ask" if dry else "asked"
    out = [
        f"  step 4: {verb} Git about {candidates} RUNNING task(s); "
        f"{len(spared)} spared as live"
    ]
    for entry in landed:
        adopted = "would adopt" if dry else "adopted"
        out.append(
            f"  step 4: {entry['repo_id']} phase {entry['phase']} task {entry['task_id']} landed "
            f"as {str(entry['commit_sha'])[:12]} — {adopted} as DONE, no attempt charged"
        )
    for entry in discarded:
        thrown = "would discard" if dry else "discarded"
        out.append(
            f"  step 4: {entry['repo_id']} phase {entry['phase']} task {entry['task_id']} did "
            f"NOT land — {thrown} the worktree back to its task anchor, row PENDING, no attempt "
            "charged"
        )
    for entry in recreated:
        made = "would re-create" if dry else "re-created"
        out.append(
            f"  step 4: {made} the missing anchor {entry['ref']} at "
            f"{str(entry['commit_sha'])[:12]}"
        )
    out.extend(
        f"  step 4: PROVENANCE {entry['repo_id']} phase {entry['phase']} task "
        f"{entry['task_id']} — {str(entry['commit_sha'])[:12]} reached "
        "`phases.post_commit_sha` but NO `attempts` row was open to record it; the two pointers "
        "§11.5 pairs now disagree"
        for entry in missing
    )
    out.extend(
        f"  step 4: UNRESOLVED {entry['repo_id']} phase {entry['phase']} task "
        f"{entry['task_id']} — {entry['reason']}"
        for entry in unresolved
    )
    out.extend(
        f"  step 4: spared {entry['repo_id']} phase {entry['phase']} task {entry['task_id']} — "
        "its phase lease is still live, so Git was not asked and nothing was discarded"
        for entry in spared
    )
    return out


def _floor_lines(result: Mapping[str, object], *, dry: bool) -> list[str]:
    """§11.5 step 5, reported per repo, because the three outcomes are not degrees of one thing.

    "3 repos demoted" is the line this function refuses to print, for `_arbitration_lines`' reason
    one step over: a demotion DISCARDS landed work and re-runs it, a repo left unchanged has a
    frontier that was merely confirmed, and a repo reported `UNRESOLVED` was not judged at all.
    An operator's next action differs for each, so each gets its own line with its own reason
    (docs/INTEGRATION_HONESTY.md D44).

    `applied` rather than `dry` decides the headline verb. The two can disagree: a real run whose
    every repo was already at its floor writes nothing, and printing "demoted" there would report
    a state change that did not happen — the defect `raise_budget_applied` exists for.
    """
    report = result["reentry_floors"]
    if not isinstance(report, Mapping):  # pragma: no cover - the payload always carries it
        return []
    demoted = cast(Sequence[Mapping[str, object]], report["demoted"])
    unchanged = cast(Sequence[Mapping[str, object]], report["unchanged"])
    unresolved = cast(Sequence[Mapping[str, object]], report["unresolved"])
    verb = "would demote" if dry else "demoted"
    out: list[str] = []
    if not demoted and not unresolved:
        out.append(
            f"  step 5: {int(cast(int, report['candidates']))} repo(s) already sit at their "
            "re-entry floor; nothing to demote"
        )
    for entry in demoted:
        phases = cast(Sequence[str], entry["phases"])
        evidence = cast(Mapping[str, bool], entry["evidence"])
        read = ", ".join(f"{name}={holds}" for name, holds in evidence.items())
        out.append(
            f"  step 5: {verb} {entry['repo_id']} to floor {entry['floor']} — "
            f"{', '.join(phases)} back to PENDING (attempts retained)"
            + (f"; evidence read {read}" if read else "")
        )
    out.extend(
        f"  step 5: {entry['repo_id']} unchanged — {entry['reason']}" for entry in unchanged
    )
    out.extend(
        f"  step 5: UNRESOLVED for {entry['repo_id']} — {entry['reason']}"
        for entry in unresolved
    )
    return out


def _reap_lines(result: Mapping[str, object], *, dry: bool) -> list[str]:
    """§11.5 step 2, reported so that a partial sweep can never read as a complete one.

    **The `failed` list is printed entry by entry with its reason, and that is the point of this
    function** (docs/INTEGRATION_HONESTY.md D44). `WorktreeManager.reap` and
    `ContainerSandbox.reap` both go to deliberate trouble to keep "removed", "attempted and
    unresolved" and "spared because a live row claims it" as three separate facts; a caller that
    printed only `reaped` would collapse them again one layer up and hand the operator a clean
    line for a sweep that left a container running or a checkout on disk. A count is not enough
    either — the operator's next action is `docker rm <name>` or `git worktree remove <name>`,
    and that needs the name.

    **"No orphans" is only ever printed when `failed` is empty too.** The first draft of this
    function printed the `reaped`-derived summary and the `failed` entries independently, so a
    sweep that attempted two worktrees and removed neither emitted `no orphan worktrees`
    followed by two `FAILED` lines — a headline contradicting the detail directly beneath it,
    which is the D44 collapse this function was written to prevent, reintroduced in the reporting
    layer. The clean line is now gated on both lists.

    **Each line names the namespace it searched**, because "0 orphans" and "I was looking in the
    wrong place" are the same output otherwise — see `_reap_orphan_worktrees` on why that
    distinction is not hypothetical here.
    """
    out: list[str] = []
    for kind, key in (("worktree", "reaped_worktrees"), ("container", "reaped_containers")):
        report = result[key]
        if not isinstance(report, Mapping):  # pragma: no cover - the payload always carries both
            continue
        skipped = report["skipped"]
        error = report["error"]
        scope = report["namespace"]
        reaped = cast(Sequence[str], report["reaped"])
        failed = cast(Sequence[Mapping[str, str]], report["failed"])
        if isinstance(skipped, str):
            out.append(f"  step 2: no {kind} sweep — {skipped}")
        elif isinstance(error, str):
            out.append(f"  step 2: the {kind} sweep did not run — {error}")
        elif reaped:
            verb = "would reap" if dry else "reaped"
            out.append(f"  step 2: {verb} {len(reaped)} orphan {kind}(s): {', '.join(reaped)}")
        elif not failed:
            out.append(f"  step 2: no orphan {kind}s in {scope}")
        else:
            out.append(f"  step 2: no orphan {kind} could be removed in {scope}")
        out.extend(
            f"  step 2: FAILED to reap {kind} {entry['name']} — {entry['reason']}"
            for entry in failed
        )
    return out


def _budget_lines(result: Mapping[str, object]) -> list[str]:
    ceiling = result["raise_budget"]
    before = result["budget_ledger_before"]
    if ceiling is None or not isinstance(before, Mapping):
        return []
    verb = "raised" if result["raise_budget_applied"] else "WOULD raise (nothing written)"
    halt = " and clear the sticky halt" if before["halted"] else ""
    return [
        f"  --raise-budget: {verb} max_usd ${float(before['max_usd']):.2f} -> "
        f"${float(cast(float, ceiling)):.2f}{halt}"
    ]


def _repoll_lines(result: Mapping[str, object]) -> list[str]:
    match result["repoll_prs"]:
        case "not-requested":
            return []
        case "skipped-dry-run":
            return [
                "  --repoll-prs: SKIPPED under --dry-run — it is the one network call on this "
                "verb, and §11.5 promises a resume's reconciliation makes none"
            ]
        case "failed":
            return [f"  --repoll-prs: FAILED — {result['pr_sync_error']}"]
        case _:
            sync = result["pr_sync"]
            return _pr_sync_lines(sync) if isinstance(sync, Mapping) else []


def _refuse_unbuilt_resume_flags(
    *,
    from_phase: int | None,
    repo: str | None,
    reset_attempts: bool,
    revalidation: Revalidation | None,
    raise_revalidation_rounds: int | None,
) -> None:
    """Exit 2 rather than accept a flag whose behaviour does not exist (Rule 11, "fail loud").

    Every flag below scopes or re-drives the continuation in a way §11.5 step 8 does not
    implement. **The clause that stood here until ADR-0080 said "the CONTINUATION this verb
    cannot perform", and step 8 falsified it**: the verb continues now. What each of these flags
    still names is absent for its own reason, and none of them is step 8's absence:
    `--from-phase` names a per-repo start OTHER than the re-entry floor, which is the one thing
    §11.5 step 5 exists to compute; `--repo` names a scope `_continue_impl` takes as `only` but
    that `_resume_impl` does not thread — reconciling the whole ledger and continuing a fifth of
    it is a resume whose two halves disagree about what run they are on; and `--reset-attempts`,
    `--revalidation` and `--raise-revalidation-rounds` name machinery (`attempts` rewriting, the
    §3.5.1 revalidation rounds) that has no implementation anywhere in `src/`.

    A parser that accepted `--from-phase 2` and then resumed from wherever it liked is worse than
    one that refuses: the operator believes they scoped the resume, and nothing tells them
    otherwise. `--raise-budget`, `--raise-wave-budget`, `--accept-drift`, `--repoll-prs`,
    `--no-continue` and `--dry-run` are NOT here — those are implemented and do what they say.
    """
    unbuilt = {
        "--from-phase": from_phase is not None,
        "--repo": repo is not None,
        "--reset-attempts": reset_attempts,
        "--revalidation": revalidation is not None,
        "--raise-revalidation-rounds": raise_revalidation_rounds is not None,
    }
    named = sorted(flag for flag, given in unbuilt.items() if given)
    if named:
        raise UsageError(
            f"{', '.join(named)} cannot be honoured: each one scopes or re-drives the "
            "continuation in a way §11.5 step 8 does not implement. Step 8 itself IS built and "
            "runs on every `fleet resume` this refusal does not stop (pass `--no-continue` to "
            "withhold it, or `--dry-run` to preview the reconciliation and write nothing). "
            "`--from-phase` names a per-repo start OTHER than the re-entry floor — the phase "
            "ABOVE the HIGHEST phase below the settled frontier whose evidence still holds or "
            "which is a DEGRADED/SKIPPED hard stop, never that phase itself, and SCAN if there "
            "is no such phase — which is the one thing §11.5 step 5 exists to compute; "
            "`--repo` names a scope the reconciliation does not carry, and a resume "
            "that reconciles the whole ledger and continues a fifth of it is a resume whose two "
            "halves disagree; `--reset-attempts`, `--revalidation` and "
            "`--raise-revalidation-rounds` name machinery with no implementation in `src/`. "
            "Accepting the flag and ignoring it would let an operator believe they had scoped "
            "the resume. Re-run without it to get the "
            "reconciliation that IS built (step 1's config digests, "
            "steps 2, 3, 4, 5, 6, 7 and 8, `--repoll-prs`, the budget raises)."
        )


async def _count_stale_running(
    conn: aiosqlite.Connection, run_id: str, horizons: tuple[str, str]
) -> int:
    """What `--dry-run` previews: the row count the sweep below would reclaim, read-only."""
    rows = await _rows(
        conn,
        "SELECT COUNT(*) FROM phases WHERE run_id = ? AND status = 'RUNNING'"  # noqa: S608
        + _STALE_HEARTBEAT_PREDICATE,
        (run_id, *horizons),
    )
    return 0 if not rows else int(rows[0][0])


async def _reset_stale_running(path: Path, run_id: str, horizons: tuple[str, str]) -> int:
    """§11.5 step 3: reclaim the leases of workers that died, and NOTHING else.

    `horizons` is `(config_cutoff, now)` — the two clocks `_STALE_HEARTBEAT_PREDICATE` requires a
    row to have outlived BOTH of. Returns the number of rows reclaimed. See
    `_RESET_RUNNING_TO_PENDING_SQL` for why `attempts` is untouched and why the fence bump is the
    part that makes this safe.
    """
    stamp = _iso(_now())
    async with StateWriter(path, owner="fleet-resume") as writer:

        async def unit(db: aiosqlite.Connection) -> int:
            cursor = await db.execute(
                _RESET_RUNNING_TO_PENDING_SQL + _STALE_HEARTBEAT_PREDICATE,
                (stamp, run_id, *horizons),
            )
            return int(cursor.rowcount)

        return await writer.submit(unit)


# --------------------------------------------------------------------------------------
# §11.5 step 4 — Git is the arbiter: did this task's commit land, or did it not?
# --------------------------------------------------------------------------------------


#: The tasks step 4 asks Git about, and the `lease_live` flag that decides whether it may act.
#:
#: **`RUNNING` tasks, MINUS those whose phase lease is still alive.** `RUNNING` is exactly the
#: status §3.2 step 6.5 writes in the same transaction that records `tasks.pre_commit_sha` — so a
#: `RUNNING` row is the one state in which BOTH questions this step asks have an anchor to be
#: asked against. A `CLAIMED` row has no `pre_commit_sha` yet and has therefore touched neither
#: the branch nor the worktree; `PENDING`, `DONE` and `FAILED` are settled. §11.5 step 4 names
#: `RUNNING` and `schema.sql`'s `ix_mutations_open` note ("asking git whether a RUNNING task's
#: commit is on the branch") names it again.
#:
#: **The liveness gate is NOT optional and it is NOT supplied by the step-3 sweep.** Step 3 resets
#: `phases` rows and only the STALE ones; a task row is untouched by it in either case. So without
#: this predicate the set step 4 acts on is precisely the complement of what step 3 protects: a
#: worker alive by both clocks, caught between `git apply` and `git commit`, presents exactly the
#: "nothing landed, worktree dirty" shape — and step 4 would `reset --hard` + `clean -fdx` its
#: live checkout. That is the two-writer collision the lease exists to prevent, with the worktree
#: destroyed instead of doubly written.
#:
#: It is **composed from** `_LIVE_SANDBOX_PREDICATE`, never restated beside it, for the reason
#: that constant already gives about step 3's: a second copy that drifted LOOSER would arbitrate a
#: worker step 3 calls alive. Composing it also inherits two properties that a hand-rolled lease
#: check gets wrong. `lease_owner`/`lease_expires_at` are NOT consulted — `complete_phase`
#: (`state/repository.py`) NULLs both in the same statement that sets the terminal status, so a
#: lease-based guard reads a finished phase as unclaimed and a mid-write one as abandoned. And the
#: negated staleness test, rather than `status = 'RUNNING'` alone, is what makes `--dry-run`
#: correct: the preview does not run the step-3 sweep, so it meets unswept rows and must reach the
#: same verdict the real run does.
#:
#: **NOT IMPLEMENTED, deliberately (ADR-0087 §4):** §11.5 step 4's *second* selector — "or any
#: `phases` row whose `post_commit_sha` does not resolve on its branch". A phase row with a
#: dangling pointer and no `RUNNING` task is not a candidate here. That correction reads and
#: rewrites the column §11.5 step 5's `evidence_holds` consumes, so it belongs with step 5; this
#: SQL is not a complete rendering of the step's candidate set and must not be read as one.
_ARBITRATED_TASKS_SQL: Final = (
    "SELECT t.task_id, t.repo_id, t.phase, t.pre_commit_sha, "  # noqa: S608 - a Final constant
    "       p.base_ref, p.pre_commit_sha, "
    "       ((t.repo_id, t.phase) IN (SELECT repo_id, phase FROM phases "
    "                                  WHERE run_id = ?" + _LIVE_SANDBOX_PREDICATE + ")) "
    "  FROM tasks t "
    "  LEFT JOIN phases p "
    "    ON p.run_id = t.run_id AND p.repo_id = t.repo_id AND p.phase = t.phase "
    " WHERE t.run_id = ? AND t.status = 'RUNNING' "
    " ORDER BY t.created_at, t.task_id"
)


async def _reconcile_tasks_with_git(
    settings: FleetSettings,
    path: Path,
    run_id: str,
    *,
    horizons: tuple[str, str],
    dry_run: bool,
) -> dict[str, object]:
    """§11.5 step 4: for every `RUNNING` task whose lease is NOT live, copy Git's answer down.

    **A driver over `vcs/commits.py`, not new machinery.** Two primitives are CALLED —
    `find_task_commit` ("Resume's ONLY question … There is no third answer") and `discard_task`
    (the no-branch) — and both already carry this step's contract in their own docstrings.
    `scoped_range` is reached only *inside* `find_task_commit`, not from here, and is named for
    what it constrains rather than as a call this function makes: the trailer search is scoped to
    the phase anchor, which is why `_recreate_phase_anchor` runs first. Nothing here
    re-implements a git read.

    **Two branches, because Git has two answers**, and the SPEC's "there is no third branch and no
    tree-SHA comparison" is about the *verdict*, not about the ask:

    * a SHA came back ⇒ the work is durable in Git. The task row goes `DONE`, the SHA is copied
      onto `attempts.commit_sha` and `phases.post_commit_sha`, and nothing re-runs.
    * nothing came back ⇒ nothing landed. `discard_task` resets the worktree to **the task's own
      anchor**, the row goes back to `PENDING`, and the rung re-runs.

    A candidate Git could not be *asked* about at all — no worktree on disk, no anchor to scope
    the range to, an unsettled probe — is reported in `unresolved` and left exactly as it was.
    That is not a third verdict; it is the absence of one, and collapsing it into either branch is
    the four-state collapse `RollbackIndeterminateError` exists to refuse one layer down. It would
    be a `DONE` row claiming a commit nobody found, or a discarded worktree on a hunch.

    **`phases.attempts` is never written by this function, in either branch.** §11.5 step 4 says
    so twice ("do not increment `attempts`" / "again without incrementing `attempts`"): a crash is
    not an attempt the repo made, and charging one here would spend ADR-0014's ladder on work that
    either already landed or never ran. The constraint is satisfied by *not writing the column* —
    no statement below names it — and `tests/test_cli.py` asserts on the column either side.

    **`tasks.pre_commit_sha` survives the discard branch deliberately.** After `discard_task` the
    branch tip *is* that anchor, so the value is still true; clearing it would leave a second
    crash — before the row is re-claimed and `record_task_anchor` re-reads the tip — with no
    anchor at all, and `discard_task` refuses to guess one. Whole-phase rollback is the call that
    clears it (`vcs/commits.rollback_phase`'s contract), and this is not that.

    **A task whose phase lease is still live is not arbitrated at all** — see
    `_ARBITRATED_TASKS_SQL` on why the step-3 sweep does not supply that gate. It is reported in
    `spared_live` rather than dropped, for the same reason step 2 puts `live_sandbox_names` in its
    payload: "0 tasks reconciled" and "every open task was spared as live" are the same output
    with opposite meanings, and only one of them means the operator has nothing to do.

    `--dry-run` runs every git READ and no git or SQL write: the verdicts are computed and
    reported, `discard_task` is not called, and the anchor is not re-created.
    """
    rows = await _with_ro(
        path, lambda conn: _rows(conn, _ARBITRATED_TASKS_SQL, (run_id, *horizons, run_id))
    )
    report: dict[str, object] = {
        "candidates": 0,
        "spared_live": [],
        "landed": [],
        "discarded": [],
        "anchors_recreated": [],
        "provenance_missing": [],
        "unresolved": [],
        "applied": False,
    }
    candidates = []
    for row in rows:
        target = {"task_id": str(row[0]), "repo_id": str(row[1]), "phase": int(row[2])}
        if int(row[6]):
            cast(list[object], report["spared_live"]).append(dict(target))
        else:
            candidates.append(row)
    report["candidates"] = len(candidates)
    if not candidates:
        return report

    work_root = (settings.root / settings.config.run.work_dir).resolve()
    landed: list[tuple[str, str, int, str]] = []
    discarded: list[tuple[str, str, int]] = []

    for task_id, repo_id, phase, task_anchor, base_ref, phase_anchor, _live in candidates:
        target = {"task_id": str(task_id), "repo_id": str(repo_id), "phase": int(phase)}
        worktree = work_root / str(repo_id)
        if not await asyncio.to_thread((worktree / ".git").exists):
            _unresolved(report, target, f"no worktree at {worktree}")
            continue
        git = Git(worktree)
        branch = f"migrate/{repo_id}"
        try:
            anchor = await _recreate_phase_anchor(
                git,
                report,
                run_id=run_id,
                repo_id=str(repo_id),
                phase=int(phase),
                base_ref=None if base_ref is None else str(base_ref),
                pre_commit_sha=None if phase_anchor is None else str(phase_anchor),
                dry_run=dry_run,
            )
            if anchor is None:
                _unresolved(report, target, "no phase anchor to scope the trailer search to")
                continue
            if await git.resolve(branch) is None:
                _unresolved(report, target, f"{branch} does not exist in {worktree}")
                continue
            sha = await find_task_commit(
                git, branch=branch, pre_commit_sha=anchor, task_id=str(task_id)
            )
            if sha is not None:
                landed.append((str(task_id), str(repo_id), int(phase), sha))
                cast(list[object], report["landed"]).append({**target, "commit_sha": sha})
                continue
            if task_anchor is None:
                _unresolved(report, target, "the task row carries no `pre_commit_sha` to reset to")
                continue
            if not dry_run:
                await discard_task(git, task_pre_commit_sha=str(task_anchor), branch=branch)
            discarded.append((str(task_id), str(repo_id), int(phase)))
            cast(list[object], report["discarded"]).append(dict(target))
        except (GitError, OSError) as exc:
            # Rule 11: a git call that failed or never settled is carried out verbatim, never
            # swallowed and never turned into a verdict. The row is untouched, so the next
            # resume asks again.
            _unresolved(report, target, f"{type(exc).__name__}: {exc}")

    if dry_run or not (landed or discarded):
        return report
    report["applied"] = True
    unwritten = await _persist_arbitration(path, run_id, landed=landed, discarded=discarded)
    for task_id, repo_id, phase, sha in unwritten:
        cast(list[object], report["provenance_missing"]).append(
            {"task_id": task_id, "repo_id": repo_id, "phase": phase, "commit_sha": sha}
        )
    return report


def _unresolved(report: dict[str, object], target: Mapping[str, object], reason: str) -> None:
    """One place to append to `unresolved`, so no branch can forget the `reason`.

    A name and no reason is the D44 collapse: the operator's next action depends entirely on
    whether the worktree is missing, the anchor is gone, or git refused.
    """
    cast(list[object], report["unresolved"]).append({**target, "reason": redact_text(reason)})


async def _recreate_phase_anchor(
    git: Git,
    report: dict[str, object],
    *,
    run_id: str,
    repo_id: str,
    phase: int,
    base_ref: str | None,
    pre_commit_sha: str | None,
    dry_run: bool,
) -> str | None:
    """"The anchor ref itself is re-created from `phases.base_ref` if it is missing" (§11.5 step 4).

    `phases.base_ref` is the ref's NAME and `phases.pre_commit_sha` is the commit it named — the
    §11.5 authority table's "the ref lives in Git, the *name* lives here". So a missing anchor is
    recoverable exactly when the cached SHA still resolves, and the recreation is a `update-ref`
    back to it, never a fresh cut at the current tip: cutting a new anchor at the tip would move
    the scoped range's floor above commits this very step is about to search for, and every task
    whose commit sits between the two would be arbitrated as "nothing landed" and discarded.

    Returns the SHA the trailer search must be scoped to, or `None` when there is nothing to
    recover from — a phase that never cut an anchor has no mutation to reconcile.
    """
    ref = base_ref or f"refs/fleet/{run_id}/{repo_id}/phase-{phase}/base"
    existing = await git.resolve(ref)
    if existing is not None:
        return existing
    if pre_commit_sha is None or await git.resolve(pre_commit_sha) is None:
        return None
    if not dry_run:
        await git.update_ref(ref, pre_commit_sha, message="fleet resume step 4 anchor recreate")
    cast(list[object], report["anchors_recreated"]).append(
        {"repo_id": repo_id, "phase": phase, "ref": ref, "commit_sha": pre_commit_sha}
    )
    return pre_commit_sha


async def _persist_arbitration(
    path: Path,
    run_id: str,
    *,
    landed: Sequence[tuple[str, str, int, str]],
    discarded: Sequence[tuple[str, str, int]],
) -> list[tuple[str, str, int, str]]:
    """Copy Git's answers down, for the whole run, in ONE `StateWriter` unit (§11.5).

    One unit rather than one per task because the corrections are a single reconciliation: a
    partial commit would leave `tasks` reconciled against a `phases.post_commit_sha` that is not,
    which is the disagreement this step exists to remove.

    **The `attempts` row updated is the newest rung this task produced.** Inside the subquery's
    `task_id = ?` scope, the only two columns that can distinguish rows are `attempt` (the ladder
    rung) and `retry_ordinal` (a re-execution of that same rung, which `schema.sql` makes append
    rather than collide); both are in the `ORDER BY`, newest first. `commit_sha IS NULL` keeps a
    row that already recorded its own commit from being overwritten with another's.

    **`revalidation_round` is in the `ORDER BY` and CANNOT change the answer — it is named, not
    load-bearing, and an earlier version of this docstring claimed the opposite.** That claim was
    that "round 2's attempt 1 and round 1's attempt 1 tie" here without it. They cannot: each
    round is its own `tasks` row, because `revalidation_key` is `'r{round}:{hash}'` and
    `ux_tasks_ident` keys `tasks` on `IFNULL(revalidation_key, '')` — so two rounds never share a
    `task_id`, and this subquery never sees more than one round. `schema.sql`'s reason for putting
    the column in the `attempts` uniqueness key ("round 2 overwrites round 1's evidence") is real,
    and it is about a key that is NOT scoped by `task_id`; it does not transfer to this sort. The
    term is retained only so the sort names the same components the declared key does, and because
    newest-round-first is the right direction if a future writer ever does put two rounds under one
    `task_id`. It is not what makes this query correct.

    **`finished_at` is deliberately NOT a tiebreak.** `schema.sql` declares it `TEXT NOT NULL` on
    `attempts` (the nullable `finished_at` in that file belongs to `runs`), and `record_attempt`
    inserts both timestamps at completion — so a started-but-unfinished row is not a state this
    table can hold, and a sort on it would be ordering by a wall clock (§11.5's clock rule) for
    nothing. The shape a crash really leaves is **no row at all**, which is what the return value
    below exists for.

    **Returns the landed entries whose `attempts` row could not be found**, and that return value
    is not a courtesy. `attempt_id = (SELECT … LIMIT 1)` over an empty subquery is
    `attempt_id = NULL`, which matches zero rows *silently* — while `phases.post_commit_sha` is
    written in the same unit regardless. The two pointers §11.5's authority table pairs would then
    disagree with nothing saying so, and §11.5 step 5's `evidence_holds` reads one of them. The
    caller reports every entry by name (Rule 11); the `phases` write is still made, because the
    commit really is on the branch and Git is what that column points at.
    """
    stamp = _iso(_now())
    unwritten: list[tuple[str, str, int, str]] = []
    async with StateWriter(path, owner="fleet-resume") as writer:

        async def unit(db: aiosqlite.Connection) -> None:
            for task_id, repo_id, phase, sha in landed:
                await db.execute(
                    "UPDATE tasks SET status = 'DONE', claimed_by = NULL, "
                    "    lease_expires_at = NULL WHERE run_id = ? AND task_id = ?",
                    (run_id, task_id),
                )
                cursor = await db.execute(
                    "UPDATE attempts SET commit_sha = ? WHERE attempt_id = ("
                    "    SELECT attempt_id FROM attempts "
                    "     WHERE run_id = ? AND task_id = ? AND commit_sha IS NULL "
                    "     ORDER BY attempt DESC, revalidation_round DESC, retry_ordinal DESC "
                    "     LIMIT 1)",
                    (sha, run_id, task_id),
                )
                if not cursor.rowcount:
                    unwritten.append((task_id, repo_id, phase, sha))
                await db.execute(
                    "UPDATE phases SET post_commit_sha = ?, updated_at = ? "
                    " WHERE run_id = ? AND repo_id = ? AND phase = ?",
                    (sha, stamp, run_id, repo_id, phase),
                )
            for task_id, _repo_id, _phase in discarded:
                # The fence bump is the half that makes this safe, exactly as it is in step 3's
                # `_RESET_RUNNING_TO_PENDING_SQL`: a killed worker that is somehow still alive
                # carries the old `fence_token` on every write, and only the bump makes its next
                # one match zero rows. `attempts` is not named here, in any table.
                await db.execute(
                    "UPDATE tasks SET status = 'PENDING', claimed_by = NULL, "
                    "    lease_expires_at = NULL, fence_token = fence_token + 1 "
                    " WHERE run_id = ? AND task_id = ?",
                    (run_id, task_id),
                )

        await writer.submit(unit)
    return unwritten


# --------------------------------------------------------------------------------------
# §11.5 step 5 — demote every repo to its re-entry floor
# --------------------------------------------------------------------------------------

_FLOOR_ROWS_SQL: Final = (
    "SELECT repo_id, phase, status, post_commit_sha FROM phases "
    " WHERE run_id = ? ORDER BY repo_id, phase"
)


async def _demote_to_floors(
    settings: FleetSettings,
    path: Path,
    run_id: str,
    *,
    dry_run: bool,
    now: datetime,
) -> tuple[dict[str, object], dict[str, Phase]]:
    """§11.5 step 5: re-check each phase's durable evidence and demote each repo to its floor.

    **A driver over `orchestrator/reentry.py` and `demote_to_floor`, not new machinery.** The
    floor rule lives in `phase_floor`, the four durable predicates in `evidence_holds`, the lazy
    pairing of the two in `resume_floor`, the membership rule of the write in `demotable_phases`
    and the write itself in `SqliteStateRepository.demote_to_floor`. Nothing here restates any of
    them; this function supplies the three things none of those can reach on their own — the
    `phases` rows, the per-repo paths production writes to, and the one `findings` fact
    (`VerificationReport`) that `fleet.orchestrator` may not import `fleet.cli` to look up.

    **ONE route, not two.** `--dry-run` and the real run compute the identical plan from the
    identical read; the branch is the terminal persist and nothing above it, which is step 4's
    shape rather than step 3's. Step 3 needs a second function because its two SQL texts are two
    routes kept in agreement by a shared predicate constant; step 5 has one computation, so a
    preview derived by a second comprehension over `span >= floor and SUCCEEDED` would be defect
    D74 introduced on purpose — hence `demotable_phases`, called here and inside the transaction.
    `report["applied"]` is an explicit boolean for the reason `raise_budget_applied` is: a payload
    that reports only what was asked let `--dry-run` assert a state change that never happened.

    **`--dry-run` reaches Git and nothing else.** `evidence_holds` runs `rev-parse`-class reads
    and `merge-base --is-ancestor` against local checkouts, plus two path probes; it opens no
    socket, so the preview keeps §11.5's promise that a resume's reconciliation makes no network
    call. The SQL write is the only thing `dry_run` suppresses.

    **Every repo lands in exactly one of four buckets, and none of them is a bare count** (D44).
    `demoted` carries the floor, the phases and the evidence that produced them; `unchanged`
    carries the reason nothing moved, which is a different fact from "no repo needed to move";
    and `unresolved` carries the repos step 5 could not judge at all — a refused destination, a
    Git call that failed, or a `phases` row that moved under the floor computation. Collapsing
    any two of them is how an operator reads a fleet as reconciled when a quarter of it was
    skipped.

    **The second return value is the floors themselves, and step 6 is why it exists.**
    `reentry.plan_unblocking` takes `floors` as an input it never re-derives, and the report is
    not that input: it carries the floor of every repo this step *demoted* and drops the floor of
    every repo it left `unchanged` because nothing above the floor was `SUCCEEDED`. Feeding step 6
    from the report would tell it `floor is None` for those repos — which `Unblocking`'s docstring
    defines as "step 5 computed no floor", a different and false fact. So the mapping is returned
    beside the report rather than parsed back out of it.
    """
    report: dict[str, object] = {
        "candidates": 0,
        "demoted": [],
        "unchanged": [],
        "unresolved": [],
        "applied": False,
    }

    conn = await connect_ro(path)
    try:
        rows = await _rows(conn, _FLOOR_ROWS_SQL, (run_id,))
        dests = await _dest_paths(conn, settings.config.build.monorepo_dir_overrides)
        verified = set(await _verifications(conn, run_id))
    finally:
        await conn.close()

    by_repo: dict[str, dict[Phase, EvidenceRow]] = {}
    for repo_id, phase, status, post_commit_sha in rows:
        by_repo.setdefault(str(repo_id), {})[Phase(int(phase))] = EvidenceRow(
            status=RepoStatus(str(status)),
            post_commit_sha=None if post_commit_sha is None else str(post_commit_sha),
        )
    report["candidates"] = len(by_repo)
    computed: dict[str, Phase] = {}
    if not by_repo:
        return report, computed

    # UNSUFFIXED, and that is the contract: `RepoEvidence.for_repo` appends
    # `reentry.MIRROR_CACHE_SUBDIR` itself so the one expression that knows git mirrors live a
    # level down is over there rather than here. Passing the suffixed path would probe
    # `<cache_dir>/git/git/<slug>.git`, `_mirror_is_initialized` would answer False for every
    # repo, and the whole fleet's floor would silently be SCAN on every resume.
    cache_dir = (settings.root / settings.config.run.cache_dir).resolve()
    work_dir = (settings.root / settings.config.run.work_dir).resolve()
    plans: list[tuple[str, Phase, tuple[Phase, ...], dict[Phase, RepoStatus], str]] = []

    for repo_id, repo_rows in by_repo.items():
        statuses = {phase: row.status for phase, row in repo_rows.items()}
        dest = dests.get(repo_id)
        if dest is None:
            # `layout()` refused this repo's destination (the reserved `_scc/` namespace, or a
            # `dest:` that escapes the root). Phase 3's evidence is `<worktree>/<dest>/BUILD.bazel`
            # and there is no dest, so this repo cannot be judged — reported, never guessed.
            _unresolved(report, {"repo_id": repo_id}, "no destination resolves for this repo")
            continue
        repo = RepoEvidence.for_repo(
            repo_id,
            cache_dir=cache_dir,
            work_dir=work_dir,
            dest=dest,
            has_verification_report=repo_id in verified,
        )

        async def probe(phase: Phase, *, rows: Mapping[Phase, EvidenceRow] = repo_rows,
                        repo: RepoEvidence = repo) -> bool:
            return await evidence_holds(phase, rows=rows, repo=repo)

        try:
            floor, evidence = await resume_floor(repo_rows, probe)
        except (GitError, OSError) as exc:
            # Rule 11: a Git call that failed is carried out verbatim. The repo's rows are
            # untouched and the next resume asks again.
            _unresolved(report, {"repo_id": repo_id}, f"{type(exc).__name__}: {exc}")
            continue
        if floor is None:
            reason = (
                "a phase requires human intervention"
                if RepoStatus.REQUIRES_HUMAN_INTERVENTION in statuses.values()
                else "every phase has already settled"
            )
            cast(list[object], report["unchanged"]).append({"repo_id": repo_id, "reason": reason})
            continue
        computed[repo_id] = floor
        plan = demotable_phases(statuses, floor)
        if not plan:
            cast(list[object], report["unchanged"]).append(
                {
                    "repo_id": repo_id,
                    "reason": f"floor is {floor.name} and no phase at or above it is SUCCEEDED",
                }
            )
            continue
        plans.append((repo_id, floor, plan, statuses, _floor_reason(floor, evidence)))
        cast(list[object], report["demoted"]).append(
            {
                "repo_id": repo_id,
                "floor": floor.name,
                "phases": [phase.name for phase in plan],
                "evidence": {phase.name: holds for phase, holds in sorted(evidence.items())},
            }
        )

    if dry_run or not plans:
        return report, computed
    await _apply_floor_demotions(path, run_id, plans, report, now=now)
    return report, computed


def _floor_reason(floor: Phase, evidence: Mapping[Phase, bool]) -> str:
    """The one `PhaseDemotion.reason` stamped on EVERY phase in the span, so it is worded as a
    repo-level cause.

    `demote_to_floor` writes one `PhaseDemoted` finding per demoted phase and all of them carry
    this single string. A reason phrased as "the evidence for THIS phase does not hold" would be
    true only of the floor itself and false of every phase above it, which is a finding row
    asserting something untrue about work it just discarded.
    """
    read = ", ".join(f"{phase.name}={holds}" for phase, holds in sorted(evidence.items()))
    return (
        f"§11.5 step 5: this repo's re-entry floor recomputed to {floor.name}, so every SUCCEEDED "
        f"phase at or above it is re-entry territory and returns to PENDING with its attempts "
        f"retained. Durable evidence read below the frontier: {read or 'none consulted'}."
    )


async def _apply_floor_demotions(
    path: Path,
    run_id: str,
    plans: Sequence[tuple[str, Phase, tuple[Phase, ...], dict[Phase, RepoStatus], str]],
    report: dict[str, object],
    *,
    now: datetime,
) -> None:
    """One `StateWriter` for the whole fleet, one `BEGIN IMMEDIATE` per repo (ADR-0077).

    The writer is opened and closed HERE rather than around the whole of `_resume_impl` because
    `state/db.py`'s write slot is process-wide module state and steps 3 and 4 each open and close
    their own; a fourth one nested inside either would raise `SingleWriterViolationError`.

    `observed` is what makes the floor a property of the write: the statuses this run computed the
    floor from are handed to `demote_to_floor`, which refuses if the rows moved underneath them.
    That repo is then reported `unresolved` rather than silently skipped, because "nothing was
    demotable" and "the snapshot went stale" send the operator to opposite places.
    """
    demoted = cast(list[dict[str, object]], report["demoted"])
    kept: list[dict[str, object]] = []
    async with StateWriter(path, owner="fleet-resume-step5") as writer:
        read_conn = await connect_ro(path)
        try:
            repository = SqliteStateRepository(writer=writer, read_conn=read_conn)
            for entry, (repo_id, floor, plan, statuses, reason) in zip(demoted, plans, strict=True):
                try:
                    records = await repository.demote_to_floor(
                        run_id, repo_id, floor=floor, reason=reason, now=now, observed=statuses
                    )
                except FloorSnapshotStaleError as exc:
                    _unresolved(report, {"repo_id": repo_id}, str(exc))
                    continue
                applied = tuple(record.phase for record in records)
                if applied != plan:
                    # The only way in: the repo acquired a `REQUIRES_HUMAN_INTERVENTION` row
                    # between the two reads, which `demote_to_floor` short-circuits to `()`
                    # rather than refusing. Reported, never printed as a demotion that happened.
                    _unresolved(
                        report,
                        {"repo_id": repo_id},
                        f"planned {[p.name for p in plan]} but the transaction applied "
                        f"{[p.name for p in applied]}; nothing is claimed for this repo",
                    )
                    continue
                kept.append(entry)
        finally:
            await read_conn.close()
    report["demoted"] = kept
    report["applied"] = bool(kept)


# --------------------------------------------------------------------------------------
# §11.5 step 6 — recompute `blocked_by`, and move the repos it frees into an appended wave
# --------------------------------------------------------------------------------------


class UnblockLookupError(StateDbError):
    """A `blocked_by` name the fleet knows as a repo produced no `BlockerState`. Rule 11.

    `orchestrator.reentry.still_blocking` is fail-closed (ADR-0090 §2.4, R2-CLOSED): a name it
    cannot resolve is RETAINED. That polarity is right, and it is also what makes a broken
    lookup invisible — a resolver that silently returned nothing for every name would leave
    every entry in place and report itself as "the recompute found nothing to remove", which is
    indistinguishable from a healthy fleet. So the one case that is a *harness* fault rather
    than an unresolvable name — the `repos` table knows this id and the status lookup did not
    produce a state for it — is raised rather than folded into the retained set.
    """


def _blocker_states(phase_rows: Sequence[tuple[str, int, str]]) -> dict[str, BlockerState]:
    """Group each blocker's `phases` rows into the SET of statuses `BlockerState` carries.

    **This function no longer projects, and the loss of the projection is the point.** Until the
    predicate reshape it reduced a blocker's rows to the one `RepoStatus` `BlockerState` could
    hold, and the choice of reduction was load-bearing and easy to get wrong. `BlockerState` now
    carries `frozenset[RepoStatus]` — every row — so there is nothing here to choose and no
    caller-side rule for a reader to check. What is left is a group-by.

    **The reasoning that rule embodied is carried forward here rather than deleted with it,
    because it is the reason the reshape was needed** — measured by lane **W21** (round E,
    `1d0c8f6`) against this function, and reproduced by lane W16 by executing the shipped
    `cli._quarantine_impl` against a temp database rather than by reading it:

        A repo `fleet quarantine` removes *between phases* ends at `{SCAN: SKIPPED,
        TRANSFORM: SUCCEEDED}` — `_quarantine_impl` moves only rows outside `TERMINAL_STATUSES`,
        and `SUCCEEDED` is inside it, so a landed phase survives above the `SKIPPED` one. The
        observed vector was `[(1, 'SKIPPED'), (2, 'SUCCEEDED')]`, with the `OperatorQuarantine`
        finding written and the dependent `BLOCKED`. Under `state/projection._fold_repos`'
        highest-phase-wins fold that projects `SUCCEEDED`, and the dependents of an audited
        quarantine are re-admitted on every `fleet resume`.

    W21's repair was to reduce by *the lowest phase whose status is not `SUCCEEDED`, and
    `SUCCEEDED` only when every phase is*. That rule is **correct**, and it is the same predicate
    `reentry.still_blocking` now applies directly — "every row landed, or the blocker has not" —
    which is why the reshape could delete the reduction without losing the guarantee rather than
    by overruling it. The difference is only where it lives: as a caller-side rule it was a lossy
    stand-in that W21 correctly labelled as one, and a second caller reducing differently would
    have been unconstrained; as `LANDED_STATUSES` plus `all(...)` it is a property of the unit and
    a caller cannot express the loss at all.

    `_fold_repos`' fold is not wrong where it lives — it answers "which phase is this repo *at*",
    for the projection — it simply does not transfer to "has this blocker landed".
    """
    grouped: dict[str, set[RepoStatus]] = {}
    for repo_id, _phase, status in phase_rows:
        grouped.setdefault(repo_id, set()).add(RepoStatus(status))
    return {
        repo_id: BlockerState(phase_statuses=frozenset(statuses))
        for repo_id, statuses in grouped.items()
    }


async def _read_blocker_states(
    conn: aiosqlite.Connection, run_id: str, names: set[str]
) -> dict[str, BlockerState]:
    """`BlockerState` for every name that resolves to a repo carrying `phases` rows in this run.

    Usable on the `mode=ro` handle AND on the writer's connection inside a unit, which is the
    point: the guard the transaction re-evaluates is the guard the preview reported.
    """
    ordered = tuple(sorted(names))
    if not ordered:
        return {}
    marks = ",".join("?" for _ in ordered)
    phase_rows = [
        (str(repo_id), int(phase), str(status))
        for repo_id, phase, status in await _rows(
            conn,
            "SELECT repo_id, phase, status FROM phases "  # noqa: S608 - placeholders are '?' only
            f" WHERE run_id = ? AND repo_id IN ({marks})",
            (run_id, *ordered),
        )
    ]
    return _blocker_states(phase_rows)


async def _resolvable_repo_ids(conn: aiosqlite.Connection, names: set[str]) -> set[str]:
    """Which of `names` the `repos` table knows. A `contract_id` is in `blocked_by` and not here."""
    ordered = tuple(sorted(names))
    if not ordered:
        return set()
    marks = ",".join("?" for _ in ordered)
    return {
        str(row[0])
        for row in await _rows(
            conn,
            f"SELECT repo_id FROM repos WHERE repo_id IN ({marks})",  # noqa: S608 - as above
            ordered,
        )
    }


def _refuse_unresolved_blockers(
    run_id: str, known: set[str], states: Mapping[str, BlockerState]
) -> None:
    """A name the fleet knows as a repo MUST have a `BlockerState`. See `UnblockLookupError`."""
    missing = sorted(known - set(states))
    if missing:
        raise UnblockLookupError(
            f"run {run_id}: {missing} appear in some repo's `blocked_by` and in `repos`, but the "
            "status lookup produced no BlockerState for them. `still_blocking` is fail-closed, so "
            "these entries would be RETAINED and step 6 would report itself as having found "
            "nothing to remove — a broken lookup and a healthy fleet are the same output. "
            "Nothing was written."
        )


def _unblocking_entry(plan: Unblocking) -> dict[str, object]:
    """One repo's whole verdict — both halves of the split, never a count (D44).

    `removed` and `remaining` are `plan_unblocking`'s own values, reported rather than
    re-derived: `Unblocking` carries both precisely so no reader computes "did this list empty?"
    from the other one.
    """
    return {
        "repo_id": plan.repo_id,
        "removed": list(plan.removed),
        "remaining": list(plan.remaining),
        "floor": None if plan.floor is None else plan.floor.name,
    }


def _blocker_resolver(run_id: str) -> BlockerResolver:
    """The `BlockerState` lookup `SqliteStateRepository.clear_blocked_by` runs INSIDE its own
    transaction, closed over the run.

    Injected rather than implemented in `fleet.state` for `_demote_to_floors`' reason one step
    over: the lookup needs the `findings` and `repos` facts the CLI's read layer owns, and
    `fleet.state` may not reach up for them. What the repository owns is *when* it runs — on the
    writer's connection, under `BEGIN IMMEDIATE`, so the guard is a property of the write and not
    of this caller. The identical function is called by the `mode=ro` preview above, so the plan
    the operator is shown and the plan the transaction re-derives cannot be two rules.

    The loud check travels with it: a name the `repos` table knows that produced no `BlockerState`
    raises `UnblockLookupError` in BOTH routes, because fail-closed retention makes a broken
    lookup read exactly like a healthy fleet.
    """

    async def resolve(
        conn: aiosqlite.Connection, names: frozenset[str]
    ) -> Mapping[str, BlockerState]:
        states = await _read_blocker_states(conn, run_id, set(names))
        _refuse_unresolved_blockers(run_id, await _resolvable_repo_ids(conn, set(names)), states)
        return states

    return resolve


async def _unblock_dependents(
    settings: FleetSettings,
    path: Path,
    run_id: str,
    *,
    floors: Mapping[str, Phase],
    dry_run: bool,
    now: datetime,
) -> dict[str, object]:
    """§11.5 step 6: recompute `blocked_by`, and append the wave the freed repos move into.

    **A driver over `orchestrator/reentry.plan_unblocking` and
    `SchedulerStore.append_unblocked_wave`, not new machinery.** The removal rule lives in
    `still_blocking`, the whole per-repo decision in `plan_unblocking`, and the wave allocation
    in `append_unblocked_wave`. Nothing here restates any of them; this function supplies the
    three things none of those can reach — the `phases` rows, the blockers' statuses, and step
    5's floors.

    **ONE route, not two — the same shape as step 5's.** `--dry-run` and the real run call the
    *same* `plan_unblocking` with the *same* inputs; the branch is the terminal persist and
    nothing above it. A preview derived by a second comprehension over "is this blocker still
    RHI?" is defect D74 introduced on purpose, and it is the seam ADR-0090's design closes by
    construction: `Unblocking` carries `removed` **and** `remaining` so that neither route
    re-derives "did this list empty?".

    **`floors` is passed in and never re-derived** — step 5 computed it (`_demote_to_floors` over
    `phase_floor`). Step 6 REPORTS the floor and does not apply it: a status write that moved a
    row down to the floor here would be a demotion outside step 5's audited `PhaseDemoted` path,
    which `models.enums.demote` refuses by construction. The only status write here is the
    `BLOCKED -> PENDING` the emptied list implies.

    **Four buckets, none of them a bare count (D44).** `unblocked` is a repo whose list is now
    empty — it re-enters the queue; `retained` is a repo still held, *by these names*, which is a
    different fact from "no repo was freed"; `unresolved` is a repo step 6 refused to write
    because its rows moved; and `wave_error` is the appended wave failing after the `blocked_by`
    edits committed. Collapsing any two of them is how an operator reads a fleet as re-admitted
    when a quarter of it is still blocked.
    """
    report: dict[str, object] = {
        "candidates": 0,
        "unblocked": [],
        "retained": [],
        "unresolved": [],
        "wave_index": None,
        "wave_error": None,
        "applied": False,
    }

    conn = await connect_ro(path)
    try:
        blocked_by_rows = [
            (str(repo_id), names)
            for repo_id, blocked_by in await _rows(
                conn,
                # `phases.blocked_by` is `TEXT NOT NULL DEFAULT '[]'`, so the empty case is
                # `'[]'` and not NULL; the filter is the walrus below, over the DECODED list, so
                # a hand-written `'[ ]'` or a duplicated name cannot slip past a SQL predicate.
                "SELECT repo_id, blocked_by FROM phases WHERE run_id = ?",
                (run_id,),
            )
            if (names := sorted(set(json.loads(str(blocked_by) or "[]"))))
        ]
        named = {name for _repo_id, names in blocked_by_rows for name in names}
        blocker_statuses = await _read_blocker_states(conn, run_id, named)
        known = await _resolvable_repo_ids(conn, named)
    finally:
        await conn.close()
    _refuse_unresolved_blockers(run_id, known, blocker_statuses)

    plans = plan_unblocking(
        blocked_by_rows=blocked_by_rows, blocker_statuses=blocker_statuses, floors=floors
    )
    report["candidates"] = len(plans)
    refused: Mapping[str, str] = {}
    if not dry_run and any(plan.removed for plan in plans):
        refused = await _apply_unblocking(settings, path, run_id, plans, report, floors=floors,
                                          now=now)
    for plan in plans:
        if plan.repo_id in refused:
            _unresolved(report, {"repo_id": plan.repo_id}, refused[plan.repo_id])
            continue
        bucket = "retained" if plan.remaining else "unblocked"
        cast(list[object], report[bucket]).append(_unblocking_entry(plan))
    return report


async def _apply_unblocking(
    settings: FleetSettings,
    path: Path,
    run_id: str,
    plans: Sequence[Unblocking],
    report: dict[str, object],
    *,
    floors: Mapping[str, Phase],
    now: datetime,
) -> dict[str, str]:
    """One `StateWriter`, one `BEGIN IMMEDIATE` per repo, then ONE `append_unblocked_wave`.

    The writer is opened and closed HERE for `_apply_floor_demotions`' reason: `state/db.py`'s
    write slot is process-wide module state and steps 3, 4 and 5 each open and close their own.

    **TWO transactions, not one, and it is forced rather than chosen.**
    `SchedulerStore.append_unblocked_wave` submits its own unit — that is where its
    `MAX(wave_index) + 1` allocation lives, and allocating inside the write is the property
    ADR-0090 ruling W bought. Reaching into it to share this function's transaction would mean
    re-implementing its SQL here, which is the seam the ruling closes. So the order is
    `blocked_by` first, wave second, and the residue is stated rather than hidden: a crash
    between them leaves a freed repo `PENDING` in the wave it already sat in. That is the state
    step 5's demotion leaves behind on every resume (ADR-0089 §4) — the repo's old wave answers
    `OPEN`, so `open_wave` refuses every later wave until it re-settles. Visible, and one more
    `fleet resume` does not repeat it, because the entry is already gone from `blocked_by` and
    nothing re-appends the wave; an operator sees a repo at a floor in an early wave. The
    opposite order is worse: it appends a fresh synthetic wave on *every* resume until the
    `blocked_by` write lands.

    Returns the repos whose write was refused, with the reason, so the caller can report them
    rather than count them.
    """
    refused: dict[str, str] = {}
    written: list[str] = []
    freed: list[str] = []
    resolve = _blocker_resolver(run_id)
    async with StateWriter(path, owner="fleet-resume-step6") as writer:
        read_conn = await connect_ro(path)
        try:
            store = SqliteSchedulerStore(writer=writer, read_conn=read_conn)
            repository = SqliteStateRepository(writer=writer, read_conn=read_conn)
            for plan in plans:
                if not plan.removed:
                    continue  # nothing to write; `plan_unblocking` emits every repo, freed or not
                try:
                    await repository.clear_blocked_by(
                        run_id,
                        plan.repo_id,
                        observed=plan,
                        floors=floors,
                        resolve=resolve,
                        now=now,
                    )
                except BlockedBySnapshotStaleError as exc:
                    refused[plan.repo_id] = str(exc)
                    continue
                written.append(plan.repo_id)
                if not plan.remaining:
                    freed.append(plan.repo_id)
            report["applied"] = bool(written)
            if freed:
                try:
                    report["wave_index"] = await store.append_unblocked_wave(
                        run_id,
                        freed,
                        now=now,
                        max_usd_per_repo=settings.config.budgets.wave_max_cost_usd_per_repo,
                    )
                except WaveNotReadyError as exc:
                    # Rule 11: carried out in the payload, never swallowed. The `blocked_by`
                    # edits above are already committed and stay committed — a repo that is no
                    # longer blocked but has not moved wave is recoverable; re-blocking it to
                    # tidy the report would discard a correct write.
                    report["wave_error"] = str(exc)
        finally:
            await read_conn.close()
    return refused


def _unblock_lines(result: Mapping[str, object], *, dry: bool) -> list[str]:
    """§11.5 step 6, reported per repo, for `_floor_lines`' reason one step over.

    "2 repos un-blocked" cannot tell an operator whether a quarantined dependency was re-admitted
    or a subtree is still held, and those are opposite facts about the same run. `applied` rather
    than `dry` decides the verb, because a real run whose every entry is retained writes nothing.
    """
    report = result["unblocked_dependents"]
    if not isinstance(report, Mapping):  # pragma: no cover - the payload always carries it
        return []
    unblocked = cast(Sequence[Mapping[str, object]], report["unblocked"])
    retained = cast(Sequence[Mapping[str, object]], report["retained"])
    unresolved = cast(Sequence[Mapping[str, object]], report["unresolved"])
    verb = "would clear" if dry else "cleared"
    out: list[str] = []
    if not int(cast(int, report["candidates"])):
        return ["  step 6: no repo carries a `blocked_by` entry; nothing to recompute"]
    for entry in unblocked:
        removed = cast(Sequence[str], entry["removed"])
        floor = entry["floor"]
        out.append(
            f"  step 6: {verb} {', '.join(removed)} from {entry['repo_id']} — its `blocked_by` "
            f"is empty, so it returns to PENDING"
            + (f" at floor {floor}" if isinstance(floor, str) else "")
        )
    for entry in retained:
        removed = cast(Sequence[str], entry["removed"])
        remaining = cast(Sequence[str], entry["remaining"])
        out.append(
            f"  step 6: {entry['repo_id']} still blocked by {', '.join(remaining)}"
            + (f" ({verb} {', '.join(removed)})" if removed else "")
        )
    out.extend(
        f"  step 6: UNRESOLVED for {entry['repo_id']} — {entry['reason']}"
        for entry in unresolved
    )
    wave = report["wave_index"]
    if isinstance(wave, int):
        out.append(f"  step 6: freed repos moved into appended wave {wave} (synthetic)")
    error = report["wave_error"]
    if isinstance(error, str):
        out.append(f"  step 6: the appended wave FAILED — {error}")
    return out


# --------------------------------------------------------------------------------------
# §11.5 step 2 — reap the `fleet-<run_id>-*` worktrees and containers no live row claims
# --------------------------------------------------------------------------------------


async def _live_sandbox_names(
    conn: aiosqlite.Connection, run_id: str, horizons: tuple[str, str]
) -> set[str]:
    """The `fleet-<run_id>-<repo>-<attempt>` names step 2 must spare, derived from `phases`.

    **TWO names per live row, not one**, and the second is not defensive padding.
    `phases.attempts` is a *charged* counter while the sandbox name carries a *rung* number:
    `PhaseRunner._dispatch` computes `attempt = phases.attempts + 1` (`orchestrator/runner.py`),
    so the checkout a live worker is writing into right now is `attempts + 1`, while `attempts`
    names the rung whose charge has already landed and whose sandbox the same worker may still
    be tearing down. Sparing both is the safe direction of the two errors available here: a
    spared orphan costs disk until the next `fleet resume` re-attempts it (the sweep is
    idempotent and self-healing), whereas a reaped live checkout costs the run.
    """
    rows = await _rows(
        conn,
        "SELECT repo_id, attempts FROM phases WHERE run_id = ?"  # noqa: S608
        + _LIVE_SANDBOX_PREDICATE,
        (run_id, *horizons),
    )
    return {
        sandbox_name(run_id, str(repo_id), rung)
        for repo_id, attempts in rows
        for rung in (int(attempts), int(attempts) + 1)
    }


def _reap_worktree_manager(settings: FleetSettings, run_id: str) -> WorktreeManager:
    """Seam: the one construction site, so a test can drive the sweep against a scripted git."""
    return WorktreeManager(
        repo_dir=(settings.root / settings.config.run.monorepo_path).resolve(),
        work_dir=(settings.root / settings.config.run.work_dir).resolve(),
        run_id=run_id,
    )


def _reap_container_sandbox() -> ContainerSandbox:
    """Seam: the one construction site, so a test never has to reach a real docker daemon."""
    return ContainerSandbox()


async def _reap_orphan_worktrees(
    settings: FleetSettings, run_id: str, live: set[str], *, dry_run: bool
) -> dict[str, object]:
    """The worktree half of step 2. Never raises — every outcome is reported to the operator.

    The `--dry-run` branch does not call `reap()` at all: it re-applies `reap()`'s own two
    filters (the `fleet-<run_id>-` prefix, then `live`) to the same `list_registered()` listing
    and reports the difference. Calling `reap()` and discarding the result would remove the
    worktrees, which is the one thing a preview may not do.

    Nothing outside `fleet-<run_id>-*` is reachable from here in EITHER branch, because the
    prefix filter is applied before any `remove()` and the preview applies the identical one.

    **KNOWN LIMITATION, measured, not speculative — today this sweep finds nothing in a real run,
    and the reported `namespace` is what makes that visible instead of silent.** Two independent
    namespace mismatches sit between this code and the worktrees production actually cuts, and
    both live outside this module (so they are reported here, not reached across and patched):

    * **Registry.** `CloneWorker._materialize_worktree` (`workers/clone.py`) runs
      `git worktree add` inside the repo's own MIRROR, `<cache_dir>/<slug(repo_id)>.git`. This
      manager asks `run.monorepo_path`, so `list_registered()` interrogates a git directory that
      never registered them.
    * **Name.** `OrchestratorContext.worktree` (`orchestrator/context.py`) returns
      `work_dir/<repo_id>`, with no `fleet-<run_id>-` prefix and no attempt suffix — so even
      against the right registry, `reap()`'s prefix filter would spare every one of them.

    `WorktreeManager` is not constructed anywhere else in `src/` (only `sandbox_name` is, by
    `workers/buildverify.py` and `workers/rdepverify.py`, which is why the CONTAINER half of step
    2 does work). Closing this needs `clone.py` and `context.py` to adopt **`checkout_name`** —
    the attempt-FREE `fleet-<run_id>-<repo>` form — and that is a separate change with its own
    migration question for worktrees already on disk.

    **NOT `sandbox_name`, which this docstring prescribed until round D's final review.** The
    directory both files own is the cross-phase repo checkout, and `docs/SPEC.md` §3.3's
    name-form table gives that primitive `fleet-<run_id>-<repo>` explicitly: Phase 2 reads the
    tree Phase 1 cloned, so an attempt in the name would hand Phase 2 — and every retry — a
    different empty directory. ADR-0085 §1 makes the same argument from the type,
    `OrchestratorContext.worktree(repo_id)` having no `attempt` parameter. A reconciler who made
    the code match the old sentence would have implemented the form the table forbids.
    """
    monorepo = (settings.root / settings.config.run.monorepo_path).resolve()
    scope = f"{run_prefix(run_id)}* registered in {monorepo}"
    if not await asyncio.to_thread((monorepo / ".git").exists):
        # A settled negative established by a `stat`, not by a subprocess exit code, and so not
        # the unsettled-probe shape D44 was filed for: with no repository there is no worktree
        # registry, so there is nothing this sweep could reap. Reported rather than skipped
        # silently — an operator whose `run.monorepo_path` is mistyped must see that step 2
        # found nothing because it had nowhere to look, not because the run was clean.
        return {
            "reaped": [],
            "failed": [],
            "error": None,
            "skipped": f"no git repository at {monorepo} (`run.monorepo_path`)",
            "namespace": scope,
        }
    manager = _reap_worktree_manager(settings, run_id)
    try:
        if dry_run:
            prefix = run_prefix(run_id)
            registered = await manager.list_registered()
            names = [
                p.name for p in registered if p.name.startswith(prefix) and p.name not in live
            ]
            return {
                "reaped": names,
                "failed": [],
                "error": None,
                "skipped": None,
                "namespace": scope,
            }
        result = await manager.reap(live_names=live)
    except WorktreeError as exc:
        # `list_registered()` raising means git was never successfully consulted, so the sweep
        # has no answer at all — a different fact from "it swept and some entries resisted", and
        # carried in a different key for that reason.
        return {
            "reaped": [],
            "failed": [],
            "error": str(exc),
            "skipped": None,
            "namespace": scope,
        }
    return {
        "reaped": list(result.reaped),
        # NOT dropped, NOT folded into `reaped`, NOT reduced to a count: `ReapResult.failed`
        # names worktrees this sweep attempted and could not verify gone, and a caller that
        # reported only `reaped` would let an operator read a partial sweep as a complete one
        # (docs/INTEGRATION_HONESTY.md D44). `_reap_lines` prints every entry with its reason.
        "failed": [{"name": f.name, "reason": f.reason} for f in result.failed],
        "error": None,
        "skipped": None,
        "namespace": scope,
    }


async def _reap_orphan_containers(
    run_id: str, live: set[str], *, dry_run: bool
) -> dict[str, object]:
    """The container half of step 2. Never raises; same reporting shape as the worktree half.

    **`live` is the set of SANDBOX names and is handed to `reap()` exactly as it is** — not
    pre-expanded into concrete container names here. That is `reap()`'s documented contract:
    since `aa16846` it spares via `sandbox/container.py`'s `claims()`, which matches a live
    sandbox name against a container named `<sandbox_name>-t<token>` by `-`-delimited prefix
    (`BuildverifyWorker` names every container that way, `workers/buildverify.py`
    `_container_prefix`/`_invocation_name`).

    An earlier draft of this function did expand the set itself: it listed the run's containers,
    computed the spared subset here, and passed that concrete list as `live_names`. It cited
    `reap()` sparing "by EXACT membership" — the pre-`aa16846` behaviour, false against the code
    it was calling. Beyond restating logic that already exists, the expansion opened a window
    `claims()` does not have: `spared` was computed from a listing taken before `reap()` took its
    own, so any container started between the two listings was absent from `spared` and would be
    `docker rm --force`d despite a live row claiming it — killing a running build, the exact
    outcome the paragraph justifying the expansion said it was preventing.

    `--dry-run` must not call `reap()` (it removes), so the preview re-applies `reap()`'s own two
    filters — the run prefix, then `claims()` — to a listing of its own. It imports `claims`
    rather than restating the rule, so the preview cannot drift from what the real sweep does.

    **The preview reads `list_with_verdict`, which carries a failed `docker ps` in its own
    `error` field rather than returning `[]` for it** (docs/INTEGRATION_HONESTY.md D73, whose
    second residual this branch was; the lenient `list_by_prefix` wrapper this used to call was
    deleted in `f10a863` once its last caller moved off it). The non-preview
    branch below has kept a failed `docker ps` apart from an empty one since `cfd89c7` — `reap()`
    returns a `failed` entry naming the prefix — while the preview collapsed them and printed
    `no orphan containers` during a docker outage. That is worse here than on the sweep path, not
    milder: `--dry-run` exists to be believed, and the one answer a health check must never
    fabricate is "there is nothing wrong". The failure is carried in `error`, the SAME key the
    worktree half already uses when `list_registered()` raises, so `_reap_lines` prints
    `the container sweep did not run — …` and the clean headline is not reachable. `error` is not
    `failed`: nothing was attempted, so there is no entry to name.
    """
    sandbox = _reap_container_sandbox()
    prefix = run_prefix(run_id)
    scope = f"docker containers named {prefix}*"
    try:
        if dry_run:
            listing = await sandbox.list_with_verdict(prefix)
            if listing.error is not None:
                return {
                    "reaped": [],
                    "failed": [],
                    "error": listing.error,
                    "skipped": None,
                    "namespace": scope,
                }
            names = [
                name
                for name in listing.names
                if name.startswith(prefix)
                and not any(claims(live_name, name) for live_name in live)
            ]
            return {
                "reaped": names,
                "failed": [],
                "error": None,
                "skipped": None,
                "namespace": scope,
            }
        result = await sandbox.reap(run_id=run_id, live_names=live)
    except OSError as exc:
        # Neither `list_with_verdict` nor `reap` guards the spawn itself, so a host with no `docker`
        # on PATH raises here. That is "docker was never asked", not "the run has no containers",
        # and the two must not print the same line.
        return {
            "reaped": [],
            "failed": [],
            "error": f"docker was never invoked ({type(exc).__name__}: {exc})",
            "skipped": None,
            "namespace": scope,
        }
    return {
        "reaped": list(result.reaped),
        # Same obligation as the worktree half, one layer up: `ContainerReapResult.failed` is a
        # container docker was asked about and did not confirm removed. Swallowing it would
        # compound the D32 container leak with a backstop that lies about having caught it.
        "failed": [{"name": f.name, "reason": f.reason} for f in result.failed],
        "error": None,
        "skipped": None,
        "namespace": scope,
    }


def _validate_accept_drift(settings: FleetSettings, names: Sequence[str]) -> tuple[str, ...]:
    """`--accept-drift SECTION` accepts EXACTLY the named section — never its neighbours.

    A name that is not a §10 drift section is exit 2 with the valid list, because silently
    ignoring `--accept-drift budget` (singular) would refuse the resume for a reason the operator
    believes they just accepted.
    """
    known = set(settings.section_digests)
    accepted: list[str] = []
    for name in names:
        if name not in known:
            raise UsageError(
                f"--accept-drift {name!r} is not a config drift section; §10 lists "
                f"{', '.join(sorted(known))}"
            )
        accepted.append(name)
    return tuple(dict.fromkeys(accepted))


async def _record_drift_findings(
    path: Path, run_id: str, sections: Sequence[str], settings: FleetSettings
) -> None:
    """"Each accepted section writes its own audited `ConfigDrift` finding" (§10)."""
    stamp = _iso(_now())
    async with StateWriter(path, owner="fleet-resume") as writer:

        async def unit(db: aiosqlite.Connection) -> None:
            await db.executemany(
                "INSERT INTO findings (run_id, repo_id, kind, severity, fingerprint, payload,"
                "                      created_at) "
                "VALUES (?, NULL, 'ConfigDrift', 'warn', ?, ?, ?) "
                "ON CONFLICT (run_id, IFNULL(repo_id, ''), kind, fingerprint) "
                "DO UPDATE SET payload = excluded.payload, created_at = excluded.created_at",
                [
                    (
                        run_id,
                        _fingerprint(run_id, name, settings.section_digest(name)),
                        json.dumps(
                            {
                                "section": name,
                                "digest": settings.section_digest(name),
                                "accepted_by": "--accept-drift",
                            },
                            sort_keys=True,
                        ),
                        stamp,
                    )
                    for name in sections
                ],
            )
            await db.execute(
                "UPDATE runs SET config_digests = ?, config_sha256 = ? WHERE run_id = ?",
                (
                    json.dumps(dict(settings.section_digests), sort_keys=True),
                    settings.config_sha256(),
                    run_id,
                ),
            )

        await writer.submit(unit)


async def _raise_wave_ceiling(path: Path, run_id: str, wave: int, ceiling: float) -> None:
    """§10: an audited `findings` row naming the wave and the new ceiling, then the new number."""
    stamp = _iso(_now())
    async with StateWriter(path, owner="fleet-resume") as writer:

        async def unit(db: aiosqlite.Connection) -> None:
            await db.execute(
                "INSERT INTO findings (run_id, repo_id, kind, severity, fingerprint, payload,"
                "                      created_at) "
                "VALUES (?, NULL, 'WaveBudgetRaised', 'warn', ?, ?, ?) "
                "ON CONFLICT (run_id, IFNULL(repo_id, ''), kind, fingerprint) "
                "DO UPDATE SET payload = excluded.payload, created_at = excluded.created_at",
                (
                    run_id,
                    _fingerprint(run_id, str(wave), f"{ceiling:.6f}"),
                    json.dumps({"wave_index": wave, "new_max_usd": ceiling}, sort_keys=True),
                    stamp,
                ),
            )
            await db.execute(
                "UPDATE waves SET max_usd = ? WHERE run_id = ? AND wave_index = ?",
                (ceiling, run_id, wave),
            )

        await writer.submit(unit)


@dataclass(frozen=True, slots=True)
class _LedgerRow:
    """`budget_ledger` as `--raise-budget` needs to see it: before the change, and for the JSON."""

    spent_usd: float
    reserved_usd: float
    max_usd: float
    halted: bool

    @property
    def committed_usd(self) -> float:
        return self.spent_usd + self.reserved_usd

    def payload(self) -> dict[str, object]:
        return {
            "spent_usd": self.spent_usd,
            "reserved_usd": self.reserved_usd,
            "max_usd": self.max_usd,
            "halted": self.halted,
        }


async def _refuse_bad_raise_budget(
    conn: aiosqlite.Connection, run_id: str, ceiling: float
) -> _LedgerRow:
    """Every reason `--raise-budget` cannot be honoured, decided read-only so `--dry-run` says so.

    Run in BOTH modes and before anything is written. A `--dry-run` that exited 0 for a figure the
    real invocation refuses is worse than no preview: an operator recovering from a sticky exit-3
    halt dry-runs precisely to learn whether their number works.

    The name says RAISE. `ceiling < max_usd` is refused for that reason and not out of pedantry:
    the CAS would happily accept `--raise-budget 5` against a $100 ceiling, clear the halt, and
    write an audited `RunBudgetRaised` finding recording the LOWERING as a raise — after which the
    run re-halts almost immediately with an audit trail that says the opposite of what happened.
    """
    rows = await _rows(
        conn,
        "SELECT spent_usd, reserved_usd, max_usd, halted FROM budget_ledger WHERE run_id = ?",
        (run_id,),
    )
    if not rows:
        raise UsageError(
            f"--raise-budget: run {run_id} has no `budget_ledger` row, so there is no ceiling to "
            "raise. The ledger is opened when the run first admits work (§11.2); a run that has "
            "not reached that point is not budget-halted and needs no raise."
        )
    ledger = _LedgerRow(
        spent_usd=float(rows[0][0]),
        reserved_usd=float(rows[0][1]),
        max_usd=float(rows[0][2]),
        halted=bool(rows[0][3]),
    )
    if ceiling < ledger.committed_usd:
        raise UsageError(
            f"--raise-budget {ceiling:.2f} is below what run {run_id} has already committed: "
            f"${ledger.spent_usd:.2f} spent + ${ledger.reserved_usd:.2f} still reserved against a "
            f"${ledger.max_usd:.2f} ceiling. `budget_ledger` refuses a ceiling under "
            "`spent_usd + reserved_usd` by CHECK constraint, so pass a figure greater than "
            f"{ledger.committed_usd:.2f}. Nothing was written."
        )
    if ceiling < ledger.max_usd:
        raise UsageError(
            f"--raise-budget {ceiling:.2f} would LOWER run {run_id}'s ceiling from "
            f"${ledger.max_usd:.2f}, and this flag only raises. Lowering it here would clear "
            "`halted` and write a `RunBudgetRaised` finding recording a cut as a raise, and the "
            "run would re-halt almost at once with an audit trail that says otherwise. If a lower "
            "ceiling is genuinely wanted, that is a config change plus a fresh run, not a "
            "resume. Nothing was written."
        )
    return ledger


async def _raise_run_ceiling(path: Path, run_id: str, ceiling: float) -> None:
    """§10 exit 3's only exit: raise `budget_ledger.max_usd` and clear the sticky halt, audited.

    `halted = 1` is durable and deliberately sticky — the reservation CAS itself carries
    `AND halted = 0` (§11.2), so once it is set no worker in any process can dispatch, in this
    run or the next one. That is correct for a runaway and fatal for everything else: without a
    writer that clears the flag, the halted ledger is unrecoverable and every later `fleet
    resume` re-halts on a ledger nobody can reset. §10 names `--raise-budget` as the way out, so
    this is that writer.

    The new ceiling is applied as a CAS, not a read-then-write: `spent_usd + reserved_usd <=
    :ceiling` is `budget_ledger`'s own CHECK constraint, and asserting it in the `WHERE` turns a
    ceiling below what the run has already committed into a refusal with the real numbers instead
    of an `IntegrityError` traceback. The audit row is written in the same transaction as the new
    number, because a raise recorded only on success is a raise lost to the next crash.
    """
    stamp = _iso(_now())
    async with StateWriter(path, owner="fleet-resume") as writer:

        async def unit(db: aiosqlite.Connection) -> bool:
            cursor = await db.execute(
                "UPDATE budget_ledger SET max_usd = ?, halted = 0, updated_at = ? "
                " WHERE run_id = ? AND spent_usd + reserved_usd <= ? AND max_usd <= ?",
                (ceiling, stamp, run_id, ceiling, ceiling),
            )
            if int(cursor.rowcount) != 1:
                return False
            await db.execute(
                "INSERT INTO findings (run_id, repo_id, kind, severity, fingerprint, payload,"
                "                      created_at) "
                "VALUES (?, NULL, 'RunBudgetRaised', 'warn', ?, ?, ?) "
                "ON CONFLICT (run_id, IFNULL(repo_id, ''), kind, fingerprint) "
                "DO UPDATE SET payload = excluded.payload, created_at = excluded.created_at",
                (
                    run_id,
                    _fingerprint(run_id, f"{ceiling:.6f}"),
                    json.dumps(
                        {
                            "new_max_usd": ceiling,
                            "halt_cleared": True,
                            "raised_by": "--raise-budget",
                        },
                        sort_keys=True,
                    ),
                    stamp,
                ),
            )
            return True

        if await writer.submit(unit):
            return

    # A zero-rowcount here means the ledger moved between `_refuse_bad_raise_budget`'s read and
    # this CAS — another process spent, reserved or raised in the gap. Re-read and report what it
    # says NOW rather than repeating the stale numbers the validator already approved.
    await _with_ro(path, lambda conn: _refuse_bad_raise_budget(conn, run_id, ceiling))
    raise UsageError(
        f"--raise-budget {ceiling:.2f} on run {run_id} matched no `budget_ledger` row and the "
        "re-read found nothing to object to, so the ledger changed underneath this command. "
        "Nothing was written; re-run and the fresh numbers will be checked again."
    )


# --------------------------------------------------------------------------------------
# fleet gc
# --------------------------------------------------------------------------------------


@app.command()
def gc(
    ctx: typer.Context,
    cache_max_age: Annotated[str | None, typer.Option("--cache-max-age")] = None,
    events_keep_runs: Annotated[int | None, typer.Option("--events-keep-runs")] = None,
    disk: Annotated[bool, typer.Option("--disk", help="LRU-evict the Bazel disk cache.")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    force: Annotated[bool, typer.Option("--force", help="Run against a live run.")] = False,
) -> None:
    """Retention only, never state. Refuses a run with live work unless `--force`.

    Exits 9 when `--disk` cannot bring the cache under `budgets.max_disk_gb` or leaves less than
    `preflight.min_free_bytes` free: §10 gives ENOSPC its own code precisely because it is the one
    failure that can void the write guarantee, so it is detected before the next write.
    """
    opts = _options(ctx)
    with _mapped_errors():
        settings = _load_settings(opts)
        path = _require_db(opts)
        _check_schema_version(path)
        age_s = (
            settings.config.gc.cache_max_age_s()
            if cache_max_age is None
            else _duration(cache_max_age)
        )
        keep = (
            settings.config.gc.events_keep_runs if events_keep_runs is None else events_keep_runs
        )
        result = _run(
            _gc_impl(opts, settings, path, age_s=age_s, keep=keep, dry_run=dry_run, force=force)
        )
        if disk:
            result |= _gc_disk(settings, dry_run=dry_run)
        _emit(
            opts,
            result,
            [
                f"{'would evict' if dry_run else 'evicted'} "
                f"{result['llm_cache_rows']} llm_cache row(s), "
                f"{result['event_rows']} event row(s), {result['attempt_rows']} attempt row(s)"
            ]
            + ([f"disk: {result['disk_bytes_freed']} bytes freed"] if disk else []),
        )


def _duration(text: str) -> int:
    try:
        return parse_duration_s(text)
    except ValueError as exc:
        raise UsageError(f"--cache-max-age {text!r}: {exc}") from exc


async def _gc_impl(
    opts: GlobalOptions,
    settings: FleetSettings,
    path: Path,
    *,
    age_s: int,
    keep: int,
    dry_run: bool,
    force: bool,
) -> dict[str, object]:
    cutoff = _iso(datetime.fromtimestamp(_now().timestamp() - age_s, tz=UTC))
    conn = await connect_ro(path)
    try:
        run_id = await _resolve_run(conn, opts)
        live = await _rows(
            conn,
            "SELECT COUNT(*) FROM phases WHERE run_id = ? AND status = 'RUNNING'",
            (run_id,),
        )
        if live and int(live[0][0]) and not force:
            raise UsageError(
                f"run {run_id} has {live[0][0]} phase(s) RUNNING; gc refuses to evict beneath live "
                "work (§10). Pass --force if you know the run is dead."
            )
        keepers = [
            str(row[0])
            for row in await _rows(
                conn,
                "SELECT run_id FROM runs ORDER BY started_at DESC, run_id DESC LIMIT ?",
                (max(keep, 0),),
            )
        ]
        cache_rows = await _rows(
            conn, "SELECT COUNT(*) FROM llm_cache WHERE last_hit_at < ?", (cutoff,)
        )
        placeholders = ",".join("?" for _ in keepers) or "''"
        event_rows = await _rows(
            conn,
            f"SELECT COUNT(*) FROM events WHERE run_id NOT IN ({placeholders})",  # noqa: S608
            tuple(keepers),
        )
        attempt_rows = await _rows(
            conn,
            f"SELECT COUNT(*) FROM attempts WHERE run_id NOT IN ({placeholders})",  # noqa: S608
            tuple(keepers),
        )
    finally:
        await conn.close()

    counts: dict[str, object] = {
        "run_id": run_id,
        "dry_run": dry_run,
        "llm_cache_rows": int(cache_rows[0][0]),
        "event_rows": int(event_rows[0][0]),
        "attempt_rows": int(attempt_rows[0][0]),
        "keep_runs": keepers,
        "cache_max_age_s": age_s,
    }
    _ = settings
    if dry_run:
        return counts

    async with StateWriter(path, owner="fleet-gc") as writer:

        async def unit(db: aiosqlite.Connection) -> None:
            await db.execute("DELETE FROM llm_cache WHERE last_hit_at < ?", (cutoff,))
            await db.execute(
                f"DELETE FROM events WHERE run_id NOT IN ({placeholders})",  # noqa: S608
                tuple(keepers),
            )
            await db.execute(
                f"DELETE FROM attempts WHERE run_id NOT IN ({placeholders})",  # noqa: S608
                tuple(keepers),
            )

        await writer.submit(unit)
    return counts


def _gc_disk(settings: FleetSettings, *, dry_run: bool) -> dict[str, object]:
    """LRU-evict the shared Bazel disk cache down to `budgets.max_disk_gb`, then check the floor.

    **The floor check is unconditional, and that is a fix rather than a detail.** This function
    used to return early when `run.cache_dir` did not exist — which is exactly the state of the
    very first run on a host, i.e. the one case where nothing has been evicted yet and the
    operator most needs to be told there is no room. "No cache to evict" and "there is room" are
    different claims, and only the second one is worth exiting 0 on (§11.3, §13 row 42).
    """
    cache_dir = (settings.root / settings.config.run.cache_dir).resolve()
    limit = settings.config.budgets.max_disk_gb * 1024**3
    files = (
        sorted(
            ((p, p.stat()) for p in cache_dir.rglob("*") if p.is_file()),
            key=lambda item: item[1].st_atime,
        )
        if cache_dir.exists()
        else []
    )
    total = sum(stat.st_size for _, stat in files)
    freed = 0
    for candidate, stat in files:
        if total - freed <= limit:
            break
        if not dry_run:
            with suppress(OSError):
                candidate.unlink()
        freed += stat.st_size

    remaining = total - freed
    free_bytes = disk_free_bytes(cache_dir) + (0 if dry_run else freed)
    if remaining > limit or free_bytes < settings.config.preflight.min_free_bytes:
        raise DiskExhaustedError(
            f"{cache_dir}: {remaining} bytes remain against budgets.max_disk_gb "
            f"({limit} bytes) and {free_bytes} bytes are free against "
            f"preflight.min_free_bytes ({settings.config.preflight.min_free_bytes}). §10 exit 9 — "
            "ENOSPC inside a BEGIN IMMEDIATE is detected before the write, not reported after it."
        )
    return {
        "disk_bytes_freed": freed,
        "disk_dir": str(cache_dir),
        "disk_total_bytes": remaining,
    }


def _require_disk_headroom(settings: FleetSettings) -> dict[str, object]:
    """The §12.22 criterion, at the top of every phase that consumes gigabytes.

    "A run driven against a volume trimmed below `preflight.min_free_bytes` evicts the disk
    cache, then exits **9** with `FailureClass.DISK_EXHAUSTED` and a valid, complete
    `migration_state.json`." Evicting first is what makes the refusal honest: exiting 9 while
    holding 300 GB of reclaimable Bazel cache would be a harness blaming the operator for its own
    garbage. `fleet gc --disk` is the same code path, run by hand.

    Per-repo enforcement is NOT here and must not be: the floor is re-checked before every clone
    and every container start by the workers themselves (§11.3), because a volume that had room
    at startup is the failure mode, not the fix.
    """
    return _gc_disk(settings, dry_run=False)


# --------------------------------------------------------------------------------------
# fleet contracts
# --------------------------------------------------------------------------------------


@contracts_app.command("list")
def contracts_list(
    ctx: typer.Context,
    filters: Annotated[list[str] | None, typer.Option("--filter")] = None,
    extractable_only: Annotated[bool, typer.Option("--extractable-only")] = False,
    sort: Annotated[str | None, typer.Option("--sort", help="repos-freed | confidence")] = None,
    output_format: Annotated[OutputFormat, typer.Option("--format")] = OutputFormat.TABLE,
) -> None:
    """Every `contracts` row with kind, owner, consumers, extractability and status."""
    opts = _options(ctx)
    with _mapped_errors():
        path = _require_db(opts)
        _check_schema_version(path)
        rows = _run(_contracts_rows(opts, path, filters or (), extractable_only, sort))
        if output_format is OutputFormat.JSON or opts.json_output:
            _echo_json(rows)
            return
        for row in rows:
            _echo(
                f"{row['contract_id']:<40} {row['kind']:<16} {row['status']:<12} "
                f"owner={row['owning_repo_id']} consumers={row['consumers']} "
                f"extractable={row['extractable']}"
            )


async def _contracts_rows(
    opts: GlobalOptions,
    path: Path,
    filters: Sequence[str],
    extractable_only: bool,
    sort: str | None,
) -> list[dict[str, object]]:
    allowed = {"kind", "status"}
    clauses: list[str] = []
    params: list[object] = []
    for item in filters:
        key, sep, value = item.partition("=")
        if not sep or key not in allowed:
            raise UsageError(f"--filter {item!r}: expected `kind=X` or `status=X` (§10)")
        clauses.append(f"{key} = ?")
        params.append(value)
    if extractable_only:
        clauses.append("extractable = 1")

    conn = await connect_ro(path)
    try:
        run_id = await _resolve_run(conn, opts)
        where = " AND ".join(["run_id = ?", *clauses])
        # `repos_freed` is not a column — §6 keeps it in the ranking payload — so the consumer
        # count is the sort key, which is the quantity 6c-H ranks by (§3.1 5b).
        order = {
            "repos-freed": "json_array_length(consumer_repo_ids) DESC, contract_id",
            "confidence": "extraction_confidence DESC, contract_id",
        }.get(sort or "", "contract_id")
        # `where`/`order` are assembled from a fixed allowlist above, never from operator text;
        # every VALUE is still a bound parameter.
        sql = (
            "SELECT contract_id, kind, owning_repo_id, status, extractable, "  # noqa: S608
            "       extraction_confidence, json_array_length(consumer_repo_ids), "
            "       hoist_target_path "
            f"  FROM contracts WHERE {where} ORDER BY {order}"
        )
        rows = await _rows(conn, sql, (run_id, *params))
    finally:
        await conn.close()
    keys = (
        "contract_id",
        "kind",
        "owning_repo_id",
        "status",
        "extractable",
        "extraction_confidence",
        "consumers",
        "hoist_target_path",
    )
    return [dict(zip(keys, row, strict=True)) for row in rows]


@contracts_app.command("inspect")
def contracts_inspect(
    ctx: typer.Context,
    contract_id: Annotated[str, typer.Argument()],
    output_format: Annotated[OutputFormat, typer.Option("--format")] = OutputFormat.TABLE,
) -> None:
    """One contract in full: carriers, generated paths, consumers, `confidence_factors`."""
    opts = _options(ctx)
    with _mapped_errors():
        path = _require_db(opts)
        _check_schema_version(path)
        row = _run(_contract_detail(opts, path, contract_id))
        if output_format is OutputFormat.JSON or opts.json_output:
            _echo_json(row)
            return
        for key, value in sorted(row.items()):
            _echo(f"{key:<24} {value}")


async def _contract_detail(
    opts: GlobalOptions, path: Path, contract_id: str
) -> dict[str, object]:
    conn = await connect_ro(path)
    try:
        run_id = await _resolve_run(conn, opts)
        rows = await _rows(
            conn,
            "SELECT contract_id, kind, identifier, owning_repo_id, status, status_detail, "
            "       extractable, extraction_confidence, confidence_factors, source_paths, "
            "       generated_paths, consumer_repo_ids, content_sha256, hoist_target_path "
            "  FROM contracts WHERE run_id = ? AND contract_id = ?",
            (run_id, contract_id),
        )
    finally:
        await conn.close()
    if not rows:
        raise UsageError(f"no contract {contract_id!r} in run {run_id}")
    keys = (
        "contract_id",
        "kind",
        "identifier",
        "owning_repo_id",
        "status",
        "status_detail",
        "extractable",
        "extraction_confidence",
        "confidence_factors",
        "source_paths",
        "generated_paths",
        "consumer_repo_ids",
        "content_sha256",
        "hoist_target_path",
    )
    return dict(zip(keys, rows[0], strict=True))


# --------------------------------------------------------------------------------------
# fleet stubs
# --------------------------------------------------------------------------------------


@stubs_app.command("list")
def stubs_list(
    ctx: typer.Context,
    filters: Annotated[list[str] | None, typer.Option("--filter")] = None,
    unresolved_only: Annotated[bool, typer.Option("--unresolved-only")] = False,
    output_format: Annotated[OutputFormat, typer.Option("--format")] = OutputFormat.TABLE,
) -> None:
    """Every `stubs` row plus the "degraded and unresolved" reconciliation view (§3.5.1)."""
    opts = _options(ctx)
    with _mapped_errors():
        path = _require_db(opts)
        _check_schema_version(path)
        rows = _run(_stub_rows(opts, path, filters or (), unresolved_only))
        if output_format is OutputFormat.JSON or opts.json_output:
            _echo_json(rows)
            return
        for row in rows:
            _echo(
                f"{row['consumer_repo_id']:<28} → {row['provider_repo_id']:<28} "
                f"{row['coord_key']:<40} {row['state']:<12} rounds={row['rounds']}"
            )


async def _stub_rows(
    opts: GlobalOptions, path: Path, filters: Sequence[str], unresolved_only: bool
) -> list[dict[str, object]]:
    columns = {"state": "state", "consumer": "consumer_repo_id", "provider": "provider_repo_id"}
    clauses: list[str] = []
    params: list[object] = []
    for item in filters:
        key, sep, value = item.partition("=")
        if not sep or key not in columns:
            raise UsageError(f"--filter {item!r}: expected state=/consumer=/provider= (§10)")
        clauses.append(f"{columns[key]} = ?")
        params.append(value)
    if unresolved_only:
        clauses.append("state IN ('ACTIVE','SUPERSEDED')")

    conn = await connect_ro(path)
    try:
        run_id = await _resolve_run(conn, opts)
        where = " AND ".join(["run_id = ?", *clauses])
        # `where` is built from the `columns` allowlist; every value is a bound parameter.
        sql = (
            "SELECT consumer_repo_id, provider_repo_id, stub_coord_key, "  # noqa: S608
            "       stub_fidelity, state, "
            "       revalidation_round, COALESCE(abandon_reason, '') "
            f"  FROM stubs WHERE {where} "
            " ORDER BY consumer_repo_id, stub_coord_key, revalidation_round"
        )
        rows = await _rows(conn, sql, (run_id, *params))
    finally:
        await conn.close()
    keys = (
        "consumer_repo_id",
        "provider_repo_id",
        "coord_key",
        "stub_fidelity",
        "state",
        "rounds",
        "abandon_reason",
    )
    return [dict(zip(keys, row, strict=True)) for row in rows]


@stubs_app.command("resolve")
def stubs_resolve(
    ctx: typer.Context,
    provider: Annotated[str, typer.Argument()],
    revalidation: Annotated[Revalidation | None, typer.Option("--revalidation")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """The manual T1 trigger: supersede a provider's ACTIVE stubs and enqueue revalidation."""
    with _mapped_errors():
        _phase_preflight(ctx)
        _ = (provider, revalidation, dry_run)
        _unavailable("stubs resolve", "src/fleet/workers/buildverify.py")


@stubs_app.command("abandon")
def stubs_abandon(
    ctx: typer.Context,
    consumer: Annotated[str, typer.Argument()],
    coord_key: Annotated[str, typer.Argument()],
    reason: Annotated[str, typer.Option("--reason", help="Required; recorded.")],
) -> None:
    """The manual T4: `ABANDONED` with `abandon_reason='OPERATOR'`; the consumer stays DEGRADED."""
    opts = _options(ctx)
    with _mapped_errors():
        path = _require_db(opts)
        _check_schema_version(path)
        if not reason.strip():
            raise UsageError("--reason is required and must not be empty (§10)")
        result = _run(_stub_abandon(opts, path, consumer, coord_key, reason))
        _emit(opts, result, [f"abandoned {consumer} → {coord_key} ({result['rows']} row(s))"])


async def _stub_abandon(
    opts: GlobalOptions, path: Path, consumer: str, coord_key: str, reason: str
) -> dict[str, object]:
    conn = await connect_ro(path)
    try:
        run_id = await _resolve_run(conn, opts)
        existing = await _rows(
            conn,
            "SELECT COUNT(*) FROM stubs WHERE run_id = ? AND consumer_repo_id = ? "
            "  AND stub_coord_key = ? AND state IN ('ACTIVE','SUPERSEDED')",
            (run_id, consumer, coord_key),
        )
    finally:
        await conn.close()
    if not int(existing[0][0]):
        raise UsageError(
            f"no ACTIVE/SUPERSEDED stub {consumer} → {coord_key} in run {run_id}; "
            "`fleet stubs list --unresolved-only` shows what can be abandoned"
        )

    stamp = _iso(_now())
    async with StateWriter(path, owner="fleet-stubs") as writer:

        async def unit(db: aiosqlite.Connection) -> int:
            cursor = await db.execute(
                # `resolved_at` is not decoration: schema.sql CHECKs that a non-ACTIVE row has
                # one, so an abandon that skipped it would be rejected by the database.
                "UPDATE stubs SET state = 'ABANDONED', abandon_reason = 'OPERATOR', "
                "       state_changed_at = ?, resolved_at = COALESCE(resolved_at, ?) "
                " WHERE run_id = ? AND consumer_repo_id = ? AND stub_coord_key = ? "
                "   AND state IN ('ACTIVE','SUPERSEDED')",
                (stamp, stamp, run_id, consumer, coord_key),
            )
            await db.execute(
                "INSERT INTO findings (run_id, repo_id, kind, severity, fingerprint, payload,"
                "                      created_at) "
                "VALUES (?, ?, 'StubAbandoned', 'warn', ?, ?, ?) "
                "ON CONFLICT (run_id, IFNULL(repo_id, ''), kind, fingerprint) "
                "DO UPDATE SET payload = excluded.payload, created_at = excluded.created_at",
                (
                    run_id,
                    consumer,
                    _fingerprint(run_id, consumer, coord_key),
                    redact_text(
                        json.dumps(
                            {"consumer": consumer, "coord_key": coord_key, "reason": reason},
                            sort_keys=True,
                        )
                    ),
                    stamp,
                ),
            )
            return cursor.rowcount

        rows = await writer.submit(unit)
    return {"run_id": run_id, "consumer": consumer, "coord_key": coord_key, "rows": rows}


# --------------------------------------------------------------------------------------
# fleet models
# --------------------------------------------------------------------------------------


@models_app.command("list")
def models_list(
    ctx: typer.Context,
    role: Annotated[str | None, typer.Option("--role")] = None,
    tier: Annotated[ModelTier | None, typer.Option("--tier")] = None,
    output_format: Annotated[OutputFormat, typer.Option("--format")] = OutputFormat.TABLE,
) -> None:
    """Resolve the active profile and print every `role → tier → [backend, model_id, …]`.

    Offline and side-effect-free, so it doubles as the "what will this run actually call"
    pre-flight — including for `--profile`, which is what selects the profile resolved here.
    """
    opts = _options(ctx)
    with _mapped_errors():
        settings = _load_settings(opts)
        router = llm_router(settings)
        rows: list[dict[str, object]] = []
        for name, route in router.routes():
            if role is not None and name != role:
                continue
            if tier is not None and route.tier is not tier:
                continue
            rows += [
                {
                    "role": name,
                    "tier": route.tier.value,
                    "backend": target.backend,
                    "model_id": target.model_id,
                    "base_url": target.base_url,
                    "effort": target.effort,
                }
                for target in route.targets
            ]
        if output_format is OutputFormat.JSON or opts.json_output:
            # `llm_cache` is reported here because it is a property of the call this pre-flight
            # is predicting: `off` means every route below is a fresh, billed call. It is read
            # off `settings`, not off `opts`, because `_load_settings` has already folded
            # `--llm-cache` in — so this echoes the mode the run will RESOLVE, including the
            # `fleet.yaml` value when the flag was not typed.
            _echo_json(
                {
                    "profile": settings.profile,
                    "llm_cache": settings.config.llm.cache_mode,
                    "routes": rows,
                }
            )
            return
        _echo(f"profile {settings.profile}  (llm-cache {settings.config.llm.cache_mode})")
        for row in rows:
            _echo(
                f"  {row['role']:<20} {row['tier']:<10} {row['backend']:<20} "
                # `or "-"`: ADR-0075 made `effort` optional, and an undeclared one must not
                # reach an operator as the Python literal `None`. The JSON branch above keeps
                # the real value — `null` is the correct machine answer; `-` is the human one.
                f"{row['model_id']:<32} effort={row['effort'] or '-'}"
            )


@models_app.command("profiles")
def models_profiles(
    ctx: typer.Context,
    output_format: Annotated[OutputFormat, typer.Option("--format")] = OutputFormat.TABLE,
) -> None:
    """List the profiles in `config/models.yaml` and mark the active one."""
    opts = _options(ctx)
    with _mapped_errors():
        settings = _load_settings(opts)
        names = sorted(settings.models.profiles)
        if output_format is OutputFormat.JSON or opts.json_output:
            _echo_json({"active": settings.profile, "profiles": names})
            return
        for name in names:
            _echo(f"{'*' if name == settings.profile else ' '} {name}")


@models_app.command("check")
def models_check(
    ctx: typer.Context,
    tier: Annotated[ModelTier | None, typer.Option("--tier")] = None,
    backend: Annotated[str | None, typer.Option("--backend")] = None,
    timeout_s: Annotated[int, typer.Option("--timeout-s")] = 20,
    strict: Annotated[
        bool, typer.Option("--strict", help="Any unreachable target ⇒ exit 8.")
    ] = False,
    output_format: Annotated[OutputFormat, typer.Option("--format")] = OutputFormat.TABLE,
) -> None:
    """Probe each configured target once. The only command that talks to a model without working.

    The transport half is wired: a target whose `backend` has not registered (its SDK is absent,
    or the name is a typo) can never be reached, and under `--strict` that is exit 8 — ADR-0023's
    fail-closed rule, reported before a run spends anything discovering it.
    """
    opts = _options(ctx)
    with _mapped_errors():
        settings = _load_settings(opts)
        router = llm_router(settings)
        available = set(registry())
        rows: list[dict[str, object]] = []
        for name, route in router.routes():
            if tier is not None and route.tier is not tier:
                continue
            for target in route.targets:
                if backend is not None and target.backend != backend:
                    continue
                rows.append(
                    {
                        "role": name,
                        "tier": route.tier.value,
                        "backend": target.backend,
                        "model_id": target.model_id,
                        "registered": target.backend in available,
                    }
                )
        unreachable = sorted({str(r["backend"]) for r in rows if not r["registered"]})
        if output_format is OutputFormat.JSON or opts.json_output:
            _echo_json({"profile": settings.profile, "targets": rows, "unreachable": unreachable})
        else:
            for row in rows:
                mark = "ok" if row["registered"] else "UNREGISTERED"
                _echo(f"  {row['role']:<20} {row['backend']:<20} {row['model_id']:<32} {mark}")
        _ = timeout_s
        if unreachable and strict:
            raise FleetCliError(
                f"backend(s) {', '.join(unreachable)} are not registered, so every target routed "
                "through them is unreachable. ADR-0023 is fail-closed: the run stops admitting "
                "work rather than silently downgrading a tier (§11.8).",
                exit_code=ExitCode.TIER_UNAVAILABLE,
            )


# --------------------------------------------------------------------------------------
# entry points
# --------------------------------------------------------------------------------------


def build_app() -> typer.Typer:
    """The Typer app. A function so a test can hold it without importing module state."""
    return app


def command_paths() -> tuple[tuple[str, ...], ...]:
    """Every invocable `fleet …` path, derived from the click group.

    Derived rather than listed: a verb added below is covered by the `--help` test the moment it
    exists, which is the only way a parametrized suite stays honest as §10 grows.
    """
    root = typer.main.get_command(app)

    def walk(command: ClickCommand, prefix: tuple[str, ...]) -> list[tuple[str, ...]]:
        if isinstance(command, TyperGroup):
            out: list[tuple[str, ...]] = []
            for name in sorted(command.commands):
                out += walk(command.commands[name], (*prefix, name))
            return out
        return [prefix]

    return tuple(walk(root, ()))


def main() -> None:
    """Console-script entry point (`fleet`, `python -m fleet`)."""
    app()
