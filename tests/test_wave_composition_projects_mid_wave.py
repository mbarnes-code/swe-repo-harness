"""The four wave composition roots in `cli.py` hand down a projector that is actually LIVE.

`RunContext.projector` was declared, consumed by three `ctx.project()` calls in
`orchestrator/runner.py`, and **never constructed anywhere in `src/`**: `Projector(` sites 0,
`projector=` keyword arguments 0, so all three of those calls were permanent no-ops on every
shipped run. `docs/SPEC.md` §6 specifies a debounced projector and accepts "staleness of up to
one second"; what shipped was staleness bounded by `budgets.wave_max_wallclock_s`, whose default
is 14 400 s. It was wired at `17553b5`. Nothing in `tests/` drove a composition root before this
file, which is the whole reason a specified feature shipped inert.

**The assertion here is that the state vector MOVED while the wave was still running.** Two
weaker assertions are deliberately refused:

* `ctx.projector is not None` — a field-identity check. It passes over a projector that is built,
  handed down and **never started**, which is a projection just as dead as an absent one. The
  same shape passed throughout the period `RunContext.llm_cache` was dead one subsystem over
  (`tests/test_run_context_llm_cache.py`), so this project has been bitten by it twice.
* `sha256` of `migration_state.json` — "the projector RAN", not "the state MOVED", and a digest-
  keyed instrument passed three of its four validation checks in round F and was caught only by
  the cosmetic control. At the time this file was written, `MigrationState.updated_at` carried
  `default_factory=utcnow`, so every rebuild yielded different bytes even from a database nothing
  had touched — the round-F failure mode, reproduced below as the case that used to discriminate
  it. **Corrected 2026-09-01 (round Y task 1, ADR-0106, SPEC §12.17):** `updated_at` is now
  derived from the latest of the already-read `phases`/`waves`/`contracts`/`collisions`
  timestamps, so a rebuild of a genuinely untouched database is byte-identical too — see
  `test_projection.py::test_two_projections_of_an_untouched_database_are_byte_identical`. A
  whole-file digest is still the wrong quantity for THIS file's purpose (it answers "did the
  projector run", never "did the wave admit a new repo"), just no longer for the round-F reason.

The quantity is therefore **the ordered sequence of distinct per-repo `status` vectors**
`{repo_id: phases[<this phase>].status}` read out of `migration_state.json` while
`cli._run_*_wave` is executing, and the property asserted is that a **strictly intermediate**
vector appears — at least one repo already terminal while at least one is not. A mid-wave refresh
cannot leave that quantity unchanged: the vector is a pure function of the `phases` rows, the
wave moves each repo `PENDING → RUNNING → SUCCEEDED` at distinct instants, and any rebuild
between the first and last terminal write reports a vector equal to neither the pre-wave one nor
the final one. The vector reads `status` and nothing else, so it is indifferent to whether the
file's bytes move for any other reason: a rebuild from an unchanged database now reports an
identical vector AND (post-ADR-0106) identical bytes; either way the vector is the property
this file needs.

An intermediate vector is also the one thing the trailing `project_once(...)` every wave command
already runs **cannot** manufacture: that call happens once, after the wave, from the final
database. Observing `SUCCEEDED/RUNNING/RUNNING` at all is proof that something wrote the file
during the wave.

**The control that makes a null meaningful** is `_phase_statuses()`, read out of `phases` after
the wave: without it, "no refresh observed" is indistinguishable from "nothing happened", and a
wave that silently admitted nobody would certify the projector green-by-vacuum.

## Shape, and what is real

Real temp database, real `StateWriter`, real `SqliteSchedulerStore` and `WaveScheduler`, real
`PhaseRunner`, real leases and fences, real `RunContext` and real `Projector` — both built by
`cli.py` itself, not by this file — real debounce (`MIN_PROJECTION_INTERVAL_S`, un-patched), and
a real `FleetSettings.load` of the shipped `config/` directory. **Only the phase's own work is
replaced**: each root's pipeline worker, payload factory and result sink are monkeypatched at
their `fleet.cli` attribute, because what is under test is the composition, not any phase's
labour. `ctx.project()` is never called by this file; every call comes from `PhaseRunner`.

The staggering is causal rather than timed: repo *k*'s stub worker blocks until the projection
on disk shows repos 0..*k*-1 terminal (`_PROJECTION_GATE_TIMEOUT_S`, then gives up and returns
normally). So on a live projector the wave takes one debounce window per repo, and on a dead one
it fails the assertion instead of hanging.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar
from uuid import UUID

import pytest

from fleet import cli
from fleet.graph.sequence import WavePlan
from fleet.models.enums import Phase
from fleet.models.graph import MigrationWave
from fleet.orchestrator.scheduler import SqliteSchedulerStore
from fleet.settings import FleetSettings
from fleet.state import db as dbmod
from fleet.state.db import StateWriter, connect_ro, initialize_database
from fleet.state.projection import DEFAULT_PROJECTION_PATH, project_once
from fleet.state.repository import SqliteStateRepository
from fleet.workers.base import (
    BaseWorker,
    WorkerContext,
    WorkerInput,
    WorkerOutput,
    WorkerResult,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = REPO_ROOT / "config"

NOW = datetime(2026, 8, 25, 9, 0, 0, tzinfo=UTC)
RUN_ID = UUID("33333333-3333-4333-8333-333333333333")
RUN = str(RUN_ID)
WAVE_INDEX = 0

#: Three repos, so there are TWO distinct intermediate vectors to observe rather than one — a
#: single one could in principle be produced by a rebuild that raced the wave's end, two in
#: strictly increasing order cannot.
REPOS: tuple[str, ...] = ("repo-a", "repo-b", "repo-c")

#: Long enough for the shipped 1 Hz debounce with slack; short enough that a DEAD projector
#: fails the assertion in bounded time instead of hanging the suite. Two gated repos, so a
#: dead-projector run costs 2 × this per case.
_PROJECTION_GATE_TIMEOUT_S = 6.0

_POLL_S = 0.004

#: Statuses a repo holds while the wave still owes it work. `-` is "this phase has no row in the
#: projection yet". Everything else `RepoStatus` defines is a verdict the wave has finished
#: writing, and this file deliberately does not enumerate those: a new terminal status added to
#: the enum must count as terminal here without anyone remembering to come back.
_UNSETTLED: frozenset[str] = frozenset({"-", "PENDING", "RUNNING"})


# ======================================================================================
# the instrument
# ======================================================================================


def _status_vector(raw: bytes, phase: Phase) -> dict[str, str] | None:
    """`{repo_id: status}` for this phase, or `None` if the file is not yet a readable state.

    Reads `status` and nothing else. That is the whole point: `updated_at`, `usage` and every
    other churning field are excluded by construction, so this cannot report movement that the
    database did not have.
    """
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError:  # pragma: no cover - atomic_write makes a torn read impossible
        return None
    repos = doc.get("repos") or {}
    vector: dict[str, str] = {}
    for repo_id in REPOS:
        phases = (repos.get(repo_id) or {}).get("phases") or {}
        vector[repo_id] = str((phases.get(str(int(phase))) or {}).get("status", "-"))
    return vector


def _settled(vector: Mapping[str, str]) -> int:
    """How many repos the wave has finished with, per this vector."""
    return sum(1 for status in vector.values() if status not in _UNSETTLED)


def _read_vector(path: Path, phase: Phase) -> dict[str, str] | None:
    """Sync on purpose: a `Path` read has no place in a coroutine body (ruff ASYNC240), and the
    projection is a few kilobytes written by `os.replace`, so there is nothing to await."""
    try:
        return _status_vector(path.read_bytes(), phase)
    except FileNotFoundError:
        return None


class _ProjectionWatcher:
    """Polls `migration_state.json` and keeps the ordered DISTINCT status vectors it saw."""

    def __init__(self, path: Path, phase: Phase) -> None:
        self._path = path
        self._phase = phase
        self._stop = False
        self._task: asyncio.Task[None] | None = None
        self.vectors: list[dict[str, str]] = []
        #: Byte-level rebuilds, kept ONLY so a failure can say whether the projector was idle or
        #: was running and writing an unchanged state. Never asserted on — see the module
        #: docstring on why a whole-file digest is the wrong quantity.
        self.rebuilds = 0
        self._last_raw: bytes | None = None

    def _sample(self) -> None:
        try:
            raw = self._path.read_bytes()
        except FileNotFoundError:
            return
        if raw != self._last_raw:
            self._last_raw = raw
            self.rebuilds += 1
        vector = _status_vector(raw, self._phase)
        if vector is not None and (not self.vectors or vector != self.vectors[-1]):
            self.vectors.append(vector)

    async def _loop(self) -> None:
        while not self._stop:
            self._sample()
            await asyncio.sleep(_POLL_S)

    def start(self) -> None:
        self._sample()
        self._task = asyncio.create_task(self._loop(), name="w3-projection-watcher")

    async def stop(self) -> None:
        self._stop = True
        if self._task is not None:
            await self._task
            self._task = None
        self._sample()

    def intermediate(self) -> list[dict[str, str]]:
        """Vectors in which SOME repo is terminal and SOME repo is not yet.

        Neither the pre-wave rebuild nor the command's trailing `project_once` can produce one:
        the first sees no repo done and the second sees them all done.
        """
        return [v for v in self.vectors if 0 < _settled(v) < len(REPOS)]


async def _await_projection(path: Path, phase: Phase, done: Sequence[str]) -> bool:
    """Block until the projection ON DISK shows every repo in `done` terminal. `False` on timeout.

    Returning rather than raising is deliberate: a dead projector must make this file fail its
    assertion with the measured vectors in the message, not disappear into a worker exception the
    runner would record as `FailureClass.UNKNOWN` and a reader would misdiagnose.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _PROJECTION_GATE_TIMEOUT_S
    while loop.time() < deadline:
        vector = _read_vector(path, phase)
        if vector is not None and all(vector.get(r) == "SUCCEEDED" for r in done):
            return True
        await asyncio.sleep(_POLL_S)
    return False


