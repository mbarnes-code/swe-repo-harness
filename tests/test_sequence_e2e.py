"""§3.1 6c-H end to end, through the real CLI, over real git repositories.

Contract hoisting had every one of its parts tested and none of them connected. `graph/cycles.py`
could dissolve an SCC given contracts; `workers/contracts.py` could discover them; `fleet scan`
could persist them — and `cli.py::_sequence_impl` called `break_cycles(graph, config=...)` with no
`contracts=` at all, so in production every SCC hoisting could have dissolved fell through to the
atomic-wave fallback or to `MANUAL` instead. Nothing failed: the feature was simply unreachable,
and no test in the suite could see that, because every test that exercised hoisting passed the
contracts to `break_cycles` by hand.

The fixture is the smallest fleet where the distinction is visible and real:

* `acme-identity` owns the hand-written `acme.identity.v1` proto and its `package.json` declares a
  dependency on `@acme/billing`;
* `acme-billing` carries the checked-in **generated** binding for that proto, and the binding
  imports `@acme/identity`.

That is a genuine two-repo cycle in the repo graph and a false one in fact: billing's only path
back to identity runs through the interface, not the implementation. Once the proto is its own
node — a sink — billing depends on the contract and the loop is severed with no edge broken.

The three tests are one claim and its two controls:

* `test_a_contract_cycle_is_dissolved_by_scan_then_sequence` — the payoff. `CONTRACT_HOIST`, a
  trivial SCC, the contract `HOISTED` in the database, and its wave strictly earlier than every
  consumer's. If `contracts=` is ever dropped again this is the test that fails.
* `test_the_same_fleet_stays_cyclic_when_contracts_are_skipped` — the control. `--skip-contracts`
  over the SAME repositories must NOT dissolve. Without it the first test proves only that the
  fixture is acyclic for some other reason; with it, the dissolution is attributable to the
  contracts wiring and to nothing else. `--no-hoist-contracts` is asserted alongside it because
  the two flags disable different halves (discovery, ranking) and both must still mean no hoist.
* `test_an_already_hoisted_contract_is_not_re_ranked_by_a_second_sequence` — §3.1: a `HOISTED`
  contract is **pre-committed**, "never re-ranked or re-proposed", and
  `fleet scan && fleet sequence && fleet sequence` leaves `contracts` unchanged. A second run that
  re-proposed it would be ranking a hoist whose files a Phase-2 PR has already moved.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import aiosqlite
import pytest
from typer.testing import CliRunner

from fleet.cli import (
    CONTRACT_NOT_SHARED_FINDING_KIND,
    ExitCode,
    UnresolvedFindingsError,
    _committed_contracts,
    _hoist_watch_for_run,
    _now,
    _persist_contract_not_shared_findings,
    _refuse_unresolved_collisions,
    app,
)
from fleet.graph.cycles import GraphFinding
from fleet.graph.infer import EDGE_BASE_CONFIDENCE
from fleet.models.enums import BreakStrategy, ContractStatus, EdgeKind
from fleet.state.db import StateWriter, connect_ro
from tests.test_cli import MODELS_YAML, RUN_ID, fresh_db, seed_run
from tests.test_scan_e2e import _fresh_db, _make_repo
from tests.test_workers_contracts import (
    CYCLE_FLEET,
    IDENTITY_BINDING,
    IDENTITY_SOURCE,
    PROTO_ID,
    _binding,
    _pkg,
)

runner = CliRunner()

FLEET_YAML = """\
run:
  monorepo_path: ../acme-monorepo
  cache_dir: cache/
  work_dir: work/
concurrency:
  cpu_pool_workers: 1
  docker: 1
verify:
  container_memory: 64m
budgets:
  max_rss_mb: 512
preflight:
  # §11.3's floor is 50 GiB, which no developer machine — and no CI runner — clears with room to
  # spare, so a fixture that left it at the default would exit 9 before Phase 1 started. Lowered
  # rather than disabled, because 0 would mean "unchecked" and this fleet is four repos of a few
  # kilobytes: the gate really runs here, it simply passes. The refusal itself is asserted where
  # it belongs, against a floor no volume can clear (`test_cli.py`, `test_workers_scan.py`).
  min_free_bytes: 1048576
  # §12.11/D116 Leg C (round VI task 111, fix round): this fixture's `package.json`s were never
  # vetted to succeed under a REAL native build (no `"test"` script, and some declare cross-repo
  # dependency names unpublished by design -- e.g. `acme-billing`/`acme-identity`'s proto cycle
  # fixture). Leg C's own red-path gate turns that pre-existing native-baseline failure into a
  # real `phases.status = 'SKIPPED'`, corrupting this file's own hoist/wave assertions, which have
  # nothing to do with baseline behavior. Disabled here entirely for the same reason `tests/
  # test_scan_e2e.py`'s own `FLEET_YAML` disables it. (Measured: without this override,
  # `test_a_contract_cycle_is_dissolved_by_scan_then_sequence` newly fails -- `payload["repos"]`
  # goes from 3 to 0 because all three fixture repos get gated `BaselineRed`/`SKIPPED`.)
  baseline_build:
    enabled: false
"""
#: §11.3/§12.22: the concurrency/verify/budgets keys above are lowered the same way
#: `min_free_bytes` is -- see the note on `tests/test_scan_e2e.py`'s `FLEET_YAML`.
"""No `graph:` section: hoisting is on by default, which is the configuration under test."""

OWNER = "acme-identity"
CONSUMERS = ("acme-billing", "acme-reporting")


@pytest.fixture
def cycle_fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Three real git repos forming the contract cycle, a config bundle and a fresh db."""
    sources = {
        name: _make_repo(tmp_path / "sources", name, dict(files))
        for name, files in CYCLE_FLEET.items()
    }
    workspace = tmp_path / "workspace"
    config = workspace / "config"
    config.mkdir(parents=True)
    (config / "fleet.yaml").write_text(FLEET_YAML, encoding="utf-8")
    (config / "models.yaml").write_text(MODELS_YAML, encoding="utf-8")
    entries = "".join(f"  - name: {name}\n    url: {path}\n" for name, path in sources.items())
    (config / "repos.yaml").write_text(
        f"version: 1\ndefaults:\n  ref: main\nrepos:\n{entries}", encoding="utf-8"
    )
    _fresh_db(workspace / "state" / "fleet.db")
    monkeypatch.chdir(workspace)
    return workspace


