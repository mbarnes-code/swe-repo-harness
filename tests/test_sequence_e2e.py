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

import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from fleet.cli import ExitCode, app
from fleet.graph.infer import EDGE_BASE_CONFIDENCE
from fleet.models.enums import BreakStrategy, ContractStatus, EdgeKind
from tests.test_cli import MODELS_YAML
from tests.test_scan_e2e import _fresh_db, _make_repo
from tests.test_workers_contracts import CYCLE_FLEET, IDENTITY_BINDING, IDENTITY_SOURCE, PROTO_ID

runner = CliRunner()

FLEET_YAML = """\
run:
  monorepo_path: ../acme-monorepo
  cache_dir: cache/
  work_dir: work/
concurrency:
  cpu_pool_workers: 1
preflight:
  # §11.3's floor is 50 GiB, which no developer machine — and no CI runner — clears with room to
  # spare, so a fixture that left it at the default would exit 9 before Phase 1 started. Lowered
  # rather than disabled, because 0 would mean "unchecked" and this fleet is four repos of a few
  # kilobytes: the gate really runs here, it simply passes. The refusal itself is asserted where
  # it belongs, against a floor no volume can clear (`test_cli.py`, `test_workers_scan.py`).
  min_free_bytes: 1048576
"""
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


def _base(root: Path) -> list[str]:
    return ["--config", str(root / "config" / "fleet.yaml"), "--db", str(root / "state/fleet.db")]


def _scan(root: Path, *extra: str) -> Any:
    return runner.invoke(
        app, [*_base(root), "scan", "--skip-classify", *extra], catch_exceptions=False
    )


def _sequence(root: Path, *extra: str) -> Any:
    return runner.invoke(
        app, [*_base(root), "--json", "sequence", *extra], catch_exceptions=False
    )


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


@pytest.mark.xfail(
    strict=True,
    reason=(
        "SPEC §12.8 residual, documented in "
        ".superpowers/sdd/round-HH-criteria-closure/task-2-report.md: "
        "graph/cycles.py::_materialize (called from break_cycles' 6c-H hoist) computes real "
        "CONTRACT_IMPL/CONTRACT_CONSUME DependencyEdge objects in memory for wave assignment, "
        "but cli.py::_sequence_impl never calls repository.insert_edges (or any other write) "
        "with them. insert_edges' ONLY production call site in the whole tree is "
        "cli.py::_persist_scan_edges, which runs at SCAN time — before any hoist exists — and "
        "builds its InferenceInput with no `contracts=` argument, so infer_contract_edges never "
        "fires there either. The two kinds are therefore never written to the persisted `edges` "
        "table by any real `fleet scan`/`fleet sequence` invocation today, even though the "
        "contract genuinely reaches status=HOISTED (proved by the test just above). This test "
        "pins the TARGET state; flip it to a real assertion (drop the xfail) once persistence "
        "is wired, and see the report for the recommended fix shape."
    ),
)
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
