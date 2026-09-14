"""Behaviour tests for `src/fleet/workers/contracts.py` — §3.1 step 5b, contract extraction.

Every fleet in this file is a **real tree on disk**, and every `SymbolRef` handed to the worker
comes out of the real `symbolindex.scan_file`. That is deliberate: 5b's whole input surface is
"paths, plus the two bounded facts step 4 recorded per file", so a test that hand-wrote its symbol
rows would be testing a fiction of the joins rather than the joins. The proto packages, the
`@generated` markers and the `**/generated/**` layouts below are the ones a real fleet has.

What each test is for:

* `test_the_worker_is_instantiable_and_re_entry_is_idempotent` — `BaseWorker.preconditions_hold`
  is abstract *on purpose* (a defaulted `return True` IS the blind replay it exists to forbid), so
  a subclass that forgets it cannot be constructed at all. The second half runs 5b twice over one
  fleet and pins byte-identical rows: `contracts` is keyed `(run_id, contract_id)` and rebuilt
  whole, so a second scan that produced a second row per vendored copy would be the nine-rows-for-
  one-proto defect ADR-0019 exists to prevent.
* `test_one_proto_two_consumers_collapses_to_a_single_owned_node` — the shape §3.1 5b (iii) names.
  One package carried by three repos is ONE node with one owner; the vendored copy does not vote
  for ownership (5b iv) and the generated copy is evidence of consumption, never of ownership
  (5b ii).
* the four `_ladder_owner` tests — one per rung of §3.1 5b (iv)'s ownership ladder
  (`graph/collisions.py::ownership_rank`), which nothing in this suite used to assert. They are
  the reason `_payload` takes `commit_counts`/`publishes` and the reason `LADDER_FLEET` exists:
  with `SHARED_FLEET` the ladder is not thinly covered, it is **unreachable** — one carrier is
  vendored and one generated, so `_owner`'s `eligible` filter leaves a single candidate, and a
  one-element sort returns that candidate whatever the sort key says. Every rung was constant or
  unreached, and inverting the depth rung left this file entirely green. Each of the four is the
  unique discriminator of at least one mutation of the rung it names; the matrix is in the round
  report, and the fixture docstrings say which mutation each one answers.
* `test_hoisting_the_discovered_contract_dissolves_the_two_repo_cycle` — **the payoff.** Discovery
  and cycle-breaking were each green in isolation and had never been run against each other:
  `infer_contract_edges` could always convert a `ContractNode`, and nothing produced one. This
  test runs discovery → `break_cycles` → `CONTRACT_HOIST` over one fixture and asserts the SCC
  becomes trivial with **no edge broken**. If 5b ever stops filling `generated_paths`, the
  retarget in `cycles._materialize` silently matches nothing and this is the only test that fails.
* `test_a_contract_no_repo_owns_is_not_invented_as_a_node` — every carrier generated ⇒ the package
  is published outside the fleet. Inventing a node for it would hand the sequencer a member no
  repo can ever migrate.
* `test_a_proto_beside_implementation_code_is_rejected_with_its_reason` — `extractable = 0` is
  useless without the predicate that produced it; an operator reading `fleet contracts inspect`
  has to see *why*, and the order of the predicates is itself the contract (§3.1 5b vi).
* `test_a_symbol_only_consumer_gets_no_contract_consume_edge` — pins a KNOWN, un-fabricated gap
  rather than hiding it: `ContractNode` carries no evidence path for a consumer detected by symbol
  reference alone, so `infer_contract_edges` cannot emit its edge. The test asserts the honest
  state (recorded as a consumer, no edge) so the day the model gains the field, it fails loudly.
* `test_discovery_is_identical_under_a_different_hash_seed` — a real subprocess, because
  `PYTHONHASHSEED` is fixed at interpreter start. 5b feeds `contracts.contract_id` into `edge_key`
  and into `run_digest` (§11.6); set-iteration order reaching the output would make two runs of
  one fleet disagree.
* `test_fleet_scan_extracts_contracts_through_the_cli` — `fleet scan` used to REFUSE when
  contracts were enabled. This drives the real command over a real fleet and asserts the rows land
  in the database, because a verb that prints a summary it did not persist is the failure an
  end-to-end test is for.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from typer.testing import CliRunner

import fleet
from fleet.cli import ExitCode, app
from fleet.graph.build import build_graph
from fleet.graph.cycles import break_cycles
from fleet.graph.infer import InferenceInput, ManifestDependency, OwnerIndex, infer_edges
from fleet.llm.client import CallBudget
from fleet.models.enums import (
    BreakStrategy,
    ContractKind,
    ContractStatus,
    Ecosystem,
    EdgeKind,
    FailureClass,
    NodeKind,
    SymbolKind,
)
from fleet.models.graph import ContractNode, GraphNode, SymbolRef
from fleet.models.repo import Coordinate, ManifestRef, RawDependency
from fleet.settings import ContractsSection, GraphSection
from fleet.workers.base import WorkerContext
from fleet.workers.contracts import (
    ContractExtractionWorker,
    ContractsInput,
    ContractsOutput,
    RepoFacts,
    carry_over_committed,
    discover_contracts,
)
from fleet.workers.interrogate import is_ignored
from fleet.workers.symbolindex import scan_file
from tests.test_cli import MODELS_YAML
from tests.test_scan_e2e import _fresh_db, _make_repo

REPO_ROOT = Path(fleet.__file__).resolve().parents[2]
runner = CliRunner()

# ---------------------------------------------------------------------------------------
# fixture content — a real proto package, a real generated binding, real implementation code
# ---------------------------------------------------------------------------------------

IDENTITY_PROTO = """\
syntax = "proto3";

package acme.identity.v1;

service IdentityService {
  rpc GetUser (GetUserRequest) returns (User);
}

message User {
  string id = 1;
}

message GetUserRequest {
  string id = 1;
}
"""

VENDOR_PROTO = """\
syntax = "proto3";

package vendorapi.v1;

message Invoice {
  string id = 1;
}
"""


def _binding(package: str, *, imports: str | None = None) -> str:
    """A checked-in generated binding, marker and all — §3.1 5b (ii)'s `@generated` header."""
    head = "// Code generated by protoc. DO NOT EDIT.\n// @generated\n"
    body = f"export class {package.split('.')[-2].title()}Client {{}}\n"
    return head + (f"import {{ Client }} from '{imports}';\n" if imports else "") + body


