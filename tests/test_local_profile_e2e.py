"""SPEC §12.41 piece 2: drives the FULL `scan → sequence → transform → build → verify → pr`
chain under `--profile local`, against `tests/fixtures/llm/stub_openai_server.py`'s real loopback
stub server (piece 1, task 21) — and is the task expected to CLOSE §12.41 ("a local-only profile
runs the whole pipeline").

**Controller ruling (inherited from task 21's brief, binding here too):** SPEC §12 item 41's
literal text names the full six-phase chain; a narrower "Phase 1-3" reading in
`docs/CRITERIA_PLAN.md`'s own paraphrase is superseded. This file drives all six phases.

**Why the pipeline never enables `repo_classify`.** `--skip-classify` and `--deterministic-only`
stay on for both runs below, exactly as every other e2e file in this suite uses them — the CHEAP
and WORKHORSE proof points this criterion needs do not require the Phase 1 classifier at all:
`Role.PR_TITLE` is CHEAP and `Role.PR_BODY` is WORKHORSE (`fleet.llm.roles.SPEC_ROLE_TIERS`), and
`workers/prwriter.py::_compose` calls both for every repo whose PR actually opens — which for this
fixture's two libraries (wave 0) happens on the very first `fleet pr`, no `--sync` required. That
is a smaller, faster fixture path than turning on classification or transform's LLM repair rung,
and it still exercises a real CHEAP→PROMPTED negotiation (proof point 1) and a real WORKHORSE call
at a rung above PROMPTED (this file's second, non-required check). No HEAVY-tier role fires on
this happy-path fixture (HEAVY roles are all repair/authoring/conflict paths this fixture never
triggers), so `default_profile`'s HEAVY target and `local`'s HEAVY→JSON_SCHEMA rung are never
dispatched here; WORKHORSE alone satisfies the brief's "one HEAVY-or-WORKHORSE call".

**Why two full workspaces, not one.** The brief's own guidance (task 22): "run the SAME small
fixture fleet once under the `default` profile ... then compare against a second run under
`--profile local`" is more honest than a hardcoded recorded value, since it proves equivalence
LIVE rather than against a stale snapshot that can drift from the shipped pipeline out from under
it. Both runs use the identical `FIXTURE_REPOS` fixture set and identical CLI flags (only
`--profile local` differs, plus the stub server + loopback guard wrapping the local run) — two
independent `tmp_path` subtrees, each with its own monorepo sibling directory
(`make_monorepo(workspace)` writes to `workspace.parent`, so the two workspaces need distinct
parents or their monorepos would collide).

**Why the `default` run is a legitimate baseline despite no `ANTHROPIC_API_KEY` ever being set.**
`RunContext.__post_init__` (`orchestrator/context.py`) always builds a real `LadderModelClient`
against the LIVE backend registry regardless of profile — there is no "LLM disabled" flag. Under
`default`, `write_pr_title`/`write_pr_body` (`workers/prwriter.py::_compose`) really are attempted
against the real `anthropic` backend, and its missing-API-key check raises before any socket is
touched (an `LlmError` subclass), which `_compose`'s `except LlmError` catches — so the baseline
run's `llm_cache` table for its (would-be) CHEAP/WORKHORSE rows stays exactly EMPTY, and the run
still exits 0 with code-rendered PR bodies (`workers/prwriter.py`'s own docstring: "a run with the
LLM disabled still opens an honest PR"). That emptiness is not a workaround — it is this file's
Rule 12 discriminator for proof points 1 and 3 (see `_assert_*` below): the exact same query,
pointed at the exact same fixture fleet under the wrong profile, finds nothing to assert on.

**Config: the workspace's `config/models.yaml` for the LOCAL run is the REAL, shipped
`config/models.yaml`**, read off disk and string-substituted only on the `local` profile's
placeholder `base_url` — the same edit that file's own comments describe an operator making
("`http://localhost:8000/v1` ... edit to match the server you run"). This is what backs the
mechanical "switching profiles needs zero `src/` edits" assertion below with the actual shipped
`capabilities_override` blocks, not a hand-rolled approximation of them.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import subprocess
import sys
from collections.abc import Mapping
from concurrent.futures import Executor, ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from typer.testing import CliRunner

from fleet import cli
from fleet.cli import ExitCode, app
from fleet.models.enums import RepoStatus
from fleet.orchestrator import budgets as budgets_module
from fleet.state.db import connect_ro
from fleet.state.projection import build_state
from tests.fixtures.llm.stub_openai_server import assert_loopback_only, running_stub_server
from tests.test_build_e2e import (
    FakeBazel,
    FakeFilterRepo,
    FakeGazelle,
    FakeResolver,
    build,
    make_monorepo,
    verify,
)
from tests.test_pr_e2e import FakeForge, run_id_of, run_pr
from tests.test_transform_e2e import (
    FIXTURE_REPOS,
    TS_IMPORT_RULE,
    _fresh_db,
    _make_repo,
    _write_config,
    _write_engine,
    base_args,
    query,
    scan,
    sequence,
    transform,
    write_rules,
)

runner = CliRunner()

#: `main` tip this task branched from (task 21 merged, piece 1). Paired with `_HEAD_SHA` below —
#: `test_switching_profiles_requires_zero_src_edits` diffs this FIXED historical range, scoped to
#: `src/`, and asserts it is empty, since this whole task is test-only infrastructure over shipped
#: code.
_BASE_SHA = "7b3235b30170940573b43967cbfc929f6f92db60"

#: `main` tip immediately after task 22 (this file, §12.41 piece 2) merged — the merge commit
#: `a701f48` (parents `_BASE_SHA` and the task's own single commit `1c65ae3`). Pinning the diff's
#: SECOND endpoint here, rather than leaving it implicit (defaulting to the working tree / current
#: `HEAD`), is the fix for a regression this task itself suffered: every unrelated commit landing
#: to `src/` on `main` AFTER task 22 merged used to make the open-ended diff non-empty, even though
#: task 22's own diff never touched `src/`. `_BASE_SHA..._HEAD_SHA` is a closed, immutable range —
#: it can never gain or lose content as `main` moves forward, because both endpoints already exist.
_HEAD_SHA = "a701f48f1b2f8e4ea62619b203eac04257a6cd1d"

_REPO_ROOT = Path(__file__).resolve().parents[1]

_REAL_MODELS_YAML_PATH = _REPO_ROOT / "config" / "models.yaml"

#: The literal `local` profile placeholder in the shipped `config/models.yaml` — every target's
#: `base_url`. Substituted for the stub server's real loopback URL; nothing else in the file
#: changes.
_PLACEHOLDER_BASE_URL = "http://localhost:8000/v1"


# -------------------------------------------------------------------------------------------
# workspace construction — mirrors `tests.test_transform_e2e`'s own `fleet` fixture body,
# called twice (once per profile) rather than once via fixture injection, since both runs must
# exist side by side in ONE test for a live comparison (see module docstring).
# -------------------------------------------------------------------------------------------


def _make_fleet_workspace(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    root.mkdir()
    sources = {
        repo_name: _make_repo(root / "sources", repo_name, files)
        for repo_name, files in FIXTURE_REPOS.items()
    }
    workspace = root / "workspace"
    workspace.mkdir()
    _write_config(workspace, sources, engine_module="fleet_fixture_engine")
    _write_engine(workspace, "fleet_fixture_engine", probe_available=True)
    _write_engine(workspace, "fleet_fixture_blind_engine", probe_available=False)
    write_rules(workspace, TS_IMPORT_RULE)
    _fresh_db(workspace / "state" / "fleet.db")
    return workspace


def _install_local_profile(workspace: Path, base_url: str) -> None:
    """Overwrites the workspace's `config/models.yaml` (written by `_write_config` above with the
    test suite's `default`-only `MODELS_YAML`) with the REAL, shipped `config/models.yaml` — both
    profiles, real `capabilities_override` blocks — patched only on the `local` profile's
    `base_url` placeholder."""
    real_text = _REAL_MODELS_YAML_PATH.read_text(encoding="utf-8")
    assert _PLACEHOLDER_BASE_URL in real_text, (
        "config/models.yaml's local-profile base_url placeholder moved — update this "
        "substitution to match"
    )
    patched = real_text.replace(_PLACEHOLDER_BASE_URL, base_url)
    (workspace / "config" / "models.yaml").write_text(patched, encoding="utf-8")


def _fake_cpu_pool(max_workers: int) -> Executor:
    """Stands in for `orchestrator.budgets.new_cpu_pool` for the duration of the loopback-guarded
    local-profile run below.

    `new_cpu_pool` builds a real `ProcessPoolExecutor` over Python's `forkserver` start method
    (`orchestrator/budgets.py`), whose control channel is a Unix-domain socket the executor
    `socket.connect()`s to on every fresh worker spawn. `assert_loopback_only`'s guard (task 21's
    fixture, reused UNMODIFIED here — CLAUDE.md's "do not touch its core mechanism") reads
    `address[0] if isinstance(address, tuple) else address`: a Unix path is a bare string, not a
    `(host, port)` tuple, so it falls through as `host` and is neither `127.0.0.1` nor anything
    else the guard was ever asked to recognise as loopback — it is a different KIND of connection
    than the guard's docstring is about (an LLM backend reaching only `127.0.0.1`), and blocking
    it is a false positive this fixture must route around rather than paper over by touching the
    guard itself. `Limits.cpu_pool` is typed as the stdlib `Executor` ABC, not concretely
    `ProcessPoolExecutor` (`orchestrator/budgets.py::Limits`), so a `ThreadPoolExecutor` is a
    same-interface stand-in: no subprocess, no forkserver, no Unix socket — and this fixture's
    few-kilobyte repos need no real process isolation to prove anything about `--profile local`.
    """
    return ThreadPoolExecutor(max_workers=max_workers)


def _install_fake_bazel_stack(monkeypatch: pytest.MonkeyPatch, workspace: Path) -> None:
    """The same four seams `tests.test_build_e2e`'s `bazel` fixture installs, built fresh per
    workspace since `FakeBazel` is constructed against ONE workspace's log directory."""
    monkeypatch.setattr(cli, "FILTER_REPO_RUNNER", FakeFilterRepo())
    monkeypatch.setattr(cli, "RESOLVER_RUNNER", FakeResolver())
    monkeypatch.setattr(cli, "GAZELLE_RUNNER", FakeGazelle())
    monkeypatch.setattr(cli, "BAZEL_RUNNER", FakeBazel(workspace / "artifacts" / "fake-bazel"))


