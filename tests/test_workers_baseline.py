"""§12.11/D116 Leg B (round VI task 107, ADR-0135): `workers/baseline.py`, at the worker level.

Every test here injects a `CommandRunner` and never touches a real `docker`/`pip`/`npm` — the
same seam `buildverify`/`rdepverify`'s own tests use (§7's "command construction is verifiable
with no daemon and no network"). The real end-to-end proof — a real `fleet scan` writing
`repos.baseline_ok`/`repos.baseline_test_count` for a real repo, through a real (but offline,
zero-dependency) `npm install`/`npm test` — lives in `tests/test_workers_scan.py`'s sibling e2e
file, `tests/test_scan_e2e.py` (see the test added there this task: `test_baseline_measures_the_
npm_fixture_repo_and_the_scan_writes_it_back`).

Covers: the worker exists and claims `phase=SCAN`; `enabled=False` and `ecosystem=None` both
skip cleanly with `output=None` (so `_scan_rows` writes nothing — `repos.baseline_ok` stays
NULL); an ecosystem with no `native_baseline()` support (e.g. `Ecosystem.GO`) skips cleanly too;
a green build+test (`_classify`'s exit-0 branch); pytest's own "no tests collected" exit (5,
`_classify`'s one named cross-ecosystem special case); a build failure short-circuits before the
test step ever runs; a test failure is recorded as `baseline_ok=False` WITHOUT the worker ever
returning `status="failed"` (the green-path contract this module's own docstring states); an
`OSError` from the runner (a host with no `docker` on `PATH`) is caught and classified the same
way, never crashing the chained `ScanPipelineWorker`; the containerized `_argv` shape (image,
network, memory, cpus) when `payload.image` is set; and a genuine cancel/deadline still reports
`status="cancelled"`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from fleet import ecosystems
from fleet.models.enums import Ecosystem, FailureClass, Phase
from fleet.util.proc import ProcResult
from fleet.workers.base import WorkerContext, implements_preconditions
from fleet.workers.baseline import BaselineInput, BaselineOutput, BaselineWorker, _classify
from tests.test_workers_scan import RecordingLog, make_ctx

ecosystems.discover()  # idempotent (base.py::discover) -- `for_ecosystem` needs the registry
# populated, and this module (unlike test_workers_scan.py's four workers) is the first SCAN_UNIT
# test file that reads it.


@dataclass
class ScriptedRunner:
    """A `CommandRunner` returning one scripted `ProcResult` per call, in order. Never spawns a
    real process — the whole point (module docstring)."""

    results: list[ProcResult]
    calls: list[tuple[tuple[str, ...], Path | None]] = field(default_factory=list)

    async def __call__(
        self,
        argv: Any,
        *,
        cwd: Path | None = None,
        env: Any = None,
        deadline: float | None = None,
        timeout_s: float | None = None,
    ) -> ProcResult:
        self.calls.append((tuple(argv), cwd))
        return self.results[len(self.calls) - 1]


class RaisingRunner:
    """A `CommandRunner` that raises `OSError`, standing in for a host with no `docker` on
    `PATH` — the shape `_invoke`'s own `except OSError` guard exists for."""

    def __init__(self, exc: OSError) -> None:
        self._exc = exc
        self.calls = 0

    async def __call__(self, argv: Any, **kwargs: Any) -> ProcResult:
        self.calls += 1
        raise self._exc


def _result(exit_code: int, *, started: bool = True, timed_out: bool = False) -> ProcResult:
    return ProcResult(
        argv=("x",),
        exit_code=exit_code,
        stdout_tail="",
        stderr_tail="",
        duration_ms=5,
        timed_out=timed_out,
        started=started,
    )


def _payload(**overrides: Any) -> BaselineInput:
    base: dict[str, Any] = {
        "repo_id": "acme-billing",
        "ecosystem": Ecosystem.PYPI,
        # Never touched: `runner` is always injected in these tests, so `_runner_for` never
        # builds a real `LoggedRunner` from this path.
        "log_dir": "artifacts/logs-not-touched-by-these-tests",
    }
    base.update(overrides)
    return BaselineInput(**base)


def _run(worker: BaselineWorker, ctx: WorkerContext, payload: BaselineInput) -> Any:
    return asyncio.run(worker.run(ctx, payload))


# =======================================================================================
# (1) shape
# =======================================================================================