IDENTITY_BINDING = "src/api/acme/identity/v1/identity_pb.ts"
"""Checked-in generated code in a directory NOT named `generated/`, which is the common real
shape and the interesting one: `graph/infer.py` discounts an edge whose evidence matches
`scan.generated_globs` to 0.48 — below `graph.min_confidence` — so a binding under
`src/generated/` produces an edge that never orders and therefore never forms a cycle. The
`// Code generated by protoc` marker step 4 records is what makes THIS file both a full-confidence
ordering edge and a retargetable one."""
IDENTITY_SOURCE = "proto/acme/identity/v1/identity.proto"


def _pkg(name: str, deps: Mapping[str, str] | None = None) -> str:
    body: dict[str, object] = {"name": name, "version": "1.0.0"}
    if deps:
        body["dependencies"] = dict(deps)
    return json.dumps(body, indent=2)


# ---------------------------------------------------------------------------------------
# building a payload from a real tree
# ---------------------------------------------------------------------------------------


def _write_fleet(root: Path, repos: Mapping[str, Mapping[str, str]]) -> None:
    for repo_id, files in sorted(repos.items()):
        for rel, text in sorted(files.items()):
            target = root / repo_id / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")


def _real_symbols(root: Path, repo_id: str, files: Mapping[str, str]) -> list[SymbolRef]:
    """Step 4's own extractor over step 4's own files. Nothing here invents a `symbols` row."""
    config = ContractsSection()
    out: list[SymbolRef] = []
    for rel in sorted(files):
        scan = scan_file(
            str(root / repo_id),
            rel,
            repo_id,
            2_097_152,
            {},
            {},
            {},
            tuple(config.generated_markers),
        )
        out.extend(scan.symbols)
    return out


def _payload(
    root: Path,
    repos: Mapping[str, Mapping[str, str]],
    *,
    owns: Mapping[str, tuple[str, ...]] | None = None,
    committed: Sequence[ContractNode] = (),
    commit_counts: Mapping[str, int] | None = None,
    publishes: Mapping[str, bool] | None = None,
    blob_shas: Mapping[str, str] | None = None,
) -> ContractsInput:
    """`commit_counts` and `publishes` override ladder rungs (i) and (iii) PER REPO.

    They exist because without them the ownership ladder is not merely under-tested, it is
    unreachable: every `RepoFacts` row this helper built carried `commit_count=1` and
    `publishes_coordinate=True`, so two of `ownership_rank`'s four rungs were constant by
    construction and no fixture in this file could express a defect in either. The defaults below
    are the values that were hardcoded, so every fixture that does not ask for an override sees
    exactly the payload it saw before.

    `blob_shas` feeds `ContractsInput.blob_shas` directly — real `fleet scan` never populates it
    (no `ls-tree` capture exists at preflight, see `workers/contracts.py`'s module docstring), but
    the field exists precisely to be fed a listing the moment one is available, and the discovery
    code already honours it. A test supplying it here is exercising that documented input surface
    directly, not fabricating capture infrastructure.
    """
    _write_fleet(root, repos)
    symbols: list[SymbolRef] = []
    for repo_id, files in sorted(repos.items()):
        symbols.extend(_real_symbols(root, repo_id, files))
    scan = _scan_defaults()
    return ContractsInput(
        repos=tuple(
            RepoFacts(
                repo_id=repo_id,
                worktree_path=str(root / repo_id),
                commit_count=(commit_counts or {}).get(repo_id, 1),
                owns=(owns or {}).get(repo_id, ()),
                publishes_coordinate=(publishes or {}).get(repo_id, True),
            )
            for repo_id in sorted(repos)
        ),
        symbols=tuple(symbols),
        committed=tuple(committed),
        blob_shas=dict(blob_shas or {}),
        config=ContractsSection(),
        ignore_globs=scan["ignore"],
        vendor_globs=scan["vendor"],
        generated_globs=scan["generated"],
        min_extraction_confidence=GraphSection().min_extraction_confidence,
    )


def _scan_defaults() -> dict[str, tuple[str, ...]]:
    from fleet.settings import ScanSection

    section = ScanSection()
    return {
        "ignore": tuple(section.ignore_globs),
        "vendor": tuple(section.vendor_globs),
        "generated": tuple(section.generated_globs),
    }


class _RecordingLog:
    """A logger that RECORDS instead of writing.

    Not a bare sentinel (which would make this file green against a worker that never reached its
    own logging line) and not the process-wide structlog pipeline either: `obs/log.py` tees to a
    file handle installed per `fleet` invocation, and a unit test that logs into whichever handle
    a previous test left behind fails with `I/O operation on closed file` — the exact defect
    `_scan_impl`'s `log_configure()` comment records. Recording makes the log an assertion.
    """

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def info(self, event: str, **fields: object) -> None:
        self.events.append((event, fields))

    def warning(self, event: str, **fields: object) -> None:
        self.events.append((event, fields))


def _ctx(log: _RecordingLog | None = None) -> WorkerContext:
    """Everything 5b may touch, and nothing it may not: `db`, `llm`, `router` and `limits` are
    inert sentinels, so a worker that reached for a model or a database would raise here."""
    sentinel: Any = object()
    return WorkerContext(
        run_id=uuid4(),
        repo_id="contracts",
        attempt=1,
        workdir="/nonexistent/worktree",
        lease_owner="test:test:1:boot",
        lease_fence=1,
        deadline=1e12,
        cancel=asyncio.Event(),
        budget=CallBudget(remaining_tokens=0, remaining_usd=0.0, deadline=1e12),
        db=sentinel,
        llm=sentinel,
        router=sentinel,
        limits=sentinel,
        log=cast("Any", log or _RecordingLog()),
    )


def _run(payload: ContractsInput) -> ContractsOutput:
    worker = ContractExtractionWorker()
    log = _RecordingLog()
    result = asyncio.run(worker.run(_ctx(log), payload))
    assert result.status == "ok", result.error
    assert result.output is not None
    assert [event for event, _ in log.events] == ["contracts.extracted"], (
        "5b must report its own totals; a step that logs nothing is one nobody can audit"
    )
    return result.output


def _ctx_with_deadline(*, deadline: float, cancelled: bool) -> WorkerContext:
    """`_ctx()` pins `deadline=1e12` and never sets `cancel` -- these two knobs are what the
    cancelled-vs-timeout discrimination in `run()` reads, so they need a way in from a test."""
    sentinel: Any = object()
    event = asyncio.Event()
    if cancelled:
        event.set()
    return WorkerContext(
        run_id=uuid4(),
        repo_id="contracts",
        attempt=1,
        workdir="/nonexistent/worktree",
        lease_owner="test:test:1:boot",
        lease_fence=1,
        deadline=deadline,
        cancel=event,
        budget=CallBudget(remaining_tokens=0, remaining_usd=0.0, deadline=deadline),
        db=sentinel,
        llm=sentinel,
        router=sentinel,
        limits=sentinel,
        log=cast("Any", _RecordingLog()),
    )


