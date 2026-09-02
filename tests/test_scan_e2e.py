"""Phase 1 end to end, through the real CLI, over real git repositories (§3.1, §12).

This is the first test in the suite that drives clone → interrogate → symbol index → edge
inference → cycle breaking → wave layering through `fleet scan` and `fleet sequence` rather than
through in-process fixtures, and it exists to catch the class of defect the unit tests
*structurally cannot see*: a composition error. Every step here is separately covered and
separately green; what is unproven until this file runs is that they agree with each other about
edge orientation, about which table holds what, and about who is allowed to write a status.

The fixture fleet is five real temporary git repositories with real manifests — an npm app that
declares a dependency on an npm library, a Python app that declares a dependency on a Python
library, and one repository that has been `git init`-ed and never committed to. They are cloned
over the filesystem, so the whole file runs with no network; the one model-bearing step
(`classify`, ADR-0008) is dropped with `--skip-classify`, which is what makes "no model call"
a property of the command line rather than of a mock.

Each test says why it matters. Together:

* the tables actually hold what Phase 1 claims to produce — asserted against `repos`,
  `manifests`, `coordinates` and `symbols`, never against stdout, because a verb that prints a
  summary it did not persist is exactly the failure an end-to-end test is for;
* one empty repository is `SKIPPED` with an `EmptyRepo` finding instead of stopping the run —
  one bad repo in 250 must not cost the other 249;
* the library lands in an earlier wave than the app that depends on it. This is the payoff: it
  is the single assertion that proves orientation, inference, layering and persistence all agree,
  and it is the exact thing a reversed graph would break while passing every step's own tests;
* a second `fleet scan` clones nothing and duplicates no row;
* a scan interrupted mid-fleet resumes without losing what landed;
* the run exits 0 and `migration_state.json` round-trips back into `MigrationState`.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from fleet.cli import ExitCode, app
from fleet.models.state import MigrationState
from fleet.state.db import SCHEMA_PATH
from tests.test_cli import MODELS_YAML

runner = CliRunner()

FLEET_YAML = """\
run:
  monorepo_path: ../acme-monorepo
  cache_dir: cache/
  work_dir: work/
concurrency:
  cpu_pool_workers: 1
preflight:
  # §11.3's floor is 50 GiB, which no developer machine — and no CI runner — clears with room to
  # spare, so a fixture that left it at the default would exit 9 before Phase 1 started. Lowered
  # rather than disabled, because 0 would mean "unchecked" and this fleet is four repos of a few
  # kilobytes: the gate really runs here, it simply passes. The refusal itself is asserted where
  # it belongs, against a floor no volume can clear (`test_cli.py`, `test_workers_scan.py`).
  min_free_bytes: 1048576
"""
"""Contracts are left ENABLED (the shipped default). This fleet declares no IDL, so §3.1 step 5b
discovers nothing — which is the case worth having in the end-to-end file: a fleet that shares no
contract must produce zero `contracts` rows *and still exit 0*, rather than the refusal this
config previously had to answer with `hoist_contracts: false`."""

LFS_FLEET_YAML = FLEET_YAML + "  require_lfs_binary: false\n"
"""`FLEET_YAML` plus `preflight.require_lfs_binary: false`, used only by `lfs_fleet` below.

Whether `git-lfs` happens to be on the PATH of the machine running this suite is not this
fixture's business — what it exists to prove is that `has_lfs` is measured and persisted for a
repo that declares `filter=lfs`, not that the `require_lfs_binary` gate (`clone.py`'s
`shutil.which("git-lfs")` check) fires. Disabling it keeps the assertion true regardless of the
host's toolchain."""

#: Repo name → (relative path, contents). Real manifests: the npm and python adapters parse
#: these files for real, and the edges under test come out of what they declare.
FIXTURE_REPOS: dict[str, dict[str, str]] = {
    "acme-lib-ts": {
        "package.json": json.dumps(
            {"name": "@acme/lib", "version": "1.4.0", "main": "src/index.ts"}, indent=2
        ),
        "src/index.ts": (
            "export function formatMoney(cents: number): string {\n"
            "  return `$${(cents / 100).toFixed(2)}`;\n"
            "}\n"
            "export class Ledger {\n"
            "  total = 0;\n"
            "}\n"
        ),
    },
    "acme-app-ts": {
        "package.json": json.dumps(
            {
                "name": "@acme/app",
                "version": "0.2.0",
                "dependencies": {"@acme/lib": "^1.4.0", "left-pad": "^1.3.0"},
            },
            indent=2,
        ),
        # The deep specifier is deliberate: `@acme/lib/src/index` reaches INTO the dependency's
        # source layout, which is the kind of cross-repo import a migration legitimately rewrites
        # (to the package entry) — and it gives `tests/test_transform_e2e.TS_IMPORT_RULE` a
        # rewrite whose result is not its own input, so a second pass is a no-op.
        "src/main.ts": (
            "import { formatMoney } from '@acme/lib/src/index';\n"
            "export function render(cents: number): string {\n"
            "  return formatMoney(cents);\n"
            "}\n"
        ),
    },
    "acme-lib-py": {
        "pyproject.toml": (
            "[project]\n"
            'name = "acme-lib-py"\n'
            'version = "2.0.1"\n'
            "dependencies = []\n"
        ),
        "acme_lib_py/__init__.py": (
            "def normalize(name: str) -> str:\n"
            '    """Lower-case a coordinate name."""\n'
            "    return name.lower()\n"
            "\n"
            "\n"
            "class Registry:\n"
            "    pass\n"
        ),
    },
    "acme-app-py": {
        "pyproject.toml": (
            "[project]\n"
            'name = "acme-app-py"\n'
            'version = "0.1.0"\n'
            'dependencies = ["acme-lib-py>=2.0", "requests>=2.31"]\n'
        ),
        "acme_app_py/main.py": (
            "from acme_lib_py import normalize\n"
            "\n"
            "\n"
            "def run(name: str) -> str:\n"
            "    return normalize(name)\n"
        ),
    },
    #: `git init` and nothing else. §3.1 step 1: "SKIPPED, not an error".
    "acme-empty": {},
}

