"""Phase 2 end to end, through the real CLI, over real git repositories (§3.2, §12).

`tests/test_scan_e2e.py` proved Phase 1's composition; this file proves Phase 2's, over the same
five real temporary repositories, and it is the first test that drives
`fleet scan` → `fleet sequence` → `fleet transform` all the way to commits on `migrate/<repo>`.
What is unproven until it runs is not any single worker — `tests/test_workers_transform.py`
covers those against real git — but whether the CLI, the `PhaseRunner`, the scheduler, the
registry and the two transform workers agree about *who computes the relocation plan*, *which
anchor the guard is scoped to*, and *who is allowed to write a status*.

**No rewrite engine is installed on this host** — `ast-grep`, `libcst` and `ts-morph` are all
absent (ADR-0006 names all three; none is a dependency). The engine is therefore injected the way
§9 already provides for: `transform.engines` is a `name → module` map, `config/rules/*.yml` names
the engine, and this file writes a real `Rewriter` module and puts it on `sys.path`. Everything
downstream of it — the rule loader, `RewritePipeline`, the diff, `git apply`, the trailered commit
— is the shipped code. The model path is dropped by `--deterministic-only`, which caps the ladder
at the one rung that composes no prompt, so "no model call" is a property of the command line
rather than of a mock (exactly as `--skip-classify` is for scan).

Each test says why it matters. Together:

* one commit per task on `migrate/<repo>`, each carrying its `Fleet-Patch-Id` — the trailers are
  what turn "did my work land?" into a git query from a bare clone (ADR-0024);
* the rewrite changed the file it claimed to, asserted on the WORKTREE, because a database that
  records a rewrite nobody performed is the failure an end-to-end test exists to catch;
* re-running the phase over an already-transformed branch adds nothing, and the two-condition
  guard (§3.2 step 6.1) is what makes it so;
* a repo whose transform fails costs exactly itself: its siblings finish and the wave closes;
* §3.2's success criterion is checked against git for every repo the run calls `SUCCEEDED` — and
  the one clause that needs an engine (the parse probe) is reported as NOT RUN rather than
  silently counted as a pass;
* a crash that left commits on the branch and no row to show for it resumes without redoing them.
"""

from __future__ import annotations

import importlib
import json
import sqlite3
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from fleet.cli import ExitCode, app
from fleet.llm.client import LlmError
from tests.test_cli import MODELS_YAML
from tests.test_scan_e2e import FIXTURE_REPOS, _fresh_db, _make_repo

runner = CliRunner()

#: repo → its monorepo destination. §3.3's `layout(repo)` cannot compute one on this checkout —
#: `bazel/layout.py` reads `monorepo_dir`/`path_tail` off a registered `EcosystemAdapter` and
#: `src/fleet/ecosystems/` does not exist — so the fleet manifest carries `dest:` explicitly,
#: which is the override §3.3 gives precedence to anyway.
DESTINATIONS: dict[str, str] = {
    "acme-lib-ts": "ts/acme/lib",
    "acme-app-ts": "ts/acme/app",
    "acme-lib-py": "py/acme_lib_py",
    "acme-app-py": "py/acme_app_py",
}

FLEET_YAML = """\
run:
  monorepo_path: ../acme-monorepo
  cache_dir: cache/
  work_dir: work/
graph:
  hoist_contracts: false
concurrency:
  cpu_pool_workers: 1
transform:
  rules_dir: config/rules
  engines:
    fixture: {engine_module}
preflight:
  # §11.3's floor is 50 GiB, which no developer machine — and no CI runner — clears with room to
  # spare, so a fixture that left it at the default would exit 9 before Phase 1 started. Lowered
  # rather than disabled, because 0 would mean "unchecked" and this fleet is four repos of a few
  # kilobytes: the gate really runs here, it simply passes. The refusal itself is asserted where
  # it belongs, against a floor no volume can clear (`test_cli.py`, `test_workers_scan.py`).
  min_free_bytes: 1048576
"""

#: A real `Rewriter` (§7.4): pure `source` → patch, no worktree read, no tool on PATH. It is a
#: search/replace driven by the rule's own params, with `{{dest_path}}`/`{{repo_id}}` rendered
#: through the shipped `render_template`, so the run params really do reach the engine.
ENGINE_MODULE = '''\
"""An in-process rewrite engine for the Phase 2 end-to-end test (§7.4, §9 transform.engines)."""

from __future__ import annotations

from pathlib import Path

from fleet.models.enums import TransformTier
from fleet.models.tasks import FilePatch
from fleet.rewrite.apply import make_unified_diff
from fleet.rewrite.rules import EngineUnavailableError, RewriteRule, render_template

PROBE_AVAILABLE = {probe_available}


class FixtureRewriter:
    engine = "fixture"

    async def apply(
        self, rule: RewriteRule, path: str, source: str, params: dict[str, str]
    ) -> FilePatch | None:
        find = render_template(params["find"], params)
        replace = render_template(params["replace"], params)
        rewritten = source.replace(find, replace)
        diff = make_unified_diff(path, source, rewritten)
        if not diff:
            return None
        return FilePatch(
            path=path,
            diff=diff,
            tier=TransformTier.DETERMINISTIC,
            parse_probe_ok=False,
            rule_id=rule.id,
        )

    async def parse_probe(self, path: str) -> bool:
        if not PROBE_AVAILABLE:
            raise EngineUnavailableError(
                "rewrite engine 'fixture' cannot probe: no grammar binary on PATH"
            )
        text = Path(path).read_text(encoding="utf-8")
        return text.count("{{") == text.count("}}")


REWRITER = FixtureRewriter()
'''