def test_cancelled_before_dispatch_reports_status_cancelled_not_an_attempt() -> None:
    """A genuine `ctx.cancelled()` is an operator decision and stays `cancelled` (not an attempt)
    -- `TRANSIENT_INFRA`, matching the sibling scan workers' convention (classify/interrogate/
    symbolindex/baseline). Before this worker's own fix (`334edeb`), `run()` had NO cancellation
    or deadline check at all and would run `_path_universe`/`discover_contracts` regardless."""
    result = asyncio.run(
        ContractExtractionWorker().run(
            _ctx_with_deadline(deadline=1e12, cancelled=True), ContractsInput()
        )
    )
    assert result.status == "cancelled"
    assert result.error is not None
    assert result.error.failure_class == FailureClass.TRANSIENT_INFRA


def test_expired_before_dispatch_reports_status_timeout_and_charges_an_attempt() -> None:
    """A genuine `ctx.expired()` (deadline already passed, no operator cancel) must be a
    chargeable `timeout` result (§11) -- conflating it with `cancelled` would let a repo whose
    contract extraction always times out loop forever instead of escalating to
    REQUIRES_HUMAN_INTERVENTION after 3 tries."""
    result = asyncio.run(
        ContractExtractionWorker().run(
            _ctx_with_deadline(deadline=-1.0, cancelled=False), ContractsInput()
        )
    )
    assert result.status == "timeout"
    assert result.error is not None
    assert result.error.failure_class == FailureClass.TIMEOUT


def _stable(output: ContractsOutput) -> str:
    """The output minus the one wall-clock field. `detected_at` is stamped per construction and is
    excluded from every identity in this system for the same reason `edge_key` excludes it."""
    return json.dumps(
        {
            "contracts": [
                node.model_dump(mode="json", exclude={"detected_at"}) for node in output.contracts
            ],
            "collisions": [
                row.model_dump(mode="json", exclude={"detected_at"}) for row in output.collisions
            ],
            "unidentifiable": list(output.unidentifiable_idl_paths),
        },
        sort_keys=True,
    )


def _by_id(output: ContractsOutput) -> dict[str, ContractNode]:
    return {node.contract_id: node for node in output.contracts}


# ---------------------------------------------------------------------------------------
# the fleets
# ---------------------------------------------------------------------------------------

SHARED_FLEET: Mapping[str, Mapping[str, str]] = {
    # the owner: the hand-written IDL, alone in its directory
    "acme-identity": {
        IDENTITY_SOURCE: IDENTITY_PROTO,
        "package.json": _pkg("@acme/identity"),
        "src/index.ts": "export const version = '1';\n",
    },
    # a vendored copy — a carrier, but never the owner (§3.1 5b iv)
    "acme-billing": {
        "third_party/acme/identity/v1/identity.proto": IDENTITY_PROTO,
        "package.json": _pkg("@acme/billing"),
    },
    # generated bindings — evidence of consumption only (§3.1 5b ii)
    "acme-reporting": {
        IDENTITY_BINDING: _binding("acme.identity.v1"),
        "package.json": _pkg("@acme/reporting"),
    },
}

CYCLE_FLEET: Mapping[str, Mapping[str, str]] = {
    "acme-identity": {
        IDENTITY_SOURCE: IDENTITY_PROTO,
        # the manifest edge that closes the cycle: the owner depends on a consumer
        "package.json": _pkg("@acme/identity", {"@acme/billing": "^1.0.0"}),
        "src/index.ts": "export const version = '1';\n",
    },
    "acme-billing": {
        IDENTITY_BINDING: _binding("acme.identity.v1", imports="@acme/identity"),
        "package.json": _pkg("@acme/billing"),
    },
    "acme-reporting": {
        IDENTITY_BINDING: _binding("acme.identity.v1"),
        "package.json": _pkg("@acme/reporting"),
    },
}

LADDER_DEEP = "proto/acme/identity/v1/identity.proto"
"""`_Carrier.depth` is `path.count("/")` — 4 here."""
LADDER_SHALLOW = "contracts/identity.proto"
"""The same package, one directory down — depth 1, and matched by no vendor or generated glob."""

LADDER_FLEET: Mapping[str, Mapping[str, str]] = {
    # TWO carriers that are neither vendored nor generated, at DIFFERING depths. That is what
    # `SHARED_FLEET` cannot supply: its second carrier sits under `third_party/`, so `_owner`'s
    # `eligible` filter in `workers.contracts._owner` leaves one element and the sort decides
    # nothing. Here `eligible` has two, the two disagree on depth, and `repo_id` — the LAST rung —
    # would crown the other one, so each rung's own answer is visible in `owning_repo_id`.
    "acme-identity": {
        LADDER_DEEP: IDENTITY_PROTO,
        "package.json": _pkg("@acme/identity"),
    },
    "acme-platform": {
        LADDER_SHALLOW: IDENTITY_PROTO,
        "package.json": _pkg("@acme/platform"),
    },
    # a consumer, so the node clears `min_consumers` and is a real EXTRACTABLE row
    "acme-reporting": {
        IDENTITY_BINDING: _binding("acme.identity.v1"),
        "package.json": _pkg("@acme/reporting"),
    },
}

TIED_DEPTH_FLEET: Mapping[str, Mapping[str, str]] = {
    **LADDER_FLEET,
    # the same contract at the same depth in both carriers, so rung (ii) cannot answer and the
    # contest falls through to whichever lower rung the test varies.
    "acme-platform": {LADDER_DEEP: IDENTITY_PROTO, "package.json": _pkg("@acme/platform")},
}


PROTO_ID = "proto:acme.identity.v1"


# =======================================================================================
# 1 — the worker itself
# =======================================================================================


def test_the_worker_is_instantiable_and_re_entry_is_idempotent(tmp_path: Path) -> None:
    """It really overrides `preconditions_hold`, and a second pass rebuilds identical rows.

    Why both in one test: they are the same property seen twice. `preconditions_hold` is abstract
    precisely so no worker inherits "yes, replay me" by accident, and 5b's answer is an unusual
    *unconditional* yes — which is only true because the step is a whole-run REBUILD keyed
    `(run_id, contract_id)`. If the rebuild were an accumulation, the honest answer would be
    `False` and this fleet's one vendored proto would become two rows on the second scan.
    """
    payload = _payload(tmp_path, SHARED_FLEET)
    worker = ContractExtractionWorker()  # constructs ⇒ no abstract method left unimplemented
    assert asyncio.run(worker.preconditions_hold(_ctx(), payload)) is True

    first, second = _run(payload), _run(payload)
    assert _stable(first) == _stable(second)
    assert [node.contract_id for node in first.contracts] == [PROTO_ID]


