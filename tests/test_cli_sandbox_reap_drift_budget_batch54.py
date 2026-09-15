"""§15.1 item 3, Wave 7.9 batch 54 (group G10d): `cli.py`'s sandbox reaping seams plus the
`--accept-drift` / `--raise-wave-budget` / `--raise-budget` writers.

Scope (per the dispatch brief): `_live_sandbox_names`, `_reap_worktree_manager`,
`_reap_container_sandbox`, `_reap_orphan_worktrees`, `_reap_orphan_containers`,
`_validate_accept_drift`, `_record_drift_findings`, `_raise_wave_ceiling`, `_LedgerRow`,
`_raise_run_ceiling` — `_refuse_bad_raise_budget` is out of scope (already proven).

Method, following the precedent at `tests/test_cli_resume_report_lines_batch51.py`: every
function here already has SOME reaching test in `tests/test_cli.py` that drives it through a
real `fleet resume` invocation, but a full-CLI invocation's cost makes exhausting every branch of
every one of these 10 targets through that route alone impractical, and several branches are
genuinely never asserted anywhere (see each test's docstring for the specific `grep`-backed gap).
Each test below either drives the gap through `fleet resume` directly (when the gap is in
end-to-end behaviour an operator would see) or calls the target function in isolation against a
bare `sqlite3`/`aiosqlite` database built with `tests.test_cli`'s own `fresh_db`/`seed_run`/
`write_config` helpers (when the gap is internal plumbing three other functions also share) —
whichever isolates the branch under test from the other nine targets' behaviour.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import aiosqlite
import pytest
from typer.testing import CliRunner

from fleet.cli import (
    ExitCode,
    UsageError,
    _iso,
    _LedgerRow,
    _live_sandbox_names,
    _now,
    _raise_run_ceiling,
    _raise_wave_ceiling,
    _reap_container_sandbox,
    _record_drift_findings,
    _validate_accept_drift,
    app,
)
from fleet.sandbox.container import ContainerSandbox
from fleet.util.proc import ProcResult
from fleet.util.proc import run as real_docker_runner
from tests.test_cli import FLEET_YAML, RUN_ID, base_args, fresh_db, seed_run, write_config

runner = CliRunner()

STAMP = "2026-08-08T12:00:00+00:00"


@pytest.fixture
def resume_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """The default single-run workspace `tests.test_cli`'s own `workspace` fixture builds, kept
    local rather than imported so this file's fixture graph does not depend on another test
    module's fixture-discovery quirks (matching the precedent at
    `tests/test_resume_unblocking.py`'s own `fleet` fixture).

    Deliberately keeps the SHIPPED `FLEET_YAML`, whose `run.monorepo_path` (`../acme-monorepo`)
    does not exist on disk — that is exactly the state
    `test_resume_reports_the_worktree_sweep_as_skipped_when_the_monorepo_is_absent` below needs.
    """
    from fleet.settings import FleetSettings

    write_config(tmp_path, fleet=FLEET_YAML)
    settings = FleetSettings.load(tmp_path / "config")
    fresh_db(tmp_path / "state" / "fleet.db")
    seed_run(
        tmp_path / "state" / "fleet.db",
        config_digests=json.dumps(dict(settings.section_digests), sort_keys=True),
    )
    monkeypatch.chdir(tmp_path)
    yield tmp_path


@pytest.fixture(autouse=True)
def _no_test_here_may_reach_the_host_docker_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same hazard, same default, as `tests/test_cli.py`'s identically-named autouse fixture:
    `fleet resume` sweeps containers, and a test that does not care about that sweep must not
    reach whatever docker daemon the developer or CI runner happens to have up. Tests that DO
    care override this with their own `monkeypatch.setattr` inside the test body.
    """
    monkeypatch.setattr(
        "fleet.cli._reap_container_sandbox", lambda: ContainerSandbox(runner=_EmptyDocker())
    )


def _proc(argv: Sequence[str], exit_code: int, stdout: str = "") -> ProcResult:
    return ProcResult(
        argv=tuple(argv), exit_code=exit_code, stdout_tail=stdout, stderr_tail="",
        duration_ms=1, timed_out=False, started=True,
    )


