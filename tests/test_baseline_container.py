"""`docker/fleet-baseline.Dockerfile`: the native-baseline sandbox image (ADR-0135 Leg D, round VI
task 108) — a SEPARATE image from `docker/fleet-build.Dockerfile`'s Bazel `--network=none`
sandbox, built WITH network access so `EcosystemAdapter.native_baseline()`'s `build_argv`/
`test_argv` (`src/fleet/models/build.py::NativeBaseline`) can resolve an arbitrary third-party
repo's OWN, pre-migration PyPI/npm dependencies exactly as ADR-0135 ruling 3 requires.

Same discipline as `test_sandbox.py`'s two live-daemon tests: gate on the image being present
LOCALLY (never pull — a registry round-trip would make a green run depend on someone else's
uptime) and skip LOUDLY, naming why, rather than let an absent daemon look like a pass.

**Corrected 2026-09-10 (round VI task 108 fix round, review Critical finding 1).** The two
container-driving tests below used to build their own `spec_for_attempt` call with `command=
["sh", "-c", "<build> && <test>"]` — ONE joined container invocation — on the stated (and false)
premise that this is how Leg B's worker would call it. It is not: `src/fleet/workers/baseline.py`
(Leg B, landed round VI task 107, merged to `main` at `121665e` — AFTER this task's original
`main`@`fa6e1d7` dispatch) calls `spec_for_attempt` TWICE, once per step, each in its OWN
separately-named (`-baseline-build`/`-baseline-test`), separately-`--rm`'d container. The two
tests below now drive the REAL, UNMODIFIED `fleet.workers.baseline.BaselineWorker` — not a
hand-rolled re-implementation of its shape — so there is no second chance for this suite's own
premise about Leg B to drift from Leg B's actual code again. This is also what caught the real
image defect the joined-invocation version could not see: `PIP_USER=1` + a plain `HOME=` landed
Python installs in the FIRST container's own ephemeral layer, invisible to the SECOND container's
`pytest` — fixed in `docker/fleet-baseline.Dockerfile` via `PYTHONUSERBASE=/work/...` (see that
file's own dated comment).

This is still Leg D's own suite — proving the IMAGE genuinely has a working Python+pytest and
Node+npm toolchain with network access, now proven specifically under Leg B's real two-container
invocation shape. It does not modify `src/fleet/workers/baseline.py` itself (Leg B, merged and
reviewed — this task's job is to make the image work with what Leg B actually does, not to ask
Leg B to change) and does not touch the shipped `tests/test_scan_e2e.py::FIXTURE_REPOS` fixtures,
which have no native test files of their own yet (a disclosed Leg E gap, not this task's to
close) — the fixture content used below is a LOCAL, throwaway repo shaped identically to the real
`acme-lib-py`/`acme-lib-ts` fixtures (same dependency declarations: `requests>=2.31`,
`left-pad@^1.3.0`) plus one real test file, so this suite can prove the toolchain rather than
merely assert it exists.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from uuid import UUID

import pytest

from fleet import ecosystems
from fleet.models.enums import Ecosystem
from fleet.sandbox.container import ContainerSandbox, ContainerSpec, docker_run_argv
from fleet.settings import PreflightSection
from fleet.util.proc import run
from fleet.workers.baseline import BaselineInput, BaselineWorker
from tests.test_workers_scan import make_ctx

ecosystems.discover()  # idempotent (base.py::discover) -- for_ecosystem() needs it populated

RUN_ID = UUID("00000000-0000-4000-8000-0000000010c8")  # task 108
BASELINE_IMAGE = PreflightSection().baseline_build.container_image
"""Read from the setting, never re-typed — same reasoning `test_sandbox.py`'s `FLEET_BUILD_IMAGE`
gives for `VerifySection.container_image`: this suite is about the image the fleet will actually
run, so retagging `settings.py` must move the test with it."""

BASELINE_NETWORK = PreflightSection().baseline_build.container_network
BASELINE_MEMORY = PreflightSection().baseline_build.container_memory
BASELINE_CPUS = PreflightSection().baseline_build.container_cpus


def _baseline_image_usable() -> tuple[bool, str]:
    """`test_sandbox.py::_fleet_build_image_usable`'s shape, restated for this separate image
    rather than imported: the two gates share no code today, and importing one test module from
    another for a three-line helper would couple their skip messages for no benefit."""
    if shutil.which("docker") is None:
        return False, "docker CLI not on PATH"

    async def probe() -> tuple[bool, str]:
        if not await ContainerSandbox().available():
            return False, "docker daemon not reachable"
        images = await run(["docker", "images", "--quiet", BASELINE_IMAGE], timeout_s=30)
        if not images.ok or not images.stdout_tail.strip():
            return False, (
                f"image {BASELINE_IMAGE} not built on this host (no network pull here); "
                f"build it: docker build -f docker/fleet-baseline.Dockerfile "
                f"-t {BASELINE_IMAGE} docker/"
            )
        return True, ""

    return asyncio.run(probe())


_IMAGE_OK, _IMAGE_WHY = _baseline_image_usable()


def _write_py_fixture(root: Path) -> None:
    """Shaped identically to the real `acme-lib-py`/`acme-app-py` fixtures
    (`tests/test_scan_e2e.py::FIXTURE_REPOS`): a `pyproject.toml` with no `[build-system]` table
    (matching the real fixtures verbatim) and a real third-party dependency (`requests`), so
    `pip install -e .` genuinely resolves something over the network rather than a no-op. The one
    difference from the real fixtures — an actual test file — is the disclosed Leg E gap this
    module's own docstring names.
    """
    (root / "pyproject.toml").write_text(
        '[project]\nname = "acme-lib-py"\nversion = "2.0.1"\ndependencies = ["requests>=2.31"]\n'
    )
    pkg = root / "acme_lib_py"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        'def normalize(name: str) -> str:\n    """Lower-case a coordinate name."""\n'
        "    return name.lower()\n"
    )
    (root / "test_acme_lib_py.py").write_text(
        "from acme_lib_py import normalize\n"
        "import requests  # proves the network-resolved dependency actually imports\n\n\n"
        "def test_normalize():\n"
        '    assert normalize("ACME") == "acme"\n'
        '    assert requests.__name__ == "requests"\n'
    )


def _write_js_fixture(root: Path) -> None:
    """Shaped identically to the real `acme-lib-ts` fixture's `package.json` dependency
    (`left-pad@^1.3.0`), plus a real `test` script — `native_baseline()`'s own `test_argv` is
    plain `npm test`, and the real fixture's `package.json` has no `scripts.test` yet (the same
    disclosed Leg E gap)."""
    (root / "package.json").write_text(
        json.dumps(
            {
                "name": "@acme/lib",
                "version": "1.4.0",
                "main": "index.js",
                "scripts": {"test": "node test.js"},
                "dependencies": {"left-pad": "^1.3.0"},
            },
            indent=2,
        )
    )
    (root / "index.js").write_text(
        "function formatMoney(cents) {\n"
        '  return "$" + (cents / 100).toFixed(2);\n'
        "}\n"
        "module.exports = { formatMoney };\n"
    )
    (root / "test.js").write_text(
        'const assert = require("assert");\n'
        'const leftPad = require("left-pad"); '
        "// proves the network-resolved dependency actually imports\n"
        'const { formatMoney } = require("./index.js");\n\n'
        'assert.strictEqual(formatMoney(150), "$1.50");\n'
        'assert.strictEqual(leftPad("1", 3, "0"), "001");\n'
    )


def _real_baseline_payload(*, ecosystem: Ecosystem, worktree: Path) -> BaselineInput:
    """Leg B's own real config surface (`preflight.baseline_build`), read from the SAME setting
    the shipped default now points at (`BASELINE_IMAGE`, etc.) — never a hand-typed re-statement
    of Leg B's field values, so a drift in `settings.py`'s defaults shows up here too."""
    return BaselineInput(
        repo_id="acme-baseline-live",
        ecosystem=ecosystem,
        worktree_path=str(worktree),
        image=BASELINE_IMAGE,
        container_memory=BASELINE_MEMORY,
        container_cpus=BASELINE_CPUS,
        container_network=BASELINE_NETWORK,
        log_dir=str(worktree / "logs-not-touched"),  # runner=run is injected below; never read
    )


@pytest.mark.integration
@pytest.mark.skipif(not _IMAGE_OK, reason=f"fleet baseline image unusable: {_IMAGE_WHY}")
async def test_the_real_baseline_worker_measures_a_pypi_repo_through_two_real_containers(
    tmp_path: Path,
) -> None:
    """Drives the REAL, UNMODIFIED `fleet.workers.baseline.BaselineWorker` (Leg B, merged and
    reviewed) — not a hand-rolled re-implementation of its invocation shape — against a real
    docker daemon and this task's real built image, with a real PyPI dependency (`requests`).

    `BaselineWorker._argv`/`_measure` build TWO separate `spec_for_attempt` calls internally, one
    per step (`-baseline-build`, `-baseline-test`), each its own `--rm`'d container mounting the
    SAME `worktree` at `/work` — this test asserts the OUTPUT (`baseline_ok`, `baseline_test_
    count`, both exit codes), not the argv shape, precisely because the argv shape is Leg B's own
    code and this task's job is to prove the IMAGE works with it, not to re-assert Leg B's own
    already-reviewed internals.
    """
    _write_py_fixture(tmp_path)
    ctx = make_ctx(tmp_path, seconds_left=180.0)
    worker = BaselineWorker(runner=run)  # the REAL fleet.util.proc.run, real docker/pip/pytest
    payload = _real_baseline_payload(ecosystem=Ecosystem.PYPI, worktree=tmp_path)

    result = await worker.run(ctx, payload)

    assert result.status == "ok", result.error
    assert result.output is not None
    assert result.output.build_exit_code == 0, "the build (pip install -e .) step failed"
    assert result.output.test_exit_code == 0, "the test (pytest) step failed"
    assert result.output.baseline_ok is True, (
        "a healthy PyPI repo with a real third-party dependency must measure baseline_ok=True "
        "through Leg B's real two-separate-container invocation shape"
    )
    assert result.output.baseline_test_count == 1


@pytest.mark.integration
@pytest.mark.skipif(not _IMAGE_OK, reason=f"fleet baseline image unusable: {_IMAGE_WHY}")
async def test_the_real_baseline_worker_measures_an_npm_repo_through_two_real_containers(
    tmp_path: Path,
) -> None:
    """The Node.js/npm half of the test above, same shape: the REAL `BaselineWorker` against a
    real npm dependency (`left-pad`), through two real, separate containers."""
    _write_js_fixture(tmp_path)
    ctx = make_ctx(tmp_path, seconds_left=180.0)
    worker = BaselineWorker(runner=run)
    payload = _real_baseline_payload(ecosystem=Ecosystem.NPM, worktree=tmp_path)

    result = await worker.run(ctx, payload)

    assert result.status == "ok", result.error
    assert result.output is not None
    assert result.output.build_exit_code == 0, "the build (npm install) step failed"
    assert result.output.test_exit_code == 0, "the test (npm test) step failed"
    assert result.output.baseline_ok is True
    assert result.output.baseline_test_count == 1


@pytest.mark.integration
@pytest.mark.skipif(not _IMAGE_OK, reason=f"fleet baseline image unusable: {_IMAGE_WHY}")
async def test_the_baseline_image_survives_an_unmapped_uid_with_no_env_passed() -> None:
    """`docker_run_argv` passes no `--env` unless the caller supplies `env=`, and neither
    `BaselineWorker._argv` (the two tests above, real production code) nor this test's own
    `ContainerSpec` supply one — so `PIP_BREAK_SYSTEM_PACKAGES`/`PIP_USER`/`PYTHONUSERBASE`/`HOME`
    (the arbitrary-uid fixes `docker/fleet-baseline.Dockerfile` documents as load-bearing) must
    survive as image `ENV`, the same fact `test_sandbox.py`'s
    `test_the_fleet_build_image_runs_bazels_lookups_as_an_unmapped_uid` asserts for the sibling
    image's `HOME`/`USER` — restated here rather than shared, for a genuinely different failure
    mode: pip's own PEP-668/user-site behaviour, not Bazel's `GetUserName()`.

    Deliberately NO bind mount (unlike the two tests above): a passwd-less uid with no
    `/etc/passwd` entry has no ownership over a HOST-owned directory either, and mounting one
    here would prove a directory-permissions fact about the HOST, not about this image — exactly
    why `test_the_fleet_build_image_runs_bazels_lookups_as_an_unmapped_uid` mounts nothing for
    its own probe. What this proves instead: `$HOME` is writable by an uid the image never saw at
    build time, and pip's own break-system-packages/user-site/PYTHONUSERBASE environment survives
    with no `--env` on the `docker run` line — real production behaviour (a bind-mounted
    worktree the HOST already owns, and the actual persistence-across-two-containers fact) is
    what the two tests above cover under the REAL host uid.
    """
    probe = (
        "echo HOME=$HOME; touch $HOME/probe.txt && echo WRITABLE; "
        "env | grep -E '^PIP_|^PYTHONUSERBASE'"
    )
    spec = ContainerSpec(
        image=BASELINE_IMAGE,
        name=f"fleet-{RUN_ID}-baseline-env-probe",
        command=("sh", "-c", probe),
        network=BASELINE_NETWORK,
        user="4242:4242",
    )
    assert not [a for a in docker_run_argv(spec) if a.startswith("--env")], (
        "the harness passes no --env; a test that did would prove the wrong image"
    )

    result = await ContainerSandbox().run(spec, timeout_s=60)

    assert result.ok, result.stderr_tail
    assert "HOME=/home/fleet" in result.stdout_tail, result.stdout_tail
    assert "WRITABLE" in result.stdout_tail, result.stdout_tail
    assert "PIP_BREAK_SYSTEM_PACKAGES=1" in result.stdout_tail, result.stdout_tail
    assert "PIP_USER=1" in result.stdout_tail, result.stdout_tail
    assert "PYTHONUSERBASE=/work/.fleet-baseline-pyuser" in result.stdout_tail, result.stdout_tail
