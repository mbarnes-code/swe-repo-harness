"""Round VIII, §15.1 item 3, Wave 7.3 batch 35 — mutation-proof `cli.py`'s contract-extraction
plumbing (`src/fleet/cli.py` group G2, `_scan_impl`'s step 5b feeders): `_contract_repo_facts`,
`_contract_symbols`, `_committed_contracts`, `_contract_rows`. `_extract_contracts` itself (the
fifth function of this batch) is covered end to end through the real CLI in
`tests/test_contracts_criterion_scale.py` — see the new test added there for the disclosed
divergent-clause gap this batch was asked to close.

These four functions are ordinary SQL-projection helpers with no CLI surface of their own (they
are called only from inside `_extract_contracts`), so `tests/test_cli.py`/`tests/test_scan_e2e.py`
exercise them only incidentally, through whatever shape a given fixture's fleet happens to take.
Each test below calls the function DIRECTLY against a real (schema-fresh, file-backed) sqlite
database — the same "call the private function against a real db" pattern
`tests/test_sequence_e2e.py::test_a_failed_contract_survives_a_real_re_scan` already uses for
`_committed_contracts` — rather than driving a full `fleet scan`, because the branch each test
targets (a boolean derived from a second table, a kind filter, a status filter, a delete-then-
reinsert idempotency guard) is a property of the SQL itself, not of anything an end-to-end scan
would need to prove afresh.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import aiosqlite

from fleet.cli import _committed_contracts, _contract_repo_facts, _contract_rows, _contract_symbols
from fleet.models.enums import ContractKind, ContractStatus, SymbolKind
from fleet.models.graph import ContractNode
from fleet.settings import RepoEntry
from fleet.state.db import SCHEMA_PATH

RUN_ID = "11111111-1111-1111-1111-111111111111"


def _fresh_db(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.execute(
            "INSERT INTO runs (run_id, started_at, config_sha256, harness_version) "
            "VALUES (?, '2026-09-14T00:00:00Z', 'deadbeef', 'test')",
            (RUN_ID,),
        )
    finally:
        conn.close()
    return path


class _FakeCtx:
    """Stands in for `RunContext` for the one method `_contract_repo_facts` calls on it
    (`ctx.worktree(repo_id)` -> `work_dir / repo_id`, `orchestrator/context.py:302-304`).
    Constructing a real `RunContext` needs a writer/ledger/router this function never touches."""

    def worktree(self, repo_id: str) -> Path:
        return Path("/fake-work") / repo_id


# =======================================================================================
# 1 — `_contract_repo_facts`: the `publishes_coordinate` ladder-rung boolean (§3.1 5b (iv))
# =======================================================================================


def test_contract_repo_facts_flags_only_the_repo_that_actually_publishes_a_coordinate(
    tmp_path: Path,
) -> None:
    """`publishes_coordinate` must be True for exactly the repo(s) named in
    `coordinates.owner_repo_id`, and False (not merely absent) for every other repo in the fleet —
    the tie-break rung (ii) of the ownership ladder reads this per repo, not as a fleet-wide flag.

    Mutation target: `entry.name in publishers` (`cli.py`) flipped to `not in` would make every
    non-publisher look like a publisher and vice versa; this test's two-repo fixture (one real
    publisher, one non-publisher) is the minimum shape that distinguishes "flag reversed" from
    "flag always True"/"flag always False".
    """
    db_path = tmp_path / "fleet.db"
    _fresh_db(db_path)
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO repos (repo_id, name, url, commit_count, updated_at) "
            "VALUES ('acme-identity', 'acme-identity', 'x', 5, '2026-09-14T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO repos (repo_id, name, url, commit_count, updated_at) "
            "VALUES ('acme-checkout', 'acme-checkout', 'y', 0, '2026-09-14T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO coordinates (coord_key, ecosystem, name, owner_repo_id, first_seen_at) "
            "VALUES ('npm::@acme/identity', 'npm', '@acme/identity', 'acme-identity', "
            "'2026-09-14T00:00:00Z')"
        )
    finally:
        conn.close()

    fleet = (
        RepoEntry(name="acme-identity", url="x", owns=("proto:acme.identity.v1",)),
        RepoEntry(name="acme-checkout", url="y"),
    )

    async def _read() -> tuple:
        async with aiosqlite.connect(db_path) as aconn:
            return await _contract_repo_facts(_FakeCtx(), aconn, fleet)

    facts = asyncio.run(_read())
    by_id = {f.repo_id: f for f in facts}

    assert by_id["acme-identity"].publishes_coordinate is True, by_id["acme-identity"]
    assert by_id["acme-identity"].commit_count == 5
    assert by_id["acme-identity"].owns == ("proto:acme.identity.v1",)
    assert by_id["acme-identity"].worktree_path == str(Path("/fake-work/acme-identity"))

    assert by_id["acme-checkout"].publishes_coordinate is False, by_id["acme-checkout"]
    assert by_id["acme-checkout"].commit_count == 0
    assert by_id["acme-checkout"].owns == ()


# =======================================================================================
# 2 — `_contract_symbols`: the `CONTRACT_SYMBOL_KINDS` filter
# =======================================================================================


def test_contract_symbols_excludes_kinds_outside_the_contract_symbol_kinds_table(
    tmp_path: Path,
) -> None:
    """5b's own docstring: "the `symbols` rows 5b joins on, and only those". A `function`-kind
    symbol (not one of `CONTRACT_SYMBOL_KINDS`) must never reach the worker even though it lives
    in the same `symbols` table, same run, same repo.

    Mutation target: dropping the `WHERE kind IN (...)` clause (or widening it to select every
    kind) would let the `function`-kind row through; this test's fixture carries one in-scope
    (`module`) and one out-of-scope (`function`) row for the SAME repo/path prefix so a mutation
    that selects everything is caught by an extra row, not by a wrong repo/path.
    """
    db_path = tmp_path / "fleet.db"
    _fresh_db(db_path)
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO repos (repo_id, name, url, updated_at) "
            "VALUES ('acme-identity', 'acme-identity', 'x', '2026-09-14T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO symbols (run_id, repo_id, fqn, kind, path, line, language, "
            "    is_definition, exported) "
            "VALUES (?, 'acme-identity', 'acme.identity.v1', 'module', 'identity.proto', 1, "
            "    'proto', 1, 1)",
            (RUN_ID,),
        )
        conn.execute(
            "INSERT INTO symbols (run_id, repo_id, fqn, kind, path, line, language, "
            "    is_definition, exported) "
            "VALUES (?, 'acme-identity', 'helper', 'function', 'helper.py', 3, 'python', 1, 0)",
            (RUN_ID,),
        )
    finally:
        conn.close()

    async def _read() -> tuple:
        async with aiosqlite.connect(db_path) as aconn:
            return await _contract_symbols(aconn, RUN_ID)

    symbols = asyncio.run(_read())
    kinds = {s.kind for s in symbols}
    assert kinds == {SymbolKind.MODULE}, (
        f"only the CONTRACT_SYMBOL_KINDS row may survive, got kinds={kinds}"
    )
    assert len(symbols) == 1, symbols


# =======================================================================================
# 3 — `_committed_contracts`: the committed-status membership filter (§3.1 "re-runnable by
# construction"; ADR-0123's carry-over feeder)
# =======================================================================================


def test_committed_contracts_excludes_a_live_extractable_row(tmp_path: Path) -> None:
    """The complementary half of `tests/test_sequence_e2e.py::
    test_a_failed_contract_survives_a_real_re_scan`'s FAILED-inclusion proof: that test proves the
    status list is not too NARROW (FAILED must be selected); this test proves it is not too WIDE
    — a live `EXTRACTABLE` row (still mid-ladder, not yet committed to anything) must NOT be
    treated as `committed` alongside a genuinely `HOISTED` one, or `carry_over_committed` would
    re-apply a hoist decision that was never actually made.

    Mutation target: widening `status IN ('HOISTED','MIGRATED','FORBIDDEN','FAILED')` to also
    admit `'EXTRACTABLE'` (an easy typo-shaped mistake next to a real `FAILED`/`FORBIDDEN`
    addition, exactly the shape ADR-0123's own history records happening once already) is caught
    by this fixture's two rows, one of each membership.
    """
    db_path = tmp_path / "fleet.db"
    _fresh_db(db_path)
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO repos (repo_id, name, url, updated_at) "
            "VALUES ('acme-identity', 'acme-identity', 'x', '2026-09-14T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO contracts (run_id, contract_id, kind, identifier, owning_repo_id, "
            "    extractable, hoist_target_path, status, detected_at) "
            "VALUES (?, 'proto:acme.identity.v1', 'PROTO', 'acme.identity.v1', 'acme-identity', "
            "    1, 'third_party/acme/identity/v1', 'HOISTED', '2026-09-14T00:00:00Z')",
            (RUN_ID,),
        )
        conn.execute(
            "INSERT INTO contracts (run_id, contract_id, kind, identifier, owning_repo_id, "
            "    extractable, hoist_target_path, status, detected_at) "
            "VALUES (?, 'proto:acme.billing.v1', 'PROTO', 'acme.billing.v1', 'acme-identity', "
            "    1, 'third_party/acme/billing/v1', 'EXTRACTABLE', '2026-09-14T00:00:00Z')",
            (RUN_ID,),
        )
    finally:
        conn.close()

    async def _read() -> tuple:
        async with aiosqlite.connect(db_path) as aconn:
            return await _committed_contracts(aconn, RUN_ID)

    committed = asyncio.run(_read())
    ids = {n.contract_id for n in committed}
    assert ids == {"proto:acme.identity.v1"}, (
        f"only the HOISTED row is 'committed'; the EXTRACTABLE row must be excluded, got {ids}"
    )
    assert committed[0].status == ContractStatus.HOISTED


# =======================================================================================
# 4 — `_contract_rows`: DELETE-then-reinsert idempotency (§11.7) — a contract dropped from a
# rebuilt graph must not survive as a stale row
# =======================================================================================


def _node(contract_id: str, identifier: str) -> ContractNode:
    return ContractNode(
        contract_id=contract_id,
        kind=ContractKind.PROTO,
        identifier=identifier,
        owning_repo_id="acme-identity",
        extractable=True,
        hoist_target_path=f"third_party/{identifier.replace('.', '/')}",
        status=ContractStatus.EXTRACTABLE,
    )


class _Output:
    """Just enough of `ContractsOutput`'s shape for `_contract_rows`, which reads only
    `.contracts` and `.collisions`."""

    def __init__(self, contracts: tuple[ContractNode, ...]) -> None:
        self.contracts = contracts
        self.collisions: tuple = ()


def test_contract_rows_deletes_a_no_longer_detected_contract_on_rebuild(tmp_path: Path) -> None:
    """A graph rebuild that no longer detects contract A (only B this time) must leave `contracts`
    holding B alone for this `run_id` — the docstring's own claim ("a contract that no longer
    exists must not survive the rebuild"). Two different `contract_id`s are used (not the same id
    with different fields) so a broken DELETE surfaces as row ACCUMULATION rather than an
    `IntegrityError` on the `(run_id, contract_id)` primary key — the discriminating shape, since a
    crash would fail loud on its own without telling us whether DELETE ran at all.

    Mutation target: the `DELETE FROM contracts WHERE run_id = ?` statement disabled (e.g.
    tautologically-false WHERE) leaves stale rows across calls.
    """
    db_path = tmp_path / "fleet.db"
    _fresh_db(db_path)
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO repos (repo_id, name, url, updated_at) "
            "VALUES ('acme-identity', 'acme-identity', 'x', '2026-09-14T00:00:00Z')"
        )
    finally:
        conn.close()

    node_a = _node("proto:acme.identity.v1", "acme.identity.v1")
    node_b = _node("proto:acme.billing.v1", "acme.billing.v1")

    async def _apply(output: _Output) -> None:
        async with aiosqlite.connect(db_path) as aconn:
            await _contract_rows(RUN_ID, output, "2026-09-14T00:00:00Z")(aconn)  # type: ignore[arg-type]
            await aconn.commit()

    asyncio.run(_apply(_Output((node_a,))))
    first = (
        sqlite3.connect(db_path)
        .execute("SELECT contract_id FROM contracts WHERE run_id = ?", (RUN_ID,))
        .fetchall()
    )
    assert first == [("proto:acme.identity.v1",)], first

    asyncio.run(_apply(_Output((node_b,))))
    second = (
        sqlite3.connect(db_path)
        .execute("SELECT contract_id FROM contracts WHERE run_id = ?", (RUN_ID,))
        .fetchall()
    )
    assert second == [("proto:acme.billing.v1",)], (
        f"contract A must be GONE after a rebuild that no longer detects it, got {second}"
    )
