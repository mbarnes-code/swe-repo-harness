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
import shutil
import sqlite3
import subprocess
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
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
from fleet.llm.roles import SPEC_ROLE_TIERS
from fleet.migrations import LATEST_VERSION
from fleet.models.build import BuildUnit, SupportFile
from fleet.models.enums import Ecosystem, Phase
from fleet.models.state import SCHEMA_VERSION
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
      - { backend: anthropic, model_id: claude-haiku-4-5, effort: low,
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
    # BEFORE it re-enters the phase drivers (still stubs, hence exit 1) — the ordering that
    # matters, since a raise recorded only on success is a raise lost to the next crash.
    real = runner.invoke(app, [*base_args(workspace), "resume", "--raise-wave-budget", "50"])
    assert real.exit_code == ExitCode.UNEXPECTED_ERROR, real.output

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
    # The resume itself cannot complete (the phase drivers are stubs) but the audit is written
    # before it re-enters them, which is the ordering that matters: an accepted drift that is
    # only recorded on success is an accepted drift lost to the next crash.
    assert result.exit_code in {ExitCode.SUCCESS, ExitCode.UNEXPECTED_ERROR}, result.output
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
