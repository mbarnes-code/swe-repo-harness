"""`cli._eligible_contract_units` — round VIII, §15.1 item 3, Wave 7.0 Batch 27.

`_eligible_contract_units` (`src/fleet/cli.py`) is the RUN's Phase 3 CONTRACT domain: every
wave-member contract whose `contracts.status` is `HOISTED` or `MIGRATED`. It feeds BOTH
`_build_impl`'s PASS 0 (hoisted-contract-content ingest, before any repo ingest) and PASS 2b
(read-only `BuildPlan`s for those same contracts).

`tests/test_build_e2e.py`'s own hoist-ingest fixture family
(`test_a_hoisted_contracts_content_is_really_merged_with_the_trailer` and its two neighbours)
already proves the INCLUSION half — a real `HOISTED` contract really ingests and really merges —
but every one of those tests uses a fixture with exactly one contract, always eligible, so none of
them can distinguish "every wave-member contract is eligible" from "only HOISTED/MIGRATED
wave-member contracts are eligible". This file adds that missing EXCLUSION case: a contract whose
status has moved to `FAILED` (§3.1 6c-H's own rollback outcome, `ContractStatus.FAILED` —
"hoist attempted and rolled back") must never be re-ingested by a later `fleet build`.

Reuses `_make_hoist_ingest_workspace` verbatim (CLAUDE.md Rule 8: reuse, don't re-author a second
hoist fixture) — the same `CYCLE_FLEET`/`PROTO_ID` fixture the three existing hoist tests already
use — and stamps the contract `FAILED` by direct SQL before the first `fleet build`, the same
technique `tests/test_build_e2e.py::test_a_degraded_repo_with_no_rhi_repo_exits_7` uses to isolate
one status value from how a repo comes to hold it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fleet import cli
from tests.test_build_e2e import (
    FakeBazel,
    FakeResolver,
    _make_hoist_ingest_workspace,
    build,
    make_monorepo,
    query,
    scan,
    sequence,
    transform,
)
from tests.test_transform_e2e import git
from tests.test_workers_contracts import PROTO_ID


def _stamp_contract_status(workspace: Path, *, contract_id: str, status: str) -> None:
    import sqlite3

    conn = sqlite3.connect(workspace / "state" / "fleet.db", isolation_level=None)
    try:
        conn.execute("UPDATE contracts SET status = ? WHERE contract_id = ?", (status, contract_id))
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM contracts WHERE contract_id = ? AND status = ?",
                (contract_id, status),
            ).fetchone()[0]
            == 1
        ), "the fixture would prove nothing if the stamp did not land"
    finally:
        conn.close()


def test_a_failed_contract_is_never_re_ingested_by_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A contract whose status is `FAILED` (§3.1 6c-H's rollback outcome) is OUT of
    `_eligible_contract_units`'s domain — `fleet build` must ingest it, merge it or plan a
    `BuildPlan` for it exactly as if it had never hoisted at all.

    Reddens under widening `_eligible_contract_units`'s status filter (e.g. `ContractStatus.
    HOISTED, ContractStatus.MIGRATED` → also accepting `FAILED`, or dropping the filter entirely)
    — the contract would then hit `_ingest_contract_source` again, and this fixture's `identity.
    proto` is still on disk unchanged from the earlier hoist, so a real re-ingest produces a
    SECOND `Hoisted-Contract: <id>` merge commit on `integration`, which this test refuses.
    """
    workspace = _make_hoist_ingest_workspace(tmp_path)
    monkeypatch.chdir(workspace)
    assert scan(workspace).exit_code == cli.ExitCode.SUCCESS
    assert sequence(workspace).exit_code == cli.ExitCode.SUCCESS

    # -- anti-vacuity: the fixture really hoisted BEFORE the stamp, exactly like the sibling ---
    # -- tests in test_build_e2e.py -------------------------------------------------------------
    assert query(
        workspace,
        "SELECT status, hoist_target_path FROM contracts WHERE contract_id = ?",
        (PROTO_ID,),
    ) == [("HOISTED", "proto/acme/identity/v1")], "the fixture must really hoist first"

    assert transform(workspace).exit_code == cli.ExitCode.SUCCESS

    # -- isolate ONE status value, the same technique test_build_e2e.py's own
    # -- test_a_degraded_repo_with_no_rhi_repo_exits_7 uses: hand-write the row directly so the
    # -- assertion is about what `_eligible_contract_units` does with a FAILED contract, not
    # -- about how a contract comes to hold that status (a real rollback is proven elsewhere,
    # -- tests/test_build_e2e.py::test_a_hoist_rollback_targets_the_real_contract_merge...).
    _stamp_contract_status(workspace, contract_id=PROTO_ID, status="FAILED")

    monorepo = make_monorepo(workspace)
    monkeypatch.setattr(cli, "RESOLVER_RUNNER", FakeResolver())
    monkeypatch.setattr(cli, "BAZEL_RUNNER", FakeBazel(workspace / "artifacts" / "fake-bazel"))
    assert cli.FILTER_REPO_RUNNER is None, "the seam must be absent for this test to mean anything"

    result = build(workspace, "--no-sandbox")
    assert result.exit_code == cli.ExitCode.SUCCESS, result.output

    # -- the FAILED contract was never re-ingested: no second (or first, post-stamp) hoist ------
    # -- merge for it, and no ContractIngestFailed finding either (it must be SKIPPED, not -------
    # -- attempted-and-failed) -------------------------------------------------------------------
    assert (
        query(workspace, "SELECT payload FROM findings WHERE kind = 'ContractIngestFailed'") == []
    ), "a FAILED contract must be skipped, not attempted"
    hoist_shas = (
        git(
            monorepo,
            "log",
            "integration",
            "--format=%H",
            "--grep=^Hoisted-Contract: " + PROTO_ID + "$",
        )
        .strip()
        .splitlines()
    )
    assert hoist_shas == [], (
        f"a FAILED contract must never land a hoist merge on `integration`: {hoist_shas}"
    )
    # And the status is untouched: `_eligible_contract_units` is read-only over `contracts`.
    assert query(workspace, "SELECT status FROM contracts WHERE contract_id = ?", (PROTO_ID,)) == [
        ("FAILED",)
    ]