def test_a_hoisted_contract_survives_the_rebuild_it_is_not_part_of(tmp_path: Path) -> None:
    """A `HOISTED` row is re-applied after the whole-run DELETE, with its `hoist_target_path`.

    Why it matters more than it looks: the hoist is already committed in git and named by an open
    PR. A rebuild that recomputed it back to `EXTRACTABLE` would let 6c-H rank and re-propose a
    contract whose files have already moved, and §3.1's exit condition counts
    `contracts WHERE status IN ('HOISTED','MIGRATED')` against `wave_members`.
    """
    payload = _payload(tmp_path, SHARED_FLEET)
    fresh = _run(payload).contracts
    hoisted = [
        node.model_copy(update={"status": ContractStatus.HOISTED, "hoist_target_path": "proto/x"})
        for node in fresh
    ]
    carried = carry_over_committed(fresh, hoisted)
    assert [n.status for n in carried] == [ContractStatus.HOISTED]
    assert carried[0].hoist_target_path == "proto/x"

    gone = carry_over_committed([], hoisted)
    assert [n.contract_id for n in gone] == [PROTO_ID], "a hoisted contract is never dropped"


def test_a_failed_contract_survives_the_rebuild_it_is_not_part_of(tmp_path: Path) -> None:
    """A `FAILED` row is carried across an EMPTY fresh set, exactly like `HOISTED` — never like
    `REJECTED`, which is deliberately dropped and re-derived (§12.31 case (ii), Leg C2, round VI
    task 66, ADR-0123).

    The measured judgment call this test proves: `FAILED`'s underlying hoist commit is not yet
    reverted (Leg D doesn't exist), so the code is physically in the monorepo just like a `HOISTED`
    row's — a post-hoist rescan can no longer see the ORIGINAL duplication (the code already moved
    into the shared package), so `fresh` legitimately comes back EMPTY for this contract_id, the
    same shape `test_a_hoisted_contract_survives_the_rebuild_it_is_not_part_of` above already
    exercises for `HOISTED` via its own `gone = carry_over_committed([], hoisted)` case. Before
    `carry_over_committed`'s widening (this task), an EMPTY fresh set for a `FAILED` row made it
    vanish from `out` entirely — the identical "vanished row" lie the function's own docstring
    warns about for `HOISTED`/`MIGRATED`.

    **This test alone does NOT prove the decision is reachable in production** (fix-round finding
    C1, controller review): it constructs `committed` nodes directly, bypassing
    `cli._committed_contracts` — the ONLY production feeder of this function's `committed`
    argument — entirely. `_committed_contracts`'s own SQL `status IN (...)` list had to be widened
    too, or a real `FAILED` row would never reach this function at all. See
    `tests/test_sequence_e2e.py::test_a_failed_contract_survives_a_real_re_scan` for the sibling
    proof that drives the real path (a real `fleet scan`/`fleet sequence`, then `_committed_
    contracts` called directly against the resulting database).
    """
    payload = _payload(tmp_path, SHARED_FLEET)
    fresh = _run(payload).contracts
    failed = [
        node.model_copy(
            update={
                "status": ContractStatus.FAILED,
                "hoist_target_path": "proto/x",
                "status_detail": "hoist_broke_owner",
            }
        )
        for node in fresh
    ]
    carried = carry_over_committed(fresh, failed)
    assert [n.status for n in carried] == [ContractStatus.FAILED]
    assert carried[0].hoist_target_path == "proto/x"

    gone = carry_over_committed([], failed)
    assert [n.contract_id for n in gone] == [PROTO_ID], "a failed contract is never dropped"


# =======================================================================================
# 2 — discovery, identity and ownership
# =======================================================================================


def test_one_proto_two_consumers_collapses_to_a_single_owned_node(tmp_path: Path) -> None:
    """Three repos carry `acme.identity.v1`; the fleet gets ONE node, owned by the author.

    §3.1 5b (iii): identity is `(kind, identifier)`, so nine vendored copies are one row, never
    nine. §3.1 5b (iv): the vendored carrier loses ownership to the repo whose copy is neither
    vendored nor generated — otherwise whichever repo sorted first would "own" an interface it
    only copied, and the hoist would migrate the wrong directory.
    """
    output = _run(_payload(tmp_path, SHARED_FLEET))
    nodes = _by_id(output)
    assert list(nodes) == [PROTO_ID], "one package carried three ways must be one node"

    node = nodes[PROTO_ID]
    assert node.kind is ContractKind.PROTO
    assert node.identifier == "acme.identity.v1"
    assert node.owning_repo_id == "acme-identity"
    assert node.consumer_repo_ids == ["acme-billing", "acme-reporting"]
    assert {entry["repo_id"] for entry in node.source_paths} == {"acme-identity", "acme-billing"}
    assert [entry["path"] for entry in node.generated_paths] == [IDENTITY_BINDING]
    assert node.status is ContractStatus.EXTRACTABLE
    assert node.hoist_target_path == "proto/acme/identity/v1"


def test_the_multi_carrier_contract_writes_its_step_8_collision_row(tmp_path: Path) -> None:
    """Two carriers ⇒ one `CONTRACT` collision row naming both and recording the applied rule.

    §3.1 step 8 makes this the *same* contest as `COORDINATE`, resolved by the *same* `owns:`
    ladder — an operator must be able to see that two repos ship the same published interface
    before anything merges, which is the whole reason the audit runs before any transformation.
    """
    output = _run(_payload(tmp_path, SHARED_FLEET))
    assert len(output.collisions) == 1
    collision = output.collisions[0]
    assert (collision.kind, collision.key) == ("CONTRACT", PROTO_ID)
    assert collision.repo_ids == ["acme-billing", "acme-identity"]
    assert collision.resolution is not None
    assert "acme-identity" in collision.resolution


def test_an_owns_hint_beats_the_ladder_and_is_recorded_in_the_confidence(tmp_path: Path) -> None:
    """`owns:` in `config/repos.yaml` wins outright, and its absence costs `owner_fallback`.

    Why the modifier and not just the choice: §3.1 5b (vi) requires the score to be
    reconstructible from `confidence_factors` alone. An operator who declared the owner gets a
    higher-confidence extraction than one the ladder guessed at, and the row has to say which.
    """
    hinted = _run(_payload(tmp_path / "a", SHARED_FLEET, owns={"acme-billing": (PROTO_ID,)}))
    guessed = _run(_payload(tmp_path / "b", SHARED_FLEET))

    assert _by_id(hinted)[PROTO_ID].owning_repo_id == "acme-billing"
    assert "owner_fallback" not in _by_id(hinted)[PROTO_ID].confidence_factors
    assert _by_id(guessed)[PROTO_ID].confidence_factors["owner_fallback"] == 0.9

    node = _by_id(guessed)[PROTO_ID]
    product = 0.9
    for value in node.confidence_factors.values():
        product *= value
    assert node.extraction_confidence == pytest.approx(product)