# ======================================================================================
# the stubs — the phase's own labour, and nothing else
# ======================================================================================


class _StubInput(WorkerInput):
    repo_id: str


class _StubOutput(WorkerOutput):
    pass


def _stub_worker_class(phase: Phase, projection: Path) -> type[BaseWorker[Any, Any]]:
    """A worker for `phase` whose only behaviour is to WAIT ITS TURN, observed off the disk.

    Repo *k* returns as soon as the projection shows repos 0..*k*-1 terminal, which is what makes
    the intermediate vectors deterministic instead of a timing race: on a live projector each
    completion is one debounce window after the previous one, and the watcher polls 250× faster.
    """

    class _StubWorker(BaseWorker[_StubInput, _StubOutput]):
        __slots__ = ()

        name: ClassVar[str] = "w3-stub"
        input_model: ClassVar[type[WorkerInput]] = _StubInput
        output_model: ClassVar[type[WorkerOutput]] = _StubOutput
        cancel_grace_s: ClassVar[float] = 0.01

        def __init__(self, **_kwargs: Any) -> None:
            """`**_kwargs` so `BuildPipelineWorker(bazel_runner=...)`'s call shape still fits."""

        async def run(
            self, ctx: WorkerContext, payload: _StubInput
        ) -> WorkerResult[_StubOutput]:
            index = REPOS.index(ctx.repo_id)
            if index:
                await _await_projection(projection, phase, REPOS[:index])
            return WorkerResult(status="ok", output=_StubOutput())

        async def preconditions_hold(self, ctx: WorkerContext, payload: _StubInput) -> bool:
            return True

    _StubWorker.phase = phase  # type: ignore[misc]
    return _StubWorker


