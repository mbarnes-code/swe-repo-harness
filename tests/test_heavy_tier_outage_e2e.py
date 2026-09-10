"""SPEC §12.43 case (iv) — round VI task 99 (research-51's "43-C"): a real, fixture-fleet,
CLI-driven HEAVY-tier outage.

research-51 traced the whole failure-halt chain end to end through EXISTING code (nothing here is
new mechanism, only the fixture that proves it): `openai_compatible`'s `APIConnectionError` →
`TransportError` → `client.py`'s `TierUnavailable` → `classify.py`'s `_error_for` →
`workers/base.py`'s `NON_RETRYABLE` → `orchestrator/retry.py`'s TERMINATE/PENDING → a
`BackendUnavailable` finding then `RunHalted` → exit 8. task 94 (43-A/43-B) closed cases (i) and
(ii)'s previously-zero-coverage sub-assertions; this file closes case (iv), the last residual
`docs/CRITERIA_PLAN.md` §43 named as "still fully open".

**The load-bearing fact this fixture rests on**: `src/fleet/llm/roles.py`'s `SPEC_ROLE_TIERS` is
"documentation and a default for tests, never authority — the active `config/models.yaml`
decides, which is what makes re-tiering a config edit." Routing `repo_classify` to `HEAVY` in this
file's own `config/models.yaml` (shipped default: `CHEAP`) is therefore a LEGAL operator
configuration, not a test-only monkeypatch — the same mechanism 4 existing e2e fixtures already
use to write their own `config/models.yaml` (`test_contracts_criterion_scale.py`,
`test_new_language_touchpoints_e2e.py`, `test_collisions_wiring.py`,
`test_hoist_rollback_wiring.py`). It is what makes `ClassifyWorker` a legitimate vehicle for a
HEAVY-tier outage, with no dependency on `workers/rewrite.py`'s separately-tracked `LlmError`
misclassification (round VI task 100, a different worktree; not touched here).

`ClassifyWorker` is skipped by MOST, not every, existing e2e fixture: 12 real
`runner.invoke(...)` scan sites across the suite pass `--skip-classify` (re-measured — a raw
`grep -n -- --skip-classify tests/*.py` returns 15, but 3 of those are prose mentions in a
docstring, not an actual CLI argument). `tests/test_cli.py` has TWO sites that drive
`ClassifyWorker` through the real CLI already — with the backend registry monkeypatched
(`monkeypatch.setattr(client_module, "registry", lambda: {"anthropic": _ScriptedClassify()})`),
not skipped. What is genuinely novel here (research-51's own flagged "one real unknown") is
driving `ClassifyWorker` through the real CLI against an UNMONKEYPATCHED backend registry, over
a real socket — no existing fixture had done that.

Two arms over the SAME config skeleton and the SAME two-repo fixture fleet — only the two HEAVY
targets' `base_url`s differ:

* **Arm 1 (control, and Rule 12's discriminator for arm 2):** both HEAVY targets point at a REAL,
  live loopback stub server (`tests/fixtures/llm/stub_openai_server.py`). `fleet scan` runs
  WITHOUT `--skip-classify` and completes — proving the fixture would otherwise succeed, so arm
  2's exit 8 is attributable to the outage and not to some unrelated defect in this fixture.
* **Arm 2 (case (iv)):** both HEAVY targets point at reserved, unbound loopback ports —
  `127.0.0.1:1` and `127.0.0.1:2` — refusing instantly and deterministically. research-51's
  explicit warning: never bind-then-close an ephemeral port to simulate this, which is a TOCTOU
  race (worse in this environment, where sibling round-VI lanes are live). A reserved low port
  refuses immediately; two DISTINCT ports give "every target tried" a real second entry rather
  than one port asked about twice.

**What this file does NOT close, disclosed rather than papered over**: the criterion's paired
`--deterministic-only` clause ("the same fixture under `fleet transform --deterministic-only`
completes its rule-resolvable repos with exit 0 and dispatches no LLM call at all") is proven
elsewhere (`tests/test_transform_e2e.py:1158`) but not against THIS file's own broken-HEAVY
fixture — research-51 treats it as a structural obstacle behind case (iv) specifically, not a
separate work item, and the task brief's own case-(iv) sub-assertion list (exit 8, a real
unreachable target, real `fleet resume` behavior, zero HEAVY calls landing, a valid checkpoint)
does not name it either. See `docs/CRITERIA_PLAN.md` §43 for the disclosed residual this leaves.
"""

from __future__ import annotations

import json
import socket
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from fleet.cli import ExitCode, app
from fleet.models.enums import RepoStatus
from fleet.models.state import MigrationState
from tests.fixtures.llm.stub_openai_server import StatusReply, running_stub_server
from tests.test_scan_e2e import FIXTURE_REPOS, FLEET_YAML, _fresh_db, _make_repo, query

runner = CliRunner()

#: Two real repos — enough for "repos" (plural, §12.43 case (iv)'s own wording) to be a
#: meaningful assertion rather than a vacuous one, small enough to keep the fixture fast. Reused
#: verbatim from `test_scan_e2e.FIXTURE_REPOS` rather than re-authored, per CLAUDE.md Rule 8.
_NAMES: tuple[str, ...] = ("acme-lib-py", "acme-app-py")

#: `fleet.llm.roles.SPEC_ROLE_TIERS`'s own shipped mapping, verbatim, EXCEPT `repo_classify`,
#: routed to `HEAVY` instead of the shipped `CHEAP` — the one config edit this whole fixture rests
#: on (see module docstring). `{heavy_a}`/`{heavy_b}` are the only two values that differ between
#: the control and outage arms below.
_MODELS_YAML_TEMPLATE = """\
version: 2
roles:
  conflict_resolution: HEAVY
  api_incompat_rewrite: HEAVY
  build_authoring: HEAVY
  cycle_break_proposal: HEAVY
  escalation: HEAVY
  transform_repair: WORKHORSE
  build_diagnosis: WORKHORSE
  manifest_extract: WORKHORSE
  pr_body: WORKHORSE
  repo_classify: HEAVY
  dep_disambiguate: CHEAP
  pr_title: CHEAP
default_profile: default
profiles:
  default:
    HEAVY:
      - {{ backend: openai_compatible, model_id: fixture-heavy-a, effort: high, price: free,
          base_url: '{heavy_a}',
          capabilities_override: {{ supports_json_schema: true, max_output_tokens: 2048,
                                   structured_output_modes: [JSON_SCHEMA, PROMPTED] }} }}
      - {{ backend: openai_compatible, model_id: fixture-heavy-b, effort: high, price: free,
          base_url: '{heavy_b}',
          capabilities_override: {{ supports_json_schema: true, max_output_tokens: 2048,
                                   structured_output_modes: [JSON_SCHEMA, PROMPTED] }} }}
    WORKHORSE:
      - {{ backend: anthropic, model_id: claude-sonnet-5, effort: high,
          api_key_env: ANTHROPIC_API_KEY,
          price: {{ in_per_mtok: 3.0, out_per_mtok: 15.0 }} }}
    CHEAP:
      - {{ backend: anthropic, model_id: claude-haiku-4-5,
          api_key_env: ANTHROPIC_API_KEY,
          price: {{ in_per_mtok: 1.0, out_per_mtok: 5.0 }} }}
"""
"""Neither WORKHORSE nor CHEAP is ever dispatched by `fleet scan` (§3.1 steps 1-4 have exactly one
LLM-bearing step, `classify` — `pr_body`/`pr_title` are Phase 4 roles) — both are left pointed at
the real `anthropic` backend, unreachable without `ANTHROPIC_API_KEY`, exactly as inert as they
are in every other e2e fixture in this suite that never sets that variable."""


