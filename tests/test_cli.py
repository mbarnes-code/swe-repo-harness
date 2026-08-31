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
import sys
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

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
from fleet.state import db as dbmod
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
    # BEFORE it stops on the unbuilt §11.5 step 8 (exit 2, ADR-0076 — a reconciliation
    # that succeeded, not a crash) — the ordering that matters, since a raise recorded only on
    # success is a raise lost to the next crash.
    real = runner.invoke(
        app,
        [*base_args(workspace), "resume", "--no-continue", "--raise-wave-budget", "50"],
    )
    assert real.exit_code == ExitCode.SUCCESS, real.output

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
    db = workspace / "state" / "fleet.db"
    _put_in_flight(db)
    # §3.1's Phase 1 exit condition (SPEC §12 criterion 9) runs for real at the end of `fleet
    # sequence` now, so this fixture's fleet has to satisfy it: `_put_in_flight` seeds a phase-1
    # row only for `acme-commons` (criterion (c) reads `acme-billing`'s absence from `phases` as
    # an unexplained drop from the wave count), and neither repo has a `manifests` row or a
    # `no-manifest` finding (criterion (a)).
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO phases (run_id, repo_id, phase, status, updated_at) "
            "VALUES (?, 'acme-billing', 1, 'PENDING', ?)",
            (RUN_ID, "2026-08-08T12:00:00+00:00"),
        )
        conn.executemany(
            "INSERT INTO findings (run_id, repo_id, kind, severity, fingerprint, payload, "
            "                      created_at) VALUES (?, ?, 'no-manifest', 'warn', ?, '{}', ?)",
            [
                (RUN_ID, repo, f"no-manifest:{repo}", "2026-08-08T12:00:00+00:00")
                for repo in ("acme-commons", "acme-billing")
            ],
        )
    finally:
        conn.close()
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
        [
            *base_args(workspace), "resume", "--no-continue",
            "--accept-drift", "budgets", "--accept-drift", "gc",
        ],
    )
    # The resume itself cannot complete (§11.5 step 8 is unbuilt) but the audit is
    # written before it stops, which is the ordering that matters: an accepted drift that is
    # only recorded on success is an accepted drift lost to the next crash.
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


UNAVAILABLE_CALL_SITES: Mapping[str, tuple[tuple[str, ...], str]] = MappingProxyType(
    {
        "plan": (("plan",), "src/fleet/workers/relocate.py"),
        "migrate": (
            ("migrate",),
            "src/fleet/workers/relocate.py (Phase 2 of the end-to-end run)",
        ),
        "stubs resolve": (
            ("stubs", "resolve", "acme-commons"),
            "src/fleet/workers/buildverify.py",
        ),
    }
)
"""Every `_unavailable` call site, and the ONLY text each may put after `Related module: `.

A POSITIONAL whitelist, and the counterpart to `UNAVAILABLE_MESSAGE_VOCABULARY` below: that one
governs the message's fixed prose, this one governs its one variable field. The two partition the
message at `Related module: ` and neither can see the other's half, which is why both are here.

All three call sites are covered because the sweep that found the first escape found it in the
shape the **`migrate`** site already uses — a parenthetical hung off the module argument — and
`migrate` and `stubs resolve` had no test over their messages at all. Guarding only the site a
reviewer reported is how a class survives its own fix (CLAUDE.md: sweep for the class).

Widening an entry to go green is the wrong response unless the new text is still a pointer and
still says nothing about the named module — see the assertion's own failure message.
"""


UNAVAILABLE_MESSAGE_VOCABULARY: frozenset[str] = frozenset(
    {"and", "any", "cannot", "cli", "config", "dispatching", "error", "fleet", "has", "identity",
     "implementation", "in", "it", "its", "mirror", "mutex", "no", "nothing", "preconditions",
     "run", "schema", "stopped", "the", "then", "this", "validated", "verb", "version", "was",
     "without", "work", "written"}
)
"""Every word the unavailable-verb message's fixed prose may contain, before `Related module: `.

This is a WHITELIST, not a list of forbidden stub-words, and that inversion is the point: a
status claim about the named module has to be made out of WORDS, so an unpredicted form of one —
"unimplemented", "TODO", "placeholder", "not yet wired", "(still a stub)" — enlarges the spoken
set and trips the assertion without anyone having predicted that wording.

**Scoped to the prose, and that scope is load-bearing.** `module`, `related` and every component
of the module path are deliberately absent, because they live on the other side of the split. A
prose edit therefore cannot REFER to the named module — not by the word `module`, not by its
path — without tripping this, which is what closes a hole the earlier, whole-message version of
this set left open. Measured rather than argued: appending `The module has no implementation.`
to the prose introduces no word the whole-message set lacked — `the`, `module`, `has`, `no` and
`implementation` are all spoken elsewhere in the message already — so equality over the wider
scope stays GREEN while the message tells an operator that a dispatched worker is unwritten.
Prose-scoped, the same edit is RED on `module`. (Set equality is blind to a licensed word
appearing in a NEW PLACE; narrowing the scope is what gives `module` a place it may not appear.)

The normaliser is `[a-z]+` runs of the lower-cased output with ANSI colour stripped and
whitespace collapsed, so wrapping, punctuation, digits and `§` are invisible to it: a message
re-wrapped at a different terminal width has the same spoken set, and a word cannot hide in a
line break.

**It is hand-maintained and it rots CLOSED.** The assertion is an equality, so message drift in
either direction — a word added, a word dropped — is a red. A stale set produces false failures
and never a false pass, which is the only rot a hand-maintained list may have. Editing the
message legitimately will therefore trip it; **widening this set to go green is the wrong
response** unless the new word says nothing about the named module — see the failure message.
"""


@pytest.mark.parametrize("verb", sorted(UNAVAILABLE_CALL_SITES))
def test_unavailable_verb_names_a_module_without_calling_it_a_stub(
    verb: str, workspace: Path
) -> None:
    """A verb with no implementation exits 1 naming a module — never a bare traceback, and never
    a claim about the named module's status. Rule 11: fail loud, say where.

    Retargeted from `scan` to `plan` when `fleet scan` was wired to the real workers, then
    widened to all three `_unavailable` call sites.

    **The inverted assertion is the point.** This test previously required the word
    `NotImplementedError` to be PRESENT in the output, which is why D63 survived: the message
    told the operator `workers/relocate.py` "still raises NotImplementedError", that module has
    never raised it, `RelocateWorker` is dispatched by `fleet transform` on every run, and the
    only test over the message certified the false word. An instrument that asserts a claim
    cannot also falsify it.

    **The name asserts an ABSENCE, so the name needs its own proof (CLAUDE.md Rule 12), and it
    took three cuts to get one.** Cut one was `"NotImplementedError" not in output` — a one-entry
    blacklist, defeated by `"src/fleet/workers/relocate.py (still a stub)"` at the call site, the
    parenthetical shape `migrate` already uses. Cut two answered that with a whole-message word
    whitelist, and left a hole a reviewer walked straight through: `has`, `no` and
    `implementation` are the message's own words, so appending ` has no implementation` to the
    module argument said of a dispatched worker exactly what D63 said, in licensed vocabulary.
    Both escapes needed nothing but a different string literal at a call site — the accidentally
    reachable side of the stop rule, twice.

    Cut three splits the message at `Related module: ` and whitelists each half by a different
    mechanism:

    * the variable half positionally — the pointer must be, exactly, the one string
      `UNAVAILABLE_CALL_SITES` licenses for this verb, so ANY text appended to it fails whatever
      words it is built from;
    * the fixed half by vocabulary — `UNAVAILABLE_MESSAGE_VOCABULARY`, which excludes `module`,
      `related` and every path component, so prose cannot refer to the named module at all
      without tripping.

    Neither half can see the other's, so the pair is complementary rather than layered: a word
    added to the prose is invisible to the positional assertion, and everything after the split
    is invisible to the vocabulary one.

    What remains, measured and stated rather than implied closed: prose that says something false
    about the module while referring to it only as `it` — the one licensed word that could carry
    the reference. Appending `It has no implementation.` to the prose is green here, verified the
    same way as the escapes above. Reaching it needs a sentence a reader would parse as being
    about the module rather than about the verb, built from 32 words that cannot name the module
    and cannot add a word of their own; that is contrivance, not the different-string-literal
    reach both earlier escapes had. It is a boundary, and unlike the previous two it is not one
    this test's own criterion says to fix.
    """
    argv, pointer = UNAVAILABLE_CALL_SITES[verb]
    result = runner.invoke(app, [*base_args(workspace), *argv], catch_exceptions=False)
    assert result.exit_code == ExitCode.UNEXPECTED_ERROR
    assert "Traceback" not in result.output
    assert "no implementation in the CLI" in result.output

    # D63's own word, kept as a named regression pin. Both whitelists below subsume it — but Rule
    # 12 says a redundancy question between two instruments is settled with mutations before
    # either is deleted, and this one has not been. It also carries the ledger reference a
    # whitelist cannot.
    assert "NotImplementedError" not in result.output, (
        "D63: the message must not tell an operator that a live, dispatched module is a stub"
    )

    message = " ".join(re.sub(r"\x1b\[[0-9;]*m", "", result.output).split())
    prose, split, tail = message.partition("Related module: ")
    assert split, (
        "the message no longer names a module. Rule 11: an operator told only that a verb is "
        "unavailable cannot act on it, and `where` is the whole reason this message is long."
    )

    # The variable half: a POINTER, and exactly the licensed one. Whatever is appended to it —
    # `(still a stub)`, ` has no implementation` — is a claim about the module, and the module
    # argument is not where claims may be made (see `_unavailable`).
    assert tail == f"{pointer}.", (
        f"`fleet {verb}` puts something other than its licensed pointer after `Related module: "
        f"`. This field names a module and asserts NOTHING about it: an annotation appended "
        f"here is D63's claim in a new position, and it passes a vocabulary check whenever it is "
        f"built from the message's own words. Fix the call site, not this table — and if the "
        f"pointer genuinely changed, change it here in full."
    )

    # The fixed half: only these words, so prose cannot characterise the module — or name it.
    licensed = UNAVAILABLE_MESSAGE_VOCABULARY | set(re.findall(r"[a-z]+", verb))
    spoken = set(re.findall(r"[a-z]+", prose.lower()))
    assert spoken == licensed, (
        "the unavailable-verb message's prose speaks a word UNAVAILABLE_MESSAGE_VOCABULARY does "
        "not license, or has stopped speaking one it does. If you added a word: this message "
        "names a module as a POINTER and asserts NOTHING about it (see `_unavailable`), so a "
        "word that characterises the named module's status — stub, unimplemented, TODO, "
        "placeholder, missing, pending — is D63 returning in new wording, and so is `module` or "
        "a path component, which would let the prose refer to it. The fix is the message, not "
        "this set. If you removed a word, check that the operator can still tell WHERE the gap "
        "is; that is the whole reason this message exists."
    )


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


def test_status_digest_is_byte_identical_across_two_clean_db_runs_under_a_warm_llm_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§12.21's determinism claim, proved over a REAL run — the test above only pins the
    digest's *shape*.

    Two SEPARATE, from-scratch workspaces (their own `config/`, their own fresh `state/fleet.db`)
    are pointed at the SAME real git repositories and both driven through `fleet scan` (classify
    **not** skipped — the one model-bearing step in Phase 1, ADR-0008) and `fleet sequence`. The
    second run's registered backend EXPLODES on any `invoke`, so it can only reach `SUCCESS`
    under `--llm-cache read-only` if every `repo_classify` call was answered out of the §11.6
    cache rather than a live model — the cache is seeded by copying `llm_cache`'s rows out of the
    first (real, default read-write) run straight into the second workspace's freshly-created
    database, which is the whole of what "warm cache" means here: `llm_cache.cache_key` is
    explicitly run-UNSCOPED (`state/schema.sql`), so a row minted under one run_id is a
    legitimate hit for another, and no new harness plumbing is needed to prime it.

    `--llm-cache read-only` hard-failing on an actual cache MISS is proved elsewhere (see the
    round-O task-2 brief) — that is deliberately not re-tested here. What this test adds is the
    byte-identical claim itself, over a run that structurally required a working cache replay to
    finish at all — not over two computations of the same static fixture DB, and not over a
    classify answer the digest could be accidentally insensitive to: ADR-0008 makes
    classification advisory metadata nothing in `waves`/`edges`/`cycles`/`contracts`/
    `collisions`/`attempts` may branch on, so the two fake backends below are free to answer
    identically and the equality below is about the DETERMINISTIC sections, not about the model.

    **What is and is not exercised.** `run_digest` has 7 sections (`DIGEST_SECTIONS`,
    `state/digest.py`). Only TWO — `waves` and `edges` — hold non-trivial content in this
    fixture (a two-repo, acyclic, no-contract, no-collision, no-repair fleet; verified by
    querying the DB after a run). The other FIVE are empty-but-equal here, each for a distinct
    structural reason rather than by coincidence: `cycles`/`contracts`/`collisions` are empty
    because this fixture has no SCC, no shared IDL and no naming clash to produce one;
    `attempts` is empty because its digest query filters on `approach_signature <> ''`
    (`state/digest.py`), which only a repair-ladder rewrite ever sets, and nothing here fails a
    deterministic step badly enough to trigger repair; `patch_trailers` is empty because the CLI
    calls `run_digest(conn, UUID(run_id))` with no `patch_trailers` argument at
    `cli.py:_status_once`, so it is unconditionally `{}` unless a caller supplies one, and no
    `fleet pr` ever runs in this test. An empty-vs-empty comparison is a real, deterministic
    equality — SQLite returned the identical empty list both times — but it is not evidence
    about determinism of THOSE sections' content, only that they stayed structurally empty on
    both runs. Exercising the other five for real (a cyclic/contract fixture, a forced repair,
    a `fleet pr` pass) is out of this task's scope (round-O task-2 brief: no new harness
    plumbing) and is left to whichever future test builds that fixture.
    """
    from fleet.llm import client as client_module
    from fleet.llm.client import BackendReply, StructuredOutputMode
    from fleet.models.tasks import ModelCapabilities, TokenUsage

    class _ScriptedClassify:
        """A `repo_classify` answer that is fixed and schema-valid. What it says does not
        matter — ADR-0008 — only that both runs get the SAME real model-bearing step answered
        without a network, so the two runs are comparable at all."""

        name = "anthropic"
        version = 1

        def declared_capabilities(self, target: Any) -> ModelCapabilities:
            return ModelCapabilities(
                supports_json_schema=True,
                max_output_tokens=4096,
                structured_output_modes=(StructuredOutputMode.JSON_SCHEMA,),
            )

        async def invoke(self, target: Any, *args: Any, **kwargs: Any) -> BackendReply:
            return BackendReply(
                text=json.dumps(
                    {
                        "ecosystem": "npm",
                        "is_library": False,
                        "confidence": 0.9,
                        "rationale": "fixture backend for the §12.21 warm-cache determinism test",
                    }
                ),
                usage=TokenUsage(input_tokens=10, output_tokens=5, model_id=target.model_id),
                finish_reason="stop",
            )

    class _ExplodingClassify:
        """Registered for the SECOND run only. Reaching `invoke` at all means `--llm-cache
        read-only` fell through to a live call instead of the warm cache — the one way this
        test's digest equality could be an accident rather than a real second execution."""

        name = "anthropic"
        version = 1

        def declared_capabilities(self, target: Any) -> ModelCapabilities:
            return _ScriptedClassify().declared_capabilities(target)

        async def invoke(self, *args: Any, **kwargs: Any) -> BackendReply:
            raise AssertionError(
                "the second run must replay repo_classify from the warm §11.6 cache; reaching "
                "the backend means --llm-cache read-only made a live call"
            )

    def make_repo(root: Path, name: str, files: dict[str, str]) -> Path:
        """One real git repository — the same shape `tests/test_scan_e2e.py::_make_repo` builds,
        kept local here rather than imported: that module imports `MODELS_YAML` FROM this one, so
        the reverse import would be circular."""
        path = root / name
        path.mkdir(parents=True)
        env = {
            "GIT_AUTHOR_NAME": "Fleet Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Fleet Fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "HOME": str(path),
            "PATH": "/usr/bin:/bin:/usr/local/bin",
        }
        subprocess.run(
            ["git", "init", "--initial-branch=main"],  # noqa: S607
            cwd=path, check=True, capture_output=True, env=env,
        )
        for rel, text in sorted(files.items()):
            target = path / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        subprocess.run(
            ["git", "add", "-A"], cwd=path, check=True, capture_output=True, env=env  # noqa: S607
        )
        subprocess.run(
            ["git", "commit", "-m", "fixture"],  # noqa: S607
            cwd=path, check=True, capture_output=True, env=env,
        )
        return path

    def llm_cache_snapshot(root: Path) -> tuple[tuple[str, ...], list[tuple[Any, ...]]]:
        conn = sqlite3.connect(root / "state" / "fleet.db")
        try:
            cols = tuple(row[1] for row in conn.execute("PRAGMA table_info(llm_cache)"))
            rows = [
                tuple(row)
                for row in conn.execute(
                    f"SELECT {', '.join(cols)} FROM llm_cache"  # noqa: S608 — cols is PRAGMA
                ).fetchall()
            ]
            return cols, rows
        finally:
            conn.close()

    def seed_llm_cache(
        root: Path, snapshot: tuple[tuple[str, ...], list[tuple[Any, ...]]]
    ) -> None:
        cols, rows = snapshot
        conn = sqlite3.connect(root / "state" / "fleet.db")
        try:
            placeholders = ", ".join("?" for _ in cols)
            conn.executemany(
                f"INSERT INTO llm_cache ({', '.join(cols)}) VALUES ({placeholders})",  # noqa: S608
                rows,
            )
            conn.commit()
        finally:
            conn.close()

    # A minimal, real dependency edge — one npm library, one npm app that imports it — mirroring
    # `tests/test_scan_e2e.py`'s fixture shape closely enough that scan/sequence's real ecosystem
    # adapters and edge inference run for real, over real manifests.
    repo_files = {
        "acme-lib": {
            "package.json": json.dumps({"name": "@acme/lib", "version": "1.0.0"}, indent=2),
            "src/index.ts": "export const value = 1;\n",
        },
        "acme-app": {
            "package.json": json.dumps(
                {
                    "name": "@acme/app",
                    "version": "1.0.0",
                    "dependencies": {"@acme/lib": "^1.0.0"},
                },
                indent=2,
            ),
            "src/main.ts": (
                "import { value } from '@acme/lib';\nexport const doubled = value * 2;\n"
            ),
        },
    }
    sources = {
        name: make_repo(tmp_path / "sources", name, files)
        for name, files in repo_files.items()
    }

    fleet_yaml = (
        "run:\n  monorepo_path: ../acme-monorepo\n  cache_dir: cache/\n  work_dir: work/\n"
        "concurrency:\n  cpu_pool_workers: 1\n"
        "preflight:\n  min_free_bytes: 1048576\n"
    )
    repos_yaml = "version: 1\ndefaults:\n  ref: main\nrepos:\n" + "".join(
        f"  - name: {name}\n    url: {path}\n" for name, path in sources.items()
    )

    def build_workspace(label: str) -> Path:
        ws = tmp_path / label
        write_config(ws, fleet=fleet_yaml, repos=repos_yaml)
        fresh_db(ws / "state" / "fleet.db")
        return ws

    ws1 = build_workspace("run1")
    ws2 = build_workspace("run2")

    # `--repos` (from `--config`'s directory) must match the CWD-relative path §9 hashes into
    # `runs.config_digests` — see `tests/test_scan_e2e.py`'s `fleet` fixture for the same chdir.
    monkeypatch.chdir(ws1)

    # Run 1: clean DB, classify live, default cache mode (read-write) — warms the cache.
    monkeypatch.setattr(client_module, "registry", lambda: {"anthropic": _ScriptedClassify()})
    scan1 = runner.invoke(app, [*base_args(ws1), "scan"], catch_exceptions=False)
    assert scan1.exit_code == ExitCode.SUCCESS, scan1.output
    seq1 = runner.invoke(app, [*base_args(ws1), "sequence"], catch_exceptions=False)
    assert seq1.exit_code == ExitCode.SUCCESS, seq1.output

    digest1_result = runner.invoke(
        app, [*base_args(ws1), "--json", "status", "--digest"], catch_exceptions=False
    )
    assert digest1_result.exit_code == ExitCode.SUCCESS, digest1_result.output
    digest1 = json.loads(digest1_result.stdout)["digest"]

    snapshot = llm_cache_snapshot(ws1)
    assert snapshot[1], "run 1 made no repo_classify call — the fake backend was never reached"

    # Seed run 2's fresh DB with the warm cache BEFORE scanning: `llm_cache` is run-unscoped by
    # design, so copying its rows into a different run's database is a legitimate way to prime a
    # replay, not a special case.
    seed_llm_cache(ws2, snapshot)

    # Run 2: a SEPARATE clean workspace and DB, classify live, forced read-only, and a backend
    # that raises on any call — success is only possible via a cache hit.
    monkeypatch.chdir(ws2)
    monkeypatch.setattr(client_module, "registry", lambda: {"anthropic": _ExplodingClassify()})
    scan2 = runner.invoke(
        app, [*base_args(ws2), "--llm-cache", "read-only", "scan"], catch_exceptions=False
    )
    assert scan2.exit_code == ExitCode.SUCCESS, scan2.output
    seq2 = runner.invoke(app, [*base_args(ws2), "sequence"], catch_exceptions=False)
    assert seq2.exit_code == ExitCode.SUCCESS, seq2.output

    digest2_result = runner.invoke(
        app, [*base_args(ws2), "--json", "status", "--digest"], catch_exceptions=False
    )
    assert digest2_result.exit_code == ExitCode.SUCCESS, digest2_result.output
    digest2 = json.loads(digest2_result.stdout)["digest"]

    assert digest2 == digest1, (
        "two clean-DB runs of the same fleet produced different run_digests — §12.21's "
        "run-equivalence proof does not hold"
    )
    # The equality is not vacuous: it is taken over sections that actually hold something.
    conn = sqlite3.connect(ws1 / "state" / "fleet.db")
    try:
        (wave_count,) = conn.execute("SELECT COUNT(*) FROM wave_members").fetchone()
        (edge_count,) = conn.execute("SELECT COUNT(*) FROM edges").fetchone()
    finally:
        conn.close()
    assert wave_count > 0 and edge_count > 0, (
        "the fixture produced no waves/edges — the digest equality above would be over empty "
        "sections and would prove nothing"
    )


