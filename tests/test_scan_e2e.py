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
  docker: 1
verify:
  container_memory: 64m
budgets:
  max_rss_mb: 512
preflight:
  # §11.3's floor is 50 GiB, which no developer machine — and no CI runner — clears with room to
  # spare, so a fixture that left it at the default would exit 9 before Phase 1 started. Lowered
  # rather than disabled, because 0 would mean "unchecked" and this fleet is four repos of a few
  # kilobytes: the gate really runs here, it simply passes. The refusal itself is asserted where
  # it belongs, against a floor no volume can clear (`test_cli.py`, `test_workers_scan.py`).
  min_free_bytes: 1048576
"""
"""§11.3/§12.22: `concurrency.docker`/`verify.container_memory`/`budgets.max_rss_mb` above are
lowered the same way `min_free_bytes` is -- the shipped defaults (4 x 8 GiB + 4 GiB = 36864 MiB)
breach `budgets.max_host_rss_mb` (12288) by design, and `_load_settings` now enforces
`validate_memory_budget` at startup. The refusal itself is asserted separately (`test_cli.py`)."""
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

    D23 companion (`_persist_scan_edges`'s own path, the common case): scan-time inference never
    retargets an edge — only `graph/cycles.py::_materialize`, run at `fleet sequence` time after a
    contract hoist, does that — so every row `_persist_scan_edges` writes must read back
    `retargeted_from_repo_id IS NULL`. Asserted here, not merely left implicit, so the fix that
    makes D23's retargeted case persist (see `test_sequence_e2e.py`) cannot be a regression that
    writes a bogus value into every ordinary edge instead.
    """
    assert scan(fleet).exit_code == ExitCode.SUCCESS

    rows = query(
        fleet,
        "SELECT src_id, dst_id, kind, evidence_path, confidence, retargeted_from_repo_id "
        "FROM edges WHERE dst_id IS NOT NULL ORDER BY src_id, dst_id",
    )
    declared = {(str(row[0]), str(row[1])) for row in rows}
    assert ("acme-app-ts", "acme-lib-ts") in declared
    assert ("acme-app-py", "acme-lib-py") in declared
    assert ("acme-lib-ts", "acme-app-ts") not in declared, "the edge is reversed"

    npm_edge = next(row for row in rows if row[0] == "acme-app-ts")
    assert npm_edge[3] == "package.json", "an edge must name the file that proves it"
    assert float(npm_edge[4]) >= 0.5, "a declared dependency below min_confidence orders nothing"

    assert rows, "fixture must produce at least one edge for this assertion to mean anything"
    assert all(row[5] is None for row in rows), (
        f"a scan-time edge must never carry a retarget provenance: {rows}"
    )

    # §3.5's admission key is derived from the same graph: the libraries carry a blast radius.
    radii = dict(query(fleet, "SELECT repo_id, blast_radius FROM repos"))
    assert radii["acme-lib-ts"] == 1
    assert radii["acme-app-ts"] == 0


# ---------------------------------------------------------------------------------------
# D114 (a)+(b): the file_blobs capture and §12 criterion 9(d)'s wiring
# ---------------------------------------------------------------------------------------


def test_scan_captures_file_blobs_and_sequence_resolves_every_edges_evidence_path(
    fleet: Path,
) -> None:
    """D114 (a): `fleet scan` persists `file_blobs` — a real per-repo `ls-tree` capture — and
    D114 (b): `fleet sequence`'s Phase 1 exit condition genuinely resolves criterion (d)
    (`graph/sequence.py::check_criterion_d`) against it, rather than falling back to
    `cli.py::_phase1_exit_report`'s old always-`True` default.

    This is the positive control for the discriminator test below: it proves the capture step
    ran and produced real rows, and that a genuinely resolving `edges.evidence_path` still passes
    `fleet sequence` once the vacuous default is gone.
    """
    assert scan(fleet).exit_code == ExitCode.SUCCESS

    file_blobs = {
        (str(repo_id), str(path)): str(blob_sha)
        for repo_id, path, blob_sha in query(
            fleet, "SELECT repo_id, path, blob_sha FROM file_blobs"
        )
    }
    assert ("acme-app-ts", "package.json") in file_blobs, "the manifest itself must be captured"
    assert ("acme-lib-ts", "package.json") in file_blobs
    assert ("acme-app-py", "pyproject.toml") in file_blobs
    assert all(len(sha) == 40 for sha in file_blobs.values()), "a real git blob SHA, not a stub"
    assert "acme-empty" not in {repo for repo, _ in file_blobs}, (
        "an empty repo (no head_sha) cut no worktree; capturing it would be a phantom entry"
    )

    result = sequence(fleet)
    assert result.exit_code == ExitCode.SUCCESS, result.output


