"""§10's command surface: the flags an operator types and the exit codes CI reads.

Every test here answers "why does this matter":

* **`--help` on every verb, offline** — the CLI is the only entry point in the harness, and a
  verb whose parser cannot be rendered without a database is a verb an operator cannot discover
  on a fresh checkout. Parametrized over `command_paths()`, which is *derived from the click
  group*, so a verb added to §10 tomorrow is covered the moment it exists.
* **Each documented exit code produced by the condition that documents it.** §10's whole reason
  for eleven codes is that CI can tell them apart: a budget stop that exits 1 reads as
  "unexpected error", and an operator cannot distinguish "the wave ran out of money" (resumable
  with `--raise-wave-budget`) from "the harness crashed" (not resumable at all).
* **`migrate-db` on a v0 database initializes; on an older one it runs the ladder.** These are
  different code paths in different modules (`state/db.py` vs `migrations/`) and §6 refuses to
  let either impersonate the other — `CREATE TABLE IF NOT EXISTS` never executes an `ALTER`.
* **`quarantine` needs no config edit.** That is the entire reason the verb exists: editing
  `config/repos.yaml` trips drift detection, whose only escape accepts every co-edited change.
* **`--accept-drift <section>` accepts exactly one section.** A flag that accepted its
  neighbours would be `--force-config-drift` with a friendlier name.
* **No secret reaches stdout or stderr.** A clone URL carrying a `github_pat_…` is the exact
  shape §11.4 exists to stop, and an error path is where redaction is most often forgotten.
* **A typed internal error maps to its code, not a traceback.** Rule 11.
"""

from __future__ import annotations

import ast
import asyncio
import json
import re
import shutil
import sqlite3
import subprocess
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from fleet.bazel.lockfile import MODULE_LOCK_PATH
from fleet.cli import (
    BuildInput,
    BuildOutput,
    BuildPipelineWorker,
    ExitCode,
    TransformStepUnavailableError,
    _abandon_repo,
    _prepare_repo,
    _TransformPlan,
    app,
    command_paths,
)
from fleet.llm.client import discover as llm_discover
from fleet.llm.roles import SPEC_ROLE_TIERS
from fleet.migrations import LATEST_VERSION
from fleet.models.build import BuildUnit, SupportFile
from fleet.models.enums import Ecosystem, Phase
from fleet.models.state import SCHEMA_VERSION
from fleet.sandbox.container import ContainerSandbox
from fleet.sandbox.worktree import WorktreeManager
from fleet.state.db import SCHEMA_PATH, StateWriter
from fleet.util.proc import ProcResult
from fleet.util.proc import run as proc_run
from fleet.vcs.git import Git, GitCommandError, GitError
from fleet.workers.base import WorkerContext
from tests.test_migrations import _v6_database

runner = CliRunner()

REPO_SRC = Path(__file__).resolve().parents[1] / "src" / "fleet"

RUN_ID = "11111111-1111-4111-8111-111111111111"

#: Every `Role` must be declared or `LlmRouter` refuses at startup — a role with no model behind
#: it is a job the harness will reach and be unable to run (§7.7). Generated from the enum so a
#: new role cannot leave this fixture quietly stale.
ROLES_BLOCK = "".join(
    f"  {role.value}: {tier.value}\n" for role, tier in sorted(SPEC_ROLE_TIERS.items())
)

MODELS_YAML = f"""\
version: 2
roles:
{ROLES_BLOCK}\
default_profile: default
profiles:
  default:
""" + """\
    HEAVY:
      - { backend: anthropic, model_id: claude-opus-5, effort: high,
          api_key_env: ANTHROPIC_API_KEY,
          price: { in_per_mtok: 5.0, out_per_mtok: 25.0 } }
    WORKHORSE:
      - { backend: anthropic, model_id: claude-sonnet-5, effort: high,
          api_key_env: ANTHROPIC_API_KEY,
          price: { in_per_mtok: 3.0, out_per_mtok: 15.0 } }
    CHEAP:
      - { backend: anthropic, model_id: claude-haiku-4-5,
          api_key_env: ANTHROPIC_API_KEY,
          price: { in_per_mtok: 1.0, out_per_mtok: 5.0 } }
"""

#: ADR-0023's "swap the fleet to local models" profile — the one `--profile local` selects.
LOCAL_PROFILE_YAML = """\
  local:
    HEAVY:
      - { backend: openai_compatible, model_id: local-heavy, effort: high, price: free,
          base_url: 'http://localhost:8001/v1', api_key_env: LOCAL_LLM_API_KEY }
    WORKHORSE:
      - { backend: openai_compatible, model_id: local-workhorse, effort: medium, price: free,
          base_url: 'http://localhost:8001/v1', api_key_env: LOCAL_LLM_API_KEY }
    CHEAP:
      - { backend: openai_compatible, model_id: local-cheap, effort: low, price: free,
          base_url: 'http://localhost:8001/v1', api_key_env: LOCAL_LLM_API_KEY }
"""

#: §9 rule 5's canonical exit-2 case: a HEAVY target that declares neither a price nor `free`.
UNPRICED_MODELS_YAML = MODELS_YAML.replace(
    "          price: { in_per_mtok: 5.0, out_per_mtok: 25.0 } }",
    "        }",
)

REPOS_YAML = """\
version: 1
defaults:
  ref: main
repos:
  - name: acme-commons
    url: https://github.com/acme/acme-commons
  - name: acme-billing
    url: https://github.com/acme/acme-billing
"""

#: The leak the redactor exists for. `redaction.patterns`'s `github_pat` detector matches it, so
#: §9 rule 4 refuses the file at load — and the refusal must not quote the token back.
LEAKY_TOKEN = "github_pat_" + "A1b2C3d4E5f6G7h8I9j0" + "K1l2M3n4O5p6Q7r8S9t0"
LEAKY_REPOS_YAML = f"""\
version: 1
repos:
  - name: acme-commons
    url: https://x-access-token:{LEAKY_TOKEN}@github.com/acme/acme-commons
"""


FLEET_YAML = (
    "run:\n  monorepo_path: ../acme-monorepo\n"
    # See the note in `tests/test_scan_e2e.py`: the shipped 50 GiB floor would refuse every
    # command on a developer volume, so the fixture lowers it rather than disabling it. The
    # refusal is asserted separately, against a floor no volume can clear.
    "preflight:\n  min_free_bytes: 1048576\n"
)


def write_config(
    tmp_path: Path,
    *,
    fleet: str = FLEET_YAML,
    models: str = MODELS_YAML,
    repos: str = REPOS_YAML,
) -> Path:
    """The three-file `config/` directory §9 documents. Returns `config/fleet.yaml`."""
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "fleet.yaml").write_text(fleet, encoding="utf-8")
    (config_dir / "models.yaml").write_text(models, encoding="utf-8")
    (config_dir / "repos.yaml").write_text(repos, encoding="utf-8")
    # `transform.rules_dir` (default `config/rules`) must exist: an ABSENT directory is refused
    # (see `_transform_rules`, cli.py) since it is indistinguishable from a deleted/mistyped
    # config. Present-and-empty is the fixture's way of saying "this fleet has no rewrite
    # rules", exactly as `tests/test_transform_e2e.py`'s fixture already does.
    (config_dir / "rules").mkdir(parents=True, exist_ok=True)
    return config_dir / "fleet.yaml"


def fresh_db(path: Path) -> Path:
    """A database at the shipped baseline, built the way `migrate-db` builds one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    finally:
        conn.close()
    return path


def seed_run(
    path: Path,
    *,
    run_id: str = RUN_ID,
    repos: tuple[str, ...] = ("acme-commons", "acme-billing"),
    config_digests: str = "{}",
) -> None:
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO runs (run_id, started_at, config_sha256, config_digests, "
            "                  harness_version) VALUES (?, ?, ?, ?, ?)",
            (run_id, "2026-08-08T12:00:00+00:00", "a" * 64, config_digests, "0.1.0"),
        )
        for name in repos:
            conn.execute(
                "INSERT INTO repos (repo_id, name, url, updated_at) VALUES (?, ?, ?, ?)",
                (name, name, f"https://example.invalid/{name}", "2026-08-08T12:00:00+00:00"),
            )
    finally:
        conn.close()


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A cwd-isolated workspace: config, a database at the baseline, and one seeded run.

    The run records TODAY's per-section digests, so the default state is "no drift" and a drift
    test has to create the drift it asserts on. Seeding `{}` instead would make every resume
    report drift in every section — which reads as a passing drift test that proves nothing.
    """
    from fleet.settings import FleetSettings

    write_config(tmp_path)
    settings = FleetSettings.load(tmp_path / "config")
    fresh_db(tmp_path / "state" / "fleet.db")
    seed_run(
        tmp_path / "state" / "fleet.db",
        config_digests=json.dumps(dict(settings.section_digests), sort_keys=True),
    )
    monkeypatch.chdir(tmp_path)
    yield tmp_path


def base_args(root: Path) -> list[str]:
    return ["--config", str(root / "config" / "fleet.yaml"), "--db", str(root / "state/fleet.db")]