def _succeeded_repos(root: Path) -> set[str]:
    """`RepoStatus.SUCCEEDED`, via the real `state/projection.py` (never the table directly) —
    the same discipline `tests.test_pr_e2e.cycles_through_the_projection` uses. Set once Phase 3
    (verify) passes; independent of whether Phase 4 opened, held, or never reached a PR for that
    repo, so `run_pr`'s held apps do not change this set."""

    async def read() -> set[str]:
        conn = await connect_ro(root / "state" / "fleet.db")
        try:
            state = await build_state(conn, UUID(run_id_of(root)))
        finally:
            await conn.close()
        return {
            repo_id
            for repo_id, repo_state in state.repos.items()
            if repo_state.status == RepoStatus.SUCCEEDED
        }

    return asyncio.run(read())


# -------------------------------------------------------------------------------------------
# the LOCAL-profile CLI-driver wrappers — `--profile` is a `main_callback` (global) option, so
# it must precede the subcommand token (Click/Typer parses left-to-right; a global option after
# the subcommand name is not recognised — `tests/test_cli.py:2012` shows the required order).
# The imported `scan`/`sequence`/`transform`/`build`/`verify`/`run_pr` helpers append `*extra`
# AFTER the subcommand, so they cannot express `--profile`; these mirror their exact bodies with
# one insertion rather than reimplementing the CLI-invocation pattern from scratch.
# -------------------------------------------------------------------------------------------