class _EmptyDocker:
    """`docker ps` reporting nothing, `docker rm` never called — the harmless default."""

    async def __call__(self, argv: Sequence[str], **_kw: Any) -> ProcResult:
        args = list(argv)
        if args[1:3] == ["ps", "--all"]:
            return _proc(args, 0, "")
        raise AssertionError(f"unexpected docker invocation in this fixture: {args}")


class _MissingDockerBinary:
    """The runner never settles at all: a host with no `docker` on PATH, D38's own shape."""

    async def __call__(self, argv: Sequence[str], **_kw: Any) -> ProcResult:
        raise FileNotFoundError(f"[Errno 2] No such file or directory: {argv[0]!r}")


# ======================================================================================
# `_reap_orphan_worktrees` — the "no git repository at ..." skip branch
# ======================================================================================


def test_resume_reports_the_worktree_sweep_as_skipped_when_the_monorepo_is_absent(
    resume_workspace: Path,
) -> None:
    """The shipped `FLEET_YAML` fixture's `run.monorepo_path` (`../acme-monorepo`) does not exist,
    so THIS branch — not the reap-or-fail branches below it — is the one every non-`_reap_workspace`
    test in `tests/test_cli.py` fires on every single time it invokes `fleet resume`. It fires
    constantly and, per a `grep` for `reaped_worktrees` and `"skipped"` in that whole file, is
    never once asserted on: no test reads `payload["reaped_worktrees"]["skipped"]` or
    `["namespace"]` at all.

    Why that gap matters on its own terms (not just "no assertion"): the function's docstring
    says this branch exists so "an operator whose `run.monorepo_path` is mistyped must see that
    step 2 found nothing because it had nowhere to look, not because the run was clean" — and a
    mutation that silently turned this early return into an attempt to construct a real
    `WorktreeManager` against a nonexistent directory would still exit 0 (the surrounding sweep
    swallows `WorktreeError`), just with `"error"` populated and `"skipped"` gone. Every existing
    assertion on `reaped_worktrees` only ever runs against `_reap_workspace`'s REAL git repo, so
    none of them could see that swap.
    """
    result = runner.invoke(
        app, [*base_args(resume_workspace), "--json", "resume", "--no-continue"]
    )
    assert result.exit_code == ExitCode.SUCCESS, result.output
    reaped = json.loads(result.stdout)["reaped_worktrees"]

    assert reaped["reaped"] == []
    assert reaped["failed"] == []
    assert reaped["error"] is None, "a missing monorepo was read as an attempted-and-failed sweep"
    assert reaped["skipped"] is not None
    assert "no git repository at" in reaped["skipped"]
    assert "acme-monorepo" in reaped["skipped"]
    assert "run.monorepo_path" in reaped["skipped"], (
        "the operator needs the CONFIG KEY name, not just the path, to know what to fix"
    )
    assert reaped["namespace"] == f"fleet-{RUN_ID}-* registered in " + str(
        (resume_workspace / ".." / "acme-monorepo").resolve()
    )


# ======================================================================================
# `_reap_orphan_containers` — the `OSError` branch (docker never invoked at all)
# ======================================================================================


