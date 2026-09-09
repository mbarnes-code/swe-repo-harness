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
from tests.fixtures.llm.stub_openai_server import running_stub_server
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
