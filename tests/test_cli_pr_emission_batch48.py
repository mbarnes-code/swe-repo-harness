"""Round VIII §15.1 item 3, Wave 7.7 batch 48 (group G7) — direct, standalone unit tests of
`src/fleet/cli.py`'s PR candidate-selection + emission subsystem:

`_PrCandidate`, `_atomic_wave_findings`, `_pr_candidates`, `_report_with_stubs`, `_pr_units`,
`_pr_impl`, `_drain_llm_findings`, `_emit_prs` (`_emit_one_pr` excluded — already proven in Wave
2), `_extract_migration_notes`, `_regenerate_pr_body`, `_remote_branch_tip`, `_promote_one_pr`,
`_promote_prs`, `_pr_lines`.

Several of these (`_report_with_stubs`, `_extract_migration_notes`, `_regenerate_pr_body`,
`_promote_one_pr`) already carry direct unit tests in `tests/test_cli.py`, and the whole subsystem
is exercised end to end by `tests/test_pr_e2e.py`. Every test below was chosen by first reading
what those two files actually reach (`grep`-confirmed absent: `wave=`/glob `--repo` filtering,
`scc_incomplete`, `PrEmissionError`'s sibling `llm_findings_flush_failed`/`outcome.held`/`no
'origin' ref` branches, and any assertion on `_pr_lines`' own formatted text) and then targeting
specifically the branch that survey found untested — built on the cheapest fixture that reaches
it: a bare `fresh_db`-schema SQLite file plus hand-seeded rows for the DB-backed functions (mirror
of `tests/test_cli.py`'s own `test_pr_impl_admits_a_second_layer_dependent...` convention), a real
(but bazel/gazelle-free) two-branch git remote for the git-primitive functions (mirroring
`tests/test_cli.py::_promotion_repo`), and hand-built `_PrCandidate`/`PullRequestDraft` fixtures
for the pure functions — never the full `fleet pr`/real-bazel/`gh` e2e machinery
`tests/test_pr_e2e.py` exists to amortize.

Two items in this batch's scope get no new test here, by design, not omission:

* `_PrCandidate` is a frozen, slotted dataclass with no branching logic of its own — nothing to
  mutate. Its one behaviourally-relevant field (`scc`, `None` for the overwhelmingly common
  singleton case) is what `test_pr_units_groups_scc_members_by_scc_id_and_keeps_singletons_
  separate` below exercises, since `_pr_units` is the sole consumer that branches on it.
* `_extract_migration_notes` already has two direct tests in `tests/test_cli.py` covering both of
  its only two reachable paths (marker absent / marker present) — `render_body` only ever emits
  the marker when `notes` is truthy, so a third "marker present with empty notes" case cannot
  occur through any real caller. No gap found.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import pytest
from pydantic import ValidationError

from fleet import cli
from fleet.cli import (
    GlobalOptions,
    _atomic_wave_findings,
    _drain_llm_findings,
    _emit_prs,
    _pr_candidates,
    _pr_impl,
    _pr_lines,
    _pr_units,
    _PrCandidate,
    _promote_one_pr,
    _promote_prs,
    _record_verification,
    _regenerate_pr_body,
    _remote_branch_tip,
    _report_with_stubs,
)
from fleet.models.enums import BreakStrategy, Phase, PrState, RepoStatus, StubFidelity, StubState
from fleet.models.graph import CycleFinding
from fleet.models.tasks import PullRequestDraft, VerificationReport
from fleet.settings import FleetSettings
from fleet.state.db import StateWriter, connect_ro
from fleet.vcs.git import Git
from fleet.workers.prwriter import PrwriterOutput
from tests.test_cli import (
    FLEET_YAML,
    _FakeForgeForPromotion,
    _held_pr_record,
    _promotion_repo,
    _sh,
    _stub_report,
    fresh_db,
    seed_run,
    write_config,
)

RUN: Final = "10000000-1000-4000-8000-000000000048"
_STAMP: Final = "2026-09-14T00:00:00+00:00"
_NOW: Final = datetime(2026, 9, 14, tzinfo=UTC)

#: A `fleet.yaml` with a monorepo path that lives INSIDE `tmp_path` (the shipped fixture's
#: `../acme-monorepo` resolves a level above it), so `_monorepo_checkout`'s only two preconditions
#: (a `.git` dir, the configured branch checked out) can be satisfied with a plain `git init` and
#: no bazel/gazelle — the functions under test here never reach a real build.
_MONOREPO_FLEET_YAML = FLEET_YAML.replace(
    "monorepo_path: ../acme-monorepo", "monorepo_path: monorepo"
)


def _candidate(
    repo_id: str, *, scc: CycleFinding | None = None, dependencies: tuple[str, ...] = ()
) -> _PrCandidate:
    """A minimal, valid `_PrCandidate` — everything but `repo_id`/`scc`/`dependencies` held at an
    inert default, mirroring `tests/test_cli.py::_pr_candidate` but with a caller-chosen `repo_id`
    (that helper hardcodes `acme-lib-py`, which cannot express two DISTINCT candidates in one
    `_pr_units`/`_promote_prs` grouping test)."""
    return _PrCandidate(
        repo_id=repo_id,
        wave_index=0,
        status=RepoStatus.SUCCEEDED,
        source_url=f"https://example.invalid/{repo_id}",
        source_sha="a" * 40,
        report=_stub_report(stubbed=False),
        seed="seed-1",
        stub_states={},
        stub_fidelity={},
        dependencies=dependencies,
        scc=scc,
    )


def _init_monorepo(tmp_path: Path) -> Path:
    """A real (non-bare) git repo at `tmp_path/monorepo` with `integration` checked out and one
    commit — the entire precondition `_monorepo_checkout` enforces, with no `origin` remote and no
    bazel/gazelle involved, since every test using this drives `_emit_one_pr`/`_promote_one_pr`
    through a double rather than for real."""
    _sh(tmp_path, "init", "-q", "monorepo")
    monorepo_path = tmp_path / "monorepo"
    _sh(monorepo_path, "config", "user.email", "fleet-test@example.invalid")
    _sh(monorepo_path, "config", "user.name", "fleet-test")
    _sh(monorepo_path, "checkout", "-q", "-b", "integration")
    (monorepo_path / "README.md").write_text("root\n", encoding="utf-8")
    _sh(monorepo_path, "add", "-A")
    _sh(monorepo_path, "commit", "-q", "-m", "root")
    return monorepo_path


def _insert_cycle_finding(db_path: Path, *, run_id: str, finding: CycleFinding) -> None:
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO findings (run_id, repo_id, kind, severity, fingerprint, payload, "
            "                      created_at) VALUES (?, NULL, 'CycleDetected', 'warn', ?, ?, ?)",
            (run_id, finding.scc_id, finding.model_dump_json(), _STAMP),
        )
        conn.commit()
    finally:
        conn.close()


# ======================================================================================
# _atomic_wave_findings
# ======================================================================================


async def test_atomic_wave_findings_excludes_non_atomic_wave_break_strategies(
    tmp_path: Path,
) -> None:
    """Own docstring: "A repo absent from the result is not in any ATOMIC_WAVE SCC (it may still
    be in an EDGE_BREAK/CONTRACT_HOIST/MANUAL one...)". Every existing caller/test only ever seeds
    an ATOMIC_WAVE finding (`tests/test_pr_e2e.py::test_an_atomic_wave_scc_ships_one_pr_shared_
    by_every_member`) — the filter's OTHER half, that a co-existing MANUAL/EDGE_BREAK finding in
    the SAME run is correctly excluded rather than leaking its members in, has never been proven.
    """
    db_path = fresh_db(tmp_path / "state" / "fleet.db")
    seed_run(db_path, run_id=RUN, repos=("acme-a", "acme-b", "acme-c", "acme-d"))
    atomic = CycleFinding(
        scc_id="scc:" + "a" * 16,
        members=["acme-a", "acme-b"],
        edges=[],
        break_strategy=BreakStrategy.ATOMIC_WAVE,
        atomic_wave_index=0,
    )
    manual = CycleFinding(
        scc_id="scc:" + "b" * 16,
        members=["acme-c", "acme-d"],
        edges=[],
        break_strategy=BreakStrategy.MANUAL,
    )
    _insert_cycle_finding(db_path, run_id=RUN, finding=atomic)
    _insert_cycle_finding(db_path, run_id=RUN, finding=manual)

    conn = await connect_ro(db_path)
    try:
        result = await _atomic_wave_findings(conn, RUN)
    finally:
        await conn.close()

    assert set(result) == {"acme-a", "acme-b"}, result
    assert result["acme-a"].scc_id == atomic.scc_id
    assert result["acme-b"].scc_id == atomic.scc_id


# ======================================================================================
# _pr_candidates — the `wave` filter and the `only` fnmatch glob
# ======================================================================================


async def test_pr_candidates_applies_the_wave_filter_and_the_only_glob(tmp_path: Path) -> None:
    """`tests/test_cli.py::test_pr_impl_admits_a_second_layer_dependent_and_reports_stub_limited_
    not_full` (the only direct `_pr_candidates` unit test) always calls with `wave=None,
    only=None`; `tests/test_pr_e2e.py` always drives `--repo` with an EXACT repo_id, never a glob,
    and never drives `fleet pr --wave`. Both filters — `waves.get(repo_id) != wave` and
    `not fnmatch(repo_id, only)` — have therefore never been proven to actually filter."""
    write_config(tmp_path)
    settings = FleetSettings.load(tmp_path / "config")
    db_path = fresh_db(tmp_path / "state" / "fleet.db")
    seed_run(db_path, run_id=RUN, repos=("acme-alpha", "acme-beta"))

    async with StateWriter(db_path, owner="test-batch48-filters") as writer:

        async def unit(conn: object) -> None:
            for wave_index in (0, 1):
                await conn.execute(  # type: ignore[attr-defined]
                    "INSERT INTO waves (run_id, wave_index, computed_at) VALUES (?, ?, ?)",
                    (RUN, wave_index, _STAMP),
                )
            for repo_id, wave_index in (("acme-alpha", 0), ("acme-beta", 1)):
                await conn.execute(  # type: ignore[attr-defined]
                    "INSERT INTO phases (run_id, repo_id, phase, status, updated_at) "
                    "VALUES (?, ?, ?, 'SUCCEEDED', ?)",
                    (RUN, repo_id, int(Phase.VERIFY), _STAMP),
                )
                await conn.execute(  # type: ignore[attr-defined]
                    "INSERT INTO wave_members (run_id, wave_index, node_kind, node_id) "
                    "VALUES (?, ?, 'REPO', ?)",
                    (RUN, wave_index, repo_id),
                )
                await conn.execute(  # type: ignore[attr-defined]
                    "UPDATE repos SET head_sha = ? WHERE repo_id = ?", ("a" * 40, repo_id)
                )

        await writer.submit(unit)
        for repo_id in ("acme-alpha", "acme-beta"):
            report = VerificationReport(
                run_id=uuid.UUID(RUN), repo_id=repo_id, build_ok=True, test_ok=True, verdict="PASS"
            )
            await _record_verification(writer, RUN, repo_id, report, seed="", now=_NOW)

    read_conn = await connect_ro(db_path)
    try:
        wave1, _ = await _pr_candidates(read_conn, settings, RUN, 1, None)
        globbed, _ = await _pr_candidates(read_conn, settings, RUN, None, "acme-a*")
        both, _ = await _pr_candidates(read_conn, settings, RUN, 0, "acme-a*")
        none_match, _ = await _pr_candidates(read_conn, settings, RUN, 1, "acme-a*")
    finally:
        await read_conn.close()

    assert [c.repo_id for c in wave1] == ["acme-beta"], wave1
    assert [c.repo_id for c in globbed] == ["acme-alpha"], globbed
    assert [c.repo_id for c in both] == ["acme-alpha"], both
    assert [c.repo_id for c in none_match] == [], none_match


# ======================================================================================
# _report_with_stubs — the `stub_fidelity` dict comprehension's `if key in fidelity` guard
# ======================================================================================


def test_report_with_stubs_raises_rather_than_silently_admitting_an_unfidelitied_stub() -> None:
    """The REVERSE mismatch — a `states` key `fidelity` lacks — cannot produce a silently-wrong
    report: `VerificationReport`'s own model invariant ("`stub_fidelity` must name exactly the
    coord_keys in `verified_against_stubs`") refuses it. This is also the discriminating half: a
    mutation that deleted the `if key in fidelity` guard would turn this into a bare `KeyError`
    from the dict comprehension itself, never reaching — and never raising — the model's own
    clearly-worded validation error.
    """
    report = _stub_report(stubbed=True)
    states = {"npm:@acme/gone": StubState.ACTIVE, "npm:@acme/no-fidelity": StubState.ACTIVE}
    fidelity = {"npm:@acme/gone": StubFidelity.PUBLISHED_ARTIFACT}

    with pytest.raises(ValidationError, match="stub_fidelity must name exactly"):
        _report_with_stubs(report, states, fidelity)


# ======================================================================================
# _pr_units — grouping by `scc.scc_id`, order-preserving, non-adjacent members included
# ======================================================================================


def test_pr_units_groups_scc_members_by_scc_id_and_keeps_singletons_separate() -> None:
    """Also the only real exercise of `_PrCandidate.scc` in this batch (see the module docstring):
    `_pr_units` is the sole consumer that branches on it. Candidates are deliberately ordered
    `(x, solo, y)` — `x` and `y` share one SCC but are NOT adjacent in the input — so a mutation
    that grouped by contiguous run (an `itertools.groupby`-shaped regression) rather than by
    `scc_id` would fail this, while a naive "members always arrive adjacent" fixture would not."""
    scc = CycleFinding(
        scc_id="scc:" + "c" * 16,
        members=["acme-x", "acme-y"],
        edges=[],
        break_strategy=BreakStrategy.ATOMIC_WAVE,
        atomic_wave_index=0,
    )
    x = _candidate("acme-x", scc=scc)
    y = _candidate("acme-y", scc=scc)
    solo = _candidate("acme-solo")

    units = _pr_units((x, solo, y))

    assert units == ((x, y), (solo,)), units


# ======================================================================================
# _pr_impl — `scc_incomplete`: an ATOMIC_WAVE SCC missing a member that has not reached VERIFY
# ======================================================================================


async def test_pr_impl_reports_scc_incomplete_when_a_member_has_not_reached_verify(
    tmp_path: Path,
) -> None:
    """`dry_run=True` deliberately avoids `_emit_prs`/`_promote_prs` (neither runs when `eligible`
    is empty and nothing is `promotable`), so this needs no monorepo checkout at all — the ONLY
    thing under test is the `missing = sorted(set(scc.members) - member_ids)` branch, which no
    existing test reaches: `tests/test_pr_e2e.py::test_an_atomic_wave_scc_ships_one_pr_shared_by_
    every_member` always brings every member through VERIFY together."""
    write_config(tmp_path)
    settings = FleetSettings.load(tmp_path / "config")
    db_path = fresh_db(tmp_path / "state" / "fleet.db")
    seed_run(db_path, run_id=RUN, repos=("acme-a", "acme-b"))
    scc = CycleFinding(
        scc_id="scc:" + "d" * 16,
        members=["acme-a", "acme-b"],
        edges=[],
        break_strategy=BreakStrategy.ATOMIC_WAVE,
        atomic_wave_index=0,
    )
    _insert_cycle_finding(db_path, run_id=RUN, finding=scc)

    async with StateWriter(db_path, owner="test-batch48-scc-incomplete") as writer:

        async def unit(conn: object) -> None:
            await conn.execute(  # type: ignore[attr-defined]
                "INSERT INTO waves (run_id, wave_index, computed_at) VALUES (?, 0, ?)",
                (RUN, _STAMP),
            )
            await conn.execute(  # type: ignore[attr-defined]
                "INSERT INTO phases (run_id, repo_id, phase, status, updated_at) "
                "VALUES (?, 'acme-a', ?, 'SUCCEEDED', ?)",
                (RUN, int(Phase.VERIFY), _STAMP),
            )
            await conn.execute(  # type: ignore[attr-defined]
                "INSERT INTO wave_members (run_id, wave_index, node_kind, node_id) "
                "VALUES (?, 0, 'REPO', 'acme-a')",
                (RUN,),
            )
            await conn.execute(  # type: ignore[attr-defined]
                "UPDATE repos SET head_sha = ? WHERE repo_id = 'acme-a'", ("a" * 40,)
            )

        await writer.submit(unit)
        report = VerificationReport(
            run_id=uuid.UUID(RUN), repo_id="acme-a", build_ok=True, test_ok=True, verdict="PASS"
        )
        await _record_verification(writer, RUN, "acme-a", report, seed="", now=_NOW)

    result = await _pr_impl(
        GlobalOptions(),
        settings,
        db_path,
        run_id=RUN,
        wave=None,
        only=None,
        ready=False,
        dry_run=True,
    )

    assert result["scc_incomplete"] == {scc.scc_id: ["acme-b"]}, result
    assert result["eligible"] == [], result
    assert result["opened"] == {}, result
    assert result["exit_code"] == 0, result  # scc_incomplete is reported, never a failure


# ======================================================================================
# _drain_llm_findings — the `except Exception` swallow-and-log branch
# ======================================================================================


class _FailingFindingsSink:
    """A minimal double for `RunContext.llm_findings`: `flush()` always raises, exactly the
    condition `_drain_llm_findings`'s `except Exception` branch exists to survive."""

    def __init__(self) -> None:
        self.pending = 3

    async def flush(self) -> None:
        raise RuntimeError("disk full")