def _ladder_owner(
    tmp_path: Path, repos: Mapping[str, Mapping[str, str]], **facts: Any
) -> ContractNode:
    """Run 5b and return the one contract node — after proving the ladder had a real contest.

    The candidate assertion is not decoration. The defect these tests exist to catch is a ladder
    that never runs: with one eligible carrier `sorted()` returns it whatever the key says, and
    every assertion on `owning_repo_id` below would hold under any mutation of any rung. So the
    candidate set is checked before the winner is.

    It is derived the way `_owner` derives `eligible` — `source_paths` minus the vendored copies —
    and NOT as the bare set of `source_paths` repo ids, which was this guard's first form and was
    blind by construction: `source_paths` carries every non-generated carrier INCLUDING vendored
    ones, so it reads the same whether the rival carrier is eligible or not. Measured: with both
    rivals moved under `third_party/`, the bare-repo-id form stayed green on the guard and let
    `test_repo_id_settles_a_contest_the_other_rungs_all_tie` pass over a one-element sort — the
    exact state this module is here to make impossible. This form fails all four.
    """
    node = _by_id(_run(_payload(tmp_path, repos, **facts)))[PROTO_ID]
    vendor_globs = _scan_defaults()["vendor"]
    eligible = {
        entry["repo_id"]
        for entry in node.source_paths
        if not is_ignored(str(entry["path"]), vendor_globs)
    }
    assert eligible == {"acme-identity", "acme-platform"}, (
        f"the ownership ladder must have two candidates to rank, got {sorted(eligible)}"
    )
    return node


def test_the_busier_carrier_wins_the_commit_count_rung(tmp_path: Path) -> None:
    """Rung (iii): equal depth, so the repo with MORE commits owns it.

    `ownership_rank` negates `commit_count` because the ladder sorts ascending and this rung wants
    the largest value first — a rung that is easy to write as an unnegated `commit_count` and then
    silently mean its opposite. `acme-identity` keeps the default 1 and sorts first by `repo_id`,
    so it wins under both a dropped rung and an unnegated one.

    Quantity: `owning_repo_id`. The candidates tie on rungs (i), (ii) and differ only in
    `commit_count`, so nothing but this rung can order them.
    """
    node = _ladder_owner(tmp_path, TIED_DEPTH_FLEET, commit_counts={"acme-platform": 9})
    assert node.owning_repo_id == "acme-platform"


def test_a_publishing_carrier_outranks_a_non_publishing_one(tmp_path: Path) -> None:
    """Rung (i): the repo that publishes a coordinate for this package owns it.

    `_payload` used to hardcode `publishes_coordinate=True` for every repo, which made this rung
    constant and therefore unfalsifiable. Here `acme-identity` publishes nothing; it would still
    win on `repo_id`, so the assertion holds only if rung (i) really ran and really preferred the
    publisher. Note the rung is written `not publishes_coordinate` — dropping the `not` is the
    natural typo and it inverts the whole rung.
    """
    node = _ladder_owner(tmp_path, TIED_DEPTH_FLEET, publishes={"acme-identity": False})
    assert node.owning_repo_id == "acme-platform"


def test_repo_id_settles_a_contest_the_other_rungs_all_tie(tmp_path: Path) -> None:
    """Rung (iv): with every other rung tied, the lexicographically first `repo_id` owns it.

    This is the rung that makes the ladder TOTAL, and a total order is the whole reason the
    docstring on `ownership_rank` gives for the rung existing: without it the winner depends on
    the order rows came out of the carrier index, and two runs over one fleet can disagree. So the
    fixture ties rungs (i)-(iii) deliberately — this is the only test here whose answer changes
    when `repo_id`'s direction changes, because it is the only one that reaches rung (iv).
    """
    assert _ladder_owner(tmp_path, TIED_DEPTH_FLEET).owning_repo_id == "acme-identity"


def test_the_shallower_carrier_wins_even_against_a_busier_one(tmp_path: Path) -> None:
    """Rung (ii), and rung (ii)'s POSITION — deliberately one test, not two.

    `acme-platform`'s copy is 3 directories shallower; `acme-identity`'s is deeper and has 9
    commits against 1. The ladder is `(publishes, depth, -commits, repo_id)`, so depth answers
    first and the shallow carrier wins anyway. Rungs (i) and (iv) are tied and unreached — and
    `repo_id`, if it were reached, would crown the other repo.

    Why the two properties share one test: the depth rung's direction and its position are not
    independently discriminable. A fixture that asserts only "shallowest wins" (depths differing,
    commits tied) reddens under exactly the mutations that redden this one, MINUS the rung-order
    swap — its mutation set is a strict subset of this one's, measured. Shipping both would add a
    case that is the unique discriminator of nothing, which is the failure mode CLAUDE.md's
    coverage-in-discrimination rule names. So the weaker fixture was deleted and this one keeps
    its work.

    Quantity watched: `owning_repo_id`, which IS `_owner`'s `ranked[0].repo_id` — the output of
    the sort under test. Depth is the only rung that can decide this pair while the ladder is
    intact, so no defect in it can leave that quantity unchanged; and because depth and
    `commit_count` disagree here, a defect that merely reorders the two rungs cannot either.
    """
    node = _ladder_owner(tmp_path, LADDER_FLEET, commit_counts={"acme-identity": 9})
    assert node.owning_repo_id == "acme-platform"


def test_a_contract_no_repo_owns_is_not_invented_as_a_node(tmp_path: Path) -> None:
    """A package carried ONLY as generated output belongs to somebody else's fleet.

    §3.1 5b (ii) is what makes this decidable: generated code is evidence of consumption, never of
    ownership. Without the rule, the twelve repos holding a checked-in copy of a third party's
    proto each look like a candidate owner, and the DAG gains a node that no repo can migrate and
    no wave can ever complete.
    """
    fleet_files = {
        "acme-billing": {
            "src/generated/vendorapi/v1/vendor.proto": VENDOR_PROTO,
            "package.json": _pkg("@acme/billing"),
        },
        "acme-reporting": {
            "src/generated/vendorapi/v1/vendor.proto": VENDOR_PROTO,
            "package.json": _pkg("@acme/reporting"),
        },
    }
    output = _run(_payload(tmp_path, fleet_files))
    assert output.contracts == (), "a generated-only package must not become a node"
    assert output.collisions == ()


