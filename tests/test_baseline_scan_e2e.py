"""§12.11/D116 Leg B (round VI task 107, ADR-0135): a real `fleet scan`, over real git repos,
proving `repos.baseline_ok`/`repos.baseline_test_count` get written for real — the "done looks
like" bullet the task brief itself demands ("A real end-to-end test (through the CLI, reading
back real DB state)").

**Why NPM, not PyPI, and why `container_image: null`.** Leg D (the container image ADR-0135
ruling 3 requires) had not landed when this task was written (round VI task 108, a sibling task
this same wave) — so this file exercises the worker's OTHER documented escape hatch:
`preflight.baseline_build.container_image: null` runs `build_argv`/`test_argv` directly on the
harness host. `npm install`/`npm test` against a package with ZERO external dependencies need no
network and no fragile host toolchain assumption (confirmed by hand before writing this file:
`npm install --no-audit --no-fund` against such a package resolves in well under a second,
offline, on this project's own dev host) — unlike PyPI's `pip install -e .`, which resolves
against whatever `python3` is first on `PATH` and, on a Debian-family host with no active
virtualenv, hits PEP 668's "externally-managed-environment" refusal. Both fixture repos below
therefore publish an NPM coordinate; neither is `tests/test_scan_e2e.py`'s own fixture fleet
(this file builds its own two-repo fleet, reusing that module's git/config/db helpers) — that
file's own Python fixture (`acme-app-py`) declares real PyPI dependencies (`requests>=2.31`)
that would need real network to resolve under a bare-host baseline run, which is exactly the
blast-radius hazard `settings.py::BaselineBuild.container_image`'s shipped UNBUILT-placeholder
default exists to avoid for every OTHER e2e test in this suite that scans that fixture fleet
under the shipped config (their runs never override `container_image`, so they get the fast,
harmless "docker: pull access denied" classification instead — see this module's sibling test
`test_the_shipped_default_config_records_baseline_ok_false_fast_not_a_hang_or_a_network_call`,
which proves that side of the contract for `test_scan_e2e.py`'s own Python/npm fixture without
requiring a real Docker daemon at all, only a real `docker` binary that fails to find the image).
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.test_scan_e2e import (
    _fresh_db,
    _make_repo,
    _write_config,
    query,
    scan,
)

FLEET_YAML_HOST_BASELINE = """\
run:
  monorepo_path: ../acme-monorepo
  cache_dir: cache/
  work_dir: work/
concurrency:
  cpu_pool_workers: 1
  docker: 1
verify:
  container_memory: 64m
budgets:
  max_rss_mb: 512
preflight:
  min_free_bytes: 1048576
  baseline_build:
    # The documented, explicit, narrower escape hatch (settings.py's own docstring: "never the
    # shipped default") -- this file's whole reason to exist is proving the OBSERVED write path,
    # which needs a command that can actually run in this sandboxed dev environment.
    container_image: null
"""

#: Zero external dependencies each, so `npm install` never touches the network (confirmed by
#: hand: ~150ms, offline, before this file was written). `scripts.test` is what `_classify`
#: actually observes -- the green repo's exits 0, the red repo's exits 1 (a REAL native test
#: failure, not a build failure: `npm install` succeeds for both).
GREEN_NPM_REPO = {
    "package.json": (
        "{\n"
        '  "name": "@acme/baseline-green",\n'
        '  "version": "1.0.0",\n'
        '  "scripts": {\n'
        '    "test": "node -e \\"console.log(\'1 passing\')\\""\n'
        "  }\n"
        "}\n"
    ),
}

RED_NPM_REPO = {
    "package.json": (
        "{\n"
        '  "name": "@acme/baseline-red",\n'
        '  "version": "1.0.0",\n'
        '  "scripts": {\n'
        '    "test": "node -e \\"process.exit(1)\\""\n'
        "  }\n"
        "}\n"
    ),
}


@pytest.fixture
def baseline_fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A two-repo fleet: one native baseline that genuinely passes, one that genuinely fails.
    Neither is `test_scan_e2e.py`'s own fixture fleet (see module docstring for why)."""
    sources = {
        "acme-baseline-green": _make_repo(
            tmp_path / "sources", "acme-baseline-green", GREEN_NPM_REPO
        ),
        "acme-baseline-red": _make_repo(tmp_path / "sources", "acme-baseline-red", RED_NPM_REPO),
    }
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_config(
        workspace,
        sources,
        names=list(sources),
        fleet_yaml=FLEET_YAML_HOST_BASELINE,
    )
    _fresh_db(workspace / "state" / "fleet.db")
    monkeypatch.chdir(workspace)
    yield workspace


def test_a_real_fleet_scan_writes_baseline_ok_and_baseline_test_count_for_real(
    baseline_fleet: Path,
) -> None:
    """The brief's own "done looks like" bullet: a real `fleet scan`, real DB state read back.

    `npm install` then `npm test` run for real, bare (no container -- see module docstring), for
    BOTH repos. `acme-baseline-green`'s script prints and exits 0: `baseline_ok=1`,
    `baseline_test_count=1` (the observed-count classifier's exit-0 branch). `acme-baseline-red`'s
    script exits 1: `baseline_ok=0`, `baseline_test_count=0` -- and the scan still exits 0 (the
    green-path contract: a native test's own failure never escalates the repo's status).
    """
    result = scan(baseline_fleet)
    assert result.exit_code == 0, result.output

    def _rows() -> dict[str, tuple[Any, Any]]:
        return {
            repo_id: (ok, count)
            for repo_id, ok, count in query(
                baseline_fleet,
                "SELECT repo_id, baseline_ok, baseline_test_count FROM repos "
                "WHERE repo_id IN ('acme-baseline-green', 'acme-baseline-red')",
            )
        }

    rows = _rows()
    assert rows["acme-baseline-green"] == (1, 1), rows
    assert rows["acme-baseline-red"] == (0, 0), rows

    # And a REPEAT scan (§11.7 idempotent upsert) writes the identical rows, not a duplicate or
    # a NULL regression -- the same "re-scan writes the same rows" property `test_scan_e2e.py`
    # already asserts for `clone`'s own columns.
    result2 = scan(baseline_fleet)
    assert result2.exit_code == 0, result2.output
    assert _rows() == rows