#: The one rewrite this fleet needs, and the shape of a cross-repo import rewrite in general:
#: the specifier stays a **module specifier**.
#:
#: This rule used to replace `'@acme/lib'` with `'//ts/acme/lib:lib'`, and that produced the only
#: red repo in the real-Bazel build for two rounds: `ts/acme/app/src/main.ts(1,29): error TS2307:
#: Cannot find module '//ts/acme/lib:lib' or its corresponding type declarations`. A Bazel label
#: is not a module specifier and no TypeScript resolver has ever resolved one — labels describe
#: the §3.3 dependency edge, one phase later, in `BuildTarget.deps`, and `tsc` never reads them.
#:
#: What the rule rewrites now is a real breakage and a language-legal one: the deep specifier
#: `'@acme/lib/src/index'` reaches into the dependency's source layout, and the package the
#: monorepo links is the one `JsAdapter` publishes — so the import is re-pointed at the package
#: **entry**, which `JsAdapter.import_specifier` says is `@acme/lib` before the migration and
#: `@acme/lib` after it. The result is not its own input, so the pipeline's second pass is a
#: no-op rather than an annotation appended once per pass.
#:
#: All THREE run params render, so a params map that never reached the engine would fail the rule
#: with a `KeyError` instead of silently writing `{{dest_path}}` into a source file — and
#: `{{import_specifier}}` is the adapter-derived fact whose absence left a rule author with only
#: a path and an id to point a cross-repo import at.
TS_IMPORT_RULE = """\
rules:
  - id: ts-monorepo-import
    description: record the migration on a cross-repo import whose specifier survives it
    engine: fixture
    languages: [typescript]
    applies_to: ["**/src/main.ts"]
    rule:
      pattern: "'@acme/lib/src/index'"
    params:
      find: "'@acme/lib/src/index'"
      replace: "'@acme/lib' /* fleet: {{repo_id}} -> {{dest_path}} as {{import_specifier}} */"
"""

#: A rule that CLAIMS a file and then changes nothing — §5's `RULE_MISS`. Deliberately not a
#: crash: it is what an operator's incomplete rule set looks like, and it is the failure the
#: fleet has to survive one repo at a time.
PY_MISSING_RULE = """\
rules:
  - id: py-import-rewrite
    description: claims the python entrypoint and matches nothing in it
    engine: fixture
    languages: [python]
    applies_to: ["**/acme_app_py/main.py"]
    rule:
      pattern: "import acme_lib_py.nonexistent"
    params:
      find: "from acme_lib_py.nonexistent import missing"
      replace: "from acme.lib import missing"
"""


# ---------------------------------------------------------------------------------------
# fixture fleet
# ---------------------------------------------------------------------------------------


def _write_config(root: Path, sources: dict[str, Path], *, engine_module: str) -> None:
    config = root / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "fleet.yaml").write_text(
        FLEET_YAML.format(engine_module=engine_module), encoding="utf-8"
    )
    (config / "models.yaml").write_text(MODELS_YAML, encoding="utf-8")
    # D21/§11.4: `redaction.history_scrub_file` defaults to `config/rules/secrets.txt` and
    # `resolve_replace_text` (vcs/filter_repo.py) refuses to proceed with a non-empty configured
    # path that is not on disk — the same rule production `config/` satisfies. A fixture that
    # left this unprovisioned would make every test reaching Phase 3 ingest raise
    # `HistoryScrubUnavailableError` the moment `cli.py`'s `_ingest_build_source` started passing
    # `replace_text` through. One inert regex entry is enough to be real without asserting
    # anything about its content — nothing here is a real secret.
    rules_dir = config / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    (rules_dir / "secrets.txt").write_text(
        "regex:-----BEGIN [A-Z ]*PRIVATE KEY-----==>***REDACTED:private_key***\n",
        encoding="utf-8",
    )
    entries = "".join(
        f"  - name: {name}\n    url: {sources[name]}\n"
        + (f"    dest: {DESTINATIONS[name]}\n" if name in DESTINATIONS else "")
        for name in FIXTURE_REPOS
    )
    (config / "repos.yaml").write_text(
        f"version: 1\ndefaults:\n  ref: main\nrepos:\n{entries}", encoding="utf-8"
    )


def _write_engine(root: Path, name: str, *, probe_available: bool) -> None:
    (root / f"{name}.py").write_text(
        ENGINE_MODULE.format(probe_available=probe_available), encoding="utf-8"
    )


def write_rules(root: Path, *bodies: str) -> None:
    """Replace `config/rules/` with exactly these rule files (rules are DATA, §7.4)."""
    rules_dir = root / "config" / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    for existing in rules_dir.glob("*.yml"):
        existing.unlink()
    for index, body in enumerate(bodies):
        (rules_dir / f"r{index}.yml").write_text(body, encoding="utf-8")