def test_criterion_d_fails_a_real_sequence_when_an_edges_evidence_path_does_not_resolve(
    fleet: Path,
) -> None:
    """Rule-12 discriminator for D114 (b). Under the OLD `evidence_exists` default (always
    `True`, `graph/sequence.py::check_criteria`'s own fallback), `fleet sequence` exits 0
    regardless of what `edges.evidence_path` says — the exact vacuous behavior D114 (b) closes.
    `check_criterion_d` itself was already correct and already unit-tested
    (`tests/test_graph_sequence.py`); this test's job is proving the CALLER-SIDE WIRING is real:
    a real edge whose `evidence_path` is corrupted to a path never tracked at `head_sha` must now
    fail `fleet sequence` with exit 6 and name the specific edge — a case that would ALSO have
    silently passed under the old default.
    """
    assert scan(fleet).exit_code == ExitCode.SUCCESS

    conn = sqlite3.connect(fleet / "state" / "fleet.db", isolation_level=None)
    try:
        changed = conn.execute(
            "UPDATE edges SET evidence_path = 'nonexistent/ghost.json' "
            "WHERE src_id = 'acme-app-ts' AND dst_id = 'acme-lib-ts'"
        ).rowcount
    finally:
        conn.close()
    assert changed == 1, "the fixture's declared-dependency edge must exist to mutate"

    result = sequence(fleet)
    assert result.exit_code == ExitCode.UNRESOLVED_FINDINGS == 6, result.output
    assert "(d)" in result.output, result.output
    assert "acme-app-ts" in result.output, result.output
    assert "nonexistent/ghost.json" in result.output, result.output


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


# ---------------------------------------------------------------------------------------
# INTERNAL_IMPORT, end to end (§12 item 8 / §12.8's fixture-fleet done bar)
# ---------------------------------------------------------------------------------------
#
# `test_graph_build.py::test_an_undeclared_import_is_an_edge_but_a_declared_one_is_not_doubled`
# proves the same distinction at the unit level, against a hand-built `InferenceInput` that never
# touches a parser, a manifest adapter, or the `edges` table. Nothing in the suite proved that the
# real pieces agree: that `_python_symbols`' `ast`-derived `IMPORT` fqn, `PythonAdapter.publishes`'
# coordinate, and `OwnerIndex.match_import`'s token matching actually meet in the middle when
# driven through a real `fleet scan`. Kept as its own two-repo fleet for the same reason the three
# sections above are — folding it into `FIXTURE_REPOS` would perturb every wave-ordering and
# table-count assertion elsewhere in this file.
#
# Two repos only: `acme-shared-py` publishes a coordinate (`pyproject.toml`'s `[project].name`),
# `acme-consumer-py` imports it (`from acme_shared_py import helper`) via real Python source real
# `ast.parse` walks. `declare_dependency` toggles whether `acme-consumer-py`'s own manifest also
# lists it — the Rule 12 discriminator: the same import must produce `INTERNAL_IMPORT` when
# undeclared and `DECLARED_DEP` (never both) when declared.


def _make_internal_import_fleet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, declare_dependency: bool
) -> Path:
    consumer_deps = '["acme-shared-py>=1.0"]' if declare_dependency else "[]"
    sources = {
        "acme-shared-py": _make_repo(
            tmp_path / "sources",
            "acme-shared-py",
            {
                "pyproject.toml": (
                    "[project]\n"
                    'name = "acme-shared-py"\n'
                    'version = "1.0.0"\n'
                    "dependencies = []\n"
                ),
                "acme_shared_py/__init__.py": (
                    "def helper(name: str) -> str:\n"
                    "    return name.upper()\n"
                ),
            },
        ),
        "acme-consumer-py": _make_repo(
            tmp_path / "sources",
            "acme-consumer-py",
            {
                "pyproject.toml": (
                    "[project]\n"
                    'name = "acme-consumer-py"\n'
                    'version = "0.1.0"\n'
                    f"dependencies = {consumer_deps}\n"
                ),
                # No corresponding manifest entry when `declare_dependency` is False: exactly
                # the shape `_import_edges` (`graph/infer.py`) names — "an import whose module
                # prefix resolves to a coordinate owned by ANOTHER repo, with NO corresponding
                # manifest entry".
                "acme_consumer_py/main.py": (
                    "from acme_shared_py import helper\n"
                    "\n"
                    "\n"
                    "def run(name: str) -> str:\n"
                    "    return helper(name)\n"
                ),
            },
        ),
    }
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_config(workspace, sources, names=list(sources))
    _fresh_db(workspace / "state" / "fleet.db")
    monkeypatch.chdir(workspace)
    return workspace


@pytest.fixture
def internal_import_fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`acme-consumer-py` imports `acme-shared-py` with NO declared dependency."""
    return _make_internal_import_fleet(tmp_path, monkeypatch, declare_dependency=False)


@pytest.fixture
def internal_import_fleet_declared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The same two repos, but `acme-consumer-py` DECLARES the dependency it imports."""
    return _make_internal_import_fleet(tmp_path, monkeypatch, declare_dependency=True)