# -------------------------------------------------------------------------------------------
# fixture fleet — two workspaces (control / outage), same repos, same config skeleton
# -------------------------------------------------------------------------------------------


def _write_config(
    root: Path, sources: Mapping[str, Path], *, names: Sequence[str], heavy_a: str, heavy_b: str
) -> None:
    config = root / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "fleet.yaml").write_text(FLEET_YAML, encoding="utf-8")
    (config / "models.yaml").write_text(
        _MODELS_YAML_TEMPLATE.format(heavy_a=heavy_a, heavy_b=heavy_b), encoding="utf-8"
    )
    entries = "".join(f"  - name: {name}\n    url: {sources[name]}\n" for name in names)
    (config / "repos.yaml").write_text(
        f"version: 1\ndefaults:\n  ref: main\nrepos:\n{entries}", encoding="utf-8"
    )


def _make_workspace(tmp_path: Path, label: str, *, heavy_a: str, heavy_b: str) -> Path:
    root = tmp_path / label
    root.mkdir()
    sources = {name: _make_repo(root / "sources", name, FIXTURE_REPOS[name]) for name in _NAMES}
    workspace = root / "workspace"
    workspace.mkdir()
    _write_config(workspace, sources, names=_NAMES, heavy_a=heavy_a, heavy_b=heavy_b)
    _fresh_db(workspace / "state" / "fleet.db")
    return workspace


def _make_workspace_custom(
    tmp_path: Path,
    label: str,
    *,
    models_yaml: str,
    fleet_yaml: str = FLEET_YAML,
) -> Path:
    """Like `_make_workspace` above, but for a case (round VI task 102's cases (ii)/(iii)) whose
    config needs — a non-default `llm.failover.*` override, or per-target capabilities the
    shared `_MODELS_YAML_TEMPLATE` cannot express (case (iii)'s deliberately-drifting target) —
    go beyond what `_write_config` covers. SAME two-repo fixture fleet; only the config CONTENT
    differs."""
    root = tmp_path / label
    root.mkdir()
    sources = {name: _make_repo(root / "sources", name, FIXTURE_REPOS[name]) for name in _NAMES}
    workspace = root / "workspace"
    workspace.mkdir()
    config = workspace / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "fleet.yaml").write_text(fleet_yaml, encoding="utf-8")
    (config / "models.yaml").write_text(models_yaml, encoding="utf-8")
    entries = "".join(f"  - name: {name}\n    url: {sources[name]}\n" for name in _NAMES)
    (config / "repos.yaml").write_text(
        f"version: 1\ndefaults:\n  ref: main\nrepos:\n{entries}", encoding="utf-8"
    )
    _fresh_db(workspace / "state" / "fleet.db")
    return workspace


def _args(root: Path) -> list[str]:
    return ["--config", str(root / "config" / "fleet.yaml"), "--db", str(root / "state/fleet.db")]


def scan_real_classify(root: Path, *, json_output: bool = False) -> Any:
    """`fleet scan` with NO `--skip-classify` — the flag 12 of the suite's real `runner.invoke`
    scan sites pass (re-measured; see the module docstring). This is deliberately the ONLY
    difference from the shared `scan()` helpers elsewhere in this test suite."""
    argv = [*_args(root)]
    if json_output:
        argv.append("--json")
    argv.append("scan")
    return runner.invoke(app, argv, catch_exceptions=False)


def resume_cmd(root: Path) -> Any:
    return runner.invoke(app, [*_args(root), "--json", "resume"], catch_exceptions=False)


# -------------------------------------------------------------------------------------------
# the stub responder (arm 1 / control only) — answers RepoClassification's JSON_SCHEMA rung
# -------------------------------------------------------------------------------------------


def _classify_responder(request: Mapping[str, object]) -> Mapping[str, object]:
    """Answers `llm.schemas.RepoClassification`'s JSON_SCHEMA rung (`negotiate()` picks it: both
    HEAVY targets declare `supports_json_schema: true`) — the only shape this fixture's classify
    calls actually negotiate, so there is no branch on request shape the way
    `test_local_profile_e2e.py`'s multi-role responder needs."""
    content = json.dumps(
        {
            "ecosystem": "pypi",
            "is_library": True,
            "confidence": 0.91,
            "rationale": "manifest evidence (pyproject.toml) indicates a Python library.",
        }
    )
    return {
        "model": request.get("model", "fixture-heavy"),
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 30, "completion_tokens": 14},
    }


# -------------------------------------------------------------------------------------------
# Arm 1 — control: the fixture would otherwise succeed
# -------------------------------------------------------------------------------------------