@pytest.fixture
def fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Five real git repos, a config bundle with `dest:` per repo, a rules file, and an engine.

    The engine module is written into the workspace and the workspace is put on `sys.path`: that
    IS the §9 injection seam (`transform.engines` is a name → module map), so nothing here
    monkeypatches a worker or a pipeline.
    """
    sources = {
        name: _make_repo(tmp_path / "sources", name, files)
        for name, files in FIXTURE_REPOS.items()
    }
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_config(workspace, sources, engine_module="fleet_fixture_engine")
    _write_engine(workspace, "fleet_fixture_engine", probe_available=True)
    _write_engine(workspace, "fleet_fixture_blind_engine", probe_available=False)
    write_rules(workspace, TS_IMPORT_RULE)
    _fresh_db(workspace / "state" / "fleet.db")
    monkeypatch.chdir(workspace)
    monkeypatch.syspath_prepend(str(workspace))
    importlib.invalidate_caches()
    for module in ("fleet_fixture_engine", "fleet_fixture_blind_engine"):
        monkeypatch.delitem(__import__("sys").modules, module, raising=False)
    yield workspace


def base_args(root: Path) -> list[str]:
    return ["--config", str(root / "config" / "fleet.yaml"), "--db", str(root / "state/fleet.db")]


def scan(root: Path, *extra: str) -> Any:
    return runner.invoke(
        app, [*base_args(root), "scan", "--skip-classify", *extra], catch_exceptions=False
    )


def sequence(root: Path, *extra: str) -> Any:
    return runner.invoke(app, [*base_args(root), "sequence", *extra], catch_exceptions=False)


def transform(root: Path, *extra: str, json_output: bool = True) -> Any:
    argv = [*base_args(root)]
    if json_output:
        argv.append("--json")
    argv += ["transform", "--deterministic-only", *extra]
    return runner.invoke(app, argv, catch_exceptions=False)


def scanned(root: Path) -> None:
    """Phase 1 and the wave plan, both asserted — a transform test that starts from a broken
    scan would report Phase 2's failure for Phase 1's reason."""
    assert scan(root).exit_code == ExitCode.SUCCESS
    assert sequence(root).exit_code == ExitCode.SUCCESS


def query(root: Path, sql: str, params: tuple[object, ...] = ()) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(root / "state" / "fleet.db")
    try:
        return [tuple(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def payload(result: Any) -> dict[str, Any]:
    return dict(json.loads(result.stdout))


# ---------------------------------------------------------------------------------------
# git helpers — the branch is the record, so every assertion reads it
# ---------------------------------------------------------------------------------------


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), *args],  # noqa: S607 - `git` from PATH, as every suite does
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout.strip()


def worktree(root: Path, repo_id: str) -> Path:
    return root / "work" / repo_id


def anchor_of(root: Path, repo_id: str) -> str:
    rows = query(
        root,
        "SELECT pre_commit_sha FROM phases WHERE repo_id = ? AND phase = 2",
        (repo_id,),
    )
    assert rows and rows[0][0], f"{repo_id} has no phase-2 anchor"
    return str(rows[0][0])


def commits_on_branch(root: Path, repo_id: str) -> list[dict[str, str]]:
    """Every commit the phase added to `migrate/<repo>`, oldest first, with git-parsed trailers."""
    fmt = (
        "%H%x1f%(trailers:key=Fleet-Patch-Id,valueonly,separator=%x2c)"
        "%x1f%(trailers:key=Fleet-Task-Id,valueonly,separator=%x2c)"
        "%x1f%(trailers:key=Fleet-Run-Id,valueonly,separator=%x2c)%x1f%s%x1e"
    )
    tree = worktree(root, repo_id)
    raw = git(tree, "log", f"--format={fmt}", f"{anchor_of(root, repo_id)}..migrate/{repo_id}")
    entries: list[dict[str, str]] = []
    for record in raw.split("\x1e"):
        body = record.strip()
        if not body:
            continue
        sha, patch_id, task_id, run_id, subject = body.split("\x1f")
        entries.append(
            {
                "sha": sha,
                "patch_id": patch_id.strip(),
                "task_id": task_id.strip(),
                "run_id": run_id.strip(),
                "subject": subject.strip(),
            }
        )
    return list(reversed(entries))


def crash_the_phase(root: Path, repo_id: str) -> None:
    """Put the phase row back where a `SIGKILL` mid-dispatch would leave it: `RUNNING`, holding a
    lease nobody will renew, with no attempt charged (§3.2 step 6.4 — "no attempt is consumed").

    The same technique `test_scan_e2e` uses, and for the same reason: the interruption under test
    is a *state*, not a signal, and reproducing it exactly is what makes the resume deterministic.
    """
    long_ago = "2020-01-01T00:00:00.000000+00:00"
    conn = sqlite3.connect(root / "state" / "fleet.db", isolation_level=None)
    try:
        conn.execute(
            "UPDATE phases SET status = 'RUNNING', attempts = 0, "
            "    lease_owner = 'dead-host:dead:1:deadbeef', lease_expires_at = ?, "
            "    heartbeat_at = ?, updated_at = ? "
            " WHERE repo_id = ? AND phase = 2",
            (long_ago, long_ago, long_ago, repo_id),
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------------------
# 1. the commits
# ---------------------------------------------------------------------------------------


def test_transform_lands_one_trailered_commit_per_task(fleet: Path) -> None:
    """`scan` → `sequence` → `transform` exits 0 and `migrate/<repo>` holds ONE commit per task,
    each carrying its `Fleet-Patch-Id`.

    Why the trailers and not a table: ADR-0024 made the commit the durable record, so "did this
    task land?" is a git query answerable from a bare clone with the database deleted. A phase
    that wrote rows and squashed its work into one untrailered commit would satisfy every SQL
    assertion in the suite and be unrecoverable after a crash.
    """
    scanned(fleet)
    result = transform(fleet)
    assert result.exit_code == ExitCode.SUCCESS, result.output

    statuses = dict(query(fleet, "SELECT repo_id, status FROM phases WHERE phase = 2"))
    assert set(statuses) == set(DESTINATIONS), "every sequenced repo needs a phase-2 row"
    assert set(statuses.values()) == {"SUCCEEDED"}, statuses

    run_id = str(query(fleet, "SELECT run_id FROM runs")[0][0])
    for repo_id in DESTINATIONS:
        commits = commits_on_branch(fleet, repo_id)
        expected = len(FIXTURE_REPOS[repo_id]) + (1 if repo_id == "acme-app-ts" else 0)
        assert len(commits) == expected, f"{repo_id}: {[c['subject'] for c in commits]}"
        assert all(len(c["patch_id"]) == 64 for c in commits), commits
        assert all(c["run_id"] == run_id for c in commits), commits
        task_ids = [c["task_id"] for c in commits]
        assert len(set(task_ids)) == len(task_ids), f"{repo_id}: two tasks share a Fleet-Task-Id"

    # The pointers §3.2 step 6.3 requires SQLite to hold — and nothing more than pointers.
    tips = dict(query(fleet, "SELECT repo_id, post_commit_sha FROM phases WHERE phase = 2"))
    for repo_id in DESTINATIONS:
        assert tips[repo_id] == git(worktree(fleet, repo_id), "rev-parse", f"migrate/{repo_id}")


def test_the_rewrite_changed_the_file_it_claimed_to(fleet: Path) -> None:
    """The rewritten file on disk really carries the rewrite, at its relocated path.

    Why on the worktree and not on `attempts`: every layer between the rule and the file — the
    pipeline's buffer, the unified diff, `git apply --index`, the commit — can succeed against a
    tree nobody changed. The database would then record a migration that did not happen, which is
    precisely what an end-to-end test is for.
    """
    scanned(fleet)
    assert transform(fleet).exit_code == ExitCode.SUCCESS

    tree = worktree(fleet, "acme-app-ts")
    moved = tree / "ts/acme/app/src/main.ts"
    assert moved.is_file(), "the relocation never reached the worktree"
    assert not (tree / "src/main.ts").exists(), "the source path survived the rename"

    body = moved.read_text(encoding="utf-8")
    # Every run param rendered: a params map that never reached the engine would have failed the
    # rule with a KeyError rather than writing a literal placeholder into the source.
    assert "fleet: acme-app-ts -> ts/acme/app as @acme/app" in body, body
    # The import is still an import. `//` would mean a Bazel label had been written into a
    # TypeScript source, which is the defect that kept `acme-app-ts` red under real Bazel: `tsc`
    # answers a label with "TS2307: Cannot find module".
    assert "from '@acme/lib'" in body, body
    assert "//ts/acme/lib" not in body, "a Bazel label was written into a source file"

    # ... and the manifest moved untouched: a relocation is a rename, not a rewrite.
    package = tree / "ts/acme/app/package.json"
    assert json.loads(package.read_text(encoding="utf-8"))["name"] == "@acme/app"


# ---------------------------------------------------------------------------------------
# 2. idempotency — the two-condition guard
# ---------------------------------------------------------------------------------------


def test_re_running_transform_over_a_landed_branch_duplicates_nothing(fleet: Path) -> None:
    """A second transform of an already-transformed repo adds NO commit, and the §3.2 step 6.1
    guard is what stops it.

    The guard is two conditions and both are exercised here: the relocation is skipped by
    `git apply --check --reverse` (the file is already at its destination and gone from its
    source), and the rewrite is skipped by the `Fleet-Task-Id` trailer found in
    `<phases.pre_commit_sha>..migrate/<repo>` — a rule that now produces no diff is otherwise
    indistinguishable from a `RULE_MISS`, and the ladder would spend a rung repairing a file that
    is already correct.

    The phase rows are put back to `RUNNING` first, because a `SUCCEEDED` phase is never
    re-admitted: without that the run would prove only that the scheduler skipped the repo, which
    is not the property under test.
    """
    scanned(fleet)
    assert transform(fleet).exit_code == ExitCode.SUCCESS
    before = {repo: commits_on_branch(fleet, repo) for repo in DESTINATIONS}

    for repo_id in DESTINATIONS:
        crash_the_phase(fleet, repo_id)
    result = transform(fleet)
    assert result.exit_code == ExitCode.SUCCESS, result.output

    after = {repo: commits_on_branch(fleet, repo) for repo in DESTINATIONS}
    assert after == before, "a re-run duplicated commits on migrate/<repo>"
    for repo_id, commits in after.items():
        ids = [commit["patch_id"] for commit in commits]
        assert len(set(ids)) == len(ids), f"{repo_id}: a Fleet-Patch-Id was committed twice"

    # The `already_applied` event of §3.2 step 6.1 is recorded, not merely implied by silence.
    applied = query(
        fleet,
        "SELECT repo_id FROM attempts WHERE phase = 2 AND already_applied = 1 ORDER BY repo_id",
    )
    assert {row[0] for row in applied} == set(DESTINATIONS), applied

    # And a plain re-invocation (no surgery) is a no-op too: every wave is settled.
    plain = transform(fleet)
    assert plain.exit_code == ExitCode.SUCCESS, plain.output
    assert payload(plain)["waves"] == []
    assert {repo: commits_on_branch(fleet, repo) for repo in DESTINATIONS} == before


def test_a_crash_mid_transform_resumes_without_redoing_landed_commits(fleet: Path) -> None:
    """Commits on the branch with no row to show for them are RECONCILED, never re-applied.

    The interruption is the real one §3.2 step 6.4 describes: the relocation commits landed and
    the process died before the rewrite, so git says "done" for work SQLite has no record of.
    Resume must ask git — the landed commits keep their SHAs, exactly the missing work is
    performed, and no attempt is consumed for the part that had already landed. Re-applying
    instead would double the tree's history and hand Phase 3 a branch whose diff no longer
    describes the repo.
    """
    write_rules(fleet)  # the first pass is a pure relocation: no rule set had been authored yet
    scanned(fleet)
    assert transform(fleet).exit_code == ExitCode.SUCCESS
    landed = commits_on_branch(fleet, "acme-app-ts")
    assert len(landed) == 2, landed
    assert all(commit["subject"].startswith("fleet(relocate)") for commit in landed)

    write_rules(fleet, TS_IMPORT_RULE)  # ... and now the rewrite this fleet actually needs
    crash_the_phase(fleet, "acme-app-ts")

    result = transform(fleet)
    assert result.exit_code == ExitCode.SUCCESS, result.output

    resumed = commits_on_branch(fleet, "acme-app-ts")
    assert [c["sha"] for c in resumed[: len(landed)]] == [c["sha"] for c in landed], (
        "the resume rewrote history that had already landed"
    )
    assert len(resumed) == len(landed) + 1, [c["subject"] for c in resumed]
    assert resumed[-1]["subject"].startswith("fleet(rewrite)")
    assert "as @acme/app */" in (
        worktree(fleet, "acme-app-ts") / "ts/acme/app/src/main.ts"
    ).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------------------
# 3. per-repo isolation (§11.1) on a real path
# ---------------------------------------------------------------------------------------


def test_a_failing_repo_does_not_stop_its_siblings_or_the_wave(fleet: Path) -> None:
    """One repo ends `REQUIRES_HUMAN_INTERVENTION`; its wave-mate finishes and the wave closes.

    Why it matters more here than under fake workers: this failure travels the whole real path —
    a rule claims a file, produces no change, `RewriteWorker` reports `RULE_MISS`, `RetryPolicy`
    finds the ladder spent and terminates the repo — and every one of those steps runs inside the
    same `TaskGroup` as the sibling. §11.1's isolation is the property that one latent bug in one
    repo must not discard the paid-for work of the other 249.

    The failing repo also KEEPS the commits it landed before it failed: they are on the branch
    whatever the row says, and rolling them back would be the phase anchor being used as a task
    anchor (§3.2 step 6.5).
    """
    write_rules(fleet, TS_IMPORT_RULE, PY_MISSING_RULE)
    scanned(fleet)
    result = transform(fleet)
    assert result.exit_code == ExitCode.REQUIRES_HUMAN_INTERVENTION, result.output

    statuses = dict(query(fleet, "SELECT repo_id, status FROM phases WHERE phase = 2"))
    assert statuses["acme-app-py"] == "REQUIRES_HUMAN_INTERVENTION", statuses
    for survivor in ("acme-lib-ts", "acme-lib-py", "acme-app-ts"):
        assert statuses[survivor] == "SUCCEEDED", statuses

    # The wave closed: every member is settled, so the run did not stall on the failure.
    assert set(statuses.values()) <= {"SUCCEEDED", "REQUIRES_HUMAN_INTERVENTION"}
    failure = query(
        fleet,
        "SELECT failure_class, last_error FROM phases WHERE repo_id = 'acme-app-py' AND phase = 2",
    )
    assert failure[0][0] == "RULE_MISS", failure
    assert "acme_app_py/main.py" in str(failure[0][1]), failure

    landed = commits_on_branch(fleet, "acme-app-py")
    assert len(landed) == 2, "the failing repo lost the relocation commits it had landed"
    assert len(commits_on_branch(fleet, "acme-app-ts")) == 3


# ---------------------------------------------------------------------------------------
# 3b. disk headroom that develops MID-WAVE (D96 phase-2 half, §11.3)
# ---------------------------------------------------------------------------------------


def test_relocate_refuses_a_repo_when_disk_pressure_develops_after_the_phase_entry_gate(
    fleet: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The phase-ENTRY gate (`_require_disk_headroom`, fixed round CC) is a single reading taken
    once, before the wave starts. `RelocateWorker` must re-check per repo (D96's other half) —
    otherwise a volume that had room at wave start and fills up by repo 40 sails every later repo
    straight into `ENOSPC` inside a `BEGIN IMMEDIATE` (§13 row 42).

    This is proven by making the two readings disagree, not by raising the configured floor
    (which would ALSO trip the phase-entry gate and prove nothing about the per-repo check):
    `scan`/`sequence` run first, against the real, unpatched `fleet.util.fs.free_bytes` — so
    Phase 1 succeeds. THEN, and only then, `fleet.util.fs.free_bytes` is patched to report 0
    bytes free everywhere. `transform`'s own phase-entry gate (`cli.py`'s `_gc_disk` calls
    `free_bytes` through its own `from fleet.util.fs import free_bytes as disk_free_bytes`
    binding, captured at import time — a SEPARATE name from the one this patch replaces) still
    reads the real, plentiful disk and passes. `RelocateWorker.run` calls `require_free_space`,
    which is defined in `fleet.util.fs` and resolves `free_bytes` through THAT module's own
    globals at call time — so it sees the patched 0 — and refuses before `ctx.workdir` in the
    worktree the relocation would have written to.

    A `min_free_bytes` this low (`FLEET_YAML`'s `1048576`) would never refuse an unpatched real
    disk, which is what makes the phase-entry pass, and never proves the per-repo check with a
    genuinely full volume — the patch is standing in for the disk filling up between the two
    readings, exactly as an operator's host would between wave start and repo 40.
    """
    scanned(fleet)

    import fleet.util.fs as fs_module

    monkeypatch.setattr(fs_module, "free_bytes", lambda _path: 0)

    result = transform(fleet)

    assert result.exit_code == ExitCode.DISK_EXHAUSTED == 9, result.output
    assert "min_free_bytes" in result.output, result.output
    assert "1048576" in result.output, result.output
    # Pinned to RELOCATE specifically, not merely "some Phase 2 worker's disk check fired": if
    # RelocateWorker's own check were the one silently missing, its writes would go through — a
    # real 0-byte-free volume cannot actually receive them, but this test does not fill the real
    # disk, it only makes `free_bytes` REPORT 0 — so `land_patches` would still succeed here, and
    # the run would proceed to `RewriteWorker`, whose OWN check (unaffected by this test's
    # mutation) would fire instead and still exit 9 with the SAME failure_class, silently
    # certifying a defective relocate.py green. "relocate to" is `RelocateWorker`'s own operation
    # string (`relocate.py`'s `require_free_space(..., operation=f"relocate to {dest_path}")`);
    # `rewrite.py` and `buildgen.py` each use their own, disjoint operation strings.
    assert "relocate to" in result.output, result.output
    assert payload(result)["commits"] == 0, (
        "a commit landed before the refusal — the check ran too late, or a DIFFERENT worker's "
        "check (e.g. rewrite's) caught it instead of relocate's own"
    )

    failures = query(
        fleet,
        "SELECT repo_id, failure_class, last_error FROM phases "
        "WHERE phase = 2 AND failure_class IS NOT NULL",
    )
    assert failures, "no phase-2 row recorded the disk-exhausted failure"
    assert all(failure_class == "DISK_EXHAUSTED" for _, failure_class, _ in failures), failures
    assert any("relocate to" in str(detail) for _, _, detail in failures), failures

    # Nothing from Phase 2 reached git for the repo(s) the halt caught: the refusal fired BEFORE
    # `scoped_tempdir`/`land_patches`, not after a partial move.
    for repo_id in DESTINATIONS:
        statuses = dict(query(fleet, "SELECT repo_id, status FROM phases WHERE phase = 2"))
        assert statuses.get(repo_id) != "SUCCEEDED", (
            f"{repo_id}: reached SUCCEEDED after the volume was reported full"
        )