LIBRARIES = ("acme-lib-ts", "acme-lib-py")
APPLICATIONS = ("acme-app-ts", "acme-app-py")


# ---------------------------------------------------------------------------------------
# fixture fleet
# ---------------------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=cwd,
        check=True,
        capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "Fleet Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Fleet Fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "HOME": str(cwd),
            "PATH": "/usr/bin:/bin:/usr/local/bin",
        },
    )


def _make_repo(root: Path, name: str, files: dict[str, str]) -> Path:
    """One real git repository. `files` empty ⇒ initialized with no commit, which is the
    empty-repo case §3.1 step 1 gates: `rev-list --count --all` is 0 and there is no HEAD."""
    path = root / name
    path.mkdir(parents=True)
    _git(path, "init", "--initial-branch=main")
    for rel, text in sorted(files.items()):
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    if files:
        _git(path, "add", "-A")
        _git(path, "commit", "-m", "fixture")
    return path


def _make_repo_with_submodule(root: Path, name: str, target: Path, submodule_path: str) -> Path:
    """One real git repository whose HEAD tree carries a genuine submodule reference: a real
    `.gitmodules` and a `160000` gitlink entry, produced by an actual `git submodule add` against
    `target` — not a hand-written `.gitmodules` with no gitlink behind it.

    `target` never has to be fetched again after this returns: §3.1 step 1's probe
    (`CloneWorker._submodule_count`) only reads `.gitmodules`' text at `head_sha` via `git show`,
    so the submodule's own history being unreachable later is exactly the case this fixture is
    for. `protocol.file.allow=always` is required — modern git refuses a local-path submodule
    fetch by default (CVE-2022-39253), and `target` is deliberately local so this fixture never
    touches the network.
    """
    path = _make_repo(root, name, {"app.py": "print('hi')\n"})
    _git(path, "-c", "protocol.file.allow=always", "submodule", "add", str(target), submodule_path)
    _git(path, "commit", "-m", "add submodule")
    return path


def _make_repo_with_default_branch(
    root: Path, name: str, files: dict[str, str], branch: str
) -> Path:
    """One real git repository whose default branch is `branch`, not `main` — a `git branch -m`
    rename of the fixture commit `_make_repo` already made, so `symbolic-ref --short HEAD` in the
    finished mirror genuinely reports `branch` rather than a hand-edited ref with no commit behind
    it.
    """
    path = _make_repo(root, name, files)
    _git(path, "branch", "-m", branch)
    return path


def _write_config(
    root: Path, sources: dict[str, Path], *, names: Sequence[str], fleet_yaml: str = FLEET_YAML
) -> None:
    config = root / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "fleet.yaml").write_text(fleet_yaml, encoding="utf-8")
    (config / "models.yaml").write_text(MODELS_YAML, encoding="utf-8")
    entries = "".join(
        f"  - name: {name}\n    url: {sources[name]}\n" for name in names
    )
    (config / "repos.yaml").write_text(
        f"version: 1\ndefaults:\n  ref: main\nrepos:\n{entries}", encoding="utf-8"
    )


