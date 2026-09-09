"""`docker/fleet-baseline.Dockerfile`: the native-baseline sandbox image (ADR-0135 Leg D, round VI
task 108) — a SEPARATE image from `docker/fleet-build.Dockerfile`'s Bazel `--network=none`
sandbox, built WITH network access so `EcosystemAdapter.native_baseline()`'s `build_argv`/
`test_argv` (`src/fleet/models/build.py::NativeBaseline`) can resolve an arbitrary third-party
repo's OWN, pre-migration PyPI/npm dependencies exactly as ADR-0135 ruling 3 requires.

Same discipline as `test_sandbox.py`'s two live-daemon tests: gate on the image being present
LOCALLY (never pull — a registry round-trip would make a green run depend on someone else's
uptime) and skip LOUDLY, naming why, rather than let an absent daemon look like a pass.

This is Leg D only — proving the IMAGE genuinely has a working Python+pytest and Node+npm
toolchain with network access, invocable through this codebase's own `spec_for_attempt`/
`ContainerSandbox` machinery. It does not build `src/fleet/workers/baseline.py` (Leg B, a sibling
task) and does not touch the shipped `tests/test_scan_e2e.py::FIXTURE_REPOS` fixtures, which have
no native test files of their own yet (a disclosed Leg E gap, not this task's to close) — the
fixture content used below is a LOCAL, throwaway repo shaped identically to the real
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

from fleet.sandbox.container import (
    ContainerSandbox,
    ContainerSpec,
    docker_run_argv,
    spec_for_attempt,
)
from fleet.settings import PreflightSection
from fleet.util.proc import run

RUN_ID = UUID("00000000-0000-4000-8000-0000000010c8")  # task 108
BASELINE_IMAGE = PreflightSection().baseline_build.container_image
"""Read from the setting, never re-typed — same reasoning `test_sandbox.py`'s `FLEET_BUILD_IMAGE`
gives for `VerifySection.container_image`: this suite is about the image the fleet will actually
run, so retagging `settings.py` must move the test with it."""

BASELINE_NETWORK = PreflightSection().baseline_build.network
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
        '[project]\nname = "acme-lib-py"\nversion = "2.0.1"\n'
        'dependencies = ["requests>=2.31"]\n'
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
        "    assert requests.__name__ == \"requests\"\n"
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


@pytest.mark.integration
@pytest.mark.skipif(not _IMAGE_OK, reason=f"fleet baseline image unusable: {_IMAGE_WHY}")
async def test_the_baseline_image_runs_a_real_pip_install_and_pytest_with_network(
    tmp_path: Path,
) -> None:
    """The Python half of the brief's scope item 1: a real interpreter + `pytest`, run through
    this codebase's own `spec_for_attempt`/`docker_run_argv`/`ContainerSandbox` — the identical
    machinery Bazel's own sandbox invokes (`workers/buildverify.py::BuildverifyWorker._argv`) —
    against a mounted worktree, as the arbitrary HOST uid `current_user_spec()` produces, with
    real network access to resolve a real PyPI dependency (`requests`).

    Two argv, joined by `sh -c '... && ...'` exactly as `EcosystemAdapter.native_baseline()`
    documents ("run ... after `build_argv` (if any) succeeds") — this is the shape Leg B's own
    `spec_for_attempt` call is expected to build, restated here as a proof rather than guessed.
    """
    _write_py_fixture(tmp_path)
    spec = spec_for_attempt(
        run_id=RUN_ID,
        repo="acme-lib-py",
        attempt=1,
        image=BASELINE_IMAGE,
        command=["sh", "-c", "python3 -m pip install -e . && python3 -m pytest -q"],
        worktree=tmp_path,
        memory=BASELINE_MEMORY,
        cpus=BASELINE_CPUS,
        network=BASELINE_NETWORK,
    )
    assert spec.network != "none", (
        "ADR-0135 ruling 3: a native baseline must NOT run under Bazel's own network=none "
        "posture, or dependency resolution against the real PyPI cannot succeed"
    )

    result = await ContainerSandbox().run(spec, timeout_s=180)

    assert result.ok, result.stderr_tail
    assert "1 passed" in result.stdout_tail, result.stdout_tail


@pytest.mark.integration
@pytest.mark.skipif(not _IMAGE_OK, reason=f"fleet baseline image unusable: {_IMAGE_WHY}")
async def test_the_baseline_image_runs_a_real_npm_install_and_test_with_network(
    tmp_path: Path,
) -> None:
    """The Node.js/npm half of scope item 2, mirroring the Python test above exactly:
    `JsAdapter.native_baseline()`'s own argv (`npm install` then `npm test`), run against a real
    third-party npm dependency (`left-pad`) resolved over the real network, through the same
    production container machinery.
    """
    _write_js_fixture(tmp_path)
    spec = spec_for_attempt(
        run_id=RUN_ID,
        repo="acme-lib-ts",
        attempt=1,
        image=BASELINE_IMAGE,
        command=["sh", "-c", "npm install && npm test"],
        worktree=tmp_path,
        memory=BASELINE_MEMORY,
        cpus=BASELINE_CPUS,
        network=BASELINE_NETWORK,
    )

    result = await ContainerSandbox().run(spec, timeout_s=180)

    assert result.ok, result.stderr_tail


@pytest.mark.integration
@pytest.mark.skipif(not _IMAGE_OK, reason=f"fleet baseline image unusable: {_IMAGE_WHY}")
async def test_the_baseline_image_survives_an_unmapped_uid_with_no_env_passed() -> None:
    """`docker_run_argv` passes no `--env` unless the caller supplies `env=`, and Leg D's own
    `spec_for_attempt` calls above supply none — so `PIP_BREAK_SYSTEM_PACKAGES`/`PIP_USER`/`HOME`
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
    build time, and pip's own break-system-packages/user-site environment survives with no
    `--env` on the `docker run` line — real production behaviour is a bind-mounted worktree the
    HOST already owns, which the two tests above cover under the REAL host uid.
    """
    probe = "echo HOME=$HOME; touch $HOME/probe.txt && echo WRITABLE; env | grep ^PIP_"
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