# ---------------------------------------------------------------------------------------
# 4. the phase's success criterion (§3.2)
# ---------------------------------------------------------------------------------------


def test_the_success_criterion_holds_for_every_transformed_repo(fleet: Path) -> None:
    """§3.2's criterion is CHECKED against git, not assumed: a non-empty diff against the
    pre-transform tree, every changed path under `<dest>/`, no unresolved file, and a parse probe
    that exits 0 for every rewritten file.

    Why the driver checks it and the test checks the driver: a phase that marks itself
    `SUCCEEDED` on a tree it did not change hands Phase 3 a branch to rewrite into monorepo
    history. The criterion is the last point at which that is cheap to notice, and exit 6 is what
    it costs.
    """
    scanned(fleet)
    result = transform(fleet)
    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert payload(result)["parse_probe_unavailable"] == [], (
        "the injected engine can probe, so the criterion's fourth clause must have RUN"
    )

    for repo_id, dest in DESTINATIONS.items():
        tree = worktree(fleet, repo_id)
        anchor = anchor_of(fleet, repo_id)
        changed = git(tree, "diff", "--name-status", f"{anchor}..migrate/{repo_id}")
        assert changed, f"{repo_id}: the phase changed nothing"

        # Deletions outside `<dest>/` are legitimate only for the paths the relocation plan
        # moved; everything the phase WROTE must be under `<dest>/`. Git's own rename pairing
        # cannot answer this — a four-line file whose import line changed scores 47% similarity,
        # under `-M`'s 50% default — so the pre-transform tree is the authority.
        before = set(git(tree, "ls-tree", "-r", "--name-only", anchor).splitlines())
        for line in changed.splitlines():
            status, _, rest = line.partition("\t")
            paths = rest.split("\t")
            if status.startswith("D"):
                assert paths[0] in before, f"{repo_id}: deleted {paths[0]}, which it never had"
                continue
            assert paths[-1].startswith(f"{dest}/"), f"{repo_id}: wrote {paths[-1]}"