@pytest.fixture
def cycle_fleet_low_scc_hard_max(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Identical to `cycle_fleet` except `fleet.yaml` sets `graph.scc_hard_max: 1` — below the
    real SCC's own member count, so §3.1 6e's `MANUAL` ladder rung is reached through the real
    CLI rather than only against a hand-built `build_graph`/`break_cycles` call
    (`tests/test_graph_cycles.py::test_beyond_scc_hard_max_the_harness_refuses`, which does not
    go through `cli._sequence_impl` at all)."""
    sources = {
        name: _make_repo(tmp_path / "sources", name, dict(files))
        for name, files in CYCLE_FLEET.items()
    }
    workspace = tmp_path / "workspace"
    config = workspace / "config"
    config.mkdir(parents=True)
    (config / "fleet.yaml").write_text(FLEET_YAML + "graph:\n  scc_hard_max: 1\n", encoding="utf-8")
    (config / "models.yaml").write_text(MODELS_YAML, encoding="utf-8")
    entries = "".join(f"  - name: {name}\n    url: {path}\n" for name, path in sources.items())
    (config / "repos.yaml").write_text(
        f"version: 1\ndefaults:\n  ref: main\nrepos:\n{entries}", encoding="utf-8"
    )
    _fresh_db(workspace / "state" / "fleet.db")
    monkeypatch.chdir(workspace)
    return workspace


def _base(root: Path) -> list[str]:
    return ["--config", str(root / "config" / "fleet.yaml"), "--db", str(root / "state/fleet.db")]


def _scan(root: Path, *extra: str) -> Any:
    return runner.invoke(
        app, [*_base(root), "scan", "--skip-classify", *extra], catch_exceptions=False
    )


def _sequence(root: Path, *extra: str) -> Any:
    return runner.invoke(app, [*_base(root), "--json", "sequence", *extra], catch_exceptions=False)


def _query(root: Path, sql: str, params: tuple[object, ...] = ()) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(root / "state" / "fleet.db")
    try:
        return [tuple(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _waves(root: Path) -> Mapping[tuple[str, str], int]:
    return {
        (str(kind), str(node_id)): int(index)
        for index, kind, node_id in _query(
            root, "SELECT wave_index, node_kind, node_id FROM wave_members"
        )
    }


def _scan_then_sequence(root: Path, *sequence_flags: str) -> dict[str, Any]:
    assert _scan(root).exit_code == ExitCode.SUCCESS
    result = _sequence(root, *sequence_flags)
    assert result.exit_code == ExitCode.SUCCESS, result.output
    payload: dict[str, Any] = json.loads(result.stdout)
    return payload


# =======================================================================================
# the payoff
# =======================================================================================


def test_a_contract_cycle_is_dissolved_by_scan_then_sequence(cycle_fleet: Path) -> None:
    """`fleet scan` → `fleet sequence` breaks the two-repo cycle by HOISTING, not by breaking.

    This is the assertion the whole 6c-H mechanism existed without: the saturating trial, the
    ranking and contract discovery were each green in isolation while `_sequence_impl` passed no
    `contracts=` at all, so no production run had ever hoisted anything. What is proved here is
    end to end and observable from outside the process — the reported `break_strategy`, the
    absence of any broken edge, the row in `contracts`, and the wave numbers in `wave_members`.

    The wave ordering is the part a stub could not fake. A contract is a sink in `G`, so hoisting
    it correctly puts it strictly *before* every repo that consumes it; a hoist that reported
    `CONTRACT_HOIST` while leaving the contract in the consumers' wave would order a migration
    against an interface that has not moved yet.
    """
    payload = _scan_then_sequence(cycle_fleet)

    cycles = payload["cycles"]
    assert len(cycles) == 1, f"the fixture must really be cyclic before the hoist: {cycles}"
    (finding,) = cycles
    assert finding["break_strategy"] == BreakStrategy.CONTRACT_HOIST.value, finding
    assert finding["hoisted_contract_ids"] == [PROTO_ID]
    assert finding["broken_edge_keys"] == [], "hoisting must not also suppress an edge"
    assert sorted(finding["members"]) == ["acme-billing", OWNER]

    # the SCC is trivial afterwards: every member got its own wave index, which is only possible
    # when no repo is left condensed with another.
    assert payload["hoisted"] == [PROTO_ID]
    assert payload["repos"] == 3, payload
    assert payload["contracts"] == 1, "the hoisted contract is a wave member in its own right"

    status = _query(
        cycle_fleet,
        "SELECT status, hoist_target_path FROM contracts WHERE contract_id = ?",
        (PROTO_ID,),
    )
    assert status == [(ContractStatus.HOISTED.value, "proto/acme/identity/v1")], status

    waves = _waves(cycle_fleet)
    contract_wave = waves[("CONTRACT", PROTO_ID)]
    for consumer in CONSUMERS:
        assert contract_wave < waves[("REPO", consumer)], (
            f"{consumer} consumes {PROTO_ID} and must migrate after it, not with it"
        )
    assert contract_wave < waves[("REPO", OWNER)], "the owner implements the contract"


# =======================================================================================
# §12.8 residual — CONTRACT_IMPL/CONTRACT_CONSUME never reach the `edges` table
# =======================================================================================


def test_the_hoisted_contract_produces_real_contract_impl_and_consume_edges_in_the_table(
    cycle_fleet: Path,
) -> None:
    """§12.8's residual, payoff half. `test_a_contract_cycle_is_dissolved_by_scan_then_sequence`
    above already proves the contract really reaches `status='HOISTED'` and orders the waves
    correctly. What it does NOT check — because the real pipeline cannot yet produce it — is
    whether the `edges` table itself ever gains a `CONTRACT_IMPL` row for the owner and a
    `CONTRACT_CONSUME` row per real consumer, at `EDGE_BASE_CONFIDENCE`, evidenced by the real
    carrier paths. `graph/infer.py::infer_contract_edges` and its confidence/DAG-membership are
    already proven at the hand-built-`InferenceInput` unit level
    (`tests/test_graph_build.py:296-305,396-411`); this is the fixture-fleet, real-`edges`-table
    proof CLAUDE.md's measurement discipline requires before treating that unit proof as evidence
    of what a real run persists.
    """
    payload = _scan_then_sequence(cycle_fleet)
    assert payload["hoisted"] == [PROTO_ID], "the hoist itself must still succeed (unchanged)"

    contract_edges = {
        (str(kind), str(src_id)): (str(dst_id), str(evidence_path), float(confidence))
        for kind, src_id, dst_id, evidence_path, confidence in _query(
            cycle_fleet,
            "SELECT kind, src_id, dst_id, evidence_path, confidence FROM edges "
            "WHERE kind IN ('CONTRACT_IMPL', 'CONTRACT_CONSUME')",
        )
    }

    assert contract_edges[("CONTRACT_IMPL", OWNER)] == (
        PROTO_ID,
        IDENTITY_SOURCE,
        EDGE_BASE_CONFIDENCE[EdgeKind.CONTRACT_IMPL],
    ), "the owner's real proto source path must carry the 1.0-confidence CONTRACT_IMPL row"

    for consumer in CONSUMERS:
        assert contract_edges[("CONTRACT_CONSUME", consumer)] == (
            PROTO_ID,
            IDENTITY_BINDING,
            EDGE_BASE_CONFIDENCE[EdgeKind.CONTRACT_CONSUME],
        ), f"{consumer}'s real generated binding must carry the 0.85-confidence CONSUME row"


# =======================================================================================
# D23 — the retargeted edge's pre-hoist owner must persist, not just be computed
# =======================================================================================


def test_the_retargeted_contract_consume_edge_persists_its_pre_hoist_owner(
    cycle_fleet: Path,
) -> None:
    """D23's round-trip proof: `retargeted_from_repo_id` must survive `insert_edges`, not merely
    be computed correctly in memory by `graph/cycles.py::_materialize`.

    `acme-billing`'s own generated binding (`IDENTITY_BINDING`) both imports `@acme/identity` —
    a real, source-scanned `INTERNAL_IMPORT` edge at 0.8 confidence, `acme-billing -> acme-identity`
    — and is itself the contract's checked-in generated carrier for `acme-billing`. That is exactly
    what `_materialize` retargets onto the hoisted contract (kind rewritten to `CONTRACT_CONSUME`,
    `retargeted_from_repo_id` set to the pre-retarget destination, `acme-identity`) — distinct from
    the fresh 0.85-confidence `CONTRACT_CONSUME` row `infer_contract_edges` always writes for every
    consumer of a hoisted contract, which was never retargeted and must read back NULL.

    Before the fix, `insert_edges` enumerated fifteen columns and dropped this one silently — both
    rows below existed in `edges`, and `retargeted_from_repo_id` read NULL for both.
    """
    payload = _scan_then_sequence(cycle_fleet)
    assert payload["hoisted"] == [PROTO_ID], "the hoist itself must still succeed (unchanged)"

    rows = _query(
        cycle_fleet,
        "SELECT confidence, retargeted_from_repo_id FROM edges "
        "WHERE kind = 'CONTRACT_CONSUME' AND src_id = ? AND evidence_path = ? "
        "ORDER BY confidence",
        ("acme-billing", IDENTITY_BINDING),
    )
    assert len(rows) == 2, (
        "acme-billing must carry both the retargeted real-import row and the fresh "
        f"infer_contract_edges CONSUME row: {rows}"
    )
    (retargeted_confidence, retargeted_from), (fresh_confidence, fresh_from) = rows

    assert retargeted_confidence == pytest.approx(EDGE_BASE_CONFIDENCE[EdgeKind.INTERNAL_IMPORT])
    assert retargeted_from == OWNER, (
        "the retargeted edge's pre-hoist destination (acme-identity) must survive the write path "
        f"— read back {retargeted_from!r}"
    )

    assert fresh_confidence == pytest.approx(EDGE_BASE_CONFIDENCE[EdgeKind.CONTRACT_CONSUME])
    assert fresh_from is None, (
        "infer_contract_edges' own fresh CONSUME row was never retargeted and must stay NULL "
        f"— read back {fresh_from!r}"
    )


# =======================================================================================
# §12.23's last leg — the re-run idempotency proof D23's own fix guarantees "by construction"
# =======================================================================================


def test_the_retargeted_edges_retargeted_from_repo_id_survives_a_second_scan_and_sequence(
    cycle_fleet: Path,
) -> None:
    """§12.23's last leg: re-run idempotency for `edges.retargeted_from_repo_id`, over the SAME
    scenario `test_the_retargeted_contract_consume_edge_persists_its_pre_hoist_owner` above proves
    on a SINGLE run.

    That test proves the value is *written* correctly once — it never re-runs, so it cannot
    distinguish "written correctly" from "written correctly and then silently clobbered by a
    second run". D23's ledger entry and `docs/CRITERIA_PLAN.md` §12.23 both name that distinction
    as the one remaining gap: the `ON CONFLICT ... DO UPDATE SET` clause in `insert_edges`
    deliberately excludes `retargeted_from_repo_id`, which guarantees the column is unchanged
    across a re-run BY CONSTRUCTION — but "by construction" is a claim about the code, not yet a
    claim a re-running TEST has proven.

    `fleet scan && fleet sequence` twice over the identical, unchanged `cycle_fleet` repos: the
    second `scan` re-persists the original source-scanned edges (same `run_id` — reused per
    `_scan_run_id` when `--run` is not passed), the second `sequence` re-applies the retarget via
    `_persist_contract_edges` (its own docstring: "re-running `fleet sequence` for the same run
    re-materializes and re-persists the identical rows rather than duplicating them") — and the
    assertion is that the retargeted row's `retargeted_from_repo_id` reads back byte-identical
    before and after.
    """
    first = _scan_then_sequence(cycle_fleet)
    assert first["hoisted"] == [PROTO_ID], "the hoist itself must still succeed (unchanged)"

    def _retargeted_from() -> object:
        rows = _query(
            cycle_fleet,
            "SELECT confidence, retargeted_from_repo_id FROM edges "
            "WHERE kind = 'CONTRACT_CONSUME' AND src_id = ? AND evidence_path = ? "
            "ORDER BY confidence",
            ("acme-billing", IDENTITY_BINDING),
        )
        assert len(rows) == 2, (
            "acme-billing must still carry both the retargeted real-import row and the fresh "
            f"infer_contract_edges CONSUME row: {rows}"
        )
        (retargeted_confidence, retargeted_from), _fresh = rows
        assert retargeted_confidence == pytest.approx(
            EDGE_BASE_CONFIDENCE[EdgeKind.INTERNAL_IMPORT]
        )
        return retargeted_from

    before = _retargeted_from()
    assert before == OWNER, (
        f"sanity: the first run must persist the pre-hoist owner — read {before!r}"
    )

    # the re-run: SAME fixture, unchanged git repos, SAME run_id (`fleet scan` reuses the latest
    # run per `_scan_run_id` when `--run` is not passed — verified directly against
    # `cli.py::_scan_run_id`, not assumed).
    second = _scan_then_sequence(cycle_fleet)
    assert second["hoisted"] == [], (
        "a pre-committed (HOISTED) contract must not be re-hoisted a second time — unchanged "
        "from `test_an_already_hoisted_contract_is_not_re_ranked_by_a_second_sequence` above"
    )

    after = _retargeted_from()
    assert after == before == OWNER, (
        "edges.retargeted_from_repo_id must be byte-identical across a re-run of `fleet scan "
        f"&& fleet sequence` (§12.23) — first run read {before!r}, second run read {after!r}"
    )


# =======================================================================================
# the control
# =======================================================================================


def test_the_same_fleet_stays_cyclic_when_contracts_are_skipped(cycle_fleet: Path) -> None:
    """`--skip-contracts` over the SAME repos does not dissolve: it falls to the atomic path.

    This is what makes the previous test evidence rather than coincidence. If the cycle fixture
    were dissolvable for any reason other than the hoist — an edge below `min_confidence`, a
    manifest that failed to parse, a symbol never indexed — the payoff test would pass with the
    contracts wiring torn out again, which is precisely the defect it exists to catch. Here the
    contracts step writes no row, so 6c-H has an empty candidate set, and the identical fleet must
    come out the other side still cyclic and condensed into ONE atomic unit.

    `--no-hoist-contracts` is checked on the same fleet because it disables the *other* half: the
    rows exist, and 6c-H must still decline to rank them.
    """
    assert _scan(cycle_fleet, "--skip-contracts").exit_code == ExitCode.SUCCESS
    assert _query(cycle_fleet, "SELECT COUNT(*) FROM contracts") == [(0,)]

    skipped = _sequence(cycle_fleet)
    assert skipped.exit_code == ExitCode.SUCCESS, skipped.output
    payload = json.loads(skipped.stdout)
    (finding,) = payload["cycles"]
    assert finding["break_strategy"] != BreakStrategy.CONTRACT_HOIST.value, (
        "with no contracts persisted there is nothing to hoist; a CONTRACT_HOIST here would mean "
        "the strategy is being decided by something other than the contracts"
    )
    assert finding["hoisted_contract_ids"] == []
    assert payload["hoisted"] == []
    assert payload["contracts"] == 0
    assert finding["broken_edge_keys"], (
        "with hoisting unreachable the ladder must fall through to 6d and pay for the ordering "
        "with a suppressed edge — that cost is exactly what 6c-H exists to avoid"
    )

    # and the second half of the same claim: the rows may exist, but hoisting stays off on request
    assert _scan(cycle_fleet).exit_code == ExitCode.SUCCESS
    assert _query(cycle_fleet, "SELECT COUNT(*) FROM contracts") == [(1,)]
    refused = _sequence(cycle_fleet, "--no-hoist-contracts")
    assert refused.exit_code == ExitCode.SUCCESS, refused.output
    assert json.loads(refused.stdout)["hoisted"] == []
    assert _query(cycle_fleet, "SELECT status FROM contracts") == [
        (ContractStatus.EXTRACTABLE.value,)
    ], "a declined hoist must not leave a HOISTED row behind"


def test_a_scc_beyond_hard_max_refuses_sequencing_with_exit_6_through_the_real_cli(
    cycle_fleet_low_scc_hard_max: Path,
) -> None:
    """§3.1 6e / §10 exit 6, through `cli._sequence_impl`'s own inline check (`manual = tuple(...
    if res.break_strategy is BreakStrategy.MANUAL)`) -- this is inline logic in `_sequence_impl`
    itself, not delegated to a named helper, and until now had no coverage above the direct
    `break_cycles()` unit level (`tests/test_graph_cycles.py::
    test_beyond_scc_hard_max_the_harness_refuses`, `test_a_41_repo_cycle_completes_without_hanging`
    -- neither goes through the real CLI).
    """
    assert _scan(cycle_fleet_low_scc_hard_max, "--skip-contracts").exit_code == ExitCode.SUCCESS
    result = _sequence(cycle_fleet_low_scc_hard_max)
    assert result.exit_code == ExitCode.UNRESOLVED_FINDINGS, result.output
    assert "resolved MANUAL and can never be sequenced" in result.output, result.output
    # the plan is never persisted for a MANUAL-resolved SCC: no repo may get a wave index, or an
    # operator reading `wave_members` would see a plan the harness itself refused to commit to.
    assert _query(cycle_fleet_low_scc_hard_max, "SELECT COUNT(*) FROM wave_members") == [(0,)]


# =======================================================================================
# pre-commitment
# =======================================================================================


def test_an_already_hoisted_contract_is_not_re_ranked_by_a_second_sequence(
    cycle_fleet: Path,
) -> None:
    """A `HOISTED` contract is pre-committed: the second `sequence` re-proposes nothing.

    §3.1 states it outright — an already-`HOISTED` contract is treated as pre-committed and
    "never re-ranked or re-proposed" — and the reason is not tidiness. The hoist is already
    committed in git and named by an open PR; re-ranking it would let 6c-H propose moving files
    that have already moved, and would let a re-run's `max_hoists_per_scc` budget be spent twice
    on the same contract. The observable form of "not re-ranked" is that the second run reports an
    empty `hoisted` list while still producing the same dissolved plan from the same rows.
    """
    first = _scan_then_sequence(cycle_fleet)
    assert first["hoisted"] == [PROTO_ID]

    second = _sequence(cycle_fleet)
    assert second.exit_code == ExitCode.SUCCESS, second.output
    payload = json.loads(second.stdout)
    assert payload["hoisted"] == [], "a pre-committed contract must not be hoisted a second time"
    assert payload["cycles"] == [], (
        "the retargets a HOISTED contract earns are re-applied before 6c-H runs, so the SCC is "
        "already gone and there is no cycle left to resolve"
    )
    assert payload["wave_index_by_repo"] == first["wave_index_by_repo"]
    assert payload["contracts"] == 1

    # §3.1: `fleet scan && fleet sequence && fleet sequence` leaves `contracts` unchanged.
    assert _query(cycle_fleet, "SELECT contract_id, status FROM contracts") == [
        (PROTO_ID, ContractStatus.HOISTED.value)
    ]


# =======================================================================================
# §12.31 Leg E (round VI task 58): `--forbid-hoist` -- real wiring, not the exit-2 stub
# =======================================================================================


def test_a_forbid_hoist_veto_survives_a_real_re_scan_and_re_sequence(cycle_fleet: Path) -> None:
    """`--forbid-hoist PROTO_ID` on the SAME real cycle the payoff test hoists: exit 0, the
    contract `FORBIDDEN` rather than `HOISTED`, the SCC falls through to 6d/6e instead, a
    `ContractHoistOverride` finding is written, and the veto survives a REAL second `fleet scan`
    (not merely a second `fleet sequence`) -- the "sticky across re-sequencing" claim
    `docs/SPEC.md:6746-6753` makes, proved end to end rather than by inspecting the code and
    asserting it should work (CLAUDE.md Rule 12).

    `test_sequence_refuses_the_cycle_flags_it_cannot_thread` in `tests/test_scan_e2e.py` is the
    old-passes/new-fails discriminator for the refusal itself (this task genuinely displaced the
    `exit 2` assertion on THAT fixture); this test is the substantive proof the flag now DOES
    something, which needs a fixture with a real hoistable contract -- `test_scan_e2e.py`'s does
    not have one.
    """
    assert _scan(cycle_fleet).exit_code == ExitCode.SUCCESS
    assert _query(
        cycle_fleet, "SELECT status FROM contracts WHERE contract_id = ?", (PROTO_ID,)
    ) == [(ContractStatus.EXTRACTABLE.value,)], "must genuinely be a live hoist candidate first"

    forbidden = _sequence(cycle_fleet, "--forbid-hoist", PROTO_ID)
    assert forbidden.exit_code == ExitCode.SUCCESS, forbidden.output
    payload = json.loads(forbidden.stdout)
    assert payload["hoisted"] == [], "a forbidden contract must never be hoisted"
    (finding,) = payload["cycles"]
    assert finding["break_strategy"] != BreakStrategy.CONTRACT_HOIST.value, finding
    assert finding["hoisted_contract_ids"] == []
    assert finding["broken_edge_keys"], (
        "with hoisting forbidden the ladder must fall through to 6d, same as "
        "test_the_same_fleet_stays_cyclic_when_contracts_are_skipped above"
    )

    assert _query(
        cycle_fleet,
        "SELECT status, status_detail FROM contracts WHERE contract_id = ?",
        (PROTO_ID,),
    ) == [(ContractStatus.FORBIDDEN.value, "operator_forbid_hoist")]

    override_findings = _query(
        cycle_fleet,
        "SELECT kind, severity, repo_id FROM findings WHERE kind = ? ORDER BY finding_id",
        ("ContractHoistOverride",),
    )
    assert override_findings == [("ContractHoistOverride", "warn", OWNER)], (
        "the finding is the authority (docs/SPEC.md:6762-6768), repo_id the contract's owner"
    )

    # THE proof this task exists for: a real second `fleet scan` rebuilds `contracts` from
    # scratch (DELETE-then-INSERT, §3.1 "re-runnable by construction") and must not reset the
    # veto -- `workers/contracts.py::carry_over_committed` now carries `FORBIDDEN` across that
    # rebuild the same way it always carried `HOISTED`/`MIGRATED`.
    rescanned = _scan(cycle_fleet)
    assert rescanned.exit_code == ExitCode.SUCCESS, rescanned.output
    assert _query(
        cycle_fleet, "SELECT status FROM contracts WHERE contract_id = ?", (PROTO_ID,)
    ) == [(ContractStatus.FORBIDDEN.value,)], "the veto must survive the whole-run rebuild"

    # and sticky within a re-sequence too, with the flag NOT repeated -- `_hoist_contracts`'
    # `status is ContractStatus.EXTRACTABLE` filter alone keeps a FORBIDDEN row out of candidacy.
    resequenced = _sequence(cycle_fleet)
    assert resequenced.exit_code == ExitCode.SUCCESS, resequenced.output
    assert json.loads(resequenced.stdout)["hoisted"] == [], (
        "never re-proposed once FORBIDDEN, even with --forbid-hoist not repeated"
    )


# =======================================================================================
# §12.31 case (ii), Leg C2 (round VI task 66, ADR-0123): a FAILED contract survives a real
# re-scan -- the fix-round proof for a controller review finding (C1)
# =======================================================================================


def test_a_failed_contract_survives_a_real_re_scan(cycle_fleet: Path) -> None:
    """`carry_over_committed`'s `FAILED` widening (ADR-0123) is inert unless its ONLY production
    feeder, `_committed_contracts`, also selects `FAILED` rows -- round VI task 66's first landing
    widened only `carry_over_committed` and left `_committed_contracts`'s `status IN (...)` list
    unchanged, so a `FAILED` contract was silently dropped and RE-DERIVED AS `EXTRACTABLE` on the
    very next `fleet scan`: the exact `REJECTED` treatment ADR-0123 argues against, re-hoisting a
    contract that had just broken a build. Fixed by adding `'FAILED'` to `_committed_contracts`'s
    SQL list (mirroring round VI task 58's `e3b1a86`, which widened BOTH halves for `FORBIDDEN` in
    the same commit).

    This test drives the REAL path up to but not including a second `fleet scan`'s own
    graph-consistency audit: `_committed_contracts` is called DIRECTLY against a real database a
    real `fleet scan`+`fleet sequence` produced, rather than a hand-built `ContractNode` passed
    straight to `carry_over_committed` -- exactly the two-anchor blindness the reviewer's finding
    named: the pre-existing unit test (`tests/test_workers_contracts.py::
    test_a_failed_contract_survives_the_rebuild_it_is_not_part_of`) bypasses `_committed_contracts`
    entirely and could not have caught this. A full SECOND `fleet scan` is deliberately not run
    here: `ADR-0122` (Leg D design, Decision 1, landed before this task) already documents that
    `_graph_edges` applies no `contracts.status IN (...)` filter symmetric to `_graph_nodes`'s, so
    a `contracts.status = 'FAILED'` row -- this task's own new capability -- makes the FLEET-WIDE
    scan's graph-consistency audit crash with `GraphError` on the retargeted `CONTRACT_CONSUME`
    edge the FIRST `fleet sequence` already wrote (confirmed by actually running a second `fleet
    scan` here and reproducing exactly that crash before this test was rewritten to avoid it).
    That fix is `task-65-brief.md`'s own scope (Leg D slice 1, a separate, concurrent, non-
    overlapping task) -- not this test's job, and the controller has already been told (this
    task's own report) that task-65 must merge before task-66 for this exact reason. Calling
    `_committed_contracts` directly is the sanctioned alternative the review comment itself named
    ("call `_committed_contracts` directly (or run a full `fleet scan`-equivalent)").

    The `UPDATE contracts SET status = 'FAILED'` below is the exact SQL `cli._BuildSink.__call__`
    runs in production (`cli.py`, this task's own new write) -- a real Phase 3 build failure is
    not reproduced here (that is `tests/test_build_e2e.py`'s job, proven separately, real bazel
    seam included); this test isolates the SCAN-side carry-over question only, which is what C1
    is about.
    """
    assert _scan(cycle_fleet).exit_code == ExitCode.SUCCESS
    assert _query(
        cycle_fleet, "SELECT status FROM contracts WHERE contract_id = ?", (PROTO_ID,)
    ) == [(ContractStatus.EXTRACTABLE.value,)], "must genuinely be a live hoist candidate first"

    sequenced = _sequence(cycle_fleet)
    assert sequenced.exit_code == ExitCode.SUCCESS, sequenced.output
    assert json.loads(sequenced.stdout)["hoisted"] == [PROTO_ID]
    assert _query(
        cycle_fleet, "SELECT status FROM contracts WHERE contract_id = ?", (PROTO_ID,)
    ) == [(ContractStatus.HOISTED.value,)], "sanity: must genuinely be HOISTED before it can FAIL"

    run_id = str(_query(cycle_fleet, "SELECT run_id FROM runs")[0][0])
    conn = sqlite3.connect(cycle_fleet / "state" / "fleet.db", isolation_level=None)
    try:
        conn.execute(
            "UPDATE contracts SET status = 'FAILED' WHERE run_id = ? AND contract_id = ?",
            (run_id, PROTO_ID),
        )
    finally:
        conn.close()
    assert _query(
        cycle_fleet, "SELECT status FROM contracts WHERE contract_id = ?", (PROTO_ID,)
    ) == [(ContractStatus.FAILED.value,)], "sanity: the simulated Phase 3 write landed"

    # THE proof C1 is about: `_committed_contracts` -- the ONLY production feeder of
    # `carry_over_committed`'s `committed` argument -- must actually SELECT the FAILED row, not
    # merely be theoretically able to carry it over if handed one.
    async def _read_committed() -> tuple[str, ...]:
        async with aiosqlite.connect(cycle_fleet / "state" / "fleet.db") as aconn:
            nodes = await _committed_contracts(aconn, run_id)
            return tuple(node.contract_id for node in nodes if node.status == ContractStatus.FAILED)

    failed_committed_ids = asyncio.run(_read_committed())
    assert failed_committed_ids == (PROTO_ID,), (
        "a FAILED contract must be selected by _committed_contracts -- if this is empty, its SQL "
        "status IN (...) list is not including 'FAILED' (C1)"
    )


def test_a_failed_contracts_watch_survives_into_a_later_wave(cycle_fleet: Path) -> None:
    """`_hoist_watch_for_run` must keep watching a contract once it is `FAILED`, not only while
    it is `HOISTED`/`MIGRATED` -- controller review finding I3, round VI task 66 fix round.

    `_hoist_watch_for_run` is recomputed FRESH per wave (`_run_build_wave`, inside `_build_impl`'s
    wave loop). Before this fix, once the FIRST failing consumer's dispatch wrote `contracts.
    status = 'FAILED'` (via `_BuildSink`), a `HOISTED`/`MIGRATED`-only filter would silently stop
    watching that contract for every LATER-wave consumer of the SAME broken hoist -- each of those
    would then hit an unattributed, still-`retryable=True` `BUILD_ERROR` and burn its own full
    retry ladder, exactly the opposite of what §12.31(ii) needs (every SCC member's `phases.
    attempts` should stay unspent, not just the first repo to trip the finding).

    This test proves the fix at the function `_hoist_watch_for_run` actually reads from, against a
    real database a real `fleet scan`+`fleet sequence` produced (the same shape as the C1 proof
    above, for the same reason: a hand-built `HoistWatch` tuple would not have caught either bug).
    """
    assert _scan(cycle_fleet).exit_code == ExitCode.SUCCESS
    sequenced = _sequence(cycle_fleet)
    assert sequenced.exit_code == ExitCode.SUCCESS, sequenced.output
    assert json.loads(sequenced.stdout)["hoisted"] == [PROTO_ID]

    run_id = str(_query(cycle_fleet, "SELECT run_id FROM runs")[0][0])
    hoist_target_path = _query(
        cycle_fleet, "SELECT hoist_target_path FROM contracts WHERE contract_id = ?", (PROTO_ID,)
    )[0][0]
    conn = sqlite3.connect(cycle_fleet / "state" / "fleet.db", isolation_level=None)
    try:
        conn.execute(
            "UPDATE contracts SET status = 'FAILED' WHERE run_id = ? AND contract_id = ?",
            (run_id, PROTO_ID),
        )
    finally:
        conn.close()

    async def _read_watch() -> tuple[tuple[str, str], ...]:
        async with aiosqlite.connect(cycle_fleet / "state" / "fleet.db") as aconn:
            watch = await _hoist_watch_for_run(aconn, run_id)
            return tuple((w.contract_id, w.hoist_target_path) for w in watch)

    watched = asyncio.run(_read_watch())
    assert watched == ((PROTO_ID, hoist_target_path),), (
        "a FAILED contract must still be watched -- if this is empty, a later wave's consumer of "
        "the SAME broken hoist would go unattributed and burn its own full retry ladder (I3)"
    )


# =======================================================================================
# §12.31 case (i), Leg A (round VI task 55): not-shared-after-retarget rollback
# =======================================================================================

HUB_PROTO = """\
syntax = "proto3";

package acme.hub.v1;

service HubService {
  rpc GetWidget (GetWidgetRequest) returns (Widget);
}

message Widget {
  string id = 1;
}

message GetWidgetRequest {
  string id = 1;
}
"""

NOT_SHARED_ID = "proto:acme.hub.v1"

NOT_SHARED_FLEET: Mapping[str, Mapping[str, str]] = {
    "acme-hub": {
        "proto/acme/hub/v1/hub.proto": HUB_PROTO,
        # the manifest edge that closes the cycle: the owner depends on its one REAL consumer
        "package.json": _pkg("@acme/hub", {"@acme/spoke0": "^1.0.0"}),
    },
    "acme-spoke0": {
        "src/api/acme/hub/v1/hub_pb.ts": _binding("acme.hub.v1", imports="@acme/hub"),
        "package.json": _pkg("@acme/spoke0"),
    },
}
"""Real 2-repo cycle, same shape as `CYCLE_FLEET` but with only ONE real file-carrying consumer
(`acme-spoke0`) — deliberately, not by oversight. §3.1 5b (v)'s symbol join is the only mechanism
that could add a SECOND declared consumer with no `source_paths`/`generated_paths` entry of its
own (`workers/contracts.py::_consumers`'s "KNOWN GAP" docstring), and
`test_workers_contracts.py`'s own "gap 2" finding records that no shipped extractor emits the
non-definition reference symbol that join needs — "§3.1 5b (v)'s symbol join is currently
unreachable from real scan data." A real `fleet scan` over any file-based fixture therefore cannot
by itself produce a contract whose *declared* consumer count exceeds its *post-retarget* one: every
file-based consumer join (`_consumers`'s `sources`/`generated` sets) populates
`source_paths`/`generated_paths` too, and `infer_contract_edges` synthesizes a `CONTRACT_CONSUME`
edge from those fields alone, independent of whether a real dependency edge ever existed. This is a
new, disclosed finding of task 55's own measurement, not assumed from the brief: the E2E fixture
below runs a real `fleet scan` first (real repos, real edges, real 5b detection genuinely rejecting
this contract for `min_consumers` with only 1 real consumer — confirmed by the test itself before
seeding anything), then completes the ONE row real detection cannot yet produce with a direct SQL
UPDATE, mirroring `test_workers_contracts.py`'s own precedent for injecting the identical missing
input surface (a synthetic `SymbolRef`) when a real extractor cannot supply it yet."""


@pytest.fixture
def not_shared_fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Two real git repos forming a genuine 2-cycle, a config bundle and a fresh db."""
    sources = {
        name: _make_repo(tmp_path / "sources", name, dict(files))
        for name, files in NOT_SHARED_FLEET.items()
    }
    workspace = tmp_path / "workspace"
    config = workspace / "config"
    config.mkdir(parents=True)
    (config / "fleet.yaml").write_text(FLEET_YAML, encoding="utf-8")
    (config / "models.yaml").write_text(MODELS_YAML, encoding="utf-8")
    entries = "".join(f"  - name: {name}\n    url: {path}\n" for name, path in sources.items())
    (config / "repos.yaml").write_text(
        f"version: 1\ndefaults:\n  ref: main\nrepos:\n{entries}", encoding="utf-8"
    )
    _fresh_db(workspace / "state" / "fleet.db")
    monkeypatch.chdir(workspace)
    return workspace


def _seed_second_consumer_with_no_edge_evidence(root: Path) -> None:
    """Complete the ONE row real 5b detection cannot yet produce (see `NOT_SHARED_FLEET`'s
    docstring): add a second *declared* consumer, `acme-ghost-consumer`, that carries no
    `source_paths`/`generated_paths` entry of its own — the exact shape §3.1 5b (v)'s symbol join
    would produce if any shipped extractor could reach it. `acme-ghost-consumer` is a nominal
    identifier only: it is never registered as a real repo, because nothing downstream of this
    UPDATE ever resolves `consumer_repo_ids` against the `repos` table — graph nodes come from
    `_graph_nodes` (the real `repos` rows), and `infer_contract_edges` skips any consumer with no
    path entry, so a phantom identifier with no matching path can never produce a node or an edge.

    Run against the row a real `fleet scan` already wrote (confirmed by the caller to be genuinely
    `REJECTED`/`status_detail='min_consumers'`, i.e., not fabricated from nothing): only the fields
    the missing symbol-join signal would have changed are touched.
    """
    conn = sqlite3.connect(root / "state" / "fleet.db")
    try:
        (run_id,) = conn.execute("SELECT run_id FROM runs").fetchone()
        (consumers_json,) = conn.execute(
            "SELECT consumer_repo_ids FROM contracts WHERE run_id = ? AND contract_id = ?",
            (run_id, NOT_SHARED_ID),
        ).fetchone()
        consumers = sorted({*json.loads(consumers_json), "acme-ghost-consumer"})
        conn.execute(
            "UPDATE contracts SET consumer_repo_ids = ?, extractable = 1, "
            "extraction_confidence = 0.9, hoist_target_path = 'contracts/acme/hub/v1', "
            "status = 'EXTRACTABLE', status_detail = '' "
            "WHERE run_id = ? AND contract_id = ?",
            (json.dumps(consumers), run_id, NOT_SHARED_ID),
        )
        conn.commit()
    finally:
        conn.close()


def test_a_not_shared_after_retarget_contract_is_rejected_through_the_real_cli(
    not_shared_fleet: Path,
) -> None:
    """§12.31 case (i), Leg A, end to end: `fleet scan` (real detection, real rejection for
    `min_consumers` with 1 real consumer) + a seeded second declared consumer with no edge
    evidence (the one signal real detection cannot yet emit, see `NOT_SHARED_FLEET`'s docstring)
    + `fleet sequence` (fully real — this is what exercises `_hoist_contracts`'s new check and
    `cli._rejected_contract_rows`/`_persist_contract_not_shared_findings`).
    """
    assert _scan(not_shared_fleet).exit_code == ExitCode.SUCCESS
    # Confirm the premise before seeding anything: real 5b detection genuinely rejects this
    # contract with only 1 real file-carrying consumer -- the seed step adds a SECOND declared
    # consumer, it does not fabricate the whole row.
    assert _query(not_shared_fleet, "SELECT extractable, status, status_detail FROM contracts") == [
        (0, ContractStatus.REJECTED.value, "min_consumers")
    ]

    before_attempts = sorted(_query(not_shared_fleet, "SELECT repo_id, attempts FROM phases"))
    assert before_attempts, "the phases table must be non-empty or the equality below is vacuous"

    _seed_second_consumer_with_no_edge_evidence(not_shared_fleet)
    assert _query(not_shared_fleet, "SELECT extractable, status FROM contracts") == [
        (1, ContractStatus.EXTRACTABLE.value)
    ], "seed did not take"

    result = _sequence(not_shared_fleet)
    assert result.exit_code == ExitCode.SUCCESS, result.output
    payload = json.loads(result.stdout)

    assert payload["hoisted"] == []
    (finding,) = payload["cycles"]
    assert finding["break_strategy"] != BreakStrategy.CONTRACT_HOIST.value, (
        "the hoist must be rolled back, not committed"
    )
    assert finding["hoisted_contract_ids"] == []

    # `contracts.status='REJECTED'` for that contract, with the Leg A detail string.
    assert _query(
        not_shared_fleet,
        "SELECT status, status_detail FROM contracts WHERE contract_id = ?",
        (NOT_SHARED_ID,),
    ) == [(ContractStatus.REJECTED.value, "not_shared_after_retarget")]

    # one findings row, kind='ContractNotShared'.
    findings = _query(
        not_shared_fleet,
        "SELECT kind, severity, repo_id FROM findings WHERE kind = ?",
        ("ContractNotShared",),
    )
    assert findings == [("ContractNotShared", "warn", "acme-hub")]

    # zero wave_members rows with node_kind='CONTRACT' for this contract.
    assert _query(
        not_shared_fleet,
        "SELECT COUNT(*) FROM wave_members WHERE node_kind = 'CONTRACT' AND node_id = ?",
        (NOT_SHARED_ID,),
    ) == [(0,)]

    # phases.attempts unchanged for every repo -- the harness's own bad hypothesis must not
    # consume a repo's three chances (§3.1 6c-H rollback).
    after_attempts = sorted(_query(not_shared_fleet, "SELECT repo_id, attempts FROM phases"))
    assert after_attempts == before_attempts

    # the command exited 0 -- `_phase1_exit_report` still passes (fact 3 of the brief: a
    # REJECTED contract is in neither the HOISTED/MIGRATED count nor the CONTRACT wave_members
    # count, so criterion (c) still holds).


# =======================================================================================
# min_consumers is actually threaded from settings, not a hardcoded default
# (round VI task 55, controller-review fix wave)
# =======================================================================================

LOWERED_MIN_CONSUMERS_FLEET_YAML = (
    FLEET_YAML
    + """\
scan:
  contracts:
    min_consumers: 1
"""
)
"""Same bundle as `FLEET_YAML`, plus `scan.contracts.min_consumers: 1` -- the operator-configured
value `cli._sequence_impl` must pass to `break_cycles(...)`. If it silently fell back to
`ContractsSection()`'s hardcoded field default (`2`) instead of this value, the fixture below
(1 real post-retarget consumer) would still be rejected under this config, exactly as it is under
the default -- this test would then pass for the wrong reason. Proving the THREADED value changes
the outcome needs an operator setting that disagrees with the hardcoded default and flips the
result; `1` is the smallest legal value (`Field(ge=1)`) and is exactly what a real not-shared
contract's actual single consumer needs to pass the check.
"""


@pytest.fixture
def not_shared_fleet_min_consumers_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Identical to `not_shared_fleet` except `fleet.yaml` sets
    `scan.contracts.min_consumers: 1`."""
    sources = {
        name: _make_repo(tmp_path / "sources", name, dict(files))
        for name, files in NOT_SHARED_FLEET.items()
    }
    workspace = tmp_path / "workspace"
    config = workspace / "config"
    config.mkdir(parents=True)
    (config / "fleet.yaml").write_text(LOWERED_MIN_CONSUMERS_FLEET_YAML, encoding="utf-8")
    (config / "models.yaml").write_text(MODELS_YAML, encoding="utf-8")
    entries = "".join(f"  - name: {name}\n    url: {path}\n" for name, path in sources.items())
    (config / "repos.yaml").write_text(
        f"version: 1\ndefaults:\n  ref: main\nrepos:\n{entries}", encoding="utf-8"
    )
    _fresh_db(workspace / "state" / "fleet.db")
    monkeypatch.chdir(workspace)
    return workspace


def test_the_configured_min_consumers_value_actually_reaches_the_6c_h_check(
    not_shared_fleet_min_consumers_1: Path,
) -> None:
    """The load-bearing regression this fix closes: `cli._sequence_impl` must pass
    `settings.config.scan.contracts.min_consumers` to `break_cycles(...)`, not leave the
    parameter to default to a bare `ContractsSection()` instantiation (which would silently use
    the field default, `2`, regardless of what an operator configured).

    Identical fixture and seeding to
    `test_a_not_shared_after_retarget_contract_is_rejected_through_the_real_cli` (1 real
    post-retarget consumer after a seeded second declared one with no edge evidence) — the ONLY
    difference is `scan.contracts.min_consumers: 1` in `fleet.yaml`. Under the hardcoded default
    (`2`) this contract is rejected (proven by the sibling test above); under an operator's real
    `min_consumers: 1`, its one real consumer is enough, and the SAME fixture must be HOISTED
    instead. A test that could pass under either the threaded value or the hardcoded default
    would prove nothing about the threading — this one cannot: the two outcomes are mutually
    exclusive and the fixture is unchanged, so only the configured value being read explains a
    flip.
    """
    assert _scan(not_shared_fleet_min_consumers_1).exit_code == ExitCode.SUCCESS
    # premise: real 5b detection also uses this config (`workers/contracts.py:797`'s
    # `len(consumers) < payload.config.min_consumers`), so with min_consumers=1 and 1 real
    # consumer, 5b itself passes the contract too -- it is EXTRACTABLE here, not REJECTED, which
    # is the opposite of the sibling test's premise at the default of 2 (documented, not asserted
    # away: this confirms the two tests' premises genuinely differ, not just their fleet.yaml).
    assert _query(
        not_shared_fleet_min_consumers_1, "SELECT extractable, status, status_detail FROM contracts"
    ) == [(1, ContractStatus.EXTRACTABLE.value, "")]

    _seed_second_consumer_with_no_edge_evidence(not_shared_fleet_min_consumers_1)

    result = _sequence(not_shared_fleet_min_consumers_1)
    assert result.exit_code == ExitCode.SUCCESS, result.output
    payload = json.loads(result.stdout)

    assert payload["hoisted"] == [NOT_SHARED_ID], (
        "with the operator's configured min_consumers=1, the contract's one real post-retarget "
        "consumer is enough -- the hoist must be COMMITTED, the opposite of the sibling test's "
        "outcome under the hardcoded default of 2, over the identically-shaped fixture"
    )
    (finding,) = payload["cycles"]
    assert finding["break_strategy"] == BreakStrategy.CONTRACT_HOIST.value
    assert finding["hoisted_contract_ids"] == [NOT_SHARED_ID]

    assert _query(
        not_shared_fleet_min_consumers_1,
        "SELECT status, status_detail FROM contracts WHERE contract_id = ?",
        (NOT_SHARED_ID,),
    ) == [(ContractStatus.HOISTED.value, "")], (
        "status flips to HOISTED; the hoisted-contract writer only moves `status`, never "
        "`status_detail` (`_hoisted_contract_rows`), so it stays the empty string 5b left it at"
    )
    assert _query(
        not_shared_fleet_min_consumers_1,
        "SELECT COUNT(*) FROM findings WHERE kind = ?",
        ("ContractNotShared",),
    ) == [(0,)], "no rejection finding when the hoist is actually committed"


# =======================================================================================
# the findings writer only writes its own kind (round VI task 55, controller-review fix)
# =======================================================================================


async def test_the_findings_writer_never_mislabels_a_different_kind_in_report_findings(
    tmp_path: Path,
) -> None:
    """Unit-level, against `_persist_contract_not_shared_findings` directly: not expressible
    through the real CLI today, because `CycleReport.findings` carries only `ContractNotShared`
    rows in production (Leg A is the only producer). Mirrors `test_workers_contracts.py`'s own
    precedent for testing a rule the worker implements ahead of the input its real producer can
    supply — here a *second* `GraphFinding` kind, which no `src/` code emits into
    `CycleReport.findings` yet, but which the writer's own docstring claims is already handled
    safely ("a future leg adding a second finding kind gets its own writer rather than this one's
    DELETE scope silently widening").

    Without the `kind == CONTRACT_NOT_SHARED_FINDING_KIND` filter, every row in `findings` would
    be stamped with the literal `CONTRACT_NOT_SHARED_FINDING_KIND` in the `kind` column
    regardless of the `GraphFinding`'s own `.kind` — a foreign kind would be silently mislabelled
    as `ContractNotShared` (its true kind would still be visible inside the JSON `payload` via
    `asdict(finding)`, but the `kind` column itself, and every reader keyed on it, would be wrong).
    """
    db_path = fresh_db(tmp_path / "state" / "fleet.db")
    seed_run(db_path, repos=("acme-owner",))

    own_kind_finding = GraphFinding(
        kind=CONTRACT_NOT_SHARED_FINDING_KIND,
        severity="warn",
        repo_id="acme-owner",
        payload={"contract_id": "proto:acme.owned.v1"},
    )
    foreign_kind_finding = GraphFinding(
        kind="SomeFutureLegKind",
        severity="warn",
        repo_id="acme-owner",
        payload={"contract_id": "proto:acme.other.v1"},
    )

    async with StateWriter(db_path, owner="test-findings-writer-filter") as writer:
        await _persist_contract_not_shared_findings(
            writer, RUN_ID, [own_kind_finding, foreign_kind_finding], now=_now()
        )

    rows = _query(tmp_path, "SELECT kind, fingerprint FROM findings ORDER BY kind")
    assert rows == [(CONTRACT_NOT_SHARED_FINDING_KIND, own_kind_finding.fingerprint)], (
        "the foreign-kind finding must be dropped by this writer entirely, not written under "
        "the wrong kind — a second writer (not built here) owns persisting it"
    )


# =======================================================================================
# `_refuse_unresolved_collisions` really keys on `resolution IS NULL`, not on `severity`
# alone (§10 exit 6)
# =======================================================================================


async def _insert_collision(
    db_path: Path, *, run_id: str, key: str, severity: str, resolution: str | None
) -> None:
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO collisions (run_id, kind, key, repo_ids, severity, resolution, "
            "                        detected_at) VALUES (?, 'COORDINATE', ?, '[\"a\",\"b\"]', "
            "                        ?, ?, ?)",
            (run_id, key, severity, resolution, "2026-08-08T12:00:00+00:00"),
        )
    finally:
        conn.close()


async def test_a_resolved_error_severity_collision_does_not_refuse_the_run(
    tmp_path: Path,
) -> None:
    """`_refuse_unresolved_collisions`'s SQL filters on `severity = 'error' AND resolution IS
    NULL`, not on `severity = 'error'` alone. Every collision kind actually wired into production
    today (`COORDINATE`, via `cli._sequence_impl`'s bare `CollisionInput(coordinates=..., ...)`
    call — `contracts=`/`dests=` are never populated) happens to keep `severity='error'` in
    lockstep with `resolution is None` (`graph/collisions.py::_coordinate_collisions`), so no
    currently-reachable `fleet sequence` invocation can exercise the distinction. But
    `_contract_collisions`/`_dest_collisions` (unwired today — this module's `CollisionInput`
    construction site, `cli.py:3628-3629`, never populates `contracts=`/`dests=`) both allow
    `severity='error'` alongside a non-NULL `resolution` — a genuine "the operator must be told,
    but the harness already decided" row — and this writer's own docstring commits to reading
    `resolution IS NULL` specifically so that when either detector is wired in, an already-resolved
    error-severity collision does not spuriously block every subsequent `fleet sequence`. Proven
    directly against the function, ahead of the input its current real producer can supply — the
    same precedent `test_the_findings_writer_never_mislabels_a_different_kind_in_report_findings`
    above sets for `_persist_contract_not_shared_findings`.
    """
    db_path = fresh_db(tmp_path / "state" / "fleet.db")
    seed_run(db_path, repos=("acme-owner", "acme-other"))
    await _insert_collision(
        db_path,
        run_id=RUN_ID,
        key="already-resolved",
        severity="error",
        resolution="owner:acme-owner",
    )

    conn = await connect_ro(db_path)
    try:
        await _refuse_unresolved_collisions(conn, RUN_ID)  # must not raise
    finally:
        await conn.close()


async def test_an_unresolved_error_severity_collision_still_refuses_the_run(
    tmp_path: Path,
) -> None:
    """Control for the sibling test above: the SAME `severity='error'` value, but with
    `resolution IS NULL` (the shape every collision persisted by a real `fleet sequence` today
    actually takes when it blocks), still raises. Without this control, the sibling test could
    pass merely because `_refuse_unresolved_collisions` had stopped checking `severity` at all.
    """
    db_path = fresh_db(tmp_path / "state" / "fleet.db")
    seed_run(db_path, repos=("acme-owner", "acme-other"))
    await _insert_collision(
        db_path, run_id=RUN_ID, key="still-open", severity="error", resolution=None
    )

    conn = await connect_ro(db_path)
    try:
        with pytest.raises(UnresolvedFindingsError):
            await _refuse_unresolved_collisions(conn, RUN_ID)
    finally:
        await conn.close()