def _local_args(root: Path) -> list[str]:
    return [*base_args(root), "--profile", "local"]


def scan_local(root: Path, *extra: str) -> Any:
    return runner.invoke(
        app, [*_local_args(root), "scan", "--skip-classify", *extra], catch_exceptions=False
    )


def sequence_local(root: Path, *extra: str) -> Any:
    return runner.invoke(app, [*_local_args(root), "sequence", *extra], catch_exceptions=False)


def transform_local(root: Path, *extra: str, json_output: bool = True) -> Any:
    argv = [*_local_args(root)]
    if json_output:
        argv.append("--json")
    argv += ["transform", "--deterministic-only", *extra]
    return runner.invoke(app, argv, catch_exceptions=False)


def build_local(root: Path, *extra: str, json_output: bool = True) -> Any:
    argv = [*_local_args(root)]
    if json_output:
        argv.append("--json")
    argv += ["build", *extra]
    return runner.invoke(app, argv, catch_exceptions=False)


def verify_local(root: Path, *extra: str, json_output: bool = True) -> Any:
    argv = [*_local_args(root)]
    if json_output:
        argv.append("--json")
    argv += ["verify", *extra]
    return runner.invoke(app, argv, catch_exceptions=False)


def run_pr_local(root: Path, *extra: str) -> Any:
    return runner.invoke(app, [*_local_args(root), "--json", "pr", *extra], catch_exceptions=False)


