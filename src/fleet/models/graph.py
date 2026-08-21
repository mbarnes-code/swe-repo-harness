"""Graph nodes, edges, contracts, cycles, collisions, waves (SPEC §5.3)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Annotated, Final, Literal

from pydantic import Field, model_validator

from fleet.models.base import FleetModel, utcnow
from fleet.models.enums import (
    CONTRACT_EDGE_KINDS,
    BreakStrategy,
    ContractKind,
    ContractStatus,
    EdgeKind,
    NodeKind,
    SymbolKind,
)
from fleet.models.repo import Coordinate, RepoId
from fleet.util.hashing import sha256_text

ContractId = Annotated[str, Field(min_length=3, pattern=r"^[a-z_]+:[a-z0-9._/:-]+$")]
NodeId = RepoId | ContractId

EdgeKey = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
"""THE logical primary key of an edge, and the ONLY form in which one model may reference an
edge belonging to another. Content-derived, so it survives the graph being rebuilt from `edges`
on resume (ADR-0004); a rowid does not, and a cycle-break decision recorded against a reassigned
rowid silently points at a different edge — unauditable and un-rollbackable.

Derived by `edge_key_for()` below, which is the ONE definition of the recipe in the system: §6's
DDL, the v007 back-fill, and inference all call it rather than restate it."""

_NUL: Final = "\x00"

NO_LINE: Final = -1
"""What a missing `evidence_line` hashes as. §6 declares `edges.evidence_line INTEGER NOT NULL
DEFAULT -1` — "-1, not NULL: it is part of the key" — so the derivation must agree with the
column, or a row would re-key on its way through SQLite."""

EDGE_KEY_COLUMNS: Final[tuple[str, ...]] = (
    "src_kind", "src_id", "dst_kind", "dst_coord_key", "kind", "evidence_path", "evidence_line",
)
"""The `edges` columns that ARE the preimage, in hash order (§6 `UNIQUE (run_id, …)`).