def test_baseline_worker_is_instantiable_and_claims_phase_scan() -> None:
    """Instantiation IS the proof `preconditions_hold` is overridden (`implements_preconditions`
    — the same convention `test_workers_scan.py` uses for the other four SCAN_UNIT workers)."""
    worker = BaselineWorker()
    assert worker.phase == Phase.SCAN
    assert worker.name == "baseline"
    assert implements_preconditions(BaselineWorker)


# =======================================================================================
# (2) skip-cleanly paths -- never an error, output=None so `_scan_rows` writes nothing
# =======================================================================================


def test_disabled_skips_cleanly_and_never_invokes_the_runner(tmp_path: Path) -> None:
    runner = ScriptedRunner(results=[])
    worker = BaselineWorker(runner=runner)
    ctx = make_ctx(tmp_path)
    result = _run(worker, ctx, _payload(enabled=False))
    assert result.status == "ok"
    assert result.output is None
    assert runner.calls == []


def test_no_ecosystem_skips_cleanly(tmp_path: Path) -> None:
    """A repo that has published no coordinate yet (`cli._primary_ecosystem` returns `None`) —
    e.g. before `interrogate` has landed, or a manifest-less repo."""
    runner = ScriptedRunner(results=[])
    worker = BaselineWorker(runner=runner)
    ctx = make_ctx(tmp_path)
    result = _run(worker, ctx, _payload(ecosystem=None))
    assert result.status == "ok"
    assert result.output is None
    assert runner.calls == []


def test_an_ecosystem_with_no_native_baseline_support_skips_cleanly(tmp_path: Path) -> None:
    """GO's adapter inherits the Leg A default (`None`) — never wired, per Leg A's own disclosed
    residual. Confirms this worker treats that as a clean skip, not an error."""
    runner = ScriptedRunner(results=[])
    worker = BaselineWorker(runner=runner)
    ctx = make_ctx(tmp_path)
    result = _run(worker, ctx, _payload(ecosystem=Ecosystem.GO))
    assert result.status == "ok"
    assert result.output is None
    assert runner.calls == []


# =======================================================================================
# (3) the green build+test path, and the observed count it produces
# =======================================================================================


def test_a_healthy_native_build_and_test_records_baseline_ok_true_and_count_one(
    tmp_path: Path,
) -> None:
    runner = ScriptedRunner(results=[_result(0), _result(0)])  # build, then test
    worker = BaselineWorker(runner=runner)
    ctx = make_ctx(tmp_path)
    result = _run(worker, ctx, _payload())
    assert result.status == "ok"
    output = result.output
    assert isinstance(output, BaselineOutput)
    assert output.baseline_ok is True
    assert output.baseline_test_count == 1
    assert output.build_exit_code == 0
    assert output.test_exit_code == 0
    # PyPI: build_argv=["python3","-m","pip","install","-e","."], test_argv=["python3","-m",
    # "pytest"] (Leg A, `ecosystems/py.py`) -- both invoked, in order, bare (no `image` set).
    assert len(runner.calls) == 2
    assert runner.calls[0][0][:2] == ("python3", "-m")
    assert "pytest" in runner.calls[1][0]


def test_pytest_no_tests_collected_is_baseline_ok_true_count_zero(tmp_path: Path) -> None:
    """Exit 5 -- pytest's own documented "no tests were collected" -- is NOT a failure (module
    docstring, "The observed-count classifier"). This is the one case that would be misread as
    `baseline_ok=False` by a naive `exit_code == 0` check."""
    runner = ScriptedRunner(results=[_result(0), _result(5)])
    worker = BaselineWorker(runner=runner)
    ctx = make_ctx(tmp_path)
    result = _run(worker, ctx, _payload())
    output = result.output
    assert output is not None
    assert output.baseline_ok is True
    assert output.baseline_test_count == 0
    assert output.test_exit_code == 5


def test_a_build_failure_short_circuits_before_the_test_step_ever_runs(tmp_path: Path) -> None:
    runner = ScriptedRunner(results=[_result(1)])  # build fails; no second scripted result
    worker = BaselineWorker(runner=runner)
    ctx = make_ctx(tmp_path)
    result = _run(worker, ctx, _payload())
    assert result.status == "ok"  # never "failed" -- green-path contract
    output = result.output
    assert output is not None
    assert output.baseline_ok is False
    assert output.baseline_test_count == 0
    assert output.build_exit_code == 1
    assert output.test_exit_code is None
    assert len(runner.calls) == 1  # the test step was never invoked