class _RecordingLog:
    def __init__(self) -> None:
        self.errors: list[tuple[str, dict[str, object]]] = []

    def error(self, event: str, **fields: object) -> None:
        self.errors.append((event, fields))


class _FakeDrainCtx:
    """Duck-typed `RunContext` stand-in carrying only the three attributes `_drain_llm_findings`
    reads (`llm_findings`, `log`, `run_id`) — a real `RunContext` needs a live DB/ledger/limits
    this branch has no use for."""

    def __init__(self) -> None:
        self.run_id = uuid.UUID(RUN)
        self.llm_findings = _FailingFindingsSink()
        self.log = _RecordingLog()


async def test_drain_llm_findings_swallows_a_flush_failure_and_logs_it() -> None:
    """No existing test drives `ctx.llm_findings.flush()` to raise at all (`grep -rn
    llm_findings_flush_failed tests/` finds nothing before this test) — the closest,
    `tests/test_pr_e2e.py::test_fleet_pr_persists_its_llm_findings_even_when_the_command_fails_
    partway`, proves the drain runs on a command failure, not that the drain ITSELF surviving a
    failed flush is what keeps a fleet of successfully-opened PRs from being reported as a crash.
    """
    ctx = _FakeDrainCtx()

    await _drain_llm_findings(ctx)  # type: ignore[arg-type]  # must not raise

    assert len(ctx.log.errors) == 1, ctx.log.errors
    event, fields = ctx.log.errors[0]
    assert event == "llm_findings_flush_failed"
    assert fields["run_id"] == str(ctx.run_id)
    assert fields["pending"] == 3
    assert fields["error"] == "disk full"