# -------------------------------------------------------------------------------------------
# the stub responder — extends task 21's fixture with the two shapes THIS run's HEAVY-absent,
# WORKHORSE/CHEAP fixture actually needs, plus JSON_SCHEMA for completeness (never dispatched by
# this fixture, but `openai_compatible.build_payload`'s third branch — see its own file).
# -------------------------------------------------------------------------------------------


def _local_responder(request: Mapping[str, object]) -> Mapping[str, object]:
    """Answers whichever `structured_output_mode` `openai_compatible.build_payload` sent, read off
    the request shape it actually built (`tools` => TOOL_CALL, `response_format` => JSON_SCHEMA,
    neither => PROMPTED) — never off which role/tier is expected, so an unexpected negotiation
    result is answered correctly rather than masked."""
    served_model = request.get("model", "served-model")
    if "tools" in request:
        # WORKHORSE rung this fixture actually dispatches: `Role.PR_BODY`.
        arguments = json.dumps(
            {
                "body": "Migrated by the §12.41 local-profile fixture pipeline.",
                "highlights": [],
            }
        )
        return {
            "model": served_model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "emit_response", "arguments": arguments},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 33, "completion_tokens": 14},
        }
    if "response_format" in request:
        # HEAVY rung: never dispatched by this fixture (see module docstring), answered anyway
        # so the responder is honest about every mode the request builder can produce.
        content = json.dumps({"title": "chore: migrate into monorepo"})
        return {
            "model": served_model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 24, "completion_tokens": 8},
        }
    # PROMPTED floor: `Role.PR_TITLE`, this fixture's CHEAP call (proof point 1).
    content = json.dumps({"title": "chore: migrate into monorepo"})
    return {
        "model": served_model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 19, "completion_tokens": 6},
    }


# -------------------------------------------------------------------------------------------
# the three proof-point assertions, each its own function so Rule 12's discriminator (below) can
# run the SAME assertion against the wrong (default-profile) database and show it fail.
# -------------------------------------------------------------------------------------------


def _assert_cheap_rows_are_prompted(root: Path) -> None:
    """§12.41 proof point 1: `LlmCallRecord.structured_output_mode` (persisted to `llm_cache`,
    `models/tasks.py::LlmCallRecord`) is `PROMPTED` for every CHEAP-tier row this run wrote."""
    sql = "SELECT structured_output_mode FROM llm_cache WHERE tier = ?"
    modes = [row[0] for row in query(root, sql, ("CHEAP",))]
    assert modes, (
        "no CHEAP-tier llm_cache row exists — the negotiation this proof point needs never happened"
    )
    assert set(modes) == {"PROMPTED"}


def _assert_backend_is_openai_compatible(root: Path) -> None:
    """§12.41 proof point 3: `SELECT DISTINCT backend FROM llm_cache` == `{'openai_compatible'}`
    for this run's rows."""
    backends = {row[0] for row in query(root, "SELECT DISTINCT backend FROM llm_cache")}
    assert backends == {"openai_compatible"}


# -------------------------------------------------------------------------------------------
# 1. the mechanical assertion — switching profiles needed zero src/ edits
# -------------------------------------------------------------------------------------------