def test_a_real_test_failure_is_recorded_as_data_never_as_a_worker_failure(
    tmp_path: Path,
) -> None:
    """The brief's own words: "should still complete without crashing your worker ... write
    baseline_ok=0". `status` stays "ok" -- this is what makes it distinguishable from a repo
    whose whole SCAN phase failed."""
    runner = ScriptedRunner(results=[_result(0), _result(1)])
    worker = BaselineWorker(runner=runner)
    ctx = make_ctx(tmp_path)
    result = _run(worker, ctx, _payload())
    assert result.status == "ok"
    assert result.error is None
    output = result.output
    assert output is not None
    assert output.baseline_ok is False
    assert output.baseline_test_count == 0
    assert output.test_exit_code == 1


# =======================================================================================
# (4) infrastructure faults never crash the chained pipeline
# =======================================================================================


def test_a_missing_docker_binary_is_caught_and_classified_not_raised(tmp_path: Path) -> None:
    """`OSError` (`FileNotFoundError` in the real case) from the runner must never escape `run()`
    -- `ScanPipelineWorker._baseline` has no try/except of its own around this sub-step, unlike a
    standalone worker's `execute()`/`_run_one` (module docstring, `_invoke`)."""
    runner = RaisingRunner(FileNotFoundError("docker"))
    worker = BaselineWorker(runner=runner)
    ctx = make_ctx(tmp_path, log=RecordingLog())
    result = _run(worker, ctx, _payload())
    assert result.status == "ok"
    output = result.output
    assert output is not None
    assert output.baseline_ok is False
    assert output.baseline_test_count == 0
    assert output.build_exit_code is None
    assert runner.calls == 1  # never reached the test step either


def test_cancelled_before_dispatch_reports_status_cancelled(tmp_path: Path) -> None:
    runner = ScriptedRunner(results=[])
    worker = BaselineWorker(runner=runner)
    ctx = make_ctx(tmp_path)
    ctx.cancel.set()
    result = _run(worker, ctx, _payload())
    assert result.status == "cancelled"
    assert result.error is not None
    assert result.error.failure_class == FailureClass.TIMEOUT
    assert runner.calls == []


# =======================================================================================
# (5) the containerized argv shape (ADR-0135 ruling 3: a SEPARATE, networked container)
# =======================================================================================


def test_containerized_argv_uses_the_configured_image_and_network(tmp_path: Path) -> None:
    runner = ScriptedRunner(results=[_result(0), _result(0)])
    worker = BaselineWorker(runner=runner)
    ctx = make_ctx(tmp_path)
    result = _run(
        worker,
        ctx,
        _payload(
            image="fleet-baseline:test-tag",
            container_network="bridge",
            container_memory="1g",
            container_cpus="1.0",
        ),
    )
    assert result.status == "ok"
    assert len(runner.calls) == 2
    build_argv = runner.calls[0][0]
    assert build_argv[0] == "docker"
    assert build_argv[1] == "run"
    assert "--network=bridge" in build_argv
    assert "--memory=1g" in build_argv
    assert "--cpus=1.0" in build_argv
    assert "fleet-baseline:test-tag" in build_argv
    # ADR-0135 ruling 3: never Bazel's own sandbox flag/network.
    assert "--network=none" not in build_argv


def test_bare_host_argv_is_the_adapters_own_command_unwrapped(tmp_path: Path) -> None:
    """`image=None` (the explicit, narrower escape hatch — never the shipped default, see
    `settings.py::BaselineBuild.container_image`) runs `build_argv`/`test_argv` verbatim, with
    no `docker` wrapper at all."""
    runner = ScriptedRunner(results=[_result(0), _result(0)])
    worker = BaselineWorker(runner=runner)
    ctx = make_ctx(tmp_path)
    result = _run(worker, ctx, _payload(image=None))
    assert result.status == "ok"
    assert runner.calls[0][0][0] == "python3"
    assert runner.calls[1][0][0] == "python3"


# =======================================================================================
# (6) `_classify` in isolation
# =======================================================================================


@pytest.mark.parametrize(
    ("exit_code", "started", "timed_out", "expected_ok", "expected_count"),
    [
        (0, True, False, True, 1),
        (5, True, False, True, 0),
        (1, True, False, False, 0),
        (2, True, False, False, 0),
        (0, False, True, False, 0),  # never started (clock failure) beats exit_code
        (0, True, True, False, 0),  # killed at deadline beats exit_code too
    ],
)
def test_classify_reads_the_test_steps_own_exit_code(
    exit_code: int, started: bool, timed_out: bool, expected_ok: bool, expected_count: int
) -> None:
    result = _result(exit_code, started=started, timed_out=timed_out)
    ok, count = _classify(result)
    assert ok is expected_ok
    assert count is expected_count