def test_an_undeclared_cross_repo_import_produces_a_real_internal_import_edge(
    internal_import_fleet: Path,
) -> None:
    """§12 item 8: "every known cross-repo edge is discovered including at least one
    `INTERNAL_IMPORT` with no corresponding manifest entry" — proven against a real `fleet scan`,
    not only the hand-built `InferenceInput` in `test_graph_build.py`.

    `acme-consumer-py/acme_consumer_py/main.py` line 1 imports `acme_shared_py`, the coordinate
    `acme-shared-py` publishes via its `pyproject.toml`, and `acme-consumer-py`'s own manifest
    declares no dependency on it at all — real `ast`-derived symbols, a real manifest adapter, a
    real `OwnerIndex` built from the real `coordinates` table, landing in the real `edges` table.
    """
    result = scan(internal_import_fleet)
    assert result.exit_code == ExitCode.SUCCESS, result.output

    edges = query(
        internal_import_fleet,
        "SELECT src_id, dst_id, kind, evidence_path, evidence_line FROM edges "
        "WHERE kind = 'INTERNAL_IMPORT'",
    )
    assert edges == [
        ("acme-consumer-py", "acme-shared-py", "INTERNAL_IMPORT", "acme_consumer_py/main.py", 1)
    ], edges


def test_the_owning_repos_published_version_lands_and_a_dependents_range_never_clobbers_it(
    internal_import_fleet_declared: Path,
) -> None:
    """§37 Blocker B: the value `ManifestAdapter.publishes(path)` already computes every scan was
    discarded before reaching `coordinates` — this proves it now lands, and proves the one
    distinction that matters: only the OWNING repo's own publish may set it.

    `acme-shared-py` publishes `pyproject.toml`'s `[project].version = "1.0.0"` — a real concrete
    version. `acme-consumer-py` DECLARES a dependency on that same coordinate at `>=1.0` — a
    RANGE, a different kind of value entirely. Scanned in TWO separate `--only` invocations, the
    owning repo FIRST and the dependent SECOND, so the dependent's write is the one that lands
    last against the `coordinates` row — the exact ordering that would expose a version write
    with no `owned` guard. The stored `coordinates.version` must be the publisher's `1.0.0`,
    never the dependent's range and never NULL.
    """
    first = scan(internal_import_fleet_declared, "--only", "acme-shared-py")
    assert first.exit_code == ExitCode.SUCCESS, first.output
    second = scan(internal_import_fleet_declared, "--only", "acme-consumer-py")
    assert second.exit_code == ExitCode.SUCCESS, second.output

    rows = query(
        internal_import_fleet_declared,
        "SELECT version FROM coordinates WHERE coord_key = 'pypi::acme-shared-py'",
    )
    assert rows == [("1.0.0",)], rows


def test_declaring_the_same_import_turns_it_into_a_declared_dep_not_an_internal_import(
    internal_import_fleet_declared: Path,
) -> None:
    """Rule 12: the fixture-fleet proof above is a genuine discriminator, not a vacuous one.

    Same two repos, same import — but `acme-consumer-py`'s manifest now declares
    `acme-shared-py>=1.0`. `_import_edges`'s manifest-entry check (`(sym.repo_id, coord.key) in
    declared`) must suppress the `INTERNAL_IMPORT` edge in favor of the `DECLARED_DEP` edge
    `_dependency_edges` emits for the same pair — never both, and never neither
    (`test_an_undeclared_import_is_an_edge_but_a_declared_one_is_not_doubled` in
    `test_graph_build.py` proves this transition at the unit level; this proves the real manifest
    adapter and real symbol extraction agree with it).
    """
    result = scan(internal_import_fleet_declared)
    assert result.exit_code == ExitCode.SUCCESS, result.output

    kinds = {
        row[0]
        for row in query(
            internal_import_fleet_declared,
            "SELECT kind FROM edges WHERE src_id = 'acme-consumer-py' "
            "AND dst_id = 'acme-shared-py'",
        )
    }
    assert kinds == {"DECLARED_DEP"}, kinds


# ---------------------------------------------------------------------------------------
# SHARED_RESOURCE, end to end (§12 item 8 / §12.8's fixture-fleet done bar, ADR-0109)
# ---------------------------------------------------------------------------------------
#
# `test_graph_build.py`'s `full_input()` proves SHARED_RESOURCE at the unit level: two hand-built
# `DB_TABLE` `SymbolRef`s sharing one fqn ("users") produce one advisory edge per ordered pair.
# Nothing proved the real regex extractor (`workers/symbolindex.py::_pattern_symbols`, the
# `db_table` pattern in `DEFAULT_RESOURCE_PATTERNS`) and the real `edges` table agree with that
# rule. Kept as its own two-repo fleet, same reason as INTERNAL_IMPORT above: folding a
# `CREATE TABLE` literal into `FIXTURE_REPOS` would perturb every wave-ordering and table-count
# assertion elsewhere in this file.
#
# Two repos with NO manifest relation: each ships one Python file whose text literally contains
# `CREATE TABLE <name> (...)`, matched by the `db_table` pattern (case-insensitive; no `.sql`
# extension needed — the pattern scans every indexed file's raw text regardless of language).
# `same_table` toggles whether the second repo's DDL names the SAME table — the Rule 12
# discriminator: identical shape, different table name, no edge.