def _fresh_db(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    finally:
        conn.close()
    return path


@pytest.fixture
def fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A workspace holding five real git repos, a config bundle naming them, and a fresh db."""
    sources = {
        name: _make_repo(tmp_path / "sources", name, files)
        for name, files in FIXTURE_REPOS.items()
    }
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_config(workspace, sources, names=list(FIXTURE_REPOS))
    _fresh_db(workspace / "state" / "fleet.db")
    monkeypatch.chdir(workspace)
    yield workspace


def base_args(root: Path) -> list[str]:
    return ["--config", str(root / "config" / "fleet.yaml"), "--db", str(root / "state/fleet.db")]


def scan(root: Path, *extra: str) -> Any:
    return runner.invoke(
        app,
        [*base_args(root), "scan", "--skip-classify", *extra],
        catch_exceptions=False,
    )


def sequence(root: Path, *extra: str) -> Any:
    return runner.invoke(
        app, [*base_args(root), "--json", "sequence", *extra], catch_exceptions=False
    )


def query(root: Path, sql: str, params: tuple[object, ...] = ()) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(root / "state" / "fleet.db")
    try:
        return [tuple(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


# ---------------------------------------------------------------------------------------
# the scan
# ---------------------------------------------------------------------------------------


def test_scan_persists_the_phase_1_evidence_tables(fleet: Path) -> None:
    """`fleet scan` exits 0 and the DATABASE holds a row per repo, its manifests, its
    coordinates and its symbols.

    Why on the tables and not on stdout: the whole point of Phase 1 is the evidence it leaves
    behind for steps 5–8 and for Phase 2, and a summary line is not evidence. Every earlier
    checkpoint could print a plausible total while writing nothing, because nothing downstream of
    the workers had ever been asked to read what they produced.
    """
    result = scan(fleet)
    assert result.exit_code == ExitCode.SUCCESS, result.output

    repos = {row[0]: row for row in query(fleet, "SELECT repo_id, head_sha, url FROM repos")}
    assert set(repos) == set(FIXTURE_REPOS), "every manifest entry must reach `repos`"
    for name in (*LIBRARIES, *APPLICATIONS):
        assert repos[name][1], f"{name} recorded no head_sha"

    manifests = dict(
        query(fleet, "SELECT repo_id, path FROM manifests WHERE repo_id NOT LIKE '%empty%'")
    )
    assert manifests["acme-lib-ts"] == "package.json"
    assert manifests["acme-app-py"] == "pyproject.toml"

    published = dict(
        query(
            fleet,
            "SELECT coord_key, owner_repo_id FROM coordinates "
            " WHERE owner_repo_id IS NOT NULL",
        )
    )
    assert published["npm:@acme:lib"] == "acme-lib-ts"
    assert published["pypi::acme-lib-py"] == "acme-lib-py"

    external = query(
        fleet, "SELECT coord_key FROM coordinates WHERE owner_repo_id IS NULL ORDER BY coord_key"
    )
    assert ("npm::left-pad",) in external, "an external dependency is still a coordinate"

    symbols = dict(
        query(fleet, "SELECT repo_id, COUNT(*) FROM symbols GROUP BY repo_id")
    )
    assert symbols["acme-lib-ts"] > 0, "the TypeScript library indexed no symbols"
    assert symbols["acme-lib-py"] > 0, "the Python library indexed no symbols"


def test_an_empty_repo_is_skipped_with_a_finding_and_the_fleet_continues(fleet: Path) -> None:
    """The commit-less repo is `SKIPPED` with an `EmptyRepo` finding, and the other four scan.

    Why: one pathological repo in a fleet of 250 must cost exactly itself. The failure mode this
    guards is not a crash — it is the *quiet* version, where the empty repo takes the run's exit
    code to 7 (or the ladder's three attempts) and an operator is asked to intervene on a
    repository that has done nothing wrong.
    """
    result = scan(fleet)
    assert result.exit_code == ExitCode.SUCCESS, result.output

    statuses = dict(query(fleet, "SELECT repo_id, status FROM phases WHERE phase = 1"))
    assert statuses["acme-empty"] == "SKIPPED"
    for name in (*LIBRARIES, *APPLICATIONS):
        assert statuses[name] == "SUCCEEDED", f"{name} did not survive the empty repo"

    findings = query(
        fleet, "SELECT kind FROM findings WHERE repo_id = 'acme-empty' ORDER BY kind"
    )
    assert ("EmptyRepo",) in findings

    assert query(fleet, "SELECT COUNT(*) FROM manifests WHERE repo_id = 'acme-empty'") == [(0,)]


def test_scan_makes_no_model_call_by_default_over_local_repos(fleet: Path) -> None:
    """The default scan path over local repositories reaches no model and no network.

    Why it has to be mechanical: `classify` is the only model-bearing step (ADR-0008) and it is
    advisory, so a run that quietly attempted it against an unreachable backend would halt the
    whole fleet with exit 8 for a label nothing is allowed to branch on. The proof is that a
    `ModelClient.complete` wired to explode is never called.
    """
    calls: list[str] = []

    async def explode(*args: object, **kwargs: object) -> object:
        calls.append("complete")
        raise AssertionError("the default scan path must not reach a model")

    monkeypatched = pytest.MonkeyPatch()
    try:
        monkeypatched.setattr("fleet.llm.client.LadderModelClient.complete", explode)
        assert scan(fleet).exit_code == ExitCode.SUCCESS
    finally:
        monkeypatched.undo()
    assert calls == []
    assert dict(query(fleet, "SELECT repo_id, kind FROM repos"))["acme-app-ts"] == "unknown"


# ---------------------------------------------------------------------------------------
# the payoff: the sequencer agrees with the scanner
# ---------------------------------------------------------------------------------------


def test_a_library_is_sequenced_before_the_app_that_depends_on_it(fleet: Path) -> None:
    """Both libraries land in a strictly earlier wave than the apps that declare them.

    **This is the assertion the whole file is for.** It is the only one that fails if any single
    link in the chain is inverted or dropped: the manifest adapter's coordinate, the
    `coordinates` ownership oracle, `infer_edges`' dependent → dependency orientation, the
    `G_order = G_dag.reverse()` in `build_graph`, the longest-path layering in `assign_waves`,
    and the `wave_members` rows the plan is read back out of. A mirror-image graph migrates the
    fleet leaves-first and passes every one of those modules' own tests.
    """
    assert scan(fleet).exit_code == ExitCode.SUCCESS
    result = sequence(fleet)
    assert result.exit_code == ExitCode.SUCCESS, result.output

    waves = json.loads(result.stdout)["wave_index_by_repo"]
    assert waves["acme-lib-ts"] < waves["acme-app-ts"], (
        f"npm library in wave {waves['acme-lib-ts']}, its consumer in "
        f"{waves['acme-app-ts']}: the ordering subgraph is reversed"
    )
    assert waves["acme-lib-py"] < waves["acme-app-py"], (
        f"python library in wave {waves['acme-lib-py']}, its consumer in "
        f"{waves['acme-app-py']}: the ordering subgraph is reversed"
    )

    # The same order has to survive the round trip into SQLite, because that — not the JSON —
    # is what the scheduler admits from.
    members = dict(
        query(fleet, "SELECT node_id, wave_index FROM wave_members WHERE node_kind = 'REPO'")
    )
    assert members["acme-lib-ts"] < members["acme-app-ts"]
    assert members["acme-lib-py"] < members["acme-app-py"]
    assert "acme-empty" not in members, "a SKIPPED repo must never be given a wave (§3.1 step 7)"


def test_the_declared_dependency_edge_points_from_dependent_to_dependency(fleet: Path) -> None:
    """`edges` is written dependent → dependency, with the manifest as its evidence path.

    Why separately from the wave assertion: the wave order is the *consequence*, and a test that
    only checks the consequence cannot say whether a future inversion happened in inference or in
    layering. This pins the orientation at the row level, where §3.1 step 5 states it.
    """
    assert scan(fleet).exit_code == ExitCode.SUCCESS

    rows = query(
        fleet,
        "SELECT src_id, dst_id, kind, evidence_path, confidence FROM edges "
        " WHERE dst_id IS NOT NULL ORDER BY src_id, dst_id",
    )
    declared = {(str(row[0]), str(row[1])) for row in rows}
    assert ("acme-app-ts", "acme-lib-ts") in declared
    assert ("acme-app-py", "acme-lib-py") in declared
    assert ("acme-lib-ts", "acme-app-ts") not in declared, "the edge is reversed"

    npm_edge = next(row for row in rows if row[0] == "acme-app-ts")
    assert npm_edge[3] == "package.json", "an edge must name the file that proves it"
    assert float(npm_edge[4]) >= 0.5, "a declared dependency below min_confidence orders nothing"

    # §3.5's admission key is derived from the same graph: the libraries carry a blast radius.
    radii = dict(query(fleet, "SELECT repo_id, blast_radius FROM repos"))
    assert radii["acme-lib-ts"] == 1
    assert radii["acme-app-ts"] == 0


# ---------------------------------------------------------------------------------------
# re-entry
# ---------------------------------------------------------------------------------------


def test_a_second_scan_clones_nothing_and_duplicates_no_row(fleet: Path) -> None:
    """Re-running `fleet scan` is a no-op: no git operation, no second row, same head SHAs.

    Why: `fleet scan` is the verb an operator re-runs after every intervention, and a re-scan
    that re-clones 250 mirrors is a re-scan nobody dares run. The clone is proved absent by
    DELETING the source repositories first — the mirrors and worktrees are already on disk, so a
    run that touches the network at all cannot succeed.

    Not covered here: `edges.retargeted_from_repo_id` stability across a re-scan. This test only
    checks the `edges` row COUNT is unchanged; the `retargeted_from_repo_id` value itself is never
    persisted at all (`state/repository.py`'s `insert_edges`/`EdgeRow` carry no such column/field),
    so its idempotency is untestable until D23 (`docs/INTEGRATION_HONESTY.md`) is fixed. See
    `docs/CRITERIA_PLAN.md` §23's "Remaining, genuinely unclosable by a test" note.
    """
    assert scan(fleet).exit_code == ExitCode.SUCCESS
    before = {
        table: query(fleet, f"SELECT COUNT(*) FROM {table}")[0][0]  # noqa: S608
        for table in ("repos", "manifests", "coordinates", "symbols", "edges", "phases", "findings")
    }
    assert before["findings"] > 0, "the empty repo's EmptyRepo finding must exist to be re-tested"
    heads = dict(query(fleet, "SELECT repo_id, head_sha FROM repos"))
    attempts = dict(query(fleet, "SELECT repo_id, attempts FROM phases WHERE phase = 1"))

    shutil.rmtree(fleet.parent / "sources")

    result = scan(fleet)
    assert result.exit_code == ExitCode.SUCCESS, result.output
    after = {
        table: query(fleet, f"SELECT COUNT(*) FROM {table}")[0][0]  # noqa: S608
        for table in before
    }
    assert after == before, "a re-scan duplicated rows"
    assert dict(query(fleet, "SELECT repo_id, head_sha FROM repos")) == heads
    assert dict(query(fleet, "SELECT repo_id, attempts FROM phases WHERE phase = 1")) == attempts


def test_a_second_sequence_leaves_waves_and_wave_members_unchanged(fleet: Path) -> None:
    """`fleet scan && fleet sequence && fleet sequence` (§12 item 23) is a no-op for `waves` and
    `wave_members`: same row counts, same wave assignment per node, the second time as the first.

    Why these two tables specifically: the earlier re-scan test above proves `edges`, `symbols`
    and `manifests` survive a repeated `fleet scan` untouched, but that test never calls
    `fleet sequence` — so it cannot see a defect where `assign_waves` or its persistence layer
    reruns and reassigns wave indices, or duplicates a `wave_members` row, on a plan that was
    already laid out. `waves`/`wave_members` are written only by `sequence`, so this is the table
    pair that test structurally cannot reach.
    """
    assert scan(fleet).exit_code == ExitCode.SUCCESS
    first = sequence(fleet)
    assert first.exit_code == ExitCode.SUCCESS, first.output
    before_counts = {
        table: query(fleet, f"SELECT COUNT(*) FROM {table}")[0][0]  # noqa: S608
        for table in ("waves", "wave_members")
    }
    assert before_counts["waves"] > 0 and before_counts["wave_members"] > 0, (
        "the fixture must really produce waves for a re-sequence to have anything to duplicate"
    )
    before_members = dict(
        query(fleet, "SELECT node_kind || ':' || node_id, wave_index FROM wave_members")
    )
    before_waves = query(fleet, "SELECT wave_index, max_usd FROM waves ORDER BY wave_index")

    second = sequence(fleet)
    assert second.exit_code == ExitCode.SUCCESS, second.output
    after_counts = {
        table: query(fleet, f"SELECT COUNT(*) FROM {table}")[0][0]  # noqa: S608
        for table in before_counts
    }
    assert after_counts == before_counts, "a re-sequence duplicated a wave or a wave member"
    assert (
        dict(query(fleet, "SELECT node_kind || ':' || node_id, wave_index FROM wave_members"))
        == before_members
    ), "a re-sequence reassigned a node to a different wave"
    assert (
        query(fleet, "SELECT wave_index, max_usd FROM waves ORDER BY wave_index") == before_waves
    )


def test_an_interrupted_scan_resumes_without_losing_completed_work(fleet: Path) -> None:
    """A fleet scanned in two halves — the second half interrupted mid-flight — ends complete,
    with the first half's rows untouched.

    The interruption is the real one: the process is killed while it holds a phase lease, which
    leaves a `RUNNING` row nobody will ever renew. Until §6's reaper reclaims it that repo is
    invisible to admission, so the failure this guards is a resumed run that reports success
    having quietly scanned 249 of 250 repos. What must survive is everything the first pass
    landed — no re-clone, no duplicated manifest, no reset attempt counter.
    """
    assert scan(fleet, "--only", "acme-lib-*").exit_code == ExitCode.SUCCESS
    landed = query(
        fleet, "SELECT repo_id, path, sha256 FROM manifests ORDER BY repo_id, path"
    )
    assert {row[0] for row in landed} == set(LIBRARIES)

    # The crash: `acme-app-ts` was claimed and the orchestrator died holding the lease.
    run_id = query(fleet, "SELECT run_id FROM runs")[0][0]
    long_ago = "2020-01-01T00:00:00.000000+00:00"
    conn = sqlite3.connect(fleet / "state" / "fleet.db", isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO repos (repo_id, name, url, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (repo_id) DO NOTHING",
            ("acme-app-ts", "acme-app-ts", "https://example.invalid/app", long_ago),
        )
        conn.execute(
            "INSERT INTO phases (run_id, repo_id, phase, status, lease_owner, lease_fence, "
            "                    lease_expires_at, heartbeat_at, updated_at) "
            "VALUES (?, 'acme-app-ts', 1, 'RUNNING', 'dead-host:dead:1:deadbeef', 1, ?, ?, ?)",
            (run_id, long_ago, long_ago, long_ago),
        )
    finally:
        conn.close()

    result = scan(fleet)
    assert result.exit_code == ExitCode.SUCCESS, result.output

    statuses = dict(query(fleet, "SELECT repo_id, status FROM phases WHERE phase = 1"))
    assert statuses["acme-app-ts"] == "SUCCEEDED", "the reaped lease was never re-admitted"
    assert all(
        statuses[name] == "SUCCEEDED" for name in (*LIBRARIES, *APPLICATIONS)
    ), statuses
    final = query(fleet, "SELECT repo_id, path, sha256 FROM manifests ORDER BY repo_id, path")
    assert set(landed) <= set(final), "the resume rewrote the first pass's manifests"
    assert len(final) == len(set(final)) == 4, f"a manifest was duplicated: {final}"

    # And the resumed fleet still sequences the right way round.
    waves = json.loads(sequence(fleet).stdout)["wave_index_by_repo"]
    assert waves["acme-lib-ts"] < waves["acme-app-ts"]


# ---------------------------------------------------------------------------------------
# outputs
# ---------------------------------------------------------------------------------------


def test_the_run_projects_a_migration_state_that_round_trips(fleet: Path) -> None:
    """`migration_state.json` is written and validates back into `MigrationState`.

    Why: §11.5 makes the projection the harness's public read model — `fleet status`, a resume
    and every operator dashboard read it, and a file that only *looks* like JSON is a run whose
    state cannot be recovered. Round-tripping through the model is the only check that catches an
    enum or a timestamp serialized in a shape the loader rejects.
    """
    assert scan(fleet).exit_code == ExitCode.SUCCESS
    projection = fleet / "migration_state.json"
    assert projection.exists(), "scan wrote no projection"

    state = MigrationState.model_validate_json(projection.read_text(encoding="utf-8"))
    assert set(state.repos) == set(FIXTURE_REPOS)
    assert str(state.run_id) == query(fleet, "SELECT run_id FROM runs")[0][0]
    assert state.repos["acme-empty"].status.value == "SKIPPED"
    assert state.repos["acme-lib-ts"].status.value == "SUCCEEDED"


def test_scan_refuses_the_flags_it_cannot_honour(fleet: Path) -> None:
    """`--refresh` and a foreign `--repos` are refused with exit 2, not accepted and ignored.

    Why this is a test and not a docstring: an accepted-and-ignored flag is indistinguishable
    from an honoured one at the exit code, which is the only thing CI reads. The same rule is
    what makes `--only` and `--skip-symbols` trustworthy — they are asserted to have an effect
    below, so the refusals are the other half of one property.
    """
    assert scan(fleet, "--refresh").exit_code == ExitCode.USAGE
    elsewhere = runner.invoke(
        app,
        [*base_args(fleet), "scan", "--skip-classify", "--repos", str(fleet / "other.yaml")],
        catch_exceptions=False,
    )
    assert elsewhere.exit_code == ExitCode.USAGE
    assert "repos.yaml" in elsewhere.output

    assert scan(fleet, "--skip-symbols", "--only", "acme-lib-ts").exit_code == ExitCode.SUCCESS
    assert query(fleet, "SELECT COUNT(*) FROM symbols") == [(0,)]
    assert query(fleet, "SELECT COUNT(*) FROM manifests") == [(1,)]


def test_preflight_only_is_refused_rather_than_completing_a_phase_it_did_not_finish(
    fleet: Path,
) -> None:
    """`--preflight-only` exits 2 and writes nothing, because the phase model cannot express it.

    Why it is refused and not implemented: `phases` holds one row per (run, repo, SCAN) and
    `PhaseRunner` re-dispatches its own `partial` hand-backs until that row is terminal, so a
    worker that stopped after step 1 is simply re-entered and finishes anyway. The version that
    "worked" — truncating the step list — completed the phase SUCCEEDED with no manifests and no
    symbols, and a SUCCEEDED phase is never re-admitted: the fleet could never be asked for them
    again. A refusal costs an operator one flag; that outcome costs them the run.
    """
    result = scan(fleet, "--preflight-only", "--only", "acme-lib-ts")
    assert result.exit_code == ExitCode.USAGE, result.output
    assert "SUCCEEDED phase is never re-admitted" in result.output
    assert query(fleet, "SELECT COUNT(*) FROM phases") == [(0,)]


def test_sequence_refuses_the_cycle_flags_it_cannot_thread(fleet: Path) -> None:
    """`--accept-breaks` / `--force-hoist` / `--forbid-hoist` / `--break-cycles manual` exit 2.

    Why: `break_cycles()` takes a `GraphSection` and nothing else, so there is no parameter for
    any of them. They were parsed and dropped, which is the worst of the three options — an
    operator who forbade a hoist and got one has been told by exit 0 that the harness agreed.
    `--max-hoists-per-scc` and `--scc-atomic-threshold` DO have keys on that section, so they are
    threaded rather than refused, and the run succeeds.
    """
    assert scan(fleet).exit_code == ExitCode.SUCCESS
    for flag in (
        ["--accept-breaks", "abc"],
        ["--force-hoist", "proto:acme"],
        ["--forbid-hoist", "proto:acme"],
        ["--break-cycles", "manual"],
    ):
        refused = sequence(fleet, *flag)
        assert refused.exit_code == ExitCode.USAGE, f"{flag} was silently accepted"

    honoured = sequence(fleet, "--max-hoists-per-scc", "1", "--scc-atomic-threshold", "3")
    assert honoured.exit_code == ExitCode.SUCCESS, honoured.output


# ---------------------------------------------------------------------------------------
# git-scale hazards: submodules and LFS are recorded, never a crash (§12 item 26)
# ---------------------------------------------------------------------------------------
#
# Each fixture below is its own one-repo fleet rather than an addition to `FIXTURE_REPOS`:
# folding a sixth repo into the five-repo fleet above would perturb the wave-ordering and
# table-count assertions every other test in this file makes. Kept separate, the two tests here
# read exactly like `test_an_empty_repo_is_skipped_with_a_finding_and_the_fleet_continues` above
# — a real git repo, driven through real `fleet scan`, asserted against the tables `clone.py`
# actually writes — for the two remaining §3.1 step 1 hazards that file's fixture never plants.


_LFS_POINTER = (
    "version https://git-lfs.github.com/spec/v1\n"
    "oid sha256:" + "0" * 64 + "\n"
    "size 123\n"
)
"""A real Git LFS pointer file's content — the small, fixed text format LFS substitutes for the
tracked blob in the repository proper. `CloneWorker._has_lfs` reads `.gitattributes`, never the
blob a pointer resolves to, so this is a complete fixture without the megabyte the pointer names."""


@pytest.fixture
def submodule_fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A workspace holding one real repo with a genuine submodule reference, and a fresh db."""
    target = _make_repo(tmp_path / "sources", "acme-submodule-target", {"README.md": "hi\n"})
    source = _make_repo_with_submodule(
        tmp_path / "sources", "acme-with-submodule", target, "vendor/sub"
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_config(workspace, {"acme-with-submodule": source}, names=["acme-with-submodule"])
    _fresh_db(workspace / "state" / "fleet.db")
    monkeypatch.chdir(workspace)
    yield workspace


def test_a_submodule_reference_is_recorded_and_the_repo_still_scans(
    submodule_fleet: Path,
) -> None:
    """A repo declaring a real submodule is not a crash, and it is not gated at all.

    `clone.py` never sets `_Preflight.gate` for a submodule — `SubmodulePresent` is a FINDING,
    published alongside `repos.submodule_count`, and the phase completes `SUCCEEDED`. Nothing
    here needs the submodule's own history to be reachable: `_materialize_worktree`'s
    `git worktree add` never initializes a gitlink on its own (that needs an explicit
    `git submodule update --init`, which this harness never runs), and `_submodule_count` only
    reads `.gitmodules`' text at `head_sha`.
    """
    result = scan(submodule_fleet)
    assert result.exit_code == ExitCode.SUCCESS, result.output

    statuses = dict(query(submodule_fleet, "SELECT repo_id, status FROM phases WHERE phase = 1"))
    assert statuses["acme-with-submodule"] == "SUCCEEDED"

    repos = dict(query(submodule_fleet, "SELECT repo_id, submodule_count FROM repos"))
    assert repos["acme-with-submodule"] == 1

    findings = query(
        submodule_fleet,
        "SELECT kind FROM findings WHERE repo_id = 'acme-with-submodule' ORDER BY kind",
    )
    assert ("SubmodulePresent",) in findings


@pytest.fixture
def lfs_fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A workspace holding one real repo declaring `filter=lfs` with a real pointer file."""
    source = _make_repo(
        tmp_path / "sources",
        "acme-with-lfs",
        {
            ".gitattributes": "*.bin filter=lfs diff=lfs merge=lfs -text\n",
            "model.bin": _LFS_POINTER,
        },
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_config(
        workspace, {"acme-with-lfs": source}, names=["acme-with-lfs"], fleet_yaml=LFS_FLEET_YAML
    )
    _fresh_db(workspace / "state" / "fleet.db")
    monkeypatch.chdir(workspace)
    yield workspace


def test_an_lfs_pointer_file_is_recorded_and_the_repo_still_scans(lfs_fleet: Path) -> None:
    """A repo declaring `filter=lfs` with a real pointer file is not a crash.

    `clone.py`'s `_has_lfs` reads only `.gitattributes`' text at `head_sha` — the pointer's own
    content is irrelevant to the probe, which is why a pointer file with no real object behind it
    is a complete fixture. With `require_lfs_binary: false` (`lfs_fleet`'s config) the presence
    check cannot become a gate, so this asserts what the probe measures and persists
    (`repos.has_lfs`), not the separate `require_lfs_binary` gate (`clone.py`'s
    `shutil.which("git-lfs")` check), which this fixture does not exercise.
    """
    result = scan(lfs_fleet)
    assert result.exit_code == ExitCode.SUCCESS, result.output

    statuses = dict(query(lfs_fleet, "SELECT repo_id, status FROM phases WHERE phase = 1"))
    assert statuses["acme-with-lfs"] == "SUCCEEDED"

    repos = dict(query(lfs_fleet, "SELECT repo_id, has_lfs FROM repos"))
    assert repos["acme-with-lfs"] == 1


# ---------------------------------------------------------------------------------------
# a non-`main` default branch is recorded, never a crash (§12 item 26, 5th fixture category)
# ---------------------------------------------------------------------------------------
#
# The submodule/LFS section above closed item 26's 3rd and 4th named hazards; this closes the
# 5th and last — "default-branched to `trunk`". Kept as its own one-repo fleet for the same
# reason: folding a sixth repo into `FIXTURE_REPOS` would perturb the wave-ordering assertions
# elsewhere in this file, and a fixture isolating this one variable (real commits, no submodule,
# no LFS, nothing empty) is what `docs/CRITERIA_PLAN.md` §12.26's done bar asks for.


@pytest.fixture
def trunk_fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A workspace holding one real repo whose default branch is `trunk`, not `main`."""
    source = _make_repo_with_default_branch(
        tmp_path / "sources", "acme-on-trunk", {"README.md": "hi\n"}, "trunk"
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_config(workspace, {"acme-on-trunk": source}, names=["acme-on-trunk"])
    _fresh_db(workspace / "state" / "fleet.db")
    monkeypatch.chdir(workspace)
    yield workspace


def test_a_trunk_default_branch_is_recorded_and_the_repo_still_scans(trunk_fleet: Path) -> None:
    """A repo default-branched to `trunk` is not a crash — item 26's 5th and last fixture
    category, and it exercises the PRIMARY resolution path, not the fallback list.

    `clone.py`'s `_default_branch` (`:495-507`) tries `git symbolic-ref --short HEAD` FIRST and
    only falls through to `payload.branch_fallbacks` — which is `("main", "master", "trunk",
    "develop")`, `trunk` included — if that symbolic ref does not resolve. This fixture's mirror
    has a genuine `HEAD` pointing at `refs/heads/trunk` (a real `git branch -m` rename of an
    actual commit, per `_make_repo_with_default_branch`), so its `symbolic-ref` resolves cleanly
    on the first try; the fallback tuple containing the same string is never consulted, and
    `default_branch_source` records that.
    """
    result = scan(trunk_fleet)
    assert result.exit_code == ExitCode.SUCCESS, result.output

    statuses = dict(query(trunk_fleet, "SELECT repo_id, status FROM phases WHERE phase = 1"))
    assert statuses["acme-on-trunk"] == "SUCCEEDED"

    repos = {
        row[0]: row
        for row in query(
            trunk_fleet, "SELECT repo_id, default_branch, default_branch_source FROM repos"
        )
    }
    assert repos["acme-on-trunk"] == ("acme-on-trunk", "trunk", "symbolic-ref")


# ---------------------------------------------------------------------------------------
# a contract vendored into nine repos: the scale case §12 item 23 names explicitly
# ---------------------------------------------------------------------------------------
#
# `state/schema.sql`'s own comment on `contracts` states the property in these terms — "nine
# repos vendoring one proto package => exactly one row" — and SPEC.md item 23 gives the same
# number. Every carrier below sits under `third_party/`, one of `scan.vendor_globs`' default
# patterns, so none is the "real" (non-vendored) owner; `workers/contracts.py::_owner`'s
# `eligible = [... not vendored ...] or list(sources)` fallback is exactly what makes a node with
# an owner still exist when EVERY carrier is a vendored copy, which is the shape the criterion
# names ("a contract vendored into nine fixture repos"), not nine-vendored-plus-one-canonical.

_VENDORED_PROTO = """\
syntax = "proto3";

package acme.vendored.v1;

message Widget {
  string id = 1;
}
"""

_VENDOR_REPO_NAMES = tuple(f"acme-vendor-{i:02d}" for i in range(1, 10))
"""Nine repos, `acme-vendor-01` .. `acme-vendor-09` — the scale SPEC item 23 names."""

VENDORED_CONTRACT_ID = "proto:acme.vendored.v1"


@pytest.fixture
def vendored_contract_fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Nine real git repos, each vendoring a byte-identical copy of the same `.proto` package
    under `third_party/`, plus a config bundle and a fresh db."""
    sources = {
        name: _make_repo(
            tmp_path / "sources",
            name,
            {
                "third_party/acme/vendored/v1/vendored.proto": _VENDORED_PROTO,
                "package.json": json.dumps({"name": f"@acme/{name}", "version": "1.0.0"}),
            },
        )
        for name in _VENDOR_REPO_NAMES
    }
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_config(workspace, sources, names=list(_VENDOR_REPO_NAMES))
    _fresh_db(workspace / "state" / "fleet.db")
    monkeypatch.chdir(workspace)
    yield workspace


def test_a_contract_vendored_into_nine_repos_stays_one_row_across_a_second_scan(
    vendored_contract_fleet: Path,
) -> None:
    """§12 item 23, literally: nine repos vendoring one `.proto` package produce exactly ONE
    `contracts` row, both after the first `fleet scan` and — unchanged — after a second.

    The `CONTRACT` collision this fleet also raises (nine claimants of one contract_id, §3.1 step
    8) is checked the same way, for the same reason: `audit_collisions`' `_contract_collisions`
    fires whenever `len(repo_ids) >= 2` for one `contract_id` regardless of vendored status, so
    nine identical vendored copies are a real, non-trivial collision row — not the zero-collision
    case the five-repo `fleet` fixture elsewhere in this file exercises. A defect that re-derives
    the graph on a second scan and, say, dropped or re-created either row would be invisible to a
    test built on a fixture with only one or two carriers, where "one row" is true almost by
    construction; nine independent carriers is what makes the identity key — not the carrier
    count — the thing actually under test.
    """
    first = scan(vendored_contract_fleet)
    assert first.exit_code == ExitCode.SUCCESS, first.output

    contracts_before = query(
        vendored_contract_fleet,
        "SELECT contract_id, owning_repo_id, content_sha256, extractable FROM contracts",
    )
    assert len(contracts_before) == 1, f"nine vendored copies produced {contracts_before}"
    contract_id, owner_before = contracts_before[0][0], contracts_before[0][1]
    assert contract_id == VENDORED_CONTRACT_ID
    assert owner_before in _VENDOR_REPO_NAMES, "the ladder must still crown SOME carrier as owner"

    collisions_before = query(
        vendored_contract_fleet, "SELECT kind, key, repo_ids, severity FROM collisions"
    )
    assert len(collisions_before) == 1, f"nine claimants produced {collisions_before}"
    kind, key, repo_ids_json, severity = collisions_before[0]
    assert (kind, key) == ("CONTRACT", VENDORED_CONTRACT_ID)
    assert sorted(json.loads(repo_ids_json)) == sorted(_VENDOR_REPO_NAMES)
    assert severity == "warn", "byte-identical vendored copies must not read as a real conflict"

    # Proved absent by DELETING the source repositories first, exactly as the five-repo re-scan
    # test above does: a re-scan that touches the network at all cannot succeed from here.
    shutil.rmtree(vendored_contract_fleet.parent / "sources")

    second = scan(vendored_contract_fleet)
    assert second.exit_code == ExitCode.SUCCESS, second.output

    contracts_after = query(
        vendored_contract_fleet,
        "SELECT contract_id, owning_repo_id, content_sha256, extractable FROM contracts",
    )
    assert contracts_after == contracts_before, "a re-scan changed the one vendored contract row"

    collisions_after = query(
        vendored_contract_fleet, "SELECT kind, key, repo_ids, severity FROM collisions"
    )
    assert collisions_after == collisions_before, "a re-scan changed the vendored collision row"


def test_a_degraded_repo_with_no_rhi_repo_exits_7(fleet: Path) -> None:
    """D93 / SPEC §3.5.1 point 5: a run with a `DEGRADED` repo and NO
    `REQUIRES_HUMAN_INTERVENTION` repo exits **7**, not 0 — the specific trigger D93 names,
    proved in isolation from the already-covered RHI trigger.

    `DEGRADED` does not arise naturally from a Phase 1 scan (it is a transform/build-time
    consequence of a stub, §3.5) — that mechanism is proved elsewhere — so this test writes the
    phase row directly, the same technique `crash_the_phase` uses in `test_transform_e2e.py`, to
    isolate the CLI's exit-code determination from how a repo comes to be `DEGRADED`. The re-scan
    is real (not a stub of the CLI) so it exercises the actual `cli.py::scan` code path that
    reads `phases` after the run and picks `ExitCode`.
    """
    assert scan(fleet).exit_code == ExitCode.SUCCESS
    before = dict(query(fleet, "SELECT repo_id, status FROM phases WHERE phase = 1"))
    assert "REQUIRES_HUMAN_INTERVENTION" not in before.values(), before
    victim = sorted(before)[0]

    conn = sqlite3.connect(fleet / "state" / "fleet.db", isolation_level=None)
    try:
        conn.execute(
            "UPDATE phases SET status = 'DEGRADED' WHERE repo_id = ? AND phase = 1",
            (victim,),
        )
    finally:
        conn.close()

    result = scan(fleet)
    assert result.exit_code == ExitCode.REQUIRES_HUMAN_INTERVENTION == 7, result.output

    after = dict(query(fleet, "SELECT repo_id, status FROM phases WHERE phase = 1"))
    assert after[victim] == "DEGRADED", after
    assert "REQUIRES_HUMAN_INTERVENTION" not in after.values(), after