def test_resume_reports_docker_never_invoked_when_the_binary_is_missing(
    resume_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D38's own shape, one layer up: every existing `--dry-run`/non-preview container test in
    `tests/test_cli.py` scripts a docker that STARTS and then either succeeds or exits non-zero
    (`ps_fails`, `rm_fails`) — none of them scripts a runner whose subprocess spawn itself raises
    `OSError` (`FileNotFoundError`, no `docker` on PATH), so `_reap_orphan_containers`'s own
    `except OSError as exc:` clause — the one that tells an operator "docker was never invoked" as
    opposed to "docker ran and refused" — has never actually fired in this suite.

    `ContainerSandbox.list_with_verdict`'s spawn is deliberately unguarded (its docstring: the
    `OSError` guard belongs at the call site with something to say about it), so this reaches all
    the way out of `sandbox.reap()` into `cli.py`'s catch — exercising the real call chain, not a
    stand-in for it.
    """
    monkeypatch.setattr(
        "fleet.cli._reap_container_sandbox", lambda: ContainerSandbox(runner=_MissingDockerBinary())
    )
    result = runner.invoke(
        app, [*base_args(resume_workspace), "--json", "resume", "--no-continue"]
    )
    assert result.exit_code == ExitCode.SUCCESS, result.output
    reaped = json.loads(result.stdout)["reaped_containers"]

    assert reaped["reaped"] == []
    assert reaped["failed"] == []
    assert reaped["skipped"] is None, "an environment fault is not the same fact as 'nothing here'"
    assert reaped["error"] is not None
    assert "docker was never invoked" in reaped["error"]
    assert "FileNotFoundError" in reaped["error"], "docker's own absence must be named, not hidden"


# ======================================================================================
# `_reap_container_sandbox` — the real (never-monkeypatched) seam body
# ======================================================================================


def test_reap_container_sandbox_seam_constructs_a_real_docker_backed_sandbox() -> None:
    """Every single test in `tests/test_cli.py` (and every test above in this file) monkeypatches
    `fleet.cli._reap_container_sandbox` away via an autouse fixture, precisely so no test reaches
    a real docker daemon. That means the seam's OWN body — `return ContainerSandbox()`, wiring the
    REAL subprocess runner and the literal `"docker"` binary name — has never once executed
    anywhere in this suite; a mutation that swapped in the wrong binary name or a no-op runner
    would pass every existing test.
    """
    sandbox = _reap_container_sandbox()
    assert isinstance(sandbox, ContainerSandbox)
    assert sandbox._docker == "docker"
    assert sandbox._runner is real_docker_runner, (
        "the seam must wire the REAL subprocess runner, not a stand-in with no daemon behind it"
    )


# ======================================================================================
# `_live_sandbox_names` — direct call, two repos in one run, one row from another run
# ======================================================================================


def _put_running_phase(
    db: Path, run_id: str, repo: str, *, heartbeat_at: str, attempts: int
) -> None:
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO phases (run_id, repo_id, phase, status, attempts, heartbeat_at, "
            "                    heartbeat_ttl_seconds, lease_owner, lease_fence, "
            "                    lease_expires_at, updated_at) "
            "VALUES (?, ?, 2, 'RUNNING', ?, ?, 300, 'host:cid:1:boot', 4, ?, ?)",
            (run_id, repo, attempts, heartbeat_at, heartbeat_at, STAMP),
        )
    finally:
        conn.close()


async def test_live_sandbox_names_unions_both_rungs_across_every_repo_and_excludes_other_runs(
    tmp_path: Path,
) -> None:
    """Every existing e2e reap test (`tests/test_cli.py`) seeds exactly ONE live `phases` row, so
    the set comprehension's behaviour across MULTIPLE repos in the same run is asserted nowhere:
    a mutation that read only `rows[0]` (or otherwise collapsed the loop) would still satisfy
    every one of them. This also drives the `WHERE run_id = ?` half of the query directly — a
    second run's live row, seeded here, must not leak into this run's spared set.
    """
    other_run = "22222222-2222-4222-8222-222222222222"
    db = fresh_db(tmp_path / "state" / "fleet.db")
    seed_run(db, repos=("acme-commons", "acme-billing"))
    seed_run(db, run_id=other_run, repos=("acme-other",))

    now = _now()
    fresh = _iso(now)
    _put_running_phase(db, RUN_ID, "acme-commons", heartbeat_at=fresh, attempts=2)
    _put_running_phase(db, RUN_ID, "acme-billing", heartbeat_at=fresh, attempts=0)
    _put_running_phase(db, other_run, "acme-other", heartbeat_at=fresh, attempts=5)

    from datetime import timedelta

    horizons = (_iso(now - timedelta(seconds=300)), fresh)
    async with aiosqlite.connect(db) as conn:
        live = await _live_sandbox_names(conn, RUN_ID, horizons)

    assert live == {
        "fleet-11111111-1111-4111-8111-111111111111-acme-commons-2",
        "fleet-11111111-1111-4111-8111-111111111111-acme-commons-3",
        "fleet-11111111-1111-4111-8111-111111111111-acme-billing-0",
        "fleet-11111111-1111-4111-8111-111111111111-acme-billing-1",
    }


# ======================================================================================
# `_validate_accept_drift` — a section named twice
# ======================================================================================


def test_validate_accept_drift_deduplicates_a_section_named_twice(tmp_path: Path) -> None:
    """`--accept-drift budgets --accept-drift budgets` (a plausible fat-fingered retry, or a
    script that composes the flag list without checking for repeats) must accept `budgets` ONCE.
    `_record_drift_findings` runs the sections through `executemany` and relies on this tuple
    already being duplicate-free (its own `ON CONFLICT` upsert would hide a duplicate silently);
    no existing test ever calls `_validate_accept_drift`/`--accept-drift` with a repeated name, so
    the `dict.fromkeys` dedup is currently asserted nowhere.
    """
    from fleet.settings import FleetSettings

    write_config(tmp_path)
    settings = FleetSettings.load(tmp_path / "config")

    accepted = _validate_accept_drift(settings, ["budgets", "gc", "budgets"])

    assert accepted == ("budgets", "gc"), "a repeated section must not appear twice, nor reorder"


# ======================================================================================
# `_record_drift_findings` — the `runs` row it persists, not just the `findings` it writes
# ======================================================================================


async def test_record_drift_findings_persists_the_new_baseline_onto_the_runs_row(
    tmp_path: Path,
) -> None:
    """`tests/test_cli.py`'s `test_accepted_drift_writes_one_config_drift_finding_per_section`
    (the only test that reaches this function) asserts only the `findings` rows it writes, never
    the `UPDATE runs SET config_digests = ?, config_sha256 = ?` half in the same unit. That half
    is what makes an accepted drift STAY accepted: without it, the next `fleet resume` re-reads
    `runs.config_digests` unchanged, `settings.drifted_sections(baseline)` reports the very
    section the operator just accepted as drifted all over again, and `--accept-drift` never
    actually advances the baseline it exists to advance.
    """
    from fleet.settings import FleetSettings

    write_config(tmp_path)
    settings = FleetSettings.load(tmp_path / "config")
    db = fresh_db(tmp_path / "state" / "fleet.db")
    seed_run(db, config_digests="{}")

    await _record_drift_findings(db, RUN_ID, ("budgets", "gc"), settings)

    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT config_digests, config_sha256 FROM runs WHERE run_id = ?", (RUN_ID,)
        ).fetchone()
    finally:
        conn.close()
    assert json.loads(row[0]) == dict(settings.section_digests), (
        "the runs row was not advanced to today's digests, so the next resume re-reports drift"
    )
    assert row[1] == settings.config_sha256()


# ======================================================================================
# `_raise_wave_ceiling` — scoped to the NAMED wave only, among several
# ======================================================================================


def _put_wave(db: Path, run_id: str, wave_index: int, max_usd: float) -> None:
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO waves (run_id, wave_index, computed_at, max_usd, synthetic) "
            "VALUES (?, ?, ?, ?, 0)",
            (run_id, wave_index, STAMP, max_usd),
        )
    finally:
        conn.close()


async def test_raise_wave_ceiling_updates_only_the_named_wave(tmp_path: Path) -> None:
    """`tests/test_cli.py`'s only reaching test (`test_raise_wave_budget_clears_the_halt_and_is_
    audited`) seeds a SINGLE wave (index 0), so `UPDATE waves SET max_usd = ? WHERE run_id = ? AND
    wave_index = ?`'s `wave_index` scoping is unverified — a mutation that dropped it (or that
    always targeted wave 0) would still pass that test. Here two waves exist and only wave 1 is
    raised.
    """
    db = fresh_db(tmp_path / "state" / "fleet.db")
    seed_run(db)
    _put_wave(db, RUN_ID, 0, 10.0)
    _put_wave(db, RUN_ID, 1, 20.0)

    await _raise_wave_ceiling(db, RUN_ID, 1, 75.0)

    conn = sqlite3.connect(db)
    try:
        ceilings = dict(
            conn.execute(
                "SELECT wave_index, max_usd FROM waves WHERE run_id = ? ORDER BY wave_index",
                (RUN_ID,),
            ).fetchall()
        )
        payloads = [
            json.loads(r[0])
            for r in conn.execute("SELECT payload FROM findings WHERE kind = 'WaveBudgetRaised'")
        ]
    finally:
        conn.close()

    assert ceilings == {0: 10.0, 1: 75.0}, "wave 0's ceiling moved when only wave 1 was raised"
    assert payloads == [{"wave_index": 1, "new_max_usd": 75.0}]


# ======================================================================================
# `_LedgerRow` — `payload()`'s full field set and `committed_usd`'s arithmetic
# ======================================================================================


def test_ledger_row_payload_names_all_four_fields_and_committed_usd_sums_spent_and_reserved() -> (
    None
):
    """`payload()` feeds `resume --json`'s `budget_ledger_before` key, and the only existing
    assertion on it (`tests/test_cli.py::test_dry_run_reports_the_budget_raise_as_not_applied`)
    checks a single field, `["halted"]`. A mutation that swapped `spent_usd`/`reserved_usd` in the
    returned dict, or that changed `committed_usd` from a sum to one of its two addends, would
    still pass that test.
    """
    row = _LedgerRow(spent_usd=12.5, reserved_usd=3.25, max_usd=50.0, halted=True)

    assert row.committed_usd == pytest.approx(15.75)
    assert row.payload() == {
        "spent_usd": 12.5,
        "reserved_usd": 3.25,
        "max_usd": 50.0,
        "halted": True,
    }


# ======================================================================================
# `_raise_run_ceiling` — the CAS-lost-the-race branch: refuse, don't fabricate success
# ======================================================================================


async def test_raise_run_ceiling_refuses_rather_than_fabricate_success_when_the_ledger_moved(
    tmp_path: Path,
) -> None:
    """No existing test ever makes `UPDATE budget_ledger SET max_usd = ?, halted = 0, ... WHERE
    run_id = ? AND spent_usd + reserved_usd <= ? AND max_usd <= ?` match zero rows — every
    `--raise-budget` test in `tests/test_cli.py` raises a ledger nobody else has touched, so the
    CAS always succeeds first try and `if int(cursor.rowcount) != 1: return False` (plus the
    re-read-and-report fallback below it) has never actually executed.

    The docstring on `_raise_run_ceiling` names the exact scenario this drives: "another process
    spent, reserved or raised in the gap" between the read-only validation an operator's
    `--raise-budget` figure was checked against and this write. Simulated here by another process
    ALREADY raising `max_usd` to $20 (a legal ledger state — `budget_ledger` CHECKs `spent_usd +
    reserved_usd <= max_usd`, so the race must itself be a legal ledger transition, not an
    illegal one no code path could ever produce) between seeding the ledger and calling this
    function with a STALE `--raise-budget 12` — a figure that was fine against the ORIGINAL $10
    ceiling but would now silently LOWER a ceiling someone else already raised further. The
    function must refuse with the FRESH number rather than clobbering the concurrent raise.
    """
    db = fresh_db(tmp_path / "state" / "fleet.db")
    seed_run(db)
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO budget_ledger (run_id, spent_usd, reserved_usd, max_usd, halted, "
            "                           updated_at) VALUES (?, 5.0, 0.0, 10.0, 1, ?)",
            (RUN_ID, STAMP),
        )
    finally:
        conn.close()

    # The race: another process raises the ceiling to $20 (and clears the halt) after the
    # (simulated) earlier validation approved a stale $12 figure against the original $10.
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        conn.execute(
            "UPDATE budget_ledger SET max_usd = 20.0, halted = 0 WHERE run_id = ?", (RUN_ID,)
        )
    finally:
        conn.close()

    with pytest.raises(UsageError, match=r"20\.00"):
        await _raise_run_ceiling(db, RUN_ID, 12.0)

    conn = sqlite3.connect(db)
    try:
        ledger = conn.execute(
            "SELECT max_usd, halted FROM budget_ledger WHERE run_id = ?", (RUN_ID,)
        ).fetchone()
        raised = conn.execute(
            "SELECT COUNT(*) FROM findings WHERE kind = 'RunBudgetRaised'"
        ).fetchone()[0]
    finally:
        conn.close()

    assert ledger == (20.0, 0), (
        "the stale $12 CAS clobbered the concurrent process's $20 raise instead of refusing"
    )
    assert raised == 0, "an audited raise was recorded for a write that never actually happened"