async def _stub_payloads(
    *, repo_id: str, phase: Phase, attempt: int, remaining_units: Sequence[str] | None
) -> _StubInput:
    return _StubInput(repo_id=repo_id)


class _StubSink:
    """Accepts the phase sink's constructor shape and persists nothing.

    Evidence writing is the phase's business (`ResultSink`'s own contract); the terminal `phases`
    write this file measures is the RUNNER's, and stays real.
    """

    def __init__(self, *_args: Any, **_kwargs: Any) -> None: ...

    async def __call__(self, **_kwargs: Any) -> None: ...


class _StubClaimHook:
    """Accepts `_TransformClaimHook`'s constructor shape and does nothing (D89 Phase 2 Task A,
    ADR-0102).

    `_TransformClaimHook` is TRANSFORM's own phase labour — it reads `payload.sources`/
    `.targets`, which only a real `TransformInput` carries — so it belongs beside `_StubWorker`/
    `_StubSink` in the stub set, not among "everything else" this file keeps real. Without this,
    `_drive_transform`'s `_StubInput` payload (no `.sources`) makes the real hook raise
    `AttributeError` inside `_dispatch`, which the runner's isolation swallows into a per-repo
    failure and the wave never reaches `SUCCEEDED` — a regression this file's own DB-truth
    control (see the module docstring) is what caught.
    """

    def __init__(self, *_args: Any, **_kwargs: Any) -> None: ...

    async def __call__(self, **_kwargs: Any) -> None: ...