def test_the_parse_probe_is_reported_as_not_run_when_no_engine_can_run_it(
    fleet: Path,
) -> None:
    """With an engine that cannot probe, the run still succeeds — and SAYS the probe did not run.

    This is the honest form of a gap: `ast-grep`, `libcst` and `ts-morph` are all absent on this
    host, so §3.2 step 4's probe is unavailable in the shipped configuration too. "The probe
    returned 0" and "the probe never ran" are different facts about a rewritten file, and
    collapsing them would let a corrupt rewrite ship as a verified one. The other three clauses
    of the criterion are still enforced, so this is a narrowed check, not a skipped one.
    """
    config = fleet / "config" / "fleet.yaml"
    config.write_text(
        FLEET_YAML.format(engine_module="fleet_fixture_blind_engine"), encoding="utf-8"
    )
    scanned(fleet)
    result = transform(fleet)
    assert result.exit_code == ExitCode.SUCCESS, result.output

    unprobed = list(payload(result)["parse_probe_unavailable"])
    assert len(unprobed) == 1, unprobed
    assert unprobed[0].startswith("acme-app-ts: "), unprobed
    assert "no grammar binary on PATH" in unprobed[0]

    # And it is loud on the human path too, not only in `--json`.
    for repo_id in DESTINATIONS:
        crash_the_phase(fleet, repo_id)
    human = transform(fleet, json_output=False)
    assert human.exit_code == ExitCode.SUCCESS, human.output
    assert "parse probe DID NOT RUN" in human.output