def test_status_digest_differs_when_a_fixture_source_file_mutates_between_two_clean_db_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§12.21 clause 3 — the direct inverse of the test above.

    That test proves the digest is *stable* across two clean runs of an UNCHANGED fleet. It does
    not prove the digest is *sensitive* to real content: a digest section accidentally computed
    from something that never varies (a constant, a path that never changes) would pass it just
    as cleanly as a genuinely content-derived one. This test closes that gap by mutating one real
    fixture source file between the two runs and asserting the digest MOVES.

    Same two-workspace, warm-cache, forced-read-only harness as the precedent test, with one
    difference inserted between run 1 and run 2: `acme-app`'s `package.json` dependency on
    `@acme/lib` is rewritten from an open range (`^1.0.0`) to a pinned exact version (`1.0.0`),
    committed to the SAME source git repository both workspaces clone from. `graph/infer.py`'s
    `_manifest_edges` (`_is_pinned`) reads exactly that string to choose the edge `kind`:
    `DECLARED_DEP` for an open range, `PUBLISHED_ARTIFACT` for a pin — so this one-line manifest
    edit is a genuine, minimal change to a value `state/digest.py`'s `edges` section hashes
    (`src_kind, src_id, dst_kind, dst_id, kind`), not a cosmetic touch of the file.

    The mutation is deliberately NOT a `.ts` source edit: `ClassifyWorker._messages` (§ precedent
    test above) sends the model a PATH LISTING, never file bytes, and `manifests/npm.py` parses
    only `package.json` — the ecosystem adapters here never read `.ts` file content for the graph
    at all (`workers/symbolindex.py` does extract import symbols from `.ts` sources by regex, but
    `acme-app` already declares `@acme/lib` in `package.json`, so an edit to the import statement
    itself would be dead-lettered by `_import_edges`'s `declared` dedup and move nothing). A
    `package.json` version-spec edit is therefore the smallest change that is (a) real fixture
    source content, (b) provably read by the scan/graph path, and (c) INVISIBLE to the classify
    prompt — the two runs' `llm_cache.cache_key`s stay identical, so run 2 can still succeed
    under `--llm-cache read-only` against the exploding backend, exactly as run 1's cache-hit
    proof requires. If the mutation touched a path the prompt lists (add/remove a file) or the
    prompt's content (it doesn't — only paths are sent), that guarantee would not hold.
    """
    from fleet.llm import client as client_module
    from fleet.llm.client import BackendReply, StructuredOutputMode
    from fleet.models.tasks import ModelCapabilities, TokenUsage

    class _ScriptedClassify:
        """Same fixture backend as the precedent test — what it says does not matter
        (ADR-0008), only that both runs get the one model-bearing step answered identically."""

        name = "anthropic"
        version = 1

        def declared_capabilities(self, target: Any) -> ModelCapabilities:
            return ModelCapabilities(
                supports_json_schema=True,
                max_output_tokens=4096,
                structured_output_modes=(StructuredOutputMode.JSON_SCHEMA,),
            )

        async def invoke(self, target: Any, *args: Any, **kwargs: Any) -> BackendReply:
            return BackendReply(
                text=json.dumps(
                    {
                        "ecosystem": "npm",
                        "is_library": False,
                        "confidence": 0.9,
                        "rationale": "fixture backend for the §12.21 digest-sensitivity test",
                    }
                ),
                usage=TokenUsage(input_tokens=10, output_tokens=5, model_id=target.model_id),
                finish_reason="stop",
            )

    class _ExplodingClassify:
        """Registered for run 2 only. Reaching `invoke` means the mutation below disturbed the
        classify cache key — the prompt is path-listing-only (see docstring), so it should not
        — and this run would then be a live-model run rather than the intended cache replay."""

        name = "anthropic"
        version = 1

        def declared_capabilities(self, target: Any) -> ModelCapabilities:
            return _ScriptedClassify().declared_capabilities(target)

        async def invoke(self, *args: Any, **kwargs: Any) -> BackendReply:
            raise AssertionError(
                "run 2 must replay repo_classify from the warm §11.6 cache; reaching the "
                "backend means the package.json mutation changed the classify prompt/cache key"
            )

    def make_repo(root: Path, name: str, files: dict[str, str]) -> Path:
        """Identical helper to the precedent test's, kept local for the same reason: no shared
        module to import it from without a circular import."""
        path = root / name
        path.mkdir(parents=True)
        env = {
            "GIT_AUTHOR_NAME": "Fleet Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Fleet Fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "HOME": str(path),
            "PATH": "/usr/bin:/bin:/usr/local/bin",
        }
        subprocess.run(
            ["git", "init", "--initial-branch=main"],  # noqa: S607
            cwd=path, check=True, capture_output=True, env=env,
        )
        for rel, text in sorted(files.items()):
            target = path / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        subprocess.run(
            ["git", "add", "-A"], cwd=path, check=True, capture_output=True, env=env  # noqa: S607
        )
        subprocess.run(
            ["git", "commit", "-m", "fixture"],  # noqa: S607
            cwd=path, check=True, capture_output=True, env=env,
        )
        return path

    def commit_pinned_dependency(repo_path: Path) -> None:
        """Mutates `package.json` in place, commits it, and — Rule 12's zero-change gate
        discipline — proves via `git diff --numstat` that the commit actually changed a file
        before the caller trusts the digest comparison built on top of it."""
        env = {
            "GIT_AUTHOR_NAME": "Fleet Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Fleet Fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "HOME": str(repo_path),
            "PATH": "/usr/bin:/bin:/usr/local/bin",
        }
        before_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607
            cwd=repo_path, check=True, capture_output=True, env=env, text=True,
        ).stdout.strip()

        manifest_path = repo_path / "package.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["dependencies"]["@acme/lib"] == "^1.0.0", (
            "fixture package.json no longer has the open-range spec this mutation assumes"
        )
        manifest["dependencies"]["@acme/lib"] = "1.0.0"  # open range -> pinned exact release
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

        subprocess.run(
            ["git", "add", "-A"],  # noqa: S607
            cwd=repo_path, check=True, capture_output=True, env=env,
        )
        subprocess.run(
            ["git", "commit", "-m", "pin @acme/lib to an exact release"],  # noqa: S607
            cwd=repo_path, check=True, capture_output=True, env=env,
        )

        numstat = subprocess.run(  # noqa: S603 - fixed argv, before_sha is our own rev-parse
            ["git", "diff", "--numstat", before_sha, "HEAD"],  # noqa: S607
            cwd=repo_path, check=True, capture_output=True, env=env, text=True,
        ).stdout
        assert numstat.strip(), (
            "the mutation commit produced a zero-line diff (Rule 12's zero-change gate) — the "
            "digest-differs assertion below would not be testing source sensitivity at all"
        )
        assert "package.json" in numstat, (
            f"the mutation diff does not touch package.json as expected: {numstat!r}"
        )

    def llm_cache_snapshot(root: Path) -> tuple[tuple[str, ...], list[tuple[Any, ...]]]:
        conn = sqlite3.connect(root / "state" / "fleet.db")
        try:
            cols = tuple(row[1] for row in conn.execute("PRAGMA table_info(llm_cache)"))
            rows = [
                tuple(row)
                for row in conn.execute(
                    f"SELECT {', '.join(cols)} FROM llm_cache"  # noqa: S608 — cols is PRAGMA
                ).fetchall()
            ]
            return cols, rows
        finally:
            conn.close()

    def seed_llm_cache(
        root: Path, snapshot: tuple[tuple[str, ...], list[tuple[Any, ...]]]
    ) -> None:
        cols, rows = snapshot
        conn = sqlite3.connect(root / "state" / "fleet.db")
        try:
            placeholders = ", ".join("?" for _ in cols)
            conn.executemany(
                f"INSERT INTO llm_cache ({', '.join(cols)}) VALUES ({placeholders})",  # noqa: S608
                rows,
            )
            conn.commit()
        finally:
            conn.close()

    def edge_kinds(root: Path) -> list[str]:
        conn = sqlite3.connect(root / "state" / "fleet.db")
        try:
            return sorted(
                str(kind) for (kind,) in conn.execute("SELECT kind FROM edges").fetchall()
            )
        finally:
            conn.close()

    # Same minimal npm dependency-edge fixture as the precedent test.
    repo_files = {
        "acme-lib": {
            "package.json": json.dumps({"name": "@acme/lib", "version": "1.0.0"}, indent=2),
            "src/index.ts": "export const value = 1;\n",
        },
        "acme-app": {
            "package.json": json.dumps(
                {
                    "name": "@acme/app",
                    "version": "1.0.0",
                    "dependencies": {"@acme/lib": "^1.0.0"},
                },
                indent=2,
            ),
            "src/main.ts": (
                "import { value } from '@acme/lib';\nexport const doubled = value * 2;\n"
            ),
        },
    }
    sources = {
        name: make_repo(tmp_path / "sources", name, files)
        for name, files in repo_files.items()
    }

    fleet_yaml = (
        "run:\n  monorepo_path: ../acme-monorepo\n  cache_dir: cache/\n  work_dir: work/\n"
        "concurrency:\n  cpu_pool_workers: 1\n"
        "preflight:\n  min_free_bytes: 1048576\n"
    )
    repos_yaml = "version: 1\ndefaults:\n  ref: main\nrepos:\n" + "".join(
        f"  - name: {name}\n    url: {path}\n" for name, path in sources.items()
    )

    def build_workspace(label: str) -> Path:
        ws = tmp_path / label
        write_config(ws, fleet=fleet_yaml, repos=repos_yaml)
        fresh_db(ws / "state" / "fleet.db")
        return ws

    ws1 = build_workspace("run1")
    ws2 = build_workspace("run2")

    monkeypatch.chdir(ws1)

    # Run 1: clean DB, classify live, default cache mode (read-write) — warms the cache, and
    # captures the digest of the UNMUTATED fixture.
    monkeypatch.setattr(client_module, "registry", lambda: {"anthropic": _ScriptedClassify()})
    scan1 = runner.invoke(app, [*base_args(ws1), "scan"], catch_exceptions=False)
    assert scan1.exit_code == ExitCode.SUCCESS, scan1.output
    seq1 = runner.invoke(app, [*base_args(ws1), "sequence"], catch_exceptions=False)
    assert seq1.exit_code == ExitCode.SUCCESS, seq1.output

    digest1_result = runner.invoke(
        app, [*base_args(ws1), "--json", "status", "--digest"], catch_exceptions=False
    )
    assert digest1_result.exit_code == ExitCode.SUCCESS, digest1_result.output
    payload1 = json.loads(digest1_result.stdout)
    digest1, sections1 = payload1["digest"], payload1["sections"]

    snapshot = llm_cache_snapshot(ws1)
    assert snapshot[1], "run 1 made no repo_classify call — the fake backend was never reached"

    kinds1 = edge_kinds(ws1)
    assert kinds1 == ["DECLARED_DEP"], (
        f"run 1's fixture is not in the expected pre-mutation state, got {kinds1!r}"
    )

    # THE MUTATION: a real, committed content change to a fixture source file, between the two
    # otherwise-identical runs — confirmed non-cosmetic via `git diff --numstat` above.
    commit_pinned_dependency(sources["acme-app"])

    # Seed run 2's fresh DB with the warm cache BEFORE scanning, exactly as the precedent test
    # does: `llm_cache` is run-unscoped, and the mutation above does not touch anything the
    # classify prompt sends (paths only, never content), so the same cache rows are still a
    # legitimate replay for run 2's classify calls.
    seed_llm_cache(ws2, snapshot)

    # Run 2: a SEPARATE clean workspace and DB, over the MUTATED source tree, classify forced
    # read-only against an exploding backend — success is only possible via the cache hit.
    monkeypatch.chdir(ws2)
    monkeypatch.setattr(client_module, "registry", lambda: {"anthropic": _ExplodingClassify()})
    scan2 = runner.invoke(
        app, [*base_args(ws2), "--llm-cache", "read-only", "scan"], catch_exceptions=False
    )
    assert scan2.exit_code == ExitCode.SUCCESS, scan2.output
    seq2 = runner.invoke(app, [*base_args(ws2), "sequence"], catch_exceptions=False)
    assert seq2.exit_code == ExitCode.SUCCESS, seq2.output

    digest2_result = runner.invoke(
        app, [*base_args(ws2), "--json", "status", "--digest"], catch_exceptions=False
    )
    assert digest2_result.exit_code == ExitCode.SUCCESS, digest2_result.output
    payload2 = json.loads(digest2_result.stdout)
    digest2, sections2 = payload2["digest"], payload2["sections"]

    kinds2 = edge_kinds(ws2)
    assert kinds2 == ["PUBLISHED_ARTIFACT"], (
        f"the mutation did not flip the edge kind as expected, got {kinds2!r} — the fixture "
        "assumption behind this test (an open-range spec pinned to an exact release) no longer "
        "holds, and the digest-differs assertion below would not be testing what it claims to"
    )

    assert digest2 != digest1, (
        "the run_digest did not change when a real fixture source file was mutated between two "
        "otherwise-identical clean-DB runs — §12.21's digest-sensitivity claim does not hold: "
        "either a digest section is computed from something that never varies, or the mutation "
        "above is not reaching it"
    )
    # Localize the difference: the ONE section a pinned-vs-open-range edge kind can move is
    # `edges` (`state/digest.py`'s per-section hashing, `edge_kinds` above confirms the DB-level
    # cause). Every other section stayed empty-but-equal in both runs (same acyclic, no-contract,
    # no-collision, no-repair, no-PR fixture as the precedent test), so this is not a case where
    # the whole digest moved for an unexplained reason.
    assert sections2["edges"] != sections1["edges"], (
        "the digests differ, but not in the edges section — the mutation moved something other "
        "than what this test intends to prove sensitive"
    )
    for name in ("waves", "cycles", "contracts", "collisions", "patch_trailers", "attempts"):
        assert sections2[name] == sections1[name], (
            f"section {name!r} moved too — the mutation's blast radius is wider than the single "
            "edge-kind change this test's fixture assumptions rely on"
        )


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


def test_llm_cache_flag_reaches_the_run_it_is_a_flag_on(workspace: Path) -> None:
    """`--llm-cache` selects the mode a `RunContext`-assembled client will actually use (§11.6).

    Why: this replaces `test_llm_cache_flag_reaches_the_caching_client`, which was true of a
    helper and false of a run. That test asserted that `cli.caching_client` — a function with
    **zero callers in `src/`** — mapped the flag onto a constructor argument, and that
    `fleet models list --json` *reported* the flag back. Deleting §11.6's only production
    consumption point, `RunContext.__post_init__`'s cache branch, left it green: the property in
    its name ("reaches the caching client") was false for the whole life of the harness while it
    passed, because nothing on any shipped path built a caching client at all.

    Every assertion below is on a **resolved value in a live interpreter** — the settings object
    the CLI actually loads and the client an actual `RunContext` actually assembles — never on a
    declaration and never on the flag echoed back to stdout. The declaration is what carried the
    other half of this defect: `--llm-cache` was declared `= LlmCacheMode.READ_WRITE`, a
    non-optional default that an unconditional override would have substituted over an
    operator's `cache_mode: off`.
    """
    from fleet.cli import GlobalOptions, LlmCacheMode, _load_settings

    # Quoted: bare `off` is a YAML 1.1 boolean, and `llm.cache_mode` is a `Literal` of strings,
    # so an unquoted one is an exit-2 config error rather than the mode this test is about.
    write_config(workspace, fleet=FLEET_YAML + 'llm:\n  cache_mode: "off"\n')
    config_path = workspace / "config" / "fleet.yaml"

    # 1. No flag: `fleet.yaml` wins. This is the case the non-optional default would break.
    unflagged = _load_settings(GlobalOptions(config_path=config_path))
    assert unflagged.config.llm.cache_mode == "off", (
        "with no --llm-cache typed, the operator's fleet.yaml value must survive; a flag default "
        "reaching the override channel silently re-rolls a run the operator asked to replay"
    )
    assert _client_mode(workspace, unflagged) == "off"

    # 2. The flag overrides it, in both directions away from the config value.
    for flag, expected in (
        (LlmCacheMode.READ_ONLY, "read-only"),
        (LlmCacheMode.READ_WRITE, "read-write"),
        (LlmCacheMode.OFF, "off"),
    ):
        settings = _load_settings(GlobalOptions(config_path=config_path, llm_cache=flag))
        assert settings.config.llm.cache_mode == expected
        assert _client_mode(workspace, settings) == expected, (
            f"--llm-cache {flag.value} must reach the client a run assembles, not just settings"
        )

    # 3. The pre-flight reports the RESOLVED mode, which with no flag is the config's.
    surfaced = runner.invoke(app, [*base_args(workspace), "--json", "models", "list"])
    assert surfaced.exit_code == ExitCode.SUCCESS, surfaced.output
    assert json.loads(surfaced.stdout)["llm_cache"] == "off"


def _client_mode(workspace: Path, settings: Any) -> str:
    """The `mode` of the client a real `RunContext` assembles from `settings` — read off the
    object, in the running interpreter, exactly as a phase run would get it.

    Not `opts.cache_mode` and not `settings.config.llm.cache_mode`: those are the input. What
    this test exists to prove is that the value arrives at the one client every worker calls.
    """
    from fleet.llm.roles import LlmRouter
    from fleet.orchestrator.budgets import Ceilings, CostLedger, Limits
    from fleet.orchestrator.context import RunContext, default_logger
    from fleet.state.db import connect_ro, initialize_database
    from fleet.state.repository import SqliteStateRepository

    async def _build() -> str:
        path = workspace / "state" / "cache-mode.db"
        await initialize_database(path)
        async with StateWriter(path, owner="test-cache-mode") as writer:
            read_conn = await connect_ro(path)
            try:
                repo = SqliteStateRepository(writer=writer, read_conn=read_conn)
                ledger = CostLedger(
                    repo,
                    run_id=RUN_ID,
                    ceilings=Ceilings.from_settings(
                        settings.config.budgets, settings.config.stubs
                    ),
                )
                ctx = RunContext(
                    run_id=uuid.UUID(RUN_ID),
                    config=settings.config,
                    writer=writer,
                    repository=repo,
                    read_conn=read_conn,
                    ledger=ledger,
                    limits=Limits(
                        git_net=asyncio.Semaphore(1),
                        subprocess=asyncio.Semaphore(1),
                        docker=asyncio.Semaphore(1),
                        llm={},
                        cpu_pool=cast(Any, None),
                        ledger=ledger,
                    ),
                    llm=LlmRouter.from_models_config(
                        settings.models, profile=settings.profile
                    ),
                    log=default_logger("test.cache-mode"),
                    work_dir=workspace / "work",
                    harness_version="0.1.0",
                )
                return str(ctx.model_client._mode)  # type: ignore[union-attr]
            finally:
                await read_conn.close()

    try:
        return asyncio.run(_build())
    finally:
        dbmod._release_write_slot()


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

CONTINUE_KNOBS_YAML = (
    "run:\n  monorepo_path: ../acme-monorepo\n"
    "preflight:\n  min_free_bytes: 1048576\n"
    "budgets:\n  build_timeout_s: 999\n"
)
"""`FLEET_YAML` with `budgets.build_timeout_s` set to a value NO Typer default shares.