def test_an_avro_schema_resolves_to_a_real_contract(tmp_path: Path) -> None:
    """An `.avsc` under `idl_globs` now produces a real `AVRO` contract, not an unidentifiable path.

    §3.1 5b keys `AVRO` on the schema's bare `namespace` field (not `namespace.name`) and step 4
    now parses it (`symbolindex._avro_symbols`), so `_index_symbols` has a real `SymbolKind.MODULE`
    row to key `_identify` off. This replaces the old
    `test_an_avro_schema_is_reported_rather_than_guessed_at`, whose premise (no `.avsc` parser
    exists, so the file is reported in `unidentifiable_idl_paths` instead of guessed at) is exactly
    what this fix retires.
    """
    output = _run(
        _payload(
            tmp_path,
            {"acme-events": {"schema/user.avsc": '{"namespace": "acme.events", "name": "User"}'}},
        )
    )
    assert output.unidentifiable_idl_paths == ()
    nodes = _by_id(output)
    assert list(nodes) == ["avro:acme.events"]
    node = nodes["avro:acme.events"]
    assert (node.kind, node.identifier) == (ContractKind.AVRO, "acme.events")


def test_an_avro_idl_namespace_annotation_resolves_to_a_real_contract(tmp_path: Path) -> None:
    """`.avdl`'s `@namespace("...")` annotation is a different syntax path than `.avsc`'s JSON
    field and must be independently proven — `_avro_symbols` dispatches on suffix."""
    avdl = '@namespace("acme.mail") protocol Mail {\n  record Message { string body; }\n}\n'
    output = _run(_payload(tmp_path, {"acme-mail": {"schema/mail.avdl": avdl}}))
    nodes = _by_id(output)
    assert list(nodes) == ["avro:acme.mail"]
    assert nodes["avro:acme.mail"].identifier == "acme.mail"


def test_a_thrift_namespace_prefers_the_star_slot_over_a_per_language_one(tmp_path: Path) -> None:
    """§3.1 5b (i)'s THRIFT tie-break: the `*` (all-languages) slot wins even though a
    lexicographically-earlier per-language slot is also present — proves PRECEDENCE, not just
    that a `*` slot is readable at all."""
    thrift = "namespace java com.acme.events\nnamespace * acme.events.thrift\n"
    output = _run(_payload(tmp_path, {"acme-events": {"idl/events.thrift": thrift}}))
    nodes = _by_id(output)
    assert list(nodes) == ["thrift:acme.events.thrift"]
    assert nodes["thrift:acme.events.thrift"].identifier == "acme.events.thrift"


def test_a_thrift_namespace_with_no_star_slot_picks_the_lexicographically_first(
    tmp_path: Path,
) -> None:
    """The actual discriminating case (Rule 12): with no `*` slot, two per-language scopes whose
    VALUES differ. A fixture with only one slot cannot tell "pick by the ordering rule" apart from
    "just pick whatever slot exists" — this one can, because `java` sorts before `py` and the two
    slots carry different values."""
    thrift = "namespace py acme.events.py\nnamespace java acme.events.java\n"
    output = _run(_payload(tmp_path, {"acme-events": {"idl/events.thrift": thrift}}))
    nodes = _by_id(output)
    assert list(nodes) == ["thrift:acme.events.java"]
    assert nodes["thrift:acme.events.java"].identifier == "acme.events.java"


def test_an_idl_file_with_no_namespace_falls_back_to_unpackaged(tmp_path: Path) -> None:
    """An `.avsc`/`.thrift` with no namespace declared falls into the existing
    `_unpackaged.<repo_id>.<dir>` path unmodified — `_identify` itself was never touched by this
    fix, so the shared fallback must keep working for AVRO/THRIFT exactly as it already does for
    PROTO."""
    output = _run(
        _payload(
            tmp_path,
            {
                "acme-a": {"schema/user.avsc": '{"type": "record", "name": "User"}'},
                "acme-b": {"idl/events.thrift": "struct Ping {\n  1: string id\n}\n"},
            },
        )
    )
    ids = sorted(_by_id(output))
    assert ids == ["avro:_unpackaged.acme-a.schema", "thrift:_unpackaged.acme-b.idl"]


# =======================================================================================
# 3 — extractability
# =======================================================================================


def test_a_proto_beside_implementation_code_is_rejected_with_its_reason(tmp_path: Path) -> None:
    """The clean-subtree predicate fails, `extractable` is 0, and the REASON is on the row.

    §3.1 5b (vi): "a `.proto` sitting beside `Service.java` is not extractable because the hoist
    would have to split a directory". The recorded predicate is the load-bearing half — an
    operator reading `fleet contracts inspect` cannot act on a bare `extractable = 0`, and the
    predicate order is itself a contract (this contract passes `min_consumers` first, so
    `clean_subtree` is genuinely the first failure rather than an accident of evaluation order).
    """
    fleet_files = {
        "acme-identity": {
            "proto/identity.proto": IDENTITY_PROTO,
            "proto/IdentityService.java": "class IdentityService {}\n",
            "package.json": _pkg("@acme/identity"),
        },
        "acme-billing": {IDENTITY_BINDING: _binding("acme.identity.v1")},
        "acme-reporting": {IDENTITY_BINDING: _binding("acme.identity.v1")},
    }
    node = _by_id(_run(_payload(tmp_path, fleet_files)))[PROTO_ID]
    assert node.consumer_repo_ids == ["acme-billing", "acme-reporting"]
    assert node.extractable is False
    assert node.status is ContractStatus.REJECTED
    assert node.status_detail == "clean_subtree"
    assert node.hoist_target_path is None, "§6's CHECK: no target path without extractability"


def test_a_contract_with_one_consumer_is_rejected_before_anything_else(tmp_path: Path) -> None:
    """`min_consumers` is the first predicate: a contract nobody else uses is not shared.

    Hoisting it would add a node and an ordering constraint while freeing nothing — the migration
    gets strictly longer for no reduction in coupling.
    """
    fleet_files = {
        "acme-identity": {IDENTITY_SOURCE: IDENTITY_PROTO},
        "acme-billing": {IDENTITY_BINDING: _binding("acme.identity.v1")},
    }
    node = _by_id(_run(_payload(tmp_path, fleet_files)))[PROTO_ID]
    assert node.consumer_repo_ids == ["acme-billing"]
    assert (node.extractable, node.status_detail) == (False, "min_consumers")