def _make_shared_resource_fleet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, same_table: bool
) -> Path:
    other_table = "orders" if same_table else "invoices"
    sources = {
        "acme-orders-svc": _make_repo(
            tmp_path / "sources",
            "acme-orders-svc",
            {
                "pyproject.toml": (
                    "[project]\n"
                    'name = "acme-orders-svc"\n'
                    'version = "1.0.0"\n'
                    "dependencies = []\n"
                ),
                "acme_orders_svc/schema.py": (
                    'TABLE_DDL = "CREATE TABLE orders (id INTEGER PRIMARY KEY)"\n'
                ),
            },
        ),
        "acme-orders-report": _make_repo(
            tmp_path / "sources",
            "acme-orders-report",
            {
                "pyproject.toml": (
                    "[project]\n"
                    'name = "acme-orders-report"\n'
                    'version = "1.0.0"\n'
                    "dependencies = []\n"
                ),
                "acme_orders_report/schema.py": (
                    f'TABLE_DDL = "CREATE TABLE {other_table} (id INTEGER PRIMARY KEY)"\n'
                ),
            },
        ),
    }
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_config(workspace, sources, names=list(sources))
    _fresh_db(workspace / "state" / "fleet.db")
    monkeypatch.chdir(workspace)
    return workspace


@pytest.fixture
def shared_resource_fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Both repos' DDL name the same table (`orders`)."""
    return _make_shared_resource_fleet(tmp_path, monkeypatch, same_table=True)


@pytest.fixture
def shared_resource_fleet_distinct(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The same two repos, but the second repo's DDL names a DIFFERENT table."""
    return _make_shared_resource_fleet(tmp_path, monkeypatch, same_table=False)


def test_two_repos_naming_the_same_table_produce_a_real_shared_resource_edge(
    shared_resource_fleet: Path,
) -> None:
    """§12 item 8: SHARED_RESOURCE ("one table/topic/queue named by two repos") proven against a
    real `fleet scan`, not only the hand-built `InferenceInput` in `test_graph_build.py`.

    Both `acme-orders-svc/acme_orders_svc/schema.py` and
    `acme-orders-report/acme_orders_report/schema.py` line 1 name `orders` via the real
    `db_table` regex — no `.sql` extension, no manifest declaration on either side. The relation
    is symmetric and written as one row per ordered pair (`infer.py`'s own docstring), so both
    directions must land, each with `confidence == EDGE_BASE_CONFIDENCE[SHARED_RESOURCE] == 0.5`
    (no vendor/generated modifier applies to either path).
    """
    result = scan(shared_resource_fleet)
    assert result.exit_code == ExitCode.SUCCESS, result.output

    edges = sorted(
        query(
            shared_resource_fleet,
            "SELECT src_id, dst_id, kind, evidence_path, evidence_line, confidence "
            "FROM edges WHERE kind = 'SHARED_RESOURCE'",
        )
    )
    assert edges == [
        (
            "acme-orders-report", "acme-orders-svc", "SHARED_RESOURCE",
            "acme_orders_report/schema.py", 1, 0.5,
        ),
        (
            "acme-orders-svc", "acme-orders-report", "SHARED_RESOURCE",
            "acme_orders_svc/schema.py", 1, 0.5,
        ),
    ], edges


def test_naming_a_different_table_produces_no_shared_resource_edge(
    shared_resource_fleet_distinct: Path,
) -> None:
    """Rule 12: the fixture-fleet proof above is a genuine discriminator, not a vacuous one.

    Same two repos, same shape of DDL literal — but the second repo's table is `invoices`, not
    `orders`. `_shared_resource_edges`'s `len(repos) < 2` check, keyed per fqn, must then see
    only one repo per table name and emit nothing.
    """
    result = scan(shared_resource_fleet_distinct)
    assert result.exit_code == ExitCode.SUCCESS, result.output

    edges = query(
        shared_resource_fleet_distinct, "SELECT kind FROM edges WHERE kind = 'SHARED_RESOURCE'"
    )
    assert edges == [], edges


# ---------------------------------------------------------------------------------------
# DYNAMIC_REF, end to end (§12 item 8 / §12.8's fixture-fleet done bar, ADR-0109)
# ---------------------------------------------------------------------------------------
#
# `test_graph_build.py`'s `full_input()` proves DYNAMIC_REF at the unit level with a hand-built
# `SymbolRef(kind=SymbolKind.DYNAMIC_REF, ...)`. Nothing proved the real `py_importlib` regex
# (`workers/symbolindex.py::DEFAULT_DYNAMIC_PATTERNS`) actually produces one, nor that
# `_dynamic_ref_edges`'s FALLBACK path — resolving via `defined` (a real symbol definition)
# rather than via `OwnerIndex.match_import` (a published coordinate, the path INTERNAL_IMPORT
# and API_CONTRACT both use) — fires for real. The FQN below is deliberately chosen so NO
# published coordinate's token prefix matches it (`match_import` returns `None`), which forces
# the fallback branch: this is the case the docstring calls "a reference the compiler cannot
# see", not a second INTERNAL_IMPORT wearing a different `EdgeKind`.
#
# Two repos: `acme-dynref-lib` defines a real Python function under `internal/registry.py`
# (`ast`-derived fqn `internal.registry.build_widget`, unrelated to its own coordinate's tokens
# `acme`/`dynref`/`lib`); `acme-dynref-app` never `import`s it — only via
# `importlib.import_module("internal.registry.build_widget")`, a string literal an `ast.Import`
# walk cannot see, which is exactly why this fires as DYNAMIC_REF and not INTERNAL_IMPORT.


def _make_dynamic_ref_fleet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, target: str
) -> Path:
    sources = {
        "acme-dynref-lib": _make_repo(
            tmp_path / "sources",
            "acme-dynref-lib",
            {
                "pyproject.toml": (
                    "[project]\n"
                    'name = "acme-dynref-lib"\n'
                    'version = "1.0.0"\n'
                    "dependencies = []\n"
                ),
                "internal/registry.py": (
                    "def build_widget(name: str) -> str:\n"
                    "    return name\n"
                ),
            },
        ),
        "acme-dynref-app": _make_repo(
            tmp_path / "sources",
            "acme-dynref-app",
            {
                "pyproject.toml": (
                    "[project]\n"
                    'name = "acme-dynref-app"\n'
                    'version = "1.0.0"\n'
                    "dependencies = []\n"
                ),
                "acme_dynref_app/loader.py": (
                    "import importlib\n"
                    "\n"
                    "\n"
                    "def load() -> object:\n"
                    f'    return importlib.import_module("{target}")\n'
                ),
            },
        ),
    }
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_config(workspace, sources, names=list(sources))
    _fresh_db(workspace / "state" / "fleet.db")
    monkeypatch.chdir(workspace)
    return workspace