`run_id` is deliberately absent. It partitions *rows* — `edges` is per-run and CASCADEs with its
run — but it does not identify an *edge*: hashing it would give the same edge two keys in two
runs, which breaks §11.6 outright (`CycleFinding.broken_edge_keys` is a digest input, and §12.21
requires two runs from a clean database to produce a byte-identical `run_digest`) and re-creates,
one level up, the very instability ADR-0026 introduced these keys to remove."""


def edge_key_for(
    *,
    src_kind: str,
    src_id: str,
    dst_kind: str,
    dst_ref: str,
    kind: str,
    evidence_path: str,
    evidence_line: int | None,
) -> str:
    """THE `edge_key` recipe. Everything else in the system calls this or cites it.

    sha256 over `(src_kind, src_id, dst_kind, dst_ref, kind, evidence_path, evidence_line)`,
    NUL-joined, where `dst_ref` is `dst_coordinate.key` for a REPO dst and the `contract_id` for a
    CONTRACT dst — which is exactly what §6 stores in `edges.dst_coord_key`.

    Semantics only: no rowid, no `run_id`, no timestamp, no insertion order, so the key survives
    the graph being rebuilt from `edges` on resume (ADR-0004) *and* is the same string in every
    run that infers the same edge. `dst_kind` is hashed even though today's `kind` implies it —
    a key whose collision-freedom depends on a CHECK constraint in another layer holding is a key
    with a hidden premise, and `Coordinate.key` and `contract_id` are both `:`-separated lowercase
    tokens drawn from two independently-extensible enums.
    """
    return sha256_text(
        _NUL.join(
            [
                src_kind,
                src_id,
                dst_kind,
                dst_ref,
                kind,
                evidence_path,
                str(NO_LINE if evidence_line is None else evidence_line),
            ]
        )
    )


def edge_key_from_row(values: Sequence[object]) -> str:
    """`edge_key_for` over one `edges` row's `EDGE_KEY_COLUMNS` values, in that order.

    The adapter the SQL side uses — the v007 back-fill registers it as a SQLite function, and the
    §6 drift guard feeds it the columns it reads back out of the DDL's UNIQUE tuple. It converts,
    it does not re-derive: there is still exactly one recipe.
    """
    if len(values) != len(EDGE_KEY_COLUMNS):
        raise ValueError(f"expected {len(EDGE_KEY_COLUMNS)} values, got {len(values)}")
    text = ["" if value is None else str(value) for value in values]
    return edge_key_for(
        src_kind=text[0],
        src_id=text[1],
        dst_kind=text[2],
        dst_ref=text[3],
        kind=text[4],
        evidence_path=text[5],
        evidence_line=None if values[6] is None else int(text[6]),
    )

SccId = Annotated[str, Field(pattern=r"^scc:[0-9a-f]{16}$")]
"""`"scc:" + sha256("\\x00".join(sorted(members))).hexdigest()[:16]`. Derived from the member
set, not from `min(repo_ids)`: an int "derived from" strings is either `hash()` — randomized per
process under PYTHONHASHSEED — or undefined. Membership change yields a NEW id (see
`CycleFinding.superseded_by`) rather than renumbering an SCC an in-flight PR already names."""

DAG_EDGE_KINDS: frozenset[EdgeKind] = frozenset(
    {EdgeKind.DECLARED_DEP, EdgeKind.PUBLISHED_ARTIFACT, EdgeKind.INTERNAL_IMPORT,
     EdgeKind.API_CONTRACT, EdgeKind.CONTRACT_IMPL, EdgeKind.CONTRACT_CONSUME}
)  # SHARED_RESOURCE and DYNAMIC_REF are advisory, excluded from ordering by default (ADR-0018).
# The two CONTRACT_* kinds are ordering edges by construction: they exist only because a
# contract was hoisted, and the hoist is exactly a statement about migration order (ADR-0019).


class GraphNode(FleetModel):
    """A DAG node. `kind` decides which table `node_id` addresses (ADR-0019). This is the only
    node abstraction in the system: there is no per-symbol node, because the symbol index is
    unbounded and the contract set is not."""

    kind: NodeKind = NodeKind.REPO
    node_id: NodeId

    @property
    def key(self) -> tuple[str, str]:
        """The networkx node identity. Total order, so layering is reproducible."""
        return (self.kind.value, self.node_id)


class DependencyEdge(FleetModel):
    """(src_kind, src_id) depends on (dst_kind, dst_id). Persisted to `edges`; the DAG is built
    from this table on demand and never stored (ADR-0004)."""

    edge_id: int | None = Field(
        default=None,
        description="SQLite rowid; None before insert. LOCAL and NON-PORTABLE — reassigned when "
        "the graph is rebuilt on resume, and therefore FORBIDDEN in any cross-model reference. "
        "Use `edge_key`. A `list[int]` naming edges is a schema bug (§12).",
    )
    edge_key: EdgeKey = Field(
        description="THE logical PK, derived by `edge_key_for()` — the one definition of the "
        "recipe (see it and `EDGE_KEY_COLUMNS` above; §6's UNIQUE tuple is (run_id, "
        "*EDGE_KEY_COLUMNS) and its back-fill calls the same function). Stable across runs, "
        "across rebuilds, and across a re-scan that reorders insertion.",
    )
    src_kind: NodeKind = NodeKind.REPO
    src_id: NodeId = Field(description="repo_id when src_kind is REPO, else a contract_id")
    dst_kind: NodeKind = NodeKind.REPO
    dst_coordinate: Coordinate | None = Field(
        default=None, description="Required iff dst_kind is REPO; a contract has no Coordinate"
    )
    dst_id: NodeId | None = Field(
        default=None,
        description="Resolved dst node: repo_id (owner of dst_coordinate) or contract_id. "
        "None with dst_kind=REPO → external dependency, orders nothing.",
    )
    dst_candidate_repo_ids: list[RepoId] = Field(
        default_factory=list,
        description="All owners when a coordinate is published by >1 repo; drives `ambiguous`",
    )
    retargeted_from_repo_id: RepoId | None = Field(
        default=None,
        description="Pre-hoist dst repo when this row was retargeted to a contract node "
        "(§3.1 5b viii). The rollback record: restoring it un-hoists the edge exactly.",
    )
    kind: EdgeKind
    version_spec: str | None = None
    base_confidence: float = Field(ge=0.0, le=1.0, description="EdgeKind base, pre-modifier")
    confidence: float = Field(ge=0.0, le=1.0, description="base × Π confidence_factors, clamped")
    confidence_factors: dict[str, float] = Field(
        default_factory=dict,
        description="Every applied modifier, e.g. {'vendored': 0.4, 'open_range': 0.9}. "
        "The score must be reconstructible from this dict alone (§3.1 step 5).",
    )
    ambiguous: bool = Field(default=False, description="dst coordinate has >1 candidate owner")
    ordering_suppressed: bool = Field(
        default=False, description="Broken as a cycle feedback edge; evidence retained (§3.1 6d)"
    )
    evidence_path: str = Field(min_length=1, description="Repo-relative path proving this edge")
    evidence_line: int | None = Field(default=None, ge=1)
    detected_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _node_shape(self) -> DependencyEdge:
        if (self.dst_kind, self.dst_id) == (self.src_kind, self.src_id):
            raise ValueError(f"self-edge on {self.src_kind}:{self.src_id}")
        if self.dst_kind is NodeKind.REPO and self.dst_coordinate is None:
            raise ValueError("a REPO dst edge must carry dst_coordinate")
        if self.dst_kind is NodeKind.CONTRACT and self.dst_id is None:
            raise ValueError("a CONTRACT dst edge must resolve to a contract_id")
        if (self.kind in CONTRACT_EDGE_KINDS) != (self.dst_kind is NodeKind.CONTRACT):
            raise ValueError(f"{self.kind} is valid iff dst_kind is CONTRACT")
        if self.src_kind is NodeKind.CONTRACT and self.dst_kind is not NodeKind.CONTRACT:
            raise ValueError("a contract node has no outbound edge into a repo (§3.1 5b vii)")
        return self

    @property
    def is_internal(self) -> bool:
        """True when this edge addresses a node inside the fleet — a resolved repo, or any
        contract (contracts are internal by construction: they are carved out of the fleet)."""
        return self.dst_id is not None

    def orders_migration(self, min_confidence: float = 0.5) -> bool:
        """The single predicate the sequencer uses. Low confidence is recorded and reported,
        but never orders migration (§3.1 step 5)."""
        return (
            self.is_internal
            and self.kind in DAG_EDGE_KINDS
            and not self.ordering_suppressed
            and self.confidence >= min_confidence
        )


class SymbolRef(FleetModel):
    """One entry in the cross-repo symbol index (Constraint 4). Persisted to `symbols`."""

    symbol_id: int | None = None
    repo_id: RepoId
    fqn: str = Field(min_length=1, description="Fully-qualified name, ecosystem-normalized")
    kind: SymbolKind
    path: str
    line: int = Field(ge=1)
    language: str
    is_definition: bool = Field(description="True = defined here; False = referenced here")
    exported: bool = False


class ContractNode(FleetModel):
    """A declared unit of shared interface that can be hoisted out of its owning repo and
    migrated as its own DAG node (ADR-0019, §3.1 step 5b). Persisted to `contracts`."""

    contract_id: ContractId = Field(
        description="'{kind.lower()}:{identifier}', case-folded. THE node id and the row PK; "
        "nine repos vendoring one proto package collapse to one value."
    )
    kind: ContractKind
    identifier: str = Field(min_length=1, description="proto package / namespace / OpenAPI slug")
    owning_repo_id: RepoId | None = Field(
        default=None, description="Chosen by the §3.1 5b (iv) ladder; None only while DETECTED"
    )
    source_paths: list[dict[str, str]] = Field(
        default_factory=list,
        description="[{repo_id, path, blob_sha}] over every carrier; generated files excluded",
    )
    generated_paths: list[dict[str, str]] = Field(
        default_factory=list,
        description="[{repo_id, path}] checked-in generated output; DELETED, not migrated (§3.3)",
    )
    consumer_repo_ids: list[RepoId] = Field(
        default_factory=list,
        description="Denormalized convenience copy; the indexed truth is `edges` (§3.1 5b v)",
    )
    extractable: bool = Field(
        default=False, description="Passed every §3.1 5b (vi) predicate; only these may be hoisted"
    )
    extraction_confidence: float = Field(
        default=0.0, ge=0.0, le=1.0, description="0.9 × Π confidence_factors, clamped"
    )
    confidence_factors: dict[str, float] = Field(
        default_factory=dict,
        description="Every applied modifier, e.g. {'divergent': 0.5}. The score must be "
        "reconstructible from this dict alone, exactly as for DependencyEdge.",
    )
    content_sha256: str = Field(
        default="", description="sha256 of the sorted DISTINCT source blob SHAs; one distinct "
        "SHA ⇒ every vendored copy is byte-identical ⇒ they collapse with no penalty"
    )
    hoist_target_path: str | None = Field(
        default=None, description="layout() of this node (§3.3); non-null whenever extractable"
    )
    status: ContractStatus = ContractStatus.DETECTED
    status_detail: str = Field(
        default="", description="e.g. the failing predicate name behind REJECTED"
    )
    detected_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _hoistable_is_substantiated(self) -> ContractNode:
        if self.extractable and (self.hoist_target_path is None or self.owning_repo_id is None):
            raise ValueError(f"{self.contract_id}: extractable needs an owner and a target path")
        if (
            self.status in (ContractStatus.HOISTED, ContractStatus.MIGRATED)
            and not self.extractable
        ):
            raise ValueError(f"{self.contract_id}: hoisted a non-extractable contract")
        return self

    @property
    def node(self) -> GraphNode:
        return GraphNode(kind=NodeKind.CONTRACT, node_id=self.contract_id)


class CycleFinding(FleetModel):
    """A non-trivial SCC and **how it was broken** (§3.1 step 6). Emitted rather than resolved
    silently (ADR-0013 Phase 1), but never merely detected: `break_strategy` is mandatory."""

    scc_id: SccId = Field(description="Content-derived over `members`; see the SccId docstring")
    members: list[RepoId] = Field(min_length=2)
    edges: list[EdgeKey] = Field(description="All intra-SCC edge_keys")
    feedback_edge_keys: list[EdgeKey] = Field(
        default_factory=list, description="BACK edges from the deterministic DFS (step 6b)"
    )
    proposed_break_edge_key: EdgeKey | None = Field(
        default=None, description="Cheapest break_cost tuple (step 6c)"
    )
    superseded_by: SccId | None = Field(
        default=None,
        description="Set when a re-scan changes this SCC's membership: the finding is superseded "
        "by the new scc_id rather than mutated in place, so the decision an already-open "
        "ATOMIC_WAVE PR was opened under stays readable (§3.1 step 6).",
    )
    hoisted_contract_ids: list[ContractId] = Field(
        default_factory=list,
        description="Contracts committed by 6c-H for this SCC, in the order hoisted. Non-empty "
        "with break_strategy EDGE_BREAK means hoisting helped but did not finish the job.",
    )
    broken_edge_keys: list[EdgeKey] = Field(
        default_factory=list, description="Edges actually set ordering_suppressed (step 6d)"
    )
    break_strategy: BreakStrategy = BreakStrategy.EDGE_BREAK
    atomic_wave_index: int | None = Field(
        default=None, ge=0, description="Set iff break_strategy is ATOMIC_WAVE"
    )
    rationale: str = Field(default="", description="Prose only; the ONLY LLM-writable field here")

    @model_validator(mode="after")
    def _strategy_is_substantiated(self) -> CycleFinding:
        if self.break_strategy is BreakStrategy.CONTRACT_HOIST and not self.hoisted_contract_ids:
            raise ValueError(f"scc {self.scc_id}: CONTRACT_HOIST with no hoisted contracts is "
                             "detection, not breaking")
        if self.break_strategy is BreakStrategy.CONTRACT_HOIST and self.broken_edge_keys:
            raise ValueError(f"scc {self.scc_id}: CONTRACT_HOIST means no edge was broken; "
                             "use EDGE_BREAK when 6d also had to run")
        if self.break_strategy is BreakStrategy.EDGE_BREAK and not self.broken_edge_keys:
            raise ValueError(f"scc {self.scc_id}: EDGE_BREAK with no broken edges is detection, "
                             "not breaking")
        if self.break_strategy is BreakStrategy.ATOMIC_WAVE and self.atomic_wave_index is None:
            raise ValueError(f"scc {self.scc_id}: ATOMIC_WAVE requires atomic_wave_index")
        return self


class CollisionFinding(FleetModel):
    """One row of the `collisions` table (§3.1 step 8). Detected before any transformation."""

    collision_id: int | None = None
    kind: Literal["COORDINATE", "CONTRACT", "DEST_PATH", "FILE_PATH", "DEP_VERSION"]
    key: str = Field(
        min_length=1, description="coord_key / contract_id / dest path / monorepo path"
    )
    repo_ids: list[RepoId] = Field(min_length=2)
    blob_shas: list[str] = Field(
        default_factory=list, description="FILE_PATH only; identical SHAs => safe dedupe"
    )
    severity: Literal["warn", "error"] = "warn"
    resolution: str | None = Field(
        default=None, description="Applied policy; NULL + severity=error fails `fleet sequence`"
    )
    detected_at: datetime = Field(default_factory=utcnow)


class MigrationWave(FleetModel):
    """One topological layer. All members are mutually independent and migrate in parallel.
    A wave holds repo nodes, contract nodes, or both; the earliest waves are usually
    contract-only, which is exactly how a hoisted contract migrates first (ADR-0019)."""

    wave_index: int = Field(ge=0)
    repo_ids: list[RepoId] = Field(default_factory=list)
    contract_ids: list[ContractId] = Field(
        default_factory=list, description="Contract nodes in this wave; built before any consumer"
    )
    depends_on_waves: list[int] = Field(default_factory=list)
    atomic_scc_ids: list[SccId] = Field(
        default_factory=list,
        description="SCCs migrating as one unit in this wave; members advance phases together",
    )
    synthetic: bool = Field(
        default=False,
        description="APPENDED to an already-sequenced plan rather than produced by the sequencing "
        "pass (§3.5), so `wave_index` is above every wave the plan then held — the first appended "
        "layer is `max(waves) + 1`. Mirrors `waves.synthetic`. Records HOW the wave was allocated, "
        "not why: the flag names no cause, so the projection can say a repo migrated outside its "
        "original layer but not what freed it.",
    )
    wave_started_at: datetime | None = Field(
        default=None,
        description="First admission into this wave. Persisted, and CUMULATIVE across resumes — "
        "`budgets.wave_max_wallclock_s` is measured from it, so a crash-loop cannot buy unbounded "
        "time by restarting the clock (§3.4). None until the wave is first entered.",
    )
    computed_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _wave_is_non_empty(self) -> MigrationWave:
        if not self.repo_ids and not self.contract_ids:
            raise ValueError(f"wave {self.wave_index} has no members")
        return self