#: An engine whose probe RAN but produced no verdict — the D37 shape ADR-0067 exists for: killed
#: at its deadline, never started, or an exit code that is neither pass nor fail.
#: `EngineUnavailableError` cannot stand in for this: `ensure_available()` never even gets a
#: chance to answer for a tool that IS present and DID run, which is exactly the distinction
#: §3.2 step 4 needs.
INDETERMINATE_ENGINE_MODULE = '''\
"""An engine whose probe RAN but produced no verdict (ADR-0067, D37 e2e proof)."""

from __future__ import annotations

from pathlib import Path

from fleet.models.enums import TransformTier
from fleet.models.tasks import FilePatch
from fleet.rewrite.apply import make_unified_diff
from fleet.rewrite.rules import ProbeIndeterminateError, RewriteRule, render_template


class FixtureRewriter:
    engine = "fixture"

    async def apply(
        self, rule: RewriteRule, path: str, source: str, params: dict[str, str]
    ) -> FilePatch | None:
        find = render_template(params["find"], params)
        replace = render_template(params["replace"], params)
        rewritten = source.replace(find, replace)
        diff = make_unified_diff(path, source, rewritten)
        if not diff:
            return None
        return FilePatch(
            path=path,
            diff=diff,
            tier=TransformTier.DETERMINISTIC,
            parse_probe_ok=False,
            rule_id=rule.id,
        )

    async def parse_probe(self, path: str) -> bool:
        raise ProbeIndeterminateError(
            f"parse probe for {Path(path).name!r} ran but produced no verdict "
            "(killed at its deadline)"
        )


REWRITER = FixtureRewriter()
'''


def test_an_indeterminate_probe_blocks_the_run_unlike_a_genuinely_missing_engine(
    fleet: Path,
) -> None:
    """ADR-0067 (D37), the defect this ADR exists to close: a probe that RAN but produced no
    verdict must fail the run — exit 6, `§3.2`'s success criterion does not hold — where the
    previous test's genuinely-missing-engine case exits SUCCESS with only a warning. Collapsing
    the two into one `EngineUnavailableError` bucket is exactly what let a corrupt rewrite ship
    with a green exit code before this ADR.
    """
    (fleet / "fleet_fixture_indeterminate_engine.py").write_text(
        INDETERMINATE_ENGINE_MODULE, encoding="utf-8"
    )
    config = fleet / "config" / "fleet.yaml"
    config.write_text(
        FLEET_YAML.format(engine_module="fleet_fixture_indeterminate_engine"), encoding="utf-8"
    )
    scanned(fleet)

    result = transform(fleet, json_output=False)
    assert result.exit_code == ExitCode.UNRESOLVED_FINDINGS, result.output
    assert "produced no verdict" in result.output
    assert "acme-app-ts" in result.output
    # And it is NOT reported as the non-blocking "engine unavailable" warning — the two must stay
    # visibly distinct outcomes, not the same message with a different exit code.
    assert "no rewrite engine is installed" not in result.output


# ---------------------------------------------------------------------------------------
# 5. the flags
# ---------------------------------------------------------------------------------------