# --------------------------------------------------------------------------------------
# --help: discoverable offline, on every verb
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", command_paths(), ids=lambda p: " ".join(p))
def test_every_verb_renders_help_offline(path: tuple[str, ...], tmp_path: Path) -> None:
    """`fleet <verb> --help` exits 0 with no database, no network and no model.

    Why: the CLI is the only entry point, and §12's acceptance criteria are literally "this
    command exits 0". A `--help` that needs `state/fleet.db` cannot be run on a fresh checkout,
    which makes the surface undiscoverable exactly when an operator most needs it. Derived from
    the click group, so a verb added to §10 is covered without editing this test.
    """
    result = runner.invoke(app, [*path, "--help"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert not (tmp_path / "state").exists()


def test_root_help_lists_every_command() -> None:
    """The group help is the operator's index; a verb missing from it is a verb nobody finds."""
    result = runner.invoke(app, ["--help"], catch_exceptions=False)
    assert result.exit_code == 0
    for verb in ("scan", "sequence", "migrate", "migrate-db", "transform", "resume", "pr"):
        assert verb in result.output
    for verb in ("status", "quarantine", "abort", "gc"):
        assert verb in result.output


def test_migrate_and_migrate_db_are_distinct_verbs() -> None:
    """§10 keeps them apart on purpose: `migrate-db` is the ONLY DDL path, and every other
    command — `fleet migrate` included — *reads* `user_version` and refuses on a mismatch rather
    than rebuilding tables underneath a live run. Merging them puts a table rebuild on the
    startup path of the busiest verb in the harness."""
    paths = {" ".join(p) for p in command_paths()}
    assert "migrate" in paths
    assert "migrate-db" in paths


# --------------------------------------------------------------------------------------
# exit 2 — configuration
# --------------------------------------------------------------------------------------


def test_unpriced_target_exits_2(tmp_path: Path) -> None:
    """An unpriced target is exit 2, at startup, before a repo is touched (§9 rule 5).

    Why: with no declared price the ledger reserves $0.00 for every call, so
    `budgets.run_max_cost_usd` never trips and a 250-repo run bills unbounded dollars while every
    cost assertion still passes. Exit 2 says "edit a file"; exit 1 would say "file a bug".
    """
    config = write_config(tmp_path, models=UNPRICED_MODELS_YAML)
    result = runner.invoke(app, ["--config", str(config), "models", "list"])
    assert result.exit_code == ExitCode.USAGE == 2, result.output
    assert "price" in result.output.lower()


def test_max_cost_usd_may_only_lower_the_ceiling(workspace: Path) -> None:
    """§10: `--max-cost-usd` "overrides `budgets.run_max_cost_usd` downward only".

    Why: a flag that silently raised a run ceiling would make the durable ledger's fail-closed
    guarantee a suggestion — the one number an operator cannot raise without an audit trail.
    """
    result = runner.invoke(
        app, [*base_args(workspace), "--max-cost-usd", "9999", "models", "profiles"]
    )
    assert result.exit_code == ExitCode.USAGE
    assert "downward" in result.output.lower()


def test_context_policy_refuses_rung_one(workspace: Path) -> None:
    """`--context-policy 1=...` is refused with exit 2, deterministically (§10).

    Why: rung 1 is the deterministic rung and composes no prompt, so a context policy there
    governs nothing. Refusing before any model call means the operator learns it in a second
    rather than after a wave of cache misses.
    """
    result = runner.invoke(
        app,
        [*base_args(workspace), "transform", "--context-policy", "1=EVIDENCE_ONLY"],
    )
    assert result.exit_code == ExitCode.USAGE
    assert "rung 1" in result.output


def test_context_policy_refuses_unknown_policy(workspace: Path) -> None:
    """A policy outside `ContextPolicy` is exit 2 with the valid names listed.

    Why: the policy is an `llm_cache` key component (§11.6). A typo accepted silently would key
    the cache on a value no rung will ever ask for again — every call a permanent miss.
    """
    result = runner.invoke(
        app, [*base_args(workspace), "transform", "--context-policy", "2=EVIDENCE_PLUS_VIBES"]
    )
    assert result.exit_code == ExitCode.USAGE
    assert "EVIDENCE_PLUS_PRIORS" in result.output


def test_schema_version_mismatch_is_exit_2_not_a_silent_upgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A database behind the harness refuses with exit 2 naming `fleet migrate-db` (§6).

    Why: "every other command reads `user_version` and refuses on a mismatch rather than
    upgrading underneath a live run". An implicit upgrade is a table rebuild racing live workers.
    """
    write_config(tmp_path)
    db = fresh_db(tmp_path / "state" / "fleet.db")
    conn = sqlite3.connect(db, isolation_level=None)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION - 1}")
    conn.close()
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, [*base_args(tmp_path), "status"])
    assert result.exit_code == ExitCode.USAGE
    assert "migrate-db" in result.output


# --------------------------------------------------------------------------------------
# exit 10 — the durable wave ledger
# --------------------------------------------------------------------------------------


def _exhaust_wave(db: Path, *, wave: int = 0, max_usd: float = 10.0, spent: float = 12.0) -> None:
    """A wave whose members have already spent past the ceiling frozen at first admission.

    §6: the wave's ceiling lives in `waves.max_usd` and its spend is `SUM(repo_ledger.spent_usd)`
    over its REPO members — never duplicated, so this is the real durable condition, not a mock.
    """
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO waves (run_id, wave_index, computed_at, max_usd) VALUES (?, ?, ?, ?)",
            (RUN_ID, wave, "2026-08-08T12:00:00+00:00", max_usd),
        )
        conn.execute(
            "INSERT INTO wave_members (run_id, wave_index, node_kind, node_id) "
            "VALUES (?, ?, 'REPO', 'acme-commons')",
            (RUN_ID, wave),
        )
        conn.execute(
            "INSERT INTO repo_ledger (run_id, repo_id, spent_usd, max_usd, updated_at) "
            "VALUES (?, 'acme-commons', ?, ?, ?)",
            (RUN_ID, spent, spent + 1.0, "2026-08-08T12:00:00+00:00"),
        )
    finally:
        conn.close()


def test_wave_cost_exhausted_exits_10(workspace: Path) -> None:
    """A wave over its durable ceiling halts with exit 10 — not 1, and not 3.

    Why: §10 gives the wave ceiling its own code because the remedy is its own flag. Exit 3 means
    a sticky run-level `halted = 1` that only `--raise-budget` clears; exit 10 means one wave's
    ledger, cleared by `--raise-wave-budget`. Collapsed into 1, CI reads either as a crash and
    the operator retries the wrong thing forever.
    """
    _exhaust_wave(workspace / "state" / "fleet.db")
    result = runner.invoke(app, [*base_args(workspace), "transform"])
    assert result.exit_code == ExitCode.WAVE_COST_EXHAUSTED == 10, result.output
    assert "--raise-wave-budget" in result.output


def test_raise_wave_budget_clears_the_halt_and_is_audited(workspace: Path) -> None:
    """`fleet resume --raise-wave-budget` clears exit 10 and writes the finding that says so.

    Why: the wave ledger is durable, so without this flag a resume re-enters the same wave
    carrying the same spend and halts again, forever. And an unaudited raise is an unexplained
    budget in next week's report.
    """
    db = workspace / "state" / "fleet.db"
    _exhaust_wave(db)
    halted = runner.invoke(app, [*base_args(workspace), "resume", "--dry-run"])
    assert halted.exit_code == ExitCode.WAVE_COST_EXHAUSTED

    preview = runner.invoke(
        app, [*base_args(workspace), "resume", "--dry-run", "--raise-wave-budget", "50"]
    )
    assert preview.exit_code == ExitCode.SUCCESS, preview.output

    # `--dry-run` previewed the clearance and wrote nothing; the real resume writes the audit
    # BEFORE it stops on the unbuilt §11.5 step 5 (exit 2, ADR-0076 — a reconciliation that
    # succeeded, not a crash) — the ordering that matters, since a raise recorded only on success
    # is a raise lost to the next crash.
    real = runner.invoke(app, [*base_args(workspace), "resume", "--raise-wave-budget", "50"])
    assert real.exit_code == ExitCode.USAGE, real.output

    conn = sqlite3.connect(db)
    try:
        kinds = [row[0] for row in conn.execute("SELECT kind FROM findings")]
        ceiling = conn.execute(
            "SELECT max_usd FROM waves WHERE run_id = ? AND wave_index = 0", (RUN_ID,)
        ).fetchone()
    finally:
        conn.close()
    assert "WaveBudgetRaised" in kinds
    assert ceiling[0] == pytest.approx(50.0)


# --------------------------------------------------------------------------------------
# exit 11 — sequence refused mid-run
# --------------------------------------------------------------------------------------


def _put_in_flight(db: Path, repo: str = "acme-commons", status: str = "RUNNING") -> None:
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO phases (run_id, repo_id, phase, status, updated_at) VALUES (?,?,1,?,?)",
            (RUN_ID, repo, status, "2026-08-08T12:00:00+00:00"),
        )
    finally:
        conn.close()


def test_sequence_refused_mid_run_exits_11(workspace: Path) -> None:
    """`fleet sequence` against a fleet in flight exits 11, having changed nothing.

    Why: renumbering waves under a repo that is mid-transform moves it into a wave whose
    dependencies have not landed — the exact failure the topological sequencer exists to prevent.
    §10 makes it 11 rather than 3 deliberately: a refusal costs nothing and leaves no `halted`
    ledger, while exit 3 is sticky and needs `--raise-budget`; CI cannot tell those apart from
    one code.
    """
    _put_in_flight(workspace / "state" / "fleet.db")
    result = runner.invoke(app, [*base_args(workspace), "sequence"])
    assert result.exit_code == ExitCode.SEQUENCE_REFUSED == 11, result.output
    assert "--force-resequence" in result.output

    conn = sqlite3.connect(workspace / "state" / "fleet.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM waves").fetchone()[0] == 0
    finally:
        conn.close()


def test_force_resequence_overrides_the_refusal(workspace: Path) -> None:
    """`--force-resequence` is the documented override, and it actually sequences.

    Why: a refusal with no escape hatch is a run an operator cannot recover; §3.1 names the flag,
    so it must reach the plan writer rather than merely being accepted by the parser.
    """
    _put_in_flight(workspace / "state" / "fleet.db")
    result = runner.invoke(
        app, [*base_args(workspace), "--json", "sequence", "--force-resequence"]
    )
    assert result.exit_code == ExitCode.SUCCESS, result.output
    payload = json.loads(result.stdout)
    assert payload["forced"] is True
    assert payload["repos"] == 2


def test_sequence_refuses_unresolved_error_collisions_with_exit_6(workspace: Path) -> None:
    """An unresolved `severity='error'` collision fails `fleet sequence` with exit 6 (§10).

    Why: §3.1 step 8 detects collisions BEFORE any transform precisely so they are resolved in
    config rather than in a half-migrated monorepo. Exiting 0 here would migrate two repos onto
    one destination path.
    """
    conn = sqlite3.connect(workspace / "state" / "fleet.db", isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO collisions (run_id, kind, key, repo_ids, severity, detected_at) "
            "VALUES (?, 'DEST_PATH', 'libs/acme', '[\"a\",\"b\"]', 'error', ?)",
            (RUN_ID, "2026-08-08T12:00:00+00:00"),
        )
    finally:
        conn.close()
    result = runner.invoke(app, [*base_args(workspace), "sequence"])
    assert result.exit_code == ExitCode.UNRESOLVED_FINDINGS == 6, result.output


# --------------------------------------------------------------------------------------
# migrate-db: fresh vs ladder
# --------------------------------------------------------------------------------------


def test_migrate_db_initializes_a_fresh_database(tmp_path: Path) -> None:
    """A v0 (or absent) database is INITIALIZED from `schema.sql`, not refused.

    Why: this is the named defect. `migrate()` refuses `user_version = 0` by name, because a
    ladder cannot `ALTER` tables that do not exist — so the fresh branch has to be wired here or
    `fleet migrate-db` cannot create the very database every other verb requires.
    """
    write_config(tmp_path)
    db = tmp_path / "state" / "fleet.db"
    result = runner.invoke(
        app, ["--config", str(tmp_path / "config/fleet.yaml"), "--db", str(db), "migrate-db"]
    )
    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert db.exists()
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='phases'"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_migrate_db_runs_the_ladder_on_an_older_database(tmp_path: Path) -> None:
    """An existing database one rung back is lifted by the ladder, not re-initialized.

    Why: the other half of the same defect. `CREATE TABLE IF NOT EXISTS` creates a *missing*
    schema and never executes an `ALTER`, so an "apply schema.sql idempotently" path would leave
    this database at its old shape while reporting success (§6).
    """
    write_config(tmp_path)
    (tmp_path / "state").mkdir()
    # A genuine v6 database — the pre-v007 table SHAPES with data in them, not a v7 file with its
    # PRAGMA rewound. Rewinding the pragma would test nothing: the ladder would try to add a
    # column that already exists and fail, which is precisely what happens when the two branches
    # of this command are conflated.
    db = _v6_database(tmp_path / "state" / "fleet.db")

    result = runner.invoke(
        app, ["--config", str(tmp_path / "config/fleet.yaml"), "--db", str(db), "migrate-db"]
    )
    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert f"{LATEST_VERSION - 1} → {LATEST_VERSION}" in result.output
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_VERSION
    finally:
        conn.close()


def test_migrate_db_dry_run_writes_nothing(tmp_path: Path) -> None:
    """`--dry-run` names the branch it would take and leaves the filesystem alone."""
    write_config(tmp_path)
    db = tmp_path / "state" / "fleet.db"
    result = runner.invoke(
        app,
        ["--config", str(tmp_path / "config/fleet.yaml"), "--db", str(db), "migrate-db",
         "--dry-run"],
    )
    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert "initialize" in result.output
    assert not db.exists()


# --------------------------------------------------------------------------------------
# quarantine: state, not config
# --------------------------------------------------------------------------------------


def test_quarantine_writes_a_finding_and_skips_without_touching_config(workspace: Path) -> None:
    """`fleet quarantine` records the removal as STATE and never asks for a config edit.

    Why: that is the whole reason §10 gives it a verb. The alternative — deleting the repo from
    `config/repos.yaml` — moves the `repos` section digest, so the next resume reports config
    drift whose only escape (`--force-config-drift`) silently accepts every OTHER co-edited
    change too. Here the config bytes are asserted byte-identical afterwards.
    """
    db = workspace / "state" / "fleet.db"
    repos_yaml = workspace / "config" / "repos.yaml"
    before = repos_yaml.read_bytes()
    _put_in_flight(db, "acme-commons", "PENDING")

    result = runner.invoke(
        app,
        [*base_args(workspace), "quarantine", "acme-commons", "--reason", "pathological submodule"],
    )
    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert repos_yaml.read_bytes() == before

    conn = sqlite3.connect(db)
    try:
        finding = conn.execute(
            "SELECT kind, repo_id, payload FROM findings WHERE kind = 'OperatorQuarantine'"
        ).fetchone()
        status = conn.execute(
            "SELECT status FROM phases WHERE run_id = ? AND repo_id = 'acme-commons'", (RUN_ID,)
        ).fetchone()
    finally:
        conn.close()
    assert finding is not None
    assert finding[1] == "acme-commons"
    assert "pathological submodule" in finding[2]
    assert status[0] == "SKIPPED"


def test_quarantine_requires_a_reason(workspace: Path) -> None:
    """`--reason` is required and non-empty: the reason IS the audit record (§10)."""
    empty = runner.invoke(
        app, [*base_args(workspace), "quarantine", "acme-commons", "--reason", "   "]
    )
    assert empty.exit_code == ExitCode.USAGE
    missing = runner.invoke(app, [*base_args(workspace), "quarantine", "acme-commons"])
    assert missing.exit_code != 0


def test_quarantine_propagates_blocked_by_to_dependents(workspace: Path) -> None:
    """A quarantined repo blocks its transitive dependents "exactly as an abandonment does".

    Why: §3.5's containment rule. A dependent left `PENDING` would be admitted into its wave and
    migrated against a dependency that is never landing.
    """
    db = workspace / "state" / "fleet.db"
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO edges (edge_key, run_id, src_kind, src_id, dst_kind, dst_id, "
            "                   dst_coord_key, kind, base_confidence, confidence, "
            "                   evidence_path, detected_at) "
            "VALUES (?, ?, 'REPO', 'acme-billing', 'REPO', 'acme-commons', "
            "        'maven:com.acme:commons', 'DECLARED_DEP', 0.95, 0.95, 'pom.xml', ?)",
            ("b" * 64, RUN_ID, "2026-08-08T12:00:00+00:00"),
        )
        for repo in ("acme-commons", "acme-billing"):
            conn.execute(
                "INSERT INTO phases (run_id, repo_id, phase, status, updated_at) "
                "VALUES (?, ?, 1, 'PENDING', ?)",
                (RUN_ID, repo, "2026-08-08T12:00:00+00:00"),
            )
    finally:
        conn.close()

    result = runner.invoke(
        app, [*base_args(workspace), "quarantine", "acme-commons", "--reason", "unfixable"]
    )
    assert result.exit_code == ExitCode.SUCCESS, result.output

    conn = sqlite3.connect(db)
    try:
        status, blocked_by = conn.execute(
            "SELECT status, blocked_by FROM phases WHERE run_id = ? AND repo_id = 'acme-billing'",
            (RUN_ID,),
        ).fetchone()
    finally:
        conn.close()
    assert status == "BLOCKED"
    assert json.loads(blocked_by) == ["acme-commons"]


def test_quarantine_dry_run_changes_nothing(workspace: Path) -> None:
    """`--dry-run` reports the same plan and writes no finding."""
    result = runner.invoke(
        app,
        [*base_args(workspace), "quarantine", "acme-commons", "--reason", "look first",
         "--dry-run"],
    )
    assert result.exit_code == ExitCode.SUCCESS, result.output
    conn = sqlite3.connect(workspace / "state" / "fleet.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM findings").fetchone()[0] == 0
    finally:
        conn.close()


# --------------------------------------------------------------------------------------
# --accept-drift: exactly one section
# --------------------------------------------------------------------------------------


def _seed_drifted_run(workspace: Path, *, matching: tuple[str, ...]) -> None:
    """Record a baseline in which only `matching` sections agree with today's config."""
    from fleet.settings import FleetSettings

    settings = FleetSettings.load(workspace / "config")
    baseline = {
        name: (digest if name in matching else "0" * 64)
        for name, digest in settings.section_digests.items()
    }
    conn = sqlite3.connect(workspace / "state" / "fleet.db", isolation_level=None)
    try:
        conn.execute(
            "UPDATE runs SET config_digests = ? WHERE run_id = ?",
            (json.dumps(baseline, sort_keys=True), RUN_ID),
        )
    finally:
        conn.close()


def test_accept_drift_accepts_exactly_the_named_section(workspace: Path) -> None:
    """`--accept-drift budgets` accepts `budgets` and still refuses every other drifted section.

    Why: this granularity is the whole point of per-section digests. With one opaque hash the
    only escape is `--force-config-drift`, which accepts every co-edited change in order to
    accept one — §10 says in as many words that this is how an operator loses a run.
    """
    from fleet.settings import CONFIG_SECTIONS, FleetSettings

    settings = FleetSettings.load(workspace / "config")
    everything_but_budgets_and_run = tuple(
        name for name in settings.section_digests if name not in {"budgets", "run"}
    )
    _seed_drifted_run(workspace, matching=everything_but_budgets_and_run)
    assert "budgets" in CONFIG_SECTIONS

    partial = runner.invoke(
        app, [*base_args(workspace), "resume", "--dry-run", "--accept-drift", "budgets"]
    )
    assert partial.exit_code == ExitCode.USAGE, partial.output
    assert "run" in partial.output
    assert "budgets" not in partial.output.split("section(s)")[-1]

    both = runner.invoke(
        app,
        [*base_args(workspace), "--json", "resume", "--dry-run",
         "--accept-drift", "budgets", "--accept-drift", "run"],
    )
    assert both.exit_code == ExitCode.SUCCESS, both.output
    assert set(json.loads(both.stdout)["accepted_sections"]) == {"budgets", "run"}


def test_accept_drift_rejects_a_name_that_is_not_a_section(workspace: Path) -> None:
    """A misspelt section is exit 2 with the valid list, never a silently ignored flag.

    Why: silently ignoring `--accept-drift budget` (singular) refuses the resume for a reason the
    operator believes they just accepted — the worst possible failure mode for an escape hatch.
    """
    result = runner.invoke(
        app, [*base_args(workspace), "resume", "--dry-run", "--accept-drift", "budget"]
    )
    assert result.exit_code == ExitCode.USAGE
    assert "budgets" in result.output


def test_accepted_drift_writes_one_config_drift_finding_per_section(workspace: Path) -> None:
    """"Each accepted section writes its own audited `ConfigDrift` finding" (§10)."""
    from fleet.settings import FleetSettings

    settings = FleetSettings.load(workspace / "config")
    _seed_drifted_run(
        workspace,
        matching=tuple(n for n in settings.section_digests if n not in {"budgets", "gc"}),
    )
    result = runner.invoke(
        app,
        [*base_args(workspace), "resume", "--accept-drift", "budgets", "--accept-drift", "gc"],
    )
    # The resume itself cannot complete (§11.5 step 5 is unbuilt) but the audit is written
    # before it stops, which is the ordering that matters: an accepted drift that is only
    # recorded on success is an accepted drift lost to the next crash.
    assert result.exit_code in {ExitCode.SUCCESS, ExitCode.USAGE}, result.output
    conn = sqlite3.connect(workspace / "state" / "fleet.db")
    try:
        sections = sorted(
            json.loads(row[0])["section"]
            for row in conn.execute("SELECT payload FROM findings WHERE kind = 'ConfigDrift'")
        )
    finally:
        conn.close()
    assert sections == ["budgets", "gc"]


def test_profile_on_resume_is_refused_without_force(workspace: Path) -> None:
    """`--profile` on a resume is "permitted but audited" — refused without the force flag (§10).

    Why: the profile is part of `runs.config_sha256` and of the `llm_cache` key, so swapping it
    mid-run silently re-prices and re-answers everything the run has left to do.
    """
    result = runner.invoke(
        app, [*base_args(workspace), "--profile", "default", "resume", "--dry-run"]
    )
    assert result.exit_code == ExitCode.USAGE
    assert "--force-config-drift" in result.output


# --------------------------------------------------------------------------------------
# §11.4 — no secret reaches stdout or stderr
# --------------------------------------------------------------------------------------


def test_no_secret_reaches_stdout_on_the_error_path(tmp_path: Path) -> None:
    """A config whose remote carries a `github_pat_…` is refused WITHOUT quoting the token.

    Why: §9 rule 4 refuses the file, and §11.4 says the report of a leak must not itself be the
    leak — an error message that echoes the offending line writes the credential into the CI log
    the redactor exists to keep it out of. Both streams are asserted, because a redactor applied
    to stdout and forgotten on stderr is a redactor that is not applied.
    """
    config = write_config(tmp_path, repos=LEAKY_REPOS_YAML)
    result = runner.invoke(app, ["--config", str(config), "models", "list"])
    assert result.exit_code == ExitCode.USAGE, result.output
    assert LEAKY_TOKEN not in result.output
    assert "github_pat_" not in result.output
    assert "repos.yaml" in result.output


def test_secret_in_a_run_url_is_redacted_from_status_output(workspace: Path) -> None:
    """Even a token that reached the database is scrubbed on the way back out (§11.4).

    Why: redaction at write time is best-effort — rows predate patterns, and `--json` re-serializes
    whatever SQLite holds. The CLI is the last egress boundary, so it redacts unconditionally.
    """
    conn = sqlite3.connect(workspace / "state" / "fleet.db", isolation_level=None)
    try:
        conn.execute(
            "UPDATE repos SET url = ? WHERE repo_id = 'acme-commons'",
            (f"https://x-access-token:{LEAKY_TOKEN}@github.com/acme/acme-commons",),
        )
        conn.execute(
            "INSERT INTO findings (run_id, repo_id, kind, severity, fingerprint, payload, "
            "                      created_at) VALUES (?, 'acme-commons', 'PreflightFailed', "
            "'error', ?, ?, ?)",
            (
                RUN_ID,
                "c" * 64,
                json.dumps({"url": f"https://{LEAKY_TOKEN}@github.com/acme/x"}),
                "2026-08-08T12:00:00+00:00",
            ),
        )
    finally:
        conn.close()
    result = runner.invoke(app, [*base_args(workspace), "--json", "status"])
    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert LEAKY_TOKEN not in result.output


# --------------------------------------------------------------------------------------
# typed errors never escape as tracebacks
# --------------------------------------------------------------------------------------


def test_missing_database_is_a_message_not_a_traceback(tmp_path: Path) -> None:
    """A typed internal error exits with its documented code and an actionable line (Rule 11).

    Why: exit 1 with a traceback tells CI "unexpected error" and tells the operator nothing about
    what to do. Here the refusal names the file AND the verb that creates it.
    """
    write_config(tmp_path)
    result = runner.invoke(
        app,
        ["--config", str(tmp_path / "config/fleet.yaml"), "--db", str(tmp_path / "nope.db"),
         "status"],
        catch_exceptions=False,
    )
    assert result.exit_code == ExitCode.USAGE
    assert "Traceback" not in result.output
    assert "migrate-db" in result.output


def test_unknown_run_id_names_the_run(workspace: Path) -> None:
    """`--run` naming nothing is exit 2 with the id echoed, not a `NoneType` crash later."""
    result = runner.invoke(app, [*base_args(workspace), "--run", "does-not-exist", "status"])
    assert result.exit_code == ExitCode.USAGE
    assert "does-not-exist" in result.output


def test_unimplemented_verb_names_the_stub_module(workspace: Path) -> None:
    """A verb whose worker is still a stub exits 1 with the module named — never a bare
    `NotImplementedError` traceback. Rule 11: fail loud, and say which file to open.

    Retargeted from `scan` to `plan` when `fleet scan` was wired to the real workers: the
    property under test is "a stubbed verb names its stub", not "scan is stubbed", so it has to
    follow the stubs rather than pin the harness to its own incompleteness.
    """
    result = runner.invoke(app, [*base_args(workspace), "plan"], catch_exceptions=False)
    assert result.exit_code == ExitCode.UNEXPECTED_ERROR
    assert "NotImplementedError" in result.output
    assert "workers/relocate.py" in result.output
    assert "Traceback" not in result.output


# --------------------------------------------------------------------------------------
# read-only verbs
# --------------------------------------------------------------------------------------


def test_status_json_is_machine_readable(workspace: Path) -> None:
    """`--json` is a contract: §12's criteria are asserted by parsing this, not by eyeballing."""
    _put_in_flight(workspace / "state" / "fleet.db", "acme-commons", "RUNNING")
    result = runner.invoke(app, [*base_args(workspace), "--json", "status"])
    assert result.exit_code == ExitCode.SUCCESS, result.output
    payload = json.loads(result.stdout)
    assert payload["run_id"] == RUN_ID
    assert payload["repos"][0]["repo"] == "acme-commons"


def test_status_metrics_out_writes_prometheus_text(workspace: Path) -> None:
    """`--metrics-out` is an OUTPUT file, not a server: §14.5 stands and nothing binds a port."""
    out = workspace / "metrics.prom"
    result = runner.invoke(
        app, [*base_args(workspace), "status", "--metrics", "--metrics-out", str(out)]
    )
    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert out.read_text(encoding="utf-8").startswith("# TYPE")


def test_status_digest_is_the_run_equivalence_proof(workspace: Path) -> None:
    """`--digest` emits the §11.6 `run_digest` two runs are proven equivalent by."""
    result = runner.invoke(app, [*base_args(workspace), "--json", "status", "--digest"])
    assert result.exit_code == ExitCode.SUCCESS, result.output
    payload = json.loads(result.stdout)
    assert len(payload["digest"]) == 64


def test_models_list_renders_an_undeclared_effort_as_a_dash(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0075 made `effort` optional, so a target can now resolve with `effort is None`. The
    human table must not hand that to an operator as the Python literal `None` — `fleet models
    list` is the "what will this run actually call" pre-flight, and `effort=None` reads as a
    value that was set rather than one that was never declared.

    The router is monkeypatched rather than the YAML edited because on a base where
    `BackendTarget.effort` is still `Literal[...] = "medium"` there is no way to express the
    post-ADR-0075 shape in `config/models.yaml` at all — omitting the key substitutes the
    default, which is the very defect ADR-0075 removes. This drives the real `models_list`
    rendering path over a real `LlmRouter`; only the target's `effort` is forced.
    """
    from fleet import cli as cli_module
    from fleet.llm.roles import LlmRouter

    resolve = cli_module.llm_router

    def undeclared(settings: Any) -> LlmRouter:
        router = resolve(settings)
        return LlmRouter(
            {name: route.tier for name, route in router.routes()},
            {
                route.tier: tuple(
                    target.model_copy(update={"effort": None}) for target in route.targets
                )
                for _, route in router.routes()
            },
            profile=settings.profile,
            required_roles=(),
        )

    monkeypatch.setattr(cli_module, "llm_router", undeclared)

    table = runner.invoke(app, [*base_args(workspace), "models", "list"])
    assert table.exit_code == ExitCode.SUCCESS, table.output
    assert "effort=-" in table.output
    assert "effort=None" not in table.output

    # The machine path is deliberately NOT dashed: `null` is the correct JSON answer for a value
    # the operator never declared, and a consumer must be able to tell it from a literal "-".
    payload = json.loads(
        runner.invoke(app, [*base_args(workspace), "--json", "models", "list"]).stdout
    )
    assert {row["effort"] for row in payload["routes"]} == {None}


def test_models_list_resolves_the_active_profile_offline(workspace: Path) -> None:
    """`fleet models list` is the "what will this run actually call" pre-flight — no network.

    Why: `--profile` changes which targets every role resolves through, and both `backend` and
    `model_id` are `llm_cache` key components. An operator has to be able to see the resolution
    before paying for it.
    """
    result = runner.invoke(app, [*base_args(workspace), "--json", "models", "list"])
    assert result.exit_code == ExitCode.SUCCESS, result.output
    payload = json.loads(result.stdout)
    assert payload["profile"] == "default"
    assert {row["tier"] for row in payload["routes"]} == {"HEAVY", "WORKHORSE", "CHEAP"}


@pytest.mark.skipif(
    "openai_compatible" not in llm_discover(),
    reason="the `local` profile routes every tier through `openai_compatible`, which has no "
    "adapter under src/fleet/llm/backends/ yet. Since the CLI startup path began passing the "
    "LIVE "
    "§7.7 registry to FleetSettings.load(known_backends=...), §9 rule 2 correctly refuses that "
    "profile with exit 2 — the gate doing exactly its job, not a regression. This is a real "
    "capability probe in the conftest idiom, not an xfail: it re-arms and asserts the original "
    "behaviour by itself the moment the openai_compatible backend registers.",
)
def test_profile_flag_selects_the_profile_every_role_resolves_through(tmp_path: Path) -> None:
    """`--profile local` reaches `LlmRouter`, which is the defect this wiring closes (§10).

    Why: a `--profile` that is parsed and dropped means "swap the fleet to local models" silently
    keeps calling the hosted profile — and because backend/model_id are cache-key components, the
    operator also gets the other profile's cached answers replayed at them.
    """
    config = write_config(tmp_path, models=MODELS_YAML + LOCAL_PROFILE_YAML)
    result = runner.invoke(
        app, ["--config", str(config), "--profile", "local", "--json", "models", "list"]
    )
    assert result.exit_code == ExitCode.SUCCESS, result.output
    payload = json.loads(result.stdout)
    assert payload["profile"] == "local"
    assert {row["model_id"] for row in payload["routes"]} == {
        "local-heavy",
        "local-workhorse",
        "local-cheap",
    }


def test_llm_cache_flag_reaches_the_caching_client(workspace: Path) -> None:
    """`--llm-cache off` reaches `CachingModelClient.mode`, not just the parser (§11.6).

    Why: this is the other half of the named defect. A `--llm-cache off` that is accepted and
    dropped means an operator trying to reproduce a result gets last week's cached answers
    replayed — and, because a hit is indistinguishable from a fresh call at that boundary except
    for cost, nothing in the output says so.
    """
    from fleet.cli import GlobalOptions, LlmCacheMode, caching_client
    from fleet.llm.cache import MemoryLlmCacheStore
    from fleet.settings import FleetSettings

    surfaced = runner.invoke(
        app, [*base_args(workspace), "--llm-cache", "off", "--json", "models", "list"]
    )
    assert surfaced.exit_code == ExitCode.SUCCESS, surfaced.output
    assert json.loads(surfaced.stdout)["llm_cache"] == "off"

    settings = FleetSettings.load(workspace / "config")
    for flag, expected in (
        (LlmCacheMode.OFF, "off"),
        (LlmCacheMode.READ_ONLY, "read-only"),
        (LlmCacheMode.READ_WRITE, "read-write"),
    ):
        client = caching_client(
            object(),  # type: ignore[arg-type]  - the decorator never calls it here
            settings,
            MemoryLlmCacheStore(),
            GlobalOptions(llm_cache=flag),
        )
        assert client._mode == expected


def test_gc_refuses_to_evict_under_live_work(workspace: Path) -> None:
    """`fleet gc` refuses a run with live phases unless `--force` (§10).

    Why: `gc` trims `events`/`attempts` and the LLM cache. Doing that beneath a RUNNING worker
    deletes the evidence of the attempt currently in progress.
    """
    _put_in_flight(workspace / "state" / "fleet.db", "acme-commons", "RUNNING")
    refused = runner.invoke(app, [*base_args(workspace), "gc"])
    assert refused.exit_code == ExitCode.USAGE
    assert "--force" in refused.output

    forced = runner.invoke(app, [*base_args(workspace), "gc", "--force", "--dry-run"])
    assert forced.exit_code == ExitCode.SUCCESS, forced.output


def test_abort_checkpoints_and_regenerates_the_projection(workspace: Path) -> None:
    """`fleet abort` exits 0, resets RUNNING rows and rewrites `migration_state.json` (§10).

    Why: "any non-zero exit leaves a valid checkpoint and a regenerated `migration_state.json`" —
    the deliberate stop must uphold the same invariant, or a clean abort is worse than a crash.
    """
    _put_in_flight(workspace / "state" / "fleet.db", "acme-commons", "RUNNING")
    result = runner.invoke(app, [*base_args(workspace), "abort", "--reason", "operator"])
    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert (workspace / "migration_state.json").exists()

    conn = sqlite3.connect(workspace / "state" / "fleet.db")
    try:
        status, fence = conn.execute(
            "SELECT status, lease_fence FROM phases WHERE run_id = ? AND repo_id = 'acme-commons'",
            (RUN_ID,),
        ).fetchone()
    finally:
        conn.close()
    assert status == "PENDING"
    assert fence == 1


# --------------------------------------------------------------------------------------
# §11.5 — what `fleet resume` reconciles today (steps 1, 3, 7, --repoll-prs, --raise-budget)
# --------------------------------------------------------------------------------------

IMPATIENT_STALE_YAML = (
    "run:\n  monorepo_path: ../acme-monorepo\n  stale_after_s: 30\n"
    "preflight:\n  min_free_bytes: 1048576\n"
)
"""`FLEET_YAML` with the config liveness horizon cut to 30 s — the operator action that makes the
two clocks disagree, since `phases.heartbeat_ttl_seconds` stays at the schema's 300."""


def _reseal_config_digests(workspace: Path) -> None:
    """Record TODAY's per-section digests as the run's baseline, after a test edits `config/`.

    A test that changes `config/fleet.yaml` to exercise something else would otherwise be testing
    §10 drift detection instead: the resume refuses at step 1 and never reaches the code under
    test.
    """
    from fleet.settings import FleetSettings

    settings = FleetSettings.load(workspace / "config")
    conn = sqlite3.connect(workspace / "state" / "fleet.db", isolation_level=None)
    try:
        conn.execute(
            "UPDATE runs SET config_digests = ? WHERE run_id = ?",
            (json.dumps(dict(settings.section_digests), sort_keys=True), RUN_ID),
        )
    finally:
        conn.close()


STALE_HEARTBEAT = "2020-01-01T00:00:00.000000+00:00"
"""Older than any `run.stale_after_s` a config could name, so the sweep's gate is exercised by
the AGE of the heartbeat and not by an artificially tiny TTL."""


def _put_leased(
    db: Path,
    repo: str,
    *,
    heartbeat_at: str | None,
    attempts: int = 2,
    fence: int = 4,
    phase: int = 2,
    ttl_s: int = 300,
) -> None:
    """One `RUNNING` phase row as a live worker would have left it: an owner, a fence it was
    granted at claim time, `attempts` already spent on real work, and the per-phase liveness TTL.

    `ttl_s` defaults to 300 because that is what every real row carries: `claim_phase` never
    writes `heartbeat_ttl_seconds`, so `schema.sql`'s DEFAULT is what production rows hold
    regardless of `run.stale_after_s`.
    """
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO phases (run_id, repo_id, phase, status, attempts, heartbeat_at, "
            "                    heartbeat_ttl_seconds, lease_owner, lease_fence, "
            "                    lease_expires_at, updated_at) "
            "VALUES (?, ?, ?, 'RUNNING', ?, ?, ?, 'host:cid:1:boot', ?, ?, ?)",
            (
                RUN_ID, repo, phase, attempts, heartbeat_at, ttl_s, fence,
                heartbeat_at, "2026-08-08T12:00:00+00:00",
            ),
        )
    finally:
        conn.close()


def _phase_row(db: Path, repo: str) -> tuple[Any, ...]:
    conn = sqlite3.connect(db)
    try:
        return tuple(
            conn.execute(
                "SELECT status, attempts, lease_fence, lease_owner, heartbeat_at FROM phases "
                " WHERE run_id = ? AND repo_id = ?",
                (RUN_ID, repo),
            ).fetchone()
        )
    finally:
        conn.close()


def test_resume_reclaims_a_stale_lease_without_charging_an_attempt(workspace: Path) -> None:
    """§11.5 step 3: a dead worker's row returns to PENDING, keeps `attempts`, bumps the fence.

    Why each half matters, and neither is cosmetic:

    * **`attempts` is retained.** A crash is not an attempt the repo made. If a resume charged
      one, three resumes would exhaust ADR-0014's three-rung ladder and send a repo that never
      failed a transform to REQUIRES_HUMAN_INTERVENTION — the harness would libel it, and the
      operator would go looking for a defect that does not exist.
    * **`lease_fence` is bumped.** Resetting the status alone is what produces two writers on
      `migrate/<repo>`: the row is re-admitted while the original container is still alive
      enough to finish a `git apply`. Every write that container issues carries
      `AND lease_fence = ?`, so the bump — and only the bump — makes its next write match zero
      rows and abort.
    """
    db = workspace / "state" / "fleet.db"
    _put_leased(db, "acme-commons", heartbeat_at=STALE_HEARTBEAT, attempts=2, fence=4)

    result = runner.invoke(app, [*base_args(workspace), "resume"])
    # Step 5 is not built, so the verb still refuses — but AFTER the reconciliation is durable,
    # and with exit 2, which is "retrying unchanged is futile", not "the harness crashed".
    assert result.exit_code == ExitCode.USAGE, result.output

    status, attempts, fence, owner, heartbeat = _phase_row(db, "acme-commons")
    assert status == "PENDING"
    assert attempts == 2, "a resume charged the repo an attempt it never spent"
    assert fence == 5, "the lease was handed back without invalidating the old holder's writes"
    assert owner is None
    assert heartbeat is None
    assert (workspace / "migration_state.json").exists(), "§11.5 step 7 did not run"


def test_resume_leaves_a_lease_that_is_still_heart_beating_alone(workspace: Path) -> None:
    """A `RUNNING` row whose heartbeat is fresh survives the sweep untouched.

    Why: `fleet resume` can be run against a fleet that is still partly in flight (that is the
    point of `--dry-run` as a health check). Reclaiming a live worker's lease is exactly the
    two-writer collision the lease exists to prevent, and the fence bump would make the live
    worker abort mid-phase for no reason.
    """
    db = workspace / "state" / "fleet.db"
    fresh = datetime.now(UTC).isoformat(timespec="microseconds")
    _put_leased(db, "acme-commons", heartbeat_at=fresh, attempts=1, fence=7)

    result = runner.invoke(app, [*base_args(workspace), "resume"])
    assert result.exit_code == ExitCode.USAGE, result.output

    status, attempts, fence, owner, _heartbeat = _phase_row(db, "acme-commons")
    assert (status, attempts, fence, owner) == ("RUNNING", 1, 7, "host:cid:1:boot")


def test_resume_dry_run_previews_the_sweep_and_writes_nothing(workspace: Path) -> None:
    """§11.5: "steps 1–7 make no network call and invoke no model, so a resume is free and can be
    run as a dry-run health check". A preview that mutated the ledger would not be one.

    Why the COUNT is asserted and not just the exit code: a dry run that reported 0 for a fleet
    with a dead worker tells the operator the run is healthy, which is the one answer a health
    check must never get wrong.
    """
    db = workspace / "state" / "fleet.db"
    _put_leased(db, "acme-commons", heartbeat_at=STALE_HEARTBEAT)

    result = runner.invoke(app, [*base_args(workspace), "--json", "resume", "--dry-run"])
    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert json.loads(result.stdout)["stale_running_reset"] == 1

    status, attempts, fence, owner, _hb = _phase_row(db, "acme-commons")
    assert (status, attempts, fence, owner) == ("RUNNING", 2, 4, "host:cid:1:boot")
    assert not (workspace / "migration_state.json").exists()


def test_resume_refuses_the_flags_whose_behaviour_does_not_exist(workspace: Path) -> None:
    """`--from-phase` is exit 2, not a silently discarded argument.

    Why: every flag refused here scopes or re-drives §11.5 step 5, which has no implementation.
    A parser that accepted `--from-phase 2` and then resumed from wherever it liked leaves the
    operator believing they scoped the resume — and nothing anywhere tells them otherwise.
    """
    result = runner.invoke(app, [*base_args(workspace), "resume", "--from-phase", "2"])
    assert result.exit_code == ExitCode.USAGE, result.output
    assert "--from-phase" in result.output
    assert "step 5" in result.output


# ---- --repoll-prs -------------------------------------------------------------------


def _seed_pr_record(db: Path, repo: str, *, state: str, url: str) -> None:
    """One persisted `PullRequestDraft`, written exactly where `fleet pr` writes it."""
    from fleet.cli import PR_RECORD_KIND, _fingerprint

    payload = json.dumps(
        {
            "run_id": RUN_ID, "repo_id": repo, "wave_index": 0,
            "branch": f"migrate/{repo}", "base": "integration",
            "title": f"migrate {repo}", "body": "body",
            "source_url": f"https://example.invalid/{repo}", "source_sha": "a" * 40,
            "state": state, "url": url, "created_at": "2026-08-08T12:00:00+00:00",
        }
    )
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO findings (run_id, repo_id, kind, severity, fingerprint, payload, "
            "                      created_at) VALUES (?, ?, ?, 'info', ?, ?, ?)",
            (
                RUN_ID, repo, PR_RECORD_KIND,
                _fingerprint(RUN_ID, repo, PR_RECORD_KIND), payload,
                "2026-08-08T12:00:00+00:00",
            ),
        )
    finally:
        conn.close()


class _MergedForge:
    """`gh pr view` answering MERGED, and nothing else. Records every invocation."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    async def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        deadline: float | None = None,
        timeout_s: float | None = None,
    ) -> ProcResult:
        _ = (cwd, env, deadline, timeout_s)
        call = tuple(argv)
        self.calls.append(call)
        stdout = ""
        if call[1:3] == ("auth", "status"):
            stdout = "Logged in to github.invalid\n"
        elif call[1:3] == ("pr", "view"):
            stdout = json.dumps(
                {
                    "state": "MERGED",
                    "mergedAt": "2026-08-09T12:00:00Z",
                    "mergeCommit": {"oid": "f" * 40},
                }
            )
        else:  # pragma: no cover - an unrecognised argv is a test bug, loudly
            raise AssertionError(f"unexpected gh invocation: {call}")
        return ProcResult(
            argv=call, exit_code=0, stdout_tail=stdout, stderr_tail="",
            duration_ms=1, timed_out=False,
        )


def test_resume_repoll_prs_ingests_the_merge_the_harness_never_saw(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`fleet resume --repoll-prs` runs §3.4 step 5 once and the `pr_merged` event lands.

    Why this flag exists at all: `MERGED` is a fact about the forge that three gates consume (the
    §3.4 stacking precondition, the §3.5 `blocked_by` release, the §3.5.1 T1 stub trigger) and
    nothing in the harness can produce by reasoning. A run whose orchestrator died after wave 0
    opened its PRs has no poller left; without this flag the operator's only options are to leave
    every later wave blocked forever or to run a second verb the §11.5 sequence does not mention.

    It is the same `_pr_sync_impl` `fleet pr --sync` calls — deliberately not a second sync path,
    because `MERGED` has exactly one writer.
    """
    from fleet import cli

    db = workspace / "state" / "fleet.db"
    url = "https://github.invalid/acme/monorepo/pull/1"
    _seed_pr_record(db, "acme-commons", state="DRAFTED", url=url)
    forge = _MergedForge()
    monkeypatch.setattr(cli, "GH_RUNNER", forge)

    result = runner.invoke(app, [*base_args(workspace), "resume", "--repoll-prs"])
    assert result.exit_code == ExitCode.USAGE, result.output

    conn = sqlite3.connect(db)
    try:
        stored = json.loads(
            conn.execute(
                "SELECT payload FROM findings WHERE kind = 'PullRequest'"
            ).fetchone()[0]
        )
        events = [
            row[0] for row in conn.execute("SELECT event FROM events WHERE run_id = ?", (RUN_ID,))
        ]
    finally:
        conn.close()
    assert stored["state"] == "MERGED", "the re-poll did not write what the forge said"
    assert "pr_merged" in events, "nothing unblocks the dependent without this event"
    assert [c for c in forge.calls if c[1:3] == ("pr", "view")], "the forge was never asked"


def test_resume_dry_run_never_reaches_the_forge_even_with_repoll_prs(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--dry-run --repoll-prs` exits 0 without a single `gh` invocation.

    Why: §11.5 promises a resume's reconciliation "makes no network call and invokes no model",
    which is the entire basis for using `fleet resume --dry-run` as a health check in CI and on a
    disconnected operator laptop. A preview that silently polled 250 PRs would break that promise
    in the one mode that advertises it.
    """
    from fleet import cli

    _seed_pr_record(
        workspace / "state" / "fleet.db", "acme-commons", state="DRAFTED",
        url="https://github.invalid/acme/monorepo/pull/1",
    )

    async def refuse(*_args: Any, **_kwargs: Any) -> ProcResult:  # pragma: no cover
        raise AssertionError("a --dry-run resume invoked the forge")

    monkeypatch.setattr(cli, "GH_RUNNER", refuse)
    result = runner.invoke(app, [*base_args(workspace), "resume", "--dry-run", "--repoll-prs"])
    assert result.exit_code == ExitCode.SUCCESS, result.output


# ---- --raise-budget -----------------------------------------------------------------


def _halt_ledger(db: Path, *, spent: float = 10.0, max_usd: float = 10.0) -> None:
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO budget_ledger (run_id, spent_usd, reserved_usd, max_usd, halted, "
            "                           updated_at) VALUES (?, ?, 0.0, ?, 1, ?)",
            (RUN_ID, spent, max_usd, "2026-08-08T12:00:00+00:00"),
        )
    finally:
        conn.close()


def _ledger(db: Path) -> tuple[float, int]:
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT max_usd, halted FROM budget_ledger WHERE run_id = ?", (RUN_ID,)
        ).fetchone()
    finally:
        conn.close()
    return float(row[0]), int(row[1])


def test_raise_budget_clears_the_sticky_halt_and_is_audited(workspace: Path) -> None:
    """`fleet resume --raise-budget` is the ONLY writer that can clear `budget_ledger.halted`.

    Why: exit 3 is durable and deliberately sticky — the reservation CAS itself carries
    `AND halted = 0`, so once set, no worker in any process can dispatch, in this run or any
    later one. §10 names `--raise-budget` as the way out; before this, the flag was accepted and
    discarded, so the documented recovery from a run-level budget stop silently did nothing and
    the run was unrecoverable. And an unaudited raise is an unexplained budget in next week's
    report, so the finding is written in the same transaction as the number.
    """
    db = workspace / "state" / "fleet.db"
    _halt_ledger(db, spent=10.0, max_usd=10.0)

    result = runner.invoke(app, [*base_args(workspace), "resume", "--raise-budget", "50"])
    assert result.exit_code == ExitCode.USAGE, result.output

    assert _ledger(db) == (50.0, 0)
    conn = sqlite3.connect(db)
    try:
        payloads = [
            json.loads(row[0])
            for row in conn.execute(
                "SELECT payload FROM findings WHERE kind = 'RunBudgetRaised'"
            )
        ]
    finally:
        conn.close()
    assert payloads == [
        {"halt_cleared": True, "new_max_usd": 50.0, "raised_by": "--raise-budget"}
    ]


def test_raise_budget_below_committed_spend_is_refused_with_the_real_numbers(
    workspace: Path,
) -> None:
    """A ceiling under `spent_usd + reserved_usd` is exit 2, and the ledger is untouched.

    Why: `budget_ledger` CHECKs `spent_usd + reserved_usd <= max_usd`, so the alternative to this
    refusal is an `IntegrityError` traceback that names neither the number the operator typed nor
    the number they needed — and a half-cleared halt would be worse than the halt.
    """
    db = workspace / "state" / "fleet.db"
    _halt_ledger(db, spent=10.0, max_usd=10.0)

    result = runner.invoke(app, [*base_args(workspace), "resume", "--raise-budget", "1"])
    assert result.exit_code == ExitCode.USAGE, result.output
    assert "10.00" in result.output
    assert _ledger(db) == (10.0, 1), "a refused raise still moved the ledger"


def test_resume_will_not_reclaim_a_row_the_per_phase_ttl_still_calls_alive(
    workspace: Path,
) -> None:
    """Lowering `run.stale_after_s` must NOT let a resume reclaim a lease the harness calls live.

    Why this is the dangerous one. Two clocks answer "is this worker dead":
    `run.stale_after_s` (live config, named by §11.5 step 3) and `phases.heartbeat_ttl_seconds`
    (per row, what `PhaseRecord.is_stale` and every liveness check in the orchestrator compare
    against). `settings.py` says the column exists so "a config change cannot retroactively
    declare a live worker dead" — but `claim_phase` never writes it, so every real row carries
    the schema default of 300 no matter what the operator configured, and the two clocks CAN
    disagree.

    The scenario, if the sweep trusted config alone: an operator sets `stale_after_s: 30` to make
    the reaper responsive. Live workers heart-beating every few seconds hold rows stamped
    `ttl = 300`. A `fleet resume` — which `--dry-run`'s framing as a health check actively invites
    running against a live fleet — would see every heartbeat older than 30 s, reset live `RUNNING`
    rows to `PENDING` and bump their fences, while `is_stale` and `reap_expired_phase_leases`
    both still call those workers alive. That is two writers on `migrate/<repo>`.

    So the gate is the CONJUNCTION of both horizons, and this test pins the conservative side:
    60 s of silence, a 30 s config horizon, a 300 s row horizon — not reclaimed.
    """
    db = workspace / "state" / "fleet.db"
    write_config(workspace, fleet=IMPATIENT_STALE_YAML)
    # Re-seal, or the resume refuses for config drift and never reaches the sweep under test.
    _reseal_config_digests(workspace)
    sixty_s_ago = (datetime.now(UTC) - timedelta(seconds=60)).isoformat(timespec="microseconds")
    _put_leased(db, "acme-commons", heartbeat_at=sixty_s_ago, attempts=3, fence=9, ttl_s=300)

    result = runner.invoke(app, [*base_args(workspace), "resume"])
    assert result.exit_code == ExitCode.USAGE, result.output

    status, attempts, fence, owner, _hb = _phase_row(db, "acme-commons")
    assert (status, attempts, fence, owner) == ("RUNNING", 3, 9, "host:cid:1:boot"), (
        "a lowered `run.stale_after_s` reclaimed a lease the per-phase TTL still calls live"
    )


def test_resume_reclaims_once_both_horizons_are_breached(workspace: Path) -> None:
    """The other half of the conjunction: a row past BOTH clocks is still swept.

    Why it is asserted separately: a gate made conservative by breaking it would pass the test
    above and quietly stop reconciling anything, which is the failure §11.5 step 3 exists to
    prevent. Same fixture, same config, one row whose silence exceeds the 300 s per-row TTL too.
    """
    db = workspace / "state" / "fleet.db"
    long_gone = (datetime.now(UTC) - timedelta(seconds=3600)).isoformat(timespec="microseconds")
    _put_leased(db, "acme-commons", heartbeat_at=long_gone, attempts=3, fence=9, ttl_s=300)

    result = runner.invoke(app, [*base_args(workspace), "resume"])
    assert result.exit_code == ExitCode.USAGE, result.output

    status, attempts, fence, owner, _hb = _phase_row(db, "acme-commons")
    assert (status, attempts, fence, owner) == ("PENDING", 3, 10, None)


# --------------------------------------------------------------------------------------
# §11.5 step 2 — the orphan reap (ADR-0081)
# --------------------------------------------------------------------------------------

REAP_YAML = (
    "run:\n  monorepo_path: monorepo\n  work_dir: work/\n"
    "preflight:\n  min_free_bytes: 1048576\n"
)
"""`FLEET_YAML` with the repo INSIDE the workspace. The shipped fixture says `../acme-monorepo`,
which resolves above `tmp_path` into pytest's shared tmp base — a directory a sibling test (or a
sibling lane) could be writing at the same time. A reap test that cut real worktrees there would
be reaping in a tree it does not own."""


def _proc(argv: Sequence[str], exit_code: int, stdout: str = "", stderr: str = "",
          *, started: bool = True, timed_out: bool = False) -> ProcResult:
    return ProcResult(
        argv=tuple(argv), exit_code=exit_code, stdout_tail=stdout, stderr_tail=stderr,
        duration_ms=1, timed_out=timed_out, started=started,
    )


class _ScriptedDocker:
    """The docker CLI at `util.proc.run`'s interface, so `ContainerSandbox` itself is under test.

    A hand-written fake of `ContainerSandbox` would assert this module's *expectations* of the
    reap rather than the reap; injecting at the subprocess boundary keeps `reap()`, `claims()`
    and `_remove_with_reason` on the tested path, which is where the sparing rule actually lives.
    """

    def __init__(
        self,
        present: Sequence[str] = (),
        rm_fails: Sequence[str] = (),
        starts_after_first_listing: str | None = None,
    ) -> None:
        self.present = list(present)
        self.rm_fails = set(rm_fails)
        self.removed: list[str] = []
        self.listings = 0
        #: A container that appears only once the sweep has already listed once — a build that
        #: started while step 2 was running. Nothing can make it appear to a sweep that lists a
        #: single time, which is the point: see the test that uses it.
        self._late = starts_after_first_listing

    async def __call__(self, argv: Sequence[str], **_kw: Any) -> ProcResult:
        args = list(argv)
        if args[1:3] == ["ps", "--all"]:
            pattern = args[args.index("--filter") + 1].removeprefix("name=")
            hits = [n for n in self.present if re.match(pattern, n)]
            self.listings += 1
            if self._late is not None and self.listings == 1:
                self.present.append(self._late)
            return _proc(args, 0, "\n".join(hits))
        if args[1:3] == ["rm", "--force"]:
            name = args[3]
            if name in self.rm_fails:
                return _proc(args, 1, stderr=f"Error response from daemon: {name} is in use")
            self.removed.append(name)
            self.present.remove(name)
            return _proc(args, 0)
        raise AssertionError(f"the reap ran an unexpected docker command: {args}")


class _NeverSettlingGit:
    """A `git` whose `worktree remove` never starts — D44's unsettled probe, which
    `WorktreeManager.remove` must refuse to treat as a licence to delete."""

    def __init__(self, registered: Sequence[Path]) -> None:
        self.registered = list(registered)

    async def __call__(self, argv: Sequence[str], **_kw: Any) -> ProcResult:
        args = list(argv)
        if args[3:5] == ["worktree", "list"]:
            body = "".join(f"worktree {p}\n\n" for p in self.registered)
            return _proc(args, 0, body)
        if args[3:5] == ["worktree", "remove"]:
            return _proc(args, 124, started=False, timed_out=True)
        if args[3:5] == ["worktree", "prune"]:
            return _proc(args, 0)
        raise AssertionError(f"the reap ran an unexpected git command: {args}")


@pytest.fixture(autouse=True)
def _no_test_may_reach_the_host_docker_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    """`fleet resume` now sweeps containers, and every resume test would otherwise `docker ps`
    against whatever daemon the developer has running — and `docker rm --force` anything it
    matched. Autouse, because the hazard belongs to the code under test and not to the tests that
    remember to opt out of it: the default here is a daemon that reports nothing."""
    monkeypatch.setattr("fleet.cli._reap_container_sandbox", lambda: ContainerSandbox(
        runner=_ScriptedDocker()
    ))


def _git_in(repo: Path, *args: str) -> None:
    """One fixed-argv `git`, never a shell. The single subprocess site in this section, so the
    two bandit suppressions are asserted once rather than at every call — the same shape
    `tests/test_transform_e2e.py` and `tests/test_scan_e2e.py` already use for their git
    fixtures. `git` comes from PATH deliberately: these fixtures must exercise the same binary
    `WorktreeManager` will invoke, and that is whatever `git_bin="git"` resolves to."""
    subprocess.run(  # noqa: S603 - fixed argv built here, never a shell, no test input
        ["git", *args],  # noqa: S607 - `git` from PATH, as every suite in this repo does
        cwd=repo, check=True, capture_output=True,
    )


def _reap_workspace(workspace: Path) -> Path:
    """Repoint the run at a real git repo inside `tmp_path` and return it."""
    write_config(workspace, fleet=REAP_YAML)
    _reseal_config_digests(workspace)
    repo = workspace / "monorepo"
    repo.mkdir()
    _git_in(repo, "init", "--initial-branch=main", ".")
    _git_in(repo, "config", "user.email", "fleet@example.invalid")
    _git_in(repo, "config", "user.name", "Fleet Test")
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git_in(repo, "add", "README.md")
    _git_in(repo, "commit", "-m", "initial")
    return repo


def _cut(repo: Path, workspace: Path, name: str) -> Path:
    """A REAL `git worktree add`, so the sweep is validated against git's own registry rather
    than against a listing the test wrote itself."""
    path = workspace / "work" / name
    _git_in(repo, "worktree", "add", "--detach", str(path), "HEAD")
    return path


def _sandbox(repo_id: str, attempt: int) -> str:
    return f"fleet-{RUN_ID}-{repo_id}-{attempt}"


def test_resume_reap_spares_the_rung_the_live_row_is_actually_on(workspace: Path) -> None:
    """A live row at `attempts = 2` is working in `-3`, NOT in `-2`, and `-3` must survive.

    Why `+ 1` is the whole test and not an off-by-one nicety: `phases.attempts` is a *charged*
    counter and the sandbox name carries a *rung* number. `PhaseRunner._dispatch` dispatches with
    `attempt = ladder.attempts + 1` (`orchestrator/runner.py:686`), so the checkout a live worker
    holds open right now is always one past what its row records. A reap that derived live names
    from `attempts` alone would classify every live sandbox in the fleet as an orphan and delete
    the checkout of every worker still running — the sweep would be at its most destructive
    exactly when the fleet is at its healthiest.

    `-2` is spared too, and deliberately: it is the rung whose charge has already landed and
    whose sandbox the same worker may still be tearing down (`workers/rdepverify.py:331` removes
    `sandbox_name(..., ctx.attempt)` on cancel). Sparing an orphan costs disk until the next
    resume; reaping a live checkout costs the run.
    """
    repo = _reap_workspace(workspace)
    db = workspace / "state" / "fleet.db"
    fresh = datetime.now(UTC).isoformat(timespec="microseconds")
    _put_leased(db, "acme-commons", heartbeat_at=fresh, attempts=2)

    live_rung = _cut(repo, workspace, _sandbox("acme-commons", 3))
    teardown_rung = _cut(repo, workspace, _sandbox("acme-commons", 2))
    orphan = _cut(repo, workspace, _sandbox("acme-commons", 1))

    result = runner.invoke(app, [*base_args(workspace), "--json", "resume"])
    assert result.exit_code == ExitCode.USAGE, result.output
    payload = json.loads(result.stdout)

    assert _sandbox("acme-commons", 3) in payload["live_sandbox_names"], (
        "the live set was derived from `attempts` without the `+ 1` the dispatcher adds"
    )
    assert payload["reaped_worktrees"]["reaped"] == [_sandbox("acme-commons", 1)]
    assert live_rung.exists(), "the reap deleted the checkout a live worker is writing into"
    assert teardown_rung.exists()
    assert not orphan.exists()


def test_resume_reap_is_silent_on_a_clean_tree_and_fires_on_one_injected_into_it(
    workspace: Path,
) -> None:
    """The third of the three validations CLAUDE.md guardrail 6 requires, and the only one that
    can catch a detector broken on FRESH instances rather than on the fixture it was developed
    against: same workspace, same driver, one synthetic orphan injected between two invocations.

    A detector validated only against a pre-planted orphan can pass while being silently inert on
    anything created after setup — and this sweep's whole job is to find debris that appeared
    after the harness stopped looking.
    """
    repo = _reap_workspace(workspace)

    clean = runner.invoke(app, [*base_args(workspace), "--json", "resume"])
    assert clean.exit_code == ExitCode.USAGE, clean.output
    assert json.loads(clean.stdout)["reaped_worktrees"]["reaped"] == []

    injected = _cut(repo, workspace, _sandbox("acme-commons", 4))

    after = runner.invoke(app, [*base_args(workspace), "--json", "resume"])
    assert after.exit_code == ExitCode.USAGE, after.output
    assert json.loads(after.stdout)["reaped_worktrees"]["reaped"] == [_sandbox("acme-commons", 4)]
    assert not injected.exists(), "the detector was inert on a worktree created after setup"


def test_resume_reap_cannot_reach_a_worktree_outside_the_run_namespace(workspace: Path) -> None:
    """Two neighbours registered in the same repo survive: another run's sandbox, and a worktree
    with no `fleet-` prefix at all.

    Why this is asserted rather than assumed: the repository holds worktrees that are evidence,
    not debris — `worktrees/wt-WT1-example` is quoted in ADR-0074 and is a live fixture for
    another lane. A reap whose predicate widened to "anything registered that no row claims"
    would remove them, and would look like a passing sweep while doing it.
    """
    repo = _reap_workspace(workspace)
    other_run = _cut(repo, workspace, "fleet-99999999-9999-4999-8999-999999999999-acme-1")
    unrelated = _cut(repo, workspace, "wt-WT1-example")
    orphan = _cut(repo, workspace, _sandbox("acme-commons", 1))

    result = runner.invoke(app, [*base_args(workspace), "--json", "resume"])
    assert result.exit_code == ExitCode.USAGE, result.output

    assert json.loads(result.stdout)["reaped_worktrees"]["reaped"] == [_sandbox("acme-commons", 1)]
    assert other_run.exists(), "the sweep crossed into another run's namespace"
    assert unrelated.exists(), "the sweep removed a worktree that is not a fleet sandbox at all"
    assert not orphan.exists()


def test_resume_dry_run_names_the_orphans_it_would_reap_and_removes_none(
    workspace: Path,
) -> None:
    """`--dry-run` is a health check, so it must both leave the orphan on disk AND name it.

    A preview that removed nothing and reported nothing is indistinguishable from a clean fleet,
    which is the one answer a health check must never get wrong; a preview that reported by
    calling `reap()` and discarding the result would be a preview that reaps.
    """
    repo = _reap_workspace(workspace)
    orphan = _cut(repo, workspace, _sandbox("acme-commons", 1))

    result = runner.invoke(app, [*base_args(workspace), "resume", "--dry-run"])
    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert f"would reap 1 orphan worktree(s): {_sandbox('acme-commons', 1)}" in result.output
    assert orphan.exists(), "a --dry-run reaped a worktree"


def test_resume_reports_every_worktree_the_sweep_could_not_remove(workspace: Path) -> None:
    """D44, one layer up: `ReapResult.failed` reaches the operator by NAME and with its reason,
    and the clean headline is not printed above it.

    The failure injected is D44's own shape — a `git worktree remove` that never started, which
    `WorktreeManager.remove` refuses to read as a licence to delete. A caller that reported only
    `reaped` would print a sweep with two stuck checkouts as a completed one, and the operator's
    next action (`git worktree remove <name>`) needs the name, so a count would not do either.

    The second assertion is the discriminating half. A `_reap_lines` that emitted the
    `reaped`-derived summary and the failures independently still passed the first assertion
    while printing `no orphan worktrees` immediately above two `FAILED` lines — the headline
    contradicting the detail beneath it, which is the same collapse in the reporting layer.
    """
    repo = _reap_workspace(workspace)
    stuck = [workspace / "work" / _sandbox("acme-commons", n) for n in (1, 2)]
    for path in stuck:
        path.mkdir(parents=True)
    monkey = _NeverSettlingGit(stuck)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "fleet.cli._reap_worktree_manager",
            lambda settings, run_id: WorktreeManager(
                repo_dir=repo, work_dir=workspace / "work", run_id=run_id, runner=monkey
            ),
        )
        result = runner.invoke(app, [*base_args(workspace), "resume"])

    assert result.exit_code == ExitCode.USAGE, result.output
    for path in stuck:
        assert f"FAILED to reap worktree {path.name}" in result.output
        assert path.exists(), "an unsettled `worktree remove` was treated as licence to delete"
    assert "did not settle" in result.output, "the reason was reduced to a bare name"
    assert "no orphan worktrees" not in result.output, (
        "a sweep that removed nothing and failed twice printed the clean headline"
    )


def test_resume_reap_hands_the_container_sweep_sandbox_names_not_expanded_ones(
    workspace: Path,
) -> None:
    """The live set reaches `ContainerSandbox.reap` as SANDBOX names, and `claims()` does the
    matching — the caller must not pre-expand it into concrete container names.

    No container is ever named `sandbox_name(...)` on its own: `BuildverifyWorker` appends
    `-t<token>` per invocation. `reap()` has spared by `-`-delimited prefix since `aa16846`, so
    handing it the sandbox names is both sufficient and race-free. A caller that instead listed
    the containers itself, computed a spared set and passed THAT would leave a window between its
    listing and `reap()`'s own in which a newly started container is missing from the spared set
    and gets `docker rm --force`d with a live row claiming it.

    `-t` tokens differ between the two containers so the assertion cannot be satisfied by string
    equality anywhere on the path.
    """
    _reap_workspace(workspace)
    db = workspace / "state" / "fleet.db"
    fresh = datetime.now(UTC).isoformat(timespec="microseconds")
    _put_leased(db, "acme-commons", heartbeat_at=fresh, attempts=2)

    live_container = f"{_sandbox('acme-commons', 3)}-t0badcafe"
    orphan_container = f"{_sandbox('acme-commons', 1)}-tdeadbeef"
    docker = _ScriptedDocker(present=[live_container, orphan_container, "unrelated-service"])

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("fleet.cli._reap_container_sandbox", lambda: ContainerSandbox(runner=docker))
        result = runner.invoke(app, [*base_args(workspace), "--json", "resume"])

    assert result.exit_code == ExitCode.USAGE, result.output
    assert json.loads(result.stdout)["reaped_containers"]["reaped"] == [orphan_container]
    assert docker.removed == [orphan_container], (
        "the sweep force-removed a container a live row claims"
    )


def test_resume_container_sweep_lists_once_so_a_build_starting_mid_sweep_survives(
    workspace: Path,
) -> None:
    """The property the test above CANNOT see, and the reason it needed a second one.

    That test hands a static docker inventory to the sweep, so it measures "which of these fixed
    names ended up removed". Under a caller that lists the containers itself, computes a spared
    set and passes THAT to `reap()`, the answer is unchanged and the test passes — the quantity
    it watches does not move under the defect (CLAUDE.md guardrail 6, the fourth question). The
    defect only shows up in the WINDOW between two listings, so the fixture has to open one.

    `starts_after_first_listing` is a second container on the live rung — a retry of a build the
    live row owns — that docker first reports only after the sweep has listed once. A sweep that
    takes ONE listing cannot see it and therefore cannot kill it. A sweep that takes two sees it
    in the second, finds it absent from a spared set computed from the first, and
    `docker rm --force`s a running build.
    """
    _reap_workspace(workspace)
    db = workspace / "state" / "fleet.db"
    fresh = datetime.now(UTC).isoformat(timespec="microseconds")
    _put_leased(db, "acme-commons", heartbeat_at=fresh, attempts=2)

    orphan_container = f"{_sandbox('acme-commons', 1)}-tdeadbeef"
    late_start = f"{_sandbox('acme-commons', 3)}-tfeedface"
    docker = _ScriptedDocker(
        present=[f"{_sandbox('acme-commons', 3)}-t0badcafe", orphan_container],
        starts_after_first_listing=late_start,
    )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("fleet.cli._reap_container_sandbox", lambda: ContainerSandbox(runner=docker))
        result = runner.invoke(app, [*base_args(workspace), "resume"])

    assert result.exit_code == ExitCode.USAGE, result.output
    assert docker.listings == 1, (
        "the sweep asked docker twice; the gap between the two answers is a window in which a "
        "container a live row claims is absent from the spared set"
    )
    assert late_start not in docker.removed, (
        "a build that started while step 2 was running was force-removed"
    )
    assert docker.removed == [orphan_container]


def test_resume_reports_every_container_docker_would_not_confirm_removed(
    workspace: Path,
) -> None:
    """The container half of the same obligation: `ContainerReapResult.failed` is printed with
    its reason, never folded into `reaped` and never dropped.

    D32 is the open container-leak defect this sweep is the backstop for. A backstop that
    reported a `docker rm` refusal as a completed removal would not merely miss the leak — it
    would tell the operator the leak had been caught.
    """
    _reap_workspace(workspace)
    stuck = f"{_sandbox('acme-commons', 1)}-tdeadbeef"
    docker = _ScriptedDocker(present=[stuck], rm_fails=[stuck])

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("fleet.cli._reap_container_sandbox", lambda: ContainerSandbox(runner=docker))
        result = runner.invoke(app, [*base_args(workspace), "resume"])

    assert result.exit_code == ExitCode.USAGE, result.output
    assert f"FAILED to reap container {stuck}" in result.output
    assert "is in use" in result.output, "docker's own reason was discarded"
    assert "no orphan containers" not in result.output


def test_resume_step_5_refusal_does_not_share_an_exit_code_with_a_crash(
    workspace: Path,
) -> None:
    """A reconciliation that fully succeeded exits 2, never 1 (ADR-0076).

    Why CI cares, concretely: exit 1 is "unexpected error", and the reflex wrapper retries it.
    Retrying `fleet resume --repoll-prs` costs one `gh pr view` per open PR per iteration, so a
    250-PR fleet burns 250 forge calls per retry and trips GitHub's secondary rate limit — at
    which point the re-poll starts failing and the operator is debugging a rate limit instead of
    reading "step 5 is not implemented". Exit 2 is §10's "the operator must edit a file, a flag
    or a stub before retrying", which is exactly true here and is the signal not to loop.
    """
    result = runner.invoke(app, [*base_args(workspace), "resume"])
    assert result.exit_code != ExitCode.UNEXPECTED_ERROR, result.output
    assert result.exit_code == ExitCode.USAGE
    assert "step 5" in result.output


def test_the_reconciliation_payload_is_emitted_before_the_step_5_refusal(
    workspace: Path,
) -> None:
    """`--json resume` (no `--dry-run`) prints the full report on stdout, THEN exits 2.

    Why the ordering is the assertion and not the exit code: the exit-2 path is the normal
    outcome of a real resume today, so it is the path on which an operator's tooling has to learn
    what was reconciled. If `_emit` ran after the refusal instead of before it, stdout would be
    empty and the only trace of a committed stale-lease sweep and a rewritten
    `migration_state.json` would be an error line on stderr — a machine-readable verb that emits
    nothing machine-readable exactly when it has something to say.

    ADR-0076 states this ordering as a property; this is the test it cites. Every other `--json`
    resume test passes `--dry-run` and therefore exercises the exit-0 path, which would keep
    passing if the ordering were reversed.
    """
    db = workspace / "state" / "fleet.db"
    _put_leased(db, "acme-commons", heartbeat_at=STALE_HEARTBEAT, attempts=2, fence=4)

    result = runner.invoke(app, [*base_args(workspace), "--json", "resume"])
    assert result.exit_code == ExitCode.USAGE, result.output

    payload = json.loads(result.stdout)  # empty stdout => the raise beat the emit
    assert payload["dry_run"] is False
    assert payload["stale_running_reset"] == 1
    assert payload["projection"], "the payload does not name the projection step 7 wrote"
    assert (workspace / "migration_state.json").exists()


def test_a_forge_failure_under_repoll_prs_still_reconciles_and_then_reports(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A flag that adds a step must not subtract the steps that would have run without it.

    Why: `fleet resume --repoll-prs` is the natural command after a crash that stranded PRs, and
    a crashed host is exactly where `gh` credentials have expired. If the forge error propagated,
    that invocation would reconcile NOTHING — every dead worker's row still `RUNNING` with its
    old fence, `migration_state.json` still describing the pre-crash fleet — while the plain
    `fleet resume` the operator did not run would have reclaimed them. The operator would have
    been punished for being more thorough.

    Nothing is swallowed (Rule 11): the forge error is the exit code and it is in the output.
    """
    from fleet import cli

    db = workspace / "state" / "fleet.db"
    _put_leased(db, "acme-commons", heartbeat_at=STALE_HEARTBEAT, attempts=2, fence=4)
    _seed_pr_record(db, "acme-billing", state="DRAFTED",
                    url="https://github.invalid/acme/monorepo/pull/2")

    async def unauthenticated(argv: Sequence[str], **_kwargs: Any) -> ProcResult:
        """The post-crash host: `gh` is there, the credentials are not."""
        return ProcResult(
            argv=tuple(argv),
            exit_code=1,
            stdout_tail="",
            stderr_tail="gh: To get started with GitHub CLI, please run: gh auth login",
            duration_ms=1,
            timed_out=False,
        )

    monkeypatch.setattr(cli, "GH_RUNNER", unauthenticated)
    result = runner.invoke(app, [*base_args(workspace), "resume", "--repoll-prs"])

    # The forge failure is a real failure and keeps exit 1 — it is NOT the step-5 refusal.
    assert result.exit_code == ExitCode.UNEXPECTED_ERROR, result.output
    status, attempts, fence, owner, _hb = _phase_row(db, "acme-commons")
    assert (status, attempts, fence, owner) == ("PENDING", 2, 5, None), (
        "the forge failure abandoned the purely-local §11.5 step 3"
    )
    assert (workspace / "migration_state.json").exists(), "step 7 was abandoned too"


def test_dry_run_reports_the_budget_raise_as_not_applied(workspace: Path) -> None:
    """`--dry-run --raise-budget` previews the change and says, in the payload, that it did not
    happen.

    Why: an operator recovering from a sticky exit-3 halt does the prudent thing and dry-runs
    first. Emitting `"raise_budget": 50.0` beside `"dry_run": true` with no other key lets a CI
    gate assert the ceiling was raised and the halt cleared while `budget_ledger.halted` is still
    1 — the same silently-discarded-flag defect `_refuse_unbuilt_resume_flags` exists to kill.
    """
    db = workspace / "state" / "fleet.db"
    _halt_ledger(db, spent=10.0, max_usd=10.0)

    result = runner.invoke(
        app, [*base_args(workspace), "--json", "resume", "--dry-run", "--raise-budget", "50"]
    )
    assert result.exit_code == ExitCode.SUCCESS, result.output
    payload = json.loads(result.stdout)
    assert payload["raise_budget"] == 50.0
    assert payload["raise_budget_applied"] is False
    assert payload["budget_ledger_before"]["halted"] is True
    assert _ledger(db) == (10.0, 1), "a --dry-run moved the ledger"


def test_dry_run_refuses_a_budget_figure_the_real_run_would_refuse(workspace: Path) -> None:
    """The preview is worth having only if it gives the same verdict as the real invocation.

    Why: a `--dry-run --raise-budget 1` that exits 0 tells the operator their number works. They
    then run it for real against a halted ledger and get exit 2 — having learned nothing from the
    step whose entire purpose was to tell them in advance.
    """
    db = workspace / "state" / "fleet.db"
    _halt_ledger(db, spent=10.0, max_usd=10.0)

    result = runner.invoke(
        app, [*base_args(workspace), "resume", "--dry-run", "--raise-budget", "1"]
    )
    assert result.exit_code == ExitCode.USAGE, result.output
    assert _ledger(db) == (10.0, 1)


def test_raise_budget_refuses_to_lower_a_ceiling(workspace: Path) -> None:
    """The flag is named `--raise-budget`, and it now refuses to do the opposite.

    Why: the CAS only guarded `spent + reserved <= ceiling`, so `--raise-budget 5` typed for a
    run with a $100 ceiling and $1 committed would match — cutting the ceiling to $5, clearing
    the halt, and writing an audited `RunBudgetRaised` finding that records the cut as a raise.
    The run then re-halts almost immediately, and the audit trail says the opposite of what
    happened, which is worse than the halt it "fixed".
    """
    db = workspace / "state" / "fleet.db"
    _halt_ledger(db, spent=1.0, max_usd=100.0)

    result = runner.invoke(app, [*base_args(workspace), "resume", "--raise-budget", "5"])
    assert result.exit_code == ExitCode.USAGE, result.output
    assert "LOWER" in result.output
    assert _ledger(db) == (100.0, 1), "a refused raise still moved the ledger"

    conn = sqlite3.connect(db)
    try:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM findings WHERE kind = 'RunBudgetRaised'"
            ).fetchone()[0]
            == 0
        )
    finally:
        conn.close()


def test_dry_run_says_out_loud_that_it_skipped_the_repoll(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--dry-run --repoll-prs` reports "skipped", not a `null` indistinguishable from "not asked".

    Why: `--dry-run` must not reach the network (§11.5), so skipping is correct — but a payload
    where "the flag was passed and ignored" looks exactly like "the flag was never passed" is how
    an operator concludes the PR state was refreshed when it was not, and then reads a stale
    `blocked_by` as gospel.
    """
    from fleet import cli

    async def refuse(*_args: Any, **_kwargs: Any) -> ProcResult:  # pragma: no cover
        raise AssertionError("a --dry-run resume invoked the forge")

    plain = runner.invoke(app, [*base_args(workspace), "--json", "resume", "--dry-run"])
    assert json.loads(plain.stdout)["repoll_prs"] == "not-requested"

    monkeypatch.setattr(cli, "GH_RUNNER", refuse)
    asked = runner.invoke(
        app, [*base_args(workspace), "--json", "resume", "--dry-run", "--repoll-prs"]
    )
    assert asked.exit_code == ExitCode.SUCCESS, asked.output
    assert json.loads(asked.stdout)["repoll_prs"] == "skipped-dry-run"


def test_pr_ready_refuses_while_a_stub_is_unresolved(workspace: Path) -> None:
    """`fleet pr --ready` is exit 2 while any `stubs` row is ACTIVE or SUPERSEDED (§3.5.1).

    Why: `--ready` is the ONLY path from draft to ready-for-review, so it is the one gate between
    a stubbed build and a human merging it. Nothing in the CLI may promote a repo out of
    `DEGRADED` — only a green revalidation round does that.
    """
    conn = sqlite3.connect(workspace / "state" / "fleet.db", isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO stubs (stub_id, run_id, repo_id, stub_coord_key, consumer_repo_id, "
            "                   provider_repo_id, pinned_version, bazel_label, state, "
            "                   stub_fidelity, state_changed_at, created_at) "
            "VALUES ('s1', ?, 'acme-billing', 'maven:com.acme:commons', 'acme-billing', "
            "        'acme-commons', '1.4.0', '//third_party/stubs:commons', 'ACTIVE', "
            "        'PUBLISHED_ARTIFACT', ?, ?)",
            (RUN_ID, "2026-08-08T12:00:00+00:00", "2026-08-08T12:00:00+00:00"),
        )
    finally:
        conn.close()
    result = runner.invoke(app, [*base_args(workspace), "pr", "--ready"])
    assert result.exit_code == ExitCode.USAGE, result.output
    assert "ACTIVE" in result.output


# --------------------------------------------------------------------------------------
# exit 9 — the disk ceiling the spec declared and nothing enforced
# --------------------------------------------------------------------------------------
IMPOSSIBLE_FLOOR = 2**62
"""~4.6 EB — larger than any volume, so the refusals below are driven by a REAL `statvfs` and
report the free-space number the kernel actually gave the harness."""

DISK_FLOOR_YAML = (
    "run:\n  monorepo_path: ../acme-monorepo\n"
    f"preflight:\n  min_free_bytes: {IMPOSSIBLE_FLOOR}\n"
)


def test_a_phase_refuses_to_start_below_the_disk_floor_with_exit_9(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§12.22 criterion 22: a run against a volume under `preflight.min_free_bytes` evicts the
    Bazel disk cache and then exits **9**, rather than starting and meeting `ENOSPC` later.

    Why this test exists: `budgets.max_disk_gb`, `preflight.min_free_bytes`,
    `FailureClass.DISK_EXHAUSTED` and exit 9 were all declared in §9/§11.3 and, until now, read
    by nothing except `fleet gc --disk` — while this project's own test suite drove the host to 0
    bytes free. A ceiling nothing checks is documentation.

    The refusal must name BOTH numbers. "Not enough disk space" leaves an operator unable to tell
    a 2 GB shortfall from a 200 GB one, and those have different fixes.
    """
    write_config(tmp_path, fleet=DISK_FLOOR_YAML)
    fresh_db(tmp_path / "state" / "fleet.db")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, [*base_args(tmp_path), "scan"])

    assert result.exit_code == ExitCode.DISK_EXHAUSTED == 9, result.output
    assert str(IMPOSSIBLE_FLOOR) in result.output, result.output
    assert "min_free_bytes" in result.output, result.output
    assert str(shutil.disk_usage(tmp_path).free)[:3] in result.output, result.output


def test_the_floor_is_checked_even_when_there_is_no_cache_directory_to_evict(
    tmp_path: Path,
) -> None:
    """The first run on a host is the one that most needs the answer, and it used to be skipped.

    `_gc_disk` returned early when `run.cache_dir` did not exist — which is precisely the state
    before the first clone, i.e. before anything has been written and while the operator can
    still act. "There is nothing to evict" and "there is room" are different claims; only the
    second is worth exiting 0 on. Asserted against a workspace with no `cache/` at all.
    """
    from fleet.cli import DiskExhaustedError, _require_disk_headroom
    from fleet.settings import FleetSettings

    write_config(tmp_path, fleet=DISK_FLOOR_YAML)
    settings = FleetSettings.load(tmp_path / "config")
    assert not (settings.root / settings.config.run.cache_dir).exists()

    with pytest.raises(DiskExhaustedError) as raised:
        _require_disk_headroom(settings)

    assert raised.value.exit_code == ExitCode.DISK_EXHAUSTED
    assert str(IMPOSSIBLE_FLOOR) in str(raised.value)


def test_a_reachable_floor_lets_the_phase_proceed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The negative control. A gate that refused unconditionally would pass both tests above and
    make the harness unable to run at all — which is why the fixture fleets lower the floor
    rather than removing the key."""
    from fleet.settings import FleetSettings

    write_config(tmp_path)
    fresh_db(tmp_path / "state" / "fleet.db")
    monkeypatch.chdir(tmp_path)
    settings = FleetSettings.load(tmp_path / "config")

    from fleet.cli import _require_disk_headroom

    outcome: dict[str, object] = _require_disk_headroom(settings)
    assert outcome["disk_bytes_freed"] == 0, "nothing to evict, and the floor was cleared"


def test_a_missing_rules_dir_is_refused_not_silently_zero_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deleted or mistyped `transform.rules_dir` used to `return ()` from `_transform_rules`,
    which reads identically to "this fleet has no rewrite rules" — `_rewrite_targets` claims no
    relocated file against an empty rule set, so every repo in a ~250-repo fleet silently
    finished as a pure relocation with no rewrite applied and nothing in the output to say the
    directory was even missing. §9 gives `transform.rules_dir` no "disabled" value, so the two
    cases (absent vs. deliberately empty) are conflated unless an absent directory is refused.
    """
    from fleet.cli import _transform_rules
    from fleet.settings import ConfigFileError, FleetSettings

    config_dir = write_config(tmp_path).parent
    rules_dir = config_dir / "rules"
    rules_dir.rmdir()
    assert not rules_dir.exists()
    monkeypatch.chdir(tmp_path)
    settings = FleetSettings.load(tmp_path / "config")

    with pytest.raises(ConfigFileError) as raised:
        _transform_rules(settings)

    assert str(rules_dir.resolve()) in str(raised.value)
    assert "does not exist" in str(raised.value)


def test_a_present_but_empty_rules_dir_is_the_legitimate_zero_rules_fleet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The negative control. A fleet whose migration really is a pure relocation still has to be
    expressible — refusing every absent directory unconditionally would make "no rewrite rules"
    inexpressible, not just safer. It is said by leaving `config/rules` PRESENT and empty, which
    `load_rules` already treats as zero rules with no special-casing needed in `_transform_rules`.
    """
    from fleet.cli import _transform_rules
    from fleet.settings import FleetSettings

    config_dir = write_config(tmp_path).parent
    assert (config_dir / "rules").is_dir()
    monkeypatch.chdir(tmp_path)
    settings = FleetSettings.load(tmp_path / "config")

    assert _transform_rules(settings) == ()


# --------------------------------------------------------------------------------------
# `_transform_criterion`'s fourth clause (ADR-0067, D37): indeterminate vs. unavailable
# --------------------------------------------------------------------------------------


def _init_repo_with_two_commits(repo: Path) -> str:
    """A real two-commit git repo: `dest/good.ts` and `dest/bad.ts` at HEAD~1, both edited at
    HEAD. Returns the HEAD~1 sha — `_transform_criterion` diffs against it via `git`, not a
    fake, so the fixture has to be real git history."""
    def git(*args: str) -> None:
        subprocess.run(  # noqa: S603
            ["git", "-C", str(repo), *args],  # noqa: S607 - "git" from PATH, as every suite does
            check=True,
            capture_output=True,
            text=True,
        )

    repo.mkdir(parents=True)
    git("init", "--initial-branch=main")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "fleet-test")
    (repo / "dest").mkdir()
    (repo / "dest" / "good.ts").write_text("export const good = 1;\n", encoding="utf-8")
    (repo / "dest" / "bad.ts").write_text("export const bad = 1;\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-m", "pre")
    pre_sha = subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), "rev-parse", "HEAD"],  # noqa: S607 - "git" from PATH
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (repo / "dest" / "good.ts").write_text("export const good = 2;\n", encoding="utf-8")
    (repo / "dest" / "bad.ts").write_text("export const bad = 2;\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-m", "rewrite")
    return pre_sha


#: `bad.ts` never produces a verdict; everything else parses. `CALLS` records every path probed,
#: so the test can tell whether the loop reached the file AFTER the indeterminate one.
_INDETERMINATE_ENGINE_MODULE = """\
from __future__ import annotations

from fleet.rewrite.rules import ProbeIndeterminateError

CALLS: list[str] = []


class FakeRewriter:
    engine = "fake"

    async def apply(self, rule, path, source, params):
        return None

    async def parse_probe(self, path: str) -> bool:
        CALLS.append(path)
        if path.endswith("bad.ts"):
            raise ProbeIndeterminateError(f"probe for {path!r} produced no verdict")
        return True


REWRITER = FakeRewriter()
"""

#: Every probe raises `EngineUnavailableError` — the genuinely-absent-binary shape, injected
#: rather than relied on from the host: `tools/bin/ast-grep` is vendored and ON PATH for this
#: suite (conftest), so a test that wanted a real missing engine would not get one.
_UNAVAILABLE_ENGINE_MODULE = """\
from __future__ import annotations

from fleet.rewrite.rules import EngineUnavailableError

CALLS: list[str] = []


class FakeRewriter:
    engine = "fake"

    async def apply(self, rule, path, source, params):
        return None

    async def parse_probe(self, path: str) -> bool:
        CALLS.append(path)
        raise EngineUnavailableError("rewrite engine 'fake' is unavailable: no binary on PATH")


REWRITER = FakeRewriter()
"""


def test_probe_indeterminate_blocks_and_does_not_stop_the_rest_of_the_repos_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0067 (D37) parts 2 and 3, pinned directly against `_transform_criterion`.

    Why this matters, in two parts:
    * an indeterminate probe is a **violation** (`§3.2`'s success criterion fails, exit 6),
      unlike a genuinely missing engine, which is a non-blocking warning — so the two must not be
      reported the same way;
    * the `break` became `continue`: `bad.ts`'s indeterminate probe must not excuse `good.ts`
      from being probed too — one slow or corrupt file must not vouch for the other thirty-nine.
    """
    from fleet.cli import TransformOutput, _transform_criterion, _TransformEvidence, _TransformPlan
    from fleet.models.enums import RepoStatus
    from fleet.rewrite.rules import RewriteRule
    from fleet.settings import FleetSettings

    repo = tmp_path / "repo1"
    pre_sha = _init_repo_with_two_commits(repo)

    engine_dir = tmp_path / "engines"
    engine_dir.mkdir()
    (engine_dir / "fake_indeterminate_engine.py").write_text(
        _INDETERMINATE_ENGINE_MODULE, encoding="utf-8"
    )
    monkeypatch.syspath_prepend(str(engine_dir))
    monkeypatch.delitem(__import__("sys").modules, "fake_indeterminate_engine", raising=False)

    config = write_config(
        tmp_path,
        fleet=FLEET_YAML
        + "transform:\n  rules_dir: config/rules\n  engines:\n    fake: "
        "fake_indeterminate_engine\n",
    )
    settings = FleetSettings.load(config.parent)

    rule = RewriteRule(
        id="fake-rule",
        engine="fake",
        languages=["typescript"],
        applies_to=["**/*.ts"],
        rule={"pattern": "x"},
    )
    plan = _TransformPlan(
        repo_id="repo1",
        worktree=repo,
        branch="main",
        dest_path="dest",
        import_specifier="",
        pre_commit_sha=pre_sha,
        base_ref="main",
        sources=(),
        targets=(),
    )
    evidence = _TransformEvidence()
    evidence.record(
        TransformOutput(
            repo_id="repo1", rewritten=["dest/bad.ts", "dest/good.ts"], unresolved=[]
        )
    )

    violations, unprobed = asyncio.run(
        _transform_criterion(
            settings,
            plans={"repo1": plan},
            evidence=evidence,
            statuses={"repo1": RepoStatus.SUCCEEDED},
            rules=[rule],
        )
    )

    from fake_indeterminate_engine import CALLS  # type: ignore[import-not-found]

    assert any(call.endswith("good.ts") for call in CALLS), (
        "good.ts was never probed — the indeterminate probe for bad.ts stopped the loop, so the "
        "`break` was not turned into a `continue`"
    )
    assert len(violations) == 1, violations
    assert "produced no verdict" in violations[0]
    assert "dest/bad.ts" in violations[0]
    assert unprobed == [], "an indeterminate probe must be a violation, not a non-blocking warning"


def test_engine_unavailable_is_deduped_per_repo_and_engine_not_per_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unavailable arm's own `continue` needs the `(repo_id, engine)` dedupe the ADR asks
    for: a host with no engine installed at all must still emit ONE `parse_probe_unavailable`
    line per repo per engine, not one per rewritten file — otherwise a 40-file repo prints the
    same warning forty times.
    """
    from fleet.cli import TransformOutput, _transform_criterion, _TransformEvidence, _TransformPlan
    from fleet.models.enums import RepoStatus
    from fleet.rewrite.rules import RewriteRule
    from fleet.settings import FleetSettings

    repo = tmp_path / "repo1"
    pre_sha = _init_repo_with_two_commits(repo)

    engine_dir = tmp_path / "engines"
    engine_dir.mkdir()
    (engine_dir / "fake_unavailable_engine.py").write_text(
        _UNAVAILABLE_ENGINE_MODULE, encoding="utf-8"
    )
    monkeypatch.syspath_prepend(str(engine_dir))
    monkeypatch.delitem(__import__("sys").modules, "fake_unavailable_engine", raising=False)

    config = write_config(
        tmp_path,
        fleet=FLEET_YAML
        + "transform:\n  rules_dir: config/rules\n  engines:\n    fake: "
        "fake_unavailable_engine\n",
    )
    settings = FleetSettings.load(config.parent)

    rule = RewriteRule(
        id="ts-rule",
        engine="fake",
        languages=["typescript"],
        applies_to=["**/*.ts"],
        rule={"pattern": "x"},
    )
    plan = _TransformPlan(
        repo_id="repo1",
        worktree=repo,
        branch="main",
        dest_path="dest",
        import_specifier="",
        pre_commit_sha=pre_sha,
        base_ref="main",
        sources=(),
        targets=(),
    )
    evidence = _TransformEvidence()
    evidence.record(
        TransformOutput(
            repo_id="repo1", rewritten=["dest/bad.ts", "dest/good.ts"], unresolved=[]
        )
    )

    violations, unprobed = asyncio.run(
        _transform_criterion(
            settings,
            plans={"repo1": plan},
            evidence=evidence,
            statuses={"repo1": RepoStatus.SUCCEEDED},
            rules=[rule],
        )
    )

    from fake_unavailable_engine import CALLS  # type: ignore[import-not-found]

    assert len(CALLS) == 2, "both rewritten files must still be probed (`continue`, not `break`)"
    assert violations == [], "a genuinely missing engine must not block the run"
    assert len(unprobed) == 1, f"expected one deduped warning for (repo1, fake), got {unprobed}"


def test_rewritten_path_no_rule_claims_reaches_unprobed_not_silently_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Since D49 (`9a7148c`), `output.rewritten` holds every landed `FilePatch.path`, not just
    rule-matched unit names — a repair rung's collateral edits (e.g. a `package.json` touched
    while resolving a `.ts` unit) land here too, and no rule covers `.json` at all.

    Before this fix, `rule is None` hit a bare `continue` under a
    `# pragma: no cover - a rewritten file was claimed by some rule` — an invariant D49 broke.
    The file was neither probed nor reported: SUCCESS with an unparsed, unmentioned file. This
    pins that it now reaches the operator via `unprobed` instead of vanishing, while a
    rule-matched file in the SAME repo still probes exactly as before (no over-correction: the
    fix must not treat matched files as unmatched, or vice versa) — and that an indeterminate
    probe on ANOTHER file stays a `violations` entry, not folded into `unprobed`, so ADR-0067's
    split (indeterminate blocks, "no way to check it" warns) still holds now that "no way to
    check it" has two distinct causes (missing engine, and — new here — no matching rule).
    """
    from fleet.cli import TransformOutput, _transform_criterion, _TransformEvidence, _TransformPlan
    from fleet.models.enums import RepoStatus
    from fleet.rewrite.rules import RewriteRule
    from fleet.settings import FleetSettings

    repo = tmp_path / "repo1"
    pre_sha = _init_repo_with_two_commits(repo)
    (repo / "dest" / "collateral.json").write_text("{}\n", encoding="utf-8")

    engine_dir = tmp_path / "engines"
    engine_dir.mkdir()
    (engine_dir / "fake_indeterminate_engine.py").write_text(
        _INDETERMINATE_ENGINE_MODULE, encoding="utf-8"
    )
    monkeypatch.syspath_prepend(str(engine_dir))
    monkeypatch.delitem(__import__("sys").modules, "fake_indeterminate_engine", raising=False)

    config = write_config(
        tmp_path,
        fleet=FLEET_YAML
        + "transform:\n  rules_dir: config/rules\n  engines:\n    fake: "
        "fake_indeterminate_engine\n",
    )
    settings = FleetSettings.load(config.parent)

    rule = RewriteRule(
        id="ts-rule",
        engine="fake",
        languages=["typescript"],
        applies_to=["**/*.ts"],
        rule={"pattern": "x"},
    )
    plan = _TransformPlan(
        repo_id="repo1",
        worktree=repo,
        branch="main",
        dest_path="dest",
        import_specifier="",
        pre_commit_sha=pre_sha,
        base_ref="main",
        sources=(),
        targets=(),
    )
    evidence = _TransformEvidence()
    evidence.record(
        TransformOutput(
            repo_id="repo1",
            rewritten=["dest/bad.ts", "dest/good.ts", "dest/collateral.json"],
            unresolved=[],
        )
    )

    violations, unprobed = asyncio.run(
        _transform_criterion(
            settings,
            plans={"repo1": plan},
            evidence=evidence,
            statuses={"repo1": RepoStatus.SUCCEEDED},
            rules=[rule],
        )
    )

    from fake_indeterminate_engine import CALLS  # type: ignore[import-not-found]

    assert any(call.endswith("good.ts") for call in CALLS), (
        "the rule-matched, cleanly-parsing file must still be probed exactly as before"
    )
    assert any(call.endswith("bad.ts") for call in CALLS), (
        "the rule-matched, indeterminate file must still be probed exactly as before"
    )
    assert not any(call.endswith("collateral.json") for call in CALLS), (
        "a path no rule claims has no engine to route it through — it must not be probed at all"
    )

    assert len(violations) == 1 and "dest/bad.ts" in violations[0], (
        "the indeterminate probe on a rule-matched file is still a violation, unaffected by the "
        f"unmatched file: {violations}"
    )

    assert len(unprobed) == 1, f"expected exactly one unprobed entry: {unprobed}"
    assert "dest/collateral.json" in unprobed[0], unprobed
    assert "repo1" in unprobed[0], unprobed


def test_no_rule_match_and_missing_engine_are_separate_unprobed_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A genuinely missing engine (`EngineUnavailableError`, deduped per `(repo_id, engine)`) and
    a path no rule claims at all are both "no configured way to check this" facts, and both land
    in the same non-blocking `unprobed` list — but they are DIFFERENT facts about different
    files, so the engine-unavailable dedupe key must not accidentally swallow the no-rule entry
    (or vice versa): this pins that a repo with one of each produces exactly two lines, not one.
    """
    from fleet.cli import TransformOutput, _transform_criterion, _TransformEvidence, _TransformPlan
    from fleet.models.enums import RepoStatus
    from fleet.rewrite.rules import RewriteRule
    from fleet.settings import FleetSettings

    repo = tmp_path / "repo1"
    pre_sha = _init_repo_with_two_commits(repo)
    (repo / "dest" / "collateral.json").write_text("{}\n", encoding="utf-8")

    engine_dir = tmp_path / "engines"
    engine_dir.mkdir()
    (engine_dir / "fake_unavailable_engine.py").write_text(
        _UNAVAILABLE_ENGINE_MODULE, encoding="utf-8"
    )
    monkeypatch.syspath_prepend(str(engine_dir))
    monkeypatch.delitem(__import__("sys").modules, "fake_unavailable_engine", raising=False)

    config = write_config(
        tmp_path,
        fleet=FLEET_YAML
        + "transform:\n  rules_dir: config/rules\n  engines:\n    fake: "
        "fake_unavailable_engine\n",
    )
    settings = FleetSettings.load(config.parent)

    rule = RewriteRule(
        id="ts-rule",
        engine="fake",
        languages=["typescript"],
        applies_to=["**/*.ts"],
        rule={"pattern": "x"},
    )
    plan = _TransformPlan(
        repo_id="repo1",
        worktree=repo,
        branch="main",
        dest_path="dest",
        import_specifier="",
        pre_commit_sha=pre_sha,
        base_ref="main",
        sources=(),
        targets=(),
    )
    evidence = _TransformEvidence()
    evidence.record(
        TransformOutput(
            repo_id="repo1",
            rewritten=["dest/good.ts", "dest/collateral.json"],
            unresolved=[],
        )
    )

    violations, unprobed = asyncio.run(
        _transform_criterion(
            settings,
            plans={"repo1": plan},
            evidence=evidence,
            statuses={"repo1": RepoStatus.SUCCEEDED},
            rules=[rule],
        )
    )

    from fake_unavailable_engine import CALLS  # type: ignore[import-not-found]

    assert violations == [], "neither cause blocks the run"
    assert [str(repo / "dest" / "good.ts")] == CALLS, (
        "collateral.json has no rule and therefore no engine to attempt — it must never reach "
        f"the (unavailable) engine at all: {CALLS}"
    )
    assert len(unprobed) == 2, f"expected one line per distinct cause, got {unprobed}"
    assert any("unavailable" in line.lower() for line in unprobed), unprobed
    assert any("dest/collateral.json" in line for line in unprobed), unprobed


def test_transform_max_patch_bytes_is_threaded_from_settings_to_rewrite_input(
    tmp_path: Path,
) -> None:
    """D49: `transform.max_patch_bytes` (`settings.py:452`) must reach `RewriteInput`, not just
    sit at its own 1_048_576 field default.

    `TransformInput.max_patch_bytes` and `RewriteInput.max_patch_bytes` both default to
    1_048_576 purely so `check_diff` has SOME cap even before a driver threads the configured
    value onto the field (see `RewriteInput.max_patch_bytes`'s docstring in `workers/rewrite.py`,
    which names this exact wiring). Before `_transform_payloads`/`_rewrite_input` threaded it,
    a smaller configured cap never reached the worker: a patch sized between the configured cap
    and the 1 MiB field default was silently ACCEPTED instead of rejected, and the rejection
    message — which names `transform.max_patch_bytes` — reported a value that was never the one
    actually enforced. A test that only checked the field exists would not catch that: the field
    already existed at its default, wired to nothing.
    """
    from fleet.cli import Phase, TransformPipelineWorker, _transform_payloads, _TransformPlan
    from fleet.rewrite.apply import check_diff
    from fleet.settings import FleetSettings

    config = write_config(
        tmp_path,
        fleet=FLEET_YAML + "transform:\n  rules_dir: config/rules\n  max_patch_bytes: 100\n",
    )
    settings = FleetSettings.load(config.parent)
    assert settings.config.transform.max_patch_bytes == 100, "fixture sanity"

    plan = _TransformPlan(
        repo_id="repo1",
        worktree=tmp_path / "repo1",
        branch="main",
        dest_path="dest",
        import_specifier="",
        pre_commit_sha="a" * 40,
        base_ref="main",
        sources=(),
        targets=("dest/file.ts",),
    )
    build = _transform_payloads(settings, {"repo1": plan}, rules=[])
    payload = asyncio.run(
        build(repo_id="repo1", phase=Phase.TRANSFORM, attempt=1, remaining_units=None)
    )
    assert payload.max_patch_bytes == 100, (
        "TransformInput.max_patch_bytes must carry the configured value, not its own "
        "1_048_576 field default"
    )

    rewrite_input = TransformPipelineWorker()._rewrite_input(payload, owed=set())
    assert rewrite_input.max_patch_bytes == 100, (
        "the configured cap must reach RewriteInput — otherwise check_diff enforces the field "
        "default (1 MiB) no matter what the operator configured"
    )

    # The value that actually reaches the worker is the one `check_diff` enforces: a patch sized
    # between the configured 100-byte cap and the 1 MiB field default is rejected under the
    # CONFIGURED cap, exactly as the rejection message (which names `transform.max_patch_bytes`)
    # claims — never silently accepted under the field default. `check_diff` uses strict `>`, so
    # this patch (500 bytes) must land on the reject side of a 100-byte cap.
    diff = "x" * 500
    reason = check_diff(diff, "dest", max_bytes=rewrite_input.max_patch_bytes)
    assert reason is not None and "transform.max_patch_bytes" in reason, reason
    assert "100" in reason, "the reason must cite the CONFIGURED cap, not the 1 MiB default"


def test_max_patch_bytes_at_exactly_the_cap_is_accepted_not_rejected(tmp_path: Path) -> None:
    """`check_diff` uses strict `>` (`rewrite/apply.py`): a patch sized exactly at the configured
    cap must be ACCEPTED. Pinned separately from the threading test above because an off-by-one
    in either the wiring or a future refactor of `check_diff` could silently flip `>` to `>=` and
    reject legitimate patches right at the boundary — the failure mode D49 exists to prevent is a
    cap that is either not enforced at all or enforced at the wrong value, and this catches the
    "enforced at the wrong value" half at the boundary itself.
    """
    from fleet.rewrite.apply import check_diff

    diff = "--- a/dest/file.txt\n+++ b/dest/file.txt\n@@ -1 +1 @@\n-old\n+new\n"
    size = len(diff.encode("utf-8"))
    assert check_diff(diff, "dest", max_bytes=size) is None, (
        "a patch at exactly max_bytes must be accepted (strict `>`, not `>=`)"
    )
    reason = check_diff(diff, "dest", max_bytes=size - 1)
    assert reason is not None and "transform.max_patch_bytes" in reason, (
        "one byte over the cap must still be rejected"
    )


def test_each_configured_bazel_cache_is_created_and_tagged_with_the_flag_it_feeds(
    tmp_path: Path,
) -> None:
    """`verify.disk_cache` → `--disk_cache`, `verify.repository_cache` → `--repository_cache`.

    The two used to be conveyed by list POSITION, which is why the flags could not be emitted
    safely at all: nothing in a host path says which cache it is, and swapping them is silent —
    bazel would accept both directories and simply never hit either. So the configured paths here
    are deliberately role-less names (`cache/one`, `cache/two`) rather than the defaults' `disk`
    and `repo`: an implementation that recovered the role by sniffing the directory NAME, instead
    of carrying it from the setting it was read from, cannot pass.
    """
    from fleet.cli import _cache_mounts
    from fleet.settings import FleetSettings

    write_config(
        tmp_path,
        fleet=FLEET_YAML + "verify:\n  disk_cache: cache/one\n  repository_cache: cache/two\n",
    )
    settings = FleetSettings.load(tmp_path / "config")

    mounts = _cache_mounts(settings)

    by_role = {mount.role: mount for mount in mounts}
    assert set(by_role) == {"disk", "repository"}
    assert by_role["disk"].path == str((tmp_path / "cache/one").resolve())
    assert by_role["repository"].path == str((tmp_path / "cache/two").resolve())
    assert all(Path(mount.path).is_dir() for mount in mounts), (
        "docker creates a missing bind-mount source as root-owned, which the --user container "
        "then cannot write to"
    )
    assert [m.flag(sandboxed=True) for m in mounts] == [
        "--disk_cache=/cache/one",
        "--repository_cache=/cache/two",
    ]
    assert [m.mount().target for m in mounts] == ["/cache/one", "/cache/two"], (
        "the flag value IS the mount target; if these two lists ever disagree the container is "
        "handed a cache bazel was never told about"
    )


def test_exit_codes_reuse_the_orchestrator_constants() -> None:
    """`ExitCode` aliases the halt constants rather than restating their numbers.

    Why: `budgets.py` already owns `WAVE_BUDGET_EXIT_CODE = 10` and `RUN_BUDGET_EXIT_CODE = 3`,
    and `runner.py` raises `RunHalted(exit_code=...)` from them. A second set of literals in the
    CLI is a second source of truth that drifts silently — and the symptom is CI reading a
    resumable budget stop as a crash.
    """
    from fleet.orchestrator import budgets, runner

    assert ExitCode.RUN_COST_EXHAUSTED == budgets.RUN_BUDGET_EXIT_CODE == 3
    assert ExitCode.WAVE_COST_EXHAUSTED == budgets.WAVE_BUDGET_EXIT_CODE == 10
    assert ExitCode.WAVE_WALL_CLOCK_EXHAUSTED == runner.WAVE_WALLCLOCK_EXIT_CODE == 4
    assert ExitCode.MEMORY_EXHAUSTED == runner.HOST_MEMORY_EXIT_CODE == 5
    assert ExitCode.TIER_UNAVAILABLE == runner.TIER_UNAVAILABLE_EXIT_CODE == 8
    assert ExitCode.DISK_EXHAUSTED == runner.DISK_EXIT_CODE == 9
    assert ExitCode.SEQUENCE_REFUSED == 11


# --------------------------------------------------------------------------------------
# D26, D27 — BuildPipelineWorker._publish / _publish_module_lock (docs/INTEGRATION_HONESTY.md)
# --------------------------------------------------------------------------------------
# Real git, real worktrees, no injected runner: the defects are both about WHICH git question
# `_publish`/`_publish_module_lock` ask, not about argv construction, so a fake `CommandRunner`
# would prove nothing — it would answer whatever the test told it to. `worker_ctx` (conftest.py)
# supplies inert sentinels for `db`/`llm`/`router`/`limits`/`log`, which `_publish`'s git-only
# path never calls EXCEPT `log.warning` on the legitimate "no lockfile in this worktree" path —
# so `_StubLog` below stands in for `log` alone, wherever a test's worktree carries none.


class _StubLog:
    """A structlog-`BoundLogger`-shaped no-op. `_publish` warns (never raises) when a build
    worktree carries no `MODULE.bazel.lock`, which is the ordinary, non-error state these tests'
    minimal fixtures are in."""

    def warning(self, *args: object, **kwargs: object) -> None:
        pass


async def test_publish_is_not_blocked_by_worktree_droppings_outside_the_pathspec(
    tmp_path: Path, worker_ctx: WorkerContext
) -> None:
    """D26: `_publish`'s idempotence guard used to ask `is_dirty()` — `git status --porcelain`
    over the WHOLE worktree — rather than the pathspec it had just staged. A real Bazel's own
    `--build_event_json_file=bazel-<unit>-events.json` is a RELATIVE path
    (`buildverify._bazel_argv`), so it lands inside this same worktree; convenience symlinks do
    too. On a PUBLISH-only re-entry whose generated files were already committed, `git add`
    therefore staged nothing for `paths`, but `is_dirty()` still saw those droppings and answered
    dirty — sending `git commit` at an empty index, which fails and takes the whole dispatch down.
    `fleet resume` does not exist, so PUBLISH is the only recovery route: this defect permanently
    stranded any repo whose worktree carried so much as one build-tool dropping outside `paths`,
    on every subsequent re-entry.
    """
    origin = tmp_path / "origin"
    build_dir = tmp_path / "build"
    origin.mkdir()
    origin_git = Git(origin)
    await origin_git.exec(["init", "-q"])
    await origin_git.commit("init", allow_empty=True)
    await origin_git.exec(
        ["worktree", "add", "-q", "-b", "migrate/acme-commons", str(build_dir), "HEAD"]
    )

    build_git = Git(build_dir)
    dest = "libs/widget"
    build_bazel = f"{dest}/BUILD.bazel"
    (build_dir / dest).mkdir(parents=True)
    (build_dir / dest / "BUILD.bazel").write_text("build_rule()\n")
    await build_git.exec(["add", "--", build_bazel])
    await build_git.commit(f"Generate Bazel targets for {dest}")

    # The re-entry state this defect needs: generated files already on the branch, and a real
    # Bazel run's own events file — outside `paths` — sitting untracked in the worktree.
    (build_dir / "bazel-acme-commons-events.json").write_text("{}\n")

    unit = BuildUnit(unit_id="acme-commons", ecosystem=Ecosystem.UNKNOWN, dest=dest)
    payload = BuildInput(
        repo_id="acme-commons",
        dest=dest,
        unit=unit,
        integration_ref="refs/fleet/test/integration/0",
        integration_worktree=str(tmp_path / "unused-integration"),
        lock_dir=str(tmp_path / "locks"),
    )
    output = BuildOutput(repo_id="acme-commons")
    stub_log: Any = _StubLog()  # `WorkerContext.log` is a `structlog.BoundLogger`; conftest.py's
    # own `worker_ctx` sentinels are `Any`-typed for the identical reason — nothing here needs a
    # real logger to be typed as one, only to answer `.warning(...)`.
    ctx = replace(worker_ctx, workdir=str(build_dir), log=stub_log)

    result = await BuildPipelineWorker()._publish(ctx, payload, output)

    assert result is None, (
        "a re-entry whose generated files were already on the branch must be a no-op, not a "
        f"failure over an unrelated worktree dropping: {result}"
    )
    assert output.already_published is True


async def test_publish_module_lock_survives_a_crash_between_materialize_and_commit(
    tmp_path: Path, worker_ctx: WorkerContext
) -> None:
    """D27: `_publish_module_lock` used to compare the lock's content against the FILE sitting in
    the integration worktree rather than what `integration_branch` actually carries. A dispatch
    that dies between `materialize` (which writes the bytes) and `commit` (which lands them)
    leaves exactly that state — correct bytes on disk, nothing reachable from the branch — and
    every later re-entry read the file, found it byte-identical to what it was about to write, and
    returned without ever committing: `module_lock_published` said the lock had landed while the
    branch shipped with none, and a `--network=none` container build of it exits 32 at `Error
    computing the main repository mapping`. This reproduces the crash directly — materialize,
    no commit, re-enter — and asserts the lock actually reaches the branch, which is D27's own
    suggested check: "assert `git show <ref>:MODULE.bazel.lock` resolves".
    """
    origin = tmp_path / "origin"
    integration_dir = tmp_path / "integration"
    origin.mkdir()
    origin_git = Git(origin)
    await origin_git.exec(["init", "-q"])
    await origin_git.commit("init", allow_empty=True)
    await origin_git.exec(
        ["worktree", "add", "-q", "-b", "integration", str(integration_dir), "HEAD"]
    )

    lock_content = '{"lockFileVersion": 15, "moduleFileHash": "abc"}\n'
    # The crash: a PRIOR run's `materialize` wrote the bytes and died before `add`/`commit` ran.
    (integration_dir / MODULE_LOCK_PATH).write_text(lock_content)

    integration_git = Git(integration_dir)
    unit = BuildUnit(unit_id="acme-commons", ecosystem=Ecosystem.UNKNOWN, dest="libs/widget")
    payload = BuildInput(
        repo_id="acme-commons",
        dest="libs/widget",
        unit=unit,
        integration_ref="refs/fleet/test/integration/0",
        integration_worktree=str(integration_dir),
        integration_branch="integration",
        lock_dir=str(tmp_path / "locks"),
    )
    output = BuildOutput(repo_id="acme-commons")
    lock = SupportFile(path=MODULE_LOCK_PATH, content=lock_content)

    await BuildPipelineWorker()._publish_module_lock(
        worker_ctx, payload, output, integration_git, lock
    )

    assert output.module_lock_published is True
    landed = await integration_git.blob_at("integration", MODULE_LOCK_PATH)
    assert landed is not None, (
        "the lock must reach the branch even when a prior crash left byte-identical content "
        "sitting uncommitted in the worktree — comparing against the file alone (D27) let this "
        "state read as 'already published' forever, and the branch never actually got a lock"
    )
    shown = await integration_git.text(["show", f"integration:{MODULE_LOCK_PATH}"])
    assert shown == lock_content.strip()


# --------------------------------------------------------------------------------------
# I5 (docs/superpowers/plans/review-36.md) — every GitCommandError construction forwards `started`
# --------------------------------------------------------------------------------------


def test_every_gitcommanderror_construction_forwards_started() -> None:
    """I5: `GitCommandError.started` defaults to `True`, so a construction site that forgets to
    forward it silently claims a process that never launched actually ran and exited — exactly
    the misclassification the field exists to prevent (`base.clock_failure`'s ADR-0014 ladder
    reads `started`/`timed_out` to tell "we never asked" from "we asked and it ran too long", and
    those cost a repo differently). `cli.py:3634` dropped it once already, under a docstring that
    (at the time) implied every site forwarded it. The class itself cannot enforce its callers'
    keywords, so this is a source-text invariant rather than a behavioural one — the same shape
    `vcs/git.py`'s own module docstring uses for "no shell, ever".
    """
    offenders: list[str] = []
    for path in sorted(REPO_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "GitCommandError"
                and not any(kw.arg == "started" for kw in node.keywords)
            ):
                offenders.append(f"{path.relative_to(REPO_SRC.parents[1])}:{node.lineno}")
    assert offenders == [], (
        "GitCommandError constructed without forwarding `started` (defaults to True, silently "
        f"claiming a never-started process ran and exited): {offenders}"
    )


# --------------------------------------------------------------------------------------
# D43 (docs/INTEGRATION_HONESTY.md) — `_prepare_repo`'s `resolve(branch)` must not let a
# timed-out probe reach `checkout -B`
# --------------------------------------------------------------------------------------
# D43's mechanism: `Git.resolve` used to return `None` for BOTH "the branch does not exist" and
# "the probe never settled" (never started past a passed deadline, or killed at its deadline).
# `_prepare_repo` (cli.py:3787-3789) reads a `None` tip as licence to run `git checkout -B
# <branch> HEAD` — `-B` is CREATE-OR-RESET-HARD, so if `migrate/<repo>` already carries this
# run's committed work, a `rev-parse` that merely FAILED TO ANSWER force-resets it to HEAD, and
# that work becomes reachable only from the reflog. D42's fix (`d37f4ba`) made `Git.resolve` call
# `_require_settled` and raise `GitCommandError` instead of returning `None` on an unsettled
# probe, and `_prepare_repo`'s caller (cli.py:4296) already wraps the call in
# `except (TransformStepUnavailableError, GitError, OSError): await _abandon_repo(...)` — so the
# destructive branch should now be structurally unreachable from a timeout. Nothing pinned that:
# an audit found `_prepare_repo` has no dedicated regression test, and the protection is
# inherited entirely from `Git.resolve`'s own tests in `tests/test_vcs.py`. One refactor of
# `_prepare_repo`'s `except` clause — or of `_prepare_repo` itself — would silently restore the
# branch-destroying bug with nothing here to fail.
#
# `_prepare_repo` builds its own `Git(worktree)` internally with no runner-injection parameter
# (cli.py:3783), so `tests/test_vcs.py`'s `ScriptedRunner` idiom cannot be handed to it directly.
# `_TimeoutOnResolveRunner` below follows that same idiom — a `CommandRunner` is the injected
# seam (CLAUDE.md guardrail 3) — but wraps the REAL runner rather than replacing it wholesale:
# only the one `rev-parse --verify --quiet <branch>^{commit}` probe that decides "reset or reuse"
# is scripted to the passed-deadline shape `util.proc.run` synthesises for a probe that never
# started (§7.1: `started=False`, `timed_out=True`, `exit_code=124` — the one shape
# `util.proc.is_producible_shape` admits for `started=False`); `is_dirty`, `checkout`, `log`,
# `ls-tree` and everything else run for real. A blanket fake would make every git call fail
# identically, which would pass a "does not reach checkout -B" test for the wrong reason — ANY
# git failure here routes to `_abandon_repo`, not specifically an unsettled resolve. The test that
# proves the DISTINCTION (timeout vs. genuine absence) needs real git for the genuine-absence
# case too, so `test_a_genuinely_absent_branch_still_takes_the_checkout_b_path` runs
# `_prepare_repo` with no monkeypatching at all — real git, real worktree, same idiom the D26/D27
# section above uses for questions that are about WHICH git answer is read, not about argv shape.


class _TimeoutOnResolveRunner:
    """`CommandRunner` that answers every git call for real except the ONE probe that resolves
    `target` — which it answers with the shape `util.proc.run` synthesises for a call that never
    got to start past an already-passed deadline (`started=False`, `timed_out=True`,
    `exit_code=124`). That is D43's exact scenario: `Git.resolve(target)` asked a question and
    got back the fleet's clock, not an answer, and the caller must not read that as "no such
    ref".
    """

    def __init__(self, target: str) -> None:
        self._needle = f"{target}^{{commit}}"
        self.calls: list[tuple[str, ...]] = []

    async def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        deadline: float | None = None,
        timeout_s: float | None = None,
    ) -> ProcResult:
        parts = tuple(argv)
        self.calls.append(parts)
        if parts and parts[-1] == self._needle:
            return ProcResult(
                argv=parts,
                exit_code=124,
                stdout_tail="",
                stderr_tail="",
                duration_ms=0,
                timed_out=True,
                started=False,
                cwd=cwd,
            )
        return await proc_run(argv, cwd=cwd, env=env, deadline=deadline, timeout_s=timeout_s)


async def _real_worktree_with_migrate_branch(root: Path, repo_id: str) -> tuple[Path, str, str]:
    """A real git repo at `<root>/work/<repo_id>` (the Phase 1 cut `_prepare_repo` requires)
    whose `migrate/<repo_id>` branch already carries one commit `checkout -B` would discard —
    exactly the "committed work reachable only from the reflog" D43 describes. Returns
    `(worktree, branch, migrated_sha)`.
    """
    worktree = root / "work" / repo_id
    worktree.mkdir(parents=True)
    git = Git(worktree)
    await git.exec(["init", "-q", "-b", "main"])
    (worktree / "README.md").write_text("hello\n")
    await git.exec(["add", "--", "README.md"])
    await git.commit("init")

    branch = f"migrate/{repo_id}"
    await git.exec(["checkout", "-b", branch])
    (worktree / "migrated.txt").write_text("phase 2 work product\n")
    await git.exec(["add", "--", "migrated.txt"])
    migrated_sha = await git.commit("migration work this test must not lose")
    await git.exec(["checkout", "main"])
    return worktree, branch, migrated_sha


def _seed_phase_row(db_path: Path, repo_id: str) -> None:
    """The `phases` row `_prepare_repo` UPDATEs into and `_abandon_repo`'s `WHERE status =
    'PENDING'` requires — created by `repository.upsert_phase` before `_prepare_repo` runs in the
    real `fleet transform` flow (cli.py:4276-4282), reproduced directly here rather than driving
    the whole orchestrator for a test that is about `_prepare_repo`'s git handling alone.
    """
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO phases (run_id, repo_id, phase, updated_at) VALUES (?, ?, ?, ?)",
            (RUN_ID, repo_id, int(Phase.TRANSFORM), "2026-08-08T12:00:00+00:00"),
        )
    finally:
        conn.close()


async def test_a_timed_out_resolve_never_reaches_checkout_b(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D43: a `resolve(branch)` that times out must not be read as "the branch does not exist".

    `git checkout -B <branch> HEAD` (cli.py:3789) is CREATE-OR-RESET-HARD: if `migrate/<repo>`
    already carries this run's committed work, a `rev-parse` that merely FAILED TO ANSWER —
    rather than genuinely finding no such ref — must not reset it, or that work becomes reachable
    only from the reflog. `Git.resolve` now raises `GitCommandError` on an unsettled probe
    instead of returning `None` (D42's fix), so `_prepare_repo`'s `if tip is None: checkout -B`
    is structurally unreachable on a timeout — the exception propagates out of `_prepare_repo`
    before that branch is ever considered. Proven here by asserting `checkout` never once
    appears among the git calls this run made, and that the branch's tip is untouched.
    """
    from fleet.settings import FleetSettings

    settings = FleetSettings.load(workspace / "config")
    repo_id = "acme-commons"
    worktree, branch, migrated_sha = await _real_worktree_with_migrate_branch(workspace, repo_id)
    _seed_phase_row(workspace / "state" / "fleet.db", repo_id)

    fake_runner = _TimeoutOnResolveRunner(branch)
    monkeypatch.setattr("fleet.cli.Git", lambda path: Git(path, runner=fake_runner))

    db_path = workspace / "state" / "fleet.db"
    async with StateWriter(db_path, owner="test-d43-timeout") as writer:
        with pytest.raises(GitCommandError) as excinfo:
            await _prepare_repo(
                settings,
                writer=writer,
                run_id=RUN_ID,
                repo_id=repo_id,
                dest_path="libs/widget",
                import_specifier=repo_id,
                rules=(),
                now=datetime.now(UTC),
            )

    assert excinfo.value.timed_out is True, (
        "the raised error must carry timed_out=True — the evidence that this was an unsettled "
        "probe, not a settled 'no such branch'"
    )
    checkout_calls = [c for c in fake_runner.calls if "checkout" in c]
    assert checkout_calls == [], (
        "a timed-out resolve() must never reach `checkout -B` — the destructive command this "
        f"test exists to keep unreachable: {checkout_calls}"
    )
    tip_after = await Git(worktree).text(["rev-parse", branch])
    assert tip_after == migrated_sha, (
        "the migration commit must still be the branch's tip — a `checkout -B` here would have "
        "force-reset it onto main's HEAD, discarding it (reachable only via reflog afterwards)"
    )


async def test_a_timed_out_resolve_routes_to_abandon_not_a_branch_reset(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D43: `_prepare_repo`'s caller (cli.py:4296) catches exactly `(TransformStepUnavailableError,
    GitError, OSError)` around this call and routes to `_abandon_repo`, which marks the repo's
    phase row `REQUIRES_HUMAN_INTERVENTION` rather than sending it down the branch-reset path
    (§11.1: one repo's git failure is contained, not silently sent through the destructive
    branch). This replicates that exact except clause rather than driving the full `fleet
    transform` CLI command end to end, so a future refactor of either side — the clause's
    exception tuple, or `_prepare_repo` itself — that reopens the timeout-as-absence gap fails
    this test without needing the whole orchestrator wired up.
    """
    from fleet.settings import FleetSettings

    settings = FleetSettings.load(workspace / "config")
    repo_id = "acme-commons"
    worktree, branch, migrated_sha = await _real_worktree_with_migrate_branch(workspace, repo_id)
    db_path = workspace / "state" / "fleet.db"
    _seed_phase_row(db_path, repo_id)

    fake_runner = _TimeoutOnResolveRunner(branch)
    monkeypatch.setattr("fleet.cli.Git", lambda path: Git(path, runner=fake_runner))

    async with StateWriter(db_path, owner="test-d43-abandon") as writer:
        now = datetime.now(UTC)
        try:
            await _prepare_repo(
                settings,
                writer=writer,
                run_id=RUN_ID,
                repo_id=repo_id,
                dest_path="libs/widget",
                import_specifier=repo_id,
                rules=(),
                now=now,
            )
        except (TransformStepUnavailableError, GitError, OSError) as exc:
            await _abandon_repo(writer, RUN_ID, repo_id, detail=str(exc), now=now)
        else:
            pytest.fail("resolve() was scripted to time out; _prepare_repo must not succeed")

    read_conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        row = read_conn.execute(
            "SELECT status, failure_class FROM phases WHERE run_id = ? AND repo_id = ? "
            "AND phase = ?",
            (RUN_ID, repo_id, int(Phase.TRANSFORM)),
        ).fetchone()
    finally:
        read_conn.close()

    assert row == ("REQUIRES_HUMAN_INTERVENTION", "PREFLIGHT"), (
        "the unsettled probe must abandon this repo's phase row, not leave it PENDING for a "
        f"branch-reset retry: {row}"
    )
    tip_after = await Git(worktree).text(["rev-parse", branch])
    assert tip_after == migrated_sha, (
        "the abandon path must still leave the migration commit as the branch's tip — routing "
        "to _abandon_repo must not itself have touched git"
    )


async def test_a_genuinely_absent_branch_still_takes_the_checkout_b_path(
    workspace: Path,
) -> None:
    """The distinguishing case, without which the two tests above could be satisfied by simply
    deleting the `checkout -B` call rather than fixing the timeout/absence distinction. `resolve`
    on a branch that genuinely does not exist is a SETTLED "no such rev" (git ran to completion
    and said no) — §3.2 step 6's ordinary first-transform case — and must still create it. No
    monkeypatching here: real git, real worktree, the default `Git(worktree)` `_prepare_repo`
    builds itself, the same idiom the D26/D27 section above uses for questions that are about
    WHICH git answer is read rather than about argv construction.
    """
    from fleet.settings import FleetSettings

    settings = FleetSettings.load(workspace / "config")
    repo_id = "acme-commons"
    worktree = workspace / "work" / repo_id
    worktree.mkdir(parents=True)
    git = Git(worktree)
    await git.exec(["init", "-q", "-b", "main"])
    (worktree / "README.md").write_text("hello\n")
    await git.exec(["add", "--", "README.md"])
    await git.commit("init")
    # migrate/<repo_id> deliberately never created — the branch genuinely does not exist.

    db_path = workspace / "state" / "fleet.db"
    _seed_phase_row(db_path, repo_id)
    async with StateWriter(db_path, owner="test-d43-real-absence") as writer:
        plan = await _prepare_repo(
            settings,
            writer=writer,
            run_id=RUN_ID,
            repo_id=repo_id,
            dest_path="libs/widget",
            import_specifier=repo_id,
            rules=(),
            now=datetime.now(UTC),
        )

    assert isinstance(plan, _TransformPlan)
    branch = f"migrate/{repo_id}"
    assert plan.branch == branch
    current = await git.text(["rev-parse", "--abbrev-ref", "HEAD"])
    assert current == branch, (
        f"a genuinely absent branch must still take the `checkout -B` path and land on it: "
        f"{current!r}"
    )
