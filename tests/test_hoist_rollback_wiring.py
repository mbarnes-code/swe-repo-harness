"""Round VI task 72 — §12.31/D111 Leg D, the production wiring (ADR-0122 Decision 7) that
connects a `HoistBrokeOwner` finding (Leg C2, `_BuildSink`, round VI task 66) to
`cli.unhoist_contract` (slice 1, task 65) and `cli.execute_hoist_rollback` (slice 2, task 71).
Neither of those two functions had ANY production call site before this task; `cli.
_reconcile_hoist_rollbacks` is that call site, wired into `_build_impl` right after its wave loop
finishes (`fleet build` and `fleet resume`'s Phase 3 continuation both funnel through
`_build_impl`, so the check lives there once).

Two layers, deliberately kept separate:

1. **Unit-level, fast** (`unhoist_contract`/`execute_hoist_rollback` both monkeypatched to
   lightweight fakes): proves `_reconcile_hoist_rollbacks`'s OWN logic — extraction/dedup of
   contract ids from `HoistBrokeOwner` findings, the REFUSED-skips-the-second-call branch, the
   APPLIED-calls-it-with-the-blast-set branch, and sequential multi-contract processing — without
   paying for real git/bazel on every case. This is what makes Rule 12 mutation testing of this
   task's own new code affordable.
2. **Real end to end** (mirrors `tests/test_build_e2e.py`'s
   `test_a_real_build_failure_naming_a_hoisted_contracts_package_is_attributed_and_terminal`, task
   66's own e2e proof): a real `fleet scan`/`sequence`/`transform`/`build` over real git
   repositories, a real `bazel` failure seam tripping a real `HoistBrokeOwner` finding, and this
   task's new wiring running as part of that SAME `fleet build` invocation — proving the call
   site, not just the function it calls.

**Why the fake `Forge` in the e2e tests resolves merge shas LAZILY, at `view()` time, by reading
real git history rather than a value fixed at seed time.** `_build_impl`'s PASS 1 merges every
eligible repo onto `integration` (`_ingest_build_source`) BEFORE the wave loop dispatches `bazel
build` and before this task's new wiring pass runs at the very end — all inside ONE `fleet build`
call. So the merge sha a hand-seeded `PullRequestDraft` needs to point at does not exist yet at
the moment the row is written (before `build()` is even called); it exists by the time
`execute_hoist_rollback`'s `forge.view()` actually asks, because that call happens after PASS 1 of
the SAME invocation. `_RealMergeForge` below defers the lookup to exactly that moment.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import subprocess
import uuid as _uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fleet import cli
from fleet.cli import (
    PR_RECORD_KIND,
    HoistRollbackOutcome,
    UnhoistOutcome,
    _hoist_broke_contract_ids,
    _reconcile_hoist_rollbacks,
)
from fleet.llm.roles import SPEC_ROLE_TIERS
from fleet.models.enums import PrState
from fleet.models.tasks import PullRequestDraft
from fleet.settings import FleetSettings
from fleet.state.db import StateWriter, connect_ro, initialize_database
from fleet.vcs.forge import PrStatus
from tests.test_build_e2e import (  # noqa: F401  (fixtures, used by injection)
    DESTINATIONS,
    _bazel_seam_failing_one_dest,
    build,
    filter_repo,
    fleet,
    gazelle,
    monorepo,
    resolver,
    transformed,
)
from tests.test_transform_e2e import query as e2e_query

RUN_ID = "44444444-4444-4444-8444-444444444444"
OTHER_RUN_ID = "55555555-5555-4555-8555-555555555555"
NOW = datetime(2026, 9, 8, 12, 0, 0, tzinfo=UTC)

# -- minimal config, copied from tests/test_unhoist_rollback.py's own recipe -------------------
_ROLES_BLOCK = "".join(
    f"  {role.value}: {tier.value}\n" for role, tier in sorted(SPEC_ROLE_TIERS.items())
)
_MODELS_YAML = (
    "version: 2\nroles:\n" + _ROLES_BLOCK + "default_profile: default\nprofiles:\n  default:\n"
    "    HEAVY:\n"
    "      - { backend: anthropic, model_id: claude-opus-5, effort: high,\n"
    "          api_key_env: ANTHROPIC_API_KEY,\n"
    "          price: { in_per_mtok: 5.0, out_per_mtok: 25.0 } }\n"
    "    WORKHORSE:\n"
    "      - { backend: anthropic, model_id: claude-sonnet-5, effort: high,\n"
    "          api_key_env: ANTHROPIC_API_KEY,\n"
    "          price: { in_per_mtok: 3.0, out_per_mtok: 15.0 } }\n"
    "    CHEAP:\n"
    "      - { backend: anthropic, model_id: claude-haiku-4-5,\n"
    "          api_key_env: ANTHROPIC_API_KEY,\n"
    "          price: { in_per_mtok: 1.0, out_per_mtok: 5.0 } }\n"
)
_REPOS_YAML = (
    "version: 1\ndefaults:\n  ref: main\nrepos:\n"
    "  - name: acme-commons\n    url: https://github.com/acme/acme-commons\n"
)
_FLEET_YAML = (
    "run:\n  monorepo_path: ../acme-monorepo\n"
    "preflight:\n  min_free_bytes: 1048576\n"
    "concurrency:\n  docker: 1\n"
    "verify:\n  container_memory: 64m\n"
    "budgets:\n  max_rss_mb: 512\n"
)


def _settings(tmp_path: Path) -> FleetSettings:
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "fleet.yaml").write_text(_FLEET_YAML, encoding="utf-8")
    (config_dir / "models.yaml").write_text(_MODELS_YAML, encoding="utf-8")
    (config_dir / "repos.yaml").write_text(_REPOS_YAML, encoding="utf-8")
    (config_dir / "rules").mkdir(parents=True, exist_ok=True)
    return FleetSettings.load(config_dir)


@pytest.fixture
async def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "state" / "fleet.db"
    await initialize_database(path)
    return path


def _seed_run_and_repo(db_path: Path, *, run_id: str, repo_ids: tuple[str, ...]) -> None:
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO runs (run_id, started_at, config_sha256, config_digests, "
            "                  harness_version) VALUES (?, ?, ?, '{}', ?)",
            (run_id, "2026-09-08T00:00:00+00:00", "a" * 64, "0.1.0"),
        )
        for repo_id in repo_ids:
            conn.execute(
                "INSERT INTO repos (repo_id, name, url, updated_at) VALUES (?, ?, ?, ?)",
                (
                    repo_id,
                    repo_id,
                    f"https://example.invalid/{repo_id}",
                    "2026-09-08T00:00:00+00:00",
                ),
            )
    finally:
        conn.close()


def _seed_hoist_broke_finding(
    db_path: Path, *, run_id: str, repo_id: str, contract_id: str, salt: str = ""
) -> None:
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO findings (run_id, repo_id, kind, severity, fingerprint, payload, "
            "                      created_at) VALUES (?, ?, 'HoistBrokeOwner', 'error', ?, ?, ?)",
            (
                run_id,
                repo_id,
                f"hoistbroke:{repo_id}{salt}",
                json.dumps(
                    {
                        "contract_id": contract_id,
                        "hoist_target_path": "contracts/x",
                        "repo_id": repo_id,
                        "matched_line": "no such target",
                    }
                ),
                "2026-09-08T00:00:00+00:00",
            ),
        )
    finally:
        conn.close()


# --------------------------------------------------------------------------------------
# Layer 1: `_hoist_broke_contract_ids` — the raw extraction/dedup query.
# --------------------------------------------------------------------------------------


async def test_hoist_broke_contract_ids_dedupes_and_sorts_and_scopes_by_run(db_path: Path) -> None:
    """Two `HoistBrokeOwner` findings can name the SAME contract (a contract owned by one repo
    that also breaks a different repo's build) — the result must be the distinct set, sorted, and
    must never leak a finding from a different run_id."""
    _seed_run_and_repo(db_path, run_id=RUN_ID, repo_ids=("r1", "r2", "r3"))
    _seed_run_and_repo(db_path, run_id=OTHER_RUN_ID, repo_ids=("r4",))
    _seed_hoist_broke_finding(db_path, run_id=RUN_ID, repo_id="r1", contract_id="proto:b")
    _seed_hoist_broke_finding(db_path, run_id=RUN_ID, repo_id="r2", contract_id="proto:a")
    _seed_hoist_broke_finding(
        db_path, run_id=RUN_ID, repo_id="r3", contract_id="proto:b", salt=":dup"
    )
    _seed_hoist_broke_finding(db_path, run_id=OTHER_RUN_ID, repo_id="r4", contract_id="proto:z")

    read_conn = await connect_ro(db_path)
    try:
        result = await _hoist_broke_contract_ids(read_conn, RUN_ID)
    finally:
        await read_conn.close()

    assert result == ("proto:a", "proto:b")


async def test_hoist_broke_contract_ids_is_empty_when_nothing_broke(db_path: Path) -> None:
    _seed_run_and_repo(db_path, run_id=RUN_ID, repo_ids=("r1",))
    read_conn = await connect_ro(db_path)
    try:
        result = await _hoist_broke_contract_ids(read_conn, RUN_ID)
    finally:
        await read_conn.close()
    assert result == ()


# --------------------------------------------------------------------------------------
# Layer 1: `_reconcile_hoist_rollbacks`'s own branching, with `unhoist_contract`/
# `execute_hoist_rollback` monkeypatched to lightweight fakes — no real git, no real graph.
# --------------------------------------------------------------------------------------


def _unhoist_stub(by_contract: dict[str, UnhoistOutcome], calls: list[str]):
    async def fake(read_conn, settings, *, writer, run_id, contract_id, now):
        calls.append(contract_id)
        return by_contract[contract_id]

    return fake


def _execute_stub(by_contract: dict[str, HoistRollbackOutcome], calls: list[tuple[str, tuple]]):
    async def fake(read_conn, settings, *, run_id, contract_id, blast_set, forge):
        calls.append((contract_id, tuple(blast_set)))
        return by_contract[contract_id]

    return fake


async def test_reconcile_hoist_rollbacks_skips_the_revert_series_on_refused(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_run_and_repo(db_path, run_id=RUN_ID, repo_ids=("owner",))
    _seed_hoist_broke_finding(db_path, run_id=RUN_ID, repo_id="owner", contract_id="proto:demo")
    settings = _settings(tmp_path)

    refused = UnhoistOutcome(
        decision="REFUSED",
        contract_id="proto:demo",
        blast_set=("owner",),
        blocking_repo_ids=("blocker",),
    )
    unhoist_calls: list[str] = []
    execute_calls: list[tuple[str, tuple]] = []
    monkeypatch.setattr(
        cli, "unhoist_contract", _unhoist_stub({"proto:demo": refused}, unhoist_calls)
    )
    monkeypatch.setattr(cli, "execute_hoist_rollback", _execute_stub({}, execute_calls))

    async with StateWriter(db_path, owner="test-reconcile") as writer:
        read_conn = await connect_ro(db_path)
        try:
            entries = await _reconcile_hoist_rollbacks(
                read_conn,
                settings,
                writer=writer,
                run_id=RUN_ID,
                forge=object(),
                now=NOW,  # type: ignore[arg-type]
            )
        finally:
            await read_conn.close()

    assert unhoist_calls == ["proto:demo"]
    assert execute_calls == [], "REFUSED must never reach execute_hoist_rollback"
    assert len(entries) == 1
    assert entries[0].contract_id == "proto:demo"
    assert entries[0].unhoist is refused
    assert entries[0].rollback is None


async def test_reconcile_hoist_rollbacks_calls_the_revert_series_on_applied_with_the_blast_set(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_run_and_repo(db_path, run_id=RUN_ID, repo_ids=("owner",))
    _seed_hoist_broke_finding(db_path, run_id=RUN_ID, repo_id="owner", contract_id="proto:demo")
    settings = _settings(tmp_path)

    applied = UnhoistOutcome(
        decision="APPLIED",
        contract_id="proto:demo",
        blast_set=("owner", "consumer"),
        demoted_repo_ids=("consumer",),
    )
    committed = HoistRollbackOutcome(
        decision="COMMITTED",
        contract_id="proto:demo",
        ordered_shas=("a" * 40,),
        already_reverted_shas=(),
        newly_reverted_shas=("a" * 40,),
    )
    unhoist_calls: list[str] = []
    execute_calls: list[tuple[str, tuple]] = []
    monkeypatch.setattr(
        cli, "unhoist_contract", _unhoist_stub({"proto:demo": applied}, unhoist_calls)
    )
    monkeypatch.setattr(
        cli, "execute_hoist_rollback", _execute_stub({"proto:demo": committed}, execute_calls)
    )

    async with StateWriter(db_path, owner="test-reconcile") as writer:
        read_conn = await connect_ro(db_path)
        try:
            entries = await _reconcile_hoist_rollbacks(
                read_conn,
                settings,
                writer=writer,
                run_id=RUN_ID,
                forge=object(),
                now=NOW,  # type: ignore[arg-type]
            )
        finally:
            await read_conn.close()

    assert unhoist_calls == ["proto:demo"]
    assert execute_calls == [("proto:demo", ("owner", "consumer"))], (
        "execute_hoist_rollback must be called with unhoist's OWN blast_set, unchanged"
    )
    assert len(entries) == 1
    assert entries[0].unhoist is applied
    assert entries[0].rollback is committed


async def test_reconcile_hoist_rollbacks_processes_several_contracts_sequentially_in_order(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two contracts, one REFUSED and one APPLIED, both broke a build this run — both must be
    reconciled, in sorted `contract_id` order, one at a time (never both `unhoist_contract` calls
    before either `execute_hoist_rollback` call, which a concurrent/batched implementation could
    produce)."""
    _seed_run_and_repo(db_path, run_id=RUN_ID, repo_ids=("owner-a", "owner-z"))
    _seed_hoist_broke_finding(db_path, run_id=RUN_ID, repo_id="owner-a", contract_id="proto:a")
    _seed_hoist_broke_finding(db_path, run_id=RUN_ID, repo_id="owner-z", contract_id="proto:z")
    settings = _settings(tmp_path)

    outcomes = {
        "proto:a": UnhoistOutcome(
            decision="APPLIED", contract_id="proto:a", blast_set=("owner-a",)
        ),
        "proto:z": UnhoistOutcome(
            decision="REFUSED",
            contract_id="proto:z",
            blast_set=("owner-z",),
            blocking_repo_ids=("x",),
        ),
    }
    rollback = HoistRollbackOutcome(
        decision="COMMITTED",
        contract_id="proto:a",
        ordered_shas=(),
        already_reverted_shas=(),
        newly_reverted_shas=(),
    )
    order: list[str] = []

    async def unhoist_fake(read_conn, settings, *, writer, run_id, contract_id, now):
        order.append(f"unhoist:{contract_id}")
        return outcomes[contract_id]

    async def execute_fake(read_conn, settings, *, run_id, contract_id, blast_set, forge):
        order.append(f"execute:{contract_id}")
        return rollback

    monkeypatch.setattr(cli, "unhoist_contract", unhoist_fake)
    monkeypatch.setattr(cli, "execute_hoist_rollback", execute_fake)

    async with StateWriter(db_path, owner="test-reconcile") as writer:
        read_conn = await connect_ro(db_path)
        try:
            entries = await _reconcile_hoist_rollbacks(
                read_conn,
                settings,
                writer=writer,
                run_id=RUN_ID,
                forge=object(),
                now=NOW,  # type: ignore[arg-type]
            )
        finally:
            await read_conn.close()

    assert [e.contract_id for e in entries] == ["proto:a", "proto:z"]
    # sorted order (a before z) AND sequential: "proto:a"'s pair happens before "proto:z" even
    # starts, and "proto:z" (REFUSED) never reaches `execute_fake` at all.
    assert order == ["unhoist:proto:a", "execute:proto:a", "unhoist:proto:z"]


# --------------------------------------------------------------------------------------
# Layer 2: real end to end, through `fleet build` itself — the call site, not just the callees.
# --------------------------------------------------------------------------------------

_CONTRACT_ID = "openapi:acme.shared"
_TARGET_PATH = "contracts/openapi/acme-shared"
_STDERR = (
    "ERROR: /work/ts/acme/app/BUILD.bazel:5:12: no such target "
    f"'//{_TARGET_PATH}:pkg': target 'pkg' not declared in package '{_TARGET_PATH}'\n"
)


class _RealMergeForge:
    """Resolves each PR's merge sha/timestamp by reading the REAL git history `fleet build`'s
    PASS 1 ingest already produced by the time this is called (see module docstring) — never a
    value fixed at seed time, which is what lets a `PullRequestDraft` be hand-seeded BEFORE the
    merge that answers it exists."""

    def __init__(self, monorepo_path: Path, repo_by_url: dict[str, str]) -> None:
        self._monorepo = monorepo_path
        self._repo_by_url = dict(repo_by_url)
        self.calls: list[str] = []

    async def view(self, url: str) -> PrStatus:
        self.calls.append(url)
        repo_id = self._repo_by_url[url]
        result = await asyncio.to_thread(_git_log_source_repo, self._monorepo, repo_id)
        line = result.stdout.strip().splitlines()[0]
        sha, iso = line.split("|", 1)
        return PrStatus(
            url=url,
            state=PrState.MERGED,
            merged_at=datetime.fromisoformat(iso),
            merge_commit_sha=sha,
        )


def _git_log_source_repo(monorepo_path: Path, repo_id: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [  # noqa: S607
            "git",
            "-C",
            str(monorepo_path),
            "log",
            "integration",
            "--format=%H|%cI",
            "--grep",
            f"^Source-Repo: {repo_id}$",
        ],
        capture_output=True,
        text=True,
        check=True,
    )


def _seed_pr(
    db_path: Path, run_id: str, *, repo_id: str, url: str, contract_id: str | None = None
) -> None:
    draft = PullRequestDraft(
        run_id=_uuid.UUID(run_id),
        repo_id=repo_id,
        contract_id=contract_id,
        wave_index=0,
        branch=f"migrate/{repo_id}",
        title=f"[fleet] migrate {repo_id}",
        body="body",
        source_url=f"https://example.invalid/{repo_id}",
        source_sha="a" * 40,
        state=PrState.MERGED,
        url=url,
    )
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO findings (run_id, repo_id, kind, severity, fingerprint, payload, "
            "                      created_at) VALUES (?, ?, ?, 'info', ?, ?, ?)",
            (
                run_id,
                repo_id,
                PR_RECORD_KIND,
                f"pr:{repo_id}",
                draft.model_dump_json(),
                "2026-09-08T00:00:00+00:00",
            ),
        )
    finally:
        conn.close()


def _ekey(*parts: str) -> str:
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


def _seed_contract_and_edges(
    db_path: Path, run_id: str, *, contract_id: str, owner: str, consumer: str, target_path: str
) -> None:
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO contracts (run_id, contract_id, kind, identifier, owning_repo_id, "
            "                       extractable, hoist_target_path, status, detected_at) "
            "VALUES (?, ?, 'OPENAPI', 'acme.shared', ?, 1, ?, 'HOISTED', ?)",
            (run_id, contract_id, owner, target_path, "2026-09-08T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO edges (edge_key, run_id, src_kind, src_id, dst_kind, dst_id, "
            "                   dst_coord_key, kind, base_confidence, confidence, "
            "                   evidence_path, detected_at) "
            "VALUES (?, ?, 'REPO', ?, 'CONTRACT', ?, ?, 'CONTRACT_IMPL', 0.95, 0.95, "
            "        'x.yaml', ?)",
            (
                _ekey("impl", owner, contract_id),
                run_id,
                owner,
                contract_id,
                contract_id,
                "2026-09-08T00:00:00+00:00",
            ),
        )
        conn.execute(
            "INSERT INTO edges (edge_key, run_id, src_kind, src_id, dst_kind, dst_id, "
            "                   dst_coord_key, kind, base_confidence, confidence, "
            "                   evidence_path, detected_at) "
            "VALUES (?, ?, 'REPO', ?, 'CONTRACT', ?, ?, 'CONTRACT_CONSUME', 0.9, 0.9, "
            "        'x.yaml', ?)",
            (
                _ekey("consume", consumer, contract_id),
                run_id,
                consumer,
                contract_id,
                contract_id,
                "2026-09-08T00:00:00+00:00",
            ),
        )
    finally:
        conn.close()


def _phase_row(root: Path, repo_id: str, phase: int) -> tuple[str, int]:
    rows = e2e_query(
        root,
        "SELECT status, attempts FROM phases WHERE repo_id = ? AND phase = ?",
        (repo_id, phase),
    )
    assert len(rows) == 1, rows
    return str(rows[0][0]), int(rows[0][1])


def _integration_tip(monorepo_path: Path) -> str:
    result = subprocess.run(  # noqa: S603
        ["git", "-C", str(monorepo_path), "rev-parse", "integration"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _revert_commits_on_integration(monorepo_path: Path, contract_id: str) -> list[str]:
    result = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "git",
            "-C",
            str(monorepo_path),
            "log",
            "integration",
            "--format=%H",
            "--grep",
            f"^Fleet-Contract-Rollback-Id: {contract_id}$",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.strip().splitlines() if line]


@pytest.mark.integration
def test_a_real_fleet_build_wires_a_real_hoist_rollback_end_to_end(
    fleet: Path,  # noqa: F811
    monorepo: Path,  # noqa: F811
    filter_repo,  # noqa: F811
    resolver,  # noqa: F811
    gazelle,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§12.31 Leg D, production wiring (round VI task 72, ADR-0122 Decision 7): a real
    `fleet build` whose bazel step really fails, naming a real `HoistBrokeOwner` finding, drives
    this task's new call site all the way to a REAL `git revert -m 1` commit landing on
    `integration` — with no explicit call to any of task 65/71/72's functions anywhere in this
    test; `build()` alone is what runs it.

    Owner = `acme-app-ts` (whose build fails); consumer = `acme-lib-py`, chosen specifically
    because it has NO real dependency relationship with the owner (an independent ecosystem in
    this fixture) — so its blast-set membership comes ENTIRELY from the hand-seeded
    `CONTRACT_CONSUME` edge below, isolating what this test is actually proving.
    """
    _ = (filter_repo, resolver, gazelle)
    transformed(fleet)
    owner = "acme-app-ts"
    consumer = "acme-lib-py"
    run_id = str(e2e_query(fleet, "SELECT run_id FROM runs")[0][0])
    db_path = fleet / "state" / "fleet.db"

    consumer_transform_before = _phase_row(fleet, consumer, 2)
    assert consumer_transform_before[0] == "SUCCEEDED"

    _seed_contract_and_edges(
        db_path,
        run_id,
        contract_id=_CONTRACT_ID,
        owner=owner,
        consumer=consumer,
        target_path=_TARGET_PATH,
    )
    owner_url = f"https://forge.invalid/{owner}/pull/1"
    consumer_url = f"https://forge.invalid/{consumer}/pull/1"
    _seed_pr(db_path, run_id, repo_id=owner, url=owner_url, contract_id=_CONTRACT_ID)
    _seed_pr(db_path, run_id, repo_id=consumer, url=consumer_url)

    forge = _RealMergeForge(monorepo, {owner_url: owner, consumer_url: consumer})
    monkeypatch.setattr(cli, "_forge", lambda settings: forge)

    fake_bazel = _bazel_seam_failing_one_dest(
        fleet / "artifacts" / "fake-bazel-task72",
        fail_dest=DESTINATIONS[owner],
        fail_stderr=_STDERR,
    )
    monkeypatch.setattr(cli, "BAZEL_RUNNER", fake_bazel)

    build(fleet, "--no-sandbox")  # exit code not asserted: one repo of the fleet fails on purpose

    assert e2e_query(
        fleet, "SELECT status FROM contracts WHERE contract_id = ?", (_CONTRACT_ID,)
    ) == [("FAILED",)]

    revert_shas = _revert_commits_on_integration(monorepo, _CONTRACT_ID)
    assert len(revert_shas) == 2, (
        "one revert per blast-set entry that was actually MERGED: the contract's own hoist "
        f"(owner) and the consumer -- got {revert_shas}"
    )
    assert set(forge.calls) == {owner_url, consumer_url}

    consumer_transform_after = _phase_row(fleet, consumer, 2)
    assert consumer_transform_after == ("PENDING", consumer_transform_before[1]), (
        "the blast-set consumer's TRANSFORM row is demoted to PENDING with attempts UNCHANGED"
    )

    owner_build_after = _phase_row(fleet, owner, 3)
    assert owner_build_after[0] == "REQUIRES_HUMAN_INTERVENTION", (
        "the owner's own BUILD failure stays terminal -- unaffected by its own rollback demotion "
        "attempt, which `demote_to_floor` short-circuits to a no-op for a repo already carrying "
        "REQUIRES_HUMAN_INTERVENTION (task 65 review I3's `unresolved_repo_ids` path)"
    )

    demotion_findings = e2e_query(
        fleet,
        "SELECT repo_id, payload FROM findings WHERE kind = 'HoistRollbackDemotion' AND run_id = ?",
        (run_id,),
    )
    assert [row[0] for row in demotion_findings] == [consumer]


@pytest.mark.integration
def test_a_hoist_rollback_that_cannot_find_its_anchor_fails_loud_for_one_contract_only(
    fleet: Path,  # noqa: F811
    monorepo: Path,  # noqa: F811
    filter_repo,  # noqa: F811
    resolver,  # noqa: F811
    gazelle,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix round (task-72 controller review I2): the REAL production shape — no `PullRequestDraft`
    anywhere carries `contract_id` (D122; measured, not assumed — no call site in `src/fleet`
    constructs one). Every other test in this module hand-seeds `contract_id` on the owner's own
    PR record, which is what let the original landing's tests all hit the `APPLIED`/`COMMITTED`
    success path and never exercise this one.

    Without that hand-seeding, `_ordered_revert_shas`'s scan for a contract-owned draft finds
    nothing, so `execute_hoist_rollback` raises `RollbackAnchorError`. Before this fix round, that
    exception propagated uncaught out of `_reconcile_hoist_rollbacks` and `_build_impl`, and
    `runner.invoke(..., catch_exceptions=False)` (this suite's own `build()` helper) re-raised it
    straight out of the test — an unrelated repo (`acme-lib-ts`, given NO edge to the contract at
    all, so it is never a blast-set member regardless of what `execute_hoist_rollback` does) would
    never even get its own status reported, because the WHOLE invocation crashed. After this fix,
    `build()` returns normally: `contracts.status` still reads `FAILED` (`unhoist_contract`'s own
    effect, unaffected — it never raises here), no revert commit lands, a `HoistRollbackFailed`
    finding records why, and the unrelated repo's own BUILD phase still reaches `SUCCEEDED`.
    """
    _ = (filter_repo, resolver, gazelle)
    transformed(fleet)
    owner = "acme-app-ts"
    consumer = "acme-lib-py"
    unrelated = "acme-lib-ts"
    run_id = str(e2e_query(fleet, "SELECT run_id FROM runs")[0][0])
    db_path = fleet / "state" / "fleet.db"

    _seed_contract_and_edges(
        db_path,
        run_id,
        contract_id=_CONTRACT_ID,
        owner=owner,
        consumer=consumer,
        target_path=_TARGET_PATH,
    )
    owner_url = f"https://forge.invalid/{owner}/pull/1"
    consumer_url = f"https://forge.invalid/{consumer}/pull/1"
    # The realistic production shape (fix round I2): NO `contract_id` on the owner's own draft.
    _seed_pr(db_path, run_id, repo_id=owner, url=owner_url)
    _seed_pr(db_path, run_id, repo_id=consumer, url=consumer_url)

    forge = _RealMergeForge(monorepo, {owner_url: owner, consumer_url: consumer})
    monkeypatch.setattr(cli, "_forge", lambda settings: forge)

    fake_bazel = _bazel_seam_failing_one_dest(
        fleet / "artifacts" / "fake-bazel-task72-noanchor",
        fail_dest=DESTINATIONS[owner],
        fail_stderr=_STDERR,
    )
    monkeypatch.setattr(cli, "BAZEL_RUNNER", fake_bazel)

    # The discriminator itself: this call must not raise. Pre-fix, `RollbackAnchorError`
    # propagates uncaught through `catch_exceptions=False` and fails this test with a traceback
    # instead of a normal `Result` — see this task's fix-round report for the old-fails proof.
    build(fleet, "--no-sandbox")

    assert e2e_query(
        fleet, "SELECT status FROM contracts WHERE contract_id = ?", (_CONTRACT_ID,)
    ) == [("FAILED",)], "unhoist_contract's own DB/graph write is unaffected by the later raise"

    assert _revert_commits_on_integration(monorepo, _CONTRACT_ID) == [], (
        "no anchor to resume from means no revert commit may land"
    )

    failed_findings = e2e_query(
        fleet,
        "SELECT payload FROM findings WHERE kind = 'HoistRollbackFailed' AND run_id = ?",
        (run_id,),
    )
    assert len(failed_findings) == 1, failed_findings
    failed_payload = json.loads(failed_findings[0][0])
    assert failed_payload["contract_id"] == _CONTRACT_ID
    assert failed_payload["error_type"] == "RollbackAnchorError"

    unrelated_build = _phase_row(fleet, unrelated, 3)
    assert unrelated_build[0] == "SUCCEEDED", (
        f"an UNRELATED repo with no edge to the contract must complete normally -- got "
        f"{unrelated_build}"
    )


@pytest.mark.integration
def test_a_second_fleet_build_invocation_is_idempotent_and_makes_no_new_commits(
    fleet: Path,  # noqa: F811
    monorepo: Path,  # noqa: F811
    filter_repo,  # noqa: F811
    resolver,  # noqa: F811
    gazelle,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulates a second `fleet build`/`fleet resume` invocation over the SAME durable run
    (identical `HoistBrokeOwner` finding still present, contract already `FAILED`, revert already
    committed): re-running the wiring must make no new commits and must not error."""
    transformed(fleet)
    owner = "acme-app-ts"
    consumer = "acme-lib-py"
    run_id = str(e2e_query(fleet, "SELECT run_id FROM runs")[0][0])
    db_path = fleet / "state" / "fleet.db"

    _seed_contract_and_edges(
        db_path,
        run_id,
        contract_id=_CONTRACT_ID,
        owner=owner,
        consumer=consumer,
        target_path=_TARGET_PATH,
    )
    owner_url = f"https://forge.invalid/{owner}/pull/1"
    consumer_url = f"https://forge.invalid/{consumer}/pull/1"
    _seed_pr(db_path, run_id, repo_id=owner, url=owner_url, contract_id=_CONTRACT_ID)
    _seed_pr(db_path, run_id, repo_id=consumer, url=consumer_url)

    forge = _RealMergeForge(monorepo, {owner_url: owner, consumer_url: consumer})
    monkeypatch.setattr(cli, "_forge", lambda settings: forge)
    fake_bazel = _bazel_seam_failing_one_dest(
        fleet / "artifacts" / "fake-bazel-task72-idem",
        fail_dest=DESTINATIONS[owner],
        fail_stderr=_STDERR,
    )
    monkeypatch.setattr(cli, "BAZEL_RUNNER", fake_bazel)

    build(fleet, "--no-sandbox")
    tip_after_first = _integration_tip(monorepo)
    revert_shas_after_first = _revert_commits_on_integration(monorepo, _CONTRACT_ID)
    assert len(revert_shas_after_first) == 2

    # A second invocation over the identical run — no CLI exception (`catch_exceptions=False` in
    # `build()` means a raised exception fails this test directly, which is the "does not error"
    # proof), no new commit on `integration`, and the same two revert commits (nothing duplicated).
    build(fleet, "--no-sandbox")
    tip_after_second = _integration_tip(monorepo)
    revert_shas_after_second = _revert_commits_on_integration(monorepo, _CONTRACT_ID)

    assert tip_after_second == tip_after_first, "a second invocation must commit NOTHING new"
    assert sorted(revert_shas_after_second) == sorted(revert_shas_after_first)


@pytest.mark.integration
def test_a_real_fleet_build_refuses_the_rollback_and_never_calls_the_revert_series(
    fleet: Path,  # noqa: F811
    monorepo: Path,  # noqa: F811
    filter_repo,  # noqa: F811
    resolver,  # noqa: F811
    gazelle,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decision 6's downstream-merged-blocker shape, real this time. The contract's blast set is
    `{owner, consumer} = {acme-app-py, acme-lib-ts}` (hand-seeded `CONTRACT_IMPL`/
    `CONTRACT_CONSUME` edges — neither repo has a real relationship with the other). `acme-app-ts`
    is deliberately NOT a blast-set member; it is `acme-lib-ts`'s real, scan-derived
    `DECLARED_DEP` dependent (`package.json` declares `@acme/lib`) whose own PR is separately
    seeded `MERGED` — exactly Decision 6's shape: a real transitive downstream dependent, outside
    the blast set, that has already merged against the very code the rollback would delete.

    (`acme-app-ts` must stay OUTSIDE the blast set for this to discriminate at all: `unhoist_
    contract`'s own blocking-check skips any descendant that is ALSO a blast-set member — by
    design, since a fellow blast-set member is being rolled back too, not stranded by the
    rollback. A first draft of this test made `acme-app-ts` the contract owner, which put it
    IN the blast set via its own `CONTRACT_IMPL` edge and silently defeated the refusal — caught
    by this test failing on the `execute_hoist_rollback` spy below before this fix.)

    The proof that `execute_hoist_rollback` (task 71's function) is never reached: `cli.
    execute_hoist_rollback` itself is monkeypatched to a spy that raises if called at all — a
    stronger assertion than a call-count check, since any invocation fails the test immediately
    with a clear stack rather than a downstream assertion mismatch.
    """
    _ = (filter_repo, resolver, gazelle)
    transformed(fleet)
    owner = "acme-app-py"  # the contract's owner; unrelated to the blocker/consumer pair
    consumer = "acme-lib-ts"  # the blast-set member
    blocker = "acme-app-ts"  # real DECLARED_DEP dependent of `consumer`, MERGED, not blast_set
    run_id = str(e2e_query(fleet, "SELECT run_id FROM runs")[0][0])
    db_path = fleet / "state" / "fleet.db"

    _seed_contract_and_edges(
        db_path,
        run_id,
        contract_id=_CONTRACT_ID,
        owner=owner,
        consumer=consumer,
        target_path=_TARGET_PATH,
    )
    owner_url = f"https://forge.invalid/{owner}/pull/1"
    consumer_url = f"https://forge.invalid/{consumer}/pull/1"
    blocker_url = f"https://forge.invalid/{blocker}/pull/1"
    _seed_pr(db_path, run_id, repo_id=owner, url=owner_url, contract_id=_CONTRACT_ID)
    _seed_pr(db_path, run_id, repo_id=consumer, url=consumer_url)
    _seed_pr(db_path, run_id, repo_id=blocker, url=blocker_url)

    def _explode(*args, **kwargs):
        raise AssertionError(
            "execute_hoist_rollback must NEVER be called on the REFUSED branch, but it was"
        )

    monkeypatch.setattr(cli, "execute_hoist_rollback", _explode)
    fake_bazel = _bazel_seam_failing_one_dest(
        fleet / "artifacts" / "fake-bazel-task72-refused",
        fail_dest=DESTINATIONS[owner],
        fail_stderr=_STDERR,
    )
    monkeypatch.setattr(cli, "BAZEL_RUNNER", fake_bazel)

    build(fleet, "--no-sandbox")  # would raise the AssertionError above if the branch were wrong

    assert e2e_query(
        fleet, "SELECT status FROM contracts WHERE contract_id = ?", (_CONTRACT_ID,)
    ) == [("FAILED",)], "the finding-driven write (task 66) happens regardless of the rollback"

    assert _revert_commits_on_integration(monorepo, _CONTRACT_ID) == [], (
        "no git revert may land when the rollback is REFUSED"
    )
    refused = e2e_query(
        fleet,
        "SELECT payload FROM findings WHERE kind = 'HoistRollbackRefused' AND run_id = ?",
        (run_id,),
    )
    assert len(refused) == 1
    payload = json.loads(str(refused[0][0]))
    assert payload["contract_id"] == _CONTRACT_ID
    assert blocker in payload["blocking_repo_ids"].split(",")