@pytest.fixture
def dynamic_ref_fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`acme-dynref-app` dynamically imports the exact FQN `acme-dynref-lib` defines."""
    return _make_dynamic_ref_fleet(tmp_path, monkeypatch, target="internal.registry.build_widget")


@pytest.fixture
def dynamic_ref_fleet_unresolvable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The same two repos, but the dynamic string names a function nobody defines."""
    return _make_dynamic_ref_fleet(
        tmp_path, monkeypatch, target="internal.registry.missing_widget"
    )


def test_a_string_built_import_of_a_real_symbol_produces_a_real_dynamic_ref_edge(
    dynamic_ref_fleet: Path,
) -> None:
    """§12 item 8: DYNAMIC_REF ("a reference the compiler cannot see") proven against a real
    `fleet scan`, not only the hand-built `InferenceInput` in `test_graph_build.py`.

    `acme-dynref-app/acme_dynref_app/loader.py` line 5 names
    `internal.registry.build_widget` inside `importlib.import_module(...)` — no `ast.Import`
    node, no published coordinate whose token prefix matches it, so `_dynamic_ref_edges` must
    take the `defined`-symbol fallback — and `acme-dynref-lib/internal/registry.py` line 1 is
    the real `ast`-derived definition it resolves to, with
    `confidence == EDGE_BASE_CONFIDENCE[DYNAMIC_REF] == 0.3`.
    """
    result = scan(dynamic_ref_fleet)
    assert result.exit_code == ExitCode.SUCCESS, result.output

    edges = query(
        dynamic_ref_fleet,
        "SELECT src_id, dst_id, kind, evidence_path, evidence_line, confidence FROM edges "
        "WHERE kind = 'DYNAMIC_REF'",
    )
    assert edges == [
        (
            "acme-dynref-app", "acme-dynref-lib", "DYNAMIC_REF",
            "acme_dynref_app/loader.py", 5, 0.3,
        )
    ], edges


def test_a_dynamic_reference_to_nothing_defined_produces_no_dynamic_ref_edge(
    dynamic_ref_fleet_unresolvable: Path,
) -> None:
    """Rule 12: the fixture-fleet proof above is a genuine discriminator, not a vacuous one.

    Same two repos, same `importlib.import_module(...)` shape — but the string now names
    `internal.registry.missing_widget`, which nothing defines and no coordinate's prefix
    matches. `_dynamic_ref_edges`'s `defined.get(sym.fqn, ())` must come back empty and emit
    nothing.
    """
    result = scan(dynamic_ref_fleet_unresolvable)
    assert result.exit_code == ExitCode.SUCCESS, result.output

    edges = query(
        dynamic_ref_fleet_unresolvable, "SELECT kind FROM edges WHERE kind = 'DYNAMIC_REF'"
    )
    assert edges == [], edges