`fleet build --timeout` defaults to 1800 and `budgets.build_timeout_s` defaults to 1800 too, so a
call site that hardcoded the Typer literal is indistinguishable from one that reads config under
the default fixture. 999 is what makes that mutation expressible."""


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

    # `--no-continue` because this test's subject is step 3, and its fixture was not written to
    # survive step 8 driving the phase composition roots over it (ADR-0080). Exit 0 is the whole
    # of the claim: the reconciliation below is durable and nothing refused it.
    result = runner.invoke(app, [*base_args(workspace), "resume", "--no-continue"])
    assert result.exit_code == ExitCode.SUCCESS, result.output

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

    result = runner.invoke(app, [*base_args(workspace), "resume", "--no-continue"])
    assert result.exit_code == ExitCode.SUCCESS, result.output

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

    Why: every flag refused here scopes or re-drives the continuation in a way §11.5 step 8 does
    not implement. **Until ADR-0080 the reason was that the continuation itself did not exist**;
    step 8 is wired now and this test's subject survived the change, because what each flag names
    is absent for its OWN reason — a start other than the computed floor, a scope the
    reconciliation does not carry, `attempts` rewriting and revalidation rounds with no
    implementation in `src/`. A parser that accepted `--from-phase 2` and
    then resumed from wherever it liked leaves the
    operator believing they scoped the resume — and nothing anywhere tells them otherwise.

    The last two assertions pin the RULE the refusal states, not just that it refuses. Both
    operator-facing step-5 messages described the missing work as "demote to the earliest phase
    whose precondition holds", and both halves of that are retracted: `preconditions_hold` is not
    the resume predicate (`rdepverify` answers `True` for a repo with no BUILD row, so a walk over
    it promotes never-built repos — the reason `orchestrator/reentry.phase_floor` reads evidence
    instead), and the floor is the phase ABOVE the HIGHEST holder below the settled frontier,
    never the earliest. The correction reached `docs/SPEC.md`, `docs/DECISIONS.md`,
    `models/enums.py` and the plan; this string is the surface it had not reached, and the surface
    an operator actually reads.

    **The built-steps enumeration is pinned too, and it is a separate class from the rule above.**
    This message's closing clause tells a refused operator what a plain `fleet resume` WOULD do;
    when step 4 landed it still read "steps 2, 3 and 7", making it the third stale-absence site of
    the round after `ResumeIncompleteError` and `resume.__doc__` (V2 finding I2). `--repo` is the
    flag an operator is most likely to type immediately after reading that step 4 ran, which makes
    this the worst of the three placements for a stale list. The assertion reads the parenthetical
    as a whole rather than a fixed substring, so a successor that builds step 6 and forgets to add
    it trips too.
    """
    # NO `--no-continue`, deliberately: `_refuse_unbuilt_resume_flags` runs before
    # `_resume_impl`, so the continuation is unreachable from this path and adding the flag would
    # hide that ordering. It is the one bare `resume` invocation 10e left bare.
    result = runner.invoke(app, [*base_args(workspace), "resume", "--from-phase", "2"])
    assert result.exit_code == ExitCode.USAGE, result.output
    assert "--from-phase" in result.output
    assert "step 5" in result.output
    built = " ".join(result.output.split())
    built = built[built.index("reconciliation that IS built (") :]
    built = built[: built.index(")")]
    assert "steps 2, 3, 4, 5, 6, 7 and 8" in built, (
        f"the refusal's built-steps list is stale: {built!r} — step 4 (Git-as-arbiter task "
        "reconciliation), step 5 (the re-entry demotion, wired in at `2f0db34`), step 6 (the "
        "`blocked_by` recompute and its appended wave) and step 8 (the continuation, wired in "
        "by ADR-0080) all run on every plain `fleet resume`"
    )
    assert "precondition" not in result.output, (
        "the refusal names a predicate step 5 does not use, and cannot use"
    )
    assert "HIGHEST phase below the settled frontier" in result.output, (
        "the refusal states the floor without its quantifier, which is the half two earlier "
        "fixes in this class left behind"
    )
    assert "DEGRADED/SKIPPED hard stop" in result.output, (
        "the refusal drops `_HARD_STOPS`, which is the OTHER half a fix in this class already "
        "omitted once: a floor stated without it reads as evidence-only and re-runs excluded work"
    )


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

    result = runner.invoke(app, [*base_args(workspace), "resume", "--no-continue", "--repoll-prs"])
    assert result.exit_code == ExitCode.SUCCESS, result.output

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


# ---- stub_reconcile (§3.5.1, §13 row 45, §10; D80 / ADR-0098) -----------------------


def _put_consumer_at_verify_degraded(db: Path, repo: str = "acme-commons") -> None:
    """Phases 1-3 `SUCCEEDED`, phase 4 `DEGRADED`: `reentry.phase_floor` finds every phase
    already settled (`DEGRADED` is one of `_SETTLED_FOR_DEMOTION`'s two hard stops, and the walk
    never moves the floor onto it) and returns `None`, so §11.5 step 5 demotes nothing here. The
    fixture models a repo that finished migrating against a stub, not one interrupted mid-phase —
    which is what `stub_reconcile` actually reconciles, and keeps step 5 from being a confound on
    the assertions below.
    """
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        for phase, status in (
            (1, "SUCCEEDED"), (2, "SUCCEEDED"), (3, "SUCCEEDED"), (4, "DEGRADED"),
        ):
            conn.execute(
                "INSERT INTO phases (run_id, repo_id, phase, status, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (RUN_ID, repo, phase, status, "2026-08-08T12:00:00.000000+00:00"),
            )
    finally:
        conn.close()


def _put_stub(
    db: Path,
    *,
    consumer: str = "acme-commons",
    provider: str = "acme-billing",
    coord_key: str = "acme-billing@1.0.0",
    state: str = "ACTIVE",
) -> None:
    """One open `stubs` row exactly as `--stub-blocked` would have written it (§3.5.1): the
    consumer is `DEGRADED` against a provider that never opened a PR, so `stub_reconcile`'s only
    legal decision is T4 -> `ABANDONED` with `abandon_reason='END_OF_RUN'` — §13 row 45's
    carve-out needs an open PR, and this fixture seeds none.
    """
    stamp = "2026-08-08T12:00:00.000000+00:00"
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO stubs (stub_id, run_id, repo_id, stub_coord_key, consumer_repo_id, "
            "                   provider_repo_id, pinned_version, bazel_label, state, "
            "                   stub_fidelity, revalidation_round, max_revalidation_rounds, "
            "                   state_changed_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PUBLISHED_ARTIFACT', 0, 2, ?, ?)",
            (
                "22222222-2222-4222-8222-222222222222",
                RUN_ID, consumer, coord_key, consumer, provider, "1.0.0",
                f"//third_party/stubs/{provider}", state, stamp, stamp,
            ),
        )
    finally:
        conn.close()


def test_resume_reconciles_an_open_stub_unconditionally_even_without_repoll(
    workspace: Path,
) -> None:
    """D80 / ADR-0098: `stub_reconcile` runs inside `fleet resume` UNCONDITIONALLY — not gated on
    `--repoll-prs` — because §10's `fleet resume` row, §13 row 35 and §3.5.1 all require it and
    only §11.5's own numbered list omitted it. A plain, unflagged `fleet resume` is the case that
    matters: `repoll_prs` stays `"not-requested"` there, and gating this step on
    `repoll == "polled"` would silently never reconcile a stub on that path — the exact failure
    §13 row 35 exists to prevent. Proving that requires NOT passing `--repoll-prs` here.

    The provider ("acme-billing") never opened a PR, so §13 row 45's carve-out does not apply and
    `orchestrator.stubs.reconcile()`'s only legal decision is T4: `ACTIVE` -> `ABANDONED`,
    `abandon_reason='END_OF_RUN'`, an `UnresolvedStub` finding. Asserted at three independent
    layers so a partial wire-up (writes the row but not the finding, or vice versa, or writes
    both but the projection still drops it) cannot pass: the `stubs` row itself, the `findings`
    row, and `migration_state.json#unresolved_stubs` — the §13 row 35 contract an operator
    actually reads to know the run needs a human.
    """
    db = workspace / "state" / "fleet.db"
    _put_consumer_at_verify_degraded(db)
    _put_stub(db)

    result = runner.invoke(app, [*base_args(workspace), "--json", "resume", "--no-continue"])
    assert result.exit_code == ExitCode.SUCCESS, result.output
    payload = json.loads(result.stdout)
    assert payload["repoll_prs"] == "not-requested", "the fixture must not imply --repoll-prs"
    assert payload["stub_reconcile"]["abandoned"] == ["acme-commons→acme-billing@1.0.0"]
    assert payload["stub_reconcile"]["held_for_merge"] == []

    conn = sqlite3.connect(db)
    try:
        state, abandon_reason, resolved_at = conn.execute(
            "SELECT state, abandon_reason, resolved_at FROM stubs "
            " WHERE run_id = ? AND consumer_repo_id = 'acme-commons' "
            "   AND stub_coord_key = 'acme-billing@1.0.0'",
            (RUN_ID,),
        ).fetchone()
        finding = conn.execute(
            "SELECT kind, severity FROM findings "
            " WHERE run_id = ? AND repo_id = 'acme-commons' AND kind = 'UnresolvedStub'",
            (RUN_ID,),
        ).fetchone()
    finally:
        conn.close()
    assert (state, abandon_reason) == ("ABANDONED", "END_OF_RUN")
    assert resolved_at is not None, "schema.sql CHECKs a non-ACTIVE row carries one"
    assert finding == ("UnresolvedStub", "warn")

    published = json.loads((workspace / "migration_state.json").read_text())
    assert published["unresolved_stubs"] == {"acme-commons": ["acme-billing@1.0.0"]}
    assert published["repos"]["acme-commons"]["stub_states"] == {
        "acme-billing@1.0.0": "ABANDONED"
    }


def _seed_fresh_pr_record(db: Path, repo: str, *, state: str, url: str) -> None:
    """Like `_seed_pr_record`, but `created_at` is real "now" rather than that helper's fixed
    2026-08-08 stamp. `_awaiting_merge` bounds the §13 row 45 carve-out by
    `pr.merge_wait_timeout_s` (default 2 days) measured against wall-clock `_now()`, so a fixed
    past `created_at` would silently age out of the carve-out as real time moves past it — this
    test is about whether the row is held AT ALL, not about the age bound, so the PR must be
    fresh at whatever moment the suite actually runs.
    """
    from fleet.cli import PR_RECORD_KIND, _fingerprint

    now = datetime.now(UTC).isoformat(timespec="microseconds")
    payload = json.dumps(
        {
            "run_id": RUN_ID, "repo_id": repo, "wave_index": 0,
            "branch": f"migrate/{repo}", "base": "integration",
            "title": f"migrate {repo}", "body": "body",
            "source_url": f"https://example.invalid/{repo}", "source_sha": "a" * 40,
            "state": state, "url": url, "created_at": now,
        }
    )
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO findings (run_id, repo_id, kind, severity, fingerprint, payload, "
            "                      created_at) VALUES (?, ?, ?, 'info', ?, ?, ?)",
            (
                RUN_ID, repo, PR_RECORD_KIND,
                _fingerprint(RUN_ID, repo, PR_RECORD_KIND), payload, now,
            ),
        )
    finally:
        conn.close()


