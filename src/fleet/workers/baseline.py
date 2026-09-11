"""Phase 1 (§3.1 step 1): the native, PRE-migration build/test baseline (§12.11/D116 Leg B,
ADR-0135, `docs/DECISIONS.md`).

**What this worker is, in one sentence.** For every repo whose adapter declares a
`native_baseline()` (Leg A, round VI task 105 — PyPI and NPM today), run that adapter's own
`build_argv`/`test_argv` against the repo's PRE-migration worktree, OBSERVE the real exit code,
and write `repos.baseline_ok`/`repos.baseline_test_count` from that observation — never from
`NativeBaseline.test_unit_count`, which `src/fleet/models/build.py`'s own `Field(description=...)`
documents as a STATIC value re-deriving the MIGRATED side's own test-target count (Leg A's
disclosed caveat). Wiring that value through unchanged would make
`workers/buildverify.py::test_count_regressed` compare a value against itself and silently defeat
the exact regression ADR-0135 ruling 1 exists to catch — this worker exists to produce a
genuinely independent number instead.

**Updated 2026-09-10 (round VI task 116):** Leg C (round VI task 111) has landed since this
paragraph was written — a failing native build/test no longer goes unescalated overall. What
remains true, unchanged, of THIS worker specifically: a repo whose native build or test genuinely
fails still completes with `baseline_ok=False` recorded as DATA, never as a worker failure — this
`run()` NEVER returns `status="failed"` for a native-command outcome, however it exits, and this
worker itself never writes a `BaselineRed` finding or a `RepoStatus.SKIPPED`. What is now false is
that no escalation happens at all: `cli.py::_gate_baseline_red` (Leg C's gate, run at scan time,
downstream of this worker) reads the `baseline_ok=0` this worker already wrote (via
`_ScanEvidence.record`/`evidence.baseline_red`) and, in one transaction, flips the repo's `phases`
row to `status = 'SKIPPED'` and inserts a `BaselineRed` finding beside it — so a genuinely failing
native build now DOES produce a `BaselineRed` finding, `SKIPPED` status, and `baseline_ok=0`, just
not from inside this worker's own `run()`. The only statuses THIS worker can return are `"ok"`
(always, for a native outcome including a failed one) and `"cancelled"` (a genuine
scheduling-level cancel/deadline, the same class of event every other SCAN_UNIT worker reports
the same way). This is deliberate: §3.1 step 1 is the fleet's OWN gate, and observing it must
never be indistinguishable from the gate itself misbehaving — the escalation is Leg C's job, not
this worker's.

**Container posture (ADR-0135 ruling 3): a SEPARATE, NETWORKED container from Bazel's own
`--network=none` sandbox — never inside it.** `_argv` reuses `spec_for_attempt`/`docker_run_argv`
exactly as `workers/buildverify.py` does for Bazel's own sandbox, but addresses a DIFFERENT,
operator-configured image (`preflight.baseline_build.container_image`) with a DIFFERENT default
network (`"bridge"`, not `"none"`) — nothing here ever reaches Bazel's own sandbox image or its
`--network=none` flag. Leg D (round VI task 108, dispatched the same wave as this task) owns
building that image; it had not landed when this task was written, so the shipped default is an
UNBUILT placeholder tag (`settings.py`'s own `BaselineBuild.container_image` docstring has the
measured detail: `docker run` on an unbuilt local tag fails in ~0.3s with exit 125, "pull access
denied" — fast, loud, and never a hang). Until an operator points `container_image` at a real Leg
D image, every repo this worker measures records `baseline_ok=False` for that fast, disclosed
reason — never a crash (see `_invoke`'s `OSError` guard for the same disclosure one layer down,
covering a host with no `docker` on `PATH` at all). `container_image: null` is the documented
escape hatch for a controlled fixture (this worker's own end-to-end test uses it, deliberately,
against a repo this test controls completely): build_argv/test_argv then run directly on the
harness host, inheriting its own environment — never the shipped default, and never silent (the
class docstring above states why).

**Where the ecosystem comes from, and why it can never disagree with Phase 3's.** `native_baseline
(unit: BuildUnit)` needs a `BuildUnit`, which does not exist yet in Phase 1 — `dest`/`srcs`/
`test_srcs` are `layout()`/buildgen's own Phase-3 output. This worker constructs a MINIMAL
placeholder (`dest=""`, no `srcs`/`test_srcs`) sufficient only to satisfy the method's signature;
`build_argv`/`test_argv` for both adapters implemented so far are unit-independent literal argv
lists (confirmed against `py.py`/`js.py`), so the placeholder never reaches an adapter's output.
The one field the placeholder's absence WOULD affect — `test_targets(unit)`'s own count, folded
into `NativeBaseline.test_unit_count` — is exactly the value this worker never uses (see above).
`payload.ecosystem` itself is resolved by the caller (`cli.py::_primary_ecosystem`) the SAME way
`repos.primary_coord_key` is (`min(coord.key for coord in published)`, read for its ecosystem
instead of its key) — so this worker's adapter choice can never disagree with what Phase 3 later
selects as `_RepoFacts.ecosystem` for the identical repo.

**The observed-count classifier (`_classify`) and its one named limit.** ADR-0135 ruling 1
requires `baseline_test_count` at the SAME per-`BuildUnit` granularity `test_targets()` already
produces on the migrated side — 0 or 1, never a raw test-case count. Rather than re-deriving that
granularity from a `BuildUnit` this phase cannot build, this worker derives it directly from the
TEST STEP'S OWN exit code: exit 0 is "at least one native test ran and passed" (count 1); pytest's
own documented exit 5 ("no tests were collected") is "this repo genuinely has no native tests"
(not a failure — `workers/buildverify.py`'s own `no_test_targets`/`NO_TESTS_FOUND` draws the
identical line one phase later, for the identical reason) — count 0, `baseline_ok=True`; any other
exit is a real failure, `baseline_ok=False`, count 0. **Known limit, disclosed rather than
patched:** there is no cross-ecosystem equivalent of pytest's exit 5 wired in here — an `npm test`
script that is simply ABSENT (`"Missing script: \"test\""`) exits non-zero and is classified as a
failure (`baseline_ok=False`) rather than as "no native tests", which is the wrong verdict for that
one case. Leg C/E (this task's own sibling/future legs) own refining this further; it is out of
scope for a green-path-only worker and does not affect PyPI, which every current fixture repo
this worker's own tests exercise uses.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import ClassVar, Final

from pydantic import Field

from fleet import ecosystems
from fleet.models.build import BuildUnit, NativeBaseline
from fleet.models.enums import Ecosystem, FailureClass, Phase
from fleet.models.repo import RepoId
from fleet.orchestrator.registry import register_worker
from fleet.sandbox.container import docker_run_argv, spec_for_attempt
from fleet.sandbox.worktree import sandbox_name
from fleet.util.proc import CommandRunner, ProcResult
from fleet.workers.base import (
    BaseWorker,
    WorkerContext,
    WorkerError,
    WorkerInput,
    WorkerOutput,
    WorkerResult,
    loop_now,
)
from fleet.workers.buildverify import LoggedRunner

__all__ = [
    "UNIT",
    "BaselineInput",
    "BaselineOutput",
    "BaselineWorker",
]

UNIT: Final = "baseline"

_PYTEST_NO_TESTS_COLLECTED: Final = 5
"""pytest's own documented exit code for "no tests were collected" (`_pytest.config.ExitCode.
NO_TESTS_COLLECTED`) -- distinguished from a real test failure (1), an interrupted run (2), or a
usage/internal error (3/4). See this module's own docstring ("The observed-count classifier") for
why this is the one cross-ecosystem special case wired in and what is deliberately NOT."""


class BaselineInput(WorkerInput):
    """One repo's native-build measurement. Resolved from `preflight.baseline_build` (§9) and
    from Phase 1's own `interrogate` step's manifests -- never from a `BuildUnit` Phase 3 has not
    built yet (see this module's docstring)."""

    repo_id: RepoId
    ecosystem: Ecosystem | None = Field(
        default=None,
        description="The repo's PRIMARY published ecosystem, resolved by the caller the SAME way "
        "`repos.primary_coord_key` is (`cli._primary_ecosystem`). `None` when the repo publishes "
        "no coordinate yet (e.g. an EmptyRepo, or a manifest-less repo) -- the worker skips.",
    )
    worktree_path: str | None = None
    enabled: bool = True
    timeout_s: int = Field(default=1800, gt=0)
    image: str | None = None
    container_memory: str = "2g"
    container_cpus: str = "2.0"
    container_network: str = "bridge"
    min_free_bytes: int = 0
    log_dir: str = Field(min_length=1)
    remaining_units: tuple[str, ...] | None = Field(
        default=None, description="The checkpoint's owed units; None = no checkpoint"
    )


class BaselineOutput(WorkerOutput):
    """`repos.baseline_ok`/`repos.baseline_test_count`'s OBSERVED source. `None` means "nothing
    was measured" (disabled, or no adapter support for this ecosystem) -- `ScanOutput.baseline`
    stays `None` for that case and `_scan_rows` writes nothing, leaving `repos.baseline_ok` NULL
    exactly as it is today (ADR-0135's own exemption-set ruling depends on that NULL surviving
    unchanged for a repo this worker never measured)."""

    repo_id: RepoId
    ecosystem: Ecosystem | None = None
    baseline_ok: bool = False
    baseline_test_count: int = Field(default=0, ge=0)
    build_exit_code: int | None = None
    test_exit_code: int | None = None


@register_worker
class BaselineWorker(BaseWorker[BaselineInput, BaselineOutput]):
    """§3.1 step 1's native (pre-migration) build+test. See this module's docstring for the full
    design: container posture, ecosystem resolution, and the observed-count classifier."""

    __slots__ = ("_runner",)

    name: ClassVar[str] = UNIT
    phase: ClassVar[Phase] = Phase.SCAN
    input_model: ClassVar[type[WorkerInput]] = BaselineInput
    output_model: ClassVar[type[WorkerOutput]] = BaselineOutput

    def __init__(self, *, runner: CommandRunner | None = None) -> None:
        """`runner=None` builds a `LoggedRunner` per invocation from `payload.log_dir` (the same
        collaborator `buildverify`/`rdepverify` share), so the full native build/test stream
        reaches disk. A test injects its own recorder and never touches a real `docker`/`pip`/
        `npm`."""
        self._runner = runner

    async def preconditions_hold(self, ctx: WorkerContext, payload: BaselineInput) -> bool:
        """Chained inside `ScanPipelineWorker` (`cli.py`), whose own re-entry check already
        covers the shared worktree; this override exists only so every `BaseWorker` subclass
        makes an explicit re-entry claim (`implements_preconditions`), matching every sibling
        SCAN_UNIT worker's own pattern (`classify.py`, `symbolindex.py`)."""
        if payload.remaining_units is None or UNIT in payload.remaining_units:
            return False
        return await asyncio.to_thread(Path(payload.worktree_path or ctx.workdir).is_dir)

    async def run(self, ctx: WorkerContext, payload: BaselineInput) -> WorkerResult[BaselineOutput]:
        if payload.remaining_units is not None and UNIT not in payload.remaining_units:
            # Already landed under an earlier attempt: re-running would measure the same repo
            # twice for no reason (§3.1 step 1 is a one-shot pre-migration probe, not a resumable
            # multi-unit loop the way `symbolindex`'s batches are).
            return WorkerResult[BaselineOutput](status="ok", completed_units=[UNIT])
        if not payload.enabled or payload.ecosystem is None:
            # Disabled, or nothing published yet: `output=None` means `_scan_rows` writes
            # nothing, so `repos.baseline_ok` stays NULL -- exactly what "genuinely disabled"
            # must mean (ADR-0135's own exemption-set ruling depends on this).
            return WorkerResult[BaselineOutput](status="ok", completed_units=[UNIT])
        now = loop_now()
        if ctx.cancelled() or ctx.expired(now):
            # A genuine `ctx.cancelled()` is an operator decision and stays `cancelled` (not an
            # attempt); `ctx.expired()` is a real timeout and must be a chargeable `timeout`
            # result (§11) -- conflating the two would let a repo that times out on every attempt
            # never escalate to REQUIRES_HUMAN_INTERVENTION.
            timed_out = ctx.expired(now)
            return WorkerResult[BaselineOutput](
                status="timeout" if timed_out else "cancelled",
                error=WorkerError(
                    failure_class=(
                        FailureClass.TIMEOUT if timed_out else FailureClass.TRANSIENT_INFRA
                    ),
                    retryable=True,
                    stderr_tail="cancelled before the native baseline was dispatched",
                ),
            )

        adapter = ecosystems.for_ecosystem(payload.ecosystem)
        placeholder_unit = BuildUnit(unit_id=payload.repo_id, ecosystem=payload.ecosystem, dest="")
        native = adapter.native_baseline(placeholder_unit)
        if native is None:
            # This ecosystem's adapter has no native-baseline capability yet (MAVEN/GRADLE/GO/
            # CARGO/UNKNOWN, per Leg A) -- skip cleanly, never an error (the brief's own words).
            return WorkerResult[BaselineOutput](status="ok", completed_units=[UNIT])

        worktree = Path(payload.worktree_path or ctx.workdir)
        runner = self._runner_for(ctx, payload)
        ok, count, build_exit, test_exit = await self._measure(
            ctx, payload, native, worktree, runner
        )
        ctx.log.info(
            "baseline_measured",
            ecosystem=payload.ecosystem.value,
            baseline_ok=ok,
            baseline_test_count=count,
            build_exit_code=build_exit,
            test_exit_code=test_exit,
        )
        return WorkerResult[BaselineOutput](
            status="ok",
            output=BaselineOutput(
                repo_id=payload.repo_id,
                ecosystem=payload.ecosystem,
                baseline_ok=ok,
                baseline_test_count=count,
                build_exit_code=build_exit,
                test_exit_code=test_exit,
            ),
            completed_units=[UNIT],
        )

    # ------------------------------------------------------------------ internals

    async def _measure(
        self,
        ctx: WorkerContext,
        payload: BaselineInput,
        native: NativeBaseline,
        worktree: Path,
        runner: CommandRunner,
    ) -> tuple[bool, int, int | None, int | None]:
        """`(baseline_ok, baseline_test_count, build_exit_code, test_exit_code)` -- the whole of
        this worker's green-path verdict, from the TEST step's own exit code (see this module's
        docstring, "The observed-count classifier"). A build failure short-circuits: no test can
        have run against code that never installed."""
        build_exit: int | None = None
        if native.build_argv:
            build_result = await self._invoke(
                ctx, payload, native.build_argv, worktree, runner, step="build"
            )
            if build_result is None:
                return False, 0, None, None
            build_exit = build_result.exit_code
            if not build_result.ok:
                return False, 0, build_exit, None
        test_result = await self._invoke(
            ctx, payload, native.test_argv, worktree, runner, step="test"
        )
        if test_result is None:
            return False, 0, build_exit, None
        ok, count = _classify(test_result)
        return ok, count, build_exit, test_result.exit_code

    async def _invoke(
        self,
        ctx: WorkerContext,
        payload: BaselineInput,
        command: Sequence[str],
        worktree: Path,
        runner: CommandRunner,
        *,
        step: str,
    ) -> ProcResult | None:
        """One native-build/test step's `ProcResult`, or `None` if the invocation itself never
        produced one (a host with no `docker` on `PATH`, or any other `OSError` `asyncio.
        create_subprocess_exec` can raise). `None` is NOT re-raised: this worker's whole
        green-path contract (module docstring) is "observe and record", never "escalate the
        repo" -- an infrastructure fault the native build does not control must classify exactly
        like any other observed failure, not crash the chained `ScanPipelineWorker` that called
        it (which has no try/except of its own around this sub-step, unlike a standalone
        worker's `execute()`/`_run_one`)."""
        argv = self._argv(ctx, payload, command, worktree, step=step)
        try:
            return await runner(
                argv, cwd=worktree, deadline=ctx.deadline, timeout_s=payload.timeout_s
            )
        except OSError as exc:
            ctx.log.warning("baseline_invocation_failed", step=step, reason=str(exc))
            return None

    def _argv(
        self,
        ctx: WorkerContext,
        payload: BaselineInput,
        command: Sequence[str],
        worktree: Path,
        *,
        step: str,
    ) -> tuple[str, ...]:
        """The command as executed: bare on the host, or ADR-0135 ruling 3's SEPARATE, networked
        container -- never Bazel's own `--network=none` sandbox image (see module docstring)."""
        if payload.image is None:
            return tuple(command)
        spec = spec_for_attempt(
            run_id=ctx.run_id,
            repo=ctx.repo_id,
            attempt=ctx.attempt,
            image=payload.image,
            command=command,
            worktree=worktree,
            container_workdir="/work",
            memory=payload.container_memory,
            cpus=payload.container_cpus,
            network=payload.container_network,
            min_free_bytes=payload.min_free_bytes,
            name=f"{sandbox_name(ctx.run_id, ctx.repo_id, ctx.attempt)}-baseline-{step}",
        )
        return tuple(docker_run_argv(spec))

    def _runner_for(self, ctx: WorkerContext, payload: BaselineInput) -> CommandRunner:
        if self._runner is not None:
            return self._runner
        return LoggedRunner(
            Path(payload.log_dir) / str(ctx.run_id), stem=f"{self.name}-{ctx.attempt}"
        )


def _classify(result: ProcResult) -> tuple[bool, int]:
    """`(baseline_ok, baseline_test_count)` from the TEST step's own exit code -- never from
    `NativeBaseline.test_unit_count` (see this module's docstring). `started`/`timed_out` are
    checked first and win regardless of `exit_code`: a clock failure says nothing about the
    native suite (the identical `clock_failure` ordering `workers/base.py` documents at length,
    for the identical reason)."""
    if not result.started or result.timed_out:
        return False, 0
    if result.exit_code == 0:
        return True, 1
    if result.exit_code == _PYTEST_NO_TESTS_COLLECTED:
        return True, 0
    return False, 0