# ---------------------------------------------------------------------------------------
# API_CONTRACT, end to end (§12 item 8's last EdgeKind, ADR-0111's own gRPC-consumer research)
# ---------------------------------------------------------------------------------------
#
# `_api_contract_edges` (`graph/infer.py`) requires, for the same FQN, an `is_definition=True`
# GRPC_SERVICE/PROTO_MESSAGE symbol in one repo and an `is_definition=False` symbol of the same
# FQN in another. `_proto_symbols` (`workers/symbolindex.py`) only ever emits the DEFINITION
# side — it parses `.proto` declarations, never a consumer's reference to an already-generated
# stub — so nothing produced the reference side until `scan.api_contract_patterns` extended the
# existing `_pattern_symbols` mechanism (the same one `SHARED_RESOURCE`/`DYNAMIC_REF` already
# use, §12 item 8's DYNAMIC_REF section above) with a pattern over a gRPC stub's wire-level RPC
# path, `/package.Service/Method` — the one string literal every generated gRPC client emits
# verbatim regardless of target language, because it is the HTTP/2 path gRPC's OWN wire protocol
# uses, not a per-language codegen convention.

_GRPC_WIDGET_PROTO = """\
syntax = "proto3";

package acme.widgets.v1;

service WidgetService {
}
"""