# ======================================================================================
# the fixture — real everything below the phase's labour
# ======================================================================================


@dataclass(slots=True)
class _Bed:
    settings: FleetSettings
    db_path: Path
    repository: SqliteStateRepository
    writer: StateWriter
    read_conn: Any
    pool: Executor
    projection: Path

    async def phase_statuses(self, phase: Phase) -> dict[str, str]:
        """The DB TRUTH control. Read raw out of `phases`, which is authoritative (§11.5)."""
        async with self.read_conn.execute(
            "SELECT repo_id, status FROM phases WHERE run_id = ? AND phase = ?",
            (RUN, int(phase)),
        ) as cursor:
            rows = await cursor.fetchall()
        return {str(r[0]): str(r[1]) for r in rows}


@pytest.fixture(autouse=True)
def _release_slot() -> Iterator[None]:
    yield
    dbmod._release_write_slot()


@pytest.fixture
async def bed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_Bed]:
    """`DEFAULT_PROJECTION_PATH` is RELATIVE, so the wave writes into the process's cwd — chdir
    into `tmp_path` or a test writes `migration_state.json` into the checkout."""
    monkeypatch.chdir(tmp_path)
    settings = FleetSettings.load(CONFIG_DIR, env={})
    db_path = tmp_path / "state" / "fleet.db"
    await initialize_database(db_path)
    pool = ThreadPoolExecutor(1)
    async with StateWriter(db_path, owner="test-wave-composition") as writer:
        read_conn = await connect_ro(db_path)
        try:
            repository = SqliteStateRepository(writer=writer, read_conn=read_conn)
            await repository.upsert_run(
                RUN, started_at=NOW, config_sha256="c" * 64, harness_version="0.1.0"
            )
            await repository.open_budget_ledger(RUN, max_usd=1000.0, now=NOW)
            store = SqliteSchedulerStore(writer=writer, read_conn=read_conn)
            await store.record_plan(
                RUN,
                WavePlan(
                    waves=(MigrationWave(wave_index=WAVE_INDEX, repo_ids=sorted(REPOS)),),
                    wave_index_by_node={},
                    cycle_findings=(),
                    excluded_repo_ids=(),
                ),
                now=NOW,
                max_usd_per_repo=8.0,
            )
            for repo_id in REPOS:
                await repository.upsert_repo(
                    repo_id,
                    name=repo_id,
                    url=f"https://example.invalid/{repo_id}.git",
                    now=NOW,
                )
            yield _Bed(
                settings=settings,
                db_path=db_path,
                repository=repository,
                writer=writer,
                read_conn=read_conn,
                pool=pool,
                projection=tmp_path / DEFAULT_PROJECTION_PATH,
            )
        finally:
            await read_conn.close()
            pool.shutdown(wait=False)


async def _seed_phase(bed: _Bed, phase: Phase) -> None:
    for repo_id in REPOS:
        await bed.repository.upsert_phase(RUN, repo_id, phase, now=NOW)


# ======================================================================================
# the four composition roots
# ======================================================================================
#
# `src/fleet/cli.py` has FIVE `RunContext(` sites. Four of them are these wave functions; the
# fifth is `_emit_prs`, which assembles a context to open pull requests and drives no wave — it
# is the one site that legitimately passes no projector, and it is excluded here for that reason
# and not by oversight.


def _patch(
    monkeypatch: pytest.MonkeyPatch, phase: Phase, bed: _Bed, names: Mapping[str, str]
) -> None:
    monkeypatch.setattr(cli, names["worker"], _stub_worker_class(phase, bed.projection))
    monkeypatch.setattr(cli, names["payloads"], lambda *a, **k: _stub_payloads)
    monkeypatch.setattr(cli, names["sink"], _StubSink)