def test_resume_stub_reconcile_holds_a_stub_whose_provider_still_has_an_open_pr(
    workspace: Path,
) -> None:
    """§13 row 45's carve-out, exercised through the wired CLI path rather than
    `orchestrator.stubs.reconcile()` called directly: a provider with a `DRAFTED` PR is a human
    mid-review, not a giveup, so the row is left open and reported under `held_for_merge` instead
    of abandoned — and no `UnresolvedStub` finding is written for it.
    """
    db = workspace / "state" / "fleet.db"
    _put_consumer_at_verify_degraded(db)
    _put_stub(db)
    _seed_fresh_pr_record(
        db, "acme-billing", state="DRAFTED",
        url="https://github.invalid/acme/monorepo/pull/2",
    )

    result = runner.invoke(app, [*base_args(workspace), "--json", "resume", "--no-continue"])
    assert result.exit_code == ExitCode.SUCCESS, result.output
    payload = json.loads(result.stdout)
    assert payload["stub_reconcile"]["abandoned"] == []
    assert payload["stub_reconcile"]["held_for_merge"] == ["acme-commons→acme-billing@1.0.0"]

    conn = sqlite3.connect(db)
    try:
        state = conn.execute(
            "SELECT state FROM stubs WHERE run_id = ? AND consumer_repo_id = 'acme-commons' "
            "  AND stub_coord_key = 'acme-billing@1.0.0'",
            (RUN_ID,),
        ).fetchone()[0]
        finding = conn.execute(
            "SELECT COUNT(*) FROM findings WHERE run_id = ? AND kind = 'UnresolvedStub'",
            (RUN_ID,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert state == "ACTIVE", "held, not abandoned — the provider's PR is still open"
    assert finding == 0


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

    result = runner.invoke(
        app, [*base_args(workspace), "resume", "--no-continue", "--raise-budget", "50"]
    )
    assert result.exit_code == ExitCode.SUCCESS, result.output

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

    result = runner.invoke(
        app, [*base_args(workspace), "resume", "--no-continue", "--raise-budget", "1"]
    )
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

    result = runner.invoke(app, [*base_args(workspace), "resume", "--no-continue"])
    assert result.exit_code == ExitCode.SUCCESS, result.output

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

    result = runner.invoke(app, [*base_args(workspace), "resume", "--no-continue"])
    assert result.exit_code == ExitCode.SUCCESS, result.output

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
        ps_fails: str | None = None,
    ) -> None:
        self.present = list(present)
        self.rm_fails = set(rm_fails)
        self.removed: list[str] = []
        self.listings = 0
        #: `docker ps` exits non-zero with this stderr — a stopped daemon, or a socket this user
        #: cannot open. `present` is deliberately still populated: the containers ARE there, and
        #: the sweep simply cannot be told about them. A fake that also emptied `present` would
        #: be testing the empty-inventory case under a different name.
        self._ps_fails = ps_fails
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
            if self._ps_fails is not None:
                return _proc(args, 1, stderr=self._ps_fails)
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

    result = runner.invoke(app, [*base_args(workspace), "--json", "resume", "--no-continue"])
    assert result.exit_code == ExitCode.SUCCESS, result.output
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

    clean = runner.invoke(app, [*base_args(workspace), "--json", "resume", "--no-continue"])
    assert clean.exit_code == ExitCode.SUCCESS, clean.output
    assert json.loads(clean.stdout)["reaped_worktrees"]["reaped"] == []

    injected = _cut(repo, workspace, _sandbox("acme-commons", 4))

    after = runner.invoke(app, [*base_args(workspace), "--json", "resume", "--no-continue"])
    assert after.exit_code == ExitCode.SUCCESS, after.output
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

    result = runner.invoke(app, [*base_args(workspace), "--json", "resume", "--no-continue"])
    assert result.exit_code == ExitCode.SUCCESS, result.output

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


def test_resume_dry_run_does_not_read_a_dead_workers_row_as_a_claim_on_its_sandbox(
    workspace: Path,
) -> None:
    """`_LIVE_SANDBOX_PREDICATE`'s negation, on the ONE path where it is load-bearing.

    Step 2 runs after step 3's sweep, so in a real resume every stale `RUNNING` row has already
    become `PENDING` and `status = 'RUNNING'` alone would give the same answer — which is exactly
    why the negation reads as deletable dead code. `--dry-run` does NOT run the sweep: it only
    counts what the sweep would reclaim, so step 2 sees the unswept rows, and without the
    negation a crashed run's own corpse claims its own orphans and the preview reports nothing to
    reap.

    **What this measures, and why the defect can move it (CLAUDE.md guardrail 6, the fourth
    question).** The state built is the one the predicate is *about*: a `phases` row, `RUNNING`,
    with a heartbeat older than both horizons, at `attempts = 2` — so a predicate that called it
    live would spare rungs 2 and 3, and the worktree cut here is rung 3. The two discriminating
    assertions therefore read empty/non-empty in opposite directions under
    `NOT (1 …)` → `NOT (0 …)`.
    `test_resume_dry_run_names_the_orphans_it_would_reap_and_removes_none`
    above builds **no `phases` rows at all**, so its live set is empty either way and no change to
    the liveness predicate can move its outcome; ADR-0081 §2 credited it with this job from
    `ead96e6`, the commit that landed the ADR, until the correction that retracted it.
    """
    repo = _reap_workspace(workspace)
    db = workspace / "state" / "fleet.db"
    _put_leased(db, "acme-commons", heartbeat_at=STALE_HEARTBEAT, attempts=2)

    orphan = _cut(repo, workspace, _sandbox("acme-commons", 3))

    result = runner.invoke(app, [*base_args(workspace), "--json", "resume", "--dry-run"])
    assert result.exit_code == ExitCode.SUCCESS, result.output
    payload = json.loads(result.stdout)

    # The CONTROLS. Both hold under the neutered negation too: the row is stale by both clocks
    # whatever step 2 concludes, and a preview removes nothing by definition.
    assert payload["stale_running_reset"] == 1, "the row was not stale by step 3's own reckoning"
    assert orphan.exists(), "a --dry-run reaped a worktree"
    # The DISCRIMINATING assertions.
    assert payload["live_sandbox_names"] == [], (
        "a dead worker's RUNNING row was read as a live claim on its sandbox — the negation in "
        "`_LIVE_SANDBOX_PREDICATE` is gone, and only `--dry-run` can see it go"
    )
    assert payload["reaped_worktrees"]["reaped"] == [_sandbox("acme-commons", 3)], (
        "the preview told an operator with a crashed run and an orphan on disk that there was "
        "nothing to reap"
    )


def test_resume_dry_run_preview_cannot_name_a_worktree_outside_the_run_namespace(
    workspace: Path,
) -> None:
    """The preview's OWN prefix filter, which the non-dry namespace test above cannot reach.

    `test_resume_reap_cannot_reach_a_worktree_outside_the_run_namespace` runs the non-dry path,
    so it exercises `WorktreeManager.reap`'s filter and never the copy `--dry-run` applies to its
    own `list_registered()` listing. The docstring on `_reap_orphan_worktrees` asserts containment
    "in EITHER branch"; this is the second branch.

    **What this measures, and why the defect can move it.** Two neighbours are *registered in the
    same git repository* the preview interrogates — another run's sandbox, and a worktree with no
    `fleet-` prefix at all — so the prefix filter has a non-empty input to reject, which is the
    thing the single pre-existing `--dry-run` case (one registered worktree) never gave it. Drop
    `p.name.startswith(prefix)` and the previewed list grows to include them.

    Why the non-`fleet-` neighbour is named `wt-WT1-example` and not something abstract: that is a
    real worktree of this repository, quoted in ADR-0074 as live evidence. `--dry-run` removes
    nothing itself — **the operator is the removal path** — so a preview that named it would have
    a human run `git worktree remove` on another lane's evidence.
    """
    repo = _reap_workspace(workspace)
    other_run = _cut(repo, workspace, "fleet-99999999-9999-4999-8999-999999999999-acme-1")
    unrelated = _cut(repo, workspace, "wt-WT1-example")
    orphan = _cut(repo, workspace, _sandbox("acme-commons", 1))

    result = runner.invoke(app, [*base_args(workspace), "--json", "resume", "--dry-run"])
    assert result.exit_code == ExitCode.SUCCESS, result.output
    payload = json.loads(result.stdout)

    # The CONTROLS. A preview removes nothing whatever its filter says, so all three survive
    # before and after the defect; what the operator would then act on is the list below.
    assert other_run.exists() and unrelated.exists() and orphan.exists(), (
        "a --dry-run reaped a worktree"
    )
    # The DISCRIMINATING assertion — equality, not membership: a widened filter ADDS names.
    assert payload["reaped_worktrees"]["reaped"] == [_sandbox("acme-commons", 1)], (
        "the preview offered an operator a worktree outside `fleet-<run_id>-*` to remove"
    )


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
        result = runner.invoke(app, [*base_args(workspace), "resume", "--no-continue"])

    assert result.exit_code == ExitCode.SUCCESS, result.output
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
        result = runner.invoke(app, [*base_args(workspace), "--json", "resume", "--no-continue"])

    assert result.exit_code == ExitCode.SUCCESS, result.output
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
        result = runner.invoke(app, [*base_args(workspace), "resume", "--no-continue"])

    assert result.exit_code == ExitCode.SUCCESS, result.output
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
        result = runner.invoke(app, [*base_args(workspace), "resume", "--no-continue"])

    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert f"FAILED to reap container {stuck}" in result.output
    assert "is in use" in result.output, "docker's own reason was discarded"
    assert "no orphan containers" not in result.output


def test_resume_dry_run_preview_spares_the_container_a_live_row_claims(
    workspace: Path,
) -> None:
    """The preview's own `claims()` filter, which no test put an input in front of.

    The container preview must not call `reap()` (it removes), so it re-applies `reap()`'s two
    filters — the run prefix, then `claims()` — to a listing of its own. The prefix half is
    enforced upstream by `docker ps --filter name=`, so `claims()` is the only part of that copy
    with anything left to do.

    **What this measures, and why the defect can move it.** The fixture puts a live row and a
    LIVE CONTAINER in front of the preview: a `RUNNING` row heart-beating now at `attempts = 2`,
    and a container named `<sandbox>-t<token>` on rung 3 — the rung `_live_sandbox_names` derives
    from `attempts + 1`. The single pre-existing `--dry-run` case ran under the autouse docker
    fake with an EMPTY inventory, so its `claims()` had no input and the filter could be deleted
    without moving anything it watched. Drop `and not any(claims(...))` here and the previewed
    list gains `live_container` — a running build offered to an operator as reapable, and the
    operator is the removal path.

    The `-t` tokens differ so no assertion on this path can be satisfied by string equality.
    """
    _reap_workspace(workspace)
    db = workspace / "state" / "fleet.db"
    fresh = datetime.now(UTC).isoformat(timespec="microseconds")
    _put_leased(db, "acme-commons", heartbeat_at=fresh, attempts=2)

    live_container = f"{_sandbox('acme-commons', 3)}-t0badcafe"
    orphan_container = f"{_sandbox('acme-commons', 1)}-tdeadbeef"
    docker = _ScriptedDocker(present=[live_container, orphan_container])

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("fleet.cli._reap_container_sandbox", lambda: ContainerSandbox(runner=docker))
        result = runner.invoke(app, [*base_args(workspace), "--json", "resume", "--dry-run"])

    assert result.exit_code == ExitCode.SUCCESS, result.output
    payload = json.loads(result.stdout)

    # The CONTROLS. Neither moves under the defect: a preview issues no `docker rm`, and the live
    # set is computed before the preview and is what the deleted filter would have CONSULTED.
    assert docker.removed == [], "a --dry-run removed a container"
    assert payload["live_sandbox_names"] == [
        _sandbox("acme-commons", 2), _sandbox("acme-commons", 3),
    ], "the filter under test was handed an empty live set, so it had no input to reject"
    # The DISCRIMINATING assertion.
    assert payload["reaped_containers"]["reaped"] == [orphan_container], (
        "the preview offered an operator a container a live row claims — `docker rm --force` on "
        "a running build, executed by the human the preview is written for"
    )


def test_resume_dry_run_reports_a_docker_ps_it_could_not_run_instead_of_no_orphans(
    workspace: Path,
) -> None:
    """D73's second residual: the `--dry-run` preview read a LENIENT listing that returned `[]`
    for a stopped daemon and for a run with no containers alike, so a health check run during a
    docker outage printed `no orphan containers`. (That wrapper, `ContainerSandbox.list_by_prefix`,
    was deleted in `f10a863` once the move to `list_with_verdict` left it with no callers.)

    That is the one answer a preview must never fabricate. `--dry-run` exists to be believed —
    an operator runs it precisely when they suspect debris — and "there is nothing wrong" is the
    false reassurance D73 is about. The non-preview branch has distinguished the two since
    `cfd89c7`; only the preview still collapsed them.

    **What this measures.** `docker.removed` and the previewed name list are CONTROLS: a preview
    removes nothing by definition, and a failed listing yields an empty preview both before and
    after the fix, so neither quantity moves under the defect. The discriminating assertions are
    on the reported VERDICT — that `_reap_lines` printed the sweep as not having run, in docker's
    own words, and that the clean headline is absent.

    The container fixture still HOLDS an orphan: docker knows about it and simply cannot say so.
    A fixture with an empty `present` would be testing the honestly-empty case by another name.
    """
    _reap_workspace(workspace)
    orphan = f"{_sandbox('acme-commons', 1)}-tdeadbeef"
    docker = _ScriptedDocker(
        present=[orphan],
        ps_fails="Cannot connect to the Docker daemon at unix:///var/run/docker.sock.",
    )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("fleet.cli._reap_container_sandbox", lambda: ContainerSandbox(runner=docker))
        result = runner.invoke(app, [*base_args(workspace), "resume", "--dry-run"])

    assert result.exit_code == ExitCode.SUCCESS, result.output
    # The CONTROLS. Neither moves under the defect.
    assert docker.removed == [], "a --dry-run removed a container"
    assert orphan not in result.output, (
        "the preview cannot name a container it was never told about — true before the fix too"
    )
    # The DISCRIMINATING assertions.
    assert "the container sweep did not run" in result.output, (
        "a preview that could not read the inventory reported on it anyway"
    )
    assert "Cannot connect to the Docker daemon" in result.output, (
        "docker's own reason is what tells the operator to start the daemon and re-run"
    )
    assert "no orphan containers" not in result.output, (
        "telling an operator their fleet is clean because docker would not answer is the defect"
    )


# `test_resume_step_5_refusal_does_not_share_an_exit_code_with_a_crash` and
# `test_the_step_5_refusal_does_not_call_step_2_absent_beside_its_own_step_2_lines` used to live
# here. **ADR-0080 deleted them with their subject.** Both were tests OF `ResumeIncompleteError`:
# the first asserted its exit code was 2 and not 1, the second parsed the `Step(s) N (gloss)
# is/are absent` enumeration out of its message and asserted which numbers appeared. There is no
# such message and no such class, so neither could be rewritten into anything that discriminates
# — a kept shell asserting "exit 0, and the output does not enumerate absent steps" passes under
# every mutation of the code it used to guard, which is a deleted test wearing a passing costume.
#
# What was worth keeping was re-homed rather than dropped. The floor-rule restatement the first
# one pinned in rendered output survives in `_refuse_unbuilt_resume_flags`'s message and is
# asserted by `test_resume_refuses_the_flags_whose_behaviour_does_not_exist` above. The second
# one's rule — *the verb must not call a step absent in the same stdout that reports it running*
# — is now carried by `test_a_no_continue_resume_says_step_8_was_withheld_rather_than_absent`.

# `test_the_three_resume_re_entry_summaries_state_one_rule_and_it_is_not_the_earliest` used to
# live here. It moved to `tests/test_floor_rule_statements.py` (Layer E) with this change: the
# three sites it pins state the §11.5-step-5 re-entry-floor RULE, and that module measured
# itself blind to them — all nine of its cases passed while all three said "the earliest
# incomplete phase". Binding one rule in two files is the hand-maintained agreement that module
# exists to end, so this is a move and not a copy. What stayed here is what belongs to §10's
# command surface: the two rendered-output assertions above, which pin what the step-5 REFUSAL
# says, not what the floor rule is.


def test_the_reconciliation_payload_is_emitted_even_when_the_continuation_throws(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--json resume` prints ONE full report on stdout even if step 8 dies mid-delegate.

    Why the ordering is the assertion and not the exit code: a `fleet resume` writes the
    stale-lease sweep, the step-4 arbitration and `migration_state.json` BEFORE step 8 runs, so
    the only path on which an operator's tooling learns what was reconciled is the one where the
    thing after it failed. If `_emit` ran after the continuation rather than in a `finally`,
    stdout would be empty exactly when the verb has most to say.

    **Re-homed from `test_the_reconciliation_payload_is_emitted_before_the_step_5_refusal`,
    whose subject ADR-0080 deleted.** That test rode the exit-2 refusal, which was the normal
    outcome of a real resume; the normal outcome now is a continuation, so the guarantee moved to
    the failure mode that replaced it. It is strictly stronger: a `raise` after `_emit` was
    satisfied by any statement order at all, and this one is not satisfied by putting `_emit`
    after the continuation.

    Unique discriminator of: moving the `_emit` call out of the `finally` (stdout empty, the
    `json.loads` below raises), and of emitting the continuation as a SECOND document (the
    `json.loads` below raises on trailing data).
    """
    _step5_landed_fleet(workspace)

    async def explode(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise RuntimeError("the delegate fell over")

    from fleet import cli

    monkeypatch.setattr(cli, "_refuse_concurrent_mirror_run", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_build_impl", explode)

    result = runner.invoke(app, [*base_args(workspace), "--json", "resume"])
    assert result.exit_code == ExitCode.UNEXPECTED_ERROR, result.output

    payload = json.loads(result.stdout)  # empty stdout => the raise beat the emit
    assert payload["dry_run"] is False
    assert payload["reentry_floors"]["applied"] is True, (
        "step 5's write is the thing this payload exists to report, and it did not happen"
    )
    assert payload["projection"], "the payload does not name the projection step 7 wrote"
    assert payload["continuation"] is None, (
        "a continuation that threw is reported as having produced a result"
    )
    assert payload["git_arbitration"] is not None
    assert (workspace / "migration_state.json").exists()


def test_a_no_continue_resume_writes_the_reconciliation_and_withholds_only_step_8(
    workspace: Path,
) -> None:
    """`--no-continue` is not a preview: everything §11.5 writes is written, at exit 0.

    This is the flag the §11.5-step tests in this file pass, so it earns a case of its own
    rather than being trusted because thirty other tests use it. The pair with `--dry-run` is the
    point: `--dry-run` withholds the WRITES and step 8; `--no-continue` withholds step 8 alone.

    Unique discriminator of: `no_continue` not reaching `withheld` (the continuation runs and
    `payload["continuation"]` is a dict), and of `--no-continue` short-circuiting before
    `_resume_impl` (the stale lease is not reclaimed and no projection is written).
    """
    db = workspace / "state" / "fleet.db"
    _put_leased(db, "acme-commons", heartbeat_at=STALE_HEARTBEAT, attempts=2, fence=4)

    result = runner.invoke(app, [*base_args(workspace), "--json", "resume", "--no-continue"])
    assert result.exit_code == ExitCode.SUCCESS, result.output

    payload = json.loads(result.stdout)
    assert payload["continuation"] is None, "`--no-continue` ran step 8 anyway"
    assert payload["stale_running_reset"] == 1, "`--no-continue` also withheld step 3's write"
    assert payload["projection"], "`--no-continue` also withheld step 7"
    status, attempts, fence, _owner, _hb = _phase_row(db, "acme-commons")
    assert (status, attempts, fence) == ("PENDING", 2, 5), (
        "`--no-continue` behaved as a preview; it withholds step 8 and nothing else"
    )


def test_a_no_continue_resume_says_step_8_was_withheld_rather_than_absent(
    workspace: Path,
) -> None:
    """The verb never calls a step absent in the same stdout that reports it running.

    Re-homed from `test_the_step_5_refusal_does_not_call_step_2_absent_beside_its_own_step_2_
    lines`, which parsed the deleted refusal's absent-step enumeration. The defect class is the
    same one and it outlived its instance: an operator reading "step N is absent" beside that
    step's own output has to decide which half to believe, and the half that says "absent" sends
    them to do by hand what the verb already did.

    Unique discriminator of: rendering the withheld continuation as `"step 8: absent"` or
    omitting `_continue_lines` from the human path entirely (the first assertion), and of
    `_continue_lines` reporting a withheld step 8 as a completed one (the second).
    """
    result = runner.invoke(app, [*base_args(workspace), "resume", "--no-continue"])
    assert result.exit_code == ExitCode.SUCCESS, result.output
    output = " ".join(result.output.split())

    assert "step 8: withheld" in output, (
        f"a withheld step 8 is not reported as withheld: {output!r}"
    )
    assert "absent" not in output, (
        f"the report calls a §11.5 step absent beside the output of the steps that ran: {output!r}"
    )
    assert "continued" not in output, (
        "a withheld continuation is reported as one that ran"
    )


def test_a_scan_floor_is_named_in_the_payload_and_never_served(workspace: Path) -> None:
    """ADR-0080 ruling B: a `SCAN` floor is reported and skipped, never served.

    `fleet scan` takes five flags no resume carries and no `wave` at all, so serving a `SCAN`
    floor means step 8 choosing five values the operator never wrote. The skip is only safe
    because it is LOUD: on a fleet of un-started repos `computed_floors` is `SCAN`-heavy, and a
    silent skip is a `fleet resume` that exits 0 having migrated nothing.

    Unique discriminator of: `result[_SCAN_SKIPPED_KEY] = list(skipped)` -> `= []` in
    `_continue_impl` (the payload assertion), and of dropping `_continue_lines`' skip branch (the
    stdout assertion). Both are invisible to every other case in this file, which asserts on the
    reconciliation and not on step 8's report.

    The lease seeding is not decoration: `computed_floors` has one entry per repo with `phases`
    rows, so a fixture with no rows at all yields an EMPTY plan and would pass the "nothing was
    served" half while asserting nothing about the skip.
    """
    _put_leased(
        workspace / "state" / "fleet.db",
        "acme-commons",
        heartbeat_at=STALE_HEARTBEAT,
        attempts=2,
        fence=4,
    )
    result = runner.invoke(app, [*base_args(workspace), "--json", "resume"])
    assert result.exit_code == ExitCode.SUCCESS, result.output

    continuation = json.loads(result.stdout)["continuation"]
    assert continuation is not None, "step 8 did not run on a plain `fleet resume`"
    assert continuation["scan_floor_not_continued"] == ["acme-commons"], (
        "the repo whose floor is SCAN was dropped from the report instead of named in it"
    )
    assert continuation["driven"] == [], "a SCAN floor was served"

    human = runner.invoke(app, [*base_args(workspace), "resume"])
    assert human.exit_code == ExitCode.SUCCESS, human.output
    assert "NOT continued" in " ".join(human.output.split()), (
        "the skip is invisible to an operator not reading `--json`"
    )


def test_the_continuation_is_driven_from_step_5s_floors_and_the_declared_config(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Step 8 is reached with step 5's OWN floors and with config-declared knobs.

    Two claims, both of which a green suite would otherwise be blind to. **The floors** come from
    `computed_floors`, not from the `reentry_floors` report: the report's `unchanged` rows carry a
    reason and no floor, so a step 8 fed from the report continues every repo that needed a
    demotion and silently drops every repo that did not — which on a healthy fleet is most of it.
    **The knobs** are the operator's declared config, never a literal at the call site; a literal
    here is character-for-character the recorded `effort: low` defect.

    Unique discriminator of: passing `reentry_floors` instead of `computed_floors` to
    `_continue_from_floors` (the floors assertion), and of replacing `budgets.build_timeout_s` at
    the call site with `fleet build --timeout`'s Typer literal (the `timeout_s` assertion).

    **The fixture is chosen so the first of those is EXPRESSIBLE, and the obvious one is not.**
    Measured: with `_step5_landed_fleet`'s repo — Phase 3 `SUCCEEDED`, evidence gone, so step 5
    DEMOTES it — the report-derived mapping and `computed_floors` agree, the mutation is a
    semantic no-op and this case passes under the exact defect it exists to catch. The two
    anchors have to differ: here Phase 3 is `PENDING`, so the floor is still `BUILD` and step 5
    writes nothing, the repo lands in `unchanged` with no `floor` key, and a step 8 fed from the
    report plans nothing at all. That is the healthy-fleet case, which is most of a fleet.
    """
    write_config(
        workspace,
        fleet=CONTINUE_KNOBS_YAML,
    )
    _reseal_config_digests(workspace)
    _step5_mirror(workspace)
    _worktree, anchor = _arbitration_worktree(workspace)
    _step5_seed(
        workspace / "state" / "fleet.db",
        statuses={1: "SUCCEEDED", 2: "SUCCEEDED", 3: "PENDING", 4: "PENDING"},
        post_commit_sha={2: anchor},
    )

    seen: dict[str, object] = {}

    async def recorder(*_args: object, **kwargs: object) -> dict[str, object]:
        seen.update(kwargs)
        return {"exit_code": int(ExitCode.SUCCESS), "run_id": RUN_ID}

    async def quiet(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {"exit_code": int(ExitCode.SUCCESS), "run_id": RUN_ID}

    from fleet import cli

    monkeypatch.setattr(cli, "_refuse_concurrent_mirror_run", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_build_impl", recorder)
    # The floor is `BUILD`, so the plan SPANS up to `VERIFY` and the driver really enters
    # `_verify_impl` — a repo built and not verified is the half of C1 above the floor. It is
    # stubbed separately rather than with `recorder` so `seen` stays `_build_impl`'s kwargs
    # alone, which is what the two knob assertions below read.
    monkeypatch.setattr(cli, "_verify_impl", quiet)

    result = runner.invoke(app, [*base_args(workspace), "--json", "resume"])
    assert result.exit_code == ExitCode.SUCCESS, result.output

    payload = json.loads(result.stdout)
    assert payload["reentry_floors"]["demoted"] == [], (
        "the fixture demoted something, so the report and `computed_floors` agree and the "
        "floors-source mutation is a no-op here"
    )
    continuation = payload["continuation"]
    assert [entry["phase"] for entry in continuation["plan"]] == ["BUILD", "VERIFY"], (
        "step 5's computed floor did not reach step 8's plan"
    )
    assert continuation["driven"] == ["BUILD", "VERIFY"]
    assert seen["timeout_s"] == 999, "`budgets.build_timeout_s` did not reach `_build_impl`"
    assert seen["sandboxed"] is True
    assert seen["wave"] is None, "step 8 invented a wave the operator never named"


def test_the_continuation_takes_the_disk_headroom_gate_before_its_first_delegate(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§12.22's disk-headroom refusal, on the `fleet resume` continuation (subtask 10c).

    `fleet build` and `fleet verify` call `_require_disk_headroom` at the top of their own
    command bodies (cli.py's two `_require_disk_headroom(settings)` calls) and exit 9 before any
    phase work starts. ADR-0080 §7 records that `_continue_impl` re-enters `_build_impl`/
    `_verify_impl` directly and does NOT take it — "newly reachable" once step 8 stopped being a
    refusal. `_continue_impl` already takes one gate exactly this way, once and "before the first
    delegate" (its own docstring, ADR-0080 ruling C, for the mirror mutex); this is the same
    shape for the same reason: a precondition of doing phase work at all, not a property of
    which phase runs first.

    Unique discriminator of: a missing, or per-delegate-only, `_require_disk_headroom` call on
    this path. Pre-fix, nothing blocks the plan and the monkeypatched `_build_impl` below runs
    (`seen` is populated); post-fix, `DiskExhaustedError` (§10 exit 9) is raised before it and
    `seen` stays empty.
    """
    write_config(workspace, fleet=DISK_FLOOR_YAML)
    _reseal_config_digests(workspace)
    _step5_mirror(workspace)
    _worktree, anchor = _arbitration_worktree(workspace)
    _step5_seed(
        workspace / "state" / "fleet.db",
        statuses={1: "SUCCEEDED", 2: "SUCCEEDED", 3: "PENDING", 4: "PENDING"},
        post_commit_sha={2: anchor},
    )

    seen: dict[str, object] = {}

    async def recorder(*_args: object, **kwargs: object) -> dict[str, object]:
        seen.update(kwargs)
        return {"exit_code": int(ExitCode.SUCCESS), "run_id": RUN_ID}

    from fleet import cli

    monkeypatch.setattr(cli, "_refuse_concurrent_mirror_run", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_build_impl", recorder)

    result = runner.invoke(app, [*base_args(workspace), "resume"])

    assert result.exit_code == ExitCode.DISK_EXHAUSTED == 9, result.output
    assert str(IMPOSSIBLE_FLOOR) in result.output, result.output
    assert "min_free_bytes" in result.output, result.output
    assert seen == {}, "the BUILD delegate ran before the disk floor was checked"


def test_a_halting_delegate_hands_its_own_exit_code_back_through_resume(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§10's exit code is the halting delegate's, read off ITS payload — not re-derived here.

    `_raise_for_continuation` delegates to `_raise_for_phase` precisely so §10's table is not
    copied a fourth time. This is the case that proves the wire reaches `fleet resume`: without
    it, `_raise_for_continuation` could be a no-op on this path and every other case in this file
    would still be green, because they all reach a `SCAN`-only plan that never halts.

    Unique discriminator of: dropping the `_raise_for_continuation(continuation)` call at the end
    of `resume` (exit 0 instead of 10), and of raising on `halted is None` (the `--no-continue`
    and SCAN-only cases above go red instead).
    """
    _step5_landed_fleet(workspace)

    async def halting(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {
            "exit_code": int(ExitCode.WAVE_COST_EXHAUSTED),
            "run_id": RUN_ID,
            "halt": "the wave ceiling is spent",
        }

    from fleet import cli

    monkeypatch.setattr(cli, "_refuse_concurrent_mirror_run", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_build_impl", halting)

    result = runner.invoke(app, [*base_args(workspace), "--json", "resume"])
    assert result.exit_code == ExitCode.WAVE_COST_EXHAUSTED, result.output

    payload = json.loads(result.stdout)
    assert payload["continuation"]["halted_phase"] == "BUILD"
    assert payload["projection"], (
        "the reconciliation was not reported before the halting exit code ended the process"
    )


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
    result = runner.invoke(app, [*base_args(workspace), "resume", "--no-continue", "--repoll-prs"])

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

    result = runner.invoke(
        app, [*base_args(workspace), "resume", "--no-continue", "--raise-budget", "5"]
    )
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
# §11.5 step 4 — Git is the arbiter of whether a RUNNING task's commit landed
# --------------------------------------------------------------------------------------

ARB_TASK = "22222222-2222-4222-8222-222222222222"
ARB_ATTEMPT = "33333333-3333-4333-8333-333333333333"
ARB_PHASE = 2
ARB_ANCHOR_REF = f"refs/fleet/{RUN_ID}/acme-commons/phase-2/base"
ARB_OTHER_TASK = "55555555-5555-4555-8555-555555555555"
ARB_OTHER_ATTEMPT = "66666666-6666-4666-8666-666666666666"
ARB_OTHER_ANCHOR_REF = f"refs/fleet/{RUN_ID}/acme-billing/phase-2/base"


def _git_out(repo: Path, *args: str) -> str:
    """The read half of `_git_in`, kept beside it for the same reason: one fixed-argv site."""
    return subprocess.run(  # noqa: S603 - fixed argv built here, never a shell, no test input
        ["git", "-C", str(repo), *args],  # noqa: S607 - `git` from PATH, as every suite does
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _arbitration_worktree(workspace: Path, repo_id: str = "acme-commons") -> tuple[Path, str]:
    """A REAL `work/<repo>` checkout on `migrate/<repo>` with a REAL phase anchor ref.

    Real git, not a scripted runner: step 4's whole claim is that Git — not a row, not a fake —
    decides whether the work landed, and a fixture that answered the trailer query itself would
    be asserting that the harness believes its own mock.
    """
    worktree = workspace / "work" / repo_id
    worktree.mkdir(parents=True)
    _git_in(worktree, "init", "--initial-branch=main", ".")
    _git_in(worktree, "config", "user.email", "fleet@example.invalid")
    _git_in(worktree, "config", "user.name", "Fleet Test")
    (worktree / "src.java").write_text("class A {}\n", encoding="utf-8")
    _git_in(worktree, "add", "-A")
    _git_in(worktree, "commit", "-m", "phase 1 checkout")
    _git_in(worktree, "checkout", "-B", f"migrate/{repo_id}")
    anchor = _git_out(worktree, "rev-parse", "HEAD")
    _git_in(worktree, "update-ref", f"refs/fleet/{RUN_ID}/{repo_id}/phase-2/base", anchor)
    return worktree, anchor


def _land_task_commit(worktree: Path, task_id: str = ARB_TASK) -> str:
    """The commit §3.2 step 6 would have made: a real commit carrying a real `Fleet-Task-Id`."""
    (worktree / "dest.java").write_text("class B {}\n", encoding="utf-8")
    _git_in(worktree, "add", "-A")
    _git_in(worktree, "commit", "-m", f"relocate acme-commons\n\nFleet-Task-Id: {task_id}")
    return _git_out(worktree, "rev-parse", "HEAD")


def _seed_running_task(
    db: Path,
    *,
    repo_id: str = "acme-commons",
    task_id: str = ARB_TASK,
    attempt_id: str = ARB_ATTEMPT,
    task_anchor: str,
    phase_anchor: str,
    base_ref: str | None = ARB_ANCHOR_REF,
    attempts: int = 2,
    heartbeat_at: str | None = STALE_HEARTBEAT,
    with_attempt_row: bool = True,
) -> None:
    """A crash caught mid-task: `phases` RUNNING with an anchor and a DEAD heartbeat, `tasks`
    RUNNING with its own anchor, and the `attempts` row of the rung that was interrupted.

    **`heartbeat_at` defaults to `STALE_HEARTBEAT`, and that default is load-bearing.** It is the
    real crashed shape: §11.5 step 3 reclaims the `phases` row (leaving `attempts` alone), and
    step 4 then meets a row no longer claimed by anyone. An earlier version of this helper wrote
    `heartbeat_at = NULL` to keep step 3 out of the way — which also made every fixture in this
    section incapable of presenting step 4 with a LIVE lease, and hid the defect V2 filed as C1.
    `heartbeat_at=<fresh>` is what
    `test_resume_step4_spares_the_task_whose_phase_lease_is_still_live` passes.

    `with_attempt_row=False` seeds a `RUNNING` task with **no** open `attempts` row — the state in
    which the `attempts.commit_sha` write has nothing to update while `phases.post_commit_sha` is
    written anyway.
    """
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO phases (run_id, repo_id, phase, status, attempts, base_ref, "
            "                    pre_commit_sha, heartbeat_at, lease_owner, lease_fence, "
            "                    updated_at) "
            "VALUES (?, ?, ?, 'RUNNING', ?, ?, ?, ?, 'host:cid:1:boot', 4, ?)",
            (RUN_ID, repo_id, ARB_PHASE, attempts, base_ref, phase_anchor, heartbeat_at,
             "2026-08-08T12:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO tasks (task_id, run_id, repo_id, phase, kind, dest_path, status, "
            "                   pre_commit_sha, fence_token, created_at) "
            "VALUES (?, ?, ?, ?, 'RELOCATE', 'java/acme-commons', 'RUNNING', ?, 4, ?)",
            (task_id, RUN_ID, repo_id, ARB_PHASE, task_anchor, "2026-08-08T12:00:00+00:00"),
        )
        if with_attempt_row:
            conn.execute(
                "INSERT INTO attempts (attempt_id, run_id, repo_id, task_id, phase, attempt, "
                "                      command, exit_code, started_at, finished_at) "
                'VALUES (?, ?, ?, ?, ?, 1, \'["git","apply"]\', 0, ?, ?)',
                (attempt_id, RUN_ID, repo_id, task_id, ARB_PHASE,
                 "2026-08-08T12:00:00+00:00", "2026-08-08T12:00:01+00:00"),
            )
    finally:
        conn.close()


def _arbitration_state(
    db: Path,
    *,
    task_id: str = ARB_TASK,
    attempt_id: str = ARB_ATTEMPT,
    repo_id: str = "acme-commons",
) -> tuple[Any, ...]:
    """(task status, task fence, attempts.commit_sha, phases.attempts, phases.post_commit_sha).

    `phases.status` and the lease columns are deliberately absent: §11.5 step 3's sweep writes
    them in the same command, and a tuple that mixed the two steps' writes could not be read as a
    statement about step 4.
    """
    conn = sqlite3.connect(db)
    try:
        task = conn.execute(
            "SELECT status, fence_token FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        attempt = conn.execute(
            "SELECT commit_sha FROM attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone() or (None,)
        phase = conn.execute(
            "SELECT attempts, post_commit_sha FROM phases WHERE run_id = ? AND repo_id = ? "
            "  AND phase = ?",
            (RUN_ID, repo_id, ARB_PHASE),
        ).fetchone()
    finally:
        conn.close()
    return (*task, *attempt, *phase)


def test_resume_step4_adopts_the_commit_git_says_landed_without_charging_an_attempt(
    workspace: Path,
) -> None:
    """§11.5 step 4's YES branch: Git found the trailer, so the row is corrected to match it.

    Why each half matters:

    * **The row is corrected, never Git.** §11.5's authority rule is one-directional — "on any
      disagreement about whether a change landed, Git is authoritative and the SQLite row is
      corrected. Never the reverse." A resume that re-ran this task would re-apply a patch whose
      effect is already on `migrate/<repo>`, and the §3.2 step 6.1 guard would then skip it and
      report `already_applied` for work this very command decided had not happened.
    * **`phases.attempts` is untouched, and that is asserted on the column.** The crash is not an
      attempt the repo made. Charging one here spends ADR-0014's three-rung ladder on work that
      *succeeded*, and three crashed resumes would send a repo whose transform landed cleanly to
      REQUIRES_HUMAN_INTERVENTION.
    * **The SHA reaches BOTH pointers.** `attempts.commit_sha` is the provenance of the rung and
      `phases.post_commit_sha` is what §11.5 step 5's evidence check reads; writing one and not
      the other leaves the next step arbitrating against a pointer nobody reconciled.
    """
    db = workspace / "state" / "fleet.db"
    worktree, anchor = _arbitration_worktree(workspace)
    sha = _land_task_commit(worktree)
    _seed_running_task(db, task_anchor=anchor, phase_anchor=anchor)

    result = runner.invoke(app, [*base_args(workspace), "--json", "resume", "--no-continue"])
    assert result.exit_code == ExitCode.SUCCESS, result.output

    report = json.loads(result.stdout)["git_arbitration"]
    assert report["candidates"] == 1
    assert [entry["task_id"] for entry in report["landed"]] == [ARB_TASK]
    assert report["discarded"] == [] and report["unresolved"] == []

    status, fence, attempt_sha, attempts, post = _arbitration_state(db)
    assert status == "DONE", "Git said the commit landed and the row still claims it is running"
    assert (attempt_sha, post) == (sha, sha), "the commit SHA did not reach both pointers"
    assert attempts == 2, "step 4 charged an attempt for work that had already landed"
    assert fence == 4, "an adopted task's fence was bumped; nothing was handed back"
    assert (worktree / "dest.java").exists(), "landed work was discarded from the worktree"


def test_resume_step4_discards_the_worktree_of_a_task_whose_commit_never_landed(
    workspace: Path,
) -> None:
    """§11.5 step 4's NO branch: nothing on the branch, so the debris goes and the rung re-runs.

    Why the discard is not optional: a killed `git apply` leaves the worktree dirty and nothing
    on the branch (§3.2 step 6.4). Re-entering the phase against that tree re-applies a patch on
    top of a half-applied one, and `git apply --check` then refuses a patch that is in fact
    perfectly good — the repo is failed for a defect the resume created.

    **The reset goes to `tasks.pre_commit_sha`, not to `phases.base_ref`**, and
    `vcs/commits.discard_task` refuses to express the other one. Resetting to the phase anchor
    would delete the commits of earlier tasks in the same phase whose rows are already `DONE` and
    will never re-run, leaving the phase's success criterion to pass on a tree missing most of
    its rewrites. §11.5 step 4's own sketch NAMED that anchor until `6bf198f` — past tense: a
    reader who greps the section for it today will not find it, because ADR-0087
    adjudicated it and the SPEC sentence was corrected in the same change.

    `attempts` is retained here too, and for a stronger reason than in the YES branch: nothing
    ran, so there is nothing to charge (`FailureClass.TRANSIENT_INFRA`, §11.5 step 4's own
    wording).
    """
    db = workspace / "state" / "fleet.db"
    worktree, phase_anchor = _arbitration_worktree(workspace)
    # An EARLIER task of the same phase, already `DONE` and already committed. Its commit sits
    # BETWEEN the phase anchor and this task's anchor, which is the whole reason the two anchors
    # exist — and the only arrangement in which the wrong one is distinguishable from the right
    # one. A fixture that anchored both at the same commit would pass under a `discard_task` call
    # handed `phases.pre_commit_sha`, and pass silently.
    _land_task_commit(worktree, task_id="44444444-4444-4444-8444-444444444444")
    task_anchor = _git_out(worktree, "rev-parse", "HEAD")
    debris = worktree / "half-applied.java"
    debris.write_text("class Broken {\n", encoding="utf-8")
    _seed_running_task(db, task_anchor=task_anchor, phase_anchor=phase_anchor)

    result = runner.invoke(app, [*base_args(workspace), "--json", "resume", "--no-continue"])
    assert result.exit_code == ExitCode.SUCCESS, result.output

    report = json.loads(result.stdout)["git_arbitration"]
    assert [entry["task_id"] for entry in report["discarded"]] == [ARB_TASK]
    assert report["landed"] == [] and report["unresolved"] == []

    status, fence, attempt_sha, attempts, post = _arbitration_state(db)
    assert status == "PENDING", "a task Git says never landed was not returned to the queue"
    assert fence == 5, "the row was handed back without invalidating the old holder's writes"
    assert attempts == 2, "step 4 charged an attempt for a rung that never ran"
    assert (attempt_sha, post) == (None, None), (
        "a SHA was recorded for a commit that is not on the branch"
    )
    assert not debris.exists(), "the half-applied tree survived the discard"
    # The DISCRIMINATING pair: both hold at `tasks.pre_commit_sha` and both fail at
    # `phases.pre_commit_sha`, which is the anchor §11.5 step 4's own sketch named.
    assert _git_out(worktree, "rev-parse", "HEAD") == task_anchor, (
        "the discard rewound past this task's own anchor, deleting an earlier DONE task's commit"
    )
    assert (worktree / "dest.java").exists(), "the earlier task's landed work was deleted"
    assert (worktree / "src.java").exists(), "the discard reached past the phase anchor too"


def test_resume_step4_recreates_the_missing_anchor_at_the_sha_it_named_not_at_the_tip(
    workspace: Path,
) -> None:
    """"The anchor ref itself is re-created from `phases.base_ref` if it is missing" (§11.5 step 4).

    The ref lives in Git and its NAME lives in SQLite (§11.5's authority table), so a lost ref is
    recoverable from `phases.pre_commit_sha` — and only from there. **Re-cutting it at the branch
    tip instead is the failure this test exists for**: the tip is *above* the commits step 4 is
    about to search for, so `<tip>..migrate/<repo>` is empty, every landed task arbitrates as
    "nothing landed", and the resume discards a phase's worth of green work while reporting a
    successful reconciliation. The assertion that separates the two is `landed`, not the mere
    existence of the ref: both recreations produce a ref.
    """
    db = workspace / "state" / "fleet.db"
    worktree, anchor = _arbitration_worktree(workspace)
    sha = _land_task_commit(worktree)
    _git_in(worktree, "update-ref", "-d", ARB_ANCHOR_REF)
    assert _git_out(worktree, "for-each-ref", "--format=%(refname)", ARB_ANCHOR_REF) == ""
    _seed_running_task(db, task_anchor=anchor, phase_anchor=anchor)

    result = runner.invoke(app, [*base_args(workspace), "--json", "resume", "--no-continue"])
    assert result.exit_code == ExitCode.SUCCESS, result.output

    report = json.loads(result.stdout)["git_arbitration"]
    assert [entry["ref"] for entry in report["anchors_recreated"]] == [ARB_ANCHOR_REF]
    assert _git_out(worktree, "rev-parse", ARB_ANCHOR_REF) == anchor, (
        "the anchor was re-cut somewhere other than the commit `phases.pre_commit_sha` names"
    )
    # The DISCRIMINATING assertion: an anchor re-cut at the tip yields an empty scoped range,
    # which reads as "nothing landed" and discards the very commit that did.
    assert [entry["commit_sha"] for entry in report["landed"]] == [sha]
    assert _arbitration_state(db)[0] == "DONE"


def test_resume_step4_dry_run_reports_both_verdicts_and_writes_neither(workspace: Path) -> None:
    """`--dry-run` is the free health check §11.5 promises: every git READ, no git or SQL write.

    Why the verdicts must still be *computed*: a preview that reported nothing about open tasks is
    indistinguishable from a run with none, and "one task's work is on the branch and one task's
    worktree is about to be thrown away" is precisely what an operator runs the health check to
    learn before committing to it.

    **Two repos, one per branch — and the fixture is what this test is about.** The first version
    of it seeded one landed task in one repo, which meant *neither* of the two `if not dry_run:`
    guards protecting a **git** write was reachable at all: the landed branch `continue`s before
    `discard_task`, and an anchor that exists returns from `_recreate_phase_anchor` before its
    `update_ref`. Deleting either guard left that test green, so "writes nothing" was proven for
    the SQL half alone (V2 finding I3). Here `acme-billing` did NOT land and its anchor ref has
    been deleted from git while `phases.base_ref`/`pre_commit_sha` still name it, so a real run
    would take both git-write paths and this preview must take neither.
    """
    db = workspace / "state" / "fleet.db"
    landed_wt, landed_anchor = _arbitration_worktree(workspace)
    sha = _land_task_commit(landed_wt)
    _seed_running_task(db, task_anchor=sha, phase_anchor=landed_anchor)

    other_wt, other_anchor = _arbitration_worktree(workspace, repo_id="acme-billing")
    debris = other_wt / "half-applied.java"
    debris.write_text("class Broken {\n", encoding="utf-8")
    _git_in(other_wt, "update-ref", "-d", ARB_OTHER_ANCHOR_REF)
    _seed_running_task(
        db,
        repo_id="acme-billing",
        task_id=ARB_OTHER_TASK,
        attempt_id=ARB_OTHER_ATTEMPT,
        task_anchor=other_anchor,
        phase_anchor=other_anchor,
        base_ref=ARB_OTHER_ANCHOR_REF,
    )

    result = runner.invoke(app, [*base_args(workspace), "--json", "resume", "--dry-run"])
    assert result.exit_code == ExitCode.SUCCESS, result.output

    report = json.loads(result.stdout)["git_arbitration"]
    # Both verdicts were REACHED — without this the git-write guards below are vacuous.
    assert [entry["commit_sha"] for entry in report["landed"]] == [sha]
    assert [entry["task_id"] for entry in report["discarded"]] == [ARB_OTHER_TASK]
    assert [entry["ref"] for entry in report["anchors_recreated"]] == [ARB_OTHER_ANCHOR_REF]
    assert report["applied"] is False

    # The three DISCRIMINATING assertions, one per guarded write.
    assert debris.exists(), "a --dry-run ran `discard_task` against a real worktree"
    assert _git_out(other_wt, "for-each-ref", "--format=%(refname)", ARB_OTHER_ANCHOR_REF) == "", (
        "a --dry-run re-created a git ref it was only asked to preview"
    )
    assert _arbitration_state(db) == ("RUNNING", 4, None, 2, None), (
        "a --dry-run wrote the reconciliation it was only asked to preview"
    )
    assert _arbitration_state(
        db, task_id=ARB_OTHER_TASK, attempt_id=ARB_OTHER_ATTEMPT, repo_id="acme-billing"
    ) == ("RUNNING", 4, None, 2, None)


def test_resume_step4_spares_the_task_whose_phase_lease_is_still_live(workspace: Path) -> None:
    """A worker alive by BOTH clocks keeps its worktree, its row and its fence.

    **This is the data-loss shape (V2 finding C1), and the ordering alone does not prevent it.**
    §11.5 step 3 resets `phases` rows and only the STALE ones — no `tasks` row, in either case —
    so a live worker's `RUNNING` tasks survive the sweep untouched. Without a liveness gate of its
    own, step 4 would ask Git about them and, on "nothing landed", `reset --hard` + `clean -fdx`
    the checkout the worker is writing into *right now*. And "nothing landed" is exactly what a
    healthy worker between `git apply` and `git commit` presents: dirty tree, no commit. That is
    the two-writer collision the lease exists to prevent, with the worktree destroyed rather than
    doubly written.

    The gate is composed from `_LIVE_SANDBOX_PREDICATE`, so it agrees with step 2 and step 3 by
    construction. That also settles the case a hand-rolled lease check gets wrong: `complete_phase`
    (`state/repository.py`) NULLs `lease_owner` and `lease_expires_at` in the same statement that
    writes the terminal status, so a guard reading those columns cannot tell a finished phase from
    an unclaimed one. The predicate reads `status` and the two heartbeat clocks instead.

    The CONTROL is `stale_running_reset == 0`: step 3 independently agrees this worker is alive,
    so a failure here cannot be read as "the fixture's heartbeat was stale after all".
    """
    db = workspace / "state" / "fleet.db"
    worktree, anchor = _arbitration_worktree(workspace)
    debris = worktree / "half-applied.java"
    debris.write_text("class Broken {\n", encoding="utf-8")
    fresh = datetime.now(UTC).isoformat(timespec="microseconds")
    _seed_running_task(db, task_anchor=anchor, phase_anchor=anchor, heartbeat_at=fresh)

    # Rendered output first, then the payload. Sparing writes nothing, so the second invocation
    # sees the identical state and the two readings are of one run's worth of behaviour.
    rendered = runner.invoke(app, [*base_args(workspace), "resume", "--no-continue"])
    assert rendered.exit_code == ExitCode.SUCCESS, rendered.output
    result = runner.invoke(app, [*base_args(workspace), "--json", "resume", "--no-continue"])
    assert result.exit_code == ExitCode.SUCCESS, result.output
    payload = json.loads(result.stdout)

    # The CONTROL.
    assert payload["stale_running_reset"] == 0, (
        "step 3 reclaimed this lease, so the fixture is not the live-worker case at all"
    )
    report = payload["git_arbitration"]
    # The DISCRIMINATING assertions.
    assert [entry["task_id"] for entry in report["spared_live"]] == [ARB_TASK]
    assert report["candidates"] == 0
    assert report["discarded"] == [] and report["landed"] == []
    assert debris.exists(), (
        "step 4 discarded the worktree of a worker step 3 had just declared alive"
    )
    assert _arbitration_state(db) == ("RUNNING", 4, None, 2, None), (
        "a live worker's task row was reconciled underneath it"
    )
    assert "phase lease is still live" in " ".join(rendered.output.split()), (
        "a spared task was dropped silently; 0 reconciled and 1 spared as live are the same "
        "output with opposite meanings"
    )


def test_resume_step4_reports_a_landed_commit_no_attempts_row_could_record(
    workspace: Path,
) -> None:
    """The two pointers §11.5 pairs can diverge, and the divergence must not be silent.

    `UPDATE attempts SET commit_sha = ? WHERE attempt_id = (SELECT … LIMIT 1)` over an empty
    subquery is `attempt_id = NULL`, which matches zero rows **without error**, while
    `phases.post_commit_sha` is written in the same unit regardless. §11.5's authority table pairs
    `attempts.commit_sha` with `phases.post_commit_sha` as the two pointers into Git that let
    resume ask a targeted question; §11.5 step 5's `evidence_holds` reads one of them. A resume
    that left them disagreeing and reported a clean reconciliation is the Rule 11 failure mode.

    The `phases` write is still made and that is deliberate, not an oversight: the commit really is
    on the branch, and that column points at Git. What must not happen is silence about the half
    that did not land.
    """
    db = workspace / "state" / "fleet.db"
    worktree, anchor = _arbitration_worktree(workspace)
    sha = _land_task_commit(worktree)
    _seed_running_task(db, task_anchor=anchor, phase_anchor=anchor, with_attempt_row=False)

    # Rendered output, not `--json`: the divergence has to reach the human who is reading the
    # reconciliation report, and the line is derivable only from `provenance_missing`.
    result = runner.invoke(app, [*base_args(workspace), "resume", "--no-continue"])
    assert result.exit_code == ExitCode.SUCCESS, result.output
    output = " ".join(result.output.split())

    # The CONTROL: the landed verdict itself is unaffected, so a failure below is about the
    # provenance write and not about the arbitration.
    status, _fence, _attempt_sha, attempts, post = _arbitration_state(db)
    assert (status, post, attempts) == ("DONE", sha, 2)
    assert f"landed as {sha[:12]}" in output

    # The DISCRIMINATING assertions.
    assert f"step 4: PROVENANCE acme-commons phase 2 task {ARB_TASK}" in output
    assert "NO `attempts` row was open to record it" in output, (
        "the two pointers diverged and the operator was told nothing"
    )


def test_resume_step4_reports_a_candidate_it_could_not_ask_git_about_instead_of_a_verdict(
    workspace: Path,
) -> None:
    """A question Git could not be asked is not an answer, and must not be collapsed into one.

    §11.5 step 4 says "there is no third branch", and that is a statement about Git's *verdict*:
    a commit is on the branch or it is not. It is not licence to invent a verdict when the
    question could not be put — here, a `RUNNING` task whose worktree is gone. Collapsing to the
    YES branch marks a task `DONE` with a SHA nobody found; collapsing to the NO branch calls
    `discard_task` on a tree that may hold the only copy of the work. The row is therefore left
    exactly as it was, and the operator is told why by name.
    """
    db = workspace / "state" / "fleet.db"
    _seed_running_task(db, task_anchor="a" * 40, phase_anchor="b" * 40)

    result = runner.invoke(app, [*base_args(workspace), "resume", "--no-continue"])
    assert result.exit_code == ExitCode.SUCCESS, result.output

    assert f"step 4: UNRESOLVED acme-commons phase 2 task {ARB_TASK}" in result.output
    assert "no worktree at" in result.output, "the reason was reduced to a bare name"
    assert _arbitration_state(db) == ("RUNNING", 4, None, 2, None), (
        "a candidate Git was never asked about was reconciled anyway"
    )


def test_resume_step4_corrects_a_fabricated_attempts_commit_sha_pointing_off_branch(
    workspace: Path,
) -> None:
    """SPEC §12 item 15's fabricated reverse disagreement — a CORRUPTED pointer, not an absent one.

    `test_resume_step4_reports_a_landed_commit_no_attempts_row_could_record` (~3288) covers the
    divergence where no `attempts` row exists to record the SHA at all. This is a different
    divergence: an `attempts` row exists and its `commit_sha` is hand-edited (raw SQL, never the
    normal write path — nothing in this harness produces this organically) to a SHA that is
    **real but not on `migrate/<repo>` at all** — divergent history, not merely "not yet landed"
    (the discard test's scenario, `3079`). §12 item 15's own words: "Fabricating the reverse
    disagreement … makes resume correct the column, never `git reset` the branch to match it,
    which is the mechanical form of the 'Git wins' invariant (§11.5)."

    **It was `xfail(strict=True)` and it is not any more (D87).** `_persist_arbitration`'s
    `attempts` `UPDATE` used to be a single statement whose subquery filtered `commit_sha IS
    NULL`, so a row already carrying a value — including this test's fabricated, off-branch one —
    matched zero rows and was silently left uncorrected, while `phases.post_commit_sha` was
    corrected in the same unit regardless. The fix selects the target `attempt_id` first, by
    `task_id` and the same `ORDER BY` alone, then updates that row unconditionally — the marker is
    deleted rather than left passing, so this is now a guard on the mechanism the fix landed.

    Two things must both hold, and the fixture is built so a defect in either one is
    distinguishable from the other:

    * **The verdict itself must be unaffected.** `_reconcile_tasks_with_git` never reads
      `attempts.commit_sha` to decide landed/discarded — it asks Git directly via
      `find_task_commit`, scoped to the phase anchor. So the fabricated pointer must not change
      what Git is asked, only what SQLite currently (wrongly) claims.
    * **The column, not the branch.** §11.5's authority rule is one-directional: SQLite is
      corrected to match Git, and "the harness never writes to Git to make it agree with a row."
      The DISCRIMINATING assertion is therefore on `migrate/<repo>`'s tip, not only on the SQLite
      row: a resume that `git reset --hard`-ed the branch onto the fabricated SHA would leave the
      row and the branch agreeing with each other and disagreeing with what Git actually recorded
      before the corruption — the exact inversion item 15 rules out.
    """
    db = workspace / "state" / "fleet.db"
    worktree, anchor = _arbitration_worktree(workspace)
    # A REAL commit, reachable in this repo, but never merged onto `migrate/acme-commons` —
    # divergent history the trailer search scoped to `<phase anchor>..migrate/acme-commons` will
    # never see.
    _git_in(worktree, "checkout", "-b", "rogue", anchor)
    (worktree / "rogue.java").write_text("class Rogue {}\n", encoding="utf-8")
    _git_in(worktree, "add", "-A")
    _git_in(worktree, "commit", "-m", "unrelated divergent history, never on migrate/acme-commons")
    rogue_sha = _git_out(worktree, "rev-parse", "HEAD")
    _git_in(worktree, "checkout", "migrate/acme-commons")

    sha = _land_task_commit(worktree)
    assert rogue_sha != sha, "the fixture's fabricated SHA coincides with the real landed one"
    _seed_running_task(db, task_anchor=anchor, phase_anchor=anchor)

    # The corruption: raw SQL, not `_land_task_commit` or any write path resume itself uses —
    # simulating bit-rot or a discrepancy nothing in this harness produced organically.
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        conn.execute(
            "UPDATE attempts SET commit_sha = ? WHERE attempt_id = ?", (rogue_sha, ARB_ATTEMPT)
        )
    finally:
        conn.close()

    result = runner.invoke(app, [*base_args(workspace), "--json", "resume", "--no-continue"])
    assert result.exit_code == ExitCode.SUCCESS, result.output

    report = json.loads(result.stdout)["git_arbitration"]
    assert report["candidates"] == 1
    assert [entry["commit_sha"] for entry in report["landed"]] == [sha], (
        "the fabricated SQLite pointer changed what Git was asked about, rather than being "
        "overruled by what Git actually shows"
    )
    assert report["discarded"] == [] and report["unresolved"] == []

    status, fence, attempt_sha, attempts, post = _arbitration_state(db)
    assert status == "DONE"
    assert (attempt_sha, post) == (sha, sha), (
        "the SQLite row kept the fabricated, off-branch SHA instead of being corrected to match "
        "Git — the reverse of §11.5's authority rule"
    )
    assert attempts == 2, "step 4 charged an attempt for work Git says already landed"
    assert fence == 4, "an adopted task's fence was bumped; nothing was handed back"

    # The negative half of the invariant: the BRANCH must not have been reset to agree with the
    # fabricated pointer. Both the tip and the file the true landing produced are asserted.
    assert _git_out(worktree, "rev-parse", "migrate/acme-commons") == sha, (
        "the branch was moved to reconcile with a fabricated SQLite pointer instead of the "
        "column being corrected to match the branch"
    )
    assert (worktree / "dest.java").exists(), "the real landed work was discarded"
    assert not (worktree / "rogue.java").exists(), (
        "the worktree was checked out onto the fabricated, off-branch commit"
    )

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


# --------------------------------------------------------------------------------------
# SPEC.md §12 item 10, `_transform_criterion`'s three named violation branches: an explicit
# `parse_probe` -> `False` verdict, an empty diff, and a write outside the wave's `dest_path`.
# --------------------------------------------------------------------------------------

#: Every probe RUNS and returns an explicit verdict — `False` for `bad.ts`, `True` for everything
#: else. Distinct from `_INDETERMINATE_ENGINE_MODULE` (ran, no verdict, raises) and from
#: `_UNAVAILABLE_ENGINE_MODULE` (never ran at all, raises before returning). Until this module,
#: no fake `parse_probe` in this suite ever returned `False` explicitly.
_PROBE_FALSE_ENGINE_MODULE = """\
from __future__ import annotations

CALLS: list[str] = []


class FakeRewriter:
    engine = "fake"

    async def apply(self, rule, path, source, params):
        return None

    async def parse_probe(self, path: str) -> bool:
        CALLS.append(path)
        return not path.endswith("bad.ts")


REWRITER = FakeRewriter()
"""


def _init_repo_with_one_commit(repo: Path) -> str:
    """A real one-commit git repo: `dest/good.ts` at HEAD. Returns HEAD's own sha, so a caller
    diffing `<this sha>..<branch>` gets a genuinely EMPTY range — §12 item 10's "non-empty diff"
    clause has to be driven by real git history producing zero entries, not a fake."""

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
    git("add", "-A")
    git("commit", "-m", "pre")
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), "rev-parse", "HEAD"],  # noqa: S607 - "git" from PATH
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _init_repo_with_write_outside_dest(repo: Path) -> str:
    """Like `_init_repo_with_two_commits`, but the second commit's changes include a file OUTSIDE
    `dest/` (`other/rogue.ts`) alongside the legitimate `dest/good.ts` edit. Returns the HEAD~1
    sha the caller diffs against."""

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
    git("add", "-A")
    git("commit", "-m", "pre")
    pre_sha = subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), "rev-parse", "HEAD"],  # noqa: S607 - "git" from PATH
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (repo / "dest" / "good.ts").write_text("export const good = 2;\n", encoding="utf-8")
    (repo / "other").mkdir()
    (repo / "other" / "rogue.ts").write_text("export const rogue = 1;\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-m", "rewrite, plus a write outside dest/")
    return pre_sha


def test_parse_probe_returning_false_is_a_genuine_violation_not_unprobed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SPEC.md §12 item 10: "parse probe 0 for every touched file" — a probe that RAN and
    explicitly said "this does not parse" (`False`, no raise) is the plain positive case the
    criterion names, distinct from `ProbeIndeterminateError` (ran, no verdict) and
    `EngineUnavailableError` (never ran at all) — both already covered above. Until this test,
    every fake `parse_probe` in this suite returned `True` or raised; none ever returned `False`,
    so cli.py:4736-4737 (`if not probed: violations.append(...)`) was unexercised.

    Discriminating mutation: folding the `if not probed:` branch into `unprobed.append(...)`
    instead of `violations.append(...)` (or deleting the branch outright) turns this red —
    `violations` comes back `[]` and `unprobed` gains the entry instead, which the old shape
    (asserting only "some list is non-empty") would not have caught but this test's explicit
    `unprobed == []` / `len(violations) == 1` pair does.
    """
    from fleet.cli import TransformOutput, _transform_criterion, _TransformEvidence, _TransformPlan
    from fleet.models.enums import RepoStatus
    from fleet.rewrite.rules import RewriteRule
    from fleet.settings import FleetSettings

    repo = tmp_path / "repo1"
    pre_sha = _init_repo_with_two_commits(repo)

    engine_dir = tmp_path / "engines"
    engine_dir.mkdir()
    (engine_dir / "fake_probe_false_engine.py").write_text(
        _PROBE_FALSE_ENGINE_MODULE, encoding="utf-8"
    )
    monkeypatch.syspath_prepend(str(engine_dir))
    monkeypatch.delitem(__import__("sys").modules, "fake_probe_false_engine", raising=False)

    config = write_config(
        tmp_path,
        fleet=FLEET_YAML
        + "transform:\n  rules_dir: config/rules\n  engines:\n    fake: "
        "fake_probe_false_engine\n",
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

    from fake_probe_false_engine import CALLS  # type: ignore[import-not-found]

    assert any(call.endswith("good.ts") for call in CALLS), (
        "good.ts must still be probed alongside the file whose probe explicitly failed"
    )
    assert any(call.endswith("bad.ts") for call in CALLS)
    assert len(violations) == 1, violations
    assert "the parse probe failed for" in violations[0]
    assert "dest/bad.ts" in violations[0]
    assert unprobed == [], (
        "an explicit `False` verdict is a genuine violation of §12 item 10's parse-probe "
        f"clause, not a non-blocking `unprobed` warning: {unprobed}"
    )


def test_empty_diff_is_rejected_as_a_violation(tmp_path: Path) -> None:
    """SPEC.md §12 item 10: "non-empty diff" — a repo whose phase reports SUCCEEDED but whose
    `git diff <pre_commit_sha>..<branch>` is empty must be a violation (cli.py:4678-4682), not
    silently accepted. Until this test, no existing test drove `_changed_entries` to return zero
    entries; every fixture repo in this module had real changes between the two commits it
    diffs.

    Discriminating mutation: deleting the `if not changed:` branch, or inverting it to
    `if changed:`, turns this red — `violations` comes back `[]` instead of carrying the
    EMPTY-diff message.
    """
    from fleet.cli import _transform_criterion, _TransformEvidence, _TransformPlan
    from fleet.models.enums import RepoStatus
    from fleet.settings import FleetSettings

    repo = tmp_path / "repo1"
    head_sha = _init_repo_with_one_commit(repo)

    config = write_config(tmp_path)
    settings = FleetSettings.load(config.parent)

    plan = _TransformPlan(
        repo_id="repo1",
        worktree=repo,
        branch="main",
        dest_path="dest",
        import_specifier="",
        pre_commit_sha=head_sha,
        base_ref="main",
        sources=(),
        targets=(),
    )
    evidence = _TransformEvidence()

    violations, unprobed = asyncio.run(
        _transform_criterion(
            settings,
            plans={"repo1": plan},
            evidence=evidence,
            statuses={"repo1": RepoStatus.SUCCEEDED},
            rules=[],
        )
    )

    assert len(violations) == 1, violations
    assert "is EMPTY" in violations[0], violations
    assert head_sha[:12] in violations[0], violations
    assert "repo1" in violations[0], violations
    assert unprobed == []


def test_a_write_outside_dest_path_is_rejected_as_a_violation(tmp_path: Path) -> None:
    """SPEC.md §12 item 10: "zero changed paths outside the repo's `dest_path`" — a patch that
    touches a file outside the wave's `dest_path` (here `other/rogue.ts`, alongside a legitimate
    `dest/good.ts` edit) must be reported (cli.py:4691-4697), even though the diff is otherwise
    non-empty. Until this test, no existing test planted a changed path outside `dest_path` at
    all — every fixture's changes in this module landed entirely under `dest/`.

    Discriminating mutation: deleting the `elif not post.startswith(f"{plan.dest_path}/"):`
    branch, or the `if outside:` violation append, turns this red — `violations` comes back
    `[]` instead of naming `other/rogue.ts`.
    """
    from fleet.cli import _transform_criterion, _TransformEvidence, _TransformPlan
    from fleet.models.enums import RepoStatus
    from fleet.settings import FleetSettings

    repo = tmp_path / "repo1"
    pre_sha = _init_repo_with_write_outside_dest(repo)

    config = write_config(tmp_path)
    settings = FleetSettings.load(config.parent)

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

    violations, unprobed = asyncio.run(
        _transform_criterion(
            settings,
            plans={"repo1": plan},
            evidence=evidence,
            statuses={"repo1": RepoStatus.SUCCEEDED},
            rules=[],
        )
    )

    assert len(violations) == 1, violations
    assert "outside dest/" in violations[0], violations
    assert "other/rogue.ts" in violations[0], violations
    assert unprobed == []


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


async def test_abandon_repo_redacts_a_credential_in_detail_before_writing_last_error(
    workspace: Path,
) -> None:
    """§12.20 — `_abandon_repo` is a `phases.last_error` write site with its OWN redaction call
    (`redact_text(detail)`, right above the `UPDATE phases` in cli.py) rather than relying on
    `SqliteStateRepository.complete_phase`'s. `detail` here is `str(exc)` from a git/OS failure
    that escaped `_prepare_repo` — exactly the shape that can quote a clone URL with embedded
    creds. Driven through the real write path — a real SQLite `phases` row, the real
    `_abandon_repo` coroutine, no mock of `redact_text` — so a regression that dropped the call
    would fail this test rather than only a unit test of `redact_text` itself.
    """
    repo_id = "acme-commons"
    db_path = workspace / "state" / "fleet.db"
    _seed_phase_row(db_path, repo_id)

    pat = "github_pat_11ABCDEFG0abcdefghijklmnopqrstuvwxyz0123456789ABCDEF"
    detail = f"fatal: could not read from remote https://oauth2:{pat}@gitea.local:3001/x.git"

    async with StateWriter(db_path, owner="test-redact-abandon") as writer:
        await _abandon_repo(writer, RUN_ID, repo_id, detail=detail, now=datetime.now(UTC))

    read_conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        last_error, finding_payload = read_conn.execute(
            "SELECT p.last_error, f.payload FROM phases p "
            "JOIN findings f ON f.run_id = p.run_id AND f.repo_id = p.repo_id "
            "WHERE p.run_id = ? AND p.repo_id = ? AND p.phase = ?",
            (RUN_ID, repo_id, int(Phase.TRANSFORM)),
        ).fetchone()
    finally:
        read_conn.close()

    for persisted in (last_error, finding_payload):
        assert pat not in persisted, f"a live PAT reached a persisted column: {persisted!r}"
        assert "github_pat_" not in persisted
        assert "«redacted:" in persisted, "the placeholder must survive, or debugging is blind"
    assert "gitea.local" in last_error, "the host must survive — over-redaction is a bug too"


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


# --------------------------------------------------------------------------------------
# §11.5 step 5 — the demotion to each repo's re-entry floor, wired into `_resume_impl`
# --------------------------------------------------------------------------------------

STEP5_REPO = "acme-commons"


def _step5_mirror(workspace: Path, repo_id: str = STEP5_REPO, *, suffix: str = "git") -> Path:
    """The bare mirror Phase 1's evidence looks for, at the path production writes it to.

    `cli._scan_payloads` builds `CloneInput.cache_dir` as `<root>/<run.cache_dir>/git` and
    `workers/clone.CloneWorker._mirror_path` appends `<slug(repo_id)>.git`. `suffix` exists so a
    test can put the mirror one directory ABOVE the real location — `<root>/<cache_dir>/<slug>.git`
    — which is what a caller that forgot the `/git` component would look at.
    """
    from fleet.sandbox.worktree import slug

    mirror = workspace / "cache" / suffix / f"{slug(repo_id)}.git"
    mirror.mkdir(parents=True)
    (mirror / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (mirror / "objects").mkdir()
    return mirror


def _step5_seed(
    db: Path,
    *,
    repo_id: str = STEP5_REPO,
    statuses: Mapping[int, str],
    post_commit_sha: Mapping[int, str | None] = MappingProxyType({}),
    attempts: int = 2,
) -> None:
    """`phases` rows for one repo, exactly as the columns step 5 reads them.

    Written as raw SQL rather than through the repository so the fixture can express states the
    repository's own transitions would refuse to produce — which is the whole point of a resume
    fixture: it reproduces what a crash left behind, not what a healthy run would write.
    """
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        for phase, status in statuses.items():
            conn.execute(
                "INSERT INTO phases (run_id, repo_id, phase, status, attempts, post_commit_sha, "
                "                    updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (RUN_ID, repo_id, phase, status, attempts,
                 post_commit_sha.get(phase), "2026-08-08T12:00:00+00:00"),
            )
    finally:
        conn.close()


def _step5_landed_fleet(workspace: Path) -> str:
    """A repo whose SCAN, TRANSFORM and BUILD all landed and whose VERIFY has not started.

    Returns the commit both `phases.post_commit_sha` pointers name. There is no `BUILD.bazel` on
    disk, so Phase 3's evidence fails and Phase 2's holds — which puts the floor at BUILD and
    makes the fixture distinguish "demote the frontier" from "demote everything".
    """
    _step5_mirror(workspace)
    worktree, anchor = _arbitration_worktree(workspace)
    _step5_seed(
        workspace / "state" / "fleet.db",
        statuses={1: "SUCCEEDED", 2: "SUCCEEDED", 3: "SUCCEEDED", 4: "PENDING"},
        post_commit_sha={2: anchor, 3: anchor},
    )
    assert not (worktree / "java" / STEP5_REPO / "BUILD.bazel").exists()
    return anchor


def _step5_rows(db: Path, repo_id: str = STEP5_REPO) -> dict[int, tuple[str, int]]:
    conn = sqlite3.connect(db)
    try:
        return {
            int(phase): (str(status), int(attempts))
            for phase, status, attempts in conn.execute(
                "SELECT phase, status, attempts FROM phases WHERE run_id = ? AND repo_id = ?",
                (RUN_ID, repo_id),
            )
        }
    finally:
        conn.close()


def _db_dump(db: Path) -> str:
    """Every row of every table, as SQL. Used instead of an enumeration of forbidden sinks.

    A `--dry-run` test that lists the tables it believes a write would touch is a blacklist, and
    the third escape defeats it (CLAUDE.md Rule 12). This asserts what the database MAY be after
    the command: exactly what it was. A write to a table nobody predicted trips it too.
    """
    conn = sqlite3.connect(db)
    try:
        return "\n".join(conn.iterdump())
    finally:
        conn.close()


def test_resume_step_5_demotes_the_span_above_the_floor_and_reports_what_it_discarded(
    workspace: Path,
) -> None:
    """§11.5 step 5's write, end to end through `fleet resume` — and the report beside it.

    The fixture is chosen so the floor is neither of the two answers a broken implementation
    lands on by accident. Phase 2's evidence HOLDS (the pointer resolves and is an ancestor of
    `migrate/<repo>`) and Phase 3's does NOT (no `<dest>/BUILD.bazel` on disk), so the floor is
    BUILD: not SCAN, which is what dropping the pointer read or mislocating the mirror produces,
    and not VERIFY, which is what an implementation that never walks backward produces.

    **`attempts` is asserted on the column, not inferred.** §11.5 step 5 demotes to re-run work
    whose evidence is gone; charging the repo an attempt for it would spend a rung of ADR-0014's
    ladder on a crash the repo did not cause, and three resumes would send a healthy repo to
    REQUIRES_HUMAN_INTERVENTION. `demote_to_floor` satisfies this by not naming the column, which
    is invisible from the outside — hence the assertion.

    **The finding is asserted because a silent demotion is the defect ADR-0077 §5 names.** A
    demotion made through `transition(..., resume=True)` would leave every status assertion in
    this test passing and emit nothing, so the run's audit trail would not record that landed
    work was discarded.
    """
    db = workspace / "state" / "fleet.db"
    _step5_landed_fleet(workspace)

    result = runner.invoke(app, [*base_args(workspace), "--json", "resume", "--no-continue"])
    assert result.exit_code == ExitCode.SUCCESS, result.output

    report = json.loads(result.stdout)["reentry_floors"]
    assert report["applied"] is True
    assert report["unresolved"] == [] and report["unchanged"] == []
    assert [entry["repo_id"] for entry in report["demoted"]] == [STEP5_REPO]
    entry = report["demoted"][0]
    assert entry["floor"] == "BUILD", (
        "the floor is not the phase ABOVE the highest holder below the frontier"
    )
    assert entry["phases"] == ["BUILD"], "the demoted span is not `phase >= floor and SUCCEEDED`"
    assert entry["evidence"] == {"BUILD": False, "TRANSFORM": True}, (
        "the walk consulted phases it should not have, or answered them wrongly"
    )

    rows = _step5_rows(db)
    assert rows[3][0] == "PENDING", "the phase above the floor kept its SUCCEEDED status"
    assert rows[2][0] == "SUCCEEDED", "a phase BELOW the floor was demoted"
    assert all(attempts == 2 for _status, attempts in rows.values()), (
        "step 5 charged an attempt; ADR-0014's ladder is spent on work the repo did not fail"
    )

    conn = sqlite3.connect(db)
    try:
        findings = conn.execute(
            "SELECT severity, payload FROM findings WHERE run_id = ? AND kind = 'PhaseDemoted'",
            (RUN_ID,),
        ).fetchall()
    finally:
        conn.close()
    assert len(findings) == 1, "the demotion is invisible to whoever reads this run"
    assert findings[0][0] == "warn"
    assert "BUILD" in json.loads(findings[0][1])["reason"] or "BUILD" in str(findings[0][1])


def test_resume_step_5_dry_run_previews_the_same_plan_and_leaves_the_database_byte_identical(
    workspace: Path,
) -> None:
    """`--dry-run` computes the plan the real run applies, and writes nothing at all.

    **The fixture reaches the guard.** `if dry_run or not plans` is only decided by `dry_run`
    when `plans` is non-empty, so a fixture with nothing to demote would exercise the `not plans`
    arm and prove nothing about `--dry-run` — which is exactly how step 4's first dry-run test
    passed while leaving half its claim untested. The plan asserted below is non-empty.

    **"Writes nothing" is asserted as a whitelist, not as a list of forbidden sinks.** The whole
    database is dumped before and after and compared. An enumeration of `phases`, `findings` and
    `checkpoints` is a blacklist that a fourth table defeats silently; this fails on any write to
    any table, predicted or not. `migration_state.json` — the one sink outside SQLite — is
    asserted separately, because step 7 is skipped under `--dry-run` for its own reason.

    **The round trip is what binds the two routes to one rule.** The preview and the write both
    call `orchestrator.reentry.demotable_phases`; running both against the same fixture and
    asserting the plans are EQUAL is what would go red if a future edit gave either route a
    membership rule of its own (defect D74's shape).
    """
    db = workspace / "state" / "fleet.db"
    _step5_landed_fleet(workspace)
    before = _db_dump(db)

    preview = runner.invoke(app, [*base_args(workspace), "--json", "resume", "--dry-run"])
    assert preview.exit_code == ExitCode.SUCCESS, preview.output
    planned = json.loads(preview.stdout)["reentry_floors"]

    assert planned["applied"] is False
    assert [entry["phases"] for entry in planned["demoted"]] == [["BUILD"]], (
        "the preview has nothing to plan, so this test cannot reach the guarded write"
    )
    assert _db_dump(db) == before, "--dry-run wrote to the database"
    assert not (workspace / "migration_state.json").exists()

    applied = runner.invoke(app, [*base_args(workspace), "--json", "resume", "--no-continue"])
    assert applied.exit_code == ExitCode.SUCCESS, applied.output
    written = json.loads(applied.stdout)["reentry_floors"]
    assert written["applied"] is True
    assert [
        (entry["repo_id"], entry["floor"], entry["phases"]) for entry in written["demoted"]
    ] == [(entry["repo_id"], entry["floor"], entry["phases"]) for entry in planned["demoted"]], (
        "the preview and the write disagree about what step 5 demotes"
    )


def test_resume_step_5_dry_run_says_it_would_demote_and_the_real_run_says_it_did(
    workspace: Path,
) -> None:
    """The two renderings differ in the VERB and in nothing else (`_arbitration_lines`' rule).

    A preview that printed "demoted acme-commons" would be a report of a state change that did
    not happen — the defect `raise_budget_applied` was added to the payload to kill one key over.
    """
    _step5_landed_fleet(workspace)
    preview = runner.invoke(app, [*base_args(workspace), "resume", "--dry-run"])
    assert preview.exit_code == ExitCode.SUCCESS, preview.output
    assert "step 5: would demote acme-commons to floor BUILD" in preview.output
    assert "step 5: demoted" not in preview.output

    applied = runner.invoke(app, [*base_args(workspace), "resume", "--no-continue"])
    assert applied.exit_code == ExitCode.SUCCESS, applied.output
    assert "step 5: demoted acme-commons to floor BUILD" in applied.output
    assert "would demote" not in applied.output


def test_resume_step_5_reads_the_post_commit_sha_pointer_and_not_only_the_status(
    workspace: Path,
) -> None:
    """The `phases.post_commit_sha` column is DECISIVE, so a projection that drops it fails here.

    `EvidenceRow.post_commit_sha` defaults to `None` and `state.repository.PhaseRow` does not
    carry the column at all, so `EvidenceRow(status=row.status)` type-checks, reviews clean, and
    silently answers `False` for Phases 2 and 3 across the entire fleet — demoting every repo
    further than its evidence warrants. No fixture inside `orchestrator/reentry.py` can express
    that: it is reachable only from the caller, which is `cli._demote_to_floors`.

    Two arms over one fixture, differing only in the column: with the pointer the floor is BUILD,
    and with it NULL the floor drops to TRANSFORM and one more phase of landed work is discarded.
    A caller that never read the column gives the second answer in both arms.
    """
    db = workspace / "state" / "fleet.db"
    _step5_landed_fleet(workspace)
    with_pointer = runner.invoke(app, [*base_args(workspace), "--json", "resume", "--dry-run"])
    assert with_pointer.exit_code == ExitCode.SUCCESS, with_pointer.output
    kept = json.loads(with_pointer.stdout)["reentry_floors"]["demoted"][0]

    conn = sqlite3.connect(db, isolation_level=None)
    try:
        conn.execute(
            "UPDATE phases SET post_commit_sha = NULL WHERE run_id = ? AND phase = 2", (RUN_ID,)
        )
    finally:
        conn.close()
    without = runner.invoke(app, [*base_args(workspace), "--json", "resume", "--dry-run"])
    assert without.exit_code == ExitCode.SUCCESS, without.output
    dropped = json.loads(without.stdout)["reentry_floors"]["demoted"][0]

    assert (kept["floor"], kept["phases"]) == ("BUILD", ["BUILD"])
    assert (dropped["floor"], dropped["phases"]) == ("TRANSFORM", ["TRANSFORM", "BUILD"]), (
        "clearing `phases.post_commit_sha` did not change the floor, so the caller is not "
        "projecting the column onto `EvidenceRow` at all"
    )
    assert kept["evidence"]["TRANSFORM"] is True and dropped["evidence"]["TRANSFORM"] is False


def test_resume_step_5_finds_the_mirror_under_the_configured_cache_dir_plus_git(
    workspace: Path,
) -> None:
    """The mirror this caller actually probes must resolve to `<root>/<cache_dir>/git/<slug>.git`.

    **Where the `git` segment comes from has moved once already, which is why this test asserts
    the RESOLVED path rather than either side of the boundary.** At `2f0db34` `cli` appended it
    and `RepoEvidence.for_repo` took a `git_cache_dir`; at `7b2d48e` a sibling inverted that —
    `for_repo` now takes an unsuffixed `cache_dir` and appends `reentry.MIRROR_CACHE_SUBDIR`
    itself — and left this caller passing the old suffixed value under the old keyword, which is
    how `main` came to be red. A test written against either signature would have gone green
    again on the rename while the fleet probed `cache/git/git/<slug>.git`.

    So: move the mirror, run the command, and read the floor. With the mirror at
    `cache/git/<slug>.git` the walk stops at SCAN and the floor is TRANSFORM; with the identical
    mirror one directory up at `cache/<slug>.git` — where a caller that double-appended or forgot
    the segment would look — SCAN fails and the floor drops to SCAN itself. The consequence that
    makes it worth a test is silent and fleet-wide: every repo's Phase-1 evidence fails and the
    whole fleet is demoted to SCAN on every resume.
    """
    db = workspace / "state" / "fleet.db"
    worktree, anchor = _arbitration_worktree(workspace)
    # Phase 2's pointer is a commit that is NOT on `migrate/<repo>`: the branch was force-reset
    # off it, which is exactly the state §11.5's authority table says the column must not be
    # trusted for. So Phase 2 fails, Phase 3 fails (no BUILD.bazel), and the walk reaches SCAN.
    _git_in(worktree, "checkout", "-B", "detached-work")
    (worktree / "extra.java").write_text("class C {}\n", encoding="utf-8")
    _git_in(worktree, "add", "-A")
    _git_in(worktree, "commit", "-m", "work off the migrate branch")
    off_branch = _git_out(worktree, "rev-parse", "HEAD")
    _git_in(worktree, "checkout", f"migrate/{STEP5_REPO}")
    _step5_seed(
        db,
        statuses={1: "SUCCEEDED", 2: "SUCCEEDED", 3: "SUCCEEDED", 4: "PENDING"},
        post_commit_sha={2: off_branch, 3: off_branch},
    )
    assert anchor != off_branch

    _step5_mirror(workspace, suffix="git")
    found = runner.invoke(app, [*base_args(workspace), "--json", "resume", "--dry-run"])
    assert found.exit_code == ExitCode.SUCCESS, found.output
    at_git = json.loads(found.stdout)["reentry_floors"]["demoted"][0]

    shutil.rmtree(workspace / "cache" / "git")
    _step5_mirror(workspace, suffix=".")
    missed = runner.invoke(app, [*base_args(workspace), "--json", "resume", "--dry-run"])
    assert missed.exit_code == ExitCode.SUCCESS, missed.output
    at_cache = json.loads(missed.stdout)["reentry_floors"]["demoted"][0]

    assert at_git["floor"] == "TRANSFORM", (
        "SCAN's evidence did not hold with the mirror at `<cache_dir>/git/<slug>.git`, so the "
        "caller is not deriving `git_cache_dir` the way `cli._scan_payloads` does"
    )
    assert at_git["evidence"]["SCAN"] is True
    assert at_cache["floor"] == "SCAN" and at_cache["evidence"]["SCAN"] is False, (
        "a mirror one directory ABOVE the real location satisfied Phase 1, so the `/git` "
        "component is not being applied and this test cannot see its omission"
    )


def test_resume_step_5_demotes_a_repo_whose_worktree_the_reaper_removed_all_the_way_to_scan(
    workspace: Path,
) -> None:
    """The reaped-worktree state step 5 exists to detect, driven through the real command.

    `reentry._worktree_present` guards `_build_evidence` and `_transform_evidence` before either
    touches Git, because `Git` shells out with the worktree as `cwd` and a `resolve()` against a
    directory that is not there raises `FileNotFoundError` rather than answering. Subtask 5 could
    not bind that guard: from inside `reentry.py` there is no way to distinguish "returned False"
    from "raised and something upstream swallowed it". From here there is — a raise reaches
    `_demote_to_floors`' `except (GitError, OSError)` and the repo lands in `unresolved` with a
    reason, instead of being demoted.

    So the assertion is BOTH halves: the repo is demoted to SCAN (its Git-backed evidence is all
    gone, which is the correct answer), and `unresolved` is empty (the guard answered rather than
    raised).
    """
    db = workspace / "state" / "fleet.db"
    _step5_mirror(workspace)
    worktree, anchor = _arbitration_worktree(workspace)
    _step5_seed(
        db,
        statuses={1: "SUCCEEDED", 2: "SUCCEEDED", 3: "SUCCEEDED", 4: "PENDING"},
        post_commit_sha={2: anchor, 3: anchor},
    )
    shutil.rmtree(worktree)

    result = runner.invoke(app, [*base_args(workspace), "--json", "resume", "--no-continue"])
    assert result.exit_code == ExitCode.SUCCESS, result.output
    report = json.loads(result.stdout)["reentry_floors"]

    assert report["unresolved"] == [], (
        "a missing worktree raised out of the evidence predicates instead of answering False"
    )
    entry = report["demoted"][0]
    assert entry["floor"] == "SCAN"
    assert entry["phases"] == ["SCAN", "TRANSFORM", "BUILD"]
    assert entry["evidence"] == {"SCAN": False, "TRANSFORM": False, "BUILD": False}
    assert [status for status, _attempts in _step5_rows(db).values()] == [
        "PENDING",
        "PENDING",
        "PENDING",
        "PENDING",
    ]


def test_resume_step_5_contains_a_git_failure_to_the_one_repo_it_happened_to(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A probe that raises lowers one repo out of the report; it does not abort the resume.

    `resume_floor` propagates every exception its probe raises, and `Git.resolve` /
    `Git.is_ancestor` raise `GitCommandError` on an unsettled probe (defect D42 — a deadline hit
    is not a verdict). Containment is the caller's job: an abort here would take down the whole
    command, so one repo's expired deadline would stop the other 249 being reconciled at all,
    which is the asymmetry `evidence_holds`' own docstring argues about one level down.

    The assertions are the containment AND its visibility. A `try/except: pass` would satisfy the
    first alone and hand the operator a fleet that reads as fully reconciled.
    """
    db = workspace / "state" / "fleet.db"
    anchor = _step5_landed_fleet(workspace)
    _step5_seed(
        db,
        repo_id="acme-billing",
        statuses={1: "SUCCEEDED", 2: "SUCCEEDED", 3: "SUCCEEDED", 4: "PENDING"},
        post_commit_sha={2: anchor, 3: anchor},
    )
    from fleet import cli as fleet_cli

    real = fleet_cli.evidence_holds

    async def exploding(phase: Phase, *, rows: object, repo: object, **kw: object) -> bool:
        if getattr(repo, "repo_id", "") == "acme-billing":
            # The D42 shape verbatim: `util.proc.run` reports a deadline hit as `timed_out`
            # with `started=True`, and `Git.is_ancestor` turns that into this exception rather
            # than into a verdict. Constructed with the real fields so the reason the operator
            # reads is the one production would print.
            raise GitCommandError(
                ["git", "merge-base", "--is-ancestor"],
                124,
                "deadline exceeded before `git merge-base` settled",
                timed_out=True,
                started=True,
            )
        return await real(phase, rows=rows, repo=repo, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr("fleet.cli.evidence_holds", exploding)

    result = runner.invoke(app, [*base_args(workspace), "--json", "resume", "--no-continue"])
    assert result.exit_code == ExitCode.SUCCESS, result.output
    report = json.loads(result.stdout)["reentry_floors"]

    assert [entry["repo_id"] for entry in report["demoted"]] == [STEP5_REPO], (
        "the repo whose probe failed took the healthy repo's reconciliation down with it"
    )
    assert [entry["repo_id"] for entry in report["unresolved"]] == ["acme-billing"]
    assert "deadline exceeded" in report["unresolved"][0]["reason"], (
        "the failure was contained but not reported, so the fleet reads as fully reconciled"
    )
    rendered = runner.invoke(app, [*base_args(workspace), "resume", "--dry-run"])
    assert "step 5: UNRESOLVED for acme-billing" in rendered.output, (
        "the failure was contained but the operator's own report does not say so"
    )
    assert _step5_rows(db, "acme-billing")[3][0] == "SUCCEEDED", (
        "a repo step 5 could not judge was demoted anyway"
    )


def test_resume_step_5_refuses_a_floor_whose_phase_rows_moved_under_it(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review finding I4: the floor is computed outside the transaction that applies it.

    The interleaving is not adversarial and needs no second process to be real. `_resume_impl`
    deliberately spares a non-stale `RUNNING` phase, so that phase is the unsettled frontier when
    `phase_floor` runs; `evidence_holds` then does Git I/O over the whole fleet, seconds to
    minutes; and in that window the live worker's `complete_phase` writes the row `SUCCEEDED`. The
    transaction re-reads, finds it `SUCCEEDED` **in-transaction**, and demotes work that landed
    while the resume was looking away — deleting the span's checkpoints and minting a warn
    finding whose reason describes evidence that never failed.

    Nothing that already existed closes it: `state/db.py`'s single-writer slot is process-wide
    module state, `BEGIN IMMEDIATE` carries no snapshot across from the `mode=ro` handle, and a
    lease guard is blind to precisely this case because `complete_phase` NULLs
    `lease_owner`/`lease_expires_at` in the same statement that sets the status.

    The fixture writes the row from inside the evidence probe, which is the same window the live
    worker writes in. The assertion is that NOTHING was written for that repo and that the
    operator is told — a silent `()` would report a healthy no-op for a race.
    """
    db = workspace / "state" / "fleet.db"
    _step5_landed_fleet(workspace)
    from fleet import cli as fleet_cli

    real = fleet_cli.evidence_holds

    async def racing(phase: Phase, *, rows: object, repo: object, **kw: object) -> bool:
        conn = sqlite3.connect(db, isolation_level=None)
        try:
            conn.execute(
                "UPDATE phases SET status = 'SUCCEEDED' WHERE run_id = ? AND phase = 4", (RUN_ID,)
            )
        finally:
            conn.close()
        return await real(phase, rows=rows, repo=repo, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr("fleet.cli.evidence_holds", racing)

    result = runner.invoke(app, [*base_args(workspace), "--json", "resume", "--no-continue"])
    assert result.exit_code == ExitCode.SUCCESS, result.output
    report = json.loads(result.stdout)["reentry_floors"]

    assert report["demoted"] == [] and report["applied"] is False
    assert [entry["repo_id"] for entry in report["unresolved"]] == [STEP5_REPO]
    assert "moved before this write" in report["unresolved"][0]["reason"]
    assert _step5_rows(db)[3][0] == "SUCCEEDED", (
        "the transaction applied a floor computed from rows that had already moved"
    )
    conn = sqlite3.connect(db)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM findings WHERE kind = 'PhaseDemoted'"
        ).fetchone()[0] == 0, "a PhaseDemoted finding was minted for a demotion that was refused"
    finally:
        conn.close()


def test_resume_step_5_re_opens_the_closed_wave_it_demotes_a_member_out_of(
    workspace: Path,
) -> None:
    """A PINNED CONSEQUENCE, not a desired behaviour. Recorded so subtask 8 inherits a measurement.

    `WaveState` is COMPUTED, never stored: `orchestrator.scheduler.Admission.wave_state` reads
    every `wave_members` row's `phases` status and answers `CLOSED` iff all of them are in
    `SETTLED_STATUSES`. No column holds it and `demote_to_floor` writes no `waves` or
    `wave_members` row. So `SUCCEEDED → PENDING` on a member of a closed wave re-opens that wave,
    by construction and with nothing to observe it.

    **This is neither authorised nor forbidden by the SPEC.** `docs/SPEC.md` §3.5's rule "closed
    waves are never re-opened" sits in the *Wave re-entry — un-blocking* paragraph and governs
    the `blocked_by` → `PENDING` path, whose remedy is a synthetic wave; §11.5 step 5 does not
    mention waves at all. The design assigns "no closed wave is re-opened" to subtask 8, which
    cannot honour it: this commit breaches it upstream of everything subtask 8 measures.

    No fix is attempted here and none is promised. `graph.sequence.append_synthetic_waves` is not
    the mechanism either — it filters to `ref not in plan.wave_index_by_node`, and every repo in
    this population already has a wave index. ADR-0089 §4 dates the disclosure.
    """
    from fleet.orchestrator.scheduler import SqliteSchedulerStore, WaveScheduler, WaveState
    from fleet.settings import FleetSettings
    from fleet.state.db import connect_ro
    from fleet.state.repository import SqliteStateRepository

    db = workspace / "state" / "fleet.db"
    _step5_landed_fleet(workspace)
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        conn.execute(
            # `max_usd` is non-zero because `_refuse_exhausted_wave` runs long before step 5
            # and a zero ceiling is an exhausted wave (§11.2) — the resume would exit 10 and this
            # test would never reach the demotion it exists to observe.
            "INSERT INTO waves (run_id, wave_index, computed_at, max_usd) VALUES (?, 0, ?, 8.0)",
            (RUN_ID, "2026-08-08T12:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO wave_members (run_id, wave_index, node_kind, node_id) "
            "VALUES (?, 0, 'REPO', ?)",
            (RUN_ID, STEP5_REPO),
        )
    finally:
        conn.close()

    async def _wave_state() -> WaveState:
        async with StateWriter(db, owner="w7-wave-state") as writer:
            read_conn = await connect_ro(db)
            try:
                return await WaveScheduler(
                    run_id=RUN_ID,
                    phase=Phase.BUILD,
                    store=SqliteSchedulerStore(writer=writer, read_conn=read_conn),
                    db=SqliteStateRepository(writer=writer, read_conn=read_conn),
                    budgets=FleetSettings.load(workspace / "config").config.budgets,
                    clock=lambda: datetime(2026, 8, 8, 12, tzinfo=UTC),
                ).wave_state(0)
            finally:
                await read_conn.close()

    wave_state = lambda: asyncio.run(_wave_state())  # noqa: E731

    assert wave_state() is WaveState.CLOSED, (
        "the fixture's wave is not closed to begin with, so this test cannot see the re-opening"
    )
    result = runner.invoke(app, [*base_args(workspace), "resume", "--no-continue"])
    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert _step5_rows(db)[3][0] == "PENDING"
    assert wave_state() is WaveState.OPEN, (
        "the measured consequence this test exists to pin has changed; ADR-0089 §4 records it "
        "as OPEN at `2f0db34`, and a change here needs a decision, not an edited constant"
    )


def test_resume_step_5_leaves_a_repo_at_its_floor_alone_and_says_which(
    workspace: Path,
) -> None:
    """`unchanged` and `demoted` are not degrees of one thing (D44).

    A run where every repo already sits at its floor writes nothing, and `applied` must be
    `False` on that path even without `--dry-run` — the payload reports what HAPPENED, not what
    was asked, which is the defect `raise_budget_applied` exists for one key over. The reason is
    printed per repo because "every phase settled" and "a phase needs a human" send the operator
    to opposite places.
    """
    db = workspace / "state" / "fleet.db"
    _step5_seed(db, statuses={1: "SUCCEEDED", 2: "SUCCEEDED", 3: "SUCCEEDED", 4: "SUCCEEDED"})
    _step5_seed(
        db,
        repo_id="acme-billing",
        statuses={1: "SUCCEEDED", 2: "REQUIRES_HUMAN_INTERVENTION", 3: "PENDING", 4: "PENDING"},
    )
    before = _db_dump(db)

    result = runner.invoke(app, [*base_args(workspace), "resume", "--no-continue"])
    assert result.exit_code == ExitCode.SUCCESS, result.output
    assert "step 5: acme-commons unchanged — every phase has already settled" in result.output
    assert (
        "step 5: acme-billing unchanged — a phase requires human intervention" in result.output
    )
    assert "step 5: demoted" not in result.output
    assert "would demote" not in result.output
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute("SELECT phase, status FROM phases WHERE repo_id = 'acme-billing'")
        assert dict(rows) == {1: "SUCCEEDED", 2: "REQUIRES_HUMAN_INTERVENTION", 3: "PENDING",
                              4: "PENDING"}, "§12 item 46 (ii): a sweep moved an RHI repo"
    finally:
        conn.close()
    assert _db_dump(db) == before, "a run with nothing to demote still wrote to the database"


# --------------------------------------------------------------------------------------
# The layer cycle `demotable_phases` opened — import order, not behaviour
# --------------------------------------------------------------------------------------


def test_state_repository_imports_first_in_a_fresh_interpreter(tmp_path: Path) -> None:
    """`import fleet.state.repository` must work when it is the FIRST `fleet` module imported.

    ADR-0089 §2 took an upward import — `state.repository` → `orchestrator.reentry` — as a
    deliberate cost, and recorded that `import fleet.state.repository` and `import fleet.cli`
    were each run clean before the decision was written. The second half was true; the first was
    not, and was not true at `2f0db34` either. `fleet.orchestrator.__init__` re-exports
    `context`, which imports `orchestrator.findings`, which imports `EventRow` back out of
    `state.repository` — so whichever of the two is imported first decides whether the cycle
    closes. `fleet.cli` reaches `orchestrator` first and is fine; anything reaching
    `state.repository` first got `ImportError: cannot import name 'EventRow' from partially
    initialized module`. `pytest tests/test_repository.py` was such a caller and had been failing
    at collection since `2f0db34` (W11, round D).

    This lives in `test_cli.py` and not in `test_repository.py` on purpose: restoring the
    module-scope import takes that whole module out at *collection*, so no assertion inside it
    can discriminate — every test there fails together, which proves nothing about any one of
    them. Every assertion in this module, by contrast, still passes under that mutation, because
    `from fleet.cli import ...` at the top imports `orchestrator` first. That is what makes this
    test the one that fails.

    A subprocess is the fixture, not an implementation detail: the property is about a cold
    interpreter's module table, and this session's is already populated by this module's own
    imports.
    """
    probe = subprocess.run(  # fixed argv built here, never a shell, no test input
        [sys.executable, "-c", "import fleet.state.repository as r; r.SqliteStateRepository"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert probe.returncode == 0, (
        "importing `fleet.state.repository` before any other `fleet` module failed — the "
        f"layer cycle is closed again:\n{probe.stderr}"
    )
    assert "partially initialized module" not in probe.stderr, probe.stderr

    reverse = subprocess.run(  # fixed argv built here, never a shell, no test input
        [sys.executable, "-c", "import fleet.cli; import fleet.state.repository"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert reverse.returncode == 0, (
        "the order that always worked stopped working, so the deferral broke something other "
        f"than the cycle:\n{reverse.stderr}"
    )