@pytest.fixture
def api_contract_fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`acme-grpc-provider` defines the `.proto` service; `acme-grpc-consumer` never sees the
    `.proto` file at all — only a hand-written stand-in for a generated `_pb2_grpc.py` client,
    carrying the wire path literal `/acme.widgets.v1.WidgetService/GetWidget` a real `protoc`
    gRPC plugin emits into `channel.unary_unary(...)` calls."""
    sources = {
        "acme-grpc-provider": _make_repo(
            tmp_path / "sources",
            "acme-grpc-provider",
            {
                "api/widgets.proto": _GRPC_WIDGET_PROTO,
                "pyproject.toml": (
                    "[project]\n"
                    'name = "acme-grpc-provider"\n'
                    'version = "1.0.0"\n'
                    "dependencies = []\n"
                ),
            },
        ),
        "acme-grpc-consumer": _make_repo(
            tmp_path / "sources",
            "acme-grpc-consumer",
            {
                "pyproject.toml": (
                    "[project]\n"
                    'name = "acme-grpc-consumer"\n'
                    'version = "1.0.0"\n'
                    "dependencies = []\n"
                ),
                "acme_grpc_consumer/widgets_pb2_grpc.py": (
                    "class WidgetServiceStub(object):\n"
                    "    def __init__(self, channel):\n"
                    "        self.GetWidget = channel.unary_unary(\n"
                    "                '/acme.widgets.v1.WidgetService/GetWidget',\n"
                    "                )\n"
                ),
            },
        ),
    }
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_config(workspace, sources, names=list(sources))
    _fresh_db(workspace / "state" / "fleet.db")
    monkeypatch.chdir(workspace)
    return workspace


def test_a_grpc_stub_wire_path_produces_a_real_api_contract_edge(
    api_contract_fleet: Path,
) -> None:
    """§12 item 8: API_CONTRACT proven against a real `fleet scan`, not only the hand-built
    `InferenceInput` in `test_graph_build.py`.

    `acme-grpc-consumer/acme_grpc_consumer/widgets_pb2_grpc.py` line 4 names
    `/acme.widgets.v1.WidgetService/GetWidget` inside a `channel.unary_unary(...)` call — the
    `scan.api_contract_patterns` `grpc_method_path` regex captures `acme.widgets.v1.WidgetService`
    as a `GRPC_SERVICE` reference (`is_definition=False`), and
    `acme-grpc-provider/api/widgets.proto` line 3's `service WidgetService {}` under `package
    acme.widgets.v1;` is the real `_proto_symbols`-derived definition (`is_definition=True`) it
    resolves to.
    """
    result = scan(api_contract_fleet)
    assert result.exit_code == ExitCode.SUCCESS, result.output

    edges = query(
        api_contract_fleet,
        "SELECT src_id, dst_id, kind, evidence_path, evidence_line FROM edges "
        "WHERE kind = 'API_CONTRACT'",
    )
    assert edges == [
        (
            "acme-grpc-consumer",
            "acme-grpc-provider",
            "API_CONTRACT",
            "acme_grpc_consumer/widgets_pb2_grpc.py",
            4,
        )
    ], edges


def test_a_grpc_stub_naming_a_different_service_produces_no_api_contract_edge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule 12: the fixture-fleet proof above is a genuine discriminator, not a vacuous one.

    Same two repos, same wire-path shape — but the consumer's stub names `OtherService`, which
    `acme-grpc-provider`'s `.proto` never defines. `_api_contract_edges`'s `definitions.get(sym.fqn,
    [])` must come back empty for that FQN and emit nothing.
    """
    sources = {
        "acme-grpc-provider": _make_repo(
            tmp_path / "sources",
            "acme-grpc-provider",
            {
                "api/widgets.proto": _GRPC_WIDGET_PROTO,
                "pyproject.toml": (
                    "[project]\n"
                    'name = "acme-grpc-provider"\n'
                    'version = "1.0.0"\n'
                    "dependencies = []\n"
                ),
            },
        ),
        "acme-grpc-consumer": _make_repo(
            tmp_path / "sources",
            "acme-grpc-consumer",
            {
                "pyproject.toml": (
                    "[project]\n"
                    'name = "acme-grpc-consumer"\n'
                    'version = "1.0.0"\n'
                    "dependencies = []\n"
                ),
                "acme_grpc_consumer/other_pb2_grpc.py": (
                    "class OtherServiceStub(object):\n"
                    "    def __init__(self, channel):\n"
                    "        self.GetOther = channel.unary_unary(\n"
                    "                '/acme.widgets.v1.OtherService/GetOther',\n"
                    "                )\n"
                ),
            },
        ),
    }
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_config(workspace, sources, names=list(sources))
    _fresh_db(workspace / "state" / "fleet.db")
    monkeypatch.chdir(workspace)

    result = scan(workspace)
    assert result.exit_code == ExitCode.SUCCESS, result.output

    edges = query(workspace, "SELECT kind FROM edges WHERE kind = 'API_CONTRACT'")
    assert edges == [], edges


# ---------------------------------------------------------------------------------------
# API_CONTRACT, HTTP_OPERATION side (§12 item 8's LAST gap, round VI research-29 +
# CRITERIA_PLAN §8's revised done bar)
# ---------------------------------------------------------------------------------------
#
# The gRPC block above closed the reference (consumer) side for GRPC_SERVICE; HTTP_OPERATION was
# the sole remaining EdgeKind gap because nothing ever produced either side of it: `_proto_symbols`
# never runs over YAML/JSON OpenAPI documents, and `_pattern_symbols` had no pattern for an HTTP
# path template. `_openapi_path_symbols` (workers/symbolindex.py) now extracts the DEFINITION side
# from an OpenAPI document's `paths:` block (gated on the document's own `openapi:`/`swagger:`
# root key, per `_is_openapi_document`), and `DEFAULT_API_CONTRACT_PATTERNS["http_path_template"]`
# extracts the REFERENCE side from a generated-client-shaped literal — research-29's real
# `openapi-generator-cli` run found this exact literal, brace placeholders included, survives
# byte-identical across python/typescript-fetch/typescript-axios and matches the spec's own
# `paths:` key, which is what makes a single FQN-equality join (unmodified `_api_contract_edges`)
# honest here.

_OPENAPI_WIDGET_SPEC = """\
openapi: 3.0.0
info:
  title: Widget API
  version: 1.0.0
paths:
  /widgets/{widgetId}:
    get:
      operationId: getWidget
      responses:
        '200':
          description: OK
  /widgets:
    post:
      operationId: createWidget
      responses:
        '201':
          description: Created
"""


@pytest.fixture
def http_contract_fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`acme-http-provider` defines the OpenAPI spec; `acme-http-consumer` never sees the spec
    file at all — only a hand-written stand-in for an `openapi-generator-cli` python client's
    `resource_path = '/widgets/{widgetId}'` line (research-29's own generated-output evidence)."""
    sources = {
        "acme-http-provider": _make_repo(
            tmp_path / "sources",
            "acme-http-provider",
            {
                "openapi/widgets.yaml": _OPENAPI_WIDGET_SPEC,
                "pyproject.toml": (
                    "[project]\n"
                    'name = "acme-http-provider"\n'
                    'version = "1.0.0"\n'
                    "dependencies = []\n"
                ),
            },
        ),
        "acme-http-consumer": _make_repo(
            tmp_path / "sources",
            "acme-http-consumer",
            {
                "pyproject.toml": (
                    "[project]\n"
                    'name = "acme-http-consumer"\n'
                    'version = "1.0.0"\n'
                    "dependencies = []\n"
                ),
                "acme_http_consumer/default_api.py": (
                    "class DefaultApi:\n"
                    "    def get_widget(self, widget_id, **kwargs):\n"
                    "        resource_path = '/widgets/{widgetId}'\n"
                    "        return self._call(resource_path)\n"
                ),
            },
        ),
    }
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_config(workspace, sources, names=list(sources))
    _fresh_db(workspace / "state" / "fleet.db")
    monkeypatch.chdir(workspace)
    return workspace


def test_an_openapi_path_template_produces_a_real_api_contract_edge(
    http_contract_fleet: Path,
) -> None:
    """§12 item 8's last `EdgeKind`: API_CONTRACT proven for HTTP_OPERATION against a real
    `fleet scan`, not only a hand-built `InferenceInput`.

    `acme-http-consumer/acme_http_consumer/default_api.py` line 3 names
    `/widgets/{widgetId}` inside a plain string literal — `http_path_template` captures it as an
    `HTTP_OPERATION` reference (`is_definition=False`), and
    `acme-http-provider/openapi/widgets.yaml` line 6's `/widgets/{widgetId}:` key under the
    document's `openapi:` root is `_openapi_path_symbols`' real DEFINITION
    (`is_definition=True`) it resolves to.
    """
    result = scan(http_contract_fleet)
    assert result.exit_code == ExitCode.SUCCESS, result.output

    edges = query(
        http_contract_fleet,
        "SELECT src_id, dst_id, kind, evidence_path, evidence_line FROM edges "
        "WHERE kind = 'API_CONTRACT'",
    )
    assert edges == [
        (
            "acme-http-consumer",
            "acme-http-provider",
            "API_CONTRACT",
            "acme_http_consumer/default_api.py",
            3,
        )
    ], edges


def test_an_openapi_consumer_naming_an_undefined_path_produces_no_api_contract_edge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule 12: the fixture-fleet proof above is a genuine discriminator, not a vacuous one.

    Same two repos, same `http_path_template` shape — but the consumer's literal is
    `/gadgets/{gadgetId}`, which `acme-http-provider`'s spec never defines.
    `_api_contract_edges`'s `definitions.get(sym.fqn, [])` must come back empty for that FQN and
    emit nothing.
    """
    sources = {
        "acme-http-provider": _make_repo(
            tmp_path / "sources",
            "acme-http-provider",
            {
                "openapi/widgets.yaml": _OPENAPI_WIDGET_SPEC,
                "pyproject.toml": (
                    "[project]\n"
                    'name = "acme-http-provider"\n'
                    'version = "1.0.0"\n'
                    "dependencies = []\n"
                ),
            },
        ),
        "acme-http-consumer": _make_repo(
            tmp_path / "sources",
            "acme-http-consumer",
            {
                "pyproject.toml": (
                    "[project]\n"
                    'name = "acme-http-consumer"\n'
                    'version = "1.0.0"\n'
                    "dependencies = []\n"
                ),
                "acme_http_consumer/other_api.py": (
                    "class OtherApi:\n"
                    "    def get_gadget(self, gadget_id, **kwargs):\n"
                    "        resource_path = '/gadgets/{gadgetId}'\n"
                    "        return self._call(resource_path)\n"
                ),
            },
        ),
    }
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_config(workspace, sources, names=list(sources))
    _fresh_db(workspace / "state" / "fleet.db")
    monkeypatch.chdir(workspace)

    result = scan(workspace)
    assert result.exit_code == ExitCode.SUCCESS, result.output

    edges = query(workspace, "SELECT kind FROM edges WHERE kind = 'API_CONTRACT'")
    assert edges == [], edges


def test_an_fstring_path_literal_does_not_produce_a_spurious_api_contract_edge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule 12's REQUIRED false-positive fixture (CRITERIA_PLAN §8's revised done bar): a bare
    `/`-prefixed string is nowhere near as distinctive as gRPC's dotted-FQN-then-bare-method
    shape, so `http_path_template` must not fire on the realistic false positive CRITERIA_PLAN §8
    names — an ordinary dynamic log/file-path literal that merely LOOKS like a path template.

    The consumer file below logs an f-string, `f"/widgets/{widgetId}"`, that is BYTE-IDENTICAL to
    `acme-http-provider`'s real defined path `/widgets/{widgetId}` — deliberately, so that if
    `http_path_template` did not exclude f-strings this WOULD resolve against a real definition
    and produce a genuine (spurious) edge. This is what makes the fixture a real discriminator
    rather than one that merely differs by FQN: reverting `http_path_template`'s
    `(?<![fFrRbB])` lookbehind reddens exactly this test while leaving
    `test_an_openapi_path_template_produces_a_real_api_contract_edge` (a plain, non-f-string
    literal) green.
    """
    sources = {
        "acme-http-provider": _make_repo(
            tmp_path / "sources",
            "acme-http-provider",
            {
                "openapi/widgets.yaml": _OPENAPI_WIDGET_SPEC,
                "pyproject.toml": (
                    "[project]\n"
                    'name = "acme-http-provider"\n'
                    'version = "1.0.0"\n'
                    "dependencies = []\n"
                ),
            },
        ),
        "acme-http-fp-consumer": _make_repo(
            tmp_path / "sources",
            "acme-http-fp-consumer",
            {
                "pyproject.toml": (
                    "[project]\n"
                    'name = "acme-http-fp-consumer"\n'
                    'version = "1.0.0"\n'
                    "dependencies = []\n"
                ),
                "acme_http_fp_consumer/jobs.py": (
                    "import logging\n"
                    "\n"
                    "logger = logging.getLogger(__name__)\n"
                    "\n"
                    "\n"
                    "def handle_widget(widgetId):\n"
                    '    logger.info(f"/widgets/{widgetId}")\n'
                ),
            },
        ),
    }
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_config(workspace, sources, names=list(sources))
    _fresh_db(workspace / "state" / "fleet.db")
    monkeypatch.chdir(workspace)

    result = scan(workspace)
    assert result.exit_code == ExitCode.SUCCESS, result.output

    edges = query(workspace, "SELECT kind FROM edges WHERE kind = 'API_CONTRACT'")
    assert edges == [], edges


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