def test_transform_refuses_the_flags_it_cannot_honour(fleet: Path) -> None:
    """`--stub-blocked` and `--no-anchoring-guard` exit 2 rather than parsing and doing nothing.

    Why a test and not a docstring: an accepted-and-ignored flag is indistinguishable from an
    honoured one at the exit code, which is all CI reads. An operator who disabled the anchoring
    guard and got no change has been told by exit 0 that the harness agreed with them.

    `--context-policy` USED to be a third flag here — closed (§12.35): it now genuinely reaches
    the worker (`cli._apply_context_policy_overrides` → `FleetSettings.config.transform.ladder`
    → `orchestrator/runner.py::_drive`'s `LadderState` → `BaseWorker.execute`), so
    `test_context_policy_reaches_the_worker_not_the_hardcoded_default` below is its discriminating
    proof instead of a refusal.
    """
    scanned(fleet)
    for flag in (
        ["--stub-blocked"],
        ["--no-anchoring-guard"],
        ["--max-attempts", "9"],
    ):
        refused = transform(fleet, *flag)
        assert refused.exit_code == ExitCode.USAGE, f"{flag} was silently accepted"
    assert query(fleet, "SELECT COUNT(*) FROM phases WHERE phase = 2") == [(0,)]


def test_context_policy_reaches_the_worker_not_the_hardcoded_default(fleet: Path) -> None:
    """§12.35's CLI-level proof: `--context-policy 2=EVIDENCE_PLUS_PRIORS` must change what a
    REAL worker receives at rung 2, not merely parse and then be dropped.

    The discriminator (Rule 12) is sharper than the flag's own value, because tier is DERIVED
    from context policy (`workers/base._TIER_FOR_RUNG`) and never declared twice: rung 2's
    hardcoded default is `EVIDENCE_ONLY`, tier `LLM_REPAIR`, role `transform_repair` — but
    `EVIDENCE_PLUS_PRIORS` maps to tier `LLM_ESCALATION`, role `escalation`. If the pre-fix
    hardcoded ladder were still in effect (the old refusal's own claim: the override "would be
    parsed and then ignored by every rung"), this run would call `transform_repair` with
    `EVIDENCE_ONLY` and no `rejected_approaches` key, exactly as the untouched default would.
    Seeing `escalation` called instead, with the overridden policy and the `rejected_approaches`
    key that only that policy adds, is proof the CONFIGURED value reached
    `BaseWorker.execute` — a mis-wiring could not coincidentally produce this exact shape.

    `acme-app-py`'s deterministic rung 1 is a designed `RULE_MISS` (`PY_MISSING_RULE`), so it
    escalates past rung 1 and is the only repo that calls the model at all — the other three
    repos' rules match cleanly at rung 1 and never reach this code path. `--max-attempts 2` caps
    the ladder so the run terminates right after that one call.
    """
    write_rules(fleet, TS_IMPORT_RULE, PY_MISSING_RULE)
    scanned(fleet)

    calls: list[tuple[str, str]] = []

    async def fake_complete(
        self: object, role: str, messages: object, response_model: object, **kwargs: object
    ) -> object:
        content = "\n".join(m.content for m in messages)  # type: ignore[attr-defined]
        calls.append((role, content))
        # No real backend in this e2e test: the point is to observe what WOULD have been sent,
        # not to simulate a repair. `_repair()` catches `LlmError` and turns it into a typed,
        # isolated repo failure (`WorkerRepairError` → `FailureClass.UNKNOWN`), never a crash.
        raise LlmError("simulated: no real backend in this e2e test")

    monkeypatched = pytest.MonkeyPatch()
    try:
        monkeypatched.setattr("fleet.llm.client.LadderModelClient.complete", fake_complete)
        result = runner.invoke(
            app,
            [
                *base_args(fleet),
                "--json",
                "transform",
                "--max-attempts",
                "2",
                "--context-policy",
                "2=EVIDENCE_PLUS_PRIORS",
            ],
            catch_exceptions=False,
        )
    finally:
        monkeypatched.undo()

    assert result.exit_code == ExitCode.REQUIRES_HUMAN_INTERVENTION, result.output
    assert len(calls) == 1, f"expected exactly one LLM call (acme-app-py's rung 2), got {calls}"
    role, content = calls[0]
    assert role == "escalation", (
        f"expected role 'escalation' (tier LLM_ESCALATION, derived from the configured "
        f"EVIDENCE_PLUS_PRIORS override) — got {role!r}, which is what rung 2's HARDCODED "
        f"default (EVIDENCE_ONLY -> LLM_REPAIR -> 'transform_repair') would have produced"
    )
    assert '"context_policy": "EVIDENCE_PLUS_PRIORS"' in content, content
    assert '"rejected_approaches"' in content, (
        "the rendered evidence has no rejected_approaches key — rung 2 ran under the HARDCODED "
        f"default EVIDENCE_ONLY, not the configured EVIDENCE_PLUS_PRIORS override: {content}"
    )

    statuses = dict(query(fleet, "SELECT repo_id, status FROM phases WHERE phase = 2"))
    assert statuses["acme-app-py"] == "REQUIRES_HUMAN_INTERVENTION", statuses
    for survivor in ("acme-lib-ts", "acme-lib-py", "acme-app-ts"):
        assert statuses[survivor] == "SUCCEEDED", statuses


def test_dry_run_emits_the_plan_and_writes_nothing(fleet: Path) -> None:
    """`--dry-run` stops at §3.2 step 1: the plan is emitted, and NOTHING is created.

    Why "nothing" is the assertion: the plan is the last point before the harness starts moving a
    tree, and a dry run that cut the phase anchor or created `migrate/<repo>` would have already
    made the decision it was asked to preview.
    """
    scanned(fleet)
    result = transform(fleet, "--dry-run")
    assert result.exit_code == ExitCode.SUCCESS, result.output

    plan = dict(payload(result)["plan"])
    assert set(plan) == set(DESTINATIONS), plan
    app_plan = dict(plan["acme-app-ts"])
    assert app_plan["moves"] == {
        "package.json": "ts/acme/app/package.json",
        "src/main.ts": "ts/acme/app/src/main.ts",
    }
    assert app_plan["rewrites"] == ["ts/acme/app/src/main.ts"]

    assert query(fleet, "SELECT COUNT(*) FROM phases WHERE phase = 2") == [(0,)]
    branches = git(worktree(fleet, "acme-app-ts"), "branch", "--list", "migrate/*")
    assert branches == "", f"--dry-run created a branch: {branches}"