def test_a_proto_with_no_package_is_never_merged_with_another(tmp_path: Path) -> None:
    """Two package-less protos get two `_unpackaged.*` ids, and neither is extractable.

    §3.1 5b (i) forces `extractable = 0` for the fallback identity. The reason is collapse: every
    package-less proto in the fleet would otherwise share one identifier, and 5b (iii) would union
    their source files into a single node whose hoist moves unrelated files into one directory.
    """
    bare = 'syntax = "proto3";\n\nmessage Ping {\n  string id = 1;\n}\n'
    output = _run(
        _payload(
            tmp_path,
            {"acme-a": {"idl/ping.proto": bare}, "acme-b": {"idl/ping.proto": bare}},
        )
    )
    ids = sorted(_by_id(output))
    assert ids == ["proto:_unpackaged.acme-a.idl", "proto:_unpackaged.acme-b.idl"]
    assert all(node.status_detail in ("min_consumers", "identifier") for node in output.contracts)
    assert not any(node.extractable for node in output.contracts)


def test_a_symbol_only_consumer_gets_no_contract_consume_edge(tmp_path: Path) -> None:
    """TWO known gaps, pinned so neither can be mistaken for working behaviour.

    Gap 1 — the one named in the task. §3.1 5b (v) counts a repo that *references*
    `<identifier>.<Symbol>` as a consumer, but `ContractNode` has nowhere to record WHERE that
    reference lives, and `infer_contract_edges` emits one `CONTRACT_CONSUME` per evidence *path*.
    So such a repo is a consumer with no edge: the hoist does not free it and it stays ordered
    behind the owner. That is the safe direction (over-ordering is safe, under-ordering is not)
    and it is not papered over — filing the consumer's own hand-written file under
    `generated_paths` would mark it for DELETION in §3.3, which is worse than a missing edge.
    Closing it means adding a consumer-evidence field to `ContractNode`, at which point this test
    must be rewritten to assert the edge.

    Gap 2, found while writing gap 1: **no shipped extractor emits a non-definition API symbol at
    all.** `symbolindex._proto_symbols` marks every `GRPC_SERVICE`/`PROTO_MESSAGE` it finds as a
    definition, and nothing anywhere emits `HTTP_OPERATION` — so §3.1 5b (v)'s symbol join is
    currently unreachable from real scan data. The row below is therefore constructed rather than
    extracted, and that is the point: it exercises the rule the worker implements while recording
    that step 4 cannot yet supply its input.
    """
    from fleet.graph.infer import infer_contract_edges

    fleet_files = {
        "acme-identity": {IDENTITY_SOURCE: IDENTITY_PROTO},
        "acme-reporting": {IDENTITY_BINDING: _binding("acme.identity.v1")},
        "acme-audit": {"src/audit.ts": "export const audited = true;\n"},
    }
    payload = _payload(tmp_path, fleet_files)
    assert not [
        symbol for symbol in payload.symbols if symbol.kind is SymbolKind.GRPC_SERVICE
        and not symbol.is_definition
    ], "gap 2: if step 4 starts emitting references, build this fixture from a real file"

    reference = SymbolRef(
        repo_id="acme-audit",
        fqn="acme.identity.v1.IdentityService",
        kind=SymbolKind.GRPC_SERVICE,
        path="src/audit.ts",
        line=1,
        language="typescript",
        is_definition=False,
    )
    node = _by_id(_run(payload.model_copy(update={"symbols": (*payload.symbols, reference)})))[
        PROTO_ID
    ]
    assert node.consumer_repo_ids == ["acme-audit", "acme-reporting"]
    consumed = {
        edge.src_id
        for edge in infer_contract_edges([node])
        if edge.kind is EdgeKind.CONTRACT_CONSUME
    }
    assert consumed == {"acme-reporting"}, "the symbol-only consumer has no evidence path to cite"


# =======================================================================================
# 4 — the payoff: discovery feeding the cycle breaker
# =======================================================================================


def _cycle_edges(payload: ContractsInput) -> InferenceInput:
    """Step 5's real inference over the cycle fleet's real symbols and declared manifest.

    The manifest facts are stated rather than parsed — `interrogate` is covered elsewhere and its
    output is a `ManifestDependency` either way — but the IMPORT symbol that closes the cycle is
    the one `symbolindex` really extracted from the generated binding on disk.
    """
    owners = OwnerIndex.from_published(
        [
            ("acme-identity", Coordinate(ecosystem=Ecosystem.NPM, group="@acme", name="identity")),
            ("acme-billing", Coordinate(ecosystem=Ecosystem.NPM, group="@acme", name="billing")),
            (
                "acme-reporting",
                Coordinate(ecosystem=Ecosystem.NPM, group="@acme", name="reporting"),
            ),
        ]
    )
    billing = Coordinate(
        ecosystem=Ecosystem.NPM, group="@acme", name="billing", version_spec="^1.0.0"
    )
    return InferenceInput(
        owners=owners,
        dependencies=(
            ManifestDependency(
                manifest=ManifestRef(
                    repo_id="acme-identity",
                    path="package.json",
                    ecosystem=Ecosystem.NPM,
                    adapter="npm",
                    adapter_version=1,
                    sha256="0" * 64,
                ),
                raw=RawDependency(raw_id="@acme/billing", version_spec="^1.0.0"),
                coordinate=billing,
            ),
        ),
        symbols=payload.symbols,
    )


def test_hoisting_the_discovered_contract_dissolves_the_two_repo_cycle(tmp_path: Path) -> None:
    """Discovery → `break_cycles` → the SCC is gone, and **no edge was broken**.

    THE test this whole module exists for. `acme-identity` declares a dependency on
    `acme-billing`; `acme-billing`'s only path back is an import inside the *generated* binding
    for `acme.identity.v1`. That is a cycle in the repo graph and it is not a real one: once the
    proto package is its own node, billing depends on the contract (a sink) rather than on the
    owner, and the loop is severed with every edge's evidence intact.

    Both halves were separately green before this test and had never met: `infer_contract_edges`
    could convert a `ContractNode` and nothing built one; `cycles._materialize` retargets by
    `evidence_path` and nothing filled `generated_paths`. A regression in either produces
    `ATOMIC_WAVE` here — two repos migrating as one PR instead of three independent ones.
    """
    payload = _payload(tmp_path, CYCLE_FLEET)
    edges = infer_edges(_cycle_edges(payload))
    node = _by_id(_run(payload))[PROTO_ID]
    assert node.status is ContractStatus.EXTRACTABLE, node.status_detail

    cfg = GraphSection()
    graph = build_graph(
        [GraphNode(kind=NodeKind.REPO, node_id=repo.repo_id) for repo in payload.repos],
        edges,
        dag_edge_kinds=cfg.dag_edge_kinds,
        min_confidence=cfg.min_confidence,
        hoist_contracts=True,
    )
    report = break_cycles(graph, contracts=[node], config=cfg)
    assert len(report.resolutions) == 1, "the fixture must really be cyclic before the hoist"
    resolution = report.resolutions[0]
    assert resolution.break_strategy is BreakStrategy.CONTRACT_HOIST
    assert resolution.hoisted_contract_ids == (PROTO_ID,)
    assert resolution.broken_edge_keys == (), "hoisting must not also break an edge"
    assert [c.contract_id for c in report.hoisted_contracts] == [PROTO_ID]

    import networkx as nx

    sizes = {
        len(component)
        for component in nx.strongly_connected_components(report.graph.G_dag)
    }
    assert sizes == {1}, "every SCC must be trivial once the contract is hoisted"