def test_heavy_tier_control_arm_classify_dispatches_through_the_real_stub_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule 12's discriminator for the outage test below: SAME config skeleton, SAME two-repo
    fleet, HEAVY's two targets pointed at a REAL live server instead of two dead ports. `fleet
    scan` — without `--skip-classify` — completes, and every `repo_classify` `llm_cache` row was
    negotiated for real at tier HEAVY through `openai_compatible`.

    **Not the first test to drive `ClassifyWorker` through the real CLI — corrected from an
    overclaim in an earlier draft.** `tests/test_cli.py`'s
    `test_status_digest_is_byte_identical_across_two_clean_db_runs_under_a_warm_llm_cache` (and a
    sibling scan a few hundred lines later) already invoke `fleet scan` with no `--skip-classify`
    and reach `ClassifyWorker`, via `monkeypatch.setattr(client_module, "registry", lambda: {...})`
    — a substituted backend registry, not a real socket. The genuinely novel property here,
    stated precisely: the first test to drive `ClassifyWorker` through the real CLI against an
    UNMONKEYPATCHED backend registry, over a real socket (research-51's own "one real unknown"
    flag — no existing fixture had done that)."""
    with running_stub_server() as server:
        server.set_responder(_classify_responder)
        workspace = _make_workspace(
            tmp_path, "control", heavy_a=server.base_url, heavy_b=server.base_url
        )
        monkeypatch.chdir(workspace)

        result = scan_real_classify(workspace, json_output=True)
        assert result.exit_code == ExitCode.SUCCESS, result.output
        payload = json.loads(result.stdout)
        assert payload["succeeded"] == len(_NAMES), payload
        assert payload["failed"] == 0

        assert server.requests, (
            "the stub server never answered a request — classify never actually dispatched, "
            "which would make this control worthless"
        )

    rows = query(
        workspace,
        "SELECT tier, backend, structured_output_mode FROM llm_cache WHERE role = ?",
        ("repo_classify",),
    )
    assert len(rows) == len(_NAMES), rows
    for tier, backend, mode in rows:
        assert tier == "HEAVY"
        assert backend == "openai_compatible"
        assert mode == "JSON_SCHEMA"


# -------------------------------------------------------------------------------------------
# Arm 2 — case (iv): every target for HEAVY unreachable
# -------------------------------------------------------------------------------------------

_HEAVY_A = "http://127.0.0.1:1/v1"
_HEAVY_B = "http://127.0.0.1:2/v1"


def _assert_port_genuinely_refuses(host: str, port: int) -> None:
    """A control on the fixture's own premise (Rule 12): before trusting the CLI's exit code,
    confirm at the raw socket layer — independent of any harness code — that nothing is
    listening. If this ever failed, exit 8 below would prove nothing about a HEAVY-tier outage;
    it would just be measuring whatever DID answer."""
    with pytest.raises(ConnectionRefusedError), socket.create_connection((host, port), timeout=2):
        pass


def test_heavy_tier_outage_halts_the_run_and_resume_finds_the_repos_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§12.43 case (iv), through the real CLI on a real fixture fleet: "With every target for
    `HEAVY` unreachable, the run exits 8 with a `BackendUnavailable` finding naming the tier and
    each target tried, zero repos in `REQUIRES_HUMAN_INTERVENTION`, zero `HEAVY` calls served by
    another tier (asserted over `llm_cache.tier`), a valid checkpoint, and a `fleet resume` that
    finds those repos still `PENDING`."
    """
    _assert_port_genuinely_refuses("127.0.0.1", 1)
    _assert_port_genuinely_refuses("127.0.0.1", 2)

    workspace = _make_workspace(tmp_path, "outage", heavy_a=_HEAVY_A, heavy_b=_HEAVY_B)
    monkeypatch.chdir(workspace)

    # -----------------------------------------------------------------------------------
    # sub-assertion 1: the real exit-8 process status, from a real CLI invocation (no --json,
    # matching this suite's own precedent for a non-zero-exit assertion — `test_scan_e2e.py`'s
    # `test_a_degraded_repo_with_no_rhi_repo_exits_7` reads state back via SQL rather than
    # parsing stdout past a `_fail()` error line).
    # -----------------------------------------------------------------------------------
    result = scan_real_classify(workspace)
    assert result.exit_code == ExitCode.TIER_UNAVAILABLE == 8, result.output

    # -----------------------------------------------------------------------------------
    # sub-assertion 2: a BackendUnavailable finding naming the tier and EACH target tried — a
    # real unreachable target (the control above and the raw-socket check confirm this is not a
    # synthesized WorkerError), not a hand-built stand-in.
    # -----------------------------------------------------------------------------------
    finding_rows = query(
        workspace, "SELECT payload FROM findings WHERE kind = 'BackendUnavailable'"
    )
    assert finding_rows, "no BackendUnavailable finding was written"
    for (raw,) in finding_rows:
        finding_payload = json.loads(raw)
        observed = finding_payload["observed"]
        assert "HEAVY" in observed, observed
        assert "fixture-heavy-a" in observed, observed
        assert "fixture-heavy-b" in observed, observed
        assert finding_payload["failover_triggers_scope"] == "tier"

    # -----------------------------------------------------------------------------------
    # sub-assertion 3: zero repos in REQUIRES_HUMAN_INTERVENTION. §11.8's own comment on the
    # BACKEND_UNAVAILABLE/TERMINATE branch (`orchestrator/retry.py`) is literal: "the lease is
    # left to expire and the row stays for `fleet resume`, so no attempt is charged to a repo
    # that did nothing wrong" — no write to PENDING happens at halt time, on purpose, so the row
    # is still RUNNING here (its lease has not yet gone stale by real wall-clock time). What
    # matters for THIS sub-assertion is what it is NOT: neither repo advanced to
    # REQUIRES_HUMAN_INTERVENTION or SUCCEEDED. The "still PENDING" half of the SPEC sentence is
    # what `fleet resume` (sub-assertion 6, below) is actually about.
    # -----------------------------------------------------------------------------------
    phase_rows = dict(query(workspace, "SELECT repo_id, status FROM phases WHERE phase = 1"))
    assert set(phase_rows) == set(_NAMES), phase_rows
    assert "REQUIRES_HUMAN_INTERVENTION" not in phase_rows.values(), phase_rows
    assert "SUCCEEDED" not in phase_rows.values(), phase_rows

    # -----------------------------------------------------------------------------------
    # sub-assertion 4: zero HEAVY calls served by another tier (asserted over llm_cache.tier) —
    # AND, the brief's stronger reading, zero HEAVY-tier calls landed AT ALL (not just zero
    # successful ones). `llm_cache` only ever holds a SUCCESSFULLY negotiated call, so an empty
    # table for this role does not by itself prove "nothing was attempted" — that half is proven
    # BY CONSTRUCTION: `_assert_port_genuinely_refuses` above already showed nothing listens on
    # either port, so no TCP handshake — and therefore no HTTP round trip — could ever complete
    # against either target, regardless of how many times the ladder tried.
    # -----------------------------------------------------------------------------------
    classify_rows = query(
        workspace, "SELECT tier FROM llm_cache WHERE role = ?", ("repo_classify",)
    )
    assert classify_rows == [], (
        f"a repo_classify call was served from SOME tier — {classify_rows} — but every HEAVY "
        "target is unreachable and no other tier may ever silently answer it (ADR-0023)"
    )
    downgraded = query(
        workspace,
        "SELECT tier FROM llm_cache WHERE role = ? AND tier != 'HEAVY'",
        ("repo_classify",),
    )
    assert downgraded == [], downgraded

    # -----------------------------------------------------------------------------------
    # sub-assertion 5: a valid checkpoint. "Any non-zero exit leaves a valid checkpoint and a
    # regenerated migration_state.json" (docs/SPEC.md §10) — read back through the REAL Pydantic
    # model, not `json.loads`, so a malformed projection would fail this assertion rather than
    # silently pass a loose dict-shape check.
    # -----------------------------------------------------------------------------------
    state_path = workspace / "migration_state.json"
    assert state_path.exists(), "no migration_state.json was written after the halt"
    state = MigrationState.model_validate_json(state_path.read_text(encoding="utf-8"))
    assert state.needs_human == []
    for name in _NAMES:
        assert state.repos[name].status is not RepoStatus.REQUIRES_HUMAN_INTERVENTION, (
            name,
            state.repos[name],
        )
        assert state.repos[name].status is not RepoStatus.SUCCEEDED, (name, state.repos[name])

    # -----------------------------------------------------------------------------------
    # sub-assertion 6: a real `fleet resume` CLI invocation that finds those repos still
    # PENDING — not a direct `reap_expired_phase_leases`/`_demote_to_floors` call.
    #
    # §11.5 step 3's reclaim (`cli.py::_reset_stale_running`) is gated on a REAL wall-clock
    # staleness: `phases.heartbeat_ttl_seconds` carries the schema's hardcoded `DEFAULT 300`
    # (`claim_phase` never stamps a config-derived value there — a disclosed, separate defect;
    # see `cli.py`'s own `_STALE_HEARTBEAT_PREDICATE` comment), so a resume run milliseconds
    # after the halt genuinely finds the lease still live and reclaims nothing — confirmed by
    # direct measurement (`stale_running_reset: 0` against an unmodified `heartbeat_at`, before
    # this line was added). Backdating `heartbeat_at` here isolates the SAME mechanism this
    # suite already isolates one call at a time — `test_scan_e2e.py`'s own
    # `test_a_degraded_repo_with_no_rhi_repo_exits_7` writes a `phases` row directly "to isolate
    # the CLI's exit-code determination from how a repo comes to be DEGRADED" — from the real
    # 300+ second wait a live rerun would otherwise need to actually observe it. The HALT itself,
    # the finding, the checkpoint and every assertion above are entirely real; only the *wall
    # clock* between the halt and the moment `fleet resume` is entitled to reclaim the lease is
    # simulated.
    # -----------------------------------------------------------------------------------
    backdated = (datetime.now(UTC) - timedelta(seconds=400)).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")
    conn = sqlite3.connect(workspace / "state" / "fleet.db")
    try:
        conn.execute("UPDATE phases SET heartbeat_at = ? WHERE phase = 1", (backdated,))
        conn.commit()
    finally:
        conn.close()

    resume_result = resume_cmd(workspace)
    assert resume_result.exit_code == ExitCode.SUCCESS, resume_result.output
    resume_payload = json.loads(resume_result.stdout)
    assert resume_payload["stale_running_reset"] == len(_NAMES), resume_payload
    continuation = resume_payload["continuation"]
    assert continuation["driven"] == [], continuation
    assert sorted(continuation["scan_floor_not_continued"]) == sorted(_NAMES), continuation

    after_resume = dict(query(workspace, "SELECT repo_id, status FROM phases WHERE phase = 1"))
    assert set(after_resume.values()) == {"PENDING"}, after_resume


# =============================================================================================
# Round VI task 102 — §12.43's last two residuals (TEST-ONLY):
#   1. cases (i)-(iii)'s "on the fixture fleet" framing clause, driven through the real CLI on
#      the SAME two-repo fixture fleet/config skeleton above (only the two HEAVY targets' shapes
#      differ per case);
#   2. case (iv)'s own paired `--deterministic-only` clause, re-run against THIS file's broken-
#      HEAVY config (arm 2's unreachable ports).
#
# docs/CRITERIA_PLAN.md's §43 entry names these as the sole remaining work before the criterion
# counts toward `<n> of 48`. research-51 judged extending cases (i)-(iii) onto the fixture fleet
# "nearly free" once the arm-1/arm-2 skeleton exists; this section is that extension.
# =============================================================================================


# ---------------------------------------------------------------------------------------------
# Case (i): CONNECTION failover — a HEAVY target that refuses connections, the run completes on
# its second target.
# ---------------------------------------------------------------------------------------------


def test_case_i_connection_failover_on_the_fixture_fleet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§12.43 case (i), on the SAME two-repo fixture fleet arm 1/arm 2 already build: `HEAVY`'s
    first target (`fixture-heavy-a`) is the identical unbound loopback port arm 2 uses
    (`127.0.0.1:1` — refuses instantly, never a TOCTOU-prone bind-then-close), its second target
    (`fixture-heavy-b`) the real stub server arm 1 uses. `fleet scan` (real classify, no
    `--skip-classify`) completes for BOTH repos, each independently exhibiting the SAME single
    induced CONNECTION failover the unit-level tests already proved
    (`tests/test_runner.py::test_an_induced_connection_failover_leaves_phases_attempts_and_
    transient_retries_unchanged`) — but now read back from real SQLite state after a real CLI
    invocation, over a real socket, against an unmonkeypatched backend registry, not a
    directly-constructed `LadderModelClient`/`FailoverBackend`.

    `open_after_failures` is left at its shipped default (3): with only 2 repos and a target
    that NEVER recovers, the breaker never has a reason to open here — that is case (ii)'s
    scenario, not this one. One CONNECTION failure per repo is exactly what the unit test calls
    "a single induced failover", not a breaker trip.
    """
    with running_stub_server() as server:
        server.set_responder(_classify_responder)
        workspace = _make_workspace(tmp_path, "case_i", heavy_a=_HEAVY_A, heavy_b=server.base_url)
        monkeypatch.chdir(workspace)

        result = scan_real_classify(workspace)
        assert result.exit_code == ExitCode.SUCCESS, result.output

        # sub-assertion 1: one backend_failover event per repo, trigger CONNECTION, naming both
        # targets — the real §11.8 event, not a hand-built `BackendFailover`.
        failover_rows = query(
            workspace, "SELECT payload FROM events WHERE event = 'backend_failover'"
        )
        assert len(failover_rows) == len(_NAMES), failover_rows
        for (raw,) in failover_rows:
            payload = json.loads(raw)
            assert payload["role"] == "repo_classify"
            assert payload["trigger"] == "CONNECTION"
            assert payload["from_model_id"] == "fixture-heavy-a"
            assert payload["to_model_id"] == "fixture-heavy-b"

        # sub-assertion 2: the resulting llm_cache row carries the SECOND target's attribution.
        cache_rows = query(
            workspace,
            "SELECT backend, model_id FROM llm_cache WHERE role = ?",
            ("repo_classify",),
        )
        assert len(cache_rows) == len(_NAMES), cache_rows
        for backend, model_id in cache_rows:
            assert backend == "openai_compatible"
            assert model_id == "fixture-heavy-b"

        # sub-assertion 3: phases.attempts / transient_retries UNCHANGED by the failover — the
        # concrete baseline a clean run (arm 1's control test, above) also shows: one successful
        # attempt, zero transient retries. The failover cost a backend hop, not a phase rung.
        phase_rows = query(
            workspace, "SELECT attempts, transient_retries FROM phases WHERE phase = 1"
        )
        assert phase_rows == [(1, 0)] * len(_NAMES), phase_rows


# ---------------------------------------------------------------------------------------------
# Case (ii): a target whose whole §11.8 backoff schedule is exhausted by 429s opens the breaker
# and the run fails over to the second target; a later probe past cooldown restores it.
# ---------------------------------------------------------------------------------------------
#
# **Corrected (round VI task 102 fix round) — the recovery half IS closable on the real fixture
# fleet, and the note this replaces was wrong about why not.** That note claimed no config knob
# in `src/` could serialize HEAVY-tier dispatch, based on a literal grep for `llm_concurrency(` —
# a method that is real but genuinely unused (`settings.py:1305`'s own declaration is the only
# hit). Review traced the ACTUAL data flow, which does not go through that method at all:
# `settings.py`'s `LlmConcurrency.for_tier()` sizes a `ResizableLimiter` per tier in
# `orchestrator/budgets.py::Limits.create()`, and `workers/classify.py:162` wraps `ClassifyWorker`'s
# entire `ctx.llm.complete(...)` call in `async with ctx.limits.for_tier(tier):` — exactly the
# worker both tests below drive. Setting `concurrency.llm.heavy: 1` (a legal, already-declared
# `fleet.yaml` section — see `FLEET_YAML`'s own `concurrency:` block) genuinely serializes the two
# repos' classify calls: measured directly, `fixture-heavy-a`'s raw request timestamps show one
# repo's full retry-and-failover sequence (15 raw HTTP attempts, ~13s) complete before the second
# repo's first request ever arrives — no interleaving, confirmed by inspecting arrival order, not
# assumed. `test_case_ii_backend_health_breaker_opens_on_the_fixture_fleet` above still proves the
# OPEN-transition/failover half (with a large `cooldown_s` so recovery deliberately never
# triggers, keeping that test's scope narrow); the test below closes the cooldown/`HALF_OPEN`
# recovery half the note used to disclaim.


def test_case_ii_backend_health_breaker_opens_on_the_fixture_fleet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§12.43 case (ii)'s OPEN-transition/failover half, on the SAME two-repo fixture fleet: both
    HEAVY targets point at ONE real stub server (same `base_url`, differentiated only by the
    `model` field in the request — `BackendHealth`'s key is `f"{backend}:{model_id}"`, not the
    URL). `fixture-heavy-a` answers every request with a genuine HTTP 429
    (`StatusReply(status=429, ...)`, mapping to the `openai` SDK's own `RateLimitError`, distinct
    from the plain-body 5xx path); `fixture-heavy-b` always answers successfully. `open_after_
    failures` is lowered to 1 (a legal `llm.failover.*` operator config, per research-51's own
    load-bearing fact about this config block) so a single qualifying failure opens the breaker
    deterministically regardless of which repo's call happens to exhaust its backoff first.

    See the module-level comment above this test for the disclosed cooldown/`HALF_OPEN` scope
    limit.
    """
    fleet_yaml = FLEET_YAML + (
        "llm:\n  failover:\n    open_after_failures: 1\n    cooldown_s: 120\n"
    )

    def responder(request: Mapping[str, object]) -> Mapping[str, object] | StatusReply:
        if request.get("model") == "fixture-heavy-a":
            return StatusReply(
                status=429,
                body={"error": {"message": "rate limited", "type": "rate_limit_error"}},
            )
        return _classify_responder(request)

    with running_stub_server() as server:
        server.set_responder(responder)
        workspace = _make_workspace_custom(
            tmp_path,
            "case_ii",
            fleet_yaml=fleet_yaml,
            models_yaml=_MODELS_YAML_TEMPLATE.format(
                heavy_a=server.base_url, heavy_b=server.base_url
            ),
        )
        monkeypatch.chdir(workspace)

        result = scan_real_classify(workspace)
        assert result.exit_code == ExitCode.SUCCESS, result.output

        # sub-assertion 1: the breaker genuinely opened — a real `backend_health_transition`
        # event naming `fixture-heavy-a`, DOWN, citing `open_after_failures=1` — not merely "the
        # call succeeded somehow".
        transitions = query(
            workspace, "SELECT payload FROM events WHERE event = 'backend_health_transition'"
        )
        down_transitions = [
            json.loads(raw)
            for (raw,) in transitions
            if json.loads(raw)["to_state"] == "DOWN"
            and json.loads(raw)["model_id"] == ("fixture-heavy-a")
        ]
        assert down_transitions, "fixture-heavy-a never transitioned to DOWN"
        assert all("open_after_failures=1" in t["reason"] for t in down_transitions)

        # sub-assertion 2: every classify call failed over to fixture-heavy-b with trigger
        # RATE_LIMIT (the "walked its entire backoff schedule" trigger, not a bare CONNECTION).
        failover_rows = query(
            workspace, "SELECT payload FROM events WHERE event = 'backend_failover'"
        )
        assert len(failover_rows) == len(_NAMES), failover_rows
        for (raw,) in failover_rows:
            payload = json.loads(raw)
            assert payload["trigger"] == "RATE_LIMIT"
            assert payload["from_model_id"] == "fixture-heavy-a"
            assert payload["to_model_id"] == "fixture-heavy-b"

        # sub-assertion 3: zero calls ever served from anywhere but fixture-heavy-b, and every
        # repo's classify still lands (§11.8: a busy DOWN target must not sink the tier).
        cache_rows = query(
            workspace,
            "SELECT backend, model_id FROM llm_cache WHERE role = ?",
            ("repo_classify",),
        )
        assert len(cache_rows) == len(_NAMES), cache_rows
        for backend, model_id in cache_rows:
            assert backend == "openai_compatible"
            assert model_id == "fixture-heavy-b"


#: `fixture-heavy-a`'s own retry-and-failover sequence (client `max_transient_retries=4` × up to
#: `_SDK_TRANSIENT_RETRIES=2` retries + 1 initial SDK attempt = 5 × 3 = 15 raw HTTP requests,
#: measured directly, deterministic — jitter only varies the SLEEP between them, never the
#: COUNT) always completes within its own first 15 raw requests. This threshold is set well
#: above that (20, not 15) so repo 1's own exhaustion can never accidentally cross it, while
#: staying well inside repo 2's own retry budget (it needs at most 5 more failing attempts,
#: comfortably under its own 15-request ceiling) — repo 2's probe succeeds quickly rather than
#: needing its own full retry budget.
_CASE_II_RECOVERY_SUCCESS_AFTER: int = 20


def test_case_ii_cooldown_and_half_open_recovery_on_the_fixture_fleet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§12.43 case (ii)'s cooldown/`HALF_OPEN`-recovery half, on the real fixture fleet —
    `docs/CRITERIA_PLAN.md` §43's residual the review round on this task found was NOT actually
    blocked (see the corrected module comment above `test_case_ii_backend_health_breaker_opens_
    on_the_fixture_fleet`).

    `concurrency.llm.heavy: 1` genuinely serializes the two repos' classify dispatch (`workers/
    classify.py:162`'s `async with ctx.limits.for_tier(tier):` around the whole `ctx.llm.
    complete(...)` call, sized from this exact config leaf via `orchestrator/budgets.py::Limits.
    create()`), so repo 1's entire retry-and-failover sequence against `fixture-heavy-a`
    completes before repo 2's classify call ever starts — no interleaving, unlike the concurrent
    dispatch this task's first attempt measured. `open_after_failures: 1` opens the breaker on
    repo 1's single qualifying failure; `cooldown_s: 0` (the schema's own integer floor — a
    fractional value is rejected at config load) makes the very next `may_call` check
    unconditionally eligible to probe (`elapsed < 0` can never hold), which is repo 2's — so
    recovery does not depend on any wall-clock race between repo 1 finishing and repo 2 starting.
    `fixture-heavy-a`'s responder answers 429 for its own first `_CASE_II_RECOVERY_SUCCESS_AFTER`
    requests (covering repo 1's entire exhaustion with margin) and succeeds after that, so
    whichever request is repo 2's `HALF_OPEN` probe eventually lands on a real success.

    Asserts the full lifecycle read back from real SQLite events after a real CLI invocation:
    `UP -> DOWN` (repo 1's exhaustion), `DOWN -> HALF_OPEN` ("cooldown elapsed; probing"),
    `HALF_OPEN -> UP` ("probe succeeded") — and that repo 2's own `llm_cache` row is attributed
    to the RECOVERED `fixture-heavy-a`, not `fixture-heavy-b`, proving the probe's success was
    real and not a leftover failover.
    """
    fleet_yaml = (
        FLEET_YAML.replace(
            "concurrency:\n  cpu_pool_workers: 1\n  docker: 1\n",
            "concurrency:\n  cpu_pool_workers: 1\n  docker: 1\n  llm:\n    heavy: 1\n",
        )
        + "llm:\n  failover:\n    open_after_failures: 1\n    cooldown_s: 0\n"
    )
    assert "llm:\n    heavy: 1\n" in fleet_yaml, "the concurrency.llm.heavy override did not apply"

    calls_a: list[int] = []

    def responder(request: Mapping[str, object]) -> Mapping[str, object] | StatusReply:
        if request.get("model") != "fixture-heavy-a":
            return _classify_responder(request)
        calls_a.append(1)
        if len(calls_a) <= _CASE_II_RECOVERY_SUCCESS_AFTER:
            return StatusReply(
                status=429,
                body={"error": {"message": "rate limited", "type": "rate_limit_error"}},
            )
        content = json.dumps(
            {
                "ecosystem": "pypi",
                "is_library": True,
                "confidence": 0.9,
                "rationale": "recovered: this is fixture-heavy-a answering after its HALF_OPEN "
                "probe succeeded.",
            }
        )
        return {
            "model": "fixture-heavy-a",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 30, "completion_tokens": 14},
        }

    with running_stub_server() as server:
        server.set_responder(responder)
        workspace = _make_workspace_custom(
            tmp_path,
            "case_ii_recovery",
            fleet_yaml=fleet_yaml,
            models_yaml=_MODELS_YAML_TEMPLATE.format(
                heavy_a=server.base_url, heavy_b=server.base_url
            ),
        )
        monkeypatch.chdir(workspace)

        result = scan_real_classify(workspace)
        assert result.exit_code == ExitCode.SUCCESS, result.output

        # The full lifecycle, in order: UP -> DOWN -> HALF_OPEN -> UP.
        transitions = [
            json.loads(raw)
            for (raw,) in query(
                workspace, "SELECT payload FROM events WHERE event = 'backend_health_transition'"
            )
        ]
        a_transitions = [t for t in transitions if t["model_id"] == "fixture-heavy-a"]
        assert [t["to_state"] for t in a_transitions] == ["DOWN", "HALF_OPEN", "UP"], a_transitions
        assert "open_after_failures=1" in a_transitions[0]["reason"], a_transitions[0]
        assert "cooldown elapsed" in a_transitions[1]["reason"], a_transitions[1]
        assert "probe succeeded" in a_transitions[2]["reason"], a_transitions[2]

        # Exactly one failover (repo 1's), and it is NOT the whole story — repo 2 recovered.
        failover_rows = query(
            workspace, "SELECT payload FROM events WHERE event = 'backend_failover'"
        )
        assert len(failover_rows) == 1, failover_rows
        (failover_payload,) = (json.loads(raw) for (raw,) in failover_rows)
        assert failover_payload["trigger"] == "RATE_LIMIT"
        assert failover_payload["to_model_id"] == "fixture-heavy-b"

        # llm_cache: one row landed on fixture-heavy-b (repo 1's failover), one on the RECOVERED
        # fixture-heavy-a (repo 2's probe) — not two on fixture-heavy-b, which is what it would
        # read if the recovery silently never happened and repo 2 just failed over too.
        cache_rows = query(
            workspace, "SELECT model_id FROM llm_cache WHERE role = ?", ("repo_classify",)
        )
        assert sorted(model_id for (model_id,) in cache_rows) == [
            "fixture-heavy-a",
            "fixture-heavy-b",
        ], cache_rows

        # The busy-loop half of case (ii)'s own sentence, over the recorded call log: repo 1's
        # exhaustion and repo 2's probe together must not exceed a bounded, accounted-for number
        # of raw requests to fixture-heavy-a — never an unbounded poll.
        assert 16 <= len(calls_a) <= _CASE_II_RECOVERY_SUCCESS_AFTER + 15, len(calls_a)


# ---------------------------------------------------------------------------------------------
# Case (iii): schema exhaustion AND CapabilityDrift, in ONE induced scenario.
# ---------------------------------------------------------------------------------------------


#: `fixture-heavy-b`'s capabilities PROMISE `structured_output_modes: [JSON_SCHEMA, PROMPTED]`
#: (the best of which is JSON_SCHEMA — `promised_mode`'s reading) but withhold `supports_json_
#: schema` (defaults False), so `negotiate()` — which reads the boolean flags, never the promise
#: list — can only reach PROMPTED. `actual` (PROMPTED) ranks below `promised` (JSON_SCHEMA):
#: exactly `CapabilityDrift`, driven entirely by config (`merge_capabilities`/`negotiate`/
#: `promised_mode` in `src/fleet/llm/client.py`), no response-content trickery needed.
_MODELS_YAML_TEMPLATE_DRIFT = """\
version: 2
roles:
  conflict_resolution: HEAVY
  api_incompat_rewrite: HEAVY
  build_authoring: HEAVY
  cycle_break_proposal: HEAVY
  escalation: HEAVY
  transform_repair: WORKHORSE
  build_diagnosis: WORKHORSE
  manifest_extract: WORKHORSE
  pr_body: WORKHORSE
  repo_classify: HEAVY
  dep_disambiguate: CHEAP
  pr_title: CHEAP
default_profile: default
profiles:
  default:
    HEAVY:
      - {{ backend: openai_compatible, model_id: fixture-heavy-a, effort: high, price: free,
          base_url: '{base_url}',
          capabilities_override: {{ supports_json_schema: true, max_output_tokens: 2048,
                                   structured_output_modes: [JSON_SCHEMA, PROMPTED] }} }}
      - {{ backend: openai_compatible, model_id: fixture-heavy-b, effort: high, price: free,
          base_url: '{base_url}',
          capabilities_override: {{ max_output_tokens: 2048,
                                   structured_output_modes: [JSON_SCHEMA, PROMPTED] }} }}
    WORKHORSE:
      - {{ backend: anthropic, model_id: claude-sonnet-5, effort: high,
          api_key_env: ANTHROPIC_API_KEY,
          price: {{ in_per_mtok: 3.0, out_per_mtok: 15.0 }} }}
    CHEAP:
      - {{ backend: anthropic, model_id: claude-haiku-4-5,
          api_key_env: ANTHROPIC_API_KEY,
          price: {{ in_per_mtok: 1.0, out_per_mtok: 5.0 }} }}
"""


def test_case_iii_schema_exhaustion_and_capability_drift_on_the_fixture_fleet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§12.43 case (iii), both halves in ONE induced call, on the SAME two-repo fixture fleet —
    the residual `tests/test_llm_findings.py::test_schema_exhaustion_and_capability_drift_
    together_in_one_induced_call` proved only at component level (`TieredScriptedBackend`).

    ONE real stub server serves both HEAVY targets. `fixture-heavy-a` always answers with
    schema-VALID JSON that is model-INVALID (`confidence` out of Pydantic's `[0, 1]` range),
    exhausting `llm.max_schema_repairs` (shipped default 1: initial + one repair, both invalid)
    and failing over with trigger `SCHEMA_UNSATISFIED`. `fixture-heavy-b` answers validly, but
    its capabilities are engineered (see `_MODELS_YAML_TEMPLATE_DRIFT` above) to achieve PROMPTED
    where its own `structured_output_modes` promises JSON_SCHEMA — `CapabilityDrift`, on the SAME
    call the failover happened in, exactly as the component-level test proves, but now read back
    from real SQLite state after a real CLI classify dispatch.

    Deterministic and concurrency-safe regardless of which repo's call lands on which target
    first: both targets' scripted behaviour is stateless (always-invalid / always-valid-but-
    drifting), so nothing here depends on call order or timing the way case (ii)'s cooldown would.
    """

    def responder(request: Mapping[str, object]) -> Mapping[str, object]:
        if request.get("model") == "fixture-heavy-a":
            content = json.dumps(
                {
                    "ecosystem": "pypi",
                    "is_library": True,
                    "confidence": 5.0,  # out of Pydantic's ge=0.0, le=1.0 -- schema-valid JSON,
                    "rationale": "always invalid: exhausts llm.max_schema_repairs on purpose.",
                }
            )
            return {
                "model": request.get("model"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 30, "completion_tokens": 14},
            }
        return _classify_responder(request)

    with running_stub_server() as server:
        server.set_responder(responder)
        workspace = _make_workspace_custom(
            tmp_path,
            "case_iii",
            models_yaml=_MODELS_YAML_TEMPLATE_DRIFT.format(base_url=server.base_url),
        )
        monkeypatch.chdir(workspace)

        result = scan_real_classify(workspace)
        assert result.exit_code == ExitCode.SUCCESS, result.output

        # sub-assertion 1: schema exhaustion AND failover, trigger SCHEMA_UNSATISFIED.
        failover_rows = query(
            workspace, "SELECT payload FROM events WHERE event = 'backend_failover'"
        )
        assert len(failover_rows) == len(_NAMES), failover_rows
        for (raw,) in failover_rows:
            payload = json.loads(raw)
            assert payload["trigger"] == "SCHEMA_UNSATISFIED"
            assert payload["from_model_id"] == "fixture-heavy-a"
            assert payload["to_model_id"] == "fixture-heavy-b"

        # sub-assertion 2: a CapabilityDrift finding attributed to fixture-heavy-b (the target
        # that actually answered under drift), from the SAME call the failover happened in.
        # Exactly ONE row, not one per repo: `findings` is fleet-level for this kind
        # (`repo_id IS NULL`) and deduplicated on `(run_id, kind, fingerprint)`
        # (`orchestrator/findings.py`'s `ON CONFLICT` clause) — both repos' calls drift
        # identically (same role/tier/backend/model_id/promised/actual), so the second is a
        # genuine dedup, not a missed write.
        drift_rows = query(workspace, "SELECT payload FROM findings WHERE kind = 'CapabilityDrift'")
        assert len(drift_rows) == 1, drift_rows
        for (raw,) in drift_rows:
            payload = json.loads(raw)
            assert payload["role"] == "repo_classify"
            assert payload["model_id"] == "fixture-heavy-b"
            assert payload["promised"] == "JSON_SCHEMA"
            assert payload["actual"] == "PROMPTED"

        # sub-assertion 3: the run still completes, cleanly, through the real answering target.
        cache_rows = query(
            workspace,
            "SELECT backend, model_id, structured_output_mode FROM llm_cache WHERE role = ?",
            ("repo_classify",),
        )
        assert len(cache_rows) == len(_NAMES), cache_rows
        for backend, model_id, mode in cache_rows:
            assert backend == "openai_compatible"
            assert model_id == "fixture-heavy-b"
            assert mode == "PROMPTED"


# ---------------------------------------------------------------------------------------------
# Case (iv)'s own paired clause: the SAME fixture, under `fleet transform --deterministic-only`,
# with HEAVY still broken, dispatches no LLM call at all.
# ---------------------------------------------------------------------------------------------


def sequence_cmd(root: Path) -> Any:
    return runner.invoke(app, [*_args(root), "sequence"], catch_exceptions=False)


def transform_cmd(root: Path, *, json_output: bool = True) -> Any:
    argv = [*_args(root)]
    if json_output:
        argv.append("--json")
    argv += ["transform", "--deterministic-only"]
    return runner.invoke(app, argv, catch_exceptions=False)


def test_deterministic_only_dispatches_no_llm_call_against_the_broken_heavy_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case (iv)'s own second clause (`docs/SPEC.md` §12.43, `grep -n "on the fixture fleet"`):
    "the same fixture under `fleet transform --deterministic-only` completes its rule-resolvable
    repos with exit 0 and dispatches no LLM call at all." Proven elsewhere
    (`tests/test_transform_e2e.py::test_deterministic_only_reaches_no_model`) but never before
    against THIS file's own broken-HEAVY config (arm 2's unreachable ports, reused verbatim via
    `_make_workspace`) — the residual `docs/CRITERIA_PLAN.md` §43 names.

    `fleet scan --skip-classify` (classify is not this clause's concern — cases (i)-(iii) above
    already cover it; `--deterministic-only`'s OWN cap is Phase 2's) then `fleet sequence` builds
    the wave plan; `fleet transform --deterministic-only` then runs against a `config/models.yaml`
    whose `HEAVY` targets are the SAME two unreachable ports (`127.0.0.1:1`/`:2`) case (iv)'s own
    exit-8 test drives through classify — if the deterministic cap ever let a rung 2 escalation
    through, THIS run would either hang or exit 8 against those same dead ports, never exit 0.
    Exit 0 is therefore already strong evidence nothing was dispatched; `llm_cache` staying empty
    for every role (not just `repo_classify`) is the direct proof.

    **A measured, disclosed scope note for this exact fixture (Rule 12's own "audit mutations for
    expressibility" — a mutation nothing can express reports a pass that means nothing):** these
    two Python-only repos (`acme-lib-py`, `acme-app-py`) have no cross-repo import needing a
    textual rewrite, so BOTH repos resolve as a pure relocation with zero identified rewrite
    units — confirmed directly by re-running this exact scenario with `cli.py`'s deterministic
    cap (`_validate_transform_flags`'s `return (1 if deterministic_only else max_attempts),
    policies`) mutated to `return max_attempts, policies` (the cap removed entirely): the output
    is BYTE-IDENTICAL (`exit_code: 0, succeeded: 2, commits: 4, failed: 0`) with or without the
    cap, because nothing in this fixture ever fails at rung 1 for the cap to have a chance to
    gate. That mutation is therefore a confirmed NO-OP here and is not reported as a discriminator
    (reverted; `git diff --stat src/` was empty both before measuring and after).

    **The mutation actually reported, confirmed to discriminate:** `workers/base.py`'s
    `_TIER_FOR_RUNG[None]` (rung 1's tier stamp) mutated from `TransformTier.DETERMINISTIC` to
    `TransformTier.LLM_REPAIR` reddens this test's own `exit_code == ExitCode.SUCCESS` assertion
    — `attempts`' own `CHECK ((tier = 'DETERMINISTIC') = (context_policy IS NULL))` constraint
    (rung 1's `context_policy` stays `None`; only the tier label changed) fails at INSERT time,
    the per-repo `TaskGroup` isolation catches it as a `repo_task_escaped`
    `sqlite3.IntegrityError`, and the wave-open guard then refuses (`ExitCode.USAGE`, not
    `SUCCESS`). This proves the test is sensitive to whether rung 1 is genuinely stamped
    DETERMINISTIC, not merely "always green regardless of what rung 1 does". Reverted;
    `git diff --stat src/` empty after.
    """
    workspace = _make_workspace(tmp_path, "det_only", heavy_a=_HEAVY_A, heavy_b=_HEAVY_B)
    (workspace / "config" / "rules").mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(workspace)

    calls: list[str] = []

    async def explode(*args: object, **kwargs: object) -> object:
        calls.append("complete")
        raise AssertionError("the deterministic transform path must not reach a model")

    monkeypatch.setattr("fleet.llm.client.LadderModelClient.complete", explode)

    scan_result = runner.invoke(
        app, [*_args(workspace), "scan", "--skip-classify"], catch_exceptions=False
    )
    assert scan_result.exit_code == ExitCode.SUCCESS, scan_result.output
    assert sequence_cmd(workspace).exit_code == ExitCode.SUCCESS

    result = transform_cmd(workspace)
    assert result.exit_code == ExitCode.SUCCESS, result.output
    payload = json.loads(result.stdout)
    assert payload["succeeded"] == len(_NAMES), payload
    assert payload["failed"] == 0, payload
    assert calls == [], "LadderModelClient.complete was reached under --deterministic-only"

    cache_count = query(workspace, "SELECT COUNT(*) FROM llm_cache")
    assert cache_count == [(0,)], cache_count

    tiers = dict(query(workspace, "SELECT repo_id, tier FROM attempts WHERE phase = 2"))
    assert tiers == dict.fromkeys(_NAMES, "DETERMINISTIC"), tiers