def test_deterministic_only_reaches_no_model(fleet: Path) -> None:
    """The whole transform runs with a `ModelClient.complete` wired to explode.

    Why it has to be mechanical: `--deterministic-only` caps the ladder at rung 1, the rung that
    composes no prompt (ADR-0021). If the cap were merely advisory, a repo whose rules missed
    would escalate to a `WORKHORSE` call against an unreachable backend and take the whole fleet
    down with exit 8 — a run-scoped halt caused by one operator's incomplete rule set.
    """
    calls: list[str] = []

    async def explode(*args: object, **kwargs: object) -> object:
        calls.append("complete")
        raise AssertionError("the deterministic transform path must not reach a model")

    scanned(fleet)
    monkeypatched = pytest.MonkeyPatch()
    try:
        monkeypatched.setattr("fleet.llm.client.LadderModelClient.complete", explode)
        assert transform(fleet).exit_code == ExitCode.SUCCESS
    finally:
        monkeypatched.undo()
    assert calls == []
    tiers = dict(query(fleet, "SELECT repo_id, tier FROM attempts WHERE phase = 2"))
    assert tiers == dict.fromkeys(DESTINATIONS, "DETERMINISTIC")


def test_transform_before_sequence_refuses_rather_than_inventing_a_wave(fleet: Path) -> None:
    """With no wave plan there is nothing to transform, and the verb says so instead of
    transforming the fleet in manifest order.

    Why: the wave plan IS the dependency order (§3.1 step 7). A transform that ran without one
    would migrate a dependent before the dependency it imports, which is the single failure the
    topological sequencer was built to prevent — and it would look like a success.
    """
    assert scan(fleet).exit_code == ExitCode.SUCCESS
    result = transform(fleet)
    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert payload(result)["waves"] == []
    assert query(fleet, "SELECT COUNT(*) FROM phases WHERE phase = 2") == [(0,)]


# ---------------------------------------------------------------------------------------
# 6. `_TransformEvidence.record()` — D49's third leg, and the regression it introduced
# ---------------------------------------------------------------------------------------
#
# `9a7148c` (D49) correctly changed what `output.rewritten` holds: deterministic unit names
# before, landed `FilePatch.path`s after — the latter is what `_transform_criterion`'s parse
# probe needs, since a multi-file LLM repair can land up to 64 paths for one unit. But
# `_TransformEvidence.record()` kept deduping `output.unresolved` against `set(prior.rewritten)`,
# which used to be an identity check ("was this unit already resolved") and after the change is a
# coincidental *filename* match — `rewritten` and `unresolved` no longer share a namespace.
#
# These two tests exercise `record()` directly, the way `tests/test_cli.py`'s
# `_transform_criterion` tests already do, rather than driving a full multi-attempt `fleet
# transform` run: reproducing the exact deadline-and-repair timing through the real CLI would
# pin the scenario far less precisely than constructing the two `TransformOutput`s the worked
# example in the task brief describes.


def test_a_units_own_failure_survives_a_siblings_collateral_rewrite() -> None:
    """D49 regression, pinned: a unit whose canonical name coincides with a DIFFERENT unit's
    collaterally-landed sibling path must still be reported unresolved.

    Worked scenario: unit `B`'s LLM repair also touches a legitimate sibling `dest/x.py`, so
    attempt 1's `rewritten` records `dest/x.py` even though `B` — not `dest/x.py` — is the unit
    that actually resolved. Unit `A`'s own canonical name IS `dest/x.py`, and `A` genuinely fails
    on attempt 2. Keying the dedup on `prior.rewritten` (paths) drops `A`'s failure because
    `dest/x.py` already sits there from `B`'s unrelated edit; keying it on `completed_units`
    (unit identity) does not, because `dest/x.py` was never `A`'s own completed unit.

    Confirmed to fail against `HEAD~0`'s pre-fix `record()` (dedup on `set(prior.rewritten)`) and
    to pass after keying the dedup on `completed_units` instead.
    """
    from fleet.cli import TransformOutput, _TransformEvidence

    evidence = _TransformEvidence()

    # Attempt 1 (partial): unit B lands; its repair collaterally rewrites sibling dest/x.py.
    evidence.record(
        TransformOutput(repo_id="repo1", rewritten=["dest/b.py", "dest/x.py"], unresolved=[]),
        completed_units=["rewrite:dest/b.py"],
    )

    # Attempt 2: unit A — whose own canonical name is "dest/x.py" — genuinely fails.
    evidence.record(
        TransformOutput(repo_id="repo1", rewritten=[], unresolved=["dest/x.py"]),
        completed_units=[],
    )

    assert evidence.by_repo["repo1"].unresolved == ["dest/x.py"], (
        "A's genuine failure was dropped — dest/x.py was never A's own completed unit, only "
        "B's collateral edit landed a file at that path"
    )


def test_a_units_own_completion_still_clears_it_from_unresolved() -> None:
    """The behaviour the D49 regression must not break: a unit resolved under its OWN identity
    on a prior attempt is still correctly dropped from `unresolved`, and a genuinely still-failing
    sibling is not swept away with it.

    `completed_units` — namespaced `rewrite:<path>` at the `TransformPipelineWorker` level — is
    populated by `RewriteWorker.run` on every path that legitimately resolves a unit: the
    deterministic land (`rewrite.py:380`), the idempotent `find_task_commit` shortcut
    (`rewrite.py:328`), and the repair-rung land (`rewrite.py:452`) all append the loop's own
    `unit`, never a collateral path. That is what makes it the right identity key.
    """
    from fleet.cli import TransformOutput, _TransformEvidence

    evidence = _TransformEvidence()

    # Attempt 1: unit C resolves cleanly under its own name.
    evidence.record(
        TransformOutput(repo_id="repo1", rewritten=["dest/c.py"], unresolved=[]),
        completed_units=["rewrite:dest/c.py"],
    )

    # Attempt 2: C is (spuriously) re-reported unresolved alongside a genuinely-failing D.
    evidence.record(
        TransformOutput(
            repo_id="repo1", rewritten=[], unresolved=["dest/c.py", "dest/d.py"]
        ),
        completed_units=[],
    )

    assert evidence.by_repo["repo1"].unresolved == ["dest/d.py"], (
        "C's prior completion should still clear it from unresolved, and D — never completed — "
        "must not be dropped alongside it"
    )