# =======================================================================================
# 5 — determinism
# =======================================================================================


def discovery_fingerprint() -> str:
    """Discovery over a fixed fleet, as one hashable string. Importable by the subprocess."""
    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        payload = _payload(Path(raw), SHARED_FLEET)
        listings = {
            repo.repo_id: tuple(
                sorted(
                    path.relative_to(Path(repo.worktree_path)).as_posix()
                    for path in Path(repo.worktree_path).rglob("*")
                    if path.is_file()
                )
            )
            for repo in payload.repos
        }
        output = discover_contracts(payload, listings)
    return _stable(output)


@pytest.mark.parametrize("seed", ["0", "1", "12345"])
def test_discovery_is_identical_under_a_different_hash_seed(seed: str) -> None:
    """A real subprocess, because `PYTHONHASHSEED` is fixed at interpreter start.

    `contract_id` is hashed into `edge_key` and into `run_digest` (§11.6, §12.21: two runs from a
    clean database must agree byte for byte). Every index in `discover_contracts` is built from
    sorted input for exactly this reason; a single `set` iteration reaching the output would make
    a re-scan reorder `source_paths` and change `content_sha256` for a fleet that did not change.
    """
    env = {
        **os.environ,
        "PYTHONHASHSEED": seed,
        "PYTHONPATH": os.pathsep.join([str(REPO_ROOT / "src"), str(REPO_ROOT)]),
    }
    probe = (
        "from tests.test_workers_contracts import discovery_fingerprint;"
        "print(discovery_fingerprint())"
    )
    out = subprocess.run(  # noqa: S603
        [sys.executable, "-c", probe], env=env, capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == discovery_fingerprint()


# =======================================================================================
# 6 — through the CLI
# =======================================================================================

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
  # vetted to succeed under a REAL native build (no `"test"` script, some declare cross-repo
  # dependency names unpublished by design). Leg C's own red-path gate turns that pre-existing
  # native-baseline failure into a real `phases.status = 'SKIPPED'`, corrupting this file's own
  # contract-wiring assertions, which have nothing to do with baseline behavior. Disabled here
  # entirely for the same reason `tests/test_scan_e2e.py`'s own `FLEET_YAML` disables it.
  baseline_build:
    enabled: false
"""
#: §11.3/§12.22: the concurrency/verify/budgets keys above are lowered the same way
#: `min_free_bytes` is -- see the note on `tests/test_scan_e2e.py`'s `FLEET_YAML`.
"""No `graph:` section at all: contract hoisting is ON by default, which is precisely the
configuration `fleet scan` used to refuse to run under."""


@pytest.fixture
def cli_fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Three real git repositories sharing one proto package, plus a config bundle and a db."""
    sources = {
        name: _make_repo(tmp_path / "sources", name, dict(files))
        for name, files in SHARED_FLEET.items()
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


def test_fleet_scan_extracts_contracts_through_the_cli(cli_fleet: Path) -> None:
    """`fleet scan` runs step 5b instead of refusing, and the ROWS are in the database.

    The refusal this replaces was correct while 5b had no implementation — a graph with no
    contract nodes is indistinguishable from a fleet that shares none. The assertion is on
    `contracts`/`collisions` rather than on stdout for the same reason: a summary line is not
    evidence, and every earlier checkpoint in this project could print a total it never wrote.
    """
    result = runner.invoke(
        app,
        [
            "--config",
            str(cli_fleet / "config" / "fleet.yaml"),
            "--db",
            str(cli_fleet / "state/fleet.db"),
            "scan",
            "--skip-classify",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == ExitCode.SUCCESS, result.output

    conn = sqlite3.connect(cli_fleet / "state" / "fleet.db")
    try:
        rows = conn.execute(
            "SELECT contract_id, kind, owning_repo_id, consumer_repo_ids, extractable, status, "
            "       hoist_target_path FROM contracts"
        ).fetchall()
        collisions = conn.execute(
            "SELECT kind, key, repo_ids, severity FROM collisions"
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == 1, f"expected exactly one contract row, got {rows}"
    contract_id, kind, owner, consumers, extractable, status, target = rows[0]
    assert (contract_id, kind, owner) == (PROTO_ID, "PROTO", "acme-identity")
    assert json.loads(consumers) == ["acme-billing", "acme-reporting"]
    assert (extractable, status, target) == (1, "EXTRACTABLE", "proto/acme/identity/v1")
    assert [(c[0], c[1], json.loads(c[2]), c[3]) for c in collisions] == [
        ("CONTRACT", PROTO_ID, ["acme-billing", "acme-identity"], "warn")
    ]

    # and the read-only verb an operator actually uses can render what 5b wrote: `fleet contracts`
    # was shipped against a table nothing had ever populated, so its SELECT had never met a row.
    listed = runner.invoke(
        app,
        [
            "--config",
            str(cli_fleet / "config" / "fleet.yaml"),
            "--db",
            str(cli_fleet / "state/fleet.db"),
            "--json",
            "contracts",
            "list",
            "--extractable-only",
        ],
        catch_exceptions=False,
    )
    assert listed.exit_code == ExitCode.SUCCESS, listed.output
    assert json.loads(listed.stdout)[0]["contract_id"] == PROTO_ID


def test_skip_contracts_still_means_what_it_says(cli_fleet: Path) -> None:
    """`--skip-contracts` writes no `contracts` row — the flag disables the step, not the check.

    §3.1 5b names the skip explicitly ("in which case every node in the graph is a repo"), and a
    flag that parses and then does nothing is worse than an absent one: the operator reads the run
    as having honoured it.
    """
    result = runner.invoke(
        app,
        [
            "--config",
            str(cli_fleet / "config" / "fleet.yaml"),
            "--db",
            str(cli_fleet / "state/fleet.db"),
            "scan",
            "--skip-classify",
            "--skip-contracts",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == ExitCode.SUCCESS, result.output

    conn = sqlite3.connect(cli_fleet / "state" / "fleet.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM contracts").fetchone()[0] == 0
    finally:
        conn.close()