def test_switching_profiles_requires_zero_src_edits() -> None:
    """This whole task (§12.41 piece 2) is test-only infrastructure over already-shipped
    production code (`--profile local` already existed, `openai_compatible.py` already existed,
    `config/models.yaml`'s `local` profile already existed) — the `git diff --stat` over `src/`,
    scoped to the FIXED historical range `_BASE_SHA.._HEAD_SHA` this task's own commit landed in,
    must be empty.

    This is a claim about task 22's OWN diff, not a claim that `src/` never changes on `main`
    again — so the range's second endpoint is pinned to the merge commit that landed this task,
    not left to default to the working tree / current `HEAD`. An open-ended second endpoint would
    make this assertion fail on every later, unrelated `src/` commit forever after — this is
    exactly the regression round VI task 35 repaired (no §12 criterion moved; a full-suite health
    check surfaced this test failing on unrelated `main` churn, not a real product defect)."""
    result = subprocess.run(  # noqa: S603
        ["git", "diff", "--stat", _BASE_SHA, _HEAD_SHA, "--", "src/"],  # noqa: S607
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout == "", (
        "task 22 (SPEC 12.41 piece 2) was supposed to be test-only infra needing ZERO src/ "
        f"edits, but the diff {_BASE_SHA}..{_HEAD_SHA} (src/ only) is non-empty:\n{result.stdout}"
    )


# -------------------------------------------------------------------------------------------
# 2. the full six-phase run, both profiles, and the three proof points
# -------------------------------------------------------------------------------------------


def test_local_profile_runs_the_full_six_phase_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`scan → sequence → transform → build → verify → pr` under `--profile local`, through a
    real loopback stub server, matches a `default`-profile run on the same fixture fleet: same
    exit code, same `SUCCEEDED` repo set — while the LOCAL run additionally proves the three
    §12.41 proof points, each with a Rule 12 discriminator showing it is not vacuously true.
    """
    # Proof point 2: no ANTHROPIC_API_KEY for the whole run — asserted at the start, and again
    # after both runs complete, so "for the whole run" actually covers the whole test body.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert "ANTHROPIC_API_KEY" not in os.environ

    workspace_default = _make_fleet_workspace(tmp_path, "default-fleet")
    workspace_local = _make_fleet_workspace(tmp_path, "local-fleet")

    # Both workspaces' engine modules go on sys.path once, up front — content is identical
    # between the two (`_write_engine` always writes the same generated module), so whichever
    # is imported first is safely reused for the other.
    monkeypatch.syspath_prepend(str(workspace_default))
    monkeypatch.syspath_prepend(str(workspace_local))
    importlib.invalidate_caches()
    for module in ("fleet_fixture_engine", "fleet_fixture_blind_engine"):
        monkeypatch.delitem(sys.modules, module, raising=False)

    # ---------------------------------------------------------------------------------------
    # baseline: the SAME fixture fleet, under `default` (no --profile flag => models.yaml's
    # own default_profile). No stub server, no loopback guard needed: `write_pr_title`/
    # `write_pr_body` raise a caught `LlmError` before any socket is touched (module docstring).
    # ---------------------------------------------------------------------------------------
    monkeypatch.chdir(workspace_default)
    assert scan(workspace_default).exit_code == ExitCode.SUCCESS
    assert sequence(workspace_default).exit_code == ExitCode.SUCCESS
    assert transform(workspace_default).exit_code == ExitCode.SUCCESS
    make_monorepo(workspace_default)
    _install_fake_bazel_stack(monkeypatch, workspace_default)
    assert build(workspace_default, "--no-sandbox").exit_code == ExitCode.SUCCESS
    assert verify(workspace_default).exit_code == ExitCode.SUCCESS
    monkeypatch.setattr(cli, "GH_RUNNER", FakeForge())
    baseline_pr = run_pr(workspace_default)
    assert baseline_pr.exit_code == ExitCode.SUCCESS, baseline_pr.output
    baseline_succeeded = _succeeded_repos(workspace_default)

    # ---------------------------------------------------------------------------------------
    # the local-profile run — the whole six-phase chain inside the loopback guard (brief
    # requirement 6: not just the LLM-calling phases).
    # ---------------------------------------------------------------------------------------
    with running_stub_server() as server:
        server.set_responder(_local_responder)
        _install_local_profile(workspace_local, server.base_url)
        monkeypatch.chdir(workspace_local)
        # See `_fake_cpu_pool`'s own docstring: routes around a real, unrelated false positive
        # the guard would otherwise raise on `ProcessPoolExecutor`'s forkserver control socket.
        # `cli.py` does `from orchestrator.budgets import new_cpu_pool` (an early-bound name), so
        # both the defining module and `cli`'s own imported name must be patched.
        monkeypatch.setattr(budgets_module, "new_cpu_pool", _fake_cpu_pool)
        monkeypatch.setattr(cli, "new_cpu_pool", _fake_cpu_pool)
        with assert_loopback_only() as guard:
            assert scan_local(workspace_local).exit_code == ExitCode.SUCCESS
            assert sequence_local(workspace_local).exit_code == ExitCode.SUCCESS
            assert transform_local(workspace_local).exit_code == ExitCode.SUCCESS
            make_monorepo(workspace_local)
            _install_fake_bazel_stack(monkeypatch, workspace_local)
            assert build_local(workspace_local, "--no-sandbox").exit_code == ExitCode.SUCCESS
            assert verify_local(workspace_local).exit_code == ExitCode.SUCCESS
            monkeypatch.setattr(cli, "GH_RUNNER", FakeForge())
            local_pr = run_pr_local(workspace_local)
        # The CONTROL half of Rule 12 (stub_openai_server.py's own docstring): a legitimate
        # loopback-only run must not trip the guard.
        assert guard.blocked_attempts == []
        assert server.requests, "the stub server never answered a request — nothing was dispatched"

    assert local_pr.exit_code == ExitCode.SUCCESS, local_pr.output
    local_succeeded = _succeeded_repos(workspace_local)

    # -----------------------------------------------------------------------------------
    # the criterion itself: same exit code, same SUCCEEDED set as the default profile
    # -----------------------------------------------------------------------------------
    assert local_pr.exit_code == baseline_pr.exit_code == ExitCode.SUCCESS
    # `acme-empty` is `SKIPPED` (§3.1 step 7 — no manifest, nothing to migrate), never
    # `SUCCEEDED`, under EITHER profile — excluded from the expected set for that reason, not
    # hardcoded from one run's output.
    expected_succeeded = set(FIXTURE_REPOS) - {"acme-empty"}
    assert local_succeeded == baseline_succeeded == expected_succeeded

    # -----------------------------------------------------------------------------------
    # proof point 2 (again): still absent after both runs
    # -----------------------------------------------------------------------------------
    assert "ANTHROPIC_API_KEY" not in os.environ

    # -----------------------------------------------------------------------------------
    # proof points 1 and 3, on the LOCAL run
    # -----------------------------------------------------------------------------------
    _assert_cheap_rows_are_prompted(workspace_local)
    _assert_backend_is_openai_compatible(workspace_local)

    # A WORKHORSE row exists too (`Role.PR_BODY`) and negotiated ABOVE the PROMPTED floor —
    # not a required proof point, but it is what makes this responder's TOOL_CALL branch a real
    # discriminator rather than dead code: if `negotiate()` had instead handed WORKHORSE the
    # PROMPTED floor too, this would still pass (both branches answer with valid replies), but
    # the mode assertion below would catch a WORKHORSE call that silently never rose above CHEAP.
    workhorse_sql = "SELECT structured_output_mode FROM llm_cache WHERE tier = ?"
    workhorse_modes = {row[0] for row in query(workspace_local, workhorse_sql, ("WORKHORSE",))}
    assert workhorse_modes, "no WORKHORSE-tier llm_cache row exists — Role.PR_BODY never ran"
    assert workhorse_modes != {"PROMPTED"}, (
        f"WORKHORSE negotiated no higher than PROMPTED ({workhorse_modes}) — the local profile's "
        "capabilities_override for WORKHORSE promises TOOL_CALL"
    )

    # -----------------------------------------------------------------------------------
    # Rule 12: the SAME two assertion functions, pointed at the DEFAULT-profile baseline's own
    # database, must FAIL — proving neither reads as true no matter what it is pointed at. The
    # baseline's llm_cache is empty for CHEAP/WORKHORSE (see module docstring: the missing-key
    # `LlmError` is caught before any cache write), so both assertions fail on the "no rows"
    # branch — a genuine discriminator, not a coincidence of an unrelated fixture difference.
    # -----------------------------------------------------------------------------------
    with pytest.raises(AssertionError):
        _assert_cheap_rows_are_prompted(workspace_default)
    with pytest.raises(AssertionError):
        _assert_backend_is_openai_compatible(workspace_default)