async def _drive_scan(bed: _Bed, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(
        monkeypatch,
        Phase.SCAN,
        bed,
        {"worker": "ScanPipelineWorker", "payloads": "_scan_payloads", "sink": "_ScanSink"},
    )
    await cli._run_scan_wave(
        bed.settings,
        repository=bed.repository,
        writer=bed.writer,
        read_conn=bed.read_conn,
        db_path=bed.db_path,
        run_id=RUN,
        fleet=(),
        members=REPOS,
        steps=(),
        pool=bed.pool,
        lanes=4,
        symbol_batch_rows=None,
        evidence=cli._ScanEvidence(),
    )


async def _drive_transform(bed: _Bed, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(
        monkeypatch,
        Phase.TRANSFORM,
        bed,
        {
            "worker": "TransformPipelineWorker",
            "payloads": "_transform_payloads",
            "sink": "_TransformSink",
        },
    )
    monkeypatch.setattr(cli, "_TransformClaimHook", _StubClaimHook)
    await cli._run_transform_wave(
        bed.settings,
        repository=bed.repository,
        writer=bed.writer,
        read_conn=bed.read_conn,
        db_path=bed.db_path,
        run_id=RUN,
        wave_index=WAVE_INDEX,
        plans={},
        rules=(),
        only=None,
        pool=bed.pool,
        evidence=cli._TransformEvidence(),
    )


async def _drive_build(bed: _Bed, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(
        monkeypatch,
        Phase.BUILD,
        bed,
        {"worker": "BuildPipelineWorker", "payloads": "_build_payloads", "sink": "_BuildSink"},
    )
    await cli._run_build_wave(
        bed.settings,
        config=bed.settings.config,
        repository=bed.repository,
        writer=bed.writer,
        read_conn=bed.read_conn,
        db_path=bed.db_path,
        run_id=RUN,
        wave_index=WAVE_INDEX,
        plans={},
        members=REPOS,
        monorepo=bed.db_path.parent,
        lock_dir=bed.db_path.parent,
        build_root=bed.db_path.parent,
        sandboxed=False,
        pool=bed.pool,
        evidence=cli._BuildEvidence(),
    )


async def _drive_verify(bed: _Bed, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(
        monkeypatch,
        Phase.VERIFY,
        bed,
        {"worker": "VerifyPipelineWorker", "payloads": "_verify_payloads", "sink": "_VerifySink"},
    )
    await cli._run_verify_wave(
        bed.settings,
        config=bed.settings.config,
        repository=bed.repository,
        writer=bed.writer,
        read_conn=bed.read_conn,
        db_path=bed.db_path,
        run_id=RUN,
        wave_index=WAVE_INDEX,
        plans={},
        members=REPOS,
        verify_root=bed.db_path.parent,
        sandboxed=False,
        rdeps_limit=0,
        rdeps_sample_n=0,
        affected_only=False,
        pool=bed.pool,
        evidence=cli._VerifyEvidence(),
    )


_ROOTS: dict[str, tuple[Phase, Callable[[_Bed, pytest.MonkeyPatch], Any]]] = {
    "_run_scan_wave": (Phase.SCAN, _drive_scan),
    "_run_transform_wave": (Phase.TRANSFORM, _drive_transform),
    "_run_build_wave": (Phase.BUILD, _drive_build),
    "_run_verify_wave": (Phase.VERIFY, _drive_verify),
}


@pytest.mark.parametrize("root", sorted(_ROOTS))
async def test_a_wave_refreshes_the_projection_while_it_is_still_running(
    root: str, bed: _Bed, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The composition root named by `root` hands down a projector that is live DURING the wave.

    **UNIQUELY DISCRIMINATES** against dropping `projector=projector` from *this* root's
    `RunContext(` call and no other: each of the four parameters is the only case that reddens
    under its own site's mutation, which is precisely the gap a single-root test would leave for
    the other three. It also reddens — all four together — under `await projector.start()` being
    removed (a projector that is constructed, handed down and never started: the state in which
    `ctx.projector is not None` is TRUE and the projection is as dead as before the fix) and
    under `RunContext.project()`'s body being replaced by `pass`.

    It stays GREEN under a cosmetic reflow of the wiring region in all four roots.
    """
    phase, drive = _ROOTS[root]
    await _seed_phase(bed, phase)

    # The previous command's `project_once`: the file exists on disk and is STALE, which is the
    # state a real wave starts from and the only one in which "it changed" means anything.
    await project_once(bed.db_path, run_id=RUN_ID, path=bed.projection)
    before = _status_vector(bed.projection.read_bytes(), phase)
    assert before == dict.fromkeys(REPOS, "PENDING"), before

    watcher = _ProjectionWatcher(bed.projection, phase)
    watcher.start()
    try:
        await drive(bed, monkeypatch)
    finally:
        await watcher.stop()

    # THE DB-TRUTH CONTROL. Without it "no refresh observed" is indistinguishable from "the wave
    # admitted nobody", and a vacuous wave would certify the projector green.
    truth = await bed.phase_statuses(phase)
    assert truth == dict.fromkeys(REPOS, "SUCCEEDED"), (
        f"the wave itself did not move {phase.name} to terminal ({truth}); the projection "
        "measurement below would be vacuous"
    )

    intermediate = watcher.intermediate()
    assert len(intermediate) >= 2, (
        f"{root}: migration_state.json never showed a state STRICTLY BETWEEN all-PENDING and "
        f"all-SUCCEEDED while the wave ran, so no projector refreshed it mid-wave — the state "
        f"the harness shipped in. Distinct vectors seen: {watcher.vectors}. Byte-level rebuilds "
        f"seen: {watcher.rebuilds} (a non-zero count here beside zero intermediate vectors "
        f"means the projector ran and the STATE did not move, which is a different defect)."
    )

    # Monotone, and it passes through BOTH intermediate counts. A single intermediate vector
    # could in principle be a rebuild racing the wave's end; 1 then 2 in order cannot be, and
    # `updated_at` churn cannot produce either.
    settled = [_settled(v) for v in watcher.vectors]
    assert settled == sorted(settled), (
        f"{root}: the observed vectors do not advance monotonically: {watcher.vectors}"
    )
    assert {1, len(REPOS) - 1} <= set(settled), (
        f"{root}: the projection did not pass through every intermediate stage of the wave "
        f"(settled counts observed: {settled}); vectors: {watcher.vectors}"
    )


# ======================================================================================
# the instrument, held to the discipline it enforces
# ======================================================================================


async def test_a_rebuild_from_an_unchanged_database_moves_neither_bytes_nor_vector(
    bed: _Bed,
) -> None:
    """Why `_status_vector`, not `sha256(migration_state.json)`, is still the right quantity here
    — executable, not asserted. History, corrected 2026-09-01 (round Y task 1, ADR-0106):

    This case used to assert `first != second` — two `project_once` rebuilds with NOTHING
    happening in between produced DIFFERENT bytes (`MigrationState.updated_at` was
    `default_factory=utcnow`, so it churned on wall-clock time alone) while the status vector
    stayed equal, which is exactly how round F's first attempt at the mid-wave measurement above
    passed three of its four validation checks under the wrong quantity: its cosmetic control
    read 4 where it had to read 0. ADR-0106 fixed the root cause for SPEC §12.17 — `updated_at`
    is now derived from already-read `phases`/`waves`/`contracts`/`collisions` timestamps, so the
    two rebuilds below are now byte-identical too, and `first != second` would itself be the
    wrong assertion to make.

    What this case still proves: the mid-wave instrument's choice of quantity (`_status_vector`,
    not a digest) was never dependent on the churn being present — the vector agreed with itself
    whether or not the bytes did, and it still does now that ADR-0106 has made them agree too.
    A digest-keyed instrument is STILL the wrong tool for the wave cases above (it answers "did
    the projector run", never "did the wave admit a new repo"); it simply no longer produces a
    false positive on an untouched database while being wrong for that separate reason.
    """
    await _seed_phase(bed, Phase.TRANSFORM)
    await project_once(bed.db_path, run_id=RUN_ID, path=bed.projection)
    first = bed.projection.read_bytes()
    await project_once(bed.db_path, run_id=RUN_ID, path=bed.projection)
    second = bed.projection.read_bytes()

    assert first == second, (
        "two rebuilds of an untouched database produced different bytes; ADR-0106's "
        "`_derive_updated_at` no longer holds SPEC §12.17's byte-identity, or something else in "
        "the projection is newly non-deterministic"
    )
    assert _status_vector(first, Phase.TRANSFORM) == _status_vector(second, Phase.TRANSFORM), (
        "the status vector moved without the database moving — the instrument is reading "
        "something other than the state"
    )