# ======================================================================================
# _emit_prs — the `outcome.held` handling branch (worker-computed, distinct from `_pr_impl`'s own
# static "dependency not MERGED per the DB" hold)
# ======================================================================================


async def test_emit_prs_records_a_worker_computed_hold_as_held_not_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_emit_one_pr` is excluded from this batch's scope (already proven, Wave 2), but the
    CALLER-side handling of `outcome.held` inside `_emit_prs` — `held[repo_id] =
    list(outcome.unmerged_dependencies); continue` — is a genuinely separate branch from
    `_pr_impl`'s own precomputed `held` (a unit with `outcome.held=True` was DISPATCHED, meaning
    `_pr_impl`'s static check passed; the worker's own live-forge check is what disagreed — the
    "observed, never assumed" second check `_pr_impl`'s docstring names). No existing test ever
    drives `_emit_one_pr` to return `held=True` (`grep -rn unmerged_dependencies tests/` finds
    nothing before this test), so `_emit_prs`'s handling of it has never been exercised. `gh` is
    never invoked here — the point is to isolate `_emit_prs`'s own dict bookkeeping."""
    write_config(tmp_path, fleet=_MONOREPO_FLEET_YAML)
    settings = FleetSettings.load(tmp_path / "config")
    db_path = fresh_db(tmp_path / "state" / "fleet.db")
    seed_run(db_path, run_id=RUN, repos=("acme-solo",))
    _init_monorepo(tmp_path)

    unit = (_candidate("acme-solo"),)

    async def fake_emit_one_pr(
        ctx: object,
        worker: object,
        settings_arg: object,
        unit_arg: object,
        *,
        records: object,
        monorepo_path: object,
        pr_root: object,
        ready: object,
    ) -> PrwriterOutput:
        return PrwriterOutput(held=True, unmerged_dependencies=["acme-blocker"])

    monkeypatch.setattr(cli, "_emit_one_pr", fake_emit_one_pr)

    opened, drafted, held, failed = await _emit_prs(
        settings, db_path, run_id=RUN, units=(unit,), records={}, ready=False
    )

    assert held == {"acme-solo": ["acme-blocker"]}, held
    assert opened == {}, opened
    assert drafted == [], drafted
    assert failed == {}, failed


# ======================================================================================
# _regenerate_pr_body — a non-empty `dependencies` list (every existing test uses `()`)
# ======================================================================================


def test_regenerate_pr_body_renders_a_dependencys_current_forge_state() -> None:
    """Every `_regenerate_pr_body` test in `tests/test_cli.py` builds its `unit` from
    `_pr_candidate`, which hardcodes `dependencies=()` — the whole `[DependencyPr(repo_id=dep,
    url=..., state=...) for dep in dependencies]` construction, including the `records` lookup for
    a dependency's CURRENT observed url/state, has never been exercised with a real dependency."""
    dep_record = PullRequestDraft(
        run_id=uuid.UUID(RUN),
        repo_id="acme-dep",
        wave_index=0,
        branch="migrate/acme-dep",
        base="integration",
        title="migrate acme-dep",
        body="body",
        source_url="https://example.invalid/acme-dep",
        source_sha="d" * 40,
        state=PrState.OPEN,
        url="https://github.invalid/acme/monorepo/pull/acme-dep",
    )
    candidate = _candidate("acme-consumer", dependencies=("acme-dep",))
    record = _held_pr_record(
        repo_id="acme-consumer",
        branch="migrate/acme-consumer",
        source_url="https://example.invalid/acme-consumer",
        url="https://github.invalid/acme/monorepo/pull/acme-consumer",
    )

    regenerated = _regenerate_pr_body(
        (candidate,), record, {("acme-dep", None): dep_record}, draft=False
    )

    assert f"- `acme-dep` — {dep_record.url} (`OPEN`)" in regenerated, regenerated

    # The control: a dependency ABSENT from `records` must render the documented fallback rather
    # than crashing on a missing key or inventing a state.
    regenerated_unknown = _regenerate_pr_body((candidate,), record, {}, draft=False)
    assert "- `acme-dep` — no PR yet (`DRAFTED`)" in regenerated_unknown, regenerated_unknown


# ======================================================================================
# _remote_branch_tip — the "no such ref on the remote yet" -> None branch
# ======================================================================================


async def test_remote_branch_tip_is_none_for_a_branch_never_pushed(tmp_path: Path) -> None:
    """Every `_remote_branch_tip` exercise in `tests/test_pr_e2e.py` reads a branch that IS on the
    remote (the promotion tests always push `migrate/acme-lib-py` first) — the `if not
    out.strip(): return None` branch, for a branch that exists only locally, has never fired under
    test."""
    _remote, clone = _promotion_repo(tmp_path)
    _sh(clone, "checkout", "-q", "-b", "migrate/never-pushed")
    (clone / "local-only.txt").write_text("x\n", encoding="utf-8")
    _sh(clone, "add", "-A")
    _sh(clone, "commit", "-q", "-m", "local only, never pushed")
    git = Git(clone)

    tip = await _remote_branch_tip(git, "origin", "migrate/never-pushed")

    assert tip is None


# ======================================================================================
# _promote_one_pr — the "no 'origin' ref; nothing to force-push onto" branch
# ======================================================================================


async def test_promote_one_pr_reports_no_origin_ref_for_a_branch_never_pushed(
    tmp_path: Path,
) -> None:
    """None of the five existing `_promote_one_pr` tests in `tests/test_cli.py` reach the
    `remote_tip is None` branch — they all use `_promotion_repo`'s `migrate/acme-lib-py`, which is
    always pushed. `record.branch` existing LOCALLY (so the FIRST precondition passes) but never
    having been pushed is the one remaining precondition failure this function can report."""
    _remote, clone = _promotion_repo(tmp_path)
    _sh(clone, "checkout", "-q", "-b", "migrate/never-pushed")
    (clone / "local-only.txt").write_text("x\n", encoding="utf-8")
    _sh(clone, "add", "-A")
    _sh(clone, "commit", "-q", "-m", "local only, never pushed")
    git = Git(clone)
    forge = _FakeForgeForPromotion()
    record = _held_pr_record(branch="migrate/never-pushed")
    body_path = tmp_path / "out" / "body.md"

    reason = await _promote_one_pr(
        git, forge, record, url=record.url, body="new body", body_path=body_path
    )

    assert reason is not None and "nothing to force-push onto" in reason, reason
    assert forge.edited == []
    assert forge.readied == []
    assert not body_path.exists()


# ======================================================================================
# _promote_prs — the shared-PR loop writing the promoted state to EVERY SCC member
# ======================================================================================


async def test_promote_prs_writes_the_promoted_state_to_every_scc_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`tests/test_pr_e2e.py`'s two `_promote_prs` proofs (through the real CLI) only ever promote
    a SINGLETON unit. The "same draft against every member" loop `_promote_prs` shares with
    `_emit_prs` (per both functions' own docstrings) has never been exercised with more than one
    `repo_id` in `unit`. `_promote_one_pr` is monkeypatched to a plain success so this test is
    scoped to `_promote_prs`'s own per-member bookkeeping, not to git/forge mechanics already
    proven directly."""
    write_config(tmp_path, fleet=_MONOREPO_FLEET_YAML)
    settings = FleetSettings.load(tmp_path / "config")
    db_path = fresh_db(tmp_path / "state" / "fleet.db")
    seed_run(db_path, run_id=RUN, repos=("acme-x", "acme-y"))
    _init_monorepo(tmp_path)

    scc = CycleFinding(
        scc_id="scc:" + "e" * 16,
        members=["acme-x", "acme-y"],
        edges=[],
        break_strategy=BreakStrategy.ATOMIC_WAVE,
        atomic_wave_index=0,
    )
    x = _candidate("acme-x", scc=scc)
    y = _candidate("acme-y", scc=scc)
    unit = (x, y)
    shared_url = "https://github.invalid/acme/monorepo/pull/shared"
    record = _held_pr_record(
        repo_id="acme-x",
        branch="migrate/acme-x",
        scc_id=scc.scc_id,
        member_repo_ids=["acme-x", "acme-y"],
        url=shared_url,
    )

    async def fake_promote_one_pr(
        git: object,
        forge: object,
        record_arg: object,
        *,
        url: object,
        body: object,
        body_path: object,
    ) -> None:
        return None

    monkeypatch.setattr(cli, "_promote_one_pr", fake_promote_one_pr)

    promoted, failed = await _promote_prs(
        settings, db_path, run_id=RUN, promotable=[(unit, shared_url, record)], records={}
    )

    assert promoted == {"acme-x": shared_url, "acme-y": shared_url}, promoted
    assert failed == {}, failed

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT repo_id, payload FROM findings WHERE kind = 'PullRequest' ORDER BY repo_id"
        ).fetchall()
    finally:
        conn.close()
    states = {str(row[0]): json.loads(str(row[1]))["state"] for row in rows}
    assert states == {"acme-x": "OPEN", "acme-y": "OPEN"}, states


# ======================================================================================
# _pr_lines — the formatted OUTPUT text itself (every e2e test reads the JSON payload instead)
# ======================================================================================


def test_pr_lines_renders_every_section_including_scc_incomplete() -> None:
    """`grep -n "result.output" tests/test_pr_e2e.py` shows `.output` used only inside failure
    assertion messages (`assert x.exit_code == ..., x.output`) — no test anywhere asserts on
    `_pr_lines`' actual rendered text, the thing a non-`--json` operator reads. This covers every
    section, including `SCC INCOMPLETE`, which no run in this tree has ever reached."""
    result: dict[str, object] = {
        "opened": {"acme-open": "https://forge.invalid/pr/1"},
        "draft": ["acme-open"],
        "held": {"acme-held": ["acme-dep"]},
        "already_open": [],
        "unverified": ["acme-unverified"],
        "scc_incomplete": {"scc:aaaaaaaaaaaaaaaa": ["acme-missing"]},
    }

    lines = _pr_lines(result)

    assert lines[0] == "pr: opened 1 PR(s) (1 draft), held 1, 0 already open", lines[0]
    assert "  acme-open -> https://forge.invalid/pr/1" in lines
    assert "  HELD acme-held: dependency PR not MERGED — acme-dep" in lines
    assert "  SKIPPED acme-unverified: no persisted VerificationReport" in lines
    assert "  SCC INCOMPLETE scc:aaaaaaaaaaaaaaaa: waiting on VERIFY for acme-missing" in lines