def test_disabling_baseline_build_leaves_baseline_ok_null(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`preflight.baseline_build.enabled: false` genuinely disables the worker (the brief's own
    "done looks like" bullet) -- proven against the SAME green fixture repo that measures
    `baseline_ok=1` above, so the only variable is the flag. `baseline_test_count` stays at the
    SCHEMA's own default (`state/schema.sql`: `INTEGER NOT NULL DEFAULT 0`) rather than NULL --
    `baseline_ok IS NULL` alone is what "never measured" means (schema.sql's own comment)."""
    sources = {
        "acme-baseline-green": _make_repo(
            tmp_path / "sources", "acme-baseline-green", GREEN_NPM_REPO
        ),
    }
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_config(
        workspace,
        sources,
        names=list(sources),
        fleet_yaml=FLEET_YAML_HOST_BASELINE
        + "  baseline_build:\n    enabled: false\n    container_image: null\n",
    )
    _fresh_db(workspace / "state" / "fleet.db")
    monkeypatch.chdir(workspace)

    result = scan(workspace)
    assert result.exit_code == 0, result.output
    rows = query(
        workspace,
        "SELECT baseline_ok, baseline_test_count FROM repos WHERE repo_id = 'acme-baseline-green'",
    )
    assert rows == [(None, 0)], rows


def test_an_unbuilt_configured_image_records_baseline_ok_false_fast_not_a_hang_or_a_network_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The OTHER half of the container-posture contract: an operator-configured, UNBUILT local
    docker tag, a real `fleet scan` over a repo whose native baseline WOULD need real network to
    resolve (a `package.json` declaring a real external dependency) still completes fast and
    green, because `docker run` on an unbuilt local tag fails in well under a second with no pull
    attempted against an unreachable dependency at all. `settings.py`'s own docstring measured
    this at ~0.3s / exit 125 ("pull access denied") by hand; this test proves it through the real
    CLI within a generous 25s ceiling -- proof against a hang, not a tight timing assertion.

    **Corrected 2026-09-10 (round VI task 108, Leg D landed).** This test used to rely on the
    SHIPPED PYDANTIC DEFAULT itself being an unbuilt placeholder tag (`fleet-baseline:
    leg-d-pending`) -- true only in the interim between Leg B landing and Leg D landing. Leg D
    (`docker/fleet-baseline.Dockerfile`) now exists and repointed that default at a REAL image;
    on any host where an operator has actually run the documented `docker build`, the shipped
    default is no longer unbuilt, and this test's old premise ("the default config" ⇒ "an unbuilt
    image") would silently start asserting a fact about the HOST's build state instead of a fact
    about this contract. Fixed by naming the unbuilt tag EXPLICITLY in the fixture's own
    `preflight.baseline_build.container_image` override (the placeholder tag Leg B originally
    shipped as the default, reused here as a deliberately-nonexistent local tag no `docker build`
    in this repo ever produces) -- the fast-fail contract this test exists to prove is unchanged,
    only its coupling to whichever tag happens to be the current pydantic default is removed.
    """
    real_dep_repo = {
        "package.json": (
            "{\n"
            '  "name": "@acme/baseline-needs-network",\n'
            '  "version": "1.0.0",\n'
            '  "dependencies": {"left-pad": "^1.3.0"},\n'
            '  "scripts": {"test": "node -e \\"console.log(1)\\""}\n'
            "}\n"
        ),
    }
    sources = {
        "acme-baseline-needs-network": _make_repo(
            tmp_path / "sources", "acme-baseline-needs-network", real_dep_repo
        ),
    }
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_config(
        workspace,
        sources,
        names=list(sources),
        fleet_yaml=(
            "run:\n"
            "  monorepo_path: ../acme-monorepo\n"
            "  cache_dir: cache/\n"
            "  work_dir: work/\n"
            "concurrency:\n"
            "  cpu_pool_workers: 1\n"
            "  docker: 1\n"
            "verify:\n"
            "  container_memory: 64m\n"
            "budgets:\n"
            "  max_rss_mb: 512\n"
            "preflight:\n"
            "  min_free_bytes: 1048576\n"
            "  baseline_build:\n"
            # Named EXPLICITLY rather than left to the pydantic default (round VI task 108, Leg
            # D landed): this is the placeholder tag Leg B's own default used to be, kept here
            # as a deliberately-nonexistent local tag so the fast-fail contract this test proves
            # does not depend on whether an operator has actually built Leg D's real image on
            # the host running this test.
            "    container_image: fleet-baseline:leg-d-pending\n"
        ),
    )
    _fresh_db(workspace / "state" / "fleet.db")
    monkeypatch.chdir(workspace)

    started = time.monotonic()
    result = scan(workspace)
    elapsed = time.monotonic() - started
    assert result.exit_code == 0, result.output
    assert elapsed < 25.0, f"scan took {elapsed:.1f}s -- looks like a hang, not a fast refusal"
    rows = query(
        workspace,
        "SELECT baseline_ok, baseline_test_count FROM repos "
        "WHERE repo_id = 'acme-baseline-needs-network'",
    )
    assert rows == [(0, 0)], rows
